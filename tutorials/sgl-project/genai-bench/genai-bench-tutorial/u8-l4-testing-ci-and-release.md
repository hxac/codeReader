# 测试体系、CI 与发布

> 本讲是专家层（U8）的最后一篇，也是整本学习手册的收尾工程篇。前面 u8-l1 用 `benchmark` 主流程把各子系统缝合成一条数据流，本讲则回答一个对二次开发者更重要的问题：**我改完代码后，项目用什么机制保证它不会被改坏，又怎么把一个可信的版本交付出去？**

## 1. 本讲目标

学完本讲，你应当能够：

- 读懂 `tests/` 目录的镜像式组织方式，理解 `conftest.py` 中的 `autouse` fixture 在防「测试间状态泄漏」上的作用。
- 看懂 `.github/workflows/ci.yml` 中 `test / lint / build` 三个 job 各自做了什么，以及 93% 覆盖率门槛与 `ruff` + `mypy` 双重静态检查的意义。
- 理解 `.github/workflows/release.yml` 的「双源版本校验」机制：为什么 GitHub Release tag 必须与 `pyproject.toml` 里的版本号严格相等。
- 掌握 `Dockerfile` 的分层构建逻辑与 `Makefile` 提供的本地开发快捷命令，能用 Docker 跑一次基准。

## 2. 前置知识

在进入源码前，先用大白话建立几个关键概念。

- **单元测试（unit test）**：针对一个函数或类写的「输入→期望输出」断言。genai-bench 用 `pytest` 作为测试运行器。
- **测试覆盖率（coverage）**：测试执行时「踩到」了多少行源码的百分比。覆盖率不是质量的充分条件，但一个覆盖率断崖通常是「没人测的死角」的信号。
- **夹具（fixture）**：pytest 提供的依赖注入机制，用 `@pytest.fixture` 标记一个函数，测试函数把它当参数就能拿到其返回值；加 `autouse=True` 则无需声明参数、每个测试自动套用。
- **CI（持续集成，Continuous Integration）**：每次 `push` 或发 PR 时，GitHub Actions 自动在云端跑一遍测试与检查，红的 CI 意味着这次改动有问题。
- **静态检查 / Lint**：不运行代码、只读源码就能发现问题的工具。本项目用 `ruff`（格式 + 代码规范）、`mypy`（类型检查）。
- **pre-commit**：在 `git commit` 之前本地自动跑一遍检查的钩子，把问题挡在进入仓库之前。
- **uv**：一个用 Rust 写的极速 Python 包管理器，本项目用它替代传统的 `pip` / `venv` 组合。
- **单一数据源（single source of truth）**：同一个事实（如版本号）只在一个地方定义，其它地方去「读」它而不是各自维护。u1-l1 已讲过版本号的这种设计，本讲会在发布流程里看到它的闭环。

## 3. 本讲源码地图

本讲横跨「测试、CI 配置、构建脚本、发布流水线」四类工件，它们都不在 `genai_bench/` 包里，而是仓库根目录与 `.github/` 下的工程文件：

| 文件 | 作用 |
| --- | --- |
| `tests/conftest.py` | 全局 pytest fixture，重置 `OpenAIUser` 类属性与 `warning_once` 缓存，提供 mock tokenizer。 |
| `tests/`（整目录） | 镜像 `genai_bench/` 包结构的测试集合，共 72 个 `test_*.py` 文件。 |
| `.github/workflows/ci.yml` | CI 流水线：矩阵测试、覆盖率门槛、ruff/mypy 静态检查、构建校验。 |
| `.github/workflows/release.yml` | 发布流水线：跑测试→双源版本校验→发 PyPI→构建并推送多架构 Docker 镜像。 |
| `Makefile` | 本地开发命令封装（test / lint / format / build / build-image 等）。 |
| `Dockerfile` | 容器镜像构建脚本，以 `genai-bench` 为 ENTRYPOINT。 |
| `docs/user-guide/run-benchmark-using-docker.md` | 用 Docker 跑基准的用户指南。 |
| `genai_bench/version.py` | 运行时读取已安装包版本的唯一出口。 |
| `pyproject.toml` / `.coveragerc` / `mypy.ini` / `.pre-commit-config.yaml` | 各工具的配置文件。 |

## 4. 核心概念与源码讲解

本讲的三个最小模块按「写代码 → 守代码 → 发代码」的顺序组织：先有测试体系保证正确性，再用 CI 与静态检查守住质量线，最后通过发布流水线把可信版本交付到 PyPI 与容器仓库。

### 4.1 测试组织：pytest 目录、conftest 与测试模式

#### 4.1.1 概念说明

genai-bench 是一个有多套认证后端、多种任务模态、跨进程分布式架构的项目，很容易出现「改 A 测试坏了 B」的耦合问题。因此测试体系要解决两件事：

1. **可发现**：测试放在哪里、按什么规则命名，运行器才能自动找到。
2. **可隔离**：每个测试跑完后不留下副作用，不污染下一个测试。

项目用 **「测试目录镜像源码目录」** 的约定解决可发现问题：`tests/auth/azure/` 对应 `genai_bench/auth/azure/`，`tests/metrics/` 对应 `genai_bench/metrics/`，依此类推。这样改了某个模块，立刻知道去哪个测试目录验证。可隔离则靠 `conftest.py` 里的 `autouse` fixture 来兜底。

#### 4.1.2 核心流程

一次本地测试运行的流程：

```
make test (或 pytest tests)
   │
   ├─ pytest 收集所有 test_*.py（共 72 个文件）
   ├─ 对每个测试，先套用 conftest.py 里 autouse=True 的 fixture
   │     ├─ reset_openai_user_attrs   保存 → 测试 → 还原类属性
   │     └─ reset_warning_once_cache  清空去重告警集合
   ├─ 按需注入 mock_tokenizer / mock_tokenizer_path
   ├─ 运行测试函数，收集断言结果
   └─ pytest-cov 统计覆盖率，按 .coveragerc 排除部分文件
```

测试目录按子系统划分，数量分布大致为：`tests/auth/`（含多云子目录）最多，其次是 `tests/cli/`、`tests/storage/`、`tests/analysis/`，以及 `tests/metrics/`、`tests/sampling/`、`tests/scenarios/`、`tests/ui/`、`tests/distributed/`、`tests/user/`、`tests/data/`，外加一个 `tests/integration/`（跨云组合的集成测试）和顶层的 `tests/test_time_units.py`。

#### 4.1.3 源码精读

先看唯一的 `conftest.py`（全仓只有这一个，位于 `tests/` 根，对所有测试生效）：

[tests/conftest.py:L10-L25](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/tests/conftest.py#L10-L25) — `reset_openai_user_attrs` 是一个 `autouse=True` 的 fixture，它的关键设计是「先存原值、`yield` 让测试跑、再还原」。这是因为在 u3-l3 中讲过：主流程会把 `host` 与 `auth_provider` 作为**类属性**注入到 `OpenAIUser` 上。如果不还原，一个测试注入的 mock 值会泄漏到下一个测试，导致诡异失败。`yield` 前后正好对应「setup / teardown」两段。

[tests/conftest.py:L28-L35](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/tests/conftest.py#L28-L35) — `reset_warning_once_cache` 同样是 `autouse`。u3-l2 提到过 `warning_once`：同一个告警只打印一次，靠一个全局集合 `_warning_once_keys` 去重。这个集合在测试间必须清空，否则「第一次告警」的断言会失败。

[tests/conftest.py:L38-L47](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/tests/conftest.py#L38-L47) — `mock_tokenizer` fixture 不加载在线模型，而是指向 `tests/fixtures/local_bert_base_uncased` 这个**仓库内置的离线 tokenizer**，让测试既真实（用 `AutoTokenizer.from_pretrained` 真正算 token）又无需联网。

再看一个典型的单元测试，体会项目的测试模式：

[tests/metrics/test_metrics.py:L31-L50](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/tests/metrics/test_metrics.py#L31-L50) — 这是 u4-l1 的 `RequestMetricsCollector` 的测试。注意它的写法：用 `MagicMock(spec=UserChatResponse)` 造一个「形状合法」的假响应，手动填好 `start_time` / `time_at_first_token` / `end_time` / token 计数，然后直接断言计算结果（`ttft == 100`、`e2e_latency == 110`）。这种「构造 mock 输入 → 调真实生产代码 → 断言数值」正是全仓测试的主流范式，无需起任何服务。

最后看版本号本身的测试，它揭示了 `version.py` 的运行时机制：

[tests/cli/test_version.py:L15-L30](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/tests/cli/test_version.py#L15-L30) — 这个测试 `patch` 了 `importlib.metadata.version` 让它返回 `"1.2.3"`，再 `importlib.reload` 强制重新加载 `genai_bench.version` 与 `genai_bench.cli.cli`，最后调用 CLI 的 `--version` 断言输出 `genai-bench version 1.2.3`。它验证了 u1-l1 讲过的「版本号单一数据源」：CLI 的版本完全来自 `version.py` 里那行 `importlib.metadata.version("genai-bench")`。

#### 4.1.4 代码实践

**实践目标**：在本地跑一遍测试套件，并观察 `autouse` fixture 的隔离效果。

**操作步骤**：

1. 安装开发依赖（参考 `make dev`）：`uv pip install ".[dev,multi-cloud]"`。
2. 运行全部测试：`make test`（等价于 `uv run pytest tests --cov --cov-config=.coveragerc -vv -s`）。
3. 只跑单个子系统：`uv run pytest tests/metrics -vv`。
4. 跑单个测试函数：`uv run pytest tests/metrics/test_metrics.py::test_request_level_metrics_calculation_with_chat_response -vv`。

**需要观察的现象**：

- 步骤 2 末尾会打印一份覆盖率报告，TOTAL 行给出整体百分比。
- 步骤 3/4 应全部通过；若你故意在某个测试里给 `OpenAIUser.host` 赋一个脏值而不还原，会发现隔离被打破（这只是用来理解 fixture 价值的思考实验，**不要真的改源码**）。

**预期结果**：本地覆盖率应接近 CI 中的水平（门槛是 93%）。**待本地验证**：精确百分比取决于你的运行环境与是否装齐 `[multi-cloud]` 可选依赖；缺云 SDK 时部分测试可能被跳过（skip）而非失败。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `reset_openai_user_attrs` 必须用 `yield` 把逻辑拆成两段，而不能只在测试前还原？

> **答案**：fixture 需要在「测试运行前」保存原始值，「测试运行后」还原。`yield` 之前的代码是 setup（保存原值），`yield` 暂停把控制权交给测试，`yield` 之后的代码是 teardown（还原）。若只在测试前还原，就丢失了「先保存再还原」的语义——你无法还原一个从未保存过的值。

**练习 2**：`mock_tokenizer` fixture 为什么用仓库内置的 `local_bert_base_uncased`，而不是直接 `AutoTokenizer.from_pretrained("bert-base-uncased")`？

> **答案**：为了测试**离线、确定性、不依赖网络**。从 HuggingFace Hub 在线拉取会受网络波动与配额影响，还可能因模型仓库变更导致测试不稳定（flaky）。内置 fixture 让测试在无网的 CI 环境里也能复现真实 tokenizer 行为。

### 4.2 CI 与代码质量：ci.yml 三段式守门

#### 4.2.1 概念说明

CI 的本质是「把人工 review 之前能自动判定的质量门，交给机器守」。genai-bench 的 CI 把守门动作分成三个独立 job：

- **test**：在 Python 3.10 / 3.11 / 3.12 三个版本上跑测试，并卡 93% 覆盖率门槛。
- **lint**：用 `ruff` 查格式与代码规范、用 `mypy` 查类型。
- **build**：确认包能正常构建出来（依赖前两个 job 通过）。

三段式的好处是**关注点分离 + 并行**：lint 与 test 可以同时跑，build 在两者都绿之后才跑，任何一个 job 红了都能立刻定位是「测试挂了」「风格不合规」还是「构建坏了」。

#### 4.2.2 核心流程

```
push / PR 到 main、develop
        │
   ┌────┴───── CI workflow 触发 ─────┐
   ▼                                  ▼
 test job (矩阵 3.10/3.11/3.12)    lint job (单 3.11)
   ├─ uv sync 依赖                   ├─ ruff format --diff
   ├─ pytest --cov                   ├─ ruff check
   ├─ coverage --fail-under=93       └─ mypy
   └─ （main + 3.11 时算覆盖率徽章数）
                  │
                  ▼ （needs: [test, lint]）
            build job
                  └─ uv build -vvv
```

#### 4.2.3 源码精读

先看触发条件——CI 只在 `main` 与 `develop` 分支的 push 和 PR 上跑：

[.github/workflows/ci.yml:L3-L7](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/.github/workflows/ci.yml#L3-L7) — `on.push.branches` 与 `on.pull_request.branches` 限定了触发分支，避免在任意特性分支的 push 上浪费 CI 资源。

**test job** 用矩阵跑三个 Python 版本：

[.github/workflows/ci.yml:L10-L33](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/.github/workflows/ci.yml#L10-L33) — 关键点有三：其一，`matrix.python-version: ["3.10", "3.11", "3.12"]` 与 `pyproject.toml` 声明的 `requires-python = ">=3.10,<3.13"`（[pyproject.toml:L6](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/pyproject.toml#L6)）严格对应，矩阵覆盖整个支持区间。其二，安装用 `uv sync --extra dev --extra multi-cloud`（[ci.yml:L25](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/.github/workflows/ci.yml#L25)），即开发工具与全部云 SDK 都装上，确保所有测试都能跑。其三，`coverage report --fail-under=93`（[ci.yml:L31-L33](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/.github/workflows/ci.yml#L31-L33)）是硬门槛：覆盖率不到 93% 整个 job 直接失败。

覆盖率统计本身会排除一批「难测或不值得测」的文件，配置在 `.coveragerc`：

[.coveragerc:L1-L10](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/.coveragerc#L1-L10) — `omit` 列表排除了报告生成（`excel_report.py` / `plot_report.py` / `flexible_plot_report.py` / `plot_config.py`）、实时 UI（`ui/*`）、日志（`logging.py`）以及测试自身（`tests/*`）。这些要么是 u6 讲过的「薄封装/绘图」层，要么是 u7 讲过的「纯渲染」层，强求高覆盖率性价比低。换言之，93% 是在「剔除渲染层之后」对核心逻辑的硬要求。

**lint job** 串了三道静态检查：

[.github/workflows/ci.yml:L62-L72](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/.github/workflows/ci.yml#L62-L72) — `ruff format --diff` 检查格式（只打印差异不修改，有差异即失败），`ruff check` 检查代码规范（启用的规则集见 `pyproject.toml`），`mypy genai_bench --config-file=mypy.ini` 做类型检查。注意 mypy 只查 `genai_bench` 不查 `tests`，对测试网的类型要求更宽松。`mypy.ini` 本身开了 `ignore_missing_imports` 与若干 `disable_error_code`（[mypy.ini:L1-L6](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/mypy.ini#L1-L6)），是为了在依赖复杂、联合类型较多的现实里控制误报。

`ruff` 的规则在 `pyproject.toml` 里集中声明：

[pyproject.toml:L101-L124](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/pyproject.toml#L101-L124) — 启用 `E`（pycodestyle）、`F`（Pyflakes）、`B`（bugbear）、`SIM`（simplify）、`G`（logging）几组，并显式忽略一批（如 `G004` 允许日志里用 f-string、`E731` 允许 lambda 赋值）。这种「启用大类 + 白名单忽略」是务实项目的常见做法。

**build job** 必须等前两个 job 都通过：

[.github/workflows/ci.yml:L74-L91](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/.github/workflows/ci.yml#L74-L91) — `needs: [test, lint]` 声明了依赖，只有测试和静态检查都绿，才执行 `uv build -vvv` 真正打 sdist/wheel 包。这是「交付物可信」的最后一道关：能构建出合法的发行包。

除了 CI，本地还有 `pre-commit` 把同样的检查前置到 `git commit` 瞬间：

[.pre-commit-config.yaml:L1-L32](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/.pre-commit-config.yaml#L1-L32) — 三个 repo：通用 hooks（尾随空格、文件结尾、JSON/TOML/YAML 合法性、大文件）、`ruff`（含 `--fix` 自动修）、`mypy`。配置后，`git commit` 会先跑这些钩子，不通过则拒绝提交。注意 pre-commit 里 ruff 的版本（`v0.7.4`）与 `pyproject.toml` 中 dev 依赖的 `ruff~=0.15.0`（[pyproject.toml:L48](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/pyproject.toml#L48)）不完全一致——版本漂移是真实存在的工程细节，理想情况下应保持对齐。

#### 4.2.4 代码实践

**实践目标**：在本地复现 CI 的 lint 与 test，体会三道静态检查各自抓什么。

**操作步骤**：

1. `make lint`（等价 `ruff format --diff` + `mypy`）。
2. `make format`（等价 `isort` + `ruff format` + `ruff check --fix`，会**就地修改**代码）。
3. `uv run ruff check genai_bench tests` 单独跑规范检查。
4. 阅读完整 `.github/workflows/ci.yml`，对照本节的三段式说明，标注每个 step 属于 test / lint / build 哪一类。

**需要观察的现象**：

- 步骤 1 在干净代码上应无输出（通过）。
- 若你（在自己的 fork 里）故意写一行 `import os` 但不用它，步骤 3 的 `ruff check` 会报 `F401`（未使用导入），而 mypy 不会报——这说明 ruff 与 mypy 分工不同。
- 步骤 4 应能确认：覆盖率门槛只在 test job、mypy 只在 lint job、`uv build` 只在 build job。

**预期结果**：干净仓库上 `make lint` 通过、`make test` 全绿且覆盖率 ≥ 93%。**待本地验证**：若未安装全部 `[multi-cloud]` 依赖，少数云相关测试可能 skip，但不影响 lint。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `.coveragerc` 要把 `ui/*` 和 `logging.py` 排除在覆盖率统计之外？这会降低质量要求吗？

> **答案**：`ui/*`（实时仪表盘渲染）和 `logging.py`（rich 日志 handler）属于 u7 讲过的「纯渲染/纯 I/O」层，难以在不引入大量 mock 的情况下有意义地测试，强求覆盖率会催生「为覆盖而覆盖」的低价值测试。排除它们是**聚焦**而非**放水**：93% 的门槛因此更准确地反映核心逻辑（采样、指标、认证、采样编排）的覆盖情况，反而提升了对关键路径的约束力。

**练习 2**：CI 里 test job 用了三个 Python 版本的矩阵，但 lint 与 build job 都只用 3.11。为什么不对每个 job 都跑矩阵？

> **答案**：lint（代码风格/类型）与 build（打包）的结果基本与具体小版本无关，没必要 ×3 重复消耗。而 test 必须跨版本，因为运行时行为（如标准库 API 差异、类型注解处理）在 3.10/3.11/3.12 间确有不同，矩阵能捕获版本相关的回归。这是「在风险高的环节上投入、在风险低的环节上节省」的典型 CI 权衡。

### 4.3 Docker 与发布：release.yml、Dockerfile 与版本管理

#### 4.3.1 概念说明

发布（release）与 CI 的根本区别在于**不可逆性与可信度**：CI 失败可以重跑，但一个已发到 PyPI 的版本号**不能复用、不能删除**（PyPI 不允许重新上传同版本）。因此 release 流水线的设计哲学是「宁可失败也不要发错」。

genai-bench 的发布有两个产物：

1. **PyPI 包**：`pip install genai-bench` 的来源。
2. **Docker 镜像**：推到 GitHub Container Registry（ghcr.io），供不想自己装 Python 环境的用户直接 `docker pull`。

两者都依赖一个核心校验：**版本号必须双源一致**。

#### 4.3.2 核心流程

```
GitHub Release 发布（或 workflow_dispatch 手动触发）
        │
   release job
        ├─ 跑测试 + 覆盖率门槛（再守一次，发版前最后验证）
        ├─ 提取版本号①：从 Release tag（如 v0.0.5）→ 0.0.5
        ├─ 提取版本号②：从 pyproject.toml 读取
        ├─ 校验 ① == ②，不一致立即失败
        ├─ uv build 打包 → 发 PyPI（仅 release 事件）
        └─ Docker：buildx 多架构（amd64+arm64）→ 推 ghcr.io
              tags: .../genai-bench:latest 与 :<version>
```

#### 4.3.3 源码精读

发布流水线的触发与权限：

[.github/workflows/release.yml:L1-L21](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/.github/workflows/release.yml#L1-L21) — 触发条件有两种：`release` 事件 `published`（GitHub 上点「Publish release」），或 `workflow_dispatch` 手动触发并传入 `tag` 输入。`permissions` 显式声明了 `id-token: write`（用于 PyPI 的可信发布）、`packages: write`（推 GHCR 镜像）——这是 GitHub Actions 的安全最佳实践，遵循最小权限原则，而不是默认全开。

发版前**再跑一次测试**作为最后防线：

[.github/workflows/release.yml:L43-L50](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/.github/workflows/release.yml#L43-L50) — 注意这里用 `python -m pytest ... --cov-report=term-missing` 且同样 `--fail-under=93`，还设了 `timeout-minutes: 10`。发版通道的测试与 CI 的测试目的不同：CI 是「每次改动都验证」，发版是「打包前再确认一次交付物可信」，并加了超时防止挂死。

最关键的是**双源版本校验**，这是整个发布流程的灵魂：

[.github/workflows/release.yml:L52-L67](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/.github/workflows/release.yml#L52-L67) — 第一步从 Release tag 取版本：若是 release 事件取 `github.event.release.tag_name`，否则取手动输入；再用 `${TAG#v}` 这个 shell 参数展开剥掉前导 `v`（`v0.0.5` → `0.0.5`），写入 `$GITHUB_OUTPUT`。

[.github/workflows/release.yml:L69-L77](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/.github/workflows/release.yml#L69-L77) — 第二步用一段内联 Python（`tomllib` 读 `pyproject.toml`）取出 `[project].version`，这是版本号的**真正数据源**（[pyproject.toml:L3](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/pyproject.toml#L3) 当前为 `0.0.5`）。

[.github/workflows/release.yml:L79-L87](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/.github/workflows/release.yml#L79-L87) — 第三步比较两者，不等则 `exit 1`。这把 u1-l1 讲的「单一数据源」落到了发布闭环：日常版本来自 `pyproject.toml`，但发版动作由 Git tag 驱动，二者必须一致，防止「tag 打成 v0.0.5、代码里还是 0.0.4」的错发。当前仓库的 tag `v0.0.5` 与 `pyproject.toml` 的 `0.0.5` 正好匹配，最近的提交记录也有 `[release] Update version to 0.0.5`。

校验通过后，先发 PyPI 再发 Docker：

[.github/workflows/release.yml:L89-L96](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/.github/workflows/release.yml#L89-L96) — `uv build` 打包，`pypa/gh-action-pypi-publish` 发 PyPI，且 `if: github.event_name == 'release'` 确保只有真正的 release 事件才发包（手动 `workflow_dispatch` 不会误发到 PyPI）。

[.github/workflows/release.yml:L98-L124](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/.github/workflows/release.yml#L98-L124) — Docker 部分用 QEMU + Buildx 做 `linux/amd64,linux/arm64` 双架构镜像，登录 GHCR 后构建并推送两个 tag：`:latest` 与 `:<version>`（如 `:0.0.5`），还用 `cache-from/cache-to: type=gha` 借 GitHub Actions 缓存加速。注意 `build-args: PKG_VERSION=${{ steps.tagver.outputs.version }}` 把版本号作为构建参数传进 Dockerfile。

Dockerfile 本身用分层思路最小化镜像与缓存复用：

[Dockerfile:L1-L31](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/Dockerfile#L1-L31) — 基于 `python:3.11-slim`；先用 `apt-get` 装系统依赖（wget/curl/gcc/git/build-essential，其中 gcc/build-essential 是某些带 C 扩展的 Python 包编译所需）；再 `pipx install uv`；然后 `COPY . .` 拷代码；最后用 `ARG PKG_VERSION` 接收 release 注入的版本号，`uv version ${PKG_VERSION}` 改写、`uv pip install --system .` 装入系统环境。结尾清 apt 缓存瘦身。

[Dockerfile:L38](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/Dockerfile#L38) — `ENTRYPOINT ["genai-bench"]` 把容器入口设为 CLI 本身，于是 `docker run <image> benchmark ...` 里 `benchmark` 之后的部分就是直接传给 `genai-bench` 的参数（这在用户指南里有体现）。

`Makefile` 提供了本地复现这些动作的快捷方式：

[Makefile:L104-L110](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/Makefile#L104-L110) — `build-image` 用 `docker build`（若检测到 `nerdctl` 则优先用它）构建镜像，`push-image` 推到 `REGISTRY`。注意 [Makefile:L7](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/Makefile#L7) 的 `REGISTRY` 当前是占位符 `<secret>.ocir.io/...` 并带 `TODO(slin): replace with public docker registry` 注释，说明这是内部 OCI 仓库的遗留配置，与 release.yml 推的公开 ghcr.io 是两条路径。

用户侧的 Docker 用法写在专门文档里：

[docs/user-guide/run-benchmark-using-docker.md:L7-L17](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/docs/user-guide/run-benchmark-using-docker.md#L7-L17) — 既可 `docker pull ghcr.io/moirai-internal/genai-bench:v0.0.3` 拉官方镜像（注意文档示例里的版本号 v0.0.3 略落后于当前 pyproject 的 0.0.5，属文档待同步），也可 `docker build . -f Dockerfile -t genai-bench:dev` 本地自建。

最后回到版本号的运行时出口，把整条链路收口：

[genai_bench/version.py:L1-L3](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/genai_bench/version.py#L1-L3) — 这三行是「单一数据源」的读取端：运行时用 `importlib.metadata.version("genai-bench")` 从**已安装的发行包元数据**里取版本，而不是在代码里硬编码字符串。于是版本的完整闭环是：`pyproject.toml` 定义 → 打包时写进包元数据 → 安装后由 `version.py` 读出 → `--version` 暴露给用户；发版时再用 Git tag 与之比对，确保发出去的就是源码里写的。

#### 4.3.4 代码实践

**实践目标**：本地构建并运行 genai-bench 的 Docker 镜像，理解 ENTRYPOINT 设计。

**操作步骤**：

1. 构建镜像：`docker build . -f Dockerfile -t genai-bench:dev`（若想模拟 release 的版本注入，加 `--build-arg PKG_VERSION=0.0.5`）。
2. 验证入口与版本：`docker run --rm genai-bench:dev --version`，应输出 `genai-bench version 0.0.5`（或你注入的 PKG_VERSION）。
3. 对照 [docs/user-guide/run-benchmark-using-docker.md:L78-L110](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/docs/user-guide/run-benchmark-using-docker.md#L78-L110)，观察：因为 `ENTRYPOINT` 是 `genai-bench`，`docker run ... benchmark --api-backend ...` 中 `benchmark` 之后的参数直接成为 CLI 参数。
4. （可选，需要一个可达的推理服务）按文档示例用 `--network host` 或自建 `benchmark-network` 跑一次最小 text-to-text 基准，用 `docker logs --follow <容器ID>` 看实时 UI。

**需要观察的现象**：

- 步骤 2 输出的版本号应与 `pyproject.toml` 的 `version` 一致，印证「元数据→version.py→CLI」的链路。
- 步骤 3 若不写 `benchmark` 子命令（如直接 `docker run --rm genai-bench:dev --help`），看到的是 `genai-bench` 顶层帮助，说明 ENTRYPOINT 固定、子命令由用户传入。
- 步骤 4 在容器内产出的实验目录默认落在容器内 `/genai-bench` 下，**容器删除即丢失**；要持久化需像文档那样用 `-v $HOST_OUTPUT_DIR:$CONTAINER_OUTPUT_DIR` 挂卷并配合 `--experiment-base-dir`。

**预期结果**：镜像构建成功、`--version` 正确。**待本地验证**：步骤 4 的实际压测结果取决于你接入的推理服务；若没有服务，可仅做步骤 1–3 的构建与入口验证，同样能掌握本节要点。

#### 4.3.5 小练习与答案

**练习 1**：假设有人把 Git tag 打成了 `v0.0.5`，但忘记把 `pyproject.toml` 从 `0.0.4` 改成 `0.0.5` 就发了 Release，release.yml 会怎样？

> **答案**：在 [release.yml:L79-L87](https://github.com/sgl-project/genai-bench/blob/7fd04d8c53b1df20450019fb7eedec0c4caec24f/.github/workflows/release.yml#L79-L87) 的「Verify versions match」步骤，tag 版本 `0.0.5` 与 pyproject 版本 `0.0.4` 不等，`exit 1` 使整个 job 失败，PyPI 不会发包、Docker 镜像不会推送。这正是双源校验的价值：把「tag 与代码版本不一致」的错发挡在不可逆的发布动作之前。

**练习 2**：`genai_bench/version.py` 为什么用 `importlib.metadata.version("genai-bench")` 而不是直接写 `__version__ = "0.0.5"`？

> **答案**：直接硬编码会造成「两个地方都要改」——改了 `pyproject.toml` 还得记得改 `version.py`，极易漏改导致版本不一致。用 `importlib.metadata` 从已安装包的元数据读取，就保证了 `pyproject.toml` 是唯一数据源：打包工具会把 `pyproject.toml` 的版本写进包元数据，运行时再读出来，整个链路只有一处定义。这就是 u1-l1 强调的单一数据源原则在版本管理上的落地。

**练习 3**：release.yml 里发 PyPI 的步骤有 `if: github.event_name == 'release'`，但 Docker 推送步骤没有这个条件。这意味着什么？

> **答案**：这意味着手动用 `workflow_dispatch` 触发时，**不会发 PyPI 包，但仍会构建并推送 Docker 镜像**。这是一种有意的安全分级：PyPI 版本号永久占用、不可撤销，所以只在真正的 release 事件才发；而 Docker `:latest`/`:<version>` 标签可以覆盖重推，风险较低，允许手动触发用于测试镜像构建。不过要注意：手动触发推的 `:<version>` tag 若与已发布的正式版同名，会覆盖该版本的镜像。

## 5. 综合实践

把本讲三块内容串起来，做一次「从改代码到验证交付」的完整走查（在自己的 fork 中进行，**不要改动上游源码**）：

1. **改一行、验一遍**：在某处加一条无害的注释或日志（不改变行为），然后依次执行 `make format` → `make lint` → `make test`，确认本地三关全过、覆盖率仍 ≥ 93%。体会 pre-commit / ruff / mypy / pytest 各自把的是哪道关。
2. **解读 CI**：打开 `.github/workflows/ci.yml`，画一张表，把每个 step 归入 test / lint / build，并标注它使用的 Python 版本、是否卡覆盖率门槛。再回答：为什么 `build` job 要 `needs: [test, lint]`？
3. **构建镜像并核对版本闭环**：`docker build . -f Dockerfile -t genai-bench:dev --build-arg PKG_VERSION=0.0.5`，然后 `docker run --rm genai-bench:dev --version`。对照 `pyproject.toml`、`version.py`、`release.yml` 的版本校验三处，画出「版本号在哪里定义、在哪里读取、在哪里比对」的数据流图。
4. **追踪发版门禁**：阅读 `release.yml`，列出从「Release 发布」到「镜像推送成功」之间所有会让流程失败的检查点（测试失败、覆盖率不足、版本不一致等），体会「宁可失败也不要发错」的设计。

完成后再回看 u8-l1 的主流程：你会更清楚，正因为有这样一套测试 + CI + 发版护栏，那条 `benchmark` 七段流水线才敢于在每次改动后自动重新验证、可信地发布新版本。

## 6. 本讲小结

- **测试组织**：`tests/` 镜像 `genai_bench/` 的包结构，72 个 `test_*.py` 按子系统分目录；`conftest.py` 用 `autouse` fixture 在每个测试后还原 `OpenAIUser` 类属性、清空 `warning_once` 缓存，杜绝跨测试状态泄漏；主流测试范式是「`MagicMock(spec=...)` 造输入 → 调真实生产代码 → 断言数值」。
- **CI 守门**：`ci.yml` 分 test（3 版本矩阵 + 93% 覆盖率门槛）、lint（ruff format/check + mypy）、build（`uv build`，依赖前两者）三段并行/串行 job；`.coveragerc` 把渲染层（ui/logging/报告脚本）排除，让 93% 聚焦核心逻辑。
- **代码质量工具链**：ruff 规则集中声明在 `pyproject.toml`，mypy 配置在 `mypy.ini`（放宽联合类型误报），pre-commit 把 ruff/mypy 前置到 commit 瞬间。
- **发布流水线**：`release.yml` 在发版前再跑一次测试，核心是「Git tag 版本 == pyproject.toml 版本」的双源校验，通过后发 PyPI（仅 release 事件）并推多架构 Docker 镜像到 ghcr.io。
- **版本管理闭环**：`pyproject.toml` 是单一数据源 → 打包写进元数据 → `version.py` 用 `importlib.metadata` 运行时读出 → CLI `--version` 暴露；发版时 Git tag 再与之比对，形成可信闭环。
- **Docker 运行**：`Dockerfile` 基于 `python:3.11-slim` 分层构建、`ENTRYPOINT ["genai-bench"]` 让容器即 CLI；`Makefile` 的 `build-image`/`test`/`lint`/`format` 是本地开发快捷方式。

## 7. 下一步学习建议

至此，整本 genai-bench 学习手册（U1–U8，30 篇讲义）已完整覆盖从「认识项目」到「主流程编排」再到「测试与发布」的全链路。建议的后续方向：

- **动手扩展**：结合 u8-l3 的扩展指南与本讲的测试体系，尝试新增一个最小后端 User 或场景，并**为它补一个 `tests/` 下的测试**，跑 `make test` 确认覆盖率不掉，体会「功能 + 测试」配套的开发节奏。
- **深读 CI/发布源文件**：把 `.github/workflows/` 下两个 YAML 当作「GitHub Actions 的实战教材」逐行读，对照官方文档理解 `matrix`、`needs`、`permissions`、`GITHUB_OUTPUT`、`buildx` 多架构等概念。
- **回看全局**：用本讲建立的「测试—CI—发布」护栏视角，重新审视 u8-l1 的 `benchmark` 主流程——你会理解为什么那条流水线敢把元数据先写盘、敢在报告阶段从磁盘重读：因为有这套自动化质量门兜底，每次重跑与重发布都是可信的。
- **关注版本与文档同步**：当前 `docs/user-guide/run-benchmark-using-docker.md` 的镜像 tag 示例（v0.0.3）与代码版本（0.0.5）存在漂移，这是一个真实可练手的「文档维护」小任务。
