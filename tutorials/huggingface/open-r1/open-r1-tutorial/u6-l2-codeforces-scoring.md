# Codeforces 评分系统

## 1. 本讲目标

本讲是「竞赛编程评分」单元的第二篇，承接 [u6-l1 IOI 评分系统](u6-l1-ioi-scoring.md)。上一篇我们学会了「子任务（subtask）+ grader」的重量级判分；本讲转向另一类竞赛——Codeforces（简称 CF），看 open-r1 如何给模型生成的 CF 题解打分。

学完后你应当能够：

1. 说清 `score_submission` 如何把「官方测试用例」与「额外生成的测试用例」拼成完整的判分包，并在 Piston 沙箱里逐点评测。
2. 复现三种 `scoring_mode`（`pass_fail` / `partial` / `weighted_sum`）各自的奖励公式，并能根据「通过数 / 总数」手算奖励值。
3. 理解 `@alru_cache` 异步缓存与 parquet 文件如何让大规模测试用例的加载既快又省。
4. 把这套判分链路串回 GRPO：知道 `cf_code` 这个奖励名是如何被 YAML 选中、被 `score_submission` 驱动的。

## 2. 前置知识

在进入源码前，先建立三点直觉。

**第一，CF 的判分粒度比 IOI「扁平」。** IOI 题目有「子任务」层级，一个子任务里多个测试点取最小分（全有或全无）；CF 题目通常就是「一串测试用例」，每个用例非过即挂，没有子任务聚合。所以 CF 的判分核心是：**有多少个测试用例通过**。本讲的评分函数正是围绕「通过数 / 总数」展开。

**第二，测试用例有两类来源。** 一类是题目自带的 **官方测试用例（official tests）**，随数据集一起提供；另一类是 open-r1 额外 **生成的系统测试用例（generated tests）**，用于更严格地压测模型解，存放在单独的 parquet 文件里，需要通过环境变量 `CF_TESTS_FOLDER` 指向。判分时二者拼接在一起。

**第三，判分要跑在 Piston 沙箱上。** 与 IOI 一样，CF 判分依赖一个改造过的 [Piston](https://github.com/engineer-man/piston) 服务，里面装了专门的 `codeforces` 包。客户端 `PistonClient`（上一篇 u6-l1 已介绍其负载均衡与重试）负责把代码与测试用例送进去执行。本讲聚焦「打分逻辑」，Piston 客户端本身的细节留到 [u6-l3 Piston 执行客户端与负载均衡](u6-l3-piston-client.md)。

> 关键术语：测试用例（test case）、官方测试（official tests）、生成测试（generated tests）、`scoring_mode`（评分模式）、Piston、`codeforces` 包、`CF_TESTS_FOLDER`、`@alru_cache`。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
| --- | --- |
| [src/open_r1/utils/competitive_programming/cf_scoring.py](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/utils/competitive_programming/cf_scoring.py) | **本讲主角**。CF 评分的全部逻辑：测试用例加载、单点判分、`score_submission` 主循环与三种 `scoring_mode`。 |
| [src/open_r1/utils/competitive_programming/utils.py](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/utils/competitive_programming/utils.py) | 提供 `batched` 迭代器，用于把测试用例切批并发评测。 |
| [src/open_r1/utils/competitive_programming/piston_client.py](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/utils/competitive_programming/piston_client.py) | `PistonClient.send_execute`：真正把代码送进沙箱执行的 HTTP 客户端。 |
| [src/open_r1/rewards.py](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/rewards.py) | `cf_code_reward` 与注册表中的 `"cf_code"` 条目：把判分逻辑包成 GRPO 奖励函数。 |
| [src/open_r1/configs.py](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/configs.py) | `code_eval_test_batch_size` 与 `code_eval_scoring_mode` 两个超参字段。 |
| [recipes/Qwen2.5-Coder-7B-Instruct/grpo/config_codeforces.yaml](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/recipes/Qwen2.5-Coder-7B-Instruct/grpo/config_codeforces.yaml) | CF 的 GRPO 训练配方，演示 `cf_code` 奖励的实际接线。 |

## 4. 核心概念与源码讲解

### 4.1 测试用例的装配：official + generated

#### 4.1.1 概念说明

`score_submission` 的第一件事，是确定「拿哪些测试用例来评判这份提交」。CF 的测试用例由两部分拼接：

- **official tests**：题目自带的、随数据集列 `official_tests` 直接提供的用例（通常是赛时可见的简单样例 + 部分隐藏样例）。
- **generated tests**：open-r1 为了更严苛地评估而**额外生成**的系统测试用例，按比赛（contest）分文件存成 parquet，运行时按需加载。

二者用 `+` 列表拼接成一条扁平的测试序列。这是 CF 与 IOI 的一个结构差异：IOI 把测试点组织在「子任务」树里；CF 则是一维列表，逐点判过/不过。

#### 4.1.2 核心流程

```
score_submission(client, problem_data, submission, ...)
  │
  │  test_cases = problem_data["official_tests"] + get_generated_tests(problem_data["id"])
  │                       (列表)                       (异步加载，可能为空 [])
  │
  ├─ 若 test_cases 为 None 或空 → 返回 None（不是编程题 / 无可判用例，跳过样本）
  └─ 若 submission 为空        → 返回 no_submission_reward（默认 -1.0，重罚没交代码）
```

注意三个「提前返回」语义：返回 `None` 表示「这条样本不参与判分」（与 open-r1 其它奖励一致，`GRPOTrainer` 会跳过 `None`）；而 `no_submission_reward` 是一个**负数惩罚**——模型若没产出可用代码，就给它一个明确的负梯度，逼它学会「至少要交点东西」。

#### 4.1.3 源码精读

拼接与提前返回的核心三行：

[cf_scoring.py:106-112](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/utils/competitive_programming/cf_scoring.py#L106-L112)：把官方用例与生成用例拼接；若没有用例返回 `None`，若没有提交返回 `no_submission_reward`。

```python
test_cases = problem_data["official_tests"] + (await get_generated_tests(problem_data["id"]))
# invalid/not a coding problem
if test_cases is None or len(test_cases) == 0:
    return None
# no code extracted
if not submission:
    return no_submission_reward
```

注意 `official_tests` 是同步列表，而 `get_generated_tests` 是 `await` 异步调用——所以整个 `score_submission` 必须是 `async`。`problem_data["id"]` 的格式是 `"contest_id/problem_letter"`（如 `"1234/A"`），`get_generated_tests` 会从中拆出 `contest_id` 去定位文件（见 4.2）。

#### 4.1.4 代码实践

**实践目标**：理解 `problem_data` 的数据形状，定位「official 测试从哪来」。

**操作步骤**：

1. 打开 CF 数据集页面 <https://huggingface.co/datasets/open-r1/codeforces>，查看 `verifiable-prompts` 配置的字段。
2. 在本地写一段「示例代码」读取一条样本（**示例代码，非项目原有**）：

   ```python
   from datasets import load_dataset
   ds = load_dataset("open-r1/codeforces", "verifiable-prompts", split="train")
   row = ds[0]
   print(row.keys())
   print("id =", row["id"])
   print("official_tests 条数 =", len(row["official_tests"]))
   print("language =", row.get("language"))
   ```

**需要观察的现象**：样本里应包含 `id`、`official_tests`、`language` 等字段；`official_tests` 是一个列表，元素形如 `{"input": ..., "output": ...}`。

**预期结果**：`official_tests` 直接随数据集加载，无需额外配置；而 `generated_tests` 需要单独的 parquet 文件（4.2 讲）。

> 若无法联网下载该数据集，则「待本地验证」字段结构，但可确定 `official_tests` 是 `score_submission` 直接读取的列。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `official_tests` 为空但 `generated_tests` 非空时，`score_submission` 不会返回 `None`？

**答案**：因为 `test_cases = official_tests + generated_tests` 是列表拼接，只要二者合计非空，`len(test_cases) == 0` 就不成立，函数会继续往下判分；只有**合计**为空才返回 `None`。

**练习 2**：`no_submission_reward` 默认是 `-1.0`，而 `no_compile_reward` 默认是 `-0.1`。为什么「没交代码」的惩罚比「编译失败」更重？

**答案**：编译失败至少说明模型产出了一段结构完整的代码、只是有 bug；而 `submission` 为空意味着模型连代码块都没产出（`extract_code` 抽取失败）。后者是更基本的失败，应给更强负梯度，引导模型先学会「输出可解析的代码」。

---

### 4.2 generated 测试用例的异步缓存加载：parquet + alru_cache

#### 4.2.1 概念说明

「生成的系统测试用例」数据量大，不能塞进数据集本身，而是按比赛分文件存成 **parquet**（一种列式存储格式，读取快、体积小）。open-r1 用两个函数把这层数据接进来：

- `get_generated_contest_tests(contest_id)`：读取某场比赛对应的 parquet，返回「problem_id → 用例列表」的字典。
- `get_generated_tests(problem_id)`：在上面的字典里查具体某道题，返回该题的用例列表。

关键优化是 **`@alru_cache(maxsize=32)`**：这是一个**异步 LRU 缓存**装饰器（来自 `async_lru` 库）。GRPO 训练时同一个 `contest_id` 会被成百上千次查询（一场比赛有多道题、每道题有大量采样），缓存避免反复读盘解析 parquet。`maxsize=32` 表示最多缓存 32 场比赛的结果，超出按 LRU（最近最少使用）淘汰。

#### 4.2.2 核心流程

```
get_generated_tests(problem_id="1234/A")
  │  contest_id = "1234"
  └─ get_generated_contest_tests("1234")      ← @alru_cache(maxsize=32)
       │
       ├─ 读 CF_TESTS_FOLDER 环境变量，未设置 → 抛 ValueError
       ├─ parquet 路径 = CF_TESTS_FOLDER/test_cases_1234.parquet
       ├─ 文件不存在 → 返回 {}  （该比赛无生成用例，不报错）
       └─ 文件存在 → aiofiles 异步读取 → pandas 读 parquet → 按 problem_id 分组 → 返回字典
```

注意「文件不存在返回 `{}`」是**静默降级**：并非每场比赛都有生成用例，缺失时该比赛所有题的 `get_generated_tests` 都得到空列表 `[]`，于是 `test_cases` 就只有 `official_tests`，判分照常进行。

`parquet_path` 用了 `f"test_cases_{int(contest_id):04d}.parquet"`，即比赛号被格式化成至少 4 位、前补零的文件名（如 `1234` → `test_cases_1234.parquet`）。

#### 4.2.3 源码精读

装饰器与缓存函数：

[cf_scoring.py:58-86](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/utils/competitive_programming/cf_scoring.py#L58-L86)：`@alru_cache(maxsize=32)` 缓存按比赛读取 parquet 的结果；文件缺失则静默返回 `{}`。

```python
@alru_cache(maxsize=32)  # TODO make this configurable
async def get_generated_contest_tests(contest_id: str) -> list[dict]:
    ...
    tests_folder = os.environ.get("CF_TESTS_FOLDER", None)
    if not tests_folder:
        raise ValueError("CF_TESTS_FOLDER environment variable not set! ...")
    ...
    parquet_path = os.path.join(tests_folder, f"test_cases_{int(contest_id):04d}.parquet")
    if not await aiofiles.os.path.exists(parquet_path):
        return {}
    async with aiofiles.open(parquet_path, "rb") as f:
        content = await f.read()
    df = pd.read_parquet(BytesIO(content))
    grouped_tests = df.groupby("problem_id").apply(
        lambda x: x[["input", "output"]].to_dict("records")
    ).to_dict()
    return grouped_tests
```

读取全程用 `aiofiles` 做**异步 IO**，避免阻塞事件循环——因为整个判分是 `async` 的，并发评测许多提交时，任何阻塞 IO 都会拖垮吞吐。pandas 的 `read_parquet` 本身是同步的，所以先用 `aiofiles` 把字节异步读进内存（`BytesIO`），再交给 pandas 解析。

薄封装层 `get_generated_tests`：

[cf_scoring.py:89-91](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/utils/competitive_programming/cf_scoring.py#L89-L91)：从 `problem_id` 拆出 `contest_id`，在缓存字典里查具体题目，查不到返回 `[]`。

```python
async def get_generated_tests(problem_id: str) -> list[dict]:
    contest_id = problem_id.split("/")[0]
    return (await get_generated_contest_tests(contest_id)).get(problem_id, [])
```

#### 4.2.4 代码实践

**实践目标**：搞清 `CF_TESTS_FOLDER` 的命名约定与缓存行为。

**操作步骤**（源码阅读型实践）：

1. 阅读上面两段源码，回答：若 `CF_TESTS_FOLDER=/data/cf_tests`，`problem_id = "42/C"`，会去读哪个文件？
2. 假设该文件不存在，`score_submission` 最终会用多少个测试用例判分？
3. 思考：如果 `maxsize=32` 改成 `1`，在「同一步训练同时采样到 33 场不同比赛」时会发生什么？

**需要观察的现象 / 预期结果**：

1. 文件路径为 `/data/cf_tests/test_cases_0042.parquet`（`int("42")` → `42` → `:04d` → `0042`）。
2. 文件不存在 → `get_generated_contest_tests` 返回 `{}` → `get_generated_tests` 返回 `[]` → `test_cases` 只有 `official_tests`，判分正常进行，**不会报错**。
3. `maxsize=1` 时，缓存只能容纳 1 场比赛；访问第 2 场会淘汰前一场；到第 33 场时缓存命中率趋近 0，几乎所有查询都要重新读 parquet，IO 压力剧增、吞吐下降。这就是缓存容量要匹配「同时活跃的比赛多样性」的原因。

> 若想真正运行，可创建 `test_cases_0001.parquet`（用 pandas 写几行 `input/output/problem_id`），设置 `CF_TESTS_FOLDER`，调用 `await get_generated_contest_tests("1")` 验证返回的字典结构——但需安装 `async_lru`、`aiofiles`、`pandas`，「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：为什么用 `@alru_cache` 而不是普通 `functools.lru_cache`？

**答案**：被装饰的函数是 `async def`，返回的是协程对象。普通 `lru_cache` 会把「协程对象」本身当作结果缓存，调用方每次拿到的是同一个已消耗/未消耗的协程，无法正确 `await` 多次。`alru_cache` 专为协程设计，缓存的是 `await` 之后的**真实返回值**，对调用方透明。

**练习 2**：`int(contest_id)` 这一步隐含了什么假设？如果 `contest_id` 是 `"abc"` 会怎样？

**答案**：它假设 `problem_id` 的第一段是**纯数字**的比赛号（CF 比赛 ID 确实是数字）。若 `contest_id="abc"`，`int("abc")` 会抛 `ValueError`，且因为不在 `try` 内，异常会向上冒泡。这要求上游数据格式规范。

---

### 4.3 单点判分：score_single_test_case 与 Piston codeforces 包

#### 4.3.1 概念说明

有了测试用例列表，下一步是「对单个测试用例，判断提交代码是否通过」。这正是 `score_single_test_case` 的职责：它把**提交代码 + 单个测试用例 + 题目配置**打包成一组文件，POST 给 Piston 的 `codeforces` 包执行，再根据返回的 JSON 判定结果。

Piston 的 `codeforces` 包（[slurm/piston/README.md:26-29](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/slurm/piston/README.md#L26-L29) 提到，需单独 `install` `{"language": "codeforces", "version": "1.0.0"}`）内部会：编译提交代码、按 `input.txt` 喂输入、拿程序输出与 `correct_output.txt` 比、必要时调用 `checker.py`（题目自带的自定义校验器），最后在 **stdout 打印 `"1"` 表示通过**。这是 CF 判分的关键约定：**通过与否看 stdout 是否为 `"1"`**。

#### 4.3.2 核心流程

```
score_single_test_case(client, problem_data, test_input, test_output, submission, lang)
  │
  ├─ 组装 files：
  │     • main.{lang}            ← 提交代码
  │     • checker.py             ← problem_data["generated_checker"]（若有）
  │     • input.txt              ← test_input
  │     • correct_output.txt     ← test_output
  │     • grader_config          ← TIME_LIMIT / MEMORY_LIMIT / INPUT_MODE
  │
  ├─ run_timeout = (time_limit + 10) * 1000 毫秒   ← Piston 硬超时（实际时限由 CF 脚本管）
  │
  ├─ language = "cf_python3" if lang=="python" else "c++17"
  │
  ├─ await client.send_execute(payload, language=...)
  │     成功 → 返回 Piston 结果 JSON（dict）
  │     异常 → 打印日志，返回 False
```

调用方据此判定两类结果（见 4.4）：

- **编译失败**：`result["compile"]["code"] != 0`
- **运行通过**：`result["run"]["code"] == 0 且 result["run"]["stdout"].strip() == "1"`

#### 4.3.3 源码精读

[cf_scoring.py:12-55](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/utils/competitive_programming/cf_scoring.py#L12-L55)：单点判分。组装五类文件，按语言选 `cf_python3` 或 `c++17`，交给 `PistonClient.send_execute`。

```python
result = await client.send_execute(
    {
        "files": [
            {"name": f"main.{submission_language}", "content": submission},
            *(
                [{"name": "checker.py", "content": problem_data["generated_checker"]}]
                if problem_data["generated_checker"]
                else []
            ),
            {"name": "input.txt", "content": test_input},
            {"name": "correct_output.txt", "content": test_output},
            {
                "name": "grader_config",
                "content": "\n".join(
                    f"{key}={value}"
                    for key, value in {
                        "TIME_LIMIT": problem_data["time_limit"],
                        "MEMORY_LIMIT": problem_data["memory_limit"],
                        "INPUT_MODE": problem_data["input_mode"],
                    }.items()
                ),
            },
        ],
        "run_timeout": (problem_data["time_limit"] + 10) * 1000,
    },
    language="cf_python3" if submission_language == "python" else "c++17",
)
```

两个细节值得注意：

- **`checker.py` 条件展开**：用 `*( [...] if problem_data["generated_checker"] else [] )` 实现「有 checker 才附上 checker.py」。CF 很多题用**自定义 checker**（如浮点特判、多解接受），`generated_checker` 为空时则走朴素输出比对。
- **双层超时**：`run_timeout` 是给 Piston 的硬上限 `(time_limit + 10)*1000` 毫秒（注释 [cf_scoring.py:47](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/utils/competitive_programming/cf_scoring.py#L47) 说真正的时间判定由 `codeforces` 脚本内部按 `grader_config` 里的 `TIME_LIMIT` 做）。这与 u6-l1 IOI 的「`time_limit+3`」类似，都是给沙箱留点余量。

> 类型标注小坑：函数签名标注 `-> tuple[str, str]`，但实际返回的是 Piston 的 JSON 字典（成功）或 `False`（异常）。这个标注已与实现脱节，读源码时以实际行为为准——调用方 4.4 里也是按 dict 取 `result["compile"]["code"]` 等字段的。

异常处理与 `send_execute` 的返回结构（来自客户端）：

[cf_scoring.py:51-55](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/utils/competitive_programming/cf_scoring.py#L51-L55)：异常时打印并返回 `False`，保证单个测试点失败不致整个判分崩溃。

```python
except Exception as e:
    print(f"Error scoring submission: {e}")
    return False
```

#### 4.3.4 代码实践

**实践目标**：吃透「stdout == 1 即通过」这条判分约定，以及它如何被上层解读。

**操作步骤**（源码阅读型实践）：

1. 阅读 [cf_scoring.py:127-132](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/utils/competitive_programming/cf_scoring.py#L127-L132)，回答：一个测试点「通过」需要同时满足哪两个条件？
2. 阅读 [piston_client.py:137-166](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/utils/competitive_programming/piston_client.py#L137-L166) 的 `send_execute`，确认返回的 JSON 里确实包含 `compile` 和 `run` 两个键。
3. 思考：为什么「编译失败」要单独短路（返回 `no_compile_reward`），而不是当作普通「未通过」？

**需要观察的现象 / 预期结果**：

1. 通过需满足 `result["run"]["code"] == 0`（进程正常退出）**且** `result["run"]["stdout"].strip() == "1"`（checker 判定通过）。前者排除运行时崩溃（RE）、超时（TLE）等，后者排除「答案错」（WA）。
2. `send_execute` 返回 Piston v2 API 的标准结构，含 `compile`（带 `code`/`stderr`）与 `run`（带 `code`/`stdout`/`stderr`）。
3. 编译失败意味着代码**根本无法运行**，对所有测试用例都一样——逐个跑只是浪费时间。短路能省下整道题的评测开销（见 4.4 的早停）。

#### 4.3.5 小练习与答案

**练习 1**：若某测试点执行时 Piston worker 网络异常，`score_single_test_case` 返回 `False`。上层会把它当作「通过」还是「未通过」？

**答案**：当作**未通过**。上层判定为 `result and result["run"]["code"] == 0 and ...`（[cf_scoring.py:130-132](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/utils/competitive_programming/cf_scoring.py#L130-L132)），`False` 使第一个 `result and ...` 短路为 `False`，即未通过。注意它**不会**触发编译短路（`False["compile"]` 会报错，但判编译短路用的是 `result and result["compile"]["code"] != 0`，`False and ...` 直接短路为 `False`，不进入编译失败分支）。

**练习 2**：`submission_language` 只允许哪两个值？分别映射到 Piston 的哪个语言包？

**答案**：只允许 `"python"` 和 `"cpp"`（[cf_scoring.py:20-21](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/utils/competitive_programming/cf_scoring.py#L20-L21) 校验，否则抛 `ValueError`）。`python` → `"cf_python3"`，`cpp` → `"c++17"`（[cf_scoring.py:49](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/utils/competitive_programming/cf_scoring.py#L49)）。这对应 CF 数据集「python 与 cpp 两种语言」的设计（配方注释里也写了 `8k * 2, for python and cpp`）。

---

### 4.4 score_submission 主循环与三种 scoring_mode

#### 4.4.1 概念说明

`score_submission` 是 CF 评分的「总调度」：它把测试用例**分批并发**评测，根据结果计算最终奖励。其中最核心的设计是 **`scoring_mode`**——同一个「通过数 / 总数」可以映射成三种不同的奖励曲线，对应三种训练哲学：

| `scoring_mode` | 公式 | 对「部分正确」的态度 |
| --- | --- | --- |
| `pass_fail` | 全过=1.0，否则=0.0 | 完全不给分（稀疏二值） |
| `partial` | 通过数 / 总数 | 线性给分（最鼓励部分正确） |
| `weighted_sum` | `pass_fail_score + 0.1 * (通过数/总数)` | 主项二值 + 小额部分分加成 |

`weighted_sum` 是代码里的默认值，也是 CF 配方实际使用的模式（[config_codeforces.yaml:80](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/recipes/Qwen2.5-Coder-7B-Instruct/grpo/config_codeforces.yaml#L80)）。它的妙处在于：全对时奖励 1.1（比 `partial` 的 1.0 更高，重赏完全正确），但仍有 0~0.1 的小梯度鼓励「多过几个用例」，避免稀疏奖励下模型早期学不动。

#### 4.4.2 核心流程

```
score_submission(...)
  test_cases = official + generated          （4.1）
  若空 → None；若无 submission → -1.0
  passed = 0
  for 每批 test_batch in batched(test_cases, test_batch_size):
      并发跑该批所有 score_single_test_case
      ├─ 若任一编译失败 → return no_compile_reward (-0.1)   ← 编译短路
      ├─ 标记每个用例是否通过 (run.code==0 且 stdout=="1")
      ├─ 若 pass_fail 模式 且 任一未通过 → break            ← 仅 pass_fail 早停
      └─ passed += 本批通过数
  pass_fail_score = 1.0 if passed == total else 0.0
  按 scoring_mode 返回：
      pass_fail     → pass_fail_score
      partial       → passed / total
      weighted_sum  → pass_fail_score + 0.1 * (passed / total)
```

**两个关键控制流细节**（容易看漏）：

1. **编译短路对三种模式都生效**：只要某批出现编译失败，立即返回 `no_compile_reward`，不再跑后续用例——因为编译不过的代码对所有用例都是 0 分。
2. **「未通过即 break」的早停只对 `pass_fail` 生效**（[cf_scoring.py:133-134](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/utils/competitive_programming/cf_scoring.py#L133-L134)）。`partial` 和 `weighted_sum` 需要**精确的通过数**，所以必须跑完全部用例；只有 `pass_fail`（只要不全对就是 0 分）才能在「出现一个失败」时安全地提前结束。源码注释 [cf_scoring.py:115](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/utils/competitive_programming/cf_scoring.py#L115) 的「assuming non partial score」正是指此。

`test_batch_size` 控制「每批多大」。来自 [utils.py:4-11](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/utils/competitive_programming/utils.py#L4-L11) 的 `batched`；当 `test_batch_size < 1` 时，[cf_scoring.py:116](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/utils/competitive_programming/cf_scoring.py#L116) 直接把整个 `test_cases` 当一批（`else [test_cases]`），即「不分批、一次全跑」。CF 配方设 `code_eval_test_batch_size: -1`（[config_codeforces.yaml:79](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/recipes/Qwen2.5-Coder-7B-Instruct/grpo/config_codeforces.yaml#L79)）正是此意——结合 `weighted_sum` 模式需要全跑，分批无收益，干脆一批跑完。

#### 4.4.3 源码精读

主循环（编译短路 + 早停 + 计数）：

[cf_scoring.py:114-135](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/utils/competitive_programming/cf_scoring.py#L114-L135)：分批并发评测；编译失败短路；`pass_fail` 模式遇失败早停；其余模式累计精确通过数。

```python
passed_test_cases = 0
for test_batch_to_run in batched(test_cases, test_batch_size) if test_batch_size >= 1 else [test_cases]:
    results = await asyncio.gather(
        *[
            asyncio.create_task(
                score_single_test_case(
                    client, problem_data, test_case["input"], test_case["output"], submission, submission_language
                )
            )
            for test_case in test_batch_to_run
        ]
    )
    if any(result and result["compile"]["code"] != 0 for result in results):
        return no_compile_reward

    tests_passed_results = [
        result and result["run"]["code"] == 0 and result["run"]["stdout"].strip() == "1" for result in results
    ]
    if scoring_mode == "pass_fail" and any(not test_passed for test_passed in tests_passed_results):
        break
    passed_test_cases += sum(1 for test_passed in tests_passed_results if test_passed)
```

三种评分模式：

[cf_scoring.py:137-146](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/utils/competitive_programming/cf_scoring.py#L137-L146)：先算 `pass_fail_score`（全过才 1.0），再按 `scoring_mode` 分派三种公式。

```python
pass_fail_score = 1.0 if passed_test_cases == len(test_cases) else 0.0

if scoring_mode == "pass_fail":
    return pass_fail_score
elif scoring_mode == "partial":
    return passed_test_cases / len(test_cases)
elif scoring_mode == "weighted_sum":
    return pass_fail_score + 0.1 * (passed_test_cases / len(test_cases))
else:
    raise ValueError(f"Invalid scoring mode: {scoring_mode}")
```

用数学语言写清三式（记 \(p\) 为通过数，\(n\) 为总数，\(s = \mathbb{1}[p = n]\) 为「是否全过」）：

\[
\text{pass\_fail} = s
\]

\[
\text{partial} = \frac{p}{n}
\]

\[
\text{weighted\_sum} = s + 0.1 \cdot \frac{p}{n}
\]

注意 `weighted_sum` 的上限：当 \(p = n\) 时为 \(1 + 0.1 = 1.1\)，比另两种的 1.0 上限更高，给「完全正确」额外奖励。

#### 4.4.4 代码实践（本讲核心实践）

**实践目标**：给定 `passed=8`、`total=10`，手算三种 `scoring_mode` 的奖励，并判断哪种最鼓励部分正确。再写一段「示例代码」用纯 Python 复刻公式做对照。

**操作步骤**：

1. **手算**（套用上面三式，\(p=8, n=10, s = \mathbb{1}[8=10] = 0\)）：
   - `pass_fail`：\(s = 0\)
   - `partial`：\(8/10 = 0.8\)
   - `weighted_sum`：\(0 + 0.1 \times 0.8 = 0.08\)
2. **对照源码**：打开 [cf_scoring.py:137-146](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/utils/competitive_programming/cf_scoring.py#L137-L146)，确认你的手算与 `pass_fail_score = 1.0 if 8 == 10 else 0.0` 一致。
3. **写示例代码复刻公式**（**示例代码，非项目原有**，纯本地可跑，无需 Piston）：

   ```python
   def cf_score(passed: int, total: int, mode: str) -> float:
       p, n = passed, total
       pass_fail_score = 1.0 if p == n else 0.0
       if mode == "pass_fail":
           return pass_fail_score
       elif mode == "partial":
           return p / n
       elif mode == "weighted_sum":
           return pass_fail_score + 0.1 * (p / n)
       raise ValueError(mode)

   for mode in ("pass_fail", "partial", "weighted_sum"):
       print(mode, cf_score(8, 10, mode))
   # 顺带看「全对」上限
   print("weighted_sum full:", cf_score(10, 10, "weighted_sum"))
   ```

**需要观察的现象**：三个模式对 `8/10` 分别输出 `0.0`、`0.8`、`0.08`；`weighted_sum` 全对时输出 `1.1`。

**预期结果**：

| 场景 | pass_fail | partial | weighted_sum |
| --- | --- | --- | --- |
| 8/10 通过 | 0.0 | 0.8 | 0.08 |
| 10/10 全对 | 1.0 | 1.0 | 1.1 |
| 0/10 全错 | 0.0 | 0.0 | 0.0 |

**结论**：**`partial` 最鼓励部分正确**（8/10 直接给 0.8 的高分）。`weighted_sum` 给部分正确极少分（0.08），它主要靠 `pass_fail` 主项，仅用小额加成提供梯度，并重赏完全正确（1.1）。`pass_fail` 完全不给部分分。三者分别对应「只要完全对」「线性鼓励」「重赏完全对 + 微弱梯度」三种训练偏好。

#### 4.4.5 小练习与答案

**练习 1**：同样 `passed=8, total=10`，如果把 `weighted_sum` 的系数从 `0.1` 改成 `0.5`，奖励变成多少？这会让训练更偏向哪种行为？

**答案**：\(0 + 0.5 \times 0.8 = 0.4\)。部分正确的奖励从 0.08 升到 0.4，模型会更愿意「多过几个用例」而非「孤注一掷求全对」，行为更接近 `partial`。系数越大，越鼓励部分正确；系数越小，越接近纯 `pass_fail`。

**练习 2**：为什么 `partial` 模式下，把 `test_batch_size` 从 `-1`（全跑）改成 `2`（每批 2 个）**不会**改变最终奖励，但在 `pass_fail` 模式下却可能显著省时？

**答案**：`partial` 需要精确通过数，循环里没有早停（早停仅对 `pass_fail`），无论分几批都要跑完全部用例，最终 `passed/total` 不变。`pass_fail` 模式下，一旦某批出现失败就 `break`，分批越小，越能在早期某个小批失败时立刻终止，省下后续用例的评测时间——所以分批只对 `pass_fail` 有加速意义。

**练习 3**：`no_compile_reward` 与 `no_submission_reward` 这两个参数能通过 YAML 配置吗？

**答案**：**不能**。看奖励注册表 [rewards.py:689-696](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/rewards.py#L689-L696)，`cf_code_reward` 只被注入了 `test_batch_size` 与 `scoring_mode` 两个超参；`no_compile_reward=-0.1`、`no_submission_reward=-1.0` 取的是 `score_submission` 函数签名的默认值（[cf_scoring.py:100-101](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/utils/competitive_programming/cf_scoring.py#L100-L101)），目前硬编码、未暴露为配置项。想改需要改源码。

---

### 4.5 从 score_submission 到 GRPO：cf_code_reward 与配置接线

#### 4.5.1 概念说明

`score_submission` 是个 `async` 函数，不能直接当 GRPO 奖励用——GRPO 的奖励函数签名是「接收一个 batch 的 completions 和按列拆开的 `**kwargs`，返回 `list[float]`」。`cf_code_reward` 就是这层**适配器**：它从模型输出里抽代码、从 `kwargs` 还原每道题的 `problem_data`、并发驱动 `score_submission`，把结果收集成奖励列表。最后，奖励注册表把字符串名 `"cf_code"` 映射到这个函数，使其能被 YAML 选中。

#### 4.5.2 核心流程

```
GRPOTrainer 采到一批 completions
  │
  ├─ reward_funcs = ["cf_code", "code_format"]        ← YAML: reward_funcs
  │
  └─ cf_code_reward(completions, **kwargs)            ← kwargs 含 official_tests/id/language 等列
       ├─ piston_client = get_piston_client_from_env()
       ├─ code_snippets = [extract_code(c, lang) for ...]   ← 从 markdown 代码块抽代码
       ├─ problems_data = [dict(zip(kwargs.keys(), v)) for v in zip(*kwargs.values())]  ← 还原每题 dict
       ├─ 异步并发：cf_score_submission(client, problem_data, code, scoring_mode=...)
       │     异常 → None（跳过）
       └─ 返回 list[float | None]
```

#### 4.5.3 源码精读

注册表把 `"cf_code"` 焊上两个超参：

[rewards.py:689-696](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/rewards.py#L689-L696)：`partial` 把 `code_eval_test_batch_size` 与 `code_eval_scoring_mode` 焊进 `cf_code_reward`；`update_wrapper` 保留 `__name__` 以通过单测。

```python
"cf_code": update_wrapper(
    partial(
        cf_code_reward,
        test_batch_size=script_args.code_eval_test_batch_size,
        scoring_mode=script_args.code_eval_scoring_mode,
    ),
    cf_code_reward,
),
```

适配器把列式 kwargs 还原成「每题一个 dict」，并并发判分：

[rewards.py:453-471](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/rewards.py#L453-L471)：`dict(zip(kwargs.keys(), values))` 把被 trl 按列拆开的字段重新拼成 `problem_data`，逐题丢给 `cf_score_submission`，异常兜底为 `None`。

```python
problems_data = [dict(zip(kwargs.keys(), values)) for values in zip(*kwargs.values())]

loop = _init_event_loop()
evals = [
    loop.create_task(
        run_catch_exceptions(
            cf_score_submission(
                piston_client,
                problem_data,
                code,
                test_batch_size=test_batch_size,
                scoring_mode=scoring_mode,
                submission_language=problem_data.get("language", None),
            )
        )
    )
    for problem_data, code in zip(problems_data, code_snippets)
]
results = loop.run_until_complete(asyncio.gather(*evals))
```

两个超参的源头（`configs.py`）：

[configs.py:276-285](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/configs.py#L276-L285)：`code_eval_test_batch_size`（默认 1）与 `code_eval_scoring_mode`（默认 `weighted_sum`）。

```python
code_eval_test_batch_size: int = field(
    default=1,
    metadata={"help": "for each generation, evaluate these many test cases in parallel, ..."},
)
code_eval_scoring_mode: Literal["pass_fail", "partial", "weighted_sum"] = field(
    default="weighted_sum",
    metadata={"help": "use fraction of passed test cases as reward. If false, use 0/1 scoring."},
)
```

CF 配方的实际接线：

[config_codeforces.yaml:61-66](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/recipes/Qwen2.5-Coder-7B-Instruct/grpo/config_codeforces.yaml#L61-L66)：`reward_funcs` 用 `cf_code` + `code_format`，权重 1.0 与 0.1——判分是大头，格式只是小幅塑形。

```yaml
reward_funcs:
- cf_code
- code_format
reward_weights:
- 1.0
- 0.1
```

[config_codeforces.yaml:79-80](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/recipes/Qwen2.5-Coder-7B-Instruct/grpo/config_codeforces.yaml#L79-L80)：本配方一次性全跑（`-1`）并用 `weighted_sum` 计分。

```yaml
code_eval_test_batch_size: -1
code_eval_scoring_mode: weighted_sum
```

> 注意 `submission_language` 的来源：`cf_code_reward` 用 `problem_data.get("language", None)`（[rewards.py:465](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/rewards.py#L465)），即语言来自数据集的 `language` 列；`score_submission` 签名里的默认 `"cpp"` 只在直接调用且不传该参数时才生效，注册表路径下会被数据集的值覆盖。

#### 4.5.4 代码实践

**实践目标**：追踪从 YAML 一个字段到最终打分的完整链路。

**操作步骤**（源码阅读型实践）：

1. 在 [config_codeforces.yaml:80](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/recipes/Qwen2.5-Coder-7B-Instruct/grpo/config_codeforces.yaml#L80) 找到 `code_eval_scoring_mode: weighted_sum`。
2. 跟到 [configs.py:282](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/configs.py#L282) `GRPOScriptArguments.code_eval_scoring_mode` 字段。
3. 跟到 [rewards.py:693](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/rewards.py#L693)，`partial` 把它焊进 `cf_code_reward`。
4. 跟到 [rewards.py:464](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/rewards.py#L464)，传给 `cf_score_submission` 的 `scoring_mode`。
5. 落到 [cf_scoring.py:139-146](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/utils/competitive_programming/cf_scoring.py#L139-L146) 的分支选择。

**需要观察的现象 / 预期结果**：一个 YAML 字段 → dataclass 字段 → 注册表 `partial` 注入 → 适配器透传 → `score_submission` 分支，五跳链路全程可追溯，无任何「魔法」隐式转换。

> 若想真正跑一次 `cf_code_reward`，需要 Piston worker（`PISTON_ENDPOINTS`）+ `CF_TESTS_FOLDER` + vLLM 在线采样，属重量级依赖，「待本地验证」；纯打分逻辑可用 4.4 的示例代码本地验证。

#### 4.5.5 小练习与答案

**练习 1**：`reward_weights: [1.0, 0.1]` 中，`0.1` 对应的是哪个奖励函数？为什么它权重这么小？

**答案**：`reward_weights` 与 `reward_funcs` 一一对应（u3-l1 讲过加权求和），所以 `0.1` 对应 `code_format`（格式奖励）。它小是因为「代码能过测试」才是 CF 训练的真正目标（`cf_code` 权重 1.0），格式奖励只起轻微塑形作用，避免喧宾夺主。

**练习 2**：`cf_code_reward` 里用 `run_catch_exceptions` 把异常吞成 `None`。这对 GRPO 训练为什么重要？

**答案**：GRPO 一步要评测海量采样（`num_generations` × prompt 数），若某条评测抛错（Piston worker 崩溃、网络抖动等）就会让整个训练步失败。吞成 `None` 让该样本被跳过（`GRPOTrainer` 忽略 `None`），训练继续——这与 u5-l2「奖励函数永不抛错」的设计哲学一致。

---

## 5. 综合实践

把本讲四块知识串起来：**用 stub（桩）客户端本地复现 `score_submission` 的完整控制流**，验证三种 `scoring_mode` 与编译短路、早停行为，全程无需真实 Piston。

**实践目标**：用一个假的 `PistonClient`（让它按预设结果返回）驱动真实的 `score_submission`，亲手触发「编译短路」「部分通过」「全通过」三条路径，并核对奖励值。

**操作步骤**：

1. 确认 open-r1 已安装（`pip install -e .`），或在 `PYTHONPATH=src` 下运行。
2. 写一段「示例代码」（**示例代码，非项目原有**）：

   ```python
   import asyncio
   from open_r1.utils.competitive_programming import cf_scoring

   # 构造假的 problem_data：3 个 official 测试，无生成测试
   problem_data = {
       "id": "1/A",
       "official_tests": [
           {"input": "1", "output": "2"},
           {"input": "2", "output": "4"},
           {"input": "3", "output": "6"},
       ],
       "generated_checker": "",   # 无自定义 checker
       "time_limit": 2,
       "memory_limit": 256,
       "input_mode": "stdin",
       "language": "cpp",
   }

   async def fake_single(client, pd, ti, to, sub, lang="cpp"):
       """桩函数：根据 test_input 决定返回什么结果，模拟真实 Piston JSON。"""
       if sub == "COMPILE_ERROR":
           return {"compile": {"code": 1, "stderr": "boom"}, "run": {"code": 0, "stdout": ""}}
       # input "1"→过, "2"→不过(WA), "3"→过
       ok = (ti == "1" or ti == "3")
       return {
           "compile": {"code": 0, "stderr": ""},
           "run": {"code": 0, "stdout": "1" if ok else "0"},
       }

   class FakeClient:
       pass

   async def run():
       cf_scoring.score_single_test_case = fake_single   # 打桩
       client = FakeClient()

       # 场景 A：编译失败 → 期望 -0.1 (no_compile_reward)
       r_a = await cf_scoring.score_submission(client, problem_data, "COMPILE_ERROR",
                                               test_batch_size=-1, scoring_mode="weighted_sum")
       # 场景 B：普通提交，2/3 通过
       r_b = await cf_scoring.score_submission(client, problem_data, "int main(){}",
                                               test_batch_size=-1, scoring_mode="weighted_sum")
       r_b_partial = await cf_scoring.score_submission(client, problem_data, "int main(){}",
                                               test_batch_size=-1, scoring_mode="partial")
       # 场景 C：全通过（把桩换成全过）
       async def fake_all_ok(client, pd, ti, to, sub, lang="cpp"):
           return {"compile": {"code": 0}, "run": {"code": 0, "stdout": "1"}}
       cf_scoring.score_single_test_case = fake_all_ok
       r_c = await cf_scoring.score_submission(client, problem_data, "int main(){}",
                                               test_batch_size=-1, scoring_mode="weighted_sum")
       print("A 编译失败:", r_a)
       print("B 2/3 weighted_sum:", r_b, "| partial:", r_b_partial)
       print("C 3/3 全对 weighted_sum:", r_c)

   asyncio.run(run())
   ```

**需要观察的现象**：

- 场景 A：第一个测试点编译失败，立即返回 `-0.1`，不会跑后面的用例（编译短路）。
- 场景 B：2/3 通过。`weighted_sum` = \(0 + 0.1 \times (2/3) \approx 0.0667\)；`partial` = \(2/3 \approx 0.6667\)。
- 场景 C：3/3 全对，`weighted_sum` = \(1.0 + 0.1 \times 1.0 = 1.1\)。

**预期结果**：

```
A 编译失败: -0.1
B 2/3 weighted_sum: 0.06666666666666667 | partial: 0.6666666666666666
C 3/3 全对 weighted_sum: 1.1
```

**反思题**：把场景 B 的 `test_batch_size` 从 `-1` 改成 `1`，`partial` 模式的结果会变吗？为什么？

> 参考思路：`partial` 无早停，分批只改变「何时并发」，不改变「跑哪些用例」，故结果不变（仍 0.6667）。这正好印证 4.4「早停只对 `pass_fail` 生效」的结论。

> 说明：打桩 `score_single_test_case` 是为了让纯逻辑本地可跑；真实环境下这一步由 `PistonClient.send_execute` 完成（依赖 `PISTON_ENDPOINTS` 与 `codeforces` 包）。`get_generated_tests` 因 `CF_TESTS_FOLDER` 未设会抛错——本例靠 `official_tests` 非空仍可判分？**注意**：`test_cases = official + (await get_generated_tests(...))` 会先 await 生成测试，若 `CF_TESTS_FOLDER` 未设会抛 `ValueError`。要让本例真正跑通，需把 `get_generated_contest_tests` 也打桩为返回 `{}`，或设置一个空的 `CF_TESTS_FOLDER` 目录——**待本地验证**打桩细节。

## 6. 本讲小结

- **测试用例两源拼接**：`test_cases = problem_data["official_tests"] + get_generated_tests(id)`，CF 用扁平列表（不像 IOI 的子任务树）；合计为空返回 `None`（跳过），无提交返回 `-1.0`。
- **生成用例靠 parquet + `@alru_cache`**：`get_generated_contest_tests` 按 `CF_TESTS_FOLDER/test_cases_{contest_id:04d}.parquet` 异步读取并按 problem_id 分组，`alru_cache(maxsize=32)` 缓存避免重复读盘，文件缺失静默返回 `{}`。
- **单点判分看 stdout `"1"`**：`score_single_test_case` 把代码+用例+`grader_config` 打包送 Piston `codeforces` 包，通过 = `run.code==0 且 stdout.strip()=="1"`；编译失败看 `compile.code != 0`。
- **主循环三段控制流**：分批并发（`batched`）→ 编译失败短路（三种模式都生效，返回 `-0.1`）→ `pass_fail` 模式遇失败早停（`partial`/`weighted_sum` 必须全跑以得精确通过数）。
- **三种评分公式**：`pass_fail` = 全过?1:0；`partial` = 通过数/总数（最鼓励部分正确）；`weighted_sum` = `pass_fail_score + 0.1*(通过数/总数)`（默认值，全对上限 1.1，给部分正确微弱梯度）。
- **接线五跳可追溯**：YAML `code_eval_scoring_mode` → `configs.py` 字段 → 注册表 `partial` 注入 → `cf_code_reward` 透传 → `score_submission` 分支；`no_compile_reward`/`no_submission_reward` 目前硬编码、未暴露为配置。

## 7. 下一步学习建议

- **下一篇 [u6-l3 Piston 执行客户端与负载均衡](u6-l3-piston-client.md)**：本讲把 `PistonClient` 当黑盒用了，下一篇拆开它——多端点令牌桶负载均衡、指数退避重试、健康检查，以及按 `LOCAL_RANK`/`WORLD_SIZE` 给多 GPU 训练分片端点，解释「为什么 CF/IOI 判分不会把 Piston 挤爆」。
- **回顾 [u6-l1 IOI 评分系统](u6-l1-ioi-scoring.md)**：对比 IOI 的子任务「全有或全无 + grader 印分」与 CF 的「扁平用例 + stdout `1` + 三种 scoring_mode」，体会两类竞赛判分的取舍。
- **延伸阅读**：[slurm/piston/README.md](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/slurm/piston/README.md) 了解如何在集群上部署 `codeforces` 包的 Piston worker；想自定义 `compile`/`run` 行为可参考其引用的 [piston codeforces 包](https://github.com/guipenedo/piston/blob/master/packages/codeforces/1.0.0/run)。
- **动手改改看**：尝试把 `weighted_sum` 的 `0.1` 系数提取为 `score_submission` 的一个新参数，并在注册表里暴露成 YAML 字段（参考 `code_eval_scoring_mode` 的接线方式），用本讲综合实践的桩客户端验证你的改动。
