# 自动调参 Autotuner

## 1. 本讲目标

写出一个能跑的算子只是第一步，写出一个**跑得快**的算子往往需要在「分块大小、流水级数、内存布局、CV 配比」等多个参数之间反复试错。这些参数的最优值与具体硬件、具体形状强相关，人工枚举既费力又容易漏掉最优解。tile-lang 提供了一套**自动调参（autotuning）**框架，把「给定一组候选配置 → 编译 → 上板计时 → 选出最快」这件事自动化、并行化、可缓存化。

本讲聚焦 `tilelang.autotuner`，学完后你应当能够：

1. 用 `@tilelang.autotune` 装饰器为算子定义一个**调参空间**（手动枚举或 `itertools` 笛卡尔积），并理解它与 `@tilelang.jit` 的协作契约。
2. 说清 `AutoTuner.run` 的三段式主流程：**并行编译 → 串行评测 → 取最优**，以及为什么编译可并行、评测必须串行。
3. 掌握 `autotuner.capture` 的输入张量注入机制（`set_autotune_inputs`），理解它如何取代手写 `supply_prog`。
4. 理解 `profiler.bench` 的 NPU 计时原理（L2 刷缓存、warmup/rep、event 计时）与正确性校验（`assert_allclose`）。
5. 会把调参结果**回填并复用**：直接调用最优 kernel、`get_tuner_result()` 查看结果、磁盘缓存命中后跳过整轮调参，以及「显式传参即跳过调参」的快捷用法。

## 2. 前置知识

在进入 autotuner 之前，请确认你已经理解以下概念（前序讲义已建立）：

- **JIT 即时编译链路**（见 [u1-l5](u1-l5-jit-and-pipeline.md)）：`@tilelang.jit` 装饰的函数在首次按 shape 调用时触发「lowering → codegen → bisheng 编译 → ctypes 加载」。autotuner 本质上就是把这条链路**对一组配置各跑一次**，再比较耗时。
- **GEMM 算子结构与分块参数**（见 [u1-l4](u1-l4-first-gemm.md)、[u7-l2](u7-l2-hi-perf-gemm.md)）：`block_M`/`block_N` 是 M/N 维分块大小，`K_L1` 是 K 维每次搬入 L1 的分段大小，`num_stages` 是流水重叠度。这些正是 autotuner 要搜索的「旋钮」。
- **PassContext 配置**（见 [u6-l1](u6-l1-pass-overview.md)）：Ascend 上常需开启 `TL_ASCEND_AUTO_CV_COMBINE`/`TL_ASCEND_AUTO_SYNC`/`TL_ASCEND_MEMORY_PLANNING` 等开关，它们通过 `pass_configs` 传入，**每个候选配置都共享同一套 pass 配置**。
- **`torch.npu` 与 NPU stream**（见 [u7-l3](u7-l3-torch-aclgraph.md)、[u7-l4](u7-l4-debug-profiling.md)）：计时依赖 `torch.npu.synchronize()` 与 `torch.npu.Event`，与 CUDA 上的 `do_bench` 几乎同构。

一个直觉比喻：autotuner 像是一个「考试选拔系统」——你提供一份**考卷清单**（候选配置），系统为每份考卷**印一份试卷**（编译，可并行印刷），然后让同一个考生（单块 NPU）**依次作答并掐表**（评测必须串行），最后选出**用时最短**的那份。编译产物与最优成绩都会存进「档案室」（磁盘缓存），下次同题直接调档。

## 3. 本讲源码地图

本讲涉及的关键文件及其作用如下：

| 文件 | 作用 |
| --- | --- |
| [tilelang/autotuner/tuner.py](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/autotuner/tuner.py) | autotuner 核心：`@autotune` 装饰器、`AutoTuneImpl`、`AutoTuner.run` 主流程、缓存键、并行编译与串行评测 |
| [tilelang/autotuner/capture.py](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/autotuner/capture.py) | 输入张量捕获：线程局部栈 `CaptureStack`、`set_autotune_inputs`/`get_autotune_inputs` |
| [tilelang/profiler/bench.py](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/profiler/bench.py) | 底层计时函数 `do_bench`：L2 刷缓存、warmup/rep 估算、event 计时 |
| [tilelang/profiler/__init__.py](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/profiler/__init__.py) | `Profiler`：`_get_inputs` 生成输入、`assert_allclose` 正确性校验、`do_bench` 封装 |
| [tilelang/autotuner/param.py](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/autotuner/param.py) | `CompileArgs`/`ProfileArgs`/`AutotuneResult`：编译/评测参数、结果落盘与加载 |
| [tilelang/jit/\_\_init\_\_.py](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/jit/__init__.py) | `@jit` 的 `wrapper`：从 kwargs 中 `pop("__tune_params")` —— autotuner 与 jit 的接缝 |
| [tilelang/jit/kernel.py](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/jit/kernel.py) | `JITKernel`：`get_profiler`/`update_tuner_result`/`get_tuner_result` |
| [tilelang/env.py](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/env.py) | `TILELANG_AUTO_TUNING_*` 并发控制、`CacheState` 缓存开关、`TILELANG_CACHE_DIR` |
| [examples/autotune/example_gemm_autotune.py](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/autotune/example_gemm_autotune.py) | 手动枚举/笛卡尔积配置空间的完整 GEMM 调参示例 |
| [examples/autotune/example_gemm_carver.py](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/autotune/example_gemm_carver.py) | 用 Carver 自动生成候选配置的示例 |

> 说明：本仓库里 autotuner 是 tile-lang 通用框架，**与后端无关**（CUDA/HIP/Ascend 都用同一套代码）。本讲在讲解时会标注它在 Ascend（`torch.npu`）路径上的具体行为。永久链接均指向当前 HEAD `ee60e122`。

## 4. 核心概念与源码讲解

本讲拆成五个最小模块：①入口与参数空间，②主流程（并行编译 + 串行评测），③capture 输入注入，④bench 计时与校验，⑤结果缓存与回填。

### 4.1 入口：@autotune 装饰器与参数空间

#### 4.1.1 概念说明

autotuner 的使用入口是装饰器 `@tilelang.autotune(configs=..., ...)`，它**总是和 `@tilelang.jit` 成对出现**，且 `@autotune` 在外、`@jit` 在内：

```python
@tilelang.autotune(configs=[...], ref_prog=..., supply_prog=..., rtol=1e-2, atol=1e-2)
@tilelang.jit(out_idx=[-1], pass_configs={...})
def matmul(M, N, K, block_M, block_N, K_L1, dtype="float16", accum_dtype="float"):
    ...
    return main   # 返回 @T.prim_func
```

这里的**关键设计**是：被装饰函数的参数被分成了两类。

- **固定参数**（`M, N, K`）：调用时由用户传入，决定算子的形状，每个候选配置都用同一组值。
- **可调参数**（`block_M, block_N, K_L1`）：调参时**不**由用户传入，而是由 autotuner 从 `configs` 里逐个填入。当用户**不**传这些可调参数时（如 `matmul(M, N, K)`），autotuner 触发完整搜索；当用户**显式传入**它们时（如 `matmul(M, N, K, block_M=128, block_N=128, K_L1=64)`），autotuner 会检测到「可调参数已被提供」而**跳过整轮调参**，直接按该配置编译一次——这是把调参结果快速回填、复用的快捷方式。

`configs` 可以是一个**列表**（每个元素是 `{"参数名": 值}` 的字典），也可以是一个**返回列表的可调用对象**（在调参开始时按当时的 `M/N/K` 现算空间）。两种写法在示例里都有：

#### 4.1.2 核心流程

参数空间的来源有三种典型写法，复杂度依次升高：

1. **手动枚举**：直接写几个字典。适合已经心里有数的少数候选。
2. **笛卡尔积**：用 `itertools.product` 对多组取值做全组合。空间大小是各维度取值数的乘积。
3. **Carver 自动生成**：用 `tilelang.carver.MatmulTemplate` + Ascend 架构描述，让框架根据硬件约束推荐 `topk` 个高质量候选（见 4.1.3 源码示例 `example_gemm_carver.py`）。

若用笛卡尔积，设第 \(i\) 个旋钮有 \(c_i\) 个取值，则总候选数为：

\[ N_{\text{configs}} = \prod_{i} c_i \]

例如 `block_M∈{64,128,256}`、`block_N∈{64,128,256}`、`K_L1∈{64,128}` 的笛卡尔积有 \(3\times3\times2=18\) 个候选。候选越多编译越久，需权衡。

调用阶段的判定逻辑（伪代码）：

```
func = matmul(M, N, K)              # 不传可调参数 → 触发搜索
# 或
func = matmul(M, N, K, 128, 128, 64) # 传了可调参数 → 跳过搜索，直接编译
out = func(a, b)                     # out 是最优 kernel 的输出
```

#### 4.1.3 源码精读

`@autotune` 是一个**带关键字参数**的装饰器工厂。它接受 `configs`（dict 或 Callable）、评测参数（`warmup/rep/timeout`）、校验参数（`ref_prog/supply_prog/rtol/atol/...`），返回一个 `decorator`，后者把 jit 产物包装成 `AutoTuneImpl`：

[example_gemm_autotune.py:54-62 — 装饰器堆叠与配置空间](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/autotune/example_gemm_autotune.py#L54-L62) — `@autotune` 在外、`@jit` 在内；`configs` 接收一个列表。

[example_gemm_autotune.py:27-42 — 两种配置空间写法](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/autotune/example_gemm_autotune.py#L27-L42) — `get_config()` 手动枚举 3 个候选；`get_config_combination()` 用 `itertools.product` 生成 18 个候选。

装饰器内部，`autotune(...)` 返回 `decorator`，`decorator` 从 jit 产物中取出 `jit_impl` 并构造 `AutoTuneImpl`：

[tuner.py:747-775 — decorator 把 jit_impl 包成 AutoTuneImpl](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/autotuner/tuner.py#L747-L775) — 注意它兼容两种入参：带 `__jit_impl__` 的 wrapper（`@autotune` 套 `@jit` 的产物）或直接的 `_JitImplementation` 实例。

[tuner.py:601-668 — AutoTuneImpl：装饰器返回的可调用对象](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/autotuner/tuner.py#L601-L668) — `__call__` 把 `(args, kwargs)` 作为缓存键；`jit_compile` 通过 `self.jit_impl.wrapper(*args, **kwargs, __tune_params=config_arg)` 把单个配置注入。

`@autotune` 与 `@jit` 之间的**接缝**就是那个特殊关键字参数 `__tune_params`。`@jit` 的 wrapper 在执行前会把它从 kwargs 里 `pop` 出来，再展开成普通关键字参数传给用户函数：

[jit/\_\_init\_\_.py:212-226 — wrapper 抽出 \_\_tune_params 并展开](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/jit/__init__.py#L212-L226) — 这就是「可调参数如何被填进用户函数」的真相：autotuner 调 `wrapper(..., __tune_params={"block_M":128,...})`，wrapper 把它展开成 `func(*args, **kwargs, block_M=128, ...)`。

至于「显式传参即跳过调参」的判定，在 `AutoTuner.run` 中实现，详见 4.2.3。

#### 4.1.4 代码实践

**实践目标**：理解「触发搜索 vs 跳过搜索」两种调用方式。

**操作步骤**：

1. 打开 `examples/autotune/example_gemm_autotune.py`，定位到末尾的调用。
2. 阅读第 95 行 `func = matmul(M, N, K)`（不传可调参数，触发搜索）。
3. 把它改成 `func = matmul(M, N, K, 128, 128, 64)`（显式传入，跳过搜索）。
4. 两种情况下分别运行 `python examples/autotune/example_gemm_autotune.py`。

**需要观察的现象**：触发搜索时会看到 tqdm 进度条 `Compiling configurations` 与 `Bench configurations`，并逐条打印 `Tuned Latency ... with config ...`；显式传参时这两段几乎不出现，直接打印 `Best Config`。

**预期结果**：显式传参的版本应该秒级返回（只编译 1 次），搜索版则需要编译并评测全部候选（耗时与候选数成正比）。**待本地验证**：精确耗时取决于机器与 NPU 型号。

#### 4.1.5 小练习与答案

**练习 1**：若把 `configs` 写成一个函数 `def get_config(): return [...]`，autotuner 何时调用它？
**答案**：在 `AutoTuner.run` 开头，若 `self.configs` 是 `Callable`，会用 `self.configs(*self._kernel_parameters)` 现算（见 [tuner.py:308-309](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/autotuner/tuner.py#L308-L309)）。这样空间可以依赖运行时的 `M/N/K`。

**练习 2**：候选字典里写了一个用户函数里**没有**的参数名，会发生什么？
**答案**：会抛 `ValueError("Unused keys in config: ...")`，见 [tuner.py:429-431](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/autotuner/tuner.py#L429-L431)。autotuner 只把「同时出现在函数签名和 config 里」的键挑出来传给用户函数。

### 4.2 主流程：并行编译 + 串行评测 + 最优选

#### 4.2.1 概念说明

`AutoTuner.run()` 是整个调参的核心。它把「印试卷（编译）」和「作答掐表（评测）」**显式拆成两段**，因为这两段对资源的需求完全不同：

- **编译**是 CPU 密集型（跑 lowering pass、生成 C++、调 bisheng 编 `.so`），且各候选之间互不依赖，可以**并行**。autotuner 用 `ThreadPoolExecutor` 并发编译。
- **评测**必须独占 NPU（一次只能跑一个 kernel 实例），且要刷 L2、warmup、掐表，因此**串行**执行，并对每个候选设一个 `timeout` 防止个别配置卡死拖累全局。

这个「编译并行、评测串行」的划分是 autotuner 设计上最值得记住的一点。

#### 4.2.2 核心流程

`AutoTuner.run` 的三段式伪代码：

```
1. 准备阶段
   - 解析函数签名 parameters
   - 若 configs 是 Callable，现算空间
   - 生成缓存 key（见 4.5）
   - 若命中内存/磁盘缓存 → 直接返回
   - 若「可调参数已被显式提供」→ 跳过搜索，只编译一次

2. 并行编译段
   - num_workers = 按 CPU 数/利用率计算（受 env 限制）
   - pool = ThreadPoolExecutor(num_workers)
   - 对每个 config：pool.submit(jit_compile, **config)   # NPU 下包一层 set_device
   - tqdm 收集成功的 (jit_kernel, config)，失败的记 debug 日志跳过

3. 串行评测段
   - for (jit_kernel, config) in results:
       latency, ref_latency = run_with_timeout(target_fn, timeout, jit_kernel)
       if latency < best_latency: 更新 best
       tqdm 打印本条 latency 与当前 best
   - 把 best_kernel 写回 latency/config/ref_latency
   - 落盘缓存（若开启）
   - 返回 AutotuneResult(best_latency, best_config, best_kernel, ...)
```

并发度由三个环境变量控制（[env.py:101-103](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/env.py#L101-L103)）：

| 环境变量 | 默认 | 含义 |
| --- | --- | --- |
| `TILELANG_AUTO_TUNING_CPU_COUNTS` | `-1` | `>0` 时直接指定 worker 数；`-1` 表示改用利用率比例 |
| `TILELANG_AUTO_TUNING_CPU_UTILITIES` | `0.9` | worker 数 = `可用 CPU 数 × 0.9` |
| `TILELANG_AUTO_TUNING_MAX_CPU_COUNT` | `-1` | worker 数硬上限；`-1` 表示不限 |

`timeout` 用 `signal.SIGALRM` 实现（仅 Unix 主线程有效），超时的候选记 warning 并跳过，不会让整轮调参挂死。

#### 4.2.3 源码精读

**并行编译段**——先算 worker 数，再用线程池提交所有编译任务：

[tuner.py:463-480 — 并发度计算](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/autotuner/tuner.py#L463-L480) — `cpu_counts>0` 直接用；否则用 `利用率×可用CPU`；`max_cpu_count` 兜底。

[tuner.py:482-528 — ThreadPoolExecutor 并行编译](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/autotuner/tuner.py#L482-L528) — 关键点：NPU 可用时用 `npu_device_wrapper` 把 `torch.npu.set_device(device)` 包进每个编译任务（[tuner.py:494-499](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/autotuner/tuner.py#L494-L499)），保证多线程编译都指向同一块 NPU；编译失败的候选 `continue` 跳过。

**串行评测段**——注意它**不在**线程池里跑，而是普通 for 循环加 `run_with_timeout`：

[tuner.py:530-553 — 串行评测 + 取最优](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/autotuner/tuner.py#L530-L553) — 注释明说「不能用 ThreadPoolExecutor 给 target_fn 套 timeout，因为 tma init 在单线程下行为异常」（[tuner.py:535-537](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/autotuner/tuner.py#L535-L537)）；超时与异常都 catch 掉，只 warning 不中断。

**评测单元 `target_fn`**——拿到一个 `JITKernel`，生成/复用输入、做正确性校验、再计时：

[tuner.py:341-420 — target_fn：校验 + 计时](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/autotuner/tuner.py#L341-L420) — `(not skip_check) and ref_prog is not None` 时调 `profiler.assert_allclose`（[tuner.py:407-413](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/autotuner/tuner.py#L407-L413)）；随后 `profiler.do_bench(...)` 拿到 latency（[tuner.py:414](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/autotuner/tuner.py#L414)）；`ref_latency` 只在首次测一次用于横向对比。

**「显式传参即跳过」判定**——在评测段之前：

[tuner.py:441-461 — 检测到可调参数已提供则跳过搜索](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/autotuner/tuner.py#L441-L461) — 只编译一次（`self.jit_compile()`，不带 config），把产物直接当结果，**不计时、不比较**。这印证了 4.1 说的快捷回填用法。

**收尾**——把最优 kernel 的 latency/config/ref_latency 写回，组装 `AutotuneResult`：

[tuner.py:562-586 — 写回最优并落盘缓存](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/autotuner/tuner.py#L562-L586) — `best_kernel.update_tuner_result(...)`；若全部候选都失败则 `raise RuntimeError`。

#### 4.2.4 代码实践

**实践目标**：亲手观察「并行编译、串行评测」。

**操作步骤**：

1. 运行 `TILELANG_AUTO_TUNING_CPU_COUNTS=2 python examples/autotune/example_gemm_autotune.py --m 1024 --n 1024 --k 1024`。
2. 观察第一段进度条 `Compiling configurations: N`（N=候选数，这里是 3）。
3. 观察第二段进度条 `Bench configurations: N`，以及每条 `Tuned Latency ... ms with config ...`。
4. 把 `get_config_combination()` 替换进 `configs=get_config_combination()`（18 个候选），再跑一次，对比总耗时与候选数的关系。

**需要观察的现象**：编译段多条日志几乎同时出现（并发），评测段严格一条接一条（串行）；终端右侧 `best_latency` 会单调下降到最优值。

**预期结果**：候选数从 3 → 18，总耗时应大致线性增长（编译约占大头）。**待本地验证**：精确数字依赖 NPU 与 CPU。

#### 4.2.5 小练习与答案

**练习 1**：为什么编译用线程池（`ThreadPoolExecutor`）而不是进程池？
**答案**：因为 tile-lang 的 JIT 编译产物（`JITKernel`、`.so` 句柄）需要在主进程里被后续评测与调用复用；线程池共享内存空间，编译完的 kernel 对象可以直接传回。评测段更不能用进程池——NPU 上下文不能跨进程共享。

**练习 2**：某个候选编译失败会怎样？某个候选评测超时会怎样？
**答案**：编译失败——`future.result()` 抛异常被 catch，记 debug 日志后 `continue`，该候选不进入评测段（[tuner.py:524-528](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/autotuner/tuner.py#L524-L528)）。评测超时——`run_with_timeout` 抛 `TimeoutException`，记 warning 后 `continue`（[tuner.py:539-541](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/autotuner/tuner.py#L539-L541)）。两种都不会中断整轮调参；只有**所有**候选都失败时才 `raise RuntimeError`。

### 4.3 capture：输入张量注入机制

#### 4.3.1 概念说明

评测每个候选时，autotuner 需要**喂给 kernel 一组输入张量**。最简单的办法是写一个 `supply_prog(params)`，返回输入张量列表（如示例里的 `supply_prog`）。但在某些场景（比如输入要从一个真实模型里取、或形状很特殊），你更希望**在调用现场把已经准备好的张量直接交出去**，而不是让框架随机生成。

`autotuner.capture` 提供了一个轻量的**线程局部栈**机制来做这件事：用 `with tilelang.autotuner.set_autotune_inputs(a, b):` 包住调用，框架在评测时就会用栈顶这组张量，**忽略** `supply_prog`。它用线程局部变量（`threading.local`）存放，避免多线程编译时互相串扰。

#### 4.3.2 核心流程

```
# 用户侧
a = torch.randn(M, K).half().npu()
b = torch.randn(K, N).half().npu()
with tilelang.autotuner.set_autotune_inputs(a, b):
    func = matmul(M, N, K)   # 内部评测时会用 (a, b) 作为输入

# 框架侧（set_profile_args 里）
if get_autotune_inputs() is not None:
    supply_prog = lambda _: get_autotune_inputs()   # 取栈顶张量
```

栈结构保证可嵌套：内层 `with` 压栈、退出时弹栈，外层逻辑看到的还是外层的张量。

#### 4.3.3 源码精读

[capture.py:7-84 — 线程局部 CaptureStack](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/autotuner/capture.py#L7-L84) — `_local = threading.local()`，每个线程一份栈；`_get_current_stack()` 懒初始化。

[capture.py:87-98 — AutotuneInputsCapture 上下文管理器](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/autotuner/capture.py#L87-L98) — `__enter__` 压栈、`__exit__` 弹栈。

[capture.py:101-119 — set_autotune_inputs 接受两种参数风格](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/autotuner/capture.py#L101-L119) — 既支持 `set_autotune_inputs(a, b, c)`，也支持 `set_autotune_inputs([a, b, c])`。

[capture.py:122-127 — get_autotune_inputs 取栈顶](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/autotuner/capture.py#L122-L127) — 栈空返回 `None`。

autotuner 消费它的地方在 `set_profile_args`：

[tuner.py:217-222 — 在 set_autotune_inputs 上下文里忽略 supply_prog](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/autotuner/tuner.py#L217-L222) — 若处于捕获上下文，把 `supply_prog` 替换成「返回栈顶张量」的 lambda，并 warning 提示用户传入的 `supply_prog` 被忽略。

#### 4.3.4 代码实践

**实践目标**：用 `set_autotune_inputs` 取代 `supply_prog`。

**操作步骤**：

1. 复制 `example_gemm_autotune.py`，删除装饰器里的 `supply_prog=supply_prog`。
2. 在调用前准备好 `a, b`，用 `with tilelang.autotuner.set_autotune_inputs(a, b):` 包住 `func = matmul(M, N, K)`。
3. 运行并确认日志里出现 `supply_prog will be ignored as this program is under with set_autotune_inputs context.`。

**需要观察的现象**：终端出现上述 warning，且评测段正常完成、`Best Config` 正常打印。

**预期结果**：与使用 `supply_prog` 时结果一致（输入张量相同）。**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：为什么用 `threading.local()` 而不是普通全局变量？
**答案**：因为编译段是 `ThreadPoolExecutor` 多线程并发（见 4.2）。若用全局变量，多个线程的 `set_autotune_inputs` 会互相覆盖；线程局部变量保证每个编译/评测线程看到自己的栈，互不串扰。

**练习 2**：`set_autotune_inputs` 和 `supply_prog` 同时存在时，哪个生效？
**答案**：`set_autotune_inputs` 生效，`supply_prog` 被忽略并 warning（[tuner.py:219-222](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/autotuner/tuner.py#L219-L222)）。

### 4.4 bench：NPU 计时与正确性校验

#### 4.4.1 概念说明

评测一个候选要做两件事：**确认它算得对**、**测量它跑得多快**。

- **算得对**：把 kernel 输出和 `ref_prog`（参考实现，如 PyTorch 的 `A @ B`）逐元素比较，允许 `rtol/atol` 容差与 `max_mismatched_ratio` 的不一致比例。校验不通过的候选等价于编译失败，会被跳过。
- **跑得多快**：用 `do_bench` 计时。NPU 上计时的难点是 **L2 cache 会缓存上一次的输入**，让本次看起来特别快。`do_bench` 在每次正式计时前先用一块 256MB 的 buffer 把 L2 冲掉，保证每次测量起点一致；再按估算的耗时自动算出 warmup/repeat 次数，用 `torch.npu.Event` 掐表。

#### 4.4.2 核心流程

`Profiler.do_bench`（对外封装）的流程：

```
ins = input_tensors or self._get_inputs()   # 取/生成输入
bench_func = partial(adapter, *ins)          # 绑定输入
return do_bench(bench_func, warmup=..., rep=..., _n_warmup=..., _n_repeat=...)
```

底层 `do_bench`（`bench.py`）的流程：

```
1. fn() 一次 + synchronize，确认能跑
2. 分配 256MB cache buffer（fast_flush 用 int，否则 int8）
3. 跑 5 次「cache.zero_() + fn()」估算 estimate_ms
4. n_warmup = max(1, warmup/estimate_ms)，n_repeat = max(1, rep/estimate_ms)
5. warmup n_warmup 次
6. 正式计时 n_repeat 次：每次 cache.zero_() → start_event.record() → fn() → end_event.record()
7. synchronize，对 (start,end) elapsed_time 求均值/中位/min/max 返回
```

#### 4.4.3 源码精读

[profiler/bench.py:9-52 — do_bench 签名与 256MB L2 刷缓存](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/profiler/bench.py#L9-L52) — `fn()` 先跑一次验证；`fast_flush=True` 时用 `torch.empty(256e6//4, dtype=int)`（即 256MB）在 NPU 上 `zero_()` 来冲 L2。注意设备写死 `device="npu"`，这是 Ascend 专用计时路径。

[profiler/bench.py:54-72 — 自动估算 warmup/repeat 次数](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/profiler/bench.py#L54-L72) — `estimate_ms` 由 5 次平均得到；`_n_warmup/_n_repeat` 可强制覆盖。

[profiler/bench.py:74-102 — 正式计时与聚合](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/profiler/bench.py#L74-L102) — 每轮先 `cache.zero_()` 再掐表；`return_mode` 默认 `"mean"`，也支持 `min/max/median` 与分位数。

`Profiler` 对它的封装（torch 路径，Ascend 走这里）：

[profiler/\_\_init\_\_.py:228-264 — Profiler.do_bench 的 torch 分支](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/profiler/__init__.py#L228-L264) — `func is None` 时用 `self.adapter` 作为被测对象，`partial(func, *ins)` 绑定输入，转交底层 `do_bench`。

正确性校验：

[profiler/\_\_init\_\_.py:89-149 — assert_allclose：与 ref_prog 逐张量比对](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/profiler/__init__.py#L89-L149) — 先跑 `ref_prog(*ins)`、再跑 `self.func(*ins)`，两边都 `torch.npu.synchronize()`，再用 `torch_assert_close` 比 `ins + outs`，容差由 `rtol/atol/max_mismatched_ratio` 控制。

[profiler/\_\_init\_\_.py:75-80 — _get_inputs 按需生成输入](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/profiler/__init__.py#L75-L80) — 跳过输出张量（`result_idx`）与 workspace 张量（`workspace_idx`），对剩余参数用 `self.supply(param)` 生成数据；`supply` 由 `tensor_supply_type` 决定（如 `Integer`/`Normal`/`Auto`）。

#### 4.4.4 代码实践

**实践目标**：理解 `ref_prog`/容差与计时稳定性。

**操作步骤**：

1. 在 `example_gemm_autotune.py` 里，把 `atol=1e-2, rtol=1e-2` 改成 `atol=1e-6, rtol=1e-6`（极严）。
2. 运行，观察是否仍有候选能通过校验。
3. 把 `return_mode`（需直接用 `AutoTuner`/`Profiler` 时才能设）相关的统计改为更长 `rep`（如装饰器加 `rep=200`），观察最优 latency 是否更稳定。

**需要观察的现象**：容差过严时，部分候选可能因数值不一致被 `assert_allclose` 判失败（fp16 GEMM 在大 K 下累加误差通常在 1e-2~1e-3 量级）。

**预期结果**：`atol/rtol=1e-2` 能通过；改成 `1e-6` 大概率有候选甚至全部失败。**待本地验证**：fp16 累加误差与具体形状有关。

#### 4.4.5 小练习与答案

**练习 1**：为什么每次正式计时前要 `cache.zero_()`？
**答案**：为了**冲掉 L2 cache 里残留的上一次输入**，避免本次 kernel 因命中缓存而显得异常快，保证各候选、各次测量的起点一致（[bench.py:85-86](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/profiler/bench.py#L85-L86)）。

**练习 2**：`return_mode="min"` 和 `"mean"` 哪个更适合选最优配置？
**答案**：`"min"` 反映该配置的**最好一次**表现（噪声下限），常用于挑配置；`"mean"` 反映**典型**表现。autotuner 默认用 `"mean"`（见 [bench.py:18](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/profiler/bench.py#L18)），更稳健地反映真实部署耗时。两种都可按需切换。

### 4.5 结果缓存与回填：从 AutotuneResult 到可复用 kernel

#### 4.5.1 概念说明

调参很贵（编译 + 上板），所以 autotuner 做了**两级缓存**来避免重复劳动：

1. **内存缓存**：进程内一个 dict，key 相同则直接返回上次的 `AutotuneResult`。
2. **磁盘缓存**：在 `~/.tilelang/cache/autotuner/<key>/` 下落盘一组文件（最优配置、kernel 源码、`.so`、参数等），下次进程启动也能命中。

缓存键（`key`）是 SHA256，综合了：tile-lang 版本、函数源码、`configs`、`CompileArgs`/`ProfileArgs` 的哈希、以及函数默认参数。**任何一项变了，key 就变**，从而安全地失效。

调参完成后，最优 kernel 可以**直接当函数调用**（`result.kernel(a, b)` 或装饰器返回的 `func(a, b)`），它的 `latency/config/ref_latency` 已通过 `update_tuner_result` 写回，用 `get_tuner_result()` 可查。

#### 4.5.2 核心流程

```
run() 开头：
  key = SHA256(version, func_source, configs, hash(compile_args), hash(profile_args), defaults)
  if 缓存开启:
      if key in 内存缓存: return 内存缓存[key]
      result = 从磁盘加载(key)
      if result: 回填内存缓存; return result
  ... 执行搜索 ...
  把 best 落盘（若缓存开启且后端不是 dlpack）
  存入内存缓存
  return AutotuneResult
```

磁盘上每个 key 目录包含的文件（[param.py:23-30](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/autotuner/param.py#L23-L30)）：

| 文件 | 内容 |
| --- | --- |
| `best_config.json` | 最优配置字典 |
| `function.pkl` | cloudpickle 序列化的 `@T.prim_func` |
| `latency.json` | `{"latency":..., "ref_latency":...}` |
| `kernel.cu` / `wrapped_kernel.cu` | 设备 kernel 源码 / 包装后源码 |
| `kernel_lib.so` | bisheng 编译产物 |
| `params.pkl` | kernel 参数（`KernelParam` 列表） |
| `auto_gm_idx.pkl` | workspace 消除自动分配的索引（见 [u5-l4](u5-l4-workspace-reduction.md)） |

#### 4.5.3 源码精读

**缓存键生成**：

[tuner.py:251-283 — generate_cache_key](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/autotuner/tuner.py#L251-L283) — `_normalize_param` 把 TVM `Var` 等转成可序列化值；`key_data` 含 `version/func_source/configs/compile_args/profile_args` 的哈希；`json.dumps(sort_keys=True)` 后 SHA256。

[tuner.py:313-328 — 内存优先、磁盘其次](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/autotuner/tuner.py#L313-L328) — 两级查找；命中内存缓存时还会 warning 建议「改用 `@tilelang.autotune` 装饰器以获更好性能」。

**CompileArgs 的哈希**——注意它把 `"auto"` 平台解析成具体 `A2/A3/A5`，让缓存按具体平台分区：

[param.py:71-85 — CompileArgs.\_\_hash\_\_](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/autotuner/param.py#L71-L85) — `platform` 用 `determine_platform(self.platform)` 解析；`pass_configs`/`compile_flags` 用 `json.dumps(sort_keys=True)` 归一化。

**结果落盘/加载**：

[param.py:317-346 — AutotuneResult.save_to_disk](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/autotuner/param.py#L317-L346) — 写 `best_config.json`/`function.pkl`/`latency.json`，并委托 `_save_kernel_to_disk` 存 kernel。

[param.py:158-225 — _save_kernel_to_disk 存源码/.so/params/auto_gm_idx](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/autotuner/param.py#L158-L225) — `.so` 用 `shutil.copy` 从 `kernel.adapter.libpath` 拷过来；`auto_gm_idx` 是 workspace 消除运行时分配所需的索引。

[param.py:348-400 — load_from_disk 重建 JITKernel](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/autotuner/param.py#L348-L400) — 读回各文件，用 `JITKernel.from_database` 重建可调用 kernel，并 `update_tuner_result` 写回 latency/config。

**回填到可调用对象**：

[kernel.py:400-421 — JITKernel.update_tuner_result](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/jit/kernel.py#L400-L421) — 把 `latency/config/ref_latency` 挂到 kernel 实例上。

[kernel.py:423-442 — JITKernel.get_tuner_result](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/jit/kernel.py#L423-L442) — 返回 `{"latency", "config", "ref_latency"}` 字典；未调参过则抛 `ValueError`。示例里 `func.get_tuner_result()` 用的就是它（[example_gemm_autotune.py:97](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/autotune/example_gemm_autotune.py#L97)）。

**缓存开关**：

[env.py:191-215 — CacheState 全局缓存开关](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/env.py#L191-L215) — `enable_cache`/`disable_cache`/`is_cache_enabled`；示例开头 `tilelang.cache.clear_cache()`（[example_gemm_autotune.py:8](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/autotune/example_gemm_autotune.py#L8)）则用于**强制重算**。

#### 4.5.4 代码实践

**实践目标**：体验磁盘缓存的命中与失效。

**操作步骤**：

1. 删除缓存目录：`rm -rf ~/.tilelang/cache/autotuner`。
2. 运行 `python examples/autotune/example_gemm_autotune.py`，记录耗时 `T1`。
3. **不改任何代码**，再次运行，记录耗时 `T2`。
4. 进 `~/.tilelang/cache/autotuner/<某个 sha256 目录>/`，`cat best_config.json` 查看最优配置。
5. 把示例开头的 `tilelang.cache.clear_cache()` 注释掉再运行，对比行为。

**需要观察的现象**：第二次运行（`T2`）应远快于第一次（`T1`），因为命中磁盘缓存，跳过了全部编译与评测；终端看不到 `Compiling/Bench configurations` 进度条。`best_config.json` 里是最优的 `block_M/block_N/K_L1`。

**预期结果**：`T2 ≪ T1`（通常 T2 为秒级，T1 与候选数成正比）。**待本地验证**。

#### 4.5.5 小练习与答案

**练习 1**：改了 kernel 函数体里的一行注释，缓存会失效吗？
**答案**：**会**。因为缓存键包含 `inspect.getsource(self.fn)`（[tuner.py:272](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/autotuner/tuner.py#L272)），源码字符串一变 SHA256 就变，旧缓存不会被命中，会重新调参。

**练习 2**：`execution_backend="dlpack"` 时为什么不能落盘缓存？
**答案**：dlpack 后端的 kernel 句柄是进程内对象、不产生独立 `.so` 文件，没有可持久化的库（[tuner.py:577-579](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/autotuner/tuner.py#L577-L579)），故只做内存缓存、跳过磁盘。Ascend 默认走 `cython` 后端，磁盘缓存可用。

## 5. 综合实践

把本讲五个最小模块串起来，完成一次**完整的 GEMM 自动调参闭环**。

**任务**：为 Ascend 上的 fp16 GEMM（`C = A @ B`，`M=N=K=1024`）定义一个调参空间，跑一次 autotuner，记录最优配置与时延，并验证缓存命中。

**建议步骤**：

1. 以 `examples/autotune/example_gemm_autotune.py` 为模板。它已经把 Ascend 三件套 pass 配置开好了（[example_gemm_autotune.py:20-24](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/autotune/example_gemm_autotune.py#L20-L24)）：
   ```python
   pass_configs = {
       tilelang.PassConfigKey.TL_ASCEND_AUTO_CV_COMBINE: True,
       tilelang.PassConfigKey.TL_ASCEND_AUTO_SYNC: True,
       tilelang.PassConfigKey.TL_ASCEND_MEMORY_PLANNING: True,
   }
   ```
2. 定义搜索空间（笛卡尔积）：`block_M∈{64,128}`、`block_N∈{128,256}`、`K_L1∈{64,128}`，共 8 个候选。注意 Ascend 上 block 维度需 16 对齐、`K_L1` 需整除 K（见 [u1-l4](u1-l4-first-gemm.md)）。
3. 装饰器堆叠 `@tilelang.autotune(configs=..., ref_prog=lambda A,B: A@B, supply_prog=..., atol=1e-2, rtol=1e-2)` 套 `@tilelang.jit(out_idx=[-1], pass_configs=pass_configs)`。
4. `clear_cache()` 后首次运行，记录最优 `block_M/block_N/K_L1` 与 `latency`、`ref_latency`。
5. 不改代码再跑一次，确认命中磁盘缓存（秒回）。
6. **进阶**：换用 `examples/autotune/example_gemm_carver.py` 的 Carver 路径（[example_gemm_carver.py:29-50](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/autotune/example_gemm_carver.py#L29-L50)），让框架按 Ascend 架构推荐 `topk=20` 个候选，对比手动笛卡尔积与 Carver 推荐的最优时延差异。

**交付物**：一张表，列出至少 3 个候选的 `latency`、最优配置、以及缓存命中前后的运行耗时对比。无法在真实 NPU 上运行时，标注「待本地验证」并说明预期趋势。

## 6. 本讲小结

- `@tilelang.autotune` 与 `@tilelang.jit` **成对使用**（autotune 在外），通过 `__tune_params` 这个特殊 kwarg 把候选配置注入用户函数；显式传入可调参数则**跳过搜索**直接编译。
- `AutoTuner.run` 是「**并行编译 + 串行评测 + 取最优**」三段式：编译用 `ThreadPoolExecutor`（CPU 密集、可并行、NPU 下包 `set_device`），评测必须串行（单 NPU）并用 `run_with_timeout`（SIGALRM）防卡死。
- `autotuner.capture` 用**线程局部栈**实现 `set_autotune_inputs`，让你在调用现场直接交出输入张量，覆盖 `supply_prog`。
- `profiler.bench.do_bench` 用 **256MB buffer 冲 L2** + 自动 warmup/rep + `torch.npu.Event` 计时；`assert_allclose` 用 `rtol/atol/max_mismatched_ratio` 做**正确性校验**，不过的候选被当作失败跳过。
- 结果有**内存 + 磁盘两级缓存**，键是综合版本/源码/configs/参数的 SHA256；最优 kernel 经 `update_tuner_result` 写回，`get_tuner_result()` 可查，`result.kernel(a,b)` 可直接调用。
- 并发度由 `TILELANG_AUTO_TUNING_CPU_COUNTS/_UTILITIES/_MAX_CPU_COUNT` 三个环境变量控制；`tilelang.cache.clear_cache()` 强制重算。

## 7. 下一步学习建议

- **Carver 深入**：本讲只示范了 `carver.MatmulTemplate` 的调用，建议阅读 `tilelang/carver/` 下的 Ascend 架构描述与 `recommend_hints` 实现，理解它如何根据硬件约束（L1/UB 容量、Mmad 分形）生成高质量候选，从而替代盲目的笛卡尔积。
- **结合 pass 调参**：可调参数不止 `block_M/block_N/K_L1`，还可以把 `num_stages`（[u3-l6](u3-l6-pipelined.md)）、`threads`（[u5-l3](u5-l3-vid-reduction.md)）、`TL_ASCEND_*` 开关纳入搜索空间，做联合调参。
- **AOT 与部署**：调参得到最优 `.so` 后，参考 [u7-l3](u7-l3-torch-aclgraph.md) 把它 AOT 导出并接入 PyTorch / ACLGraph，把「调参产物」变成「可部署算子」。
- **性能归因**：autotuner 只告诉你「哪个最快」，若要理解「为什么这个最快」，结合 [u7-l4](u7-l4-debug-profiling.md) 的 `msprof op` 采集各候选的算力/带宽利用率，把调参结果与性能模型对应起来。
