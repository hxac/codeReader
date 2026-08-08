# cute_op 自定义算子与编译缓存

## 1. 本讲目标

本讲承接上一讲（u2-l2 Softmax 前向内核逐行解读），把视线从「设备侧内核算的是什么」移到「主机侧是怎么把内核接到 PyTorch、并让它的编译结果可复用的」。

读完本讲你应该能够：

1. 说清 `@cute_op` 是如何把一个 CuTe-DSL 内核函数注册成 `torch.library` 自定义算子的，以及它为什么能让 `torch.compile` 正确地捕获这个算子。
2. 理解 `Softmax.compile` 这个静态方法为什么用一个带「符号维度」的假张量（fake tensor）就能编译出对任意 batch 都可复用的产物。
3. 看懂 `jit_cache` 装饰器的「内存 + 磁盘 `.o` 文件」两级缓存、它的缓存键、文件锁并发模型，以及源码指纹（fingerprint）如何让缓存随源码改动自动失效。

本讲不讲解归约内核内部的算法（那是 u2-l2 / u2-l4 的内容），只讲解「编译产物是怎么产生、缓存、并被 PyTorch 调用的」这条主机侧链路。

## 2. 前置知识

本讲假设你已经读过 u2-l2，知道 `Softmax` 类里有三个角色：

- `__call__`（`@cute.jit`）：主机侧编排者，推导 grid/block/cluster 并启动设备内核。
- `kernel`（`@cute.kernel`）：真正跑在 GPU 上的并行内核。
- `compile`（`@staticmethod`）：本讲的主角之一，负责把内核「编译成机器码」。

下面补充几个本讲会用到的、CuTe-DSL / PyTorch 生态的概念。

**自定义算子（custom op）与 `torch.library`。**
PyTorch 2.x 推荐用 `torch.library.custom_op` 把一个 Python 函数注册成命名算子（如 `quack::_softmax_fwd`）。注册之后，这个算子就被 `torch.compile`（Dynamo）当作图中的一个节点识别，而不是被「展开」成一堆 Python 调用。每个自定义算子还需要配一个「fake」（也叫 meta）实现，告诉 tracer 这个算子会输出什么形状——这是 tracing 时的占位逻辑。

**functionalization 与 `mutates_args`。**
`torch.compile` 背后有一套叫 functionalization 的机制，它要求图里每个算子都是「纯函数」（不原地改输入）。对于必须原地写输出的算子，注册时要声明 `mutates_args`，让 functionalization 知道哪些张量被改了，从而在追踪时正确模拟副作用。

**符号整数（symbolic int）与 fake tensor。**
`cute.sym_int()` 创建一个「编译期未知、运行期才确定」的整数。用符号维度构造的 CuTe 张量叫 fake tensor——它没有真实数据，只携带「形状、步长、对齐」这类元信息。把 fake tensor 喂给 `cute.compile`，编译器会把符号维度当成运行期变量，从而产出一个「该维度可变」的 cubin。

**TVM FFI 与 `.o` 文件。**
CuTe-DSL 把编译好的内核通过 `export_to_c` 导出成一个目标文件（`.o`），再由 `tvm_ffi` 加载并跨 FFI 边界调用。加载一个已有的 `.o` 大约只要 1 ms，而从头编译一个内核大约要 500 ms——这正是缓存 `.o` 文件的价值。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
|------|------|
| `quack/dsl/torch_library_op.py` | 定义 `@cute_op` 装饰器，把内核函数注册为 `torch.library` 自定义算子，并提供 eager 旁路。 |
| `quack/compile_utils.py` | 提供 `make_fake_tensor`，用符号维度构造编译用的 fake tensor。 |
| `quack/softmax.py` | `Softmax.compile` 静态方法与 `_softmax_fwd` 的 `@cute_op` 注册，是本讲的两条主线案例。 |
| `quack/cache/jit.py` | `jit_cache` 装饰器：内存 + 磁盘两级缓存、文件锁、源码指纹。 |
| `quack/cache/__init__.py` | 缓存运行期开关（`CACHE_ENABLED` / `CACHE_DIR` / `EXTRA_SOURCE_DIRS`）与严格的导入顺序。 |

一句话串联：`@cute_op` 负责「让 PyTorch 认识并正确追踪这个算子」，`compile` 负责「用 fake tensor 编译出可复用产物」，`jit_cache` 负责「把这个产物缓存到磁盘，下次直接加载」。

## 4. 核心概念与源码讲解

### 4.1 @cute_op：把内核注册为 torch.library 自定义算子

#### 4.1.1 概念说明

写好一个 CuTe-DSL 内核之后，希望它能像普通 PyTorch 函数一样被调用，而且最好能在 `torch.compile` 下无缝工作。`torch.library.custom_op` 就是 PyTorch 提供的标准注册入口。

但直接用 `custom_op` 有两个麻烦：

1. 每个算子都得手写一个「fake/meta」孪生函数（`_*_fake`）来告诉 tracer 输出形状。
2. 在 eager（非编译）模式下，每次调用都要穿过 `torch.library` 的分发 + functionalization 边界，开销约 60 微秒/次。对于像 rmsnorm 这种小而快的内存受限内核，这 60 微秒往往比内核本身还大。

`@cute_op` 就是针对 QuACK 的情况做的专用封装。QuACK 的算子有一个关键共性——**它们只原地修改输入张量，不返回新的形状**。这意味着：

- fake 实现可以是一个纯 `no-op`：没有新的输出形状要报告。
- 编译完全由 `jit_cache` 在真正执行时负责，不需要在 trace 期做任何事。

#### 4.1.2 核心流程

`@cute_op` 注册一次算子的流程：

```
@cute_op("quack::_softmax_fwd", mutates_args={"out"})
def _softmax_fwd(x, out): ...

  └─> torch.library.custom_op(name, fn, mutates_args=...)   # 注册真身
        └─> @op.register_fake: def _fake(...): return        # fake 是空操作
              └─> 返回 _EagerBypassOp(op, fn)                  # 一个可调用包装器
```

注册得到的不是原始 `op`，而是一个 `_EagerBypassOp` 包装器。它在被调用时做一个分支：

- **编译模式**（`torch.compiler.is_compiling()` 为真）：走真正的 `op`，让 Dynamo 把它捕获成图节点（用那个 no-op fake）。
- **eager 模式**：直接调用原始函数 `fn`，绕过分发 + functionalization 边界，省掉那 60 微秒。

#### 4.1.3 源码精读

先看 `cute_op` 装饰器本体：它用 `torch.library.custom_op` 注册函数，并附上一个空操作的 fake。

注册 `custom_op` 并绑定 no-op fake（[quack/dsl/torch_library_op.py:46-64](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/dsl/torch_library_op.py#L46-L64)）：

```python
def dec(fn: Callable) -> Any:
    kwargs: dict[str, Any] = {"mutates_args": mutates_args}
    if schema is not None:
        kwargs["schema"] = schema
    if device_types is not None:
        kwargs["device_types"] = device_types
    op = torch.library.custom_op(name, fn, **kwargs)

    @op.register_fake
    def _fake(*args, **kw):
        # Pure no-op: our ops only mutate their input tensors ...
        return

    return _EagerBypassOp(op, fn)
```

> 中文说明：`custom_op` 把 `fn` 注册为命名算子；`@op.register_fake` 把 `_fake` 绑定为它的 fake 实现，由于算子只原地改输入、无新形状输出，fake 直接 `return`（什么都不做）。最后返回 `_EagerBypassOp(op, fn)`。

模块顶部文档串专门解释了「为什么不依赖 `torch.compiler.is_compiling()` 来写 fake」以及「为什么不需要手写 fake 孪生」的设计取舍（[quack/dsl/torch_library_op.py:1-17](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/dsl/torch_library_op.py#L1-L17)）。

再看 `_EagerBypassOp` 的调用分支——这是「eager 旁路」的核心：

调用时按是否编译分流（[quack/dsl/torch_library_op.py:92-95](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/dsl/torch_library_op.py#L92-L95)）：

```python
def __call__(self, *args, **kwargs):
    if torch.compiler.is_compiling():
        return self._custom_op(*args, **kwargs)
    return self._init_fn(*args, **kwargs)
```

> 中文说明：编译期走 `self._custom_op`（被 Dynamo 捕获成图节点）；eager 直接走原始 `self._init_fn`，跳过分发边界。

类文档串量化了这个旁路的收益：rmsnorm 内核约 10 微秒，而穿过边界约 125 微秒，旁路对小内核意义很大（[quack/dsl/torch_library_op.py:70-85](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/dsl/torch_library_op.py#L70-L85)）。

最后看真实用例 `_softmax_fwd` 的注册与函数体：

`_softmax_fwd` 用 `@cute_op` 注册（[quack/softmax.py:193-207](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/softmax.py#L193-L207)）：

```python
@cute_op("quack::_softmax_fwd", mutates_args={"out"})
def _softmax_fwd(x: torch.Tensor, out: torch.Tensor) -> None:
    assert x.dim() == 2, "Input must be 2D"
    assert x.dtype in [torch.float16, torch.bfloat16, torch.float32], "Unsupported dtype"
    if x.numel() == 0:
        return
    N = x.size(1)
    dtype, out_dtype = [torch2cute_dtype_map[t.dtype] for t in [x, out]]
    Softmax.compile(dtype, out_dtype, N)(x, out)
```

> 中文说明：算子命名为 `quack::_softmax_fwd`，声明原地修改 `out`。函数体只做轻量校验、推导 `N` 与 dtype 映射，真正的活交给 `Softmax.compile(...)(x, out)`——先编译（命中缓存则直接加载），再用真实张量 `(x, out)` 调用编译产物。

注意返回类型是 `None`：因为没有新张量产生，结果直接写进被 `mutates_args` 标记的 `out`。这正是 fake 可以是 no-op 的原因。

#### 4.1.4 代码实践

**实践目标**：亲手确认「注册 + eager 旁路」两件事——算子确实进了 `torch.library`，且 eager 下走的是原始函数体。

**操作步骤**（源码阅读型实践，不需要 GPU）：

1. 打开 `quack/dsl/torch_library_op.py`，确认 `cute_op` 内部调用了 `torch.library.custom_op`，并且 `@op.register_fake` 的 `_fake` 函数体只有一句 `return`。
2. 打开 `quack/softmax.py` 的 `_softmax_fwd`，确认它被 `@cute_op("quack::_softmax_fwd", mutates_args={"out"})` 装饰。
3. 在 Python 里（需已安装 quack）执行：
   ```python
   import torch, quack
   # 算子应已注册到 torch.library
   print(torch.ops.quack._softmax_fwd.default)
   ```
4. 阅读 `_EagerBypassOp.__call__`，回答：为什么 `torch.compiler.is_compiling()` 这个分支判断是「正确且必要」的？

**需要观察的现象**：
- 第 3 步应打印出一个 `_C.OpOverload`（或 `OpOverloadPacket`）对象，证明算子确实在 `torch.ops.quack` 命名空间下可见。

**预期结果**：算子可被 `torch.ops.quack._softmax_fwd` 解析到。

**待本地验证**：第 3 步的具体打印格式随 PyTorch 版本变化，需在你本机的 PyTorch 版本下确认。

#### 4.1.5 小练习与答案

**练习 1**：如果某个新算子需要返回一个**新的**输出张量（而不是原地修改 `out`），`@cute_op` 当前的 no-op fake 还能用吗？

> **参考答案**：不能直接套用。no-op fake 的前提是「算子只改输入、不产生新形状」。若算子要返回新张量，fake 必须根据输入形状计算出输出形状并返回，否则 Dynamo tracing 时会拿到错误的（空）输出形状，导致图构建错误。

**练习 2**：为什么 eager 模式下要绕过 `torch.library` 的分发边界？

> **参考答案**：分发 + functionalization 边界每调用约 60 微秒，对小内核（如 rmsnorm 约 10 微秒）是主要开销。eager 下直接调用原始函数体省掉这部分，又因为算子只原地改输入、无新输出，绕过并不破坏正确性。

---

### 4.2 compile 静态方法与符号（fake）张量

#### 4.2.1 概念说明

`Softmax.compile` 是把「Python 写的 DSL 内核」变成「GPU 机器码」的入口。它解决一个关键问题：**如何只编译一次，就能服务任意 batch 大小？**

答案是「符号维度」。batch（行数 M）是运行期才知道的量；而 N（每行宽度）和 dtype 在编译期就已知，且它们会显著影响内核的 tile 划分、线程数、cluster 配置。所以策略是：

- **N 和 dtype 作为编译期常量**，写进 cubin（不同的 N/dtype 编译出不同的产物）。
- **batch 作为符号维度**，编译产物里的 batch 是个运行期变量，任何 batch 都能用同一个 cubin。

这样，缓存键只需要 `(dtype, out_dtype, N)`，batch 不参与缓存键——这正是「一次编译、任意 batch 复用」的实现基础。

#### 4.2.2 核心流程

`compile` 的工作流：

```
Softmax.compile(dtype, out_dtype, N)
  │
  ├─ batch_sym = cute.sym_int()                      # batch 用符号整数
  ├─ div = math.gcd(128 // dtype.width, N)            # 对齐/向量化相关的整除性
  ├─ x_cute, out_cute = fake_tensor(dt, (batch_sym, N), div)   # 构造两个 fake 张量
  └─ cute.compile(
         Softmax(dtype, N),          # 内核实例（N 已是编译期常量）
         x_cute, out_cute,           # fake 张量（batch 符号化）
         fake_stream(use_tvm_ffi_env_stream=True),   # 流由运行期 FFI 环境提供
         options="--enable-tvm-ffi",
     )  ──> 返回 compiled_fn（一个可调用对象）
```

`compiled_fn` 之后会被 `_softmax_fwd` 用真实张量调用：`Softmax.compile(dtype, out_dtype, N)(x, out)`。注意它只传 `(x, out)` 两个参数——编译时绑定的 `fake_stream` 带有 `use_tvm_ffi_env_stream=True`，意味着真正的 CUDA 流由调用方所在的 FFI 环境在运行期注入，编译产物不需要显式接收流。

`div`（divisibility）的作用：它告诉编译器「步长可以假定整除到 `div` 个元素」，从而允许更宽的向量化加载。`make_fake_tensor` 用它生成符号步长与假定对齐。

#### 4.2.3 源码精读

`Softmax.compile` 全貌（[quack/softmax.py:178-190](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/softmax.py#L178-L190)）：

```python
@staticmethod
@jit_cache
def compile(dtype, out_dtype, N):
    batch_sym = cute.sym_int()
    div = math.gcd(128 // dtype.width, N)
    x_cute, out_cute = [fake_tensor(dt, (batch_sym, N), div) for dt in [dtype, out_dtype]]
    return cute.compile(
        Softmax(dtype, N),
        x_cute,
        out_cute,
        cute.runtime.make_fake_stream(use_tvm_ffi_env_stream=True),
        options="--enable-tvm-ffi",
    )
```

> 中文说明：`batch_sym` 让 batch 维符号化；`div` 由 dtype 位宽与 N 取最大公约数得到；用 `fake_tensor`（即 `make_fake_tensor`）造出符号张量；`cute.compile` 把 `Softmax(dtype, N)` 实例的 `__call__`（`@cute.jit`）针对这些符号张量特化编译。注意 `@jit_cache` 包在最外层，意味着「编译结果」被缓存。

`softmax.py` 顶部的导入说明了 `fake_tensor` 与 `cute_op`、`jit_cache` 的来源（[quack/softmax.py:17-21](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/softmax.py#L17-L21)）：

```python
from quack.compile_utils import make_fake_tensor as fake_tensor
from quack.dsl import cute_op
...
from quack.cache import jit_cache
```

> 中文说明：`fake_tensor` 是 `make_fake_tensor` 的别名，`cute_op` 来自 `quack.dsl`（它是一个带副作用导入的子包，见 u1-l3），`jit_cache` 来自 `quack.cache`。

再看 `make_fake_tensor` 如何用符号步长构造 fake 张量（[quack/compile_utils.py:8-33](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/compile_utils.py#L8-L33)）：

```python
def make_fake_tensor(dtype, shape, divisibility=1, leading_dim=-1) -> Optional[cute.Tensor]:
    ...
    if leading_dim is not None and leading_dim < 0:
        leading_dim = len(shape) + leading_dim
    stride = tuple(
        cute.sym_int64(divisibility=divisibility) if i != leading_dim else 1
        for i in range(len(shape))
    )
    assumed_align = max(divisibility * dtype.width // 8, 1)
    return cute.runtime.make_fake_tensor(dtype, shape, stride=stride, assumed_align=assumed_align)
```

> 中文说明：`leading_dim=-1`（默认）表示最后一维（N）步长静态为 1（即 N 是连续维），其余维（batch）用 `sym_int64(divisibility=...)` 给出符号步长。于是 N 在编译期确定、batch 在运行期确定。`assumed_align` 由 `divisibility` 推出，用于声明对齐。

把两段串起来：`Softmax.compile` 调 `make_fake_tensor(dt, (batch_sym, N), div)`，其中 `leading_dim` 默认 -1，所以 N 是连续维、batch 是符号维——这正是产物能跨 batch 复用的根因。

`_softmax_fwd` 如何调用编译产物（[quack/softmax.py:205-207](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/softmax.py#L205-L207)）：

```python
    N = x.size(1)
    dtype, out_dtype = [torch2cute_dtype_map[t.dtype] for t in [x, out]]
    Softmax.compile(dtype, out_dtype, N)(x, out)
```

> 中文说明：运行期从真实张量取出 `N` 与 dtype，调用 `Softmax.compile(dtype, out_dtype, N)`（命中缓存时是约 1 ms 的 `.o` 加载），再用真实 `(x, out)` 执行。`x` 的真实 batch 在这一步才进入，而 cubin 早已为「任意 batch」编译好。

#### 4.2.4 代码实践

**实践目标**：跟踪 `Softmax.compile` → `cute.compile`，解释符号 batch 维如何让产物对任意 batch 复用，并回答缓存键里包含哪些字段。

**操作步骤**（源码阅读型实践）：

1. 在 `quack/softmax.py` 中定位 `Softmax.compile`（L178-190），确认 `batch_sym = cute.sym_int()` 且 `fake_tensor` 的形状是 `(batch_sym, N)`。
2. 在 `quack/compile_utils.py` 中定位 `make_fake_tensor`，确认 `leading_dim=-1` 使 N 成为静态连续维、batch 成为符号维。
3. 回答：若把 `batch_sym` 换成一个具体整数（如 `4`），会发生什么？
4. 回答：缓存键是 `("Softmax.compile", dtype, out_dtype, N)`（详见 4.3），batch 为什么**不**在键里？

**需要观察的现象 / 预期结果**：
- 符号维让编译器把 batch 当运行期变量，产物与 batch 无关；故同一 `(dtype, out_dtype, N)` 的所有 batch 共用一个 cubin、一个 `.o` 文件。
- 若 batch 写死成 `4`，产物只能服务 batch=4，缓存键隐式绑定到了具体 batch，复用性丧失，且不同 batch 反复编译。

**待本地验证**：若你本机有 GPU，可运行 `pytest tests/test_softmax.py -x -k "bfloat16"`，观察首次（冷）编译耗时与第二次（热）加载耗时的巨大差异。

#### 4.2.5 小练习与答案

**练习 1**：`div = math.gcd(128 // dtype.width, N)` 中，对 bfloat16（位宽 16）、N=4096，`div` 是多少？它的物理含义是什么？

> **参考答案**：`128 // 16 = 8`，`gcd(8, 4096) = 8`，故 `div = 8`。含义是步长可假定整除到 8 个元素，对应 16 字节对齐、可做宽度为 8 的向量化加载；它被传给 `make_fake_tensor` 的 `divisibility`，进而决定符号步长与 `assumed_align`。

**练习 2**：为什么 `compile` 用 `make_fake_stream(use_tvm_ffi_env_stream=True)` 而不是一个真实 CUDA 流？

> **参考答案**：编译期没有（也不应该依赖）具体的运行期流。`use_tvm_ffi_env_stream=True` 表示「真实流由调用方所在的 TVM FFI 环境在运行期注入」。这样编译产物只接收 `(x, out)`，流在执行时自动取自环境，既保持编译与具体流解耦，又能在任意流上运行。

---

### 4.3 jit_cache：内存 + 磁盘的两级缓存

#### 4.3.1 概念说明

`cute.compile` 每次约 500 ms，而加载一个现成的 `.o` 只约 1 ms。`jit_cache` 装饰器的任务就是：让「同一个编译键」在第二次及以后只花 1 ms。

它是一个两级缓存：

- **内存级**：进程内的一个字典 `cache`，键是调用参数。同进程第二次命中直接返回对象，零磁盘 IO。
- **磁盘级**：把编译产物 `export_to_c` 成一个 `.o` 文件落盘。跨进程、跨运行都能复用——CI 里持久化缓存就是靠它。

磁盘缓存有两个精巧设计：

1. **缓存键 + 源码指纹双层目录**。`.o` 的路径是 `<缓存根>/<源码指纹>/<键哈希>.o`。键哈希由 `(函数全限定名, *调用参数)` 决定；源码指纹则把整个 `quack` 包的源码、Python 版本、cutlass/tvm_ffi 版本都哈希进去。源码一改，指纹变，旧缓存自动失效。
2. **文件锁防并发重复编译**。多个 xdist worker 同时撞上同一个冷键时，用每键一个 `flock`：编译在**独占锁**内进行，其余 worker 等锁、看到 `.o` 出现后直接加载，避免 N 个进程重复编译同一个键。

此外，当存在「异步编译池」时，冷 miss 会被交给 CPU 子进程编译，当前调用直接抛 `CompilePending` 让调用方先干别的、稍后重试（本讲只点到为止，u8-l2 会深入）。

#### 4.3.2 核心流程

`jit_cache.wrapper(*args, **kwargs)` 的判断顺序：

```
wrapper(args):
  cache_key = args + sorted(kwargs)
  ┌─ 1. 内存字典命中?  ───────────────── yes ─> 返回 cache[cache_key]   (零开销)
  │
  ├─ 2. CACHE_ENABLED 关闭?  ─────────── yes ─> 进程内编译、只存内存、不落盘
  │
  ├─ 计算 sha = hash(qualname, *cache_key)
  ├─ 计算 cache_path = 缓存根 / 源码指纹
  ├─ o_path   = cache_path / f"{sha}.o"
  ├─ lock_path= cache_path / f"{sha}.lock"
  │
  ├─ 3. o_path 已存在?  ──────────────── yes ─> 共享锁内 load_module ─> 返回   (热路径 ~1ms)
  │      └─ 加载失败(损坏)?  ─> 删除 .o 当 miss 处理
  │
  ├─ 3b. 有异步编译池?  ──────────────── yes ─> 投递任务 / 抛 CompilePending
  │
  └─ 4. 独占锁内：
         ├─ 再次检查 o_path(防竞态) ── yes ─> 加载返回
         ├─ misses += 1; compiled = fn(*args)           # 真正编译 (~500ms)
         ├─ export_to_c 写到 .o.tmp.<pid> 再原子 rename  # 防止半成品 .o
         └─ 存内存并返回
```

「先乐观检查存在性、再共享锁加载」（步骤 3）是热路径优化；「编译在独占锁内、锁内再复查」（步骤 4）是防并发重复编译的关键。

#### 4.3.3 源码精读

**缓存根目录的生成**：优先用 `QUACK_CACHE_DIR` 环境变量，否则落到临时目录下按用户名隔离的 `quack_cache`（[quack/cache/jit.py:47-53](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/cache/jit.py#L47-L53)）：

```python
def get_cache_path() -> Path:
    if _state.CACHE_DIR is not None:
        cache_dir = Path(_state.CACHE_DIR)
    else:
        cache_dir = Path(tempfile.gettempdir()) / getuser() / "quack_cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir
```

> 中文说明：`.o` 缓存根由 `CACHE_DIR` 决定，默认 `<tmp>/<user>/quack_cache`。

**源码指纹**：哈希整个 `quack` 包加运行期 ABI 戳（[quack/cache/jit.py:67-82](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/cache/jit.py#L67-L82)）：

```python
@functools.lru_cache(maxsize=1)
def _compute_source_fingerprint() -> str:
    h = hashlib.sha256()
    h.update(f"py{sys.version_info.major}.{sys.version_info.minor}".encode())
    h.update(f"cutlass={cutlass.__version__}".encode())
    h.update(f"tvm_ffi={tvm_ffi.__version__}".encode())
    import quack as _quack
    _hash_source_dir(h, Path(_quack.__file__).resolve().parent)
    for extra_dir in _state.EXTRA_SOURCE_DIRS:
        _hash_source_dir(h, Path(extra_dir).resolve())
    return h.hexdigest()
```

> 中文说明：指纹 = Python 主次版本 + cutlass 版本 + tvm_ffi 版本 + 整个 `quack` 包所有 `.py` 的内容（`_hash_source_dir` 递归哈希）+ 额外源码目录。`lru_cache(maxsize=1)` 让指纹只算一次。源码一改，指纹变，旧的 `<指纹>/` 子目录就再也命中不了，等价于自动失效。

`_hash_source_dir` 递归遍历目录下所有 `.py`，把相对路径与内容都喂给哈希（[quack/cache/jit.py:56-64](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/cache/jit.py#L56-L64)）。

**缓存键哈希**：把「函数全限定名 + 调用参数」pickle 后再 sha256（[quack/cache/jit.py:85-86](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/cache/jit.py#L85-L86)）：

```python
def _key_to_hash(key: tuple) -> str:
    return hashlib.sha256(pickle.dumps(key)).hexdigest()
```

> 中文说明：对 `Softmax.compile` 而言，`key = ("Softmax.compile", dtype, out_dtype, N)`，pickle 后哈希得文件名。dtype 与 N 在键里，batch 不在。

**`jit_cache` 装饰器主体**——内存命中与禁用缓存两段（[quack/cache/jit.py:160-184](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/cache/jit.py#L160-L184)）：

```python
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        nonlocal hits, misses
        cache_key = args + tuple(sorted(kwargs.items())) if kwargs else args
        enabled = _state.CACHE_ENABLED

        # 1. In-memory hit.
        if cache_key in cache:
            hits += 1
            return cache[cache_key]

        # 2. Cache disabled: pure in-process compile, no disk side effects.
        if not enabled:
            misses += 1
            compiled_fn = fn(*args, **kwargs)
            cache[cache_key] = compiled_fn
            return compiled_fn

        sha = _key_to_hash((fn.__qualname__,) + cache_key)
        cache_path = get_cache_path() / _compute_source_fingerprint()
        cache_path.mkdir(parents=True, exist_ok=True)
        o_path = cache_path / f"{sha}.o"
        lock_path = cache_path / f"{sha}.lock"
```

> 中文说明：`cache_key` = 调用参数（无 kwargs 时直接是 args）；内存字典命中就立即返回；`CACHE_ENABLED=0` 时只做进程内编译、不落盘。否则算出 `sha`、构造 `o_path = <根>/<指纹>/<sha>.o`。

**热路径**：乐观存在性检查 + 共享锁加载（[quack/cache/jit.py:205-224](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/cache/jit.py#L205-L224)）：

```python
        if o_path.exists():
            try:
                with FileLock(lock_path, exclusive=False, timeout=LOCK_TIMEOUT):
                    if o_path.exists():
                        try:
                            loaded = _load_cached()
                        except Exception as e:
                            _quarantine_corrupt(e)   # 损坏的 .o 当 miss，删掉重编
                        else:
                            cache[cache_key] = loaded
                            hits += 1
                            return loaded
            except RuntimeError:
                pass  # lock timeout; fall through to slow path
```

> 中文说明：`.o` 已存在时用**共享锁**加载，多个读者可并发；加载失败（如被 kill 留下的截断文件）则 `_quarantine_corrupt` 删掉它、当 miss 处理。

**慢路径**：独占锁内编译 + 原子落盘（[quack/cache/jit.py:275-341](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/cache/jit.py#L275-L341)，节选关键段）：

```python
            lock = FileLock(lock_path, exclusive=True, timeout=LOCK_TIMEOUT)
            lock.__enter__()
        ...
        try:
            if o_path.exists():        # 锁内复查，防「等锁期间别人已编完」
                ...
                return loaded
            misses += 1
            compiled_fn = fn(*args, **kwargs)              # 真正编译
            tmp_path = o_path.with_suffix(f".o.tmp.{os.getpid()}")
            compiled_fn.export_to_c(                       # 先写临时文件
                object_file_path=str(tmp_path),
                function_name=EXPORT_FUNC_NAME,
            )
            os.replace(tmp_path, o_path)                   # 再原子改名就位
            cache[cache_key] = compiled_fn
            return compiled_fn
        finally:
            lock.__exit__(None, None, None)
```

> 中文说明：编译在**独占锁**内进行；锁内复查 `.o` 是否已被别进程写好；导出时先写 `.o.tmp.<pid>` 再 `os.replace` 原子改名，避免被 kill 留下截断的半成品 `.o` 永久卡死缓存。装饰器文档串详细解释了「编译在锁内」相比「编译在锁外」如何消除冷缓存的 convoy 问题（[quack/cache/jit.py:131-154](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/cache/jit.py#L131-L154)）。

**运行期开关与严格的导入顺序**：`CACHE_ENABLED` / `CACHE_DIR` / `EXTRA_SOURCE_DIRS` 定义在包 `__init__.py`，且**必须**在导入 `quack.cache.jit` 之前定义（因为 `jit.py` 顶部 `import quack.cache as _state` 拿到的是部分初始化的包对象，运行期通过属性访问读这些名）（[quack/cache/__init__.py:22-43](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/cache/__init__.py#L22-L43)）：

```python
CACHE_ENABLED: bool = os.getenv("QUACK_CACHE_ENABLED", "1") == "1"
CACHE_DIR: Optional[str] = os.getenv("QUACK_CACHE_DIR", None)
EXTRA_SOURCE_DIRS: List[Path] = []
```

> 中文说明：`QUACK_CACHE_ENABLED=0` 关磁盘缓存、`QUACK_CACHE_DIR` 改缓存根、`EXTRA_SOURCE_DIRS` 让下游项目把自己的源码也纳入指纹。

#### 4.3.4 代码实践

**实践目标**：定位 `.o` 缓存目录的生成逻辑，并用环境变量验证缓存行为。

**操作步骤**：

1. 在 `quack/cache/jit.py` 中找到 `get_cache_path`（L47-53）与 `_compute_source_fingerprint`（L67-82），推导出某个 `Softmax.compile(bfloat16, bfloat16, 4096)` 产物的完整目录形如 `<tmp>/<user>/quack_cache/<指纹>/<sha>.o`。
2. 阅读 `tests/test_cache.py::test_jit_cache_lock_serializes_redundant_compiles`（L72-206），理解它如何用 N 个子进程撞同一冷键、并断言「编译体只被执行一次」（即独占锁生效）。
3. （需 GPU，待本地验证）设置一个独立缓存目录后运行两次 softmax：
   ```bash
   export QUACK_CACHE_DIR=/tmp/quack_demo_cache
   pytest tests/test_softmax.py -x -k "bfloat16"    # 第一次：冷编译，生成 .o
   pytest tests/test_softmax.py -x -k "bfloat16"    # 第二次：热加载
   ls -R $QUACK_CACHE_DIR                            # 观察到 <指纹>/<sha>.o
   ```

**需要观察的现象**：
- 第 3 步第一次运行较慢（约 500 ms/内核的编译），第二次明显变快（约 1 ms 加载）。
- 缓存目录下出现一层以源码指纹命名的子目录，里面有 `<sha>.o` 与 `<sha>.lock`。

**预期结果**：同一 `(dtype, out_dtype, N)` 只产生一个 `.o`；改 N（如 4096 → 8192）会产生另一个 `sha` 不同的 `.o`。

**待本地验证**：第 3 步的具体耗时与 `.o` 路径需在本机 GPU 环境确认；`QUACK_CACHE_ENABLED=0` 时应观察到不落盘（目录里不新增 `.o`）。

#### 4.3.5 小练习与答案

**练习 1**：为什么源码指纹要把**整个 `quack` 包**哈希进去，而不仅仅是 `quack/cache/`？

> **参考答案**：被缓存的是任意一个被 `@jit_cache` 装饰的编译函数的产物，它可能依赖 `quack` 包里任何地方的内核逻辑（如 `softmax.py`、`reduce.py`）。只要这些被依赖的源码改了，旧 cubin 就可能失效。哈希整个包能保证「源码一改、指纹一变、旧缓存自动作废」，比逐函数追踪依赖更简单也更安全。

**练习 2**：N 个 xdist worker 同时第一次请求同一个冷键，会发生几次真正的 `cute.compile`？

> **参考答案**：一次。慢路径在**独占锁**内编译并锁内复查 `.o`：第一个拿到锁的 worker 编译并落盘，其余 N-1 个等锁、进入锁内时复查发现 `.o` 已存在，直接加载返回。这正是 `test_jit_cache_lock_serializes_redundant_compiles` 断言的内容。

**练习 3**：导出 `.o` 时为什么要先写 `.o.tmp.<pid>` 再 `os.replace`，而不是直接写到目标路径？

> **参考答案**：防止进程在导出中途被 kill（如 xdist worker OOM、超时）留下截断的半成品 `.o`。一旦这种半成品留在最终路径，advisory `flock` 会随进程死亡而释放，后续所有运行都会加载到损坏文件而永久失败。先写临时文件再原子改名，保证目标路径上的 `.o` 要么不存在、要么完整。

---

## 5. 综合实践

**任务**：把本讲三个模块串起来，亲手验证「调用 → 注册 → 编译 → 缓存」一条龙。

**步骤**：

1. **注册侧**：在 `quack/softmax.py` 找到 `_softmax_fwd`，确认它被 `@cute_op("quack::_softmax_fwd", mutates_args={"out"})` 注册；在 `torch_library_op.py` 确认 `cute_op` 调用了 `torch.library.custom_op` 且 fake 是 no-op。用一句话写下：「为什么 `out` 必须出现在 `mutates_args` 里」。
2. **编译侧**：在 `Softmax.compile` 中确认 `batch_sym` 是符号维、`N` 是编译期常量。写下缓存键的三元组 `(dtype, out_dtype, N)`，并解释 batch 为何不参与键。
3. **缓存侧**：在 `jit.py` 中跟踪 `wrapper` 的四段判断（内存命中 / 禁用 / 热路径加载 / 独占锁编译），并写出 `.o` 的完整路径模板 `<缓存根>/<源码指纹>/<键哈希>.o`。
4. **实跑（需 GPU，待本地验证）**：
   ```bash
   export QUACK_CACHE_DIR=/tmp/quack_u2l6
   python -c "
   import torch, quack
   x = torch.randn(8, 4096, dtype=torch.bfloat16, device='cuda')
   quack.softmax(x)             # 冷：触发编译并落盘 .o
   quack.softmax(x)             # 热：内存字典命中
   "
   ```
   再把 N 改成 8192 跑一次，观察 `$QUACK_CACHE_DIR` 下出现**第二个** `.o`（因为 N 进了键、哈希不同）。

**预期结果**：
- 第 1 步：因为算子把结果原地写进 `out`、无新输出，故须声明 `mutates_args`，fake 才能 no-op。
- 第 2 步：键是 `(dtype, out_dtype, N)`，batch 符号化故不进键。
- 第 4 步：同一 N 复用一个 `.o`；不同 N 产生不同 `.o`。

**待本地验证**：第 4 步的 `.o` 数量与路径需在本机 GPU 环境确认。

## 6. 本讲小结

- `@cute_op` = 专用于 QuACK 的 `torch.library.custom_op` 封装：注册命名算子、绑一个 no-op fake（因为算子只原地改输入），并返回 `_EagerBypassOp` 在 eager 下绕过分发边界、在 `torch.compile` 下走真身被捕获成图节点。
- `Softmax.compile` 用 `cute.sym_int()` 把 batch 设为符号维、`N`/dtype 设为编译期常量，再用 `make_fake_tensor` 造 fake 张量喂给 `cute.compile`，于是产物对任意 batch 复用，缓存键只需 `(dtype, out_dtype, N)`。
- `make_fake_tensor` 让最后一维（N）步长静态为 1、其余维（batch）符号化，并把 `divisibility` 翻译成对齐与向量化假设。
- `jit_cache` 是「内存字典 + 磁盘 `.o`」两级缓存：热路径用共享锁加载（~1 ms），冷路径在独占锁内编译并原子落盘（~500 ms），损坏 `.o` 会被隔离重编。
- 磁盘路径为 `<缓存根>/<源码指纹>/<键哈希>.o`：源码指纹哈希整个 `quack` 包以实现「源码改→缓存失效」，键哈希由 `(函数全限定名, *参数)` pickle 而来。
- 运行期开关 `QUACK_CACHE_ENABLED` / `QUACK_CACHE_DIR` / `EXTRA_SOURCE_DIRS` 定义在 `quack.cache.__init__`，且其定义顺序与子模块导入顺序强绑定。

## 7. 下一步学习建议

本讲建立了「主机侧如何注册、编译、缓存内核」的认知。接下来建议：

- **横向延伸**：阅读 `quack/rmsnorm.py`、`quack/cross_entropy.py`，对比它们各自的 `compile` 静态方法与 `@cute_op` 注册，确认这套模式在归约家族里是一致的。
- **纵向深入（缓存）**：进入 u8-l2「`.o` JIT 缓存与异步编译池」，深入学习 `quack/cache/async_compile.py` 的多 worker 并行编译池、`CompilePending` 的 defer-and-retry 机制，以及 `--async-compile=N` 如何让冷编译与测试重叠。
- **进入 GEMM 主机侧**：本讲的 `compile`/`jit_cache` 是 GEMM 体系主机侧的基础；学完 u8-l2 后可直接进入 u4-l1「GEMM 编译与计划缓存」，看更复杂的 `_compile_gemm` 如何在同一套缓存之上构建计划（plan）缓存。
