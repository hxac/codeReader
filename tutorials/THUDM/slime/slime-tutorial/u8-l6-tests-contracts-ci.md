# 测试、契约测试与 CI

## 1. 本讲目标

slime 是一个把 Megatron 训练和 SGLang 推理缝合成「采样→训练→权重同步」闭环的大框架，且开放了 21+ 个 `--xxx-path` 自定义接口（见 [u6-l1](u6-l1-customization-overview.md)）。接口越多，「我写的自定义函数到底符不符合框架期望的形状」就越难靠人眼判断。本讲解决的就是这个「信心」问题：slime 用什么手段，让你**不跑 GPU、不连 SGLang**，就能确认自己写的 `custom_generate` / `custom_rm` / `rollout_function` 不会被框架「咬」。

学完本讲，你应当能够：

1. 说清 slime 的测试与 CI 分成「常驻 CPU 层」和「标签触发的 GPU 端到端层」两层，以及为什么这样切。
2. 看懂 `pyproject.toml` 里 pytest 的 markers / testpaths / strict-markers 配置，并理解它们如何决定哪些测试会被发现。
3. 理解 `tests/plugin_contracts/` 下四类契约测试分别在守护哪一种自定义 hook 的「形状契约」，并能讲清三层断言（签名匹配 / 最小调用验返回 / 调用点稳定）。
4. 用 `SLIME_CONTRACT_*` 环境变量把你自己的实现喂给契约测试，跑通一次纯 CPU 自检。

## 2. 前置知识

- **自定义接口回顾（[u6-l1](u6-l1-customization-overview.md)）**：slime 把骨架写死、把「肉」做成可用 import 路径字符串注入的函数。`--custom-generate-function-path mypkg.myfile.generate` 会被 `load_function` 解析成函数对象。本讲要回答的核心问题就是「这个字符串指向的函数，签名对不对」。
- **`load_function`**：4 行核心逻辑——`rpartition('.')` 切出模块路径与属性名、`importlib.import_module` 导入、`getattr` 取属性。本讲的契约测试反复用它来「加载你的实现」。
- **pytest 基础**：`pytest` 会自动发现 `test_*.py` 文件与 `test_` 开头的函数；`@pytest.mark.xxx` 给测试打标签；`-m "xxx"` 按标签筛选；`@pytest.mark.parametrize` 让一个测试函数对多组数据各跑一次。
- **GitHub Actions 标签（label）触发**：PR 上可以贴标签（如 `run-ci-megatron`），CI 工作流能用 `contains(github.event.pull_request.labels.*.name, '...')` 判断某个标签在不在，从而决定某个昂贵的 GPU 任务要不要跑。
- **fcntl 文件锁**：Linux 上 `flock(fd, LOCK_EX | LOCK_NB)` 可以对一个文件描述符加非阻塞排他锁，slime 用它在共享内存 `/dev/shm` 下做「这张卡被谁占着」的进程级锁。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| `pyproject.toml` | pytest 配置中枢：markers、testpaths、`--strict-markers`、norecursedirs |
| `docs/en/developer_guide/ci.md` | CI 设计文档：两层结构、标签与任务的对应表、如何写新测试 |
| `.github/workflows/pr-test.yml.j2` | CI 工作流的 Jinja2 模板（真正生效的 `pr-test.yml` 由它生成） |
| `tests/plugin_contracts/_shared.py` | 契约测试的公共脚手架：桩模块、环境变量映射、`run_contract_test_for_file` |
| `tests/plugin_contracts/test_plugin_generate_contracts.py` | **契约类一**：自定义生成函数 `custom_generate` 的契约 |
| `tests/plugin_contracts/test_plugin_rollout_contracts.py` | **契约类二**：整条 `rollout_function` 的契约 |
| `tests/plugin_contracts/test_plugin_runtime_hook_contracts.py` | **契约类三**：5 个运行期 hook 的契约（日志、奖励后处理、数据转换等） |
| `tests/plugin_contracts/test_plugin_path_loading_contracts.py` | **契约类四**：6 个 `--xxx-path` 插件 + `custom_rm` 的加载与形状契约 |
| `slime/utils/misc.py` | `load_function` 的实现（契约测试与生产代码共用） |
| `slime/rollout/sglang_rollout.py` | 生产侧 `generate_and_rm`，契约测试一的真身调用点 |
| `tests/test_qwen2.5_0.5B_short.py` | GPU 端到端测试的范例：`prepare()/execute()` 范式 + `NUM_GPUS` |
| `tests/ci/gpu_lock_exec.py` | GPU 锁：用 fcntl 在 `/dev/shm` 给每张卡配一把文件锁 |

## 4. 核心概念与源码讲解

### 4.1 测试与 CI 的两层结构

#### 4.1.1 概念说明

slime 的测试遵循一个朴素但关键的取舍：**绝大多数不变量应当快速、便宜地验证；完整训练/采样的端到端行为虽然重要，但太贵（要 GPU、要模型权重、要数分钟），所以只在被明确请求时才跑。**

由此产生两层：

1. **常驻 CPU 正确性测试**：每个 PR、每次 push 到 `main`、手动触发都会跑。只装 CPU 版 PyTorch 和轻量依赖，秒级到分钟级返回。它是「第一道防线」，目的是把那些「一改就静默崩掉」的 RL 基础设施 bug 拦在 GPU 运行之前。
2. **标签触发的 GPU 端到端测试**：在自建 GPU runner 上、用 Docker 容器跑真实的 Megatron + SGLang 训练流程。只有在 PR 上贴了对应标签（如 `run-ci-megatron`）或手动 `workflow_dispatch` 时才触发。

契约测试属于第一层（纯 CPU），也是本讲的主角。

#### 4.1.2 核心流程

一个 PR 提交后，CI 大致这样流动：

```text
PR 提交 / push 到 main / workflow_dispatch
        │
        ├──【常驻、无需标签】
        │     cpu-unittest   ──→ 跑 ~30 个 CPU 单测 + 4 个契约测试文件
        │     agent-test     ──→ 跑 4 个 agent adapter 测试（需 openai/anthropic SDK）
        │
        └──【按需、需贴标签】
              run-ci-megatron   → e2e-test-megatron   (GPU, dense/MoE/PPO/OPD/...)
              run-ci-sglang-config → e2e-test-sglang-config (GPU, 异构拓扑)
              run-ci-precision  → e2e-test-precision  (GPU, 并行数值一致性)
              run-ci-ckpt       → e2e-test-ckpt        (GPU, 检查点存读)
              run-ci-image      → e2e-test-image       (GPU, 换成 test 镜像重跑 megatron 矩阵)
              run-ci-changed    → e2e-test-changed     (混合，只跑改动的测试文件)
```

关键点：**CPU 任务不进 Docker、不抢 GPU、不调 `gpu_lock_exec.py`**；GPU 任务则一定进 `slimerl/slime:latest` 容器，先 `pip install -e . --no-deps`，再用 `gpu_lock_exec.py --count <N>` 抢卡，最后 `python tests/<test_file>.py` 跑测试。

#### 4.1.3 源码精读

**（a）pytest 配置中枢** —— `pyproject.toml`：

[pyproject.toml:L40-L44](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/pyproject.toml#L40-L44) 是 pytest 的入口配置：

- `addopts = "--verbose --pyargs --durations=0 --strict-markers"` —— 其中 `--strict-markers` 很重要：**任何没在 `markers` 列表里声明就使用的 `@pytest.mark.xxx` 都会直接报错**。这防止「打了个拼错的标签却静默不筛选」的陷阱。
- `testpaths = ["./tests"]` —— 只在 `tests/` 下发现测试，写成显式路径是为了避免 import 到别处同名 `tests` 模块。
- `norecursedirs`（[pyproject.toml:L46-L61](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/pyproject.toml#L46-L61)）显式排除 `examples`、`scripts`、`tools`、`docs`、`tutorials` 等目录，不让 pytest 误把它们当测试。

[pyproject.toml:L63-L71](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/pyproject.toml#L63-L71) 声明了 7 个合法 marker：`unit / integration / system / acceptance / docs / skipduringci / pleasefixme`。注意：这里的 marker 体系是「测试规模/意图」分类（单元、集成、系统、验收），**与 CI 的标签（label）是两套独立东西**——marker 是 pytest 层面的 `-m` 筛选，label 是 GitHub 层面决定哪个 CI 任务触发。初学者容易混淆，记住：marker 管「pytest 跑哪些用例」，label 管「CI 跑哪个任务」。

**（b）两层 CI 的总纲** —— `docs/en/developer_guide/ci.md`：

[docs/en/developer_guide/ci.md:L1-L9](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/docs/en/developer_guide/ci.md#L1-L9) 一句话点明设计意图：「大多数不变量应在不等 GPU 机队的情况下快速检查，而完整训练/采样行为仍由 GPU e2e 任务覆盖。」

[docs/en/developer_guide/ci.md:L14-L22](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/docs/en/developer_guide/ci.md#L14-L22) 定义两个 CPU 任务：`cpu-unittest` 安装 CPU PyTorch 与轻依赖后，用 `python tests/<test_file>.py` 跑注册过的单测和契约测试；`agent-adapter-test` 同理但额外装 `openai / openai-agents / anthropic`。明确写着「CPU jobs do not use Docker, do not acquire GPUs」。

**（c）标签与任务的对应表** —— 这是本讲「run-ci 标签」模块的核心：

[docs/en/developer_guide/ci.md:L46-L59](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/docs/en/developer_guide/ci.md#L46-L59) 给出完整的「触发条件 → 任务 → 类型 → 说明」表，整理如下：

| 触发标签 / 条件 | 任务名 | 类型 | 说明 |
|---|---|---|---|
| 自动（无需标签） | `cpu-unittest` | CPU | 常驻：参数校验、调度、奖励、sample、rollout 校验、**plugin 契约** |
| 自动（无需标签） | `agent-test` | CPU | 常驻：agent adapter 测试（需 provider SDK） |
| `run-ci-sglang-config` | `e2e-test-sglang-config` | GPU | SGLang 异构拓扑、混合/卸载场景 |
| `run-ci-megatron` | `e2e-test-megatron` | GPU | Megatron 训练主干：dense/MoE/PPO/MTP/OPD/异步/PD/debug 回放 |
| `run-ci-precision` | `e2e-test-precision` | GPU | 数值精度与并行一致性 |
| `run-ci-ckpt` | `e2e-test-ckpt` | GPU | 检查点存读正确性（CPU/GPU optimizer、async save） |
| `run-ci-image` | `e2e-test-image` | GPU | 在 `slimerl/slime-test:latest` 镜像上重跑 megatron 矩阵 |
| `run-ci-changed` | `e2e-test-changed` | 混合 | 只跑改动的测试文件，按各文件的 `NUM_GPUS` 决定要不要卡 |

注意：表里「自动」的两个 CPU 任务，虽然 ci.md 行文也提到过 `run-ci-cpu-unittest`、`run-ci-agent` 这种标签名，但它们在模板里被标成 `always: True`，即**不依赖标签也会跑**（见 4.1.3(d)）。标签名只是「该任务的别名」，真正决定跑不跑的是 `if:` 条件。

**（d）「常驻 vs 标签触发」在模板里如何落地** —— `.github/workflows/pr-test.yml.j2`：

[.github/workflows/pr-test.yml.j2:L147-L158](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/.github/workflows/pr-test.yml.j2#L147-L158) 是模板循环里最关键的一段：

```jinja
<% if config.get('always') %>
    if: github.event_name == 'pull_request' || ... == 'workflow_dispatch' || ... == 'push'
<% else %>
    if: (workflow_dispatch) || (pull_request && contains(labels, '<< config.label >>'))
<% endif %>
```

也就是说：`always: True` 的任务（CPU 两个）在任何事件下都跑；其余任务只有当 PR 标签里含 `config.label`（例如 `run-ci-megatron`）时才跑。这就是「标签触发」的真正机制。

`cpu-unittest` 的任务定义见 [.github/workflows/pr-test.yml.j2:L62-L101](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/.github/workflows/pr-test.yml.j2#L62-L101)，可以看到四个契约测试文件都被注册进矩阵（`num_gpus: 0`），与其它单测并列。这印证了「契约测试是 CPU 常驻任务的一等公民」。

**（e）run-ci-changed 的动态矩阵** —— 它不写死要跑什么，而是 `git diff` 出改动的测试文件：

[.github/workflows/pr-test.yml.j2:L285-L310](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/.github/workflows/pr-test.yml.j2#L285-L310) 用 `git diff --name-only --diff-filter=AM origin/main...HEAD -- 'tests/test_*.py' 'tests/plugin_contracts/test_*.py'` 找出新增/修改的测试文件，再对每个文件 `grep -oP '^NUM_GPUS\s*=\s*\K\d+'` 抽数 `NUM_GPUS`，缺省补 8。这就是 ci.md 反复强调「CPU 测试要写 `NUM_GPUS = 0`」的原因——否则 `run-ci-changed` 会以为你要 8 张卡。

#### 4.1.4 代码实践

**实践目标**：亲手验证 marker 与标签是两套独立机制，并理解 `--strict-markers` 的保护作用。

**操作步骤**：

1. 在仓库根目录查看所有合法 marker 与配置：
   ```bash
   grep -n "markers\|strict-markers\|testpaths" pyproject.toml
   ```
2. 故意制造一个 strict-markers 报错（**示例代码，请在临时分支或临时文件里试，不要提交**）：在某个 `tests/test_*.py` 里加一个用例：
   ```python
   def test_strict_markers_demo():
       pass
   test_strict_markers_demo = pytest.mark.this_marker_does_not_exist(test_strict_markers_demo)
   ```
   再跑 `python -m pytest tests/<那个文件>.py`。

**需要观察的现象**：

- 步骤 1 能看到 7 个 marker 与 `--strict-markers`。
- 步骤 2 会因为 `this_marker_does_not_exist` 未声明而**立即报错退出**，而不是「静默打了个无效标签」。

**预期结果**：你会切身体会到 `--strict-markers` 把「标签拼写错误」从「运行时不生效的隐性 bug」提升为「启动即失败」。

> 待本地验证：步骤 2 的确切报错文案取决于 pytest 版本，核心信息形如 `'this_marker_does_not_exist' not found in markers_namespace`。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `cpu-unittest` 任务里每个测试都标了 `num_gpus: 0`，但模板里 CPU 分支并不调用 `gpu_lock_exec.py`？这个 `0` 还有意义吗？

**答案**：CPU 分支用 `num_gpus == 0` 作判据走「直接 `python <test>`」路径，绕过 `gpu_lock_exec.py`（见模板 L215-L219）。这个 `0` 在 CPU 任务里其实是冗余的（CPU 任务本来就不抢卡），但它的真正价值体现在 **`run-ci-changed`**：当这个测试文件被改时，`run-ci-changed` 会 grep `NUM_GPUS`，写成 `0` 才能保证它即使在混合任务里也不会去抢 8 张卡。

**练习 2**：`@pytest.mark.unit` 和 PR 标签 `run-ci-megatron` 分别属于哪一层？它们会互相影响吗？

**答案**：marker 属于 pytest 层（用 `-m` 筛选用例），label 属于 GitHub Actions 层（决定哪个 CI 任务触发）。两者互不影响：贴了 `run-ci-megatron` 只会让 GPU e2e 任务跑起来，至于任务内部 pytest 跑哪些用例，仍由测试文件的 `__main__` 入口（`python <file>`）或文件内的 marker 决定。

---

### 4.2 plugin 契约测试：四类 hook 的形状保证

#### 4.2.1 概念说明

slime 的 21+ 个 `--xxx-path` 接口，本质上都是「框架在某处用 `load_function(path)` 拿到你的函数，然后按某个**固定签名**调用它」。例如框架假设 `custom_generate(args, sample, sampling_params)` 一定是这个三个参数的异步函数——一旦你写成了 `(sample, args, sampling_params)`，框架不会在启动时报错，而是会在运行期以「参数对不上」的形式炸掉，且只在 GPU 上、几分钟后才暴露。

**契约测试（contract test）**就是为了把这种「隐式假设」变成「显式断言」。它不去跑真正的训练，而是：

1. 用 `inspect.signature` 检查你的函数参数列表是否符合框架期望；
2. 用一组假数据**真的调用一次**你的函数，检查返回值结构是否正确；
3. 对运行期 hook，额外断言「框架源码里调用它的那一行字符串仍然存在」（防止重构悄悄改了调用约定）。

slime 把这些契约按 hook 种类拆成 **4 个文件**，即「四类契约」：

| 文件 | 守护的接口 | 代表参数 |
|---|---|---|
| `test_plugin_generate_contracts.py` | 自定义生成函数 | `--custom-generate-function-path` |
| `test_plugin_rollout_contracts.py` | 整条 rollout 函数 | `--rollout-function-path` |
| `test_plugin_runtime_hook_contracts.py` | 运行期 hook（日志/奖励后处理/数据转换/数据后处理） | `--custom-reward-post-process-path` 等 5 个 |
| `test_plugin_path_loading_contracts.py` | 可加载插件的路径契约 | `--data-source-path`、`--buffer-filter-path`、`--custom-rm-path` 等 |

#### 4.2.2 核心流程

四类契约测试都遵循同一个套路，只是「被测函数」不同：

```text
                ┌─────────────────────────────────────────┐
默认行为测试 ──→ │ 1. 检查「默认实现」自身仍符合契约          │  （回归护栏）
                │    e.g. generate_rollout 的签名稳定       │
                └─────────────────────────────────────────┘
                ┌─────────────────────────────────────────┐
用户实现测试 ──→ │ 2. 从 SLIME_CONTRACT_XXX_PATH 读你的路径 │  （自检）
                │    （没设环境变量就用「参考实现」兜底）    │
                └─────────────────────────────────────────┘
                              │
                              ▼
                ┌─────────────────────────────────────────┐
                │ 3. load_function(path) 拿到你的函数      │
                │ 4. inspect.signature 比对参数顺序/种类    │  ← 断言层一：签名匹配
                │ 5. 用假数据调用一次，断言返回结构         │  ← 断言层二：最小调用验返回
                │ 6.（仅 runtime hook）检查调用点字符串     │  ← 断言层三：调用点稳定
                └─────────────────────────────────────────┘
```

「断言层三」只对 runtime hook 做：因为它测的是「框架某文件里是否仍写着 `self.custom_reward_post_process_func(self.args, samples)` 这一行」，即框架的**调用点**没被重构掉。

#### 4.2.3 源码精读

**（a）公共脚手架 `_shared.py`** —— 四个文件都 import 它。先看它如何让测试在纯 CPU 上跑起来：

[tests/plugin_contracts/_shared.py:L22-L46](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/tests/plugin_contracts/_shared.py#L22-L46) 的 `install_stubs` 是「CPU 可跑」的关键：如果 `ray`、`sglang_router`、`transformers` 没装，就用 `types.ModuleType` 造一个最小桩模块塞进 `sys.modules`。这样 `import slime.rollout.sglang_rollout` 时不会因为缺 SGLang 而失败——契约测试只需测「函数形状」，不需要真的推理引擎。

[tests/plugin_contracts/_shared.py:L12](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/tests/plugin_contracts/_shared.py#L12) 定义环境变量前缀 `ENV_PREFIX = "SLIME_CONTRACT_"`；[tests/plugin_contracts/_shared.py:L49-L54](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/tests/plugin_contracts/_shared.py#L49-L54) 提供 `contract_env_name(key)`（拼成 `SLIME_CONTRACT_<KEY>`）与 `get_contract_path(key, default)`（优先读环境变量，否则用默认参考实现路径）。这就是 `SLIME_CONTRACT_*` 的全部来源。

[tests/plugin_contracts/_shared.py:L57-L88](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/tests/plugin_contracts/_shared.py#L57-L88) 的 `run_contract_test_for_file` 把命令行 `--xxx-path` 参数自动转成 `SLIME_CONTRACT_XXX_PATH` 环境变量，再 `pytest.main([file])`。所以你可以两种方式喂自定义实现：**设环境变量**，或 **`python tests/.../test_xxx.py --xxx-path your.path`**。

**（b）契约类一：custom_generate** —— 最贴近生产调用路径的一类。

它直接调用真正的生产函数 `generate_and_rm`，因为 `custom_generate` 正是在那里被框架加载并调用的。[slime/rollout/sglang_rollout.py:L223-L262](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/rollout/sglang_rollout.py#L223-L262) 显示，框架在 L250 先取 `custom_func_path`，在 L253 `load_function` 加载，在 L255 用 `inspect.signature(...).parameters` 判断有没有 `evaluation` 形参，从而决定怎么调。契约测试就是为这段逻辑把守「你的函数签名对不对」。

参考实现见 [tests/plugin_contracts/test_plugin_generate_contracts.py:L71-L77](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/tests/plugin_contracts/test_plugin_generate_contracts.py#L71-L77)：`async def custom_generate(args, sample, sampling_params)`，写回 `tokens/response/response_length/reward/status`。

两个核心断言点：

- [test_plugin_generate_contracts.py:L98-L100](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/tests/plugin_contracts/test_plugin_generate_contracts.py#L98-L100) —— **断言层一（签名匹配）**：`assert params[:3] == ("args", "sample", "sampling_params")`，前三个位置参数必须严格是这个顺序。
- [test_plugin_generate_contracts.py:L90-L95](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/tests/plugin_contracts/test_plugin_generate_contracts.py#L90-L95) —— **断言层二（最小调用验返回）**：返回必须是 `Sample`，且 `tokens` 是 list、`response` 是 str、`response_length` 是 int、`reward` 非 None。

「用户覆盖」测试见 [test_plugin_generate_contracts.py:L159-L173](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/tests/plugin_contracts/test_plugin_generate_contracts.py#L159-L173)：`get_contract_path("CUSTOM_GENERATE_FUNCTION_PATH", REFERENCE_...)` 拿到你的路径（或参考实现），加载后先比对签名，再真的 `asyncio.run(generate_and_rm(...))` 跑一次。**这就是本讲综合实践要复刻的入口。**

**（c）契约类二：rollout_function** —— 守护整条 rollout 流水线的形状。

它要求自定义 `rollout_function` 与默认 `generate_rollout` **签名逐参数一致**：[test_plugin_rollout_contracts.py:L126-L135](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/tests/plugin_contracts/test_plugin_rollout_contracts.py#L126-L135) 不仅比对参数名元组，还比对每个参数的 `kind`（位置/关键字/可变参数）与 `default`。

参考实现 [test_plugin_rollout_contracts.py:L65-L81](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/tests/plugin_contracts/test_plugin_rollout_contracts.py#L65-L81) 展示了正确形状：训练路径返回 `RolloutFnTrainOutput(samples=list[list[Sample]])`，评估路径返回 `RolloutFnEvalOutput(data=dict)`。这两种 dataclass 见 [slime/rollout/base_types.py:L7-L16](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/rollout/base_types.py#L7-L16)，而 [slime/rollout/base_types.py:L19-L26](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/rollout/base_types.py#L19-L26) 的 `call_rollout_fn` 还兼容旧式返回（裸 list 或裸 dict），契约测试里专门有一条 `test_default_rollout_compat_wrapper_stability` 守护这个兼容包装。

「用户覆盖 + 形状对齐」见 [test_plugin_rollout_contracts.py:L149-L156](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/tests/plugin_contracts/test_plugin_rollout_contracts.py#L149-L156)：只有当路径**不是默认实现**时，才进一步调用 `assert_rollout_function_matches_default_contract` 真跑一次训练+评估输出并校验结构。

**（d）契约类三：runtime hook** —— 唯一带「调用点稳定」断言的一类。

[test_plugin_runtime_hook_contracts.py:L131-L177](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/tests/plugin_contracts/test_plugin_runtime_hook_contracts.py#L131-L177) 用一个 `HOOK_CASES` 列表把 5 个 hook（`custom_rollout_log` / `custom_eval_rollout_log` / `custom_reward_post_process` / `custom_convert_samples_to_train_data` / `rollout_data_postprocess`）参数化，每条 case 携带：环境变量名、默认参考路径、**框架调用点所在文件**、**调用点字符串标记**、期望参数、一个调用函数。

**断言层三（调用点稳定）**见 [test_plugin_runtime_hook_contracts.py:L180-L182](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/tests/plugin_contracts/test_plugin_runtime_hook_contracts.py#L180-L182)：

```python
def test_runtime_hook_callsite_is_stable(case: HookCase):
    assert case.runtime_marker in Path(case.source_path).read_text()
```

它直接读框架源码（如 `slime/ray/rollout.py`），断言里面**仍然写着** `self.custom_reward_post_process_func(self.args, samples)` 这串字符。意义在于：一旦有人重构框架、改了调用约定（比如把 `samples` 换成 `rollout_samples`），这条测试立刻失败，提醒同步更新文档与契约。**这是把「隐式调用约定」钉死的最后一道保险。**

**断言层一+层二**见 [test_plugin_runtime_hook_contracts.py:L185-L189](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/tests/plugin_contracts/test_plugin_runtime_hook_contracts.py#L185-L189)：加载函数、比对参数元组、再 `case.invoke(fn)` 真调一次。

**（e）契约类四：path loading + custom_rm** —— 覆盖最广的一类。

[test_plugin_path_loading_contracts.py:L277-L320](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/tests/plugin_contracts/test_plugin_path_loading_contracts.py#L277-L320) 用 `SYNC_CASES` 参数化 6 个 `--xxx-path` 插件：`eval_function`、`dynamic_filter`、`buffer_filter`、`data_source`、`rollout_sample_filter`、`rollout_all_samples_process`。每个 case 提供 `default_check`（查默认实现）与 `path_check`（查你的实现）两个回调，分别被 [test_plugin_path_loading_contracts.py:L323-L330](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/tests/plugin_contracts/test_plugin_path_loading_contracts.py#L323-L330) 的两条参数化测试调用。

特别地，`custom_rm` 有「单样本」与「整组（group_rm）」两种互斥签名，见 [test_plugin_path_loading_contracts.py:L333-L368](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/tests/plugin_contracts/test_plugin_path_loading_contracts.py#L333-L368)：它读 `SLIME_CONTRACT_GROUP_RM` 判断走哪条分支——单样本期望前两参是 `(args, sample)` 返 float，整组期望 `(args, samples)` 返 list。这也是为什么 `run_contract_test_for_file` 支持 `extra_args`（如 `--group-rm`）与 `extra_setup`（把开关也写进环境变量）。

#### 4.2.4 代码实践

**实践目标**：跑通一个已有的契约测试文件，确认你的环境能 import slime 并通过全部断言。

**操作步骤**：

1. 安装最小依赖（CPU 即可）：
   ```bash
   pip install torch --index-url https://download.pytorch.org/whl/cpu
   pip install pytest numpy packaging pyyaml omegaconf tqdm httpx requests ray pybase64 pylatexenc sympy aiohttp pillow safetensors psutil transformers
   pip install -e . --no-deps
   ```
2. 直接运行契约测试（两种等价方式任选）：
   ```bash
   # 方式 A：当脚本跑（命中文件末尾 __main__）
   python tests/plugin_contracts/test_plugin_generate_contracts.py
   # 方式 B：用 pytest 跑
   python -m pytest tests/plugin_contracts/test_plugin_generate_contracts.py
   ```

**需要观察的现象**：终端打印若干条 `test_...` 用例结果，最终 `== passed in N.NNs ==`。

**预期结果**：在没有设任何 `SLIME_CONTRACT_*` 环境变量时，测试用「参考实现」兜底，应当全部通过。

> 待本地验证：通过的具体用例数随版本变化（当前 generate 契约有 4 条用例）；若 `import slime` 因缺其它原生依赖失败，请先按 ci.md 的 CPU 依赖清单补齐。

#### 4.2.5 小练习与答案

**练习 1**：四类契约里，哪一类会去读框架源码文件（`Path(...).read_text()`）？为什么只有它这么做？

**答案**：契约类三（runtime hook）。因为它守护的是「框架在运行期调用 hook 的那一行代码」的稳定性——这是一种**调用约定**，光测你的函数签名不够，还得保证框架没把调用点改没。其余三类守护的是「插件函数自身的形状」，只 import 你的函数即可，不必读框架源码文本。

**练习 2**：契约类二的 `test_rollout_function_path_contract_supports_user_override` 里有一句 `if rollout_path != DEFAULT_ROLLOUT_FUNCTION_PATH:` 才跑完整对齐，为什么默认实现反而跳过完整对齐？

**答案**：默认实现 `generate_rollout` 需要 SGLang 才能真跑（参考 u3-l2），而契约测试是纯 CPU 环境。所以对默认实现只做「签名稳定」检查（`assert_rollout_function_signature_matches_default`），不真跑；只有当你提供了自定义实现（理应可在 CPU 上用假 `data_source` 跑通）时，才进一步用 `ContractDataSource` 真跑一次校验结构。

---

### 4.3 SLIME_CONTRACT_\* 与 load_function 的自检闭环

#### 4.3.1 概念说明

前两节讲了「CI 怎么组织」和「契约测什么」。本节把两者缝起来，回答一个工程师最关心的问题：**「我刚写完一个 `custom_generate`，怎么用最快的方式确认它没问题？」**

答案就是 `SLIME_CONTRACT_*` 环境变量 + `load_function` 构成的**自检闭环**：

- 你写的自定义函数挂在一个可 import 的模块路径上（如 `myproj.myrollout.my_generate`）；
- 契约测试通过 `load_function` 把这个字符串解析成函数对象；
- 你用 `SLIME_CONTRACT_CUSTOM_GENERATE_FUNCTION_PATH=myproj.myrollout.my_generate` 告诉契约测试「测这个」；
- 契约测试用它替换默认参考实现，跑签名比对 + 最小调用 + 返回结构校验。

这套机制的好处是：**完全脱离训练集群、脱离 SGLang、脱离 GPU**，几秒钟就能知道你的 hook 形状对不对。它本质上是一种「轻量级单元测试替身」——你不必理解整个 RL 闭环，只需对齐「函数签名 + 返回结构」这一最小契约。

#### 4.3.2 核心流程

```text
你的实现：myproj/myrollout.py
    async def my_generate(args, sample, sampling_params): ...
            │
            │  （import 路径字符串）
            ▼
环境变量：SLIME_CONTRACT_CUSTOM_GENERATE_FUNCTION_PATH=myproj.myrollout.my_generate
            │
            ▼
契约测试 get_contract_path("CUSTOM_GENERATE_FUNCTION_PATH", 默认参考实现)
            │
            ▼
load_function(path)  ── rpartition / import_module / getattr ──→ 你的函数对象
            │
            ▼
三层断言：签名前三位 == (args, sample, sampling_params)
          asyncio.run(generate_and_rm(...)) 真调一次
          assert_sample_contract(返回)  ← tokens/response/reward 检查
            │
            ▼
        全绿 → 你的实现「形状合格」，可上 GPU 验证真实行为
```

注意：契约测试**只保证形状，不保证语义正确**。比如你的 `my_generate` 就算返回了合法的 `Sample`，但 token 内容是错的、loss_mask 标反了，契约测试发现不了——那要靠 GPU e2e 与人工检查。契约测试负责「不会被框架当场咬」，不负责「行为就是你想要的」。

#### 4.3.3 源码精读

`load_function` 的真身非常短，[slime/utils/misc.py:L39-L47](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/utils/misc.py#L39-L47)：

```python
def load_function(path):
    module_path, _, attr = path.rpartition(".")   # 切最后一个点
    module = importlib.import_module(module_path)  # 导入模块
    return getattr(module, attr)                   # 取属性（函数或类）
```

它是契约测试与生产代码的**共同底座**：生产侧 `sglang_rollout.py:253` 用它加载 `custom_generate`，契约测试用同一个函数加载你的实现——所以「契约测试能加载成功」本身就保证了「生产侧也能加载成功」。

环境变量如何被消费，回顾 [tests/plugin_contracts/_shared.py:L53-L54](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/tests/plugin_contracts/_shared.py#L53-L54)：

```python
def get_contract_path(key: str, default: str | None = None) -> str | None:
    return os.environ.get(contract_env_name(key), default)
```

即 `SLIME_CONTRACT_<KEY>`。对 `custom_generate`，key 就是 `CUSTOM_GENERATE_FUNCTION_PATH`，完整变量名即 `SLIME_CONTRACT_CUSTOM_GENERATE_FUNCTION_PATH`。

而「把命令行参数转环境变量」的逻辑在 [tests/plugin_contracts/_shared.py:L57-L88](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/tests/plugin_contracts/_shared.py#L57-L88)：它把 `--custom-generate-function-path foo.bar` 解析后 `key.upper()` 变成 `CUSTOM_GENERATE_FUNCTION_PATH`，加前缀写成环境变量，再 `pytest.main([file])`。因此下面两种写法等价：

```bash
# 写法一：环境变量
export SLIME_CONTRACT_CUSTOM_GENERATE_FUNCTION_PATH=myproj.myrollout.my_generate
python tests/plugin_contracts/test_plugin_generate_contracts.py

# 写法二：命令行参数（run_contract_test_for_file 会转成同样的环境变量）
python tests/plugin_contracts/test_plugin_generate_contracts.py \
       --custom-generate-function-path myproj.myrollout.my_generate
```

#### 4.3.4 代码实践（本讲主实践）

**实践目标**：为你自己写一个最小 `custom_generate`，用 `SLIME_CONTRACT_CUSTOM_GENERATE_FUNCTION_PATH` 把它喂给契约测试，跑通并记录每个断言点。

**操作步骤**：

1. 在任意可 import 的位置（例如仓库根目录新建 `myproj/myrollout.py`，并保证 `PYTHONPATH` 含仓库根）写一个最小实现（**示例代码，非项目原有文件**）：
   ```python
   # myproj/myrollout.py
   from slime.utils.types import Sample

   async def my_generate(args, sample: Sample, sampling_params: dict):
       # 最小合规：写回 tokens / response / response_length / reward / status
       sample.tokens = [101, 102, 103]
       sample.response = "hello"
       sample.response_length = len(sample.tokens)
       sample.reward = 0.0          # 真实奖励由框架后续算
       sample.status = Sample.Status.COMPLETED
       return sample
   ```
2. 把它喂给契约测试（确保 `PYTHONPATH` 含仓库根，使 `myproj.myrollout.my_generate` 可被 `import_module`）：
   ```bash
   export SLIME_CONTRACT_CUSTOM_GENERATE_FUNCTION_PATH=myproj.myrollout.my_generate
   python tests/plugin_contracts/test_plugin_generate_contracts.py
   ```
3. 故意把签名写错（第二个实验），把 `my_generate(args, sample, sampling_params)` 改成 `my_generate(sample, args, sampling_params)`，重跑步骤 2。

**需要观察的现象与断言点**：

| 断言点 | 来源 | 步骤 2（正确） | 步骤 3（签名错） |
|---|---|---|---|
| 前三位参数 == `("args","sample","sampling_params")` | `assert_custom_generate_signature_matches_expected` | 通过 | **失败** |
| `load_function` 能加载 | `load_function(custom_generate_path)` | 通过 | 通过（加载本身不验签名） |
| 返回是 `Sample` 且字段齐全 | `assert_sample_contract` | 通过 | 不会执行到（签名已挂） |
| `generate_and_rm` 跑通 | `asyncio.run(generate_and_rm(...))` | 通过 | — |

**预期结果**：步骤 2 全绿；步骤 3 在「断言层一（签名匹配）」即失败，报 `assert params[:3] == ("args","sample","sampling_params")`，这正是契约测试把你拦在 GPU 运行之前的价值。

> 待本地验证：步骤 2 的确切通过用例数取决于 slime 版本；若提示找不到 `slime.utils.types`，请确认已 `pip install -e . --no-deps` 且依赖已装。

#### 4.3.5 小练习与答案

**练习 1**：如果你把 `my_generate` 写成同步函数（去掉 `async`），契约测试会在哪一步失败？为什么？

**答案**：在 `asyncio.run(generate_and_rm(...))` 这一步。生产侧 `sglang_rollout.py:256` 用 `await custom_generate_func(...)` 调用它，`await` 一个非协程对象会抛 `TypeError`。签名检查（层一）查不出这个——它只看参数名，不看是否 `async`——所以失败会发生在「最小调用验返回」（层二）。这说明签名匹配是必要但不充分的护栏。

**练习 2**：`run_contract_test_for_file` 为什么要把命令行 `--xxx-path` 转成 `SLIME_CONTRACT_*` 环境变量，而不是直接把值传进测试函数？

**答案**：因为同一个文件里有**多条**测试函数都要读这个路径（如默认行为测试、用户覆盖测试），而 pytest 是按函数收集的，没有简单的「给整个文件传参」机制。把它沉淀成进程级环境变量后，文件内任何函数用 `get_contract_path(key)` 都能读到，且和「你直接 `export` 环境变量再跑」完全等价——两种入口自然统一。

---

## 5. 综合实践

把本讲的三条主线串起来，完成一个「从写自定义函数到 CI 验证」的完整闭环。

**任务**：实现一个自定义 `rollout_sample_filter`（对应 `--rollout-sample-filter-path`，见契约类四），并通过它的契约测试。

**步骤**：

1. **查契约**：打开 [test_plugin_path_loading_contracts.py:L249-L258](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/tests/plugin_contracts/test_plugin_path_loading_contracts.py#L249-L258)，读出 `check_rollout_sample_filter_path` 对它的形状要求：前两位参数必须是 `(args, groups)`，其中 `groups` 是 `list[list[Sample]]`，调用后至少要有一个样本的 `remove_sample` 被置真。
2. **写实现**（示例代码）：在 `myproj/myrollout.py` 加：
   ```python
   def my_sample_filter(args, groups):
       for group in groups:
           if group:
               group[-1].remove_sample = True   # 把每组最后一条标记剔除
   ```
3. **CPU 自检**（4.3 的闭环）：
   ```bash
   export SLIME_CONTRACT_ROLLOUT_SAMPLE_FILTER_PATH=myproj.myrollout.my_sample_filter
   python tests/plugin_contracts/test_plugin_path_loading_contracts.py
   ```
   预期：`rollout_sample_filter` 这条参数化用例（`test_path_loading_path_aligns_with_expected_format[rollout_sample_filter]`）通过。
4. **接入 CI 心智**：回答——这个文件若被你改动并提交，该贴哪个标签让它在你**自己的** PR 上跑一遍？答：贴 `run-ci-changed`（因为它是 `tests/plugin_contracts/test_*.py`，会被 `git diff` 捕获，并用其 `NUM_GPUS = 0` 走 CPU 路径）；它同时也在常驻的 `cpu-unittest` 里，所以即使不贴标签，每个 PR 也会自动跑。
5. **理解边界**：写一句话说明「契约测试通过」能保证什么、不能保证什么。

**预期结果**：你能够独立判断「任意一个 `--xxx-path` 接口」需要什么形状、用哪个契约文件自检、在 CI 里由哪个标签/任务覆盖。

> 待本地验证：步骤 3 的用例名取决于 pytest 对参数化 id 的生成；若 `remove_sample` 不是 `Sample` 的合法属性，参考 `reference_rollout_sample_filter` 的写法（它确实用了 `sample.remove_sample = True`，说明运行期靠动态属性传递剔除信号）。

## 6. 本讲小结

- slime 的测试与 CI 是**两层**：常驻 CPU 层（`cpu-unittest` / `agent-test`，无需标签、不抢卡）负责把不变量快速拦下；标签触发的 GPU e2e 层（`run-ci-megatron` 等）负责验证真实训练/采样行为。**marker（pytest）与 label（GitHub Actions）是两套独立机制**，别混淆。
- `pyproject.toml` 是 pytest 中枢：`testpaths=["./tests"]` 限定发现范围、`norecursedirs` 排除 examples/scripts/tools 等、`--strict-markers` 让未声明的标签**启动即报错**。
- `tests/plugin_contracts/` 用 **4 个文件**守护四类 hook 的形状契约：custom_generate / rollout_function / runtime hook / path loading + custom_rm，每个文件都靠 `_shared.py` 的桩模块在纯 CPU 上运行。
- 契约测试用**三层断言**：① `inspect.signature` 比对参数顺序（签名匹配）；② 用假数据真调一次并校验返回结构（最小调用验返回）；③ 对 runtime hook 额外读框架源码文本，确保调用点字符串没被重构掉（调用点稳定）。
- **`SLIME_CONTRACT_*` + `load_function`** 构成自检闭环：设环境变量（或传 `--xxx-path`）把你的实现喂给契约测试，几秒钟、无 GPU 即可确认形状合格；但它只保证「不会被框架咬」，不保证语义正确。
- `run-ci-changed` 按 `git diff` 动态发现改动的 `tests/test_*.py` 与 `tests/plugin_contracts/test_*.py`，并 grep 每个文件的 `NUM_GPUS`——所以纯 CPU 测试务必写 `NUM_GPUS = 0`。

## 7. 下一步学习建议

- **回到接口主线**：本讲验证的是「形状」，真正每种 hook 的**语义**与最佳实践见 [u6-l2 自定义生成函数](u6-l2-custom-generate-function.md)、[u6-l3 自定义奖励与转换](u6-l3-custom-reward-and-conversion.md)、[u6-l5 自定义损失与 off-policy](u6-l5-custom-loss-offpolicy.md)。建议挑一个接口，先写实现 → 跑本讲的契约自检 → 再读对应讲义补语义。
- **想给 slime 加测试**：照 [docs/en/developer_guide/ci.md:L96-L121](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/docs/en/developer_guide/ci.md#L96-L121) 的「Writing a New Test」流程——CPU 测试加 `NUM_GPUS=0` 与 `if __name__=="__main__": raise SystemExit(pytest.main([__file__]))`，再在 `.github/workflows/pr-test.yml.j2` 注册并运行 `generate_github_workflows.py` 重新生成 yaml。
- **深入 GPU e2e 范式**：读 [tests/test_qwen2.5_0.5B_short.py](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/tests/test_qwen2.5_0.5B_short.py) 的 `prepare()/execute()` 结构，对照 [tests/ci/gpu_lock_exec.py:L201-L221](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/tests/ci/gpu_lock_exec.py#L201-L221) 的 `FdLock` 理解多任务如何在同一台 GPU 机上用 `/dev/shm` 文件锁互斥抢卡。
- **可观测性配合**：当 GPU e2e 测试出现吞吐异常时，契约测试帮不上忙（它不跑真实流程），此时转向 [u8-l4 可观测性](u8-l4-observability-trace-metrics.md) 的 trace/指标/profile 三件套定位瓶颈。
