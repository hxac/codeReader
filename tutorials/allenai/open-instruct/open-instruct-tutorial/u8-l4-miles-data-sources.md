# 数据源、混合、超长排除与源准备

> 本讲对应 MILES 单元第四讲。前两讲（u8-l2、u8-l3）我们看过了「TOML → RunSpec → RunConfig」的配置编译链，以及 workflow/driver 的执行编排。本讲进入这两条链的交汇点：**数据在 GPU 任务里是如何被准备成「可训练的提示集」的**——多数据源如何声明与混合、验证器目标如何随样本携带、一份全新数据集如何经 source preparation 入库，以及三个近期更新的重点：`data.over_length = "exclude"` 的训练侧超长提示排除策略、**注册表 `grading` 声明的准备期语法校验与 named-judge 绑定对受信判分声明的继承**（PR [#2250](https://github.com/allenai/open-instruct/pull/2250)），以及新增的 `nemo_abstention_v1` 源适配器（PR [#2252](https://github.com/allenai/open-instruct/pull/2252)，判分语义详见 u8-l13）。

## 1. 本讲目标

学完本讲，你应该能够：

1. 读懂 `[data]` 小节的五种输入模式（`sources` / `tasks` / `rl_manifest` / `prompt_data` / `recipe`），并写出一份合法的多源 mixture 声明。
2. 解释一条训练样本的完整结构：渲染后的 `input`、验证器目标 `metadata.verifiers`、可信注册表 `verifiers.json` 三者如何分工。
3. 说出 source preparation（`source_preparation.py` + `source_catalog.py`）在「原始 JSONL → 可训练 source 包」这条流水线里各做什么、为什么所有输入都要钉哈希，并了解新增的 `nemo_abstention_v1` 适配器守住哪些契约。
4. 掌握 `data.over_length = "exclude"` 的训练侧排除语义——held-out 数据仍然严格失败、排除计数如何按 source/pool 记入 `manifest.json` 的 `prompt_length` 块、被掏空的配置 source（或池）为何直接导致准备失败，以及策略与上限为何是不可变准备身份的一部分。
5. **掌握本轮更新的核心变化**：准备期用 `grading.validate_spec` 校验注册表 `grading` 声明的**语法**（不导入工厂、不猜测默认值），objective×scope 的**组合**约束留给加载期；named-judge 绑定替换工厂配置时必须继承原注册表的受信 `grading` 声明，丢弃过声明的陈旧缓存会被拒绝。

## 2. 前置知识

- **RunSpec 与编译链（承接 u8-l2）**：一份 TOML run 文件被 `RunSpec` 加载并编译出 `RunConfig`；`[data]` 小节是其中的数据契约。本讲只关心 `[data]` 的字段语义与它在准备期如何被消费。
- **准备期（preparation）vs 训练期**：MILES 的数据准备发生在 GPU 任务的 CPU 侧（workflow 的 `data_preparation` 阶段，见 u8-l3 的 4.1 节），产出**不可变**（immutable）的目录：一旦写好，resume 时只做哈希核验、绝不重采样。这是贯穿本讲的第一个关键词。
- **可验证奖励（承接 u6-l3）**：RLVR 的每条训练 prompt 都要带一个「怎么判分」的目标（target），比如一道数学题的参考答案。MILES 的约定是：**样本只携带注册表里已登记的验证器名字 + 目标参数，绝不携带任意代码**；名字到工厂函数的映射放在一份可信的 `verifiers.json` 注册表里（判分细节在 u8-l6 展开）。
- **判分输入契约（承接 u8-l2 的 `reward_final_answer_only` / `reward_reasoning_mode`）**：本轮统一判分输入后，注册表条目还可以携带一个 `grading` 声明（`objective`/`scope`/`require_response`），描述这个验证器消费「思考段还是交付回答」。准备期只校验它的语法；完整契约在 u8-l12 展开，本讲 4.5 讲准备侧的那一半。
- **rollout 提示长度上限**：编译链会推出 `rollout_max_prompt_len`，默认等于 `inference.max_context_length - inference.max_response_length`（见 [open_instruct/miles/configuration/run_spec.py:650-657](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/miles/configuration/run_spec.py#L650-L657)）。准备期用它逐条检查渲染后的 prompt token 数——这正是 `over_length` 的作用边界。
- **池（pool）**：多源 mixture 之上还可以把若干 source 或行标签归入命名池并配采样权重（`data.pools` + `data.sampling`）。排除计数会分别按 source 和 pool 两个维度记账；池内还可以配置优先级排序（`data.sampling.priority`），准备期会随之做逐行校验（见 4.4），所以需要知道池的存在，细节不必深究。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [open_instruct/miles/datasets/mixture.py](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/miles/datasets/mixture.py) | mixture 配置的共享校验：`validate_sources`（名字/配比）与 `source_data`（源级配置继承） |
| [open_instruct/miles/datasets/run_data.py](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/miles/datasets/run_data.py) | 本讲主文件：`validate_data` 模式分派、`prepare_data` 不可变准备、验证器目标携带、`over_length` 排除与计数、`grading` 语法校验与 named-judge 继承 |
| [open_instruct/miles/rewards/grading.py](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/miles/rewards/grading.py) | 统一判分输入层：`validate_spec`（准备期语法校验）、`GradingPolicy`（加载期组合校验）与 `ResponseParts` 切分（详见 u8-l12） |
| [open_instruct/miles/datasets/source_preparation.py](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/miles/datasets/source_preparation.py) | 版本化源适配器：把钉哈希的原始 JSONL 经 reviewed 适配器转成 source 包（prompts/verifiers/dispositions/catalog），本轮新增 `nemo_abstention_v1` |
| [open_instruct/miles/datasets/source_catalog.py](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/miles/datasets/source_catalog.py) | 把 catalog + `NAME=FRACTION` 权重解析核验成一份 `[data]` TOML 片段 |
| [open_instruct/miles/execution/workflow.py](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/miles/execution/workflow.py) | 调用方：`data_preparation` 阶段把 `spec.data` 连同 `rollout_max_prompt_len` 交给 `prepare_data` |
| [open_instruct/test_miles_over_length.py](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/test_miles_over_length.py) | `over_length` 排除策略的配套单测（CPU 即可运行），是本讲实践的验证工具 |
| [open_instruct/test_miles_run_data.py](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/test_miles_run_data.py) | `prepare_data` 的主单测，本轮新增 grading 语法校验与 named-judge 继承的四个测试 |
| [docs/miles/data-and-evaluation.md](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/docs/miles/data-and-evaluation.md) | 研究者视角的数据文档：模式表、多源示例、`Run-specific prompt limits` 一节与池采样优先级 |
| [docs/miles/source-preparation.md](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/docs/miles/source-preparation.md) | source preparation 的计划文件格式与适配器总表（末尾新增 abstention 一节） |

## 4. 核心概念与源码讲解

### 4.1 mixture 混合：多源声明与固定配比

#### 4.1.1 概念说明

MILES 的多源混合回答的问题是：**「一份 run 想同时用 N 个来源的数据，各自的采样份额怎么声明、怎么保证声明是对的？」** 答案是 `[data]` 下写 `[[data.sources]]` 数组：每个源一个安全标识名 `name`、一个配比 `fraction`，再带上该源自己的输入模式字段（prepared JSONL、命名任务或 rl_manifest 都可以）。

两个关键约束：

1. **配比必须归一**：所有 `fraction` 之和必须等于 1（浮点误差容忍到 \(10^{-9}\)），且每个 fraction 落在 \((0, 1]\) 内、必须是有限数。归一化检查的数学形式是：

\[
\Big|\textstyle\sum_{i} f_i - 1\Big| \le 10^{-9},\quad f_i \in (0, 1],\quad f_i \in \mathbb{R}
\]

2. **名字必须唯一且「安全」**：只允许字母、数字、下划线、连字符（正则 `[a-zA-Z0-9_-]+`）。名字会成为分区文件名（`source-{name}.jsonl`）和 manifest 计数的键，不允许出现路径分隔符之类的字符。

另外，当配置了 `data.pools`（池化采样）时走的是 **pooled 模式**：此时源不再自带 `fraction`（份额由 `data.sampling` 的池权重决定），带了反而报错。

#### 4.1.2 核心流程

```text
[data] TOML
  ├─ validate_data（run_data.py）
  │    ├─ 恰好选择一种模式：tasks / sources / recipe / rl_manifest / prompt_data
  │    └─ 若是 sources 模式：
  │         mixture.validate_sources(sources, pooled=有池?)
  │         逐源：禁止源内嵌套 sources/seed/shuffle/over_length
  │                递归 validate_data(mixture.source_data(source, data))
  ├─ prepare_data._sources（准备期）
  │    ├─ 逐源加载子模式 → 得到分区（train + 若干 eval split）
  │    ├─ 分区改名：train → source-{name}，eval-{label} → eval-{name}-{label}
  │    ├─ 合并各源的验证器注册表，同名不同定义 → 报冲突
  │    └─ 每个 source-{name} 必须有非空训练数据
  └─ 产出：source-{name}.jsonl（每源一个）+ mixture.json（份额与行数）
       → 训练期 mixture_provider 读 mixture.json 再次 validate_sources 并核对行数
```

注意「源内递归」这一点：每个 `[[data.sources]]` 条目剥掉 `name`/`fraction`/`pool` 三个字段后，剩下的就是一个完整的子 `[data]` 配置（继承顶层的 `seed` 和 `shuffle`），会被再次走 `validate_data`——所以一个源可以用 prepared JSONL，另一个源用命名任务，互相独立。

#### 4.1.3 源码精读

**配比与名字校验**。[open_instruct/miles/datasets/mixture.py:9-29](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/miles/datasets/mixture.py#L9-L29) 的 `validate_sources`：非空列表、每项是表、名字匹配安全正则且不重复；pooled 模式禁止 `fraction`（L19-23，配额改由 `data.sampling` 表达）；普通模式逐项检查 fraction 类型与取值范围（L24-25），最后用 `math.fsum` 累加并要求与 1 的差不超过 \(10^{-9}\)（L28-29）。用 `math.fsum` 而不是普通累加是为了把浮点求和误差降到最低，只留出容差处理不可避免的表示误差。

**源级配置继承**。[open_instruct/miles/datasets/mixture.py:32-35](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/miles/datasets/mixture.py#L32-L35) 的 `source_data` 把顶层的 `seed`（默认 17）与 `shuffle`（默认 true）和源自身剩余字段合并成子 `[data]` 字典——这就是「源继承全局种子」的实现点。

**禁止源内覆盖全局字段**。[open_instruct/miles/datasets/run_data.py:227-232](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/miles/datasets/run_data.py#L227-L232)：源条目里出现 `sources`/`seed`/`shuffle`/`over_length` 任何之一都直接报 `InputError`——`over_length` 是全局策略（本讲 4.4），**不允许按源覆盖**，文档也明确「per-source overrides are rejected」。

**多源加载与注册表合并**。[open_instruct/miles/datasets/run_data.py:659-683](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/miles/datasets/run_data.py#L659-L683) 的 `_sources`：对每个源调 `_load(mixture.source_data(...))` 得到分区、溯源信息和验证器注册表；`source-{name}` 分区名冲突会报错（L678-679）；两个源注册了同名验证器但定义不同会报「conflicting reward registry entry」（L670-675）——同名同义才允许共享。每个源的训练分区必须非空（L665-666）。

**mixture.json 落盘**。[open_instruct/miles/datasets/run_data.py:928-969](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/miles/datasets/run_data.py#L928-L969)：准备期末尾把每个源的 `name`、可选 `pool`/`fraction`、相对路径、文件 SHA-256 与**行数**写进 `mixture.json`（配池时还会写 `pools` 与 `sampling`，并用 `indexed_rows` 预建索引）。训练期的 `mixture_provider.load`（[open_instruct/miles/rollout/mixture_provider.py:88-118](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/miles/rollout/mixture_provider.py#L88-L118)）会再次 `validate_sources` 并按 manifest 里的哈希与行数核对实际文件——所以 4.4 节的排除必须让「manifest 描述的是排除后的文件」，否则运行期行数检查会炸。

**文档示例**。[docs/miles/data-and-evaluation.md:102-129](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/docs/miles/data-and-evaluation.md#L102-L129) 给出一个 90% Dolci Think（prepared JSONL）+ 10% Sudoku（命名任务）的双源示例，可直接当作实践模板；文档里的单节点示例路径本轮已随维护示例重组更新为 [configs/miles/examples/tiny/node.toml](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/configs/miles/examples/tiny/node.toml)（[docs/miles/data-and-evaluation.md:47](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/docs/miles/data-and-evaluation.md#L47)）。

#### 4.1.4 代码实践

**实践目标**：为一份自制数学题集写一份双源 mixture 声明，并用 `validate` 子命令确认它能被 MILES 接受（全程 CPU，不需要 GPU）。

1. 先手工造一份最小 prepared JSONL（示例代码，保存为 `mymath/train.jsonl`，两行即可）和一个最小注册表 `mymath/verifiers.json`：

   ```json
   {"input": "USER: 2+2=? ASSISTANT:", "label": "4",
    "metadata": {"verifiers": [{"name": "gsm8k", "target": "4", "weight": 1.0}],
                 "query": "2+2=?"}}
   ```

   ```json
   {"gsm8k": {"factory": "open_instruct.miles.rewards.verifiers.GSM8KVerifier"}}
   ```

   （行内的 `name: "gsm8k"` 必须能在注册表里找到；正式准备时还会要求渲染模板哈希等字段，见 4.2。）

2. 写一份 run TOML（示例代码，保存为 `mymath-run.toml`，模型等其余字段可从 `configs/miles/examples/tiny/dev.toml` 复制）：

   ```toml
   [data]
   seed = 17
   shuffle = true

   [[data.sources]]
   name = "mymath"
   fraction = 0.75
   prompt_data = "/abs/path/mymath/train.jsonl"
   reward_config = "/abs/path/mymath/verifiers.json"

   [[data.sources]]
   name = "sudoku"
   fraction = 0.25
   tasks = [{ task = "synlogic-sudoku", train_count = 8 }]
   ```

3. 在仓库根目录运行 `uv run python -m open_instruct.miles validate mymath-run.toml`，确认通过。
4. 故意把两个 fraction 改成 `0.7` 和 `0.2`，再跑一次 `validate`，观察「fractions must sum to 1」的报错；再把某个源名改成 `my math`（带空格），观察安全标识符报错。
5. **需要观察的现象**：合法配置静默通过；非法配比/名字在 validate 阶段就被拦截，不会等到 GPU 任务里才失败。实际输出待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `fraction` 校验要求「有限数」并显式排除布尔值之外的类型检查（`type(fraction) not in (int, float)`）？

**答案**：`float("nan")` 是浮点数但不是有限数，`math.isfinite` 把它挡在外面；用 `type(...) not in (...)` 而不是 `isinstance` 是因为 Python 里 `bool` 是 `int` 的子类，`isinstance(True, int)` 为真——`type` 精确匹配可以拒绝把 `true` 当配比 1 传入的 TOML 手误。

**练习 2**：两个源都注册了名为 `"math"` 的验证器，但一个指向 `MathVerifier`、另一个指向 `StrictMathVerifier`，准备期会发生什么？为什么这样设计？

**答案**：`_sources` 抛出「conflicting reward registry entry 'math'」。因为样本只携带验证器名字，运行期按名字查注册表——如果同名允许不同定义，不同源的样本会被静默换成不同的判分器，奖励语义就不可信了；同名同义则允许合并。

**练习 3**：pooled 模式下源为什么不能带 `fraction`？

**答案**：池化采样的份额由 `data.pools` 的归组与 `data.sampling` 的权重表达；如果源还能自带 fraction，就存在两套配额，语义冲突。所以 `validate_sources(pooled=True)` 直接拒绝。

### 4.2 run_data 准备：五种输入模式与验证器目标的携带

#### 4.2.1 概念说明

`run_data.py` 是数据准备的主文件，模块 docstring 一句话概括了它的契约（[open_instruct/miles/datasets/run_data.py:1-7](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/miles/datasets/run_data.py#L1-L7)）：把命名任务、现成 JSONL 或被采纳的 manifest 变成**带验证目标的渲染 prompt + 奖励注册表 + 溯源 manifest**，检查 prompt 长度、奖励元数据与训练/评估重叠，并记录输入输出哈希，让 resume 复用同一份数据而不是静默重采样。

五种输入模式（[docs/miles/data-and-evaluation.md:9-17](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/docs/miles/data-and-evaluation.md#L9-L17) 的模式表）：

| 模式 | 契约 |
| --- | --- |
| `[[data.sources]]` | 多源 + 配比（4.1 已讲，源内可用下面三种模式） |
| `[[data.tasks]]` | 命名任务：`task` + `train_count`/`eval_count` + 可选 `prompt_wrapper` |
| `rl_manifest` | 采纳一份不可变 manifest 及其支持的验证器绑定 |
| `prompt_data` | 现成 prepared JSONL；须配 `reward_config`，可选交替的「数据集名/评估路径」对 |
| `recipe` | **直接拒绝**：命名 recipe 未移植，报错而不是猜 |

验证器目标的携带是本模块的第二个重点，它分三层：

1. **样本行内**：`metadata.verifiers` 是非空数组，每项 `{name, target, weight?}`——`name` 必须在注册表里，`target` 是判分参数（如数学参考答案），`weight` 必须是有限数。
2. **可信注册表**：`verifiers.json` 把名字映射到**工厂函数的完整导入路径**（如 `"open_instruct.miles.rewards.verifiers.GSM8KVerifier"`），可选地带配置与 `grading` 声明。注册表由配置方/准备方提供，样本无权指定工厂——这是 RLVR 数据安全的基本边界。
3. **运行期**：判分模块按名字取工厂、用样本里的 target 实例化验证器（细节在 u8-l6）。

#### 4.2.2 核心流程

`prepare_data`（准备期唯一入口，由 workflow 调用）的骨架：

```text
prepare_data(data, hf_checkpoint, output, max_prompt_length, seed, registry_overrides)
  ├─ validate_data(data)                      # 模式/字段/任务/pool 全量校验（CPU 期拦截）
  ├─ 若有 registry_overrides 且配置了 reward_config：
  │    overrides ← _inherit_grading_policies(原注册表, overrides)   # 继承受信 grading（4.5）
  ├─ 计算 contract = {data, hf 文件哈希, max_prompt_length, seed, overrides}   # 准备身份
  ├─ output 已存在？
  │    ├─ 是 → 核对 contract、逐文件核对输入/输出哈希，全部一致 → 返回旧结果（不可变复用）
  │    └─ 否 → 继续准备
  ├─ 按模式加载：_tasks（渲染+采样）/ _adopt（rl_manifest）/ _prepared（流式读 JSONL）/ _sources（多源）
  │    其中 _prepared 对注册表逐条：工厂路径检查 + grading.validate_spec 语法校验（4.5）
  │    若有 overrides：registry ← registry | _inherit_grading_policies(registry, overrides)
  ├─ 逐分区逐行验证：
  │    ├─ 行结构：input 非空、metadata.verifiers 合法、模板哈希一致
  │    ├─ 重新 tokenize 核对 prompt 长度 ≤ max_prompt_length（over_length 策略在此生效，见 4.4）
  │    ├─ 训练行附加 IFEval 训练目标安全筛
  │    └─ 记录 train/eval 的 prompt 哈希与样本 ID（重叠检查）
  ├─ 排除落地 + 计数 + 空源守卫（4.4 详解）
  ├─ 写 staging 目录：各分区 .jsonl、verifiers.json、mixture.json、manifest.json
  ├─ 复核输入哈希未变 → staging.rename(output)   # 原子发布
  └─ 返回 result（prompt_data / eval_prompt_data / reward_config / manifest 路径）
```

其中 `max_prompt_length` 来自编译链：workflow 在 `data_preparation` 阶段取 `planned.miles["rollout_max_prompt_len"]`（缺省 2048）传入（[open_instruct/miles/execution/workflow.py:358-371](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/miles/execution/workflow.py#L358-L371)）；而 `rollout_max_prompt_len` 的默认值是 `rollout_max_context_len - rollout_max_response_len`（[open_instruct/miles/configuration/run_spec.py:650-657](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/miles/configuration/run_spec.py#L650-L657)）。同一份 `[data]` 换一个上下文预算，`contract` 就不同——这是 4.4 里「改策略/上限必须换新 run 根目录」的根源。

#### 4.2.3 源码精读

**模式与字段校验**。[open_instruct/miles/datasets/run_data.py:187-262](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/miles/datasets/run_data.py#L187-L262) 的 `validate_data`：先拒绝未知字段（L190-207，`over_length` 在白名单里）；`over_length` 只允许 `fail`/`exclude` 两个取值（L208，4.4 详解）；五种模式恰好选一（L209-213）；`recipe` 显式拒绝（L216-217）；`prompt_data` 模式必须配 `reward_config`（L218-219）；命名任务必须是 `TASKS` 表里的已知任务且计数为正整数（L233-260）。

**命名任务的钉扎数据集**。[open_instruct/miles/datasets/run_data.py:37-48](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/miles/datasets/run_data.py#L37-L48) 的 `TASKS` 把每个任务名钉到「数据集 ID + revision + 验证器名」，例如 `gsm8k` 钉在 `ai2-adapt-dev/rlvr_gsm8k_zs` 的特定提交上——命名任务的可复现性靠这张表。[open_instruct/miles/datasets/run_data.py:552-648](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/miles/datasets/run_data.py#L552-L648) 的 `_tasks` 完成「按种子选行 → 剥掉参考答案 → 包装 prompt → 渲染 → 挂验证器」全流程；L624 给每行写上 `"verifiers": [{"name": verifier, "target": target, "weight": 1.0}]`——**验证器目标就是这样随样本携带的**；`multiplication` 任务还演示了双验证器加权（乘法正确性权重 10 + R1 格式权重 1，[open_instruct/miles/datasets/run_data.py:626-630](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/miles/datasets/run_data.py#L626-L630)）。

**prepared JSONL 的流式读取**。[open_instruct/miles/datasets/run_data.py:92-144](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/miles/datasets/run_data.py#L92-L144) 的 `_JsonlRows`：一遍流式统计行数并绑定文件哈希（L101-113，`.gz` 用整文件哈希，普通 JSONL 用逐行拼接哈希；注释明确「resume 身份绑定原始压缩产物，而非解压内容」）；迭代时逐行解析并跳过 `excluded` 集合中的下标（L118-139）——**这个 `excluded` 集合正是 4.4 排除策略在流式模式下的落地机制**，它让源文件字节保持原样。

**逐行验证**。[open_instruct/miles/datasets/run_data.py:308-346](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/miles/datasets/run_data.py#L308-L346) 的 `_verify_row`：要求渲染 input 非空（L309-310）、`metadata.verifiers` 为非空数组且每项 name 在注册表内、target 存在、weight 有限（L312-329）；核对 `rendering_template_sha256` 与 checkpoint 的 chat template 一致（L330，经 `_verify_rendering_template` L384-391）——防止「换 tokenizer 后旧 prepared 数据悄悄错位」；用当前 tokenizer 重新编码并检查长度（L331-341，长度分支见 4.4）；若行自带 `prompt_token_ids` 则逐一比对（L342-344）；最后把 `run_prompt_tokens` 与 token-ID 哈希注回 metadata（L345-346），供排除计数与 resume 校验使用。

**注册表的可信性与 grading 语法**。[open_instruct/miles/datasets/run_data.py:510-539](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/miles/datasets/run_data.py#L510-L539) 的 `_prepared`：注册表必须是非空映射，每个条目的 `factory` 必须是含点的完整导入路径（L514-521）——保证它是代码库里真实存在的 reviewed 验证器，而不是数据集塞进来的任意字符串；本轮新增：每条注册再过一遍 `grading.validate_spec(spec)` 语法校验，失败则包装成带验证器名的 `InputError`（L522-525，详见 4.5）。

**不可变 resume**。[open_instruct/miles/datasets/run_data.py:744-775](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/miles/datasets/run_data.py#L744-L775)：`contract` 记录了 `data` 原文、HF checkpoint 全部小文件哈希、`max_prompt_length`、种子等（L744-751，`registry_overrides` 若存在也计入）；output 已存在时逐项比对 contract（L759-760，不一致报「Preparation contract changed」）与所有输入/输出哈希（L761-766），全过则原样返回旧 result。老准备产物缺新校验时也不会静默绕过——`_verify_resumed_data`（L420-454）会在 resume 时补跑 IF 训练目标筛与模板核对。

**原子发布**。[open_instruct/miles/datasets/run_data.py:901-993](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/miles/datasets/run_data.py#L901-L993)：全部产物先写进同级临时 staging 目录，复核输入哈希未变后一次 `rename` 成正式目录（L986-989）——要么拿到完整准备目录，要么什么都没有，不存在半成品。

#### 4.2.4 代码实践

**实践目标**：追踪「验证器目标如何随样本携带」这条链，并验证 prepared 模式的注册表约束。

1. 阅读 `_tasks`（上文链接），找到 `multiplication` 任务给每行写两个验证器、权重 10:1 的代码位置，抄下该行 metadata 的完整 JSON 形状。
2. 对照 `_verify_row`（上文链接），列出一条 prepared 行必须满足的全部条件（不少于 6 条）。
3. 运行注册表相关的既有单测（CPU 即可，本轮起还覆盖 grading 语法与 named-judge 继承）：

   ```bash
   uv run pytest open_instruct/test_miles_run_data.py -q
   ```

4. 手工把 `mymath/verifiers.json` 里的 factory 改成 `"GSM8KVerifier"`（去掉模块路径），再运行 4.1 实践里的 `validate`（或直接准备），观察「explicit factory paths」报错。实际输出待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：`prompt_data` 模式为什么强制要求 `reward_config`？

**答案**：样本行只带验证器名字；没有注册表，名字无法解析成工厂函数，判分语义悬空。`validate_data` 明确「Prepared prompt_data requires a trusted reward_config」。

**练习 2**：resume 时为什么比对的是「contract + 输入输出哈希」两样，而不是重新跑一遍校验就放行？

**答案**：比对哈希保证的是**同一性**（数据没变），重新校验只能保证**合法性**（数据仍合法但不一定相同）。若只做后者，输入文件被换成另一份同样合法的数据也会被接受，训练种群就静默漂移了；这正是 docstring 里「不接受被改动的文件」的含义。两样都做：哈希定身份，`_verify_resumed_data` 补合法性。

**练习 3**：`_JsonlRows` 为什么对 `.gz` 文件用整文件 SHA-256、对普通 JSONL 用逐行哈希？

**答案**：gzip 是带状态的压缩格式，同样的解压内容可以对应不同的压缩字节（时间戳、压缩级别）；绑定原始压缩字节（整文件哈希）保证 resume 身份锚定在「原始产物」上；普通 JSONL 没有这层歧义，逐行哈希在单遍流式读取时即可完成，避免再读一遍大文件。

### 4.3 源目录与准备：source_preparation 与 source_catalog

#### 4.3.1 概念说明

4.2 的 `prompt_data` 模式消费的是「已经渲染好的 JSONL」。那这些 JSONL 从哪来？`source_preparation.py` 就是**新数据入库的前门**：它把一组**钉了 SHA-256 的本地 JSONL 卫生导出**，经过**人工评审过的适配器**（adapter）转换成标准的 source 包。三个设计立场写在文件头（[open_instruct/miles/datasets/source_preparation.py:1-5](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/miles/datasets/source_preparation.py#L1-L5)）：

1. 输入是本地 JSONL 卫生导出，**绝不联网下载、绝不静默重采样**；
2. 适配器由评审过的代码选择（FACTORIES 表），**数据集本身不能指定导入**；
3. 不支持的行**可评审地省略**（omitted），写入 `dispositions.jsonl` 说明原因，而不是悄悄丢弃。

一个 source 包的产物：`prompts.jsonl`（渲染后的行）、`verifiers.json`（该源的注册表）、`dispositions.jsonl`（省略记录）、`hygiene-receipt.json`（前置卫生回执副本），以及 catalog 里的一条记录。

本轮更新在这条流水线上新增了 **`nemo_abstention_v1` 适配器**：把 Nemotron-RL-QA-Abstention-v1 的闭卷问答数据转成带 `abstention` 验证器目标的 source 包。它在适配阶段就守住四条契约：保留原始 system/user 问题、输出结构化 question/answer 目标并校验、禁止携带 tools（闭卷）、按 `domain`（或从 HotpotQA 的 24 位十六进制 source ID 推断）分层。判分侧的 rubric/裁判/失败策略属于 u8-l13 的内容。

`source_catalog.py` 则是「包 → run 配置」的桥：给它 catalog 路径和一组 `NAME=FRACTION` 权重，它核验每个源的资格（qualification）回执与产物哈希，然后输出一段可直接粘贴进 run 文件的 `[data]` TOML 片段。

#### 4.3.2 核心流程

```text
原始数据集导出（钉 revision）
  → 卫生处理（去污染等，外部完成）+ hygiene receipt
  → source plan（JSON：每源一个 adapter + 钉哈希的 inputs + receipt）
  → python -m open_instruct.miles.datasets.source_preparation plan.json \
        --tokenizer <ckpt> --output <bundle-dir>
       ├─ 逐源逐行 adapt()：messages 规范化 + 按 adapter 提取验证器 target
       │    失败行 → dispositions.jsonl（omitted + 原因），计数器记账
       │    （nemo_abstention_v1：校验原始问题/闭卷/目标结构，见下）
       ├─ 用 checkpoint 的 chat template 渲染 + tokenize 计数
       ├─ 写 prompts.jsonl / verifiers.json / catalog.json
       │    （abstention/reference_judge：注册表 config 记 {"name": ...}，
       │      行 metadata 保留 judge_query 供裁判服务复用原对话）
  → 资格评审（qualification，外部）→ catalog 状态 qualified
  → python -m open_instruct.miles.datasets.source_catalog catalog.json \
        --source a=0.75 --source b=0.25 --output data-fragment.toml
       ├─ 核验 qualification 回执与 prompt_data/reward_config 哈希
       └─ 产出 [[data.sources]] TOML 片段 → 并入 run 文件
```

#### 4.3.3 源码精读

**适配器到工厂的映射**。[open_instruct/miles/datasets/source_preparation.py:33-48](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/miles/datasets/source_preparation.py#L33-L48) 的 `FACTORIES` 把 14 类验证器（math、structured、instruction、ifeval、calendar、reasoning_gym、litmus 等）映射到工厂导入路径；本轮在表首新增 `"abstention"` 项，指向 `judge_registry.NamedJudgeVerifier`（与 `reference_judge` 同一工厂——判分行为由 rubric 绑定区分，见 u8-l13）。行数据里出现的验证器名必须在这张表内，产出注册表时直接以工厂路径写进 `verifiers.json`。

**adapt 的职责**。[open_instruct/miles/datasets/source_preparation.py:191-381](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/miles/datasets/source_preparation.py#L191-L381) 的 `adapt(raw, kind, options)` 返回四元组 `(messages, target, verifier_name, stratum)`：先把各种原始形态（Responses API 参数、Dolci 扁平文本、工具对话等）规范化为纯文本 messages（L83-136 的 `messages`，对角色、字段、内容类型全部白名单化）；再按适配器提取判分目标——例如 `math_v1` 取参考答案字段并拒绝证明题（L346-357，证明需要裁判契约）。**未知的适配器名是 PlanError 直接终止**（L378-380），注释点明原因：拼错的适配器名否则会省略所有行却仍报告「prepared」。

**新适配器 nemo_abstention_v1**。[open_instruct/miles/datasets/source_preparation.py:358-368](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/miles/datasets/source_preparation.py#L358-L368)：目标构造为 `{"kind": abstention.KIND, "question": ..., "answer": ...}` 并先过 `abstention.validate_target`；随后三条硬校验——messages 必须恰好是 `[system, user]` 且 user 内容与目标里的 question 逐字一致（防止「渲染后的问题」与「参考用的问题」漂移）、行内不得携带 tools（闭卷契约，`raw.tools` 与 `responses_create_params.tools` 都查）、stratum 取 `domain` 字段，缺失时只对 HotpotQA 已知的 24 位十六进制 source ID 做推断、其余记 `Unknown`（注释明确不为未来所有缺 domain 的数据猜测）。配套测试 [open_instruct/test_miles_source_preparation.py:706-724](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/test_miles_source_preparation.py#L706-L724) 断言了「保留原始 messages 与事实目标」「换掉 question 即报 original system/user 错」两个行为。

**prepare 主循环**。[open_instruct/miles/datasets/source_preparation.py:395-545](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/miles/datasets/source_preparation.py#L395-L545)：要求显式 chat template 并记录其哈希；每个输入先验哈希（L384-388 的 `checked_file`）；要求钉 revision 且 split 为 train/unassigned；每行身份唯一。本轮两处小改：注册表里 `reference_judge` 与 `abstention` 一样写 `config = {"name": verifier}`（[L467-468](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/miles/datasets/source_preparation.py#L467-L468)，绑定名交给 run 侧的 judging 配置解析）；成功行的 metadata 同样为这两类验证器保留完整原始对话 `judge_query`（[L511-512](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/miles/datasets/source_preparation.py#L511-L512)），裁判服务直接复用它、不必重渲染。省略行进 `dispositions.jsonl`，最终 catalog 记录 prepared/omitted 计数、分层（strata）与产物哈希。

**catalog 解析**。[open_instruct/miles/datasets/source_catalog.py:17-60](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/miles/datasets/source_catalog.py#L17-L60) 的 `resolve`：源必须在 catalog 里且状态为 `qualified`（L27-28）；有资格回执时核对回执路径不逃出 catalog 目录、回执哈希一致、回执确实通过该源、产物哈希（`prompt_data`/`reward_config`）与 catalog 记录一致（L29-44）——**任何一个哈希不匹配都要求重新评审**，杜绝「评审后偷换文件」；bundle 路径同样不得逃逸（L48-50）。最后跑一遍 `mixture.validate_sources` + `run_data.validate_data`（L57-59），保证产出的片段一进 run 文件就是合法配置。[open_instruct/miles/datasets/source_catalog.py:63-87](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/miles/datasets/source_catalog.py#L63-L87) 的 CLI main 把结果写成 `[data]` TOML 片段（用 `open("x")` 独占创建，拒绝覆盖已有文件）。

**文档对照**。[docs/miles/source-preparation.md](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/docs/miles/source-preparation.md) 给出 plan.json 的最小格式、命令行用法与全部适配器总表；本轮末尾新增「Abstention-aware factoid QA」一节（[L576-579](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/docs/miles/source-preparation.md#L576-L579)），指明 `nemo_abstention_v1` 必须搭配命名 rubric `nvidia/qa-abstention-v1` 并链接到 abstention.md。文档中同样明确「长 prompt 保留在池中：把训练上下文上限设够，或者显式准备一个短上下文变体」——这正是 4.4 的 `over_length` 想在 run 一侧解决的问题。

#### 4.3.4 代码实践

**实践目标**：为一个（假想的）新数学题集写一份 source plan 骨架，并跑通 `--help` 与参数校验（不真正准备数据）；再用 `adapt` 直接体验 `nemo_abstention_v1` 的契约校验。

1. 阅读 [docs/miles/source-preparation.md](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/docs/miles/source-preparation.md) 的 plan 示例，写一份自己的 plan（示例代码，`plan.json`）：

   ```json
   {
     "schema_version": 1,
     "sources": {
       "mini-math": {
         "adapter": "math_v1",
         "inputs": [{"path": "exports/mini-math.jsonl", "sha256": "REPLACE",
                      "revision": "PINNED_COMMIT", "split": "train"}],
         "hygiene_receipt": {"path": "exports/receipt.json", "sha256": "REPLACE"}
       }
     }
   }
   ```

2. 运行 `uv run python -m open_instruct.miles.datasets.source_preparation --help` 与 `uv run python -m open_instruct.miles.datasets.source_catalog --help`，确认两个 CLI 的参数。
3. 把 `sha256` 填成随便一个十六进制串、指向真实存在的文件，跑一次准备，观察 `Input hash mismatch` 报错——体会「先验哈希」在第一个输入处就拦截。
4. **abstention 契约体验**（CPU 即可，不需要 tokenizer）：

   ```bash
   uv run pytest open_instruct/test_miles_source_preparation.py::test_abstention_preserves_original_messages_and_factual_target -q
   ```

   然后对照 [open_instruct/test_miles_source_preparation.py:706-724](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/test_miles_source_preparation.py#L706-L724) 的夹具，用 `python -c` 直接调 `source_preparation.adapt(raw, "nemo_abstention_v1")`：分别构造「带 tools 的行」「question 与 user 消息不一致的行」，观察两条 ValueError；再把 `domain` 去掉、`id` 换成 24 位十六进制串，观察 stratum 如何推断为 `HotpotQA`。
5. 若手头没有合适的 tokenizer checkpoint，本实践止步于报错观察即可；**需要观察的现象**：哈希不匹配的输入永远不会进入适配阶段；abstention 契约违规在 `adapt` 阶段（而非 GPU 任务里）就被拒绝。实际输出待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：为什么「未知适配器名」是 PlanError（终止准备），而「个别行不满足适配器」只是 omitted（记入 dispositions）？

**答案**：前者说明**计划本身写错了**——所有行都会被省略，若继续会产出一份空壳「prepared」报告；后者是**数据中的正常异质**，省略原因需要人工评审。`PlanError` 的 docstring 就这么区分（L51-52）。

**练习 2**：`source_catalog.resolve` 为什么要求 qualification 回执里的 `artifact_sha256` 与 catalog 记录的 `prompt_data`/`reward_config` 哈希都一致？

**答案**：资格评审针对的是**特定字节**的产物。如果评审后允许换文件，评审结论就不再覆盖实际训练数据；哈希对不上时唯一的出路是「requalify the bundle」——重新评审新字节。

**练习 3**：`nemo_abstention_v1` 为什么要求 user 消息与目标里的 `question` 逐字一致，而不是允许「语义相同」的改写？

**答案**：目标里的 question/answer 会成为裁判服务构造判分 prompt 的输入（u8-l13）；如果训练时模型看到的问题文本与判分时用的参考问题不一致，奖励信号就在度量两个不同的问题，打分失去意义。「逐字一致」是最便宜的机器可校验代理，改写等价性无法在准备期可靠判定。同理它拒绝 tools——带工具的问答与闭卷 abstention 判分是两种契约。

### 4.4 超长提示排除策略：data.over_length

#### 4.4.1 概念说明

一个发布版本的服务可能同时承载上下文预算差异很大的多个 run。准备期会用**本 run 的 tokenizer** 把渲染后的 prompt 重新编码，与 `rollout_max_prompt_len` 比较。该特性引入时只有一种策略：任何一条 prompt 超限就中止准备（`fail`，仍是缺省）。可选策略：

```toml
[data]
over_length = "exclude"   # 缺省是 "fail"
```

`exclude` 的语义边界，文档 ([docs/miles/data-and-evaluation.md:531-570](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/docs/miles/data-and-evaluation.md#L531-L570)) 讲得很完整，可以归纳成六条：

1. **只作用于训练侧**：held-out（评估）prompt 超限仍然报错——评估种群不允许被预算悄悄改写。
2. **不截断、不改写**：超限行被整行跳过，prompt 文本、顺序、身份、源配比全部保留在原始输入里。
3. **不回填**：命名任务的 `train_count` 选的是**候选行**，排除后少了就少了，不会补抽。
4. **其余校验照常作用于所有候选行（含被排除的行）**：验证器约束、token-ID/模板一致性、训练/评估重叠检查一个不少——排除不能成为绕过校验的后门。
5. **记账**：`manifest.json` 新增 `prompt_length` 块，记录策略、上限、作用域（training）与按 source（及配置池时的按 pool）的 `original/excluded/retained` 三元计数；同样的计数打进准备日志。
6. **守卫**：每个配置的 `data.sources` 条目、每个此前非空的池必须至少保留一行（否则准备失败）；单输入/命名任务 run 里基于元数据的「源分组」只是溯源标签，允许被整个掏空（只报警不失败）；整体训练输入必须至少剩一行。

此外，策略与上限都进入不可变准备身份：**改 `over_length` 或改 `rollout_max_prompt_len` 都等于改 contract，resume 会报「Preparation contract changed」，必须换新的 run 根目录重新准备**。

本轮更新在排除记账处又叠加了一条**池采样优先级的逐行校验**：配置了 `data.sampling.priority`（u8-l11）的 pooled run，每条**被保留**的训练行都必须在指定 metadata 字段上带一个 1..4 的整数优先级标签，缺标签或越界同样让准备失败——优先级标签属于「准备的清单」的一部分，必须在准备期就齐备。

#### 4.4.2 核心流程

`prepare_data` 主循环中与排除相关的数据流：

```text
对每个分区 split、每行 row（下标 index 从 1 起）：
  1. _verify_row(..., exclude_over_length = (policy=="exclude" 且 split∈training),
                 training = split∈training)
       ├─ len(ids) > limit 且未被豁免 → InputError（训练行附带「可设 exclude」的提示）
       └─ 通过 → metadata.run_prompt_tokens = len(ids)
  2. split∈training：
       over_limit = run_prompt_tokens > max_prompt_length
       计数键 name = 配置源名 ?? metadata.source_dataset ?? metadata.source_name ?? split名
       length_counts[name][original += 1; excluded|retained += 1]
       若配置池：pool_counts[metadata.sampling_pool] 同样三元记账
                 若配置 data.sampling.priority 且该行被保留：
                   pool_sampling.priority_value(metadata, priority.field)  # 1..4 整数，否则报错
       over_limit → excluded.add(index)
  3. 记录重叠身份：prompt 哈希与样本 ID 进 train/eval 集合（含被排除行）
  4. 分区收尾：excluded 非空 →
       流式（_JsonlRows）：rows.excluded = excluded   # 源文件不动
       物化（tasks/adopt 生成的列表）：partitions[split] 过滤后重建
日志：每个 source/pool 打 "policy=.. limit=.. original=.. excluded=.. retained=.."
守卫：配置源或池 retained==0 → InputError；训练整体为空 → InputError
```

关键点：**排除发生在验证之后**。`_verify_row` 带 `exclude_over_length` 豁免的只是「超限即报错」这一条；行结构、验证器、模板哈希等任何其他不合法照样让准备失败——配套测试 `test_exclusion_does_not_hide_invalid_rows` 就是用四个不同的坏行验证这一点。

#### 4.4.3 源码精读

**取值校验（两处）**。配置加载期：[open_instruct/miles/configuration/run_spec.py:322](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/miles/configuration/run_spec.py#L322) 在 RunSpec 校验 `[data]` 时用 `validation.choice(data.get("over_length", "fail"), "data.over_length", ("fail", "exclude"))`；`over_length` 同时被登记为 data 节的合法结构化字段（[open_instruct/miles/configuration/constraints.py:303-313](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/miles/configuration/constraints.py#L303-L313)）。准备入口又独立校验一次：[open_instruct/miles/datasets/run_data.py:208](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/miles/datasets/run_data.py#L208)——直接调 `run_data.prepare_data`（绕过 RunSpec）的路径也被覆盖。

**超限判定与提示语**。[open_instruct/miles/datasets/run_data.py:331-341](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/miles/datasets/run_data.py#L331-L341)：`if not ids or (len(ids) > limit and not exclude_over_length)` 才报错；报错信息除了「immutable input 不能截断，请调大上限或准备更短的 prompt」外，当该行属于训练且确实超限时，额外附上一句 `Alternatively, set data.over_length = "exclude" ...`（L333-337）。注意 `not ids`（0 token）不在豁免之列——空 prompt 永远是错误，配套测试 `test_zero_token_training_error_does_not_suggest_length_exclusion` 确认这种情况不会建议排除。

**排除的训练侧限定**。[open_instruct/miles/datasets/run_data.py:818-825](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/miles/datasets/run_data.py#L818-L825)：调用 `_verify_row` 时 `exclude_over_length=exclude_over_length and split in training`——held-out 分区拿到的永远是 False，所以评估行超限直接抛错（配套测试 `test_evaluation_remains_strict` 断言报错里不出现 `data.over_length` 字样）。`exclude_over_length` 本身在 L796 由 `data.get("over_length", "fail") == "exclude"` 解析。

**计数与源名解析**。[open_instruct/miles/datasets/run_data.py:839-866](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/miles/datasets/run_data.py#L839-L866)：训练行先算 `over_limit`，再解析计数键——**配置源名优先**（`source_names` 由 `data.sources` 的 `name` 构成，L797），没有配置源时回退到行内 `metadata.source_dataset` → `metadata.source_name` → 分区名（`train`）。这个回退链让单文件输入里的旧 Workbench 导出（只带 `source_name`）也能按数据集标签分组记账；两个标签都缺/非字符串时统一落进 `train` 桶。每个桶是 `{"original": n, "excluded": n, "retained": n}`；配置池时再用 `metadata.sampling_pool`（由 `pools.annotate` 注入）记一份池维度计数。**本轮新增**（L859-864）：配置了 `data.sampling.priority` 且该行被保留时，逐行调 [pool_sampling.priority_value](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/miles/rollout/pool_sampling.py#L39-L44)（[open_instruct/miles/rollout/pool_sampling.py:39-44](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/miles/rollout/pool_sampling.py#L39-L44)），要求 metadata 字段上是 1..4 的整数，否则带行号报 `InputError`。`over_limit` 的行把下标加入 `excluded`（L865-866）。

**排除如何落地**。[open_instruct/miles/datasets/run_data.py:873-877](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/miles/datasets/run_data.py#L873-L877)：分区循环结尾，`excluded` 非空时——prepared 模式（`_JsonlRows`）只把下标集合挂到 `rows.excluded`（源文件字节完全不动，`__len__`/`__iter__` 据此跳过，见 [open_instruct/miles/datasets/run_data.py:118-139](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/miles/datasets/run_data.py#L118-L139)）；tasks/adopt 模式产出的物化列表则直接重建为保留行。同时 L867-868 只对保留行调 `remember_validation`，把 `run_prompt_tokens` 等注解缓存下来，供写出时复用。

**日志与守卫**。[open_instruct/miles/datasets/run_data.py:878-895](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/miles/datasets/run_data.py#L878-L895)：逐 source/pool 打 INFO 日志（`Prompt length policy=exclude limit=N source=X original=A excluded=B retained=C`）；`retained` 为 0 时，**只有两种情况报错**——`kind == "pool"`（配了池，池是采样分配单元，空池会破坏权重语义）或 `"sources" in data`（配置源同样是分配单元）。注释写明：单输入里的数据集标签只描述溯源、不是配置的采样分配，「可能在这个预算下整组消失」。最后 L894-895 兜底：训练整体没有行 → 报错。

**重叠检查覆盖被排除行**。[open_instruct/miles/datasets/run_data.py:869-872 与 896-897](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/miles/datasets/run_data.py#L869-L897)：prompt 哈希与样本 ID 的收集发生在排除判定之外——被排除的训练行仍然参与「训练/评估重叠」比对，重复的 prompt 或身份照样让准备失败。

**manifest 记账**。[open_instruct/miles/datasets/run_data.py:976-982](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/miles/datasets/run_data.py#L976-L982)：manifest 新增 `prompt_length` 块，字段为 `policy`、`max_prompt_length`、`scope: "training"`、`sources: {名字: {original, excluded, retained}}`，配置池时再加 `pools`。同时（L928-969）mixture.json 记录的是**排除后**文件的行数与哈希，运行期 `mixture_provider` 的行数核对因此继续成立——「manifest 的行数与哈希描述保留后的文件」。

**不可变身份**。[open_instruct/miles/datasets/run_data.py:744-760](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/miles/datasets/run_data.py#L744-L760)：`data`（含 `over_length`）与 `max_prompt_length` 都在 contract 里；配套测试 `test_exclude_streams_boundaries_counts_and_immutable_resume` 最后两轮分别改策略、改上限，两者都命中「Preparation contract changed」。

**示例配置**。[configs/miles/examples/tiny/dev.toml:30-32](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/configs/miles/examples/tiny/dev.toml#L30-L32)：维护示例本轮按模型家族重组进 `tiny/` 与 `small/` 两个目录，两份 dev.toml 都显式写 `[data] over_length = "exclude"`，注释说明「排除超预算训练提示并按源计数；held-out 保持严格」。

**配套测试全景**。[open_instruct/test_miles_over_length.py](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/test_miles_over_length.py)（314 行、12 个测试）把上述每条语义都钉成了断言，值得按测试名通读一遍：边界计数与不可变 resume（[L36-54](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/test_miles_over_length.py#L36-L54)，注意恰好等于上限的行被保留）、缺省仍失败且提示 exclude（[L61-68](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/test_miles_over_length.py#L61-L68)）、排除不掩盖非法行（[L71-84](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/test_miles_over_length.py#L71-L84)）、评估严格（[L87-94](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/test_miles_over_length.py#L87-L94)）、tasks/adopt 物化输入同样可排除（[L98-122](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/test_miles_over_length.py#L98-L122)）、被掏空的配置源/池拒绝（[L126-163](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/test_miles_over_length.py#L126-L163)）、策略取值校验（[L166-177](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/test_miles_over_length.py#L166-L177)）、溯源标签可整组消失（[L189-240](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/test_miles_over_length.py#L189-L240)）、训练整体不可被排空（[L243-266](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/test_miles_over_length.py#L243-L266)）、源名回退矩阵与配置名优先（[L269-306](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/test_miles_over_length.py#L269-L306)）。

#### 4.4.4 代码实践

**实践目标**：亲手构造超长样本，对比 `fail` 与 `exclude` 两种策略下的行为差异，读到 manifest 的三元计数，并验证「配置源被掏空 → 准备失败」。

**任务 A：跑通官方测试**（CPU 即可，测试用假 tokenizer，不需要模型）：

```bash
uv run pytest open_instruct/test_miles_over_length.py -q
```

对照 4.4.3 的测试全景，给每个测试写一行「它钉住的语义」。

**任务 B：最小复现实验**。以下脚本模仿测试的夹具（示例代码，`over_length_demo.py`，放在仓库外的临时目录运行；思路来自 `test_miles_over_length.py` 的 `row`/`prepared_input`/`environment` 辅助）：

```python
# 示例代码：对比 data.over_length = "fail" 与 "exclude"
import json, sys
from pathlib import Path
sys.path.insert(0, ".")
from open_instruct.miles.datasets import run_data

tmp = Path(sys.argv[1]); tmp.mkdir(parents=True, exist_ok=True)
hf = tmp / "hf"; hf.mkdir(exist_ok=True)
(hf / "tokenizer_config.json").write_text('{"chat_template": "TMPL"}')

def row(text, sid):
    return {"input": text, "metadata": {
        "verifiers": [{"name": "gsm8k", "target": "42"}],
        "prepared_sample_id": sid}}

rows_path = tmp / "input.jsonl"
rows_path.write_text("".join(json.dumps(r) + "\n" for r in [
    row("short question", "s1"),
    row("x" * 500, "s2"),          # 假设会超过给定的 limit
    row("another fine one", "s3"),
]))
registry = tmp / "registry.json"
registry.write_text(json.dumps(
    {"gsm8k": {"factory": run_data.FACTORIES["gsm8k"]}}))

for policy in ("fail", "exclude"):
    data = {"prompt_data": str(rows_path), "reward_config": str(registry),
            "over_length": policy}
    out = tmp / f"prepared-{policy}"
    try:
        result = run_data.prepare_data(data, hf, out, max_prompt_length=100, seed=17)
        manifest = json.loads(Path(result["manifest"]).read_text())
        print(policy, "->", manifest["prompt_length"])
    except Exception as error:
        print(policy, "-> FAILED:", type(error).__name__, error)
```

说明：真实准备会用 checkpoint 的 tokenizer 计数；上面的假模板没有真实 tokenizer，`_tokenizer` 会被真实调用——如果本机没有可用 tokenizer，请改用 `unittest.mock` 拦截 `run_data._tokenizer`（测试正是用 monkeypatch 这样做的，见 [open_instruct/test_miles_run_data.py:33-41](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/test_miles_run_data.py#L33-L41)）。运行结果待本地验证。

**需要观察的现象与预期结果**：

1. `fail`：抛 `InputError`，信息含「Prompt has N tokens」以及「set data.over_length = "exclude"」建议；输出目录不存在。
2. `exclude`：准备成功；`prompt_data` 指向的 JSONL 只剩 2 行；`manifest["prompt_length"]` 形如 `{"policy": "exclude", "max_prompt_length": 100, "scope": "training", "sources": {"train": {"original": 3, "excluded": 1, "retained": 2}}}`。
3. 同参数重跑 `exclude`：返回与第一次相同的 result（不可变复用）；把 policy 改回 `fail` 或把 limit 改成 101 再跑：报「Preparation contract changed」。
4. **掏空验证**：把三条样本全换成长文本（或参照测试 `test_mixture_retained_counts_preserve_fractions_and_reject_empty_source` 的 `empty=True` 分支，构造两个源、其中 b 源全部超限），`exclude` 下应报 `Training source 'b' has no rows after prompt-length exclusion`，且输出目录不存在。
5. **优先级标签校验（本轮新增）**：在上面的 `data` 里加 `"pools"`/`"sampling"` 并配 `"priority": {"field": "reward_priority"}`（参照 [docs/miles/data-and-evaluation.md:942-977](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/docs/miles/data-and-evaluation.md#L942-L977) 的示例），但只在部分行上写 `metadata.reward_priority`：被保留的缺标签行应报 `Priority metadata.reward_priority must be an integer in 1..4`。实际输出待本地验证（也可对照 `open_instruct/test_miles_pool_priority.py` 的断言）。

#### 4.4.5 小练习与答案

**练习 1**：为什么配置源（`data.sources`）与池的 `retained == 0` 要报错，而单输入里元数据标签分组的 `retained == 0` 只是记账？

**答案**：配置源和池是**采样分配单元**——mixture 的 fraction 与 pool 权重都以它们为分母。一个份额被掏空后，剩余源/池的份额会被运行期重新解释，训练种群与配置声明不一致。元数据标签只描述「这行数据从哪来」，不参与分配，整组消失只改变种群构成，记录在 manifest 里供人工审查即可。

**练习 2**：`exclude` 策略下，一条超限的训练行与一条评估 prompt 完全相同（文本一样），准备会怎样？

**答案**：训练行被排除（不进训练文件），但它的 prompt 哈希仍进入了 `train_prompts` 集合；评估行也进入 `eval_prompts`。两者相交触发「Training and held-out data overlap by prompt or source identity」报错——重叠检查明确覆盖被排除的候选行，排除不能洗白数据泄漏。

**练习 3**：把 `over_length` 从 `"exclude"` 改回 `"fail"` 在同一个 run 根目录 resume，会发生什么？为什么这么设计？

**答案**：报「Preparation contract changed; refusing to alter resumed run data」。策略与上限参与准备身份：同一 run 目录里已经按 exclude 产出的训练文件如果被 fail 语义复用（或反之），训练种群与配置声明就不再对应。想换策略只能换新的 `output.root` 重新准备，保证每个 run 目录描述唯一确定的训练种群。

**练习 4**：为什么池采样优先级的逐行校验只查**被保留**的行（`not over_limit`），而不查被排除的行？

**答案**：被排除的行根本不会进入训练清单，它们的标签永远不会被优先级排序消费；提前为死数据报错只会把「超长排除」的宽容语义重新变成硬失败。反过来，被保留的行一定会参与池内 1→2→3→4 的优先级遍历（u8-l11），缺标签会在运行期才爆——准备期校验把它前移到 CPU 阶段。

### 4.5 grading 声明的准备期校验与 named-judge 继承（本轮更新重点）

#### 4.5.1 概念说明

统一判分输入（u8-l12）之后，注册表条目多了一个可选的 `grading` 声明：

```json
{
  "trusted": {
    "factory": "open_instruct.miles.rewards.judge_registry.NamedJudgeVerifier",
    "grading": {"objective": "process", "scope": "full", "require_response": false}
  }
}
```

它回答的问题是：**这个验证器要消费模型的哪一段输出**（只看交付回答、只看思考段，还是全文）、缺失回答算不算失败。这是「受信的判分契约」——它声明在注册表里、由配置方掌控，数据集行内无权覆盖。

本模块讲它在**准备期**的两个行为：

1. **语法校验，且只做语法校验**。`_prepared` 会对注册表的每一条调 `grading.validate_spec`：`grading` 只接受 `objective`/`scope`/`require_response` 三个键，取值必须在各自枚举内，`require_response` 必须是布尔。**它故意不导入工厂函数、不猜测默认值**——`objective` 与 `scope` 的**组合**约束（answer 必须 response、process 必须 thinking/full 等）依赖工厂自己的 `grading_objective`/`grading_scope` 声明，留到注册表加载期由 `GradingPolicy` 检查（driver 启动前的 `validate_registry`，见 u8-l3/u8-l12）。准备期保持轻量：不实例化任何验证器就能拒绝写错的 JSON。

2. **named-judge 绑定继承受信声明**。run 配置里的 named-judge 绑定（`[judging.bindings]`，见 u8-l13）会以 `registry_overrides` 的形式**替换**注册表条目的工厂与配置——但替换实现不等于替换判分目标：原条目里的 `grading` 声明必须被继承进覆盖值；若覆盖自己显式带了 `grading`，则以覆盖为准。更进一步，继承后的完整 overrides 会参与**准备契约**（contract）：旧版本产出的、把 grading 声明丢掉的缓存，resume 时对不上 contract，直接被拒绝——「陈旧缓存宁可不复用，也不静默换判分目标」。

#### 4.5.2 核心流程

```text
准备期（prepare_data）
  ├─ _prepared 逐条注册表检查：
  │    factory 必须是含点完整路径
  │    grading.validate_spec(spec)：
  │       键 ⊆ {objective, scope, require_response}
  │       objective ∈ {answer, process, format, composite}
  │       scope ∈ {response, thinking, full}
  │       require_response 必须 bool
  │       → 违规：InputError("Verifier 'name': grading ...")
  │       （组合约束不在此查——留给加载期 verifier_policy/GradingPolicy）
  ├─ registry_overrides 存在且配置了 reward_config：
  │    overrides ← _inherit_grading_policies(原注册表, overrides)
  │       每条覆盖：无显式 grading 且原条目有 → 继承 {"grading": 原声明}
  │       显式 grading → 覆盖优先
  │    （继承后的 overrides 进入 contract → 陈旧缓存 resume 被拒）
  └─ 写出注册表前：registry ← registry | _inherit_grading_policies(registry, overrides)

加载期（driver 启动前，u8-l3 / u8-l12）
  └─ validate_registry → GradingPolicy.from_spec / __post_init__：
       answer/composite 必须 scope=response
       process 必须 scope=thinking|full
       require_response=false 必须 thinking|full
```

#### 4.5.3 源码精读

**语法校验器**。[open_instruct/miles/rewards/grading.py:25-40](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/miles/rewards/grading.py#L25-L40) 的 `validate_spec`：docstring 直说意图——「不导入工厂、不假设其默认值；准备刻意保持轻量。Objective/视图组合依赖工厂声明，由加载期的 verifier_policy 检查」。实现就是四条纯字典/枚举检查。枚举常量定义在同文件 [L14-19](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/miles/rewards/grading.py#L14-L19)。

**组合约束在加载期**。[open_instruct/miles/rewards/grading.py:137-167](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/miles/rewards/grading.py#L137-L167) 的 `GradingPolicy`：`__post_init__`（L144-152）检查三条组合规则（answer/composite 要求 `scope=response`、process 要求 `thinking|full`、`require_response=false` 要求显式 `thinking|full`）；`from_spec`（L157-167）在构造时应用声明值与默认值。为什么准备期不能查组合？配套测试 [test_prepared_grading_syntax_does_not_override_factory_defaults](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/test_miles_run_data.py#L239-L256)（[open_instruct/test_miles_run_data.py:239-256](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/test_miles_run_data.py#L239-L256)）给出了答案：一个只声明 `{"scope": "full"}` 的条目**合法通过准备**（`{"grading": {"scope": "full"}}` 单看语法没有冲突），因为工厂自己可能声明 `grading_objective="process"`——`scope=full` 对它是默认值。若准备期替工厂猜「objective 缺省就是 answer」，就会误杀这类条目；所以组合检查必须等工厂类真正可见时做。

**准备入口的接线**。[open_instruct/miles/datasets/run_data.py:522-525](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/miles/datasets/run_data.py#L522-L525)：`_prepared` 在工厂路径检查之后，把每条注册表条目喂给 `grading.validate_spec`，`ValueError` 被包装成 `InputError(f"Verifier {name!r}: {error}")`——报错信息带着验证器名，方便定位是哪条声明写错。配套参数化测试 [test_preparation_still_rejects_bad_grading_syntax](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/test_miles_run_data.py#L259-L262)（[open_instruct/test_miles_run_data.py:259-262](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/test_miles_run_data.py#L259-L262)）用四组坏值（`scope: "bad"`、`objective: "bad"`、`require_response: 1`、多余键）钉住这条路径。

**继承函数**。[open_instruct/miles/datasets/run_data.py:686-703](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/miles/datasets/run_data.py#L686-L703) 的 `_inherit_grading_policies(registry, overrides)`：docstring 一句话概括设计立场——「替换实现不得替换其受信判分目标。named-judge 绑定替换的是工厂/配置，不是输入策略；显式 supplied 的覆盖策略仍然优先」。实现上对每条覆盖查原注册表同名条目，有 `grading` 就 deepcopy 出 `{"grading": ...}` 前置，再用 `|` 合并——覆盖自身的键（含显式 `grading`）自然胜出。测试 [test_explicit_override_policy_wins_over_inherited_policy](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/test_miles_run_data.py#L315-L318)（[open_instruct/test_miles_run_data.py:315-318](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/test_miles_run_data.py#L315-L318)）断言了「显式 answer/response 覆盖压过继承的 process/full」。

**两个调用点**。其一在 contract 计算之前（[open_instruct/miles/datasets/run_data.py:738-743](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/miles/datasets/run_data.py#L738-L743)）：`registry_overrides` 先被 `_inherit_grading_policies` 增强，再进 contract——注释写明动机：「把继承的策略纳入准备契约。这会拒绝那些绑定覆盖丢弃了策略的旧准备产物，同时不 invalidate 没有策略声明的普通准备」。其二在写出注册表之前（[open_instruct/miles/datasets/run_data.py:783-787](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/miles/datasets/run_data.py#L783-L787)）：合并时再次走继承，保证**落盘的 `verifiers.json` 里，被绑定替换的条目仍带着原 grading 声明**。

**端到端测试**。[test_judge_binding_retains_grading_policy_and_refuses_old_dropped_policy_cache](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/test_miles_run_data.py#L265-L312)（[open_instruct/test_miles_run_data.py:265-312](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/open_instruct/test_miles_run_data.py#L265-L312)）把整条链钉死：原注册表条目带 `grading: {objective: process, scope: …, require_response: false}`，named-judge 覆盖只带 factory+config；断言产出注册表里该条目等于「覆盖 + 继承的 grading」、调用方传入的 overrides 字典**没有被就地改动**（L302，`"grading" not in overrides["trusted"]`）、同参数 resume 幂等、manifest 的 contract 里记录的是**继承后**的 overrides（L306）；最后手工把 manifest 改回「无继承的旧式 overrides」模拟陈旧缓存，resume 立刻报「Preparation contract changed」（L307-312）。

#### 4.5.4 代码实践

**实践目标**：亲手触发 grading 语法报错，并验证「准备期不查组合、加载期才查」的分层设计。

1. **跑官方测试**（CPU 即可）：

   ```bash
   uv run pytest open_instruct/test_miles_run_data.py -q -k "grading or judge_binding or override_policy"
   ```

   对照 4.5.3 引用的四个测试，给每个写一行「它钉住的语义」。

2. **语法报错**：在 4.4.4 任务 B 的脚本基础上，把注册表改成（示例代码）：

   ```python
   registry.write_text(json.dumps({"gsm8k": {
       "factory": run_data.FACTORIES["gsm8k"],
       "grading": {"scope": "thinking", "extra": True}}}))
   ```

   以 `over_length = "fail"`（短样本不超限）跑 `prepare_data`，预期抛 `InputError`，信息形如 `Verifier 'gsm8k': grading accepts only objective, scope and require_response`。再分别试 `{"objective": "answer!"}`、`{"require_response": 1}`，记录各自的报错文本。实际输出待本地验证。

3. **组合分层**：把 `grading` 改成 `{"objective": "answer", "scope": "thinking"}`——**准备会通过**（语法上两者都合法），因为组合约束属于加载期。用一行代码直接验证加载期的判定（示例代码）：

   ```python
   from open_instruct.miles.rewards import grading
   grading.GradingPolicy.from_spec(
       {"grading": {"objective": "answer", "scope": "thinking"}})   # ValueError
   grading.GradingPolicy.from_spec(
       {"grading": {"objective": "process", "scope": "full", "require_response": False}})  # OK
   ```

   预期第一条抛 `grading answer/composite objectives require scope=response; declare process or format`，第二条构造成功。待本地验证。

4. **需要观察的现象**：坏语法在准备期被拒（带验证器名）；坏组合在准备期静默通过、在 `GradingPolicy.from_spec` 处被拒——这正是 docstring 承诺的分层。

#### 4.5.5 小练习与答案

**练习 1**：为什么 `validate_spec` 连「objective 缺省」都不补一个默认值，而是留空交给加载期？

**答案**：缺省值的合法取值依赖工厂声明——`MaxLenVerifier` 这类 token 验证器默认 objective 是 `process`、scope 是 `full`，而多数答案验证器默认 `answer`。准备期没有导入工厂（也不该导入：保持轻量、避免在数据准备路径上执行任意验证器代码），任何「替工厂猜默认」都可能误杀合法声明或放行非法声明。测试 L253-256 的注释说得直白：「轻量的数据准备不能假设每个工厂都声明 answer objective」。

**练习 2**：named-judge 绑定覆盖为什么必须继承原条目的 `grading`，而不是让新绑定自带一套？

**答案**：绑定改变的是「谁来判」（换一个裁判服务/rubric），不是「判哪段输入」。判分输入契约（objective/scope）是数据与训练配置之间的一部分——训练侧可能依据「这个任务只对交付回答计分」做了数据分析或阈值决策；换绑定时悄悄把 scope 从 full 改回 response，奖励语义就变了，且没有任何配置文件显式声明过这个变化。继承 + 显式覆盖优先，同时满足「默认不漂移」与「需要时可以改」。

**练习 3**：`_inherit_grading_policies` 为什么在**两处**被调用（contract 之前与注册表合并时），而不是只在合并时调一次？

**答案**：合并处解决的是「落盘的 verifiers.json 内容正确」；contract 处解决的是「旧缓存拒绝」。若只在合并时继承，旧版本代码产出的 manifest 里 contract 记录的是未继承的 overrides——新代码 resume 时算出的 contract（已继承）与旧记录不相等，这本可以靠运气对上；但反过来，若 contract 不包含继承结果，「丢弃了声明的旧缓存」与「正确的新缓存」在 contract 上无法区分，陈旧错误数据就会被静默复用。两处调用各管一头：一个管身份，一个管内容。

## 5. 综合实践

把本讲五个模块串成一条完整流水线（全程 CPU，除任务 C 外不需要 GPU）：

**任务：为一个自制数学题集走完「源准备 → 混合声明 → 超长排除 → grading 校验」全链路。**

1. **造数据**：手写 12 道两位数加法题，其中 2 道故意写成 500+ 字符的长题干（模拟超长 prompt），保存为 JSONL（每行 `{"messages": [{"role": "user", "content": "..."}], "expected_answer": "..."}`）。
2. **入库（对照 4.3）**：写一份 `math_v1` 的 source plan 骨架（inputs 的 sha256 用 `sha256sum` 算出真实值），跑 `python -m open_instruct.miles.datasets.source_preparation --help` 熟悉 CLI；如有条件用一个小 checkpoint 的 tokenizer 真正跑一次 prepare，检查 `prompts.jsonl` 行数、`dispositions.jsonl` 里的省略原因和 catalog 计数。没有条件则记录「待本地验证」。
3. **混合（对照 4.1）**：写一份双源 TOML（自制数学 + `gsm8k-less-noise` 命名任务，配比 0.5/0.5），`uv run python -m open_instruct.miles validate` 通过；再分别制造配比不等于 1、源名带空格两个错误版本，记录报错。
4. **排除（对照 4.4）**：按 4.4.4 任务 B 的脚本分别以 `fail` / `exclude` 准备这份数据（limit 设成只够 10 道短题），对比：报错 vs 成功；成功时 `manifest.json` 的 `prompt_length` 三元计数是否为 `original=12, excluded=2, retained=10`；再把两个源中的自制数学源全部换成长题，验证「source has no rows after prompt-length exclusion」。
5. **grading 校验（对照 4.5）**：给注册表里的 math 条目加上 `{"grading": {"objective": "process", "scope": "full", "require_response": false}}` 重跑准备，确认通过且落盘的 `verifiers.json` 原样保留该声明；再依次换成 `{"extra": 1}`（准备期报错）与 `{"objective": "answer", "scope": "thinking"}`（准备期通过、`GradingPolicy.from_spec` 报错），记录两级报错文本。
6. **验证器目标核对（对照 4.2）**：打开准备产物的一行，抄下 `metadata.verifiers`，并在 `verifiers.json` 里找到同名条目的 factory 路径，用 `python -c "import open_instruct.miles.rewards.verifiers"` 确认该模块真实可导入——完成「名字 → 工厂 → 目标」三层的闭环。

## 6. 本讲小结

- **mixture 混合**：`[[data.sources]]` 以「安全名 + 归一配比」声明多源；每个源剥掉 `name`/`fraction`/`pool` 后是一份继承全局 seed/shuffle 的子 `[data]` 配置，可各自选择输入模式；`over_length` 只能写在全局 `[data]`，按源覆盖被拒绝。
- **run_data 准备**：`prepare_data` 是唯一入口，五种模式恰好选一；验证器目标以 `metadata.verifiers`（名字+目标+权重）随样本携带、以 `verifiers.json`（名字→工厂导入路径）提供可信解析；准备产物以 contract + 输入/输出哈希构成不可变身份，staging 目录原子发布。
- **源目录与准备**：`source_preparation` 用钉哈希的输入与评审过的适配器把原始 JSONL 变成 source 包，不支持的行可评审地省略；本轮新增 `nemo_abstention_v1` 适配器，在适配阶段守住「原始 system/user 问题、结构化 question/answer 目标、闭卷无 tools、domain 分层」四条契约并保留 `judge_query`；`source_catalog` 核验资格回执与产物哈希后产出 `[data]` TOML 片段——数据集永远不能自带工厂或导入。
- **超长排除**：`data.over_length = "exclude"` 让训练侧跳过超限 prompt 并按 source/pool 记 `original/excluded/retained` 三元计数进 manifest 与日志；held-out 仍然严格失败；其余校验覆盖所有候选行含被排除行；配置源与池被掏空即失败；策略与上限属于不可变准备身份；本轮在排除记账处叠加了池采样优先级的逐行校验（保留行必须带 1..4 整数标签）。
- **grading 准备期校验（本轮更新）**：注册表条目的 `grading` 声明在准备期只过**语法**校验（键集、枚举、布尔），不导入工厂、不猜默认值；objective×scope 组合约束留给加载期的 `GradingPolicy`；named-judge 绑定替换工厂配置时必须继承原条目的受信 `grading`（显式覆盖优先），继承结果进入 contract——丢弃声明的陈旧缓存被「Preparation contract changed」拒绝。
- **运行期衔接**：`mixture.json` 记录的是排除后文件的行数与哈希，运行期 `mixture_provider` 的行数核对因此继续成立——准备期过滤与运行期采样检查无缝衔接。

## 7. 下一步学习建议

- 下一讲（u8-l5）讲 **rollout 提供者**：本讲产出的 prepared 目录如何被 `PoolPromptProvider` 等按池采样、耗尽与排空，以及本轮可与优先级组序组合的池采样——建议先回顾本讲 4.1 的 `mixture.json` 结构。
- `grading` 声明在**运行期**的完整语义（ResponseParts 切分、`selected_input` 消费者迁移、schema 2 证据隔离）见 **u8-l12（统一判分输入）**；验证器名字如何变成真实的判分进程见 **u8-l6（MILES 奖励体系）**；想在更底层自定义一个验证器，先做 **u6-l3（可验证奖励注册表）** 的实践。
- abstention 判分侧的 rubric、裁判服务复用与失败策略见 **u8-l13（Abstention QA 奖励）** 与 [docs/miles/abstention.md](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/docs/miles/abstention.md)。
- 池采样优先级（`data.sampling.priority`）与独立的 priority 提供者、epoch 预算的完整语义见 **u8-l11**；本讲 4.4 只覆盖了它在准备期的逐行校验。
- 本讲 4.4 的策略与 `inference.max_context_length` / `max_response_length` 联动，复习 **u8-l2（配置系统）** 中 `[inference]` 小节的编译规则。
- 想了解长提示在训练侧的另一条路线（序列打包、长序列预算与准入控制），阅读 [docs/miles/long-sequences.md](https://github.com/allenai/open-instruct/blob/8cd7c9eb4e4d997cd482e9f88f5249d081728d9a/docs/miles/long-sequences.md) 并对照 **u8-l10**。
