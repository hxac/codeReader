# Script 类语义：__init__、__call__ 与实例化

## 1. 本讲目标

在 [u1-l5](u1-l5-naive-matmul-tilus-script.md) 里，我们已经会写一个能跑的 naive matmul，但一直把 `MatmulV0()` 和 `matmul(...)` 这两次「调用」当成黑箱。本讲的目标是把这两层调用的内部机制彻底讲清楚：

1. 理解 **`Script(...)` 为什么不是返回一个 `Script` 对象，而是返回一个 `InstantiatedScript`**——即 `Script.__new__` 的拦截机制与 `InstantiatedScriptCache` 的缓存。
2. 区分 **编译期超参**（写在 `__init__` 里的 `self.block_m` 等）与 **运行时参数**（`__call__` 的标注参数），并理解 `CallParameters` 如何把 `__call__` 参数进一步细分为常量参数、内核参数、调优参数三类，以及它们如何组成 **JIT key** 与 **tuning key**。
3. 学会用 **`debug_schedule`** 把整个 autotune 搜索空间压缩成单个调度，只编译一份内核，便于调试与排查缓存。

学完后，你应能回答：「我写了一个 Script，到底什么时候触发 JIT 重编译？什么时候只是查 dispatch 表？」并能用 `debug_schedule` 精准固定一份调度去检查缓存目录。

## 2. 前置知识

本讲承接 [u1-l5](u1-l5-naive-matmul-tilus-script.md)，假定你已经掌握：

- **Script 骨架**：一个内核即继承 `tilus.Script` 的类，`__init__` 设超参、`__call__` 写算子逻辑（见 [u1-l3](u1-l3-first-kernel-vector-add.md)）。
- **指针类型 `~dtype` 与 `int`/`int32` 的区分**：`int` 是编译期常量、换值即重编译；`int32` 是运行时标量（见 [u1-l4](u1-l4-datatypes-and-pointer-types.md)）。
- **缓存目录**：编译产物按内容哈希落在 `cache_dir` 下（见 [u1-l2](u1-l2-install-run-package-layout.md)）。

本讲还会用到两个 Python 背景：

- **`__new__` 与 `__init__` 的分工**：`__new__(cls, ...)` 负责创建并 **返回** 一个对象，`__init__(self, ...)` 负责初始化这个对象。Tilus 正是利用「`__new__` 可以返回任意对象」这一特性，让 `Script(...)` 直接返回一个编译产物。
- **`inspect.signature`**：Python 标准库，用来读取函数的形参列表（名字、默认值、类型标注）。Tilus 用它来分析 `__init__` 与 `__call__` 的签名。

> 提示：如果你对 `@autotune` 装饰器本身还陌生，可以先跳到本讲的 4.3 节看一个最小例子，再回头读 4.1。`@autotune` 的完整讲解在 [u2-l4](u2-l4-autotune-and-schedule-space.md)。

## 3. 本讲源码地图

本讲聚焦于「用户写出的 Script 类 → 一个可调用对象」这条链路，涉及的关键文件如下：

| 文件 | 作用 |
| --- | --- |
| [python/tilus/lang/script.py](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/script.py) | 定义 `Script` 基类、`Attributes`、`@autotune` 装饰器。核心是 `Script.__new__` 拦截构造。 |
| [python/tilus/lang/instantiated_script.py](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/instantiated_script.py) | 定义 `InstantiatedScript`（可调用门面）、`CallParameters`（参数分类）、`JitInstance`（一次 JIT 编译的载体）、`InstantiatedScriptCache`（实例缓存）。本讲最重的文件。 |
| [python/tilus/lang/constructs/state.py](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/constructs/state.py) | 定义 `tilus.Class`——与 `Script` 平级的「可复用构造」，帮你理解 `Script` 在类型体系中的位置。 |
| [python/tilus/lang/instructions/base.py](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/instructions/base.py) | `builder_context` / `InstructionGroup`，说明 `self.*` 指令为何只在 `__call__` 转译期间生效。 |
| examples/matmul/matmul_v0.py / examples/attention/flash_attention_v1.py | 真实示例：前者是本讲的对照样本，后者展示 `@autotune` + `debug_schedule` 的标准用法。 |

## 4. 核心概念与源码讲解

### 4.1 Script.__new__ 与 InstantiatedScript

#### 4.1.1 概念说明

在普通 Python 里，`MyClass()` 会先调用 `MyClass.__new__` 创建实例、再调用 `MyClass.__init__` 初始化它，最终返回那个实例。Tilus 做了一件反直觉但很关键的事：

> **`Script(...)` 不会返回一个 `Script` 对象，而是返回一个 `InstantiatedScript`。**

为什么这么做？因为 `Script` 子类只是「内核的描述」，真正能被调用、能编译、能 launch 的是另一类对象——`InstantiatedScript`。把「描述」和「产物」用同一个语法糖衔接起来，用户写 `matmul = MatmulV0()` 时拿到的就已经是一个可以 `matmul(m, n, k, a, b, c)` 的可调用对象，体验上和普通函数一样自然。

这里有个微妙之处：既然 `__new__` 返回的不是 `cls` 的实例，那么 `__init__` **永远不会被 Python 正常地调用**。你写在子类里的 `__init__`（比如设 `self.block_m = 64`）其实是在 **转译阶段** 由 Tilus 自己显式调用的，详见 4.1.3。

为了不让同一个 `(Script子类, 构造参数)` 反复重建 `InstantiatedScript`，Tilus 还加了一层进程内的 `InstantiatedScriptCache` 做记忆化。

#### 4.1.2 核心流程

把「写一个 Script 到跑起来」拆成 **两层调用**，整体流程如下：

```
用户代码:  matmul = MatmulV0()              # 第 1 层：实例化
           │
           ▼  Script.__new__(cls, ...) 拦截
           InstantiatedScriptCache.get(cls, args, kwargs)
           │  （按 (cls, args, kwargs) 记忆化）
           ▼  命中则直接返回，否则 new 一个
           InstantiatedScript(cls, args, kwargs)
             · 读取 _autotune_space（@autotune 收集的搜索空间）
             · generate_schedules(...) → 一组 schedule（每个是一份 __init__ 参数）
             · CallParameters(cls) → 分析 __call__ 参数分类
             · jit_instances / dispatch_table 初始为空
           │
           ▼  返回 InstantiatedScript
用户代码:  matmul(m, n, k, a, b, c)         # 第 2 层：调用
           │
           ▼  InstantiatedScript.__call__
             · extract_keys(args) → (jit_key, tuning_key)
             · 查 dispatch_table；未命中则
                 JitInstance(...) → 转译 + 构建 + benchmark → 选最优
             · 取 launch func，只传 kernel_params，发起 CUDA launch
```

要点：

- 第 1 层（实例化）做的是 **静态分析**：解析搜索空间、生成候选 schedule、分析 `__call__` 参数。**此时并不编译**，只是准备好「怎么编译」的元信息。
- 第 2 层（调用）才会真正触发 **JIT 编译**（如果没缓存），且按 `args` 动态决定编译哪一份。
- `InstantiatedScriptCache` 保证 `MatmulV0()` 写多次也只构造一次 `InstantiatedScript`。

#### 4.1.3 源码精读

**① `Script.__new__` 拦截构造，委托给缓存**

`Script` 把构造直接转交给 `InstantiatedScriptCache.get`，并声明返回类型是 `InstantiatedScript`：

[python/tilus/lang/script.py:50-59](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/script.py#L50-L59) —— `__new__` 不创建 `Script` 实例，而是从缓存取一个 `InstantiatedScript` 返回。

```python
def __new__(cls, *args, **kwargs) -> InstantiatedScript:
    from tilus.lang.instantiated_script import InstantiatedScriptCache
    instantiated_script: InstantiatedScript = InstantiatedScriptCache.get(
        script_cls=cls, script_args=args, script_kwargs=kwargs,
    )
    return instantiated_script
```

注意这里 `from ... import` 写在函数体内，是为了打破 `script.py` 与 `instantiated_script.py` 之间的循环导入（`instantiated_script.py` 反向导入了 `Script`）。

正因为 `__new__` 返回的不是 `cls` 实例，Python 的 `__init__` 协议认为「无需初始化」，于是你写的 `MatmulV0.__init__` 在第 1 层 **不会** 被调用。它真正被调用的地方在 4.1.3 ③。

**② `InstantiatedScriptCache`：进程内记忆化**

[python/tilus/lang/instantiated_script.py:907-947](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/instantiated_script.py#L907-L947) —— 用一个类级字典 `cache` 按「归一化后的 key」缓存 `InstantiatedScript`。

```python
class InstantiatedScriptCache:
    cache: dict[Any, InstantiatedScript] = {}

    @classmethod
    def get(cls, script_cls, script_args, script_kwargs):
        key = cls._normalize_key((script_cls, script_args, script_kwargs))
        if key not in cls.cache:
            instantiated_script = InstantiatedScript(script_cls, script_args, script_kwargs)
            cls.cache[key] = instantiated_script
        return cls.cache[key]
```

`_normalize_key` 把可能不可哈希的 `args/kwargs`（如 dict、list）递归转成可哈希形式（list→tuple、dict→排序后的 tuple）。这保证了 `MatmulV0()` 写多少次，都只构造一次 `InstantiatedScript`。注意这层缓存 **只在进程内**，跨进程不共享；跨进程/跨次运行靠的是磁盘上的 dispatch 表（见 4.2.3 与综合实践）。

**③ `InstantiatedScript.__init__`：静态分析，不编译**

[python/tilus/lang/instantiated_script.py:807-822](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/instantiated_script.py#L807-L822) —— 构造时只收集元信息，`jit_instances` / `dispatch_table` 都是空的。

```python
class InstantiatedScript:
    def __init__(self, script_cls, script_args, script_kwargs):
        self.script_cls = script_cls
        self.script_name = to_snake_case(script_cls.__name__)
        self.space = getattr(script_cls, "_autotune_space", {})   # @autotune 收集的空间
        self.build_options = BuildOptions.create(debug_block=script_cls.debug_block)
        self.schedules = generate_schedules(self.space, script_cls, script_args, script_kwargs)
        self.params = CallParameters(script_cls)                  # 分析 __call__ 参数
        ...
        self.jit_instances: dict[JitKey, JitInstance] = {}        # 初始为空：还没编译
        self.dispatch_table: dict[...] = {}
```

这里能看到三层结构如何衔接：

- `self.space` 来自类属性 `_autotune_space`，它由 `@autotune` 装饰器逐层写入（见 4.3.3）。
- `self.schedules` 是把搜索空间展开后得到的一组 `__init__` 参数字典（见 4.3.3 的 `generate_schedules`）。
- `self.params` 是 `CallParameters` 对 `__call__` 签名的分析结果（见 4.2）。

`__init__` 里 **没有任何编译动作**——真正的转译/构建发生在第 2 层调用、`JitInstance` 被创建时。

**④ `InstantiatedScript.__call__`：第 2 层调用与 JIT 触发**

[python/tilus/lang/instantiated_script.py:824-858](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/instantiated_script.py#L824-L858) —— 这才是「调用内核」的入口。

```python
def __call__(self, *args, **kwargs):
    ...
    keys = extract_keys(args, self.const_params, self.tuning_params)   # (jit_key, tuning_key)
    compiled_func = self.dispatch_table.get(keys, None)
    if compiled_func is None:                          # 慢路径
        jit_key, tuning_key = keys
        jit_instance = self.jit_instances.get(jit_key, None)
        if jit_instance is None:
            jit_instance = JitInstance(self.script_cls, self.params, self.build_options,
                                       self.schedules, jit_key)
            self.jit_instances[jit_key] = jit_instance
        compiled_program = jit_instance._pick_best_program(args)   # 转译+构建+benchmark
        compiled_func = compiled_program.get_launch_func()
        self.dispatch_table[(jit_key, tuning_key)] = compiled_func
    kernel_args = (args[i] for i in self.kernel_params)            # 只传 kernel 参数
    return compiled_func(*kernel_args)
```

关键判断：「命中 `dispatch_table`」就走快路径直接 launch；未命中才创建 `JitInstance` 去编译。**是否触发 JIT 取决于 `(jit_key, tuning_key)` 是否在表里**——这正是 4.2 要讲清的主题。

**⑤ 你的 `__init__` 在哪里被调用？**

由于第 1 层 `__new__` 没走 `__init__`，Tilus 在 **转译每一份 schedule 时** 显式调用它。`JitInstance._instantiate_schedule` 用 `object.__new__` 绕过被改写的 `Script.__new__`，再手动调用 `__init__`：

[python/tilus/lang/instantiated_script.py:443-448](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/instantiated_script.py#L443-L448) —— 用 `object.__new__` 创建真实 `Script` 对象，再用当前 schedule 调用 `__init__`。

```python
# we have redefined the __new__ for Script, thus we need to use object.__new__ ...
script_obj = object.__new__(script_cls)
script__init__ = getattr(script_cls, "__init__")
script__init__(script_obj, **schedule)            # 用这一份 schedule 的参数初始化
```

每一份 schedule（即 `__init__` 的一组超参取值）都会创建一个独立的 `script_obj`，再交给 `Transpiler` 翻译成 IR。这就解释了为什么 `__init__` 的参数（如 `block_m`）能产生多份不同的编译产物——**每个 schedule 一份内核**。

**⑥ `Script` 与 `tilus.Class` 的关系（顺带厘清）**

`Script` 继承自 `InstructionInterface`，因此 `self.*` 上挂着全部通用指令与硬件指令组：

[python/tilus/lang/script.py:41-42](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/script.py#L41-L42) —— `class Script(InstructionInterface)`，指令能力全部来自 `InstructionInterface`。

而 [python/tilus/lang/constructs/state.py:18-46](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/constructs/state.py#L18-L46) 里的 `tilus.Class` 同样继承 `InstructionInterface`，但它不是内核，而是「把内核逻辑拆成可复用组件」的辅助类（例如流水线 `Pipeline`）。二者的指令能力相同，区别在于 `Script` 有 `attrs`/`__call__` 启动语义、会被实例化成 `InstantiatedScript`，而 `Class` 只在被某个 `Script` 使用时随之一并转译。理解这点能避免把 `Script` 和 `Class` 混为一谈。

#### 4.1.4 代码实践

**实践目标**：亲眼确认 `Script(...)` 返回的是 `InstantiatedScript` 而不是 `Script`，并理解 `__init__` 在第 1 层不被调用。

**操作步骤**（基于 [examples/matmul/matmul_v0.py](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/examples/matmul/matmul_v0.py) 的 `MatmulV0`）：

1. 在 `MatmulV0.__init__` 第一行（`super().__init__()` 之前）加一句打印：

   ```python
   def __init__(self):
       print("[__init__] 被调用，self.block_m 即将被设置为 64")
       super().__init__()
       self.block_m = 64
       ...
   ```

2. 在脚本里实例化并打印类型：

   ```python
   matmul = MatmulV0()
   print("type(matmul) =", type(matmul).__name__)
   ```

3. 暂时 **不要** 调用 `matmul(...)`，先观察第 2 步的输出。

**需要观察的现象**：

- `type(matmul)` 打印出 `InstantiatedScript`，而 **不是** `MatmulV0` 或 `Script`。
- 第 2 步中 **看不到** `[__init__] 被调用` 的打印（因为第 1 层 `__new__` 没走 `__init__`）。

**预期结果**：当你随后调用 `matmul(m, n, k, a, b, c)` 时，才会出现 `[__init__] 被调用` 的打印（可能因并行转译出现多次，每个 schedule 一次）。这印证了 4.1.3 ⑤ 的结论：`__init__` 在转译阶段由 `JitInstance._instantiate_schedule` 显式调用。

> 如果手边没有 GPU，第 1、2 步仍可运行（实例化不触发编译），但第 3 步的调用需要可用的 CUDA target；若无法运行，明确标注「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：如果在 `Script.__new__` 里直接 `return cls(...)`（即返回一个 `Script` 实例自己），会发生什么？

**参考答案**：会无限递归——`cls(...)` 又会触发 `__new__`，再次 `return cls(...)`，直到栈溢出。这正是 Tilus 必须用 `object.__new__(script_cls)`（见 4.1.3 ⑤）来创建真实对象的原因。

**练习 2**：`InstantiatedScriptCache.cache` 是类级字典（`cache: dict = {}`）。如果你在同一个进程里写 `a = MatmulV0(); b = MatmulV0()`，`a is b` 成立吗？这意味着什么？

**参考答案**：成立（前提是构造参数相同，使得 `_normalize_key` 一致）。意味着同一进程内同一 Script + 同一构造参数只构造一次 `InstantiatedScript`，它持有的 `jit_instances` / `dispatch_table` 在多次调用间共享，从而避免重复编译。

---

### 4.2 编译期超参与运行时参数

#### 4.2.1 概念说明

Tilus 把「参数」分成两个世界：

| 世界 | 写在哪里 | 何时确定 | 改变它的后果 |
| --- | --- | --- | --- |
| **编译期超参（hyperparameters）** | `__init__` 的形参 / `self.xxx` | 编译时 | 产生一份 **不同的内核**（不同的 schedule） |
| **运行时参数（call params）** | `__call__` 的形参标注 | 运行时 | 依类型不同，可能触发 JIT、可能只查表、也可能只是 launch 入参 |

`__call__` 的参数看似都是「运行时」，但 Tilus 用 `CallParameters` 把它们再细分成三类，决定每个参数 **如何影响编译与调度**：

1. **常量参数（const_params）**：标注为 `int/float/bool/str`。值会 **逐字进入 JIT key**，换一个值就触发一次新的 JIT 编译。
2. **调优参数（tuning_params）**：标注为整数 `DataType`（如 `int32`/`int64`）。值不逐字进 JIT key，而是按「整除性指纹 + 取整桶」分别进入 JIT key 与 tuning key，用来 **在不重编译的前提下选最优 schedule**。
3. **内核参数（kernel_params）**：标注为 `DataType`（非整数，如 `~float16` 指针）。直接作为 CUDA launch 的实参传递，**不参与任何 key**。

这套分类是回答「什么时候触发 JIT」的核心。

#### 4.2.2 核心流程

`CallParameters.extract_params` 遍历 `__call__` 的形参，按下表分类（其中 `divisibility_key` 与取整桶见 4.2.3）：

```
对 __call__ 的每个参数 p（跳过 self）：
  ├─ 标注 ∈ {bool, int, float, str}         → const_param  （值逐字进 jit_key）
  ├─ 标注是 DataType 且 is_integer()         → tuning_param （指纹进 jit_key，桶进 tuning_key）
  │                                            同时也是 kernel_param
  └─ 标注是 DataType/PointerType（非整数）    → kernel_param （只作 launch 实参）
```

随后 `extract_keys(args, const_params, tuning_params)` 用调用实参算出两个 key：

```
jit_key     = [ args[i]            for i in const_params  ]   # 精确值
            + [ divisibility_key[arg % 32] for tuning ... ]   # 整除性指纹（当前实现恒为 1，见 4.2.3）

tuning_key  = [ round_up(arg)      for tuning_params     ]   # 取整到「桶」，用于 dispatch 查表
```

两个 key 的用途截然不同：

- **jit_key 决定「编译哪一份」**：`InstantiatedScript.__call__` 用 `(jit_key, tuning_key)` 查 dispatch 表；若 jit_key 对应的 `JitInstance` 不存在，就创建并编译。const 参数改值 → jit_key 变 → 重新编译。
- **tuning_key 决定「用哪份已编译内核」**：同一 jit_key 下，`JitInstance` 已经编译了所有 schedule（可能多份），`_pick_best_program` 用 tuning_key 查 dispatch_table 选最优那份。tuning 参数改值（但仍在同一桶内）→ tuning_key 不变 → **不重编译，只复用**。

> 一句话：**常量参数影响「编译」，调优参数影响「选型」**。

#### 4.2.3 源码精读

**① `CallParameters`：按标注三分类**

[python/tilus/lang/instantiated_script.py:230-302](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/instantiated_script.py#L230-L302) —— 读取 `__call__` 签名，强制要求每个参数都有标注，并把标注归类。

关键判定在 [L273-288](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/instantiated_script.py#L273-L288)：

```python
if isinstance(param.annotation, (DataType, PointerType, TensorPointerType)) or param.annotation in [
    bool, int, float, str,
]:
    ...
    annotation = param.annotation
    if annotation in [bool, int, float, str]:
        self.const_params.append(index)                 # pythonic 常量 → 进 JIT key
    else:
        self.kernel_params.append(index)                # hidet 类型 → launch 实参
        if isinstance(annotation, DataType) and annotation.is_integer():
            self.tuning_params.append(index)            # 整数 hidet 类型 → 还参与调优
```

还做了两项校验：参数必须是 `POSITIONAL_OR_KEYWORD`（[L261](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/instantiated_script.py#L261)）、必须带标注（[L265](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/instantiated_script.py#L265)），且 `from __future__ import annotations` 会被显式拒绝（[L289-296](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/instantiated_script.py#L289-L296)），因为那会把标注变成字符串、破坏上面的 `isinstance` 判定。

对照 `MatmulV0.__call__` 的签名 [examples/matmul/matmul_v0.py:57-65](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/examples/matmul/matmul_v0.py#L57-L65)：

| 参数 | 标注 | 分类 |
| --- | --- | --- |
| `m_size` | `int32` | tuning_param（整数 hidet 类型） + kernel_param |
| `n_size` | `int` | const_param |
| `k_size` | `int` | const_param |
| `a_ptr/b_ptr/c_ptr` | `~float16` | kernel_param（指针，非整数） |

所以 `n_size`/`k_size` 一变就会重编译；`m_size` 变化只走 dispatch 选型（除非跨桶，见 ②）。

**② `extract_keys` 与「取整桶」**

[python/tilus/lang/instantiated_script.py:327-358](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/instantiated_script.py#L327-L358) —— 用调用实参算 `(jit_key, tuning_key)`。

```python
for i in const_params:
    jit_key.append(args[i])                              # 精确值
for i in tuning_params:
    arg = args[i]
    jit_key.append(divisibility_key[arg % 32])           # 整除性指纹
    block = 1 << max((arg.bit_length() - 2), 0)
    tuning_key.append((arg + block - 1) // block * block)  # 向上取整到 block 的倍数
```

`tuning_key` 的「桶」由 `block` 决定。以 `m_size` 为例（设为 4096、4097、6144）：

- `arg=4096`：`bit_length=13`，`block = 1<<11 = 2048`，桶 = `(4096+2047)//2048*2048 = 4096`。
- `arg=4097`：`block` 仍为 2048，桶 = `(4097+2047)//2048*2048 = 6144`。
- `arg=6144`：桶 = 6144。

也就是说，`m_size` 落在 `[2049, 4096]` 都映射到桶 4096；`[4097, 6144]` 映射到 6144。**同一桶内的不同尺寸共用一份 dispatch 选择**，避免为每个尺寸都重新 benchmark。

**③ 一个诚实的小细节：`divisibility_key` 当前恒为全 1**

[python/tilus/lang/instantiated_script.py:310-324](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/instantiated_script.py#L310-L324) 初始化 `divisibility_key`：

```python
def _init_divisibility_key():
    global divisibility_key
    divisibility_key_list = []
    multiples = [1]                       # 注意：这个列表在循环中从未被追加
    for n in range(32):
        for m in reversed(multiples):     # 永远只有 m=1
            if n % m == 0:                # n % 1 恒为 0
                divisibility_key_list.append(m)
                break
        else:
            assert False
    divisibility_key = tuple(divisibility_key_list)   # → (1, 1, ..., 1) 共 32 个 1
```

`multiples` 始终是 `[1]`，所以 `divisibility_key = (1,)*32`，`divisibility_key[arg % 32]` 永远返回 1。**当前实现中，tuning 参数对 jit_key 的贡献恒为 1**——也就是说，目前 tuning 参数的变化不会经由整除性指纹触发重编译，真正区分编译与否的是 const 参数；tuning 参数只通过「桶」影响 dispatch 选型。读源码时看到这层「占位式」的设计，不要被变量名误导以为它在做复杂整除判定。

**④ `JitInstance`：一次 JIT 编译的载体**

当 `dispatch_table` 未命中、且对应 jit_key 的 `JitInstance` 不存在时，才会创建 `JitInstance`。它的构造函数尾部立刻触发转译：

[python/tilus/lang/instantiated_script.py:371-402](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/instantiated_script.py#L371-L402) —— 构造末尾调用 `self._transpile_programs()`，把每个 schedule 并行转译成 `Program`。

```python
class JitInstance:
    def __init__(self, script_cls, call_params, build_options, schedules, jit_key):
        ...
        self.schedules = schedules
        self.jit_key = jit_key
        self.transpiled_programs: list[Program] = []
        self.compiled_programs: list[CompiledProgram] = []
        self.dispatch_table: dict[TuningKey, int] = {}
        ...
        self._transpile_programs()        # 构造即转译
```

`_transpile_programs`（[L475-591](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/instantiated_script.py#L475-L591)）用 `parallel_imap` 并行地为每个 schedule 调用 `_instantiate_schedule`（即 4.1.3 ⑤ 里那个 `object.__new__` + `__init__` + `Transpiler.transpile` 的过程），然后把 IR 文本哈希成 8 位作为缓存目录名的一部分。构建（nvcc）则延迟到 `_pick_best_program` 里按需进行。

**⑤ 选优：`_pick_best_program`**

[python/tilus/lang/instantiated_script.py:700-771](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/instantiated_script.py#L700-L771) —— 用 tuning_key 查 dispatch 表；若没有，则对每份已编译内核做 `benchmark_func`，取最快者写回表。

注意 [L716-718](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/instantiated_script.py#L716-L718) 的优化：**只有一份已编译内核时直接跳过 benchmark**——这正是 `debug_schedule` 把空间压成单点后「不再调优」的实现原因（见 4.3）。

#### 4.2.4 代码实践

**实践目标**：用 const 参数 vs tuning 参数的对比，亲手验证「换 const 值会重编译，换 tuning 值（同桶）不会」。

**操作步骤**（基于 `MatmulV0`，把 `m_size` 视作 tuning、`n_size` 视作 const）：

1. 先设一个临时缓存目录，便于观察：

   ```python
   import tilus
   tilus.option.cache_dir("/tmp/tilus-u2l1-keys")
   ```

2. 固定 `n_size, k_size`（const），先用 `m_size=4096` 跑一次，再清空屏幕、用 `m_size=4097`（仍在桶 6144 内？请按 4.2.3 ② 自行核算）跑第二次。观察第二次是否出现 `Building`/`Tuning` 进度条。
3. 然后 **改 `n_size`**（const，例如 4096→2048）再跑一次，观察是否出现新的编译进度条。

**需要观察的现象**：

- 改 `m_size`（tuning）且仍落在原桶：无（或极少）编译，直接命中 dispatch；终端看不到 `Building` 进度条。
- 改 `n_size`（const）：触发新的 JIT 编译，终端出现 `Scheduling` / `Building` 进度条，缓存目录下出现新的 `scripts/matmul_v0/...` 子目录。

**预期结果**：与上面的分类一致。**待本地验证**：桶的边界与是否真命中 dispatch 受你的 GPU/target 与是否首次运行影响；若想稳定复现「同桶不重编译」，建议把 `m_size` 设为同一桶内的两个值（如 4096 与 4000，二者 `bit_length` 与 `block` 相同）。

#### 4.2.5 小练习与答案

**练习 1**：把 `MatmulV0.__call__` 里 `k_size: int` 改成 `k_size: int32`，会对性能与编译次数有什么影响？

**参考答案**：`k_size` 从 const 变成 tuning。原本 `k_size` 每取一个新值就重编译一次；改成 `int32` 后，不同 `k_size`（同桶）共用同一份已编译内核、只走 dispatch 选型，**编译次数显著减少**。代价是：内核内部无法再用 `k_size` 做「编译期常量折叠」（例如把 `range(k_size)` 展开成固定次数），可能损失一些编译期优化。这是「编译次数」与「单内核优化空间」的取舍。

**练习 2**：`a_ptr: ~float16` 属于哪一类？它会出现在 jit_key 里吗？

**参考答案**：属于 kernel_param（指针是非整数 DataType），既不是 const 也不是 tuning。它 **不出现在 jit_key 里**，只作为 launch 实参传给已编译的 launch 函数（见 `InstantiatedScript.__call__` 最后的 `kernel_args = (args[i] for i in self.kernel_params)`）。所以换一个输入张量指针不会触发重编译。

---

### 4.3 debug_schedule 调试

#### 4.3.1 概念说明

当内核挂了 `@autotune`，搜索空间往往是几十甚至上百个 schedule 的笛卡尔积，每次都要并行转译、构建、benchmark，既慢又难定位问题。`debug_schedule` 就是为这种场景准备的「紧急刹车」：

> **`debug_schedule` 是一个类属性，写成字典。一旦设置，它会直接覆盖整个 autotune 搜索空间，把候选 schedule 压成「仅此一份」。**

它的典型用途：

- **只编译一份**：在调试 IR / emitter / 缓存时，避免被庞大的搜索空间拖慢。
- **配合 `debug_block`**：`debug_block` 要求内核只剩唯一一份，否则报错（见 4.3.3 ③）。
- **快速复现**：把出问题的调度钉死，确保每次跑的都是同一份内核。

需要特别强调的是：`debug_schedule` 的键 **必须与 `__init__` 的形参同名**（且 `__init__` 必须把这些项作为形参），因为它是通过「把字典当作 `__init__` 的关键字参数」来注入的。

#### 4.3.2 核心流程

`generate_schedules` 里有一个二选一的分支：

```
if script_cls.debug_schedule:          # 设置了 debug_schedule
    spanned_space = [script_cls.debug_schedule]   # 只剩这一个候选
else:
    spanned_space = span_space(space)               # 笛卡尔展开全部 autotune 空间

for each item in spanned_space:
    init_kwargs = 用户传入 kwargs | item            # 注入到 __init__
    bound_args = signature(__init__).bind(self, **init_kwargs)
    schedule = bound_args.arguments（去掉 self）
```

也就是说：

- **不设 `debug_schedule`**：`span_space` 把所有 `@autotune` 维度做笛卡尔积，得到 N 个 schedule，全部转译+构建+benchmark（见 [u2-l4](u2-l4-autotune-and-schedule-space.md)）。
- **设了 `debug_schedule`**：`spanned_space` 只含这一个字典，只编译 1 份；且因 `_pick_best_program` 对「单份内核」跳过 benchmark（4.2.3 ⑤），**连调优也省了**。

#### 4.3.3 源码精读

**① `debug_schedule` 与 `debug_block` 的声明**

[python/tilus/lang/script.py:44-48](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/script.py#L44-L48) —— 两个调试开关都是 `Script` 的类属性，默认 `None`。

```python
# the compiled program will print the instruction output of the specified block
debug_block: Optional[tuple[int, int, int]] = None

# specify the schedule used for debugging. it will override any autotune space
debug_schedule: Optional[dict[str, Any]] = None
```

注释明确写着「it will override any autotune space」。

**② `generate_schedules` 的二选一分支**

[python/tilus/lang/instantiated_script.py:127-159](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/instantiated_script.py#L127-L159) —— 关键分支在 [L132-135](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/instantiated_script.py#L132-L135)。

```python
init_func = getattr(script_cls, "__init__")
init_args = [None] + list(script_args)        # 第一个位置给 self
signature = inspect.signature(init_func)

if script_cls.debug_schedule:
    spanned_space = [script_cls.debug_schedule]     # ← 覆盖整个空间
else:
    spanned_space = span_space(space)

for spanned_dict in spanned_space:
    conflict_names = set(spanned_dict) & set(script_kwargs)
    if conflict_names:
        raise ValueError(...)                      # 不允许与用户显式传入的 kwargs 冲突
    init_kwargs = dict(script_kwargs) | spanned_dict
    bound_args = signature.bind(*init_args, **init_kwargs)   # 注入到 __init__
    bound_args.apply_defaults()
    schedule = dict(bound_args.arguments)
    schedule.pop("self")
    schedules.append(schedule)
```

几个要点：

- `init_args = [None] + list(script_args)`：`signature.bind` 的第一个位置参数绑定到 `self`，这里用 `None` 占位（真正的 `self` 在 `_instantiate_schedule` 里用 `object.__new__` 创建）。
- [L138-144](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/instantiated_script.py#L138-L144) 的冲突检查：`debug_schedule` / autotune 维度不能与用户实例化时显式传入的同名 kwarg 重复。
- `schedule.pop("self")`：最终 schedule 只保留 `__init__` 的「真实超参」，去掉 `self`。

**③ `debug_block` 必须搭配 `debug_schedule`**

[python/tilus/lang/instantiated_script.py:643-644](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/instantiated_script.py#L643-L644) —— 构建完成后，若启用了 `debug_block` 但存在多份内核，直接报错。

```python
if self.script_cls.debug_block and len(self.compiled_programs) > 1:
    raise ValueError("Please specify the debug_schedule when debug_block is set. ")
```

因为 `debug_block` 的语义是「打印某个指定线程块的指令输出」，必须在唯一的内核上才有意义；所以它强制你先用 `debug_schedule` 把多选一压成单选一。

**④ 真实示例：`@autotune` 叠加 + `debug_schedule` 钉死**

`flash_attention_v1.py` 给出了标准写法——多个 `@autotune` 堆叠定义空间，再用 `debug_schedule` 指向其中一组取值：

[examples/attention/flash_attention_v1.py:15-23](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/examples/attention/flash_attention_v1.py#L15-L23) —— 三个 `@autotune` 各给一个候选，再由 `debug_schedule` 钉到单点。

```python
@tilus.autotune("num_warps", [4])
@tilus.autotune("block_q", [64])
@tilus.autotune("block_kv", [64])
class FlashAttention(tilus.Script):
    debug_schedule = dict(
        num_warps=4,
        block_q=64,
        block_kv=64,
    )
```

对应 [examples/attention/flash_attention_v1.py:25-34](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/examples/attention/flash_attention_v1.py#L25-L34) 的 `__init__`，`num_warps`/`block_q`/`block_kv` 都是 `__init__` 的形参——这是 `debug_schedule` 能注入的前提。

更复杂的 `matmul_a16wx.py` 把 4 个 `@autotune` 维度（候选数 3×5×2×3=90 个 schedule）用一行 `debug_schedule` 压成 1 个：[examples/quantization/matmul_a16wx.py:135-145](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/examples/quantization/matmul_a16wx.py#L135-L145)。

**⑤ `@autotune` 如何把空间写到类上**

[python/tilus/lang/script.py:106-152](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/script.py#L106-L152) —— 装饰器把 `{arg_names: arg_values}` 累积到类属性 `_autotune_space`，正是 `InstantiatedScript.__init__` 里 `getattr(script_cls, "_autotune_space", {})` 读取的对象。多个 `@autotune` 堆叠即多次合并进同一个 dict。

#### 4.3.4 代码实践

**实践目标**：为一个 matmul Script 设置 `debug_schedule`，确认只编译单个配置，并在缓存目录里看到「只有一份内核」。

**操作步骤**：

1. 以 `MatmulV0` 为基础做一处改造——把原本硬编码的 `block_m/block_n/block_k` 提为 `__init__` 形参（这是 `debug_schedule` 能注入的前提）：

   ```python
   class MatmulV0Debug(tilus.Script):
       debug_schedule = dict(block_m=64, block_n=64, block_k=16)   # 钉死单点

       def __init__(self, block_m: int, block_n: int, block_k: int):
           super().__init__()
           self.block_m = block_m
           self.block_n = block_n
           self.block_k = block_k

       def __call__(self, m_size: int32, n_size: int, k_size: int,
                    a_ptr: ~float16, b_ptr: ~float16, c_ptr: ~float16):
           ...  # 与 MatmulV0.__call__ 完全一致
   ```

2. 指定一个干净的缓存目录并编译一次：

   ```python
   import tilus, shutil
   shutil.rmtree("/tmp/tilus-u2l1-debug", ignore_errors=True)
   tilus.option.cache_dir("/tmp/tilus-u2l1-debug")
   kern = MatmulV0Debug()
   kern(4096, 4096, 4096, a, b, c)   # 假设 a,b,c 已就绪
   ```

3. 查看缓存目录结构：

   ```bash
   ls /tmp/tilus-u2l1-debug/scripts/matmul_v0_debug/*/
   cat /tmp/tilus-u2l1-debug/scripts/matmul_v0_debug/*/schedule.txt
   ```

**需要观察的现象**：

- 终端只出现 **一行** 编译（无 `Tuning` 进度条，因为只有单份内核会跳过 benchmark，见 4.2.3 ⑤）。
- `schedule.txt` 里只有 **一行** 记录（index 0），`programs/` 下只有一个软链接 `0`。

**预期结果**：与现象一致。作为对照，**删掉 `debug_schedule` 那一行**（并加 `@tilus.autotune("block_m", [64, 128])` 之类），重新清缓存编译，你会看到多份 schedule、出现 `Tuning` 进度条、`schedule.txt` 有多行。这一对照能直观体现 `debug_schedule` 的「压缩」作用。**待本地验证**：具体目录名含 `jit_key` 与 IR 哈希，以你本机实际生成为准。

#### 4.3.5 小练习与答案

**练习 1**：如果把 `debug_schedule = dict(block_m=64)`（漏写 `block_n/block_k`）会怎样？

**参考答案**：`generate_schedules` 用 `signature.bind(self, block_m=64)` 绑定 `__init__`。由于 `block_n`/`block_k` 没有默认值，`bind` 会抛 `TypeError`（缺少必填参数）。Tilus 会把错误加上调用处的文件名/行号后重新抛出（见 [instantiated_script.py:149-155](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/instantiated_script.py#L149-L155)）。所以 `debug_schedule` 必须覆盖 `__init__` 的所有无默认值形参，或让那些形参有默认值。

**练习 2**：`debug_schedule` 与 `InstantiatedScript.compile()`（编译但不执行）搭配，适合什么场景？

**参考答案**：适合 **CI 里的编译冒烟测试**——在不具备某架构 GPU（如本地没有 sm100a）的机器上，用 `debug_schedule` 钉一份调度、配合 `tilus.target.scope` 指定目标，调用 `instance.compile(...)` 只做「转译 + 构建」、不 launch 也不 benchmark，验证内核能在该 target 下编过。这正是 `compile()` 方法的用途（见 [instantiated_script.py:860-885](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/instantiated_script.py#L860-L885)）。

## 5. 综合实践

把本讲三个模块串起来，做一个「从黑箱到可观测」的小任务。

**任务**：把 `MatmulV0` 改造成一个 **可调优、可钉死** 的内核，并沿调用链验证每一层。

**步骤**：

1. **参数化 `__init__`**：把 `block_m/block_n/block_k` 提为 `__init__` 形参（参考 4.3.4）。
2. **加搜索空间**：挂两个 `@autotune`，例如：

   ```python
   @tilus.autotune("block_m", [64, 128])
   @tilus.autotune("block_k", [16, 32])
   class MatmulTunable(tilus.Script):
       ...
   ```

3. **观察完整流程**：清空缓存目录后调用一次，确认终端依次出现 `Scheduling`（4 个 schedule 并行转译）→ `Building`（4 份 nvcc 编译）→ `Tuning`（benchmark 选优）。
4. **加 `debug_schedule`**：在类体里加 `debug_schedule = dict(block_m=64, block_k=16)`，再次清缓存调用，确认只剩 1 份、无 `Tuning`。
5. **验证参数分类**：在两种取值下分别改 `m_size`（tuning，同桶）与 `n_size`（const），观察哪个会触发新的 `Scheduling`/`Building`（应只有改 const 时触发）。
6. **检查缓存产物**：打开 `schedule.txt`、`meta.json`（其中 `const_params`/`tuning_params`/`kernel_params` 正是 `CallParameters` 的分类结果，见 [instantiated_script.py:556-566](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/instantiated_script.py#L556-L566)），对照本讲 4.2 的分类表逐项核对。

**验收标准**：

- 能用一句话说清 `MatmulTunable()`（第 1 层，返回 `InstantiatedScript`，只做静态分析）与 `matmul(...)`（第 2 层，按 key 触发 JIT 或查表）的区别。
- 能从 `meta.json` 里读出哪些参数会触发重编译（const）、哪些只影响选型（tuning）、哪些只是 launch 实参（kernel）。
- 能用 `debug_schedule` 把 4 份内核压成 1 份，并在缓存目录里证实。

> 提示：`meta.json` 是连接本讲与后续 [u8-l1](u8-l1-caching-mechanism.md)（缓存机制）的桥梁，留意它的 `jit_key` 字段正是本讲 `extract_keys` 算出的常量值 + 整除性指纹。

## 6. 本讲小结

- **`Script(...)` 不返回 `Script`**：`Script.__new__` 拦截构造，经 `InstantiatedScriptCache.get` 返回一个 `InstantiatedScript`；因此你写的 `__init__` 在第 1 层 **不被调用**，而是在转译阶段由 `object.__new__` + 显式 `__init__(**schedule)` 触发（每个 schedule 一次）。
- **两层调用**：第 1 层（实例化）只做静态分析（`generate_schedules` + `CallParameters`），不编译；第 2 层（`__call__`）才按 `(jit_key, tuning_key)` 决定是否 JIT。
- **三类 `__call__` 参数**：const（`int/float/bool/str`，值进 jit_key，换值重编译）、tuning（整数 `DataType`，桶进 tuning_key 选型、不逐字重编译）、kernel（指针/非整数 `DataType`，只作 launch 实参）。
- **tuning 的「桶」**：`extract_keys` 把 tuning 参数向上取整到 2 的幂次桶，同桶尺寸共用 dispatch 选择，避免逐尺寸重编译。
- **`debug_schedule`**：一个类属性字典，直接覆盖整个 autotune 空间为单点，常用于调试、配合 `debug_block`、以及 CI 的 compile-only 冒烟测试。
- **诚实细节**：当前 `divisibility_key` 恒为全 1，tuning 参数经由整除性指纹对 jit_key 的贡献目前为常数 1——读源码时不要被命名误导。

## 7. 下一步学习建议

- 想深入 `@autotune` 装饰器与 `span_space` 的笛卡尔展开，以及 dispatch 缓存的环境指纹，请读 **[u2-l4 自动调优：@autotune 与调度空间](u2-l4-autotune-and-schedule-space.md)**。
- 想知道 `__call__` 里的 `self.global_view`/`load_global` 等调用到底是怎么变成 IR 的（即 `_instantiate_schedule` 里那个 `Transpiler`），请读 **[u3-l2 Transpiler：从 Python AST 到 Tilus IR](u3-l2-transpiler-ast-to-ir.md)**。
- 想了解 `JitInstance._transpile_programs` 之后、`build_program` 的完整编译六阶段，请读 **[u3-l1 编译流水线总览：build_program 全流程](u3-l1-compilation-pipeline-overview.md)**。
- 对缓存键如何计算、为何改 emitter 后要手动删缓存感兴趣，请读 **[u8-l1 缓存机制与缓存目录结构](u8-l1-caching-mechanism.md)**。
