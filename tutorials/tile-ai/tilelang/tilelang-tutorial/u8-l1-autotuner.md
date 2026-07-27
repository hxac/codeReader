# Autotuner：配置搜索与并行编译

## 1. 本讲目标

tilelang 把一个 tile 级 DSL 翻译成 CUDA/HIP kernel，但「翻译得对」和「跑得快」是两回事。同一个 GEMM，分块大小 `block_M/block_N/block_K`、流水线级数 `num_stages`、线程数 `threads` 选不同值，性能可以差好几倍——而这些值往往无法靠人脑判断，必须实测。

本讲讲解 tilelang 内置的自动调优器（Autotuner）。读完本讲，你应该能够：

1. 用 `@tilelang.autotune(configs=...)` 装饰器暴露可调参数，并定义配置空间。
2. 用程序化接口 `AutoTuner.from_kernel(...).set_*().run()` 显式控制编译参数与基准参数。
3. 理解 `run()` 内部的「并行编译 + 基准测量」生产者-消费者流水线，以及 `warmup/rep/timeout` 与 `early_stop` 的作用。
4. 掌握分组编译（grouped compile）与 `par_compile` 这两条并行编译路径，理解它们各自适合什么场景。
5. 知道调优结果如何被缓存键保护、如何在内存与磁盘间复用。

本讲覆盖两个最小模块：`tilelang.autotuner`（调优主体）与 `tilelang.jit.par_compile`（底层批量编译引擎）。

## 2. 前置知识

阅读本讲前，建议你已经掌握：

- **JIT 装饰器与 lazy/eager 模式**（讲义 u4-l2）：知道 `@tilelang.jit` 把 Python 函数包成 `JITImpl`，lazy 模式返回 `PrimFunc`，调用返回 `JITKernel`；eager 模式直接返回结果张量。Autotuner 是叠在 `@tilelang.jit` 之上的第二层装饰器。
- **软件流水线 `T.Pipelined`**（讲义 u3-l3）：`num_stages` 是本讲最常见的可调参数之一。
- **编译总流程 `tilelang.lower`**（讲义 u4-l1）：调优器要为每个候选配置跑一遍完整 Pass 流水线，理解「PrimFunc → Pass → device codegen → adapter」的链路很有帮助。
- **编译缓存机制**（讲义 u4-l3）：缓存键（cache key）的设计哲学在这里再次出现——Autotuner 的结果复用同样依赖 SHA-256 缓存键。

几个术语先统一：

- **配置空间（config space）**：一组候选的参数字典，例如 `{"block_M": 128, "block_N": 128, "block_K": 32, "num_stages": 2}`。每个字典代表一种待测的 kernel 写法。
- **候选（candidate / config）**：配置空间里的一个具体字典。
- **编译参数（compile args）**：与候选无关的「编译环境」参数，如 `target`、`execution_backend`、`out_idx`、`pass_configs`。
- **基准参数（profile args）**：决定怎么测延迟的参数，如 `warmup`、`rep`、`timeout`、`supply_type`、`ref_prog`。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [tilelang/autotuner/tuner.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/autotuner/tuner.py) | 调优器主体：`AutoTuner` 类、`run()` 编排、`@autotune` 装饰器、`AutoTuneImpl` 适配层。 |
| [tilelang/autotuner/__init__.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/autotuner/__init__.py) | 包入口，导出 `autotune`、`AutoTuner`、`set_autotune_inputs`、`get_autotune_inputs`。 |
| [tilelang/autotuner/param.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/autotuner/param.py) | 三个 frozen dataclass：`CompileArgs`、`ProfileArgs`、`AutotuneResult`，以及磁盘序列化逻辑。 |
| [tilelang/autotuner/grouped_compile.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/autotuner/grouped_compile.py) | 分组编译：把多个候选的 device IR 合并成一份，只跑一次设备代码生成。 |
| [tilelang/autotuner/capture.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/autotuner/capture.py) | 线程局部的输入张量捕获栈 `set_autotune_inputs`。 |
| [tilelang/jit/\_\_init\_\_.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/jit/__init__.py) | 模块级 `par_compile` 与 `JITImpl.par_compile`，底层批量编译引擎。 |
| [docs/programming_guides/autotuning.md](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/docs/programming_guides/autotuning.md) | 官方调优指南，覆盖两种用法与全部环境变量。 |
| [examples/gemm/example_gemm_autotune.py](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/examples/gemm/example_gemm_autotune.py) | 可运行示例：GEMM 调优，含 roller 提示与手动笛卡尔积两种配置空间。 |

---

## 4. 核心概念与源码讲解

### 4.1 调优全景与核心数据结构

#### 4.1.1 概念说明

自动调优本质上是一个**搜索-编译-测量-取优**的循环。tilelang 的设计可以概括成一句话：

> 给我一个 kernel 工厂函数和一组候选配置，我把每个配置都编译成一个真实 kernel，用同一份输入测延迟，最后把最快的那个连同它的配置和源码打包还给你。

整个调优器围绕三个 frozen（不可变）数据类组织，定义在 `tilelang/autotuner/param.py`：

- **`CompileArgs`**：编译环境（`out_idx`、`target`、`execution_backend`、`target_host`、`verbose`、`pass_configs`）。所有候选共享同一份编译环境，差异只在候选字典里。
- **`ProfileArgs`**：基准环境（`warmup`、`rep`、`timeout`、`backend`、`supply_type`、`ref_prog`、容差等）。所有候选共享同一份基准环境，保证「同一把尺子量」。
- **`AutotuneResult`**：调优产物（最优 `latency`、`config`、`ref_latency`、`libcode`、`func`、`kernel`）。这是 `run()` 的返回值，也是缓存到磁盘再加载回来的对象。

为什么 `CompileArgs` 和 `ProfileArgs` 都是 frozen 且带 `__hash__`？因为它们要参与**缓存键**的计算——只有把它们哈希进键里，才能保证「换了一个 `target` 或换了一组容差」时不会错误地复用旧结果。

#### 4.1.2 核心流程

调优主流程（高层）：

```text
configs (list[dict] 或 callable)
   │
   ├─ generate_cache_key()  ──► 内存缓存命中？磁盘缓存命中？ ──► 直接返回 AutotuneResult
   │       （命中即跳过整个搜索）
   │
   └─ 不命中：
        for cfg in configs:
            elaborate(cfg)  ──► PrimFunc
            lower + codegen ──► JITKernel      （并行，线程池）
        for kernel in compiled:
            benchmark(kernel)                   （测量，可选并行/多 GPU）
            validate(kernel, ref_prog)          （正确性校验）
        选 latency 最小者 ──► AutotuneResult
        写回内存缓存 + 磁盘缓存
```

配置空间的大小是各可调参数取值集合的笛卡尔积：

\[
|C| = \prod_{i=1}^{n} |V_i|
\]

其中 \(V_i\) 是第 \(i\) 个可调参数（如 `block_M`）的候选取值集合。例如 `block_M∈{64,128,256}`、`block_N∈{64,128,256}`、`block_K∈{32,64}`、`num_stages∈{0,1,2,3}`、`thread_num∈{128,256}`、`enable_rasteration∈{True,False}` 的笛卡尔积就是 \(3\times3\times2\times4\times2\times2=288\) 个候选。这正是 `examples/gemm/example_gemm_autotune.py` 默认搜索空间的大小，也是为什么需要并行编译的原因。

#### 4.1.3 源码精读

包入口只做四件事的再导出，结构很轻：

[autotuner/\_\_init\_\_.py:1-9](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/autotuner/__init__.py#L1-L9) —— 导出 `autotune`/`AutoTuner` 与输入捕获 `set_autotune_inputs`/`get_autotune_inputs`。注意 `autotune` 也被挂到顶层 `tilelang` 包：[\_\_init\_\_.py:211](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/__init__.py#L211)（`from .autotuner import autotune`），所以 `@tilelang.autotune(...)` 与 `@tilelang.autotuner.autotune(...)` 等价。

三个数据类的字段定义清晰展示了「编译环境 / 基准环境 / 产物」的分工：

[param.py:44-62](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/autotuner/param.py#L44-L62) —— `CompileArgs`，注意它有一个 `compile_program` 方法直接委托给 `tilelang.compile`，这是「每个候选走一遍标准编译」的落点。

[param.py:76-87](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/autotuner/param.py#L76-L87) —— `CompileArgs.__hash__` 把 `execution_backend/target/target_host/verbose/pass_configs` 拼成 JSON 再 SHA-256，确保编译环境变化时缓存键也变。

[param.py:114-141](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/autotuner/param.py#L114-L141) —— `ProfileArgs` 与其 `__hash__`。注意 `supply_prog`/`ref_prog`/`manual_check_prog` 是 `Callable`，**不参与**哈希（不可哈希），所以换参考实现不会让缓存失效——这点设计上有点反直觉，调优时若改了参考实现的语义，需要手动清缓存。

[param.py:144-162](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/autotuner/param.py#L144-L162) —— `AutotuneResult`，`run()` 的返回类型，`kernel` 字段就是可直接调用的最优 `JITKernel`。

缓存键的生成在调优器主体里：

[tuner.py:457-481](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/autotuner/tuner.py#L457-L481) —— `generate_cache_key`。键数据包含 `version`（tilelang 版本）、函数默认参数、闭包自由变量（`extra_parameters`）、**函数源码**、`configs`、以及 `compile_args`/`profile_args` 的哈希。注意「函数源码」也进了键——这意味着你改了一行 DSL 代码，缓存自动失效，这是非常正确的设计。

#### 4.1.4 代码实践（源码阅读型）

1. **实践目标**：理解缓存键由哪些要素决定，从而知道「改了什么会触发重新调优」。
2. **操作步骤**：
   - 打开 [tuner.py:469-481](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/autotuner/tuner.py#L469-L481)，对照 `key_data` 字典逐项标注「这是用户输入 / 编译环境 / 基准环境 / 程序本身」。
   - 打开 [param.py:76-141](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/autotuner/param.py#L76-L141)，确认 `supply_prog`/`ref_prog` 不进哈希。
3. **需要观察的现象**：哪些改动会让键变（版本、源码、configs、target、容差），哪些不会（参考函数对象本身）。
4. **预期结果**：得到一张「缓存失效条件表」。例如「改 `block_M` 默认值」会失效（默认值进 `op_parameters`），「换一个新的 `ref_prog` 函数对象」不会失效。
5. **运行结果**：待本地验证（无需 GPU，纯源码阅读）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `CompileArgs` 和 `ProfileArgs` 用 `@dataclass(frozen=True)`？

**参考答案**：frozen 使实例不可变，从而可哈希（`__hash__` 才能稳定）；同时不可变保证了「一次调优会话内编译/基准环境不被意外篡改」，避免不同候选之间基准条件漂移。

**练习 2**：如果你把参考实现 `ref_prog` 从「`A @ B`」改成「`A @ B + 1`」（一个故意错误的参考），缓存会失效吗？调优结果会出错吗？

**参考答案**：缓存**不会**失效，因为 `ref_prog` 是 `Callable`，不参与 `ProfileArgs.__hash__`；但调优会因正确性校验失败（`assert_allclose`）而对该候选报警告并跳过，极端情况下可能所有候选都失败、`run()` 抛 `RuntimeError("Auto-tuning failed: ...")`。教训：改参考实现语义时务必手动清缓存（删除 `$TILELANG_CACHE_DIR/autotuner` 下对应目录或设 `TILELANG_AUTO_TUNING_DISABLE_CACHE=1`）。

---

### 4.2 两种使用入口：装饰器与程序化接口

#### 4.2.1 概念说明

tilelang 提供两种风格上等价、控制粒度不同的调优入口：

- **装饰器风格** `@tilelang.autotune(configs=...)`：叠在 `@tilelang.jit` 之上，可调参数写成函数带默认值的参数。调用被装饰的函数时，第一次会触发调优、之后命中进程内缓存。最贴近日常写 kernel 的体验。
- **程序化风格** `AutoTuner.from_kernel(kernel, configs).set_compile_args(...).set_profile_args(...).run()`：显式构造 `AutoTuner` 对象，链式设置编译/基准参数，手动调用 `run()` 返回 `AutotuneResult`。适合脚本化、CI、需要精细控制每个参数的场景。

两者共享同一个 `AutoTuner` 类与同一个 `run()`——装饰器只是把 `AutoTuner` 包了一层 `AutoTuneImpl`，并在每次调用时算一个进程内缓存键。

#### 4.2.2 核心流程

**装饰器风格**的关键约束：可调参数必须是**函数签名里的带默认值参数**。装饰器会用候选字典里的值覆盖这些默认值，把其余（非可调）参数原样透传。所以典型写法是：

```python
@tilelang.autotune(configs=matmul_configs, warmup=25, rep=100, timeout=60)
@tilelang.jit(out_idx=[-1])
def matmul(M, N, K,                       # 问题尺寸（非可调）
           block_M=128, block_N=128, block_K=32,   # 可调：分块
           threads=128, num_stages=3):             # 可调：线程/流水线
    @T.prim_func
    def kernel(...): ...
    return kernel
```

**程序化风格**则把「kernel 工厂」与「配置空间」分开传：

```python
tuner = AutoTuner.from_kernel(kernel_factory, configs)
tuner.set_compile_args(target="auto", out_idx=[-1])
tuner.set_profile_args(supply_type=..., ref_prog=..., warmup=3, rep=20)
result = tuner.run()
```

#### 4.2.3 源码精读

`@autotune` 装饰器本身是一个「参数化装饰器工厂」，它要求被装饰对象已经是 `JITImpl`（即已被 `@tilelang.jit` 装饰）：

[tuner.py:1442-1543](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/autotuner/tuner.py#L1442-L1543) —— `autotune(...)` 函数。注意 [tuner.py:1513-1516](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/autotuner/tuner.py#L1513-L1516)：直接 `@autotune`（不带参数）会抛 `ValueError`——必须写成 `@autotune(configs=...)` 关键字形式。内部 `decorator` 断言 `isinstance(impl, JITImpl)`，再返回一个 `AutoTuneImpl`。

`AutoTuneImpl` 是装饰器风格的适配层，它的 `__call__` 负责「算进程内缓存键 → 未命中则跑 `run()` → 命中则复用」：

[tuner.py:1391-1436](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/autotuner/tuner.py#L1391-L1436) —— `AutoTuneImpl.__call__`。要点：
- [tuner.py:1399-1409](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/autotuner/tuner.py#L1399-L1409)：进程内缓存键是 `(norm_args, norm_kwargs)`，其中 `do_not_specialize` 列出的参数会被排除——这是「让某些参数变化不触发重新调优」的开关。
- [tuner.py:1418-1420](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/autotuner/tuner.py#L1418-L1420)：`jit_compile`/`jit_elaborate` 两个闭包把「用户调用参数 + 候选参数」合并后喂给 `JITImpl`。
- [tuner.py:1422-1423](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/autotuner/tuner.py#L1422-L1423)：未命中时调用 `autotuner.run()`，把 `(best_kernel, best_config)` 存进 `_tuner_cache`。

`AutoTuneImpl.get_tunner` 把装饰器参数装配成一个 `AutoTuner` 实例：

[tuner.py:1358-1389](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/autotuner/tuner.py#L1358-L1389) —— 用 `functools.partial` 把 `warmup/rep/timeout/early_stop/early_stop_factor` 烤进 `autotuner.run`，这样 `run()` 调用时就不用再传这些。

程序化风格的入口非常简洁：

[tuner.py:277-288](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/autotuner/tuner.py#L277-L288) —— `AutoTuner.from_kernel`，实际就是 `cls(kernel, configs)` 的别名，提供语义化的构造方式。

[tuner.py:290-345](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/autotuner/tuner.py#L290-L345) —— `set_compile_args`。`target=None` 时读环境变量 `TILELANG_DEFAULT_TARGET`，`execution_backend=None` 时读 `TILELANG_EXECUTION_BACKEND`，并把字符串 target 经 `determine_target` 归一化成 TVM `Target`。返回 `self` 以支持链式调用。

[tuner.py:347-428](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/autotuner/tuner.py#L347-L428) —— `set_profile_args`。其中 [tuner.py:385-405](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/autotuner/tuner.py#L385-L405) 有一段重要逻辑：如果当前处于 `with set_autotune_inputs(...)` 上下文里，会把捕获的张量**冻结**成一个按设备缓存的 `supply_prog`，确保基准线程能拿到同一份输入。

官方文档对两种风格的对照示例很完整，建议对照阅读：[autotuning.md:14-76](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/docs/programming_guides/autotuning.md#L14-L76)（装饰器）与 [autotuning.md:86-118](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/docs/programming_guides/autotuning.md#L86-L118)（程序化）。

#### 4.2.4 代码实践（源码阅读型 + 可选运行）

1. **实践目标**：用同一段 GEMM 工厂，分别以装饰器和程序化两种方式发起调优，理解它们的等价性。
2. **操作步骤**：
   - 阅读 [examples/gemm/example_gemm_autotune.py:117-168](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/examples/gemm/example_gemm_autotune.py#L117-L168)：`kernel` 工厂把 `block_M/block_N/block_K/num_stages/thread_num/enable_rasteration` 都设为 `None` 默认参数（可调），`get_best_config` 用 `AutoTuner.from_kernel(...).set_compile_args(...).set_profile_args(...).run(warmup=3, rep=20)` 发起调优。
   - 对照 [autotuning.md:38-62](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/docs/programming_guides/autotuning.md#L38-L62) 的装饰器写法。
   - （需 CUDA GPU）运行 `python examples/gemm/example_gemm_autotune.py --use_autotune --m 1024 --n 1024 --k 1024`，观察进度条与 `Tuned Latency ... with config ...` 日志。
3. **需要观察的现象**：两种入口最终都调用 `AutoTuner.run()`；程序化风格的 `result.config` 与 `result.kernel` 分别是最优配置与可调用 kernel。
4. **预期结果**：装饰器风格 `matmul(M,N,K)` 返回最优 `JITKernel`；程序化风格 `tuner.run()` 返回 `AutotuneResult`，取 `.kernel` 即可。
5. **运行结果**：无 GPU 时为源码阅读型实践；有 GPU 时记录最优 config 与延迟（待本地验证）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `@autotune` 必须叠在 `@tilelang.jit` 之上，不能单独用？

**参考答案**：`autotune` 内部的 `decorator` 断言 `isinstance(impl, JITImpl)`（[tuner.py:1522](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/autotuner/tuner.py#L1522)）。`AutoTuneImpl` 需要 `JITImpl` 提供的 `get_tir`/`compile`/`parse_args`/`signature` 等能力来「用候选参数实例化 PrimFunc 并编译」。装饰器只负责搜配置，编译能力全部来自下层 `JITImpl`。

**练习 2**：`do_not_specialize=["batch"]` 会改变什么行为？

**参考答案**：它让 `AutoTuneImpl.__call__` 在算进程内缓存键时**排除** `batch` 参数（[tuner.py:1399-1405](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/autotuner/tuner.py#L1399-L1405)）。于是不同 `batch` 共享同一份调优结果——前提是你的 kernel 对 `batch` 不敏感（或愿意接受次优）。这能把「N 个 batch 各调一次」省成「调一次、N 个 batch 复用」。

---

### 4.3 run() 的编排：并行编译与基准测量

#### 4.3.1 概念说明

`AutoTuner.run()` 是整个调优器的心脏。它要解决一个工程难题：**编译是 CPU 密集且可并行的，基准测量必须串行（共享一个 GPU）或受限并行（多 GPU），而二者又要尽量重叠以节省墙钟时间。**

tilelang 的解法是一个**生产者-消费者流水线**：

- **生产者**：一个 `ThreadPoolExecutor` 并行编译各个候选，产出 `JITKernel`。
- **消费者**：一组基准 worker 线程，从队列里取已编译的 kernel，做正确性校验 + 测延迟。
- **重叠**：编译出一个就喂一个给基准队列，不必等全部编译完（`use_pipeline=True` 时基准线程提前启动）。

此外还有三个工程细节：超时保护、早停（early stop）、多 GPU 基准。

#### 4.3.2 核心流程

`run()` 的主循环（简化伪代码）：

```text
# 1. 缓存检查（内存 → 磁盘），命中即返回
key = generate_cache_key(...)
if cached: return cached

# 2. 把 configs 展开成 config_args，校验可调参数
config_args = [展开每个 config, 注入 __pass_configs__]

# 3. 启动编译线程池（生产者）
pool, futures = _prepare_compile_execution(...)

# 4. 启动基准 worker 线程（消费者），从队列取 kernel
for worker_device in benchmark_devices:
    start Thread(_benchmark_worker_loop)

# 5. 主循环：等编译完成 → 把 kernel 入基准队列 → 收基准结果
while pending_futures:
    done = wait(FIRST_COMPLETED)
    for future in done:
        for (idx, cfg, kernel, err) in future.result():
            if err: skip
            else: enqueue_benchmark(kernel)
    drain_benchmark_results(non-blocking)

# 6. 等所有基准结果回来
wait until benchmark_processed == benchmark_expected

# 7. 选最优，组装 AutotuneResult，写缓存
```

每个候选的基准（`_benchmark_target`）内部做三件事：

1. **准备输入**：用 `supply_prog`/`supply_type` 生成或复用输入张量。
2. **正确性校验**：若有 `ref_prog`，跑 `profiler.assert_allclose(ref_prog, ...)`。
3. **测延迟**：`profiler.do_bench(n_warmup=warmup, n_repeat=rep, ...)`。

**早停（early_stop）** 的原理：基准前先用 5 次迭代估一个 `estimate_ms`，若它已经超过 `best_latency * early_stop_factor`，就直接跳过完整基准、返回估计值。这样明显慢的候选不会浪费 `rep` 次完整测量。共享的最优延迟通过一个 `list[float]`（`shared_best_latency_ref`）在线程间传递。

#### 4.3.3 源码精读

`run()` 的签名暴露了所有可调旋钮：

[tuner.py:935-947](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/autotuner/tuner.py#L935-L947) —— 注意默认 `timeout=180`、`early_stop_factor=2.0`，且 `early_stop_factor < 1.0` 会被拒绝（[tuner.py:967-968](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/autotuner/tuner.py#L967-L968)）。

缓存检查的「内存优先、磁盘次之」两级：

[tuner.py:999-1018](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/autotuner/tuner.py#L999-L1018) —— 受 `env.is_cache_enabled() and not env.is_autotune_cache_disabled()` 双重门控。命中内存缓存时还会打印一条建议用 `@tilelang.autotune` 的警告（因为直接用 `AutoTuner.from_kernel` 会绕过进程内 `_tuner_cache`）。

候选展开与可调参数校验：

[tuner.py:1029-1041](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/autotuner/tuner.py#L1029-L1041) —— 每个 config 字典只保留「函数签名里存在的键」，多出来的键（除保留键 `pass_configs`）会抛 `Unused keys` 错误。这是「候选键名必须匹配可调参数名」的强制约束。

并行度的解析（CPU worker 数量）：

[tuner.py:550-569](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/autotuner/tuner.py#L550-L569) —— `_resolve_num_compile_workers`。优先级：`TILELANG_AUTO_TUNING_CPU_COUNTS`（>0 时直接指定）> `TILELANG_AUTO_TUNING_CPU_UTILITIES`（默认 0.9，按可用 CPU 比例）> 受 `TILELANG_AUTO_TUNING_MAX_CPU_COUNT` 封顶。这三个环境变量定义在 [env.py:389-392](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/env.py#L389-L392)。

编译执行的准备（生产者侧）：

[tuner.py:571-652](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/autotuner/tuner.py#L571-L652) —— `_prepare_compile_execution`。其中 [tuner.py:589-601](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/autotuner/tuner.py#L589-L601) 的 `cuda_device_wrapper` 会给每个编译任务绑定 `torch.cuda.set_device`，避免多线程编译时 CUDA 上下文串台。

基准 worker 的主循环（消费者侧），含超时机制：

[tuner.py:655-764](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/autotuner/tuner.py#L655-L764) —— `_benchmark_worker_loop`。超时的实现是 [tuner.py:726-731](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/autotuner/tuner.py#L726-L731)：把基准调用丢进一个 daemon 子线程，主 worker 线程 `join(timeout=timeout)`，若超时未返回则记为 `"timeout"` 跳过该候选。这是一种「软超时」——daemon 线程可能仍在跑，但结果不再被采纳。

单个候选的基准逻辑（输入供给 + 校验 + 测延迟 + 早停）：

[tuner.py:766-868](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/autotuner/tuner.py#L766-L868) —— `_benchmark_target`。注意 [tuner.py:843-851](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/autotuner/tuner.py#L843-L851) 把 `shared_best_latency[0] * early_stop_factor` 作为 `early_stop_baseline` 传给 `do_bench`。

早停的底层实现（在 profiler 里）：

[bench.py:197-218](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/profiler/bench.py#L197-L218) —— 先用 5 次迭代估 `estimate_ms`，若 `estimate_ms > early_stop_baseline` 则直接返回估计值，跳过完整 `rep` 次测量。

最优结果的记录与共享：

[tuner.py:1108-1118](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/autotuner/tuner.py#L1108-L1118) —— `_record_benchmark_result`：一旦发现更小延迟，就更新 `best_latency/best_config/best_kernel`，并把新最优写回 `shared_best_latency_ref[0]`，让其它 worker 的早停阈值同步收紧。

主循环（编译 future 完成 → 入基准队列 → 非阻塞收结果）：

[tuner.py:1209-1255](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/autotuner/tuner.py#L1209-L1255) —— `finally` 块确保无论是否异常，都给基准队列发 `None` 哨兵、join worker、关线程池与进度条。

#### 4.3.4 代码实践（可运行，需 CUDA GPU）

1. **实践目标**：观察 `warmup/rep/timeout/early_stop` 对调优耗时与结果的影响。
2. **操作步骤**：
   - 准备一个小 GEMM（如 M=N=K=512）与一个缩减的配置空间（如 4 个候选），用 4.2 的程序化写法。
   - 第一次设 `tuner.run(warmup=3, rep=20, timeout=60, early_stop=False)`。
   - 第二次清缓存后设 `tuner.run(warmup=3, rep=20, timeout=60, early_stop=True, early_stop_factor=2.0)`。
   - 对照 `autotuner.log`（工作目录下）与终端的 `Tuned Latency ...` 行。
3. **需要观察的现象**：开 `early_stop` 后，明显慢于最优 2 倍的候选在日志里只出现一次「估计延迟」而非完整 `rep` 次测量的稳定值；总墙钟时间缩短。
4. **预期结果**：两次得到相同的 `best_config`（早停只跳过慢候选，不影响选最优），但第二次更快。
5. **运行结果**：待本地验证（需 GPU）。

#### 4.3.5 小练习与答案

**练习 1**：基准 worker 的超时为什么用「daemon 子线程 + join」而不是直接 `signal.alarm`？

**参考答案**：`signal.alarm`（SIGALRM）只能在 POSIX 主线程使用（见 [tuner.py:175-177](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/autotuner/tuner.py#L175-L177) 的 `run_with_timeout` 注释）。而基准 worker 本身就跑在子线程里，无法再用 SIGALRM；因此改用「把单次基准调用丢进 daemon 子线程、worker 线程 join(timeout)」的方式实现软超时。代价是被超时的 daemon 线程可能仍在 GPU 上跑，但结果不会被采纳。

**练习 2**：`early_stop_factor` 设成 1.0 会怎样？设成 0.5 呢？

**参考答案**：设成 1.0 意味着「估计延迟只要大于当前最优就跳过」，激进但合法（[tuner.py:967-968](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/autotuner/tuner.py#L967-L968) 只拒绝 `< 1.0`）。设成 0.5 会被 `run()` 直接抛 `ValueError`，因为低于 1.0 意味着「比最优还快才不跳过」，逻辑上会让几乎所有候选都被跳过、失去调优意义。

---

### 4.4 分组编译：多候选共享一次设备代码生成

#### 4.4.1 概念说明

并行编译虽然快，但每个候选都要独立跑一次**设备代码生成**（device codegen，即把 TIR 编译成 cubin），这是调优里最贵的步骤之一（nvcc 调用很慢）。`enable_grouped_compile=True` 提供了一条优化路径：

> 把若干个候选的 **device IR 合并成一个 IRModule**，只调用一次 device codegen，得到一个共享的 device runtime module；再为每个候选单独构建 host 部分，并 `import_module` 共享的 device module。

这把「N 次 device codegen」降到「⌈N / group_compile_size⌉ 次」。代价是 host 部分仍要逐个构建，且当前**只支持 CUDA + tvm_ffi 后端**。

#### 4.4.2 核心流程

分组编译的步骤（对应源码注释里的五步）：

```text
对一组 unit_items 中的每个候选:
    1. elaborate(config) ──► PrimFunc，并改写 global_symbol 加唯一后缀 _gc_{idx}
    2. lower_to_host_device_ir(prim_func) ──► (host_mod, device_mod, params, ...)

把所有 device_mod 的函数合并成一个 merged_device_mod（符号去重）
    3. device_codegen(merged_device_mod) ──► grouped_device_rt_mod   （只一次！）

对每个候选:
    4. host_codegen(host_mod) ──► grouped_host_rt_mod
       grouped_host_rt_mod.import_module(grouped_device_rt_mod)   （共享 device）
    5. 构造 TVMFFIKernelAdapter + JITKernel，共享同一份 device 源码
```

关键点：每个候选的 device 函数被重命名（加 `_gc_{idx}` 后缀）以避免符号冲突；合并后 device codegen 只跑一次；每个候选的 host runtime 通过 `import_module` 复用这份共享 device module。

#### 4.4.3 源码精读

是否启用分组编译的判定（限定后端）：

[tuner.py:532-548](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/autotuner/tuner.py#L532-L548) —— `_resolve_grouped_compile_mode`。只有 `enable_grouped_compile=True and group_compile_size>1 and target_kind=="cuda" and execution_backend=="tvm_ffi"` 四者同时成立才真正激活，否则降级为逐候选编译并打印警告。

分组编译的主体：

[grouped_compile.py:28-198](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/autotuner/grouped_compile.py#L28-L198) —— `compile_grouped_unit_tvm_ffi`。几个关键片段：

[grouped_compile.py:64-69](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/autotuner/grouped_compile.py#L64-L69) —— 每个候选先 elaborate 成 PrimFunc，再用 `with_attr("global_symbol", unique_symbol)` 给符号加 `_gc_{idx}` 后缀，避免合并时撞名。

[grouped_compile.py:105-134](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/autotuner/grouped_compile.py#L105-L134) —— 合并所有 device_mod 的函数到一个 `merged_device_mod`，并用 `merged_names` 集合检测重复符号（重复则抛 `Duplicate device global symbol`），最后只调用一次 `device_codegen`。

[grouped_compile.py:138-191](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/autotuner/grouped_compile.py#L138-L191) —— 逐候选构建 host runtime，`grouped_host_rt_mod.import_module(grouped_device_rt_mod)` 让 host 复用共享 device module，再装配出独立的 `JITKernel`（`from_database=True` 表示跳过重复编译）。

桶分（bucketing）逻辑在主调优器里：

[tuner.py:628-644](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/autotuner/tuner.py#L628-L644) —— 先按「每个候选的 `pass_configs`」分桶（同桶共享 pass 配置），再每桶按 `group_compile_size` 切分成多个编译单元。这意味着不同 pass_configs 的候选不会被混在一个 device module 里——这是必要的，因为 pass_configs 会改变生成的 device 代码。

#### 4.4.4 代码实践（源码阅读型 + 可选运行）

1. **实践目标**：理解分组编译「一次 device codegen」带来的加速。
2. **操作步骤**：
   - 阅读 [grouped_compile.py:105-134](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/autotuner/grouped_compile.py#L105-L134)，确认 device codegen 只调用一次。
   - （需 CUDA GPU）用程序化接口对 8 个候选分别测 `run(enable_grouped_compile=False)` 与 `run(enable_grouped_compile=True, group_compile_size=4)` 的墙钟时间。
3. **需要观察的现象**：开启分组编译后，日志里 `device codegen` 相关阶段（nvcc 调用）次数从 8 降到 2；总编译时间下降。
4. **预期结果**：相同 `best_config`，分组编译更快。
5. **运行结果**：待本地验证（需 GPU）。

#### 4.4.5 小练习与答案

**练习 1**：为什么分组编译要给每个候选的 device 函数加 `_gc_{idx}` 后缀？

**参考答案**：多个候选的 PrimFunc 默认 `global_symbol` 通常相同（如同名 `main`），直接合并到一个 IRModule 会符号冲突。加唯一后缀让每个 device kernel 在共享 module 里有唯一名字（[grouped_compile.py:67-69](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/autotuner/grouped_compile.py#L67-L69)），且 [grouped_compile.py:113-119](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/autotuner/grouped_compile.py#L113-L119) 还有显式的重复检测兜底。

**练习 2**：为什么分组编译目前只支持 `cuda + tvm_ffi`？

**参考答案**：其它执行后端（nvrtc/torch/cython/cutedsl）的产物形态不同（如 nvrtc 是裸 cubin + Python launcher，torch 走 DLPack），它们不通过 TVM `rt_mod.import_module` 共享 device module 这套机制。`compile_grouped_unit_tvm_ffi` 写死了 `TVMFFIKernelAdapter` 与 `host_codegen`/`device_codegen` 的 TVM 路径（[grouped_compile.py:164-189](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/autotuner/grouped_compile.py#L164-L189)），所以 [tuner.py:540](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/autotuner/tuner.py#L540) 显式限定后端，不满足则降级。

---

### 4.5 par_compile：手动批量编译的底层引擎

#### 4.5.1 概念说明

`AutoTuner.run()` 是「编译 + 测量 + 取优」的一站式方案。但有时你只想要**批量编译**这一步——比如自己写测量逻辑、或只关心每个候选的源码。`tilelang.jit.par_compile` 就是这个底层引擎：它接收一批 PrimFunc（或一批 config），用线程池并行编译，返回 `list[JITKernel]`，**不做任何测量与选优**。

`par_compile` 也是 `AutoTuner` 内部的编译路径之一（非分组模式下，每个候选的 `jit_compile` 最终落到这里）。理解它有助于你看清「调优器 = par_compile + 测量 + 取优」的分层关系。

#### 4.5.2 核心流程

`par_compile` 的逻辑非常直接：

```text
funcs = [f1, f2, ..., fN]              # 一批 PrimFunc
with ThreadPoolExecutor(num_workers):
    for f in funcs: submit(compile, f, ...)   # 每个独立 compile（走 cached() 缓存层）
    as_completed 收集结果，按原顺序返回 list[JITKernel]
```

`JITImpl.par_compile` 在此基础上多做一步「elaborate」：它接收的是 config 字典列表，先逐个 `get_tir(**cfg)` 把 config 实例化成 PrimFunc，再交给模块级 `par_compile`。

#### 4.5.3 源码精读

模块级 `par_compile`（底层引擎）：

[jit/\_\_init\_\_.py:173-254](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/jit/__init__.py#L173-L254) —— 用 `concurrent.futures.ThreadPoolExecutor(num_workers, "tl-par-comp")` 并行提交，每个任务调 `compile`（即 [jit/\_\_init\_\_.py:91-170](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/jit/__init__.py#L91-L170) 的 `compile`，内部走 `cached()` 缓存层）。`ignore_error=True` 时单候选失败记 warning 并置 `None`，否则整体抛异常。注意它通过 `future_map[future]=i` 保证结果按输入顺序返回。

`JITImpl.par_compile`（config → PrimFunc → 引擎）：

[jit/\_\_init\_\_.py:393-440](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/jit/__init__.py#L393-L440) —— 先用 `tqdm` 串行 `get_tir(**cfg)` 把每个 config 实例化成 PrimFunc（这一步叫 "Elaborating"，对应 [jit/\_\_init\_\_.py:422](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/jit/__init__.py#L422)），再委托模块级 `par_compile`。

`AutoTuner` 如何复用 `par_compile`：装饰器路径的 `AutoTuneImpl._make_jit_compile_func` 给每个候选构造一个 `jit_compile` 闭包：

[tuner.py:1331-1356](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/autotuner/tuner.py#L1331-L1356) —— 注意 [tuner.py:1348-1349](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/autotuner/tuner.py#L1348-L1349)：lazy 且无 per-config pass_configs 时走 `self.jit_impl(*args, **kwargs, __tune_params=config_arg)`——这是 `JITImpl.__call__` 的一条特化路径，用 `__tune_params` 把候选参数注入缓存键（见 [jit/\_\_init\_\_.py:475-481](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/tilelang/jit/__init__.py#L475-L481) 的 `parse_cache_key`），最终也经 `compile`→`cached()` 完成编译。

文档里给出了 `par_compile` 的手动用法：

[autotuning.md:226-248](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/docs/programming_guides/autotuning.md#L226-L248) —— 用 `impl.par_compile(cfgs, num_workers=4)` 拿到一批 kernel 后「自己写基准」。

#### 4.5.4 代码实践（源码阅读型 + 可选运行）

1. **实践目标**：用 `par_compile` 批量编译一组 GEMM 配置，自己测延迟，体会它与 `AutoTuner` 的分工。
2. **操作步骤**：
   - 写一个 `@tilelang.jit` 的 GEMM 工厂（可调参数为 `block_M/block_N/block_K`）。
   - 准备 3 个 config 字典。
   - 调用 `factory.par_compile(cfgs, num_workers=2)` 得到 3 个 `JITKernel`。
   - 自己用 `kernel.get_profiler().do_bench()` 逐个测延迟（参考 u8-l3 的 profiler 讲义）。
3. **需要观察的现象**：`par_compile` 不做正确性校验、不选优、不写 autotuner 缓存，只返回 kernel 列表；编译进度条显示 "Elaborating" 与 "Parallel Compiling" 两个阶段。
4. **预期结果**：得到 3 个可调用 kernel 与它们的延迟，手动挑出最快的一个。
5. **运行结果**：待本地验证（需 GPU）。

#### 4.5.5 小练习与答案

**练习 1**：`AutoTuner.run()` 和 `JITImpl.par_compile()` 都做并行编译，区别在哪？

**参考答案**：`par_compile` 只做「批量编译」，返回 `list[JITKernel]`，不测量、不校验、不选优、不写 autotuner 缓存。`AutoTuner.run()` 在编译之外还多了「正确性校验 + 基准测量 + 取最优 + 内存/磁盘结果缓存 + 超时/早停/多 GPU」一整套编排。可以说 `AutoTuner` = `par_compile`（或 grouped compile）+ 测量与选优的包装。

**练习 2**：`par_compile` 里每个候选都调 `compile`，而 `compile` 内部走 `cached()`。这意味着同一批里如果有两个相同 config，会编译两次吗？

**参考答案**：不会真正重复编译。`cached()` 是基于 TIR 内容的缓存层（讲义 u4-l3），两个相同 config 产生相同的 PrimFunc，第二个会在 `cached()` 层命中缓存直接返回。但 elaborate 阶段（`get_tir`）仍会执行两次——所以去重最好在调用 `par_compile` 之前自己做。

---

## 5. 综合实践

把本讲的「配置空间、两种入口、并行编译、缓存复用」串起来，完成下面这个端到端的小任务（需 CUDA GPU；无 GPU 时改为源码阅读 + 伪代码）。

**任务**：为一个分块 GEMM 定义 `block_M/block_N/block_K/num_stages` 的配置空间并跑 autotune，记录最优 config 与延迟。

**步骤**：

1. **写 kernel 工厂**。参考 [examples/gemm/example_gemm_autotune.py:117-153](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/examples/gemm/example_gemm_autotune.py#L117-L153)，把 `block_M/block_N/block_K/num_stages/thread_num` 设为默认 `None` 的可调参数，kernel 体内用 `T.Pipelined(..., num_stages=num_stages)`、`T.gemm`、`T.copy`。

2. **定义配置空间**。写一个 `get_configs(M,N,K)` 返回笛卡尔积，例如：
   ```python
   # 示例代码：缩小后的配置空间，便于快速跑通
   import itertools
   def get_configs(M, N, K):
       return [
           dict(block_M=bm, block_N=bn, block_K=bk, num_stages=s, thread_num=128)
           for bm, bn, bk, s in itertools.product(
               [64, 128], [64, 128], [32, 64], [1, 2, 3])
       ]
   ```
   （共 \(2\times2\times2\times3=24\) 个候选。）

3. **选一种入口发起调优**。
   - 装饰器风格：`@tilelang.autotune(configs=get_configs, warmup=25, rep=100, timeout=60)` 叠在 `@tilelang.jit(out_idx=[-1])` 上，调用 `matmul(M,N,K)`。
   - 程序化风格：仿照 [example_gemm_autotune.py:155-168](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/examples/gemm/example_gemm_autotune.py#L155-L168)，用 `AutoTuner.from_kernel(kernel, get_configs(M,N,K)).set_compile_args(...).set_profile_args(ref_prog=..., backend="event").run(warmup=3, rep=20)`。

4. **提供输入**。用 `with set_autotune_inputs(A, B, C):` 包裹调用，确保每个候选测的是同一份数据（[autotuning.md:136-139](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/docs/programming_guides/autotuning.md#L136-L139)）。

5. **记录结果**。打印 `result.config`（最优配置）与 `result.latency`（延迟）。再算一下 TFlops：\(2MNK / \text{latency} \times 10^{-9}\)。

6. **验证缓存**。第二次运行同样的调用，应直接命中缓存秒回（观察终端不再出现 "Parallel Compiling" 进度条），证明缓存键生效。

**预期结果**：得到一组「config → latency」对照，以及一个最优 `config`；第二次运行命中缓存。若无 GPU，请把第 3 步写成可运行脚本并标注「待本地验证」，重点完成第 1、2、4 步的代码与配置空间设计。

## 6. 本讲小结

- **调优 = 搜索 + 编译 + 测量 + 取优 + 缓存**。三个 frozen 数据类 `CompileArgs`/`ProfileArgs`/`AutotuneResult` 分别承载编译环境、基准环境与产物；缓存键把版本、源码、configs、编译/基准环境哈希进去，保证「改了什么就重调什么」。
- **两种等价入口**：`@tilelang.autotune(configs=...)` 装饰器（贴近日常写法，进程内 `_tuner_cache` 复用）与 `AutoTuner.from_kernel(...).set_*().run()` 程序化接口（脚本化、CI 友好）。二者共享同一个 `AutoTuner.run()`。
- **`run()` 是生产者-消费者流水线**：线程池并行编译候选（生产者），基准 worker 线程从队列取 kernel 做校验 + 测延迟（消费者），编译出一个喂一个。`warmup/rep` 控制测量精度，`timeout` 用 daemon 子线程 + join 实现软超时，`early_stop` 用估计延迟跳过明显慢的候选。
- **分组编译** 把多个候选的 device IR 合并、只跑一次 device codegen，把昂贵的 nvcc 调用次数降到 1/⌈group_compile_size⌉，目前限 CUDA + tvm_ffi。
- **`par_compile` 是底层批量编译引擎**，只编译不测量；`AutoTuner` 在它（或 grouped compile）之上叠加了校验、测量、选优与缓存。
- **环境变量**：`TILELANG_AUTO_TUNING_DISABLE_CACHE`（关 autotune 磁盘缓存）、`TILELANG_AUTO_TUNING_CPU_UTILITIES/CPU_COUNTS/MAX_CPU_COUNT`（控制并行度）、`TILELANG_DISABLE_CACHE`（关所有缓存的总开关）。

## 7. 下一步学习建议

- **讲义 u8-l2（Carver）**：手动定义配置空间是「穷举」，Carver 则基于硬件 arch 模型与 template 推荐「更可能快」的 tile 候选。读完本讲后，你会更理解 [example_gemm_autotune.py:48-77](https://github.com/tile-ai/tilelang/blob/c6294f07e3c9cb452e13ce5a18f2cfd9c218d81d/examples/gemm/example_gemm_autotune.py#L48-L77) 里 `with_roller=True` 那条路径的价值——它把搜索空间从笛卡尔积换成了硬件感知的少量优质候选。
- **讲义 u8-l3（Profiler）**：本讲的 `do_bench`、`warmup/rep`、`TensorSupplyType`、`event/cupti/cudagraph` 后端都来自 profiler，下一讲会深入拆解。
- **继续阅读源码**：想看清「候选如何变成 kernel」的全链路，可顺着 `AutoTuner.run()` → `_default_compile` → `CompileArgs.compile_program` → `tilelang.compile` → `cached()`（讲义 u4-l3）一路读下去。
- **实践延伸**：尝试为一个 FlashAttention 或 MLA kernel（见 `examples/flash_attention`、`examples/deepseek_mla`）定义配置空间并跑 autotune，体会大模型核心算子调优时的配置空间设计取舍。
