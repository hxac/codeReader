# 分层 prefill KV 缓冲复用（Layerwise Prefill KV Buffer Reuse）

## 1. 本讲目标

本讲讲解 PR #12852 引入的「分层 prefill KV 缓冲复用」。读完本讲你应当能够：

- 说清楚在 ascend_store 的 **layerwise prefill 卸载路径**下，为什么要把「每层独立分配的设备 KV 缓冲」改成「多个层分时复用一组有限物理缓冲」，以及这样做为什么能回收 HBM、放大可用 KV 容量。
- 读懂新增的 `LayerwiseCacheLayout` 数据结构，理解 `num_shared_buffers` / `prefetch_layer_map` / `storage_indices` / `has_layer_reuse` 如何把**逻辑层**映射到**有限物理缓冲**。
- 掌握 `apply_layerwise_kv_cache_plan` 在 KV cache 张量分配前重排描述符的时机，以及 `NPUWorker` 按 `num_layers / num_tensors` 因子放大 `available_kv_cache_memory_bytes` 的内存核算。
- 理解 `pool_worker` / `pool_scheduler` 在缓冲复用下「回载再复用、上一层保存完成后才复用」的时序，以及 `config_data` 的 partial GVA、`kv_transfer` 的错误传播（`raise` 替代 `log`）改动。
- 理解 `sfa_v1` 把 `record_attention_compute_start` 前移，使 GLM-5.2 等**无 indexer 的 SFA 层**也能打开 prefetch gate。

## 2. 前置知识

本讲是高级特性，默认你已读过以下两篇讲义：

- **u10-l2 PD 分离与 KV 传输连接器**：你需要知道 PD 分离（Prefill/Decode disaggregation）用 `kv_role`（`kv_producer` / `kv_consumer` / `kv_both`）区分角色，以及 `AscendStoreConnector` 是一个 **KV Pool 池化连接器**，支持 layerwise 逐层 put/get。本讲所有改动都发生在该连接器的 layerwise 数据面里。
- **u5-l2 MLA / SFA / DSA 与稀疏注意力**：你需要知道 SFA（Sparse Flash Attention）在 MLA 隐空间上叠加 indexer 选 top-k 块；有些层（如 GLM-5.2 的部分层）会**复用别的层算好的 top-k 索引**，因此自身没有 indexer、走 `skip_topk` 分支。

两个关键名词先统一：

- **逻辑层（logical layer）**：模型里第 \(i\) 个 transformer 层，在代码里表现为一个 `KVCacheTensor` 描述符（`shared_by=["model.layers.{i}.self_attn"]`）。
- **物理缓冲（physical buffer）**：真正在 NPU HBM 上分配的一块 KV 张量。未开复用时逻辑层与物理缓冲一一对应；开复用后多个逻辑层共享同一块物理缓冲。

还需要一点 MemCache 背景：layerwise 路径的 `backend=memcache` 用的是「全局虚拟地址（GVA）+ 批拷贝（`batch_copy`）」语义——保存时 `batch_alloc` 申请 GVA 再 `batch_copy` 把 HBM 拷到远端，加载时 `batch_get_key_info` 取回 GVA、`batch_add_lease` 拿读租约、再 `batch_copy` 把远端拷回 HBM。GVA 是这块缓冲在池里的「门牌号」。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `vllm_ascend/distributed/kv_transfer/kv_pool/ascend_store/layerwise_cache_layout.py` | **本讲主角**。定义 `LayerwiseCacheLayout` 布局规划，把逻辑层映射到有限物理缓冲；`apply_layerwise_kv_cache_plan` 在分配前重排张量描述。 |
| `vllm_ascend/worker/worker.py` | `NPUWorker.determine_available_memory` 中按 `num_layers / num_tensors` 因子放大可用 KV 显存。 |
| `vllm_ascend/worker/model_runner_v1.py` | `NPUModelRunner.initialize_kv_cache` 中调用 `apply_layerwise_kv_cache_plan`。 |
| `vllm_ascend/distributed/kv_transfer/kv_pool/ascend_store/pool_worker.py` | 每个 worker 子进程的 KV Pool 数据面：构造 layerwise 配置、按层提交 save/load、prefetch、save→load 时序。 |
| `vllm_ascend/distributed/kv_transfer/kv_pool/ascend_store/pool_scheduler.py` | 引擎核心侧的 KV Pool 调度器：hit 检查、命中 token 计算、决定每步 save 什么。 |
| `vllm_ascend/distributed/kv_transfer/kv_pool/ascend_store/kv_transfer.py` | 收发线程（`KVCacheStoreLayerSendingThread` / `KVCacheStoreLayerRecvingThread`）：真正的 `batch_copy`、`wait_for_save_layer`、错误捕获与传播。 |
| `vllm_ascend/distributed/kv_transfer/kv_pool/ascend_store/config_data.py` | 请求元数据：`partial_save_gvas_by_group` / `partial_load_gvas_by_group` 等 partial GVA 字段。 |
| `vllm_ascend/attention/sfa_v1.py` | 把 `record_attention_compute_start` 前移，使无 indexer 的 SFA 层也能打开 prefetch gate。 |
| `vllm_ascend/memcache_comm_fence.py` | `AttentionComputeStartGate`：计算流到达注意力边界时打开的栅栏，MemCache 线程等它再发起 H2D。 |

永久链接基址（当前 HEAD `7201c97a`）：

```
https://github.com/vllm-project/vllm-ascend/blob/7201c97a61a17425b558b6b5e53ab0d30ae8151d/
```

---

## 4. 核心概念与源码讲解

### 4.1 动机与整体设计：为什么让多个层复用一块缓冲

#### 4.1.1 概念说明

在 layerwise prefill 卸载场景（`backend=memcache` + `use_layerwise=True`）里，P 节点算 prefill 时，**一层算完就把这一层的 KV 卸载（D2H/L2G）到远端 MemCache 池**，D 节点 decode 前**一层一层地把 KV 回载（H2D）回 HBM**。这是为了支持超长上下文、跨节点复用 KV。

问题在于：默认情况下 vLLM 会为**每一个 transformer 层独立分配一块设备 KV 缓冲**。模型若有 27 层、每层缓冲容量是 `num_blocks × block_bytes`，那 HBM 里就要常驻 27 份。而 layerwise 卸载的本质是「**同一时刻只有正在算的那一层需要 KV 驻留 HBM**」——前一层算完、卸载出去之后，它的设备缓冲其实就空了，完全可以拿给后一层用。

「分层 prefill KV 缓冲复用」就是把这个观察落地：**不再为每层各分配一块缓冲，而是只分配有限的 \(K\) 块物理缓冲，让 \(N\) 个逻辑层按 round-robin 分时复用它们**。逻辑上每层仍「看起来」有完整容量的 KV，物理上 HBM 占用从 \(N\) 份降到约 \(K\) 份，回收的显存转成更大的 `num_blocks`，从而**放大可用 KV 容量**。

两个关键设计约束：

1. **逻辑布局不变**：模型前向代码、注意力后端看到的 KV cache 张量形状、按层寻址方式都不变。复用是「在分配前偷偷重排描述符」实现的，对上层透明。
2. **时序安全**：多层共享一块缓冲时，后一层要往缓冲里 load 新数据，必须等前一层把旧数据**保存完（卸载出 HBM）**——否则会把还没存走的数据冲掉。这正是后面「上一层保存完成后才复用」时序的由来。

#### 4.1.2 核心流程

整体分为「规划 → 分配 → 运行时」三个阶段：

```text
[规划阶段：构建 LayerwiseCacheLayout]
  读 additional_config 里的 layerwise_num_shared_buffers(=K) 等
  → build_layerwise_cache_layout(num_layers=N, extra_config)
  → 得到 storage_indices（哪些层共用哪块物理缓冲）
       prefetch_layer_map（层 X 的 load 要等哪一层 save 完成）
       has_layer_reuse（是否真的发生复用）

[分配阶段：放大预算 + 重排描述符]
  NPUWorker.determine_available_memory:
     available_kv_cache_memory_bytes *= (N / num_tensors)   # 因子放大
  NPUModelRunner.initialize_kv_cache:
     apply_layerwise_kv_cache_plan(kv_cache_config)         # 分配前重排张量描述
     → 把 N 个每层独立的 KVCacheTensor 合并成 num_tensors 个共享缓冲

[运行时阶段：逐层 save / load，时序受控]
  每步前向：pool_worker 把 save/load 任务按物理层填进 layer_save_tasks/layer_load_tasks
  save_kv_layer(L_i)：发起第 i 层卸载，record save_finished_event[i]
  wait_for_layer_load(L_j)：若 j 复用了某块缓冲 → 等 save_finished_event[reuse_source] 再 load
  SFA 层在算注意力前 record_attention_compute_start → 打开 prefetch gate
```

#### 4.1.3 源码精读

整个特性的「开关探测」由 `get_gva_layerwise_config` 完成：它只在 `AscendStoreConnector` / `MooncakeConnectorStoreV1`、且 `backend=memcache` 且 `use_layerwise=True` 时返回 extra_config，否则返回 `None`（即特性静默关闭）：

[get_gva_layerwise_config 探测特性开关](https://github.com/vllm-project/vllm-ascend/blob/7201c97a61a17425b558b6b5e53ab0d30ae8151d/vllm_ascend/distributed/kv_transfer/kv_pool/ascend_store/layerwise_cache_layout.py#L31-L63)

`apply_layerwise_kv_cache_plan` 是分配阶段的入口，先探测开关、构建布局，若 `has_layer_reuse` 为假则原样返回（不破坏无复用场景）：

[apply_layerwise_kv_cache_plan：探测并按 storage_indices 重排张量描述](https://github.com/vllm-project/vllm-ascend/blob/7201c97a61a17425b558b6b5e53ab0d30ae8151d/vllm_ascend/distributed/kv_transfer/kv_pool/ascend_store/layerwise_cache_layout.py#L157-L203)

其中合并核心是把每个 `slot`（一组共享同一物理缓冲的逻辑层）重写成**一个** `KVCacheTensor`，其 `shared_by` 列出该缓冲被哪些层共享：

```python
# layerwise_cache_layout.py:L184-L198（节选）
new_tensors = []
for slot in layout.storage_indices:
    slot_sizes = {old_tensors[index].size for index in slot}
    if len(slot_sizes) != 1:
        raise ValueError("Layers sharing a layerwise KV buffer must have equal tensor sizes.")
    reference_spec = layer_specs[layer_names[slot[0]]]
    if any(layer_specs[layer_names[index]] != reference_spec for index in slot[1:]):
        raise ValueError("Layers sharing a layerwise KV buffer must have identical cache specs.")
    new_tensors.append(
        KVCacheTensor(
            shared_by=[layer_names[index] for index in slot],
            size=old_tensors[slot[0]].size,
        )
    )
kv_cache_config.kv_cache_tensors = new_tensors
```

注意两条硬约束：同一物理缓冲里的层**张量大小必须相等、cache spec 必须完全相同**，否则报错。这保证多层共享一块缓冲在物理上是合法的。

#### 4.1.4 代码实践（源码阅读型）

1. **目标**：理解「逻辑布局不变」是如何在分配阶段透明实现的。
2. **步骤**：打开 `model_runner_v1.py` 的 `initialize_kv_cache`，找到 `apply_layerwise_kv_cache_plan(kv_cache_config, self.vllm_config)` 这一行，确认它发生在 `initialize_kv_cache_tensors`（真正分配张量）**之前**。
3. **观察现象**：跟踪 `kv_cache_config.kv_cache_tensors` 在 `apply_layerwise_kv_cache_plan` 调用前后长度变化——调用前是 \(N\) 个描述符，调用后变成 `num_tensors` 个（每个 `shared_by` 含多个层名）。
4. **预期结果**：上层 `initialize_kv_cache_tensors` 拿到的是合并后的描述符，分配出的物理张量数减少，但每个 `layer_name` 仍能通过 `shared_by` 找到所属张量，注意力后端按 `layer_name` 取到的还是一块合法 KV——逻辑视图未变。
5. 真实 NPU 上的显存对比**待本地验证**（无 NPU 环境只能读源码）。

[initialize_kv_cache 中分配前调用 apply_layerwise_kv_cache_plan](https://github.com/vllm-project/vllm-ascend/blob/7201c97a61a17425b558b6b5e53ab0d30ae8151d/vllm_ascend/worker/model_runner_v1.py#L3628)

#### 4.1.5 小练习与答案

**练习 1**：如果某模型 8 层、设 `layerwise_num_shared_buffers=8`，会发生缓冲复用吗？
**答**：不会。`has_layer_reuse` 要求复用层数多于缓冲数；此处两者相等，`apply_layerwise_kv_cache_plan` 原样返回，worker 也不会放大显存（见 4.3）。

**练习 2**：为什么合并时要求同缓冲内各层 cache spec 完全相同？
**答**：一块物理缓冲的张量形状/dtype/block 布局是唯一的，若两层 spec 不同（如 `num_kv_heads` 不同），它们无法安全共享同一块内存。

---

### 4.2 LayerwiseCacheLayout：逻辑层到物理缓冲的映射规则

#### 4.2.1 概念说明

`LayerwiseCacheLayout` 是本特性新增的**布局规划对象**，它一次性回答四个问题：

- 有几块物理缓冲？（`num_shared_buffers`，即配置里的 \(K\)）
- 哪些逻辑层各自独占一块缓冲、不参与复用？（`independent_layers`，默认 `[0]`，即第 0 层）
- 每块物理缓冲里都装了哪些逻辑层？（`storage_indices`，一个「槽 → 层列表」的二维结构）
- 某一层的 load，要等哪一层的 save 完成后才能复用缓冲？（`prefetch_layer_map`）
- 是否真的发生了复用？（`has_layer_reuse`）

[dataclass 定义](https://github.com/vllm-project/vllm-ascend/blob/7201c97a61a17425b558b6b5e53ab0d30ae8151d/vllm_ascend/distributed/kv_transfer/kv_pool/ascend_store/layerwise_cache_layout.py#L21-L28)

#### 4.2.2 核心流程

记 \(N=\text{num\_layers}\)、\(K=\text{num\_shared\_buffers}\)、独立层集合为 \(I\)（默认 \(\{0\}\)）、复用层列表 \(R=[0,N)\setminus I\)。规则是「复用层在 \(K\) 块缓冲间 round-robin（轮转）分布」：

- `has_layer_reuse`：只有当复用层比缓冲多时才为真：

\[
\text{has\_layer\_reuse} = |R| > K
\]

- `storage_indices`：先给每个独立层一块独占缓冲，再把 \(R\) 按 round-robin 分进 \(K\) 块缓冲。第 \(s\) 块缓冲（\(0\le s<K\)）装的是：

\[
\text{slot}_s = [\,R[s],\ R[s+K],\ R[s+2K],\ \ldots\,]
\]

- `prefetch_layer_map`：复用层 \(R[j]\)（\(j\ge K\)）会复用「轮转里它前一圈的层」的缓冲，即源层是 \(R[j-K]\)：

\[
\text{prefetch\_layer\_map}[\,R[j]\,] = R[j-K], \quad j = K, K+1, \ldots, |R|-1
\]

这条映射就是「上一层保存完成后才复用」的依据：加载 \(R[j]\) 前，必须等 \(R[j-K]\) 的 save 完成（它正占用着同一块缓冲）。

用测试用例验证（\(N=27, K=6, I=\{0\}\)，故 \(R=[1,2,\ldots,26]\)）：

- `storage_indices[1] = [R[0], R[6], R[12], R[18], R[24]] = [1,7,13,19,25]`
- `storage_indices[2] = [R[1], R[7], R[13], R[19], R[25]] = [2,8,14,20,26]`
- `prefetch_layer_map[7]=R[6-K]=R[0]=1`，`prefetch_layer_map[8]=R[1]=2`

#### 4.2.3 源码精读

[build_layerwise_cache_layout：解析配置并构造布局](https://github.com/vllm-project/vllm-ascend/blob/7201c97a61a17425b558b6b5e53ab0d30ae8151d/vllm_ascend/distributed/kv_transfer/kv_pool/ascend_store/layerwise_cache_layout.py#L75-L139)

关键的 round-robin 段：

```python
# layerwise_cache_layout.py:L119-L130（节选）
independent_layer_set = set(independent_layers)
reused_layers = [index for index in range(num_layers) if index not in independent_layer_set]
has_layer_reuse = len(reused_layers) > num_shared_buffers
prefetch_layer_map = {
    reused_layers[next_index]: reused_layers[next_index - num_shared_buffers]
    for next_index in range(num_shared_buffers, len(reused_layers))
}
storage_indices = [[layer] for layer in independent_layers]
for slot in range(num_shared_buffers):
    members = list(range(slot, len(reused_layers), num_shared_buffers))
    if members:
        storage_indices.append([reused_layers[index] for index in members])
```

配置解析里有几个细节值得注意：

- `layerwise_num_shared_buffers` 为空时 `num_shared_buffers = num_layers`（即默认不复用）；类型必须是 int，传 `True` 会被拒（避免 `bool` 当 `1` 用踩坑）。
- `layerwise_independent_layers` 支持 `'all'`（全部独立，等于关闭复用）、显式列表（支持负索引，`-1` 即最后一层）。
- `layerwise_prefetch_layers` 控制首层一次性预取多少层（默认 `min(K, 8)`），是 prefetch 深度。

[配置项常量与默认值](https://github.com/vllm-project/vllm-ascend/blob/7201c97a61a17425b558b6b5e53ab0d30ae8151d/vllm_ascend/distributed/kv_transfer/kv_pool/ascend_store/layerwise_cache_layout.py#L15-L18)

#### 4.2.4 代码实践（运行单测）

1. **目标**：亲手验证 round-robin 映射与测试断言一致。
2. **步骤**：在仓库根目录运行
   ```bash
   pytest tests/ut/distributed/ascend_store/test_layerwise_cache_layout.py -v
   ```
3. **观察现象**：`test_reuse_layout_matches_round_robin_storage_slots` 会断言 `storage_indices[1]==[1,7,13,19,25]`、`prefetch_layer_map[7]==1`。
4. **预期结果**：全部用例通过。该 UT 属于 u11-l4 讲到的 UT 层（无 NPU 即可跑）。
5. 若想自定义参数，可在 Python 里直接 `from vllm_ascend.distributed.kv_transfer.kv_pool.ascend_store.layerwise_cache_layout import build_layerwise_cache_layout`，传入不同 \(N\)、\(K\) 打印 `storage_indices` 与 `prefetch_layer_map`。

#### 4.2.5 小练习与答案

**练习 1**：\(N=27, K=6\) 时 `prefetch_layer_map[13]` 等于多少？为什么？
**答**：等于 7。因为 \(R\) 中 13 位于下标 12，源下标为 \(12-6=6\)，\(R[6]=7\)——即第 13 层 load 复用的是第 7 层刚腾出的缓冲。

**练习 2**：`has_layer_reuse` 为 `False` 时，`prefetch_layer_map` 里有几项？
**答**：0 项。`range(K, |R|)` 在 \(|R|\le K\) 时为空，不会有任何「等上一层 save」的依赖。

---

### 4.3 内存核算放大与张量描述重排（worker + model_runner）

#### 4.3.1 概念说明

复用让物理张量数从 \(N\) 降到 `num_tensors`（约 \(|I|+K\)）。但 vLLM 计算「能装多少 KV block」时，默认按「每层一块缓冲」的假设预算显存——若不调整，复用省下的 HBM 不会被转换成更多 block。

因此需要两步**配套改动**：

1. **worker 侧放大可用显存**：把 `available_kv_cache_memory_bytes` 乘以因子 \(\text{factor}=N/\text{num\_tensors}\)，让下游 block 数计算「以为」有更多显存，从而算出更大的 `num_blocks`。
2. **model_runner 侧重排张量描述**：在分配前用 `apply_layerwise_kv_cache_plan` 把 \(N\) 个描述符合并成 `num_tensors` 个，使实际分配的物理张量数减少，正好抵消掉第 1 步放大的那部分显存。

净效果：`num_blocks` 变大（逻辑每层容量变大），物理 HBM 占用不变——这正是「放大可用 KV 容量」。

#### 4.3.2 核心流程

记 \(N=\text{num\_layers}\)、\(T=\text{num\_tensors}=|\text{storage\_indices}|\)。放大因子：

\[
\text{factor} = \frac{N}{T}
\]

worker 放大：

\[
\text{available\_kv\_cache\_memory\_bytes} \;\leftarrow\; \lfloor \text{available\_kv\_cache\_memory\_bytes} \times \text{factor} \rfloor
\]

仅当 `has_layer_reuse` 为真才放大。下游 vLLM 据此算出更多 block；随后 `apply_layerwise_kv_cache_plan` 把每层缓冲合并，实际只分配 \(T\) 份，回归真实 HBM 占用。

#### 4.3.3 源码精读

[worker.py：determine_available_memory 中按因子放大](https://github.com/vllm-project/vllm-ascend/blob/7201c97a61a17425b558b6b5e53ab0d30ae8151d/vllm_ascend/worker/worker.py#L574-L589)

```python
# worker.py:L574-L589（节选）
self.available_kv_cache_memory_bytes = self.requested_memory - profile_result.non_kv_cache_memory

extra_config = get_gva_layerwise_config(self.vllm_config.kv_transfer_config)
if extra_config is not None:
    num_layers = self.model_config.get_num_layers(self.parallel_config)
    layout = build_layerwise_cache_layout(num_layers, extra_config)
    if layout.has_layer_reuse:
        num_tensors = len(layout.storage_indices)
        factor = num_layers / num_tensors
        self.available_kv_cache_memory_bytes = int(self.available_kv_cache_memory_bytes * factor)
        logger.info(
            "Layerwise KV cache reuse uses %d buffers for %d layers; scale logical KV budget by %.3f.",
            num_tensors, num_layers, factor,
        )
```

注意它紧接着还会调用 `update_available_memory_for_sparse_kv_offload`（u10-l6 稀疏卸载的预算调整），两者顺序叠加——本讲的放大先发生。

[model_runner_v1.py：分配前重排张量描述](https://github.com/vllm-project/vllm-ascend/blob/7201c97a61a17425b558b6b5e53ab0d30ae8151d/vllm_ascend/worker/model_runner_v1.py#L3623-L3628)

```python
# model_runner_v1.py:L3623-L3628（节选）
kv_cache_config = deepcopy(kv_cache_config)
self.kv_cache_config = kv_cache_config
...
self.may_add_encoder_only_layers_to_kv_cache_config()
apply_layerwise_kv_cache_plan(kv_cache_config, self.vllm_config)   # ← 分配前重排
self.maybe_add_kv_sharing_layers_to_kv_cache_groups(kv_cache_config)
```

`apply_layerwise_kv_cache_plan` 内部还做了若干校验：只支持**单个 KV cache group**、每个原张量 `shared_by` 恰好含一层、且张量数等于 base 层数（不支持 hybrid/MTP 混入 base 层场景），否则 `NotImplementedError`。

[apply_layerwise_kv_cache_plan 的前置校验](https://github.com/vllm-project/vllm-ascend/blob/7201c97a61a17425b558b6b5e53ab0d30ae8151d/vllm_ascend/distributed/kv_transfer/kv_pool/ascend_store/layerwise_cache_layout.py#L171-L181)

#### 4.3.4 代码实践（数值演算型）

1. **目标**：用一个具体数字体会「放大与重排如何抵消、净得更大 num_blocks」。
2. **步骤**：假设某模型 \(N=27\) 层，每层缓冲 \(S=1\) GB，可用 KV 显存预算 27 GB，\(K=6, I=\{0\}\)。
   - 关闭复用：\(T=27\)，27 GB 恰好每层 1 GB → `num_blocks` 由单层容量决定。
   - 开启复用：\(T=|I|+K=7\)，`factor=27/7≈3.857`，worker 把预算放大为 \(27\times3.857\approx104\) GB；下游按「每层 1 GB」算出约 104/27≈3.857 倍的 `num_blocks`；`apply_layerwise_kv_cache_plan` 把 27 个描述符合并成 7 个物理张量，实际占用 \(7\times S\times\text{num\_blocks\_new}\) 与放大后预算自洽，物理 HBM 占用未超标。
3. **观察现象**：日志会打印 `Layerwise KV cache reuse uses 7 buffers for 27 layers; scale logical KV budget by 3.857.`，随后 `Available KV cache memory` 比关闭复用时大约 3.857 倍。
4. **预期结果**：逻辑每层可承载的 token 数约为复用前的 3.857 倍（HBM 放大收益）。
5. 真实 NPU 上的绝对数字**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：为什么 worker 要先放大、model_runner 再合并，而不是反过来？
**答**：顺序必须配套。先放大让 vLLM 的 block 数计算产出更大 `num_blocks`；再合并让实际只分配 \(T\) 份物理张量。若只放大不合并，HBM 会真的超占 \(N/T\) 倍而 OOM；若只合并不放大，`num_blocks` 不会增大，复用毫无收益。

**练习 2**：`has_layer_reuse=False` 时这两处会发生什么？
**答**：worker 的 `if layout.has_layer_reuse` 分支不执行（不放大）；`apply_layerwise_kv_cache_plan` 提前 `return`（不合并）。完全等价于未启用本特性。

---

### 4.4 ascend_store layerwise 数据面时序：回载再复用、保存完成后才复用、partial GVA、错误传播

#### 4.4.1 概念说明

布局和分配搞定后，运行时由 `pool_worker`（每 worker 子进程）与 `pool_scheduler`（引擎核心）协作完成逐层 save/load。复用引入两个新约束：

- **回载再复用**：layerwise 模式下，即便 vLLM 本地已有 cache（`num_computed_tokens>0`），也仍要从池里 load——因为逐层传输的数据未必在本地 HBM。`pool_scheduler` 用 `force_layerwise_load` 表达这一点。
- **保存完成后才复用**：多层共享一块缓冲时，后一层的 load 必须等前一层的 save 完成腾出缓冲，依赖来自 `prefetch_layer_map`。

此外本特性还配套调整了 **partial GVA**（半块数据的存取键）与**错误传播**（`batch_copy` 失败由 `log` 改为 `raise`，经 `raise_if_failed` 抛回主线程）。

#### 4.4.2 核心流程

每步前向（layerwise）的数据面时序：

```text
pool_worker.process_layer_data(requests):
  for 物理层 L in [0, num_layers):
     _process_save_for_layer_batch(...)   # 填 layer_save_tasks[L]
  _prepare_load_gvas(...)                 # 取 load 用的 GVA + 读租约
  _alloc_gvas_for_save(...)               # 申请 save 用的 GVA（含 partial）
  _build_shared_save_data()               # 共享块数据预计算
  for 物理层 L in [0, num_layers):
     _process_load_for_layer_batch(...)   # 填 layer_load_tasks[L]
  _build_shared_load_data()

前向每层钩子（被 model_runner 逐层调用）:
  save_kv_layer(L):                       # 发起第 L 层卸载
     sync_save_events[L].record()
     send_thread.add_request(layer_save_tasks[L])
     当 L == 最后一层: 等所有 save 完成
  wait_for_layer_load(L):                 # 等第 L 层回载
     _submit_ready_layer_loads():         # prefetch：首层提交 num_prefetch_layers 个
        for 待提交层 j:
           reuse_source = prefetch_layer_map.get(j)   # 复用依赖
           recv_thread.add_request(LayerLoadTask(wait_for_save_layer=reuse_source, ...))
     等 layer_load_finished_events[L]

收发线程内部（KVCacheStoreLayerRecvingThread._handle_request）:
  if wait_for_save is not None:
     等 layer_save_finished_events[wait_for_save]   # ← 保存完成后才复用
  if attention_start_gate is not None:
     等 gate                                            # ← 计算流到注意力再 load
  batch_copy(G2L) 回载；失败 raise（进入 _fatal_error）
```

错误传播链：`run()` 主循环 `try/except` 捕获异常存入 `_fatal_error` 并退出线程；主线程在 `wait_for_layer_load` / `save_kv_layer` 入口调 `raise_if_failed()` 把它重新抛出，从而**让请求失败而非静默吞掉**。

#### 4.4.3 源码精读

**（a）pool_worker 构造 layerwise 配置**：`_init_layerwise_config` 从 `build_layerwise_cache_layout` 取 `has_layer_reuse`、`independent_layers`、`prefetch_layer_map`、`num_prefetch_layers`：

[pool_worker._init_layerwise_config](https://github.com/vllm-project/vllm-ascend/blob/7201c97a61a17425b558b6b5e53ab0d30ae8151d/vllm_ascend/distributed/kv_transfer/kv_pool/ascend_store/pool_worker.py#L362-L379)

**（b）load 的复用依赖**：`_submit_ready_layer_load` 把 `prefetch_layer_map.get(layer_id)` 作为 `wait_for_save_layer` 写进 `LayerLoadTask`：

[pool_worker._submit_ready_layer_loads：把复用源写进 wait_for_save_layer](https://github.com/vllm-project/vllm-ascend/blob/7201c97a61a17425b558b6b5e53ab0d30ae8151d/vllm_ascend/distributed/kv_transfer/kv_pool/ascend_store/pool_worker.py#L1596-L1623)

**（c）收发线程真正「等保存完成」**：`KVCacheStoreLayerRecvingThread._handle_request` 在 batch_copy 前 `wait` 对应层的 save 事件：

[kv_transfer.py：load 前等 reuse_source 的 save 完成](https://github.com/vllm-project/vllm-ascend/blob/7201c97a61a17425b558b6b5e53ab0d30ae8151d/vllm_ascend/distributed/kv_transfer/kv_pool/ascend_store/kv_transfer.py#L1593-L1601)

**（d）partial GVA**：layerwise 卸载时最后一个不满整块的 block（partial block）无法用 block_hash 当键（它还不完整），改用请求级键 `model@partial@req_id@group@block@end_token@rank`。`config_data` 里 `ReqMeta` 增加了 `partial_save_gvas_by_group` / `partial_load_gvas_by_group` 承载它们的 GVA：

[config_data.py：partial GVA 字段](https://github.com/vllm-project/vllm-ascend/blob/7201c97a61a17425b558b6b5e53ab0d30ae8151d/vllm_ascend/distributed/kv_transfer/kv_pool/ascend_store/config_data.py#L987-L988)

`ReqMeta.from_request_tracker` 通过 `save_partial_block=self.layerwise_offload` 决定是否启用半块保存（见 `pool_scheduler._build_req_meta` 传入 `save_partial_block=self.layerwise_offload`）。

**（e）错误传播**：`run` 捕获异常存 `_fatal_error`：

[kv_transfer.py：run 循环捕获异常](https://github.com/vllm-project/vllm-ascend/blob/7201c97a61a17425b558b6b5e53ab0d30ae8151d/vllm_ascend/distributed/kv_transfer/kv_pool/ascend_store/kv_transfer.py#L505-L520)

`raise_if_failed` 把它重新抛出，`save` 线程的 `batch_copy` 失败现在 `raise RuntimeError`（而非仅记日志）：

[kv_transfer.py：raise_if_failed 重新抛出](https://github.com/vllm-project/vllm-ascend/blob/7201c97a61a17425b558b6b5e53ab0d30ae8151d/vllm_ascend/distributed/kv_transfer/kv_pool/ascend_store/kv_transfer.py#L377-L379)

[kv_transfer.py：batch_copy 失败改为 raise](https://github.com/vllm-project/vllm-ascend/blob/7201c97a61a17425b558b6b5e53ab0d30ae8151d/vllm_ascend/distributed/kv_transfer/kv_pool/ascend_store/kv_transfer.py#L1440-L1441)

**（f）pool_scheduler 配合**：`build_connector_meta` 里 `force_skip_save = kv_role == "kv_consumer" and not self.consumer_is_to_put`（u10-l2 讲过的开关）。复用场景下「reused buffers must save every step」，见 `_process_running_cached_request` 注释——decode 期复用缓冲每步都要 save：

[pool_scheduler._process_running_cached_request：复用缓冲每步必 save](https://github.com/vllm-project/vllm-ascend/blob/7201c97a61a17425b558b6b5e53ab0d30ae8151d/vllm_ascend/distributed/kv_transfer/kv_pool/ascend_store/pool_scheduler.py#L828-L833)

[pool_scheduler：layerwise_offload 标志构造](https://github.com/vllm-project/vllm-ascend/blob/7201c97a61a17425b558b6b5e53ab0d30ae8151d/vllm_ascend/distributed/kv_transfer/kv_pool/ascend_store/pool_scheduler.py#L191-L198)

#### 4.4.4 代码实践（跟踪调用链）

1. **目标**：把「保存完成后才复用」这条时序在源码里走通。
2. **步骤**：
   - 在 `pool_worker.py` 找到 `_submit_ready_layer_loads`，确认 `reuse_source = self.prefetch_layer_map.get(layer_id)` 被塞进 `LayerLoadTask(wait_for_save_layer=reuse_source, ...)`。
   - 在 `kv_transfer.py` 的 `KVCacheStoreLayerRecvingThread._handle_request` 里，找到 `if wait_for_save is not None:` 分支，确认它 `wait` 的是 `layer_save_finished_events[wait_for_save]`。
   - 在 `pool_worker.save_kv_layer` 里，确认发起 save 后会把 `layer_save_finished_events[L]` 置位（经由 send 线程）。
3. **观察现象**：三处构成「save 优先 → load 等 save → 复用安全」的闭环。
4. **预期结果**：你能画出 `save(L=1) → load(L=7) 等 save(1)` 的时序箭头图（因为 `prefetch_layer_map[7]=1`）。
5. 真实多卡 PD 分离下的端到端日志**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：为什么 layerwise 卸载下「load」要从 block 0 开始（`load_start_block=0`），而不是从 `vllm_cached_tokens` 开始？
**答**：因为远端池存的是逐层数据，未必与本地前缀 cache 一致；layerwise 路径强制全量回载（`force_layerwise_load`），保证每层数据完整。见 `_process_load_for_layer_batch` 中 `load_start_block = 0 if layerwise_offload else ...`。

**练习 2**：把 `batch_copy` 失败从 `log` 改成 `raise`，对上层有什么好处？
**答**：失败会经 `_fatal_error` → `raise_if_failed` 抛到主线程，请求会明确失败并重算，而不是线程悄悄退出、KV 半空导致后续静默错误。

---

### 4.5 SFA prefetch gate 前移：让无 indexer 的层也能触发预取

#### 4.5.1 概念说明

MemCache 的 H2D 回载与计算流要**重叠**才高效。`AttentionComputeStartGate` 是个跨线程栅栏：注意力前向在「即将提交注意力算子」时 `record` 一个 NPU event，MemCache 的 load 线程在 `batch_copy(G2L)` 前 `wait` 这个 event，使回载恰好在计算流真正到达注意力边界时才开始（而不是 Python 调用刚到这里时）。

问题：原本 `record_attention_compute_start()` 写在 `indexer_select_post_process` 内部，**只有带 indexer 的 SFA 层**才会触发。但 GLM-5.2 的一些层**复用别的层的 top-k 索引**（`skip_topk=True`，自身无 indexer），根本不进 `indexer_select_post_process`——它们的 gate 永远不打开，prefetch 卡住。

修复：把 `record_attention_compute_start()` 提到 `skip_topk` 分支**之前**，对**每一个** SFA 层都打开 gate。

#### 4.5.2 核心流程

```text
SFA forward:
  ... 写 KV cache、scatter k_li_scale ...
  record_attention_compute_start()        # ← 前移：所有 SFA 层都开 gate
  if skip_topk:                           # 无 indexer、复用 top-k 的层走这里
      topk_indices = _get_indexcache_topk_indices(...)
  else:                                   # 有 indexer 的层走这里
      topk_indices = indexer_select_post_process(...)   # 旧版 gate 在这里面

MemCache load 线程（同 4.4）:
  if attention_start_gate is not None:
     等 gate（即等计算流到达注意力）→ 再 batch_copy(G2L)
```

#### 4.5.3 源码精读

[sfa_v1.py：record_attention_compute_start 前移到 skip_topk 之前](https://github.com/vllm-project/vllm-ascend/blob/7201c97a61a17425b558b6b5e53ab0d30ae8151d/vllm_ascend/attention/sfa_v1.py#L2026-L2035)

```python
# sfa_v1.py:L2026-L2035（节选）
# Open the prefetch gate for every SFA layer. Some GLM-5.2 layers
# reuse cached top-k indices and have no indexer, so recording this
# inside indexer_select_post_process would leave their gate closed.
record_attention_compute_start()

if self.skip_topk:
    topk_indices = self._get_indexcache_topk_indices(topk_num_tokens)
else:
    if not self.has_indexer:
        raise RuntimeError(f"skip_topk is False but indexer is None. layer_name={self.layer_name}.")
    ...
    topk_indices = self.indexer_select_post_process(...)
```

栅栏实现本身在 `memcache_comm_fence.py`：`AttentionComputeStartGate.record` 记一个 NPU event 并唤醒等待者，`wait` 同步该 event；`reset_attention_compute_start_gate` 为**每一层**新建一个 gate（layerwise prefetch 任务持有提交时刻的 gate 引用）：

[memcache_comm_fence.py：AttentionComputeStartGate 与三个入口函数](https://github.com/vllm-project/vllm-ascend/blob/7201c97a61a17425b558b6b5e53ab0d30ae8151d/vllm_ascend/memcache_comm_fence.py#L27-L91)

pool_worker 侧：`start_load_kv` 调 `reset_attention_compute_start_gate()` 开新 gate；`_submit_ready_layer_loads` 只对「非当前层」的 prefetch 任务挂 gate（当前层立即需要、不必等）：

[pool_worker.start_load_kv：每步重置 gate](https://github.com/vllm-project/vllm-ascend/blob/7201c97a61a17425b558b6b5e53ab0d30ae8151d/vllm_ascend/distributed/kv_transfer/kv_pool/ascend_store/pool_worker.py#L821-L827)

#### 4.5.4 代码实践（改参数观察型）

1. **目标**：理解 gate 前移对「无 indexer 层」的必要性。
2. **步骤**：读 `sfa_v1.py` 的 `AscendSFAImpl.__call__`（或对应前向方法），定位 `self.has_indexer` 与 `self.skip_topk` 两个标志；对照 `_has_shared_indexer_layers`（构造时由 `indexer_types=='shared'` 判定）。
3. **观察现象**：一个 GLM-5.2 模型里，部分层 `has_indexer=False, skip_topk=True`（复用 top-k），它们在修复前不会触发 `record_attention_compute_start`。
4. **预期结果**：修复后这些层的 prefetch gate 也会打开，MemCache load 能与注意力计算重叠。
5. 重叠带来的吞吐收益**待本地验证**。

#### 4.5.5 小练习与答案

**练习 1**：为什么不在前向一开始（写 KV 之前）就 record gate？
**答**：gate 的语义是「计算流**到达注意力**时打开」，目的是让 H2D 与注意力计算重叠。过早 record 会让回载在注意力之前就发起，既占带宽又达不到重叠注意力的目的。

**练习 2**：`reset_attention_compute_start_gate` 为什么每层都要新建一个 gate 对象？
**答**：layerwise prefetch 任务在提交时持有「当时的 gate」引用；每层新建保证各层 gate 独立，避免上一层的事件被下一层误用。

---

## 5. 综合实践

把本讲知识串起来，完成一个「**从配置到时序**」的端到端梳理任务（源码阅读型，无需 NPU）：

1. **配置层**：假设你要为一个 27 层的模型开启分层缓冲复用，写出 `additional-config` 里 `kv_connector` / `kv_connector_extra_config` 的 JSON 片段（`backend=memcache`、`use_layerwise=True`、`layerwise_num_shared_buffers=6`），并说明 `kv_role` 在 P/D 节点分别取什么值。
2. **规划层**：用 Python 调 `build_layerwise_cache_layout(27, {...})`，打印 `has_layer_reuse`、`storage_indices`、`prefetch_layer_map`，手算 `factor=27/len(storage_indices)`，与 worker 日志的 `scale logical KV budget by ...` 对照。
3. **分配层**：画出 `worker.determine_available_memory`（放大）→ `model_runner.initialize_kv_cache`（`apply_layerwise_kv_cache_plan` 重排）→ `initialize_kv_cache_tensors`（按合并后描述符分配）的顺序图，标注「为什么顺序不能反」。
4. **时序层**：以 `prefetch_layer_map[7]=1` 为例，画出 `save(L=1)` 与 `load(L=7)` 在 `KVCacheStoreLayerRecvingThread._handle_request` 中通过 `layer_save_finished_events[1]` 串起来的时序，并标出 `attention_start_gate` 在哪一步被等。
5. **错误层**：描述一次 `batch_copy` save 失败时，异常如何从 send 线程的 `run()` → `_fatal_error` → 主线程 `save_kv_layer` 入口的 `raise_if_failed` 传播。

**验收**：你能不看源码讲清「开启 `layerwise_num_shared_buffers` 后，NPUWorker 如何按 `num_layers/num_tensors` 放大可用 KV 显存、`apply_layerwise_kv_cache_plan` 如何让多个 transformer 层共享有限物理缓冲；为何要在上一层保存完成后才复用同一缓冲；无 indexer 的 SFA 层为何需要把 prefetch gate 前移」——这正是本讲的 practice_task。

---

## 6. 本讲小结

- **动机**：layerwise prefill 卸载下同一时刻只有当前层需要 KV 驻留 HBM，故可让 \(N\) 个逻辑层分时复用 \(K\) 块物理缓冲，回收 HBM、放大可用 KV 容量，且逻辑布局不变。
- **映射规则**：`LayerwiseCacheLayout` 用 round-robin 把复用层分进 \(K\) 块缓冲（`storage_indices`），并用 `prefetch_layer_map[L]=L的前一轮层` 表达「L 的 load 要等谁 save 完成」；`has_layer_reuse=|R|>K` 决定特性是否真正生效。
- **内存核算**：worker 按 `factor=num_layers/num_tensors` 放大 `available_kv_cache_memory_bytes`，model_runner 在分配前用 `apply_layerwise_kv_cache_plan` 把描述符合并成 `num_tensors` 个——两步配套，净得更大 `num_blocks` 而 HBM 不超。
- **数据面时序**：`pool_worker` 逐层填 save/load 任务；收发线程在 load 前 `wait` 复用源的 save 事件（保存完成后才复用），并在 `attention_start_gate` 打开后才 `batch_copy`；decode 期复用缓冲每步都要 save。
- **配套改动**：partial block 用请求级键（partial GVA）存取；`batch_copy` 失败由 `log` 改为 `raise`，经 `raise_if_failed` 抛回主线程。
- **SFA gate 前移**：`record_attention_compute_start` 移到 `skip_topk` 分支前，使 GLM-5.2 等无 indexer（复用 top-k）的 SFA 层也能打开 prefetch gate。

## 7. 下一步学习建议

- 若想理解本特性所依赖的连接器整体（mooncake / ascend_store / MultiConnector、`consumer_is_to_put` 开关、PIECEWISE 图模式），回看 **u10-l2**。
- 若想理解 SFA / indexer / `skip_topk` / `has_indexer` 的注意力侧细节，回看 **u5-l2**。
- 若关注另一条 KV 卸载路线（把主 KV 卸到主机、decode 期仅 top-k 回载到常驻缓冲），继续读 **u10-l6 稀疏 KV 缓存卸载**，并与本讲对比「稀疏卸载」与「分层缓冲复用」的异同。
- 想跑本特性的单测，参见 **u11-l4 测试与 CI 体系**，重点看 `tests/ut/distributed/ascend_store/test_layerwise_cache_layout.py`。
