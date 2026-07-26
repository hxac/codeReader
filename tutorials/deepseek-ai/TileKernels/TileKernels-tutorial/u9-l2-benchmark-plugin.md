# 讲义 u9-l2：benchmark 插件与回归检测

## 1. 本讲目标

本讲承接 u1-l2（安装、运行与测试工作流），深入 TileKernels 的「性能基准（benchmark）」基础设施。读完后你应当能够：

- 说出 `benchmark_timer` 与 `benchmark_record` 两个 fixture 各自的职责，并写出一条最小调用；
- 描述 benchmark 记录的 JSONL schema，以及一条记录如何被生成、写入、收集；
- 解释 `make_param_key` 如何生成「稳定且可比」的 key，为什么排序与定宽格式化缺一不可；
- 理解 baseline 回归检测的阈值语义、退出码机制（含 missing 也算失败），以及终端报告的 `--`/`++`/`=` 状态；
- 理解 pytest-xdist 多 worker 场景下的 GPU 绑定与显存 fraction 切分算法。

本讲只覆盖两个最小模块：`tests/pytest_benchmark_plugin.py`（pytest 插件）与 `tile_kernels/testing/bench.py`（基准工具函数）。

## 2. 前置知识

阅读本讲前，建议你已经建立以下认知（来自前置讲义）：

- **pytest 的 marker 与 fixture**：`@pytest.mark.benchmark` 给用例打标签；fixture 通过参数注入复用对象。本讲的两个核心能力都以 fixture 形式提供。
- **pytest-xdist（`-n`）**：用多进程并行跑测试，每个进程叫一个 worker（`gw0`、`gw1`…），环境变量 `PYTEST_XDIST_WORKER` 标识当前 worker。
- **带宽算子**：转置等算子性能受限于显存带宽，故用「有效带宽 GB/s」而非单纯延迟来衡量好坏（见 u3-l2）。
- **CUPTI**：NVIDIA 的性能分析接口，比 Python 计时（`time.perf_counter`）更准确地测量 kernel 实际执行时间，因为它绕开 CPU 异步启动的干扰。
- **pluggy**：pytest 底层的钩子注册框架，同名插件被重复注册会报错——这是本讲插件文件命名的一个关键背景。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `tests/pytest_benchmark_plugin.py` | benchmark pytest 插件主体：CLI 选项、marker、两个 fixture、回归检测、终端报告、xdist GPU/显存切分。 |
| `tile_kernels/testing/bench.py` | benchmark 公共工具：`make_param_key`（稳定 key）、`make_param_id`（pytest id）、`dtype_to_str`、stdout 抑制上下文等。 |
| `tests/conftest.py` | 根 conftest，通过 `pytest_plugins` 把上述插件加载进会话。 |
| `tests/transpose/test_transpose.py` | 真实使用范例：展示 `benchmark_timer`/`benchmark_record` 在一个 benchmark 用例里的标准写法。 |

---

## 4. 核心概念与源码讲解

### 4.1 插件加载机制与 CLI 选项

#### 4.1.1 概念说明

pytest 的扩展点（hook）可以放在两类文件里：

- `conftest.py`：pytest 自动发现、自动加载，无需声明。
- 普通模块：需要被某处 `pytest_plugins = [...]` 显式声明后才会加载。

TileKernels 把 benchmark 相关的钩子放在一个**故意不叫 `conftest.py`** 的文件里，再由根 `conftest.py` 用 `pytest_plugins` 加载。这样做的目的是规避 pluggy 的「重复注册」错误——某些场景下同名 hook 被发现两次会直接报错。这个命名取舍与 u9-l3 讲的随机种子插件是同一套思路。

加载之后，插件通过四个 hook 接入 pytest 生命周期：`pytest_addoption`（注册命令行选项）、`pytest_configure`（会话开始前的配置）、`pytest_collection_modifyitems`（收集完用例后改写它们）、`pytest_sessionfinish`/`pytest_terminal_summary`（会话结束时的退出码与报告）。

#### 4.1.2 核心流程

```text
pytest 启动
  └─ 读取 tests/conftest.py 的 pytest_plugins
       └─ 加载 tests/pytest_benchmark_plugin
            ├─ pytest_addoption    → 注册 4 个 CLI 选项
            ├─ pytest_configure    → 注册 'benchmark' marker + 初始化共享状态 + (xdist) GPU 绑定
            └─ pytest_collection_modifyitems
                 ├─ 无 --run-benchmark → 给所有 benchmark 用例加 skip
                 └─ 有 --run-benchmark → benchmark 用例与正确性用例一起跑
```

四个 CLI 选项：

| 选项 | 默认值 | 作用 |
| --- | --- | --- |
| `--run-benchmark` | `False`（开关） | 是否运行 benchmark 用例（默认跳过）。 |
| `--benchmark-output` | `None` | 把每条结果以 JSONL 追加写入该路径。 |
| `--benchmark-regression-threshold` | `0.15`（15%） | 触发回归/改进警告的相对变化阈值。 |
| `--benchmark-verbose` | `False`（开关） | 终端报告里额外显示 `extras` 列（如 speedup）。 |

#### 4.1.3 源码精读

根 conftest 用 `pytest_plugins` 加载两个插件（注释明确说明了「不叫 conftest.py」的原因）：

[tests/conftest.py:7-10](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tests/conftest.py#L7-L10) —— 声明要加载的两个非 conftest 插件模块。

插件文件顶部同样有说明：

[tests/pytest_benchmark_plugin.py:1-9](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tests/pytest_benchmark_plugin.py#L1-L9) —— 解释了为何刻意避开 `conftest.py` 命名。

四个 CLI 选项集中注册：

[tests/pytest_benchmark_plugin.py:33-56](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tests/pytest_benchmark_plugin.py#L33-L56) —— `pytest_addoption` 注册上述四个选项，其中回归阈值默认 `0.15`。

「默认跳过 benchmark」的逻辑在收集阶段实现：没有 `--run-benchmark` 时，给所有带 `benchmark` 关键字的用例追加 skip marker：

[tests/pytest_benchmark_plugin.py:94-101](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tests/pytest_benchmark_plugin.py#L94-L101) —— 不带开关时跳过全部 benchmark 用例；带上开关则它们与正确性用例一起跑。注释提示：若想**只**跑 benchmark，应显式 `-m benchmark`。

#### 4.1.4 代码实践

**实践目标**：在不实际跑 kernel 的前提下，验证「默认跳过」与「插件加载」行为。

**操作步骤**：

1. 在项目根目录执行 `pytest tests/transpose/test_transpose.py --collect-only -q`，观察收集到的用例里包含 `test_transpose_benchmark`。
2. 执行 `pytest tests/transpose/test_transpose.py -q --co | grep benchmark`，确认它在列表里。
3. 加上 `-m benchmark --collect-only -q`，确认只有 benchmark 用例被选中（marker 已生效）。

**需要观察的现象**：即使不运行，收集阶段也能看到 benchmark 用例存在；`-m benchmark` 能精确筛出它们，说明 marker 注册成功、插件已被加载。

**预期结果**：`--collect-only` 列出用例，`-m benchmark` 只剩 benchmark 用例。是否真正运行 kernel 取决于是否有 GPU，**待本地验证（需 GPU）**。

#### 4.1.5 小练习与答案

**Q1**：为什么插件文件不直接叫 `conftest.py`？
**A**：因为 pluggy 会把被多次发现的同名 hook 视为重复注册而报错；改用普通模块名 + `pytest_plugins` 显式加载，注册只发生一次。

**Q2**：默认情况下（不加任何开关）benchmark 用例会被跑吗？
**A**：不会。`pytest_collection_modifyitems` 会给它们加 skip marker，需要 `--run-benchmark` 才运行。

---

### 4.2 benchmark_timer：CUPTI 计时

#### 4.2.1 概念说明

`benchmark_timer` 是一个 pytest fixture，返回一个**可调用对象** `(fn, **overrides) -> float`。你把要测的函数（通常是 `lambda: kernel(x)`）传进去，它返回该 kernel 的执行时间（微秒）。

它本身不发明计时方法，而是包装了 TileLang 自带的 `tilelang.profiler.bench.do_bench`，默认走 **CUPTI 后端**。相比 CPU 端掐表，CUPTI 直接在 GPU 侧测量 kernel 的起止，排除了「CPU 异步发出 kernel → GPU 才真正执行」之间的时间差，数值更稳。`do_bench` 原本返回的是**毫秒**，fixture 把它乘以 `1e3` 换算成**微秒**，使整数位更有可读性。

#### 4.2.2 核心流程

```text
benchmark_timer(fn, rep=30)
  └─ 构造 kwargs = {backend='cupti', warmup=0, rep=30}
       └─ 用调用方 overrides 覆盖（如 rep=100）
            └─ t_ms = do_bench(fn, **kwargs)   # CUPTI 多次重复取均值
                 └─ return t_ms * 1e3           # 毫秒 → 微秒
```

- `warmup=0`：不做额外预热（用例自己负责先跑一次触发 JIT 编译）。
- `rep=30`：重复 30 次取统计量，降低单次抖动。
- `**overrides`：每个用例可临时覆盖任意参数，例如 `benchmark_timer(fn, rep=100)`。

#### 4.2.3 源码精读

fixture 主体非常薄，核心是「固定默认参数 + 允许覆盖 + 单位换算」三件事：

[tests/pytest_benchmark_plugin.py:428-447](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tests/pytest_benchmark_plugin.py#L428-L447) —— `benchmark_timer` 定义。注意第 440 行延迟导入 `do_bench`（避免在无 GPU 环境收集阶段就触发 tilelang 导入），第 443 行设默认 kwargs，第 445 行 `* 1e3` 把毫秒换成微秒。

真实用例里，先调用一次 kernel 触发编译，再掐表：

[tests/transpose/test_transpose.py:82-83](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tests/transpose/test_transpose.py#L82-L83) —— 第 82 行 `count_bytes(x, transpose(x))` 这一次调用同时起到「预热/JIT 编译」与「统计字节数」两个作用；第 83 行才正式用 `benchmark_timer` 掐表。

#### 4.2.4 代码实践

**实践目标**：理解计时单位与 `rep` 参数。

**操作步骤**：

1. 阅读上面的 fixture 源码，确认 `do_bench` 返回毫秒、fixture 返回微秒。
2. 在一个本地 benchmark 用例里，临时把调用改成 `benchmark_timer(fn, rep=5)` 与 `benchmark_timer(fn, rep=100)` 各跑一次。
3. 比较两次返回值的波动幅度。

**需要观察的现象**：`rep` 越大，多次运行间数值波动越小（方差更低），但单次用例耗时越长。

**预期结果**：延迟数值的单位是微秒（μs），量级与 kernel 复杂度匹配。具体数值**待本地验证（需 GPU）**。

#### 4.2.5 小练习与答案

**Q1**：为什么用 CUPTI 而不是 `time.perf_counter()`？
**A**：GPU kernel 是异步发出的，CPU 端掐表会把「发出」到「真正执行」的延迟算进去，且不含 GPU 内部排队时间；CUPTI 在 GPU 侧直接测量，更准更稳。

**Q2**：`benchmark_timer` 返回值的单位是什么？从哪一行代码决定？
**A**：微秒。由 `do_bench(...) * 1e3`（毫秒 ×1000）这一行决定。

---

### 4.3 benchmark_record：JSONL schema 与稳定 key

#### 4.3.1 概念说明

`benchmark_record` 是另一个 fixture，返回一个可调用对象 `_record(...)`。它的职责有四：

1. 在终端打印一条人读摘要（`BENCH ...`）；
2. 把这条结果作为一行 JSON 追加写入 `--benchmark-output` 指定的 JSONL 文件；
3. 把记录收集到会话级列表 `config._benchmark_results`，供会话末尾的回归报告使用；
4. （由调用方负责）传入结构化字段，使每条记录可被一个**全局唯一且稳定**的 key 标识。

「稳定 key」是回归检测的前提：同一段代码、同一组参数，今天和昨天必须生成同一个 key，才能与 baseline 文件里的记录对得上。key 的构造规则是：

```text
key = f'{kernel}/{operation}[{param_str}]'   # 有参数时
key = f'{kernel}/{operation}'                # 无参数时
```

其中 `param_str` 由 `tile_kernels.testing.bench.make_param_key` 生成。

#### 4.3.2 核心流程

```text
benchmark_record(kernel=..., operation=..., params=..., time_us=..., bandwidth_gbs=..., extras=...)
  ├─ key = kernel/operation[make_param_key(params)]
  ├─ 打印 "BENCH {key}: {time_us} us, bandwidth_gbs=..."
  ├─ record = {kernel, operation, params(排序), time_us(round 2), bandwidth_gbs?, extras?}
  ├─ 若指定了 --benchmark-output：
  │     └─ 加锁 → 以 'a' 追加写一行 json.dumps(record)
  └─ 加会话锁 → config._benchmark_results.append(record)
```

JSONL schema（每行一个 JSON 对象）：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `kernel` | str | 算子名，如 `transpose`、`batched_transpose`。 |
| `operation` | str | 操作名，如 `fwd`、`bwd`。 |
| `params` | dict | 参数字典，**写入前按 key 排序**，保证顺序无关。 |
| `time_us` | float | 延迟（微秒），保留 2 位小数。 |
| `bandwidth_gbs` | float \| None | 有效带宽（GB/s），保留 4 位小数，可缺省。 |
| `extras` | dict \| None | 任意附加列（如 speedup），可缺省。 |

#### 4.3.3 源码精读

`benchmark_record` 的 docstring 直接给出 schema，并说明它同时做「打印 + 写文件 + 收集 + 回归告警」四件事：

[tests/pytest_benchmark_plugin.py:358-376](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tests/pytest_benchmark_plugin.py#L358-L376) —— fixture 定义与 JSONL schema 文档。

key 的构造（注意这里用的是调用方传入的原始 `params` 顺序，仅用于打印）：

[tests/pytest_benchmark_plugin.py:382-386](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tests/pytest_benchmark_plugin.py#L382-L386) —— 拼出 `kernel/operation[params]` 形式的 key。

写入 record 时把 params 排序、数值取整，并加锁追加写文件：

[tests/pytest_benchmark_plugin.py:401-422](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tests/pytest_benchmark_plugin.py#L401-L422) —— 构造 record（第 404 行 `dict(sorted(params.items()))` 是稳定 key 的关键之一），第 414-418 行用 `_jsonl_write_lock` 保护追加写，第 421-422 行把记录收进会话列表。

`make_param_key` 与它的两张映射表是「稳定 key」的核心，位于 `bench.py`：

[tile_kernels/testing/bench.py:83-105](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/testing/bench.py#L83-L105) —— `_SHORT_NAME`（把长参数名缩短，如 `num_ep_ranks→ep`）、`_WIDTH`（给每个参数定一个右对齐宽度）、`make_param_key`（按定宽格式化拼接）。

`make_param_key` 只有一行，但信息量很大：

```python
param_str = ','.join(
    f'{_SHORT_NAME.get(k, k)}={format(v, f">{_WIDTH.get(k)}") if k in _WIDTH else v}'
    for k, v in params.items() if v is not None
)
```

它做四件事来保证「稳定且可比」：

1. **过滤 `None`**：`if v is not None`，缺省参数不进 key，避免「显式传 None」与「不传」产生不同 key。
2. **短名映射**：`_SHORT_NAME.get(k, k)` 把长名字缩短，key 更紧凑。
3. **定宽格式化**：对 `_WIDTH` 里的参数，用 `format(v, ">W")` 右对齐补空格到固定宽度。这让终端报告里不同位数的值列对齐；同时同一个值总是格式化成同一个字符串，保证稳定。
4. **顺序由调用方/排序决定**：配合 record 写入时的 `dict(sorted(params.items()))`，保证无论调用方传参顺序如何，最终比对用的 key 顺序一致。

> 注意区分两个 key：第 383 行运行时拼的 `key` 用原始 `params` 顺序，**只用于打印**；回归比对用的是 `_make_key(rec)`（见 4.4），它从**已排序**的 record 重建 key——两端的排序一致，所以比对是稳定的。

真实用例的完整写法（计数 → 计时 → 记录三步）：

[tests/transpose/test_transpose.py:77-91](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tests/transpose/test_transpose.py#L77-L91) —— `@pytest.mark.benchmark` 标记 + 两个 fixture 注入 + `count_bytes` 算字节数 + `benchmark_timer` 掐表 + `benchmark_record` 落盘。带宽用 `num_bytes / t_us / 1e3` 计算（见 4.3.4）。

#### 4.3.4 代码实践

**实践目标**：手算一个真实 key，验证 `make_param_key` 的稳定性。

**操作步骤**：

1. 取转置用例的一组参数 `params = {'num_tokens': 4096, 'hidden': 7168, 'dtype': 'e4m3'}`（注意真实用例在 [test_transpose.py:88](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tests/transpose/test_transpose.py#L88) 已把 `dtype` 经 `dtype_to_str` 转成字符串）。
2. 按 `make_param_key` 规则手算：
   - `num_tokens` 在 `_WIDTH`（宽 5）：`format(4096, ">5")` = `" 4096"`（4 位右补 1 空格）→ `num_tokens= 4096`；
   - `hidden` 在 `_WIDTH`（宽 4）：`format(7168, ">4")` = `"7168"` → `hidden=7168`；
   - `dtype` 不在 `_WIDTH`：原样 → `dtype=e4m3`。
   - 结果：`num_tokens= 4096,hidden=7168,dtype=e4m3`，完整 key 为 `transpose/fwd[num_tokens= 4096,hidden=7168,dtype=e4m3]`。
3. 带宽公式核对：`bandwidth_gbs = num_bytes / t_us / 1e3`。单位推导：

\[
  \text{GB/s} = \frac{\text{bytes}}{10^{9}} \cdot \frac{1}{\text{seconds}}
              = \frac{\text{bytes}}{10^{9}} \cdot \frac{10^{6}}{t_{\mu s}}
              = \frac{\text{bytes}}{t_{\mu s} \cdot 10^{3}}
  \]

   与代码 `num_bytes / t_us / 1e3` 完全一致。

**需要观察的现象**：同一组参数每次都生成同一个 key；不同位数的 `num_tokens`（如 4096 vs 16384）在 key 里被定宽对齐。

**预期结果**：key 字符串稳定可复现；带宽公式单位正确。此部分为纯源码阅读型实践，可在无 GPU 环境完成；实际写入 JSONL 需在本地带 GPU 环境运行 `--run-benchmark`，**待本地验证**。

#### 4.3.5 小练习与答案

**Q1**：`make_param_key` 为什么要过滤 `None` 值？
**A**：因为同一个算子可能「不传某参数」与「显式传 None」语义相同，过滤后两者生成同一个 key，才能正确匹配 baseline。

**Q2**：为什么 `params` 在写入 JSONL 前要做 `dict(sorted(params.items()))`？
**A**：为了让 key 与参数的传入顺序无关——无论调用方以什么顺序拼 `params`，排序后顺序固定，`_make_key` 重建出的 key 才稳定可比。

---

### 4.4 回归检测：阈值、退出码、终端报告

#### 4.4.1 概念说明

光有当前结果不够，还需要一个**baseline（基线）**文件作为参照。TileKernels 把基线放在与插件同目录的 `benchmark_baselines.jsonl`（同 schema 的 JSONL）。回归检测的逻辑很简单：

- 对每条当前结果，用相同 key 去 baseline 里查；
- 计算 `ratio = current_us / baseline_us`；
- `ratio > 1 + 阈值` → **回归（变慢，`--`）**；
- `ratio < 1 - 阈值` → **改进（变快，`++`）**；
- 其余 → 持平（`=`）；
- key 在 baseline 里找不到 → **missing（新算子或新参数）**。

默认阈值 15%，即慢了 15% 以上才算回归。一个重要细节：**missing 也算失败**——会话退出码会被改成非 0，强制你为新 benchmark 补上 baseline。

#### 4.4.2 核心流程

```text
会话结束
  └─ _detect_regressions(config)
       ├─ 取 config._benchmark_results（本次结果）
       ├─ _load_baselines() → {key: record}
       ├─ 对每条 rec：
       │     key = _make_key(rec)
       │     if key 不在 baselines: → missing
       │     else: ratio = current/baseline
       │           ratio > 1+thr → regressions
       │           ratio < 1-thr → improvements
       └─ 返回 (results, baselines, regressions, improvements, missing)
  └─ pytest_sessionfinish
       └─ if (regressions or missing) and exitstatus==0: exitstatus = 1
  └─ pytest_terminal_summary
       └─ 打印两张表（有 baseline 的 + missing 的）+ 汇总 + REGRESSIONS 警告
```

#### 4.4.3 源码精读

`_detect_regressions` 完成全部比对，返回五元组：

[tests/pytest_benchmark_plugin.py:112-147](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tests/pytest_benchmark_plugin.py#L112-L147) —— 第 135-145 行是核心：`_make_key` 取 key，第 141 行算 `ratio`，第 142-145 行按阈值分流到 regressions/improvements。

退出码：只要存在回归**或** missing，且原本退出码是 0，就改成 1：

[tests/pytest_benchmark_plugin.py:150-163](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tests/pytest_benchmark_plugin.py#L150-L163) —— `pytest_sessionfinish` 把检测结果暂存到 `config._benchmark_detection` 供报告使用；第 162-163 行是退出码逻辑（注意 `regressions or missing`）。

终端报告：先打「有 baseline」的对比表（含 `--`/`++`/`=` 状态列与可选 extras 列），再打「missing」表，最后给汇总行与回归警告：

[tests/pytest_benchmark_plugin.py:170-308](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tests/pytest_benchmark_plugin.py#L170-L308) —— `pytest_terminal_summary`。第 233-238 行决定状态符号；第 300-308 行打印汇总（总数/有基线/missing/回归/改进/阈值）；第 310-317 行在检测到回归时打出 `!! REGRESSIONS DETECTED !!`。

key 重建与 baseline 加载：

[tests/pytest_benchmark_plugin.py:450-475](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tests/pytest_benchmark_plugin.py#L450-L475) —— `_make_key` 从**已排序**的 record 重建 key（与 4.3 强调的稳定性对应）；`_load_baselines` 把基线 JSONL 读成 `{key: record}` 字典，文件不存在则返回空字典。

#### 4.4.4 代码实践

**实践目标**：体验阈值与 missing 对退出码/报告的影响（不依赖 GPU 也能理解机制）。

**操作步骤**：

1. 准备一个假 baseline：在 `tests/benchmark_baselines.jsonl` 写一行（若文件不存在则新建），例如：
   ```json
   {"kernel":"transpose","operation":"fwd","params":{"dtype":"e4m3","hidden":7168,"num_tokens":4096},"time_us":10.0}
   ```
   （注意 `params` 已排序，与真实写入格式一致。）
2. 本地带 GPU 跑 `pytest tests/transpose/test_transpose.py -m benchmark --run-benchmark --benchmark-output=/tmp/out.jsonl --benchmark-regression-threshold=0.15`。
3. 改阈值重跑：`--benchmark-regression-threshold=0.05` 与 `--benchmark-regression-threshold=0.5`，对比终端报告里同一组结果的 `Stat` 列变化（`--`/`++`/`=`）。
4. 故意把 baseline 里的 `time_us` 改成很大（如 1000.0），观察当前结果被判定为 `++`（改进）；改成很小（如 0.1），观察被判定为 `--`（回归）且退出码非 0。
5. 删掉 baseline 文件重跑，观察「所有结果都 missing」且退出码非 0（missing 也算失败）。

**需要观察的现象**：阈值越小，越多结果被标成 `--`/`++`；阈值越大，越多结果标成 `=`；missing 永远会让退出码非 0。

**预期结果**：报告表格、汇总行、`!! REGRESSIONS DETECTED !!` 警告按预期出现；退出码在有回归或 missing 时为 1。具体数值**待本地验证（需 GPU）**；机制部分可通过手算 ratio 验证。

#### 4.4.5 小练习与答案

**Q1**：默认阈值 15%，当前延迟 11.0μs、baseline 10.0μs，ratio=1.10，会被判为什么状态？
**A**：`=`（持平）。因为 `1.10` 既不大于 `1+0.15=1.15`，也不小于 `1-0.15=0.85`。

**Q2**：为什么「missing」也会让 pytest 退出码非 0？
**A**：见第 162-163 行 `if (regressions or missing)`。这是刻意设计：新 benchmark 没有基线就无法判断是否回归，强制开发者补上 baseline，避免回归被静默放过。

---

### 4.5 xdist 多 worker：GPU 绑定与内存切分

#### 4.5.1 概念说明

用 `pytest -n 4` 跑 4 个 xdist worker 时，默认它们会争抢同一张 GPU，极易显存溢出（OOM）。本插件在 `pytest_configure` 里做了两件事来和平共处：

1. **GPU 绑定**：每个 worker 按自己的编号 `gw{i}` 选一张物理 GPU，通过设置 `CUDA_VISIBLE_DEVICES` 把它「焊」死在这张卡上。
2. **显存 fraction 切分**：当多个 worker 共享同一张卡时，按「每张卡上的 worker 数」均分可用显存，给每张卡再预留 10GB 系统开销。

注意 worker 数可以**多于** GPU 数：此时通过取模让多 worker 复用同一张卡，并由 fraction 切分保证不 OOM。

#### 4.5.2 核心流程

```text
pytest_configure（每个 worker 进程各跑一次）
  └─ worker_id = PYTEST_XDIST_WORKER        # 'gw0','gw1',...
  └─ gpu_id = int(worker_id.replace('gw',''))
  └─ num_gpus = torch.cuda.device_count()
  └─ CUDA_VISIBLE_DEVICES = gpu_id % num_gpus   # 取模：多 worker 可复用同卡
  └─ total_workers = PYTEST_XDIST_WORKER_COUNT
  └─ workers_per_gpu = ceil(total_workers / num_gpus)
  └─ reserve = 10 GB
  └─ total_mem = mem_get_info(0)[1]
  └─ usable = max(total_mem - reserve, 0)
  └─ mem_per_worker = usable / workers_per_gpu
  └─ fraction = clamp(mem_per_worker / total_mem, 0, 1)
  └─ torch.cuda.set_per_process_memory_fraction(fraction)
```

#### 4.5.3 源码精读

GPU 绑定与显存切分都在 `pytest_configure` 里，仅在检测到 xdist worker 环境变量时执行：

[tests/pytest_benchmark_plugin.py:63-83](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tests/pytest_benchmark_plugin.py#L63-L83) —— 第 67-71 行做 GPU 绑定（`gpu_id % num_gpus` 实现多 worker 复用同卡）；第 75-83 行算 fraction：第 76 行 `workers_per_gpu = ceil(total/num_gpus)`，第 77 行预留 10GB，第 80 行均分，第 82 行 clamp 到 `[0,1]`，第 83 行设置每进程显存上限。

同一段里还会初始化会话级共享状态（带线程锁）：

[tests/pytest_benchmark_plugin.py:85-91](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tests/pytest_benchmark_plugin.py#L85-L91) —— `_benchmark_results` 列表与 `_benchmark_results_lock` 锁，供 `benchmark_record` 收集结果；带 `--run-benchmark` 时关闭警告，避免被刷屏。

#### 4.5.4 代码实践

**实践目标**：理解 worker→GPU→fraction 的映射，会手算切分结果。

**操作步骤**：

1. 假设机器有 `num_gpus=2`，跑 `pytest -n 4`（`total_workers=4`）。
2. 手算每个 worker 的归属：
   - `gw0`：`gpu_id=0`，`0 % 2 = 0` → 第 0 张卡；
   - `gw1`：`gpu_id=1`，`1 % 2 = 1` → 第 1 张卡；
   - `gw2`：`gpu_id=2`，`2 % 2 = 0` → 第 0 张卡（与 gw0 共享）；
   - `gw3`：`gpu_id=3`，`3 % 2 = 1` → 第 1 张卡（与 gw1 共享）。
3. `workers_per_gpu = ceil(4/2) = 2`。若每卡 80GB，则 `usable = 80-10 = 70GB`，`mem_per_worker = 70/2 = 35GB`，`fraction = 35/80 ≈ 0.4375`。
4. （本地，需 GPU）跑 `pytest -n 2 --run-benchmark tests/transpose/test_transpose.py`，观察是否因 fraction 限制而在大 shape 下报 OOM——这正是切分在起作用。

**需要观察的现象**：worker 数 > GPU 数时，多个 worker 落到同一张卡；fraction 随 `workers_per_gpu` 增大而减小。

**预期结果**：映射与 fraction 手算结果一致。实际多卡运行**待本地验证（需多 GPU）**。

#### 4.5.5 小练习与答案

**Q1**：`num_gpus=1`、`-n 4` 时，`CUDA_VISIBLE_DEVICES` 会是什么？fraction 多大？
**A**：四个 worker 都 `gpu_id % 1 = 0`，都绑到唯一一张卡；`workers_per_gpu=ceil(4/1)=4`，fraction = `(total-10GB)/4 / total`，即四等分（扣 10GB 后）的可用显存。

**Q2**：为什么要预留 10GB？
**A**：给系统/框架（CUDA context、驱动、其他进程）留出开销，避免把显存压到极限导致 OOM；见第 77 行 `_reserve_bytes = 10 * (1024 ** 3)`。

---

## 5. 综合实践

把本讲四个要点串成一个完整的「跑基准 → 落盘 → 回归检测」流程。请在带 GPU 的本地环境完成（无 GPU 时改为纯源码阅读版）。

**任务**：为 `batched_transpose` 跑一次完整 benchmark 并解读结果。

1. **落盘**：运行
   ```bash
   pytest tests/transpose/test_transpose.py -m benchmark --run-benchmark \
          --benchmark-output=/tmp/bench.jsonl
   ```
   打开 `/tmp/bench.jsonl`，任取一行，确认它包含 `kernel/operation/params/time_us/bandwidth_gbs` 字段，且 `params` 已按 key 排序。

2. **手算 key**：挑一条记录，按 4.3.4 的规则手算它的 `make_param_key`，再与 `_make_key` 重建的 key（即 `kernel/operation[...]`）比对，确认一致。

3. **造回归**：把该条记录复制进 `tests/benchmark_baselines.jsonl`，并把 `time_us` 改成当前值的 1/3（模拟一个很快的旧基线）。重跑并加 `--benchmark-regression-threshold=0.15`，确认终端报告里这一条被标成 `--`、退出码为 1、出现 `!! REGRESSIONS DETECTED !!`。

4. **调阈值**：把阈值改成 `0.5` 重跑，确认同一条变成 `=`、退出码回到 0。据此体会「阈值是回归检测的旋钮」。

5. **解释稳定性**：用一句话说明，为什么即使你打乱 `params` 字典里键值对的书写顺序，最终 key 仍然不变。（提示：写入前排序 + `_make_key` 从排序后的 record 重建。）

**预期结果**：能独立产出 JSONL、读懂每行字段、手算 key、并用阈值旋钮控制回归判定。运行结果**待本地验证（需 GPU）**。

## 6. 本讲小结

- benchmark 插件刻意不叫 `conftest.py`，由根 conftest 用 `pytest_plugins` 加载，以规避 pluggy 重复注册；四个 CLI 选项控制开关、输出、阈值、详细度。
- `benchmark_timer` 包装 TileLang 的 `do_bench`，默认走 CUPTI 后端、`rep=30`，返回**微秒**（毫秒 ×1e3）。
- `benchmark_record` 同时做打印、写 JSONL、收集进会话列表；JSONL schema 为 `kernel/operation/params(排序)/time_us/bandwidth_gbs/extras`。
- 稳定 key 由 `make_param_key`（短名映射 + 定宽格式化 + 过滤 None）与 record 写入前的 `sorted` 共同保证，使「同参数同 key」成立。
- 回归检测按 `ratio = current/baseline` 与阈值（默认 15%）判 `--`/`++`/`=`；**missing 也算失败**，退出码会被改成 1。
- xdist 多 worker 通过 `gpu_id % num_gpus` 绑定 GPU、按 `workers_per_gpu` 均分（扣 10GB 后的）显存并设 `set_per_process_memory_fraction`，避免并发 OOM。

## 7. 下一步学习建议

- 阅读 `tests/pytest_random_plugin.py` 并对照本讲，理解「同样不叫 conftest.py、由 `pytest_plugins` 加载」这套命名取舍在随机种子插件里的另一面（见 u9-l3）。
- 回到 `tile_kernels/testing/numeric.py` 的 `count_bytes`（带宽分子），结合本讲的 `bandwidth_gbs` 公式，完整理解「字节数 → 延迟 → 带宽」的计量链路（见 u9-l1、u3-l2）。
- 选一个尚未在 `benchmark_baselines.jsonl` 里登记的算子（如某个 mhc kernel），按本讲的 schema 为它补一条 baseline，亲手走一遍「missing → 补基线 → 持平」的闭环。
