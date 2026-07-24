# 安装与运行第一个基准

## 1. 本讲目标

上一篇（u1-l1）我们已经从宏观上认识了 genai-bench：它是一个面向 LLM 服务系统的 token 级基准测试工具。本讲把认知落到「手上能跑」——把工具装到本机，并真正发起一次最小的基准测试。

学完本讲，你应当能够：

1. 用 `pip` 安装 genai-bench，并知道 `aws / azure / gcp / multi-cloud / dev` 等可选依赖（extras）分别装什么、何时才需要装。
2. 读懂 README「Quick Start」里的 `genai-bench benchmark ...` 命令，说出每个关键参数的含义，并意识到「默认行为会把一次看似最小的运行放大成几十次」。
3. 看懂一次实验在磁盘上产出了哪些文件（实验目录、`experiment_metadata.json`、每次 run 的 JSON、`summary.xlsx`、图表 PNG），并能解释它们的来源与生成时机。

本讲只覆盖「安装 → 跑第一个基准 → 看产出物」这条最短路径；命令背后各子系统的内部机制（认证、采样、指标、分布式等）留给后续单元。

## 2. 前置知识

在动手之前，先用大白话澄清几个概念：

- **基准测试（benchmark）**：用一批人造的请求去「压」一个服务，测量它的速度与稳定性。你可以理解为「给服务器做体检」。
- **LLM 服务**：一个能接收文本请求、返回生成文本的 HTTP 服务，通常兼容 OpenAI 的 `/v1/chat/completions` 接口。vLLM、SGLang、真实的 OpenAI API 都属于这一类。
- **CLI（命令行接口）**：在终端里通过 `xxx --option value` 形式调用的程序。genai-bench 的命令名就叫 `genai-bench`。
- **子命令**：CLI 下的「子动作」。`genai-bench` 下挂了三个子命令：`benchmark`（跑测试）、`excel`（生成表格）、`plot`（画图）。本讲的主角是 `benchmark`。
- **pip 与可选依赖（extras）**：pip 安装一个包时，可以用 `包名[extra]` 语法额外安装一组可选依赖，例如 `pip install genai-bench[aws]` 会顺带装上 AWS 相关的库。
- **tokenizer（分词器）**：LLM 不直接数「字符」，而是数「token」。genai-bench 需要一个与被测模型匹配的 tokenizer 来精确统计输入/输出 token 数；本讲你只要知道 `--model-tokenizer` 必填即可，细节后面讲。

如果你还没读过 u1-l1，建议先读，了解项目的三大能力（CLI / 实时 UI / 实验分析器）以及 `__init__.py` 里 `gevent.monkey.patch_all()` 这一运行前提。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
| --- | --- |
| [README.md](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/README.md) | 项目门面，包含「Installation」一句话与「Quick Start」命令示例，是本讲命令的原始出处。 |
| [pyproject.toml](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/pyproject.toml) | 项目元数据：CLI 入口点（console_scripts）、Python 版本要求、核心依赖、可选依赖分组。 |
| [docs/getting-started/installation.md](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/docs/getting-started/installation.md) | 官方安装指南：PyPI / 开发 / Docker 三种方式与验证步骤。 |
| [genai_bench/cli/cli.py](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/cli.py) | CLI 入口与 `benchmark` 子命令的完整实现，是「跑第一个基准」的核心源码。 |
| [genai_bench/cli/option_groups.py](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/option_groups.py) | `benchmark` 命令全部参数（`--api-backend`、`--max-time-per-run` 等）的定义与帮助文本。 |
| [genai_bench/cli/utils.py](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/utils.py) | 实验目录命名（`get_experiment_path`）与单次 run 时长控制（`manage_run_time`）。 |
| [genai_bench/cli/validation.py](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/validation.py) | 默认并发档位、默认场景表（`DEFAULT_SCENARIOS_BY_TASK`）与各类参数校验。 |
| [genai_bench/utils.py](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/utils.py) | 通用工具，含文件名清洗函数 `sanitize_string`。 |
| [genai_bench/version.py](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/version.py) | 版本号读取，供 `--version` 使用。 |
| [Makefile](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/Makefile) | 开发者快捷命令：`make uv` / `make install` / `make dev` 等。 |

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：**安装方式**、**快速开始命令解析**、**运行产出物**。

### 4.1 安装方式

#### 4.1.1 概念说明

genai-bench 是一个标准的 Python 包，发布在 PyPI 上。最简单的安装方式就是一句 `pip install genai-bench`。但项目还把一部分「按需才用」的依赖拆成了可选分组（optional dependencies / extras），这样默认安装更轻量——只在真正需要某个云厂商时才把它装上。

理解这一点很关键：genai-bench 默认就能跑 OpenAI 兼容后端的基准；只有当你想压测 AWS Bedrock、Azure OpenAI、GCP Vertex，或想把结果上传到对应云存储时，才需要额外的云 SDK。这就是可选依赖存在的意义。

还有个常被忽略的点：安装后为什么终端里能直接敲 `genai-bench`？因为 `pyproject.toml` 里声明了一个 console_scripts 入口点，pip 安装时会据此在环境的 `bin/` 下生成同名可执行文件。注意仓库里**没有** `__main__.py`，命令完全靠这个入口点启动。

#### 4.1.2 核心流程

安装一条龙可以这样划分：

1. **确认 Python 版本**：项目硬性要求 Python 3.10–3.12（见下方源码精读的版本约束）。
2. **选择安装方式**：
   - 普通用户：`pip install genai-bench`（PyPI）。
   - 需要某朵云：`pip install genai-bench[aws]`（或 `azure` / `gcp` / `multi-cloud`）。
   - 二次开发者：用 Makefile 走 `uv` 虚拟环境 + 可编辑（editable）安装。
   - 容器化场景：Docker 拉取或自行构建。
3. **验证安装**：运行 `genai-bench --version` 等命令确认 CLI 可用。

```text
(可选)创建虚拟环境
      │
      ▼
pip install genai-bench          # 基础安装
      │  (或) pip install genai-bench[multi-cloud]   # 需要多云时
      │  (或) make install                          # 开发者 editable 安装
      ▼
genai-bench --version           # 验证：能跑、版本正确
genai-bench --help              # 验证：看到 benchmark / excel / plot 三个子命令
```

#### 4.1.3 源码精读

**（1）CLI 入口点是怎么注册的**——这是「为什么敲 `genai-bench` 就能启动」的根因：

```toml
[project.scripts]
genai-bench = "genai_bench.cli.cli:cli"
```

> [pyproject.toml:43-44](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/pyproject.toml#L43-L44)：声明命令名 `genai-bench` 指向 `genai_bench.cli.cli` 模块里的 `cli` 函数（一个 click group）。pip 安装时会据此在环境的 `bin/` 下生成同名可执行文件。

**（2）Python 版本约束**：

> [pyproject.toml:6](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/pyproject.toml#L6)：`requires-python = ">=3.10,<3.13"`，即支持 Python 3.10、3.11、3.12。（官方安装文档里提到 3.13，但 pip 实际以这里的 `<3.13` 硬约束为准，建议用 3.10–3.12。）

**（3）可选依赖分组（extras）**——按需安装云厂商 SDK：

> [pyproject.toml:46-83](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/pyproject.toml#L46-L83)：定义了 `dev`（ruff/mypy/pytest 等）、`aws`（boto3/botocore）、`azure`（azure-storage-blob/azure-identity）、`gcp`（google-cloud-storage/google-auth）、`multi-cloud`（AWS+Azure+GCP 合集）、`docs`（mkdocs 等）六组。例如要压测 AWS Bedrock 又要把结果传到 S3，就装 `genai-bench[aws]`；只想先跑通 OpenAI 兼容服务，基础安装即可。

**（4）README 的一句话安装**：

> [README.md:43-46](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/README.md#L43-L46)：给出 `pip install genai-bench` 并指向详细安装指南。

**（5）官方安装指南的三种方式**：

> [docs/getting-started/installation.md:7-58](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/docs/getting-started/installation.md#L7-L58)：分别讲 PyPI 安装、开发安装（`make uv` → `source .venv/bin/activate` → `make install`）、Docker 安装（`docker pull` 或 `docker build`）。

**（6）Makefile 里的开发安装快捷命令**：

```makefile
install: ## Install project dependencies.
	uv pip install --editable .

dev: ## Install development dependencies.
	uv pip install ".[dev,multi-cloud]"
```

> [Makefile:40-46](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/Makefile#L40-L46)：`make install` 用 `uv` 以 editable 模式装当前目录（改源码即时生效）；`make dev` 额外装上开发与多云依赖。`make uv` 则负责先装好 `uv` 并建虚拟环境（[Makefile:118-120](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/Makefile#L118-L120)）。

**（7）版本号来源**——`--version` 读的是它：

```python
import importlib.metadata
__version__ = importlib.metadata.version("genai-bench")
```

> [genai_bench/version.py:1-3](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/version.py#L1-L3)：运行时向已安装的包元数据要版本号（单一数据源在 `pyproject.toml` 的 `version = "0.0.5"`），避免版本号在多处写死而不同步。

**（8）安装后的验证命令**：

> [docs/getting-started/installation.md:60-73](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/docs/getting-started/installation.md#L60-L73)：提供 `genai-bench --version` / `--help` / `benchmark --help` 三条验证命令。

#### 4.1.4 代码实践

**实践目标**：完成 genai-bench 的安装并验证 CLI 可用。

**操作步骤**：

1. 确认 Python 版本（期望 3.10–3.12）：
   ```bash
   python3 --version
   ```
2. （推荐）创建并激活虚拟环境，再安装：
   ```bash
   python3.11 -m venv .venv
   source .venv/bin/activate
   pip install --upgrade pip
   pip install genai-bench
   ```
3. 验证三连：
   ```bash
   genai-bench --version       # 期望: genai-bench version 0.0.5
   genai-bench --help          # 期望: 看到 benchmark / excel / plot 三个子命令
   genai-bench benchmark --help
   ```

**需要观察的现象**：

- `--version` 打印的版本号与 `pyproject.toml` 中 `version = "0.0.5"` 一致（u1-l1 讲过的单一数据源设计）。
- `--help` 列出 `benchmark`、`excel`、`plot` 三条命令。
- `benchmark --help` 会刷出非常长的一屏选项——这很正常，`benchmark` 是整个项目最「重」的命令。

**预期结果**：三条命令都不报错。若提示 `command not found`，多半是虚拟环境未激活或 pip 的 `bin/` 不在 `PATH` 中，参考 [docs/getting-started/installation.md:120-129](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/docs/getting-started/installation.md#L120-L129) 的「Permission Issues」用 `pip install --user` 或虚拟环境解决。

> 说明：是否真实可运行取决于本地环境，安装步骤本身「待本地验证」。本讲不替你执行这些命令。

#### 4.1.5 小练习与答案

**练习 1**：你想压测 AWS Bedrock，并希望结果上传到 S3。应该用哪条安装命令？

> **答案**：`pip install genai-bench[aws]`（若还要 Azure/GCP，可用 `[multi-cloud]` 一次装齐）。AWS 的 `boto3`/`botocore` 在 `aws` 可选分组里（[pyproject.toml:56-59](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/pyproject.toml#L56-L59)），默认安装不含它们。

**练习 2**：仓库里没有 `genai_bench/__main__.py`，为什么安装后还能用 `genai-bench` 命令启动？

> **答案**：因为命令是通过 `pyproject.toml` 里的 `[project.scripts]` 入口点（`genai-bench = "genai_bench.cli.cli:cli"`，[pyproject.toml:43-44](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/pyproject.toml#L43-L44)）注册的，pip 安装时会生成可执行文件，无需依赖 `__main__.py`。

---

### 4.2 快速开始命令解析

#### 4.2.1 概念说明

README 给了一条「快速开始」的 text-to-text 基准命令。但直接复制粘贴是跑不通的——它里面有占位符，还省略了一个必填参数。本模块的目标是让你真正读懂这条命令：每个参数控制什么、哪些是必填、哪些有「隐式默认值」。

这里有一个非常重要的认知点：**`benchmark` 命令的默认行为，会把一次「看起来最小」的运行放大成几十次运行**。不理解默认值，就会被产物数量和运行时长吓到。

#### 4.2.2 核心流程

`benchmark` 命令的处理主干（本讲只看与「跑通」直接相关的部分，其余机制留给后续单元）：

```text
解析所有 CLI 参数（click + option_groups + validation 校验，并填充默认场景/并发）
      │
      ▼
认证 / 加载 tokenizer / 加载数据 / 构造采样器
      │
      ▼
创建实验目录、写出 experiment_metadata.json      ← 在发请求之前就落盘
      │
      ▼
双层循环：for 每个 traffic_scenario:
              for 每个并发档位 (num_concurrency):
                  启动一次 run（按 max_time_per_run / max_requests_per_run 任一达限即止）
                  聚合并保存这次 run 的 JSON
      │
      ▼
收尾：生成 summary.xlsx + 图表 PNG
```

其中「双层循环」是理解「产物数量与运行时长」的关键，下面用源码精读展开。

#### 4.2.3 源码精读

**（1）README 的 Quick Start 命令**——本讲命令的原始出处（注意含占位符）：

```bash
# Text generation (chat completions)
genai-bench benchmark --api-backend "your-backend" \
  --api-base "http://localhost:8080" \
  --api-key "your-api-key" \
  --api-model-name "your-model" \
  --task text-to-text \
  --max-time-per-run 5 \
  --max-requests-per-run 100
```

> [README.md:52-62](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/README.md#L52-L62)：`--api-backend "your-backend"` 是占位符，必须换成真实后端（如 `openai`/`vllm`/`sglang`）。此外该示例**漏掉了必填的 `--model-tokenizer`**，实际运行时需要补上。

**（2）`benchmark` 命令的定义与参数装配**：

```python
@click.command(context_settings={"show_default": True})
@api_options
@model_auth_options
...
@experiment_options
@sampling_options
...
@click.pass_context
def benchmark(ctx, api_backend, api_base, ...):
    """Run a benchmark based on user defined scenarios."""
```

> [genai_bench/cli/cli.py:62-157](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/cli.py#L62-L157)：`benchmark` 用一串装饰器把 10 个「参数组」（`api_options`、`experiment_options` 等）叠加进来。`show_default=True` 表示帮助里会显示每个参数的默认值。这种「分组装饰器」让海量参数井井有条（分组细节在 u1-l4、u8-l2 讲）。

**（3）`--api-backend` 可选值**——决定用哪个后端 User 类去发请求：

> [genai_bench/cli/option_groups.py:79-103](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/option_groups.py#L79-L103)：`--api-backend` 只能从 12 个值里选（`openai`、`oci-openai`、`oci-cohere`、`oci-cohere-v2`、`oci-genai`、`cohere`、`aws-bedrock`、`azure-openai`、`gcp-vertex`、`together`、`vllm`、`sglang`），且必填。`vllm`/`sglang` 因都是 OpenAI 兼容接口，内部会被映射复用同一个 User 类（详见 u3-l3）。`prompt=True` 表示没在命令行给时会交互式询问。

**（4）README 示例漏掉的必填参数 `--model-tokenizer`**：

> [genai_bench/cli/option_groups.py:630-638](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/option_groups.py#L630-L638)：`--model-tokenizer` 标注了 `required=True, prompt=True`，必须提供一个 HuggingFace 可加载的分词器或本地路径。它由 [validation.py:172-205](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/validation.py#L172-L205) 的 `validate_tokenizer` 实际加载（先尝试匿名下载，遇 401/403 才要求 `HF_TOKEN`）。

**（5）单次 run 的两个「刹车」**——理解 `--max-time-per-run` 与 `--max-requests-per-run`：

> [genai_bench/cli/option_groups.py:437-454](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/option_groups.py#L437-L454)：注意 `--max-time-per-run` 的单位是**分钟**（minute），不是秒。所以 README 里 `--max-time-per-run 5` 是「每次 run 最多 5 分钟」。

这两个「刹车」在代码里被换算与执行：

```python
max_time_per_run *= 60   # 分钟 → 秒
```

> [genai_bench/cli/cli.py:333](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/cli.py#L333)：进入循环前把分钟换算成秒。

```python
while total_run_time < max_time_per_run:
    gevent.sleep(1)
    total_run_time += 1
    total_completed_requests = environment.runner.stats.total.num_requests
    if total_completed_requests >= max_requests_per_run:
        logger.info(f"⏩ Exit the run as {total_completed_requests} requests have been completed.")
        break
return int(total_run_time)
```

> [genai_bench/cli/utils.py:37-53](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/utils.py#L37-L53)：`manage_run_time` 每秒轮询一次，「到时」或「请求数达标」任一条件成立就结束本次 run。把 `--max-requests-per-run` 设小，能快速跑完一次实验。

**（6）总 run 数 = 场景数 × 并发档位数**——评估「这次实验要跑多久」的关键：

```python
iteration_values = batch_size if iteration_type == "batch_size" else num_concurrency
total_runs = len(traffic_scenario) * len(iteration_values)
```

> [genai_bench/cli/cli.py:410-411](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/cli.py#L410-L411)：对 `text-to-text`，`iteration_type` 默认是 `num_concurrency`，故总 run 数 = 场景数 × 并发档位数。

⚠️ **重要提醒（默认行为的「惊喜」）**：若不显式传 `--traffic-scenario` 与 `--num-concurrency`，会用默认值。默认并发是 9 档：

```python
DEFAULT_NUM_CONCURRENCIES = [1, 2, 4, 8, 16, 32, 64, 128, 256]
```

> [genai_bench/cli/validation.py:40](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/validation.py#L40)

`text-to-text` 默认场景是 5 个：

```python
DEFAULT_SCENARIOS_FOR_CHAT = [
    "N(480,240)/(300,150)",
    "D(100,100)",
    "D(100,1000)",
    "D(2000,200)",
    "D(7800,200)",
]
```

> [genai_bench/cli/validation.py:43-49](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/validation.py#L43-L49)

也就是说，**README 那条「不传场景和并发」的命令，实际会跑 \(5 \times 9 = 45\) 次 run**（每次最多 5 分钟），对第一次试用来说太重了。

默认场景是如何被选中的？当你不传 `--traffic-scenario` 时：

```python
def validate_traffic_scenario_callback(ctx, param, value):
    task = ctx.params.get("task")
    ...
    if value:
        return [validate_scenario_callback(v) for v in value]
    if ctx.params.get("dataset_path") or ctx.params.get("dataset_config"):
        return ["dataset"]
    if task not in DEFAULT_SCENARIOS_BY_TASK:
        raise click.BadParameter(f"No default traffic scenarios defined for task '{task}'")
    logger.info(f"Using default traffic scenarios for task {task}")
    return DEFAULT_SCENARIOS_BY_TASK[task]
```

> [genai_bench/cli/validation.py:151-169](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/validation.py#L151-L169)：三种情况——①显式传了场景就用你传的；②没传场景但传了数据集，走 `dataset` 模式（直接采样数据集原始条目）；③都没传，按任务查默认场景表 `DEFAULT_SCENARIOS_BY_TASK`。

所以「跑第一个基准」的实用建议是：**显式收窄场景与并发**，例如只跑 1 个场景、1 档并发：

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

这样 \(total\_runs = 1 \times 1 = 1\)，几分钟内就能看到完整产物。（场景字符串 `D(100,100)` 的语法在 u2-l2 详讲，此处照抄即可。）

#### 4.2.4 代码实践

**实践目标**：弄清楚一条命令会触发多少次 run，并（在有服务时）亲历一次 run。

**操作步骤**：

1. 运行 `genai-bench benchmark --help`，找到 `--traffic-scenario`、`--num-concurrency`、`--max-time-per-run`、`--max-requests-per-run` 四个选项。
2. 对照本讲默认值（5 个 chat 场景、9 档并发），手算 README 原始命令的 `total_runs`。
3. 再手算上面那条「收窄后」命令的 `total_runs`。
4. （可选，需真实服务）执行「收窄后」命令，亲历一次 run。

**需要观察的现象**：

- 帮助信息里 `show_default=True` 是否如实显示了默认值（注意 `--num-concurrency` 的默认是一个列表）。
- 有真实服务时：终端先打印欢迎语与全部参数（[cli.py:169-177](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/cli.py#L169-L177)），再提示实验保存目录（[cli.py:346-348](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/cli.py#L346-L348)），随后出现实时仪表盘。

**预期结果**：原始命令 45 次 run；收窄命令 1 次 run。有真实服务时跑完会在实验目录看到产物（见 4.3）。

> 说明：第 1–3 步是纯源码阅读型实践，不需要真实服务器；第 4 步是否成功取决于你本地是否有可达的模型服务，运行结果「待本地验证」。本讲不替你执行命令。

#### 4.2.5 小练习与答案

**练习 1**：README 的 text-to-text 示例没有传 `--traffic-scenario` 和 `--num-concurrency`，会发生什么？

> **答案**：校验回调会把场景补成 5 个默认 chat 场景、并发补成 9 档（1…256），因此会跑 \(5 \times 9 = 45\) 次 run，而不是 1 次（依据 [validation.py:43-49](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/validation.py#L43-L49) 与 [validation.py:40](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/validation.py#L40)）。

**练习 2**：`--max-time-per-run 5` 中的 5 是什么单位？为什么源码里有一行 `max_time_per_run *= 60`？

> **答案**：单位是**分钟**（见 [option_groups.py:446-454](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/option_groups.py#L446-L454) help 文本「Unit: minute」）。`*= 60`（[cli.py:333](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/cli.py#L333)）把分钟换算成秒，供 `manage_run_time` 用秒计时比较。

**练习 3**：如果不传 `--api-backend` 而传了一个不存在的后端名（比如仍用占位符 `your-backend`），会在哪一步报错？

> **答案**：在参数校验阶段就报错。`--api-backend` 是 `click.Choice` 枚举（[option_groups.py:79-103](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/option_groups.py#L79-L103)），不在支持列表里会直接抛 `BadParameter`，命令根本进不了主流程。

---

### 4.3 运行产出物

#### 4.3.1 概念说明

一次实验（experiment）= 双层循环里的若干次 run + 最后的汇总报告。genai-bench 会把这一切都落盘到一个「实验目录」里。看懂这个目录的结构，你就能找到：配置快照、原始指标、汇总表格、可视化图，以及出错时的调试快照。

要注意：实验目录**默认就在当前工作目录下**（并非固定的 `experiments/`，除非你用 `--experiment-base-dir` 指定）。README 示例里写 `./experiments/your_experiment` 只是建议的存放习惯。

#### 4.3.2 核心流程

产出物的生成顺序与代码执行顺序一致：

1. **实验目录命名**：按 `{api_backend}_{task}_{model}_{timestamp}` 规则生成目录名并 `mkdir`。
2. **写元数据**：把命令行参数与配置写进 `experiment_metadata.json`（整次实验只写一次，**在发请求之前**）。
3. **逐 run 落盘**：每跑完一次 run，把该 run 的聚合指标存成一个 JSON 文件。
4. **单场景图**：每个场景的所有并发跑完后，画一张「推理速度 vs 吞吐」散点图。
5. **汇总报告**：全部 run 跑完后，生成 `{目录名}_summary.xlsx` 和一组 plot PNG。
6. **可选上传**：若加 `--upload-results`，把整个目录上传到对象存储（默认 OCI）。

#### 4.3.3 源码精读

**（1）实验目录的命名规则**：

```python
timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
default_name = (
    f"{api_backend}_"
    f"{server_engine + '_' if server_engine else ''}"
    f"{server_version + '_' if server_version else ''}"
    f"{task}_{model}_{timestamp}"
)
folder_name = experiment_folder_name or default_name
...
if experiment_base_dir:
    experiment_path = base_dir / folder_name
else:
    experiment_path = Path(folder_name)
```

> [genai_bench/cli/utils.py:82-100](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/utils.py#L82-L100)：默认目录名形如 `<api_backend>_<server_engine>_<server_version>_<task>_<model>_<时间戳>`（没传 `server_engine`/`server_version` 会省略对应段）。可用 `--experiment-folder-name` 自定义名字、`--experiment-base-dir` 指定父目录；都不传则建在**当前工作目录**下。目录已存在时会警告可能覆盖（[utils.py:102-105](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/utils.py#L102-L105)）。举例：`openai_text-to-text_gpt2_20260723_101530`。

**（2）最先落盘的 `experiment_metadata.json`**：

```python
experiment_metadata = ExperimentMetadata(
    cmd=cmd_line,
    benchmark_version=GENAI_BENCH_VERSION,
    api_backend=api_backend,
    ...
    traffic_scenario=traffic_scenario,
    max_time_per_run_s=max_time_per_run,
    max_requests_per_run=max_requests_per_run,
    ...
)
experiment_metadata_file.write_text(experiment_metadata.model_dump_json(indent=4))
```

> [genai_bench/cli/cli.py:350-377](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/cli.py#L350-L377)：用 Pydantic 模型 `ExperimentMetadata` 把「这次实验怎么跑的」完整记录（含原始命令行 `cmd`、版本、后端、场景、规模限制等），`model_dump_json` 写成带缩进的 JSON。**它在发任何请求之前就写好了**，所以即使后续请求全失败，你也能拿到这份配置快照（字段含义在 u1-l5 详讲）。

**（3）每次 run 的 JSON 文件命名**：

```python
run_name = (
    f"{sanitized_scenario_str}_{task}_{iteration_type}_"
    f"{iteration}_time_{total_run_time}s.json"
)
aggregated_metrics_collector.save(
    os.path.join(experiment_folder_abs_path, run_name), metrics_time_unit,
)
```

> [genai_bench/cli/cli.py:503-510](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/cli.py#L503-L510)：每次 run 结束后保存一个 JSON，文件名编码了「场景 + 任务 + 迭代类型 + 迭代值 + 实际耗时」。

其中场景串会被 `sanitize_string` 清洗成文件名安全的形式：

```python
def sanitize_string(input_str: str):
    return (
        input_str.replace("/", "_").replace(",", "_").replace("(", "").replace(")", "")
    )
```

> [genai_bench/utils.py:9-16](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/utils.py#L9-L16)：把 `/` 与 `,` 替换成 `_`，并删去 `(`、`)`。注意是顶层 `genai_bench/utils.py`，不是 `genai_bench/cli/utils.py`（`cli.py` 顶部 `from genai_bench.utils import sanitize_string` 导入的就是它）。因此场景 `D(100,100)` → `D100_100`（逗号变 `_`、括号删除，D 后**没有**额外下划线）；`N(480,240)/(300,150)` → `N480_240_300_150`。对应的 run 文件名形如 `D100_100_text-to-text_num_concurrency_1_time_60s.json`。

**（4）每个场景的散点图**：

```python
plot_single_scenario_inference_speed_vs_throughput(
    scenario_str, experiment_folder_abs_path, task,
    scenario_metrics, iteration_type,
)
```

> [genai_bench/cli/cli.py:525-531](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/cli.py#L525-L531)：一个场景的所有并发档位跑完后，画出「推理速度 vs 吞吐」散点图，落在实验目录里。

**（5）收尾的汇总 Excel 与灵活绘图**：

```python
experiment_metadata, run_data = load_one_experiment(experiment_folder_abs_path)
create_workbook(
    experiment_metadata, run_data,
    os.path.join(experiment_folder_abs_path,
                 f"{Path(experiment_folder_abs_path).name}_summary.xlsx"),
    percentile="mean", metrics_time_unit=metrics_time_unit, task=task,
)
```

> [genai_bench/cli/cli.py:545-556](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/cli.py#L545-L556)：所有 run 结束后，先 `load_one_experiment` 把整个实验目录读回来，再用 `create_workbook` 生成 `<目录名>_summary.xlsx`（默认用 `mean` 百分位汇总）。

```python
plot_experiment_data_flexible(
    [(experiment_metadata, run_data)],
    group_key="traffic_scenario",
    experiment_folder=experiment_folder_abs_path,
    plot_config=tts_config, metrics_time_unit=metrics_time_unit,
)
```

> [genai_bench/cli/cli.py:562-570](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/cli.py#L562-L570)：按 `traffic_scenario` 分组、用默认 preset 生成一组 PNG（`text-to-speech` 任务用 `2x4_tts`，其余用默认 2x4 网格）。所以 Excel 和图都是**自动生成**的，无需额外敲 `excel`/`plot`（那两个命令用于事后对多个实验做汇总分析）。

**（6）出错时的调试快照（仅异常路径）**：

> [genai_bench/cli/cli.py:468-487](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/cli.py#L468-L487)：当某次 run 的 `aggregate_metrics_data` 抛 `ValueError` 时，会先把该 run 的逐请求明细存成 `debug_for_run_{sanitized_scenario}_{concurrency}.json`，再把异常抛出，方便排查。

把上面拼起来，一次完整实验后，目录里大致是这样（以最小规模 1 场景 × 1 并发为例）：

```text
openai_text-to-text_gpt2_20260723_101530/
├── experiment_metadata.json                  # 整次实验的元数据（命令、版本、配置）
├── D100_100_text-to-text_num_concurrency_1_time_60s.json   # 每个 run 的聚合指标
├── D100_100_inference_speed_vs_throughput.png              # 该场景的散点图
├── openai_text-to-text_gpt2_20260723_101530_summary.xlsx   # 汇总 Excel
└── *.png                                       # 汇总 plot 图（按 traffic_scenario 分组）
```

#### 4.3.4 代码实践

**实践目标**：亲眼看到实验目录与产物的结构，并验证产物的分阶段生成。

**操作步骤（二选一）**：

- **方案 A（有本地模型服务）**：启动一个 OpenAI 兼容服务（如本地 vLLM/SGLang），然后用本讲「收窄后」的那条命令（1 场景 × 1 并发、`--max-time-per-run 1 --max-requests-per-run 10`）跑一次。
- **方案 B（无模型服务，观察部分产物）**：用一个必然连不上的占位地址（如 `--api-base "http://localhost:1"`）执行同一条命令。⚠️ genai-bench **没有**真正的 dry-run 模式，但它会在发请求**之前**就先创建实验目录并写好 `experiment_metadata.json`；随后因连不上服务，请求会失败。

无论哪种方案，跑完（或中断）后：

1. 在当前目录找到刚才生成的实验目录（名字里有时间戳）。
2. 查看 `experiment_metadata.json`：
   ```bash
   cat <实验目录>/experiment_metadata.json
   ```
   确认里面的 `cmd`、`api_backend`、`task` 是否与你的输入一致。
3. 方案 A 下，检查是否生成了 run JSON、`*_summary.xlsx` 和 PNG；方案 B 下，至少能看到目录与 `experiment_metadata.json`。

**需要观察的现象**：

- 目录命名是否符合 `{api_backend}_{task}_{model}_{timestamp}` 规则。
- run JSON 的文件名里，场景串是否被 `sanitize_string` 清洗过（`D(100,100)` → `D100_100`，无括号、无斜杠逗号）。

**预期结果**：

- 方案 A：得到完整的实验目录，含元数据、run JSON、Excel、PNG。
- 方案 B：只得到「目录 + `experiment_metadata.json`」，没有 run JSON（因为请求阶段就失败了）——这恰好验证了产物是**分阶段**生成的。

> 说明：实际能否跑通取决于是否有可用的模型服务，具体现象「待本地验证」。**不要**假设命令一定成功——genai-bench 需要真实后端。

#### 4.3.5 小练习与答案

**练习 1**：为什么即使模型服务不可达、请求全部失败，实验目录里通常还是会出现 `experiment_metadata.json`？

> **答案**：因为 `experiment_metadata.json` 在双层运行循环**之前**就已写盘（[cli.py:374-377](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/cli.py#L374-L377)），它记录的是「实验打算怎么跑」，与请求成败无关。真正依赖请求结果的 run JSON、汇总 Excel 是在循环中/循环后才生成。

**练习 2**：默认情况下，实验目录建在哪里？如何改？

> **答案**：默认建在**当前工作目录**下（[utils.py:99-100](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/utils.py#L99-L100) 的 `else` 分支）。用 `--experiment-base-dir` 指定父目录，用 `--experiment-folder-name` 指定目录名。

**练习 3**：场景 `N(480,240)/(300,150)` 经过 `sanitize_string` 后，会变成什么样的文件名片段？

> **答案**：`/`→`_`、`,`→`_`、`(`和`)`→删除，结果为 `N480_240_300_150`（依据 [genai_bench/utils.py:9-16](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/utils.py#L9-L16)）。

**练习 4**：如果某次 run 因为指标异常抛了 `ValueError`，你能从哪里找到这次 run 的逐请求明细？

> **答案**：从实验目录下的 `debug_for_run_{sanitized_scenario}_{concurrency}.json` 里找（[cli.py:468-487](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/cli/cli.py#L468-L487)）。源码在抛错前会先把明细落盘，方便排查。

## 5. 综合实践

把本讲三个模块串起来，完成一个端到端的「最小可观察实验」：

1. **安装**：`pip install genai-bench`，运行 `genai-bench --version` 确认可用。
2. **设计命令**：写一条 `text-to-text` 命令，要求 `total_runs = 1`（即 1 个场景 × 1 档并发），`--max-time-per-run 1 --max-requests-per-run 10`，并补上 README 漏掉的 `--model-tokenizer`（如 `gpt2`）。
3. **预测产物**：在跑之前，先在纸上列出你预期会生成的目录名、run JSON 文件名（注意 `sanitize_string` 的清洗规则：`D(100,100)` → `D100_100`）。
4. **执行并核对**：在有本地模型服务时执行；跑完后 `ls` 实验目录，把实际产物与你的预测逐项对照。
5. **读元数据**：打开 `experiment_metadata.json`，找到 `cmd` 字段，确认它完整记录了你输入的命令行——这正是后续 `excel`/`plot` 命令复现实验的依据。思考：为什么基准工具要把原始命令行也存进元数据？（答案：为了让实验**可复现**——别人拿到这个目录，照着 `cmd` 就能复跑同样配置。）

这个任务覆盖了「安装 → 命令解析 → 产物结构」整条链路，是后续讲义（尤其 u8-l1 的 benchmark 主流程）的实操预演。

## 6. 本讲小结

- genai-bench 通过 `pyproject.toml` 的 `[project.scripts]` 注册 `genai-bench` 命令；普通安装用 `pip install genai-bench`，云厂商能力靠 `[aws]`/`[azure]`/`[gcp]`/`[multi-cloud]` 可选依赖按需开启（无需 `__main__.py`）。
- Python 版本要求 3.10–3.12（`requires-python = ">=3.10,<3.13"`）；开发者可用 `make uv` + `make install` 走可编辑安装。
- `benchmark` 子命令挂在 `cli` 组下；README 的「快速开始」含占位符且漏了必填的 `--model-tokenizer`，照抄跑不通。
- 默认行为有「惊喜」：不指定场景/并发时，text-to-text 会跑 \(5 \times 9 = 45\) 次 run；`--max-time-per-run` 单位是分钟（代码里 `*= 60` 换算成秒）。
- 产物分阶段落盘：**先**写 `experiment_metadata.json`（发请求前），**再**按 run 写 JSON（文件名经 `sanitize_string` 清洗，`D(100,100)` → `D100_100`），**最后**生成 `*_summary.xlsx` 与 plot PNG；某 run 指标异常时会留 `debug_for_run_*.json`。
- 实验目录默认在当前工作目录，命名含 `{api_backend}_{task}_{model}_{timestamp}`，并非固定的 `experiments/`。

## 7. 下一步学习建议

本讲让你「跑通」了第一个基准。接下来建议：

- 想看懂仓库整体怎么组织 → 学 **u1-l3 目录结构与模块全景**，建立从 `cli` 到各子系统的源码地图。
- 想理解 `benchmark` 之外的两个子命令 → 学 **u1-l4 CLI 入口与三大命令**，了解 `excel`/`plot` 如何复用 analysis 子系统。
- 想理解实验产出的数据契约（`experiment_metadata.json` 的字段从哪来）→ 学 **u1-l5 协议数据模型**，认识 Pydantic 模型族。
- 想深入「场景串、采样、双层循环」的内部机制 → 进入 **U2 单元（任务、场景与数据采样）**，尤其 **u2-l2（场景定义与解析）**。
- 如果你想直接挑战把整条主流程串起来，可以把 **u8-l1 benchmark 主流程编排（capstone）** 作为远期目标，但建议先补齐 U1–U2 的基础。
