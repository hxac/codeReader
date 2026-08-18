# 输入与输出数据格式：竞赛题与模型预测

## 1. 本讲目标

上一讲我们建立了「生成器—验证器—元验证器」三角色的宏观认识。本讲把镜头拉近到**数据层**：这个仓库的流水线吃什么、吐什么。学完本讲，你应该能够：

1. 准确说出 `inputs/*.json` 中每道竞赛题的 5 个字段（`id`、`question`、`answer`、`contest`、`problem_idx`）各自的含义与取值方式。
2. 准确说出 `outputs/*.jsonl` 中每条模型预测记录的字段（`question`、`problem_idx`、`model_prediction`），以及 `model_prediction` 内部 `proof`、`average_automatic_rating`、`human_rating` 三个字段分别代表什么。
3. 区分 `.json`（整份文件一个 JSON 数组）与 `.jsonl`（每行一条独立 JSON 记录）两种格式在读写代码上的差异，并理解流水线为什么两种都要支持。
4. 通过 `problem_idx` 把输入与输出关联起来，亲手核对 README 中「Putnam 2024 得 118/120」这类官方成绩。

数据格式是这个仓库最值得先学透的部分：`inference/` 里的全部代码都是围绕这几个字段流转的，字段搞清楚了，后面读 `main.py` 时你会始终知道「手里这条数据长什么样」。

## 2. 前置知识

- **JSON 与 JSONL**：JSON 是通用的结构化文本格式。常规用法是一个文件存一个 JSON 值（通常是数组或对象）；JSONL（JSON Lines）则是**每行存一个独立的 JSON 对象**，整个文件是"行的序列"。JSONL 的好处是可以逐行流式读写、可以 `append` 追加，特别适合流水线中间产物——本仓库的 `outputs/` 和推理过程中的所有 `input.jsonl`/`output.jsonl` 都是这种格式。
- **LaTeX 题面**：`question` 字段里存的是数学竞赛题原文，用 LaTeX 记号书写（例如 `\\( \\Omega \\)` 表示圆的符号、`\\begin{itemize}` 表示列表）。它们是**字符串里的转义序列**，不是渲染后的公式。
- **主键与 join**：输入文件记录"题目"，输出文件记录"模型对某道题的预测"。两份数据要靠一个**双方都有的唯一标识**关联（类似数据库里的主键 / join key）。本仓库的这个标识就是 `problem_idx`。
- **两种"分数"**：这个仓库同时出现"自动评分"（由验证器模型打分，多次取平均）和"人工评分"（按竞赛官方记分制给分）。理解这两条评分线的关系，正是 DeepSeekMath-V2「用验证器当奖励模型」主张在数据上的投影。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [inputs/IMO2025.json](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inputs/IMO2025.json) | IMO 2025 全部 6 道题的输入（JSON 数组），本讲的输入样板 |
| [inputs/CMO2024.json](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inputs/CMO2024.json) | CMO 2024 全部 6 道题的输入，结构与 IMO2025 完全一致 |
| [inputs/CMO2025.json](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inputs/CMO2025.json) / [inputs/Putnam2024.json](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inputs/Putnam2024.json) | 另外两份输入；Putnam 有 12 题（A1–A6、B1–B6），是字段取值差异的最好例子 |
| [outputs/IMO2025.jsonl](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/outputs/IMO2025.jsonl) | 模型在 IMO 2025 上的逐题预测（JSONL），本讲的输出样板 |
| [outputs/README.md](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/outputs/README.md) | 一行许可说明：IMO-ProofBench 两份输出对应的基准来自 Google DeepMind，Apache 2.0 许可 |
| [inference/main.py](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py) | 主循环第 397–428 行是**消费这些字段的地方**，本讲只看这一段，用来回答"这些字段分别被谁用掉了" |
| [inference/utils.py](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/utils.py) | `read_data` 同样实现了 .json/.jsonl 双格式读取，可与 main.py 的写法对照 |

## 4. 核心概念与源码讲解

### 4.1 输入数据：inputs/*.json 的字段结构

#### 4.1.1 概念说明

`inputs/` 目录下有 4 份竞赛输入：`IMO2025.json`（6 题）、`CMO2024.json`（6 题）、`CMO2025.json`（6 题）、`Putnam2024.json`（12 题）。每份文件都是一个 **JSON 数组**，数组里每个元素是一道题。所有题目共用同一套 5 字段结构：

| 字段 | 类型 | 含义 | 取值示例 |
| --- | --- | --- | --- |
| `id` | int | 该文件内的顺序编号，从 1 开始 | `1`、`2`… |
| `question` | str | LaTeX 书写的完整题面 | 见下方源码 |
| `answer` | str | 参考答案；**证明题为字面量字符串 `"null"`** | `"k = 0, 1, 3 for all n"`、`"null"` |
| `contest` | str | 所属竞赛名 | `"IMO2025"`、`"Putnam2024"` |
| `problem_idx` | str | 全局唯一主键，格式为 `{contest}-{题号}` | `"IMO2025-1"`、`"Putnam2024-B5"` |

两个容易混淆的字段要特别区分：

- `id` 只在**单个文件内**唯一——4 份文件的 `id` 都从 1 开始，跨文件必然冲突；
- `problem_idx` 因为拼上了竞赛名，是**跨所有文件唯一**的。这就是为什么后续流水线（证明池、聚合、精炼）一律用 `problem_idx` 而不用 `id` 做主键。

另一个值得注意的细节：`answer` 是**字符串** `"null"`，而不是 JSON 的空值 `null`。区分题型的信号就藏在这里——`"IMO2025-1"` 这类求值题有真实答案串，而 `"IMO2025-2"` 这类证明题答案是 `"null"`。回看上一讲的概念：定理证明没有简短的标准答案可以比对，这正是"最终答案奖励不适用于定理证明、必须依赖验证器打分"在数据格式上的体现。

#### 4.1.2 核心流程

输入文件的消费路径（第 1 轮初始化）：

```text
inputs/*.json（或 .jsonl）
    │  按逗号分隔的 input_paths 逐个加载
    ▼
每条记录注入 source_name = 文件名去扩展名（如 "IMO2025"）
    ▼
用 question 字段渲染 proof_generation 提示词模板
    ▼
注入 messages = [{"role": "user", "content": 提示词}]
    ▼
写入 {output_dirname}/proof_gen_R1/input.jsonl（转为 JSONL 格式）
```

注意：`id`、`answer`、`contest`、`problem_idx` 在这一步**不会被消费**，它们作为元数据随记录一起透传，供后续阶段（人工核对、证明池索引）使用。

#### 4.1.3 源码精读

IMO2025.json 的第一条记录，5 个字段一次看全：

[inputs/IMO2025.json:L2-L8](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inputs/IMO2025.json#L2-L8) —— 这是 IMO 2025第 1 题：`id` 为 1，`question` 是关于"sunny line"的完整 LaTeX 题面，`answer` 给出答案 `"k = 0, 1, 3 for all n"`，`contest` 为 `"IMO2025"`，`problem_idx` 为 `"IMO2025-1"`。

再看一条证明题，观察 `answer` 的特殊取值：

[inputs/IMO2025.json:L9-L15](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inputs/IMO2025.json#L9-L15) —— IMO 2025 第 2 题（几何证明题），第 12 行的 `answer` 是字符串 `"null"`：证明题没有可简写的标准答案，只能靠验证器给证明打分。

Putnam2024 展示了 `problem_idx` 的另一种取值形态：

[inputs/Putnam2024.json:L83-L84](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inputs/Putnam2024.json#L83-L84) —— Putnam 分上午场（A）与下午场（B），所以主键是 `"Putnam2024-B6"` 这样的 `{contest}-{场次}{题号}` 格式，全场 12 题从 `Putnam2024-A1` 排到 `Putnam2024-B6`。

最后是消费这些字段的代码：

[inference/main.py:L404-L407](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L404-L407) —— `input_paths` 按逗号拆分后逐个加载；分支判断文件后缀：`.json` 用 `json.load` 一次性读入整个数组，否则（`.jsonl`）逐行 `json.loads`。这就是流水线**同时兼容两种格式**的入口。

[inference/main.py:L413-L415](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L413-L415) —— 给每条记录注入 `source_name`（取自文件名，如 `"IMO2025"`）。多份输入合并成一个任务列表时，靠它区分记录来自哪个文件。

[inference/main.py:L418-L423](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L418-L423) —— 第 418 行是**唯一真正消费 `question` 的地方**：用它渲染证明生成模板；然后 `item.update` 注入 `messages` 字段。其余字段原样保留在记录里向后透传。

`inference/utils.py` 里有一份功能等价的双格式读取实现，可对照阅读：

[inference/utils.py:L8-L16](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/utils.py#L8-L16) —— `read_data` 同样按后缀分流：`.jsonl` 逐行 `json.loads`，其余用 `json.load` 整读。细节讲解留到 u3-l2，这里只需知道"双格式读取"在本仓库出现了两处。

#### 4.1.4 代码实践

**实践目标**：用脚本确认 4 份输入文件的题目数量与字段清单，验证"所有题目共用 5 字段结构"这一论断。

**操作步骤**（以下为示例代码，不是仓库原有文件）：

```python
# inspect_inputs.py —— 放在仓库根目录运行：python3 inspect_inputs.py
import json

for name in ["IMO2025", "CMO2024", "CMO2025", "Putnam2024"]:
    data = json.load(open(f"inputs/{name}.json", "r"))   # .json：整份一次读入
    all_fields = sorted({key for item in data for key in item})
    print(f"{name}: {len(data)} 题, 字段 = {all_fields}")
    print(f"  problem_idx 示例: {data[0]['problem_idx']} ... {data[-1]['problem_idx']}")
    null_answers = sum(1 for item in data if item["answer"] == "null")
    print(f"  证明题(answer=='null')数量: {null_answers}")
```

**需要观察的现象**：4 份文件是否都输出同一套字段清单；`Putnam2024` 的首尾 `problem_idx` 与其他三份有何不同；各文件证明题占比。

**预期结果**（基于对仓库数据的实际核对）：

```text
IMO2025: 6 题, 字段 = ['answer', 'contest', 'id', 'problem_idx', 'question']
  problem_idx 示例: IMO2025-1 ... IMO2025-6, 证明题 1 道
CMO2024: 6 题, 字段同上, problem_idx: CMO2024-1 ... CMO2024-6, 证明题 4 道
CMO2025: 6 题, 字段同上, problem_idx: CMO2025-1 ... CMO2025-6
Putnam2024: 12 题, 字段同上, problem_idx: Putnam2024-A1 ... Putnam2024-B6
```

其中 IMO2025 只有第 2 题 `answer` 为 `"null"`；CMO2024 有 4 道证明题（第 1、2、3、5 题），第 4、6 题有具体数值答案（如 `"\\sqrt{5}"`）。

#### 4.1.5 小练习与答案

**练习 1**：为什么流水线用 `problem_idx` 而不是 `id` 做跨文件主键？

**答案**：`id` 只在单文件内从 1 递增，`IMO2025.json` 和 `CMO2024.json` 都有 `id=1` 的记录，合并后必然冲突；`problem_idx` 拼接了竞赛名（如 `"IMO2025-1"`、`"Putnam2024-B6"`），在所有输入文件范围内唯一，适合做多源数据合并后的 join key 与证明池文件名。

**练习 2**：`answer` 字段的值 `"null"` 和 JSON 空值 `null` 有什么区别？如何用代码区分一道题是不是证明题？

**答案**：`"null"` 是 4 个字符的字符串（`json.loads` 后得到 Python 的 `str`），而 `null` 会被解析成 Python 的 `None`。区分方式：`item["answer"] == "null"`（注意加引号）判定为证明题。设计成字符串可能是为了让 `answer` 字段类型统一为 `str`，下游不必做类型分支。

**练习 3**：如果把 `inputs/` 下 4 份文件的全部 `problem_idx` 收集到一个 set 里，一共多少个元素？

**答案**：\( 6 + 6 + 6 + 12 = 30 \) 个，且两两不同（竞赛名前缀保证了唯一性）。这个并集就是流水线能处理的全部题目集合。

### 4.2 输出数据：outputs/*.jsonl 的记录结构

#### 4.2.1 概念说明

`outputs/` 目录下有 5 份 JSONL 文件，是 DeepSeekMath-V2 的**逐题最终预测**：`IMO2025.jsonl`、`CMO2024.jsonl`、`Putnam2024.jsonl`（对应 inputs 里的三份竞赛），以及 `IMO-ProofBench-Basic.jsonl`、`IMO-ProofBench-Advanced.jsonl`（在 Google DeepMind 的 IMO-ProofBench 基准上的预测，各 30 条，`outputs/README.md` 注明了该基准的来源与 Apache 2.0 许可）。

每行一条记录，结构如下：

```text
{
  "question":                <str>  原题面（与输入的 question 相同，方便单文件自包含阅读）
  "problem_idx":             <str>  题目主键，与输入一一对应
  "model_prediction": {
    "proof":                 <str>  模型最终产出的完整证明（Markdown + LaTeX）
    "average_automatic_rating": <float> 验证器对这条证明的自动评分均值
    "human_rating":          <int>  人工按竞赛官方记分制给的分数
  }
}
```

三个关键理解点：

1. **输出记录里没有 `contest`、`answer`、`id`**（已实际核对：`outputs/` 全部 5 个文件中不存在这两个键）。要知道一道题的答案或竞赛名，必须拿 `problem_idx` 回 `inputs/*.json` 做 join。`question` 被冗余保留，是为了让输出文件不依赖输入也能独立阅读。
2. **一条记录只有一个最终 `proof`**。流水线内部每题会生成、验证、精炼很多个证明（中间产物不在这里发布），发布的是最终选定的那条——通常对应自动评分最高的证明。
3. **两条评分线并存**：`average_automatic_rating` 是验证器（自动）多次打分的均值，`human_rating` 是人工分。把两者放在一起，正是在数据层面检验上一讲的命题：自动验证器给出的分数与人类专家判定是否一致。

#### 4.2.2 核心流程

一条输出记录的"账本"逻辑：

```text
对某道题 problem_idx:
    流水线多轮 生成 → 验证 → 元验证 → 精炼
    ↓
    选出最终证明 proof                       → model_prediction.proof
    多次自动验证评分取均值                     → model_prediction.average_automatic_rating
    人类专家按该竞赛官方记分制评阅同一份证明     → model_prediction.human_rating
```

`human_rating` 的量纲**随竞赛记分制变化**（可从数据分布直接观察）：

| 竞赛 | 每题满分（观察值） | 观察到的取值 |
| --- | --- | --- |
| IMO 2025 / IMO-ProofBench | 7 | 0、6、7 |
| CMO 2024 | 21 | 0、9、21 |
| Putnam 2024 | 10 | 8、10 |

一个漂亮的交叉验证：`Putnam2024.jsonl` 中 12 题的 `human_rating` 有 11 题 10 分、`Putnam2024-B5` 为 8 分，总和 \( 11 \times 10 + 8 = 118 \)，正好等于 README 第 44 行写的 "a near-perfect 118/120 on Putnam 2024"。同理 IMO 2025 为 \( 5 \times 7 + 0 = 35 \) 分（第 6 题得 0 分），对应 README 中的 "gold-level scores on IMO 2025"。

`average_automatic_rating` 的取值多为 `1.0`，也有 `0.9296875`、`0.7109375`、`0.0234375` 这类分数。它们是**多次** 0/0.5/1 档自动评分的均值（分母未在文件中记录，推测与 `n_verification_per_proof` 等参数有关，待确认）。分母普遍呈 64 的倍数形态（如 \( 0.9296875 = 59.5 / 64 \)），暗示每次发布的验证采样次数相当大——这正是上一讲"扩展验证算力"在数据上的痕迹。

#### 4.2.3 源码精读

[outputs/IMO2025.jsonl:L1](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/outputs/IMO2025.jsonl#L1) —— 整个文件的第 1 行就是一条完整记录（JSONL 一行一记录，这一行约 1.4 万字符）：`question` 是 sunny line 题面原文；`problem_idx` 为 `"IMO2025-1"` 可与输入 join；`model_prediction.proof` 是一段完整的构造+必要性证明（以 `\boxed{\{0,1,3\}}` 收尾）；`average_automatic_rating` 为 `1.0`（验证器均值给满）；`human_rating` 为 `7`（IMO 官方满分）。

对照同题的输入记录 [inputs/IMO2025.json:L2-L8](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inputs/IMO2025.json#L2-L8) —— 输入里的 `question`、`problem_idx` 原样出现在输出里，而输入里的 `answer`（`"k = 0, 1, 3 for all n"`）在输出中不存在：想知道"模型答对了没有"，得自己 join 两份文件比对（证明末尾的 `\boxed{\{0,1,3\}}` 恰与答案一致）。

[outputs/CMO2024.jsonl:L3](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/outputs/CMO2024.jsonl#L3) —— CMO 2024 第 3 题的记录，`average_automatic_rating` 为 `0.9296875` 而 `human_rating` 为 `0`：自动验证器给了接近满分，人工判定却不给分。这条记录是"生成-验证差距"最直观的样例——验证器与人类判定出现分歧时，正是元验证与验证器迭代训练要解决的问题。

[outputs/README.md:L1](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/outputs/README.md#L1) —— 说明 `IMO-ProofBench-Basic/Advanced` 两份输出所用基准来自 Google DeepMind 的 superhuman/imobench，采用 Apache 2.0 许可。注意这两份输出的 `problem_idx` 形如 `"PB-Basic-001"`，**在 `inputs/` 里找不到对应输入文件**——基准题目本身不在本仓库发布范围内，只发布了模型在其上的预测。

还有一个"缺失"值得留意：`inputs/` 里有 `CMO2025.json`（6 题），但 `outputs/` 里**没有** `CMO2025.jsonl`。输入集合与输出集合不是一一对应的，做数据对账时不能假设"每个输入都有输出"。

#### 4.2.4 代码实践

**实践目标**：逐行流式读取 `outputs/IMO2025.jsonl`，统计记录数并检查字段类型，体会 JSONL 的读取方式。

**操作步骤**（示例代码）：

```python
# inspect_outputs.py —— python3 inspect_outputs.py
import json

records = []
with open("outputs/IMO2025.jsonl", "r") as f:
    for line in f:                      # JSONL：逐行读
        if line.strip():
            records.append(json.loads(line))

print(f"共 {len(records)} 条记录")
first = records[0]
for key, value in first.items():
    if key == "question":
        continue                        # 题面太长，跳过
    print(f"  {key}: {type(value).__name__}", end="")
    if isinstance(value, dict):
        print(f" -> {{ {', '.join(f'{k}: {type(v).__name__}' for k, v in value.items())} }}")
    else:
        print(f" (值 = {value})")

total = sum(r["model_prediction"]["human_rating"] for r in records)
print(f"human_rating 总和: {total}")
```

**需要观察的现象**：记录数是否等于输入文件题数；`model_prediction` 是不是 dict；`proof` 是不是 str；`human_rating` 总和。

**预期结果**（已实际核对数据）：共 6 条记录；顶层键为 `question`（str）、`problem_idx`（str）、`model_prediction`（dict → `proof`: str、`average_automatic_rating`: float、`human_rating`: int）；`human_rating` 总和为 35（5 题 7 分 + 第 6 题 0 分）。

#### 4.2.5 小练习与答案

**练习 1**：如何只用 `outputs/Putnam2024.jsonl` 和 `inputs/Putnam2024.json` 两个文件，验证 README 的 "118/120"？

**答案**：逐行读输出文件，对 `model_prediction["human_rating"]` 求和得 118；满分上限按 12 题 × 每题 10 分 = 120。两数相除即 118/120。join 并不需要——`human_rating` 就在输出里；需要 join 的场景是取 `answer`、`contest` 等只存在于输入中的字段。

**练习 2**：`average_automatic_rating = 1.0` 但 `human_rating = 0` 的记录说明什么？在 `outputs/IMO-ProofBench-Advanced.jsonl` 中找出一个这样的例子。

**答案**：说明验证器（自动评分）认为证明完美，而人类专家判零分——验证器被"骗"了，生成-验证差距扩大了。例如 `PB-Advanced-020`（`average_automatic_rating` 为 `1.0`，`human_rating` 为 `0`）。这正是论文主张用元验证器复核低质量评价、并持续用难例训练验证器的动机。

**练习 3**：为什么 `question` 要在输出文件里冗余一份，而 `answer` 却不带上？

**答案**：`question` 让输出文件**自包含**——读者不依赖 inputs 就能看懂证明针对什么题；而 `answer` 若一起发布，评估脚本或读者可能不自觉地"对着答案打分"，且对证明题它本来就是 `"null"`，信息量低。此外输出是"模型视角"的数据，模型解题时不应看到答案，发布格式与之保持一致。（此为合理推断，仓库未明说，待确认。）

### 4.3 .json 与 .jsonl：两种格式的读写差异与选择原因

#### 4.3.1 概念说明

本仓库对两种格式的分工非常清晰：

| 场景 | 格式 | 原因 |
| --- | --- | --- |
| `inputs/*.json`（静态题库） | JSON 数组 | 一次性读入、内容不变、方便人工编辑与整体替换 |
| `outputs/*.jsonl` 与流水线全部中间 `input.jsonl`/`output.jsonl` | JSONL | 需要流式逐条处理；生成过程可能中断，需要**追加写 + 断点续跑**（generate.py 的 `.meta` 机制按批记录完成进度，u2-l2 详述） |

读写代码差异的核心：

```python
# .json：整份文件是一个 JSON 值
data = json.load(open(path))            # 一次读入 → list[dict]
json.dump(data, open(path, "w"))

# .jsonl：每行一个独立 JSON 对象
with open(path) as f:
    items = [json.loads(line) for line in f if line.strip()]
with open(path, "a") as f:              # 追加写是 JSONL 的常规操作
    for item in items:
        print(json.dumps(item), file=f)
```

一个易错点：JSONL 文件**不是合法的单一 JSON 文档**，对它用 `json.load` 会直接抛异常；反过来，把 JSON 数组当 JSONL 逐行解析也必然失败。另外 `json.dumps` 默认不换行，写 JSONL 时一行只 `dumps` 一个对象（main.py 用 `print(json.dumps(item), file=file)` 正是利用 `print` 自动补换行）。

#### 4.3.2 核心流程

第 1 轮初始化时的格式转换全链路：

```text
inputs/IMO2025.json + inputs/CMO2024.json   （.json，json.load 整读）
        │  合并 + 注入 source_name + 渲染 question 为 messages
        ▼
{output_dirname}/proof_gen_R1/input.jsonl    （.jsonl，逐行写出）
        │  generate.py 逐行读、按批分发、逐行追加写
        ▼
{output_dirname}/proof_gen_R1/output.jsonl   （.jsonl）
        │  后续每轮 prepare_* 函数都是 jsonl → jsonl
        ▼
（最终人工评审后整理为 outputs/*.jsonl 发布）
```

#### 4.3.3 源码精读

[inference/main.py:L406-L412](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L406-L412) —— 双格式分支的原文：`.json` 走 `json.load`；否则打开文件逐行 `json.loads` 追加到列表。`input_paths` 支持 `"inputs/IMO2025.json,inputs/CMO2024.json"` 这样的逗号拼接多源输入，因此这段代码天然要兼容两种后缀。

[inference/main.py:L425-L428](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/main.py#L425-L428) —— 写出 JSONL 的代码：`print(json.dumps(item), file=file, flush=True)`，`flush=True` 保证进程被中断时已写行不丢缓冲——这是为断点续跑服务的细节。

[inference/utils.py:L10-L16](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/utils.py#L10-L16) —— `read_data` 的同款双格式实现，供流水线其余阶段复用；紧随其后的 [inference/utils.py:L5-L6](https://github.com/deepseek-ai/DeepSeek-Math-V2/blob/665c840782baf7faae8a8b244ea313f3cfcc346f/inference/utils.py#L5-L6) 的 `hash_problem_idx` 则是为**没有** `problem_idx` 字段的自定义数据兜底：用题面 SHA256 生成稳定主键。换句话说，官方输入里那个 `problem_idx` 字段不是必需品，而是"有了更好"的元数据。

#### 4.3.4 代码实践

**实践目标**：制造一次"格式误用"报错，直观记住两种格式的边界。

**操作步骤**（示例代码）：

```python
# format_trap.py —— python3 format_trap.py
import json

# 错误示范 1：用 json.load 读 JSONL
try:
    json.load(open("outputs/IMO2025.jsonl"))
except json.JSONDecodeError as e:
    print(f"JSONL 不能整读: {e.msg} (第 {e.lineno} 行第 {e.colno} 列)")

# 错误示范 2：把 JSON 数组文件当 JSONL 逐行解析
try:
    [json.loads(line) for line in open("inputs/IMO2025.json")]
except json.JSONDecodeError as e:
    print(f"JSON 不能逐行读: {e.msg} (第 {e.lineno} 行)")
```

**需要观察的现象**：两段各抛出什么异常、报错位置在第几行。

**预期结果**：错误示范 1 在第 2 行报 `Extra data`（第一行解析完后还有内容）；错误示范 2 在第 1 行就报错（`[` 开头的行不是完整 JSON 对象；具体报错信息取决于行内容，`Expecting property name enclosed in double quotes` 或 `Extra data`，待本地验证具体消息文案）。记住结论即可：**整读用 `.json`，逐行读用 `.jsonl`，混用必炸**。

#### 4.3.5 小练习与答案

**练习 1**：给 `main.py` 传 `--input_paths inputs/IMO2025.json,inputs/CMO2024.json` 后，`proof_gen_R1/input.jsonl` 会有几行？`source_name` 各是什么？

**答案**：\( 6 + 6 = 12 \ 行；前 6 行 `source_name == "IMO2025"`，后 6 行 `source_name == "CMO2024"`（取自文件名去扩展名，见 main.py L405）。

**练习 2**：为什么中间产物选 JSONL 而不是 JSON 数组？

**答案**：三个原因：① 生成任务按批分发、可能中途失败，JSONL 支持"已完成几条就写几条"的追加写；② 断点续跑时只需逐行数出已完成记录、配合 `.meta` 里的批次集合即可跳过（u2-l2 展开）；③ 流式处理不必把全部记录一次性载入内存。JSON 数组要做到等价效果就得整文件重写，既慢又不安全。

**练习 3**：写出一段代码，把 `inputs/CMO2024.json` 转成等价的 `CMO2024.jsonl`。

**答案**：

```python
import json
data = json.load(open("inputs/CMO2024.json", "r"))
with open("CMO2024.jsonl", "w") as f:
    for item in data:
        print(json.dumps(item), file=f)   # 或 f.write(json.dumps(item) + "\n")
```

转换后每行是一道题的完整 JSON 对象，`read_data` 与 main.py 的 `.jsonl` 分支都能直接读它。

## 5. 综合实践

**任务：写一个 `reconcile.py` 对账脚本，把本讲三个模块的知识串起来。**

要求实现三件事：

1. **建主键索引**：读取 `inputs/` 下全部 4 份 JSON 文件，收集所有 `problem_idx` 到一个集合；再读取 `outputs/` 下全部 5 份 JSONL，收集输出侧的 `problem_idx` 集合。打印"有输入无输出"的竞赛（预期发现：`CMO2025` 的 6 个主键只出现在输入侧）与"有输出无输入"的主键前缀（预期发现：`PB-Basic-*`、`PB-Advanced-*` 共 60 个）。
2. **join 并对账成绩**：对 `Putnam2024`，把输出按 `problem_idx` 与输入 join，打印每题的 `answer`、`average_automatic_rating`、`human_rating` 三列表格，最后一行输出 `human_rating` 总分（预期 118，满分 120）。
3. **找"验证器误判"样例**：扫描全部 5 份输出，列出 `average_automatic_rating >= 0.9` 且 `human_rating == 0` 的记录的 `problem_idx` 与所在文件（预期至少包含 `IMO2025-6`、`CMO2024-3`、`PB-Advanced-003`、`PB-Advanced-020`，这些就是生成-验证差距的实证样例）。

提示：输入用 `json.load` 整读、输出逐行 `json.loads`；join 用 dict 而不是双层 for；对账结果若与预期不符，优先检查是否漏了 `line.strip()` 处理空行。

**预期产出**：一份能复跑的脚本 + 一段 5 行以内的结论（哪两个集合不对齐、Putnam 总分多少、找到几条误判样例）。这些结论里的每一个数字，你都应该能指着仓库里具体的文件和行说出来源。

## 6. 本讲小结

- 输入 `inputs/*.json` 是 JSON 数组，每题 5 字段：`id`（文件内序号）、`question`（LaTeX 题面，唯一被模板消费的字段）、`answer`（证明题为字符串 `"null"`）、`contest`、`problem_idx`（全局唯一主键）。
- 输出 `outputs/*.jsonl` 每行一条记录：`question`（冗余自包含）、`problem_idx`（与输入 join 的钥匙）、`model_prediction`（内含 `proof`、`average_automatic_rating`、`human_rating`）；输出侧没有 `contest`/`answer`/`id`。
- `human_rating` 采用各竞赛官方记分制（IMO 每题 7 分、CMO 21 分、Putnam 10 分），Putnam 的 \( 11 \times 10 + 8 = 118 \) 与 README 的 "118/120" 精确吻合。
- `average_automatic_rating` 是验证器多次 0/0.5/1 评分的均值，它与 `human_rating` 的分歧记录（如 `CMO2024-3`、`PB-Advanced-020`）就是"生成-验证差距"的数据实证。
- `.json` 整读（`json.load`）、`.jsonl` 逐行读（`json.loads(line)`），main.py 与 utils.py 的 `read_data` 都实现了双格式兼容；中间产物一律用 JSONL 以支持追加写与断点续跑。
- 输入集合与输出集合并不一一对应：`CMO2025.json` 无对应输出，`IMO-ProofBench` 两份输出无对应输入（基准题目不在本仓库发布）。

## 7. 下一步学习建议

数据格式已经烂熟于心，下一讲 **u1-l3《运行方式与参数总览：从 run.sh 到 main.py》** 将让这份数据真正"跑起来"：配置 API Key、理解 `run.sh` 的参数、亲手（小规模）触发第 1 轮 `proof_gen_R1/input.jsonl` 的生成——你会发现本讲 4.1.2 那张消费路径图就是它的执行蓝图。之后进入 u2 单元精读 `generate.py`：JSONL 的逐行读写与断点续跑（本讲埋下的伏笔）将在那里得到完整解释。继续阅读源码时，建议带着本讲的 `reconcile.py` 结论对照：每读到一个新中间文件，就问一句"它的 `problem_idx` 从哪来、`question` 还在不在"。
