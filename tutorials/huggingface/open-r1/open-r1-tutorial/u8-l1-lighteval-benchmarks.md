# LightEval 基准评估

## 1. 本讲目标

训练出一个模型只是开始，要回答「它到底有多强」，必须跑**基准评估（benchmark）**：用一组带标准答案的考题去测模型，得到可横向比较的分数。open-r1 全程使用 Hugging Face 的 [lighteval](https://github.com/huggingface/lighteval) 框架来做这件事。

本讲聚焦「评估这一步是如何被组织起来的」。学完后你应当能够：

1. 看懂 lighteval 的**任务字符串（task string）**格式，并理解 open-r1 如何用 `LIGHTEVAL_TASKS` 注册表集中管理所有考题。
2. 用一条 `make evaluate` 命令在本地单机上跑通一个基准（或在没有 GPU 时打印出最终命令做 dry-run），并解释 `MODEL_ARGS` 每个参数的含义。
3. 理解 `run_lighteval_job` 如何根据**模型参数量**和**注意力头数**自动决定用「数据并行」还是「张量并行」，并在 Slurm 上提交评估作业。
4. 知道评估的三个入口：训练后自动评估（回调）、`scripts/run_benchmarks.py` 命令行、`make evaluate` 本地命令。

本讲属于「评估与测试」单元的评估篇，承接 [u1-l3 安装与环境搭建](u1-l3-installation-setup.md)（评估依赖 `lighteval` + `vllm`，需先装好环境），也与 [u2-l1 SFT 训练脚本主流程](u2-l1-sft-script-walkthrough.md) 中的「评估阶段」、[u7-l3 回调与 Hub 推送](u7-l3-callbacks-hub-wandb.md) 中的「推送完成后跑基准」相呼应。

---

## 2. 前置知识

### 2.1 什么是基准评估

训练好的语言模型需要量化它的能力。做法是：选一批**有标准答案的题目**（比如数学题集 MATH-500、竞赛 AIME、代码生成 LiveCodeBench），让模型作答，再用一个**判分规则**把模型的输出和标准答案比对，给出一个 0~1（或百分比）的分数。这套「考题 + 判分」就叫一个**任务（task）/ 基准（benchmark）**。

不同的 benchmark 测不同能力：MATH-500/AIME 测数学推理，GPQA Diamond 测研究生级科学知识，LiveCodeBench 测代码生成。DeepSeek-R1 论文正是用这几个基准来汇报成绩，open-r1 要复现，就必须用**同一套题、同一套判分**，才能和论文对齐。

### 2.2 lighteval 与 vLLM

[lighteval](https://github.com/huggingface/lighteval) 是 Hugging Face 的轻量评估框架。它的角色是：

- 接收一个**任务字符串**（告诉它考哪套题、几 shot）。
- 接收**模型参数**（告诉它用哪个模型、什么精度、怎么生成）。
- 内部用 **vLLM** 做推理后端（加载模型、批量生成回答），再用任务自带的判分器打分。
- 输出结果到目录，可选上传到 Hub。

vLLM 是高吞吐推理引擎，评估时用它而非 transformers 原生生成，是因为评估常常要**每题采样很多次**（AIME 要 64 次、LiveCodeBench 要 16 次，见 README 第 531–536 行表）来估计 `pass@1`，吞吐很关键。

### 2.3 两种并行：data parallel vs tensor parallel

多卡评估时，vLLM 有两种并行策略，理解它们的区别是本讲核心之一：

- **数据并行（data parallel, DP）**：每张 GPU 放**一份完整的模型副本**，把考题分摊到各卡，各跑各的。优点是**线性提升吞吐**、卡间几乎不用通信；前提是**模型能塞进单卡显存**。适合小模型。
- **张量并行（tensor parallel, TP）**：把**一个模型切片**，多张 GPU 合力跑同一个请求。优点是**能装下超大模型**；代价是每个请求都要跨卡通信、更慢。适合单卡装不下的大模型。

直觉记法：**模型装得下 → DP（图快）；装不下 → TP（图活）**。open-r1 的评估代码就是按「参数量是否 ≥ 30B」在两者间切换。

---

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [src/open_r1/utils/evaluation.py](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/utils/evaluation.py) | 评估核心：任务注册表、`run_lighteval_job`（决定并行策略并提交 Slurm 作业）、`run_benchmark_jobs`（遍历基准） |
| [src/open_r1/utils/hub.py](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/utils/hub.py) | 两个判定函数：`get_param_count_from_repo_id`（数参数量）、`get_gpu_count_for_vllm`（按注意力头数算 GPU 数） |
| [scripts/run_benchmarks.py](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/scripts/run_benchmarks.py) | 命令行入口：把模型 id + 基准名翻译成对 `run_benchmark_jobs` 的调用 |
| [Makefile](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/Makefile) | `make evaluate` 目标：在本地拼出并直接执行 `lighteval vllm` 命令 |
| [slurm/evaluate.slurm](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/slurm/evaluate.slurm) | 集群上真正跑 lighteval 的脚本，由 `run_lighteval_job` 提交 |
| [src/open_r1/utils/callbacks.py](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/utils/callbacks.py) | 训练后自动触发评估的回调 |

---

## 4. 核心概念与源码讲解

### 4.1 基准任务注册：LIGHTEVAL_TASKS 与 register_lighteval_task

#### 4.1.1 概念说明

lighteval 用一个**任务字符串**来指定「考哪套题、怎么考」。它的规范格式是：

```
{suite}|{task_name}|{num_fewshot}|{tail}
```

- 第 1 段 `suite`：任务所属的套件，最常见是 `lighteval`（官方核心任务）或 `extended`（扩展任务）。
- 第 2 段 `task_name`：具体任务名，如 `math_500`、`aime24`、`gpqa:diamond`、`lcb:codegeneration`。
- 第 3 段 `num_fewshot`：few-shot 示例数（0 表示 zero-shot，不给样例直接考）。open-r1 全部用 0。
- 第 4 段 `tail`：lighteval 规范要求的尾部字段，open-r1 固定填 `0`。

例如 `lighteval|math_500|0|0` 表示「用 lighteval 核心套件的 math_500 任务，zero-shot 考」；`extended|lcb:codegeneration|0|0` 表示「用扩展套件的 LiveCodeBench 代码生成任务」。

open-r1 不想让这些字符串散落在各处，于是沿用本项目反复出现的**注册表模式**（与 [u3-l2 奖励注册表](u3-l2-reward-registry-accuracy.md)、[u7-l3 回调注册表](u7-l3-callbacks-hub-wandb.md) 同构）：用一个字典 `LIGHTEVAL_TASKS` 把「短名 → 完整任务字符串」集中登记。

#### 4.1.2 核心流程

注册流程是「定义一个登记函数 + 在模块加载时调用它若干次」：

1. `register_lighteval_task(configs, eval_suite, task_name, task_list, num_fewshot)`：把传入的 `task_list`（可能是逗号分隔的多个任务）逐个套上 `{suite}|{task}|{num_fewshot}|0` 外壳，写进 `configs[task_name]`。
2. 模块级依次调用它登记 6 个基准。
3. `get_lighteval_tasks()` 返回所有已登记的短名列表。
4. `SUPPORTED_BENCHMARKS = get_lighteval_tasks()` 作为对外公开的「支持列表」。

#### 4.1.3 源码精读

登记函数把短名翻译成 lighteval 字符串（[src/open_r1/utils/evaluation.py:27-49](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/utils/evaluation.py#L27-L49)）：

```python
def register_lighteval_task(configs, eval_suite, task_name, task_list, num_fewshot=0):
    # 把 task_list 里每个任务都套上 "suite|task|fewshot|0" 外壳
    task_list = ",".join(f"{eval_suite}|{task}|{num_fewshot}|0" for task in task_list.split(","))
    configs[task_name] = task_list
```

模块加载时登记 6 个基准（[src/open_r1/utils/evaluation.py:52-59](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/utils/evaluation.py#L52-L59)）：

```python
LIGHTEVAL_TASKS = {}
register_lighteval_task(LIGHTEVAL_TASKS, "lighteval", "math_500", "math_500", 0)
register_lighteval_task(LIGHTEVAL_TASKS, "lighteval", "aime24", "aime24", 0)
register_lighteval_task(LIGHTEVAL_TASKS, "lighteval", "aime25", "aime25", 0)
register_lighteval_task(LIGHTEVAL_TASKS, "lighteval", "gpqa", "gpqa:diamond", 0)
register_lighteval_task(LIGHTEVAL_TASKS, "extended", "lcb", "lcb:codegeneration", 0)
register_lighteval_task(LIGHTEVAL_TASKS, "extended", "lcb_v4", "lcb:codegeneration_v4", 0)
```

注意短名和真实任务名并非总一致：`gpqa` 这个短名映射到 `gpqa:diamond`（只考 Diamond 子集），`lcb` 映射到 `lcb:codegeneration`。这就是注册表的价值——**对外用稳定短名，内部做映射**。

最后对外暴露支持列表（[src/open_r1/utils/evaluation.py:62-66](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/utils/evaluation.py#L62-L66)）：

```python
def get_lighteval_tasks():
    return list(LIGHTEVAL_TASKS.keys())

SUPPORTED_BENCHMARKS = get_lighteval_tasks()
```

#### 4.1.4 代码实践

**目标**：用 `run_benchmarks.py` 的 `--list-benchmarks` 打印出当前支持的全部基准（不需要 GPU，纯字符串操作）。

**步骤**：

```bash
# 在仓库根目录，确保 PYTHONPATH 指向 src（Makefile 已 export，手动跑也行）
PYTHONPATH=src python scripts/run_benchmarks.py --list-benchmarks
```

**应观察**：终端打印 `Supported benchmarks:` 及 6 个短名（`math_500`、`aime24`、`aime25`、`gpqa`、`lcb`、`lcb_v4`）。

**预期结果**：与 `LIGHTEVAL_TASKS.keys()` 完全一致。这条命令的实现在 [scripts/run_benchmarks.py:42-46](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/scripts/run_benchmarks.py#L42-L46)，它直接遍历 `SUPPORTED_BENCHMARKS` 打印。

> 若 `lighteval`/`trl` 等依赖未安装，导入 `run_benchmarks.py` 会失败。可退而用「源码阅读型实践」：在 Python 里 `import` 不到时，直接阅读上述源码行，手写出 `LIGHTEVAL_TASKS["gpqa"]` 的值。

#### 4.1.5 小练习与答案

**练习 1**：`register_lighteval_task(LIGHTEVAL_TASKS, "extended", "lcb", "lcb:codegeneration", 0)` 执行后，`LIGHTEVAL_TASKS["lcb"]` 的值是什么？

**答案**：`"extended|lcb:codegeneration|0|0"`。注意 suite 是 `extended`，任务名是 `lcb:codegeneration`。

**练习 2**：如果想新增一个「5-shot 的某任务」，应该改哪一行代码？任务字符串会变成什么？

**答案**：在 [evaluation.py:52-59](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/utils/evaluation.py#L52-L59) 区块新增一次 `register_lighteval_task(..., num_fewshot=5)` 调用，对应字符串第 3 段会变成 `5`，例如 `lighteval|xxx|5|0`。

---

### 4.2 make evaluate：本地单机评估入口

#### 4.2.1 概念说明

最直接的评估方式是**在本地机器上直接跑 lighteval**。README 第 446–505 行给出了原始命令，而 `make evaluate`（[Makefile:35-53](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/Makefile#L35-L53)）把这些命令封装成一个目标，只需要传模型、任务、并行方式即可。

它的本质是：**用 make 函数在「解析时」算出并行参数，拼出 `MODEL_ARGS` 字符串，再在「执行时」调用 `lighteval vllm`**。所以 `make evaluate` 不经过 `evaluation.py`，而是直接走 lighteval CLI——这是它与 Slurm 路线（4.3 节）的根本区别。

#### 4.2.2 核心流程

`make evaluate` 接收四个 make 变量：

| 变量 | 必填 | 含义 |
|------|------|------|
| `MODEL` | 是 | 模型 id，如 `deepseek-ai/DeepSeek-R1-Distill-Qwen-1.5B` |
| `TASK` | 是 | 任务短名，如 `math_500`、`aime24`、`lcb` |
| `PARALLEL` | 否 | `data`（数据并行）或 `tensor`（张量并行），不填则单卡 |
| `NUM_GPUS` | 否 | 并行度，配合 `PARALLEL` 使用 |

执行逻辑（伪代码）：

```
若 PARALLEL=data   → PARALLEL_ARGS = "data_parallel_size=$(NUM_GPUS)"
若 PARALLEL=tensor → PARALLEL_ARGS = "tensor_parallel_size=$(NUM_GPUS)"
否则               → PARALLEL_ARGS = ""  （单卡，无并行字段）

若 PARALLEL=tensor → 导出 VLLM_WORKER_MULTIPROC_METHOD=spawn  # vLLM TP 必需

拼 MODEL_ARGS = "pretrained=$(MODEL),dtype=bfloat16,$(PARALLEL_ARGS),max_model_length=32768,gpu_memory_utilization=0.8,generation_parameters={...}"

若 TASK=lcb → lighteval vllm $MODEL_ARGS "extended|lcb:codegeneration|0|0" --use-chat-template --output-dir ...
否则        → lighteval vllm $MODEL_ARGS "lighteval|$(TASK)|0|0"          --use-chat-template --output-dir ...
```

注意 `lcb` 是特例：它走 `extended` 套件，其它都走 `lighteval` 套件。

#### 4.2.3 源码精读

`evaluate` 目标的主体（[Makefile:35-53](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/Makefile#L35-L53)）：

```makefile
evaluate:
	# 解析时算出 PARALLEL_ARGS
	$(eval PARALLEL_ARGS := $(if $(PARALLEL),$(shell \
		if [ "$(PARALLEL)" = "data" ]; then echo "data_parallel_size=$(NUM_GPUS)"; \
		elif [ "$(PARALLEL)" = "tensor" ]; then echo "tensor_parallel_size=$(NUM_GPUS)"; fi),))
	# tensor 模式必须开 spawn
	$(if $(filter tensor,$(PARALLEL)),export VLLM_WORKER_MULTIPROC_METHOD=spawn &&,) \
	MODEL_ARGS="pretrained=$(MODEL),dtype=bfloat16,$(PARALLEL_ARGS),max_model_length=32768,gpu_memory_utilization=0.8,generation_parameters={max_new_tokens:32768,temperature:0.6,top_p:0.95}" && \
	if [ "$(TASK)" = "lcb" ]; then \
		lighteval vllm $$MODEL_ARGS "extended|lcb:codegeneration|0|0" --use-chat-template --output-dir data/evals/$(MODEL); \
	else \
		lighteval vllm $$MODEL_ARGS "lighteval|$(TASK)|0|0" --use-chat-template --output-dir data/evals/$(MODEL); \
	fi
```

逐项解释 `MODEL_ARGS` 各字段：

| 字段 | 含义 | 为什么是这个值 |
|------|------|----------------|
| `pretrained=$(MODEL)` | 要评估的模型 id | 注意 Makefile 用 `pretrained=`，而 Slurm 脚本用 `model_name=`，两者都是 lighteval 合法的模型参数键 |
| `dtype=bfloat16` | 计算精度 bfloat16 | 省显存、与训练精度一致 |
| `data_parallel_size=` / `tensor_parallel_size=` | 并行度 | 由 `PARALLEL` 决定写哪个；单卡时此段为空 |
| `max_model_length=32768` | 最长上下文 32k token | R1 风格的长思维链可能很长，必须给够上下文窗口 |
| `gpu_memory_utilization=0.8` | vLLM 预留 80% 显存 | 留 20% 余量防 OOM |
| `generation_parameters={max_new_tokens:32768,temperature:0.6,top_p:0.95}` | 生成参数 | 采样温度 0.6、top_p 0.95，正是 DeepSeek-R1 官方推荐的推理采样配置 |

#### 4.2.4 代码实践

**目标**：用 `make -n`（dry-run）打印出 `make evaluate` **最终会执行的完整命令**，不真正运行（无 GPU 也能做）。

**步骤**：

```bash
# 单卡评估 math_500（dry-run，只打印不执行）
make -n evaluate MODEL=deepseek-ai/DeepSeek-R1-Distill-Qwen-1.5B TASK=math_500

# 数据并行评估（dry-run）
make -n evaluate MODEL=deepseek-ai/DeepSeek-R1-Distill-Qwen-1.5B TASK=aime24 PARALLEL=data NUM_GPUS=8

# 张量并行评估 32B 大模型（dry-run）
make -n evaluate MODEL=deepseek-ai/DeepSeek-R1-Distill-Qwen-32B TASK=aime24 PARALLEL=tensor NUM_GPUS=8
```

**应观察**：make 打印出一行（或几行）shell 命令，其中 `MODEL_ARGS` 已被完整展开，能看到 `pretrained=...`、`data_parallel_size=8`（或 `tensor_parallel_size=8`）、`generation_parameters={...}` 等字段。tensor 模式下应能看到前置的 `export VLLM_WORKER_MULTIPROC_METHOD=spawn`。

**预期结果**：

- 单卡命令形如 `lighteval vllm "pretrained=deepseek-ai/DeepSeek-R1-Distill-Qwen-1.5B,dtype=bfloat16,,max_model_length=32768,..." "lighteval|math_500|0|0" --use-chat-template --output-dir data/evals/...`。
- 注意单卡时 `PARALLEL_ARGS` 为空，`MODEL_ARGS` 里会出现连续两个逗号（`dtype=bfloat16,,max_model_length`）——这是 Makefile 拼接的副作用，lighteval 会忽略空字段。
- 若有 GPU 且依赖已装（参考 [u1-l3](u1-l3-installation-setup.md)），去掉 `-n` 即可真正运行，结果写入 `data/evals/<MODEL>/`。

**如果无法确定运行结果**：本地无 GPU 时，本实践即 dry-run，明确标注「待本地验证真实运行结果」。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `PARALLEL=tensor` 时要先 `export VLLM_WORKER_MULTIPROC_METHOD=spawn`？

**答案**：vLLM 的张量并行需要在多进程间共享 CUDA 张量，默认的 `fork` 启动方式在某些环境下会出问题，`spawn` 更安全。数据并行不需要，所以只有 tensor 分支才导出。README 第 449、501 行的手动示例也强调了这一点。

**练习 2**：用 `make evaluate` 跑 `lcb` 时，任务字符串会是什么？为什么和 `math_500` 不同？

**答案**：是 `extended|lcb:codegeneration|0|0`，因为 LiveCodeBench 属于 lighteval 的 `extended` 扩展套件而非 `lighteval` 核心套件，所以 Makefile 用了 `if TASK=lcb` 的特例分支。

---

### 4.3 run_lighteval_job：Slurm 评估任务的 GPU 与张量并行决策

#### 4.3.1 概念说明

`make evaluate` 适合本地单机。但在生产中，评估常常在 Slurm 集群上以作业形式提交——尤其训练刚结束、要把新 checkpoint 推到 Hub 后立刻评估时。这条链路由 `run_lighteval_job`（[src/open_r1/utils/evaluation.py:69-103](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/utils/evaluation.py#L69-L103)）驱动。

它的核心职责只有一个：**根据模型大小，自动决定用多少 GPU、用 DP 还是 TP，然后拼一条 `sbatch slurm/evaluate.slurm ...` 命令提交作业**。决策依据是两条规则：

1. **vLLM 的硬约束**：张量并行时，GPU 数必须同时整除注意力头数和 64。这由 `get_gpu_count_for_vllm` 保证。
2. **模型参数量**：参数量 ≥ 30B 的大模型用 TP（切片），否则用 DP（副本）。

#### 4.3.2 核心流程

决策算法（伪代码）：

```
task_list   = LIGHTEVAL_TASKS[benchmark]
model_name  = training_args.hub_model_id
revision    = training_args.hub_model_revision

# 规则 1：按注意力头数算出 vLLM 允许的 GPU 数（从 8 往下减）
num_gpus = get_gpu_count_for_vllm(model_name, revision)

# 规则 2：按参数量决定并行策略
if get_param_count_from_repo_id(model_name) >= 30_000_000_000:
    tensor_parallel = True          # 大模型：TP，num_gpus 保持上面算的值
else:
    num_gpus = 2                    # 小模型：DP，固定 2 卡（代码注释：Hack while cluster is full）
    tensor_parallel = False

拼 sbatch 命令：--gres=gpu:{num_gpus} --job-name=... slurm/evaluate.slurm <benchmark> <task_list> <model> <revision> <tensor_parallel> <trust_remote_code> [base64(system_prompt)]
subprocess.run(cmd, check=True)
```

#### 4.3.3 源码精读

决策与命令拼装的主体（[src/open_r1/utils/evaluation.py:74-103](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/utils/evaluation.py#L74-L103)）：

```python
task_list = LIGHTEVAL_TASKS[benchmark]
model_name = training_args.hub_model_id
model_revision = training_args.hub_model_revision
num_gpus = get_gpu_count_for_vllm(model_name, model_revision)
if get_param_count_from_repo_id(model_name) >= 30_000_000_000:
    tensor_parallel = True
else:
    num_gpus = 2  # Hack while cluster is full
    tensor_parallel = False
```

注意一个关键细节：**小模型分支里，`num_gpus` 被「硬覆盖」为 2**，前面 `get_gpu_count_for_vllm` 算出的值被丢弃了。也就是说，基于注意力头数的 GPU 计算**只对 ≥ 30B 的大模型真正生效**。代码注释 `Hack while cluster is full` 表明这是集群资源紧张时的临时妥协。

参数量怎么来的？`get_param_count_from_repo_id`（[src/open_r1/utils/hub.py:89-118](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/utils/hub.py#L89-L118)）有两层：

```python
def get_param_count_from_repo_id(repo_id):
    try:
        metadata = get_safetensors_metadata(repo_id)       # 优先读权重元数据
        return list(metadata.parameter_count.values())[0]
    except Exception:
        pattern = r"((\d+(\.\d+)?)(x(\d+(\.\d+)?))?)([bm])"  # 回退：正则解析 repo id
        matches = re.findall(pattern, repo_id.lower())
        ...
        return int(max(param_counts)) if param_counts else -1  # 都失败返回 -1
```

即「先读 safetensors 元数据里的真实参数量；读不到就从 repo id 里正则抠数字（如 `1.5b`、`8x7b`）；再读不到返回 `-1`」。返回 `-1` 时 `-1 >= 30B` 为假，会走小模型（DP）分支——这是一种**保守的兜底**。

GPU 数怎么来的？`get_gpu_count_for_vllm`（[src/open_r1/utils/hub.py:121-132](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/utils/hub.py#L121-L132)）：

```python
def get_gpu_count_for_vllm(model_name, revision="main", num_gpus=8):
    config = AutoConfig.from_pretrained(model_name, revision=revision, trust_remote_code=True)
    num_heads = config.num_attention_heads
    while num_heads % num_gpus != 0 or 64 % num_gpus != 0:
        num_gpus -= 1
    return num_gpus
```

约束可形式化为（设 \(H\) 为注意力头数）：

\[
\text{valid}(g) \iff (H \bmod g = 0) \land (64 \bmod g = 0), \qquad g^{*} = \max\{\, g \in \{1,\dots,8\} : \text{valid}(g) \,\}
\]

从 \(g=8\) 往下减，直到同时满足两个整除条件。例如 \(H=32\)：\(32\bmod 8=0\) 且 \(64\bmod 8=0\)，得 \(g^{*}=8\)。

最后是命令拼装（[src/open_r1/utils/evaluation.py:85-103](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/utils/evaluation.py#L85-L103)）：

```python
cmd = VLLM_SLURM_PREFIX.copy()
cmd_args = [
    f"--gres=gpu:{num_gpus}",
    f"--job-name=or1_{benchmark}_{model_name.split('/')[-1]}_{model_revision}",
    "slurm/evaluate.slurm", benchmark, f'"{task_list}"',
    model_name, model_revision, f"{tensor_parallel}", f"{model_args.trust_remote_code}",
]
if training_args.system_prompt is not None:
    # base64 编码，避免特殊字符破坏 sbatch 命令行；在 slurm 脚本里解码
    prompt_encoded = base64.b64encode(training_args.system_prompt.encode()).decode()
    cmd_args.append(prompt_encoded)
cmd[-1] += " " + " ".join(cmd_args)
subprocess.run(cmd, check=True)
```

两个要点：

- `VLLM_SLURM_PREFIX`（[evaluation.py:18-24](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/utils/evaluation.py#L18-L24)）用 `env -i bash -c "... source /etc/profile.d/*.sh ...; sbatch "` 包裹，目的是在一个**干净的环境**里启动 sbatch，避免训练任务里残留的 DeepSpeed 等环境变量泄漏到评估进程（`slurm/evaluate.slurm:49` 也有对应的 `ACCELERATE_USE_DEEPSPEED=false`）。
- `system_prompt` 用 **base64 编码**后作为第 7 个位置参数传给 slurm 脚本，脚本里再 `base64 --decode` 还原（[slurm/evaluate.slurm:57](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/slurm/evaluate.slurm#L57)），避免 system prompt 里的引号、换行破坏命令行。

slurm 脚本拿到 `tensor_parallel` 标志后，决定 `MODEL_ARGS` 用 `tensor_parallel_size` 还是 `data_parallel_size`（[slurm/evaluate.slurm:38-42](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/slurm/evaluate.slurm#L38-L42)）：

```bash
if [ "$TENSOR_PARALLEL" = "True" ]; then
    MODEL_ARGS="...,tensor_parallel_size=$NUM_GPUS,..."
else
    MODEL_ARGS="...,data_parallel_size=$NUM_GPUS,..."
fi
```

#### 4.3.4 代码实践（源码阅读型）

**目标**：针对几种真实模型，手算 `run_lighteval_job` 的决策结果，验证对规则的理解。

**步骤**：

1. 阅读 [hub.py:89-132](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/utils/hub.py#L89-L132) 与 [evaluation.py:78-83](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/utils/evaluation.py#L78-L83)。
2. 对下表三个模型，分别推断：参数量分支、最终 `num_gpus`、`tensor_parallel` 值。

| 模型 | 参数量（约） | 走哪个分支 | num_gpus | tensor_parallel |
|------|------------|-----------|----------|-----------------|
| `DeepSeek-R1-Distill-Qwen-1.5B` | 1.5B | 小模型（< 30B） | 2（硬编码） | False（DP） |
| `DeepSeek-R1-Distill-Qwen-7B` | 7B | 小模型（< 30B） | 2（硬编码） | False（DP） |
| `DeepSeek-R1-Distill-Qwen-32B` | 32B | 大模型（≥ 30B） | 由 `get_gpu_count_for_vllm` 算 | True（TP） |

**应观察/预期结果**：

- 前两者被「一视同仁」地强制设为 2 卡 DP——这就是 `Hack while cluster is full` 的效果，7B 本可上更多卡但被压到 2 卡。
- 32B 才真正进入 TP 分支，`num_gpus` 取决于其注意力头数是否被 8（及 64）整除。

> 待本地验证：在有网络的机器上 `python -c "from open_r1.utils.hub import get_gpu_count_for_vllm, get_param_count_from_repo_id; print(get_param_count_from_repo_id('deepseek-ai/DeepSeek-R1-Distill-Qwen-32B'))"` 可看真实参数量与计算过程。

#### 4.3.5 小练习与答案

**练习 1**：一个 repo id 既读不到 safetensors 元数据、正则也匹配不到任何 `数字+b/m` 模式时，`get_param_count_from_repo_id` 返回什么？`run_lighteval_job` 会因此走哪个分支？

**答案**：返回 `-1`（[hub.py:118](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/utils/hub.py#L118)）。由于 `-1 >= 30_000_000_000` 为假，会走**小模型（DP）分支**，`num_gpus=2`、`tensor_parallel=False`。这是一种保守兜底。

**练习 2**：`get_gpu_count_for_vllm` 为什么要检查 `64 % num_gpus == 0`？

**答案**：这是 vLLM 张量并行的硬约束之一，与注意力机制（尤其 GQA/分组注意力的头数划分）相关。不满足会导致 vLLM 启动报错，所以代码从 8 往下减直到两个整除条件都满足。

---

### 4.4 run_benchmarks.py 与训练后自动评估

#### 4.4.1 概念说明

`run_lighteval_job` 一次只评估一个基准。实际中我们常要「一次跑多个基准」或「训练完自动评估」。open-r1 提供了三个入口汇入 `run_lighteval_job`：

1. **`scripts/run_benchmarks.py`**：命令行工具，指定模型 id 和基准列表，手动提交 Slurm 评估作业。
2. **训练后自动评估**：`PushToHubRevisionCallback` 在把 checkpoint 推到 Hub 后，若检测到 Slurm 可用，自动调用 `run_benchmark_jobs` 评估（与 [u7-l3](u7-l3-callbacks-hub-wandb.md) 衔接）。
3. **`make evaluate`**：本地直接跑（4.2 节，不进 Slurm）。

`run_benchmark_jobs`（[evaluation.py:106-118](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/utils/evaluation.py#L106-L118)）是前两个入口共享的「遍历器」。

#### 4.4.2 核心流程

```
run_benchmark_jobs(training_args, model_args):
    benchmarks = training_args.benchmarks
    if benchmarks == ["all"]:            # 魔法值 "all" 展开为全部支持基准
        benchmarks = get_lighteval_tasks()
    for benchmark in benchmarks:
        if benchmark in 支持列表:
            run_lighteval_job(benchmark, ...)   # 逐个提交 Slurm 作业
        else:
            raise ValueError("Unknown benchmark ...")   # 拼写校验
```

#### 4.4.3 源码精读

`run_benchmark_jobs` 的遍历与校验（[src/open_r1/utils/evaluation.py:106-118](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/utils/evaluation.py#L106-L118)）：

```python
def run_benchmark_jobs(training_args, model_args):
    benchmarks = training_args.benchmarks
    if len(benchmarks) == 1 and benchmarks[0] == "all":
        benchmarks = get_lighteval_tasks()   # "all" → 全部 6 个基准
    for benchmark in benchmarks:
        print(f"Launching benchmark `{benchmark}`")
        if benchmark in get_lighteval_tasks():
            run_lighteval_job(benchmark, training_args, model_args)
        else:
            raise ValueError(f"Unknown benchmark {benchmark}")
```

两个要点：一是 `"all"` 是一个**魔法值**，只在「列表恰好只有一个元素且为 `"all"`」时展开，所以 `["math_500", "all"]` 不会被展开（会被当成普通列表，且 `"all"` 不在支持列表里会抛错）。二是每个基准名都要在 `get_lighteval_tasks()` 里查得到，否则直接报错——这是**输入校验**，避免拼写错误悄悄静默失败。

`scripts/run_benchmarks.py` 是它的命令行封装（[scripts/run_benchmarks.py:39-57](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/scripts/run_benchmarks.py#L39-L57)）：

```python
def main():
    parser = TrlParser(ScriptArguments)
    args = parser.parse_args_and_config()[0]
    if args.list_benchmarks:                 # --list-benchmarks：只打印支持列表
        for benchmark in SUPPORTED_BENCHMARKS:
            print(f"  - {benchmark}")
        return
    benchmark_args = SFTConfig(               # 用 SFTConfig 当轻量载体，只填评估需要的字段
        output_dir="", hub_model_id=args.model_id,
        hub_model_revision=args.model_revision,
        benchmarks=args.benchmarks, system_prompt=args.system_prompt,
    )
    run_benchmark_jobs(benchmark_args, ModelConfig(...))
```

注意它**借用 `SFTConfig` 当配置载体**（只填 `hub_model_id`/`hub_model_revision`/`benchmarks`/`system_prompt` 等评估需要的字段，其余留空），因为 `run_benchmark_jobs`/`run_lighteval_job` 只读这几个字段。README 第 570、603、634、664 行给出的用法就是：

```bash
python scripts/run_benchmarks.py --model-id {model_id} --benchmarks math_500
```

最后看训练后自动评估的集成（[src/open_r1/utils/callbacks.py:70-77](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/utils/callbacks.py#L70-L77)）：

```python
if is_slurm_available():
    dummy_config.benchmarks = args.benchmarks
    def run_benchmark_callback(_):
        print(f"Checkpoint {global_step} pushed to hub.")
        run_benchmark_jobs(dummy_config, self.model_config)
    future.add_done_callback(run_benchmark_callback)
```

它把 `run_benchmark_jobs` 挂成 Hub 上传 `Future` 的**完成回调**：只有当 checkpoint 推送完成后、且集群上有 Slurm 时，才触发评估。`is_slurm_available()`（[callbacks.py:28-34](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/utils/callbacks.py#L28-L34)）通过尝试 `sinfo` 命令判断——本地机器没有 Slurm 时这段自动评估会被跳过，不会误触发。

#### 4.4.4 代码实践

**目标**：体会 `run_benchmark_jobs` 的 `"all"` 展开与校验行为（无需 GPU）。

**步骤**（示例代码，非项目原有，仅用于理解逻辑）：

```python
# 示例代码：在能 import open_r1 的环境里运行（需要 trl 等依赖）
from open_r1.utils.evaluation import get_lighteval_tasks, SUPPORTED_BENCHMARKS

# 1. 看 "all" 会展开成哪些基准
print(get_lighteval_tasks())

# 2. 模拟 run_benchmark_jobs 的校验逻辑
def fake_run(benchmarks):
    if len(benchmarks) == 1 and benchmarks[0] == "all":
        benchmarks = get_lighteval_tasks()
    for b in benchmarks:
        if b not in get_lighteval_tasks():
            raise ValueError(f"Unknown benchmark {b}")
        print("would launch:", b)

fake_run(["all"])              # 应展开为 6 个基准
fake_run(["math_500", "gpqa"]) # 正常
# fake_run(["math500"])        # 拼写错误，会抛 ValueError
```

**应观察**：第一次打印 6 个短名；`fake_run(["all"])` 逐行打印 6 个 `would launch`；拼写错误版本抛 `ValueError`。

**预期结果**：与 `SUPPORTED_BENCHMARKS` 一致。若环境缺依赖无法 import，则改为阅读 [evaluation.py:106-118](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/utils/evaluation.py#L106-L118) 源码，手动推演。

> 待本地验证：依赖齐备时的真实 import 与执行结果。

#### 4.4.5 小练习与答案

**练习 1**：`run_benchmark_jobs(["math_500", "all"])` 会发生什么？

**答案**：不会展开 `"all"`（因为列表长度是 2，不满足 `len==1`）。遍历时 `"all"` 不在 `get_lighteval_tasks()` 里，抛 `ValueError: Unknown benchmark all`。

**练习 2**：本地机器（无 Slurm）训练完一个 checkpoint，会自动跑基准吗？

**答案**：不会。[callbacks.py:70](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/utils/callbacks.py#L70) 的 `if is_slurm_available()` 为假，自动评估回调被跳过。本地评估需手动用 `make evaluate`。

---

## 5. 综合实践

**任务**：为一个 distilled 模型**端到端**走一遍 math_500 评估，先用 dry-run 看清命令、再（有 GPU 时）真正跑通，并用自己的话解释每个参数。

**步骤**：

1. **列出支持的基准**，确认 `math_500` 在内：

   ```bash
   PYTHONPATH=src python scripts/run_benchmarks.py --list-benchmarks
   ```

2. **dry-run 查看最终命令**：

   ```bash
   make -n evaluate MODEL=deepseek-ai/DeepSeek-R1-Distill-Qwen-1.5B TASK=math_500
   ```

3. **解读命令**：把打印出的 `MODEL_ARGS` 拆成一张表，逐字段写明含义（参考 4.2.3 节的表格），并解释任务字符串 `lighteval|math_500|0|0` 的四段分别是什么。

4. **（有 GPU 时）真正运行**，去掉 `-n`：

   ```bash
   make evaluate MODEL=deepseek-ai/DeepSeek-R1-Distill-Qwen-1.5B TASK=math_500
   ```

   结果会写入 `data/evals/deepseek-ai/DeepSeek-R1-Distill-Qwen-1.5B/`。

5. **对比 Slurm 路线**：阅读 [evaluation.py:78-83](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/utils/evaluation.py#L78-L83)，说明 1.5B 这个小模型如果改走 `run_lighteval_job`（Slurm），会被分配几卡、用 DP 还是 TP（答案：2 卡 DP，因为 < 30B）。

**需要观察的现象**：

- dry-run 打印的命令里 `pretrained=` 后跟完整模型 id，`generation_parameters={max_new_tokens:32768,temperature:0.6,top_p:0.95}` 完整出现。
- 真实运行时 vLLM 加载模型、lighteval 跑完 500 道数学题、输出一个准确率分数（README 第 579 行报告 1.5B 模型 MATH-500 约 83.1）。

**预期结果**：

- 能独立解释 `MODEL_ARGS` 全部字段与任务字符串四段含义。
- 能说出 `make evaluate`（本地、直接 lighteval CLI）与 `run_lighteval_job`（Slurm、提交 sbatch）两条路线的区别。

> 若本地无 GPU：完成步骤 1–3、5 即可，步骤 4 标注「待本地验证真实运行结果」。

---

## 6. 本讲小结

- open-r1 用 **lighteval + vLLM** 做评估，任务用规范字符串 `{suite}|{task}|{num_fewshot}|0` 表示，集中在 `LIGHTEVAL_TASKS` 注册表里（`math_500`、`aime24`、`aime25`、`gpqa`、`lcb`、`lcb_v4`），对外暴露为 `SUPPORTED_BENCHMARKS`。
- **本地评估**走 `make evaluate`：它直接拼并执行 `lighteval vllm` 命令，用 `PARALLEL=data|tensor` 选择 `data_parallel_size` 或 `tensor_parallel_size`，单卡时两者皆空。
- **集群评估**走 `run_lighteval_job`：按「参数量 ≥ 30B」决定 TP（大模型）还是 DP（小模型，且 `num_gpus` 被「Hack」硬编码为 2），TP 的 GPU 数还需满足「整除注意力头数且整除 64」的 vLLM 约束。
- `MODEL_ARGS` 关键字段：`pretrained`/`dtype`/并行度/`max_model_length=32768`/`gpu_memory_utilization=0.8`/`generation_parameters`（温度 0.6、top_p 0.95，对齐 DeepSeek-R1 推理采样）。
- 三个评估入口：`make evaluate`（本地）、`scripts/run_benchmarks.py`（手动 Slurm）、`PushToHubRevisionCallback`（训练后自动，仅 Slurm 可用时触发）。
- `run_benchmark_jobs` 支持 `"all"` 展开与基准名校验；system prompt 经 base64 编码传入 sbatch 以避免特殊字符破坏命令行。

---

## 7. 下一步学习建议

- **测试体系**：评估代码本身如何被测试？继续学 [u8-l2 测试体系与代码质量](u8-l2-tests-and-quality.md)，看 `tests/` 下如何用单元测试守护各模块行为。
- **回调与 Hub 推送**：想深入「训练后自动评估」的完整链路，复习 [u7-l3 回调、Hub 版本推送与实验追踪](u7-l3-callbacks-hub-wandb.md)，重点是 `PushToHubRevisionCallback` 的 `Future` 完成回调机制。
- **代码奖励评测**：LiveCodeBench（`lcb`）背后是 open-r1 的代码奖励/沙箱系统，可回到 [u5-l1 代码奖励函数与执行脚本模板](u5-l1-code-reward-template.md) 与 [u6 竞赛编程评分](u6-l1-ioi-scoring.md) 了解代码题如何判分。
- **源码延伸阅读**：通读 [slurm/evaluate.slurm](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/slurm/evaluate.slurm) 全文，理解评估结果如何上传到 `open-r1/open-r1-eval-leaderboard` 与 details 仓库，形成「评估 → 上榜」的闭环。
