# @asc.jit 装饰器：JITFunction 的设计与执行入口

## 1. 本讲目标

上一讲（u1-l5）我们站在「地图」层面看了一次 JIT 调用经过的五步链路。本讲要下钻到这条链路的「总调度室」——`python/asc/runtime/jit.py`，逐行精读 `JITFunction` 这个类。学完本讲，你应该能够：

1. 说清 `@asc.jit` 的两种写法（带括号传选项 / 不带括号直接装饰）分别走到哪段代码。
2. 解释 `kernel[core_num, stream](...)` 这句中括号启动语法是如何靠 `__getitem__` 实现的。
3. 读懂 `_run` 的六个动作：默认选项合并、选项分流、参数绑定、ConstExpr 分流、缓存编译、下发执行。
4. 理解 `JITFunction` 用三个**类属性**（`codegen`/`compiler`/`launcher`）组合出整条编译执行链的可扩展设计，以及为什么这是 pyasc 留给二次开发者的第一个扩展点。
5. 独立排查两类高频报错：「选项名不认识」和「参数名与配置关键字冲突」。

## 2. 前置知识

阅读本讲前，你只需要具备：

- **装饰器是什么**：`@asc.jit` 写在 `def` 上方，等价于 `my_kernel = asc.jit(my_kernel)`。装饰器本质是一个「接收函数、返回新对象」的函数。
- **Python 的下标语法糖**：`obj[k]` 会调用 `obj.__getitem__(k)`；`obj[a, b]` 会调用 `obj.__getitem__((a, b))`，即多个下标自动打包成元组。这是理解启动语法的全部前提。
- **dataclass（数据类）**：用 `@dataclass` 装饰的类，字段声明即构造参数，可通过 `dataclasses.fields()` 拿到字段名列表。pyasc 的三类「选项袋」都是 dataclass。
- **`inspect.signature(...).bind(...)`**：把实参按函数签名「绑定」成形参名到实参值的有序映射，但**不执行**函数。pyasc 用它来做参数分流。
- 回顾 u1-l5 的两个结论：AST 在装饰时就已抓取；三类选项袋中 `CodegenOptions`/`CompileOptions` 参与缓存 key，`LaunchOptions` 不参与。本讲会给出这两条结论在源码里的落点。

不需要 NPU 硬件也能完成本讲实践，但需要 `import asc` 能成功（即按 u1-l2 完成过源码安装，`libpyasc` 已编译）。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| `python/asc/runtime/jit.py` | JIT 总入口：`JITFunction` 类与 `jit` 装饰器 | 全部四个最小模块 |
| `python/asc/__init__.py` | 包入口，把 `jit` 与三个选项袋 re-export 成 `asc.jit` 等 | `@asc.jit` 这个名字从哪来 |
| `python/asc/codegen/function.py` | `Function` 基类：装饰时的源码/AST 捕获、`split_args` | 继承关系与 ConstExpr 分流的落点 |
| `python/asc/codegen/function_visitor.py` | `FunctionVisitor` 与 `CodegenOptions` | 只取 `CodegenOptions` 字段（ visitor 本身在 u4 详讲） |
| `python/asc/runtime/compiler.py` | 毕昇编译驱动 | 只取 `CompileOptions` 字段（u3-l4/l5 详讲） |
| `python/asc/runtime/launcher.py` | 任务下发 | 只取 `LaunchOptions` 字段（u3-l6 详讲） |
| `python/asc/common/compat.py` | 跨 Python 版本兼容工具 | `merge_dict` / `get_annotations` |
| `examples/08_rmsnorm/rmsnorm.py` | 真实示例 | `@asc.jit(kernel_type=...)` 的实际用法 |

## 4. 核心概念与源码讲解

### 4.1 JITFunction 与 @asc.jit 装饰器

#### 4.1.1 概念说明

`@asc.jit` 做的事只有一件：把一个普通 Python 函数包装成 `JITFunction` 实例。从这一刻起，这个对象就有了「双重身份」：

- 作为**对象**，它保存着函数的源码、AST、默认编译选项，并等待被 `[核数, 流](...)` 语法触发；
- 作为 `Function` 基类的子类，它甚至还保留了直接当函数调用的能力（后面 4.3 会看到这个有趣细节）。

`jit()` 支持两种写法，由 `fn` 是否为 `None` 区分：

- `@asc.jit`（不带括号）：`fn` 就是被装饰的函数，直接构造 `JITFunction`；
- `@asc.jit(kernel_type=..., debug=True)`（带括号）：先返回一个内层 `decorator`，再由它去构造 `JITFunction`。

源码里还用 `typing.overload` 为这两种写法分别声明了类型签名，方便 IDE 与类型检查器区分「返回 JITFunction」和「返回装饰器」两种返回类型。

#### 4.1.2 核心流程

```text
@asc.jit(kernel_type=AIV_ONLY)        @asc.jit
def kernel(...): ...                  def kernel(...): ...
        │                                   │
        ▼                                   ▼
jit(fn=None, kernel_type=...)         jit(fn=kernel)
        │                                   │
        ▼                                   │
返回内层 decorator ────────┐                │
                           ▼                ▼
                    decorator(kernel) = JITFunction(kernel, **options)
                           │
                           ▼
              JITFunction.__init__ 三步检查/初始化：
              ① super().__init__(fn)  → Function 基类抓源码与 AST
              ② get_clashed_args      → 参数名与配置关键字冲突？有则报错
              ③ unknown_options       → 选项名不在白名单？有则报错
              ④ 记录 default_options / launch_options / kernel_cache
```

#### 4.1.3 源码精读

先看 `jit` 函数本体。两个 `@overload` 只是类型声明，真正的实现在最后一个函数里，通过 `fn is None` 判断是哪种用法：

[python/asc/runtime/jit.py:L215-L235](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/jit.py#L215-L235)

上面这段代码定义了 `@overload` 的两种签名（不带选项 / 带选项），并在实现中：`fn is None` 时返回内层 `decorator`（对应带括号用法），否则直接 `decorator(fn)`（对应不带括号用法）；两条路径最终都落到 `JITFunction(fn, **options)`。

再看 `JITFunction.__init__`，这是本讲的第一段关键代码：

[python/asc/runtime/jit.py:L35-L46](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/jit.py#L35-L46)

这段构造函数依次完成：调用 `Function` 基类构造（抓取源码与 AST，见下）；`get_clashed_args` 检查被装饰函数的**形参名**是否与配置关键字撞名，撞了立刻抛 `RuntimeError`；`set(options).difference(self.get_config_keywords())` 检查**选项名**是否都在白名单里，不在则抛 `RuntimeError`；最后把合法选项存进 `self.default_options`，初始化空的 `LaunchOptions` 与进程内缓存字典 `kernel_cache`。

`get_clashed_args` 与 `get_config_keywords` 是这两道检查的实现：

[python/asc/runtime/jit.py:L59-L64](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/jit.py#L59-L64)

上面这段用 `inspect.signature(fn)` 拿到被装饰函数的形参表，与配置关键字集合求交集，交集非空即冲突。

[python/asc/runtime/jit.py:L125-L135](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/jit.py#L125-L135)

上面这段就是**配置关键字白名单的唯一来源**：遍历 `CodegenOptions`、`CompileOptions`、`LaunchOptions` 三个 dataclass，用 `dataclasses.fields` 收集全部字段名，结果缓存到类属性 `_config_keywords` 上避免重复计算。注意它在 staticmethod 里用的是 `__class__.get_config_keywords()`，这样子类覆写后依然走子类版本。

三个 dataclass 的字段加起来，就是完整的合法选项清单（截止当前 HEAD）：

| 选项袋 | 字段（即合法选项名） | 传递途径 | 参与缓存 key |
| --- | --- | --- | --- |
| `CodegenOptions` | `capture_exceptions`、`ir_multithreading` | 小括号或装饰器 | 是 |
| `CompileOptions` | `debug`、`strip_loc`、`verify_sync`、`print_ir_before_all`、`run_passes`、`kernel_type`、`opt_level`、`auto_sync`、`auto_sync_log`、`bisheng_options`、`always_compile`、`matmul_cube_only`、`insert_sync` | 小括号或装饰器 | 是 |
| `LaunchOptions` | `core_num`、`stream` | **只能**来自中括号 `kernel[...]` | 否 |

字段定义分别见 [python/asc/codegen/function_visitor.py:L36-L39](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/function_visitor.py#L36-L39)、[python/asc/runtime/compiler.py:L27-L41](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/compiler.py#L27-L41)、[python/asc/runtime/launcher.py:L48-L51](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/launcher.py#L48-L51)。

`super().__init__(fn)` 落到 `Function` 基类，这一步在**装饰时**（而不是第一次调用时）就完成源码与 AST 的捕获，细节属于下一讲 u3-l2，这里只给出落点：

[python/asc/codegen/function.py:L36-L49](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/function.py#L36-L49)

上面这段在构造时即用 `inspect` 抓取函数源码、起始行号，切成行列表存入 `self.src`，并把 `self.node` 设为解析出的 `ast.FunctionDef`。也就是说，`@asc.jit` 一执行完，函数体就再也不会按 Python 语义「重新解释」了。

最后，`@asc.jit` 这个名字是包入口 re-export 出来的：

[python/asc/__init__.py:L9-L25](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/__init__.py#L9-L25)

上面这段从各子模块导入 `CodegenOptions`、`CompileOptions`、`LaunchOptions` 与 `jit` 并列入 `__all__`，所以用户代码里 `asc.jit`、`asc.CodegenOptions` 都能直接使用。

真实示例可以参考 rmsnorm 算子的装饰器写法（`config` 即 `asc.runtime.config`）：

[examples/08_rmsnorm/rmsnorm.py:L70-L73](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/examples/08_rmsnorm/rmsnorm.py#L70-L73)

上面这段用带参形式 `@asc.jit(kernel_type=config.KernelType.AIV_ONLY)` 装饰核函数，`kernel_type` 正是 `CompileOptions` 的字段之一，因此能通过白名单检查并成为默认编译选项。

#### 4.1.4 代码实践

**实践目标**：亲手触发并解释 `JITFunction.__init__` 的两道守门检查，弄清 `get_config_keywords` 的来源。

**操作步骤**（示例代码，非项目原有文件）：

1. 确认环境已按 u1-l2 安装 pyasc（能 `import asc` 即可，无需 NPU）。
2. 编写脚本 `jit_guard_test.py`：

   ```python
   import asc
   import asc.runtime.config as config

   # ① 带合法选项的空算子：应当顺利通过装饰
   @asc.jit(kernel_type=config.KernelType.AIV_ONLY)
   def empty_kernel(x: asc.GlobalAddress, block_length: asc.ConstExpr[int]):
       pass

   print("empty_kernel decorated OK:", type(empty_kernel))

   # ② 故意传一个不存在的选项名
   @asc.jit(foo=1)
   def bad_option_kernel(x: asc.GlobalAddress):
       pass

   # ③ 参数名与配置关键字撞名（debug 是 CompileOptions 的字段）
   @asc.jit
   def clashed_kernel(debug, x: asc.GlobalAddress):
       pass
   ```

3. 依次注释掉 ②、③ 单独运行，记录每条的完整报错文本与抛出位置（装饰行还是调用行）。

**需要观察的现象**：

- ① 打印出 `empty_kernel decorated OK: <class 'asc.runtime.jit.JITFunction'>`（装饰本身不触发编译）。
- ② 在**装饰行**抛出 `RuntimeError: The following option names are unknown: foo`。
- ③ 在**装饰行**抛出 `RuntimeError: The following argument names conflict with JIT configuration options: debug`。

**预期结果与解释**：

- ② 的报错来自 `__init__` 中 `set(options).difference(self.get_config_keywords())` 的检查；白名单由 `get_config_keywords` 遍历三个 dataclass 的字段拼出，`foo` 不在其中。
- ③ 的报错来自 `get_clashed_args`：`debug` 同时是核函数形参名与 `CompileOptions` 字段名。之所以要禁止，是因为 `_run` 里的 `extract_kwargs` 会**按字段名从调用 kwargs 中把同名键抽走并删除**（见 4.3.3），若允许撞名，传给算子的实参值会被误当成编译配置抽走，轻则参数绑定失败，重则编译选项被污染——所以选择在装饰时快速失败（fail fast）。
- 同理，`core_num`、`stream`、`ir_multithreading` 等所有字段名都不能作为核函数形参名。

以上运行结果为**待本地验证**（本讲义编写环境未安装编译好的 pyasc，报错文案以你本机实际输出为准）。

#### 4.1.5 小练习与答案

**练习 1**：`@asc.jit` 和 `@asc.jit()`（带空括号）行为有区别吗？

**答案**：没有实质区别但机制不同。`@asc.jit` 走 `jit(fn=kernel)` 分支直接返回 `JITFunction`；`@asc.jit()` 的 `fn` 是 `None`，返回内层 `decorator` 再作用于函数，最终同样得到 `JITFunction(kernel)`。两者得到的对象等价，前者少一层闭包。

**练习 2**：为什么 `get_config_keywords` 要把结果缓存到类属性 `_config_keywords`？

**答案**：三个 dataclass 的字段集合在运行期不变，而 `get_clashed_args` 每次装饰、`__init__` 每次检查都要用这份清单；缓存避免每次重复反射 `fields()`。同时它用 `getattr(cls, attr, None)` 先查缓存，属于典型的「计算一次、挂到类上」的记忆化写法。

**练习 3**：如果把核函数的形参命名为 `stream`，会在什么时候报错？为什么？

**答案**：在装饰时（`@asc.jit` 执行时）就报错。因为 `stream` 是 `LaunchOptions` 的字段，会进入 `get_config_keywords` 的白名单，`get_clashed_args` 求交集非空，`__init__` 立即抛 `RuntimeError`，根本等不到调用。

### 4.2 __getitem__ 启动语法：kernel[核数, 流] 是怎么工作的

#### 4.2.1 概念说明

pyasc 沿用了异构编程里「启动配置与调用参数分离」的惯例：小括号里只放算子的**数据参数**，而「用多少个核、下到哪条流」这类**执行配置**放进中括号。这套语法不需要任何编译器魔法，靠的就是 Python 的下标协议：`kernel[8](x, y)` 会被解释成 `(kernel.__getitem__(8))(x, y)`。

`__getitem__` 返回的不是执行结果，而是**绑定方法 `self._run`**——一个可调用对象。中括号负责把启动配置暂存到 `self.launch_options`，随后的圆括号调用才真正走编译与下发。

#### 4.2.2 核心流程

```text
kernel[8](gm_x, gm_y, 1024)
   │
   ├─ kernel[8]        →  __getitem__(8)
   │      LaunchOptions(core_num=8) 存入 self.launch_options
   │      返回 self._run
   │
   └─ (self._run)(gm_x, gm_y, 1024)   →  进入 _run 主流程（见 4.3）
          读取 self.launch_options，编译并下发

kernel[8, stream](...)  →  __getitem__((8, stream))  →  LaunchOptions(8, stream)
kernel[(8, stream)](...) 与上等价（本来就是同一个元组）
```

#### 4.2.3 源码精读

[python/asc/runtime/jit.py:L48-L57](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/jit.py#L48-L57)

上面这段实现了完整的中括号语法：单个 `int` 下标被解释为核数，构造成 `LaunchOptions(core_num=...)`；元组下标则按位置解包成 `LaunchOptions(*user_launch_options)`，即 `(core_num, stream)` 顺序。任何构造异常都会被捕获并统一改抛 `TypeError("Parse user launch options failed")`，避免让用户直面 dataclass 的内部报错；构造成功后走到 `else` 分支返回 `self._run`。

`LaunchOptions` 只有两个字段，顺序就是中括号里的填写顺序：

[python/asc/runtime/launcher.py:L48-L51](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/launcher.py#L48-L51)

上面这个 dataclass 定义了 `core_num`（默认 0）与 `stream`（默认 `None`）。`core_num=0` 与 `stream=None` 的具体语义由 Launcher 决定，留到 u3-l6 展开。

有两个值得注意的细节：

1. **`launch_options` 是实例状态**。连续写 `kernel[8](...)` 再 `kernel[4](...)` 没问题，因为每次 `__getitem__` 都会整体覆盖 `self.launch_options`；但如果把 `run8 = kernel[8]` 和 `run4 = kernel[4]` 两个句柄都存下来再依次调用，两个句柄共享同一个 `JITFunction` 实例，实际生效的会是**最后一次中括号**写入的配置。日常顺序使用不受影响。
2. **为什么 `LaunchOptions` 不参与缓存 key**。核数与流只影响「这一次怎么执行」，不影响生成的 Kernel 二进制内容，所以 u1-l5 提到的「中括号选项不参与缓存 key」在源码上的落点就是：`_run` 里根本不会把 `self.launch_options` 传给 `_cache_kernel`，只在最后的 `_run_launcher` 处使用。

#### 4.2.4 代码实践

**实践目标**：验证 `kernel[...]` 返回的是可调用对象，且中括号参数就是 `LaunchOptions` 的构造参数。

**操作步骤**（示例代码）：

1. 在 4.1 实践脚本 ① 的 `empty_kernel` 基础上追加：

   ```python
   launcher = empty_kernel[4]
   print("type of kernel[4]:", type(launcher))          # 应为绑定方法 _run
   print("launch_options now:", empty_kernel.launch_options)
   empty_kernel[4, None]
   print("launch_options now:", empty_kernel.launch_options)
   ```

2. 再试一个非法下标 `empty_kernel["four"]`，观察报错。

**需要观察的现象**：`type(launcher)` 应打印出 bound method 之类的形式（`<bound method JITFunction._run of ...>`）；两次 `launch_options` 分别是 `LaunchOptions(core_num=4, stream=None)` 的数据类表示；非法下标抛出 `TypeError: Parse user launch options failed`。

**预期结果**：与上一致。说明中括号只是「改状态 + 返回 `_run`」，真正的编译执行发生在圆括号。**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：`kernel[16]` 与 `kernel[(16,)]` 等价吗？

**答案**：等价。`kernel[16]` 传入 `int` 16；`kernel[(16,)]` 传入单元素元组，走 `LaunchOptions(*user_launch_options)` 即 `LaunchOptions(16)`，两条路径构造出相同的 `core_num=16`。

**练习 2**：如果用户写成 `kernel[core_num=8]`（中括号里用关键字），会发生什么？

**答案**：会抛 `TypeError: Parse user launch options failed`。中括号内容被当成元组交给 `LaunchOptions(*user_launch_options)`，而这里的下标其实是一个 `slice` 或直接语法报错（`kernel[core_num=8]` 在 Python 3.9 及以前是语法错误，3.10+ 会传 slice），无论哪种都无法按 `(core_num, stream)` 位置解包成功，最终被 `except` 捕获改抛统一错误。中括号只支持位置形式。

### 4.3 _run 主流程：选项分流、参数绑定与三步调度

#### 4.3.1 概念说明

`_run` 是整条 JIT 链路的总调度函数，只有 9 行有效代码，却串起了 u1-l5 地图里的全部五步。它要解决三个问题：

1. **选项从哪来**：装饰器默认选项与调用时选项要合并，再按三个 dataclass 分流。
2. **参数怎么分**：调用实参要先经过签名绑定，再按类型标注切成「运行时参数」和「ConstExpr 编译期常量」。
3. **链路怎么串**：查缓存（未命中则 codegen + compiler）→ launcher 下发。

#### 4.3.2 核心流程

```text
_run(*args, **kwargs)
 ① merge_dict(default_options, kwargs)        # 调用时选项覆盖装饰器默认
 ② extract_kwargs(CodegenOptions, kwargs)     # 抽走 codegen 选项，并从 kwargs 删除
    extract_kwargs(CompileOptions, kwargs)    # 抽走 compile 选项，并从 kwargs 删除
    （剩下的 kwargs 只可能是核函数的具名实参）
 ③ signature(fn).bind(*args, **kwargs)        # 按原函数签名绑定，得到 {形参名: 实参值}
 ④ split_args(call_args, annotations)         # 按 ConstExpr 标注分流
        → runtime_args（进 kernel ABI）/ constexprs（进 IR 与缓存 key）
 ⑤ _cache_kernel(...) → CompiledKernel        # 两级缓存；未命中走 _run_codegen + _run_compiler
 ⑥ _run_launcher(binary, self.launch_options, tuple(runtime_args.values()))
```

#### 4.3.3 源码精读

先看总调度本体：

[python/asc/runtime/jit.py:L204-L212](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/jit.py#L204-L212)

上面这段就是 `_run` 全部逻辑：先用 `merge_dict` 把装饰器默认选项与本次调用 kwargs 合并（调用时优先）；再用 `extract_kwargs` 两次抽走 `CodegenOptions` 与 `CompileOptions` 的字段；接着用 `inspect.signature(self.fn).bind(...)` 把剩余实参按原函数签名绑定为 `call_args`；`split_args` 依据类型标注切出 `runtime_args` 与 `constexprs`；`_cache_kernel` 负责产出 `CompiledKernel`（含缓存查找与真正的 codegen/compile）；最后 `_run_launcher` 用 `__getitem__` 暂存的启动选项把 kernel 下发执行。

`merge_dict` 的语义决定了优先级（`dict1 | dict2` 中后者覆盖前者）：

[python/asc/common/compat.py:L48-L53](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/common/compat.py#L48-L53)

上面这段是跨版本兼容实现：Python 3.9+ 用字典合并运算符 `|`，旧版本用 `{**d1, **d2}`。两种写法都是第二个参数（调用时 kwargs）覆盖第一个参数（装饰器默认选项）。

`extract_kwargs` 是选项分流的关键，注意它会**删除**抽走的键：

[python/asc/runtime/jit.py:L96-L104](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/jit.py#L96-L104)

上面这段遍历目标 dataclass 的字段名，凡是在 `base_kwargs` 中出现的同名键都被搬进 `kwargs` 并从 `base_kwargs` 里 `del` 掉，最后用收集到的键构造 dataclass 实例。**这个 `del` 正是 4.1 中禁止参数名撞配置关键字的根本原因**：若核函数有个形参恰叫 `debug`，用户 `kernel(x, debug=3.14)` 里的 `3.14` 会被这里当成编译选项抽走，`bind` 时该形参便无值可绑，且 `CompileOptions.debug` 被污染成 `3.14`。还要注意 `LaunchOptions` 不经过 `extract_kwargs`——它只来自中括号，这正是「中括号选项不参与缓存 key」的机制落点。

参数绑定后，`split_args` 完成运行时/编译期分流（其内部逻辑属于 u3-l3 主题，这里看调用点即可）：

[python/asc/codegen/function.py:L119-L132](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/function.py#L119-L132)

上面这段按类型标注逐个判断：标注是 `ConstExpr`（含 `ConstExpr[int]` 这类下标形式）的实参进入 `constexprs`，其余进入 `runtime_args`。回到 `_run`，`runtime_args.values()` 最终被元组化后交给 launcher（第 212 行），而 `constexprs` 只影响 codegen 与缓存 key，不进入 kernel ABI。

缓存环节与 u1-l5 的结论一一对应：

[python/asc/runtime/jit.py:L156-L182](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/jit.py#L156-L182)

上面这段先对每个运行时实参求 `get_arg_type`，拼出缓存因子；`kernel_cache.get(mem_cache_key, None)` 命中进程内缓存则直接返回；否则算文件缓存键、尝试从落盘文件 `pickle.load` 出 `CompiledKernel`；都未命中才走 `self._run_codegen(...)`（AST→ASC-IR）和 `self._run_compiler(...)`（Pass + 毕昇编译）。三处 `not compile_options.always_compile` 条件说明 `always_compile=True` 既不读缓存也不写缓存，完全绕过。

最后补充一个容易忽略的细节——`JITFunction` 并没有覆写 `__call__`，直接圆括号调用会走 `Function` 基类的实现：

[python/asc/codegen/function.py:L60-L61](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/function.py#L60-L61)

上面这两行让 `kernel(x, y)`（不带中括号）退化为调用**原始 Python 函数**。对核函数而言这通常没有意义（函数体里是 `asc.GlobalTensor` 等 JIT 专用对象），但这个设计保证了 `JITFunction` 在任何「把它当普通函数用」的场合不会炸掉，也常被单元测试用来执行 Host 侧辅助逻辑。真正走 JIT 的入口只有一个：`kernel[...](...)`。

#### 4.3.4 代码实践

**实践目标**：用一个最小可运行算子验证「小括号选项覆盖装饰器默认选项」与「调用时传未知选项」的行为。

**操作步骤**（示例代码，基于 examples/01_add 的结构简化）：

1. 准备 `option_flow.py`：

   ```python
   import asc
   import asc.runtime.config as config

   @asc.jit(kernel_type=config.KernelType.AIV_ONLY)   # 装饰器默认：AIV_ONLY
   def add_kernel(x: asc.GlobalAddress, y: asc.GlobalAddress, z: asc.GlobalAddress,
                  block_length: asc.ConstExpr[int]):
       ...  # 照抄 examples/01_add/add.py 的核函数体

   # 调用时用小括号覆盖/追加选项（合法）：
   # add_kernel[8](x, y, z, 1024, debug=True)
   # 调用时传未知选项（非法）：
   # add_kernel[8](x, y, z, 1024, bar=1)
   ```

2. 在 Model 模式下（参考 `config.set_platform` 的用法，见 examples/01_add）分别放开两条调用，记录输出。

**需要观察的现象**：

- `debug=True` 走的是 `extract_kwargs(CompileOptions, kwargs)`，被抽走后不参与参数绑定；同时因为 `debug` 参与缓存因子，开启后应生成新的缓存条目（可与 u1-l5 的 PYASC_DUMP_PATH 实践结合观察）。
- `bar=1` 不是任何 dataclass 字段，两次 `extract_kwargs` 都不会抽走它，残留到 `inspect.signature(self.fn).bind(*args, **kwargs)` 时由 **inspect** 抛出 `TypeError: got an unexpected keyword argument 'bar'` 一类的错误——注意这条错误发生在**调用时**而非装饰时，与 4.1 中装饰时的白名单检查是两道不同的防线。

**预期结果**：合法调用正常编译执行；非法调用报 inspect 绑定错误。**待本地验证**（需已安装 pyasc 且配置好 Model/NPU 之一）。

#### 4.3.5 小练习与答案

**练习 1**：`_run` 里为什么先 `extract_kwargs` 再 `bind`，反过来行不行？

**答案**：不行。`bind` 必须只看到「属于核函数签名的实参」才能绑定成功；若先 `bind`，混在 kwargs 里的 `debug=True` 等选项键会导致「意外的关键字参数」错误。先抽出并删除选项键，剩下的才是纯调用参数。

**练习 2**：`@asc.jit(kernel_type=AIV_ONLY)` 装饰后，某次调用又写了 `kernel_type=None`（小括号），最终生效哪个？

**答案**：生效调用时的 `None`。`merge_dict(self.default_options, kwargs)` 中 kwargs 覆盖默认值，`kernel_type` 属于 `CompileOptions` 字段，会以调用值进入本次编译选项并参与缓存 key。

**练习 3**：`always_compile=True` 时，两级缓存分别表现为什么行为？

**答案**：都不生效。代码中读内存缓存（第 161 行）、读文件缓存（第 169 行）、写文件缓存（第 178 行）三处都被 `not compile_options.always_compile` 短路，每次调用都完整重跑 codegen 与编译，适合调试生成产物时使用。

### 4.4 组合式设计：可替换的 codegen / compiler / launcher

#### 4.4.1 概念说明

`JITFunction` 没有把「AST→IR」「IR→二进制」「二进制→执行」三段逻辑写死在自己身体里，而是通过三个**类属性**持有实现类：

```python
class JITFunction(Function[P, T]):
    codegen: Type[FunctionVisitor] = FunctionVisitor
    compiler: Type[Compiler] = Compiler
    launcher: Type[Launcher] = Launcher
```

这是典型的**组合优于继承 + 策略模式**：三个属性的类型是 `Type[...]`（类本身而非实例），在需要时才实例化。想换掉某一段（例如自定义一个做特殊 IR 改写的 visitor、或替换 launcher 做抓包统计），只需子类化 `JITFunction` 并覆写一个类属性，整条链路的其余部分原样复用。

#### 4.4.2 核心流程

```text
JITFunction（默认实现）
 ├── self.codegen  = FunctionVisitor   # _run_codegen 中实例化：AST → ir.ModuleOp
 ├── self.compiler = Compiler          # _run_compiler 中实例化：ModuleOp → CompiledKernel
 └── self.launcher = Launcher          # _run_launcher 中实例化：CompiledKernel → 设备执行

替换方式（示例代码）：
 class MyJIT(JITFunction):
     codegen = MyVisitor          # 只换前端，compiler/launcher 自动沿用

 def my_jit(fn=None, **options):
     ... return MyJIT(fn, **options)   # 再包一个自己的装饰器即可
```

#### 4.4.3 源码精读

三个类属性与三个使用点：

[python/asc/runtime/jit.py:L30-L33](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/jit.py#L30-L33)

上面三行声明了默认策略：`codegen` 指向 `FunctionVisitor`（AST 遍历器，u4 单元主角），`compiler` 指向 `Compiler`（MLIR Pass + 毕昇编译，u3-l4/l5），`launcher` 指向 `Launcher`（参数打包与下发，u3-l6）。由于是类属性，所有实例共享默认值；子类覆写即改变整条链。

三个使用点分别在自己的 `_run_*` 方法里实例化策略类：

[python/asc/runtime/jit.py:L184-L202](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/jit.py#L184-L202)

上面这段是三个策略的调用现场。`_run_codegen`：先 `create_context()` 建 MLIR 上下文，把 `global_builder`（u2-l5 讲过的全局 builder 单例）指到该上下文，再用 `self.codegen(...)` 构造 visitor 并 `visitor.visit(self.node)` 遍历装饰时抓好的 AST，最后取出 `global_builder.get_ir_module()` 返回 `ir.ModuleOp`；`finally` 里的 `global_builder.teardown()` 保证无论成败都复位全局状态（这也是 u2-l6 提过「TPipe 编译期强制、teardown 自动复位」的时机）。`_run_compiler` 与 `_run_launcher` 则分别实例化 `self.compiler(options)` 与 `self.launcher(options)` 并委派执行。

MLIR 上下文的创建是一个静态工厂：

[python/asc/runtime/jit.py:L106-L111](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/jit.py#L106-L111)

上面这段创建 `ir.Context`（来自 pybind 扩展 `asc._C`），先关闭多线程保证 IR 构建的线程安全，再 `load_dialects` 注册 Asc/EmitAsc 等方言。`CodegenOptions.ir_multithreading` 为 `False` 时 `_run_codegen` 会再补一次 `disable_multithreading()`（幂等的双保险）；默认该选项为 `True`，表示「不额外限制」，而上下文创建时本就已关闭多线程。

这套设计带来的直接收益可以在 `get_arg_type` 末尾看到——测试设施也能挂进同一框架：

[python/asc/runtime/jit.py:L238-L247](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/jit.py#L238-L247)

上面这段定义了 `MockTensor` / `MockValue` 两个哑对象，让 `get_arg_type` 在没有 torch/numpy 真实张量时也能为「假参数」推出口径正确的参数类型，服务于单元测试（参数分类规则本身是 u3-l3 的主题）。

#### 4.4.4 代码实践

**实践目标**：通过子类化替换 `launcher`，体验「换一段策略、其余不动」的扩展方式，并用日志验证替换生效。

**操作步骤**（示例代码）：

1. 编写 `my_jit.py`：

   ```python
   import asc
   from asc.runtime.jit import JITFunction
   from asc.runtime.launcher import Launcher

   class LoggingLauncher(Launcher):
       def run(self, kernel, fn_name, runtime_args):
           print(f"[LoggingLauncher] about to launch '{fn_name}' with {len(runtime_args)} runtime args")
           return super().run(kernel, fn_name, runtime_args)

   class MyJIT(JITFunction):
       launcher = LoggingLauncher

   def my_jit(fn=None, **options):
       if fn is None:
           return lambda f: MyJIT(f, **options)
       return MyJIT(fn, **options)
   ```

2. 把 examples/01_add/add.py 中的 `@asc.jit` 换成 `@my_jit`（其余不动），在 Model 模式下运行。

**需要观察的现象**：每次下发前多打印一行 `[LoggingLauncher] about to launch ...`；算子计算结果与原版完全一致（因为我们只包了一层日志，最终仍调 `super().run`）。

**预期结果**：证明 `JITFunction` 的三段链路可以独立替换，且替换点全部集中在三个类属性上。**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `codegen`/`compiler`/`launcher` 存「类」而不是在 `__init__` 里存「实例」？

**答案**：三段的构造参数各不相同且要到执行时才确定（codegen 需要 spec/options，compiler/launcher 需要各自的 options 实例），存类可以延迟到 `_run_*` 里再带上当次参数实例化；同时类属性可以被子类一行覆写，天然构成策略替换点。

**练习 2**：如果你只想统计每次编译耗时而不关心下发，应该覆写哪个属性/方法？

**答案**：覆写 `compiler` 类属性，提供 `Compiler` 的子类并在其 `run` 外包计时；或直接子类化 `JITFunction` 覆写 `_run_compiler` 方法。下发统计才动 `launcher`。

**练习 3**：`global_builder.teardown()` 放在 `finally` 里有什么意义？

**答案**：`global_builder` 是进程级单例，若 codegen 中途抛异常而没有复位，残留状态会污染下一次 JIT 编译（例如上一个 kernel 的插入点/TPipe 约束）。`finally` 保证成功、失败两条路径都会清理，这也是 pyasc 前端「编译期状态不跨 kernel 泄漏」的机制保障。

## 5. 综合实践

把本讲四个模块串成一张「亲手推导」的任务：

1. **抄一条真实调用**。打开 [examples/08_rmsnorm/rmsnorm.py:L70-L73](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/examples/08_rmsnorm/rmsnorm.py#L70-L73)，找到该文件中 `rmsnorm_kernel[...]` 的启动调用行，抄下完整的 `kernel[核数](实参...)` 语句。
2. **逐段标注**。在这条语句旁用注释标出：中括号内容进入 `__getitem__` 的哪个分支、`LaunchOptions` 最终的字段值；圆括号里的每个实参在 `split_args` 之后落在 `runtime_args` 还是 `constexprs`（依据第 71-73 行的类型标注）。
3. **跟踪一条选项**。该示例装饰器传了 `kernel_type=config.KernelType.AIV_ONLY`。请沿着 `_run` → `merge_dict` → `extract_kwargs(CompileOptions, ...)` → `_gen_cache_factors` 的路径，说明这个值如何进入编译选项与缓存 key（提示：[python/asc/runtime/jit.py:L137-L154](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/jit.py#L137-L154) 中 `vars(compile_options)` 的拼接方式意味着改任何一个编译选项都会生成新 key）。
4. **画出类图**。用纸笔画出 `jit` 函数、`JITFunction`、`Function`、三个策略类之间的关系（谁装饰谁、谁继承谁、谁组合谁），并标出 `__getitem__` 返回 `_run` 这条边。
5. **写一份 20 行以下的「报错排查卡」**：汇总本讲遇到的三类报错（未知选项名 / 参数名撞关键字 / 调用时未知 kwarg），各写一行「现象 → 抛出时机 → 根因代码行」。

完成后，你应该可以不看讲义复述 `_run` 的六个动作。

## 6. 本讲小结

- `@asc.jit` 有两种写法，靠 `jit(fn=None, **options)` 中 `fn` 是否为 `None` 区分，最终都构造 `JITFunction(fn, **options)`；装饰时就完成 AST 捕获与两道守门检查（参数名撞配置关键字、选项名不在白名单）。
- 配置关键字白名单的唯一来源是 `get_config_keywords`：`CodegenOptions` + `CompileOptions` + `LaunchOptions` 三个 dataclass 的全部字段名。
- `kernel[核数, 流](...)` 完全由 `__getitem__` 实现：int/tuple 分别构造 `LaunchOptions`，随后返回绑定方法 `self._run`，圆括号才真正触发编译执行；`LaunchOptions` 因此天然不参与缓存 key。
- `_run` 六个动作：合并默认选项 → 两次 `extract_kwargs` 分流选项（会删除抽走的键，这是禁止参数撞名的根因）→ `signature.bind` 绑定参数 → `split_args` 切分运行时参数与 ConstExpr → `_cache_kernel`（两级缓存，`always_compile` 全绕过）→ `_run_launcher` 下发。
- `JITFunction` 用 `codegen`/`compiler`/`launcher` 三个类属性组合整条链路，子类覆写一个属性即可替换一段策略，这是 pyasc 前端的第一个扩展点。
- 不带中括号直接 `kernel(...)` 会走 `Function.__call__` 执行原始 Python 函数；JIT 的唯一入口是 `kernel[...](...)`。

## 7. 下一步学习建议

- **u3-l2（函数对象与源码捕获）**：本讲多次停在 `super().__init__(fn)` 这一步，下一讲深入 `Function` 基类的源码抓取、`cache_key` 哈希构成与 `DependenciesFinder` 全局依赖分析。
- **u3-l3（参数特化）**：`split_args` 切出的 `runtime_args` 如何进一步映射为 `PlainArgType`/`PointerArgType`/`StructArgType`，直接决定 kernel 的参数 ABI。
- **动手预习**：通读 [python/asc/runtime/jit.py](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/jit.py) 全文（不到 250 行），确认你能为每一行方法写出一句中文用途说明——这是检验本讲成果的最好方式。
