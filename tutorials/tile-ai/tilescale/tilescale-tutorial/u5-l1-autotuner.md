# Autotuner 框架

## 1. 本讲目标

TileLang 内置了一个自动调优器（autotuner）：给定一组候选配置，它会**并行编译**每个候选、**校验正确性**、**测延迟**，最后选出最快的那个，并把结果**缓存**起来下次复用。本讲讲清这个框架如何使用、内部如何运转。

学完本讲你应当能够：

1. 用 `@tilelang.autotune` 装饰器（叠加在 `@tilelang.jit` 之上）或 `AutoTuner.from_kernel(...)` 编程式 API，声明可调参数并启动一次调优。
2. 理解 `set_autotune_inputs` / `get_autotune_inputs` 的「线程本地栈」捕获机制，以及三种输入张量供给方式的优先级。
3. 看懂 `AutoTuner.run` 的完整流程：配置空间展开 → 并行编译池 → 超时评测 → 选最优。
4. 说清两级缓存（内存 + 磁盘）与 cache key 的构成，能读取/复用/导出调优结果。

本讲承接 u3-l6（JIT 适配器与运行时调用）：调优器复用了那里讲过的 `tilelang.compile`、`JITKernel`、`Profiler`、`TensorSupplyType`、`do_bench` 等组件，只是在外面包了一层「搜索 + 选优 + 缓存」。

## 2. 前置知识

- **tile 级 kernel 怎么写**：你已经能写出带 `T.Kernel`、`T.alloc_shared`、`T.copy`、`T.gemm`、`T.Pipelined` 的 kernel（u1-l3、u2）。可调参数（如 `block_M`、`num_stages`、`threads`）就是把 kernel 工厂函数里的这些常量变成**带默认值的函数参数**。
- **JITKernel 与 compile**（u3-l6）：`tilelang.compile(func)` 把一个 `PrimFunc` 编译成可调用的 `JITKernel`；`JITKernel.get_profiler().do_bench()` 用来测延迟。调优器就是对「同一个 kernel 的不同配置」反复调用这两步。
- **搜索空间 = 笛卡尔积**：如果你给 `block_M ∈ {64,128,256}`、`block_N ∈ {64,128,256}`、`block_K ∈ {32,64}`、`num_stages ∈ {0,1,2,3}`、`thread_num ∈ {128,256}`、`enable_rasterization ∈ {True,False}` 六个维度，那么总配置数为：

  \[ |C| = 3 \times 3 \times 2 \times 4 \times 2 \times 2 = 288 \]

  调优器要做的就是穷举这 \(|C|\) 个配置，测量每个的延迟，再取最优：

  \[ c^{*} = \arg\min_{c \in C} \mathrm{latency}(c) \]

- **线程本地存储（thread-local storage）**：调优器用 `ThreadPoolExecutor` 并发编译多个配置。如果用一个普通全局变量来传递「当前该用哪组输入张量」，多个工作线程会互相覆盖。`threading.local()` 让每个线程各持一份独立副本，互不干扰——这是输入捕获机制的底层原理。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [tilelang/autotuner/tuner.py](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/autotuner/tuner.py) | 调优器核心：`AutoTuner` 类（搜索+评测+缓存）、`AutoTuneImpl`、`@autotune` 装饰器。本讲的主角。 |
| [tilelang/autotuner/capture.py](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/autotuner/capture.py) | 输入张量捕获：`set_autotune_inputs` / `get_autotune_inputs` 及线程本地栈 `CaptureStack`。 |
| [tilelang/autotuner/param.py](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/autotuner/param.py) | 数据类 `CompileArgs`、`ProfileArgs`、`AutotuneResult`（含磁盘持久化 `save_to_disk` / `load_from_disk`）。 |
| [tilelang/autotuner/__init__.py](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/autotuner/__init__.py) | 对外导出 `autotune`、`AutoTuner`、`set_autotune_inputs`、`get_autotune_inputs`。 |
| [tilelang/jit/__init__.py](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/jit/__init__.py) | `JITImpl` 的 `__tune_params` 机制——`@autotune` 与 `@jit` 叠加的衔接点；另有手动扫查 `par_compile`。 |
| [examples/gemm/example_gemm_autotune.py](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/examples/gemm/example_gemm_autotune.py) | 编程式调优完整示例：`AutoTuner.from_kernel(...).set_compile_args(...).set_profile_args(...).run()`。 |
| [docs/programming_guides/autotuning.md](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/docs/programming_guides/autotuning.md) | 官方使用文档，含装饰器/编程两种工作流与最佳实践。 |

## 4. 核心概念与源码讲解

### 4.1 可调参数声明与 @autotune 装饰器

#### 4.1.1 概念说明

「调优」的本质是：把 kernel 里那些影响性能但不影响正确性的**编译期常量**（tile 大小 `block_M/N/K`、软件流水级数 `num_stages`、线程数 `threads`、是否 rasterization 等）暴露成**可调参数**，让调优器替你穷举取值、量延迟、挑最优。

TileLang 的设计是把可调参数写成**工厂函数的带默认值参数**。例如：

```python
@tilelang.jit(out_idx=[-1])
def matmul(M, N, K, block_M=128, block_N=128, block_K=32,
           num_stages=3, threads=128, ...):   # block_M/N/K/num_stages/threads 都是可调参数
    @T.prim_func
    def kernel(...):
        ...
    return kernel
```

调优器有两种用法：

- **装饰器式**：`@tilelang.autotune(configs=...)` 叠在 `@tilelang.jit` **之上**，调用函数即触发一次调优。
- **编程式**：手动构造 `AutoTuner.from_kernel(...).set_compile_args(...).set_profile_args(...).run()`，拿到 `AutotuneResult`。

两种用法底层最终都汇聚到 `AutoTuner.run`，本模块先讲声明与装饰器入口。

#### 4.1.2 核心流程

装饰器式调优的调用链如下（伪代码）：

```
@tilelang.autotune(configs=...)
@tilelang.jit(...)          # 先执行：把工厂函数包成 JITImpl
def matmul(M, N, K, block_M=128, ...): ...

matmul(M, N, K)             # 调用
   └─ AutoTuneImpl.__call__              # tuner.py
        ├─ 用 (args, kwargs) 构造进程内缓存 key
        ├─ 命中缓存 → 直接返回 best kernel
        └─ 未命中 → get_tunner() 构造 AutoTuner
                   ├─ set_profile_args(...)   # 测评参数
                   ├─ set_compile_args(...)   # 编译参数
                   └─ run()                   # 搜索 + 评测 + 落缓存
```

关键点：`@autotune` **必须**叠在 `@tilelang.jit` 之上。装饰器自下而上执行：`@tilelang.jit` 先把函数包成 `JITImpl` 对象，`@autotune` 再接收这个 `JITImpl`、返回一个 `AutoTuneImpl`（一个可调用对象）。两者的衔接靠 `JITImpl` 内部一个特殊参数 `__tune_params`。

#### 4.1.3 源码精读

`autotune` 装饰器入口在 [tilelang/autotuner/tuner.py:692-787](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/autotuner/tuner.py#L692-L787)。核心是它对被装饰对象的类型断言——只接受 `JITImpl`：

```python
def autotune(func=None, *, configs, warmup=25, rep=100, timeout=100, ...):
    ...
    def decorator(impl):
        assert isinstance(impl, JITImpl), \
            "The @autotune decorator can only be applied to @tilelang.jit decorated instances."
        return AutoTuneImpl(jit_impl=impl, configs=configs, ...)
    return decorator
```

这意味着如果忘了在下面先加 `@tilelang.jit`，运行时会立刻抛 `AssertionError`。

`AutoTuneImpl` 是装饰器式调优的核心，定义在 [tilelang/autotuner/tuner.py:628-689](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/autotuner/tuner.py#L628-L689)。它的 `__call__` 负责缓存 key 与构造 `AutoTuner`：

```python
def __call__(self, *args, **kwargs) -> JITKernel:
    key_args_tuple = args
    key_kwargs_tuple = tuple(sorted(kwargs.items()))
    key = (key_args_tuple, key_kwargs_tuple)              # 进程内缓存 key
    if key not in self._tuner_cache:
        def jit_compile(**config_arg):
            return self.jit_impl(*args, **kwargs, __tune_params=config_arg)  # ← 关键
        autotuner = self.get_tunner()
        autotuner.jit_compile = jit_compile
        autotuner.set_kernel_parameters(key, self.jit_impl.signature.parameters)
        artifact = autotuner.run()
        self._tuner_cache[key] = artifact.kernel
    return self._tuner_cache[key]
```

注意 `jit_compile` 调用时把候选配置 `config_arg`（如 `{"block_M":128, "num_stages":2, ...}`）通过 `__tune_params=config_arg` 传进 `JITImpl`。`__tune_params` 是一个普通用户不会碰的「内部通道参数」，在 [tilelang/jit/__init__.py:419-426](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/jit/__init__.py#L419-L426) 的 `JITImpl.__call__` 里被取出、并入编译参数：

```python
else:
    key = self.parse_cache_key(*args, **kwargs)
    tune_params = kwargs.pop("__tune_params", {})
    kernel = self._kernel_cache.get(key, None)
    if kernel is None:
        kernel = self.compile(*args, **kwargs, **tune_params)   # 不同 tune_params 编译出不同 kernel
        self._kernel_cache[key] = kernel
    return kernel
```

`parse_cache_key`（[tilelang/jit/__init__.py:385-391](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/jit/__init__.py#L385-L391)）会把 `tune_params` 也纳入 key，所以同一工厂函数下不同配置编译出的 kernel 互不覆盖。

`get_tunner()`（[tuner.py:649-673](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/autotuner/tuner.py#L649-L673)）把装饰器参数转成 `AutoTuner` 实例，并调 `set_profile_args` / `set_compile_args`（这两个方法见 4.3）。它还用 `partial(autotuner.run, self.warmup, self.rep, self.timeout)` 把 warmup/rep/timeout 预先绑死。

> **顺带一提（手动扫查）**：如果你不想用自动选优，只想批量编译一组配置、自己来 bench，可用 `JITImpl.par_compile(configs, num_workers=4)`（[tilelang/jit/__init__.py:312](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/jit/__init__.py#L312)）。它返回一组已编译 kernel，调优器内部的并行编译其实就是类似的线程池思路。

#### 4.1.4 代码实践

实践目标：验证「`@autotune` 必须叠在 `@tilelang.jit` 之上」这条规则，并观察装饰器式调优的最小可运行形态。

操作步骤：

1. 在本地写一个最小脚本 `my_autotune.py`，照搬 [docs/programming_guides/autotuning.md:38-76](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/docs/programming_guides/autotuning.md#L38-L76) 的装饰器示例（`@tilelang.autotune(configs=matmul_configs)` 叠在 `@tilelang.jit(out_idx=[-1])` 上）。
2. **故意**删掉中间那行 `@tilelang.jit(out_idx=[-1])`，再运行，观察报错。
3. 恢复后，用 `with set_autotune_inputs(A, B, C):` 包住调用 `matmul(M, N, K)` 运行一次。

需要观察的现象：

- 步骤 2 应抛出 `AssertionError: The @autotune decorator can only be applied to @tilelang.jit decorated instances.`——印证装饰器对 `JITImpl` 的类型断言。
- 步骤 3 首次运行会看到 tqdm 进度条（`Compiling configurations` 与 `Bench configurations`），并在工作目录生成 `autotuner.log`；第二次运行（同参数）几乎瞬间返回，因为命中进程内缓存 `self._tuner_cache`。

预期结果：步骤 3 返回一个 `JITKernel`，可用 `kernel(A, B, C)` 调用。具体延迟与最优配置取决于你的 GPU，**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：如果 `@autotune` 下面的不是 `@tilelang.jit` 而是一个普通函数，会发生什么？为什么？

> **答案**：会抛 `AssertionError`。因为 `autotune` 的 `decorator(impl)` 里写了 `assert isinstance(impl, JITImpl)`，它只接受 `@tilelang.jit` 产出的 `JITImpl` 对象；普通函数或裸 `PrimFunc` 都不满足（裸 `PrimFunc` 还会单独抛一条 `ValueError`）。

**练习 2**：`AutoTuneImpl.__call__` 里把候选配置通过 `__tune_params=config_arg` 传给 `JITImpl`，而不是直接改工厂函数的实参。这样做有什么好处？

> **答案**：`__tune_params` 是一个被 `JITImpl.__call__` 专门识别的「内部通道」，它会被并入编译参数并计入 `parse_cache_key` 的 key。好处是：(1) 不污染用户可见的函数签名与调用语义；(2) 让「同一组业务参数、不同调优配置」编译出的 kernel 在 `JITImpl._kernel_cache` 中互不覆盖。

### 4.2 输入张量捕获：set_autotune_inputs / get_autotune_inputs

#### 4.2.1 概念说明

调优器要 bench 每个 kernel，就需要**输入张量**。问题在于：不同的配置 `block_M/N/K` 可能要求不同的输入 shape/dtype，而且你希望每个配置都在**同一批数据**上测量，结果才可比。TileLang 提供三种输入供给方式，按优先级从高到低：

1. **`with set_autotune_inputs(A, B, C):`**（推荐）：用一个上下文管理器「钉住」一组固定输入，所有配置共用它们，保证可复现。
2. **自定义 `supply_prog`**：一个根据 kernel 签名返回张量列表的回调函数。
3. **内置生成器 `supply_type`**：`TensorSupplyType.Auto/Integer/Uniform/Normal/Zero/...`，按 dtype 启发式生成（静态 shape 才可用）。

本模块专讲最高优先级的 `set_autotune_inputs`，它的实现是一个精巧的**线程本地栈**。

#### 4.2.2 核心流程

```
with set_autotune_inputs(A, B, C):       # __enter__: 把 [A,B,C] 压入「本线程的」栈
    tuned = matmul(M, N, K)
        └─ AutoTuner.run → set_profile_args
             └─ 检测到 get_autotune_inputs() 非 None
                  → 把 supply_prog 改写为 lambda _: get_autotune_inputs()
                  → target_fn 测评时 supply_prog(params) 返回 [A,B,C]
# __exit__: 弹栈
```

要点：

- 栈是**线程本地**（`threading.local()`）的。因为 `run` 用线程池并发，普通全局变量会被多线程互相覆盖。
- 栈结构支持**嵌套**（外层 `with` 包内层 `with`，`top()` 取最近一层），虽然实际很少嵌套。
- 捕获到的张量只在 `set_profile_args` 里被「读取一次」并固化进 `supply_prog`；一旦离开 `with` 块再调 `run` 就取不到了（栈已空）。

#### 4.2.3 源码精读

整个捕获机制在 [tilelang/autotuner/capture.py](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/autotuner/capture.py)，不到 130 行。线程本地栈的取得在 [capture.py:81-84](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/autotuner/capture.py#L81-L84)：

```python
_local = threading.local()

def _get_current_stack() -> CaptureStack:
    if not hasattr(_local, "capture_stack"):
        _local.capture_stack = CaptureStack()
    return _local.capture_stack
```

`CaptureStack` 就是对 Python list 的薄封装（`push/pop/top/size`）。`set_autotune_inputs` 返回的上下文管理器在 [capture.py:87-118](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/autotuner/capture.py#L87-L118)：

```python
class AutotuneInputsCapture:
    def __init__(self, tensors): self.tensors = tensors
    def __enter__(self): _get_current_stack().push(self)
    def __exit__(self, *exc): _get_current_stack().pop()

def set_autotune_inputs(*args):
    # 同时支持 set_autotune_inputs(a,b,c) 与 set_autotune_inputs([a,b,c])
    if len(args) == 1 and isinstance(args[0], (list, tuple)):
        tensors = list(args[0])
    else:
        tensors = list(args)
    return AutotuneInputsCapture(tensors)
```

读取端 `get_autotune_inputs` 在 [capture.py:121-126](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/autotuner/capture.py#L121-L126)，栈空时返回 `None`：

```python
def get_autotune_inputs():
    stack = _get_current_stack()
    return stack.top().tensors if stack else None
```

消费端在 `AutoTuner.set_profile_args`，[tuner.py:234-237](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/autotuner/tuner.py#L234-L237)：

```python
if get_autotune_inputs() is not None:
    if supply_prog is not None:
        logger.warning("`supply_prog` will be ignored as this program is under `with set_autotune_inputs` context.")
    supply_prog = lambda _: get_autotune_inputs()   # 覆盖为「直接返回捕获的张量」
```

这就是优先级的代码体现：一旦处于 `set_autotune_inputs` 上下文，即便你另外传了 `supply_prog`，它也会被忽略并发出一条 warning。

#### 4.2.4 代码实践

实践目标：亲手验证 `set_autotune_inputs` 的线程本地性，并对比它与 `supply_type` 的差异。

操作步骤：

1. 在一个脚本里直接调用捕获 API，不开任何 `with`：

   ```python
   from tilelang.autotuner import get_autotune_inputs
   print(get_autotune_inputs())        # 预期 None（栈空）
   ```

2. 用 `with` 包住一次打印：

   ```python
   import torch
   from tilelang.autotuner import set_autotune_inputs, get_autotune_inputs
   A = torch.empty(8, 8)
   with set_autotune_inputs(A):
       print(get_autotune_inputs() is A)   # 预期 True
   print(get_autotune_inputs())            # 预期 None（已弹栈）
   ```

3. （选做）在 `with` 块内再开一个 `with`，观察 `top()` 取到的是哪一层。

需要观察的现象：步骤 1 和退出 `with` 后都得到 `None`；`with` 内得到捕获的张量本身。嵌套时 `top()` 总返回最内层。

预期结果：与上述一致。注意这些调用本身不需要 GPU，可纯 CPU 验证。

#### 4.2.5 小练习与答案

**练习 1**：为什么捕获栈要用 `threading.local()` 而不能用模块级的一个普通 list？

> **答案**：因为 `AutoTuner.run` 用 `ThreadPoolExecutor` 并发编译/评测多个配置（见 4.3）。若用普通全局 list，多个工作线程同时 `push/pop` 会互相串改，读到错误的输入。`threading.local()` 让每个线程拥有独立的 `capture_stack`，互不干扰。

**练习 2**：如果你既写了 `with set_autotune_inputs(A,B,C):`，又在 `set_profile_args` 里传了 `supply_prog=my_fn`，最终评测用的是哪个？

> **答案**：用 `set_autotune_inputs` 捕获的 `[A,B,C]`。`set_profile_args` 检测到 `get_autotune_inputs()` 非 `None` 后，会把 `supply_prog` 覆盖成 `lambda _: get_autotune_inputs()`，并打印一条 warning 提示你的 `supply_prog` 被忽略了。

### 4.3 搜索与评测流程：AutoTuner.run

#### 4.3.1 概念说明

`AutoTuner.run` 是整个框架的「心脏」：它把一组候选配置变成一个最优 `JITKernel`。它要做四件事——

1. **展开配置空间**：把 `configs`（list[dict] 或 callable）规整成与工厂函数签名对齐的实参列表，非法 key 直接报错。
2. **并行编译**：用线程池并发编译所有候选（编译是最耗时的部分），CUDA 下每个工作线程固定到当前 device。
3. **逐个评测**：对每个编译成功的 kernel，构造 `Profiler`、（可选）做正确性校验、`do_bench` 测延迟，单个配置有超时保护。
4. **选最优并落缓存**：取延迟最小的作为 best，写回两级缓存。

#### 4.3.2 核心流程

```
AutoTuner.run(warmup, rep, timeout):
  1. 提取闭包自由变量（如外层的 M/N/K），并入 cache key 材料
  2. 若 configs 是 callable → configs(*kernel_parameters) 展开成 list[dict]
  3. generate_cache_key(...) → key；命中内存/磁盘缓存则直接返回
  4. 把每个 config dict 过滤成「仅与工厂函数签名匹配」的 kwargs；多余 key 报 ValueError
  5. 【快路径】若可调参数已被显式提供 → 跳过搜索，直接 JIT 编译返回
  6. 计算 num_workers（由 TILELANG_AUTO_TUNING_CPU_* 决定）
  7. ThreadPoolExecutor 并发提交 jit_compile(**config) → 收集 (kernel, config)
  8. 顺序遍历编译成功的 kernel：
        run_with_timeout(target_fn, timeout, kernel)
          ├─ profiler = kernel.get_profiler(supply_type)
          ├─ （可选）profiler.assert_allclose(ref_prog, ...) 正确性校验
          ├─ latency = profiler.do_bench(warmup, rep, input_tensors)
          └─ （可选）测 ref_prog 参考延迟
        记录 best_latency / best_config / best_kernel
  9. best_kernel.update_tuner_result(...)；构造 AutotuneResult；写缓存
```

「顺序评测、并发编译」是个重要设计：编译可以安全并发（各编译各的），但评测（尤其涉及 TMA 初始化）在线程里行为不稳定，所以评测用单线程 + `run_with_timeout` 超时保护（源码注释 [tuner.py:562-563](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/autotuner/tuner.py#L562-L563) 明确说明了这一点）。

#### 4.3.3 源码精读

**配置展开与校验**在 [tuner.py:460-473](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/autotuner/tuner.py#L460-L473)：每个 config 的 key 必须是工厂函数的形参，否则抛 `Unused keys in config`；一个 config 都没有则抛 `No configurations to tune`：

```python
for config in self.configs:
    new_kwargs = {}
    for name, _ in parameters.items():
        if name in config:
            new_kwargs[name] = config[name]
    unused_keys = set(config.keys()) - set(new_kwargs.keys())
    if len(unused_keys) > 0:
        raise ValueError(f"Unused keys in config: {unused_keys}")
    config_args.append(new_kwargs)
if len(config_args) == 0:
    raise ValueError("No configurations to tune, please check your `@autotune` decorator")
```

**「可调参数已被显式提供」快路径**在 [tuner.py:479-499](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/autotuner/tuner.py#L479-L499)：如果你调用时直接把某个可调参数（如 `block_M=128`）当作业务参数传了进去，调优器会跳过整个搜索，只用这一组配置编译一次。这正是文档里「想跳过调优就显式覆盖可调参数」提示的代码出处。

**并发数计算**在 [tuner.py:500-518](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/autotuner/tuner.py#L500-L518)，受三个环境变量控制（见 [tilelang/env.py:243-246](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/env.py#L243-L246)）：`TILELANG_AUTO_TUNING_CPU_COUNTS`（>0 时直接指定）、`TILELANG_AUTO_TUNING_CPU_UTILITIES`（默认 0.9，按可用 CPU 比例）、`TILELANG_AUTO_TUNING_MAX_CPU_COUNT`（上限）。

**并发编译池**在 [tuner.py:520-555](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/autotuner/tuner.py#L520-L555)，CUDA 可用时用 `cuda_device_wrapper` 把每个工作线程绑到当前 device：

```python
pool = concurrent.futures.ThreadPoolExecutor(max_workers=num_workers)
...
def cuda_device_wrapper(func, device):
    def inner(**config_arg):
        torch.cuda.set_device(device)
        return func(**config_arg)
    return inner

for i, config_arg in enumerate(config_args):
    compile_func = self.jit_compile
    if torch.cuda.is_available():
        device = torch.cuda.current_device()
        compile_func = cuda_device_wrapper(self.jit_compile, device)
    future = pool.submit(compile_func, **config_arg)
```

编译失败（如某配置超出 shared memory）不会终止整个调优，而是记 debug 日志后跳过（[tuner.py:553-555](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/autotuner/tuner.py#L553-L555)）。

**评测核心 `target_fn`**在 [tuner.py:380-458](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/autotuner/tuner.py#L380-L458)，关键是正确性校验与计时这几步（节选）：

```python
profiler = jit_kernel.get_profiler(tensor_supply_type=supply_type)
...
if (not skip_check) and (ref_prog is not None):
    if manual_check_prog is not None:
        profiler.manual_assert_close(ref_prog, input_tensors=self.jit_input_tensors, manual_check_prog=manual_check_prog)
    else:
        profiler.assert_allclose(ref_prog, input_tensors=self.jit_input_tensors,
                                 rtol=rtol, atol=atol, max_mismatched_ratio=max_mismatched_ratio)
latency = profiler.do_bench(warmup=warmup, rep=rep, input_tensors=self.jit_input_tensors)
```

`ref_prog` 是参考实现（如 `A @ B.T`），`rtol/atol/max_mismatched_ratio` 控制数值容差（默认 `1e-2 / 1e-2 / 1%`）。`do_bench` 即 u3-l6 讲过的 L2-flush + CUDA event 计时。

**超时保护 `run_with_timeout`**在 [tuner.py:55-64](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/autotuner/tuner.py#L55-L64)，用 POSIX `SIGALRM`：

```python
def run_with_timeout(func, timeout, *args, **kwargs):
    signal.signal(signal.SIGALRM, timeout_handler)
    signal.alarm(timeout)
    try:
        result = func(*args, **kwargs)
    finally:
        signal.alarm(0)
    return result
```

评测循环在 [tuner.py:558-580](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/autotuner/tuner.py#L558-L580)，超时或异常都「跳过该配置」而非崩溃，并实时更新 `best_latency`：

```python
for i in progress_bar:
    jit_kernel, config = results_with_configs[i]
    try:
        latency, ref_latency = run_with_timeout(target_fn, timeout, jit_kernel)
    except TimeoutException:
        logger.warning(...); continue
    except Exception:
        logger.warning(...); continue
    if latency < best_latency:
        best_latency, best_config, best_kernel = latency, config, jit_kernel
    tqdm.write(f"Tuned Latency {latency} with config {config} at index {i}")
```

最后在 [tuner.py:584-613](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/autotuner/tuner.py#L584-L613)：若无任何配置通过编译+评测，抛 `RuntimeError`；否则用 `best_kernel.update_tuner_result(latency, config, ref_latency)` 把结果贴回 kernel（[tilelang/jit/kernel.py:601-622](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/jit/kernel.py#L601-L622)），构造 `AutotuneResult` 并落缓存。

编程式用法的一个完整范例见 [examples/gemm/example_gemm_autotune.py:149-161](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/examples/gemm/example_gemm_autotune.py#L149-L161)：

```python
autotuner = (
    AutoTuner.from_kernel(kernel=kernel, configs=get_configs(M, N, K, with_roller))
    .set_compile_args(out_idx=[-1], target="auto")
    .set_profile_args(supply_type=tl.TensorSupplyType.Integer, ref_prog=ref_program, skip_check=False)
)
return autotuner.run(warmup=3, rep=20)
```

注意这里 `set_compile_args` / `set_profile_args` 都返回 `self`，所以可链式调用。

#### 4.3.4 代码实践

实践目标：理解 `run` 的「配置空间展开 → 报错 → 并发编译 → 评测」各阶段，亲手制造一次配置错误并解读日志。

操作步骤：

1. 复制 [examples/gemm/example_gemm_autotune.py](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/examples/gemm/example_gemm_autotune.py)。
2. 在 `get_configs` 的非 roller 分支（[example_gemm_autotune.py:78-107](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/examples/gemm/example_gemm_autotune.py#L78-L107)）里，故意把某个 config dict 的 key 改错，例如把 `"block_M"` 写成 `"blockM"`。
3. 用一组**小规模**参数运行以加快迭代：`python examples/gemm/example_gemm_autotune.py --m 512 --n 512 --k 512`（注意 `main` 内 `use_autotune` 被硬编码为 `True`）。
4. 恢复 key，再次运行；运行中观察终端 tqdm 与 `autotuner.log`。

需要观察的现象：

- 步骤 2 应抛 `ValueError: Unused keys in config: {'blockM'}`——印证配置 key 必须与工厂函数形参同名。
- 步骤 4 会看到两段进度条：`Compiling configurations`（并发编译，对应线程池阶段）与 `Bench configurations`（顺序评测），逐条打印 `Tuned Latency ... with config {...} at index i`，并实时显示 `best_latency`。

预期结果：最终打印 `result.config`（最优配置字典）与延迟，并算出 TFlops（见 [example_gemm_autotune.py:214-228](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/examples/gemm/example_gemm_autotune.py#L214-L228)）。具体数值**待本地验证**（依赖 GPU 型号与 SM 版本）。

> 若无 GPU，可改为「源码阅读型实践」：对照 [tuner.py:520-580](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/autotuner/tuner.py#L520-L580)，在纸面上跟踪 `config_args` 列表如何先被并发 `jit_compile`、再被顺序 `target_fn` 评测，标注每个 `continue`（编译失败 / 超时 / 异常）发生在哪一段。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `run` 用线程池**并发编译**，却用单线程**顺序评测**？

> **答案**：编译各配置互不依赖、且是整个调优最耗时的环节，并发能显著缩短墙钟时间。而评测阶段涉及 GPU kernel 启动与 TMA/warp 初始化，在多线程并发下行为不稳定（源码注释明确提到 `tma init may behave strangely with one thread`），因此评测改为顺序执行，并用 `run_with_timeout`（`SIGALRM`）给每个配置加超时保护。

**练习 2**：某个候选配置因 `block_M=256` 导致 shared memory 超限，编译失败。这会让整个调优崩溃吗？

> **答案**：不会。编译收集循环里用 `try/except` 包住 `future.result()`（[tuner.py:550-555](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/autotuner/tuner.py#L550-L555)），失败的配置只记一条 debug 日志然后跳过，不进入评测列表。只有当**所有**配置都失败（`best_kernel is None`）时，`run` 才在 [tuner.py:584-587](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/autotuner/tuner.py#L584-L587) 抛 `RuntimeError`。

### 4.4 结果缓存与复用

#### 4.4.1 概念说明

调优很贵（要编译几十上百个 kernel），所以结果必须能**缓存复用**。TileLang 用两级缓存：

- **内存缓存**（`AutoTuner._memory_cache`，进程内 dict）：同一进程内对同一 key 的重复调用瞬间命中。
- **磁盘缓存**（`$TILELANG_CACHE_DIR/autotuner/<key>/`）：跨进程、跨运行复用，存的是最优配置的完整产物（配置、源码、编译库、参数）。

是否启用由两个开关控制：全局 `TILELANG_DISABLE_CACHE` 与仅 autotune 的 `TILELANG_AUTO_TUNING_DISABLE_CACHE`（[tilelang/env.py:344-357](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/env.py#L344-L357)）。此外 `AutotuneResult` 还能手动 `save_to_disk` / `load_from_disk`，把调优结果导出/导入到任意目录，适合 CI 与多机分发。

#### 4.4.2 核心流程

```
run() 开头：
  key = generate_cache_key(...)          # sha256(version, op_params, 闭包自由变量,
                                        #        func_source, configs, compile_args, profile_args)
  if 缓存开启:
      if key in _memory_cache: return 内存结果
      result = load_from_disk(cache_dir/key)
      if result: 回填内存缓存; return 磁盘结果

run() 结尾（选出 best 后）：
  autotune_result = AutotuneResult(latency, config, ref_latency, libcode, func, kernel)
  if 缓存开启 且 后端支持: save_to_disk(cache_dir/key)
  _memory_cache[key] = autotune_result
```

cache key 的设计原则：**凡是会影响编译结果或评测结果的因素，都要进 key**——TileLang 版本、函数源码、可调参数默认值、闭包里的自由变量（如外层 `M/N/K`）、配置列表、编译参数、评测参数。这样改了任何一个，key 就变，触发重新调优。

#### 4.4.3 源码精读

cache key 生成在 [tuner.py:266-299](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/autotuner/tuner.py#L266-L299)：

```python
def generate_cache_key(self, parameters, extra_parameters):
    # 收集可调参数的默认值
    op_parameters = []
    for _, default_value in parameters.items():
        if default_value.default is not inspect.Parameter.empty:
            op_parameters.append(default_value.default)
    if self._kernel_parameters is not None:
        op_parameters += _normalize_param(self._kernel_parameters)

    func_source = inspect.getsource(self.fn)
    key_data = {
        "version": __version__,
        "op_parameters": tuple(op_parameters),
        "extra_parameters": extra_parameters,
        "func_source": func_source,
        "configs": self.configs,
        "compile_args": hash(self.compile_args),
        "profile_args": hash(self.profile_args),
    }
    key_string = json.dumps(key_data, sort_keys=True)
    return hashlib.sha256(key_string.encode()).hexdigest()
```

注意 `extra_parameters` 来自 `run` 开头对**闭包自由变量**的提取（[tuner.py:329-341](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/autotuner/tuner.py#L329-L341)）。这解决了一个陷阱：像 `def gemm(M,N,K): def kernel(...)` 这种嵌套写法里，`M/N/K` 是闭包变量而非函数参数，如果只看源码它们是符号化的、会导致 key 永远相同（即便 M 变了），所以要把它们的实际值单独抽出来进 key。

`CompileArgs` / `ProfileArgs` 自定义了 `__hash__`（[param.py:68-78](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/autotuner/param.py#L68-L78) 与 [param.py:117-128](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/autotuner/param.py#L117-L128)），各自用 sha256 over 其字段，保证 key 稳定。

`run` 里的缓存查询在 [tuner.py:348-367](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/autotuner/tuner.py#L348-L367)：先查内存、再查磁盘，且二者都受 `env.is_cache_enabled() and not env.is_autotune_cache_disabled()` 双开关控制：

```python
with self._lock:
    if env.is_cache_enabled() and not env.is_autotune_cache_disabled():
        if key in self._memory_cache:
            ...  # 命中内存缓存，发 warning 建议改用 @autotune
            return cached_result
        result = self._load_result_from_disk(key)
        if result is not None:
            self._memory_cache[key] = result   # 回填内存
            return result
```

落盘在 [tuner.py:604-611](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/autotuner/tuner.py#L604-L611)，注意 `torch`（DLPack）后端**不落盘**（它不持久化编译产物），只走内存缓存：

```python
if self.compile_args.execution_backend in ("torch",):
    logger.warning("DLPack backend does not support cache saving to disk.")
else:
    with self._lock:
        if env.is_cache_enabled() and not env.is_autotune_cache_disabled():
            self._save_result_to_disk(key, autotuner_result)
self._memory_cache[key] = autotuner_result
```

磁盘上每个 key 目录的文件由 `AutotuneResult.save_to_disk`（[param.py:358-388](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/autotuner/param.py#L358-L388)）与 `_save_kernel_to_disk`（[param.py:176-260](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/autotuner/param.py#L176-L260)）写出，文件名常量定义在 [param.py:24-35](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/autotuner/param.py#L24-L35)：

| 文件 | 内容 |
| --- | --- |
| `best_config.json` | 最优配置字典 |
| `latency.json` | 最优延迟与参考延迟 |
| `function.pkl` | cloudpickle 序列化的 PrimFunc |
| `device_kernel.cu` | 设备端 kernel 源码 |
| `host_kernel.cu` | 主机端启动器源码 |
| `params.pkl` | `KernelParam` 列表 |
| `executable.so` / `kernel_lib.so` / `kernel.cubin` | 编译产物（按 execution_backend 不同而不同） |

所有写盘都用「写临时文件 + `os.replace` 原子替换」（`_safe_write_file`，[param.py:157-166](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/autotuner/param.py#L157-L166)），避免多进程并发时读到半截文件。`load_from_disk`（[param.py:390-448](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/autotuner/param.py#L390-L448)）反向把这些文件读回、用 `JITKernel.from_database` 重建 kernel。

#### 4.4.4 代码实践

实践目标：验证缓存命中，并查看磁盘缓存目录的实际内容。

操作步骤：

1. 确保未禁用缓存（不设 `TILELANG_DISABLE_CACHE` / `TILELANG_AUTO_TUNING_DISABLE_CACHE`）。
2. 运行一次调优（用小规模参数），记录耗时（主要花在 `Compiling configurations` + `Bench configurations`）。
3. 查看磁盘目录 `~/.tilelang/cache/autotuner/`（默认 `TILELANG_CACHE_DIR`，见 [env.py:228](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/env.py#L228)），其下应有以 sha256 哈希命名的子目录，内含上表所列文件。用编辑器打开 `best_config.json` 与 `latency.json`。
4. **再次运行同一调优**，对比耗时。
5. （选做）设 `TILELANG_AUTO_TUNING_DISABLE_CACHE=1` 再运行，确认会重新编译评测。

需要观察的现象：

- 第一次运行：有完整的编译+评测进度条，磁盘目录被写入。
- 第二次运行：几乎瞬间返回（命中内存或磁盘缓存），无/极少的编译进度。
- 步骤 5：又恢复成完整编译+评测。

预期结果：`best_config.json` 是一个形如 `{"block_M": 128, "block_N": 256, "block_K": 64, "num_stages": 3, "thread_num": 256, "enable_rasteration": true}` 的字典；`latency.json` 形如 `{"latency": ..., "ref_latency": ...}`。具体数值**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：你没有改 kernel 源码，只把函数闭包里的 `K` 从 1024 改成 2048，cache key 会变吗？为什么？

> **答案**：会变。`generate_cache_key` 不仅哈希 `func_source`，还把闭包自由变量（`run` 里提取的 `extra_parameters`，含 `K` 的实际数值）纳入 key 数据。`K` 从 1024 变 2048 会让 `extra_parameters` 改变，从而 sha256 改变，触发重新调优。这正是源码注释里强调的「若只提取源码，M/N/K 会符号化导致缓存问题」的对策。

**练习 2**：为什么 `torch`（DLPack）执行后端的调优结果不写磁盘缓存？

> **答案**：DLPack/Torch 后端不把编译产物（cubin/.so）持久化为可重新加载的二进制，落盘后无法用 `JITKernel.from_database` 重建，所以 `run` 末尾对 `execution_backend in ("torch",)` 专门跳过 `save_to_disk` 并发 warning。该后端只享受进程内内存缓存。若需要跨运行复用，应改用 `tvm_ffi`/`cython`/`nvrtc` 等后端。

## 5. 综合实践

把本讲的三个主题（声明可调参数、固定输入、缓存复用）串起来，做一个端到端的小任务：

**任务**：用**装饰器式** `@tilelang.autotune` 重写 [examples/gemm/example_gemm_autotune.py](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/examples/gemm/example_gemm_autotune.py) 中 `get_best_config` 的编程式调优，并满足：

1. 可调参数 `block_M/block_N/block_K/num_stages/threads` 以带默认值的形参出现在被 `@tilelang.jit` 装饰的工厂函数上（参考 [docs/programming_guides/autotuning.md:38-62](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/docs/programming_guides/autotuning.md#L38-L62) 的写法）。
2. 用 `with set_autotune_inputs(A, B, C):` 钉住固定输入，保证各配置在相同数据上评测。
3. 调用 `matmul(M, N, K)` 触发调优，拿到返回的 `JITKernel` 后用 `kernel.get_profiler().do_bench()` 再测一次，确认延迟与调优器报告的 `best_latency` 一致。
4. 第二次以相同参数调用，确认命中缓存（瞬间返回）；查看 `~/.tilelang/cache/autotuner/<key>/best_config.json`，与编程式 `get_best_config` 得到的最优配置对比是否相同。

**验收标准**：能说清「装饰器式与编程式两条路径最终都汇入 `AutoTuner.run`」；能解释你看到的 `best_config.json` 里每个字段的含义；能指出哪一步产生了 `autotuner.log`（答：`run` 开头 `_init_logger_handlers()`，[tuner.py:319](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/autotuner/tuner.py#L319)）。

> 提示：若 GPU 不可用，可降级为「源码阅读型」——对照本讲 4.1–4.4，在纸面上把装饰器调用链与缓存命中判定画成流程图，标注每一步对应的源码行号。

## 6. 本讲小结

- **声明可调参数**：把 tile 大小、`num_stages`、`threads` 等写成工厂函数的带默认值参数；`@tilelang.autotune(configs=...)` **必须**叠在 `@tilelang.jit` 之上，通过 `JITImpl` 的内部通道参数 `__tune_params` 把每个候选配置送进编译。
- **两条用法**：装饰器式（`@autotune`→`AutoTuneImpl`→`AutoTuner.run`）与编程式（`AutoTuner.from_kernel(...).set_*().run()`），底层都汇入 `AutoTuner.run`。
- **输入捕获**：`set_autotune_inputs` 用**线程本地栈**固定输入，优先级高于 `supply_prog` 与 `supply_type`；这是为了在并发编译下避免跨线程污染。
- **搜索流程**：`run` 先校验配置 key、再并发编译（线程池，CUDA 下绑定 device）、再顺序评测（`do_bench` + 可选 `assert_allclose` 正确性校验 + `SIGALRM` 超时），编译/超时/异常的配置自动跳过。
- **缓存复用**：cache key 哈希了版本、源码、可调参数默认值、**闭包自由变量**、configs、编译/评测参数；命中内存或磁盘缓存即跳过整个搜索；磁盘产物用原子写（`os.replace`），`torch` 后端不落盘。
- **产物可移植**：`AutotuneResult.save_to_disk` / `load_from_disk` 可把最优配置+源码+编译库导出到任意目录，适合 CI 与多机分发。

## 7. 下一步学习建议

- 下一讲 **u5-l2 Carver 与 Roller 代价模型**：本讲的 `get_configs(with_roller=True)`（[example_gemm_autotune.py:48-77](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/examples/gemm/example_gemm_autotune.py#L48-L77)）展示了如何用 Roller 给出「设备感知」的候选配置，把盲目笛卡尔积换成有针对性的搜索空间，正好承接。
- 若想深入**评测细节**，回看 u3-l6 的 `Profiler.do_bench`（L2 flush + CUDA event）与 `TensorSupplyType`，本讲的 `target_fn` 完全建立在其上。
- 若想做**手动扫查**而非自动选优，阅读 `JITImpl.par_compile`（[tilelang/jit/__init__.py:312](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/jit/__init__.py#L312)）并自己驱动 bench。
- 建议继续阅读 [docs/programming_guides/autotuning.md](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/docs/programming_guides/autotuning.md) 的「Example Gallery」与「Best Practices」，对照真实示例（如 `examples/deepseek_nsa`、`examples/gdn`）巩固。
