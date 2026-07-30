# triton.language.extra.cann 与 libdevice

## 1. 本讲目标

本讲是「Ascend 语言扩展」单元的第一讲。学完后你应当能够：

- 说清楚 Ascend 语言扩展是**如何**被挂载成 `triton.language.extra.cann` 这个命名空间的（安装期机制，而非运行时魔法）。
- 读懂 `libdevice.py` 里每一个数学函数（如 `reciprocal`、`log1p`、`acos`）的写法，理解 `@core.extern` 装饰器与 `core.extern_elementwise` 的协作。
- 掌握 libdevice 函数在 **SIMD 模式**与 **SIMT 模式**下分别走哪条实现，以及为什么会这样设计。
- 认识 `__hmf_*` 这类符号最终是如何经 `libdevice.10.bc` 与 `--link-aicore-bitcode` 链接到内核里的。

> 前置衔接：本讲假设你已读过 u1-l2（代码分层原则）与 u1-l4（第一个 kernel）。本讲涉及的 `compile_mode`/SIMT 概念会在 u6-l1、u6-l2 深入，此处只需建立「同一份 Python 调用会按模式分派到不同后端实现」的直觉。

---

## 2. 前置知识

### 2.1 什么是 libdevice

在 CUDA 生态里，`libdevice` 是 NVIDIA 提供的一组**预编译好的、与硬件高度相关的数学函数位码库**（`libdevice.10.bc`），里面是 `__nv_expf`、`__nv_log2f` 这类用 LLVM bitcode 写好的函数。编译器在把 kernel 编成机器码时，把这些函数「链接」进来即可调用，无需在 IR 里手写泰勒展开。

昇腾（CANN）生态借用了同样的思路：`triton-ascend` 在 `third_party/ascend/backend/lib/libdevice.10.bc` 里提供了一组 `__hmf_*` 前缀的数学函数，对应昇腾 Vector/SIMT 计算单元的高效实现。本讲的 `libdevice.py` 就是这组函数的 **Python 声明层**——它本身不含实现，只是告诉编译器「请在 IR 里生成一个对 `__hmf_xxx` 的外部调用」。

### 2.2 extern（外部函数）是什么意思

在 Triton 里，绝大多数算子（`tl.add`、`tl.dot`）会被编译器翻译成具体的 MLIR dialect 算子。但 libdevice 函数不同——它对应一个**外部符号**：编译器不关心它的内部实现，只在 IR 里写一句「调用名为 `__hmf_recipf` 的函数」，等后续链接阶段把 `libdevice.10.bc` 合并进来时再解析这个符号。

这种「先声明、后链接」的模式，正是 `core.extern_elementwise` 与 `@core.extern` 装饰器要解决的核心问题。

### 2.3 SIMD 与 SIMT 两条计算路径（直觉版）

昇腾 NPU 有两种执行模型：

- **SIMD（向量）模式**：一条指令处理一个数据块，是默认且历史最久的路径。
- **SIMT 模式**：类似 GPU 的「每线程一个元素」模型，仅在 950（A5）等较新硬件上可用。

同一个数学函数（比如 `log1p`），在两种模式下要调用的硬件实现**符号不同**，甚至实现策略都不同。`libdevice.py` 的关键设计就是用 `triton_enable_libdevice_simt()` 这个开关，在编译期为同一个 Python 接口挑选正确的后端实现。

> 这两段是什么关系、何时启用，会在 u6-l1、u6-l2 详述。本讲只需记住：**函数签名不变，实现按模式分派**。

---

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `setup.py` | 安装/链接机制：把 `third_party/ascend/language/cann` 挂载成 `triton.language.extra.cann` |
| `third_party/ascend/language/cann/__init__.py` | cann 包入口：组装 `libdevice` + `extension`，并做一批别名映射 |
| `third_party/ascend/language/cann/libdevice.py` | 100+ 个数学函数的 Python 声明，每个含 SIMD/SIMT 分派 |
| `python/triton/language/core.py` | 定义 `@core.extern`、`@core.builtin`、`extern_elementwise`、`dispatch`——外部函数到 IR 的桥 |
| `third_party/ascend/backend/utils.py` | `triton_enable_libdevice_simt()`、`is_compile_on_910_95()`、`get_ascend_arch_from_env()`——模式开关 |
| `third_party/ascend/backend/compiler.py` | `get_libdevice()` 返回 `libdevice.10.bc`；编译期 `--link-aicore-bitcode` 链接 |
| `third_party/ascend/tutorials/07-extern-functions.py` | 可运行示例：调用 `libdevice.asin` |
| `third_party/ascend/unittest/pytest_ut/test_log1p.py` | 测试：调用 `libdevice.log1p` 并与 torch 对比 |

---

## 4. 核心概念与源码讲解

## 4.1 模块一：language.extra.cann 的挂载机制

### 4.1.1 概念说明

读者在 kernel 里写 `import triton.language.extra.cann.libdevice as libdevice` 时，这个 `cann` 子包在 `triton` 源码树里其实**并不存在**——它来自 `third_party/ascend/language/cann/`。这件事是**安装期**完成的，由 `setup.py` 用两种方式落地，对应两种安装形态：

- **whl 包安装**：通过 Python 的 entry points，把目录注册为 `triton.language.extra.cann`。
- **源码 editable 安装（`pip install -e .`）**：用符号链接（symlink）把目录链到 `python/triton/language/extra/cann`。

无论哪种，结果都是 `triton.language.extra.cann` 这个命名空间指向 ascend 的 `language/cann` 目录。这正是 u1-l2「目标亲和代码留在 `third_party/ascend`」分层原则的落地：core 完全不知道 cann 的存在，是安装机制把它「插」进来的。

### 4.1.2 核心流程

```text
third_party/ascend/language/cann/      ← 源头：ascend 亲和代码
        │
        │  setup.py 安装期：
        │   (a) whl  → entry_point("triton.language.extra.cann" → cann 目录)
        │   (b) editable → symlink(python/triton/language/extra/cann → cann 目录)
        ▼
triton.language.extra.cann             ← 用户 import 的目标
   ├── __init__.py    （组装 libdevice + extension，做别名映射）
   ├── libdevice.py   （100+ 数学函数声明）  ← 本讲重点
   └── extension/     （custom_op / sync / mem_ops 等，见 u7-l2、u7-l3）
```

### 4.1.3 源码精读

whl 安装时把 `language` 目录的每个子项注册为 `triton.language.extra.<名字>`（[setup.py:777-781](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/setup.py#L777-L781)）：此处对 `language_dir` 下每个条目（`cann` 就是其中之一）生成一个 entry point，把它映射到 `triton.language.extra.<x>`。

editable 模式则用 symlink 实现同样的映射（[setup.py:823-831](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/setup.py#L823-L831)）：在 `python/triton/language/extra/` 下为每个后端的 language 子目录建一个符号链接。`update_symlink` 保证重新安装时链接会被刷新。

挂载点建好后，`cann/__init__.py` 负责「组装」这个命名空间（[third_party/ascend/language/cann/__init__.py:21-32](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/language/cann/__init__.py#L21-L32)）：

```python
from triton.language import math
from triton.backends.ascend.utils import triton_enable_libdevice_simt

from . import libdevice
from . import extension

extension.parallel = extension.aux_ops.parallel
if not triton_enable_libdevice_simt():
    libdevice.atan2 = extension.math_ops.atan2
libdevice.isfinited = extension.math_ops.isfinited
libdevice.finitef = extension.math_ops.finitef
libdevice.flip = extension.flip
```

这段揭示一个关键事实：**`cann.libdevice` 命名空间是个「聚合层」，而非全部自研**。`atan2`、`isfinited`、`finitef`、`flip` 其实来自 `extension.math_ops` / `extension`，只是在导入时挂到了 `libdevice` 名下，让用户能像 CUDA 那样统一从 `libdevice` 取用。其中 `atan2` 的指向还**随模式切换**：非 SIMT 模式下用 `extension` 版，SIMT 模式下保留 `libdevice.atan2` 自身（见 [libdevice.py:1228](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/language/cann/libdevice.py#L1228) 的实现）。

紧接着一大批基础函数直接复用 core 的 `triton.language.math`（[third_party/ascend/language/cann/__init__.py:34-51](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/language/cann/__init__.py#L34-L51)）：

```python
libdevice.exp = math.exp
libdevice.log = math.log
libdevice.cos = math.cos
# ... 一大批
math.tanh = libdevice.tanh   # 反向：把 core math.tanh 指向 libdevice.tanh
```

这里有两点值得注意：

1. `libdevice.exp`/`log`/`cos` 等并非 `libdevice.py` 自己定义的，而是 core `math` 模块的实现——在 ascend 上 `tl.math.exp` 与 `libdevice.exp` 是同一个东西。
2. 第 51 行 `math.tanh = libdevice.tanh` 是**反向 patch**：它把 core 的 `math.tanh` 改写成 ascend 自研的 `libdevice.tanh`。这是一种运行时 monkey patch（u1-l2 提到过的「ascend 用 monkey patch 改 core 行为」），但改动代码体仍留在 ascend 子树。

最后 `__all__ = ["libdevice", "extension"]`（[第 53 行](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/language/cann/__init__.py#L53)）声明对外只暴露这两个名字，与官方文档路径 `triton.language.extra.cann.libdevice`、`triton.language.extra.cann.extension` 一致。

### 4.1.4 代码实践

1. **实践目标**：验证 `triton.language.extra.cann` 确实来自 `third_party/ascend/language/cann`，而非 core。
2. **操作步骤**：
   - 在已安装 `triton-ascend` 的环境里执行 `python -c "import triton.language.extra.cann.libdevice as m; print(m.__file__)"`。
3. **需要观察的现象**：
   - whl 安装时，路径指向 site-packages 里的 `triton/language/extra/cann/libdevice.py`；
   - editable 安装时，路径直接指向仓库内的 `third_party/ascend/language/cann/libdevice.py`（symlink 解析结果）。
4. **预期结果**：打印出的路径都能追溯到 `third_party/ascend/language/cann/`。
5. 若环境不可用，标注「待本地验证」——这是一条纯安装期验证，不影响后续阅读。

### 4.1.5 小练习与答案

**练习 1**：为什么 `cann/__init__.py` 要把 `libdevice.exp` 指向 `math.exp`，而不是自己在 `libdevice.py` 里再写一个 `exp`？

**参考答案**：`math.exp` 是 core 提供的目标无关实现，ascend 直接复用即可避免重复维护；同时让 `libdevice.exp` 与 `tl.math.exp` 行为一致，减少用户心智负担。只有 ascend 需要**特化**的函数（如 `reciprocal`、`tanh`）才在 `libdevice.py` 里专门实现。

**练习 2**：`math.tanh = libdevice.tanh` 这行会在什么时刻生效？是否需要重新 import？

**参考答案**：在 `import triton.language.extra.cann`（或其子模块）时立即生效，因为它直接修改了已加载的 `triton.language.math` 模块对象的 `tanh` 属性。无需重新 import `math`，因为 Python 模块是单例，属性被原地替换。

---

## 4.2 模块二：@core.extern 装饰器与 extern_elementwise

### 4.2.1 概念说明

`libdevice.py` 里的函数几乎都用 `@core.extern` 或 `@core.builtin` 装饰。这两个装饰器在 core 里其实是**同一个东西**——`extern` 直接返回 `builtin(fn)`。它们的作用是把一个普通 Python 函数登记为「Triton 内建函数」，使其能在 `@triton.jit` kernel 内被识别，并自动获得一个 `_semantic`（语义上下文）参数。

真正干活的是 `core.extern_elementwise`：它接收「函数名、库路径、参数列表、按 dtype 分派的符号表」，把一次 Python 调用翻译成 IR 里的一条 `extern_elementwise` 算子，该算子引用 `libdevice.10.bc` 里的某个 `__hmf_*` 符号。

### 4.2.2 核心流程

```text
libdevice.reciprocal(x)                         # 用户在 kernel 内调用
        │  @core.extern 已把它登记为 builtin，
        │  Triton 在 JIT 编译时注入 _semantic
        ▼
core.extern_elementwise(lib_name, lib_path,     # core.py:3381
                        [x], {dtype: (symbol, ret)}, is_pure, _semantic)
        │  1) 对每个 arg 做 to_tensor、收集 arg_types
        │  2) 处理广播、确定 ret_type
        ▼
dispatch(func, ..., arg_type_symbol_dict, ...)  # core.py:3340
        │  用 arg_types 在符号表里查到 symbol
        ▼
builder.create_extern_elementwise(              # 生成 IR op
     lib_name, lib_path, symbol, arg_list, ret_type, is_pure)
        │
        ▼
ttir/IR:  %y = extern_elementwise @libdevice/__hmf_recipf(%x) : (tensor<...f32>) -> tensor<...f32>
```

其中符号表的「按 dtype 查表」是关键：同一个数学函数，输入是 `fp32` 与 `fp16` 时可能对应**不同的 `__hmf_*` 符号**（见 4.3 节）。

### 4.2.3 源码精读

先看装饰器本身。`extern` 就是 `builtin` 的别名（[python/triton/language/core.py:3433-3435](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/python/triton/language/core.py#L3433-L3435)）：

```python
def extern(fn):
    """A decorator for external functions."""
    return builtin(fn)
```

`builtin` 包装器确保函数只能在 JIT 上下文里被调用——它要求调用方传入非空的 `_semantic`，而这个参数是 `@triton.jit` 编译时自动注入的（[python/triton/language/core.py:34-45](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/python/triton/language/core.py#L34-L45)）：

```python
def builtin(fn: T) -> T:
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if "_semantic" not in kwargs or kwargs["_semantic"] is None:
            raise ValueError("Did you forget to add @triton.jit ? ...")
        return fn(*args, **kwargs)
    setattr(wrapper, TRITON_BUILTIN, True)
```

这就解释了为什么 libdevice 函数签名里都带一个 `_semantic=None`：它在普通 Python 调用时是 `None`（触发报错），在 JIT 编译时由 Triton 填入真实语义对象。

接着是 `extern_elementwise` 的预处理（[python/triton/language/core.py:3381-3420](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/python/triton/language/core.py#L3381-L3420)，节选关键逻辑）：

```python
def extern_elementwise(lib_name, lib_path, args, arg_type_symbol_dict, is_pure, _semantic=None):
    dispatch_args = args.copy()
    arg_types = []
    for i in builtins.range(len(dispatch_args)):
        dispatch_args[i] = _semantic.to_tensor(dispatch_args[i])   # 标量也提升为 tensor
        arg_types.append(dispatch_args[i].dtype)
        ...
    ret_type = arg_type_symbol_dict[arg_types][1]                  # 由符号表给出返回类型
    ...
    func = _semantic.builder.create_extern_elementwise             # 拿到 IR builder 方法
    return dispatch(func, lib_name, lib_path, dispatch_args, arg_type_symbol_dict, ret_type, is_pure, _semantic)
```

它在 `to_tensor`（把 Python 标量/常量提升为 IR tensor）、广播形状对齐后，把真正的「选符号 + 建 IR op」交给 `dispatch`。`dispatch` 用输入 dtype 元组在 `arg_type_symbol_dict` 里查到对应的 `symbol`，再调用 builder（[python/triton/language/core.py:3371-3377](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/python/triton/language/core.py#L3371-L3377)）：

```python
    if arg_types not in arg_type_symbol_dict:
        raise ValueError(f"input arg type does not match. ...")
    else:
        symbol = arg_type_symbol_dict[arg_types][0]
        builder = _semantic.builder
        return tensor(func(lib_name, lib_path, symbol, arg_list, ret_type.to_ir(builder), is_pure), ret_type)
```

注意第 3375 行：`symbol = arg_type_symbol_dict[arg_types][0]`——符号表每个条目是 `(symbol, ret_type)` 二元组，`[0]` 取符号名，`[1]` 取返回类型。如果输入 dtype 组合不在表里，就会抛 `ValueError`，这正是 libdevice 函数「只支持特定 dtype」的根本原因。

### 4.2.4 代码实践

1. **实践目标**：通过阅读源码，确认 `libdevice.reciprocal` 到 `builder.create_extern_elementwise` 的整条调用链。
2. **操作步骤**：
   - 打开 [libdevice.py:28-44](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/language/cann/libdevice.py#L28-L44)，确认 `reciprocal` 调用了 `core.extern_elementwise`。
   - 跳到 [core.py:3381](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/python/triton/language/core.py#L3381)，确认它最后调 `dispatch`。
   - 跳到 [core.py:3375](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/python/triton/language/core.py#L3375)，确认它用 dtype 查表取 `symbol` 并调 `builder.create_extern_elementwise`。
3. **需要观察的现象**：三段代码的参数如何层层透传（`arg_type_symbol_dict` 一路传到 `dispatch`）。
4. **预期结果**：能在脑中画出 4.2.2 节那张调用链图。
5. 这是一条「源码阅读型实践」，无需运行设备。

### 4.2.5 小练习与答案

**练习 1**：如果调用 `libdevice.reciprocal(x)` 时 `x` 是 `bf16`，但 `reciprocal` 的符号表里没有 `(bf16,)` 条目，会发生什么？

**参考答案**：`dispatch` 在 [core.py:3371](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/python/triton/language/core.py#L3371) 判断 `arg_types not in arg_type_symbol_dict` 成立，抛出 `ValueError("input arg type does not match...")`，编译失败。这要求用户先 `tl.cast` 到受支持的 `fp32`/`fp16`。

**练习 2**：`@core.extern` 和 `@core.builtin` 有什么区别？

**参考答案**：没有区别。`extern(fn)` 的实现就是 `return builtin(fn)`（[core.py:3434-3435](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/python/triton/language/core.py#L3434-L3435)）。二者都给函数打上 `TRITON_BUILTIN` 标记并要求 `_semantic`，仅是语义上的命名区分：`extern` 暗示「外部库函数」，`builtin` 更通用。在 `libdevice.py` 里两种装饰器都有使用。

---

## 4.3 模块三：libdevice.py 的 SIMD/SIMT 双实现

### 4.3.1 概念说明

`libdevice.py` 里有 100 多个数学函数，初看杂乱，但按「SIMD/SIMT 如何分派」可归为三类清晰的模式。决定分派的是 `triton_enable_libdevice_simt()` 这个开关（[third_party/ascend/backend/utils.py:589-591](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/utils.py#L589-L591)）：

```python
def triton_enable_libdevice_simt():
    enable_libdevice_simt = os.getenv("TRITON_ENABLE_LIBDEVICE_SIMT", False)
    return enable_libdevice_simt and is_compile_on_910_95()
```

它返回真需要**同时**满足两个条件：

1. 环境变量 `TRITON_ENABLE_LIBDEVICE_SIMT` 被设置（非空）。
2. `is_compile_on_910_95()` 为真——即当前硬件是 950（A5）系列（[third_party/ascend/backend/utils.py:39-47](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/utils.py#L39-L47)，通过 `acl.get_soc_name()` 探测）。

> **关键约束**：在非 950 硬件上，无论是否设置环境变量，`triton_enable_libdevice_simt()` 恒为 `False`。因此 SIMT libdevice 路径只在 950 上有意义。

### 4.3.2 核心流程：三种实现模式

\[ \text{libdevice 函数} = \begin{cases} \text{SIMT: 调用 } \_\_hmf\_*\_fp32 & \text{若 } \texttt{triton\_enable\_libdevice\_simt()} \\ \text{SIMD: 见下文三种模式} & \text{否则} \end{cases} \]

SIMD 分支（`else`）又分三种写法：

| 模式 | 代表函数 | SIMD 分支做什么 | SIMT 分支做什么 |
| --- | --- | --- | --- |
| **A. 双 extern（符号不同）** | `reciprocal`、`log1p`、`relu`、`tan` | extern 调 `__hmf_*f`(fp32) + `__hmf_*Dh`(fp16) | extern 调 `__hmf_*_fp32`（仅 fp32） |
| **B. extern + 软件实现** | `acos`、`sinh`、`cosh`、`erfinv`、`gamma`、`rint` | 用 `builder.create_*` 基础算子 + 多项式**合成** | extern 调 `__hmf_*_fp32` |
| **C. 仅 SIMT** | `logb`、`scalbn`、`clz`、`abs`、`erf` | `static_assert(False)` **编译报错** | extern 调 `__hmf_*_fp32` |

模式 A 的差异是「符号不同」；模式 B 的差异是「SIMD 根本不调 libdevice，而是用 IR 原语自己算」；模式 C 则是「SIMD 暂不支持」。下面分别精读。

### 4.3.3 源码精读

**模式 A：reciprocal**（[libdevice.py:28-44](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/language/cann/libdevice.py#L28-L44)）

计算 \( y = 1/x \)：

```python
@core.extern
def reciprocal(arg0, _semantic=None):
    if triton_enable_libdevice_simt():
        return core.extern_elementwise("", "", [arg0], {
            (core.dtype("fp32"), ): ("__hmf_reciprocal_fp32", core.dtype("fp32")),
        }, is_pure=True, _semantic=_semantic)
    return core.extern_elementwise(
        "", "", [arg0], {
            (core.dtype("fp32"), ): ("__hmf_recipf", core.dtype("fp32")),
            (core.dtype("fp16"), ): ("__hmf_recipDh", core.dtype("fp16")),
        }, is_pure=True, _semantic=_semantic)
```

要点：

- SIMT 分支只有 `fp32` 一个符号 `__hmf_reciprocal_fp32`。
- SIMD 分支有 `fp32`→`__hmf_recipf`、`fp16`→`__hmf_recipDh` 两个符号。后缀 `f` 表示 fp32，`Dh` 表示 fp16（half），这是 CANN libdevice 的命名约定。
- `lib_name`/`lib_path` 传空串 `""`——这两个参数在 ascend 实现里并不关键，符号本身已足够（链接由 `bisheng_options` 默认带上 libdevice，见 4.4 节）。
- `is_pure=True` 表示该函数无副作用、可被 CSE/内联等优化自由处理。

**模式 B：acos**（[libdevice.py:1296-1357](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/language/cann/libdevice.py#L1296-L1357)）

```python
@core.builtin
@math._check_dtype(dtypes=["bf16", "fp16", "fp32"])
@math._add_math_1arg_docstr("acos")
def acos(arg0: core.tensor, _semantic=None):
    if triton_enable_libdevice_simt():
        ...
        return core.extern_elementwise("", "", [arg0], {
            (core.dtype("fp16"), ): ("__hmf_acos_fp16", core.dtype("fp16")),
            (core.dtype("fp32"), ): ("__hmf_acos_fp32", core.dtype("fp32")),
        }, is_pure=True, _semantic=_semantic)
    else:
        # 用多项式 + sqrt/where 合成 acos
        poly = _semantic.add(1.0, _semantic.mul(0.166667, arg0_2, True), True)
        ...
        acos_center = _semantic.sub(pi_half, _semantic.mul(arg0, poly, True), True)
        ...
        return res_mid_boundary
```

SIMT 分支照例 extern 调 `__hmf_acos_*`；但 **SIMD 分支不再调 libdevice**，而是用 `_semantic.mul/add/sub`、`math.sqrt`、`_semantic.where` 等基础 IR 算子，把 \( \arccos(x) \) 拆成分段多项式近似（中心区用幂级数，边缘区用 `2·arctan(√((1-|x|)/(1+|x|)))` 变换）。这么做的原因是：SIMD 路径下 `__hmf_acos` 符号在 `libdevice.10.bc` 里可能不可用或性能不佳，于是退化为「用通用算子自己算」。

> 这类函数还常带 `@math._check_dtype` 装饰器，在进入函数体前就校验 dtype，给出更友好的报错。

**模式 C：logb**（[libdevice.py:191-198](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/language/cann/libdevice.py#L191-L198)）

```python
@core.extern
def logb(arg0, _semantic=None):
    if not triton_enable_libdevice_simt():
        core.static_print("livdevice.logb for simd is unspported for now.")
        core.static_assert(False)
    return core.extern_elementwise("", "", [arg0], {
        (core.dtype("fp32"), ): ("__hmf_logb_fp32", core.dtype("fp32")),
    }, is_pure=True, _semantic=_semantic)
```

`core.static_assert(False)` 是**编译期断言**：一旦在 SIMD 模式下调用 `logb`，编译就会在这里失败并打印提示。这类函数目前只在 SIMT（950）下可用。

**特殊的 arch 条件：rint**（[libdevice.py:2375-2390](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/language/cann/libdevice.py#L2375-L2390)）

```python
if get_ascend_arch_from_env() == "Ascend910_9589":
    # 有硬件支持的版本：直接 extern __hmf_rint
    @core.extern
    def rint(arg0, _semantic=None):
        return core.extern_elementwise("", "", [arg0], {...}, ...)
else:
    # 其它硬件：SIMT 用 extern，SIMD 用软件实现
    @core.builtin
    def rint(arg0, _semantic=None):
        ...
```

这里在**模块导入时**用 `get_ascend_arch_from_env()`（读 `TRITON_ASCEND_ARCH` 环境变量，[utils.py:526-557](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/utils.py#L526-L557)）做条件定义：`910_9589` 这个特定型号有硬件 `rint`，直接 extern；其它型号走「SIMT extern + SIMD 软件」的常规套路。这是一种比 `triton_enable_libdevice_simt()` 更细粒度的、按 arch 的分派。

### 4.3.4 代码实践

1. **实践目标**：在 kernel 里调用 `libdevice.log1p` 并与 torch 对比，验证正确性。
2. **操作步骤**：
   - 参考 [tutorials/07-extern-functions.py](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/tutorials/07-extern-functions.py) 的写法（该文件第 31 行 `import ... cann.libdevice as libdevice`，第 48 行 `libdevice.asin(x)`），把内核改成 `y = libdevice.log1p(x)`，即计算 \( y = \ln(1+x) \)。
   - 或直接阅读现成测试 [test_log1p.py:34-43](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/unittest/pytest_ut/test_log1p.py#L34-L43)，其内核 `tmp2 = tmp0 + libdevice.log1p(tmp1)` 与 torch 的 `x0 + torch.log1p(x1)` 对比。
3. **需要观察的现象**：
   - 默认（SIMD/`unstructured_in_simt`）模式下，开启 `TRITON_DEBUG=1` dump IR，应能看到 `log1p` 被翻译成对 `__hmf_log1pf`（fp32）/`__hmf_log1pDh`（fp16）的外部调用。
   - 对比 `log1p` 与 `acos` 的 dump：`log1p`（模式 A）IR 里是单个 extern 调用；`acos`（模式 B）SIMD 模式下 IR 里会出现一串 `mul/add/sqrt/select`，而**没有** `__hmf_acos` 调用。
4. **预期结果**：数值上与 `torch.log1p` 一致（fp32 误差在 `rtol/atol=1e-4` 内）。
5. **关于 SIMT 分支**：要观察 SIMT 符号 `__hmf_log1p_fp32`，需在 **950 硬件**上设置 `TRITON_ENABLE_LIBDEVICE_SIMT=1` 并以 `force_simt_only=True` 启动（参见 [libdevice_developer_guide.md](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/docs/en/libdevice/libdevice_developer_guide.md) 顶部示例）。非 950 硬件无法触发，此部分**待本地验证**。

### 4.3.5 小练习与答案

**练习 1**：`libdevice.reciprocal` 在 SIMD 模式下支持 `fp16`，但 SIMT 模式下只支持 `fp32`。如果你在 950 上以 `force_simt_only=True` 运行一个对 `fp16` 张量调用 `reciprocal` 的 kernel，会发生什么？

**参考答案**：SIMT 分支的符号表只有 `(fp32,)` 条目（[libdevice.py:37-39](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/language/cann/libdevice.py#L37-L39)）。`dispatch`（[core.py:3371](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/python/triton/language/core.py#L3371)）发现 `(fp16,)` 不在表里，抛 `ValueError`。需先 `tl.cast(x, tl.float32)`。

**练习 2**：为什么 `acos` 在 SIMD 模式下不直接 extern `__hmf_acos`，而要写一大段多项式？

**参考答案**：SIMD 路径下 `libdevice.10.bc` 不一定提供 `__hmf_acos` 的可用实现（或精度/性能不达标），于是 ascend 选择用通用的 `mul/add/sqrt/where` 算子把 \( \arccos \) 分段多项式合成出来——这些通用算子在 SIMD Vector 单元上有高效支持。这是「能用通用算子就不依赖专用符号」的工程取舍。

**练习 3**：`triton_enable_libdevice_simt()` 在 910B（非 950）硬件上设置 `TRITON_ENABLE_LIBDEVICE_SIMT=1` 后返回什么？

**参考答案**：返回 `False`。因为该函数是 `enable_libdevice_simt and is_compile_on_910_95()`（[utils.py:590-591](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/utils.py#L590-L591)），`is_compile_on_910_95()` 在 910B 上为 `False`，与运算结果为 `False`。SIMT libdevice 路径在 910B 上无法启用。

---

## 4.4 模块四：libdevice.10.bc 与 --link-aicore-bitcode 链接

### 4.4.1 概念说明

前面所有 `__hmf_*` 符号，在 IR 里只是「外部声明」。要让它真正变成可执行代码，必须在编译末段把 `libdevice.10.bc` 这个 bitcode 库**链接**进内核。这件事在 ascend 后端由 `compiler.py` 的 `ttir_to_npubin`/`ttadapter_to_npubin` 阶段完成，借助于 BiSheng 编译器的 `--link-aicore-bitcode` 选项。

### 4.4.2 核心流程

```text
IR 里： %y = extern_elementwise @__hmf_recipf(%x)
        │  （符号未定义，只是引用）
        ▼
compiler.py 组装 BiSheng 命令行：
   bishengir-compile <input.ttadapter>
       ... 
       -cce-link-aicore-ll-module <libdevice.10.bc>   ← 来自 NPUOptions.bisheng_options（默认）
       --link-aicore-bitcode=<xxx.bc>                 ← 来自 IR 里解析出的 bitcode 声明
        ▼
BiSheng 把 libdevice.10.bc 内联进来，解析 __hmf_recipf → 昇腾指令
```

### 4.4.3 源码精读

`get_libdevice()` 返回随包发布的 bitcode 路径（[third_party/ascend/backend/compiler.py:983-985](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/compiler.py#L983-L985)）：

```python
def get_libdevice():
    current = os.path.dirname(__file__)
    return os.path.join(current, "lib/libdevice.10.bc")
```

即 `third_party/ascend/backend/lib/libdevice.10.bc`。它**默认**就被链接——`NPUOptions.bisheng_options` 把它写进了每一次 BiSheng 编译（[compiler.py:1026-1027](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/compiler.py#L1026-L1027)）：

```python
    extern_libs: dict = None
    bisheng_options: str = "-cce-link-aicore-ll-module " + get_libdevice()
```

也就是说， ascend 上每次编译内核都会带上 `-cce-link-aicore-ll-module <libdevice.10.bc>`，这正是 `__hmf_*` 符号能被解析的根本原因——无需用户手动指定。

除默认外，还有两条额外的 bitcode 链接通路。其一是从 Linalg IR 文本里解析出的 `bitcode = "xxx.bc"` 声明（[compiler.py:914-918](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/compiler.py#L914-L918)），逐个转成 `--link-aicore-bitcode=xxx.bc`：

```python
bitcodes = metadata["bitcodes"]
if bitcodes is not None:
    for bitcode in bitcodes:
        _compile_option_list += [f"--link-aicore-bitcode={bitcode}"]
```

其二是 `TRITON_ENABLE_LIBDEVICE` 环境变量，显式强制再链接一次 `libdevice.10.bc`（[compiler.py:920-922](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/compiler.py#L920-L922)）：

```python
enable_libdevice = os.getenv("TRITON_ENABLE_LIBDEVICE", False)
if enable_libdevice:
    _compile_option_list += [f"--link-aicore-bitcode={get_libdevice()}"]
```

> 注意区分两个名字相近的环境变量：`TRITON_ENABLE_LIBDEVICE`（本节，控制链接 bitcode）与 `TRITON_ENABLE_LIBDEVICE_SIMT`（4.3 节，控制 Python 层选 SIMT 符号）。前者作用于编译链接阶段，后者作用于 libdevice 函数的分派，二者配合使用才能完整跑通 SIMT libdevice 路径。

### 4.4.4 代码实践

1. **实践目标**：确认 `libdevice.10.bc` 存在，并理解它默认就被链接。
2. **操作步骤**：
   - 在仓库内查找该文件：`ls third_party/ascend/backend/lib/libdevice.10.bc`。
   - 阅读 [compiler.py:1027](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/compiler.py#L1027)，确认 `NPUOptions.bisheng_options` 默认含 `-cce-link-aicore-ll-module <libdevice.10.bc>`。
3. **需要观察的现象**：`libdevice.10.bc` 是一个 LLVM bitcode 文件（二进制）。
4. **预期结果**：文件存在；任何 ascend kernel 编译都默认链接它，所以 4.3 节里的 `__hmf_*` extern 调用总能被解析。
5. 若想观察 BiSheng 实际命令行，可在编译时设 `TRITON_DEBUG=1`，编译器会打印 `[DEBUG] cmd_list:`（[compiler.py:948](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/compiler.py#L948)），其中可见 `-cce-link-aicore-ll-module .../libdevice.10.bc`。**待本地验证**。

### 4.4.5 小练习与答案

**练习 1**：如果删掉 `NPUOptions.bisheng_options` 里的 `-cce-link-aicore-ll-module libdevice.10.bc`，调用 `libdevice.reciprocal` 的 kernel 会在哪个阶段失败？

**参考答案**：在 BiSheng 链接阶段失败——IR 里 `__hmf_recipf` 是未定义外部符号，没有 bitcode 提供实现，BiSheng 会报符号未解析（undefined symbol）。编译期（TTIR→Linalg）不会报错，因为那时它只是 extern 声明。

**练习 2**：`TRITON_ENABLE_LIBDEVICE` 与 `TRITON_ENABLE_LIBDEVICE_SIMT` 各自作用在哪个阶段？

**参考答案**：`TRITON_ENABLE_LIBDEVICE` 作用在**编译链接阶段**，额外追加 `--link-aicore-bitcode=libdevice.10.bc`（[compiler.py:920-922](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/compiler.py#L920-L922)）；`TRITON_ENABLE_LIBDEVICE_SIMT` 作用在 **Python 层 libdevice 函数分派**，决定 `reciprocal` 等函数选用 `__hmf_*_fp32`（SIMT）还是 `__hmf_*f`（SIMD）符号（[libdevice.py:36](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/language/cann/libdevice.py#L36)）。

---

## 5. 综合实践

把本讲四个模块串起来，完成一次「从调用到链接」的完整追踪：

**任务**：写一个最小的 `reciprocal` kernel，调用 `cann.libdevice.reciprocal`，验证数值，并追踪它从 Python 到 bitcode 链接的完整链路。

```python
# 示例代码（基于 tutorials/07-extern-functions.py 改写）
import torch
import torch_npu
import triton
import triton.language as tl
import triton.language.extra.cann.libdevice as libdevice   # 模块一：经 setup.py 挂载

@triton.jit
def recip_kernel(x_ptr, y_ptr, n_elements, BLOCK_SIZE: tl.constexpr):
    pid = tl.program_id(axis=0)
    offsets = pid * BLOCK_SIZE + tl.arange(0, BLOCK_SIZE)
    mask = offsets < n_elements
    x = tl.load(x_ptr + offsets, mask=mask)
    y = libdevice.reciprocal(x)          # 模块三：默认 SIMD 分支 → __hmf_recipf
    tl.store(y_ptr + offsets, y, mask=mask)

x = torch.rand(4096, device="npu") + 0.5
out = torch.empty_like(x)
recip_kernel[(triton.cdiv(x.numel(), 1024),)](x, out, x.numel(), BLOCK_SIZE=1024)
torch.testing.assert_close(out, 1.0 / x, rtol=1e-4, atol=1e-4)
```

完成后，请按顺序回答 / 验证：

1. **挂载**（模块一）：`libdevice.__file__` 指向哪里？它是否来自 `third_party/ascend/language/cann/`？
2. **桥接**（模块二）：说出 `libdevice.reciprocal(x)` → `core.extern_elementwise` → `dispatch` → `builder.create_extern_elementwise` 的调用链，并指出 dtype 查表发生在 `dispatch` 的哪一行。
3. **分派**（模块三）：默认模式下，IR 里应出现哪个符号（`__hmf_recipf` 还是 `__hmf_reciprocal_fp32`）？为什么？`acos` 在同样模式下为什么看不到 `__hmf_acos` 调用？
4. **链接**（模块四）：这个符号由谁解析？请指出 `get_libdevice()` 返回的路径与 `NPUOptions.bisheng_options` 中的默认链接选项。

> 完成后建议：开启 `TRITON_DEBUG=1` dump 出 IR，亲自确认第 3 点的符号名；这一步需要可运行的 NPU 环境，**待本地验证**。

---

## 6. 本讲小结

- `triton.language.extra.cann` 是安装期由 `setup.py`（entry points / symlink）从 `third_party/ascend/language/cann` 挂载进 core 的，core 自身不知道它的存在——这是 u1-l2 分层原则的落地。
- `cann/__init__.py` 是个**聚合层**：`libdevice` 既含 ascend 自研函数，也复用 core `triton.language.math`（`exp`/`log`/`cos` 等），还把 `extension.math_ops` 的部分函数挂到 `libdevice` 名下。
- libdevice 函数经 `@core.extern`（= `@core.builtin`）登记，由 `core.extern_elementwise` + `dispatch` 翻译成 IR 的 `extern_elementwise` 算子；符号按输入 dtype 从 `arg_type_symbol_dict` 查表选出。
- `libdevice.py` 用 `triton_enable_libdevice_simt()`（仅在 950 生效）在编译期分派 SIMT/SIMD 实现，形成三种模式：双 extern（`reciprocal`）、extern+软件实现（`acos`）、仅 SIMT（`logb`）。
- 所有 `__hmf_*` 外部符号最终由 `libdevice.10.bc` 经 `NPUOptions.bisheng_options` 的 `-cce-link-aicore-ll-module` 默认链接解析；`TRITON_ENABLE_LIBDEVICE` 可追加 `--link-aicore-bitcode`。

---

## 7. 下一步学习建议

- **u7-l2（custom_op 框架）**：本讲只覆盖了 `cann` 包里的 `libdevice`（纯数学函数）。`cann/extension` 下还有更强大的 `tl.custom_op` 自定义算子注册机制，是写硬件亲和算子的主流入口，建议接着读。
- **u7-l3（内置访存类自定义算子）**：`extension/mem_ops.py` 的 `index_select`/`gather_out_to_ub` 等访存算子，与 libdevice 的「纯计算 extern」形成对照。
- **u6-l1 / u6-l2（SIMD/SIMT 双路径）**：本讲多次提到 `triton_enable_libdevice_simt()` 与 950，想彻底理解 SIMT 何时启用、`force_simt_only` 如何跳过 Linalg，请进入第 6 单元。
- **源码延伸**：通读 [libdevice.py](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/language/cann/libdevice.py) 的全部函数，按本讲的「三种模式」分类表自行归类，能快速建立全局印象；配套文档见 [libdevice_developer_guide.md](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/docs/en/libdevice/libdevice_developer_guide.md)。
