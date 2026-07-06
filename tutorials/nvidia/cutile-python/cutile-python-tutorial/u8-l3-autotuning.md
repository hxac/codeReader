# 自动调优 tune：exhaustive_search 与子进程 benchmark 安全

## 1. 本讲目标

GPU 内核的性能对「tile 尺寸、`num_ctas`、`num_worker_warps`」这类编译期配置极为敏感，而最优配置又取决于具体的 shape、dtype 与显卡架构——很难人工拍板。cuTile 在 `cuda.tile.tune` 子包里提供了自动调优工具，让你把一组候选配置交给它，它替你逐一计时、淘汰慢的、回报最快的。

本讲聚焦生产级入口 `exhaustive_search`，学完后你应当掌握：

- `exhaustive_search` 的**三阶段收敛**流程：Phase 0 warmup + 动态超时过滤、Phase 1 收敛 Top-K、Phase 2 按 cutoff 剪枝慢配置。
- 单个配置如何用 **Welford 在线算法**增量计算均值/方差与 95% 置信区间，并据此判定「已收敛」。
- 为什么 benchmark 必须在**子进程 worker** 里经 IPC 执行，以隔离内核死锁/超时；`benchmark_with_timeout` 如何据此动态收紧启动超时。
- `_export_ipc_benchmark_payload` / `_benchmark_with_ipc_payload` 如何把一次启动序列化到子进程并回传计时；`TileLaunchTimeoutError` 与 `CUDA_TILE_BENCHMARK_DISABLE_SUBPROCESS` 的作用。
- `TuningResult` / `Measurement` 的统计字段含义，以及实验性 `autotune_launch` 的两级缓存与 reservoir 采样（已废弃）。

## 2. 前置知识

本讲是「运行时调度、导出与扩展」单元的一讲，假定你已建立以下认知（来自前置讲义）：

- **load–compute–store 范式与 `ct.launch`**（u3-l1、u8-l1）：内核在 `ct.launch(stream, grid, kernel, args)` 时被 JIT 编译并经 `cuLaunchKernel` 投放；`grid` 是 block 数量。
- **Constant 参数与编译期特化**（u3-l5、u3-l6）：`ct.Constant[int]` 这类参数的每个唯一取值会被烘焙进 cubin、生成一份独立的内核；tile 尺寸 `tm/tn/tk` 通常就是 Constant，改值即触发重新编译。这正是「搜索空间」里每个配置都要单独编译计时的原因。
- **分块 GEMM 与 `replace_hints`**（u3-l6）：`samples/MatMul.py` 是典型的调优对象，`kernel.replace_hints(num_ctas=...)` 可在不重定义内核的前提下替换编译期 hint。

补充两个本讲要用到的基础概念：

- **在线统计（online statistics）**：逐样本更新均值与方差，无需存储全部样本。cuTile 用的是 Welford 算法，下文会给出公式。
- **子进程隔离（process isolation）**：把「可能死循环」的工作丢到一个独立的操作系统子进程里做，父进程在子进程超时后可以直接把它杀掉而自身不受影响——这是用多进程换「一个坏配置不拖垮整次调优」的关键。

## 3. 本讲源码地图

本讲涉及的关键文件，按职责分组：

| 文件 | 作用 |
| --- | --- |
| `src/cuda/tile/tune/__init__.py` | 子包门面，只导出 `exhaustive_search`、`TuningResult`、`Measurement` 三个公共符号。 |
| `src/cuda/tile/tune/_tune.py` | 调优主逻辑：`exhaustive_search` 三阶段搜索、`_TimingCandidate` 单配置计时与收敛判定、`Measurement`/`TuningResult` 结果数据模型。 |
| `src/cuda/tile/tune/_tune_utils.py` | benchmark 安全层：`benchmark_with_timeout`、`_TimedBenchmarkRunner` 子进程管理、`_benchmark_worker_main`、`TileLaunchTimeoutError`、`CUDA_TILE_BENCHMARK_DISABLE_SUBPROCESS` 解析。 |
| `src/cuda/tile/tune/_benchmark_worker.py` | 子进程入口脚本：`python -m cuda.tile.tune._benchmark_worker`，连上父进程后转发 payload 给 C++ 执行。 |
| `src/cuda/tile/_cext.pyi` | C++ 扩展类型存根：`_benchmark`（直接计时）、`_export_ipc_benchmark_payload`（序列化启动）、`_benchmark_with_ipc_payload`（子进程内反序列化并执行）。 |
| `experimental/tile_experimental/.../_autotuner.py` | 实验性、**已废弃**的 `autotune_launch`：两级缓存 + reservoir 采样，用于对比理解。 |

读码建议：先看 `_tune.py` 的数据模型（`Measurement`/`TuningResult`/`_TimingCandidate`），再读 `exhaustive_search` 的三阶段主循环，最后钻进 `_tune_utils.py` 的子进程隔离层。

## 4. 核心概念与源码讲解

### 4.1 调优结果的数据模型：Measurement 与 TuningResult

#### 4.1.1 概念说明

调优的产出不是「一个最快的配置」，而是「**每个配置的计时统计 + 哪些配置失败了**」。原因有二：

1. 你往往想知道前几名差距有多大（第一名是否显著优于第二名），而不是只拿一个冠军。
2. 有些配置会编译失败、超时或死锁——它们不应让整次调优崩溃，而要被记录为 failure 供排查。

因此 cuTile 用两个不可变 dataclass 承载结果：`Measurement` 描述「单个配置 + 它的计时统计」，`TuningResult` 描述「整次搜索的全貌」。

#### 4.1.2 核心流程

一次 `exhaustive_search` 的产出可以这样理解：

```
search_space (一组配置)
        │  逐一计时（成功 / 失败）
        ▼
successes: [Measurement, ...]   ← 每个成功配置的 (config, mean_us, error_margin_us, num_samples)
failures:  [(config, exc_type, msg), ...]  ← 每个失败配置的异常信息
        │  best = min(successes, key=mean_us)
        ▼
TuningResult { best, successes, failures }
```

`Measurement` 的统计字段含义（关键术语）：

- `mean_us`：内核执行时间的均值，单位微秒。
- `num_samples`：为此配置实际跑了多少次 benchmark。
- `error_margin_us`：**95% 置信区间的一半宽度**（即 \(\bar{x} \pm \text{error\_margin}\)）。它由 Welford 算法给出的均值方差换算而来（详见 4.2）。

`TuningResult.summary()` 会把成功配置按 `mean_us` 升序排列，标星号 (`*`) 指出 `best`，并在配置很多时只显示 `top_k` 名与 `bottom_k` 名、中间用 `... N more not shown` 折叠。

#### 4.1.3 源码精读

`Measurement` 与 `TuningResult` 都是 `frozen=True, kw_only=True` 的不可变 dataclass，且通过 `Generic[T]` 让 `config` 的类型跟随你的搜索空间元素类型（可以是 `int`、`dict`、`tuple` 等任意类型）：

- [src/cuda/tile/tune/_tune.py:21-36](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/tune/_tune.py#L21-L36) —— `Measurement`，注释明确 `error_margin_us` 是「95% 置信区间的一半」。
- [src/cuda/tile/tune/_tune.py:38-49](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/tune/_tune.py#L38-L49) —— `TuningResult`，三个字段 `best` / `successes` / `failures`。

`summary()` 的折叠逻辑值得一看，它解释了「为什么打印结果有时会省略中间名次」：

- [src/cuda/tile/tune/_tune.py:64-84](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/tune/_tune.py#L64-L84) —— `top_k` 与 `bottom_k` 之间的名次被折叠成 `... N more not shown`，并对齐各列宽度；`best` 用 `*` 标记。注意「若中间只省略 1 行，不如全显示」这个细节（L67-68），避免只藏一行造成困惑。

#### 4.1.4 代码实践

**实践目标**：在不真正启动 GPU 的情况下，理解 `TuningResult` 的结构与 `summary` 输出格式。

**操作步骤**（源码阅读型实践）：

1. 阅读 `test/test_tune.py` 中的 `test_exhaustive_search_returns_best`（[test/test_tune.py:37-72](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/test/test_tune.py#L37-L72)），它用 `monkeypatch` 把真正的 `_benchmark` 替换成「按配置返回固定耗时」的假函数，构造出 `times = {64: 5.0, 128: 1.0, 256: 3.0}`。
2. 关注断言 `result.best.config == 128`、`result.best.mean_us == 1.0`，以及 `"3 succeeded, 0 failed" in str(result)`。
3. 想象把 `search_space` 扩大到 20 个配置，预测 `str(result)` 会输出多少行（提示：默认 `top_k=10, bottom_k=2`）。

**需要观察的现象 / 预期结果**：`TuningResult` 的 `successes` 是按 `mean_us` 升序排列的 `Measurement` 序列，`best` 永远等于 `successes` 中 `mean_us` 最小者；`str(result)` 的首行形如 `N succeeded, M failed`。本实践为纯阅读，无运行结果，**待本地验证**你对 `summary` 折叠行为的预测。

#### 4.1.5 小练习与答案

**练习 1**：若一次搜索有 5 个成功配置、2 个失败配置，`str(result)` 的首行是什么？`failures` 字段的每个元素是什么类型？

> **答**：首行是 `5 succeeded, 2 failed`。`failures` 的每个元素是三元组 `(config, exc_type_name, message)`，类型为 `tuple[T, str, str]`（见 `_tune.py:48-49`）。

**练习 2**：`Measurement` 为什么把 `error_margin_us` 定义成「95% 置信区间的一半」而不是完整的区间宽度？

> **答**：一半宽度可以直接写成 `mean ± error_margin` 的紧凑形式（`summary` 里就是 `123.4±1.2 us`），同时便于和 `mean_us` 做比较（如 Phase 2 用 `mean - error_margin < cutoff` 判断「是否还有可能超越冠军」，见 4.3）。

---

### 4.2 单个配置的计时候选：_TimingCandidate

#### 4.2.1 概念说明

搜索空间里每一个配置（例如 `{'tm': 64, 'tn': 64, 'tk': 32}`）都需要被独立编译、独立反复计时，并判断「已经测得够准了吗」。`_TimingCandidate` 就是承载「一个配置 + 它不断增长的统计量」的对象。

它的核心难点是：**多少次采样算够？** 采太少，噪声大；采太多，浪费 GPU 时间。cuTile 的做法是「**增量采样 + 收敛判定**」：每跑一批 5 次，用 Welford 算法更新均值与置信区间，命中任一停止条件就判定收敛、不再测它。

#### 4.2.2 核心流程

`_TimingCandidate` 的状态机：

```
新建 candidate（num_samples=0, mean_us=0, m2=0）
   │
   ▼ warmup()          ← 首次启动在子进程里计时（防死锁，见 4.4），再额外跑几次预热
   │
   ▼ run_benchmark()   ← 每批跑 _BATCH_REPEATS=5 次，每次调用 _add_sample()
   │                      用 Welford 增量更新 mean_us / m2 / error_margin_us
   │
   ▼ converged()?      ← 命中任一停止条件即收敛，移出待测堆
   │
   └─ 否 → 回堆，等下一轮再 run_benchmark
      是 → 进入 converged 列表，最终 to_measurement() 产出 Measurement
```

**Welford 在线算法**（用于 `_add_sample`）：设第 \(n\) 次采样值为 \(x_n\)，已有均值 \(\mu_{n-1}\) 与二阶累积量 \(M2_{n-1}\)，则更新公式为：

\[
\mu_n = \mu_{n-1} + \frac{x_n - \mu_{n-1}}{n}
\]

\[
M2_n = M2_{n-1} + (x_n - \mu_{n-1})(x_n - \mu_n)
\]

样本方差 \(s^2 = M2_n / (n-1)\)；**均值的标准误**方差为 \(\text{var} = s^2 / n\)；95% 置信区间的一半宽度为：

\[
\text{error\_margin} = \sqrt{\text{var}} \times 1.96
\]

常数 1.96 是正态分布的 95% 双侧分位数。这套公式的好处是**只需保留三个标量**（`num_samples`、`mean_us`、`m2`），无需存储任何历史样本。

**收敛判定** `converged()` 要求**采样数至少 `_MIN_REPEATS=5`**，且满足下列条件之一（见源码 L375-381）：

- 相对误差 `< 1%`：`error_margin_us <= 0.01 * mean_us`
- 绝对误差 `<= 0.5us`：`error_margin_us <= 0.5`
- 已跑到上限：`num_samples >= _MAX_REPEATS`（=1000）
- 累计耗时超限：`mean_us * num_samples > _MAX_MEASURE_TIME_US`（=5,000,000us，即 5 秒）

#### 4.2.3 源码精读

`_TimingCandidate` 是个普通 `@dataclass`，字段既是状态也是统计量：

- [src/cuda/tile/tune/_tune.py:349-358](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/tune/_tune.py#L349-L358) —— 字段定义：`config` / `grid` / `kernel` / `get_args` 描述「怎么启动」，`num_samples` / `mean_us` / `m2` / `error_margin_us` 描述「测得多准」。

`_add_sample` 就是上面 Welford 公式的直接落地：

- [src/cuda/tile/tune/_tune.py:389-398](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/tune/_tune.py#L389-L398) —— 注意 `1.96` 这个魔法数与注释 `# 95% confidence interval`；只在 `num_samples > 1` 时才计算 error_margin（单次采样方差无定义）。

`run_benchmark` 每批最多跑 `_BATCH_REPEATS` 次，且不会超过 `_MAX_REPEATS` 的总上限：

- [src/cuda/tile/tune/_tune.py:371-373](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/tune/_tune.py#L371-L373) —— `min(num_times, _MAX_REPEATS - self.num_samples)` 这一行是关键，确保总采样数封顶。

`converged()` 的四条停止条件：

- [src/cuda/tile/tune/_tune.py:375-381](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/tune/_tune.py#L375-L381) —— 注意它用 `and` 连接「至少 `_MIN_REPEATS`」与「四个 or 条件之一」，意味着前 4 次采样无论误差多小都不会判收敛。

所有阈值常量集中在文件末尾，便于调参：

- [src/cuda/tile/tune/_tune.py:339-346](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/tune/_tune.py#L339-L346) —— `_BATCH_REPEATS=5`、`_WARM_UP_REPEATS=3`、`_TOP_K=5`、`_MAX_REPEATS=1000`、`_MIN_REPEATS=5`、`_MAX_MEASURE_TIME_US=5_000_000`、`_MAX_DYNAMIC_LAUNCH_TIMEOUT_SEC=5.0`、`_MIN_DYNAMIC_LAUNCH_TIMEOUT_SEC=1.0`。

#### 4.2.4 代码实践

**实践目标**：通过一个故意「先快后慢」的假计时序列，观察 `_TimingCandidate` 何时停止采样。

**操作步骤**（源码阅读型实践）：

1. 阅读 `test_exhaustive_search_skips_slow_configs`（[test/test_tune.py:75-124](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/test/test_tune.py#L75-L124)）。该测试为配置 6 设计了序列 `[1.1, 9.0, 1.1, 9.0, 1.1, 20.0, 20.0, ...]`，为配置 7 设计了 `[20.0, 20.5, 20.0, 20.5, 20.0]`。
2. 跟踪配置 6：前几次均值较低，但后续样本很高——它会**继续采样**直到无法超越 Top-K 的 cutoff（详见 4.3），断言 `sorted_successes[5].num_samples > 2`。
3. 跟踪配置 7：一直很慢，断言 `sorted_successes[6].num_samples == 1`，即只跑了 warmup 后的初始批次就被 Phase 2 提前放弃。

**需要观察的现象 / 预期结果**：`num_samples` 并非每个配置都相同——快且稳定的配置早早收敛（`num_samples == 2`，因测试把 `_MIN_REPEATS` 调成 2、`_BATCH_REPEATS` 调成 1）；慢配置要么多跑几批确认无望、要么迅速被剪枝。本实践为纯阅读，**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：一个内核真实耗时恒为 `10.0us`（无噪声），每次采样都精确返回 `10.0`。它会在第几次采样后收敛？

> **答**：每次 `error_margin_us` 都为 0，满足 `error_margin_us <= 0.5`（也满足 `<= 0.01 * mean_us = 0.1`）。但收敛还要求 `num_samples >= _MIN_REPEATS = 5`，所以会在第 5 次采样后（即第一次 `converged()` 检查命中）收敛。

**练习 2**：为什么 `_add_sample` 用 `var = sample_var / num_samples` 而不是直接用 `sample_var` 作为误差？

> **答**：`sample_var` 是**单次采样**的方差；而我们关心的是**样本均值**的不确定度。均值的标准误方差 = 单次方差 / 样本数（即 \(\sigma^2/n\)），采样越多均值越可信，所以除以 `num_samples` 让 error_margin 随采样数收敛到 0。

---

### 4.3 三阶段穷举搜索主流程：exhaustive_search

#### 4.3.1 概念说明

`exhaustive_search` 是 cuTile 的生产级调优入口，签名如下（关键术语：**搜索空间**、**grid 函数**、**args 函数**、**hints 函数**）：

```python
exhaustive_search(
    search_space, stream, grid_fn, kernel, args_fn, hints_fn=None, *, quiet=False
) -> TuningResult
```

它的设计哲学是「**穷举但有剪枝**」：理论上要把每个配置测到收敛，但当某个配置明显不可能夺冠时，就提前停止为它花钱。整个过程分三个阶段，对应终端里看到的 `[Phase 0/2]`、`[Phase 1/2]`、`[Phase 2/2]` 进度提示。

#### 4.3.2 核心流程

三阶段主循环（核心是**一个 min-heap** + **一个 converged 列表**）：

```
Phase 0  Warmup & initial run（对每个配置）
  for cfg in search_space:
      candidate.warmup(...)              # 子进程计时首跑 + 预热，更新动态超时
      candidate.run_benchmark(5)         # 初始一批采样
      若失败 → errors.append(cfg, ...)   # 编译/超时/死锁都进 failures
      若已 converged → converged.append
      否则 → heappush(running, (mean, err, idx, candidate))  # 按 mean_us 排序

Phase 1  Converging Top-K（凑齐 _TOP_K=5 个收敛配置）
  while len(converged) < _TOP_K and running:
      从 running 弹出当前最快的 candidate
      再跑一批 run_benchmark(5)
      converged → 入列；否则按新 mean 回堆

Phase 2  Converging all configs（剪枝慢配置）
  cutoff = max(mean_us of Top-K converged)        # 冠军门槛
  for candidate in running:
      while (未收敛 and mean - error_margin < cutoff):  # 还有理论可能超冠军
          run_benchmark(5)
      # 一旦 best-case(mean-err) 都追不上 cutoff，立即放弃
```

三个关键剪枝思想：

1. **动态超时过滤**（Phase 0）：首次启动在子进程里计时，若成功，则把超时上限收紧为「最慢成功启动的 2 倍、上限 5 秒」。后续配置若死循环超过这个收紧后的阈值，就被 `TileLaunchTimeoutError` 过滤（见 4.4）。
2. **Top-K 优先**（Phase 1）：只把采样预算集中花在当前最快的几个配置上，尽快凑齐 5 个收敛配置，确立「冠军门槛 cutoff」。
3. **cutoff 剪枝**（Phase 2）：对剩余配置，只要它的**最好可能**（`mean - error_margin`）已经追不上 cutoff，就立即停止——即使还没收敛。条件 `mean - error_margin < cutoff` 用了 4.1 里强调的 error_margin 一半宽度。

#### 4.3.3 源码精读

入口与初始状态：

- [src/cuda/tile/tune/_tune.py:131-243](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/tune/_tune.py#L131-L243) —— `exhaustive_search` 函数签名与 docstring；docstring 里有一个完整的 GEMM 调优例子，含 `ByTarget(sm_100=cfg['num_ctas'])` 这种按架构传 hint 的写法，是最好的用法范例。

**Phase 0**——warmup + 动态超时：

- [src/cuda/tile/tune/_tune.py:245-278](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/tune/_tune.py#L245-L278) —— 注意 L260-272 的 `try/except`：任何异常（编译失败、超时、死锁）都被捕获并记入 `errors`，配置被 `continue` 跳过；L264-269 即「动态超时收紧」逻辑：`dynamic_launch_timeout_sec = min(slowest_wall_time_sec * 2, _MAX_DYNAMIC_LAUNCH_TIMEOUT_SEC)`。
- [src/cuda/tile/tune/_tune.py:360-369](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/tune/_tune.py#L360-L369) —— `_TimingCandidate.warmup`：**首次**启动走 `benchmark_with_timeout`（子进程 + 超时保护），返回墙钟时间用于动态超时；其余 `_WARM_UP_REPEATS-1` 次走普通 `_benchmark` 纯预热。

**Phase 1**——凑齐 Top-K：

- [src/cuda/tile/tune/_tune.py:280-295](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/tune/_tune.py#L280-L295) —— min-heap 按 `(mean_us, error_margin_us, i, candidate)` 排序，整数 `i` 是稳定 tie-breaker；每轮弹出**当前最快**者再测一批。

**Phase 2**——cutoff 剪枝：

- [src/cuda/tile/tune/_tune.py:297-320](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/tune/_tune.py#L297-L320) —— L300-307 计算 cutoff（已收敛配置若 ≤ `_TOP_K` 个，cutoff 就是其中最慢者；否则取 Top-K 里最慢者）；L310-312 的 `while` 条件 `candidate.mean_us - candidate.error_margin_us < cutoff_mean_us` 即「理论最好成绩还能赢」才继续测。

收尾——构造结果、处理「全军覆没」：

- [src/cuda/tile/tune/_tune.py:322-336](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/tune/_tune.py#L322-L336) —— 全部成功配置转 `Measurement`；若 `successes` 为空（所有配置都失败），抛 `ValueError("No valid config found in search space.")`，消息里附带第一个失败配置的异常信息便于排查。

#### 4.3.4 代码实践

**实践目标**：对一个真实 GEMM 内核跑一次小规模 `exhaustive_search`，打印 top 配置及计时。

**操作步骤**：

1. 基于 `samples/MatMul.py` 的 `matmul_kernel`（[samples/MatMul.py:33-37](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/samples/MatMul.py#L33-L37)），构造一个小的 `(tm, tn, tk)` 搜索空间，例如 `product((64, 128), (64, 128), (32, 64))` 共 8 个配置。
2. 按本讲 docstring（[src/cuda/tile/tune/_tune.py:182-211](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/tune/_tune.py#L182-L211)）写 `grid_fn`、`args_fn`、`hints_fn`：`grid_fn` 用 `ct.cdiv(M, tm), ct.cdiv(N, tn)`，`args_fn` 返回 `(x, y, out.clone(), tm, tn, tk)`。
3. 调用 `ct.tune.exhaustive_search(...)` 并 `print(result)`。
4. 用 `result.best.config` 取出最优 tile 尺寸，按 docstring 末尾的方式重新 `ct.launch` 验证数值与 `x @ y` 一致。

**需要观察的现象 / 预期结果**：终端会打印三阶段进度条（若在 TTY 下），最后输出形如：

```
8 succeeded, 0 failed
* {'tm': 128, 'tn': 128, 'tk': 32}: 123.4±1.2 us (5 samples)
  {'tm': 128, 'tn': 64,  'tk': 32}: 145.6±2.0 us (5 samples)
  ...
```

冠军行带 `*`。若机器无 GPU 或未装 `tileiras`，本实践**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：Phase 1 的循环条件是 `len(converged) < _TOP_K and running`。如果搜索空间只有 3 个配置，Phase 1 会怎样？

> **答**：`_TOP_K=5`，但配置数不足 5。Phase 0 结束时 `converged` 最多 3 个，Phase 1 的 `len(converged) < 5` 虽为真，但 `running` 很快被弹空，循环自然结束。Phase 2 的 cutoff 取「已收敛配置中最慢者」（因 `len(converged) <= _TOP_K`，走 L300 的分支）。

**练习 2**：Phase 2 的 `while` 条件用 `mean - error_margin < cutoff` 而不是 `mean < cutoff`，为什么？

> **答**：`mean - error_margin` 是该配置的**最好可能成绩**（均值的 95% 置信下界）。如果连最好可能都追不上 cutoff，那继续测也不可能翻盘，应立即剪枝；反之若 `mean - error_margin < cutoff` 还成立，说明置信下界仍低于冠军，理论上多测几次（均值可能下降）还有机会，值得继续。这是一种保守、不轻易误杀的剪枝。

---

### 4.4 子进程隔离与超时安全：benchmark_with_timeout、_TimedBenchmarkRunner 与 _benchmark_worker

#### 4.4.1 概念说明

这是本讲最关键也最精巧的一层。问题来自一个残酷现实：**调优时要测的配置里有可能是「坏内核」——它可能死循环、可能触发硬件挂起**。如果直接在调优进程里 `cuLaunchKernel`，一个死循环内核会把整个 Python 进程永远挂住，整次调优（以及你的 notebook / 训练脚本）一起陪葬。

cuTile 的解法是**进程级隔离**：

1. 调优主进程**不直接**启动那个有风险的内核；
2. 而是把「一次启动所需的全部信息」序列化成一个 `bytes` payload；
3. 把 payload 通过 IPC 发给一个**子进程 worker**，由子进程真正执行 `_benchmark_with_ipc_payload`；
4. 父进程在子进程上设一个超时，超时仍未回结果就**杀掉子进程**、抛 `TileLaunchTimeoutError`，然后继续测下一个配置。

这样「坏配置」最多只杀死一个可重建的子进程，主进程安然无恙。三个核心术语：

- **`benchmark_with_timeout`**：父进程侧的入口，决定「走子进程还是回退到直接计时」。
- **`_TimedBenchmarkRunner`**：父进程侧的子进程池管理器（懒启动、复用、终止）。
- **`_benchmark_worker`**：子进程入口脚本，循环接收 payload、调用 C++ 执行、回传计时。

#### 4.4.2 核心流程

父进程侧 `benchmark_with_timeout` 的决策树：

```
benchmark_with_timeout(stream, grid, kernel, pyargs, dynamic_timeout, inactive_timeout)
   │
   ├─ 若 CUDA_TILE_BENCHMARK_DISABLE_SUBPROCESS 为真 → 直接 _benchmark(...)，返回 (us, None)
   │                                                    （无超时保护，wall_time=None）
   │
   ├─ payload = _export_ipc_benchmark_payload(stream, grid, kernel, pyargs)
   │    └─ 若返回 None（无法序列化，如某些不支持 IPC 的参数）→ 回退直接 _benchmark，返回 (us, None)
   │
   └─ 加 _timed_benchmark_lock（保证子进程串行）
        runner = _get_or_start_worker()       # 懒启动并复用子进程
        timeout = dynamic_timeout if runner.is_running() else inactive_timeout
        (elapsed_us, wall_time) = runner.run(payload, timeout)
        └─ runner.run：发送 (BENCHMARK, task_id, payload) → 等待 timeout 秒
              ├─ 超时无响应 → terminate(worker)，raise TileLaunchTimeoutError
              └─ 收到 (task_id, ok, value, details) → 返回 (value, wall_time)
```

子进程侧（`_benchmark_worker_main`）的主循环：

```
conn = Client(address, authkey=...)   # 连上父进程
loop:
    req = conn.recv()
    若 req == (STOP,) → 退出
    否则 (BENCHMARK, task_id, payload):
        try: elapsed_us = _benchmark_with_ipc_payload(payload)   # C++ 反序列化并启动内核
        except: 回传 (task_id, False, exc_type, traceback)
        else:  回传 (task_id, True, elapsed_us, None)
```

几个精巧点：

- **懒启动 + 复用**：worker 不是每次都新建，`_get_or_start_worker` 会复用仍存活的 worker；若 worker 已死（`process.poll() is not None`）则先 terminate 再启新的。
- **启动超时**：`listener.accept()` 不支持超时，所以用一个 daemon 线程 + `join(timeout=5s)` 等子进程连上来（`_WORKER_START_TIMEOUT_SEC`），连不上就杀子进程并报错，避免卡死。
- **task_id 校验**：父进程维护单调递增的 `task_id`，收到响应若 `result_task_id != task_id` 说明协议错乱，立即终止 worker 并报错。
- **环境变量门控**：`CUDA_TILE_BENCHMARK_DISABLE_SUBPROCESS` 设为 `true/1/t/yes/y/on` 即禁用子进程、回退到直接 `_benchmark`（牺牲超时保护换简单，主要用于调试 / 二分定位）。
- **`_benchmark` 的回退返回值**：回退路径返回的 `wall_time_sec` 为 `None`，这正是 `exhaustive_search` Phase 0 里 `if wall_time_sec is not None` 判断的来源——回退时无法安全测墙钟，动态超时不收紧。

#### 4.4.3 源码精读

**入口与回退分支**——`benchmark_with_timeout`：

- [src/cuda/tile/tune/_tune_utils.py:219-235](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/tune/_tune_utils.py#L219-L235) —— 三条路径（禁用子进程 / payload 序列化失败 / 正常 IPC）在这里分叉；L233-234 的 timeout 选择：runner 已在跑用动态超时，否则用 `inactive_runner_timeout_sec`（首次含子进程启动+编译，给更宽松的默认值）。

**异常类型与开关常量**：

- [src/cuda/tile/tune/_tune_utils.py:26-34](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/tune/_tune_utils.py#L26-L34) —— `TileLaunchTimeoutError(RuntimeError)`、子进程命令字 `BENCHMARK`/`STOP`、`_WORKER_START_TIMEOUT_SEC=5.0`、`_DISABLE_SUBPROCESS_ENV_NAME="CUDA_TILE_BENCHMARK_DISABLE_SUBPROCESS"` 及其真值集合。
- [src/cuda/tile/tune/_tune_utils.py:214-216](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/tune/_tune_utils.py#L214-L216) —— `_benchmark_subprocess_disabled()` 解析环境变量，默认 `"false"`。

**父进程侧的子进程管理**——`_TimedBenchmarkRunner.run` 与生命周期：

- [src/cuda/tile/tune/_tune_utils.py:51-79](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/tune/_tune_utils.py#L51-L79) —— `run` 的核心：发送任务 → `wait([conn], timeout_sec)` 等响应 → 超时则 `terminate(graceful_shutdown=False)` 并抛 `TileLaunchTimeoutError`（L63-65）→ 收到响应做 task_id 与 ok 校验，并返回 `(value, wall_time)`。
- [src/cuda/tile/tune/_tune_utils.py:81-102](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/tune/_tune_utils.py#L81-L102) —— `_get_or_start_worker`：生成 32 字节随机 authkey、用 `multiprocessing.connection.Listener` 监听，`subprocess.Popen([sys.executable, "-m", "cuda.tile.tune._benchmark_worker", address, authkey.hex()], env={..., "CUDA_TILE_IPC_BENCHMARK_WORKER": "1"})` 启动子进程。
- [src/cuda/tile/tune/_tune_utils.py:133-157](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/tune/_tune_utils.py#L133-L157) —— `terminate`：优雅停（发 STOP 等 1s）→ `process.terminate()` 等 1s → 仍不死则 `process.kill()`，最后关连接。这是「坏内核」被杀的执行点。

**子进程侧**——worker 主循环与入口脚本：

- [src/cuda/tile/tune/_tune_utils.py:192-211](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/tune/_tune_utils.py#L192-L211) —— `_benchmark_worker_main`：收到 `STOP` 即退；收到 `BENCHMARK` 调 `_benchmark_with_ipc_payload(payload)`，成功回 `(task_id, True, elapsed_us, None)`、失败回 `(task_id, False, exc_type, traceback)`。
- [src/cuda/tile/tune/_benchmark_worker.py:13-22](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/tune/_benchmark_worker.py#L13-L22) —— `main`：从 `sys.argv` 取地址与 authkey，`Client(address, authkey=bytes.fromhex(authkey_hex))` 连父进程，转入 `_benchmark_worker_main`。这就是 `python -m cuda.tile.tune._benchmark_worker` 的入口。

**进程级单例与退出清理**：

- [src/cuda/tile/tune/_tune_utils.py:170-189](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/tune/_tune_utils.py#L170-L189) —— 模块级单例 `_timed_benchmark_runner` + `_timed_benchmark_lock`，`atexit.register(_terminate_timed_benchmark_runner)` 确保父进程退出时优雅关闭子进程。

**C++ 桥接的三个原语**（`_cext.pyi`）：

- [src/cuda/tile/_cext.pyi:109-113](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_cext.pyi#L109-L113) —— `_benchmark(stream, grid, kernel, pyargs_tuples) -> float`：直接在当前进程启动内核并返回微秒，**不带超时保护**。
- [src/cuda/tile/_cext.pyi:123-127](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_cext.pyi#L123-L127) —— `_export_ipc_benchmark_payload(...) -> bytes | None`：把一次启动序列化为 payload；返回 `None` 表示无法序列化（回退路径）。
- [src/cuda/tile/_cext.pyi:130](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_cext.pyi#L130) —— `_benchmark_with_ipc_payload(payload) -> float`：子进程内反序列化 payload 并执行，返回微秒。

#### 4.4.4 代码实践

**实践目标**：构造一个会死循环的内核，亲眼看到子进程 worker 用 `TileLaunchTimeoutError` 把它过滤进 `failures`，并用 `CUDA_TILE_BENCHMARK_DISABLE_SUBPROCESS=1` 对比行为差异。

**操作步骤**：

1. 直接复用测试里的死循环内核 `conditional_dead_loop_kernel`（[test/test_tune.py:284-293](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/test/test_tune.py#L284-L293)）：当输入张量首元素 `!= 0` 时进入 `while dead_loop_flag != 0: ct.atomic_add(...)` 的死循环；首元素为 `0` 时正常返回。
2. 以 `search_space = [0, 1, 0]` 调用 `exhaustive_search`（参考 [test/test_tune.py:295-320](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/test/test_tune.py#L295-L320)）：
   - 配置 `0`（首元素 0，正常）会成功；
   - 配置 `1`（首元素 1，死循环）会在子进程里触发 `TileLaunchTimeoutError`，被收进 `result.failures`。
3. 检查 `result.failures[0]` 的 `(cfg, exc_type)` 应为 `(1, "TileLaunchTimeoutError")`，且其错误消息里的超时秒数应小于配置上限（因动态超时已被前一个成功配置收紧）。
4. 重新在终端里 `export CUDA_TILE_BENCHMARK_DISABLE_SUBPROCESS=1` 后再跑：此时 benchmark 回退到直接 `_benchmark`，**不再有超时保护**——死循环配置会**直接挂住整个进程**（这就是该开关仅用于调试的原因）。

**需要观察的现象 / 预期结果**：

- 默认（子进程开启）：`len(result.failures) == 1` 且类型是 `TileLaunchTimeoutError`，主进程毫发无伤地继续完成调优。
- 禁用子进程：进程会卡在死循环配置上不动（这是预期的危险行为，仅用于理解开关含义，**不要在生产中关闭**）。

⚠️ 步骤 4 会让进程挂死，建议加一个外部 `timeout` 命令包裹（如 `timeout 30 python ...`）以免手动 kill。若无 GPU 环境，本实践**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `benchmark_with_timeout` 在「payload 序列化返回 None」时要回退到直接 `_benchmark`，而不是直接报错？

> **答**：某些内核参数组合可能暂时无法导出成 IPC payload（例如某些不支持跨进程传递的对象）。报错会让这类配置被误判为「失败」；而回退到直接计时仍能测出耗时（只是放弃超时保护），让调优继续进行。回退时返回 `wall_time=None`，让上游 Phase 0 知道「这次没拿到安全的墙钟时间、不要收紧动态超时」。

**练习 2**：`_TimedBenchmarkRunner.terminate` 里依次用了 `conn.send(STOP)` → `process.terminate()` → `process.kill()` 三级手段，为什么要分三级？

> **答**：代价从低到高、副作用从大到小地收尾。优雅停（发 STOP）让 worker 自己干净退出，最快且无残留；若 worker 卡死（正是死循环场景）则 `terminate()`（SIGTERM）强杀；若连 SIGTERM 都不响应（极端挂起）再 `kill()`（SIGKILL）兜底。三级保证了「既能优雅复用，又能确保坏 worker 一定被回收」。

---

### 4.5 实验性 autotune_launch：两级缓存与 reservoir 采样

#### 4.5.1 概念说明

除了主推的 `exhaustive_search`，cuTile 还在 `experimental/tile_experimental` 里保留了一个更老、**已废弃**（docstring 里明确写了 `.. deprecated:: Use cuda.tile.tune.exhaustive_search instead`）的 `autotune_launch`。讲它的目的不是鼓励使用，而是用它的两个经典设计——**两级缓存**与**reservoir 采样**——帮你对照理解「调优器在工程上通常要解决哪些问题」。

`autotune_launch` 与 `exhaustive_search` 的定位差异：

| 维度 | `exhaustive_search`（主推） | `autotune_launch`（已废弃） |
| --- | --- | --- |
| 调用语义 | 只搜索，返回 `TuningResult`，由你自己决定怎么 launch | 搜索 + 立即 launch + 自动缓存下次 |
| 搜索策略 | 穷举 + 三阶段剪枝 | reservoir 采样最多 `max_iter` 个配置 |
| 缓存 | 无（让你自己管理） | 内建两级缓存，按 `(kernel, args)` 复用最优配置 |
| 计时安全 | 子进程隔离 + 超时 | 直接 `torch.cuda.Event` 计时，无死锁隔离 |
| 计时方式 | Welford 在线统计 + 置信区间 | 固定 `rep` 次取平均（`_time_ms`） |

#### 4.5.2 核心流程

`autotune_launch` 的执行流程：

```
autotune_launch(stream, grid_fn, kernel, args_fn, search_space, key=None, ...)
   │
   ├─ search_space 若是 callable → 调用它得到 iterable
   ├─ _reservoir_sample(search_space, k=max_iter, max_items=10_000)
   │      ← 从（可能无限大的）搜索空间里均匀采 max_iter 个配置
   │
   ├─ 加 _autotune_lock，取/建 default_tile_context.autotune_cache
   │      （两级 dict: {kernel_key -> {arg_key -> _CacheEntry}})
   │   ├─ kernel_key = kernel._pyfunc（被装饰的原 Python 函数）
   │   ├─ arg_key   = key 或 _default_key(args)（每个张量的 (shape,dtype,stride)）
   │   │
   │   ├─ 命中缓存（且非 force_retune）→ cache_hit=True，直接复用
   │   └─ 未命中 → shuffle 后逐配置：
   │         with compiler_timeout(compiler_time_limit_sec):
   │             time_ms = _time_ms(...)            # torch.cuda.Event 计时
   │         捕获 TileCompilerTimeoutError / TileCompilerExecutionError → skip
   │         记录 (cfg, time_ms)，更新 best
   │      写回 per_kernel[arg_key] = _CacheEntry(...)
   │
   └─ 在锁外用 best 配置 ct.launch，返回 TunedResult{cache_hit, ...}
```

两个关键术语：

- **两级缓存（two-level cache）**：外层按 `kernel._pyfunc`（内核身份）分桶，内层按 `arg_key`（输入张量的 shape/dtype/stride 指纹）分桶。这样「同一个内核、不同输入尺寸」会各自缓存各自的最优配置，互不污染。
- **reservoir 采样（reservoir sampling）**：当搜索空间是一个生成器（甚至无限流）时，蓄水池算法能在只遍历一遍、占用 \(O(k)\) 内存的前提下，等概率地抽出 \(k\) 个元素。cuTile 还加了 `max_items=10_000` 的安全上限，防止真的无限流把采样拖垮。

`_default_key` 的指纹规则：对每个运行时参数，若它是张量（有 `shape`/`dtype`），取 `(shape, dtype, stride)`；否则取 `type(arg).__name__`。这意味着「同样 shape 但不同 stride 的两个张量」会被视作不同的 key——因为 stride 影响访存性能、最优配置可能不同。

#### 4.5.3 源码精读

**默认缓存键**：

- [experimental/tile_experimental/src/cuda/tile_experimental/_autotuner.py:49-62](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/experimental/tile_experimental/src/cuda/tile_experimental/_autotuner.py#L49-L62) —— `_default_key`：张量取 `(shape, dtype, stride)`（stride 区分 PyTorch 的 `.stride()` 与 NumPy 的 `.strides`，后者按字节、需除以 `itemsize`），非张量取类型名。
- [experimental/tile_experimental/src/cuda/tile_experimental/_autotuner.py:35-46](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/experimental/tile_experimental/src/cuda/tile_experimental/_autotuner.py#L35-L46) —— `_shape_dtype_stride`：兼容 PyTorch（`stride()` 方法）与 NumPy（`strides` 字节属性）的 stride 抽取。

**reservoir 采样**：

- [experimental/tile_experimental/src/cuda/tile_experimental/_autotuner.py:160-184](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/experimental/tile_experimental/src/cuda/tile_experimental/_autotuner.py#L160-L184) —— 经典蓄水池：前 `k` 个直接入池，第 `n` 个（`n > k`）以概率 `k/n` 随机替换池中某项；`n_seen > max_items` 即停（安全上限）。

**计时函数**（对照 `exhaustive_search` 的 Welford）：

- [experimental/tile_experimental/src/cuda/tile_experimental/_autotuner.py:65-82](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/experimental/tile_experimental/src/cuda/tile_experimental/_autotuner.py#L65-L82) —— `_time_ms`：先 `warmup` 几次，再用 `torch.cuda.Event(enable_timing=True)` 的 `start/end.record(stream)` + `end.synchronize()` 测 `rep` 次的总耗时，返回 `ms / rep`。注意它**没有置信区间**，也不做收敛——固定次数取平均，简单但不如 Welford 稳健。

**两级缓存主逻辑**：

- [experimental/tile_experimental/src/cuda/tile_experimental/_autotuner.py:289-356](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/experimental/tile_experimental/src/cuda/tile_experimental/_autotuner.py#L289-L356) —— 锁内取/建两级 dict（L290-304）、命中判断（L309-311）、未命中时 shuffle 配置后逐一 `compiler_timeout` 内计时并捕获编译异常（L313-351）、写回 `_CacheEntry`（L356）。
- [experimental/tile_experimental/src/cuda/tile_experimental/_autotuner.py:358-378](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/experimental/tile_experimental/src/cuda/tile_experimental/_autotuner.py#L358-L378) —— **锁外**执行真正的 `ct.launch`（避免长时间持锁阻塞其他线程），返回 `TunedResult`（含 `cache_hit` 标志）。

**废弃声明**：

- [experimental/tile_experimental/src/cuda/tile_experimental/_autotuner.py:198-272](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/experimental/tile_experimental/src/cuda/tile_experimental/_autotuner.py#L198-L272) —— docstring 与 `warnings.warn(..., DeprecationWarning)` 都明确指向 `exhaustive_search`。

#### 4.5.4 代码实践

**实践目标**：通过阅读废弃实现，对比理解「调优器缓存」的工程取舍。

**操作步骤**（源码阅读型实践）：

1. 阅读 `_default_key`（L49-62），回答：若你对同一个内核分别传入一个 `shape=(1024,1024)` 连续张量和一个 `shape=(1024,1024)` 但转置过（stride 不同）的张量，它们会命中同一个缓存条目吗？
2. 阅读 `_reservoir_sample`（L160-184），手动模拟一个含 10 个元素的搜索空间、`k=3`、固定 `rng` 种子，跟踪每一步哪些元素进入/被替换出蓄水池。
3. 对照 4.2 的 Welford 收敛，指出 `_time_ms`（固定 `rep=10` 次取平均）在噪声较大时会高估还是低估某些配置的真实性能。

**需要观察的现象 / 预期结果**：

- 问题 1：**不会命中**。stride 不同 → `arg_key` 不同 → 内层 dict 的不同条目，各自独立调优。这是合理设计：不同 stride 的访存模式差异巨大，最优 tile 尺寸往往不同。
- 问题 3：固定 10 次取平均没有异常值剔除也没有置信区间，**噪声大的配置**其 10 次均值方差很大，单次调优结果可能不可复现——这正是 `exhaustive_search` 改用 Welford + 收敛判定的动机之一。

本实践为纯阅读，**待本地验证**你对蓄水池模拟的手动跟踪。

#### 4.5.5 小练习与答案

**练习 1**：`autotune_launch` 为什么把真正的 `ct.launch` 放在 `_autotune_lock` **之外**执行？

> **答**：调优（编译 + 多次计时）很慢，若在锁内 launch 会长时间阻塞其他线程的调优请求，使并发退化成串行。把「决定 best 配置」这件需要一致性的事放进锁内，把「实际投放内核」这件可以并发的事放到锁外，是兼顾正确性与并发性的典型做法。`exhaustive_search` 因为不做内建缓存、无共享状态，干脆连这把锁都不需要。

**练习 2**：废弃的 `autotune_launch` 用 `torch.cuda.Event` 直接计时，缺了 `exhaustive_search` 子进程隔离层的什么关键能力？

> **答**：缺了**对死循环/挂起内核的超时隔离**。直接在主进程用 Event 计时，一旦内核死循环，`end.synchronize()` 会永远阻塞，主进程挂死。`exhaustive_search` 通过子进程 + `TileLaunchTimeoutError` 解决了这个问题——这是新工具取代旧工具的核心改进之一。

---

## 5. 综合实践

把本讲的三块主要内容（三阶段搜索、子进程超时隔离、结果统计）串起来，完成下面这个端到端调优任务：

**任务**：为 `samples/MatMul.py` 的 GEMM 内核做一次完整调优，并验证子进程隔离层的有效性。

**步骤**：

1. **准备内核与搜索空间**：复用 [samples/MatMul.py:33-37](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/samples/MatMul.py#L33-L37) 的 `matmul_kernel`。在 `M=N=1024, K=512, dtype=float16` 下，构造搜索空间：

   ```python
   from itertools import product
   keys = ("tm", "tn", "tk", "num_ctas")
   search_space = [dict(zip(keys, v)) for v in product(
       (64, 128), (64, 128), (32, 64), (1, 2))]   # 共 16 个配置
   ```

2. **编写三个回调**：仿照 [src/cuda/tile/tune/_tune.py:182-203](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/tune/_tune.py#L182-L203) 的 docstring 示例，写出 `grid_fn`（用 `ct.cdiv`）、`args_fn`（注意 `out.clone()`，避免被多次计时污染）、`hints_fn`（`{'num_ctas': ByTarget(sm_100=cfg['num_ctas'])}`）。

3. **跑搜索并打印**：调用 `ct.tune.exhaustive_search(...)`，`print(result)`。观察终端的三阶段进度（`[Phase 0/2]` → `[Phase 1/2]` → `[Phase 2/2]`）与最终的排名表，确认带 `*` 的是冠军。

4. **验证子进程隔离**：把 `search_space` 里**临时**插一个会死循环的配置（可参照 [test/test_tune.py:284-293](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/test/test_tune.py#L284-L293) 的 `conditional_dead_loop_kernel` 思路，单独跑一次带 `[1]` 的搜索）。确认：
   - 默认行为下：该配置出现在 `result.failures` 里，类型是 `TileLaunchTimeoutError`，其余 15 个配置正常完成。
   - `CUDA_TILE_BENCHMARK_DISABLE_SUBPROCESS=1` 下：进程会卡死（用 `timeout 60 python ...` 包裹保护自己），从而**亲身体验**这个开关为何「仅限调试」。

5. **核对统计字段**：从 `result.best` 读出 `mean_us`、`error_margin_us`、`num_samples`，对照 4.2 的 Welford 解释，理解为何冠军通常 `num_samples` 较小（很快满足相对误差 `< 1%`）。

**验收标准**：

- 能正确解释终端输出里每个字段的来源（`mean_us` 来自 Welford 均值、`error_margin_us` 来自 95% 置信区间的一半、`num_samples` 因收敛判定而各配置不同）。
- 能说出「为什么死循环配置不会拖垮整次调优」的完整链路：`benchmark_with_timeout` → `_TimedBenchmarkRunner.run` 超时 → `terminate` 杀子进程 → 抛 `TileLaunchTimeoutError` → Phase 0 的 `try/except` 把它收进 `errors`。

⚠️ 步骤 4 的禁用子进程场景会让进程挂死，务必用 `timeout` 命令包裹。若无 GPU 或未装 `tileiras`，整个综合实践**待本地验证**。

## 6. 本讲小结

- `cuda.tile.tune.exhaustive_search` 是生产级调优入口，输出 `TuningResult{best, successes, failures}`；`Measurement` 用 Welford 算法在线计算 `mean_us` 与 95% 置信区间的一半 `error_margin_us`。
- 单配置计时由 `_TimingCandidate` 承载，每批跑 `_BATCH_REPEATS=5` 次增量更新统计量；`converged()` 要求至少 `_MIN_REPEATS=5` 次且命中「相对误差 <1% / 绝对误差 ≤0.5us / 跑满 1000 次 / 累计超 5s」之一。
- 搜索分三阶段：Phase 0 warmup 并据首个成功配置的墙钟**动态收紧**启动超时；Phase 1 用 min-heap 凑齐 `_TOP_K=5` 个收敛配置确立 cutoff；Phase 2 用 `mean - error_margin < cutoff` **剪枝**追不上冠军的慢配置。
- benchmark 的**死锁安全**靠子进程隔离：`benchmark_with_timeout` 把启动序列化成 payload 经 IPC 发给 `_benchmark_worker` 子进程，超时则杀子进程并抛 `TileLaunchTimeoutError`，使坏配置不拖垮主进程。
- `CUDA_TILE_BENCHMARK_DISABLE_SUBPROCESS=true/1/...` 可禁用子进程、回退到无保护直接计时（仅用于调试）；`_export_ipc_benchmark_payload` 返回 `None` 时也会回退，此时 `wall_time=None`、不收紧动态超时。
- 实验性的 `autotune_launch`（已废弃）提供对照：它用两级缓存（`{kernel._pyfunc -> {arg_key -> _CacheEntry}}`，`arg_key` 含张量 stride）与 reservoir 采样，但用 `torch.cuda.Event` 固定次数计时、无超时隔离，这正是被 `exhaustive_search` 取代的主因。

## 7. 下一步学习建议

- **调试与性能工具（u8-l5）**：调优选出最优配置后，下一步是用 `CUDA_TILE_DUMP_TILEIR`、Nsight Compute 等工具**理解为什么**这个配置更快，把经验固化成搜索空间设计直觉。
- **AOT 导出与签名（u8-l2）**：若你打算把调优得到的配置固化进产品，可结合 `export_kernel` 做提前编译，并理解不同 `tm/tn/tk` 取值如何影响 mangled 符号名与 cubin 数量。
- **继续阅读源码**：建议精读 `_tune_utils.py` 的 `_TimedBenchmarkRunner`（[L43-167](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/tune/_tune_utils.py#L43-L167)）完整理解子进程生命周期管理，以及 `test/test_tune.py` 的全部用例——它们用 `monkeypatch` 把 benchmark 假掉，是验证你对三阶段与剪枝条件理解的最佳材料。
