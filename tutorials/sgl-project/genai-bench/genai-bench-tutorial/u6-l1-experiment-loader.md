# 实验结果加载 experiment_loader

## 1. 本讲目标

一次 `benchmark` 压测结束后，磁盘上会散落着大量 JSON 文件：一个 `experiment_metadata.json` 加上若干「每个 scenario×concurrency 一份」的 run 结果文件。而负责出报告的 `excel` / `plot` 命令并不关心这些文件叫什么、放在哪，它们只想要一个干净的、按场景和并发组织好的数据结构。

本讲讲解 `genai_bench/analysis/experiment_loader.py` 如何扮演「磁盘 → 内存」的搬运工。学完后你应当能够：

- 说清 `load_one_experiment` 与 `load_multiple_experiments` 各自的输入输出结构。
- 解释 run 数据是如何被索引成 `scenario → concurrency → MetricsData` 的嵌套字典。
- 描述 `filter_criteria` 过滤的两条路径，以及「缺失场景 / 缺失并发级别」告警的触发条件。
- 读懂这一层如何与 `ExperimentMetadata`（u1-l5）、`AggregatedMetrics`（u4-l2）衔接，为后续 u6-l2（Excel）与 u6-l4（绘图）铺路。

## 2. 前置知识

在进入源码前，先建立三个直觉。

**直觉一：一次实验（experiment）= 一张 scenario×concurrency 的网格。**
回顾 u4-l2，一个 **run** 由 `[scenario, concurrency]` 唯一界定；而一次完整实验就是「对所有场景」×「对所有并发档位」跑完全部 run。因此加载实验结果，本质上就是把一堆「以文件为单位的 run」重新拼回这张二维网格。

**直觉二：磁盘上有两类 JSON，结构完全不同。**
- `experiment_metadata.json`：整次实验的「身份证」，对应 `ExperimentMetadata`（u1-l5），记录这次实验跑了哪些场景、哪些并发档位、用了什么后端。
- `*_concurrency_1_time_60s.json` 这类文件：单个 run 的聚合结果，对应 `AggregatedMetrics`（u4-l2），外加一个 `individual_request_metrics` 逐请求明细列表。

加载器要做的，就是把这两类文件分别读成对象，再用 `scenario` 与并发值把它们粘进同一张表。

**直觉三：加载器是「纯读」模块。**
它不跑压测、不算指标、不出图，只负责「把文件读进来、按规则组织、必要时告警」。这让它可以被 `excel` / `plot` 命令反复复用——对一份旧实验目录，想换种报告样式随时重出，而不必重跑压测（见 u1-l4）。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [genai_bench/analysis/experiment_loader.py](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/analysis/experiment_loader.py) | 本讲主角：把实验目录的 JSON 读成 `ExperimentMetadata` 与 `scenario→concurrency` 嵌套结构。 |
| [genai_bench/protocol.py](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/protocol.py) | 定义 `ExperimentMetadata`，是元数据加载的目标类型与过滤字段的来源。 |
| [genai_bench/metrics/metrics.py](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/metrics/metrics.py) | 定义 `AggregatedMetrics` 与 `RequestLevelMetrics`，是 run 数据加载的目标类型。 |
| [tests/analysis/test_experiment_loader.py](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/tests/analysis/test_experiment_loader.py) | 加载器的单元测试，是理解预期行为最可靠的依据。 |
| [tests/analysis/mock_experiment_data.json](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/tests/analysis/mock_experiment_data.json) / [mock_run_data.json](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/tests/analysis/mock_run_data.json) | 仿造的元数据与单 run 数据，本讲代码实践的素材来源。 |

## 4. 核心概念与源码讲解

### 4.1 元数据加载：读取「实验身份证」并按规则过滤

#### 4.1.1 概念说明

进入一个实验目录后，加载器第一件事就是找 `experiment_metadata.json`。这个文件是整次实验的宏观描述：跑了哪些 `traffic_scenario`、哪些 `num_concurrency`（或 `batch_size`）、用的什么后端和模型。

加载时要同时做两件事：把 JSON 反序列化成强类型的 `ExperimentMetadata`，以及（可选地）按 `filter_criteria` 过滤——比如「我只想看 `D(100,100)` 这个场景的结果」。过滤不通过时，这次实验会被整条丢弃（返回 `None`），从而在「加载多份实验」时自然被跳过。

#### 4.1.2 核心流程

`load_experiment_metadata` 的流程是：

1. 打开 JSON 文件，`json.load` 成 dict。
2. `ExperimentMetadata(**data)` 反序列化——Pydantic 会按 u1-l5 讲过的字段约束（如 `conint(ge=1)` 限制并发不小于 1、`Literal` 枚举迭代类型）做校验。
3. 若传了 `filter_criteria`，调用 `apply_filter_to_metadata`；返回 `False` 就记一条 INFO 日志并返回 `None`。
4. 否则返回「（可能已被过滤收窄过的）`ExperimentMetadata`」。

`apply_filter_to_metadata` 对每个过滤键分三类处理：

| 过滤键情况 | 行为 |
| --- | --- |
| 键不在 `ExperimentMetadata.model_fields` 里 | 直接返回 `False`（拼写错误也会被这里挡下） |
| 键是 `traffic_scenario` | 取「元数据场景 ∩ 过滤场景」的交集，原地改写 `traffic_scenario`；交集为空则返回 `False` |
| 其它键 | 用 `getattr` 取值做相等比较，不等则返回 `False` |

注意 `traffic_scenario` 是「列表型」字段，过滤不是「全有或全无」，而是「收窄」——只保留你指定的那几个场景，其余从元数据里剔除。这一点很关键：它让 run 数据加载阶段能据此跳过无关文件。

#### 4.1.3 源码精读

读取并反序列化元数据，不匹配过滤则丢弃：

[genai_bench/analysis/experiment_loader.py:124-150](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/analysis/experiment_loader.py#L124-L150) — 打开 `experiment_metadata.json`，构造 `ExperimentMetadata`；若带过滤且不匹配，记 INFO 日志后返回 `None`。

过滤逻辑的三分支（不存在的键 / `traffic_scenario` 收窄 / 普通相等比较）：

[genai_bench/analysis/experiment_loader.py:153-192](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/analysis/experiment_loader.py#L153-L192) — `apply_filter_to_metadata`：先校验键是否是合法字段，再对 `traffic_scenario` 做交集收窄，其余字段做相等判断；任一不满足即返回 `False`。

`ExperimentMetadata` 的关键字段（过滤键必须命中这里声明的字段名）：

[genai_bench/protocol.py:222-278](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/protocol.py#L222-L278) — `ExperimentMetadata`：注意 `traffic_scenario`、`num_concurrency`、`batch_size`、`iteration_type` 这几个字段，它们既是过滤的常见目标，也是后续一致性检查的依据。

#### 4.1.4 代码实践

**实践目标**：验证 `apply_filter_to_metadata` 对不同过滤键的三种处理，并观察 `traffic_scenario` 的「收窄」语义。

**操作步骤**：仓库已提供 `tests/analysis/mock_experiment_data.json`，其 `traffic_scenario` 含 5 个场景、`num_concurrency` 为 `[1,2,4,8,16]`、`model` 为 `Meta-Llama-3.1-70B-Instruct`。写一段脚本（示例代码，非项目原有）：

```python
# 示例代码：实践元数据过滤
import json
from genai_bench.protocol import ExperimentMetadata
from genai_bench.analysis.experiment_loader import apply_filter_to_metadata

with open("tests/analysis/mock_experiment_data.json") as f:
    md = ExperimentMetadata(**json.load(f))

# (1) 收窄场景：只保留 D(100,100)
ok = apply_filter_to_metadata(md, {"traffic_scenario": ["D(100,100)"]})
print("case1 pass?", ok, "->", md.traffic_scenario)

# (2) 普通字段相等比较
ok = apply_filter_to_metadata(md, {"model": "Meta-Llama-3.1-70B-Instruct"})
print("case2 pass?", ok)

# (3) 不存在的字段（拼写错误 model-name）
ok = apply_filter_to_metadata(md, {"model-name": "Meta-Llama-3.1-70B-Instruct"})
print("case3 pass?", ok)
```

**需要观察的现象**：case1 返回 `True` 且 `md.traffic_scenario` 被原地收窄成只剩 `["D(100,100)"]`；case2 返回 `True`；case3 返回 `False` 并打印 `Filter key model-name is not in the metadata.`。

**预期结果**：与 `test_apply_filter_to_metadata`（[test_experiment_loader.py:213-237](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/tests/analysis/test_experiment_loader.py#L213-L237)）的断言一致。注意 case1 会**修改** `md` 本身，所以三条用例不要共用同一个对象，否则收窄会串味。

#### 4.1.5 小练习与答案

**练习 1**：如果过滤条件是 `{"traffic_scenario": ["D(120,100)"]}`（场景不存在于实验中），返回什么？
**答案**：返回 `False`。`apply_filter_to_metadata` 算出交集为空集，打印 `The scenarios ['D(120,100)'] you want to filter is not presented in your experiments.` 并返回 `False`，对应 [test_experiment_loader.py:219-225](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/tests/analysis/test_experiment_loader.py#L219-L225)。

**练习 2**：为什么 `traffic_scenario` 用交集收窄，而其它字段用相等比较？
**答案**：因为 `traffic_scenario` 是列表（一次实验含多个场景），「过滤」语义是「在这些场景里挑我想要的几个」，所以是集合运算；而 `model`、`server_version` 等是标量字段，一次实验只有一个值，匹配即保留、不匹配即排除，所以用相等。

---

### 4.2 run 数据加载与索引：把散落文件拼回 scenario×concurrency 网格

#### 4.2.1 概念说明

元数据只是「这次实验**打算**跑什么」，真正落盘的指标在 run 文件里。一个 run 文件长这样（命名见 u1-l2，场景串经 `sanitize_string` 清洗）：

```
D100_100_text-to-text_concurrency_1_time_60s.json
└─ {sanitized_scenario}_{task}_{iteration_type}_{value}_time_{seconds}s.json
```

加载器要把这些文件读进来，按「场景 → 并发值」两层索引，组织成下面这个类型别名描述的嵌套结构：

```python
ExperimentMetrics = Dict[
    str,                                   # traffic-scenario
    Dict[int, MetricsData],                # concurrency-level -> 指标数据
]
```

其中每个叶子节点 `MetricsData` 含两块：反序列化后的 `AggregatedMetrics`，以及原始的逐请求明细列表 `individual_request_metrics`。

#### 4.2.2 核心流程

`load_run_data` 处理单个 run 文件，流程是：

1. `json.load` 读取，把 `data["aggregated_metrics"]` 反序列化成 `AggregatedMetrics`。
2. 从中取出 `scenario`（如 `"D(100,100)"`）与迭代类型 / 迭代值（`num_concurrency` 或 `batch_size`）。
3. 若带了 `traffic_scenario` 过滤且当前场景不在过滤集，直接 `return` 跳过该文件——这是过滤的「第二路径」，在文件级生效。
4. 否则把数据写进 `run_data`：
   - 用一个临时的 `set`（键名如 `num_concurrency_levels`）累计「本场景已见哪些并发值」，供后续一致性检查。
   - 用并发值作键写入叶子节点 `{"aggregated_metrics": ..., "individual_request_metrics": ...}`。

注意第 4 步有两个 `setdefault`：第一个建场景字典与「已见并发值集合」，第二个建并发值到指标的映射。这个「已见并发值集合」是临时记账用的，**不会**出现在最终返回的结构里——`load_one_experiment` 用完就把它删掉（见 4.3）。

而 `load_one_experiment` 负责决定「哪些文件算 run 文件」：它用一条正则扫描目录，只挑名字符合 `*.+(...)_(concurrency|batch_size)_\d+_time_\d+s.json` 的文件交给 `load_run_data`。

#### 4.2.3 源码精读

`load_run_data` 的文件级过滤与索引写入：

[genai_bench/analysis/experiment_loader.py:195-239](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/analysis/experiment_loader.py#L195-L239) — 读 run JSON、反序列化 `AggregatedMetrics`、按 `traffic_scenario` 过滤跳过、用 `iteration_value` 作键写入两层嵌套字典，并用 `num_concurrency_levels`/`batch_size_levels` 集合累计已见并发值。

`load_one_experiment` 用正则识别 run 文件并循环调用 `load_run_data`：

[genai_bench/analysis/experiment_loader.py:82-87](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/analysis/experiment_loader.py#L82-L87) — `sorted(os.listdir(...))` 后用正则 `^.+_.+_(?:concurrency|batch_size)_\d+_time_\d+s\.json$` 过滤，匹配的才交给 `load_run_data`（`sorted` 保证多 run 时加载顺序确定）。

run 文件的目标类型 `AggregatedMetrics`（含 `scenario` / `num_concurrency` / `batch_size` / `iteration_type` 等被取键字段）：

[genai_bench/metrics/metrics.py:136-149](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/metrics/metrics.py#L136-L149) — `AggregatedMetrics` 的 run 元数据字段，正是 `load_run_data` 读取 `scenario` 与迭代值的来源。

#### 4.2.4 代码实践

**实践目标**：验证单个 run 文件被正确索引成 `scenario → concurrency` 结构。

**操作步骤**：直接用仓库里的 `mock_run_data.json`（其 `aggregated_metrics.scenario` 为 `"D(100,100)"`、`num_concurrency` 为 `1`），运行（示例代码）：

```python
# 示例代码：加载单个 run 文件
from genai_bench.analysis.experiment_loader import load_run_data

run_data = {}
load_run_data("tests/analysis/mock_run_data.json", run_data, None)

leaf = run_data["D(100,100)"][1]
print("scenario      :", leaf["aggregated_metrics"].scenario)
print("num_completed :", leaf["aggregated_metrics"].num_completed_requests)
print("indiv count   :", len(leaf["individual_request_metrics"]))
```

**需要观察的现象**：`run_data` 顶层键是场景字符串 `"D(100,100)"`，下一层键是并发整数 `1`；叶子里 `aggregated_metrics` 是 `AggregatedMetrics` 对象（`.scenario` 为 `"D(100,100)"`），`individual_request_metrics` 是长度为 2 的列表。

**预期结果**：与 `test_load_run_data`（[test_experiment_loader.py:245-260](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/tests/analysis/test_experiment_loader.py#L245-L260)）断言一致：`"D(100,100)" in run_data`、`1 in run_data["D(100,100)"]`、逐请求明细第 0 条 `num_input_tokens == 92`。注意此时 `run_data["D(100,100)"]` 里还残留一个临时的 `num_concurrency_levels` 集合——单独调用 `load_run_data` 时不会被清理，只有走 `load_one_experiment` 才会删（见 4.3）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `load_run_data` 要用 `setdefault(scenario, {}).setdefault(iteration_key, set())` 累计一个「已见并发值集合」，而不是直接写叶子节点？
**答案**：因为一致性检查（4.3）需要知道「每个场景实际见到了哪些并发值」，才能和元数据里**计划**的并发档位比对、找出缺失。这个集合是给检查用的临时账本，不是最终数据。

**练习 2**：文件级过滤（`traffic_scenario` 不匹配就 `return`）和元数据级过滤（4.1 的收窄）是什么关系？
**答案**：两者配合。元数据级先把 `traffic_scenario` 收窄成「想要的场景」，随后 `load_run_data` 在文件级用同一个过滤集跳过无关 run 文件。如果只收窄元数据却不在文件级过滤，无关 run 仍会被读进来、污染结构。对应 `test_load_run_data_with_filter`（过滤不匹配时 `run_data == {}`，[test_experiment_loader.py:269-279](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/tests/analysis/test_experiment_loader.py#L269-L279)）。

---

### 4.3 一致性检查与缺失告警：编排加载并校验「计划 vs 实际」

#### 4.3.1 概念说明

`load_one_experiment` 是加载层的总编排，它把 4.1（元数据）和 4.2（run 数据）串起来，并多干一件重要的事：**一致性检查**——比对「元数据计划跑的」与「磁盘上实际有的」，发现缺失就告警。

为什么要这一步？因为压测可能中途失败、被中断，或某些 run 文件丢失，导致「计划跑 5 个场景×5 个并发」但「实际只落盘了一部分」。如果不检查，下游报告会基于残缺数据默默出图，得出误导性结论。加载器的做法是：**宁可大声告警，也不静默吞掉缺失**。

它有两类检查：

| 检查 | 触发条件 | 处理 |
| --- | --- | --- |
| 缺失场景 | 元数据 `traffic_scenario` 里有、但 `run_data` 里没有 | 打 warning，并从元数据里**移除**该场景 |
| 缺失并发档位 | 某场景实际见到的并发值 ⊊ 元数据计划的并发值 | 打 warning 列出缺失档位，**不**移除数据 |

而 `load_multiple_experiments` 只是「遍历子目录、对每个子目录调 `load_one_experiment`、收集结果」的薄封装，用于一次处理多个实验目录。

#### 4.3.2 核心流程

`load_one_experiment` 的整体流程：

1. 拼 `experiment_metadata.json` 路径；不存在则返回 `(None, {})`。
2. `load_experiment_metadata` 读元数据（含过滤）；为空则返回 `(None, {})`。
3. 正则扫描 run 文件，逐个 `load_run_data` 填充 `run_data`；若 `run_data` 为空则提前返回（只剩元数据）。
4. **缺失场景检查**：遍历元数据场景，不在 `run_data` 的打 warning 并 `remove`。
5. **缺失并发检查**：算出「计划并发档位集合」（按 `iteration_type` 取 `num_concurrency` 或 `batch_size`），与每个场景「已见并发值集合」做差集，差集非空则 warning；然后删掉临时记账集合。
6. 返回 `(experiment_metadata, run_data)`。

第 5 步有个细节：计划档位来自元数据，而已见档位是 `load_run_data` 写入的临时集合，键名形如 `num_concurrency_levels`。检查完立刻 `del` 掉它，保证最终返回的 `run_data` 叶子里只剩「并发值 → 指标」的干净映射。

#### 4.3.3 源码精读

`load_one_experiment` 的入口与缺失场景检查：

[genai_bench/analysis/experiment_loader.py:56-98](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/analysis/experiment_loader.py#L56-L98) — 读元数据、正则扫描 run 文件、填充 `run_data`；随后遍历元数据场景，缺失的打 warning 并 `traffic_scenario.remove(scenario)`。

缺失并发档位检查与临时集合清理：

[genai_bench/analysis/experiment_loader.py:100-121](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/analysis/experiment_loader.py#L100-L121) — 用字典 `.get(iteration_type, [])` 按迭代类型取计划档位，与每场景已见集合做差集得到 `missing_concurrency`，非空则 warning；最后 `del scenario_data[..._levels]` 清掉临时记账键。

`load_multiple_experiments` 的薄封装：

[genai_bench/analysis/experiment_loader.py:26-53](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/analysis/experiment_loader.py#L26-L53) — 遍历子目录，对每个子目录调 `load_one_experiment`，非空结果（`metadata and run_data`）才收集进列表。

#### 4.3.4 代码实践

**实践目标**：亲手拼一个「残缺」的实验目录，观察两类告警同时触发，并确认返回结构是干净的 `scenario→concurrency`。

**操作步骤**：用仓库里的两个 mock 文件组装一个临时实验目录——只放 1 个 run 文件（`D(100,100)` @ 并发 1），而元数据声明了 5 个场景、并发 `[1,2,4,8,16]`。这会同时触发「缺失 4 个场景」和「`D(100,100)` 缺失并发 `[2,4,8,16]`」两类告警。脚本（示例代码）：

```python
# 示例代码：组装残缺实验目录并加载
import os, shutil, json
from genai_bench.analysis.experiment_loader import load_one_experiment

exp_dir = "/tmp/u6l1_experiment"
os.makedirs(exp_dir, exist_ok=True)
# 1) 元数据
shutil.copy("tests/analysis/mock_experiment_data.json",
            os.path.join(exp_dir, "experiment_metadata.json"))
# 2) 单个 run 文件：名字必须匹配正则 ..._concurrency_<n>_time_<n>s.json
shutil.copy("tests/analysis/mock_run_data.json",
            os.path.join(exp_dir, "D100_100_text-to-text_concurrency_1_time_60s.json"))

metadata, run_data = load_one_experiment(exp_dir)

print("metadata.traffic_scenario:", metadata.traffic_scenario)
for scenario, levels in run_data.items():
    print(f"scenario={scenario!r} concurrency_keys={list(levels.keys())}")
```

**需要观察的现象**：终端先打印若干 `‼️ Scenario ... in metadata but metrics not found!`（缺失场景），再打印 `‼️ Scenario 'D(100,100)' is missing num_concurrency levels: [2, 4, 8, 16].`；随后 `metadata.traffic_scenario` 被收窄成只剩 `['D(100,100)']`；`run_data` 只有一个键 `'D(100,100)'`，其下并发键只有 `1`，且**没有**残留的 `num_concurrency_levels` 临时键。

**预期结果**：告警文本与 `test_load_one_experiment_with_missing_concurrency_levels`（[test_experiment_loader.py:104-169](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/tests/analysis/test_experiment_loader.py#L104-L169)）断言的三条 warning 一致。若想消除「缺失场景」告警，可改成带过滤调用：`load_one_experiment(exp_dir, {"traffic_scenario": ["D(100,100)"]})`，此时元数据先收窄到 `D(100,100)`，缺失场景检查便不再触发（但缺失并发的告警仍在，因为计划并发仍含 `[2,4,8,16]`）。

#### 4.3.5 小练习与答案

**练习 1**：缺失场景会被 `traffic_scenario.remove` 移除，但缺失并发档位却不移除数据，为什么不对称？
**答案**：缺失场景意味着「该场景一行数据都没有」，留在元数据里只会让下游报告去找一个不存在的键、报错或画空图，所以直接移除最干净；而缺失并发只是「该场景部分档位没跑到」，已有的档位数据仍然有效，应当保留供报告使用，只需用 warning 提醒用户「数据不全」。

**练习 2**：`load_multiple_experiments` 里为什么用 `if metadata and run_data:` 过滤？
**答案**：`load_one_experiment` 在「无元数据文件」「元数据被过滤掉」「没有 run 文件」等情况下会返回 `(None, {})` 或 `(metadata, {})`。用 `if metadata and run_data:` 能把这些「空壳」实验剔除，避免把没有实际指标的结果交给下游报告。

---

## 5. 综合实践

把本讲三个最小模块串起来，完成一次「端到端加载 + 报告就绪」的演练。

**任务**：基于 `tests/analysis/mock_experiment_data.json` 与 `mock_run_data.json`，构造一个**完整**的实验目录，让 `load_one_experiment` **零告警**地返回，并验证返回结构可直接喂给下游。

**步骤**：

1. 复制 `mock_experiment_data.json` 为 `experiment_metadata.json`。该元数据声明了 5 个场景、`num_concurrency=[1,2,4,8,16]`。
2. 为了零告警，最省力的做法是**收窄元数据**而非补齐 25 个 run 文件：把 `mock_experiment_data.json` 里的 `traffic_scenario` 改成只含 `["D(100,100)"]`、`num_concurrency` 改成 `[1]`。
3. 复制 `mock_run_data.json` 为 `D100_100_text-to-text_concurrency_1_time_60s.json`（名字须匹配正则）。
4. 调用 `metadata, run_data = load_one_experiment(exp_dir)`，确认**无 warning**，且 `run_data["D(100,100)"][1]["aggregated_metrics"]` 是一个 `AggregatedMetrics`、`individual_request_metrics` 是 2 条明细。
5.（进阶）模仿下游报告的取值方式：打印 `run_data["D(100,100)"][1]["aggregated_metrics"]` 上的顶层标量字段，例如 `requests_per_second`、`mean_output_throughput_tokens_per_s`，以及 `stats.ttft.p99` 这类嵌套统计，体会「加载层产出的结构 = 报告层的输入」这一承接关系（具体取值路径见 u6-l2/u6-l4）。

**预期结果**：步骤 4 零告警、结构干净（叶子里没有 `num_concurrency_levels` 残留）。这个练习验证了加载层的核心承诺——**把磁盘上任意命名、任意数量的 JSON，归一成一张确定性的 scenario×concurrency 表**，这正是 `excel`/`plot` 命令能对任意旧实验重出报告的基础。

## 6. 本讲小结

- 加载器是「纯读」模块，把实验目录的两类 JSON（`experiment_metadata.json` 与各 run JSON）读成内存对象，不跑压测、不算指标。
- 输出结构是 `scenario(str) → concurrency(int) → {aggregated_metrics, individual_request_metrics}` 的两层嵌套字典，叶子里的 `aggregated_metrics` 是已反序列化的 `AggregatedMetrics`。
- 过滤分两条路径：元数据级 `apply_filter_to_metadata`（`traffic_scenario` 做交集**收窄**、其它字段做相等比较），文件级 `load_run_data` 据此跳过无关 run。
- 一致性检查有两类告警：缺失场景（warning + 从元数据移除）、缺失并发档位（warning 但保留已有数据）。
- `load_multiple_experiments` 是「遍历子目录逐个加载」的薄封装，用 `if metadata and run_data` 剔除空壳实验。
- 加载层产出的结构，正是 u6-l2（Excel 报告）与 u6-l4（绘图报告）的统一输入，让报告可以脱离压测、随时重出。

## 7. 下一步学习建议

加载层只负责「读」，下一步看「写」与「画」：

- **u6-l2 Excel 报告生成**：看 `analysis/excel_report.py` 如何遍历本讲产出的 `run_data`、按场景与并发填进 Excel 的多个 sheet，并理解 `SCENARIO_MAP` 与定价信息的作用。
- **u6-l4 灵活绘图与 plot 命令**：看 `flexible_plot_report.py` 如何按 `group_key` 对同一份 `run_data` 分组绘图，承接本讲的索引结构。

阅读源码时，建议带着一个问题：「下游取值时为什么能直接用 `run_data[scenario][concurrency]["aggregated_metrics"].stats.xxx`？」——答案就在本讲建立的两层索引里。若想反过来理解 run JSON 是怎么落盘的，可回看 u4-l2 的 `AggregatedMetricsCollector.save`。
