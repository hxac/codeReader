# 仓库目录结构与核心入口

## 1. 本讲目标

上一讲（[u1-l1](u1-l1-project-overview.md)）我们建立了全局观：open-r1 是对 DeepSeek-R1 的「完全开源复现」，可执行核心就是 `sft.py`、`grpo.py`、`generate.py` 三个薄脚本，外加一个 `Makefile`。

这一讲我们要回答一个更具体的问题：**「我要找的东西在仓库的哪个目录里？」** 学完本讲你应当能够：

1. 说出仓库顶层每个目录（`src` / `scripts` / `recipes` / `slurm` / `tests`）的职责，能立刻判断一段功能该去哪个目录看。
2. 定位「训练、生成、评估」三大入口文件，并说明它们如何被 `accelerate launch` / `make` / `sbatch` 调起。
3. 理解 `src/open_r1/utils` 这个工具包的导出关系，以及它内部 `competitive_programming` 子模块是干什么的。
4. 看懂 `recipes/` 与 `slurm/` 两个目录的命名约定，能根据「模型名 + 任务 + 配方后缀」推断出对应的 YAML 文件路径。

本讲是后续所有源码精读讲义的「地图」，先把地图刻在脑子里，后面读代码就不会迷路。

## 2. 前置知识

本讲是纯目录与入口的梳理，不需要你懂训练算法。但有几个名词最好先有个印象（上一讲已引入，这里复习）：

- **SFT（监督微调）**：用「问题 + 标准答案/推理过程」教模型模仿，对应入口 `sft.py`。
- **GRPO（强化学习）**：让模型自己生成多个回答，靠「奖励函数」打分来进化，对应入口 `grpo.py`。
- **数据生成（Distilabel）**：用一个大模型批量产出推理数据，对应入口 `generate.py`。
- **三元组配置**：每个训练脚本都解析三个参数对象——脚本参数（`ScriptArguments`）、训练参数（`SFTConfig`/`GRPOConfig`）、模型参数（`ModelConfig`）。这是 open-r1「simple by design」的核心约定，本讲会在入口处再次确认。
- **YAML 配方（recipe）**：把一堆命令行参数写进一个 `.yaml` 文件，训练时用 `--config xxx.yaml` 一次性加载，避免命令行写一长串。

如果你对 Python 包的 `__init__.py` 作用还不熟，只要记住一点：**它决定了一个包「对外暴露哪些名字」**。本讲我们会看到 open-r1 巧妙地用 `utils/__init__.py` 把分散的工具函数集中导出。

## 3. 本讲源码地图

本讲涉及的关键文件如下，按目录归类：

| 文件 | 作用 |
|------|------|
| [README.md](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/README.md) | 项目总说明，给出三大脚本的定位与各种运行命令 |
| [Makefile](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/Makefile) | 把 `install` / `test` / `evaluate` 等常用流程封装成 make 目标 |
| [src/open_r1/__init__.py](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/__init__.py) | 包的入口文件，实际只有许可证头，没有业务导出 |
| [src/open_r1/sft.py](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/sft.py) | SFT 训练入口脚本 |
| [src/open_r1/grpo.py](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/grpo.py) | GRPO 强化学习训练入口脚本 |
| [src/open_r1/generate.py](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/generate.py) | Distilabel 数据生成流水线构造器 |
| [src/open_r1/utils/__init__.py](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/utils/__init__.py) | 工具包的对外导出口，集中暴露 `get_dataset` / `get_model` 等函数 |
| [src/open_r1/utils/competitive_programming/__init__.py](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/utils/competitive_programming/__init__.py) | 竞赛编程评分子包的导出口 |
| [recipes/README.md](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/recipes/README.md) | 配方目录的使用说明，揭示 `--model/--task/--config` 命名约定 |

## 4. 核心概念与源码讲解

### 4.1 顶层目录地图：src / scripts / recipes / slurm / tests

#### 4.1.1 概念说明

一个机器学习项目的仓库通常会被切成「**源码 / 脚本 / 配置 / 调度 / 测试**」五类东西。open-r1 把这个划分做得非常干净，每个顶层目录恰好承担一类职责：

- **`src/`**：真正会被 `import` 的 Python 包（`open_r1`）。训练、生成、奖励等核心逻辑都在这里。
- **`scripts/`**：独立的命令行小工具，**不是**包的一部分，平时用 `python scripts/xxx.py` 直接跑，用于评估、去污染、起路由服务等辅助工作。
- **`recipes/`**：YAML「配方」目录，把训练/过滤的超参数固化成文件，方便复现与分享。
- **`slurm/`**：Slurm 集群的作业提交脚本（`.slurm` / `.sh`），用于多机多卡大规模训练与推理服务部署。
- **`tests/`**：单元测试与慢测试，保证核心函数行为正确。

这个「五目录」划分是后续阅读的指南针：想看「怎么训练」去 `src/`；想跑「一键命令」看 `Makefile` 和 `scripts/`；想换「超参数」改 `recipes/`；想上「集群」用 `slurm/`；想验证「对不对」跑 `tests/`。

#### 4.1.2 核心流程

当你要复现一个实验时，调用链大致是：

```text
你 ──sbatch──> slurm/train.slurm ──> accelerate launch ──> src/open_r1/sft.py
                                          │
                          读取 recipes/<模型>/<任务>/config_xxx.yaml
```

即：**调度脚本（slurm）→ 启动器（accelerate）→ 入口脚本（src）→ 配方（recipes）**。理解了这条链，你就知道改哪一层会影响到什么。

README 里用一句话点明了这种「simple by design」的哲学——项目核心就是几个脚本加一个 Makefile：

> The project is simple by design and mostly consists of `src/open_r1`（含三个脚本）与 `Makefile`。详见 [README.md:21-28](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/README.md#L21-L28)。

下面是仓库顶层目录与各自包含内容的速览（基于实际 git 跟踪文件整理）：

```text
open-r1/
├── README.md                  # 总说明（安装、训练、评估、数据生成）
├── Makefile                   # install / style / quality / test / evaluate 等命令
├── setup.py / setup.cfg       # Python 打包与 ruff/flake8 配置
├── LICENSE
├── assets/plan-of-attack.png  # 三步走示意图
├── logs/                      # 训练日志输出目录（.gitkeep 占位）
├── src/open_r1/               # 【核心包】训练/生成/奖励源码（详见 4.2）
├── scripts/                   # 【独立工具】评估、去污染、路由服务（详见 4.3 无，此处简述）
│   ├── decontaminate.py        #   8-gram 数据去污染
│   ├── run_benchmarks.py       #   批量提交 lighteval 评测
│   ├── e2b_router.py           #   E2B 沙箱路由服务
│   ├── morph_router.py         #   Morph 沙箱路由服务
│   └── pass_rate_filtering/    #   GRPO 数据通过率过滤
├── recipes/                   # 【YAML 配方】训练超参数（详见 4.3）
├── slurm/                     # 【集群脚本】多机训练/服务部署（详见 4.3）
└── tests/                     # 【测试】
    ├── test_rewards.py         #   奖励函数单元测试
    ├── utils/test_data.py      #   数据加载单元测试
    └── slow/test_code_reward.py#   代码奖励的慢测试（需沙箱）
```

#### 4.1.3 源码精读

`Makefile` 是「五目录」之外的总开关，它把 `src` 加进 `PYTHONPATH`，并定义了几个最常用的目标：

[Makefile:4-6](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/Makefile#L4-L6) —— 这两行很关键：`export PYTHONPATH = src` 让你无需 `pip install` 就能 `import open_r1`；`check_dirs := src tests` 则界定了代码风格检查的范围（注意 `scripts/` 不在风格检查里，它是更松散的工具脚本）。

`Makefile` 的几个核心目标对照如下（摘自 [Makefile:10-31](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/Makefile#L10-L31)）：

| 目标 | 命令 | 作用 |
|------|------|------|
| `make install` | 建 venv + 装 vllm/flash-attn + `pip install -e ".[dev]"` | 一键开发环境 |
| `make test` | `pytest -sv --ignore=tests/slow/ tests/` | 跑快速单元测试（跳过需沙箱的慢测试） |
| `make slow_test` | `pytest -sv -vv tests/slow/` | 跑慢测试 |
| `make style` / `make quality` | ruff + isort + flake8 | 格式化 / 代码质量检查 |
| `make evaluate` | `lighteval vllm ...` | 评估模型（详见 u8 单元） |

注意 `make test` 故意 `--ignore=tests/slow/`——这就是为什么 `tests/` 要分两层：普通测试人人能跑，慢测试需要外部沙箱服务。

#### 4.1.4 代码实践

**实践目标**：用真实命令确认「五目录」的职责，建立肌肉记忆。

**操作步骤**：

1. 在仓库根目录运行 `git ls-files | head -40`，观察顶层文件。
2. 运行 `git ls-files src/open_r1 | wc -l` 与 `git ls-files scripts | wc -l`，对比「核心包」与「独立脚本」的文件数量。
3. 运行 `make -n test`（`-n` 表示只打印命令不执行），查看 `make test` 实际会执行的 pytest 命令。

**需要观察的现象**：

- `src/open_r1` 下的文件都是 `.py` 包模块；`scripts/` 下也都是 `.py`，但它们不会被 `import open_r1` 引入，只能命令行单独跑。
- `make -n test` 打印出的命令里带有 `--ignore=tests/slow/`。

**预期结果**：你能口述出「想跑测试用 `make test`，它会跳过 `tests/slow/`」。

**待本地验证**：`git ls-files` 的具体计数随版本变化，请以你本地的实际输出为准。

#### 4.1.5 小练习与答案

**练习 1**：你想给一个奖励函数加单元测试，应该把测试文件放在 `tests/` 还是 `tests/slow/`？

> **答案**：放在 `tests/`（如已有的 `tests/test_rewards.py`）。只有需要外部沙箱/网络服务的测试才放 `tests/slow/`，因为 `make test` 会忽略后者。

**练习 2**：为什么 `Makefile` 要写 `export PYTHONPATH = src`？

> **答案**：open-r1 的包源码在 `src/open_r1`，而不是仓库根目录的 `open_r1`。把 `src` 加进 `PYTHONPATH` 后，即使没有 `pip install -e .`，也能直接 `import open_r1`，方便本地边改边测。

---

### 4.2 三大入口脚本与 utils 包导出关系

#### 4.2.1 概念说明

上一讲已经点名的三个入口脚本，本讲我们从「目录与导入」的角度再看一遍：

- **`sft.py`**：SFT 蒸馏的入口。被 `accelerate launch` 调起。
- **`grpo.py`**：GRPO 强化学习的入口。比 `sft.py` 多注入了一组「奖励函数」。
- **`generate.py`**：Distilabel 数据生成的流水线构造器，**不是一个 `main` 脚本**，而是提供一个 `build_distilabel_pipeline()` 函数，由 `slurm/generate.slurm` 或你自己的脚本调用。

这三个脚本都很「薄」——它们只负责**组装**：把数据加载、模型加载、训练器（Trainer）拼到一起，真正的训练能力来自 TRL 库。而「数据加载、模型加载」这些可复用的零件，统一放在 `src/open_r1/utils/` 里，并通过 `utils/__init__.py` 集中导出。

这里有一个反直觉的设计点值得记住：**`src/open_r1/__init__.py` 几乎是空的**（只有许可证头），真正的「对外接口」不在包根，而在 `utils/__init__.py`。所以你在脚本里看到的是 `from open_r1.utils import get_dataset, get_model, get_tokenizer`，而不是 `from open_r1 import ...`。

#### 4.2.2 核心流程

三个入口脚本共享同一个「启动套路」：

```text
TrlParser 解析三元组 (ScriptArguments, TrainingConfig, ModelConfig)
        │
        ├── 训练参数 → set_seed / 配置日志
        ├── 脚本参数 → get_dataset()   （来自 utils.data）
        ├── 模型参数 → get_model() / get_tokenizer()  （来自 utils.model_utils）
        │
        └── 组装 Trainer（SFTTrainer / GRPOTrainer）→ train() → save → push_to_hub
```

GRPO 多出来的一步是：**`from open_r1.rewards import get_reward_funcs`**，把奖励函数列表注入 `GRPOTrainer`。这是 `grpo.py` 与 `sft.py` 在导入上最明显的区别。

`utils/__init__.py` 的导出则像一张「零件清单」：

```text
utils/__init__.py
  ├── get_dataset          （来自 utils.data）          —— 加载数据集/混合数据集
  ├── get_model            （来自 utils.model_utils）   —— 按精度/量化加载模型
  ├── get_tokenizer        （来自 utils.model_utils）   —— 加载分词器并处理 chat template
  ├── is_e2b_available     （来自 utils.import_utils）  —— 是否装了 E2B 沙箱依赖
  └── is_morph_available   （来自 utils.import_utils）  —— 是否装了 Morph 沙箱依赖
```

注意：`callbacks.py`、`hub.py`、`wandb_logging.py` 等**没有**放进这张清单，它们在脚本里是直接 `from open_r1.utils.callbacks import get_callbacks` 这样精确导入的——也就是说，清单里只放「到处都要用的核心零件」，其余按需导入。

#### 4.2.3 源码精读

先看包根入口 `src/open_r1/__init__.py`——它真的只有许可证，没有任何 `import` 或 `__all__`：

[src/open_r1/__init__.py:1-14](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/__init__.py#L1-L14) —— 14 行全部是许可证注释，确认了「包根不导出任何东西」的设计。

再看真正的对外接口 `utils/__init__.py`：

[src/open_r1/utils/__init__.py:1-6](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/utils/__init__.py#L1-L6) —— 这 6 行把 `get_dataset`、`get_model`、`get_tokenizer`、`is_e2b_available`、`is_morph_available` 从各自子模块汇聚上来，并声明 `__all__`。这就是为什么入口脚本能用一行 `from open_r1.utils import get_dataset, get_model, get_tokenizer` 同时拿到三个核心函数。

入口脚本的导入区块印证了上面的「零件清单」。SFT 脚本是这样拼装零件的：

[src/open_r1/sft.py:45-49](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/sft.py#L45-L49) —— `sft.py` 从 `open_r1.utils` 拿数据/模型/分词器，从 `open_r1.utils.callbacks` 拿回调，从 `open_r1.utils.wandb_logging` 拿实验追踪初始化，而 `SFTTrainer`、`TrlParser`、`ModelConfig` 来自 `trl` 库（训练能力委托出去）。

GRPO 脚本的导入几乎一样，只多了一行奖励函数：

[src/open_r1/grpo.py:24-29](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/grpo.py#L24-L29) —— 注意第 25 行 `from open_r1.rewards import get_reward_funcs`，这是 GRPO 区别于 SFT 的关键导入。

两个脚本末尾的启动块完全同构，只是三元组的类型不同：

[src/open_r1/sft.py:166-169](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/sft.py#L166-L169) —— SFT 解析 `(ScriptArguments, SFTConfig, ModelConfig)` 三元组并调用 `main()`。

[src/open_r1/grpo.py:178-181](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/grpo.py#L178-L181) —— GRPO 解析 `(GRPOScriptArguments, GRPOConfig, ModelConfig)` 三元组。`GRPOScriptArguments` 比 `ScriptArguments` 多带了一堆奖励相关超参数（详见 u3 单元）。

第三个入口 `generate.py` 则完全是另一种形态——它**没有** `if __name__ == "__main__"`，而是一个函数库：

[src/open_r1/generate.py:23-36](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/generate.py#L23-L36) —— `build_distilabel_pipeline(...)` 接收模型名、prompt 模板、温度、`num_generations` 等参数，返回一个 distilabel 的 `Pipeline` 对象，再由调用方 `.run()` 并 `push_to_hub`。

最后看一下竞赛编程评分子包的导出，它展示了「子包内部也用 `__init__.py` 收口」的同一套模式：

[src/open_r1/utils/competitive_programming/__init__.py:1-19](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/utils/competitive_programming/__init__.py#L1-L19) —— 把 IOI 评分（`score_subtask`、`SubtaskResult`）、Codeforces 评分（`score_submission`）、Piston/Morph 客户端工厂、代码补丁（`patch_code`）等从 6 个子模块汇聚导出。这个子包是 open-r1 最有特色、也最重的部分，将在 u5、u6 单元专门讲解。

#### 4.2.4 代码实践

**实践目标**：验证「入口脚本通过 `utils/__init__.py` 拿到核心零件」这一关系，并亲手触发一次导入。

**操作步骤**：

1. 在仓库根目录确认 `PYTHONPATH` 已包含 `src`（`Makefile` 已设）。运行：
   ```bash
   PYTHONPATH=src python -c "from open_r1.utils import get_dataset, get_model, get_tokenizer; print('ok')"
   ```
2. 再运行下面这行，直接打印 `utils` 包对外暴露的全部名字：
   ```bash
   PYTHONPATH=src python -c "import open_r1.utils as u; print(u.__all__)"
   ```
3. 对比 `open_r1` 包根本身是否也导出了这些名字：
   ```bash
   PYTHONPATH=src python -c "import open_r1; print(getattr(open_r1, '__all__', '无 __all__'))"
   ```

**需要观察的现象**：

- 第 1 步打印 `ok`，说明三个核心函数确实能从 `open_r1.utils` 一次性导入。
- 第 2 步打印 `['get_tokenizer', 'is_e2b_available', 'is_morph_available', 'get_model', 'get_dataset']`。
- 第 3 步打印 `无 __all__`，印证包根是空的。

**预期结果**：你亲眼确认了「核心零件清单在 `utils/__init__.py`，不在包根」。

**待本地验证**：若依赖（如 `datasets`、`trl`）未安装，第 1 步可能报 `ModuleNotFoundError`，请先按 u1-l3 安装 `.[dev]`。

#### 4.2.5 小练习与答案

**练习 1**：`grpo.py` 比 `sft.py` 多了哪一行关键导入？它说明了 GRPO 的什么特性？

> **答案**：多了 `from open_r1.rewards import get_reward_funcs`。这说明 GRPO 是靠「奖励函数打分」驱动的强化学习，而 SFT 只需要标准答案、不需要奖励。

**练习 2**：`generate.py` 为什么没有 `if __name__ == "__main__"`？

> **答案**：因为它不是直接运行的脚本，而是一个**函数库**，提供 `build_distilabel_pipeline()`。实际运行由 `slurm/generate.slurm` 或用户自己写的脚本（如 README 里的 `pipeline.py` 示例）来调用它。

**练习 3**：为什么脚本里写 `from open_r1.utils import ...` 而不是 `from open_r1 import ...`？

> **答案**：因为 `src/open_r1/__init__.py` 是空的（只有许可证），没有导出任何名字；核心函数都由 `src/open_r1/utils/__init__.py` 收口导出。

---

### 4.3 recipes 与 slurm 目录的组织约定

#### 4.3.1 概念说明

`recipes/` 和 `slurm/` 是两个「约定优于配置」的目录，它们各自的文件命名都遵循固定规则，看懂规则就能盲猜路径。

**`recipes/`（配方目录）** 的组织逻辑是：**一个模型 + 一个任务 = 一个子目录，里面放若干 YAML 配方**。命名模板为：

```text
recipes/<模型名>/<任务>/config_<配方后缀>.yaml
```

其中 `<任务>` 只有两种取值：`sft` 或 `grpo`。例如：

- `recipes/OpenR1-Distill-7B/sft/config_distill.yaml` —— 给 OpenR1-Distill-7B 做 SFT 蒸馏的配方。
- `recipes/Qwen2.5-1.5B-Instruct/grpo/config_demo.yaml` —— 给 Qwen2.5-1.5B-Instruct 做 GRPO 的演示配方。
- `recipes/Qwen2.5-Coder-7B-Instruct/grpo/config_codeforces.yaml` —— 同模型但面向 Codeforces 竞赛编程的配方。

此外 `recipes/` 还有两个特殊子目录：`accelerate_configs/`（存放 DDP/FSDP/ZeRO2/ZeRO3 等 accelerate 启动配置）和 `dataset_filtering/`（GRPO 数据按通过率过滤的配方）。

**`slurm/`（集群脚本目录）** 的组织逻辑则是：**一个流程阶段 = 一个 `.slurm` 脚本**。核心是 `train.slurm`，它接受 `--model`、`--task`、`--config` 三个参数，并据此去 `recipes/` 里找对应的 YAML。命名约定让两者天然对得上：

```text
sbatch slurm/train.slurm --model OpenR1-Distill-7B --task sft --config distill
                         ↓                ↓             ↓
        recipes/OpenR1-Distill-7B/sft/config_distill.yaml
```

#### 4.3.2 核心流程

一次 Slurm 训练的「参数 → 路径」解析流程：

```text
sbatch slurm/train.slurm --model {M} --task {T} --config {C} --accelerator {A}
        │
        ├── 拼出 YAML 路径：recipes/{M}/{T}/config_{C}.yaml
        ├── 选 accelerate 配置：recipes/accelerate_configs/{A}.yaml
        └── srun + accelerate launch src/open_r1/{T}.py --config <上面的 YAML>
```

也就是说，`--task` 同时决定了「用哪个入口脚本」和「recipes 下的哪一层子目录」。`--config` 只是该子目录下某个 YAML 的后缀名。`--accelerator`（如 `zero2`/`zero3`/`fsdp`/`ddp`）决定显存优化策略。

`slurm/` 目录里的其他脚本对应不同阶段：

| 脚本 | 阶段 |
|------|------|
| `train.slurm` | 训练（SFT/GRPO） |
| `generate.slurm` | 数据生成 |
| `evaluate.slurm` | 评估 |
| `compute_pass_rate.slurm` | GRPO 数据通过率计算 |
| `serve_r1.slurm` / `serve_router.slurm` / `experimental/serve_r1_vllm.slurm` | 推理服务部署 |
| `e2b_router.slurm` / `morph_router.slurm` | 沙箱路由服务（防限流） |
| `piston/` | Piston 代码执行 worker 部署 |

#### 4.3.3 源码精读

`recipes/README.md` 用具体命令揭示了命名约定。复现 OpenR1-Distill-7B 的命令是：

[recipes/README.md:5-9](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/recipes/README.md#L5-L9) —— `--model OpenR1-Distill-7B --task sft --config distill --accelerator zero3`，对照仓库即 `recipes/OpenR1-Distill-7B/sft/config_distill.yaml` 与 `recipes/accelerate_configs/zero3.yaml`。

训练 OlympicCoder-32B 时则切到 FSDP（因为 32B 模型显存压力大）：

[recipes/README.md:13-21](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/recipes/README.md#L13-L21) —— `--task sft --config v00.00 --accelerator fsdp`，对应 `recipes/OlympicCoder-32B/sft/config_v00.00.yaml`。这里能看到同一个 `--task sft` 在不同模型下指向不同子目录，但目录结构完全一致。

README 主文档也明示了「`--config` 后缀 → YAML 文件」的对应关系，并允许在命令行追加参数覆盖 YAML：

[README.md:398-402](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/README.md#L398-L402) —— `{config_suffix}` 指「具体的 config」，`{accelerator}` 指 `recipes/accelerate_configs` 下的 accelerate 配置。

#### 4.3.4 代码实践

**实践目标**：根据命名约定，盲猜一个不存在的配方的 YAML 路径，并用真实命令验证你的推断。

**操作步骤**：

1. 在仓库根目录列出所有配方：
   ```bash
   git ls-files 'recipes/**/*.yaml'
   ```
2. 假设你要给 `Qwen2.5-1.5B-Instruct` 跑 GRPO 的 `demo` 配方，先**手写**出你预期的 YAML 路径，再运行：
   ```bash
   git ls-files 'recipes/Qwen2.5-1.5B-Instruct/grpo/config_demo.yaml'
   ```
3. 同样手写并验证对应的 `sb`urm 命令会选中的 accelerate 配置与入口脚本：
   ```bash
   git ls-files 'recipes/accelerate_configs/zero2.yaml' 'src/open_r1/grpo.py'
   ```

**需要观察的现象**：

- 第 1 步会列出所有 YAML，你能看到 `<模型>/<sft|grpo>/config_xxx.yaml` 的统一形态，外加 `accelerate_configs/` 与 `dataset_filtering/` 两类。
- 第 2、3 步都能匹配到真实文件，证明命名约定成立。

**预期结果**：你能在不看文档的情况下，从 `sbatch ... --model X --task grpo --config Y` 反推出 `recipes/X/grpo/config_Y.yaml` 和 `src/open_r1/grpo.py`。

**待本地验证**：文件清单以你本地 `git ls-files` 输出为准。

#### 4.3.5 小练习与答案

**练习 1**：命令 `sbatch slurm/train.slurm --model OlympicCoder-7B --task sft --config v00.00 --accelerator zero3` 会用到哪两个 YAML 文件、哪个入口脚本？

> **答案**：`recipes/OlympicCoder-7B/sft/config_v00.00.yaml`、`recipes/accelerate_configs/zero3.yaml`，入口脚本是 `src/open_r1/sft.py`。

**练习 2**：`recipes/accelerate_configs/` 下的文件（如 `zero3.yaml`、`fsdp.yaml`）和 `recipes/<模型>/<任务>/config_xxx.yaml` 是同一类东西吗？

> **答案**：不是。前者是 **accelerate 启动器配置**（决定多卡/显存优化策略，由 `accelerate launch --config_file` 读取）；后者是**训练超参数配方**（由入口脚本经 `TrlParser --config` 读取）。两者由 `slurm/train.slurm` 的不同参数分别选中。

**练习 3**：`slurm/e2b_router.slurm` 与 `slurm/morph_router.slurm` 解决的是什么问题？

> **答案**：当大量训练任务同时调用 E2B/Morph 云沙箱执行代码时容易被限流。这两个脚本起一个「路由服务」，让所有任务共享同一个路由 IP，由路由统一管理并发，从而规避沙箱限流（详见 u5-l3）。

## 5. 综合实践

把本讲三个模块串起来，完成一次「目录寻宝」：假设你想复现 **OpenR1-Distill-7B 的 SFT 蒸馏**，请按下面的清单走一遍，把「调度 → 入口 → 配方 → 零件」这条链亲手连起来。

1. **画目录树**：用 `tree` 或 `git ls-files` 输出 `src/open_r1` 的完整文件列表，整理成树状图，为每个 `.py` 文件写一句话职责说明（参考 4.1.2 的速览表与 4.2 的零件清单），并用星号 `★` 标出训练主入口 `sft.py`。
2. **追导入链**：打开 `src/open_r1/sft.py`，从 [第 45-49 行的导入区](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/sft.py#L45-L49) 出发，逐个找出 `get_dataset`、`get_model`、`get_tokenizer`、`get_callbacks`、`init_wandb_training` 分别定义在 `utils/` 下的哪个文件里。
3. **验命名约定**：写出复现命令
   ```bash
   sbatch --nodes=1 slurm/train.slurm --model OpenR1-Distill-7B --task sft --config distill --accelerator zero3
   ```
   然后用 `git ls-files` 验证它要找的 YAML（`recipes/OpenR1-Distill-7B/sft/config_distill.yaml` 与 `recipes/accelerate_configs/zero3.yaml`）确实存在。
4. **形成一句话结论**：用一句话回答「open-r1 的代码、配方、调度分别放在哪三个目录，它们如何被一条 `sbatch` 命令串起来？」

完成后，你应当在脑子里拥有一张可点击的「地图」，后续读 `sft.py` / `grpo.py` 内部逻辑时，随时能定位到对应的工具模块。

## 6. 本讲小结

- 仓库顶层分为五类目录：**`src/`（核心包）、`scripts/`（独立工具）、`recipes/`（YAML 配方）、`slurm/`（集群脚本）、`tests/`（测试）**，职责互不重叠。
- 三大入口脚本中，`sft.py` 与 `grpo.py` 是带 `if __name__ == "__main__"` 的可运行入口，`generate.py` 则是提供 `build_distilabel_pipeline()` 的函数库。
- 入口脚本通过 `from open_r1.utils import ...` 拿到核心零件；**真正的对外接口在 `utils/__init__.py`，而包根 `open_r1/__init__.py` 是空的**。
- `recipes/` 的命名约定是 `recipes/<模型名>/<sft|grpo>/config_<后缀>.yaml`，`--task` 同时决定入口脚本与配方子目录。
- `slurm/train.slurm` 用 `--model/--task/--config/--accelerator` 四个参数把「调度 → 入口 → 训练配方 → accelerate 配置」串成一条链。
- `make test` 故意 `--ignore=tests/slow/`，所以 `tests/` 分两层：快速单元测试人人可跑，慢测试需外部沙箱。

## 7. 下一步学习建议

本讲只画了地图，还没进任何一扇门。建议按以下顺序继续：

1. **先搞懂配置系统**：下一讲 [u1-l4 配置系统与 YAML 训练配方](u1-l4-config-system.md) 会带你读 `configs.py` 里的 `ScriptArguments` / `SFTConfig` / `GRPOConfig` 数据类和 `TrlParser` 如何合并命令行与 YAML，这是理解入口脚本 `main()` 的前提。
2. **再装好环境**：如果还没装，先做 [u1-l3 安装与环境搭建](u1-l3-installation-setup.md)，确保能 `import open_r1`。
3. **进阶阅读入口脚本**：装好环境、懂了配置后，进入 u2 单元逐段精读 `src/open_r1/sft.py` 的 `main()` 函数。
4. **延展阅读**：对竞赛编程感兴趣的话，可以先把 `src/open_r1/utils/competitive_programming/` 子包的 `__init__.py`（本讲 4.2.3 已引用）当目录页浏览一遍，建立印象，到 u5/u6 单元再深入。
