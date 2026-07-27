# u6-l3 报告与基线对比

## 1. 本讲目标

上一讲 u6-l1 解决了「测出来的 latency 是否可信」（`bench_kernel` 的 CUPTI 纯 kernel 计时、L2 flush、CUDA-events 回退控制）。本讲解决紧接着的下一个问题：**测完之后，这些数字如何被收集、归类、落盘、并与基线对比**。

学完本讲你应该能够：

1. 说出 `BenchmarkReport` 三个静态方法 `record / dump / clear` 的职责，以及它们在 pytest 生命周期（session start / 每个 test / session finish）里的调用时机。
2. 解释 tag 体系（`tileops` 与 `torch` / `torch-ref` / `fa3` / `fla` / `flashinfer` 等基线 tag）如何驱动 `dump()` 的 markdown 分组与 conftest 的 JUnit 属性输出。
3. 理解为什么 `record()` 的第一参数**应该传 Op 实例而非字符串别名**——Op 实例能给出「类名 + 模块」的规范身份（canonical identity），字符串别名会丢失模块信息并造成归类混淆。
4. 记住两条信任边界：每个基准必须记录至少一条非 `tileops` 基线；ref 函数必须就地定义，**绝不从 `tests/` 或 `workloads/` 导入 oracle**。

## 2. 前置知识

本讲默认你已经掌握 u6-l1 的内容，特别是：

- **`BenchmarkBase[W]` 与 `_build_result`**：基准调 `bench_kernel` 拿到纯 kernel latency 后，`_build_result` 把它组装成含 `latency_ms` / `tflops` / `bandwidth_tbs` 的结果字典；若计时偏离默认 CUPTI 协议（如走了 CUDA-events 回退），还会附上 `timing` 字段。
- **manifest 驱动**（u6-l2）：FLOP 与字节数来自 `op.eval_roofline()`，基准本身不算公式。

本讲用到的几个新术语，先给直觉：

- **SOL 效率（Speed-of-Light efficiency）**：硬件理论上最短时间除以实测时间。要算它，实测时间必须有可对比的基线，否则只是孤立的一个数字。这就是「每个基准必须记录至少一条基线」的根本原因。
- **规范身份（canonical identity）**：一条基准结果「属于谁」的答案。TileOPs 用「Op 类名 + 该类所在的模块路径」来回答，而不是单靠一个字符串名字。
- **JUnit `user_properties`**：pytest 把每个 test 附加的键值对写进 JUnit XML 报告，供 CI 下游（如 nightly 报表脚本）消费。基准数据除了写 `profile_run.log`，还会落到这里。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `benchmarks/benchmark_base.py` | 定义 `BenchmarkReport` 静态收集器（`record/dump/clear`）、`_extract_op_config` 配置提取、`_get_env_metadata` 环境信息，以及线程局部的 `_bench_results`。 |
| `benchmarks/conftest.py` | pytest 钩子：session 开始 `clear()`、session 结束 `dump("profile_run.log")`、每个 test 调用后把 `_bench_results.entries` 拆成 tileops / baseline 写进 JUnit `user_properties`。 |
| `benchmarks/ops/bench_gemm.py` | 一个完整的基准文件范例：记录 `tileops` 与 `torch-cublas` / `torch-scaled-mm` / `flashinfer-*` 多条基线。 |
| `benchmarks/ops/bench_norm.py` | 更简洁的范例：`tileops` + `torch-ref` 两条记录。 |
| `docs/design/testing.md` | 报告与基线规则（§Benchmarks / Reporting rules）。 |

## 4. 核心概念与源码讲解

### 4.1 BenchmarkReport 静态收集器

#### 4.1.1 概念说明

`BenchmarkReport` 是一个**进程内、静态的**结果收集器。说它「静态」是因为它的三个方法都是 `@staticmethod`，没有实例状态——所有结果存在类级别的字典 `_records` 里。基准函数不需要 `new` 一个 `BenchmarkReport`，直接 `BenchmarkReport.record(...)` 即可。

它的生命周期由 pytest 钩子驱动，形成「收集 → 落盘 → 清空」的闭环：

- **session 开始**：`clear()` 清空旧记录，保证本次运行干净。
- **每个基准 test 执行中**：`record(...)` 追加一条结果。
- **session 结束**：`dump("profile_run.log")` 把所有记录写成一份 markdown 报告。

`BenchmarkReport` 同时维护**两条并行的存储路径**，服务于两类不同消费者：

1. `_records`（类级 dict）→ 人类可读的 `profile_run.log`（markdown）。
2. `_bench_results.entries`（线程局部 list）→ 给 conftest 钩子消费、转写成 JUnit XML 属性，供 CI 机器读取。

这两条路径在同一次 `record()` 调用里一起写入，保证「人看的报告」和「机器读的属性」永远一致。

#### 4.1.2 核心流程

`record(op_or_name, params, result, tag)` 的一次调用：

```text
record(op_or_name, params, result, tag="tileops")
  ├── 判定身份：Op 实例？ → name = 类名, op_module = 模块, op_config = 配置
  │                 字符串？ → name = 该串, op_module = None, op_config = None
  ├── 过滤 params：丢弃不可序列化项、私有项、临时局部变量
  ├── 组装 record_entry = {params, result, tag, [config]}
  ├── _records[name].append(record_entry)            # → 喂给 dump()
  └── _bench_results.entries.append({tag, op:name, [op_module], **result})  # → 喂给 conftest
```

`dump(path)` 的流程：

```text
dump("profile_run.log")
  ├── 写报告头 + 时间戳
  ├── ## Environment（torch/cuda 版本、GPU 型号、驱动、时钟）
  └── 对 _records 里每个 op name：
        ├── 写 ## {op_name}
        └── 按 tag 分组：
              ├── 写 ### {tag}
              └── 写 markdown 表格（列 = 参数键 + 结果键 [+ config]）
```

#### 4.1.3 源码精读

类与三个静态方法的声明，注意全部是 `@staticmethod`、状态在类级 `_records`：

[BenchmarkReport 类与 record/dump/clear](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/benchmarks/benchmark_base.py#L628-L768) —— `BenchmarkReport._records: dict = {}` 是类属性，所有调用共享。

`record` 的身份判定与两条存储写入：

[record 身份判定 + params 过滤 + 双写](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/benchmarks/benchmark_base.py#L647-L694) —— 关键点：传字符串时 `op_module = None`、`op_config = None`；传 Op 实例时三者都从对象上反射出来。`_is_serializable` 保证 `shape=(4096,4096)` 这种「原始元组」原样保留，而不是被拍平成元素数。

`dump` 的「按 op name → 按 tag」两级分组：

[dump 按 tag 分组生成表格](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/benchmarks/benchmark_base.py#L714-L758) —— 先 `setdefault(entry["tag"], []).append` 聚合，再对每个 tag 组打印表头与数据行；`result_keys` 会把 `timing` 这类附加结果键自动并入列。

生命周期钩子在 conftest：

[sessionstart→clear, sessionfinish→dump](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/benchmarks/conftest.py#L23-L28) —— 这就是「`pytest benchmarks/` 自动生成 `profile_run.log`」的来源。

#### 4.1.4 代码实践

实践目标：亲眼看到 `profile_run.log` 的结构，理解「按 op name → 按 tag」两级分组。

操作步骤（需 CUDA GPU）：

1. 跑一个最小基准子集，例如只跑 norm：
   ```bash
   pytest benchmarks/ops/bench_norm.py -q
   ```
2. 打开产物 `profile_run.log`，定位到 `## RMSNormFwdOp` 区块。

需要观察的现象：

- 报告顶部是 `## Environment`，列出 torch/cuda 版本、GPU 型号、驱动与时钟（来自 `_get_env_metadata`）。
- 每个 op name 下有若干 `### {tag}` 小节，每个小节是一张 markdown 表。
- 同一个 op 的 `tileops` 与 `torch-ref` 两条记录**分属两个 tag 小节**，而不是挤在一张表里。

预期结果：`## RMSNormFwdOp` 下能看到 `### tileops` 与 `### torch-ref` 两张表，列里都有 `latency_ms` / `tflops` / `bandwidth_tbs`。若该次运行 CUPTI 失败走了回退，`tileops` 表里会多出一列 `timing=cuda-events`（由 u6-l1 讲过的 `_build_result` 注入）。

> 若本机无 GPU 或 pytest 因 JIT 报错，此步标为「待本地验证」；可改为直接读 `benchmarks/ops/bench_norm.py` 的 `record` 调用，手推 `dump()` 会生成几个 tag 小节。

#### 4.1.5 小练习与答案

**练习 1**：`BenchmarkReport` 为什么没有 `__init__`、方法全是静态？如果改成实例方法会有什么后果？
**答案**：因为基准函数是各自独立的 pytest test，没有地方长期持有一个「报告实例」；类级 `_records` 让所有 test 共享同一份收集器。改成实例方法就需要在 session 级 fixture 里持有实例并在每个 test 间传递，徒增耦合，且线程局部 `_bench_results` 仍要单独存在。

**练习 2**：`_records` 是类属性 `_records: dict = {}`，两次连续 `pytest` 运行之间会残留吗？
**答案**：进程级不残留——每次 `pytest` 是新进程，类属性重新初始化；进程内由 `pytest_sessionstart` 的 `clear()` 显式清空，保证同一 session 内不混入上轮数据。

---

### 4.2 tag 体系与报告分组

#### 4.2.1 概念说明

tag 是一条基准结果的「实现来源」标签。它的核心约定是：

- **`tileops` 是被测方**——永远是本库自己的实现。
- **其余 tag 都是基线（baseline）**——PyTorch、第三方库等参照实现。

仓库里实际在用的 tag（按出现频次大致）：`tileops`（124 次）、`torch`、`torch-ref`、`fla`、`fa3`、`flashinfer`、`torch-sdpa`、`vllm`、`mamba`、`torch-cublas`、`triton`、`sgl-kernel` 等。testing.md 要求**优先复用既有 tag**，不要随手发明新 tag，否则下游报表脚本会漏识别。

tag 在两处发挥分流作用：

1. `dump()` 用它做二级分组（见 4.1）。
2. **conftest 钩子**用 `tag.startswith("tileops")` 把被测方与基线分开，并据此计算加速比。

注意「以 `tileops` 开头」而非「等于 `tileops`」——这允许变体 tag，如 `tileops-fused`、`tileops-nopad-3wg`、`tileops-unfused`，它们仍被认作 tileops 侧的实现变体。

#### 4.2.2 核心流程

conftest 里 `pytest_runtest_call` 钩子对一个 test 收集到的 entries 做拆分：

```text
entries = _bench_results.entries          # 本次 test 内所有 record() 的条目
  for e in entries:
      if e["tag"].startswith("tileops"):  → tileops_entry（取第一条）
      else:                               → baseline_entries
```

随后：

- tileops 侧写出 `op` / `op_module` / `tileops_variant`（若 tag 形如 `tileops_xxx`）/ `tileops_latency_ms` / `tileops_tflops` / `tileops_bandwidth_tbs`。
- 每条基线写出 `{tag}_latency_ms` / `{tag}_tflops` / **`{tag}_ratio`**，其中 ratio 是基线 latency 与 tileops latency 之比：

\[
  \text{ratio} = \frac{\text{baseline\_latency\_ms}}{\text{tileops\_latency\_ms}}
\]

ratio > 1 表示 tileops 更快（基线更慢）。第一条基线还会额外写一套「无前缀」的旧键（`baseline_tag` / `baseline_latency_ms` / `baseline_ratio`）以兼容老的 nightly 报表脚本。

#### 4.2.3 源码精读

conftest 的 tileops/基线拆分与变体提取：

[conftest 拆分 tileops 与 baseline](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/benchmarks/conftest.py#L41-L65) —— `tag.startswith("tileops")` 是判据；变体名通过 `tag[len("tileops_"):]` 截取（注意代码里判的是 `tileops_` 带下划线）。

基线属性写入（含兼容旧键与 tag 前缀键、以及 ratio 计算）：

[基线 JUnit 属性与 ratio](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/benchmarks/conftest.py#L71-L95) —— `bl_latency / tl` 即 ratio 公式的代码化；`tl > 0 and bl_latency > 0` 防止除零。

testing.md 对 tag 复用与基线强制的要求：

[Reporting rules: 至少一条非 tileops 基线 + 复用既有 tag](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/docs/design/testing.md#L152-L154) —— 明确列出推荐 tag 集合，并要求引入新 tag 前先更新下游消费者。

一个干净范例：`bench_norm.py` 同时记 `tileops` 与 `torch-ref`：

[bench_norm 记录 tileops 与 torch-ref 两条](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/benchmarks/ops/bench_norm.py#L53-L57) —— 同一个 `op` 实例、同一份 `locals()`，只有 `result` 与 `tag` 不同，这是写基线的标准范式。

#### 4.2.4 代码实践

实践目标：理解变体 tag 如何被 conftest 识别为 tileops 侧。

操作步骤：

1. 阅读 `benchmarks/ops/bench_moe_fused_moe.py`，找到形如 `tag="tileops-fused"` / `tag="tileops-nopad-3wg"` 的 record 调用。
2. 对照 conftest 的拆分逻辑，回答：这些变体 tag 会被放进 `tileops_entry` 还是 `baseline_entries`？变体名（如 `fused`）会以什么 JUnit 属性键出现？

需要观察的现象：变体 tag 仍算 tileops 侧，且 `tileops_variant` 属性会带上 `fused` / `nopad-3wg` 等后缀。

预期结果：conftest 会把它们归入 tileops，并写 `tileops_variant=fused`；同时它们**不会**被当作基线，因此不会产生 `{tag}_ratio`。

> 若无 GPU，本步为纯源码阅读型实践，结论可由阅读 conftest 第 45–57 行直接得出。

#### 4.2.5 小练习与答案

**练习 1**：一个 test 只记了 `tileops`、没记任何基线，conftest 会怎样？这违反了哪条规则？
**答案**：`baseline_entries` 为空，JUnit 里只有 tileops 属性、没有 `baseline_*` / `*_ratio`。这违反 testing.md「每个基准必须记录至少一条非 tileops 基线」，SOL 效率无法计算。

**练习 2**：为什么 ratio 的定义是 `baseline / tileops` 而不是 `tileops / baseline`？
**答案**：这样 ratio > 1 直观表示「tileops 比基线快多少倍」，越大越好，与「性能优化追求更大值」的方向一致，便于报表排序与阈值告警。

---

### 4.3 规范身份（Op 实例 vs 字符串别名）与基线规则

#### 4.3.1 概念说明

这是本讲最关键的设计点。`record()` 的第一参数 `op_or_name` 既接受 **Op 实例**，也接受**字符串**。两种走法在「身份信息」上天差地别：

| 传入形式 | `name` | `op_module` | `op_config` |
| --- | --- | --- | --- |
| Op 实例 | `op.__class__.__name__`（如 `GemmOp`） | `op.__class__.__module__`（如 `tileops.ops.gemm`） | 从 op 反射出的 kernel config |
| 字符串别名 | 该字符串原样 | `None` | `None` |

**为什么要「类名 + 模块」两件套才算规范身份？** 因为光有类名不够：仓库里可能存在同名类分布在不同模块，或一个基准用字符串拼出一个「派生名」（如 `f"{op_name}_fp8"`，会得到 `MulFwdOp_fp8`、`WhereFwdOp_fp8` 这类）。字符串别名只给一个孤零零的名字，丢失了「这条结果到底属于哪个模块里的哪个 op」这条信息，下游报表就无法可靠归类，容易把不同来源的结果误并到一组。

因此 testing.md 的指导是：第一参数**可以是 Op 实例或字符串，但同一基准文件内要保持一致**；而要拿到完整规范身份（含 `op_module`、`op_config`），就必须传 Op 实例。

与之配套的另一条信任边界是 **oracle 隔离**：基线的 ref 函数（参照实现）必须**就地定义在基准文件里**，绝不能从 `tests/` 或 `workloads/` 导入。因为测试文件里的 `ref_program` 是正确性判据（ground truth），若基准直接复用它，就会把「正确性依赖」与「性能对比」耦合在一起——一旦 ref 被改，正确性测试和性能基线会同步漂移，信任模型就被破坏。

#### 4.3.2 核心流程

身份与配置的提取链：

```text
record(op_or_name, ...)
  if isinstance(op_or_name, str):  name=串, op_module=None, op_config=None
  else:                             name=类名, op_module=模块
                                    op_config = _extract_op_config(op)
                                        ├── 优先 op.config（显式覆盖）
                                        ├── 再试 op.kernel.config（eager-init）
                                        └── 再试 op._kernel_cache 里首个 kernel 的 config（纯 lazy）
```

`_extract_op_config` 之所以要兜底三种 Op 形态，是因为 TileOPs 里 Op 的 kernel 装配方式不统一（见 u2/u3）：有的在 `__init__` 里 eager 装好 `op.kernel`，有的是 lazy 缓存 `op._kernel_cache`，有的带个 dummy kernel 占位。无论哪种，config（block 尺寸、num_warps 等调优参数）都是后续看报告时定位「这跑的是哪份 kernel 配置」的关键，所以要尽量提取出来附在记录里。

#### 4.3.3 源码精读

`record` 第一参数的身份分支：

[record: Op 实例 vs 字符串的身份判定](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/benchmarks/benchmark_base.py#L647-L654) —— 字符串走法直接放弃 `op_module` 与 `op_config`。

`_extract_op_config` 三形态兜底：

[_extract_op_config 兜底三种 Op 装配形态](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/benchmarks/benchmark_base.py#L586-L625) —— 注释明确列出 eager-init（`GemmOp`）、lazy-with-dummy（`FFTC2COp`）、pure-lazy-cache（`_SoftmaxBaseOp` / 规约算子）三种模式。

「字符串别名」的真实用例——elementwise 的 FP8 基准用拼出来的字符串名：

[bench_independent_elementwise 用 f"{op_name}_fp8" 字符串别名](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/benchmarks/ops/bench_independent_elementwise.py#L384-L391) —— 这里第一参数是字符串，因此这些记录的 `op_module` 为 `None`、无 `op_config`，下游只能靠名字归类，是规范身份不完整的典型情形。

对比正确的做法——`bench_gemm.py` 始终传 Op 实例：

[bench_gemm 传 op 实例记录 tileops 与 torch-cublas](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/benchmarks/ops/bench_gemm.py#L110-L119) —— `op = GemmOp(...)` 后，tileops 与基线两条记录都传同一个 `op` 实例，身份与 config 都齐全。

testing.md 对「第一参数可实例可字符串」与「ref 就地定义」的规定：

[record 第一参数约定 + ref 就地定义](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/docs/design/testing.md#L137-L138) —— 「never import from tests/ or workloads/」即 oracle 隔离边界。

#### 4.3.4 代码实践

实践目标：用真实代码体会「传 Op 实例 vs 传字符串别名」在身份上的差异。

操作步骤（源码阅读型，无需 GPU）：

1. 打开 `benchmarks/ops/bench_gemm.py` 第 110–119 行，确认 `BenchmarkReport.record(op, ...)` 传的是 `GemmOp` 实例。手推：`name="GemmOp"`、`op_module="tileops.ops.gemm"`、`op_config` 来自 `op.kernel.config`。
2. 打开 `benchmarks/ops/bench_independent_elementwise.py` 第 384 行，确认 `record(f"{op_name}_fp8", ...)` 传的是字符串。手推：`name` 形如 `"MulFwdOp_fp8"`、`op_module=None`、`op_config=None`。
3. 回答：如果同一份报告里既有 `tileops.ops.gemm.GemmOp`，未来又在别的模块（如某个实验性 `tileops.ops.experimental.gemm`）出现同名 `GemmOp`，传 Op 实例能把二者区分开吗？传字符串 `"GemmOp"` 呢？

需要观察的现象：传实例时 JUnit 的 `op_module` 属性能区分两个 `GemmOp`；传字符串时两者都只显示 `GemmOp`，下游报表会把它们误并。

预期结果：这正是「record 必须传 Op 实例而非字符串别名」的原因——实例给出「类名 + 模块」的规范身份，字符串只给孤名。testing.md 要求同一文件内保持一致，是为了避免同一 op 在报告里时而带模块、时而不带，造成归类混乱。

> 关于本讲规格里提到的 `maximum` / `cmp_eq` 这类别名：仓库里实际的算子名是规范类名 `MaximumFwdOp`、`EqFwdOp` 等（见 `tileops/manifest/elementwise_binary.yaml`），真正的字符串别名用例是上面第 2 步的 `f"{op_name}_fp8"` 与 `"where_fp8"`。结论一致——字符串别名丢失模块信息。

#### 4.3.5 小练习与答案

**练习 1**：为什么 oracle 隔离要求 ref 函数就地定义，而不能从 `tests/` 导入测试里的 `ref_program`？
**答案**：测试里的 `ref_program` 是正确性判据（ground truth）。若基准复用它，正确性与性能基线就耦合到同一份代码：ref 一改，正确性测试和性能基线同步漂移，你将无法独立判断「是实现变慢了还是 ref 变了」。就地定义让性能基线成为独立的、可追溯的参照。

**练习 2**：`_extract_op_config` 为什么要兜底「纯 lazy 缓存」这种形态（取 `_kernel_cache` 首个 kernel 的 config）？
**答案**：纯 lazy 的 Op（如 `SoftmaxFwdOp`）在构造期没有 `op.kernel`，kernel 只在首次 forward 后才进 `op._kernel_cache`。基准是在 `profile()`（即 forward）之后才 `record()`，此时缓存里已有 kernel，取首个即可拿到 config；同一 op 的缓存 kernel 共享 dtype/op_kind，取首个对「一条记录对应一份 config」足够。

---

## 5. 综合实践

把本讲三块内容串起来：**收集 → 归类身份 → 基线对比**。

任务：为 `bench_gemm.py` 里的一次 `test_gemm_bench` 手推「数据从产生到落盘」的完整旅程。

1. **产生**：`bm.profile(op, a, b)` 调 `bench_kernel` 得 latency，`_build_result` 算出 `{latency_ms, tflops, bandwidth_tbs}`（u6-l1）。
2. **记录 tileops**：`BenchmarkReport.record(op, locals(), result, tag="tileops")`。写出这一步会产生哪些字段——`name`、`op_module`、`op_config` 分别是什么？`_records["GemmOp"]` 与 `_bench_results.entries` 各追加什么？
3. **记录基线**：紧接着 `record(op, locals(), result_bl, tag="torch-cublas")`，同样手推字段。
4. **conftest 拆分**：本 test 的 entries 进入 `pytest_runtest_call`，哪条进 `tileops_entry`、哪条进 `baseline_entries`？会写出哪些 JUnit 属性？`torch-cublas_ratio` 的值怎么算？
5. **落盘**：session 结束 `dump("profile_run.log")`，`## GemmOp` 下会出现哪几个 `### tag` 小节？

完成后，你应该能用一张图把「一次 record 调用 → 两条存储 → 两类消费者（markdown 报告 / JUnit XML）」完整画出来，并解释每个环节里「Op 实例」与「tag」各自承载了什么信息。

> 若有 GPU，可实际运行 `pytest benchmarks/ops/bench_gemm.py -q` 后查看 `profile_run.log` 与（若启用 JUnit）XML 里的 `user_properties` 来对照你的手推；若无 GPU，本任务为纯源码追踪型，结论由阅读本讲引用的源码行直接得出。

## 6. 本讲小结

- `BenchmarkReport` 是进程内静态收集器，`record/dump/clear` 全为 `@staticmethod`，状态在类级 `_records`；生命周期由 conftest 驱动——session 开始 `clear()`、session 结束 `dump("profile_run.log")`。
- `record()` 同时写两条路径：`_records`（喂 markdown 报告）与线程局部 `_bench_results.entries`（喂 conftest → JUnit XML），保证「人看的」与「机器读的」一致。
- tag 体系以 `tileops` 为被测方、其余为基线；conftest 用 `tag.startswith("tileops")` 拆分并支持 `tileops-fused` 等变体；每个基准必须记至少一条非 `tileops` 基线，否则无法算 SOL 加速比。
- `record()` 第一参数传 Op 实例才能拿到「类名 + 模块」的规范身份（canonical identity）和 kernel config；传字符串别名（如 `f"{op_name}_fp8"`）会丢失 `op_module` 与 `op_config`，造成归类不完整。
- ref 函数必须就地定义，绝不从 `tests/` 或 `workloads/` 导入——这是 oracle 隔离信任边界，防止正确性判据与性能基线耦合漂移。

## 7. 下一步学习建议

- 本讲得到的 `latency_ms` / `tflops` / `bandwidth_tbs` 与基线 ratio 是 **raw time**（M4 产物）。下一单元 u7（Roofline 性能模型）会把这些数与硬件理论上限对照，算出 SOL 效率与 bound type——建议接着读 u7-l1。
- 想理解 `eval_roofline()`（`ManifestBenchmark` 读 FLOP/字节的来源）如何被 codegen 合成，可跳到 u8-l2（Roofline 代码生成）。
- 若对「基准如何按文件进程隔离运行、失败如何兜底」感兴趣，可读 u6-l4（夜行基准 CI 运行器），它解释了本讲的 conftest 钩子在「每文件一进程」下如何保证不丢报告。
