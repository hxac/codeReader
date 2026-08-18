# 证明池机制：持久化、去重与谱系追踪

## 1. 本讲目标

本讲是进阶难点单元 u5 的第一讲，深入 `main.py` 中 `_prepare_proof_agg_tasks` 函数的「证明池」部分。学完后你应该能够：

1. 描述证明池文件的落盘组织方式 `{proof_pool_dirname}/{source_name}/{problem_idx}.jsonl`，以及每条记录的全部字段。
2. 解释 `meanscore` 如何由同一证明的多次验证评分取均值得到，`score2ratings` 如何按分数桶聚合同分评价，以及 JSON 落盘造成的键类型变化。
3. 说明 `proof_id` 递增编号的分配规则，以及 `dep_proof_ids` 如何追踪「这条证明是从哪些旧证明精炼来的」这条谱系链，并理解入库时对父证明的存在性断言。
4. 独立实现一个可复现源码行为的 `proof_pool.py` 模块（`load_pool` + `append_proofs`），并用手造数据验证去重断言与编号递增。

## 2. 前置知识

在阅读本讲之前，你需要先理解以下来自前几讲的概念（这里只做一句话回顾，细节请回看对应讲义）：

- **流水线四环**（u1-l1）：生成 → 验证 → 元验证 → 精炼，多轮循环。证明池是这个循环的「记忆」。
- **每轮的中间文件**（u4-l1）：`main.py` 在每轮产出 `proof_gen_R{R}`、`proof_verification_R{R}`、`meta_verification_R{R}` 三类子目录；R≥2 的精炼输入由上一轮的验证输出经 `prepare_proof_refinement` 汇合而成。
- **验证输出与评分契约**（u3-l1、u4-l2）：验证器按 0 / 0.5 / 1 三档给证明打分，分数写在输出的 `\boxed{}` 里；`prepare_proof_verification` 已经把每条记录切出 `proof`（纯 Solution）、`self_eval`、`self_eval_score` 等字段。
- **problem_idx 主键**（u1-l2、u3-l2）：输入题目的全局唯一标识（如 `"IMO2025-1"`），缺失时用题面的 SHA-256 哈希兜底。
- **generate.py 的字段透传**（u2-l1）：生成引擎以 `{**item}` 合并输出、保留输入的全部原字段——这一点是谱系信息能跨轮存活的关键。
- **多重采样**（u2-l2）：同一条输入会被复制 n 份发出去，所以一个证明文本天然会收到多次验证。

另外一个本讲反复用到的数据结构直觉：`prepare_proof_refinement` 在调用本讲的主角之前，会把上一轮的验证输出整理成四个嵌套字典（问题 → 证明文本 → 各种信息），然后按题目打包成 `tasks` 列表传进来。本讲的主角 `_prepare_proof_agg_tasks` 拿到的每个 task 就是一道题的全部证据材料。

## 3. 本讲源码地图

| 文件 | 本讲关注的部分 | 作用 |
| --- | --- | --- |
| [inference/main.py](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py) | `_prepare_proof_agg_tasks` 的证明池读写段（L165-L220） | 本讲主角：读旧池、双重去重、聚合评分、分配编号、追加写入、维护谱系 |
| [inference/main.py](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py) | `prepare_proof_refinement` 中 task 的构造（L286-L395） | 说明池记录的上游数据从哪来 |
| [inference/main.py](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py) | `__main__` 中对本函数的调用点（L430-L446） | `round_idx`、`use_old_proofs_for_refinement` 等参数的实际取值 |
| [inference/utils.py](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/utils.py) | `hash_problem_idx`（L5-L6） | 无 `problem_idx` 字段时的稳定主键 |
| [inference/run.sh](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/run.sh) | `proof_pool_dirname` 的配置（L6-L7） | 官方运行时证明池的落盘位置 |

证明池落在 `run.sh` 配置的 `${output_dirname}/proof_pool` 目录下，即与各轮中间文件同级，整体重启后依然保留。

## 4. 核心概念与源码讲解

### 4.1 证明池是什么：一题一文件的持久化账本

#### 4.1.1 概念说明

流水线会跑很多轮（`run.sh` 里 `max_rounds=16`），每轮每题都会产生一批新证明和一批验证评价。如果没有一个跨轮的「账本」，第 10 轮的程序就不知道第 3 轮曾经写出过什么证明、它们得了多少分。**证明池（proof pool）就是这个账本**：每道题一个 jsonl 文件，追加写入，记录这道题历史上所有「值得记住」的证明及其验证结果。

它同时服务三个下游需求：

1. **精炼选材**：下一轮精炼要从历史证明里挑最好的若干个作为参考（u5-l2 的主题）。
2. **早停**：一旦某题出现过满分证明，就没必要继续精炼它了（本讲 4.4.3 会看到判断位置）。
3. **去重**：同一证明文本不应被重复计分、重复入库。

「账本」的每一行是一条证明记录，字段固定为 7 个：

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `proof` | str | 证明正文（纯 Solution，已剥掉 Self Evaluation） |
| `meanscore` | float | 该证明所有验证评分的均值 |
| `score2ratings` | dict | 分数 → 评价列表 的桶聚合 |
| `self_eval` | dict | `{'self_eval': str, 'self_eval_score': float}`，生成器的自我评价 |
| `proof_id` | int | 本题范围内递增的证明编号 |
| `dep_proof_ids` | list | 父证明的 proof_id 列表（谱系） |
| `round_idx` | int | 该证明产生于哪一轮 |

#### 4.1.2 核心流程

先看主角函数的入口部分，搞清楚「一题一文件」的路径是怎么拼出来的：

```
对每个 task（一道题）:
    source_name = item 里的 'source_name'（R1 时注入，缺省 'temp_source_name'）
    problem_idx = str(item['problem_idx'])，无该字段则 = SHA-256(题面)
    池文件路径 = f"{proof_pool_dirname}/{source_name}/{problem_idx}.jsonl"
    读取该文件（若存在）→ 旧池记录 + 两个去重集合
    处理本题的新证明 → 追加写入该文件
```

`source_name` 与 `problem_idx` 这两级目录名的分工：前者隔离**数据源**（`IMO2025`、`CMO2024`……来自输入文件名），后者隔离**题目**。两级拼接保证了不同竞赛的同名题号（比如两场比赛都有「第 1 题」）不会写进同一个文件。

#### 4.1.3 源码精读

函数签名与 task 解包——注意 task 是个四元组，后面三个都是「证明文本 → 信息」的字典：

[inference/main.py:L165-L175](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L165-L175)

```python
def _prepare_proof_agg_tasks(tasks, round_idx=None, proof_pool_dirname=None, use_old_proofs_for_refinement=False, num_trials=16, n_best_proofs_to_sample=6, n_proofs_to_refine=4, max_rating_per_score=4):
    data = []
    trials = []
    print(f"tasks = {len(tasks[0])}", flush=True)
    for (item, proof2ratings, proof2self_eval, proof2dep_proof_ids) in tasks:
        source_name = item.get('source_name', 'temp_source_name')
        if 'problem_idx' in item:
            problem_idx = str(item['problem_idx'])
        else:
            problem_idx = hash_problem_idx(item['question'].strip())
```

这段做了三件事：解包四元组；取 `source_name`（用 `.get` 兜底成 `'temp_source_name'`，说明作者允许跑不带来源名的临时数据）；确定 `problem_idx` 主键——注意 `str(...)` 的防御性转换，即使数据里存的是整数也能安全拼进文件路径。

主键兜底函数在 utils.py，只有两行：

[inference/utils.py:L5-L6](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/utils.py#L5-L6)

```python
def hash_problem_idx(question):
    return hashlib.sha256(question.encode()).hexdigest()
```

对题面做 SHA-256，返回十六进制摘要。它的两个关键性质（u3-l2 已详细讲过）：**确定性**——同一题面永远得到同一主键，跨轮、跨进程一致；**雪崩效应**——题面差一个字符主键就完全不同，不会撞车。

池文件路径的拼接与落盘字段名的定义：

[inference/main.py:L179-L180](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L179-L180)

```python
        proof_pool_path = f"{proof_pool_dirname}/{source_name}/{problem_idx}.jsonl"
        if os.path.exists(proof_pool_path):
```

写入时字段名与顺序的权威定义（`dict(zip(...))` 这一行就是 4.1.1 表格的出处）：

[inference/main.py:L211-L218](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L211-L218)

```python
        os.makedirs(os.path.dirname(proof_pool_path), exist_ok=True)
        with open(proof_pool_path, "a") as file:
            for record in proof_meanscore_ratings_tuples:
                record = dict(zip(['proof', 'meanscore', 'score2ratings', 'self_eval', 'proof_id', 'dep_proof_ids'], record))
                record['round_idx'] = round_idx
                for _id in record['dep_proof_ids']:
                    assert _id in proof_id_dedup or float(_id) < 0, f"{_id} {len(proof_id_dedup)} {proof_pool_path}"
                print(json.dumps(record), file=file, flush=True)
```

三个细节值得圈出：文件以 **`"a"` 追加模式**打开（历史记录永不重写）；**逐行 `flush`**（崩溃时最多丢当前行，已写行不丢）；每行写入前对 `dep_proof_ids` 做存在性断言（4.4 详解）。

最后确认配置入口——argparse 注册与 run.sh 的取值：

- [inference/main.py:L22](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L22)：`--proof_pool_dirname` 是 `required=True` 的参数，help 文本就写着 "directory to maintain a pool of generated proofs for each evaluated problem"。
- [inference/run.sh:L6-L7](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/run.sh#L6-L7)：`proof_pool_dirname=${output_dirname}/proof_pool`，即池目录嵌在输出目录里。

#### 4.1.4 代码实践

**实践目标**：亲手建出符合源码组织的池目录结构，验证「一题一文件」。

**操作步骤**（以下 `make_pool_tree.py` 为示例代码，可直接运行，不依赖任何 API）：

```python
# make_pool_tree.py（示例代码）
import hashlib, json, os

def hash_problem_idx(question):  # 复刻 utils.py:5-6
    return hashlib.sha256(question.encode()).hexdigest()

pool_root = "mini_pool"
problems = [
    {"source_name": "MyTest", "problem_idx": "MyTest-1", "question": "证明题一……"},
    {"source_name": "MyTest", "problem_idx": "MyTest-2", "question": "证明题二……"},
    {"source_name": "Another", "problem_idx": "Another-1", "question": "另一场比赛的第 1 题……"},
]
for p in problems:
    idx = str(p["problem_idx"]) if "problem_idx" in p else hash_problem_idx(p["question"].strip())
    path = f"{pool_root}/{p['source_name']}/{idx}.jsonl"
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a") as f:
        f.write(json.dumps({"proof": "placeholder", "meanscore": 0.0,
                            "score2ratings": {}, "self_eval": {"self_eval": "null", "self_eval_score": 0},
                            "proof_id": 1, "dep_proof_ids": [], "round_idx": 1}) + "\n")
print(sorted(os.walk(pool_root).__next__()[1]))  # 顶层应只有两个 source_name 目录
```

**需要观察的现象**：生成的目录树为 `mini_pool/MyTest/MyTest-1.jsonl`、`mini_pool/MyTest/MyTest-2.jsonl`、`mini_pool/Another/Another-1.jsonl` 三个文件，两个数据源分属不同子目录。

**预期结果**：`MyTest` 与 `Another` 两个子目录互不干扰；如果把第一题的 `problem_idx` 字段删掉再跑，会生成一个以 64 位十六进制哈希命名的文件，且重复运行文件名不变（SHA-256 的确定性）。

#### 4.1.5 小练习与答案

**练习 1**：为什么池路径要分 `source_name` 和 `problem_idx` 两级，而不是直接用 `problem_idx` 一级？

**答案**：`problem_idx` 的唯一性承诺是「竞赛名拼题号、全局唯一」（u1-l2），依赖输入数据自身规范。两级结构让不同数据源在文件系统层面天然隔离，即使两个数据源出现了相同的 `problem_idx`（比如都叫 `1`），也不会写进同一个文件互相污染；同时也方便按数据源整体浏览或备份池目录。

**练习 2**：`item.get('source_name', 'temp_source_name')` 这个兜底值什么时候会生效？

**答案**：当题目记录里没有 `source_name` 字段时。正常流程中 R1 初始化（main.py L413-L414）会为每条输入注入 `source_name`，且该字段随记录一路透传，所以官方数据下不会触发；只有直接手工构造 task、或输入来自未经 R1 初始化的旁路数据时才会落到 `temp_source_name` 这个共享目录——多条来源不同的题会混进同一个子目录，仅靠 `problem_idx` 区分。

---

### 4.2 读池与双重去重：`proof_dedup` 与 `proof_id_dedup`

#### 4.2.1 概念说明

池是追加写的，每次进入本函数都要先把旧记录读回来。读的过程中程序同时维护**两个去重集合**，分别对应证明的两种「身份」：

- **内容身份**：`proof_dedup` 存 `(problem_idx, proof)` 二元组——证明正文一字不差才算重复。
- **编号身份**：`proof_id_dedup` 存历史 `proof_id`——编号不允许被两个不同证明占用。

为什么要两套？因为它们防的是不同的病：内容去重保证「同一证明只入一次账、只算一次分」；编号去重保证 `proof_id` 这个谱系坐标不被复用——如果编号能重复，`dep_proof_ids` 里的「父证明是 3 号」就会有歧义。读池阶段这两个集合用 `assert` 做完整性校验：**池文件自身不许有重复**，一旦发现立刻崩溃，而不是静默吞掉。

#### 4.2.2 核心流程

```
proof_dedup = set(); proof_id_dedup = set(); old_proof_pool = []
若池文件存在:
    逐行 json.loads
    断言 (problem_idx, proof) 未出现过        ← 池内证明文本重复则崩溃
    断言 proof_id 未出现过（缺省按 'null'）   ← 池内编号重复则崩溃
    把五元组 (proof, meanscore, score2ratings, self_eval, proof_id) 加入 old_proof_pool
    两个集合各 add 一份
下一个新编号:
    nxt_proof_id = max(proof_id_dedup) + 1    （池为空则从 1 开始）
```

#### 4.2.3 源码精读

读池与双重断言的完整实现：

[inference/main.py:L180-L194](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L180-L194)

```python
        if os.path.exists(proof_pool_path):
            with open(proof_pool_path, "r") as file:
                for line in file:
                    record = json.loads(line)
                    assert (problem_idx, record['proof']) not in proof_dedup
                    assert record.get('proof_id', 'null') not in proof_id_dedup
                    old_proof_pool.append(
                        (record['proof'], record['meanscore'], record['score2ratings'], record['self_eval'], record.get('proof_id', 'null'))
                    )
                    proof_dedup.add((problem_idx, record['proof']))
                    proof_id_dedup.add(record.get('proof_id', 'null'))
        if proof_id_dedup:
            nxt_proof_id = max(proof_id_dedup) + 1
        else:
            nxt_proof_id = 1
```

逐行说明：

- 两个 `assert` 是**池文件的自检**：它们只可能在「池文件本身已经被外部工具写坏」时触发，正常流水线产出的池不会违反。
- 旧记录被还原成**五元组**（proof, meanscore, score2ratings, self_eval, proof_id）——注意没有 `dep_proof_ids`，这个形状差异在 4.4.3 还会展开。
- `record.get('proof_id', 'null')`：历史记录若缺 `proof_id`，用字符串 `'null'` 占位参与去重。两张缺 `proof_id` 的记录会撞在 `'null'` 上触发断言。
- `nxt_proof_id = max(proof_id_dedup) + 1`：新编号从历史最大编号续接，保证**单调递增、永不复用**。

与之配套的还有「新证明入池前」的内容去重跳过（与读池共用同一个 `proof_dedup` 集合）：

[inference/main.py:L196-L198](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L196-L198)

```python
        for proof, ratings in proof2ratings.items():
            if (problem_idx, proof) in proof_dedup:
                continue
```

这一步赋予证明池「**一次成账、不重算**」的语义：某证明文本只要进过池，之后无论它再被验证多少次，新的评分都不会更新池里那条记录（4.4.4 分析这个取舍）。

一个值得留意的边界情况：`max(proof_id_dedup)` 在集合里混有 `'null'` 字符串和整数时会抛 `TypeError`（Python 3 不允许 `str` 与 `int` 比大小）。由于本仓库代码写入的 `proof_id` 恒为整数，这只在外部工具写坏池文件时才可能发生——与上面两个断言同属「防御外部损坏」的范畴，但触发的是 TypeError 而非 AssertionError。待本地验证：可手工构造一条含 `"proof_id": null` 的池记录复现。

#### 4.2.4 代码实践

**实践目标**：用 `load_pool` 复刻读池逻辑，验证三种损坏场景下的行为差异。

**操作步骤**（`load_pool` 为示例代码，逻辑逐行对应 main.py L180-L194）：

```python
# proof_pool.py 之 load_pool（示例代码）
import json, os

def load_pool(pool_path, problem_idx):
    old_proof_pool, proof_dedup, proof_id_dedup = [], set(), set()
    if os.path.exists(pool_path):
        with open(pool_path) as file:
            for line in file:
                record = json.loads(line)
                assert (problem_idx, record['proof']) not in proof_dedup, "证明文本重复"
                assert record.get('proof_id', 'null') not in proof_id_dedup, "proof_id 重复"
                old_proof_pool.append(
                    (record['proof'], record['meanscore'], record['score2ratings'],
                     record['self_eval'], record.get('proof_id', 'null')))
                proof_dedup.add((problem_idx, record['proof']))
                proof_id_dedup.add(record.get('proof_id', 'null'))
    nxt_proof_id = max(proof_id_dedup) + 1 if proof_id_dedup else 1
    return old_proof_pool, proof_dedup, proof_id_dedup, nxt_proof_id
```

然后做三组实验：① 池里放两条 `proof` 相同的记录 → 应触发「证明文本重复」断言；② 两条 `proof` 不同但 `proof_id` 都是 1 → 应触发「proof_id 重复」断言；③ 三条正常记录（proof_id 为 1、2、5）→ `nxt_proof_id` 应为 6。

**需要观察的现象**：前两组分别在对应断言处以 `AssertionError` 崩溃并打印你写的提示信息；第三组正常返回。

**预期结果**：`nxt_proof_id == 6`，且 `proof_dedup` 的大小等于记录条数、`proof_id_dedup == {1, 2, 5}`。第三组验证了编号不必连续也能正确续接（max+1 而非 count+1，这正是「永不复用」的关键——哪怕中间的编号被删掉，也不会把旧编号分配给新证明）。

#### 4.2.5 小练习与答案

**练习 1**：如果把 `nxt_proof_id = max(proof_id_dedup) + 1` 改成 `len(proof_id_dedup) + 1`，什么情况下会出 bug？

**答案**：当历史编号不连续时。比如池里已有编号 1、2、5（编号 3、4 的行被手工删除，或未来某种跳号逻辑），`len+1 = 4` 会把 4 分配给新证明——4 没被占用看似安全，但下一轮 `len+1 = 5` 就会把已存在的 5 再分给另一个证明，`dep_proof_ids` 从此无法唯一指认父证明。`max+1` 保证新编号严格大于一切历史编号，与连续性无关。

**练习 2**：读池断言失败时程序是「跳过这条记录继续跑」还是「崩溃」？这个选择与 u4-l3 里 `prepare_meta_verification` 的容错风格有何不同？

**答案**：崩溃（裸 `assert`，不被 try 包住）。这与 `prepare_meta_verification` 里「boxed 解析失败就 `continue` 静默丢弃」的宽容风格相反。理由是错误的严重度不同：丢一条元验证输入只是少复核一条评价，损失可控；而池文件损坏意味着谱系与评分账本不可信，后续所有精炼决策都建立在脏数据上，不如尽早失败暴露问题。

---

### 4.3 新证明入库：`meanscore`、`score2ratings` 与递增 `proof_id`

#### 4.3.1 概念说明

读回旧池后，函数遍历本题上一轮的验证结果（`proof2ratings`：证明文本 → 该证明收到的所有评价），为每个**池里没有的**新证明计算三样东西并入库：

1. **`meanscore`（均分）**：同一证明会被验证 \( n \) 次（`--n_verification_per_proof`，run.sh 配 64），把这 \( n \) 个分数取平均，得到这个证明的「共识分」。它是下游排序选优的主键（u5-l2）。

   \[ \text{meanscore}(p) = \frac{1}{|R_p|} \sum_{r \in R_p} \text{score}(r) \]

   其中 \( R_p \) 是证明 \( p \) 收到的全部验证评价。以 64 次验证为例：48 次给 1.0、8 次给 0.5、8 次给 0.0，则 meanscore \( = (48 \times 1.0 + 8 \times 0.5 + 8 \times 0.0) / 64 = 52/64 = 0.8125 \)。

2. **`score2ratings`（分数桶）**：把评价按分数分桶存放，形如 `{1.0: [48 条评价], 0.5: [8 条], 0.0: [8 条]}`。它保留了每条**具体批语**，供精炼提示词按桶采样展示（u5-l2 讲「单桶最多 8 条、多桶每桶最多 max_rating_per_score 条」的上限时会用到）。

3. **`proof_id`（编号）**：从 `nxt_proof_id` 起逐个分配，每发一个 `+1`。

注意「同一证明的多次验证」是怎么来的：上一讲 u4-l2 里每条精炼请求被复制 \( n \) 份发给验证器，`prepare_proof_refinement` 又按证明文本精确分组（main.py L340：`if prover_output not in problem2proof2ratings[problem]`），所以同一文本的多条验证输出自然聚到同一个 key 下，`ratings` 列表就是它们的汇总。

#### 4.3.2 核心流程

```
for proof, ratings in proof2ratings.items():
    若 (problem_idx, proof) 已在池中: 跳过（不重算）
    meanscore = float(mean([r['score'] for r in ratings]))
    score2ratings = {}: 把每条 rating 塞进 r['score'] 对应的桶
    proof_dedup.add((problem_idx, proof))
    proof_id = nxt_proof_id; nxt_proof_id += 1
    新记录 = (proof, meanscore, score2ratings, self_eval, proof_id, dep_proof_ids)   ← 六元组
逐条写入池文件（补 round_idx、断言父编号存在）
```

#### 4.3.3 源码精读

聚合与编号分配的完整实现：

[inference/main.py:L195-L210](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L195-L210)

```python
        proof_meanscore_ratings_tuples = []
        for proof, ratings in proof2ratings.items():
            if (problem_idx, proof) in proof_dedup:
                continue
            meanscore = float(np.mean([rating['score'] for rating in ratings]))
            score2ratings = {}
            for rating in ratings:
                score = rating['score']
                if score not in score2ratings:
                    score2ratings[score] = []
                score2ratings[score].append(rating)
            proof_dedup.add((problem_idx, proof))
            proof_id = nxt_proof_id
            nxt_proof_id += 1
            record = (proof, meanscore, score2ratings, proof2self_eval[proof], proof_id, proof2dep_proof_ids[proof])
            proof_meanscore_ratings_tuples.append(record)
```

要点：

- `float(np.mean(...))` 把 numpy 标量转回 Python float——否则 `json.dumps` 无法序列化 `np.float64`（旧版 numpy 下会抛 TypeError），这是一个必要的类型落地。
- `score2ratings` 的桶键在**内存里是 float**（来自 boxed 解析的 `float(...)`，u4-l2/u4-l3 已讲）。但 `json.dumps` 会把 dict 的键转成字符串落盘，于是池文件里是 `{"1.0": [...], "0.5": [...]}`；下游消费时专门做了一次转回：

[inference/main.py:L247](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L247)

```python
                        score2ratings = {float(key): val for key, val in score2ratings.items()}
```

这行是「JSON 键必须是字符串」这一语言限制的补丁——**任何自己写的池文件读取工具都必须做同样的转换**，否则紧随其后的 `assert isinstance(score, float)`（L255）会拦住你。这是本讲最容易踩的坑。

- 新记录是**六元组**，比旧池的五元组多一个 `dep_proof_ids`。两种形状能共用同一条下游代码，靠的是切片：

[inference/main.py:L245](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L245)

```python
                    proof, meanscore, score2ratings, self_eval, proof_id = proof_meanscore_ratings_tuples[idx][:5]
```

`[:5]` 让五元组（旧池记录）和六元组（新记录）都能解包成功——不过 4.4.4 会说明这同时意味着旧证明的谱系信息在再利用时不被读取。

- `proof2self_eval[proof]` 是 dict `{'self_eval': ..., 'self_eval_score': ...}`（在 prepare_proof_refinement 里构造，见 [inference/main.py:L342-L345](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L342-L345)），作为整条存入 `self_eval` 字段；下游排序键 `x[3]['self_eval_score']`（L227）正依赖这个形状。

写入动作本身已在 4.1.3 引用（L211-L218），此处不重复。

#### 4.3.4 代码实践

**实践目标**：实现 `append_proofs`，用手造的 2 轮共 5 条证明数据验证 meanscore 计算、桶聚合与编号递增。

**操作步骤**（示例代码，接在 4.2.4 的 `proof_pool.py` 里）：

```python
# proof_pool.py 之 append_proofs（示例代码）
import numpy as np

def append_proofs(pool_path, problem_idx, round_idx, proof2ratings, proof2self_eval, proof2dep_proof_ids):
    old_pool, proof_dedup, proof_id_dedup, nxt_proof_id = load_pool(pool_path, problem_idx)
    new_records = []
    for proof, ratings in proof2ratings.items():
        if (problem_idx, proof) in proof_dedup:
            continue                                   # 已入池：跳过，不重算
        meanscore = float(np.mean([r['score'] for r in ratings]))
        score2ratings = {}
        for r in ratings:
            score2ratings.setdefault(r['score'], []).append(r)
        proof_dedup.add((problem_idx, proof))
        proof_id = nxt_proof_id; nxt_proof_id += 1     # 递增分配
        new_records.append((proof, meanscore, score2ratings,
                            proof2self_eval[proof], proof_id, proof2dep_proof_ids[proof]))
    with open(pool_path, "a") as f:
        for rec in new_records:
            d = dict(zip(['proof', 'meanscore', 'score2ratings', 'self_eval', 'proof_id', 'dep_proof_ids'], rec))
            d['round_idx'] = round_idx
            for _id in d['dep_proof_ids']:
                assert _id in proof_id_dedup or float(_id) < 0, f"父证明 {_id} 不在池中"
            f.write(json.dumps(d) + "\n")
    return new_records
```

测试数据（示例代码）：

```python
# 第 1 轮：3 个证明，各收到 4 次验证（分数故意不同）
p2r = {
    "证明A": [{"rating": "好", "score": 1.0}, {"rating": "尚可", "score": 0.5},
              {"rating": "好", "score": 1.0}, {"rating": "好", "score": 1.0}],   # meanscore = 0.875
    "证明B": [{"rating": "差", "score": 0.0}, {"rating": "差", "score": 0.0}],   # meanscore = 0.0
    "证明C": [{"rating": "半对", "score": 0.5}, {"rating": "半对", "score": 0.5}],  # meanscore = 0.5
}
se = {p: {"self_eval": "自评正文", "self_eval_score": 1.0} for p in p2r}
dp = {p: [] for p in p2r}                       # 首轮证明没有父证明
append_proofs("mini_pool/MyTest/MyTest-1.jsonl", "MyTest-1", 1, p2r, se, dp)

# 第 2 轮：2 个精炼证明，分别由 1 号和 (2,3) 号证明精炼而来
p2r2 = {
    "证明A'（改进版）": [{"rating": "好", "score": 1.0}, {"rating": "好", "score": 1.0}],
    "证明B''（改进版）": [{"rating": "半对", "score": 0.5}, {"rating": "差", "score": 0.0}],
}
se2 = {p: {"self_eval": "自评", "self_eval_score": 0.5} for p in p2r2}
dp2 = {"证明A'（改进版）": [1], "证明B''（改进版）": [2, 3]}
append_proofs("mini_pool/MyTest/MyTest-1.jsonl", "MyTest-1", 2, p2r2, se2, dp2)
```

**需要观察的现象**：第 1 轮后池文件 3 行，`proof_id` 为 1、2、3；第 2 轮后 5 行，新行 `proof_id` 为 4、5，`round_idx` 分别为 1 和 2；`证明A` 的 `meanscore` 为 0.875，其 `score2ratings` 在文件里是 `{"1.0": 3 条, "0.5": 1 条}`（注意键是字符串）。

**预期结果**：五条记录齐备；再次用第 1 轮的 `p2r` 调用 `append_proofs` 时 `new_records` 为空列表（内容去重生效，meanscore 不被第 2 次计算覆盖）；若把 `dp2` 里某个父编号改成 99 再跑，断言「父证明 99 不在池中」崩溃。以上均为本地可复现的纯 Python 行为，运行细节待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：`meanscore` 为什么取均值而不是取中位数或最小值？

**答案**：均值把证明质量表达成验证器的「共识置信度」：64 次验证里满分比例越高，均值越接近 1。中位数在 0/0.5/1 三档下分辨率有限（64 次里 33 次 0 分、31 次 1 分时中位数是 0，均值是 0.484，后者保留了「几乎一半验证者认可」的信息）；最小值则过于悲观，一次 0 分就判死刑，与「验证器本身可能出错、需要元验证复核」的体系设定矛盾。另外下游早停条件是 `meanscore > 0.99999`（见 4.4.3），只有均值能表达「全体一致给满分」这个语义。

**练习 2**：直接用 `json.loads` 读池文件后，`score2ratings` 的键是什么类型？为什么下游要做 `{float(key): val ...}` 的转换？

**答案**：字符串。JSON 规范要求对象的键必须是字符串，`json.dumps` 落盘时把 float 键 `1.0` 转成了 `"1.0"`，读回来不会自动还原。而下游对键做数值排序（`sorted(list(score2ratings.keys()))`，L248）并用 `assert isinstance(score, float)`（L255）校验类型，若不转换，字符串排序会把 `"0.5"` 排到 `"0.0"` 与 `"1.0"` 之间只是碰巧正确、但类型断言会先崩溃。

---

### 4.4 谱系追踪与入库的边界细节

#### 4.4.1 概念说明

精炼的本质是「拿若干旧证明（连同它们的验证批语）作为参考，生成一个更好的新证明」。如果把每个证明看成一个节点，那么每个新证明都应记下它的**父节点集合**——这就是 `dep_proof_ids`（dependency proof ids）记录的谱系（lineage）。有了它，整个运行结束后可以从池文件重建一棵「证明家谱」：哪个证明由哪几个证明精炼而来、哪一支最终拿到了满分。这对复盘模型行为、筛选有效的精炼路径都有价值。

谱系信息的生命周期是一条横跨三轮文件的长链：

```
本轮精炼请求（proof_gen_R{R+1}/input.jsonl）
    记录 dep_proof_ids = 参考证明的编号列表          ← L276-L279 写入
        ↓ generate.py 以 {**item} 合并输出，原字段透传  ← u2-l1
下一轮生成输出（proof_gen_R{R+1}/output.jsonl）
    dep_proof_ids 随行保留
        ↓ prepare_proof_verification 不 pop 该字段      ← u4-l2
验证输出（proof_verification_R{R+1}/output.jsonl）
        ↓ prepare_proof_refinement 按证明文本分组收集    ← L346
proof2dep_proof_ids[证明文本] = item['dep_proof_ids']
        ↓ 新证明入库                                    ← L209
池记录的 dep_proof_ids 字段（断言父编号全部在旧池中）  ← L216-L217
```

入库时还有一层**质量闸门**：断言每个父编号确实存在于旧池（`proof_id_dedup`），防止谱系指向不存在的证明。注意断言还允许 `float(_id) < 0` 的负数编号——当前仓库没有任何代码生产负数编号，这是一个预留的哨兵约定（例如将来想标记「外部导入的证明」），其确切用途**待确认**。

#### 4.4.2 核心流程

谱系写入侧（本轮组合生成时，属于 u5-l2 的完整逻辑，这里只看与本讲相关的两行）：

```
对组合里的每个被选中的证明:
    dep_proof_ids.append(该证明的 proof_id)      ← 新请求记下参考了谁
sample = deepcopy(item)
sample.update({'messages': msg, 'dep_proof_ids': dep_proof_ids})
```

谱系读取侧（下一轮入库时）：

```
新记录.dep_proof_ids = proof2dep_proof_ids[证明文本]   ← 上轮请求里带来的父编号
对每个 _id in dep_proof_ids:
    断言 _id 在旧池编号集合中，或 _id 为负数哨兵
```

#### 4.4.3 源码精读

精炼请求如何携带谱系（这是 `dep_proof_ids` 的出生点）：

[inference/main.py:L243-L246](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L243-L246)

```python
                for idx in indices[:num_proofs_to_include]:
                    proof, meanscore, score2ratings, self_eval, proof_id = proof_meanscore_ratings_tuples[idx][:5]
                    dep_proof_ids.append(proof_id)
```

以及把它挂到样本上：

[inference/main.py:L275-L279](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L275-L279)

```python
                sample = deepcopy(item)
                sample.update({
                    'messages': msg,
                    'dep_proof_ids': dep_proof_ids
                })
```

下游按证明文本收集谱系（`prepare_proof_refinement` 内，每个证明文本第一次出现时记下它带来的父编号）：

[inference/main.py:L340-L346](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L340-L346)

```python
                if prover_output not in problem2proof2ratings[problem]:
                    problem2proof2ratings[problem][prover_output] = []
                    problem2proof2self_eval[problem][prover_output] = {
                        'self_eval': item.get('self_eval', 'null'),
                        'self_eval_score': item.get('self_eval_score', 0)
                    }
                    problem2proof2dep_proof_ids[problem][prover_output] = item.get('dep_proof_ids', [])
```

入库断言（已在 4.1.3 引用 L211-L218，聚焦这两行）：

[inference/main.py:L216-L217](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L216-L217)

```python
                for _id in record['dep_proof_ids']:
                    assert _id in proof_id_dedup or float(_id) < 0, f"{_id} {len(proof_id_dedup)} {proof_pool_path}"
```

一个精确的细节：断言只对照**旧池**的 `proof_id_dedup`——本轮新分配的编号并没有加进这个集合（L207-L208 只递增计数器）。这意味着「新证明依赖同轮诞生的另一个新证明」会立刻断言失败。这在逻辑上是自洽的：父编号来自上一轮的参考证明，它们必然已经在上一轮入库；同轮互相依赖在因果上不可能发生，断言恰好把这扇门焊死。

入库之后的三个边界行为：

[inference/main.py:L219-L223](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L219-L223)

```python
        if use_old_proofs_for_refinement:
            proof_meanscore_ratings_tuples += old_proof_pool

        if any(record[1] > 0.99999 for record in proof_meanscore_ratings_tuples):
            continue
```

1. **旧池并入候选**：`use_old_proofs_for_refinement=True` 时（`__main__` 里恒为 True，见 [inference/main.py:L438](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L438)），历史证明与新证明一起进入下一轮的选材候选池——注意五元组在此拼接，再次印证 4.3.3 的 `[:5]` 切片设计。
2. **早停**：`record[1] > 0.99999` 即 `meanscore` 近似满分（用 ε 而非 `== 1` 防浮点误差）。检查发生在旧池并入**之后**，所以「历史任何一轮出现过满分证明」都足以让本题永久退出精炼。这是证明池参与测试时算力调度的关键一环（u6-l1 展开）。
3. 最后是主循环的调用点，确认 `round_idx` 的语义：

[inference/main.py:L430-L446](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L430-L446)

```python
                previous_proof_verification_output_path = f"{output_dirname}/proof_verification_R{R - 1}/output.jsonl"
                previous_meta_verification_output_path = f"{output_dirname}/meta_verification_R{R - 1}/output.jsonl"
                prepare_proof_refinement(
                    path=previous_proof_verification_output_path,
                    ...
                    round_idx=R - 1,
                    proof_pool_dirname=proof_pool_dirname,
                    use_old_proofs_for_refinement=True,
```

`round_idx=R-1`：处理的是第 R-1 轮的验证输出，所以池记录的 `round_idx` 标的是**证明产生并受验证的那一轮**，而不是入库这一轮。另外 L445-L446 的 `break` 说明最后一轮（`R = max_rounds+1`）虽不再发起生成，但 `prepare_proof_refinement` 仍会执行——最后一轮的证明同样入库，池最终是完整的。

#### 4.4.4 深入一步：写入语义、幂等性与并行安全

把本讲的所有细节拼起来，可以推出证明池三个非显而易见的系统性质：

- **一次成账（write-once）**：某证明文本一旦入库，后续同文本证明被 L197 直接跳过，其 `meanscore` 与批语**永不更新**。如果重新跑了一轮验证、分数分布变了，池里仍是第一次的账。这是有意为之的取舍——池语义是「该证明首次被评估时的结论」，避免了重跑导致的账本抖动。
- **崩溃幂等**：`prepare_proof_refinement` 只在精炼输入文件不存在时被调用（u4-l1 的文件存在性续跑检查）。假设进程在「池已追加、精炼输入未写出」的窗口内崩溃，重启后重跑本函数：上一轮写入的新证明此时已在旧池中，会被 L197 跳过而不会重复追加——池文件天然幂等，无需额外的日志或锁。
- **并行无锁**：`prepare_proof_refinement` 用 `multiprocessing.Pool` 并行调用本函数（[inference/main.py:L385](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L385)，`_split_jobs(tasks, 50)` 把题目分成约 50 组分发）。由于**每道题独占一个池文件**，不同 worker 进程写的文件互不相交，追加点（文件末尾）也不冲突，整个机制不需要任何文件锁。

#### 4.4.5 代码实践

**实践目标**：从池文件重建证明谱系树，并验证崩溃幂等性。

**操作步骤**（示例代码，复用 4.2.4/4.3.4 的 `proof_pool.py`）：

```python
# lineage.py（示例代码）
import json
from proof_pool import append_proofs   # 4.3.4 已实现

path = "mini_pool/MyTest/MyTest-1.jsonl"
# ……用 4.3.4 的两轮数据先跑出 5 条记录……
records = [json.loads(l) for l in open(path)]
id2rec = {r['proof_id']: r for r in records}

def print_tree(pid, depth=0):
    r = id2rec[pid]
    print("  " * depth + f"[{r['proof_id']}] R{r['round_idx']} meanscore={r['meanscore']}")
    children = [x for x in records if pid in x['dep_proof_ids']]
    for c in sorted(children, key=lambda x: x['proof_id']):
        print_tree(c['proof_id'], depth + 1)

for r in records:
    if not r['dep_proof_ids']:
        print_tree(r['proof_id'])
```

然后做幂等实验：再次调用第 2 轮的 `append_proofs(...)`，对比调用前后池文件的行数与内容（可用 `sha256sum`）。

**需要观察的现象**：谱系树打印出 1 号、2 号两个根节点，4 号挂在 1 号下，5 号挂在 2、3 号下（`dep_proof_ids=[2, 3]` 的多父节点）；重复调用 `append_proofs` 后文件行数保持 5，哈希值不变。

**预期结果**：树结构与 4.3.4 手造数据一致；幂等实验确认同文本证明不重复入库。多父节点（5 号同时依赖 2、3 号）说明谱系是 **DAG（有向无环图）而非树**——这正对应 `n_proofs_to_refine > 1` 时一个精炼请求参考多个证明的情形（run.sh 配置 `n_proofs_to_refine=1`，此时每代单亲；argparse 默认值 4 时会出现多亲）。待本地验证。

#### 4.4.6 小练习与答案

（本模块实践已含大练习，这里补两个概念题。）

**练习 1**：为什么 L217 的断言用 `float(_id) < 0` 而不是 `_id < 0`？

**答案**：防御父编号可能是字符串的情况。池文件经 JSON 序列化，若外部工具曾把 `dep_proof_ids` 写成 `["1", "2"]`，直接 `_id < 0` 会抛 TypeError（str 与 int 不可比较）；`float(_id)` 对 `"1"`、`1` 都能求值。这行断言要在脏数据面前尽量给出明确的 AssertionError 信息（错误串里带了池文件路径与集合大小），而不是含糊的类型错误。

**练习 2**：池记录里存了 `dep_proof_ids`，但旧池记录还原成五元组时丢掉了它。这意味着什么？

**答案**：意味着谱系信息**只写入、不再向上传播**——当 `use_old_proofs_for_refinement=True` 让历史证明重新进入精炼候选时，新精炼请求的 `dep_proof_ids` 只记录它直接参考的那些证明编号（L246），不会把父证明的父证明一并带上。完整的家谱必须回到池文件里按 `proof_id` 递归查表重建（正是 4.4.5 实践做的事）。这把运行时的记录开销压到 O(直接父数)，把 O(全祖先) 的重建留给事后分析，是一个典型的「存储扁平化、查询递归化」设计。

## 5. 综合实践

**任务：把 4.2–4.4 的零件组装成一个「迷你版池维护器」，并模拟一次崩溃恢复。**

1. **准备数据**：手造两道题（`MyTest-1`、`MyTest-2`）各两轮的 `proof2ratings` / `proof2self_eval` / `proof2dep_proof_ids`，第 1 轮每题 3 个证明，第 2 轮每题 2 个精炼证明（`MyTest-2` 的某个精炼证明设计成 `meanscore=1.0`，即 4 次验证全给满分）。
2. **跑入库**：用 `append_proofs` 依次处理两题两轮，得到两个池文件共 10 条记录。检查：两题的 `proof_id` 各自从 1 递增到 5（**编号是按题独立的**，不是全局的——两题都有 1 号证明，分属不同文件）。
3. **验证早停语义**：仿照 L222 写一行 `any(r['meanscore'] > 0.99999 for r in 记录)`，确认 `MyTest-2` 因第 2 轮出现满分证明而应被判「跳过后续精炼」，`MyTest-1` 不跳过。
4. **模拟崩溃恢复**：把 `MyTest-1` 第 2 轮的 `append_proofs` 调用理解为「写池成功但精炼输入没写出」的中间态；重新执行同一次调用，断言池文件行数不变、无重复行——复现 4.4.4 分析的崩溃幂等性。
5. **重建谱系**：用 4.4.5 的 `print_tree` 打印两题的证明家谱，确认 `MyTest-2` 的满分证明挂在你指定的父节点下。

**验收标准**：`MyTest-1` 池 5 行且谱系树有两个根；`MyTest-2` 池 5 行且早停条件为真；重复调用后两个文件的 SHA-256 不变。全程无 API 调用，纯本地可完成（具体运行输出待本地验证）。

## 6. 本讲小结

- 证明池是**一题一文件**的追加式账本：`{proof_pool_dirname}/{source_name}/{problem_idx}.jsonl`，每行一条证明记录，含 `proof / meanscore / score2ratings / self_eval / proof_id / dep_proof_ids / round_idx` 七个字段。
- 读池时用 `proof_dedup`（内容身份）与 `proof_id_dedup`（编号身份）**双重去重集合并做自检断言**；新编号取 `max(proof_id_dedup) + 1`，单调递增、永不复用。
- `meanscore` 是同一证明 \( n \) 次验证评分的均值，`score2ratings` 按分数桶保留每条具体批语；落盘后桶键由 float 变字符串，下游必须 `float(key)` 转回。
- `dep_proof_ids` 在精炼请求出生、经 generate.py 字段透传与验证阶段存活、最终入库，并用断言保证父编号必在旧池中；谱系整体构成一张 DAG，可事后按 `proof_id` 递归重建。
- 池的三个系统性质：**一次成账**（同文本证明不重算分）、**崩溃幂等**（重跑自动跳过已入库证明）、**并行无锁**（每题独占文件，`multiprocessing.Pool` 分题并行）。
- 已知边界：`max(proof_id_dedup)` 遇混入的 `'null'` 字符串会抛 TypeError；`float(_id) < 0` 的负数哨兵编号在当前代码中没有生产者（待确认）。

## 7. 下一步学习建议

本讲把证明「存」进了池；下一讲 **u5-l2 聚合与组合策略** 讲怎么「用」：`_prepare_proof_agg_tasks` 的后半段（L222-L284）如何按 `(meanscore, self_eval_score)` 双键排序取前 `n_best_proofs_to_sample` 个证明、用 `itertools.combinations` 枚举至多 `num_trials` 个组合、按分数桶采样批语拼装精炼摘要。建议带着本讲的两个物件去读：五/六元组切片 `[:5]` 的消费现场，以及 `score2ratings` 的 float 键转换（L247）。之后再进入 u5-l3 看 `prepare_proof_refinement` 如何用 `multiprocessing.Pool` 把本函数并行地跑在全部题目上。
