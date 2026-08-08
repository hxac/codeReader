# 目录结构与代码地图

## 1. 本讲目标

上一讲我们建立了 slime 的全局认知：它是一个把 Megatron 训练与 SGLang 推理缝合成「采样→训练→权重同步」闭环的 RL 后训练框架。本讲的目标是给你一张**读源码时的导航地图**。学完本讲，你应该能够：

1. 说出 slime 仓库的每个顶层目录（`slime/`、`slime_plugins/`、`tools/`、`scripts/`、`examples/`、`tests/`、`docs/` 等）分别存放什么。
2. 区分核心包 `slime/` 下的五大子包 `ray/`、`backends/`、`rollout/`、`utils/`、`agent/` 各自承担什么职责。
3. 知道 `train.py` / `train_async.py` 是程序入口，`slime/utils/arguments.py` 是参数中枢，并在拿到一个文件路径时能判断它属于哪一层。

本讲不展开任何算法细节，只解决一个问题：**代码放在哪里、各目录之间是什么关系**。有了这张地图，后续每一讲你都能迅速定位到正确的目录。

## 2. 前置知识

阅读本讲前，建议你已经读完 [u1-l1 项目总览](u1-l1-project-overview.md)，了解 slime 的三大模块（rollout / data buffer / training）和它们串成闭环的数据流。本讲会用到几个基础概念：

- **Python 包（package）**：一个含有 `__init__.py` 的目录，可以被 `import`。slime 既是一个可 `import` 的包，又是一个带命令行入口的程序。
- **入口文件（entry point）**：程序真正开始执行的那个 `.py` 文件，slime 的入口是仓库根目录下的 `train.py`。
- **子包（subpackage）**：包内部的下一级目录，slime 用子包来划分「编排 / 训练后端 / 推理后端 / 数据生成 / 工具」这些不同关注点。
- **插件（plugin）**：与核心包分离、可独立替换的扩展代码，slime 把「自定义模型结构」这类内容放在 `slime_plugins/`。

如果你还不熟悉 RL 训练的基本流程（采样、奖励、优势估计、策略更新），不必担心——本讲只讲目录，不讲算法。

## 3. 本讲源码地图

本讲涉及的关键文件如下，它们主要用来确认「包的边界」和「入口在哪里」：

| 文件 / 目录 | 作用 |
| --- | --- |
| `setup.py` | 打包配置，声明了 slime 包含哪些目录、依赖什么、要求的 Python 版本 |
| `pyproject.toml` | 工具链配置（black/isort/ruff/pytest），并把 `slime` 与 `slime_plugins` 标记为「第一方代码」 |
| `requirements.txt` | 运行期依赖列表（ray、sglang-router、transformers 等） |
| `train.py` | 主入口：同步训练循环 |
| `train_async.py` | 异步训练入口：提前发起下一轮 rollout |
| `slime/__init__.py` | 核心包的初始化文件（本仓库中为空，是一个值得注意的设计选择） |
| `slime/utils/arguments.py` | 参数中枢：合并 Megatron / SGLang / slime 三族参数 |

## 4. 核心概念与源码讲解

### 4.1 顶层目录总览与包的边界

#### 4.1.1 概念说明

打开 slime 仓库，你会看到一批顶层目录。在动手读源码之前，先搞清楚两件事：

1. **哪些目录属于「核心包」**——即被 `import slime` 时真正引入的代码；
2. **哪些目录属于「外围资源」**——启动脚本、文档、示例、测试等，它们不会被 import，但对运行和学习至关重要。

slime 用 `setup.py` 明确划定了核心包的边界：只有名字以 `slime` 或 `slime_plugins` 开头的目录才算作第一方包。

#### 4.1.2 核心流程

把顶层目录按角色分成三类来记忆：

- **核心代码**：`slime/`（框架本体）、`slime_plugins/`（模型/缓冲区插件）。
- **运行入口与脚本**：`train.py` / `train_async.py`（程序入口）、`scripts/`（启动脚本）、`tools/`（权重转换等命令行工具）。
- **学习与工程资源**：`examples/`（可运行示例）、`tests/`（测试）、`docs/`（文档）、`docker/`（镜像构建）、`imgs/`（架构图）。

一个典型的命令行运行链路是：用户执行 `scripts/run-*.sh` → 内部调用 `ray job submit` 启动 `train.py` → `train.py` 从 `slime/` 各子包中组装出训练循环。所以理解目录，本质上是理解「一次训练是从哪些目录里拼出来的」。

#### 4.1.3 源码精读

包的边界在 `setup.py` 中用 `find_packages` 显式声明，只纳入 `slime*` 和 `slime_plugins*`：

[setup.py:36](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/setup.py#L36) —— 用 `find_packages(include=["slime*", "slime_plugins*"])` 声明只有这两个前缀的目录会被打包成可 import 的模块，`scripts/`、`tools/`、`examples/` 都不在此列。

[setup.py:36-40](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/setup.py#L36-L40) —— 同一段 `setup(...)` 中，`install_requires` 从 `requirements.txt` 读取依赖，`python_requires=">=3.10"` 限定最低 Python 版本。

`pyproject.toml` 在工具链层面再次确认了同样的「第一方代码」划分，并指定了测试目录：

[pyproject.toml:17-19](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/pyproject.toml#L17-L19) —— `known_first_party = ["slime", "slime_plugins"]`、`src_paths = ["slime", "slime_plugins"]`，告诉 isort/ruff 这两个是项目自己的代码，排序与静态检查都按第一方处理。

[pyproject.toml:44](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/pyproject.toml#L44) —— `testpaths = ["./tests"]`，pytest 只在 `tests/` 下发现测试用例。

依赖方面，`requirements.txt` 透露了 slime 的两大技术栈依赖：

[requirements.txt:18](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/requirements.txt#L18) `ray[default]` —— 编排层基于 Ray。
[requirements.txt:21](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/requirements.txt#L21) `sglang-router>=0.3.0` —— 推理后端基于 SGLang。
[requirements.txt:25](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/requirements.txt#L25) `xxhash` —— 注释写明用于 disk delta weight sync（增量权重同步的校验与编解码）。

#### 4.1.4 代码实践

实践目标：亲自确认「哪些目录是核心包、哪些不是」。

操作步骤：

1. 在仓库根目录执行 `python -c "from setuptools import find_packages; print(find_packages(include=['slime*','slime_plugins*']))"`。
2. 观察输出列表，确认其中只包含 `slime`、`slime.agent`、`slime.backends...`、`slime_plugins.models...` 等，**不包含** `scripts`、`tools`、`examples`。
3. 再执行 `git ls-files | cut -d/ -f1 | sort | uniq -c`，统计每个顶层目录下被 git 跟踪的文件数量。

需要观察的现象：`find_packages` 的输出与 `git ls-files` 的统计能对应上——核心代码集中在 `slime/` 与 `slime_plugins/`，其余顶层目录是脚本/资源。

预期结果：你会看到 `slime/` 下有上百个 `.py` 文件，而 `tools/`、`scripts/` 虽然也有不少文件，但它们不会被 `import slime` 引入。若 `find_packages` 命令在你的环境因缺少 `setuptools` 而失败，可改为直接阅读 `setup.py` 第 36 行得到同样结论。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `tools/convert_hf_to_torch_dist.py` 不能用 `import tools.convert_hf_to_torch_dist` 来调用？
**答案**：因为 `tools` 不在 `setup.py` 的 `find_packages(include=["slime*", "slime_plugins*"])` 范围内，它不是可 import 的包，而是通过命令行 `python tools/convert_hf_to_torch_dist.py ...` 直接执行的脚本。

**练习 2**：`requirements.txt` 里的 `xxhash` 和 `blake3` 分别服务于哪类功能？（提示：看注释）
**答案**：`xxhash` 的注释明确写明用于 disk delta weight sync（增量权重同步的校验和与编解码）；`blake3` 同样是哈希库，配合做权重差异的快速校验。它们都服务于 U5 将要讲的权重同步模块。

### 4.2 `slime/` 包的五大子包导航

#### 4.2.1 概念说明

`slime/` 是框架本体。它没有按「功能」零散地堆放文件，而是用五个子包清晰地切分关注点。先把一句话职责记在心里，再读源码就不会迷路：

| 子包 | 一句话职责 | 文件数（约） |
| --- | --- | --- |
| `ray/` | **编排层**：用 Ray 分配 GPU、创建训练/推理工人、暴露统一接口 | 8 |
| `backends/` | **两个重量级后端**：`megatron_utils/`(训练) + `sglang_utils/`(推理) | 62 |
| `rollout/` | **数据生成层**：调推理引擎采样、算奖励、过滤、产出训练样本 | 22 |
| `utils/` | **共享工具与参数中枢**：参数解析、PPO 数学、类型定义、指标/追踪 | 34 |
| `agent/` | **智能体 RL 运行时**：协议适配器、轨迹管理、沙箱 | 13 |

一个值得注意的细节：核心包的入口文件 `slime/__init__.py` 是**空文件**。这意味着 slime 没有在包级别维护全局状态或配置，一切组件都是在程序入口（`train.py`）按需 import、动态拼装的。

[slime/__init__.py](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/__init__.py#L1) —— 空的包初始化文件，说明 `import slime` 不会触发任何隐式逻辑，所有装配工作由入口脚本显式完成。

#### 4.2.2 核心流程

五大子包在一次训练中是这样协作的（数据/控制流方向）：

```text
        ┌──────────────────────────── train.py（入口，编排） ────────────────────────────┐
        │                                                                                │
        ▼                                                                                ▼
┌───────────────┐  分配 GPU / 建工人    ┌──────────────────┐   采样请求    ┌─────────────────────┐
│   slime/ray/  │ ───────────────────▶ │ slime/backends/  │ ───────────▶ │   slime/rollout/    │
│  编排层        │                       │ megatron_utils/  │              │  数据生成层          │
│ (placement    │ ◀─────────────────── │  (训练工人)       │ ◀─────────── │ (generate_rollout)  │
│   group)      │   训练 / 同步权重      │ sglang_utils/    │   训练样本    │  奖励/过滤           │
└───────────────┘                       │  (推理工人)       │              └─────────────────────┘
        │                                └──────────────────┘                       │
        │  parse_args / 类型 / 指标                                                       │ Sample 数据载体
        ▼                                                                                ▼
┌───────────────────────────┐                                    ┌──────────────────────────┐
│       slime/utils/        │                                    │       slime/agent/       │
│ arguments / ppo_utils /   │ ◀──────── 各层共享 ──────────────── │  智能体运行时（可选）      │
│ types / trace / metric    │                                    │ adapters / trajectory    │
└───────────────────────────┘                                    └──────────────────────────┘
```

- `ray/` 是「指挥」，负责把 GPU 划给训练还是推理、把工人对象创建出来；
- `backends/` 是「干重活的两个工人」，左边 `megatron_utils/` 训练，右边 `sglang_utils/` 推理；
- `rollout/` 是「数据车间」，调用推理工人产出带奖励的样本，再交给训练工人消费；
- `utils/` 是「公共库」，被前四者共同依赖；
- `agent/` 是「可选的智能体扩展」，当你做多轮工具调用 / 沙箱 agent 时才会用到。

#### 4.2.3 源码精读

逐个子包看关键文件，建立「需要改 X 功能时去哪里找」的索引。

**(1) `slime/ray/`（编排层）** —— 文件最少但地位最高，是 `train.py` 直接 import 的层：

[slime/ray/](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/ray/__init__.py#L1) 目录下，关键文件包括 `placement_group.py`（分配 GPU、创建工人）、`actor_group.py`、`train_actor.py`（训练工人抽象）、`rollout.py`（RolloutManager 封装）、`rollout_validation.py`、`utils.py`。

**(2) `slime/backends/`（两个后端）** —— 文件最多的子包（约 62 个 `.py`），又分为两半：

- `megatron_utils/`：训练后端，含 `actor.py`、`model.py`、`loss.py`、`data.py`、`model_provider.py`、`initialize.py`，以及三个子目录 `server/`（HTTP 服务端）、`update_weight/`（权重同步）、`megatron_to_hf/`（检查点格式转换）。
- `sglang_utils/`：推理后端，含 `sglang_engine.py`、`sglang_config.py`、`external.py`、`server_control.py`、`arguments.py`。

[slime/backends/](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/__init__.py#L1) 的 `__init__.py` 也是空的，同样遵循「子包独立、入口装配」的风格。

**(3) `slime/rollout/`（数据生成层）** —— 含 `sglang_rollout.py`（默认 rollout 函数）、`data_source.py`（数据源与缓冲区）、`base_types.py`（Sample 等数据结构）、`fully_async_rollout.py`、`sglang_streaming_rollout.py`，以及 `rm_hub/`（奖励模型分发）、`filter_hub/`（动态采样过滤）两个子目录。

**(4) `slime/utils/`（共享工具）** —— 文件最多的「杂货铺」但井井有条：`arguments.py`（参数中枢）、`ppo_utils.py`（优势估计）、`types.py`（Sample 类型）、`misc.py`（含 `load_function`、`should_run_periodic_action`）、`trace_utils.py`、`metric_utils.py`、`dp_schedule.py`、`disk_delta.py` 等。

**(5) `slime/agent/`（智能体运行时）** —— 含 `adapters/`（`anthropic.py`、`openai.py`、`common.py`，把外部协议适配成可训练轨迹）、`harness/`（`claude_code.py`、`codex.py`）、`trajectory.py`、`sandbox.py`、`parsing.py`。

#### 4.2.4 代码实践

实践目标：为五大子包各写一行职责注释，形成你自己的导航笔记。

操作步骤：

1. 在仓库根目录执行 `find slime -name '*.py' -printf '%h\n' | cut -d/ -f1-2 | sort | uniq -c`，统计每个子包的文件数。
2. 依次 `ls slime/ray`、`ls slime/backends/megatron_utils`、`ls slime/backends/sglang_utils`、`ls slime/rollout`、`ls slime/utils`、`ls slime/agent`，浏览每个子包的文件名。
3. 把本讲 4.2.1 的职责表抄进你的笔记，并在每个子包旁边补一句「我看到的关键文件名」。

需要观察的现象：`backends/` 文件最多（训练+推理两套），`utils/` 次之，`ray/` 文件最少但名字（`placement_group`、`actor_group`）最贴近「编排」。

预期结果：你得到一张带文件计数的子包职责表，并能凭文件名猜出每个子包的主线。

#### 4.2.5 小练习与答案

**练习 1**：如果你要修改「奖励计算」的逻辑，应该去哪个子包？要去更具体的哪个目录？
**答案**：去 `slime/rollout/rm_hub/`（reward model hub），它按 `rm_type` 分发到 `deepscaler.py`、`math_utils.py`、`f1.py`、`gpqa.py` 等内置奖励函数。

**练习 2**：`slime/utils/` 里有 `arguments.py`，`slime/backends/megatron_utils/` 和 `slime/backends/sglang_utils/` 下也各有一个 `arguments.py`，它们是什么关系？
**答案**：`utils/arguments.py` 是总中枢（`parse_args`），负责合并三族参数；两个 backend 下的 `arguments.py` 分别定义各自后端的专属参数（Megatron 训练参数、SGLang 推理参数），最终被中枢调用并合并。这会在 U8-L3 详细讲解。

**练习 3**：为什么 `slime/__init__.py` 是空的，这对读源码有什么影响？
**答案**：因为 slime 不在包级别持有全局配置或副作用，组件全靠入口脚本显式 import 装配。影响是：读源码时你要从 `train.py` 这类入口顺着 import 往里看，而不是期待 `import slime` 会自动初始化什么东西。

### 4.3 入口文件与参数中枢

#### 4.3.1 概念说明

slime 是「既是库又是程序」的项目：它的程序入口不在 `slime/` 包内部，而是放在仓库**根目录**的 `train.py` 与 `train_async.py`。这两个文件都只有约 100 行，非常薄，它们的工作只是「解析参数 → 调用 `slime/ray/` 把工人建起来 → 跑训练循环」。

参数中枢则是 `slime/utils/arguments.py` 里的 `parse_args`。slime 需要同时接受三类参数：Megatron 的训练参数、SGLang 的推理参数、slime 自己的参数，全部混在一条命令行里，由 `parse_args` 统一拆分与校验。

#### 4.3.2 核心流程

入口的装配流程很线性：

```text
命令行参数
   │
   ▼
train.py 的 __main__ → parse_args()  （slime/utils/arguments.py：合并三族参数）
   │
   ▼
train(args)
   │
   ├─ create_placement_groups(args)   ← slime/ray/placement_group.py：分配 GPU
   ├─ create_rollout_manager(args)    ← slime/ray/placement_group.py：建推理工人
   ├─ create_training_models(args)    ← slime/ray/placement_group.py：建训练工人
   └─ for rollout_id in range(...):   ← 训练主循环（采样→训练→保存→同步权重→评估）
```

两个入口的区别：`train.py` 是同步循环（一轮 rollout 完成后才训练）；`train_async.py` 是异步循环（在训练的同时提前发起下一轮 rollout，要求训练与推理**不共卡**）。异步入口在文件开头就断言了这一约束。

#### 4.3.3 源码精读

`train.py` 的 import 直接揭示了它依赖哪些子包：

[train.py:1-6](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/train.py#L1-L6) —— `import ray`；从 `slime.ray.placement_group` 引入 `create_placement_groups / create_rollout_manager / create_training_models`；从 `slime.utils.arguments` 引入 `parse_args`；从 `slime.utils.logging_utils` 引入日志/追踪；从 `slime.utils.misc` 引入 `should_run_periodic_action`。可以看到入口只碰 `ray/` 和 `utils/` 两个子包，`backends/` 与 `rollout/` 是被 `ray/` 间接驱动的。

入口的真正起点在文件末尾：

[train.py:97-99](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/train.py#L97-L99) —— `if __name__ == "__main__": args = parse_args(); train(args)`。这是整个程序的执行起点：先解析参数，再进入 `train(args)` 主循环。

异步入口的关键差异在一开头的断言：

[train_async.py:9-11](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/train_async.py#L9-L11) —— 注释说明框架支持 fully async 等异步方案（见 `examples/fully_async`），并 `assert not args.colocate`，即异步训练不允许训练与推理共卡（colocate）。这是因为异步需要训练和下一轮采样同时占用 GPU，共卡会冲突。

参数中枢虽不在这两个入口文件里，但它是 `train.py` 第一个调用的函数所在：

[slime/utils/arguments.py](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/utils/arguments.py#L1) —— `parse_args` 在这里定义，负责把 Megatron / SGLang / slime 三族参数合并、校验并返回一个统一的 `args` 命名空间。后续每一讲涉及「某个 `--xxx` 参数」时，源头都指向这里。

#### 4.3.4 代码实践

实践目标：从一个 import 语句出发，定位到它所在的子包和文件。

操作步骤：

1. 打开 `train.py` 第 3 行：`from slime.ray.placement_group import create_placement_groups, create_rollout_manager, create_training_models`。
2. 在仓库根目录执行 `grep -n "def create_placement_groups" slime/ray/placement_group.py`，确认这个函数确实定义在 `slime/ray/` 子包里。
3. 对 `slime.utils.arguments.parse_args` 做同样的事：`grep -n "def parse_args" slime/utils/arguments.py`。
4. 在笔记里画一条「`train.py` 第 X 行 import → 落在 `slime/<子包>/<文件>.py` 第 Y 行」的对照表。

需要观察的现象：入口文件的每一个名字都能在 `slime/` 的某个子包里找到定义点，没有任何「凭空出现」的函数。

预期结果：你得到一张入口 import → 子包定义点的映射表，证明「入口很薄、逻辑都在 `slime/` 内部」。若 `grep` 不可用，可在编辑器里对函数名做「转到定义」。

#### 4.3.5 小练习与答案

**练习 1**：`train.py` 为什么只 import `slime.ray.*` 和 `slime.utils.*`，却从不直接 import `slime.backends.*`？
**答案**：因为 `ray/` 是编排层，它把 `backends/` 的训练/推理工人封装在 `RayTrainGroup` / `RolloutManager` 等统一接口后面。入口只和编排层对话，不直接碰后端，这样换后端时入口代码不用改。

**练习 2**：`train_async.py` 第 11 行的 `assert not args.colocate` 如果在 colocate 模式下被触发会怎样？
**答案**：程序会在启动训练前立即抛出 `AssertionError` 并退出，因为异步训练要求训练与下一轮采样同时占用 GPU，而 colocate（共卡）模式下二者抢同一批 GPU，必然冲突，所以一开始就拒绝。

### 4.4 插件、脚本与工具目录

#### 4.4.1 概念说明

核心包 `slime/` 之外，有几个目录虽然不被 import，但同样重要：

- **`slime_plugins/`**：与核心包并列的第一方插件目录，目前含 `models/`（自定义模型结构，例如 `models/glm5/`）和 `rollout_buffer/`（数据缓冲区相关的生成器，例如 `rollout_buffer/generator/`）。它和 `slime/` 一样会被打包，是「可被框架发现并加载」的扩展点。
- **`scripts/`**：各种模型的启动脚本 `run-<model>.sh`，以及 `models/`（每个模型一份 Megatron 参数配置 `<model>.sh`）和 `low_precision/`（低精度相关脚本）。
- **`tools/`**：命令行工具，主要是权重格式转换（`convert_hf_to_torch_dist.py`、`convert_torch_dist_to_hf.py`、`convert_hf_to_fp8.py` 等）和性能分析（`analyze_profile.py`、`profile_rollout.py`、`trace_timeline_viewer.py`）。
- **`examples/`**：可运行示例，如 `search-r1/`、`multi_agent/`、`coding_agent_rl/`、`fully_async/`、`delta_weight_sync/` 等，是学习自定义接口的最佳范例。
- **`tests/`**：测试，含 `ci/`、`plugin_contracts/`（插件契约测试）、`test_agent/`、`utils/`。
- **`docs/`**：文档，分 `en/` 与 `zh/`，各有 `get_started/`、`advanced/`、`developer_guide/`、`blogs/`、`examples/`、`platform_support/`。

#### 4.4.2 核心流程

这些目录的协作体现在「从拿到模型到跑通训练」的完整路径：

```text
HF 检查点
   │
   ▼  tools/convert_hf_to_torch_dist.py（权重转换）
Megatron torch_dist 检查点
   │
   ▼  scripts/models/<model>.sh（提供 Megatron 模型参数）+ scripts/run-<model>.sh（启动）
train.py
   │
   ▼  （可选）slime_plugins/models/<model>/（自定义模型结构插件）/ examples/<场景>/（自定义接口范例）
训练循环
   │
   ▼  tests/plugin_contracts/（验证你的自定义实现符合契约）
通过
```

#### 4.4.3 源码精读

插件目录的结构（注意它有两层 `slime_plugins`，外层是仓库目录、内层才是包）：

[slime_plugins/models/](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime_plugins/models/__init__.py#L1) —— `slime_plugins/models/` 下按模型族组织，例如 `models/glm5/`，用于注册自定义模型结构（与 `slime/backends/megatron_utils/model_provider.py` 配合）。`slime_plugins/rollout_buffer/generator/` 则提供数据缓冲区相关的生成器插件。

启动脚本的规模（仅列举部分）：

[scripts/](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/scripts/run-qwen3-4B.sh#L1) —— 仓库提供了从 `run-qwen2.5-0.5B` 到 `run-glm5.2-744B-A40B` 的数十个启动脚本，每个脚本通常配套 `scripts/models/<model>.sh` 提供该模型的 Megatron 参数。

工具目录覆盖「转换 + 分析」两类：

[tools/](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/tools/convert_hf_to_torch_dist.py#L1) —— `convert_hf_to_torch_dist.py` / `convert_torch_dist_to_hf.py` 负责 HF 与 Megatron torch_dist 双向转换；`convert_hf_to_fp8.py` / `convert_hf_to_int4.py` 负责低精度；`analyze_profile.py` / `profile_rollout.py` / `trace_timeline_viewer.py` 负责性能剖析。

#### 4.4.4 代码实践

实践目标：在不动核心代码的前提下，定位「自定义某个能力」时该参考哪个 `examples/` 子目录、用哪个 `scripts/` 启动。

操作步骤：

1. 执行 `ls examples`，浏览所有示例目录名。
2. 针对以下三个需求，分别写出你会去参考的 `examples/` 子目录：
   - 「我想做多轮检索增强（搜索）的 RL」
   - 「我想让多个 agent 协同完成一条 rollout」
   - 「我想用异步方式跑 rollout」
3. 执行 `ls scripts/models`，找一个与你熟悉模型对应的配置文件（如 `qwen3-4B.sh`），用 `head -30 scripts/models/qwen3-4B.sh` 查看它定义了哪些变量。

需要观察的现象：`examples/` 的子目录名直接对应一类自定义场景；`scripts/models/*.sh` 通常只定义 `MODEL_ARGS` 一类变量，被 `run-*.sh` source 进来。

预期结果：你能为上述三个需求分别匹配到 `examples/search-r1`、`examples/multi_agent`、`examples/fully_async`，并理解 `scripts/models/*.sh` 与 `scripts/run-*.sh` 是「模型参数」与「训练参数」的分离。

#### 4.4.5 小练习与答案

**练习 1**：`slime_plugins/` 和 `examples/` 都含有用户写的扩展代码，它们有什么本质区别？
**答案**：`slime_plugins/` 是被 `setup.py` 打包的第一方插件目录（`include=["slime*", "slime_plugins*"]`），框架会按约定自动发现它（如 `slime_plugins/models/`）；`examples/` 不被打包，是供你阅读和拷贝改写的范例，需要你把自己的实现路径通过 `--xxx-path` 参数显式传给框架。

**练习 2**：为什么要把「模型参数」（`scripts/models/*.sh`）和「训练参数」（`scripts/run-*.sh`）分成两个文件？
**答案**：因为同一个模型可以被多种训练配方复用（不同 batch size、不同 RL 算法），把模型结构相关的参数（层数、头数、并行度等）单独抽到 `models/*.sh`，由 `run-*.sh` 通过 `source` 引入，能避免在多个启动脚本里重复维护模型参数。

## 5. 综合实践

把本讲的知识串起来，完成一张**完整的目录导航地图**。

任务：在笔记中产出一份带标注的树状图，要求：

1. 列出全部顶层目录（`slime/`、`slime_plugins/`、`train.py`、`train_async.py`、`tools/`、`scripts/`、`examples/`、`tests/`、`docs/`、`docker/`、`imgs/`），每个标注「核心包 / 入口 / 脚本 / 工具 / 示例 / 测试 / 文档 / 资源」之一。
2. 展开 `slime/` 的五大子包，每个子包标注四个标签之一：**训练(train) / 编排(orchestration) / 推理+数据生成(rollout) / 工具(utils) / 智能体(agent)**，并各列出 1–2 个你认为最关键的文件名。
3. 用箭头画出「一次训练的装配链路」：`scripts/run-*.sh → train.py → parse_args(slime/utils/) → create_*(slime/ray/) → backends/ + rollout/`。

验证方法：随机挑一个文件路径（例如 `slime/backends/megatron_utils/loss.py`），对照你的地图，说出它属于哪个子包、大致负责什么、在装配链路的哪一环被用到。如果说不清楚，就回到对应章节复习。

如果无法在本机运行命令，可仅依据本讲给出的目录树和文件列表完成标注，并在不确定处标注「待本地验证」。

## 6. 本讲小结

- slime 仓库由核心包 `slime/` + 插件包 `slime_plugins/`（两者被 `setup.py` 打包）加上入口脚本与外围资源（`scripts/`、`tools/`、`examples/`、`tests/`、`docs/`）组成。
- `slime/` 用五个子包切分关注点：`ray/`(编排)、`backends/`(训练+推理两个后端，文件最多)、`rollout/`(数据生成)、`utils/`(共享工具与参数中枢)、`agent/`(智能体运行时)。
- 程序入口是仓库根目录的 `train.py`（同步）与 `train_async.py`（异步），它们都很薄，只做「解析参数 → 调用 `slime/ray/` 装配工人 → 跑循环」。
- `slime/utils/arguments.py` 的 `parse_args` 是参数中枢，合并 Megatron / SGLang / slime 三族参数；两个 `backends/*/arguments.py` 分别定义各自后端的专属参数。
- `slime/__init__.py` 是空的，说明框架不在包级别持有全局状态，一切由入口脚本显式 import 装配——所以读源码要从入口顺着 import 往里看。
- `scripts/`（启动脚本 + 模型参数）、`tools/`（权重转换与剖析）、`examples/`（自定义接口范例）是运行与二次开发的关键资源。

## 7. 下一步学习建议

有了这张地图，下一步建议：

1. 先读 **[u1-l3 环境搭建与安装](u1-l3-environment-setup.md)**，把 slime 真正装起来、能 `import slime`，并理解 `requirements.txt` / `build_conda.sh` / Docker 镜像的作用。
2. 装好后直接进入 **[u1-l4 运行第一个训练](u1-l4-first-training-run.md)**，对照 `scripts/run-qwen3-4B.sh` 看一次启动链路如何串起本讲提到的目录。
3. 想从源码角度理解「入口如何装配出训练循环」，进入 **[u1-l6 训练主循环 train.py 全景](u1-l6-train-loop-overview.md)**，那时你会再次用到本讲的入口→子包映射。
4. 对编排层感兴趣可提前浏览 `slime/ray/placement_group.py`，对数据流感兴趣可先扫一眼 `slime/rollout/sglang_rollout.py` 的函数名（不要求看懂细节）。
