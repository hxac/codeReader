# u6-l4 Checkpoint 体系：保存、恢复与断点续训

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出一个 OLMo-core 训练 checkpoint 在磁盘上的完整目录布局，以及每一部分是谁写出来的。
2. 跟踪 `Checkpointer.save()` / `Checkpointer.load()` 的完整执行路径，理解「临时目录 + 原子替换」如何保证 checkpoint 要么完整、要么不存在。
3. 解释 `CheckpointerCallback` 的保存决策树：永久 checkpoint、ephemeral checkpoint、`fixed_steps`、`max_checkpoints` 与删除策略如何协作。
4. 理解底层 `olmo_core.distributed.checkpoint` 如何包装 PyTorch DCP（`torch.distributed.checkpoint`），实现「用一种并行拓扑保存、用另一种拓扑加载」的重分片（resharding）能力，以及直传 S3/GCS 的读写器。
5. 掌握 `LoadStrategy` 三个取值（`if_available` / `always` / `never`）的使用场景，弄清一次断点续训到底恢复了哪些状态（模型、优化器、数据加载器、RNG、回调），并动手完成一次「训练 → 中断 → 续训」实验。

本讲是单元六的收官：u6-l1 讲了训练循环，u6-l2 讲了 train module，u6-l3 讲了回调系统。你会发现 checkpoint 恰好是这三者的交汇点——保存动作由回调触发，保存内容由 train module 和 Trainer 共同提供，而底层读写则委托给分布式模块。

## 2. 前置知识

### 2.1 为什么需要专门的 checkpoint 体系

在最简单的单卡 PyTorch 教程里，「保存模型」就是 `torch.save(model.state_dict(), "model.pt")`。但分布式大模型训练下这不够用，原因有三个：

1. **参数是分片的**。FSDP/张量并行下，每个 rank 只持有部分参数（或 `DTensor` 分片）。朴素 `state_dict` 保存的是本地碎片，换一个 world size 就拼不回去。
2. **要恢复的不只是模型**。断点续训（resume）还需要：优化器动量、学习率调度进度、数据加载器读到了哪个 batch、每个 rank 的 RNG 状态、各类回调自己的簿记。缺一样，续出来的训练曲线就和不断裂的曲线对不上。
3. **存储可能很远**。AI2 的训练数据与产物放在 Weka/GCS 上，checkpoint 最好能直接写 `gs://...`、`s3://...`，而不是先落盘再手动上传。

PyTorch 官方对问题 1 的回答是 DCP（`torch.distributed.checkpoint`）：各 rank 协商「谁写哪个张量的哪一段」，把数据写成一组分片文件，外加一份记录「每个张量在哪个文件的哪个字节区间」的元数据。加载时按目标拓扑重新分配字节区间，因此**保存和加载的 world size、分片策略可以不同**。OLMo-core 的 `olmo_core.distributed.checkpoint` 就是在 DCP 之上加了远程存储支持（`RemoteFileSystemWriter/Reader`）和一批工具函数；`olmo_core.train.checkpoint.Checkpointer` 再往上包一层，把「训练 checkpoint」的目录结构和 Trainer 生命周期管起来。

### 2.2 需要回忆的前置概念

- **rank 与 fs_local_rank**（u7-l3 会展开）：rank 是进程在全局通信组里的编号；`fs_local_rank` 是进程在所在节点内的编号。写共享文件系统时只允许 fs_local_rank 0 建目录/做重命名，其余 rank 用 barrier + `wait_for` 等待可见。
- **barrier / broadcast_object**：`olmo_core.distributed.utils` 提供的集体通信封装，未初始化分布式时它们是空操作——这意味着 `Checkpointer` 在**单进程（不起 torchrun）**下也能直接用，这一点会被我们的代码实践利用。
- **回调钩子**（u6-l3）：`CheckpointerCallback` 是默认自动补齐的四个回调之一（priority=1），保存动作全部发生在 `post_train_batch` / `post_train` 钩子里，`Trainer.fit()` 的循环正文一行 checkpoint 代码都没有。
- **TrainerStateDict**（u6-l1 提过指标与簿记）：Trainer 自己的可序列化状态，本讲会展开它的字段。
- **URL 路径**：OLMo-core 里 `gs://bucket/path`、`s3://bucket/path` 与本地路径用同一套 API 处理，内部用 `is_url()` 分流。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/olmo_core/train/checkpoint.py](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/checkpoint.py) | `Checkpointer` / `CheckpointerConfig` / `CheckpointMetadata`：定义训练 checkpoint 的目录结构，实现 save / save_async / load / 目录扫描 / 原子写入 |
| [src/olmo_core/train/callbacks/checkpointer.py](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/callbacks/checkpointer.py) | `CheckpointerCallback`：训练循环里的保存决策者（何时存、永久还是临时、删谁）与 `CheckpointRemovalStrategy` |
| [src/olmo_core/distributed/checkpoint/\_\_init\_\_.py](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/distributed/checkpoint/__init__.py) | 分布式 checkpoint 高层 API：`save_state_dict` / `load_state_dict` / `save_model_and_optim_state` / `unshard_checkpoint` / `load_keys` 等 |
| [src/olmo_core/distributed/checkpoint/filesystem.py](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/distributed/checkpoint/filesystem.py) | `RemoteFileSystemWriter` / `RemoteFileSystemReader`：DCP 存储读写器，支持本地与云存储、多线程写、按字节区间远程读 |
| [src/olmo_core/distributed/checkpoint/utils.py](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/distributed/checkpoint/utils.py) | `prepare_qk_expansion` / `finish_qk_expansion`：加载时共享 QK 增益 → 逐头增益的形状迁移工具 |
| [src/olmo_core/train/common.py](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/common.py) | `LoadStrategy` 枚举（另有 `Duration`、`ReduceType` 等，u6-l1 已讲） |
| [src/olmo_core/train/trainer.py](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/trainer.py) | `Trainer.fit()` 的恢复入口、`TrainerStateDict`、`load_checkpoint` / `save_checkpoint` 系列 |
| [src/test/train/checkpoint_test.py](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/test/train/checkpoint_test.py) | Checkpointer 的单元/分布式测试，是本讲代码实践的主要依据 |

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：

- **4.1 Checkpointer**：一次保存/加载的完整执行路径与磁盘布局。
- **4.2 CheckpointerCallback**：训练循环里的保存决策树。
- **4.3 分布式 checkpoint 底层**：DCP 封装、远程读写器与重分片。
- **4.4 断点续训**：`LoadStrategy`、恢复了什么、RNG 的 world size 陷阱。

### 4.1 模块一：Checkpointer——训练 checkpoint 的目录布局与保存/加载

#### 4.1.1 概念说明

`Checkpointer`（注意：不是 `CheckpointerCallback`）回答的问题是：**「一个训练 checkpoint 由哪些文件组成、怎么写出去、怎么读回来」**。它对上暴露 `save()` / `save_async()` / `load()` 三个主方法，对下把模型和优化器状态委托给 `olmo_core.distributed.checkpoint`。

一个保存到本地目录的 checkpoint 长这样：

```
save_folder/
└── step10/                      # Checkpointer.CHECKPOINT_DIR = "step{step}"
    ├── .metadata.json           # Checkpointer 自己的元数据（ephemeral 标记、版本号），rank0 最后写入
    ├── train/
    │   ├── rank0.pt             # 每个 rank 一份 TrainerStateDict（torch.save）
    │   └── rank1.pt
    └── model_and_optim/         # 模型 + 优化器的 DCP 分布式 checkpoint
        ├── .metadata            # DCP 元数据（pickle）：每个张量在哪个文件的哪个字节区间
        ├── __0_0.distcp         # rank 0 写出的数据分片
        └── __1_0.distcp         # rank 1 写出的数据分片
```

三部分各有作者：`train/rankN.pt` 由每个 rank 各自 `torch.save`；`model_and_optim/` 由 DCP 集体写入；`.metadata.json` 只由 rank 0 写，而且是**最后**写——这是「checkpoint 要么完整、要么不存在」不变式的关键：目录扫描时必须看到 `.metadata.json` 才承认这是一个有效 checkpoint（见 4.1.3 的 `dir_is_checkpoint`），所以保存中途被杀掉的目录永远不会被误当成可恢复点。

#### 4.1.2 核心流程

`Checkpointer.save(dir, train_module, train_state, ephemeral)` 的执行流程（本地目录情形）：

```text
1. torch.cuda.synchronize()            # 确保 GPU 上的计算全部完成
2. _temporary_wd(dir):
   a. _prepare_dir: save_overwrite 则清空目录，否则目录非空即抛 FileExistsError；barrier()
   b. _get_tmp_dir: 创建同级临时目录 "step10-tmp"（fs_local_rank 0 建，其余 rank 等待可见）
3. 在临时目录内：
   a. _save_train_state: 每个 rank 写 train/rank{N}.pt
   b. train_module 有 save_state_dict_direct → 直接委托（如 OLMoDDPTrainModule）；
      否则 save_state_dict(model_and_optim/, train_module.state_dict_to_save(), ...)
4. _teardown_tmp_dir: barrier() → fs_local_rank 0 把 "step10-tmp" 原子 rename 成 "step10"
                      → 所有 rank 等待最终目录可见
5. rank0 写 .metadata.json（ephemeral 标记在这里）
```

保存到 URL（如 `gs://...`）时流程相同，区别是：每个 rank 各有一个本地临时目录，第 4 步改为把临时目录里的文件逐个 `upload()` 到远端。

`Checkpointer.load(dir, train_module)` 的流程：

```text
1. 读 trainer state：先试 train/rank{本rank}.pt，失败回落 train/rank0.pt
   （回落是为换 world size 续训准备的）
2. rank0 读 model_and_optim/ 的 DCP 元数据；若不存在且没有 trainer state，
   则把 dir 本身当作「纯权重 checkpoint」
3. train_module 有 load_state_dict_direct → 直接委托；
   否则：state_dict = train_module.state_dict_to_load(metadata)
         load_state_dict(model_and_optim/, state_dict)   # DCP 按元数据填充
         train_module.load_state_dict(state_dict)        # 写回模型/优化器
4. 返回 trainer_state（可能为 None），交给 Trainer.load_state_dict
```

#### 4.1.3 源码精读

先看类的骨架和三个 ClassVar。[checkpoint.py:L78-L100](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/checkpoint.py#L78-L100) 定义了 `Checkpointer`：`METADATA_FNAME = ".metadata.json"` 是有效性标记，`CHECKPOINT_DIR = "step{step}"` 是目录命名模板，`FS_TIMEOUT = 120.0` 是等待慢速文件系统（NFS）可见性的超时。`__post_init__` 里 fs_local_rank 0 负责创建 `work_dir`。

`save()` 的核心分支在 [checkpoint.py:L102-L145](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/checkpoint.py#L102-L145)：整个写入发生在 `with self._temporary_wd(dir) as wd:` 上下文里；模型/优化器有两种保存路线——

```python
if hasattr(train_module, "save_state_dict_direct"):
    # OLMoDDPTrainModule 等自己管理分布式 checkpoint 的读写
    train_module.save_state_dict_direct(train_module_dir, ...)
else:
    save_state_dict(train_module_dir, train_module.state_dict_to_save(), ...)
```

这是一个鸭子类型（duck-typing）逃生口：某些 train module 的状态无法表示成扁平 state dict，就自己实现直接读写。方法存在性检查而非 isinstance，保持了对任意 `TrainModule` 实现的开放性。`with` 块结束后，[checkpoint.py:L145](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/checkpoint.py#L145) 才写元数据：`self._save_metadata(dir, CheckpointMetadata(ephemeral=ephemeral))`。

临时目录的原子替换逻辑在 [checkpoint.py:L541-L551](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/checkpoint.py#L541-L551)（`_temporary_wd` 上下文管理器），真正的重命名/上传在 [checkpoint.py:L497-L539](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/checkpoint.py#L497-L539)：本地路径时先 `barrier()` 再由 fs_local_rank 0 执行 `tmp_dir.replace(str(dir))`——`Path.replace` 是原子的 rename 系统调用，观察者要么看到旧的 `step10-tmp`，要么看到完整的 `step10`；URL 路径时逐文件 `upload()` 后清掉临时目录。注释里专门解释了为什么两处都要 barrier：防止某个 rank 抢跑写入后又被别人的 `clear_directory` 清掉。

每个 rank 各写一份训练状态的地方是 [checkpoint.py:L421-L431](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/checkpoint.py#L421-L431)：`torch.save(train_state, train_dir / f"rank{get_rank()}.pt")`——按 rank 分文件是因为 RNG 状态、数据加载器游标本就该每个进程一份。

`load()` 里最有意思的是 trainer state 的两级探测与元数据回退，见 [checkpoint.py:L220-L260](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/checkpoint.py#L220-L260)：先试本 rank 的文件、再回落 rank0（注释说明这发生在「用不同 world size 恢复」时）；随后 rank 0 读 `model_and_optim` 的 DCP 元数据，若 `model_and_optim/.metadata` 不存在且没有 trainer state，就把 `dir` 本身当作 checkpoint 目录——这允许加载一个「只有模型权重（可能带优化器状态）、不是训练快照」的目录，正是从 HF 转换来的权重或 midtrain 产物进入训练的入口。加载的另一半在 [checkpoint.py:L262-L284](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/checkpoint.py#L262-L284)，同样先探测 `load_state_dict_direct` 再走扁平 state dict 路线。

目录扫描三件套：

- [checkpoint.py:L338-L354](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/checkpoint.py#L338-L354) `dir_is_checkpoint`：目录下只要有 `.metadata`（纯 DCP 目录），**或者**同时存在 `train/rank0.pt`、`model_and_optim/.metadata`、`.metadata.json` 三样，才算训练 checkpoint。
- [checkpoint.py:L356-L385](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/checkpoint.py#L356-L385) `find_checkpoints`：用正则 `^step(\d+)$` 匹配目录名，可按 `.metadata.json` 里的 ephemeral 标记过滤。
- [checkpoint.py:L401-L419](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/checkpoint.py#L401-L419) `latest_checkpoint`：取 step 数字最大的那个，找不到抛 `FileNotFoundError`——这是续训时定位恢复点的依据。

`save_async()`（[checkpoint.py:L147-L197](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/checkpoint.py#L147-L197)）与同步版结构一致，区别是把模型/优化器的写盘换成 `async_save_state_dict(...)` 返回的 `Future`，并把 `.metadata.json` 的写入挂到 `future.add_done_callback`——元数据仍然最后落地，原子性不变式在异步路径下依然成立。两个前置校验值得注意：分布式下必须有独立的 checkpointer 进程组（后面 4.2 会看到它由回调创建），实现了 `save_state_dict_direct` 的 train module 不支持异步保存。

#### 4.1.4 代码实践

**实践目标**：亲眼看到 checkpoint 的目录布局，并验证一次 save → load 往返。

**操作步骤**：

1. 确认环境可用（u1-l2 的开发安装）。
2. 运行 Checkpointer 的本地测试（CPU 即可，它会用 gloo 后端起 2 个进程）：

   ```bash
   pytest -v src/test/train/checkpoint_test.py -k "local and not async"
   ```

3. 阅读被测函数 [checkpoint_test.py:L16-L41](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/test/train/checkpoint_test.py#L16-L41)：它构建 `Checkpointer(work_dir=...)` + `BasicTrainModule`（tiny 模型 + AdamW），保存到 `step10` 目录，然后断言 `train/rank0.pt`、`train/rank1.pt` 存在、`model_and_optim` 非空、`find_checkpoints` 返回 `[(10, dir)]`、`latest_checkpoint` 等于该目录，最后 `load()` 回来验证 `train_state["rank"]` 与本 rank 一致。
4. 想直接观察目录，可在 `run_checkpointer` 的 `barrier()` 之后加一行临时代码 `print(list(Path(dir).rglob("*")))`（自己 clone 的仓库里改，别改上游），或者用下面的独立脚本（**示例代码**，单进程即可运行，因为 `barrier`/`broadcast_object` 未初始化分布式时是空操作）：

   ```python
   # save_load_probe.py（示例代码，非项目自带）
   import os, torch
   os.environ["OLMO_SHARED_FS"] = "1"   # 单进程下本地目录即"共享文件系统"
   from olmo_core.train.checkpoint import Checkpointer
   from olmo_core.train.train_module import BasicTrainModule

   model = torch.nn.Sequential(torch.nn.Linear(8, 8), torch.nn.Linear(8, 8))
   optim = torch.optim.AdamW(model.parameters())
   tm = BasicTrainModule(model, optim, 128)          # (model, optim, rank_microbatch_size)
   ckptr = Checkpointer(work_dir="/tmp/ck-work")
   ckpt = "/tmp/ck-save/step10"
   ckptr.save(ckpt, tm, {"global_step": 10})
   print("is checkpoint:", ckptr.dir_is_checkpoint(ckpt))
   print("files:", sorted(str(p) for p in __import__("pathlib").Path(ckpt).rglob("*")))
   state = ckptr.load(ckpt, tm)
   print("trainer state:", state)
   ```

**需要观察的现象**：`files` 列表里有 `.metadata.json`、`train/rank0.pt`、`model_and_optim/.metadata` 和至少一个 `*.distcp` 文件；保存过程中如果盯住文件系统，会短暂看到 `step10-tmp` 目录。

**预期结果**：`is checkpoint: True`，`trainer state: {'global_step': 10}`；pytest 用例全绿。本环境未安装 torch，以上命令未实际运行，**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `dir_is_checkpoint` 要求 `.metadata.json` 存在，而保存流程把它放在最后写？

**答案**：`.metadata.json` 由 rank 0 在所有数据落地之后写，相当于「提交点」。若保存中途进程被杀，目录里可能已有部分 `train/*.pt` 或 `*.distcp`，但没有 `.metadata.json`，`find_checkpoints` / `latest_checkpoint` 就不会把它当成有效恢复点，续训自然不会加载一个残缺快照。删除 checkpoint 时也遵循同一逻辑——先删 `.metadata.json` 使其失效（见 4.2）。

**练习 2**：如果用 4 卡保存、8 卡恢复，`load()` 读取 trainer state 时会发生什么？

**答案**：先尝试 `train/rank{当前rank}.pt`。8 卡下 rank 4–7 在 4 卡 checkpoint 里没有对应文件，`torch.load` 抛 `FileNotFoundError` 后回落到 `train/rank0.pt`（[checkpoint.py:L225-L230](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/checkpoint.py#L225-L230)）。模型/优化器部分则由 DCP 元数据重分片，天然与 world size 无关。代价是 RNG 状态不恢复——4.4 会看到 Trainer 里的 world size 判断。

**练习 3**：`save()` 里 `torch.cuda.synchronize()` 的作用是什么？

**答案**：GPU 计算是异步的，`state_dict_to_save()` 拿到的张量可能还有未完成的 kernel。先把当前设备同步住，保证后续 CPU 侧序列化（`_write_items` 里 `data.cpu()`）读到的是完整、最新的数值，避免保存出「半新半旧」的权重。

### 4.2 模块二：CheckpointerCallback——训练循环里的保存决策者

#### 4.2.1 概念说明

`Checkpointer` 只知道「怎么存」，什么时候存、存哪种、存几份、删谁，由 `CheckpointerCallback` 决定。回忆 u6-l3：它是 Trainer 未显式配置时**自动补齐**的默认回调（priority=1），所有逻辑挂在三个钩子上：`pre_train`（初始化进程组、存 step0 快照、收集上次运行遗留的 ephemeral checkpoint）、`post_train_batch`（保存决策树）、`post_train`（收尾终存）。

它管理的 checkpoint 分两类：

- **永久 checkpoint（permanent）**：按 `save_interval`（默认 250 步）或在 `fixed_steps` 指定的步数保存，受 `max_checkpoints`（默认 3）限制数量，超限删最旧的。
- **ephemeral checkpoint（临时）**：按 `ephemeral_save_interval` 更频繁地保存，随时只保留最新一份，专为可抢占作业（preemptible job）设计——被抢占了也能从最近几分钟内恢复，而不是从上个永久点重来。

两者的唯一磁盘区别是 `.metadata.json` 里的 `ephemeral` 标记（`CheckpointMetadata`，[checkpoint.py:L72-L75](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/checkpoint.py#L72-L75)）。

#### 4.2.2 核心流程

`post_train_batch`（每个训练步末尾）的决策树：

```text
1. 非阻塞地检查上一个异步保存是否完成；完成后才执行已排队的删除
2. step ∈ fixed_steps            → 存永久 checkpoint，加入 _checkpoints，trim 到 max_checkpoints
3. else if save_interval 整除 step → 存永久 checkpoint，同上
4. else if ephemeral_save_interval 整除 step
        且未被 block_ephemeral_checkpoints
        且距上次任意保存 ≥ ephemeral_cooldown（若设置）
     → 存 ephemeral checkpoint；_ephemeral_checkpoints 超过 1 份时调度删除旧的
```

`post_train`（训练正常结束）：若当前步晚于最近一次保存（`step > _latest_checkpoint_step`），用**同步**方式补存一份终态 checkpoint（保证 `fit()` 返回时一切落盘），然后阻塞等待所有异步操作完成。

`pre_train` 的三件事：① 若 `save_async is None` 且存在 CPU 后端则默认开异步，并为异步 checkpointing 创建一个 30 分钟超时的独立进程组；② 从头训练时（step==0 且未从 checkpoint 恢复）保存 step0 快照；③ 扫描 save_folder，把上次运行遗留的 ephemeral checkpoint 收进待删列表。

#### 4.2.3 源码精读

字段定义在 [checkpointer.py:L49-L139](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/callbacks/checkpointer.py#L49-L139)：`save_interval=250`、`ephemeral_save_interval=None`、`save_async=None`（默认自动）、`remove=ephemeral_only`、`fixed_steps=None`、`max_checkpoints=3`。`__post_init__`（[L125-L139](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/callbacks/checkpointer.py#L125-L139)）校验了三条约束：interval 至少为 1、ephemeral 必须小于永久 interval、max_checkpoints 至少为 1。

决策树本体在 [checkpointer.py:L282-L315](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/callbacks/checkpointer.py#L282-L315)，其中永久分支的两个动作是：

```python
self._checkpoints.append(self._save_checkpoint())
self._trim_checkpoints()          # 超过 max_checkpoints 时把最旧的调度删除
```

ephemeral 分支则是 `while len(self._ephemeral_checkpoints) > 1: 调度删除最旧的`——即**只在存入新 ephemeral 时**淘汰上一份，保证磁盘上任意时刻最多一份（外加已调度未执行的删除）。`ephemeral_cooldown` 是可选的节流阀：距上次保存太近就跳过本次 ephemeral。

删除的实现在 [checkpointer.py:L179-L209](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/callbacks/checkpointer.py#L179-L209)，第一句就是关键注释「Remove metadata file first to invalidate the checkpoint」：rank 0 先删 `.metadata.json`，再做（可能很慢的）`clear_directory`——即使清目录中途被打断，这个目录也已经不是合法 checkpoint 了，与保存侧的原子性策略对称。注意删除走的是 `self.trainer.run_bookkeeping_op(...)`，即挂到 u6-l1 讲过的异步簿记队列里执行，避免阻塞训练；对 GCS 这类慢存储还给了 180 秒软超时。

`pre_train` 里为异步保存建进程组的代码在 [checkpointer.py:L224-L232](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/callbacks/checkpointer.py#L224-L232)：

```python
if is_distributed() and self.save_async and self.checkpointer.process_group is None:
    ...
    self.checkpointer.process_group = dist.new_group(timeout=timedelta(minutes=30))
```

为什么需要独立进程组？异步保存把写盘挪到后台线程，其中的集合通信不能和训练主循环用的 NCCL 组互相踩，所以用 CPU 后端（gloo）另建一组。收集遗留 ephemeral checkpoint 的逻辑在 [checkpointer.py:L241-L280](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/callbacks/checkpointer.py#L241-L280)：只有 rank 0 扫描（避免打爆远端存储），结果 `broadcast_object` 给所有 rank；判定「遗留」的条件是不等于 step0、不晚于当前步、不在 fixed_steps、不是 save_interval 的倍数，且属于 `ephemeral_only`（ephemeral interval 的倍数）或 `all_non_permanent` 策略覆盖的范围。

异步保存的等待逻辑在 [checkpointer.py:L150-L177](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/callbacks/checkpointer.py#L150-L177)：`_await_last_checkpoint(blocking=False)` 在每步只检查 Future 是否已完成（完成才收割并等 rank 0 确认目录有效），`_save_checkpoint` 开头则强制阻塞等待上一次保存——绝不会有两个写盘任务并发进行。`post_train` 的同步终存在 [checkpointer.py:L317-L325](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/callbacks/checkpointer.py#L317-L325)：`self._save_checkpoint(save_async=False)` 显式绕过异步。

`CheckpointRemovalStrategy` 的三个取值见 [checkpointer.py:L27-L46](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/callbacks/checkpointer.py#L27-L46)：`ephemeral_only`（只删 ephemeral interval 产出的）、`all_non_permanent`（连不在 save_interval/fixed_steps 上的都删）、`never`（一个不删，磁盘自己扛）。

#### 4.2.4 代码实践

**实践目标**：不经运行、纯靠源码推演出「哪些 step 会留下 checkpoint」。

**操作步骤**：设 `save_interval=250`，`ephemeral_save_interval=50`，`fixed_steps=[1000]`，`max_checkpoints=3`，训练到 step 1200 结束（`Duration.steps(1200)`）。

1. 逐行阅读 [checkpointer.py:L282-L325](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/callbacks/checkpointer.py#L282-L325)，对 step = 1..1200 手工执行决策树。
2. 画出时间轴：每行一个相关 step，标注「永久 / ephemeral / 触发删除谁 / trim 掉谁」。

**需要观察的现象**：注意三个细节——step 1000 同时是 `fixed_steps` 成员和 `save_interval` 倍数，但 `if/elif` 链只会走第一个分支（存一次）；ephemeral 只在新 ephemeral 保存时淘汰上一份；`post_train` 会在 1200 补一份。

**预期结果**（**待本地验证**，可先自行推演再对照）：

- step 0：pre-train checkpoint（`pre_train_checkpoint` 默认为真且非续训），进 `_checkpoints`。
- 永久序列：0, 250, 500, 750, 1000（fixed，与 4×250 重合）→ 存入 1000 后 `_checkpoints` 长度 5，trim 到 3，删 0 和 250 → 1200 终存 → trim → 最终保留 {750, 1000, 1200}。
- ephemeral 序列：50, 100, ..., 1150，任意时刻磁盘上至多两份（新的 + 已调度删除的旧的）；最后一个 1150 会留到下次运行 `pre_train` 时被收进待删列表。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `post_train` 的终存要用 `save_async=False`？

**答案**：`post_train` 之后 Trainer 就要 `fit()` 返回、进程可能退出。异步保存的 Future 依赖后台线程完成，若此时退出可能丢 checkpoint；用同步保存保证 `fit()` 返回时最后一个 checkpoint 已完整落盘（随后 `_await_last_checkpoint()` 也会阻塞等待此前排队的异步任务）。

**练习 2**：`ephemeral_save_interval` 为什么必须小于 `save_interval`（`__post_init__` 强制校验）？

**答案**：ephemeral 的意义是「在两个永久点之间提供更密的恢复点」。若它 ≥ save_interval，它的每个保存点都会被永久 interval 的 `elif` 分支先截住（或相反产生冗余保存），机制退化且容易误删；校验保证了决策树两个分支的语义互斥、清晰。

**练习 3**：删除 checkpoint 时为什么先删 `.metadata.json` 再 `clear_directory`，而且只让 rank 0 / fs_local_rank 0 动手？

**答案**：先删标记文件让目录立刻「失效」——即使 `clear_directory` 在远端存储上耗时很久或中途失败，`find_checkpoints` 也不会再把它当恢复点（保存侧不变式的镜像）。只让一个 rank 动手是为了避免多进程同时删同一目录造成的竞态与重复请求；慢存储场景下删除被包进 `run_bookkeeping_op` 异步簿记，不阻塞训练步。

### 4.3 模块三：分布式 checkpoint 底层——DCP 封装、远程读写器与重分片

#### 4.3.1 概念说明

[olmo_core.distributed.checkpoint](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/distributed/checkpoint/__init__.py#L1-L24) 的模块文档字符串把两个卖点写在最前面：**换拓扑无损互载**（FSDP/FSDP2 下用不同 world size 或分片策略保存/加载）与**直连对象存储**（S3/GCS，加载时每个 rank 只下载自己需要的那部分字节）。它由两层组成：

- **函数层**：`save_state_dict` / `async_save_state_dict` / `load_state_dict`（任意 state dict 的一对一封装，`Checkpointer` 用的就是它们）；`save_model_and_optim_state` / `load_model_and_optim_state`（直接吃 model + optimizer 的便捷版）；`unshard_checkpoint` / `load_keys` / `load_unsharded_model_state`（把分片 checkpoint 还原成普通 `torch.load` 能读的文件）。
- **存储层**：`RemoteFileSystemWriter` / `RemoteFileSystemReader`（filesystem.py），实现 DCP 的 `StorageWriter` / `StorageReader` 接口，本地与 URL 统一处理。

重分片为什么可行？因为 DCP 的 `.metadata` 记录的是**全局张量视图**：每个 key 的完整形状，以及它的每个分片位于哪个文件的哪个 `(offset, length)` 字节区间。加载时目标 state dict 来自**当前**模型（`train_module.state_dict_to_load(metadata)`），DCP 只需按当前每个 rank 持有的目标张量，去元数据里查「这些字节在谁那里」，各自读取后填进目标（可能是 `DTensor`）——保存时的分片方式完全不影响。

#### 4.3.2 核心流程

`save_state_dict` 的流程：

```text
1. _prepare_env_for_save: 清空/检查目录 + barrier + 建目录（Checkpointer 场景下因 _skip_prepare=True 被跳过）
2. 构造 DefaultSavePlanner(dedup_save_to_lowest_rank=True, enable_plan_caching=...)
3. dist_cp.state_dict_saver.save(state_dict, storage_writer=RemoteFileSystemWriter(dir, ...), ...)
   内部：各 rank 规划要写的 items → 协调器汇总去重 → 各自写文件 → finish() 汇总成 .metadata
```

`RemoteFileSystemWriter.write_data` 的并行策略：把本 rank 的待写项按字节大小降序贪心装进 `thread_count` 个桶（`_split_by_size_and_type`），每桶写一个 `__{rank前缀}_{序号}.distcp` 文件，用线程池（或进程池）并发执行；每个文件先写临时文件、`fdatasync`、再 rename/上传。协调器最后把所有 `WriteResult`（key → 文件/偏移/长度）pickle 成 `.metadata`。

`RemoteFileSystemReader` 的读取策略：读 `.metadata` 后，对计划中的每个读取项只取 `get_bytes_range(文件, offset, length)` 指定区间——远程场景下这就是 HTTP Range 请求，天然「按需下载分片」；`pre_download=True` 时则先把涉及文件整体缓存到本地 `work_dir` 再读。

#### 4.3.3 源码精读

函数层入口 [distributed/checkpoint/\_\_init\_\_.py:L72-L119](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/distributed/checkpoint/__init__.py#L72-L119) 是 `save_state_dict`：一行构造 planner，一行调用 `dist_cp.state_dict_saver.save`，`dedup_save_to_lowest_rank=True` 表示当多个 rank 持有同一份数据（如 Replicate 布局的张量）时只让编号最低的 rank 写，节省存储。异步版 [L122-L156](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/distributed/checkpoint/__init__.py#L122-L156) 换成 `dist_cp.state_dict_saver.async_save`——文档字符串说明了它的两段式：「先把 state dict 转存（de-stage）到 CPU，再在独立线程里写盘」，因此主训练循环几乎不被阻塞。加载版 [L159-L186](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/distributed/checkpoint/__init__.py#L159-L186) 就是 `RemoteFileSystemReader` + `dist_cp.state_dict_loader.load` 的组合。

模型+优化器便捷版的准备工作在 [L732-L751](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/distributed/checkpoint/__init__.py#L732-L751)：`_prepare_state_dict` 用 `dist_cp_sd.get_model_state_dict(model, options=cpu_offload=True)` 等函数拿到「规范化的扁平视图」——这层 API 自动处理 FSDP 的 unshard、`DTensor` 的全量视图、优化器状态的参数名化，是「免设置 state_dict_type」提示（[L213-L217](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/distributed/checkpoint/__init__.py#L213-L217)）的出处。`load_model_and_optim_state`（[L302-L394](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/distributed/checkpoint/__init__.py#L302-L394)）还支持 `key_mapping`（当前参数名 ↔ checkpoint 参数名映射，经 `swap_param_keys` 前后换名），其 warning（[L331-L342](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/distributed/checkpoint/__init__.py#L331-L342)）值得读：DCP 只加载「目标里已有的 key」，checkpoint 独有的 key 会被静默忽略——改了 param group 结构再加载时要小心。

写侧的分桶与落盘在 [filesystem.py:L317-L394](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/distributed/checkpoint/filesystem.py#L317-L394)（`write_data`）：桶数由 `thread_count` 决定，文件名由 `gen_file_name()` 生成 `__{rank前缀}_{序号}.distcp`；`throttle_uploads` 打开时改走 `do_n_at_a_time`，每节点只允许 `max(num_nodes//4, 1)` 个 rank 同时上传，防止把对象存储打挂。单文件的写入工具 [filesystem.py:L119-L209](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/distributed/checkpoint/filesystem.py#L119-L209)（`_write_items`）里有一个防御性检查：若张量实际写出字节与预期差超过 1MB，抛 `OLMoCheckpointError`——这通常意味着数据是共享更大 storage 的视图，序列化出了多余字节，宁可在保存时就失败也不留隐患。协调器汇总元数据并写 `.metadata` 的地方是 [filesystem.py:L396-L431](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/distributed/checkpoint/filesystem.py#L396-L431)（`finish`），同样走临时文件 + `fdatasync` + rename/上传。

读侧 [filesystem.py:L453-L587](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/distributed/checkpoint/filesystem.py#L453-L587) 是 `RemoteFileSystemReader`：`_get_bytes`（[L478-L483](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/distributed/checkpoint/filesystem.py#L478-L483)）按 `(relative_path, offset, length)` 取字节，`pre_download` 时先经 `resource_path(..., local_cache=work_dir)` 缓存整文件；`read_data`（[L502-L545](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/distributed/checkpoint/filesystem.py#L502-L545)）多线程拉取区间、`torch.load(weights_only=False)`（注释：torchao float8 需要）、按 `read_item` 的偏移 `narrow` 出目标分片、`copy_` 进（可能是 DTensor 的）目标张量。`read_metadata`（[L547-L567](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/distributed/checkpoint/filesystem.py#L547-L567)）找不到 `.metadata` 时会给出友好报错并提示「是不是想读 `model_and_optim` 子目录」。

训练之外的消费工具：

- `unshard_checkpoint`（[\_\_init\_\_.py:L466-L620](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/distributed/checkpoint/__init__.py#L466-L620)）把分片 checkpoint 转成 `torch.load` / safetensors 可读的整文件，三种策略 `one_file` / `one_file_per_tensor` / `chunks`（按字节上限分卷）见 [L397-L463](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/distributed/checkpoint/__init__.py#L397-L463)；只能在非分布式上下文调用。
- `load_keys`（[L623-L655](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/distributed/checkpoint/__init__.py#L623-L655)）只取指定 key（同样非分布式）；`load_unsharded_model_state`（[L658-L684](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/distributed/checkpoint/__init__.py#L658-L684)）单 rank 即可拉全量模型权重到 CPU。

最后是 [distributed/checkpoint/utils.py](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/distributed/checkpoint/utils.py) 里的一对形状迁移工具：当模型从「QK 增益全层共享（形状 `(d_model,)`）」升级为「每头独立增益（`(n_heads, head_dim)`）」后，旧 checkpoint 无法直接加载。`prepare_qk_expansion`（[utils.py:L13-L57](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/distributed/checkpoint/utils.py#L13-L57)）严格校验形状差异只出现在 `.q_norm.weight` / `.k_norm.weight` 上，把目标替换成一个小的「装载位」张量骗过 DCP 的形状检查；DCP 加载完成后 `finish_qk_expansion`（[utils.py:L60-L72](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/distributed/checkpoint/utils.py#L60-L72)）把共享向量 `repeat` 到每头再 `distribute_tensor` 回目标布局。文档字符串强调「严格、显式开启、绝不修改源 checkpoint」。

#### 4.3.4 代码实践

**实践目标**：像下游消费者（如转换脚本）一样读取一个 DCP checkpoint 的元数据与权重。

**操作步骤**：在 4.1.4 产出的 `/tmp/ck-save/step10` 基础上（**示例代码**，须在非分布式上下文运行）：

```python
# dcp_probe.py（示例代码）
from olmo_core.distributed.checkpoint import (
    get_checkpoint_metadata, load_unsharded_model_state,
)

meta = get_checkpoint_metadata("/tmp/ck-save/step10/model_and_optim")
print("keys:", len(meta.state_dict_metadata))
for k in list(meta.state_dict_metadata)[:8]:
    print(" ", k)

model_sd = load_unsharded_model_state("/tmp/ck-save/step10")
for k, v in model_sd.items():
    print(k, tuple(v.shape))
```

再故意把目录指错做一次：

```python
get_checkpoint_metadata("/tmp/ck-save/step10")   # 指向训练 checkpoint 根目录
```

**需要观察的现象**：第一段打印出 key 数量与名字（`model.0.weight` 之类，取决于 BasicTrainModule 保存的视图）；第二段打印出每个张量的**完整**形状——尽管磁盘上是分片存储的；第三段抛 `FileNotFoundError`，报错信息里提示「是不是想用 `.../model_and_optim`」。

**预期结果**：如上三条全部出现即通过。`load_unsharded_model_state` 的对象可能因 BasicTrainModule 的保存选项而与 `model.state_dict()` 的 key 有前缀差异，属正常现象。**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：`dedup_save_to_lowest_rank=True` 解决什么问题？不开启会怎样？

**答案**：数据并行 + Replicate 布局下，多个 rank 的目标 state dict 里会出现同一个（全量）张量。不开启去重时每个 rank 各写一份，checkpoint 体积随 world size 线性膨胀；开启后只有编号最低的 rank 写、其余跳过，元数据里该张量只登记一份位置。

**练习 2**：为什么 `_write_items` 里张量先 `data.cpu()` 再 `torch.save` 到临时文件，最后才 rename 到正式文件名？

**答案**：`data.cpu()` 把 GPU 张量同步拷到主机内存（注释标明这是阻塞拷贝、含传输完成），避免序列化线程触碰 GPU 上下文；写临时文件 + `fdatasync` + rename 保证读者永远看不到半截文件——与 4.1 里 `step10-tmp` → `step10` 的目录级原子性是同一思想在文件级的实现。

**练习 3**：`load_unsharded_model_state` 与 `unshard_checkpoint` 有何差别？

**答案**：`unshard_checkpoint` 把结果写成磁盘文件（`torch.load` / safetensors 可读），供转换、发布等离线用途，且要求非分布式上下文；`load_unsharded_model_state` 直接返回 CPU 内存里的 state dict，允许分布式作业中的单个 rank 调用（文档明确它「不发集体通信」），适合在线地取全量权重。

### 4.4 模块四：断点续训——LoadStrategy、恢复了什么、RNG 陷阱

#### 4.4.1 概念说明

前面解决了「怎么存」，最后一块拼图是「训练开始时要不要加载、从哪加载、加载什么」。三个问题分别由 `TrainerConfig` 的 `load_strategy`、`load_path`（或 `save_folder` 自身）、`load_trainer_state` / `load_optim_state` 回答。

`LoadStrategy` 只有三个取值（[common.py:L108-L128](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/common.py#L108-L128)）：

| 取值 | 行为 |
| --- | --- |
| `if_available`（默认） | 先找 save_folder、再找 load_path，找到就恢复，找不到就从头训 |
| `always` | 同上，但找不到直接抛 `FileNotFoundError`——用于「这次运行必须是在续训」的强断言 |
| `never` | 即使 save_folder 里有 checkpoint 也不加载——从头训或纯评测 |

> 注意：本仓库的 `LoadStrategy` **只有这三个值**。如果你在学习其他训练库（如原版 OLMo）时见过更多策略名，那些并不存在于 OLMo-core；「改配置续训」在这里的做法是：恢复点由 save_folder/load_path 决定，策略用上面三选一，其余想改的东西（如 `hard_stop`、interval、日志配置）通过命令行 dotlist 覆盖（u2-l3）。

一个重要区分：**save_folder 与 load_path 的语义不同**。save_folder 命中时是「续训」，强制加载 trainer state（数据进度、step 计数）和优化器状态；load_path 是「从别处初始化」（典型：从预训练权重继续微调），默认**不**加载 trainer state——例子 [examples/llm/train.py:L129-L137](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/examples/llm/train.py#L129-L137) 里 `load_trainer_state` 默认 False，注释写着「只有续训同一数据集时才有意义」。

#### 4.4.2 核心流程

`Trainer.fit()` 开头的恢复逻辑（[trainer.py:L696-L728](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/trainer.py#L696-L728)）：

```text
若 no_checkpoints=False 且未加载过 且 strategy ≠ never:
    maybe_load_checkpoint(save_folder, load_trainer_state=True, load_optim_state=True)   # 续训语义
    若设了 load_path 且还没加载: maybe_load_checkpoint(load_path)                        # 初始化语义
    仍未加载:
        strategy == always → FileNotFoundError
        否则 → 警告 "will train from scratch..."
```

`maybe_load_checkpoint`（[trainer.py:L1130-L1162](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/trainer.py#L1130-L1162)）= rank 0 用 `contains_checkpoint` 探测 → `broadcast_object` 结论 → 有则调 `load_checkpoint`。`load_checkpoint`（[trainer.py:L1049-L1128](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/trainer.py#L1049-L1128)）先由 rank 0 判断给定目录是不是 checkpoint，不是就扫 `latest_checkpoint` 并把结果广播（避免每个进程都去打远端存储），然后 `checkpointer.load(...)` 拿回 trainer state 交给 `load_state_dict`，最后依次触发 `callback.post_checkpoint_loaded(dir)` 并置 `checkpoint_loaded=True`。

恢复的内容由 `TrainerStateDict`（[trainer.py:L100-L109](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/trainer.py#L100-L109)）刻画：`global_step`、`global_train_tokens_seen`、`global_train_petaflops`、`max_steps`、`data_loader` 状态、`epoch`、保存时的 `world_size`、`rng`、每个回调自己的 state。

`load_state_dict`（[trainer.py:L1002-L1047](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/trainer.py#L1002-L1047)）逐项恢复，并打印一行著名日志 `Will resume training from step {N}, epoch {E}`——续训是否生效，看它就知道。两个细节：

1. **RNG 只在 world size 一致时恢复**（[L1037-L1047](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/trainer.py#L1037-L1047)）：保存的是每个 rank 各自的 RNG 状态，换 world size 后 rank↔状态的对应关系已无意义，强行恢复反而引入不对称，所以直接放弃并告警。这也解释了 4.1 练习 2 里「回落 rank0 的 trainer state」拿到的 RNG 是不会被用的。
2. **`max_steps` 存而不取**：`load_state_dict` 恢复了 step、token、epoch、回调，却没有回填 `max_steps`——学习率调度曲线的总长（u5-l2 的 horizon）由**本次运行**的配置决定。这正是官方多阶段配方（预训练→midtrain→anneal 换调度器重设 lr）能工作的前提之一：checkpoint 不会把旧的训练总长强加给新阶段。

保存侧的 Trainer 入口是 `save_checkpoint` / `save_checkpoint_async`（[trainer.py:L1164-L1242](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/trainer.py#L1164-L1242)）：路径为 `save_folder/step{global_step}`；保存前先 `_log_metrics()` + `_join_bookkeeping_ops()`——因为某些回调的状态（如日志计数）依赖指标先落账；两处都 `record_metric("checkpoint/save_duration_s", ..., reduce_type=ReduceType.max)`，异步版的度量在 Future 完成回调里记。ConsoleLoggerCallback 的默认指标白名单里就有 `checkpoint/*`（[console_logger.py:L49](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/callbacks/console_logger.py#L49)），所以这些耗时会自然出现在控制台。

还有一条「只加载不训练」的路径：`Trainer.eval_checkpoints()`（[trainer.py:L784-L891](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/trainer.py#L784-L891)）逐个加载 `checkpoints_to_eval` 里的 checkpoint 跑评测，加载时显式 `load_optim_state=False`（[L848-L851](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/trainer.py#L848-L851)），因为评测只要权重。

最后补一个优化器相关的细粒度开关：`load_checkpoint(..., reset_optimizer_states_on_load=...)` 只对实现 `load_state_dict_direct` 的 train module 生效；留 `None` 时，`Checkpointer.load` 仅在**确认是真实续训**（checkpoint 里有 trainer state）的前提下取 train module 的 `reset_optimizer_states_on_resume` 默认值——这个「只有真续训才重置动量」的语义由测试 [checkpoint_test.py:L160-L187](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/test/train/checkpoint_test.py#L160-L187) 用一个鸭子类型替身锁定：有无 `train/rank0.pt` 决定传入 direct API 的 reset 值是 `True` 还是 `None`。

#### 4.4.3 源码精读（补充：配置如何拼装）

`TrainerConfig` 的相关字段在 [train/config.py:L34-L41](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/config.py#L34-L41)：`save_folder`（必填）、`load_path`、`load_strategy`、`load_optim_state` / `load_trainer_state`、`checkpointer: CheckpointerConfig`。构建时（[train/config.py:L187-L193](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/config.py#L187-L193)）有两处默认值下推：`checkpointer.save_overwrite` 未设则继承 `TrainerConfig.save_overwrite`；`checkpointer.work_dir` 未设则取 save_folder（本地路径时）。`CheckpointerConfig` 本身（[checkpoint.py:L50-L69](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/checkpoint.py#L50-L69)）只有 `save_overwrite`、`pre_download`、线程数、`throttle_uploads` 等工程旋钮——又一次体现「策略在上层（TrainerConfig/回调），机制在底层（Checkpointer/DCP）」的分层。

#### 4.4.4 代码实践

**实践目标**：把 `fit()` 的恢复决策整理成一张可查的表。

**操作步骤**：

1. 精读 [trainer.py:L687-L728](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/trainer.py#L687-L728) 与 [common.py:L108-L128](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/common.py#L108-L128)。
2. 对下表每个格子写出结果：加载自哪里（save_folder / load_path / 不加载）、加载什么（trainer+optim / 默认 / —）、找不到时的结局。

   | load_strategy | save_folder 有 ckpt | save_folder 无、load_path 指向 ckpt | 两处都没有 |
   | --- | --- | --- | --- |
   | if_available | ? | ? | ? |
   | always | ? | ? | ? |
   | never | ? | ? | ? |

**需要观察的现象**：注意 `never` 分支连探测都不会做；`always` 的报错消息会同时列出两个位置。

**预期结果**：

| load_strategy | save_folder 有 | load_path 有（save_folder 无） | 都没有 |
| --- | --- | --- | --- |
| if_available | save_folder，trainer+optim 全载 | load_path，`load_trainer_state`/`load_optim_state` 按配置默认 | 警告后从头训 |
| always | 同上 | 同上 | `FileNotFoundError` |
| never | 不加载（即使有） | 不加载 | 从头训 |

#### 4.4.5 小练习与答案

**练习 1**：训练在 step 173 崩溃，重启后日志出现 `Will resume training from step 150`——为什么不是 173？

**答案**：恢复点只能是**已落盘的 checkpoint**。step 173 的状态只存在于内存里；最近的是某个保存间隔（`save_interval` 或更近的 `ephemeral_save_interval`）产出的 step 150 checkpoint，于是从 150 重放 150–173。这正是 ephemeral checkpoint 存在的理由：把「最多要重放多少步」从 save_interval 缩到 ephemeral_save_interval。

**练习 2**：换 world size 续训（4 卡存、8 卡载）后，哪些状态确定恢复、哪些确定不恢复？

**答案**：恢复——模型权重、优化器状态（DCP 重分片，与拓扑无关）、`global_step`、token 计数、epoch（从回落到的 `rank0.pt` 读）。不恢复——各 rank 的 RNG 状态（world size 不匹配直接跳过并告警）；数据加载器状态按其自身逻辑处理（u4-l1/u4-l2 讲过其指纹校验与全局索引）。此外 `max_steps` 本来就不从 checkpoint 恢复。

**练习 3**：从 `load_path` 加载一个预训练权重做微调，为什么默认不加载 trainer state？如果数据集和预训练完全相同，加载它会更快收敛吗？

**答案**：`load_path` 的语义是「用别处的权重初始化」，不是「继续那次训练」——微调通常换了数据、目标与调度，恢复别人的数据进度和 step 计数反而错位，所以默认 `load_trainer_state=False`（[examples/llm/train.py:L102-L105](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/examples/llm/train.py#L102-L105)）。若数据与配置完全一致，加载 trainer state 等价于把那次训练无缝延长，自然不会更差；但「微调」通常不满足这个前提，此时恢复 data loader 状态（含旧数据指纹校验）大概率直接报错或行为错乱。

## 5. 综合实践

**任务：完成一次「训练 → 中断 → 续训」，验证 loss 曲线无缝衔接。**

说明：讲义规划中原任务提到 `LoadStrategy.new_cmd`，该取值在 OLMo-core 中并不存在（全库检索仅命中三值枚举 `if_available` / `always` / `never`）。因此本实践用真实存在的机制完成同等的实验：恢复点由 save_folder 决定，策略与其它配置用 dotlist 覆盖。

**第一步（打底）**：运行官方测试，确认 Checkpointer 在你的机器上工作正常：

```bash
pytest -v src/test/train/checkpoint_test.py -k "local"
```

同时覆盖同步（`run_checkpointer`）与异步（`run_async_checkpointer`，[checkpoint_test.py:L82-L107](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/test/train/checkpoint_test.py#L82-L107)）两条路径，预期全绿（无 GPU 时自动走 CPU/gloo）。

**第二步（受控实验）**：写一个单进程两阶段脚本（**示例代码**骨架，基于 `BasicTrainModule` + 固定随机 batch，不依赖真实数据集）：

```python
# resume_experiment.py（示例代码，非项目自带）
import os, torch
os.environ["OLMO_SHARED_FS"] = "1"
from olmo_core.train.checkpoint import Checkpointer
from olmo_core.train.train_module import BasicTrainModule

SAVE = "/tmp/resume-exp"      # save_folder
STEPS, SAVE_AT = 10, 5

def build():
    torch.manual_seed(42)                      # 两次运行用同一 seed 初始化模型
    model = torch.nn.Sequential(torch.nn.Linear(16, 32), torch.nn.GELU(), torch.nn.Linear(32, 16))
    optim = torch.optim.AdamW(model.parameters(), lr=1e-3)
    return BasicTrainModule(model, optim, 128), model

def batch():
    g = torch.Generator().manual_seed(7)       # 固定数据流：两次运行的 batch 序列完全一致
    while True:
        x = torch.randn(4, 16, generator=g)
        yield x, x @ torch.eye(16)[:, :16]

def run_phase():
    tm, model = build()
    b = batch()
    start = 0
    ckptr = Checkpointer(work_dir="/tmp/resume-work")
    latest = ckptr.latest_checkpoint(SAVE) if os.path.isdir(SAVE) else None
    if latest is not None:                     # ← 断点续训：等价于 fit() 里的 if_available 恢复
        state = ckptr.load(latest, tm, load_trainer_state=True, load_optim_state=True)
        start = state["global_step"]
        print(f"resumed from step {start}")
    losses = []
    for step in range(start + 1, STEPS + 1):
        x, y = next(b)
        loss = tm.train_batch({"input_ids": x.long(), "label_mask": None},
                              torch.nn.functional.mse_loss(model(x), y))
        losses.append(float(loss))
        if step == SAVE_AT:
            ckptr.save(f"{SAVE}/{ckptr.checkpoint_dirname(step)}", tm,
                       {"global_step": step})
            print(f"saved at step {step} — 在这里 Ctrl-C 模拟中断")
    print("losses:", [round(l, 6) for l in losses])

if __name__ == "__main__":
    run_phase()
```

`BasicTrainModule.train_batch` 的确切签名/返回值请以 [train_module.py:L245-L259](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/train_module/train_module.py#L245-L259) 附近的实现为准，跑通前允许按实际接口微调调用方式（这正是练习的一部分：去读它的 `train_batch`）。

**操作步骤**：

1. 首次运行：`python resume_experiment.py`，应看到 saved at step 5 后继续跑完，打印 10 个 loss。
2. 删掉 `/tmp/resume-exp`，在 saved 与下一步之间 Ctrl-C 杀掉进程（模拟 step 5 中断），得到只含 `step5` 的 save_folder。
3. 再次运行同一命令：应打印 `resumed from step 5`，losses 从第 6 步开始共 5 个。
4. **对照**：把第 3 步的「step 6–10 的 loss」与第 1 步 uninterrupted 运行中「step 6–10 的 loss」逐项比较。
5. **变体**：把 `latest_checkpoint` 探测处想象成三种策略——注释掉加载逻辑即为 `never`；加载前断言 `latest is not None` 否则抛错即为 `always`。真实训练里等价于 `--trainer.load_strategy=never` / `always` 这类 dotlist 覆盖（u2-l3）。

**需要观察的现象**：中断重跑后 `resumed from step 5`；`{SAVE}/step5` 目录含 `.metadata.json`、`train/rank0.pt`、`model_and_optim/`。

**预期结果**：CPU 上固定 seed 时，续训的第 6–10 步 loss 与不间断运行的对应步**逐位一致**（优化器动量、权重、数据流全部恢复）；GPU 上因非确定性内核允许小容差。若对不齐，按「模型 → 优化器 → 数据流 → RNG」的顺序排查哪个没恢复。以上实验未在本环境运行（无 torch），**待本地验证**。

**进阶（真实 Trainer 层面）**：如果你完成了 u4-l1 的随机 `.npy` 数据实践，可参考 `src/examples/llm/train.py` 组装一次真实 `Trainer.fit()`（数据路径需自行准备）：第一次 `torchrun --nproc-per-node=1 ... --save-folder=/tmp/exp` 跑几个 step 后 Ctrl-C，改一个无关字段（如 `--trainer.cancel_check_interval=10`）再跑同一命令，观察日志中的 `Will resume training from step N` 与 `train/CE loss` 是否衔接。**待本地验证**。

## 6. 本讲小结

- 一个训练 checkpoint = `train/rankN.pt`（每 rank 一份 Trainer 状态）+ `model_and_optim/`（DCP 分布式模型与优化器状态）+ `.metadata.json`（rank 0 最后写的有效性标记）；目录名固定为 `step{N}`。
- 原子性贯穿始终：临时目录/临时文件 → `fdatasync` → rename/上传；删除则先摘 `.metadata.json` 再清目录。不变式是「checkpoint 要么完整、要么不存在」。
- `Checkpointer` 管「怎么存」，`CheckpointerCallback` 管「何时存」：`save_interval`/`fixed_steps`/`max_checkpoints` 管永久点，`ephemeral_save_interval` 给可抢占作业提供密集恢复点；异步保存需要独立 gloo 进程组，终存永远同步。
- 底层 `olmo_core.distributed.checkpoint` 包装 PyTorch DCP：`.metadata` 记录全局张量视图与字节区间，因此**保存与加载的 world size、分片策略可以不同**；`RemoteFileSystemWriter/Reader` 让同一套 API 直写直读 S3/GCS，读取时每 rank 只取自己那部分字节。
- 恢复语义分两档：命中 save_folder 是**续训**（强加载 trainer + optim 状态）；命中 load_path 是**初始化**（默认不加载 trainer 状态）。`LoadStrategy` 三值 `if_available` / `always` / `never` 只控制「找不找、找不到怎么办」。
- 续训恢复 step/token/epoch/回调/数据进度与（world size 一致时的）RNG；`max_steps` 存而不取，训练总长与 LR 曲线始终由本次运行配置决定。

## 7. 下一步学习建议

- **u7-l1（数据并行）**：本讲多次出现「FSDP 分片、DTensor、重分片」，下一单元将解释这些并行策略本身，理解后可回头做「换 world size 续训」的真实实验。
- **u10-l5（Checkpoint 转换）**：`unshard_checkpoint` / `load_unsharded_model_state` / `key_mapping` 正是 OLMo-core ↔ Hugging Face 权重互转的原料，届时会看到它们在转换脚本里的实际用法。
- **源码延伸阅读**：[src/test/train/checkpoint_test.py](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/test/train/checkpoint_test.py)（含 S3/GCS 变体与 reset 语义测试）；[ddp_train_module.py 的 save/load_state_dict_direct](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/train/train_module/transformer/ddp_train_module.py#L1150)（直接读写 API 的完整实现）；若想深挖 DCP 本身，对照阅读 `torch.distributed.checkpoint` 的官方 `FileSystemWriter` 与本库 `RemoteFileSystemWriter` 的差异（分桶、节流上传、写后校验）。
