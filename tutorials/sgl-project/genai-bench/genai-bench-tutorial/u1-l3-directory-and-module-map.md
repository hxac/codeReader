# 目录结构与模块全景

## 1. 本讲目标

在 u1-l1 里我们建立了「genai-bench 是什么」的整体认知，在 u1-l2 里我们跑通了第一个基准、看到了实验产出物。现在要回答第三个问题：**这些代码到底放在哪里、各部分各管什么？**

读完本讲，你应该能够：

- 画出 `genai_bench` 包的目录结构，说出每个顶层目录与子包的作用。
- 说明 `cli / user / sampling / scenarios / data / metrics / auth / storage / analysis / ui / distributed` 这 11 个子包各自的职责。
- 区分「被 `cli.py` 直接 import 的子系统」和「被间接调用的子系统」，并能据此绘制一张从 CLI 到各功能模块的依赖草图。
- 定位每个子系统的入口文件，为后续逐模块精读（u2 之后）打好地图。

> 本讲只做「地图」级别介绍，不深入任何子系统的实现细节。每个子系统的内部机制会在后续单元单独成篇。

## 2. 前置知识

本讲假设你已经读过 u1-l1、u1-l2，因此下面这些概念不再重复解释，只点出与本讲相关的那一面：

- **Locust 驱动的并发**：genai-bench 基于 Locust 构造虚拟用户发请求。本讲你会看到 `user/` 子包和 `distributed/` 子包是怎么和 Locust 衔接的。
- **Pydantic 数据契约**：u1-l1 提到项目用 Pydantic 统一数据模型。本讲你会看到这些模型集中在 `protocol.py`，被几乎所有子包共享。
- **gevent monkey patch**：u1-l1 强调 `__init__.py` 必须最先执行 `monkey.patch_all()`。本讲你会看到它就在包入口的位置，是整个包加载的第一行。

补一个本讲要用的术语：

- **子包（subpackage）**：Python 里一个目录只要含有 `__init__.py` 就是一个包，包里面还可以嵌套包，嵌套的叫子包。`genai_bench/auth/oci/` 这种就是 `auth` 包下的 `oci` 子包。

## 3. 本讲源码地图

本讲主要「读地图」，涉及的文件不多，但都是定位整个项目的关键坐标：

| 文件 | 作用 |
|------|------|
| [README.md](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/README.md) | 项目自述，给出能力概览与快速开始。 |
| [docs/index.md](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/docs/index.md) | 文档站点首页，给出文档分区（Getting Started / User Guide / Developer Guide），是理解项目知识结构的第二入口。 |
| [genai_bench/\_\_init\_\_.py](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/__init__.py) | 包入口，第一行执行 gevent monkey patch。 |
| [genai_bench/cli/cli.py](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/cli.py) | CLI 入口与 `benchmark` 主编排函数，是整张依赖草图的「中心」。 |
| [genai_bench/cli/validation.py](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/validation.py) | 后端/任务校验，持有「后端名 → User 类」的映射表，是 CLI 间接触达 `user/` 子包的桥梁。 |
| [genai_bench/storage/factory.py](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/storage/factory.py) | 存储工厂，示范了项目里「按需惰性导入云 SDK」的通用模式。 |

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：**4.1 顶层目录结构**、**4.2 子包职责地图**、**4.3 入口与调用链概览**。

### 4.1 顶层目录结构

#### 4.1.1 概念说明

一个成熟的开源项目通常由几类内容组成：源码、测试、文档、示例、构建与 CI 配置。genai-bench 也不例外。我们先站在仓库**根目录**的高度俯瞰，弄清「哪个目录装哪一类东西」，再下沉到 `genai_bench/` 这个核心包内部。

这样做的好处是：以后你看到任何一个文件路径（比如 `tests/cli/test_validation.py`），都能立刻根据它所在的顶层目录判断——这是测试代码、文档、还是核心源码。

#### 4.1.2 核心流程

仓库根目录的顶层组织可以这样分层记忆：

```
sgl-project-genai-bench/          # 仓库根
├── genai_bench/                  # 【核心源码包】所有运行逻辑都在这里
├── tests/                        # 【测试】pytest，目录结构与 genai_bench 镜像
├── docs/                         # 【文档】mkdocs 站点源文件
├── examples/                     # 【示例】可独立运行的小脚本与配置样例
├── .github/workflows/            # 【CI】GitHub Actions 流水线
├── Dockerfile                    # 【构建】容器镜像定义
├── Makefile                      # 【构建】常用命令快捷方式（如 make dev/test）
├── pyproject.toml                # 【构建】包元数据、依赖、入口点、工具配置
├── mypy.ini / .pre-commit-config.yaml  # 【质量】类型检查与提交前检查
├── README.md / LICENSE           # 【说明】
```

一个值得注意的对应关系：`tests/` 的子目录几乎和 `genai_bench/` 的子包一一镜像——`tests/auth/` 测 `genai_bench/auth/`，`tests/cli/` 测 `genai_bench/cli/`。这种镜像约定让你找测试和找源码一样容易。

#### 4.1.3 源码精读

文档侧的「知识结构」可以从文档首页一眼看清。`docs/index.md` 把文档分成三块：

[docs/index.md:L59-L83](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/docs/index.md#L59-L83) — 这段把文档分为 🚀 Getting Started（安装/任务/CLI/指标/迁移）、📖 User Guide（运行/场景/多云/Docker/Excel/绘图/上传）、🔧 Developer Guide（贡献/扩展）。这三块恰好对应「会用 → 进阶用 → 二次开发」三个层次，与本学习手册的入门/进阶/专家三层划分思路一致。

包入口的位置与内容也值得一看。`genai_bench/__init__.py` 整个文件只做一件事：

[genai_bench/\_\_init\_\_.py:L1-L7](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/__init__.py#L1-L7) — 在任何其他 import 之前调用 `monkey.patch_all()`。注释解释了原因：Locust 依赖 gevent 的协作式并发，如果不打补丁，阻塞式 I/O（如 HTTP 请求）会卡住整个 worker 进程，导致心跳超时。这就是为什么它必须放在包入口的最前面——保证无论谁 import `genai_bench`，patch 都先发生。

#### 4.1.4 代码实践

**实践目标**：用 `git ls-files` 自行核对本讲给出的目录树，建立「眼见为实」的印象。

**操作步骤**：

1. 在仓库根目录执行（只读命令，安全）：

   ```bash
   git ls-files | sed 's#/.*##' | sort -u
   ```

   这会列出所有被 git 跟踪文件的**顶层目录**去重结果。

2. 再执行下面这条，看 `genai_bench/` 下一级有哪些子包：

   ```bash
   git ls-files 'genai_bench/*/__init__.py' | sed 's#__init__.py##'
   ```

**需要观察的现象**：第一条命令应该出现 `genai_bench`、`tests`、`docs`、`examples`、`.github` 等顶层条目；第二条应该列出 11 个子包。

**预期结果**：与 4.1.2 给出的目录树一致。若某项对不上，说明你的本地工作区与讲义所基于的 HEAD（`7fd04d8`）有差异。

> 本实践为只读命令，「待本地验证」具体输出，但目录集合是确定的。

#### 4.1.5 小练习与答案

**练习 1**：项目为什么没有 `setup.py`，构建配置放在哪里？

**参考答案**：项目采用现代 Python 打包方式，构建配置集中在根目录的 `pyproject.toml`（u1-l1 已介绍它用 hatchling 作为构建后端），因此不需要传统的 `setup.py`。

**练习 2**：`tests/` 目录的子目录命名和 `genai_bench/` 有什么规律？这样设计有什么好处？

**参考答案**：两者子目录基本镜像对应（`tests/auth/` ↔ `genai_bench/auth/` 等）。好处是：看到一条测试路径就能立刻定位它测的是哪个源码子包，降低维护和导航成本。

---

### 4.2 子包职责地图

#### 4.2.1 概念说明

`genai_bench/` 下面有 11 个子包，外加几个顶层模块文件。每个子包解决基准测试流水线里的一个独立环节。理解它们的关系，就理解了 genai-bench 的整体架构。

一条朴素的心智模型是「数据流」：**输入（数据/场景）→ 采样成请求 → 由 User 后端发出 → 收集指标 → 聚合 → 分析报告**，外加上「认证/存储」两条横切能力，以及「CLI 编排 / 分布式运行 / 实时 UI / 日志」四层基础设施。

#### 4.2.2 核心流程

下表把 11 个子包按「数据流 + 基础设施」两类列出，并给出每个子包的职责与代表文件：

| 子包 | 类别 | 职责（一句话） | 代表文件 |
|------|------|----------------|----------|
| `cli/` | 基础设施 | click 命令行入口、选项分组、校验、编排 | `cli.py`、`option_groups.py`、`validation.py`、`report.py`、`utils.py` |
| `protocol.py`（顶层模块） | 数据契约 | 全局共享的 Pydantic 请求/响应/元数据模型 | `protocol.py` |
| `data/` | 输入 | 数据集配置与加载（本地/HF/自定义） | `config.py`、`loaders/factory.py` |
| `scenarios/` | 输入 | 流量场景字符串解析（如 `D(100,100)`） | `base.py`、`text.py`、`multimodal.py` |
| `sampling/` | 输入→请求 | 把场景+数据组合成具体的 `UserRequest` | `base.py`、`text.py`、`image.py` |
| `user/` | 执行 | 各模型后端的 Locust 虚拟用户，发请求+解析响应 | `base_user.py`、`openai_user.py`、`oci_*`、`aws_bedrock_user.py` 等 |
| `metrics/` | 指标 | 单请求指标计算 + 运行级聚合 | `metrics.py`、`request_metrics_collector.py`、`aggregated_metrics_collector.py` |
| `auth/` | 横切 | 模型认证与存储认证的 provider 体系 | `unified_factory.py`、`model_auth_provider.py`，及 `aws/azure/gcp/oci/openai/...` 子包 |
| `storage/` | 横切 | 多云对象存储的统一上传/下载 | `factory.py`、`base.py`、`oci_storage.py`、`aws_storage.py` 等 |
| `analysis/` | 产出 | 加载实验结果、生成 Excel 报告与绘图 | `experiment_loader.py`、`excel_report.py`、`plot_config.py`、`plot_report.py`、`flexible_plot_report.py` |
| `ui/` | 基础设施 | 基于 rich 的实时仪表盘 | `dashboard.py`、`layout.py`、`plots.py` |
| `distributed/` | 基础设施 | Locust master/worker 分布式运行编排 | `runner.py` |

还有几个**顶层模块文件**不在任何子包里，但被多处复用：

- `version.py`：版本号单一数据源（u1-l1 讲过）。
- `utils.py`：通用小工具，如 `sanitize_string`（把场景串清洗成文件名，u1-l2 见过 `D(100,100) → D100_100`）、`is_single_experiment_folder`。
- `time_units.py`：延迟指标的时间单位（s↔ms）转换，贯穿保存/UI/报告。
- `logging.py`：rich 日志 handler 与延迟 flush（u1-l1 提到的「富日志」实现）。

> 一句话总结架构：`cli/` 是总指挥，沿着 `data → scenarios → sampling → user → metrics → analysis` 这条数据流推进，`auth/` 和 `storage/` 提供横切的云能力，`distributed/`、`ui/`、`logging/` 提供运行期基础设施。

#### 4.2.3 源码精读

一个体现「横切能力按云厂商组织」的细节，是 `auth/` 与 `storage/` 子包内部又按厂商分子包。以存储工厂为例，它示范了项目里反复出现的「惰性导入」模式：

[genai_bench/storage/factory.py:L36-L44](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/storage/factory.py#L36-L44) — 只有当用户真的选了 `oci` 或 `aws` 时，才在函数内部 `import` 对应的存储类。注释写明原因：「Lazy import to avoid requiring OCI SDK if not used」（惰性导入，避免在不用时也强依赖 OCI SDK）。这正是 u1-l2 讲过的「云能力靠可选依赖按需开启」在源码层面的落实——不装 `oci` 包，就不会触发对它的 import。

而 `user/` 子包虽然存在，却不被 `cli.py` 直接 import，而是通过校验逻辑里的映射表被选中：

[genai_bench/cli/validation.py:L25-L38](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/validation.py#L25-L38) — `API_BACKEND_USER_MAP` 把每个后端名（如 `openai`、`aws-bedrock`，以及复用 OpenAIUser 的 `vllm`、`sglang`）映射到对应的 User 类。注意 `vllm` 和 `sglang` 直接指向 `OpenAIUser`，因为它们都暴露 OpenAI 兼容 API——这是「复用」而非「重复实现」。

#### 4.2.4 代码实践

**实践目标**：用 4.2.2 的表格做自查，确认你能在源码里找到每个子包的入口文件。

**操作步骤**：

1. 用 `git ls-files 'genai_bench/*/*.py'` 列出每个子包下的 Python 文件。
2. 对照表格的「代表文件」列，逐一确认它们确实存在。
3. 特别确认 `distributed/` 子包下只有 `runner.py` 一个核心文件（它是最「重」的单文件模块之一）。

**需要观察的现象**：每个子包都有一个 `__init__.py`（标志它是一个包），且代表文件都能找到。

**预期结果**：11 个子包、各自的代表文件全部命中。若想确认子包数量，可数 `genai_bench/*/__init__.py` 的个数，应为 11。

#### 4.2.5 小练习与答案

**练习 1**：`protocol.py` 放在顶层而不是放进某个子包，为什么？

**参考答案**：因为它是被几乎所有子包共享的「数据契约」（请求/响应/实验元数据模型）。放顶层意味着任何子包都能平等地 `from genai_bench.protocol import ...`，避免循环依赖和「某个子包拥有全局模型」的不合理归属。

**练习 2**：`storage/factory.py` 为什么把各云厂商的 import 写在函数体内而不是文件顶部？

**参考答案**：为了「惰性导入」——只在用户实际选用某家云时才加载其 SDK，否则不强依赖、也不付出 import 开销。这与项目用可选依赖（`[aws]`/`[azure]`/`[gcp]`/`[multi-cloud]`）按需安装云能力的策略一致。

---

### 4.3 入口与调用链概览

#### 4.3.1 概念说明

有了目录地图，最后一个问题是：**程序从哪里开始、又怎么把各子包串起来？** 这就要看 `genai_bench/cli/cli.py`。

入口由两部分组成：

1. **命令注册**：用 click 定义一个 `cli` group，再把 `benchmark`、`excel`、`plot` 三个子命令挂上去（u1-l4 会细讲）。
2. **`benchmark` 主编排**：一个大函数，按阶段依次调用各子系统，完成「认证 → 数据 → 采样 → 运行 → 报告 → 上传」的全流程（u8-l1 会作为 capstone 详讲）。

本讲只关注其中的「依赖关系」：通过读 `cli.py` 顶部的 import，就能看出 `cli` 直接依赖哪些子系统。而**未被直接 import 的子系统**（`user`、`scenarios`、`metrics`）是被**间接**调用的——这是初学者最容易忽略的细节。

#### 4.3.2 核心流程

先看命令注册骨架（伪代码）：

```text
@click.group()
def cli(): ...                      # 顶层命令组

@click.command()
@api_options @sampling_options ...  # 一长串选项分组装饰器
def benchmark(...): ...             # 主编排函数

@click.command()
def excel(...): ...                 # 复用 analysis 子系统，导出 Excel
@click.command()
def plot(...): ...                  # 复用 analysis 子系统，绘图

cli.add_command(benchmark)
cli.add_command(excel)
cli.add_command(plot)
```

再看 `benchmark` 的主流程阶段，以及每个阶段调用哪个子系统：

```text
[ui]        create_dashboard()
[logging]   LoggingManager / init_logger
[auth]      UnifiedAuthFactory.create_model_auth()
[cli]       validate_tokenizer / validate_prefix_options
[data]      DatasetConfig + DataLoaderFactory.load_data_for_task()
[sampling]  Sampler.create(task=...)        # 间接 → scenarios
[protocol]  ExperimentMetadata 写盘
[locust]    Environment(user_classes=[user_class])   # user_class 来自 ctx.obj → user/
[distributed] DistributedRunner.setup()     # 间接产出 → metrics collector
─── 双层循环：scenario × iteration ───
[metrics]   aggregate_metrics_data() / save()
[analysis]  plot_single_scenario... (过程图)
─── 收尾 ───
[analysis]  load_one_experiment + create_workbook (Excel)
[analysis]  plot_experiment_data_flexible (报告图)
[auth]+[storage] create_storage_auth + StorageFactory.create_storage + upload_folder
```

由此可以总结出一张「直接依赖 vs 间接依赖」的对照表：

| 子系统 | 是否被 `cli.py` 直接 import | 触达方式 |
|--------|----------------------------|----------|
| analysis / auth / data / distributed / logging / sampling / storage / ui | ✅ 直接 | `cli.py` 顶部 import |
| protocol（`ExperimentMetadata`）、utils、version | ✅ 直接 | `cli.py` 顶部 import |
| **user/** | ❌ 间接 | `validation.py` 的 `API_BACKEND_USER_MAP` 把 User 类塞进 `ctx.obj["user_class"]`，`cli.py` 再取出 |
| **scenarios/** | ❌ 间接 | 由 `Sampler.create(task)` 在 sampling 子系统内部使用 |
| **metrics/** | ❌ 间接 | 由 `DistributedRunner` 在 setup 时产出 `metrics_collector`，`cli.py` 通过 `runner.metrics_collector` 取用 |

记住这张表，你就能解释一个常见困惑：「为什么 `cli.py` 顶部看不见 `user`、`scenarios`、`metrics` 这三个子包的 import，但它们明明参与了运行？」答案就是：它们是被间接调用的。

#### 4.3.3 源码精读

`cli.py` 顶部的项目内 import 集中体现了「直接依赖」集合：

[genai_bench/cli/cli.py:L12-L44](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/cli.py#L12-L44) — 这里能直接看到 `analysis.*`、`auth.unified_factory`、`data.*`、`distributed.runner`、`logging`、`protocol`、`sampling.base`、`storage.factory`、`ui.dashboard`、`utils`、`version` 等模块被引入；唯独没有 `user`、`scenarios`、`metrics`。这正是上表「直接 vs 间接」的源码依据。

`user/` 是怎么被间接选中的？看校验回调：

[genai_bench/cli/validation.py:L257-L270](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/validation.py#L257-L270) — `validate_api_backend` 在解析 `--api-backend` 时，从映射表取出对应的 User 类，并写入 `ctx.obj["user_class"]`。随后 `cli.py` 里的 `user_class = ctx.obj.get("user_class")` 把它取出来用。这是一条「校验回调把子系统类注入 click 上下文」的隐式通道。

三个命令最终通过 `add_command` 注册进 `cli` group：

[genai_bench/cli/cli.py:L668-L670](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/cli.py#L668-L670) — `benchmark` 是本模块自己定义的；`excel` 和 `plot` 来自 `genai_bench/cli/report.py`，二者都复用 `analysis` 子系统。以 `excel` 为例：

[genai_bench/cli/report.py:L47-L57](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/report.py#L47-L57) — `excel` 命令接收实验目录，调用 `load_one_experiment`（analysis）读取结果，再用 `create_workbook`（analysis）写出 Excel。可以看到它和 `benchmark` 收尾阶段的逻辑是同一套 analysis 能力的复用。

#### 4.3.4 代码实践

**实践目标**：把 4.3.2 的「直接 vs 间接」对照表，亲手从源码验证一遍。这是本讲的配套小实践（综合实践在第 5 节还会画一张更完整的图）。

**操作步骤**：

1. 打开 `genai_bench/cli/cli.py` 第 12–44 行。
2. 逐条 import，在一张纸上记下「import 来源 → 对应子系统」。例如：
   - `from genai_bench.analysis.excel_report import create_workbook` → `analysis`
   - `from genai_bench.sampling.base import Sampler` → `sampling`
3. 数一数：直接 import 涉及了哪几个子系统？是否确实**没有** `user`、`scenarios`、`metrics`？
4. 对这三者，分别在 `validation.py`（user）、`sampling/`（scenarios）、`distributed/runner.py`（metrics）里找到它们被间接引用的证据。

**需要观察的现象**：`cli.py` 顶部 import 覆盖 8 个子系统 + 3 个顶层模块；缺的三个正是通过间接通道接入的。

**预期结果**：与 4.3.2 的对照表完全一致。

> 本实践为纯源码阅读型，无需运行，结果可直接在源码中确认。

#### 4.3.5 小练习与答案

**练习 1**：`excel` 和 `plot` 两个命令为什么没有像 `benchmark` 那样放在 `cli.py` 里，而是放在 `report.py`？

**参考答案**：出于职责分离与复用。`benchmark` 是重量级的主编排，`excel`/`plot` 是轻量的「事后报告」命令，二者都只是对 `analysis` 子系统的薄封装。把它们独立到 `report.py`，让 `cli.py` 聚焦于编排，结构更清晰。

**练习 2**：假如你要新增一个后端 `my-backend`，仅从本讲的地图看，至少需要改动哪个子包、又需要在哪个「映射表」里登记？

**参考答案**：需要在 `user/` 子包里新增一个 User 子类（或在合适基类上实现），并在 `cli/validation.py` 的 `API_BACKEND_USER_MAP` 里登记后端名到该类的映射，这样 `--api-backend my-backend` 才能选中它。（完整扩展流程见 u8-l3。）

---

## 5. 综合实践

**任务**：绘制一张「从 `cli` 到各子系统」的完整依赖草图，把本讲三个模块的知识串起来。这是把「目录地图」和「调用链」合并成一张全景图的综合练习。

**操作步骤**：

1. **列出顶层目录**：用 4.1 的目录树，写出仓库根的 8–9 个顶层条目及其类别。
2. **画出 11 个子包**：按 4.2 的「数据流 + 基础设施」分类，把它们排成一条从输入到产出的流水线，外加上横切层与基础设施层。建议布局：

   ```text
   输入：   data → scenarios → sampling
   执行：                     → user
   指标：                              → metrics
   产出：                                        → analysis
   横切：   auth（模型/存储）   storage
   基础设施：cli（总指挥）   distributed   ui   logging
   契约：   protocol（全局共享）
   ```

3. **标注直接/间接依赖**：从 `cli` 出发画箭头到各子系统，**实线**表示 `cli.py` 直接 import（analysis/auth/data/distributed/logging/sampling/storage/ui/protocol/utils/version），**虚线**表示间接调用（经 `validation.py` → `user`；经 `sampling` → `scenarios`；经 `distributed` → `metrics`）。
4. **标注每个箭头上的「触发点」**：例如 `cli → auth` 标注「`UnifiedAuthFactory.create_model_auth`」，`cli → sampling` 标注「`Sampler.create(task)`」，`cli → analysis`（收尾）标注「`create_workbook` / `plot_experiment_data_flexible`」。
5. **自查**：草图里是否每个子包都至少被一条边连到？`protocol.py` 是否被画成「全局共享」而非只属于某一个子包？

**需要观察的现象**：画完后你会发现，`cli` 像一个轮毂（hub），向各子系统辐射；而 `user/scenarios/metrics` 三者不与 `cli` 直连，而是挂在 `validation/sampling/distributed` 这三条支线上。

**预期结果**：得到一张与 4.3.2 对照表一致、且能解释「为何三个子包不出现在 `cli.py` import 里」的全景图。这张图也是后续 u8-l1「benchmark 主流程 capstone」的草图底版。

> 本实践为源码阅读 + 手绘型，不涉及运行；产物是一张图（纸笔或绘图工具均可）。

## 6. 本讲小结

- 仓库根目录分为核心源码（`genai_bench/`）、测试（`tests/`，与源码镜像）、文档（`docs/`）、示例（`examples/`）、构建与 CI（`Dockerfile`/`Makefile`/`pyproject.toml`/`.github/workflows/`）几大类。
- `genai_bench/` 下有 11 个子包，沿「输入→执行→指标→产出」的数据流组织，外加认证/存储横切层与 CLI/分布式/UI/日志基础设施层；`protocol.py` 是全局共享的数据契约。
- 包入口 `__init__.py` 第一行就做 gevent monkey patch，这是 Locust 协作式并发能正常工作的前提。
- `storage/factory.py` 等处用「惰性导入」落实「云能力按需安装」的策略；`user/` 子包通过 `validation.py` 的 `API_BACKEND_USER_MAP` 被选中。
- `cli.py` 顶部 import 揭示了 `cli` 的**直接**依赖（analysis/auth/data/distributed/logging/sampling/storage/ui 等），而 `user`、`scenarios`、`metrics` 三个子包是**间接**被调用的。
- `benchmark/excel/plot` 三个命令挂在 `cli` group 下；`excel`、`plot` 位于 `report.py`，是 `analysis` 子系统的薄封装。

## 7. 下一步学习建议

本讲建立的是「地图」，接下来要逐层钻进具体子系统。建议学习顺序：

1. **u1-l4（CLI 入口与三大命令）**：紧接本讲的「入口」主题，深入 click group、`add_command` 注册机制，以及 `benchmark/excel/plot` 三命令的参数与职责。
2. **u1-l5（协议数据模型 protocol.py）**：先掌握全局共享的数据契约，它是后续所有子系统的通用语言。
3. 之后进入 u2（任务/场景/数据采样），从流水线的「输入端」开始逐包精读；最终在 u8-l1 用一篇 capstone 把本讲的草图升级为完整的主流程时序图。

> 阅读建议：在进入 u1-l4 之前，不妨先把本讲第 5 节画出的依赖草图留在手边，后续每读一个子包就在图上补一个「内部细节框」，地图会越读越细。
