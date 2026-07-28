# 性能剖析与基准测试

## 1. 本讲目标

写出一个能跑的 kernel 只是第一步；要判断它「快不快」「还有多少优化空间」，必须能**精确地测量它的延迟**。本讲解决一个问题：**如何用 TileLang 自带的 profiler 模块，对一个编译好的 kernel 做可信的 GPU 基准测试，并把延迟换算成 TFLOPS**。

学完本讲，你应当能够：

- 用 `kernel.get_profiler()` 拿到一个 `Profiler` 对象，并用它的 `do_bench()` 测延迟。
- 理解 `do_bench` 为什么要在每次运行前「冲刷 L2 cache」，以及它如何自动估算 warmup / repeat 次数。
- 区分三种计时后端 `event` / `cupti` / `cudagraph`，知道何时用哪种。
- 用 `TensorSupplyType` 控制喂给 kernel 的输入张量分布，理解它对正确性校验与计时的不同影响。
- 写一个遍历若干 `(M, N, K)` 的 GEMM benchmark 脚本，输出延迟与 TFLOPS 表格。

## 2. 前置知识

在进入源码前，先建立三个直觉。

**第一，GPU 计时为什么不能直接用 `time.time()`。** GPU 调用是「异步」的：你在 Python 里写下 `kernel(a, b)`，这条命令只是把一个 kernel 放进 GPU 的执行队列，CPU 立刻返回，**真正的计算可能还没开始**。所以必须用 `torch.cuda.synchronize()` 强制等 GPU 做完，或用专门的「CUDA Event」在 GPU 侧打时间戳。本讲的 `do_bench` 用的就是后者。

**第二，为什么要冲刷 L2 cache。** GPU 片上有 L2 cache。如果上一次 kernel 刚把数据搬进 L2，第二次再跑同一份数据就会命中 cache、读得飞快——但这不是「冷启动」的真实性能。为了得到稳定、可复现的测量，`do_bench` 在每次计时前都会写一块大 buffer 把 L2 「冲掉」（术语叫 cache flushing），强制每次都从显存重新读。

**第三，TFLOPS 是怎么算的。** 对一个 \( M \times K \) 乘 \( K \times N \) 的矩阵乘，总浮点运算量是

\[
\text{FLOPs} = 2 \cdot M \cdot N \cdot K
\]

（每个输出元素做 \(K\) 次乘加，乘和加各算一次，故乘 2）。若测得延迟为 \(t\) 毫秒，则吞吐为

\[
\text{TFLOPS} = \frac{2 \cdot M \cdot N \cdot K}{t \times 10^{-3}} \times 10^{-12} = \frac{2 \cdot M \cdot N \cdot K}{t} \times 10^{-9}
\]

这正是项目里现成的写法（见 4.4 节）。

> 本讲承接 u8-l1（自动调优 autotuner）：autotuner 内部正是调用 `profiler.do_bench(...)` 来给每个候选配置量延迟、再挑最优的。理解了本讲的 `do_bench`，你也就理解了 autotuner 的「评分函数」。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
|------|------|
| `tilelang/profiler/__init__.py` | 定义 `Profiler` 类：封装输入张量供给、正确性校验、以及对外暴露的 `do_bench()` 方法。 |
| `tilelang/profiler/bench.py` | 真正的计时引擎：`do_bench()` 顶层函数，含 L2 冲刷、warmup/repeat 自动估算、三种计时后端实现。 |
| `tilelang/jit/kernel.py` | `JITKernel.get_profiler()`：从编译产物构造 `Profiler` 的入口。 |
| `tilelang/utils/tensor.py` | `TensorSupplyType` 枚举与 `get_tensor_supply()`：决定用什么分布的随机张量喂给 kernel。 |
| `examples/gemm/example_gemm.py` | 最小调用范例：编译 GEMM → 取 profiler → `do_bench`。 |
| `examples/flash_decoding/example_mha_inference.py` | 项目内「延迟换算 TFLOPS」的标准范式。 |

阅读顺序建议：先看 `kernel.py` 的 `get_profiler`（怎么拿），再看 `profiler/__init__.py` 的 `Profiler.do_bench`（薄壳），最后钻进 `bench.py` 的 `do_bench`（真正干活的地方）。

## 4. 核心概念与源码讲解

本讲按四个最小模块拆分：**profiler 模块**、**do_bench 精确计时**、**计时后端 backend**、**tensor 供给**。

### 4.1 profiler 模块与 Profiler 类

#### 4.1.1 概念说明

`Profiler` 是一个把「输入张量供给 + 正确性校验 + 计时」三件事打包在一起的辅助类。它的定位是：你拿到一个编译好的、可被 torch 张量调用的 `JITKernel` 后，不用自己手写 `torch.randn` 造数据、不用自己写 `synchronize`、不用自己造 cache 冲刷 buffer——`Profiler` 全帮你做了。

它的输入是 `KernelParam` 列表（描述每个参数的 dtype 和 shape）和 `result_idx`（哪些参数是输出张量）。知道哪些是输出后，`Profiler` 在自动造输入时会**跳过输出张量**（输出由 kernel 自己写）。

#### 4.1.2 核心流程

```
JITKernel.get_profiler(tensor_supply_type)
        │
        ▼
Profiler(params, result_idx, supply_type).with_default_adapter(adapter)
        │  __post_init__: 把 supply_type 变成可调用的 supply = get_tensor_supply(...)
        ▼
对外提供:
  ├─ do_bench(...)      → 计时（薄壳，转调 bench.do_bench）
  ├─ assert_allclose()  → 跑参考实现比对数值正确性
  ├─ run_once()         → 单次运行
  └─ _get_inputs()      → 按 supply_type 自动造一组输入张量
```

`Profiler` 本身「可调用」（实现了 `__call__`），调用它就等于调用底层的 adapter，也就是真的跑一次 kernel。

#### 4.1.3 源码精读

`get_profiler` 是构造入口，它把编译产物的 `params`、`out_idx` 和你指定的供给类型打包成 `Profiler`，并把 kernel 的 adapter 挂上去：

[.tilelang/jit/kernel.py:450-464](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/jit/kernel.py#L450-L464) — `get_profiler` 用编译出的 `params`/`out_idx` 构造 `Profiler`，默认供给类型是 `TensorSupplyType.Auto`，再经 `with_default_adapter` 把可调用 adapter 装上。

`Profiler` 是一个 `@dataclass`，三个核心字段是参数列表、输出下标、供给类型：

[.tilelang/profiler/__init__.py:21-40](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/profiler/__init__.py#L21-L40) — `Profiler` 数据类定义；`__post_init__` 在构造完成后立刻把 `supply_type` 解析成具体的 `supply = get_tensor_supply(self.supply_type)`，这样后续每次造输入只需调 `self.supply(param)`。

造输入的逻辑在 `_get_inputs`，关键在于**跳过输出张量**：

[.tilelang/profiler/__init__.py:62-70](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/profiler/__init__.py#L62-L70) — `_get_inputs`：当 `with_output=False`（计时场景）时，凡是落在 `result_idx` 里的参数都不造（输出由 kernel 写）；其余参数按形状调 `self.supply(param)` 生成。

`Profiler` 之所以「可调用」，是因为它把 adapter 暴露成了 `func` 属性并实现了 `__call__`：

[.tilelang/profiler/__init__.py:283-289](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/profiler/__init__.py#L283-L289) — `func` 属性断言 adapter 已装好；`__call__` 直接转发到 `self.func`。因此 `profiler(a, b)` 等价于直接跑一次 kernel。

除了计时，`Profiler` 还顺带提供了正确性校验 `assert_allclose`——跑一遍参考实现、再跑一遍 kernel、用 `torch_assert_close` 比对（允许一定比例的元素不匹配）：

[.tilelang/profiler/__init__.py:104-162](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/profiler/__init__.py#L104-L162) — `assert_allclose`：先 `reference_program(*ins)`，再 `self.func(*ins)`，对齐后用 `torch_assert_close` 比较。注意它内部硬编码了 `torch.cuda.synchronize()`，所以这是 CUDA 专用路径。

#### 4.1.4 代码实践

**实践目标**：从编译产物取出 `Profiler`，跑一次 `assert_allclose` 和一次 `run_once`，确认 profiler 能正确驱动 kernel。

**操作步骤**（基于 `examples/gemm/example_gemm.py`，以下为示例代码）：

```python
# 示例代码：接 example_gemm.py 的 matmul
import torch, tilelang

kernel = matmul.compile(M=512, N=512, K=512, block_M=128, block_N=128, block_K=32)
profiler = kernel.get_profiler()                  # 默认 TensorSupplyType.Auto

# 1) 正确性校验：profiler 自动造输入，与 torch 参考实现比对
profiler.assert_allclose(lambda a, b: a @ b, rtol=1e-2, atol=1e-2)

# 2) 跑一次，看输出形状
out = profiler.run_once()
print(type(out), out.shape if isinstance(out, torch.Tensor) else None)
```

**需要观察的现象**：`assert_allclose` 不抛异常即说明数值正确；`run_once` 返回的应是 `(512, 512)` 的输出张量。

**预期结果**：校验通过、输出形状正确。**待本地验证**（需 CUDA 或 MACA 设备）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `Profiler` 在自动造输入时要跳过 `result_idx` 里的参数？
**答案**：`result_idx` 标记的是输出张量，其值由 kernel 写入；如果也给它造随机输入，既无意义又会与 kernel 的输出混淆。计时/运行时只需要喂「输入」。

**练习 2**：`get_profiler()` 的参数 `tensor_supply_type` 默认是什么？若不传会怎样？
**答案**：默认 `TensorSupplyType.Auto`（见 `get_profiler` 签名）。不传时 profiler 会用 Auto 规则按 dtype 自动挑合适的分布（详见 4.4 节）。

---

### 4.2 do_bench 精确计时核心

#### 4.2.1 概念说明

`Profiler.do_bench()` 只是一个薄壳，真正干活的是 `tilelang/profiler/bench.py` 里的顶层函数 `do_bench(fn, ...)`。这个函数的设计目标是：**给任意一个可调用对象 `fn`（通常是「跑一次 kernel」），返回一个可信的、以毫秒为单位的平均延迟**。

它解决三个可信度问题：
1. **异步性**：用 CUDA Event 在 GPU 侧打时间戳，并 `synchronize`。
2. **cache 污染**：每次计时前冲刷 L2。
3. **测量噪声**：先估算 kernel 大概多慢，再据此自动决定 warmup 和重复多少次，避免「太快的 kernel 只跑 1 次」或「太慢的 kernel 跑几万次」。

#### 4.2.2 核心流程

`do_bench` 的主流程在 `_do_bench_impl` 中，分五步：

```
1. fn() + synchronize                 # 首次运行，触发懒编译/内核加载
2. 分配 cache 冲刷 buffer (cache_size MB)  # 默认 256MB，int32
3. 估算：跑 5 次，记录 cache.zero_()+fn() 的耗时 → estimate_ms
4. 算 warmup / repeat 次数：
     n_warmup  = max(1, int(warmup / estimate_ms))   # warmup 默认 25ms
     n_repeat  = max(1, int(rep    / estimate_ms))   # rep    默认 100ms
5. 按 backend 选计时实现:
     "event"     → _bench_with_cuda_events
     "cupti"     → _bench_with_cupti
     "cudagraph" → _bench_with_cudagraph
```

warmup 与 repeat 的「自动换算」是关键设计：你传的不是「跑多少次」，而是「热身总时长约多少毫秒」「计时总时长约多少毫秒」，函数按估算的单次耗时折算成次数。当然你也可以用 `_n_warmup` / `_n_repeat` 强制指定次数（注意带下划线，0 表示自动）。

#### 4.2.3 源码精读

`do_bench` 的公开签名与文档（参数含义都在这里）：

[.tilelang/profiler/bench.py:65-103](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/profiler/bench.py#L65-L103) — `do_bench` 顶层函数：`warmup=25`、`rep=100`（均为毫秒目标），`backend` 默认 `"event"`，`return_mode` 默认 `"mean"`，`cache_size` 默认 256MB。

`do_bench` 主体只做一件事——把 `device` 归一化成一个 CUDA 设备下标，然后在对应设备上下文里调 `_do_bench_impl`：

[.tilelang/profiler/bench.py:104-135](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/profiler/bench.py#L104-L135) — `do_bench` 先断言 `return_mode` 合法，再经 `_normalize_cuda_device` 把 `device` 转成下标（`None` 表示沿用当前设备），随后进入 `_do_bench_impl`。

L2 冲刷 buffer 的创建——注意 `fast_flush=True` 时用 int32（每元素 4 字节），否则用 int8：

[.tilelang/profiler/bench.py:185-188](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/profiler/bench.py#L185-L188) — 按 `cache_size`（MB）分配冲刷 buffer：`fast_flush` 时元素数 = 字节数/4、dtype 为 int32，否则全用 int8。这块 buffer 后续用 `cache.zero_()` 写零来「撑爆」L2。

单次耗时的估算（5 次平均）：

[.tilelang/profiler/bench.py:191-200](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/profiler/bench.py#L191-L200) — 用一对 CUDA Event 包住「5 次 `cache.zero_()` + `fn()`」，取平均得到 `estimate_ms`。注意估算里**包含了冲刷时间**，所以它是对「单轮总开销」的估计，用于换算次数。

warmup / repeat 的自动换算：

[.tilelang/profiler/bench.py:202-208](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/profiler/bench.py#L202-L208) — 当未手动指定（`_n_warmup`/`_n_repeat` 为 0）时，`n_warmup = max(1, int(warmup/estimate_ms))`、`n_repeat = max(1, int(rep/estimate_ms))`，再执行 `n_warmup` 次热身。

最后按 backend 分派（4.3 节详述三条分支）：

[.tilelang/profiler/bench.py:211-218](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/profiler/bench.py#L211-L218) — `backend == "event"` 走 CUDA Event；`"cupti"` 走 `torch.profiler`；`"cudagraph"` 走图回放；其余抛 `ValueError`。

#### 4.2.4 代码实践

**实践目标**：亲手调用 `bench.do_bench`，对比「手动指定次数」与「自动估算次数」两种用法，体会 warmup/rep 是「时长目标」而非「次数」。

**操作步骤**（示例代码）：

```python
# 示例代码
import torch, tilelang
from tilelang.profiler import do_bench

a = torch.randn(1024, 1024).cuda().half()
b = torch.randn(1024, 1024).cuda().half()
fn = lambda: a @ b

print("auto   :", do_bench(fn))                       # 自动估算次数
print("manual :", do_bench(fn, _n_warmup=10, _n_repeat=50))  # 强制次数
print("median :", do_bench(fn, return_mode="median"))
print("quantile:", do_bench(fn, quantiles=[0.5, 0.95]))
```

**需要观察的现象**：自动模式返回一个毫秒数；`quantiles` 模式返回一个长度 2 的列表（50% 分位与 95% 分位）。

**预期结果**：得到合理的毫秒级延迟；95% 分位 ≥ 50% 分位。**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：`warmup=25`、`rep=100` 的单位是什么？若 `estimate_ms=0.5`，自动模式下 `n_warmup` 与 `n_repeat` 各是多少？
**答案**：单位是毫秒。`n_warmup = max(1, int(25/0.5)) = 50`，`n_repeat = max(1, int(100/0.5)) = 200`。

**练习 2**：为什么估算 `estimate_ms` 时要把 `cache.zero_()` 也算进 5 次循环里？
**答案**：真实计时阶段每一轮也会先 `cache.zero_()` 再 `fn()`，估算应反映「单轮总开销」才能正确换算次数；否则会低估、导致重复次数过多。

**练习 3**：`return_mode` 有哪四种取值，默认是哪种？
**答案**：`min`/`max`/`mean`/`median`，默认 `mean`（对 `times` 张量调 `getattr(torch, return_mode)`）。

---

### 4.3 三种计时后端 backend

#### 4.3.1 概念说明

`do_bench` 提供三种 backend，对应三种「给 GPU kernel 计时」的思路，精度与开销各有取舍：

| backend | 思路 | 开销 | 特点 |
|---------|------|------|------|
| `event`（默认） | 每次迭代前后各打一个 CUDA Event | 低 | 简单稳定，最常用 |
| `cupti` | 用 `torch.profiler`（底层 CUPTI）采集 kernel 设备耗时 | 较高 | 能精确剔除冲刷自身的耗时，按 kernel 名统计 |
| `cudagraph` | 把多轮 `fn()` 录成一张 CUDA Graph，整体回放计时 | 极低 | 消除 host launch 开销，适合极短 kernel |

`event` 和 `cudagraph` 都支持 `quantiles` 与 `return_mode`；`cupti` 返回单一均值。

#### 4.3.2 核心流程

**event**：为 `n_repeat` 轮各创建一对 start/end Event；每轮先 `cache.zero_()` 冲刷，再 record start、跑 `fn()`、record end；最后 synchronize，把每轮 `s.elapsed_time(e)` 聚合成结果。

**cupti**：用 `torch.profiler.schedule(wait=1, warmup=0, active=1, repeat=1)` 跑两轮、每轮 `n_repeat` 次；遍历 profiler 事件，累加所有 CUDA kernel 的 `self_device_time_total`，但**剔除**被标注为 cache flush（`tilelang::cache_flush`）的那段时间，最后除以 `n_repeat` 得到单次纯 kernel 时间。

**cudagraph**：在一个旁路 stream 上，把 `n_repeat` 次 `fn()` 录进一张 `CUDAGraph`；然后回放 `n_retries=10` 次，每次回放前 `cache.zero_()` 冲刷，用 Event 测整张图耗时再除以 `n_repeat`。因为图内不能插冲刷，冲刷在图外做。

#### 4.3.3 源码精读

`_bench_with_cuda_events`——逐轮 Event 计时，这是最直观的实现：

[.tilelang/profiler/bench.py:221-254](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/profiler/bench.py#L221-L254) — 创建 `n_repeat` 对 Event；每轮 `cache.zero_()` 冲刷后 record start → `fn()` → record end；synchronize 后取每轮 `s.elapsed_time(e)` 组成 `times` 张量，按 `quantiles` 或 `return_mode` 聚合返回。

`_bench_with_cupti`——用 torch profiler 并剔除冲刷耗时：

[.tilelang/profiler/bench.py:257-299](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/profiler/bench.py#L257-L299) — 用 `record_function(_CACHE_FLUSH_ID)` 给 `cache.zero_()` 打标注，跑两轮各 `n_repeat` 次；统计时把所有 CUDA 事件的设备耗时求和为 `total_cuda_time`，再把标注为 `tilelang::cache_flush` 的时间求和为 `excluded_time`，纯 kernel 时间 = `(total - excluded)/n_repeat`，单位换算成毫秒返回。`_CACHE_FLUSH_ID` 定义在 [bench.py:62](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/profiler/bench.py#L62)。

`_bench_with_cudagraph`——图回放计时，注释里写明参考了 `triton.testing.do_bench_cudagraph`：

[.tilelang/profiler/bench.py:302-352](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/profiler/bench.py#L302-L352) — 在旁路 stream 上 `torch.cuda.graph(g)` 录入 `n_repeat` 次 `fn()`；随后回放 `n_retries=10` 次，每次回放前 `cache.zero_()` 冲刷（冲刷在图外，因为图要求固定执行模式）；用 Event 测整图耗时除以 `n_repeat` 得单次，再按 `return_mode`/`quantiles` 聚合。

项目里 `example_gemm.py` 默认就用 `cupti` 测 GEMM：

[.examples/gemm/example_gemm.py:54-57](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/examples/gemm/example_gemm.py#L54-L57) — `kernel.get_profiler()` 后 `profiler.do_bench(backend="cupti")` 取延迟并打印。同文件 `run_regression_perf` 也用 `cupti`（[L60-63](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/examples/gemm/example_gemm.py#L60-L63)），供 CI 回归比对性能。

#### 4.3.4 代码实践

**实践目标**：对同一个 GEMM kernel，分别用三种 backend 测延迟，体会它们的开销与稳定性差异。

**操作步骤**（示例代码）：

```python
# 示例代码
kernel = matmul.compile(M=1024, N=1024, K=1024, block_M=128, block_N=128, block_K=32)
profiler = kernel.get_profiler()

for backend in ["event", "cupti", "cudagraph"]:
    lat = profiler.do_bench(backend=backend)
    print(f"{backend:11s}: {lat:.4f} ms")
```

**需要观察的现象**：三者数值应接近；`cudagraph` 通常最稳定（host 开销被抹掉）；`cupti` 因启动 profiler 自身有开销可能略慢。

**预期结果**：三条延迟在同一量级。**待本地验证**（注意 `cudagraph` 对含动态控制流的 kernel 可能录制失败）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `_bench_with_cudagraph` 把 `cache.zero_()` 放在「图外」、而不是录进图里？
**答案**：CUDA Graph 要求固定的执行模式与地址；冲刷是为了「破坏」cache 状态，属于每次都要执行的副作用。把冲刷录进图反而会让图内首末状态不一致，且图回放追求极低开销，不宜夹带额外写。所以图内只放 `fn()`，冲刷在每次回放前单独做。

**练习 2**：`cupti` backend 是如何避免把「冲刷 buffer 自身的耗时」算进 kernel 延迟的？
**答案**：用 `torch.profiler.record_function("tilelang::cache_flush")` 给 `cache.zero_()` 打标注；统计时把 CUDA 事件总耗时减去该标注区间的耗时，得到纯 kernel 时间。

**练习 3**：三种 backend 里，哪一种**不支持** `quantiles` 与 `return_mode`？
**答案**：`cupti`。它直接返回 `(total-excluded)/n_repeat` 的单一均值，不生成 `times` 列表，故不做分位/聚合。

---

### 4.4 tensor 供给 TensorSupplyType

#### 4.4.1 概念说明

`Profiler` 在计时/校验时需要「造输入张量」。造什么样的张量？全零？全一？随机整数？正态分布？这由 `TensorSupplyType` 决定。它影响两件事：

- **正确性校验**：全零输入会让很多 bug 被掩盖（比如符号错误在 0 上看不出来），所以校验时宜用有区分度的分布（如 Normal/Randn）。
- **计时稳定性**：不同分布对 cache 命中、稀疏性、甚至某些硬件路径可能有细微影响；但对于 GEMM 这类计算密集算子，影响通常很小。

`Auto` 是默认值，它根据 dtype 智能挑选：浮点用 `uniform(-1,1)`，整数/bool/浮点8 用小范围整数，避免溢出。

#### 4.4.2 核心流程

```
TensorSupplyType (枚举: Integer/Uniform/Normal/Randn/Zero/One/Auto)
        │
        ▼
get_tensor_supply(supply_type) → 返回闭包 get_tensor(param)
        │  对每个 KernelParam:
        │    1. param.torch_dtype() 拿到 torch.dtype
        │    2. get_current_device() 拿到设备
        │    3. 校验 shape 无符号变量(否则抛错)
        │    4. 按 supply_type 选 torch 构造函数
        ▼
返回一个放在正确设备上的 torch.Tensor
```

注意：若参数的 shape 含符号变量（`tirx.Var`，即动态 shape），`get_tensor` 会直接抛错——动态 shape 的计时需要走 `Profiler.do_bench` 的 `dynamic_symbolic_constraints` 参数先把符号换成具体值。

#### 4.4.3 源码精读

`TensorSupplyType` 枚举定义：

[.tilelang/utils/tensor.py:32-39](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/utils/tensor.py#L32-L39) — 七种供给类型：`Integer`/`Uniform`/`Normal`/`Randn`/`Zero`/`One`/`Auto`。

`get_tensor_supply` 返回的闭包，重点看 `Auto` 分支如何按 dtype 分流：

[.tilelang/utils/tensor.py:42-82](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/utils/tensor.py#L42-L82) — `get_tensor` 先校验 shape 必须是静态的（含 `tirx.Var` 则抛 `ValueError`）；`Auto` 分支里：无符号整数 → `randint(0,3)`，float8 → `randint(-128,128)` 转 dtype，bool → `randint(0,2)`，float16/32/bf16 → `uniform(-1,1)`，其余 → `randint(-2,3)`。动态 shape 的合法用法见 `_substitute_dynamic_symbols`（[profiler/__init__.py:72-95](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/profiler/__init__.py#L72-L95)）。

`Profiler.do_bench` 如何把 `dynamic_symbolic_constraints` 透传给输入构造：

[.tilelang/profiler/__init__.py:253-276](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/profiler/__init__.py#L253-L276) — `run_bench` 内：若有 `input_tensors` 直接用；否则若给了 `dynamic_symbolic_constraints`，调 `_get_inputs(dynamic_symbolic_constraints=...)` 把符号换成具体值再造张量；最后 `partial(bench_target, *ins)` 固定输入，转调 `bench.do_bench`。

项目内「延迟换算 TFLOPS」的标准范式（FlashDecoding 例子）：

[.examples/flash_decoding/example_mha_inference.py:240-257](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/examples/flash_decoding/example_mha_inference.py#L240-L257) — 先算 `total_flops`，取 `profiler = kernel.get_profiler(tensor_supply_type=TensorSupplyType.Normal)`（注意这里特意用 Normal 而非默认 Auto），`latency = profiler.do_bench(...)` 后 `total_flops / latency * 1e-9` 得 TFLOPS。本讲综合实践沿用这一范式。

#### 4.4.4 代码实践

**实践目标**：对比不同 `TensorSupplyType` 对 GEMM 计时与正确性校验的影响。

**操作步骤**（示例代码）：

```python
# 示例代码
import tilelang

kernel = matmul.compile(M=1024, N=1024, K=1024, block_M=128, block_N=128, block_K=32)

for st in [tilelang.TensorSupplyType.Auto,
           tilelang.TensorSupplyType.Normal,
           tilelang.TensorSupplyType.Randn,
           tilelang.TensorSupplyType.Zero]:
    profiler = kernel.get_profiler(tensor_supply_type=st)
    lat = profiler.do_bench(backend="cupti")
    print(f"{st.name:8s}: {lat:.4f} ms")
```

**需要观察的现象**：`Zero`（全零输入）下延迟可能略低（全零数据在某些路径上更友好）；`Normal`/`Randn` 数值相近。**注意**：用全零输入做 `assert_allclose` 会掩盖符号类 bug，校验时应避免用 `Zero`。

**预期结果**：GEMM 是计算密集型，各分布延迟差异通常很小（个位数百分比内）。**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：`Auto` 对 `float16` 参数会生成什么样的张量？
**答案**：`torch.empty(*shape, ...).uniform_(-1.0, 1.0)`，即 `[-1,1]` 均匀分布的 float16 张量（见 `Auto` 分支里 `dtype in {float16, float32, bfloat16}` 的处理）。

**练习 2**：若 kernel 用了动态 shape（如 `T.dyn`），直接 `get_profiler().do_bench()` 会发生什么？应如何修正？
**答案**：`get_tensor` 遇到 shape 里的 `tirx.Var` 会抛 `ValueError`。修正方法：调用 `profiler.do_bench(dynamic_symbolic_constraints={"m": 2048, "n": 1024})`，由 `_substitute_dynamic_symbols` 把符号换成具体值再造张量。

**练习 3**：为什么 `assert_allclose` 校验时不建议用 `TensorSupplyType.Zero`？
**答案**：全零输入下，乘法恒为零、很多累加器错误与符号错误都看不出来，校验失去区分力。应选 `Normal`/`Randn` 等有区分度的分布。

---

## 5. 综合实践

把本讲四个模块串起来：**写一个 GEMM benchmark 脚本，遍历若干 `(M, N, K)`，测延迟并换算 TFLOPS，输出一张表格**。这个任务综合用到 `get_profiler`、`do_bench`、`backend` 选项与 TFLOPS 换算。

以下是示例代码（基于 `examples/gemm/example_gemm.py` 的 `matmul` kernel，本仓库无此脚本，需自行创建）：

```python
# 示例代码：gemm_benchmark.py（自行新建，非项目原有文件）
import tilelang
import tilelang.language as T

# 复用 example_gemm.py 的 kernel 定义
@tilelang.jit
def matmul(A, B, block_M, block_N, block_K, dtype=T.float16, accum_dtype=T.float32):
    M, N, K = T.const("M, N, K")
    A: T.Tensor((M, K), dtype)
    B: T.Tensor((K, N), dtype)
    C = T.empty((M, N), dtype)
    with T.Kernel(T.ceildiv(N, block_N), T.ceildiv(M, block_M), threads=128) as (bx, by):
        A_shared = T.alloc_shared((block_M, block_K), dtype)
        B_shared = T.alloc_shared((block_K, block_N), dtype)
        C_local = T.alloc_fragment((block_M, block_N), accum_dtype)
        T.clear(C_local)
        for k in T.Pipelined(T.ceildiv(K, block_K), num_stages=3):
            T.copy(A[by * block_M, k * block_K], A_shared)
            T.copy(B[k * block_K, bx * block_N], B_shared)
            T.gemm(A_shared, B_shared, C_local)
        T.copy(C_local, C[by * block_M, bx * block_N])
    return C


def bench(shapes, block=(128, 128, 32), backend="cupti"):
    bM, bN, bK = block
    # 表头
    print(f"{'M':>6} {'N':>6} {'K':>6} {'latency(ms)':>12} {'TFLOPS':>10}")
    for M, N, K in shapes:
        kernel = matmul.compile(M=M, N=N, K=K, block_M=bM, block_N=bN, block_K=bK)
        profiler = kernel.get_profiler()              # 默认 Auto 供给
        lat = profiler.do_bench(backend=backend)      # 毫秒
        flops = 2 * M * N * K                          # GEMM 浮点运算量
        tflops = flops / lat * 1e-9                    # ms → TFLOPS
        print(f"{M:>6} {N:>6} {K:>6} {lat:>12.4f} {tflops:>10.2f}")


if __name__ == "__main__":
    bench([
        (512, 512, 512),
        (1024, 1024, 1024),
        (2048, 2048, 2048),
        (4096, 4096, 4096),
    ])
```

**操作步骤**：
1. 把上面的 `matmul` 与 `bench` 放进一个新文件 `gemm_benchmark.py`（可与 `examples/gemm/example_gemm.py` 同目录便于 import）。
2. 运行 `python gemm_benchmark.py`。
3. 把 `backend` 改成 `"event"` 与 `"cudagraph"`，对比三种 backend 的延迟差异。
4. （进阶）把 `block_K` 从 32 改成 64，观察 TFLOPS 变化——这与 u6-l3 的调参维度呼应。

**需要观察的现象**：随 `(M,N,K)` 增大，TFLOPS 通常先升后趋于饱和（算力被吃满）；不同 backend 数值接近；改 `block_K` 会影响占用的 shared memory 与流水线深度。

**预期结果**：得到一张 4 行的延迟/TFLOPS 表格。具体数值取决于你的 GPU 型号与 target（CUDA/MACA），**待本地验证**。若在 MACA 上运行，需先按 u1-l2/u3-l3 配好 `MACA_PATH` 等环境变量，并确认 `import tilelang` 后 MACA 后端可用。

## 6. 本讲小结

- `JITKernel.get_profiler()` 返回一个 `Profiler`，它把「造输入 + 校验 + 计时」打包；`Profiler` 可直接调用（等价于跑一次 kernel）。
- 真正的计时引擎是 `bench.do_bench`：它先用一块 `cache_size`（默认 256MB）buffer 冲刷 L2，再用 5 次估算把「时长目标」（`warmup`/`rep` 毫秒）换算成「次数」，保证快慢 kernel 都测得稳。
- 三种 backend：`event`（逐轮 CUDA Event，默认）、`cupti`（torch.profiler，能剔除冲刷自身耗时，返回单一均值）、`cudagraph`（图回放，消除 host 开销，适合极短 kernel）。`cupti` 不支持 `quantiles`/`return_mode`。
- `TensorSupplyType` 控制输入张量分布：默认 `Auto` 按 dtype 智能挑选；校验用 Normal/Randn 更有区分力；动态 shape 须用 `dynamic_symbolic_constraints` 先把符号换具体值。
- TFLOPS 换算范式：`2*M*N*K / latency_ms * 1e-9`，与项目内 FlashDecoding 例子一致。
- autotuner（u8-l1）的「评分函数」就是 `profiler.do_bench(...)`——本讲是 autotuner 的测量基础。

## 7. 下一步学习建议

- **回到 autotuner 深挖**：带着对 `do_bench` 的理解重读 u8-l1，看 `tilelang/autotuner/tuner.py` 如何用 `profiler.do_bench(n_warmup=..., n_repeat=..., backend=...)` 给每个候选配置打分、并对比参考实现延迟（`ref_latency`）。
- **关注 HIP 资源剖析**：`JITKernel` 暴露了 `n_regs`/`n_spills`/`n_max_threads` 等资源占用属性（基于 HIP recorder），结合延迟可诊断「是不是寄存器压力/ spills 拖慢了 kernel」，详见 `tilelang/jit/kernel.py` 的 `resource_usage` 相关属性。
- **扩展到真实算子**：用本讲的 benchmark 范式去测 u8-l4 的 FlashAttention / elementwise / layer norm，把「延迟 + TFLOPS」作为优化的北极星指标。
- **代码阅读路线**：`bench.py` 的 `_bench_with_*` 三函数 → `profiler/__init__.py` 的 `Profiler.do_bench` 薄壳 → `kernel.py` 的 `get_profiler` → `autotuner/tuner.py` 的调用点，构成一条完整的「测量」调用链。
