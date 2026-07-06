# AOT 导出与内核签名/名称修饰

## 1. 本讲目标

本讲承接 u8-l1（launch 与调用约定）和 u5-l2（compile_tile 流水线），回答一个问题：

> **如果不走 JIT，而是想把一个内核「预先编译好」分发出去，该怎么做？**

cuTile 的回答是 **AOT（Ahead-Of-Time，提前编译）导出**：把内核编译成一份 cubin（或 TileIR 字节码）写到文件，运行时由任意宿主代码（甚至非 Python 代码）通过 CUDA Driver API 直接启动。

学完本讲，你应当能够：

1. 用 `export_kernel` 把一个 `@ct.kernel` 内核 AOT 导出为 cubin 或 tileir 字节码，并说清楚它与 JIT 的差异。
2. 用 `KernelSignature` + 五种 `ParameterConstraint`（`ScalarConstraint`/`ArrayConstraint`/`ListConstraint`/`TupleConstraint`/`ConstantConstraint`）精确描述一组内核参数的类型与编译期假设，掌握 `shape_constant` 与 u3-l7 引入的 `static_shape_dims` 的对应关系。
3. 解释 `_validate_constraint_support` 如何按 `calling_convention.version` 把 tuple 参数与静态形状门控到 `cutile_python_v2`。
4. 读懂 `mangle_kernel_name` 产出的符号名（含 tuple 的 `T` 前缀、静态形状的 `s` 谓词），并用 `demangle_kernel_name` 可逆地反推签名。

## 2. 前置知识

在进入源码前，先用通俗语言铺几个概念。

- **JIT vs AOT**：JIT（Just-In-Time）是 u8-l1 讲的「`ct.launch` 时按真实参数现编译现启动」；AOT 是「提前把内核编译成二进制，运行时只启动、不编译」。AOT 的代价是用户必须**自己**精确描述参数的类型和假设（JIT 时这些由框架自动推断）；好处是产物可脱离 cuTile Python 独立分发、可固定编译选项、启动期无编译延迟。
- **签名（signature）**：一组「参数约束」的有序集合，外加一个调用约定和一个可选符号名。它既是 AOT 编译的输入（告诉编译器「请按这套假设编译」），又是「二进制接口契约」（告诉调用方「请按这个顺序和格式塞参数」）。
- **约束（constraint）**：对单个参数的描述。除了类型，还携带编译期**假设**（assumption），例如「第 0 维长度恒为 16」「base 地址 16 字节对齐」。假设越强，编译器越能优化；但运行时若违反假设就是未定义行为。
- **调用约定（calling convention）**：规定三件事——二进制参数格式、支持的约束集合、名称修饰算法。本讲聚焦 `cutile_python_v1` 与 `cutile_python_v2`（v2 额外支持 tuple 参数与静态形状，详见 u8-l1）。
- **名称修饰（name mangling**，又叫 mangling）：把「函数名 + 签名」编码成一个唯一的、可读的字符串符号。同一份内核函数按不同签名编译会得到不同符号，从而能共存于同一个 cubin。
- **可逆 demangle**：mangling 不仅可正着编，还能原样反着解回签名。cuTile 在 `mangle_kernel_name` 内部就 assert 了「编→解→相等」的往返一致性。

> 提示：本讲的签名/约束/修饰逻辑**全部是纯 Python**，不碰 GPU。你可以脱离显卡单独构造签名、mangle、demangle 来学习——这也是后面代码实践能真正跑起来的原因。只有真正生成 cubin 那一步才需要 `tileiras` + GPU。

## 3. 本讲源码地图

本讲涉及的关键文件都在 `src/cuda/tile/compilation/` 子包下：

| 文件 | 作用 |
| --- | --- |
| [`__init__.py`](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/compilation/__init__.py) | 子包门面，再导出 `export_kernel`、`KernelSignature`、五种约束、`CallingConvention`、`mangle/demangle_kernel_name`。 |
| [`_export.py`](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/compilation/_export.py) | AOT 导出入口 `export_kernel`，把编译产物（cubin/字节码）写入文件。 |
| [`_signature.py`](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/compilation/_signature.py) | 五种 `ParameterConstraint`、`KernelSignature`、别名组校验、调用约定门控 `_validate_constraint_support`。 |
| [`_name_mangling.py`](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/compilation/_name_mangling.py) | `mangle_kernel_name` / `demangle_kernel_name` 及递归下降解析器。 |

辅助理解（非本讲精读对象，但实践会引用）：

- [`src/cuda/tile/_compile.py`](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_compile.py) 的 `compile_tile` 是 `export_kernel` 真正调用的编译总入口（见 u5-l2）。
- [`test/test_export_compat.py`](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/test/test_export_compat.py) 给出 v1/v2/静态形状三类真实 AOT 导出 + 原生 Driver 启动的端到端范例。
- [`test/test_name_mangling.py`](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/test/test_name_mangling.py) 给出几乎全部修饰编码的 golden 样例。

---

## 4. 核心概念与源码讲解

### 4.1 AOT 导出入口：export_kernel

#### 4.1.1 概念说明

`export_kernel` 是 AOT 编译的唯一公共入口。它接收一个已装饰的 `@ct.kernel` 内核、**一串** `KernelSignature`（同一个内核可以为多组不同假设各编译一份）、一个目标 GPU 架构（如 `"sm_100"`）、以及输出格式（`"cubin"` 或 `"tileir_bytecode"`），然后把编译产物写到文件或类文件对象。

理解它的关键有两点：

1. **它只是 `compile_tile` 的薄封装**。`export_kernel` 自己不做编译，它把 `output_format` 翻译成 `compile_tile` 的 `return_cubin` / `return_bytecode` 两个开关，再把得到的二进制写盘。真正的前端→IR→字节码→cubin 全流程（u5-l2）发生在 `compile_tile` 内部。
2. **AOT 与 JIT 走的是同一条 `compile_tile`**。区别仅在于：JIT 时签名由 `ct.launch` 根据真实参数自动推断；AOT 时签名由用户显式给出。这保证了 AOT 产物与 JIT 行为一致。

#### 4.1.2 核心流程

`export_kernel` 的执行过程可以用下面这段伪代码概括：

```
export_kernel(kernel, signatures, output_file, *, gpu_code, output_format, bytecode_version):
    1. 解析 bytecode_version 字符串（"13.1"）→ BytecodeVersion
    2. output_format → (return_bytecode, return_cubin) 二选一：
         "cubin"            → (False, True)
         "tileir_bytecode"  → (True,  False)
         其它               → 抛 ValueError
    3. res = compile_tile(kernel._annotated_function, signatures,
                          sm_arch=gpu_code,
                          compiler_options=kernel._compiler_options,
                          return_bytecode=..., return_cubin=...)
    4. 打开 output_file（文件名 / PathLike / 已打开的二进制流）
    5. 按 return_bytecode 写 res.bytecode，否则写 res.cubin
```

注意 `signatures` 是一个**列表**：`compile_tile` 会让 `_IrKeeper` 为列表里的每一个签名各生成一份 final IR，再合并成一份字节码（同一份 cubin 里可以装多个符号，每个符号对应一个签名）。这正是 AOT「一次导出多份特化」的能力来源。

#### 4.1.3 源码精读

先看函数签名与文档，它清楚地列出了五个参数的语义：

[src/cuda/tile/compilation/_export.py:13-19](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/compilation/_export.py#L13-L19) —— `export_kernel` 的形参：`kernel`、`signatures`（非空的签名序列）、`output_file`（文件名或二进制流）、`gpu_code`（目标架构）、`output_format`、`bytecode_version`。

格式开关的翻译在下面这段，把字符串映射到 `compile_tile` 的两个布尔返回开关：

[src/cuda/tile/compilation/_export.py:48-55](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/compilation/_export.py#L48-L55) —— `cubin` 与 `tileir_bytecode` 二选一，未知格式抛 `ValueError`。

核心调用——把内核的 `AnnotatedFunction`、签名序列、编译选项交给 `compile_tile`：

[src/cuda/tile/compilation/_export.py:57-62](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/compilation/_export.py#L57-L62) —— 注意它取的是 `kernel._annotated_function`（u5-l1 讲过的注解解析产物）与 `kernel._compiler_options`（`num_ctas`/`occupancy`/`opt_level`/`num_worker_warps` 冻结 dataclass），把 AOT 导出与 kernel 对象上承载的元数据无缝衔接。

最后按开关写盘，`_open` 上下文管理器统一处理「文件名 / PathLike / 已打开流」三种输入：

[src/cuda/tile/compilation/_export.py:64-68](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/compilation/_export.py#L64-L68) —— 二进制写入，`return_bytecode` 写 `res.bytecode`，否则写 `res.cubin`。

`signatures` 中若有 `symbol is None` 的签名，`compile_tile` 会自动给它补上 mangled 符号名（见 4.3.3）：

[src/cuda/tile/_compile.py:460-463](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_compile.py#L460-L463) —— 遍历签名，对 `symbol is None` 者调用 `with_mangled_symbol(pyfunc.__name__)`。

#### 4.1.4 代码实践

**实践目标**：跟踪 `export_kernel` 到 `compile_tile` 的衔接，并用真实范例验证产物可被原生 CUDA Driver 加载启动。

**操作步骤**：

1. 阅读上面的源码引用，确认 `export_kernel` 本身不编译、只翻译开关与写盘。
2. 打开 [`test/test_export_compat.py:112-143`](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/test/test_export_compat.py#L112-L143)（`test_export_compat_cutile_python_v1`），看它如何：构造一个 `KernelSignature` → `export_kernel(..., output_file=BytesIO(), output_format="cubin")` → 用 `cuLibraryLoadData` 加载返回的 cubin 字节 → `cuLibraryGetKernel` 按符号名取出内核 → `cuLaunchKernel` 启动。
3. 注意该测试的符号名 `"kernel_1_Kt1_I13_Si32_..."` 是自动 mangle 出来的（签名里没传 `symbol`），这与 4.1.3 最后一条引用吻合。

**需要观察的现象**：

- 导出得到的 cubin 是一段与 cuTile Python 运行时**解耦**的纯二进制，可以被 ctypes 直接喂给 Driver API。
- 调用方必须按签名的二进制格式（见 u8-l1 与本讲 4.2.2）逐个塞参数，符号名要和 mangled name 完全一致，否则 `cuLibraryGetKernel` 取不到内核。

**预期结果**：在装有 `tileiras` 与 GPU 的环境下，该测试通过，`a1`/`a2` 的值与断言一致。**待本地验证**（本实践的 cubin 实际产出需要 `tileiras` + GPU；符号拼接与签名构造可在无 GPU 环境下先做，见 4.4.4）。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `output_format` 传成 `"ptx"` 会怎样？
**答案**：在 [src/cuda/tile/compilation/_export.py:54-55](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/compilation/_export.py#L54-L55) 抛 `ValueError(f"Unknown output format 'ptx'")`。cuTile 的 AOT 只支持 cubin 与 tileir 字节码两种产物。

**练习 2**：为什么 `signatures` 是一个列表而不是单个签名？
**答案**：因为同一份内核函数可以按多组不同假设各编译一份特化，全部装进同一个 cubin、靠不同的 mangled 符号名区分。`compile_tile` 内部的 `_IrKeeper` 会为列表里的每个签名各生成一份 final IR（u5-l2）。

---

### 4.2 参数约束体系：ParameterConstraint 五兄弟

#### 4.2.1 概念说明

`ParameterConstraint` 是一个类型别名，代表「对单个内核参数的描述」。它有五个具体子类，正好对应 cuTile 内核能接收的五类参数：

| 约束类 | 对应内核参数 | 二进制参数格式（摘自 docs） |
| --- | --- | --- |
| `ScalarConstraint` | 标量 | 单个对应 C 类型的值 |
| `ArrayConstraint` | 数组 | `1 + 2·ndim` 个值：base 指针 + ndim 个 shape + ndim 个 stride |
| `ListConstraint` | 数组列表 | 2 个值：元素缓冲指针 + `int32` 长度 |
| `TupleConstraint`（v2） | 元组 | 元素按顺序「摊平」成连续参数，每个元素遵循其自身约束的格式 |
| `ConstantConstraint` | 常量 | **省略**——值已烘焙进 cubin，不出现在启动参数里 |

本讲的重点是其中两个与 u3-l7 新特性相关的字段：

- **`ArrayConstraint.shape_constant`**：把数组某些维度的长度特化为**编译期常量**。它与 u3-l7 讲的 `ct.ArrayAnnotation(static_shape_dims=...)` 是一件事的两面——`static_shape_dims=(0,)` 是**内核注解侧**（声明「第 0 维请特化」），`shape_constant=(16, None)` 是**AOT 签名侧**（声明「第 0 维特化值为 16」）。二者都要求 `cutile_python_v2`。
- **`TupleConstraint`**：把多个相关参数打包成一个 Python `tuple` 传入，内核内用 `pair[i]` 解包；AOT 侧用 `TupleConstraint([子约束, ...])` 描述。

每种约束还携带若干**编译期假设**（assumption）：对齐、整除性、别名关系、静态形状/步长等。这些假设会被 u6-l2 的数据流分析吃掉，转化为对齐优化。

> 简写约定：在 `KernelSignature` 的 `parameters` 里，你可以直接传一个裸 `bool`/`int`/`float`（自动包成 `ConstantConstraint`），或一个裸 `tuple`（自动包成 `TupleConstraint`）。这由 `_to_constraint` 完成。

#### 4.2.2 核心流程

约束对象的构造流程（以最复杂的 `ArrayConstraint` 为例）：

```
ArrayConstraint(dtype, ndim, *, index_dtype, stride_lower_bound_incl, alias_groups,
                may_alias_internally, stride_constant=None, shape_constant=None,
                stride_divisible_by=1, shape_divisible_by=1, base_addr_divisible_by=1):
    1. 校验 dtype / ndim / index_dtype(只能是 int32/uint32/int64)
    2. 用 _parse_assumption_tuple 把每个「按维度」的序列归一成长度恰为 ndim 的 tuple
    3. 一致性去冗余：
         - 若某维 stride_constant 已给定，则该维 stride_lower_bound_incl 必须相容并被置 None
         - 若某维 stride_constant 已给定，则该维 stride_divisible_by 必须整除它并被置 1
         - 若某维 shape_constant 已给定，则该维 shape_divisible_by 必须整除它并被置 1
    4. 全部字段经 object.__setattr__ 写入（frozen dataclass）
```

关键设计：`shape_constant` 与 `shape_divisible_by` 在同一维上**互斥表达**——若形状已是常量，整除性就冗余了（常量本身即可推出整除性），所以 `_remove_redundant_divisibility_constraints` 会把对应维的 `shape_divisible_by` 折成 1。这一点在后面 4.4 讲修饰编码时会再次出现：静态形状用 `s` 谓词表达后，同维的 `i`（shape divisibility）谓词会被抑制。

#### 4.2.3 源码精读

先看类型别名与简写归一函数——这是「裸值自动包成约束」的入口：

[src/cuda/tile/compilation/_signature.py:270-282](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/compilation/_signature.py#L270-L282) —— `ParameterConstraint` 是五者的联合别名；`_to_constraint` 把裸 `bool/int/float` 包成 `ConstantConstraint`、裸 `tuple` 包成 `TupleConstraint`。

`TupleConstraint` 极简——它只是一个不可变元组，元素也允许任意嵌套（递归走 `_to_constraint`）：

[src/cuda/tile/compilation/_signature.py:224-236](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/compilation/_signature.py#L224-L236) —— `items` 字段；构造时每个元素经 `_to_constraint` 归一。

`ArrayConstraint.shape_constant` 的文档明确标注了它与 v2 的绑定，以及它对 `shape_divisible_by` 的压制关系：

[src/cuda/tile/compilation/_signature.py:69-75](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/compilation/_signature.py#L69-L75) —— 「Requires `cutile_python_v2`」；`shape_constant[i]` 一旦给定，`shape_divisible_by[i]` 冗余、必须相容、随后被忽略。

构造期对 `shape_constant` 的解析（非负整数校验）：

[src/cuda/tile/compilation/_signature.py:138-139](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/compilation/_signature.py#L138-L139) —— 用 `_check_optional_nonnegative_int` 校验每个元素，确保静态形状非负。

`ConstantConstraint` 的 `__eq__` 有一个精巧设计：它区分 `1`、`True`、`1.0` 三种值，并对浮点按比特比较（让 `NaN == NaN`），这保证了「相同常量值 → 相同 mangled 名 → 命中同一份 cubin」：

[src/cuda/tile/compilation/_signature.py:255-267](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/compilation/_signature.py#L255-L267) —— 类型不同先判不等；浮点用 `struct.pack("=d", ...)` 比特比较。

#### 4.2.4 代码实践

**实践目标**：在纯 Python（无 GPU）环境下构造含 `TupleConstraint` 与 `ArrayConstraint(shape_constant=...)` 的约束，并观察字段归一行为。

**操作步骤**：

```python
# 示例代码：纯 Python，仅需 import cuda.tile 成功
import cuda.tile as ct
from cuda.tile.compilation import (KernelSignature, ArrayConstraint, TupleConstraint,
                                   ScalarConstraint, CallingConvention)

# 1) 一个 1D float32 数组，把第 0 维静态化为 8
a = ArrayConstraint(ct.float32, 1, index_dtype=ct.int32,
                    stride_lower_bound_incl=0, alias_groups=(),
                    may_alias_internally=False, shape_constant=(8,),
                    shape_divisible_by=(8,))   # 故意同时给整除性，看是否被压制
print("shape_constant =", a.shape_constant)    # (8,)
print("shape_divisible_by =", a.shape_divisible_by)  # (1,) —— 被折成 1

# 2) 一个 tuple[Tensor, Tensor]（两个 int32 标量，演示用）
t = TupleConstraint([ScalarConstraint(ct.int32), ScalarConstraint(ct.int32)])
print("items =", t.items)

# 3) 简写：裸 tuple → TupleConstraint，裸 int → ConstantConstraint
sig = KernelSignature([t, a, (10,), 42],
                      CallingConvention.cutile_python_v2())
print("归一后参数数 =", len(sig.parameters))   # 4
```

**需要观察的现象**：

- `shape_divisible_by` 从 `(8,)` 被自动改成 `(1,)`——这就是 `_remove_redundant_divisibility_constraints` 的效果，因为形状已知为常量 8，整除性 8 已冗余。
- 第 3 步里 `(10,)`（裸 tuple）和 `42`（裸 int）都被 `_to_constraint` 归一成了 `TupleConstraint`/`ConstantConstraint`，所以 `len(sig.parameters)` 仍是 4。

**预期结果**：`shape_divisible_by` 输出 `(1,)`；参数数为 4。**待本地验证**（依赖 `import cuda.tile` 成功；逻辑本身与 GPU 无关，与 `test_name_mangling.py` 同构）。

#### 4.2.5 小练习与答案

**练习 1**：`shape_constant=(8,)` 与 `static_shape_dims=(0,)` 是什么关系？
**答案**：`static_shape_dims` 是**内核注解侧**（`ct.ArrayAnnotation(static_shape_dims=(0,))`，声明「这个维度我想特化」），`shape_constant` 是 **AOT 签名侧**（声明「这个维度特化后的具体值是多少」）。JIT 时框架会根据 `static_shape_dims` 从启动参数里取值并填进 `shape_constant`；AOT 时由用户直接给 `shape_constant`。二者都要求 `cutile_python_v2`。

**练习 2**：`ConstantConstraint(1)`、`ConstantConstraint(True)`、`ConstantConstraint(1.0)` 三者相等吗？
**答案**：不相等。见 [src/cuda/tile/compilation/_signature.py:259-261](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/compilation/_signature.py#L259-L261)，`type(self.value) is not type(other.value)` 时直接判不等。它们会 mangle 出不同的符号（`I1` / `B1` / `F...`），各对应一份独立的 cubin。

---

### 4.3 KernelSignature：签名的构造、校验与调用约定门控

#### 4.3.1 概念说明

`KernelSignature` 把「一组参数约束 + 一个调用约定 + 一个可选符号名」打包成不可变对象。它是 AOT 编译的输入单元，也是 mangling 的输入。

它做三件重要的事：

1. **简写归一**：经 `_to_constraint` 把裸值/tuple 转成正式约束。
2. **别名组校验**（`_validate_alias_groups`）：检查 `alias_groups` 的使用是否合法——每个组必须被**至少两个**约束引用（只引用一次是冗余、报错），且同一个组不能横跨不同类型的约束（如 list 存储不允许与 array 别名）。
3. **调用约定门控**（`_validate_constraint_support`）：对每个约束递归检查「这个约束是否被所选调用约定支持」。这就是 tuple 与静态形状被强制要求 `cutile_python_v2` 的地方。

本讲的核心结论之一：**门控发生在 `KernelSignature` 的构造器里**（而不是 `export_kernel` 或 `compile_tile` 里）。这意味着无论你走到 AOT 还是 demangle，只要构造一个 `KernelSignature`，就会触发校验。

#### 4.3.2 核心流程

`KernelSignature.__init__` 的流程：

```
KernelSignature(parameters, calling_convention, symbol=None):
    1. 校验 symbol 是 str 或 None
    2. 校验 calling_convention 是 CallingConvention 实例
    3. parameters = tuple(_to_constraint(c) for c in parameters)   # 简写归一
    4. _validate_alias_groups(parameters)                           # 别名组一致性
    5. for x in parameters:
           _validate_constraint_support(x, calling_convention)      # 递归门控
    6. 冻结写入 parameters / calling_convention / symbol
```

`_validate_constraint_support` 的递归规则（这是本讲的「v2 门控表」）：

| 约束 | v1 是否支持 | v2 是否支持 | 备注 |
| --- | --- | --- | --- |
| `ScalarConstraint` | ✅ | ✅ | 总是支持 |
| `ConstantConstraint` | ✅ | ✅ | 总是支持 |
| `ArrayConstraint`（无 `shape_constant`） | ✅ | ✅ | |
| `ArrayConstraint`（含 `shape_constant`） | ❌ | ✅ | `version < 2` 抛错 |
| `TupleConstraint` | ❌ | ✅ | `version < 2` 抛错，再递归校验 `items` |
| `ListConstraint` | ✅（递归看 element） | ✅ | 递归校验 `element` |

> 设计直觉：v1 的二进制格式没有给 tuple 和静态形状预留编码位，所以必须升级到 v2。这个「能力 → 版本」的绑定在 AOT 侧是**显式硬门控**（构造期抛 `ValueError`），而在 JIT 侧（u8-l1）则是 `minimum_calling_convention` **自动升级**到 v2。两种机制保证同一件事：用了 tuple/静态形状，就一定是 v2。

#### 4.3.3 源码精读

`KernelSignature.__init__`——注意第 4、5 步分别做别名组校验与递归调用约定门控：

[src/cuda/tile/compilation/_signature.py:321-338](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/compilation/_signature.py#L321-L338) —— 构造器主体；`_to_constraint` 归一后依次跑 `_validate_alias_groups` 与逐个 `_validate_constraint_support`。

调用约定门控的递归实现，正是「tuple 与静态形状需 v2」的落点：

[src/cuda/tile/compilation/_signature.py:511-529](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/compilation/_signature.py#L511-L529) —— `ArrayConstraint` 分支检查 `any(x is not None for x in constraint.shape_constant) and cconv.version < 2`；`TupleConstraint` 分支检查 `cconv.version < 2`；`ListConstraint` 递归 `element`。

别名组校验——保证每个组被 ≥2 个约束引用、且不跨类型：

[src/cuda/tile/compilation/_signature.py:410-435](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/compilation/_signature.py#L410-L435) —— 用 `use_count` 统计每个组被引用次数，`count == 1` 即判冗余报错；`constraint_type_by_group` 防止同组跨类型。

`with_mangled_symbol`——`compile_tile` 给 `symbol is None` 的签名自动补名时调用的就是它：

[src/cuda/tile/compilation/_signature.py:340-352](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/compilation/_signature.py#L340-L352) —— 调 `mangle_kernel_name(function_name, self)` 生成符号，再用 `with_symbol` 返回带符号的副本。

`from_kernel_args`——一个「从样例参数反推签名」的便捷方法，但文档明确警告它可能产生意外假设（例如样例数组恰好 16 字节对齐就会被假设成永远对齐），仅供测试/原型：

[src/cuda/tile/compilation/_signature.py:365-407](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/compilation/_signature.py#L365-L407) —— 底层调 C++ 扩展 `get_parameter_constraints_from_pyargs`，再走 `KernelSignature` 构造器（同样触发 4.3.2 的校验）。

#### 4.3.4 代码实践

**实践目标**：亲手触发 v1 门控错误，验证 tuple 与静态形状在 v1 下被拒绝。

**操作步骤**：

```python
# 示例代码：纯 Python
import cuda.tile as ct
from cuda.tile.compilation import (KernelSignature, ArrayConstraint, TupleConstraint,
                                   ScalarConstraint, CallingConvention)

v1 = CallingConvention.cutile_python_v1()

# 1) v1 + tuple → 应抛 ValueError
try:
    KernelSignature([(ScalarConstraint(ct.int32),)], v1)
except ValueError as e:
    print("tuple v1 报错:", e)

# 2) v1 + 静态形状 → 应抛 ValueError
try:
    KernelSignature(
        [ArrayConstraint(ct.float32, 1, index_dtype=ct.int32,
                         stride_lower_bound_incl=0, alias_groups=(),
                         may_alias_internally=False, shape_constant=(8,))],
        v1)
except ValueError as e:
    print("static shape v1 报错:", e)
```

**需要观察的现象**：两次都抛 `ValueError`，且消息分别包含 `"Tuple parameters are not supported by calling convention cutile_python_v1; version >= 2 is required"` 与 `"Static array shapes are not supported by calling convention cutile_python_v1; version >= 2 is required"`。

**预期结果**：与 [`test/test_export_compat.py:208-227`](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/test/test_export_compat.py#L208-L227)（`test_static_shape_with_v1_raises` / `test_tuple_with_v1_raises`）的断言完全一致。**待本地验证**（仅需 `import cuda.tile`）。

#### 4.3.5 小练习与答案

**练习 1**：为什么把 `_validate_constraint_support` 放在 `KernelSignature.__init__` 里，而不是放在 `export_kernel` 里？
**答案**：因为 `KernelSignature` 是描述签名的唯一入口——AOT 导出、`from_kernel_args`、甚至 demangle（demangle 内部也会 `KernelSignature(parameters, cconv, symbol)` 重建签名，见 4.4.3）都要构造它。把门控放在构造器里，就保证了「无论从哪条路径进来，非法组合都第一时间被拒」。

**练习 2**：`from_kernel_args` 为什么被标注「仅供测试/原型」？
**答案**：因为它会从样例参数**反推**假设——例如样例数组的 base 地址恰好 16 字节对齐，它就会把 `base_addr_divisible_by=16` 写进签名，而真实运行时未必满足，违反即未定义行为。生产环境应手动构造 `KernelSignature` 显式声明假设。见 [src/cuda/tile/compilation/_signature.py:375-385](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/compilation/_signature.py#L375-L385) 的 warning。

---

### 4.4 名称修饰：mangle_kernel_name 与可逆 demangle

#### 4.4.1 概念说明

mangling 把「函数名 + 签名」编码成一个唯一符号串。cuTile 的编码是**人可读**的（不像 C++ 的 Itanium ABI 那样晦涩），并且**可逆**——`demangle_kernel_name` 能从符号串原样还原出函数名与 `KernelSignature`。

符号名的整体结构是：

\[
\text{symbol} \;=\; \underbrace{\text{func}}_{\text{函数名}} \;+\; \text{"\_K"} \;+\; \underbrace{\text{cconv.code}}_{\text{如 }t1\text{ / }t2} \;+\; \sum_{p\,\in\,\text{params}} \bigl(\text{"\_"} + \text{mangle}(p)\bigr)
\]

每个参数的 mangle 结果以一个**大写前缀字母**打头，标明约束种类：

| 前缀 | 约束 | 后跟内容 |
| --- | --- | --- |
| `S` | `ScalarConstraint` | dtype 编码（如 `i32`、`f32`） |
| `A` | `ArrayConstraint` | `{ndim}{dtype}` + 若干「轴谓词段」+ 可选 extras |
| `L` | `ListConstraint` | 别名组 + 可选 `i` + 元素约束的 mangle |
| `T` | `TupleConstraint`（v2） | `{count}` + 各元素约束的 mangle（**递归**） |
| `B` | `ConstantConstraint(bool)` | `0` 或 `1` |
| `I` | `ConstantConstraint(int)` | 十进制有符号整数（负数用 `_` 前缀，如 `I_7`） |
| `F` | `ConstantConstraint(float)` | 16 位十六进制（小端 double 的比特表示） |

本讲聚焦两个 v2 新编码：

- **tuple 的 `T` 前缀**：`T{count}{item0}{item1}...`，递归嵌套。例如 `TupleConstraint([Scalar(int32), Scalar(float32)])` → `T2Si32Sf32`；嵌套元组 `TupleConstraint([TupleConstraint([])])` → `T1T0`。
- **静态形状的 `s` 谓词**：在 `ArrayConstraint` 里，每个「按维度」的假设被表达成「**轴位掩码** + 谓词字母 + 值」。`shape_constant` 用字母 `s`，且会**抑制**同维的 `i`（shape divisibility）谓词。

#### 4.4.2 核心流程

mangle 的核心是「**按位掩码把同谓词的轴分桶**」。对 `ArrayConstraint`：

```
_mangle_array_constraint(a):
    ret = f"{a.ndim}{dtype}"                          # 如 "2f32"
    # 收集 5 类按维度假设，每类一个字母：
    #   s=shape_constant, i=shape_divisible_by, t=stride_constant,
    #   v=stride_divisible_by, l=stride_lower_bound_incl
    axis_predicates = OrderedDict()
    for (values, letter, default) in [(shape_constant,"s",None),
                                       (shape_divisible_by,"i",1),
                                       (stride_constant,"t",None),
                                       (stride_divisible_by,"v",1),
                                       (stride_lower_bound_incl,"l",None)]:
        for i, v in enumerate(values):
            if v != default:
                pred = f"{letter}{mangle_signed_int(v)}"   # 如 "s16"
                axis_predicates[pred] |= (1 << i)          # 把轴 i 并入该谓词的位掩码

    # 按位掩码分桶：相同掩码的谓词拼在一起
    by_mask = group axis_predicates by mask
    for mask in sorted(by_mask):                      # 掩码升序
        ret += f"_{mask:x}{by_mask[mask]}"            # 如 "_1s16"、"_3l0"

    # extras：base 对齐 p、别名组 gN、内部别名 i、index_dtype(u/w)
    ...
```

> 关键直觉：位掩码用十六进制写，每一位代表一个轴。例如 2D 数组、两轴都设了 `stride_lower_bound_incl=0`，掩码就是 `0b11 = 3`，写成 `_3l0`；只有第 0 轴是静态形状 16，掩码就是 `0b01 = 1`，写成 `_1s16`。这种「掩码分桶」让多个轴的同种假设共享一段编码，紧凑且无歧义。

demangle 是 mangle 的镜像：一个递归下降解析器（`_Cursor`）逐字符消费符号串，按前缀字母分派到对应的 `_demangle_*` 函数，重建约束对象。

mangle 的「往返一致性」由 `mangle_kernel_name` 自身保证：它编完之后立刻调 `_demangle_kernel_name` 解一遍，再 `assert` 解出来的函数名与参数列表和原输入相等。这把「编解对称」变成了一个不可违反的运行时不变量。

#### 4.4.3 源码精读

`mangle_kernel_name`——注意末尾两行 assert 就是「往返一致性」保证：

[src/cuda/tile/compilation/_name_mangling.py:19-30](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/compilation/_name_mangling.py#L19-L30) —— 先映射别名组、拼出 `func + "_K" + cconv.code + 各参数 mangle`，然后 `_demangle_kernel_name` 解回，`assert` 函数名与 `parameters` 都相等。

`_mangle_constraint`——前缀字母分派的总开关，T/A/L/S/B/I/F 七种：

[src/cuda/tile/compilation/_name_mangling.py:139-161](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/compilation/_name_mangling.py#L139-L161) —— 注意 `TupleConstraint → "T" + _mangle_tuple_constraint`；浮点常量用 `struct.pack("<d", ...)` 取 64 位比特再 `unpack("<Q")` 转 16 位 hex。

`_mangle_tuple_constraint`——递归嵌套的关键，格式 `{count}{item0}{item1}...`：

[src/cuda/tile/compilation/_name_mangling.py:376-380](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/compilation/_name_mangling.py#L376-L380) —— `f"{len(constraint.items)}" + "".join(_mangle_constraint(e) ...)`，自然支持任意深度嵌套（如 `T1T1T1Si32`）。

`_mangle_array_constraint` 的「轴谓词分桶」实现：

[src/cuda/tile/compilation/_name_mangling.py:190-209](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/compilation/_name_mangling.py#L190-L209) —— 用 `_collect_axis_predicate` 收集 5 类谓词到 `axis_predicates`（键=`字母+值`，值=轴位掩码），再按掩码 `by_mask` 分桶、掩码升序输出 `_{mask:x}{preds}`。

`_collect_axis_predicate`——把单个谓词并入其轴位掩码：

[src/cuda/tile/compilation/_name_mangling.py:428-436](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/compilation/_name_mangling.py#L428-L436) —— `axis_predicates[pred] |= (1 << i)`，跳过等于默认值的项（这就是 `shape_constant` 抑制 `shape_divisible_by` 的机制——后者默认 1，给定静态形状后被 4.2.2 折成 1，等于默认值，自然不编码）。

demangle 总入口 `_demangle_kernel_name`——先 `rfind("_K")` 切出函数名，解析调用约定，再循环解析每个参数，最后**构造 `KernelSignature`**（从而再次触发 4.3 的校验）：

[src/cuda/tile/compilation/_name_mangling.py:37-54](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/compilation/_name_mangling.py#L37-L54) —— 末行 `KernelSignature(parameters, cconv, symbol)`。这解释了 [`test/test_name_mangling.py:272-281`](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/test/test_name_mangling.py#L272-L281) 为何 demangle 一个含 tuple 的 v1 符号会抛 `version >= 2`——重建签名时被门控。

dtype 编码表（mangle 与 demangle 共用）：

[src/cuda/tile/compilation/_name_mangling.py:407-425](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/compilation/_name_mangling.py#L407-L425) —— 如 `int32→"i32"`、`float32→"f32"`、`bfloat16→"bf16"`、`tfloat32→"tf32"`、`float8_e4m3fn→"f8m3fn"`。

#### 4.4.4 代码实践

**实践目标**：亲手 mangle 一个含 tuple 与静态形状的 v2 签名，定位 `T` 与 `s` 的编码位置，再用 `demangle_kernel_name` 验证可逆。

**操作步骤**：

```python
# 示例代码：纯 Python，无 GPU
import cuda.tile as ct
from cuda.tile.compilation import (mangle_kernel_name, demangle_kernel_name,
                                   KernelSignature, ArrayConstraint, TupleConstraint,
                                   ScalarConstraint, CallingConvention)

# 一个 tuple[int32, int32] + 一个静态形状(8,) 的 1D float32 数组
params = [
    TupleConstraint([ScalarConstraint(ct.int32), ScalarConstraint(ct.int32)]),
    ArrayConstraint(ct.float32, 1, index_dtype=ct.int32,
                    stride_lower_bound_incl=0, alias_groups=(),
                    may_alias_internally=False, shape_constant=(8,)),
]
sig = KernelSignature(params, CallingConvention.cutile_python_v2())

name = mangle_kernel_name("my_kernel", sig)
print("mangled =", name)
# 期望形如：my_kernel_Kt2_T2Si32Si32_A1f32_1s8l0
#                          ^^ tuple: T + count=2 + 两个 Si32
#                                                ^^^^^^^^^ array: 1D f32, 掩码1→s8, 掩码3→l0

# 可逆验证
fname, dsig = demangle_kernel_name(name)
print("round-trip 函数名 =", fname)               # my_kernel
print("round-trip 参数相等 =", dsig.parameters == sig.parameters)  # True
```

**需要观察的现象**：

- `T2Si32Si32`：`T` 前缀 + 元素数 `2` + 两个标量约束的 mangle `Si32`。
- `A1f32_1s8l0`：`A` 前缀 + `ndim=1` + `dtype=f32`；`_1s8`（掩码 `1` = 第 0 轴，谓词 `s8` = 静态形状 8）；`_3l0`（掩码 `3` 在 1D 数组里退化为 `1`——实际这里 `l0` 也只覆盖第 0 轮，按位掩码 1）。注意**没有** `i` 谓词——`shape_constant` 已经把 `shape_divisible_by` 压制掉了。
- 往返后 `dsig.parameters == sig.parameters` 为 `True`（`ConstantConstraint.__eq__` 见 4.2.3，其它约束是 frozen dataclass 默认按字段比较）。

**预期结果**：mangled 名严格等于 `my_kernel_Kt2_T2Si32Si32_A1f32_1s8l0`（与 [`test/test_name_mangling.py:249-259`](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/test/test_name_mangling.py#L249-L259) 的静态形状样例 `_A2f32_1s16_3l0` 同构）。**待本地验证**（仅需 `import cuda.tile`）。

#### 4.4.5 小练习与答案

**练习 1**：为什么静态形状用 `s` 编码后，同维的 `i`（shape divisibility）谓词会消失？
**答案**：两个原因合力。其一，构造期 `_remove_redundant_divisibility_constraints`（[src/cuda/tile/compilation/_signature.py:498-508](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/compilation/_signature.py#L498-L508)）把 `shape_constant` 给定维的 `shape_divisible_by` 折成默认值 1。其二，`_collect_axis_predicate` 跳过等于默认值的项（[src/cuda/tile/compilation/_name_mangling.py:433](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/compilation/_name_mangling.py#L433)）。所以静态形状信息只通过 `s` 表达一次。

**练习 2**：浮点常量 `3.14` 的 mangle 结果 `F40091eb851eb851f` 是怎么来的？
**答案**：见 [src/cuda/tile/compilation/_name_mangling.py:155-157](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/compilation/_name_mangling.py#L155-L157)，`struct.pack("<d", 3.14)` 得到 8 字节小端 double 比特，`struct.unpack("<Q", ...)` 解释成无符号 64 位整数 `0x40091eb851eb851f`，再 `f"{i:016x}"`。这让任意 double（含 NaN、±0、±inf）都能无损、确定地编码（与 4.2.3 的比特比较 `__eq__` 配套）。

**练习 3**：demangle 一个 v1 符号 `my_kernel_Kt1_T1Si32` 会发生什么？
**答案**：会抛 `ValueError`，消息含 `"version >= 2"`。因为 `_demangle_kernel_name` 末行会 `KernelSignature(parameters, cconv, symbol)` 重建签名（[src/cuda/tile/compilation/_name_mangling.py:53](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/compilation/_name_mangling.py#L53)），而 v1 下含 `TupleConstraint` 触发 `_validate_constraint_support` 门控。这正是 [`test/test_name_mangling.py:272-276`](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/test/test_name_mangling.py#L272-L276) 验证的行为。

---

## 5. 综合实践

把本讲四个模块串起来，完成一次「带 tuple 参数 + 静态形状的 v2 内核 AOT 导出与签名验证」。本实践分两部分：A 部分纯 Python 可跑（验证签名与修饰），B 部分需 GPU + `tileiras`（验证真实 cubin）。

### A 部分：构造 v2 签名、mangle、demangle（纯 Python）

设计一个内核：接收一个 `tuple[Tensor, Tensor]`（两向量相加）和一个静态形状的输出数组。对应的 v2 签名如下：

```python
# 示例代码
import cuda.tile as ct
from cuda.tile.compilation import (mangle_kernel_name, demangle_kernel_name,
                                   KernelSignature, ArrayConstraint, TupleConstraint,
                                   ScalarConstraint, CallingConvention)

@ct.kernel
def add_pair_out(pair, out):
    a = ct.load(pair[0], (0,), (8,))
    b = ct.load(pair[1], (0,), (8,))
    ct.store(out, (0,), a + b)

# 签名：tuple[Array(float32,1D), Array(float32,1D)] + Array(float32,1D, 静态形状 8)
inner = ArrayConstraint(ct.float32, 1, index_dtype=ct.int32,
                        stride_lower_bound_incl=0, alias_groups=(),
                        may_alias_internally=False)
pair_c = TupleConstraint([inner, inner])
out_c  = ArrayConstraint(ct.float32, 1, index_dtype=ct.int32,
                         stride_lower_bound_incl=0, alias_groups=(),
                         may_alias_internally=False, shape_constant=(8,))
sig = KernelSignature([pair_c, out_c], CallingConvention.cutile_python_v2())

name = mangle_kernel_name("add_pair_out", sig)
print(name)   # 形如 add_pair_out_Kt2_T2A1f32_1l0A1f32_1l0_A1f32_1s8l0

fn, dsig = demangle_kernel_name(name)
assert fn == "add_pair_out"
assert dsig.parameters == sig.parameters
```

**你要在 mangled name 中指出**：

- `Kt2`：调用约定 v2（code = `t2`）。
- `T2A1f32_1l0A1f32_1l0`：tuple 的 `T` 前缀 + 元素数 `2` + 两个数组约束的 mangle（递归）。
- 末尾 `A1f32_1s8l0`：输出数组的 `s8` 谓词——这就是静态形状在第 0 轴的编码，且没有伴随的 `i` 谓词。

### B 部分：AOT 导出真实 cubin（待本地验证，需 GPU + tileiras）

参照 [`test/test_export_compat.py:184-205`](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/test/test_export_compat.py#L184-L205)（`test_export_compat_cutile_python_v2`，导出 `kernel_2` 并用 `cuLibraryLoadData`/`cuLaunchKernel` 启动 tuple 内核）与 [`test/test_export_compat.py:152-176`](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/test/test_export_compat.py#L152-L176)（静态形状导出），把上面的 `add_pair_out` 用 `export_kernel` 导出成 cubin：

```python
from io import BytesIO
from cuda.tile._compile import get_sm_arch
io = BytesIO()
ct.compilation.export_kernel(add_pair_out, [sig], gpu_code=get_sm_arch(),
                             output_file=io, output_format="cubin")
# io.getvalue() 即为可被 cuLibraryLoadData 加载的 cubin 字节
```

启动时按 v2 二进制格式塞参数（tuple 元素摊平为连续参数、Constant 省略、Array 为 `1+2·ndim`）。**待本地验证**：在没有 GPU/`tileiras` 的环境，A 部分仍可完整跑通并验证签名与修饰的正确性。

---

## 6. 本讲小结

- `export_kernel` 是 `compile_tile` 的薄封装：把 `output_format` 翻译成 `return_cubin`/`return_bytecode` 开关，再写盘；AOT 与 JIT 走同一条编译流水线，区别只在签名是用户显式给还是框架推断。
- 五种 `ParameterConstraint`（Scalar/Array/List/Tuple/Constant）精确描述参数类型与编译期假设；裸 `bool/int/float`/`tuple` 经 `_to_constraint` 自动归一为 `ConstantConstraint`/`TupleConstraint`。
- `ArrayConstraint.shape_constant` 与 `ct.ArrayAnnotation(static_shape_dims=...)` 是「AOT 签名侧」与「内核注解侧」的同一特性，都要求 `cutile_python_v2`；给定静态形状会压制同维的 `shape_divisible_by`。
- `KernelSignature.__init__` 做三件事：简写归一、`_validate_alias_groups` 别名组校验、`_validate_constraint_support` 按 `calling_convention.version` 递归门控（tuple 与静态形状需 v2）。
- 门控放在签名构造器里，使 AOT、`from_kernel_args`、demangle 三条路径都被同一道检查覆盖。
- `mangle_kernel_name` 用「前缀字母 + 轴位掩码分桶」产出人可读符号：tuple 用 `T{count}...`、静态形状用 `s` 谓词、浮点常量用 16 位 hex；`demangle_kernel_name` 递归下降可逆还原，且 mangle 内部 assert 往返一致。

## 7. 下一步学习建议

- **下一步读 u8-l3（自动调优 tune）**：tune 在 AOT/JIT 之上做配置搜索，会反复编译同一内核的不同 tile 尺寸特化——你将看到签名特化如何与基准子进程配合。
- **回顾 u8-l1（launch 与调用约定）**：本讲的 v1/v2 门控与 u8-l1 的 `minimum_calling_convention` 自动升级是同一件事的两侧（AOT 显式硬门控 vs JIT 自动升级），对照阅读能加深理解。
- **延伸阅读源码**：
  - [`test/test_export_compat.py`](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/test/test_export_compat.py)——唯一给出「AOT cubin + 原生 Driver 启动」端到端范例的测试，是理解二进制参数格式的最佳参考。
  - [`test/test_name_mangling.py`](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/test/test_name_mangling.py)——几乎所有 mangling 编码的 golden 样例，可作为「读懂符号名」的练习题库。
  - [`src/cuda/tile/_compile.py`](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_compile.py) 的 `compile_tile`（u5-l2）——跟踪 `signatures` 列表如何驱动 `_IrKeeper` 为每个签名各生成一份 final IR。
