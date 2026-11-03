# 第 7 单元·第 1 讲：RLVR 奖励函数与训练数据

> 本讲是「强化学习」单元（第 6 章）的第一讲。从本单元开始，我们终于要**修改模型权重**了——前四个单元里所有提升推理能力的方法（温度采样、CoT、自一致性、Best-of-N、自我精炼）都是推理时扩展，不动权重；而现在，我们将用强化学习让 0.6B 基座模型自己学会推理。万事开头难，RL 的「头」就是两样东西：**训练数据**和**奖励函数**。本讲把这两个零件准备好。

## 1. 本讲目标

学完本讲，你应该能够：

1. 用自己的话解释 **RLVR（Reinforcement Learning with Verifiable Rewards，可验证强化学习）** 的核心思想：为什么它可以用「规则验证器」替代 RLHF 中昂贵的学习型奖励模型。
2. 精读 `reward_rlvr`，说清它的两步链路——先**强制**从回答中提取 `\boxed{}`，再用第 3 章的 `grade_answer` 做数学等价判分——并解释为什么「正确但没有 `\boxed{}`」只能得 0 分。
3. 独立使用 `load_math_train` 获取 GRPO 训练数据，理解它的「本地缓存 + 主备双 URL」下载策略，以及它与前几讲下载函数的异同。
4. 为后续三讲（rollout 采样 → GRPO 损失 → 训练循环）准备好数据与奖励这两个前置零件。

本讲**不需要 GPU**：`reward_rlvr` 和 `load_math_train` 都是纯 Python/sympy 代码，不加载任何模型。

## 2. 前置知识

本讲建立在已有认知之上，先快速对齐：

- **推理时扩展 vs 训练时扩展**（u5、u6 各讲）：前者在生成阶段多花算力（多采样、多轮迭代），权重不变；后者在训练阶段多花算力，权重被更新。第 6 章属于后者。
- **答案提取三级回退**（u4-l2）：`extract_final_candidate` 默认策略是「先找 `\boxed{}` → 找不到就取全文最后一个数字 → 再不行返回全文」。本讲会看到 RLVR 把后两级回退**关闭**（`fallback=None`）。
- **数学等价判分**（u4-l3）：`grade_answer` 把预测与标准答案各自 `normalize_text` 后拆分成片段，逐片段做「字符串捷径 + SymPy 差为零」的等价比较，默认拒绝（不匹配就是 `False`）。
- **下载机制**（u1-l3）：`utils.download_file` 有主备双 URL、分块下载、按大小校验缓存。本讲的 `load_math_train` 是同一设计哲学的轻量变体。
- **一点点 RL 词汇**（新概念，本讲解释）：强化学习里被训练的模型叫**策略**（policy）；模型生成的一条完整回答叫 **rollout**（与 completion 同义）；验证器给 rollout 打的分叫**奖励**（reward）。

如果你还没学过 u4-l2/u4-l3，建议先补——本讲的奖励函数完全建立在那一套提取与判分管线之上。

## 3. 本讲源码地图

| 文件 | 角色 | 本讲涉及 |
| --- | --- | --- |
| [reasoning_from_scratch/ch06.py](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch06.py) | 第 6 章可复用模块（rollout、奖励、GRPO 损失、训练循环） | `load_math_train`、`reward_rlvr`，及 `compute_grpo_loss` 中的奖励调用点 |
| [ch06/01_main-chapter-code/ch06_main.ipynb](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch06/01_main-chapter-code/ch06_main.ipynb) | 章节主 notebook（教学叙述与演示） | 6.1.2 节（RLHF→RLVR）、6.4 节（数据加载）、6.6 节（奖励计算） |
| [reasoning_from_scratch/ch03.py](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch03.py) | 第 3 章评测模块（被 ch06 导入复用） | `get_last_boxed`、`extract_final_candidate`、`grade_answer`、`load_math500_test` |
| [reasoning_from_scratch/utils.py](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/utils.py) | 通用下载工具（对照用） | `download_file` |
| [ch08/02_generate_distillation_data/math_train_sample.json](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch08/02_generate_distillation_data/math_train_sample.json) | MATH 训练集的 5 题小样本（第 8 章蒸馏目录下） | 实践环节的离线替代数据 |

一个值得注意的设计：`ch06.py` 顶部直接从 `ch03` 导入判分管线，而不是重写一份——**评测用的判分标准和训练用的奖励标准是同一套代码**，这保证了「训练时拿到的奖励」与「评测时的准确率」口径完全一致。

## 4. 核心概念与源码讲解

### 4.1 RLVR 概念：从 RLHF 到可验证奖励

#### 4.1.1 概念说明

要让模型通过强化学习变强，必须先回答一个问题：**怎么给一条回答打分？**

历史上第一种答案是 **RLHF（基于人类反馈的强化学习）**，InstructGPT（2022）用它把 GPT-3 变成了初代 ChatGPT。做法是人类标注员对模型回答写偏好排序，再**训练一个单独的奖励模型**（reward model）去模仿人类打分。问题是：这个奖励模型往往本身就是一个大号 LLM，训练和维护都很贵，而且它会「漂」——奖励模型觉得好的，未必真的对。

**RLVR 换了一个思路：如果任务本身有客观对错，就不需要任何模型来打分，直接用确定性规则验证。**

- 数学题：答案对就是 1，错就是 0（用 sympy 判等价）；
- 编程题：代码能通过单元测试/能编译运行就是 1，否则 0。

这套「可自动验证的确定性奖励」就是 RLVR（Reinforcement Learning with Verifiable Rewards）。它 2025 年因 DeepSeek-R1 而爆火：R1 证明了不靠人工偏好数据、不靠学习型奖励模型，仅靠「最终答案对不对」这种二值信号，就能训练出强推理模型。 notebook 6.1.2 节对此有明确表述：RLVR 用「automatically verifiable, deterministic rewards」取代学习型奖励模型。

三个容易混淆的点，提前澄清：

1. **RLVR ≠ 某个具体算法**。它是一种奖励设计思想；本书用 GRPO 算法来消费这种奖励（GRPO 是 6.2 节的内容，属后续讲义）。
2. **过程奖励（PRM）被尝试过且失败了**。DeepSeek-R1 团队曾用过程奖励模型给中间解题步骤打分，但未成功，结论是只用最终答案的正确性奖励更好（notebook 6.6 节备注）。所以本讲的奖励**只看最终答案**。
3. **RLVR 直接作用在基座模型上**是可行的（DeepSeek-R1-Zero 路线），本书正是如此：拿第 2~5 章一直在用的 0.6B base 模型直接做 RL 训练。

用数学语言写出来，本讲的奖励函数就是一个指示函数：

\[ r(y, y^{*}) = \mathbb{1}\Big[\,\text{extract}_{\text{boxed}}(y) \neq \varnothing \;\wedge\; \text{grade}\big(\text{extract}_{\text{boxed}}(y),\, y^{*}\big) = \text{True}\,\Big] \]

其中 \(y\) 是模型 rollout，\(y^{*}\) 是标准答案。两项缺一不可：既要把答案写进 `\boxed{}`（格式），又要数学上等价（正确）。

#### 4.1.2 核心流程

RLVR 训练的最外层循环（细节留待 u7-l3/u7-l4）：

```
问题 x ──▶ 策略 π_W（Qwen3 0.6B）──▶ rollout y（模型生成的回答）
                                          │
                                          ▼
                          ┌────────────────────────────────┐
                          │ 验证器 r(y, y*)   ← 本讲主角    │
                          │  1) 强制提取 \boxed{...}        │
                          │  2) grade_answer 数学判分        │
                          └────────────────────────────────┘
                                          │  0.0 / 1.0
                                          ▼
                          GRPO 组内比较 → 优势 → 更新权重（后续讲义）
```

本讲在整条链路上的位置：提供「训练数据（x, y\*）」和「验证器 r」两个零件。它们有个共同点——**都不需要神经网络参与**：数据是静态 JSON，奖励是纯 Python + sympy。

#### 4.1.3 源码精读

RLVR 的概念叙述在 notebook 的 6.1 节，强烈建议通读：[ch06_main.ipynb](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch06/01_main-chapter-code/ch06_main.ipynb)（6.1.1 RLHF 三阶段图示 → 6.1.2 RLVR 转折 → 6.2 GRPO 与 PPO 对比，含「厨师比喻」）。

代码层面，本讲概念的最直接体现是 `ch06.py` 的导入区：

[reasoning_from_scratch/ch06.py:L5-L9](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch06.py#L5-L9)

```python
from .qwen3 import KVCache
from .ch03 import (
    extract_final_candidate, grade_answer, render_prompt
)
from .ch04 import top_p_filter
```

这几行说明：第 6 章不发明新的判分逻辑，`extract_final_candidate`（提取）与 `grade_answer`（判分）原封不动复用第 3 章评测管线；`top_p_filter` 复用第 4 章采样。**「评测怎么判对错，训练就怎么发奖励」**——这是全书代码组织的一条暗线。

奖励在 GRPO 损失中的消费点（本讲只看标星的两行，函数整体留待 u7-l3）：

[reasoning_from_scratch/ch06.py:L132-L149](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch06.py#L132-L149)

```python
    prompt = render_prompt(example["problem"])   # 用第3章模板渲染题干
    ...
    for _ in range(num_rollouts):
        # Stage 1: generate rollouts
        token_ids, prompt_len, text = sample_response(...)
        # Stage 2: compute rewards
        reward = reward_rlvr(text, example["answer"])   # ← 本讲主角在此被调用
```

也就是说：每条 rollout 文本 `text` 与训练样本的 `answer` 字段（标准答案）一起送进 `reward_rlvr`，得到 0/1 奖励。

#### 4.1.4 代码实践：三个「打分器」横向对比（源码阅读型）

1. **实践目标**：把全书出现过的三个打分/判分函数放在一起对比，体会 RLVR 奖励在「信息量」上的定位——它同时知道标准答案和格式要求。
2. **操作步骤**：
   - 打开 [reasoning_from_scratch/ch03.py:L360-L381](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch03.py#L360-L381)（`grade_answer`，评测判分）、`ch05.py` 中的 `heuristic_score`（u6-l2 讲过：纯文本三档加分 + 简洁奖励）、[reasoning_from_scratch/ch06.py:L101-L108](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch06.py#L101-L108)（`reward_rlvr`，训练奖励）。
   - 填写下面这张表（答案见本节末尾）：

   | 函数 | 是否使用标准答案 | 是否需要跑模型 | 奖励/分数范围 | 使用场景 |
   | --- | --- | --- | --- | --- |
   | `grade_answer` |  |  |  | 评测 |
   | `heuristic_score` |  |  |  | 推理时扩展择优 |
   | `reward_rlvr` |  |  |  | RL 训练 |

3. **需要观察的现象**：`heuristic_score` 完全不知道正确答案，只能奖励「有 boxed、有数字、简短」这些**代理信号**；而 `reward_rlvr` 手握标准答案，给出的是**真值信号**。
4. **预期结果**：表格能让你一句话说清「为什么训练时扩展（RLVR）上限更高」——它能优化真正的对错，而不是对错的样子。

#### 4.1.5 小练习与答案

**练习 1**：RLHF 和 RLVR 的奖励分别从哪里来？各自的主要代价是什么？

> **答案**：RLHF 的奖励来自「人类偏好数据训练出的奖励模型」，代价是要维护一个额外的大 LLM，且其打分可能偏离真实质量；RLVR 的奖励来自确定性规则验证（数学判分、代码运行），几乎零成本、可复现，但只适用于**有客观对错**的任务。

**练习 2**：为什么「解数学题」适合 RLVR，而「写一首好诗」不适合？

> **答案**：数学题的答案可以自动验证对错（sympy 等价比较），验证器简单可靠；诗的好坏没有客观标准，无法写成确定性规则，只能回到人工偏好或学习型奖励模型（RLHF/RLAIF）。

**练习 3**：DeepSeek-R1 团队为什么最终放弃过程奖励（给中间步骤打分），只用最终答案奖励？（提示：notebook 6.6 节）

> **答案**：他们对过程奖励模型的尝试未获成功，结论是只用最终答案的正确性奖励训练效果更好。过程奖励需要判断「这一步是否正确」，本身又是一个难学的信号；最终答案的二值信号虽粗，但确定且不可作弊。

### 4.2 load_math_train：GRPO 训练数据加载

#### 4.2.1 概念说明

强化学习需要「问题 + 标准答案」的配对数据。第 3 章的 MATH-500 是**评测集**，绝不能拿来训练——否则模型「背过」考题，评测准确率虚高（数据泄漏）。因此第 6 章使用 [rasbt/math_full_minus_math500](https://github.com/rasbt/math_full_minus_math500) 数据集：从原始 MATH 训练集中**剔除 MATH-500 那 500 题后**剩余的 12000 题（notebook 6.4 节输出 `Dataset size: 12000`）。

每条数据是个字典，notebook 打印的 `math_train[4]` 显示字段有：`problem`（题干）、`answer`（标准答案，如 `"6"`）、`level`（难度）、`type`（题型，如 `"Algebra"`）、`solution`（人类参考解答）、`unique_id`（编号）。

关键取舍：**训练只用 `problem` 和 `answer` 两个字段**。`solution` 明明就在那里，为什么不用？notebook 6.4 节说得很清楚：RLVR 要让模型**自由探索解法**，而不是模仿某一种特定解法和文风——模仿参考解答是第 8 章蒸馏（SFT）的路线，两条路线刻意分开。

#### 4.2.2 核心流程

`load_math_train` 的加载策略（与 u1-l3 讲过的 `download_file` 同一哲学，轻量实现）：

```
load_math_train(local_path="math_train.json", save_copy=True)
  │
  ├─ 本地文件已存在？──是──▶ json.load 读盘 ──▶ 返回（完全不联网）
  │
  └─ 否 ──▶ requests.get(主 URL: GitHub raw, timeout=30)
               │                     │
             成功                 抛 RequestException
               │                     │ 打印 "Using backup URL."
               │                     ▼
               │           requests.get(备用 URL: Backblaze B2)
               │                     │
               └────────┬────────────┘
                        ▼
                  data = r.json()（整段解析）
                        │
            save_copy=True？──是──▶ json.dump(indent=2) 落盘备份
                        ▼
                  返回 data（12000 条 dict 的列表）
```

#### 4.2.3 源码精读

[reasoning_from_scratch/ch06.py:L19-L50](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch06.py#L19-L50)

```python
def load_math_train(local_path="math_train.json", save_copy=True):
    local_path = Path(local_path)

    url = (
        "https://raw.githubusercontent.com/rasbt/"
        "math_full_minus_math500/refs/heads/main/"
        "math_full_minus_math500.json"
    )
    backup_url = (
        "https://f001.backblazeb2.com/file/reasoning-from-scratch/"
        "MATH/math_full_minus_math500.json"
    )
```

函数签名带两个开关：`local_path` 指定缓存文件名，`save_copy=True` 表示下载后把 JSON 存到本地，下次运行免下载。上面定义了主 URL（GitHub raw）与备用 URL（Backblaze B2 对象存储）——GitHub 在部分网络环境不可达时自动换源。

[reasoning_from_scratch/ch06.py:L32-L44](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch06.py#L32-L44)

```python
    if local_path.exists():
        with local_path.open("r", encoding="utf-8") as f:
            data = json.load(f)
    else:
        try:
            r = requests.get(url, timeout=30)
            r.raise_for_status()
        except requests.RequestException:
            print("Using backup URL.")
            r = requests.get(backup_url, timeout=30)
            r.raise_for_status()

        data = r.json()
```

这段是「本地优先」逻辑：文件存在就直接读盘返回，**不校验内容、不比对大小**；不存在才联网。主 URL 请求包在 `try` 里，任何 `requests.RequestException`（DNS 失败、超时、4xx/5xx 后的 `raise_for_status` 等）都会落入备用分支并打印提示。注意**备用请求没有再包 `try`**——两个源都失败时异常直接向上抛，调用者拿到的就是清晰的 requests 报错。另外与 `download_file` 的「分块写盘」不同，这里 `r.json()` 一次性把整个响应解析成 Python 对象，没有进度条——因为数据集是一个不大的 JSON 文档，而非数 GB 的权重文件。

[reasoning_from_scratch/ch06.py:L46-L50](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch06.py#L46-L50)

```python
        if save_copy:
            with local_path.open("w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)

    return data
```

下载成功且 `save_copy=True` 时，以缩进格式落盘（方便 diff 与人工查看），最后返回数据。

与仓库里另外两个下载器横向对比（结构对齐 u1-l3 的结论）：

| | `utils.download_file` | `ch03.load_math500_test` | `ch06.load_math_train` |
| --- | --- | --- | --- |
| 主 URL | Hugging Face | GitHub raw（[ch03.py:L423-L441](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch03.py#L423-L441)） | GitHub raw |
| 备用 URL | Backblaze B2（`backup_url` 参数传入） | **无** | Backblaze B2（硬编码） |
| 下载方式 | `stream=True` 分块 + 进度显示 | `r.json()` 整段 | `r.json()` 整段 |
| 缓存判定 | 本地大小 == Content-Length | 文件存在即可 | 文件存在即可 |
| 数据规模 | 1.2 GB 级权重文件 | 500 题 | 12000 题 |

（`download_file` 见 [utils.py:L53](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/utils.py#L53)。）可以看到：数据集越小，加载器越简单；只有大文件才值得流式下载与大小校验。

#### 4.2.4 代码实践：加载数据并统计答案类型

1. **实践目标**：跑通训练数据加载，验证缓存行为，并对 `answer` 字段的形态建立直觉（它将作为 `reward_rlvr` 的 `ground_truth` 输入）。
2. **操作步骤**：写一个脚本（示例代码，非项目原有文件）：

   ```python
   # practice_u7_l1_data.py —— 示例代码
   import re
   from collections import Counter
   from reasoning_from_scratch.ch06 import load_math_train

   math_train = load_math_train()            # 首次运行需联网下载 12000 题
   print("数据集大小:", len(math_train))
   print("首条字段:", sorted(math_train[0].keys()))

   # 再调用一次，验证本地缓存：应瞬时返回，且不打印 "Using backup URL."
   math_train_again = load_math_train()
   print("二次加载条数一致:", len(math_train_again) == len(math_train))

   def classify(ans: str) -> str:
       if re.fullmatch(r"-?\d+", ans):      return "整数"
       if re.fullmatch(r"-?\d*\.\d+", ans): return "小数"
       if "\\frac" in ans:                  return "分数"
       if "\\sqrt" in ans:                  return "根式"
       if re.search(r"[A-Za-z]", ans):      return "含字母/文本"
       if re.search(r"[,;]", ans):          return "多元(逗号分隔)"
       return "其他"

   dist = Counter(classify(ex["answer"]) for ex in math_train[:50])
   print("前 50 题答案类型分布:")
   for kind, n in dist.most_common():
       print(f"  {kind}: {n}")
   ```

   **离线替代方案**：如果网络受限，可直接读取仓库自带的 5 题小样本（字段结构与全量集一致，但没有 `solution` 字段）：

   ```python
   import json
   with open("ch08/02_generate_distillation_data/math_train_sample.json") as f:
       math_train = json.load(f)   # 5 条，answer 依次为 "0","98","4","17","6"
   ```

3. **需要观察的现象**：
   - 首次调用会停顿（下载），第二次调用瞬时完成且无任何网络提示；
   - 全量集首条字段应包含 `problem/answer/level/solution/type/unique_id` 六个键；
   - 类型分布中哪一类占比最高。
4. **预期结果**：数据集大小为 **12000**（notebook 6.4 节已验证）；二次加载一致为 `True`；小样本前 5 题答案全是整数字符串。**前 50 题的类型分布数值待本地验证**——基于 MATH 数据集以数值答案为主的常识，预期「整数」占大头，伴有少量分数/根式/文本型答案。

#### 4.2.5 小练习与答案

**练习 1**：本地已存在 `math_train.json` 时，`load_math_train()` 还会访问网络吗？如果本地文件是残缺的（比如上次下载中途被杀进程留下半个文件），会发生什么？

> **答案**：不会联网——命中 `local_path.exists()` 分支直接 `json.load`。但因为缓存判定只看「文件是否存在」，不看大小或完整性，残缺文件会导致 `json.load` 抛 JSON 解码异常。这与 `download_file` 的「大小 == Content-Length」校验不同；修复方法是删掉本地残缺文件重新下载。

**练习 2**：主 URL 请求失败后，代码如何切换到备用 URL？如果备用 URL 也失败呢？

> **答案**：`except requests.RequestException` 捕获主 URL 的任何请求异常，打印 `"Using backup URL." 后再请求备用地址。备用请求没有 try 包裹，若也失败，异常直接抛给调用者（含 `raise_for_status` 的 HTTP 错误信息），不会返回半成品数据。

**练习 3**：为什么训练集必须剔除 MATH-500？这 500 题在项目里扮演什么角色？

> **答案**：MATH-500 是第 3 章以来的**评测基准**；若训练集中含有评测题，模型会「背题」，评测分数虚高且无法泛化（数据泄漏）。因此 `math_full_minus_math500` 从 MATH 全集中显式去掉了这 500 题，训练与评测严格不重叠。

### 4.3 reward_rlvr：强制 `\boxed{}` 的正确性奖励

#### 4.3.1 概念说明

`reward_rlvr` 是 RLVR 思想在本项目的落地，只有 8 行。它同时是**显式的正确性奖励**和**隐式的格式奖励**：

- **显式**：答案与标准答案数学等价 → 1.0，否则 0.0；
- **隐式**：只有答案写在 `\boxed{}` 里才有资格参与判分（notebook 6.6 节原话：奖励 1.0 只在最终答案为 `\boxed{}` 格式时给出，`fallback=None` 实现了这一点）。

为什么强制格式？因为奖励必须是**机器可定位**的：一条自由文本里可能到处都是数字，不约定「答案在 `\boxed{}` 里」，验证器就无法可靠地找到「最终答案」。这个约定从第 3 章的 `render_prompt`（提示模板要求 `\boxed{ANSWER}`）一路贯穿到 RLVR 奖励——提示模板要求的输出契约，奖励函数来检查。

回忆 u4-l2 的三级回退：评测时「没有 boxed 也可以抓最后一个数字兜底」是合理的宽容；但**训练时宽容反而有害**——如果「正确但没写 `\boxed{}`」也能得分，模型就没有学会结构化输出格式的压力。所以 `reward_rlvr` 把回退**整体关闭**（`fallback=None`），只认 `\boxed{}`。

#### 4.3.2 核心流程

```
reward_rlvr(answer_text, ground_truth)
  │
  ├─ 第一步：extract_final_candidate(text, fallback=None)
  │     ├─ 文本中有 \boxed{...} ──▶ 返回花括号内的内容（保留 LaTeX 原文）
  │     └─ 没有 \boxed{} ────────▶ 返回 ""（数字回退/全文回退均被关闭）
  │
  ├─ extracted 为空？ ──是──▶ return 0.0        # 格式不合格，直接出局
  │
  └─ 第二步：grade_answer(extracted, ground_truth)
        ├─ True  ──▶ return 1.0
        └─ False ──▶ return 0.0
```

四种典型情形（标准答案为 `83`，即 notebook 6.5/6.6 节的例题「Half the value of 3x−9 is x+37. What is the value of x?」，题干原文用 LaTeX 花括号包裹公式）：

| 模型输出 | 第一步提取 | 第二步判分 | 奖励 | 失败原因 |
| --- | --- | --- | --- | --- |
| `\boxed{83}` | `"83"` | True | **1.0** | — |
| `The correct answer is \boxed{83}` | `"83"` | True | **1.0** | —（boxed 前有废话没关系，取**最后一个** boxed） |
| `The final answer is 83` | `""` | 不进入 | **0.0** | 无格式：答案对了但没有 `\boxed{}` |
| `We get \boxed{38}` | `"38"` | False | **0.0** | 无正确性：格式对了但数值错 |

notebook 6.6 节实际运行了后三条加第一条变体，输出恰好是 `1.0 / 1.0 / 0.0 / 0.0`——这正是 GRPO 需要的「组内有分化」的奖励分布。

#### 4.3.3 源码精读

[reasoning_from_scratch/ch06.py:L101-L108](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch06.py#L101-L108)

```python
def reward_rlvr(answer_text, ground_truth):
    extracted = extract_final_candidate(
        answer_text, fallback=None  # Require \boxed{}
    )
    if not extracted:
        return 0.0
    correct = grade_answer(extracted, ground_truth)
    return float(correct)
```

逐行看：

- **L102-104**：调用第 3 章的提取器，但传 `fallback=None`——注释 `# Require \boxed{}` 点明意图：强制要求 boxed 格式。
- **L105-106**：`extracted` 为空字符串（没有 boxed，或输入本身是空文本）时直接返回 `0.0`，**连判分都不进**。`not extracted` 对 `""` 和 `None` 都成立，是个防御性写法。
- **L107-108**：`grade_answer` 返回 Python `bool`；`float(correct)` 转成 `1.0`/`0.0` 浮点数，方便后续拼成张量做组内统计（u7-l3 的 `torch.tensor(roll_rewards)`）。

第一步的内部路径（为什么 `fallback=None` 就能关闭回退）：

[reasoning_from_scratch/ch03.py:L186-L205](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch03.py#L186-L205)

```python
def extract_final_candidate(text, fallback="number_then_full"):
    result = ""

    if text:
        # Prefer the last boxed expression if present
        boxed = get_last_boxed(text.strip())
        if boxed:
            result = boxed.strip().strip("$ ")

        # If no boxed expression, try fallback
        elif fallback in ("number_then_full", "number_only"):
            m = RE_NUMBER.findall(text)
            if m:
                result = m[-1]
            elif fallback == "number_then_full":
                result = text
    return result
```

走查 `fallback=None` 时的两条路径：文本含 `\boxed{}` → `get_last_boxed`（嵌套花括号计数提取**最后一个** boxed，见 [ch03.py:L148-L163](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch03.py#L148-L163)）命中，返回括号内内容；文本不含 `\boxed{}` → `elif fallback in ("number_then_full", "number_only")` 判 `None in (...)` 为 `False`，**数字回退与全文回退双双跳过**，`result` 保持 `""` 返回。空文本 `""` 则连 `if text:` 都进不去，同样返回 `""`。两条路都汇入 `reward_rlvr` 的 `0.0` 出口。

第二步判分（u4-l3 已精读，这里只看它在奖励链路中的角色）：

[reasoning_from_scratch/ch03.py:L360-L381](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch03.py#L360-L381)

```python
def grade_answer(pred_text, gt_text):
    result = False  # Default outcome if checks fail
    ...
        if (gt_parts and pred_parts
           and len(gt_parts) == len(pred_parts)):
            result = all(
                equality_check(gt, pred)
                for gt, pred in zip(gt_parts, pred_parts)
            )
    return result
```

预测与标准答案各自 `normalize_text` + `split_into_parts` 后**逐片段**做 SymPy 等价比较；任何一步失败都保持默认 `False`（默认拒绝）。所以 `"38"` vs `"83"` 字符串不等、数值不等 → `False` → 奖励 0.0；而 `\boxed{\frac{1}{2}}` vs `0.5` 这类写法差异会被 sympy 化简抹平 → `True` → 奖励 1.0。判分的全部细节（隐式乘法、多元答案拆分、异常兜底）见 u4-l3。

最后把视角拉高：`reward_rlvr` 在整条 GRPO 链路里只是「Stage 2」，它上游吃 `sample_response` 的输出文本、下游把 0/1 喂给组内优势计算（[ch06.py:L167-L171](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch06.py#L167-L171)，`advantages = (rewards - rewards.mean()) / (rewards.std() + 1e-4)`）。奖励是 0/1，优势是组内标准化后的带符号实数——这个转换是 u7-l3 的主菜。

#### 4.3.4 代码实践：四象限奖励实验

1. **实践目标**：亲手验证奖励函数对「格式 × 正确性」四种组合的判定，确认隐式格式奖励的存在。
2. **操作步骤**：无需加载模型，CPU 秒级完成（示例代码）：

   ```python
   # practice_u7_l1_reward.py —— 示例代码
   from reasoning_from_scratch.ch06 import reward_rlvr

   GROUND_TRUTH = "83"   # notebook 6.5 节例题的标准答案
   candidates = [
       ("boxed 且正确",     r"\boxed{83}"),
       ("boxed 但错误",     r"\boxed{38}"),
       ("正确但无 boxed",   "The final answer is 83"),
       ("空文本",           ""),
   ]
   for name, text in candidates:
       print(f"{name:12s} -> reward = {reward_rlvr(text, GROUND_TRUTH)}")
   ```

   再做一个小延伸，体会 `fallback` 开关的差别（对照 u4-l2 的评测行为）：

   ```python
   from reasoning_from_scratch.ch03 import extract_final_candidate
   t = "The final answer is 83"
   print(repr(extract_final_candidate(t, fallback=None)))             # RLVR：('')
   print(repr(extract_final_candidate(t, fallback="number_then_full")))  # 评测：'83'
   ```

3. **需要观察的现象**：四条奖励值；同一个文本在两种 `fallback` 设定下的提取结果差异。
4. **预期结果**：四条奖励依次为 `1.0 / 0.0 / 0.0 / 0.0`（前三条与 notebook 6.6 节实际输出一致；空文本按 L105 的 `not extracted` 路径必为 `0.0`）。延伸实验输出 `''` 与 `'83'`——同一句话，评测管线「宽容地」抓到数字，RLVR 奖励「严格地」拒绝发分。精确的打印格式待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：`The final answer is 83` 数学上完全正确，为什么只能得 0 分？这个设计在训练中会「教会」模型什么？

> **答案**：因为 `fallback=None` 关闭了数字/全文回退，没有 `\boxed{}` 的回答在第一步就返回 `0.0`。训练久了，模型会学到「必须把最终答案写进 `\boxed{}`」这一格式契约——这正是隐式格式奖励的作用：不仅奖励答对，还奖励**以可验证的方式**答对。

**练习 2**：`We get \boxed{38}` 是在哪一步、因为什么拿到 0 分的？

> **答案**：第一步提取成功（得到 `"38"`，格式合格），失败在第二步：`grade_answer("38", "83")` 中字符串捷径不等、SymPy 化简差不为零，默认拒绝返回 `False`，于是 `float(False) = 0.0`。它与「无 boxed」殊途同归，但失败环节不同——一个挂在格式，一个挂在正确性。

**练习 3**：`reward_rlvr` 里的 `float(correct)` 能删掉吗（直接 `return correct`）？为什么作者要转一下？

> **答案**：功能上删掉也能用（`bool` 可参与算术），但下游 `compute_grpo_loss` 要把多次 rollout 的奖励拼成 `torch.tensor(roll_rewards)` 做均值和标准差；显式转成 `1.0/0.0` 浮点让奖励的类型契约稳定为 float，避免 bool 张量在统计计算中引发歧义，也让日志打印（`reward={sample['reward']:.3f}`）格式统一。

## 5. 综合实践：不碰模型的「奖励实验台」

把本讲两个模块串起来，提前摸一下 GRPO 的「组」概念——全程不需要 GPU，几秒跑完。

**任务**：取仓库自带的 5 题小样本，对每道题手工构造 3 条「假想 rollout」（正确且有格式 / 有格式但错误 / 正确但无格式），用 `reward_rlvr` 打分，再按 GRPO 公式手算组内优势，观察奖励如何变成学习信号。

1. **操作步骤**（示例代码）：

   ```python
   # practice_u7_l1_bench.py —— 示例代码
   import json
   import torch
   from reasoning_from_scratch.ch06 import reward_rlvr

   with open("ch08/02_generate_distillation_data/math_train_sample.json") as f:
       problems = json.load(f)[:3]

   for ex in problems:
       gt = ex["answer"]                 # 如 "0"、"98"、"4"
       wrong = str(int(gt) + 1)          # 构造一个错误答案
       outputs = [
           rf"\boxed{{{gt}}}",           # 正确且有格式（注意 f-string 双花括号转义）
           rf"\boxed{{{wrong}}}",        # 有格式但错误
           f"The answer is {gt}",        # 正确但无格式
       ]
       rewards = torch.tensor([reward_rlvr(o, gt) for o in outputs])
       adv = (rewards - rewards.mean()) / (rewards.std() + 1e-4)   # GRPO 组内标准化
       print(f"unique_id={ex['unique_id']:>2}  gt={gt:>3}  "
             f"rewards={rewards.tolist()}  advantages={[round(a, 4) for a in adv.tolist()]}")
   ```

   若已下载全量训练集，可把 `problems` 换成 `load_math_train()[:3]`（注意全量集才有 `solution` 字段，本题只用到 `answer`）。

2. **需要观察的现象**：
   - 每组奖励都是 `[1.0, 0.0, 0.0]` 的模式；
   - 优势大约是「一个正值 + 两个相等的负值」（数值待本地验证，按均值 1/3、标准差约 0.577 推算，约为 `+1.15 / -0.58 / -0.58`）；
   - 试着把三条输出全部换成不含 `\boxed{}` 的文本再跑一次：奖励全 0，**优势也全 0**。
3. **预期结果与思考**：奖励 `1.0/0.0/0.0` 经过 \((r_i - \mu_r)/(\sigma_r + \epsilon)\) 标准化后，变成「正的优势鼓励、负的优势抑制」——这正是下一讲 GRPO 损失 `-(advantage * logp).mean()` 的输入。而「全 0 奖励 → 全 0 优势」对应 notebook 6.7 节的重要结论：**一组 rollout 全对或全错时模型不更新**，只有组内有分化才有学习信号。这也解释了为什么 `reward_rlvr` 必须严格：奖励越有区分度，组内越容易分化。

## 6. 本讲小结

- **RLVR 用确定性验证器替代 RLHF 的学习型奖励模型**：数学对错可自动判分，奖励廉价、可复现、不可作弊；DeepSeek-R1 证明了这条路可行，且过程奖励（PRM）反而不如只用最终答案奖励。
- **`load_math_train`（ch06.py:L19-L50）** 加载剔除 MATH-500 后的 12000 题训练集：本地缓存优先、GitHub 主 URL 失败自动切 Backblaze B2 备用 URL；训练只消费 `problem` 与 `answer` 两个字段，刻意不用 `solution`（让模型自由探索）。
- **`reward_rlvr`（ch06.py:L101-L108）两步链路**：`extract_final_candidate(fallback=None)` 强制提取 `\boxed{}` → 提取失败直接 0 分 → `grade_answer` 做数学等价判分 → 返回 `1.0/0.0`。
- 奖励同时是**显式正确性奖励**与**隐式格式奖励**：「正确但没写 `\boxed{}`」与「写了 `\boxed{}` 但答错」同样得 0 分，失败环节不同。
- 奖励函数**原样复用第 3 章评测管线**（`ch06.py` 顶部导入 `extract_final_candidate, grade_answer, render_prompt`），保证训练奖励与评测准确率同一口径。
- 本讲的两个零件——数据与奖励——将在下一讲被 `sample_response` 串起来：模型对 `problem` 采样 rollout，rollout 与 `answer` 进奖励函数。

## 7. 下一步学习建议

- **下一讲 u7-l2「Rollout 采样：sample_response」**：精读 [ch06.py:L53-L98](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch06.py#L53-L98)，看 KV cache、温度缩放、top-p 过滤如何被压缩成一个为 RL 服务的精简生成函数（`@torch.no_grad()` 的选择、EOS 提前停止、返回 `token_ids + prompt_len + text` 三件套的设计意图）。
- 之后再进入 u7-l3（GRPO 损失：优势计算、序列 logprob、策略梯度）与 u7-l4（训练循环）。建议按此顺序预习 notebook 6.5、6.7~6.11 节。
- 想回顾判分管线细节的读者可重读 u4-l2（提取与归一化）、u4-l3（SymPy 等价判定）；第 7 章还会把本讲的「隐式格式奖励」升级为**显式**的 `<think>` 标签格式奖励（u8-l4），届时对照体会两种做法的差别。
