# u7-l5 SFT 配方启动器：scripts/train/sft.py、A4B 配方与 CUDA 13 MoE 镜像

## 1. 本讲目标

学完本讲，你应该能够：

1. 理解「配方（recipe）」的运行模型：一条 `uv run python scripts/train/sft.py --recipe <名字>` 命令如何展开成一个完整的 Beaker 任务，为什么它**不需要先构建镜像、不需要先提交代码**。
2. 读懂 `RECIPES` 字典里 c12r 与 a4b 两个配方的每一类字段，并能说出两者在镜像、基座、学习率、并行与显存、超时预算上的关键差异及其动机。
3. 解释 A4B（MoE v2 / `OLMoDDPModel`）训练为什么必须换镜像：transformer-engine 依赖、cuda13-moe 镜像变体如何构建、以及缺 TE 时启动期的报错防线。
4. 会用 `--dry_run` 与 `tests/test_sft_recipes.py` 验证配方的展开不变量。

本讲是「从零跑通」与「读懂入口」之后的** launch 层**：u7-l1 精读了训练入口 `open_instruct/olmo_core_finetune.py`，本讲讲的是站在它上面、把一整次已知可跑的训练固化成一条命令的启动器。

## 2. 前置知识

- **Beaker 与 mason.py**（u1-l4、u9-l1）：Beaker 是 Ai2 的 GPU 任务平台；`mason.py` 把一条训练命令包装成 Beaker 任务（注入镜像、环境变量、数据挂载）。`--pure_docker_mode` 表示直接用镜像自带环境运行；`--min_runtime` 设置 Beaker 的 `minRuntime`（受保护最短运行时，至少 5 分钟且带单位）；`--auto_resume` 设置 `autoResume`（被抢占后自动重排）。
- **MoE v2 / OLMoDDPModel**（u1-l2、u1-l4）：Olmo 3.5 Small（A4B）是混合专家模型，其 MoE 层的 token permutation（专家分发前的重排）调用 transformer-engine（TE）的算子；它拒绝 FSDP，走 OLMo-core 的 DDP 训练模块。
- **numpy 预编码缓存**（u3-l2、u7-l1）：OLMo-core 路线的 SFT 不在训练时现分词，而是先跑一个 CPU 分词作业，把 token 流写成可 mmap 的 numpy 文件；训练启动时若缓存目录不命中会直接硬失败。
- **transformer-engine（TE）**：NVIDIA 的训练库，这里只用到它的 `pytorch.permutation` 一组算子。它是**预编译二进制 + 从源码编译的 torch 绑定**的混合体，因此对镜像里 torch/cuBLAS 的版本非常敏感——这是本讲镜像变体一节的伏笔。
- **配方（recipe）**：本讲的核心概念——把一次已知良好的训练运行（基座、数据、超参、镜像、集群、超时）固化成一个字典，让「换个基座重跑」「断点续跑」都退化成一条命令加一两个覆盖参数。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [scripts/train/sft.py](https://github.com/allenai/open-instruct/blob/a4cd966a9e202843baba653115045e6fd6f473db/scripts/train/sft.py) | 配方启动器：`RECIPES` 注册表 + 把配方展开成 mason 命令的 `main` |
| [tests/test_sft_recipes.py](https://github.com/allenai/open-instruct/blob/a4cd966a9e202843baba653115045e6fd6f473db/tests/test_sft_recipes.py) | 用 `--dry_run` 断言每个配方展开不变量的测试 |
| [Dockerfile](https://github.com/allenai/open-instruct/blob/a4cd966a9e202843baba653115045e6fd6f473db/Dockerfile) | cuda12 / cuda13 / cuda13-moe 三种训练镜像的构建定义 |
| [scripts/train/build_image_and_launch.sh](https://github.com/allenai/open-instruct/blob/a4cd966a9e202843baba653115045e6fd6f473db/scripts/train/build_image_and_launch.sh) | 统一构建/启动入口：`--cuda-version 13 --moe` 构建并使用 MoE 变体镜像 |
| [scripts/test/run_gpu_pytest.sh](https://github.com/allenai/open-instruct/blob/a4cd966a9e202843baba653115045e6fd6f473db/scripts/test/run_gpu_pytest.sh) | GPU 测试脚本：按镜像名把 `-cuda13`/`-cuda13-moe` 路由到 CUDA 13 集群 holmes |
| [open_instruct/olmo_core_utils.py](https://github.com/allenai/open-instruct/blob/a4cd966a9e202843baba653115045e6fd6f473db/open_instruct/olmo_core_utils.py) | `require_transformer_engine`：缺 TE 时的启动期报错（含修复命令） |
| [open_instruct/olmo_core_finetune.py](https://github.com/allenai/open-instruct/blob/a4cd966a9e202843baba653115045e6fd6f473db/open_instruct/olmo_core_finetune.py) | 训练入口（u7-l1 已精读）：numpy 缓存解析、TE 检查调用点、checkpoint 语义 |
| [docs/algorithms/sft_running_guide.md](https://github.com/allenai/open-instruct/blob/a4cd966a9e202843baba653115045e6fd6f473db/docs/algorithms/sft_running_guide.md) | 用户文档：两个配方的一句话说明、告诫与速度估计 |

## 4. 核心概念与源码讲解

### 4.1 配方注册表与 mason 命令拼装

#### 4.1.1 概念说明

`scripts/train/sft.py` 解决的问题是：一次 SFT 实验牵涉**十几个训练参数、一个固定镜像、一个集群布局、一份预分词缓存、一组超时预算**，任何一项手抄错都会浪费一次 2×8 B300、几十小时的运行。配方把这些全部固化进一个字典，使得：

- **一条命令即一次完整运行**：`uv run python scripts/train/sft.py --recipe c12r`。
- **训练代码来自镜像而非当前 checkout**：每个配方固定（pin）它运行所用的 Beaker 镜像 ID。镜像里装好了依赖、拷进了当时的 `open_instruct/` 与 `scripts/` 代码。所以你不需要先构建镜像、也不需要先提交代码——你本地的 checkout 只用来**生成** mason 命令。模块 docstring 把这一点写得很直白：[scripts/train/sft.py:L6-L8](https://github.com/allenai/open-instruct/blob/a4cd966a9e202843baba653115045e6fd6f473db/scripts/train/sft.py#L6-L8)（训练代码来自镜像；重复执行同名命令会从输出目录最新 checkpoint 恢复，目录无时间戳）。
- **断点续跑无时间戳**：输出目录名只含 run 名（如 `a4b-s34521`），不含时间戳。重复执行同一条命令会落进同一个目录，从最新 checkpoint 恢复；配合 mason 的 `--auto_resume`，Beaker 抢占后也会自动重排续跑。

#### 4.1.2 核心流程

```text
uv run python scripts/train/sft.py --recipe a4b [--checkpoint DIR --name RUN | --seed N | --steps N] [--dry_run]
        │
        ├─ 1. argparse 解析；从 RECIPES 取配方
        ├─ 2. 派生 run 名（默认 <recipe>-s<seed>[(-<steps>st)]）并校验字符集
        ├─ 3. beaker account whoami 取用户名 → output_dir = /weka/.../sft/<user>/<name>
        ├─ 4. 拼训练命令 train = [torchrun --nnodes=N ... open_instruct/olmo_core_finetune.py
        │        --model_name_or_path <checkpoint>  + recipe["train_args"]
        │        + --max_train_steps/--checkpointing_steps/--ephemeral_save_interval
        │        + --data_loader_seed/--run_name/--output_dir]
        ├─ 5. 拼 mason 命令 = [python mason.py --cluster --workspace --budget --image
        │        --pure_docker_mode --num_nodes --gpus 8 --priority --min_runtime --auto_resume
        │        --timeout --extra_weka_buckets --env OLMO_SHARED_FS=1
        │        --env EXPECTED_NUMPY_CACHE=<cache>  + recipe["env"] 逐项 --env
        │        --task_name --description  --  + train]
        └─ 6. --dry_run ? 打印命令 : subprocess.run(mason) 真正提交 Beaker
```

checkpoint 策略也在这里统一计算：全程中点一个永久 checkpoint，另外每 1000 步（`SAVE_EVERY`）一个临时（ephemeral）恢复点——OLMo-core 要求临时保存频率**严格高于**永久保存，见 [scripts/train/sft.py:L259-L262](https://github.com/allenai/open-instruct/blob/a4cd966a9e202843baba653115045e6fd6f473db/scripts/train/sft.py#L259-L262) 的注释。

#### 4.1.3 源码精读

**① 配方字典的结构**。以 c12r 为例（[scripts/train/sft.py:L27-L38](https://github.com/allenai/open-instruct/blob/a4cd966a9e202843baba653115045e6fd6f473db/scripts/train/sft.py#L27-L38)）：`image`（Beaker 镜像 ID）、`checkpoint`（起始的 OLMo-core DCP 权重目录）、`steps`（18,410 步——注释解释这是 seed 34521 的加载顺序恰好消费半个 epoch C12 的第一步，其他 seed 只差几步）、`seed`（数据加载顺序种子）、`cache`（冻结的预分词缓存名）、`train_args`（直接喂给 `olmo_core_finetune.py` 的参数列表）、可选 `env`（额外环境变量）、`cluster`/`nodes`/`timeout`/`min_runtime`（Beaker 侧预算）。

**② run 名与输出目录的派生**（[scripts/train/sft.py:L229-L247](https://github.com/allenai/open-instruct/blob/a4cd966a9e202843baba653115045e6fd6f473db/scripts/train/sft.py#L229-L247)）：默认名 `<recipe>-s<seed>`；显式 `--steps` 会追加 `-<steps>st` 后缀，让短跑进自己的目录、之后的完整运行不会从短跑的 checkpoint 恢复（L234-235 注释）。`--checkpoint` 换基座时**必须**同时给 `--name`（L230-231），防止你的自定义运行恢复进默认 run 的目录。名字还要过一道正则校验（L236-237），因为它会被拼进 Weka 路径。`beaker account whoami` 失败且非 dry_run 时直接报错退出；dry_run 则用 `<user>` 占位（L240-246）。

**③ 训练命令的拼装**（[scripts/train/sft.py:L249-L264](https://github.com/allenai/open-instruct/blob/a4cd966a9e202843baba653115045e6fd6f473db/scripts/train/sft.py#L249-L264)）：多机启动用 `torchrun --nnodes=<nodes> --node_rank=$BEAKER_REPLICA_RANK --master_addr=$BEAKER_LEADER_REPLICA_HOSTNAME --master_port=29400 --nproc_per_node=8`——`$BEAKER_*` 是 Beaker 注入的多机拓扑环境变量。注意 L255-256 的注释与 `shlex.quote`：mason 把命令拼给 `bash -c` 时**不会**给普通参数加引号，所以 checkpoint 路径要自己 quote。

**④ mason 命令的拼装与缓存防线**（[scripts/train/sft.py:L265-L278](https://github.com/allenai/open-instruct/blob/a4cd966a9e202843baba653115045e6fd6f473db/scripts/train/sft.py#L265-L278)）：镜像、集群、`--gpus 8`、`--min_runtime`/`--timeout`/`--auto_resume` 全部来自配方；`--extra_weka_buckets` 挂载基座 checkpoint 所在的 Weka 桶。两个固定环境变量之外，配方的 `env` 列表被逐项展开成 `--env <VAR=VAL>`（L273 的列表推导）。其中 `EXPECTED_NUMPY_CACHE=<cache>`（L272）是**缓存身份防线**：配方的 cache 值（如 `bf82a8fab8-6068a350-rowaligned`）必须与作业实际解析到的缓存目录一致，否则作业在训练开始前死掉——c12r 的注释（[scripts/train/sft.py:L36-L38](https://github.com/allenai/open-instruct/blob/a4cd966a9e202843baba653115045e6fd6f473db/scripts/train/sft.py#L36-L38)）说明 mixer、tokenizer、模板或 `--seed` 任一项不同都会解析到不同缓存。

这个环境变量的**读取方在固定镜像内的训练代码里**——在当前 main checkout 中 grep 不到 `EXPECTED_NUMPY_CACHE` 的消费者，这正是「训练代码来自镜像」的直接证据。main 上等价的保护在训练入口：缓存目录名由 `compute_config_hash(dcs, tc)` 加种子后缀拼出（[open_instruct/olmo_core_finetune.py:L557-L564](https://github.com/allenai/open-instruct/blob/a4cd966a9e202843baba653115045e6fd6f473db/open_instruct/olmo_core_finetune.py#L557-L564)），目录不命中时抛 `FileNotFoundError` 并打印可直接复制的分词作业命令（[open_instruct/olmo_core_finetune.py:L576-L622](https://github.com/allenai/open-instruct/blob/a4cd966a9e202843baba653115045e6fd6f473db/open_instruct/olmo_core_finetune.py#L576-L622)）。

**⑤ 缓存目录名的组成**（[open_instruct/olmo_core_finetune.py:L94-L111](https://github.com/allenai/open-instruct/blob/a4cd966a9e202843baba653115045e6fd6f473db/open_instruct/olmo_core_finetune.py#L94-L111)）：

\[
\text{目录名} = \underbrace{\text{config\_hash}}_{\text{mixer/分词器/模板/长度}} - \underbrace{\text{sha256}(\texttt{"seed:max\_seq\_length"})[:8]}_{\text{种子后缀}} + \texttt{"-rowaligned"}
\]

两个种子要分清：`train_args` 里的 `--seed 33333` 是 `args.tracking.seed`，**参与缓存目录命名**（所以注释说「--seed names the cache」）；配方的 `seed` 字段（34521）经 `--data_loader_seed` 进入训练，只决定**数据顺序**。这也解释了 run 名为什么是 `a4b-s34521` 而缓存名里的后缀来自 33333——按上式应为 `sha256("33333:65536")` 的前 8 位（可用一行 Python 本地验证）。

#### 4.1.4 代码实践

1. **实践目标**：不提交任何 Beaker 任务，看到 a4b 配方展开出的完整 mason 命令。
2. **操作步骤**：在仓库根目录运行 `uv run python scripts/train/sft.py --recipe a4b --dry_run`。
3. **需要观察的现象**：第一行 `Output: /weka/oe-adapt-default/allennlp/deletable_checkpoint/<user>/sft/a4b-s34521`（无 beaker CLI 时 `<user>` 是占位符），第二行是完整的 `python mason.py ...` 命令。
4. **预期结果**：命令里能依次找到 `--image 01M7VCQM65SD251KYS4Z1AD8FG`、`--cluster ai2/holmes`、`--num_nodes 2`、`--env EXPECTED_NUMPY_CACHE=bf82a8fab8-6068a350-rowaligned`、`--env PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True`，`--` 之后以 `torchrun --nnodes=2 ...` 开头并以 `--output_dir .../a4b-s34521` 结尾。（待本地验证：本机需已 `uv sync`。）

#### 4.1.5 小练习与答案

**练习 1**：为什么 `--checkpoint` 必须搭配 `--name`？
**答案**：输出目录由 run 名决定。若允许换基座但不改名，新运行会落进默认 run 的目录并**从默认运行的 checkpoint 恢复**，等于静默丢弃了你指定的新基座（[scripts/train/sft.py:L230-L231](https://github.com/allenai/open-instruct/blob/a4cd966a9e202843baba653115045e6fd6f473db/scripts/train/sft.py#L230-L231) 的报错信息就是这个意思）。

**练习 2**：同一个配方跑 `--steps 100` 的短测，再跑完整 18,410 步，两者会互相污染吗？
**答案**：不会。显式 `--steps` 使默认 run 名带上 `-100st` 后缀（L235），短测写进 `a4b-s34521-100st` 目录；完整运行仍写 `a4b-s34521`。

**练习 3**：`--ephemeral_save_interval` 在 18,410 步的 run 里是多少？为什么不能等于 `--checkpointing_steps`？
**答案**：`SAVE_EVERY=1000`（L22），而永久 checkpoint 在中点 `(18410+1)//2 = 9205` 步。L261-262 注释说明 OLMo-core 要求临时保存**严格更频繁**于永久保存——临时 checkpoint 服务断点续跑，永久 checkpoint 服务评估产出，两者语义不同。

### 4.2 a4b 配方：数据、优化器与显存工程

#### 4.2.1 概念说明

a4b 是 c12r 的「平行实验臂」：**数据、缓存、种子、步数完全相同**，只把基座从 dense 的 B1 换成 Olmo 3.5 Small hero（A4B MoE）的 4T decay 端点。这样实验对比只隔离「基座」这一个变量。但 MoE 模型带来三个新问题——专家放不进单卡显存、交叉熵的 logits 张量爆炸、TE 内核带来的计算图变化——a4b 的 `train_args` 增量几乎全部是对这三个问题的工程回应。

#### 4.2.2 核心流程

先看两个配方的**不变量**（这是 a4b 复用 c12r 冻结缓存的前提）：

| 字段 | c12r = a4b |
|---|---|
| `steps` / `seed`（数据顺序） | 18,410 / 34521 |
| `cache` | `bf82a8fab8-6068a350-rowaligned` |
| `--mixer_list`（4 个 parquet + 权重） | 完全一致 |
| `--extra_numpy_dirs`（10% 长上下文 replay） | `/weka/.../h182/replay-b1c12r` |
| `--seed`（命名缓存） | 33333 |
| `--max_seq_length` / batch / grad accum | 65536 / 1 / 1 |

再看 a4b 的**增量**（右侧为其动机，全部来自配方内注释）：

| 增量 | 值 | 动机（源码注释原文依据） |
|---|---|---|
| 基座 | `olmo35-4b-active-4t-decay159k-causal/step162974` | A4B hero 的 4T decay 端点 |
| 峰值 LR | `7.3e-5`（c12r 为 `2e-4`） | 按两基座预训练 LR 之比缩放 |
| `--expert_parallel_degree` | `8` | 专家在节点内 8 卡间分片 |
| `--loss_implementation` | `fused_linear` | 默认 CE 物化 24.5 GiB logits 会 OOM |
| `env` | `PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True` | 否则第一次 backward 因碎片 OOM |
| `timeout` / `min_runtime` | `72h` / `2h15m`（c12r 为 `14h` / `45m`） | 步速慢约 4 倍；首个临时 checkpoint 约第 2 小时 |
| `--config_name` | `scripts/c12/a4b_sft_config.json` | **只存在于 a4b 镜像里**，main 没有 |

#### 4.2.3 源码精读

**① 学习率的推导**。a4b 的注释（[scripts/train/sft.py:L109-L113](https://github.com/allenai/open-instruct/blob/a4cd966a9e202843baba653115045e6fd6f473db/scripts/train/sft.py#L109-L113)）写明：峰值 LR 是 c12r 的 2e-4 乘以两个基座预训练 LR 之比：

\[
\text{LR}_{a4b} = 2\times10^{-4} \times \frac{4.04\times10^{-4}}{1.1\times10^{-3}} \approx 7.35\times10^{-5} \;\Rightarrow\; \texttt{7.3e-5}
\]

直觉：SFT 峰值 LR 按「基座预训练最后用的 LR」等比缩放，避免对用更小 LR 训到收敛的基座施加过大扰动。

**② EP8 与数据并行批大小**。[scripts/train/sft.py:L192-L194](https://github.com/allenai/open-instruct/blob/a4cd966a9e202843baba653115045e6fd6f473db/scripts/train/sft.py#L192-L194) 设置专家并行度为 8（节点内 8 卡分片专家）。L123-124 的注释解释了一个容易混淆的点：EP 把专家分片到**同样的 16 个 rank** 上，不改变数据并行的划分——每步的数据并行批大小仍是 16 条序列（2 节点 × 8 卡 × batch 1）。

**③ 显存工程三件套**：

- **fused linear+CE**（[scripts/train/sft.py:L195-L197](https://github.com/allenai/open-instruct/blob/a4cd966a9e202843baba653115045e6fd6f473db/scripts/train/sft.py#L195-L197)）：注释「The default CE materialises 24.5 GiB of logits and OOMs」。65,536 的序列长度 × 大词表，默认实现会先物化整个 logits 张量再算交叉熵；`fused_linear` 把线性投影与 CE 融合，避免物化。
- **分配器设置**（[scripts/train/sft.py:L199-L200](https://github.com/allenai/open-instruct/blob/a4cd966a9e202843baba653115045e6fd6f473db/scripts/train/sft.py#L199-L200)）：`expandable_segments:True` 来自 A4B 预训练配方，缓解显存碎片导致的第一次 backward OOM。它是 a4b 唯一的 `env` 项，经 mason 的 `--env` 注入。
- **整块重计算 + 关闭嵌套 checkpoint**（[scripts/train/sft.py:L115-L121](https://github.com/allenai/open-instruct/blob/a4cd966a9e202843baba653115045e6fd6f473db/scripts/train/sft.py#L115-L121) 的镜像注释）：模型配置用整块重计算（recompute_each_block），同时关掉嵌套的 `checkpoint_attn` / `checkpoint_permute_moe_unpermute` / `checkpoint_second_unpermute`——#2291 实测同 CE 下 **-16% s/step**；反过来关整块重计算则直接 OOM。配套地，嵌套 checkpoint 关闭后 torch.compile 会在第一次 backward OOM，所以两个配方都传 `--no_compile_model`（a4b 的 L160-161 注释）。训练侧统一的 `--activation_checkpointing_mode budget` + `--activation_memory_budget 1` 则见 [scripts/train/sft.py:L162-L165](https://github.com/allenai/open-instruct/blob/a4cd966a9e202843baba653115045e6fd6f473db/scripts/train/sft.py#L162-L165)。
- **flash_2 注意力**（[scripts/train/sft.py:L158-L159](https://github.com/allenai/open-instruct/blob/a4cd966a9e202843baba653115045e6fd6f473db/scripts/train/sft.py#L158-L159)）：镜像里的 FA4 在 SM100（B300）上拒绝 head_dim 256，所以显式选 flash_2——这是「训练行为由镜像决定」的又一例：注意力实现的可用性取决于镜像里装的库版本。

**④ 必须诚实对待的告诫**（[docs/algorithms/sft_running_guide.md:L36-L40](https://github.com/allenai/open-instruct/blob/a4cd966a9e202843baba653115045e6fd6f473db/docs/algorithms/sft_running_guide.md#L36-L40)）：约 7.4 s/step 与约 38 小时是**从 #2291 的 300 步基准外推的估计值**（且是在更早的镜像 `01M6XV36` 上测的）；固定镜像 `01M7VCQM65SD251KYS4Z1AD8FG` 只通过了一次 **40 步冒烟测试（8.20 s/step，步 10-40，CE 吻合）**；**尚无完整训练运行用过这个配方**。读文档时要区分「实测」「外推」与「冒烟」三种证据等级。

**⑤ 保存语义的 MoE 边界**：两个配方都传 `--no_save_async`。对 a4b 这不只是习惯——[open_instruct/olmo_core_finetune.py:L526-L535](https://github.com/allenai/open-instruct/blob/a4cd966a9e202843baba653115045e6fd6f473db/open_instruct/olmo_core_finetune.py#L526-L535) 的 `_save_async` 对 `OLMoDDPModel` **强制**返回 False（OLMo-core 的异步保存拒绝自存 state dict 的 DDP 训练模块），即使忘了传 flag 也只是降级为同步保存并打警告。

#### 4.2.4 代码实践

1. **实践目标**：用缓存目录命名公式确认 a4b 与 c12r 命中**同一个** numpy 缓存目录，从而理解「换基座不重分词」。
2. **操作步骤**：(a) 读 [open_instruct/olmo_core_finetune.py:L104-L111](https://github.com/allenai/open-instruct/blob/a4cd966a9e202843baba653115045e6fd6f473db/open_instruct/olmo_core_finetune.py#L104-L111) 的 `_numpy_cache_dir`；(b) 本地运行一行 Python：`import hashlib; print(hashlib.sha256(b"33333:65536").hexdigest()[:8])`；(c) 对比配方 cache 值 `bf82a8fab8-6068a350-rowaligned` 的中段。
3. **需要观察的现象**：`--seed 33333` 与 `--max_seq_length 65536` 两个值如何进入哈希；两个配方的这组值完全一致。
4. **预期结果**：哈希前缀与 `6068a350` 一致（若不一致则说明缓存名由其他机制参与，需回读 `compute_config_hash`——待本地验证）。另注意 c12r 注释「the job's cache dir holds a symlink to the frozen cache」（[scripts/train/sft.py:L94-L96](https://github.com/allenai/open-instruct/blob/a4cd966a9e202843baba653115045e6fd6f473db/scripts/train/sft.py#L94-L96)）：作业的 cache 目录里放的是指向冻结缓存的符号链接，`--extra_numpy_dirs` 再把 replay 数据并进来，两者共同保证「同一份数据、同一个顺序」。

#### 4.2.5 小练习与答案

**练习 1**：a4b 的 `--extra_numpy_dirs` 指向 `h182/replay-b1c12r`，这个名字透露了什么？
**答案**：replay 数据是 c12r 所属 H182 实验线冻结的产物，a4b 直接复用——这是「数据侧零差异」设计的一部分（a4b 是 H203 的 a4b 臂，对照 c12r）。

**练习 2**：为什么 a4b 的 `min_runtime` 是 2h15m 而 c12r 只有 45m？
**答案**：`min_runtime` 要保证任务至少活过**第一个恢复点**（步 1000 的临时 checkpoint）：c12r 约第 41 分钟到达（[scripts/train/sft.py:L106-L107](https://github.com/allenai/open-instruct/blob/a4cd966a9e202843baba653115045e6fd6f473db/scripts/train/sft.py#L106-L107)），a4b 按估计步速约第 2 小时到达（L204-205），故分别给 45m / 2h15m。

**练习 3**：如果有人提议「把 a4b 的嵌套 MoE checkpoint 打开以再省点显存、换更快速度」，配方注释会怎么回应？
**答案**：方向反了。#2291 的结论是**关掉**嵌套 checkpoint（保留整块重计算）才提速 -16% s/step；要再省显存只能关整块重计算，而那样会 OOM（[scripts/train/sft.py:L118-L121](https://github.com/allenai/open-instruct/blob/a4cd966a9e202843baba653115045e6fd6f473db/scripts/train/sft.py#L118-L121)）。

### 4.3 CUDA 13 MoE 镜像变体与 transformer-engine

#### 4.3.1 概念说明

MoE v2（`OLMoDDPModel`）的 token permutation 调用 TE 的算子，而 open-instruct 的默认镜像（cuda12 / cuda13）**不安装 TE**。#2298 补上这条链路时没有改动默认镜像，而是新增了第三个变体 `cuda13-moe`。为什么不能把 TE 塞进默认镜像？因为 TE 2.16.1 的预编译 cu13 库需要一个 torch 自带 cuBLAS（13.1.0.3）**不导出**的符号（`cublasLtGroupedMatrixLayoutInit_internal`）；修复办法是把 torch 绑定的 cuBLAS 指向基座镜像里更新的系统副本——但这会改变镜像里**所有**代码看到的 cuBLAS，所以必须是独立变体（[Dockerfile:L88-L95](https://github.com/allenai/open-instruct/blob/a4cd966a9e202843baba653115045e6fd6f473db/Dockerfile#L88-L95) 的注释逐条记录了这些理由，并说明为什么 `LD_LIBRARY_PATH` 行不通——torch 硬编码加载自带路径）。

#### 4.3.2 核心流程

变体的防线分三层，失败时间逐层提前：

```text
构建期  Dockerfile cuda13-moe 阶段：
        装 TE 2.16.1（绑定 venv 的 torch 编译）→ cuBLAS 符号链接替换 → nm 检查符号存在
          ↓ 镜像
启动期  build_image_and_launch.sh --cuda-version 13 --moe：
        校验组合合法 → IMAGE_VARIANT=-moe 传给 docker build → 镜像名追加 -moe
        run_gpu_pytest.sh：*-cuda13 | *-cuda13-moe → 路由到 ai2/holmes（CUDA 13 集群）
          ↓ 任务
运行期  olmo_core_finetune.py：模型是 OLMoDDPModel → require_transformer_engine()
        → TE 缺失时立即 RuntimeError（附正确启动命令），而非等到第一次 forward
```

#### 4.3.3 源码精读

**① Dockerfile 的变体选择**（[Dockerfile:L1-L9](https://github.com/allenai/open-instruct/blob/a4cd966a9e202843baba653115045e6fd6f473db/Dockerfile#L1-L9)）：两个构建参数 `CUDA_VERSION` 与 `IMAGE_VARIANT` 组合出 `FROM cuda${CUDA_VERSION}${IMAGE_VARIANT}`——三个命名基座阶段分别是 cuda12（12.8.1）、cuda13（13.0.3）与 cuda13-moe（**13.3.1-cudnn** 基座）。注意 `ENV DS_SKIP_CUDA_CHECK=1` 位于 cuda13-moe 阶段定义内（L7-8）：torch 按 CUDA 13.0 构建，没有它 DeepSpeed 拒绝对 13.3 工具链 JIT 构建自己的算子（如 CPU Adam）；由于 `ENV` 只随 `FROM cuda13-moe` 继承，**两个默认镜像不受影响**。

**② TE 安装与构建期自检**（[Dockerfile:L96-L112](https://github.com/allenai/open-instruct/blob/a4cd966a9e202843baba653115045e6fd6f473db/Dockerfile#L96-L112)）：整个变体增量收在一条 `if [ "$IMAGE_VARIANT" = "-moe" ]` 守卫的 RUN 里——先装构建工具（cmake/ninja/pybind11），从 venv 定位 cuDNN 头文件与库目录，再 `uv pip install --no-build-isolation "transformer-engine[pytorch]==2.16.1"`（绑定按当前 venv 的 torch 编译，`TE_MAX_JOBS=2` 为小内存构建机留余量）；然后把 venv 里 `nvidia/cu13/lib` 的 `libcublas`/`libcublasLt` 替换为指向 `/usr/local/cuda/lib64` 系统副本的符号链接，最后 `nm -D ... | grep -q cublasLtGroupedMatrixLayoutInit_internal` 在**构建期**验证符号确实可用——链接失败或符号缺失都会让 `exit 1` 终止构建，而不是留一个运行时才炸的镜像。

**③ 启动器的 `--moe` 路由**（[scripts/train/build_image_and_launch.sh:L36-L45](https://github.com/allenai/open-instruct/blob/a4cd966a9e202843baba653115045e6fd6f473db/scripts/train/build_image_and_launch.sh#L36-L45)）：`--moe` 必须搭配 `--cuda-version 13`，否则报错退出（只有 CUDA 13 镜像有 TE 变体）；随后 `image_variant="-moe"` 进入镜像名（[L73](https://github.com/allenai/open-instruct/blob/a4cd966a9e202843baba653115045e6fd6f473db/scripts/train/build_image_and_launch.sh#L73)）并作为 `--build-arg IMAGE_VARIANT` 传给 docker build（[L86-L94](https://github.com/allenai/open-instruct/blob/a4cd966a9e202843baba653115045e6fd6f473db/scripts/train/build_image_and_launch.sh#L86-L94)）。这与 u1-l4 讲过的「干净工作区 + 按 commit 复用镜像」流程完全共用。

**④ GPU 测试的集群路由**（[scripts/test/run_gpu_pytest.sh:L14-L23](https://github.com/allenai/open-instruct/blob/a4cd966a9e202843baba653115045e6fd6f473db/scripts/test/run_gpu_pytest.sh#L14-L23)）：按镜像名整串后缀匹配 `*-cuda13|*-cuda13-moe` 才去 `ai2/holmes`，其余按 CUDA 12 处理。#2298 之前只有 `*-cuda13` 一个模式——`-cuda13-moe` 镜像会被误路由到 CUDA 12 集群（在错误驱动版本的机器上跑 CUDA 13 镜像）。

**⑤ 运行期防线及其调用点**（[open_instruct/olmo_core_utils.py:L575-L587](https://github.com/allenai/open-instruct/blob/a4cd966a9e202843baba653115045e6fd6f473db/open_instruct/olmo_core_utils.py#L575-L587)）：OLMo-core 在 TE 缺失时把 permutation 算子守卫式地置为 `None`，没有检查的话模型要到**第一次 forward** 才以难懂的方式崩掉。`require_transformer_engine()` 检查 `oc_moe_utils.moe_permute is None` 并抛出带修复指引的 `RuntimeError`——报错文本直接给出正确命令 `scripts/train/build_image_and_launch.sh --cuda-version 13 --moe SCRIPT`。调用点在训练入口（[open_instruct/olmo_core_finetune.py:L648-L658](https://github.com/allenai/open-instruct/blob/a4cd966a9e202843baba653115045e6fd6f473db/open_instruct/olmo_core_finetune.py#L648-L658)）：`setup_model` 之后判定 `isinstance(model, ddp_model_lib.OLMoDDPModel)`，是 MoE v2 才检查——dense 模型不需要 TE，不应被这个检查挡住。

回到配方视角：a4b 固定的镜像 `01M7VCQM65SD251KYS4Z1AD8FG` 由 a4b-bigbatch 分支线（含 #2291，open-instruct e436f7647）构建，该镜像同时携带 main 没有的 `scripts/c12/a4b_sft_config.json`（[scripts/train/sft.py:L115-L121](https://github.com/allenai/open-instruct/blob/a4cd966a9e202843baba653115045e6fd6f473db/scripts/train/sft.py#L115-L121) 注释：EMO off、sync_1d EP、flash_2、整块重计算与嵌套 checkpoint 关闭都写在这个配置里）。也就是说**即使在 main 上 dry_run 成功，真正执行时读的还是镜像里的那份代码与配置**。

#### 4.3.4 代码实践

1. **实践目标**：亲自确认「默认镜像上启动 a4b 会失败」的失败方式与正确命令。
2. **操作步骤**：(a) 读 [open_instruct/olmo_core_utils.py:L575-L587](https://github.com/allenai/open-instruct/blob/a4cd966a9e202843baba653115045e6fd6f473db/open_instruct/olmo_core_utils.py#L575-L587)，抄下 `RuntimeError` 的完整报错文本；(b) 读 [scripts/train/sft.py:L115-L121](https://github.com/allenai/open-instruct/blob/a4cd966a9e202843baba653115045e6fd6f473db/scripts/train/sft.py#L115-L121)，确认 a4b 的 image 字段与 c12r 不同；(c) 用 `git log --oneline -- Dockerfile` 找到 #2298 提交并 `git show` 它，对照 `--moe` 分支与 `nm` 检查的引入位置。
3. **需要观察的现象**：报错文本中给出的修复命令、`--moe` 对 CUDA 版本的约束、构建期符号检查的 `exit 1` 语义。
4. **预期结果**：能回答——默认镜像（无 TE）上手动跑 A4B 时，`setup_model` 判定 MoE v2 后 `require_transformer_engine()` 立即抛 RuntimeError（不是第一次 forward 才崩）；要自己构建镜像线，正确命令是 `./scripts/train/build_image_and_launch.sh --cuda-version 13 --moe SCRIPT`；而走配方启动器，正确命令就是 `uv run python scripts/train/sft.py --recipe a4b`（镜像已固定，无需构建）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 cuda13-moe 用 13.3.1-**cudnn** 基座而 cuda13 用 13.0.3-devel？
**答案**：TE 的 torch 绑定要从源码编译，需要 venv 之外可用的 cuDNN 开发文件；13.3.1-cudnn-devel 基座同时提供新版系统 cuBLAS（供符号替换用）与 cuDNN（Dockerfile 注释 L88-95 与安装块 L100-105）。

**练习 2**：`DS_SKIP_CUDA_CHECK=1` 会不会影响默认镜像的行为？
**答案**：不会。它写在 cuda13-moe 命名阶段内，只有最终 `FROM cuda13-moe` 的变体镜像继承它；cuda12/cuda13 的最终阶段从各自基座起建，看不到这个 ENV。

**练习 3**：配方直接引用现成镜像 ID（`01M7VCQM...`），而仓库又有 `--cuda-version 13 --moe` 构建入口，两者什么关系？
**答案**：配方走「已经构建好、已冒烟验证」的镜像，保证可复现；`--moe` 入口让你从**当前 checkout** 构建同类变体镜像（例如你要改训练代码重跑 MoE v2 时）。前者固定不动，后者随 commit 变化。

### 4.4 配方不变量测试：tests/test_sft_recipes.py

#### 4.4.1 概念说明

启动器本身没有 GPU 逻辑，最容易出错的地方是「配方字典 → mason 命令」的**展开**：漏一个 `--env`、镜像字段拼错、训练命令没跟上 `--`，都要到提交任务后才暴露。`tests/test_sft_recipes.py` 的思路是：`--dry_run` 路径不需要 Beaker（whoami 失败时用 `<user>` 占位），所以可以在 CI 里真实执行 `sft.py main()`，再对**打印出的命令**做断言——把「计划一次发射」本身当成被测行为（文件 docstring 原话：every recipe plans a launch, and the A4B recipe keeps what makes it run）。

#### 4.4.2 核心流程

```text
importlib 按 fixture 路径加载 scripts/train/sft.py（它不是包内模块）
        ↓
plan()：monkeypatch 掉 subprocess.run（连 beaker whoami 都不真正执行）
        设 sys.argv = ["sft.py", --recipe, name, --dry_run] → 调 sft.main()
        从 capsys 抓 stdout 最后一行 → shlex.split 成参数列表
        ↓
断言配方不变量：--image / --num_nodes / --cluster / --max_train_steps /
--data_loader_seed / --run_name / EXPECTED_NUMPY_CACHE / 每个 recipe env 前面是 --env /
"--" 之后以 torchrun --nnodes=<nodes> 开头
```

#### 4.4.3 源码精读

**① 把脚本当模块加载**（[tests/test_sft_recipes.py:L10-L14](https://github.com/allenai/open-instruct/blob/a4cd966a9e202843baba653115045e6fd6f473db/tests/test_sft_recipes.py#L10-L14)）：`scripts/train/` 不是包，测试用 `importlib.util.spec_from_file_location` 直接从路径加载并 `exec_module`——这也顺带执行了模块顶层的 `RECIPES` 定义。

**② plan 辅助函数**（[tests/test_sft_recipes.py:L17-L28](https://github.com/allenai/open-instruct/blob/a4cd966a9e202843baba653115045e6fd6f473db/tests/test_sft_recipes.py#L17-L28)）：把 `sft.subprocess.run` 换成必抛 `OSError` 的假函数，使 whoami 走进 dry_run 的占位分支；断言第一行输出以 `Output: /weka/oe-adapt-default/allennlp/deletable_checkpoint/<user>/sft/` 开头，然后取**最后一行**（mason 命令）做 `shlex.split`。

**③ 逐配方参数化断言**（[tests/test_sft_recipes.py:L35-L50](https://github.com/allenai/open-instruct/blob/a4cd966a9e202843baba653115045e6fd6f473db/tests/test_sft_recipes.py#L35-L50)）：`@pytest.mark.parametrize` 遍历 `sorted(sft.RECIPES)`，**新增配方自动纳入测试**。断言逐项对照：`--image` 等于配方字段、`--num_nodes`/`--cluster`/`--max_train_steps`/`--data_loader_seed` 同理、`--run_name` 是 `<name>-s<seed>`、`EXPECTED_NUMPY_CACHE=<cache>` 在参数列表里、配方的每个 env 字符串**前一个参数是 `--env`**（L46-47 用 `cmd.index(env) - 1` 检查，防止 env 被拼进训练命令）、`--` 之后前两项是 `torchrun --nnodes=<nodes>`（L49-50）。

**④ a4b 专属不变量**（[tests/test_sft_recipes.py:L53-L71](https://github.com/allenai/open-instruct/blob/a4cd966a9e202843baba653115045e6fd6f473db/tests/test_sft_recipes.py#L53-L71)）：这一组断言编码了 4.2/4.3 节讲的所有「a4b 之所以能跑」的条件——镜像**不同于** c12r（L55-56 注释直说：main 的镜像没有 TE）；`--config_name` 指向镜像内的 `scripts/c12/a4b_sft_config.json`；基座路径前缀正确；`--expert_parallel_degree` 为 8；`--loss_implementation` 为 `fused_linear`；LR 为 7.3e-5；含 `--no_compile_model` 与分配器 env；最后把 `--mixer_list` 到 `--local_cache_dir` 之间的整段（即完整 mixer）与 c12r 逐参数对比，连同 `--extra_numpy_dirs`/`--seed`/`--max_train_steps`/`--max_seq_length` 一起断言相等——**用测试锁死「a4b 与 c12r 数据侧零差异」这个实验设计**。

#### 4.4.4 代码实践

1. **实践目标**：在本地跑通配方不变量测试，并观察它如何抓住一个故意注入的错误。
2. **操作步骤**：(a) 运行 `uv run pytest tests/test_sft_recipes.py -v`；(b) 临时把 `scripts/train/sft.py` 里 a4b 的 `"learning_rate"` 值改为 `7.4e-5`（改完**记得还原**，本仓库不允许留下源码修改），再跑一次同一条 pytest 命令观察失败信息；(c) 还原后重跑确认全绿。
3. **需要观察的现象**：三个用例（c12r、a4b 的参数化用例 + a4b 专属用例）通过；注入错误后 `test_a4b_recipe` 在 `assert value(cmd, "--learning_rate") == "7.3e-5"` 处失败。
4. **预期结果**：全部通过；改动学习率后恰好一条断言失败并指出实际值。（待本地验证：需已 `uv sync`；步骤 (b) 只在本地实验，勿提交。）

#### 4.4.5 小练习与答案

**练习 1**：为什么测试要专门断言 `EXPECTED_NUMPY_CACHE=...` 在**命令参数**里，而不是去检查缓存目录存在？
**答案**：这是「展开不变量」测试：它验证启动器把配方字段正确注入 mason 命令。缓存目录是否存在是 Weka 上的运行时事实，单测环境既无法访问、也不该访问——那属于作业启动后训练入口的职责（缓存不命中会硬失败，见 4.1.3 ⑤）。

**练习 2**：`cmd[cmd.index(env) - 1] == "--env"` 这个看似奇怪的断言防的是什么 bug？
**答案**：防止配方 env 列表被**拼错位置**——比如忘了 `--env` 前缀，`PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True` 就会落到 `--` 之后的训练命令里，变成一个无效参数。断言用「env 字符串前一个 token 必须是 `--env`」锁住注入点（[tests/test_sft_recipes.py:L46-L47](https://github.com/allenai/open-instruct/blob/a4cd966a9e202843baba653115045e6fd6f473db/tests/test_sft_recipes.py#L46-L47)）。

**练习 3**：新增第三个配方时，测试需要怎么改？
**答案**：`test_recipe_plans_a_launch` 因 parametrize 自动覆盖新配方，不需要改；只有当新配方有自己的「必须保真」的差异点（像 a4b 的 TE 镜像、EP8、fused_linear）时，才补一个类似 `test_a4b_recipe` 的专属用例。

## 5. 综合实践

把本讲四个模块串起来的任务（全程无需 GPU，也无需提交 Beaker 任务）：

**第一步：画出展开流程图。** 对照 [scripts/train/sft.py:L249-L283](https://github.com/allenai/open-instruct/blob/a4cd966a9e202843baba653115045e6fd6f473db/scripts/train/sft.py#L249-L283)，绘制「本地命令 → 配方展开 → mason 标志 → Beaker 任务」的流程图：`--image/--cluster/--num_nodes/--min_runtime/--timeout` 各标一个来源字段；`--env` 标三个来源（固定的 `OLMO_SHARED_FS=1`、`EXPECTED_NUMPY_CACHE`、recipe env 列表）；`--` 之后标出 torchrun 前缀与 `olmo_core_finetune.py` 及其参数的三个来源（`--checkpoint` 覆盖项、`recipe["train_args"]`、main 里现算的 `--max_train_steps`/`--checkpointing_steps`/`--ephemeral_save_interval`/`--data_loader_seed`/`--run_name`/`--output_dir`）。

**第二步：dry_run 并标注。** 运行 `uv run python scripts/train/sft.py --recipe a4b --dry_run`，在打印的命令上用三种颜色/记号分别标出：来自 `recipe["env"]` 的参数（应只有 `PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True`）、来自 `recipe["train_args"]` 的参数（从 `--config_name` 到 `--loss_implementation fused_linear`）、来自 main 现算的参数（`--max_train_steps 18410`、`--checkpointing_steps 9205`、`--ephemeral_save_interval 1000`、`--data_loader_seed 34521`、`--run_name`、`--output_dir`）。再解释 `EXPECTED_NUMPY_CACHE` 与 `--extra_numpy_dirs` 如何让 a4b 复用 c12r 的冻结缓存：两个配方 mixer/分词器/模板/`--seed 33333`/`--max_seq_length 65536` 全同 → `compute_config_hash` 与种子后缀相同 → 解析到同一 `<hash>-6068a350-rowaligned` 目录，replay 数据由 `--extra_numpy_dirs` 并入（作业 cache 目录内是指向冻结缓存的符号链接）。

**第三步：回答「为什么默认镜像跑不了 a4b」。** 对照 [Dockerfile:L88-L112](https://github.com/allenai/open-instruct/blob/a4cd966a9e202843baba653115045e6fd6f473db/Dockerfile#L88-L112) 与 [open_instruct/olmo_core_utils.py:L575-L587](https://github.com/allenai/open-instruct/blob/a4cd966a9e202843baba653115045e6fd6f473db/open_instruct/olmo_core_utils.py#L575-L587) 写三句话答案：MoE v2 的 token permutation 依赖 TE；默认镜像不装 TE（OLMo-core 把算子置 None）；在默认镜像上启动会在 `setup_model` 判定 `OLMoDDPModel` 后立即抛出带修复指引的 RuntimeError。正确命令：配方式 `uv run python scripts/train/sft.py --recipe a4b`；自建镜像式 `./scripts/train/build_image_and_launch.sh --cuda-version 13 --moe SCRIPT`。

**第四步：a4b 相对 c12r 的五个关键差异表。** 每项一句话动机（参考答案）：

| # | 差异 | 动机（一句话） |
|---|---|---|
| 1 | 镜像 `01M5KX…` → `01M7VC…` | MoE v2 需要 transformer-engine 与镜像内的 a4B 模型配置，main 镜像两者皆无 |
| 2 | 基座 dense B1 step20000 → A4B hero 4T decay 端点 step162974 | 实验要隔离的变量就是基座本身 |
| 3 | 峰值 LR 2e-4 → 7.3e-5 | 按两基座预训练 LR 之比（4.04e-4 / 1.1e-3）等比缩放，避免对低 LR 收敛基座扰动过大 |
| 4 | 新增 EP8 + fused_linear CE + expandable_segments + flash_2/整块重计算组合 | MoE 专家分片放得下、65k 词表 logits 不物化、防碎片 OOM、绕开 FA4 在 SM100 上拒绝 head_dim 256 |
| 5 | timeout 14h→72h、min_runtime 45m→2h15m | 步速慢约四倍（约 7.4 s/step 外推），首个临时 checkpoint 约第 2 小时，预算随之放大 |

**第五步（可选）：跑不变量测试。** `uv run pytest tests/test_sft_recipes.py -v`，把四个用例与你流程图里的节点对应起来。

## 6. 本讲小结

- **配方 = 固化的一次运行**：`RECIPES` 字典固定镜像、基座、数据、超参、集群与预算；训练代码来自镜像而非 checkout，重复执行同名命令从无时间戳输出目录的最新 checkpoint 断点续跑。
- **启动器只做展开**：`main` 把配方翻译成 mason 命令（`--image/--cluster/--num_nodes/--env/--min_runtime/--timeout` + `--` 后的 torchrun 训练命令），`--dry_run` 让这一切无需 Beaker 即可检查。
- **a4b = c12r 的换基座臂**：数据、缓存（`bf82a8fab8-6068a350-rowaligned`）、种子、18,410 步全同；增量是 TE 镜像、A4B 基座、7.3e-5 的缩放 LR、EP8/fused_linear/expandable_segments 的显存工程与放大的超时预算；速度数字是外推估计，固定镜像仅过 40 步冒烟，尚无完整运行。
- **cuda13-moe 镜像变体是三层防线**：构建期装 TE 并用 `nm` 验证 cuBLAS 符号；启动期 `--cuda-version 13 --moe` 路由与 `-cuda13-moe` 的 GPU 测试集群路由；运行期 `require_transformer_engine` 在 MoE v2 判定后立即给出带修复命令的报错。
- **不变量测试锁住展开正确性**：`tests/test_sft_recipes.py` 用 dry_run 输出断言每个配方的 mason 展开（镜像/节点/集群/步数/种子/缓存/逐项 env/torchrun 前缀），并用 a4b 专属用例锁住「数据侧零差异 + TE 镜像」这两条实验前提。

## 7. 下一步学习建议

- **往下钻训练入口**：回到 u7-l1 的 `open_instruct/olmo_core_finetune.py`，重点读 `--expert_parallel_degree` 如何进入 `TransformerDataParallelConfig`（本讲 [L648-L668](https://github.com/allenai/open-instruct/blob/a4cd966a9e202843baba653115045e6fd6f473db/open_instruct/olmo_core_finetune.py#L648-L668) 之后的并行配置组装），理解 EP8 与 HSDP 的关系。
- **学 checkpoint 导出**：SFT 配方产出的 OLMo-core checkpoint 如何转成 HF 格式去做评估——阅读 u7-l4（`convert_moe_checkpoint_to_hf.py`），其中 `OLMoDDP` 融合优化器的 `.main` 权重布局正是 MoE v2 训练模块的保存格式。
- **对照另一种 launch 层**：u9-l1 精读 mason.py 本体（sft.py 只是它的一个调用方），理解 `--extra_weka_buckets`、`--env` 与 `--` 后命令在 mason 内部如何变成 Beaker 任务规格。
- **延伸到 MILES**：若你对「固定镜像 + 断点恢复 + 不变量测试」这套工程模式感兴趣，u8-l2/u8-l15 展示了 MILES 配置系统（TOML → RunSpec → RunConfig）如何把同样的思想做成了完整的 schema 驱动体系。
