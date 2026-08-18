# 评估框架：eval.py 入口与 BaseEvaluator 骨架

## 1. 本讲目标

本讲是评估单元（第 6 单元）的第一篇，目标是把评估侧的「骨架」看清楚。学完本讲，你应该能够：

1. 解释 `eval.py` 如何凭草稿 checkpoint 的 `architectures` 字段查 `EVALUATORS` 表分发出正确的 Evaluator，以及 `TASKS` 如何规定「评哪些数据集、各评多少条」。
2. 说出 `eval_datasets/*.jsonl` 的 `turns` 字段格式约束，以及一条样本从 JSONL 行到 `input_ids` 的完整准备过程。
3. 理解多卡评测时样本如何按 stride 分片、统计量如何跨 rank `all_reduce` 汇总，并亲手推导结果表格里 `accept_len`、`verify_rate`、`accept_rate@k` 每一列的计算公式。

本讲刻意把投机解码的解码循环（`generate_decoding_sample`）和拒绝采样验证（`verify_draft_tokens`）当作黑盒——它们虽然也定义在 `base_evaluator.py` 里，但分别留给 u6-l2 和 u6-l3 逐行精读。本讲只关心「评测一场是怎么组织起来的」。

## 2. 前置知识

- **投机解码回顾（承接 u1-l1）**：小草稿模型先 propose 一串候选 token，大目标模型一次前向 verify，按接受概率逐个接受并兜底采样。评估侧关心的不是答案对不对，而是「草稿像不像目标」——所以本讲的核心指标全部围绕接受长度和接受率展开。
- **spawn 自举与 init_dist（承接 u1-l3）**：`eval.py` 和 `train.py` 一样用 `torch.multiprocessing.spawn` 自举，进程数由 `CUDA_VISIBLE_DEVICES` 决定；`init_dist(local_rank)` 推导出 `device / global_rank / world_size` 三元组。评估侧每个进程持有一份完整的目标模型 + 草稿模型，各自负责不同的样本。
- **集合通信 `all_reduce`（承接 u3-l6）**：把所有 rank 手里的同名张量按位求和后广播回所有人。它是集体操作，所有 rank 必须都调用，否则互相等待直至超时。
- **两个符号约定**：一轮验证中，记草稿提议的有效 token 数为 \( n_t \)（effective proposal length），其中被接受的草稿 token 数为 \( a_t \)。每轮无论接受多少，目标模型都会额外「兜底」提交 1 个 token，所以每轮实际产出 \( a_t + 1 \) 个 token。这两个量是本讲所有指标的原料。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [eval.py](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/eval.py) | 评估命令行入口：`EVALUATORS` 分发表、`TASKS` 配额、spawn 自举 |
| [deepspec/eval/base_evaluator.py](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/base_evaluator.py) | 评估框架本体：数据加载、分片执行、指标汇总与打印；解码循环与验证函数也在本文件（后续两讲精读） |
| [eval_datasets/gsm8k.jsonl](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/eval_datasets/gsm8k.jsonl) | 评测数据集样例（1319 行，每行一个 `turns` 字段） |
| [deepspec/eval/dspark/evaluator.py](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/dspark/evaluator.py) | DSpark 具体评估器，展示子类如何填 `BaseEvaluator` 的抽象钩子 |
| [deepspec/eval/eagle3/evaluator.py](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/eagle3/evaluator.py) | Eagle3 具体评估器（本讲只看类声明，细节留 u6-l6） |
| [deepspec/data/parser.py](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/data/parser.py) | `encode_chat_messages`：把消息列表渲染成 token 序列 |
| [scripts/eval/eval.sh](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/scripts/eval/eval.sh) | 官方启动脚本样例 |

## 4. 核心概念与源码讲解

评估侧的分层与训练侧（第 3 单元）如出一辙：一个薄入口 + 一个模板基类 + 每算法一个子类。`BaseEvaluator` 用模板方法模式规定「评测一场」的固定流程（遍历数据集 → 每卡跑分到的样本 → 汇总 → 打表），把「模型怎么建、单样本怎么生成」下放给子类。

### 4.1 eval.py 分发：EVALUATORS 注册表、TASKS 配额与自举入口

#### 4.1.1 概念说明

训练侧入口 `train.py` 把 `trainer_cls` 直接写在配置文件里（「配置即代码」），而评估侧没有配置文件——它需要一个更轻量的分发机制。DeepSpec 的选择是：**草稿 checkpoint 自带答案**。训练时各模型族的 `build_draft_config` 会往草稿 config 写入 `architectures` 字段（如 `Qwen3DSparkModel`，见 u4-l2），评估入口只需读出这个字符串查表，就能唯一确定该用哪个 Evaluator。这样 `eval.py` 完全不需要用户指定算法类型，`--draft_name_or_path` 一个参数就够了。

`TASKS` 则是评测的「实验设计」：一个 `(数据集名, 样本配额)` 元组列表，规定评哪些基准、各抽多少条。配额小于数据集实际行数时会先洗牌再截断（见 4.3 节）。

#### 4.1.2 核心流程

`eval.py` 的执行流程：

1. 父进程 `parse_args` 解析命令行，并把模块级常量 `TASKS` 复制进 `args.tasks`。
2. `torch.multiprocessing.spawn` 按 `torch.cuda.device_count()`（即 `CUDA_VISIBLE_DEVICES` 的长度）拉起每 GPU 一个 worker，`args` 作为公共参数传给所有 worker。
3. 每个 worker 进入 `main(local_rank, args)`：
   - rank 0 把 `args` 以 JSON 打印出来（实验记录）；
   - `AutoConfig.from_pretrained` 只加载草稿的 config（不加载权重）；
   - `draft_config.architectures[0]` 查 `EVALUATORS` 得到 Evaluator 类；
   - 实例化 `evaluator(local_rank, args)`（构造函数内部会 `init_dist` 并加载两份模型）；
   - `evaluate()` 跑完整场评测，`clean_up()` 销毁进程组。

#### 4.1.3 源码精读

分发表——key 是草稿 config 里的 `architectures[0]`，value 是 Evaluator 类：

[eval.py:L10-L16](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/eval.py#L10-L16)

这段代码定义了 4 种草稿模型类到 4 个 Evaluator 的映射。注意两点：DFlash 的 `architectures` 仍是 `Qwen3DSparkModel`（u5-l3 讲过它只是 DSpark 的配置变体），所以 DFlash checkpoint 自动复用同一个 `Qwen3DSparkEvaluator`，表里不需要单独一行；`Eagle3DraftModel` 是为兼容外部（SpecForge 风格）checkpoint 准备的别名，也指向 `Qwen3Eagle3Evaluator`。

评测任务与配额：

[eval.py:L18-L28](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/eval.py#L18-L28)

9 个基准覆盖数学（gsm8k/math500/aime25）、代码（humaneval/mbpp/livecodebench）和通用对话（mt-bench/alpaca/arena-hard-v2）。第二个元素是每个数据集的抽样上限，例如 gsm8k 文件实际有 1319 行，但只评 500 条。

入口与分发逻辑：

[eval.py:L50-L65](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/eval.py#L50-L65)

`main` 的四行就是评估侧的全部调度：读 config → 查表 → 实例化 → `evaluate()`。若 `architectures[0]` 不在表里，L54 的字典取值会直接 `KeyError`——快速失败，不做任何猜测。`spawn` 的 `nprocs` 写死为 `torch.cuda.device_count()`，与训练侧约定一致：进程数完全由 `CUDA_VISIBLE_DEVICES` 决定，不用 torchrun。

子类一侧只需极少的类属性即可接入框架。以 DSpark 为例：

[deepspec/eval/dspark/evaluator.py:L32-L42](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/dspark/evaluator.py#L32-L42)

`draft_model_cls` 指定草稿模型类（加载哪种 checkpoint），`max_proposal_tokens` 是一个抽象属性，DSpark 直接返回草稿模型的 `block_size`（即一次提议多少个 token，block7 就是 7）。Gemma4 变体则更薄，只换一个类属性：

[deepspec/eval/dspark/evaluator.py:L224-L225](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/dspark/evaluator.py#L224-L225)

[deepspec/eval/eagle3/evaluator.py:L191-L192](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/eagle3/evaluator.py#L191-L192)

#### 4.1.4 代码实践

**实践目标**：验证「checkpoint 自带分发键」这条链路真实成立。

**操作步骤**：

1. 在仓库根目录执行（需要联网，只需 `transformers`，不需要 GPU）：

```bash
# 示例命令（不加载权重，只读 config）
python -c "
from transformers import AutoConfig
for repo in ['deepseek-ai/dspark_qwen3_4b_block7', 'deepseek-ai/dflash_qwen3_4b_block7', 'deepseek-ai/eagle3_qwen3_4b_ttt7']:
    cfg = AutoConfig.from_pretrained(repo)
    print(repo, '->', cfg.architectures)
"
```

2. 对照 `EVALUATORS` 表，写出每个 repo 会分到哪个 Evaluator 类。

**需要观察的现象**：三个 checkpoint 的 `architectures` 输出。

**预期结果**：dspark 与 dflash 都输出 `['Qwen3DSparkModel']`，eagle3 输出 `['Qwen3Eagle3Model']`（或别名 `Eagle3DraftModel`），分别命中表的第 1、1、3/5 行——这解释了为什么 DFlash 不需要任何评估侧专用代码。具体输出待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `eval.py` 不需要一个 `--algorithm` 参数？

**答案**：算法身份已经写在草稿 checkpoint 的 `config.architectures` 里（训练时由各模型族 `build_draft_config` 写入），入口读出后查 `EVALUATORS` 即可。少一个人工参数就少一处「用户说 DSpark、checkpoint 其实是 Eagle3」的一致性风险。

**练习 2**：如果某天新增了 `Llama3DSparkModel`，`eval.py` 需要改哪几处才能评测它？

**答案**：至少两处——在 `EVALUATORS` 表加 `"Llama3DSparkModel": Llama3DSparkEvaluator` 一行；并保证该 Evaluator 已随 `deepspec/eval/...` 包可导入（`eval.py` 顶部的 import 要覆盖到）。`TASKS` 与 `main` 都不用动。

**练习 3**：`spawn` 之后每个 worker 都会执行一遍 `AutoConfig.from_pretrained`，这样设计有什么代价与好处？

**答案**：代价是每个进程各自做一次（很轻，只是读 config，且有本地缓存）；好处是 `main` 函数完全自包含、无共享状态，父子进程之间只传不可变的 `args`，避免了「父进程加载、子进程传递」的复杂序列化问题。

### 4.2 load_and_process_dataset：turns 数据契约与逐样本准备

#### 4.2.1 概念说明

评估框架不依赖 `datasets` 库，评测集就是仓库里的一批 JSONL 文件（`eval_datasets/` 目录）。每行的唯一契约是一个 `turns` 字段：非空字符串列表，存多轮 user 提问。为什么没有 assistant 回复？回忆 u2-l1：训练数据要由目标模型重写 assistant 回复，评测同理——草稿模型的 KPI 是「像目标模型」，所以答案必须由目标模型在投机解码循环里现场生成，数据集只提供问题。

一个容易被忽略的细节：虽然格式允许 `turns` 有多轮，`load_and_process_dataset` 会强制截断到第一轮（`turns[:1]`），`run_dataset` 也只构造单条 user 消息——**当前框架实际只做单轮评测**，`mt-bench` 这类多轮基准也只评第一问。

#### 4.2.2 核心流程

一条样本从 JSONL 到模型的旅程：

```
JSONL 一行 {"turns": ["...问题..."]}
    │ load_and_process_dataset：逐行读、校验 turns、截断为首轮
    ▼
rows: [{"turns": ["...问题..."], ...其余字段原样保留}]
    │ run_dataset：shuffle + 截断到 max_samples，按 stride 分片（见 4.3）
    ▼
messages = [{"role": "user", "content": turns[0]}]
    │ encode_chat_messages：apply_chat_template 渲染 + 分词
    ▼
input_ids: [1, seq_len] 的 LongTensor（含 generation prompt，不含回答）
    │ generate_one_sample →（子类黑盒，u6-l2 展开）
    ▼
SimpleNamespace(output_ids, acceptance_lengths, proposal_lengths, ...)
```

停止词（stop token）的解析也在样本准备阶段完成一次：`resolve_stop_token_ids` 优先读目标模型 `generation_config.eos_token_id`（可能是一个 list，例如 Qwen3 同时有普通 EOS 与思考段结束符），没有才回退 `tokenizer.eos_token_id`，最后去重成 `list[int]`。解码循环每提交一批新 token 都要检查是否命中停止词。

#### 4.2.3 源码精读

数据集加载与 `turns` 契约：

[deepspec/eval/base_evaluator.py:L31-L55](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/base_evaluator.py#L31-L55)

逐行 `json.loads` 后做三重断言：`turns` 是 list、非空、每个元素是字符串；失败信息带上 `文件:行号`，脏数据能立刻定位。L53 的 `row["turns"] = turns[:1]` 就是「只评第一轮」的强制截断。数据集根目录默认 `./eval_datasets`（[L28](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/base_evaluator.py#L28)），所以**eval.py 必须在仓库根目录下启动**，否则 `dataset_path.exists()` 断言失败。

真实数据长这样（gsm8k 第一行，`turns` 只有一个元素，末尾带固定的作答指令）：

[eval_datasets/gsm8k.jsonl:L1](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/eval_datasets/gsm8k.jsonl#L1)

停止词解析：

[deepspec/eval/base_evaluator.py:L82-L97](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/base_evaluator.py#L82-L97)

消息到 token 的转换复用训练侧同一个函数（保证评测与训练的 prompt 渲染完全一致）：

[deepspec/data/parser.py:L183-L200](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/data/parser.py#L183-L200)

`encode_chat_messages` 内部调用 tokenizer 的 `apply_chat_template` 渲染对话文本再分词，`add_special_tokens=False` 防止模板里的特殊 token 被二次添加。`run_dataset` 调用它时传 `add_generation_prompt=True`（拼上 assistant 头，如 `<|im_start|>assistant\n`）和 `enable_thinking=False`（非思考模式，与已发布 checkpoint 的训练方式一致）。

#### 4.2.4 代码实践

**实践目标**：亲眼确认 `turns` 契约与「截断到第一轮」行为。

**操作步骤**：

1. 查看前两行原始数据：

```bash
head -n 2 eval_datasets/gsm8k.jsonl
```

2. 用框架自己的加载函数验证（在仓库根目录执行；该文件顶部 import 了 torch/transformers，需已安装，但无需 GPU）：

```bash
# 示例命令
python -c "
from deepspec.eval.base_evaluator import load_and_process_dataset
rows = load_and_process_dataset('gsm8k')
print('总条数:', len(rows))
print('首条 turns 长度:', len(rows[0]['turns']))
print('字段:', list(rows[0].keys()))
"
```

3. （可选）构造一个两轮的假数据集目录，验证截断。把下面的内容存成 `my_eval_datasets/two_turns.jsonl`（两行，第一行 turns 有 2 个元素）：

```json
{"turns": ["第一问", "第二问"]}
```

然后调用 `load_and_process_dataset('two_turns', dataset_root='my_eval_datasets')`，打印 `rows[0]['turns']`。

**需要观察的现象**：步骤 2 输出的条数（应为 1319）、turns 长度（应为 1）；步骤 3 中假数据的 turns 长度。

**预期结果**：gsm8k 共 1319 行、每行 turns 截断后长度为 1；两轮假数据被截成只剩 `["第一问"]`。注意步骤 3 会在 `DeepSpec-tutorial/` 之外创建临时目录，做完请删除，或直接在 `/tmp` 下建目录并传绝对路径。

#### 4.2.5 小练习与答案

**练习 1**：为什么校验放在加载时用 `assert` 硬失败，而不是跳过坏行？

**答案**：评测指标是对总量敏感的统计量。静默跳过坏行会让「实际评测条数」与预期不符且无人知晓；带上文件名与行号的断言能在第一时间暴露数据制作错误，这与 u2-l1 中 `validate_conversations` 的「宁可立刻失败」是同一工程哲学。

**练习 2**：`turns` 契约允许 `["问题", "追问"]`，框架会评第二问吗？

**答案**：不会。`load_and_process_dataset` 在 L53 强制 `turns[:1]`，`run_dataset` 也只构造一条 user 消息。多轮字段是为数据格式兼容预留的，当前实现是纯单轮评测。

**练习 3**：`resolve_stop_token_ids` 为什么优先 `generation_config.eos_token_id` 而不是 `tokenizer.eos_token_id`？

**答案**：generation_config 是模型作者声明的「生成时用什么当停止词」，可能包含多个 token（list 形式）；tokenizer 的 `eos_token_id` 只有一个且未必与生成配置一致。停止词直接决定 `trim_output_ids` 截到哪里、解码循环何时 break，用错会把回答截断过长或过短。

### 4.3 run_dataset 与指标汇总：多卡分片、allreduce 与结果表

#### 4.3.1 概念说明

这是 `BaseEvaluator` 的主干，也是本讲最核心的模块。它回答三个问题：

1. **样本怎么分**：数据并行按 stride 切——第 \( r \) 个 rank 处理下标满足 \( idx \equiv r \pmod{W} \) 的样本（\( W \) 为 world_size）。每卡独立持有完整的 target + draft 模型，bsz=1 逐条生成。
2. **种子怎么定**：数据集级先 `seed_all(seed)`，随后每条样本 `seed_all(seed + idx)`——种子挂在**样本下标**上而不是 rank 上，所以无论用 1 卡还是 8 卡，同一条样本的采样序列完全一致，多卡评测结果可复现、且与单卡等价。
3. **指标怎么合**：每个 rank 本地把本卡所有样本的逐轮统计累积成计数器（整数求和），再跨 rank `all_reduce` 求和，最后由 rank 0 换算成比率并打表。**先合计数、后除**，与训练侧 `add_metric` 的 ratio 归约（u3-l6）是同一套思路，避免「各卡先平均再平均」的加权错误。

先给出三个指标的定义。设一场评测共 \( T \) 轮验证，第 \( t \) 轮的有效提议长度为 \( n_t \)、接受草稿数为 \( a_t \)：

\[ \text{accept\_len} = \frac{1}{T}\sum_{t=1}^{T}(a_t + 1), \qquad \text{verify\_rate} = \frac{\sum_t a_t}{\sum_t (n_t + 1)} \]

- **accept_len**：平均每轮验证提交多少个 token（接受草稿 + 1 个兜底）。它近似投机解码的期望加速比上界——串行自回归产出同样多 token 需要 \( a_t+1 \) 次目标前向，而投机解码只要 1 次。
- **verify_rate**：目标模型验证过的所有 token（草稿 \( n_t \) 个 + 兜底 1 个）中被接受的比例，即「草稿命中率」。分母 \( \sum_t(n_t+1) \) 正是目标模型一次前向实际算过的 token 总数。
- 两者由一个恒等式联系：\( \displaystyle \text{verify\_rate} = \frac{\text{accept\_len} - 1}{\bar{n} + 1} \)，其中 \( \bar n \) 是表格 `#propose` 列显示的平均有效草稿长度。

第三个指标 **accept_rate@k** 是按提议槽位细分的接受率：

\[ \text{accept\_rate@}k = \frac{\#\{t : a_t > k\}}{\#\{t : n_t > k\}} \]

即「在所有提议长度超过 \( k \) 的轮次中，第 \( k+1 \) 个草稿槽位被接受（前缀未在第 k 位断裂）的比例」。它刻画接受概率随槽位深度的衰减——DSpark 的位置衰减损失（u4-l4）就是冲着这条曲线去的。

#### 4.3.2 核心流程

`BaseEvaluator.evaluate()` 的顶层编排：

```
for dataset_name, max_samples in self.tasks:          # 遍历 TASKS
    responses = run_dataset(dataset_name, max_samples)  # 本卡分到的样本逐条生成
    metric_summary = allreduce_response_metrics(responses)  # 跨卡合计数
    record_dataset_metrics(...)                        # rank 0 换算比率、逐数据集打一行表
report_results()                                       # rank 0 汇总打全表（可选写 TensorBoard）
```

`run_dataset` 内部：

```
seed_all(seed)
rows = load_and_process_dataset(dataset_name)
若 max_samples < len(rows)：random.Random(seed).shuffle(rows); rows = rows[:max_samples]
stop_token_ids = resolve_stop_token_ids(target_model, tokenizer)
for idx in range(global_rank, len(rows), world_size):   # stride 分片
    seed_all(seed + idx)                                 # 种子挂在样本下标上
    input_ids = encode_chat_messages(tokenizer, [user 消息], add_generation_prompt=True, enable_thinking=False)
    responses.append(generate_one_sample(input_ids, stop_token_ids))   # 子类黑盒
```

`allreduce_response_metrics` 内部：本地把每条 response 的三个等长列表（`acceptance_lengths` / `proposal_lengths` / `accepted_draft_lengths`，由 u6-l2 的解码循环产出）折叠进 4 个标量计数器 + 2×`max_proposal_tokens` 个逐槽位计数器，然后把标量拼成一个张量、槽位拼成另一个张量，各做一次 `dist.all_reduce(SUM)`。

#### 4.3.3 源码精读

构造函数与抽象契约——评估版的模板方法基类：

[deepspec/eval/base_evaluator.py:L444-L467](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/base_evaluator.py#L444-L467)

`__init__` 里 `init_dist(local_rank)`（u1-l3 精读过的同一函数）返回 device / global_rank / world_size，随后调用抽象的 `build_models()` 让子类加载两份模型。`max_proposal_tokens`、`build_models`、`generate_one_sample` 三个成员都raise `NotImplementedError`，是子类必须填的钩子。DSpark 子类的 `generate_one_sample` 就是把通用解码循环和自己实现的四个算法钩子组装起来（细节留 u6-l2/u6-l4）：

[deepspec/eval/dspark/evaluator.py:L162-L179](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/dspark/evaluator.py#L162-L179)

`run_dataset` 的分片与逐样本准备：

[deepspec/eval/base_evaluator.py:L513-L548](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/base_evaluator.py#L513-L548)

重点看三行：L522-L526 的「洗牌 + 截断到 `max_samples`」（用 `args.seed` 建独立 `random.Random`，可复现）；L530 的 stride 分片循环 `range(self.global_rank, len(dataset), self.world_size)`；L531 的逐样本种子 `seed_all(int(self.args.seed) + idx)`——`seed_all` 会同时固定 torch/CUDA/random/numpy 四个随机源（[deepspec/utils/\_\_init\_\_.py:L20-L24](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/utils/__init__.py#L20-L24)）。L533-L539 构造单条 user 消息并渲染成 `input_ids`，注意被注释掉的 `enable_thinking=True` 暗示了思考模式是预留能力。

本地累积与逐槽位计数：

[deepspec/eval/base_evaluator.py:L585-L598](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/base_evaluator.py#L585-L598)

每轮验证累加三个量：`proposal_count`（轮数 \( T \)）、`acceptance_length_sum`（\( \sum(a_t+1) \)，注意 L591 存的是含兜底的 `acceptance_length`）、`proposal_length_sum`（\( \sum n_t \)）。内层循环实现逐槽位计数：提议长度 \( n_t > k \) 则第 \( k \) 槽「被提议」+1，接受数 \( a_t > k \) 则「被接受」+1——正是 4.3.1 中 accept_rate@k 的分子分母。

两次 all_reduce：

[deepspec/eval/base_evaluator.py:L600-L618](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/base_evaluator.py#L600-L618)

4 个标量计数器拼成一个 int64 张量一次归约；2×`max_proposal_tokens` 个槽位计数器拼成另一个张量归约（`numel() > 0` 的守卫兼容 `max_proposal_tokens` 为 0 的退化情形）。这两次调用是**集体操作**，所有 rank 都会执行到（`evaluate` 对每个数据集无条件调用本函数），不存在某张卡缺席导致的死等。

比率换算：

[deepspec/eval/base_evaluator.py:L469-L511](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/base_evaluator.py#L469-L511)

`build_metrics_row` 在归约后的全局计数上做除法：`acceptance_length = acceptance_length_sum / proposal_count`、`verify_rate = acceptance_length_sum / (proposal_length_sum + proposal_count)`（分母就是 \( \sum(n_t+1) \)）、`draft_tokens_per_proposal = proposal_length_sum / proposal_count`。某个槽位提议数为 0 时该位记 `None`（表格里显示 `-`），例如长提议很少时高槽位可能没有样本。`proposal_count == 0`（比如所有样本 prefill 后首个 token 即命中停止词）整体置零防除零。

结果表格：

[deepspec/eval/base_evaluator.py:L115-L164](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/base_evaluator.py#L115-L164)

列名固定为 `dataset / target_model / draft_model / #propose / accept_len / verify_rate / accept_rate@0..`，槽位列数取所有数据集行的最大值，不足补 `-`。L154-L156 对应三个格式化：`#propose` 列显示 `f"{draft_tokens_per_proposal:.2f}+1"`（提醒读者每轮还附带 1 个兜底 token）、`accept_len` 两位小数、`verify_rate` 四位小数。模型名取路径 basename，便于把表格直接贴进实验记录。

顶层编排与打印：

[deepspec/eval/base_evaluator.py:L713-L725](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/base_evaluator.py#L713-L725)

[deepspec/eval/base_evaluator.py:L691-L705](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/base_evaluator.py#L691-L705)

`evaluate` 是模板方法：每个数据集「跑 → 归约 → 记录」，`record_dataset_metrics` 只让 rank 0 把行加入 `metrics_rows` 并当场打印一行无表头的小表（`sample_count <= 0` 时跳过）。全部数据集结束后 `report_results` 打印带表头的汇总表；若命令行给了 `--tensorboard-dir` 和 `--step`，还会把每个指标写成 `eval/<dataset>/<metric>` 标量曲线（[L632-L664](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/base_evaluator.py#L632-L664)）——这是把评测挂进训练循环、按 checkpoint step 追踪接受率曲线的钩子。

需要说明：DSpark 子类重写了 `evaluate` 以插入置信度记录器（[deepspec/eval/dspark/evaluator.py:L181-L189](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/dspark/evaluator.py#L181-L189)），但其主体仍是「run_dataset → allreduce → record」同一条流水线，置信度指标本身留到 u6-l5。

#### 4.3.4 代码实践

分两部分：A 是无 GPU 也能跑的机制模拟，B 是本讲的主实践（真机小规模评测）。

**实践 A（无 GPU）：用纯 Python 复算分片与指标公式**

1. 实践目标：验证 stride 分片不重不漏，以及 4.3.1 的三个指标公式与框架实现一致。
2. 操作步骤（示例代码，存为任意临时脚本运行）：

```python
import random

W, seed, max_samples = 2, 980406, 8          # 模拟 2 卡、gsm8k 抽 8 条
rows = list(range(1319))                      # gsm8k 实际行数
random.Random(seed).shuffle(rows)              # 对应 L523-L526
rows = rows[:max_samples]
shards = [rows[r::W] for r in range(W)]        # 对应 L530 的 stride 分片
assert sorted(x for s in shards for x in s) == rows
print("每卡样本数:", [len(s) for s in shards])

rounds = [(7, 3), (7, 7), (5, 2), (7, 0)]     # 每轮 (有效提议 n_t, 接受 a_t)
T = len(rounds)
a_sum, n_sum = sum(a for _, a in rounds), sum(n for n, _ in rounds)
print("accept_len  =", (a_sum + T) / T)                       # 4.0
print("verify_rate =", a_sum / (n_sum + T))                   # 0.4
print("#propose    =", f"{n_sum / T:.2f}+1")                  # 6.50+1
print("恒等式校验  =", (a_sum / T + 1 - 1) / (n_sum / T + 1))  # 0.4
print("accept_rate@k =",
      [sum(1 for _, a in rounds if a > k) / sum(1 for n, _ in rounds if n > k)
       for k in range(7)])
```

3. 需要观察的现象：分片断言通过；三个指标与手算一致。
4. 预期结果：每卡 4 条样本；`accept_len=4.0`、`verify_rate=0.4`、`#propose=6.50+1`、恒等式返回 0.4；`accept_rate@k` 依次为 `[0.75, 0.75, 0.5, 0.25, 0.25, 0.25, 0.0]`。可把这些数值与 `build_metrics_row` 的公式逐项对照。

**实践 B（主实践，需 GPU）：对已发布 checkpoint 跑 8 条 gsm8k**

1. 实践目标：端到端跑通一次评测，并解读输出表格每一列。
2. 操作步骤：
   - 把 [eval.py:L18-L28](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/eval.py#L18-L28) 的 `TASKS` 临时改为只保留 `("gsm8k", 8)`（评完记得还原，不要提交这次改动）；
   - 参照 [scripts/eval/eval.sh:L7-L14](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/scripts/eval/eval.sh#L7-L14) 的写法，在仓库根目录启动（`draft_name_or_path` 直接用 Hugging Face repo id，README 的 Released Checkpoints 表列出了全部 12 个）：

```bash
# 示例命令（单卡；约需下载 Qwen3-4B 目标模型 + 草稿 checkpoint，显存需容纳两者）
CUDA_VISIBLE_DEVICES=0 python eval.py \
  --target_name_or_path Qwen/Qwen3-4B \
  --draft_name_or_path deepseek-ai/dspark_qwen3_4b_block7
```

3. 需要观察的现象：启动时 rank 0 打印的 args JSON（确认 `tasks` 已变成你改过的列表）；生成过程中无报错；结束时打出一张单行表格。
4. 预期结果：表格列为 `dataset | target_model | draft_model | #propose | accept_len | verify_rate | accept_rate@0..@6`——block7 的 `max_proposal_tokens = block_size = 7`，故槽位列恰好 7 个（`max_positions` 由实际数据决定，最多 7 列）；`target_model` 显示 `Qwen3-4B`、`draft_model` 显示 `dspark_qwen3_4b_block7`（均取 basename）；`num_samples` 不出现在表中但应为 8（可加打印验证）。由于只有 8 条样本、且逐样本种子固定，重跑两次结果应完全一致。具体指标数值待本地验证（与 checkpoint 训练质量相关，8 条样本的波动很大，不能与论文 Table 1 对比）。

#### 4.3.5 小练习与答案

**练习 1**：`run_dataset` 里每条样本都 `seed_all(seed + idx)`，为什么这能让「2 卡评测」和「1 卡评测」得到相同结果？

**答案**：采样种子只依赖样本下标 `idx`，与该样本分到哪个 rank 无关。1 卡时该样本在 rank 0 上以种子 `seed+idx` 生成；2 卡时无论它落在哪张卡，进入 `generate_one_sample` 前都被重置成同一个种子，CUDA/random 的随机序列逐位一致，输出与统计量一致。前提是 bsz=1 且每样本独立前缀计算——框架正是这么写的。

**练习 2**：若某 rank 分到 0 个样本（例如 3 卡评 2 条），会发生什么？会不会死锁？

**答案**：不会死锁。该 rank 的 `responses` 为空列表，`allreduce_response_metrics` 对空列表累积出全零计数器，两次 `dist.all_reduce` 照常参加（集体操作全员到位），只是贡献为零；全局计数与比率仍然正确。`sample_count <= 0` 的守卫只出现在 rank 0 的 `record_dataset_metrics`，且它检查的是归约后的全局值，不会误删有效结果。

**练习 3**：表格里 `accept_len` 明显高于 `verify_rate × (#propose)`，两者矛盾吗？

**答案**：不矛盾，差的是每轮 +1 的兜底 token。精确关系是 \( \text{accept\_len} = \text{verify\_rate} \times (\bar n + 1) + 1 \)（`#propose` 列显示的正是 \( \bar n + 1 \) 中的 \( \bar n \) 加后缀 "+1"）。直觉上：每轮目标模型一次前向看 \( n_t + 1 \) 个 token、提交 \( a_t + 1 \) 个，`verify_rate` 度量「验证过的 token 有多少没白算」，`accept_len` 度量「一次验证换来几个 token」，后者恒含保底的 1。

## 5. 综合实践

设计一个小型「对比评测」实验，把本讲三个模块串起来：

1. **数据契约**：用 `head -n 5` 检查 `gsm8k.jsonl` 与另一个数据集（如 `math500.jsonl`）的 `turns` 格式差异，用 `load_and_process_dataset` 确认两者行数（gsm8k 为 1319，math500 待本地验证）。
2. **配额机制**：不改代码，回答「TASKS 里 `("gsm8k", 500)` 时实际评的是哪 500 条」——用实践 A 的两行代码重放 `random.Random(980406).shuffle` + 截断，打印被选中的原始行号集合。
3. **真机评测**（需 GPU）：把 `TASKS` 改为 `[("gsm8k", 30)]`，分别用 `deepseek-ai/dspark_qwen3_4b_block7` 和 `deepseek-ai/dflash_qwen3_4b_block7` 各跑一次，把两张单行表抄进笔记。
4. **解读**：对比两次评测的 `accept_len`、`verify_rate` 和 `accept_rate@k` 衰减形状，用 u4-l4 的损失设计（DSpark 多了 L1 蒸馏与置信度监督）解释差异方向；再用 `--temperature 0.7` 重跑一次 DSpark，观察温度升高对接受率的影响并尝试解释（提示：拒绝采样在任意温度下都保持目标分布不变，但接受概率 `min(1, p/q)` 的分布形状会变）。
5. 全部完成后把 `TASKS` 还原。30 条样本的绝对数值仅供参考，重点看两个 checkpoint 的**相对**差异是否稳定。

## 6. 本讲小结

- `eval.py` 是薄入口：spawn 每 GPU 一个 worker，凭草稿 checkpoint 的 `config.architectures[0]` 查 `EVALUATORS` 表分发 Evaluator，DFlash 因 architectures 与 DSpark 相同而自动复用同一评估器。
- 评测数据契约极简：`eval_datasets/<name>.jsonl` 每行一个非空字符串列表 `turns`，加载时强制截断到第一轮；assistant 回复由目标模型在投机解码循环中现场生成，`encode_chat_messages` 复用训练侧同一渲染路径保证 prompt 一致。
- 多卡分片是 stride 切分（rank r 取下标 r, r+W, r+2W…），逐样本种子 `seed_all(seed + idx)` 挂在样本下标上，使评测结果与卡数无关、可复现。
- 指标体系全部由逐轮计数 \((n_t, a_t)\) 构成：`accept_len` 是含兜底的平均每轮提交 token 数（加速比近似上界），`verify_rate` 是草稿 token 命中率（满足恒等式 \( \text{verify\_rate}=(\text{accept\_len}-1)/(\bar n+1) \)），`accept_rate@k` 按槽位刻画接受概率衰减。
- 汇总遵循「本地合计数、all_reduce 求和、rank 0 再除」的次序，与训练侧 ratio 指标同一哲学；两次 all_reduce 是集体操作，全员必须到位。
- `BaseEvaluator` 是模板方法基类：`evaluate/run_dataset/allreduce/打表` 全部固化，子类只需填 `build_models`、`max_proposal_tokens`、`generate_one_sample` 三个钩子（DSpark 子类另重写 `evaluate` 插入置信度记录）。

## 7. 下一步学习建议

骨架已通，下一讲 u6-l2《投机解码主循环》将打开本讲最大的黑盒 `generate_decoding_sample`（[deepspec/eval/base_evaluator.py:L307-L441](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/base_evaluator.py#L307-L441)）：prefill 与首 token、`init_context/propose/update` 四个算法钩子的调用时机、KV cache 的 `crop` 维护，以及 `acceptance_lengths` 等三个统计列表是在哪里被 append 的——那正是本讲指标的原始来源。再往后 u6-l3 深挖 `verify_draft_tokens` 的拒绝采样数学。建议阅读顺序：先 u6-l2 再 u6-l3，然后带着对钩子协议的理解去读 u6-l4（DSpark 钩子实现）。
