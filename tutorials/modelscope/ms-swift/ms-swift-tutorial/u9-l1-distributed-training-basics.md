# 分布式训练基础

## 1. 本讲目标

本讲是「分布式与高性能训练」单元的第一篇。前面几讲（u5 系列）我们都在「单卡」假设下理解 SFT 训练流水线，但当模型变大（7B、14B、72B）或数据变多时，单卡显存和算力都会不够用，必须用多张卡甚至多台机器一起训练。

学完本讲，你应该能够：

- 说清 `swift sft` 在多卡下是如何被 `torchrun` 拉起的，以及 `NPROC_PER_NODE` / `NNODES` 等环境变量如何决定进程拓扑。
- 理解 ms-swift 把 `--deepspeed zero2` 这种简写展开成真实 ZeRO 配置的源码流程，并能根据显存压力在 zero2/zero3/zero3_offload 之间做选择。
- 知道 FSDP2 与 `device_map`（模型并行）两种并行方式的差异、它们与 DeepSpeed 的互斥关系，以及 ms-swift 对「device_map + DDP」混用做的特殊补丁。
- 动手跑一个 `NPROC_PER_NODE=2 + deepspeed zero2` 的多卡 SFT，并与单卡对比显存和速度。

> 本讲只覆盖「数据并行家族」（DDP / DeepSpeed ZeRO / FSDP）与「最朴素的模型并行 device_map」。更高级的张量并行 / 流水并行 / 序列并行留到 u9-l2、u9-l3、u9-l4（Megatron）。

---

## 2. 前置知识

在进入源码之前，先用最朴素的语言建立三个直觉。读者如果已经熟悉，可以跳到第 4 节。

### 2.1 为什么要分布式：显存与算力两道墙

一次前向 + 反向训练，GPU 显存里至少要装下三类东西：

| 类别 | 内容（以混合精度 Adam 为例） | 大小（Ψ 为模型参数量） |
| --- | --- | --- |
| 模型参数 | 权重本身（bf16） | \(2\Psi\) 字节 |
| 梯度 | 反向传播产生的梯度（bf16） | \(2\Psi\) 字节 |
| 优化器状态 | Adam 的一阶/二阶矩 + fp32 主权重 | \(12\Psi\) 字节 |
| 激活值 | 前向中间结果（与 batch、序列长度相关） | 另算 |

一个 7B 模型光是「参数 + 梯度 + 优化器状态」就要约 \(16\Psi = 16 \times 7 \times 10^9 \approx 112\) GB，单张 80G 卡根本放不下——这是**显存墙**。即便放得下（比如 LoRA 只训少量参数），单卡算力也会让训练慢到不可接受——这是**算力墙**。分布式训练就是用多卡同时解决这两道墙。

### 2.2 三种最朴素的并行思路

1. **数据并行（Data Parallel, DP / DDP）**：每张卡都完整持有模型，把数据切分给各卡各自算前向反向，再在反向时把梯度「归约（all-reduce）」求平均。优点是简单，缺点是每张卡都冗余存了完整模型，显存墙没解决。
2. **模型并行（Model Parallel, MP / device_map）**：把模型按层「切片」，第 1～10 层在 0 号卡、第 11～20 层在 1 号卡，数据像流水线一样流过去。显存分摊了，但同一时刻只有一张卡在干活，算力利用率低。
3. **分片并行（ZeRO / FSDP）**：数据和模型都不完整复制，而是把「优化器状态 / 梯度 / 参数」切成 N 份分给 N 张卡，用到时再临时聚合（all-gather）。这是目前大模型全量微调的主流方案，也是本讲重点。

### 2.3 谁来启动这些进程

PyTorch 自己不带「把一个训练脚本同时拉起 N 个进程」的能力，这件事由 **`torchrun`**（`torch.distributed.run`）完成。`torchrun` 会在每个进程里注入一组环境变量（`RANK` / `LOCAL_RANK` / `WORLD_SIZE` / `LOCAL_WORLD_SIZE` / `MASTER_ADDR` / `MASTER_PORT`），训练脚本读这些变量就知道「我是第几个进程、一共几个进程、通信地址是什么」。ms-swift 的工作，就是在 `swift` 命令和真正的训练脚本之间，加一层「要不要套 torchrun」的判定。

---

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `swift/cli/main.py` | CLI 总入口：判定是否套 `torchrun`，拼出真实命令并 `subprocess.run`。 |
| `swift/cli/utils.py` | `SWIFT_SINGLE_DEVICE_MODE` 单设备模式相关的环境变量修正。 |
| `swift/utils/env.py` | 分布式环境解析：`get_dist_setting` / `is_dist` / `is_master` / `is_mp` / `is_mp_ddp`。 |
| `swift/utils/torch_utils.py` | `set_device` / `init_process_group`，以及 DDP 默认配置。 |
| `swift/arguments/base_args/base_args.py` | `BaseArguments` 读取 `get_dist_setting`、`ddp_*` 字段、`_init_device`。 |
| `swift/arguments/base_args/model_args.py` | `device_map` / `max_memory` 字段及其 MP+DDP 适配。 |
| `swift/arguments/sft_args.py` | `_init_deepspeed` / `_init_fsdp`：把简写展开成真实配置并做互斥校验。 |
| `swift/config/zero*.json` | DeepSpeed ZeRO 各档预设配置。 |
| `swift/model/patcher.py` | `patch_mp_ddp`：猴子补丁让 accelerate 支持「device_map + DDP」。 |
| `swift/trainers/mixin.py` | MP+DDP 下优化器状态设备错位的修补。 |
| `examples/yaml/deepspeed/` | 多卡 DeepSpeed 训练 + 推理的现成示例。 |

---

## 4. 核心概念与源码讲解

### 4.1 torchrun 多卡启动

#### 4.1.1 概念说明

ms-swift 的 `swift` 命令本身**不做训练**，它是一个「发射器」（见 u1-l4）。在多卡场景下，发射器要多做一件事：判断要不要在训练脚本前面套一个 `torchrun`。

判定的总开关是两个环境变量：

- `NPROC_PER_NODE`：单机几张卡（每张卡一个进程）。
- `NNODES`：一共几台机器。

只要这两者任一被设置，ms-swift 就认为「用户想要分布式」，从而把命令改写成 `python -m torch.distributed.run ...` 的形式。注意：**`NPROC_PER_NODE` 是环境变量，不是 `swift` 的命令行参数**——这是初学者最常踩的坑，写错成 `--nproc_per_node` 是不会生效的。

一个关键的「子命令白名单」约束：只有 `pt / sft / rlhf / infer`（以及 megatron 系列）会真正套 torchrun；`export / eval / deploy` 等即便你设了 `NPROC_PER_NODE` 也会被忽略（这些命令的多卡走另外的路径，比如 u8-l3 的临时 deploy 服务）。

#### 4.1.2 核心流程

```
用户执行: NPROC_PER_NODE=2 swift sft config.yaml
        │
        ▼
cli_main() 取子命令 method_name = 'sft'
        │
        ▼
parse_yaml_args(argv)        # 先把 yaml 展开成 --key value（见 u2-l2）
        │
        ▼
use_torchrun()               # 读 NPROC_PER_NODE / NNODES → True
        │
        ▼
get_torchrun_args()          # 收集 5 个分布式环境变量
        │
        ▼
判定 method_name ∈ {pt,sft,rlhf,infer} 且 use_torchrun()？
        │  是
        ▼
args = [python, '-m', 'torch.distributed.run',
        '--nproc_per_node', '2', ...其它..., file_path, *argv]
        │
        ▼
print(f'run sh: ...')        # 这行是排查问题的第一线索
subprocess.run(args)         # 真正把训练交给 torchrun 拉起
```

torchrun 起来后，会在每个进程中注入 `RANK` / `LOCAL_RANK` / `WORLD_SIZE` / `LOCAL_WORLD_SIZE` 等环境变量，下游的 `get_dist_setting()` 正是去读这些变量。

#### 4.1.3 源码精读

判定是否要走 torchrun 的总开关，靠读两个环境变量：

[swift/cli/main.py:L30-L35](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/cli/main.py#L30-L35) —— `use_torchrun` 只要发现 `NPROC_PER_NODE` 或 `NNODES` 被设置就返回 `True`。

收集 torchrun 需要的参数（注意都来自环境变量，且全大写）：

[swift/cli/main.py:L74-L83](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/cli/main.py#L74-L83) —— `get_torchrun_args` 把 `NPROC_PER_NODE` / `MASTER_PORT` / `NNODES` / `NODE_RANK` / `MASTER_ADDR` 拼成 `--nproc_per_node 2` 之类的参数。

真正决定命令形态的「分叉路口」：

[swift/cli/main.py:L95-L98](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/cli/main.py#L95-L98) —— 若不需要 torchrun，直接 `python file_path argv`；否则套 `python -m torch.distributed.run <torchrun_args> file_path argv`。条件里的 `method_name not in {'pt', 'sft', 'rlhf', 'infer'}` 正是上文说的子命令白名单。

下游如何消费 torchrun 注入的环境变量：

[swift/utils/env.py:L27-L34](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/utils/env.py#L27-L34) —— `get_dist_setting` 返回 `(rank, local_rank, world_size, local_world_size)` 四元组，是整个 swift 包里判断「我是不是分布式、我是第几个进程」的统一入口。

[swift/utils/env.py:L58-L61](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/utils/env.py#L58-L61) —— `is_dist` 用 `rank >= 0 and local_rank >= 0` 判断是否处于分布式（torchrun 总会把它们设成 ≥0，单进程时是 -1）。

[swift/utils/env.py:L48-L50](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/utils/env.py#L48-L50) —— `is_master` 判断当前进程是否为主进程（rank=0），用于「只有主进程落盘 checkpoint / 打日志」的防重复。

多机场景的拓扑：

[swift/utils/env.py:L37-L40](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/utils/env.py#L37-L40) —— `get_node_setting` 读 `NODE_RANK`（我是第几台机器）与 `NNODES`（一共几台），多机训练时必填。

参数对象在构造期就把分布式信息固化下来：

[swift/arguments/base_args/base_args.py:L183-L185](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/arguments/base_args/base_args.py#L183-L185) —— `BaseArguments.__post_init__` 调用 `get_dist_setting()`，把 `rank/local_rank/global_world_size/local_world_size` 写进 args，后续模板、数据集、trainer 都直接读这四个字段。

最后，每个进程要把自己的 GPU 绑定好：

[swift/arguments/base_args/base_args.py:L315-L317](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/arguments/base_args/base_args.py#L315-L317) —— `_init_device` 在分布式时调用 `set_device()`。

[swift/utils/torch_utils.py:L159-L165](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/utils/torch_utils.py#L159-L165) —— `set_device` 把当前进程绑到 `local_rank` 对应的卡上，这是「0 号进程用 0 号卡、1 号进程用 1 号卡」的根源。

> 补充：sft 训练时实际的 `dist.init_process_group`（NCCL 通信域初始化）由 HF Trainer（经 accelerate）在内部完成，torchrun 已经把环境变量备齐；而 infer / export 则在各自 args 里显式调用 `init_process_group`（如 `swift/arguments/infer_args.py`、`swift/arguments/export_args.py`）。

#### 4.1.4 代码实践

**实践目标**：在不真正训练的前提下，验证 `swift sft` 在多卡下被改写成了 torchrun 命令，并看清进程拓扑。

**操作步骤**：

1. 进入项目根目录，确认已按 u1-l2 安装好 swift。
2. 直接用现成示例配置，故意把 `NPROC_PER_NODE` 设为 2，但**不真正跑训练**——我们只看 `run sh:` 这一行打印：

```bash
# 注意：这是示例命令，请勿在无 GPU 环境真正执行训练
NPROC_PER_NODE=2 CUDA_VISIBLE_DEVICES=0,1 \
swift sft --model Qwen/Qwen2.5-0.5B --dataset swift/self-cognition#5 --output_dir /tmp/dist_test --max_length 512
```

3. 观察终端第一行打印的 `run sh: ...`，会看到类似：

```
run sh: `/path/to/python -m torch.distributed.run --nproc_per_node 2 --master_port ... /path/to/swift/cli/sft.py --model Qwen/Qwen2.5-0.5B ...`
```

4. 对照源码：确认 `--nproc_per_node 2` 正是 `get_torchrun_args()` 从 `NPROC_PER_NODE` 翻译来的；`-m torch.distributed.run` 正是 `cli_main` 的分叉分支。

**需要观察的现象**：

- 第一行的 `run sh:` 已变成 `python -m torch.distributed.run ...` 形态。
- 日志中会出现两行 `rank: 0, local_rank: 0, world_size: 2, local_world_size: 2` 和 `rank: 1, local_rank: 1, ...`（对应 [base_args.py:L184-L185](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/arguments/base_args/base_args.py#L184-L185)）。

**预期结果**：`run sh:` 与源码逻辑一致即算通过。

**待本地验证**：实际能否拉起 2 进程取决于机器是否真有 2 张可见 GPU；若只有 1 张卡，把 `NPROC_PER_NODE` 改成 1 也能看到「不套 torchrun」的分支（`use_torchrun` 仍可能为 True，但只有单进程）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `swift export --deepspeed zero2 ...` 设了 `NPROC_PER_NODE=2` 也不会走 torchrun？

**参考答案**：因为 [main.py:L95](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/cli/main.py#L95) 的条件里有 `method_name not in {'pt', 'sft', 'rlhf', 'infer'}`，`export` 不在白名单内，即便 `use_torchrun()` 为真也会走「直接 python」分支。

**练习 2**：多机训练时，用户除了设 `NPROC_PER_NODE` 还要设哪些环境变量？

**参考答案**：`NNODES`（总机器数）、`NODE_RANK`（当前机器编号）、`MASTER_ADDR`（主节点 IP）、`MASTER_PORT`（通信端口）。它们都经 [get_torchrun_args](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/cli/main.py#L74-L83) 透传给 torchrun。

---

### 4.2 DeepSpeed ZeRO 配置

#### 4.2.1 概念说明

DeepSpeed ZeRO（Zero Redundancy Optimizer）是对「数据并行的冗余」开刀：标准 DDP 每张卡都完整复制了参数、梯度、优化器状态，ZeRO 把这三类东西按需切分，从而大幅降低单卡显存。

设模型有 Ψ 个参数、N 张卡、用混合精度 Adam（优化器状态 12Ψ 字节、梯度 2Ψ、参数 2Ψ，单卡基线 16Ψ），ZeRO 三档的显存近似为：

\[
\text{ZeRO-1（切优化器状态）}: 2\Psi + 2\Psi + \frac{12\Psi}{N}
\]

\[
\text{ZeRO-2（切优化器状态 + 梯度）}: 2\Psi + \frac{14\Psi}{N}
\]

\[
\text{ZeRO-3（连参数一起切）}: \frac{16\Psi}{N}
\]

直觉上：ZeRO-1/2 每张卡仍持有完整参数（前向反向不用频繁通信，速度快），ZeRO-3 显存最优但每层前向反向都要 all-gather 收集参数（通信更多、较慢）。因此工程上有一条经验法则——**显存够就用 zero2，显存不够才上 zero3；显存再不够就 zero3_offload 把优化器状态甚至参数卸载到 CPU**。

ms-swift 把这套配置做了「简写映射」：用户写 `--deepspeed zero2`，框架自动替换成仓库内的 `swift/config/zero2.json`，避免每个人手写一大段 JSON。

#### 4.2.2 核心流程

```
用户: --deepspeed zero2
        │
        ▼
SftArguments.__post_init__ → _init_deepspeed()
        │
        ├─ require_version('deepspeed')          # 没装 deepspeed 直接报错
        │
        ├─ 互斥校验: is_mp() 且非 ray → 报错
        │   （device_map 模型并行与 ZeRO 不兼容）
        │
        ├─ 简写映射: 'zero2' → swift/config/zero2.json
        │
        ├─ json_parse_to_dict → self.deepspeed = dict
        │
        ├─ (可选) 注入 zero_hpz_partition_size (ZeRO++)
        ├─ (可选) 注入 deepspeed_autotp_size (AutoTP)
        └─ logger.info(f'Using deepspeed: {self.deepspeed}')
        │
        ▼
该 dict 最终交给 HF Trainer（accelerate）→ DeepSpeed 引擎按 stage 切分
```

#### 4.2.3 源码精读

`_init_deepspeed` 是 DeepSpeed 接入的总入口：

[swift/arguments/sft_args.py:L251-L282](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/arguments/sft_args.py#L251-L282) —— 完整逻辑：版本校验 → 与 device_map 互斥校验 → 简写映射 → 解析成 dict → 可选注入 ZeRO++/AutoTP。

互斥校验（很重要，避免用户踩坑）：

[swift/arguments/sft_args.py:L254-L257](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/arguments/sft_args.py#L254-L257) —— 当 `is_mp()` 为真（即每进程多卡、device_map 模型并行）且未用 ray 时，同时开 DeepSpeed 会直接 `raise ValueError`。

简写映射表：

[swift/arguments/sft_args.py:L260-L267](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/arguments/sft_args.py#L260-L267) —— 把 `zero0/zero1/zero2/zero3/zero2_offload/zero3_offload` 六个简写替换为 `swift/config/` 下对应 JSON 文件路径。

现在对照真实配置文件，看 zero2 与 zero3 的差异（这是本模块的核心知识点）：

[swift/config/zero2.json:L15-L27](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/config/zero2.json#L15-L27) —— zero2 的 `zero_optimization.stage=2`，`offload_optimizer.device="none"`（不卸载到 CPU），并启用 `allgather_partitions / reduce_scatter` 等通信优化。

[swift/config/zero3.json:L15-L36](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/config/zero3.json#L15-L36) —— zero3 多了 `offload_param`（参数也可卸载）、`stage3_*_bucket_size` 等分片参数，并设 `stage3_gather_16bit_weights_on_model_save=true`——这正是 ZeRO-3 保存权重时必须「临时聚合」的体现（否则每张卡只有 1/N 的参数，存不全）。

CPU 卸载版本的差异：

[swift/config/zero2_offload.json:L17-L20](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/config/zero2_offload.json#L17-L20) —— 与 zero2 唯一关键区别：`offload_optimizer.device="cpu"`，把 Adam 状态挪到内存，省显存但更慢。

[swift/config/zero3_offload.json:L17-L24](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/config/zero3_offload.json#L17-L24) —— 同时把优化器状态和参数都卸载到 CPU，是显存最省、速度最慢的档位。

> 这六个 JSON 里大量字段是 `"auto"`（如 `train_batch_size` / `gradient_accumulation_steps` / `bf16.enabled`），意思是「交给 HF Trainer 用命令行参数自动填充」，这就是为什么你不需要在 deepspeed 配置里再写一遍 batch size。

#### 4.2.4 代码实践

**实践目标**：跑一个 `NPROC_PER_NODE=2 + deepspeed zero2` 的多卡 LoRA SFT，记录显存与速度，再换 `zero3` 对比。

**操作步骤**：项目已内置示例，直接用：

```bash
# 示例命令，需 2 张可见 GPU，待本地验证
cd examples/yaml/deepspeed
NPROC_PER_NODE=2 CUDA_VISIBLE_DEVICES=0,1 \
swift sft examples/yaml/deepspeed/sft.yaml
```

该 YAML 的关键行（末尾 `deepspeed: zero2`）：

[examples/yaml/deepspeed/sft.yaml:L36](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/examples/yaml/deepspeed/sft.yaml#L36) —— 一行 `deepspeed: zero2` 即触发 4.2.2 的整套展开流程。

启动脚本：

[examples/yaml/deepspeed/sft.sh:L1-L3](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/examples/yaml/deepspeed/sft.sh#L1-L3) —— `NPROC_PER_NODE=2` + `CUDA_VISIBLE_DEVICES=0,1` 是标准两卡写法。

**需要观察的现象**：

1. 启动日志出现 `Using deepspeed: {'zero_optimization': {'stage': 2, ...}, ...}`，确认简写已被展开成 dict。
2. 日志中出现两条 `rank: 0 ...` / `rank: 1 ...`，确认两个进程都进入了 DeepSpeed 引擎。
3. 训练时 `nvidia-smi` 上两张卡的显存基本对称（zero2 把优化器状态 + 梯度均分）。

**对比实验**：把 `sft.yaml` 里的 `deepspeed: zero2` 改成 `zero3` 再跑一次，观察：

- 显存：zero3 单卡峰值应**低于** zero2（参数也被切分）。
- 速度：zero3 的每步耗时通常**高于** zero2（频繁 all-gather 参数）。
- 保存 checkpoint：zero3 因 `stage3_gather_16bit_weights_on_model_save=true`，保存时会有一段额外的参数聚合时间。

**预期结果**：能用一张小表记录「zero2 / zero3 / zero3_offload」三档的「单卡峰值显存 / 每步秒数」，验证 §4.2.1 的经验法则。

**待本地验证**：显存与速度的具体数字取决于 GPU 型号、模型大小、batch、序列长度，本讲无法给出绝对值。

#### 4.2.5 小练习与答案

**练习 1**：同样 2 卡训 7B，为什么 zero3 比 zero2 更省显存却通常更慢？

**参考答案**：zero3 把参数也切分到各卡（显存从 \(2\Psi + 14\Psi/N\) 降到 \(16\Psi/N\)），但前向反向每一层都需要 all-gather 把参数临时聚合回来，通信量更大；zero2 每卡常驻完整参数，前向反向无需聚合参数，通信更少故更快。

**练习 2**：用户传 `--deepspeed zero3`，源码里 `self.deepspeed` 最终变成什么类型？

**参考答案**：先是字符串 `'zero3'`，经 [简写映射](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/arguments/sft_args.py#L260-L267) 替换为 `swift/config/zero3.json` 路径，再经 `json_parse_to_dict` 变成 `dict`，最终是解析后的字典配置。

**练习 3**：如果同时设了 `--device_map auto` 和 `--deepspeed zero2`，会发生什么？

**参考答案**：`_init_deepspeed` 检测到 `is_mp()` 为真会 `raise ValueError('DeepSpeed is not compatible with device_map ...')`（[sft_args.py:L255-L257](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/arguments/sft_args.py#L255-L257)）。

---

### 4.3 FSDP/device_map 并行

#### 4.3.1 概念说明

除了 DeepSpeed ZeRO，PyTorch 生态还有两条并行的路：

1. **FSDP（Fully Sharded Data Parallel）**：PyTorch 官方的「全分片数据并行」，思路与 ZeRO-3 几乎一致（把参数/梯度/优化器状态都切分）。ms-swift 当前接入的是 **FSDP2**（新版，基于 DTensor/`fully_shard`），通过 `--fsdp fsdp2` 开启。它和 DeepSpeed 是**二选一**的关系。

2. **device_map（朴素模型并行）**：来自 transformers 的 `from_pretrained(device_map=...)`，把模型按层映射到不同卡上。它**不是**数据并行——数据仍是一份，只是模型被切片了。它的典型用途是「单卡放不下、但又懒得/不能用 ZeRO」的场景（如加载大模型做 LoRA）。

ms-swift 还支持一种少见的混用 **MP+DDP**：用 `device_map` 把模型切片到「每进程多张卡」，同时用 DDP 在多个进程间做数据并行。这需要框架对 accelerate 做猴子补丁才能跑通。

三者的互斥/兼容关系（源码强制）：

| 组合 | 是否允许 | 判定位置 |
| --- | --- | --- |
| DeepSpeed + device_map（is_mp） | ❌ 报错 | `_init_deepspeed` |
| FSDP2 + device_map（is_mp） | ❌ 报错 | `_init_fsdp` |
| FSDP2 + DeepSpeed | ❌ 报错 | `_init_fsdp` |
| device_map（MP） + DDP | ✅（需补丁） | `patch_mp_ddp` |

#### 4.3.2 核心流程

**device_map 的判定（is_mp）**：

```
get_dist_setting() → local_world_size（每进程的卡数 = NPROC_PER_NODE）
get_device_count() → n_gpu（CUDA_VISIBLE_DEVICES 里的总卡数）
        │
        ▼
is_mp(): n_gpu // local_world_size >= 2 ？
        │  是（每进程分到 ≥2 张卡） → device_map 模型并行
        ▼
is_mp_ddp(): is_dist() and is_mp() and world_size > 1 ？
        │  是 → 进入 MP+DDP 补丁路径
```

**device_map 字段的处理**：

```
用户 --device_map auto (或 JSON/dict)
        │
        ▼
_init_device_map():
  ├─ json_parse_to_dict 解析
  └─ MP+DDP 适配: local_rank>0 时，把每个设备 id 加上 local_rank 偏移
        │
        ▼
get_model_kwargs() 打包 device_map → from_pretrained(device_map=...)
```

**FSDP2 的处理**：

```
用户 --fsdp fsdp2
        │
        ▼
_init_fsdp():
  ├─ 互斥校验（device_map / DeepSpeed）
  ├─ 简写 'fsdp2' → swift/config/fsdp2.json
  ├─ 拆出 fsdp 字符串（如 'full_shard auto_wrap offload'）
  ├─ 拆出 fsdp_config dict
  ├─ 设 FSDP_VERSION=2 环境变量
  └─ _check_fsdp2_compatibility（save_only_model / gradient_checkpointing 兼容性）
```

#### 4.3.3 源码精读

先看 `is_mp` 的精确判定：

[swift/utils/env.py:L64-L75](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/utils/env.py#L64-L75) —— 当 `n_gpu // local_world_size >= 2`（每进程分到 ≥2 张卡）时返回 True。例如 `CUDA_VISIBLE_DEVICES=0,1,2,3` + `NPROC_PER_NODE=2`，每进程 2 张卡 → is_mp 为真。

[swift/utils/env.py:L78-L84](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/utils/env.py#L78-L84) —— `is_mp_ddp` 在 is_mp 基础上再要求 `is_dist() and world_size > 1`，即「模型并行 + 多进程数据并行」同时存在。

`device_map` 字段定义与处理：

[swift/arguments/base_args/model_args.py:L86](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/arguments/base_args/model_args.py#L86) —— `device_map` 字段，接受 `'auto'` / `'cpu'` / dict / JSON 字符串。

[swift/arguments/base_args/model_args.py:L95-L104](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/arguments/base_args/model_args.py#L95-L104) —— `_init_device_map` 的 MP+DDP 适配：当 `local_world_size > 1 and local_rank > 0` 时，把 dict 里每个整型设备 id 加上 `local_rank` 偏移。这是为了让「1 号进程不要和 0 号进程抢同一组卡」——0 号进程用卡 {0,1}，1 号进程自动偏移到用卡 {2,3}。

device_map 如何流到模型加载：

[swift/arguments/base_args/model_args.py:L237](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/arguments/base_args/model_args.py#L237) —— `get_model_kwargs()` 把 `device_map` 打包，最终交给 transformers 的 `from_pretrained`，由 accelerate 的 `infer_auto_device_map` 计算每层放哪张卡。

FSDP2 的接入与校验：

[swift/arguments/sft_args.py:L284-L329](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/arguments/sft_args.py#L284-L329) —— `_init_fsdp` 完整流程：互斥校验、简写映射、拆分 fsdp/fsdp_config、设环境变量、兼容性检查。注意 [L293-L294](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/arguments/sft_args.py#L293-L294) 明确禁止 FSDP2 与 DeepSpeed 同时使用。

[swift/arguments/sft_args.py:L331-L359](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/arguments/sft_args.py#L331-L359) —— `_check_fsdp2_compatibility` 提醒：FSDP2 下 `gradient_checkpointing` 应改用 `fsdp_config.activation_checkpointing`，是 FSDP2 与传统用法的细微差异。

MP+DDP 的核心补丁（让 accelerate 支持这种混用）：

[swift/model/patcher.py:L445-L473](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/model/patcher.py#L445-L473) —— `patch_mp_ddp` 用猴子补丁替换 accelerate 的 `infer_auto_device_map`，关键改动是 `device_ids = list(range(local_rank, n_gpu, local_world_size))`——让每个 DDP 进程只在自己的那组卡上分配层，从而 DDP 包裹的「每个进程的模型切片」结构一致。

trainer 侧对 MP+DDP 的修补：

[swift/trainers/mixin.py:L292-L299](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/trainers/mixin.py#L292-L299) —— MP+DDP 下 Adam 的 `step` 张量可能落在不同设备上导致报错，这里检测到设备错位时把 `step` 挪到 CPU，是 MP+DDP 能稳定训练的「最后一公里」补丁。

#### 4.3.4 代码实践

**实践目标**：源码阅读型实践——根据 `CUDA_VISIBLE_DEVICES` 与 `NPROC_PER_NODE` 的不同组合，预测 `is_mp` / `is_mp_ddp` 的取值，理解何时进入 device_map 路径。

**操作步骤**：

1. 用 Python 直接调用 swift 的工具函数（不训练）：

```python
# 示例代码：手动设环境变量后查询分布式判定
import os
from swift.utils import is_mp, is_mp_ddp, get_dist_setting

def probe(nproc, visible, rank=0, local_rank=0, world_size=None):
    os.environ['NPROC_PER_NODE'] = str(nproc)   # 仅用于触发 use_torchrun，不影响 get_dist_setting
    os.environ['CUDA_VISIBLE_DEVICES'] = visible
    # 模拟 torchrun 注入的变量
    os.environ['RANK'] = str(rank)
    os.environ['LOCAL_RANK'] = str(local_rank)
    os.environ['WORLD_SIZE'] = str(world_size if world_size else nproc)
    os.environ['LOCAL_WORLD_SIZE'] = str(nproc)
    print(f'nproc={nproc}, visible={visible}, n_gpu={len(visible.split(","))}, '
          f'dist={get_dist_setting()}, is_mp={is_mp()}, is_mp_ddp={is_mp_ddp()}')

probe(2, '0,1')        # 纯 DDP
probe(2, '0,1,2,3')    # MP+DDP：每进程 2 卡
probe(1, '0,1')        # 纯 device_map 模型并行（单进程 2 卡）
```

2. 在能 `import swift` 的环境运行，对照源码核对结果。

**需要观察的现象与预期结果**：

| 调用 | n_gpu | local_world_size | is_mp | is_mp_ddp | 含义 |
| --- | --- | --- | --- | --- | --- |
| `probe(2,'0,1')` | 2 | 2 | False | False | 纯 DDP，每进程 1 卡 |
| `probe(2,'0,1,2,3')` | 4 | 2 | True | True | MP+DDP，每进程 2 卡 |
| `probe(1,'0,1')` | 2 | 1 | True | False | 纯 device_map（单进程） |

核对依据：[is_mp](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/utils/env.py#L64-L75) 是 `n_gpu // local_world_size >= 2`；[is_mp_ddp](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/utils/env.py#L78-L84) 还要求 `is_dist() and world_size > 1`。

**待本地验证**：`is_mp` 内部调用 `get_device_count()`，真实 GPU 数以 `torch.cuda.device_count()` 为准；上表假设 `CUDA_VISIBLE_DEVICES` 全部对应真实可见设备。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `_init_device_map` 在 `local_rank > 0` 时要给 device_map 里的设备 id 加 `local_rank` 偏移？

**参考答案**：MP+DDP 下每个进程都各自做模型并行，若所有进程都用同一组卡，就会互相抢占。加偏移让 0 号进程用卡 {0,1}、1 号进程用 {2,3}，保证各进程占用的物理卡不重叠（[model_args.py:L101-L104](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/arguments/base_args/model_args.py#L101-L104)）。

**练习 2**：`--fsdp fsdp2` 和 `--deepspeed zero3` 都做了「全分片」，ms-swift 允许同时开吗？

**参考答案**：不允许。`_init_fsdp` 会 `raise ValueError('FSDP2 is not compatible with DeepSpeed.')`（[sft_args.py:L293-L294](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/arguments/sft_args.py#L293-L294)）。两者功能重叠，择一即可。

**练习 3**：什么场景下你会优先选 `device_map` 而不是 DeepSpeed？

**参考答案**：当只是想「把一个放不下的模型加载进来做 LoRA」、且不想引入 DeepSpeed 的复杂配置与通信开销时，`device_map auto` 最省事；它本质是模型并行，不切分优化器状态，适合参数量略超单卡显存的场景。需要全量微调大模型时才该用 ZeRO/FSDP。

---

## 5. 综合实践

把三个最小模块串起来：**为一个 7B 模型的 LoRA 微调，设计分布式训练方案并解释每个选择的源码依据。**

任务背景：你只有一台 2×24G（如 4090）机器，要对 `Qwen/Qwen2.5-7B-Instruct` 做 LoRA 微调。

请按下列步骤完成（含源码阅读 + 待本地验证的运行部分）：

1. **拓扑设计**：写出启动命令的环境变量组合（`NPROC_PER_NODE` / `CUDA_VISIBLE_DEVICES`），并据此推断 [get_dist_setting()](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/utils/env.py#L27-L34) 返回的四元组、[is_mp()](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/utils/env.py#L64-L75) 的值。

   - 参考答案：`NPROC_PER_NODE=2 CUDA_VISIBLE_DEVICES=0,1`，返回 `(rank=0/1, local_rank=0/1, world_size=2, local_world_size=2)`，`n_gpu=2`、`is_mp()=False`（2//2=1，不 ≥2）→ 走纯 DDP 路径。

2. **配置选择**：24G 卡放 7B + LoRA 偏紧，应选哪一档 DeepSpeed？给出源码依据。

   - 参考答案：选 `zero2`（[_init_deepspeed 简写映射](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/arguments/sft_args.py#L260-L267)）。LoRA 可训参数很少，主要省的是优化器状态对基座梯度的冗余；若仍 OOM 再降到 `zero2_offload`（[zero2_offload.json](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/config/zero2_offload.json#L17-L20) 把优化器卸载到 CPU）。

3. **编写 YAML**：基于 [examples/yaml/deepspeed/sft.yaml](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/examples/yaml/deepspeed/sft.yaml)，把模型换成 7B、`deepspeed` 设为 `zero2`，其余参数自行调整。

4. **运行并记录**（待本地验证）：执行 `NPROC_PER_NODE=2 CUDA_VISIBLE_DEVICES=0,1 swift sft your.yaml`，记录：
   - `run sh:` 行是否含 `-m torch.distributed.run --nproc_per_node 2`（验证 4.1）。
   - `Using deepspeed: {... 'stage': 2 ...}`（验证 4.2）。
   - 两卡显存峰值与每步耗时。

5. **对比实验**（待本地验证）：把 `zero2` 换成 `zero3` 再跑，按 §4.2.1 的公式预期「显存降、速度降」，用实测验证。

6. **写一份小结**：用 3 句话回答——「在 ms-swift 里，从敲下 `swift sft` 到 DeepSpeed 引擎真正切分参数，中间经过了哪几个关键函数？」（提示：`cli_main` → `get_dist_setting` → `_init_deepspeed` → HF Trainer/accelerate → DeepSpeed 引擎）。

---

## 6. 本讲小结

- `swift` 命令是「发射器」：是否套 `torchrun` 由 `NPROC_PER_NODE` / `NNODES` 环境变量经 [use_torchrun](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/cli/main.py#L30-L35) 决定，且只有 `pt/sft/rlhf/infer` 走多进程分支。
- 进程拓扑统一由 [get_dist_setting()](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/utils/env.py#L27-L34) 从 torchrun 注入的环境变量解析，`is_dist` / `is_master` / `is_mp` 都建立在它之上。
- DeepSpeed 接入集中在 [_init_deepspeed](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/arguments/sft_args.py#L251-L282)：它把 `zero2/zero3/...` 简写展开成 `swift/config/` 下的 JSON dict，并与 `device_map` 互斥。
- ZeRO 三档遵循「显存换速度」的连续谱：zero2（参数常驻、最快）→ zero3（参数也切分、更省）→ *_offload（卸载到 CPU、最省最慢），公式见 §4.2.1。
- FSDP2（[_init_fsdp](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/arguments/sft_args.py#L284-L329)）是 ZeRO-3 的 PyTorch 原生替代，与 DeepSpeed、device_map 都互斥。
- `device_map` 是朴素的按层模型并行；ms-swift 用 [patch_mp_ddp](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/swift/model/patcher.py#L445-L473) 让它能与 DDP 混用，并在 trainer 里修补优化器设备错位。

---

## 7. 下一步学习建议

本讲解决的是「数据并行家族 + 朴素模型并行」的启动与配置。当你需要更强的并行能力时，继续往下读：

- **u9-l2 序列并行**：当序列长度（长文本/长视频）成为显存瓶颈，DDP/ZeRO 都帮不上时，学 `sequence_parallel` 模块如何用 Ulysses / Ring-Attention 把单条序列切到多卡。
- **u9-l3 / u9-l4 Megatron-SWIFT**：当需要张量并行（TP）/ 流水并行（PP）/ 专家并行（EP）——也就是 GPT 级别预训练用的那套——时，转向 `swift/megatron`，理解 mcore-bridge 如何让 Megatron 像 transformers 一样易用。
- **u9-l5 Ray 分布式调度**：当训练跨多机、或需要异步 RL（GRPO rollout 与训练分离）时，学 `ray_utils` 如何把单机代码透明地变成 Ray actor。
- **延伸阅读**：[examples/yaml/deepspeed/](https://github.com/modelscope/ms-swift/blob/3d61b9318b27fdd5659e530cd36db7f4ce740fd7/examples/yaml/deepspeed/) 的现成示例，以及 `swift/config/` 下 `zero0/zero1/zero2/zero2_offload/zero3/zero3_offload/fsdp2` 七个配置文件，对照本讲逐行读懂每个字段。
