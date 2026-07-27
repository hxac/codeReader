# Profiler 与基准测试

## 1. 本讲目标

写出一个能跑、且结果正确的 kernel 只是第一步；真正决定它能不能上生产的是「在目标硬件上到底快不快、稳不稳」。本讲聚焦 tilelang 的**测量工具链**，学完后你应当能够：

- 用 `kernel.get_profiler().do_bench()` 测出一次 kernel 调用的真实 GPU 延迟（毫秒），并理解 `warmup` / `rep` 的自动推断原理。
- 读懂 `do_bench` 三种计时后端（`event` / `cupti` / `cudagraph`）的差异，知道什么场景选哪个。
- 用 `TensorSupplyType` 控制 profiler 自动构造输入张量的方式（随机整数、正态、均匀、全零……），并明白为什么不同算子要选不同供给。
- 用 `Profiler.assert_allclose` / `assert_consistent` 在测延迟前先保证正确性。
- 仿照 `examples/flash_attention` 与 `examples/deepseek_mla` 的写法，给出一份「延迟 + 吞吐（TFlops）+ 参考实现对照」的完整性能报告。

本讲承接 u8-l1（Autotuner）。Autotuner 的「测量」环节内部正是调用的这套 `do_bench`；理解了本讲，你也就理解了 autotuner 评分数字是怎么来的。

## 2. 前置知识

- **GPU 计时的两种噪声**：① **kernel launch 开销**——从 CPU 发起调用到 GPU 真正开始执行之间有微秒级空隙，单次测量会被它严重高估；② **L2 cache 残留**——前一次运行把数据缓存在 L2 里，后一次运行「作弊」般地命中缓存，导致测出来偏快且不稳定。`do_bench` 的全部设计都是围绕消除这两类噪声展开的。
- **CUDA Event 计时**：`torch.cuda.Event(enable_timing=True)` 在 GPU 流里打两个时间戳，`start.elapsed_time(end)` 返回它们之间的毫秒数。它测的是 GPU 侧真实执行区间，不受 CPU 调度抖动影响。
- **`JITKernel` 与 adapter**（u4-l2、u7-l1）：`@tilelang.jit` 编译后得到 `JITKernel`，它持有一个 `adapter`（可调用对象，吃 torch.Tensor、吐 torch.Tensor）。`get_profiler()` 就是把这个 adapter 包成一个 `Profiler`。
- **吞吐与延迟的换算**：若一次调用完成 `total_flops` 次浮点运算，耗时 `latency` 毫秒，则吞吐（TFlops）为：

  \[
  \text{TFlops} \;=\; \frac{\text{total\_flops}}{\text{latency}_{\text{ms}}} \times 10^{-9}
  \]

  分母是毫秒，分子除以毫秒得到「flops / ms = 10³ flops / s」，再除以 10¹²（Tera）即乘 10⁻⁹。这正是 examples 里 `total_flops / latency * 1e-9` 的来历。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [tilelang/profiler/bench.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/profiler/bench.py) | **基准测量引擎**。`do_bench()` 与三个后端实现（event/cupti/cudagraph）都在这里，是本讲最核心的文件。 |
| [tilelang/profiler/__init__.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/profiler/__init__.py) | **`Profiler` 类**。把「输入供给 + adapter + 正确性校验 + 调用 do_bench」编排成一个数据类。 |
| [tilelang/utils/tensor.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/utils/tensor.py) | **`TensorSupplyType` 枚举与 `get_tensor_supply`**。定义输入张量怎么自动生成，以及 `torch_assert_close` 容错比较。 |
| [tilelang/jit/kernel.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/jit/kernel.py) | `JITKernel.get_profiler()` 的定义点（约 L469），是把 kernel 接入 profiler 的入口。 |
| [examples/flash_attention/example_mha_fwd_bhsd.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/examples/flash_attention/example_mha_fwd_bhsd.py) | FlashAttention（BHSD）前向示例，演示 `get_profiler` + `assert_allclose` + `do_bench` + TFlops 打印的完整范式。 |
| [examples/deepseek_mla/example_mla_decode.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/examples/deepseek_mla/example_mla_decode.py) | DeepSeek MLA decode 示例，演示 `tensor_supply_type=Randn` 与 `backend="cupti"` 的用法。 |

公共 API 在 `tilelang/__init__.py` 中导出：`Profiler` 与 `TensorSupplyType`（见 [tilelang/__init__.py:L193-L198](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/__init__.py#L193-L198)），因此可直接写 `tilelang.Profiler`、`tilelang.TensorSupplyType`。

---

## 4. 核心概念与源码讲解

本讲按「从底层到上层」拆成四个最小模块：① `do_bench` 测量引擎（最底层、与 tilelang 无关的通用工具）；② `Profiler` 编排者；③ `TensorSupplyType` 输入供给；④ 实战算子对照。

### 4.1 do_bench：基准测量引擎

#### 4.1.1 概念说明

`do_bench(fn, ...)` 是一个**与 tilelang 无关的通用 PyTorch 计时函数**：给它任意一个零参可调用对象 `fn`，它返回 `fn` 单次执行的平均毫秒数。它的设计目标只有一个——在 GPU 上得到**干净、可复现**的 kernel 延迟。它解决三件事：

1. **消除 launch 开销**：先 warmup 让 GPU「热」起来、缓存/JIT 稳定，再正式计时。
2. **消除 L2 残留**：每次计时前写一块大 buffer 把 L2 cache「冲刷」干净，让每次测量都面对冷缓存（或至少一致的缓存状态）。
3. **自动推断迭代次数**：用户只给「目标 warmup 时长」和「目标总测量时长」（都是毫秒），函数先用 5 次试跑估出单次耗时，再反推需要跑多少轮。

#### 4.1.2 核心流程

`do_bench` 的整体节奏可以用下面这段伪代码概括：

```
do_bench(fn, warmup=25ms, rep=100ms):
    1. fn(); synchronize()                      # 首次调用，触发编译/分配
    2. 分配 L2 冲刷 buffer（cache_size MB）
    3. 试跑 5 次（每次先 cache.zero_() 再 fn()），
       用 CUDA event 估出 estimate_ms = 总时长 / 5
    4. [可选] 若 estimate_ms > early_stop_baseline → 提前返回
    5. 反推迭代数：
         n_warmup  = max(1, ⌊warmup / estimate_ms⌋)
         n_repeat  = max(1, ⌊rep    / estimate_ms⌋)
    6. 跑 n_warmup 次（不计入）
    7. 按选定 backend 正式计时 n_repeat 次，每次前 cache.zero_()
         - event     ：每轮 record start/end event，最后取聚合
         - cupti     ：用 torch.profiler，扣掉冲刷自身耗时
         - cudagraph ：把 n_repeat 次 fn 录进 CUDA Graph 回放
    8. 返回聚合结果（mean/median/min/max 或 quantiles）
```

两个关键公式（自动迭代推断）：

\[
n_{\text{warmup}} = \max\left(1,\;\left\lfloor \frac{t_{\text{warmup}}}{\hat t} \right\rfloor\right),\qquad
n_{\text{repeat}} = \max\left(1,\;\left\lfloor \frac{t_{\text{rep}}}{\hat t} \right\rfloor\right)
\]

其中 \(\hat t\) 是 5 次试跑得到的单次耗时估计。这样无论 kernel 是 10µs 还是 10ms，总测量时间都稳定在 ~`rep` 毫秒量级。

#### 4.1.3 源码精读

**公共入口** `do_bench` 主要做参数校验与设备作用域管理，真正的逻辑在 `_do_bench_impl`。见 [tilelang/profiler/bench.py:L68-L141](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/profiler/bench.py#L68-L141)：

- L108 校验 `return_mode ∈ {min,max,mean,median}`。
- L110 用 `_normalize_cuda_device` 把 `int / torch.device / None` 统一成 CUDA 设备索引（见 [tilelang/profiler/bench.py:L144-L156](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/profiler/bench.py#L144-L156)），传 `None` 表示沿用隐式当前设备。
- L111-L126 若指定了设备，用 `with torch.cuda.device(device_idx)` 把后续 event/stream/buffer 都绑定到该设备。

**主实现** `_do_bench_impl` 见 [tilelang/profiler/bench.py:L172-L236](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/profiler/bench.py#L172-L236)，对应伪代码的第 1-7 步：

- L187-L188：`fn()` 首次调用 + 同步，触发 lazy 编译与显存分配。
- L192-L195：分配 L2 冲刷缓冲。`fast_flush=True`（默认）用 `int32`（4 字节），所以元素数 `cache_bytes // 4`；否则用 `int8`。默认 `cache_size=256` MB。`cache` 张量本身只是用来被反复 `zero_()` 以填充 L2。
- L198-L207：5 次试跑估 `estimate_ms`。注意每轮先 `cache.zero_()` 再 `fn()`，确保估出来的也是冷缓存下的耗时。
- L210-L218：**提前终止**。若给了 `early_stop_baseline` 且估计值已超过基线，直接返回估计值，省掉完整计时——autotuner 海选粗排时很有用。
- L221-L222：上述两个公式落地。
- L224-L226：warmup 阶段（纯跑，不计时）。
- L228-L236：按 `backend` 分派到三个内部函数。

**后端一：CUDA Events**（默认）见 [tilelang/profiler/bench.py:L239-L272](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/profiler/bench.py#L239-L272)。为每一轮单独创建 `start/end` event，循环里「冲缓存 → record start → fn() → record end」，最后同步取 `s.elapsed_time(e)`，再用 `torch.mean/median/min/max`（由 `getattr(torch, return_mode)` 动态选）或 `torch.quantile` 聚合。优点是简单通用、支持分位数；缺点是每轮都吃一次 launch 开销。

**后端二：CUPTI** 见 [tilelang/profiler/bench.py:L275-L317](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/profiler/bench.py#L275-L317)。用 `torch.profiler.profile` 抓 CUDA 活动，关键细节在 L299-L316：因为 `cache.zero_()` 与用户代码里的 `torch.zeros` 可能复用同一个生成的 kernel 名，所以**只用 `record_function("tilelang::cache_flush")` 标注的范围来扣减冲刷耗时**（`_CACHE_FLUSH_ID`，定义在 [tilelang/profiler/bench.py:L65](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/profiler/bench.py#L65)），其余 CUDA 事件计入 kernel 时间。它是三者里最贴近「真实设备时间」的，但开销最大，且不支持 `quantiles`/`return_mode`（固定返回均值）。

**后端三：CUDA Graph** 见 [tilelang/profiler/bench.py:L320-L370](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/profiler/bench.py#L320-L370)。把 `n_repeat` 次 `fn()` 录进一张 `CUDAGraph`，再回放 `n_retries=10` 次取均值。注释（L334-L336）强调：**冲缓存只能在回放之前做**，因为图内的执行模式是固定的。Graph 回放几乎消除了 launch 开销，适合测极轻量 kernel；缺点是要求 `fn` 在捕获期间行为确定（输入输出地址固定）。

> 注：`do_bench` 的整体思路借鉴自 Triton 的 `triton.testing.do_bench`（cudagraph 分支注释明确说明 follows `triton.testing.do_bench_cudagraph`），suppress 工具类则来自 DeepGEMM（见 [tilelang/profiler/bench.py:L16-L60](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/profiler/bench.py#L16-L60) 注释）。

#### 4.1.4 代码实践

**目标**：体会 `warmup` / `rep` 自动推断与三种 backend 的数值差异。

**步骤**（需要一台有 CUDA GPU 的机器）：

1. 构造一个零参可调用对象，例如对固定大小的两个 tensor 做矩阵乘：

```python
# 示例代码：直接调用底层 do_bench（不依赖 tilelang kernel）
import torch, tilelang
from tilelang.profiler.bench import do_bench

a = torch.randn(2048, 2048, device="cuda", dtype=torch.float16)
b = torch.randn(2048, 2048, device="cuda", dtype=torch.float16)
fn = lambda: torch.matmul(a, b)

for backend in ["event", "cupti", "cudagraph"]:
    print(backend, do_bench(fn, backend=backend), "ms")

# 观察分位数与不同聚合
print("quantiles", do_bench(fn, quantiles=[0.5, 0.95, 0.99]))
print("min", do_bench(fn, return_mode="min"))
```

2. 故意把 `warmup` 调到 1、`rep` 调到 1，再对比默认值，观察结果波动。

**需要观察的现象**：三种 backend 给出的毫秒数应处于同一量级；`cudagraph` 通常最小（无 launch 开销），`event` 居中，`cupti` 因计入设备侧总时间可能略大。分位数会显示 95/99 分位比中位数更高，体现尾延迟。

**预期结果**：能稳定打印出三组延迟数；手动调小 `warmup/rep` 后数值方差变大。**若没有 GPU，此步标记为「待本地验证」。**

#### 4.1.5 小练习与答案

**练习 1**：为什么 `do_bench` 要在每次正式计时前调用 `cache.zero_()`？不调会怎样？
**答**：为了冲刷 L2 cache，让每轮测量面对一致的（冷的）缓存状态。不调的话，前一轮的输出数据可能驻留在 L2，下一轮命中缓存，测出来的延迟偏小且不可复现，掩盖真实性能。

**练习 2**：`estimate_ms` 是用 5 次试跑算出来的。已知 `warmup=25`、`rep=100`、`estimate_ms=0.5`，求 `n_warmup` 与 `n_repeat`。
**答**：`n_warmup = max(1, ⌊25/0.5⌋) = 50`；`n_repeat = max(1, ⌊100/0.5⌋) = 200`。

**练习 3**：`early_stop_baseline` 这个参数最适合用在哪个场景？
**答**：autotuner 海选大量候选配置时——对一眼就比当前最优慢很多的候选，用 5 次试跑的估计值直接返回，跳过完整 `do_bench`，省下绝大多数测量时间。

---

### 4.2 Profiler 类：输入供给、正确性与基准的编排者

#### 4.2.1 概念说明

`do_bench` 只接受「零参可调用对象」，但 tilelang kernel 的调用需要**真实输入张量**。`Profiler` 就是那层胶水：它持有 kernel 的参数描述（`params`、`result_idx`）、一个输入供给策略（`supply_type`）和一个 `adapter`，对外提供「自动造输入 → 跑正确性校验 → 跑延迟基准」的一站式接口。它本质上是一个 `@dataclass`，配置即对象。

#### 4.2.2 核心流程

```
Profiler(params, result_idx, supply_type, adapter)
   ├── __post_init__: 合法化 result_idx；构造 supply = get_tensor_supply(supply_type)
   ├── _get_inputs(): 遍历 params，对非输出参数调 supply(param) 造输入张量
   ├── assert_allclose(ref): 用同样输入跑 ref 与 adapter，逐输出 torch_assert_close
   ├── assert_consistent(repeat=10): 同输入跑多次，检查输出完全一致（查 race condition）
   ├── run_once(): 跑一次，返回输出
   └── do_bench(...): partial(adapter, *ins) 得到零参闭包，交给 bench.do_bench
```

`Profiler.__call__` 直接委托给 `adapter`，所以 `profiler(*inputs)` 等价于直接调用 kernel。

#### 4.2.3 源码精读

**数据类定义与字段**见 [tilelang/profiler/__init__.py:L21-L35](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/profiler/__init__.py#L21-L35)：四个字段——`params: list[KernelParam]`、`result_idx: list[int]`、`supply_type: TensorSupplyType`、`adapter: BaseKernelAdapter | None`。

**`__post_init__`** 见 [tilelang/profiler/__init__.py:L37-L40](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/profiler/__init__.py#L37-L40)：先把 `result_idx` 合法化（`_legalize_result_idx`，L42-L56，支持 `None/int/list` 三种输入，并处理负索引），再用 `get_tensor_supply` 造一个 `supply` 闭包（见 4.3 节）。

**`_get_inputs`** 见 [tilelang/profiler/__init__.py:L62-L70](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/profiler/__init__.py#L62-L70)：遍历 `params`，**跳过 `result_idx` 标记的输出位**（`with_output=False` 时），对剩余每个 `KernelParam` 调 `self.supply(param)` 生成张量。它还支持 `dynamic_symbolic_constraints`（L62、L72-L95）——当 kernel 含符号维度（`T.dynamic`）时，用约束字典把符号替换成具体整数再造张量。

**正确性校验 `assert_allclose`** 见 [tilelang/profiler/__init__.py:L104-L162](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/profiler/__init__.py#L104-L162)：用同一份输入分别跑 `reference_program` 与 `self.func`（即 adapter），把两边输出归一为 list，逐对调 `torch_assert_close`（允许一定比例元素超差，见 4.3.3）。L155-L156 还特别处理 fp8：比较前先升精度到 float32。这是「测延迟前先确认算对」的标准动作。

**一致性检查 `assert_consistent`** 见 [tilelang/profiler/__init__.py:L195-L212](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/profiler/__init__.py#L195-L212)：同一输入跑 `repeat` 次，要求每次输出 `torch.allclose`。注释明确写着「Used to check no race condition inside the kernel」——split-K、atomic_add 这类有并发写回的 kernel 必跑这个，否则会出现「偶发错误、平均延迟却很好」的隐蔽 bug。

**`do_bench` 方法**见 [tilelang/profiler/__init__.py:L220-L283](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/profiler/__init__.py#L220-L283)，核心是内部闭包 `run_bench`（L254-L278）：

- L255-L259：选择被测对象——`func` 参数优先，否则用 `self.adapter`。
- L260-L265：决定输入——显式 `input_tensors` > `dynamic_symbolic_constraints` 造的输入 > 默认 `_get_inputs()`。
- L266：`bench_func = partial(bench_target, *ins)`——把带输入的调用偏应用成**零参闭包**，正是 `do_bench` 要求的形式。
- L267-L278：转交 `bench.do_bench`，透传 `warmup/rep/quantiles/backend/return_mode/device/early_stop_baseline`。

注意 L256 的断言：若既没传 `func` 也没设 `adapter`，会报「benchmarking function should be provided」。这也意味着你可以把 `Profiler` 当成一个通用的、带输入供给的基准器——`profiler.do_bench(ref_program)` 同样工作，examples 里正是这么做的。

**`get_profiler` 入口**在 `JITKernel` 上，见 [tilelang/jit/kernel.py:L469-L483](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/jit/kernel.py#L469-L483)：

```python
return Profiler(self.params, self.out_idx, tensor_supply_type).with_default_adapter(self.adapter)
```

一行就把 kernel 的参数描述、输出索引、默认 adapter 注入 profiler。`with_default_adapter`（[L58-L60](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/profiler/__init__.py#L58-L60)）返回 `self`，方便链式调用。

#### 4.2.4 代码实践

**目标**：对一个 tilelang GEMM kernel，先校验正确性，再用两种 backend 测延迟并对比。

**步骤**（需要 CUDA GPU）：

```python
# 示例代码：基于 u1-l4 的 GEMM 写法
import torch, tilelang
import tilelang.language as T

@tilelang.jit(out_idx=[2])
def matmul(M=512, N=512, K=512):
    @T.prim_func
    def main(A: T.Tensor((M, K), "float16"),
             B: T.Tensor((K, N), "float16"),
             C: T.Tensor((M, N), "float16")):
        with T.Kernel(T.ceildiv(M, 128), T.ceildiv(N, 128), threads=128) as (bx, by):
            A_sh = T.alloc_shared((128, 128), "float16")
            B_sh = T.alloc_shared((128, 128), "float16")
            C_fr = T.alloc_fragment((128, 128), "float32")
            T.clear(C_fr)
            for k in T.Pipelined(T.ceildiv(K, 128), num_stages=3):
                T.copy(A[bx*128:(bx+1)*128, k*128:(k+1)*128], A_sh)
                T.copy(B[k*128:(k+1)*128, by*128:(by+1)*128], B_sh)
                T.gemm(A_sh, B_sh, C_fr)
            T.copy(C_fr, C[bx*128:(bx+1)*128, by*128:(by+1)*128])

    return main

kernel = matmul(512, 512, 512)
profiler = kernel.get_profiler()

# 参考实现
ref = lambda A, B: torch.matmul(A.float(), B.float()).half()
profiler.assert_allclose(ref, rtol=1e-2, atol=1e-2)   # 正确性
profiler.assert_consistent(repeat=5)                   # 一致性 / race

t_event = profiler.do_bench(warmup=500)                # 默认 event 后端
t_cupti = profiler.do_bench(backend="cupti")
print(f"event={t_event:.3f} ms  cupti={t_cupti:.3f} ms")
```

**需要观察的现象**：`assert_allclose` 通过即说明 kernel 正确；两种 backend 延迟同量级。把 `num_stages` 改成 1 再测一次，应能看到流水线带来的延迟下降。

**预期结果**：打印两组延迟，且 event ≤ cupti 大致成立。**无 GPU 时标记「待本地验证」。**

#### 4.2.5 小练习与答案

**练习 1**：`profiler.do_bench(ref_program)` 与 `profiler.do_bench()`（不传参）测的是同一个东西吗？
**答**：不是。前者把参考实现（通常用 PyTorch eager 写）当成被测对象，测的是「baseline 延迟」；后者用 `self.adapter`，测的是「tilelang kernel 延迟」。两者使用同一份 profiler 自动生成的输入，因而可以直接对比。

**练习 2**：为什么对含 `T.atomic_add` 写回的 split-K kernel，`assert_consistent` 比 `assert_allclose` 更值得跑？
**答**：split-K 让多个 block 竞争写同一输出地址，存在数据竞争（race）。`assert_allclose` 用的是某一次的随机输入，可能恰好「走运」通过；`assert_consistent` 反复跑同一输入，能放大偶发的竞争错误，更容易暴露问题。

---

### 4.3 TensorSupplyType：输入数据供给

#### 4.3.1 概念说明

`TensorSupplyType` 是一个枚举，决定 profiler 自动给每个输入张量填什么数据。它之所以重要，是因为**不同算子对输入分布的敏感度不同**：

- 整数/small-range 输入适合 GEMM 正确性测试（值小、不溢出、好对齐）。
- 正态/均匀分布更贴近真实推理数据，对 softmax、attention 这类对数值范围敏感的算子更稳。
- 全零/全一常用于排查「NaN 是否来自数据」。

#### 4.3.2 核心流程

`get_tensor_supply(supply_type)` 是个**工厂**：返回一个闭包 `get_tensor(param)`，它根据 `KernelParam` 的 dtype/shape/无符号性/低精度等属性，结合 `supply_type` 选分支生成 `torch.Tensor`。

```
get_tensor_supply(type) -> get_tensor(param) -> torch.Tensor
   按 type 分支：
     Auto     ：按 dtype 智能挑（见下）
     Integer  ：torch.randint(-2, 3) 为主，fp8/无符号/布尔各有特化
     Uniform  ：float32 上 uniform(-1,1) 再 .to(dtype)
     Normal   ：float32 上 normal(-1,1) 再 .to(dtype)
     Randn    ：torch.randn 再 .to(dtype)
     Zero/One ：全 0 / 全 1
```

#### 4.3.3 源码精读

**枚举定义**见 [tilelang/utils/tensor.py:L32-L39](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/utils/tensor.py#L32-L39)：共 7 个成员 `Integer / Uniform / Normal / Randn / Zero / One / Auto`。

**`get_tensor_supply` 工厂**见 [tilelang/utils/tensor.py:L42-L118](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/utils/tensor.py#L42-L118)。两个关键点：

1. **静态形状前置检查**（L51-L63）：若 `param.shape` 为空或含 `tirx.Var`（符号维度），直接抛错——因为没法为未知维度分配张量。这正是 `Profiler._get_inputs` 在符号维度场景需要 `dynamic_symbolic_constraints` 的原因。
2. **`Auto` 分支**（L66-L82）：按 dtype 智能选择。无符号整数走 `randint(0,3)`；**fp8** 走 `randint(-128,128, int8).to(fp8)`（避免直接 random 出 NaN/Inf，因为 fp8 表示范围极窄）；fp4 走 `randint(0,16)`；布尔走 `randint(0,2)`；普通 fp16/fp32/bf16 走 `uniform(-1,1)`。这一段解释了为什么 examples 大多用默认的 `Auto` 就够了。

**`Integer` 分支**（L90-L104）与 Auto 对整数/低精度类型的处理几乎一致；`Uniform/Normal/Randn`（L105-L110）则先在 float32 上生成再 `.to(dtype)`。注意 L84-L88 的特例：当 dtype 是 `int8` 但选了 `Uniform/Normal` 时，直接返回全 1——因为这两个分布产生的是浮点数，强转 int8 会全部截断为 0，没有意义。

**容错比较 `torch_assert_close`**（被 `assert_allclose` 调用）见 [tilelang/utils/tensor.py:L205-L293](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/utils/tensor.py#L205-L293)。它与 `torch.allclose` 的最大区别是允许「一定比例元素超差」（`max_mismatched_ratio`，默认 0.001）：先 `_compare_attributes` 对齐 shape/dtype/device，再 `_equalize_attributes` 提升精度（L163-L202），最后统计 `~torch.isclose` 的比例，超过阈值才报错，并在错误信息里给出首个失配位置（L266-L277）。这套容差机制对低精度（fp8/fp4）kernel 的正确性校验不可或缺。

#### 4.3.4 代码实践

**目标**：观察不同 `TensorSupplyType` 对一个 attention 类 kernel 正确性校验通过率的影响。

**步骤**（需要 CUDA GPU）：

1. 复用 4.2.4 的 GEMM kernel，把 `get_profiler()` 换成不同供给：

```python
for st in [tilelang.TensorSupplyType.Auto,
           tilelang.TensorSupplyType.Integer,
           tilelang.TensorSupplyType.Normal,
           tilelang.TensorSupplyType.Zero]:
    p = kernel.get_profiler(tensor_supply_type=st)
    try:
        p.assert_allclose(ref, rtol=1e-2, atol=1e-2)
        print(st, "OK", p.do_bench(warmup=200), "ms")
    except AssertionError as e:
        print(st, "FAIL", str(e)[:80])
```

2. 对比：`Zero` 供给下 GEMM 输出全 0，校验必然通过但测不出真实性能特征；`Integer` 与 `Normal` 都应通过。

**需要观察的现象**：`Zero` 虽然通过校验，但因为输入稀疏（全 0），延迟可能与真实负载不同（有时更慢，因为某些快速路径不被触发）。

**预期结果**：四种供给都应通过 GEMM 校验；延迟数值接近但不完全相同。**无 GPU 时标记「待本地验证」。**

#### 4.3.5 小练习与答案

**练习 1**：为什么 fp8 dtype 在 `Auto` 分支要用 `randint(-128, 128, int8).to(fp8)` 而不是直接 `torch.randn(...).to(fp8)`？
**答**：fp8 的有效范围极窄（e4m3 最大约 ±448，e5m2 更小），`randn` 产生的值大多会溢出到 Inf，甚至经注意力 softmax 后变成 NaN，无法用于正确性校验。用小范围整数生成的 fp8 值落在安全区间，保证校验有意义。

**练习 2**：`profiler._get_inputs()` 跳过了哪些参数？为什么？
**答**：跳过 `result_idx` 标记的输出张量。因为输出由 kernel 自己写入，profiler 不应为它造随机初值（否则可能掩盖「kernel 没真正写输出」的错误），且 `assert_allclose` 只比较输出、不比较输入侧的输出位。

---

### 4.4 实战对照：FlashAttention 与 MLA 的性能测量

#### 4.4.1 概念说明

tilelang 的 examples 不是「能跑的玩具」，而是**对标 FlashAttention / DeepSeek MLA 等一线实现的高性能 kernel**。它们提供了一个标准范式：用 `get_profiler` 同时完成「正确性对照参考实现 + 延迟测量 + 吞吐换算」。学完这个范式，你就能为自己的 kernel 写出一份可信的性能报告。

#### 4.4.2 核心流程

两个 example 的 `main()` 节奏完全一致：

```
1. 算 total_flops（用于换算 TFlops）
2. kernel = flashattn(...)              # @tilelang.jit 装饰，调用返回 JITKernel
3. profiler = kernel.get_profiler(...)  # 可指定 tensor_supply_type
4. profiler.assert_allclose(ref_program, rtol, atol)   # 正确性
5. ref_latency = profiler.do_bench(ref_program, ...)   # 参考实现延迟
6. latency     = profiler.do_bench(...)                # tilelang 延迟
7. 打印 latency 与 TFlops = total_flops / latency * 1e-9
```

`@autotune` 版本（`tune=True`）则把「do_bench 测量」交给 autotuner 内部循环，最后读取 `kernel.latency` / `kernel.config` / `kernel.ref_latency`。

#### 4.4.3 源码精读

**FlashAttention（BHSD）**见 [examples/flash_attention/example_mha_fwd_bhsd.py:L120-L156](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/examples/flash_attention/example_mha_fwd_bhsd.py#L120-L156)：

- L129-L132 算 FLOPS：`flops_per_matmul = 2*batch*heads*seq_q*seq_kv*dim`，总共两次 matmul（QK 与 softmax·V），故 `total_flops = 2 * flops_per_matmul`；causal 时上半三角被 mask，乘 0.5。
- L135 构造 kernel，L136 用 `partial(ref_program, is_causal=...)` 把参考函数的 `is_causal` 固定成零参偏函数——参考函数签名是 `ref_program(Q, K, V, is_causal)`，而 profiler 只会传三个输入张量。
- L138-L139 `get_profiler()` + `assert_allclose`。
- L141-L146 **对照打印**：先测参考实现（`profiler.do_bench(ref_program_processed, warmup=500)`）再测 tilelang（`profiler.do_bench(warmup=500)`），各自换算 TFlops。`warmup=500`（ms）比默认 25 大得多，因为 attention 单次耗时较长，需要更多 warmup 让 GPU 频率稳定。
- L158-L169 的 `run_regression_perf` 用 `backend="cupti"` 给回归测试一个稳定的性能数字——cupti 设备侧计时更不易受 CPU 抖动影响。

**DeepSeek MLA decode**见 [examples/deepseek_mla/example_mla_decode.py:L218-L239](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/examples/deepseek_mla/example_mla_decode.py#L218-L239)：

- L226-L228 算 FLOPS：QK 段 `2*batch*heads*kv_ctx*(dim+pe_dim)`（MLA 把 RoPE 部分 `pe_dim` 与上下文部分 `dim` 拼起来算注意力），PV 段 `2*batch*heads*kv_ctx*dim`。
- L235 `get_profiler(tensor_supply_type=tilelang.TensorSupplyType.Randn)`——MLA 用 `Randn` 而非默认 `Auto`，因为 MLA 对输入数值分布更敏感，`Randn` 更贴近真实推理分布，校验更稳。
- L236 `assert_allclose(ref_program, rtol=1e-4, atol=1e-4)`——容差比 FlashAttention 的 1e-2 更严，因为 MLA decode 输出维度高、数值范围受控，可以压到更小容差。
- L237-L239 测延迟并换算 TFlops。L258 同样用 `backend="cupti"` 做回归。

两个例子合在一起印证了本讲的核心方法论：**正确的性能报告 = 合适的输入供给 + 参考实现对照 + 足够的 warmup + 一致的计时后端**。

#### 4.4.4 代码实践

**目标**：跑通 FlashAttention example，得到一份「tilelang vs 参考实现」的延迟与吞吐对照表。

**步骤**（需要 CUDA GPU）：

1. 直接运行：

```bash
python examples/flash_attention/example_mha_fwd_bhsd.py \
    --batch 1 --heads 32 --seq_q 256 --seq_kv 256 --dim 64
```

2. 阅读输出中的 `Ref: X.XX ms` / `Ref: X.XX TFlops` 与 `Tile-lang: X.XX ms` / `Tile-lang: X.XX TFlops`。
3. 再跑一次 `--is_causal`，观察 causal 版本的 TFlops 是否约为非 causal 的 2 倍（因为 `total_flops *= 0.5` 而实际计算量也减半，TFlops 数值应接近，但延迟应下降）。

**需要观察的现象**：tilelang 的延迟应显著低于（或至少持平）PyTorch eager 参考，TFlops 显著更高；causal 版延迟更低。

**预期结果**：终端打印四行性能数字，tilelang TFlops 远高于参考。**无 GPU 时标记「待本地验证」——此时可改为源码阅读型实践：跟踪 L141 与 L144 两次 `do_bench` 调用的差异（一个传 `ref_program_processed`、一个用默认 adapter），并解释为何它们共用同一份输入。**

#### 4.4.5 小练习与答案

**练习 1**：FlashAttention example 里 `total_flops *= 0.5`（causal），但延迟通常并不会正好减半。为什么 TFlops 数字看起来仍然合理？
**答**：`total_flops` 是「理论计算量」，causal 把它砍半；分母 `latency` 是实测耗时。TFlops = 理论 FLOPS / 实测延迟，反映的是「硬件在多大程度逼近峰值」。causal 下分子分母大致同步下降，所以 TFlops 数字与非 causal 同量级，仍然可信地刻画了硬件利用率。

**练习 2**：MLA example 用 `TensorSupplyType.Randn`、FlashAttention 用默认 `Auto`。如果互换会怎样？
**答**：FlashAttention 互换通常仍能通过（两者对 fp16 输入都生成合理范围）；MLA 换成 `Auto` 在 fp16 下走 `uniform(-1,1)`，多数情况也能通过，但 MLA 的多 split 归约对极端值更敏感，`Randn`（更接近真实分布）能减少「偶发数值超差」导致的误报，容差也能开得更严（1e-4）。

---

## 5. 综合实践

设计一份**完整的 GEMM 性能报告**，把本讲四个模块串起来。任务：

1. 用 `@tilelang.jit` 写一个分块 GEMM（参考 u1-l4 / u3-l1），M=N=K=4096，fp16 输入、fp32 累加、fp16 输出。
2. 调 `kernel.get_profiler()`，先 `assert_allclose` 对照 `torch.matmul`（注意把 fp16 matmul 结果升 fp32 再比较，容差 1e-2），再 `assert_consistent(repeat=10)`。
3. 分别用 `event` 与 `cupti` 两种 backend 测 tilelang kernel 与参考实现的延迟，填入下表（示例模板，数值待本地验证）：

   | 对象 | backend | 延迟 (ms) | TFlops |
   |------|---------|-----------|--------|
   | torch.matmul | event | ? | ? |
   | tilelang | event | ? | ? |
   | tilelang | cupti | ? | ? |

   TFlops 用 `2*M*N*K / latency_ms * 1e-9` 计算。

4. 把 `num_stages` 在 {1, 2, 3} 之间扫一遍（手改参数即可，不必接 autotuner），记录每个配置的延迟，验证「软件流水线通常更快」的直觉。
5. 写一段 3-5 行的结论：tilelang 相对参考的加速比、哪个 backend 更适合你的场景、哪个 `num_stages` 最优。

**评判标准**：报告应包含正确性校验通过的证据、至少两种 backend 的延迟、以及基于延迟换算的 TFlops；结论应能解释数字背后的原因（如流水线隐藏访存、cupti 计入设备总时间等）。无 GPU 时，改为「源码阅读型报告」：精读 `_do_bench_impl` 的 L198-L236，画出三种 backend 在「冲缓存位置、计时对象、是否支持分位数」三方面的对比表。

## 6. 本讲小结

- `do_bench(fn, warmup, rep, backend, ...)` 是与 tilelang 无关的通用 GPU 计时引擎：先用 5 次试跑估单次耗时，再按 `⌊warmup/estimate⌋`、`⌊rep/estimate⌋` 自动推断迭代数，每次正式计时前用一块 256MB buffer 冲刷 L2。
- 三种计时后端各有取舍：`event`（默认，通用、支持分位数）、`cupti`（设备侧最准、扣冲刷耗时，回归测试首选）、`cudagraph`（回放消除 launch 开销，适合极轻量 kernel）。
- `Profiler` 是把「输入供给 + adapter + 正确性校验 + do_bench」编成一个 `@dataclass`；`JITKernel.get_profiler()` 一行注入 adapter；`do_bench` 方法用 `partial(adapter, *ins)` 把带输入的调用包成零参闭包。
- 测延迟前务必先 `assert_allclose`（对照参考实现）与 `assert_consistent`（查 race condition），否则「又快又错」毫无意义。
- `TensorSupplyType` 决定输入分布；`Auto` 对 fp8/fp4 等低精度做了安全特化（小范围整数避免溢出），`torch_assert_close` 允许一定比例元素超差以适配低精度。
- 吞吐换算公式：`TFlops = total_flops / latency_ms * 1e-9`；FlashAttention 与 MLA 两个 example 共同示范了「参考对照 + warmup + 一致 backend」的标准性能报告范式。

## 7. 下一步学习建议

- **回到 autotuner**：本讲的 `do_bench` 正是 u8-l1 中 Autotuner 测量候选配置的底层引擎。建议重读 `tilelang/autotuner/tuner.py`，确认 `ProfileArgs` 里的 `warmup/rep/early_stop_baseline` 是如何透传到这里的。
- **Carver 与静态性能预估**：u8-l2 的 Carver 不实测而是用静态模型估访存量与波数，可与本讲的「实测延迟」对比，理解「静态排序 + 实测取优」的上下游关系。
- **调试性能问题**：若 `do_bench` 测出的延迟不理想，下一步是用 u9-l1 的 lower trace / pass 可视化观察 IR，确认 `lower_tile_op`、`inject_pipeline` 等是否按预期工作；或用 `kernel.get_kernel_source()` 看 SASS 层面的指令。
- **多后端对照**：结合 u4-l4（target 与多后端）与 u7-l1（execution backend），尝试用不同 `execution_backend`（tvm_ffi/nvrtc/torch）编译同一 kernel，用本讲的 `do_bench` 测出 adapter 自身对延迟的影响。
