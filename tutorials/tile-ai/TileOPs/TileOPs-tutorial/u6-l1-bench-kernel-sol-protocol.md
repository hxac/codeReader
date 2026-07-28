# bench_kernel 与 SOL 计时协议

## 1. 本讲目标

本讲进入 TileOPs 的性能基准（benchmark）层。正确性测试回答的是「算得对不对」，基准回答的是「算得快不快」——而要回答「快不快」，必须先把「一次 kernel 到底花了多少时间」测准。学完本讲，你应该能够：

- 理解 `BenchmarkBase[W]` 泛型基类的职责，以及 `calculate_flops` / `calculate_memory` 两个抽象方法的契约。
- 掌握 `bench_kernel` 的 NVIDIA SOL-ExecBench 风格计时协议：warmup / repeat / trial 三层结构、输入克隆、CUPTI 纯 kernel 计时。
- 理解为什么要做 L2 cache flush，以及「标注窗口（annotation window）投影」如何只统计被测 kernel 而排除 flush 本身。
- 理解 CUPTI 投影失败时回退到 CUDA-events 的条件、回退路径与 CUPTI 的计时差异（约 6–7 倍膨胀），以及如何用 `TILEOPS_ALLOW_CUDA_EVENTS_FALLBACK` 开关阻止回退。

本讲是性能篇的第一块基石：后续 u6-l2（manifest 驱动基准）、u6-l3（报告与基线）、u7（roofline 性能模型）都建立在「测出的 latency 可信」这一前提之上。

## 2. 前置知识

- **Op / Kernel 双层分离**（u1-l1、u2-l1）：一个算子被拆成主机侧入口 `Op`（L2）和 TileLang GPU 实现 `Kernel`（L1）。基准测的是「调用一次 Op」的端到端 GPU kernel 时间。
- **Speed-of-Light（SOL）效率**（u1-l1、u7-l1）：TileOPs 不和别的实现比谁快，而是和硬件理论上限比。效率定义为：

  \[ \text{SOL efficiency} = \frac{\text{理论最短时间（roofline 推出的下界）}}{\text{实际测得的 kernel 时间}} \]

  这意味着「实际时间」必须尽可能纯净——只含 kernel 执行，不含 Python 调度、CUDA launch overhead、缓存预热。本讲讲的就是如何把这个「实际时间」测干净。
- **测试三件套**（u5-l1）：`WorkloadBase`（`gen_inputs` 生成输入）、`TestBase`、`FixtureBase`。基准也复用 workload 来生成输入，但**永不导入 test**（避免把正确性参考实现泄漏进基准）。
- **pytest 参数化**：基准本质上是 `@pytest.mark.parametrize` 的测试函数，运行 `pytest benchmarks/` 会自动生成 `profile_run.log`。

一个关键直觉：GPU 计时和 CPU 计时不同。CPU 上「调用 + 计时」是同步的；GPU 上「调用」只是把一个 kernel 排进队列（launch），真正执行在毫秒级之后。如果用 CPU 墙钟时间计时，会把大量的 launch overhead（约 50–60 微秒）和 Python 开销算进 kernel 时间里，对快 kernel（< 10 微秒）会造成约 6–7 倍的膨胀。`bench_kernel` 的全部复杂度，都是在解决「如何只测 GPU kernel 本身的执行时间」。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `benchmarks/benchmark_base.py` | 基准框架全部核心：`bench_kernel` 计时协议、`BenchmarkBase` 泛型基类、`ManifestBenchmark`、`BenchmarkReport` 收集器。本讲聚焦前两者。 |
| `docs/design/testing.md` | 测试与基准的设计规约，定义 `BenchmarkBase[W]` 的契约与基准文件清单（file checklist）。 |
| `benchmarks/ops/bench_norm.py` | 真实基准示例，展示 `ManifestBenchmark` + `profile` + `BenchmarkReport.record` 的标准用法。 |

## 4. 核心概念与源码讲解

### 4.1 BenchmarkBase 泛型基类与 FLOP/字节记账

#### 4.1.1 概念说明

`BenchmarkBase` 是所有基准类的抽象基类。它的职责不是「计时」（计时交给 `bench_kernel`），而是把「一次 profile 的结果」组装成一个结构化的字典，并算出 TFLOPS 与带宽这两个衍生指标。

它用 `Generic[W]` 做泛型参数化，`W` 是 **workload 的能力协议（capability protocol）**，而不是具体的 `WorkloadBase` 类。这是个重要的设计点：不同基准需要 workload 提供不同的能力（有的只需要 `shape`/`dtype` 元数据，有的还需要 `gen_inputs()`），用协议而不是固定基类，可以让基准声明「我精确需要哪些能力」。

两个抽象方法 `calculate_flops()` 和 `calculate_memory()` 是子类必须实现的契约：返回该 workload 的算术量（FLOP 数）和内存搬运量（字节数）。注意本讲只关注「它们怎么被 `_build_result` 用掉」，至于数值从哪来（manifest roofline 还是手算）是 u6-l2 的主题。

#### 4.1.2 核心流程

`BenchmarkBase` 的对外入口是 `profile(functor, *inputs)`，流程：

1. 在 `torch.no_grad()` 下调用 `bench_kernel(functor, args=inputs)`，拿到纯 kernel latency（毫秒）。
2. 把 latency 交给 `_build_result` 组装结果字典。
3. `_build_result` 做三件事：
   - 记录 `latency_ms`；
   - 如果本次计时偏离了默认协议（比如回退到了 CUDA-events，或跳过了输入克隆），把偏离信息塞进结果，保证报告透明；
   - 用 `calculate_flops()` / `calculate_memory()` 算出 TFLOPS 和带宽：

     \[ \text{TFLOPS} = \frac{\text{flops}}{\text{latency\_ms} \times 10^{-3}} \times 10^{-12}, \qquad \text{bandwidth (TB/s)} = \frac{\text{bytes}}{\text{latency\_ms} \times 10^{-3}} \times 10^{-12} \]

     （代码里用 `flops / latency * 1e-9`，因为 latency 单位是毫秒，换算后即得 TFLOPS / TB·s⁻¹。）

#### 4.1.3 源码精读

`BenchmarkBase` 用 `Generic[W], ABC` 双继承，`W` 是能力协议的类型参数：

[BenchmarkBase 泛型基类与抽象方法 — benchmarks/benchmark_base.py:413-432](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/benchmarks/benchmark_base.py#L413-L432)

> `calculate_flops` / `calculate_memory` 用 `@abstractmethod` 标注，返回 `Optional[float]`——返回 `None` 表示该指标不适用，结果里就省略它（见 testing.md 第 153 行的规则）。

`profile` 在 `torch.no_grad()` 下调用 `bench_kernel`，然后组装结果：

[profile 入口 — benchmarks/benchmark_base.py:434-445](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/benchmarks/benchmark_base.py#L434-L445)

> 还有个孪生方法 `profile_autograd`（447–455 行），用于需要反向传播（fwd+bwd）的场景：它**不**包 `torch.no_grad()`，且要求传入零参闭包（callable 自己捕获输入）。计时协议完全相同。

结果组装逻辑在 `_build_result`，注意它如何把「协议偏离」透明化：

[_build_result 组装与协议偏离标注 — benchmarks/benchmark_base.py:457-471](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/benchmarks/benchmark_base.py#L457-L471)

> `_bench_meta` 是个线程局部变量（89–90 行），`bench_kernel` 在运行时把这次计时的元信息（`timing`、`inputs_cloned`）写进去，`_build_result` 读出来。只有当 `timing != "cupti"` 或 `inputs_cloned is False` 时才写入结果字段——也就是说，默认（CUPTI + 已克隆）的结果字典里**不会**出现这两个键，报告保持干净；一旦偏离，立刻可见。这是「测出的数字必须诚实」的工程体现。

#### 4.1.4 代码实践

**实践目标**：确认 `_build_result` 的指标换算，并理解 `Optional` 的语义。

**操作步骤**：

1. 打开 `benchmarks/benchmark_base.py` 的 `_build_result`（457–471 行）。
2. 手算：若某次 RMSNorm profile 得到 `latency_ms = 0.02`（20 微秒），`calculate_flops()` 返回 `8_388_608`（2×M×N，M=1024,N=4096 的平方和+归一化近似），验证 `result["tflops"]` 应为 `8388608 / 0.02 * 1e-9 ≈ 419.4`。
3. 阅读一个真实基准 `benchmarks/ops/bench_norm.py` 的 `test_rms_norm_bench`（41–55 行），对照它如何构造 `ManifestBenchmark`、调用 `bm.profile(op, *inputs)`、再用 `BenchmarkReport.record(op, locals(), result, tag="tileops")` 记录，以及随后用同一 `bm` 记录一条 `tag="torch-ref"` 基线。

[基准示例 — benchmarks/ops/bench_norm.py:41-55](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/benchmarks/ops/bench_norm.py#L41-L55)

**需要观察的现象**：`profile` 返回的字典在默认（CUPTI）路径下只含 `latency_ms` / `tflops` / `bandwidth_tbs` 三个键；只有走 CUDA-events 回退时才会多出 `timing` 键。

**预期结果**：手算的 TFLOPS 与代码公式 `flops / latency * 1e-9` 一致。**待本地验证**（需真实 GPU）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `BenchmarkBase` 用 `Generic[W]` 而不是直接写成 `BenchmarkBase(ABC)` 并在 `__init__` 里收 `WorkloadBase`？

**答案**：因为不同基准对 workload 的能力需求不同。`ShapeDtypeWorkload`（只要 `shape`/`dtype`）够 `ManifestBenchmark` 用；而需要真正生成输入的基准要 `InputGeneratingWorkload`（`gen_inputs()`）。用协议类型参数 `W`，每个基准可以精确声明「我需要哪些能力」，而不被迫依赖最重的 `WorkloadBase`。参见 testing.md 第 122–130 行。

**练习 2**：若 `calculate_flops()` 返回 `None`，结果字典会怎样？

**答案**：`_build_result` 里 `if flops is not None` 守卫，`None` 时直接跳过，结果字典里不会出现 `tflops` 键。`calculate_memory()` 同理。

---

### 4.2 bench_kernel 计时协议（warmup / repeat / trial / 输入克隆）

#### 4.2.1 概念说明

`bench_kernel` 是整个基准层的计时心脏。它实现了 NVIDIA 的 **SOL-ExecBench** 风格协议（源码注释引用了 arxiv.org/abs/2603.19173），核心目标是拿到**纯 kernel 执行时间**，剔除 launch overhead 与缓存效应。

它有三层嵌套的循环结构，理解每一层的目的非常关键：

| 层 | 默认值 | 作用 |
| --- | --- | --- |
| `n_warmup`（预热） | 10 | 让 kernel 完成 JIT 编译、CUDA context 初始化、缓存预热。**不计时**。 |
| `n_trials`（独立试验） | 3 | 独立重复整个测量，取中位数以抵抗偶发的异常试验（系统抖动、其它进程抢占）。 |
| `n_repeat`（每次试验内的迭代） | 50 | 在一个 CUPTI profiling 窗口里连续跑 N 次，窗口内总 kernel 时间除以 N，得到该 trial 的平均。 |

最终返回的是 **3 个 trial mean 的中位数**（单位毫秒）。

另一个关键设计是**输入克隆**：每次迭代都给 kernel 喂「新鲜地址」的输入张量。这听起来奇怪——输入内容又没变，为什么要克隆？因为现代 GPU 和驱动会缓存「同一地址的张量」的访存模式，连续重复访问同一块内存会得到过分乐观的 L2 命中率，掩盖真实的 DRAM 行为。克隆让每次访存都「冷启动」，测到的是真实的 SOL 行为。

#### 4.2.2 核心流程

`bench_kernel(fn, args, n_warmup=10, n_repeat=50, n_trials=3)` 的执行步骤：

```
1. 校验 args 是 tuple（gen_inputs() 必须返回 tuple）。
2. 取 L2 flush 缓冲 cache（按真实 L2 大小分配，见 4.4）。
3. 构造输入克隆池：
   - 若有 tensor 参数且总内存 × 3 份克隆 ≤ 1 GB：预克隆 3 份，轮转使用（_run(i) 用 pool[i%3]）。
   - 若超过 1 GB：跳过克隆（避免 OOM），记录 warning，_bench_meta.inputs_cloned = False。
   - 若无参数：直接 fn()。
4. 预热：循环 n_warmup 次，每次 cache.zero_()（flush）+ _run(i)，最后 torch.cuda.synchronize()。
5. CUPTI 计时主路径（见 4.3）：每个 trial 开一个 torch.profiler 上下文，窗口内跑 n_repeat 次，
   用 _sum_kernel_time_us 统计窗口内纯 kernel 时间。
6. 若 CUPTI 失败 → CUDA-events 回退（见 4.4）。
7. 释放克隆池 + empty_cache()，返回 trial_means 的中位数。
```

为什么是中位数而不是均值？因为系统抖动（其它进程、驱动调度）会让个别 trial 偏大，均值会被拉高，中位数对离群值更稳健。

#### 4.2.3 源码精读

函数签名与协议文档：

[bench_kernel 签名与 SOL-ExecBench 协议说明 — benchmarks/benchmark_base.py:187-220](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/benchmarks/benchmark_base.py#L187-L220)

> 注意 docstring 第 203 行明确：返回的是「median trial mean（对异常 trial 稳健）」。

输入克隆池的构造，含 1 GB 跳过阈值：

[输入克隆池与 1 GB 跳过阈值 — benchmarks/benchmark_base.py:227-259](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/benchmarks/benchmark_base.py#L227-L259)

> 关键点：`_bench_meta.inputs_cloned = arg_pool is not None or not has_args`（259 行）。即使没有参数（`not has_args`），也算「已克隆」（因为没有可克隆的输入，不存在地址复用问题），所以默认是 `True`；只有「有参数但太大被跳过克隆」时才是 `False`，这时报告会标注 `inputs_cloned: False`。

预热循环（不计时）：

[warmup 预热 — benchmarks/benchmark_base.py:262-265](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/benchmarks/benchmark_base.py#L262-L265)

最终返回中位数（注意是排序后取中点）：

[返回 trial 中位数 — benchmarks/benchmark_base.py:357-358](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/benchmarks/benchmark_base.py#L357-L358)

> `trial_means.sort()` 后取 `len//2`。对默认 3 个 trial，就是取排序后的第 2 个（中位数）。

#### 4.2.4 代码实践

**实践目标**：理解克隆池的轮转机制与中位数选取。

**操作步骤**：

1. 阅读 227–259 行，回答：`_N_CLONES = 3` 的克隆池在 `n_repeat = 50` 次迭代中如何轮转？（`arg_pool[i % 3]`，即地址在 3 份之间循环复用。）
2. 思考：为什么不把 50 次迭代每份都克隆成独立的 50 份？答案是内存——3 份轮转已经足够打破「完全相同的地址序列」带来的缓存惯量，同时把内存占用压到 1/17。
3. 阅读 357–358 行，确认 3 个 trial 的中位数选取。设想 trial means 为 `[0.018, 0.021, 0.095]`（第三个被系统抖动拉高），中位数是 `0.021`，而均值是 `0.045`——验证中位数对离群值更稳健。

**需要观察的现象**：理解代码后，能口述「一次 `bench_kernel` 调用总共执行 `n_warmup + n_trials × n_repeat = 10 + 3×50 = 160` 次 kernel」。

**预期结果**：能解释轮转复用与中位数稳健性。**待本地验证**（若要实测数字需 GPU）。

#### 4.2.5 小练习与答案

**练习 1**：为什么预热阶段（warmup）不计入任何统计？

**答案**：预热让 TileLang kernel 完成首次 JIT 编译（首次调用明显变慢，见 u1-l2）、让 CUDA context 与 driver 缓存就绪。如果把这些一次性开销算进 kernel 时间，测到的就不是「稳态执行时间」而是「冷启动时间」，SOL 效率会假性偏低。

**练习 2**：某超大输入总内存为 2 GB，克隆逻辑会如何处理？结果字典会多出什么字段？

**答案**：2 GB × 3 = 6 GB > 1 GB 阈值（239 行），走 `else` 分支：跳过克隆，记 warning，`arg_pool = None`，`_run(i)` 直接用原 `args`。于是 `_bench_meta.inputs_cloned = False`，`_build_result` 会在结果字典里加 `"inputs_cloned": False`，提醒读者这次测的是「地址复用」条件下的数字。

---

### 4.3 CUPTI 纯 kernel 计时与标注窗口投影

#### 4.3.1 概念说明

CUPTI（CUDA Profiling Tools Interface）是 NVIDIA 提供的底层 profiling 接口，能直接读到每个 GPU kernel 的精确执行时间，**不含 launch overhead**。`bench_kernel` 通过 `torch.profiler`（底层是 Kineto + CUPTI）来获取它。

但这里有个棘手的问题：profiler 捕获的是一段时间内**所有** GPU 活动——既包括被测 kernel，也包括 L2 flush 的 `cache.zero_()`。如果把 flush 时间也算进去，就污染了测量。TileOPs 的解法是**标注窗口（annotation window）**：

- 用 `torch.profiler.record_function("tileops_bench_kernel")` 把「被测调用」包起来，这个 Python scope 会被 Kineto **投影（project）** 到设备时间线上，形成一个时间窗口。
- 统计时，只数「落在窗口内的 CUDA kernel」，窗口外的（flush）一律排除。
- 而且无论被测 kernel 叫什么名字都算（因为按窗口而非按名字过滤），所以哪怕一个 op 内部 launch 了多个 kernel，全都计入。

#### 4.3.2 核心流程

每个 trial 的 CUPTI 计时流程：

```
开 torch.profiler.profile(activities=[CPU, CUDA]):
  for i in range(n_repeat):
    cache.zero_()              # flush L2
    torch.cuda.synchronize()   # 把 flush 排干，确保其 device 事件落在窗口之前
    with record_function(_KERNEL_REGION):   # 打开标注窗口
      _run(i)                  # 被测调用（内部 launch 的 kernel 都进窗口）
    torch.cuda.synchronize()   # 排干被测调用，确保下一个 flush 不串入本窗口
total_us, n_regions = _sum_kernel_time_us(kineto_results)
if n_regions != n_repeat:      # 窗口数必须等于迭代数，否则投影不可信
    raise _CuptiProjectionError(...)
trial_means.append((total_us / n_repeat) * 1e-3)   # 微秒→毫秒
```

两个 `torch.cuda.synchronize()` 是关键工程细节：它们只增加主机侧延迟（等待 GPU），但保证了 flush 的 device 事件绝不会落入被测窗口。源码注释（267–272 行）解释了为什么不用 `torch.profiler.schedule`：因为它的 warmup/active 边界会让排队 launch 跨边界泄漏。

`_sum_kernel_time_us` 是统计核心：它遍历 Kineto C++ 事件（绕过 `key_averages()`，后者对大 trace 慢约 16 倍），收集所有 `_KERNEL_REGION` 标注窗口和所有 CUDA kernel，然后用二分查找判断每个 kernel 是否落在某个窗口内，累加窗口内的 kernel 时间。

#### 4.3.3 源码精读

标注窗口的常量定义，注释说明了为何 flush 不会落入窗口：

[_KERNEL_REGION 标注名 — benchmarks/benchmark_base.py:97-102](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/benchmarks/benchmark_base.py#L97-L102)

统计函数，用 bisect 判断 kernel 是否落在窗口内：

[_sum_kernel_time_us 窗口内 kernel 累加 — benchmarks/benchmark_base.py:105-143](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/benchmarks/benchmark_base.py#L105-L143)

> 关键：返回 `(total_us, n_regions)`，调用方用 `n_regions == n_repeat` 校验投影完整性（118–119 行注释）。

每个 trial 的 profiling 上下文与窗口包络，注意两次 `synchronize`：

[CUPTI trial 循环与标注窗口 — benchmarks/benchmark_base.py:274-311](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/benchmarks/benchmark_base.py#L274-L311)

> 第 288 行 `with torch.profiler.record_function(_KERNEL_REGION)` 是窗口边界。第 291 行调用 `_sum_kernel_time_us(profiler.profiler.kineto_results)`——直接拿 Kineto 结果对象，不走 Python 解析层。

投影完整性校验与异常（注意本轮新增的 kernel 计数诊断）：

[投影窗口数校验与诊断 — benchmarks/benchmark_base.py:294-310](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/benchmarks/benchmark_base.py#L294-L310)

> 本轮（commit 2392b7e）增强了诊断：当 `n_regions != n_repeat` 时，额外统计捕获到的 CUDA kernel 数 `n_cuda_kernels`（296–299 行），用 `_logger.debug` 记录详细的 mismatch 信息（300–305 行），并把 kernel 数写进异常消息（306–309 行）。这些诊断信息在排查「为什么 CUPTI 投影失败」时极有价值——它们能区分「一个 kernel 都没捕获到（环境彻底坏了）」和「捕获了 kernel 但窗口没投影上（Kineto 不稳定）」。

#### 4.3.4 代码实践

**实践目标**：理解窗口投影机制，能解释 `_sum_kernel_time_us` 为何只统计窗口内 kernel。

**操作步骤**：

1. 阅读 `_sum_kernel_time_us`（105–143 行），跟踪数据结构：`windows` 收集所有 `_KERNEL_REGION` 标注的 `(start_ns, end_ns)`，`kernels` 收集所有 CUDA kernel 的 `(start_ns, duration_ns)`。
2. 理解 140–142 行的二分查找：`bisect_right(starts, start_ns) - 1` 找到 kernel 起始时间对应的窗口索引，再判断 `start_ns < ends[idx]` 确认它确实落在窗口内。
3. 回答实践任务中的问题：`_sum_kernel_time_us` 为何只统计标注窗口内的 kernel？——因为窗口外的主要是 `cache.zero_()`（L2 flush），它不是被测对象；按窗口过滤而非按 kernel 名字过滤，保证被测 op 内部 launch 的多个 kernel（无论叫什么）都被正确计入。
4. 阅读本轮新增的诊断（296–309 行），设想一个场景：`n_regions=0` 但 `n_cuda_kernels=150`（50 repeat × 3，但全在窗口外）。这说明什么？——CUDA 活动捕获正常，但 `record_function` 的 scope 没被投影到设备时间线，是 Kineto 投影层的问题。

**需要观察的现象**：理解后能口述「窗口投影 = 用 Python 端的 record_function scope 作为时间过滤器，只保留该 scope 内的 device kernel」。

**预期结果**：能解释窗口过滤原理与新增诊断的含义。**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：`_sum_kernel_time_us` 为什么用 `bisect` 而不是对每个 kernel 遍历所有窗口？

**答案**：性能。窗口排序后，二分查找把「判断 kernel 属于哪个窗口」从 O(n_windows) 降到 O(log n_windows)。对 `n_repeat=50` 的窗口和大量 kernel 事件，这避免了 O(n²) 的扫描。源码注释（113–114 行）也提到要绕过慢 16 倍的 `key_averages()`，整体都是为了在大 trace 上保持速度。

**练习 2**：如果被测调用内部 launch 了 3 个 kernel（比如一个 fused op 的多 kernel 协作），`_sum_kernel_time_us` 会怎么算？

**答案**：全部计入。因为过滤是按「是否落在 `_KERNEL_REGION` 窗口内」，而非按 kernel 名字。只要这 3 个 kernel 的起始时间落在窗口内，它们的 `duration_ns` 都会被累加进 `total_us`。这正是多 kernel 协作算子（如 attention、cumsum 三阶段扫描，见 u3-l4）能被正确计时的基础。

---

### 4.4 L2 cache flush 与 CUDA-events 回退控制

#### 4.4.1 概念说明

**L2 cache flush** 是另一个保证测量纯净的关键机制。GPU 的 L2 cache 会缓存最近访问的内存，如果被测 kernel 第二次跑时输入还在 L2 里，它的有效带宽会假性飙升、延迟假性下降——测到的是「热缓存」而非真实 SOL 行为。`bench_kernel` 在每次迭代前调用 `cache.zero_()`：这块 `cache` 是一个按设备真实 L2 大小分配的缓冲，对它做 `zero_()` 会用无意义数据**填满整个 L2**，把上一轮的缓存内容挤出去，强迫被测 kernel 重新从 DRAM 取数。

这块缓冲是惰性分配、全局复用的（`_l2_flush_cache`），按 `torch.cuda.get_device_properties(0).L2_cache_size` 精确分配；若查询失败则退回 256 MB。

**CUDA-events 回退**是 CUPTI 不可用时的保底路径。当 CUPTI 投影失败（`n_regions != n_repeat`，通常意味着当前环境的 torch.profiler/Kineto 不稳定），`bench_kernel` 会改用 `torch.cuda.Event` 计时：在被测调用前后各 record 一个 event，用 `start.elapsed_time(end)` 取墙钟差。

但 CUDA-events 计时**包含 launch overhead**（约 50–60 微秒/次），对快 kernel（< 10 微秒）会造成约 **6–7 倍膨胀**，得到的 latency 不可用于 SOL 效率计算。因此本轮（commit 2392b7e）新增了环境变量 `TILEOPS_ALLOW_CUDA_EVENTS_FALLBACK`：

- 默认 `"1"`：允许回退（向后兼容，但会产生膨胀的、不可信的数字）。
- 设为 `"0"`：**禁止**回退，CUPTI 失败时直接抛 `RuntimeError`，宁可不产生数字，也不产生假数字。

这是一个「诚实优先」的设计选择：在 CI 或严肃基准里设 `"0"`，能阻止污染的 latency 流入 roofline 效率计算。

#### 4.4.2 核心流程

L2 flush 缓冲的分配：

```
_get_l2_flush_cache():
  若全局 _l2_flush_cache 为 None:
    l2_bytes = device.L2_cache_size   # 真实 L2 大小
    若 l2_bytes <= 0: 警告并退回 256 MB
    _l2_flush_cache = torch.empty(l2_bytes // 4, dtype=int32, device="cuda")
  返回全局缓冲
```

CUPTI 失败后的回退决策：

```
except _CuptiProjectionError as exc:
    allow_fallback = (TILEOPS_ALLOW_CUDA_EVENTS_FALLBACK == "1")   # 默认允许
    if not allow_fallback:
        raise RuntimeError(...)        # 禁止回退：直接报错
    _logger.warning(... 6-7x 膨胀 ...)
    trial_means = []                   # 触发下面的 events 路径

# CUDA-events 回退计时（仅在 trial_means 为空时）
for _ in range(n_trials):
    预分配 n_repeat 对 start/end events
    for i in range(n_repeat):
        cache.zero_()                  # flush
        torch.cuda.synchronize()       # 排干 flush（本轮新增，模仿 CUPTI 行为）
        start_events[i].record()
        _run(i)
        end_events[i].record()
    torch.cuda.synchronize()
    times = [s.elapsed_time(e) ...]
    trial_means.append(mean(times))
```

注意回退路径本轮也加了 `torch.cuda.synchronize()`（342 行）来排干 flush，与 CUPTI 路径保持一致的测量语义——虽然 events 路径本身有 launch overhead 膨胀，但至少 flush 语义对齐了。

#### 4.4.3 源码精读

L2 flush 缓冲的惰性分配：

[_get_l2_flush_cache — benchmarks/benchmark_base.py:146-162](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/benchmarks/benchmark_base.py#L146-L162)

> 用 `int32`（4 字节）分配 `l2_bytes // 4` 个元素，正好填满 L2。查询失败时退回 256 MB（155–160 行）。

回退决策与 `TILEOPS_ALLOW_CUDA_EVENTS_FALLBACK` 开关（本轮核心新增）：

[CUPTI 回退控制开关 — benchmarks/benchmark_base.py:312-330](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/benchmarks/benchmark_base.py#L312-L330)

> 314 行读取环境变量，默认 `"1"`。316–322 行：禁止时抛 `RuntimeError`，消息里明确说「这会阻止产生约 7 倍膨胀的不准确基准数据」，并提示如何调试（设回 `"1"` 看日志）。324–329 行：允许时 warning 量化膨胀（约 50–60 微秒 launch overhead，快 kernel 膨胀 6–7 倍）。330 行清空 `trial_means` 触发下面的 events 路径。

CUDA-events 回退计时（注意本轮新增的排干 flush 同步）：

[CUDA-events 回退计时路径 — benchmarks/benchmark_base.py:332-349](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/benchmarks/benchmark_base.py#L332-L349)

> 335 行注释「Mimic CUPTI behavior: flush L2 before measurement window」。342 行 `torch.cuda.synchronize()` 是本轮新增——确保 `cache.zero_()` 的 flush 在测量窗口打开前排干。events 路径依然先预分配所有 event 对象（337–338 行）再循环 record，避免循环内分配。

#### 4.4.4 代码实践

**实践目标**：理解 L2 flush 的作用、CUPTI 回退条件与诊断信息来源，以及如何用开关阻止回退。

**操作步骤**：

1. **为何要克隆输入**：阅读 4.2 的克隆池（227–259 行）。克隆让每次迭代的输入张量地址不同，打破驱动/硬件对「同一地址」的访存缓存惯量，配合 L2 flush，确保 kernel 每次都从冷状态（DRAM）取数。
2. **为何取 trial 中位数**：阅读 357–358 行。3 个独立 trial 抵抗偶发系统抖动，中位数比均值对离群值稳健（见 4.2.4 的练习 3）。
3. **`_sum_kernel_time_us` 为何只统计窗口内 kernel**：阅读 105–143 行与 4.3.3。窗口外的主要是 `cache.zero_()` flush，必须排除；被测 kernel 无论名字都计入。
4. **CUPTI 回退条件与诊断来源**：阅读 312–330 行。回退的触发条件是 `_sum_kernel_time_us` 返回的 `n_regions != n_repeat`（即标注窗口没在每个 repeat 上都投影成功）。诊断信息来源是两处：一是 `_logger.debug` 记录的 `n_regions` / `n_repeat` / `n_cuda_kernels` mismatch 详情（300–305 行），二是 `_CuptiProjectionError` 异常消息里携带的 kernel 计数（306–309 行）。要看到 debug 日志，需把 `tileops.bench` logger 调到 DEBUG 级别。
5. **阻止回退**：设想在 CI 中设 `TILEOPS_ALLOW_CUDA_EVENTS_FALLBACK=0` 跑基准。当某环境 CUPTI 不稳定时，基准会直接 `RuntimeError` 失败（316–322 行），而不是产出膨胀 6–7 倍的假 latency。这正是严肃基准想要的「宁可失败，不可撒谎」行为。

**需要观察的现象**：能区分两条路径——CUPTI 成功时 `_bench_meta.timing = "cupti"`（311 行），结果字典干净；回退时 `_bench_meta.timing = "cuda-events"`（334 行），结果字典多出 `timing` 键。

**预期结果**：能完整回答实践任务的四个子问题（克隆、中位数、窗口统计、回退条件与诊断）。**待本地验证**（CUPTI 回退行为依赖具体环境的 torch.profiler 稳定性）。

#### 4.4.5 小练习与答案

**练习 1**：为什么 L2 flush 用「一块填满 L2 的缓冲」而不是直接调某个「清缓存」API？

**答案**：因为 GPU 没有通用的、低开销的「清空整个 L2」API。标准做法是分配一块等于 L2 大小的缓冲，对它做写入（`zero_()`），用新数据把旧缓存内容**挤出** L2（cache 替换策略）。`_get_l2_flush_cache` 按设备真实 `L2_cache_size` 精确分配，保证正好覆盖整个 L2（151–162 行）。

**练习 2**：默认 `TILEOPS_ALLOW_CUDA_EVENTS_FALLBACK=1`，一个快 kernel（真实 5 微秒）走了 CUDA-events 回退，报告的 latency 大约是多少？这个数字能用来算 SOL 效率吗？

**答案**：CUDA-events 路径每次调用含约 50–60 微秒 launch overhead，所以报告的 latency 约 `5 + 55 ≈ 60` 微秒，膨胀约 12 倍（源码对快 kernel 的描述是「6–7 倍」，具体取决于 kernel 真实时长与 overhead 的比例）。这个数字**不能**用来算 SOL 效率——它严重高估了实际 kernel 时间，会让效率假性偏低。这也是为什么回退时结果字典会标注 `timing: "cuda-events"` 警示读者，以及为什么严肃场景应设 `=0` 直接失败。

**练习 3**：`_native_output_suppressor`（165–182 行）解决什么问题？为什么不是无条件启用？

**答案**：它抑制 tilelang 在 profiling 时的 stdout/stderr 噪音。但不能无条件启用：tilelang 的 `suppress_stdout_stderr` 用 `dup2` 把 `/dev/null` 覆盖到 `sys.stdout.fileno()`，而在 pytest 的 fd capture 下，那个 fileno 是 capture 临时文件，覆盖会损坏它（后续读出 `EBADF`）。所以它只在 `stdout/stderr` 确实是进程 fd 1/2 时才启用（174 行），否则返回 `nullcontext` 不做事。

---

## 5. 综合实践

**任务**：把本讲四个模块串起来，模拟一次完整的基准 profile 心智 walkthrough。

**操作步骤**：

1. 选一个已实现算子，比如 `RMSNormFwdOp`，参考 `benchmarks/ops/bench_norm.py` 的 `test_rms_norm_bench`（41–55 行）。
2. 假设调用 `bm.profile(op, *inputs)`，从 `BenchmarkBase.profile`（434–445 行）出发，逐步追踪一次 profile 的完整生命：
   - 进入 `torch.no_grad()` → 调 `bench_kernel(op, args=inputs)`（444 行）。
   - `bench_kernel` 取 L2 flush 缓冲（227 行）→ 构造 3 份克隆池（240–243 行，假设输入 < 1 GB）→ 10 次 warmup（262 行）。
   - 3 个 trial，每个 trial 在 CUPTI 下跑 50 repeat（277–290 行），每次先 flush 再 sync 再开窗口跑 op。
   - `_sum_kernel_time_us` 统计窗口内 kernel（105–143 行），校验 `n_regions == 50`（294 行）。
   - 假设 CUPTI 成功：`trial_means` 填入 3 个值，`_bench_meta.timing = "cupti"`（311 行），释放克隆池（353 行），返回中位数（358 行）。
   - 回到 `_build_result`（457–471 行）：写入 `latency_ms`；`timing == "cupti"` 所以**不**写 timing 键；调 `calculate_flops()`/`calculate_memory()`（来自 `ManifestBenchmark` 的 `eval_roofline`，见 u6-l2）算 TFLOPS 与带宽。
3. **故障演练**：假设这个环境 torch.profiler 不稳定，第 2 个 trial 投影出 `n_regions=40 != 50`。重走回退路径：
   - 抛 `_CuptiProjectionError`（306 行，消息含 `n_cuda_kernels`）。
   - 检查 `TILEOPS_ALLOW_CUDA_EVENTS_FALLBACK`（314 行）：
     - 若 `=0`（CI 严肃模式）：抛 `RuntimeError`，基准失败，不产出假数字。
     - 若 `=1`（默认）：warning 提示 6–7 倍膨胀（324 行），`trial_means` 清空，走 events 路径（332–349 行），`_bench_meta.timing = "cuda-events"`。
   - `_build_result` 检测到 `timing != "cupti"`，在结果字典写入 `timing: "cuda-events"`（460–462 行）警示读者。
4. 画出这张「正常路径 vs 回退路径」的对照流程图，标注每一步的行号。

**预期结果**：你能向同事完整讲清「一个 latency 数字从 GPU 跑出来到写进 `profile_run.log`，中间经历了哪些保证它纯净的工程机制（克隆、flush、窗口投影、中位数），以及它什么情况下会变成不可信（回退）」。**待本地验证**（实际数字与回退触发依赖真实 GPU 与 torch.profiler 状态）。

## 6. 本讲小结

- `BenchmarkBase[W]` 用泛型类型参数 `W`（能力协议，非 `WorkloadBase`）声明基准对 workload 的精确需求；子类实现 `calculate_flops` / `calculate_memory`，`_build_result` 把 latency 组装成含 TFLOPS/带宽的字典，并把任何「协议偏离」透明写进结果。
- `bench_kernel` 实现 NVIDIA SOL-ExecBench 风格协议：10 warmup + 3 trials × 50 repeats，返回 trial mean 的**中位数**（毫秒）。
- 输入克隆池预克隆 3 份轮转使用，打破地址复用带来的缓存惯量；超过 1 GB 自动跳过克隆并标注 `inputs_cloned: False`。
- L2 flush 用一块按真实 L2 大小分配的缓冲 `zero_()`，把旧缓存挤出，强迫 kernel 从 DRAM 取数，测到真实 SOL 行为。
- CUPTI 经 `torch.profiler` 拿纯 kernel 时间（无 launch overhead）；用 `record_function` 标注窗口投影到设备时间线，`_sum_kernel_time_us` 只累加窗口内 kernel，排除 flush，且不依赖 kernel 名字（支持多 kernel 协作算子）。
- CUPTI 投影失败（`n_regions != n_repeat`）时回退到 CUDA-events，但该路径含约 50–60 微秒 launch overhead，快 kernel 膨胀约 6–7 倍、不可用于 SOL 效率；本轮新增 `TILEOPS_ALLOW_CUDA_EVENTS_FALLBACK` 开关（`=0` 时直接 `RuntimeError`，禁止产出假数字），并新增 kernel 计数诊断帮助定位投影失败原因。

## 7. 下一步学习建议

- **u6-l2（manifest 驱动基准）**：本讲的 `calculate_flops` / `calculate_memory` 到底从哪取数？答案在 `ManifestBenchmark` 与 `Op.eval_roofline()`——它从 manifest roofline 读 FLOP/字节，禁止基准本地硬编码公式。下一篇详讲 `workloads_to_params` / `workload_field_params` 如何把 manifest workloads 转成 pytest 参数。
- **u6-l3（报告与基线对比）**：`BenchmarkReport.record` / `dump` 如何按 tag 分组生成 markdown 表格，为何必须记录至少一条非 tileops 基线，以及为何 `record` 要传规范 Op 实例而非字符串别名。
- **u7-l1（SOL 模型与度量）**：本讲测出的「实际时间」如何与 manifest 给的「理论最短时间」结合算出 SOL 效率，bound type 如何由形状决定。
- **源码延伸阅读**：`scripts/ci/run_benchmarks.py`（u6-l4）展示了 CI 如何为每个基准文件起独立进程跑这套协议，遇到 hang/segfault 时如何用 py-spy dump 与合成 junit 条目保证不丢报告。
