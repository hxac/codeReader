# JAX / XLA FFI 互操作

## 1. 本讲目标

本讲讲解 cuTile 如何把一个用 `@ct.kernel` 写的 tile 内核接入 JAX 计算图。学完后你应当掌握：

- 会用 `cutile_call` + `OutputPlaceholder` / `InputOutput` 把一个 cuTile 内核包装成 JAX 可调用、可 `jax.jit`、可求导上下文（作为 custom op）的算子；
- 理解 `cutile_call` 如何从内核的 **参数注解树 `parameter_annotations`** 按叶子节点逐个派生出每个参数的 **role**（输入/输出/常量/标量）与对应的 `ParameterConstraint`，以及 **为什么 tuple 参数会被显式拒绝**；
- 理解 `register_ffi` / `register_primitive` 这两步如何把一个 C++ FFI handler 注册成 XLA 的 `cutile_launch` custom call，并挂上 MLIR lowering；
- 理解 C++ 侧 `cutile_call_handler` 的 **三阶段执行模型**（INSTANTIATE / INITIALIZE / EXECUTE）、`KernelEntry` 进程级 cubin 注册表与引用计数生命周期，以及 `pack_buffer` 如何按 `cutile_python_v1` 调用约定把一个 XLA buffer 展开成「指针 + shape + strides」的字面参数序列。

本讲是「运行时调度、导出与扩展」单元的一讲，承接 u8-l1（`launch` 与调用约定）建立的 `cutile_python_v1` 二进制参数格式直觉，把它放到 JAX/XLA 这个具体的宿主框架里再走一遍。

## 2. 前置知识

在进入本讲前，你应当已经了解（来自前置讲义）：

- **load–compute–store 范式与 `@ct.kernel`**（u3-l1）：内核是「被翻译的 Python」，本身不能直接调用，必须由宿主端「启动」。
- **`launch` 与 `cutile_python_v1` 调用约定**（u8-l1）：启动一个 cuTile 内核时，每个数组参数会被展开成 `1 + 2·ndim` 个字面参数——一个设备指针、`ndim` 个 shape、`ndim` 个 strides（行优先，末维 stride=1）；标量参数则是单个字面值。本讲 C++ 侧的 `pack_buffer` 就是这套约定的复刻。
- **参数注解树 `parameter_annotations`**（u5-l1、u3-l7）：`AnnotatedFunction` 用一棵 `ParameterAnnotationNode` 树表达参数注解，叶子节点 `LeafAnnotationNode` 带 `constant` / `array` / `scalar` / `list` 四个互斥字段；tuple 参数产生非叶子节点（`HomogeneousTupleNode` / `HeterogeneousTupleNode`）。本讲的关键恰恰是「JAX 集成只认叶子」。
- **`KernelSignature` 与 `ParameterConstraint`**（u8-l2）：AOT/JIT 编译都把每个参数归一成一个约束（`ArrayConstraint` / `ScalarConstraint` / `ConstantConstraint` 等），约束决定 cubin 的特化与 mangled 符号名。

此外，你需要对 JAX 有最浅层的概念：JAX 程序是先 trace 成一个「计算图」（HLO/MLIR），再 lower 到后端执行。一个 JAX 原生不认识的算子（比如我们的 cuTile 内核）要进入这个图，必须注册成一个 **custom call**（XLA 术语）或 **FFI target**（JAX 侧术语）。本讲就是把 cuTile 内核做成这样一个 custom call。

> 名词速辨：**FFI**（Foreign Function Interface）在这里特指 XLA 提供的、让用户用 C/C++ 实现自定义算子执行逻辑的 C API（`xla/ffi/api/c_api.h`）。它不是 Python 的 ctypes/cffi。

## 3. 本讲源码地图

本讲横跨 Python 前端桥接层与 C++ 运行时桥接层，涉及的关键文件如下：

| 文件 | 角色 | 作用 |
| --- | --- | --- |
| `src/cuda/tile/jax/__init__.py` | Python 包入口 | 导出 `cutile_call` / `OutputPlaceholder` / `InputOutput`；在 import 时自动调用 `register_ffi()` + `register_primitive()`。 |
| `src/cuda/tile/jax/_jax.py` | Python 桥接核心 | `cutile_call` 用户 API、role 派生、MLIR lowering、cubin 编译缓存。本讲主力文件。 |
| `cext/xla_ffi.h` | C++ 头文件 | 声明 FFI handler 与 type id 访问器。 |
| `cext/xla_ffi.cpp` | C++ 执行器 | 三阶段 handler、`KernelEntry` 注册表、`pack_buffer`、`cuLaunchKernel`。 |
| `cext/xla_ffi_py.cpp` | C++ Python 绑定 | 把 C++ handler 包成 `PyCapsule` 暴露给 Python，供 `register_ffi` 取用。 |

一句话关系链：用户调 `cutile_call` →（Python）派生 role、编译 cubin、构造 MLIR custom call 属性 →（C++ `cutile_call_handler`）三阶段执行，INSTANTIATE 时加载 cubin、EXECUTE 时打包参数并 `cuLaunchKernel`。

## 4. 核心概念与源码讲解

本讲按「数据如何流动」拆成四个最小模块：

1. **4.1 `cutile_call`：从注解树派生 role**——把 JAX 侧的 Python 参数翻译成内核 arg 的角色与约束。
2. **4.2 注册 XLA FFI：`register_ffi` 与 `register_primitive`**——Python 与 C++ 的 capsule 桥，以及 MLIR 注册。
3. **4.3 lowering：编译 cubin 并装配 launch 属性**——`_cutile_call_ffi_p_lower`、`compile_kernel_cached`、`_array_constraint`、`pack_scalar`。
4. **4.4 C++ 执行器：`cutile_call_handler` 三阶段与 `KernelEntry` 注册表**——cubin 生命周期与参数打包。

### 4.1 cutile_call：从注解树派生 role

#### 4.1.1 概念说明

`cutile_call` 是用户把 cuTile 内核接入 JAX 图的唯一入口。它的核心设计是 **「一个参数一个角色」**（one-arg-one-role）：内核的第 `i` 个参数，在 JAX 侧对应 `args[i]`，会被归入且仅归入一种 **role**：

- `'i'`：输入 buffer（只读 `jax.Array`）。
- `'o'`：输出 buffer（`OutputPlaceholder`，由 JAX 分配，作为函数返回值）。
- `'io'`：输入兼输出 buffer（`InputOutput`，in-place 更新）。
- `'c'`：常量标量（编译期嵌入 cubin，每个取值编一份）。
- `'s'`：运行时标量（不嵌入 cubin，启动时按位打包传入）。

这套「角色」抽象是必需的，因为 XLA FFI 区分 **buffer 参数**（GPU 显存指针，由 XLA 调度分配）与 **属性/标量**（编译期或启动期的小常数），而 cuTile 内核的参数语义比这更丰富（数组、标量、常量、可别名）。`cutile_call` 的第一职责就是把内核参数的「cuTile 语义」翻译成「XLA FFI 语义」。

这套翻译的**事实来源是参数注解树**，而非运行时 `isinstance` 探测。具体说，`cutile_call` 会读 `kernel._annotated_function.parameter_annotations`，对每个 `LeafAnnotationNode` 取其 `constant` / `array` / `scalar` 字段，决定 role 与约束。

#### 4.1.2 核心流程

`cutile_call(grid, kernel, args)` 的执行流程可以概括为：

```text
1. 取出注解树 annotations = kernel._annotated_function.parameter_annotations
2. 校验：每个注解节点必须是 LeafAnnotationNode（否则 tuple 参数 → NotImplementedError）
3. 遍历 (args[i], annotations[i])，按 Python 值类型 + 注解字段派生 role：
     OutputPlaceholder          → 'o'，记一个输出
     jax.Array                  → 'i'，记一个输入
     InputOutput                → 'io'，记一个输入 + 一个输出，并登记 alias
     bool/int/float + constant  → 'c'，塞进 constants 列表
     bool/int/float + 非常量    → 's'，塞进 scalars 列表
4. _check_roles：Constant 注解的参数必须配 'c' role（防止把常量当运行时标量传）
5. 计算 alias_group（同一 jax.Array 被多次引用时编组）
6. 构造一个 inline 的 jax.jit wrapper，绑定 cutile_call_ffi_p primitive，
   把 kernel/grid/outputs/constants/scalars/roles/alias_group 作为 primitive 参数
7. 返回 wrapper(*input_arrays)
```

注意第 6 步：真正进入 JAX 图的是 `cutile_call_ffi_p`（一个 JAX `Primitive`），`cutile_call` 自己只是在 trace 之前做参数归类。输出个数为 0/1/多个时返回值形态不同（单个直接返回数组，多个返回元组）。

#### 4.1.3 源码精读

先看两个轻量数据类，它们是 `cutile_call` 的参数「标记类型」：

[src/cuda/tile/jax/_jax.py:37-50](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/jax/_jax.py#L37-L50) 定义了 `OutputPlaceholder`（声明一个输出 buffer 的 shape/dtype）与 `InputOutput`（把一个输入 buffer 同时当作输出，实现 in-place）。

`cutile_call` 的入口与注解树获取：

[src/cuda/tile/jax/_jax.py:144-151](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/jax/_jax.py#L144-L151) 取出 `parameter_annotations`，并校验实参个数与内核形参个数一致。

接下来是本讲的一条关键防线——**tuple 参数的显式拒绝**：

[src/cuda/tile/jax/_jax.py:156-161](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/jax/_jax.py#L156-L161) 遍历每个注解节点，只要不是 `LeafAnnotationNode`（即出现了 `HomogeneousTupleNode` / `HeterogeneousTupleNode`，对应 tuple 参数），立刻抛 `NotImplementedError`，并明确提示「tuple parameters are not supported via the JAX/FFI integration」。注释解释了原因：JAX/FFI 集成是「一参数一角色」，且直接读叶子的 `.constant`/`.array`/`.scalar`，无法把非叶子的 tuple 节点展平成 buffer/constraint。

随后的 role 派生主循环：

[src/cuda/tile/jax/_jax.py:163-184](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/jax/_jax.py#L163-L184) 按 Python 值类型 + 注解的 `constant` 字段把每个参数归入 `'o'/'i'/'io'/'c'/'s'`，同时维护四个并行列表（`outputs` / `input_arrays` / `constants` / `scalars`）与 alias 表。注意 bool/int/float 三种 Python 标量共用一个分支，是否进 `constants` 完全由 `ann_node.constant` 决定——这正是「事实来源是注解，不是类型」的体现。

[src/cuda/tile/jax/_jax.py:212-221](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/jax/_jax.py#L212-L221) 的 `_check_roles` 是一道交叉校验：凡是 `ann_node.constant` 为真的参数，role 必须是 `'c'`。这能拦下「内核声明了 `ct.Constant`，但用户却传了一个会被 JAX 当动态值的标量」这类错误。

最后，`cutile_call` 把归类结果绑到一个 JAX primitive 上：

[src/cuda/tile/jax/_jax.py:192-209](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/jax/_jax.py#L192-L209) 用 `@partial(jax.jit, inline=True)` 构造一个内联 wrapper，调用 `cutile_call_ffi_p.bind(...)`，把 `kernel`、`grid`、`outputs`、`constants`、`scalars`、`roles`、`alias_group` 作为 primitive 的「参数/属性」传入；单个输出解包返回，多个输出返回元组。

#### 4.1.4 代码实践

**实践目标**：用源码阅读验证「role 由注解决定，而非由 Python 类型决定」。

1. 阅读 [src/cuda/tile/jax/_jax.py:176-182](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/jax/_jax.py#L176-L182) 这段对 `bool/int/float` 的分支，确认同一个 Python `int` 值会因为 `ann_node.constant` 的真伪而分别走 `'c'` 或 `'s'`。
2. 对照 `test/test_jax.py` 中的两个内核：
   - `_scale` 的 `c: ct.Constant`（[test/test_jax.py:20-23](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/test/test_jax.py#L20-L23)）→ `c` 的 role 是 `'c'`，值烘焙进 cubin；
   - `_scale_non_const` 的 `c`（[test/test_jax.py:32-35](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/test/test_jax.py#L32-L35)）无注解 → `c` 的 role 是 `'s'`，运行时打包传入。
3. **需要观察的现象**：两个内核同样接收一个 Python `int`，但编译产物与可复用性不同——前者改值要重新编译，后者不需要。

> 待本地验证：在有 GPU 与 JAX 的环境下，分别用 `_scale` 与 `_scale_non_const` 各跑两次不同 factor，结合 `CUDA_TILE_LOGS=1` 观察前者是否触发两次 cubin 编译、后者只编译一次。

#### 4.1.5 小练习与答案

**练习 1**：一个内核签名是 `f(x, y, c: ct.Constant)`，用户调用 `cutile_call(grid, f, (x, OutputPlaceholder(...), 3))`。三个参数的 role 分别是什么？

**答案**：`'i'`（x 是 jax.Array）、`'o'`（OutputPlaceholder）、`'c'`（注解声明 Constant，3 是 int，命中 `ann_node.constant` 分支）。

**练习 2**：把上题的 `c: ct.Constant` 去掉，改成 `f(x, y, c)`，再传同样的 `3`。role 变成什么？会对 JIT 缓存行为产生什么影响？

**答案**：role 变成 `'s'`（运行时标量）。`3` 不再烘焙进 cubin，于是不同取值共用同一份 cubin，无需重新编译（这正是 `test_runtime_scalar_for_non_constant_param` 验证的行为）。

### 4.2 注册 XLA FFI：register_ffi 与 register_primitive

#### 4.2.1 概念说明

`cutile_call` 把参数归类后，真正进入 JAX 图的是一个名为 `cutile_call_ffi` 的 JAX `Primitive`。但光有 primitive 不够——JAX/XLA 还需要知道「当遇到这个 primitive 时，应该 lower 成哪条 MLIR custom call、这条 custom call 由哪个 C++ 函数执行」。这两件事分别由 `register_primitive` 与 `register_ffi` 完成。

这里有个容易混淆的点：JAX 的 FFI 注册分两层：

- **type 注册**（`jax.ffi.register_ffi_type`）：声明一种 custom call 的「类型」（一个 type_id + type_info），告诉 XLA 这个 custom call 携带哪种自定义状态对象。
- **target 注册**（`jax.ffi.register_ffi_target`）：声明一个「执行器」（instantiate + execute 回调），给定名字（这里是 `"cutile_launch"`）。

而 Python 侧的 C++ 回调，是通过 `PyCapsule`（一个携带 C 指针的不透明 Python 对象）从 `_cext` 传过来的——这避免了在 Python 里写 C 函数签名绑定。

`register_primitive` 则把 primitive `cutile_call_ffi_p` 与它的 MLIR lowering（`_cutile_call_ffi_p_lower`）和抽象求值（`def_abstract_eval`）绑定起来。

#### 4.2.2 核心流程

```text
import cuda.tile.jax 时（见 __init__.py）：
  若 HAS_JAX：
    register_ffi()       # 注册 XLA FFI type + target，名字都叫 "cutile_launch"
    register_primitive() # 创建 cutile_call_ffi_p，挂 lowering 与 abstract_eval
```

`register_ffi` 的两步：

```text
1. 从 cext 取出 call_type_id / call_type_info（C++ 侧 CutileCallState 的 type 信息）
   → jax.ffi.register_ffi_type("cutile_launch", {type_id, type_info}, platform='CUDA')
2. 从 cext 取出 call_handler（C++ 函数 cutile_call_handler 的指针，包成 capsule）
   → jax.ffi.register_ffi_target("cutile_launch",
        {instantiate: handler, execute: handler}, platform='CUDA')
```

注意 `instantiate` 与 `execute` 用的是**同一个** handler——C++ 的 `cutile_call_handler` 是个统一入口，内部按 `CallFrame.stage` 自行分派（见 4.4）。

#### 4.2.3 源码精读

包入口的自动注册：

[src/cuda/tile/jax/__init__.py:16-20](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/jax/__init__.py#L16-L20) 在 `HAS_JAX` 为真时自动调 `register_ffi()` + `register_primitive()`，否则记一条警告。这意味着用户只要 `import cuda.tile.jax`，FFI 就已经注册好了。

`register_ffi` 主体：

[src/cuda/tile/jax/_jax.py:226-238](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/jax/_jax.py#L226-L238) 先 `register_ffi_type`（携带从 cext 取的 type_id/type_info），再 `register_ffi_target`（携带从 cext 取的 call_handler）。两个注册都绑定到 `platform='CUDA'`，名字都是 `"cutile_launch"`。

`register_primitive` 主体：

[src/cuda/tile/jax/_jax.py:245-260](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/jax/_jax.py#L245-L260) 创建 `jax.extend.core.Primitive("cutile_call_ffi")`，标记 `multiple_results = True`（因为一个内核可能有多个输出），用 `mlir.register_lowering` 挂上 `_cutile_call_ffi_p_lower`，并用 `def_abstract_eval` 声明抽象求值——输出形状由 `output_shape_dtypes` 决定，与输入无关。

那三个从 cext 取出的「capsule」是怎么来的？看 C++ 绑定层：

[cext/xla_ffi_py.cpp:12-32](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/cext/xla_ffi_py.cpp#L12-L32) 定义了三个 `METH_NOARGS` Python 函数，分别把 `cutile_call_handler`（函数指针）、`cutile_call_state_type_id()`、`cutile_call_state_type_info()` 包成 `PyCapsule` 返回。Python 侧的 `cext.xla_ffi_get_call_handler()` 拿到的就是这个 capsule，里面装的是 C++ 函数 `cutile_call_handler` 的地址。这样 JAX 拿到一个 capsule 就能直接把它当作 C 回调传给 XLA，全程不需要手写 C 绑定。

#### 4.2.4 代码实践

**实践目标**：理解「import 即注册」与 capsule 桥。

1. 在 Python 里执行 `import cuda.tile.jax`，然后 `jax.ffi.get_ffi_target("cutile_launch", platform="cuda")`（待本地验证 API 名），观察是否能取到已注册的 target。
2. 阅读 [cext/xla_ffi_py.cpp:12-15](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/cext/xla_ffi_py.cpp#L12-L15)，确认 `py_get_call_handler` 返回的 capsule 里装的是 `&cutile_call_handler`（C++ 函数地址）。
3. **需要观察的现象**：FFI 注册是进程级、一次性的；重复 `import` 不会重复注册（因为 `register_ffi` 在模块顶层只跑一次）。

> 待本地验证：若手动再调一次 `cuda.tile.jax._jax.register_ffi()`，JAX 通常会因重复注册同名 target 而报错或覆盖——这说明这套注册的幂等性由「模块顶层只调用一次」保证，而非函数本身。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `register_ffi` 要先注册 type，再注册 target？两者各自携带什么信息？

**答案**：type 注册携带「这个 custom call 的自定义状态对象的类型标识」（`type_id` + `type_info`，对应 C++ 的 `CutileCallState` 及其 deleter），让 XLA 知道如何在 instantiate/execute 之间保存与销毁状态；target 注册携带「执行器回调」（`instantiate` + `execute`，这里都指向 `cutile_call_handler`），让 XLA 知道真正干活的是哪个 C 函数。

**练习 2**：`register_primitive` 里 `multiple_results = True` 的作用是什么？如果去掉会怎样？

**答案**：它告诉 JAX 这个 primitive 的返回值是一个「列表/元组」（对应多个输出 buffer），JAX 会把它当作多输出算子处理。若去掉，JAX 会把返回的多个值当成单个值，多输出内核（如同时写 sin 和 cos）的形状推断与解包都会出错。

### 4.3 lowering：编译 cubin 并装配 launch 属性

#### 4.3.1 概念说明

当 JAX trace 到 `cutile_call_ffi_p` 时，会调用 4.2 注册的 lowering 函数 `_cutile_call_ffi_p_lower`。它的职责是把 4.1 派生出的 role 列表 + 输入 avals **物化成一次具体的 cubin 编译 + 一组 MLIR 属性**，最终生成一条 `custom_call @cutile_launch` 指令。

这一步是 cuTile「JIT 编译」在 JAX 场景下的发生点：lowering 时调用 `compile_kernel_cached`，把内核按当前约束编译成 cubin 字节，再把 cubin 作为 MLIR 属性挂到 custom call 上。

这里有几个关键设计：

- **约束派生**：每个 role 对应一个 `ParameterConstraint`。数组 role 用 `_array_constraint` 从 JAX 的 `aval`（形状/dtype）+ 注解（index_dtype/alias）合成一个 `ArrayConstraint`；标量 role 用 `pack_scalar` 把 Python 标量按位打包成 64 位整数；常量 role 直接用 `ConstantConstraint`。
- **cubin 去重**：`compile_kernel_cached` 用 `(id(kernel), constraints)` 作 key 缓存，并对 cubin 算 SHA-256 得到 `cubin_id`，作为 C++ 侧注册表的键（见 4.4）。
- **属性清单**：lowering 最终把 `cubin_code` / `cubin_id` / `function_name` / `buffer_ids` / `index_bitwidths` / `scalar_packed` / `num_inputs` / `num_outputs` / `grid_x/y/z` 全部作为属性塞进 custom call。这些属性就是 C++ handler 在 instantiate/execute 时能读到的全部信息。

#### 4.3.2 核心流程

lowering 主循环（按 role 顺序处理每个内核参数位置 `pos`）：

```text
对每个 (pos, role)：
  先判断该参数是否是 i64 索引数组（annotations[pos].array.index_dtype == int64）
  role == 'i'  → buffer_ids 加一个输入下标，append ArrayConstraint(in_aval)
  role == 'o'  → buffer_ids 加一个输出下标（偏移 num_inputs），append ArrayConstraint(out_aval, alias=None)
  role == 'io' → buffer_ids 加输入下标，append ArrayConstraint(in_aval)，并登记 input_output_aliases
  role == 's'  → buffer_ids 加一个标量下标（偏移 num_inputs+num_outputs），pack_scalar 打包，append ScalarConstraint
  role == 'c'  → 不占 buffer 槽，append ConstantConstraint(常量值)

调用 compile_kernel_cached(kernel, constraints) → (symbol, cubin_bytes, cubin_id)
用 jax.ffi.ffi_lowering("cutile_launch", ...) 生成 custom_call，
  把上述所有属性 + operand_output_aliases 一并传入
```

`buffer_ids` 的语义很关键：它是一个「内核参数位置 → FFI buffer 列表下标」的映射。FFI 把所有 buffer 按 **输入在前、输出在后** 的顺序排成一张表（标量紧随其后），而内核参数的顺序可能与之不同（比如常量在中间、输出在前面），`buffer_ids` 就是这个重排表。

#### 4.3.3 源码精读

lowering 函数签名与状态准备：

[src/cuda/tile/jax/_jax.py:282-308](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/jax/_jax.py#L282-L308) 初始化 `constraints` / `buffer_ids` / `index_bitwidths` / `input_output_aliases` 等容器，并再次取出注解树。注意注释说明了 `index_bitwidths` 与 `buffer_ids` 平行，用于在 execute 期拒绝把超范围的 shape/stride 喂给 i32 内核。

role 主循环：

[src/cuda/tile/jax/_jax.py:310-342](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/jax/_jax.py#L310-L342) 逐 role 派生约束与 buffer id。重点：
- 第 311-313 行从 `annotations[pos].array.index_dtype` 推断该数组是否用 int64 索引——这正是 `ct.IndexedWithInt64` 注解（[src/cuda/tile/_stub.py:1084](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_stub.py#L1084)）流入 JAX 的接缝。
- 第 335-336 行对标量判断是否要 int64：`annotations[pos].scalar.dtype == ct.int64`，对应 `ct.ScalarInt64` 注解（[src/cuda/tile/_stub.py:1096](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_stub.py#L1096)）。
- 第 341 行：常量 role **不占 buffer 槽**，只 append 约束。

编译与属性装配：

[src/cuda/tile/jax/_jax.py:344-368](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/jax/_jax.py#L344-L368) 调 `compile_kernel_cached` 拿到 `(symbol, cubin_bytes, cubin_id)`，然后用 `jax.ffi.ffi_lowering("cutile_launch", ...)` 生成 custom_call，把 `cubin_code`、`cubin_id`、`function_name`、`buffer_ids`、`scalar_packed`、`index_bitwidths`、`num_inputs`、`num_outputs`、`grid_x/y/z` 全作为属性传入。注释解释了一个重要的 MLIR 特性：**MLIR bytecode 会对相同属性去重**，所以同一份 cubin 在图里被 N 处引用时，字节只序列化一次；C++ 侧凭 `cubin_id` 在进程级注册表里 load-or-find。

`compile_kernel` 与缓存：

[src/cuda/tile/jax/_jax.py:375-389](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/jax/_jax.py#L375-L389) 用约束构造 `KernelSignature(constraints, CallingConvention.cutile_python_v1())`，再调 `export_kernel` 编译出 cubin。**注意这里固定用 `cutile_python_v1` 调用约定**——这与「tuple 参数已被拒绝」是一致的：v1 不支持 tuple，而 JAX 集成恰好也不支持 tuple。

[src/cuda/tile/jax/_jax.py:398-413](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/jax/_jax.py#L398-L413) 的 `compile_kernel_cached` 用 `(id(kernel), constraints)` 作 key 做进程级缓存，并对 cubin 算 SHA-256 得 `cubin_id`。`_constraint_cache_key`（第 392-395 行）把 `ConstantConstraint` 归一为按值缓存（相同常量值命中缓存），其它约束按对象本身缓存。

数组约束合成：

[src/cuda/tile/jax/_jax.py:421-474](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/jax/_jax.py#L421-L474) 的 `_array_constraint` 把 JAX aval 翻译成 `ArrayConstraint`：算行优先 strides（XLA 默认布局）、对 i32 内核做 shape/stride 越界检查（第 441-449 行，越界则提示用 `ct.IndexedWithInt64`）、并填充对齐优化需要的 `stride_constant` / `stride_divisible_by` / `shape_divisible_by` / `base_addr_divisible_by`（256 字节对齐，承接 u6-l2 的整除性传播）。

标量打包：

[src/cuda/tile/jax/_jax.py:263-279](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/jax/_jax.py#L263-L279) 的 `pack_scalar` 把 Python 标量翻译成 `(DType, int64_bit_pattern)`：bool→int32 的 0/1；int→int32（默认）或 int64（`want_int64`，越界 int32 直接报错提示 `ct.ScalarInt64`）；float→float32 的 IEEE-754 位模式。这个位模式稍后会塞进 `scalar_packed` 数组交给 C++。

#### 4.3.4 代码实践

**实践目标**：观察同一份 cubin 在图中被去重。

1. 阅读 [test/test_jax.py:131-145](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/test/test_jax.py#L131-L145) 的 `test_multiple_calls`：它在一个图里调三次 `_scale`（相同 factor），然后断言 lower 出的 MLIR 文本里 `@cutile_launch` 出现 3 次。
2. **操作步骤**：把测试里的 `lower(...).as_text()` 打印出来，搜索 `cubin_code` 或 `cubin_id` 属性。
3. **需要观察的现象**：launch op 有 3 条，但 cubin 字节属性因 MLIR 去重只序列化一份（属性表里只出现一次定义）。
4. **预期结果**：3 次 `@cutile_launch`，但 cubin 字节只存一份——这正是 [src/cuda/tile/jax/_jax.py:346-350](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/jax/_jax.py#L346-L350) 注释描述的设计。

> 待本地验证：MLIR 文本表示对大字节串的打印策略因版本而异；若文本里看不到完整字节，可改用 `.as_string()` 的二进制或在 C++ 侧 `cutile_call_instantiate` 加日志统计 `cuLibraryLoadData` 的实际调用次数（应少于 launch 次数）。

#### 4.3.5 小练习与答案

**练习 1**：`buffer_ids` 这个数组解决的是什么问题？请举一个内核参数顺序与 FFI buffer 顺序不一致的例子。

**答案**：它解决「内核形参顺序 ≠ FFI buffer 表顺序」的重排问题。FFI buffer 表固定是「输入在前、输出在后、标量最后」，但内核形参可以任意交错。例如内核 `_interleaved(c: ct.Constant, x, y)`（[test/test_jax.py:50-53](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/test/test_jax.py#L50-L53)）第一个参数是常量（不占 buffer 槽）、第二三是输入与输出——`buffer_ids` 把内核位置 1、2 正确映射到 FFI 表的下标 0（输入）、1（输出）。

**练习 2**：为什么 `compile_kernel` 固定用 `cutile_python_v1`，而不是像普通 `launch` 那样按需升级到 v2？

**答案**：因为 JAX 集成在 4.1 已经拒绝了所有 tuple 参数（`NotImplementedError`），也只用了 v1 支持的约束集合，永远不会出现需要 v2 的特性。固定 v1 让 C++ 侧 `pack_buffer` 的打包格式（指针+shape+strides）保持简单确定。

### 4.4 C++ 执行器：cutile_call_handler 三阶段与 KernelEntry 注册表

#### 4.4.1 概念说明

前面三节都是「编译期 / trace 期」的事——生成了一条 `custom_call @cutile_launch` 进 MLIR 图。真正在 GPU 上跑内核，发生在 XLA 执行这条 custom call 时，由 C++ 函数 `cutile_call_handler` 接管。

XLA FFI 的执行模型是 **三阶段**（stage）的，同一个 handler 会被以不同 `stage` 多次调用：

- **INSTANTIATE**：图被实例化时调用一次。读属性（cubin、函数名、buffer_ids 等），**加载 cubin**（`cuLibraryLoadData` + `cuLibraryGetKernel`），构造一个 `CutileCallState` 挂到执行上下文。
- **INITIALIZE**：每次执行流初始化时调用。把 context-independent 的 kernel 落到当前 CUDA context（`cuKernelGetFunction`）。
- **EXECUTE**：每次实际执行时调用。从 XLA buffer 打包参数、`cuLaunchKernel`。

这套设计的核心动机是 **「加载一次、执行多次」**：cubin 加载（`cuLibraryLoadData`）是昂贵且全局有副作用的操作，绝不能每次执行都做。为此 C++ 侧维护了一个进程级的 **`KernelEntry` 注册表**，按 `cubin_id`（cubin 的 SHA-256）去重，并用引用计数管理生命周期——多个 `CutileCallState` 引用同一 cubin 时共享同一个 `KernelEntry`，最后一个释放时才 `cuLibraryUnload`。

#### 4.4.2 核心流程

handler 入口分派（[cext/xla_ffi.cpp:576-591](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/cext/xla_ffi.cpp#L576-L591)）：

```text
cutile_call_handler(cf):
  if 是 metadata 查询:    → populate_metadata（声明 traits 与 state_type_id）
  elif cf.stage == INSTANTIATE: → cutile_call_instantiate(cf)
  elif cf.stage == INITIALIZE:  → cutile_call_initialize(cf)
  elif cf.stage == EXECUTE:     → cutile_call_execute(cf)
```

INSTANTIATE 的注册表交互：

```text
cutile_call_instantiate:
  解析属性（cubin_id / cubin_code / function_name / buffer_ids / index_bitwidths /
           scalar_packed / num_inputs / num_outputs / grid_*）
  在 g_kernel_registry 里按 cubin_id 查找：
    命中且 slot 活跃  → 复用 KernelEntry，refcount++
    命中但 slot 已死  → load_kernel 重新加载，复活该 slot
    未命中            → load_kernel 新建，插入新 slot
  构造 CutileCallState（持有 entry +1 ref），set_state 到上下文
```

EXECUTE 的参数打包：

```text
cutile_call_execute:
  取 stream、取 state
  对 state->buffer_ids 的每个内核参数位置 k：
    bid = buffer_ids[k]
    若 bid < num_inputs            → 取输入 buffer，pack_buffer
    若 bid < num_inputs+num_outputs → 取输出 buffer，pack_buffer
    否则                            → 取标量 scalar_packed[...]，直接当一个 Word
  把每个 Word 的地址收集成指针数组 cuarg_pointers
  cuLaunchKernel(kernel_handle, grid, block=(1,1,1), stream, cuarg_pointers, nullptr)
```

`pack_buffer` 按 `cutile_python_v1` 把一个 XLA buffer 展开成 `1 + 2·ndim` 个 64 位 `Word`：一个设备指针、`ndim` 个 shape、`ndim` 个行优先 strides。对 i32 内核，预先拒绝超范围的 shape/stride 以免被截断读取。

> 关于 `block=(1,1,1)`：cuTile 内核表达的是 block 级并行（u2-l1），block 内部的线程/warp 划分由后端 `tileiras` 在 cubin 里决定，所以宿主启动时 block 维度恒为 1，并行度全部由 grid 表达。

#### 4.4.3 源码精读

`KernelEntry` 与注册表：

[cext/xla_ffi.cpp:86-99](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/cext/xla_ffi.cpp#L86-L99) 定义 `KernelEntry`，持有 `CUlibrary` / `CUkernel` 与 `refcount`，析构时 best-effort `cuLibraryUnload`。[cext/xla_ffi.cpp:101-110](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/cext/xla_ffi.cpp#L101-L110) 定义 `KernelRegistry`，是一个 `HashMap<CubinId, KernelEntry*>`，注释指出 slot 在最后一个引用销毁后会被置空（而非删除），未来同 `cubin_id` 的注册会复活该 slot。

`CubinId` 与哈希：

[cext/xla_ffi.cpp:32-51](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/cext/xla_ffi.cpp#L32-L51) 定义 32 字节的 `CubinId`（SHA-256 摘要）及其 `Hash`——注释说明 SHA-256 输出已均匀，取一个 64 位字做哈希足够。

`CutileCallState`（每个 instantiate 一份的执行状态）：

[cext/xla_ffi.cpp:116-150](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/cext/xla_ffi.cpp#L116-L150) 持有 driver api 指针、`KernelEntry*`（+1 ref）、cubin_id、grid 维度，以及三个并行数组 `buffer_ids` / `index_bitwidths` / `scalar_packed`。析构时递减 `entry->refcount`，归零则 `delete entry` 并把注册表 slot 置空。`kCutileCallStateTypeInfo`（第 155-161 行）注册了 deleter，让 XLA 在销毁状态时自动 `delete`。

INSTANTIATE：

[cext/xla_ffi.cpp:332-424](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/cext/xla_ffi.cpp#L332-L424) 是最长的函数。前半段（第 342-378 行）逐个解析 4.3 装配的那些属性（`cubin_id` 校验为 32 字节 U8 数组、`cubin_code`/`function_name` 为字符串、`num_inputs`/`num_outputs`/`grid_*` 为 S32 标量、`buffer_ids`/`index_bitwidths` 为 S32 数组且长度一致、`scalar_packed` 为 U64 数组）。第 380-398 行是 find-or-load 注册表交互（命中则 `++refcount` 复用，否则 `load_kernel` 新建/复活）。第 400-416 行构造并填充 `CutileCallState`，第 418 行 `set_state` 挂到上下文。

`load_kernel`：

[cext/xla_ffi.cpp:280-311](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/cext/xla_ffi.cpp#L280-L311) 用 `cuLibraryLoadData` 把 cubin 字节加载成 context-independent 的 `CUlibrary`，再用 `cuLibraryGetKernel` 按函数名取出 `CUkernel`，失败时给出带 cubin_id hex 的错误信息。注意第 296-299 行把非 NUL 结尾的 `ByteSpan` 函数名拷进临时缓冲再补 NUL。

INITIALIZE：

[cext/xla_ffi.cpp:427-440](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/cext/xla_ffi.cpp#L427-L440) 调 `cuKernelGetFunction` 确保 context-independent kernel 落到当前 CUDA context，为 execute 做准备。

EXECUTE 与 `pack_buffer`：

[cext/xla_ffi.cpp:503-569](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/cext/xla_ffi.cpp#L503-L569) 是 execute 主体。第 516-548 行按 `buffer_ids` 逐位置取 buffer 或标量，调用 `pack_buffer` 或直接把标量塞成 `Word`。第 551-555 行把每个 `Word` 的地址收集成指针数组（`cuLaunchKernel` 要求参数是「指针的数组」）。第 557-564 行发起 `cuLaunchKernel`，block 维度恒为 `(1,1,1)`。

[cext/xla_ffi.cpp:459-501](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/cext/xla_ffi.cpp#L459-L501) 的 `pack_buffer` 是 `cutile_python_v1` 调用约定的 C++ 复刻：push 设备指针、push `ndim` 个 shape（写满 i64 lane）、再 back-to-front 算行优先 strides 并 push。对 `index_bitwidth==32` 预先拒绝超范围的 shape/stride（第 465-473、491-496 行），避免内核读到截断的下标。

handler 入口：

[cext/xla_ffi.cpp:576-591](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/cext/xla_ffi.cpp#L576-L591) 是唯一的导出 handler，按 metadata 查询 / stage 三分支分派。注意 metadata 分支声明了 `XLA_FFI_HANDLER_TRAITS_COMMAND_BUFFER_COMPATIBLE`，这意味着 `cutile_launch` 可以进入 JAX 的 `jax.export` 序列化产物——这正是 [test/test_jax.py:326-369](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/test/test_jax.py#L326-L369) `test_export_run_in_new_process` 能把图导出、在新进程里重跑的前提。

#### 4.4.4 代码实践

**实践目标**：跟踪 cubin 注册表的「加载一次、执行多次」与跨进程独立生命周期。

1. 阅读 [test/test_jax.py:326-369](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/test/test_jax.py#L326-L369) `test_export_run_in_new_process`：它 `jax.export` 一个含 `cutile_call` 的图，序列化成 blob，在**全新子进程**里 `import cuda.tile.jax`（重新注册 FFI）后反序列化执行。
2. **操作步骤**：在该测试基础上，临时在 [cext/xla_ffi.cpp:286](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/cext/xla_ffi.cpp#L286) 的 `cuLibraryLoadData` 前加一条 `printf("load cubin_id=...\n")`，重编 cext 后跑测试。
3. **需要观察的现象**：子进程的 `cutile_call_instantiate` 仍会触发一次 `cuLibraryLoadData`（因为新进程的注册表是空的），但同一进程内多次执行同一图只加载一次。
4. **预期结果**：cubin 生命周期与 Python 进程绑定，`jax.export` 把 cubin 字节嵌进了序列化产物，所以新进程能凭嵌入的 cubin 重新加载——这验证了 4.3「cubin 作为 MLIR 属性」与 4.4「按 cubin_id 注册」两套机制是自洽的。

> 待本地验证：本实践需重编 C++ 扩展（`make -C build`，参见 u1-l3）。若不便改 C++，可改为在 Python 侧 monkeypatch `compile_kernel_cached`（参考 `test_cubin_id_round_trip`，[test/test_jax.py:254-276](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/test/test_jax.py#L254-L276)）统计编译次数。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `KernelRegistry` 的 slot 在引用归零时被置空（`item->value = nullptr`）而不是从 HashMap 里删除？

**答案**：为了让未来同一个 `cubin_id` 的注册能「复活」已有 slot，省去一次 HashMap 插入（见 [cext/xla_ffi.cpp:392-396](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/cext/xla_ffi.cpp#L392-L396) 的 `if (item) { item->value = entry; } else { insert }` 分支）。这典型地发生在「图被实例化→销毁→再实例化」的场景。

**练习 2**：`cuLaunchKernel` 的 block 参数为什么是 `(1,1,1)`？这是否意味着内核只在一个线程上跑？

**答案**：不是。cuTile 内核表达 block 级并行（u2-l1），block 内部的线程/warp 划分由后端 `tileiras` 在 cubin 内部决定（编译时已固定），宿主看到的「block 维度」恒为 1。真正的并行度由 grid 维度表达，每个 grid block 在硬件上映射到一个 CTA，CTA 内部的线程协作由 cubin 自己编排。

## 5. 综合实践

把本讲四个模块串起来：用 `cutile_call` 把一个 `vector_add` 内核接入最小 JAX 程序，验证正确性，再观察 tuple 参数被拒绝。

**实践目标**：端到端走一遍「定义内核 → cutile_call 接入 → jax.jit → 数值验证 → tuple 拒绝」。

**操作步骤**（示例代码，需在有 GPU + JAX 的环境运行）：

```python
# 示例代码：本讲综合实践，基于 test/test_jax.py 的模式编写
import jax
import jax.numpy as jnp
import numpy as np
import cuda.tile as ct
from cuda.tile.jax import cutile_call, OutputPlaceholder

# 1) 定义一个 vector_add 内核：z = x + y，逐 tile 处理
@ct.kernel
def vector_add(x, y, z, TILE: ct.Constant):
    bid = ct.bid(0)
    tx = ct.load(x, (bid,), (TILE,))
    ty = ct.load(y, (bid,), (TILE,))
    ct.store(z, (bid,), tx + ty)

# 2) 用 cutile_call 接入 JAX。
#    注意：TILE 是 ct.Constant，必须作为 static_argnums 传入，
#    否则 JAX 会把 int 当 0D 数组（动态值），无法烘焙进 cubin。
@jax.jit(static_argnums=(3,))
def add(x, y, tile):
    grid = (ct.cdiv(x.shape[0], tile),)
    ph = OutputPlaceholder(x.shape, x.dtype)   # 输出 buffer
    return cutile_call(grid, vector_add, (x, y, ph, tile))

x = jnp.arange(1024, dtype=jnp.float32)
y = jnp.ones(1024, dtype=jnp.float32) * 100
z = add(x, y, 128)          # TILE=128
print(z)                    # 预期：1024 个 100..1123 的值
np.testing.assert_array_equal(np.asarray(z), np.asarray(x) + np.asarray(y))

# 3) 观察 lower 出的 MLIR 里出现了 cutile_launch custom call
print(jax.jit(add, static_argnums=(3,)).lower(x, y, 128).as_text()
      .count("@cutile_launch"))   # 预期：1

# 4) 尝试 tuple 参数 —— 应被 cutile_call 显式拒绝
@ct.kernel
def bad_kernel(x, y, addends: tuple[ct.Constant[int], int]):
    bid = ct.bid(0)
    ct.store(y, bid, ct.load(x, bid, 1) + addends[0] + addends[1])

try:
    cutile_call((10,), bad_kernel,
                (x, OutputPlaceholder(x.shape, x.dtype), (3, 4)))
except NotImplementedError as e:
    print("按预期拒绝:", e)   # 预期：含 "tuple parameters are not supported ..."
```

**需要观察的现象**：

1. `add(x, y, 128)` 输出正确的逐元素和。
2. MLIR 文本里恰好出现 1 处 `@cutile_launch`。
3. 改 `tile=64` 再调一次 `add`：因为是 `ct.Constant`，会触发重新编译出一份新 cubin（待本地用 `CUDA_TILE_LOGS=1` 确认）。
4. `bad_kernel` 的调用抛 `NotImplementedError`，错误信息明确指向 tuple 参数。

**预期结果**：数值正确；MLIR 含 1 个 cutile_launch；常量 tile 改值触发重编译；tuple 参数被拒。如果某一步与预期不符，先核对内核参数注解与 `cutile_call` 实参个数、顺序是否一致。

> 待本地验证：本实践的运行结果依赖具体 GPU 与 JAX/cuTile 版本；在没有 tileiras 后端的环境会编译失败。Tuple 拒绝行为不依赖 GPU，可在纯 CPU + JAX 环境验证（参考 `test_tuple_parameter_rejected`，[test/test_jax.py:442-451](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/test/test_jax.py#L442-L451)）。

## 6. 本讲小结

- `cutile_call` 是 cuTile 内核接入 JAX 图的唯一入口，采用 **「一参数一角色」** 设计，role（`'i'/'o'/'io'/'c'/'s'`）由 **参数注解树 `parameter_annotations`** 的叶子节点决定，而非运行时类型；`OutputPlaceholder` 声明输出，`InputOutput` 实现 in-place 别名。
- JAX 集成 **只认叶子注解**，因此 tuple 参数（产生 `HomogeneousTupleNode`/`HeterogeneousTupleNode`）在 `cutile_call` 入口就被显式拒绝（`NotImplementedError`）；相应地，编译固定用 `cutile_python_v1` 调用约定。
- 注册分两步：`register_ffi` 把 C++ handler（经 `xla_ffi_py.cpp` 包成 `PyCapsule`）注册成 XLA FFI type+target `"cutile_launch"`；`register_primitive` 创建 `cutile_call_ffi_p` primitive 并挂 MLIR lowering 与抽象求值。`import cuda.tile.jax` 即自动完成。
- lowering（`_cutile_call_ffi_p_lower`）按 role 派生 `ParameterConstraint`、调 `compile_kernel_cached` 编译 cubin（按 `(kernel, constraints)` 缓存，cubin_id = SHA-256(cubin)），并把 cubin/buffer_ids/scalar_packed 等作为 MLIR 属性塞进 custom call；MLIR 会对相同 cubin 字节去重。
- C++ `cutile_call_handler` 是三阶段（INSTANTIATE/INITIALIZE/EXECUTE）统一入口：INSTANTIATE 解析属性并 find-or-load cubin 到进程级 `KernelEntry` 注册表（按 cubin_id 去重、引用计数管理、slot 可复活）；EXECUTE 按 `buffer_ids` 用 `pack_buffer` 把 XLA buffer 展开成 `cutile_python_v1` 的「指针+shape+strides」字面序列，再 `cuLaunchKernel`（block 恒为 1）。
- `cutile_launch` 声明为 command-buffer compatible，故可进入 `jax.export` 序列化产物并在新进程重跑——cubin 字节嵌在导出 blob 里，新进程凭 cubin_id 重新加载。

## 7. 下一步学习建议

- **自动调优**：本讲的 `compile_kernel_cached` 只做了进程级编译缓存，但没有搜索 tile 尺寸。下一步可学 u8-l3（`exhaustive_search`），看如何把 cutile_call 的编译与计时串成自动调优流程。
- **AOT 导出深读**：本讲只用到 `export_kernel` 的薄封装与 mangled symbol，建议结合 u8-l2（`KernelSignature` / `mangle_kernel_name` / `_validate_constraint_support`）理解符号名如何随约束变化。
- **JAX 多 GPU 分片**：[test/test_jax.py:403-427](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/test/test_jax.py#L403-L427) 的 `test_jit_sharding` 展示了 cutile_call 与 `jax.shard_map` / `NamedSharding` 配合做跨 GPU 分片，可作为进阶练习。
- **阅读建议**：想更透彻理解 `pack_buffer` 与 `cutile_python_v1` 的关系，可回看 u8-l1（`extract_cuda_args`）与 u8-l2（约束体系），它们是同一套调用约定在 JIT 直接启动、AOT 导出、JAX FFI 三个入口的三种复刻。
