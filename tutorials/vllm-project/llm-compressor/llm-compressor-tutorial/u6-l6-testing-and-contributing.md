# 测试与贡献流程

## 1. 本讲目标

本讲是全手册的收官篇，承接 u6-l4（自定义 Modifier）。你已经学会「写一个新的压缩算法」，本讲要回答最后一个问题：**怎么验证它是对的、怎么让它通过项目的质量门、怎么把它贡献回仓库**。

学完后你应当能够：

- 读懂 `pyproject.toml` 里声明的七种 pytest marker，知道每种 marker 的语义，并能在本地按 marker 选择性跑测试。
- 读懂 `tests/testing_utils.py` 里 `requires_gpu` / `requires_gpu_mem` / `requires_compute_capability` / `torchrun` 这套「硬件门槛装饰器」，理解 `multi_gpu` 这类标记如何与它们配合。
- 用 `Makefile` 的 `quality` / `style` / `test` / `test-xpu` 四个目标在本地完成「格式化 → 检查 → 测试」一整套动作。
- 读懂 `.buildkite/` 下 CI 如何把测试拆成 base / transformers / xpu 三条线，并理解变更检测（`if_changed`）与覆盖率合并。
- 理解 `ruff` / `mypy` 的配置约束，以及 `CONTRIBUTING.md` 里「认领工作 → 实现 → PR」的完整流程，并能把一个最小单元测试跑通且通过 ruff/mypy。

## 2. 前置知识

- **pytest marker（标记）**：给测试贴的「标签」。`@pytest.mark.unit` 就是给一个测试函数贴上 `unit` 标签，之后可以用 `pytest -m unit` 只跑带这个标签的测试。marker 必须先在配置里「注册」，否则 pytest 会报警告。
- **skipif / skip**：pytest 的条件跳过机制。`@pytest.mark.skipif(条件, reason=...)` 在条件为真时跳过该测试，而不是让它失败。这是「没有 GPU 就不跑 GPU 测试」的标准做法。
- **torchrun**：PyTorch 启动多卡（多进程）分布式任务的命令行工具，`torchrun --nproc_per_node=2 ...` 会拉起 2 个进程，每个进程对应一张卡（一个 rank）。
- **ruff / mypy**：ruff 是极快的 Python 静态检查 + 格式化工具（同时取代 flake8/isort/black 的部分功能）；mypy 是静态类型检查器。二者都是「提交前必须过」的门槛。
- **Buildkite**：项目使用的 CI 服务，配置以 YAML 形式存放在仓库的 `.buildkite/` 目录。
- 建议先读 u6-l4，理解一个 Modifier 的「钩子」长什么样，本讲的综合实践就是给它补一个单元测试。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `pyproject.toml` | 注册 pytest marker、配置 ruff 与 mypy，是「标记清单」与「风格规则」的唯一事实来源。 |
| `Makefile` | 本地开发的四个入口：`quality`（检查）、`style`（格式化）、`test`（跑测试）、`test-xpu`（跑 XPU 测试）。 |
| `tests/testing_utils.py` | 一套硬件门槛装饰器：`requires_gpu` / `requires_gpu_mem` / `requires_compute_capability` / `torchrun` / `requires_hf_token`。 |
| `setup.py` | `extras_require["dev"]` 定义测试与 lint 的全部依赖（pytest、ruff、mypy 等）。 |
| `.buildkite/pipeline.yml` | CI 总入口，分发到 GPU 与 XPU 两条线。 |
| `.buildkite/gpu-tests/scripts/run-tests.sh` | GPU CI 的核心脚本，区分 base / transformers 两类测试，安装 nightly compressed-tensors。 |
| `.buildkite/gpu-tests/gpu-tests-H100.yml` | H100 上的测试分组、Python 矩阵、覆盖率合并、transformers 变更检测。 |
| `tools/lint_cuda.py` | 自定义 AST 检查器，禁止直接用 `torch.cuda.*`，建议改用 `torch.accelerator.*`。 |
| `CONTRIBUTING.md` | 贡献流程：认领工作、RFC、开发环境搭建、风格与测试入口。 |

## 4. 核心概念与源码讲解

### 4.1 测试标记体系：pyproject.toml 里的七种 marker

#### 4.1.1 概念说明

一个压缩库的测试天然分很多种：有的一秒跑完只检查纯函数（单元测试），有的要加载真实大模型跑一遍量化（回归测试），有的要联网下载 HuggingFace 模型（集成测试），有的要两张以上 GPU（多卡测试）。如果每次都全跑，开发者等不起；如果手动挑文件跑，又容易漏。

pytest 的 **marker（标记）** 机制就是为解决这个问题：给每个测试贴一个或多个标签，再用 `-m` 表达式按标签筛选。项目把所有合法标签集中注册在 `pyproject.toml`，既是「允许使用的标签清单」，也防止拼错标签时 pytest 静默忽略。

#### 4.1.2 核心流程

1. 在 `pyproject.toml` 的 `[tool.pytest.ini_options].markers` 下声明每个标签及其一句话说明。
2. 在测试函数上贴标签：`@pytest.mark.unit`。
3. 跑测试时用 `-m` 筛选：`pytest -m unit`（只跑 unit）、`pytest -m "not multi_gpu"`（排除多卡）、`pytest -m "unit and not integration"`（组合）。
4. 没有在配置里注册的标签，pytest 会在收集阶段发出 `PytestUnknownMarkWarning`。

#### 4.1.3 源码精读

marker 的唯一事实来源是 `pyproject.toml`：

[pyproject.toml:17-27](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/pyproject.toml#L17-L27) 注册了七种 marker，每个都带一句话语义说明。逐条含义如下（按声明顺序）：

| marker | 声明语义 | 实际用途 |
|--------|----------|----------|
| `smoke` | 快速检查基本功能 | 较少，主要在 `tests/unit/core/` 等轻量套件 |
| `sanity` | 保证新改动不破坏已有功能 | **声明但实际几乎未使用** |
| `regression` | 详细验证主要功能正确 | 极少（仅 1 处） |
| `integration` | 接入第三方服务（如 HF） | 较多，多见于需要加载真实模型的测试 |
| `unit` | 验证代码正确性与回归 | **使用最多**，遍布 `tests/llmcompressor/modifiers/` |
| `example` | 针对 `examples/` 目录的测试 | `tests/examples/` |
| `multi_gpu` | 需要多张 GPU | 分布式测试，配合 `requires_gpu(N)` |

> **重要提醒（不编造原则）**：marker 的「声明」和「实际使用」并不对等。`sanity` 虽然在配置里声明，但在当前 `tests/` 下**没有任何测试实际贴了它**；而 `unit` 是被贴得最多的标签（上百处）。所以「按 marker 筛选」时，筛选 `sanity` 会选中 0 个测试，筛选 `unit` 会选中一大批。阅读源码时不要假设「声明了就一定有人用」。

同一节里还有一个值得注意的配置：

[pyproject.toml:27](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/pyproject.toml#L27) `tmp_path_retention_policy = "failed"` 表示只有失败的测试才保留其临时目录，避免磁盘被一堆成功的临时文件塞满。

再看一个真实的、纯 CPU 可跑的单元测试如何贴标签。`LengthAwareSampler` 的测试类里每个方法都贴了 `unit`：

[tests/llmcompressor/datasets/test_length_aware_sampler.py:17-21](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/tests/llmcompressor/datasets/test_length_aware_sampler.py#L17-L21) 给 `test_batch_size_parameter` 贴上 `@pytest.mark.unit`，断言采样器的 `batch_size` 被正确设置。这类测试不依赖 GPU、不联网，是本地快速验证的首选。

#### 4.1.4 代码实践

1. **实践目标**：用 marker 筛选本地可跑的测试，观察筛选效果。
2. **操作步骤**：
   - 只收集（`--co`，不实际执行）带 `unit` 标签的测试，看看有多少：
     ```bash
     pytest -m unit --co -q tests/llmcompressor/datasets/
     ```
   - 同样收集 `sanity` 标签的测试：
     ```bash
     pytest -m sanity --co -q tests/
     ```
   - 实际跑一个纯 CPU 的 unit 测试并看详细输出：
     ```bash
     pytest -m unit -v tests/llmcompressor/datasets/test_length_aware_sampler.py
     ```
3. **需要观察的现象**：`-m unit` 能收集到一批测试；`-m sanity` 收集到 **0 个**（因为该标签当前未被实际使用）；最后一条命令应全部通过。
4. **预期结果**：理解「marker 是过滤用的标签，但标签是否真被使用要回到源码确认」。如果环境无 GPU/无网络，请只用上面这种纯 CPU 测试验证。
5. 若本地无法运行，标注「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：为什么要在 `pyproject.toml` 里声明 marker，而不是让开发者随便贴？

**答案**：声明清单让 pytest 把「未注册的标签」识别为警告，从而捕捉拼写错误（比如把 `unit` 写成 `uint`）；同时集中的清单本身就是给团队的「可用标签字典」，避免每个人发明自己的标签。

**练习 2**：写一个 `-m` 表达式，选出「需要多卡、但又排除集成测试」的用例。

**答案**：`pytest -m "multi_gpu and not integration"`。

---

### 4.2 硬件门槛：requires_gpu 与 torchrun 装饰器

#### 4.2.1 概念说明

`multi_gpu` 这个 marker 只是「贴个标签」，它本身**不会**让测试在没卡时跳过。真正决定「没卡就跳过」的是 `tests/testing_utils.py` 里的一套装饰器。理解这一点很关键：一个分布式测试通常同时挂着三件东西——

- `@pytest.mark.multi_gpu`：标签，便于人/CI 按类别筛选；
- `@requires_gpu(2)`：门槛，机器不够 2 张卡就 `skipif` 跳过；
- `@torchrun(world_size=2)`：执行方式，自动用 `torchrun` 拉起多进程。

三者职责不同，缺一不可。

#### 4.2.2 核心流程

`requires_gpu` 的判定逻辑：

1. 调 `torch.accelerator.device_count()` 得到可用加速器数量。
2. 与所需数量比较，不足则构造一个 `pytest.mark.skipif(...)` 装饰器。
3. 装饰器既支持「裸用」（默认要求 1 张卡），也支持「带参」（`requires_gpu(2)` 要求 2 张）。

`torchrun` 装饰器则实现「单进程调试、多进程执行」的双面性：

1. 主进程（普通 pytest）被调用时，它**重新用 `torchrun` 拉起子进程**，把当前测试函数作为目标。
2. 子进程里（靠 `TORCHELASTIC_RUN_ID` 环境变量识别）真正执行测试，可选地自动 `init_dist()`。

#### 4.2.3 源码精读

`_enough_gpus` 与 `requires_gpu` 是门槛的核心：

[tests/testing_utils.py:301-338](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/tests/testing_utils.py#L301-L338) `_enough_gpus` 用 `torch.accelerator.device_count()` 数卡（注意用的是 `accelerator` 而非 `cuda`，这与本讲 4.5 要讲的 `lint_cuda.py` 一致）；`requires_gpu` 通过判断参数是不是 `int` 来区分「带参 vs 裸用」，最终产出 `pytest.mark.skipif(not _enough_gpus(n), reason=...)`。

除「卡数」外，还有「显存」与「算力版本」两道更细的门槛：

[tests/testing_utils.py:348-395](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/tests/testing_utils.py#L348-L395) `requires_gpu_mem(required_amount)` 按吉字节要求总显存（注释特别提醒 H100 标称 80GiB 实测约 79.2GiB）；`requires_compute_capability(major, minor)` 按算力版本跳过——例如 `requires_compute_capability(9, 0)` 表示「需要 H100 及以上」。

`torchrun` 装饰器把「写分布式测试」降到「写普通函数」：

[tests/testing_utils.py:398-486](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/tests/testing_utils.py#L398-L486) 主进程里它拼出一条 `python -m torch.distributed.run --nproc_per_node N -m pytest "文件::函数" -sx` 的命令并 `subprocess.run`；若当前已在 torchrun 子进程内（检测到 `TORCHELASTIC_RUN_ID`），则按 `init_dist` 参数决定是否自动初始化进程组后直接跑函数。这就解释了为什么开发者用普通 `pytest` 命令就能触发多卡测试。

来看一个把三者叠在一起的真实分布式测试：

[tests/llmcompressor/modifiers/quantization/test_quantization_ddp.py:19-22](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/tests/llmcompressor/modifiers/quantization/test_quantization_ddp.py#L19-L22) 依次挂 `@pytest.mark.multi_gpu`（分类）、`@requires_gpu(2)`（门槛）、`@torchrun(world_size=2)`（拉起 2 进程）。机器只有 1 张卡时，`requires_gpu(2)` 会让它被跳过而不是失败——这正是 H100 CI 注释里说的「1 卡时多卡测试被 skip」的原因。

> 补充：还有一道「数据门槛」[tests/testing_utils.py:489-492](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/tests/testing_utils.py#L489-L492) `requires_hf_token`，在环境变量 `HF_TOKEN` 缺失时跳过那些需要 gated 模型（受限模型）访问的测试。

#### 4.2.4 代码实践

1. **实践目标**：在不实际拥有多卡的情况下，理解这些测试如何被「收集但跳过」。
2. **操作步骤**：
   - 只收集 `multi_gpu` 标签的测试（不执行）：
     ```bash
     pytest -m multi_gpu --co -q tests/llmcompressor/modifiers/quantization/test_quantization_ddp.py
     ```
   - 真正执行它（单卡或无卡环境）：
     ```bash
     pytest -v tests/llmcompressor/modifiers/quantization/test_quantization_ddp.py
     ```
3. **需要观察的现象**：第一条命令能列出测试名；第二条命令里这些测试应显示为 `SKIPPED`，原因是「Not enough GPUs available, 2 GPUs required」。
4. **预期结果**：体会「marker 负责分类、`requires_gpu` 负责门槛」的分工——标签让测试被**选中**，门槛让它在条件不满足时被**跳过**。
5. 若本地无 GPU 环境无法运行，标注「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：如果删掉 `@requires_gpu(2)` 只保留 `@torchrun(world_size=2)`，在单卡机器上会发生什么？

**答案**：`torchrun` 仍会尝试拉起 2 个进程争抢同一张卡，大概率因显存不足或设备争用而**报错失败**，而不是优雅跳过。门槛装饰器的作用就是把「硬件不满足」从「失败」降级为「跳过」。

**练习 2**：`torchrun` 装饰器是靠哪个环境变量判断「我现在是不是已经被 torchrun 拉起的子进程」？

**答案**：`TORCHELASTIC_RUN_ID`（见源码 `if "TORCHELASTIC_RUN_ID" in os.environ` 分支）。

---

### 4.3 Makefile：本地开发的四个入口

#### 4.3.1 概念说明

`Makefile` 把「检查、格式化、测试」这几件最常做的事固化成四条命令，开发者不用记一长串 ruff/pytest 参数。它同时通过一套 `TARGETS` / `PYTEST_ARGS` 的小机制，允许你按需排除某些耗时很长的测试目录（transformers、examples、sparsity）。

#### 4.3.2 核心流程

- `make quality`：只检查不改动——`ruff check` + `ruff format --check` + `lint_cuda.py`（CUDA 用法检查）。
- `make style`：直接改文件——`ruff format` + `ruff check --fix` + 再 format 一次 + `lint_cuda.py --fix`。
- `make test`：跑 `pytest tests`，并按 `TARGETS` 决定是否忽略 `tests/llmcompressor/transformers`、`tests/examples`、`tests/sparsity`。
- `make test-xpu`：用独立的 `pytest-xpu.ini` 跑 Intel XPU 测试。

贡献前典型流程是 `make style` → `make quality` → `make test`。

#### 4.3.3 源码精读

`CHECKDIRS` 定义了所有检查/格式化作用范围：

[Makefile:1-2](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/Makefile#L1-L2) 把 `src tests examples setup.py` 设为检查目录，意味着 ruff/lint_cuda 会覆盖源码、测试、示例和打包脚本。

`TARGETS` 与 `PYTEST_ARGS` 的「按需忽略」机制：

[Makefile:11-21](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/Makefile#L11-L21) 默认 `TARGETS` 为空，于是三处 `findstring` 都匹配失败，`PYTEST_ARGS` 被追加三个 `--ignore`，即默认**忽略** transformers/examples/sparsity 三类测试。若你 `make test TARGETS=transformers`，则 transformers 那条 ignore 不再生效，transformers 测试被纳入。这是一种用 make 变量开关测试范围的轻量做法。

`quality` 与 `style` 两个目标：

[Makefile:25-39](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/Makefile#L25-L39) `quality` 是「只读检查」（`--check`、`--fail-on-issues`），CI 用它做门槛；`style` 是「写入修复」（`--fix`），本地开发用。注释解释了为何 `ruff format` 要跑两次：第一次先把长行折行，再让 `ruff check --fix` 修 lint，最后再 format 一次修掉 fix 引入的格式问题。

`test` 与 `test-xpu`：

[Makefile:42-49](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/Makefile#L42-L49) `test` 用 `pytest -ra tests ... --ignore tests/lmeval --ignore tests/tools`（`-ra` 表示在摘要里报告所有除通过外的结果，即跳过/失败都会列出来）；`test-xpu` 则切到独立配置文件 `pytest -c pytest-xpu.ini`，该文件 [pytest-xpu.ini:1-10](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/pytest-xpu.ini#L1-L10) 只跑三个量化相关的测试文件，是一个精简的 XPU 冒烟集。

自定义 lint 工具 `tools/lint_cuda.py` 的作用：

[tools/lint_cuda.py:2-7](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/tools/lint_cuda.py#L2-L7) 它是一个 AST 扫描器，目的是把代码里直接的 `torch.cuda.*` 调用找出来，建议改用设备无关的 `torch.accelerator.*`。这正是前文 `requires_gpu` 用 `torch.accelerator.device_count()` 而非 `torch.cuda.device_count()` 的原因——项目要求代码对 CUDA/XPU 都兼容。

#### 4.3.4 代码实践

1. **实践目标**：用 Makefile 跑通「检查 → 测试」本地闭环。
2. **操作步骤**：
   ```bash
   make quality        # 只读检查，应无报错
   make test TARGETS="" # 跑默认测试集（已忽略 transformers/examples/sparsity）
   ```
   如果只关心某一个小目录，也可直接：
   ```bash
   pytest -ra tests/llmcompressor/observers/test_min_max.py -v
   ```
3. **需要观察的现象**：`make quality` 输出 "Running python quality checks" 后无报错退出；`make test` 会因机器是否有 GPU/网络而出现不同数量的 `SKIPPED`。
4. **预期结果**：`-ra` 摘要里能看到 `S`（skipped）和 `.`（passed）的统计，理解默认测试集已排除三类重测试。
5. 若本地未安装 dev 依赖，标注「待本地验证」（需先 `pip install -e ./[dev]`，见 4.5）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `make style` 里 `ruff format` 要执行两次？

**答案**：第一次 format 先把超长行折行，给随后的 `ruff check --fix` 一个干净的输入；`--fix` 可能引入新的格式瑕疵，所以最后再 format 一次收尾。

**练习 2**：想临时把 `tests/examples` 纳入测试范围，该怎么做？

**答案**：`make test TARGETS=examples`——`findstring` 命中后，对应的 `--ignore tests/examples` 不再被追加。

---

### 4.4 CI 流水线：.buildkite 如何组织 GPU / transformers / xpu 测试

#### 4.4.1 概念说明

本地 `make test` 跑的是默认子集；CI 则要在真实 GPU（H100/L4）和 Intel XPU 上把更重的测试也跑一遍，还要兼顾「省机器」——只在相关代码变更时才触发某些昂贵的线。`.buildkite/` 用一个总入口加若干子配置实现这件事。

#### 4.4.2 核心流程

1. `.buildkite/pipeline.yml` 是总入口，分两个 step：GPU 测试（默认 H100）与 XPU 测试。
2. GPU step 根据元数据 `test-runner` 选择具体配置（如 `gpu-tests-H100.yml`）。
3. `gpu-tests-H100.yml` 把测试拆成 **Base Tests**（`make test`）与 **Transformers Tests**（逐文件跑 `tests/llmcompressor/transformers`），后者还有一道「变更检测」：只有改了 `src/`、`tests/` 等相关路径才触发。
4. `run-tests.sh` 负责：建 uv 虚拟环境 → 装 `.[dev]` → 装 nightly compressed-tensors → 按 `base`/`transformers` 分支跑测试 → 可选地收集覆盖率。
5. XPU step 用 `if_changed` 仅在 `modifiers` 相关路径变更时才触发。

#### 4.4.3 源码精读

总入口分发到两条线：

[.buildkite/pipeline.yml:1-23](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/.buildkite/pipeline.yml#L1-L23) 第一个 step 读 `test-runner` 元数据（默认 `H100`）再 `pipeline upload` 对应的 `gpu-tests-$RUNNER.yml`；第二个 step 是 XPU 测试，带 `if_changed` 列表 `src/llmcompressor/modifiers/**`、`tests/llmcompressor/modifiers/**`、`.buildkite/xpu-tests/**`——即只有这些路径有改动，XPU 这条昂贵的线才会跑。

`run-tests.sh` 是 GPU 测试的执行核心：

[.buildkite/gpu-tests/scripts/run-tests.sh:4-5](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/.buildkite/gpu-tests/scripts/run-tests.sh#L4-L5) 接受 `base` 或 `transformers` 两种 `TEST_TYPE`。

[.buildkite/gpu-tests/scripts/run-tests.sh:43-50](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/.buildkite/gpu-tests/scripts/run-tests.sh#L43-L50) 关键细节：CI 会 clone compressed-tensors 仓库并以 `BUILD_TYPE=nightly` 安装，确保 llm-compressor 对 compressed-tensors 的「未来改动」提前适配。这也是 CONTRIBUTING 里建议本地也「从源码装 compressed-tensors」的背景。

[.buildkite/gpu-tests/scripts/run-tests.sh:56-68](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/.buildkite/gpu-tests/scripts/run-tests.sh#L56-L68) `base` 分支直接 `make test`；`transformers` 分支则**逐文件**跑 `tests/llmcompressor/transformers` 下的每个 `test_*.py`（用 `find ... | sort`），每文件独立进程、失败不中断后续（`TEST_EXIT_CODE` 暂存），并在开启覆盖率时用 `--cov-append` 串联。

`gpu-tests-H100.yml` 的分组、矩阵与变更检测：

[.buildkite/gpu-tests/gpu-tests-H100.yml:1-4](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/.buildkite/gpu-tests/gpu-tests-H100.yml#L1-L4) 开头注释直接点明：多卡测试（如 `test_quantization_ddp.py` 带 `@requires_gpu(2)`、torchrun `--nproc_per_node=2` 的 example）在只有 1 张 GPU 时会被 **skip**，`tests/examples/` 与 `tests/sparsity/` 也不在 base 套件里；要真正跑多卡测试需单独申请 `nvidia.com/gpu: 2`。

[.buildkite/gpu-tests/gpu-tests-H100.yml:70-72](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/.buildkite/gpu-tests/gpu-tests-H100.yml#L70-L72) Base Tests 在 Python `["3.10", "3.13"]` 两个版本上做矩阵测试，保证兼容性下限与上限。

[.buildkite/gpu-tests/gpu-tests-H100.yml:88-111](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/.buildkite/gpu-tests/gpu-tests-H100.yml#L88-L111) "Detect changes for Transformers Tests" 这一步用 `git diff --name-only` 检查 PR 相对基线改了哪些文件，只有命中 `src/`、`tests/`、`setup.py`、`MANIFEST.in` 或 transformers CI 脚本，才 `upload` transformers 测试；`.md` 文档改动不触发。这是 CI 省 GPU 机器的关键。

XPU 测试在 Docker 里跑：

[.buildkite/xpu-tests/scripts/run-tests-xpu.sh:18-36](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/.buildkite/xpu-tests/scripts/run-tests-xpu.sh#L18-L36) 在专用镜像容器里 `uv pip install .[dev]`（带 XPU 版 torch 的 extra-index），再 `make test-xpu`，并用 `numactl` 绑定 NUMA 节点与 CPU 集，确保 XPU 测试在确定性的 NUMA 拓扑下执行。

#### 4.4.4 代码实践

1. **实践目标**：用源码阅读的方式还原一次 PR 在 CI 上会触发哪些测试线。
2. **操作步骤**：
   - 假设你的 PR 只改了 `src/llmcompressor/observers/min_max.py` 和对应测试。对照 `pipeline.yml` 与 `gpu-tests-H100.yml` 的 `if_changed` / `git diff` 规则，逐条判断：Base Tests 跑吗？Transformers Tests 跑吗？XPU Tests 跑吗？
   - 再假设 PR 只改了 `src/llmcompressor/modifiers/gptq/base.py`，重新判断 XPU 线是否触发。
3. **需要观察的现象**：第一种情况下 XPU 线**不触发**（observers 不在 `if_changed` 的 `modifiers/**` 里），Transformers 线**触发**（命中 `src/`）；第二种情况下 XPU 线**触发**。
4. **预期结果**：画出「改动路径 → 触发的 CI 线」对照表，理解变更检测如何省机器。
5. 本实践为源码阅读型，无需运行命令；结论可对照 YAML 里的 glob 模式逐一验证。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `transformers` 测试要「逐文件」跑，而不是一次性 `pytest tests/llmcompressor/transformers`？

**答案**：逐文件跑能让单个测试文件的崩溃/OOM 不影响其它文件（脚本用 `|| TEST_EXIT_CODE=$?` 暂存失败码后继续），便于定位是哪个集成测试出问题；代价是启动开销更大，但 transformers 测试本就重，这种隔离是值得的。

**练习 2**：CI 为什么要把 compressed-tensors 装成 nightly？

**答案**：llm-compressor 紧依赖 compressed-tensors 的序列化格式与算子，装 nightly 可提前发现未来 compressed-tensors 改动对本库的破坏（上下游协同的「前置适配」）。

---

### 4.5 代码风格（ruff / mypy）与贡献流程

#### 4.5.1 概念说明

代码风格门槛（ruff + mypy）和贡献流程（CONTRIBUTING）是「让外部贡献能被合并」的两块基石。ruff 管「格式 + 基础 lint」，mypy 管「类型正确性」，二者配置都在 `pyproject.toml`；测试与 lint 的依赖都在 `setup.py` 的 `dev` extras 里。`CONTRIBUTING.md` 则规定了「先认领、再实现、走 PR」的协作礼仪，以及大型特性要走 RFC。

#### 4.5.2 核心流程

1. 安装开发环境：`pip install -e ./[dev]`（同时拉起 ruff/mypy/pytest）。
2. 写代码 → `make style` 自动格式化 → `make quality` 检查 → `make test` 测试。
3. 找/建 issue → 评论认领 → 等维护者分配（绿灯）→ 实现 → 提 PR。
4. 大型特性先提交 RFC（feature request 模板 + 标注 RFC + Slack 讨论）。

#### 4.5.3 源码精读

ruff 与 mypy 的配置集中在 `pyproject.toml`：

[pyproject.toml:5-6](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/pyproject.toml#L5-L6) mypy 的检查范围是 `files = "src/llmcompressor"`，即**只对源码做类型检查、不强制测试目录**（这也是 Makefile 里 `quality` 注释 "leaving out mypy src for now" 的体现——`make quality` 当前主要跑 ruff）。

[pyproject.toml:8-15](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/pyproject.toml#L8-L15) ruff 配置：`line-length = 88`（一行最多 88 字符）；`lint.select = ["E", "F", "W", "I"]` 启用 pycodestyle 错误/警告、Pyflakes、isort 四类规则；`lint.extend-ignore = ["E203", "W605"]` 放宽两条；isort 里 `known-first-party = ["llmcompressor"]` 告诉 isort 把 `llmcompressor` 当作本仓库第一方包来排序 import。另外 `extend-exclude` 排除了 `tracing/` 和 `version.py`。

测试与 lint 依赖在 `setup.py` 的 `dev` extras：

[setup.py:150-174](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/setup.py#L150-L174) `dev` 分四组：测试框架（`pytest>=6.0.0`、`pytest-mock`、`pytest-rerunfailures`）、测试依赖（beautifulsoup4、trl、pandas、torchvision、librosa 等，用于跑各类集成测试）、lint/类型（`mypy~=1.10.0`、`ruff~=0.4.8`，注意版本用 `~` 锁定到兼容小版本）、pre-commit 与文档工具。所以 `pip install -e ./[dev]` 一步到位拿到所有开发工具。

贡献流程在 `CONTRIBUTING.md`：

[CONTRIBUTING.md:36-52](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/CONTRIBUTING.md#L36-L52) "Claiming Work" 四步：找/建 issue（看 `good first issue` 标签）→ 评论说想做并简述方案 → 等维护者分配（「绿灯」）→ 开始实现。强调**先沟通再动手**，避免重复劳动和方向偏离。一周没回复可以礼貌催一下。

[CONTRIBUTING.md:54-64](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/CONTRIBUTING.md#L54-L64) 大型特性要走 RFC（Request for Comments）：用 feature request 模板建 issue 并标注 RFC，说明动机、解决的问题、备选方案、拟议改动，然后到 vLLM Slack 的 `#llm-compressor` 频道讨论；高关注度特性会指派一名 committer 作为 DRI（Directly Responsible Individual）来推进决策。

[CONTRIBUTING.md:66-92](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/CONTRIBUTING.md#L66-L92) "Setup for development"：`pip install -e ./[dev]`（并建议本地也从源码装 compressed-tensors），然后 `make style` / `make quality` / `make test`。警告：跑全部测试很慢，且部分测试需要多张 GPU。

> 补充：`docs/developer-tutorials/` 下有 [add-modifier.md](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/docs/developer-tutorials/add-modifier.md)、`add-observer.md`、`add-moe-support.md` 等开发教程，CONTRIBUTING 顶部就指向了它们——这是「贡献一个新算法」最直接的入门资料，与本手册的 u6-l4 / u6-l5 互为补充。

#### 4.5.4 代码实践

1. **实践目标**：验证你写的代码能通过 ruff/mypy 门槛。
2. **操作步骤**：
   - 装 dev 依赖：`pip install -e ./[dev]`
   - 故意写一行超长且 import 乱序的小文件，例如：
     ```python
     # 示例代码：故意不合规
     import torch
     import os
     from llmcompressor.modifiers import Modifier
     def foo(very_long_parameter_name_a, very_long_parameter_name_b, very_long_parameter_name_c): pass
     ```
   - 跑 `ruff check <文件>` 与 `ruff format --check <文件>`，观察报错；再 `ruff format <文件>` + `ruff check --fix <文件>` 自动修复，看它如何把 `os`/`torch`/`Modifier` 的 import 顺序整理成「标准库→第三方→第一方」。
3. **需要观察的现象**：isort 会把 `os`（标准库）排在 `torch`（第三方）前，`llmcompressor`（第一方）排最后；超长行被 format 折成多行。
4. **预期结果**：体会「`make style` 能自动解决大部分风格问题，`make quality` 是不可自动修的只读门槛」。
5. 若本地未装 ruff，标注「待本地验证」。

#### 4.5.5 小练习与答案

**练习 1**：mypy 的检查范围是整个仓库吗？

**答案**：不是。[pyproject.toml] 里 `files = "src/llmcompressor"`，mypy 只检查源码目录，不强制 `tests/`。

**练习 2**：为什么 isort 需要知道 `known-first-party = ["llmcompressor"]`？

**答案**：isort 把 import 分三段排序（标准库 → 第三方 → 第一方），必须告诉它 `llmcompressor` 是本仓库的第一方包，否则它会被误当作第三方包和 `torch` 混排在一起。

---

## 5. 综合实践

把本讲的知识串起来，完成一件「贡献者每天都会做的小事」：**给一个已有 modifier 补一个最小单元测试，并让它通过质量门**。

1. **实践目标**：跑通「读测试 → 写测试 → 跑测试 → 过风格检查」的完整闭环。
2. **操作步骤**：
   - 先在本地装好环境：`pip install -e ./[dev]`。
   - 跑一组现成的 smoke/unit 测试作为热身，阅读其输出（注意 `-ra` 摘要里的 passed/skipped 统计）：
     ```bash
     pytest -m unit -v tests/llmcompressor/datasets/test_length_aware_sampler.py
     pytest -v tests/llmcompressor/observers/test_min_max.py
     ```
   - 选一个已有 modifier 的纯函数补一个最小单元测试。例如 GPTQ 的 `make_empty_hessian` / `quantize_weight` 就是很好的纯函数目标（参考 [tests/llmcompressor/modifiers/gptq/test_gptq_quantize.py:21-54](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/tests/llmcompressor/modifiers/gptq/test_gptq_quantize.py#L21-L54) 的写法）。新建一个测试函数，给它贴 `@pytest.mark.unit`，断言一个**已知正确**的不变量（如 `quantize_weight` 返回的 `loss >= 0`）。
   - 跑你新加的测试确认通过：
     ```bash
     pytest -m unit -v <你的测试文件>
     ```
   - 过风格与类型门槛：
     ```bash
     make style
     make quality
     ```
   - 对照 [CONTRIBUTING.md:36-52](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/CONTRIBUTING.md#L36-L52) 的流程：在 issue 区找一个 `good first issue`（或描述你想补的测试覆盖缺口）→ 评论说明 → 等绿灯 → 提 PR。
3. **需要观察的现象**：新测试被 `-m unit` 选中并通过；`make quality` 无报错；`make style` 不再改动你的文件（说明已合规）。
4. **预期结果**：你产出一个「带 `unit` 标签、通过 ruff/mypy、能在单 CPU 上秒级跑完」的测试，符合 CI 的 Base Tests 要求。
5. 如果 `quantize_weight` 的输入构造在本地难以确定，可直接照搬参考测试里的构造方式；若环境跑不起来，标注「待本地验证」。

> 这一步把本手册多个讲义串了起来：u4-l1（GPTQ 算法）提供被测对象、u6-l4（自定义 Modifier）提供扩展点视角、本讲提供验证与贡献流程。一个能被合并的算法贡献 = 正确的实现 + 覆盖它的单元测试 + 通过风格门。

## 6. 本讲小结

- **marker 是过滤用的标签**，七种 marker 集中注册在 `pyproject.toml`；但声明不等于使用——`unit` 用得最多，`sanity` 实际未被使用，筛选结果要回到源码确认。
- **标签与门槛是两回事**：`@pytest.mark.multi_gpu` 负责分类，`@requires_gpu(N)` / `requires_gpu_mem` / `requires_compute_capability` 负责让硬件不满足时优雅跳过，`@torchrun` 负责自动拉起多进程。
- **Makefile 是本地开发入口**：`quality`（只读检查）、`style`（自动修复）、`test`（默认测试集，按 `TARGETS` 开关 transformers/examples/sparsity）、`test-xpu`（独立 ini）。`make style`→`make quality`→`make test` 是贡献前的标准闭环。
- **CI 用 .buildkite 组织**：总入口分发到 GPU（H100/L4）与 XPU 两条线；GPU 线再拆 base（`make test`）与 transformers（逐文件跑），并通过 `git diff` 与 `if_changed` 做「只在相关变更时才跑昂贵测试」的省机器策略；CI 还会装 nightly compressed-tensors 做前置适配。
- **风格门槛是 ruff + mypy**：配置都在 `pyproject.toml`（行宽 88、启用 E/F/W/I、isort 识别第一方包、mypy 仅查 `src/llmcompressor`）；依赖在 `setup.py` 的 `dev` extras。
- **贡献要讲流程**：先在 issue 认领并等维护者绿灯再动手，大型特性走 RFC；`good first issue` 是新手切入点，`docs/developer-tutorials/` 是官方「加 modifier/observer」教程。

## 7. 下一步学习建议

- 本讲结束了全手册的正文。若你想真正动手贡献，建议挑一个 `good first issue`，把 u6-l4（自定义 Modifier）或 u6-l5（自定义 Observer）里写的扩展按本讲的流程补上单元测试并提 PR。
- 继续阅读源码的方向：把 `tests/llmcompressor/modifiers/` 下某个算法（如 `test_gptq_quantize.py`、`transform/awq/test_base.py`）的测试断言当作「行为规约」，反推算法应满足的不变量，这是深入理解算法实现的高效路径。
- 想理解 CI 的覆盖率合并细节，可读 `.buildkite/gpu-tests/scripts/combine-coverage.sh`，看多份 `.coverage.*` 如何经 `coverage combine` 汇总。
- 关注 `docs/developer-tutorials/` 下的 `add-modifier.md` / `add-observer.md` / `add-moe-support.md`，它们与本手册的扩展点讲义互补，是官方维护的最新贡献指南。
