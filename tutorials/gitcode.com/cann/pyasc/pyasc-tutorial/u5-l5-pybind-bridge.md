# pybind 桥接层：python/src 的 C++ 入口

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清 `libpyasc` 扩展模块的导出结构：一个顶层模块挂 `ir`、`passes`、`translation` 三个子模块，分别对应 `IR.cpp`、`Passes.cpp`、`Translation.cpp`，而 `OpBuilder.cpp` 的内容挂在 `ir` 子模块内部。
2. 理解 `PyOpBuilder` 这个 C++ 包装类如何把 MLIR 的 `OpBuilder` 连同「当前插入点 + 当前 Location」一起暴露给 Python。
3. 掌握 `create_asc_*` 系列方法的**双轨来源**：少数手写在 `OpBuilder.cpp`，绝大多数由 `ascir-tblgen -gen-pybind-defs` 从 `.td` 定义生成、以 `.inc` 文件的形式被 `#include` 进来。
4. 能独立完成一次「Python 调用 → pybind → MLIR Op 创建」的全链路追踪（本讲以 `asc.TQueBind` 构造函数为例）。

本讲是第 5 单元的第 5 讲。前一讲（u5-l4）讲了 TableGen 后端如何**生成**绑定代码；本讲站在生成代码的**消费侧**，看这些生成代码被拼进哪个 C++ 文件、以什么类结构暴露给 Python 前端。

## 2. 前置知识

### 2.1 pybind11 是什么

pybind11 是一个只用 C++ 头文件的库，用来把 C++ 的类和函数暴露成 Python 的模块、类和方法。它最核心的三个原语是：

| pybind11 写法 | 作用 |
|---|---|
| `PYBIND11_MODULE(名字, m)` | 定义一个 Python 扩展模块，`名字` 必须与最终 `.so` 文件名一致 |
| `py::class_<T>(m, "T")` | 把 C++ 类 `T` 注册为 Python 类 `"T"` |
| `.def("方法名", 函数/lambda)` | 给这个类添加一个 Python 可调用的方法 |

所以阅读本讲的诀窍是：**看到 `py::class_` 就是「Python 多了一个类」，看到 `.def(` 就是「这个类多了一个方法」，看到 `m.def(` 就是「这个模块多了一个函数」**。

### 2.2 两个会影响理解的 pybind11 细节

- `py::module_local()`：本仓库所有 `py::class_` 都带这个标志，它让类型注册只对当前扩展模块可见，避免与其他扩展模块（例如未来的其他 MLIR 绑定）冲突。
- `py::dynamic_attr()`：允许 Python 侧给对象**动态挂属性**（`obj.foo = 1` 不报错）。`Builder` 类带了这个标志，前端因此能在 builder 对象上暂存自定义状态。

### 2.3 需要回忆的前置概念

- **MLIR 对象模型**（u5-l1）：`MLIRContext`（上下文）→ `ModuleOp`（顶层模块）→ `Operation`（一个操作节点）→ `Value`（SSA 值）→ `Type`/`Attribute`。`OpState` 是所有「具体 Op 的 C++ 基类」的公共基类。
- **OpBuilder**（u5-l6 将从使用侧总结，本讲看其 C++ 定义）：MLIR 提供的「操作工厂」，负责在指定插入点创建 Operation。
- **TableGen 生成管线**（u5-l4）：`.td` 里的 `def AscendC_QueBindOp` 经 `-gen-pybind-defs` 展开成 `.def("create_asc_QueBindOp", ...)` 代码，产出到 `AscOpBindings.h.inc`。
- **`create_asc_XxxOp` 命名法**（u1-l3 的检索链）：`create_` + 方言缩写 `asc` + `_` + **Op 的 C++ 类名**。

## 3. 本讲源码地图

| 文件 | 角色 | 暴露给 Python 的核心内容 |
|---|---|---|
| [python/src/Module.cpp](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/src/Module.cpp) | 模块注册入口，全文件只有 8 行有效代码 | 三个子模块 `ir`/`passes`/`translation` |
| [python/src/InitFuncDef.h](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/src/InitFuncDef.h) | 四个初始化函数的统一声明 | （仅声明，无实现） |
| [python/src/IR.cpp](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/src/IR.cpp) | `ir` 子模块的主体 | `Context`、`Type`、`Value`、`Operation`、`OpState`、`ModuleOp`、`FuncOp`、枚举、类型工厂函数 |
| [python/src/OpBuilder.cpp](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/src/OpBuilder.cpp) | `ir.Builder` 类的实现 | `PyOpBuilder` 的全部方法，含手写与生成两路 `create_*` |
| [python/src/Passes.cpp](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/src/Passes.cpp) | `passes` 子模块 | `PassManager` 类、`common.*` 与 `ascendc.*` 两箱 `add_*` 函数 |
| [python/src/Translation.cpp](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/src/Translation.cpp) | `translation` 子模块 | 唯一函数 `ir_to_ascendc` |
| [python/src/CMakeLists.txt](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/src/CMakeLists.txt) | 构建脚本 | 声明 5 个源文件与对两个 TableGen 生成目标的依赖 |
| [python/asc/_C/__init__.py](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/_C/__init__.py) | Python 侧的入口 | `from .libpyasc import ir, passes, translation` |

配套引用（消费侧）：[python/asc/language/fwk/tpipe.py](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/fwk/tpipe.py)、[python/asc/language/core/utils.py](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/core/utils.py)、[python/asc/runtime/jit.py](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/jit.py)、[python/asc/runtime/compiler.py](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/compiler.py)、[lib/TableGen/GenPybindDefs.cpp](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/TableGen/GenPybindDefs.cpp)、[include/ascir/Dialect/Asc/IR/Fwk/TQue.td](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/IR/Fwk/TQue.td)。

## 4. 核心概念与源码讲解

本讲的四个最小模块：**Module.cpp 注册入口**、**IR.cpp 上下文与 Op 对象绑定**、**OpBuilder.cpp 的双轨 create**、**Passes/Translation 绑定**。

### 4.1 模块一：Module.cpp 注册入口——整个桥的「总开关」

#### 4.1.1 概念说明

`libpyasc` 是一个 pybind11 扩展模块（在 Linux 上是 `libpyasc.so`，位于 `asc/_C/` 目录，u1-l2 讲过它的构建）。任何一个 pybind11 模块都必须有且仅有一个 `PYBIND11_MODULE` 宏作为入口，宏的第一个参数必须和 `.so` 的文件名完全一致——这就是为什么构建产物叫 `libpyasc` 而不是别的名字。

pyasc 没有把全部绑定塞进一个巨型 cpp，而是拆成 5 个文件、由 4 个 `init*Module` 函数分头注册，`Module.cpp` 只负责「开三个子模块、各交给一个初始化函数」。这种拆分让 IR、Pass、Translation 三块代码互不干扰，各自可以独立演进。

#### 4.1.2 核心流程

```text
import asc._C
   │  (Python 加载 asc/_C/libpyasc.so)
   ▼
PYBIND11_MODULE(libpyasc, m)          ← Module.cpp L19
   ├── m.def_submodule("ir")          → initIRModule(...)          ← IR.cpp，同时会调入 OpBuilder.cpp 的内容
   ├── m.def_submodule("passes")      → initPassesModule(...)      ← Passes.cpp
   └── m.def_submodule("translation") → initTranslationModule(...) ← Translation.cpp
```

于是在 Python 里得到的是**嵌套命名空间**：`ir.Context`、`passes.ascendc.add_insert_sync`、`translation.ir_to_ascendc`。注意 `Builder` 并不是第四个子模块——它藏在 `ir` 里面，名字是 `ir.Builder`。

#### 4.1.3 源码精读

整个注册入口只有一段：

[python/src/Module.cpp:L18-L26](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/src/Module.cpp#L18-L26) —— 匿名命名空间里定义 `libpyasc` 模块，先设一句 docstring，然后依次创建 `ir`、`passes`、`translation` 三个子模块并立即转交给对应的初始化函数。`def_submodule` 的返回值（一个 `py::module`）以右值传入，所以每个初始化函数拿到的就是「已经挂到父模块上的子模块对象」，往里面注册的东西天然可见。

四个初始化函数的原型统一声明在头文件里：

[python/src/InitFuncDef.h:L17-L24](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/src/InitFuncDef.h#L17-L24) —— 声明 `initIRModule`、`initPassesModule`、`initTranslationModule`、`initBuilderInIRModule` 四个函数。它们被放进 `namespace pybind11 { namespace asc { ... } }` 这个略显特别的命名空间——这只是本仓库的组织习惯，让每个 `.cpp` 都能以 `py::asc::xxx` 的短名提供实现，避免再引一个项目头文件。

构建侧把 5 个源文件编成一个模块，并显式依赖两个 TableGen 生成目标：

[python/src/CMakeLists.txt:L14-L20](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/src/CMakeLists.txt#L14-L20) —— `pybind11_add_module(libpyasc MODULE NO_EXTRAS ...)` 列出 5 个源文件：`IR.cpp`、`Module.cpp`、`OpBuilder.cpp`、`Passes.cpp`、`Translation.cpp`。

[python/src/CMakeLists.txt:L42-L45](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/src/CMakeLists.txt#L42-L45) —— `add_dependencies` 声明依赖 `AscPybindGen` 与 `AscTypesPybindGen` 两个生成目标（对应 u5-l4 讲过的 `AscOpBindings.h.inc` 与 `AscTypeBindings.h.inc`），保证 `.inc` 先于编译生成。

Python 侧的入口则极薄：

[python/asc/_C/__init__.py:L9](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/_C/__init__.py#L9) —— `from .libpyasc import ir, passes, translation`。整个 `asc` 包里几十处 `from .._C import ir` 最终都汇到这一行。

#### 4.1.4 代码实践

**实践目标**：用 `dir()` 亲自验证三子模块结构与 `Builder` 的位置。

**操作步骤**（任意装好 pyasc 的环境，不需要 NPU）：

```bash
python3 -c "
from asc._C import ir, passes, translation
print('ir 子模块的部分成员:', [n for n in dir(ir) if not n.startswith('_')][:20])
print('Builder 在 ir 里吗:', hasattr(ir, 'Builder'))
print('passes 的子模块:', [n for n in dir(passes) if not n.startswith('_')])
print('translation 的成员:', [n for n in dir(translation) if not n.startswith('_')])
print('ir 模块 docstring:', ir.__doc__)
"
```

**需要观察的现象**：

1. `ir` 的成员里既有 `Context`、`Type`、`Value`、`ModuleOp`、`FuncOp`，也有 `Builder`、`get_local_tensor_type`、`get_kernel_arg_attrs` 等；
2. `passes` 的子模块恰好是 `ascendc` 和 `common` 两个；
3. `translation` 的成员只有 `ir_to_ascendc` 一个（外加模块级属性）。

**预期结果**：输出与上述三条一致，印证「三子模块 + Builder 藏在 ir」的结构。若在你的版本上 `dir()` 结果略有出入，以实际输出为准。**待本地验证**（本讲未替你运行）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `PYBIND11_MODULE(libpyasc, m)` 的第一个参数必须是 `libpyasc`？如果改成 `pyasc_bridge` 会怎样？

**答案**：pybind11 用这个参数生成模块的初始化函数名（`PyInit_libpyasc`），Python 解释器加载 `libpyasc.so` 时按文件名查找 `PyInit_libpyasc`。改成 `pyasc_bridge` 后初始化函数名变成 `PyInit_pyasc_bridge`，与文件名不匹配，`import` 会直接报「动态模块未定义模块导出函数」。

**练习 2**：`OpBuilder.cpp` 里的绑定最终出现在哪个 Python 子模块下？为什么？

**答案**：出现在 `ir` 子模块下（类名 `ir.Builder`）。因为 `Module.cpp` 只创建了 `ir/passes/translation` 三个子模块，而 `IR.cpp` 的 `initIRModule` 在末尾调用了 `initBuilderInIRModule(m)`（见 4.2.3），把 Builder 注册进了传给它的同一个 `m`（即 `ir` 子模块）。

### 4.2 模块二：IR.cpp——上下文、类型与 Op 对象的绑定

#### 4.2.1 概念说明

`IR.cpp` 是「MLIR 对象模型到 Python 类」的翻译表。它的绑定对象不是 pyasc 特有的东西，而是 MLIR 的公共基础设施：上下文（Context）、类型（Type）、值（Value）、操作（Operation）、以及一组成 Op 包装（OpState 及其派生 ModuleOp/FuncOp/ForOp 等）。

为什么需要 OpState 这个中间层？MLIR 里「一个 Op」在 C++ 侧有两副面孔：通用的 `Operation*`（任何 Op 都是它）和每个 Op 特有的瘦包装类（如 `func::FuncOp`，提供 `getNumArguments()` 这类便捷方法）。`OpState` 是这些瘦包装的公共基类。绑定层让 `ModuleOp`、`FuncOp`、`ForOp` 都继承 `OpState`（`py::class_<ModuleOp, OpState>`），于是 `dump()`、`set_attr()`、`get_result()` 这些公共方法只需在 `OpState` 上绑一次。

#### 4.2.2 核心流程

前端一次 JIT 的 IR 生命周期，对应的绑定调用顺序：

```text
jit.py create_context()
   → ir.Context()                      ← IR.cpp 绑定的 Context 构造
   → context.disable_multithreading()
   → ir.load_dialects(context)         ← 注册 ascendc/emitasc/scf/func/... 8 个方言
jit.py _run_codegen()
   → global_builder.set_ir_builder(context)
       → ir.Builder(context)           ← OpBuilder.cpp 的 PyOpBuilder 构造
       → builder.create_ModuleOp()     → ir.ModuleOp
   → FunctionVisitor 遍历 AST，不断往 Builder 里 create_*
   → 返回 global_builder.get_ir_module()
```

#### 4.2.3 源码精读

**上下文与方言注册**是使用一切 IR 的第一步：

[python/src/IR.cpp:L186-L207](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/src/IR.cpp#L186-L207) —— `bindContextAndDialect` 做两件事：其一，绑定 `Context` 类并只暴露一个 `disable_multithreading()` 方法（L188-L190）；其二，提供模块级函数 `load_dialects(context)`（L192-L206），它新建一个 `DialectRegistry`，把 `arith`、`ascendc`、`emitasc`、`emitc`、`func`、`memref`、`scf`、`vector` 共 8 个方言插进去，注册 ascendc/emitasc 的外部模型与内联接口、func 的全部扩展，最后 `loadAllAvailableDialects()`。这一步决定了「这个 Context 能创建哪些 Op」——漏掉任何一个方言，前端相应 `create_*` 就会在运行时报未知操作。

对应消费侧在 [python/asc/runtime/jit.py:L107-L111](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/jit.py#L107-L111)：`create_context()` 三步走（构造 → 关多线程 → 加载方言），每次未命中缓存的重编译都会新建一个独立 Context。

**类型系统**分两层绑定。通用类型类：

[python/src/IR.cpp:L209-L250](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/src/IR.cpp#L209-L250) —— `bindType` 绑定 `Type` 类：`is_integer`/`is_index` 判断、`__eq__`/`__neq__` 让 Python 能用 `==` 比较两个 MLIR 类型、`get_py_name()` 把 `i32`/`f16` 翻译成前端的 `int32`/`float16` 风格名字（这是 u2-l1 `DataType.from_ir` 反查的基础）、`__str__` 打印成 MLIR 文本。

类型**工厂函数**则是模块级 `m.def(...)`：

[python/src/IR.cpp:L302-L323](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/src/IR.cpp#L302-L323) —— `bindTensorType` 提供 `get_global_tensor_type` / `get_local_tensor_type`，各有「带 shape」与「不带 shape」两个重载，直接调用后端 `ascendc::GlobalTensorType::get` / `LocalTensorType::get`。这正是 u2-l2 中 `LocalTensor` 类型创建的落点（例如 `tpipe.py` 里 `ir.get_local_tensor_type(dtype.to_ir())`）。

**枚举绑定**把 Ascend C 的枚举镜像给 Python：

[python/src/IR.cpp:L88-L184](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/src/IR.cpp#L88-L184) —— `bindEnums` 用 `py::enum_` 绑定 `AddressSpace`、`RoundMode`、`MaskMode`、`TPosition`、`CMPMODE` 等十余个枚举。以 `TPosition` 为例（L164-L166）：它**没有枚举任何 `.value`**，只提供了一个静态 `symbolize`，把 Python `IntEnum` 的整数值翻译回 C++ 枚举。这就是为什么前端写 `ir.TPosition.symbolize(pos)`（见 `tpipe.py` 的 deque/enque 重载）而不是直接传枚举对象——Python 侧的 `TPosition` 来自 `language/core/enums.py`（u2-l4），两者靠整数值对接。

**Value 与 Operation** 是 IR 图的节点：

[python/src/IR.cpp:L335-L370](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/src/IR.cpp#L335-L370) —— `bindValue` 绑定 SSA 值：`get_defining_op`（找不到返回 `None`）、`replace_all_uses_with`（重写全部使用点，Pass 层改图的基础）、`get_type`、`id()`（用底层指针当唯一标识，供 Python 侧做字典 key）。L375-L376 顺带绑定了 `OpResult`（Op 的结果）与 `BlockArgument`（块参数）两个 `Value` 子类。

[python/src/IR.cpp:L462-L512](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/src/IR.cpp#L462-L512) —— `bindOperation` 绑定通用 `Operation`：`get_name`（返回如 `ascendc.add` 的全名）、`get_num_operands/get_operand`、`get_num_results/get_result`、以及一组按名字读属性的工具（`has_unit_attr`、`get_str_attr`、`get_bool_attr`、`get_integer_attr`、`get_flat_symbol_ref_attr`）。前端读后端回传信息（u3-l4 的 `asc.compile_mix`）就是靠这些方法。

**OpState 家族**：

[python/src/IR.cpp:L514-L549](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/src/IR.cpp#L514-L549) —— `OpState` 绑定所有 Op 包装的公共能力：`set_attr`、`get_result`（带越界检查，抛 `pybind11::index_error`）、`get_region`、`dump`、`__str__`（用 `getOpPrintingFlags()`，即带 debug info 的打印，见 L58-L63）、`append_operand`、`verify`，以及只读属性 `op`（退回通用 `Operation*`）。有了这一层，后面每个具体 Op 类只需绑「自己特有」的方法。

[python/src/IR.cpp:L551-L576](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/src/IR.cpp#L551-L576) —— `ModuleOp` 继承 `OpState`，新增 `get_body`（返回模块体 `Block*`）、`has_function`（按符号名查函数、可校验函数类型）、`need_insert_sync`、`erase`。其中 `need_insert_sync`（L569-L574）在模块里 `walk` 一遍，只要发现任何 `ascendc::LocalTensorAutoOp` 就返回 True——这正是 u3-l4 讲过的「`insert_sync=None` 时自动判定是否要重建同步」在 C++ 侧的真身。

[python/src/IR.cpp:L578-L620](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/src/IR.cpp#L578-L620) —— `FuncOp` 新增 `get_arg`/`get_num_args`/`add_entry_block`/`set_type`/`set_arg_names`（用 `NameLoc` 给参数起可打印名字）、`make_aicore`（打 `asc.aicore` 属性）、`make_global`（设为 public 并打 `asc.global` 属性，即「这是 Kernel 函数」的标记，u4-l4 讲过）。

**一个跨层协作的典型例子**——kernel 参数 ABI 的读取：

[python/src/IR.cpp:L65-L86](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/src/IR.cpp#L65-L86) —— 文件级辅助函数 `getKernelArgAttrs`：先 `walk` 整个模块找到带 `asc.global` 属性的函数（即 Kernel），再逐参数读 `emitasc::KernelArgumentAttr`，缺省按 `Explicit` 处理。

[python/src/IR.cpp:L640-L657](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/src/IR.cpp#L640-L657) —— 把它暴露为 `ir.get_kernel_arg_attrs(mod)`，同时绑定 `KernelArgument` 枚举（`Explicit`/`FftsAddr`）。u3-l6 讲过的「FftsAddr 隐藏参数注入」就是 Pass 打上该属性、launcher 经此函数读出、打包参数时多塞一个 uint64 的完整闭环。

**组装顺序**：

[python/src/IR.cpp:L661-L685](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/src/IR.cpp#L661-L685) —— `initIRModule` 按固定顺序调用 16 个 `bind*` 函数，最后绑定 `InsertPoint` 类（仅供保存/恢复插入点时做不透明句柄用），并把 `m` 交给 `initBuilderInIRModule`——Builder 就是在这里挂进 `ir` 子模块的。

#### 4.2.4 代码实践

**实践目标**：不改任何源码，直接用 `ir` 子模块手动搭一个「Context → Module → 类型」的最小环境，体会 `load_dialects` 的必要性。

**操作步骤**：

```bash
python3 -c "
from asc._C import ir

# 1) 只建 Context、不加载方言
ctx = ir.Context()
b = ir.Builder(ctx)
mod = b.create_ModuleOp()
print('未加载方言时也能建 ModuleOp:', type(mod).__name__)

# 2) 加载方言后再取类型
ir.load_dialects(ctx)
b2 = ir.Builder(ctx)
i32 = b2.get_i32_type()
print('i32 的打印:', str(i32), '| py 名:', i32.get_py_name())
gm = ir.get_global_tensor_type(i32)
lm = ir.get_local_tensor_type(i32)
print('GM tensor 类型:', str(gm))
print('LM tensor 类型:', str(lm))
"
```

**需要观察的现象**：`get_py_name()` 返回 `int32`；两个 tensor 类型的 `__str__` 是 MLIR 文本形态（形如 `!ascendc.global_tensor<i32>` / `!ascendc.local_tensor<i32>`，具体拼写以实际输出为准）。

**预期结果**：能成功创建 ModuleOp 与 tensor 类型。如果把第 2 步的 `ir.load_dialects(ctx)` 换到一个全新 Context 上跳过，再尝试 `get_local_tensor_type`，预期因方言未注册而报错——这条对比实验**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：`ModuleOp.need_insert_sync` 为什么放在 IR.cpp 而不是 Passes.cpp？

**答案**：它不是 Pass，只是对模块做一次只读遍历（`walk` 找 `LocalTensorAutoOp`），属于「IR 上的查询」。Passes.cpp 只暴露「往 PassManager 里装 Pass」的函数；查询类能力归属 `ir` 子模块，与 `get_kernel_arg_attrs` 同类。

**练习 2**：前端 `dtype.to_ir()` 生成 `!ascendc.local_tensor` 的元素类型后，是怎么变成一个 Python 可打印对象的？

**答案**：`OpBuilder` 的 `get_i32_type()` 等方法在 C++ 侧构造 `mlir::IntegerType` 并按值返回，pybind11 把它包装成 `ir.Type` 的 Python 实例；`__str__`（IR.cpp L243-L249）在 C++ 侧调用 MLIR 的 `print` 写进字符串再返回。Python 全程只持有包装对象，不解析 MLIR 内部结构。

### 4.3 模块三：OpBuilder.cpp——PyOpBuilder 与 create_asc_* 的双轨制

#### 4.3.1 概念说明

`OpBuilder.cpp` 的主角是 `PyOpBuilder`：一个 C++ 类，成员只有两个——MLIR 的 `OpBuilder builder` 和 `Location loc`。绑定层没有直接暴露 `mlir::OpBuilder`（它没有虚函数、大量方法是模板，pybind11 无法逐个绑定），而是包了一层，把「创建 Op」这件事收敛成签名固定的 lambda。

为什么要额外携带 `loc`？MLIR 的 `builder.create<OpTy>(loc, ...)` 每次都要传源码位置。pyasc 的错误定位（u4-l5 的 CodegenError）能指到 Python 源码行，靠的就是 FunctionVisitor 在遍历 AST 时不断调用 `builder.set_loc(文件名, 行, 列)` 更新这个成员，之后所有 `create_*` 自动带上最新位置。

`create_asc_*` 系列有**两条来源**：

| 来源 | 判据 | 例子 |
|---|---|---|
| 手写 | 直接写在 `bindCreateAsc*` 函数里 | `create_asc_SetFlagOp`、`create_asc_PrintfOp`、`get_quebind_type` |
| 生成 | `.td` 定义经 `-gen-pybind-defs` 展开，从 `.inc` 被 include | `create_asc_QueBindOp`、绝大多数 `asc.*` API |

**判别方法只有一个**：在 `OpBuilder.cpp` 里全文搜 `create_asc_QueBindOp`，搜不到，就说明它来自 L949 的那个 `#include "ascir/Dialect/Asc/IR/AscOpBindings.h.inc"`。

手写的为什么手写？三类原因：参数需要**特殊翻译**（Python `uint8_t` → `symbolizeHardEvent` 枚举）、返回值不是单个 Value（要打包成 tuple）、或者目标根本不是 ascendc 方言的 Op（`create_func_*`、`create_scf_*`、`create_arith_*`、`create_memref_*`、`create_vector_*`、`create_emitc_*`、`create_emitasc_*`——这些是上游 MLIR 方言，pyasc 没有为它们写 td 的 pybind 生成）。

#### 4.3.2 核心流程

一次 `builder.create_asc_QueBindOp(t)` 的完整落点（生成轨）：

```text
Python:  builder.create_asc_QueBindOp(ir_type)
           │ pybind11 查到生成代码里的 .def("create_asc_QueBindOp", lambda)
           ▼
生成代码(AscOpBindings.h.inc，由 GenPybindDefs.cpp 从 TQue.td 生成):
           return self.create<ascendc::QueBindOp>(que_bind);
           │ 模板成员 PyOpBuilder::create<OpTy> 补上当前 loc
           ▼
PyOpBuilder::create<OpTy>(L134-138):
           builder.create<OpTy>(loc, args...)   ← MLIR 通用工厂
           │ 在当前插入点把 Operation 挂进 Block
           ▼
返回 ascendc::QueBindOp 的单个结果 → 按 Value 返回 Python
```

#### 4.3.3 源码精读

**PyOpBuilder 类骨架**：

[python/src/OpBuilder.cpp:L54-L86](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/src/OpBuilder.cpp#L54-L86) —— 类定义：构造函数接收 `MLIRContext*`，`loc` 初始化为 `UnknownLoc`；`setLoc` 有三个重载（直接给 Location / 给名字串 / 给文件名+行+列+可选名字），`getLoc`/`resetLoc` 读写，`getBuilder` 与 `operator->` 把内部的 `OpBuilder` 暴露给同文件的其他代码。

[python/src/OpBuilder.cpp:L90-L126](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/src/OpBuilder.cpp#L90-L126) —— 插入点管理：`setInsertionPointToStart/ToEnd`、`setInsertionPointAfter`、`restoreInsertionPoint`。注意每个方法在移动插入点的同时**顺手同步 loc**（取目标处已有 Op 的位置，空块则重置为 UnknownLoc）——这就是「u4 控制流讲过的游离块预访问后能接回原位置」在 C++ 侧的实现细节。

[python/src/OpBuilder.cpp:L128-L138](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/src/OpBuilder.cpp#L128-L138) —— 两个 `create`：L128-L132 是「按操作名字符串创建」的通用入口（配合 Python 侧的 `builder.create(name, operands, types)`，用于极少数动态场景）；L134-L138 是模板版本 `create<OpTy>(args...) -> OpTy`，**自动补上成员 `loc`**。所有生成出来的 `create_asc_*` 代码都调用它，这就是源码位置信息被无感附加的地方。

**绑定注册的总装**：

[python/src/OpBuilder.cpp:L1005-L1037](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/src/OpBuilder.cpp#L1005-L1037) —— `initBuilderInIRModule` 用 `py::class_<PyOpBuilder> clss(m, "Builder", py::module_local(), py::dynamic_attr())` 把类注册为 `ir.Builder`，然后按「初始化 → loc → 插入点 → 基础类型 → 特殊类型 → 属性 → 各家族 create」的顺序调用 22 个 `bind*` 函数。这个顺序也是阅读该文件的推荐路线图。

**手写轨示例一：枚举翻译**：

[python/src/OpBuilder.cpp:L862-L872](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/src/OpBuilder.cpp#L862-L872) —— `create_asc_SetFlagOp` / `create_asc_WaitFlagOp`：Python 传来的 `event` 是 `uint8_t`，先经 `getHardEvent`（定义在 L174-L180，内部 `symbolizeHardEvent`，失败抛 `runtime_error`）翻译成 `ascendc::HardEvent` 枚举，再创建 Op。u2-l4 讲过「HardEvent 方向是编译期属性」，在绑定层的表现就是：它在参数里、而不是在运行时 Value 里。

**手写轨示例二：复杂类型工厂**：

[python/src/OpBuilder.cpp:L294-L305](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/src/OpBuilder.cpp#L294-L305) —— `get_quebind_type(src, dst, depth)`：两个 `uint8_t` 各自 `symbolizeTPosition`（未知值抛错），然后 `getType<ascendc::QueBindType>(*srcPos, *dstPos, depth)` 一步得到队列类型。对比 [L326-L383](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/src/OpBuilder.cpp#L326-L383) 的 `get_matmul_type`（60 余个参数、手工装配 `MatmulConfigAttr`），就能理解为什么复杂类型走手写、简单类型走生成。

**手写轨示例三：多结果 Op**：

[python/src/OpBuilder.cpp:L969-L982](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/src/OpBuilder.cpp#L969-L982) —— `create_asc_GetMrgSortResults` 创建 `GetMrgSortResultOp` 后用 `py::make_tuple` 把 4 个结果打包返回。生成器只会处理「0 个或 1 个结果」两种形态（见下），多结果的只能手写。

**生成轨的接入点**：

[python/src/OpBuilder.cpp:L949](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/src/OpBuilder.cpp#L949) —— `bindCreateAscEventOperations` 的 `.def(...)` 链**中间**插着 `#include "ascir/Dialect/Asc/IR/AscOpBindings.h.inc"`。这个文件的内容是一长串 `.def("create_asc_XxxOp", ...)` 文本，被预处理机拼进当前这条链式表达式——上一讲（u5-l4）说「产物被 include 进 OpBuilder.cpp 的 .def 链中间」，指的就是这一行。类似地，[L411](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/src/OpBuilder.cpp#L411) 的 `#include "ascir/API/AscTypeBindings.h.inc"` 把上百个参数结构体**类型**的 getter 拼进了类型工厂那条链。

**生成器如何决定方法名与返回值**（回顾 u5-l4，聚焦与 create 相关的三段）：

[lib/TableGen/GenPybindDefs.cpp:L51-L58](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/TableGen/GenPybindDefs.cpp#L51-L58) —— 方法名的拼装规则：`create_` + （方言名，`ascendc` 特判缩写为 `asc`）+ `_` + Op 的 C++ 类名。所以 `AscendC_QueBindOp`（td 记录名）→ C++ 类 `QueBindOp` → Python 方法 `create_asc_QueBindOp`。

[lib/TableGen/GenPybindDefs.cpp:L63-L74](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/TableGen/GenPybindDefs.cpp#L63-L74) —— 结果只有一个时返回类型写 `Value` 并在函数体前加 `return`，否则返回 Op 本身；函数体就是一行 `self.create<命名空间::类名>(按序逗号连接的实参)`。

[lib/TableGen/GenPybindDefs.cpp:L78-L84](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/TableGen/GenPybindDefs.cpp#L78-L84) —— 从「最后一个必选参数」往后全部声明为 pybind11 具名参数并带默认值，所以 Python 侧可以 `builder.create_asc_AddOp(dst=..., src0=..., src1=..., ...=...)` 按名传参、省略可选项。

据此可以**推断**（非源码原文，属生成产物的推导示例）`create_asc_QueBindOp` 的生成形态大致为：

```cpp
// 示例代码：以下是根据 GenPybindDefs.cpp 规则对 TQue.td L191 推导出的生成产物形态，
// 实际内容以构建产物 AscOpBindings.h.inc 为准。
.def("create_asc_QueBindOp", [](PyOpBuilder &self, const Type &que_bind) -> Value {
    return self.create<ascendc::QueBindOp>(que_bind);
}, "que_bind"_a)
```

其 td 来源：

[include/ascir/Dialect/Asc/IR/Fwk/TQue.td:L191-L194](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/IR/Fwk/TQue.td#L191-L194) —— `def AscendC_QueBindOp : AscendC_Op<"que_bind", [AscConstructor]>`，只有一个结果 `AscendC_QueBind:$que_bind`——单结果，正对应生成器里 `retVal = true` 的分支。

**上游方言家族的手写绑定**（补充地图）：

[python/src/OpBuilder.cpp:L694-L728](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/src/OpBuilder.cpp#L694-L728) —— `bindCreateScfOperations`：`create_scf_ForOp`（lb/ub/step + 可选 init_args）、`create_scf_IfOp`（condition + 可选 ret_types + with_else）、`create_scf_YieldOp`、`create_scf_WhileOp`、`create_scf_ConditionOp`。u4-l3 的循环与分支 IR 化最终都落在这几个方法上。同类的还有 arith（L582-L692）、memref（L730-L761）、vector（L763-L779）、emitc（L781-L802）、emitasc（L804-L835）、func（L474-L502）、常量族（L504-L580）。

#### 4.3.4 代码实践

**实践目标**：确认 `create_asc_QueBindOp` 不在手写代码里，并摸清 `Builder` 的方法命名规律。

**操作步骤**：

```bash
# 1) 在 OpBuilder.cpp 中全文搜索（应无手写命中）
grep -n "create_asc_QueBindOp" python/src/OpBuilder.cpp || echo "手写代码中没有 → 来自生成的 .inc"

# 2) 数一数两类来源的规模
grep -c '\.def("create_' python/src/OpBuilder.cpp
grep -n '#include "ascir' python/src/OpBuilder.cpp

# 3) Python 侧看 Builder 有多少 create_asc_* 方法
python3 -c "
from asc._C import ir
ms = [n for n in dir(ir.Builder) if n.startswith('create_asc_')]
print('create_asc_* 方法数:', len(ms))
print('QueBindOp 在其中吗:', 'create_asc_QueBindOp' in ms)
others = [n for n in dir(ir.Builder) if n.startswith('create_') and not n.startswith('create_asc_')]
print('非 ascendc 家族前缀分布:', sorted({n.rsplit('_',2)[0] for n in others}))
"
```

**需要观察的现象**：步骤 1 输出「手写代码中没有」；步骤 3 中 `create_asc_*` 数量远大于手写 `.def("create_` 计数（差值即生成代码贡献的部分），且非 ascendc 家族的前缀分布在 `create_arith`、`create_scf`、`create_func`、`create_memref`、`create_vector`、`create_emitc`、`create_emitasc` 等几组。

**预期结果**：与上述一致。具体数字随版本变化，记录你环境里的两个数字即可。**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：`create_arith_AddIOp` 为什么不能像 `create_asc_AddL2Op` 一样由 `-gen-pybind-defs` 生成？

**答案**：`-gen-pybind-defs` 只遍历本仓库 `.td` 里 `Op` 记录的派生定义（`records.getAllDerivedDefinitions("Op")`，见 GenPybindDefs.cpp L90-L92）。`arith.AddIOp` 属于 MLIR 上游方言，其 td 不在本仓库 TableGen 的扫描范围内，因此必须在 `OpBuilder.cpp` 手写绑定（L587-L590）。

**练习 2**：如果把 `PyOpBuilder::create<OpTy>`（L134-L138）里补 `loc` 的那行去掉、直接用 `builder.create` 的无 loc 重载，前端会有什么可观察变化？

**答案**：所有生成的 IR 节点将失去源码位置（变成 UnknownLoc）。可观察后果：dump 出的 mlir 不再带 `loc(...)` 行号信息，u4-l5 讲的 CodegenError 报错定位与 `PYASC_DUMP_PATH` 产物里的位置标注都会退化甚至失效。（MLIR 侧 `create` 的无 loc 重载可用性以实际代码为准，此处结论按「位置丢失」的方向作答。）

**练习 3**：想新加一个 `create_asc_FooOp` 的 Python 入口，最少要改哪几个文件？

**答案**：正常路径下**一个都不用改桥接层**——只要在 `.td` 里 `def AscendC_FooOp : AscendC_Op<...>`，`AscOpBindings.h.inc` 重新生成后 `.def("create_asc_FooOp", ...)` 自动出现。只有当该 Op 需要特殊参数翻译（如枚举 symbolize）或多结果打包时，才需要在 `OpBuilder.cpp` 手写。这正是 u5-l4 说的「TableGen 免去手写数千个绑定」的落点。

### 4.4 模块四：Passes.cpp 与 Translation.cpp——把后端能力交给 Python

#### 4.4.1 概念说明

`Passes.cpp` 暴露的是「跑 Pass」的两样东西：`PassManager` 类（一个 Pass 容器/执行器）和一大箱 `add_*` 函数（每个函数把一个具体 Pass 装进 PassManager）。`Translation.cpp` 则只暴露一个函数 `ir_to_ascendc`，把跑完 Pass 的 IR 模块翻译成 Ascend C 源码文本。

这两个文件是 u3-l4「编译器驱动」在 C++ 侧的对接面：`compiler.py` 里所有 `passes.ascendc.add_xxx(pm)` 调用，逐条对应 `Passes.cpp` 里的一行宏展开。

#### 4.4.2 核心流程

```text
Compiler.run_passes(mod)                       ← compiler.py L176
   pm = passes.PassManager(mod.get_context())  ← Passes.cpp L41-42 绑定的构造
   pm.enable_verifier()
   [可选] pm.enable_printing()                 ← print_ir_before_all 的落点
   _schedule_lowering/optimizing/postprocessing(pm)
       → passes.common.add_canonicalizer(pm)     等  ← DEFINE_ADD_PASS 宏
       → passes.ascendc.add_insert_sync(pm)      等  ← DEFINE_ADD_PASS_ON 宏（嵌套到 FuncOp 层级）
   pm.run(mod)                                 ← Passes.cpp L52-59，失败抛 runtime_error
Compiler.run_translation(mod)
   → translation.ir_to_ascendc(mod)            ← Translation.cpp L32-39
```

#### 4.4.3 源码精读

**两个宏消除全部样板**：

[python/src/Passes.cpp:L27-L30](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/src/Passes.cpp#L27-L30) —— `DEFINE_ADD_PASS(NAME, CONSTRUCTOR)` 生成「模块级函数 `NAME(PassManager&)`，函数体是 `pm.addPass(CONSTRUCTOR())`」；`DEFINE_ADD_PASS_ON(NEST, ...)` 则生成 `pm.addNestedPass<NEST>(...)`，把 Pass 加到**嵌套层级**（这里是 `func::FuncOp`）。二者的区别对应 MLIR 的 op-specific pass 与 generic pass：作用在函数内部的 Pass（如 `insert_sync`）走嵌套版本，作用在整个模块的 Pass（如 `generate_boilerplate`）走普通版本。这也是 u6-l1「Pass 全景表」里判断 Pass 作用域的依据。

**PassManager 的四个方法**：

[python/src/Passes.cpp:L37-L75](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/src/Passes.cpp#L37-L75) —— 绑定 `PassManager` 类：
- 构造：`PassManager(context)`；
- `get_pipeline_str()`：把当前流水线打印成 MLIR 文本管道语法（`any(passes...)` 形态），调试时非常有用；
- `run(mod)`：装好 SourceMgr 诊断处理器后执行，失败抛 `runtime_error`（Python 侧表现为异常）；
- `enable_verifier(enable)`：每个 Pass 之后跑 IR 校验；
- `enable_printing()`：打开「每个 Pass 前后都打印 IR」——即 u3-l4 的 `print_ir_before_all=True` 的底层实现（注意它同时打印前与后，且 `printAfterOnlyOnFailure=true`）。

**两箱 Pass**：

[python/src/Passes.cpp:L77-L89](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/src/Passes.cpp#L77-L89) —— `common` 子模块装 9 个 MLIR 上游通用 Pass：`canonicalizer`、`cse`、`inliner`、`licm`、`print_ir`、`reconcile_unrealized_casts`、`sccp`、`strip_debug_info`、`symbol_dce`。

[python/src/Passes.cpp:L91-L111](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/src/Passes.cpp#L91-L111) —— `ascendc` 子模块装 pyasc 自研的 16 个 Pass（`add_privatize_func`、`add_input_output_tensor`、`add_hoist_ub_allocation`、`add_materialize_tensor`、`add_unify_pipe`、`add_erase_sync`、`add_insert_sync`、`add_hoist_que_bind`、`add_generate_boilerplate`、`add_legalize_kernel_args`、`add_declare_py_struct`、`add_detect_kernel_type`、`add_detect_enable_debug`、`add_verify_sync`、`add_define_cube_only`、`add_noop_pass`）。每个 `create*Pass` 工厂函数都来自 u6-l1 将精读的 `ascir/Dialect/Asc/Transforms/Passes.h`。

Python 侧的对应调用见 [python/asc/runtime/compiler.py:L120-L131](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/compiler.py#L120-L131)（`_schedule_lowering` 混排两箱 Pass）与 [L176-L179](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/compiler.py#L176-L179)（`run_passes` 的三行：建 pm、开校验、按需开打印）。

**翻译入口**：

[python/src/Translation.cpp:L30-L40](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/src/Translation.cpp#L30-L40) —— `initTranslationModule` 只绑一个函数 `ir_to_ascendc(mod)`：把模块写进 `std::string` 流，调用后端 `translateToAscendC`（u6-l5 的 `lib/Target/AscendC/Translation.cpp`），失败抛 `runtime_error`，成功返回 Ascend C 源码字符串。消费侧在 [python/asc/runtime/compiler.py:L115-L117](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/compiler.py#L115-L117) 的 `run_translation`。

#### 4.4.4 代码实践

**实践目标**：不写 kernel，直接在 Python 里手工搭「Context → Module → PassManager」，打印空流水线并跑一次通用 Pass。

**操作步骤**：

```bash
python3 -c "
from asc._C import ir, passes

ctx = ir.Context()
ir.load_dialects(ctx)
b = ir.Builder(ctx)
mod = b.create_ModuleOp()

pm = passes.PassManager(mod.get_context())   # ModuleOp 经 OpState.get_context 拿上下文
passes.common.add_canonicalizer(pm)
passes.common.add_symbol_dce(pm)
print('pipeline:', pm.get_pipeline_str())

pm.enable_verifier()
pm.run(mod)
print('run 之后模块仍在:', str(mod)[:40], '...')
"
```

**需要观察的现象**：`get_pipeline_str()` 打印出形如 `module(...)` 包着 `canonicalize`、`symbol-dce` 的文本管道；`pm.run` 对空模块正常返回，不抛异常。

**预期结果**：如上。可以再试 `passes.ascendc.add_noop_pass(pm)` 体会嵌套 Pass 装箱后 `get_pipeline_str()` 的变化（会出现 `builtin.module(... (func.func(...)))` 一类的嵌套结构）。**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：`add_insert_sync` 用 `DEFINE_ADD_PASS_ON`，`add_generate_boilerplate` 用 `DEFINE_ADD_PASS`，这个差异说明什么？

**答案**：`insert_sync` 被装成 `addNestedPass<func::FuncOp>(...)`，说明它逐函数处理函数体内部的同步指令序列；`generate_boilerplate` 是模块级 Pass，看的是整个模块的符号表与 Kernel 声明结构。作用层级不同，改 IR 的权限与遍历入口也不同。

**练习 2**：`pm.run()` 失败时 Python 侧看到什么？为什么不直接返回 bool？

**答案**：看到 `runtime_error` 转成的 Python `RuntimeError` 异常（Passes.cpp L57-L58 主动 `throw`）。用异常而非返回值，是因为 Pass 失败属于「这一轮编译整体失败」，`compiler.py` 上层不打算在失败后继续；异常能自然中断流水线并把 MLIR 诊断信息一路带出去。

## 5. 综合实践

**任务：追踪 `asc.TQueBind(...)` 构造调用，产出一张「Python 调用 → pybind → MLIR Op 创建」的时序说明。**

这是本讲规格中指定的主实践，把四个最小模块串成一条线。

**第一步：找到 Python 侧的调用点。**
[python/asc/language/fwk/tpipe.py:L51-L53](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/fwk/tpipe.py#L51-L53) —— `TQueBind.__init__` 的核心三行：`builder = global_builder.get_ir_builder()` 取得 `ir.Builder`（即 C++ 的 `PyOpBuilder` 实例）；`ir_type = builder.get_quebind_type(src, dst, depth)` 先造类型；`self.handle = builder.create_asc_QueBindOp(ir_type)` 再造 Op。注意 `TQue` 是 `TQueBind` 的子类（tpipe.py L540），所以框架风格示例里 `asc.TQue(...)` 走的也是这个构造函数。

**第二步：确认 `get_quebind_type` 是手写绑定。**
[python/src/OpBuilder.cpp:L294-L305](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/src/OpBuilder.cpp#L294-L305) —— 手写 lambda：两次 `symbolizeTPosition` 把 Python 的整数枚举翻成 C++ 枚举，`getType<ascendc::QueBindType>` 产出 `!ascendc.que_bind<...>` 类型对象，按 `Type` 返回 Python。

**第三步：确认 `create_asc_QueBindOp` 是生成绑定。**
在 `OpBuilder.cpp` 全文搜索无命中；生成代码从 [L949](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/src/OpBuilder.cpp#L949) 的 `#include "ascir/Dialect/Asc/IR/AscOpBindings.h.inc"` 拼入；该 `.inc` 由 [include/ascir/Dialect/Asc/IR/CMakeLists.txt:L42-L43](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/IR/CMakeLists.txt#L42-L43) 用 `-gen-pybind-defs` 生成；td 源是 [include/ascir/Dialect/Asc/IR/Fwk/TQue.td:L191-L194](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/IR/Fwk/TQue.td#L191-L194) 的 `AscendC_QueBindOp`（单结果 `que_bind`）。

**第四步：按 GenPybindDefs.cpp 的规则推导生成形态并标注每行来源。**
生成规则在 [lib/TableGen/GenPybindDefs.cpp:L51-L84](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/TableGen/GenPybindDefs.cpp#L51-L84)：方法名（L51-L58）、单结果返回 `Value`（L63-L65）、函数体一行 `self.create<...>`（L71-L73）、具名参数（L78-L83）。推导出的形态见 4.3.3 中的「示例代码」块。

**第五步：画出时序说明。**

```text
Python 侧                       pybind/C++ 侧
──────────                      ──────────────
asc.TQueBind(VECIN, VECOUT, 2)
  │ tpipe.py L51: global_builder.get_ir_builder()
  │   ← 返回 utils.py L144 创建、缓存的 ir.Builder（= C++ PyOpBuilder）
  ├─ builder.get_quebind_type(src, dst, depth)
  │      └──────────────────→ OpBuilder.cpp L294: symbolize×2 + getType<QueBindType>
  │   ← ir.Type（!ascendc.que_bind<...>）
  ├─ builder.create_asc_QueBindOp(ir_type)
  │      └──────────────────→ AscOpBindings.h.inc 生成的 .def（经 L949 include）
  │                            └→ PyOpBuilder::create<QueBindOp>（L134-138，补 loc）
  │                                 └→ mlir::OpBuilder::create（插入点追加 Operation）
  │   ← ir.Value（该 Op 的唯一结果）
  └─ self.handle = <ir.Value>      （随后被 TQueBind/IRValue 包装，u2-l3）
```

**验证方式（可选，需要能跑 02_add_framework 示例的环境）**：设置 `PYASC_DUMP_PATH` 运行 `examples/02_add_framework/add_framework.py`，在导出的 `codegen.mlir` 中找到 `ascendc.que_bind`（TQue/TQueBind 声明节点），确认它的类型拼写与第二步产出的类型一致、且 loc 指向示例源码中的 `asc.TQue(...)` 所在行——后者正是 `PyOpBuilder` 携带 `loc` 成员的直接证据。**待本地验证**。

**交付物**：上面的时序图（补充你观察到的 loc 行号）+ 一句话回答：「为什么这条链路里既有手写绑定又有生成绑定？」（参考答案：类型构造需要枚举翻译所以手写；Op 创建是单结果、参数直传的规整形态，正好落在生成器的模板能力之内。）

## 6. 本讲小结

- `libpyasc` 由 [Module.cpp](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/src/Module.cpp) 一处注册，挂出 `ir`/`passes`/`translation` 三个子模块；`Builder` 不是独立子模块，由 `IR.cpp` 的 `initIRModule` 末尾挂进 `ir`。
- `IR.cpp` 绑定的是 MLIR 公共对象模型（Context/Type/Value/Operation/OpState 及 ModuleOp、FuncOp 派生），并承担几个 pyasc 特有查询：`need_insert_sync`、`get_kernel_arg_attrs`。
- `OpBuilder.cpp` 的 `PyOpBuilder` = MLIR `OpBuilder` + 当前 `Location`；模板成员 `create<OpTy>` 统一补 loc，是所有生成绑定的共同落点。
- `create_asc_*` 是双轨制：需要枚举翻译、多结果打包或属于上游方言的走手写；规整的单/零结果 ascendc Op 由 `-gen-pybind-defs` 从 td 自动生成并经两个 `.inc` 拼进 `.def` 链。
- `Passes.cpp` 用两个宏把 25 个 Pass（9 个 common + 16 个 ascendc）装成 `passes.common.*` 与 `passes.ascendc.*` 两箱函数；`Translation.cpp` 只有一个 `ir_to_ascendc`。
- 追踪任一 `builder.create_asc_XxxOp` 的口诀：先在 `OpBuilder.cpp` 搜方法名，搜不到就到 `AscOpBindings.h.inc` 的生成规则（GenPybindDefs.cpp）+ 对应 `.td` 里找 `def AscendC_XxxOp`。

## 7. 下一步学习建议

下一讲（u5-l6）「OpBuilder 使用侧：前端如何创建 IR」会从 Python 使用视角总结 `global_builder` 的生命周期与「类型计算 + create_asc_XxxOp + 包装返回 IRValue」的三段式套路——本讲看清了桥的 C++ 侧结构，下一讲把镜头切回 Python 侧的调用模式，两讲合起来就是完整的「前端造 IR」机制。

更远一点的衔接：

- 想看 Pass 的 C++ 实现：第 6 单元 u6-l1 起，从 `include/ascir/Dialect/Asc/Transforms/Passes.h`（本讲 `create*Pass` 工厂函数的出处）进入。
- 想看 Ascend C 发射：u6-l5 精读 `lib/Target/AscendC/Translation.cpp`，即本讲 `translateToAscendC` 的真身。
- 想自己加一个新 Op 的 Python 入口：按 u5-l3 的 td 定义 + 本讲 4.3.5 练习 3 的结论，验证「零桥接层改动」的生成链路，再到 `python/test/unit` 补一个用例（u7-l6）。
