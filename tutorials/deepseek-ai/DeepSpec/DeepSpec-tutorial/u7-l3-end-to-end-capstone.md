# 毕业实战：从数据到评测的端到端小规模全流程

## 1. 本讲目标

这是整套手册的收官之讲。前面六个单元分别拆解了数据流水线、训练框架、DSpark/Eagle3 建模与评估系统；本讲把它们**串成一次你亲手完成、成本可控的真实运行**。学完后你应当能够：

1. 独立完成「小规模数据准备 → 小目标缓存 → 缩小规模训练 → 评测」的端到端全流程，并理解每一步磁盘上发生了什么。
2. 解释中途 `kill` 进程后，重启如何凭 `step_latest` 符号链接与 `next_micro_step` 无缝续训，以及哪些并行布局不能变。
3. 读懂 `eval.py` 输出的结果表格，用 `accept_len`、`verify_rate`、`accept_rate@k` 判断一个草稿模型的质量。

本讲的原则是：**不改一行源码逻辑，只用官方脚本 + `--opts` 覆盖**，把默认约 38 TB 的目标缓存压到几个 GB、把 10 epoch 的训练压到几十个 optimizer step。

## 2. 前置知识

本讲是综合实战，直接复用前六单元的结论，这里只做最简回顾：

- **三阶段流水线（u1-l2）**：数据准备产出训练 JSONL 与目标缓存（target cache）；训练读缓存产出 checkpoint（`~/checkpoints/<project_name>/<exp_name>/step_*`）；评估读 checkpoint 与 `eval_datasets/*.jsonl` 产出指标。三阶段之间只通过磁盘文件交接。
- **`--opts` 点路径覆盖（u1-l4）**：`--opts "train.lr=3e-4"` 会沿点路径覆盖配置树中**已存在**的键，值经 `yaml.safe_load` 做类型推断；覆盖后运行 `finalize_cfg` 派生目录。
- **目标缓存协议（u2-l4）**：缓存目录 = `manifest.json`（元数据合同）+ `samples.idx`（56 字节定长索引）+ `shard-*.bin`（字节负载）。每样本字节数由序列长度、目标隐宽度、抓取层数决定。
- **训练进度唯一真相源（u3-l2/u3-l5）**：`next_micro_step` 是进度的唯一事实，权重、优化器状态、RNG 都随 checkpoint 落盘；`step_latest` 符号链接原子翻转，指向最新完整目录。
- **评估指标（u6-l1）**：`accept_len` 是每轮平均提交 token 数（含兜底 token），`verify_rate` 是草稿 token 命中率，二者由解码循环中逐轮计数的三个列表汇总而来。

如果以上任何一条你已经模糊，建议先回看对应讲义再动手。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `scripts/data/prepare_data.sh` | 数据准备三步的总包装脚本，记录全部默认参数 |
| `scripts/data/README.md` | 数据流水线官方文档，含 38 TB 存储警告与缩小建议 |
| `scripts/data/prepare_target_cache.py` | 缓存生成脚本：forward hook 抓层、分片写入、finalize 索引 |
| `scripts/train/train.sh` | 训练启动脚本，示范 `--opts` 覆盖 `data.target_cache_path` |
| `scripts/eval/eval.sh` | 评测启动脚本，指定目标模型与 draft checkpoint 路径 |
| `train.py` / `eval.py` | 两个入口：spawn 每 GPU 一个进程 |
| `config/dspark/dspark_qwen3_4b.py` | Qwen3-4B 的 DSpark 配置，本讲所有 `--opts` 的落点 |
| `deepspec/trainer/base_trainer.py` | 训练日程计算、主循环、存盘触发点 |
| `deepspec/trainer/ckpt_manager.py` | `step_latest` 发现/保存/恢复 |
| `deepspec/data/target_cache_dataset.py` | 缓存体积公式、训练前合同校验、collator 过滤 |
| `deepspec/eval/base_evaluator.py` | 数据集加载与指标定义 |
| `deepspec/eval/dspark/evaluator.py` | DSpark 评估器：加载模型、`max_proposal_tokens` |

## 4. 核心概念与源码讲解

本讲的三个最小模块：**小规模数据准备**、**`--opts` 缩小训练规模**、**断点续训与评测解读**。

### 4.1 模块一：小规模数据准备

#### 4.1.1 概念说明

默认流水线面向真实训练：约百万级样本、`max_length=4096`、抓取 5 层目标隐状态，缓存约 38 TB。毕业实战的目标是**验证你理解每个环节**，而不是训出好模型，所以要做三个维度的收缩：

1. **样本数**：几百条足够跑通全链路。
2. **序列长度**：`data.max_length` 从 4096 降到几百，缓存体积随长度线性下降。
3. **抓取层数**：`model.target_layer_ids` 从 5 层减到 2 层，隐状态张量（占体积 ~99.9%）按层数等比缩小。

官方文档明确背书了第 3 个手段：

> Storage warning：……如果存储有限，用更小的训练集和/或在配置里减少 `model.target_layer_ids`（抓的层越少，缓存按比例越小）。见 [scripts/data/README.md:L115-L121](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/scripts/data/README.md#L115-L121)。

另外，标准流水线的第 2 步（用推理引擎重生成答案）需要一个在线服务（u2-l3）。毕业实战可以**临时跳过**：直接把「user 提问 + 原始 assistant 回复」喂给缓存生成脚本，机制上完全合法——但要明白这违背了「答案必须由目标模型重生成」的训练原则（u2-l3 讲过原因），只适合跑通流程的 smoke run。

#### 4.1.2 核心流程

先推导缓存体积公式（u2-l4 的协议结论）。设序列真实长度为 \( L \)、目标隐宽度 \( H \)、抓取层数 \( K \)，则每样本字节数：

\[
\text{bytes} = \underbrace{4L}_{\text{input_ids (int32)}} + \underbrace{L + L}_{\text{两个 uint8 mask}} + \underbrace{2LHK}_{\text{K 层隐状态 (bf16)}} + \underbrace{2LH}_{\text{last\_hidden (bf16)}} = 6L + 2LH(K+1)
\]

代码出处是 `expected_target_cache_tensor_numel` / `expected_target_cache_tensor_nbytes`（`input_ids` 每元素 4 字节、两个 mask 每元素 1 字节、两个 hidden 张量每元素 2 字节）：[deepspec/data/target_cache_dataset.py:L42-L74](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/data/target_cache_dataset.py#L42-L74)。

以 Qwen3-4B（\( H = 2560 \)）、小规模参数 \( L = 512 \)、\( K = 2 \) 为例：

- 每样本：\( 6 \times 512 + 2 \times 512 \times 2560 \times 3 = 3072 + 7864320 \approx 7.5\ \text{MiB} \)
- 512 条样本总计约 **3.75 GiB**——一张普通磁盘即可承载，对比默认 38 TB 缩小了四个数量级。

小规模数据准备的完整流程：

1. 准备一份几百条的 JSONL，每行 `{"id": ..., "conversations": [{"role": "user", ...}, {"role": "assistant", ...}, ...]}`（与 `download_and_split.py` / `generate_train_data.py` 的输出同构）。
2. 单卡运行 `prepare_target_cache.py`，用 `--opts` 同时覆盖 `model.target_layer_ids` 与 `data.max_length`。
3. 检查产出：`manifest.json`、`samples.idx`、`shard-*.bin`，且 manifest 里的 `target_layer_ids`、`max_length` 与你的覆盖一致。

#### 4.1.3 源码精读

**总包装脚本的三步**。[scripts/data/prepare_data.sh:L30-L62](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/scripts/data/prepare_data.sh#L30-L62) 依次执行：第 1 步下载切分（L30-36），第 2 步调用推理服务重生成答案（L40-54），第 3 步生成目标缓存（L56-62）。第 3 步的关键是 `--config` 传入训练配置——缓存生成与训练读的是**同一份配置**，`target_layer_ids`、`chat_template`、`max_length` 全部从配置读取，这就是「一套 `--opts` 同时约束写端和读端」的基础。

**缓存脚本自己的 `--opts`**。`prepare_target_cache.py` 的参数解析与 `train.py` 同构，`--opts` 同样走 `parse_opts_to_config`：[scripts/data/prepare_target_cache.py:L137-L154](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/scripts/data/prepare_target_cache.py#L137-L154)。注意 `--train-data-path` 可重复传参以拼接多个 JSONL（L142-146），`--local-batch-size` 默认 32（小显存可降）。

**输出目录必须为空**。rank 0 在写任何字节前调用 `prepare_target_cache_output_dir`，目录非空立即抛 `FileExistsError`：[deepspec/data/target_cache_dataset.py:L239-L249](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/data/target_cache_dataset.py#L239-L249)。原因是 finalize 阶段要重建**全局** `samples.idx` 与 `manifest.json`（[scripts/data/prepare_target_cache.py:L373-L389](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/scripts/data/prepare_target_cache.py#L373-L389)），混入旧分片会破坏索引一致性；协议不支持增量续写，宁可失败（u2-l5）。**改了覆盖参数重跑时，务必换一个新输出目录或先清空。**

**监督 token 太少的样本会被过滤**。`ConversationCollator` 对每条样本渲染模板得到 `loss_mask`，若 `loss_mask.sum() < min_loss_tokens`（默认 14）该样本被丢弃，整 batch 为空时返回 `None`：[deepspec/data/target_cache_dataset.py:L824-L856](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/data/target_cache_dataset.py#L824-L856)。两个推论：自造数据的 assistant 回复至少要有十几个 token；`max_length` 压得太狠会把长对话的 assistant 段截没，有效样本数缩水（finalize 日志会打印 `valid samples/总样本数`，见 [scripts/data/prepare_target_cache.py:L390-L393](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/scripts/data/prepare_target_cache.py#L390-L393)）。

**层数覆盖的合法性边界**。DSpark 的 `validate_target_layer_ids` 只要求：非空、严格递增、每个层号在 `{-1} ∪ [0, num_layers-1]` 内（-1 表示 embedding 输出）：[deepspec/modeling/dspark/common.py:L59-L75](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/common.py#L59-L75)。所以把 `[1, 9, 17, 25, 33]` 缩成 `[1, 9]` 完全合法（Eagle3 则强制恰好 5 层，这是它俩的差别之一，见 u5-l1）。

#### 4.1.4 代码实践：造一份小数据并生成小缓存

**实践目标**：用几百条自造数据生成一个约 4 GB 的目标缓存，验证体积公式与 manifest 内容。

**操作步骤**：

1. 写一个小脚本生成 512 条对话（示例代码，非仓库原有）：

   ```python
   # make_small_data.py（示例代码）
   import json

   QUESTIONS = [
       "用一句话解释什么是投机解码。",
       "把下面的句子翻译成英文：今天天气很好。",
       # ... 凑够几十个不同的问题模板
   ]
   with open("train_datasets/small_train.jsonl", "w", encoding="utf-8") as f:
       for i in range(512):
           q = QUESTIONS[i % len(QUESTIONS)]
           a = (f"这个问题有多种理解方式。针对第 {i} 号变体，"
                "我们先给出直接回答，再补充两三点解释，"
                "确保回复长度超过最小监督 token 数。")  # 保证 >= 14 个监督 token
           record = {"id": i, "conversations": [
               {"role": "user", "content": q},
               {"role": "assistant", "content": a},
           ]}
           f.write(json.dumps(record, ensure_ascii=False) + "\n")
   ```

   也可以替换为官方路径：跑完 `download_and_split.py` 后取 `train_datasets/perfectblend_train.jsonl` 的前几百行（用 `head -n 512`），其行格式完全一致。

2. 单卡生成小缓存（在仓库根目录执行）：

   ```bash
   CUDA_VISIBLE_DEVICES=0 python scripts/data/prepare_target_cache.py \
       --config config/dspark/dspark_qwen3_4b.py \
       --opts "model.target_layer_ids=[1, 9]" \
       --opts "data.max_length=512" \
       --opts "model.num_anchors=64" \
       --train-data-path train_datasets/small_train.jsonl \
       --output-dir ${HOME}/.cache/deepspec/capstone_cache \
       --local-batch-size 8
   ```

   说明：`model.target_layer_ids=[1, 9]` 的值会被 `yaml.safe_load` 解析成 Python 列表（所以 shell 引号必不可少）；`num_anchors` 降到 64 是因为短序列提供不了 512 个锚点候选——`sample_anchor_positions` 对凑不满的锚点用 `keep_mask` 置为无效（[deepspec/modeling/dspark/common.py:L156-L169](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/common.py#L156-L169)），调小只是省算力，不影响正确性。

3. 先按公式预估体积，再与实际对比：

   ```bash
   python - <<'EOF'
   L, H, K, N = 512, 2560, 2, 512
   per = 6*L + 2*L*H*(K+1)
   print(f"每样本 {per} 字节 ≈ {per/1024**2:.2f} MiB")
   print(f"总计 ≈ {per*N/1024**3:.2f} GiB")
   EOF
   du -sh ${HOME}/.cache/deepspec/capstone_cache
   ```

4. 检查 manifest：

   ```bash
   python -c "import json; m=json.load(open('$HOME/.cache/deepspec/capstone_cache/manifest.json')); print(m['target_layer_ids'], m['max_length'], m['num_samples'])"
   ```

**需要观察的现象**：进度日志按 rank 打印 `processed/total samples`；结束前 rank 0 打印 `Prepared target cache at ... with N/M valid samples`。

**预期结果**：缓存目录含 `manifest.json`、`samples.idx`、若干 `shard-*.bin`（单卡只有一个分片序列）；manifest 中 `target_layer_ids == [1, 9]`、`max_length == 512`；实际磁盘占用与公式预估一致（差异主要来自样本真实长度小于 512）。若第 2 步复用了旧目录会立刻 `FileExistsError`。具体数值**待本地验证**（取决于自造数据的真实长度分布）。

#### 4.1.5 小练习与答案

**练习 1**：若改用 \( L=384 \)、\( H=2560 \)、\( K=2 \)、600 条样本，缓存多大？

**答案**：每样本 \( 6\times384 + 2\times384\times2560\times3 = 2304 + 5898240 = 5900544 \) 字节 ≈ 5.63 MiB；总计 \( 5900544 \times 600 \approx 3.30\ \text{GiB} \)。

**练习 2**：为什么缓存输出目录非空就报错，而不是增量续写？

**答案**：`prepare_target_cache_output_dir`（rank 0 调用）在非空目录上抛 `FileExistsError`。因为收尾阶段要由各 rank 的 summary 重建**全局**索引与 manifest，旧分片会破坏「索引记录 ↔ 分片字节」的对应关系；协议选择「要么完整、要么明确失败」（u2-l5），且脚本无 `--resume` 机制。

**练习 3**：把 `data.max_length` 从 4096 降到 512，除了省磁盘还有什么副作用？

**答案**：体积公式中每一项都线性含 \( L \)，体积约降为 1/8；但长对话会被截断，assistant 段可能被切掉导致 `loss_mask.sum() < min_loss_tokens=14`，样本被 `ConversationCollator` 过滤，有效样本数下降——finalize 日志的 `N/M valid samples` 能直接看到比例。

### 4.2 模块二：用 `--opts` 缩小训练规模

#### 4.2.1 概念说明

训练侧的收缩目标是：**单卡、单 epoch、几十个 optimizer step、频繁存盘**。全部通过 `--opts` 完成，不动配置文件。需要覆盖的键分四组：

| 组 | 键 | 默认值 | 实战值 | 理由 |
| --- | --- | --- | --- | --- |
| 数据 | `data.target_cache_path` | `None` | 小缓存目录 | 训练唯一的数据来源 |
| 模型 | `model.target_layer_ids` | `[1,9,17,25,33]` | `[1,9]` | 必须与缓存一致（合同校验） |
| 模型 | `model.num_anchors` | `512` | `64` | 短序列锚点候选不足 |
| 训练 | `train.global_batch_size` | `512` | `32` | 决定梯度累积与总步数 |
| 训练 | `train.num_train_epochs` | `10` | `1` | 只跑一轮 |
| 日志 | `logging.checkpointing_steps` | `3000` | `4` 或 `8` | 尽早产生 checkpoint 供续训实验 |
| 身份 | `exp_name` | `dspark_block7_qwen3_4b` | 如 `capstone_dspark` | 隔离 checkpoint 目录，避免误续训官方实验 |

注意 `exp_name` 是**顶层**键（[config/dspark/dspark_qwen3_4b.py:L6-L7](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/config/dspark/dspark_qwen3_4b.py#L6-L7)），`finalize_cfg` 在覆盖**之后**运行，用新的 `exp_name` 派生 `checkpoint_dir` 与 `tensorboard_dir`（[config/dspark/dspark_qwen3_4b.py:L60-L68](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/config/dspark/dspark_qwen3_4b.py#L60-L68)），所以 `--opts "exp_name=capstone_dspark"` 会把产物挪到 `~/checkpoints/deepspec/capstone_dspark/`。

#### 4.2.2 核心流程

训练日程是纯函数推导（u3-l1）。设 world_size \( W \)、local batch \( B \)、global batch \( G_b \)、数据集大小 \( N \)：

\[
\text{grad\_acc} = \frac{G_b}{W \times B}, \qquad
\text{samples\_per\_epoch} = \left\lfloor \frac{N}{G_b} \right\rfloor \times G_b
\]

\[
\text{steps\_per\_epoch} = \frac{\lfloor \text{samples\_per\_epoch} / W \rfloor / B}{\text{grad\_acc}}, \qquad
\text{max\_steps} = \text{epochs} \times \text{steps\_per\_epoch}
\]

单卡实战的代入示例：\( W=1 \)、\( B=1 \)（默认 `local_batch_size=1`）、\( G_b=32 \)、\( N=512 \)、epochs=1：

- grad_acc \( = 32 / 1 = 32 \)
- samples_per_epoch \( = \lfloor 512/32 \rfloor \times 32 = 512 \)
- micro_batches_per_epoch \( = 512 \)，steps_per_epoch \( = 512/32 = 16 \)
- 共 16 个 optimizer step、512 个 micro step；`checkpointing_steps=8` 会在 step 8 与结束时的 step 16 各存一次盘。

约束：\( G_b \) 必须整除 \( W \times B \)，且 \( \lfloor N/G_b \rfloor \ge 1 \)，否则断言失败——所以数据只有几百条时 `global_batch_size` **必须**调小。

启动前的完整链路：`train.py` 每个 spawn 子进程各自 `parse_args`（加载配置 → `--opts` 覆盖 → 记录来源）→ `seed_all` → 从配置里取出 `trainer_cls` 直接实例化并 `train()`（[train.py:L20-L38](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/train.py#L20-L38)）。

#### 4.2.3 源码精读

**训练启动脚本**。[scripts/train/train.sh:L25-L40](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/scripts/train/train.sh#L25-L40) 只做一件事：`CUDA_VISIBLE_DEVICES` 圈定 GPU，`train.py` 自己 spawn 每卡一个进程，`--opts` 注入缓存路径。脚本头部的注释（L3-6）强调这不是 torchrun 语义——所以单卡实战只需把 `CUDA_VISIBLE_DEVICES` 改成 `0`，其余全靠追加 `--opts`。

**日程计算的三个纯函数**。整除断言在 `_compute_gradient_accumulation_steps`（[deepspec/trainer/base_trainer.py:L77-L86](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/trainer/base_trainer.py#L77-L86)），「数据太少凑不满一个全局批」的断言在 `_compute_samples_per_epoch`（L89-95），总装在 `_compute_training_schedule`（L98-135）。`BaseTrainer.__init__` 用 `len(self.train_dataset)` 作为 \( N \) 调用它（L197-212），随后 `info_board` 把这些数全部打印出来（L240-249）——**这是核对手算与实际是否一致的官方对账单**。

**训练前合同校验**。`CacheDataset` 建好后立刻 `validate_train_cache`，断言三件事：manifest 的 `target_layer_ids` 等于草稿模型的、`hidden_size` 等于草稿模型的、`target_model_name_or_path` 等于训练配置的：[deepspec/data/target_cache_dataset.py:L203-L218](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/data/target_cache_dataset.py#L203-L218)。若忘了给 `train.py` 传相同的 `model.target_layer_ids` 覆盖，启动即 AssertionError，不会静默错训。

**模型侧的派生一致性**。草稿 config 由 `build_draft_config` 从目标 config 派生，`target_layer_ids` 经校验后写入草稿 config（[deepspec/modeling/dspark/qwen3/config.py:L9-L46](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/qwen3/config.py#L9-L46)）；注意力层把 \( K \times H \) 的缓存特征经 `fc` 投回 \( H \)（u4-l2），所以 \( K=2 \) 时 `fc` 输入维度自动变成 5120——层与维度全由同一份覆盖驱动，无需手工对齐。

**主循环中的两个存盘触发点**。训练循环里，每个同步微批结束后 `if self.global_step % checkpointing_steps == 0` 触发 `save_and_eval_checkpoint`；循环正常结束再无条件存一次：[deepspec/trainer/base_trainer.py:L400-L407](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/trainer/base_trainer.py#L400-L407)。另外公开环境里 `auto_eval_command` 为 `None`，训练中存盘只会打印一行提示而不真的发起评测（L138-153）。

#### 4.2.4 代码实践：跑通小规模训练

**实践目标**：单卡、单 epoch 完成一次 DSpark 训练，`info_board` 数字与手算一致，产出 checkpoint。

**操作步骤**：

1. 启动训练（在仓库根目录）：

   ```bash
   CUDA_VISIBLE_DEVICES=0 python train.py \
       --config config/dspark/dspark_qwen3_4b.py \
       --opts "exp_name=capstone_dspark" \
       --opts "data.target_cache_path=${HOME}/.cache/deepspec/capstone_cache" \
       --opts "model.target_layer_ids=[1, 9]" \
       --opts "model.num_anchors=64" \
       --opts "train.global_batch_size=32" \
       --opts "train.num_train_epochs=1" \
       --opts "logging.logging_steps=2" \
       --opts "logging.checkpointing_steps=8"
   ```

   若训练侧覆盖了 `data.max_length` 请与缓存侧保持一致（manifest 记录了该值，训练读缓存时序列长度以缓存为准，保持一致只是避免困惑）。

2. 观察启动日志中的 `***** Running training *****` 区块，核对：`Train dataset size`、`Gradient accumulation steps = 32`、`Steps per epoch = 16`、`Max train steps = 16`。

3. 训练结束后检查产物：

   ```bash
   ls ~/checkpoints/deepspec/capstone_dspark/
   ls ~/checkpoints/deepspec/capstone_dspark/step_16/
   ```

4. （可选）`tensorboard --logdir ~/tensorboard/deepspec/capstone_dspark` 查看 loss 曲线。

**需要观察的现象**：每个 optimizer step 打印一次 loss/grad_norm/lr（`logging_steps=2` 时每 2 步一次）；step 8 处出现 `Saved checkpoint to .../step_8`，结束时出现 `step_16`；`step_latest` 是指向 `step_16` 的符号链接。

**预期结果**：`step_8` 与 `step_16` 两个目录，各含 HF 格式权重（`config.json`、`model*.safetensors` 等）、`train_config.py` 与 `training_state.rank0.pt`（文件清单的生成者见 4.3.3）。首个 step 因 `torch.compile=True` 会有明显编译耗时。精确步数**待本地验证**（取决于缓存的有效样本数是否恰为 512；若被 collator 过滤掉一些，`steps_per_epoch` 会按 `samples_per_epoch` 相应变化）。

#### 4.2.5 小练习与答案

**练习 1**：若缓存有效样本只有 300 条，其余参数同上（\( W=1, B=1, G_b=32 \), epochs=1），日程是多少？

**答案**：samples_per_epoch \( = \lfloor 300/32 \rfloor \times 32 = 288 \)；micro_batches = 288；steps_per_epoch = 288/32 = 9；共 9 个 optimizer step。注意有 12 条样本永远不被使用（凑整全局批的代价）。

**练习 2**：忘记给 `train.py` 传 `model.target_layer_ids=[1,9]`（其他都传了），会发生什么？

**答案**：草稿模型按默认 5 层构建，`fc` 输入维度为 5×2560；`validate_train_cache` 发现 manifest 的 `[1, 9]` 与草稿模型的 `[1, 9, 17, 25, 33]` 不等，断言失败并在启动时打印两边的值。这是设计好的快速失败，不是 bug。

**练习 3**：为什么 smoke run 要把 `global_batch_size` 从 512 调到 32，而不是维持 512 靠 `num_train_epochs` 补步数？

**答案**：\( N=512 \)、\( G_b=512 \) 时 samples_per_epoch=512、steps_per_epoch=1，10 个 epoch 也只有 10 个 optimizer step，且 `checkpointing_steps` 稍大就永远等不到中途存盘；调小 \( G_b \) 是在固定数据量下换取更多 optimizer step（也更贴近真实训练的有效批动态）的唯一手段。

### 4.3 模块三：断点续训与评测解读

#### 4.3.1 概念说明

**断点续训**回答的问题是：进程死了，训练如何从死点继续？DeepSpec 的答案由三件事合起来（u3-l5）：

1. 存盘只发生在同步微批边界，`step_latest` 符号链接**最后**原子翻转，因此它永远指向完整目录；
2. checkpoint 里除了权重还有 `training_state.rank{r}.pt`，记录 `next_micro_step`、优化器状态、并行布局三元组与四套 RNG 状态；
3. 重启时 `discover_latest_checkpoint` 发现 `step_latest` 即自动续训，数据采样偏移由 `next_micro_step` 推导。

「手动 kill 再重启」就是对这个闭环的实弹检验。

**评测解读**回答的问题是：训出来的草稿模型到底好不好？核心指标有三个（u6-l1/u6-l3）：

- `accept_len`：每轮验证平均提交的 token 数（含兜底 token），是加速比的近似上界；
- `verify_rate`：目标模型每验证一个 token，平均有多少被真正提交，即草稿命中率；
- `accept_rate@k`：块内第 k 个槽位的条件接受概率，刻画接受率沿位置的衰减。

三者都源自解码循环里逐轮追加的三个列表（`acceptance_lengths` 等），按「本地合计、all_reduce 求和、rank 0 再除」汇总。

#### 4.3.2 核心流程

**kill → 恢复的时间线**（以 4.2 的设置为例，共 16 步、step 8 存盘）：

```text
t0: 启动训练，无 step_latest → "Training from scratch."
t1: step 8 完成 → save_checkpoint → 目录 step_8/ → step_latest -> step_8
t2: kill -9 进程组（此刻 step_latest 仍指向 step_8，完整）
t3: 以完全相同的命令重启
    → discover_latest_checkpoint 找到 step_latest → realpath = step_8
    → 权重 from_pretrained 恢复 + 冻结 embed/lm_head
    → load_training_state 恢复优化器/RNG，next_micro_step = 8×32 = 256
    → dataloader 从偏移 256×1 = 256 条样本处续读
    → 打印 "AUTO-RESUME from .../step_8, next_micro_step=256"
t4: 跑完剩余 8 步 → 最终存盘 step_16 → step_latest -> step_16
```

**评测的流程**：`eval.py` spawn 每 GPU 一个 worker → 读 draft checkpoint 的 `config.architectures[0]` 查 `EVALUATORS` 分发 → `BaseEvaluator.evaluate` 逐数据集「加载 → 按 stride 分片逐样本生成 → all_reduce 汇总 → rank 0 打表」。

#### 4.3.3 源码精读

**发现与恢复**。`discover_latest_checkpoint` 只认 `step_latest`（链接或目录皆可），返回 realpath：[deepspec/trainer/ckpt_manager.py:L25-L29](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/trainer/ckpt_manager.py#L25-L29)。`BaseTrainer.__init__` 在构建模型之前就调用它（[deepspec/trainer/base_trainer.py:L163-L165](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/trainer/base_trainer.py#L163-L165)），随后 `load_resume_draft_model` 从 checkpoint 目录 `from_pretrained` 重建权重并重新冻结 embedding/lm_head（[deepspec/trainer/ckpt_manager.py:L64-L81](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/trainer/ckpt_manager.py#L64-L81)）。

**恢复什么、校验什么**。`load_training_state` 依次：装载优化器状态、读 `next_micro_step` 并断言其与 grad_acc 对齐、断言 `global_rank`/`world_size`/`local_batch_size` 与当前并行布局逐项相等（**换卡数或换 local batch 续训会被拒**）、恢复 torch/cuda/numpy/python 四套 RNG，最后打印 `AUTO-RESUME` 提示行（含「想强制重训就改 exp_name 或删 step_latest」）：[deepspec/trainer/ckpt_manager.py:L84-L133](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/trainer/ckpt_manager.py#L84-L133)。

**保存的四阶段全序**。`save_checkpoint` 按 barrier 排序：rank 0 回写 `train_config.py`（原配置拷贝 + 逐条追加 `--opts` 赋值行，见 [deepspec/trainer/ckpt_manager.py:L32-L53](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/trainer/ckpt_manager.py#L32-L53)）→ 全体以 FSDP gathered 形态取权重、rank 0 存成 HF 标准 checkpoint → 各 rank 存 `training_state.rank{r}.pt` → 最后 `safe_symlink` 原子翻转 `step_latest`：[deepspec/trainer/ckpt_manager.py:L136-L185](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/trainer/ckpt_manager.py#L136-L185)。因此即使 kill 打断了写入中的某个 `step_N`，`step_latest` 未翻转就仍指向旧完整目录，残缺目录被忽略。

**恢复后从哪继续读数据**。`train()` 用 `start_offset_samples = next_micro_step × local_batch_size` 构建采样器，只补跑剩余 micro step；若 `global_step >= max_train_steps` 则直接返回：[deepspec/trainer/base_trainer.py:L355-L370](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/trainer/base_trainer.py#L355-L370)。

**评测入口的分发与配额**。`EVALUATORS` 按架构名分发、`TASKS` 规定每个数据集的样本配额：[eval.py:L10-L28](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/eval.py#L10-L28)。DSpark checkpoint 的 `architectures` 写的是 `Qwen3DSparkModel`（由 `build_draft_config` 写入，u4-l2），所以自动命中 `Qwen3DSparkEvaluator`。**毕业实战需要在本地临时把 `TASKS` 改成 `[("gsm8k", 30)]`** 以控制成本——注意 `run_dataset` 的取样方式是「以 `args.seed` 打乱后取前 `max_samples` 条」，不是文件的前 30 行，但 seed 固定所以结果可复现：[deepspec/eval/base_evaluator.py:L519-L527](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/base_evaluator.py#L519-L527)。逐样本生成按 `for idx in range(global_rank, len(dataset), world_size)` 分片，且每个样本前 `seed_all(seed+idx)`，使结果与卡数无关（L530-546）。

**评估器如何加载模型**。DSpark 评估器同时把目标模型与 draft checkpoint 各加载一份到 GPU（均 bf16、sdpa），并断言 `target_layer_ids` 不含目标末层：[deepspec/eval/dspark/evaluator.py:L68-L83](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/dspark/evaluator.py#L68-L83)；`max_proposal_tokens` 直接取草稿模型的 `block_size`（L40-42）。冻结的 embed/lm_head 已随 `save_pretrained` 存进 checkpoint，评估无需再碰目标权重来初始化草稿。

**指标的定义**。`build_metrics_row` 给出三个指标的计算式：`accept_len = acceptance_length_sum / proposal_count`、`verify_rate = acceptance_length_sum / (proposal_length_sum + proposal_count)`、`accept_rate@k = accepted_at_pos[k] / proposals_at_pos[k]`：[deepspec/eval/base_evaluator.py:L469-L511](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/base_evaluator.py#L469-L511)。由于每轮 `acceptance_lengths` 追加的是「接受数 + 1」（兜底 token，见 [deepspec/eval/base_evaluator.py:L421-L425](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/base_evaluator.py#L421-L425)），在未提前停止的轮次有恒等式：

\[
\text{verify\_rate} = \frac{\sum (a_t+1)}{\sum (n_t+1)} = \frac{\text{accept\_len}}{\bar{n} + 1}
\]

即 `verify_rate` = `accept_len` ÷ 表格 `#propose` 列（该列显示为 `n̄.xx+1`）。结果表由 `build_results_table` 渲染，列为 `dataset / target_model / draft_model / #propose / accept_len / verify_rate / accept_rate@0..k`：[deepspec/eval/base_evaluator.py:L115-L164](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/base_evaluator.py#L115-L164)。

**评测启动脚本**。[scripts/eval/eval.sh:L7-L14](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/scripts/eval/eval.sh#L7-L14) 给出两个必填项：`--target_name_or_path` 必须与训练时的目标模型一致（决定 tokenizer 与验证分布），`--draft_name_or_path` 指向 `step_latest` 或具体 `step_N`。

#### 4.3.4 代码实践：kill 续训 + gsm8k 小规模评测

**实践目标**：亲历「kill -9 → 重启自动续训」，并在 gsm8k 的 30 条样本上评测自己的 checkpoint、解读表格。

**操作步骤**：

1. **kill 实验**。用 4.2 的命令启动训练（可把 `checkpointing_steps` 再调小到 4）。开另一个终端轮询：

   ```bash
   until ls ~/checkpoints/deepspec/capstone_dspark/step_latest >/dev/null 2>&1; do sleep 5; done
   ls -l ~/checkpoints/deepspec/capstone_dspark/   # 确认 step_latest 与至少一个 step_* 目录
   pkill -9 -f "train.py --config config/dspark"   # 模拟进程被杀
   ```

2. **重启**。执行与 4.2 完全相同的训练命令（尤其相同的 `CUDA_VISIBLE_DEVICES` 数量、`train.local_batch_size`、`exp_name`）。观察日志中的 `AUTO-RESUME from .../step_4, next_micro_step=...` 与 `Resumed from ...: next_micro_step=..., global_step=..., epoch=...` 两行，确认从断点继续并跑完剩余步数。

3. **对照实验（可选）**：删掉 `step_latest`（或换 `exp_name`）再启动，应看到 `Training from scratch.`。

4. **评测**。先在本地把 [eval.py:L18-L28](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/eval.py#L18-L28) 的 `TASKS` 临时改为 `TASKS = [("gsm8k", 30)]`，然后：

   ```bash
   CUDA_VISIBLE_DEVICES=0 python eval.py \
       --target_name_or_path Qwen/Qwen3-4B \
       --draft_name_or_path ${HOME}/checkpoints/deepspec/capstone_dspark/step_latest \
       --max-new-tokens 512
   ```

   （`--max-new-tokens` 从默认 2048 调小以省时间；`--temperature` 默认 1.0。）

5. 记录结果表格中 gsm8k 一行的 `#propose`、`accept_len`、`verify_rate`、各 `accept_rate@k`。

**需要观察的现象**：重启日志出现 `AUTO-RESUME`；评测先打印完整参数 JSON，随后逐数据集打印一行无表头结果，最后打印汇总表；`accept_rate@k` 沿 k 大致单调下降。

**预期结果**：断点续训后最终仍产出 `step_16` 且 `step_latest -> step_16`；评测表能完整输出。**指标量级待本地验证**——只训 16 步、几百条自造数据的草稿模型几乎没有被「教会」模仿目标分布，`accept_len` 可能只略高于 1.0、`verify_rate` 接近裸底采样水平；官方 `deepseek-ai/dspark_qwen3_4b_block7` 的参考值请对照 README 的 Released Checkpoints 表格。这正是本实践的教学点：**指标好坏反映的是训练充分程度，而链路正确性由「评测能跑完且输出无损」来验证**（拒绝采样保证输出分布与目标一致，u6-l3）。

#### 4.3.5 小练习与答案

**练习 1**：kill 发生在第一个 checkpoint 落盘之前，重启会怎样？

**答案**：`step_latest` 不存在，`discover_latest_checkpoint` 返回 `None`，走 `Training from scratch.` 分支，从 step 0 重训。已写入的半成品 `step_*` 目录若未翻转链接则被永久忽略（可手工删除）。

**练习 2**：kill 前用 1 卡训练，重启时改成 2 卡（其余不变），会发生什么？

**答案**：`load_training_state` 中 `assert saved_world_size == world_size` 失败——并行布局（world_size、local_batch_size、global_rank）是断点续训合同的一部分，因为采样偏移按 `next_micro_step × local_batch_size` 换算且各 rank 样本分配依赖布局（u3-l3）。想换布局只能从 step_latest 之前某个 checkpoint 以新布局重开。

**练习 3**：某评测结果 `#propose = 6.5+1`、`accept_len = 2.4`，`verify_rate` 应是多少？这组数说明什么？

**答案**：`verify_rate = 2.4 / 7.5 = 0.32`。含义：目标模型每验证 7.5 个 token 平均只有 2.4 个被提交，草稿命中率偏低；同时 `accept_len = 2.4 > 1` 意味着相对纯目标解码仍有约 2.4 倍的每轮产出（加速上界），提升空间在让草稿分布更贴近目标（u4-l4 的 L1 蒸馏正是在直接优化 `1 − verify_rate` 这一量）。

## 5. 综合实践

把 4.1–4.3 串成一次完整交付。任务：**在单卡、约 4 GB 磁盘、一小时内，跑通 DeepSpec 全链路并提交一份实验报告**。

执行清单（每项的细节见对应小节的实践步骤）：

1. **数据**（4.1.4）：生成 512 条 JSONL → 单卡生成小缓存（`target_layer_ids=[1,9]`、`max_length=512`、`num_anchors=64`）→ 用体积公式核对 `du -sh`，检查 manifest。
2. **训练**（4.2.4）：`--opts` 覆盖 7 个键启动训练 → 用 `info_board` 核对手算日程 → 得到 `step_8`/`step_16`。
3. **容错**（4.3.4 步骤 1-3）：确认 `step_latest` 出现后 `kill -9`，原命令重启，记录 `AUTO-RESUME` 行中的 `next_micro_step`，验证续训闭环。
4. **评测**（4.3.4 步骤 4-5）：`TASKS` 临时改为 `[("gsm8k", 30)]`，对自己的 `step_latest` 评测，抄录结果表。
5. **对照**（可选）：用相同命令评测官方 checkpoint（如 `deepseek-ai/dspark_qwen3_4b_block7`，需先下载），与自己的 16 步模型对比。

报告模板（填写并保存到你的笔记）：

```text
- 环境：GPU 型号/数量、磁盘占用（实测 vs 公式）
- 数据：原始条数 / 缓存有效条数（finalize 日志的 N/M）、每样本字节数
- 训练日程：N、W、B、G_b → grad_acc、samples_per_epoch、steps_per_epoch（手算 vs info_board）
- 续训：kill 时的 step、重启后日志的 next_micro_step、恢复的 RNG/优化器证据
- 评测：gsm8k 30 条的 #propose / accept_len / verify_rate / accept_rate@0..6
- 结论：链路是否全程无损跑通；指标差异的原因分析（训练步数 vs 数据量 vs 覆盖参数）
```

通过标准：无需回看讲义就能解释报告中每一个数字的来源。

## 6. 本讲小结

- 小规模化的三根杠杆是**样本数、`data.max_length`、`model.target_layer_ids`**；每样本缓存体积 \( 6L + 2LH(K+1) \) 字节，全部由协议常量决定，可在动手前精确预估。
- 缓存写端与训练读端靠 `validate_train_cache` 的三项合同（层号、隐宽度、目标模型名）锁定一致；`--opts` 覆盖必须**两边传同一份**，否则启动即断言失败。
- 训练日程是纯函数：grad_acc = \( G_b/(W \times B) \)，samples_per_epoch 向下取整到全局批整数倍；`info_board` 是核对手算的官方对账单。
- 断点续训闭环 = 同步边界存盘 + `step_latest` 原子翻转 + `next_micro_step` 单一真相源 + 并行布局断言；kill 实验中残缺的 `step_*` 目录因链接未翻转而被安全忽略。
- 评测指标全部来自解码循环的逐轮计数：`accept_len` 是加速比近似上界（含兜底 token），`verify_rate = accept_len / (#propose + 1)` 是草稿命中率，`accept_rate@k` 刻画块内衰减；拒绝采样保证输出分布无损，所以指标只影响速度、不影响正确性。
- 端到端跑通的价值在于**把 24 篇讲义的知识点变成一张可核对的清单**：每个数字都能回溯到一行源码。

## 7. 下一步学习建议

至此整套手册完毕。推荐三个继续深入的方向：

1. **把 smoke run 升级为真实小实验**：用 `download_and_split.py` 的真实切分 + SGLang 重生成答案（u2-l3），固定 1-2 万条样本重跑本讲流程，观察 `accept_len` 随训练步数的增长曲线（用 `eval.py --tensorboard-dir --step` 把评测点写进 TensorBoard，见 [deepspec/eval/base_evaluator.py:L632-L664](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/base_evaluator.py#L632-L664)）。
2. **横向对比三种算法**：以相同数据和日程分别训练 `dspark_qwen3_4b`、`dflash_qwen3_4b`、`eagle3_qwen3_4b`（u5-l3 的对比表），实测 accept_len/verify_rate 差异，验证讲义中的理论分析。
3. **接一个新目标模型族**：按 u7-l1 的接入清单，为一个新模型族写 modeling/config/注册代码，并用本讲的端到端流程作为验收标准——能跑完「数据→训练→评测」全链路，接入才算完成。
