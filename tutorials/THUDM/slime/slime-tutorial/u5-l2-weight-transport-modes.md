# 三种权重传输：tensor / disk full / disk delta

## 1. 本讲目标

上一讲（u5-l1）我们从鸟瞰视角看清了权重同步的「四象限选型」：`--update-weight-mode`（full/delta）× `--update-weight-transport`（nccl/disk）× `--colocate` 三个因子，会选出不同的传输类，并介绍了它们共用的 `all_gather_param` 与 `named_params_and_buffers` 工具。

本讲往下钻一层，**精读其中三类传输实现**：

1. **`UpdateWeightFromTensor`** —— 共卡（colocate）默认路径，用 CUDA IPC 把训练侧 GPU 张量直接喂给同机推理引擎，混合部署时也兼用 NCCL 广播。
2. **`UpdateWeightFromDiskDelta`** —— 跨机/外部集群场景，每轮只把「变化的张量」写成增量检查点（支持 xor / overwrite 编码 + zstd 压缩），引擎再拉取重载。
3. **`expert_routing`** —— 不是独立传输类，而是 MoE 模型下挂载在 tensor 路径里的「专家直传规划器」，让持有某个专家的 Megatron rank 用 P2P 直接发给需要它的 SGLang rank，省去冗余的全量广播。

学完后你应当能：

- 说清楚一次 `update_weights()` 在 colocate 下走的 IPC 时序、在 delta+disk 下走的「快照→diff→发布→拉取重载」时序。
- 看懂 xor 与 overwrite 两种增量编码的差异，以及为何 overwrite 是幂等而 xor 是对合（involution）。
- 判断一个 MoE 部署是否满足「rank-local 专家直传」的条件，并描述不满足时如何回退。

## 2. 前置知识

阅读本讲前，请确认你已掌握以下概念（它们在 u5-l1 与更早讲义中建立）：

- **权重同步的方向与时机**：RL 是 on-policy 的，每轮训练后由 `MegatronTrainRayActor.update_weights()` 把 Megatron 工人手里按 TP/PP/EP 分片的 torch 权重，单向搬运并注入 SGLang 推理引擎；方向是 training→rollout，不可逆。
- **TP/PP/EP 分片 vs HF 完整张量**：Megatron 内部参数是切分的，推理引擎需要 HF 命名、未切分的完整张量。聚合靠 `all_gather_param`，枚举靠 `named_params_and_buffers`。
- **`convert_to_hf`**：把 Megatron 命名的张量转成 HF 命名（这一步在传输前完成）。
- **Ray ObjectRef / ActorHandle**：slime 用 Ray 编排，推理引擎是远程 Actor，`.remote()` 返回 ObjectRef。
- **`weight_version`**：单调递增的版本号，引擎用它判断收到的权重是否最新、是否需要丢弃旧请求的 KV cache。

几个本讲会用到的分布式术语：

- **NCCL broadcast**：集合通信原语，rank 0 把一块 GPU 显存广播给组内所有 rank，常用于把权重从训练侧推到远端引擎。
- **CUDA IPC**：进程间通信句柄。同机两个进程共享同一张 GPU 时，生产者可以把显存块的 *handle* 传给消费者，消费者直接映射该显存，**只拷贝一个句柄而非整块权重**——这是 colocate 高效的关键。
- **Gloo**：PyTorch 自带的 CPU 端通信后端，这里用来在小对象（序列化后的元数据）上做 `gather_object`/`all_gather_object` 协调。
- **MoE / 专家（expert）**：混合专家模型里，每个 token 只激活部分「专家」FFN 子网络。专家权重数量多，且每个 EP（expert parallel）rank 只持有/只需要一部分专家。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [slime/backends/megatron_utils/update_weight/update_weight_from_tensor.py](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/update_weight/update_weight_from_tensor.py) | 共卡 IPC + 分布式 NCCL 的张量传输类 `UpdateWeightFromTensor`，本讲核心之一 |
| [slime/backends/megatron_utils/update_weight/update_weight_from_disk_delta.py](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/update_weight/update_weight_from_disk_delta.py) | 磁盘增量同步类 `UpdateWeightFromDiskDelta`，含快照/diff/编码/发布/重载全流程 |
| [slime/backends/megatron_utils/update_weight/expert_routing.py](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/update_weight/expert_routing.py) | MoE 专家直传规划器 `configure_expert_routing`，决定能否走 rank-local P2P |
| [slime/utils/disk_delta.py](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/utils/disk_delta.py) | delta 编解码工具：`overwrite_encode`、`checksum`、`make_tensor_reader` |
| [slime/backends/megatron_utils/update_weight/update_weight_from_distributed.py](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/update_weight/update_weight_from_distributed.py) | `UpdateWeightFromDistributed`（NCCL 广播基类）及 `_iter_non_expert_chunks` / `_iter_expert_chunks`，是 delta 类的父类 |
| [slime/backends/megatron_utils/actor.py](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/actor.py) | 工厂选择逻辑：根据 mode/transport/colocate 选出实际传输类 |

## 4. 核心概念与源码讲解

### 4.1 UpdateWeightFromTensor：张量直传（IPC + NCCL）

#### 4.1.1 概念说明

当训练工人和推理引擎**共卡**（`--colocate`，slime 默认）时，权重同步走 `UpdateWeightFromTensor`。它解决的问题很直接：训练刚更新完一组 GPU 张量，如何让同机的 SGLang 引擎用上这批新权重？

答案是 **CUDA IPC**：生产者（训练 rank）把 GPU 显存块的句柄交给消费者（引擎），引擎直接映射这片显存并装载进自己的模型权重。整块权重**不跨进程拷贝**，只传一个轻量句柄。

但 colocate 并不总是全部引擎都共卡——slime 也支持「部分引擎共卡、部分引擎在远端」的混合部署。`UpdateWeightFromTensor` 因此被设计成**同时处理两类引擎**：

- 共卡引擎 → IPC（句柄）。
- 远端引擎 → NCCL broadcast（张量真正在网络上走）。

它的名字叫 "FromTensor"，强调的是「直接从内存中的张量发」，区别于 "FromDisk"（要落盘再读）。这一点从类文档串可以一眼看出：

[update_weight_from_tensor.py:50-L56](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/update_weight/update_weight_from_tensor.py#L50-L56) —— 类文档串明确写出两条路径：Colocated 走 Gloo CPU gather + Ray IPC；Distributed 走 GPU NCCL broadcast。

> 小提示：slime 里还有个 `UpdateWeightFromDisk`（全量落盘）和 `UpdateWeightFromDistributed`（纯 NCCL 广播，也是 delta 类的父类）。本讲聚焦 tensor 与 delta 两条主路径，全量 disk 的逻辑类似 delta 的「发布」阶段但每轮写整份检查点，放在对比中顺带提及。

#### 4.1.2 核心流程

`update_weights()` 是入口，时序如下（伪代码）：

```
weight_version += 1
rank0: engine.pause_generation()   # 引擎停止接收新请求
       engine.flush_cache()        # 清空 KV cache（旧权重产生的 cache 必须失效）
       [可选] compressed-tensors 量化预处理
barrier(gloo)
local_weights = weights_getter()   # 取当前 Megatron 权重（已 backup("actor")）

# 非专家（dense）参数：分块走 IPC / NCCL
for hf_chunk in hf_weight_iterator.get_hf_weight_chunks(local_weights):
    send_hf_params(hf_chunk)       # 内部 ray.get，逐块同步

# 专家参数：若有 expert_transfer_plan，单独走 P2P
if expert_transfer_plan:
    update_expert_weights(local_weights)

释放 IPC handle + empty_cache
rank0: engine.continue_generation()
```

`_send_hf_params` 是分发枢纽，**两类引擎在这里分流**：

- 先用 `_send_to_colocated_engine` 把这一块 HF 张量发给绑定的共卡引擎（IPC）。
- 若本 rank 是分布式源 rank（DP=TP=PP=0），再用 `update_weights_from_distributed` 走 NCCL 广播给远端引擎。

共卡 IPC 内部细节（`_send_to_colocated_engine`）：

1. 把 HF 张量压平成一个大 `flattened_tensor` buffer（`FlattenedTensorBucket`），并产出 metadata。
2. 若引擎不支持多 dtype 混装，则按 dtype 分组分别压平。
3. `MultiprocessingSerializer.serialize` 把 buffer 序列化成可在 Ray 间传递的形式。
4. 用 **Gloo `gather_object`** 把组内各 rank 的序列化张量收到一个「汇总 rank」（`ipc_gather_src`）——因为一个引擎可能横跨多个训练 rank（`rollout_num_gpus_per_engine`），需要先把它们收拢到代表该引擎的那一个 rank。
5. 汇总 rank 调 `engine.update_weights_from_tensor.remote(load_format="flattened_bucket", ...)`，由引擎侧解码装载。

一个关键的并发安全细节：`_build_flattened_tensor_data` **不复用**面向 IPC 的 flattened tensor，注释解释了原因——SGLang 在把 GPU 拷贝排进模型权重后就返回 HTTP/Ray 响应，但不保证在此之前做过 CUDA-device sync，若立刻重用并覆盖生产者 buffer，会和消费者侧拷贝竞争、损坏权重。

[update_weight_from_tensor.py:38-L47](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/update_weight/update_weight_from_tensor.py#L38-L47) —— 这段注释是理解 IPC 路径正确性的钥匙。

#### 4.1.3 源码精读

**入口 `update_weights`** —— 注意 pause/flush 在前、continue 在后，专家更新是独立一阶段：

[update_weight_from_tensor.py:276-L331](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/update_weight/update_weight_from_tensor.py#L276-L331) —— `weight_version += 1` 后，rank0 先 pause+flush，逐块发送非专家权重，再单独 `_update_expert_weights`，最后 continue_generation。每块发送完都 `ipc_collect + empty_cache` 释放 IPC 句柄。

**分发枢纽 `_send_hf_params`** —— 共卡与分布式并列：

[update_weight_from_tensor.py:333-L356](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/update_weight/update_weight_from_tensor.py#L333-L356) —— 先 `_send_to_colocated_engine`（IPC），再在 `use_distribute and _is_distributed_src_rank` 时 `update_weights_from_distributed`（NCCL），合并所有 ObjectRef 返回。

**共卡 IPC 通路 `_send_to_colocated_engine`** —— 关键是 Gloo `gather_object` 收拢到 `ipc_gather_src`：

[update_weight_from_tensor.py:390-L422](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/update_weight/update_weight_from_tensor.py#L390-L422) —— `dist.gather_object(..., dst=ipc_gather_src)`，只有 `ipc_gather_src` 那个 rank 真正调用 `engine.update_weights_from_tensor.remote(...)`。

**引擎接入 `connect_rollout_engines`** —— 区分共卡/分布式、建 IPC Gloo 组、调 `configure_expert_routing`：

[update_weight_from_tensor.py:119-L151](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/update_weight/update_weight_from_tensor.py#L119-L151) —— 通过比较每个引擎的 `gpu_offset + gpu_count` 是否落在 `total_actor_gpus` 范围内，数出 `colocate_engine_nums`；若引擎总数大于它，则 `use_distribute=True` 并切出 `distributed_rollout_engines`。

[update_weight_from_tensor.py:160-L166](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/update_weight/update_weight_from_tensor.py#L160-L166) —— 为每个共卡引擎建一个 Gloo `new_group`，本 rank 落在哪个组就把它记为自己的 `_ipc_gather_group`。

[update_weight_from_tensor.py:175-L183](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/update_weight/update_weight_from_tensor.py#L175-L183) —— 调用 `configure_expert_routing`（4.3 详述），返回 `(non_expert_buckets, expert_transfer_plan)`。

#### 4.1.4 代码实践

**实践目标**：看清一次 `update_weights()` 调用中，共卡 IPC 与分布式 NCCL 两条路径如何并存。

**操作步骤**：

1. 打开 [update_weight_from_tensor.py](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/update_weight/update_weight_from_tensor.py)。
2. 在 `update_weights`（L276 起）每一行旁标注它属于 `版本号 / 暂停 / 取权重 / 发送dense / 发送expert / 释放 / 恢复` 哪一阶段。
3. 进入 `_send_hf_params`（L333），标注 `_send_to_colocated_engine` 与 `update_weights_from_distributed` 各自的前置条件（`ipc_gather_group is not None` vs `use_distribute and _is_distributed_src_rank`）。
4. 进入 `_send_to_colocated_engine`（L359），找出「Gloo 收拢 → 汇总 rank 发 remote」这两步对应的行。

**需要观察的现象**：

- 共卡路径里，真正调用 `.remote()` 的只有一个 rank（`ipc_gather_src`），其余 rank 只参与 `gather_object`。
- 分布式路径里，只有 DP=TP=PP=0 的 rank 才广播。

**预期结果**：你应得到一张「阶段 → 行号 → 通路（IPC/NCCL/都走）」的对照表。

**待本地验证**：若你有多机集群，可分别用 `--colocate`（走 IPC）与不加 colocate 但 `--update-weight-transport=nccl`（走广播）跑一轮，对比 `update_weight_metrics` 与日志中「Update weights」进度条出现的位置。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `_build_flattened_tensor_data` 不复用上一次的 flattened tensor buffer？

**参考答案**：因为 SGLang 在把 GPU 拷贝排入模型权重后就返回响应，但不保证已做 CUDA-device sync；若立刻覆盖生产者 buffer，会和消费者侧的异步拷贝竞争，损坏权重。所以每次都新建 buffer（见 L38-L47 注释）。

**练习 2**：`connect_rollout_engines` 是如何判断「哪些引擎共卡」的？

**参考答案**：用每个引擎的 `gpu_offset + gpu_count` 是否超过 `actor_num_nodes * actor_num_gpus_per_node`（即 actor 的 GPU 总区间）来判断；落在区间内的前若干个引擎计入 `colocate_engine_nums`，其余为分布式引擎（见 L119-L127）。

---

### 4.2 UpdateWeightFromDiskDelta：磁盘增量同步

#### 4.2.1 概念说明

当推理引擎部署在**远端独立集群**或**跨机**、且训练侧与引擎侧共享一个文件系统时，NCCL 直连可能不可行或不划算。此时 slime 提供「磁盘」传输：训练侧把权重写成检查点文件，引擎侧读文件重载。

全量磁盘模式（`UpdateWeightFromDisk`）每轮写整份检查点——对大模型而言 IO 开销巨大。**增量模式** `UpdateWeightFromDiskDelta` 的核心思想是：RL 训练相邻两轮的权重变化通常很小（梯度步长小），于是每轮**只写出变化的张量**，引擎再把这些「补丁」apply 到本地基线检查点上。

它支持两种增量编码（由 `--update-weight-delta-encoding` 选择）：

- **xor**：逐字节异或 `diff = new ^ old`，稀疏、zstd 压缩效果好，但接收方必须持有精确的旧基线才能还原。
- **overwrite**：记录变化位置的下标 + 新值，幂等、自描述，但变化多时下标开销变大。

`UpdateWeightFromDiskDelta` 继承自 `UpdateWeightFromDistributed`，**复用父类的 `_iter_non_expert_chunks` / `_iter_expert_chunks`** 来做 TP/EP all-gather，从而拿到「完整的 HF 张量」；然后 override 掉 `update_weights`，把发送逻辑换成「快照→diff→发布→拉取重载」。

> 约束：delta 模式只允许 `--update-weight-transport=disk`，且**不支持 `--colocate`**（共卡走 IPC 几乎零成本，快照+diff 纯属浪费）。这在校验逻辑里写死了（见 4.2.3）。

#### 4.2.2 核心流程

`update_weights` 是一个**三态机**：

```
第一次调用：capture_baseline()   # 只拍快照、不发布，立即返回
之后每次：  weight_version += 1
            _publish()           # diff + 写增量文件
            _reload_engines()    # 引擎拉取补丁 + 重载
            _record_metrics()    # 统计变化密度/线上字节
```

**为什么第一次只拍快照？** 因为增量 diff 需要一个「上一轮」基线。第一次没有上一轮，于是先建立基线快照（`self._snapshot`），并让每个引擎先物化自己的本地基线（`pull_weights(0)`），保证后续 `snapshot == engine base` 这个不变量成立。

`_capture_baseline` 关键点：

- 从 **`hf_checkpoint`**（即引擎的基线权重目录）读张量作为快照初值——而不是从 gathered 当前权重。这样即使 megatron→HF 往返会去掉 vocab padding 行，快照与引擎基线仍严格一致。
- 不在 `hf_checkpoint` 的张量（罕见）才回退到 gathered 当前值并打 warning。
- `pull_weights(0)` 与快照 gather **重叠执行**，让首次真正同步只付增量 apply 的代价。

`_publish = _encode_delta + _write_delta_files`：

`_encode_delta` 是核心算法，采用「主循环拷贝 + 线程池并发 diff/压缩」的流水线：

1. 为每个 gathered HF 张量，`view(torch.uint8).reshape(-1)` 拍平成字节一维数组。
2. 用 **pinned host-buffer 池**做高效 `non_blocking` GPU→CPU 拷贝（比 `.cpu()` 快很多）。
3. 把任务丢进 `ThreadPoolExecutor(NUM_WORKERS)`，worker 里跑 `diff_and_compress`：算 diff、判是否变化、zstd 压缩、算 checksum。
4. 主循环用 `inflight` 队列做背压（在途任务数 ≥ `2 * NUM_WORKERS` 时回收）。

`diff_and_compress` 的两条分支：

```python
if self.delta_encoding == "xor":
    diff = new ^ old
    changed = int(np.count_nonzero(diff))
elif self.delta_encoding == "overwrite":
    mask = new != old
    changed = int(np.count_nonzero(mask))
    diff = overwrite_encode(new, mask)
```

**未变化的张量直接跳过**（不进 `self._delta`）；变化的张量：zstd(level=1) 压缩、算 checksum，并把 `snapshot[name] = new`（成为下次 diff 的基线）。

`_write_delta_files`：

- 有变化的 rank 写一个 `model-NNNNN-of-NNNNN.safetensors`，`metadata` 里带每个张量的 checksum。
- rank0 写 `model.safetensors.index.json`，含 `version`、`base_version`、`delta_encoding`、`compression_format`、`checksum_format` 和 `weight_map`。
- 文件编号和 index **通过 Gloo 的 `all_gather_object` 协调**，而不是靠文件系统——因为某些共享 FS（如对象存储后端）非 POSIX，一个 rank 的写不会立刻对另一个 rank 可见。

`_reload_engines`：

1. 触发 `_post_write_hook`（对象存储后端缺乏跨机 read-after-write 一致性，需要显式「提交」步骤，如上传到对象桶）。
2. `engine.pull_weights(version)`：每个引擎把增量补丁拉到它**跨越的每一台主机**（checksum 校验），apply 到本地检查点。
3. pause/flush → `update_weights_from_disk`（普通整检查点重载）→ continue。

`_record_metrics`：all-reduce `changed_bytes / total_bytes / wire_bytes`，产出两个关键指标：

- `perf/update_weights_density` = changed / total（变化密度，越小说明增量效果越好）。
- `perf/update_weights_wire_bytes`（线上传输字节数，含压缩后大小）。

#### 4.2.3 源码精读

**类文档串** —— 一句话点明 delta 的设计意图：

[update_weight_from_disk_delta.py:30-L37](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/update_weight/update_weight_from_disk_delta.py#L30-L37) —— PP-src rank 把每个 gathered HF 张量与上次同步的 CPU 快照 diff，把变化发布成标准 HF 检查点目录；引擎的 `/pull_weights` 把 apply 扇出到它跨越的每台主机，再用普通 `update_weights_from_disk` 重载。

**三态入口 `update_weights`**：

[update_weight_from_disk_delta.py:82-L93](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/update_weight/update_weight_from_disk_delta.py#L82-L93) —— 首次调用 `_capture_baseline` 后置 `_baseline_captured = True` 并 `return`；之后才进入 `version++ → _publish → _reload_engines → _record_metrics`。

**基线快照 `_capture_baseline`**：

[update_weight_from_disk_delta.py:95-L125](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/update_weight/update_weight_from_disk_delta.py#L95-L125) —— 从 `hf_checkpoint` 种子化快照，`pull_weights(0)` 让每台主机物化本地基线，保证 `snapshot == engine base`。

**核心编码 `_encode_delta`**：

[update_weight_from_disk_delta.py:199-L273](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/update_weight/update_weight_from_disk_delta.py#L199-L273) —— pinned buffer 池 + `ThreadPoolExecutor` 流水线，`diff_and_compress` 内含 xor/overwrite 两条分支（L231-L238），未变化跳过，`snapshot[name] = new` 滚动基线（L247）。

**xor/overwrite 分支**（精读最关键 8 行）：

[update_weight_from_disk_delta.py:231-L243](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/update_weight/update_weight_from_disk_delta.py#L231-L243) —— `xor` 算 `new ^ old`；`overwrite` 用 `overwrite_encode(new, mask)`；两者都判 `changed`，未变则返回空，变化则 zstd 压缩 + checksum。

**工具 `overwrite_encode`** —— 在 disk_delta.py：

[disk_delta.py:21-L25](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/utils/disk_delta.py#L21-L25) —— 拼接 `[变化个数(u4)] + [各变化位置下标(u4)] + [各位置的新值]`。

**发布文件 `_write_delta_files`**：

[update_weight_from_disk_delta.py:133-L168](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/update_weight/update_weight_from_disk_delta.py#L133-L168) —— 用 Gloo `all_gather_object` 协调文件编号与 index，`_atomic_write` 保证原子可见。

**拉取重载 `_reload_engines`**：

[update_weight_from_disk_delta.py:170-L190](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/update_weight/update_weight_from_disk_delta.py#L170-L190) —— `post_write_hook` 提交 → `pull_weights` → pause/flush → `update_weights_from_disk` → continue。

**指标 `_record_metrics`**：

[update_weight_from_disk_delta.py:275-L294](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/update_weight/update_weight_from_disk_delta.py#L275-L294) —— all-reduce 字节计数，算 `density = changed/total` 与 `wire_bytes`。

**约束校验**（在参数校验阶段）：

[arguments.py:1995-L2011](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/utils/arguments.py#L1995-L2011) —— delta 必须 disk、不得 colocate、必须提供 `--update-weight-local-checkpoint-dir`，否则启动即报错。

#### 4.2.4 代码实践

**实践目标**：对照 `overwrite_encode`，亲手说明增量编码如何只写出变化部分，并讲清 xor 与 overwrite 的可逆性差别。

**操作步骤**：

1. 打开 [disk_delta.py](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/utils/disk_delta.py)，读懂 `overwrite_encode`（L21-L25）。
2. 准备一个最小 Python 实验（**示例代码，非项目原代码**）：

   ```python
   import numpy as np
   from slime.utils.disk_delta import overwrite_encode, checksum

   old = np.array([1, 2, 3, 4, 5], dtype=np.uint8)
   new = np.array([1, 9, 3, 4, 7], dtype=np.uint8)   # 只有位置 1、4 变化
   mask = new != old                                  # [F, T, F, F, T]

   # overwrite 增量
   patch = overwrite_encode(new, mask)
   # patch = [个数=2][位置1, 位置4][新值9, 新值7]

   # xor 增量
   diff = new ^ old                                    # [0, 11, 0, 0, 2]
   ```

3. 手算 `overwrite_encode` 的字节布局：先一个 `<u4` 的个数 `2`，再两个 `<u4` 下标 `1, 4`，最后两个 `uint8` 新值 `9, 7`。
4. 用 `checksum("adler32", new)` 算新状态的校验和，理解引擎侧 apply 后如何用 checksum 验证。

**需要观察的现象**：

- `overwrite_encode` 只存了变化位置的下标和新值，**未变化的位置完全不出现在 patch 里**。
- xor 的 `diff` 长度始终等于整张量长度（5 个字节），只是未变化位置为 0，靠后续 zstd 把这些 0 压掉。
- 若你把 `new` 再变一次（比如只有位置 2 变化），xor 的 diff 仍占满整张量；overwrite 的 patch 则只含位置 2。

**关于可逆性的结论**（这是本实践的核心）：

设基线为 \(b\)、目标为 \(n\)、增量补丁为 \(d\)。

- **xor**：\(d = n \oplus b\)。应用时 \(n = b \oplus d\)。由于异或是**对合（involution）**，对同一个基线连续应用两次会「拨回」原状：

  \[ (b \oplus d) \oplus d = b \oplus (d \oplus d) = b \oplus 0 = b \]

  也就是说 xor 补丁**不是幂等的**——重复 apply 会让权重在 \(n\) 和 \(b\) 之间来回跳。它要求接收方持有精确的旧基线 \(b\)，否则静默损坏。优点：diff 稀疏、zstd 压缩比高。

- **overwrite**：补丁里直接是「位置 → 新值」，应用就是把新值写进对应位置。连续应用两次结果不变，**幂等**：

  \[ \text{apply}(\text{apply}(b, d), d) = \text{apply}(b, d) \]

  但它**不可逆**——补丁里没存旧值，无法从补丁单独还原 \(b\)。优点：自描述、对 apply 顺序/重复鲁棒；缺点：变化密集时下标开销变大（每个变化元素要多存一个 u4 下标）。

这与源码注释完全一致：

[disk_delta.py:21-L25](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/utils/disk_delta.py#L21-L25) 注释原文："Idempotent to apply, unlike xor (an involution); the trainer picks the encoding per the docs."

**预期结果**：你能用一句话回答「为什么 overwrite 幂等而 xor 不是」——xor 存的是「差异比特」，重复施加会拨回；overwrite 存的是「目标值」，重复施加是覆盖同一结果。

**待本地验证**：在没有多机集群时，可只做上面的单机 numpy 实验，确认 `patch` 的字节布局与手算一致。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `_capture_baseline` 要从 `hf_checkpoint` 读快照，而不是直接用 gathered 的当前权重？

**参考答案**：为了保证 `snapshot == engine base`。megatron→HF 的往返可能会裁掉 vocab padding 行（embed/lm_head），若快照取 gathered 值而引擎基线取自 `hf_checkpoint`，两者会错位，导致后续 diff 把这些行误判为「变化」。从 `hf_checkpoint` 种子化可消除这一不一致（见 L95-L101 注释）。

**练习 2**：`_encode_delta` 为什么用 pinned host-buffer 池 + 线程池，而不是逐张量 `.cpu()` 顺序处理？

**参考答案**：diff/zstd/checksum 是内存带宽密集且释放 GIL 的操作，单线程会让 GPU↔CPU 拷贝和这些 CPU 计算串行等待。pinned buffer 让 `non_blocking` GPU→CPU 拷贝远快于 `.cpu()`，线程池让拷贝与计算重叠，恢复被单线程浪费的带宽（见 L11-L13 与 L199-L203 注释）。

---

### 4.3 expert_routing：MoE 专家的 rank-local 直传

#### 4.3.1 概念说明

MoE 模型有大量「专家」FFN 子网络。在默认的权重同步流程里，专家参数和非专家参数走同一条路：先在 Megatron 侧 TP/EP all-gather 到 PP-src rank，转换成 HF 张量，再整份发给引擎。

问题在于：**SGLang 引擎在 EP（expert parallel）下，每个 rank 只需要一部分专家**。全量广播会把每个 rank 不需要的专家也塞过去，造成冗余的显存搬运与 IPC 流量。

`expert_routing` 模块（函数 `configure_expert_routing`）是一个**规划器**：在 `UpdateWeightFromTensor.connect_rollout_engines` 阶段被调用一次，判断当前拓扑是否满足「专家直传」条件。满足时，它产出一份 `_expert_transfer_plan`，让 `UpdateWeightFromTensor` 在专家更新阶段用 **P2P（`isend`/`irecv`）** 直接把每个专家从「持有它的 Megatron rank」送到「需要它的 SGLang rank」；不满足时返回空 plan，回退到普通 all-gather + 整份发送。

#### 4.3.2 核心流程

`configure_expert_routing` 的整体结构是「一堆守卫条件 + 满足后建图」：

```
if full_param_info_buckets is None:        return (None, [])      # 无专家元数据
if use_distribute:                         return (None, [])      # 有远端引擎，禁用
if not engine_gpu_counts:                  return (None, [])      # 无共卡引擎
topology = _get_homogeneous_sglang_moe_topology(...)             # 要求所有引擎拓扑同构
if not _can_route_experts(...):            return (None, [])      # 不满足直传条件

# 满足条件：把参数分成 dense 与 expert
dense_infos / expert_infos = split(full_param_info_buckets)
if not expert_infos:                       return (None, [])      # 非专家模型

# 1. 确定「每个专家由哪个 Megatron rank 物理持有」
expert_infos = _resolve_expert_source_ranks(expert_infos, get_local_weight_names)
# 2. 算「每个 SGLang EP shard 对应哪些 colocated rank」
target_ranks = _get_expert_target_ranks(...)
# 3. 每个 expert → (info, layer, expert, target_ranks)
expert_params = _build_expert_params(expert_infos, target_ranks, num_experts)
# 4. 按 layer 分组，first-fit 装箱成 transfer batch（受 buffer_size 约束）
expert_transfer_plan = _build_expert_transfer_plan(expert_params, buffer_size)
dense_buckets = pack_param_info_buckets(dense_infos, buffer_size)
return (dense_buckets, expert_transfer_plan)
```

**直传条件 `_can_route_experts`**（全部 AND）：

- SGLang `pp_size == 1`（流水线分割会让专家归属复杂化）。
- SGLang `ep_size > 1`（EP=1 时每个 rank 都要全部专家，直传无收益）。
- 未启用 EPLB（expert load balance，会动态重排专家位置）。
- `ep_num_redundant_experts == 0`（无冗余专家）。
- `init_expert_location == "trivial"`（专家位置是平凡初始分布）。
- 未启用 `elastic_expert_backup`。
- Megatron 侧 `expert_tensor_parallel_world_size == 1`。
- **SGLang MoE-TP == 1**：即每个引擎占的 GPU 数 `engine_size == ep_size * moe_dp_size`（专家内部无张量并行）。

任一条件不满足就回退——这是稳妥的设计：只在「专家归属明确且一一对应」的简单拓扑下启用优化，复杂场景退回通用路径。

**专家 → 目标 rank 映射 `_get_expert_target_ranks`**：因为 MoE-TP=1，每个 EP shard 对应 `moe_dp_size` 个 rank（同一专家被 MoE-DP 复制）。函数把每个引擎的 GPU 区间按 `dp_rank * ep_size + ep_rank` 展开到对应 EP shard。

**P2P 传输 `_prepare_expert_weight_batch`**（在 `UpdateWeightFromTensor` 内）：对每个 transfer，源 rank 用 `dist.isend` 把张量发给各目标 rank，目标 rank 用 `dist.irecv` 接收，`dist.batch_isend_irecv` 批量提交。接收后 `convert_to_hf` 转 HF 命名，再走 `_send_hf_params`（IPC/NCCL）。

#### 4.3.3 源码精读

**规划入口 `configure_expert_routing`**：

[expert_routing.py:296-L377](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/update_weight/expert_routing.py#L296-L377) —— 一连串守卫，满足后建 plan 并返回 `(dense_buckets, expert_transfer_plan)`。

**直传条件 `_can_route_experts`**：

[expert_routing.py:113-L127](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/update_weight/expert_routing.py#L113-L127) —— 八个 AND 条件，任一不满足即回退。

**专家名匹配正则**：

[expert_routing.py:22-L22](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/update_weight/expert_routing.py#L22) —— `_ROUTED_EXPERT` 匹配 `...mlp.experts.linear_fc{1,2}.weight{N}`，用以从参数名里解析出 layer/expert/projection。

**目标 rank 映射 `_get_expert_target_ranks`**：

[expert_routing.py:141-L162](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/update_weight/expert_routing.py#L141-L162) —— `engine_size == ep_size * moe_dp_size` 时，把每个引擎区间展开到 EP shard。

**源 rank 解析 `_resolve_expert_source_ranks`**：

[expert_routing.py:212-L219](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/update_weight/expert_routing.py#L212-L219) —— `all_gather_object` 收集每个 rank 持有的专家名，确定每个专家的物理 owner rank。

**装箱 `_build_expert_transfer_plan` + `_pack_expert_transfer_batches`**：

[expert_routing.py:256-L288](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/update_weight/expert_routing.py#L256-L288) —— first-fit 装箱，按 `--update-weight-buffer-size` 限制每个 rank 的暂存字节，把多个 transfer 合进一个 batch 以摊薄 P2P 轮次。

**P2P 执行 `_prepare_expert_weight_batch`**（在 tensor 类内）：

[update_weight_from_tensor.py:193-L244](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/update_weight/update_weight_from_tensor.py#L193-L244) —— 源 rank `isend`、目标 rank `irecv`，`batch_isend_irecv` 批量提交；接收后 `convert_to_hf`。

**专家更新外层 `_update_expert_weights`**：

[update_weight_from_tensor.py:246-L274](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/update_weight/update_weight_from_tensor.py#L246-L274) —— 按 transfer_group/transfer_batch 两层遍历，复用 staging_buffers，每批后 `ipc_collect + empty_cache`。

#### 4.3.4 代码实践

**实践目标**：理解 expert 直传的「谁发给谁」，并能判断一个给定拓扑是否会启用。

**操作步骤**：

1. 假设有 8 个 Megatron rank（PP=1, EP=4, expert_TP=1），共 8 个专家（每 EP rank 持 2 个），SGLang 一个引擎 `ep_size=4, moe_dp_size=1`（占 4 张卡），colocate。
2. 手画一张表：每个专家编号（0-7）→ 物理持有它的 Megatron rank → 它要发往的 SGLang target rank 集合。
3. 用 `_get_expert_target_ranks`（L141）的公式 `gpu_offset + dp_rank * ep_size + ep_rank` 验证你的目标 rank 集合。
4. 把 `--sglang-ep-num-redundant-experts` 设成 2，重新走一遍 `_can_route_experts`（L113），确认现在会回退到普通路径。

**需要观察的现象**：

- 每个专家只从 1 个源 rank 发往少数几个目标 rank，没有全量广播。
- 改动任意一个守卫条件（如开 EPLB、加冗余专家、PP>1），整个优化即被关闭，日志出现 "Disable rank-local expert update: ..."。

**预期结果**：你能列出「专家 i → 源 rank r → 目标 ranks {…}」的完整映射表，并能解释 `_can_route_experts` 的每个条件为何是必要的。

**待本地验证**：P2P 行为需要真实多卡环境；无 GPU 时只做映射表手算与条件判断练习。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `use_distribute=True`（有远端引擎）时直接禁用 expert 直传？

**参考答案**：expert 直传依赖同机 P2P（`isend`/`irecv` 走 NVLink/PCIe）和 colocate 的 rank 映射；远端引擎不在同一 NCCL WORLD 子集里，P2P 不可达。此时回退到通用 all-gather + `update_weights_from_distributed`（NCCL broadcast）更简单可靠（见 L309-L311）。

**练习 2**：`_can_route_experts` 为什么要求 SGLang MoE-TP == 1？

**参考答案**：MoE-TP>1 意味着一个专家被切到多张卡上，单个 rank 收到的不是完整专家张量，无法直接 `irecv` 后装载。MoE-TP=1 保证每个 target rank 拿到的就是一份完整的专家权重（见 `_sglang_moe_tp_is_one`，L130-L138）。

---

## 5. 综合实践

**任务**：为下面的部署画出一次 `update_weights()` 的完整数据流图，并说明每一类参数走的路径。

**部署配置**：

- Megatron 训练：PP=1，EP=4，expert_TP=1，MoE 模型（含 dense + 专家权重），colocate（训练与推理共卡）。
- SGLang 推理：单引擎组，`ep_size=4, moe_dp_size=1`，占 4 张卡，与训练同机。

**要求**：

1. 标出 dense（非专家）参数走的路径：经 `get_hf_weight_chunks` → `_send_hf_params` → `_send_to_colocated_engine`（Gloo gather + IPC）。
2. 标出专家参数走的路径：经 `_update_expert_weights` → `_prepare_expert_weight_batch`（P2P isend/irecv）→ `convert_to_hf` → `_send_hf_params`。
3. 在图上标出 `pause_generation / flush_cache / continue_generation` 的位置，并解释为何 `flush_cache` 必须在装载新权重之前。
4. 回答：若把这个部署改成「推理在远端独立集群 + 共享文件系统」，并把模式切到 `--update-weight-mode=delta --update-weight-transport=disk`，数据流会如何变化？dense 和专家参数分别经 `_iter_non_expert_chunks` / `_iter_expert_chunks` gather 后，在哪一步变成「增量」？

**提示**：

- `flush_cache` 必须在前，因为旧权重生成的 KV cache 对新权重是「非法」的，继续用会导致采样错乱。
- 切到 delta 后，`update_weights` 首次调用走 `_capture_baseline`，之后走 `_publish`（`_encode_delta` 把 gathered 张量与快照 diff）→ `_reload_engines`（`pull_weights` apply 补丁 + `update_weights_from_disk` 重载）。

**预期产出**：两张数据流图（colocate tensor 路径、delta+disk 路径）+ 一段说明，指出三条核心差异：传输载体（IPC 句柄 vs 增量文件）、是否需要基线快照（否 vs 是）、专家是否仍可直传（colocate 可，delta 走 EP all-gather 后整体 diff）。

## 6. 本讲小结

- slime 用一个工厂（[actor.py:145-L168](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/actor.py#L145-L168)）按 `mode/transport/colocate` 选出传输类：delta+disk → `UpdateWeightFromDiskDelta`；colocate → `UpdateWeightFromTensor`；远端 full+nccl → `UpdateWeightFromDistributed`。
- `UpdateWeightFromTensor` 是 colocate 默认路径，共卡引擎走 CUDA IPC（只传句柄），远端引擎走 NCCL broadcast，二者在 `_send_hf_params` 里并存；它不复用 IPC buffer 以规避 CUDA sync 竞争。
- `UpdateWeightFromDiskDelta` 用「快照→diff→发布→拉取重载」三态机，每轮只写变化的张量；首次调用只建基线；diff 支持 xor（对合、需精确基线）与 overwrite（幂等、自描述）两种编码，配 zstd 压缩与 checksum 校验。
- xor 与 overwrite 的根本差别：xor 存「差异比特」、重复 apply 会拨回原状（非幂等、对合）；overwrite 存「位置→新值」、重复 apply 结果不变（幂等、不可逆）。
- `configure_expert_routing` 是 MoE 优化规划器：在 PP=1、SGLang EP>1、MoE-TP=1 等严格条件下，让专家权重走 rank-local P2P 直传，省去冗余全量广播；不满足时安全回退到通用路径。
- 三类传输共享同一套前置（pause/flush）与后置（continue）仪式，以及 `convert_to_hf` 转换；差别只在「张量怎么到引擎」。

## 7. 下一步学习建议

- **u5-l3（SGLang 引擎封装与生命周期）**：本讲的传输类最终都调用 `engine.update_weights_from_tensor` / `update_weights_from_disk` / `pull_weights`。下一讲从引擎侧看这些端点如何接收并装载权重，以及 `pause/flush/continue` 在引擎内部的真实作用。
- **u5-l4（Megatron 权重服务端）**：如果想看「反向」——引擎如何反过来请求 Megatron 算 logprob/采样——可对照阅读 `server/megatron_server.py`。
- **进阶阅读**：若你对增量同步的性能感兴趣，可在真实训练中观察 `perf/update_weights_density` 与 `perf/update_weights_wire_bytes` 两条指标随训练步数的变化曲线，验证「相邻两轮权重变化很小」这一增量假设是否成立。
