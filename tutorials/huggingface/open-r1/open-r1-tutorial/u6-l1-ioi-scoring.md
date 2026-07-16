# IOI 评分系统

## 1. 本讲目标

本讲深入 open-r1 的「竞赛编程」评分子系统中的 **IOI 评分模块**。学完后你应当能够：

- 说清 `TestResult` 与 `SubtaskResult` 两个数据结构的字段含义，以及子任务打分公式 `score = min(各测试分)`、`weighted_score = min × points`。
- 理解 `status` 属性的「最差状态优先」判定规则与状态优先级表。
- 手动跟踪 `score_subtask` 的「分批并行 + 失败早停」流程，解释为什么早停不会改变分数、却能省下大量沙箱调用。
- 理解 `run_submission` / `execute_ioi` 如何把一段 C++ 代码送进 Piston 沙箱、并从 stdout/stderr 中解析出 `(score, feedback)`。
- 看懂 `ioi_code_reward` 如何把上述零件串联成一条「模型回答 → 奖励分数」的奖励函数，供 GRPO 使用。

本讲是专家层「竞赛编程评分」单元的第一篇，承接 u5-l1（代码奖励函数与执行脚本模板）。u5-l1 讲的是「把单段 Python 代码塞进 E2B/Morph 沙箱跑测试用例」的轻量判分；本讲则升级到「IOI 竞赛题：多子任务、多测试点、带 grader、跑在 Piston 上」的重量级判分。

## 2. 前置知识

在进入源码前，先用通俗语言建立几个概念。

**什么是 IOI 题目？** 国际信息学奥林匹克（IOI）的题目通常不是「输入一组数据、输出一个答案」那么简单。一道题往往带有：

- 一段官方 **grader**（评分器）：一个 `graders/<题目>.cpp` 文件，负责读入、调用选手提交的函数、输出结果。
- 多个 **子任务（subtask）**：题目把测试点分成几组，每组有自己的满分点数（`points`）。
- 每个子任务里有多个 **测试点（test case）**，每个测试点是一对 `(test_input, test_output)`。

**什么是「子任务级评分」？** IOI 的常见约定是「全有或全无（all-or-nothing）」：一个子任务里的**所有**测试点都通过，才拿到该子任务的满分；只要有任何一个测试点没过，整个子任务得 0 分。这正是本讲代码用 `min`（取最小值）来算分的原因——一个 0 分会把整组拉到 0。

**什么是 Piston？** Piston 是一个开源的代码执行引擎（兼容 Engine API），可以在隔离容器里编译并运行用户代码。open-r1 用自建的 Piston worker 集群来批量跑 IOI 提交。本讲只需把 `PistonClient` 当作一个「能把代码送进沙箱并返回执行结果」的异步客户端。

**几个状态码缩写**（竞赛评判通用）：

| 缩写 | 含义 |
|------|------|
| AC | Accepted，完全正确 |
| PA | Partially Accepted，部分正确 |
| WA | Wrong Answer，答案错误 |
| TLE | Time Limit Exceeded，超时 |
| MLE | Memory Limit Exceeded，超内存 |
| RE | Runtime Error，运行时崩溃 |
| CE | Compilation Error，编译错误 |
| SKIPPED | 跳过（未执行） |

**为什么这一切是「奖励函数」？** 在 GRPO 强化学习里，奖励函数负责给模型生成的每个回答打一个分数。对代码题而言，最自然的分数就是「这版代码在测试点上通过了多少」。IOI 评分系统就是把这个判分流程封装成奖励信号，让模型在 GRPO 训练中学会写竞赛代码。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [ioi_scoring.py](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/utils/competitive_programming/ioi_scoring.py) | **本讲主角**。定义 `TestResult`/`SubtaskResult` 数据结构、`score_subtask` 分批早停、`run_submission`/`execute_ioi` 沙箱执行与反馈解析。 |
| [utils.py](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/utils/competitive_programming/utils.py) | 提供 `batched` 工具函数，把测试点切成一批一批。 |
| [ioi_utils.py](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/utils/competitive_programming/ioi_utils.py) | `load_ioi_tests` 从 Hub 加载测试点；`add_includes` 给选手代码补头文件。 |
| [piston_client.py](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/utils/competitive_programming/piston_client.py) | `PistonClient`（多端点负载均衡）与 `PistonError`。本讲当作黑盒。 |
| [\_\_init\_\_.py](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/utils/competitive_programming/__init__.py) | 子包导出口，决定外部能 `import` 哪些名字。 |
| [rewards.py](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/rewards.py) | `ioi_code_reward` 把评分零件组装成奖励函数，并注册到奖励注册表。 |

## 4. 核心概念与源码讲解

### 4.1 数据结构：TestResult 与 SubtaskResult

#### 4.1.1 概念说明

IOI 评分需要两级结果：

- **`TestResult`**：一个测试点跑完后的结果——叫什么名字、得了多少分（0.0~1.0）、是什么状态、有什么反馈信息。
- **`SubtaskResult`**：一个子任务（含若干测试点）的整体结果——以及由所有测试点**派生**出来的总分、加权分、总体状态。

关键设计：`SubtaskResult` 的 `score`、`weighted_score`、`status` 都是 **`@property`（派生属性）**，不是存死的字段。它们始终根据 `test_results` 列表实时计算。这意味着「早停」留下的未执行测试点，会自然地参与最终打分。

#### 4.1.2 核心流程

设一个子任务有测试点 \(t_1, t_2, \dots, t_n\)，每个测试点得分 \(s_t \in [0, 1]\)，子任务满分点数 \(P\)，精度 \(k\)。则：

\[
\text{score} = \text{round}\Big(\min_{t} s_t,\ k\Big)
\]

\[
\text{weighted\_score} = \text{round}\Big(\min_{t} s_t \times P,\ k\Big)
\]

\[
\text{status} = \arg\min_{t}\ \text{prio}(\text{status}_t)
\]

其中状态优先级表（值越小越「坏」）为：

| 状态 | 优先级值 | 严重程度 |
|------|---------|---------|
| CE | -1 | 最坏（编译都过不了） |
| RE | 0 | |
| WA | 1 | |
| MLE | 2 | |
| TLE | 3 | |
| PA | 4 | |
| AC | 5 | 最好（完全正确） |
| SKIPPED | 999 | 仅当全部未执行时才出现 |

三条派生规则的直觉：

1. **分数取最小值**：实现「全有或全无」。一个 0.0 分的测试点把整个子任务拉到 0。
2. **加权分 = 最小分 × 满分点数**：把 0~1 的归一化分数换算成实际点数。
3. **状态取最差**：一个子任务只要有一个测试点 WA，整体状态就是 WA；只有全部 AC 才是 AC。

#### 4.1.3 源码精读

`TestResult` 是一个普通 dataclass，默认值很有讲究：`score=0.0`、`status="SKIPPED"`——这正是早停时未执行测试点的初始形态。

[ioi_scoring.py:L10-L25](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/utils/competitive_programming/ioi_scoring.py#L10-L25) —— 这段定义了单个测试点的结果对象。注意默认 `status="SKIPPED"`。

`SubtaskResult` 的字段部分只存「原始数据」，三个派生属性在下面：

[ioi_scoring.py:L28-L47](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/utils/competitive_programming/ioi_scoring.py#L28-L47) —— 子任务结果只存 `points`、`score_precision`、`test_results` 等原始字段。

[ioi_scoring.py:L49-L59](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/utils/competitive_programming/ioi_scoring.py#L49-L59) —— `status` 属性用 `min(..., key=prio)` 取最差状态。`SKIPPED` 的优先级 999 最大，保证只要有一个测试点真跑了，它的状态就会盖过未执行的。

[ioi_scoring.py:L61-L73](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/utils/competitive_programming/ioi_scoring.py#L61-L73) —— `score` 属性对**全部** `test_results`（含 SKIPPED）取 `min`。这就是「全有或全无」的代码实现。

[ioi_scoring.py:L75-L89](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/utils/competitive_programming/ioi_scoring.py#L75-L89) —— `weighted_score` 在 `min` 基础上乘以 `points`，得到实际点数。

注意三处都有 `0 if not self.test_results else ...` 的保护：测试点列表为空时直接返回 0。

#### 4.1.4 代码实践

**实践目标**：在内存里手工拼出 `SubtaskResult`，验证三条派生规则。

**操作步骤**：保存下面这段脚本为 `ioi_ds_demo.py` 并运行（不需要沙箱、不需要 GPU）：

```python
# 示例代码：演示 SubtaskResult 的派生属性
from open_r1.utils.competitive_programming.ioi_scoring import SubtaskResult, TestResult

# 子任务：满分 30 分，3 个测试点
r = SubtaskResult(problem="mosaic", subtask="s1", points=30, score_precision=2)

# 情况 A：3 个全 AC
r.test_results = [TestResult("t1", 1.0, "AC"), TestResult("t2", 1.0, "AC"), TestResult("t3", 1.0, "AC")]
print("A:", r.score, r.weighted_score, r.status)  # 1.0 30.0 AC

# 情况 B：第 2 个 WA，其余 AC
r.test_results = [TestResult("t1", 1.0, "AC"), TestResult("t2", 0.0, "WA"), TestResult("t3", 1.0, "AC")]
print("B:", r.score, r.weighted_score, r.status)  # 0.0 0.0 WA

# 情况 C：第 1 个跑了且失败，后两个未执行(SKIPPED)
r.test_results = [TestResult("t1", 0.0, "WA"), TestResult("t2"), TestResult("t3")]
print("C:", r.score, r.weighted_score, r.status)  # 0.0 0.0 WA
```

**需要观察的现象**：

- 情况 A 得满分：`score=1.0, weighted_score=30.0, status=AC`。
- 情况 B 因一个 WA 被拉到 0：`score=0.0, weighted_score=0.0, status=WA`。
- 情况 C 两个测试点是默认 SKIPPED（score=0.0），仍然把整体拉到 0。

**预期结果**：三行输出分别为 `A: 1.0 30.0 AC`、`B: 0.0 0.0 WA`、`C: 0.0 0.0 WA`。情况 B 与 C 的分数相同，说明早停不会让分数变高——这一点在 4.2 会再次用到。

#### 4.1.5 小练习与答案

**练习 1**：一个子任务 4 个测试点得分是 `[1.0, 0.5, 1.0, 1.0]`，满分 100，精度 2。`score`、`weighted_score`、`status` 各是多少？

**答案**：`score = min = 0.5`；`weighted_score = 0.5 × 100 = 50.0`；状态最差为 PA（0.5 不是 0 也不是 1，对应 PA）。

**练习 2**：若一个子任务的 `test_results` 为空列表，`score` 返回什么？为什么代码要特判？

**答案**：返回 `0`（注意是整数 0，不是 0.0）。特判是为了避免对空列表调用 `min` 抛 `ValueError`。实际场景中这对应「提交抽不出代码、整个子任务被跳过」，由 4.2 的早返回触发。

---

### 4.2 score_subtask：分批并行与失败早停

#### 4.2.1 概念说明

一道 IOI 题的某个子任务可能有几十甚至上百个测试点。如果一个错误答案（比如编译都过不了）也要把全部测试点跑一遍，会白白浪费上百次沙箱调用。`score_subtask` 解决两个问题：

1. **分批并行（batched parallel）**：把测试点切成一批一批，每批用 `asyncio.gather` 并发执行，控制对沙箱集群的并发压力。
2. **失败早停（early stop）**：每跑完一批，只要批里**任何一个**测试点得 0 分，就立即停止后续批次——因为按 `min` 规则，子任务分数注定是 0，再跑也是浪费。

此外它还支持**测试点缓存**：同一次评估里，若某测试点在多个子任务间共享，只跑一次。

#### 4.2.2 核心流程

```
输入: subtask(配置), submission(代码), test_case_run_cache(缓存), test_batch_size(批大小)
1. 初始化 SubtaskResult，test_results 先全部填默认 SKIPPED
2. 计算待跑列表 tests_to_run = 不在缓存里的测试点
3. 把缓存里已有的结果填回 test_results
4. 若 submission 为空，或已有缓存的失败结果 -> 直接返回(早返回)
5. 取出 test_cases(优先 subtask 自带，否则远程 load_ioi_tests)
6. for 每一批 tests_to_run(按 test_batch_size 切):
       a. asyncio.gather 并发跑这一批
       b. 把结果写回 test_results 与缓存
       c. 若这一批有任何 score==0.0 -> break (早停)
7. 返回 SubtaskResult(分数由 @property 实时算出)
```

关键洞察：因为未执行的测试点保持 SKIPPED（score=0.0），而 `score = min(全部)`，所以**早停不会让分数虚高**——一旦有 0 分，最终分数本就是 0。早停纯粹是省算力的优化。

#### 4.2.3 源码精读

先看初始化与待跑列表的构造：

[ioi_scoring.py:L185-L198](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/utils/competitive_programming/ioi_scoring.py#L185-L198) —— 这里建出空的 `SubtaskResult`，并筛出 `tests_to_run`（跳过已在缓存里的测试点）。

[ioi_scoring.py:L201-L213](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/utils/competitive_programming/ioi_scoring.py#L201-L213) —— 先把 `test_results` 全部填成「缓存命中」或「默认 SKIPPED」；随后是两类早返回：代码为空（抽不出代码）、或缓存里已有失败结果。这两类都不必再跑任何沙箱。

[ioi_scoring.py:L215-L220](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/utils/competitive_programming/ioi_scoring.py#L215-L220) —— 取测试点：`subtask["test_cases"]` 优先（支持 list 或 dict 两种形态），否则从 Hub 用 `load_ioi_tests(year, id)` 远程加载。

核心的分批 + 早停循环：

[ioi_scoring.py:L223-L241](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/utils/competitive_programming/ioi_scoring.py#L223-L241) —— 外层 `for` 用 `batched(tests_to_run, test_batch_size)` 逐批迭代；每批 `asyncio.gather` 并发；写回结果后，`if any(test_result.score == 0.0 ...): break` 就是早停。

分批工具来自：

[utils.py:L4-L11](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/utils/competitive_programming/utils.py#L4-L11) —— `batched` 把可迭代对象切成定长列表，最后一批可能更短。`n < 1` 时直接返回原可迭代对象（注意：实践中 `test_batch_size` 应取 `>= 1`；要让所有测试点一批跑完，可把批大小设成不小于测试点总数）。

多个子任务的编排（含跨子任务缓存）：

[ioi_scoring.py:L246-L264](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/utils/competitive_programming/ioi_scoring.py#L246-L264) —— `score_subtasks` 维护一个共享 `test_case_run_cache`，逐个子任务调用 `score_subtask`，避免重跑跨子任务共享的测试点。`skip_mode` 直接当 `test_batch_size` 用：`True`（默认）即逐个跑、失败即停，适合大批量评估。

#### 4.2.4 代码实践

**实践目标**：构造 2 个子任务、每个 3 个测试点的假 `problem_data`，通过「假装」沙箱（mock 掉 `score_single_test_case`）来观察早停行为，并手算「第 1 个测试点失败」时的最终分数。

**操作步骤**：保存为 `ioi_early_stop_sim.py`（无需 Piston、无需 GPU，本地可跑）：

```python
# 示例代码：模拟 score_subtask 的早停（mock 掉沙箱执行）
import asyncio
from open_r1.utils.competitive_programming import ioi_scoring
from open_r1.utils.competitive_programming.ioi_scoring import TestResult

RUN_LOG = []  # 记录被真实"执行"的测试点，用来证明早停

def make_subtask(name, names):
    return {
        "id": "mosaic", "subtask": name, "score": 30, "score_precision": 2,
        "test_names": names,
        "test_cases": {n: ("in", "out") for n in names},
        "grader_files": [], "time_limit": 1, "memory_limit": 256,
    }

S1 = make_subtask("s1", ["1-1", "1-2", "1-3"])
S2 = make_subtask("s2", ["2-1", "2-2", "2-3"])
FAIL_FIRST = {"1-1"}  # 子任务1 的第1个测试点失败，其余通过

# 签名需与真实 score_single_test_case 调用处一致：
# score_single_test_case(client, subtask, test_name, test_input, test_output, submission)
async def fake(client, subtask, test_name, test_input, test_output, submission):
    RUN_LOG.append((subtask["subtask"], test_name))
    if test_name in FAIL_FIRST:
        return TestResult(test_name, 0.0, "WA", "Output isn't correct")
    return TestResult(test_name, 1.0, "AC", "")

ioi_scoring.score_single_test_case = fake  # 绕过 Piston

async def main():
    RUN_LOG.clear()
    r1 = await ioi_scoring.score_subtask(None, S1, "int main(){}", test_batch_size=1)
    print("子任务1 实际执行:", RUN_LOG)                       # 只应有 [('s1','1-1')]
    print("结果:", [(t.test_name, t.score, t.status) for t in r1.test_results])
    print("score=%s weighted=%s status=%s" % (r1.score, r1.weighted_score, r1.status))

    RUN_LOG.clear()
    r2 = await ioi_scoring.score_subtask(None, S2, "int main(){}", test_batch_size=1)
    print("子任务2 实际执行:", RUN_LOG)                       # 应有全部 3 个
    print("score=%s weighted=%s status=%s" % (r2.score, r2.weighted_score, r2.status))

asyncio.run(main())
```

**需要观察的现象**：

- 子任务 1：`RUN_LOG` 只有 `('s1', '1-1')` 一个——第 1 个测试点失败后立即 `break`，`1-2`、`1-3` 没有被「执行」。
- 子任务 1 的 `test_results` 里 `1-2`、`1-3` 仍是 `(0.0, SKIPPED)`，`score=0.0`、`status=WA`。
- 子任务 2：全部通过，3 个测试点都跑了，`score=1.0`、`weighted_score=30.0`、`status=AC`。

**手算「第 1 个测试点失败时」的最终分数**：批大小为 1 时，第 1 批只跑 `1-1` 并得到 0.0，触发 `break`；`1-2`、`1-3` 保持 SKIPPED（0.0）。于是

\[
\text{score} = \min(0.0,\ 0.0,\ 0.0) = 0.0,\quad \text{weighted\_score} = 0.0 \times 30 = 0.0
\]

**预期结果**：子任务 1 输出 `score=0.0 weighted=0.0 status=WA`，且只发生 1 次「执行」；子任务 2 输出 `score=1.0 weighted=30.0 status=AC`，发生 3 次「执行」。若把 `test_batch_size` 改成 3，子任务 1 的 3 个测试点会在同一批并发跑完，分数仍是 0.0，但「执行」次数变成 3——这正是批大小在「省算力」与「降延迟」之间的取舍。

#### 4.2.5 小练习与答案

**练习 1**：若把上面模拟中 `test_batch_size` 从 1 改成 2，子任务 1（3 个测试点，第 1 个失败）会发生几次沙箱「执行」？分数是多少？

**答案**：第 1 批跑 `1-1, 1-2` 两个（2 次执行），其中 `1-1` 得 0.0 触发早停，`1-3` 不再执行。共 2 次执行。分数仍为 0.0。

**练习 2**：为什么早停是「安全」的——不会因为少跑了测试点而把分数算高？

**答案**：未执行的测试点保持默认 `TestResult(score=0.0, status="SKIPPED")`，而 `score = min(全部 test_results)`。一旦有测试点得 0.0（无论失败还是 SKIPPED），`min` 结果必为 0.0，与「全部跑完」的结果一致。早停只省算力、不改分数。

---

### 4.3 沙箱执行：run_submission / execute_ioi 与反馈解析

#### 4.3.1 概念说明

4.2 里的 `score_single_test_case` 调用了 `run_submission`，本节打开这个黑盒。它负责把「一段 C++ 代码 + 一个测试点的输入/期望输出 + grader 文件」打包成 Piston 能理解的请求，再把 Piston 返回的执行结果解析成 `(score, feedback)`。

IOI 题与普通「跑脚本」题最大的不同：评分逻辑写在**官方 grader**里。grader 是一段在沙箱里运行的 C++/Python 脚本，它读入 `input.txt`、调用选手代码、把**分数打印到 stdout**、把**反馈打印到 stderr**。所以 `execute_ioi` 的核心就是「相信 stdout 里的那个数字」。

#### 4.3.2 核心流程

`run_submission` 的组装：

```
data = {
  files: [
    graders/<题目id>.cpp = submission(选手代码),
    input.txt            = test_input,
    correct_output.txt   = test_output(可选),
    ... grader_files(官方评分器各文件),
  ],
  run_timeout       = (time_limit + 3) * 1000 毫秒,  # +3 秒硬上限
  run_memory_limit  = memory_limit,
}
-> execute_ioi(client, data) -> (score, feedback)
```

`execute_ioi` 的解析决策树：

```
response = client.send_execute(data)
1. response 含 "message"        -> raise PistonError(服务级错误)
2. compile.code != 0            -> ("0", "Compilation error ...")   # 编译失败
3. response 不含 "run"          -> raise PistonError(异常响应)
4. run.code==1 且 stderr 含 MemoryError -> ("0", "Memory limit exceeded")
5. run.stdout 非空              -> (stdout, stderr)                 # 正常: stdout 是分数
6. run.signal == "SIGKILL"      -> ("0", "Time limit exceeded")     # 被强杀=超时
7. run.code != 0(其它)          -> raise PistonError(未知崩溃)
8. 兜底                         -> ("0", "Unknown error")
```

拿到 `(score, feedback)` 后，`score_single_test_case` 再用 `_extract_single_status` 把它翻译成状态码：score=0.0 时按 feedback 子串判 CE/MLE/TLE/WA/RE，score=1.0 是 AC，其余是 PA。

#### 4.3.3 源码精读

`run_submission` 负责打包，关键是文件命名约定（`graders/<id>.cpp`）和超时换算（题目 `time_limit` 秒 + 3 秒硬上限，再 ×1000 转毫秒）：

[ioi_scoring.py:L267-L299](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/utils/competitive_programming/ioi_scoring.py#L267-L299) —— 注意 `correct_output.txt` 仅在 `test_output` 非空时附带；`grader_files` 里 `content` 为空的会被 `if content` 过滤掉。

`execute_ioi` 是决策树本体，按「服务错误 → 编译错误 → 超内存 → 正常 → 超时 → 其它」逐层判定：

[ioi_scoring.py:L302-L335](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/utils/competitive_programming/ioi_scoring.py#L302-L335) —— 这里把 Piston 响应翻译成 `(score, feedback)`。第 5 步「stdout 非空即正常」是关键：IOI grader 把分数印在 stdout。

`_extract_single_status` 把分数+反馈再压成单一状态码：

[ioi_scoring.py:L110-L135](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/utils/competitive_programming/ioi_scoring.py#L110-L135) —— `score==0.0` 时靠 feedback 里的关键子串（`"Compilation error"`、`"Memory limit exceeded"`、`"Time limit exceeded"`、`"Output isn't correct"`）区分错误类型，其余一律归为 RE。

`score_single_test_case` 把上述两者粘起来，产出 `TestResult`：

[ioi_scoring.py:L138-L161](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/utils/competitive_programming/ioi_scoring.py#L138-L161) —— 调 `run_submission` 拿 `(score, feedback)`，再 `_extract_single_status` 得状态，组装成 `TestResult`。

#### 4.3.4 代码实践

**实践目标**：不连真实 Piston，仅用伪造的 `client` 单测 `execute_ioi` 的反馈解析逻辑，验证「stdout 里的数字会被当成分数」「SIGKILL 会被判成 TLE」。

**操作步骤**：保存为 `ioi_execute_sim.py`，本地可跑：

```python
# 示例代码：用假 client 验证 execute_ioi 的解析逻辑
import asyncio
from open_r1.utils.competitive_programming.ioi_scoring import execute_ioi
from open_r1.utils.competitive_programming.piston_client import PistonError

class FakeClient:
    def __init__(self, resp): self.resp = resp
    async def send_execute(self, data): return self.resp

async def try_case(resp):
    try:
        return await execute_ioi(FakeClient(resp), data={})
    except PistonError as e:
        return ("PistonError", str(e))

async def main():
    # 正常: grader 把 1.0 印在 stdout
    print(await try_case({"run": {"code": 0, "stdout": "1.0", "stderr": "", "signal": None}}))  # ('1.0','')
    # 超时: 被 SIGKILL
    print(await try_case({"run": {"code": 137, "stdout": "", "stderr": "", "signal": "SIGKILL"}}))  # ('0','Time limit exceeded')
    # 编译失败
    print(await try_case({"compile": {"code": 1, "stderr": "boom"}, "run": {"code": 0, "stdout": "", "stderr": "", "signal": None}}))  # ('0','Compilation error ...')

asyncio.run(main())
```

**需要观察的现象**：

- 正常情况下返回 `('1.0', '')`——stdout 里的 `"1.0"` 被当作分数字符串原样返回，后续 `float()` 得 1.0。
- SIGKILL 返回 `('0', 'Time limit exceeded')`。
- 编译失败返回 `('0', 'Compilation error ...')`。

**预期结果**：三行依次为 `('1.0', '')`、`('0', 'Time limit exceeded')`、`('0', 'Compilation error exit code 1\nboom')`（待本地验证 stderr 的精确拼接格式）。

#### 4.3.5 小练习与答案

**练习 1**：一个提交跑出来的响应是 `{"run": {"code": 1, "stdout": "", "stderr": "...MemoryError...", "signal": None}}`。`execute_ioi` 返回什么？最终 `_extract_single_status` 给出什么状态？

**答案**：命中第 4 步，返回 `("0", "Memory limit exceeded")`；`_extract_single_status` 看到 score=0.0 且 feedback 含 `"Memory limit exceeded"`，给出状态 `MLE`。

**练习 2**：为什么 `run_timeout` 要在题目 `time_limit` 上额外加 3 秒？

**答案**：题目自身的 `time_limit` 是由 IOI grader 脚本内部判定并反映到分数里的（超时该测试点得 0 分）；而 `run_timeout` 是 Piston 容器级别的「硬上限」。多留 3 秒缓冲，是为了让 grader 能「优雅地」判定超时并打印 0 分，而不是被容器直接 SIGKILL——那样虽然也判成 TLE，但丢失了 grader 的反馈细节。

---

### 4.4 ioi_code_reward：把评分零件串成奖励函数

#### 4.4.1 概念说明

前面三节都是「零件」。`ioi_code_reward` 是把这些零件组装成 GRPO 能直接用的奖励函数：输入模型的一批回答，输出每个回答的分数（一个 `list[float]`）。它做四件事：

1. 选执行后端（Piston 或 Morph）。
2. 从每个回答里抽出 C++ 代码并补头文件（`add_includes(extract_code(...))`）。
3. 对每条数据异步跑 `score_subtask`，异常一律兜底成空的 `SubtaskResult()`（分数 0）。
4. 取 `result.score` 作为该回答的奖励。

#### 4.4.2 核心流程

```
completions(模型回答列表) + kwargs(数据集列,含 id/subtask/test_names/...)
1. execution_client = piston 或 morph 客户端
2. code_snippets = [add_includes(extract_code(回答, "cpp"), problem_id) for ...]
3. problems_data  = [把 kwargs 各列 zip 成一条 problem_data dict]
4. 对每个 (problem_data, code) 起一个 asyncio task: score_subtask(client, problem_data, code, batch_size)
   - 用 run_catch_exceptions 包一层,异常 -> SubtaskResult()(score=0)
5. asyncio.gather 全部 task
6. return [result.score for result in results]
```

注意第 6 步取的是 `SubtaskResult.score`（归一化的 0~1 分），而非 `weighted_score`——GRPO 需要的是归一化奖励。

#### 4.4.3 源码精读

[rewards.py:L367-L417](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/rewards.py#L367-L417) —— `ioi_code_reward` 全文。注意 `add_includes(extract_code(completion[-1]["content"], "cpp"), problem_id)`：先抽代码再补头；`run_catch_exceptions` 保证任何沙箱异常都兜底为空 `SubtaskResult()`（其 `score` 因 `test_results` 为空而返回 0），奖励函数永不抛错、不拖垮训练；最后 `return [result.score for result in results]`。

奖励注册表把它注册成名字 `"ioi_code"`，并把 YAML 超参焊进去：

[rewards.py:L681-L688](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/rewards.py#L681-L688) —— 在 `get_reward_funcs` 的注册表里，`"ioi_code"` 用 `partial` 把 `test_batch_size`（来自 YAML 的 `code_eval_test_batch_size`）和 `provider_type`（来自 `ioi_provider`，默认 `"piston"`）焊死，并用 `update_wrapper` 保留 `__name__` 以通过单测。这正是 u3-l2 讲过的「注册表 + 工厂」模式。

子包的对外导出口，决定了上面这些名字怎么被外部拿到：

[\_\_init\_\_.py:L1-L19](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/utils/competitive_programming/__init__.py#L1-L19) —— `SubtaskResult`、`score_subtask`、`score_subtasks` 等从这里导出，供 `rewards.py` 用。

#### 4.4.4 代码实践

**实践目标**：阅读 IOI 配方，搞清 `ioi_code` 奖励在 GRPO 里是如何被启用的。

**操作步骤**：打开 [recipes/Qwen2.5-1.5B-Instruct/grpo/config_demo_code_ioi.yaml](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/recipes/Qwen2.5-1.5B-Instruct/grpo/config_demo_code_ioi.yaml)，找到：

- `reward_funcs: [ioi_code, code_format, format]` 与 `reward_weights: [1.0, 0.1, 0.1]`：`ioi_code` 是主奖励，另两个是格式塑形辅助奖励（权重 0.1）。
- `code_eval_test_batch_size: 3`：这就是焊进 `ioi_code` 的 `test_batch_size`——每个 prompt 采样 14 个回答（`num_generations: 14`），每个回答判分时按 3 个测试点一批、失败即停。
- `code_language: cpp` 与 `dataset_name: open-r1/ioi`：指定语言与数据集。

**需要观察的现象**：`code_eval_test_batch_size` 直接对应 4.2 的 `test_batch_size`，控制「一批跑几个测试点」。

**预期结果**：你能口头复述——「模型生成 14 个回答 → 每个回答抽 C++ 代码、补头、送 Piston → `score_subtask` 以批大小 3 分批跑、失败即停 → 取归一化 `score` 作为 `ioi_code` 奖励，与两个格式奖励加权求和」。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `ioi_code_reward` 要用 `run_catch_exceptions` 把每个子任务的异常兜底成空 `SubtaskResult()`，而不是让它抛上去？

**答案**：奖励函数在 GRPO 训练里被高频调用，且依赖外部沙箱集群。若一条数据判分抛错，会让整个训练 step 崩溃。兜底成空 `SubtaskResult()`（`score` 为 0）保证：单条判分失败只让该回答得 0 分，不影响整批训练。这延续了 u5-l2 讲过的「奖励函数永不抛错」原则。

**练习 2**：返回的奖励是 `result.score` 还是 `result.weighted_score`？为什么？

**答案**：是 `result.score`（归一化的 0~1）。GRPO 的优势估计需要在同一量纲下比较，归一化分数更适合做奖励信号；`weighted_score`（实际点数）主要用于人工查看 `to_dict()` 日志。

## 5. 综合实践

把本讲的知识串起来，完成一个「**端到端 IOI 判分模拟器**」：

1. **构造题目**：定义 2 个子任务（`s1` 满分 100、`s2` 满分 50），`s1` 含 4 个测试点、`s2` 含 3 个测试点，都自带 `test_cases`（dict 形态），并带上空的 `grader_files`。
2. **模拟两种提交**：提交 A 是「只在 `s1` 的前 2 个测试点通过、第 3 个 WA」的代码；提交 B 是「全部通过」的代码。用一个带计数的 mock `score_single_test_case` 来控制每个测试点的通过/失败。
3. **调用 `score_subtasks`**（注意它维护跨子任务缓存）：对两种提交分别以 `skip_mode=True`（即批大小 1、失败即停）评估，打印每个子任务的 `score`/`weighted_score`/`status`，以及累计的沙箱「执行」次数。
4. **验证三件事**：
   - 提交 A 在 `s1` 上因第 3 个测试点失败触发早停，第 4 个测试点未执行，`s1` 得 0 分；`s2` 全通过得满分。
   - 提交 B 两个子任务都得满分。
   - 把 `skip_mode` 改成「批大小 = 子任务测试点总数」重跑提交 A，分数不变，但「执行」次数变多——量化早停省下的算力。

提示：复用 4.2.4 的 mock 思路；注意 `score_subtasks` 的 `skip_mode` 直接作为 `test_batch_size` 传入 `score_subtask`，所以要观察「批大小=总数」的效果，可改为直接对每个子任务调 `score_subtask(..., test_batch_size=<总数>)`。最终用一句话写出：在这道题上，早停为提交 A 节省了百分之多少的沙箱调用。

## 6. 本讲小结

- IOI 评分用两级数据结构：`TestResult`（单测试点）与 `SubtaskResult`（子任务），后者的 `score`/`weighted_score`/`status` 都是 `@property`，实时由 `test_results` 派生。
- 子任务打分是 `min`（取所有测试点的最小分），实现「全有或全无」；状态取「最差优先」，优先级表里 CE 最坏、AC 最好、SKIPPED 仅在全部未执行时出现。
- `score_subtask` 用 `batched` 把测试点分批、`asyncio.gather` 并发，每批后 `any(score==0.0)` 即 `break`——这就是失败早停；未执行的测试点保持 SKIPPED(0.0)，所以早停不改分数、只省算力。
- 沙箱执行链是 `score_single_test_case → run_submission → execute_ioi`：`run_submission` 打包文件（含 `graders/<id>.cpp` 与超时换算），`execute_ioi` 把 Piston 响应解析成 `(score, feedback)`，`_extract_single_status` 再压成状态码。
- `ioi_code_reward` 把这些零件组装成 GRPO 奖励函数：抽代码 → 补头 → 异步 `score_subtask`（异常兜底为 0）→ 返回归一化 `score`；在注册表里以 `"ioi_code"` 暴露，`test_batch_size` 由 YAML 的 `code_eval_test_batch_size` 焊入。

## 7. 下一步学习建议

- 下一讲 **u6-l2 Codeforces 评分系统**：对比 IOI 的 `min`/全有或全无模型，CF 评分支持 `pass_fail`/`partial`/`weighted_sum` 三种 `scoring_mode`，并带 parquet 测试点缓存。学完能看清两套竞赛评分的差异。
- 同单元 **u6-l3 Piston 执行客户端与负载均衡**：本讲把 `PistonClient` 当黑盒，下一讲会拆开它的多端点令牌桶、指数退避重试与按 GPU 分片策略——这是支撑本讲 `send_execute` 在大规模训练下不崩的基础设施。
- 想深入 IOI 评分的外围：阅读 [ioi_utils.py](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/utils/competitive_programming/ioi_utils.py) 的 `load_ioi_tests`/`add_includes`，理解测试点的远程加载与代码补头；以及 [slurm/piston/README.md](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/slurm/piston/README.md) 里 Piston worker 集群的部署方式。
