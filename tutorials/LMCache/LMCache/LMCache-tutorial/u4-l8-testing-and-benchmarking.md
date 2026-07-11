# 测试与基准测试

## 1. 本讲目标

本讲是整个学习手册的收官篇。前面 25 讲我们读了大量源码、理解了架构，但有一个问题始终悬而未决：**我怎么知道这些代码真的正确？我怎么量化「LMCache 让推理变快了」这件事？**

读完本讲，你应当能够：

- 说出 LMCache 测试套件的**分层组织**（单机核心 / 多进程 / 分布式 / disagg），并能用 `pytest.ini` + `AGENTS.md` 给出的标准命令跑起一个子集。
- 理解 `tests/conftest.py` 里几个**全局 fixture** 在做什么：为什么测试默认要替换内存分配器、为什么默认关闭遥测、为什么没有 `pytest-benchmark` 也不会崩。
- 区分 **测试（正确性）** 与 **基准（性能）** 两件事，知道 `benchmarks/` 下每一类工具度量的是什么指标（TTFT / 吞吐 / 命中率 / 存储 I/O）。
- 用 `examples/online_session/` 的脚本，自己复现一次「冷启动 vs 缓存命中」的 TTFT 对比，并读懂它产出的 JSONL。

> 本讲承接 [u1-l6 LMCacheEngine 公共 API](u1-l6-engine-public-api.md)：那一讲的 `store / retrieve / lookup` 正是测试与基准要验证的「行为契约」。如果你还没读过，建议先回顾三大 API 的语义。

## 2. 前置知识

本讲几乎不涉及新算法，但要理解几个工程概念：

- **测试 vs 基准（benchmark）**：测试回答「代码对不对」，用断言（assert）判定通过/失败；基准回答「代码快不快、省不省」，用秒、ops/sec、命中率等数值衡量。二者目的不同，所以仓库里分开放在 `tests/` 和 `benchmarks/`。
- **pytest**：Python 最主流的测试框架。一个测试函数以 `test_` 开头，用 `assert` 判定；`pytest` 会自动发现并运行它们。**fixture** 是 pytest 提供的「测试前置/后置资源」机制（类似 setup/teardown），用 `@pytest.fixture` 标注，可在多个测试间复用。
- **monkeypatch**：pytest 提供的「运行时替换」工具，能在单个测试里把某个函数/属性换成假的实现，测试结束自动还原。LMCache 用它在不改源码的前提下，把真实的 CUDA pinned-memory 分配器换成测试友好的版本。
- **TTFT（Time To First Token）**：从发出请求到收到第一个生成 token 的墙钟时间，单位秒。它是衡量「长上下文场景下用户感知延迟」最关键的指标——prefill 一段长 prompt 的耗时几乎全部体现在 TTFT 上。
- **冷启动 / 缓存命中**：第一次请求某段 prompt，KV cache 必须从零算（冷，TTFT 高）；若 LMCache 已经存好这段 KV，后续请求可跳过 prefill 直接复用（命中，TTFT 骤降）。两者的差值就是 LMCache 的「价值」。

回顾一下 KV cache 复用链（来自 [u1-l1](u1-l1-project-overview.md)）：`请求 → prefill 产生 KV → LMCache.store 存入 → 下次请求 LMCache.lookup 命中 → retrieve 取回`。测试验证这条链不断，基准量化这条链省了多少时间。

## 3. 本讲源码地图

| 文件 / 目录 | 作用 |
|-------------|------|
| [pytest.ini](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/pytest.ini) | pytest 配置：开启日志、注册自定义 marker |
| [AGENTS.md](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/AGENTS.md) | 给 AI/人类开发者的工程约定，含「标准测试命令」 |
| [requirements/test.txt](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/requirements/test.txt) | 测试依赖（pytest、pytest-benchmark 等） |
| [tests/conftest.py](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/tests/conftest.py) | 全局 fixture 集中地（内存分配器 mock、遥测关闭、benchmark 兜底） |
| [tests/v1/](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/tests/v1) | v1 架构单测主目录，按子模块分子目录 |
| [tests/benchmarks/](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/tests/benchmarks) | 用 pytest-benchmark 写的「测试形态微基准」 |
| [benchmarks/](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/benchmarks) | 独立性能基准工具集（TTFT、I/O、RAG 等） |
| [examples/online_session/](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/examples/online_session) | 端到端 TTFT 对比脚本（冷 vs 热） |

> 一个容易混淆的点：仓库里有**两个**带 benchmark 的地方——`tests/benchmarks/`（是 pytest 测试，断言性能不退化）和 `benchmarks/`（独立脚本，度量绝对性能）。4.3 节会讲它们的分工。

## 4. 核心概念与源码讲解

### 4.1 tests/：测试分层与标准运行方式

#### 4.1.1 概念说明

LMCache 是个多架构并存的大项目（单机引擎 / 多进程 daemon / 分布式 L1-L2 / PD 分离），不同子系统对运行环境的要求天差地别：核心引擎测试要 GPU，多进程测试要起子进程，分布式测试要 Redis/S3，PD 测试要 NIXL + 两张卡。如果一把全跑，在一台普通开发机上必然大面积失败。

因此 `tests/` 按「**能不能在单机一键跑**」做了分层：

```
tests/
├── test_*.py              # legacy / 顶层零散测试（serde、observability、banner...）
├── conftest.py            # 全局 fixture
├── v1/                    # v1 架构单测主战场（约 60 个 test_*.py + 15 个子目录）
│   ├── test_cache_engine.py        # 核心：store/retrieve/lookup 端到端
│   ├── test_config.py              # 配置系统（CPU 友好）
│   ├── distributed/                # 分布式存储（需 Redis/S3/Bigtable）
│   ├── multiprocess/               # MP daemon（需起子进程）
│   ├── mp_coordinator/             # 舰队协调器（需 HTTP）
│   └── ...
├── disagg/                # PD 分离测试（需 NIXL + 多卡，且要手动开两个终端）
├── benchmarks/            # pytest-benchmark 形态的微基准
└── skipped/               # 占位的空目录（标准命令里 --ignore 它）
```

`AGENTS.md` 给出了一条「**镜像 CI 的标准命令**」，它用一连串 `--ignore` 把那些需要特殊环境的目录排除掉，剩下的就是「单机 + 一张卡」能跑的核心套件。

#### 4.1.2 核心流程

运行测试的标准姿势（三档粒度）：

```text
1. 整套（镜像 CI，排除重环境目录）:
   pytest -xvs \
     --ignore=tests/disagg \
     --ignore=tests/v1/multiprocess/ \
     --ignore=tests/v1/distributed/ \
     --ignore=tests/skipped \
     --ignore=tests/v1/storage_backend/test_eic.py

2. 单个文件:
   pytest -xvs tests/v1/test_cache_engine.py

3. 单个测试函数:
   pytest -xvs tests/v1/test_cache_engine.py::test_function_name
```

参数含义：`-x` 遇到第一个失败就停（快速定位）、`-v` 详细、`-s` 不捕获输出（能看到 `print` 和日志）。

被 `--ignore` 的几类目录各有原因：
- `tests/disagg/`：PD 分离脚本要**手动在两个终端**以 `sender`/`receiver` 角色启动（见 [tests/disagg/README.md](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/tests/disagg/README.md)），不适合 pytest 自动收集。
- `tests/v1/multiprocess/` 与 `tests/v1/distributed/`：依赖外部进程/服务，慢且易抖。
- `tests/skipped/`：目前是空目录，留作占位。
- `tests/v1/storage_backend/test_eic.py`：特殊环境测试。

#### 4.1.3 源码精读

**配置入口**。`pytest.ini` 只做了两件事：开启实时日志、注册一个自定义 marker：

```ini
[pytest]
log_cli = true
log_cli_level = INFO
markers =
    no_shared_allocator: Disable the shared-allocator monkeypatch for this test
```

见 [pytest.ini:4-6](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/pytest.ini#L4-L6)——`markers` 段注册了 `no_shared_allocator`，这样用它标记测试时 pytest 不会报「未知 marker」警告。这个 marker 的实际作用在 4.2 节展开。

**标准命令与约定**。`AGENTS.md` 的 Testing 小节就是上面那条命令的权威出处，并强调了一条核心测试哲学——**针对公共接口与 docstring 契约测试，而非实现细节**：

```text
- Write tests against the **public interface and docstring contract**, not the implementation.
- Avoid accessing private members in tests unless strongly needed.
- All new features and bug fixes should include corresponding tests.
```

见 [AGENTS.md:66-71](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/AGENTS.md#L66-L71)。翻译成行动准则：测试一个 `LMCacheEngine` 时，应该调用它的 `store/retrieve/lookup`（公共 API）并检查返回的 mask 是否符合语义，而不是去翻它内部的 `_storage_manager` 私有字段。这与本手册通篇强调的「封装」一脉相承。

**测试依赖**。测试需要的额外包独立成 [requirements/test.txt](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/requirements/test.txt)，关键几项：

- `pytest>=7.0,<9.1`：锁版本上限，避免 8.x 某些插件兼容问题。
- `pytest-benchmark` / `pytest-benchmark[histogram]`：微基准插件（见 4.3）。
- `pytest-asyncio`：异步测试支持（MP/event loop 代码用）。
- `pytest-html`、`pytest-cov`：HTML 报告与覆盖率。
- `openai`：`online_session` 端到端脚本要用 OpenAI 客户端发请求。

#### 4.1.4 代码实践

1. **实践目标**：在你本机跑起一个**不需要 GPU** 的最小测试子集，确认开发环境可用。
2. **操作步骤**：
   - 先装测试依赖：`uv pip install -r requirements/test.txt`（或 `pip install`）。
   - 选一个 CPU 友好的测试文件，例如配置系统测试：`pytest -xvs tests/v1/test_config.py`。
   - 若想看单个用例：先 `pytest -xvs tests/v1/test_config.py --collect-only` 列出所有测试名，再挑一个跑。
3. **需要观察的现象**：终端逐条打印每个测试的 `PASSED`/`FAILED`，因为 `log_cli=true` 还会带上 INFO 级日志。
4. **预期结果**：全绿（具体用例数取决于版本）。**待本地验证**（不同机器/已装依赖不同，结果可能略有差异；若无 GPU，请勿跑 `test_cache_engine.py`，那里大量用例带 `@pytest.mark.skipif(not torch.cuda.is_available())`）。
5. 若某测试失败，先看是不是环境问题（缺 Redis / 无 GPU），而不是代码问题——这正是分层与 `--ignore` 的意义。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `AGENTS.md` 的标准命令要 `--ignore=tests/v1/multiprocess/`，而不是直接删掉这个目录？
> **答案**：因为它们是**真实有效的测试**，只是需要多进程/特殊环境。`--ignore` 只是在「单机一键跑」场景下跳过它们；CI 在专门的 runner 上会单独跑这套（例如 `.buildkite` 配置）。删掉就丢失了覆盖。

**练习 2**：`pytest.ini` 里注册的 `no_shared_allocator` marker 是给谁用的——是给 pytest 框架，还是给具体测试？
> **答案**：给具体测试用。某个测试若需要「真实的、每测试独立的内存分配器」（而不是全局共享的那个 mock），就在测试函数上方加 `@pytest.mark.no_shared_allocator` 来 opt-out；4.2 节会看到它的实现。

---

### 4.2 tests/：测试基础设施——全局 fixture 与 monkeypatch

#### 4.2.1 概念说明

`tests/conftest.py` 是 pytest 的「全局配置文件」，里面定义的 fixture 对**所有**测试自动可见。LMCache 在这里做了三件关键事，目的都是「让测试又快又稳又正确」：

1. **共享内存分配器 monkeypatch**：默认把 `LMCacheEngineBuilder` 内部创建分配器的逻辑替换成一个**全局共享**的分配器实例。这样成百上千个测试不用各自分配几 GB 的 pinned buffer，否则光初始化就会把显存/内存撑爆、还会触发 `cudaHostRegister` 失败。
2. **关闭遥测**：默认把 `LMCACHE_TRACK_USAGE=false`，防止测试套件把「使用统计」误发到远端 stats 服务器。
3. **benchmark fixture 兜底**：如果没装 `pytest-benchmark`，定义一个会 `skip` 的同名 fixture，让依赖它的测试优雅跳过而不是报「fixture not found」。

这三者共同体现了一个原则：**测试基础设施应当让普通测试零配置通过，把环境差异藏进 fixture**。

#### 4.2.2 核心流程

```text
pytest 启动
  └─ 加载 conftest.py（session 级 fixture 先就位）
       ├─ disable_usage_tracking(autouse, session)  → 设 LMCACHE_TRACK_USAGE=false
       ├─ benchmark(fixture)                       → 若无 pytest-benchmark 则 skip
       └─ use_shared_allocator(autouse, function)  → 默认 monkeypatch 分配器
  └─ 收集并运行每个 test_*.py
       └─ 每个测试函数运行前：
            ├─ 若标记了 no_shared_allocator → 不 patch，用真实分配器
            └─ 否则 → patch 成全局共享分配器
```

`autouse=True` 表示「自动应用到所有测试」，无需测试显式声明参数；`scope="session"` 表示整个 pytest 运行只执行一次，`scope="function"` 表示每个测试函数前后各执行一次。

#### 4.2.3 源码精读

**遥测关闭**（session 级、autouse）：

```python
@pytest.fixture(scope="session", autouse=True)
def disable_usage_tracking():
    """Keep the test suite from sending usage telemetry to the stats server."""
    os.environ["LMCACHE_TRACK_USAGE"] = "false"
    yield
```

见 [conftest.py:40-48](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/tests/conftest.py#L40-L48)。docstring 还点出一个细节：测试遥测自身的那些用例（如 `tests/test_usage_context.py`）会在单测内用 monkeypatch 重新打开它并注入一个假 transport——这是「测试被测行为时局部反转全局默认」的标准手法。

**benchmark 兜底**：

```python
if importlib.util.find_spec("pytest_benchmark") is None:

    @pytest.fixture
    def benchmark():
        pytest.skip("pytest-benchmark is not installed")
```

见 [conftest.py:33-37](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/tests/conftest.py#L33-L37)。`pytest-benchmark` 插件本会注入一个名为 `benchmark` 的 fixture；若环境没装，这里手动定义同名 fixture 让它 `skip`，于是 `tests/benchmarks/` 下用到它的测试会被标记 SKIPPED 而非 ERROR。

**共享分配器 monkeypatch**（核心）：

```python
@pytest.fixture(autouse=True)  # function-scoped by default
def use_shared_allocator(request, monkeypatch, memory_allocator):
    """Default: patch. Opt out with @pytest.mark.no_shared_allocator."""
    if request.node.get_closest_marker("no_shared_allocator"):
        # do NOT patch for this test
        yield
        return

    def _create_shared_allocator(config, metadata, numa_mapping):
        return memory_allocator

    monkeypatch.setattr(
        LMCacheEngineBuilder,
        "_Create_memory_allocator",
        _create_shared_allocator,
    )
    yield
```

见 [conftest.py:800-816](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/tests/conftest.py#L800-L816)。逐行理解：

- `request.node.get_closest_marker("no_shared_allocator")`：检查当前测试有没有带这个 marker，有就「不 patch」（走真实分配器），用于需要验证分配/回收本身的测试。
- `monkeypatch.setattr(LMCacheEngineBuilder, "_Create_memory_allocator", ...)`：把 builder 创建分配器的方法替换成「直接返回那个全局共享 `memory_allocator`」。
- `yield` 之后 monkeypatch 自动还原——**只影响当前这一个测试**，互不污染。

**真实测试长什么样**。看核心引擎测试如何用这套设施验证 `store/retrieve/lookup` 契约（节选）：

```python
@pytest.mark.parametrize("save_unfull_chunk", [False, True])
@pytest.mark.skipif(
    not torch.cuda.is_available(),
    reason="TODO: Add non-CUDA implementation to VLLMPagedMemGPUConnectorV2",
)
def test_paged_same_retrieve_store(save_unfull_chunk, autorelease_v1):
    ...
    cfg = LMCacheEngineConfig.from_legacy(
        chunk_size=chunk_size, remote_url=None, save_unfull_chunk=save_unfull_chunk
    )
    engine = autorelease_v1(
        LMCacheEngineBuilder.get_or_create("test", cfg, dumb_metadata(kv_shape), connector, ...)
    )
    ret_mask = engine.retrieve(tokens, kvcaches=retrieved_cache, slot_mapping=slot_mapping)
```

见 [tests/v1/test_cache_engine.py:64-117](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/tests/v1/test_cache_engine.py#L64-L117)。注意它正是 4.1 说的「针对公共 API 测试」：构造一个真实 `LMCacheEngine`，调 `retrieve`，断言返回的 mask。`autorelease_v1` 是 `tests/v1/utils.py` 提供的 fixture（见 [tests/v1/utils.py](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/tests/v1/utils.py)），保证测试结束自动 `close()` 引擎避免资源泄漏。`dumb_metadata`、`generate_tokens`、`create_gpu_connector` 都是同文件里的造数据 helper（如 [utils.py:170](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/tests/v1/utils.py#L170) 的 `dumb_metadata`）。

而同一个文件里，确实有测试 opt-out 共享分配器——比如验证多 worker 隔离的用例会带 marker：

```python
@pytest.mark.parametrize("save_unfull_chunk", [False, True])
@pytest.mark.no_shared_allocator
@pytest.mark.parametrize("lmserver_v1_process", ["cpu"], indirect=True)
```

见 [tests/v1/test_cache_engine.py:1141-1143](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/tests/v1/test_cache_engine.py#L1141-L1143)。这类测试要起独立进程/独立分配器，所以显式关掉共享 patch。

#### 4.2.4 代码实践（源码阅读型）

1. **实践目标**：搞清楚「一个测试在运行时到底用的是哪个分配器」。
2. **操作步骤**：
   - 打开 [tests/v1/test_cache_engine.py](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/tests/v1/test_cache_engine.py)，用编辑器搜索 `no_shared_allocator`，记录每个带该 marker 的测试名。
   - 对比：带 marker 的测试与不带的，在「分配器来源」上的差别——前者走真实的 `_Create_memory_allocator`，后者拿到的是 conftest 注入的 `memory_allocator`。
   - 选一个**不带** marker 的测试，在 `engine = autorelease_v1(...)` 之后加一行临时日志：`print(type(engine.storage_manager.allocator_backend))`（仅用于理解，**不要提交**），跑一次看输出。**待本地验证**（需 GPU）。
3. **需要观察的现象**：不带 marker 的测试里，allocator 来自共享 fixture；带 marker 的则是新建实例。
4. **预期结果**：能口述出「默认共享、opt-out 独立」这条规则。改动后记得还原，不向源码提交调试代码。

#### 4.2.5 小练习与答案

**练习 1**：`use_shared_allocator` 是 `scope="function"`，而 `disable_usage_tracking` 是 `scope="session"`。为什么前者不能也用 session？
> **答案**：因为 monkeypatch 的替换在 `yield` 后要**还原**。function 级保证「每个测试前后都 patch/还原一对」，测试间互不影响。若用 session 级，整个运行只 patch 一次、最后才还原，中途某个测试若想 opt-out（带 `no_shared_allocator`）就无处可逃——marker 判断是 per-function 的。

**练习 2**：`benchmark` fixture 兜底用 `pytest.skip` 而不是 `pytest.fail`，体现了什么设计取向？
> **答案**：把「缺可选依赖」视为「跳过」而非「失败」。CI 上没装 `pytest-benchmark` 时不会误报红，开发者也能一眼看出是「环境缺件」而非「代码坏了」。

---

### 4.3 benchmarks/：基准工具集与性能度量

#### 4.3.1 概念说明

`benchmarks/`（注意不是 `tests/benchmarks/`）是一组**独立可运行的 Python/Shell 脚本**，目的是回答「LMCache 到底带来多大收益」以及「某个子系统的绝对性能如何」。它们不属于 pytest 套件，不判 pass/fail，而是产出**数字**（秒、ops/sec、倍率）。

[examples/README.md](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/examples/README.md) 把这些工具按用途归了档，`benchmarks/` 目录则这样分：

| 子目录 | 度量什么 | 形态 |
|--------|----------|------|
| `ttft-estimator/` | 估算某模型/上下文长度下的 TTFT（甚至无需真起服务） | 离线估算脚本 |
| `long_doc_qa/` | 长文档问答：重复 prompt 两次，第二次应因缓存命中而 TTFT 骤降 | 在线压测（打 vLLM OpenAI API） |
| `rag/` | RAG 场景吞吐、平均 TTFT、质量 | 在线压测 + 预计算 KV |
| `multi_doc_qa/` / `multi_round_qa/` | 多文档 / 多轮对话复用 | 在线压测 |
| `storage_backend_io/` | 各存储后端（磁盘 / Rust 原始块 / io_uring / HF3FS）的读写 ops/sec | 微基准 |
| `microbenchmark/` | 单个内核/数据结构的纯延迟（如 bitmap fold） | 微基准 |

与之相对，[tests/benchmarks/](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/tests/benchmarks) 是 **pytest-benchmark 形态**的微基准——它仍是测试（在 CI 里跑），用 `benchmark` fixture 度量某个函数的耗时，目的是**防止性能回退**，而非度量绝对业务收益。两者关系：`benchmarks/` 量「业务价值」，`tests/benchmarks/` 守「内核不退化」。

#### 4.3.2 核心流程

以最常用的「缓存收益度量」为例，其通用范式是**两次跑、求比值**：

```text
Run 1 (cold / warmup)：发一个长 prompt → 全量 prefill → 记 TTFT_cold
Run 2 (cache hit)   ：发同样的 prompt → LMCache.lookup 命中 → retrieve 复用 → 记 TTFT_hit

收益 = TTFT_cold / TTFT_hit   （倍率，越大越好）
节省 = 1 - TTFT_hit / TTFT_cold （百分比）
```

`long_doc_qa.py` 把这套做成了带「期望增益断言」的脚本——你可以声明「我期望至少 4.3× 提速」，达不到就非零退出，便于 CI 卡性能门禁：

```text
--expected-ttft-gain: Expected minimum speed-up in time-to-first-token
                     (warmup/query) as a factor, e.g. 4.3 for 4.3×.
--expected-latency-gain: Expected minimum speed-up in total round time ... as a factor.
```

见 [benchmarks/long_doc_qa/long_doc_qa.py:41-47](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/benchmarks/long_doc_qa/long_doc_qa.py#L41-L47)。注意它把第一次叫 `warmup`（其实是「冷」），第二次叫 `query`（命中），增益 = warmup/query。

而 `storage_backend_io` 这类**子系统微基准**不涉及 LLM，只压存储后端本身：

```text
What It Measures
- Total time to submit and complete `num_ops` write (put) or read (get) operations
- Effective ops/sec under concurrent submission
- Optional data integrity verification for read benchmarks
```

见 [benchmarks/storage_backend_io/README.md:10-14](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/benchmarks/storage_backend_io/README.md#L10-L14)。它度量「在 concurrency 并发下，N 次 put/get 用了多少秒」，用来横向对比 LocalDisk / Rust raw block / io_uring / HF3FS 等后端。

`microbenchmark/` 更微观，量单个原生算子。例如 `bitmap_ops_benchmark.py` 对比纯 Python 参考实现与 C++ 原生实现处理「百万级 key 的 bitmap fold」的延迟：

```python
"""...
The native op scans the packed ``Bitmap`` buffer directly -- no Python per-bit
loop and no ``Bitmap``<->tensor conversion -- so it stays sub-millisecond even
at multi-million-key scale where the Python scan takes hundreds of ms.
"""
```

见 [benchmarks/microbenchmark/bitmap_ops_benchmark.py:10-13](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/benchmarks/microbenchmark/bitmap_ops_benchmark.py#L10-L13)。这种微基准的价值是：在分布式 lookup（[u4-l2](u4-l2-distributed-storage.md)）那种要扫描海量 chunk 的场景里，证明原生路径比 Python 循环快上百倍，从而坐实「把热路径下沉到 C++」的必要性。

#### 4.3.3 源码精读

`benchmarks/` 多数是「打 OpenAI API、掐秒表」的脚本，核心度量逻辑高度一致。以 RAG 基准的指标定义为例（来自其 README 的「Benchmark Metrics」段）：

> **Throughput**: Request processed per second.
> **Average TTFT**: Average time taken for the model to generate the first token.
> **Average Quality**: Average quality score of generation content.

见 [benchmarks/rag/README.md:82-84](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/benchmarks/rag/README.md#L82-L84)。注意它把「命中率」隐含在了 TTFT 里——缓存是否命中，最终体现为 TTFT 的升降；RAG 基准不直接报「命中率」这个数，而是报「平均 TTFT」。直接报命中率的工具是可观测性栈（见 [u3-l5](u3-l5-observability.md) 的 Prometheus 指标 `cache hit rate`）和 `examples/chunk_statistics/`。

`storage_backend_io` 的典型用法（写基准）：

```bash
python benchmarks/storage_backend_io/storage_backend_io_benchmark.py \
  --num-ops 512 \
  --concurrency 32 \
  --backend local_disk \
  --local-disk-dir /tmp/lmcache_local_disk_bench \
  --output-json /tmp/storage_backend_io.json
```

输出形如 `local_disk: ops=512 concurrency=32 elapsed=1.234s ops/sec=415.23`（见 [storage_backend_io/README.md:180](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/benchmarks/storage_backend_io/README.md#L180)）。它的 README 还给了一个把两份 JSON 里的 `ops_per_sec` 取出来算「Rust 相对 local_disk 提升 X%」的小 Python 片段——这是横向对比后端的标准动作。

#### 4.3.4 代码实践

1. **实践目标**：跑一个**不需要 GPU、不需要 LLM** 的存储微基准，感受「度量 ops/sec」的流程。
2. **操作步骤**：
   - 用 `local_disk` 后端跑小规模写基准：
     ```bash
     python benchmarks/storage_backend_io/storage_backend_io_benchmark.py \
       --num-ops 256 --concurrency 8 --backend local_disk \
       --local-disk-dir /tmp/lmcache_bench \
       --max-local-disk-gb 1 --output-json /tmp/io.json
     ```
   - 用 `cat /tmp/io.json`（或 Python 读 JSON）查看 `write_ops_per_sec`。
3. **需要观察的现象**：终端打印一行 `local_disk: ops=... elapsed=... ops/sec=...`，JSON 文件含 `backend/num_ops/concurrency/write_elapsed_sec/write_ops_per_sec` 字段。
4. **预期结果**：得到一个具体的 ops/sec 数字。**待本地验证**（数值取决于你的磁盘，机械盘/SSD/NVMe 差异巨大，这正是该基准存在的意义）。
5. 进阶：把 `--backend` 换成 `rust_raw_block`（若环境有 Rust 扩展）再跑一次，对比 ops/sec。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `long_doc_qa.py` 把增益定义成 `warmup/query` 的比值，而不是直接报 `query` 的绝对 TTFT？
> **答案**：绝对 TTFT 强依赖硬件（H100 与消费级卡差几十倍），无法跨环境比较；而「冷/热比值」是个**归一化**指标，反映的是「缓存到底帮了多少忙」，与具体硬件无关，便于设门禁（如 `--expected-ttft-gain 4.3`）。

**练习 2**：`benchmarks/storage_backend_io` 和 `tests/benchmarks/test_benchmark.py` 都叫 benchmark，本质区别是什么？
> **答案**：前者是**独立脚本**，度量绝对性能、产出 JSON/数字，主要给人看；后者是 **pytest 测试**（用 `pytest-benchmark` 的 `benchmark` fixture），在 CI 里跑，目的是**卡性能回退门禁**，跑完会有基线对比、超标则失败。

---

### 4.4 examples/online_session/：端到端 TTFT 验证

#### 4.4.1 概念说明

`benchmarks/` 是给性能工程师的「重型武器」，而 `examples/online_session/` 是给所有人的「最小可复现 demo」——它只回答一个问题：**起了带 LMCache 的 vLLM 之后，同一个长 prompt，第二次是不是明显变快了？**

它对任何「会说 OpenAI `/v1` API」的服务都通用（vLLM、llama.cpp、代理……），因为它是从**客户端**掐秒表，不关心服务端内部。这是端到端（end-to-end）验证的精髓：**像真实用户一样发请求，量真实体验**。

[examples/README.md](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/examples/README.md) 把它列在「Tier 1 — Core Concepts」：

> `online_session/`：Measure TTFT (time-to-first-token) for cold vs. cache-hit requests. Outputs JSONL for plotting. Includes a sweep script across context lengths.

配套的还有一份 LMCache 配置 [example.yaml](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/examples/online_session/example.yaml)，展示最小可用配置（chunk_size=256、local CPU、remote 用 cachegen serde）。

#### 4.4.2 核心流程

脚本 `openai_chat_completion_client.py` 的一次完整运行：

```text
1. 解析参数（--api_base, --context_file, --num_following, --flush_cache, --out ...）
2. 选模型（默认取服务端 /models 的第一个）
3. 准备「文档」：
     - 不传 --context_file       → 生成随机 ASCII 填充文本
     - 传 --context_file 不带值  → 用自带的 ffmpeg.txt
     - 传 --context_file <路径>  → 读该文件
4. 截断到模型上下文上限（留 2048 token 安全边距 SAFETY_MARGIN）
5. Run 1（冷）: 流式发请求 → 记第一个 token 到达时刻 = TTFT_1 → 写 JSONL
6. 若 num_following > 0:
     - 若 --flush_cache: 发 10 个 1-token 的填充请求把 GPU KV 逐出去
     - Run 2..N: 同一 prompt 再发 → 记 TTFT_2..N → 写 JSONL
       （flush 时为「再次冷」，不 flush 时为「缓存命中」）
```

关键度量函数 `ttft_stream`：用 `time.perf_counter()` 在**发请求前**取起点，在**收到第一个有内容的 delta** 时取终点，差值即 TTFT：

```python
def ttft_stream(client, model, messages, printer=None):
    start = time.perf_counter()
    stream = client.chat.completions.create(model=model, messages=messages,
                                            temperature=0.0, stream=True, max_tokens=1024)
    first_tok_t = None
    ...
    for chunk in stream:
        delta = chunk.choices[0].delta
        if delta.content:
            if first_tok_t is None:
                first_tok_t = time.perf_counter()
                ...
    ...
    return first_tok_t - start, buf.getvalue()
```

见 [openai_chat_completion_client.py:98-130](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/examples/online_session/openai_chat_completion_client.py#L98-L130)。注意 `temperature=0.0`（确定性输出，排除随机性干扰）与 `max_tokens=1024`（只关心首 token，不必等生成完）。

`--flush_cache` 的实现是发 10 条「100k 随机字符 + 1 token 输出」的请求，把 GPU KV 块挤出去，从而把 Run 2 变回「冷」：

```python
def flush_kv_cache(client, model):
    filler_chat = build_chat(rand_ascii(FILLER_LEN_CHARS), "noop")
    for _ in range(NUM_FILLER_PROMPTS):
        client.chat.completions.create(model=model, messages=filler_chat,
                                       temperature=0.0, max_tokens=1, stream=False)
```

见 [openai_chat_completion_client.py:133-142](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/examples/online_session/openai_chat_completion_client.py#L133-L142)。这是模拟「缓存被别的请求挤出」的场景，用来验证「LMCache 重新缓存后仍能命中」。

主循环 Run 1 的核心：

```python
print("\n=== Run 1: baseline TTFT ===")
base_chat = build_chat(doc, args.prompt)
ttft1, gen1 = ttft_stream(client, model_id, base_chat, printer)
print(f"\033[33mTTFT_1 = {ttft1:.3f}s\033")
log_jsonl(out_path, {"run_index": 1,
                     "context_tokens": len(tok.encode(doc, add_special_tokens=False)),
                     "ttft_seconds": ttft1})
```

见 [openai_chat_completion_client.py:242-253](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/examples/online_session/openai_chat_completion_client.py#L242-L253)。JSONL 每行一条记录，schema 为 `run_index / context_tokens / ttft_seconds`（见 [online_session/README.md:113-122](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/examples/online_session/README.md#L113-L122)）。

**批量扫描脚本** `bench_ttft_sweep.sh` 把上述过程套一层循环，扫多个上下文长度，结果汇总到一个文件：

```bash
CONTEXT_SIZES=(50 1000 2000 8000 16000 24000 32000 64000 96000 128000)
: > "$MASTER_OUT"               # truncate / create the final log file
for TOKENS in "${CONTEXT_SIZES[@]}"; do
  ...
  python "$BENCH" --max_ctx_tokens "$MAX_CTX" --num_following 1 --out "$OUTFILE" --model ...
  cat "$OUTFILE" >> "$MASTER_OUT"          # append to the master log
done
```

见 [bench_ttft_sweep.sh:6-21](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/examples/online_session/bench_ttft_sweep.sh#L6-L21)。最终 `all_ttft_results.jsonl` 里每个上下文长度有「冷/热」两条，可直接喂给绘图工具画「TTFT vs 上下文长度」曲线。

#### 4.4.3 数学说明：收益与命中率

设冷启动 TTFT 为 \(T_{\text{cold}}\)、缓存命中 TTFT 为 \(T_{\text{hit}}\)，则：

\[ \text{提速倍率} = \frac{T_{\text{cold}}}{T_{\text{hit}}} \]

\[ \text{节省比例} = 1 - \frac{T_{\text{hit}}}{T_{\text{cold}}} \]

为何命中时 TTFT 会大幅下降？长上下文的 prefill 时间近似随 token 数线性增长（attention 是 \(O(n^2)\)，但 prefill 总耗时主导项随 \(n\) 增长），而 `retrieve` 取回已存 KV 是带宽受限的纯搬运，远快于重算。所以上下文越长，\(T_{\text{cold}}\) 越大，缓存收益倍率越夸张——这正是 LMCache 在长上下文/RAG 场景价值最高的原因。

注意：本脚本**不直接输出命中率**。命中率（hit rate）是「命中 token 数 / 总 token 数」的比值，属于服务端指标，由 LMCache 可观测性（[u3-l5](u3-l5-observability.md) 的 `cache hit rate` Prometheus 指标）产出。本脚本从客户端量到的 TTFT 下降，是命中率提升的**最终业务投影**。

#### 4.4.4 代码实践（本讲核心实践任务）

1. **实践目标**：起一个带 LMCache 的 vLLM 服务，复现「冷启动 vs 缓存命中」的 TTFT 差异，并读懂 JSONL。
2. **操作步骤**：
   - **起服务**：用一张 GPU 起一个 vLLM + LMCache 端点（参考 [examples/README.md](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/examples/README.md) Tier 1 的 `kv_cache_reuse/local_backends/`，配合本目录 [example.yaml](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/examples/online_session/example.yaml) 作为 `LMCACHE_CONFIG_FILE`）。确保 OpenAI API 在 `http://localhost:8000/v1` 可达。
   - **冷+热对比**（两条请求）：
     ```bash
     cd examples/online_session
     python openai_chat_completion_client.py --num_following 1
     ```
   - **观察输出**：终端会打印 `TTFT_1`（冷）与 `TTFT_2`（热），并写 `benchmark.jsonl`。
   - **再跑一遍带 flush**，确认 flush 后 Run 2 退化回接近冷：
     ```bash
     python openai_chat_completion_client.py --num_following 2 --flush_cache
     ```
3. **需要观察的现象**：典型输出（README 给的例子）：
   ```
   === Run 1: baseline TTFT ===
   TTFT_1 = 0.429s
   === Run 2: TTFT continued ===
   TTFT_2 = 0.081s
   ```
   JSONL：
   ```json
   {"run_index":1,"context_tokens":120938,"ttft_seconds":0.429}
   {"run_index":2,"context_tokens":120938,"ttft_seconds":0.081}
   ```
4. **预期结果**：`TTFT_2 << TTFT_1`（命中远快于冷），倍率 \(0.429/0.081 \approx 5.3\times\)；带 `--flush_cache` 时 Run 2 接近 Run 1（再次冷），Run 3 才重新命中。把命中率（从 Prometheus 抓的 `cache hit rate`）与 TTFT 倍率对照记录。**待本地验证**（绝对数值取决于 GPU/模型/上下文长度；若无 GPU 环境，至少完成下面的源码阅读部分）。
5. **源码阅读补充（无 GPU 也能做）**：读 [openai_chat_completion_client.py:241-276](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/examples/online_session/openai_chat_completion_client.py#L241-L276)，画出「Run1 → 可选 flush → Run2..N」的控制流，标注每条请求是冷还是热。

#### 4.4.5 小练习与答案

**练习 1**：为什么脚本固定 `temperature=0.0` 和 `max_tokens=1024`，而不是用默认随机采样、生成到 EOS？
> **答案**：`temperature=0.0` 保证两次请求的生成路径确定，排除采样随机性对 TTFT 的干扰；`max_tokens=1024` 是因为 TTFT 只关心**第一个 token 的到达**，限长既够触发首 token、又不必浪费 GPU 把整段长回答生成完，加快迭代。

**练习 2**：`--flush_cache` 发的是「100k 随机字符 + 1 token 输出」的请求，为什么这种请求能逐出之前的 KV？
> **答案**：vLLM 的 GPU KV cache 容量有限。注入 10 条长 filler 会迫使引擎为它们分配 KV 块，从而按淘汰策略把之前那条长 prompt 的 KV 块挤出去。这样下一条同 prompt 请求就 miss，等效于「冷启动」，可用来验证「缓存被逐出后，LMCache 重新写入并再次命中」的闭环。

**练习 3**：本脚本度量的是 TTFT，不是命中率。如果你想同时拿到命中率，该去哪里看？
> **答案**：开 LMCache 的可观测性（见 [u3-l5 可观测性体系](u3-l5-observability.md) 或 `examples/observability/` 的 docker compose 栈），从 Prometheus 抓 `cache hit rate` 指标；或在服务端日志里看 LMCache 打印的区间命中统计。TTFT 下降是命中率提升在客户端的投影。

---

## 5. 综合实践

把本讲三块知识串成一个完整的「**验证 + 量化**」工作流。假设你刚给 `LMCacheEngine` 的 `retrieve` 路径改了一行代码，请按下面顺序确认「没改坏」并「量化影响」：

1. **单元测试守正确性**：跑受影响范围的单测子集。
   ```bash
   pytest -xvs tests/v1/test_cache_engine.py
   ```
   若改动涉及存储后端，再补跑 `tests/v1/storage_backend/`（注意避开需要特殊环境的 `test_eic.py`）。确认全绿。**待本地验证**（需 GPU）。

2. **微基准守性能不退化**：跑 `tests/benchmarks/` 里相关微基准，对比改动前后的 `benchmark` fixture 耗时（pytest-benchmark 会给出统计与回归判定）。
   ```bash
   pytest -xvs tests/benchmarks/test_benchmark.py --benchmark-compare
   ```
   （`--benchmark-compare` 需先前保存过基线；首次跑只建基线。）**待本地验证**。

3. **端到端量化业务影响**：起 vLLM+LMCache，用 `examples/online_session/` 量改动前后的冷/热 TTFT：
   ```bash
   python examples/online_session/openai_chat_completion_client.py --num_following 1
   ```
   把改动前后的 `TTFT_2`（命中）对比，确认你的改动没让命中路径变慢。

4. **横向扫描**（可选）：用 `bench_ttft_sweep.sh` 扫多个上下文长度，画 TTFT 曲线，确认改动在各种上下文长度下都不退化。

整个流程体现的工程闭环：**单测（对不对）→ 微基准（退没退化）→ 端到端（用户感知如何）**，三层缺一不可。这也是为什么仓库把 `tests/`、`tests/benchmarks/`、`benchmarks/`、`examples/` 分开放——它们服务于这条流水线的不同环节。

## 6. 本讲小结

- LMCache 测试按「能否单机一键跑」分层：核心引擎在 `tests/v1/`，多进程/分布式/disagg 等重环境测试在各自子目录，标准命令用一连串 `--ignore` 排除后者（[AGENTS.md:47-60](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/AGENTS.md#L47-L60)）。
- 测试哲学是「针对公共接口与 docstring 契约测试，而非实现细节」，不访问私有成员（[AGENTS.md:66-71](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/AGENTS.md#L66-L71)）。
- `tests/conftest.py` 三件套全局 fixture：共享分配器 monkeypatch（默认开、用 `@pytest.mark.no_shared_allocator` opt-out）、关闭遥测、benchmark 兜底，让普通测试零配置通过。
- `benchmarks/` 是独立脚本，度量绝对性能：TTFT 估算、长文档/RAG/多轮在线压测、存储 I/O 微基准、内核微基准；`tests/benchmarks/` 则是 pytest 形态的回归门禁——前者量业务价值，后者守内核不退化。
- 缓存收益的通用度量范式是「冷/热两次跑求比值」，TTFT 下降是命中率提升在客户端的投影；命中率本身由可观测性（[u3-l5](u3-l5-observability.md)）产出。
- `examples/online_session/` 是最小端到端 demo：对任何 OpenAI 兼容服务，从客户端掐秒表量冷 vs 热 TTFT，产出 JSONL，并用 `--flush_cache` 验证「逐出后重新命中」闭环。

## 7. 下一步学习建议

至此你已读完整套 26 讲手册。建议从三个方向继续：

- **把测试与可观测性接起来**：本讲量的是客户端 TTFT，[u3-l5 可观测性体系](u3-l5-observability.md) 讲的是服务端指标（命中率、读写吞吐、OTel trace）。两者结合才是完整的「性能画像」。可动手搭 `examples/observability/` 的 docker compose 栈，把一次 `online_session` 压测的 trace 与指标对上号。
- **深入一个基准做二次开发**：挑 `benchmarks/rag/` 或 `benchmarks/long_doc_qa/`，把它的负载换成你自己的真实数据集（参考 [benchmarks/rag/README.md](https://github.com/LMCache/LMCache/blob/2756b828e86e94c18662037bb4a0c24b9de1bf13/benchmarks/rag/README.md) 的 dataset format），度量 LMCache 在你业务下的真实收益——这是把学习转化为生产决策的关键一步。
- **给新功能补测试**：回顾 [u4-l1 CLI 扩展](u4-l1-cli-command-framework.md) 到 [u4-l7 PD 传输](u4-l7-pd-disaggregation-transfer.md) 任意一讲，仿照 `tests/v1/test_cache_engine.py` 的写法，为那个模块的公共接口补一个面向契约的测试，跑通 `pytest -xvs`，完成从「读懂」到「能改、能验」的闭环。

> 恭喜你读完全部讲义。回到 [u1-l1](u1-l1-project-overview.md) 的那张数据流草图——现在你应该能在每个箭头处说出对应的源码文件、测试位置与基准工具了。这就是源码级掌握一个项目的标志。
