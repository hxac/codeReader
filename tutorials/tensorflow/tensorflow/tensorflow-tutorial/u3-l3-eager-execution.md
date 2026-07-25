# Eager 执行模式与 Context

## 1. 本讲目标

学完本讲，你应该能够：

- 说清楚 **Eager（即时）执行** 与 **Graph（图）执行** 两种模式的本质差异，以及为什么 TF2 把 Eager 设为默认。
- 理解 `Context` 这个「全局执行环境」的作用：它如何作为单例存在、如何延迟初始化 C++ 内核句柄、如何用线程局部状态记录「当前线程是否在 Eager」。
- 看懂 `execute.py` 中 op 的**立即派发**链路：一个 op 调用如何跨过建图步骤，直接进入 C++ 内核执行。
- 对照 u3-l2 讲过的 `Session.run`，理解 Eager 为何**用 `Context` 取代了 `Session`** 成为默认执行路径。

## 2. 前置知识

本讲承接 u2-l4（Operation 与 Tensor 的 Python 表示）和 u3-l2（Session 执行链路）。在继续前，请确认你已经理解以下概念：

- **Tensor 与 op 的关系**：一个 op 消费若干输入 Tensor、产出若干输出 Tensor（u2-l4）。
- **图模式下 `Session.run`** 的端到端流程：显式建图 → 放置 → 剪枝 → 分区 → 执行（u3-l2）。
- **pywrap 桥**：Python 通过 `pywrap_tfe` / `pywrap_tf_session` 调用 C++ 内核（u1-l4、u4-l4）。

几个本讲会反复用到的术语，先用一句话建立直觉：

- **Eager 执行（即时执行）**：op 一被调用就立刻执行，返回真实数值的 Tensor。像普通 Python 运算，可打断点、可 `print`。
- **Graph 执行（图执行）**：op 调用只是「在图里画一个节点」，直到 `Session.run` 或函数被真正调用时才执行。
- **Context（上下文）**：Eager 模式下管理「执行环境」的对象，相当于 Eager 世界里的「常驻 Session」。
- **线程局部状态（thread-local data）**：每个线程各持一份的状态，使不同线程可以分别处于 Eager 或 Graph 模式。

## 3. 本讲源码地图

本讲涉及两个核心源码文件，外加一个用于说明「op 如何分派」的代码生成器：

| 文件 | 作用 |
| --- | --- |
| [`tensorflow/python/eager/context.py`](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/eager/context.py) | Eager 执行的状态管理：定义 `Context` 类、全局单例、Eager/Graph 切换、设备、线程局部状态。 |
| [`tensorflow/python/eager/execute.py`](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/eager/execute.py) | op 的立即派发：把一个 op 调用翻译成对 C++ `TFE_Py_Execute` 的调用。 |
| [`tensorflow/python/framework/python_op_gen.cc`](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/framework/python_op_gen.cc) | 代码生成器：为每个 op 生成 `if tld.is_eager: ... else: 建图` 的分派代码（产物 `gen_*_ops.py` 在构建期生成）。 |

> 说明：仓库源码树里**没有** `gen_math_ops.py` 这类文件——它们由 `python_op_gen.cc` 在 Bazel 构建期生成。因此本讲引用生成器的源码来佐证「op 分派逻辑」，而不是编造一个不存在的文件。

## 4. 核心概念与源码讲解

### 4.1 Eager 与 Graph 两种执行模式的本质差异

#### 4.1.1 概念说明

在 TF1 时代，默认是 **Graph 模式**：写代码等于「搭积木」——每调用一次 op，只是在一张计算图里新增一个节点，并不真正计算。要拿到结果，必须把整张图交给 `Session.run` 去执行。这种模式性能好（可以整体优化、可序列化、可分布式部署），但调试痛苦：错误往往延迟到 `run` 时才暴露，Python 的 `print`、`if`、`for` 也无法自然嵌入。

**Eager 模式**（TF2 的默认）正好相反：op 一被调用就**立即执行**，立刻返回一个装满真实数值的 `EagerTensor`。你可以把它当成「带了 GPU 加速的 NumPy」：可以随时 `print(t)`、可以在 `if t > 0` 里分支、可以用 Python 调试器单步跟踪。

两者的差异可以用一句话概括：

> Graph 模式下，op 调用是「**声明**」（登记一个节点）；Eager 模式下，op 调用是「**执行**」（直接算出结果）。

这正是为什么 TF2 里 `import tensorflow as tf` 之后，`tf.executing_eagerly()` 默认返回 `True`，且**不再需要 `tf.Session()`**。

#### 4.1.2 核心流程

同一个 op（比如 `Add`）在两种模式下的命运完全不同：

```
                     用户写 c = a + b
                           │
            ┌──────────────┴──────────────┐
            ▼                             ▼
   Eager 模式(is_eager=True)      Graph 模式(is_eager=False)
            │                             │
  execute.quick_execute(...)        ops.create_op(...)
            │                       （在默认图里建一个 Add 节点）
  pywrap_tfe.TFE_Py_Execute(...)          │
            │                       返回 SymbolicTensor（符号占位）
   C++ 内核立即算出结果                      │
            │                       何时算？等到 Session.run / 函数被调用
   返回 EagerTensor（真实数值）
```

关键在于那个**分叉点**：op 的 Python 包装函数会先问一句「现在是不是 Eager？」，再决定走哪条路。这一问就是下面要讲的 `executing_eagerly()`。

#### 4.1.3 源码精读

**分叉点的开关：模块级常量 `GRAPH_MODE`/`EAGER_MODE` 与 `default_execution_mode`**

[context.py:55-58](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/eager/context.py#L55-L58) 定义了两种模式的整数标记，并依据 `tf2.enabled()` 决定默认模式。TF2 默认开启，所以 `default_execution_mode` 取 `EAGER_MODE`。

```python
GRAPH_MODE = 0
EAGER_MODE = 1

default_execution_mode = EAGER_MODE if tf2.enabled() else GRAPH_MODE
```

**`Context` 构造时把 `is_eager` 绑到线程局部状态**

[context.py:569-573](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/eager/context.py#L569-L573)：构造 `Context` 时，用一个线程局部对象 `EagerContextThreadLocalData` 持有 `is_eager`，其初值由上面的 `default_execution_mode` 决定。

```python
self._thread_local_data = pywrap_tfe.EagerContextThreadLocalData(
    self,
    is_eager=lambda: default_execution_mode == EAGER_MODE,
    device_spec=_starting_device_spec,
)
```

**`executing_eagerly()`：op 包装函数「问的那一句」**

[context.py:1170-1172](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/eager/context.py#L1170-L1172)：`Context` 的方法只读线程局性的 `is_eager`。

```python
def executing_eagerly(self):
  """Returns True if current thread has eager executing enabled."""
  return self._thread_local_data.is_eager
```

**代码生成器为每个 op 生成的分派代码**

[python_op_gen.cc:1721-1723](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/framework/python_op_gen.cc#L1721-L1723)：每个生成的 op 包装函数（如 `gen_math_ops.add`）开头都长这样——先取 `Context`，再读线程局性的 `is_eager`，命中则走 Eager 快路径。

```python
_ctx = _context._context or _context.context()
tld = _ctx._thread_local_data
if tld.is_eager:
  ...  # Eager 快路径：调用 _execute.execute(...)
# 否则：Graph 模式，建一个 op 节点
```

这正是「分叉点」在源码中的体现：生成代码先判断 `is_eager`，再决定是否调用本讲 4.3 的 `execute`。

#### 4.1.4 代码实践

**实践目标**：亲手观察同一行 `a + b` 在 Eager 与 Graph 两种模式下的行为差异。

**操作步骤**：

1. 打开一个 Python 解释器（已安装 TensorFlow 2.x）。
2. 运行以下代码（示例代码，非项目原有）：

   ```python
   import tensorflow as tf

   # ① 默认 Eager 模式
   print("executing_eagerly:", tf.executing_eagerly())   # True
   a = tf.constant([1.0, 2.0])
   b = tf.constant([3.0, 4.0])
   c = a + b
   print("c =", c)                                        # 立即得到 tf.Tensor([4. 6.], ...)

   # ② 切换到 Graph 模式（仅当前线程）
   with tf.compat.v1.graph_mode():
       print("executing_eagerly inside graph_mode:",
             tf.executing_eagerly())                     # False
       a2 = tf.constant([1.0, 2.0])
       b2 = tf.constant([3.0, 4.0])
       c2 = a2 + b2
       print("c2 =", c2)                                  # 只是一个 Tensor("add:0", ...) 符号节点
   ```

**需要观察的现象**：

- ① 中 `c` 直接打印出数值 `[4. 6.]`，说明 `a + b` 立即执行了。
- ② 中 `c2` 打印出来的是一个**符号 Tensor**（名字类似 `add:0`），没有数值——说明这次只建了节点、没执行。
- 离开 `graph_mode()` 块后，`tf.executing_eagerly()` 应回到 `True`。

**预期结果**：对照 [context.py:1152-1168](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/eager/context.py#L1152-L1168) 的 `_mode` 上下文管理器，它在进入时把 `is_eager` 设成目标值、退出时恢复旧值，这正是「仅当前线程、用完即恢复」的原因。

> 若你的环境未安装可运行的 TensorFlow，本步骤标注为「待本地验证」；但你仍可通过阅读上面的源码理解行为。

#### 4.1.5 小练习与答案

**练习 1**：为什么 TF2 选择把 Eager 设为默认，而不是保留 TF1 的 Graph 模式？

**参考答案**：Eager 让调试、控制流、错误定位都回归「普通 Python」体验，大幅降低入门门槛；性能损失由 `tf.function`（把 Eager 函数重新编译成优化后的图，见 u3-l4）弥补。即「默认易用，需要性能时显式建图」。

**练习 2**：`tf.compat.v1.graph_mode()` 是如何保证「退出 `with` 块后自动恢复 Eager」的？

**参考答案**：它返回 `context()._mode(GRAPH_MODE)`（[context.py:2720-2722](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/eager/context.py#L2720-L2722)），而 `_mode` 是个 `@contextmanager`（[context.py:1152-1168](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/eager/context.py#L1152-L1168)）：进入时记录 `old_is_eager` 并改成目标值，`finally` 块里恢复成 `old_is_eager`。

---

### 4.2 Context：Eager 执行的全局环境与单例

#### 4.2.1 概念说明

Graph 模式里，执行的「容器」是 `Session`——你显式创建、显式 `run`、显式 `close`。Eager 模式里没有 `Session`，取而代之的是一个**进程级单例 `Context`**。它扮演「常驻 Session」的角色，负责：

- 持有**C++ 内核的执行句柄**（一个不透明的 `TFE_Context*`），所有 op 执行都要借它派发。
- 维护**当前线程的状态**：是否 Eager、当前默认设备、scope 名等。
- 管理**物理/逻辑设备列表**、放置策略、JIT 开关等配置。

一句话：`Context` 是 Eager 模式的「执行环境大管家」。你通常不直接 `new` 它，而是通过模块级函数 `context()` 拿到那个唯一实例。

#### 4.2.2 核心流程

`Context` 的生命周期分两步走：

```
进程启动
   │
   ▼
_context = None          （模块级全局，初值为 None）
   │
   │ 首次调用 context() / ensure_initialized() / 任何 op
   ▼
_create_context()        →  new Context()  →  mark_as_global_context()
   │                        （纯 Python 对象，_context_handle 还是 None）
   ▼
_context 指向这个单例
   │
   │ 首次真正执行 op 时，op 包装调用 ctx.ensure_initialized()
   ▼
ensure_initialized()     →  TFE_NewContext(...)  （此时才真正创建 C++ 执行器）
   │                        + _initialize_logical_devices()
   ▼
_context_handle 被赋值，_initialized = True
   │
   ▼
之后每个 op 复用这个 C++ 句柄立即派发
```

两个关键设计：

1. **延迟初始化（lazy init）**：`Context` 对象可以很早就创建（只是个 Python 对象），但**真正的 C++ 执行器**要等到 `ensure_initialized()` 被调用时才建。这避免了进程启动即加载全部设备带来的开销。
2. **单例 + 锁**：`_context` 全局变量配合 `_context_lock`，保证多线程下也只创建一个全局 Context。

#### 4.2.3 源码精读

**单例：模块级 `_context` 与 `context()`**

[context.py:2487](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/eager/context.py#L2487)：全局变量初值为 `None`。

```python
_context = None
```

[context.py:2538-2542](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/eager/context.py#L2538-L2542)：`context()` 懒创建单例。

```python
def context():
  """Returns a singleton context object."""
  if _context is None:
    _create_context()
  return _context
```

[context.py:2503-2507](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/eager/context.py#L2503-L2507)：真正的创建在锁保护下完成，并标记为「全局 Context」。

```python
def _create_context():
  with _context_lock:
    if _context is None:
      ctx = Context()
      _set_context_locked(ctx)
```

[context.py:2491-2495](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/eager/context.py#L2491-L2495)：`_set_context_locked` 把 Context 同时注册给 C++ 侧（`TFE_Py_SetEagerContext`）并标记为全局。

```python
def _set_context_locked(ctx):
  global _context
  pywrap_tfe.TFE_Py_SetEagerContext(ctx)
  ctx.mark_as_global_context()
  _context = ctx
```

> 配套的 `context_safe()`（[context.py:2545-2547](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/eager/context.py#L2545-L2547)）返回当前 Context 或 `None`（不触发创建），用于「只想查询、不想强制初始化」的场景。

**延迟初始化：`ensure_initialized()`**

[context.py:698-749](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/eager/context.py#L698-L749)：核心逻辑——双重检查锁（double-checked locking），只在首次调用时真正创建 C++ 执行器。

```python
def ensure_initialized(self):
  """Initialize handle and devices if not already done so."""
  if self._initialized:
    return
  with self._initialize_lock:
    if self._initialized:          # 二次检查，避免重复初始化
      return
    ...
    opts = pywrap_tfe.TFE_NewContextOptions()
    ...  # 把 config / device_policy / tfrt / jit 等选项塞进 opts
    context_handle = pywrap_tfe.TFE_NewContext(opts)   # ← 真正创建 C++ 执行器
    ...
    self._context_handle = context_handle
    self._initialize_logical_devices()
    self._initialized = True
    if self._is_global_context:
      pywrap_tfe.TFE_Py_SetCEagerContext(self._context_handle)
```

注意三处细节：

- 先无锁判断 `_initialized`，命中直接返回，避免热路径上抢锁（性能）。
- 进入锁后再判断一次，防止两个线程同时通过第一次检查（正确性）。
- 最终把 C++ 句柄存进 `self._context_handle`，并通过 `_handle` 属性暴露（[context.py:1128-1133](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/eager/context.py#L1128-L1133)），未初始化时断言报错。

**线程局性的「当前设备」**

[context.py:1192-1200](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/eager/context.py#L1192-L1200)：`device_name`/`device_spec` 也是线程局性的——每个线程可以有自己的当前设备。

```python
@property
def device_name(self):
  return self._thread_local_data.device_name

@property
def device_spec(self):
  return self._thread_local_data.device_spec
```

这意味着 `with tf.device('/gpu:0'):` 只影响当前线程，不会污染别的线程。

**`executing_eagerly()` 公共 API**

[context.py:2575-2633](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/eager/context.py#L2575-L2633)：用户调用的 `tf.executing_eagerly()` 是个导出的模块函数，它先查单例是否存在（不存在就按 `default_execution_mode` 判断），再委托给 `Context.executing_eagerly()`。

```python
@tf_export("executing_eagerly", v1=[])
def executing_eagerly():
  ctx = context_safe()
  if ctx is None:
    return default_execution_mode == EAGER_MODE
  return ctx.executing_eagerly()
```

其 docstring（[context.py:2576-2627](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/eager/context.py#L2576-L2627)）清楚列出了它返回 `False` 的几种场景：在 `tf.function` 内部、在 `tf.data` 变换函数内、或调用 `disable_eager_execution()` 之后。

#### 4.2.4 代码实践

**实践目标**：通过单例对象确认「整个进程只有一个 Context」，并观察延迟初始化。

**操作步骤**（示例代码）：

```python
import tensorflow as tf
from tensorflow.python.eager import context

ctx1 = context.context()
ctx2 = context.context()
print("同一个对象吗？", ctx1 is ctx2)   # True —— 单例

# 初始化前后的设备状态
ctx1.ensure_initialized()
print(ctx1)                            # 打印 "Eager TensorFlow Context with N devices ..."
```

**需要观察的现象**：

- `ctx1 is ctx2` 为 `True`，证明单例。
- `print(ctx1)` 会列出已初始化的逻辑设备（CPU/GPU 等），对照 [context.py:1142-1150](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/eager/context.py#L1142-L1150) 的 `__str__`：未初始化时显示「Devices currently uninitialized」，初始化后显示设备数量与列表。

**预期结果**：单例确认通过；`__str__` 输出设备列表。

> 若无法本地运行，标注为「待本地验证」，但可结合 [context.py:1142-1150](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/eager/context.py#L1142-L1150) 的 `__str__` 源码理解。

#### 4.2.5 小练习与答案

**练习 1**：`Context.__init__` 被调用时，C++ 执行器（`TFE_NewContext`）被创建了吗？为什么这样设计？

**参考答案**：没有。`__init__` 只构造 Python 对象并设 `_context_handle = None`、`_initialized = False`（[context.py:575-579](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/eager/context.py#L575-L579)）；C++ 执行器要等到 `ensure_initialized()` 才创建。这样进程启动开销小，真正要算东西时才付出初始化成本（延迟初始化）。

**练习 2**：`context()` 和 `context_safe()` 有何区别？分别在什么场景用？

**参考答案**：`context()` 会在不存在时**触发创建**单例（[context.py:2538-2542](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/eager/context.py#L2538-L2542)）；`context_safe()` 只返回当前实例或 `None`，不创建（[context.py:2545-2547](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/eager/context.py#L2545-L2547)）。要执行 op 用前者；只想查询（如 `executing_eagerly` 的实现）用后者，避免无意中初始化整个运行时。

---

### 4.3 execute.py：op 的立即派发与跨语言桥

#### 4.3.1 概念说明

当 4.1 的分叉点判定「现在是 Eager」时，生成的 op 包装就会调用本文件的核心函数 `quick_execute`（对外通过别名 `execute` 暴露）。这个函数是 **Python 与 C++ Eager 内核之间的最后一座桥**：它把「op 名 + 输入张量 + 属性 + 目标设备」打包，交给 C++ 函数 `TFE_Py_Execute` 立即执行，再把返回的张量列表交回 Python。

可以把它理解成 Eager 模式的「单 op 版 `Session.run`」：`Session.run` 一次执行整张子图，而 `quick_execute` 一次只跑一个 op——这正是「立即」的含义。

#### 4.3.2 核心流程

`quick_execute` 的执行过程极简，只有三步：

```
quick_execute(op_name, num_outputs, inputs, attrs, ctx, name)
        │
        ▼
① 取当前设备 device_name = ctx.device_name
        │
        ▼
② ctx.ensure_initialized()   （确保 C++ 执行器已就绪，见 4.2）
        │
        ▼
③ pywrap_tfe.TFE_Py_Execute(ctx._handle, device_name, op_name,
                            inputs, attrs, num_outputs)
        │   （进入 C++：找到 op 对应 kernel、放置、执行、返回 EagerTensor 列表）
        ▼
返回 tensors（输出张量列表）
```

异常处理是它唯一「绕」的地方：C++ 侧用 `_NotOkStatusException` 表示错误，这里把它翻译成用户能看懂的 TF 异常（`core._status_to_exception`）；另外若发现输入里有 Keras 符号张量（说明用户误把图模式的符号张量喂进了 Eager 路径），则抛出专门的 `_SymbolicException` 提示。

#### 4.3.3 源码精读

**核心函数 `quick_execute`**

[execute.py:28-67](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/eager/execute.py#L28-L67) 是本文件最关键的代码：

```python
def quick_execute(op_name, num_outputs, inputs, attrs, ctx, name=None):
  device_name = ctx.device_name
  try:
    ctx.ensure_initialized()
    tensors = pywrap_tfe.TFE_Py_Execute(ctx._handle, device_name, op_name,
                                        inputs, attrs, num_outputs)
  except core._NotOkStatusException as e:
    if name is not None:
      e.message += " name: " + name
    raise core._status_to_exception(e) from None
  except TypeError as e:
    keras_symbolic_tensors = [x for x in inputs if _is_keras_symbolic_tensor(x)]
    if keras_symbolic_tensors:
      raise core._SymbolicException(...)
    raise e
  return tensors
```

读这段代码，能提炼出几个要点：

- `num_outputs` 被**显式传入**而非推断——注释说是「出于性能原因」（避免一次 op 调用还要查注册表确认输出个数）。
- 真正干活的就是 `pywrap_tfe.TFE_Py_Execute(...)` 这一行：它跨进 C++，由内核完成 kernel 查找、设备放置、执行并返回 `EagerTensor` 列表。前面 `ensure_initialized()` 保证 C++ 句柄 `ctx._handle` 已就绪。
- 错误转换：把 C++ 的 `_NotOkStatusException`（带状态码的低层异常）经 `_status_to_exception` 翻译成 `tf.errors.InvalidArgumentError` 之类用户熟悉的异常，并把 `name` 拼进消息方便定位。

**别名 `execute` 与「可取消」变体**

[execute.py:131](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/eager/execute.py#L131)：对外暴露的名字是 `execute`，默认就是 `quick_execute`。

```python
execute = quick_execute
```

[execute.py:70-119](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/eager/execute.py#L70-L119)：`execute_with_cancellation` 是支持中途取消的变体，调用的是 `TFE_Py_ExecuteCancelable` 并额外传入 `cancellation_manager`。`execute_with_callbacks`（[execute.py:122-128](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/eager/execute.py#L122-L128)）则在执行后依次回调注册的 `op_callbacks`（用于 Profiler 等工具）。

**「不记录梯度」的桩函数**

[execute.py:134-142](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/eager/execute.py#L134-L142)：有意思的是，`must_record_gradient` 在这里直接返回 `False`，`record_gradient` 是空函数——注释说「想记录梯度就 import backprop」。这是为避免在不需要梯度的常见场景下强依赖 `backprop` 模块而做的解耦；真正需要时（如 `GradientTape` 下）会被 backprop 模块替换。这承接 u5-l1（自动微分）。

**输入张量的类型规整**

[execute.py:222-277](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/eager/execute.py#L222-L277)：`args_to_matching_eager` 把一组输入统一转换成相同 dtype 的 eager 张量（如把 Python list/numpy 转成 `EagerTensor`）。这是 op 包装在调用 `execute` 之前做的预处理，确保传给 C++ 的 `inputs` 都是真正的 eager 张量。

#### 4.3.4 代码实践

**实践目标**：在 Eager 模式下执行一个加法，对照源码说明 op 是如何被**立即派发**到底层、而非进入图中的。

**操作步骤**（示例代码）：

```python
import tensorflow as tf

a = tf.constant([1.0, 2.0, 3.0])
b = tf.constant([10.0, 20.0, 30.0])
c = tf.add(a, b)        # 也可写成 a + b
print(c)                # 立即得到 tf.Tensor([11. 22. 33.], ...)
```

对照源码手动走一遍链路：

1. `tf.add` 是 `gen_math_ops.add`（构建期生成）。据 4.1.3 的生成器代码，它先 `_ctx = _context.context()`、读 `tld.is_eager`（为 `True`）。
2. 命中 Eager 快路径，调用 `_execute.execute("Add", 1, inputs=[a, b], attrs=..., ctx=_ctx)`。
3. 进入 [execute.py:28-67](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/eager/execute.py#L28-L67) 的 `quick_execute`：取 `device_name`、`ensure_initialized()`、调用 `TFE_Py_Execute`。
4. C++ 内核立即找到 `Add` 的 CPU kernel、执行，返回装着 `[11., 22., 33.]` 的 `EagerTensor`。

**需要观察的现象**：

- `c` 立即打印出数值，**没有任何 `Session`、没有任何建图过程**。
- 你可以在 `tf.add(a, b)` 这一行打断点 / 在其后加 `print`，行为完全像普通 Python。
- 对比：若用 `tf.compat.v1.graph_mode()` 包起来，`c` 就只会是个符号 Tensor，不会有数值（呼应 4.1.4）。

**预期结果**：`c` 为 `[11. 22. 33.]`，证明 `Add` 经 `execute` 立即执行而非进图。

> 若无法本地运行，标注为「待本地验证」；链路本身可完全通过阅读上面三段源码确认。

#### 4.3.5 小练习与答案

**练习 1**：`quick_execute` 为什么要显式接收 `num_outputs` 而不是自己推断？

**参考答案**：注释明确写「Explicitly provided instead of being inferred for performance reasons」（[execute.py:33-35](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/eager/execute.py#L33-L35)）。每次 op 调用都查 op 注册表来推断输出个数会带来可观开销，而代码生成器在生成 op 包装时已知输出数，直接传进来更省。

**练习 2**：如果用户把一个 Keras 符号张量（图模式的占位）误喂进 Eager 的 `tf.add`，会发生什么？

**参考答案**：`quick_execute` 的 `except TypeError` 分支会用 `_is_keras_symbolic_tensor` 检测输入（[execute.py:59-65](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/eager/execute.py#L59-L65)），命中则抛出 `core._SymbolicException`，提示「Inputs to eager execution function cannot be Keras symbolic tensors」。这能在错误发生点及时给出清晰诊断，而不是在 C++ 内部抛晦涩错误。

**练习 3**：`execute.py` 里的 `must_record_gradient()` 直接返回 `False`，那 `GradientTape` 是如何记录梯度的？

**参考答案**：本文件的 `must_record_gradient` / `record_gradient` 只是「不需要梯度」时的默认桩；当 `GradientTape` 激活时，`backprop` 模块会把这两个函数替换成真正会记录前向 op 的实现（解耦是为了避免常场景强依赖 backprop）。详见 u5-l1。

---

## 5. 综合实践

把本讲的三个模块串起来，做一个「Eager 执行链路追踪」小任务。

**任务**：写一段代码，分别展示「立即执行」与「符号建图」，并在两种模式下都用 `tf.executing_eagerly()` 标注当前状态。

**操作步骤**（示例代码）：

```python
import tensorflow as tf

def describe(tag):
    print(f"[{tag}] executing_eagerly = {tf.executing_eagerly()}")

# 场景 A：Eager，立即执行
describe("A-进入前")
a = tf.constant([1.0, 2.0])
b = tf.constant([3.0, 4.0])
print("A 中 a+b =", (a + b).numpy())   # 立即得到 numpy 数组

# 场景 B：用 tf.function 包裹（函数体在 tracing 时不立即执行）
@tf.function
def fn(x):
    print(">> tracing 时执行（建图），这段只在 trace 时打印一次")
    return x * 2 + 1

describe("B-调用 fn 前")                # True（调用方仍 Eager）
y = fn(tf.constant([10.0]))            # 内部 trace + 编译成图
print("B 结果 =", y.numpy())
```

**需要观察的现象与对应源码**：

1. 场景 A 里 `a + b` 直接拿到 numpy 值 → 对应 4.3 的 `quick_execute` 立即派发。
2. 场景 B 里函数体 `print` **只打印一次**（tracing 时），之后调用 `fn` 不再打印 → 对应函数体内部 `tf.executing_eagerly()` 为 `False`（对照 [context.py:2576-2627](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/eager/context.py#L2576-L2627) docstring 列举的「在 `tf.function` 内返回 False」）。
3. 但 `fn` 的调用方始终 Eager，`describe("B-调用 fn 前")` 打印 `True`——说明「是否 Eager」是**线程局部、随作用域变化**的状态，而非全局一刀切（对应 4.2 的 `is_eager` 线程局部性）。

**预期结果**：能清楚区分「op 调用即执行（Eager）」与「op 调用即建图（`tf.function` tracing）」两条路径，并能用本讲源码解释每一步。

> 若无法本地运行，标注为「待本地验证」；结论可通过阅读本讲引用的源码推断。

## 6. 本讲小结

- **Eager 与 Graph 的分叉点**在生成器产出代码的 `if tld.is_eager:`（[python_op_gen.cc:1721-1723](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/framework/python_op_gen.cc#L1721-L1723)）：Eager 走立即执行、Graph 走建图。
- **`Context` 是 Eager 的「常驻 Session」**：进程级单例（[context.py:2487](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/eager/context.py#L2487)、[context.py:2538-2542](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/eager/context.py#L2538-L2542)），持有 C++ 执行器句柄，延迟初始化（[context.py:698-749](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/eager/context.py#L698-L749)）。
- **「是否 Eager」是线程局性状态**：由 `_thread_local_data.is_eager`（[context.py:1170-1172](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/eager/context.py#L1170-L1172)）决定，`graph_mode()`/`eager_mode()` 用上下文管理器临时切换、用完恢复。
- **`execute.quick_execute` 是最后跨语言桥**：一行 `TFE_Py_Execute`（[execute.py:53-54](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/eager/execute.py#L53-L54)）把 op 立即派发到 C++ 内核并返回 `EagerTensor`。
- **延迟初始化 + 双重检查锁**（[context.py:698-704](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/eager/context.py#L698-L704)）兼顾了正确性与热路径性能。
- **错误处理把 C++ 状态异常翻译成用户友好的 TF 异常**，并对误喂 Keras 符号张量给出专门提示（[execute.py:55-65](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/python/eager/execute.py#L55-L65)）。

## 7. 下一步学习建议

- **u3-l4（`tf.function`、`ConcreteFunction` 与 `def_function`）**：Eager 与 Graph 之间的桥梁。本讲已经埋下伏笔——`tf.function` 在 tracing 时让 `executing_eagerly()` 返回 `False`，把 Python 函数编译成图。下一讲将展开它的 tracing 与缓存机制。
- **u5-l1（自动微分与 gradients）**：本讲提到 `execute.py` 的 `must_record_gradient`/`record_gradient` 是默认桩，真正记录梯度由 `backprop` 模块接管。学完 `tf.function` 后即可深入 `GradientTape` 是如何「录制」前向 op 的。
- **延伸阅读源码**：`tensorflow/python/eager/core.py`（异常体系）、`tensorflow/python/eager/executor.py`（异步执行）、`tensorflow/c/eager/c_api.cc`（`TFE_Py_Execute` 背后的 C++ 实现，能让你看到内核如何按 op 名查 kernel 并执行）。
