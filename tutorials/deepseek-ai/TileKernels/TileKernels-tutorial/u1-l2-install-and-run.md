# 安装、运行与测试工作流

## 1. 本讲目标

上一讲（u1-l1）我们已经建立了「TileKernels 是什么、依赖什么」的心智模型。本讲专门解决「怎么把它装上、怎么跑测试、怎么读 benchmark 结果」这三件最操作性的事。

学完后你应当能够：

- 用两种方式安装 TileKernels（本地开发版 / 发布版），并能解释它们的区别。
- 看懂 `pyproject.toml` 中「运行时依赖」与「dev 开发依赖」的分工。
- 用 `pytest` 跑单文件正确性测试、benchmark 基准测试和压力测试。
- 读懂 benchmark 输出里的「延迟（us）」和「带宽（GB/s）」，并理解回归报告。
- 理解两个关键环境变量 `TK_FULL_TEST` 和 `TK_PRINT_KERNEL_SOURCE` 的作用。

> 说明：本讲涉及的所有命令都需要真实的 NVIDIA SM90/SM100 GPU 与 CUDA 环境。本讲义写作环境没有 GPU，因此涉及「实际运行」的实践步骤均标注「待本地验证」，但每条命令都来自真实源码，可在你的 GPU 机器上照抄。

## 2. 前置知识

- **pip 与可编辑安装**：`pip install -e .` 表示「以可编辑模式安装当前目录的包」，源码改动立即生效，不需要重新安装；这是本地开发的主流方式。
- **Python 打包元数据 `pyproject.toml`**：现代 Python 项目用这一个文件描述「怎么构建、依赖什么、叫什么名字」。其中 `dependencies` 是「运行时必需」，`optional-dependencies` 是「可选附加」（如 `.[dev]` 表示附带开发工具）。
- **pytest**：Python 最常用的测试框架。它通过发现 `test_` 开头的函数、`@pytest.mark` 标记、`conftest.py` 配置文件和插件来组织测试。
- **pytest 插件机制**：pytest 通过 `pytest_addoption`（加命令行参数）、`pytest_configure`（启动钩子）、fixture（测试夹具）等钩子扩展行为。本项目的测试设施大量依赖自带的 pytest 插件。
- **环境变量**：以 `TK_` 开头的是 TileKernels 自定义的环境变量，用来在「不改源码」的前提下切换测试行为。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [README.md](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/README.md) | 项目首页，含 Installation / Testing 两节的标准命令 |
| [pyproject.toml](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/pyproject.toml) | 打包元数据：运行时依赖、dev 依赖、动态版本 |
| [tests/conftest.py](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tests/conftest.py) | 根级 conftest，负责加载两个 pytest 插件 |
| [tests/pytest_benchmark_plugin.py](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tests/pytest_benchmark_plugin.py) | benchmark 插件：CLI 参数、GPU 绑定、计时夹具、回归报告 |
| [tests/pytest_random_plugin.py](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tests/pytest_random_plugin.py) | 随机种子插件：按测试 nodeid 派生稳定种子 |
| [tests/transpose/test_transpose.py](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tests/transpose/test_transpose.py) | 转置算子的正确性 + benchmark 测试，本讲的实践对象 |
| [tile_kernels/testing/generator.py](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/testing/generator.py) | 测试参数生成器，读取 `TK_FULL_TEST` |
| [tile_kernels/transpose/batched_transpose_kernel.py](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/transpose/batched_transpose_kernel.py) | 批量转置 wrapper，读取 `TK_PRINT_KERNEL_SOURCE` |

---

## 4. 核心概念与源码讲解

### 4.1 安装方式与依赖解析

#### 4.1.1 概念说明

TileKernels 提供两种安装方式：

1. **本地开发版** `pip install -e ".[dev]"`：可编辑安装，并附带开发依赖（pytest 等）。适合要读源码、改源码、跑测试的人。
2. **发布版** `pip install tile-kernels`：从 PyPI 拉取已发布版本，只装运行时依赖。适合只想「调用」算子的用户。

两者的核心区别在于：是否安装 `dev` 这一组「可选依赖」，以及是否能即时反映源码改动。

#### 4.1.2 核心流程

打包与安装的关键信息流如下：

```text
pyproject.toml
  ├─ [build-system]      → 用 setuptools + setuptools-scm 构建
  ├─ dependencies        → 运行时必需：torch>=2.10, tilelang>=0.1.9
  ├─ requires-python     → >=3.10
  └─ [project.optional-dependencies].dev
                          → 开发附加：pytest / pytest-xdist / pytest-repeat / setuptools-scm
```

- **动态版本**：`version` 字段是 `dynamic = ["version"]`（见 [pyproject.toml:10](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/pyproject.toml#L10)），真正的版本号由 `setuptools-scm` 从 git 标签自动算出，写入 `tile_kernels/_version.py`（见 [pyproject.toml:5-6](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/pyproject.toml#L5-L6)）。这就是为什么 `tile_kernels/` 目录里看不到手写的版本号。
- **运行依赖 vs 开发依赖**：`torch` 和 `tilelang` 是任何使用场景都必需的；而 `pytest`、`pytest-xdist`（多进程并行）、`pytest-repeat`（重复跑）只在开发/测试时需要，所以放在 `dev` 里，不会被发布版用户装上。
- **下游系统依赖**：如上一讲所述，CUDA Toolkit（13.1+）和 SM90/SM100 GPU 是硬件/系统层面的依赖，**不在** `dependencies` 中——`pip install` 成功不等于能跑起来。

#### 4.1.3 源码精读

README 给出的两条标准安装命令（见 [README.md:25-37](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/README.md#L25-L37)）：

```bash
# 本地开发版
pip install -e ".[dev]"
# 发布版
pip install tile-kernels
```

对应的依赖声明在 [pyproject.toml:23-39](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/pyproject.toml#L23-L39)：

```toml
dependencies = [
    "torch>=2.10",
    "tilelang>=0.1.9"
]
requires-python = ">=3.10"
...
[project.optional-dependencies]
dev = ["setuptools", "wheel", "setuptools-scm>=8", "pytest", "pytest-xdist", "pytest-repeat"]
```

> 注意：README 的 Requirements 一节（[README.md:17-24](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/README.md#L17-L24)）写的是「TileLang 0.1.9 or higher」，与 `pyproject.toml` 的 `tilelang>=0.1.9` 一致。本仓库的 TileLang 版本下限是 **0.1.9**。

#### 4.1.4 代码实践

**目标**：在不联网或只读环境下，先看清安装会引入哪些依赖、是否满足版本要求，再决定是否真正安装。

**操作步骤**：

1. 在仓库根目录执行 pip 的「干跑」解析（不真正安装）：

   ```bash
   pip install -e ".[dev]" --dry-run
   ```

2. 阅读输出中 pip 列出的「Collecting / Requirement already satisfied」清单，重点检查：
   - `torch` 是否满足 `>=2.10`；
   - `tilelang` 是否满足 `>=0.1.9`；
   - `pytest`、`pytest-xdist`、`pytest-repeat` 是否在列。

3. 把缺失或不满足版本的依赖记成一张表。

**需要观察的现象**：pip 会打印它打算安装的包及其版本；不满足时会报版本冲突。

**预期结果**：在满足条件的机器上，`--dry-run` 应当解析成功；在缺 GPU/CUDA 的纯 CPU 机器上，`torch`/`tilelang` 仍可能解析通过（它们本身可 pip 安装），但后续跑测试会因无 GPU 失败——这正是上一讲强调的「安装成功 ≠ 能跑」。

> 待本地验证：实际输出取决于你机器上已装的包版本。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `pytest` 放在 `[project.optional-dependencies].dev` 而不是 `dependencies`？

**参考答案**：因为 `pytest` 只在开发和测试时需要，发布给「只想调用算子」的用户时不应强制安装。放进 `dependencies` 会让所有用户都被动装上测试框架，增加不必要的体积与版本约束。`.[dev]` 这种语法让开发者显式 opt-in。

**练习 2**：删掉 `.git` 目录后，`pip install -e .` 还能正常算出版本号吗？为什么？

**参考答案**：不能（或会退化成默认值/报错）。版本号由 `setuptools-scm` 从 git 历史与标签推导（见 [pyproject.toml:5-6](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/pyproject.toml#L5-L6)），没有了 `.git` 就没有可推导的来源。

---

### 4.2 运行测试与 pytest 插件机制

#### 4.2.1 概念说明

项目的测试体系建立在 pytest 之上，并提供三种运行姿势（见 [README.md:39-54](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/README.md#L39-L54)）：

| 运行姿势 | 命令 | 作用 |
|---------|------|------|
| 单文件正确性 | `pytest tests/transpose/test_transpose.py -n 4` | 只跑正确性对拍，4 进程并行 |
| 正确性 + 基准 | `pytest tests/transpose/test_transpose.py --run-benchmark` | 额外跑带 `@pytest.mark.benchmark` 的用例 |
| 压力测试 | `TK_FULL_TEST=1 pytest -n 4 --count 2` | 扩大参数范围并每个用例重复 2 次 |

其中 `-n 4` 来自 `pytest-xdist`（4 个并行 worker），`--count 2` 来自 `pytest-repeat`（每个测试重复 2 次），`--run-benchmark` 来自本项目自带的 benchmark 插件。

#### 4.2.2 核心流程

测试启动时的插件加载链条：

```text
pytest 启动
   └─ 读取 tests/conftest.py
        └─ pytest_plugins = [ 'tests.pytest_random_plugin',
                              'tests.pytest_benchmark_plugin' ]
             ├─ pytest_random_plugin:
             │     · pytest_addoption → 注册 --seed
             │     · autouse fixture seed → seed = base + sha256(nodeid)，torch.manual_seed
             └─ pytest_benchmark_plugin:
                   · pytest_addoption → 注册 --run-benchmark 等
                   · pytest_configure → 注册 benchmark 标记；绑定 xdist worker 到 GPU
                   · pytest_collection_modifyitems → 不带 --run-benchmark 时跳过 benchmark 用例
                   · fixture benchmark_timer / benchmark_record → 计时与记录
```

关键点：

- **`conftest.py` 不直接写钩子，而是用 `pytest_plugins` 列表加载两个插件文件**。这两个插件文件故意不叫 `conftest.py`，否则会被 pytest 自动发现一次、又被 `pytest_plugins` 显式加载一次，触发 pluggy 的「重复注册」错误（见 [tests/conftest.py:1-5](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tests/conftest.py#L1-L5) 的注释）。这个设计取舍在第 9 单元会详细讲。
- **benchmark 用例默认跳过**：每个 benchmark 测试都标了 `@pytest.mark.benchmark`，只有加 `--run-benchmark` 才会跑；否则被自动加上 skip 标记。
- **每个测试都有稳定且互不干扰的随机种子**：`seed = base + sha256(nodeid) % 2^31`，`nodeid` 是测试的唯一标识（含文件路径和参数），所以同一测试多次跑种子稳定（可复现），不同测试种子不同（互不干扰）。

#### 4.2.3 源码精读

根 conftest 加载插件（[tests/conftest.py:7-10](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tests/conftest.py#L7-L10)）：

```python
pytest_plugins = [
    'tests.pytest_random_plugin',
    'tests.pytest_benchmark_plugin',
]
```

benchmark 插件注册的命令行参数（[tests/pytest_benchmark_plugin.py:33-56](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tests/pytest_benchmark_plugin.py#L33-L56)）：`--run-benchmark`、`--benchmark-output`、`--benchmark-regression-threshold`（默认 0.15，即 15%）、`--benchmark-verbose`。

「不带 `--run-benchmark` 就跳过所有 benchmark 用例」的逻辑在 `pytest_collection_modifyitems`（[tests/pytest_benchmark_plugin.py:94-103](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tests/pytest_benchmark_plugin.py#L94-L103)）：

```python
def pytest_collection_modifyitems(config, items):
    if not config.getoption('--run-benchmark'):
        skip_bench = pytest.mark.skip(reason='need --run-benchmark to run')
        for item in items:
            if 'benchmark' in item.keywords:
                item.add_marker(skip_bench)
```

随机种子插件（[tests/pytest_random_plugin.py:10-18](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tests/pytest_random_plugin.py#L10-L18)），`autouse=True` 表示对每个测试自动生效：

```python
@pytest.fixture(autouse=True)
def seed(request):
    base = request.config.getoption('--seed')
    node_hash = int(hashlib.sha256(
        request.node.nodeid.encode()
    ).hexdigest(), 16) % (2**31)
    seed = base + node_hash
    torch.manual_seed(seed)
    return seed
```

一个真实正确性测试的样子（[tests/transpose/test_transpose.py:65-74](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tests/transpose/test_transpose.py#L65-L74)）：用 `x.T.contiguous()` 作为参考，与 kernel 输出做 `assert_equal` 对拍：

```python
@pytest.mark.parametrize('params', generate_test_params_transpose(is_benchmark=False), ids=make_param_id)
def test_transpose(params):
    num_tokens = params['num_tokens']
    (x,) = generate_test_data_transpose(params)
    y = tile_kernels.transpose.transpose(x)
    if num_tokens == 0:
        return
    y_ref = x.T.contiguous()
    assert_equal(y, y_ref)
```

#### 4.2.4 代码实践

**目标**：在 GPU 机器上跑通转置算子的正确性测试，确认环境可用。

**操作步骤**：

1. 先按 4.1 完成本地开发版安装：`pip install -e ".[dev]"`。
2. 跑单文件正确性测试（4 进程并行）：

   ```bash
   pytest tests/transpose/test_transpose.py -n 4
   ```

3. 观察终端输出里的 `passed / skipped / failed` 数量。

**需要观察的现象**：

- 所有 `test_transpose` 和 `test_batched_transpose` 用例应当 `passed`。
- `test_transpose_benchmark` / `test_batched_transpose_benchmark` 因为没带 `--run-benchmark`，应当显示为 `skipped`，跳过原因是 `need --run-benchmark to run`。

**预期结果**：正确性用例全部通过，benchmark 用例全部跳过。如果 benchmark 用例没被跳过，说明插件未正确加载（检查 `tests/conftest.py` 是否被识别）。

> 待本地验证：实际用例数取决于 `generate_num_tokens` / `generate_hidden_sizes` 生成的参数组合（详见 4.4）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 benchmark 用例默认跳过、需要显式 `--run-benchmark` 才跑？

**参考答案**：benchmark 计时需要 GPU 独占、耗时较长（每个用例 warmup+30 次重复），不适合每次日常开发都跑。默认跳过让「快速验证正确性」与「认真量性能」两种场景分开。

**练习 2**：`seed = base + sha256(nodeid) % 2^31` 中，把 `nodeid` 换成固定字符串 `seed = base + 0` 会有什么问题？

**参考答案**：所有测试会用同一个种子，相邻测试的随机输入不再独立，可能掩盖或放大某些 bug；而且同一测试仍稳定（可复现），但跨测试的可复现性语义会被破坏。用 `nodeid` 派生保证了「每个测试稳定且互不干扰」。

---

### 4.3 Benchmark 计时、带宽与回归报告

#### 4.3.1 概念说明

「跑 benchmark」就是给一个 kernel 测两个数：

- **延迟（latency）**：跑一次 kernel 耗时多少微秒（us）。
- **带宽（bandwidth）**：每秒搬运了多少 GB 数据（GB/s），用来判断「是否逼近显存带宽极限」。

本项目用两个 pytest fixture 完成这件事：

- `benchmark_timer(fn)`：测延迟，返回微秒。
- `benchmark_record(...)`：记录一条结果，打印到终端、可选写入 JSONL，并收集起来做回归对比。

#### 4.3.2 核心流程

一个 benchmark 测试的标准数据流（以转置为例，[tests/transpose/test_transpose.py:77-91](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tests/transpose/test_transpose.py#L77-L91)）：

```text
1. 准备输入 x
2. num_bytes = count_bytes(x, transpose(x))      # 统计读入+写出的总字节数
3. t_us    = benchmark_timer(lambda: transpose(x))  # CUPTI 计时，单位微秒
4. bandwidth_gbs = num_bytes / t_us / 1e3        # 换算成 GB/s
5. benchmark_record(kernel=, operation=, params=, time_us=, bandwidth_gbs=)
        ├─ 终端打印  BENCH key: X.X us, bandwidth_gbs=YYY.YY
        ├─ 写入 --benchmark-output 的 JSONL（若指定）
        └─ 存入 config._benchmark_results 供回归报告
```

**带宽公式的推导**：`num_bytes` 单位是字节，`t_us` 单位是微秒（\(1\,\text{us}=10^{-6}\,\text{s}\)）。带宽（GB/s）为

\[
\text{bandwidth} = \frac{\text{num\_bytes}}{t_{\text{us}}\times 10^{-6}\,\text{s}} \times \frac{1}{10^9}
                = \frac{\text{num\_bytes}}{t_{\text{us}}}\times 10^{-3}
\]

即代码里的 `num_bytes / t_us / 1e3`。验证一下：1 GB 数据用 1 秒（\(10^6\) us）传完，应为 1 GB/s；代入得 \(10^9 / 10^6 / 10^3 = 1\)，正确。

`count_bytes` 的实现非常直接——逐张量累加 `numel() * element_size()`（[tile_kernels/testing/numeric.py:58-65](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/testing/numeric.py#L58-L65)）。注意它把「输入 x」和「输出 y」**都**算进去，所以转置这类 memory-bound 算子的带宽包含了读+写。

#### 4.3.3 源码精读

`benchmark_timer` 封装了 TileLang 自带的 CUPTI 计时器，默认 `warmup=0, rep=30`（重复 30 次取统计），返回值由毫秒乘 \(10^3\) 转成微秒（[tests/pytest_benchmark_plugin.py:428-447](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tests/pytest_benchmark_plugin.py#L428-L447)）：

```python
from tilelang.profiler.bench import do_bench

def _timer(fn, **overrides):
    kwargs = dict(backend='cupti', warmup=0, rep=30)
    kwargs.update(overrides)
    return do_bench(fn, **kwargs) * 1e3  # ms → us
```

`benchmark_record` 负责打印 + 写文件 + 收集，其 JSONL 每行一条记录，schema 在 docstring 中给出（[tests/pytest_benchmark_plugin.py:366-376](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tests/pytest_benchmark_plugin.py#L366-L376)）：

```json
{ "kernel": str, "operation": str, "params": dict,
  "time_us": float, "bandwidth_gbs": float | null, "extras": dict | null }
```

跑完所有用例后，终端会打印一张「Benchmark Regression Report」表，列含 `Kernel / Latency(us) / Bandwidth(GB/s) / Ratio / Stat`（见 [tests/pytest_benchmark_plugin.py:216-249](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tests/pytest_benchmark_plugin.py#L216-L249)）。其中：

- `Ratio` = 当前延迟 / baseline 延迟。
- `Stat` 三种：`--` 表示比 baseline 慢超过阈值（回归），`++` 表示快超过阈值（改进），`=` 表示在阈值内。
- 阈值由 `--benchmark-regression-threshold` 控制，默认 `0.15`（15%）。

判定逻辑在 `_detect_regressions`（[tests/pytest_benchmark_plugin.py:112-147](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tests/pytest_benchmark_plugin.py#L112-L147)）：`ratio > 1 + threshold` 记为回归。若出现回归，`pytest_sessionfinish` 会把退出码改成非 0（[tests/pytest_benchmark_plugin.py:150-163](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tests/pytest_benchmark_plugin.py#L150-L163)），让 CI 能捕获性能劣化。

> 注意：回归对比依赖同目录下的 `benchmark_baselines.jsonl`（[tests/pytest_benchmark_plugin.py:22](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tests/pytest_benchmark_plugin.py#L22)）。若该文件不存在，报告会提示 `No baseline file found — skipping regression comparison.`，此时 benchmark 仍能正常计时和记录，只是不做对比。

#### 4.3.4 代码实践

**目标**：跑一次转置 benchmark，读懂它的延迟与带宽输出。

**操作步骤**：

1. 跑带 benchmark 的单文件测试（可同时加 `-n` 并行，但计时建议单 worker 以减少干扰）：

   ```bash
   pytest tests/transpose/test_transpose.py --run-benchmark
   ```

2. 在终端输出中找到形如下面的行：

   ```text
   BENCH transpose/fwd[...]: 12.3 us, bandwidth_gbs=1234.56
   ```

3. 查阅你的 GPU 标称显存带宽（例如 H100 约 3350 GB/s），估算 `bandwidth_gbs / 标称带宽` 这个占比。

**需要观察的现象**：每个参数组合（不同 `num_tokens`、`hidden`、`dtype`）各打印一行 `BENCH`；会话末尾若有 baseline 文件，还会打印一张回归报告表。

**预期结果**：转置是 memory-bound 算子，理想情况下带宽应接近标称显存带宽的一个较高比例（具体比值待本地验证）。若带宽远低于标称值，说明还有优化空间或受非连续输入影响。

> 待本地验证：延迟与带宽的绝对值取决于具体 GPU 型号与参数。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `count_bytes(x, transpose(x))` 要把输入和输出都算进字节数？

**参考答案**：转置既要把输入从显存「读」进来，又要把结果「写」回显存。衡量一个 memory-bound kernel 是否吃满带宽，应当统计它真正搬运的全部数据量，即读+写，所以两者都算。

**练习 2**：如果一次改动让某 kernel 从 10us 变成 12us，默认阈值下会被判为回归吗？

**参考答案**：会。`ratio = 12/10 = 1.2`，而默认阈值 `1 + 0.15 = 1.15`，`1.2 > 1.15`，所以记为回归（`Stat = --`），并且会让 pytest 退出码非 0。

---

### 4.4 两个关键环境变量：TK_FULL_TEST 与 TK_PRINT_KERNEL_SOURCE

#### 4.4.1 概念说明

这两个环境变量让你「不改源码」就能切换测试行为：

- **`TK_FULL_TEST`**：开启「压力/全量测试」。它会让测试参数生成器**扩大参数范围**（加入边界值，如 0 个 token、1 个 SM、更多 top-k / 专家数 / EP rank），用于更彻底地暴露边界 bug。
- **`TK_PRINT_KERNEL_SOURCE`**：开启后，wrapper 在启动 kernel 前会**打印 TileLang 生成的 CUDA 源码**，用于学习与调试「DSL 到底编译成了什么」。

#### 4.4.2 核心流程

```text
TK_FULL_TEST=1
   └─ generator.py 里：do_full_test = os.getenv('TK_FULL_TEST') in ['1','true','True']
        └─ do_full_test=True → 在 base_list 之外追加边界参数
                              （例：num_tokens 增加 0；num_sms 增加 1；MoE 增加 288/384 专家等）

TK_PRINT_KERNEL_SOURCE=1
   └─ 各 wrapper 里：if int(os.getenv('TK_PRINT_KERNEL_SOURCE', 0)):
                        print(kernel.get_kernel_source())
        └─ 打印该 kernel 编译出的 CUDA 代码到 stdout
```

注意两者的「读取姿势」不同：

- `TK_FULL_TEST` 用字符串白名单判断（`in ['1','true','True']`），所以 `1`、`true`、`True` 都算开，其余都算关。
- `TK_PRINT_KERNEL_SOURCE` 用 `int(...)` 转换，任何非 0 整数都算开（如 `1`），未设置时默认 `0`（关）。

#### 4.4.3 源码精读

`generator.py` 中 `TK_FULL_TEST` 的三处用法。第一处在生成 `num_tokens` 时，开启后会额外加入 `0`（零 token 边界）（[tile_kernels/testing/generator.py:10-15](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/testing/generator.py#L10-L15)）：

```python
def generate_num_tokens(alignment: int = 1, is_benchmark: bool = False) -> list[int]:
    do_full_test = os.getenv('TK_FULL_TEST') in ['1', 'true', 'True']
    base_list = [4001, 8001]
    if do_full_test and not is_benchmark:
        full_list = [0] + base_list
```

第二处在生成 SM 数时，开启后额外测 `1` 个 SM（[tile_kernels/testing/generator.py:26-32](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/testing/generator.py#L26-L32)）；第三处在 MoE 参数里，开启后增加更多 top-k / 专家数 / EP rank 组合（[tile_kernels/testing/generator.py:35-40](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/testing/generator.py#L35-L40)）。这些额外参数只在「正确性测试」（`is_benchmark=False`）里启用，benchmark 不开，以免拖慢计时。

`TK_PRINT_KERNEL_SOURCE` 在转置 wrapper 中的读取（[tile_kernels/transpose/batched_transpose_kernel.py:112-113](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/transpose/batched_transpose_kernel.py#L112-L113)）：

```python
if int(os.getenv('TK_PRINT_KERNEL_SOURCE', 0)):
    print(kernel.get_kernel_source())
```

这也是本仓库几乎所有 wrapper 的统一写法——设置一次环境变量，就能在测试输出里看到任意 kernel 的 CUDA 源码，对学 TileLang DSL 极有帮助。

#### 4.4.4 代码实践

**目标 1**：观察 TileLang 生成的 CUDA 源码。

**操作步骤**：

1. 设置环境变量后跑一次转置测试（只跑少量用例即可，用 `-k` 过滤）：

   ```bash
   TK_PRINT_KERNEL_SOURCE=1 pytest tests/transpose/test_transpose.py -n 1 -k "test_transpose[" -s
   ```

   （`-s` 关闭 pytest 对 stdout 的捕获，确保能看到 `print` 输出。）

2. 在输出中找到被打印的 CUDA 源码段，关注：`__global__` kernel 函数名、grid/block 维度、对共享内存（`__shared__`）的使用。

**需要观察的现象**：每个被编译的 kernel 会打印一段 C/CUDA 代码，能看到 TileLang 把 `T.alloc_shared`、`T.copy` 等高层原语翻译成的底层代码。

**预期结果**：能清晰看到转置 kernel 编译后的 CUDA 实现。待本地验证具体源码内容。

**目标 2**：用 `TK_FULL_TEST` 跑压力测试。

**操作步骤**（对应 [README.md:50-54](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/README.md#L50-L54)）：

```bash
TK_FULL_TEST=1 pytest -n 4 --count 2
```

**需要观察的现象**：相比不加 `TK_FULL_TEST`，用例总数明显增加（因为加入了 0 token、1 SM 等边界参数）；`--count 2` 让每个用例跑两遍。

**预期结果**：所有用例（含边界参数）通过，说明 kernel 在边界条件下也正确。待本地验证。

#### 4.4.5 小练习与答案

**练习 1**：`TK_FULL_TEST` 为什么对 benchmark 用例（`is_benchmark=True`）不生效？

**参考答案**：benchmark 关心的是「在常规参数下的性能」，加入 0 token、1 SM 这类边界参数既无性能代表性，又会显著拖慢计时。所以 generator 在 `is_benchmark=True` 时即便利 `TK_FULL_TEST=1` 也不扩展（见 `if do_full_test and not is_benchmark`）。

**练习 2**：`TK_PRINT_KERNEL_SOURCE=0` 和完全不设置该变量，行为有区别吗？

**参考答案**：没有区别。`int(os.getenv('TK_PRINT_KERNEL_SOURCE', 0))` 在变量未设置时取默认 `0`，显式设为 `0` 也是 `0`，二者都走「不打印」分支。只有设成非 0 整数（如 `1`）才会打印。

---

## 5. 综合实践

把本讲的三件事串起来，做一次「安装 → 正确性 → 性能 → 源码观察」的完整闭环。在 GPU 机器上依次执行并记录：

1. **安装**：`pip install -e ".[dev]"`，并用 `pip show tile_kernels` 查看由 `setuptools-scm` 推导出的版本号，确认动态版本生效。
2. **正确性**：`pytest tests/transpose/test_transpose.py -n 4`，记录 `passed/skipped/failed` 数量，并解释为什么 benchmark 用例被 skip。
3. **性能**：`pytest tests/transpose/test_transpose.py --run-benchmark -n 1`，从 `BENCH` 行里挑出 `hidden` 相同、`dtype` 分别为 `bf16` 和 `e4m3` 的两条，比较它们的延迟与带宽，解释「更窄的 dtype（e4m3=1 字节 vs bf16=2 字节）为什么通常带宽更低但搬运字节更少」。
4. **源码观察**：`TK_PRINT_KERNEL_SOURCE=1 pytest tests/transpose/test_transpose.py -n 1 -k "...)" -s`，截取一段生成的 CUDA 源码，标出其中使用共享内存的行。
5. **压力（可选）**：`TK_FULL_TEST=1 pytest tests/transpose/test_transpose.py --count 2`，对比用例数变化，确认边界参数（如 0 token）也被覆盖且通过。

最终交付：一张表，记录第 3 步两组 dtype 的 `time_us` 和 `bandwidth_gbs`，并写出你对「是否接近显存带宽极限」的判断。

> 待本地验证：所有数值结果依赖你的 GPU 与环境。

## 6. 本讲小结

- TileKernels 有两种安装方式：开发版 `pip install -e ".[dev]"`（含 pytest 等开发依赖、可编辑）和发布版 `pip install tile-kernels`（仅运行时依赖）。
- 运行时依赖是 `torch>=2.10`、`tilelang>=0.1.9`，开发依赖（pytest / pytest-xdist / pytest-repeat）在 `dev` 可选项里；版本号由 `setuptools-scm` 从 git 动态生成。
- 测试分三种姿势：单文件正确性（`pytest ... -n 4`）、加基准（`--run-benchmark`）、压力测试（`TK_FULL_TEST=1 ... --count 2`）。
- 根 `conftest.py` 通过 `pytest_plugins` 加载两个自带插件（随机种子 + benchmark），插件文件故意不叫 `conftest.py` 以避开 pluggy 重复注册问题。
- benchmark 用 `benchmark_timer`（CUPTI 计时）测延迟、用 `count_bytes`/延迟换算带宽（`num_bytes / t_us / 1e3`），并与 baseline 比对给出回归报告，回归会让 pytest 退出码非 0。
- `TK_FULL_TEST=1` 扩展参数边界用于更彻底的正确性测试；`TK_PRINT_KERNEL_SOURCE=1` 打印 TileLang 生成的 CUDA 源码，便于学习 DSL→CUDA 的映射。

## 7. 下一步学习建议

- 下一讲 **u1-l3 目录结构与包入口**：本讲你只在「怎么跑」层面接触了 `tile_kernels` 这个包，下一讲会画出它的内部目录树，讲清 `__init__.py` 如何聚合导出、`config` 与 `utils` 提供哪些基础设施。
- 想提前感受算子内部，可在读完 u1-l3 后，带着本讲的 `TK_PRINT_KERNEL_SOURCE=1` 技巧，去读 [tile_kernels/transpose/batched_transpose_kernel.py](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/transpose/batched_transpose_kernel.py)，对照它生成的 CUDA 源码学 TileLang DSL。
- 测试设施的深入（随机种子派生的数学、benchmark JSONL schema、回归阈值）会在 **第 9 单元（测试与基准设施）** 系统讲解；本讲只需会用即可。
