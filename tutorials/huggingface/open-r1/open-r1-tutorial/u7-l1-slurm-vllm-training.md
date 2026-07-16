# Slurm 集群训练与 vLLM 服务

> 本讲属于「大规模训练基础设施」单元（u7），承接 SFT 主流程（u2-l1）与 GRPO 主流程（u3-l1）。
> 前面那些讲义里，我们一直把训练写成「在本机一条命令跑起来」；本讲要回答的是：**当模型变大、需要几十张 GPU 时，open-r1 如何在 Slurm 集群上把它编排起来，并额外拉起一台 vLLM 服务节点来喂 GRPO 的在线采样。**

---

## 1. 本讲目标

学完本讲，你应当能够：

1. 读懂 `slurm/train.slurm` 从参数解析到最终启动的整条链路，说清每一行在干什么。
2. 理解「三元组参数（`--model/--task/--config/--accelerator`）如何定位到唯一的 YAML 配方」。
3. 掌握 `use_vllm: true` 时的 **N+1 节点拓扑**：1 台跑 vLLM 服务、N 台跑训练，以及为什么要把 vLLM 单独拆出来。
4. 手算给定节点数时 `WORLD_SIZE` 的推导过程，并解释 `--dp`/`--tp` 与单节点 8 张 GPU 的关系。
5. 理解 `srun + accelerate launch` 这套「Slurn 任务分发 + 分布式训练启动器」的组合拳。

---

## 2. 前置知识

本讲会用到一些集群与分布式训练的术语，先用大白话过一遍：

- **Slurm**：高性能计算集群的「排程器」。你把一份脚本交给它（`sbatch xxx.slurm`），它负责找空闲节点、把脚本里 `#SBATCH` 开头的「指令」读成资源申请（要几台机器、每台几张 GPU、跑多久），然后把脚本真正派发到分配到的节点上执行。
- **`#SBATCH` 指令（directive）**：写在脚本顶部、以 `#SBATCH` 开头的注释行。对 Slurm 来说它们不是注释，而是资源申请单。
- **`srun`**：Slurm 的「任务启动器」，在分配到的节点上跑一条命令；可以指定在哪些节点（`--nodelist`）、跑几个任务（`--ntasks`）。
- **`scontrol show hostnames`**：把 Slurm 分配到的节点列表展开成真实主机名，供脚本拼接。
- **环境变量**：Slurm 在脚本运行时会注入一批变量，本讲用到 `SLURM_NNODES`（节点总数）、`SLURM_JOB_NODELIST`（节点列表字符串）、`SLURM_PROCID`（当前任务在全作业中的序号）。
- **`accelerate launch`**：Hugging Face Accelerate 的分布式启动器，负责把一个普通训练脚本拉成多机多卡进程组（设置 rank、world size、rendezvous 地址等）。
- **vLLM 服务**：一个高性能推理引擎。在 GRPO 训练里，模型每个 step 都要**在线采样**大量回答（见 u3-l1 的 `num_generations`），这件事用训练框架自带的生成太慢，所以单独起一个 vLLM 服务来加速。
- **WORLD_SIZE**：分布式训练里「参与训练的进程总数」，通常等于「训练用的 GPU 总数」。

> 与 u3-l1 的衔接：GRPO 之所以需要 vLLM，正是因为它的「组采样」开销极大（每个 prompt 要采 `num_generations` 条回答）。本讲讲的，就是怎么在集群上把「vLLM 采样」和「训练更新」两件事**物理地拆到不同节点**上。

---

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [slurm/train.slurm](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/slurm/train.slurm) | **本讲主角**。把 `sbatch` 参数翻译成一次多机训练，并按需拉起 vLLM 服务节点。 |
| [slurm/README.md](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/slurm/README.md) | 记录用 SGLang 在 2×8 H100 上**部署推理服务**（`serve_r1.slurm`）的步骤。它是「服务」侧的相邻文档，与训练脚本是两条独立链路，了解即可。 |
| [slurm/generate.slurm](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/slurm/generate.slurm) | 蒸馏数据生成（u4-l1）的集群脚本，用 **Ray** 拉起 vLLM。本讲在「两种 vLLM 部署方式对比」时引用它。 |
| [recipes/Qwen2.5-1.5B-Instruct/grpo/config_demo.yaml](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/recipes/Qwen2.5-1.5B-Instruct/grpo/config_demo.yaml) | GRPO 示例配方，其中 `use_vllm: true` 是触发本讲 N+1 拓扑的开关。 |

---

## 4. 核心概念与源码讲解

本讲把 `train.slurm` 拆成三个最小模块：

1. **参数解析与配置文件定位**——从 `--model/--task/--config` 拼出 YAML 路径，并从中「抠」出训练启动所需的关键字段。
2. **`use_vllm` 分支：N+1 节点拓扑拆分**——检测是否要 vLLM，若是则把最后一台节点拨给 vLLM 服务。
3. **`srun + accelerate launch` 多机启动**——用 Slurm 把训练进程派发到各节点，再用 Accelerate 拉起分布式进程组。

---

### 4.1 参数解析与配置文件定位

#### 4.1.1 概念说明

`train.slurm` 自身**不包含任何模型/训练参数**，它只是个「编排器」。真正的超参全在 YAML 配方里（见 u1-l4 的三元组配置）。因此脚本第一件正事是：**把命令行参数翻译成 YAML 文件路径，再从该文件里抠出几样「Slurm 脚本层面需要知道」的字段**。

需要从 YAML 里抠出来的字段有两类：

- **给 Slurm/accelerate 用的**：`gradient_accumulation_steps`（梯度累积步数，accelerate 要知道它来算有效 batch）。
- **给 vLLM 服务用的**：`model_name_or_path`（要加载哪个模型）、`model_revision`（模型版本）。

这里有一个**容易踩坑的关键点**：你在 `sbatch` 命令里传的 `--model Qwen2.5-1.5B-Instruct`，并不是最终拿来加载的模型名，它只是用来**定位配方目录** `recipes/Qwen2.5-1.5B-Instruct/...`；真正喂给 vLLM 的模型名，是从 YAML 里 `grep` 出来的 `model_name_or_path`（例如 `Qwen/Qwen2.5-1.5B-Instruct`）。换句话说，`MODEL` 这个 shell 变量会在脚本中途被**覆盖**一次。

#### 4.1.2 核心流程

```text
sbatch ... --model X --task grpo --config demo --accelerator zero2 [--dp 4 --tp 2 --args "..."]
        │
        ▼
while 循环解析命令行 → MODEL / TASK / CONFIG_SUFFIX / ACCELERATOR / DP / TP / OPTIONAL_ARGS
        │
        ▼  (拼路径)
CONFIG_FILE = recipes/$MODEL/$TASK/config_$CONFIG_SUFFIX.yaml
        │
        ▼  (从 YAML 抠字段)
GRAD_ACC_STEPS = grep gradient_accumulation_steps
MODEL          = grep model_name_or_path      ← 覆盖！
REVISION       = grep model_revision
        │
        ▼  (若 --args 里显式给了 --gradient_accumulation_steps=，则再次覆盖 GRAD_ACC_STEPS)
```

#### 4.1.3 源码精读

**参数解析：一个标准的 `while/case` 循环。** 每识别一个 `--xxx` 就 `shift 2`（吃掉键和值两格）。未知键直接报错退出：

[slurm/train.slurm:L49-L85](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/slurm/train.slurm#L49-L85) —— 把 `--model/--task/--config/--accelerator/--dp/--tp/--args` 七个键依次读进 shell 变量。`--dp`/`--tp` 默认为 1（[L44-L45](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/slurm/train.slurm#L44-L45)），分别对应 vLLM 的 data/tensor 并行度（见 4.2）。

**必填校验：** 四个核心参数缺一不可：

[slurm/train.slurm:L87-L92](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/slurm/train.slurm#L87-L92) —— `MODEL/TASK/CONFIG_SUFFIX/ACCELERATOR` 任一为空就报错。

**拼出 YAML 路径并抠出 `gradient_accumulation_steps`：**

[slurm/train.slurm:L95-L96](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/slurm/train.slurm#L95-L96) —— 注意路径模板 `recipes/$MODEL/$TASK/config_$CONFIG_SUFFIX.yaml`。例如 `--model Qwen2.5-1.5B-Instruct --task grpo --config demo` 会得到 `recipes/Qwen2.5-1.5B-Instruct/grpo/config_demo.yaml`。`grep ... | awk '{print $2}'` 取 YAML 行里冒号后的第二个字段（值）。

**`--args` 覆盖逻辑：** 用户可能不想动 YAML，而想临时改梯度累积。脚本把 `--args` 拆成数组，扫描其中是否含 `--gradient_accumulation_steps=NN`：

[slurm/train.slurm:L98-L107](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/slurm/train.slurm#L98-L107) —— `${arg#*=}` 是 bash 参数展开，去掉前缀到等号，只留值；找到即 `break`。这保证命令行临时覆盖优先于 YAML 默认值。

**覆盖 MODEL / 取 REVISION：**

[slurm/train.slurm:L111-L112](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/slurm/train.slurm#L111-L112) —— 到这里 `MODEL` 变量被 YAML 里的真实模型 id（如 `Qwen/Qwen2.5-1.5B-Instruct`）覆盖，随后它被用于 `trl vllm-serve --model $MODEL`（见 4.2.3）。`REVISION` 取了 `head -n 1` 防止 YAML 里多次出现该键时取错。

#### 4.1.4 代码实践

**实践目标**：不依赖集群，在本地 bash 里验证「参数 → YAML 路径 → 字段抠取」这条链路是否如你预期。

**操作步骤**（在仓库根目录执行）：

1. 模拟脚本拼路径的过程：

```bash
MODEL="Qwen2.5-1.5B-Instruct"
TASK="grpo"
CONFIG_SUFFIX="demo"
ACCELERATOR="zero2"
CONFIG_FILE="recipes/$MODEL/$TASK/config_$CONFIG_SUFFIX.yaml"
echo "$CONFIG_FILE"
# 预期输出：recipes/Qwen2.5-1.5B-Instruct/grpo/config_demo.yaml
```

2. 用脚本里**同样的** `grep | awk` 抠出三个字段，并与 YAML 原文对照：

```bash
grep 'gradient_accumulation_steps' "$CONFIG_FILE" | awk '{print $2}'
grep 'model_name_or_path:' "$CONFIG_FILE" | awk '{print $2}'
grep 'model_revision:' "$CONFIG_FILE" | head -n 1 | awk '{print $2}'
```

3. 模拟 `--args` 覆盖：

```bash
OPTIONAL_ARGS="--learning_rate=1e-4 --gradient_accumulation_steps=8"
GRAD_ACC_STEPS=$(grep 'gradient_accumulation_steps' "$CONFIG_FILE" | awk '{print $2}')
IFS=' ' read -ra ARGS <<< "$OPTIONAL_ARGS"
for arg in "${ARGS[@]}"; do
  if [[ "$arg" == "--gradient_accumulation_steps="* ]]; then
    GRAD_ACC_STEPS="${arg#*=}"; break
  fi
done
echo "$GRAD_ACC_STEPS"   # 预期：8（被 --args 覆盖，而非 YAML 里的 4）
```

**需要观察的现象**：第 2 步三个 `grep` 分别输出 `4`、`Qwen/Qwen2.5-1.5B-Instruct`、`main`；第 3 步最终 `GRAD_ACC_STEPS` 为 `8`，证明命令行覆盖生效。

**预期结果**：与 [config_demo.yaml](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/recipes/Qwen2.5-1.5B-Instruct/grpo/config_demo.yaml) 中的 `gradient_accumulation_steps: 4`、`model_name_or_path: Qwen/Qwen2.5-1.5B-Instruct` 一致。

#### 4.1.5 小练习与答案

**练习 1**：如果 YAML 里没有 `gradient_accumulation_steps` 这一行，`GRAD_ACC_STEPS` 会是什么？会出错吗？

> **答案**：`grep` 匹配不到时返回空字符串，`awk` 也就无输入，`GRAD_ACC_STEPS` 为空。脚本本身不会在这里报错，但随后 `accelerate launch --gradient_accumulation_steps `（值为空）会传一个非法参数，**训练会在启动阶段失败**。所以配方里务必写明该字段。

**练习 2**：为什么 `MODEL` 变量在 L111 被覆盖，而不是直接沿用 `--model` 传入的值？

> **答案**：`--model` 用的是「目录名」约定（不带组织前缀，如 `Qwen2.5-1.5B-Instruct`），而 vLLM 服务和训练脚本需要的是「Hub 模型 id」（如 `Qwen/Qwen2.5-1.5B-Instruct`）。两者来源不同，故从 YAML 的 `model_name_or_path` 取真值覆盖，保证 `trl vllm-serve --model` 拿到的是可被下载的完整 id。

---

### 4.2 use_vllm 分支：N+1 节点拓扑拆分

#### 4.2.1 概念说明

这是本讲最有 open-r1 特色的一块。回顾 u3-l1：GRPO 训练每个 step 都要**对大量 prompt 在线采样回答**。这件事如果交给训练框架自己的 `.generate()`，速度会成为整个训练的瓶颈。open-r1 的做法是：**单独起一个 vLLM 推理服务**，训练进程通过 HTTP 把 prompt 发给它、拿回采样结果。

于是在集群上就出现一个问题：vLLM 服务和训练进程**抢不抢同一批 GPU**？

open-r1 选择**物理隔离**——这就是 **N+1 拓扑**：

- **1 台节点**：专职跑 vLLM 服务（`trl vllm-serve`）。
- **N 台节点**：专职跑训练（`accelerate launch`）。

为什么要拆开？因为 vLLM 会**独占**它所在节点的全部显存去做 KV cache 和连续批处理，与训练的梯度/优化器状态放一起会 OOM；分到不同节点，两者各用各的 8 张卡，互不干扰，且 vLLM 能把那一整台机器的吞吐榨满。

触发条件不是命令行参数，而是**YAML 配方里写了 `use_vllm: true`**。脚本通过 `grep` 检测这一行来决定是否进入 N+1 分支。在当前仓库里，所有 GRPO 配方都开启了它（SFT 配方则不会）。

#### 4.2.2 核心流程

先算「全量」规模，再决定要不要切一块给 vLLM：

```text
NUM_NODES  = SLURM_NNODES                      # 例如 2
GPUS_PER_NODE = 8
WORLD_SIZE = NUM_NODES × GPUS_PER_NODE         # 例如 16
NODELIST   = 展开成主机名数组 [n0, n1, ...]
MASTER_ADDR = NODELIST[0]                       # 第一个节点做 rendezvous 主节点
TRAIN_NODES = NODELIST[全部]                    # 先假定都在训练

if YAML 含 'use_vllm: true':
    TRAIN_NODES = NODELIST[0 : NUM_NODES-1]     # 前 N-1 台训练（切片）
    VLLM_NODE   = NODELIST[-1]                  # 最后一台给 vLLM
    WORLD_SIZE  = WORLD_SIZE - GPUS_PER_NODE    # 训练 world size 减 8
    NUM_NODES   = NUM_NODES - 1                 # 训练节点数减 1
    在 VLLM_NODE 上后台启动 trl vllm-serve（用 --tp / --dp）
    把 vllm_server_host 注入训练参数
```

`WORLD_SIZE` 的数学表达：

\[
\text{WORLD\_SIZE} = \text{NUM\_NODES} \times \text{GPUS\_PER\_NODE}
\]

进入 vLLM 分支后，训练侧规模变为：

\[
\text{WORLD\_SIZE}_{\text{train}} = \text{WORLD\_SIZE} - \text{GPUS\_PER\_NODE}, \quad
\text{NUM\_NODES}_{\text{train}} = \text{NUM\_NODES} - 1
\]

而 vLLM 节点的 GPU 占用由 `--dp`/`--tp` 决定，理想情况下应恰好填满一台机器：

\[
\text{TP} \times \text{DP} = \text{GPUS\_PER\_NODE} = 8
\]

例如 `--tp 2 --dp 4` 时 \(2 \times 4 = 8\)，正好用满 vLLM 节点的 8 张卡。

#### 4.2.3 源码精读

**先算全量规模与节点列表：**

[slurm/train.slurm:L114-L121](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/slurm/train.slurm#L114-L121) —— `scontrol show hostnames $SLURM_JOB_NODELIST` 把 Slurm 分配的节点展开成数组；`MASTER_ADDR` 取第一个，后面作为 accelerate 的 rendezvous 主地址；`TRAIN_NODES` 先初始化为全部节点。

**检测 `use_vllm`：**

[slurm/train.slurm:L123-L126](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/slurm/train.slurm#L123-L126) —— 用 `grep -qE '^\s*use_vllm:\s*true'` 检测 YAML 中是否存在（允许行首空格的）`use_vllm: true`。注意这是**纯文本扫描**，不解析 YAML，因此格式必须严格匹配（`true` 不能写成 `True`/`yes`）。这也解释了为什么 SFT 配方（无此行）走纯训练、GRPO 配方（有此行）走 N+1。

**核心拆分 + 后台拉起 vLLM：**

[slurm/train.slurm:L128-L136](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/slurm/train.slurm#L128-L136) —— 这是本讲最关键的几行，逐句拆：

- `TRAIN_NODES=("${NODELIST[@]:0:$((NUM_NODES - 1))}")`：bash 数组切片，取前 `NUM_NODES-1` 个元素作为训练节点。
- `VLLM_NODE=${NODELIST[-1]}`：取数组**最后一个**元素作为 vLLM 节点（bash 负数下标）。
- `WORLD_SIZE=$((WORLD_SIZE - GPUS_PER_NODE))` 与 `NUM_NODES=$((NUM_NODES - 1))`：训练规模各减一节点/八卡。
- `srun ... trl vllm-serve --model $MODEL --revision $REVISION --tensor_parallel_size $TP --data_parallel_size $DP &`：在 vLLM 节点上**后台**（末尾 `&`）启动 vLLM 服务，`trl vllm-serve` 是 trl 提供的 vLLM 服务封装命令，`--tp`/`--dp` 即 tensor / data 并行度。
- `OPTIONAL_ARGS="$OPTIONAL_ARGS --vllm_server_host=$VLLM_NODE"`：把 vLLM 节点的主机名塞进训练参数，GRPO 训练进程据此知道去哪里请求采样（该参数最终被 trl 的 GRPOConfig 消费）。

> 注意 `--vllm_server_host=$VLLM_NODE` 是一个**未在 open-r1 自身源码里出现**的字段——它是 trl 库 `GRPOConfig` 的标准字段（与 `use_vllm` 配套），open-r1 只是透传。这与 u1-l4 所讲「open-r1 大量字段继承自 trl」一致。

#### 4.2.4 代码实践

**实践目标**：手算一个具体拓扑，验证你对 N+1 拆分的理解。

**场景**：`sbatch --nodes=2 slurm/train.slurm --model Qwen2.5-1.5B-Instruct --task grpo --config demo --accelerator zero2 --dp 4 --tp 2`，且配方 `config_demo.yaml` 含 `use_vllm: true`。

**操作步骤**：在纸上（或在本地 bash 里用变量模拟）跟踪脚本：

```bash
NUM_NODES=2          # SLURM_NNODES
GPUS_PER_NODE=8
WORLD_SIZE=$((NUM_NODES * GPUS_PER_NODE))   # 16
NODELIST=(n0 n1)     # 假设两台主机名
MASTER_ADDR=${NODELIST[0]}                  # n0
TRAIN_NODES=("${NODELIST[@]}")              # [n0, n1]

USE_VLLM="true"
if [[ "$USE_VLLM" == "true" ]]; then
  TRAIN_NODES=("${NODELIST[@]:0:$((NUM_NODES - 1))}")  # [n0]
  VLLM_NODE=${NODELIST[-1]}                              # n1
  WORLD_SIZE=$((WORLD_SIZE - GPUS_PER_NODE))            # 8
  NUM_NODES=$((NUM_NODES - 1))                          # 1
fi
echo "vLLM 节点=$VLLM_NODE  训练节点=${TRAIN_NODES[*]}  训练 WORLD_SIZE=$WORLD_SIZE  训练 NUM_NODES=$NUM_NODES"
```

**需要观察的现象 / 预期结果**：

- **vLLM 节点 = `n1`**（最后一台），运行 `trl vllm-serve --tensor_parallel_size 2 --data_parallel_size 4`，占用 \(2 \times 4 = 8\) 张 GPU，正好用满该节点。
- **训练节点 = `n0`**（前 `NUM_NODES-1 = 1` 台）。
- **训练 `WORLD_SIZE = 8`**（一台 8 卡），`NUM_NODES = 1`。

**结论**：这就是「1 vLLM + 1 训练」的 1+1 拓扑；README 里也正是用这个例子说明 GRPO 的 N+1 部署（[README.md:L410-L414](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/README.md#L410-L414)）。

> **待本地验证**：以上为对脚本逻辑的手算结果；真实主机名由 Slurm 动态分配，需在实际集群 `sbatch` 后查看日志确认。

#### 4.2.5 小练习与答案

**练习 1**：如果 `--nodes=4`、`use_vllm: true`、`--dp 2 --tp 4`，训练用几台节点、几张 GPU？vLLM 用几张？

> **答案**：总 `WORLD_SIZE = 4×8 = 32`。进入 vLLM 分支后：训练 `NUM_NODES = 4-1 = 3`、`WORLD_SIZE = 32-8 = 24`（3 台 × 8 卡）；vLLM 占 1 台，`TP×DP = 4×2 = 8` 卡，恰好用满该节点。即 **3+1 拓扑**。

**练习 2**：若把 `--dp 4 --tp 4`（乘积 16）传给一台只有 8 卡的 vLLM 节点，会发生什么？

> **答案**：vLLM 需要 `TP×DP = 16` 张 GPU，但 vLLM 节点只有 8 张，`trl vllm-serve`（底层 vLLM）会因找不到足够 GPU 而启动失败。**配置 `--dp`/`--tp` 时应保证其乘积等于 `GPUS_PER_NODE`（8）**，这是隐含约束。

**练习 3**：脚本用 `grep` 检测 `use_vllm: true`。如果 YAML 写成 `use_vllm: True`（大写 T），会怎样？

> **答案**：脚本的正则是 `^\s*use_vllm:\s*true`（小写 `true`），匹配不到 `True`，于是 `USE_VLLM="false"`，**不会**拉起 vLLM 节点，整批节点全部用于训练——但训练脚本本身却声明了 `use_vllm: true`（YAML 解析时 `True` 仍会被当成布尔真），于是训练进程会去找一个并不存在的本地 vLLM，最终报错。**格式必须严格小写 `true`**。

---

### 4.3 srun + accelerate launch 多机启动

#### 4.3.1 概念说明

拆完节点，剩下两件事：把 vLLM 服务**后台**起好（4.2 已做），把训练进程**多机同时**拉起来。后者用的是一个经典组合：

- **`srun`（外层）**：负责「把命令派发到我指定的那几台训练节点上、每台跑一个任务」。它解决的是**进程在哪些物理机器上启动**。
- **`accelerate launch`（内层）**：负责「让这些进程互相认识、组成一个分布式进程组」。它解决的是**进程之间怎么通信**（谁是指挥官 rank 0、rendezvous 地址在哪、world size 多大）。

两者是**嵌套**关系：`srun ... bash -c "$LAUNCHER $CMD"`，即 srun 在每台训练节点上启动一个 bash，该 bash 再调用 `accelerate launch src/open_r1/grpo.py ...`。

`accelerate launch` 怎么知道每台机器的「身份」？关键在 `--machine_rank $SLURM_PROCID`——srun 给每台节点分配一个递增的 `SLURM_PROCID`（0,1,2,…），accelerate 拿它当本机的 rank。再加上统一的 `--main_process_ip` / `--main_process_port` 和 `--rdzv_backend=c10d`，各进程就能在 `MASTER_ADDR` 上完成会合（rendezvous）。

#### 4.3.2 核心流程

```text
CMD       = "src/open_r1/$TASK.py --config $CONFIG_FILE $OPTIONAL_ARGS"
LAUNCHER  = accelerate launch \
              --config_file recipes/accelerate_configs/$ACCELERATOR.yaml \
              --gradient_accumulation_steps $GRAD_ACC_STEPS \
              --num_machines   $NUM_NODES      \
              --num_processes  $WORLD_SIZE     \
              --main_process_ip   $MASTER_ADDR \
              --main_process_port $MASTER_PORT \
              --machine_rank   $SLURM_PROCID   \
              --rdzv_backend=c10d --max_restarts 1 --tee 3

srun --nodes=$NUM_NODES --ntasks=$NUM_NODES --nodelist=<训练节点逗号串> \
     --wait=60 --kill-on-bad-exit=1 \
     bash -c "$LAUNCHER $CMD"
```

要点：

- `--num_processes $WORLD_SIZE`：训练进程总数 = 训练用 GPU 总数。
- `--num_machines $NUM_NODES`：训练用机器数（进入 vLLM 分支后已减 1）。
- `--config_file .../$ACCELERATOR.yaml`：选哪份 DeepSpeed/FSDP/DDP 配置（见 u7-l2）。
- `--kill-on-bad-exit=1`：任一节点非零退出，立即终止整个作业，避免某台挂了还傻等。

#### 4.3.3 源码精读

**NCCL 环境变量（GPU 间通信健壮性）：**

[slurm/train.slurm:L138-L144](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/slurm/train.slurm#L138-L144) —— `NCCL_ASYNC_ERROR_HANDLING=1` 让 NCCL 在通信出错（如 broadcast 卡死）时主动报错而非无限挂起，避免作业假死。其余几行被注释掉（调试时可用）。

**拼出训练命令 `CMD`：**

[slurm/train.slurm:L146-L148](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/slurm/train.slurm#L146-L148) —— 入口脚本由 `--task` 决定（`sft`→`sft.py`、`grpo`→`grpo.py`），`--config` 指向 YAML，`$OPTIONAL_ARGS` 透传额外覆盖（含 4.2 注入的 `--vllm_server_host`）。

**拼出启动器 `LAUNCHER`：**

[slurm/train.slurm:L150-L161](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/slurm/train.slurm#L150-L161) —— 这里把上面流程里的 accelerate 参数逐项落地。注意 `--main_process_ip $MASTER_ADDR`（第一个训练节点）+ `--main_process_port 6000`（[L120](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/slurm/train.slurm#L120)）共同构成 rendezvous 地址；`--machine_rank $SLURM_PROCID` 让每台节点知道自己 rank；`--rdzv_backend=c10d` 用 PyTorch 原生会合后端；`--max_restarts 1` 允许失败重启一次。

**把训练节点数组转成逗号串，组装 `srun` 参数并启动：**

[slurm/train.slurm:L165-L174](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/slurm/train.slurm#L165-L174) ——
- L165 `NODELIST=$(IFS=,; echo "${TRAIN_NODES[*]}")`：用 `IFS=,` 把训练节点数组拼成逗号分隔字符串（注意这里**复用**了变量名 `NODELIST`，覆盖了之前的数组，换成字符串），供 `--nodelist` 使用。
- L167-L173 `SRUN_ARGS`：`--wait=60`（首个任务退出后再等 60 秒才杀其余，给收尾时间）、`--kill-on-bad-exit=1`（任一非零退出即终止整个 step）、`--nodes/--ntasks` 都设为训练节点数、`--nodelist` 限定只在训练节点跑。
- L174 `srun $SRUN_ARGS bash -c "$LAUNCHER $CMD"`：**最终启动行**。srun 在每台训练节点上启动一个 `bash -c`，执行 accelerate launch → 训练脚本。

> **收尾计时**：[slurm/train.slurm:L176-L182](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/slurm/train.slurm#L176-L182) 用 `date +%s` 差值算出作业总耗时并打印，方便核算成本。

#### 4.3.4 代码实践

**实践目标**：理解 `srun` 与 `accelerate launch` 的嵌套，能手动拼出最终命令。

**操作步骤**（本地 bash 模拟，无需集群）：

```bash
TASK=grpo
ACCELERATOR=zero2
CONFIG_FILE=recipes/Qwen2.5-1.5B-Instruct/grpo/config_demo.yaml
NUM_NODES=1            # 进入 vLLM 分支后的训练节点数
WORLD_SIZE=8
MASTER_ADDR=n0
MASTER_PORT=6000
GRAD_ACC_STEPS=4
OPTIONAL_ARGS="--vllm_server_host=n1"

CMD="src/open_r1/$TASK.py --config $CONFIG_FILE $OPTIONAL_ARGS"
LAUNCHER="ACCELERATE_LOG_LEVEL=info TRANSFORMERS_VERBOSITY=info accelerate launch \
    --config_file recipes/accelerate_configs/$ACCELERATOR.yaml \
    --gradient_accumulation_steps $GRAD_ACC_STEPS \
    --num_machines $NUM_NODES \
    --num_processes $WORLD_SIZE \
    --main_process_ip $MASTER_ADDR \
    --main_process_port $MASTER_PORT \
    --machine_rank \$SLURM_PROCID \
    --rdzv_backend=c10d --max_restarts 1 --tee 3"

echo "最终命令："
echo "srun --nodes=$NUM_NODES --ntasks=$NUM_NODES --nodelist=n0 --wait=60 --kill-on-bad-exit=1 bash -c \"$LAUNCHER $CMD\""
```

**需要观察的现象**：打印出的最终命令中，外层 `srun` 限定 `--nodelist=n0`（训练节点），内层 `accelerate launch` 用 `--num_processes 8 --num_machines 1`，并把 `--machine_rank` 交给 `$SLURM_PROCID` 在运行时填充。

**预期结果**：你能清楚看到「srun 决定在哪台机器起进程 / accelerate 决定进程怎么组网」的分层。

> **待本地验证**：真正执行需要 Slurm 集群与多 GPU；本步骤只验证命令拼装逻辑。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `MASTER_ADDR` 取 `NODELIST[0]`（第一个节点），而不是 vLLM 节点？

> **答案**：`MASTER_ADDR` 是**训练进程组**的 rendezvous 主地址，必须是一个**训练节点**。vLLM 节点不参与训练进程组（它只是个 HTTP 服务），而 `NODELIST[0]` 恰好是第一个训练节点（vLLM 取的是最后一个 `NODELIST[-1]`），所以选它作会合点是对的。

**练习 2**：`--kill-on-bad-exit=1` 和 `--wait=60` 各解决什么问题？

> **答案**：`--kill-on-bad-exit=1` 让任一节点非零退出时立即终止整个 step，避免一台挂了、其余空转烧钱；`--wait=60` 则在首个任务退出后再等 60 秒，给其他节点正常收尾（写日志、保存 checkpoint）的时间，再统一清理。两者配合实现「快速失败 + 优雅收尾」。

**练习 3**：脚本顶部有一段 `find -L /fsx/h4/ ... | xargs ... weka fs tier fetch`（[L36-L37](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/slurm/train.slurm#L36-L37)），它是干什么的？删掉会影响训练正确性吗？

> **答案**：这是 **Hugging Face 计算集群专有**的 Weka 分层存储预热——把冷数据从底层存储「拉」到本机缓存，加速后续模型/数据读取。它属于性能优化，**不影响训练正确性**；换到别的集群时这段应删除或替换为该集群的存储预热方式（README 也提示脚本是为 HF 集群优化的，需自行调整）。

---

## 5. 综合实践

把三个模块串起来，完成一次「纸上排程」演练。

**任务**：假设你要在 **3 台 8 卡节点**上跑一个 Codeforces GRPO 任务，配置文件是 `recipes/Qwen2.5-Coder-7B-Instruct/grpo/config_codeforces.yaml`（含 `use_vllm: true`），accelerate 配置用 `zero3`，vLLM 用 `--dp 8 --tp 1`。

请完成：

1. **写出完整的 `sbatch` 命令**（参考 README 里 Codeforces 的示例 [README.md:L388-L392](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/README.md#L388-L392)，但把节点数改成 3）。
2. **手算拓扑**：哪台节点跑 vLLM？哪几台跑训练？训练 `WORLD_SIZE` 是多少？vLLM 占几张 GPU？
3. **画出请求流向**：训练进程采样时，数据从哪个进程发往哪个节点？`--vllm_server_host` 指向谁？
4. **写出最终 `srun ... bash -c "$LAUNCHER $CMD"`** 中 `LAUNCHER` 的 `--num_machines` 和 `--num_processes` 取值。

**参考答案**：

1. 命令：
   ```bash
   sbatch --job-name=cf-grpo --nodes=3 slurm/train.slurm \
     --model Qwen2.5-Coder-7B-Instruct --task grpo --config codeforces \
     --accelerator zero3 --dp 8 --tp 1
   ```
2. 拓扑：`NUM_NODES=3`，总 `WORLD_SIZE=24`；进 vLLM 分支后训练 `NUM_NODES=2`、`WORLD_SIZE=16`（2 台 × 8 卡）；`VLLM_NODE` = 第 3 台（最后一台）；vLLM 占 `TP×DP=1×8=8` 张 GPU（正好用满该节点）。即 **2 训练 + 1 vLLM**。
3. 请求流向：3 台训练节点（更确切地说是 rank 0 / GRPOTrainer 主进程）通过 HTTP 把采样 prompt 发往 `VLLM_NODE`（第 3 台），`--vllm_server_host` 指向第 3 台主机名；vLLM 用 8 张卡连续批处理后把生成的 completion 回传训练进程。
4. `LAUNCHER` 中 `--num_machines 2`、`--num_processes 16`。

> **待本地验证**：以上为基于脚本逻辑的推演；真实主机名与端口占用需在实际集群作业日志中核对。

---

## 6. 本讲小结

- `train.slurm` 是**编排器**而非训练实现：它把 `--model/--task/--config/--accelerator` 翻译成 YAML 路径，再从 YAML 抠出 `gradient_accumulation_steps`、`model_name_or_path`、`model_revision` 喂给后续启动。
- **`MODEL` 变量会被覆盖**：`--model` 仅用于定位配方目录，真正加载的模型 id 取自 YAML 的 `model_name_or_path`。
- **N+1 拓扑**是 open-r1 GRPO 集群训练的标志：当 YAML 含 `use_vllm: true`（纯文本 `grep` 检测），最后一台节点拨给 `trl vllm-serve`，前 N-1 台跑训练；训练 `WORLD_SIZE` 减去 `GPUS_PER_NODE`。
- `--dp`/`--tp` 是 **vLLM 服务**的 data/tensor 并行度，应满足 `TP×DP = 8`（填满一台 8 卡节点）。
- 训练启动是 **`srun`（外层派发进程到物理节点）+ `accelerate launch`（内层组分布式进程组）** 的嵌套；`--machine_rank $SLURM_PROCID` 让每台节点知道自己 rank。
- 脚本包含若干 **HF 集群专有**细节（Weka 预热、`module load cuda/12.4`、`partition=hopper-prod`），迁移到自有集群时需按 README 提示调整。

---

## 7. 下一步学习建议

- **紧接着读 u7-l2（Accelerate 与 DeepSpeed 配置）**：本讲里 `--config_file recipes/accelerate_configs/$ACCELERATOR.yaml` 选了哪份配置，将决定显存与吞吐。建议对照 `recipes/accelerate_configs/zero3.yaml` 与 `zero2.yaml` 理解 zero stage 2/3 的差异，以及为什么大模型训练常选 zero3。
- **回看 u3-l1（GRPO 主流程）**：现在你已知道 `use_vllm`、`num_generations` 在集群上如何落地；可结合 `make_conversation` 理解「训练节点发 prompt → vLLM 节点采样 → 回传打分」的完整闭环。
- **延伸阅读**：
  - [slurm/generate.slurm](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/slurm/generate.slurm)——对比「用 Ray 拉起 vLLM 做数据生成」与「用 `trl vllm-serve` 做训练采样」两种部署方式的差异。
  - [slurm/README.md](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/slurm/README.md) 与 `slurm/serve_r1.slurm`——了解推理服务（SGLang）侧的部署。
- **如果想动手**：在自有小集群上，把 `--gres=gpu:8`、`partition`、Weka 预热等 HF 专有项改成你的环境参数，用 `--nodes=2 --task sft`（不含 `use_vllm`）先跑通**纯训练** N 节点拓扑，再尝试 GRPO 的 N+1。
