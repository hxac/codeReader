# 测试与 CI 体系

## 1. 本讲目标

本讲是「特定模型落地与二次开发」单元中面向**工程质量**的一讲。前面十几讲我们读了大量运行期源码（worker、model runner、注意力、算子、KV 传输），但一个能在昇腾 NPU 上长期演进的插件，光有运行逻辑还不够，还必须有一套**可重复、可分层、可被自动化流水线驱动**的测试与 CI 体系来兜底。

读完本讲，你应当能够：

1. 说出 vllm-ascend 的 **UT / E2E / Nightly** 三层测试各自放在哪个目录、解决什么问题。
2. 看懂 `pyproject.toml` 里 `e2e_model` / `e2e_coverage` 两个 pytest marker 的作用，并能在本地用 `pytest` 跑起单个 UT 文件、读懂输出。
3. 理解 PR 流水线 `pr_test.yaml` 是如何用 `select_tests.py` + `test_config.yaml` 做「按改动文件挑选测试」的选择性调度，而不是每次都跑全量。
4. 描述本次更新（#13442）新增的 **A5 夜间测试流水线** `schedule_nightly_test_a5.yaml` 相对普通 PR 测试的差异，以及它和 `nightly_config.yaml` 的关系。
5. 把一条新的运行期特性（以 #12852 的「layerwise 缓冲复用」为代表）落到对应的单测文件里，并判断它在 CI 中属于哪一层。

本讲承接 [u1-l3 环境准备与安装构建](u1-l3-build-and-install.md)（构建环境变量、无 NPU 的 UT 环境），并和 [u10-l7 分层 prefill KV 缓冲复用](u10-l7-layerwise-prefill-kv-buffer-reuse.md) 的特性单测互相印证。

## 2. 前置知识

- **pytest**：Python 最主流的测试框架。vllm-ascend 的测试都用 `unittest.TestCase` 或 `pytest` 函数式写法写成，统一用 `pytest` 命令运行。你需要知道「文件级 / `::用例名` 级」两种运行粒度，以及 `@pytest.mark.xxx` 打标记的用法。
- **GitHub Actions（GHA）**：本仓库托管在 GitHub，所有 CI 都用 `.github/workflows/*.yaml` 描述。你需要大致了解 `job` / `step` / `needs`（依赖编排）/ `matrix`（矩阵展开）/ `workflow_dispatch`（手动触发）这几个概念。
- **测试分层**：从下到上一般是「单元测试 UT → 端到端测试 E2E → 夜间/周回归 Nightly」。越往下越快、越不依赖硬件；越往上越接近真实部署、越需要真实 NPU。
- **选择性测试（selective testing）**：PR 不必每次跑全部测试，而是「改了哪些源码 → 跑哪些测试」。这是大型仓库节省 CI 算力的关键手段，由一张「源码路径 → 测试路径」的映射表驱动。

## 3. 本讲源码地图

本讲涉及的文件分四组：

| 文件 | 作用 |
| --- | --- |
| `tests/ut/` | 单元测试目录，按模块分目录（attention / distributed / ops / worker …），**不依赖 NPU 即可跑**的纯逻辑测试都在这里 |
| `tests/e2e/` | 端到端测试目录，按卡数与场景分（`pull_request/{one,two,four,eight}_card`、`nightly`、`weekly`），需要真实 NPU |
| `AGENTS.md` | 贡献者指南，定义 UT/ST/Nightly 的目录约定与本地运行命令 |
| `pyproject.toml` | 注册 pytest 的 `e2e_model` / `e2e_coverage` 两个 marker |
| `.github/workflows/pr_test.yaml` | **PR 流水线**：pre-commit → 选择测试 → 跑选中测试 → CI gate |
| `.github/workflows/schedule_nightly_test_a5.yaml` | **A5 夜间流水线**（#13442 新增）：手动/定时触发，跑 A5 硬件上的多节点/单节点 E2E |
| `.github/workflows/scripts/select_tests.py` | 测试选择脚本：根据 PR 改动文件挑出要跑的测试并分配到 runner |
| `.github/workflows/scripts/test_config.yaml` | 「源码路径 → 测试路径」映射表，驱动 `select_tests.py` |
| `.github/workflows/configs/nightly_config.yaml` | 夜间测试矩阵配置（A2/A3/310P/A5），A5 节为本次新增占位 |

代表性单测文件（本讲会精读）：

| 文件 | 作用 |
| --- | --- |
| `tests/ut/ops/test_prepare_finalize.py` | FusedMoE 的 prepare/finalize（MC2/All2All/AllGather 三种通信）单测，纯 CPU mock，是「典型 UT」范本 |
| `tests/ut/distributed/ascend_store/test_layerwise_cache_layout.py` | #12852 layerwise 缓冲复用的布局规划单测，是「新特性如何落 UT」的范本 |

## 4. 核心概念与源码讲解

### 4.1 测试分层与目录约定

#### 4.1.1 概念说明

vllm-ascend 把测试分成三层，每一层对应不同的「能跑多快、需要什么硬件、覆盖多大范围」：

- **Unit Test（UT，单元测试）**：放在 `tests/ut/`，覆盖单个模块的核心逻辑与边界条件。绝大多数 UT **不需要真实 NPU**——它们要么是纯 Python 逻辑，要么用 `unittest.mock` 把设备通信、前向上下文 mock 掉。这让 UT 可以在普通 CPU CI runner 上快速跑完，是每个 PR 的「快速反馈环」。
- **System Test / End-to-End（E2E，端到端测试）**：放在 `tests/e2e/`，验证真实模型从加载到生成的整条链路。E2E 需要**真实 NPU 硬件**和真实模型权重，因此又按卡数拆分目录：`one_card` / `two_card` / `four_card` / `eight_card`。
- **Nightly（夜间/回归测试）**：放在 `tests/e2e/nightly/`，跑更大模型、更长序列、benchmark 与精度回归，按夜间定时或 `/nightly` 指令触发，对应多个流水线（A2/A3/310P，本次新增 A5）。

这套约定写在了贡献者指南 `AGENTS.md` 的 Testing 小节里。

#### 4.1.2 核心流程

UT 子目录的命名直接暗含了**路由规则**（这点在 4.3 会展开）：默认目录走 CPU runner，带硬件后缀的子目录走对应 NPU runner。

```
tests/ut/
├── attention/          # CPU UT（默认）
│   └── a2/             # → A2 NPU x1
├── distributed/
│   └── ascend_store/   # KV 传输/连接器 UT，CPU mock
├── ops/                # 算子 UT（含 test_prepare_finalize.py）
├── worker/、sample/、lora/、quantization/ …
└── _310p/              # 310P 专属 UT

tests/e2e/
├── pull_request/       # PR 触发的 E2E
│   ├── one_card/  two_card/  four_card/  eight_card/
├── nightly/            # 夜间 E2E + benchmark
└── weekly/、doctests/、models/、vllm_interface/、prompts/
```

UT/E2E 的分层与目录约定见 [AGENTS.md:L76-L91](https://github.com/vllm-project/vllm-ascend/blob/7201c97a61a17425b558b6b5e53ab0d30ae8151d/AGENTS.md#L76-L91)——它明确写出 UT 在 `tests/ut/`、ST/E2E 在 `tests/e2e/`、Nightly benchmark 在 `tests/e2e/nightly/`，并要求新功能补 happy path + 失败路径、bug 修复补回归测试。

#### 4.1.3 源码精读

`AGENTS.md` 的「Running Tests」一节给出了本地运行命令模板，是本讲的「命令字典」：

- [AGENTS.md:L93-L105](https://github.com/vllm-project/vllm-ascend/blob/7201c97a61a17425b558b6b5e53ab0d30ae8151d/AGENTS.md#L93-L105)：示范了三种粒度——跑整个 UT 文件、跑单个用例（`::test_xxx`）、跑需要 NPU 的 E2E 用例（路径带卡数目录）。

贡献者指南还在「Quick Start」里要求提交前跑 lint 与本地测试：

- [AGENTS.md:L389-L409](https://github.com/vllm-project/vllm-ascend/blob/7201c97a61a17425b558b6b5e53ab0d30ae8151d/AGENTS.md#L389-L409)：`pip install -e .[dev]` → `pytest tests/` → `ruff` → `bash format.sh ci` → push 到自己的 fork。

E2E 按卡数分目录的设计，是为了让 CI 把不同卡数需求的用例路由到不同规格的 NPU runner 上（详见 4.3 的 `test_config.yaml` 注释）。

#### 4.1.4 代码实践

1. **实践目标**：建立对三层目录的肌肉记忆。
2. **操作步骤**：
   - 在仓库根目录执行 `ls -d tests/ut/*/` 列出所有 UT 模块目录。
   - 执行 `ls -d tests/e2e/pull_request/*/` 列出 E2E 的卡数目录。
   - 任选 `tests/ut/ops/` 下一个文件，看它的 import 是否依赖真实 NPU。
3. **需要观察的现象**：UT 目录里会出现 `a2/`、`a3_2/`、`310p/` 等带硬件后缀的子目录，这些是要上 NPU 的；其余默认目录是 CPU 可跑的。
4. **预期结果**：`tests/e2e/pull_request/` 下正好是 `eight_card / four_card / one_card / two_card` 四个目录。
5. 命令本身只是 `ls`，无运行结果风险。

#### 4.1.5 小练习与答案

- **练习 1**：一个验证 `AscendSampler` 采样数学正确性、且全程用 `torch.randn` 造数据、不发起任何 HCCL 通信的测试，应该放在哪一层？为什么？
  - **答案**：放在 `tests/ut/sample/`（UT 层）。它不依赖真实 NPU 与分布式，用 mock/假数据即可验证纯逻辑，应进 CPU 快速反馈环。
- **练习 2**：为什么 E2E 测试要按 `one_card / two_card / four_card / eight_card` 分目录，而不是统一放一处再用参数区分？
  - **答案**：因为不同卡数对应不同规格的 NPU runner，分目录让 CI 的路由脚本（`test_config.yaml` 的 `runner_mapping`）能直接按路径正则把用例分派到「1 卡 / 2 卡 / 4 卡 / 8 卡」的 runner，避免用参数在运行期动态切卡带来的资源调度复杂度。

---

### 4.2 pytest 配置、marker 与本地运行

#### 4.2.1 概念说明

pytest 通过「marker（标记）」给测试用例贴元数据标签，运行时可以按标签筛选。vllm-ascend 在 `pyproject.toml` 的 `[tool.pytest.ini_options]` 里注册了两个自定义 marker：`e2e_model`（标注 E2E 用例跑的是哪个模型）和 `e2e_coverage`（标注覆盖了哪些维度：架构 / 特性 / 并行 / 部署 / 硬件 / 量化 / 图模式）。这两个标签主要用于 E2E 测试的**分组统计与按需挑选**，不改变用例本身的运行行为。

需要强调一个常被忽略的细节：pytest 默认会对「未注册的 marker」抛 warning；只有写进 `pyproject.toml` 的 marker 才是「合法」的，这也是 marker 必须集中注册的原因。

#### 4.2.2 核心流程

pytest 配置生效流程：

1. pytest 启动，读取仓库根目录的 `pyproject.toml`。
2. 解析 `[tool.pytest.ini_options]` 中的 `markers` 列表，把它们登记为合法 marker。
3. 测试文件里用 `@pytest.mark.e2e_model("qwen3-32b")` 给用例贴标签。
4. 运行时可用 `-m e2e_model` 之类的表达式筛选，或被 CI 脚本读取用于统计。

#### 4.2.3 源码精读

marker 注册就这几行，却是「为什么 `e2e_model` 不报警告」的根因：

- [pyproject.toml:L93-L97](https://github.com/vllm-project/vllm-ascend/blob/7201c97a61a17425b558b6b5e53ab0d30ae8151d/pyproject.toml#L93-L97)：注册 `e2e_model`（E2E 测试模型标识）与 `e2e_coverage`（E2E 覆盖维度：arch/feature/parallel/deploy/hardware/quantization/graph_mode）。

同一个 `pyproject.toml` 还配了 ruff（line-length=120、选 E/F/UP/B/SIM/I/G 规则集）与 pymarkdown（markdown lint），它们是 CI 里 `pre-commit` 阶段会跑的格式闸门：

- [pyproject.toml:L50-L92](https://github.com/vllm-project/vllm-ascend/blob/7201c97a61a17425b558b6b5e53ab0d30ae8151d/pyproject.toml#L50-L92)：ruff lint 规则与 ignore 列表，以及 `[tool.ruff.format]`、`[tool.pymarkdown]` 配置。

#### 4.2.4 代码实践

1. **实践目标**：本地用 `pytest` 跑一个真实 UT 文件，并解释输出含义。
2. **操作步骤**（在装好 `vllm_ascend` 与 `torch` 的环境里）：
   ```bash
   pytest -sv tests/ut/ops/test_prepare_finalize.py
   ```
   只跑单个用例：
   ```bash
   pytest -sv tests/ut/ops/test_prepare_finalize.py::TestPrepareAndFinalize::test_all2all_prepare_finalize
   ```
3. **需要观察的现象**：每个用例一行 `PASSED`/`FAILED`，`-sv` 会显示用例名与捕获的打印；末尾有 `N passed in Xs` 汇总。
4. **预期结果**：该文件 4 个用例全部 `PASSED`（它们用 `unittest.mock` 把 TP/DP 通信 mock 掉，不需要真实 NPU）。
5. **待本地验证**：若环境未安装 `vllm_ascend`/`torch`/`vllm`，会在收集阶段 `ImportError`；此时可只阅读用例代码理解断言，跳过实际运行。

#### 4.2.5 小练习与答案

- **练习 1**：如果你给一个用例加了 `@pytest.mark.my_tag` 但没在 `pyproject.toml` 注册，会发生什么？
  - **答案**：pytest 会抛 `PytestUnknownMarkWarning`，提示该 marker 未注册。功能上用例仍会跑，但失去了「合法标签」的可筛选/统计能力，CI 里 `-m my_tag` 筛选也可能行为异常。
- **练习 2**：`e2e_coverage` 标注的 `graph_mode` 维度，大致对应前面哪一讲的内容？
  - **答案**：对应 [u8-l3 ACL Graph](u8-l3-aclgraph.md) 讲的 FULL 与 PIECEWISE 图模式——E2E 用例用 `e2e_coverage` 的 `graph_mode` 维度标记自己覆盖了哪种图模式，便于按维度统计覆盖。

---

### 4.3 PR 测试选择：select_tests.py + test_config.yaml

#### 4.3.1 概念说明

vllm-ascend 的全量测试（尤其 E2E）需要大量 NPU 算力，不可能每个 PR 都跑全部。因此它实现了**选择性测试（selective testing）**：根据 PR 改了哪些源码文件，挑出「相关的」UT 和 E2E 去跑。这套机制由两件东西驱动：

- **映射表 `test_config.yaml`**：一张「源码路径 → 测试路径」的模块清单，每个模块声明 `source_file_dependencies`（改了哪些源码会触发）和 `tests`（要跑哪些测试），以及 `runner_mapping`（测试路径 → runner 类型的正则）。
- **选择脚本 `select_tests.py`**：读取 PR 的 git diff，匹配 `test_config.yaml` 里的模块，收集测试、按 runner 分组、再按时长负载均衡切分，最后输出 `test_groups` / `has_tests` / `matched_modules`。

这是本讲最核心的「CI 编排」机制：它把「改一行源码」翻译成「跑哪些测试、在哪台机器上跑」。

#### 4.3.2 核心流程

`select_tests.py` 文档字符串里把流水线总结为六步（PR 驱动模式）：

```
1. Diff       → 用 git 拿到 PR 改动文件
2. Match      → 用 test_config.yaml 把改动文件匹配到「受影响模块」
3. Collect    → 收集这些模块配置的测试路径（目录展开成单文件）
4. Route      → 用 runner_mapping 把每个测试文件路由到对应 runner
5. Partition  → 按估计耗时把测试组切分到并行 runner，做负载均衡
6. Output     → 写出 test_groups / has_tests / matched_modules
```

其中还有两个优化：**Test-only 优化**（PR 只改 `tests/` 不改源码时，只跑常驻的 `default_cpu_ut` 模块 + 改动的测试文件本身）；**Bisect-tool 优化**（PR 只动 `tools/bisect` 时跳过常驻模块，只跑匹配模块）。

还有一个关键常驻模块 `default_cpu_ut`：它 `optional: false`、`cpu_only: true`、`tests: [tests/ut]`——意味着**每个 PR 都会无条件跑全部 CPU UT**，这是质量底线。

#### 4.3.3 源码精读

路由约定的「注释字典」就在 `test_config.yaml` 开头，是读懂整套分发的钥匙：

- [test_config.yaml:L13-L24](https://github.com/vllm-project/vllm-ascend/blob/7201c97a61a17425b558b6b5e53ab0d30ae8151d/.github/workflows/scripts/test_config.yaml#L13-L24)：写明 UT 默认走 CPU、`<module>/a2/` 走 A2 x1、`a2_2`/`a3_2`/`a3_4`/`310p` 走对应 NPU；E2E 按卡数目录匹配 runner，`*_310p.py` 单独路由到 310P。

常驻 CPU UT 模块，每个 PR 必跑：

- [test_config.yaml:L43-L48](https://github.com/vllm-project/vllm-ascend/blob/7201c97a61a17425b558b6b5e53ab0d30ae8151d/.github/workflows/scripts/test_config.yaml#L43-L48)：`default_cpu_ut` 模块 `optional: false`、`cpu_only: true`、`tests: [tests/ut]`。

`distributed` 模块——正是 `ascend_store` layerwise 单测归属的模块：

- [test_config.yaml:L216-L227](https://github.com/vllm-project/vllm-ascend/blob/7201c97a61a17425b558b6b5e53ab0d30ae8151d/.github/workflows/scripts/test_config.yaml#L216-L227)：只要改了 `vllm_ascend/distributed`（含 `kv_transfer/kv_pool/ascend_store/`），就触发 `tests/ut/distributed` 等测试。

选择脚本自身的六步流水线文档：

- [select_tests.py:L31-L37](https://github.com/vllm-project/vllm-ascend/blob/7201c97a61a17425b558b6b5e53ab0d30ae8151d/.github/workflows/scripts/select_tests.py#L31-L37)：Diff → Match → Collect → Route → Partition → Output。
- [select_tests.py:L96-L98](https://github.com/vllm-project/vllm-ascend/blob/7201c97a61a17425b558b6b5e53ab0d30ae8151d/.github/workflows/scripts/select_tests.py#L96-L98)：`DEFAULT_CPU_UT_MODULE = "default_cpu_ut"`，即 test-only 优化里唯一保留的常驻模块。

#### 4.3.4 代码实践

1. **实践目标**：判断一个改动会触发哪些测试、归到哪个模块。
2. **操作步骤**：
   - 阅读 `test_config.yaml` 的模块清单，找到 `source_file_dependencies` 包含你改动路径的模块。
   - 例如改动 `vllm_ascend/distributed/kv_transfer/kv_pool/ascend_store/layerwise_cache_layout.py`，命中 `distributed` 模块（依赖 `vllm_ascend/distributed`）。
   - 该模块的 `tests` 含 `tests/ut/distributed`，于是 `tests/ut/distributed/ascend_store/test_layerwise_cache_layout.py` 会被收集。
3. **需要观察的现象**：被收集的 UT 既会随 `default_cpu_ut`（全量 CPU UT）跑一遍，也会随命中的 `distributed` 模块跑一遍——但因为它们都是 CPU UT，路由到同一类 runner。
4. **预期结果**：layerwise 单测无需 NPU 即可在 PR 的 CPU runner 上执行。
5. 命令为纯阅读，无运行风险。

#### 4.3.5 小练习与答案

- **练习 1**：某 PR 只新增了一个 UT 文件、没改任何源码，`select_tests.py` 会怎么处理？
  - **答案**：触发 test-only 优化——跳过模块匹配，只跑常驻的 `default_cpu_ut` 模块 + 这个新加的测试文件本身，避免因 `optional: false` 模块触发大范围回归。
- **练习 2**：为什么 `default_cpu_ut` 要设成 `optional: false`？
  - **答案**：它是质量底线，要求每个 PR 都跑全部 CPU UT，确保没有任何 PR 让既有 UT 静默回归。`optional: false` 表示「不依赖匹配，无条件触发」。

---

### 4.4 PR 流水线 pr_test.yaml 全景

#### 4.4.1 概念说明

`.github/workflows/pr_test.yaml`（workflow 名 `E2E`）是每个 PR 触发的主流水线。它不是「一个 job 跑所有事」，而是一条**有依赖关系的 job 链**：先做静态检查与测试选择，再按选择结果跑测试，最后用一个汇总 gate job 决定整条流水线是否通过——这个 gate job 的名字稳定，可直接配进 GitHub 分支保护规则，而不用追踪每个矩阵 job 名。

注意触发条件：它只在 PR 打了 `ready` 标签（或 `labeled` 事件且标签是 `ready`）时才真正跑测试 job，否则只跑轻量检查。这是一种「草案 PR 不烧算力」的常见做法。

#### 4.4.2 核心流程

`pr_test.yaml` 的 job 链：

```
pre-commit ──┬─> select-tests ──> ensure-csrc-cache ──> run-selected-tests ──┐
             │                                                                ├─> ci-gate
             └──────────────────────────────────────────────────────────────►│
                                            run-selected-tests-upstream ─────┘
                                            recommend-tests-from-coverage
                                            generate-hitest
                                            analyze-failure-report
```

各 job 职责：

- `pre-commit`：校验 PR 标题前缀（`[BugFix]/[CI]/[Feature]…`）、读「已验证的 vLLM 版本」、跑 pre-commit（含 gitleaks 密钥扫描、markdownlint、mypy）。
- `select-tests`：调用 `select_tests.py`（4.3 讲的脚本）输出要跑哪些测试。
- `ensure-csrc-cache`：确保 C++ 内核（csrc）的编译缓存就绪，避免每个 PR 重复编译。
- `run-selected-tests`：在 NPU runner 上跑选中的测试，矩阵展开「vLLM main commit」与「vLLM release tag」两个版本。
- `ci-gate`：汇总 `pre-commit`/`select-tests`/`run-selected-tests` 的结果，全过才放行——这是配进分支保护的**唯一稳定状态检查名**。
- `analyze-failure-report`：失败时收集日志、用覆盖率推荐相关测试、生成报告。

#### 4.4.3 源码精读

触发条件与并发控制（同 PR 新推送会取消旧运行）：

- [pr_test.yaml:L18-L41](https://github.com/vllm-project/vllm-ascend/blob/7201c97a61a17425b558b6b5e53ab0d30ae8151d/.github/workflows/pr_test.yaml#L18-L41)：`on: pull_request` 的 `opened/synchronize/reopened/labeled`、目标分支过滤、`cancel-in-progress: true`。

`select-tests` job 调用选择脚本的核心步骤：

- [pr_test.yaml:L274-L282](https://github.com/vllm-project/vllm-ascend/blob/7201c97a61a17425b558b6b5e53ab0d30ae8151d/.github/workflows/pr_test.yaml#L274-L282)：装 `regex` 库后执行 `python3 .github/workflows/scripts/select_tests.py --diff-base <base_sha>`，把 PR diff 喂给选择脚本。

`run-selected-tests` job 的触发门槛（必须打 `ready` 标签、必须有测试、csrc 缓存就绪）：

- [pr_test.yaml:L323-L348](https://github.com/vllm-project/vllm-ascend/blob/7201c97a61a17425b558b6b5e53ab0d30ae8151d/.github/workflows/pr_test.yaml#L323-L348)：`needs` 依赖与 `if` 条件，矩阵跑 main commit 与 release tag 两个 vLLM 版本，复用 `_selected_tests.yaml`。

`ci-gate` 汇总 job——分支保护挂这里：

- [pr_test.yaml:L469-L506](https://github.com/vllm-project/vllm-ascend/blob/7201c97a61a17425b558b6b5e53ab0d30ae8151d/.github/workflows/pr_test.yaml#L469-L506)：检查 `pre-commit`/`select-tests` 必须是 success/skipped，若有测试则 `run-selected-tests` 必须 success，否则 `exit 1`。

PR 标题前缀校验（这也是 AGENTS.md 里 PR title 规范的执行点）：

- [pr_test.yaml:L53-L78](https://github.com/vllm-project/vllm-ascend/blob/7201c97a61a17425b558b6b5e53ab0d30ae8151d/.github/workflows/pr_test.yaml#L53-L78)：正则 `\[(BugFix|Performance|Test|CI|Feature|Doc|Misc|Community|Refactor)\]`，不匹配直接报错。

#### 4.4.4 代码实践

1. **实践目标**：把一条 PR 从推送到放行的 CI 旅程说清楚。
2. **操作步骤**：
   - 阅读 `pr_test.yaml` 的 job 列表与每个 job 的 `needs`/`if`。
   - 模拟一次「改了 `worker.py`」的 PR：`pre-commit` 过 → `select-tests` 命中 `worker` 相关模块 → `run-selected-tests` 在 NPU 上跑 → `ci-gate` 汇总。
3. **需要观察的现象**：若 PR 未打 `ready` 标签，`run-selected-tests` 因 `if` 不满足而 skip，`ci-gate` 仍可对 `pre-commit`/`select-tests` 放行。
4. **预期结果**：合并前 `ci-gate` 必须是 success，分支保护规则就配在 `ci-gate` 这个名字上。
5. 纯阅读工作流文件，无运行风险。

#### 4.4.5 小练习与答案

- **练习 1**：为什么分支保护要挂在 `ci-gate` 而不是 `run-selected-tests`？
  - **答案**：因为 `run-selected-tests` 是矩阵 job，名字会随 vLLM 版本矩阵变化，且在「没有测试」或「未打 ready 标签」时会 skip；而 `ci-gate` 是单一稳定名字，统一汇总所有前置 job 结果，适合作为稳定的状态检查。
- **练习 2**：`pr_test.yaml` 里 `select-tests` job 为什么要先 `git rebase "$BASE_SHA"`？
  - **答案**：为了让测试选择、缓存生产、缓存消费三方都基于同一个不可变 base SHA，避免「移动的 base 分支」导致缓存 key 与实际代码不一致。

---

### 4.5 A5 夜间测试流水线 schedule_nightly_test_a5.yaml

#### 4.5.1 概念说明

#13442（提交 `d3a301fdb`）新增了面向 **A5（Atlas 800 A5）硬件**的夜间测试流水线 `schedule_nightly_test_a5.yaml`（workflow 名 `Nightly-A5`）。它和 PR 流水线 `pr_test.yaml` 是两套完全不同的节奏：

| 维度 | `pr_test.yaml`（E2E） | `schedule_nightly_test_a5.yaml`（Nightly-A5） |
| --- | --- | --- |
| 触发 | 每个 PR（push/synchronize/label） | `workflow_dispatch`（手动/定时指令），可选 `/nightly` 命令 |
| 目的 | 防止 PR 引入回归 | 在新硬件（A5）上跑大模型、回归与 benchmark |
| 测试来源 | `select_tests.py` 按 diff 动态挑选 | `nightly_config.yaml` 的 A5 矩阵静态声明 |
| 硬件 | A2/A3/310P/310P-x4 | 专用 A5 runner（`linux-aarch64-a5-0` 等） |
| 编排 | 单仓库内多 job 链 + ci-gate | 镜像构建 + 多节点/单节点矩阵展开 |

一句话：**PR 测试是「改什么跑什么」的快速门禁；A5 夜间是「定时全量回归 + 新硬件适配验收」的长周期门禁。**

#### 4.5.2 核心流程

`schedule_nightly_test_a5.yaml` 的 job 链：

```
parse-trigger ──┐
                ├─> setup-vars ──> build-image ──┬─> multi-node-tests ──> single-node-tests
                └────────────────────────────►(should_run 控制每个矩阵项是否执行)
clear-pre-logs (after multi-node) | merge-benchmark-artifacts (after all)
```

关键设计：

- **`workflow_dispatch` 带输入参数**：可选 `vllm_ascend_branch`（测哪个分支）、`test_cases`（跑哪些用例，支持 `all` 或逗号分隔的名字）、`vllm_ascend_ref`（PR commit SHA，支持对未合并 PR 跑夜间）、`build_type`（daily/release）、`cann_version`。
- **`parse-trigger`**：把 `test_cases` 解析成 `filter`，决定每个矩阵项 `should_run`。
- **`setup-vars`**：读 `nightly_config.yaml` 的 A5 矩阵（`a5.multi_node.test_config` / `a5.single_node.test_config`），输出给下游矩阵。
- **`build-image`**：构建 A5 专用 nightly 镜像（`_nightly_image_build.yaml`，target=a5）。
- **`multi-node-tests` / `single-node-tests`**：复用 `_e2e_nightly_multi_node.yaml` / `_e2e_nightly_single_node.yaml`，按矩阵跑，每个矩阵项用 `should_run` 表达式判断是否执行。
- **`merge-benchmark-artifacts`**：把所有 benchmark 结果合并成 `nightly-a5.zip`。

#### 4.5.3 源码精读

workflow 名与触发方式（注意是 `workflow_dispatch`，不是 PR 触发）：

- [schedule_nightly_test_a5.yaml:L18-L66](https://github.com/vllm-project/vllm-ascend/blob/7201c97a61a17425b558b6b5e53ab0d30ae8151d/.github/workflows/schedule_nightly_test_a5.yaml#L18-L66)：`name: Nightly-A5`、`on: workflow_dispatch` 及一组输入（分支、用例、PR ref、build_type、cann_version）。

读取 A5 测试矩阵——这是 #13442 把 A5 接入既有夜间框架的接入点：

- [schedule_nightly_test_a5.yaml:L161-L168](https://github.com/vllm-project/vllm-ascend/blob/7201c97a61a17425b558b6b5e53ab0d30ae8151d/.github/workflows/schedule_nightly_test_a5.yaml#L161-L168)：`MATRIX_OUTPUTS` 指向 `a5.multi_node.test_config` 与 `a5.single_node.test_config`，调 `resolve_nightly_tests.py --mode=matrix` 把 `nightly_config.yaml` 转成 GitHub Actions 矩阵。

镜像构建与多/单节点矩阵展开：

- [schedule_nightly_test_a5.yaml:L170-L191](https://github.com/vllm-project/vllm-ascend/blob/7201c97a61a17425b558b6b5e53ab0d30ae8151d/.github/workflows/schedule_nightly_test_a5.yaml#L170-L191)：`build-image` 复用 `_nightly_image_build.yaml`，target=a5。
- [schedule_nightly_test_a5.yaml:L193-L230](https://github.com/vllm-project/vllm-ascend/blob/7201c97a61a17425b558b6b5e53ab0d30ae8151d/.github/workflows/schedule_nightly_test_a5.yaml#L193-L230)：`multi-node-tests` 用 `should_run` 表达式（`filter == 'all' || contains(filter, name)`）决定每个矩阵项是否执行，runner 为 `linux-aarch64-a5-0`。

对应的矩阵源——A5 节目前是**占位（注释状态）**，等接入真实用例：

- [nightly_config.yaml:L315-L325](https://github.com/vllm-project/vllm-ascend/blob/7201c97a61a17425b558b6b5e53ab0d30ae8151d/.github/workflows/configs/nightly_config.yaml#L315-L325)：`# a5:` 段注释掉了 `multi_node`/`single_node` 的占位用例（如 `Qwen3-235B-W8A8-EPLB`、`Qwen3.5-397B-A17B-w4a8-mtp`），说明流水线已就绪、用例待补充。

#### 4.5.4 代码实践

1. **实践目标**：说清 A5 夜间流水线相对 PR 测试的差异。
2. **操作步骤**：
   - 对照 4.5.1 的差异表，逐项在两个 yaml 里找证据（触发、测试来源、硬件）。
   - 在 `nightly_config.yaml` 找到 `a5:` 段（当前注释），理解它如何被 `resolve_nightly_tests.py` 读成矩阵。
3. **需要观察的现象**：A5 流水线没有 `select_tests.py` 这类「按 diff 挑测试」的逻辑，因为夜间回归不针对某个 PR 的改动，而是按配置全量跑。
4. **预期结果**：A5 流水线即使没改任何代码，也可以通过 `workflow_dispatch` 手动触发（例如用 `/nightly <name>` 指令）。
5. 纯阅读，无运行风险。

#### 4.5.5 小练习与答案

- **练习 1**：`schedule_nightly_test_a5.yaml` 的 `test_cases` 输入为空时会发生什么？
  - **答案**：`parse-trigger` 把 `run` 设为 `false`，下游所有矩阵 job 的 `should_run` 失效、整体不跑（只解析触发、不做实测）。
- **练习 2**：A5 流水线和 PR 流水线共享了哪些可复用 workflow？
  - **答案**：A5 复用 `_nightly_image_build.yaml`（镜像构建）、`_e2e_nightly_multi_node.yaml`、`_e2e_nightly_single_node.yaml`；PR 流水线则复用 `_selected_tests.yaml`、`_ensure_csrc_cache.yaml` 等。两者各自复用一套不同的子 workflow，对应「夜间回归」与「PR 选择性测试」两种节奏。

---

### 4.6 实例：layerwise 缓冲复用单测如何落地

#### 4.6.1 概念说明

前五个模块讲的是「测试体系怎么组织」。本模块用一个真实例子收尾：#12852 的「layerwise prefill KV 缓冲复用」（详见 [u10-l7](u10-l7-layerwise-prefill-kv-buffer-reuse.md)）这条新特性，是怎么落地成单测的。它同时示范了 UT 的两个典型写法——**纯逻辑布局测试**（不需要任何 NPU，只验证「层 → 物理缓冲」的映射对不对）和**错误路径测试**（验证非法配置会被拒绝）。

这个例子也是「新特性单测归属哪一层」的标准答案：layerwise 的核心规划逻辑 `LayerwiseCacheLayout` 是纯函数式的数据布局计算，完全可以脱离 NPU 用假 `KVCacheTensor` 喂数据来测，因此天然落在 `tests/ut/distributed/ascend_store/`（CPU UT），并随 `default_cpu_ut` 和 `distributed` 模块在 PR 上跑。

#### 4.6.2 核心流程

`test_layerwise_cache_layout.py` 的测试思路：

1. 用 `_make_vllm_config(num_layers, num_shared_buffers)` 造一个假的 vLLM 配置（`kv_connector=AscendStoreConnector`、`backend=memcache`、`use_layerwise=True`）。
2. 造一组假 `KVCacheTensor`（每个对应一层 `model.layers.N.self_attn`）。
3. 调被测函数 `build_layerwise_cache_layout(...)` 或 `apply_layerwise_kv_cache_plan(...)`。
4. 断言布局字段：`has_layer_reuse`、`num_shared_buffers`、`prefetch_layer_map`、`storage_indices` 是否符合预期（复用层在 K 个缓冲间 round-robin）。
5. 另写「非法配置应被拒绝」的用例（传 0、传布尔、传错误类型，断言抛 `TypeError`/`ValueError`）。

这正好覆盖了 AGENTS.md 要求的「happy path + 失败路径」。

#### 4.6.3 源码精读

测试夹具——构造假配置与假 KV 张量：

- [test_layerwise_cache_layout.py:L19-L33](https://github.com/vllm-project/vllm-ascend/blob/7201c97a61a17425b558b6b5e53ab0d30ae8151d/tests/ut/distributed/ascend_store/test_layerwise_cache_layout.py#L19-L33)：`_make_vllm_config` 用 `SimpleNamespace` + `MagicMock` 拼出含 `kv_connector_extra_config`（`layerwise_num_shared_buffers`）的假配置。

默认布局（不复用，每层一个缓冲）：

- [test_layerwise_cache_layout.py:L122-L129](https://github.com/vllm-project/vllm-ascend/blob/7201c97a61a17425b558b6b5e53ab0d30ae8151d/tests/ut/distributed/ascend_store/test_layerwise_cache_layout.py#L122-L129)：27 层、不开启复用时，`has_layer_reuse is False`、`num_shared_buffers == 27`、`independent_layers == [0]`。

复用布局（round-robin 分配物理缓冲）——这是「层 → 物理缓冲」映射的正确性回归：

- [test_layerwise_cache_layout.py:L132-L141](https://github.com/vllm-project/vllm-ascend/blob/7201c97a61a17425b558b6b5e53ab0d30ae8151d/tests/ut/distributed/ascend_store/test_layerwise_cache_layout.py#L132-L141)：27 层开 6 缓冲，断言 `prefetch_layer_map[7]==1`、`storage_indices[1]==[1,7,13,19,25]`，且所有层 id 恰好铺满 `0..26`（复用不丢层）。

非法配置被拒绝（失败路径）：

- [test_layerwise_cache_layout.py:L159-L167](https://github.com/vllm-project/vllm-ascend/blob/7201c97a61a17425b558b6b5e53ab0d30ae8151d/tests/ut/distributed/ascend_store/test_layerwise_cache_layout.py#L159-L167)：传 `layerwise_num_shared_buffers=True/0`、`layerwise_independent_layers=27/"1,4"` 应分别抛 `TypeError`/`ValueError`。

对照一个更「典型 UT」的范本——`test_prepare_finalize.py` 把 MoE 通信整条 mock 掉，只验证张量形状切片/拼接对不对：

- [test_prepare_finalize.py:L36-L61](https://github.com/vllm-project/vllm-ascend/blob/7201c97a61a17425b558b6b5e53ab0d30ae8151d/tests/ut/ops/test_prepare_finalize.py#L36-L61)：`test_mc2_prepare_finalize` 用 `@patch` 把 `get_forward_context`、TP world size/rank mock 掉，断言 MC2 路径下 prepare 的 padding 与 finalize 的还原形状正确。

#### 4.6.4 代码实践

1. **实践目标**：跑通 layerwise 布局单测，并解释它在 CI 中的归属。
2. **操作步骤**：
   ```bash
   pytest -sv tests/ut/distributed/ascend_store/test_layerwise_cache_layout.py
   ```
   只跑复用布局用例：
   ```bash
   pytest -sv tests/ut/distributed/ascend_store/test_layerwise_cache_layout.py::test_reuse_layout_matches_round_robin_storage_slots
   ```
3. **需要观察的现象**：用例 `PASSED`，并可看到 `storage_indices` 等断言通过；该文件不需要 NPU。
4. **预期结果**：所有布局/非法配置用例通过。CI 中它既随 `default_cpu_ut`（全量 CPU UT）跑，也随 `distributed` 模块（因依赖 `vllm_ascend/distributed`）跑，归属 **UT 层 / CPU runner**。
5. **待本地验证**：若未安装 `vllm_ascend`/`vllm`/`torch`，会在 import 阶段失败；可先读断言理解行为。

#### 4.6.5 小练习与答案

- **练习 1**：为什么 layerwise 的布局测试能放 UT，而不必放 E2E？
  - **答案**：因为 `LayerwiseCacheLayout` 是纯数据布局计算（层 id → 物理缓冲 id 的映射），输入输出都是普通 Python/张量对象，不依赖 NPU 也不依赖真实模型，用假 `KVCacheTensor` 就能完整验证 happy path 与非法配置。E2E 留给「真实模型 + 真实 PD 分离跑通」的端到端验证。
- **练习 2**：`test_reuse_layout_matches_round_robin_storage_slots` 里为什么要有「所有层 id 恰好铺满 0..26」这条断言？
  - **答案**：复用是把 N 个逻辑层压到 K 个物理缓冲，最容易出的 bug 是「某层被漏掉或重复」。这条断言保证 round-robin 分配既不丢层也不重层，是布局正确性的底线回归。

## 5. 综合实践

把本讲的知识串起来，完成一次「新特性 → 测试归属 → CI 旅程」的完整推演。

**背景**：假设你要给 `vllm_ascend/distributed/kv_transfer/kv_pool/ascend_store/layerwise_cache_layout.py` 加一个新参数 `layerwise_prefetch_strategy`（控制预取策略），并写了对应的布局单测。

**任务**：

1. **分层归属**：判断这个新单测应放哪一层（UT/E2E/Nightly）、哪个目录，并说明理由。
2. **本地运行**：写出在本地跑这个单测文件的 `pytest` 命令（含单用例粒度），并描述 `PASSED`/`FAILED` 的输出形态。
3. **模块命中**：根据 `test_config.yaml`，说明该 PR（改了 `vllm_ascend/distributed/...`）会命中哪个测试模块、是否触发 test-only 优化。
4. **CI 旅程**：从 `pr_test.yaml` 的视角，描述这个 PR 从 push 到 `ci-gate` 放行会经过哪些 job、这个单测在哪个 job 的哪类 runner 上执行。
5. **夜间对比**：说明这个改动**不会**自动进 A5 夜间流水线，要进夜间需要做什么（改 `nightly_config.yaml`、用 `/nightly` 触发）。

**参考要点**：

1. UT 层、`tests/ut/distributed/ascend_store/`——纯布局计算、不依赖 NPU。
2. `pytest -sv tests/ut/distributed/ascend_store/test_layerwise_cache_layout.py`（文件级）或 `::test_xxx`（用例级）。
3. 命中 `distributed` 模块；因同时改了源码（非 test-only），不触发 test-only 优化，`default_cpu_ut` 也会全量跑 CPU UT。
4. `pre-commit` → `select-tests`（命中 distributed）→ `ensure-csrc-cache` → `run-selected-tests`（CPU runner 跑 UT、NPU runner 跑命中的 E2E）→ `ci-gate` 汇总。
5. A5 夜间走 `nightly_config.yaml` 的静态矩阵 + `workflow_dispatch`，与 PR diff 无关；要把大模型层级的回归加进 A5，需在 `nightly_config.yaml` 的 `a5:` 段补条目并用 `/nightly <name>` 触发。

## 6. 本讲小结

- vllm-ascend 测试分三层：UT 在 `tests/ut/`（多数不需 NPU）、E2E 在 `tests/e2e/`（按卡数分目录、需真实 NPU）、Nightly 在 `tests/e2e/nightly/` + 多条夜间流水线。
- pytest 两个自定义 marker `e2e_model` / `e2e_coverage` 集中注册在 `pyproject.toml`，分别标注 E2E 的模型标识与覆盖维度；本地用 `pytest -sv <file>::<test>` 跑单用例。
- PR 不跑全量，而是由 `select_tests.py` + `test_config.yaml` 做「按 diff 选测试」；`default_cpu_ut` 模块 `optional:false` 保证每个 PR 全量跑 CPU UT 作为质量底线。
- PR 流水线 `pr_test.yaml` 是一条 job 链，最终由单一稳定名字 `ci-gate` 汇总，挂在分支保护上；测试只在打了 `ready` 标签后才真正在 NPU 上跑。
- #13442 新增的 A5 夜间流水线 `schedule_nightly_test_a5.yaml` 走 `workflow_dispatch`、读 `nightly_config.yaml` 的 A5 矩阵、在专用 A5 runner 上跑多/单节点回归与 benchmark，与 PR 的「按 diff 选测试」是两种完全不同的节奏。
- #12852 的 layerwise 缓冲复用特性，其布局规划单测 `test_layerwise_cache_layout.py` 是「新特性如何落 UT」的范本：纯布局计算用假数据测 happy path、用非法配置测失败路径，归 `distributed` 模块、CPU UT 层。

## 7. 下一步学习建议

- 想动手贡献代码：直接读 [AGENTS.md](https://github.com/vllm-project/vllm-ascend/blob/7201c97a61a17425b558b6b5e53ab0d30ae8151d/AGENTS.md) 的 Quick Start 与 Review Checklist，并配合 [u11-l5 二次开发实战：贡献一个新补丁](u11-l5-contribute-new-patch.md) 走完一次 PR 全流程。
- 想深入 CI 编排：读 `.github/workflows/scripts/select_tests.py` 全文与 `test_config.yaml` 的 `runner_mapping` / `partition` / `estimated_times` 段，理解测试如何按耗时做负载均衡切分。
- 想理解 layerwise 特性本身：回到 [u10-l7 分层 prefill KV 缓冲复用](u10-l7-layerwise-prefill-kv-buffer-reuse.md)，把本讲的布局单测和那讲的 `LayerwiseCacheLayout` 源码对照阅读，体会「特性代码 ↔ 特性测试」的镜像关系。
- 想看真实 E2E：挑 `tests/e2e/pull_request/one_card/` 下一个用例，看它如何用 `e2e_model`/`e2e_coverage` marker 标注、如何被 `runner_mapping` 路由到 1 卡 NPU runner。
