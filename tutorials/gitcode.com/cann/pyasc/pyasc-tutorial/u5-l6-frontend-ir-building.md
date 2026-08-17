# u5-l6 OpBuilder 使用侧：前端如何创建 IR

## 1. 本讲目标

上一讲（u5-l5）我们站在 C++ 侧看了 pybind 桥接层：`PyOpBuilder` 如何携带 Location、`create_asc_*` 系列方法如何一半手写一半由 TableGen 生成。本讲调转方向，站到 **Python 使用侧**，回答三个问题：

1. `asc.add`、`asc.matmul` 这些 language 层 API 是普通 Python 函数，它们的签名里并没有 builder 参数，那 IR Operation 到底是从哪里、被谁、在什么时候创建的？（答案：`global_builder` 单例）
2. 为什么 `language/basic`、`language/adv` 下上百个 API 文件长得几乎一模一样？这套「固定三段式」套路是什么，掌握它之后为什么就能独立读懂任何陌生 API 文件？
3. `create_asc_*` 吃进去、吐出来的都是 C++ 句柄（`IRHandle`），Python 侧的 Tensor、标量、Matmul 对象是如何在「Python 对象」和「IR 句柄」之间来回转换的？

学完本讲，你应该能拿着一个从没用过的 API 文件（如 `vec_reduce.py`、`fixpipe.py`），不看文档、只靠读源码写出它的最小用例 kernel 并生成合法 IR。

## 2. 前置知识

本讲是第 5 单元收尾讲，默认你已理解以下内容（不重复展开）：

- **u1-l5 / u3-l1**：`JITFunction._run` 五步主链路，其中第三步 `_run_codegen` 产出 `ir.ModuleOp`。
- **u2-l3（IRValue 体系）**：`IRHandle` 是 pybind11 暴露的 `ir.Value` 裸句柄；`IRValue` 是它的 Python 包装协议（`from_ir`/`to_ir`）；`PlainValue` 表示设备侧延迟求值标量；`RuntimeInt` 等类型别名让参数位同时容纳 IR 值与 Python 立即数。
- **u2-l5（基础 API）**：`OverloadDispatcher` 按注册顺序做类型试配，弥补 Python 无运行时重载；`require_jit` 守门。
- **u5-l1（四名合一）**：Python 方法 `create_asc_AddL2Op` ↔ C++ 类 `AddL2Op` ↔ IR 操作 `ascendc.AddL2` ↔ Ascend C `Add` 的 count 形态。注意 dump 出的 IR 前缀是 `ascendc.`，而 pybind 方法名前缀缩写为 `asc_`。
- **u5-l5（pybind 桥接层）**：`libpyasc` 挂出 `ir`、`passes`、`translation` 三个子模块，`Builder` 藏在 `ir` 里；`create_asc_*` 双轨来源（手写 + TableGen 生成）。

还需要两个 MLIR 术语的直觉：

- **插入点（insertion point）**：builder 内部记录的「下一个 Operation 插到哪个块、哪个位置之后」。构建 IR 就是不断移动插入点并创建 Operation。
- **SSA 值**：IR 里的每个值只被定义一次，`create_asc_*` 的返回值就是新 Operation 的结果 SSA 值（句柄）。

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| `python/asc/language/core/utils.py` | `GlobalBuilder` 类与 `global_builder` 单例、`require_jit`、`OverloadDispatcher`（本讲主角一） |
| `python/asc/runtime/jit.py` | `_run_codegen`：global_builder 的生命周期由它 set 与 teardown |
| `python/asc/language/basic/vec_binary.py` | 三段式套路的标准样本（`add`、`mul_cast`） |
| `python/asc/language/basic/utils.py` | `op_impl` 公共委托函数、`check_type` 类型校验表（三段式的「第二段半」） |
| `python/asc/language/basic/vec_reduce.py` | 综合实践的目标文件（另一个变体写法 `reduce_op_impl`） |
| `python/asc/language/adv/matmul.py` | 高阶 API 侧的进阶样本：双构造器、返回值包装、手工搭控制流 |
| `python/asc/language/core/ir_value.py` | `IRValue` 协议与 `materialize_ir_value` 统一漏斗 |
| `python/asc/language/core/tensor.py` | `BaseTensor.from_ir/to_ir`：Tensor 家族的句柄转换 |
| `python/asc/language/fwk/tpipe.py` | `on_teardown` 扩展点的真实用户（TPipeManager 借它自动复位） |
| `python/test/unit/language/basic/test_common_api.py` | 综合实践的参照测试（`test_block_reduce_sum_kernel`） |

## 4. 核心概念与源码讲解

### 4.1 global_builder：IR 构建的全局上下文

#### 4.1.1 概念说明

先想一个设计问题：`asc.add(z_local, x_local, y_local, count)` 的函数体要创建 `ascendc.AddL2` Operation，就必须拿到一个 `ir.Builder`。但 `asc.add` 的用户签名与 Ascend C 完全对齐，**不可能**塞一个 builder 参数进去。

pyasc 的解法是把 builder 挂在一个模块级单例上：

- 所有 language 层 API 通过 `global_builder.get_ir_builder()` 现取 builder；
- builder 由 JIT 主链路在进入 codegen 前统一放置（`set_ir_builder`），codegen 结束后统一拆除（`teardown`）；
- `require_jit` 装饰器用「builder 是否就位」当作「当前是否在 JIT 编译期」的判据，把所有 API 挡在 Host 侧误调用之外。

这是一个典型的**隐式上下文（ambient context）**模式：代价是 API 依赖全局状态、不能脱离 JIT 使用；收益是用户接口零噪音、与 Ascend C 一一对应。

#### 4.1.2 核心流程

builder 的完整生命周期被 `_run_codegen` 的 `try/finally` 夹住：

```text
kernel[核数, 流](...)                        # u3-l1: __getitem__ 返回 _run
  └─ _run → _cache_kernel（未命中缓存）
       └─ _run_codegen
            1. create_context()              # 新建 MLIR Context，加载全部方言
            2. global_builder.set_ir_builder(context)
                 ├─ builder = ir.Builder(context)
                 ├─ ir_module = builder.create_ModuleOp()
                 ├─ 插入点设到模块 body 开头
                 └─ on_teardown(reset)        # 登记"把 builder 置 None"的回调
            3. visitor.visit(kernel AST)      # 编译期重放：API 调用在这里发生
            4. return global_builder.get_ir_module()
            5. finally: global_builder.teardown()
                 ├─ 逆序执行回调（含 reset：builder = None）
                 └─ 清空回调列表
```

关键点：

- **入口即顶层**。`set_ir_builder` 把插入点直接放在模块 body 开头，所以 FunctionVisitor 构造 `FuncOp`、API 追加 Operation，全都落到同一个模块里（这就是 u4-l4 说的「子函数与 Kernel 同模块」的物理基础）。
- **teardown 是逆序回调**。后注册的清理先执行，模拟栈式展开；`set_ir_builder` 自己注册的 `reset` 把 builder 置 `None`，于是 `require_jit` 在 teardown 后必然拦截任何迟到的 API 调用。
- **`ir_module` 不在 reset 里清除**。teardown 后模块句柄仍可被取走（`_run_codegen` 在 `finally` 之前的 `return` 已取回），下一次 `set_ir_builder` 再整体覆盖。
- **on_teardown 是开放扩展点**。任何「随 codegen 生、随 codegen 死」的组件都可以登记清理回调——`TPipeManager` 就是真实用户（见 4.1.3 第 4 条）。

#### 4.1.3 源码精读

**1. GlobalBuilder 类本体与单例**——三个字段：builder、ir_module、teardown 回调列表。模块底部直接实例化为包内共享单例：

[python/asc/language/core/utils.py:L136-L170](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/core/utils.py#L136-L170)

```python
class GlobalBuilder:
    def __init__(self):
        self.builder: Optional[ir.Builder] = None
        self.ir_module: Optional[ir.ModuleOp] = None
        self.teardown_callbacks: List[Callable[[], None]] = []

global_builder = GlobalBuilder()
```

注意一个容易被误解的点：`global_builder` 是**模块级普通单例，不是 `threading.local`**。它没有做任何线程隔离；它的并发立场是「同一进程同一时刻只有一路 codegen 在跑」。这个立场由 MLIR Context 侧配合兜底——`create_context` 每次都无条件关闭 Context 多线程：

[python/asc/runtime/jit.py:L106-L111](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/jit.py#L106-L111)

```python
@staticmethod
def create_context() -> ir.Context:
    context = ir.Context()
    context.disable_multithreading()
    ir.load_dialects(context)
    return context
```

此外 `CodegenOptions.ir_multithreading`（默认 `True`，[function_visitor.py:L37-L39](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/function_visitor.py#L37-L39)）为 `False` 时 `_run_codegen` 会再次调用 `disable_multithreading()`——由于 `create_context` 已经无条件关过一次，当前 HEAD 下该分支是幂等操作，主要留作放开多线程构建时的开关（其精确演化意图待确认）。

**2. set_ir_builder：三步放置 + 登记拆除**：

[python/asc/language/core/utils.py:L143-L151](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/core/utils.py#L143-L151)

```python
def set_ir_builder(self, context: ir.Context) -> None:
    self.builder = ir.Builder(context)
    self.ir_module = self.builder.create_ModuleOp()
    self.builder.set_insertion_point_to_start(self.ir_module.get_body())

    def reset():
        self.builder = None

    self.on_teardown(reset)
```

一次调用完成：建 builder → 建空模块 → 插入点推到模块 body 起点。闭包 `reset` 被登记进回调表，teardown 时置空 builder。

**3. 生命周期由 `_run_codegen` 夹住**——jit.py 第 23 行 `from ..language.core.utils import global_builder` 引入单例，真正使用在 `_run_codegen`：

[python/asc/runtime/jit.py:L184-L194](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/jit.py#L184-L194)

```python
def _run_codegen(self, spec: Specialization, options: CodegenOptions) -> ir.ModuleOp:
    self.context = self.create_context()
    if not options.ir_multithreading:
        self.context.disable_multithreading()
    try:
        global_builder.set_ir_builder(self.context)
        visitor = self.codegen(self.src, spec, self.fn.__globals__, self.location, options, is_kernel=True)
        visitor.visit(self.node)
        return global_builder.get_ir_module()
    finally:
        global_builder.teardown()
```

即使 `visitor.visit` 抛出 CodegenError（u4-l5 的语法报错），`finally` 也保证 teardown 执行，builder 不会泄漏成「就位状态」，这是全局单例模式不出事故的关键一道保险。

**4. teardown 的逆序回调与真实扩展用户**：

[python/asc/language/core/utils.py:L159-L167](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/core/utils.py#L159-L167)

```python
def on_teardown(self, callback: Callable[[], None]) -> None:
    ...
    self.teardown_callbacks.append(callback)

def teardown(self) -> None:
    for callback in reversed(self.teardown_callbacks):
        callback()
    self.teardown_callbacks.clear()
```

u2-l6 讲过「TPipe 全局唯一、teardown 自动复位」，落地点就在这里——`TPipeManager.set` 在第一次创建 TPipe 时登记 `cls.reset`：

[python/asc/language/fwk/tpipe.py:L506-L515](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/fwk/tpipe.py#L506-L515)

```python
@classmethod
def set(cls, pipe: TPipe) -> None:
    if cls.instance is not None:
        raise RuntimeError("TPipe instance is already created, use get_tpipe_ptr() to obtain it")
    cls.instance = pipe
    global_builder.on_teardown(cls.reset)
```

于是「一次 codegen 一个 TPipe」与「一次 codegen 一个 builder」同生共死，靠的是同一个钩子。

**5. require_jit：用 builder 就位与否当 JIT 判据**：

[python/asc/language/core/utils.py:L196-L207](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/core/utils.py#L196-L207)

```python
def wrapper(*args: P.args, **kwargs: P.kwargs) -> T:
    if not isinstance(global_builder.get_ir_builder(), ir.Builder):
        caller_name = fn.__qualname__
        raise RuntimeError(f"'{caller_name}' cannot be called without initialization of global builder")
    return fn(*args, **kwargs)
```

teardown 之后 `builder` 是 `None`，`isinstance` 检查失败——所以「在 Host 侧直接调 `asc.add`」会在第一时间被拦下，而不是悄悄生成悬空 IR。

#### 4.1.4 代码实践

**实践目标**：亲眼看到 builder 的「未就位 → 就位 → 拆除」三个状态，理解 `require_jit` 拦截的时机。

**操作步骤**（示例代码，可在安装好 pyasc 的 Linux 机器上运行，无需 NPU）：

1. 写一个 3 行的小脚本 `probe_builder.py`：

   ```python
   import asc

   # 步骤 A：在 Host 侧（无 JIT）直接调用 language API
   try:
       asc.add(None, None, None, 8)
   except RuntimeError as e:
       print("[A] 被拦截:", e)

   # 步骤 B：查看 teardown 后的单例状态
   print("[B] builder =", asc.language.core.utils.global_builder.get_ir_builder())
   ```

2. 运行 `python3 probe_builder.py`。

**需要观察的现象**：

- 步骤 A 打印的报错应包含 `'add' cannot be called without initialization of global builder`，且由 [utils.py:L202-L204](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/core/utils.py#L202-L204) 抛出——证明 API 一进来就检查了 builder。
- 步骤 B 打印 `None`——此刻从未进入过 codegen，单例尚是空壳。
- 再补一步 C：用 `@asc.jit` 写一个只调 `asc.add` 的空 kernel 并以 `kernel[1]()` 触发（参数仿照 [test_common_api.py:L236-L243](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/test/unit/language/basic/test_common_api.py#L236-L243) 的 LocalTensor 构造），在 `kernel[1]()` 返回后再次打印 builder——仍应是 `None`，因为 `_run_codegen` 的 `finally` 已把它拆掉。

**预期结果**：三个观察点分别对应「未就位拦截」「从未就位」「用完即拆」。完整运行输出待本地验证（依赖已安装的 pyasc 环境）。

#### 4.1.5 小练习与答案

**练习 1**：如果 `_run_codegen` 不写 `finally`、只在正常路径末尾调用 `teardown()`，会发生什么？

**答案**：一旦 `visitor.visit` 抛错（例如 u4-l5 的 `UnsupportedSyntaxError`），teardown 被跳过，`global_builder.builder` 仍指向已失效构建现场的 builder。用户捕获异常后再次触发别的 kernel 编译时，`set_ir_builder` 会整体覆盖、侥幸自救；但如果用户在异常后于 Host 侧误调用任何 `@require_jit` API，检查会通过（builder 仍 `isinstance ir.Builder`），调用会在残缺上下文上创建悬空 IR，可能引发难定位的崩溃。`finally` 把事故面缩到最小。

**练习 2**：为什么 `set_ir_builder` 里 `reset` 只把 `builder` 置 `None`，而不把 `ir_module` 也置 `None`？

**答案**：`_run_codegen` 的 `return global_builder.get_ir_module()` 在 `finally` 之前求值，但模块对象随后仍要被 `_run_compiler` 长期使用（跑 Pass、翻译成 Ascend C）。真正需要「立刻失效」的只有 builder——它是 `require_jit` 的判据、也是误创建 IR 的通道；`ir_module` 留着无害（下一次 `set_ir_builder` 会覆盖），置空反而多了个空指针风险。

**练习 3**：`global_builder` 为什么不做成 `threading.local`，让每个线程一份？

**答案**：做成 thread-local 固然能隔离多线程并发 JIT，但当前设计按「单进程内 codegen 串行」的假设运行：`create_context` 无条件 `disable_multithreading()`，且 `ir_module`/`context` 存在 `JITFunction` 实例属性上（`self.context`），配合两级缓存（u3-l8）使得重复编译本身就不常发生。引入 thread-local 会增加复杂度却收益有限；若未来支持多线程并行 codegen，需要同时改造 Context 管理、插入点与缓存锁，不只是换一个存储容器。

---

### 4.2 create_asc_* 调用模式：language 层 API 的固定三段式

#### 4.2.1 概念说明

u5-l5 已经从生成侧讲清 `create_asc_*` 的来源；本讲从**调用侧**归纳：所有 language 层 API 文件都遵循同一个「三段式」骨架——

1. **第一段：`@overload` 签名声明**。每个可用的调用形态写一个仅含 `...` 的重载存根。它**不参与运行时分发**，纯粹写给读者和类型检查器看，等价于 Ascend C 的多原型文档。
2. **第二段：`@require_jit` + 收口实现**。真实实现用 `*args, **kwargs` 收口全部剩余参数，函数体开头两件事：`builder = global_builder.get_ir_builder()` 取 builder；`check_type` / 参数合法性检查。
3. **第三段：dispatcher 注册变体 + `create_asc_XxxL{0,1,2}Op` 落 IR**。为每个重载注册一个闭包，闭包内把 Python 值转成句柄（`tensor.to_ir()`、`_mat(x, KT.xxx).to_ir()`），再调用 builder 方法创建 Operation。

「变体」对应 u5-l3 讲过的 L0/L1/L2/L3 分级：同一个 Ascend C API 的三条原型（mask 标量、mask 数组、calCount）在 pybind 侧是三个方法，前端按实参类型择一调用。同族 API（`add`/`sub`/`max`/…共 20 来个）连第三段都完全同构，于是被抽成公共委托函数 `op_impl`——这是三段式的「工业化量产」形态。

#### 4.2.2 核心流程

以 `asc.add(z, x, y, count=tile_length)` 为例的完整调用链：

```text
FunctionVisitor.visit_Call（u4-l4：编译期直接执行被调函数）
  └─ asc.add(dst, src0, src1, count)          # @require_jit 先检查 builder
       └─ builder = global_builder.get_ir_builder()
       └─ op_impl("add", dst, src0, src1, (count,), {},
                  builder.create_asc_AddL0Op,   # mask 标量形态
                  builder.create_asc_AddL1Op,   # mask 列表形态
                  builder.create_asc_AddL2Op)   # count 形态
            ├─ builder = build_l0.__self__      # 从绑定方法反查 builder
            ├─ check_type("add", dst, src0, src1)   # dtype 白名单校验
            ├─ dispatcher.register(mask=RuntimeInt, repeat_times=..., ...)
            ├─ dispatcher.register(mask=list, ...)
            ├─ dispatcher.register(count=RuntimeInt, ...)
            └─ dispatcher(*args, **kwargs)      # 命中 count 变体：
                 └─ build_l2(dst.to_ir(), src0.to_ir(), src1.to_ir(),
                             _mat(count, KT.int32).to_ir())
                      └─ IR 中新增 ascendc.AddL2 操作
```

标量物化的类型约定（发射层据此拼回 Ascend C 实参类型，承接 u5-l3 的参数顺序约定）：

| Python 参数 | 物化目标类型 | 出处 |
| --- | --- | --- |
| `count` | `int32` | `op_impl` L2 变体 |
| `repeat_times` | `int8` | `op_impl` L0/L1 变体 |
| `mask`（标量） | `int64`（reduce 系列为 `int32`/`uint64`，各 API 略异） | `op_impl` |
| `mask`（列表元素） | `uint64` | 列表推导逐个物化 |
| `sync`/`en_xxx` 等开关 | `bit`（i1） | `matmul.py` 各处 `_mat(x, KT.bit)` |
| `is_set_mask` | 不物化，直接传 Python `bool`，进 IR 成为 UnitAttr | `op_impl` |
| 枚举（`RoundMode` 等） | `ir.Xxx.symbolize(enum)` 转 I32EnumAttr | `vec_vconv.py` `cast` |

#### 4.2.3 源码精读

**1. 标准样本 `add`：overload 存根 + 三行实现**：

[python/asc/language/basic/vec_binary.py:L21-L43](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/basic/vec_binary.py#L21-L43)

```python
@overload
def add(dst: LocalTensor, src0: LocalTensor, src1: LocalTensor, count: int, is_set_mask: bool = True) -> None:
    ...

@require_jit
@set_binary_docstring(cpp_name="Add", append_text="按元素求和。")
def add(dst: LocalTensor, src0: LocalTensor, src1: LocalTensor, *args, **kwargs) -> None:
    builder = global_builder.get_ir_builder()
    op_impl("add", dst, src0, src1, args, kwargs, builder.create_asc_AddL0Op, builder.create_asc_AddL1Op,
            builder.create_asc_AddL2Op)
```

注意三个细节：真实实现的三个 Tensor 形参是**具名**的（要参与 `check_type`），其余全收进 `args/kwargs`；`builder` 取了但没直接用——它的作用是「作为三个绑定方法的宿主」传给 `op_impl`；方法名 `create_asc_AddL0Op` 就是 u5-l1 四名合一里的 C++ 类名 `AddL0Op` 加 `create_asc_` 前缀。

**2. 公共委托 `op_impl`：同族 API 的量产线**：

[python/asc/language/basic/utils.py:L108-L135](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/basic/utils.py#L108-L135)

```python
def op_impl(callee: str, dst, src0, src1, args, kwargs, build_l0, build_l1, build_l2) -> None:
    builder = build_l0.__self__          # 绑定方法反查所属 builder
    if not isinstance(builder, ir.Builder):
        raise TypeError("Input builder must be ir.Builder")
    dispatcher = OverloadDispatcher(callee)
    check_type(callee, dst, src0, src1)

    @dispatcher.register(mask=RuntimeInt, repeat_times=RuntimeInt, repeat_params=BinaryRepeatParams,
                         is_set_mask=DefaultValued(bool, True))
    def _(mask, repeat_times, repeat_params, is_set_mask=True):
        build_l0(dst.to_ir(), src0.to_ir(), src1.to_ir(),
                 _mat(mask, KT.int64).to_ir(),
                 _mat(repeat_times, KT.int8).to_ir(), repeat_params.to_ir(), is_set_mask)
    ...
    @dispatcher.register(count=RuntimeInt, is_set_mask=DefaultValued(bool, True))
    def _(count, is_set_mask=True):
        build_l2(dst.to_ir(), src0.to_ir(), src1.to_ir(), _mat(count, KT.int32).to_ir())

    dispatcher(*args, **kwargs)
```

`build_l0.__self__` 是个值得学走的 Python 技巧：`builder.create_asc_AddL0Op` 是绑定方法，`__self__` 就是 builder 本身，于是 `op_impl` 的形参表里可以只出现三个「构造器」，不必再单传 builder。三个 `register` 与 u2-l5 讲的 OverloadDispatcher 注册顺序匹配一一对应：mask 标量→L0、mask 列表→L1、count→L2。

**3. 类型校验 `check_type`：dtype 白名单表**（节选）：

[python/asc/language/basic/utils.py:L28-L68](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/basic/utils.py#L28-L68)

```python
valids_map = {
    "add": valids,                                # fp16/fp32/int16/int32
    "add_deq_relu": {"src": [KT.int32], "dst": [KT.float16]},
    "div": valids_float,                          # 仅 fp16/fp32
    "mul_cast": {"src": [KT.float16], "dst": [KT.int8, KT.uint8]},
    ...
}
...
if src0.dtype != src1.dtype:
    raise TypeError("Src0 and src1 must be same type.")
```

这段就是「读陌生 API 时判断可用 dtype」的第一手依据：白名单写在源码里，比文档更权威。

**4. 手写三段式：`mul_cast`**——当某个 API 的参数映射与 `op_impl` 模板不完全一致时，就在本文件内手写 dispatcher（结构仍完全同构）：

[python/asc/language/basic/vec_binary.py:L347-L371](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/basic/vec_binary.py#L347-L371)

```python
@require_jit
def mul_cast(dst, src0, src1, *args, **kwargs) -> None:
    dispatcher = OverloadDispatcher(__name__)
    builder = global_builder.get_ir_builder()
    check_type("mul_cast", dst, src0, src1)

    @dispatcher.register(mask=RuntimeInt, repeat_times=RuntimeInt, repeat_params=BinaryRepeatParams)
    def _(mask, repeat_times, repeat_params):
        builder.create_asc_MulCastL0Op(dst.to_ir(), src0.to_ir(), src1.to_ir(),
                                       _mat(mask, KT.uint64).to_ir(),
                                       _mat(repeat_times, KT.int8).to_ir(), repeat_params.to_ir())
    ...
```

与 `op_impl` 唯一的差别是 mask 物化成 `uint64` 而非 `int64`——所以「同族也不盲抄模板」，读源码时要盯 `_mat` 的第二个参数。

**5. 高阶 API 侧的两个变体**（`language/adv/matmul.py`）：

- 无 dispatcher 的直连形态——`register_matmul` 只有单一原型，直接落 IR：

  [python/asc/language/adv/matmul.py:L34-L40](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/adv/matmul.py#L34-L40)

  ```python
  @require_jit
  def register_matmul(pipe: TPipe, workspace: GlobalAddress, matmul: Matmul,
                      tiling: Optional[TCubeTiling] = None) -> None:
      ir_tiling = tiling.to_ir() if tiling is not None else None
      builder = global_builder.get_ir_builder()
      builder.create_asc_RegistMatmulObjOp(pipe.to_ir(), workspace.to_ir(), matmul.to_ir(), ir_tiling)
  ```

  注意可选参数 `tiling` 为 `None` 时直接传 `None` 给 pybind（对应 IR 的 Optional 操作数），这是可选操作数的通用写法。

- 「先造类型、再造对象」形态——`Matmul.__init__` 用 `builder.get_matmul_type` 拼出 u5-l2 讲的十七参数 Matmul 类型，再用 `create_asc_ConstructOp` 在 IR 里构造一个该类型的值：

  [python/asc/language/adv/matmul.py:L97-L118](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/adv/matmul.py#L97-L118)

  ```python
  ir_type = builder.get_matmul_type(
      a.position, a.format, a.dtype.to_ir(), a.is_trans, a.layout,  # a
      ...共 17 组参数，逐一来自 MatmulType 与 MatmulConfig...
      matmul_config.batch_out_mode)
  self.handle = builder.create_asc_ConstructOp(ir_type, [])
  ```

  这展示了 create 系列不止 `create_asc_<API>Op`：还有 `get_<Xxx>Type`（造类型）、`create_asc_ConstructOp`（造聚合值）等配套方法，全部可在 u5-l5 讲的 `OpBuilder.cpp`（含生成 `.inc`）里查到。

#### 4.2.4 代码实践

**实践目标**：不看 `sub` 的实现，仅凭三段式套路预测 `asc.sub` 的行为，再用 dump 验证。

**操作步骤**：

1. 运行 02 示例并导出中间产物（无需 NPU，Model 模式即可）：

   ```bash
   export PYASC_DUMP_PATH=/tmp/pyasc_dump
   python3 examples/02_add_framework/add_framework.py -r Model
   ```

2. 打开 `/tmp/pyasc_dump/*/ascendc.cpp`，在 `compute` 对应的 kernel 函数体里找到一条 `Add` 调用，记下它的实参形态。
3. 把 [add_framework.py:L69](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/examples/02_add_framework/add_framework.py#L69) 的 `asc.add(z_local, x_local, y_local, tile_length)` 改成 `asc.sub(z_local, x_local, y_local, tile_length)`（可同步把 `vadd_launch` 里的断言改为 `z ≈ x - y`）。
4. 重新运行，diff 两份 `ascendc.cpp`。

**需要观察的现象**：`codegen.mlir` 中原 `ascendc.AddL2` 操作变为 `ascendc.SubL2`；`ascendc.cpp` 中对应调用由 `Add(...)` 变为 `Sub(...)`，参数排列完全不变。

**预期结果**：因为 `sub` 与 `add` 共用 `op_impl` 与同一套 L0/L1/L2 方法命名（仅类名前缀不同，[vec_binary.py:L391-L396](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/basic/vec_binary.py#L391-L396)），count 形态必然命中 `create_asc_SubL2Op`。具体 dump 文件内容待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：`@overload` 存根既然不参与运行时分发，删掉它程序还能跑吗？为什么要写？

**答案**：能跑——真实分发只看 `OverloadDispatcher` 注册表。但删掉后 IDE 补全、类型检查器和 `help()` 都只剩 `*args, **kwargs` 签名，用户无从知道合法形态；同时 `set_binary_docstring`/`set_common_docstring` 生成的文档也失去了挂载点对应的原型说明。它是以零运行时成本换来的接口文档。

**练习 2**：调用 `asc.add(dst, x, y, mask=[a, b], repeat_times=2, repeat_params=params)` 会命中哪个 builder 方法？`mask` 列表里的 `a`、`b` 被物化成什么类型？

**答案**：命中 `builder.create_asc_AddL1Op`（L1 = mask 数组形态）。列表内每个元素经 `[_mat(v, KT.uint64).to_ir() for v in mask]` 逐个物化为 `uint64`（见 [utils.py:L124-L129](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/basic/utils.py#L124-L129)），对应 Ascend C 的逐 bit mask 语义。

**练习 3**：`op_impl` 里为什么不把 builder 作为函数参数显式传入，而要用 `build_l0.__self__` 反查？

**答案**：两种写法都正确，反查写法让 `op_impl` 的形参表只出现「三个构造器」这一种语义的参数，调用点（如 `add` 的实现）变成纯数据式的一行枚举，20 多个同族 API 的实现可以完全同构、便于对照与维护；代价是隐式依赖「三个构造器必须来自同一个 builder」，因此入口处保留了 `isinstance(builder, ir.Builder)` 防御检查。

---

### 4.3 IRValue 包装：句柄进、对象出

#### 4.3.1 概念说明

`create_asc_*` 的参数与返回值都是 C++ 句柄（`IRHandle`），而 Python 侧流通的一切值——Tensor、Matmul、标量表达式——都是 `IRValue` 子类。于是每个 API 实现里都存在一次「**入口转换**」和（若有返回值）一次「**出口包装**」。u2-l3 已讲过 IRValue 的类型学；本讲把转换本身集中成一张「通道图」：

**入口三通道**（Python 对象 → 句柄）：

| 通道 | 写法 | 适用 |
| --- | --- | --- |
| A. 对象自述 | `t.to_ir()` | Tensor、TQue、Matmul、参数结构体等一切 IRValue 子类 |
| B. 标量漏斗 | `_mat(x, KT.xxx).to_ir()`（`_mat` = `materialize_ir_value`） | RuntimeInt/RuntimeBool/RuntimeNumeric 参数位，立即数与 IR 值通吃 |
| C. 编译期直转 | `ir.RoundMode.symbolize(e)`、直接传 `bool`/IntEnum | 枚举 Attribute、UnitAttr 开关 |

**出口三形态**（句柄 → Python 对象）：

| 形态 | 写法 | 例子 |
| --- | --- | --- |
| 1. 简单包装 | `GlobalTensor(res)` / `MatrixOffset(handle=handle)` | `get_tensor_c`、`get_offset_c` |
| 2. 双构造器 | `__init__(..., handle=None)` + `from_ir` classmethod | `Matmul`、`MatrixOffset` |
| 3. 手工搭 IR 后包装 | 直接操作 builder 建块/插点，再把块参数包成 `PlainValue` | `MatmulIterator.__enter__` |

#### 4.3.2 核心流程

入口通道 B 的判定流程（`materialize_ir_value`）：

```text
materialize_ir_value(value, required_type)
  ├─ PlainValue？ ──是──> required_type 为空则原样返回；否则 .cast(required_type)
  ├─ 其他 IRValue？──是──> 要求 required_type 为空，原样返回
  ├─ ConstExpr？ ──是──> 解包出 .value 后递归
  ├─ 非 int/float？──是──> TypeError（str/list 等在这里被拒）
  └─ 立即数 ──> 按 required_type 归一（bit→bool、int 族截断、float 族转换）
              └─> convert_value：查 builder 常量工厂表 → PlainValue(builder.get_i32(v) 等)
```

出口形态 3（`MatmulIterator`，`for count in mm.iterate(...)` 语法的背后）：

```text
__enter__
  ├─ builder.create_scf_WhileOp(...)          # 建 while 循环骨架
  ├─ save_insertion_point                     # 记住循环外的插入点
  ├─ 建 before 块 → 插入点移入 → create_asc_MatmulIterateOp + scf_ConditionOp
  ├─ 建 after 块  → 插入点移入
  └─ return PlainValue(after.get_argument(0), KT.int32)   # 循环计数变量
__exit__（with 块体即循环体被访问完毕后）
  ├─ self.count = self.count + 1              # IRValue 运算符级联生成加法 IR
  ├─ create_scf_YieldOp([count.to_ir()])      # 回填循环变量
  └─ restore_insertion_point                  # 跳回循环外
```

这解释了 u4-l3 控制流讲义中 `mm.iterate()` 计数循环的来源：它不是 FunctionVisitor 翻译 `for` 生成的，而是高阶 API 前端**亲手**用 builder 搭的 `scf.while`。

#### 4.3.3 源码精读

**1. IRValue 协议**——整个包装体系只有两个方法：

[python/asc/language/core/ir_value.py:L23-L33](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/core/ir_value.py#L23-L33)

```python
class IRValue(abc.ABC):
    @classmethod
    def from_ir(cls, handle: IRHandle) -> Self: ...
    def to_ir(self) -> IRHandle: ...
```

Tensor 家族的实现：[tensor.py:L86-L89](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/core/tensor.py#L86-L89)（GlobalTensor）与 [tensor.py:L279-L283](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/core/tensor.py#L279-L283)（LocalTensor）都是一行构造/一行返回；Matmul 的实现见 [matmul.py:L123-L128](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/adv/matmul.py#L123-L128)。

**2. 标量漏斗 `materialize_ir_value`**——API 文件顶部统一 `from ..core.ir_value import materialize_ir_value as _mat` 的那个 `_mat`：

[python/asc/language/core/ir_value.py:L344-L363](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/core/ir_value.py#L344-L363)

```python
def materialize_ir_value(value: RuntimeNumeric, required_type: Optional[DataType] = None) -> PlainValue:
    if isinstance(value, PlainValue):
        return value if required_type is None else value.cast(required_type)
    if isinstance(value, IRValue):
        if required_type is not None:
            raise ValueError("Required type cannot be specified for IRValue which is not PlainValue")
        return value
    if isinstance(value, ConstExpr):
        return materialize_ir_value(value.value, required_type)
    if not isinstance(value, (int, float)):
        raise TypeError(f"Unsupported value type for materialization: {value.__class__.__name__}")
    ...
    return convert_value(value, required_type)
```

立即数分支最终进入 `convert_value`，它内部**也是从 `global_builder.get_ir_builder()` 取 builder**（[ir_value.py:L366-L374](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/core/ir_value.py#L366-L374)），按类型查 `builder.get_i32/get_f16/...` 常量工厂表生成常量。这就是「`count=tile_length` 传 Python int 也能进 IR」的完整机制。

**3. 出口形态 1：简单包装**——`get_tensor_c` 的带返回值重载拿到句柄后一行包成 GlobalTensor：

[python/asc/language/adv/matmul.py:L183-L187](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/adv/matmul.py#L183-L187)

```python
res = builder.create_asc_MatmulGetTensorCReturnOp(ir.get_global_tensor_type(self.c_dtype.to_ir()),
                                                  self.to_ir(), en_atomic.to_ir(),
                                                  en_sequential_write.to_ir(),
                                                  _mat(sync, KT.bit).to_ir())
return GlobalTensor(res)
```

配套细节：多结果/带结果类型的 Op 需要先 `ir.get_global_tensor_type(...)` 或 `builder.get_asc_MatrixOffsetType()` 声明结果类型——这类辅助函数正是 u5-l5 说的「手写 pybind 方法」存在的原因。

**4. 出口形态 2：双构造器**——`Matmul.__init__` 开头的 handle 分支：

[python/asc/language/adv/matmul.py:L75-L80](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/adv/matmul.py#L75-L80)

```python
def __init__(self, a=None, b=None, c=None, ..., handle: Optional[IRHandle] = None):
    if handle is not None:
        self.handle = handle
        return
    builder = global_builder.get_ir_builder()
    ...用户路径：校验 + get_matmul_type + create_asc_ConstructOp...
```

用户路径走完整校验并创建 IR；`from_ir(handle)` 路径（[matmul.py:L123-L125](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/adv/matmul.py#L123-L125)）只塞句柄直接返回，专供「IR 里已存在该值、要在 Python 侧继续操作」的场景（如 Pass 或 from_ir 重建）。`MatrixOffset`（[matmul.py:L656-L684](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/adv/matmul.py#L656-L684)）是同构的又一例，还展示了 `create_asc_ConstructOp` 携带字段值列表与类型数组的写法。

**5. 出口形态 3：手工搭 IR**——`MatmulIterator` 的 `__enter__`/`__exit__`：

[python/asc/language/adv/matmul.py:L629-L653](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/adv/matmul.py#L629-L653)

```python
def __enter__(self) -> RuntimeInt:
    builder = global_builder.get_ir_builder()
    zero = builder.get_i32(0)
    op = builder.create_scf_WhileOp([zero.get_type()], [zero])
    self.insert_point = builder.save_insertion_point()
    before = builder.create_block(op.get_before())
    before.add_argument(zero.get_type())
    builder.set_insertion_point_to_start(before)
    ...
    self.count = PlainValue(after.get_argument(0), KT.int32)
    return self.count

def __exit__(self, *args) -> None:
    self.count = self.count.__add__(1)
    builder = global_builder.get_ir_builder()
    builder.create_scf_YieldOp([self.count.to_ir()])
    builder.restore_insertion_point(self.insert_point)
```

读它要抓三件事：`save/restore_insertion_point` 成对出现，保证 with 块结束后 builder 回到循环外；块参数 `after.get_argument(0)` 被直接包成 `PlainValue` 当作返回值——这正是 u2-l3 说的「PlainValue = 设备侧延迟求值标量」的典型出身；`__exit__` 里 `self.count + 1` 经运算符重载级联生成加法 IR，而非 Python 加法。

**6. 通道 C 的枚举直转**（补充样本）：

[python/asc/language/basic/vec_vconv.py:L64-L75](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/basic/vec_vconv.py#L64-L75)

```python
@require_jit
def cast(dst: LocalTensor, src: LocalTensor, round_mode: RoundMode, *args, **kwargs) -> None:
    builder = global_builder.get_ir_builder()
    dispatcher = OverloadDispatcher("cast")

    @dispatcher.register(mask=RuntimeInt, ...)
    def _(mask, repeat_times, repeat_params, is_set_mask=True):
        builder.create_asc_CastL0Op(dst.to_ir(), src.to_ir(), ir.RoundMode.symbolize(round_mode), ...)
```

`round_mode` 是 Python `IntEnum`，经 `ir.RoundMode.symbolize` 转成 u5-l2 讲的 I32EnumAttr；`tpipe.py` 的 `release_event_id` 里 `HardEvent` 枚举更是直接作为位置参数传给 `create_asc_TPipeReleaseEventIDOp`（[tpipe.py:L485-L489](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/fwk/tpipe.py#L485-L489)），转换由 pybind 侧完成——枚举永远不进 `_mat` 漏斗。

#### 4.3.4 代码实践

**实践目标**：以 `get_offset_c` 为标本，完整走一遍「句柄出 → 包装 → 继续使用」的链路。

**操作步骤**：

1. 阅读 [matmul.py:L556-L561](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/adv/matmul.py#L556-L561)：

   ```python
   @require_jit
   def get_offset_c(self) -> MatrixOffset:
       builder = global_builder.get_ir_builder()
       handle = builder.create_asc_MatmulGetOffsetCOp(builder.get_asc_MatrixOffsetType(), self.to_ir())
       return MatrixOffset(handle=handle)
   ```

2. 回答三个问题并到源码里求证：
   - `builder.get_asc_MatrixOffsetType()` 从名字推断对应 u5-l2 的哪个 TypeDef？（提示：`include/ascir/Dialect/Asc/IR/Core/Types.td` 中 `MatrixOffset` 的手写五件套）
   - 返回值走的是出口形态几？如果改成 `MatrixOffset(offset=..., row=..., ...)` 用户路径会发生什么不同的事？（对照 [matmul.py:L667-L684](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/adv/matmul.py#L667-L684)）
   - 拿到的 `MatrixOffset` 对象如果在 kernel 里继续参与 `offset.row + 1` 这样的表达式，走的是 u4-l2 的哪张运算符翻译表？
3. 若本地已构建 devtools（u7-l5），可任选一个 matmul 示例开 `PYASC_DUMP_PATH` 运行，在 `codegen.mlir` 中找到 `ascendc.MatmulGetOffsetC` 操作并确认其结果类型打印。

**需要观察的现象**：`MatrixOffset` 的两个构造路径（handle 直塞 vs 五字段用户构造）在 IR 上分别表现为「引用一个已存在的值」和「一条 `ascendc.Construct` 操作」。

**预期结果**：三个问题的答案分别是——手写 TypeDef（MatrixOffset 五件套）；形态 2 双构造器，用户路径会额外生成 ConstructOp 并校验五个字段；二元运算表（`__add__` → PlainValue 级联）。mlir 中的操作名与类型打印待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：`_mat(value, required_type)` 对 `PlainValue` 传入 `required_type` 与对其他 `IRValue` 传入 `required_type`，行为为何不同？

**答案**：`PlainValue` 是标量，可以在 IR 层做显式 cast（`.cast(required_type)`）所以允许指定目标类型；而 `GlobalAddress`、Tensor 等 IRValue 是「非标量值」，pyasc 不提供隐式类型转换，强行指定目标类型没有意义，直接抛 `ValueError`（[ir_value.py:L347-L350](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/core/ir_value.py#L347-L350)）。

**练习 2**：为什么 `MatmulIterator.__exit__` 里要 `restore_insertion_point`？删掉会怎样？

**答案**：`__enter__` 把插入点移进了 while 循环的 before/after 块；with 块结束后，后续 kernel 语句（如 `mm.end()`）必须落在循环**外**。删掉 restore，后续所有 Operation 会继续追加到循环的 after 块内部，生成语义错误的 IR（循环体内出现本应在循环外的收尾调用），且这种错误不会在 codegen 期报错，要到 Pass 或发射期才暴露。

**练习 3**：`sync: RuntimeBool = True` 这种参数传 Python `True` 和传一个 `PlainValue`（比如 `flag & other` 的结果）分别走 `materialize_ir_value` 的哪个分支？

**答案**：`True` 是 `int` 的子类但先被 `isinstance(value, PlainValue)` 排除，落入立即数分支，经 `convert_value` 用 `builder.get_i1(True)` 生成 `i1` 常量；`PlainValue` 走第一分支，`required_type=KT.bit` 时调用 `.cast(KT.bit)` 保证位宽一致（[ir_value.py:L344-L346](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/core/ir_value.py#L344-L346)）。两条路最终都以 `i1` 句柄进入 `create_asc_*`。

## 5. 综合实践

**任务：只靠三段式套路，征服一个陌生 API 文件。**

任选 `language/basic` 下一个你没 used 过的算子文件（推荐 `vec_reduce.py`，备选 `fixpipe.py`、`vec_vconv.py`），**不查任何文档**，按下述步骤写出一个能生成合法 IR 的最小 kernel。

### 5.1 读源码的五步清单（本讲的交付物之一）

1. **读 overload 存根**：列出全部合法调用形态，挑最简单的（通常是 count/标量 mask 形态）。
2. **看实现函数的显式形参**：确定哪些参数是 Tensor/结构体（要 `.to_ir()`）、哪些收进 `*args` 走 dispatcher。
3. **追 `create_asc_XxxL{0,1,2}Op` 的选择逻辑**：找到 `op_impl`/`reduce_op_impl`/内联 dispatcher，确认你的实参组合命中哪个变体；同时记下每个标量的物化类型（`_mat` 第二参数）。
4. **查 dtype 白名单**：搜同文件或 `basic/utils.py` 的 `check_type` 表，确定 dst/src 的合法类型组合。
5. **到后端反查**（可选但推荐）：按 u5-l1 四名合一，在 `include/ascir/Dialect/Asc/IR/Basic/` 找到对应 `.td` 的 `defm`，核对 IR 参数顺序。

### 5.2 以 `vec_reduce.py` 的 `block_reduce_sum` 为例的参考实现

**第一步（清单 1-3）**：读 [vec_reduce.py:L52-L70](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/basic/vec_reduce.py#L52-L70) 可得：`(dst, src, repeat, mask, dst_rep_stride, src_blk_stride, src_rep_stride)`，mask 标量→`create_asc_BlockReduceSumL0Op`、mask 列表→L1；`reduce_op_impl`（[vec_reduce.py:L20-L48](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/basic/vec_reduce.py#L20-L48)）把五个标量全部物化为 `int32`（mask 列表元素 `uint64`）。注意此 API **没有** `check_type`，dtype 约束需到 `.td`/Ascend C 文档确认，选最稳妥的 `float16`。

**第二步（写最小 kernel）**：仿照仓库真实单测 [test_common_api.py:L236-L253](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/test/unit/language/basic/test_common_api.py#L236-L253) 的手工 LocalTensor 风格（示例代码）：

```python
import asc

@asc.jit
def reduce_kernel():
    x_local = asc.LocalTensor(dtype=asc.float16, pos=asc.TPosition.VECIN, addr=0, tile_size=512)
    z_local = asc.LocalTensor(dtype=asc.float16, pos=asc.TPosition.VECOUT, addr=0, tile_size=512)
    # mask 传标量 -> 命中 L0 变体
    asc.block_reduce_sum(z_local, x_local,
                         repeat=1, mask=512, dst_rep_stride=8,
                         src_blk_stride=1, src_rep_stride=8)

reduce_kernel[1]()
```

**第三步（验证）**：`export PYASC_DUMP_PATH=/tmp/pyasc_dump && python3 reduce_kernel.py`，然后：

- 在 `codegen.mlir` 中找到 `ascendc.BlockReduceSum` 前缀的操作，确认变体后缀与参数个数；
- 在 `ascendc.cpp` 中找到对应 `BlockReduceSum<...>` 调用，核对模板实参里出现了你传的 `float16` 与位置 `VECIN/VECOUT`。

**预期结果**：dump 的 IR 中出现 L0 形态的 BlockReduceSum 操作；若把 `mask=512` 换成 `mask=[2**64 - 1, 2**64 - 1]` 重跑，应改命中 L1 变体（IR 操作名后缀变化），这正是清单第 3 步的验证点。运行输出待本地验证。

**第四步（交付清单）**：把 5.1 的五步清单套用在你实际选择的文件上，写成一张表：| 步骤 | 我读到的证据（文件:行） | 结论 |。

## 6. 本讲小结

- `global_builder` 是 `language/core/utils.py` 的**模块级单例**（非 thread-local），所有 language API 经 `get_ir_builder()` 现取 builder；其生命周期被 `jit.py _run_codegen` 的 `try/finally` 夹住：`set_ir_builder`（建 builder + 建模块 + 插入点到模块头）→ visit → `teardown()`（逆序回调、builder 置 None）。
- `on_teardown` 是开放扩展点：`set_ir_builder` 自登记 `reset`，`TPipeManager.set` 借它实现「TPipe 随 codegen 生死」的自动复位。
- language 层 API 的**固定三段式**：`@overload` 存根（纯文档）→ `@require_jit` + `check_type` 守门 → dispatcher 变体内 `to_ir()`/`_mat()` 转句柄后调 `builder.create_asc_XxxL{0,1,2}Op`；同族 API 由 `op_impl` 量产，builder 用 `build_l0.__self__` 反查。
- 标量参数统一走 `materialize_ir_value`（`_mat`）漏斗：PlainValue 直通/cast、ConstExpr 解包、立即数经 `convert_value` 的 builder 常量工厂落 IR；枚举与 bool 开关走 `symbolize`/直传通道。
- 出口包装三形态：简单包装（`GlobalTensor(res)`）、双构造器（`Matmul`/`MatrixOffset` 的 `handle` 分支 + `from_ir`）、手工搭 IR（`MatmulIterator` 用 `scf.while` + save/restore_insertion_point 自建循环并把块参数包成 `PlainValue`）。
- 掌握「五步读码清单」（overload → 显式形参 → create 选择逻辑与物化类型 → dtype 白名单 → .td 反查）后，可以不依赖文档读懂任何陌生 API 文件。

## 7. 下一步学习建议

本讲补完了第 5 单元「ASC-IR 后端基础」的最后一块拼图：现在你已经能从 Python 调用侧一路追到 `create_asc_*` 背后的 pybind 与 TableGen 生成机制。接下来两条路：

- **进入第 6 单元（u6-l1 Transforms Pass 全景）**：本讲反复出现的 `ir.ModuleOp` 在 codegen 结束后要依次流过 lowering/optimizing/postprocessing 三阶段 Pass。带着本讲的视角去读 Pass 会很顺：你在 IR 里亲手放下的 `ConstructOp`、`TQue` 类型，正是 `UnifyPipe`、`HoistUBAllocation` 这些 Pass 改写的对象。
- **提前横跳 u7-l1（Matmul 高阶 API）**：如果想立刻检验本讲成果，`language/adv/matmul.py` 是最好的练习场——用五步清单独立推导 `set_tensor_a` 两个重载、`iterate` 返回的 `MatmulIterator`（本讲 4.3 已拆解其 while 骨架）以及 `register_matmul` 的四参数形态，再对照 03/04 示例验证。
