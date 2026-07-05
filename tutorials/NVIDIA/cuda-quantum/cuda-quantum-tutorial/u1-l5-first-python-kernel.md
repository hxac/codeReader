# 第一个 Python 量子内核

## 1. 本讲目标

本讲承接 [u1-l4 第一个 C++ 量子内核](u1-l4-first-cpp-kernel.md)。在那一讲里，你已经学会用 `__qpu__` 写一个 C++ 内核、用 `cudaq::qvector` 分配比特、用 `cudaq::sample` 采样。本讲换成 **Python 前端**，达到同样目的。

学完本讲，你应该能够：

1. 用 `@cudaq.kernel` 装饰器把一段普通 Python 函数变成 CUDA-Q 量子内核。
2. 在内核里用 `cudaq.qubit()` / `cudaq.qvector()` 分配比特，并调用 `h`、`x`、`mz` 等门函数。
3. 用 `cudaq.sample(...)` 在 Python 端执行内核并读取采样分布，理解它与 C++ 端 `cudaq::sample` 的对应关系。

一个贯穿全讲的关键认知是：**Python 内核的函数体从来不真正被 Python 解释器执行**——它只是被解析成 AST，再翻译成 Quake MLIR，最终交给与 C++ 前端**同一套**运行时去跑。理解这一点，你就理解了 CUDA-Q「C++/Python 双前端共享运行时」的核心设计。

## 2. 前置知识

- **装饰器（decorator）**：Python 里 `@something` 写在 `def` 之上，等价于 `func = something(func)`。`@cudaq.kernel` 就是一个接收函数、返回「内核对象」的装饰器。
- **AST（抽象语法树）**：Python 解释器把源码先解析成一棵树状结构再执行。CUDA-Q 拦截这棵树，把它翻译成量子中间表示，而不是去执行它。
- **采样（sampling）与 shots**：量子计算的本质是概率性的。一次「采样」=把线路在模拟器（或真实 QPU）上跑一次并读出测量结果；`shots_count` 是重复跑的次数，用频率去估计概率分布。
- **C++ 内核基础**：本讲处处与 [u1-l4](u1-l4-first-cpp-kernel.md) 对照，请确保你已经读过那一讲。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
| --- | --- |
| [docs/sphinx/examples/python/intro.py](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/docs/sphinx/examples/python/intro.py) | 官方「第一个内核」示例，本讲的主线，覆盖装饰器、门、测量、采样全闭环。 |
| [docs/sphinx/examples/python/building_kernels.py](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/docs/sphinx/examples/python/building_kernels.py) | 内核构造进阶示例：参数化、受控门、自定义门、内核嵌套，本讲摘取其中片段对照。 |
| [python/cudaq/__init__.py](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/python/cudaq/__init__.py) | Python 前端的入口，决定了 `cudaq.kernel`、`cudaq.qubit`、`cudaq.sample` 这些名字从哪里来。 |
| [python/cudaq/kernel/kernel_decorator.py](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/python/cudaq/kernel/kernel_decorator.py) | `@cudaq.kernel` 装饰器与 `PyKernelDecorator` 的实现，揭示「函数 → 内核」的机制。 |
| [python/cudaq/kernel_types.py](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/python/cudaq/kernel_types.py) | `qubit` / `qvector` 等量子类型的「桩（stub）」，解释为什么这些类型只能在内核里用。 |
| [python/cudaq/runtime/sample.py](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/python/cudaq/runtime/sample.py) | Python 端 `cudaq.sample` 的实现，揭示采样循环如何调用内核。 |

## 4. 核心概念与源码讲解

### 4.1 `@cudaq.kernel` 装饰器：从 Python 函数到量子内核

#### 4.1.1 概念说明

在 C++ 前端，标识一个量子内核靠的是 `__qpu__` 宏（本质是 `__attribute__((annotate("quantum")))`，见 u1-l4）。在 Python 前端，等价的开关是 **`@cudaq.kernel` 装饰器**：贴在一个普通 `def` 之上，编译器就知道「这是一个量子内核，需要翻译成 Quake MLIR」。

关键区别在于执行模型：

- **C++ 内核**：`x(q)`、`h(q)` 是真实的 C++ 函数/宏，运行时会真的被调用，把「门名+比特 id」记录到 `ExecutionManager`（见 u1-l4）。
- **Python 内核**：`h(qubit)` 这些名字**在 Python 侧根本不是可调用的函数**。装饰器拿到的是函数的**源码与 AST**，函数体从未被 Python 解释器执行；AST 被 `ast_bridge.py` 翻译成 Quake MLIR，再交给与 C++ 共享的运行时。

这意味着：内核里能写什么、不能写什么，由「AST 桥支持哪些 Python 语法」决定，而不是由 Python 解释器决定。

#### 4.1.2 核心流程

装饰器从「函数」变成「可执行内核」分三步：

```text
@cudaq.kernel            ① 装饰器捕获函数对象，读取其源码
def kernel(): ...
                         ② 解析源码 → Python AST；提取签名(参数/返回类型)
kernel(...) 被调用时 →   ③ 首次调用触发 compile(): AST → compile_to_mlir() → Quake MLIR
                         ④ marshal_and_launch_module() 把 MLIR 交给运行时执行
```

注意第 ③ 步是**惰性的**：默认在第一次调用内核时才编译，编译结果会被缓存（`_cached_qkeModule`）。这是 `PyKernelDecorator` 类文档里明确说的：「By default, MLIR compilation is deferred until the first call to the kernel」。

#### 4.1.3 源码精读

先看主线示例 `intro.py` 的全貌，它就是一个最小的「装饰器 → 分配比特 → 加门 → 测量 → 采样」闭环：

[intro.py:6-26](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/docs/sphinx/examples/python/intro.py#L6-L26) —— 用 `@cudaq.kernel` 定义内核，分配 1 个比特，依次施加 `h/x/y/z/t/s`，最后 `mz` 测量。

```python
@cudaq.kernel
def kernel():
    qubit = cudaq.qubit()
    h(qubit); x(qubit); y(qubit); z(qubit); t(qubit); s(qubit)
    mz(qubit)
```

`@cudaq.kernel` 这个名字从哪来？看 Python 包入口的导入语句：

[__init__.py:175](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/python/cudaq/__init__.py#L175) —— `from .kernel.kernel_decorator import kernel, PyKernelDecorator`，把 `kernel` 函数挂到 `cudaq` 命名空间，于是 `@cudaq.kernel` 可用。

装饰器本体非常薄，它只是决定「带参数 / 不带参数」两种用法，真正干活的是 `PyKernelDecorator`：

[kernel_decorator.py:765-780](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/python/cudaq/kernel/kernel_decorator.py#L765-L780) —— `kernel()` 函数：无参数时直接 `return PyKernelDecorator(function)`；带参数（如 `@cudaq.kernel(verbose=True)`）时返回一个 `wrapper`。

```python
def kernel(function=None, **kwargs):
    if function:
        return PyKernelDecorator(function)
    else:
        def wrapper(function):
            return PyKernelDecorator(function, **kwargs)
        return wrapper
```

「首次调用才编译」的核心在 `compile()` 方法里——它调用 `compile_to_mlir(...)` 把 AST 翻成 Quake：

[kernel_decorator.py:266-289](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/python/cudaq/kernel/kernel_decorator.py#L266-L289) —— `compile()`：把 `self.astModule` 交给 `compile_to_mlir`，产物缓存到 `self._cached_qkeModule`。

而「调用内核」就是 `__call__`，它最终走向 `cudaq_runtime.marshal_and_launch_module`：

[kernel_decorator.py:660-680](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/python/cudaq/kernel/kernel_decorator.py#L660-L680) —— `__call__`：先 `prepare_call` 处理参数，再 `marshal_and_launch_module` 把 MLIR 模块交给运行时。

> 对比 u1-l4：C++ 侧的门调用真的会执行 `ExecutionManager`；Python 侧的门调用在 `__call__` 里根本不存在——它们早在 AST 翻译阶段就被替换成了 Quake 操作。两条路最终都汇入**同一个** C++ 运行时。

#### 4.1.4 代码实践

**实践目标**：亲眼确认「内核函数体不被 Python 执行」。

1. 新建 `probe.py`，写一个故意会抛异常的内核：

   ```python
   import cudaq

   @cudaq.kernel
   def kernel():
       q = cudaq.qubit()
       h(q)
       mz(q)
       raise RuntimeError("如果这行被打印，说明函数体被执行了")

   print(cudaq.sample(kernel))
   ```

2. 运行：`python probe.py`（需要已安装 cuda-quantum，见 [u1-l3](u1-l3-build-and-run.md)）。
3. **观察现象**：程序正常输出采样结果，**不会**抛出 `RuntimeError`。
4. **预期结果**：因为 `raise` 那行只是 AST 的一个节点，在翻译成 Quake 时会被忽略/无法映射，而 Python 解释器从不执行函数体，所以异常不会触发。（若 AST 桥对 `raise` 报编译错误，那也属于「翻译期」而非「Python 执行期」错误，正好印证同一结论。）
5. 若本地未安装 cuda-quantum，则标注「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：`@cudaq.kernel` 和 `@cudaq.kernel(verbose=True)` 在源码层面走的是同一条分支吗？

> **答案**：不同。前者 `function` 非空，直接返回 `PyKernelDecorator(function)`；后者 `function` 为 `None`，返回一个 `wrapper`，等真正装饰函数时再构造 `PyKernelDecorator`，并把 `verbose=True` 等关键字透传进去。

**练习 2**：为什么把一个普通 Python 函数装饰成内核后，`print(type(kernel))` 看到的不再是 `function`？

> **答案**：装饰器返回的是 `PyKernelDecorator` 实例而非原函数，所以 `kernel` 变成了一个内核对象，调用它走的是 `PyKernelDecorator.__call__`。

### 4.2 `qubit`/`qvector` 与门函数

#### 4.2.1 概念说明

内核里要分配量子比特、要对它们施加门。Python 端的 API 与 C++ 端高度对称，只是命名风格从 C++ 的 `cudaq::qvector` 变成 Python 的 `cudaq.qvector()`：

| 概念 | C++ 端（u1-l4） | Python 端（本讲） |
| --- | --- | --- |
| 单比特 | `cudaq::qubit q;` | `q = cudaq.qubit()` |
| 比特数组 | `cudaq::qvector qs(n);` | `qs = cudaq.qvector(n)` |
| 基本门 | `h(q); x(q);` | `h(q); x(q);` |
| 受控门 | `cudaq::ctrl(x, c, t)` 或 `x<cudaq::ctrl>(c,t)` | `x.ctrl(c, t)` |
| 测量 | `mz(q)` | `mz(q)` |

一个容易踩坑的点：`cudaq.qubit()`、`cudaq.qvector()` 在 Python 侧是**类型桩（stub）**，直接在内核外实例化会抛 `KernelTypeError`。它们存在的意义只是给 IDE 提示类型、给 AST 桥一个识别目标——真正的比特分配发生在 Quake IR 里。

#### 4.2.2 核心流程

```text
内核源码:  q = cudaq.qvector(3)        ┐
           h(q[0]); x.ctrl(q[0], q[1])  │  AST 桥识别这些名字
           mz(q)                        ┘
                        ↓ compile_to_mlir
Quake MLIR: quake.alloca ... quake.H ... quake.X (controlled) ... quake.Mz ...
                        ↓ 运行时
模拟器(qpp) 解释执行，产生测量结果
```

门函数（`h`、`x`、`mz` 等）不是从 `cudaq` 模块 `import` 来的普通函数。它们是 AST 桥在遍历内核 AST 时**按名字识别**的「内建门」。这也是为什么 `intro.py` 只 `import cudaq`，却能直接调用裸名 `h(qubit)` 而不用写 `cudaq.h`。

#### 4.2.3 源码精读

`building_kernels.py` 给出了几种典型分配方式：

[building_kernels.py:5-9](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/docs/sphinx/examples/python/building_kernels.py#L5-L9) —— 混合分配单比特 `A = cudaq.qubit()` 和多比特寄存器 `B = cudaq.qvector(3)`。

对寄存器既可以「整体施加门」，也可以「按下标取出单个比特」：

[building_kernels.py:107-115](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/docs/sphinx/examples/python/building_kernels.py#L107-L115) —— `h(register[0])` 操作第一个比特，`h(register[-1])` 操作最后一个比特。

受控门用 `.ctrl` 修饰符，这是 Python 端对应 C++ `cudaq::ctrl` 的写法：

[building_kernels.py:118-126](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/docs/sphinx/examples/python/building_kernels.py#L118-L126) —— `x.ctrl(register[0], register[1])` 就是一个 CNOT（qubit 0 控制、qubit 1 目标）。多控制比特则把控制比特放进列表，如 `x.ctrl([register[0], register[1]], register[2])`（见 [building_kernels.py:129-137](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/docs/sphinx/examples/python/building_kernels.py#L129-L137)）。

现在解释「为什么这些类型只能在内核里用」。看桩类型的定义：

[kernel_types.py:29-35](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/python/cudaq/kernel_types.py#L29-L35) —— `KernelType.__new__` 直接 `raise KernelTypeError(cls)`，所以 `cudaq.qubit()` 在主机域实例化必然报错。

```python
class KernelType:
    def __new__(cls, *args, **kwargs):
        raise KernelTypeError(cls)  # RuntimeError 子类
```

这些桩通过包入口导出，AST 桥识别到 `cudaq.qvector(...)` 调用时，并不真的执行它，而是生成 `quake.alloca`：

[__init__.py:208-209](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/python/cudaq/__init__.py#L208-L209) —— 从 `kernel_types` 导入 `qubit`、`qvector`、`qview` 等桩，注释里写得很清楚：「stubs; used only in kernels, parsed to MLIR」。

> 对比 u1-l4：C++ 侧比特的「不可拷贝」由类型系统 `delete` 拷贝构造强制；Python 侧比特的「只能在内核用」由桩的 `__new__` 抛错 + AST 桥只在内核上下文识别这两道关卡共同保证。两侧约束位置不同，意图一致。

#### 4.2.4 代码实践

**实践目标**：验证 `cudaq.qubit()` 在内核外会报错。

1. 启动 `python`，执行：

   ```python
   import cudaq
   q = cudaq.qubit()   # 预期抛 KernelTypeError(RuntimeError 子类)
   ```

2. **观察现象**：抛出异常，消息形如 `'qubit' can be used only in CUDA-Q kernels`。
3. **预期结果**：再次印证量子类型不是 Python 运行时对象，而是 MLIR 占位符。
4. 接着写一个合法内核验证下标取比特：

   ```python
   @cudaq.kernel
   def k():
       qs = cudaq.qvector(3)
       h(qs[0])
       mz(qs)
   print(cudaq.sample(k))
   ```

5. 预期 `qs[0]` 上看到约 50/50 的 0/1 分布。若本地未装包则标注「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：写一个三比特内核，要求「整体对寄存器施加 `h`」与「逐个对每个比特施加 `h`」效果相同，用采样验证。

> **答案**：`h(register)` 会对 `qvector` 里所有比特施加 H，等价于 `for i in range(register.size()): h(register[i])`。两者采样分布一致（每个比特均接近 50/50）。

**练习 2**：`x.ctrl(c, t)` 里 `c`、`t` 顺序是什么？怎样实现一个 Toffoli（双控非）？

> **答案**：第一个参数是控制比特，第二个是目标比特。Toffoli 用多控制形式：`x.ctrl([c0, c1], target)`。

### 4.3 `cudaq.sample` 调用与结果读取

#### 4.3.1 概念说明

写完内核后要用 `cudaq.sample(kernel, ...)` 执行它。它对应 C++ 端的 `cudaq::sample`（见 u1-l4）：把内核重复执行 `shots_count` 次（默认 1000），返回一个 `cudaq.SampleResult`——本质是一张 `{比特串 → 出现次数}` 的字典。

Python 端 `sample` 与 C++ 端的关键一致性：

- 默认 `shots_count=1000`，与 C++ 端的 `DEFAULT_NUM_SHOTS` 概念一致。
- 内核必须含测量（`mz/mx/my`），否则采样循环拿不到结果。
- 结果对象的行为一致：可 `print`、可按比特串迭代。

#### 4.3.2 核心流程

`sample` 在 Python 侧是一个薄封装，真正循环执行内核的逻辑很清晰：

```text
sample(kernel, *args, shots_count=1000)
   ├── 创建 ExecutionContext("sample", shots_count)
   ├── counts = SampleResult()             # 累加器
   ├── while counts.total < shots_count:
   │       result = launch_sample(... lambda: kernel(*args))  # 跑内核
   │       counts += result
   └── return counts                        # {比特串: 次数}
```

每次 `lambda: kernel(*args)` 会触发 `PyKernelDecorator.__call__`（4.1 节），把内核交给运行时跑一次。多次累加得到最终分布。

#### 4.3.3 源码精读

主线示例的收尾两行就是最典型的用法：

[intro.py:33-36](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/docs/sphinx/examples/python/intro.py#L33-L36) —— `result = cudaq.sample(kernel)` 执行内核，`print(result)` 打印采样分布。

`cudaq.sample` 这个名字同样来自包入口：

[__init__.py:178](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/python/cudaq/__init__.py#L178) —— `from .runtime.sample import sample`。

返回类型 `SampleResult` 也是在入口处暴露的：

[__init__.py:276](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/python/cudaq/__init__.py#L276) —— `SampleResult = cudaq_runtime.SampleResult`，即来自底层 C++ 运行时。

`samples` 函数的签名与采样循环（核心执行逻辑）：

[sample.py:111-115](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/python/cudaq/runtime/sample.py#L111-L115) —— 函数签名：`sample(kernel, *args, shots_count=1000, noise_model=None, explicit_measurements=False)`。

```python
def sample(kernel, *args, shots_count=1000,
           noise_model=None, explicit_measurements=False):
```

[sample.py:163-194](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/python/cudaq/runtime/sample.py#L163-L194) —— 创建 `ExecutionContext`，用 `while counts.get_total_shots() < shots_count` 循环调用 `launch_sample`，把每次结果累加进 `counts`，最后返回。注意其中有一处保护：若某次执行产生 0 个 shot（典型原因就是内核没有测量），会打印警告并跳出循环，避免死循环——这与 u1-l4 里 C++ 端「内核必须含测量」的约束呼应。

> 对比 u1-l4：C++ 端 `cudaq::sample` 把内核重复跑 `DEFAULT_NUM_SHOTS=1000` 次；Python 端 `cudaq.sample` 默认 `shots_count=1000`，逻辑几乎一一对应，两侧的 `SampleResult`/`sample_result` 也是同一份底层数据结构。

#### 4.3.4 代码实践

**实践目标**：用 `shots_count` 观察采样收敛。

1. 写一个内核并对它用不同 `shots_count` 采样：

   ```python
   import cudaq

   @cudaq.kernel
   def k():
       q = cudaq.qubit()
       h(q)
       mz(q)

   for shots in [10, 100, 1000, 10000]:
       print(shots, cudaq.sample(k, shots_count=shots))
   ```

2. **操作步骤**：依次跑 10/100/1000/10000 次，打印分布。
3. **观察现象**：shots 越大，0 与 1 的计数越接近 50/50；shots=10 时偏差明显。
4. **预期结果**：这是「用频率估计概率」的统计现象，标准差大致按 \( 1/\sqrt{N} \) 下降，即

   \[ \sigma \approx \sqrt{\frac{p(1-p)}{N}} \]

   其中 \( N \) 是 `shots_count`，\( p \) 是真实概率（此处 \( p=0.5 \)）。

5. 若本地未装包，标注「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：如果内核里没有任何 `mz/mx/my`，`cudaq.sample` 会怎样？

> **答案**：每次执行产生 0 个 shot，`counts.get_total_shots()` 一直为 0，触发 `sample.py` 里的「WARNING: ... produced 0 shots ... Exiting shot loop」并跳出，返回空结果。所以内核必须含测量。

**练习 2**：`cudaq.sample(kernel)` 返回的对象，除了 `print` 还能怎么读取？

> **答案**：它是一个类字典的 `SampleResult`，可以像字典那样遍历比特串与计数（`for bitstring, count in result.items():`），也支持取总 shots 数等接口。最直观的方式仍是 `print(result)`。

## 5. 综合实践

把本讲三个模块（装饰器、比特与门、采样）串起来，做一个 **Bell 态内核**，并与 [u1-l4](u1-l4-first-cpp-kernel.md) 的 C++ 版本对照。

**任务**：用 Python 实现两比特的 Bell 态（最大纠缠态），采样验证它只产生 `00` 和 `11` 两种结果且各占约 50%。

```python
import cudaq

@cudaq.kernel
def bell_state():
    # 分配 2 个比特
    q = cudaq.qvector(2)
    # 制备纠缠：H 门 + CNOT
    h(q[0])
    x.ctrl(q[0], q[1])   # CNOT: q[0] 控制, q[1] 目标
    # 测量全部比特
    mz(q)

# 采样
result = cudaq.sample(bell_state, shots_count=1000)
print(result)
```

**操作步骤**：

1. 把上面代码存为 `bell.py`，确保已按 [u1-l3](u1-l3-build-and-run.md) 安装/构建好 cuda-quantum。
2. 运行 `python bell.py`。
3. 对照 C++ 版本（来自 u1-l4 思路）：

   ```cpp
   // C++ 内核（示意，依赖 __qpu__ 与 cudaq:: 标头）
   __qpu__ void bell_state() {
       cudaq::qvector q(2);
       h(q[0]);
       x<cudaq::ctrl>(q[0], q[1]);
       mz(q);
   }
   // cudaq::sample(bell_state);
   ```

**预期结果**：

- Python 与 C++ 两版的采样分布应当**几乎一致**：约 50% `00`、约 50% `11`，几乎不出现 `01`/`10`。
- 这印证了本讲的核心结论：**两个前端共享同一套运行时**，所以同一逻辑的输出在统计上不可区分。
- 若想看到精确的纠缠效果，可加 `shots_count=10000` 让噪声更小。

**进一步思考**（选做）：把 `x.ctrl(q[0], q[1])` 换成多控制写法 `x.ctrl([q[0]], q[1])`，验证结果不变；再尝试 `cudaq.draw(bell_state)`（如果环境支持）打印线路图。

## 6. 本讲小结

- `@cudaq.kernel` 装饰器把普通 Python 函数变成量子内核，其本质是 `PyKernelDecorator` 捕获函数源码、解析 AST、首次调用时惰性编译成 Quake MLIR。
- Python 内核的函数体**从不被 Python 解释器执行**；`h`、`x`、`mz` 等是 AST 桥按名字识别的内建门，而非普通 Python 函数。
- `cudaq.qubit()` / `cudaq.qvector()` 是类型桩，在主机域实例化会抛 `KernelTypeError`；它们只在内核 AST 中有意义，被翻译成 `quake.alloca`。
- 受控门用 `.ctrl` 修饰符（如 `x.ctrl(control, target)`），对应 C++ 端的 `cudaq::ctrl` / `x<cudaq::ctrl>`。
- `cudaq.sample(kernel, shots_count=1000)` 默认采样 1000 次，返回 `SampleResult`；内核必须含测量，否则触发 0-shot 警告。
- Python 前端与 C++ 前端**共享同一套 C++ 运行时**，所以等价内核的采样结果在统计上一致——这是 CUDA-Q 双前端设计的核心收益。

## 7. 下一步学习建议

- 想深入了解「AST → Quake」是怎么逐节点翻译的，请阅读 [u5-l2 Python AST Bridge：从 Python 到 Quake](u5-l2-python-ast-bridge.md)。
- 想系统学习量子类型系统（`qubit`/`qvector`/`qview` 的关系与不可拷贝约束），请阅读 [u2-l1 量子类型系统](u2-l1-quantum-types.md)。
- 想了解执行模型（内核调用如何被分发到具体后端），请阅读 [u3-l1 执行模型：quantum_platform 与 QPU](u3-l1-execution-model.md)。
- 想看更多内核构造技巧（参数化、自定义门、内核嵌套），可继续精读 [building_kernels.py](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/docs/sphinx/examples/python/building_kernels.py)。
