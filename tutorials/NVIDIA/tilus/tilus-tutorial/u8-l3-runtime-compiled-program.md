# 运行时：CompiledProgram 与内核启动

## 1. 本讲目标

前几讲我们走完了「写 Script → 转译成 Tilus IR → 布局推理 → 变换 → 代码生成 → 编译成 `.so`」这条**编译期**流水线（u3-l1、u6-l1）。本讲跨过编译期的终点，进入**运行期**：那块编译好的 `lib.so` 是怎么被加载进进程的？你写下 `kernel(a, b, c)` 后，Python 里的 `torch.Tensor` 是怎样变成 GPU 上的设备指针、又怎样最终触发一次 CUDA 内核启动的？

学完本讲你应当能够：

- 说清 `CompiledProgram` 的职责：它是一个极薄的「加载 `.so` + 取出 `launch` 函数」的包装器。
- 画出从 `kernel(...)` 到 CUDA `LaunchKernelStmt` 的完整调用链，并指出 grid/cluster/warps 在哪一步被确定。
- 理解 torch 张量到设备指针的映射发生在哪里、由谁负责（tvm_ffi 的 PackedFunc 机制）。
- 掌握 `python/tilus/tensor.py` 提供的 `from_torch / empty / torch()` 等张量工具函数，知道它们与运行时如何配合。

## 2. 前置知识

本讲默认你已经掌握以下内容（来自前置讲义）：

- **编译流水线总览**（u3-l1）：`build_program` 的六阶段，以及缓存目录 `programs/<12位摘要>/` 下存放 `lib.so`、`program.txt`、`options.txt` 三件套。
- **generate_ir_module**（u6-l1）：一个 Tilus `Function` 会裂变成一对 hidet 函数——设备侧 `*_kernel`（`cuda_kernel`）与主机侧 `public` 函数，二者由 `LaunchKernelStmt` 连接；指令到 Hidet IR 的落地走发射器。
- **Script 实例化与三类参数**（u2-l1、u8-l2）：`__call__` 参数分为常量参数（const，编译期烘焙进内核）、调优参数（tuning，整数 `DataType`，按整除性/尺寸桶参与选优但不重编译）、内核参数（kernel，指针与非整数 `DataType`，运行时透传给 launch）。
- **Metadata**（u3-l3）：`Function.metadata` 携带 `grid_blocks / cluster_blocks / num_warps`，是启动配置的来源。

几个名词先统一：

- **launch 函数**：编译产物 `.so` 中导出的、名为 `launch` 的入口函数。它运行在主机侧（CPU），负责把运行时实参装配好，再发起一次 `cudaLaunchKernel`。Python 端最终调用的就是它。
- **PackedFunc**：`tvm_ffi`（TVM 的 C++ FFI 层，Tilus 依赖它做跨语言调用）提供的一种「类型擦除的、可变参数的、带自动类型转换」的可调用对象。它能把 Python 传入的 `torch.Tensor` 自动转成底层指针。
- **DLPack**：跨框架的张量交换标准。`torch.Tensor` 实现了 DLPack 接口，因此 `tvm_ffi` 可以零拷贝地拿到它的设备指针、形状、数据类型等信息。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [python/tilus/runtime/compiled_program.py](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/runtime/compiled_program.py) | 定义 `CompiledProgram`：用 `tvm_ffi.load_module` 加载 `.so`，取出 `launch` 函数；并含 `load_compiled_program`、`compiled_program_exists` 两个工具函数。这是运行时的核心，也是本讲主角。 |
| [python/tilus/runtime/__init__.py](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/runtime/__init__.py) | 运行时包的导出口，把上述三个名字 re-export 给 `tilus.runtime`。 |
| [python/tilus/tensor.py](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/tensor.py) | 顶层张量工具：`Tensor` 类与 `from_torch / empty / rand / ones / torch()` 等。它把 `torch.Tensor` 包装成 Tilus 的 host 端张量视图，是「指针映射」的 Python 侧入口。 |
| [python/tilus/lang/instantiated_script.py](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/instantiated_script.py) | `InstantiatedScript.__call__` 是 `kernel(...)` 的真正实现：选最优 program、取 launch 函数、把 `kernel_params` 透传进去。串联运行时的「上层」。 |
| [python/tilus/drivers.py](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/drivers.py) | `build_program`（编译并返回缓存目录）与 `build_and_load_program`（编译并返回 `CompiledProgram`）住在这里——注意它**不在** `runtime/` 下，本讲会厘清边界。 |
| [python/tilus/backends/codegen.py](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/backends/codegen.py) | `visit_Function` / `launch_kernel`：在代码生成阶段把 `Metadata` 的 grid/cluster/warps 写进主机侧 `LaunchKernelStmt`，决定「烘焙」的内容。 |
| [python/tilus/hidet/transforms/generate_launch_func.py](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/hidet/transforms/generate_launch_func.py) | hidet 的 pass：保证最终模块里有一个名为 `launch` 的 `public` 函数（重命名或新生成），这是运行时按名字 `module["launch"]` 取函数的依据。 |

## 4. 核心概念与源码讲解

### 4.1 CompiledProgram：加载 .so 与 launch 函数

#### 4.1.1 概念说明

编译流水线的产物是一个目录（如 `.cache/tilus/programs/<12位摘要>/`），里面有一份 `module/lib.so`。但「一个 `.so` 文件」对 Python 来说还不能直接调用——它是一段编译好的 C++/CUDA 机器码，需要一个加载器把它映射进当前进程的地址空间，并暴露出其中的函数符号。

`CompiledProgram` 就是这个加载器。它做的事极其简单：用 `tvm_ffi.load_module` 把 `.so` 加载进来，然后从模块里按名字取出 `launch` 这个函数对象，之后每次调用内核就是调用这个函数对象。

> **关键直觉**：`CompiledProgram` 自己**不计算** grid/cluster/warps，也**不转换** torch 张量。这些工作分别已经在编译期（写进了 `.so` 里的主机侧 `launch` 函数）和调用期（由 `tvm_ffi` 的参数编组机制）完成了。`CompiledProgram` 只是一个「拿着 launch 函数的薄壳」。

这一点常常被误解：很多人会以为运行时有一段 Python 代码在读取 `Metadata.grid_blocks` 然后调用 `cudaLaunchKernel`。实际上没有——`Metadata` 在代码生成阶段（u6-l1 的 `codegen.py`）就已经被翻译成主机侧 C 代码里的 `LaunchKernelStmt`，烘焙进了 `.so`。运行时只是触发它。

#### 4.1.2 核心流程

```
build_program(prog)                 # drivers.py，编译期，返回缓存目录路径 str
        │
        ▼
load_compiled_program(dir)          # runtime/__init__.py → compiled_program.py
        │
        ├── tvm_ffi.load_module(dir/module/lib.so)   # 把 .so 加载进进程
        │
        └── compiled_module["launch"]                # 取出 launch 函数对象
        │
        ▼
CompiledProgram(program_dir)        # 保存 program_dir、compiled_module、launch_func
        │
        ├── get_launch_func() → 返回 launch_func
        │
        └── __call__(*args) → launch_func(*args)     # 直接转发
```

构造 `CompiledProgram` 的标准入口是 `load_compiled_program(program_dir)`，而它通常又被 `drivers.build_and_load_program` 包一层（编译 + 加载一次完成）。三者关系后面源码精读里会逐行看到。

#### 4.1.3 源码精读

**`CompiledProgram` 的全部实现只有十来行**，先看它的构造：

```python
class CompiledProgram:
    def __init__(self, program_dir: str | Path):
        self.program_dir: Path = Path(program_dir)
        self.compiled_module = tvm_ffi.load_module(str(Path(self.program_dir) / "module" / "lib.so"))
        self.launch_func = self.compiled_module["launch"]
```

[python/tilus/runtime/compiled_program.py:35-39](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/runtime/compiled_program.py#L35-L39) 中文说明：构造时固定去 `program_dir/module/lib.so` 加载共享库，并按名字 `"launch"` 取出导出函数。注意路径是写死的 `module/lib.so`——这正好对应 u3-l1 讲过的缓存目录结构（`module/` 下放 `source.cu`、`lib.so`、`compile.sh`）。

`get_launch_func` 与 `__call__` 同样直白：

```python
    def get_launch_func(self) -> tvm_ffi.Function:
        return self.launch_func

    def __call__(self, *args):
        return self.launch_func(*args)
```

[python/tilus/runtime/compiled_program.py:41-45](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/runtime/compiled_program.py#L41-L45) 中文说明：`get_launch_func` 把内部的 `tvm_ffi.Function` 暴露出去（调优 benchmark 时会用到，见 4.2.3）；`__call__` 则把调用原样转发给 `launch_func`。`CompiledProgram` 本身就是可调用的。

旁边还有一个结构几乎相同的 `CompiledModule` 类（[compiled_program.py:20-32](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/runtime/compiled_program.py#L20-L32)），区别是它接收 `.so` 路径而非程序目录，可理解为更底层的「按文件加载」版本，`CompiledProgram` 是「按缓存目录加载」的对外版本。

**两个工具函数**：

```python
def load_compiled_program(program_dir: str | Path) -> CompiledProgram:
    return CompiledProgram(program_dir)

def compiled_program_exists(cache_dir: Path | str) -> bool:
    path = Path(cache_dir)
    return all(
        [(path / "module" / "lib.so").exists(), (path / "program.txt").exists(), (path / "options.txt").exists()]
    )
```

[python/tilus/runtime/compiled_program.py:48-82](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/runtime/compiled_program.py#L48-L82) 中文说明：`load_compiled_program` 就是 `CompiledProgram` 的工厂；`compiled_program_exists` 用「`lib.so` + `program.txt` + `options.txt`」三件套同时存在来判定一个缓存目录是否已编译完成——这正是 u3-l1 讲过的命中判据，`build_program` 会先用它快速短路、再加 `FileLock` 双重检查。

最后厘清一个边界：`build_and_load_program` **不在** `runtime/compiled_program.py`，而在 `drivers.py`：

```python
def build_and_load_program(prog: Program, options: Optional[BuildOptions] = None) -> CompiledProgram:
    return load_compiled_program(build_program(prog, options))
```

[python/tilus/drivers.py:328-344](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/drivers.py#L328-L344) 中文说明：它把「编译（`build_program` 返回目录）」和「加载（`load_compiled_program` 返回 `CompiledProgram`）」串成一步。而 `build_program` 内部在命中缓存时直接 `return str(cache_dir)`（[drivers.py:280-325](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/drivers.py#L280-L325)），所以「加载」本身永远是轻量的——重活都已在编译期干完。

包导出口也很干净：

```python
from .compiled_program import CompiledProgram, compiled_program_exists, load_compiled_program
```

[python/tilus/runtime/__init__.py:15](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/runtime/__init__.py#L15) 中文说明：`tilus.runtime` 只对外暴露这三个名字，没有别的隐藏接口。

#### 4.1.4 代码实践

**目标**：亲手加载一个已编译的内核，绕过 `kernel(...)` 高层入口，直接拿到并调用 `launch` 函数，从而体会 `CompiledProgram` 的「薄壳」本质。

**操作步骤**：

1. 先随便编译一个内核产生缓存目录（以 vector_add 为例）。设置一个明确的 `cache_dir` 便于查找：

```python
import tilus
tilus.option.cache_dir("/tmp/tilus-cache")      # 指定缓存根目录

import torch
from examples.vector_add.vector_add import VectorAdd   # 示例代码，路径依实际仓库而定

n = 1024
a = torch.randn(n, dtype=torch.float32, device="cuda")
b = torch.randn(n, dtype=torch.float32, device="cuda")
c = torch.empty_like(a)
VectorAdd(a, b, c, n)                              # 触发一次编译
```

2. 用 `build_and_load_program` 显式拿到 `CompiledProgram`（本例为演示，实际可直接用上面的调用结果）：

```python
from tilus.runtime import load_compiled_program, compiled_program_exists
from pathlib import Path

# 在 /tmp/tilus-cache/tilus/programs/ 下找到那个 12 位摘要目录
prog_dir = Path("/tmp/tilus-cache/tilus/programs/<你看到的那个hash目录>")
print("exists:", compiled_program_exists(prog_dir))

cp = load_compiled_program(prog_dir)
print(type(cp))                 # <class 'tilus.runtime.compiled_program.CompiledProgram'>
launch = cp.get_launch_func()
print(type(launch))             # tvm_ffi.Function

c.zero_()
launch(a, b, c, n)              # 直接调 launch，与 cp(a,b,c,n) 等价
print(torch.allclose(c, a + b)) # True
```

3. 观察缓存目录里确实只有 `module/lib.so`、`program.txt`、`options.txt` 三件套。

**需要观察的现象**：
- `cp(a, b, c, n)` 与 `cp.get_launch_func()(a, b, c, n)` 行为完全一致——证明 `__call__` 只是转发。
- 即使删掉 `tilus` 的 Python 层只保留 `.so`，只要 `tvm_ffi` 能加载，`launch` 仍可调用——证明启动逻辑全在 `.so` 里，不在 Python 里。

**预期结果**：`exists: True`，第二次调用后 `c == a + b` 校验通过。

> 待本地验证：上述目录名与 `examples/vector_add` 的具体导入路径依你的环境而定；若无法运行，至少完成「源码阅读型实践」——在 `compiled_program.py` 里逐行确认 `CompiledProgram` 不含任何 grid/warps 计算逻辑。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `CompiledProgram.__init__` 里把 `.so` 路径写死成 `program_dir/module/lib.so`，而不是让调用方传 `.so` 路径？

**参考答案**：因为它服务于 Tilus 固定的缓存目录约定（u3-l1）：每个程序目录都按相同的 `module/lib.so` 布局存放编译产物。固定路径让「缓存目录」成为一个自解释的加载句柄，调用方只需传目录即可。若要按任意 `.so` 加载，则用更底层的 `CompiledModule`（接收 `lib_path`）。

**练习 2**：`compiled_program_exists` 为什么要求三个文件同时存在，而不是只看 `lib.so`？

**参考答案**：`lib.so` 可能因为编译中断而残缺或为旧版本。`program.txt` + `options.txt` 与 `lib.so` 同时存在，才表示「针对这段程序文本、这套选项的一次完整编译已落盘」。这正是 `build_program` 命中短路、避免重复编译的可靠判据（见 u3-l1、u8-l1）。

---

### 4.2 指针映射与内核启动：从 kernel(...) 到 CUDA launch

#### 4.2.1 概念说明

日常使用中我们很少直接碰 `CompiledProgram`，而是写 `kernel(a, b, c, n)`。这里有两件「魔法」需要揭开：

1. **torch 张量如何变成设备指针？** 你传的是 Python 里的 `torch.Tensor`，但 CUDA 内核吃的是裸的设备地址（`void*`/`float*`）和标量。这层转换由 `tvm_ffi` 的 PackedFunc 机制完成：它识别出实参是 DLPack 兼容的张量，就零拷贝取出其 `data`（设备指针）与设备号传给 C 侧的 `launch`；识别出是 Python `int`，就按标量传。**Tilus 自己没有写这层转换代码**，它复用了 tvm_ffi 的能力。

2. **grid/cluster/warps 在哪里确定？** 在编译期。`codegen.py` 的 `visit_Function` 把 `Metadata` 翻译成主机侧的 `LaunchKernelStmt`（u6-l1 已介绍过分裂成 device/host 两个函数）。具体地：
   - `block_dim = num_warps * 32`（每 warp 32 线程）——编译期常量，写死。
   - `cluster_dim = metadata.cluster_blocks`——编译期常量，写死。
   - `grid_dim = metadata.grid_blocks`——**可以是函数参数的表达式**（如 `cdiv(n, block_elems)`），此时它被「烘焙」成主机侧 `launch` 函数体内的一条计算语句，由运行时实参算出具体值，而非 Python 计算。

   随后 hidet 的 `generate_launch_func` pass 确保这个主机侧函数最终以 `launch` 为名导出，于是 `CompiledProgram` 能按 `module["launch"]` 取到它。

> **一句话总结**：Python 侧只负责「挑出哪些实参要透传（kernel_params）」，并触发 `launch`；torch→指针的类型转换交给 tvm_ffi；grid/cluster/block 的装配早在编译期就变成了 `.so` 里的 C 代码。

#### 4.2.2 核心流程

一次 `kernel(a, b, c, n)` 的完整调用链（核心节点）：

```
InstantiatedScript.__call__(a, b, c, n)        # instantiated_script.py
   │
   ├── extract_keys(...) → (jit_key, tuning_key)   # 分三类参数
   ├── 查 self.dispatch_table[(jit_key, tuning_key)]
   │       命中 → 直接拿到 compiled_func（tvm_ffi.Function）
   │       未命中 → JitInstance._pick_best_program(args)
   │                       └── 选出最优 CompiledProgram
   │                             compiled_func = compiled_program.get_launch_func()
   │
   ├── kernel_args = (args[i] for i in self.kernel_params)   # 只透传 kernel 参数
   │
   └── compiled_func(*kernel_args)              # ← 进入 tvm_ffi，跨语言
            │
            │  tvm_ffi 把 torch.Tensor → DLTensor → 设备指针；int → 标量
            ▼
        C 侧 launch(params...)                  # .so 里的 public 函数
            │
            ├── (可选) set_kernel_max_dynamic_smem_bytes
            │
            └── LaunchKernelStmt(grid_dim, cluster_dim, block_dim, shared_mem)
                    │
                    └── cudaLaunchKernel(...)   # 真正上 GPU
```

两个关键点用公式强化记忆。grid 维度由 `Metadata.grid_blocks` 决定，它是参数的表达式：

\[
\text{grid\_dim} = (\,\texttt{cdiv}(n,\, \text{block\_elems}),\; 1,\; 1\,) \quad\text{（vector_add 示例）}
\]

block 维度恒为 warp 数乘 32：

\[
\text{block\_dim} = \text{num\_warps} \times 32
\]

后者是编译期常量；前者在主机侧 `launch` 里由运行时 `n` 求值。

#### 4.2.3 源码精读

**上层入口 `InstantiatedScript.__call__`**——这是 `kernel(...)` 的真正实现：

```python
def __call__(self, *args, **kwargs):
    ...
    # extract the JIT key and the tuning key
    keys = extract_keys(args, self.const_params, self.tuning_params)
    # check if the compiled function exists
    compiled_func: Optional[tvm_ffi.Function] = self.dispatch_table.get(keys, None)
    if compiled_func is None:
        # slow path
        jit_key, tuning_key = keys
        ...
        compiled_program = jit_instance._pick_best_program(args)
        compiled_func = compiled_program.get_launch_func()
        self.dispatch_table[(jit_key, tuning_key)] = compiled_func
    # call the compiled function
    kernel_args = (args[i] for i in self.kernel_params)
    ret = compiled_func(*kernel_args)
    return ret
```

[python/tilus/lang/instantiated_script.py:824-858](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/instantiated_script.py#L824-L858) 中文说明：①先用 `extract_keys` 把实参算成 `(jit_key, tuning_key)`（u8-l2 详述）；②查内存 dispatch 表，命中直接取 `compiled_func`，未命中走慢路径调 `_pick_best_program` 选最优 program 再取 `get_launch_func()`；③**只把 `kernel_params` 对应的实参透传**给 `compiled_func`。注意 `compiled_func` 的类型就是 `tvm_ffi.Function`，与 `CompiledProgram.get_launch_func()` 返回的一致——所以这里拿到的 `compiled_func` 与 4.1 里那个 `launch` 是同一个东西。

**为什么只透传 `kernel_params`？** 因为常量参数（const，如 matmul 的 `M/N/K`）已经在编译期烘焙进内核了，运行时再传没有意义；只有指针参数（`~dtype`）和整数 `DataType` 参数（如 vector_add 的 `n`，既是调优参数又是 kernel 参数）需要在运行时给到内核。看 `CallParameters.extract_params` 的三分逻辑：

```python
if annotation in [bool, int, float, str]:
    self.const_params.append(index)          # 编译期常量
else:
    self.kernel_params.append(index)         # 运行时透传
    if isinstance(annotation, DataType) and annotation.is_integer():
        self.tuning_params.append(index)    # 同时也是调优参数（kernel_params 的子集）
```

[python/tilus/lang/instantiated_script.py:282-288](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/instantiated_script.py#L282-L288) 中文说明：常量进 `const_params`；其余（`DataType`/`PointerType`）进 `kernel_params`，其中整数 `DataType` 同时进 `tuning_params`。所以 `kernel_params` = 所有运行时需要传给内核的参数（指针 + 整数标量），`tuning_params` ⊂ `kernel_params`。

**调优时的 launch 调用**也走同一个 `get_launch_func`，只是包了 benchmark：

```python
compiled_func = compiled_program.get_launch_func()
kernel_args = [
    args[j].clone() if isinstance(args[j], torch.Tensor) else args[j]
    for j in self.call_params.kernel_params
]
lat = benchmark_func(
    lambda: compiled_func(*kernel_args),
    warmup=..., repeat=...,
)
```

[python/tilus/lang/instantiated_script.py:728-746](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/instantiated_script.py#L728-L746) 中文说明：调优阶段对每个候选 schedule 取出它的 `launch` 函数，用克隆过的张量（避免被原地写脏）反复调用测延迟。再次印证：**所有路径最终都汇聚到 `compiled_func(*kernel_args)` 这一行**。

**torch → 指针的转换谁来做？** 注意上面把 `torch.Tensor` 直接塞进 `compiled_func(*kernel_args)`，而 `compiled_func` 是 `tvm_ffi.Function`。Tilus 仓库里没有任何一行 Python 代码做 `tensor.data_ptr()` 再传指针——这件事由 `tvm_ffi` 的 PackedFunc 编组（argument marshalling）完成：它识别 DLPack 张量，取 `data` 字段作为设备指针。这也是为什么 `Tensor.data_ptr()`（见 4.3.3）存在但运行时主路径不调用它——它是给调试/低层互操作用的。

**grid/cluster/block 在哪确定？** 看 codegen 的 `visit_Function` 如何把 Metadata 写进函数属性：

```python
self._builder = FunctionBuilder(
    name=func.name + "_kernel",
    kind="cuda_kernel" ...,
    grid_dim=self._function.metadata.grid_blocks,        # 来自 Metadata
    cluster_dim=cluster_blocks,                           # 来自 Metadata.cluster_blocks
    block_dim=func.metadata.num_warps * 32,               # num_warps × 32
    ...
)
```

[python/tilus/backends/codegen.py:209-217](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/backends/codegen.py#L209-L217) 中文说明：设备 kernel 函数的 `grid_dim/cluster_dim/block_dim` 属性直接取自 `Metadata`。`Metadata` 的字段定义在 [python/tilus/ir/func.py:45-51](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/func.py#L45-L51)，其中 `grid_blocks: tuple[Expr, Expr, Expr]` 是「参数的表达式」、`cluster_blocks: tuple[int,int,int]` 是整数常量、`num_warps: int` 是整数常量。

随后主机侧 `launch_kernel` 把这些属性烘焙成 `LaunchKernelStmt`：

```python
kernel_args = list(self.host_builder.params) + list(self.extra_params)
cluster_dim = kernel_func.attrs.cluster_dim if kernel_func.attrs.cluster_dim is not None else 1
self.host_builder.append(
    LaunchKernelStmt(
        func_var=func_var,
        args=kernel_args,
        grid_dim=normalize_dim3(kernel_func.attrs.grid_dim),
        cluster_dim=normalize_dim3(cluster_dim),
        block_dim=normalize_dim3(kernel_func.attrs.block_dim),
        shared_mem=int32(dynamic_shared_bytes),
        target="cuda",
    )
)
```

[python/tilus/backends/codegen.py:177-189](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/backends/codegen.py#L177-L189) 中文说明：主机侧把 `grid/cluster/block/shared_mem` 全部装进一条 `LaunchKernelStmt`，连同 `kernel_args`（= 函数参数 + 额外参数如 workspace 指针）一起发起内核。注意 `grid_dim` 是表达式，会在主机侧 `launch` 里由实参求值；`block_dim` 是 `num_warps*32` 常量。这一段就是「启动配置烘焙」发生的地方，运行时不再参与。

**为什么 `module["launch"]` 一定能取到？** hidet 的 `generate_launch_func` pass 保证模块里有一个名为 `launch` 的 `public` 函数——若只有一个主机函数，直接重命名为 `launch`：

```python
if len(host_funcs) == 1:
    old_name, host_func = host_funcs[0]
    renamed = Function(name="launch", params=host_func.params, body=host_func.body, ...)
    return ir_module.with_removed_functions([old_name]).with_added_functions({"launch": renamed})
```

[python/tilus/hidet/transforms/generate_launch_func.py:116-128](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/hidet/transforms/generate_launch_func.py#L116-L128) 中文说明：Tilus 每个 program 恰好有一个主机函数（即 codegen 生成的那份），所以走这条「重命名成 `launch`」的分支。这正是 `CompiledProgram` 用 `compiled_module["launch"]` 取函数的契约保证。若有多份，则改走 `add_launch_func` 另生成一个 `launch`（[generate_launch_func.py:57-101](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/hidet/transforms/generate_launch_func.py#L57-L101)），其内部同样发出 `LaunchKernelStmt`。

#### 4.2.4 代码实践

**目标**：跟踪一次 `kernel(...)` 调用，把「从 Python 实参到 CUDA launch」的完整调用链落到具体源码行，并验证 torch 张量是被 tvm_ffi 而非 Tilus 代码转换的。

**操作步骤**：

1. **阅读型跟踪**（不依赖 GPU）。打开 [instantiated_script.py:824-858](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/instantiated_script.py#L824-L858)，对照下面这张表，在源码里逐行标注每一跳：

| 步骤 | 位置 | 发生的事 |
| --- | --- | --- |
| 1 | `__call__` L837 | `extract_keys` 算 `(jit_key, tuning_key)` |
| 2 | `__call__` L840 | 查 `dispatch_table`，命中跳到步骤 6 |
| 3 | `__call__` L850 | 未命中时 `_pick_best_program` 选最优 program |
| 4 | `__call__` L851 | `compiled_program.get_launch_func()` 拿到 `tvm_ffi.Function` |
| 5 | `__call__` L855 | `kernel_args` 只取 `kernel_params` 下标的实参 |
| 6 | `__call__` L856 | `compiled_func(*kernel_args)` 进入 tvm_ffi |
| 7 | `.so` 内 `launch` | tvm_ffi 把 torch 张量→设备指针，调用 C `launch` |
| 8 | `LaunchKernelStmt` | 主机侧 `cudaLaunchKernel`，grid/cluster/block 已烘焙 |

2. **验证「Tilus 不做指针转换」**。在仓库里搜索运行时主路径是否调用 `data_ptr`：

```bash
rg "data_ptr" python/tilus/lang/instantiated_script.py python/tilus/runtime/
```

预期：`instantiated_script.py` 里**没有** `data_ptr` 调用（它只 `clone()` 张量后整体透传）；`runtime/` 下也没有。`data_ptr` 只出现在 `tensor.py`（见 4.3.3）等工具/调试处。这证明 torch→指针是 tvm_ffi 干的。

3. **观察 grid 由实参求值**（若可运行）。重新加载 vector_add，在缓存目录 `module/source.cu` 里找到主机侧 `launch` 函数，确认它接收 `n` 作为参数，并在内部用 `n` 计算 grid 维度（形如 `(n + block_elems - 1) / block_elems`），而不是一个写死的常数。

**需要观察的现象**：步骤 2 的搜索无运行时主路径命中；步骤 3 的 `launch` C 函数体里 grid 表达式含 `n`。

**预期结果**：调用链表格每行都能在源码定位到；`source.cu` 里 `launch` 的 grid 计算依赖运行时 `n`，而 block 维度是常数 `num_warps*32`。

> 待本地验证：`source.cu` 的确切写法依 nvcc/hidet 版本而异；若无法运行，重点完成步骤 1、2 的源码阅读。

#### 4.2.5 小练习与答案

**练习 1**：假设把 vector_add 的 `n` 标注从 `int32` 改成 `int`（常量参数），运行时调用链会有什么变化？

**参考答案**：`n` 会进入 `const_params` 而非 `kernel_params`。于是 `extract_keys` 把 `n` 的精确值塞进 `jit_key`（每个不同 `n` 都触发一次重编译），并且 `kernel_args` 不再包含 `n`——`compiled_func(*kernel_args)` 不传 `n`。同时主机侧 `launch` 的 grid 表达式在编译期就能求值为常数（因为 `n` 是编译期已知的）。这正是 u2-l1/u8-l2 强调的「常量烘焙」。

**练习 2**：为什么调优 benchmark 时要对 torch 张量做 `.clone()`（见 4.2.3 第二段代码），而正式 `__call__` 不 clone？

**参考答案**：调优会对同一个候选反复调用很多次测延迟；某些内核会原地写输出（如累加器）。若不 clone，前一次 benchmark 写脏的数据会影响后续测量甚至破坏用户张量。正式调用只执行一次且就是要把结果写进用户提供的输出张量，所以不能 clone（clone 会把结果写到副本里、用户拿不到）。

**练习 3**：`Metadata.grid_blocks` 是 `tuple[Expr, Expr, Expr]`（表达式），而 `cluster_blocks` 是 `tuple[int, int, int]`（整数）。这个类型差异反映了什么设计约束？

**参考答案**：grid 维度允许依赖运行时参数（如 `cdiv(n, block)`），所以用 `Expr` 表达、在主机侧 `launch` 里求值；而 cluster（Hopper/Blackwell 的线程块簇）维度必须是编译期确定的硬件配置，所以限定为整数常量。这与 codegen 里 `cluster_dim` 直接取常量、`grid_dim` 走 `normalize_dim3(Expr)` 的处理一致。

---

### 4.3 张量工具函数：from_torch / empty / torch

#### 4.3.1 概念说明

运行时要把用户的 `torch.Tensor` 喂给内核，也需要在主机侧创建、查看、转换 Tilus 张量。`python/tilus/tensor.py` 提供了这套**主机侧张量工具**。它的核心是一个轻量 `Tensor` 类——注意它和 IR 里的 `RegisterTensor/SharedTensor/GlobalTensor`（u3-l4、u4-l1）**不是一回事**：后者是编译期 IR 节点，描述内核内部的数据载体；这里的 `Tensor` 是运行期 Python 对象，本质是「一个 torch 存储 + Tilus dtype + 形状」的三元组视图，用来在主机侧准备数据、校验结果。

它解决三个问题：

1. **桥接 torch 与 Tilus dtype**：torch 用自己的 dtype 枚举，Tilus 有任意位宽类型（u1-l4），需要互转。
2. **承载低精度类型的存储视图**：subbyte 类型（如 int4）无法直接用 torch dtype 表示，只能用 `uint8` 存储再按位解读，所以 `Tensor` 把「逻辑 shape/dtype」与「物理 storage」解耦。
3. **提供构造原语**：`empty/rand/ones/zeros/randn/randint/full/from_torch` 等工厂，以及 `.torch()/.to()/.view()/.data_ptr()` 等方法。

#### 4.3.2 核心流程

`Tensor` 的数据模型：

```
Tensor
├── dtype: DataType          # Tilus 逻辑类型（含任意位宽）
├── shape: Sequence[int]     # 逻辑形状
└── storage: torch.Tensor    # 物理存储（通常是一维 uint8，device="cuda"）
```

构造路径有两类：

- **从已有 torch 张量包装**（零拷贝）：`from_torch(t)` 直接把 torch 张量当 storage，dtype 由 torch dtype 映射而来。
- **新建存储**：`empty(shape, dtype)` 先按位宽算字节数 `(nbits*prod(shape)+7)//8`，开一段 `uint8` CUDA 存储再包装。

读取路径：`.torch()` 把 storage 按可表示的 torch dtype `view` 回去（要求 dtype 能映射到 torch）；不可直接映射的低精度类型则经 `cast` 先转成标准类型（见 `__str__` 的做法）。

#### 4.3.3 源码精读

**`Tensor` 类与 `data_ptr`**：

```python
class Tensor:
    def __init__(self, dtype: DataType, shape: Sequence[int], storage: torch.Tensor):
        self.dtype: DataType = dtype
        self.shape: Sequence[int] = shape
        self.storage: torch.Tensor = storage
    ...
    def data_ptr(self) -> int:
        return self.storage.data_ptr()
```

[python/tilus/tensor.py:27-99](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/tensor.py#L27-L99) 中文说明：`Tensor` 就是「dtype + shape + storage」三元组；`data_ptr()` 委托给底层 torch storage。注意 4.2 讲过，运行时主路径不调用 `data_ptr`——torch 张量是整体透传给 tvm_ffi 的；这个方法主要供低层互操作/调试使用。

**`from_torch`——最常用的桥接**：

```python
def from_torch(torch_tensor: torch.Tensor) -> Tensor:
    dtype = dtype_from_torch(torch_tensor.dtype)
    return Tensor(dtype, torch_tensor.shape, torch_tensor)
```

[python/tilus/tensor.py:102-104](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/tensor.py#L102-L104) 中文说明：把 torch dtype 转成 Tilus dtype（`dtype_from_torch`），形状直接取 torch 的，storage 就是原 torch 张量——**不拷贝数据**，仅建视图。这是用户把数据交给 Tilus 内核的标准入口。

**`empty`——按位宽开存储**：

```python
def empty(shape: Sequence[int], dtype: DataType) -> Tensor:
    nbytes = (dtype.nbits * prod(shape) + 7) // 8
    storage = torch.empty([nbytes], dtype=torch.uint8, device="cuda")
    return Tensor(dtype, shape, storage)
```

[python/tilus/tensor.py:112-115](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/tensor.py#L112-L115) 中文说明：关键在第一行——按 dtype 的**位数**（`nbits`，对 subbyte 类型也成立）算字节数并向上取整，然后开一段一维 `uint8` CUDA 存储。低精度类型没有对应的 torch dtype，所以统一用 `uint8` 作物理载体，逻辑 dtype/shape 记在 `Tensor` 上。字节数公式：

\[
\text{nbytes} = \left\lceil \frac{\text{nbits} \times \prod \text{shape}}{8} \right\rceil
\]

**`torch()`——把 Tilus 张量读回 torch**：

```python
def torch(self) -> torch.Tensor:
    torch_dtype = dtype_to_torch(self.dtype)
    if torch_dtype is None:
        raise ValueError("PyTorch does not support dtype {} for now.".format(self.dtype.name))
    return self.storage.view(torch_dtype).reshape(self.shape)
```

[python/tilus/tensor.py:87-91](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/tensor.py#L87-L91) 中文说明：反向桥接。把 `uint8` 存储 `view` 成对应 torch dtype 再 `reshape` 回逻辑形状。若 Tilus dtype 无法映射到 torch（如 int4），直接抛错——这种情况要走 `cast` 先转成可表示类型（`__str__` 与 `to()` 就是这么做的，[tensor.py:93-96](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/tensor.py#L93-L96) 与 [tensor.py:33-44](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/tensor.py#L33-L44)）。

**`view_torch`——带显式 dtype/shape 的视图**（处理 subbyte）：

```python
def view_torch(torch_tensor: torch.Tensor, *, dtype: DataType, shape: List[int]) -> Tensor:
    assert (dtype.nbits * prod(shape) + 7) // 8 == torch_tensor.nbytes
    return Tensor(dtype, shape, torch_tensor)
```

[python/tilus/tensor.py:107-109](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/tensor.py#L107-L109) 中文说明：让用户显式指定逻辑 dtype/shape 来解读一段 torch 存储（常用于把 `uint8` 存储解读成若干 int4 元素），断言保证位数守恒。

#### 4.3.4 代码实践

**目标**：用 `from_torch/empty/torch()` 走一遍「准备输入 → 喂给内核 → 读回结果」的闭环，体会 `Tensor` 作为主机侧视图的角色。

**操作步骤**（以 vector_add 为例，若无可运行内核则改为纯张量工具练习）：

```python
import torch, tilus
from tilus import tensor as tt

# 1. 用 from_torch 准备输入（零拷贝视图）
a_torch = torch.randn(1024, device="cuda", dtype=torch.float32)
a = tt.from_torch(a_torch)
print(a.dtype, a.shape)            # float32 [1024]
print(a.data_ptr() == a_torch.data_ptr())   # True，确实是同一块存储

# 2. 用 empty 准备输出（按位宽开存储）
c = tt.empty([1024], tilus.float32)
print(c.storage.dtype, c.storage.shape)     # torch.uint8 [4096]（=1024*4 字节）

# 3.（若有内核）调用内核后，用 torch() 读回
# Kernel(a, b, c, 1024)
# print(torch.allclose(c.torch(), a_torch + b_torch))

# 4. 低精度练习：把 16 个 int4 元素塞进 8 字节
raw = torch.empty([8], dtype=torch.uint8, device="cuda")
i4 = tt.view_torch(raw, dtype=tilus.int4b, shape=[16])
print(i4.dtype.nbits, i4.shape)    # 4 [16]
```

**需要观察的现象**：
- `from_torch` 后 `data_ptr` 相等，证明零拷贝。
- `empty` 的 storage 是 `uint8` 且字节数 = `nbits*prod(shape)/8`。
- `view_torch` 用 8 字节装下 16 个 4-bit 元素。

**预期结果**：上述 print 输出与注释一致。

> 待本地验证：第 3 步依赖具体内核可用；前两步与第 4 步是纯张量工具，无需 GPU 计算即可验证存储模型。

#### 4.3.5 小练习与答案

**练习 1**：`from_torch` 与 `empty` 在「是否分配新存储」上有何区别？为什么这样设计？

**参考答案**：`from_torch` 不分配，直接复用传入 torch 张量作 storage（零拷贝），因为它代表「用户已有的数据」；`empty` 会 `torch.empty` 新开一段 `uint8` CUDA 存储，因为它要为内核的输出/中间结果提供一块新空间。一个用于输入桥接，一个用于输出分配。

**练习 2**：为什么 `Tensor.storage` 总是 `uint8` 一维，而不是直接用逻辑 dtype 的 torch 张量？

**参考答案**：因为 Tilus 支持任意位宽（含 subbyte 如 int4/float6）类型，而 torch 没有 4-bit/6-bit dtype。用 `uint8` 作「按字节寻址的原始载体」，把逻辑 dtype 与 shape 解耦地记在 `Tensor` 上，才能统一承载标准类型与低精度类型；位数守恒由 `empty`/`view_torch` 的断言保证。

---

## 5. 综合实践

把本讲三块内容串起来，完成一次「端到端跟踪 + 工具使用」的综合任务。

**任务**：写一段脚本，显式地用底层 API 重现一次内核执行的运行时路径，并在每一步打印「证据」。

```python
import tilus, torch
tilus.option.cache_dir("/tmp/tilus-track")

# ① 用张量工具准备输入/输出
from tilus import tensor as tt
n = 2048
a = tt.from_torch(torch.randn(n, device="cuda", dtype=torch.float32))
b = tt.from_torch(torch.randn(n, device="cuda", dtype=torch.float32))
c = tt.empty([n], tilus.float32)

# ② 走高层 kernel(...) 触发编译，并填好缓存
from examples.vector_add.vector_add import VectorAdd
VectorAdd(a.torch() if False else a.storage.view(torch.float32),
          b.storage.view(torch.float32),
          c.storage.view(torch.float32), n)

# ③ 现在显式用运行时 API 重新加载同一内核
from tilus.runtime import load_compiled_program, compiled_program_exists
from pathlib import Path
# 找到 programs/<hash> 目录（可用 glob）
prog_dir = next(Path("/tmp/tilus-track/tilus/programs").iterdir())
assert compiled_program_exists(prog_dir)
cp = load_compiled_program(prog_dir)

# ④ 直接调用 launch 函数，验证与高层调用等价
launch = cp.get_launch_func()
c.storage.zero_()
launch(a.storage.view(torch.float32), b.storage.view(torch.float32), c.storage.view(torch.float32), n)
print("等价校验:", torch.allclose(c.torch(), a.torch() + b.torch()))
```

**要回答的问题**（在脚本注释里写清结论）：

1. 步骤 ③ 的 `load_compiled_program` 之后，Python 侧有没有再读 `Metadata` 或算 grid？（应无）
2. 步骤 ④ 传给 `launch` 的是 torch 张量还是裸指针？谁做了转换？（torch 张量；tvm_ffi）
3. 把 `n` 改成另一个值（如 4096，且能被 block_elems 整除），步骤 ④ 还能直接复用同一个 `launch` 吗？为什么？（能，因为 `n` 是 kernel/tuning 参数，grid 在主机侧由 `n` 求值；但若 `n` 标注是 `int` 常量则不能，需要重编译。）

> 待本地验证：路径、导入与 `examples/vector_add` 的接口依环境而定。重点是理解每一步对应源码的哪个函数（`from_torch`/`empty` → `InstantiatedScript.__call__` → `_pick_best_program` → `get_launch_func` → tvm_ffi → C `launch` → `LaunchKernelStmt`）。

## 6. 本讲小结

- **`CompiledProgram` 是个薄壳**：它只做两件事——用 `tvm_ffi.load_module` 加载 `module/lib.so`，按名字取出 `launch` 函数；调用内核就是调用这个函数。它不读 `Metadata`、不算 grid、不转指针。
- **`build_and_load_program` 在 `drivers.py` 而非 `runtime/`**：它串起「编译（`build_program` 返回目录）+ 加载（`load_compiled_program`）」，加载永远轻量。
- **`kernel(...)` 的调用链**：`InstantiatedScript.__call__` → 算 `(jit_key, tuning_key)` → 查/选最优 program → `get_launch_func()` → 只透传 `kernel_params` → `compiled_func(*kernel_args)`。所有路径最终都汇聚到「调用 `launch`」这一行。
- **torch→指针由 tvm_ffi 完成**：Tilus 运行时主路径不调用 `data_ptr`，而是把 `torch.Tensor` 整体交给 `tvm_ffi.Function`，由其 PackedFunc 编组机制经 DLPack 零拷贝取设备指针。
- **grid/cluster/warps 在编译期烘焙**：`codegen.visit_Function` 把 `Metadata`（`grid_blocks` 表达式 / `cluster_blocks` 常量 / `num_warps*32`）写进主机侧 `LaunchKernelStmt`；`generate_launch_func` pass 再保证它以 `launch` 之名导出。grid 可由运行时实参在主机侧求值，block/cluster 是常量。
- **`tensor.py` 是主机侧视图工具**：`Tensor = (dtype, shape, storage)`，`from_torch` 零拷贝桥接、`empty` 按位宽开 `uint8` 存储、`torch()`/`view_torch` 处理（含 subbyte 的）回读，与 IR 里的张量节点是不同层面的概念。

## 7. 下一步学习建议

- **调试运行时产物**：下一讲 u8-l4 会讲 `debug.dump_ir`、`disable_ptxas_opt` 与 ncu/nsys 剖析。结合本讲，你可以打开 `dump_ir` 去缓存目录的 `module/ir/` 里亲眼看到主机侧 `launch` 函数被生成出来的过程，验证 4.2 讲的「烘焙」。
- **读生成的 `source.cu`**：挑一个已编译内核，打开 `programs/<hash>/module/source.cu`，找到 `launch` 函数体，对照本讲的 `LaunchKernelStmt` 理解 grid/cluster/block/shared_mem 如何落到 C 代码。
- **深入 tvm_ffi**（可选）：本讲多次提到 torch→指针是 tvm_ffi 做的。若想彻底弄清编组细节，可阅读 `tvm_ffi` 的 PackedFunc 与 DLPack 文档，理解 `DLTensor->data` 如何成为内核参数。
- **回看 u6-1**：本讲 4.2 的 codegen 部分依赖 u6-l1 的「设备/主机函数分裂」。若对 `visit_Function` 的双 builder 仍有疑问，建议重读 u6-l1 的源码精读段。
