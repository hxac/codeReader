# 分布式启动与无状态可恢复采样器

## 1. 本讲目标

上一讲（u3-l2）我们跟完了 `train()` 主循环，本讲回答两个被它"顺手用掉"的问题：**这些进程是怎么凑成一个通信组的**，以及**为什么中途 kill 掉再重启，每个 rank 能不多不少地接着上次的数据位置继续读**。全部答案集中在 [deepspec/utils/distributed.py](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/utils/distributed.py) 这一个不足 150 行的文件里。

学完本讲你应该能：

1. 推导 `init_dist` 的全局 rank 公式 \( \text{rank} = \text{node\_rank} \times L + \text{local\_rank} \)，并解释本仓库的 `RANK`/`WORLD_SIZE` 语义与 torchrun 有何不同。
2. 说明为什么在一台机器上不设任何环境变量、直接 `python train.py` 就能跑多卡训练。
3. 讲清 `StatelessResumableDistributedSampler` 的"无状态"含义：任意时刻的数据流只由 (seed, epoch 序号, rank, 偏移) 四个量纯函数式地决定，因此断点续训只需恢复 `next_micro_step` 一个整数。
4. 用"位置 ↔ (rank, 本 rank 步数)"的双射证明：同一 epoch 内 4 张卡读到的样本**不重不漏**，且从任意偏移恢复时恰好无缝衔接。
5. 区分 `is_global_main_process` 与 `is_local_main_process` 的职责边界：哪些动作全作业只能做一次，哪些日志每台机器打一份就够了。

## 2. 前置知识

本讲假设你已读过 u1-l3（train.py/eval.py 入口）与 u3-l1（BaseTrainer 初始化）。再补四个概念。

**进程组（process group）。** PyTorch 分布式训练的第一步是 `dist.init_process_group`：让一组进程互相"握手"，形成一个编号 0..W-1 的通信组，之后才能做 all_reduce、barrier 等集合通信。每个进程用一个全局唯一的 `rank` 标识，`world_size` 是进程总数。握手需要一个**会合点（rendezvous）**——通常是 `tcp://主节点IP:端口`，所有进程都去这个地址报到，凑齐 `world_size` 个后通信组成立。

**数据并行下的数据分片。** DeepSpec 用最朴素的数据并行：每张卡持有一份完整的草稿模型（默认 `no_shard`），各自读**不同的样本**算梯度，再由 FSDP 把梯度归约成全局梯度（见 u3-l2 的 `no_sync` 讨论）。如果两张卡读了同一条样本，等效 batch 变小、算力浪费；如果一条样本被所有卡跳过，它就等于没参与训练。所以"哪个 rank 该读哪些样本"必须是一个跨 rank 的精确划分——这就是采样器（Sampler）的职责。

**有状态迭代器 vs 无状态推导。** PyTorch 自带的 `DistributedSampler` 是**有状态**的：内部维护一个迭代器游标，靠外部在每个 epoch 边界调用 `set_epoch(epoch)` 换洗牌。一旦训练在第 37 步被 kill，重启后你只能从头读这个 epoch、或把迭代器位置也存进 checkpoint。DeepSpec 的做法相反：**不保存任何游标**，把"第 e 个 epoch 的洗牌顺序"设计成由 `seed + e` 确定的纯函数，任何 rank 在任何时刻都能独立重算出完整数据流，然后跳到指定位置。这就是类名里 Stateless（无状态）与 Resumable（可恢复）的含义。

**承接 u3-l2 的关键结论。** `next_micro_step` 是训练进度的唯一真相源，`global_step`、epoch、数据偏移都是它的派生量。本讲会看到这条链的最后一环：

\[ \text{数据偏移} = \text{next\_micro\_step} \times \text{local\_batch\_size} \]

把它喂给采样器，训练数据流就完整恢复了。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [deepspec/utils/distributed.py](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/utils/distributed.py) | 本讲主角：`init_dist`（L11-31）、main process 工具函数（L34-61）、`StatelessResumableDistributedSampler`（L63-141） |
| [deepspec/trainer/base_trainer.py](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/trainer/base_trainer.py) | 调用方：`__init__` 里的 `init_dist`（L158-160）、`_build_train_dataloader`（L295-314）、`train()` 的偏移计算（L360-368） |
| [train.py](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/train.py) | spawn 入口：每个 GPU 起一个进程（L45），`local_rank` 从这里来 |
| [scripts/train/train.sh](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/scripts/train/train.sh) | 单机默认启动示例，开头注释明确说明"不是 torchrun 语义"（L1-6） |
| [deepspec/trainer/ckpt_manager.py](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/trainer/ckpt_manager.py) | `TrainingResumeState`（L56-61）与 `load_training_state`（L84-133）：恢复 `next_micro_step` 并校验并行布局没变 |

## 4. 核心概念与源码讲解

### 4.1 init_dist：手写 rank 推导的分布式自举

#### 4.1.1 概念说明

`init_dist` 要解决的问题：`train.py` 用 `torch.multiprocessing.spawn` 在**每台机器**上各起了一堆进程（每个可见 GPU 一个），这些进程互相不知道对方的存在，需要有人告诉每个进程"你是全局第几号、总共多少个、去哪里会合"。

业界最常见方案是 `torchrun`：每台机器跑一个 elastic agent，由它注入 `LOCAL_RANK`、`RANK`（**全局进程号**）、`WORLD_SIZE`（**全局进程总数**）等环境变量。DeepSpec 没有走这条路——它面向的节点启动器（node launcher，内部集群调度风格）提供的是**节点粒度**的环境变量。于是 `init_dist` 干脆自己完成"节点序号 + 机内序号 → 全局进程号"的拼装。这不是重复造轮子，而是适配一套不同的启动约定。

#### 4.1.2 核心流程

设本机可见 GPU 数为 \( L \)（即本机进程数），节点序号为 \( n \)，节点总数为 \( N \)，spawn 传入的机内序号为 \( \ell \)：

\[ \text{rank} = n \times L + \ell, \qquad \text{world\_size} = N \times L \]

```text
init_dist(local_rank, timeout_minutes=60)
├── L = torch.cuda.device_count()          # 本机可见 GPU 数 = 本机进程数
├── n = env.RANK        (默认 "0")          # 注意：这里是"节点序号"，不是 torchrun 的全局 rank！
├── N = env.WORLD_SIZE  (默认 "1")          # 节点总数，不是进程总数
├── master = env.MASTER_ADDR:MASTER_PORT    # 默认 127.0.0.1:29500（单机回环即可）
├── rank = n*L + local_rank；world_size = N*L
├── torch.cuda.set_device(local_rank)       # 把本进程绑到自己的 GPU
└── dist.init_process_group(nccl, tcp://master, rank, world_size, device_id=device)
    └── 所有进程在 master 地址会合，凑齐 world_size 个后通信组成立
```

三个要点：

1. **零配置单机多卡。** 不设任何环境变量时 \( n=0 \)、\( N=1 \)、master 是本机回环地址，公式退化为 `rank = local_rank`、`world_size = GPU 数`。`train.sh` 正是这么跑的。
2. **多机只需三个环境变量。** 2 机 16 卡（每机 8 卡）时：两台机器都设 `MASTER_ADDR=<node0 的 IP> MASTER_PORT=29500 WORLD_SIZE=2`，node 0 额外设 `RANK=0`、node 1 设 `RANK=1`。于是 node 0 的进程得到 rank 0..7，node 1 得到 rank 8..15。
3. **绑定设备先行。** `set_device(local_rank)` 与传给进程组的 `device_id` 都在握手前完成，NCCL 通信组的设备归属从一开始就是明确的。

#### 4.1.3 源码精读

先看整体（仅 21 行）：

[deepspec/utils/distributed.py:L11-31](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/utils/distributed.py#L11-L31) —— `init_dist` 全文：第 12 行用 `torch.cuda.device_count()` 取本机可见 GPU 数作为"本机进程数"（它遵循 `CUDA_VISIBLE_DEVICES`）；第 13-16 行读环境变量并给全套默认值（`RANK="0"`、`WORLD_SIZE="1"`、`MASTER_ADDR="127.0.0.1"`、`MASTER_PORT="29500"`）——这四个默认值就是"单机直接跑"的全部秘密；第 17-18 行是本讲的两个核心公式；第 23-30 行以 nccl 后端、tcp 会合地址、显式 `rank`/`world_size` 建立进程组，超时默认 60 分钟，并把 `device_id` 一并绑定。

调用侧——`local_rank` 从哪来：

[train.py:L45](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/train.py#L45) —— `torch.multiprocessing.spawn(main, nprocs=torch.cuda.device_count())`：父进程按可见 GPU 数 spawn 子进程，`local_rank` 就是 spawn 分配的 0..L-1。注意第 31-38 行的 `main(local_rank)` 里，每个子进程各自 `parse_args` 再实例化 trainer——配置在每个进程里独立重建。

[deepspec/trainer/base_trainer.py:L158-160](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/trainer/base_trainer.py#L158-L160) —— `BaseTrainer.__init__` 第一件正事就是 `self.device, self.global_rank, self.world_size = init_dist(local_rank)`；设备、全局 rank、进程总数从此成为实例属性，后面所有模块（FSDP 包装、采样器、日志）都从这里取值。eval 侧的 `BaseEvaluator` 对称地做同样的事（u1-l3 已讲），一套自举服务两个入口。

启动脚本的"官方口径"：

[scripts/train/train.sh:L1-6](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/scripts/train/train.sh#L1-L6) —— 注释明确写着 "Local launch mirrors the repo's node launcher, **not standard torchrun semantics**"。第 38 行 `CUDA_VISIBLE_DEVICES=0,1,2,3,4,5,6,7 python train.py ...` 不设任何 rank 环境变量：单机 8 卡，rank 0..7，world_size=8。

与 torchrun 的语义对照（重点：**不要混用**）：

| | torchrun 约定 | 本仓库 `init_dist` 约定 |
| --- | --- | --- |
| `RANK` | 全局进程号（进程粒度） | **节点序号** node_rank（节点粒度） |
| `WORLD_SIZE` | 全局进程总数 | **节点总数** |
| 机内序号 | agent 注入 `LOCAL_RANK` 环境变量 | spawn 函数参数 `local_rank` |
| 进程创建 | 每节点一个 elastic agent | train.py 自己 `spawn`，nprocs=可见 GPU 数 |
| 多机方式 | `--nnodes --node_rank` 等参数 | 手动给每台机器设 `RANK/WORLD_SIZE/MASTER_ADDR` |

若在 torchrun 下直接跑 `train.py`，torchrun 注入的进程粒度 `RANK` 会被当成节点序号参与 `n*L+\ell` 运算，rank 会被算重——所以脚本注释才特意强调语义差异。

#### 4.1.4 代码实践

**实践目标**：不动 GPU，验证 rank 推导公式在不同启动布局下的正确性。

**操作步骤**（以下为示例代码，非仓库原有代码；无需 PyTorch，纯 Python 即可运行）：

```python
# rank_math_demo.py —— 复刻 init_dist L12-18 的推导逻辑
def init_dist_math(local_rank, visible_gpus, env):
    local_world_size = visible_gpus            # L12: device_count()
    node_rank = int(env.get("RANK", "0"))      # L13
    node_world_size = int(env.get("WORLD_SIZE", "1"))  # L14
    rank = node_rank * local_world_size + local_rank   # L17
    world_size = node_world_size * local_world_size    # L18
    return rank, world_size

# 布局一：单机 8 卡，无任何环境变量（train.sh 的情形）
for lr in range(8):
    print(lr, init_dist_math(lr, 8, {}))
# 布局二：2 机各 8 卡
for node, env in [(0, {"RANK": "0", "WORLD_SIZE": "2"}),
                  (1, {"RANK": "1", "WORLD_SIZE": "2"})]:
    for lr in range(8):
        print(f"node{node}", lr, init_dist_math(lr, 8, env))
# 布局三：单机单卡（调试常用）
print(init_dist_math(0, 1, {}))
```

**需要观察的现象**：布局一中 rank 恰为 0..7、world_size=8；布局二中 node0 得 0..7、node1 得 8..15、world_size=16；布局三退化为 (0, 1)。

**预期结果**（由公式直接推得）：三个布局的 rank 集合分别是 \{0..7\}、\{0..15\}（无重复无缺失——这正是 `dist.init_process_group` 能成立的前提，rank 冲突会在握手时挂起或报错）、\{0\}。单卡也会走完整的进程组初始化，所以后续代码里的 `dist.barrier()`/all_reduce 在单卡下同样安全。

#### 4.1.5 小练习与答案

**练习 1**：4 机、每机 8 卡，node 2 上的 5 号进程的全局 rank 是多少？world_size 是多少？

答：rank \( = 2 \times 8 + 5 = 21 \)；world_size \( = 4 \times 8 = 32 \)。

**练习 2**：为什么 `MASTER_ADDR` 默认 `127.0.0.1` 在多机场景下会失败？

答：回环地址只在本机可达。多机时所有进程必须到同一个**跨机可达**的地址会合，因此必须把 `MASTER_ADDR` 设成 node 0 的对外 IP（`MASTER_PORT` 同理要未被占用且防火墙放行）。

**练习 3**：`torch.cuda.device_count()` 在布局上起了哪两个作用？

答：一是决定 train.py spawn 的进程数（`nprocs`），二是作为 `local_world_size` 参与 rank/world_size 公式。两者必须一致，否则 spawn 出的进程数与通信组规模对不上。

### 4.2 main process 工具函数：谁有资格打印、谁有资格落盘

#### 4.2.1 概念说明

有了通信组，马上出现一个工程问题：很多动作**只应该由一个进程做**——checkpoint 目录只建一次、TensorBoard 只写一份、自动评测只提交一次；否则 16 个进程会互相覆盖。但另一些动作希望**每台机器**都能看到，比如编译提示、训练信息板——只让全局 rank 0 打的话，其他 13 台机器的控制台是空的，运维排查不便。

于是 DeepSpec 划分两级"主进程"：

- **global main process**（全局唯一）：`dist.get_rank() == 0`，负责"全作业只做一次"的动作。
- **local main process**（每机一个）：`torch.cuda.current_device() == 0`，即每台机器上 `local_rank == 0` 的那个进程，负责"每机打一份"的日志。

单机训练时两者恰好是同一个进程，多机时 global main 是"local main 中的第 0 个"。

#### 4.2.2 核心流程

| 工具 | 判定条件 | 全作业命中次数 | 典型用途 |
| --- | --- | --- | --- |
| `is_global_main_process()` | `dist.get_rank() == 0` | 1 | 建目录、写 TensorBoard、提交自动评测、执行挂起 |
| `is_local_main_process()` | `current_device() == 0` | 节点数 | info_board、编译/续训提示日志 |
| `print_on_global_main(...)` | global main 才打印 | 1 | 关键路径日志（带时间戳） |
| `print_on_local_main(...)` | local main 才打印 | 节点数 | 每机器可见的运行日志（带时间戳） |
| `main_process_first()` | rank 0 先执行，其余 barrier 等待 | — | "rank 0 先下载/写共享文件，其他人再读"的顺序控制 |

#### 4.2.3 源码精读

[deepspec/utils/distributed.py:L34-39](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/utils/distributed.py#L34-L39) —— 两个判定函数本体。注意 `is_local_main_process` 用的是 `torch.cuda.current_device()` 而非某个存储的属性：它依赖 `init_dist` 里的 `set_device(local_rank)` 已把当前设备设为本进程的 GPU，所以"当前设备为 0"等价于"我是本机的 0 号进程"。**隐含的调用顺序约束**：必须先 `init_dist` 再用这些工具函数，否则所有进程的 `current_device()` 都是默认 0，判定会失真。

[deepspec/utils/distributed.py:L42-51](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/utils/distributed.py#L42-L51) —— 两个打印函数：命中时给输出加 `%Y-%m-%d %H:%M:%S` 时间戳前缀，并 `setdefault("flush", True)`——多进程日志不 flush 极易乱序/丢失，这个默认值很实用。

[deepspec/utils/distributed.py:L54-61](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/utils/distributed.py#L54-L61) —— `main_process_first` 上下文管理器：rank 0 先 `yield`（执行 With 块）再 `barrier`；其他 rank 先 `barrier` 等 rank 0 做完再执行。数据准备脚本用它保证"rank 0 先把共享文件准备好，其余 rank 再开工"（见 [scripts/data/prepare_target_cache.py:L240](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/scripts/data/prepare_target_cache.py#L240) 的调用）。

调用方清单（谁在用哪一级）：

- [deepspec/trainer/base_trainer.py:L169](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/trainer/base_trainer.py#L169) —— `if is_global_main_process(): ensure_dir(...)`：checkpoint 根目录只建一次。
- [deepspec/trainer/base_trainer.py:L185-187](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/trainer/base_trainer.py#L185-L187) 与 [L232-233](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/trainer/base_trainer.py#L232-L233) —— `print_on_local_main` 打 "Compiling..." 与 "Training from scratch."：每台机器一份。
- [deepspec/trainer/base_trainer.py:L333-344](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/trainer/base_trainer.py#L333-L344) —— `save_and_eval_checkpoint` 中只有 global main 提交自动评测（`_launch_eval`），且用 `dist.barrier()` 等所有 rank 存完盘再继续。
- [deepspec/trainer/base_trainer.py:L346-353](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/trainer/base_trainer.py#L346-L353) —— `_save_and_suspend` 中只有 global main 执行 `go_suspend()`（u3-l2 讲过的挂起路径）。
- [deepspec/utils/training_logger.py:L19](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/utils/training_logger.py#L19)、[L47](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/utils/training_logger.py#L47) —— TensorBoard 事件文件只由 global main 写。
- checkpoint 的落盘协调同样以 global main 为闸门（[deepspec/trainer/ckpt_manager.py:L34](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/trainer/ckpt_manager.py#L34) 等，u3-l5 详讲）。

#### 4.2.4 代码实践

**实践目标**：亲手把两个"主进程"的调用点分类，建立"哪些动作全作业一次、哪些每机一次"的直觉。

**操作步骤**：

1. 在仓库根目录执行 `grep -rn "is_global_main_process\|is_local_main_process\|print_on_global_main\|print_on_local_main\|main_process_first" --include="*.py" deepspec/ scripts/`。
2. 把命中行按"判定/打印"与"global/local"分成四类，各挑 3 处点开上下文，回答：这行代码如果换成另一级会发生什么（例如 TensorBoard 改成 local main 会出现什么问题）？

**需要观察的现象**：global 类命中集中在 ckpt_manager、training_logger、评测提交、挂起、缓存收尾（[scripts/data/prepare_target_cache.py:L353](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/scripts/data/prepare_target_cache.py#L353)、[L373](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/scripts/data/prepare_target_cache.py#L373)）；local 类命中集中在训练器内的提示性打印（info_board 等）。

**预期结果**：分类后你会得到一张与 4.2.2 表格一致的清单——所有**写共享文件/发外部动作**的都是 global main，所有**人看的运行日志**都是 local main。TensorBoard 若改成 local main，多机时每个节点各写一份事件文件、互相覆盖/翻倍（具体表现待本地多机验证）。

#### 4.2.5 小练习与答案

**练习 1**：2 机 16 卡时，`is_global_main_process` 与 `is_local_main_process` 分别有几个进程返回 True？

答：各 1 个和 2 个。global main 只有一个（全局 rank 0）；local main 每机一个（两台机器各自的 0 号进程），全局 rank 分别是 0 和 8。

**练习 2**：`main_process_first` 里两个分支的 `barrier` 顺序为什么必须不同？

答：rank 0 分支是"先执行再 barrier"——它做完共享工作后到 barrier 等大家；其他分支是"先 barrier 再执行"——在 barrier 处等 rank 0 做完才进入 with 块。若两个分支顺序写反，所有进程都在等对方先过 barrier，造成死锁。

### 4.3 StatelessResumableDistributedSampler：从任意偏移恢复的数据流

#### 4.3.1 概念说明

这是本讲最核心的组件，解决的问题是：**`train()` 只构造一次 DataLoader、贯穿全部 epoch，且重启后必须从"任意中间位置"继续**。回顾 u3-l2：主循环把 `next_micro_step` 存进 checkpoint；重启时 `train()` 用

\[ \text{start\_offset} = \text{next\_micro\_step} \times b \quad (b = \text{local\_batch\_size}) \]

重建采样器。注意一个容易误读的细节：参数名叫 `start_global_offset_samples`，但它的单位是**本 rank 已消费的样本数**（跨 epoch 累计），不是"所有 rank 加起来"的全局样本数——`next_micro_step` 与 `b` 都是每卡量纲。这里的 "global" 指"贯穿 epoch 的绝对计数"，与 4.1 的"全局 rank"是两码事。

"无状态"的准确含义：采样器的输出流是四个量的纯函数

\[ \text{stream}(e, r) = P_e[\,r::R\,], \qquad P_e = \text{randperm}(N,\ \text{seed} = s + e)[:T] \]

其中 \( e \) 是 epoch 序号、\( r \) 是 rank、\( R \) 是 rank 数、\( N \) 是数据集大小、\( T \) 是每 epoch 使用的样本数（`total_size`，即 u3-l1 的 `samples_per_epoch`）。没有任何需要存盘的游标——checkpoint 里只要有 `next_micro_step`，一切都能重算出来。

#### 4.3.2 核心流程

**每个 epoch 用确定性的前 T 个。** 用 `torch.Generator` 以 `seed + epoch_idx` 播种后做 `randperm(N)`，取前 T 个（T ≤ N 且 T 是 R 的倍数，构造时断言保证）。dataset 尾部凑不满一个全局批的样本（\( N - T \) 条）在每个 epoch 都被"重新洗牌后再排除"，而不是永远弃用。

**跨 rank 的划分：按 stride 切。** rank \( r \) 取 \( P_e[r], P_e[r{+}R], P_e[r{+}2R],\dots \)。可以严格证明这是一个划分：当 \( R \mid T \) 时，映射

\[ p \;\longmapsto\; \big(p \bmod R,\; \lfloor p / R \rfloor\big) \]

是 \(\{0,\dots,T-1\} \to \{0,\dots,R{-}1\} \times \{0,\dots,T/R{-}1\}\) 的**双射**。翻译成人话：epoch 内第 \( p \) 个位置，由 rank \( p \bmod R \) 在它的第 \( \lfloor p/R \rfloor \) 步读取。每个位置恰有一个 rank 读、恰好读一次——**不重不漏**，且这条性质只依赖 stride 切法本身，与洗牌结果具体是什么无关。

**跨 epoch 连续流。** 采样器把数据看成一个无限流：`_iter_stream` 从当前 (epoch, epoch 内偏移) 开始，读完后自动进入 `epoch+1`（新 seed、新洗牌），永不回头。`__iter__` 从这个无限流里精确截取 `len(self)` 个。

**断点续训。** 给定本 rank 偏移 \( o \)：\( e = \lfloor o / (T/R) \rfloor \)，\( m = o \bmod (T/R) \)。各 rank 一起跳过自己在 epoch \( e \) 内的前 \( m \) 个样本——集体验证：四个 rank 各跳 \( m \) 个，等价于全体跳过了 \( P_e \) 的前 \( mR \) 个位置，下次读取从位置 \( mR \) 无缝继续。

**长度语义。** `num_samples=None`（默认）时 `__len__` 返回"当前 epoch 的剩余量"，兼容"每个 epoch 重建 DataLoader"的老用法；`train()` 显式传入 `num_samples = 剩余微步数 × b`，于是采样器一次供完整个剩余训练。

#### 4.3.3 源码精读

**构造与合同校验。**

[deepspec/utils/distributed.py:L76-105](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/utils/distributed.py#L76-L105) —— `__init__`：L86 起是一串防御性断言——偏移非负、数据集非空、`total_size` 不超过数据集大小且必须被 `num_replicas` 整除（L95-100，这正是 4.3.2 双射成立的前提，不整除会在 stride 切分时产生重叠或缺口）；L103-105 算出每 rank 每 epoch 样本数 `per_rank_len_per_epoch = total_size // num_replicas` 并存下偏移与 `num_samples`。注意 L64-74 的类 docstring 已把"跨 epoch 流式 + 任意偏移 + 定长输出"三个语义写清楚了。

**确定性洗牌。**

[deepspec/utils/distributed.py:L113-116](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/utils/distributed.py#L113-L116) —— `_epoch_perm`：每次用**独立的** `torch.Generator`、以 `seed + epoch_idx` 播种后 `randperm(dataset_size)` 取前 `total_size`。不碰全局 RNG，所以同样的种子在任何机器、任何进程上算出同样的排列——这是"无状态可重算"的物理基础。还要注意一点：`BaseTrainer._build_train_dataloader` 构造采样器时**没有传 seed**，因此恒为默认值 42；改 `args.seed` 只影响模型初始化等随机性，不影响数据顺序（配置里的 `seed` 与采样器的 `seed` 是两回事）。

**按 stride 切分给各 rank。**

[deepspec/utils/distributed.py:L118-119](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/utils/distributed.py#L118-L119) —— `_epoch_slice_for_rank`：一行 `perm[self.rank : self.total_size : self.num_replicas]`，就是 4.3.2 公式 \( P_e[r::R] \) 的字面实现。这一行承担了"跨 rank 不重不漏"的全部重量。

**无限流与截断。**

[deepspec/utils/distributed.py:L121-141](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/utils/distributed.py#L121-L141) —— `_iter_stream`：L122-123 由全局偏移解出 `epoch_idx` 与 `epoch 内偏移`；L125-128 先吐出本 epoch 剩余部分；L130-136 进入 `while True`，逐个 epoch 用新 seed 重算排列、整段吐出——跨边界完全无感。`__iter__`（L138-141）从这条无限流里 `next` 恰好 `len(self)` 次。`__len__`（[L107-111](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/utils/distributed.py#L107-L111)）：给了 `num_samples` 就用之；否则返回当前 epoch 剩余量（整除时返回完整一个 epoch，而不是 0）。

**训练侧的两个调用点。**

[deepspec/trainer/base_trainer.py:L295-314](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/trainer/base_trainer.py#L295-L314) —— `_build_train_dataloader`：把 `self.train_dataset`（u2-l6 的 `CacheDataset`）、`world_size`、`global_rank`、`samples_per_epoch` 与偏移/定长全部传给采样器；DataLoader 侧 `drop_last=True`、`persistent_workers=True`、`pin_memory=True`、`prefetch_factor=4`——配合一次性构建、贯穿全程的设计（`persistent_workers` 只有在 DataLoader 长期存活时才有意义）。

[deepspec/trainer/base_trainer.py:L360-368](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/trainer/base_trainer.py#L360-L368) —— `train()` 开头：由 `next_micro_step` 算出剩余微步数、再算剩余样本数 `remaining_samples = remaining_micro_steps * local_batch_size`，随后以 `start_offset_samples = next_micro_step * local_batch_size` 构造采样器。这两行就是"唯一真相源 → 数据流"的换算终点：**恢复训练 = 恢复一个整数 + 一段纯函数推导**。

**恢复侧的配套校验。**

[deepspec/trainer/ckpt_manager.py:L84-133](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/trainer/ckpt_manager.py#L84-L133) —— `load_training_state`：每个 rank 读**自己的**状态文件（L94），恢复优化器与 RNG 状态（L98、L114-117）；L101-112 断言存档时的 `next_micro_step` 对齐梯度累积、且 `global_rank`/`world_size`/`local_batch_size` 与当前运行完全一致——也就是说**断点续训必须沿用相同的并行布局**，否则偏移算术失效；L119-120 顺手用 `next_micro_step // micro_batches_per_epoch + 1` 推出当前 epoch 用于打印，这与采样器 L122 的 `epoch_idx` 推导（`o // per_rank_len`，其中 \( o = \text{next\_micro\_step} \times b \)、`per_rank_len` \( = \text{micro\_batches\_per\_epoch} \times b \)）在整数除法下完全一致——同一进度量的两种口径互相印证。

#### 4.3.4 代码实践（本讲主实践）

**实践目标**：模拟 `world_size=4`、每 epoch 100 个样本（即 `dataset_size=100`、`global_batch_size=4`，于是 `total_size=100`）、`local_batch_size=1` 的布局，分别从偏移 0 与偏移 37 恢复，验证每个 rank 读到的样本索引**不重不漏、无缝衔接**。

**操作步骤**（示例代码；仓库环境已含 torch，可直接 `python` 运行。采样器只需要 dataset 支持 `len()`，用 `list(range(100))` 代替 38TB 的真实缓存）：

```python
# sampler_resume_sim.py
from deepspec.utils.distributed import StatelessResumableDistributedSampler

N, R, T = 100, 4, 100          # dataset_size, world_size, samples_per_epoch
dataset = list(range(N))

def run(rank, offset, num):
    s = StatelessResumableDistributedSampler(
        dataset=dataset, num_replicas=R, rank=rank, total_size=T,
        start_global_offset_samples=offset, num_samples=num,
    )
    return list(iter(s))

full   = {r: run(r, 0, 62) for r in range(R)}   # 从 0 跑 62 个：epoch0 的 25 个 + epoch1 的 25 个 + epoch2 的 12 个
resume = {r: run(r, 37, 25) for r in range(R)}  # 从 37 恢复再跑 25 个

# 验证 1：无缝衔接 —— 偏移 37 恢复的流 == 从 0 的流跳过前 37 个
assert all(resume[r] == full[r][37:] for r in range(R))

# 验证 2：epoch0 不重不漏（full 的前 25 个/每 rank 恰是 epoch0）
epoch0 = [x for r in range(R) for x in full[r][:25]]
assert len(epoch0) == N and set(epoch0) == set(range(N))

# 验证 3：epoch1 剩余段不重不漏 —— 4 个 rank 各取 13 个，并集应为 epoch1 排列的后 52 个位置
tail1 = [x for r in range(R) for x in resume[r][:13]]
assert len(tail1) == 52 and len(set(tail1)) == 52

# 验证 4：断点处恰好接续 —— 偏移 37 意味着各 rank 已消费 25+12 个
assert run(0, 37, 3) == full[0][37:40]
print("epoch_idx =", 37 // (T // R), " offset_in_epoch =", 37 % (T // R))
print("all checks passed")
```

**需要观察的现象**：

- 偏移 37 时打印 `epoch_idx = 1  offset_in_epoch = 12`（因为 `per_rank_len_per_epoch = 100 // 4 = 25`，\( 37 = 1 \times 25 + 12 \)）——采样器自动跨过了一整个 epoch。
- `resume[r]` 与 `full[r][37:]` 逐元素相等：恢复前后读到的是**同一条**确定性数据流。
- 各 rank 的 epoch0 片段拼起来恰好是 0..99 的全体、无重复；epoch1 剩余段 4×13=52 个也两两不同。

**预期结果**（由源码逻辑推得，具体索引数值以本地运行为准）：rank \( r \) 在 epoch 0 读取洗牌 \( P_0 \) 的位置 \( r, r{+}4, r{+}8, \dots, r{+}96 \)；从偏移 37 恢复时，rank \( r \) 先读 \( P_1[r::4] \) 的第 12..24 项（13 个），随后进入 \( P_2[r::4] \)。全体 rank 在 epoch 1 已消费位置 0..47（每 rank 12 个 × 4），恢复后从位置 48 继续——任何位置恰好被读一次，断点前后严丝合缝。三条 assert 全部通过。

**手工推演（不运行也能做）**：把 `dataset_size` 换成 7、`total_size` 换成 4、`num_replicas=2`，先预测偏移 1 时 `epoch_idx/offset_in_epoch` 与各 rank 首个索引，再对照 L122-128 验证。

#### 4.3.5 小练习与答案

**练习 1**：`total_size=100`、`num_replicas=4` 时，偏移 51 对应的 `epoch_idx` 与 `offset_in_epoch` 是多少？rank 2 在恢复后读的第一个样本，等于"从 0 连续跑"时的第几个（自己 rank 的第几个）？

答：`per_rank_len_per_epoch = 25`，\( 51 = 2 \times 25 + 1 \)，故 `epoch_idx=2`、`offset_in_epoch=1`；rank 2 恢复后读的第一个样本等于它从 0 连续跑时的第 52 个（索引 51，零基）。

**练习 2**：为什么构造函数坚持断言 `total_size % num_replicas == 0`？举一个反例说明破坏它会怎样。

答：stride 切分 \( P[r::R] \) 的长度是 \( \lceil (T-r)/R \rceil \)，仅当 \( R \mid T \) 时各 rank 长度相等且拼起来恰好是 T 个位置。反例：T=6、R=4 时 `perm[0::4]` 与 `perm[2::4]` 各 2 个，而 `perm[1::4]`、`perm[3::4]` 各 1 个——rank 间每 epoch 样本数不齐，微步无法对齐，且位置 4、5 只被 rank 0、1 读到，划分不再是无缝的。

**练习 3**：如果断点续训时把 `world_size` 从 8 改成 4，会发生什么？

答：过不了 `load_training_state` 的校验——[ckpt_manager.py:L108-109](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/trainer/ckpt_manager.py#L108-L109) 断言存档的 `world_size` 与当前一致，直接 AssertionError。这是有意的：`per_rank_len_per_epoch` 与偏移都按旧布局定义，换布局后同一偏移对应的数据位置不再有意义。

## 5. 综合实践

把本讲三个模块串成一次"纸上推演 + 脚本验证"：

1. **布局推导**：设计一个 2 机 16 卡（每机 8 卡）的启动方案：写出两台机器各自需要设置的 `RANK`、`WORLD_SIZE`、`MASTER_ADDR`、`MASTER_PORT` 与 `CUDA_VISIBLE_DEVICES`，用 4.1.4 的脚本验证两机 rank 合起来恰为 0..15、world_size=16。
2. **职责标注**：在 `BaseTrainer` 里给每一处 `is_global_main_process` / `print_on_local_main` 调用加一行注释，写明"这个动作为什么是全局一次/每机一次"。
3. **断点轨迹**：假设 `dataset_size=1000`、`global_batch_size=32`、`world_size=8`、`local_batch_size=1`、`num_train_epochs=2`。先算 `samples_per_epoch`、`per_rank_len_per_epoch`、`micro_batches_per_epoch`、总微步数；再回答：在 `next_micro_step=250` 时被抢占，恢复时的 `epoch_idx`、`offset_in_epoch` 各是多少？（手工算完，可仿照 4.3.4 的脚本用 `dataset=list(range(1000))`、`total_size` 取上一步结果验证。）
4. **对照主循环**：回到 u3-l2 读过的 [base_trainer.py:L360-368](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/trainer/base_trainer.py#L360-L368)，确认"kill 进程 → 重启 → 读 checkpoint 恢复 `next_micro_step` → 换算偏移 → 采样器重算数据流"这条链上没有任何一步依赖未保存的状态。

第 3 步参考答案：`samples_per_epoch = (1000//32)*32 = 992`；`per_rank_len = 992/8 = 124`；`micro_batches_per_epoch = 124`（local_batch=1）；总微步 \( = 2 \times \lfloor 124 \times ? \)——注意 `steps_per_epoch = 124 // G`，而 \( G = 32/8 = 4 \)，故 `steps_per_epoch = 31`、总微步 \( = 2 \times 31 \times 4 = 248 \)。偏移换算：全局偏移 \( = 250 \times 1 = 250 \)，`epoch_idx = 250 // 124 = 2`——但总微步只有 248，250 超出了步数上限。事实上这个值根本不可能来自真实 checkpoint：存盘时断言 `next_micro_step % G == 0`（[ckpt_manager.py:L149-153](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/trainer/ckpt_manager.py#L149-L153)），而 250 对 4 不整除，恢复时的对齐断言（L101-103）会先把它拦下。换成合法的 `next_micro_step=150` 再算：`epoch_idx = 150 // 124 = 1`、`offset_in_epoch = 26`，与 `load_training_state` 打印的 `epoch = 150 // 124 + 1 = 2` 一致。这个"故意选超界值再纠正"的小陷阱，正好检验你是否真的理解了 `samples_per_epoch` 向下取整与梯度累积的联动（u3-l1 的 `_compute_training_schedule`）。

## 6. 本讲小结

- `init_dist` 用 \( \text{rank} = \text{node\_rank} \times L + \text{local\_rank} \)、\( \text{world\_size} = N \times L \) 手写自举进程组；本仓库的 `RANK`/`WORLD_SIZE` 是**节点粒度**，与 torchrun 的进程粒度语义不同，不能混用。
- 不设任何环境变量时默认单机多卡（master 为本机回环地址、单节点、world=GPU 数），`train.sh` 就是零环境变量启动。
- 两级主进程分工：global main（`dist.get_rank()==0`）负责建目录、写 TensorBoard、提交评测与挂起等"全作业一次"的动作；local main（每机 `local_rank==0`）负责每机器一份的运行日志。
- `StatelessResumableDistributedSampler` 把数据流设计成 (seed, epoch, rank) 的纯函数：每 epoch 用 `seed+epoch` 独立 Generator 洗牌取前 T 个，各 rank 按 stride \( P[r::R] \) 切分；由"位置 ↔ (rank, 本 rank 步数)"的双射保证同一 epoch 内不重不漏。
- 断点续训的换算链全部由 `next_micro_step` 派生：`偏移 = next_micro_step × local_batch_size`，`epoch_idx = 偏移 // per_rank_len`；采样器不需要存任何游标，且恢复时强制沿用相同并行布局。
- 采样器流式跨越 epoch 边界、`train()` 一次性构造贯穿全程的 DataLoader（`persistent_workers`/`drop_last`/`prefetch_factor` 均服务于此）。

## 7. 下一步学习建议

本讲补完了 u3-l2 主循环的两大依赖。第 3 单元还剩三块基础设施，建议按序推进：

1. **u3-l4（BF16Optimizer 与两段式学习率调度）**：主循环里 `optimizer.step()` 的内部——fp32 主权重管理、warmup+cosine 调度及其状态保存。
2. **u3-l5（检查点管理）**：本讲多处引用的 `ckpt_manager` 全文——`step_latest` 原子符号链接、每 rank 状态文件、`train_config.py` 回写，把"断点续训闭环"的最后一块拼图放好。
3. 想先跳出训练框架的读者，可以直接进入第 4 单元（DSpark 建模），需要时再回看本讲的采样器——它同样服务于 Eagle3 训练器（`Qwen3Eagle3Trainer` 复用同一 `BaseTrainer` 骨架与同一采样器）。
