# 测试体系与代码质量

## 1. 本讲目标

学完本讲，你应当能够：

- 读懂 open-r1 的测试分层：为什么 `tests/` 下分「快速单元测试」和 `tests/slow/` 慢集成测试两层，以及 `make test` 为什么故意忽略后者。
- 看懂 `tests/test_rewards.py` 如何用 `unittest` 为奖励函数构造「批量输入 → 逐项断言」的测试，并理解它对 `accuracy` / `format` / `len` / `cosine` / `repetition` / `soft_overlong` 等函数的覆盖。
- 看懂 `tests/utils/test_data.py` 如何为 `get_dataset` 的混合数据集分支设计权重、切分、列一致性等边界用例。
- 掌握 `Makefile` 的 `style` / `quality` / `test` / `slow_test` 四个目标，以及 `setup.cfg` 里 ruff / isort / flake8 / pytest 的配置约定。
- 能为 `get_soft_overlong_punishment` 补一个**未被现有测试覆盖的边界用例**，并用 `make test` 验证通过。

本讲是评估与测试单元（u8）的第二篇，承接 u8-l1（LightEval 基准评估），从「对外评估」转向「对内质量」。

## 2. 前置知识

在进入测试代码前，先建立三个直觉。它们都来自前面讲义，这里只做最简回顾：

1. **奖励函数的批量签名**（u3-l2、u3-l3、u3-l4）：open-r1 的每个奖励函数都接受「一批 completion」而非单条，形如 `func(completions, solution=[], **kwargs) -> list[float]`。其中 `completions` 是 `list[list[dict]]`（外层是 batch，中层是一个对话，内层是消息字典，答案在 `content` 字段）。所以测试里随处可见 `[[{"content": "..."}]]` 这种三层嵌套——这是 trl「列注入」机制决定的约定，不是随意写的。

2. **数据集混合的不变量**（u2-l2）：`get_dataset` 有两条互斥分支——`dataset_name` 单数据集、`dataset_mixture` 混合。混合分支要求各成员列名一致（否则 `concatenate` 失败），`weight` 抽样数由 `int(len(ds)*weight)` 截断决定，`test_split_size` 仅在混合分支生效。本讲的 `test_data.py` 正是在测试这些不变量与边界。

3. **三元组配置与 `__post_init__` 校验**（u1-l4）：`ScriptArguments.__post_init__` 会把 YAML 里的 `dataset_mixture` 字典翻译成 `DatasetMixtureConfig`，并做「二选一」「结构合法」「列一致」三道校验。很多数据测试并不真的调用 `get_dataset`，而是直接断言「非法配置会抛 `ValueError`」——这把 bug 拦在配置解析阶段。

如果你对以上三点还有陌生，建议先回看对应讲义；本讲重点在「如何为这些行为写测试」。

## 3. 本讲源码地图

本讲涉及的文件如下表：

| 文件 | 作用 | 本讲视角 |
| --- | --- | --- |
| `tests/test_rewards.py` | 奖励函数的快速单元测试（568 行） | 重点：批量签名、正/负/边界用例的组织 |
| `tests/utils/test_data.py` | `get_dataset` 的单元测试（129 行） | 重点：混合数据集的权重、切分、列一致性 |
| `tests/slow/test_code_reward.py` | 代码奖励的慢集成测试（需沙箱） | 重点：为什么与快速测试分离 |
| `Makefile` | 封装 install / style / quality / test / slow_test / evaluate | 重点：`PYTHONPATH=src` 与 test/slow_test 的分治 |
| `setup.cfg` | isort / flake8 / pytest 配置 | 重点：行宽 119、忽略规则、per-file-ignores |
| `setup.py` | 依赖与 extras 分组 | 重点：`tests` / `quality` / `code` extras 装了什么 |
| `src/open_r1/rewards.py` | 奖励函数实现 | 重点：被测对象 `get_soft_overlong_punishment` |

测试目录很小，只有三个测试文件加两个 `__init__.py`：

```text
tests/
├── __init__.py
├── test_rewards.py          # 快速：奖励函数
├── utils/
│   └── test_data.py         # 快速：数据加载
└── slow/
    └── test_code_reward.py  # 慢：需 E2B/Morph/Piston 沙箱
```

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：先讲测试分层（为什么 `make test` 忽略 `tests/slow/`），再分别精读两个快速测试文件，最后讲代码质量工具链。

### 4.1 测试分层：快速单元测试 vs slow 集成测试

#### 4.1.1 概念说明

open-r1 的测试天然分两层：

- **快速单元测试**（`tests/test_rewards.py`、`tests/utils/test_data.py`）：纯 Python 逻辑，只依赖 `open_r1` 本体和少量轻量库（`math-verify`、`datasets`）。秒级跑完，是日常开发和 CI 的默认目标。
- **慢集成测试**（`tests/slow/test_code_reward.py`）：代码奖励需要把模型生成的代码丢进**真实沙箱**（E2B、MorphCloud 或 Piston worker）执行，还要从 Hub 下载带 `verification_info` 的专用数据集。这类测试既花钱（云沙箱按次计费）又依赖外部服务，不适合每次提交都跑。

把它们物理隔离到不同目录，再让 `make test` 用 `--ignore` 跳过 slow 目录，是 open-r1 的核心策略：保证「`make test` 永远是快的、自包含的」。

#### 4.1.2 核心流程

`Makefile` 的测试相关目标如下（先看分治逻辑）：

- `test`：跑 `pytest`，但显式 `--ignore=tests/slow/`。
- `slow_test`：只跑 `tests/slow/`，且 `-vv` 加详细输出。
- 顶部 `export PYTHONPATH = src`：让 pytest 直接测本地 `src/open_r1` 检出，而不是 pip 装的版本——这点在 u1-l3 已强调，是「测的就是你改的代码」的保证。

#### 4.1.3 源码精读

先看 `Makefile` 顶部的环境声明，它决定了「测的是哪份代码」：

[Makefile:3-6](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/Makefile#L3-L6) —— 注释明确说明「不要加引号」，并把 `src` 注入 `PYTHONPATH`；`check_dirs := src tests` 是后续 style/quality 的扫描范围。

再看两个测试目标的分治：

[Makefile:27-31](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/Makefile#L27-L31) —— `test` 用 `--ignore=tests/slow/` 跳过慢测试，`slow_test` 则只跑慢测试。两者互不重叠。

那么 slow 测试到底「重」在哪里？看它依赖的导入与注释：

[tests/slow/test_code_reward.py:18-23](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/tests/slow/test_code_reward.py#L18-L23) —— 它 import 了 `e2b_code_interpreter` 的 `Execution` / `ExecutionError`，以及 `RoutedMorphSandbox` / `RoutedSandbox`，这些都是**外部沙箱客户端**（见 u5-l2、u5-l3）。

具体到一个用例，注释把「外部依赖」说得很直白：

[tests/slow/test_code_reward.py:27-36](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/tests/slow/test_code_reward.py#L27-L36) —— 注释 `# requires E2B, see the README.md file`，并从 Hub 加载专用数据集，对 20 条样本要求 `rewards == [1.0]*20`。IOI 那个用例甚至需要 ~64 个 Piston worker：

[tests/slow/test_code_reward.py:74-77](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/tests/slow/test_code_reward.py#L74-L77) —— 注释说明需要拉起一批 Piston worker，参考 `slurm/piston/README.md`。

正因为有这些外部依赖，把它们隔离到 `tests/slow/` 并默认忽略，是合理的工程取舍。

> **设计要点**：open-r1 没有用 pytest marker（如 `@pytest.mark.slow`）来分流，而是用**目录 + `--ignore`** 这种更朴素的方式。好处是不依赖 pytest 配置、对新人直观；代价是「慢」与「快」物理上不能混在一个文件里。如果你要新增一个需要 GPU 的测试，按约定应放进 `tests/slow/`。

### 4.2 test_rewards.py：对各奖励函数的覆盖

#### 4.2.1 概念说明

`test_rewards.py` 是 open-r1 测试覆盖最密的文件，用标准库 `unittest`（而非 pytest 风格）写成。它验证两件事：

1. **注册表机制**：`get_reward_funcs` 能把 YAML 里的字符串名正确翻译成可调用函数，且函数 `__name__` 符合预期（这是 u3-l2 讲过的「`partial` + `update_wrapper` 保留 `__name__` 以通过单测」的落脚点）。
2. **每个奖励函数的判分逻辑**：对正确、错误、边界三种输入，返回值是否符合公式。

#### 4.2.2 核心流程

该文件的组织方式是「一个被测函数 → 一组测试方法」，分散在三个 `unittest.TestCase` 子类里：

- `TestGetRewardFuncs`：只测注册表，1 个方法。
- `TestRewards`：测 `accuracy` / `format` / `reasoning_steps` / `cosine` / `len` 等直接函数，含批量与多完成情况。
- `TestRepetitionPenaltyReward`：名字叫 repetition，但实际还塞进了 `tag_count`、`code_format`、`soft_overlong` 的测试——这是历史遗留的归类，读源码时注意别被类名误导。

每个测试方法的套路一致：

1. 构造 `completion = [[{"content": "..."}]]` 这种三层嵌套输入。
2. 调用奖励函数得到 `rewards` 列表。
3. 用 `assertEqual` / `assertAlmostEqual` / `assertGreater` 断言 `rewards[0]` 或整个列表。

#### 4.2.3 源码精读

先看注册表测试——这是 u3-l2「字符串名 → 函数」契约的守门人：

[tests/test_rewards.py:37-75](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/tests/test_rewards.py#L37-L75) —— 它把 11 个 reward 名喂给 `get_reward_funcs`，断言返回 11 个函数，且每个函数的 `__name__` 与期望名一一对应。注意它构造 `GRPOScriptArguments(dataset_name="dummy", reward_funcs=reward_names)` 时用了 `dataset_name="dummy"` 这个占位值——因为 `ScriptArguments` 要求「二选一」非空，随便给个字符串绕过校验即可。

再看一个典型的「正/负用例配对」：`accuracy_reward` 的正确与错误分支。

[tests/test_rewards.py:79-98](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/tests/test_rewards.py#L79-L98) —— 三个方法分别测「分数相等→1.0」「分数不等→0.0」「无 LaTeX 金答案也判 0.0」。注意 `\boxed{\frac{63}{400}}` 这种原始字符串：测试用 `r"..."` 避免 `\f` 被当成转义。这正是 u3-l2 讲过的「`accuracy_reward` 只比数学等价、不做字符串比对」的体现——`\frac{63}{400}` 和 `\boxed{\frac{63}{400}}` 经 `math_verify` 解析后判定相等。

`len_reward` 的测试则展示了「组内相对」的语义，用 `assertGreater` 比较相对大小而非绝对值：

[tests/test_rewards.py:218-228](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/tests/test_rewards.py#L218-L228) —— 断言「更短的正确答案奖励更高」且最短者恰为 `0.5`。回顾 u3-l4 的公式：组内最短正确答案的 `lambda_val = 0.5 - (L-min)/(max-min)`，当 `L=min` 时为 `0.5`。

最后看本讲代码实践要补充的对象——`get_soft_overlong_punishment` 的现有三个用例：

[tests/test_rewards.py:461-482](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/tests/test_rewards.py#L461-L482) —— 三个用例分别覆盖：长度 50（`<= 80`，奖励 0）、长度 110（`> 100`，奖励 -1）、长度 90（介于 80 与 100 之间，奖励 -0.5）。

注意这三个用例用的是 `completion_ids=[[1]*50]` 这种「假 token id 列表」，因为 `soft_overlong_punishment_reward` 只看 `len(ids)`，不看真实文本——这是 u3-l4 强调的「唯一按 token 计长」的函数。`get_soft_overlong_punishment(max_completion_len=100, soft_punish_cache=20)` 的两个参数把阈值设为：缓冲带起点 = 100-20 = 80，硬上限 = 100。

被测函数本体如下，理解它才能设计新边界用例：

[src/open_r1/rewards.py:620-643](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/rewards.py#L620-L643) —— 三分支逻辑：

- `len <= max_completion_len - soft_punish_cache`（即 `<= 80`）→ `0.0`（不罚）。
- `80 < len <= 100`（缓冲带内）→ `(80 - len)/20`，线性从 0 降到 -1。
- `len > 100` → `-1.0`（封顶惩罚）。

#### 4.2.4 代码实践

这是一个**源码阅读 + 修改参数观察**型实践，目标是验证你对 `get_soft_overlong_punishment` 分支边界的理解。

**实践目标**：手算两个现有测试**未覆盖**的边界长度，确认你的预测，为 4.2.5 的练习和本讲综合实践做准备。

**操作步骤**：

1. 打开 [src/open_r1/rewards.py:630-641](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/rewards.py#L630-L641)，对照 `max_completion_len=100, soft_punish_cache=20` 这组参数。
2. 手算以下两个长度的奖励：
   - 长度恰为 `80`（即 `max_completion_len - soft_punish_cache`）。
   - 长度恰为 `100`（即 `max_completion_len`）。
3. 在本地 Python 里验证（不需要 GPU）：
   ```python
   from open_r1.rewards import get_soft_overlong_punishment
   fn = get_soft_overlong_punishment(max_completion_len=100, soft_punish_cache=20)
   print(fn(completion_ids=[[1]*80]))   # 预期 0.0
   print(fn(completion_ids=[[1]*100]))  # 预期 -1.0
   ```

**需要观察的现象**：

- 长度 80 走第一个分支 `80 <= 80` 为真，返回 `0.0`——说明阈值点本身「不罚」。
- 长度 100 走第二个分支 `80 < 100 <= 100` 为真，返回 `(80-100)/20 = -1.0`——说明到达硬上限时已与第三分支一样是 -1.0，两段在 100 处连续。

**预期结果**：`[0.0]` 与 `[-1.0]`。若 `PYTHONPATH` 未含 `src`，可执行 `PYTHONPATH=src python your_script.py`，或直接 `make test` 让 Makefile 替你设好。如果你本地未安装依赖（`math-verify` 等），此步为「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `test_get_reward_funcs` 要断言 `func.__name__`，而不只是断言函数可调用？

**参考答案**：因为部分奖励函数是工厂返回的闭包（如 `cosine`、`code`），若不显式 `update_wrapper`，闭包的 `__name__` 会是内层函数名甚至统一的 `<lambda>`。断言 `__name__` 等于期望名，能守住「注册表产出的函数名可被 trl 日志、`reward_weights` 对齐等下游机制识别」这一契约。这正是 u3-l2 提到的 `partial` + `update_wrapper` 的意义。

**练习 2**：`test_accuracy_reward_*` 三个用例为什么都要用 `r"..."` 原始字符串？

**参考答案**：金答案含 LaTeX 命令如 `\frac`、`\boxed`，普通字符串里 `\f` 是转义字符（换页）。用原始字符串保证反斜杠原样传给 `math_verify`，否则解析的就不是合法 LaTeX，测试会假性失败。

**练习 3**：现有 `soft_overlong` 测试覆盖了长度 50/90/110。除了 4.2.4 提到的 80、100，还有哪个长度值得补一个用例？

**参考答案**：长度 `81`——它是缓冲带的「第一格」，用于确认刚跨过阈值时惩罚从 0 跳到一个很小的负值 `(80-81)/20 = -0.05`，验证「软」过渡而非硬跳变。配合 80（0.0）与 81（-0.05）两个点，能锁死阈值边界的行为。

### 4.3 test_data.py：对 mixture 的边界测试

#### 4.3.1 概念说明

`test_data.py` 测的是 u2-l2 讲过的 `get_dataset`。它的设计思路是「用一个固定的真实小数据集 `trl-internal-testing/zen` 作参照，构造各种 `ScriptArguments` 配置，验证 `get_dataset` 的输出行数和列是否符合公式」。

它还覆盖了一类**不该进 `get_dataset` 就该被拦截**的非法配置——这类测试断言的是「抛 `ValueError`」，把 bug 拦在配置解析阶段（`ScriptArguments.__post_init__`）。

#### 4.3.2 核心流程

- `setUpClass` 在所有测试前一次性加载参照数据集，后续断言行数都拿它做基准。
- 合法路径用例：无权重混合、带权重混合、混合 + test 切分、列选择——逐个验证行数公式。
- 非法路径用例：列名不一致、既无 `dataset_name` 又无 `dataset_mixture`——断言抛 `ValueError` 并检查异常消息。

#### 4.3.3 源码精读

先看参照数据集的建立——它是所有行数断言的基准：

[tests/utils/test_data.py:24-28](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/tests/utils/test_data.py#L24-L28) —— `setUpClass` 是 `unittest` 的类级夹具，整个类只加载一次 `trl-internal-testing/zen` 的 `conversational_preference` 配置，存为 `cls.ref_dataset`。

无权重混合：直接把 train 与 test 两个 split 拼起来，行数应相加：

[tests/utils/test_data.py:37-50](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/tests/utils/test_data.py#L37-L50) —— 断言 `len(train) == len(ref.train) + len(ref.test)`。注意 `weight=None` 表示「不抽样、全量取」，这是与带权重分支的关键区别。

带权重混合：验证 u2-l2 的截断公式 `int(len(ds)*weight)`：

[tests/utils/test_data.py:52-67](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/tests/utils/test_data.py#L52-L67) —— 权重 0.25 与 0.5，断言行数为 `len(ref.train)//4 + len(ref.test)//2`。用整除 `//` 而非乘法，正是因为 `int(len*weight)` 对正数等价于向下取整（截断）。

混合 + test 切分：验证 `test_split_size` 的取整与守恒：

[tests/utils/test_data.py:69-83](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/tests/utils/test_data.py#L69-L83) —— 取 `train[:10]` 共 10 条，`test_split_size=0.2`，断言 train 8 条、test 2 条。这正是 u2-l2 讲的「sklearn 规则：test 向上取整」——10×0.2=2，恰好 8/2。

非法路径之一：列名不一致应在构造 `ScriptArguments` 时就抛错：

[tests/utils/test_data.py:106-120](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/tests/utils/test_data.py#L106-L120) —— 两个成员一个选 `["prompt"]`、一个选 `["chosen"]`，列名不一致。用 `assertRaises(ValueError)` + `assertIn("Column names must be consistent", ...)` 同时验证「抛了」和「消息对了」。注意它连 `get_dataset` 都没调，直接断言 `ScriptArguments(...)` 构造失败——把校验前置到配置层。

非法路径之二：二选一为空：

[tests/utils/test_data.py:122-125](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/tests/utils/test_data.py#L122-L125) —— `dataset_name=None, dataset_mixture=None`，断言抛 `ValueError` 且消息含 `Either ... must be provided`。

> **测试技巧小结**：`test_data.py` 示范了两类互补的断言——「合法输入 → 验证输出数值」（正例）与「非法输入 → 验证抛错及消息」（反例）。后者尤其重要：它锁死了配置系统的「不变量」，任何放松校验的改动都会立刻被测试抓住。

#### 4.3.4 代码实践

**实践目标**：通过阅读测试，反推 `get_dataset` 对一个**未在测试中出现**的权重组合的输出行数，再用代码验证。

**操作步骤**：

1. 选一个参照 split（例如 `ref_dataset["train"]`），记下它的行数 `N`（可在本地 `python -c "from datasets import load_dataset; print(len(load_dataset('trl-internal-testing/zen','conversational_preference')['train']))"` 获取）。
2. 构造一个双成员混合，权重分别为 `0.3` 与 `0.1`，预测行数为 `int(N*0.3) + int(N*0.1)`。
3. 参照 [tests/utils/test_data.py:52-67](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/tests/utils/test_data.py#L52-L67) 的写法，运行 `get_dataset` 并断言行数等于预测值。

**需要观察的现象**：由于 `int()` 截断，实际行数会略小于 `N*0.4`，差值最多 2 条（两个成员各可能少 1）。

**预期结果**：行数恰好等于 `int(N*0.3) + int(N*0.1)`。若本地无法联网下载 `trl-internal-testing/zen`，此项为「待本地验证」，可改用本地缓存的任意带 train/test split 的小数据集复现。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `test_weighted_mixture` 用 `len(ref.train)//4` 而不是 `int(len(ref.train)*0.25)`？

**参考答案**：两者对正数等价（都向下取整），但 `//4` 更直白地表达了「weight=0.25 即取 1/4」的意图，可读性更好。它也顺便验证了实现里用的是截断而非四舍五入——若实现改成 `round()`，测试会失败。

**练习 2**：`test_mixture_with_mismatched_columns` 为什么不调用 `get_dataset`？

**参考答案**：因为列一致性校验发生在 `ScriptArguments.__post_init__`（配置解析阶段），比 `get_dataset` 更早。提前到构造 `ScriptArguments` 时抛错，能在「错配置还没碰到数据」时就把问题暴露，错误信息也更聚焦。测试因此只需断言构造抛 `ValueError`。

### 4.4 代码质量工具链：Makefile + setup.cfg + setup.py extras

#### 4.4.1 概念说明

open-r1 的代码质量由三个工具分工，配置集中在 `setup.cfg`（注意：本项目**没有** `pyproject.toml`，也没有 `.flake8`、`.isort.cfg` 等独立文件，全部塞在 `setup.cfg`）：

- **ruff**：超快的 Python linter + formatter，既做 `ruff format`（格式化）也做 `ruff check`（检查）。
- **isort**：专门整理 import 顺序。
- **flake8**：补充检查（open-r1 主要用它兜底，因为 ruff 的检查项与 flake8 部分重叠）。

`Makefile` 把它们的调用封装成两条目标，区分「会改写文件」与「只检查不改」：

- `make style`：**会改写**你的代码（format + isort 重排），本地整理用。
- `make quality`：**只检查**不改（check + isort --check-only + flake8），CI 用，任何不符都会非零退出导致 CI 失败。

这条「style 改、quality 查」的区分是 HuggingFace 生态的通行约定。

#### 4.4.2 核心流程

- 开发者本地跑 `make style` 自动整理代码。
- 提交前 / CI 跑 `make quality` 确保无格式问题。
- `make test` 跑快速测试。
- `make slow_test` 单独跑慢测试。

所有工具共享一个关键约定：**行宽 119**（ruff、isort、flake8 三处都设成 119，避免互相打架）。扫描范围统一是 `check_dirs := src tests` 加 `setup.py`。

工具链本身的依赖由 `setup.py` 的 `extras["quality"]` 提供（`ruff`、`isort`、`flake8`），测试依赖由 `extras["tests"]` 提供（`pytest`、`parameterized`、`math-verify`、`jieba`）。`extras["dev"]` 是 quality + tests + eval + code 的合集，`make install` 装的就是 `[dev]`（见 u1-l3）。

#### 4.4.3 源码精读

先看 `Makefile` 的 style / quality 目标，注意两者命令的细微差别：

[Makefile:18-25](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/Makefile#L18-L25) —— `style` 用 `ruff format`（写回）+ `isort`（写回）；`quality` 用 `ruff check`（只查）+ `isort --check-only`（只查）+ `flake8`（只查）。两者都带 `--line-length 119 --target-version py310`，扫描 `$(check_dirs) setup.py`。

再看 `setup.cfg` 里的配置分区。isort 区：

[setup.cfg:1-31](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/setup.cfg#L1-L31) —— `line_length = 119` 与 Makefile 对齐；`known_first_party = open_r1` 把本项目识别为 first-party，使 import 分组时 `open_r1.xxx` 归入「项目自身」组而非 third-party；`lines_after_imports = 2` 要求 import 块后空两行再写代码。

flake8 区——关键在 `ignore` 与 `per-file-ignores`：

[setup.cfg:33-38](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/setup.cfg#L33-L38) —— 忽略 `E203`（切片空格，与 black 冲突）、`E501`（行太长，交给 ruff 管）、`E741`（模糊变量名）、`W503`/`W605` 等；`per-file-ignores` 给所有 `__init__.py` 放行 `F401`（import 未使用）——因为 `__init__.py` 的职责就是 re-export，必然有「导入了但本文件没用」的符号（回顾 u1-l2 提到的 `utils/__init__.py` 集中导出）。

pytest 区虽小但有一个细节：

[setup.cfg:40-41](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/setup.cfg#L40-L41) —— `[tool:pytest]` 只设了 `doctest_optionflags`，**没有**设 `testpaths` 或 marker。这正是为什么 `Makefile` 要显式 `--ignore=tests/slow/`：因为配置里没有声明「哪些目录算测试」，只能靠命令行参数控制。

最后看 `setup.py` 的 extras，理解工具链依赖从哪来：

[setup.py:92-98](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/setup.py#L92-L98) —— `extras["quality"]` 装 ruff/isort/flake8；`extras["tests"]` 装 pytest/parameterized/math-verify/jieba；`extras["code"]` 装沙箱相关（e2b/morph/jieba/pandas/aiofiles），对应 slow 测试所需；`extras["dev"]` 是前四者合集。这意味着：只跑 `make test` 其实只需 `extras["tests"]`（加 core 依赖），不必装重型的 vllm/flash-attn。

> **常见踩坑**：如果你只 `pip install -e .`（不带 extras），`pytest` 都找不到，更别说 `ruff`。最小可测安装是 `pip install -e ".[tests]"`；要做质量检查则 `pip install -e ".[quality]"`；全套开发则 `pip install -e ".[dev]"`（但 `[dev]` 含 eval/code，较重）。

#### 4.4.4 代码实践

**实践目标**：亲手体验 `make style` 与 `make quality` 的「改 vs 查」差异，并理解 `per-file-ignores` 的作用。

**操作步骤**：

1. 确保装了 quality 工具：`pip install -e ".[quality]"`（或完整 `make install`）。
2. 在 `tests/` 下临时建一个文件 `tests/_tmp_quality_demo.py`，故意写两种问题：
   ```python
   import os,sys
   x=1
   ```
   （import 未按 isort 规范、等号两侧无空格、可能超合并行）
3. 运行 `make quality`，观察 ruff/flake8/isort 报告哪些问题（此时文件**未被修改**）。
4. 运行 `make style`，再用 `git diff tests/_tmp_quality_demo.py` 查看工具**自动改写**了什么。
5. 再次 `make quality`，应无报错。
6. 实践结束后删除该临时文件，**不要**提交。

**需要观察的现象**：`make quality` 只输出问题不改文件；`make style` 静默改写，需用 `git diff` 才能看到变化（如 `import os,sys` → 分两行并排序、`x=1` → `x = 1`）。

**预期结果**：`make style` 后 `make quality` 退出码为 0。若你忘了把临时文件放进 `tests/`（不在 `check_dirs` 内），则不会有任何检查——这正好印证「扫描范围限定 `src tests`」。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `setup.cfg` 给 `__init__.py` 单独放行 `F401`？

**参考答案**：`__init__.py` 的职责是把子模块的符号 re-export 到包层级（如 `open_r1/utils/__init__.py` 导出 `get_dataset`），必然出现「import 了但本文件没直接使用」的符号，flake8 默认会报 `F401 imported but unused`。放行它避免误报，同时不影响对真正未使用导入的检查。

**练习 2**：如果有人新建了 `tests/slow/test_new.py` 但忘了它依赖云沙箱，`make test` 会怎样？

**参考答案**：`make test` 用 `--ignore=tests/slow/` 整体跳过该目录，所以 `test_new.py` 根本不会被收集，也就不会因为缺沙箱而失败——这正是分层的目的。但它也不会被验证，需要开发者主动 `make slow_test` 才会跑到。

**练习 3**：`make style` 与 `make quality` 的命令几乎一样，只是 format↔check、isort↔isort --check-only。为什么要保留两个目标而不是只留一个？

**参考答案**：分离「改写」与「检查」是为了让 CI 只跑只读检查（`make quality`），任何格式不符直接让 CI 失败，而不会偷偷改代码；本地开发则用 `make style` 主动整理。若只留一个会改写的目标，CI 就无法做「只验证不修改」的守门。

## 5. 综合实践

把本讲四个模块串起来：**为 `get_soft_overlong_punishment` 补一个真正有价值的边界单元测试，并让它通过 open-r1 的完整质量与测试流水线**。

### 背景

回顾 4.2.3：现有 `soft_overlong` 测试（[tests/test_rewards.py:461-482](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/tests/test_rewards.py#L461-L482)）覆盖了长度 50/90/110 三个点，但**遗漏了两个关键边界**：

- 长度恰为 `max_completion_len - soft_punish_cache`（= 80）：阈值点，应返回 `0.0`。
- 长度恰为 `max_completion_len`（= 100）：硬上限点，应返回 `-1.0`（缓冲带公式的右端点）。

补上这两个点，能把「不罚 / 缓冲带 / 封顶」三段的**接缝**都锁死。

### 操作步骤

1. **最小安装**（不需 GPU）：`pip install -e ".[tests,quality]"`。

2. **在 `tests/test_rewards.py` 的 `TestRepetitionPenaltyReward` 类中**（与现有三个 `soft_overlong` 测试为邻），新增一个方法，例如：

   ```python
   def test_soft_overlong_punishment_boundary_lengths(self):
       """Test soft overlong punishment at the two seam points:
       exactly at (max - cache) and exactly at max."""
       reward_fn = get_soft_overlong_punishment(max_completion_len=100, soft_punish_cache=20)
       # 阈值点 80: 80 <= 80, 第一分支 -> 0.0
       self.assertEqual(reward_fn(completion_ids=[[1] * 80]), [0.0])
       # 硬上限点 100: 80 < 100 <= 100, 第二分支右端点 (80-100)/20 -> -1.0
       self.assertEqual(reward_fn(completion_ids=[[1] * 100]), [-1.0])
   ```
   （此为**示例代码**，需放入现有测试文件并遵守项目风格。）

3. **跑格式与质量检查**：先 `make style` 自动整理（isort 会重排 import 顺序、ruff 调整空格），再 `make quality` 确认无报错。注意 `tests/` 在 `check_dirs` 内，所以新文件会被检查。

4. **跑测试**：`make test`（它会 `--ignore=tests/slow/`，但 `test_rewards.py` 在快速层，会被执行）。也可只跑这一个类：`PYTHONPATH=src pytest -sv tests/test_rewards.py::TestRepetitionPenaltyReward::test_soft_overlong_punishment_boundary_lengths`。

### 需要观察的现象

- `make style` 可能把你新增方法的空格、引号风格统一成项目约定（行宽 119）。
- `make quality` 退出码 0。
- `make test` 中新方法 PASS，且原有的三个 `soft_overlong` 用例仍 PASS（未受影响）。

### 预期结果

新增测试通过，`make quality` 与 `make test` 全绿。这验证了：你不仅理解了 `get_soft_overlong_punishment` 的三分支语义，还熟悉了 open-r1「写测试 → style → quality → test」的标准贡献流程。

> 若你本地未安装 `math-verify` 等依赖导致 `test_rewards.py` 整体 import 失败，可只验证新方法的逻辑（见 4.2.4 的独立脚本），完整 `make test` 标注为「待本地验证」。

## 6. 本讲小结

- open-r1 把测试物理分两层：`tests/` 下是快速单元测试，`tests/slow/` 是需云沙箱（E2B/Morph/Piston）与专用数据集的慢集成测试；`make test` 用 `--ignore=tests/slow/` 跳过后者，保证日常 CI 又快又自包含。
- `Makefile` 顶部 `export PYTHONPATH = src` 确保 pytest 测的是本地 `src/open_r1` 检出，而非 pip 装的旧版本。
- `test_rewards.py` 用 `unittest` 组织「正/负/边界」用例，`test_get_reward_funcs` 守住「字符串名 → 函数且 `__name__` 正确」的注册表契约；奖励函数的批量三层嵌套签名 `[[{"content":...}]]` 是 trl 列注入决定的。
- `test_data.py` 用固定参照数据集 `trl-internal-testing/zen` 验证 `get_dataset` 的行数公式（权重截断 `int(len*weight)`、test 向上取整），并用 `assertRaises(ValueError)` 锁死「列一致」「二选一」两类配置不变量——部分反例连 `get_dataset` 都不调，直接断言 `ScriptArguments` 构造失败。
- 代码质量工具链是 ruff + isort + flake8，配置集中在 `setup.cfg`（无 `pyproject.toml`），统一行宽 119、给 `__init__.py` 放行 `F401`；`make style` 会改写、`make quality` 只检查（CI 守门），二者扫描范围限 `src tests` + `setup.py`。
- 工具与测试依赖由 `setup.py` 的 extras 分组提供：`[quality]` 装 ruff/isort/flake8，`[tests]` 装 pytest/math-verify/jieba，`[code]` 对应 slow 测试的沙箱依赖，`[dev]` 是合集。

## 7. 下一步学习建议

- **横向对照 slow 测试**：阅读 [tests/slow/test_code_reward.py](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/tests/slow/test_code_reward.py)，结合 u5-l1/u5-l2/u5-l3，理解这些用例如何用「金标准解答 → 期望全 1.0」的策略端到端验证沙箱判分链路。
- **补全测试覆盖**：本讲综合实践只补了 `soft_overlong` 的边界。可继续审视 `cosine_scaled_reward`、`tag_count_reward` 是否有未覆盖的接缝（如 `cosine` 的 `progress` 不截断边界、`tag_count` 的多标签缺失组合），按本讲的「正/负/边界」范式补用例。
- **回到评估闭环**：本讲与 u8-l1（LightEval 基准评估）共同构成 open-r1 的「质量与评估」收尾。建议把两者连起来看：`make test`/`make quality` 守住代码正确性，`make evaluate` 守住模型能力，二者都是 `Makefile` 暴露的标准入口。
- **贡献流程**：若你想给 open-r1 提 PR，标准节奏是 `make style && make quality && make test` 三连全绿后再提交——本讲的代码实践正是这条节奏的最小演练。
