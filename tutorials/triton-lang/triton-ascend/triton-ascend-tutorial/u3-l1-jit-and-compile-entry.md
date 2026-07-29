# @triton.jit 与编译入口

## 1. 本讲目标

本讲是「Triton 编译流水线总览」单元的第一讲。在 u1-l4 中我们已经会用 `@triton.jit` 写一个 vector-add 并跑通它，但当时我们把它当成一个「黑盒」——只要写好 kernel、传进 grid，结果就出来了。

学完本讲，你应该能够：

1. 说清 `@triton.jit` 这个装饰器到底把一个普通 Python 函数变成了什么对象。
2. 跟着源码走完「调用 `kernel[grid](...)` → 触发编译 → 生成 TTIR」的完整调用链，并指出**编译在哪一行被触发**。
3. 回答本讲的核心问题：**TTIR 到底是在哪个阶段生成的？**（提示：它分两步，一步在 core，一步在 backend）
4. 认识 `triton.backends.compiler.BaseBackend` 这个抽象基类，理解 Ascend 后端是如何「插」进 Triton 的编译流程的。

本讲只看 **Python 到 TTIR 这一段**，即编译流水线的「入口」。后续 TTIR 如何变成 Linalg、如何变成 `.o`，分别在 u3-l2、u4-* 讲。

## 2. 前置知识

- **JIT（Just-In-Time，即时编译）**：函数不是提前编译好的，而是在「第一次被真正调用、参数类型已知」的那一刻才编译。Triton 采用 JIT，所以同一个 kernel 用不同形状/类型的参数调用时，可能会被编译多次（每种「特化」一份）。
- **TTIR（Triton IR）**：Triton 自己定义的中间表示（基于 MLIR）。它是「硬件无关」的——TTIR 里没有任何 GPU 或 NPU 的概念，只有 block、program、`tl.load` 之类的抽象。把 Python 翻译成 TTIR，是 Triton **core**（目标无关部分）的职责。
- **后端（backend）**：把硬件无关的 TTIR 一步步「下降（lowering）」成某类硬件能跑的二进制。Ascend 就是这样一个后端。后端用一个继承 `BaseBackend` 的类来表示。
- **AST（抽象语法树）**：Python 解释器把源码解析成的树形结构。Triton 编译 kernel 的第一步，就是遍历 kernel 函数的 AST，边遍历边「吐」出 TTIR。
- **特化（specialization）**：JIT 编译时，Triton 会根据参数的某些特征（是否是 16 的倍数、是否是 constexpr 常量等）为同一份 kernel 生成不同的编译产物。这部分细节本讲只点到为止。

如果你对 `grid`、`program`、`tl.load/tl.store` 还不熟，请先复习 u1-l4。

## 3. 本讲源码地图

本讲涉及的文件都在 **Triton core**（`python/triton/`）下，目标无关；只有在最后讲 `BaseBackend` 的「具体实现」时，才会引用一处 Ascend 后端代码作为印证。

| 文件 | 作用 |
| --- | --- |
| [python/triton/__init__.py](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/python/triton/__init__.py) | `triton` 包的入口，把 `jit`、`JITFunction`、`compile` 等名字导出给用户。 |
| [python/triton/runtime/jit.py](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/python/triton/runtime/jit.py) | `@triton.jit` 装饰器、`JITFunction`、`KernelInterface` 的全部实现。本讲的主战场。 |
| [python/triton/compiler/compiler.py](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/python/triton/compiler/compiler.py) | `compile()` 函数、`ASTSource`、`make_backend`、`CompiledKernel`。编译流程的总调度。 |
| [python/triton/backends/compiler.py](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/python/triton/backends/compiler.py) | `BaseBackend` 抽象基类、`GPUTarget` 数据类。后端的「契约」。 |
| [python/triton/compiler/code_generator.py](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/python/triton/compiler/code_generator.py) | `ast_to_ttir()`——真正把 Python AST 翻译成 TTIR 的地方。 |
| [third_party/ascend/backend/compiler.py](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/compiler.py) | `AscendBackend`（`BaseBackend` 的子类）与 `make_ttir`，用来印证 core↔backend 的衔接。 |

> 提醒（承接 u1-l2）：前五个文件是 **Triton core**，最后一个属于 **third_party/ascend**。本讲要讲的就是这两者如何衔接。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

1. **4.1 `@triton.jit` 装饰器**：装饰器把函数变成了什么。
2. **4.2 `JITFunction` / `KernelInterface`**：运行时如何触发编译。
3. **4.3 `BaseBackend` 抽象与编译入口**：`compile()` 如何调度，TTIR 在哪里生成。

### 4.1 `@triton.jit` 装饰器

#### 4.1.1 概念说明

当你写下：

```python
@triton.jit
def add_kernel(x_ptr, y_ptr, output_ptr, n_elements, BLOCK_SIZE: tl.constexpr):
    ...
```

`@triton.jit` 不是一个「立刻编译」的指令。它只是一个**装饰器**：把 `add_kernel` 这个普通 Python 函数对象，**替换**成一个 Triton 能识别的「待编译 kernel」对象（`JITFunction`）。真正的编译发生在你**第一次调用** `add_kernel[grid](...)` 的时候。

这里有一个细节：装饰器还支持「解释器模式」。如果设置了环境变量 `TRITON_INTERPRET`，`@triton.jit` 会返回一个 `InterpretedFunction`——它不编译、不上 NPU，而是用纯 Python 逐元素模拟 kernel 行为，常用于精度对照（见 u10-l1）。默认情况下走的是 `JITFunction` 真编译路径。

#### 4.1.2 核心流程

```text
@triton.jit                  # 1. 装饰器被调用，传入函数 fn
def add_kernel(...): ...      #    此时 add_kernel 还没有被编译

       │
       ▼
jit(fn)  →  decorator(fn)     # 2. 判断是否处于解释器模式
       │
       ├── TRITON_INTERPRET=1 → 返回 InterpretedFunction(fn)   # 纯 Python 模拟
       │
       └── 否则             → 返回 JITFunction(fn)             # 待编译 kernel 对象
                                                                         │
 add_kernel 这个名字，现在指向这个 JITFunction 对象 ──────────────────┘
```

注意：`JITFunction` 在构造时**不会**编译。它只是把函数的源码、签名、参数信息缓存起来，并把名字 `add_kernel` 重新绑定到这个新对象上。编译是延迟到调用时的。

#### 4.1.3 源码精读

装饰器本体在 [python/triton/runtime/jit.py:893-945](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/python/triton/runtime/jit.py#L893-L945)，关键的内层 `decorator` 如下：

[python/triton/runtime/jit.py:922-939](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/python/triton/runtime/jit.py#L922-L939) —— 根据解释器开关，决定返回 `InterpretedFunction` 还是 `JITFunction`：

```python
def decorator(fn: T) -> JITFunction[T]:
    assert callable(fn)
    if knobs.runtime.interpret:                       # 读 TRITON_INTERPRET
        from .interpreter import InterpretedFunction
        return InterpretedFunction(fn, ...)
    else:
        return JITFunction(fn, ...)                   # 默认：待编译 kernel
```

`knobs.runtime.interpret` 的来源在 [python/triton/knobs.py:461](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/python/triton/knobs.py#L461)，它就是环境变量 `TRITON_INTERPRET` 的布尔映射：

```python
class runtime_knobs(base_knobs):
    interpret: env_bool = env_bool("TRITON_INTERPRET")
```

`@triton.jit` 还支持带括号的形式（如 `@triton.jit(do_not_specialize=[...])`）和不带括号的形式（如 `@triton.jit`），这在 [jit.py:941-945](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/python/triton/runtime/jit.py#L941-L945) 用「`fn is not None`」来区分（直接传了函数 vs. 只传了关键字参数）。

而 `JITFunction.__init__` 在构造时做了什么？见 [python/triton/runtime/jit.py:758-792](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/python/triton/runtime/jit.py#L758-L792)。它**不编译**，只做三件事：把每个参数包成 `KernelParam`、初始化一个空的 kernel 缓存 `device_caches`、记录调试选项。其中最关键的一行是：

[python/triton/runtime/jit.py:778](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/python/triton/runtime/jit.py#L778) —— 每个设备首次被访问时，懒加载一个 `binder`（4.2 节会讲）：

```python
self.device_caches = defaultdict(self.create_binder)
```

`defaultdict` 的意思是：当你第一次用某个 `device` 去 `self.device_caches[device]` 取值时，它会自动调用 `create_binder()` 初始化那一项。这是 Triton 「按设备缓存编译产物」的基础。

#### 4.1.4 代码实践

**实践目标**：亲手确认「`@triton.jit` 返回的是一个 `JITFunction` 对象，而不是普通函数，且构造时不编译」。

**操作步骤**（在装好 triton-ascend 的环境里）：

```python
import triton

@triton.jit
def my_kernel(x_ptr, BLOCK_SIZE: triton.language.constexpr):
    pass

print(type(my_kernel))        # 应输出 JITFunction（或其子类），而非 function
print(repr(my_kernel))        # JITFunction(__main__:my_kernel)
```

**需要观察的现象**：`type(my_kernel)` 不是 `<class 'function'>`，而是 `JITFunction`。同时整个定义过程**没有任何编译动作**（没有打印、没有卡顿），证明编译是延迟的。

**进阶**：设置 `TRITON_INTERPRET=1` 后重新运行，观察 `type(my_kernel)` 是否变成 `InterpretedFunction`。

**预期结果**：默认为 `JITFunction`；`TRITON_INTERPRET=1` 时为 `InterpretedFunction`。若环境无法运行，记为「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：`@triton.jit` 和 `@triton.jit(debug=True)` 在源码层面走的是同一段逻辑吗？
**答案**：是的。两者都进入 `decorator(fn)`。区别只在于 `jit()` 在 [jit.py:941-945](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/python/triton/runtime/jit.py#L941-L945) 通过判断 `fn is not None` 区分：不带括号时 `fn` 就是函数本体，直接 `return decorator(fn)`；带括号时 `fn` 为 `None`，先 `return decorator`（一个装饰器工厂），再由 Python 自动把它套到函数上。

**练习 2**：为什么 `JITFunction.__init__` 里不直接编译，而要用 `defaultdict(self.create_binder)` 懒加载？
**答案**：因为编译需要知道「目标设备」和「实际参数」，而这些在装饰时（定义函数时）都还不知道。懒加载保证了只有在真正用某设备调用 kernel 时，才为该设备建立对应的 binder 和编译缓存。

---

### 4.2 `JITFunction` / `KernelInterface`

#### 4.2.1 概念说明

`JITFunction` 同时承担两个角色：

- **一个可调用的「启动器代理」**：用户写 `add_kernel[grid](args)` 来启动 kernel，这个方括号语法由 `KernelInterface.__getitem__` 提供。
- **编译的触发者**：当缓存里没有对应的编译产物时，由 `JITFunction.run` 触发一次完整编译。

`KernelInterface` 是一个很薄的基类（[jit.py:355-371](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/python/triton/runtime/jit.py#L355-L371)），它只定义了 `__getitem__` 这个「记下 grid」的语法糖；真正的 `run` 逻辑由 `JITFunction` 实现（`JITFunction` 继承了 `JITCallable` 和 `KernelInterface`，见 [jit.py:606](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/python/triton/runtime/jit.py#L606)）。

#### 4.2.2 核心流程

从「用户调用」到「编译被触发」的链路：

```text
add_kernel[grid](x, y, out, n, BLOCK_SIZE)
        │
        │  KernelInterface.__getitem__(grid)  返回一个 lambda，
        │  它记住了 grid，等待被传入实际参数
        ▼
JITFunction.run(*args, grid, warmup=False, **kwargs)        # jip.py:702
        │
        ├── 1. driver.active.get_current_device() / get_current_stream()
        │      拿到当前 NPU 设备和流
        ├── 2. binder(*args, **kwargs)
        │      得到 bound_args、specialization（特化信息）、options
        ├── 3. compute_cache_key(...) → kernel_cache.get(key)
        │      查「这个特化 + 这个设备」有没有编译过
        │
        ├── 命中 → 直接用缓存的 kernel，跳到第 5 步
        └── 未命中 → JITFunction._do_compile(...)            # jit.py:833
                │
                ├── 构造 ASTSource(self, signature, constexprs, attrs)
                └── self.compile(src, target=target, options=...)   # ← 编译触发点！
                       （self.compile 就是 triton.compiler.compile，见 4.3）
        │
        ▼
   4.（编译返回 CompiledKernel 后）写入 kernel_cache[key]
        ▼
   5. kernel.run(grid_0, grid_1, grid_2, stream, ...)        # 真正在硬件上启动
```

**本模块的关键结论**：编译的「触发点」是 [jit.py:856](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/python/triton/runtime/jit.py#L856) 的 `self.compile(src, target=target, options=options.__dict__)` 这一行。`self.compile` 不是 `JITFunction` 自己的方法，而是在 `create_binder` 里被绑定成了 `triton.compiler.compile`（见 4.3）。

#### 4.2.3 源码精读

**方括号语法** —— [python/triton/runtime/jit.py:364-371](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/python/triton/runtime/jit.py#L364-L371)：

```python
def __getitem__(self, grid) -> T:
    """
    A JIT function is launched with: fn[grid](*args, **kwargs).
    Hence JITFunction.__getitem__ returns a callable proxy that
    memorizes the grid.
    """
    return lambda *args, **kwargs: self.run(grid=grid, warmup=False, *args, **kwargs)
```

这就是为什么 `kernel[grid](args)` 等价于 `kernel.run(grid=grid, warmup=False, args)`。`grid` 被「记住」，真正调用 `run` 的是返回的那个 lambda。

**运行与缓存查找** —— [python/triton/runtime/jit.py:702-753](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/python/triton/runtime/jit.py#L702-L753)。其中触发编译的核心片段：

[python/triton/runtime/jit.py:714-727](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/python/triton/runtime/jit.py#L714-L727)：

```python
kernel_cache, kernel_key_cache, target, backend, binder = self.device_caches[device]
bound_args, specialization, options = binder(*args, **kwargs)
key = compute_cache_key(kernel_key_cache, specialization, options)
kernel = kernel_cache.get(key, None)

# Kernel is not cached; we have to compile.
if kernel is None:
    options, signature, constexprs, attrs = self._pack_args(...)
    kernel = self._do_compile(key, signature, device, constexprs, options, attrs, warmup)
```

注意第一行：访问 `self.device_caches[device]` 会（首次）触发 `create_binder()`，于是 `target`、`backend`、`binder` 都在这一刻被懒加载出来。

**编译触发点** —— [python/triton/runtime/jit.py:833-860](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/python/triton/runtime/jit.py#L833-L860)（`_do_compile`）。它构造 `ASTSource`，然后调用编译：

```python
src = self.ASTSource(self, signature, constexprs, attrs)
...
kernel = self.compile(src, target=target, options=options.__dict__)   # ← 第 856 行，真正的编译入口
kernel_cache[key] = kernel
```

**`self.compile` / `self.ASTSource` 从哪来** —— [python/triton/runtime/jit.py:665-676](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/python/triton/runtime/jit.py#L665-L676)（`create_binder`）：

```python
def create_binder(self):
    from ..compiler import CompiledKernel, compile, ASTSource, make_backend
    target = driver.active.get_current_target()
    backend = make_backend(target)
    self.compile = compile            # ← self.compile = triton.compiler.compile
    self.ASTSource = ASTSource
    binder = create_function_from_signature(self.signature, self.params, backend)
    return {}, {}, target, backend, binder
```

这就把 4.2 和 4.3 串起来了：`JITFunction` 通过 `create_binder` 把「编译」这件事委托给了 core 的 `triton.compiler.compile`，同时用 `make_backend(target)` 选定了具体的后端（Ascend）。

#### 4.2.4 代码实践

**实践目标**：用源码阅读的方式，定位「编译触发点」和「缓存命中」两条路径。

**操作步骤**：

1. 打开 [python/triton/runtime/jit.py:702](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/python/triton/runtime/jit.py#L702)（`JITFunction.run`）。
2. 顺着读：第 714 行取缓存 → 第 719 行算 key → 第 720 行 `kernel_cache.get(key, None)`。
3. 回答：如果 `kernel is None`（第 723 行），代码走到第 727 行的 `_do_compile`；再进 [jit.py:856](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/python/triton/runtime/jit.py#L856)，看到 `self.compile(src, ...)`。
4. 回到 [jit.py:672](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/python/triton/runtime/jit.py#L672)（`create_binder` 内），确认 `self.compile = compile`，而这个 `compile` 来自 `from ..compiler import ... compile`。

**需要观察的现象**：你能画出一条「`kernel[grid](...)` → `__getitem__` → `run` → `_do_compile` → `triton.compiler.compile`」的调用链，并指出缓存命中时**不会**进入 `_do_compile`。

**预期结果**：调用链清晰可画；第二次用相同参数调用同一 kernel 时，`kernel_cache.get(key)` 命中，跳过编译，直接到 [jit.py:750-752](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/python/triton/runtime/jit.py#L750-L752) 的 `kernel.run(...)` 启动。

#### 4.2.5 小练习与答案

**练习 1**：`add_kernel[grid](x, y, out, n, BLOCK_SIZE=1024)` 和 `add_kernel[grid](x, y, out, n, BLOCK_SIZE=2048)` 会触发几次编译？
**答案**：通常**两次**。`BLOCK_SIZE` 是 `tl.constexpr`，不同值会产生不同的 specialization（特化 key 不同），`compute_cache_key` 算出的 key 不同，所以 `kernel_cache.get(key)` 两次都未命中，各编译一次。这正是 JIT「按特化缓存」的特点。

**练习 2**：`KernelInterface` 里的 `run` 方法为什么写 `raise NotImplementedError`（[jit.py:361-362](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/python/triton/runtime/jit.py#L361-L362)）？
**答案**：`KernelInterface` 是基类，只定义接口契约（`__getitem__` 记 grid、`run` 真正执行）。具体 `run` 由子类 `JITFunction`（真编译）和 `InterpretedFunction`（解释执行）各自实现，所以基类里抛 `NotImplementedError` 是为了防止有人直接调用未实现的基类 `run`。

---

### 4.3 `BaseBackend` 抽象与编译入口

#### 4.3.1 概念说明

`triton.compiler.compile` 是编译流程的**总调度**。它做三件事：

1. **选后端**：根据当前 target，找到唯一一个「支持该 target」的后端类（`make_backend`）。
2. **生成 TTIR**：调用 `src.make_ir(...)`，对 Python kernel 来说就是 `ast_to_ttir()`——遍历 Python AST，吐出 TTIR。
3. **跑各阶段 pass**：按后端用 `add_stages` 注册的顺序，依次把 TTIR 一步步下降（如 `ttir → ttadapter → npubin`）。

`BaseBackend` 是后端的**抽象基类**（契约）。它规定了任何后端都必须实现的方法：`supports_target`（你支持这个硬件吗）、`parse_options`（解析编译选项）、`add_stages`（注册编译阶段）、`load_dialects`（加载方言）、`get_module_map`、`hash`。Ascend 后端的 `AscendBackend` 就是它的子类。

> **本讲的核心答案（TTIR 在哪生成）**：TTIR 的生成分两步——
> - **第一步（core，目标无关）**：`compile()` 调用 `src.make_ir()` → `ast_to_ttir()`，遍历 Python AST 生成**原始 TTIR**。这一步完全不知道硬件是 GPU 还是 NPU。
> - **第二步（backend，Ascend 的第一个阶段）**：`make_ttir`（注册在 stages 字典的 `"ttir"` 键下）对原始 TTIR 跑一组标准优化 pass（inliner、cse、licm、loop unroll 等），产出**优化后的 TTIR**。
>
> 也就是说，「Python → TTIR」由 core 完成；「TTIR 的优化」由后端的第一个阶段 `make_ttir` 完成。这两步合起来，才是你能在 dump 里看到的 `kernel.ttir.mlir`。

#### 4.3.2 核心流程

`compile()` 的主干（[compiler.py:227-383](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/python/triton/compiler/compiler.py#L227-L383)）：

```text
compile(src=ASTSource, target=GPUTarget("npu",...), options=...)
   │
   ├── backend = make_backend(target)                    # 选后端 → AscendBackend
   │     actives = [x for x in backends if x.supports_target(target)]
   │     要求恰好 1 个；AscendBackend.supports_target → backend=="npu"
   │
   ├── options = backend.parse_options(...)              # 解析选项 → NPUOptions
   ├── (缓存查 metadata_group：命中则直接返回 CompiledKernel)
   │
   ├── stages = {}
   ├── backend.add_stages(stages, options, src.language) # 注册阶段
   │     stages = {"ttir": make_ttir, "ttadapter": ttir_to_linalg, "npubin": ...}
   │
   ├── first_stage = stages.keys().index(src.ext)        # src.ext == "ttir" → 0
   │
   ├── module = src.make_ir(target, options, ...)        # ★ 生成原始 TTIR（ast_to_ttir）
   │
   └── for ext, compile_ir in stages[first_stage:]:      # 依次下降
          module = compile_ir(module, metadata)          # 先 make_ttir，再 ttir_to_linalg，...
```

这里有一个容易看漏的细节（[compiler.py:290-293](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/python/triton/compiler/compiler.py#L290-L293)）：`first_stage` 从 `src.ext`（对 `ASTSource` 是 `"ttir"`）开始算。因为 `ASTSource` 不是「从 IR 文件读进来的」（`ir_source` 为 `False`），所以 `first_stage` 不 +1，stages 循环**从 `"ttir"` 阶段开始**——也就是说 `make_ttir` 一定会被执行。如果你传入的是一个现成的 `.ttir` 文件（`IRSource`），则 `first_stage` 会 +1，跳过 `make_ttir`，方便写 IR 级测试。

#### 4.3.3 源码精读

**`BaseBackend` 契约** —— [python/triton/backends/compiler.py:23-92](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/python/triton/backends/compiler.py#L23-L92)。其中两个最关键的抽象方法：

[python/triton/backends/compiler.py:30-33](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/python/triton/backends/compiler.py#L30-L33)（`supports_target`，决定后端是否匹配当前硬件）：

```python
@staticmethod
@abstractmethod
def supports_target(target: GPUTarget):
    raise NotImplementedError
```

[python/triton/backends/compiler.py:48-58](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/python/triton/backends/compiler.py#L48-L58)（`add_stages`，注册「阶段名 → 处理函数」的字典）：

```python
@abstractmethod
def add_stages(self, stages: dict, options: object) -> None:
    """
    Populates `stages` dictionary with entries of the form:
    ir_name [str] => Function[(src: str, metadata: dict) -> str|bytes]
    ...
    Stages will be run sequentially (in insertion order)
    """
```

**`GPUTarget` 数据类** —— [python/triton/backends/compiler.py:8-13](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/python/triton/backends/compiler.py#L8-L13)：只有三个字段 `backend`、`arch`、`warp_size`。对 Ascend，`backend="npu"`，由 [third_party/ascend/backend/driver.py:227-235](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/driver.py#L227-L235) 的 `NPUDriver.get_current_target` 构造。

**`make_backend` 选后端** —— [python/triton/compiler/compiler.py:386-391](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/python/triton/compiler/compiler.py#L386-L391)：

```python
def make_backend(target: GPUTarget) -> BaseBackend:
    actives = [x.compiler for x in backends.values() if x.compiler.supports_target(target)]
    if len(actives) != 1:
        raise RuntimeError(f"{len(actives)} compatible backends ... There should only be one.")
    return actives[0](target)
```

注意 `len(actives) != 1` 会报错：Triton 要求**恰好一个**后端匹配当前 target，避免歧义。

**`AscendBackend` 实现** —— [third_party/ascend/backend/compiler.py:1200-1204](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/compiler.py#L1200-L1204)：

```python
class AscendBackend(BaseBackend):
    @staticmethod
    def supports_target(target: GPUTarget):
        return target.backend == "npu"
```

[third_party/ascend/backend/compiler.py:1269-1290](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/compiler.py#L1269-L1290)（`add_stages`，注册阶段）：

```python
def add_stages(self, stages, options, language):
    if self.target.backend == "npu":
        stages["ttir"] = lambda src, metadata: make_ttir(src, metadata, options)   # ← 第一个阶段
        if options.force_simt_only:
            stages["npubin"] = lambda src, metadata: ttir_to_npubin(src, metadata, options)
            return
        stages["ttadapter"] = lambda src, metadata: ttir_to_linalg(src, metadata, options, named_ops=True)
        ...
        stages["npubin"] = lambda src, metadata: linalg_to_bin_...(src, metadata, options)
```

可以看到，`"ttir"` 这个阶段绑定到 `make_ttir`。`make_ttir` 本身在 [third_party/ascend/backend/compiler.py:132-152](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/compiler.py#L132-L152)，它跑的就是一组通用优化 pass：

```python
def make_ttir(mod, metadata, opt):
    pm = ir.pass_manager(mod.context)
    passes.common.add_inliner(pm)
    passes.ttir.add_combine(pm)
    passes.common.add_canonicalizer(pm)
    ...
    passes.common.add_licm(pm)
    passes.ttir.add_loop_unroll(pm)
    pm.run(mod, 'make_ttir')
    ...
    return mod
```

注释里写得很明白：「the same optimize pass for triton-ir as all other backends」——这是所有后端共用的 TTIR 优化，但因为它是 `compile()` stages 循环里由后端注册的第一个阶段，所以代码物理位置在 ascend 子树里（这正是 u1-l2 讲的「后端各自注册自己的阶段」）。

**TTIR 真正生成的地方：`ast_to_ttir`** —— `compile()` 在 [python/triton/compiler/compiler.py:305](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/python/triton/compiler/compiler.py#L305) 调用 `src.make_ir(...)`。对 `ASTSource`，[compiler.py:79-82](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/python/triton/compiler/compiler.py#L79-L82)：

```python
def make_ir(self, target, options, codegen_fns, module_map, context):
    from .code_generator import ast_to_ttir
    return ast_to_ttir(self.fn, self, context=context, options=options, ...)
```

而 [python/triton/compiler/code_generator.py:1659-1698](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/python/triton/compiler/code_generator.py#L1659-L1698) 的 `ast_to_ttir`，核心就是「构造一个 `CodeGenerator`，然后访问 Python AST」：

```python
generator = CodeGenerator(context, prototype, ..., jit_fn=fn, is_kernel=True, ...)
generator.visit(fn.parse())      # ← 遍历 AST，边走边发 TTIR op
module = generator.module
...
return module
```

`fn.parse()` 把 kernel 源码解析成 Python AST（定义在 [jit.py:530-535](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/python/triton/runtime/jit.py#L530-L535)），`CodeGenerator`（一个 AST 访问者）在访问每个节点时，把 `tl.load`、`tl.store`、`+`、`tl.program_id` 等「翻译」成对应的 TTIR MLIR 算子。这一步完全目标无关——它不知道也不关心下游是 GPU 还是 NPU。

> **小结一张表**：
>
> | 步骤 | 在哪 | 代码 | 产物 | 是否目标无关 |
> | --- | --- | --- | --- | --- |
> | Python → 原始 TTIR | core | `ast_to_ttir`（`src.make_ir`） | 未优化的 TTIR module | 是 |
> | TTIR 优化 | backend 首阶段 | `make_ttir`（stages 的 `"ttir"`） | 优化后的 TTIR | pass 通用，注册在后端 |
> | TTIR → Linalg → … → npubin | backend 后续阶段 | `ttir_to_linalg` 等 | `.o` 二进制 | 否（u3-l2、u4 讲） |

#### 4.3.4 代码实践（本讲主实践）

**实践目标**：在 jit 调用链里**亲手定位**两个点——(a) 编译在哪一行被触发；(b) TTIR 在哪个阶段生成。然后用 `TRITON_DEBUG` 把生成的 TTIR 文件 dump 出来佐证。

**操作步骤**：

1. **定位编译触发点**：从 [python/triton/runtime/jit.py:856](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/python/triton/runtime/jit.py#L856) 的 `self.compile(src, target=target, options=options.__dict__)` 出发，沿 `create_binder`（[jit.py:665-676](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/python/triton/runtime/jit.py#L665-L676)）确认 `self.compile` 就是 [python/triton/compiler/compiler.py:227](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/python/triton/compiler/compiler.py#L227) 的 `compile()`。
2. **定位 TTIR 生成**：在 `compile()` 里找到 [compiler.py:305](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/python/triton/compiler/compiler.py#L305) 的 `module = src.make_ir(...)`。点进 `ASTSource.make_ir`（[compiler.py:79-82](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/python/triton/compiler/compiler.py#L79-L82)），看到它调用 `ast_to_ttir`（[code_generator.py:1659](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/python/triton/compiler/code_generator.py#L1659)）。**结论：原始 TTIR 在 `src.make_ir()` 这一步生成**。
3. **定位 TTIR 优化**：再看 stages 循环 [compiler.py:324](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/python/triton/compiler/compiler.py#L324)，第一个执行的 `compile_ir` 就是 `make_ttir`（[ascend/compiler.py:132](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/compiler.py#L132)）。
4. **用 dump 佐证**（需要 NPU 环境）：设置 `TRITON_DEBUG=1`，跑 u1-l4 的 vector-add。`make_ttir` 会在 [ascend/compiler.py:147-150](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/compiler.py#L147-L150) 打印 `Dumping intermediate results to <目录>`，并在该目录下生成 `kernel.ttir.mlir`。

**需要观察的现象**：

- 步骤 1-3 是纯源码阅读，能画出 `run → _do_compile → compile → make_ir(ast_to_ttir) → stages(make_ttir → ttir_to_linalg → ...)` 的完整链路图。
- 步骤 4 打开的 `kernel.ttir.mlir` 里，能看到形如 `tt.func public @add_kernel(...)`、`tt.load`、`tt.store`、`tt.add` 的 TTIR 算子，印证「Python 被翻译成了 TTIR」。

**预期结果**：调用链图清晰；dump 出的 TTIR 与 vector-add 的 kernel 语义一一对应（3 个指针参数、`tt.load` 两次、`tt.add`、`tt.store` 一次）。若本机无 NPU，步骤 4 记为「待本地验证」，但步骤 1-3 的源码阅读结论不受影响。

#### 4.3.5 小练习与答案

**练习 1**：如果把一个写好的 `.ttir` 文件路径直接传给 `triton.compile(path)`（而不是 `@triton.jit` 函数），还会执行 `ast_to_ttir` 吗？
**答案**：不会。传字符串路径时，`compile()` 会把它包成 `IRSource`（[compiler.py:88](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/python/triton/compiler/compiler.py#L88)），此时 `ir_source=True`，于是 `first_stage` 会 +1（[compiler.py:292-293](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/python/triton/compiler/compiler.py#L292-L293)），跳过 `make_ttir`，直接从 `IRSource.make_ir`（它只 `parse_mlir_module`，不再走 AST）往下跑后续阶段。这正是「写 IR 级测试更方便」的原因。

**练习 2**：`make_backend` 为什么要强制 `len(actives) == 1`？
**答案**：因为编译流程需要唯一的后端来提供 `add_stages`、`parse_options` 等。如果同时有多个后端 `supports_target` 返回 True，Triton 无法决定用谁的阶段流水线，所以直接报错，要求环境里对同一 target 只有一个匹配后端。

**练习 3**：`make_ttir` 里跑的 pass（inliner、cse、licm…）是 Ascend 专有的吗？
**答案**：不是。源码注释明确写「the same optimize pass for triton-ir as all other backends」。这些是 MLIR/Triton 通用的标准优化 pass。它之所以写在 ascend 子树、并由 `AscendBackend.add_stages` 注册，是因为 `compile()` 的 stages 循环由后端驱动——后端必须自己把第一个阶段（哪怕是通用优化）注册进去。

## 5. 综合实践

把本讲三个模块串起来，完成一个「全链路追踪」小任务：

**任务**：针对 u1-l4 的 `add_kernel`，写一份**调用链说明文档**，要求覆盖以下每一个环节，并附上对应的源码永久链接和行号：

1. `@triton.jit` 把 `add_kernel` 变成 `JITFunction`（引用 [jit.py:922-939](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/python/triton/runtime/jit.py#L922-L939)）。
2. `add_kernel[grid](...)` 经 `__getitem__` 调到 `run`（引用 [jit.py:364-371](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/python/triton/runtime/jit.py#L364-L371)）。
3. `run` 查缓存未命中，进入 `_do_compile`（引用 [jit.py:723-727](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/python/triton/runtime/jit.py#L723-L727)）。
4. `_do_compile` 调 `self.compile`，经 `create_binder` 得知它就是 `triton.compiler.compile`（引用 [jit.py:665-676](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/python/triton/runtime/jit.py#L665-L676) 与 [compiler.py:227](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/python/triton/compiler/compiler.py#L227)）。
5. `compile` 用 `make_backend` 选中 `AscendBackend`（引用 [compiler.py:386-391](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/python/triton/compiler/compiler.py#L386-L391) 与 [ascend/compiler.py:1202-1204](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/compiler.py#L1202-L1204)）。
6. `src.make_ir()` → `ast_to_ttir` 生成**原始 TTIR**（引用 [compiler.py:79-82](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/python/triton/compiler/compiler.py#L79-L82) 与 [code_generator.py:1659-1698](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/python/triton/compiler/code_generator.py#L1659-L1698)）。
7. stages 第一个阶段 `make_ttir` 优化 TTIR（引用 [ascend/compiler.py:132-152](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/compiler.py#L132-L152)）。
8. （可选，若有 NPU）设 `TRITON_DEBUG=1` 运行，把 dump 出的 `kernel.ttir.mlir` 贴进文档，逐行标注它对应 kernel 的哪条语句。

**交付物**：一份 Markdown 文档，包含上面 8 个环节的「文字说明 + 源码链接 + 行号」。这个练习会把「装饰器 → 启动代理 → 编译触发 → 后端选择 → TTIR 生成与优化」整条链路牢牢焊在脑子里，是进入 u3-l2（`AscendBackend` 阶段注册细节）的前置必备。

## 6. 本讲小结

- `@triton.jit` 是装饰器，把普通函数替换成 `JITFunction`（或解释器模式下的 `InterpretedFunction`），**构造时不编译**。
- `kernel[grid](args)` 的方括号语法来自 `KernelInterface.__getitem__`，它返回一个记住了 grid 的 lambda，最终调到 `JITFunction.run`。
- `JITFunction.run` 按「特化 + 设备」查缓存；未命中才编译。**编译触发点是 `_do_compile` 里的 `self.compile(src, ...)`**，而 `self.compile` 就是 core 的 `triton.compiler.compile`（在 `create_binder` 里绑定）。
- `compile()` 是总调度：`make_backend(target)` 选中唯一的 `AscendBackend`（靠 `supports_target` 判断 `backend=="npu"`），再用 `add_stages` 注册阶段流水线。
- **TTIR 分两步生成**：原始 TTIR 由 core 的 `ast_to_ttir`（`src.make_ir`，遍历 Python AST）产出，目标无关；优化由后端首阶段 `make_ttir`（stages 的 `"ttir"` 键）完成，跑的是通用 inliner/cse/licm 等 pass。
- `BaseBackend` 是后端契约，规定了 `supports_target`、`parse_options`、`add_stages`、`load_dialects` 等必须实现的方法；Ascend 通过继承它「插」进 Triton 编译流程。

## 7. 下一步学习建议

本讲只走到「TTIR 生成与优化」就停了。接下来的学习路径：

1. **u3-l2 AscendBackend：阶段注册与 NPUOptions**：精读 `AscendBackend.add_stages`，搞清 `ttir → ttadapter → npubin` 各阶段如何串联、`force_simt_only` 分支有何不同、`NPUOptions` 有哪些关键字段。
2. **u3-l3 make_ttir、编译产物、元数据与缓存**：深入了解 `make_ttir` 之后产出的元数据（`kernel_name`、`tensor_kinds`、`mix_mode`）和编译缓存目录结构。
3. **u4 单元**：如果想直接看 TTIR 之后「怎么变成 Linalg、怎么变成 `.o`」，可以跳到 u4-l1（`ttir_to_linalg` pass 编排总览）。
4. **延伸阅读**：通读一遍 [python/triton/compiler/compiler.py:227-383](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/python/triton/compiler/compiler.py#L227-L383) 的 `compile()` 全文，理解缓存命中、override、dump、metadata 写回等机制——这些是日后调试编译问题（u10-l1）的基础。
