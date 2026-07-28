# 自动调优 autotuner

## 1. 本讲目标

本讲讲解 TileLang 的自动调优器（autotuner）。一个 GEMM kernel 的性能高度依赖 `block_M/block_N/block_K`、`num_stages`、`threads` 等参数，手动逐一编译、运行、量延迟既繁琐又容易漏掉最优配置。autotuner 把「定义候选空间 → 并发编译 → 精确量延迟 → 选最优 → 落盘缓存」自动化。

学完后你应当能够：

- 说出 autotuner 的三层职责（参数空间 / 编译 / 评测）与主流程。
- 用 `AutoTuner.from_kernel(...)` 或 `@tilelang.autotune` 两种方式驱动调优。
- 理解 `CompileArgs` / `ProfileArgs` / `AutotuneResult` 三个数据类的作用。
- 解释 `set_autotune_inputs` 的输入捕获机制，以及为什么它用线程局部栈。
- 说出「分组编译（grouped compile）」为什么能把多个配置合并成一次设备编译，以及它的适用条件。
- 读懂缓存键（cache key）由哪些因素决定，以及如何用环境变量控制并发与缓存。

## 2. 前置知识

在进入本讲前，确保你已理解以下概念（均在前序讲义中讲过）：

- **JIT 与 `JITKernel`**（u3-l2）：`@tilelang.jit` 把函数包成 `JITImpl`，调用 `.compile()` 后产出可直接接受 torch 张量的 `JITKernel`。autotuner 本质上是「反复对 `JITKernel` 做 compile + benchmark」。
- **target 与执行后端**（u3-l1、u3-l3）：`target` 回答「编给谁」（如 `cuda`/`maca`），执行后端回答「怎么跑」（如 `tvm_ffi`/`nvrtc`/`mcrtc`）。二者**正交**，但分组编译只在 `cuda` + `tvm_ffi` 组合下生效。
- **GEMM 的可调参数**（u6-l3）：`block_M/N/K` 是 tile 尺寸，`num_stages` 控制软件流水线缓冲份数，`threads` 是每块线程数。
- **profiler**（u6-l3）：`JITKernel.get_profiler().do_bench(...)` 用带 L2 冲刷的方式精确量延迟，支持 `event`/`cupti`/`cudagraph` 三种后端。
- **基础 Python 知识**：`dataclass`、`concurrent.futures.ThreadPoolExecutor`、闭包（`__closure__`/`co_freevars`）、线程局部变量（`threading.local()`）。

一个直观比喻：autotuner 像一个「网格搜索 + 编译器」。你告诉它「这些参数可以取这些值」，它帮你把每组取值代入 kernel、编译、量延迟，最后把最快的那组连同编译产物一起缓存下来。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [tilelang/autotuner/__init__.py](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/autotuner/__init__.py) | 模块导出：`autotune`、`AutoTuner`、`set_autotune_inputs`、`get_autotune_inputs` |
| [tilelang/autotuner/tuner.py](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/autotuner/tuner.py) | 核心编排：`AutoTuner` 类（命令式 API）、`AutoTuneImpl` 与 `autotune` 装饰器（声明式 API） |
| [tilelang/autotuner/param.py](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/autotuner/param.py) | 三个数据类：`CompileArgs`、`ProfileArgs`、`AutotuneResult`（含磁盘缓存读写） |
| [tilelang/autotuner/capture.py](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/autotuner/capture.py) | 输入捕获栈：`set_autotune_inputs` / `get_autotune_inputs` |
| [tilelang/autotuner/grouped_compile.py](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/autotuner/grouped_compile.py) | 分组编译：把多个 PrimFunc 的设备 IR 合并成一次 `device_codegen` |
| [tilelang/autotuner/param.py](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/autotuner/param.py) | 缓存键构造 `generate_cache_key`（实现在 tuner.py）与磁盘原子落盘 |
| [examples/gemm/example_gemm_autotune.py](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/examples/gemm/example_gemm_autotune.py) | 端到端示例：定义候选空间、调用 `AutoTuner`、打印最优配置 |
| [docs/tutorials/auto_tuning.md](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/docs/tutorials/auto_tuning.md) | 官方文档：三步法（留参 → 生成候选 → 编译评测） |

## 4. 核心概念与源码讲解

### 4.1 tuner：AutoTuner 的整体架构与主流程

#### 4.1.1 概念说明

`AutoTuner` 是 autotuner 的「命令式」入口：你拿到一个 `AutoTuner` 对象，依次调用 `.set_compile_args()`、`.set_profile_args()`、`.run()`，采用方法链（method chaining，每个 setter 返回 `self`）。整个调优过程分为三大阶段：

1. **准备候选**：把 `configs`（一组字典）与 kernel 函数的参数签名对齐，得到「给每个候选配置代入哪些关键字参数」的列表 `config_args`。
2. **并发编译**：用线程池把每个配置编译成一个 `JITKernel`，编译失败的配置被记录但不会中断整体。
3. **并发评测**：每编译出一个 kernel 就喂给 benchmark worker 线程量延迟，主线程汇总结果，记下「最低延迟 → 最优配置 → 最优 kernel」。

此外，`autotuner.py` 末尾还有一套「声明式」API：`@tilelang.autotune(configs=...)` 装饰器。它必须叠在 `@tilelang.jit` 之上，内部把工作委托给 `AutoTuneImpl`（一个 `dataclass`，`__call__` 时才真正调优）。

#### 4.1.2 核心流程

下面是 `run()` 的伪代码（省略缓存与边界处理）：

```
run(warmup, rep, timeout):
    1. 用 inspect.signature 拿到 kernel 的参数列表 parameters
    2. 提取 kernel 闭包里的自由变量（如外层 M/N/K），纳入 extra_parameters
       —— 这是缓存键正确性的关键：闭包变量是符号化的，必须把具体值固化进 key
    3. 计算 cache key，先查内存缓存、再查磁盘缓存；命中则直接返回
    4. 把每个 config 字典与 parameters 对齐，过滤出 kernel 真正接受的键 → config_args
    5. 解析「分组编译」是否生效（仅 cuda + tvm_ffi）与并发 worker 数
    6. 解析 benchmark 用的设备列表（单卡 / 多卡）
    7. 把 config_args 切成编译单元，提交到线程池并发编译
    8. 主线程循环：每有一个编译完成 → 喂给 benchmark 队列 → 收 benchmark 结果
       每收到一个延迟 latency，若 latency < best_latency 则更新 best
    9. 全部完成后，用 best_kernel.update_tuner_result(...) 注入最优信息
    10. 组装 AutotuneResult，写磁盘缓存 + 内存缓存，返回
```

第 4 步的「对齐」很关键：一个 config 字典里允许有 kernel 签名中**不存在**的键吗？不行——源码会抛出 `Unused keys` 错误。但 kernel 签名里有、config 里没给的键会被跳过（用默认值 `None`）。

#### 4.1.3 源码精读

`AutoTuner` 类的定义与构造：

[tuner.py:226-252](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/autotuner/tuner.py#L226-L252) — `AutoTuner` 类。`compile_args` 与 `profile_args` 是类级默认实例（`CompileArgs()` / `ProfileArgs()`），`_lock`、`_memory_cache` 也是类级共享的，所以多个 `AutoTuner` 实例共用同一份内存缓存表。

最常用的构造入口 `from_kernel`：

[tuner.py:264-275](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/autotuner/tuner.py#L264-L275) — 工厂方法，等价于 `cls(kernel, configs)`。

`run()` 主流程里，把 config 字典与参数签名对齐、并检测「未使用的键」：

[tuner.py:968-981](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/autotuner/tuner.py#L968-L981) — 遍历每个 config，只保留 kernel 签名里存在的键；若 config 里出现了 kernel 不认识的键，抛 `ValueError`。这是防止「拼错参数名」的护栏。

主线程「编译完成即喂 benchmark」的流水线循环：

[tuner.py:1135-1167](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/autotuner/tuner.py#L1135-L1167) — 用 `concurrent.futures.wait(..., FIRST_COMPLETED)` 增量取出已完成编译，把成功的 kernel 投递进 benchmark 队列（`_enqueue_benchmark_task`），同时非阻塞地收割 benchmark 结果（`_drain_benchmark_results`）。这套「编译线程池 + benchmark 线程」的解耦让编译与测速可以重叠。

最终把最优信息写回 kernel 并组装结果：

[tuner.py:1188-1217](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/autotuner/tuner.py#L1188-L1217) — 若 `best_kernel is None`（没有任何配置编译+评测成功），抛 `RuntimeError`。否则调用 `best_kernel.update_tuner_result(latency=, config=, ref_latency=)` 注入最优数据，组装 `AutotuneResult` 并写盘。

声明式 API：装饰器 `autotune` 与 `AutoTuneImpl`：

[tuner.py:1349-1446](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/autotuner/tuner.py#L1349-L1446) — `@tilelang.autotune(configs=...)` 实际返回一个装饰器，它要求被装饰对象必须是 `JITImpl`（即必须先 `@tilelang.jit`），否则断言失败；返回的是 `AutoTuneImpl` 实例而非原函数。

`AutoTuneImpl.__call__` 里区分 lazy / eager 两种 JIT 模式，并缓存「(args, kwargs) → (best_kernel, best_config)」：

[tuner.py:1280-1343](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/autotuner/tuner.py#L1280-L1343) — 用 `_normalize_value` 把参数（含 torch 张量、Var）归一化为可哈希的 key；同一组调用参数只调优一次。`do_not_specialize` 列出的参数会被排除出 key，这样改变它们不会触发重新调优。

#### 4.1.4 代码实践

**实践目标**：用命令式 API 跑通一次 GEMM 调优，观察进度条与最优配置输出。

**操作步骤**：

1. 打开 `examples/gemm/example_gemm_autotune.py`，定位到 `get_best_config`（[example_gemm_autotune.py:110-168](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/examples/gemm/example_gemm_autotune.py#L110-L168)）。
2. 在有 CUDA GPU 的机器上执行：
   ```bash
   python examples/gemm/example_gemm_autotune.py --m 1024 --n 1024 --k 1024 --use_autotune
   ```
3. 观察终端：会先出现 `Compiling configurations` 进度条，再出现 `Bench configurations` 进度条，并逐条打印 `Tuned Latency ... with config ... at index ...`。
4. 运行结束后打印 `result.config`（最优配置字典）。

**需要观察的现象**：不同配置的延迟差异可能很大（数倍）；`best_latency` 会随进度条 `postfix` 不断被刷新为更小值。

**预期结果**：终端最终打印形如 `{'block_M': 128, 'block_N': 128, 'block_K': 64, 'num_stages': 2, 'thread_num': 128, 'enable_rasteration': True}` 的最优配置。**若没有 GPU，此命令无法完成评测，待本地验证。**

#### 4.1.5 小练习与答案

**练习 1**：为什么 `AutoTuner` 的 `run()` 在第 2 步要手动提取闭包变量（`__closure__` / `co_freevars`）放进 `extra_parameters`？

**答案**：缓存键里包含 `inspect.getsource(self.fn)`（函数源码），但源码里的 `M/N/K` 是符号名，不同形状（1024 vs 4096）的源码文本完全一样。若不把闭包里的具体数值固化进 key，形状变化会被误判为「缓存命中」，返回错误的旧结果。

**练习 2**：`@tilelang.autotune` 能否单独使用（不叠 `@tilelang.jit`）？

**答案**：不能。装饰器内部 `assert isinstance(impl, JITImpl)`（[tuner.py:1427](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/autotuner/tuner.py#L1427)），要求被装饰对象已是 `JITImpl`，所以必须先 `@tilelang.jit`。

### 4.2 param 空间：CompileArgs / ProfileArgs / AutotuneResult 与缓存键

#### 4.2.1 概念说明

`param.py` 定义三个不可变数据类（`@dataclass(frozen=True)`），它们既是配置载体，又承担缓存：

- **`CompileArgs`**：怎么编译。含 `out_idx`、`target`、`execution_backend`、`target_host`、`verbose`、`pass_configs`。核心方法是 `compile_program(program)`，它直接调用 `tilelang.compile(...)` 把一个 PrimFunc 编译成 `JITKernel`。
- **`ProfileArgs`**：怎么评测。含 `warmup`/`rep`/`timeout`/`backend`（计时后端）、`supply_type`（输入张量供给方式）、`ref_prog`（参考程序做正确性校验）、`rtol`/`atol`/`max_mismatched_ratio`（容差）、`skip_check`、`cache_input_tensors`。
- **`AutotuneResult`**：调优结果。含 `latency`、`config`、`ref_latency`、`libcode`（生成的源码）、`func`（PrimFunc）、`kernel`（最优 `JITKernel`），并负责把整套结果原子地落盘与加载。

这三个类都实现了 `__hash__`（用 `sha256(json.dumps(..., sort_keys=True))`），因为它们要参与缓存键计算。

#### 4.2.2 核心流程

缓存键（cache key）由 `AutoTuner.generate_cache_key` 生成，输入因素见 [tuner.py:444-468](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/autotuner/tuner.py#L444-L468)：

```
key_data = {
    "version": __version__,           # tilelang 版本
    "op_parameters": <默认参数值>,      # kernel 签名里的默认值
    "extra_parameters": <闭包变量>,     # 外层 M/N/K 等具体值
    "func_source": <源码文本>,          # inspect.getsource
    "configs": <候选空间>,             # 整个 configs 列表
    "compile_args": hash(CompileArgs),
    "profile_args": hash(ProfileArgs),
}
key = sha256(json.dumps(key_data, sort_keys=True))
```

也就是说，「换 target、换候选空间、改 kernel 源码、升级版本」都会让 key 变化，触发重新调优。

缓存分两级（[tuner.py:939-958](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/autotuner/tuner.py#L939-L958)）：先查进程内 `_memory_cache`（字典），再查磁盘（`AutotuneResult.load_from_disk`）。磁盘目录由 `KernelCache._get_namespace_root() / "autotuner"` 决定。

> 关于缓存开关：`TILELANG_AUTO_TUNING_DISABLE_CACHE=1` 可单独禁用 autotuner 落盘（[env.py:418-419](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/env.py#L418-L419)）；`is_cache_enabled()` 还受全局 `TILELANG_DISABLE_CACHE` 控制（[env.py:403-413](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/env.py#L403-L413)）。

#### 4.2.3 源码精读

`CompileArgs.compile_program` 是「编译一个候选」的最薄封装：

[param.py:64-74](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/autotuner/param.py#L64-L74) — 直接转发到 `tilelang.compile(program, out_idx=..., execution_backend=..., target=..., ...)`。autotuner 默认路径（`_default_compile`，[tuner.py:478-483](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/autotuner/tuner.py#L478-L483)）就是调用它。

`CompileArgs` 的稳定哈希（供缓存键）：

[param.py:76-87](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/autotuner/param.py#L76-L87) — 注意 `pass_configs`（一个嵌套 dict）被 `json.dumps(sort_keys=True)` 序列化后再哈希，保证字典键顺序不影响哈希值。

`ProfileArgs` 的字段集合：

[param.py:90-141](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/autotuner/param.py#L90-L141) — 含计时（`warmup`/`rep`/`timeout`/`backend`）、正确性校验（`ref_prog`/`rtol`/`atol`/`max_mismatched_ratio`/`skip_check`）、输入供给（`supply_type`/`supply_prog`/`cache_input_tensors`）。其 `__hash__` 只把「影响数值结果」的字段纳入哈希（不含 `ref_prog` 这类不可哈希的可调用对象）。

`AutotuneResult` 的原子落盘：

[param.py:386-468](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/autotuner/param.py#L386-L468) — 先把所有文件写进一个临时 staging 目录（`path.parent.parent/.staging/...`），最后用 `os.rename` 一步替换为目标目录。POSIX 的 rename 是原子的，所以并发读者永远不会看到「写了一半」的结果。每个文件本身也经 `_safe_write_file` 走「写临时文件 → rename」。

落盘的文件集合（见 `_get_complete_result_files`，[param.py:560-574](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/autotuner/param.py#L560-L574)）：`best_config.json`、`function.pkl`（cloudpickle 序列化的 PrimFunc）、`out_idx.json`、`latency.json`、`device_kernel.cu`/`host_kernel.cu`、按执行后端命名的可执行件（如 `executable.so`）、`params.pkl`。

#### 4.2.4 代码实践

**实践目标**：理解缓存键的构成，亲手让它「失效」。

**操作步骤**：

1. 写一个最小脚本，用 `AutoTuner` 调优一个小 GEMM（参考综合实践）。第一次运行观察 `~/.tilelang/cache/.../autotuner/` 下生成了以 64 位 hex 命名的目录。
2. 第二次运行同一脚本——这次不会出现进度条，而是命中缓存直接返回（终端会有一条 `Found kernel '...' in memory cache` 的 warning）。
3. 把 kernel 源码里随便加一行注释（改 `func_source`），或把 `block_M` 候选值改一个，再运行——key 改变，重新调优。

**需要观察的现象**：第二次运行极快（毫秒级返回）；改源码或改候选后再次出现完整进度条。

**预期结果**：验证了缓存键对 `func_source` 与 `configs` 的敏感性。

#### 4.2.5 小练习与答案

**练习 1**：`ProfileArgs.__hash__` 为什么不把 `ref_prog`（参考程序）纳入哈希？

**答案**：`ref_prog` 是 Python 函数，不可哈希（即便可哈希也不稳定）。更重要的是，`ref_prog` 只影响**正确性校验**，不影响「最优配置是哪个」的结论——只要校验通过，参考程序是谁不影响延迟排序。所以它不应触发重新调优。

**练习 2**：`execution_backend="torch"`（DLPack）时，结果会落盘吗？

**答案**：不会。[tuner.py:1208-1213](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/autotuner/tuner.py#L1208-L1213) 显式判断：`if self.compile_args.execution_backend in ("torch"): ... does not support cache saving to disk`，只写内存缓存。

### 4.3 capture：输入捕获与输入供给

#### 4.3.1 概念说明

调优时，autotuner 需要给 kernel 喂输入张量来量延迟、做正确性校验。有两种供给方式：

1. **自动生成**：由 profiler 按 `supply_type`（如 `TensorSupplyType.Normal`/`Integer`/`Auto`）随机生成形状匹配的张量。要求 kernel 的输入参数**全部是张量**。
2. **用户提供**：用 `with set_autotune_inputs(a, b, c):` 上下文管理器，把一组真实张量「冻结」下来供所有 worker 线程复用。

第二种方式解决两类问题：① kernel 有标量输入参数（自动生成器无法造标量）；② 用户想用固定的、有代表性的输入（如真实的 attention 输入）来调优。

`capture.py` 实现了一个**线程局部栈**（`threading.local()`），因为 benchmark worker 跑在独立线程里，线程局部存储能避免跨线程串扰。

#### 4.3.2 核心流程

```
用户代码:
    with set_autotune_inputs(a, b, c):   # push 一个 AutotuneInputsCapture 到栈顶
        autotuner.run()

set_profile_args 内部 (在 with 块里调用时):
    captured = get_autotune_inputs()      # 读栈顶
    if captured is not None:
        frozen = list(captured)           # 立即冻结成 list
        构造一个 supply_prog(device) -> [tensor.to(device) for ...]
        （按 device 缓存搬迁后的副本，避免重复 .to()）

benchmark worker 线程:
    量延迟时调用 supply_prog(device) 取输入
```

关键点：`set_profile_args` 在**进入 `with` 块时**（即 `run()` 之前）就被调用，所以它能在「主线程、栈非空」时读到捕获的张量并冻结。之后即便 worker 线程的线程局部栈是空的，也不依赖它——因为张量已被冻结进 `supply_prog` 的闭包。

#### 4.3.3 源码精读

线程局部栈 `CaptureStack` 与取栈顶的工具函数：

[capture.py:81-97](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/autotuner/capture.py#L81-L97) — `_get_current_stack()` 懒初始化每个线程的栈；`AutotuneInputsCapture.__enter__` push、`__exit__` pop。

用户 API `set_autotune_inputs`：

[capture.py:100-118](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/autotuner/capture.py#L100-L118) — 同时支持 `set_autotune_inputs(a, b, c)` 与 `set_autotune_inputs([a, b, c])` 两种形式。

`set_profile_args` 里读取捕获输入并构造 `supply_prog`：

[tuner.py:369-392](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/autotuner/tuner.py#L369-L392) — 读 `get_autotune_inputs()`；若非空，则冻结张量、按 device 缓存搬迁副本。注意它会 warning 提示：在 `with set_autotune_inputs` 下，传入的 `supply_prog` 会被忽略。

标量输入的校验护栏 `_validate_input_supply_requirements`：

[tuner.py:422-442](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/autotuner/tuner.py#L422-L442) — 若没有 `supply_prog`（既没自动供给也没捕获输入），且 kernel 有标量输入参数，则抛 `ValueError`，提示用 `with set_autotune_inputs(...)`。

实际评测时调用 `supply_prog` 取张量：

[tuner.py:744-754](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/autotuner/tuner.py#L744-L754) — `_benchmark_target` 内构造 `get_input_tensors_supply`：若 `supply_prog is not None` 用它，否则用 profiler 的自动生成。

#### 4.3.4 代码实践

**实践目标**：用 `set_autotune_inputs` 喂一组固定输入调优。

**操作步骤**（示例代码，非项目原有）：

```python
# 示例代码
import torch, tilelang as tl
from tilelang.autotuner import AutoTuner, set_autotune_inputs

M = N = K = 512
a = torch.randn(M, K, dtype=torch.float16, device="cuda")
b = torch.randn(N, K, dtype=torch.float16, device="cuda")
c = torch.empty(M, N, dtype=torch.float16, device="cuda")

with set_autotune_inputs(a, b, c):
    result = AutoTuner.from_kernel(kernel=my_kernel, configs=configs) \
        .set_compile_args(out_idx=[-1], target="cuda") \
        .set_profile_args(ref_prog=lambda x, y: x @ y.T) \
        .run()
```

**需要观察的现象**：所有配置的 benchmark 都复用同一组 `a/b/c`，不会因每次随机生成而引入方差。

**预期结果**：调优过程不报「scalar input」错误；`result.config` 给出该输入下的最优配置。**若无 GPU，待本地验证。**

#### 4.3.5 小练习与答案

**练习**：为什么 `capture.py` 用 `threading.local()` 而不是普通的全局列表？

**答案**：benchmark worker 跑在独立线程。若用全局列表，多个并发的 autotuner（或同一进程内嵌套的捕获）会互相污染栈；线程局部存储保证每个线程看到自己的栈。但源码注释也指出，正因为线程局部，`set_profile_args` 才必须在主线程、`with` 块内被调用，趁栈非空把张量冻结进闭包——否则 worker 线程读到的会是空栈。

### 4.4 分组编译：把 N 个配置合并成一次设备编译

#### 4.4.1 概念说明

调优一个 GEMM 时，候选空间可能有几百上千个配置。每个配置都要走一遍「lowering → device_codegen → 编译 `.so`」，其中 `device_codegen`（调用 nvcc/mxcc 编译设备源码）是最慢的一步。如果能把多个配置的设备代码**合并到一个源文件**里只编译一次，就能大幅缩短总调优时间——这就是「分组编译（grouped compile）」。

但分组编译有严格前提（[tuner.py:499-515](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/autotuner/tuner.py#L499-L515)）：**仅当 `target.kind == "cuda"` 且 `execution_backend == "tvm_ffi"` 时生效**。这是因为分组编译依赖 `tvm_ffi` 后端能把「一个共享的设备 runtime module」被多个 host module `import_module` 的能力。其它组合（如 nvrtc、maca、cython）会回退到逐配置编译，并打一条 warning。

#### 4.4.2 核心流程

分组编译单元（`compile_grouped_unit_tvm_ffi`）的处理步骤见 [grouped_compile.py:25-156](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/autotuner/grouped_compile.py#L25-L156)：

```
对 unit 里的每个配置 (idx, config):
    1. elaborate: 把 config 代入得到 PrimFunc，并改写 global_symbol 为 f"{name}_gc_{idx}"（防重名）
    2. lower_to_host_device_ir: 下译成 host_mod + device_mod + params
收集所有 lowered_items:
    3. 把每个 device_mod 的函数合并进一个 merged_device_mod（检查符号名不重复）
    4. 只调一次 device_codegen(merged_device_mod) → grouped_device_rt_mod
对每个配置:
    5. host_codegen(host_mod) → grouped_host_rt_mod
    6. grouped_host_rt_mod.import_module(grouped_device_rt_mod)  # 共享设备模块
    7. 组装 CompiledArtifact + TVMFFIKernelAdapter + JITKernel
返回每个配置的 (idx, config, jit_kernel, error)
```

核心收益：第 4 步只执行**一次**设备编译，而非 N 次。第 6 步的 `import_module` 让每个 host module 引用同一份已编译的设备 `.so`。

#### 4.4.3 源码精读

分组编译的触发条件判定：

[tuner.py:499-515](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/autotuner/tuner.py#L499-L515) — `grouped_compile_active = grouped_compile_requested and target_kind == "cuda" and execution_backend == "tvm_ffi"`。不满足时打 warning 并回退。

并发 worker 数的解析：

[tuner.py:517-536](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/autotuner/tuner.py#L517-L536) — 受三个环境变量控制：`TILELANG_AUTO_TUNING_CPU_UTILITIES`（默认 0.9，即用 90% CPU）、`TILELANG_AUTO_TUNING_CPU_COUNTS`（指定具体核数，-1 表自动）、`TILELANG_AUTO_TUNING_MAX_CPU_COUNT`（上限）。

编译单元的切分与提交：

[tuner.py:594-609](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/autotuner/tuner.py#L594-L609) — 若分组编译开启，按 `group_compile_size`（默认 2）把 configs 切成多个单元；否则每个 config 自成一个单元。每个单元提交到线程池。

合并设备 IR 与单次设备编译：

[grouped_compile.py:81-103](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/autotuner/grouped_compile.py#L81-L103) — 把所有 `device_mod.functions` 合并进 `merged_device_mod`，检查符号名不重复（重复则抛 `RuntimeError`），然后只调一次 `device_codegen`。

每个配置的 host 模块引用共享设备模块：

[grouped_compile.py:105-149](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/autotuner/grouped_compile.py#L105-L149) — `grouped_host_rt_mod.import_module(grouped_device_rt_mod)`，再用 `JITKernel(...)` + `TVMFFIKernelAdapter` 包装，`from_database=True` 表示从缓存/合并产物重建。

#### 4.4.4 代码实践

**实践目标**：对比开启/关闭分组编译的调优耗时。

**操作步骤**：

1. 在 CUDA + tvm_ffi 环境下，分别用：
   ```python
   autotuner.run(enable_grouped_compile=False)  # 默认，逐配置编译
   autotuner.run(enable_grouped_compile=True, group_compile_size=8)  # 每 8 个配置合并一次
   ```
2. 用一个较大的候选空间（如 100+ 配置）。
3. 用 `time` 计时整个 `run()`。

**需要观察的现象**：开启分组编译后，`Compiling configurations` 阶段明显变短；终端应无 warning（target=cuda 且 backend=tvm_ffi）。若你用的是 `maca` target，则会看到 `grouped compilation is currently implemented for CUDA+tvm_ffi only` 的 warning 并回退。

**预期结果**：CUDA 上分组编译总耗时显著下降。MACA 上分组编译不生效（回退为逐配置）。**待本地验证。**

#### 4.4.5 小练习与答案

**练习 1**：为什么分组编译要给每个配置的 PrimFunc 改写 `global_symbol` 为 `f"{name}_gc_{idx}"`？

**答案**：合并多个 device_mod 时，若两个配置的 kernel 同名（如都叫 `main`），合并后的 IRModule 会出现符号冲突。加 `_gc_{idx}` 后缀保证唯一，[grouped_compile.py:52-54](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/autotuner/grouped_compile.py#L52-L54)；同时合并阶段还有 `merged_names` 集合做重复检测（[grouped_compile.py:89-96](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/autotuner/grouped_compile.py#L89-L96)）。

**练习 2**：在 MACA target 上想做类似的「合并设备编译」加速，当前代码会怎样？

**答案**：会回退到逐配置编译。[tuner.py:506-514](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/autotuner/tuner.py#L506-L514) 的 `grouped_compile_active` 判定只放行 `cuda` + `tvm_ffi`；MACA 走 `mcrtc`/`tvm_ffi` 但 target_kind 不是 cuda，故不满足。这是 metax 分支的一个潜在优化点。

## 5. 综合实践

**任务**：基于 `examples/gemm/example_gemm_autotune.py`，定义一个**小规模**参数空间（含 `block_M`/`block_N`/`block_K`/`num_stages`），对 GEMM 做一次 autotune，记录最优配置并验证其正确性。

**步骤**：

1. **阅读示例**：先读 [example_gemm_autotune.py:22-107](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/examples/gemm/example_gemm_autotune.py#L22-L107) 的 `get_configs`，理解候选空间的两种生成方式（手写笛卡尔积 / Carver roller 提示）。

2. **自定义一个小空间**（示例代码，非项目原有）：

   ```python
   # 示例代码：缩小候选空间，加快首次调优
   import itertools, tilelang as tl
   from tilelang.autotuner import AutoTuner

   def small_configs():
       space = {
           "block_M": [64, 128],
           "block_N": [64, 128],
           "block_K": [32, 64],
           "num_stages": [0, 2],
       }
       keys = list(space)
       return [dict(zip(keys, vals)) for vals in itertools.product(*space.values())]
   # 共 2*2*2*2 = 16 个配置
   ```

3. **复用示例的 kernel 定义**：直接借用 [example_gemm_autotune.py:117-153](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/examples/gemm/example_gemm_autotune.py#L117-L153) 的 `kernel` 函数（注意它的签名里还有 `thread_num`、`enable_rasteration`，要么在 config 里给默认值，要么在你的 `small_configs` 里补上这两个键）。

4. **驱动调优**（参照 [example_gemm_autotune.py:155-168](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/examples/gemm/example_gemm_autotune.py#L155-L168)）：

   ```python
   # 示例代码
   M = N = K = 1024
   autotuner = (
       AutoTuner.from_kernel(kernel=kernel, configs=small_configs())
       .set_compile_args(out_idx=[-1], target="auto")
       .set_profile_args(
           supply_type=tl.TensorSupplyType.Auto,
           ref_prog=lambda A, B: A @ B.T,
           skip_check=False,
           backend="event",
       )
   )
   result = autotuner.run(warmup=3, rep=20)
   print("best config:", result.config)
   print("best latency (ms):", result.latency)
   ```

5. **记录结果**：填一张小表，记下最优 `block_M/N/K`、`num_stages`、延迟与对应的 TFLOPS（\( \text{TFLOPS} = \frac{2MNK}{\text{latency}} \times 10^{-9} \)）。

6. **验证正确性**：调优过程中 `skip_check=False` 会自动用 `ref_prog` 校验每个候选；你也可在拿到 `result.kernel` 后手动跑一次并与 `A @ B.T` 对比。

**若没有 GPU**：改为「源码阅读型实践」——在 `AutoTuner.run` 的 [tuner.py:968-981](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/autotuner/tuner.py#L968-L981) 处加日志打印 `config_args`，确认你的 16 个配置都被正确对齐到 kernel 签名；并跟踪一次 `compile_program`（[param.py:64-74](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/autotuner/param.py#L64-L74)）的调用链，理解每个配置如何变成 `JITKernel`。

**进阶**：把 `target` 换成 `{"kind": "maca"}`（参考 u3-l3），观察分组编译 warning（`CUDA+tvm_ffi only`）并确认 MACA 走逐配置编译路径。

## 6. 本讲小结

- autotuner 把调优拆成三层：**参数空间**（`configs`）→ **并发编译**（线程池）→ **并发评测**（benchmark worker 线程），由 `AutoTuner.run()` 编排（[tuner.py:882-1217](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/autotuner/tuner.py#L882-L1217)）。
- 有两套 API：命令式 `AutoTuner.from_kernel().set_compile_args().set_profile_args().run()`，声明式 `@tilelang.autotune(configs=...)`（必须叠在 `@tilelang.jit` 上）。
- `CompileArgs`/`ProfileArgs`/`AutotuneResult` 三个不可变数据类既是配置，又通过稳定 `__hash__` 参与缓存键；结果用「临时目录 + 原子 rename」落盘（[param.py:386-468](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/autotuner/param.py#L386-L468)）。
- 缓存键由版本、kernel 源码、闭包变量、候选空间、compile/profile 哈希共同决定；闭包变量提取（`__closure__`）是保证形状变化不误命中的关键。
- `set_autotune_inputs` 用线程局部栈捕获用户输入，解决标量输入与固定输入问题；捕获的张量在 `set_profile_args` 时被冻结进 `supply_prog` 闭包。
- 分组编译把多个配置的设备 IR 合并成一次 `device_codegen`，但**仅 cuda + tvm_ffi 生效**，其它（含 maca）回退逐配置编译。
- 并发度由 `TILELANG_AUTO_TUNING_CPU_UTILITIES` / `CPU_COUNTS` / `MAX_CPU_COUNT` 三个环境变量控制（[tuner.py:517-536](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/autotuner/tuner.py#L517-L536)）。

## 7. 下一步学习建议

- **u8-l3（性能剖析与基准测试）**：深入 `profiler/bench.py` 的 `do_bench`，理解 `event`/`cupti`/`cudagraph` 三种计时后端的差异，以及 L2 冲刷如何让延迟测量更准。autotuner 的 `_benchmark_target` 正是调用它。
- **Carver 模块**：本讲的 `get_configs(with_roller=True)` 用到了 `tilelang.carver.template.MatmulTemplate`。建议阅读 `tilelang/carver/template/matmul.py` 与 `tilelang/carver/roller/policy/tensorcore.py`，理解「设备感知的候选空间自动生成」如何替代手写笛卡尔积。注意 metax 分支有 `tilelang/carver/arch/maca.py`（[maca.py:41-66](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/carver/arch/maca.py#L41-L66)）。
- **u8-l2（swizzle/persistent/splitk）**：本讲调优的 `enable_rasteration`、`num_stages` 等参数的实际效果，需要结合 swizzle、persistent kernel 来理解。
- **为 MACA 加速分组编译**：作为进阶练习，研究能否让 `compile_grouped_unit_tvm_ffi` 适配 MACA 的 `tvm_ffi` 路径（参考 u7 系列对 MACA codegen/module 加载的讲解）。
