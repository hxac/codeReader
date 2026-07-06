# 目录结构与代码分层：Python / C++ / JIT 内核

## 1. 本讲目标

通过本讲，你应当能够：

1. 说出 DeepEP 仓库里 `deep_ep/`、`csrc/`、`tests/`、`docs/`、`third-party/` 这几个顶层目录各自的职责。
2. 分清「安装时编译」和「运行时 JIT 编译」两条不同的代码路径，并指出哪些源码走哪一条。
3. 看懂 `import deep_ep` 时 Python 端做了什么初始化，以及 Python 是怎样通过一个名为 `_C` 的扩展模块调到 C++ 的。
4. 画出一次 `buffer.dispatch()` 从 Python 用户代码一路到 GPU kernel 的跨层调用方向图。

本讲只看「森林」不看「树木」：我们只关心每个文件在哪一层、负责什么、被谁调用，不深入任何单个 kernel 的实现细节。具体的 dispatch/combine 流程会在 U5、U6 详解。

## 2. 前置知识

阅读本讲前，建议你已经学完 [`u1-l1`](u1-l1-project-overview.md)，知道 DeepEP 是一个 MoE 专家并行（EP）通信库，核心动作是 dispatch（把 token 发到目标专家所在 rank）和 combine（把专家输出送回原 rank）。

本讲会用到几个常识性的工程概念，先做个最简解释：

- **PyTorch C++ 扩展（C++ extension）**：PyTorch 官方支持用 C++/CUDA 写一段代码，编译成一个 `.so` 共享库，Python 通过 `import` 就能调用里面的函数。这个 `.so` 在 Python 里通常被命名为一个模块（本项目中叫 `deep_ep._C`），底层用 pybind11 把 C++ 函数/类暴露给 Python。
- **pybind11**：一个把 C++ 和 Python 互相粘起来的库。`PYBIND11_MODULE(名字, m) { ... }` 这段宏会定义一个 Python 模块，里面的 `m.def(...)` / `pybind11::class_<...>` 就是在向 Python 注册函数和类。
- **头文件库（header-only）**：指只用 `.h`/`.hpp`/`.cuh` 描述、被源文件 `#include` 后即可参与编译的代码，本身不单独编译成库。
- **JIT（Just-In-Time）编译**：相对「提前编译（AOT）」而言，指在程序运行过程中、用到某个 kernel 时才现场编译它。DeepEP V2 用 JIT 在运行时把大批 CUDA kernel 模板实例化并加载。

## 3. 本讲源码地图

本讲涉及的关键文件及其所属层次：

| 文件 | 所属层 | 作用 |
| --- | --- | --- |
| [`deep_ep/__init__.py`](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/__init__.py) | Python 包入口 | `import deep_ep` 时加载持久化环境变量、做 NCCL 校验、初始化 JIT |
| [`deep_ep/buffers/elastic.py`](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/buffers/elastic.py) | Python 接口层 | V2 的 `ElasticBuffer`、`EPHandle` 与 `dispatch/combine` 用户 API |
| [`csrc/python_api.cpp`](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/python_api.cpp) | C++ 绑定入口 | 定义 pybind11 模块 `_C`，注册 JIT / legacy / elastic 三组 API |
| [`csrc/jit/api.hpp`](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/jit/api.hpp) | C++ JIT 入口 | 暴露给 Python 的 `init_jit`，把库根/CUDA/NCCL 路径注入编译器 |
| [`csrc/elastic/buffer.hpp`](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/elastic/buffer.hpp) | C++ 缓冲区层 | V2 的 `ElasticBuffer` C++ 类与 `dispatch/combine` 的 C++ 实现 |
| [`csrc/kernels/elastic/dispatch.hpp`](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/kernels/elastic/dispatch.hpp) | C++ kernel 启动器 | `launch_dispatch`：生成代码 → 触发 JIT → 启动 GPU kernel |
| [`csrc/jit/launch_runtime.hpp`](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/jit/launch_runtime.hpp) | C++ JIT 框架 | `LaunchRuntime<Derived>` CRTP 模板，统一 generate/launch |
| [`csrc/jit/compiler.hpp`](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/jit/compiler.hpp) | C++ JIT 编译器 | `Compiler::build`：把一段 `.cu` 源码编译成 cubin |
| [`deep_ep/include/deep_ep/impls/dispatch.cuh`](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/impls/dispatch.cuh) | **运行时 JIT 内核源** | 真正的 GPU kernel 模板 `dispatch_impl<...>`，安装时不编译，运行时才 JIT |
| [`setup.py`](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/setup.py) | 构建 | 决定安装时到底编译哪些 `.cpp/.cu`，并把 `include/` 当作数据打包 |

> 提示：注意 [`deep_ep/`](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep) 这个名字同时出现在「Python 包」和「C++ include 前缀（`<deep_ep/...>`）」两个角色里——这是本讲最容易绕晕的地方，第 4 节会专门讲清。

## 4. 核心概念与源码讲解

### 4.1 仓库总体目录结构与职责分层

#### 4.1.1 概念说明

DeepEP 是一个「跨语言、跨编译时机」的项目：

- **跨语言**：用户写的是 Python（PyTorch），但真正跑通信的是 C++/CUDA 内核。两层之间靠一个 pybind11 编译出来的扩展模块 `deep_ep._C` 连接。
- **跨编译时机**：一部分 C++/CUDA 代码在你 `pip install` 时就被编译成 `.so`（**安装时编译**）；另一大批 CUDA kernel 模板则是在你运行 `dispatch()` 时才被现场编译（**运行时 JIT 编译**）。

理解这条分界线，是看懂整个目录布局的钥匙。

#### 4.1.2 顶层目录

仓库根目录下与代码相关的目录如下：

```
DeepEP/
├── deep_ep/            # Python 包（用户 import 的对象）
├── csrc/               # C++/CUDA 源码（编译成 _C 扩展）
├── tests/              # 测试（按 elastic/legacy/utils 分组）
├── docs/               # 文档（legacy.md / nvshmem.md）
├── third-party/        # 第三方依赖（如 fmt）
├── figures/            # README 用的图片
├── setup.py            # 安装/构建入口（编译 _C 扩展）
├── CMakeLists.txt      # 仅用于 IDE/调试，不是安装主路径
├── build.sh / install.sh / develop.sh   # 三种构建脚本
└── README.md
```

需要特别留意 [`CMakeLists.txt`](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/CMakeLists.txt) 顶部那句注释：`this CMake is only for debugging; for setup, please use Torch extension`。也就是说**正式安装走的是 `setup.py`，CMake 仅供 IDE 索引和调试**，初学者不要被它误导。

#### 4.1.3 Python 包 `deep_ep/` 的内部分层

[`deep_ep/`](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep) 这个包里其实装着**两类截然不同的东西**：

```
deep_ep/
├── __init__.py         # 包入口：import 时的初始化逻辑
├── buffers/            # 用户面向的缓冲区类
│   ├── elastic.py      #   V2：ElasticBuffer（本课程主线）
│   └── legacy.py       #   V1：Buffer（NVSHMEM 后端，U9 讲）
├── utils/              # Python 工具：comm/envs/event/find_pkgs/gate/refs...
└── include/deep_ep/    # 【注意】这是 C++/CUDA 头文件，不是 Python！
    ├── common/         #   公共原语：layout/math/ptx/comm/handle...
    └── impls/          #   V2 各 kernel 实现：dispatch/combine/hybrid_*...
```

关键认知：`deep_ep/include/` 虽然住在 Python 包里，但它**全是 C++/CUDA 头文件（`.cuh`）**。这样做的原因是：这些头文件需要随 Python 包一起发布（`pip install` 后跟着 wheel 走），等用户运行时由 JIT 子系统读到并现场编译。具体「随包发布」的机制在 [`setup.py`](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/setup.py) 的 `package_data` 里：

```python
package_data={
    'deep_ep': [
        'include/deep_ep/**/*',
    ]
},
```

即 [`setup.py:202-206`](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/setup.py#L202-L206) 把整个 `include/` 目录当**数据文件**打进包，而不是当源码编译掉。

#### 4.1.4 C++ 源码 `csrc/` 的内部分层

[`csrc/`](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc) 的目录几乎与功能一一对应：

```
csrc/
├── python_api.cpp      # 唯一的 pybind11 模块定义（_C）
├── elastic/            # V2 缓冲区的 C++ 实现
│   ├── buffer.hpp      #   ElasticBuffer 类 + register_apis
│   └── utils.hpp
├── legacy/             # V1 缓冲区的 C++ 实现（NVSHMEM）
├── jit/                # 运行时 JIT 编译子系统（api/compiler/cache/...）
├── kernels/            # kernel 的「启动器」（host 端 generate+build+launch）
│   ├── elastic/        #   V2 启动器：dispatch/combine/barrier/engram/pp
│   ├── legacy/         #   V1 真正的 .cu kernel（安装时编译）
│   └── backend/        #   底层后端：nccl.cu / nvshmem.cu / symmetric.hpp
├── utils/              # C++ 工具：event/hash/format/system...
└── indexing/main.cu    # 仅供 IDE 索引 kernel 符号，不参与运行
```

最容易混淆的两点：

1. `csrc/kernels/elastic/dispatch.hpp` 是「启动器」（host 代码，负责拼代码、调 JIT、配 grid/block），而 `deep_ep/include/deep_ep/impls/dispatch.cuh` 才是「真正的 GPU kernel」（device 代码，运行时被 JIT 实例化）。两者一个在 `csrc/`、一个在 `deep_ep/include/`，名字都叫 dispatch，但角色完全不同。
2. `csrc/kernels/legacy/*.cu` 是 V1 的**安装时编译** kernel，和 V2 的 JIT kernel 走的不是同一条路。

#### 4.1.5 一张「编译时机 × 目录」总览表

| 目录 | 语言 | 何时编译 | 编译产物 |
| --- | --- | --- | --- |
| `deep_ep/__init__.py`、`deep_ep/buffers/*.py`、`deep_ep/utils/*.py` | Python | 解释执行 | 无（`.py`） |
| `csrc/python_api.cpp` + `csrc/elastic/*`、`csrc/legacy/*`、`csrc/jit/*`、`csrc/utils/*`（全是 `.hpp`/`.cpp`） | C++ | 安装时（`setup.py`） | `deep_ep/_C.so` |
| `csrc/kernels/legacy/*.cu`、`csrc/kernels/backend/*.cu` | CUDA | 安装时（`setup.py`） | 同上，编进 `_C.so` |
| `deep_ep/include/deep_ep/**/*.cuh` | CUDA（header-only 模板） | **运行时 JIT** | 临时 `.cubin`，缓存在 `~/.deep_ep` 或 `EP_JIT_CACHE_DIR` |

[`setup.py`](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/setup.py) 里安装时实际编译的源码列表很短（[`setup.py:100`](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/setup.py#L100)）：

```python
sources = ['csrc/python_api.cpp',
           'csrc/kernels/legacy/layout.cu',
           'csrc/kernels/legacy/intranode.cu']
```

后面再按编译开关追加 V1 的 `internode.cu`、`internode_ll.cu`、后端 `nccl.cu`、`nvshmem.cu`、`cuda_driver.cu`（[`setup.py:112-127`](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/setup.py#L112-L127)）。注意：**这张列表里没有任何 V2 的 dispatch/combine kernel**——它们都藏在 `deep_ep/include/` 里等运行时 JIT。

#### 4.1.6 核心流程

用一个极简的「货物打包」比喻总结分层：

```text
你写的 Python 代码
      │  import deep_ep
      ▼
deep_ep/__init__.py  ──►  初始化（NCCL 校验 + JIT 注入路径）
      │  buffer.dispatch(...)
      ▼
deep_ep/buffers/elastic.py  ──►  Python 包装：参数校验、张量分配
      │  self.runtime.dispatch(...)   # self.runtime 是 _C.ElasticBuffer
      ▼
========= 跨越 Python/C++ 边界（pybind11，模块名 _C）=========
      │
      ▼
csrc/elastic/buffer.hpp  ──►  C++ ElasticBuffer::dispatch
      │  launch_dispatch(...)
      ▼
csrc/kernels/elastic/dispatch.hpp  ──►  启动器：generate → build → launch
      │  jit::compiler->build("dispatch", code)
      ▼
csrc/jit/compiler.hpp  ──►  JIT 编译：nvcc 编译生成的 .cu → cubin
      │  cuLaunchKernel
      ▼
deep_ep/include/deep_ep/impls/dispatch.cuh  ──►  真正在 GPU 上跑的 kernel
```

后面三个小节（4.2 / 4.3 / 4.4）会分别拆开 Python 层、C++ 绑定层、以及完整的跨层调用方向。

### 4.2 Python 接口层：`import deep_ep` 与 `ElasticBuffer`

#### 4.2.1 概念说明

绝大多数用户只会接触到 [`deep_ep/__init__.py`](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/__init__.py) 和 [`deep_ep/buffers/elastic.py`](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/buffers/elastic.py)。`__init__.py` 不只是「占位文件」——它在 `import` 的瞬间就执行了一整套初始化逻辑；而 `elastic.py` 里的 `ElasticBuffer` 则是用户拿到通信能力的入口。

#### 4.2.2 核心流程

`import deep_ep` 时，`__init__.py` 大致做四件事：

1. 把安装时烘焙好的持久化环境变量恢复到 `os.environ`（仅当变量未被外部设置时）。
2. 定义并执行 `check_nccl_so()`：校验 PyTorch 运行时加载的 NCCL 与 DeepEP 链接的 NCCL 是同一份二进制。
3. 定义并执行 `init_jit()`：把库根目录、CUDA 路径、NCCL 路径告诉 C++ 端的 JIT 编译器。
4. 在初始化**之后**，再 `from .buffers.elastic import ElasticBuffer, EPHandle` 等真正暴露 API。

把 1～3 放在最前面、把 API 导入放在最后，是为了保证「用户拿到 `ElasticBuffer` 时，底层 JIT 已经准备好」。

#### 4.2.3 源码精读

入口文件 [`deep_ep/__init__.py`](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/__init__.py) 顶部先恢复持久化环境变量（[`deep_ep/__init__.py:11-18`](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/__init__.py#L11-L18)）：

```python
try:
    from .envs import persistent_envs
    for key, value in persistent_envs.items():
        if key not in os.environ:
            os.environ[key] = value
except ImportError:
    pass
```

紧接着是 `init_jit()`，它 `import deep_ep._C` 并把三条路径注入 C++（[`deep_ep/__init__.py:71-80`](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/__init__.py#L71-L80)）：

```python
def init_jit():
    import deep_ep._C as _C
    library_root_path = os.path.dirname(os.path.abspath(__file__))
    _C.init_jit(library_root_path,  # 库根目录（即 deep_ep/ 包目录）
                find_cuda_home(),   # CUDA 安装路径
                find_nccl_root())   # NCCL 安装路径
```

注意这里的 `library_root_path`：它就是 `deep_ep/` 这个包的绝对路径。JIT 编译器靠它才能找到 `deep_ep/include/deep_ep/impls/dispatch.cuh` 这些随包发布的头文件。

随后这两行就是「import 时自动执行初始化」的关键（[`deep_ep/__init__.py:83-84`](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/__init__.py#L83-L84)）：

```python
check_nccl_so()
init_jit()
```

初始化完成之后，才导入对外 API（[`deep_ep/__init__.py:88-95`](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/__init__.py#L88-L95)）：`Buffer`（V1）、`ElasticBuffer` 和 `EPHandle`（V2）、`EventOverlap`、以及从 C++ 扩展直接 re-export 的 `Config`、`topk_idx_t`。

在 [`deep_ep/buffers/elastic.py`](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/buffers/elastic.py) 里，`ElasticBuffer` 在构造时真正「握住」C++ 对象（[`deep_ep/buffers/elastic.py:346-357`](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/buffers/elastic.py#L346-L357)）：

```python
self.runtime = _C.ElasticBuffer(group.rank(), group.size(),
                                self.nccl_comm_handle.get(), cpu_comm,
                                num_bytes, num_cpu_bytes,
                                allow_hybrid_mode, allow_multiple_reduction,
                                prefer_overlap_with_compute,
                                sl_idx, num_allocated_qps,
                                num_cpu_timeout_secs, num_gpu_timeout_secs,
                                self.explicit_destroy)
```

这个 `self.runtime` 就是 Python 持有的 C++ `ElasticBuffer` 实例（pybind11 对象）。此后 `dispatch()` 里那句 `self.runtime.dispatch(...)`（[`deep_ep/buffers/elastic.py:976`](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/buffers/elastic.py#L976)）便是跨越 Python→C++ 边界的那一步。

#### 4.2.4 代码实践

1. **实践目标**：亲眼看到 `import deep_ep` 时确实触发了初始化，并能定位 Python→C++ 的跳转点。
2. **操作步骤**：
   - 打开终端，执行 `python -c "import deep_ep; print(deep_ep.__version__)"`。
   - 设置 `EP_JIT_DEBUG=1` 后再执行一次 `python -c "import deep_ep"`（暂不需要 GPU）。
   - 用编辑器打开 `deep_ep/buffers/elastic.py`，跳到第 346 行与第 976 行。
3. **需要观察的现象**：`import` 能成功并打印版本号；`EP_JIT_DEBUG=1` 时 `import` 阶段通常还**不会**打印 `Generated kernel code:`，因为 JIT 编译发生在**首次调用** `dispatch()` 时而非 `import` 时（JIT 框架里的调试打印见 [`csrc/jit/launch_runtime.hpp:43-44`](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/jit/launch_runtime.hpp#L43-L44)）。
4. **预期结果**：确认 `__version__`（当前为 `'2.1.0'`，见 [`deep_ep/__init__.py:97`](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/__init__.py#L97)），并理解 `dispatch` 的真正入口是 `self.runtime.dispatch`。
5. 若无 GPU/NCCL 环境无法真正 import，请标注「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `from .buffers.elastic import ElasticBuffer` 写在 `check_nccl_so()` / `init_jit()` 之后，而不是写在文件顶部？

> **参考答案**：因为 `elastic.py` 内部会 `import deep_ep._C as _C` 并使用 `_C.ElasticBuffer`，而 `_C.init_jit(...)` 必须先把库根/CUDA/NCCL 路径注入 C++ 端，JIT 编译器才有正确的环境；把导入放在初始化之后，保证用户拿到 `ElasticBuffer` 时底层已就绪。

**练习 2**：`init_jit()` 传入的 `library_root_path` 具体是哪个目录？为什么 JIT 系统需要它？

> **参考答案**：它是 `deep_ep/` 包的绝对路径（`os.path.dirname(os.path.abspath(__file__))`）。JIT 系统需要它来定位随包发布的 `deep_ep/include/deep_ep/impls/*.cuh` 头文件——运行时 nvcc 编译生成的 `.cu` 时要 `#include` 这些头。

### 4.3 C++ 绑定层：`csrc/` 与 `_C` 扩展模块

#### 4.3.1 概念说明

Python 与 C++ 之间靠一个 pybind11 模块连接，它的名字在本项目里被固定为 `_C`。这个模块的「定义文件」有且只有一个：[`csrc/python_api.cpp`](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/python_api.cpp)。它本身不实现任何 EP 逻辑，只做「登记」——把 JIT、V1 legacy、V2 elastic 三组 C++ API 挂到同一个 Python 模块下。

#### 4.3.2 核心流程

`_C` 模块的构建规则（[`csrc/python_api.cpp:10-12`](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/python_api.cpp#L10-L12)）：

```cpp
#ifndef TORCH_EXTENSION_NAME
#define TORCH_EXTENSION_NAME _C
#endif
```

随后 `PYBIND11_MODULE(TORCH_EXTENSION_NAME, m)` 展开就是定义 Python 模块 `deep_ep._C`，其主体只做三件注册（[`csrc/python_api.cpp:31-39`](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/python_api.cpp#L31-L39)）：

```cpp
// JIT API
deep_ep::jit::register_apis(m);
// Register legacy buffer APIs
deep_ep::legacy::register_apis(m);
// Register elastic buffer (DeepEP V2) APIs
deep_ep::elastic::register_apis(m);
```

这三个 `register_apis` 分别定义在：

- [`csrc/jit/api.hpp`](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/jit/api.hpp)：注册 `init_jit`（[`csrc/jit/api.hpp:16-18`](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/jit/api.hpp#L16-L18)）。
- `csrc/legacy/buffer.hpp`：注册 V1 `Buffer` 类。
- [`csrc/elastic/buffer.hpp`](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/elastic/buffer.hpp)：注册 V2 `ElasticBuffer` 类（构造函数、`dispatch`、`combine`、`barrier`、`engram_*`、`pp_*`、`all_gather` 等，见 [`csrc/elastic/buffer.hpp:1346-1382`](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/elastic/buffer.hpp#L1346-L1382)）。

其中 elastic 的注册里，dispatch 的绑定只有一行（[`csrc/elastic/buffer.hpp:1370`](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/elastic/buffer.hpp#L1370)）：

```cpp
.def("dispatch", &ElasticBuffer::dispatch)
```

也就是说 Python 里每调用一次 `self.runtime.dispatch(...)`，最终都进入 C++ 的 `ElasticBuffer::dispatch` 成员函数。这就是「Python↔C++ 绑定」的全貌：`python_api.cpp` 是总入口，三个 `register_apis` 是分入口，`.def("方法名", &C++函数)` 是逐方法绑定。

#### 4.3.3 源码精读：`ElasticBuffer` C++ 类长什么样

打开 [`csrc/elastic/buffer.hpp`](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/elastic/buffer.hpp)，可以看到 V2 缓冲区类的核心字段（[`csrc/elastic/buffer.hpp:13-22`](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/elastic/buffer.hpp#L13-L22)）：

```cpp
class ElasticBuffer {
    // Buffer bytes = GPU buffer + CPU buffer (excludes workspace)
    // Memory layout: [[[Workspace] GPU buffer] CPU buffer]
    int64_t num_buffer_bytes;
    int64_t num_gpu_buffer_bytes;
    int64_t num_cpu_buffer_bytes;
    void* buffer;
    ...
    // NCCL context
    std::shared_ptr<nccl::NCCLSymmetricMemoryContext> nccl_context;
    ...
};
```

这告诉我们两件事：第一，V2 用的是 NCCL 对称内存上下文（`nccl_context`），这对应 u1-l1 提到的「NCCL Gin 后端」；第二，缓冲区在内存里是 `[[[Workspace] GPU buffer] CPU buffer]` 这样一段连续空间。这些细节会在 U3 详解，本讲只需记住：**`ElasticBuffer` 的 C++ 类负责持有 NCCL 上下文与缓冲区指针，并提供 `dispatch/combine` 等 host 方法**。

而 `ElasticBuffer::dispatch` 这个 host 方法真正干活时，并不会自己写通信算法，而是把所有参数打包交给 kernel 启动器 `launch_dispatch(...)`（[`csrc/elastic/buffer.hpp:980`](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/elastic/buffer.hpp#L980)），后者定义在 `csrc/kernels/elastic/dispatch.hpp`。

#### 4.3.4 代码实践

1. **实践目标**：在源码里亲手把「Python 方法名」与「C++ 函数」一一对应起来。
2. **操作步骤**：
   - 在 `csrc/` 下用搜索工具查 `register_apis`，统计共有几个定义（应为 3 个：jit / legacy / elastic）。
   - 在 [`csrc/elastic/buffer.hpp`](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/elastic/buffer.hpp) 的 `register_apis`（第 1346 行起）里数一数 `.def(...)` 的数量，列出 ElasticBuffer 暴露给 Python 的全部方法名。
   - 对照 [`deep_ep/buffers/elastic.py`](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/buffers/elastic.py)，确认每个 Python 用户方法最终都落在某个 `self.runtime.<方法名>(...)` 上。
3. **需要观察的现象**：C++ 注册的方法名与 Python 端调用的方法名完全一致（pybind11 按名字映射）。
4. **预期结果**：得到一张「Python 方法 → C++ 绑定行」对照表，例如 `dispatch → buffer.hpp:1370`、`combine → buffer.hpp:1371`、`barrier → buffer.hpp:1353`。

#### 4.3.5 小练习与答案

**练习 1**：如果我想给 `ElasticBuffer` 新增一个 Python 可调用的方法 `foo()`，至少要改哪几个文件？

> **参考答案**：至少改两处——在 `csrc/elastic/buffer.hpp` 里实现 `ElasticBuffer::foo` 并在 `register_apis` 里加一行 `.def("foo", &ElasticBuffer::foo)`；然后在 `deep_ep/buffers/elastic.py` 里包一层 Python 方法（可选，但通常需要做参数校验和张量管理）。不需要改 `python_api.cpp`，因为它只是调用 `elastic::register_apis(m)`。

**练习 2**：`deep_ep._C` 这个模块名是在哪里定下来的？

> **参考答案**：在 [`csrc/python_api.cpp:10-12`](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/python_api.cpp#L10-L12) 通过 `TORCH_EXTENSION_NAME` 宏定义为 `_C`，并被 `setup.py` 里 `CUDAExtension(name='deep_ep._C', ...)`（[`setup.py:207`](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/setup.py#L207)）采用。

### 4.4 一次 `dispatch()` 的跨层调用方向（把前面三层串起来）

#### 4.4.1 概念说明

前两节分别看了 Python 层和 C++ 绑定层。这一节把它们与 JIT 子系统缝合成一条完整调用链。理解这条链之后，你再去读 U4（JIT 系统）和 U5（dispatch 内核）时，就不会迷失在文件之间。

#### 4.4.2 核心流程

一次 `buffer.dispatch(...)` 从用户调用到 GPU kernel 执行，要穿过五层：

```text
① Python 用户 API     elastic.py  :: ElasticBuffer.dispatch
② Python→C++ 跳转      self.runtime.dispatch  （pybind11，模块 _C）
③ C++ host 实现        csrc/elastic/buffer.hpp :: ElasticBuffer::dispatch → launch_dispatch
④ C++ kernel 启动器    csrc/kernels/elastic/dispatch.hpp :: launch_dispatch
                        ├─ DispatchRuntime::generate(args)   // 生成 .cu 源码
                        ├─ jit::compiler->build("dispatch", code)  // JIT 编译
                        └─ DispatchRuntime::launch(runtime, args, stream)  // 启动
⑤ JIT 编译出的 GPU kernel  deep_ep/include/deep_ep/impls/dispatch.cuh :: dispatch_impl<...>
```

第 ④ 层是整条链的「中枢」：它一边对接 host 缓冲区，一边驱动 JIT 子系统，最后把编译产物丢到 GPU 上跑。

#### 4.4.3 源码精读

**①② Python 层**：`ElasticBuffer.dispatch` 是面向用户的签名，参数包括 `x`、`topk_idx`、`num_experts`、`async_with_compute_stream`、`handle` 等一大串（[`deep_ep/buffers/elastic.py:855`](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/buffers/elastic.py#L855)）。它做完张量分配与参数补全后，在 [`deep_ep/buffers/elastic.py:976`](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/buffers/elastic.py#L976) 调用 `self.runtime.dispatch(...)` 跨入 C++。

**③ C++ host 层**：`ElasticBuffer::dispatch` 负责校验缓冲区大小、清理 host workspace、然后把所有指针和标量交给启动器（[`csrc/elastic/buffer.hpp:980`](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/elastic/buffer.hpp#L980)）：

```cpp
// Do dispatch into the buffers (with SM limitation)
EP_HOST_ASSERT(num_sms <= jit::device_runtime->get_num_sms());
launch_dispatch(x.data_ptr(), sf_ptr, ... );
```

**④ 启动器层**：[`csrc/kernels/elastic/dispatch.hpp`](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/kernels/elastic/dispatch.hpp) 里的 `launch_dispatch` 把参数填进 `DispatchRuntime::Args`，然后是关键的「生成 → 编译 → 启动」三连（[`csrc/kernels/elastic/dispatch.hpp:225-228`](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/kernels/elastic/dispatch.hpp#L225-L228)）：

```cpp
const auto code = DispatchRuntime::generate(args);
const auto runtime = jit::compiler->build("dispatch", code);
DispatchRuntime::launch(runtime, args, stream);
```

其中 `generate` 会根据 `num_scaleout_ranks == 1` 决定实例化「直接 dispatch」还是「hybrid dispatch」模板，并拼出一段极短的 `.cu` 源码（[`csrc/kernels/elastic/dispatch.hpp:51-82`](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/kernels/elastic/dispatch.hpp#L51-L82)）：

```cpp
return fmt::format(R"(
#include <deep_ep/impls/{}.cuh>
using namespace deep_ep::elastic;
static void __instantiate_kernel() {{
    auto ptr = reinterpret_cast<void*>(&{});
}
)", header_name, func_name);
```

这段代码的精髓在于：它本身不做任何事，只是 `#include` 真正的 kernel 头文件并取模板函数 `dispatch_impl<...>`（或 `hybrid_dispatch_impl<...>`）的地址——这一取地址动作会**强迫 nvcc 把对应模板参数组合实例化出来**。所有运行时参数（SM 数、rank 数、hidden、num_topk……）都被填进模板尖括号里，变成编译期常量。

`DispatchRuntime` 本身只是一个继承自 `jit::LaunchRuntime<DispatchRuntime>` 的派生类（[`csrc/kernels/elastic/dispatch.hpp:14`](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/kernels/elastic/dispatch.hpp#L14)）。基类用 CRTP（奇异递归模板模式）把 `generate/launch` 的通用骨架放在 [`csrc/jit/launch_runtime.hpp`](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/jit/launch_runtime.hpp)，派生类只实现 `generate_impl` 和 `launch_impl` 两个钩子（[`csrc/jit/launch_runtime.hpp:29-72`](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/jit/launch_runtime.hpp#L29-L72)）。

**⑤ 真正的 GPU kernel**：被 `#include` 的 [`deep_ep/include/deep_ep/impls/dispatch.cuh`](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/impls/dispatch.cuh) 才是在 GPU 上跑的代码，开头是一长串模板参数（[`deep_ep/include/deep_ep/impls/dispatch.cuh:17`](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/impls/dispatch.cuh#L17)）：

```cpp
template <bool kIsScaleupNVLink,
          bool kDoCPUSync,
          bool kReuseSlotIndices,
          int kNumSMs,
          int kNumNotifyWarps, int kNumDispatchWarps,
          int kNumRanks,
          ...>
```

注意这些参数全部是「编译期常量类型」（`bool`、`int`），这正是 V2 选择 JIT 的根本原因——把 SM 数、rank 数、hidden 等烘焙成编译期常量，能让 nvcc 做激进的寄存器分配、共享内存布局与循环展开优化，性能远超把同样参数当作运行时变量传入的版本。这条调用链的工程含义是：**用户每换一组（hidden/expert/topk/SM）组合，DeepEP 就可能现场编译出一个新特化的 kernel**，并缓存到 `~/.deep_ep`（或 `EP_JIT_CACHE_DIR`）。

#### 4.4.4 代码实践：画出完整依赖图

1. **实践目标**：把本节描述的五层调用链画成一张可点击的依赖图，作为后续阅读 U4/U5 的导航。
2. **操作步骤**：
   - 准备一张白纸或绘图工具。从上到下画 5 个方框，分别标：
     - `deep_ep/buffers/elastic.py`（框内注明 `dispatch @ L855`、`self.runtime.dispatch @ L976`）
     - `deep_ep/__init__.py`（旁注：`import` 时 `init_jit` 注入路径）
     - `csrc/elastic/buffer.hpp`（注明 `register_apis @ L1346`、`ElasticBuffer::dispatch → launch_dispatch @ L980`）
     - `csrc/kernels/elastic/dispatch.hpp`（注明 `launch_dispatch @ L141`、generate/build/launch `@ L225-228`、`generate_impl @ L51`）
     - `csrc/jit/launch_runtime.hpp` + `csrc/jit/compiler.hpp`（注明 `LaunchRuntime<Derived>`、`Compiler::build @ L111`）
     - `deep_ep/include/deep_ep/impls/dispatch.cuh`（注明 `dispatch_impl<...> @ L17`）
   - 用箭头按「①→②→③→④→⑤」连接，并在 ④ 与 ⑤ 之间的箭头上标注「`#include` 并 JIT 实例化」。
   - 在 ② 号箭头上标注「pybind11，模块 `_C`，绑定在 `buffer.hpp:1370`」。
3. **需要观察的现象**：图里能清楚看到「安装时编译的边界」——`csrc/*` 全在边界上方（已编进 `_C.so`），`deep_ep/include/**/*.cuh` 在边界下方（运行时 JIT）。
4. **预期结果**：得到一张与 4.1.6 那张文字流程图同构、但带文件路径与行号的依赖图。建议把它保存下来，后续读 U4/U5 时反复对照。
5. 如果某些跨层跳转在阅读时仍不确定（例如 hybrid 模式额外多一层），可先标注「待确认」，等学完 U5 再补全。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `launch_dispatch` 里要先 `generate` 出一段 `.cu`、再交给 `compiler->build`，而不是在安装时就把 dispatch kernel 编译好？

> **参考答案**：因为 dispatch kernel 是带大量 `int/bool` 模板参数的模板，运行时才能根据真实的 `num_sms / num_ranks / hidden / num_topk / expert_alignment` 等确定要实例化哪个特化版本。把这些参数变成编译期常量能让 nvcc 做极致优化，因此选择「运行时按需 JIT」，并用内容哈希做缓存以避免重复编译。

**练习 2**：`DispatchRuntime::generate` 里 `num_scaleout_ranks == 1` 这个分支条件，对应物理上哪种部署形态？

> **参考答案**：`num_scaleout_ranks == 1` 表示没有跨节点（RDMA）通信，只有单节点内 NVLink 通信，因此选「直接 dispatch」模板（`dispatch.cuh` 的 `dispatch_impl`）；否则走「hybrid dispatch」两级模板（`hybrid_dispatch.cuh`）。这是 U3 拓扑域与 U5 内核链路的核心分叉点，本讲只需记住它由目录里的不同 `.cuh` 文件承载。

## 5. 综合实践

把本讲的三条主线——**目录分层**、**Python↔C++ 绑定**、**跨层调用方向**——合并成一个综合任务：

> **任务：给 DeepEP「加一个永远返回 42 的方法」并追踪它的跨层路径。**

设想你想给 `ElasticBuffer` 增加一个 `answer()` 方法，返回整数 `42`。请在不实际改源码的前提下，在纸上完成：

1. **目录定位**：写出需要修改的文件清单，并标注它们分别属于 4.1.5 表里的哪一行（Python？安装时编译 C++？还是别的）。
2. **Python 层**：写出 `deep_ep/buffers/elastic.py` 里应新增的 Python 方法体（提示：它最终要调用 `self.runtime.answer()`）。
3. **C++ 绑定层**：在 [`csrc/elastic/buffer.hpp`](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/elastic/buffer.hpp) 的 `register_apis`（第 1346 行起）里补一行 `.def("answer", &ElasticBuffer::answer)`，并写出 `ElasticBuffer::answer` 这个 C++ 成员函数的声明位置。
4. **构建影响**：说明这个改动**不需要**触发任何 JIT 重编译，为什么？（提示：它不经过 ④→⑤ 那条 JIT 链。）
5. **验证设计**：写一段最小的 Python 测试，断言 `buffer.answer() == 42`。

完成后再回头看 4.4.2 那张五层调用链：你会发现 `answer()` 只穿过了 ①②③ 三层，根本没碰到 JIT——这正是理解 DeepEP「哪些改动需要重新 JIT、哪些只需重装 `_C`」的训练。

> 说明：本任务为「源码阅读 + 设计型实践」，不需要真实 GPU，也不应真的去改源码（本讲禁止修改源码）。重点是把分层与调用方向想清楚。

## 6. 本讲小结

- DeepEP 顶层目录按职责分为 `deep_ep/`（Python 包 + 随包发布的 C++/CUDA 头）、`csrc/`（安装时编译的 C++/CUDA 源）、`tests/`、`docs/`、`third-party/`，构建主入口是 `setup.py` 而非 `CMakeLists.txt`。
- 同名的 `deep_ep` 既指 Python 包，又指 C++ include 前缀（`<deep_ep/...>`）；`deep_ep/include/deep_ep/impls/*.cuh` 是**运行时 JIT 编译**的 GPU kernel 源，靠 `setup.py` 的 `package_data` 随包发布。
- `import deep_ep` 会自动执行 `check_nccl_so()` 与 `init_jit()`：前者校验 NCCL 二进制一致，后者把库根/CUDA/NCCL 路径注入 C++ 端 JIT 编译器。
- Python 与 C++ 通过唯一一个 pybind11 模块 `_C` 连接，`csrc/python_api.cpp` 是它的总入口，再分派给 jit / legacy / elastic 三个 `register_apis`；`ElasticBuffer.dispatch` 经 `.def("dispatch", &ElasticBuffer::dispatch)` 绑定到 C++。
- 一次 `buffer.dispatch()` 穿过五层：Python 用户 API → `self.runtime.dispatch`（pybind11）→ `ElasticBuffer::dispatch`（host）→ `launch_dispatch` 启动器（generate+build+launch）→ JIT 编译出的 `dispatch_impl<...>` GPU kernel。
- V2 把大量运行时参数（SM 数、rank 数、hidden、num_topk 等）做成 kernel 模板参数，靠运行时 JIT 实例化获得极致优化，这是「`csrc/kernels/` 是启动器、`deep_ep/include/impls/` 才是真 kernel」这一目录分工的根本原因。

## 7. 下一步学习建议

- 想亲手跑通一次完整的 dispatch+combine、并解读输出带宽/延迟，请接着学 [`u1-l4`](u1-l4-run-first-test.md)（运行第一个测试）。
- 想彻底搞懂 `import deep_ep` 里 `check_nccl_so` / `init_jit` / `find_cuda_home` 的细节，去 [`u2-l1`](u2-l1-import-init.md)。
- 对本讲出现的「五层调用链」里 ④ 那段 JIT 编译感兴趣，直接进入 U4，尤其 [`u4-l1`](u4-l1-jit-overview.md)（JIT 系统总览）与 [`u4-l2`](u4-l2-kernel-codegen.md)（内核代码生成）。
- 想理解 `ElasticBuffer` 构造参数与缓冲区布局，去 [`u2-l2`](u2-l2-elastic-buffer-ctor.md) 和 [`u3-l2`](u3-l2-buffer-layout-sizing.md)。
