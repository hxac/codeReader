# Python 包入口与 torch 导入流程

## 1. 本讲目标

当你写下 `import torch` 这一行时，背后其实发生了一长串事情：版本号被加载、一批动态链接库被预加载进进程、一个用 C++ 写的扩展模块被挂载到 Python 命名空间、成百上千个算子符号被注册出来。

本讲学完后，你应该能够：

- 说出 `torch/__init__.py` 在导入过程中的关键执行顺序。
- 解释 `_running_with_deploy`、`USE_GLOBAL_DEPS`、`USE_RTLD_GLOBAL_WITH_LIBTORCH` 这几个开关的作用。
- 描述 C 扩展模块 `torch._C` 是如何被 Python 找到并初始化的（从 `PyInit__C` 到 `initModule`）。
- 看懂 `__all__` 是如何被动态扩展出来的，以及为什么 `torch.add`、`torch.Tensor` 这些符号在导入后就能直接用。

本讲是后续所有讲义的「入口基石」——只有先搞清楚 `torch` 这个包是怎么被组装起来的，后面读 Tensor、算子分发、autograd 才不会「悬在半空」。

## 2. 前置知识

阅读本讲前，你只需要掌握以下概念（不熟悉的术语我们会在用到时解释）：

- **模块（module）**：Python 中一个 `.py` 文件或一个 C 扩展，导入后成为一个命名空间对象。
- **动态链接库（shared library / `.so` / `.dll` / `.dylib`）**：一段编译好的机器码，可以在程序运行时被加载并调用。PyTorch 的 C++ 后端就是一堆 `.so`。
- **C 扩展（C extension）**：用 C/C++ 写、按 Python 规定的接口编译出来的模块，Python 可以像 import 普通 `.py` 一样 import 它。`torch._C` 就是这样一个 C 扩展。
- **`ctypes.CDLL`**：Python 标准库提供的加载动态库的工具，可以在不写 C 代码的情况下手动 `dlopen` 一个 `.so`。
- **`RTLD_GLOBAL`**：Linux 动态加载器的一个标志，表示「把这块库的符号放进全局符号表，让后面加载的其他库也能看到」。这是理解 `USE_GLOBAL_DEPS` 的关键。
- **`__all__`**：一个模块里定义的列表，约定「`from module import *` 时该导出哪些名字」。

承接上一讲（u1-l3 仓库目录结构）已经建立的心智模型：`torch/` 是 Python 前端包，`torch/csrc/` 是 Python↔C++ 绑定的 C++ 源码，`c10/`/`aten/` 是更底层的 C++ 后端。本讲要回答的核心问题是：**这几个目录是怎么在 `import torch` 的一瞬间被串起来的。**

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `torch/__init__.py` | `import torch` 的总入口。负责版本加载、全局依赖预加载、`_C` 导入、顶层符号组装。本讲主线。 |
| `torch/torch_version.py` | 定义 `TorchVersion` 类，把字符串版本号包装成可比较对象，赋给 `torch.__version__`。 |
| `torch/_utils_internal.py` | 一组「可被覆盖」的内部工具：`get_file_path`、`USE_GLOBAL_DEPS`、`USE_RTLD_GLOBAL_WITH_LIBTORCH` 等。 |
| `torch/_utils.py` | 另一组通用工具，如 `_import_dotted_name`、`classproperty`，在 `__init__.py` 顶部被导入。 |
| `torch/csrc/Module.cpp` | `_C` 扩展模块的 C++ 实现，定义了 `initModule()`——把成百上千个 C++ 类型和函数注册到 Python 的地方。 |
| `torch/csrc/stub.c` | 一个极薄的桥接文件，把 Python 规定的入口名 `PyInit__C` 转发到 `initModule()`。 |
| `setup.py` | 打包配置，说明 `_C` 扩展和 `libtorch_python.so` 是如何随包分发的。 |

## 4. 核心概念与源码讲解

我们按导入时间线把本讲拆成四个最小模块：

1. 顶部导入与版本加载（最先发生）。
2. 全局依赖加载：`USE_GLOBAL_DEPS` 与 `_load_global_deps`（在 import `_C` 之前）。
3. `torch._C` 的 C++ 初始化入口：`PyInit__C` → `initModule`（import `_C` 的瞬间）。
4. 顶层符号组装：`from torch._C import *` 与 `__all__` 扩展（import `_C` 之后）。

### 4.1 顶部导入与版本加载

#### 4.1.1 概念说明

当 Python 解释器执行 `import torch` 时，第一步是定位并执行 `torch/__init__.py`。这个文件非常长（几千行），但它的「开头」做了三件奠基性的事：

- 导入一批标准库和自己的小工具函数。
- 计算并暴露 `__version__`。
- 定义初始的 `__all__` 列表（注意：只是「初始」，后面还会被动态扩展）。

理解这一段的关键是：**`torch.__init__.py` 的顶部 import 顺序是有讲究的**。它会先 import 不依赖 C 扩展的纯 Python 小工具（`_utils`、`_utils_internal`），再用这些工具去加载 C 扩展。如果顺序反了，就会出现「还没加载 `_C` 就想用它」的循环错误。

#### 4.1.2 核心流程

```
import torch 触发执行 torch/__init__.py
        │
        ├─ 1. 导入标准库（os, sys, ctypes, typing ...）
        ├─ 2. 定义 _running_with_deploy()（遗留开关，恒返回 False）
        ├─ 3. from torch._utils import ...           # 纯 Python 工具
        ├─ 4. from torch._utils_internal import ...  # 含 USE_GLOBAL_DEPS 等开关
        ├─ 5. from torch.torch_version import __version__  # 版本号
        ├─ 6. 定义初始 __all__（并断言它已排序）
        └─ 7. （后续模块）开始加载 _C ...
```

#### 4.1.3 源码精读

文件一开始是标准库导入，紧接着是一个看起来很不起眼但被注释特别说明的函数 [torch/__init__.py:47-51](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/torch/__init__.py#L47-L51)：

```python
# As a bunch of torch.packages internally still have this check
# we need to keep this. @todo: Remove tests that rely on this check as
# they are likely stale.
def _running_with_deploy() -> builtins.bool:
    return False
```

这个函数在开源（OSS）版本里**永远返回 `False`**。它存在的意义是区分「普通安装的 PyTorch」和 Meta 内部的 `torchdeploy` 部署模式（一种把多个 Python 解释器塞进一个进程的服务器部署方式）。在 OSS 里它是占位符，保留它只是因为代码里其它地方还在调用它。这是一个典型的「为兼容性保留的空壳」。

随后是关键的工具导入 [torch/__init__.py:54-66](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/torch/__init__.py#L54-L66)：

```python
from torch._utils import (
    _functionalize_sync as _sync,
    _import_dotted_name,
    classproperty,
)
from torch._utils_internal import (
    get_file_path,
    prepare_multiprocessing_environment,
    profiler_allow_cudagraph_cupti_lazy_reinit_cuda12,
    USE_GLOBAL_DEPS,
    USE_RTLD_GLOBAL_WITH_LIBTORCH,
)
from torch.torch_version import __version__ as __version__
```

注意这里 `torch._utils` 和 `torch._utils_internal` 是纯 Python 模块，导入它们不会触发 C 扩展加载——所以放在最前面是安全的。其中 `_import_dotted_name` 是一个「按点分路径逐层取属性」的小工具 [torch/_utils.py:589-594](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/torch/_utils.py#L589-L594)：

```python
def _import_dotted_name(name):
    components = name.split(".")
    obj = __import__(components[0])
    for component in components[1:]:
        obj = getattr(obj, component)
    return obj
```

它的作用是实现类似 `import a.b.c` 的动态导入，后面注册算子文档时会用到。

版本号的加载值得单独看一眼。`__version__` 并不是一个普通字符串，而是被包装过的 `TorchVersion` 对象 [torch/torch_version.py:66](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/torch/torch_version.py#L66)：

```python
__version__ = TorchVersion(internal_version)
```

`TorchVersion` 继承自 `str`（所以打印出来还是普通字符串），但额外支持与 `packaging.version.Version` 对象和元组做比较 [torch/torch_version.py:11-27](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/torch/torch_version.py#L11-L27)。这样 `torch.__version__ > "2.0"` 和 `torch.__version__ >= (2, 1)` 都能正确工作，既向后兼容又语义正确。

接下来是初始 `__all__`，它是一个手写的字符串列表 [torch/__init__.py:82-155](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/torch/__init__.py#L82-L155)，并在后面紧跟一个排序断言 [torch/__init__.py:158-159](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/torch/__init__.py#L158-L159)：

```python
# Please keep this list sorted
if __all__ != sorted(__all__):
    raise AssertionError("__all__ must be kept sorted")
```

这个断言是一个「开发者护栏」：它强迫任何往 `__all__` 加名字的人都必须按字母序插入，否则导入直接报错。这是一种用运行时检查维护代码风格的常见技巧。

#### 4.1.4 代码实践

1. **实践目标**：确认版本号和初始 `__all__` 的行为。
2. **操作步骤**：
   ```bash
   cd <仓库根目录>
   python -c "import torch; print(torch.__version__); print(type(torch.__version__))"
   python -c "import torch; print(torch.__version__ > '2.0'); print(torch.__version__ >= (2, 0))"
   ```
3. **需要观察的现象**：第一行打印版本字符串（如 `2.x.0`），但其类型不是 `str` 而是 `torch.torch_version.TorchVersion`；第二行两个比较都应打印 `True`。
4. **预期结果**：`TorchVersion` 虽然是 `str` 子类，但能和字符串、元组正确比较。
5. **待本地验证**：上述比较结果取决于你本地安装的 PyTorch 版本号，若版本号解析格式特殊请以实际输出为准。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `from torch._utils_internal import ...` 必须在 `from torch._C import *` 之前执行？

> **参考答案**：因为 `torch._utils_internal` 提供了 `USE_GLOBAL_DEPS` 等开关，而这些开关决定了「如何加载 `_C`」（是否先预加载全局依赖）。如果反过来，加载 `_C` 时还没有这些开关可用，逻辑无法分支。

**练习 2**：把 `__all__` 列表里某个名字改成乱序，重新 import torch 会发生什么？

> **参考答案**：导入会在 [torch/__init__.py:158-159](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/torch/__init__.py#L158-L159) 的断言处抛出 `AssertionError("__all__ must be kept sorted")`，整个 `import torch` 失败。

### 4.2 全局依赖加载：`USE_GLOBAL_DEPS` 与 `_load_global_deps`

#### 4.2.1 概念说明

`torch._C` 这个 C 扩展背后链接着一大堆动态库：`libtorch_cpu`、`libtorch_cuda`（如果有 GPU）、`libc10`、以及 CUDA 的 `libcudart`、`libcublas` 等等。问题来了：当 Python 去 `dlopen` `torch/_C.cpython-*.so` 时，操作系统需要能找到这些依赖。

在某些安装方式下（例如把 CUDA 库作为 pip 依赖 `nvidia-*-cu12` 安装），这些库不在默认搜索路径上，直接 import 会失败。PyTorch 的解决办法是：**在 import `_C` 之前，先用 `ctypes.CDLL` 手动预加载一个「全局依赖库」**，让它的符号以 `RTLD_GLOBAL` 方式进入进程全局符号表，从而让后续 `_C` 能顺利解析到所有依赖。

这个「全局依赖库」就是 `libtorch_global_deps.so`。是否启用这套机制，由 `USE_GLOBAL_DEPS` 这个开关控制。

#### 4.2.2 核心流程

```
USE_GLOBAL_DEPS == True ?
        │
        ├─ True  ──► _load_global_deps():
        │              ├─ 找到 torch/lib/libtorch_global_deps.so
        │              ├─ (可选) 预加载 ROCm 运行时
        │              └─ ctypes.CDLL(..., mode=RTLD_GLOBAL)   ← 符号进全局表
        │
        └─ False ──► 跳过，直接依赖环境里的 LD_LIBRARY_PATH
                       （部分 FB 内部构建用这条路径）
        │
        ▼
from torch._C import *    ← 此时 _C 能解析到所有 C++ 依赖
```

`USE_GLOBAL_DEPS` 与另一个开关 `USE_RTLD_GLOBAL_WITH_LIBTORCH` 是「两条互斥的路线」：要么用「预加载 global deps」（默认），要么用「直接以 `RTLD_GLOBAL` 加载整个 libtorch」（少数特殊场景）。

#### 4.2.3 源码精读

开关定义在 `torch/_utils_internal.py`，注释把意图说得很清楚 [torch/_utils_internal.py:272-277](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/torch/_utils_internal.py#L272-L277)：

```python
# USE_GLOBAL_DEPS controls whether __init__.py tries to load
# libtorch_global_deps, see Note [Global dependencies]
USE_GLOBAL_DEPS = True
# USE_RTLD_GLOBAL_WITH_LIBTORCH controls whether __init__.py tries to load
# _C.so with RTLD_GLOBAL during the call to dlopen.
USE_RTLD_GLOBAL_WITH_LIBTORCH = False
```

为什么这两个开关写在 `_utils_internal.py` 而不是 `__init__.py`？因为这个文件的设计意图就是「集中放置可被覆盖的行为」——在 Meta 内部构建环境里，`_utils_internal.py` 会被一个等价但行为不同的文件替换，从而改变这些开关的值。文件顶部有一段说明 [torch/_utils_internal.py:32-36](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/torch/_utils_internal.py#L32-L36)：

```python
# this arbitrary-looking assortment of functionality is provided here
# to have a central place for overridable behavior. The motivating
# use is the FB build environment, where this source file is replaced
# by an equivalent.
```

同文件里的 `get_file_path` 是一个简单的路径拼接工具 [torch/_utils_internal.py:43-44](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/torch/_utils_internal.py#L43-L44)：

```python
def get_file_path(*path_components: str) -> str:
    return os.path.join(torch_parent, *path_components)
```

其中 `torch_parent` 是仓库/安装包里 `torch` 目录的父目录 [torch/_utils_internal.py:37-40](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/torch/_utils_internal.py#L37-L40)。它被用来定位 `torch/lib` 下的库文件、`torch/bin` 下的可执行文件等。

真正的加载逻辑在 `__init__.py` 里 [torch/__init__.py:468-516](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/torch/__init__.py#L468-L516)，核心是这几行 [torch/__init__.py:473-477](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/torch/__init__.py#L473-L477)：

```python
    lib_ext = ".dylib" if platform.system() == "Darwin" else ".so"
    lib_name = f"libtorch_global_deps{lib_ext}"
    here = os.path.abspath(__file__)
    global_deps_lib_path = os.path.join(os.path.dirname(here), "lib", lib_name)
```

它在 `torch/lib/` 下找到 `libtorch_global_deps.so`（macOS 是 `.dylib`），然后 [torch/__init__.py:486](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/torch/__init__.py#L486)：

```python
        ctypes.CDLL(global_deps_lib_path, mode=ctypes.RTLD_GLOBAL)
```

`mode=ctypes.RTLD_GLOBAL` 是整段代码的灵魂：它让这个库依赖的所有符号都暴露到进程全局符号表，这样后面加载 `_C.so` 时，它链的 `libtorch_cpu` 等库就能在全局表里找到这些符号。如果加载失败（`OSError`），代码会尝试预先加载 CUDA 依赖再重试 [torch/__init__.py:512-516](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/torch/__init__.py#L512-L516)：

```python
    except OSError as err:
        # Can happen for wheel with cuda libs as PYPI deps
        _preload_cuda_deps(err)
        ctypes.CDLL(global_deps_lib_path, mode=ctypes.RTLD_GLOBAL)
```

最后，决定走哪条路线的是这段分支 [torch/__init__.py:519-558](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/torch/__init__.py#L519-L558)：

```python
if (USE_RTLD_GLOBAL_WITH_LIBTORCH or os.getenv("TORCH_USE_RTLD_GLOBAL")) and (
    platform.system() != "Windows"
):
    # Do it the hard way. ...
    sys.setdlopenflags(os.RTLD_GLOBAL | os.RTLD_LAZY)
    from torch._C import *  # noqa: F403
    sys.setdlopenflags(old_flags)
else:
    # Easy way. ...
    if USE_GLOBAL_DEPS:
        _load_global_deps()
    from torch._C import *  # noqa: F403
```

绝大多数 OSS 用户走的是 `else` 分支（「Easy way」）：`USE_GLOBAL_DEPS=True`，先 `_load_global_deps()`，再 `from torch._C import *`。注释把两条路线的区别讲得很直白：「Hard way」（`RTLD_GLOBAL_WITH_LIBTORCH`）会把 libtorch 的所有 C++ 符号都暴露到全局表，容易和其它 C++ 库冲突导致「神秘的 segfault」，所以只在 `UBSAN`、`fbcode` 等特殊场景才用。

#### 4.2.4 代码实践

1. **实践目标**：观察全局依赖库的加载行为，并理解 `USE_GLOBAL_DEPS` 的作用。
2. **操作步骤**：
   ```bash
   # 1) 看看安装目录下是否真有这个库
   python -c "import torch, os; print(os.path.join(os.path.dirname(torch.__file__), 'lib', 'libtorch_global_deps.so'))"

   # 2) 用环境变量观察加载（Linux）
   LD_DEBUG=libs python -c "import torch" 2>&1 | grep -i global_deps | head
   ```
3. **需要观察的现象**：第 1 步应打印出 `.../torch/lib/libtorch_global_deps.so` 的真实路径；第 2 步（Linux）的 `LD_DEBUG=libs` 输出里能看到动态加载器在加载 `_C.so` 之前先解析了 `libtorch_global_deps.so`。
4. **预期结果**：确认该 `.so` 存在并被加载。Windows 上 `_load_global_deps` 会直接 `return`（不走这条路径，改由 `_load_dll_libraries` 处理）。
5. **待本地验证**：`LD_DEBUG=libs` 仅在 Linux/glibc 可用；macOS/Windows 请用各自平台的等价工具或跳过第 2 步。

#### 4.2.5 小练习与答案

**练习 1**：`USE_GLOBAL_DEPS` 和 `USE_RTLD_GLOBAL_WITH_LIBTORCH` 同时为 `True` 会有什么效果？

> **参考答案**：在 [torch/__init__.py:519](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/torch/__init__.py#L519) 的判断里，只要 `USE_RTLD_GLOBAL_WITH_LIBTORCH` 为真就走「Hard way」分支，此时 `USE_GLOBAL_DEPS` 根本不会被检查（它在 `else` 分支里）。所以两者同时为真时实际走 Hard way，`_load_global_deps` 不会被执行。

**练习 2**：为什么 `_load_global_deps` 在 Windows 上直接 `return`？

> **参考答案**：Windows 用另一套 DLL 搜索机制，由前面的 `_load_dll_libraries()`（[torch/__init__.py:180-302](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/torch/__init__.py#L180-L302)）通过 `os.add_dll_directory` 把 `torch/lib` 等目录加进搜索路径来处理，不需要 `libtorch_global_deps` 这套 `RTLD_GLOBAL` 机制。

### 4.3 `torch._C` 的 C++ 初始化入口：`PyInit__C` → `initModule`

#### 4.3.1 概念说明

`from torch._C import *` 这一行真正触发了 C 扩展的加载。Python 加载一个 C 扩展时，会去这个 `.so` 里找一个名字固定的入口函数 `PyInit__<模块名>`。对于 `torch._C`，这个函数就是 `PyInit__C`（注意有两个下划线：一个是 `PyInit_` 前缀，一个是模块名 `_C` 的前导下划线）。

但 PyTorch 没有把几千行初始化代码直接塞进 `PyInit__C`，而是用一个只有十几行的「桩文件」`stub.c` 把调用转发到一个真正的 `initModule()` 函数（定义在 `Module.cpp` 里）。这种「薄桩 + 真实现」的分层是为了让 `initModule` 也能被非 Python 的部署方式（如 `torchdeploy`）复用。

#### 4.3.2 核心流程

```
from torch._C import *
        │
        ▼  Python 在 _C.so 中查找入口符号
PyInit__C()              [stub.c]
        │  return initModule();
        ▼
initModule()             [Module.cpp]
        ├─ c10::initLogging()
        ├─ 创建 PyModuleDef，名字 = "torch._C"
        ├─ PyModule_Create → 得到 module 对象
        ├─ 初始化所有类型对象：
        │     THPDtype_init / THPLayout_init / THPDevice_init /
        │     THPVariable_initModule / THPFunction_initModule / ...
        ├─ 绑定各子系统：
        │     jit / dynamo / functorch / profiler / cuda / mps / xpu / ...
        ├─ at::init()   ← 真正初始化 ATen 后端
        └─ return module
        │
        ▼
torch._C 现在是一个装满符号的 Python 模块
```

#### 4.3.3 源码精读

先看那个薄得不能再薄的桩文件 [torch/csrc/stub.c:1-15](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/torch/csrc/stub.c#L1-L15)：

```c
#include <Python.h>

extern PyObject* initModule(void);

#ifndef _WIN32
#ifdef __cplusplus
extern "C"
#endif
__attribute__((visibility("default"))) PyObject* PyInit__C(void);
#endif

PyMODINIT_FUNC PyInit__C(void)
{
  return initModule();
}
```

注意三点：
- `extern PyObject* initModule(void);` 声明了真正的实现来自别处（`Module.cpp`）。
- `__attribute__((visibility("default")))` 保证 `PyInit__C` 这个符号在 `.so` 里是导出的（默认 hidden 可见性下，不加这个 Python 会找不到入口）。
- `extern "C"` 让 C++ 编译器不要对函数名做 name mangling，这样 Python 用 `dlsym("PyInit__C")` 才能精确匹配。

真正的 `initModule` 在 [torch/csrc/Module.cpp:2491-2493](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/torch/csrc/Module.cpp#L2491-L2493)：

```cpp
extern "C" TORCH_PYTHON_API PyObject* initModule();
// separate decl and defn for msvc error C2491
PyObject* initModule() {
```

它先创建一个名为 `"torch._C"` 的 Python 模块对象 [torch/csrc/Module.cpp:2530-2541](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/torch/csrc/Module.cpp#L2530-L2541)：

```cpp
  static struct PyModuleDef torchmodule = {
      PyModuleDef_HEAD_INIT,
      "torch._C",
      nullptr,
      sizeof(TorchModuleState),
      methods.data(),
      ...
  };
  module = PyModule_Create(&torchmodule);
  ASSERT_TRUE(module);
```

`methods.data()` 是一张巨大的方法表，里面每一项都把一个 C 函数注册成 `_C` 的方法，比如 [torch/csrc/Module.cpp:2029-2031](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/torch/csrc/Module.cpp#L2029-L2031)：

```cpp
static std::initializer_list<PyMethodDef> TorchMethods = {
    {"_log_api_usage_once", LogAPIUsageOnceFromPython, METH_O, nullptr},
    {"_initExtension", THPModule_initExtension, METH_O, nullptr},
```

这里 `_initExtension` 被绑定到 C++ 函数 `THPModule_initExtension`——记住这个名字，第 4 个模块会用到。

创建完模块对象后，是一长串类型对象的初始化 [torch/csrc/Module.cpp:2551-2566](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/torch/csrc/Module.cpp#L2551-L2566)：

```cpp
  ASSERT_TRUE(THPGenerator_init(module));
  ASSERT_TRUE(THPException_init(module));
  THPSize_init(module);
  THPDtype_init(module);       // 注册所有 dtype（float32/int64...）
  THPDeviceInfo_init(module);
  THPLayout_init(module);      // 注册 layout（strided/sparse...）
  THPMemoryFormat_init(module);
  THPQScheme_init(module);
  THPDevice_init(module);      // 注册 Device 类型
  THPStream_init(module);
  THPEvent_init(module);
  ...
  ASSERT_TRUE(THPVariable_initModule(module));  // 注册 Tensor 类型！
  ASSERT_TRUE(THPFunction_initModule(module));  // 注册 autograd Function
  ASSERT_TRUE(THPEngine_initModule(module));    // 注册 autograd 引擎
```

`THPVariable_initModule` 就是把 `Tensor` 这个 Python 类型注册进来的地方（后续 u2-l1 会深入）。接着是各子系统的绑定 [torch/csrc/Module.cpp:2572-2591](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/torch/csrc/Module.cpp#L2572-L2591)：

```cpp
  torch::jit::initJITBindings(module);          // TorchScript
  torch::dynamo::initDynamoBindings(module);    // torch.compile
  torch::functorch::impl::initFuncTorchBindings(module);  // torch.func
  ...
  torch::_export::initExportBindings(module);   // torch.export
  torch::inductor::initAOTIRunnerBindings(module);  // AOTI
```

可以看到，几乎每一个高级子系统都是在 `_C` 模块初始化时把它的 Python 绑定挂上去的。最后强制初始化 ATen 后端 [torch/csrc/Module.cpp:2680](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/torch/csrc/Module.cpp#L2680)：

```cpp
  // force ATen to initialize because it handles
  // setting up TH Errors so that they throw C++ exceptions
  at::init();
```

文件末尾还有一个「重复加载守卫」，防止同一份 `_C.so` 被加载两次导致崩溃 [torch/csrc/Module.cpp:3422-3440](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/torch/csrc/Module.cpp#L3422-L3440)：

```cpp
static void pytorch_duplicate_guard() {
  static int initialized = 0;
  if (initialized) {
    fmt::print(stderr, "pytorch: _C shared library re-initialized\n");
    abort();
  }
  initialized = 1;
}
struct call_duplicate_guard {
  call_duplicate_guard() { pytorch_duplicate_guard(); }
};
static call_duplicate_guard _call_duplicate_guard;
```

`_call_duplicate_guard` 是一个全局静态对象，它的构造函数在 `.so` 被加载时（早于 `PyInit__C`）就会执行一次，从而把「是否已加载」记录下来。

补充分发视角：`setup.py` 告诉我们 `_C` 扩展文件随包分发，且它由 CMake 编译产出 [setup.py:1201-1207](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/setup.py#L1201-L1207)：

```python
    torch_package_data = [
        "py.typed",
        # torch._C is built by CMake (torch/CMakeLists.txt) and installed into
        # torch/.  Match this interpreter's exact extension suffix, not a glob: ...
        f"_C{sysconfig.get_config_var('EXT_SUFFIX')}",
```

`EXT_SUFFIX` 形如 `.cpython-311-x86_64-linux-gnu.so`，所以你会在安装目录里看到 `torch/_C.cpython-311-...so` 这样的文件——这就是 `import torch._C` 真正加载的东西。它背后链接的 `libtorch_python.so` 也被打包进去 [setup.py:1253-1258](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/setup.py#L1253-L1258)。

#### 4.3.4 代码实践

1. **实践目标**：确认 `_C` 是一个 C 扩展模块，并观察它的真实文件路径。
2. **操作步骤**：
   ```bash
   python -c "import torch; print(torch._C); print(torch._C.__file__)"
   ```
3. **需要观察的现象**：第一行打印类似 `<module 'torch._C' from '.../torch/_C.cpython-XX-...so'>`；第二行打印这个 `.so` 的完整路径。
4. **预期结果**：`torch._C.__file__` 指向一个真实的 `.so` 文件（而非 `torch/_C/` 目录下的 `.py`）。
5. **延伸**：如果你曾在仓库源码目录里直接 `pip install`（非 `-e`）导致 `import torch` 报「loaded the `torch/_C` folder rather than the C extensions」，对照 [torch/__init__.py:1481-1497](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/torch/__init__.py#L1481-L1497) 的报错提示就能理解原因——Python 把源码里的 `torch/_C/`（纯 Python 包目录）当成了 `_C` 模块。

#### 4.3.5 小练习与答案

**练习 1**：为什么需要 `stub.c` 这个文件？能不能直接把 `PyInit__C` 的实现写在 `Module.cpp` 里？

> **参考答案**：可以，但用一个独立的薄桩文件能让 `initModule()` 成为「与 Python 入口无关」的普通函数，从而被 `torchdeploy` 等非标准 Python 嵌入环境复用（这些环境不通过 `PyInit_` 入口加载）。这也是 `initModule` 被声明为 `extern "C" TORCH_PYTHON_API` 导出的原因。

**练习 2**：`at::init()` 为什么要在 `initModule` 末尾被「强制」调用？

> **参考答案**：根据注释 [torch/csrc/Module.cpp:2678-2680](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/torch/csrc/Module.cpp#L2678-L2680)，是为了让 ATen 提前设置好 TH Error 到 C++ 异常的转换机制，否则后续算子出错时报错信息会不正确。

### 4.4 顶层符号组装：`from torch._C import *` 与 `__all__` 扩展

#### 4.4.1 概念说明

当第 4.2 节的 `from torch._C import *` 执行完毕后，`_C` 模块里那些 C++ 注册的符号（`Tensor`、各种算子、`dtype`、`Device` 等）就被「倒」进了 `torch` 命名空间。但导入流程还没结束：`__init__.py` 还要再做一些「清理」工作：

- 用 `_initExtension` 当哨兵，检测 `_C` 是否真的加载成功，失败时给出友好的安装错误提示。
- 遍历 `_C` 里的所有公开符号，把它们补进 `__all__`，并修正它们的 `__module__` 属性（让帮助文档显示成 `torch.xxx` 而不是 `torch._C.xxx`）。
- 把 `_C` 的子模块注册进 `sys.modules`，让 pickle 等机制能正确找到它们。
- 最后显式调用 `_C._initExtension(...)`，完成 layout/dtype/storage/autograd 等的 Python 侧二次初始化。

#### 4.4.2 核心流程

```
from torch._C import *          ← 4.2 已完成
        │
        ├─ try: from torch._C import _initExtension   ← 哨兵：能导入说明 _C 真的可用
        │   except ImportError: 给出 install vs develop 的友好提示
        ├─ from torch import _C as _C                 ← 显式引用，安抚 linter
        ├─ for name in dir(_C):                        ← 遍历 _C 所有符号
        │       if 公开(非下划线开头, 非 ...Base):
        │           __all__.append(name)
        │           修正 __obj.__module__ = "torch"
        ├─ _import_extension_to_sys_modules(_C)        ← 子模块注册进 sys.modules
        └─ _C._initExtension(_manager_path())          ← 二次初始化（layout/dtype/storage/...）
```

#### 4.4.3 源码精读

首先是那个哨兵检查 [torch/__init__.py:1473-1498](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/torch/__init__.py#L1473-L1498)：

```python
# Check to see if we can load C extensions, and if not provide some guidance
# on what the problem might be.
try:
    # _initExtension is chosen (arbitrarily) as a sentinel.
    from torch._C import _initExtension
except ImportError:
    import torch._C as _C_for_compiled_check

    if _C_for_compiled_check.__file__ is None:
        raise ImportError(
            textwrap.dedent(
                """
                Failed to load PyTorch C extensions:
                    It appears that PyTorch has loaded the `torch/_C` folder
                    ...
                    This error can generally be solved using the `develop` workflow
                        $ python -m pip install --no-build-isolation -v -e . && python -c "import torch"
                ...
                """
            ).strip()
        ) from None
    raise  # If __file__ is not None the cause is unknown, so just re-raise.
```

这里 `_initExtension` 被当成「任意选一个符号来试探 `_C` 是否真的可用」的哨兵。如果连它都导入失败，且 `_C.__file__ is None`（说明 Python 把源码目录 `torch/_C/` 当成了 `_C` 模块，而不是编译产物），就给出经典的「请用 `-e` 可编辑安装」提示。这正是上一讲（u1-l2）讲过的 `pip install -e .` 之所以必要的根因。

接下来是把 `_C` 的公开符号补进 `__all__` 的循环 [torch/__init__.py:1500-1523](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/torch/__init__.py#L1500-L1523)：

```python
# The torch._C submodule is already loaded via `from torch._C import *` above
# Make an explicit reference to the _C submodule to appease linters
from torch import _C as _C


__name, __obj = "", None
for __name in dir(_C):
    if __name[0] != "_" and not __name.endswith("Base"):
        __all__.append(__name)
        __obj = getattr(_C, __name)
        if callable(__obj) or inspect.isclass(__obj):
            if __obj.__module__ != __name__:  # "torch"
                # TODO: fix their module from C++ side
                if __name not in {
                    "DisableTorchFunctionSubclass",
                    "DisableTorchFunction",
                    "Generator",
                }:
                    __obj.__module__ = __name  # "torch"
    elif __name == "TensorBase":
        # issue 109438 / pr 109940. Prevent TensorBase from being copied into torch.
        delattr(sys.modules[__name__], __name)

del __name, __obj
```

这段逻辑做了三件事：
1. 遍历 `_C` 里的所有名字，凡是不以下划线开头、不以 `Base` 结尾的，都视为「公开 API」，追加进 `__all__`。这就是为什么 `torch` 命名空间里有成百上千个符号，但 `__init__.py` 里只手写了一小部分——大部分是这里自动收进来的。
2. 把这些符号的 `__module__` 改成 `"torch"`，这样 `help(torch.add)` 显示的归属就是 `torch` 而不是 `torch._C`，对用户更友好。少数几个（`Generator` 等）被显式排除在外。
3. 特殊处理 `TensorBase`：直接从 `torch` 模块删掉它，防止用户直接拿到这个基类（用户应该用 `Tensor`）。

紧接着是一个把 `_C` 子模块注册进 `sys.modules` 的辅助函数 [torch/__init__.py:1525-1546](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/torch/__init__.py#L1525-L1546)：

```python
    def _import_extension_to_sys_modules(module, memo=None):
        ...
        for name in dir(module):
            member = getattr(module, name)
            member_name = getattr(member, "__name__", "")
            if inspect.ismodule(member) and member_name.startswith(module_name):
                sys.modules.setdefault(member_name, member)
                # Recurse for submodules (e.g., `_C._dynamo.eval_frame`)
                _import_extension_to_sys_modules(member, memo)

    _import_extension_to_sys_modules(_C)
```

这一步解决的问题是：C 扩展的「子模块」（如 `_C._dynamo.eval_frame`）不是标准 Python 包，pickle 默认无法 `from _C._dynamo import eval_frame` 导入它们。把它们显式塞进 `sys.modules` 后，pickle 就能按名字找到这些对象了。

最后是真正调用 `_initExtension` 的地方 [torch/__init__.py:2646](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/torch/__init__.py#L2646)：

```python
_C._initExtension(_manager_path())  # pyrefly: ignore[bad-argument-type]
```

它传入 `torch_shm_manager` 可执行文件的路径 [torch/__init__.py:2636-2643](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/torch/__init__.py#L2636-L2643)（共享内存管理器，多进程 DataLoader 会用）。`_initExtension` 在 C++ 侧对应 `THPModule_initExtension`，它完成 Python 侧的二次初始化 [torch/csrc/Module.cpp:237-251](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/torch/csrc/Module.cpp#L237-L251)：

```cpp
  torch::utils::initializeLayouts();
  torch::utils::initializeMemoryFormats();
  torch::utils::initializeQSchemes();
  torch::utils::initializeDtypes();
  torch::tensors::initialize_python_bindings();
  std::string path = THPUtils_unpackString(shm_manager_path);
  libshm_init(path.c_str());

  auto module = THPObjectPtr(PyImport_ImportModule("torch"));
  ...
  THPStorage_postInit(module);
  THPAutograd_initFunctions();
```

注意：dtype/layout/memory_format 的 Python 对象其实是在这里（而不是在 `initModule` 里）才完成最终挂载的——`initModule` 只创建了类型骨架，`_initExtension` 把它们和 Python 侧的常量（如 `torch.float32`）关联起来。

至此，`import torch` 的全部初始化才算真正完成，`torch.add`、`torch.Tensor`、`torch.float32` 等符号全部就绪。

#### 4.4.4 代码实践

1. **实践目标**：观察 `_C` 符号如何被组装进 `torch` 命名空间，并验证 `__module__` 被修正过。
2. **操作步骤**：
   ```bash
   # 1) 看 add 的真实归属：注释里说是 torch，但它其实来自 _C
   python -c "import torch; print(torch.add.__module__); print('add' in torch.__all__)"

   # 2) 数一下 torch 暴露了多少公开符号（远多于 __init__.py 手写的那部分）
   python -c "import torch; print(len(torch.__all__))"

   # 3) 观察子模块是否进了 sys.modules
   python -c "import sys, torch; print('torch._C._dynamo.eval_frame' in sys.modules)"
   ```
3. **需要观察的现象**：第 1 步 `torch.add.__module__` 应为 `torch`（被 4.4 节循环修正过），且 `'add' in torch.__all__` 为 `True`；第 2 步 `__all__` 长度通常是几百；第 3 步为 `True`。
4. **预期结果**：证明绝大多数 `torch.*` 符号是从 `_C` 自动收集来的，且经过了 `__module__` 修正。
5. **待本地验证**：`__all__` 的具体长度取决于版本，但应远大于 `torch/__init__.py` 手写的几十个名字。

#### 4.4.5 小练习与答案

**练习 1**：`torch.add` 这个函数到底定义在哪里？为什么 `torch.add.__module__` 显示 `torch`？

> **参考答案**：它的 C++ 实现在 ATen/`torch/csrc` 里，通过 `_C` 模块注册暴露。Python 侧 `torch.add` 直接来自 `from torch._C import *`。但 [torch/__init__.py:1505-1518](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/torch/__init__.py#L1505-L1518) 的循环把它的 `__module__` 改成了 `"torch"`，所以显示成 `torch` 而非 `torch._C`。

**练习 2**：为什么要在 `_C` 加载完之后才调用 `_C._initExtension`，而不是直接在 `initModule`（C++）里把所有事做完？

> **参考答案**：`_initExtension` 需要反过来 `PyImport_ImportModule("torch")` 拿到 Python 侧的 `torch` 模块对象 [torch/csrc/Module.cpp:245-247](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/torch/csrc/Module.cpp#L245-L247)，然后往它上面挂 `torch.float32` 这类 Python 常量。在 `initModule` 执行时 `torch` 模块还在初始化中、尚未完全就绪，所以必须推迟到 `__init__.py` 主体执行到这里时才调用，形成一个「C++ 建骨架 → Python 收集符号 → C++ 二次填充」的协作循环。

## 5. 综合实践

把本讲四个模块串起来，完成下面这个「`import torch` 全链路追踪」任务：

1. **写一个最小脚本** `trace_import.py`（放在仓库外的任意目录，避免污染源码）：
   ```python
   import torch, os

   # ① 版本与版本类型
   print("version:", torch.__version__, type(torch.__version__).__name__)

   # ② 全局依赖库路径
   gd = os.path.join(os.path.dirname(torch.__file__), "lib", "libtorch_global_deps.so")
   print("global_deps exists:", os.path.exists(gd))

   # ③ _C 是 C 扩展
   print("_C file:", torch._C.__file__)

   # ④ 符号组装证据
   print("add.__module__:", torch.add.__module__)
   print("Tensor.__module__:", torch.Tensor.__module__)
   print("len(__all__):", len(torch.__all__))
   ```

2. **运行并解释每一行输出对应本讲哪个模块**：
   - 第①行对应 4.1（版本加载）。
   - 第②行对应 4.2（`_load_global_deps` 找的库）。
   - 第③行对应 4.3（`PyInit__C` 加载的 `.so`）。
   - 第④行对应 4.4（`__all__` 扩展与 `__module__` 修正）。

3. **回答两个理解性问题**（用本讲源码支撑你的答案）：
   - 如果把 `torch/_utils_internal.py` 里的 `USE_GLOBAL_DEPS` 改成 `False`，`import torch` 会跳过哪一步？是否一定失败？
   - 为什么在仓库源码目录下用 `pip install .`（非 `-e`）安装后 `import torch` 会报「loaded the `torch/_C` folder」？请引用 [torch/__init__.py:1481-1497](https://github.com/pytorch/pytorch/blob/baa92d5d799e0c51216a30d34c2f0058c7ac9936/torch/__init__.py#L1481-L1497) 说明。

> **参考要点**：`USE_GLOBAL_DEPS=False` 时跳过 `_load_global_deps()`，不一定失败——只要环境的 `LD_LIBRARY_PATH` 等已能让 `_C.so` 解析到所有依赖即可（FB 内部构建常这样）；「loaded the `torch/_C` folder」是因为源码树里存在一个纯 Python 的 `torch/_C/` 目录，Python 优先把它当成了 `_C` 模块，导致 `_C.__file__ is None`，触发哨兵报错。

## 6. 本讲小结

- `import torch` 的入口是 `torch/__init__.py`，它先导入纯 Python 工具（`_utils`/`_utils_internal`），再加载版本号和初始 `__all__`，最后才碰 C 扩展——顺序不能乱。
- `_running_with_deploy()` 在 OSS 里恒为 `False`，是为 Meta 内部 `torchdeploy` 部署模式保留的占位开关。
- `USE_GLOBAL_DEPS`（默认 `True`）控制是否在 import `_C` 前用 `ctypes.CDLL(..., RTLD_GLOBAL)` 预加载 `libtorch_global_deps.so`，让后续 C++ 依赖能被全局解析；`USE_RTLD_GLOBAL_WITH_LIBTORCH` 是与之互斥的另一条「Hard way」路线。
- C 扩展 `torch._C` 的 Python 入口是 `stub.c` 里的 `PyInit__C`，它转发到 `Module.cpp` 的 `initModule()`，后者创建模块对象、初始化所有类型（dtype/device/layout/Tensor/autograd 等）、绑定各子系统、并 `at::init()`。
- 导入 `_C` 后，`__init__.py` 用 `_initExtension` 当哨兵检测加载是否成功，遍历 `dir(_C)` 把公开符号补进 `__all__` 并修正 `__module__`，把子模块塞进 `sys.modules`，最后调用 `_C._initExtension(...)` 完成 layout/dtype/storage 的 Python 侧二次初始化。
- `_C` 扩展由 CMake 编译产出（`_C{EXT_SUFFIX}.so`），随包分发，背后链接 `libtorch_python.so`；这也是为什么必须用 `pip install -e .` 可编辑安装——否则 Python 会误把源码里的 `torch/_C/` 目录当成 `_C` 模块。

## 7. 下一步学习建议

本讲把「`import torch` 把 C++ 后端挂进 Python」的过程讲清楚了。接下来建议：

- **进入 Unit 2 学 Tensor**：本讲反复提到的 `THPVariable_initModule` 注册了 `Tensor` 类型。下一讲 u2-l1「Tensor 的 Python 实现」会从 `torch/_tensor.py` 切入，讲清楚 Python 侧 `Tensor` 类如何包装底层 `TensorImpl`。建议先读 `torch/_tensor.py` 开头，再对照本讲的 `_C` 加载流程理解「为什么 Tensor 方法能直接调用底层算子」。
- **追踪一个算子**：本讲看到 `torch.add` 来自 `from torch._C import *`。可以尝试在 u2-l4「算子的 Python 调用路径与 _C 绑定」里完整跟踪一次 `torch.add` 从 Python 到 C++ 的路径，把本讲的「入口」和后续的「分发」连成一条线。
- **延伸阅读源码**：如果想更深入，建议打开 `torch/csrc/Module.cpp` 浏览 `initModule` 里那一长串 `init*Bindings` 调用，挑一个你感兴趣的子系统（如 `torch::jit::initJITBindings` 或 `torch::dynamo::initDynamoBindings`）顺藤摸瓜，这会为 Unit 7（torch.compile）等高级讲义打下基础。
