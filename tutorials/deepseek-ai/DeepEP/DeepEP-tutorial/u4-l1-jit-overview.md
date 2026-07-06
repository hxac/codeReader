# JIT 系统总览：从 init_jit 到 build 的全流程

## 1. 本讲目标

本讲是「核心机制二：运行时 JIT 编译系统」单元的第一篇，目标是带你建立 DeepEP JIT 子系统的**整体地图**，并打通一次内核编译的**端到端主流程**。

学完后你应该能够：

- 说出 `csrc/jit/` 下六大组件（`Compiler`/`NVCCCompiler`、`KernelRuntime`、`KernelRuntimeCache`、`LaunchRuntime`、`DeviceRuntime`、`IncludeParser`）各自的职责，以及它们如何被串成一条流水线。
- 解释 DeepEP **为什么选择运行时 JIT**，而不是像多数 CUDA 项目那样在 `pip install` 阶段就把所有内核编译好。
- 一步步跟踪 `Compiler::build` 从「生成代码」到「加载 cubin」的完整流程：临时目录编译 → `fsync` → 原子重命名 → 内容寻址缓存 → 从 cubin 中提取唯一符号并加载。
- 读懂 `import deep_ep` 时 `_C.init_jit(...)` 这一句如何把 Python 探测到的路径「钉入」C++ 侧的编译器。

本讲**只讲 JIT 的骨架与主流程**，不展开「内核代码生成的模板技巧细节」（那是 u4-l2）、「缓存并发竞态与 cubin 加载的损坏检测深入」（u4-l3）、「启动框架 `LaunchArgs`/`DeviceRuntime`」（u4-l4）。本讲会点到为止，为后续讲义铺路。

---

## 2. 前置知识

### 2.1 编译期常量 vs 运行期变量

CUDA 内核里，如果一个整数（比如 SM 数、rank 数、hidden 维度）被写成**模板参数**，那么 `nvcc` 就能把它当成编译期常量：用它做循环展开、寄存器分配、常量折叠，生成出近乎「为这一组参数量身定制」的高效机器码（SASS）。反之，如果它只是函数的一个普通参数，编译器只能保守地假设它在运行时可能是任意值，很多优化就做不了。

DeepEP 的内核（`dispatch_impl`、`combine_impl` 等）带有十多个这类参数，而它们只有在「用户构造好 buffer、知道集群拓扑和模型配置」之后才能确定。这就产生了一个矛盾：**想要最优代码就要把参数编译期化，但参数要到运行时才知道**。JIT 就是为解开这个矛盾而存在的。

### 2.2 什么是 cubin、nvcc、cuobjdump

- **`nvcc`**：NVIDIA 的 CUDA C++ 编译器，把 `.cu` 源码编译成 GPU 可执行的 `cubin`（CUDA binary）或 PTX 中间表示。
- **`cubin`**：与具体 GPU 架构绑定的二进制内核。运行时可以用 CUDA 驱动 API（`cuModuleLoad`）把它加载进 GPU 并取出内核函数指针来启动。
- **`cuobjdump`**：CUDA 工具链自带的反汇编/符号查看工具，DeepEP 用它从 cubin 里枚举出内核符号名。

### 2.3 内容寻址（content-addressed）缓存

普通缓存用「名字」做 key，而内容寻址缓存用「内容的哈希」做 key。如果两段输入完全相同，哈希就相同，必然命中同一份产物；任何一点改动都会落到一个全新的目录里，互不污染。DeepEP 的 JIT 缓存目录名就是「内核签名 + 标志 + 源码」的哈希摘要。

### 2.4 前置讲义承接

- 本讲依赖 **u2-l1**：那里已经讲过 `import deep_ep` 会依次执行 `check_nccl_so()` 与 `init_jit()`，并且 `find_cuda_home()` / `find_nccl_root()` 把 CUDA 与 NCCL 的根目录路径探测好。本讲就从 `init_jit()` 把这三条路径交到 C++ 编译器手里的那一刻开始。
- 也承接 **u1-l2 / u1-l3**：你已经知道 `deep_ep/include/impls/*.cuh` 是 header-only 模板，只在运行时被 JIT 实例化；而 `csrc/*` 在安装期编译进 `_C.so`。JIT 子系统正属于「安装期编译」的 `_C.so` 内部，它负责在运行时去实例化那些模板。

---

## 3. 本讲源码地图

本讲涉及的文件都在 `csrc/jit/` 与少量周边：

| 文件 | 作用 | 本讲角色 |
|------|------|----------|
| [csrc/jit/api.hpp](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/jit/api.hpp) | JIT 子系统对 Python 暴露的入口：`init()` 与 `register_apis` | 入口，`init_jit` 落点 |
| [csrc/jit/compiler.hpp](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/jit/compiler.hpp) | 编译器基类 `Compiler` 与 `NVCCCompiler` 子类，含 `build()` 主流程 | **本讲主角** |
| [csrc/jit/cache.hpp](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/jit/cache.hpp) | `KernelRuntimeCache`：进程级运行时缓存 | 缓存命中快路径 |
| [csrc/jit/kernel_runtime.hpp](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/jit/kernel_runtime.hpp) | `KernelRuntime`：从 cubin 提取并加载唯一内核符号 | cubin 加载 |
| [csrc/jit/launch_runtime.hpp](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/jit/launch_runtime.hpp) | `LaunchRuntime<Derived>`（CRTP）：`generate`/`launch` 两段式 | 代码生成入口 |
| [csrc/jit/device_runtime.hpp](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/jit/device_runtime.hpp) | `DeviceRuntime`：缓存 GPU 属性（SM 数、架构、时钟） | 决定 `--gpu-architecture` |
| [csrc/jit/include_parser.hpp](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/jit/include_parser.hpp) | `IncludeParser`：递归解析 `<deep_ep/*>` 头文件并算哈希 | 缓存签名的一部分 |
| [csrc/utils/lazy_init.hpp](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/utils/lazy_init.hpp) | `LazyInit<T>`：首次解引用才构造的单例包装 | 各组件的惰性单例 |
| [csrc/kernels/elastic/dispatch.hpp](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/kernels/elastic/dispatch.hpp) | 一个具体的 `LaunchRuntime` 派生类 `DispatchRuntime` | 观察 generate→build→launch 三连 |
| [deep_ep/__init__.py](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/__init__.py) | Python 侧 `init_jit()` | Python 入口 |

---

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

1. **4.1 JIT 子系统全景**：六大组件地图、惰性单例、以及 `init_jit` 如何把路径钉入编译器。
2. **4.2 运行时 JIT 的动机与 `generate`**：为什么不在安装期编译、以及代码生成层如何把运行时参数变成模板实参。
3. **4.3 `Compiler::build` 端到端主流程**：临时编译 → `fsync` → 原子重命名 → 缓存加载（本讲核心，对应主实践任务）。

---

### 4.1 JIT 子系统全景：组件地图与 init_jit 初始化路径

#### 4.1.1 概念说明

DeepEP 的 JIT 子系统是一套**「编译器 + 缓存 + 运行时 + 启动框架」**四件套。它不是某种黑魔法，本质上就是：在运行时调用 `nvcc`，把一段动态生成的、带具体模板实参的 `.cu` 源码，编译成 cubin，加载进 GPU，再拿到内核函数指针去启动。

为了职责清晰，这套逻辑被拆成六个组件：

| 组件 | 单例变量 | 职责一句话 |
|------|----------|-----------|
| `Compiler` / `NVCCCompiler` | `compiler` | 编译器：拼装 `nvcc` 命令行、执行编译、把产物落到缓存目录。`build()` 是主流程。 |
| `KernelRuntime` | （由缓存持有） | 一个已加载的内核运行时：持有 `cubin` 的 module/library 句柄与内核函数指针。 |
| `KernelRuntimeCache` | `kernel_runtime_cache` | 进程级 `unordered_map` 缓存：避免同一个内核被反复加载。 |
| `LaunchRuntime<Derived>` | （静态方法，非单例） | CRTP 基类：把「代码生成 (`generate`)」与「内核启动 (`launch`)」两段式标准化。 |
| `DeviceRuntime` | `device_runtime` | 缓存当前 GPU 的属性（SM 数、共享内存、架构、时钟频率）。 |
| `IncludeParser` | `include_parser` | 递归解析 `.cu` 里的 `<deep_ep/*>` 头文件并算哈希，作为缓存签名的一部分。 |

这里有个关键设计：这些组件大多以 `LazyInit<T>` 单例的形式存在——第一次被解引用（`operator->`）时才真正构造，之后复用。这避免了 `import deep_ep` 时就立刻探测 GPU、初始化 CUDA。`LazyInit` 的实现非常薄：

[csrc/utils/lazy_init.hpp:10-25](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/utils/lazy_init.hpp#L10-L25) —— 首次 `operator->` 才调用 `factory()` 构造 `ptr`，之后再访问直接返回缓存指针。

#### 4.1.2 核心流程

`import deep_ep` → `init_jit()` 这条初始化链只做一件事：**把三条路径「钉入」编译器相关的静态变量**，让后续真正编译时能找到 `nvcc`、`cuobjdump` 和头文件。它**不编译任何东西**。

```
Python: init_jit()                        # deep_ep/__init__.py:71-80
  └─ _C.init_jit(lib_root, cuda_home, nccl_root)
       └─ C++: deep_ep::jit::init(...)    # csrc/jit/api.hpp:9-14
            ├─ Compiler::prepare_init(lib_root, cuda_home, nccl_root)
            ├─ KernelRuntime::prepare_init(cuda_home)
            └─ IncludeParser::prepare_init(lib_root)
```

注意「钉入」与「构造」是两回事：`prepare_init` 只是把路径字符串赋给类的静态成员（例如 `Compiler::cuda_home = ...`），编译器对象 `compiler` 这个 `LazyInit` 单例此时**还没被构造**——它要等到第一次 `compiler->build(...)` 时才真正实例化（参见 [csrc/jit/compiler.hpp:265-267](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/jit/compiler.hpp#L265-L267)）。这种「先钉路径、后惰性构造」的分工，让 `import` 极其轻量。

#### 4.1.3 源码精读

**Python 入口**——`init_jit` 计算库根目录（即 `deep_ep/` 包所在目录），并把 u2-l1 探测到的 CUDA/NCCL 根目录一并传入：

[deep_ep/__init__.py:71-84](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/__init__.py#L71-L84) —— `library_root_path = os.path.dirname(os.path.abspath(__file__))`，再调 `_C.init_jit(library_root_path, find_cuda_home(), find_nccl_root())`，最后在 `import` 时立即执行。

**C++ 落点**——三个 `prepare_init` 把路径分别钉到三个类的静态成员上：

[csrc/jit/api.hpp:9-18](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/jit/api.hpp#L9-L18) —— `Compiler`、`KernelRuntime`、`IncludeParser` 各拿到自己需要的路径；`register_apis` 把 `init` 暴露为 Python 的 `_C.init_jit`。

**`Compiler::prepare_init`** 把库根、CUDA home、NCCL root 存好，并顺手算出 `cuobjdump` 的路径：

[csrc/jit/compiler.hpp:30-39](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/jit/compiler.hpp#L30-L39) —— `library_include_path = library_root_path / "include"`（这就是 `<deep_ep/...>` 头文件的根），`cuobjdump_path = cuda_home / "bin" / "cuobjdump"`。

这三条路径之所以必须在编译前钉死，是因为运行时 `nvcc` 命令行需要 `-I<include>`、`cuobjdump` 需要 `cuda_home/bin/cuobjdump`，而 C++ 侧无法自行探测——探测发生在 Python（u2-l1），使用发生在 C++。

#### 4.1.4 代码实践

**实践目标**：亲手把 `init_jit` 这条链走一遍，确认三条路径被正确钉入。

**操作步骤**：

1. 在你的 DeepEP 安装环境中，启动一个 Python 进程，`import deep_ep`（这会触发 `init_jit()`）。
2. 阅读 [csrc/jit/api.hpp:9-14](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/jit/api.hpp#L9-L14) 与 [deep_ep/__init__.py:71-80](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/__init__.py#L71-L80)。
3. 思考：如果 `find_cuda_home()` 返回了一个错误的路径，`import deep_ep` 会立刻报错吗？

**需要观察的现象 / 预期结果**：

- `import deep_ep` 不会触发 `nvcc`，也不会初始化 CUDA——因为 `prepare_init` 只是字符串赋值，`compiler` 单例还没构造。
- 如果 `find_cuda_home()` 错了，`import` **不会**立刻失败；要等到第一次实际编译（如构造 `ElasticBuffer` 后调用 `dispatch`）时，`NVCCCompiler` 构造里调 `get_nvcc_version()` 才会因为 `nvcc --version` 失败而断言报错。这正是「惰性构造」的副作用：错误被推迟到真正使用时。

> 说明：本实践为「源码阅读型」，不假设你已运行命令；如果你在无 GPU 环境下只想理解流程，直接对照源码回答第 3 步即可。

#### 4.1.5 小练习与答案

**练习 1**：`init_jit` 同时调了 `KernelRuntime::prepare_init` 和 `Compiler::prepare_init`，两者都接收 `cuda_home`。它们各自用 `cuda_home` 做什么？

> **答案**：`Compiler` 用 `cuda_home` 拼出 `bin/nvcc` 与 `bin/cuobjdump` 的路径（编译与反汇编命令）；`KernelRuntime` 用 `cuda_home` 拼出 `bin/cuobjdump`，在加载 cubin 时用它枚举内核符号（见 [csrc/jit/kernel_runtime.hpp:24-32](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/jit/kernel_runtime.hpp#L24-L32)）。

**练习 2**：为什么这些组件用 `LazyInit` 单例，而不是在 `init_jit` 里直接 `new` 出来？

> **答案**：因为构造它们可能触发较重的副作用（`NVCCCompiler` 构造会调用 `nvcc --version`、读 GPU 属性；`DeviceRuntime` 构造会调 `cudaGetDeviceProperties`）。`LazyInit` 让这些副作用推迟到「真正第一次编译/启动」时，保持 `import deep_ep` 轻量，也避免在 `import` 阶段就初始化 CUDA（那会与进程 fork 冲突，正是 u2-l1 里 `find_cuda_home` 刻意不复用 PyTorch 实现的原因）。

---

### 4.2 运行时 JIT 的动机与 generate 代码生成

#### 4.2.1 概念说明

要理解 DeepEP 为什么「大费周章」在运行时编译，先看一组数字。以 dispatch 内核为例，它的模板参数有：`is_scaleup_nvlink`、`do_cpu_sync`、`num_notify_warps`、`num_dispatch_warps`、`num_scaleup_ranks`、`num_hidden_bytes`、`num_sf_packs`、`num_max_tokens_per_rank`、`num_experts`、`num_topk`、`expert_alignment`、`num_qps`、`num_timeout_cycles`……十多个。每个参数在实际部署中都有多种合法取值（专家数 64/128/256/...、top-k 1~16、hidden 4096~7168~...、scaleup_ranks 8/16/...）。

如果想在安装期把所有组合都编译好，组合数大致是各参数取值数的乘积：

\[
N_{\text{组合}} \;=\; \prod_{i} |V_i|
\]

其中 \(V_i\) 是第 \(i\) 个模板参数的取值集合。这个乘积轻松达到百万、千万量级，安装期全部编译既不现实（编译耗时与磁盘占用爆炸），也没必要（一个具体部署只会用到其中极少数组合）。

而反过来，如果完全不编译期化、把所有参数当普通运行时参数传入，又会丧失编译器优化。

**JIT 是这两者的折中**：运行时按需生成**仅包含本次调用所需的那一组实参**的 `.cu` 源码，让 `nvcc` 实例化这一个特化版本，编译一次、缓存复用。组合爆炸问题被「惰性」化解——用到哪个编哪个。

> 注意：DeepEP 的 V1 是「安装期编译 + auto-tuning 选 config」的路线；V2 改成「全 JIT + 解析式 SM/QP」。这一架构演进在 u9-l2 会专门对比，本讲只需知道结论。

#### 4.2.2 核心流程

代码生成层（`LaunchRuntime<Derived>`，一个 CRTP 基类）定义了「两段式」标准流程，所有具体内核（dispatch、combine、barrier…）都遵循它：

```
① generate(args)  → 生成一段 .cu 源码字符串（含具体模板实参）
② compiler->build(name, code) → 编译 + 缓存 + 加载，返回 KernelRuntime
③ launch(runtime, args) → 用 runtime 里的函数指针启动内核
```

`generate` 是模板方法（design pattern 意义上的）：基类负责「给代码加上 include 哈希头注释」，派生类负责「真正拼出函数名与模板实参」。

#### 4.2.3 源码精读

**`LaunchRuntime::generate`** 是代码生成的统一入口，它在派生类 `generate_impl` 产出的代码前，加一行「include 哈希」注释，并把生成结果打印给调试者：

[csrc/jit/launch_runtime.hpp:32-46](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/jit/launch_runtime.hpp#L32-L46) —— 关键是 `include_hash`：它只在第一次计算并缓存（`static std::string include_hash`），因为「代码里 include 了哪些 `deep_ep` 头」在一次进程内不变。这个哈希会被注入源码，从而进入编译缓存签名（见 4.3）。

**派生类的 `generate_impl`**（以 dispatch 为例）展示了「把运行时参数填进模板实参」的核心技巧。直接模式与 hybrid 模式生成不同的函数名：

[csrc/kernels/elastic/dispatch.hpp:51-89](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/kernels/elastic/dispatch.hpp#L51-L89) —— 当 `num_scaleout_ranks == 1`（单节点，直接模式）时实例化 `dispatch_impl<...>`，否则实例化 `hybrid_dispatch_impl<...>`。最终生成的 `.cu` 长这样（简化）：

```cpp
#include <deep_ep/impls/dispatch.cuh>
using namespace deep_ep::elastic;
static void __instantiate_kernel() {
    auto ptr = reinterpret_cast<void*>(&dispatch_impl<true, false, ..., 8, 7168, ...>);
}
```

这段代码本身**不调用**任何内核，它只是取了实例化后模板函数的地址（`&dispatch_impl<...>`）并强转成 `void*`。这个取地址动作的唯一目的，就是**强迫 `nvcc` 为这一组模板实参生成代码**——否则链接器/编译器会觉得这个模板没被用到而省略它（这就是 u4-l2 要深讲的「模板实例化代码生成技巧」，本讲只点出结论）。

**三连调用现场**就在同一个文件的 `launch_dispatch` 里：

[csrc/kernels/elastic/dispatch.hpp:226-229](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/kernels/elastic/dispatch.hpp#L226-L229) —— `generate(args)` → `compiler->build("dispatch", code)` → `launch(runtime, args, stream)`，一条龙完成「生成 → 编译 → 启动」。

#### 4.2.4 代码实践

**实践目标**：亲眼看到 DeepEP 在运行时生成的 `.cu` 源码，并指出被编译期化的参数。

**操作步骤**：

1. 设置环境变量 `export EP_JIT_DEBUG=1`。
2. 运行一次 dispatch（参考 u1-l4 的 `tests/elastic/test_ep.py`，单机 8 卡）。
3. 在终端输出里找 `Generated kernel code:` 这一行（由 [launch_runtime.hpp:43-44](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/jit/launch_runtime.hpp#L43-L44) 打印）。

**需要观察的现象 / 预期结果**：

- 你会看到一段以 `// Includes' hash value: ...` 开头的 `.cu` 代码。
- 代码里 `dispatch_impl<...>` 尖括号里的数字应当与你本次运行的参数一致：例如 `num_scaleup_ranks=8`、`num_hidden_bytes`（= `hidden × 2`，BF16 下）、`num_experts`、`num_topk`、`expert_alignment` 等。
- 这些值此刻已是「编译期常量」，`nvcc` 会据此展开循环、分配寄存器。

> 待本地验证：具体尖括号里的数字取决于你传给 `test_ep.py` 的 `--num-experts`、`--num-topk` 等参数。

#### 4.2.5 小练习与答案

**练习 1**：`LaunchRuntime::generate` 里 `include_hash` 为什么用 `static` 局部变量、且只在为空时计算一次？

> **答案**：因为「这段生成代码 include 了哪些 `deep_ep/` 头」在整次进程里不会变（`generate_impl` 的 includes 是固定的，参见 [launch_runtime.hpp:36-37](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/jit/launch_runtime.hpp#L36-L37) 的注释 `we require that generate_impl's includes never change`）。重复算递归哈希（`IncludeParser` 会递归读头文件，见 [include_parser.hpp:56-73](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/jit/include_parser.hpp#L56-L73)）开销不小，所以只算一次并缓存。

**练习 2**：生成的 `.cu` 里那个 `reinterpret_cast<void*>(&func<...>)` 如果删掉，会发生什么？

> **答案**：`nvcc`/链接器会发现 `dispatch_impl<...>` 这个模板特化从未被使用，从而不为其生成代码（或生成后被剔除）。于是 cubin 里就没有这个内核符号，`KernelRuntime` 加载时会因为「找不到唯一符号」而报「损坏的缓存目录」（详见 4.3.3）。这个看似无用的取地址，正是强制实例化的关键。

---

### 4.3 Compiler::build 端到端主流程：编译 → fsync → 原子重命名 → 加载

> 这是本讲的核心模块，也是主实践任务（画 build 流程图）的所在。

#### 4.3.1 概念说明

`Compiler::build` 是 JIT 子系统的「心脏」。给定一个内核名字和一段源码，它要保证：

1. **可复用**：同一份输入绝不重复编译（两级缓存：进程内 `unordered_map` + 磁盘内容寻址目录）。
2. **分布式安全**：多 rank（甚至多节点）同时编译同一内核时，不会因为并发写缓存而损坏——典型场景是 8 卡训练，8 个进程第一次 dispatch 都会触发同一个内核的编译。
3. **可观测**：可按需 dump PTX/SASS，方便调优。

它的核心套路是**「先编译到一个临时目录，再把整个目录原子重命名到最终缓存路径」**。目录重命名（`rename`）在本地与 POSIX 分布式文件系统上都是原子的：要么完全成功（出现新目录），要么完全失败（什么都没发生），不会留下「半个目录」让别的进程读到残缺文件。

#### 4.3.2 核心流程

`build` 的端到端流程（标号对应 4.3.3 里的函数）：

```
build(name, code)
 │
 ├─① 计算 kernel_signature = name $$ signature $$ flags $$ code
 │     并据此算出缓存目录 dir_path = <cache>/cache/kernel.<name>.<hash>
 │
 ├─② 进程内缓存命中？  ──yes──▶ 直接返回 KernelRuntime  （快路径，零编译）
 │     (KernelRuntimeCache::get)
 │     no
 ├─③ make_tmp_dir() / get_uuid()  → 建一个唯一临时目录
 │
 ├─④ compile(code, tmp_dir, tmp_cubin)  → NVCCCompiler 调 nvcc 编译出 cubin
 │     （可选：dump PTX/SASS）
 │
 ├─⑤ fsync_dir(tmp_dir)  → 自底向上把临时目录里所有文件 + 目录本身 fsync
 │
 ├─⑥ std::filesystem::rename(tmp_dir, dir_path)
 │     ├─ 成功 → 临时目录正式成为缓存目录
 │     └─ 失败（别的 rank 抢先建成） → safe_remove_all(tmp_dir)，用现成的
 │
 └─⑦ KernelRuntimeCache::get(dir_path) → 从磁盘加载 KernelRuntime 并缓存
        （内部：KernelRuntime 构造 → cuobjdump 找唯一符号 → load_kernel）
```

两级缓存的关系值得强调：第②步是「进程内、单次 Python 进程」的缓存（`KernelRuntimeCache` 的 `unordered_map`），命中则连磁盘都不用读第二次的加载开销；第⑥/⑦步是「跨进程、跨重启」的磁盘缓存（内容寻址目录），命中则不用重新 `nvcc` 编译。

#### 4.3.3 源码精读

**`build` 主流程**——这是你必须逐行读懂的函数：

[csrc/jit/compiler.hpp:111-160](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/jit/compiler.hpp#L111-L160) —— 关键点逐段说明：

- **① 内核签名与缓存目录**（[L112-L113](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/jit/compiler.hpp#L112-L113)）：`kernel_signature` 把「名字 + 编译器签名（如 `NVCC12.3`）+ 编译标志 + 源码」用 `$$` 拼起来；`dir_path` 用它的 `get_hex_digest` 作为目录名后缀。这意味着只要源码、标志、编译器版本任何一处变了，就会落到一个全新的目录，互不污染。
- **② 进程内缓存**（[L116-L117](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/jit/compiler.hpp#L116-L117)）：先问 `kernel_runtime_cache->get(dir_path)`，命中就直接返回——这就是「第二次调用同一内核零开销」的原因。
- **③ 临时目录**（[L122-L123](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/jit/compiler.hpp#L122-L123)）：`make_tmp_dir() / get_uuid()`，临时目录位于 `<cache>/tmp/<pid>-<随机>`，保证多进程不撞名。
- **④ 编译**（[L126-L138](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/jit/compiler.hpp#L126-L138)）：在临时目录里编译出 `kernel.cubin`，按需 dump PTX/SASS。
- **⑤ fsync**（[L141](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/jit/compiler.hpp#L141)）：编译完成后，先 `fsync_dir(tmp_dir)`。
- **⑥ 原子重命名**（[L145-L154](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/jit/compiler.hpp#L145-L154)）：`rename(tmp_dir_path, dir_path)`；若失败（别的 rank 抢先），用 `safe_remove_all` 清理自己的临时目录，转而用现成的。
- **⑦ 加载并缓存**（[L157-L159](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/jit/compiler.hpp#L157-L159)）：再次 `kernel_runtime_cache->get(dir_path)`，这次磁盘上已有完整产物，构造 `KernelRuntime` 并存进进程缓存。

**为什么必须 fsync**——注释和实现都明确点出：在分布式文件系统上，`close()` 不保证数据落盘可见：

[csrc/jit/compiler.hpp:91-109](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/jit/compiler.hpp#L91-L109) —— `fsync_dir` 自底向上递归 `fsync` 文件再 `fsync` 目录本身（`fsync_path` 用 `::open` + `::fsync`）。注释说这是为了「保证数据与目录项在其他节点可见」——因为下一步要 `rename`，若临时目录里的 cubin 在别的进程/节点看来还没落盘，重命名后它们会读到空文件。

**为什么用目录 rename 而不是文件 rename**——注释解释了「stale inode」问题：

[csrc/jit/compiler.hpp:119-121](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/jit/compiler.hpp#L119-L121) 与 [L143-L154](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/jit/compiler.hpp#L143-L154) —— 目录 rename 是原子的；而逐个文件 rename 会产生「目录里部分文件已就位、部分还没」的中间态。注释还特别提醒：竞态清理时**不要**用 `std::filesystem::remove_all`，它在分布式文件系统上并发操作同一父目录时可能段错误，所以改用自写的 `safe_remove_all`。

**`NVCCCompiler::compile`** 是真正拼装并执行 `nvcc` 命令的地方：

[csrc/jit/compiler.hpp:222-262](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/jit/compiler.hpp#L222-L262) —— 先用 `put` 把源码写进 `kernel.cu`（写完立刻 `fsync`，见 [put L101-L109](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/jit/compiler.hpp#L101-L109)），再执行 `cd <空临时目录> && nvcc kernel.cu -cubin -o kernel.cubin <flags>`。注意它**特意 `cd` 到另一个空目录**再编译，注释（[L230](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/jit/compiler.hpp#L230)）说是「避免当前目录文件遮蔽 C++ 标准库头」。

`NVCCCompiler` 在构造时就决定了编译标志与架构（`sm_90a` 等）：

[csrc/jit/compiler.hpp:204-220](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/jit/compiler.hpp#L204-L220) —— 它会先 `get_nvcc_version()` 校验版本 ≥ 12.3（[L186-L201](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/jit/compiler.hpp#L186-L201)），再根据 `device_runtime->get_arch(...)` 决定 `--gpu-architecture`。架构字符串由 `DeviceRuntime` 探测 GPU 得到：

[csrc/jit/device_runtime.hpp:49-58](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/jit/device_runtime.hpp#L49-L58) —— 例如 Hopper（major=9,minor=0）返回 `"90a"`。

**`KernelRuntimeCache::get`** 是两级缓存的「入口」，负责把「磁盘上的目录」变成「进程内的 `KernelRuntime`」：

[csrc/jit/cache.hpp:21-29](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/jit/cache.hpp#L21-L29) —— 先查 `unordered_map`（进程内命中），否则调 `KernelRuntime::check_validity` 校验磁盘目录完整（`kernel.cu` 与 `kernel.cubin` 都在），再构造 `KernelRuntime` 存入缓存。`check_validity` 的设计前提正是「目录是原子 rename 进来的，所以要么文件都在、要么目录压根不存在」：

[csrc/jit/kernel_runtime.hpp:65-78](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/jit/kernel_runtime.hpp#L65-L78) —— 若 `kernel.cu` 与 `kernel.cubin` 缺一，就判定缓存损坏并提示用户 `rm -rf`。

**`KernelRuntime` 构造**——从 cubin 里找出**唯一**内核符号并加载。这一步非常巧妙：它用 `cuobjdump -symbols` 枚举 cubin 里所有 `STT_FUNC` + `STO_ENTRY` 符号，过滤掉 `vprintf`/`__instantiate_kernel` 等噪声，要求**恰好剩 1 个**：

[csrc/jit/kernel_runtime.hpp:20-59](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/jit/kernel_runtime.hpp#L20-L59) —— 注意 [L31](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/jit/kernel_runtime.hpp#L31) 的 `illegal_names` 里包含 `__instantiate_kernel`——这正是 4.2 里 `generate_impl` 那个 `static void __instantiate_kernel()` 的函数名！它只是用来强制实例化的「脚手架」，本身不是内核，必须被过滤掉。最后 [L58](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/jit/kernel_runtime.hpp#L58) 调 `load_kernel`（用 CUDA 驱动 API `cuModuleLoad` + `cuModuleGetFunction`，见 [csrc/jit/handle.hpp:82-92](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/jit/handle.hpp#L82-L92)）。若符号数 ≠ 1，会打印「Corrupted JIT cache directory」并要求 `rm -rf`（[L46-L55](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/jit/kernel_runtime.hpp#L46-L55)）。

> 本讲只点到「符号枚举 + 唯一性校验」；损坏检测与 cubin 加载的更细节（含运行时 API 与驱动 API 的分支）留待 u4-l3。

#### 4.3.4 代码实践（主实践任务）

**实践目标**：把 `Compiler::build` 的端到端流程画成图，并在磁盘上亲眼看到「冷启动编译」与「热启动命中」两种状态。

**操作步骤**：

1. **画流程图**。按本讲 4.3.2 的流程，画出 `build` 的流程图，并在**每个步骤上标注对应的函数名与源码行号**。参考答案（请先自己画，再对照）：

   ```
   Compiler::build(name, code)                      [compiler.hpp:111]
     │
     ├─ 拼 kernel_signature、算 dir_path             [L112-113, get_hex_digest]
     ├─ kernel_runtime_cache->get(dir_path)          [cache.hpp:21]  ──命中──▶ 返回
     │                                                        │ 未命中
     ├─ make_tmp_dir()/get_uuid() 建临时目录          [compiler.hpp:77,122 / system.hpp:67]
     ├─ NVCCCompiler::compile  写 kernel.cu、跑 nvcc  [compiler.hpp:222-262]
     ├─ fsync_dir(tmp_dir)                           [compiler.hpp:91,141]
     ├─ rename(tmp_dir, dir_path)                    [compiler.hpp:147]
     │       失败 → safe_remove_all(tmp_dir)         [compiler.hpp:153 / system.hpp:83]
     └─ kernel_runtime_cache->get(dir_path)          [cache.hpp:21]
             └─ KernelRuntime(dir_path)              [kernel_runtime.hpp:20]
                    ├─ cuobjdump -symbols、找唯一符号 [kernel_runtime.hpp:31-55]
                    └─ load_kernel (cuModuleLoad)    [kernel_runtime.hpp:58 / handle.hpp:82]
   ```

2. **冷启动观察**。设置一个干净的缓存目录后跑一次测试：
   ```bash
   export EP_JIT_CACHE_DIR=/tmp/deep_ep_jit_demo
   rm -rf $EP_JIT_CACHE_DIR
   export EP_JIT_DEBUG=1
   # 跑一次单机 dispatch（参考 u1-l4 的 tests/elastic/test_ep.py）
   ```
   跑完后查看缓存结构：
   ```bash
   ls $EP_JIT_CACHE_DIR/cache
   ls $EP_JIT_CACHE_DIR/cache/kernel.dispatch.<某个hash>
   ```

3. **热启动观察**。在**同一个 Python 进程内**连续 dispatch 两次（或直接看 `test_ep.py` 的多轮循环），观察 `EP_JIT_DEBUG` 的输出。

**需要观察的现象 / 预期结果**：

- 冷启动：`cache/` 下出现 `kernel.dispatch.<16位hex>`、`kernel.dispatch_copy_epilogue.<hex>` 等目录；每个目录里有 `kernel.cu`（生成的源码）与 `kernel.cubin`（编译产物）。日志里能看到 `Running NVCC command: ...`。
- 热启动（同进程第二次）：日志里**不再有** `Running NVCC command`，因为第②步进程内缓存命中，直接返回——这正是「编译只在第一次发生」。
- 跨进程热启动（新进程、同 `EP_JIT_CACHE_DIR`）：仍然**不**编译（磁盘内容寻址缓存命中），但会重新走 `KernelRuntime` 构造（因为进程内缓存是空的）。

> 待本地验证：`kernel.dispatch.<hash>` 中的 `<hash>` 取决于你的参数；目录里的 `kernel.cu` 可以直接打开，对照 4.2.4 你能看到被编译期化的模板实参。

#### 4.3.5 小练习与答案

**练习 1**：`build` 在 `rename` 之前为什么要 `fsync_dir`，而 `rename` 失败时又为什么必须用 `safe_remove_all` 而非 `std::filesystem::remove_all`？

> **答案**：`fsync_dir` 保证临时目录里的 cubin/cu 在分布式文件系统上真正落盘、对其他进程/节点可见，否则 `rename` 后别的 rank 可能读到残缺文件。改用 `safe_remove_all` 是因为 [compiler.hpp:150-153](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/jit/compiler.hpp#L150-L153) 注释指出：标准库的 `remove_all` 在分布式文件系统上、多个进程并发操作同一父目录时可能段错误；`safe_remove_all`（[system.hpp:83-109](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/utils/system.hpp#L83-L109)）逐项递归、用 `error_code` 重载吞掉竞态错误，更稳健。

**练习 2**：假如你修改了 `dispatch.cuh` 里内核的一行实现，但没清缓存，会发生什么？为什么？

> **答案**：会编译出一个**全新**的内核，落到一个新目录里，旧的仍在磁盘上（变成死缓存）。原因是 `IncludeParser` 会把 `<deep_ep/impls/dispatch.cuh>` 及其递归 include 的头文件内容算进 `include_hash`（[include_parser.hpp:47-73](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/jit/include_parser.hpp#L47-L73)），头文件一改，`generate` 产出的代码头注释里的哈希就变，进而 `kernel_signature`（[compiler.hpp:112](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/jit/compiler.hpp#L112)）和 `dir_path` 都变，于是命中不了旧目录。所以改了头文件后磁盘会逐渐积累旧哈希目录，可定期 `rm -rf` 清理。

**练习 3**：`KernelRuntime` 构造里为什么要求 cubin 中「恰好 1 个」内核符号？

> **答案**：因为每个缓存目录对应**一个**特化的内核函数。`generate_impl` 用 `__instantiate_kernel` 只取了一个模板特化的地址（4.2），所以 cubin 里应当只有一个真正的内核入口（其余 `vprintf`、`__instantiate_kernel` 等被 `illegal_names` 过滤）。若符号数 ≠ 1，说明 cubin 异常（比如生成的 `.cu` 实例化了多个内核、或 cubin 损坏），此时直接报「Corrupted JIT cache directory」并要求用户 `rm -rf`（[kernel_runtime.hpp:46-55](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/jit/kernel_runtime.hpp#L46-L55)），避免加载到错误的内核指针。

---

## 5. 综合实践

把本讲的三个模块串起来，完成一个「JIT 全链路追踪」小任务：

1. **准备**：`export EP_JIT_CACHE_DIR=/tmp/deep_ep_jit_demo && rm -rf $EP_JIT_CACHE_DIR && export EP_JIT_DEBUG=1`。
2. **触发一次 dispatch**（参考 u1-l4 跑 `tests/elastic/test_ep.py` 单机 8 卡）。
3. **收集证据**，回答以下问题，每条都引用本讲给出的源码行号作为依据：
   - 从 `import deep_ep` 的 `init_jit`，到 `launch_dispatch` 里 `generate→build→launch` 三连，分别发生在哪些文件？
   - 在 `EP_JIT_DEBUG` 输出中，找到一条 `Generated kernel code:` 与一条 `Running NVCC command:`，说明它们分别由 [launch_runtime.hpp:43-44](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/jit/launch_runtime.hpp#L43-L44) 与 [compiler.hpp:234-236](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/jit/compiler.hpp#L234-L236) 打印。
   - 打开 `$EP_JIT_CACHE_DIR/cache/kernel.dispatch.<hash>/kernel.cu`，确认它的第一行是 `// Includes' hash value: ...`，且正文是 4.2.3 描述的「取模板地址」结构。
   - 在同一进程内重复 dispatch，确认第二次**没有** `Running NVCC command`，并解释它命中了 [cache.hpp:23](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/jit/cache.hpp#L23) 的哪一级缓存。
4. **画一张完整的时序图**：横轴是时间，纵轴分四条泳道（Python / C++ host / nvcc 子进程 / 磁盘缓存），把 `init_jit` → `generate` → `build`（命中与否）→ `launch` 标注上去。

> 待本地验证：本实践依赖真实 8 卡 Hopper 环境与可运行的 NCCL；若仅做源码理解，可只完成第 4 步的时序图，依据本讲给出的源码行号即可。

---

## 6. 本讲小结

- DeepEP 的 JIT 子系统由六大组件构成：`Compiler`/`NVCCCompiler`（编译）、`KernelRuntime`（已加载内核）、`KernelRuntimeCache`（进程内缓存）、`LaunchRuntime<Derived>`（生成+启动的 CRTP 基类）、`DeviceRuntime`（GPU 属性）、`IncludeParser`（头文件哈希），多以 `LazyInit` 惰性单例存在。
- `init_jit` **不编译任何东西**，它只把 Python 探测到的库根/CUDA home/NCCL root 三条路径「钉入」`Compiler`/`KernelRuntime`/`IncludeParser` 的静态成员，真正的编译推迟到首次 `build`。
- 选择运行时 JIT 是为了化解「想编译期化十多个参数以换取最优代码，但参数要到运行时才知道」的矛盾：用到哪个组合就现编哪个，靠缓存复用，避免安装期组合爆炸。
- 代码生成层用「取模板特化函数地址 + 强转 `void*`」的技巧强迫 `nvcc` 实例化特定内核；`__instantiate_kernel` 是脚手架，会被 `KernelRuntime` 的符号过滤排除。
- `Compiler::build` 的主流程是：内容寻址定位缓存目录 → 进程内缓存命中则直接返回 → 否则编译到唯一临时目录 → `fsync_dir` → **整个目录原子 rename** 到最终路径 → 从 cubin 提取唯一符号并加载。目录 rename + fsync + `safe_remove_all` 共同保证了多 rank 并发编译下的分布式文件系统安全。
- 两级缓存各有分工：`KernelRuntimeCache` 的 `unordered_map` 是「进程内、免重新加载」；磁盘上的 `cache/kernel.<name>.<hash>/` 是「跨进程、免重新编译」。

---

## 7. 下一步学习建议

本讲只画了 JIT 的骨架与 `build` 主流程，几个关键细节还没展开。建议按顺序继续：

- **u4-l2 内核代码生成**：深入 `generate_impl` 的模板实例化技巧——直接模式与 hybrid 模式如何生成不同 `func_name`、哪些参数被编译期化、为什么这种写法能让 `nvcc` 极致优化。
- **u4-l3 编译缓存与 cubin 加载**：展开 `KernelRuntime` 的符号枚举与损坏检测、内容哈希签名如何覆盖头文件、`EP_JIT_DUMP_ASM/PTX/SASS` 等调试手段。
- **u4-l4 启动框架**：`LaunchArgs`（grid/block/smem/cluster/cooperative/pdl）如何影响内核启动、`DeviceRuntime` 如何缓存 GPU 属性用于超时周期换算。

如果你对「JIT 编译出的内核到底干了什么」更感兴趣，也可以先跳到 **U5 Dispatch 内核链路**（u5-l1 直接模式 dispatch），那里会用到本讲建立的「generate→build→launch」心智模型，去看 `dispatch_impl` 这个被实例化的模板内核内部。
