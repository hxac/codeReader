# 安装与运行第一个基准

## 1. 本讲目标

上一讲（u1-l1）我们已经建立了对 genai-bench 的整体认知：它是一个面向 LLM 服务的 token 级基准测试工具。本讲把认知落到「手上能跑」的层面。学完本讲，你应当能够：

- 用 pip 完成 genai-bench 的安装，并知道何时需要额外安装 `[aws]`/`[azure]`/`[gcp]`/`[multi-cloud]` 等可选依赖。
- 读懂 README 里的「快速开始」命令，理解 `benchmark` 子命令每个关键参数的含义，尤其是默认行为可能带来的「惊喜」。
- 说清楚一次基准测试运行结束后，硬盘上的 `experiments` 目录里到底产出了哪些文件、它们各自代表什么。

本讲只关注「安装 + 跑通 + 看懂产物」这条最短路径，命令背后各子系统的内部机制（采样、指标、分布式等）留给后续讲义。

## 2. 前置知识

在动手之前，先用大白话澄清几个概念：

- **基准测试（benchmark）**：用一批人造的请求去「压」一个服务，测量它的速度和稳定性。你可以理解为「给服务器做体检」。
- **LLM 服务**：一个能接收文本请求、返回生成文本的 HTTP 服务，通常兼容 OpenAI 的 `/v1/chat/completions` 接口。vLLM、SGLang、真实的 OpenAI API 都属于这一类。
- **CLI（命令行接口）**：在终端里通过 `xxx --option value` 形式调用的程序。genai-bench 的 CLI 命令就叫 `genai-bench`。
- **子命令**：CLI 下的「子动作」。`genai-bench` 下面挂了三个子命令：`benchmark`（跑测试）、`excel`（生成表格）、`plot`（画图）。本讲的主角是 `benchmark`。
- **pip 与可选依赖**：Python 包管理器 pip 安装一个包时，可以用 `包名[extra]` 的语法额外安装一组可选依赖，例如 `pip install genai-bench[aws]` 会顺带装上 AWS 相关的库。

如果你还没读过 u1-l1，建议先读，了解项目的三大能力（CLI / 实时 UI / 实验分析器）和 `__init__.py` 里 `gevent.monkey.patch_all()` 的运行前提。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
| --- | --- |
| [README.md](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/README.md) | 项目门面，给出安装一句话和「快速开始」命令示例。 |
| [pyproject.toml](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/pyproject.toml) | Python 项目元数据：声明了入口命令、Python 版本要求、核心依赖与可选依赖分组。 |
| [docs/getting-started/installation.md](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/docs/getting-started/installation.md) | 官方安装指南，覆盖 PyPI / 开发 / Docker 三种安装方式与验证步骤。 |
| [genai_bench/cli/cli.py](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/cli.py) | CLI 入口，定义了 `cli` 组与 `benchmark` 子命令的全部编排逻辑。 |
| [genai_bench/cli/utils.py](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/utils.py) | 辅助函数：实验目录命名、单次运行的时间管理。 |
| [genai_bench/cli/validation.py](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/validation.py) | 参数校验与默认值：默认并发列表、默认场景列表都在这里。 |
| [Makefile](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/Makefile) | 开发者快捷命令：`make uv` / `make install` / `make dev` 等。 |

## 4. 核心概念与源码讲解

本讲按三个最小模块展开：**安装方式**、**快速开始命令解析**、**运行产出物**。

### 4.1 安装方式

#### 4.1.1 概念说明

genai-bench 是一个标准的 Python 包，发布在 PyPI 上。最简单的安装方式就是一句 `pip install genai-bench`。但项目还把一部分「按需才用」的依赖拆成了可选分组（optional dependencies），这样默认安装更轻量，只在真正需要某个云厂商时才把它装上。

理解这一点很关键：genai-bench 默认就能跑 OpenAI 兼容后端的基准；只有当你想压测 AWS Bedrock、Azure OpenAI、GCP Vertex，或想把结果上传到对应云存储时，才需要额外的云 SDK。这就是可选依赖存在的意义。

#### 4.1.2 核心流程

安装一条龙可以这样划分：

1. **确认 Python 版本**：项目要求 Python 3.10–3.12（见下方源码精读的版本约束）。
2. **选择安装方式**：
   - 普通用户：`pip install genai-bench`（PyPI）。
   - 需要某朵云：`pip install genai-bench[aws]`（或 `azure` / `gcp` / `multi-cloud`）。
   - 二次开发者：用 Makefile 走 `uv` 虚拟环境 + 可编辑安装。
   - 容器化场景：Docker 拉取或自行构建。
3. **验证安装**：运行 `genai-bench --version` 等命令确认 CLI 可用。

#### 4.1.3 源码精读

**入口命令是怎么注册的？** 在 `pyproject.toml` 的 `[project.scripts]` 里，把 `genai-bench` 这个命令名绑定到了 Python 函数 `genai_bench.cli.cli:cli`：

[pyproject.toml:L43-L44](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/pyproject.toml#L43-L44) —— pip 安装时会据此在 `bin/` 下生成名为 `genai-bench` 的可执行脚本，调用 `cli()` 函数。

**Python 版本约束**：

[pyproject.toml:L6-L6](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/pyproject.toml#L6) —— `requires-python = ">=3.10,<3.13"`，即支持 3.10、3.11、3.12。（官方安装文档里提到 3.13，但 pip 实际以这里的 `<3.13` 为准，建议用 3.10–3.12。）

**可选依赖分组**：

[pyproject.toml:L46-L83](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/pyproject.toml#L46-L83) —— 定义了 `dev`、`aws`、`azure`、`gcp`、`multi-cloud`、`docs` 六组。例如 `aws` 组包含 `boto3`/`botocore`，`multi-cloud` 则把 AWS+Azure+GCP 三家全打包。

**README 的一句话安装**：

[README.md:L43-L46](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/README.md#L43-L46) —— 给出 `pip install genai-bench` 并指向详细安装指南。

**官方安装指南的三种方式**：

[docs/getting-started/installation.md:L7-L58](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/docs/getting-started/installation.md#L7-L58) —— 分别讲 PyPI 安装、开发安装（`make uv` + `source .venv/bin/activate` + `make install`）、Docker 安装（`docker pull ...` 或 `docker build`）。

**Makefile 里的开发安装快捷命令**：

[Makefile:L40-L46](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/Makefile#L40-L46) —— `make install` 等价于 `uv pip install --editable .`（可编辑模式，改源码立即生效）；`make dev` 等价于 `uv pip install ".[dev,multi-cloud]"`（开发 + 全云依赖）。

[Makefile:L118-L120](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/Makefile#L118-L120) —— `make uv` 先装 `pipx` 再装 `uv`，并用 `python3.11` 建虚拟环境。

**安装后的验证命令**：

[docs/getting-started/installation.md:L60-L73](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/docs/getting-started/installation.md#L60-L73) —— 提供 `genai-bench --version` / `--help` / `benchmark --help` 三条验证命令。

#### 4.1.4 代码实践

**实践目标**：完成 genai-bench 的安装并验证 CLI 可用。

**操作步骤**：

1. 确认 Python 版本（期望 3.10–3.12）：
   ```bash
   python3 --version
   ```
2. 安装（普通用户）：
   ```bash
   pip install genai-bench
   ```
3. 验证三连：
   ```bash
   genai-bench --version
   genai-bench --help
   genai-bench benchmark --help
   ```

**需要观察的现象**：

- `--version` 应打印类似 `genai-bench version 0.0.5` 的字样（版本号来自 `pyproject.toml`，见 u1-l1 讲过的单一数据源设计）。
- `--help` 应列出 `benchmark`、`excel`、`plot` 三个子命令。
- `benchmark --help` 会刷出非常长的一屏选项，这很正常——`benchmark` 是整个项目最「重」的命令。

**预期结果**：三条命令都不报错。如果 `command not found`，多半是 pip 的 `bin/` 目录不在 `PATH` 中。

**说明**：是否真实可运行取决于本地环境，安装步骤本身「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：你想压测 AWS Bedrock，并希望结果上传到 S3。应该用哪条安装命令？

**参考答案**：`pip install genai-bench[aws]`（如果还要 Azure/GCP，可用 `[multi-cloud]` 一次装齐）。AWS 的 `boto3`/`botocore` 在 `aws` 可选分组里，默认安装不含它们。

**练习 2**：为什么 README 只写 `pip install genai-bench`，而不把所有云依赖都默认装上？

**参考答案**：为了让默认安装尽量轻量。大部分用户可能只压测 OpenAI 兼容后端，不需要任何云 SDK；把按需才用的依赖拆成可选分组，既加快安装、又减少冲突。

---

### 4.2 快速开始命令解析

#### 4.2.1 概念说明

README 给了一条「快速开始」的 text-to-text 基准命令。但直接复制粘贴是跑不通的——它里面有占位符，还省略了一个必填参数。本模块的目标是让你真正读懂这条命令：每个参数控制什么、哪些是必填、哪些有「隐式默认值」。

这里有一个非常重要的认知点：**`benchmark` 命令的默认行为，会把一次「看起来最小」的运行放大成几十次运行**。不理解默认值，就会被产物数量吓到。

#### 4.2.2 核心流程

`benchmark` 命令的处理流程（本讲只看与「跑通」直接相关的部分）：

1. **解析参数与校验**：click 解析命令行；回调函数校验后端、任务、tokenizer 等，并填充默认值（默认场景、默认并发）。
2. **认证**：根据 `--api-backend` 选择对应的认证 provider。
3. **加载 tokenizer 与数据**：加载分词器，按任务加载数据。
4. **构造采样器**：把场景、数据、tokenizer 组装成请求采样器。
5. **创建实验目录**：按命名规则生成目录，写入 `experiment_metadata.json`。
6. **双层循环跑运行**：外层遍历每个 `traffic_scenario`，内层遍历每个并发（或 batch size）；每个组合就是一次「run」。
7. **收尾报告**：跑完所有 run 后，自动生成 Excel 与图表。

其中第 6 步是理解「产物数量」的关键，下面用源码精读展开。

#### 4.2.3 源码精读

**CLI 入口与版本选项**：

[genai_bench/cli/cli.py:L47-L59](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/cli.py#L47-L59) —— `@click.group()` 定义 `cli` 组，并通过 `version_option` 暴露 `--version`。

**benchmark 子命令的注册**：

[genai_bench/cli/cli.py:L668-L670](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/cli.py#L668-L670) —— `cli.add_command(benchmark)` 等三行，把 `benchmark`、`excel`、`plot` 挂到 `cli` 组下。

**benchmark 命令函数签名（参数巨多，但很多有默认值）**：

[genai_bench/cli/cli.py:L62-L157](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/cli.py#L62-L157) —— 注意它叠了大量选项分组装饰器（`api_options`、`sampling_options` 等），并把 `context_settings={"show_default": True}` 打开，所以帮助里会显示默认值。docstring 只有一句：`Run a benchmark based on user defined scenarios.`。

**README 的快速开始命令（带占位符）**：

[README.md:L52-L61](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/README.md#L52-L61) —— 注意 `--api-backend "your-backend"` 是占位符，必须换成真实后端（如 `openai`/`vllm`/`sglang`）。

**两个必填参数（README 示例漏掉了一个）**：

- `--api-backend`：必填，且必须是受支持的后端枚举之一。
- `--model-tokenizer`：**必填**（`required=True, prompt=True`），README 示例没写，运行时会交互式提示输入。

[genai_bench/cli/option_groups.py:L630-L638](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/option_groups.py#L630-L638) —— `--model-tokenizer` 的定义，标注了 `required=True`。

**「惊喜」来源之一：默认场景有 5 个**：

[genai_bench/cli/validation.py:L43-L49](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/validation.py#L43-L49) —— `DEFAULT_SCENARIOS_FOR_CHAT` 列了 5 个场景字符串（如 `N(480,240)/(300,150)`、`D(7800,200)` 等）。

[genai_bench/cli/validation.py:L151-L169](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/validation.py#L151-L169) —— `validate_traffic_scenario_callback`：当用户既没传 `--traffic-scenario`、也没传数据集时，回退到该任务的默认场景列表。也就是说，不指定场景 ≠ 只跑一个场景，而是跑 5 个。

**「惊喜」来源之二：默认并发有 9 档**：

[genai_bench/cli/validation.py:L40-L41](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/validation.py#L40-L41) —— `DEFAULT_NUM_CONCURRENCIES = [1, 2, 4, 8, 16, 32, 64, 128, 256]`，共 9 档并发。

于是，README 那条「最小」命令在默认情况下其实是：

\[ \text{运行总次数} = \text{场景数} \times \text{并发档数} = 5 \times 9 = 45 \text{ 次 run} \]

**单次 run 的时间上限与请求上限**：

[genai_bench/cli/cli.py:L333-L333](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/cli.py#L333) —— `max_time_per_run *= 60`：`--max-time-per-run` 的单位是**分钟**，这里乘 60 换算成秒。所以 README 里的 `--max-time-per-run 5` 表示每 run 最多 5 分钟。

[genai_bench/cli/utils.py:L14-L53](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/utils.py#L14-L53) —— `manage_run_time`：每次 run 在「达到最大时长」或「完成请求数达到 `max_requests_per_run`」这两个条件中**先满足的那个**就结束。

**双层循环**：

[genai_bench/cli/cli.py:L408-L424](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/cli.py#L408-L424) —— 外层遍历 `traffic_scenario`，内层遍历 `iteration_values`（默认是并发档位），`total_runs = len(traffic_scenario) * len(iteration_values)`。

所以「跑第一个基准」的实用建议是：**显式收窄场景和并发**，避免一次跑 45 次。例如只跑 1 个场景、1 档并发：

```bash
genai-bench benchmark \
  --api-backend openai \
  --api-base "http://localhost:8080" \
  --api-key "your-api-key" \
  --api-model-name "your-model" \
  --model-tokenizer "gpt2" \
  --task text-to-text \
  --traffic-scenario "D(100,100)" \
  --num-concurrency 1 \
  --max-time-per-run 1 \
  --max-requests-per-run 10
```

这样 `total_runs = 1 × 1 = 1`，几分钟内就能看到完整产物。

#### 4.2.4 代码实践

**实践目标**：在不真正发起请求的前提下，弄清楚一条命令会触发多少次 run。

**操作步骤**：

1. 运行 `genai-bench benchmark --help`，找到 `--traffic-scenario`、`--num-concurrency`、`--max-time-per-run`、`--max-requests-per-run` 四个选项。
2. 对照本讲给出的默认值（5 个 chat 场景、9 档并发），手算 README 原始命令的 `total_runs`。
3. 再手算上面那条「收窄后」命令的 `total_runs`。

**需要观察的现象**：帮助信息里 `show_default=True` 是否如实显示了默认值（注意 `--num-concurrency` 的默认是一个列表）。

**预期结果**：原始命令 45 次 run；收窄命令 1 次 run。

**说明**：这是纯源码阅读型实践，不需要真实服务器，「待本地验证」仅指帮助输出格式。

#### 4.2.5 小练习与答案

**练习 1**：README 的 text-to-text 示例没有传 `--traffic-scenario` 和 `--num-concurrency`，会发生什么？

**参考答案**：校验回调会把场景补成 5 个默认 chat 场景、并发补成 9 档（1…256），因此会跑 5×9=45 次 run，而不是 1 次。

**练习 2**：`--max-time-per-run 5` 中的 5 是什么单位？为什么源码里有一行 `max_time_per_run *= 60`？

**参考答案**：单位是**分钟**。`*= 60` 把分钟换算成秒，供后续 `manage_run_time` 用秒来计时比较。

**练习 3**：如果不传 `--api-backend` 而传了一个不存在的后端名（比如仍用占位符 `your-backend`），会在哪一步报错？

**参考答案**：在参数校验阶段就报错。`--api-backend` 是 `click.Choice` 枚举，且 `validate_api_backend` 会校验取值，不在支持列表里会直接抛 `BadParameter`，命令根本进不了主流程。

---

### 4.3 运行产出物

#### 4.3.1 概念说明

一次实验（experiment）= 双层循环里的若干次 run + 最后的汇总报告。genai-bench 会把这一切都落盘到一个「实验目录」里。看懂这个目录的结构，你就能找到：原始指标、汇总表格、可视化图、以及出错时的调试快照。

实验目录默认就在**当前工作目录**下（不是固定的 `experiments/`，除非你用 `--experiment-base-dir` 指定）。README 示例里写 `./experiments/your_experiment` 只是建议的存放习惯。

#### 4.3.2 核心流程

产出物的生成顺序如下：

1. **实验目录命名**：按 `{api_backend}_{task}_{model}_{timestamp}` 规则生成目录名并 `mkdir`。
2. **写元数据**：把所有命令行参数与配置写进 `experiment_metadata.json`（整次实验只写一次）。
3. **逐 run 落盘**：每跑完一次 run，把该 run 的聚合指标存成一个 JSON 文件。
4. **单场景图**：每个场景的所有并发跑完后，画一张「推理速度 vs 吞吐」散点图。
5. **汇总报告**：全部 run 跑完后，生成 `{目录名}_summary.xlsx` 和一组 plot PNG。
6. **可选上传**：若加 `--upload-results`，把整个目录上传到对象存储（默认 OCI）。

#### 4.3.3 源码精读

**实验目录的命名规则**：

[genai_bench/cli/utils.py:L56-L108](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/utils.py#L56-L108) —— `get_experiment_path`：默认目录名形如 `{api_backend}_{server_engine_}{server_version_}{task}_{model}_{timestamp}`，时间戳格式 `%Y%m%d_%H%M%S`。若没传 `--experiment-base-dir`，目录直接建在当前工作目录下；已存在时会警告「可能覆盖」。

举例：`openai_text-to-text_gpt2_20260723_120000`（`server_engine`/`server_version` 为空时省略）。

**实验元数据写入**：

[genai_bench/cli/cli.py:L350-L377](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/cli.py#L350-L377) —— 构造 `ExperimentMetadata`（含 `cmd`、`benchmark_version`、`api_backend`、`task`、场景、并发、各类服务器信息等），`model_dump_json` 后写入 `experiment_metadata.json`。

**每次 run 的 JSON 文件命名**：

[genai_bench/cli/cli.py:L503-L510](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/cli.py#L503-L510) —— 文件名格式：

```
{sanitized_scenario_str}_{task}_{iteration_type}_{iteration}_time_{total_run_time}s.json
```

其中 `sanitize_string` 会把场景串里的 `/`、`,` 替换成 `_`，并去掉括号（见 [utils.py:L9-L16](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/utils.py#L9-L16)）。例如场景 `D(100,100)` 经清洗变成 `D100_100`，对应的 run 文件名类似 `D100_100_text-to-text_num_concurrency_1_time_5s.json`。

**单场景散点图**：

[genai_bench/cli/cli.py:L525-L531](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/cli.py#L525-L531) —— 每个场景跑完全部并发后，调用 `plot_single_scenario_inference_speed_vs_throughput` 画一张图。

**汇总 Excel**：

[genai_bench/cli/cli.py:L546-L556](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/cli.py#L546-L556) —— 调 `create_workbook` 生成 `{目录名}_summary.xlsx`，默认用 `percentile="mean"`。

**汇总 plot 图**：

[genai_bench/cli/cli.py:L562-L570](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/cli.py#L562-L570) —— 调 `plot_experiment_data_flexible`，按 `group_key="traffic_scenario"` 分组出图。

**出错时的调试快照（仅异常路径）**：

[genai_bench/cli/cli.py:L468-L487](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/cli.py#L468-L487) —— 当某次 run 的 `aggregate_metrics_data` 抛 `ValueError` 时，会先把该 run 的逐请求明细存成 `debug_for_run_{scenario}_{concurrency}.json`，再把异常抛出，方便排查。

把上面拼起来，一次完整实验后，目录里大致是这样：

```
openai_text-to-text_gpt2_20260723_120000/
├── experiment_metadata.json                  # 整次实验的元数据（命令、版本、配置）
├── D100_100_text-to-text_num_concurrency_1_time_5s.json   # 每个 run 的聚合指标
├── D100_100_text-to-text_num_concurrency_2_time_5s.json
├── ...                                        # (场景数 × 并发数) 个 run JSON
├── <scenario>_inference_speed_vs_throughput.png           # 每个场景一张散点图
├── openai_text-to-text_gpt2_20260723_120000_summary.xlsx  # 汇总 Excel
└── *.png                                       # 汇总 plot 图（按 traffic_scenario 分组）
```

#### 4.3.4 代码实践

**实践目标**：亲眼看到实验目录与产物的结构。

**操作步骤（二选一）**：

- **方案 A（有本地模型服务）**：启动一个 OpenAI 兼容服务（如本地 vLLM/SGLang），然后用本讲「收窄后」的那条命令（1 场景 × 1 并发、`--max-time-per-run 1 --max-requests-per-run 10`）跑一次。
- **方案 B（无模型服务，观察部分产物）**：用一个必然连不上的占位地址（如 `--api-base "http://localhost:1"`）执行同一条命令。genai-bench **没有**真正的 dry-run 模式，但它会在发请求**之前**就先创建实验目录并写好 `experiment_metadata.json`；随后因为连不上服务，请求会失败。

无论哪种方案，跑完（或中断）后：

1. 在当前目录找到刚才生成的实验目录（名字里有时间戳）。
2. 用 `cat experiment_metadata.json | head` 查看元数据，确认里面的 `cmd`、`api_backend`、`task` 是否与你的输入一致。
3. 方案 A 下，检查是否生成了 run JSON、`*_summary.xlsx` 和 PNG；方案 B 下，至少能看到目录与 `experiment_metadata.json`。

**需要观察的现象**：

- 目录命名是否符合 `{api_backend}_{task}_{model}_{timestamp}` 规则。
- run JSON 的文件名里，场景串是否被 `sanitize_string` 清洗过（无括号、无斜杠逗号）。

**预期结果**：

- 方案 A：得到完整的实验目录，含元数据、run JSON、Excel、PNG。
- 方案 B：只得到「目录 + `experiment_metadata.json`」，没有 run JSON（因为请求阶段就失败了），这恰好验证了产物是分阶段生成的。

**说明**：实际能否跑通取决于是否有可用的模型服务，具体现象「待本地验证」。**不要**假设命令一定成功——genai-bench 需要真实后端。

#### 4.3.5 小练习与答案

**练习 1**：为什么说「实验目录默认不在 `experiments/` 下」？

**参考答案**：`get_experiment_path` 在未传 `--experiment-base-dir` 时，直接把目录建在当前工作目录；`experiments/` 只是 README 里的示例习惯。想统一放到某处，需要显式传 `--experiment-base-dir ./experiments`。

**练习 2**：场景 `N(480,240)/(300,150)` 经过 `sanitize_string` 后，会变成什么样的文件名片段？

**参考答案**：`/`→`_`、`,`→`_`、`(`和`)`→删除，结果为 `N480_240_300_150`。

**练习 3**：如果某次 run 因为指标异常抛了 `ValueError`，你能从哪里找到这次 run 的逐请求明细？

**参考答案**：从实验目录下的 `debug_for_run_{sanitized_scenario}_{concurrency}.json` 里找。源码在抛错前会先把明细落盘，方便排查。

## 5. 综合实践

把本讲三个模块串起来，完成一个端到端的「最小可观察实验」：

1. **安装**：`pip install genai-bench`，运行 `genai-bench --version` 确认。
2. **设计命令**：写一条 `text-to-text` 命令，要求 `total_runs = 1`（即 1 个场景 × 1 档并发），`--max-time-per-run 1 --max-requests-per-run 10`，并补上 README 漏掉的 `--model-tokenizer`。
3. **预测产物**：在跑之前，先在纸上列出你预期会生成的目录名、文件名（注意 `sanitize_string` 的清洗规则）。
4. **执行并核对**：在有本地模型服务时执行；跑完后 `ls` 实验目录，把实际产物与你的预测逐项对照。
5. **读元数据**：打开 `experiment_metadata.json`，找到 `cmd` 字段，确认它完整记录了你输入的命令行（这正是后续 `excel`/`plot` 命令复现实验的依据）。

这个任务覆盖了「安装 → 命令解析 → 产物结构」整条链路，是后续讲义（尤其 u8-l1 的 benchmark 主流程）的实操预演。

## 6. 本讲小结

- genai-bench 通过 `pyproject.toml` 的 `[project.scripts]` 注册 `genai-bench` 命令，普通安装用 `pip install genai-bench`，云厂商能力靠 `[aws]`/`[azure]`/`[gcp]`/`[multi-cloud]` 可选依赖按需开启。
- Python 版本要求 3.10–3.12（`requires-python = ">=3.10,<3.13"`）；开发者可用 `make uv` + `make install` 走可编辑安装。
- `benchmark` 子命令挂在 `cli` 组下；README 的「快速开始」含占位符且漏了必填的 `--model-tokenizer`，照抄跑不通。
- 默认行为有「惊喜」：不指定场景/并发时，text-to-text 会跑 5 场景 × 9 并发 = 45 次 run；`--max-time-per-run` 单位是分钟。
- 产物分阶段落盘：先写 `experiment_metadata.json`，再按 run 写 JSON（文件名经 `sanitize_string` 清洗），最后生成 `*_summary.xlsx` 与 plot PNG；异常时会留 `debug_for_run_*.json`。
- 实验目录默认在当前工作目录，命名含 `{api_backend}_{task}_{model}_{timestamp}`，并非固定的 `experiments/`。

## 7. 下一步学习建议

本讲让你「跑通」了第一个基准。接下来建议：

- 想看懂仓库整体怎么组织 → 学 **u1-l3 目录结构与模块全景**，建立从 `cli` 到各子系统的源码地图。
- 想理解 `benchmark` 之外的两个子命令 → 学 **u1-l4 CLI 入口与三大命令**，了解 `excel`/`plot` 如何复用 analysis 子系统。
- 想理解实验产出的数据契约（`experiment_metadata.json` 的字段从哪来）→ 学 **u1-l5 协议数据模型**，认识 Pydantic 模型族。
- 后续想深入「场景串、采样、双层循环」的内部机制 → 进入 U2 单元（任务、场景与数据采样）。

如果你想直接挑战把整条主流程串起来，可以把 **u8-l1 benchmark 主流程编排（capstone）** 作为远期目标，但建议先补齐 U1–U2 的基础。
