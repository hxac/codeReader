# Manifest 驱动的基准

## 1. 本讲目标

上一篇（u6-l1）我们解决了「测出的 latency 是否可信」的问题——`bench_kernel` 用 CUPTI 给出纯 kernel 计时。但一个可信的延迟数字本身并没有意义，必须配上 **FLOP 数**与**字节数**才能换算成 TFLOPS、带宽，进而对照硬件理论上限（Speed-of-Light，SOL）。

本讲回答两个紧随其后的问题：

1. **基准的输入（形状、dtype、op 参数）从哪里来？** 答案是 manifest 的 `workloads`，而不是基准文件里手写的一堆魔法数字。
2. **基准报告里的 TFLOPS / 带宽分母（FLOP、字节）从哪里来？** 答案是 `op.eval_roofline()`，而不是基准文件里自己抄一遍公式。

学完本讲，你应能：

- 区分 `workloads_to_params` 与 `workload_field_params` 两个 helper 的适用场景，并知道何时用哪一个。
- 解释 `ManifestBenchmark` 如何把 FLOP/字节的来源完全外包给 `op.eval_roofline()`。
- 说清为什么「基准不得本地硬编码公式」是一条不可妥协的信任边界，而不是风格偏好。

## 2. 前置知识

在进入源码前，先建立三个直觉。

**直觉一：基准需要「参数空间」与「度量来源」两个独立输入。** 一个基准测试函数（如 `test_sum_bench`）要被 pytest 跑很多次，每次喂不同的 `(shape, dtype, op_params)`。这些「喂什么」就是**参数空间**；而每次跑完后用来算 TFLOPS 的 FLOP 数、算带宽的字节数，则是**度量来源**。TileOPs 的设计主张是：两者都应当由 manifest 驱动，而不是在基准文件里临时编造。

**直觉二：manifest 的 `workloads` 是「基准负载」而非「单测覆盖」。** 在 u4-l3 我们讲过，`workloads` 描述的是「这个算子在真实大模型里跑成什么样」（如 LLaMA-3.1-8B prefill 的 `[2048, 4096]`），它服务基准参数化；单测的形状是另一回事，针对 kernel 代码路径的边界。本讲只关心前者。

**直觉三：FLOP/字节公式是 manifest 的「声明式契约」。** 在 u4/u7 我们看到 roofline 公式（`flops`、`bytes`）写在 manifest 里，并经代码生成（u8-l2）编译成 `op.eval_roofline()` 方法。这意味着：**任何想要 FLOP/字节的人，唯一合法的获取方式就是调用 `op.eval_roofline()`。** 基准文件如果在本地再抄一份 `2 * m * n` 之类的公式，就会与 manifest 产生两份真相，迟早漂移。

把这三条合起来，本讲的核心一句话是：**基准文件应当只描述「怎么喂、怎么计时、记录成什么 tag」，而把「喂什么」外包给 manifest workloads、「度量来源」外包给 eval_roofline。**

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [`benchmarks/benchmark_base.py`](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/benchmarks/benchmark_base.py) | 本讲主角。`workloads_to_params`、`workload_field_params`、`ManifestBenchmark`、`BenchmarkBase` 全部在此。 |
| [`tileops/manifest/__init__.py`](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/manifest/__init__.py) | `load_workloads`、`WORKLOAD_RESERVED_KEYS`、`single_input_workload_contract`——`workloads_to_params` 推导 shape_key 与校验未知键的依据来自这里。 |
| [`benchmarks/ops/bench_norm.py`](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/benchmarks/ops/bench_norm.py) | `ManifestBenchmark` 的典型用例：RMSNorm/LayerNorm 基准，展示「构造 op → `bm.profile(op, *inputs)` → `record`」的骨架。 |
| [`benchmarks/ops/bench_softmax.py`](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/benchmarks/ops/bench_softmax.py) | `workloads_to_params` 的单输入用例（含 `include_extra=True` 的 LogSumExp 例子）。 |
| [`benchmarks/ops/bench_fp8_quant.py`](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/benchmarks/ops/bench_fp8_quant.py) | `workload_field_params` 的多字段用例（FP8 量化的 batch/seq_len/kv_group/index_dim/in_dtype）。 |
| [`docs/design/testing.md`](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/docs/design/testing.md) | 基准的权威规则（§Benchmarks）：workload 协议、file checklist、reporting rules。 |

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

1. **`workloads_to_params` / `workload_field_params`**——把 manifest workloads 翻译成 pytest 参数。
2. **`ManifestBenchmark` / `eval_roofline`**——把 FLOP/字节的来源外包给 op。
3. **禁止本地公式**——为什么前两个模块拼起来构成一条信任边界。

---

### 4.1 workloads_to_params / workload_field_params：从 manifest workloads 到 pytest 参数

#### 4.1.1 概念说明

基准要被参数化，就要有一组 `(形状, dtype, 可能的 op 参数)`。最朴素的做法是在基准文件里手写一个列表：

```python
# 反例：基准文件里手写魔法数字
PARAMS = [(2048, 4096, torch.float16), (8192, 8192, torch.bfloat16), ...]
```

这有两个问题：一是形状脱离了 manifest（与 u4-l3 的 `workloads` 重复且会漂移），二是失去了「LLaMA 场景标注」等语义。TileOPs 提供两个 helper 把 manifest workloads 翻译成 pytest.param，**两个 helper 各管一类算子**：

- **`workloads_to_params(op_name, include_extra=False)`**：给**单张量输入**算子用（softmax、rms_norm、sum、cumsum 这类只有一个主输入的）。它**自动**从 manifest signature 推导出 shape 键名、校验 workloads 里没有多余键、并按 `dtypes` 展开。
- **`workload_field_params(workloads, keys)`**：给**多输入或多字段**算子用（FP8 量化把维度拆成 `batch/seq_len_kv/kv_group/index_dim`、grouped GEMM、engram 这类）。它**显式**地由调用方声明「我要哪几个字段、按什么顺序」，并自动打上 `smoke`/`full` 标记。

> 这一改动是本轮新增：`workload_field_params` 作为 `workloads_to_params` 的「同伴」被引入，专门承接多字段负载。以前这类算子要么手写循环（如 `bench_norm.py` 里的 `_rms_params()`），要么各自为政；现在有了一个统一入口。

#### 4.1.2 核心流程

两个 helper 的处理流程对照如下：

```
workloads_to_params(op_name)                # 单输入路径
  │
  ├─ workloads = load_workloads(op_name)    # 取 manifest workloads
  ├─ (shape_key, allowed) = _workload_contract(op_name)
  │      └─ single_input_workload_contract(signature)
  │            └─ 要求 signature.inputs 恰好 1 个 tensor → shape_key = "{name}_shape"
  │            └─ allowed = {shape_key} ∪ {dtypes,label} ∪ signature.params 的名字
  │
  └─ for w in workloads:
        ├─ 校验 w 含 shape_key      ─┐
        ├─ 校验 w 无未知键（不在 allowed 内，且不以 __ 开头）─┤ 任一失败 → KeyError
        ├─ shape = tuple(w[shape_key])
        ├─ extra = 剥离保留键后的 op-call 参数   # 仅 include_extra=True
        └─ for dtype_str in w["dtypes"]:
              → pytest.param(shape, dtype[, dict(extra)], id="{label}-{dtype_str}")


workload_field_params(workloads, keys)      # 多字段路径
  │
  └─ for i, w in enumerate(workloads):
        ├─ args = [getattr(torch, w[k]) if k.endswith("dtype") else w[k]
        │         for k in keys]            # 按调用方声明的 keys 顺序投影
        ├─ marks = smoke if i==0 else full  # 第一个负载是 smoke 门禁，其余是 full
        └─ pytest.param(*args, marks=..., id=w["label"])
```

关键差异点：

| 维度 | `workloads_to_params` | `workload_field_params` |
| --- | --- | --- |
| 入参 | `op_name`（字符串），内部自己 `load_workloads` | `workloads`（列表，调用方先 `load_workloads`）+ `keys` 元组 |
| 适用 | signature 恰好 1 个 tensor 输入 | 任意负载结构，尤其多字段/多输入 |
| shape 来源 | 自动从 signature 推 `{name}_shape` | 调用方在 `keys` 里点名要哪些字段 |
| 未知键校验 | 有（多余键报 `KeyError`） | 无（你要什么取什么，多余字段忽略） |
| dtype 处理 | 按 `w["dtypes"]` 列表展开，每项一个 param | 把名字以 `dtype` 结尾的字段经 `getattr(torch, ...)` 解析成 `torch.dtype` |
| pytest mark | 不打 smoke/full | 第一个 `smoke`，其余 `full` |
| op-call 参数 | `include_extra=True` 时作为第 3 个元素（dict） | 无（参数就是字段本身） |

#### 4.1.3 源码精读

**`workloads_to_params` 的 shape_key 推导与未知键校验**——核心是 `_workload_contract`：

[benchmarks/benchmark_base.py:31-40](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/benchmarks/benchmark_base.py#L31-L40) 拿到算子的 signature，调 `single_input_workload_contract` 求出 `(shape_key, allowed)`；若该算子不是单输入（如 GQA），直接抛 `KeyError` 提示「多输入算子用自己的 bench 文件」。

shape_key 的真正推导在 manifest 包里：

[tileops/manifest/__init__.py:85-108](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/manifest/__init__.py#L85-L108) 要求 `signature.inputs` 恰好一个键；shape_key 由该输入名拼成 `f"{input_name}_shape"`；`allowed` 集合 = 输入的 shape_key ∪ `WORKLOAD_RESERVED_KEYS`（`{"dtypes", "label"}`，见 [第 82 行](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/manifest/__init__.py#L82)）∪ signature 声明的 `params` 名字。

以 `RMSNormFwdOp` 为例，它的 signature 输入名为 `x`，故 shape_key = `x_shape`，其 manifest workload 形如 `{'x_shape': [2048, 4096], 'dtypes': ['float16','bfloat16'], 'label': 'llama-3.1-8b-prefill'}`（见 `load_workloads` 的 docstring 示例 [第 70-72 行](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/manifest/__init__.py#L70-L72)）。若某个 workload 多了一个 `dim: 0`，但 signature 的 `params` 里没有 `dim`，`allowed` 就不含 `dim`，校验会失败。

**未知键校验本体**在 `workloads_to_params` 里：

[benchmarks/benchmark_base.py:503-511](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/benchmarks/benchmark_base.py#L503-L511) 收集所有「不在 allowed 且不以 `__` 开头」的键，若有则抛 `KeyError`，列出非法键与合法键清单。`__` 前缀是给基准私房键留的逃生口（如 `__bench_only`），不计入校验。

**`include_extra=True` 的 op-call 参数剥离**：

[benchmarks/benchmark_base.py:477-484](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/benchmarks/benchmark_base.py#L477-L484) 把 workload 里除「保留键 + shape_key」之外的键当成 op 调用参数收集（例如 `{"dim": 0}`）。注意 [第 519-523 行](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/benchmarks/benchmark_base.py#L519-L523) 每个 param 拿到的是 `dict(extra)` 的**副本**——这样某个用例在测试体里 `op_params.setdefault(...)` 改字典时，不会泄漏到共享同一 workload 的其它用例。

**`workload_field_params`——按 keys 取值 + dtype 解析 + smoke/full 标记**：

[benchmarks/benchmark_base.py:528-544](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/benchmarks/benchmark_base.py#L528-L544) 是本轮新增的同伴函数。三个要点：

- 第 536 行：`args` 按 `keys` 顺序投影字段；名字以 `"dtype"` 结尾的（如 `in_dtype`）走 `getattr(torch, w[k])` 解析成 `torch.dtype`，其余原样取值。这样调用方写 `("batch", "seq_len_kv", "index_dim", "in_dtype")` 就能把 manifest 里的字符串 `"float8_e4m3fn"` 自动变成 `torch.float8_e4m3fn`。
- 第 540 行：第一个 workload 标 `pytest.mark.smoke`，其余标 `pytest.mark.full`。`smoke` 是 PR CI 的快速门禁（只跑一个代表性形状），`full` 是夜行全量——这套标记在 [benchmarks/tests/test_run_benchmarks.py](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/benchmarks/tests/test_run_benchmarks.py) 的夜行运行器里被用来选路（详见 u6-l4）。
- 第 541 行：`id=w["label"]`，所以基准用例名就是 manifest 里的人类可读标签（如 `llama-3.1-8b-prefill`），报告里一眼能对上场景。

**两个 helper 的真实调用点对比**：

单输入 + 按 dtype 展开（softmax）：

[benchmarks/ops/bench_softmax.py:29-35](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/benchmarks/ops/bench_softmax.py#L29-L35) 一行 `@pytest.mark.parametrize("shape, dtype", workloads_to_params(_SOFTMAX_OP))` 就把 manifest 里所有 softmax 负载、所有 dtype 全展开成用例。带 op 参数的版本见同文件 LogSumExp：[第 79-82 行](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/benchmarks/ops/bench_softmax.py#L79-L82) 用 `include_extra=True`，测试体里 [第 89-90 行](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/benchmarks/ops/bench_softmax.py#L89-L90) `op_params.setdefault("dim", -1)` 再 `LogSumExpFwdOp(dtype=dtype, tune=True, **op_params)`。

多字段投影（FP8 量化）：

[benchmarks/ops/bench_fp8_quant.py:38-44](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/benchmarks/ops/bench_fp8_quant.py#L38-L44) 调 `workload_field_params(load_workloads(_FP8_QUANT_OP), ("batch", "seq_len_kv", "kv_group", "index_dim", "in_dtype"))`，基准函数签名直接是 `test_fp8_quant_bench(batch, seq_len_kv, kv_group, index_dim, in_dtype)`——字段被原样解构成位置参数。这里 `in_dtype` 自动解析成 `torch.dtype`（见测试体 [第 45-46 行](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/benchmarks/ops/bench_fp8_quant.py#L45-L46)）。

#### 4.1.4 代码实践

**实践目标**：亲手把两个 helper 的差异跑出来，观察「自动推导 vs 显式投影」「未知键校验 vs 静默忽略」。

**操作步骤**（源码阅读型，无需 GPU）：

1. 打开 [tileops/manifest/normalization.yaml](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/manifest/normalization.yaml)，找到 `RMSNormFwdOp` 的 `signature.inputs`，确认输入名是 `x`，于是 shape_key 应为 `x_shape`。
2. 在交互式 Python 里（`python -c` 即可，不需要 CUDA）执行：

   ```python
   from benchmarks.benchmark_base import workloads_to_params, workload_field_params
   from tileops.manifest import load_workloads

   # 单输入路径：自动推导 x_shape、按 dtypes 展开
   p1 = workloads_to_params("RMSNormFwdOp")
   print([(x.values[0], x.values[1]) for x in p1])   # (shape_tuple, torch.dtype)

   # 多字段路径：显式点名字段、第一项 smoke 其余 full
   wl = load_workloads("FP8QuantOp")
   p2 = workload_field_params(wl, ("batch", "index_dim", "in_dtype"))
   for x in p2:
       print(x.id, x.marks)
   ```

3. 故意造一个未知键观察报错。在 `workloads_to_params` 的校验逻辑里（[第 503-511 行](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/benchmarks/benchmark_base.py#L503-L511)），未知键会触发 `KeyError`。你可以在脑中（或临时改 manifest 副本）给 `RMSNormFwdOp` 某个 workload 加一个 `bogus: 1`，预期看到 `unknown keys ['bogus']; allowed: ['dtypes', 'label', 'x_shape']` 之类的错误。

**需要观察的现象**：

- `workloads_to_params` 返回的每个 param 是 `(shape_tuple, dtype)` 二元组（或带 `extra` 的三元组），`id` 形如 `llama-3.1-8b-prefill-float16`。
- `workload_field_params` 返回的 param 元素个数 = `keys` 长度，`id` 就是 `w["label"]`（不带 dtype 后缀），且 `marks` 上能看到 `smoke`/`full`。

**预期结果**：两者都能把 manifest workloads 翻成 pytest.param，但 `workloads_to_params` 的产物「形状是一个整体元组 + 多 dtype 展开」，而 `workload_field_params` 的产物「字段被打散成位置参数 + 不展开 dtype」。

> 若本地无 manifest 读取环境，步骤 2 的具体输出标注「待本地验证」，但两条 helper 的**返回结构差异**可由源码直接推断，不依赖运行结果。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `workload_field_params` 不做未知键校验，而 `workloads_to_params` 必须做？

**参考答案**：`workloads_to_params` 走「自动推导」路线——shape_key 与合法键都由 signature 推断，调用方没有机会声明「我想要哪些字段」，所以必须主动校验 workloads 没有多余/拼错的键，否则拼错的键会被当成 op-call 参数默默传进 op。`workload_field_params` 走「显式投影」路线——调用方在 `keys` 里逐个点名要取的字段，多余字段天然被忽略（根本不会被读取），所以不需要校验。

**练习 2**：若一个基准既想用 manifest workloads 的形状、又想自定义 `tune` 参数（autotuning 开关），该用哪个 helper？为什么 `bench_fp8_quant.py` 把 `_TUNE = True` 写在文件顶部而不是塞进 workloads？

**参考答案**：`tune` 不是 workload 的属性，它是「基准运行策略」。`bench_fp8_quant.py` 在 [第 22 行](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/benchmarks/ops/bench_fp8_quant.py#L20-L22) 的注释说得明白：「Autotuning is a bench-run policy, not a workload property; manifest workloads do not carry it.」所以 `_TUNE` 作为文件级常量、所有用例共用，而不是污染 manifest。

---

### 4.2 ManifestBenchmark / eval_roofline：FLOP 与字节的唯一来源

#### 4.2.1 概念说明

上一篇 u6-l1 讲过 `BenchmarkBase[W]` 是个泛型抽象基类，子类必须实现 `calculate_flops()` 与 `calculate_memory()`。最直白的做法是每个基准文件自己写个 `BenchmarkBase` 子类，在 `calculate_flops` 里抄一遍公式。但这就违背了「FLOP/字节公式的唯一真相是 manifest」的原则。

`ManifestBenchmark` 就是这个原则的落地：它是一个**现成的 `BenchmarkBase` 子类**，把 `calculate_flops`/`calculate_memory` 的实现**统一外包给 `op.eval_roofline()`**。基准文件不再写公式，而是 `bm = ManifestBenchmark(op_name, op, workload)`，剩下的交给它。

#### 4.2.2 核心流程

```
ManifestBenchmark(op_name, op, workload)
  └─ 继承 BenchmarkBase[ShapeDtypeWorkload]，workload 仅需提供 shape/dtype 元数据

bm.profile(op, *inputs)
  ├─ bench_kernel(op, args=inputs)   # u6-l1 的纯 kernel 计时 → latency_ms
  └─ _build_result(latency)
        ├─ calculate_flops()  ─┐
        │                      ├─ 都走 _get_roofline()
        ├─ calculate_memory() ─┘     └─ self._op.eval_roofline() → (flops, bytes)  [首次调用后缓存]
        ├─ result["tflops"] = flops / latency * 1e-9
        └─ result["bandwidth_tbs"] = bytes / latency * 1e-9
```

两个值得注意的设计：

1. **roofline 懒求值且缓存**：`eval_roofline()` 只在 `_build_result`（即计时跑完之后）才被调，且结果缓存在 `_roofline_cache`。为什么要「计时之后」才调？因为有些动态形状算子要在 `forward()` 里才能绑定 roofline 变量——`profile(op, *inputs)` 先把 op 真正跑一遍，变量才落定，此时再问 `eval_roofline()` 才拿得到正确数字。
2. **泛型边界是 `ShapeDtypeWorkload`，不是 `BenchmarkWorkload`**：`ManifestBenchmark` 只需要 workload 提供 `shape` 与 `dtype` 元数据（用来在报告里记录几何），不需要 `gen_inputs()`。输入生成是基准函数自己的事。

#### 4.2.3 源码精读

**`ManifestBenchmark` 的契约**：

[benchmarks/benchmark_base.py:546-583](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/benchmarks/benchmark_base.py#L546-L583) 整个类只做一件事：把 `calculate_flops`/`calculate_memory` 转发给 `eval_roofline()`。关键三段：

- [第 562-571 行](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/benchmarks/benchmark_base.py#L562-L571) 构造函数收 `(op_name, op, workload)`，初始化空的 `_roofline_cache`。
- [第 573-577 行](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/benchmarks/benchmark_base.py#L573-L577) `_get_roofline()`：缓存未命中时调 `self._op.eval_roofline()` 拿 `(flops, bytes)`，转成 `float` 存缓存。
- [第 579-583 行](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/benchmarks/benchmark_base.py#L579-L583) `calculate_flops`/`calculate_memory` 分别返回缓存元组的第 0、第 1 个元素。

类 docstring（[第 547-560 行](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/benchmarks/benchmark_base.py#L547-L560)）明确写了「The op must implement `eval_roofline()`」以及「Dynamic-shape ops may bind roofline variables during `forward()`，so this helper calls `op.eval_roofline()` only while building a result after profiling has executed the op」——这就是懒求值的依据。

**TFLOPS / 带宽的换算**在基类 `_build_result`：

[benchmarks/benchmark_base.py:465-470](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/benchmarks/benchmark_base.py#L465-L470) 给出换算公式。以 TFLOPS 为例：

\[
\text{TFLOPS} = \frac{\text{flops}}{\text{latency\_ms}} \times 10^{-9}
\]

其中 `flops` 来自 `eval_roofline()`，`latency_ms` 来自 `bench_kernel`。两个输入都「有名有姓」（一个来自 manifest，一个来自 GPU 实测），没有一个是基准文件本地编的。注意 `1e-9` 的来历：`flops / latency_ms` 的单位是 `ops/ms = ops / 10^{-3}s = 10^3 ops/s`，再乘 `1e-9` 得 `10^{-6} ops/s`…… 实际上代码里 `flops / latency * 1e-9` 是把「FLOP 数 / 毫秒」换算到「TFLOPS（10^12 ops/s）」：`flops / (latency_ms * 1e-3)` 是 ops/s，再 `/ 1e12` 得 TFLOPS，等价于 `flops / latency_ms * 1e-9`。带宽同理：`bytes / latency_ms * 1e-9` 得到的是 TB/s 量级（按十进制字节与秒换算后的系数选择）。

**真实用例（RMSNorm）**：

[benchmarks/ops/bench_norm.py:51-57](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/benchmarks/ops/bench_norm.py#L51-L57) 是 `ManifestBenchmark` 的标准用法：

```python
op = RMSNormFwdOp(normalized_shape=(n,), dtype=dtype, tune=tune)
bm = ManifestBenchmark(_RMS_OP_NAME, op, test)
result = bm.profile(op, *inputs)                 # tileops 实现
BenchmarkReport.record(op, locals(), result, tag="tileops")

result_bl = bm.profile(test.ref_program, *inputs)  # torch 基线
BenchmarkReport.record(op, locals(), result_bl, tag="torch-ref")
```

注意 `bm` 被复用来跑两条曲线：一条是 `op`（tileops 实现），一条是 `test.ref_program`（torch 基线）。两条都用**同一个** `ManifestBenchmark` 实例，所以两条曲线的 TFLOPS/带宽分母（FLOP、字节）**完全一致**——这正是公平对照的前提。`eval_roofline()` 给出的是「这个 workload 理论上需要多少 FLOP/字节」，与「用谁的实现去跑」无关。

#### 4.2.4 代码实践

**实践目标**：确认基准报告里的 TFLOPS 完全由 `eval_roofline()` 驱动，与基准文件无关。

**操作步骤**（源码阅读型）：

1. 读 [bench_norm.py:51-57](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/benchmarks/ops/bench_norm.py#L51-L57)，确认整段**没有任何**形如 `2 * m * n` 或 `m * n * dtype_size` 的本地公式。
2. 追踪 `bm.profile(op, *inputs)` 的调用链：`profile`（[第 434-445 行](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/benchmarks/benchmark_base.py#L434-L445)）→ `bench_kernel`（拿 latency）→ `_build_result`（[第 457-471 行](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/benchmarks/benchmark_base.py#L457-L471)）→ `calculate_flops`/`calculate_memory`（[第 579-583 行](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/benchmarks/benchmark_base.py#L579-L583)）→ `_get_roofline`（[第 573-577 行](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/benchmarks/benchmark_base.py#L573-L577)）→ `op.eval_roofline()`。
3. 若有 GPU，可运行 `pytest benchmarks/ops/bench_norm.py::test_rms_norm_bench -q`（首次 JIT 编译会慢），观察生成的 `profile_run.log` 里 RMSNorm 的 tileops 与 torch-ref 两行的 tflops/bandwidth 列。

**需要观察的现象**：基准文件里找不到 FLOP/字节公式；TFLOPS 的分子来自 manifest（经 codegen 编进 `eval_roofline`），分母来自 CUPTI 计时。

**预期结果**：报告里同一 workload 的 tileops 行与 torch-ref 行共享相同的 FLOP/字节（只是 latency 不同），所以两者 TFLOPS 差异完全反映 latency 差异。具体数值「待本地验证」（需真实 GPU）。

#### 4.2.5 小练习与答案

**练习 1**：`ManifestBenchmark` 的泛型参数为什么是 `ShapeDtypeWorkload` 而不是 `BenchmarkWorkload`（后者还要求 `gen_inputs()`）？

**参考答案**：因为 `ManifestBenchmark` 的职责只到「读 FLOP/字节 + 换算 TFLOPS/带宽」，它不负责生成输入。输入生成（`gen_inputs()`）是基准函数 `test_xxx_bench` 自己调用的（见 bench_norm 里 `inputs = test.gen_inputs()`）。把泛型收窄到 `ShapeDtypeWorkload` 表达了「我只需要 shape/dtype 元数据」的最小依赖，符合 [testing.md §Workload protocols](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/docs/design/testing.md#L120-L130) 里「按需声明能力协议」的原则。

**练习 2**：为什么 `_get_roofline` 要缓存，而不是每次 `calculate_flops`/`calculate_memory` 都重算？

**参考答案**：一次 `profile` 调用里 `_build_result` 会分别调一次 `calculate_flops` 和 `calculate_memory`，若不缓存就会调两次 `eval_roofline()`；而且同一个 `bm` 还会被复用跑基线（见 bench_norm 跑 tileops + torch-ref 两次 `profile`）。缓存把多次调用收敛成一次 `eval_roofline()`，既省开销也保证两次 `profile` 用的是同一组 FLOP/字节。

---

### 4.3 禁止本地公式：manifest 驱动的信任边界

#### 4.3.1 概念说明

把 4.1 和 4.2 拼起来，就得到本讲的总纲：**基准文件应当是「薄」的——只描述怎么喂、怎么计时、记录成什么 tag，而把数据来源完全外包。** 这不是风格偏好，而是一条信任边界：

- **形状/dtype/op 参数**外包给 manifest `workloads`（经 `workloads_to_params` / `workload_field_params`）。
- **FLOP/字节**外包给 `op.eval_roofline()`（经 `ManifestBenchmark`）。

如果一个基准文件绕开这两条、自己手写公式或魔法形状，就等于在代码里复制了一份 manifest 的真相。两份真相迟早漂移：manifest 改了公式，基准还在用旧公式算 TFLOPS，报告的效率数字就静悄悄地错了——而 SOL 效率正是 TileOPs 衡量性能的标尺（见 u1-l1 的 Speed-of-Light 属性）。

#### 4.3.2 核心流程（防漂移的三道闸）

```
manifest（唯一真相）
   │
   ├── workloads ──→ workloads_to_params / workload_field_params ──→ pytest 参数空间
   │                                                                  （闸 1：形状不准本地编）
   │
   └── roofline ──→ codegen ──→ op.eval_roofline() ──→ ManifestBenchmark
                                                    （闸 2：FLOP/字节不准本地编）
   │
benchmark 文件只保留：op 构造、bm.profile、BenchmarkReport.record、tag
                                                    （闸 3：reporting rules 强制 ≥1 条非 tileops 基线）
```

三道闸都有制度保障：

- **闸 1、2**：靠 `workloads_to_params`/`workload_field_params`/`ManifestBenchmark` 这套现成基建。基准文件「懒得自己写」就自然合规；想自己写就得绕开 helper，代码审查里一眼可辨。
- **闸 3**：[testing.md §Reporting rules](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/docs/design/testing.md#L146-L154) 与 [.claude/domain-rules/benchmark.md](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/.claude/domain-rules/benchmark.md) 都规定「每个基准必须记录至少一条非 `tileops` 基线」。这条规则确保 tileops 的数字永远有外部参照（torch / fa3 / fla），不至于自说自话。

#### 4.3.3 源码精读

「禁止本地公式」最直接的体现是：**`ManifestBenchmark` 根本没有给子类留「自己写公式」的口子**——它已经是终态。对比 [BenchmarkBase](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/benchmarks/benchmark_base.py#L413-L471) 把 `calculate_flops`/`calculate_memory` 声明为 `@abstractmethod`（[第 426-432 行](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/benchmarks/benchmark_base.py#L426-L432)），本意是留给特殊算子（如 fused 多步算子）自定义；但对**绝大多数**算子，正确做法是直接用 `ManifestBenchmark`，把公式来源交回 manifest。

reporting 规则的权威表述：

[docs/design/testing.md:153-154](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/docs/design/testing.md#L153-L154) 规定 `calculate_flops()`/`calculate_memory()` 在指标可用时应返回数值，仅在「指标不适用」时返回 `None`（从报告里省略）。`ManifestBenchmark` 永远返回 `eval_roofline()` 的数值，正符合「可用就返回、且来源是 manifest」。

oracle 防泄漏边界（与「禁止本地公式」互为表里）：

[.claude/domain-rules/benchmark.md:5](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/.claude/domain-rules/benchmark.md#L5) 规定基准「MUST NOT 从 `tests/` 或 `workloads/` 导入 oracle/ref 函数」。这与本讲的联系是：基准文件需要的 ref 基线函数（如 bench_norm 的 `ref_program`）必须**就地定义**（见 [bench_norm.py:21-27](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/benchmarks/ops/bench_norm.py#L21-L27) 的 `_RMSNormTestBaseline`），不能从 tests/workloads 偷——否则正确性测试的 oracle 会污染性能基准，两个阶段（M3 正确性、M4 基准）的独立性就被破坏（详见 u9-l1 信任模型）。

#### 4.3.4 代码实践

**实践目标**：学会判断一个基准文件是否合规（形状/FLOP 都外包、有基线、ref 就地定义）。

**操作步骤**（源码阅读型）：

1. 打开 [bench_norm.py](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/benchmarks/ops/bench_norm.py)，逐条核对：
   - 形状来源：`_rms_params()` 用 `load_workloads(_RMS_OP_NAME)`（[第 36 行](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/benchmarks/ops/bench_norm.py#L36)），不是手写魔法数字——✅（注：norm 家族历史较早，用本地 `_rms_params` 而非 `workloads_to_params`，但同样从 manifest 取形状；新算子应直接用 helper）。
   - FLOP 来源：`ManifestBenchmark`（[第 52 行](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/benchmarks/ops/bench_norm.py#L52)），无本地公式——✅。
   - 基线：`tag="torch-ref"`（[第 57 行](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/benchmarks/ops/bench_norm.py#L57)），满足「≥1 条非 tileops 基线」——✅。
   - ref 定义：`_RMSNormTestBaseline.ref_program` 就地定义在基准文件——✅。
2. 对照 [bench_softmax.py](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/benchmarks/ops/bench_softmax.py)，注意它直接用了 `workloads_to_params(_SOFTMAX_OP)`（[第 29 行](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/benchmarks/ops/bench_softmax.py#L29)）与 `ManifestBenchmark`（[第 35 行](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/benchmarks/ops/bench_softmax.py#L35)），比 norm 更「薄」、更贴近本轮推荐的写法。

**需要观察的现象**：合规基准文件里，唯一出现的「数字」是 `tag` 字符串和（偶有的）`eps`/`tune` 策略常量；没有任何 FLOP/字节公式、没有脱离 manifest 的形状。

**预期结果**：你能用一张三栏表（形状来源 / FLOP 来源 / 基线 tag）快速审查任意 `bench_*.py`。

#### 4.3.5 小练习与答案

**练习 1**：假设有人为了「省事」在 `bench_xxx.py` 里写了 `class MyBench(BenchmarkBase): def calculate_flops(self): return 2 * m * n`。这违反了什么？应如何修？

**参考答案**：违反「FLOP 唯一来源是 manifest」的信任边界——`2*m*n` 是本地硬编码公式，与 manifest 的 roofline 公式形成两份真相，manifest 改了这里不会跟着改，TFLOPS 会静默出错。修法：删掉自定义子类，改用 `ManifestBenchmark(op_name, op, workload)`，让 `calculate_flops` 经 `eval_roofline()` 取数。除非该算子的 FLOP 真的无法用 manifest roofline 表达（极少数 fused 多步算子），才保留自定义并在代码审查中说明理由。

**练习 2**：`BenchmarkReport.record(op, locals(), result, tag=...)` 的第一个参数为什么要传 Op 实例而不是字符串？

**参考答案**：这是下一篇 u6-l3 的主题（规范身份与基线归类）。简言之：传 Op 实例时，`record` 会从 `op.__class__.__name__` 与 `__module__` 推导规范身份并抽 config（见 [第 651-654 行](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/benchmarks/benchmark_base.py#L651-L654)），避免字符串别名造成的重复归类；传字符串则只当一个裸名。本讲的基准文件统一传 Op 实例（如 `record(op, ...)`），为的是让报告能按规范身份正确分桶。

---

## 5. 综合实践

**任务**：为一个假想的「单输入 + 带 op 参数」算子（以 `LogSumExpFwdOp` 为蓝本）起草一份合规的 manifest 驱动基准骨架，要求同时演示两个 helper 与 `ManifestBenchmark`。

**步骤**：

1. **参数化（用 `workloads_to_params` + `include_extra=True`）**：参照 [bench_softmax.py:79-82](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/benchmarks/ops/bench_softmax.py#L79-L82)，写出装饰器，签名声明 `shape, dtype, op_params`。
2. **测试体**：
   - 构造 workload/test、`inputs = test.gen_inputs()`。
   - `op_params.setdefault("dim", -1)`，`op = LogSumExpFwdOp(dtype=dtype, tune=True, **op_params)`。
   - `bm = ManifestBenchmark(_OP_NAME, op, test)`。
   - `result = bm.profile(op, *inputs)`；`BenchmarkReport.record(op, locals(), result, tag="tileops")`。
   - 就地定义 `baseline_fn(x)` 调 `torch.logsumexp(...)`；`result_bl = bm.profile(baseline_fn, *inputs)`；`record(..., tag="torch")`。
3. **自查三道闸**：形状是否来自 manifest？FLOP 是否来自 `eval_roofline`（即用了 `ManifestBenchmark` 而非自定义 `BenchmarkBase` 子类）？是否记录了至少一条非 tileops 基线？ref 是否就地定义？
4. **对比拓展**：把同一个 workload 列表改用 `workload_field_params(load_workloads(_OP_NAME), ("shape", "dtype"))` 写一遍，观察它**不会**按 `dtypes` 展开（`dtype` 字段会被当成单个 key 解析，而不是遍历 `dtypes` 列表）——从而体会「单输入展开型」与「多字段投影型」的本质差异。

> 步骤 4 是理解两个 helper 差异的关键：`workload_field_params` 的 `keys` 是「字段名」而非「展开指令」，它不会把 `w["dtypes"]` 列表展开成多个用例；若 workload 里同时有 `dtypes`（列表）和 `dtype`（单值）两种字段，行为完全不同。具体展开行为「待本地验证」。

## 6. 本讲小结

- TileOPs 基准有两个「数据来源」必须外包：**形状/dtype/op 参数**外包给 manifest `workloads`，**FLOP/字节**外包给 `op.eval_roofline()`。
- `workloads_to_params(op_name, include_extra=False)` 服务**单张量输入**算子：自动从 signature 推 `{name}_shape`、校验未知键、按 `dtypes` 展开，可选地把剩余键作为 op-call 参数。
- `workload_field_params(workloads, keys)` 是本轮新增的同伴，服务**多输入/多字段**算子：调用方显式点名要投影的字段、`*dtype` 字段自动解析成 `torch.dtype`、第一个负载标 `smoke` 其余标 `full`。
- `ManifestBenchmark` 是 `BenchmarkBase` 的终态子类，把 `calculate_flops`/`calculate_memory` 统一转发给 `op.eval_roofline()`（懒求值 + 缓存），基准文件因此无需写任何公式。
- 「禁止本地公式」是信任边界而非风格：本地硬编码形状或 FLOP 会与 manifest 形成两份真相，污染 SOL 效率标尺；外加「每个基准 ≥1 条非 tileops 基线」「ref 就地定义不偷 tests/workloads」两道闸，共同保证基准数字可信、可对照、不泄漏。

## 7. 下一步学习建议

- **u6-l3（报告与基线对比）**：本讲只用到 `BenchmarkReport.record` 的最简形式，下一篇深入 `record`/`dump` 如何按 tag 分组生成 markdown 表格、规范 Op 身份如何推导、为何必须传 Op 实例。
- **u7-l1/l2（Roofline 模型与字段）**：本讲把 FLOP/字节当成「`eval_roofline()` 给的黑盒」，u7 会打开这个黑盒，讲 inline/func 两种 roofline 模式与 SOL 效率公式。
- **u8-l2（Roofline 代码生成）**：想彻底理解 `eval_roofline()` 是怎么从 manifest YAML 变成可调用方法的，看 codegen。
- **建议阅读源码**：拿 [bench_softmax.py](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/benchmarks/ops/bench_softmax.py)（`workloads_to_params`）与 [bench_fp8_quant.py](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/benchmarks/ops/bench_fp8_quant.py)（`workload_field_params`）对照阅读，是巩固两个 helper 差异最快的方式。
