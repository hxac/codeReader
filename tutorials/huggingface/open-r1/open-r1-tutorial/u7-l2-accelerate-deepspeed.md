# Accelerate 与 DeepSpeed 配置

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清楚 open-r1 提供的四个 accelerate 配置（`ddp` / `zero2` / `zero3` / `fsdp`）分别把模型状态分片到什么粒度，各自的显存与通信代价。
- 读懂 `slurm/train.slurm` 如何用 `--accelerator` 选择配置，并用命令行覆盖 YAML 里的 `num_processes` / `num_machines` / `machine_rank` 等字段。
- 给定一个模型规模（例如 32B），判断该选哪种配置；并理解 open-r1 在真实 32B 训练（OlympicCoder-32B）中实际为何改用了 FSDP。
- 理解 `bf16` 混合精度与 DeepSpeed 的 `zero3_init_flag` / `zero3_save_16bit_model` 等关键字段的含义。

## 2. 前置知识

在进入源码前，先用最朴素的方式建立几个概念。

### 2.1 一张 GPU 装不下「整个训练状态」时怎么办

训练一个大模型时，单卡显存里至少要同时放下四类东西：

| 成分 | 大小（Φ 为参数量） | 说明 |
|---|---|---|
| 模型参数（权重） | 约 \(2\Phi\) 字节（bf16） | 前向推理必备 |
| 梯度 | 约 \(2\Phi\) 字节（bf16） | 反向传播必备 |
| 优化器状态（Adam） | 约 \(12\Phi\) 字节 | fp32 主权重 + 一阶矩 + 二阶矩 |
| 激活值 | 随 batch/序列长度增长 | 前向保留、反向重算用 |

加起来，光「模型相关状态」就约 \(16\Phi\) 字节。对一个 32B 模型（\(\Phi = 32 \times 10^9\)），这就是约 512 GB——远超单张 80 GB H100 的容量。**所以大模型训练的核心问题不是「算得快」，而是「先装得下」。**

### 2.2 三种「分摊」思路

- **数据并行（DDP）**：每张卡都装一份**完整**的模型副本，只把数据切片。简单、通信少，但要求单卡能装下整套状态，只能用于小模型。
- **分片数据并行（ZeRO / FSDP）**：把模型参数、梯度、优化器状态**切碎分散到多张卡**，用到时再临时拼起来。分得越细，单卡越省，但通信越多。
- **混合精度（bf16）**：用 16 位浮点做前向/反向，节省一半参数显存，同时保持 fp32 主权重稳定训练。

本讲的四个 YAML 配置，本质就是在「分片到哪一级」与「分得多少」之间做不同取舍。

### 2.3 前置讲义承接

本讲承接 [u7-l1](u7-l1-slurm-vllm-training.md)：那里讲到 `train.slurm` 用 `srun`（外层派发进程）+ `accelerate launch`（内层组建分布式进程组）启动训练。本讲就专门拆解那条 `accelerate launch --config_file ...` 里指向的 YAML 配置到底写了什么、意味着什么。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [recipes/accelerate_configs/ddp.yaml](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/recipes/accelerate_configs/ddp.yaml) | 最朴素的 DDP 配置：每卡完整副本 |
| [recipes/accelerate_configs/zero2.yaml](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/recipes/accelerate_configs/zero2.yaml) | DeepSpeed ZeRO-2：分片优化器状态 + 梯度 |
| [recipes/accelerate_configs/zero3.yaml](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/recipes/accelerate_configs/zero3.yaml) | DeepSpeed ZeRO-3：再额外分片参数 |
| [recipes/accelerate_configs/fsdp.yaml](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/recipes/accelerate_configs/fsdp.yaml) | PyTorch 原生 FSDP：FULL_SHARD（等价于 ZeRO-3 风格的全分片） |
| [slurm/train.slurm](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/slurm/train.slurm) | 通过 `--accelerator` 选择上述配置，并用命令行覆盖多机参数 |
| [recipes/README.md](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/recipes/README.md) | 记录了 7B 用 zero3、32B 用 fsdp 的真实选择 |

## 4. 核心概念与源码讲解

### 4.1 Accelerate 配置文件的角色：谁读它、何时生效

#### 4.1.1 概念说明

一个容易踩的坑：这四个 YAML 文件**根本不被 open-r1 的 Python 代码读取**。`sft.py` / `grpo.py` 里没有任何一行去 `load` 它们（`src/open_r1` 中唯一出现 `accelerate_configs` 字样的地方，是 `sft.py` 头部文档字符串里的一段示例命令，见 [src/open_r1/sft.py:21](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/sft.py#L21)）。

真正消费这些 YAML 的是 **🤗 Accelerate 的命令行 `accelerate launch`**。它在你的 Python 脚本启动**之前**就完成了：

1. 解析 YAML，确定分布式策略（DDP / DeepSpeed / FSDP）；
2. 在每张卡上 fork 出一个进程，建立进程组（NCCL）；
3. 若是 DeepSpeed，则构造 DeepSpeed 引擎；若是 FSDP，则给模型套上分片包装器。

之后 Hugging Face 的 `Trainer`（TRL 的 `SFTTrainer` / `GRPOTrainer` 继承自它）会**自动探测**当前 Accelerate 状态，据此决定如何保存 checkpoint、如何同步梯度。所以「写 YAML」和「写训练脚本」是两条独立的线，YAML 是运行环境的说明书，不是业务逻辑。

#### 4.1.2 核心流程

整条链路（承接 u7-l1）如下：

```
sbatch ... --accelerator <名>
        │
        ▼
train.slurm: --config_file recipes/accelerate_configs/<名>.yaml
             + 命令行覆盖 num_processes / num_machines / machine_rank
        │
        ▼
accelerate launch  ← 在此读取 YAML，搭建分布式环境
        │
        ▼
src/open_r1/<task>.py  ← 脚本本身对 YAML 一无所知
        │
        ▼
SFTTrainer / GRPOTrainer 自动探测并适配
```

关键点：YAML 提供**单机默认值**，而 Slurm 多机场景下，真正生效的是命令行的覆盖值。

#### 4.1.3 源码精读

`--accelerator` 的取值就是文件名（去掉 `.yaml`），见帮助文本与参数解析：

[slurm/train.slurm:19](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/slurm/train.slurm#L19) —— 帮助文本举例 `zero3`；

[slurm/train.slurm:63-66](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/slurm/train.slurm#L63-L66) —— 把 `--accelerator` 的值存进 `ACCELERATOR` 变量。

随后把它拼成路径并交给 `accelerate launch`，同时用命令行覆盖多机相关字段：

```bash
# slurm/train.slurm:150-161（精简，仅保留与本讲相关的行）
export LAUNCHER="... accelerate launch \
    --config_file recipes/accelerate_configs/$ACCELERATOR.yaml  \
    --gradient_accumulation_steps $GRAD_ACC_STEPS \
    --num_machines $NUM_NODES \           # 覆盖 YAML 的 num_machines
    --num_processes $WORLD_SIZE \          # 覆盖 YAML 的 num_processes
    --main_process_ip $MASTER_ADDR \
    --main_process_port $MASTER_PORT \
    --machine_rank $SLURM_PROCID \         # 覆盖 YAML 的 machine_rank
    --rdzv_backend=c10d \
    ..."
```

完整片段见 [slurm/train.slurm:150-161](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/slurm/train.slurm#L150-L161)。也就是说：四个 YAML 里写的 `num_processes: 8`、`num_machines: 1`、`machine_rank: 0` 只是「单机 8 卡」的默认场景；一旦走 Slurm 多机，这三个字段都会被 `--num_processes` / `--num_machines` / `--machine_rank` 覆盖。这是排错时最容易忽略的一点——**你改 YAML 的 `num_processes`，在 Slurm 下并不会生效**。

#### 4.1.4 代码实践

1. 实践目标：理解 YAML 的「默认值」与命令行「覆盖值」的关系。
2. 操作步骤：用 `accelerate launch` 的 `--help` 或 dry-run 方式，看它如何合并配置。可在本地执行：

   ```bash
   # 只打印将要执行的命令，不真正启动（accelerate 会读取 --config_file）
   ACCELERATE_LOG_LEVEL=info accelerate launch \
     --config_file recipes/accelerate_configs/zero3.yaml \
     --num_processes 2 --num_machines 1 --machine_rank 0 \
     --help 2>&1 | head -n 40
   ```

   再单独打开 `zero3.yaml` 对比 `num_processes`、`num_machines` 的值。
3. 需要观察的现象：`--num_processes 2` 是否体现在最终进程数上，而与 YAML 里的 `8` 无关。
4. 预期结果：命令行参数覆盖 YAML 默认值；YAML 中真正「不可被这条命令覆盖」的是 `deepspeed_config` / `fsdp_config` 这类策略块。
5. 若本地无 GPU 或 accelerate 未装，可改为纯文本阅读：把 `train.slurm` 第 150-161 行的变量替换成具体值（如 `NUM_NODES=2`、`WORLD_SIZE=16`），手写出最终的 `accelerate launch` 命令。这是「源码阅读型实践」，**待本地验证**。

#### 4.1.5 小练习与答案

**练习**：如果你把 `zero3.yaml` 里的 `num_processes` 从 8 改成 16，然后用 `sbatch ... --accelerator zero3` 在单节点 8 卡机器上跑，会发生什么？

**参考答案**：不会有任何改变。因为 `train.slurm` 在 `accelerate launch` 时用 `--num_processes $WORLD_SIZE` 覆盖了该字段，单节点 8 卡时 `WORLD_SIZE=8`。要真正改进程数，应改 Slurm 申请的 GPU 数或节点数，而不是改 YAML。

---

### 4.2 DDP 配置：完整副本的数据并行

#### 4.2.1 概念说明

DDP（DistributedDataParallel）是最朴素的并行：**每张卡都持有一份完整的模型、梯度、优化器状态**，只是各自处理不同的数据批次。反向传播后，所有卡用一次 all-reduce 把梯度平均。它的通信量最小、实现最稳，**代价是单卡必须能装下整套状态**——所以它只适合小模型。

#### 4.2.2 核心流程

```
每张卡：完整模型副本 + 完整优化器 + 完整梯度
   │
   ├── 前向：各自处理自己的 batch
   ├── 反向：各自算梯度
   └── all-reduce：把梯度跨卡求平均 → 所有卡梯度一致 → 同步优化器
```

关键性质：通信只在「梯度」这一步，且通信量与参数量成正比（一次 all-reduce），不含额外的参数分片通信。

#### 4.2.3 源码精读

[recipes/accelerate_configs/ddp.yaml:1-16](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/recipes/accelerate_configs/ddp.yaml#L1-L16) 全文很短，最关键的三行：

```yaml
distributed_type: MULTI_GPU   # 第 3 行：声明用 DDP
gpu_ids: all                  # 第 5 行：用本机所有 GPU
mixed_precision: bf16         # 第 8 行：bf16 混合精度
```

注意它**没有** `deepspeed_config` 块，也**没有** `fsdp_config` 块——`distributed_type: MULTI_GPU` 就是 DDP 的标志。`num_processes: 8`（第 10 行）是单机 8 卡默认值，可被命令行覆盖（见 4.1.3）。

#### 4.2.4 代码实践

1. 实践目标：直观感受 DDP「单卡装全套」的显存约束。
2. 操作步骤：
   - 估算：用本讲的公式，对一个 7B 模型（\(\Phi = 7\times10^9\)）算 DDP 下每卡「模型相关状态」约为多少 GB。
3. 需要观察的现象：单卡状态是否逼近甚至超过 80 GB。
4. 预期结果：\(16 \times 7 = 112\) GB > 80 GB。也就是说，**7B 模型在 DDP 下连「模型状态」都装不下**，更别提激活值——这正是 open-r1 对 7B 也默认用 `zero3`（见 4.4）的原因。
5. 这是纯估算实践，无需运行；若想实测，需多 GPU 环境，**待本地验证**。

#### 4.2.5 小练习与答案

**练习**：DDP 配置里完全没有 `deepspeed_config`，那 `bf16` 混合精度由谁负责实现？

**参考答案**：由 🤗 Accelerate 与 transformers 的自动混合精度（AMP）负责。`mixed_precision: bf16` 告诉 Accelerate 用 bf16 做前向/反向，transformers 的 `Trainer` 会据此设置 `torch.autocast`，与 DeepSpeed 无关。这也是为什么四个配置都能独立于 DeepSpeed 地使用 `bf16`。

---

### 4.3 DeepSpeed ZeRO-2 与 ZeRO-3：分片粒度的抉择

#### 4.3.1 概念说明

ZeRO（Zero Redundancy Optimizer）是 DeepSpeed 对「数据并行里的冗余」开刀的方案。它把训练状态分成三份可独立分片的资产，并按递进的三级分片：

| 级别 | 分片内容 | 每卡显存（约，N 卡） | 通信代价 |
|---|---|---|---|
| ZeRO-1 | 优化器状态 | \(14\Phi/N + 2\Phi\) | 与 DDP 相当 |
| ZeRO-2 | 优化器状态 + 梯度 | \(14\Phi/N + 2\Phi\)（参数仍完整） | 略多于 DDP |
| ZeRO-3 | 优化器状态 + 梯度 + 参数 | \(16\Phi/N\) | 显著增加（需 all-gather 参数） |

直觉记忆：**ZeRO-2 把「梯度和优化器」切了，但每张卡仍要装下完整模型权重；ZeRO-3 把「权重本身」也切了，所以最省显存，但每层前向/反向都要跨卡临时把权重 gather 回来，通信最重。**

对 32B 模型：
- ZeRO-2：每卡仍要装完整参数 \(2\Phi = 64\) GB，几乎填满 80 GB 卡，留给激活值/长序列的余量极少。
- ZeRO-3：每卡 \(16\Phi/N\)。当 \(N=128\)（16 节点 × 8 卡）时仅约 4 GB，腾出大量显存给超长上下文。

这正是「模型越大、越倾向 ZeRO-3」的根本原因。

#### 4.3.2 核心流程

ZeRO-2（一次迭代）：

```
每卡持有：完整参数（2Φ）+ 分片梯度 + 分片优化器
  前向/反向（完整权重）→ reduce-scatter 梯度（每卡只留自己那片）
  → 用分片优化器更新分片参数
```

ZeRO-3（一次迭代，多了权重的 gather/scatter）：

```
每卡持有：分片参数 + 分片梯度 + 分片优化器
  每层前向前：all-gather 拼出该层完整权重 → 前向 → 立即释放
  每层反向前：all-gather 该层权重 → 反向 → reduce-scatter 梯度
  → 用分片优化器更新分片参数
```

ZeRO-3 的代价是：权重要在卡间反复 gather/scatter，通信量上升；收益是单卡显存随 N 线性下降。

#### 4.3.3 源码精读

ZeRO-2 配置块 [recipes/accelerate_configs/zero2.yaml:3-8](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/recipes/accelerate_configs/zero2.yaml#L3-L8)：

```yaml
deepspeed_config:
  deepspeed_multinode_launcher: standard
  offload_optimizer_device: none      # 不把优化器卸载到 CPU
  offload_param_device: none          # 不把参数卸载到 CPU
  zero3_init_flag: false
  zero_stage: 2                       # 第 8 行：ZeRO-2
```

ZeRO-3 配置块 [recipes/accelerate_configs/zero3.yaml:3-9](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/recipes/accelerate_configs/zero3.yaml#L3-L9)：

```yaml
deepspeed_config:
  deepspeed_multinode_launcher: standard
  offload_optimizer_device: none
  offload_param_device: none
  zero3_init_flag: true               # ZeRO-3：以分片形式初始化模型
  zero3_save_16bit_model: true        # ZeRO-3：以 16-bit 保存 checkpoint
  zero_stage: 3                       # 第 9 行：ZeRO-3
```

两者都用 `distributed_type: DEEPSPEED`（[zero3.yaml:10](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/recipes/accelerate_configs/zero3.yaml#L10) 与 [zero2.yaml:9](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/recipes/accelerate_configs/zero2.yaml#L9)）。本模块对应「最小模块：zero_stage 2 vs 3 与 offload/save_16bit」，三个关键字段含义如下：

- `zero_stage`：分片等级，2 与 3 的差别见 4.3.1 表格。
- `offload_optimizer_device` / `offload_param_device`：是否把优化器/参数卸载到 CPU（`none` 表示不卸载）。open-r1 这两个配置都设为 `none`，即**纯靠 GPU 显存分片，不借助 CPU 内存**——这适合 GPU 显存充裕的集群；若要训更大模型又不想升 ZeRO-3，可改成 `cpu` 换取显存（代价是变慢）。
- `zero3_save_16bit_model`：**仅 ZeRO-3 需要**。因为 ZeRO-3 把权重分散在所有卡上，直接 `save_pretrained` 会得到分片碎片；设为 `true` 让 DeepSpeed 把它们聚合成一份可用的 16-bit 模型。ZeRO-2 因每卡都有完整权重，无需此字段。

#### 4.3.4 代码实践

1. 实践目标：精确列出 `zero2.yaml` 与 `zero3.yaml` 的关键差异，并解释大模型为何更可能选 ZeRO-3。
2. 操作步骤：
   - 打开两个文件，用 `diff` 对比：

     ```bash
     diff recipes/accelerate_configs/zero2.yaml recipes/accelerate_configs/zero3.yaml
     ```
3. 需要观察的现象：差异只在 `deepspeed_config` 块内的少数几行。
4. 预期结果（三处关键差异）：

   | 字段 | zero2.yaml | zero3.yaml | 含义 |
   |---|---|---|---|
   | `zero_stage` | `2` | `3` | 分片等级：ZeRO-2 只切优化器+梯度；ZeRO-3 再切参数 |
   | `zero3_init_flag` | `false` | `true` | ZeRO-3 以分片形式加载模型，仅 rank 0 真正读取权重，省 CPU 内存 |
   | `zero3_save_16bit_model` | （无此字段） | `true` | ZeRO-3 聚合分片权重，保存成可用 16-bit checkpoint |

5. 解释训练 32B 模型为何更可能选 ZeRO-3：32B 模型的完整参数约 64 GB（bf16），ZeRO-2 要求每卡都装下这份完整权重，单张 80 GB 卡几乎被占满，留给激活值和超长上下文的显存所剩无几；ZeRO-3 把参数也分片，每卡显存随卡数线性下降（如 16 节点时每卡仅约 4 GB），从而能塞下更大的上下文。**注意一个真实细节**：open-r1 在 OlympicCoder-32B 上实际选择了 FSDP 而非 ZeRO-3（见 4.4.3 与综合实践），原因是配合 paged AdamW 8-bit 以进一步省优化器显存——但「大模型需要把参数也分片」这一结论对 ZeRO-3 与 FSDP 都成立。
6. 本实践为纯对比与估算，`diff` 命令可直接运行；若需实测训练请用多 GPU 集群，**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：`zero3_init_flag: true` 为什么能省 CPU 内存？

**参考答案**：它让 DeepSpeed 在初始化阶段就以分片方式加载模型——只有 rank 0 进程真正从磁盘读完整权重，再切分广播给其他 rank；其余 rank 不需要一次性把完整权重读进 CPU 内存。对 32B 这种模型，这能显著降低初始化时的内存峰值。

**练习 2**：如果某集群 GPU 显存极大、但卡间带宽很窄，你会选 ZeRO-2 还是 ZeRO-3？

**参考答案**：选 ZeRO-2。ZeRO-3 每层前向/反向都要 all-gather 权重，对带宽极敏感；带宽窄时 ZeRO-2（只多 reduce-scatter 梯度，参数不动）通信更轻、吞吐更高，只要显存装得下完整模型即可。

---

### 4.4 FSDP 配置：PyTorch 原生全分片

#### 4.4.1 概念说明

FSDP（Fully Sharded Data Parallel）是 PyTorch 原生的分片数据并行方案，和 DeepSpeed ZeRO-3 是「同一种思想的两种实现」。它由 PyTorch 官方维护、与 transformers 集成更深，**不需要安装 DeepSpeed**。`fsdp_sharding_strategy: FULL_SHARD` 就等价于 ZeRO-3 的「全分片」；若设为 `SHARD_GRAD_OP` 则等价于 ZeRO-2。

#### 4.4.2 核心流程

FSDP 把模型按「Transformer 层」自动切包（`fsdp_auto_wrap_policy: TRANSFORMER_BASED_WRAP`），每个包在用之前 gather 出完整权重、用完立即释放回分片状态：

```
每层（FSDP 单元）：分片参数
  前向前：all-gather 该层完整权重 → 前向 → 释放为分片
  反向后：reduce-scatter 梯度 → 每卡只持有分片梯度
  优化器只更新分片参数
```

对比 DDP：DDP 每卡持有完整权重、从 gather/scatter；FSDP 则全程分片，仅在计算某层时临时拼合。

#### 4.4.3 源码精读

FSDP 的策略块是最长的一个，[recipes/accelerate_configs/fsdp.yaml:6-16](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/recipes/accelerate_configs/fsdp.yaml#L6-L16)：

```yaml
fsdp_config:
  fsdp_activation_checkpointing: false   # 第 7 行：等 transformers 修复后再开
  fsdp_auto_wrap_policy: TRANSFORMER_BASED_WRAP  # 按 Transformer 层切包
  fsdp_backward_prefetch: BACKWARD_PRE
  fsdp_cpu_ram_efficient_loading: true   # 仅 rank 0 加载，省 CPU 内存
  fsdp_forward_prefetch: true
  fsdp_offload_params: false             # 不把参数卸载到 CPU
  fsdp_sharding_strategy: FULL_SHARD     # 等价 ZeRO-3 全分片
  fsdp_state_dict_type: FULL_STATE_DICT
  fsdp_sync_module_states: true          # 从 rank 0 广播初始权重
  fsdp_use_orig_params: true             # 保留原始参数名（便于保存/部分冻结）
```

逐条要点：

- `fsdp_sharding_strategy: FULL_SHARD`（[fsdp.yaml:13](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/recipes/accelerate_configs/fsdp.yaml#L13)）：决定分片级别，是 FSDP 与 ZeRO 对应关系的关键。
- `fsdp_auto_wrap_policy: TRANSFORMER_BASED_WRAP`（[fsdp.yaml:8](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/recipes/accelerate_configs/fsdp.yaml#L8)）：以 Transformer 层为单元分片，粒度合适、通信效率高。
- `fsdp_cpu_ram_efficient_loading: true`（[fsdp.yaml:10](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/recipes/accelerate_configs/fsdp.yaml#L10)）：与 ZeRO-3 的 `zero3_init_flag: true` 异曲同工——只在 rank 0 加载模型再分片广播，省 CPU 内存。
- `fsdp_activation_checkpointing: false`（[fsdp.yaml:7](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/recipes/accelerate_configs/fsdp.yaml#L7)）：本应省激活值显存，但注释指出在等 transformers 的一个修复（见该行链接的 issue 引用），故暂关。

**真实选择**：[recipes/README.md:16-23](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/recipes/README.md#L16-L23) 明确记录——7B 用 `zero3` 单节点，32B 用 `fsdp` 跑 16 节点，并注释「为装下尽可能大的上下文，必须切到 FSDP1 + paged AdamW 8-bit」。这是 open-r1 在实战中对两种全分片方案的真实取舍。

#### 4.4.4 代码实践

1. 实践目标：对比 DDP 与 FSDP 配置，理解「不分片」与「全分片」的差别。
2. 操作步骤：

   ```bash
   diff recipes/accelerate_configs/ddp.yaml recipes/accelerate_configs/fsdp.yaml
   ```
3. 需要观察的现象：FSDP 比 DDP 多出整个 `fsdp_config` 块，且 `distributed_type` 从 `MULTI_GPU` 变成 `FSDP`。
4. 预期结果：DDP 只有「每卡全副本」的声明；FSDP 多出一整套分片策略（`FULL_SHARD`、`TRANSFORMER_BASED_WRAP`、`cpu_ram_efficient_loading` 等）。两者共享 `mixed_precision: bf16`、`num_processes: 8` 等通用字段。
5. 进一步思考：DDP 与 FSDP 是「不分片 vs 全分片」两个极端；ZeRO-2 / ZeRO-3（以及 FSDP 的 `SHARD_GRAD_OP`）则是中间档。配置选择本质是在这条光谱上取点。
6. `diff` 可直接运行；实测训练需多 GPU 集群，**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：把 `fsdp_sharding_strategy` 从 `FULL_SHARD` 改成 `SHARD_GRAD_OP`，分片级别会落到 ZeRO 的哪一级？

**参考答案**：落到 ZeRO-2。`SHARD_GRAD_OP` 只分片梯度（和优化器状态），参数仍在每卡完整保留，对应 ZeRO-2；`FULL_SHARD` 才把参数也分片，对应 ZeRO-3。

**练习 2**：为什么 32B 的 OlympicCoder 选 FSDP 而不是 ZeRO-3？两者不是等价吗？

**参考答案**：在「分片级别」上 `FULL_SHARD` 与 ZeRO-3 等价，都能装下 32B。但 open-r1 团队在实测中发现，配合 **paged AdamW 8-bit 优化器**（把优化器状态进一步压到 8-bit 并支持分页卸载）后，FSDP 这条路径更稳定地装下了「最大上下文」。此外 FSDP 是 PyTorch 原生、无需额外安装 DeepSpeed，与 transformers 的集成也更紧。所以这是工程稳定性 + 优化器显存压缩的综合选择，而非单纯分片级别的差异。

---

### 4.5 混合精度 bf16 与 num_processes 的多机覆盖

#### 4.5.1 概念说明

四个配置有几个**完全一致**的字段，理解它们能去掉一半的陌生感：

- `mixed_precision: bf16`：四个配置都用 bf16 混合精度——前向/反向用 16 位（省显存、算得快），但优化器仍保留 fp32 主权重（保数值稳定）。相比 fp16，bf16 动态范围与 fp32 一致，不易溢出，是大模型训练的默认选择。
- `num_processes: 8` / `num_machines: 1` / `machine_rank: 0`：单机 8 卡的默认值。如 4.1.3 所述，多机时被命令行覆盖。
- `use_cpu: false`、`rdzv_backend: static`、`same_network: true`：通用分布式环境声明，四份一致。

#### 4.5.2 核心流程

bf16 混合精度的数值流：

```
fp32 主权重（优化器持有）
   │ 转 bf16
   ▼
bf16 权重 → 前向（autocast bf16）→ loss → 反向 → bf16 梯度
   │
   ▼
更新回 fp32 主权重（避免累加误差）
```

`num_processes` 的多机覆盖流程（见 4.1.3）：

```
YAML: num_processes=8（单机默认）
   │ train.slurm 命令行 --num_processes $WORLD_SIZE
   ▼
实际生效值 = WORLD_SIZE（所有节点总 GPU 数）
```

#### 4.5.3 源码精读

四份配置的 `mixed_precision` 与 `num_processes` 行：

- [ddp.yaml:8](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/recipes/accelerate_configs/ddp.yaml#L8)、[ddp.yaml:10](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/recipes/accelerate_configs/ddp.yaml#L10)
- [zero2.yaml:14-16](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/recipes/accelerate_configs/zero2.yaml#L14-L16)
- [zero3.yaml:14-16](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/recipes/accelerate_configs/zero3.yaml#L14-L16)
- [fsdp.yaml:19-21](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/recipes/accelerate_configs/fsdp.yaml#L19-L21)

以及 `downcast_bf16: 'no'`（如 [zero3.yaml:11](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/recipes/accelerate_configs/zero3.yaml#L11)）：表示不把 bf16 进一步下cast，保持精度。

覆盖发生在 [slurm/train.slurm:153-154](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/slurm/train.slurm#L153-L154) 的 `--num_machines $NUM_NODES` 与 `--num_processes $WORLD_SIZE`。

#### 4.5.4 代码实践

1. 实践目标：确认四份配置的「公共字段」完全一致，并能指出哪些字段在多机下会被覆盖。
2. 操作步骤：

   ```bash
   # 用 grep 抽出所有配置的 mixed_precision / num_processes / num_machines 行做对比
   grep -H -E 'mixed_precision|num_processes|num_machines|machine_rank' \
     recipes/accelerate_configs/*.yaml
   ```
3. 需要观察的现象：四份配置在这些字段上取值相同。
4. 预期结果：均为 `mixed_precision: bf16`、`num_processes: 8`、`num_machines: 1`、`machine_rank: 0`（ddp.yaml 没有 `machine_rank`，但语义一致）。这说明「单机 8 卡 + bf16」是 open-r1 的基准假设，多机差异交给命令行。
5. `grep` 可直接运行，无需 GPU。

#### 4.5.5 小练习与答案

**练习**：README 中有一段在单机内同时跑 vLLM（占 1 卡）与 GRPO 训练（占其余 7 卡）的命令（[README.md:313-315](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/README.md#L313-L315)），它用 `--num_processes=7`。这与 `zero2.yaml` 里写的 `8` 冲突吗？

**参考答案**：不冲突。这正是命令行覆盖 YAML 的典型场景——YAML 写的是单机满 8 卡默认值，实际因为要让出 1 张卡给 vLLM，命令行用 `--num_processes=7` 覆盖。也说明：**真正决定进程数的永远是命令行，YAML 的 `num_processes` 只是缺省值**。

---

## 5. 综合实践

**任务**：为「在 16 节点 × 8 卡（共 128 卡）上训练一个 32B 模型，并希望尽量大的上下文」选择最合适的 accelerate 配置，并写出完整的 `sbatch` 命令与理由。

步骤：

1. 阅读四个 YAML 配置，结合本讲的显存公式判断：DDP、ZeRO-2 因每卡需装完整 32B 权重（约 64 GB）而不可行；只有 ZeRO-3 与 FSDP（`FULL_SHARD`）把参数分片，能在 128 卡下把每卡模型状态压到约 4 GB。
2. 查阅 [recipes/README.md:16-23](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/recipes/README.md#L16-L23)，确认 open-r1 真实的选择是 FSDP，并理解其注释「为装下最大上下文，切到 FSDP1 + paged AdamW 8-bit」。
3. 写出命令（与 README 一致）：

   ```bash
   sbatch --nodes=16 slurm/train.slurm --model OlympicCoder-32B --task sft \
          --config v00.00 --accelerator fsdp
   ```

4. 解释 `train.slurm` 会把 `--accelerator fsdp` 展开为 `accelerate launch --config_file recipes/accelerate_configs/fsdp.yaml --num_machines 16 --num_processes 128 --machine_rank $SLURM_PROCID ...`，其中 `num_processes` / `num_machines` / `machine_rank` 覆盖了 YAML 默认值，而 `fsdp_config` 块（`FULL_SHARD` 等策略）保持生效。
5. 反思：若改用 `--accelerator zero3`，理论上也能跑（同为全分片），但 open-r1 实测选择 FSDP 是为了配合 paged AdamW 8-bit 以进一步压缩优化器显存——**结论要服从仓库里的真实证据，而非想当然**。

预期产出：一条命令 + 一段约 150 字的选择理由，显式提及「参数分片」与「paged AdamW 8-bit」两个关键点。若本地无集群，本任务为「源码阅读 + 推理型实践」，命令无需真实提交，**待本地验证**。

## 6. 本讲小结

- open-r1 提供四个 accelerate 配置（`ddp` / `zero2` / `zero3` / `fsdp`），它们**不被 Python 脚本读取**，而是由 `accelerate launch` 在脚本启动前消费，搭建分布式环境。
- `slurm/train.slurm` 用 `--accelerator <名>` 选配置（名即文件名），并用 `--num_processes` / `--num_machines` / `--machine_rank` 覆盖 YAML 的对应默认值——**多机下改 YAML 的 `num_processes` 不生效**。
- DDP 是「每卡完整副本」，通信最轻但单卡要装全套，只适合小模型；ZeRO-2 切优化器+梯度（参数仍完整），ZeRO-3 再切参数（最省显存、通信最重）。
- ZeRO-3 相比 ZeRO-2 多两个关键字段：`zero3_init_flag: true`（分片初始化省 CPU 内存）、`zero3_save_16bit_model: true`（聚合分片权重存 16-bit checkpoint）。
- FSDP 的 `FULL_SHARD` 等价于 ZeRO-3、`SHARD_GRAD_OP` 等价于 ZeRO-2；open-r1 真实在 32B 上选了 FSDP + paged AdamW 8-bit 以装下最大上下文。
- 四份配置共享 `mixed_precision: bf16` 与单机 8 卡默认值，差异只在策略块（`deepspeed_config` / `fsdp_config` / 无）。

## 7. 下一步学习建议

- 回到 [u7-l1](u7-l1-slurm-vllm-training.md)，结合本讲重新理解 `train.slurm` 如何把 `accelerate launch` 与 `srun` 嵌套，以及 `use_vllm` 分支下 `WORLD_SIZE` 如何计算（与 `num_processes` 覆盖直接相关）。
- 继续阅读 [u7-l3](u7-l3-callbacks-hub-wandb.md)，了解训练过程中的回调与 Hub 推送，补全「训练基础设施」的最后一环。
- 进阶：阅读 DeepSpeed 官方文档对 `zero_stage` 与 offload 的说明，理解把 `offload_optimizer_device` 从 `none` 改成 `cpu` 的代价与收益；阅读 PyTorch FSDP 文档对照 `fsdp_sharding_strategy` 两种取值。
