# 精炼输入构建：prepare_proof_refinement 的数据汇合

## 1. 本讲目标

u5-l1 讲了证明池这个「账本」怎么记账，u5-l2 讲了单题如何从候选证明里采样组合、拼装精炼提示词。但这两讲的主角 `_prepare_proof_agg_tasks` 并不是凭空拿到数据的——它吃的每个四元组任务，都由外层函数 `prepare_proof_refinement`（[inference/main.py:L286-L395](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L286-L395)）从两份**扁平的 jsonl 输出**里一点点整理出来。本讲补全这块拼图：整条流水线里规模最大的一次「数据汇合」。

> 用一句话概括本讲主角：它把上一轮「验证」与「元验证」两路输出重新组织成「问题 → 证明 → 评价」的层级结构，交给进程池并行聚合，产出下一轮的精炼输入 `proof_gen_R{R}/input.jsonl`。

学完本讲，你应该能够：

1. 解释 `rating2quality` 字典如何以**评价全文**为字符级主键，把元验证的质量分 join 回每条验证评价上；
2. 描述 `problem2proof2ratings` 等四个嵌套字典如何把 generate.py 复制 \( n \) 份后的扁平行，还原成「问题 → 证明 → 评价」的层级索引，并指出各字典的填充时机与「首见优先」规则；
3. 说明 `multiprocessing.Pool` 配合 `functools.partial` 与 `_split_jobs`（切成约 50 块）并行调用 `_prepare_proof_agg_tasks` 的结构，以及 `pool.imap` 的有序性；
4. 说出主循环调用点的实际参数取值（`round_idx=R-1`、`use_old_proofs_for_refinement=True`）与「收尾轮」的真实效果；
5. 回答 u5-l2 留下的悬念：被元验证判过质量分的评价，到底有没有被精炼摘要消费？

## 2. 前置知识

### 2.1 承接前几讲：进入本函数时数据长什么样

| 来源 | 前置讲义 | 进入本讲时的形态 |
|---|---|---|
| `proof_verification_R{R-1}/output.jsonl` | u4-l2、u2-l2 | 每行 = 对某条证明的一次验证。字段含 `question`、`proof`（纯 Solution）、`output`（验证器输出，含 `</think>`）、`finish_reason`、`self_eval`、`self_eval_score`、`problem_idx`、`source_name`。**同一 (题, 证明) 有 `n_verification_per_proof` 行**（generate.py 在数据层把每行复制 n 份） |
| `meta_verification_R{R-1}/output.jsonl` | u4-l3 | 每行 = 对某条低分评价的一次元验证。关键字段 `rating`（被复核的验证批语原文，由 `prepare_meta_verification` 逐字存入）、`output`（元验证器的质量分析）、`finish_reason` |
| 证明池目录 `{proof_pool_dirname}/` | u5-l1 | 一题一文件的追加账本，是本讲函数的下游写入目标之一 |
| 四元组任务与聚合算法 | u5-l1、u5-l2 | `(item, proof2ratings, proof2self_eval, proof2dep_proof_ids)` 与组合采样——本讲只讲它们**从哪来**，不再重推算法 |

一个贯穿本讲的计数直觉：验证输出是「扁平行 × n 份复制」，而下游一切统计量（均分 `meanscore`、分数桶 `score2ratings`）都需要**把同一证明的所有评价聚在一起**才能算。这就是「汇合」存在的根本理由。

### 2.2 关键术语

| 术语 | 含义 |
|---|---|
| 汇合（confluence） | 把多路中间输出按语义主键重新对齐、还原层级结构的加工过程，类似数据库的 join + group by |
| join 主键 | 两份数据能对上号的字段。本函数中元验证侧与验证侧共享的主键是**评价全文文本** |
| 左表 / 右表 | 借用 SQL 术语：`rating2quality`（元验证侧）是待 join 的查找表，验证输出是逐行查它的主表 |
| 层级索引 | `问题 → 证明 → 评价` 三层嵌套字典，外键是题面文本、内键是证明文本 |
| 首见优先 | 某键第一次出现时初始化的附属信息（自评、谱系、题目记录），后续重复行不再覆盖 |
| 收尾轮 | 主循环 `range(start_round, max_rounds + 2)` 多出来的最后一轮，只准备精炼输入便 `break`，不再生成 |
| `partial` | `functools.partial` 把多参数函数的部分参数固化成单参数函数，配合 `pool.imap` 使用 |

### 2.3 为什么「汇合」必须存在

设想你是下一轮精炼的调度者，手里有两份文件：一份是几百 MB 的验证输出（每行一条批语），一份是小得多的元验证输出（每行一条质量复核）。要回答「第 3 题的第二条证明平均得分多少、验证器都批评了它什么、哪些批评本身被元验证器认为不靠谱」，你必须先回答「哪些行属于第 3 题」「哪些行属于同一条证明」。**扁平文件回答不了这些问题，层级索引才能**。`prepare_proof_refinement` 干的就是这件事，外加把元验证的质量信息按主键贴回对应的批语上——一次典型的 ETL（Extract-Transform-Load）。

## 3. 本讲源码地图

| 文件与段落 | 作用 |
|---|---|
| [inference/main.py:L286-L295](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L286-L295) | 函数签名与四个嵌套字典的声明 | 
| [inference/main.py:L297-L317](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L297-L317) | 从元验证输出建立 `rating2quality`（join 的左表） |
| [inference/main.py:L319-L353](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L319-L353) | 逐行读验证输出，填充四个嵌套字典（含 join 消费点） |
| [inference/main.py:L355-L369](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L355-L369) | 按题打包成 tasks 四元组，`problem_idx` 全局唯一断言 |
| [inference/main.py:L371-L395](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L371-L395) | `partial` 冻结参数、`Pool.imap` 并行消费、结果落盘 |
| [inference/main.py:L430-L446](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L430-L446) | `__main__` 中的调用点：实际生效的参数值与收尾轮 |
| [inference/main.py:L156-L163](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L156-L163) | `_split_jobs`：任务切块（u5-l2 4.4 已精读，本讲引用） |
| [inference/main.py:L165-L284](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L165-L284) | `_prepare_proof_agg_tasks`：被并行调用的下游（u5-l1、u5-l2 已精读，本讲只讲接口契约） |
| [inference/main.py:L118-L154](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L118-L154) | `prepare_meta_verification`：join 主键 `rating` 字段的出生地（u4-l3 已精读） |
| [inference/utils.py:L5-L6](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/utils.py#L5-L6)、[L19-L34](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/utils.py#L19-L34) | `hash_problem_idx` 与 `extract_boxed_answers`：主键兜底与分数解析 |
| [inference/run.sh](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/run.sh) | 官方配置：`proof_pool_dirname` 与 `--skip_meta_verification` | 

## 4. 核心概念与源码讲解

### 4.1 总览与主循环接线：三路输入、一路输出

#### 4.1.1 概念说明

把 `prepare_proof_refinement` 想成一座立交桥：两条匝道进（验证输出、元验证输出），一条主道出（下一轮精炼输入），桥下还有一条侧路通向证明池。它自己**从不调用 API**（和 main.py 其余部分一样，真正发请求的只有 generate.py），也几乎不做「决策」——排序、选组合、写证明池这些智能活全部外包给 `_prepare_proof_agg_tasks`。它负责的是**搬运与对齐**：把扁平文件变成下游能消化的层级结构。

函数签名（[inference/main.py:L286-L289](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L286-L289)）共 11 个参数，分三组：

- **路径三件套**：`path`（上一轮验证输出）、`meta_verification_path`（上一轮元验证输出）、`tar_path`（本轮精炼输入的落盘位置）；
- **账本参数**：`round_idx`（写进证明池的轮次标记）、`proof_pool_dirname`、`use_old_proofs_for_refinement`；
- **聚合参数**：`num_trials`、`n_best_proofs_to_sample`、`n_proofs_to_refine`、`max_rating_per_score`、`drop_thought`——后四个直接透传给 u5-l2 的采样算法。

#### 4.1.2 核心流程

```text
proof_verification_R{R-1}/output.jsonl ──┐
  （扁平：每行一次验证，同一证明有 n 行）  │  ① 建左表 rating2quality（4.2）
                                          │  ② 四个嵌套字典聚合 + join（4.3）
meta_verification_R{R-1}/output.jsonl ───┘  ③ 打包 tasks + 唯一性断言（4.3）
  （扁平：每行一次元验证，可整份缺席）              │
                                                   ▼  ④ Pool.imap 并行（4.4）
                                    _prepare_proof_agg_tasks × 约 50 块
                                      （u5-l1 记账 / u5-l2 采样）
                                                   │  副作用：追加写证明池
                                                   ▼
                                    proof_gen_R{R}/input.jsonl
                                    （随后由 generate.py 以 --n 复制采样）
```

主循环侧的接线在 [inference/main.py:L430-L446](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L430-L446)：轮次 R（R ≥ 2）发现 `proof_gen_R{R}/input.jsonl` 不存在时，用上一轮两个阶段的输出调用本函数。注意 `round_idx=R - 1`——被汇合的证明**产生于**第 R-1 轮（`proof_gen_R{R-1}`），这个值最终写进池记录的 `round_idx` 字段。

#### 4.1.3 源码精读

调用点与收尾轮（[inference/main.py:L430-L446](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L430-L446)）：

```python
previous_proof_verification_output_path = f"{output_dirname}/proof_verification_R{R - 1}/output.jsonl"
previous_meta_verification_output_path = f"{output_dirname}/meta_verification_R{R - 1}/output.jsonl"
prepare_proof_refinement(
    path=previous_proof_verification_output_path,
    meta_verification_path=previous_meta_verification_output_path,
    tar_path=proof_gen_input_path,
    round_idx=R - 1,
    proof_pool_dirname=proof_pool_dirname,
    use_old_proofs_for_refinement=True,
    num_trials=args.n_agg_trials,
    n_best_proofs_to_sample=args.n_best_proofs_to_sample,
    n_proofs_to_refine=args.n_proofs_to_refine,
    max_rating_per_score=4,
    drop_thought=True
)
if R == args.max_rounds + 1:
    break
```

三个容易被忽略的事实：

1. **函数签名默认值从不生效**（u5-l2 已提醒过）：主循环总是显式传参。实际取值对照表如下——注意 `use_old_proofs_for_refinement=True` 与 `max_rating_per_score=4` 是**硬编码**，命令行改不了：

| 参数 | 签名默认值（[L288](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L288)） | 实际传入 | argparse 默认 / run.sh |
|---|---|---|---|
| `use_old_proofs_for_refinement` | `False` | `True`（硬编码） | — |
| `num_trials` | `16` | `args.n_agg_trials` | 32 / 32 |
| `n_best_proofs_to_sample` | `6` | `args.n_best_proofs_to_sample` | 32 / 32 |
| `n_proofs_to_refine` | `4` | `args.n_proofs_to_refine` | 1 / 1 |
| `max_rating_per_score` | `4` | `4`（硬编码） | — |

2. **收尾轮的真实效果**：主循环跑 `range(start_round, max_rounds + 2)`，最后一轮 `R = max_rounds + 1` 走到本函数后立即 `break`（[L445-L446](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L445-L446)）。这轮调用产出的 `proof_gen_R{R}/input.jsonl` 永远不会被 generate.py 消费——从代码行为看，它唯一的实际作用是把**最后一轮生成的证明与评分刷进证明池**（池写入发生在 `_prepare_proof_agg_tasks` 内部），保证账本完整。没有这次调用，最后一轮的证明就永远不会入池。

3. **下游的消费方式**：本函数返回精炼请求条数（[L395](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L395)），但 `__main__` 并不接收返回值；generate.py 随后以 `--n = n_parallel_proof_gen // n_agg_trials` 把每条请求复制采样（[L448](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L448)，即 u5-l2 的算力守恒式）。

#### 4.1.4 代码实践

**实践目标**：不运行任何东西，靠读 [inference/main.py:L398-L446](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L398-L446)，推演出 `--max_rounds 2` 时本函数的完整调用序列。

**操作步骤**：

1. 找到循环 `for R in range(args.start_round, args.max_rounds + 2)`，代入 `start_round=1, max_rounds=2`，列出 R 的全部取值；
2. 对每个 R 判断：`proof_gen_R{R}/input.jsonl` 由哪个分支产生（R=1 走原始输入初始化，R≥2 走本函数）？`round_idx` 是多少？循环是否 `break`？
3. 填写下表（先自己填，再对照答案）。

**需要观察的现象**：本函数总共被调用几次，每次的 `round_idx` 与 `tar_path`。

**预期结果**：

| R | proof_gen_R{R}/input.jsonl 的来源 | round_idx | 之后发生什么 |
|---|---|---|---|
| 1 | 原始输入 + `proof_generation` 模板渲染（L402-L428） | — | 生成 → 验证 → 元验证（如未跳过） |
| 2 | 本函数（读 R1 的验证/元验证输出） | 1 | 正常生成 → 验证 → 元验证 |
| 3 | 本函数（读 R2 的验证/元验证输出） | 2 | `R == max_rounds + 1` 成立，立即 `break`——只刷新证明池 |

因此 `max_rounds=2` 时本函数被调用 **2 次**（R=2 与 R=3）。同理 run.sh 的 `max_rounds=16` 对应 **16 次**（R=2..17）。以上为静态推演，待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `round_idx` 传 `R - 1` 而不是 `R`？

**答案**：调用发生在第 R 轮**开始**时，被汇合的证明生成于第 R-1 轮（`proof_gen_R{R-1}` 的产物，经 `proof_verification_R{R-1}` 评分）。池记录的 `round_idx` 语义是「这条证明产生于哪一轮」（u5-l1 的字段表），所以必须写 R-1。它也恰好等于被读取的两个输出目录名里的轮次编号，便于对账。

**练习 2**：run.sh 开启了 `--skip_meta_verification`（[inference/run.sh](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/run.sh)），此时 `meta_verification_R{R-1}/output.jsonl` 根本不存在，本函数会崩溃吗？

**答案**：不会。[L298](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L298) 用 `os.path.exists` 先检查文件是否存在，不存在则整段跳过，`rating2quality` 保持空字典 `{}`，后续 join 全部得到空列表。官方配置下这套元验证机制整体处于「休眠」状态——u4-l3 说过的「代码支持但默认关闭」在这里落到了实处。

**练习 3**：如果第 R-1 轮的验证输出文件被误删，重跑 main.py 会发生什么？

**答案**：`os.path.exists(proof_gen_input_path)` 为 False 会触发本函数，函数内 `open(path)` 直接抛 `FileNotFoundError`（没有存在性检查，也不在 argparse 层兜底）。这符合 u4-l1 讲过的断点续跑设计前提：中间文件链必须完整，缺一环即硬失败。主进程里其他阶段都有「文件存在即跳过」的宽容，这里是对上游的强依赖。

### 4.2 rating2quality：以评价全文为主键的元验证 join

#### 4.2.1 概念说明

u4-l3 讲过：只有低分（≤ 0.75 档）的验证评价会被送去元验证复核，元验证输出里带着一个 `rating` 字段——被复核的那条批语原文。现在的问题是：**怎么把元验证的结论贴回原来那条批语上？**

数据模型里没有「评价 ID」。一条验证评价在两份文件中的唯一公共标识，就是它的**全文文本**。于是代码用了一个字符级 join：以批语文本为字典键，元验证结论为值。这好比用「信的全文」而不是编号去认领一封信——只要两边逐字符一致就能对上，差一个空格就永远对不上。

左表（查找表）的构造规则（[inference/main.py:L297-L317](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L297-L317)）：

- 键：`item['rating'].strip()`——元验证输出行里的 `rating` 字段；
- 值：一个**列表**，元素是 `{'quality': 质量分析文本, 'score': 质量分}`。列表而非单值，是因为 `n_meta_verification_per_rating` 可以大于 1（同一批语被元验证多次）；
- 准入闸门与 u4-l3 一脉相承：`finish_reason == 'stop'` 且输出含 `</think>`，boxed 分数解析失败则静默跳过该行。

#### 4.2.2 核心流程

```text
rating2quality = {}
若 meta_verification_path 存在:
    for 每行 in 元验证输出:
        rating = 行['rating'].strip()                     # join 键（左表侧）
        若 finish_reason != 'stop' 或 output 无 </think>: 跳过   # 截断样本丢弃
        quality = output 去思维链后取正文
        score  = quality 最后一个非空 boxed 值转 float      # 失败则跳过
        rating2quality[rating].append({quality, score})    # 同键可累积多条
```

为什么两侧的键能对上？因为**同一个字符串经过同一条加工流水线**。回看 u4-l3 的 `prepare_meta_verification`（[inference/main.py:L124-L145](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L124-L145)）：它从验证输出行的 `output` 字段出发，先 `.strip()`，再按 `</think>` 切掉思维链、再 `.strip()`，把结果存成 `rating` 字段。而本函数在验证侧（L327-L329）对**同一个来源文本**做**完全相同的三步加工**得到 join 键。同源、同流水线，故逐字符一致。

代价是极端脆弱：任何一侧多一个尾随空格、换行符差异、或 `drop_thought` 处理不对称，join 就静默 miss——`rating2quality.get(rating, [])` 返回空列表，**没有警告、没有日志**，质量信息无声丢失。JSON 序列化本身不会破坏字符串，所以正常流程下是安全的；风险来自有人改动其中一侧的加工逻辑。

#### 4.2.3 源码精读

左表构造（[inference/main.py:L297-L317](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L297-L317)）：

```python
rating2quality = {}
if os.path.exists(meta_verification_path):
    with open(meta_verification_path, "r") as file:
        for line in tqdm(file, desc='reading meta verification outputs'):
            item = orjson.loads(line)
            rating = item['rating'].strip()
            if item['finish_reason'] == 'stop' and '</think>' in item['output']:
                quality = item['output'].strip()
                if drop_thought and '</think>' in quality:
                    quality = quality.split("</think>")[-1].strip()
                scores = [s.strip() for s in extract_boxed_answers(quality) if s.strip()]
                try:
                    score = float(scores[-1])
                except:
                    continue
                if rating not in rating2quality:
                    rating2quality[rating] = []
                rating2quality[rating].append({
                    'quality': quality,
                    'score': score
                })
```

四个细节：**orjson**（L301）是高速 JSON 解析库，热循环里逐行解析大文件比标准库快得多（写盘仍用标准 `json.dumps`，见 L394）；**boxed 解析**复用 u3-l2 的 `extract_boxed_answers`，取最后一个非空值转 float；**截断的元验证输出直接丢弃**（`continue`），对应批语的质量信息就此丢失（u4-l3 已指出这一点）；**同键累积**使得 `n_meta_verification_per_rating > 1` 时一个批语对应多条质量评价，但代码只存不聚（没有取均值）。

join 的消费点在验证输出聚合循环里（[inference/main.py:L347-L351](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L347-L351)）：

```python
problem2proof2ratings[problem][prover_output].append({
    'rating': rating,
    'quality': rating2quality.get(rating, []),
    'score': score
})
```

`get(rating, [])` 的默认值就是「miss 即空」。quality 从此**骑在评价条目上**，一路进入 `score2ratings` 桶、随证明池记录落盘（u5-l1 的池记录字段表里 `score2ratings` 的每个条目都带它）。

**这就到了回答 u5-l2 悬念的时刻**：精炼摘要的拼装循环（[inference/main.py:L253-L262](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L253-L262)）只读取 `rating['rating']`——quality 字段被持久化了，却**没有进入任何提示词**。所以哪怕元验证判明某条批评不合理，精炼模型照样会看到这条批评原文。这是「存而不用」的伏笔：数据通路已修好，消费逻辑尚未接上（u6-l1 会从测试时算力的角度再讨论它）。

#### 4.2.4 代码实践

**实践目标**：亲手体验字符级主键 join 的命中与失配。

**操作步骤**（示例代码，纯 Python 可独立运行）：

```python
# join_demo.py —— 复刻 L297-L317 的左表构造与 L349 的查找
rating2quality = {}

def learn(rating_field, meta_output):
    """模拟一行元验证输出进入左表"""
    rating = rating_field.strip()                      # L302
    if '</think>' not in meta_output:                  # L303 闸门
        return
    quality = meta_output.split("</think>")[-1].strip()  # L304-L306
    import re
    boxed = re.findall(r'boxed\{([^{}]*)\}', quality)  # 简化版分数解析
    if not boxed:
        return                                         # L308-L311 解析失败跳过
    rating2quality.setdefault(rating, []).append(
        {'quality': quality, 'score': float(boxed[-1])})

R_VER = "Step 1 is wrong.\n\\boxed{0}"     # 验证侧经 strip+drop_thought 得到的批语

learn(R_VER,      "<think>x</think>Defect analysis half reasonable.\n\\boxed{0.5}")  # 命中键
learn(R_VER,      "<think>y</think>Second meta check.\n\\boxed{1}")                  # 同键再累积
learn(R_VER + " ", "<think>z</think>Trailing-space variant.\n\\boxed{0}")            # 多一个空格的键

print("命中条数:", len(rating2quality.get(R_VER, [])))          # 验证侧的主键查找
print("全部键:", list(rating2quality.keys()))
```

**需要观察的现象**：`rating2quality` 里有两个键（`R_VER` 与 `R_VER + " "`）；验证侧用 `R_VER` 查找只能拿到前两条，第三条成了永远无人认领的孤儿数据。

**预期结果**：输出 `命中条数: 2`；`全部键` 显示两个仅差一个尾随空格的字符串。把第三行的 `R_VER + " "` 改成 `R_VER`，命中条数变 3——这就是「同键累积、异键失配」的全部行为。待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：为什么不用 `problem_idx + proof_id + 第几条评价` 这样的组合键，而用评价全文？

**答案**：因为 join 发生的时刻，`proof_id` 还不存在——它是稍后在 `_prepare_proof_agg_tasks` 里才分配的（u5-l1），而且元验证输出行里根本不带 `proof_id`。数据模型里两份文件唯一的公共标识就是批语文本本身（元验证输入行的 `rating` 字段就是从验证输出逐字拷贝的）。想用结构化 ID，就得改造上下游的数据契约，发布代码选择了最省事的字符级匹配。

**练习 2**：`n_meta_verification_per_rating=2` 时，`rating2quality[rating]` 里是什么？下游怎么用它？

**答案**：同一批语的两条元验证结果，各成一个 `{'quality', 'score'}` 元素，共 2 个。下游只是原样携带（进入评价条目的 `quality` 字段落盘），既不取均值也不投票——即便两条元验证结论矛盾（0 与 1），也不会有任何调和逻辑。这是「存而不用」的又一体现。

**练习 3**：如果给 `prepare_meta_verification` 的模板输出规则加了一个前缀（比如让元验证器先复述一遍批语再分析），join 还能命中吗？

**答案**：能。join 键是元验证输出行里的 `rating` **字段**（被复核批语的原文），不是元验证器的 `output`——改 `output` 的格式只影响 quality 文本本身，不影响键。真正会打断 join 的是改动 `rating` 字段的加工（例如某侧多做了 `strip()` 之外的清洗），或者验证侧的 `output` 在两次运行间被重新生成导致文本漂移。

### 4.3 四个嵌套字典：从扁平行还原「问题 → 证明 → 评价」

#### 4.3.1 概念说明

这是本函数的主体：一次单遍扫描，把验证输出的每一行归位到四个同步维护的层级索引里（[inference/main.py:L292-L295](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L292-L295)）。注意两个键的选型：

- **外键是题面文本**（`item['question'].strip()`），不是 `problem_idx`；
- **内键是证明文本**（`item['proof'].strip()`，必要时先剥 `</think>`）。

| 字典 | 结构 | 填充时机 | 下游用途 |
|---|---|---|---|
| `problem2item` | `{题面 → 题目记录副本}` | 每题**首行**初始化一次 | `deepcopy` 成每个精炼样本的底板（u5-l2 的 L275） |
| `problem2proof2ratings` | `{题面 → {证明 → [评价条目]}}` | 每条过闸门的行 append | 算 `meanscore`、`score2ratings`（u5-l1） |
| `problem2proof2self_eval` | `{题面 → {证明 → 自评 dict}}` | 每个证明**首次出现** | 排序次键 `self_eval_score`（u5-l2） |
| `problem2proof2dep_proof_ids` | `{题面 → {证明 → 谱系列表}}` | 每个证明**首次出现** | 入池谱系 `dep_proof_ids`（u5-l1） |

三个后两个字典「首见优先」：同一证明的第 2..n 行只往 ratings 列表追加，自评与谱系不再覆盖。由于同一证明的 n 份复制品携带完全相同的 `self_eval`/`dep_proof_ids` 字段（generate.py 的 `{**item}` 合并保证，u2-l1），由哪一行首建并无差别。

#### 4.3.2 核心流程

```text
for 每行 in 验证输出:
    problem      = 行['question'].strip()             # 外键
    prover_output = 行['proof'].strip()               # 内键（必要时先剥思维链）
    闸门: finish_reason == 'stop' 且 output 含 </think>   # 否则丢弃整行
    rating = output 去思维链取正文；score = 最后一个 boxed 值转 float
                                          # 解析失败 → 丢弃整行（不建键！）
    若 problem 首次出现:
        四个字典同步建外层键；problem2item = 行去掉
        ['messages','output','input','finish_reason','meta','finished'] 的副本
    若 prover_output 首次出现:
        建内层键；登记 self_eval / dep_proof_ids（首见优先）
    problem2proof2ratings[problem][prover_output].append(
        {rating, quality: 左表查找, score})           # join 消费点
```

「折叠」的定量效果——generate.py 的 n 倍复制在这里被还原：

\[ N_{\text{行}} \approx \sum_{\text{题}} \#\text{证明}_{} \times n_{\text{verification\_per\_proof}}, \qquad \#\text{证明键} = \sum_{\text{题}} \#\text{证明} \]

截断（`finish_reason` 非 stop）或 boxed 解析失败的行被丢弃后不建键，所以同一证明的实际评价数可能小于 n；`meanscore`（u5-l1 的 `np.mean`）按实际到齐的条数取均值。

#### 4.3.3 源码精读

主循环与两级键的提取（[inference/main.py:L319-L334](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L319-L334)）：

```python
with open(path, "r") as file:
    for line in tqdm(file, desc='reading proof verification outputs'):
        item = orjson.loads(line)
        problem = item['question'].strip()
        prover_output = item['proof'].strip()
        if drop_thought and '</think>' in prover_output:
            prover_output = prover_output.split("</think>")[-1].strip()
        if item['finish_reason'] == 'stop' and '</think>' in item['output']:
            rating = item['output'].strip()
            if drop_thought and '</think>' in rating:
                rating = rating.split("</think>")[-1].strip()
            scores = [s.strip() for s in extract_boxed_answers(rating) if s.strip()]
            try:
                score = float(scores[-1])
            except:
                continue
```

对 `proof` 也做 `drop_thought` 是防御性代码：验证输入里的 `proof` 本就是纯 Solution（u4-l2 已剥过），不含 `</think>`，这两行通常是 no-op；对 `rating` 的处理则是必须的——验证器的 `output` 带 `</think>`（u2-l1 的拼接逻辑），且切分后的正文才是 join 键。闸门与解析失败都走静默丢弃（对比 u4-l2 里的断言崩溃，这里是「宽容准入」）。

两级初始化与 join（[inference/main.py:L335-L351](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L335-L351)）：

```python
if problem not in problem2proof2ratings:
    problem2proof2ratings[problem] = {}
    problem2proof2self_eval[problem] = {}
    problem2proof2dep_proof_ids[problem] = {}
    problem2item[problem] = {key: val for key, val in item.items()
                             if key not in ['messages', 'output', 'input', 'finish_reason', 'meta', 'finished']}
if prover_output not in problem2proof2ratings[problem]:
    problem2proof2ratings[problem][prover_output] = []
    problem2proof2self_eval[problem][prover_output] = {
        'self_eval': item.get('self_eval', 'null'),
        'self_eval_score': item.get('self_eval_score', 0)
    }
    problem2proof2dep_proof_ids[problem][prover_output] = item.get('dep_proof_ids', [])
problem2proof2ratings[problem][prover_output].append({
    'rating': rating,
    'quality': rating2quality.get(rating, []),
    'score': score
})
```

值得咀嚼的三点：

- **`problem2item` 保留了 `proof` 与 `prover_output` 等字段**（剔除名单里没有它们），于是每个精炼样本行都随身携带一条「首个被见到的证明」的残留字段。无害——下一轮 `prepare_proof_verification` 会用新证明覆盖 `proof`（u4-l2 的 L97）——但读输出文件时要意识到这些字段是陈旧的。
- **`dep_proof_ids` 的取值来自精炼输入行**：R1 的证明没有这个字段，`get` 兜底为 `[]`（根节点）；R≥2 的证明带着上一轮样本的 `dep_proof_ids`（u5-l2 的 L278），穿过 generate.py 与 `prepare_proof_verification` 的字段透传存活到这里，最终写入池记录完成谱系闭环。
- **自评字段的默认值**（`'null'` / `0`）只在字段整体缺失时生效，对应「生成模板不含自评小节」的配置（`--proof_gen_template` 不是 `proof_generation` 时）。

任务打包与唯一性断言（[inference/main.py:L355-L369](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L355-L369)）：

```python
problem_idx_dedup = set()
tasks = []
for problem, proof2ratings in problem2proof2ratings.items():
    item = problem2item[problem]
    if 'problem_idx' in item:
        problem_idx = str(item['problem_idx'])
    else:
        problem_idx = hash_problem_idx(item['question'].strip())
    assert problem_idx not in problem_idx_dedup, problem_idx
    problem_idx_dedup.add(problem_idx)
    tasks.append((item, proof2ratings,
                  problem2proof2self_eval[problem],
                  problem2proof2dep_proof_ids[problem]))
```

`problem_idx` 此刻才从题目记录里恢复（有字段用字段的字符串形式，没有则用题面 SHA-256，u3-l2 的稳定主键）。**断言「全局不重复」不是锦上添花**：它保证一道题只出现在一个 task 里、进而只落在一个切块里——这是 u5-l1「证明池并行无锁追加」安全性的前提。`tasks` 的顺序等于 `problem2proof2ratings` 的键序（Python 字典保持插入序），即**题目在验证输出文件里首次出现的顺序**。

#### 4.3.4 代码实践

**实践目标**：用 6 行假数据亲眼看到「n 倍折叠」与「首见优先」。

**操作步骤**（示例代码，复刻 L319-L351 的主干）：

```python
# agg_demo.py
def agg(rows):
    p2r, p2self, p2item = {}, {}, {}
    for it in rows:
        problem, proof = it['question'].strip(), it['proof'].strip()
        if it['finish_reason'] != 'stop':
            continue                                   # 截断行丢弃
        score = float(it['output'].split('\\boxed{')[1][:-1])  # 简化版分数解析
        p2r.setdefault(problem, {})
        if proof not in p2r[problem]:
            p2r[problem][proof] = []
            p2self.setdefault(problem, {})[proof] = it['self_eval_score']  # 首见登记
        p2r[problem][proof].append(score)
    return p2r, p2self

rows = [
    {'question': ' Q1 ', 'proof': 'A', 'output': '\\boxed{1}',   'finish_reason': 'stop', 'self_eval_score': 1.0},
    {'question': ' Q1 ', 'proof': 'A', 'output': '\\boxed{0}',   'finish_reason': 'stop', 'self_eval_score': 1.0},
    {'question': ' Q1 ', 'proof': 'B', 'output': '\\boxed{0.5}', 'finish_reason': 'stop', 'self_eval_score': 0.5},
    {'question': ' Q1 ', 'proof': 'B', 'output': '(被截断)',      'finish_reason': 'length', 'self_eval_score': 0.5},
    {'question': ' Q2 ', 'proof': 'C', 'output': '\\boxed{0}',   'finish_reason': 'stop', 'self_eval_score': 0.0},
    {'question': ' Q2 ', 'proof': 'C', 'output': '(无boxed)',    'finish_reason': 'stop', 'self_eval_score': 0.0},
]
p2r, p2self = agg(rows)
for q, proof2r in p2r.items():
    for p, rs in proof2r.items():
        print(f"{q!r} 证明{p!r}: 评分 {rs}, 均值 {sum(rs)/len(rs):.2f}, "
              f"自评分(首见) {p2self[q][p]}")
```

**需要观察的现象**：6 行输入折叠成「2 题 × 各 1-2 个证明键」；证明 B 只剩 1 条评分（第 4 行截断被丢）；证明 C 只剩 1 条评分（第 6 行无 boxed 被丢）；注意第 6 行**没有建新键也没有丢已有键**——C 键由第 5 行首建。

**预期结果**：`' Q1 '`（strip 后 `'Q1'`）的证明 `'A'` 评分 `[1.0, 0.0]` 均值 0.50、自评分 1.0；`'B'` 评分 `[0.5]`；`'Q2'` 的 `'C'` 评分 `[0.0]`、自评分 0.0。待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：外键为什么用题面文本而不是 `problem_idx`？这带来什么隐患？

**答案**：题面是每行必带、且经过了与内键一致的 `strip()` 规范化的字段，用它最省事。隐患是**不同题如果题面文本完全相同会被静默合并**：两题共用一个外层键，`problem2item` 只留首见的 `problem_idx`（另一题的编号无声消失，L363 的断言也不会触发，因为只剩一个 task），两题的证明混进同一个池文件。实践中竞赛题面互不相同（即便如此，若两题同文，`hash_problem_idx` 对同文题面也会给出同一个哈希——两种主键策略在这一点上行为一致）。反过来，`problem_idx` 撞车而题面不同则会触发断言崩溃，这正是断言的防御目标。

**练习 2**：一道题的某证明被验证了 4 次，其中第 2 行的 `finish_reason` 是 `length`、第 3 行的 output 里没有 `\boxed{}`。`meanscore` 按几个评分算？

**答案**：2 个。截断行与解析失败行都在 append 之前被 `continue` 丢弃，评价列表只有第 1、4 行的条目；`np.mean` 在 u5-l1 的 L199 按实际列表长度取均值。注意丢的是「行」不是「证明」——只要还有至少一条有效评分，证明照样入池。

**练习 3**：`problem2item` 的剔除名单为什么包括 `meta` 和 `finished` 这两个 main.py 里没人写入的字段？

**答案**：防御性清理。它们不是本仓库 `generate.py` 会输出的字段（它输出 `output`/`finish_reason` 并透传输入字段，u2-l1），名单像是沿用了内部版本推理引擎的字段布局——那套引擎会写 `finished`、`meta` 之类的元数据。对当前代码而言这两个键的剔除是 no-op，但保留了与其它 `prepare_*` 函数（u4-l2 的 L108、u4-l3 的 L146 用同一份名单）的一致写法。属于「死代码但无害」的一类。

### 4.4 并行汇合与落盘：partial、Pool.imap 与接口契约

#### 4.4.1 概念说明

`tasks` 是一个「每题一个四元组」的列表，每题的聚合（读池、记账、排序、组合、拼摘要、渲染模板）是纯 CPU 的 Python 字符串工程——题多了串行会慢，于是用 `multiprocessing.Pool` 分块并行。这一段只有十几行，但把三件事配合得天衣无缝：

1. **`_split_jobs(tasks, 50)`** 把任务列表切成约 50 块（u5-l2 4.4 的结论：目标是块数 ≈ 50，块大小为 `ceil(n/50)`，同题绝不跨块）；
2. **`functools.partial`** 把 7 个对全Chunk一致的配置参数冻进函数，让 `pool.imap` 拿到一个「单参数函数（吃一个块）」；
3. **`pool.imap`**（不是 `imap_unordered`）保证结果**按块顺序**产出——输出文件的行序确定，只依赖题目首现顺序。

与下游的接口契约（算法本身见 u5-l1、u5-l2，此处只列边界）：

| 方向 | 数据 | 说明 |
|---|---|---|
| 进 worker | 一个块 = 若干 task 四元组 | `(item, proof2ratings, proof2self_eval, proof2dep_proof_ids)`，来自 4.3 |
| 进 worker | 7 个配置参数（partial 冻结） | `round_idx`、`proof_pool_dirname`、`use_old_proofs_for_refinement`、`num_trials`、`n_best_proofs_to_sample`、`n_proofs_to_refine`、`max_rating_per_score` |
| 出 worker | `(data, trials)` 二元组 | `data` = 该块所有题的精炼样本行；`trials` = 每题实际组合数（u5-l2 的 `len(dedup)`） |
| 副作用 | 证明池追加写 | 发生在 **worker 进程内**（u5-l1），主进程不碰池文件 |

#### 4.4.2 核心流程

```text
_args = {7 个配置参数}                         # 冻结成一个字典
pool = multiprocessing.Pool(cpu_count())       # 进程数 = CPU 核数（与任务块数无关！）
for (_data, _trials) in tqdm(pool.imap(
        partial(_prepare_proof_agg_tasks, **_args),   # 单参数化
        _split_jobs(tasks, 50))):                     # 约 50 个块
    data.extend(_data); trials.extend(_trials)        # 按块序收集
打印 Avg trials per statement                    # 观察每题实际组合数的窗口
把 data 逐行写进 tar_path，返回 len(data)
```

两个规模数字解耦：**块数**（约 50）决定任务粒度，**进程数**（`cpu_count()`）决定并行度。块数少于进程数时多余的 worker 空闲（比如 40 题只切 1 块，32 核机器上 31 个 worker 白等）——正确性不受影响。竞赛三份输入共 18 题左右时（IMO2025 6 题 + CMO2024 6 题 + CMO2025 6 题），`_split_jobs` 直接返回 `[tasks]` 单块，实际是「借道进程池的串行」。

#### 4.4.3 源码精读

参数冻结与并行消费（[inference/main.py:L371-L387](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L371-L387)）：

```python
_args = dict(
    round_idx=round_idx,
    proof_pool_dirname=proof_pool_dirname,
    use_old_proofs_for_refinement=use_old_proofs_for_refinement,
    num_trials=num_trials,
    n_best_proofs_to_sample=n_best_proofs_to_sample,
    n_proofs_to_refine=n_proofs_to_refine,
    max_rating_per_score=max_rating_per_score,
)
data = []
trials = []
cpu_count = multiprocessing.cpu_count()
print(f"multiprocessing: {cpu_count} workers", flush=True)
pool = multiprocessing.Pool(cpu_count)
for (_data, _trials) in tqdm(pool.imap(partial(_prepare_proof_agg_tasks, **_args), _split_jobs(tasks, 50))):
    data.extend(_data)
    trials.extend(_trials)
```

为什么用多进程而不是多线程：聚合阶段是大字符串的 `str.format` 与 `deepcopy`（每个精炼样本都要拼一份可能几万 token 的提示词），Python GIL 让多线程无法并行执行这些纯 Python 代码；多进程各持解释器才能真正吃满多核。副作用（池写入）随任务一起被搬进 worker，主进程只收集结果——这正是 u5-l1「并行无锁」设计的执行面：唯一性断言（L363）+ 同题不跨块，共同保证一个池文件同一时刻只有一个进程在追加。

统计与落盘（[inference/main.py:L389-L395](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L389-L395)）：

```python
print(f"Avg trials per statement = {np.mean(trials)}", flush=True)
os.makedirs(os.path.dirname(tar_path), exist_ok=True)
with open(tar_path, "w") as file:
    for item in data:
        print(json.dumps(item), file=file, flush=True)
return len(data)
```

`Avg trials per statement` 是调参时最有用的免费指标：它告诉你 `num_trials`（`--n_agg_trials`）设 32 时平均每题真拿到了几个组合——候选不足（`\binom{m}{k}` 小于上限）或满分早停都会拉低它（u5-l2 的练习 1 展示过凑不满的情形）。`trials` 对早停的题记 0，所以这个均值也隐含了「已解决题占比」的信息。

最后回答一个顺序问题：`pool.imap` 与 `imap_unordered` 的区别在于前者**按输入顺序**产出结果。这里每行样本相互独立、下游不依赖行序，但有序版本让 `proof_gen_R{R}/input.jsonl` 的行序只由「题目首现顺序 + 块内顺序」决定——同数据同参数（固定随机种子）可复现，排查问题时能按行号对回题目。`__main__` 不接收返回值 `len(data)`，它只对人工调用（如本讲综合实践）有意义。

#### 4.4.4 代码实践

**实践目标**：验证 `partial + Pool.imap` 的两个关键性质——单参数化与**结果按块序产出**（哪怕后块先算完）。

**操作步骤**（示例代码，保存为 `imap_demo.py` 运行；Linux 下直接跑）：

```python
import multiprocessing, time
from functools import partial

def agg(opts, chunk):
    """模拟 _prepare_proof_agg_tasks：吃一个块，返回 (样本, 试验数)"""
    time.sleep((6 - chunk[0]) * 0.02)      # 让块号大的先算完
    return ([f"题{i}的精炼样本" for i in chunk], [len(chunk)])

if __name__ == '__main__':
    tasks = list(range(5))
    chunks = [tasks[i:i+1] for i in range(5)]          # 5 个块：[0],[1],[2],[3],[4]
    with multiprocessing.Pool(3) as pool:
        for data, trials in pool.imap(partial(agg, {"round_idx": 1}), chunks):
            print("收到:", data, trials)                # 观察产出顺序
```

**需要观察的现象**：块 4 最早算完（睡眠最短），但产出行仍然从「题0」开始按块序出现——先算完的块在 `imap` 里要等前面的块交卷。

**预期结果**：5 行输出依次是 题0、题1、题2、题3、题4（顺序恒定）；把 `pool.imap` 换成 `pool.imap_unordered` 再跑，行序变为完成序（大致 题4、题3、…、题0）。两种写法内容相同、只有顺序不同。待本地验证。

#### 4.4.5 小练习与答案

**练习 1**：为什么配置参数走 `partial`，而不是把 `(chunk, config)` 一起放进待迭代列表？

**答案**：`pool.imap` 只接受单参数映射函数。配置对每个块完全相同，放进列表会让同一份配置被 pickle 并传输 N 块次；`partial` 冻结一次，块作为唯一变量在迭代器里流动，语义上「变的是任务、不变的是配置」也更清晰。代价是 partial 对象本身也要随函数引用 pickle 到子进程——对 7 个标量参数来说开销可以忽略。

**练习 2**：若把 `_split_jobs(tasks, 50)` 换成手工交错切块（题 0、50、100… 进块 0），会破坏什么？

**答案**：交错切块不会让同一题进两个块（还是那 50 题），所以**正确性不破坏**——每题仍只被一个 worker 处理一次。真正破坏的是 u5-l2 4.4 分析过的负载形态：交错后每块都要碰 50 道不同的题，块数不变但没有任何收益，纯属折腾。会出事的是「同一题出现在两个块」的切法（比如按行号硬切 tasks 列表之外再复制），那会导致两个进程同时追加同一池文件、`proof_id` 撞号、谱系断言失败——L363 的唯一性断言就是在调度前把这处隐患炸出来。

**练习 3**：进程数取 `cpu_count()`，但任务只有 1 个块时会怎样？为什么这样仍然是安全的？

**答案**：1 个块意味着 `imap` 只派发 1 次任务，其余 worker 创建后闲置直到池关闭，浪费一点启动开销（fork 出 N-1 个闲置进程）。安全是因为没有共享可变状态：四个嵌套字典在分块前就切给了唯一的块，证明池文件只有一个进程会追加，主进程只做 `extend` 收集。正确性依赖的是「每题恰好一个块」这个不变量，与 worker 数量无关。

<!-- APPEND_MARKER -->


