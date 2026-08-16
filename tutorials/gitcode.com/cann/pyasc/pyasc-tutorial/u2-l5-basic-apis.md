# 基础 API：data_copy 搬运与向量计算算子

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清楚 `asc.data_copy` 在 GM（全局内存）与 UB（统一缓冲区）之间搬运数据的各种调用形态，以及每种形态背后生成的是哪个 IR 操作。
2. 掌握 `vec_*` 系列向量算子（`asc.add`、`asc.sub`、`asc.mul` 等）的统一实现模式：**overload 声明 + dispatcher 分发 + builder 创建 IR**。
3. 理解 `OverloadDispatcher` 如何在运行时按参数类型挑选重载，弥补 Python「没有真正的运行时重载」这一缺口。
4. 理解 `require_jit` 装饰器如何把基础 API 保护在 JIT 编译期，避免在普通 Python 环境误用。
5. 记住 Python 接口与 Ascend C 接口之间的**参数映射顺序规则**：运行时必选、模板必选、运行时可选、模板可选。

本讲是「language 层用户接口」单元的核心一篇。前一讲（u2-l4）我们讲了枚举与硬件位置，本讲把镜头对准 `python/asc/language/basic/` 目录——用户写算子时调用最频繁的一批接口都住在这里。

## 2. 前置知识

阅读本讲前，你需要具备以下概念（均来自前几讲）：

- **JIT 编译期 vs Python 运行期**：`@asc.jit` 函数体在「编译期」被逐行翻译成 IR，而不是按 Python 语义执行。函数体里调用 `asc.add(...)` 时，Python 确实在执行这个函数，但它的作用是**向 IR 追加一个操作节点**，不是真的在算数（u1-l5、u2-l2）。
- **IRHandle / IRValue / PlainValue**：Python 对象包装 MLIR 的 `ir.Value` 句柄；`PlainValue` 表示设备侧标量的延迟求值（u2-l3）。
- **RuntimeInt / RuntimeNumeric**：类型别名，让同一个参数位置既能接受 Python 立即数（`512`），也能接受 IR 值（`asc.get_block_idx() * block_length`）（u2-l3）。
- **LocalTensor / GlobalTensor**：UB / GM 上的视图对象，不持有数据，只有 dtype 加 IR 句柄（u2-l2）。
- **TPosition / HardEvent**：逻辑位置与流水线同步事件（u2-l4）。
- **global_builder**：模块级全局单例，JIT 编译开始时被设置，持有当前 `ir.Builder` 与 `ir.ModuleOp`（u1-l5 提过它的生命周期，本讲 4.1 会用到）。

如果你对「Ascend C 是什么」还有疑问，回顾 u1-l1：pyasc 的接口与 Ascend C 类库一一对应，本讲的每个 Python API 背后都站着一个同名的 Ascend C 函数。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [python/asc/language/basic/data_copy.py](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/basic/data_copy.py) | 数据搬运接口：`copy`、`data_copy`、`data_copy_pad`、`load_image_to_local`、`set_pad_value` |
| [python/asc/language/basic/vec_binary.py](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/basic/vec_binary.py) | 双目向量算子：`add`、`sub`、`mul`、`div`、`max`、`min` 等十余个 |
| [python/asc/language/basic/utils.py](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/basic/utils.py) | basic 层公共设施：`check_type` dtype 白名单、`op_impl` 双目算子统一实现、docstring 生成器 |
| [python/asc/language/core/utils.py](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/core/utils.py) | 前端基础设施：`OverloadDispatcher`、`GlobalBuilder`/`global_builder`、`require_jit` |
| [python/asc/language/core/types.py](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/core/types.py) | 参数结构体：`BinaryRepeatParams`、`DataCopyParams` 等 |
| [examples/01_add/add.py](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/examples/01_add/add.py) | Add 示例，`data_copy` 与 `asc.add` 的真实用法 |
| [include/ascir/Dialect/Asc/IR/Basic/OpVecBinary.td](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/IR/Basic/OpVecBinary.td) | 后端：双目向量算子的 TableGen 定义（一行 `defm` 声明一个算子的 L0~L3 全部变体） |
| [include/ascir/Dialect/Asc/IR/Base.td](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/IR/Base.td) | 后端：`BinaryL0Op`/`BinaryL1Op`/`BinaryL2Op`/`BinaryL3Op` 等 Op 模板族 |
| [include/ascir/Dialect/Asc/IR/Basic/OpDataCopy.td](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/IR/Basic/OpDataCopy.td) | 后端：`DataCopyL0Op`/`DataCopyL2Op` 等搬运操作的 TableGen 定义 |
| [python/test/unit/language/basic/test_vector_binary.py](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/test/unit/language/basic/test_vector_binary.py) | 双目算子的单元测试，三种调用形态的标准范例 |

对应关系（承接 u1-l3 的「目录镜像」规律）：`python/asc/language/basic/` 下的每个 API 文件，与 `include/ascir/Dialect/Asc/IR/Basic/` 下的同名 `.td` 文件一一对应——`vec_binary.py` 对应 `OpVecBinary.td`，`data_copy.py` 对应 `OpDataCopy.td`。

## 4. 核心概念与源码讲解

### 4.1 require_jit 与 global_builder：API 调用的守门人

#### 4.1.1 概念说明

`asc.add`、`asc.data_copy` 这些函数有一个共同特征：**它们只能在 `@asc.jit` 函数体里调用**。如果你打开一个普通的 Python 交互式环境，直接敲 `asc.add(...)`，会立刻报错。

为什么？因为这些函数的工作是「向 IR 追加操作」，而追加操作需要一个 MLIR builder。builder 只在 JIT 编译开始后才存在——由 `JITFunction._run` 在进入 codegen 阶段时创建（详见 u1-l5 主链路）。pyasc 用两个机制把这件事变成硬约束而不是「碰运气」：

- **`global_builder`**：一个模块级单例，持有当前编译的 `ir.Builder` 与 `ir.ModuleOp`。
- **`require_jit`**：一个装饰器，每次 API 被调用时检查 builder 是否就绪，未就绪直接抛异常。

#### 4.1.2 核心流程

```text
用户在普通 Python 环境调用 asc.add(...)
    │
    ▼
require_jit 包装器检查 global_builder.get_ir_builder()
    │
    ├── builder 为 None（不在 JIT 编译中）──► RuntimeError: cannot be called without
    │                                        initialization of global builder
    │
    └── builder 是 ir.Builder 实例（JIT 编译中）
            │
            ▼
        进入真正的 add 实现，向 IR 追加 asc.AddL2Op 等节点
```

时序上：`jit.py` 在编译某个 kernel 前调用 `global_builder.set_ir_builder(context)` 创建 builder；编译结束后 `teardown()` 把 builder 置回 `None`。所以「builder 是否就绪」天然就是「当前是否处于 JIT 编译中」的信号。

#### 4.1.3 源码精读

先看单例本身。[python/asc/language/core/utils.py:136-170](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/core/utils.py#L136-L170) 定义了 `GlobalBuilder` 类：`set_ir_builder` 创建 `ir.Builder` 和空的 `ir.ModuleOp`，并把插入点设在模块体开头；模块最底部的 `global_builder = GlobalBuilder()` 就是那个全局单例。所有 basic API 都通过 `global_builder.get_ir_builder()` 拿到当前 builder。

再看守门人。[python/asc/language/core/utils.py:196-207](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/core/utils.py#L196-L207) 是 `require_jit` 的全部实现——用 `functools.wraps` 包装目标函数，每次调用先做一次 `isinstance(global_builder.get_ir_builder(), ir.Builder)` 检查：

```python
def require_jit(fn: Callable[P, T]) -> Callable[P, T]:
    ...
    @functools.wraps(fn)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> T:
        if not isinstance(global_builder.get_ir_builder(), ir.Builder):
            caller_name = fn.__qualname__
            raise RuntimeError(f"'{caller_name}' cannot be called without initialization of global builder")
        return fn(*args, **kwargs)
    return wrapper
```

最后看它的使用现场。[python/asc/language/basic/vec_binary.py:38-43](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/basic/vec_binary.py#L38-L43) 中 `add` 函数头顶的 `@require_jit` 装饰器，以及函数体第一行 `global_builder.get_ir_builder()`，就是每个 basic API 的标准开场：

```python
@require_jit
@set_binary_docstring(cpp_name="Add", append_text="按元素求和。")
def add(dst: LocalTensor, src0: LocalTensor, src1: LocalTensor, *args, **kwargs) -> None:
    builder = global_builder.get_ir_builder()
    op_impl("add", dst, src0, src1, args, kwargs, builder.create_asc_AddL0Op, builder.create_asc_AddL1Op,
            builder.create_asc_AddL2Op)
```

注意 `add` 的返回值是 `None`——向量算子是「写 dst」语义，结果写进 `dst` 张量，不返回新对象。

#### 4.1.4 代码实践

1. **实践目标**：亲眼看到 `require_jit` 的报错，建立「basic API 只能活在 kernel 里」的直觉。
2. **操作步骤**：在装好 pyasc 的环境中执行 `python3 -c "import asc; asc.add(None, None, None, 8)"`（参数故意乱传，反正走不到那一步）。
3. **需要观察的现象**：终端抛出 `RuntimeError: 'add' cannot be called without initialization of global builder`。
4. **预期结果**：报错信息里的函数名 `add` 正是 `require_jit` 从 `fn.__qualname__` 取出的。对照 4.1.3 的源码确认这条报错来自哪一行。待本地验证（报错文案以本地安装版本为准）。

#### 4.1.5 小练习与答案

**练习 1**：`require_jit` 检查的是 `global_builder.get_ir_builder()` 的类型而不是「是否为 None」，这两种写法有区别吗？

**答案**：语义上等价（builder 要么是 `ir.Builder` 要么是 `None`），但 `isinstance` 检查更防御——如果未来 `GlobalBuilder` 内部用哨兵对象占位，类型检查仍能正确拦截。此外 `isinstance` 同时兼容 `ir.Builder` 的子类实例。

**练习 2**：为什么 `require_jit` 不直接在 import 时检查一次，而要每次调用都检查？

**答案**：因为「是否处于 JIT 编译中」是随时间变化的：同一个进程里，`global_builder` 在第一次 JIT 编译前是空的，编译中被设置，编译后被 `teardown` 清空。只有在每次 API 调用的瞬间检查，才能准确反映当下的状态。

### 4.2 OverloadDispatcher：给 Python 补上运行时重载

#### 4.2.1 概念说明

Ascend C 的同一个 API（如 `Add`）有多个重载原型：有的传 `count`，有的传 `mask + repeatTimes + repeatParams`。C++ 靠编译器按参数类型选择重载。

Python 的 `typing.overload` 只是给静态类型检查器看的声明，**运行时完全不生效**——模块里连续多个同名 `def`，后一个会直接覆盖前一个。你在 `data_copy.py` 顶部看到的一长串 `@overload def data_copy(...) -> None: ...` 函数体只有 `...`，它们只是「文档」。

pyasc 需要真正的运行时分发：用户传 `count=512` 走一条路，传 `mask=[...]` 走另一条路。这就是 `OverloadDispatcher` 存在的意义——它维护一个重载候选列表，调用时按参数的**实际运行时类型**逐个试配。

#### 4.2.2 核心流程

```text
dispatcher(*args, **kwargs)
    │
    ▼
按注册顺序遍历每个重载 overload：
    match_overload(overload.args, args, kwargs)
    │
    ├── 位置参数过多/类型不匹配/缺必选参数 ──► 返回 None，尝试下一个候选
    │
    └── 全部匹配成功 ──► 调用 overload.impl(**call_args)，返回结果
    
所有候选都失败 ──► RuntimeError，报错中列出全部候选签名与实际传入类型
```

`match_overload` 的匹配规则（三个要点）：

1. **位置参数**：按重载签名的参数名顺序逐一 `isinstance` 检查类型，数量超出签名范围即失败。
2. **关键字参数**：参数名必须落在「尚未被位置参数占用」的名字里，否则失败。
3. **缺省值**：签名中未提供且没有默认值的参数导致失败；有 `DefaultValued` 包装的自动填入默认值。

#### 4.2.3 源码精读

分发的入口在 [python/asc/language/core/utils.py:47-69](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/core/utils.py#L47-L69)：`__call__` 里那个 `for overload in self.overloads` 循环逐个尝试 `match_overload`，第一个匹配的候选立即执行。全部失败时，循环后面的代码把每个候选格式化成 `def name(..., 参数: 类型 = 默认值)` 的样式拼进报错——这就是你传错参数时看到的候选清单。

匹配逻辑本体在 [python/asc/language/core/utils.py:78-105](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/core/utils.py#L78-L105)：位置参数段（`zip(pos_args, args)` 配对检查）、关键字参数段、默认值回填段，三段依次执行，任一不满足返回 `None`。

注册重载有两种方式，见 [python/asc/language/core/utils.py:114-133](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/core/utils.py#L114-L133)：

- `register(**kwargs)`：显式写出每个参数的类型，如 `@dispatcher.register(mask=RuntimeInt, repeat_times=RuntimeInt, ...)`；
- `register_auto`：直接从内层函数签名的**类型注解**自动提取（无注解会抛 `ValueError`），有默认值的参数自动包装成 `DefaultValued`。

`data_copy` 用的是 `register_auto`。看 [python/asc/language/basic/data_copy.py:146-181](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/basic/data_copy.py#L146-L181)，函数体内以 `@dispatcher.register_auto` 装饰了 7 个内层函数，每个内层函数的签名就是一种合法形态，最后一句 `dispatcher(*args, **kwargs)` 触发匹配：

```python
@require_jit
@set_common_docstring(api_name="data_copy")
def data_copy(dst: BaseTensor, src: BaseTensor, *args, **kwargs) -> None:
    dispatcher = OverloadDispatcher(__name__)
    builder = global_builder.get_ir_builder()

    @dispatcher.register_auto
    def _(repeat_params: DataCopyParams):
        builder.create_asc_DataCopyL0Op(dst.to_ir(), src.to_ir(), repeat_params.to_ir())

    @dispatcher.register_auto
    def _(count: RuntimeInt):
        builder.create_asc_DataCopyL2Op(dst.to_ir(), src.to_ir(), _mat(count, KnownTypes.int_).to_ir())
    ...
    dispatcher(*args, **kwargs)
```

**注意注册顺序即优先顺序**：`DataCopyParams` 分支注册在 `count` 分支之前。由于二者类型互斥（一个要求 `DataCopyParams` 实例、一个要求 `RuntimeInt`），顺序在这里不影响结果，但读代码时要意识到「先注册者优先匹配」。

另一个细节是 `_mat`：它是 `materialize_ir_value` 的别名（u2-l3 讲过的统一物化漏斗）。用户传进来的 `count` 可能是 Python `int`，也可能是 `PlainValue`（如 `asc.get_block_idx() * n`），`_mat(count, KnownTypes.int_)` 把两者统一转成 i32 的 IR 值，`to_ir()` 后交给 builder。这就是 `RuntimeInt` 类型别名在签名里的作用——同一个参数位容纳两种来源。

#### 4.2.4 代码实践

1. **实践目标**：制造一次分发失败，读懂 OverloadDispatcher 的报错。
2. **操作步骤**：在 `@asc.jit` kernel 里写 `asc.data_copy(x_local, x_gm, count="512")`（故意传字符串），编译运行。
3. **需要观察的现象**：抛出 `RuntimeError`，报错包含「No viable candidates were found to dispatch data_copy()」、你实际传入的类型（`str`），以及全部 7 个候选签名清单。
4. **预期结果**：报错里的候选清单与 [data_copy.py:150-179](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/basic/data_copy.py#L150-L179) 中注册的 7 个内层函数一一对应。待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：`typing.overload` 声明和 `OverloadDispatcher` 各自服务谁？

**答案**：`typing.overload` 服务静态类型检查器与 IDE 补全（让用户在写代码时看到合法形态），`OverloadDispatcher` 服务运行时（编译 kernel 时真正选路）。前者是给人看的文档，后者是给机器用的逻辑。

**练习 2**：如果两个重载的签名存在包含关系（如一个接受 `RuntimeInt`，另一个接受 `int`），注册顺序会影响结果吗？

**答案**：会。`match_overload` 按注册顺序取第一个匹配者。若 `RuntimeInt`（通常包含 `int`）的候选注册在前，纯 `int` 参数就永远轮不到更特化的 `int` 候选。pyasc 的现有代码通过让各分支参数类型互斥（`DataCopyParams` / `RuntimeInt` / `list`）回避了这一陷阱。

### 4.3 vec_binary 算子：从 asc.add 到 IR 的三段式

#### 4.3.1 概念说明

`vec_binary.py` 里有十几个双目向量算子：`add`、`sub`、`mul`、`div`、`max`、`min`、`bitwise_and`、`fused_mul_add`……它们的实现高度一致，可以抽象成一个「三段式」模板：

1. **overload 声明段**：文件顶部为每个算子写 3 个 `@overload` 声明，向用户展示三种调用形态；
2. **dispatcher 分发段**：真正的实现函数体只有一行 `op_impl(...)` 调用，把分发细节委托给公共函数；
3. **builder 创建 IR 段**：`op_impl` 内部按参数形态选择 `builder.create_asc_XxxL0Op / L1Op / L2Op` 三个方法之一，把 Python 调用变成 IR 节点。

理解三段式的钥匙是 **L0/L1/L2 分级**。这是从 Ascend C 继承的概念（[docs/developer_guide.md:668-670](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/docs/developer_guide.md#L668-L670) 有权威定义）：

| 级别 | 语义 | mask 形态 | 典型调用 |
| --- | --- | --- | --- |
| L2 | 对源操作数**连续 count 个**数据计算，连续写入目的操作数 | 无 mask | `asc.add(z, x, y, count=512)` |
| L1 | 支持每个操作数的 mask、repeatTimes 控制，**逐 bit 模式** | mask 是**数组** | `asc.add(z, x, y, mask=[m1, m2], repeat_times=1, repeat_params=p)` |
| L0 | 支持每个操作数的 mask、repeatTimes 控制，**连续模式** | mask 是**单个数值** | `asc.add(z, x, y, mask=512, repeat_times=1, repeat_params=p)` |
| L3 | 以 `LocalTensor` 成员（运算符重载）形式调用 | 无 | `z = x + y`（经 IRValue 运算符重载） |

级别越低越接近硬件、控制粒度越细；L2 最常用（01_add 示例用的就是它），L0/L1 用于需要精细控制掩码和重复步长的场景。

#### 4.3.2 核心流程

以 `asc.add(z_local, x_local, y_local, tile_length)` 为例：

```text
asc.add(dst, src0, src1, *args)          # 用户调用（count 位置参数）
    │
    ▼ require_jit 检查 builder（4.1）
    │
    ▼ op_impl("add", dst, src0, src1, args, kwargs,
             builder.create_asc_AddL0Op, L1Op, L2Op)   # 三个 builder 方法作为参数传入
    │
    ├── check_type("add", dst, src0, src1)             # dtype 白名单校验
    │
    ├── dispatcher 注册 3 个候选：
    │     mask=RuntimeInt + repeat_times + repeat_params ──► build_l0
    │     mask=list + repeat_times + repeat_params     ──► build_l1
    │     count=RuntimeInt                             ──► build_l2
    │
    └── dispatcher(count=tile_length) 匹配 L2 分支
            │
            ▼ builder.create_asc_AddL2Op(dst.to_ir(), src0.to_ir(), src1.to_ir(),
                                        _mat(count, int32).to_ir())
            │
            ▼ IR 模块中追加一个 asc.AddL2Op 操作
```

#### 4.3.3 源码精读

**第一段：overload 声明**。[python/asc/language/basic/vec_binary.py:21-35](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/basic/vec_binary.py#L21-L35) 为 `add` 声明了三种形态：`count` 模式、`mask: int`（L0 连续模式）、`mask: List[int]`（L1 逐 bit 模式）。注意 `is_set_mask: bool = True` 是所有形态共有的可选参数——它是参数映射规则的活例子，稍后展开。

**第二段：实现委托**。[python/asc/language/basic/vec_binary.py:38-43](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/basic/vec_binary.py#L38-L43)（4.1.3 已展示）。每个算子的实现体完全同构，只是换了个名字和三个 builder 方法：`sub` 用 [vec_binary.py:391-396](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/basic/vec_binary.py#L391-L396) 的 `create_asc_SubL0Op/L1Op/L2Op`，`mul` 用 [vec_binary.py:297-302](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/basic/vec_binary.py#L297-L302) 的 `create_asc_MulL0Op/L1Op/L2Op`。`set_binary_docstring` 装饰器（定义于 [basic/utils.py:4343](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/basic/utils.py#L4343)）只为生成中文 docstring，不影响逻辑。

**第三段：op_impl**。[python/asc/language/basic/utils.py:108-135](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/basic/utils.py#L108-L135) 是全部双目算子共享的实现。先做 dtype 白名单校验，再用 `@dispatcher.register` 显式注册三个候选：

```python
@dispatcher.register(mask=RuntimeInt, repeat_times=RuntimeInt, repeat_params=BinaryRepeatParams,
                     is_set_mask=DefaultValued(bool, True))
def _(mask, repeat_times, repeat_params, is_set_mask: bool = True):
    build_l0(dst.to_ir(), src0.to_ir(), src1.to_ir(),
             _mat(mask, KT.int64).to_ir(),
             _mat(repeat_times, KT.int8).to_ir(), repeat_params.to_ir(), is_set_mask)

@dispatcher.register(mask=list, ...)
def _(mask: list, ...):                              # L1：mask 逐 bit
    mask = [_mat(v, KT.uint64).to_ir() for v in mask]
    build_l1(...)

@dispatcher.register(count=RuntimeInt, is_set_mask=DefaultValued(bool, True))
def _(count: RuntimeInt, is_set_mask: bool = True):  # L2：连续 count
    build_l2(dst.to_ir(), src0.to_ir(), src1.to_ir(), _mat(count, KT.int32).to_ir())
```

注意每个级别物化 IR 时用的整数类型不同：L0 的 mask 物化为 i64、L1 的 mask 每个元素物化为 ui64、repeat_times 物化为 i8、L2 的 count 物化为 i32。这些宽度与 Ascend C 函数原型的形参类型严格对齐。

**dtype 白名单**在 [python/asc/language/basic/utils.py:28-68](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/basic/utils.py#L28-L68)：`check_type` 维护一张「算子名 → src/dst 合法 dtype」表。例如 `add` 允许 float16/float32/int16/int32 且三者必须同型；`div` 只允许 float16/float32。这意味着 `asc.add` 两个 float16 张量相加输出 int32 会在**编译期**（而非运行期）被拦下——错误离你写的代码更近。

**后端对照**。前端的一行调用在后端只占一行 TableGen：[include/ascir/Dialect/Asc/IR/Basic/OpVecBinary.td:23](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/IR/Basic/OpVecBinary.td#L23) 的 `defm Add : BinaryTemplateL0123Op<"add", "Add", "operator+">;` 一次性声明了 Add 的 L0/L1/L2/L3 四个 IR 操作类。multiclass 展开逻辑在 [Base.td:227-231](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/IR/Base.td#L227-L231)。三级模板的参数差异清晰可见于 [Base.td:150-171](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/IR/Base.td#L150-L171)：

- `BinaryTemplateL0Op`：`dst, src0, src1, mask, repeatTimes, repeatParams, isSetMask(UnitAttr)`——mask 是单个操作数；
- `BinaryTemplateL1Op`：同上，但 mask 是 `Variadic<UI64>`（可变个数数组）；
- `BinaryTemplateL2Op`：只有 `dst, src0, src1, calCount, isSetMask`。

L3 见 [Base.td:196-201](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/IR/Base.td#L196-L201)，summary 是 `Call LocalTensor::operator+ method`——也就是 `z = x + y` 这种运算符写法（经 u2-l3 讲过的 IRValue 运算符重载）最终生成的形态。**函数形式 `asc.add(...)` 与运算符形式 `x + y` 殊途同归，都落到 asc dialect 的 Add 操作族**。

最后是**参数映射顺序规则**。Python 没有模板，而 Ascend C 的 API 大量使用模板参数。pyasc 的映射约定（权威定义见 [docs/developer_guide.md:920-933](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/docs/developer_guide.md#L920-L933)）：

1. 模板参数一律改为运行时参数；
2. 枚举、bool 等常量类型直接传；非常量类型加 `asc.ConstExpr[origin_type]` 标记（u2-l1）；
3. 因为 Python 语法要求可选参数必须排在必选参数之后，参数按 **运行时必选 → 模板必选 → 运行时可选 → 模板可选** 的顺序重排。

`add` 的 `is_set_mask` 正是第 3 条的实例：它在 Ascend C 中是模板可选参数（`isSetMask`，IR 里表现为 `UnitAttr` 编译期属性），在 pyasc 中变成签名末尾的 `is_set_mask: bool = True`（op_impl 里的 `DefaultValued(bool, True)`）。

#### 4.3.4 代码实践

1. **实践目标**：用同一个 kernel 体验 L2/L0/L1 三种调用形态，并对照单元测试确认写法正确。
2. **操作步骤**：阅读 [python/test/unit/language/basic/test_vector_binary.py:17-32](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/test/unit/language/basic/test_vector_binary.py#L17-L32) 的 `test_add_kernel`，把它抄进一个自己的脚本（去掉 `mock_launcher_run` fixture 相关断言），用 `python3` 直接运行，或直接执行 `pytest python/test/unit/language/basic/test_vector_binary.py -k add`。
3. **需要观察的现象**：测试中三次 `asc.add` 调用分别使用 `count=512`、`mask=512`（配合 `asc.BinaryRepeatParams(1,1,1,8,8,8)`）、`mask=[2**64-1, 2**64-1]` 三种形态；kernel 用 `add_kernel[1]()` 启动。
4. **预期结果**：三种形态全部通过 dispatcher 找到各自分支，不产生任何「No viable candidates」报错；`BinaryRepeatParams` 的默认步长参数（`dst_rep_stride=8` 等）可在 [core/types.py:26-46](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/core/types.py#L26-L46) 中查到。待本地验证（需要已安装并构建好的 pyasc 环境）。

#### 4.3.5 小练习与答案

**练习 1**：`asc.add(z, x, y, tile_length)` 与 `z = x + y`（x、y 均为 LocalTensor）在生成的 IR 上有什么异同？

**答案**：前者生成 `asc.AddL2Op`（显式 count 控制），后者经 IRValue 的 `__add__` 运算符重载生成 L3 形态的操作（对应 `LocalTensor::operator+`）。两者都属于 asc dialect 的 Add 操作族，最终都发射为 Ascend C 的 `Add` 调用；区别在控制粒度——L2 可指定 count，L3 是便捷写法。

**练习 2**：为什么 `op_impl` 要把 `create_asc_AddL0Op/L1Op/L2Op` 三个方法作为参数传入，而不是在 `op_impl` 里写死？

**答案**：为了让十余个双目算子共享同一套分发逻辑。每个算子只提供「名字 + 三个 builder 方法」，注册、匹配、物化、调用的流程全部复用 `op_impl`，新增算子只需三行。

**练习 3**：`asc.div` 的 dtype 白名单与 `asc.add` 有何不同？传两个 int32 张量给 `asc.div` 会发生什么？

**答案**：`div` 只允许 float16/float32（[basic/utils.py:33](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/basic/utils.py#L33) 的 `valids_float`），`add` 允许 float16/float32/int16/int32。传 int32 给 `asc.div` 会在编译期被 `check_type` 拦下，抛出 `TypeError: Invalid dst data type ...`。

### 4.4 data_copy：GM 与 UB 之间的搬运接口

#### 4.4.1 概念说明

NPU 上计算之前，数据必须先从 GM（Global Memory，大而慢）搬进 UB（Unified Buffer，小而快）；算完再搬回去。`data_copy` 就是这条「搬运带」的 Python 接口，对应 Ascend C 的 `DataCopy` 函数族。

`data_copy` 支持三种方向，由 dst/src 的张量类型组合决定：

| 方向 | dst | src | 场景 |
| --- | --- | --- | --- |
| 搬入 | LocalTensor | GlobalTensor | GM → UB，计算前取数 |
| 搬出 | GlobalTensor | LocalTensor | UB → GM，计算后写回 |
| UB 内拷贝 | LocalTensor | LocalTensor | UB 内部倒腾 |

它还支持多种搬运「形态」：按元素个数（count）、按块参数结构体（`DataCopyParams`）、增强参数、切片（slice）、ND 转 NZ 布局、NZ 转 ND、CO1→CO2 等。每种形态对应后端一个独立的 IR 操作。01_add 示例用的最简单——count 形态。

#### 4.4.2 核心流程

`data_copy` 的执行流程与 `asc.add` 完全同构（4.3.2），区别只在候选列表的内容。7 个候选与 IR 操作的对应关系：

| 用户传入 | IR 操作 | 用途 |
| --- | --- | --- |
| `repeat_params: DataCopyParams` | `asc.DataCopyL0Op` | 按块参数（block_count/block_len/步长）搬运 |
| `count: RuntimeInt` | `asc.DataCopyL2Op` | 按元素个数搬运（最常用） |
| `DataCopyParams + DataCopyEnhancedParams` | `asc.DataCopyEnhancedOp` | 带 deq/relu/pad 等增强选项 |
| `slice_list1, slice_list2, dim_value` | `asc.DataCopySliceOp` | 按切片列表搬运 |
| `Nd2NzParams` | `asc.DataCopyNd2NzOp` | ND 转 NZ 数据布局 |
| `Nz2NdParamsFull` | `asc.DataCopyNz2NdOp` | NZ 转 ND 数据布局 |
| `DataCopyCO12DstParams` | `asc.DataCopyCO12DstOp` | CO1 到 CO2 的搬运 |

#### 4.4.3 源码精读

**用户视角的调用**。01_add 示例中，[examples/01_add/add.py:53-54](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/examples/01_add/add.py#L53-L54) 是搬入（GM → UB，count 形态）：

```python
asc.data_copy(x_local[buf_id * tile_length:], x_gm[i * tile_length:], tile_length)
asc.data_copy(y_local[buf_id * tile_length:], y_gm[i * tile_length:], tile_length)
```

[examples/01_add/add.py:66](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/examples/01_add/add.py#L66) 是搬出（UB → GM）：

```python
asc.data_copy(z_gm[i * tile_length:], z_local[buf_id * tile_length:], tile_length)
```

三次调用的第三参数都是 `tile_length`（`RuntimeInt`），所以都命中 count 分支，生成 `asc.DataCopyL2Op`。`t[k:]` 切片生成偏移视图（u2-l2 讲过的 subindex 节点）。

**实现与候选注册**。[python/asc/language/basic/data_copy.py:146-181](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/basic/data_copy.py#L146-L181) 已在 4.2.3 展示。这里补充两个对照点：

其一，文件里还有一个更「原始」的 `copy` 接口（[data_copy.py:34-58](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/basic/data_copy.py#L34-L58)），它**不用 dispatcher**，而是手工 `isinstance` 分支：mask 是 `list` 走 `CopyL1Op`，是 `int` 走 `CopyL0Op`。对比 `data_copy` 的 dispatcher 写法，能清楚看到抽象带来的收益——重载多时 dispatcher 明显更整洁。同文件里 [mul_cast（vec_binary.py:347-371）](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/basic/vec_binary.py#L347-L371) 则演示了「显式 register + register_auto 混用」的中间形态。

其二，**结构体参数如何进 IR**。count 形态之外的大多数形态接受一个参数对象（如 `DataCopyParams`）。这类对象本身就是 `IRValue` 的子类，构造时即向 IR 追加一个 `asc.ConstructOp` 常量结构体节点。看 [python/asc/language/core/types.py:135-155](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/core/types.py#L135-L155)：`DataCopyParams.__init__` 把四个字段（`block_count`/`block_len`/`src_stride`/`dst_stride`）物化为 ui32 IR 值后调用 `builder.create_asc_ConstructOp(builder.get_asc_DataCopyParamsType(), [...])`；使用时 `repeat_params.to_ir()` 直接交出句柄。`BinaryRepeatParams`（[types.py:26-46](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/core/types.py#L26-L46)）同理，六个步长字段对应 Ascend C 的同名结构体。

**后端对照**。[include/ascir/Dialect/Asc/IR/Basic/OpDataCopy.td:76-89](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/IR/Basic/OpDataCopy.td#L76-L89) 定义了 `AscendC_DataCopyL0Op`（mnemonic `data_copy_l0`）与 `AscendC_DataCopyL2Op`（`data_copy_l2`），与前端 `create_asc_DataCopyL0Op`/`create_asc_DataCopyL2Op` 一一对应——再次印证 u1-l3 的检索链：**Python 的 `create_asc_XxxOp` → 同象限 `.td` 里的定义 → 发射层实现**。

**参数映射顺序规则再现身**。`data_copy` 的 overload 声明（[data_copy.py:61-142](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/basic/data_copy.py#L61-L142)）中，`dst`/`src` 是运行时必选参数排最前，`count`/`repeat_params` 等紧随其后；`set_pad_value`（[data_copy.py:269-279](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/basic/data_copy.py#L269-L279)）的 `pos: Optional[TPosition] = TPosition.MAX` 则是「模板可选参数改运行时可选参数」并给默认值的例子。读任何 pyasc API 签名时，都可以用这条规则反推 Ascend C 原型的参数排列。

#### 4.4.4 代码实践

1. **实践目标**：把 count 形态换成 `DataCopyParams` 形态，观察 IR 变化。
2. **操作步骤**：复制 01_add 示例为 `add_params.py`；设置 `PYASC_DUMP_PATH` 环境变量（u1-l5 讲过的四级 dump）；把其中一处 `asc.data_copy(x_local[...], x_gm[...], tile_length)` 改为 `asc.data_copy(x_local[...], x_gm[...], asc.DataCopyParams(block_count=1, block_len=tile_length))`；在 Model 模式下重新运行。
3. **需要观察的现象**：打开 dump 出的 `codegen.mlir`，搜索 `asc.data_copy`：改动前该处是 `asc.data_copy_l2` 操作（带 count 操作数），改动后变成 `asc.data_copy_l0` 操作（带一个 `DataCopyParams` 类型的操作数，且 IR 中多出一个 `ConstructOp` 常量结构体节点）。
4. **预期结果**：同一行 Python 代码仅因第三参数类型不同，生成了不同的 IR 操作节点——这正是 4.2 分发机制的直接证据。运行结果数值不变（`block_count=1, block_len=tile_length` 等价于搬 `tile_length` 个元素）。待本地验证（block_len 的单位与对齐约束请以本地 CANN 文档为准，若断言失败可调整参数）。

#### 4.4.5 小练习与答案

**练习 1**：`data_copy` 的三个方向（dst/src 类型组合）是在 Python 端检查的吗？

**答案**：不是。实现签名的 dst/src 都是 `BaseTensor`，dispatcher 的候选只区分**第三参数之后**的形态；方向合法性（如 GM→GM 不允许）留给后端 Pass 与 Ascend C 编译器检查。Python 端只保证「能生成 IR」。

**练习 2**：`DataCopyParams(block_count=1, block_len=tile_length)` 中的 `tile_length` 若是运行时值（如 `asc.get_block_idx()` 相关表达式），还能用吗？

**答案**：能。`DataCopyParams` 的字段类型是 `RuntimeInt`，构造函数内部经 `_mat` 物化，既接受 Python 立即数也接受 IR 值（`PlainValue`）。这与 4.2.3 讲的 `RuntimeInt` 语义一致。

**练习 3**：`copy` 接口与 `data_copy` 接口都做搬运，为什么 `copy` 不用 dispatcher？

**答案**：`copy` 只有两个候选（mask 为 list 或 int），手工 `isinstance` 分支已足够简单；`data_copy` 有 7 个候选且参数形态多样，dispatcher 的自动匹配与友好报错收益更大。这是「抽象程度与问题规模匹配」的工程取舍。

## 5. 综合实践

**任务**：仿照 `asc.add` 的写法，用 `asc.mul` 与 `asc.sub` 组合出一个 `z = x * y - x` 的小算子，运行验证数值正确性，并在 dump 出的 `ascendc.cpp` 中找到生成的 Ascend C 调用。这个任务串起本讲全部四个最小模块：kernel 内调用（require_jit 生效）、count 形态的向量算子（dispatcher 分发到 L2 分支）、中间结果的 UB 排布（LocalTensor）、GM/UB 搬运（data_copy）。

**操作步骤**：

1. 复制 `examples/01_add/add.py` 为 `mulsub.py`（示例代码基于 [add.py:28-79](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/examples/01_add/add.py#L28-L79) 改造，需在 kernel 中增加一个中间 LocalTensor）：

```python
# 示例代码：基于 examples/01_add/add.py 修改
@asc.jit
def mulsub_kernel(x: asc.GlobalAddress, y: asc.GlobalAddress, z: asc.GlobalAddress, block_length: int):
    offset = asc.get_block_idx() * block_length
    x_gm = asc.GlobalTensor()
    y_gm = asc.GlobalTensor()
    z_gm = asc.GlobalTensor()
    x_gm.set_global_buffer(x + offset, block_length)
    y_gm.set_global_buffer(y + offset, block_length)
    z_gm.set_global_buffer(z + offset, block_length)

    tile_length = block_length // TILE_NUM // BUFFER_NUM
    data_type = x.dtype
    buffer_size = tile_length * BUFFER_NUM * data_type.sizeof()

    x_local = asc.LocalTensor(data_type, asc.TPosition.VECIN, 0, tile_length * BUFFER_NUM)
    y_local = asc.LocalTensor(data_type, asc.TPosition.VECIN, buffer_size, tile_length * BUFFER_NUM)
    z_local = asc.LocalTensor(data_type, asc.TPosition.VECOUT, buffer_size * 2, tile_length * BUFFER_NUM)
    # 新增：中间结果张量，排在 z_local 之后，避免与上述缓冲重叠
    tmp_local = asc.LocalTensor(data_type, asc.TPosition.VECCALC, buffer_size * 3, tile_length * BUFFER_NUM)

    for i in range(TILE_NUM * BUFFER_NUM):
        buf_id = i % BUFFER_NUM

        asc.data_copy(x_local[buf_id * tile_length:], x_gm[i * tile_length:], tile_length)
        asc.data_copy(y_local[buf_id * tile_length:], y_gm[i * tile_length:], tile_length)

        asc.set_flag(asc.HardEvent.MTE2_V, buf_id)
        asc.wait_flag(asc.HardEvent.MTE2_V, buf_id)

        # 两步计算：先乘后减，都走 L2（count）形态
        asc.mul(tmp_local[buf_id * tile_length:], x_local[buf_id * tile_length:],
                y_local[buf_id * tile_length:], tile_length)
        asc.sub(z_local[buf_id * tile_length:], tmp_local[buf_id * tile_length:],
                x_local[buf_id * tile_length:], tile_length)

        asc.set_flag(asc.HardEvent.V_MTE3, buf_id)
        asc.wait_flag(asc.HardEvent.V_MTE3, buf_id)

        asc.data_copy(z_gm[i * tile_length:], z_local[buf_id * tile_length:], tile_length)

        asc.set_flag(asc.HardEvent.MTE3_MTE2, buf_id)
        asc.wait_flag(asc.HardEvent.MTE3_MTE2, buf_id)


def mulsub_launch(x: torch.Tensor, y: torch.Tensor) -> torch.Tensor:
    z = torch.zeros_like(x)
    total_length = z.numel()
    block_length = total_length // USE_CORE_NUM
    mulsub_kernel[USE_CORE_NUM, rt.current_stream()](x, y, z, block_length)
    return z
```

2. 把 `vadd_custom` 中的断言改为 `assert torch.allclose(z, x * y - x)`。
3. 设置 `PYASC_DUMP_PATH=<某个目录>`，以 Model 模式运行：`python3 mulsub.py -r Model`。
4. 打开 dump 目录中的 `ascendc.cpp`，搜索 `Mul(` 与 `Sub(`；再打开 `codegen.mlir` 搜索 `asc.mul` 与 `asc.sub`。

**需要观察的现象**：

- `codegen.mlir` 中出现 `asc.mul_l2`（或同名带 `_l2` 后缀的操作）与 `asc.sub_l2`，各自带一个 i32 的 count 操作数——证明 dispatcher 把 `tile_length` 位置参数匹配到了 L2 分支；
- `ascendc.cpp` 中对应出现 Ascend C 的 `Mul(...)` 与 `Sub(...)` 调用，形如 `AscendC::Mul(z, x, y, count)` 的调用形态（具体形态以发射层为准）；
- 程序输出 `Sample run success`，`torch.allclose` 断言通过。

**预期结果**：z 的数值等于 x * y - x（逐元素）。若把 `tmp_local` 的 addr 改成与 `z_local` 重叠（例如也用 `buffer_size * 2`），数值将出错——这能直观体会 u2-l2 强调的「UB 手工排布、须自行保证不重叠」。本实践完整运行结果**待本地验证**（需要已构建的 pyasc 与 Model 模式环境；两步计算之间的同步沿用 01_add 的结构，向量指令序列的同步语义深入讨论见 u2-l4 与 u6-l3）。

## 6. 本讲小结

- **require_jit + global_builder** 是所有 basic API 的守门人：builder 在 JIT 编译期才存在，任何在普通 Python 环境对 `asc.add` 等接口的调用都会被立即拦截。
- **OverloadDispatcher** 弥补了 Python 没有运行时重载的缺口：按注册顺序用 `isinstance` 试配参数，全部落败时给出带完整候选清单的报错；`register_auto` 直接从内层函数的类型注解生成候选。
- **vec_* 算子的三段式**：overload 声明（给人与 IDE 看）→ `op_impl` 委托（dtype 白名单 + 注册三个候选）→ `builder.create_asc_XxxL0/L1/L2Op` 落成 IR。新增一个双目算子只需三行实现加一行 TableGen `defm`。
- **L0/L1/L2/L3 分级**源自 Ascend C：L2 连续 count 最常用，L0 mask 单值连续模式，L1 mask 数组逐 bit 模式，L3 是 tensor 运算符重载形式；不同级别物化 IR 时的整数宽度（i64/ui64/i8/i32）与 Ascend C 原型严格对齐。
- **data_copy** 以 7 个 dispatcher 候选覆盖 count、块参数、增强、切片、ND↔NZ 等搬运形态，方向由 dst/src 的张量类型组合决定；参数结构体（`DataCopyParams` 等）构造时即生成 `ConstructOp` IR 节点。
- **参数映射顺序规则**：Python 无模板，Ascend C 模板参数一律改为运行时参数，并按「运行时必选、模板必选、运行时可选、模板可选」重排——`is_set_mask: bool = True` 与 `pos: Optional[TPosition] = TPosition.MAX` 都是活例子。

## 7. 下一步学习建议

本讲只讲了 `basic` 目录的两个代表性文件。接下来的学习路径：

1. **下一讲 u2-l6（TPipe/TQue/TBuf 框架）**：本讲的 01_add 风格需要手动排布 UB、手动配同步；下一讲将介绍 `fwk/tpipe.py` 的框架化内存管理与自动同步，看 `alloc_tensor/enque/deque` 如何替代手工 `LocalTensor` + `set_flag/wait_flag`。
2. **横向浏览 basic 目录**：用本讲建立的三段式套路，自己读一个没讲过的文件（如 `vec_unary.py`、`vec_reduce.py` 或 `fixpipe.py`），验证「类型注解 → dispatcher → create_asc_XxxOp」的模式是否处处成立。
3. **为单元 5 做准备**：本讲多次出现 `create_asc_AddL2Op` 这类 builder 方法，它们由 TableGen 自动生成（`defm Add : BinaryTemplateL0123Op<...>` 一行展开四个 Op 类）。单元 5 将深入 `.td` 定义、TableGen 代码生成与 pybind 桥接，搞清楚「一行 defm 如何变成 Python 可调用的 builder 方法」。
4. **后续 u6-l5（Ascend C 代码发射）**：届时回头看本讲综合实践中 dump 的 `ascendc.cpp`，就能找到生成每条 `Mul`/`Sub` 调用的发射函数所在文件。
