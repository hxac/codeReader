# 运行第一个训练：脚本与参数

## 1. 本讲目标

本讲的目标是让你**第一次把 slime 跑起来**，并看懂它是怎么被启动的。

读完本讲你应该能够：

1. 拿着 `scripts/run-qwen3-4B.sh`，逐段说清它的两段式结构（清理环境 → `ray job submit`）。
2. 读懂 `MODEL_ARGS / CKPT_ARGS / ROLLOUT_ARGS / PERF_ARGS / SGLANG_ARGS` 等参数组的职责，知道为什么 `MODEL_ARGS` 要单独写在一个 `models/*.sh` 文件里。
3. 牢记 rollout（采样产出）与 train（训练消费）之间的数据守恒公式，并能解释 slime 在哪一行代码里校验或自动推导它。
4. 认识 `--colocate`（训练与推理共用同一组 GPU）以及为什么共卡时必须调小 `--sglang-mem-fraction-static`。

本讲是动手实践型讲义，**不要求你真的有 8 张 GPU**：大部分实践是「读脚本 + 算公式 + 读校验代码」，能在任何机器上完成。

## 2. 前置知识

在开始前，请确认你已经了解（这些是前几讲的内容）：

- **slime 的定位**：它把 Megatron 训练与 SGLang 推理缝合成「采样 → 训练 → 权重同步」闭环（见 u1-l1）。
- **slime 是纯 Python 包**，但运行时必须与一组精确锁版本的 CUDA 库（torch、SGLang、Megatron-LM、flash-attn）共存，因此官方推荐用 Docker 镜像 `slimerl/slime:latest` 运行（见 u1-l3）。
- **入口很薄**：根目录的 `train.py` 只做「解析参数 → 装配 Ray 工人 → 跑训练循环」，真正的参数中枢是 `slime/utils/arguments.py` 的 `parse_args`（见 u1-l2）。

本讲会反复用到三个术语，先约定好：

| 术语 | 含义 |
| :--- | :--- |
| rollout（采样/推理） | 用 SGLang 引擎对一批 prompt 生成回答、计算奖励，产出训练样本。 |
| train（训练） | 用 Megatron 读训练样本、算梯度、更新参数。 |
| 权重同步 | 训练后把新参数从 Megatron 单向推回 SGLang 引擎（training → rollout，不可逆）。 |

此外，slime 通过 **Ray** 来调度 GPU、分配工人。Ray 有两个关键概念：

- **Ray cluster**：一组带 GPU 的机器组成集群，由一个 head 节点管理。
- **ray job submit**：把一段 Python 命令（这里是 `python3 train.py ...`）提交到集群上异步执行。slime 选择 Ray 是因为它能同时描述「训练卡」和「推理卡」两套资源的放置关系（这在 u2 会深入）。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
| :--- | :--- |
| [scripts/run-qwen3-4B.sh](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/scripts/run-qwen3-4B.sh) | Qwen3-4B 的端到端启动脚本，本讲的主角。 |
| [scripts/models/qwen3-4B.sh](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/scripts/models/qwen3-4B.sh) | Qwen3-4B 的 Megatron 结构参数，被 `source` 进主脚本。 |
| [docs/en/get_started/quick_start.md](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/docs/en/get_started/quick_start.md) | 官方快速上手文档，解释了每个参数组与供需公式。 |
| [slime/utils/arguments.py](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/utils/arguments.py) | 参数中枢。本讲重点看它如何校验供需公式、如何处理 colocate。 |
| [slime/backends/sglang_utils/arguments.py](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/sglang_utils/arguments.py) | SGLang 参数解析，重点看 `--sglang-` 前缀透传机制。 |

> 提示：前三个文件是「外围」（脚本与文档），后两个是「框架本体」。本讲会让你先看脚本，再去框架里找它背后对应的逻辑——这正是「从入口往里读源码」的练习。

## 4. 核心概念与源码讲解

本讲拆成 4 个最小模块：

- **4.1 启动脚本的两段式结构**：从清理残留进程到 `ray job submit`。
- **4.2 参数分组地图**：理解为什么脚本要把参数切成十几个数组。
- **4.3 供需平衡公式**：rollout 产出与 train 消费必须守恒，slime 在哪一行校验。
- **4.4 colocate 共卡与显存配比**：训练和推理共享 GPU 时如何不爆显存。

### 4.1 启动脚本的两段式结构：从清理到 ray job submit

#### 4.1.1 概念说明

slime 的启动脚本不是「一条命令跑起来」，而是**两个阶段**：

1. **宿主阶段（host phase）**：在跑 Python 之前，先在 shell 里做两件事——把上一次残留的 sglang / ray / python 进程杀干净，再用 `ray start --head` 起一个 Ray 集群。
2. **作业阶段（job phase）**：用 `ray job submit` 把 `python3 train.py ...` 连同一大堆参数提交到刚起的集群上。

为什么要先杀进程？因为 RL 训练动辄跑几天，中途重跑脚本时，上一轮的 SGLang 服务、Ray daemon 很可能还占着 GPU 显存和端口。脚本开头一顿 `pkill -9` 就是为了保证「干净的 GPU、干净的 Ray」，避免「明明 8 卡却只看到 4 卡可用」这类诡异问题。

为什么要用 `ray job submit` 而不是直接 `python3 train.py`？因为 slime 需要 Ray 来描述「哪几张卡给训练、哪几张卡给推理」。直接跑 Python 就失去了 Ray 的资源编排能力。

#### 4.1.2 核心流程

`run-qwen3-4B.sh` 的执行流程可以画成：

```
[1] pkill -9 sglang / ray / python   # 清理残留进程（多次，确保杀净）
        ↓
[2] set -ex; export PYTHONUNBUFFERED=1  # 打开错误即退出 + 关闭输出缓冲
        ↓
[3] 检测 NVLink / GPU 数量，确定 NUM_GPUS（默认 8）
        ↓
[4] source models/qwen3-4B.sh         # 加载 MODEL_ARGS 模型结构参数
        ↓
[5] 声明 CKPT_ARGS / ROLLOUT_ARGS / PERF_ARGS ... 等参数数组
        ↓
[6] ray start --head ...              # 起一个 Ray 集群（head 节点）
        ↓
[7] ray job submit ... -- python3 train.py ${各参数数组[@]}  # 提交作业
```

注意第 7 步：所有的 `${MODEL_ARGS[@]}`、`${ROLLOUT_ARGS[@]}` 都是 bash 数组展开，最终拼成一条超长的 `python3 train.py ...` 命令。也就是说，**这个脚本本质上只是一个「拼命令」的模板**，真正的逻辑全在 `train.py` 和它 import 的模块里。

#### 4.1.3 源码精读

先看清理段，脚本开头连续杀进程并 sleep：

[scripts/run-qwen3-4B.sh:1-12](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/scripts/run-qwen3-4B.sh#L1-L12) —— 杀掉残留的 sglang/ray/python 进程，这是「重跑前先打扫干净」的惯例。

接着是 GPU 与 NVLink 检测，决定 `NUM_GPUS`：

[scripts/run-qwen3-4B.sh:18-35](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/scripts/run-qwen3-4B.sh#L18-L35) —— 探测 NVLink 与 GPU 数量；`NUM_GPUS` 优先用环境变量，其次自动探测，最后兜底 8。注意 `HAS_NVLINK` 后面会写进 Ray 的运行时环境，用来决定是否开 NCCL 的 NVLS。

然后是起 Ray 集群（host 阶段的高潮）：

[scripts/run-qwen3-4B.sh:134-135](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/scripts/run-qwen3-4B.sh#L134-L135) —— `ray start --head` 起一个单节点 Ray 集群，`--num-gpus ${NUM_GPUS}` 告诉 Ray 这台机器有多少卡可用，`--dashboard-port=8265` 是 Ray dashboard 端口（也是下一步 `ray job submit` 要连的地址）。

最后是作业提交，也是整个脚本真正的「启动」：

[scripts/run-qwen3-4B.sh:146-161](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/scripts/run-qwen3-4B.sh#L146-L161) —— `ray job submit --address=...` 把 `python3 train.py` 提交到集群。注意三件事：① `--runtime-env-json` 里设了 `PYTHONPATH=/root/Megatron-LM/`（u1-l3 讲过 Megatron-LM 靠 PYTHONPATH 定位）；② `--colocate` 表示训练推理共卡（4.4 详讲）；③ 最后把十几个参数数组依次展开拼到命令尾部。

运行时环境那段也值得一看，它把环境变量注入到 Ray worker 进程里：

[scripts/run-qwen3-4B.sh:137-144](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/scripts/run-qwen3-4B.sh#L137-L144) —— 用 JSON 描述运行时环境，其中 `CUDA_DEVICE_MAX_CONNECTIONS=1` 是 Megatron 流水线并行下的常见调优项，`NCCL_NVLS_ENABLE` 用前面探测到的 `HAS_NVLINK` 控制是否启用 NVLink 的 NCCL 优化。

#### 4.1.4 代码实践

**实践目标**：在不运行的前提下，把 `run-qwen3-4B.sh` 划分成「host 阶段」与「job 阶段」。

**操作步骤**：

1. 打开 [scripts/run-qwen3-4B.sh](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/scripts/run-qwen3-4B.sh)。
2. 找到 `ray start --head` 这一行（约 L135），在它之前画一条分隔线。
3. 把分隔线之前的行号区间归为「host 阶段」，之后的归为「job 阶段」。
4. 在笔记里记下：job 阶段最核心的一行命令是什么？（提示：是 `python3 train.py`）

**需要观察的现象**：你会发现 host 阶段全是 shell 命令（`pkill` / `ray start` / 探测），没有任何 Python；job 阶段才出现 `train.py`。

**预期结果**：host 阶段 ≈ L1–L144（含起 Ray 集群），job 阶段 ≈ L146–L161（`ray job submit ... python3 train.py`）。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `ray start --head` 那一行删掉，直接跑 `ray job submit`，会发生什么？

**参考答案**：`ray job submit --address="http://127.0.0.1:8265"` 找不到 Ray 集群（8265 端口没有 dashboard 在监听），会连接失败报错。`ray start --head` 是把集群起起来的前提。

**练习 2**：脚本开头为什么要 sleep + 重复 `pkill`？

**参考答案**：`pkill -9` 发出 SIGKILL 后，进程不会立即释放 GPU 显存和端口，需要一点时间回收。重复 pkill 是为了抓住那些依赖被杀进程、稍后才启动的子进程；sleep 是给系统留出回收窗口，确保下一轮跑起来时 GPU 是干净的。

### 4.2 参数分组地图：从 MODEL_ARGS 到 MISC_ARGS

#### 4.2.1 概念说明

`ray job submit` 后面那一长串参数，并不是随便堆的——脚本用 bash 数组把它们**按职责分成了十几个组**，每组管一类事情。这样做的直接好处是：换模型时只改 `MODEL_ARGS`，换数据集时只改 `ROLLOUT_ARGS`，调并行度时只改 `PERF_ARGS`，互不干扰。

其中 `MODEL_ARGS` 比较特殊：它**不在主脚本里**，而是单独放在 `scripts/models/qwen3-4B.sh`，通过 `source` 命令加载。原因是：Megatron 无法像 HuggingFace 那样从检查点的 `config.json` 自动读取模型结构（层数、隐层维度、注意力头数等），必须**在命令行显式指定**。slime 把这些「描述模型长什么样」的参数固化成一份 per-model 的小脚本，复用时 `source` 一下即可（权重转换见 u1-l5）。

#### 4.2.2 核心流程

参数组的对应关系一览（以 run-qwen3-4B.sh 为准）：

| 参数组 | 来源 | 管什么 | 代表参数 |
| :--- | :--- | :--- | :--- |
| `MODEL_ARGS` | `models/qwen3-4B.sh` | 模型结构（给 Megatron） | `--num-layers 36 --hidden-size 2560` |
| `CKPT_ARGS` | 主脚本 | 检查点路径（加载/保存） | `--hf-checkpoint --ref-load --save` |
| `ROLLOUT_ARGS` | 主脚本 | 数据采样与 RL 循环 | `--rollout-batch-size --num-rollout` |
| `EVAL_ARGS` | 主脚本 | 评估 | `--eval-interval --eval-prompt-data` |
| `PERF_ARGS` | 主脚本 | Megatron 并行与显存 | `--tensor-model-parallel-size --recompute-*` |
| `GRPO_ARGS` | 主脚本 | RL 算法与损失 | `--advantage-estimator grpo --eps-clip` |
| `OPTIMIZER_ARGS` | 主脚本 | 优化器 | `--lr 1e-6 --optimizer adam` |
| `SGLANG_ARGS` | 主脚本 | SGLang 推理服务 | `--rollout-num-gpus-per-engine --sglang-mem-fraction-static` |
| `MISC_ARGS` | 主脚本 | Megatron 细节调优 | `--attention-backend flash` |

分组思路是「**框架三族参数**」：Megatron 参数（模型结构 + 并行 + 优化器，原样传给 Megatron-LM）、SGLang 参数（`--sglang-` 前缀透传，4.4 详讲）、slime 专属参数（`--rollout-*`、`--colocate` 等闭环控制）。

#### 4.2.3 源码精读

先看 `MODEL_ARGS` 是怎么来的——主脚本第 37-38 行 source 了模型配置：

[scripts/run-qwen3-4B.sh:37-38](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/scripts/run-qwen3-4B.sh#L37-L38) —— 通过 `source` 把 `models/qwen3-4B.sh` 里定义的 `MODEL_ARGS` 数组引入当前 shell。注意它用 `BASH_SOURCE[0]` 定位脚本自身目录，保证从任意 `cwd` 调用都能找到同目录的模型文件。

再看模型文件本身，它就是一组 Megatron 结构参数：

[scripts/models/qwen3-4B.sh:1-16](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/scripts/models/qwen3-4B.sh#L1-L16) —— Qwen3-4B 的结构：36 层、隐层 2560、FFN 9728、32 个注意力头里 8 个 KV 组（GQA）、RMSNorm、rotary base 默认 1000000。这些必须和真实模型权重严格匹配，否则 Megatron 会形状不匹配报错。注意 `--rotary-base "${MODEL_ARGS_ROTARY_BASE:-1000000}"` 允许用环境变量覆盖——quick_start 文档专门提醒过同结构不同版本可能用不同的 rotary base。

接着看 `ROLLOUT_ARGS`，它里面藏着本讲最关键的几个量：

[scripts/run-qwen3-4B.sh:49-64](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/scripts/run-qwen3-4B.sh#L49-L64) —— `--rollout-batch-size 32`（每轮采 32 个 prompt）、`--n-samples-per-prompt 8`（每个 prompt 生成 8 条回答）、`--global-batch-size 256`、`--num-rollout 3000`。注意这里**没有** `--num-steps-per-rollout`，这点在 4.3 会反复用到。

quick_start 文档对这些分组有官方解释，值得对照阅读：

[docs/en/get_started/quick_start.md:119-207](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/docs/en/get_started/quick_start.md#L119-L207) —— 逐组讲解 MODEL_ARGS / CKPT_ARGS / ROLLOUT_ARGS / EVAL_ARGS / PERF_ARGS / GRPO_ARGS / OPTIMIZER_ARGS / SGLANG_ARGS 的含义，是本模块的权威参考。

#### 4.2.4 代码实践

**实践目标**：建立一个「参数→所属框架」的速查表。

**操作步骤**：

1. 打开 `run-qwen3-4B.sh`，把每个参数组里的参数逐条抄下来。
2. 给每个参数标注它属于「Megatron / SGLang / slime 专属」中的哪一族。
3. 提示：带 `--sglang-` 前缀的归 SGLang；`--rollout-*`、`--colocate`、`--num-rollout`、`--balance-data`、`--use-dynamic-batch-size`、`--max-tokens-per-gpu` 归 slime 专属；其余大多是 Megatron。

**需要观察的现象**：你会发现 slime 专属参数其实不多，大部分参数都是「原样转手」给 Megatron 或 SGLang。这正是 slime「拒绝重复造轮子」的设计哲学（见 u1-l1）。

**预期结果**：得到一张三列表格，能一眼看出「这个参数最终被谁消费」。

#### 4.2.5 小练习与答案

**练习 1**：`--rollout-batch-size` 和 `--global-batch-size` 分别属于哪一族、由谁消费？

**参考答案**：都属于 slime 专属参数，由 `slime/utils/arguments.py` 解析并用于控制闭环的数据流（rollout 阶段产出多少、train 阶段消费多少），不直接交给 Megatron 或 SGLang。

**练习 2**：为什么 `MODEL_ARGS` 要写成独立文件，而不是直接写进 `run-qwen3-4B.sh`？

**参考答案**：因为同一套模型结构参数既要在「训练启动」里用，也要在「HF→torch_dist 权重转换」（u1-l5）里用。写成独立的 `models/qwen3-4B.sh` 后，两个脚本都可以 `source` 它，避免重复维护两份结构参数导致不一致。

### 4.3 供需平衡公式：rollout 产出与 train 消费的守恒

#### 4.3.1 概念说明

这是本讲**最重要的一节**。slime 把训练看成「采样 → 训练」的闭环，每一轮（rollout）必须满足：

- **产出端（rollout）**：一轮采样会生成 `rollout_batch_size × n_samples_per_prompt` 条样本。
- **消费端（train）**：一轮训练会用这些样本做 `num_steps_per_rollout` 次参数更新，每次更新吃 `global_batch_size` 条样本。

闭环要求「产出 = 消费」，于是有守恒公式：

\[
\text{rollout\_batch\_size} \times \text{n\_samples\_per\_prompt} \;=\; \text{global\_batch\_size} \times \text{num\_steps\_per\_rollout}
\]

用一句话记：**左半边是 rollout 一轮「产出」多少条样本，右半边是 train 一轮「消费」多少条样本，二者必须相等。**

为什么必须相等？因为样本是「一次性消费品」——在 on-policy RL 里，每条样本只在当前这轮被训练用一次，用完就丢（下一轮要重新采样）。如果产出 > 消费，多出来的样本浪费了；如果产出 < 消费，train 这一轮根本凑不够数据。所以 slime 强制二者相等。

#### 4.3.2 核心流程

slime 对这个公式的处理分两种情况（见 4.3.3 的源码）：

```
若用户显式设置了 --num-steps-per-rollout：
    计算 gbs = rollout_batch_size * n_samples_per_prompt // num_steps_per_rollout
    若用户也设置了 --global-batch-size：
        断言 二者相等，否则报错
    否则：
        自动把 global_batch_size 设为上面算出的 gbs

若用户没设置 --num-steps-per-rollout（默认，on-policy）：
    跳过自动校验，直接用用户给的 --global-batch-size
    （此时公式靠用户自觉保持 1:1，即 gbs = rollout_batch_size * n_samples_per_prompt）
```

关键认知：**只有当你显式给出 `--num-steps-per-rollout` 时，slime 才会帮你算或帮你校验 `global_batch_size`；不给的话，slime 信任你手动设的 `global_batch_size`，不做校验。** 这是「自动推导」与「手动维护」的切换开关。

用 run-qwen3-4B.sh 的数字验证（它没设 `--num-steps-per-rollout`）：

\[
32 \times 8 = 256, \quad \text{而 } \text{global\_batch\_size} = 256 \;\checkmark
\]

正好相等，对应「一轮采样 256 条、训练 1 步吃 256 条」的 on-policy 模式。

#### 4.3.3 源码精读

先看这四个参数在参数中枢里是怎么定义的：

[slime/utils/arguments.py:680-704](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/utils/arguments.py#L680-L704) —— `--rollout-batch-size`（required）、`--n-samples-per-prompt`（default 1）、`--global-batch-size`（default None）、`--num-steps-per-rollout`（default None）。注意第 692-694 行的注释明确点出：「希望每轮 rollout 训练 1 步时，global_batch_size 应设为 `rollout_batch_size * n_samples_per_prompt`」。

然后是真正的校验/自动推导逻辑，这是本模块的核心：

[slime/utils/arguments.py:1907-1915](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/utils/arguments.py#L1907-L1915) —— 只有当 `num_steps_per_rollout is not None` 时才进入：先用整数除算出 `global_batch_size = rollout_batch_size * n_samples_per_prompt // num_steps_per_rollout`；如果用户也给定了 `global_batch_size`，就用 `assert` 校验相等（不等就抛 AssertionError，把三个值都打印出来）；否则直接采纳算出来的值。**这正是「自动设置 / 报错」两种行为的同一处代码。**

注意：当 `num_steps_per_rollout` 是 None 时（run-qwen3-4B.sh 的情况），这段 `if` 整体跳过，`global_batch_size` 完全用用户给的值，不做公式校验。

quick_start 文档对这条公式有最权威的表述：

[docs/en/get_started/quick_start.md:156-174](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/docs/en/get_started/quick_start.md#L156-L174) —— 官方把流程拆成「Phase One 数据采样」与「Phase Two 模型训练」，明确写出 `(rollout-batch-size × n-samples-per-prompt) = (global-batch-size × num-steps-per-rollout)`，并指出：设了 `--num-steps-per-rollout` 但没设 `--global-batch-size` 时会自动推导，设了则校验。

#### 4.3.4 代码实践

**实践目标**：用 run-qwen3-4B.sh 的数字手算公式，并预测「改一个数」的后果。

**操作步骤**：

1. 从 [run-qwen3-4B.sh:49-64](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/scripts/run-qwen3-4B.sh#L49-L64) 读出：`rollout_batch_size=32`、`n_samples_per_prompt=8`、`global_batch_size=256`、`num_steps_per_rollout` 未设。
2. 计算左半边：32 × 8 = 256。
3. 因为 `num_steps_per_rollout` 未设，slime 不校验，但用户手动让 `global_batch_size=256`，二者相等 ✓。
4. 假设你把 `--rollout-batch-size` 改成 40，保持其他不变：左半边 = 40 × 8 = 320，但 `global_batch_size` 仍是 256。问：slime 会报错吗？

**需要观察的现象 / 预期结果**：

- 因为 `num_steps_per_rollout` 没设，[arguments.py:1907](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/utils/arguments.py#L1907) 的 `if` 不进入，**slime 不会报错**——但闭环会失衡（产出 320 条、消费 256 条），多出的 64 条被丢弃。这提示我们：on-policy 模式下，改 `rollout_batch_size` 必须**同步手动改 `global_batch_size`**。
- 如果你显式加了 `--num-steps-per-rollout 1`，此时 slime 会算出 gbs = 40×8//1 = 320，再用 assert 校验你给的 256 ≠ 320，**会抛 AssertionError**。

**待本地验证**：上述「多出 64 条被丢弃」是按 on-policy 语义推断的行为；若你想确认，可在本地用 `--num-rollout 1` 跑一个极小实验观察日志（需要 GPU 环境）。

#### 4.3.5 小练习与答案

**练习 1**：某脚本设 `--rollout-batch-size 16 --n-samples-per-prompt 8 --num-steps-per-rollout 2`，没设 `--global-batch-size`。问 slime 最终用的 `global_batch_size` 是多少？

**参考答案**：进入 [arguments.py:1908](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/utils/arguments.py#L1908) 自动推导：`global_batch_size = 16 × 8 // 2 = 64`。

**练习 2**：如果 `--rollout-batch-size 16 --n-samples-per-prompt 8 --num-steps-per-rollout 2 --global-batch-size 100`，会发生什么？

**参考答案**：slime 算出 gbs=64，但用户给的 100，触发 [arguments.py:1910-1914](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/utils/arguments.py#L1910-L1914) 的 assert，抛出 `AssertionError` 并打印三个值，提示 `global_batch_size 100 is not equal to rollout_batch_size 16 * n_samples_per_prompt 8 // num_steps_per_rollout 2`。

### 4.4 colocate 共卡部署与显存配比

#### 4.4.1 概念说明

slime 有两种 GPU 放置方式：

- **分离（disaggregated）**：训练卡和推理卡是两组不同的 GPU，各跑各的，互不抢显存。
- **共卡（colocate）**：训练和推理**共用同一组 GPU**。一轮里「先推理采样、把 Megatron 模型 offload 到 CPU → 再切给 SGLang 用 GPU → 训练时再 onload 回来」，通过时分复用让一组卡同时干两件事。

为什么要 colocate？因为小集群（比如单机 8 卡）下，如果训练推理各占一半卡，两边都不够用；共卡能让全部 8 张卡既参与训练又参与推理，提高利用率。代价是「在训练和推理之间来回搬运模型权重（offload/onload）」需要时间，所以适合中小规模。

共卡带来一个**显存争夺**问题：Megatron 在初始化后会先占走一部分显存（之后才能 offload），而 SGLang 也要预分配 KV cache。如果两者都按默认值抢显存，就会 OOM。解决办法是调小 SGLang 的显存占比参数 `--sglang-mem-fraction-static`——它表示 SGLang 静态预分配的显存占总显存的比例。run-qwen3-4B.sh 里设成 0.7（70%），quick_start 建议共卡时用 0.8 左右，给 Megatron 留出空间。

#### 4.4.2 核心流程

colocate 模式下，slime 参数校验阶段会发生这些事（见 4.4.3）：

```
用户传 --colocate（不设 --rollout-num-gpus）：
    rollout_num_gpus 自动 = actor_num_gpus_per_node * actor_num_nodes
    → 训练和推理共用同一组卡
    offload_train / offload_rollout 默认置 True
    → 在训练/推理切换时把对应模块搬到 CPU

由 rollout_num_gpus 和 rollout-num-gpus-per-engine 推 SGLang 拓扑：
    tp_size = rollout_num_gpus_per_engine （当 pp_size=1）
    dp_size = rollout_num_gpus / rollout_num_gpus_per_engine
```

用 run-qwen3-4B.sh 的数字算一遍（单机 8 卡、colocate）：

- `actor-num-nodes 1`，`actor-num-gpus-per-node 8`，`--colocate` → `rollout_num_gpus = 1 × 8 = 8`。
- `--rollout-num-gpus-per-engine 2` → 每个 SGLang 引擎占 2 张卡（tp_size=2）。
- `dp_size = 8 / 2 = 4` → 起了 4 个 SGLang 引擎并行服务。
- `--sglang-mem-fraction-static 0.7` → 每张卡上 SGLang 静态占 70% 显存，留 30% 给 Megatron。

#### 4.4.3 源码精读

先看 `--colocate` 等放置参数的定义：

[slime/utils/arguments.py:44-100](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/utils/arguments.py#L44-L100) —— `--rollout-num-gpus`（推理卡数）、`--rollout-num-gpus-per-engine`（每引擎卡数≈tp_size）、`--colocate`、`--offload-train`、`--offload-rollout`。注意 help 文字明确说：开 `--colocate` 会同时把 offload 置为 true。

再看 colocate 的实际处理逻辑（参数校验阶段）：

[slime/utils/arguments.py:1875-1890](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/utils/arguments.py#L1875-L1890) —— 当 `args.colocate` 为真时：① 默认把 `offload_train/offload_rollout` 设为 True（除非 release-train 模式）；② 若 `rollout_num_gpus` 没设，就令它等于 `actor_num_gpus_per_node * actor_num_nodes`（这正是「共卡」的落点）；③ 若显式设为 0，则只起 router 不起本地 SGLang 引擎。

然后是 `--sglang-mem-fraction-static` 是怎么被 slime 接住的。它不是 slime 自己定义的参数，而是 SGLang 的参数，slime 通过 `--sglang-` 前缀透传机制自动接住：

[slime/backends/sglang_utils/arguments.py:38-117](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/sglang_utils/arguments.py#L38-L117) —— `add_sglang_arguments` 用一个 `new_add_argument_wrapper` 包装了 `parser.add_argument`：每当 SGLang 的 `ServerArgs.add_cli_args` 注册一个参数（如 `--mem-fraction-static`），包装器就把它改写成 `--sglang-mem-fraction-static`（[L92-94](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/sglang_utils/arguments.py#L92-L94) 给每个 flag 加 `--sglang-` 前缀）。这样你写 `--sglang-mem-fraction-static 0.7`，slime 就知道这是要透传给 SGLang 的 `mem_fraction_static=0.7`。少数参数（如 `model_path`、`tp_size`）被列入 `skipped_args`（[L48-66](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/sglang_utils/arguments.py#L48-L66)）由 slime 自己管理、不透传。

再看 tp_size 是怎么从 `rollout-num-gpus-per-engine` 推出来的：

[slime/backends/sglang_utils/arguments.py:158-166](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/sglang_utils/arguments.py#L158-L166) —— 当 `sglang_pp_size` 为 1 时，`sglang_tp_size = rollout_num_gpus_per_engine`（即每引擎卡数就是 SGLang 的张量并行度）。

最后看 quick_start 对 colocate 与显存配比的官方说明：

[docs/en/get_started/quick_start.md:305-336](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/docs/en/get_started/quick_start.md#L305-L336) —— 对比「分离配置」（`--actor-num-gpus-per-node 4 --rollout-num-gpus 4`）与「共卡配置」（`--colocate`），并明确警告：共卡时 Megatron 会先占一部分显存，必须调小 `--sglang-mem-fraction-static`（推荐约 0.8）防 OOM。

#### 4.4.4 代码实践

**实践目标**：根据脚本参数推算 SGLang 的拓扑（引擎数 / tp_size / dp_size）。

**操作步骤**：

1. 从 [run-qwen3-4B.sh:117-120](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/scripts/run-qwen3-4B.sh#L117-L120) 读出 `--rollout-num-gpus-per-engine 2`。
2. 从 [run-qwen3-4B.sh:146-152](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/scripts/run-qwen3-4B.sh#L146-L152) 读出 `--actor-num-nodes 1 --actor-num-gpus-per-node ${NUM_GPUS} --colocate`，假设 `NUM_GPUS=8`。
3. 推算：`rollout_num_gpus = ?`、`sglang_tp_size = ?`、引擎数（dp_size）= ?、SGLang 显存占比 = ?

**需要观察的现象**：把这些数字填进一张表，体会「每引擎 2 卡、8 卡共起 4 引擎、每卡留 30% 给 Megatron」。

**预期结果**：`rollout_num_gpus=8`、`sglang_tp_size=2`、引擎数 `dp_size=8/2=4`、SGLang 占 70% 显存。

#### 4.4.5 小练习与答案

**练习 1**：同样 8 卡，如果改成**分离配置**（`--actor-num-gpus-per-node 4 --rollout-num-gpus 4`，去掉 `--colocate`），训练和推理分别用几张卡？`--sglang-mem-fraction-static` 还需要特意调小吗？

**参考答案**：训练用 4 卡、推理用 4 卡（互不共享）。分离时双方不抢同一张卡的显存，所以 `--sglang-mem-fraction-static` 可以用较大默认值（接近 0.9），不必特意调小。这正是 colocate 与分离的核心权衡。

**练习 2**：colocate 下 `--rollout-num-gpus 0` 会发生什么？

**参考答案**：见 [arguments.py:1889-1890](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/utils/arguments.py#L1889-L1890)：slime 只起 router（路由器），不起本地 SGLang 引擎。这用于「推理由外部集群托管」的场景（u8 会展开）。

## 5. 综合实践

现在把四个模块串起来，完成本讲的主任务：**复制并魔改 `run-qwen3-4B.sh`，验证供需公式的自动校验与报错行为**。

**实践目标**：亲手触发 slime 对供需公式的两种行为（自动推导 / AssertionError），从而真正理解 4.3 的代码。

**操作步骤**：

1. 复制脚本：在仓库根目录 `cp scripts/run-qwen3-4B.sh /tmp/my-run.sh`。
2. **场景 A（自动推导）**：编辑 `/tmp/my-run.sh` 的 `ROLLOUT_ARGS`，把 `--num-rollout 3000` 改成 `--num-rollout 1`（缩短，只为看校验），把 `--rollout-batch-size` 改成 `40`，删掉 `--global-batch-size 256` 这一行，新增一行 `--num-steps-per-rollout 2`。
   - 手算：slime 会推导 `global_batch_size = 40 × 8 // 2 = 160`。
3. **场景 B（报错）**：在场景 A 基础上，再把 `--global-batch-size 256` 加回去（与推导值 160 冲突）。
   - 预测：触发 [arguments.py:1910](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/utils/arguments.py#L1910) 的 assert，报 `global_batch_size 256 is not equal to ...`。
4. **场景 C（不校验）**：把 `--num-steps-per-rollout 2` 删掉（恢复默认 None），保留 `--rollout-batch-size 40` 和 `--global-batch-size 256`。
   - 预测：因为 `num_steps_per_rollout` 是 None，[arguments.py:1907](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/utils/arguments.py#L1907) 的 `if` 不进入，**不报错**，但 40×8=320 ≠ 256，闭环失衡。

**需要观察的现象**：三种场景下，slime 的行为分别是「自动设 gbs」「抛 AssertionError」「静默接受（靠用户自觉）」。

**预期结果**：

| 场景 | num-steps-per-rollout | global-batch-size | slime 行为 |
| :--- | :--- | :--- | :--- |
| A | 2 | 未设 | 自动推导为 160 |
| B | 2 | 256 | AssertionError（256 ≠ 160） |
| C | 未设 | 256 | 不校验，静默接受（失衡需用户自负） |

**待本地验证**：场景 B 的报错信息、场景 A 的自动设值，可以在有 GPU + Docker 的环境里用 `--num-rollout 1` 实际提交一次作业，看 Ray job 的早期日志（参数校验发生在 `train.py` 刚启动、`parse_args` 期间，所以即使没有真实数据也会立刻报）。无 GPU 时，也可只 `import` slime 后手动构造 namespace 跑校验逻辑来验证断言。

## 6. 本讲小结

- slime 启动脚本是**两段式**：host 阶段（清理进程 + `ray start --head`）和 job 阶段（`ray job submit ... python3 train.py`），本质是「拼一条超长命令」。
- 参数被分成 `MODEL_ARGS / CKPT_ARGS / ROLLOUT_ARGS / PERF_ARGS / SGLANG_ARGS` 等十几个 bash 数组，分属 Megatron / SGLang / slime 三族；`MODEL_ARGS` 因 Megatron 无法自动读模型结构而单独成文件。
- 闭环必须满足守恒公式 `rollout_batch_size × n_samples_per_prompt = global_batch_size × num_steps_per_rollout`；只有显式设了 `--num-steps-per-rollout` 时 slime 才会自动推导或 assert 校验 `global_batch_size`（[arguments.py:1907-1915](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/utils/arguments.py#L1907-L1915)）。
- `--colocate` 让训练推理共用同一组 GPU，默认令 `rollout_num_gpus = actor 卡数`，并强制 offload；共卡时须调小 `--sglang-mem-fraction-static` 防 OOM（run-qwen3-4B 用 0.7）。
- `--sglang-` 前缀是 slime 的透传约定：任何 SGLang 参数加这个前缀就会被自动接住并转发（[sglang_utils/arguments.py:92-94](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/sglang_utils/arguments.py#L92-L94)），这是 slime「升级零成本」的关键设计。
- SGLang 拓扑由 `rollout-num-gpus` 和 `rollout-num-gpus-per-engine` 推出：tp_size = 每引擎卡数，dp_size = 总推理卡 / 每引擎卡数。

## 7. 下一步学习建议

本讲只让你「看懂怎么启动」，但没进入 `train.py` 的内部循环。建议接下来：

- **u1-l6（训练主循环 train.py 全景）**：顺着本讲的 `python3 train.py` 往里读，看 rollout → train → save → update_weights → eval 的完整循环是怎么写的。本讲提到的供需公式，会在那里体现为「一轮里到底采几次样、训几步」。
- **u1-l5（模型权重转换）**：如果你想真的准备一个能跑的检查点，需要学 `tools/convert_hf_to_torch_dist.py`，理解为什么 `--ref-load` 必须是 torch_dist 格式而非 HF 格式。
- **延伸阅读**：想看其它规模/模型的启动范式，可对比 `scripts/run-glm4-9B.sh`（标准分离配置）与 `scripts/run-qwen3-30B-A3B.sh`（MoE 多节点），体会参数组在不同规模下如何调整。
