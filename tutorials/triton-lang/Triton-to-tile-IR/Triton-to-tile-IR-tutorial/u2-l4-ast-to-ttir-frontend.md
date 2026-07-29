# AST → TTIR 前端（TileIR 分支）

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清楚在 `ENABLE_TILE=1` 时，Triton 为什么会走一条**独立的前端** `ast_to_ttir`，以及它在哪里被选中。
- 理解 TileIR 前端与上游 Triton 前端的**复用关系**：它不是从零重写，而是继承上游 `CodeGenerator`、只覆盖少数关键方法。
- 读懂 `semantic.py` 里检测「当前是不是 TileIR」的**两种不同机制**：环境变量 `is_tileir()` 与运行期 `target.backend`，以及它们各自的分支点（如 `debug_barrier`、`make_tensor_descriptor`）。
- 动手在源码里追踪一条「前端选择 → 语义分支」的链路。

本讲只读不改源码，承接 [u2-l1 后端选择机制](u2-l1-backend-selection.md) 里建立的「`ENABLE_TILE` 选择 driver、`target.backend` 决定 compiler」的认知，把视角前移到**语言前端**这一层。

## 2. 前置知识

在开始前，先用通俗语言理清几个概念：

- **前端（frontend）**：把用户写的 Python `@triton.jit` 内核（一段 Python AST）翻译成 Triton 自己的 MLIR 方言 **TTIR**（triton dialect）的那一层。本讲说的「前端」专指这一步。
- **AST**：Abstract Syntax Tree，Python 源码解析后的语法树。Triton 用 Python 标准库 `ast` 拿到它，再「访问（visit）」每个节点生成 IR。
- **`ast_to_ttir`**：上游 Triton 提供的入口函数，名字直译就是「把 AST 变成 TTIR」。TileIR 后端提供了**同名但不同实现**的函数，二者签名一致、可互换。
- **`builder`**：生成 IR 的「建造器」对象，每个语义函数最终都调 `self.builder.create_xxx(...)` 来产生一条 IR 指令。前端的工作就是遍历 AST、不断调用 builder。
- **TTG lowering（TritonGPU lowering）**：上游 PTX 后端把 TTIR/TTGIR 进一步降级到 GPU 层的一串 pass。TileIR 后端**不跑这条降级链**，而是走自己的 cuda_tile 转换，这一点会解释为什么 `debug_barrier` 要分叉。
- **两种「是不是 TileIR」的判别**：一种读环境变量（`ENABLE_TILE`），一种读运行期的 target（`target.backend`）。它们的差别是本讲的一个重要细节。

如果对「编译三段式 `make_ttir`/`make_tileir`/`make_cubin`」还没有整体印象，建议先看 [u1-l4 端到端编译链路总览](u1-l4-e2e-pipeline-overview.md)。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [python/triton/compiler/compiler.py](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/python/triton/compiler/compiler.py) | `ASTSource.make_ir` 在这里根据 `ENABLE_TILE` **选择**用哪个 `ast_to_ttir`，是前端的「总开关」。 |
| [python/triton/compiler/code_generator.py](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/python/triton/compiler/code_generator.py) | **上游** `CodeGenerator` 与上游 `ast_to_ttir`，是 TileIR 前端复用的基类。 |
| [third_party/tileir/backend/code_generator.py](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/backend/code_generator.py) | **TileIR** 自己的 `ast_to_ttir` 与 `TileIRCodeGenerator`，本讲主角。 |
| [python/triton/language/semantic.py](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/python/triton/language/semantic.py) | 语言语义层，定义 `is_tileir()` 并在 `debug_barrier`、`make_tensor_descriptor` 等处设置 TileIR 分支。 |
| [python/triton/language/core.py](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/python/triton/language/core.py) | `tileir_tensor_descriptor` 类定义，是语义层分支点产出的产物。 |

口诀：**「compiler 选前端，code_generator 复用上游，semantic 设分支」**。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

1. **4.1 ast_to_ttir 入口**——前端在哪里、如何被选中。
2. **4.2 前端复用与差异**——`TileIRCodeGenerator` 继承了什么、改了什么。
3. **4.3 semantic 分支点**——语言语义层如何根据 TileIR 走不同分支。

---

### 4.1 ast_to_ttir 入口：前端如何被选择

#### 4.1.1 概念说明

Triton 编译一个 `@triton.jit` 内核时，第一步就是把 Python AST 翻译成 TTIR。这一步由一个名叫 `ast_to_ttir` 的函数完成。

关键问题：**上游 Triton 已经有一个 `ast_to_ttir`，为什么 TileIR 还要单独来一个？**

因为 TileIR 前端在细节上和上游有差异（函数名 mangle 规则、不支持的 noinline、descriptor 的处理等），但这些差异不足以重写整个 AST 访问器。于是 TileIR 的做法是：**提供同名函数、签名完全一致**，在编译入口用一个 `if ENABLE_TILE` 来决定 import 哪一个。这样上游的编译主流程（`compile()`）一行都不用改，只是「换了个前端实现」。

#### 4.1.2 核心流程

前端的选中流程非常短：

```text
@triton.jit kernel
   │
   ▼
ASTSource.make_ir(target, options, ...)
   │
   ├── if ENABLE_TILE == "1":
   │       from ..backends.tileir.code_generator import ast_to_ttir   # TileIR 前端
   │   else:
   │       from .code_generator import ast_to_ttir                    # 上游前端
   │
   ▼
ast_to_ttir(fn, src, context, options, codegen_fns, module_map)
   │
   ▼
TTIR ModuleOp（triton 方言）
```

注意：选前端发生在 `ASTSource.make_ir` 里，而**不是**在 driver 选择之后某个隐式的地方——它是一个显式的 `if`。

#### 4.1.3 源码精读

选择点在 [python/triton/compiler/compiler.py:78-84](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/python/triton/compiler/compiler.py#L78-L84)，这是整个前端的「总开关」：

```python
def make_ir(self, target: GPUTarget, options, codegen_fns, module_map, context):
    if os.environ.get("ENABLE_TILE", "0") == "1":
        from ..backends.tileir.code_generator import ast_to_ttir
    else:
        from .code_generator import ast_to_ttir
    return ast_to_ttir(self.fn, self, context=context, options=options, codegen_fns=codegen_fns,
                       module_map=module_map)
```

说明：这里的判断和 [u2-l1](u2-l1-backend-selection.md) 里 `_create_driver` 的判断**一模一样**——都是直接读 `ENABLE_TILE`。也就是说，driver 层和前端层各自独立地读这个环境变量来决定走 TileIR。两边都满足时，整条链路才是 TileIR。

被选中的 TileIR 前端定义在 [third_party/tileir/backend/code_generator.py:181-231](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/backend/code_generator.py#L181-L231)。它的核心两步是「构造 generator → 访问 AST」：

```python
generator = TileIRCodeGenerator(
    context, prototype, gscope=fn.get_capture_scope(),
    function_name=fn.repr(proxy) + tileir_additonal_suffix,
    jit_fn=fn, is_kernel=True,
    file_name=file_name, begin_line=begin_line,
    options=options, codegen_fns=codegen_fns, module_map=module_map, is_gluon=False,
)
generator.visit(fn.parse())

ret = generator.module
ret.context = context
ret.name = generator.function_name
return ret
```

对比上游版本 [python/triton/compiler/code_generator.py:1627-1666](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/python/triton/compiler/code_generator.py#L1627-L1666)，有两处可见差异：

1. 上游构造的是 `CodeGenerator`，TileIR 构造的是 `TileIRCodeGenerator`（子类）。
2. 上游结尾有一段**校验**，TileIR 版本**没有**：

```python
# 上游结尾（TileIR 版本无此段）
if not module.verify():
    if not fn.is_gluon():
        print(module)
    raise RuntimeError("error encountered during parsing")
```

（注：`tileir_additonal_suffix` 在当前源码里被赋成空串 `""`，名字里 `additonal` 是源码原有的拼写，这里照实引用。）

#### 4.1.4 代码实践

**实践目标**：确认前端的「分流」确实只由 `ENABLE_TILE` 一个变量决定，并看清两个 `ast_to_ttir` 的物理位置。

**操作步骤**：

1. 打开 [compiler.py:78-84](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/python/triton/compiler/compiler.py#L78-L84)，确认 `if` 分支。
2. 用编辑器全局搜索 `def ast_to_ttir`，应只得到两处：上游 `python/triton/compiler/code_generator.py` 与 TileIR `third_party/tileir/backend/code_generator.py`。
3. 注意 `from ..backends.tileir.code_generator import ast_to_ttir` 里的 `..backends.tileir` 路径——回顾 [u1-l3](u1-l3-repo-structure.md)，`triton.backends.tileir` 在磁盘上不存在，是 `setup.py` 构建期建立的符号链接，指向 `third_party/tileir/backend`。

**需要观察的现象**：两个函数签名逐字相同，因此 import 哪一个，`make_ir` 的调用代码都不用改。

**预期结果**：你能指出「前端选择 = 一个 `if` + 两个同名函数」，且 TileIR 前端物理上位于 `third_party/tileir/backend/`。

**待本地验证**：若想在运行时确认真的走了 TileIR 前端，可在 `third_party/tileir/backend/code_generator.py` 的 `ast_to_ttir` 第一行临时加 `print("tileir frontend")`，分别在不设 / 设 `ENABLE_TILE=1` 时编译一个 kernel 观察输出（本讲不要求实际运行）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 TileIR 选择「提供同名函数 + import 分流」，而不是在上游 `ast_to_ttir` 里到处加 `if is_tileir()`？

**参考答案**：因为前端绝大多数逻辑（AST 遍历、builder 调用）和上游完全一致，只有少数方法（函数 mangle、noinline、descriptor）需要改。用「子类 + 同名函数分流」可以把改动收敛到 `third_party/tileir/` 里，不污染上游代码，也让上游 `compile()` 主流程零改动。

**练习 2**：TileIR 版本的 `ast_to_ttir` 结尾没有 `module.verify()`，这意味着什么？

**参考答案**：意味着 TileIR 前端不在 Python 侧做这层「解析后立即校验、失败即抛 RuntimeError」的检查，把正确性验证往后推（依赖后续 `make_tileir` 里的 `only_contain_legal_dialects` 等校验，见 [u2-l3](u2-l3-compile-stages.md)）。

---

### 4.2 前端复用与差异：TileIRCodeGenerator

#### 4.2.1 概念说明

`TileIRCodeGenerator` 是 TileIR 前端的核心。它**继承自上游 `CodeGenerator`**，所以 AST 访问的骨架（`visit`、各种 `visit_XXX` 节点处理、region 机制等）全部复用，不必重写。TileIR 只针对自己的需要**覆盖/新增**了少数方法。

这种「继承 + 少量覆盖」是孵化器仓库里非常典型的手法：既复用上游成熟逻辑，又能在关键点插手。

#### 4.2.2 核心流程

`TileIRCodeGenerator` 相对上游 `CodeGenerator` 的关系：

```text
上游 CodeGenerator                     （AST 遍历骨架、visit、builder 调用，全部继承）
   ▲
   │ 继承
   │
TileIRCodeGenerator
   ├── __init__            覆盖：仅调 super().__init__，参数对齐
   ├── get_used_vars       新增：上游没有这个方法
   └── call_JitFunction    覆盖：改用 TileIR 的函数 mangle；noinline 不支持
```

#### 4.2.3 源码精读

**复用的证据**：文件顶部直接从上游 `triton.compiler.code_generator` 导入了一堆符号，[third_party/tileir/backend/code_generator.py:17-27](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/backend/code_generator.py#L17-L27)：

```python
from triton.compiler.code_generator import (
    _is_list_like, _is_constexpr, _is_triton_tensor, _unwrap_if_constexpr,
    ASTFunction, CodeGenerator, enter_sub_region,
    flatten_values_to_ir, unflatten_ir_values,
)
```

注意它直接 import 了上游的 `CodeGenerator` 作为基类——这就是「复用」的来源。

**类与 `__init__`**：[third_party/tileir/backend/code_generator.py:67-103](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/backend/code_generator.py#L67-L103)，`TileIRCodeGenerator(CodeGenerator)`，`__init__` 只是原样转发给 `super().__init__(...)`，本身不增删字段：

```python
class TileIRCodeGenerator(CodeGenerator):
    def __init__(self, context, prototype, gscope, function_name, jit_fn, options,
                 codegen_fns, module_map, is_gluon=False, module=None, is_kernel=False,
                 function_types=None, noinline=False, file_name=None, begin_line=0):
        super().__init__(context=context, prototype=prototype, ...)
```

**新增方法 `get_used_vars`**：[third_party/tileir/backend/code_generator.py:104-116](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/backend/code_generator.py#L104-L116)。上游 `CodeGenerator` 并没有同名方法（这是 TileIR 的新增），用 `ast.walk` 收集一个语句里被「读取」的变量名，供 region 处理使用。

**关键覆盖 `call_JitFunction`**：[third_party/tileir/backend/code_generator.py:118-178](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/backend/code_generator.py#L118-L178)。当一个 Triton kernel 调用另一个 `@triton.jit` 函数时，需要生成被调函数的定义并 mangle 出一个唯一名字。TileIR 在这里用了自己的 mangle 规则 [third_party/tileir/backend/code_generator.py:38-64](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/backend/code_generator.py#L38-L64)：

```python
fn_name = mangle_fn(get_full_name(fn), arg_types, caller_context)
...
generator = TileIRCodeGenerator(self.context, prototype, fn.get_capture_scope(),
                                module=self.module, jit_fn=fn, function_name=fn_name, ...)
```

并且在这里处理 **noinline 不支持**的情况 [third_party/tileir/backend/code_generator.py:135-143](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/backend/code_generator.py#L135-L143)：

```python
# TileIR backend does not support noinline mode currently
if fn.noinline:
    import warnings
    warnings.warn("Current backend does not support noinline mode, noinline will be turn off.",
                  RuntimeWarning)
    fn.ninline = False
```

即：如果用户给被调函数标了 `noinline`，TileIR 会发一条 `RuntimeWarning` 然后强制关掉。

#### 4.2.4 代码实践

**实践目标**：量化「TileIR 前端到底改了多少」。

**操作步骤**：

1. 在 [third_party/tileir/backend/code_generator.py](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/backend/code_generator.py) 里数一下 `TileIRCodeGenerator` 类体内部定义了几个方法（答案应为 3 个：`__init__`、`get_used_vars`、`call_JitFunction`）。
2. 对比上游 [python/triton/compiler/code_generator.py](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/python/triton/compiler/code_generator.py) 里 `CodeGenerator` 的体量（成百上千行的 `visit_XXX`）。

**需要观察的现象**：TileIR 子类极薄，绝大多数行为来自继承。

**预期结果**：你会得出结论——TileIR 前端是「薄薄一层覆盖」，真正的 AST→IR 翻译主力仍是上游 `CodeGenerator`。

#### 4.2.5 小练习与答案

**练习 1**：`get_used_vars` 是覆盖还是新增？如何判断？

**参考答案**：是**新增**。判断方法：在上游 `python/triton/compiler/code_generator.py` 里搜索 `def get_used_vars` 找不到同名方法（上游只有 `__init__`、`call_JitFunction` 等），说明它是 TileIR 独有。

**练习 2**：如果一个内核调用了带 `@triton.jit(noinline=True)` 的辅助函数，在 TileIR 后端下会发生什么？

**参考答案**：会触发一条 `RuntimeWarning`（"Current backend does not support noinline mode..."），然后 `fn.ninline` 被强制设为 `False`，即该函数仍会被内联处理。

---

### 4.3 semantic 分支点：is_tileir() 与 target.backend

#### 4.3.1 概念说明

前端负责「翻译」，而**语义层（semantic）**负责「翻译时针对某个算子到底生成哪条 IR」。很多算子在 TileIR 和 PTX 两条后端上应当生成**不同的 IR 操作**，于是语义层也需要判别「现在是不是 TileIR」。

重要细节：语义层里有**两种**判别机制，容易混淆：

| 机制 | 来源 | 典型用途 |
| --- | --- | --- |
| `is_tileir()` | 直接读环境变量 `ENABLE_TILE` | `debug_barrier` |
| `target.backend == "tileir"` | 读运行期 `driver.active.get_current_target()` | `make_tensor_descriptor`、`_has_native_tma` |

二者通常一致（都为真或都为假），但**信息来源不同**：一个是「编译时环境开关」，一个是「运行时实际选中的后端」。在绝大多数场景里等价，但理解差别有助于排查「开关与实际 driver 不一致」的诡异问题（参见 [u2-l1](u2-l1-backend-selection.md) 关于「`driver.active` 是惰性缓存、开关须在 import 前设置」的讨论）。

#### 4.3.2 核心流程

两个典型分支点的决策：

```text
debug_barrier():
    if is_tileir():              # 读 ENABLE_TILE 环境变量
        builder.create_gpu_barrier()   → 生成 gpu.barrier
    else:
        builder.create_barrier()       → 生成 barrier（后续由 TTG lowering 处理）

make_tensor_descriptor(...):
    handle = builder.create_make_tensor_descriptor(...)
    target = driver.active.get_current_target()   # 读运行期 target
    if target.backend == "tileir":
        return tl.tileir_tensor_descriptor(handle, shape, strides, type, base)  # 带 ptr
    return tl.tensor_descriptor(handle, shape, strides, type)                    # 不带 ptr
```

#### 4.3.3 源码精读

**`is_tileir()` 定义**：[python/triton/language/semantic.py:12-13](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/python/triton/language/semantic.py#L12-L13)，直接读环境变量：

```python
import os
def is_tileir():
    return os.environ.get("ENABLE_TILE", "0") == "1"
```

**`debug_barrier` 分支点**：[python/triton/language/semantic.py:1782-1788](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/python/triton/language/semantic.py#L1782-L1788)：

```python
def debug_barrier(self) -> TensorTy:
    # TileIR lowers gpu.barrier directly and does not run the TTG lowering
    # that handles ttg.barrier. Keep this backend bridge until TileIR gains
    # native ttg.barrier support.
    if is_tileir():
        return self.tensor(self.builder.create_gpu_barrier(), tl.void)
    return self.tensor(self.builder.create_barrier(), tl.void)
```

注释给出了分叉的**原因**：TileIR 直接处理 `gpu.barrier`，且**不跑**上游那条会把 barrier 降级处理的 TTG lowering；所以在语义层就先「桥接」成 `gpu.barrier`。这个 bridge（桥接）会一直保留，直到 TileIR 原生支持 `ttg.barrier`。

> 说明：`create_gpu_barrier()` 产生 GPU 方言的 `gpu.barrier`；`create_barrier()` 产生上游 triton 的 barrier，它本应在后续 TTG lowering 里被处理成 `ttg.barrier`——而那条 lowering TileIR 不跑，所以必须在这里分叉。本讲依据源码注释得出此结论，IR 具体拼写以本地 dump 为准（待本地验证）。

**`make_tensor_descriptor` 分支点**：[python/triton/language/semantic.py:1882-1888](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/python/triton/language/semantic.py#L1882-L1888)，这里用的是 `target.backend`：

```python
handle = self.builder.create_make_tensor_descriptor(base_handle, [s.handle for s in shape],
                                                    [s.handle for s in strides], block_shape, is_signed_int, padding)
target = driver.active.get_current_target()
if target.backend == "tileir":
    return tl.tileir_tensor_descriptor(handle, shape, strides, type, base)
return tl.tensor_descriptor(handle, shape, strides, type)
```

注意区别：TileIR 分支返回的 `tileir_tensor_descriptor` **多带了一个 `base`（指针）参数**。这是为了「host TMA」——TileIR 没有 host 端 TMA，需要在语言层把指针一起携带下去，由 launcher 拆解（详见 [u2-l6 TMA Tensor Descriptor](u2-l6-tma-tensor-descriptor.md)）。这个多出来的 `ptr` 字段在 [python/triton/language/core.py:1574-1585](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/python/triton/language/core.py#L1574-L1585)：

```python
class tileir_tensor_descriptor(tensor_descriptor):
    def __init__(self, handle, shape, strides, block_type, ptr):
        super().__init__(handle, shape, strides, block_type)
        # additional ptr field to satisfy tileir backend TensorDescriptor handling.
        self.ptr = ptr
        self.type = tileir_tensor_descriptor_type(block_type, ...)
```

**第三个分支点 `_has_native_tma`**：[python/triton/language/semantic.py:1098-1100](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/python/triton/language/semantic.py#L1098-L1100)，把 `tileir` 和 `cuda` 一起视为「有原生 TMA」：

```python
def _has_native_tma(self):
    target = driver.active.get_current_target()
    return ((target.backend == "cuda" or target.backend == "tileir") and target.arch >= 90)
```

#### 4.3.4 代码实践

**实践目标**（即本讲主实践）：在 `semantic.py` 里找出 `is_tileir()` 的调用点，说明 `debug_barrier` 在 TileIR 下生成了哪个 IR 操作，并解释其注释给出的原因。

**操作步骤**：

1. 在 [semantic.py](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/python/triton/language/semantic.py) 中搜索 `is_tileir()`（带括号的**调用**），排除第 12 行的 `def`。你会得到**唯一**一处调用：`debug_barrier` 内 [semantic.py:1786](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/python/triton/language/semantic.py#L1786)。
2. 阅读 [semantic.py:1782-1788](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/python/triton/language/semantic.py#L1782-L1788) 的注释。
3. 对比另一类判别：搜索 `target.backend == "tileir"`，确认它出现在 `_has_native_tma`（1100）与 `make_tensor_descriptor`（1886），机制与 `is_tileir()` 不同。

**需要观察的现象**：

- `is_tileir()` 在 `semantic.py` 里**只被 `debug_barrier` 调用一次**（其余 TileIR 判别用的是 `target.backend`）。
- `debug_barrier` 在 TileIR 下调用 `self.builder.create_gpu_barrier()`，即生成 `gpu.barrier`；在 PTX 下调用 `create_barrier()`。

**预期结果**：你能用自己的话写出——`debug_barrier` 在 TileIR 下生成 `gpu.barrier`，注释给出的原因是「TileIR 直接处理 `gpu.barrier`，且不跑那条会处理 `ttg.barrier` 的 TTG lowering，所以需要这个 bridge，直到 TileIR 原生支持 `ttg.barrier`」。

**待本地验证**：若想看到具体 IR，可设 `ENABLE_TILE=1` 编译一个含 `tl.debug_barrier()` 的内核，dump 出 `.ttir` 文件观察其中的 barrier 操作拼写（本讲不要求实际运行）。

#### 4.3.5 小练习与答案

**练习 1**：`is_tileir()` 和 `target.backend == "tileir"` 在语义层的信息来源分别是什么？它们一定相等吗？

**参考答案**：`is_tileir()` 读环境变量 `ENABLE_TILE`（编译期开关）；`target.backend == "tileir"` 读 `driver.active.get_current_target()`（运行期实际选中的后端）。在「开关在 import 前设置、driver 随之选中 TileIR」的正常情况下二者一致；但如果开关设置时机不对导致 `driver.active` 被缓存成别的后端，二者可能不一致（这正是 [u2-l1](u2-l1-backend-selection.md) 强调开关须在首次访问 driver 前设置的原因）。

**练习 2**：`make_tensor_descriptor` 在 TileIR 分支返回的对象，比 PTX 分支多携带了什么？为什么？

**参考答案**：多携带了一个 `base`（即指针 `ptr`）。因为 TileIR 没有 host 端 TMA，需要在语言层把指针一起带下去，交给 launcher 在启动时拆解成 `ptr/shape/stride`（详见 [u2-l6](u2-l6-tma-tensor-descriptor.md)）。

**练习 3**：为什么 `debug_barrier` 必须在语义层就分叉，而不是统一生成同一种 barrier、交给后端各自处理？

**参考答案**：因为上游 PTX 后端依赖后续 TTG lowering 来处理它的 barrier（→ `ttg.barrier`），而 TileIR 不跑这条 lowering；若统一生成上游 barrier，TileIR 路径上就没有 pass 去处理它。所以在语义层提前桥接成 TileIR 能直接处理的 `gpu.barrier`，是最稳妥的做法。

---

## 5. 综合实践

把本讲三个模块串起来，做一次「前端选择 → 子类覆盖 → 语义分支」的端到端源码追踪：

**任务**：写一段文字说明（不必运行代码），回答下列问题，每个回答都给出对应的源码永久链接。

1. 用户写了一个 `@triton.jit` 内核并 `ENABLE_TILE=1` 启动。从 [compiler.py:78-84](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/python/triton/compiler/compiler.py#L78-L84) 出发，说明它最终用的是哪个 `ast_to_ttir`、构造的是哪个 generator 类。
2. 这个内核里调用了一个辅助 `@triton.jit` 函数。指出负责生成被调函数定义的代码在 [code_generator.py:118-178](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/third_party/tileir/backend/code_generator.py#L118-L178)，并说明它相对上游改了两件什么事（mangle、noinline）。
3. 这个内核里用到了 `tl.debug_barrier()`。依据 [semantic.py:1782-1788](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/python/triton/language/semantic.py#L1782-L1788)，说明它在 TileIR 下生成 `gpu.barrier` 及其注释原因。
4. 这个内核里还用了 `tl.make_tensor_descriptor(...)`。依据 [semantic.py:1882-1888](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/python/triton/language/semantic.py#L1882-L1888)，说明这里用的是 `target.backend`（而非 `is_tileir()`）来判别，并指出 TileIR 分支多携带了 `base` 指针。

**预期成果**：一张从「环境变量 → 前端选择 → 子类覆盖点 → 语义分支点」的完整因果链，且每一环都有源码链接与行号支撑。

## 6. 本讲小结

- TileIR 前端通过 [compiler.py:78-84](https://github.com/triton-lang/Triton-to-tile-IR/blob/1bd89c0dfb66fc99d4d338af4baddd2874de9d87/python/triton/compiler/compiler.py#L78-L84) 的一个 `if ENABLE_TILE` 选择**同名**的 `ast_to_ttir`，上游主流程零改动。
- `TileIRCodeGenerator` 继承上游 `CodeGenerator`，是「薄薄一层覆盖」：只动 `__init__`、`get_used_vars`（新增）、`call_JitFunction`（mangle + noinline）。
- TileIR 版 `ast_to_ttir` 结尾**没有**上游的 `module.verify()` 校验，把验证推到后续 `make_tileir` 阶段。
- 语义层判别 TileIR 有**两种**机制：环境变量 `is_tileir()`（仅 `debug_barrier` 用）与运行期 `target.backend`（`make_tensor_descriptor`、`_has_native_tma` 用）。
- `debug_barrier` 在 TileIR 下生成 `gpu.barrier`，因为 TileIR 直接处理它、且不跑会处理 `ttg.barrier` 的 TTG lowering。
- `make_tensor_descriptor` 在 TileIR 分支返回带 `base` 指针的 `tileir_tensor_descriptor`，为 host TMA 缺失设计铺路（详见 u2-l6）。

## 7. 下一步学习建议

- 若想看 `tileir_tensor_descriptor` 多带的 `base` 指针在启动时如何被拆解，直接进入 [u2-l6 TMA Tensor Descriptor 的拆解与启动](u2-l6-tma-tensor-descriptor.md)。
- 若想看前端产出的 TTIR 之后被哪些 pass 转成 cuda_tile，进入 [u2-l3 三段式编译流水线](u2-l3-compile-stages.md) 与第三单元 [u3 MLIR 转换 Pass 体系](u3-l1-pass-plugin-skeleton.md)。
- 想进一步理解「为什么 TileIR 不跑 TTG lowering」，可结合 [u3-l6 无序内存模型与 AutoGenMemoryToken](u3-l6-memory-token.md) 阅读其独有的内存模型处理。
