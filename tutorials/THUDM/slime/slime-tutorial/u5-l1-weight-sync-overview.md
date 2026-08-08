# 权重同步全景：训练参数如何抵达推理引擎

## 1. 本讲目标

本讲是单元 U5（权重同步与推理后端）的第一讲，建立整个权重同步子系统的「鸟瞰图」。学完后你应该能够：

- 说清楚权重同步（weight sync）在 slime 闭环里的**位置**与**触发时机**——为什么训练完必须把新权重推给推理引擎，以及它在 `train.py` 主循环的哪一步发生。
- 看懂 `update_weight` 模块的**入口选择逻辑**：`--update-weight-mode`（full/delta）× `--update-weight-transport`（nccl/disk）如何决定走哪条传输类。
- 理解 `all_gather_param` 如何把 Megatron 按 TP/EP 切片的参数**聚合回完整张量**，以及它的异步重叠版本 `all_gather_params_async`。
- 理解 `named_params_and_buffers` 如何用一套**全局命名**统一枚举分布在各 PP/EP rank 上的参数与缓冲区。

本讲只讲「全景 + 共享工具」两个最小模块（模块入口、`all_gather_param`、`named_params_and_buffers`）。三种具体传输类（tensor/IPC、disk full、disk delta）的内部细节留给 U5 后续讲义（u5-l2、u5-l3、u5-l4）展开。

## 2. 前置知识

读本讲前，请先建立以下认知（来自前置讲义）：

- **闭环三模块**（u2-l1）：rollout（SGLang 推理生成）→ data buffer → training（Megatron 训练），训后把权重**单向**同步回 rollout。
- **Megatron 张量并行/专家并行**（u4-l5）：Megatron 把模型权重按 `tensor_model_parallel`（TP）、专家并行（EP）、流水线并行（PP）切成分片，每个 rank 只持有自己那份。`param.partition_dim`、`param.tensor_model_parallel` 是 Megatron 给参数贴的并行属性标签。
- **colocate 与 offload**（u1-l4、u4-l1）：`--colocate` 让训练与推理共用同一组 GPU，靠 offload 轮流让出显存；分离部署时训练卡与推理卡是不同物理卡。
- **HF 与 Megatron 格式区别**（u1-l5）：HF 检查点是完整、未分片的 `safetensors`；Megatron 是按并行策略分片的 `torch_dist`。推理引擎（SGLang）读的是 **HF 布局**。

一个关键矛盾，是本讲全部内容的出发点：

> Megatron 训练工人手里是**按 TP/PP/EP 分片的 torch 权重**，而 SGLang 推理引擎需要的是**HF 命名、完整张量**，且两者可能分布在**不同物理机/不同进程**上。每次训练更新后，必须有一套机制把前者转成后者并送达后者。

`update_weight` 模块就是干这件事的。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲是否精读 |
| --- | --- | --- |
| `slime/backends/megatron_utils/actor.py` | 训练工人 `MegatronTrainRayActor`；其中 `update_weights()` 方法驱动权重同步，并按参数选择具体传输类 | 精读入口选择（L145-175）与 `update_weights()`（L571-617） |
| `slime/backends/megatron_utils/update_weight/__init__.py` | 包初始化文件，**内容为空**——真正的入口在各传输类文件 | 说明（不精读） |
| `slime/backends/megatron_utils/update_weight/common.py` | 共享工具：`all_gather_param` / `all_gather_params_async`（TP/EP 分片聚合）、`named_params_and_buffers`（全局命名枚举） | **本讲核心精读** |
| `slime/backends/megatron_utils/update_weight/update_weight_from_tensor.py` | full+nccl+colocate：CUDA IPC 传输 | 仅引用类文档 |
| `slime/backends/megatron_utils/update_weight/update_weight_from_disk.py` | full+disk：写完整 HF 检查点，引擎重载 | 仅引用类文档 |
| `slime/backends/megatron_utils/update_weight/update_weight_from_distributed.py` | full+nccl+分离：NCCL broadcast 到远端引擎 | 引用关键方法 |
| `slime/backends/megatron_utils/update_weight/update_weight_from_disk_delta.py` | delta+disk：与 CPU 快照 diff，只发变化字节 | 仅引用类文档 |
| `slime/utils/arguments.py` | `--update-weight-mode` / `--update-weight-transport` 等参数定义与校验 | 精读参数定义与校验 |
| `train.py` | 训练主循环；`update_weights()` 的调用点即触发时机 | 精读触发点 |

> 注意：`update_weight/__init__.py` 是空文件，所以 `update_weight` **不是一个统一的入口函数**，而是一个**目录/模块**，真正的「入口」是 `actor.py` 里的选择逻辑（见 4.1.3）。

## 4. 核心概念与源码讲解

本讲覆盖三个最小模块：**①update_weight 模块入口（mode×transport 四象限）**、**②all_gather_param 分片聚合**、**③named_params_and_buffers 全局枚举**。

### 4.1 update_weight 模块入口：mode × transport 的四象限

#### 4.1.1 概念说明

「权重同步」解决的问题是：RL 训练是 **on-policy** 的——rollout 阶段必须用「刚刚训练完的最新策略」去生成下一批数据。训练在 Megatron 工人里更新了参数，但这些参数躺在训练进程的 GPU 上，SGLang 推理引擎用的是旧副本。所以每个训练步之后，必须把训练工人手里的最新权重**搬运并注入**到推理引擎里，引擎才能用它采样出真正 on-policy 的样本。

这件事在闭环里的位置（承接 u2-l1）：训练（`async_train`）→ 保存（`save_model`，周期触发）→ **权重同步（`update_weights`）** → 评估（周期触发）。也就是说，权重同步是「训练」与「下一轮采样」之间**必经的桥梁**，方向永远是 training→rollout 单向（推理权重只是副本，反向写回会污染优化器状态）。

为什么要专门设计一个「模块」而不是写死一段代码？因为搬运方式强烈依赖**集群拓扑**与**显存/带宽条件**：

- 训练和推理**共卡**（colocate）时，两进程在同一 GPU 上，可以走 CUDA IPC，只跨进程传一个内存句柄，几乎零拷贝。
- 训练和推理**分离**在不同卡/不同机时，要走网络（NCCL broadcast）。
- **带宽很差**（跨机、对象存储文件系统）或**模型很大**时，全量搬运太贵，可以只发**变化的部分**（delta）。
- 有时甚至不想占训练 GPU 的带宽，宁可**写盘**让引擎从磁盘重载。

slime 把这些差异抽象成两个正交开关：

- `--update-weight-mode`：`full`（每次搬全部参数）或 `delta`（只搬相对上一次变化的字节）。
- `--update-weight-transport`：`nccl`（走 GPU 网络/IPC）或 `disk`（写共享文件系统，引擎从盘重载）。

两个开关组合出「四象限」，每象限对应一个传输类。本模块（4.1）就是讲清楚这张表是怎么被选出来的。

#### 4.1.2 核心流程

权重同步的整体生命周期由 `MegatronTrainRayActor` 编排，分三步：

1. **构建期（init）**：根据 `args.update_weight_mode` / `args.update_weight_transport` / `args.colocate` 三个量，选定一个传输类，实例化为 `self.weight_updater`。`weights_getter` 是一个闭包，返回「最新 actor 权重」的字典（由 `weights_backuper` 提供）。
2. **连接期（connect）**：训练工人与推理引擎建立传输通道。NCCL 类要建 NCCL 通信组；IPC 类要建 Gloo 收集组并映射「训练 rank → 哪个共位引擎」；disk 类几乎不做事（靠共享目录）。
3. **同步期（update）**：每轮训练后调用 `weight_updater.update_weights()`，统一遵循 `暂停生成 → flush 缓存 → 传权重 → 恢复生成` 的节奏（pause→flush→update→continue）。

选择逻辑可以用下面的伪代码表达（对应真实代码 actor.py:145-168）：

```text
if mode == "delta":                 # delta 只能配 disk
    class = UpdateWeightFromDiskDelta
elif transport == "disk":           # full + disk
    class = UpdateWeightFromDisk
elif colocate:                       # full + nccl + 共卡
    class = UpdateWeightFromTensor          # 走 CUDA IPC
else:                                # full + nccl + 分离
    class = UpdateWeightFromDistributed     # 走 NCCL broadcast
```

注意一个细节：nccl 传输在 `colocate` 与否时**分叉成两个不同的类**。所以「四象限」更准确地说是「mode × transport」再加上「nccl 下是否 colocate」的一个子分叉，共对应 4 个类。

#### 4.1.3 源码精读

**入口选择逻辑**在 `actor.py` 的 `init` 末尾：

[slime/backends/megatron_utils/actor.py:L145-L168](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/actor.py#L145-L168) — 读取 `update_weight_mode` 与 `update_weight_transport`，用 if/elif 链选出具体传输类。`delta` 模式有两条断言：不能与 `--colocate` 同用（共卡时 IPC 只传句柄，delta 的快照+diff 全是白干），且必须配 `disk`。

选定后实例化：

[slime/backends/megatron_utils/actor.py:L169-L176](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/actor.py#L169-L176) — 把 `model`、`weights_getter`（闭包 `lambda: self.weights_backuper.get("actor")`，即取最新 actor 权重）、`model_name`、`quantization_config`（量化配置，用于 fp8/int4 权重转换）传给传输类，存为 `self.weight_updater`，并初始化 `weight_version`。

四个传输类的职责，直接看它们的类文档字符串最清楚：

- **`UpdateWeightFromTensor`**（colocate，CUDA IPC）：

  [slime/backends/megatron_utils/update_weight/update_weight_from_tensor.py:L50-L56](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/update_weight/update_weight_from_tensor.py#L50-L56) — 流程为 `load(dict→GPU) → broadcast PP/EP(GPU NCCL) → gather TP(GPU NCCL) → convert HF(GPU) → send`；共位时 GPU→CPU 序列化后用 `gather_object`（Gloo，CPU）收集到 `rollout_num_gpus_per_engine` 个 rank，再经 Ray IPC 送给引擎；分离引擎则走 GPU NCCL broadcast。它其实是「共位 IPC + 分离 NCCL」的混合体（见 `use_distribute` 字段）。

- **`UpdateWeightFromDistributed`**（分离，NCCL broadcast）：

  [slime/backends/megatron_utils/update_weight/update_weight_from_distributed.py:L23-L28](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/update_weight/update_weight_from_distributed.py#L23-L28) — 每个 PP rank 建一组 `slime-pp_{pp_rank}` NCCL 组，只有 `DP=TP=0` 的源 rank 负责广播；非专家（TP）参数与专家（EP）参数分两趟传输。

- **`UpdateWeightFromDisk`**（full + disk）：

  [slime/backends/megatron_utils/update_weight/update_weight_from_disk.py:L17-L18](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/update_weight/update_weight_from_disk.py#L17-L18) — 通过共享文件系统写一份完整 HF 检查点，引擎从盘重载。

- **`UpdateWeightFromDiskDelta`**（delta + disk）：

  [slime/backends/megatron_utils/update_weight/update_weight_from_disk_delta.py:L30-L37](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/update_weight/update_weight_from_disk_delta.py#L30-L37) — PP 源 rank 把每个聚合后的 HF 张量与上一次同步的 CPU 快照 diff，只把**变化部分**作为规范 HF 检查点目录发布；每个引擎的 `/pull_weights` 把增量应用到本机检查点，再用普通 `update_weights_from_disk` 重载。它继承自 `UpdateWeightFromDistributed`，复用了 TP/EP 聚合迭代器（见 4.2、4.3）。

**触发时机**——权重同步在主循环哪里发生：

[slime/backends/megatron_utils/actor.py:L571-L617](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/actor.py#L571-L617) — `MegatronTrainRayActor.update_weights()`：先从 `rollout_manager` 拿到「可更新的引擎列表 + 锁 + GPU 偏移」；若有新引擎或需要重连（offload_train+critic 场景），调 `weight_updater.connect_rollout_engines(...)` 建通道；最后在 `torch_memory_saver.disable()` 上下文里调 `self.weight_updater.update_weights()` 真正搬运。

[train.py:L27](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/train.py#L27) 与 [train.py:L85](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/train.py#L27) — 同步主循环里，`update_weights()` 出现两次：一次是循环前的**初始推送**（把加载好的初始 actor 权重灌进引擎），一次是每轮 `async_train` + `save_model` 之后的**常规同步**。注意同步循环（`train.py`）每轮都同步；而异步循环（`train_async.py:66`）按 `(rollout_id+1) % update_weights_interval == 0` 节流，可以隔几轮同步一次以让采样与训练重叠。

**参数定义与校验**——`--update-weight-mode` 与 `--update-weight-transport` 的定义和默认值：

[slime/utils/arguments.py:L124-L152](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/utils/arguments.py#L124-L152) — `--update-weight-mode` 默认 `full`；`--update-weight-transport` 默认 `nccl`。help 文字里明确写出 delta 只支持 disk。

[slime/utils/arguments.py:L1973-L2010](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/utils/arguments.py#L1973-L2010) — 校验逻辑：disk 传输必须有 `--update-weight-disk-dir`（trainer 与引擎共享的文件系统）；delta 必须配 disk、不能 colocate、且必须有 `--update-weight-local-checkpoint-dir`（每台推理机的本地 NVMe 目录）。

> **默认是哪一种？** 默认 `mode=full, transport=nccl`。由于选择链里 `colocate` 在 `else` 之前分叉：**colocate 时默认 `UpdateWeightFromTensor`（IPC），分离时默认 `UpdateWeightFromDistributed`（NCCL broadcast）**。这也是 slime 在大多数共卡训练场景下真正走的那条路。

#### 4.1.4 代码实践

本实践对应规格里的实践任务：画一张表，列出四种组合的适用条件。

1. **实践目标**：把「mode × transport（+ colocate 分叉）」四象限整理成一张选型决策表，并标出 slime 默认值。
2. **操作步骤**：
   - 打开 [actor.py:L145-L168](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/actor.py#L145-L168) 与 [arguments.py:L124-L152](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/utils/arguments.py#L124-L152)，逐条核对你表格里每一行的选择条件与所需附加参数。
   - 在笔记里画出下面的选型表（参考答案见 4.1.5，但建议先自己填）。
3. **需要观察的现象**：当你**故意**给出矛盾组合（例如 `--update-weight-mode=delta --update-weight-transport=nccl`）时，应该在参数校验阶段（`slime_validate_args`）就被 `raise ValueError` 拦下，而不是跑到 actor 里才崩。
4. **预期结果**：四象限对应四类，默认 `full+nccl`，按 colocate 二分。
5. 运行行为为「待本地验证」（需要真实 GPU 集群）；但参数校验的报错可以用 `python -c` 构造一个 `Namespace` 后调用校验函数本地复现，无需 GPU。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `delta` 模式被禁止与 `--colocate` 同用？

> **答案**：colocate 时训练与推理同卡，权重走 CUDA IPC，**只跨进程传一个内存句柄**，几乎零拷贝、零带宽。delta 的「维护 CPU 快照 + diff + 编码」相对这个几乎免费的 IPC 全是纯开销，没有收益，所以代码用断言直接禁止（见 actor.py:152 与 arguments.py:2003-2006 的报错文字）。

**练习 2**：默认参数（不改任何 `--update-weight-*`）下，colocate 训练和分离训练分别会选哪个传输类？

> **答案**：默认 `mode=full, transport=nccl`。选择链里 `colocate` 分支在 `else` 之前：colocate 命中 `elif self.args.colocate` → `UpdateWeightFromTensor`（IPC）；分离落到 `else` → `UpdateWeightFromDistributed`（NCCL broadcast）。

**练习 3**：`--release-train`（每轮训练杀掉 Megatron 进程、下轮重建）为什么被强制要求 `full + disk`？

> **答案**：release-train 训练工人会在 rollout 期间被销毁，无法保持一个常驻的 NCCL 通道或 IPC 句柄来「在线」传权重；只能把权重**持久化到磁盘**（full HF 检查点），让重新拉起的下一轮 Megatron / 引擎从盘重载。所以 arguments.py:1993-1994 强制 `--update-weight-mode=full` 且 `--update-weight-transport=disk`。

### 4.2 all_gather_param：把 TP/EP 分片聚合回完整张量

#### 4.2.1 概念说明

回顾 4.1 的核心矛盾：Megatron 工人手里的参数是**分片**的，推理引擎要的是**完整 HF 张量**。所以无论走哪条传输类，第一步都一样——**把当前 rank 持有的分片聚合成完整张量**。这就是 `all_gather_param` 的职责。

为什么不能直接 `param.data` 给引擎？因为：

- **TP 分片**：例如一个 `[hidden, 4*hidden]` 的权重在 TP=4 时被切成 4 份，每个 rank 只有 `[hidden, hidden]`。必须沿 `partition_dim` 把 4 份 `torch.cat` 拼回去。
- **EP 分片**：MoE 的专家被分散到各 EP rank，每个 rank 只持有部分专家；专家参数用**专家 TP 组**而非普通 TP 组聚合。
- **非分片参数**：`tensor_model_parallel=False` 或 `parallel_mode="duplicated"` 的参数（如 LayerNorm）每个 rank 都一样，直接返回 `param.data` 即可，不必通信。

还有两类需要**特殊处理**的「形状修正」：

- `linear_fc1`（GLU 激活的上投影）：Megatron 把 gate 和 up 两个矩阵拼在一起切成两半，全聚合后要按「先所有前半、再所有后半」重新 `chunk`，否则 gate/up 会交错错位。
- `linear_fc2`（grouped MoE 的下投影）：Megatron grouped MoE 有一个已知 bug，`partition_dim` 标的是 0 但实际应是 1，这里手动修正。
- `expert_bias`：专家偏置不是 `nn.Parameter` 而是 buffer，且不分片，直接返回。

#### 4.2.2 核心流程

`all_gather_param(name, param)` 的判断与聚合流程：

```text
if "expert_bias" in name:        return param           # buffer，不分片
if not param.tensor_model_parallel or parallel_mode=="duplicated":
                                 return param.data       # 非分片/复制参数
tp_group = expert-TP group if ".experts." in name else regular-TP group
tp_size  = 对应 world_size
if tp_size == 1:                 return param.data       # 没切，无需聚合

# 真正的聚合：
buffers = [empty_like(param.data) for _ in range(tp_size)]
dist.all_gather(buffers, param.data, group=tp_group)      # 每个 rank 拿到全部 tp_size 份
dim = param.partition_dim
# 形状修正：
if "linear_fc1.*" in name:  把每份再 chunk(2)，重排成 [前半们..., 后半们...]
if "linear_fc2.weight" in name and dim==0:  dim = 1       # 修 grouped MoE bug
return torch.cat(buffers, dim=dim)
```

它还有一个**异步重叠版本** `all_gather_params_async`，一次处理一批参数：分三阶段——①循环发起所有 `all_gather(async_op=True)` 并保存句柄；②统一 `handle.wait()` 等全部完成（让通信彼此重叠）；③再统一做 concat 与形状修正。这样多个参数的 NCCL 通信可以并行铺满带宽，而不是「聚一个、等一个、再聚下一个」。

#### 4.2.3 源码精读

[slime/backends/megatron_utils/update_weight/common.py:L15-L57](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/update_weight/common.py#L15-L57) — `all_gather_param`：先按 `expert_bias` / 非分片 / `tp_size==1` 短路返回；其余按 `.experts.` 选专家 TP 组或普通 TP 组，`dist.all_gather` 收集所有分片，再用 `partition_dim` 拼。L49-51 是 GLU `linear_fc1` 的「再 chunk + 重排」，L53-55 是 grouped MoE `linear_fc2` 的 `partition_dim` 修正。

[slime/backends/megatron_utils/update_weight/common.py:L60-L127](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/update_weight/common.py#L60-L127) — `all_gather_params_async`：三阶段批量聚合。注意它对所有传入参数都参与 `all_gather`（包括非源 rank），但**只有需要聚合的**才真正发起通信、保存 handle；非分片的直接塞 `param.data`，避免无谓通信。L102-103 统一等待所有 handle，实现通信重叠。

**它在哪被用？** 主要在 `UpdateWeightFromDistributed` 的迭代器里逐参数调用（见 u5-l4 的传输细节）。例如：

[slime/backends/megatron_utils/update_weight/update_weight_from_distributed.py:L153-L176](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/update_weight/update_weight_from_distributed.py#L153-L176) — `_iter_non_expert_chunks`：遍历 `named_params_and_buffers`，跳过专家，对每个参数调 `all_gather_param(name, param)` 聚合，再 `convert_to_hf` 转 HF 布局，按 `--update-weight-buffer-size`（默认 512MiB，见 arguments.py:517-521）攒成广播桶。注意：**所有 rank 都参与 `all_gather_param`**（NCCL 集合通信要求组内全员参与），但只有 `_is_pp_src_rank`（DP=TP=0）才继续做 HF 转换与广播。

> 关键约定：`all_gather_param` 是一个 **NCCL 集合通信**操作。集合通信要求通信组内**所有 rank 同步参与**，所以即使某个 rank 不是最终广播源，它也必须调用 `all_gather_param`（否则死锁）。这就是为什么迭代器里 `all_gather_param` 在 `if not self._is_pp_src_rank: continue` **之前**——先全员聚合，非源 rank 再退出。

#### 4.2.4 代码实践

1. **实践目标**：理解 GLU `linear_fc1` 重排的必要性。
2. **操作步骤**：阅读 [common.py:L49-L51](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/update_weight/common.py#L49-L51)。在笔记里画一个 TP=2 的小例子：假设 `linear_fc1.weight` 完整形状是 `[2*H, H]`（gate 和 up 各 `[H,H]` 纵向拼接），每个 rank 持有 `[H, H]`（含「半个 gate + 半个 up」）。
   - 先 `all_gather` 得到两份 `[H, H]`：rank0 的 `[gate上半, up上半]`、rank1 的 `[gate下半, up下半]`。
   - 如果直接 `cat(dim=0)` 会得到 `[gate上半, up上半, gate下半, up下半]`——gate 与 up 交错，**错**。
   - 按 L50-51 的逻辑：每份 `chunk(2, dim=0)` 得 `[[gate半, up半], [gate半, up半]]`，重排成 `[gate半(rank0), gate半(rank1), up半(rank0), up半(rank1)]`，即 `[gate全, up全]`——**对**。
3. **需要观察的现象**：直观看清「为什么 naive cat 会交错、为什么重排能修复」。
4. **预期结果**：在笔记里得到正确的 `[gate 全量; up 全量]` 顺序。无需运行（纯形状推理）。
5. 这是「源码阅读型实践」，行为「待本地验证」可跳过——本练习不依赖运行时。

#### 4.2.5 小练习与答案

**练习 1**：一个 `tensor_model_parallel=False` 的 LayerNorm 权重，调用 `all_gather_param` 会发生 NCCL 通信吗？

> **答案**：不会。L25-26 判断 `not param.tensor_model_parallel` 直接返回 `param.data`，根本不进 `dist.all_gather`。这类「复制」参数每个 rank 都一样，无需聚合。

**练习 2**：为什么 `all_gather_params_async` 要把「发起通信」「等待」「拼接」拆成三个循环，而不是每个参数「发起→等待→拼接」一条龙？

> **答案**：为了让多个参数的 NCCL 通信**重叠**。如果每个参数都立即 `wait`，通信是串行的——发一个、等一个、再发下一个，带宽利用率低。三阶段把所有 `all_gather(async_op=True)` 先批量发起（CUDA 流上排队），再统一等，NCCL 能在底层并行推进多个集合操作，把带宽铺满。拼接是纯本地计算，放最后做即可。

### 4.3 named_params_and_buffers：统一枚举所有参数与缓冲区

#### 4.3.1 概念说明

`all_gather_param` 解决「单个参数怎么聚合」，但它需要一个**参数清单**——要知道模型里有哪些参数、各自叫什么名字、分别由哪个 rank 持有。这就是 `named_params_and_buffers` 的职责：它是一个生成器，按一套**全局命名**产出 `(name, tensor)` 序列。

为什么需要「全局命名」？因为 Megatron 模型在不同 PP/EP rank 上的**结构是不同的**：

- **PP（流水线）**：rank 0 持有前几层，rank 1 持有后几层。模块路径里的 `decoder.layers.0` 在不同 PP rank 上指的是**不同**的全局层号。必须加上 `layer_offset` 把局部层号翻译成全局层号，否则两个 PP rank 都会声称自己有「第 0 层」。
- **VP（虚拟流水线）**：一个 PP rank 在不同 vp_stage 持有不连续的层段，也要折算。
- **EP（专家并行）**：每个 EP rank 只持有部分专家，模块路径里的 `experts.*.weight3` 的 `3` 是**本 rank 局部**专家号，要加上 `expert_offset = ep_rank * num_experts // ep_size` 折成全局专家号。
- **MTP（多 token 预测）**：投机解码用的额外层，命名空间是 `mtp.layers` 而非 `decoder.layers`，单独处理。

只有所有 rank 对「同一个物理权重」产出**相同的全局名字**，后续的聚合、diff、广播才能正确对齐。

此外还有一个 `expert_bias` 的细节：MoE 的专家偏置在 Megatron 里注册为 **buffer 而非 parameter**（不参与反向），但权重同步需要它，所以这里把名为 `expert_bias` 的 buffer 当作普通参数一起 yield。

#### 4.3.2 核心流程

`named_params_and_buffers(args, model, convert_to_global_name=True, translate_gpu_to_cpu=False)` 的两条路径：

```text
if convert_to_global_name:
    迭代器 = _named_params_and_buffers_global(args, model)   # 默认：全局命名
else:
    迭代器 = _named_params_and_buffers_vanilla(model)        # 朴素：带 vp_stage 前缀

if translate_gpu_to_cpu:
    对每个 tensor，若 torch_memory_saver 有 CPU 备份则换成 CPU 版（零拷贝）

yield (name, tensor)
```

`_named_params_and_buffers_global` 对每个参数：

```text
算 layer_offset（含 vp_stage 折算）
用正则匹配 decoder.layers.<局部层号>.<rest> 或 mtp.layers.<局部层号>.<rest>
  -> 全局层号 = 局部层号 + layer_offset
  -> 若 rest 命中 experts.<x>.(weight|bias)<局部专家号>:
       全局专家号 = 局部专家号 + expert_offset
       yield "...decoder.layers.<全局层号>.mlp.experts.<x>.<type><全局专家号>"
  -> 否则 yield "...decoder.layers.<全局层号>.<rest>"
对每个 buffer：仅当名字含 "expert_bias" 才 yield（同样做层号折算）
```

#### 4.3.3 源码精读

[slime/backends/megatron_utils/update_weight/common.py:L130-L144](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/update_weight/common.py#L130-L144) — `named_params_and_buffers` 入口：按 `convert_to_global_name` 选实现，再可选地把 GPU 张量换成 `torch_memory_saver` 的 CPU 备份（用于 offload 场景，避免碰已换出的 GPU 内存）。

[slime/backends/megatron_utils/update_weight/common.py:L147-L153](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/update_weight/common.py#L147-L153) — `_maybe_get_cpu_backup`：用 `torch_memory_saver.get_cpu_backup(zero_copy=True)` 拿 CPU 副本；没有则原样返回。这是 offload 训练时权重同步能从 CPU 快照读取的关键。

[slime/backends/megatron_utils/update_weight/common.py:L172-L251](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/update_weight/common.py#L172-L251) — `_named_params_and_buffers_global`：核心正则折算。
- L179-182 算 `expert_offset = ep_rank * num_experts // ep_size`。
- L184-191 用 `inspect.signature` 探测 `get_transformer_layer_offset` 是否接受 `vp_stage` 参数（兼容不同 Megatron 版本），算出当前段的层偏移。
- L198-206 用正则区分 `decoder.layers` 与 `mtp.layers`，未命中的参数原样 yield。
- L221-232 把局部层号加 `layer_offset` 折成全局层号；命中专家模式的再加 `expert_offset` 折全局专家号。
- L235-251 单独处理 buffer，只放行 `expert_bias`，并对其做同样的层号折算。

[slime/backends/megatron_utils/update_weight/common.py:L156-L169](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/update_weight/common.py#L156-L169) — `_named_params_and_buffers_vanilla`：朴素路径，名字加 `vp_stages.{vp_stage}.` 前缀，不做全局折算；用于不需要跨 rank 对齐命名的场景。

**它在哪里被消费？** 在 `UpdateWeightFromDistributed._iter_non_expert_chunks`（4.2.3 已引用的 update_weight_from_distributed.py:153-176）和 `_iter_expert_chunks`（同文件 L178-202）里被遍历——`named_params_and_buffers` 产出清单，`all_gather_param` 逐个聚合，`convert_to_hf` 转 HF，三者串成「枚举→聚合→转换」的标准流水。

#### 4.3.4 代码实践

1. **实践目标**：用最小的 MoE 拓扑手算 `expert_offset`，验证全局专家号折算。
2. **操作步骤**：假设 `num_experts=8`，`ep_size=2`（2 个 EP rank）。
   - 阅读 [common.py:L179-L182](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/update_weight/common.py#L179-L182) 与 [common.py:L225-L230](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/update_weight/common.py#L225-L230)。
   - 计算：EP rank 0 持有局部专家号 0..3，EP rank 1 持有局部专家号 0..3。
   - 折算后：rank 0 的 `expert_offset = 0*8//2 = 0`，全局专家号 0..3；rank 1 的 `expert_offset = 1*8//2 = 4`，全局专家号 4..7。
3. **需要观察的现象**：两个 EP rank 用同一套全局命名（0..7 无重复无遗漏）指代 8 个专家，从而 diff/广播时不会撞名。
4. **预期结果**：在笔记里列出「局部专家号 → 全局专家号」的两张映射表。
5. 纯算术实践，无需运行；运行验证「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `expert_bias` 是从 `named_buffers` 里 yield 而不是 `named_parameters`？

> **答案**：在 Megatron 的 MoE 实现里，专家偏置被注册为 buffer（`register_buffer`），不是 `nn.Parameter`——它不参与反向、不需要梯度。所以 `named_parameters()` 取不到它。但推理引擎需要它来做前向，所以 L235-251 专门遍历 `named_buffers()`，用 `"expert_bias" in name` 过滤后当作权重 yield。

**练习 2**：`translate_gpu_to_cpu=True` 这个选项在什么场景下会被用到？

> **答案**：offload 训练时，训练工人会让出 GPU、把权重换出到 CPU（由 `torch_memory_saver` 管理）。此时若权重同步直接读 GPU 张量，会触发把权重重新换回 GPU（破坏 offload 的显存节省）。`translate_gpu_to_cpu=True`（经 `_maybe_get_cpu_backup`）改为从 `torch_memory_saver` 的 CPU 备份零拷贝读取，避免唤醒 GPU 权重。这正是 actor.py 里 `with torch_memory_saver.disable()` 上下文与 offload 配合的体现。

## 5. 综合实践

把本讲三个最小模块串起来，完成一次「源码走查 + 选型决策」：

**任务**：你要给一个 **MoE 大模型 + 跨机分离部署（训练 8 卡、推理 8 卡、不同节点）、对象存储共享文件系统（跨机读后写一致性差）** 的场景，选出最合适的权重同步配置，并画出一次 `update_weights()` 的内部调用链。

**步骤**：

1. **选型**：对照 4.1 的四象限表。
   - colocate？否（分离部署）→ 排除 `UpdateWeightFromTensor`。
   - 跨机对象存储带宽差、模型大（MoE）→ 考虑 delta 省带宽。但 delta 要求每台推理机有本地 NVMe（`--update-weight-local-checkpoint-dir`）。
   - 若推理机有本地 NVMe → 选 **delta + disk**（`UpdateWeightFromDiskDelta`），配 `--update-weight-delta-encoding`（xor 最省、overwrite 幂等）。
   - 若推理机没有本地 NVMe 或求稳 → 选 **full + disk**（`UpdateWeightFromDisk`）。
   - 写出你的选择与理由，并指出它需要的附加参数（`--update-weight-disk-dir`、可选 `--update-weight-local-checkpoint-dir`、可选 `--custom-update-weight-post-write-path` 用于对象存储发布）。

2. **画调用链**：以 `UpdateWeightFromDistributed`（full+nccl+分离）为例，画出：
   ```
   actor.update_weights()
     → weight_updater.connect_rollout_engines(...)   # 建 NCCL 组 slime-pp_{pp}
     → weight_updater.update_weights()
          → engine.pause_generation / flush_cache      # rank0 暂停推理
          → _send_weights():
               → _iter_non_expert_chunks():
                    named_params_and_buffers(...)        # 4.3：枚举全局命名
                       → all_gather_param(name, param)   # 4.2：TP 聚合（全员参与）
                       → convert_to_hf(...)              # 转 HF 布局
               → _update_bucket_weights_from_distributed():  # NCCL broadcast rank0→引擎
               → _iter_expert_chunks(): ... 同理处理专家（EP 聚合）
          → engine.continue_generation                  # rank0 恢复推理
   ```
   标注：哪一步是集合通信（必须全员参与），哪一步只在 PP 源 rank 执行。

3. **核对你画的链**：对照 [update_weight_from_distributed.py:L136-L146](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/update_weight/update_weight_from_distributed.py#L136-L146)（`_send_weights` 的非专家/专家两趟）与本讲引用的 `common.py` 三个函数，确认顺序与职责无误。

**预期产出**：一份选型决策（含理由与必需参数）+ 一张标注了「集合通信点 / 仅源 rank 点」的调用链图。运行行为「待本地验证」（需真实集群）。

## 6. 本讲小结

- 权重同步是 slime 闭环里 training→rollout 的**单向桥梁**，触发点在 `train.py` 主循环的 `actor_model.update_weights()`：循环前做一次初始推送，每轮训练+保存后做常规同步（异步循环按 `update_weights_interval` 节流）。
- `update_weight` 是一个**目录模块**而非函数，真正的入口是 `actor.py:145-168` 的选择逻辑：由 `--update-weight-mode`（full/delta）× `--update-weight-transport`（nccl/disk）× `colocate` 三因子选出 `UpdateWeightFromDiskDelta` / `UpdateWeightFromDisk` / `UpdateWeightFromTensor`（IPC） / `UpdateWeightFromDistributed`（NCCL broadcast）之一；默认 `full+nccl`，按 colocate 二分。
- 所有传输类的第一步都是把 Megatron 分片参数**聚合回完整张量**，由 `all_gather_param` 完成：处理 TP/EP 分片、GLU `linear_fc1` 重排、grouped MoE `linear_fc2` 维度修正、`expert_bias` 直通；批量版 `all_gather_params_async` 用三阶段重叠通信。
- `named_params_and_buffers` 用一套**全局命名**（PP 层号 + `layer_offset`、EP 专家号 + `expert_offset`、MTP 单列）统一枚举分布在各 rank 的参数与 `expert_bias` buffer，是聚合与 diff 的清单来源。
- `all_gather_param` 是 NCCL 集合通信，**必须全员 rank 同步参与**（否则死锁），所以迭代器里聚合调用在「非源 rank 提前 continue」之前；只有 PP 源 rank（DP=TP=0）才继续 HF 转换与广播。
- delta 模式被限制为 disk-only 且禁 colocate（IPC 已近零拷贝，delta 是纯开销），release-train 强制 full+disk（进程被销毁，只能落盘）。

## 7. 下一步学习建议

本讲只建立了权重同步的**全景与共享工具**。接下来按依赖顺序：

- **u5-l2 三种权重传输**：分别钻进 `UpdateWeightFromTensor`（NCCL/IPC 混合）、`UpdateWeightFromDisk`（整 ckpt）、`UpdateWeightFromDiskDelta`（增量 + xor/overwrite 编码）的内部实现，以及 `expert_routing` 的 PP=1/EP>1 专家传输优化。本讲的 `all_gather_param` / `named_params_and_buffers` 是它们共同的底座。
- **u5-l3 SGLang 引擎封装**：看权重同步的**接收端**——`SGLangEngine` 如何在 `pause_generation / flush_cache / update_weights_from_tensor|disk|distributed / continue_generation` 之间配合，理解 4.1.3 里那个「pause→flush→update→continue」节奏的接收侧实现。
- **u5-l4 Megatron 权重服务端**：看 slime 如何**反向复用** Megatron 提供 `/generate`（logprob 采样）端点，这是闭环另一条方向的巧妙设计。
- 读完 U5 四讲后，可结合 u8-l1（sglang-config 拓扑）与 u8-l5（fp8/int4 低精度）理解权重同步在异构拓扑与量化场景下的完整形态。
