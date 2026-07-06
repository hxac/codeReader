# 环境变量体系：运行时、网络、JIT、构建四类

## 1. 本讲目标

DeepEP 把大量「调参旋钮」暴露成环境变量，覆盖了运行时行为、网络配置、JIT 编译、构建开关四大类。本讲要让你做到：

1. 能背出四大类环境变量的代表变量、取值类型与默认值，遇到问题知道该拧哪一个。
2. 彻底搞懂 DeepEP 独有的「**持久化变量**」机制——为什么少数几个变量（如 `EP_JIT_CACHE_DIR`、`EP_NCCL_ROOT_DIR`）在 `pip install` 时就被烘焙进安装包，import 时再以默认值形式注入，运行期又能被 `export` 覆盖。
3. 理解 RDMA 的服务级（Service Level, SL）与 InfiniBand 虚拟通道（Virtual Lane, VL）如何用来做流量隔离。
4. 能上手用 JIT 调试类变量（`EP_JIT_DEBUG`、`EP_JIT_DUMP_SASS`、`EP_JIT_PTXAS_CHECK` 等）观察一次内核编译，产出 PTX/SASS 文件并诊断问题。

本讲承接 [u1-l3（环境依赖、安装与构建流程）](u1-l3-build-and-install.md)。u1-l3 已经点过一句「持久化变量构建期烘焙」，本讲是把这一句话**展开成完整的代码链路**，并把 README 里那张环境变量清单逐个落到真实源码上。

## 2. 前置知识

阅读本讲前，你需要具备：

- **环境变量的基本概念**：操作系统进程级的环境变量（Linux 下 `export FOO=bar`、`os.environ['FOO']`、C 的 `std::getenv`），子进程会继承父进程的环境变量快照。
- **`pip install` 的构建期 vs 运行期**：`setup.py` 在安装机器上跑一次，产出 `.so` 和 Python 包；之后用户 `import deep_ep` 是运行期。这两者可能不在同一台机器、不在同一时刻。
- **JIT 编译**：DeepEP 不在安装期编译 CUDA 内核，而是在首次调用某个内核时，由 `csrc/jit/` 子系统现场生成 `.cu`、调 `nvcc` 编译、加载 `.cubin`。详见 [u4-l1（JIT 系统总览）](u4-l1-jit-overview.md)。
- **NCCL Gin 后端**：V2 用 header-only 的 NCCL Gin 提供对称内存与 RDMA 能力，详见 [u3-l4](u3-l4-nccl-gin-symmetric.md) 与 [u8-l2](u8-l2-nccl-comm-reuse.md)。
- **RDMA/InfiniBand 基础术语**：HCA（网卡）、QP（队列对）、RoCE 与 IB 的区别。本讲会在用到时补一句解释。

下面用一张表先把术语对齐：

| 术语 | 全称 / 含义 |
|---|---|
| env var | environment variable，进程级配置变量 |
| 持久化变量 | DeepEP 术语，指在构建期被烘焙进安装包、运行期作默认值的那几个变量 |
| JIT | Just-In-Time，运行时按需编译 CUDA 内核 |
| NVCC | NVIDIA CUDA Compiler，把 `.cu` 编成 `.cubin` |
| PTX | NVIDIA 的中间指令集（接近汇编的虚拟 ISA） |
| SASS | NVIDIA GPU 的真实机器指令（PTX 之下、硬件直接执行） |
| PTXAS | PTX 汇编器，把 PTX 编成 SASS，`--register-usage-level` 是它的旋钮 |
| SL | Service Level，RDMA 报文里的服务级字段，决定走哪条 VL |
| VL | Virtual Lane，InfiniBand 物理链路上的虚拟通道，可做流量隔离 |

## 3. 本讲源码地图

本讲横跨 Python 与 C++ 两侧，涉及的关键文件如下：

| 文件 | 在本讲的作用 |
|---|---|
| [README.md](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/README.md) | 环境变量的权威清单（四大类）与流量隔离说明 |
| [setup.py](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/setup.py) | 构建期定义 `persistent_env_names`、动态生成 `envs.py`、读取构建类变量 |
| [deep_ep/__init__.py](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/__init__.py) | import 时加载烘焙好的 `persistent_envs` 并注入 `os.environ`，以及 `EP_SUPPRESS_NCCL_CHECK` |
| [deep_ep/utils/envs.py](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/utils/envs.py) | `EP_NIC_NAME` 默认值、带宽/RDMA 探测 |
| [deep_ep/utils/comm.py](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/utils/comm.py) | `EP_REUSE_NCCL_COMM` 控制是否复用 PyTorch 的 NCCL communicator |
| [deep_ep/utils/testing.py](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/utils/testing.py) | `EP_USE_NVIDIA_TOOLS`、`EP_DISABLE_BARRIER_PROFILING` 控制基准测试行为 |
| [deep_ep/buffers/elastic.py](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/buffers/elastic.py) | `EP_BUFFER_DEBUG`、`EP_OVERRIDE_RDMA_SL` 的运行期读取 |
| [csrc/utils/system.hpp](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/utils/system.hpp) | C++ 侧统一的 `get_env<T>` 模板 |
| [csrc/jit/compiler.hpp](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/jit/compiler.hpp) | JIT 编译期读取 `EP_JIT_*` 全家桶，决定编译标志、缓存目录、dump 行为 |
| [csrc/kernels/backend/nccl.cu](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/kernels/backend/nccl.cu) | `EP_BUFFER_DEBUG`、`EP_DISABLE_GIN` 的 C++ 读取 |
| [csrc/elastic/buffer.hpp](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/elastic/buffer.hpp) | `EP_AVOID_RECORD_STREAM`、`EP_BUFFER_DEBUG` 的 C++ 读取 |

一句话：**Python 侧用 `os.environ.get(...)`、C++ 侧用 `get_env<T>(...)`，两边各自读同一批环境变量**；只有「持久化变量」会多走一段构建期烘焙的弯路。

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：

- **4.1 环境变量总览与统一读取层**：四大类清单 + Python/C++ 两套读取入口。
- **4.2 持久化配置：构建期烘焙与运行期覆盖的两级默认值机制**（本讲最深、最核心）。
- **4.3 JIT 调试类环境变量精读**（含本讲主实践任务）。
- **4.4 RDMA 服务级（SL）与虚拟通道（VL）：流量隔离原理**。

### 4.1 环境变量总览与统一读取层

#### 4.1.1 概念说明

DeepEP 的环境变量分成四大类，README 的「Environment variables」小节给出权威清单：

- **General（通用）**：影响整体行为的开关，如 `EP_BUFFER_DEBUG`（调试打印）、`EP_SUPPRESS_NCCL_CHECK`（跳过 NCCL 版本校验）、`EP_AVOID_RECORD_STREAM`、`EP_NUM_TOPK_IDX_BITS`。
- **Networking（网络）**：与 RDMA/NIC 相关，如 `EP_NIC_NAME`、`EP_OVERRIDE_RDMA_SL`、`EP_DISABLE_GIN`。
- **JIT（运行时编译）**：控制 `nvcc` 编译行为与产物，如 `EP_JIT_DEBUG`、`EP_JIT_CACHE_DIR`、`EP_JIT_DUMP_SASS`、`EP_JIT_PTXAS_CHECK` 等。
- **Debug and profiling / Build（调试剖析与构建）**：`EP_USE_NVIDIA_TOOLS`、`EP_DISABLE_BARRIER_PROFILING`，以及构建期的 `EP_NCCL_ROOT_DIR`、`TORCH_CUDA_ARCH_LIST`、`DISABLE_SM90_FEATURES` 等。

这些变量之所以重要，是因为 DeepEP 把它们当成了**不重新编译就能改行为的唯一通道**：运行期变量改完 `export` 即生效（下次 `import` 或下次建 buffer 时读取），构建期变量则在 `pip install` 时被读一次、烘焙或编译进二进制。

#### 4.1.2 核心流程

一个环境变量从「用户设置」到「影响行为」的流程：

1. 用户在 shell 里 `export EP_BUFFER_DEBUG=1`（或在 Python 里 `os.environ['EP_BUFFER_DEBUG']='1'`，但必须在 `import deep_ep` **之前**设置才能影响 C++ 静态初始化）。
2. 进程启动，`import deep_ep` 触发 `__init__.py`，先把持久化默认值注入 `os.environ`（见 4.2）。
3. 用户调用 API，Python 侧用 `os.environ.get('EP_BUFFER_DEBUG', 0)` 读取；C++ 侧用 `get_env<int>("EP_BUFFER_DEBUG")` 读取。
4. 两侧根据读到的值决定是否打印调试信息、改变缓冲区分配、改变编译标志等。

#### 4.1.3 源码精读

**Python 侧**典型的读取方式是用 `os.environ.get(name, default)`。例如 `ElasticBuffer` 构造时读 `EP_BUFFER_DEBUG` 来打印缓冲区初始化信息：

[deep_ep/buffers/elastic.py:311-313](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/buffers/elastic.py#L311-L313) —— 读 `EP_BUFFER_DEBUG`，非 0 则打印本 rank 分配了多少字节、其中 CPU 段多少。

注意一个常见陷阱：`os.environ.get('EP_BUFFER_DEBUG', 0)` 的默认值是**整数 `0`**，而环境变量读出来永远是**字符串**。所以这里的判断 `'... 字符串 ...'` 在 `if` 里依赖 Python 的真值语义——字符串 `'0'` 其实是**真**！这是 DeepEP 代码里一个值得留意的小细节：要正确判断必须用 `int(...)` 转换（C++ 侧 `get_env<int>` 会帮你转，Python 侧则要小心）。

**C++ 侧**所有环境变量读取都走一个统一的模板函数 `get_env`：

[csrc/utils/system.hpp:19-35](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/utils/system.hpp#L19-L35) —— `get_env<T>` 模板：底层调 `std::getenv`，`T=std::string` 时原样返回，`T=int` 时用 `sscanf` 转 int，未设置时返回默认值。

有了这个模板，C++ 各处读环境变量就一行：`get_env<int>("EP_DISABLE_GIN", 0)`、`get_env("EP_JIT_DEBUG", 0)`。例如 NCCL Gin 后端的开关就读自这里：

[csrc/kernels/backend/nccl.cu:84-91](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/kernels/backend/nccl.cu#L84-L91) —— `num_ranks > 1 and get_env("EP_DISABLE_GIN", 0) == 0` 才走 Gin 路径并查询 `ginType`/`railedGinType`；`EP_DISABLE_GIN=1` 会回退到非 Gin 路径，常用于排查 Gin 相关的网络配置问题。

下面这张表把四大类的代表变量与「读取位置」对应起来，方便你按图索骥：

| 类别 | 变量 | 默认值 | 读取位置（典型） |
|---|---|---|---|
| General | `EP_BUFFER_DEBUG` | `0` | Python `elastic.py:311`、C++ `buffer.hpp:865`、`nccl.cu:38` |
| General | `EP_SUPPRESS_NCCL_CHECK` | `0` | `__init__.py:51` |
| General | `EP_AVOID_RECORD_STREAM` | `0` | C++ `buffer.hpp:566` |
| General | `EP_NUM_TOPK_IDX_BITS` | `0`(auto) | 构建期 `setup.py:163`、JIT `compiler.hpp:71`（持久化） |
| Networking | `EP_NIC_NAME` | `mlx5_0` | `utils/envs.py:21` |
| Networking | `EP_OVERRIDE_RDMA_SL` | 未设置 | `elastic.py:323` |
| Networking | `EP_DISABLE_GIN` | `0` | `nccl.cu:84` |
| JIT | `EP_JIT_DEBUG` | `0` | `compiler.hpp:61` 等多处 |
| JIT | `EP_JIT_CACHE_DIR` | `$HOME/.deep_ep` | `compiler.hpp:53`（持久化） |
| JIT | `EP_JIT_DUMP_SASS` / `EP_JIT_DUMP_PTX` / `EP_JIT_DUMP_ASM` | `0` | `compiler.hpp:127,135` |
| JIT | `EP_JIT_PTXAS_CHECK` | `0` | `compiler.hpp:256` |
| Profiling | `EP_USE_NVIDIA_TOOLS` | `0` | `testing.py:143` |
| Profiling | `EP_DISABLE_BARRIER_PROFILING` | `0` | `testing.py:152` |
| Build | `EP_NCCL_ROOT_DIR` | 自动探测 | `setup.py`（持久化） |
| Build | `TORCH_CUDA_ARCH_LIST` | `9.0` | `setup.py:142` |

> 提示：完整的取值类型与默认值请以 [README.md:331-369](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/README.md#L331-L369) 为准，本表只挑了代表性变量。

#### 4.1.4 代码实践

**实践目标**：验证「Python 与 C++ 读的是同一个环境变量」，并体会字符串 vs 整数的真值陷阱。

**操作步骤**：

1. 在两个不同的 shell（或两次运行）里，分别：
   - A：`export EP_BUFFER_DEBUG=1` 后运行 `python tests/elastic/test_ep.py`（在单机 8 卡上）。
   - B：`export EP_BUFFER_DEBUG=0` 后运行同样的命令。
2. 观察输出：A 会多出形如 `Initializing EP elastic buffer with ... bytes` 与 `EP NCCL device communicator has ... allocated QPs` 的行；B 没有。

**需要观察的现象**：

- A 运行里既有 Python 打印（`Initializing EP elastic buffer ...`，来自 `elastic.py:311`），也有 C++ 打印（`New NCCL host communicator created`、`EP NCCL device communicator has ... QPs`，来自 `nccl.cu:38/79`）——证明两侧都在读同一个 `EP_BUFFER_DEBUG`。

**预期结果**：A 输出多一串调试行，B 安静。若 A 看不到 C++ 侧的行，可能是该 buffer 未触发 NCCL communicator 创建路径（单机直连），可改用多节点或换 `EP_DISABLE_GIN` 观察。

**待本地验证**：本机是否有 8 张 Hopper 卡与可用的 NCCL；若无，则只能阅读源码确认打印分支。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `if os.environ.get('EP_BUFFER_DEBUG', 0):` 在 `EP_BUFFER_DEBUG=0` 时**仍可能**为真？该怎么写才正确？

**答案**：因为环境变量读出来是字符串 `'0'`，而 Python 里非空字符串 `'0'` 是真值。正确写法应显式转 int：`if int(os.environ.get('EP_BUFFER_DEBUG', 0)):`。（DeepEP 部分位置用了 `os.environ.get(name, 0)` 直接进 `if`，依赖后续逻辑恰好兼容，但这是容易踩坑的写法。）

**练习 2**：C++ 侧 `get_env<int>("EP_DISABLE_GIN", 0)` 的第二个参数 `0` 是什么作用？

**答案**：它是「环境变量未设置时的默认值」。`get_env` 在 `std::getenv` 返回 `nullptr` 时直接返回该默认值，这样调用方不必处理「未设置」分支。

---

### 4.2 持久化配置：构建期烘焙与运行期覆盖的两级默认值机制

#### 4.2.1 概念说明

这是 DeepEP 环境变量体系里**最独特**的一环，也是本讲的核心。

绝大多数环境变量是「现设现用」：用户在运行机器上 `export`，进程读到就用。但 DeepEP 有一小撮变量叫**持久化变量（persistent env）**，它们的需求是：

> 集群管理员希望在**构建/安装机器**上把某些配置（比如缓存目录 `EP_JIT_CACHE_DIR`、NCCL 安装路径 `EP_NCCL_ROOT_DIR`）定死，让所有用户 `import` 时自动生效；但同时又允许个别用户在运行期用 `export` 覆盖。

这就形成了「**两级默认值**」：

1. **第一级（构建期烘焙）**：`pip install` 时读取安装机的环境，把值写进一个动态生成的 Python 文件 `deep_ep/envs.py`，随包发布。
2. **第二级（运行期覆盖）**：用户 `import deep_ep` 时，把烘焙值作为默认值塞进 `os.environ`，但**仅当用户没有自己设置时才塞**——用户运行期的 `export` 永远优先。

README 明确列出了哪些变量是持久化的：

> The persistent variables are: `EP_JIT_CACHE_DIR`, `EP_JIT_PRINT_COMPILER_COMMAND`, `EP_NUM_TOPK_IDX_BITS`, `EP_NCCL_ROOT_DIR`.

对应 [README.md:367](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/README.md#L367)。

#### 4.2.2 核心流程

```
构建期（pip install 机器）                  运行期（用户机器）
─────────────────────                       ─────────────────────
1. setup.py 顶部定义                        1. import deep_ep
   persistent_env_names = (                  2. __init__.py 尝试 from .envs import persistent_envs
     'EP_JIT_CACHE_DIR',                     3. 遍历 persistent_envs：
     'EP_JIT_PRINT_COMPILER_COMMAND',            if key not in os.environ:
     'EP_NUM_TOPK_IDX_BITS',                         os.environ[key] = value   ← 仅当用户未设
     'EP_NCCL_ROOT_DIR')                       （用户已 export 的不被覆盖）
2. CustomBuildPy.generate_default_envs()     4. 后续 Python/C++ 读 os.environ / getenv
   把 os.environ[name] 写成字符串             拿到「用户值 or 烘焙值 or 代码默认值」
   'persistent_envs[name] = "value"'
   生成 build_lib/deep_ep/envs.py
3. envs.py 随包一起安装
```

关键不变式（必须记住的三条）：

- **烘焙值只是默认值，不是强制值**：用户运行期 `export EP_JIT_CACHE_DIR=/tmp/foo` 一定会覆盖烘焙值。
- **烘焙发生在构建机器的环境里**：如果构建机上 `EP_JIT_CACHE_DIR` 没设置，生成的 `envs.py` 里就**根本没有这一行**（见下面源码的 `if name in os.environ else ''`），运行期就退回到代码里的硬默认（如 `$HOME/.deep_ep`）。
- **C++ 侧感知不到烘焙**：烘焙只把值塞进 `os.environ`，而 `os.environ` 又是进程环境变量，C++ 的 `std::getenv` 能直接读到——所以 C++ 不需要任何特殊代码，靠进程环境变量这个「公共黑板」天然共享。

#### 4.2.3 源码精读

**第一步：构建期定义名单 + 动态生成 envs.py。**

[setup.py:13](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/setup.py#L13) —— 顶层定义 `persistent_env_names` 四元组，这是「哪些变量会被烘焙」的唯一真相来源。

[setup.py:78-89](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/setup.py#L78-L89) —— `CustomBuildPy.generate_default_envs()`：遍历 `persistent_env_names`，**只有那些在构建机 `os.environ` 里实际存在的**变量，才被写成 `persistent_envs['NAME'] = 'value'` 这一行，落盘到 `build_lib/deep_ep/envs.py`。

注意第 83 行的写法：

```python
code += f"persistent_envs['{name}'] = '{os.environ[name]}'\n" if name in os.environ else ''
```

这行直接把构建机的值用单引号包成字面量写进 `.py` 文件——这就是「烘焙」。如果构建机没设该变量，就生成空串（什么都不写），运行期自然没有这一项。

**第二步：import 时把烘焙值注入 `os.environ`（仅当用户未设）。**

[deep_ep/__init__.py:10-18](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/__init__.py#L10-L18) —— 这就是「两级默认值」的核心实现：

```python
try:
    from .envs import persistent_envs          # 读构建期烘焙的字典
    for key, value in persistent_envs.items():
        if key not in os.environ:              # ← 关键：用户已设则不覆盖
            os.environ[key] = value
except ImportError:
    pass                                        # 开发模式下可能没有 envs.py，静默跳过
```

`if key not in os.environ` 这一行就是「运行期可覆盖」的全部秘密：用户的 `export` 已经把 key 放进了 `os.environ`，于是烘焙值被跳过；反之没人设时，烘焙值顶上。

**第三步：C++ 侧无差别读取。**

以 `EP_JIT_CACHE_DIR` 为例，它被烘焙 → import 时进 `os.environ` → 进程环境变量 → C++ 的 `std::getenv` 读到：

[csrc/jit/compiler.hpp:52-54](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/jit/compiler.hpp#L52-L54) —— 缓存目录默认 `$HOME/.deep_ep`，若 `EP_JIT_CACHE_DIR` 非空则覆盖。这条 `get_env` 读到的值，可能来自用户 `export`，也可能来自烘焙注入——C++ 无法也无需区分。

类似地，构建期的 `EP_NCCL_ROOT_DIR` 在 `setup.py` 阶段由 `find_pkgs.find_nccl_root()` 探测后被烘焙（用于运行期 JIT 找 NCCL 头文件/库），路径最终注入 [csrc/jit/compiler.hpp:27](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/jit/compiler.hpp#L27) 的 `Compiler::nccl_root` 静态成员（由 `init_jit` 钉入，详见 [u2-l1](u2-l1-import-init.md)）。

#### 4.2.4 代码实践

**实践目标**：亲眼看到「烘焙的 `envs.py`」并验证两级默认值的覆盖优先级。

**操作步骤**：

1. 在构建机上设置一个持久化变量再装包：
   ```bash
   export EP_JIT_CACHE_DIR=/opt/deep_ep_cache
   pip install .
   ```
2. 装完后查看烘焙产物：
   ```bash
   python -c "import deep_ep, os; print(os.path.dirname(deep_ep.__file__)+'/envs.py')" | xargs cat
   ```
   预期看到一行 `persistent_envs['EP_JIT_CACHE_DIR'] = '/opt/deep_ep_cache'`。
3. 运行期不设该变量，`import deep_ep` 后检查：
   ```python
   import os
   print(os.environ.get('EP_JIT_CACHE_DIR'))   # 预期 /opt/deep_ep_cache（烘焙值顶上）
   ```
4. 运行期主动覆盖：
   ```bash
   export EP_JIT_CACHE_DIR=/tmp/override
   python -c "import os; import deep_ep; print(os.environ['EP_JIT_CACHE_DIR'])"
   ```
   预期打印 `/tmp/override`（用户值优先）。

**需要观察的现象**：步骤 2 能看到烘焙的字面量；步骤 3 拿到烘焙值；步骤 4 拿到用户覆盖值。

**预期结果**：覆盖优先级 = 用户 `export` > 构建期烘焙 > 代码硬默认（`$HOME/.deep_ep`）。

**待本地验证**：如果是在开发模式（`python setup.py build` + 软链）下而非真 `pip install`，`envs.py` 可能没生成，步骤 2 会 `ImportError` 被静默吞掉——此时退回到代码硬默认。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `setup.py` 用「动态生成 `envs.py`」而不是直接把值编进 `_C.so`？

**答案**：因为这些变量（缓存目录、NCCL 路径）本质是**字符串配置**，运行期还要可被 `export` 覆盖。写成 Python 字典 `persistent_envs` 后，import 时与 `os.environ` 合并最自然；若编进 `.so`，覆盖逻辑就得在 C++ 里重写一遍 `getenv` 优先级，且改值要重新编译。烘焙成 `.py` 是最轻量、最易调试的做法。

**练习 2**：如果构建机**没设** `EP_JIT_CACHE_DIR`，运行期用户也**没设**，最终缓存目录是什么？

**答案**：`$HOME/.deep_ep`。因为 `envs.py` 里不会有这一行（`if name in os.environ else ''`），import 时 `persistent_envs` 没有该 key，`os.environ` 也没有，于是 C++ `compiler.hpp:52` 的硬默认 `$HOME/.deep_ep` 生效。

**练习 3**：`EP_NUM_TOPK_IDX_BITS` 既是持久化变量，又在 `setup.py:163-166` 被当成**编译宏** `-DEP_NUM_TOPK_IDX_BITS=...` 传给安装期编译。这两条路径会冲突吗？

**答案**：不冲突，而是**分工**。安装期编译宏决定了 `_C.so` 里 `topk_idx_t` 的位宽（32 或 64，影响 C++ 绑定层）；而烘焙进 `envs.py` 的值会在运行期注入 `os.environ`，再被 JIT 编译器 [compiler.hpp:71-72](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/jit/compiler.hpp#L71-L72) 读出来，作为 `-DEP_NUM_TOPK_IDX_BITS=...` 传给运行时编译的 CUDA 内核，保证绑定层与 JIT 内核层位宽一致。

---

### 4.3 JIT 调试类环境变量精读

#### 4.3.1 概念说明

JIT 类变量是本讲最实用的「调参旋钮」，因为 DeepEP 的内核都是运行时编译的，出问题时你几乎一定要靠它们看 `nvcc` 到底编了什么。它们全部集中在 `csrc/jit/compiler.hpp` 的 `Compiler` 与 `NVCCCompiler` 两个类里，在「构造编译器 → 决定编译标志 → 编译 → 反汇编」四个阶段被读取。

核心变量分三组：

- **编译标志组**：`EP_JIT_CPP_STANDARD`（C++ 标准，默认 20）、`EP_JIT_NVCC_COMPILER`（指定 nvcc 路径）。
- **诊断输出组**：`EP_JIT_DEBUG`（总开关，打开后会打印 nvcc 命令行、PTXAS 日志）、`EP_JIT_PRINT_COMPILER_COMMAND`（只打印命令行）、`EP_JIT_PTXAS_VERBOSE`（PTXAS 详细输出）。
- **产物 dump 组**：`EP_JIT_DUMP_PTX`、`EP_JIT_DUMP_SASS`、`EP_JIT_DUMP_ASM`（同时 dump PTX 和 SASS）、`EP_JIT_WITH_LINEINFO`（嵌入源码行号供 profiler 用）。
- **校验组**：`EP_JIT_PTXAS_CHECK`（断言内核没有溢出到 local memory）。

#### 4.3.2 核心流程

JIT 编译一次内核的流程（详见 [u4-l3](u4-l3-jit-cache-and-load.md)），环境变量在其中的介入点：

```
build(name, code)
  │
  ├─ Compiler 构造：读 EP_JIT_CPP_STANDARD/EP_JIT_DEBUG/EP_JIT_PTXAS_VERBOSE
  │   /EP_JIT_WITH_LINEINFO/EP_GIN_GDAKI_DEBUG/EP_NUM_TOPK_IDX_BITS → 拼 flags
  │
  ├─ NVCCCompiler 构造：读 EP_JIT_NVCC_COMPILER → 选 nvcc 路径；拼 -I/--gpu-architecture
  │
  ├─ 若 EP_JIT_DUMP_ASM or EP_JIT_DUMP_PTX：compile 时多产出 kernel.ptx
  ├─ 若 EP_JIT_DUMP_ASM or EP_JIT_DUMP_SASS：disassemble 产出 kernel.sass
  │
  ├─ compile():
  │    ├─ EP_JIT_DEBUG/EP_JIT_PRINT_COMPILER_COMMAND → 打印 nvcc 命令行
  │    ├─ EP_JIT_PTXAS_CHECK → 断言输出里无 "Local memory used"
  │    └─ EP_JIT_DEBUG/EP_JIT_PTXAS_VERBOSE → 打印 PTXAS 日志
  │
  └─ fsync + 原子 rename 到 cache 目录（EP_JIT_CACHE_DIR）
```

#### 4.3.3 源码精读

**编译标志的拼装**在 `Compiler` 构造函数里一次性完成：

[csrc/jit/compiler.hpp:56-72](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/jit/compiler.hpp#L56-L72) —— 这一段是 JIT 编译标志的总开关：

- `EP_JIT_CPP_STANDARD`（默认 20）决定 `-std=c++{N}`；
- `EP_JIT_DEBUG` 或 `EP_JIT_PTXAS_VERBOSE` 打开时追加 `--ptxas-options=--verbose`（让 PTXAS 打印寄存器/共享内存占用）；
- `EP_JIT_DEBUG` 或 `EP_JIT_WITH_LINEINFO` 打开时追加 `-Xcompiler -rdynamic -lineinfo`（后者让 Nsight Systems 等工具能映射回源码行）；
- `EP_GIN_GDAKI_DEBUG` 打开时定义 `NCCL_DEVICE_GIN_GDAKI_ENABLE_DEBUG=1` 宏，让 Gin GDAKI 路径打印调试。

**指定 nvcc 路径**在 `NVCCCompiler` 构造里：

[csrc/jit/compiler.hpp:208-209](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/jit/compiler.hpp#L208-L209) —— 默认 `cuda_home/bin/nvcc`，若 `EP_JIT_NVCC_COMPILER` 非空则覆盖。用于「系统有多个 CUDA 版本、想指定某一个给 DeepEP 用」的场景。

**dump PTX/SASS** 在 `build` 里按需触发：

[csrc/jit/compiler.hpp:127-138](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/jit/compiler.hpp#L127-L138) —— `EP_JIT_DUMP_ASM` 或 `EP_JIT_DUMP_PTX` 为真时，`compile` 多写一份 `kernel.ptx`；`EP_JIT_DUMP_ASM` 或 `EP_JIT_DUMP_SASS` 为真时，调 `disassemble` 用 `cuobjdump --dump-sass` 产出 `kernel.sass`。三者都会落到缓存目录，随 `kernel.cu`、`kernel.cubin` 一起保留。

> 名字小考：`EP_JIT_DUMP_ASM`（asm=汇编）等价于「同时 dump PTX 和 SASS」；只想看 PTX 用 `EP_JIT_DUMP_PTX`，只想看 SASS 用 `EP_JIT_DUMP_SASS`。

**PTXAS_CHECK 的校验**在 `compile` 末尾：

[csrc/jit/compiler.hpp:256-257](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/jit/compiler.hpp#L256-L257) —— 打开后，用正则检查 PTXAS 输出里是否出现 `Local memory used`，若出现就 `EP_HOST_ASSERT(false)`。Local memory（线程溢出到显存的栈空间）通常意味着寄存器占用过高，对追求「省 SM、低寄存器」的通信内核是危险信号，所以专门做断言。

**打印编译命令**受两个变量控制（`EP_JIT_DEBUG` 或 `EP_JIT_PRINT_COMPILER_COMMAND`），见 [csrc/jit/compiler.hpp:234-235](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/jit/compiler.hpp#L234-L235)。

#### 4.3.4 代码实践（本讲主实践任务）

**实践目标**：实际设置 3 个 JIT 调试变量，跑一次测试，观察各自的输出与产物，总结它们的调试场景。

**操作步骤**：

```bash
# 1) 清空缓存，强制重新编译，便于观察
export EP_JIT_CACHE_DIR=/tmp/deep_ep_jit_demo
rm -rf /tmp/deep_ep_jit_demo

# 2) 同时开三个开关
export EP_JIT_DEBUG=1                # 打印 nvcc 命令行 + PTXAS 日志
export EP_JIT_DUMP_SASS=1            # 落地 kernel.sass
export EP_JIT_PTXAS_CHECK=1          # 断言无 local memory

# 3) 跑一次（单机 8 卡）
python tests/elastic/test_ep.py
```

**需要观察的现象**：

1. 控制台会打印形如 `Running NVCC command: cd /tmp/... && /usr/local/cuda/bin/nvcc ... kernel.cu -cubin ...` 的行（来自 `EP_JIT_DEBUG` / `EP_JIT_PRINT_COMPILER_COMMAND`）。
2. 控制台会打印 PTXAS 的寄存器/栈占用日志（`--ptxas-options=--verbose` 的输出）。
3. 若某个内核意外用了 local memory，进程会因 `EP_JIT_PTXAS_CHECK` 的断言而中断（正常情况下不应触发）。
4. 在 `/tmp/deep_ep_jit_demo/cache/kernel.<name>.<hash>/` 目录下能看到 `kernel.cu`（生成的源码）、`kernel.cubin`（二进制）、`kernel.sass`（反汇编）。

**预期结果与调试场景总结**：

| 变量 | 产物 / 输出 | 适合的调试场景 |
|---|---|---|
| `EP_JIT_DEBUG=1` | nvcc 命令行 + PTXAS 日志 + 后续加载/启动日志 | 想确认「DeepEP 到底用哪个 nvcc、什么 flag 编的」；排查编译失败 |
| `EP_JIT_DUMP_SASS=1` | `kernel.sass` 文件 | 想看内核**实际生成**的机器指令，核对某条 PTX（如 TMA、mbarrier）是否被编译器改写；性能微调时数指令 |
| `EP_JIT_PTXAS_CHECK=1` | 断言失败则崩溃 | 怀疑寄存器溢出导致 local memory 拖慢内核；CI 里守卫「低寄存器」不变式 |

**待本地验证**：本机是否有 Hopper GPU 与可编译的 nvcc；若没有，可只读 `kernel.cu`（生成的源码）来理解 JIT 代码生成（参见 [u4-l2](u4-l2-kernel-codegen.md)），但 SASS/PTXAS 输出需要真实编译环境。

> 进阶提示：`EP_JIT_DUMP_PTX=1` 会产出 `kernel.ptx`（中间 ISA），适合对比「PTX 写的是什么 vs SASS 编成了什么」，是定位编译器优化疑点的利器。

#### 4.3.5 小练习与答案

**练习 1**：`EP_JIT_DEBUG`、`EP_JIT_PTXAS_VERBOSE`、`EP_JIT_PRINT_COMPILER_COMMAND` 三者都能产生「打印」，它们分别打印什么？哪个是超集？

**答案**：`EP_JIT_PRINT_COMPILER_COMMAND` 只打印 nvcc/cuobjdump 命令行；`EP_JIT_PTXAS_VERBOSE` 只打印 PTXAS 详细日志（寄存器/栈占用）；`EP_JIT_DEBUG` 是超集——它同时打开命令行打印、PTXAS verbose、源码行信息（`-lineinfo`），还触发 `kernel_runtime.hpp`、`launch_runtime.hpp` 里的更多调试打印。排查问题时先开 `EP_JIT_DEBUG` 一个就够。

**练习 2**：为什么 `EP_JIT_PTXAS_CHECK` 用正则匹配 PTXAS 输出里的 `Local memory used`，而不是直接读寄存器数？

**答案**：因为 DeepEP 的设计目标是「通信内核尽量不溢出到 local memory」（local memory 访问远慢于寄存器）。寄存器数高低是性能取舍，没有绝对阈值；而「出现 local memory」几乎总是意味着异常溢出，是个明确的「不该发生」信号，所以用布尔式的存在性断言最合适。

**练习 3**：设置了 `EP_JIT_DUMP_SASS=1` 后，第二次跑同样的测试**没有**重新生成 `kernel.sass`，为什么？

**答案**：因为 JIT 有两级缓存（进程内 `KernelRuntimeCache` + 磁盘内容寻址目录）。第二次跑时，签名命中已存在的缓存目录，直接走 `kernel_runtime_cache->get(dir_path)` 返回，根本不会进入 `compile`/`disassemble` 分支（见 [compiler.hpp:116-117](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/jit/compiler.hpp#L116-L117)）。想强制重新生成，需要清空 `EP_JIT_CACHE_DIR` 或换一个目录。

---

### 4.4 RDMA 服务级（SL）与虚拟通道（VL）：流量隔离原理

#### 4.4.1 概念说明

学习目标里要求理解 SL/VL 与流量隔离，这是 RDMA 网络里偏硬件的概念，但在 DeepEP 多租户集群里很关键。

- **虚拟通道（Virtual Lane, VL）**：InfiniBand 在**一条物理链路**上切出多个虚拟通道（VL0~VL15，常用 0~7），每个 VL 有独立的缓冲区和仲裁，互不阻塞——就像一条公路上画了多条车道，堵一条不影响另一条。
- **服务级（Service Level, SL）**：RDMA 报文头里一个 0~15 的字段。发送方在报文里打一个 SL，交换机会按 SL→VL 的映射表（在交换机/网卡上配置）决定这条报文走哪个 VL。
- **流量隔离（Traffic Isolation）**：把「EP 通信」和「其他训练通信（如 TP 的 all-reduce）」打到不同 SL，进而落到不同 VL，互不干扰。

DeepEP 让你能通过 `EP_OVERRIDE_RDMA_SL` 或 API 参数 `sl_idx` 指定 SL，正是为了这个目的。

#### 4.4.2 核心流程

DeepEP 里 SL 的传递链路：

```
用户设置                              DeepEP 内部
─────────                             ─────────────
export EP_OVERRIDE_RDMA_SL=1          ElasticBuffer.__init__
   或 sl_idx=1                        读取覆盖 sl_idx (elastic.py:323)
        │                                   │
        ▼                                   ▼
                                       传给 NCCLSymmetricMemoryContext
                                       设到 reqs.ginTrafficClass (nccl.cu:96)
                                              │
                                              ▼
                                       NCCL Gin 在建 QP 时用该 SL
                                       → 报文打上 SL → 交换机映射到 VL
```

数学上，SL 到 VL 的映射由交换机上的表决定，记为：

\[ \text{VL} = \text{SL2VL}(\text{SL}) \]

其中 SL2VL 是一个 16 项的查找表（IB 规范允许 SL 取 0~15）。流量隔离的效果可粗略建模：设两条流量 \(A\)（EP）与 \(B\)（TP）被分到不同 VL，则它们共享物理链路总带宽 \(C\) 时，各自在拥塞下的吞吐下界为：

\[ \text{thr}(A) \geq C \cdot \frac{w_A}{w_A + w_B} \quad \text{（按权重仲裁）} \]

而不隔离（同 VL）时则可能出现队头阻塞，使某条流量被另一条完全饿死。这正是 README 建议「把 expert-parallel 与其他 workload 分到不同 VL」的原因——对应 [README.md:376-384](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/README.md#L376-L384)。

#### 4.4.3 源码精读

**Python 侧读取 `EP_OVERRIDE_RDMA_SL`**，覆盖构造参数 `sl_idx`：

[deep_ep/buffers/elastic.py:322-324](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/buffers/elastic.py#L322-L324) —— 只要环境变量存在，就把 `sl_idx` 强转 int 覆盖掉。注意它用的是 `'EP_OVERRIDE_RDMA_SL' in os.environ`（存在性判断），所以设成 `=0` 也会生效（合法地把 SL 设为 0）。

**C++ 侧把 `sl_idx` 设进 Gin QP 的 traffic class**：

[csrc/kernels/backend/nccl.cu:93-99](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/kernels/backend/nccl.cu#L93-L99) —— `reqs.ginTrafficClass = sl_idx;` 把 SL 透传给 NCCL Gin，建 QP 时即生效；同一片段里还能看到 `ginConnectionType`（hybrid 走 `RAIL`、direct 走 `FULL`），与 [u3-l1](u3-l1-topology-domains.md) 呼应。

> 旁注：README 还提到 `EP_NIC_NAME`（默认 `mlx5_0`），它不是用来隔离流量的，而是用来**选哪块网卡**做带宽/能力探测。对应 [deep_ep/utils/envs.py:21](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/utils/envs.py#L21)，被 `get_rdma_gbs` / `check_fast_rdma_atomic_support` 用于解析 `ibstat` 输出（决定 RDMA 带宽与是否支持 fast atomic，进而影响 QP 数量，见 [u3-l3](u3-l3-sm-qp-analytical.md)）。

#### 4.4.4 代码实践

**实践目标**：理解 SL 的可观测效果（在不一定有硬件的环境下，至少验证它被正确读取并传到 Gin）。

**操作步骤**：

1. 开启调试 + 设一个非零 SL：
   ```bash
   export EP_BUFFER_DEBUG=1
   export EP_OVERRIDE_RDMA_SL=1
   ```
2. 在多节点环境跑 `python tests/elastic/test_ep.py`，观察是否能正常建出 Gin QP（`EP_BUFFER_DEBUG` 会打印 `EP NCCL device communicator has ... allocated QPs`）。
3. （可选，需 IB 交换机权限）在交换机上检查 SL→VL 映射，确认 SL=1 落到了与默认流量不同的 VL。

**需要观察的现象**：buffer 仍能正常初始化（说明 SL 值被 Gin 接受）；若 SL 值在交换机侧未配置，可能表现为带宽下降或连不上（这是网络侧问题，非 DeepEP bug）。

**预期结果**：SL 被读取并透传；真正的「隔离效果」需在集群级用 `ibdump`/交换机计数器对比 EP 流量与其他流量是否落在不同 VL。

**待本地验证**：SL→VL 映射与流量隔离效果强依赖交换机配置与多节点硬件，单机环境只能验证「变量被读取」，隔离效果待本地/集群验证。

#### 4.4.5 小练习与答案

**练习 1**：`EP_OVERRIDE_RDMA_SL` 与构造参数 `sl_idx` 同时给出不同的值，谁赢？

**答案**：`EP_OVERRIDE_RDMA_SL` 赢。因为 [elastic.py:323-324](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/buffers/elastic.py#L322-L324) 在构造函数里**无条件用环境变量覆盖** `sl_idx`（只要环境变量存在）。所以环境变量是最高优先级，便于集群管理员统一管控。

**练习 2**：为什么把 EP 流量与其他流量隔离到不同 VL 能提升整体性能，而不是「分流后总带宽变小」？

**答案**：物理链路总带宽 \(C\) 不变，但不同 VL 有独立缓冲区与仲裁，避免了队头阻塞与跨流量互相拖累。其收益主要来自「降低尾延迟」和「避免拥塞放大」，而非抬高峰值带宽。在 EP 这种对延迟敏感、报文大小差异大的场景下尤其受益。

---

## 5. 综合实践

把本讲的知识串起来，做一次「从烘焙到 JIT 产物」的端到端观察。

**任务**：在构建期烘焙一个自定义缓存目录，运行期用 JIT 调试变量编译一次内核，最终拿到该内核的 `.cu`/`.cubin`/`.sass` 三件套，并解释每个文件分别由哪个环境变量促成。

**步骤**：

1. **构建期烘焙**：
   ```bash
   export EP_JIT_CACHE_DIR=/opt/deep_ep_baked_cache
   pip install .
   python -c "import deep_ep, os; print(os.path.dirname(deep_ep.__file__)+'/envs.py')" | xargs cat
   ```
   预期在 `envs.py` 里看到 `persistent_envs['EP_JIT_CACHE_DIR'] = '/opt/deep_ep_baked_cache'`。
2. **运行期（换一个 shell，不设 `EP_JIT_CACHE_DIR`）**：
   ```bash
   export EP_JIT_DEBUG=1
   export EP_JIT_DUMP_SASS=1
   export EP_JIT_PTXAS_CHECK=1
   python tests/elastic/test_ep.py
   ```
   预期：`import deep_ep` 时烘焙值把 `os.environ['EP_JIT_CACHE_DIR']` 设为 `/opt/deep_ep_baked_cache`（可用步骤 1 的 `python -c` 验证），所以内核会编译到该目录。
3. **检查产物**：进入 `/opt/deep_ep_baked_cache/cache/kernel.<name>.<hash>/`，列出文件。
4. **回答**：
   - `kernel.cu` 是谁生成的？（答：JIT 代码生成，见 [u4-l2](u4-l2-kernel-codegen.md)）
   - `kernel.cubin` 是谁编译的？（答：`NVCCCompiler::compile`，受 `EP_JIT_DEBUG` 打印命令行）
   - `kernel.sass` 是哪个环境变量带来的？（答：`EP_JIT_DUMP_SASS=1`）
   - 为什么内核落在了 `/opt/deep_ep_baked_cache` 而不是 `$HOME/.deep_ep`？（答：构建期烘焙 + 用户未覆盖，两级默认值机制生效）
5. **覆盖实验**：在步骤 2 的 shell 里追加 `export EP_JIT_CACHE_DIR=/tmp/override`，重跑（记得清旧缓存或换内核签名），确认产物落到 `/tmp/override`——证明运行期 `export` 优先于烘焙值。

**预期结果**：你能完整复述「烘焙 → import 注入 → C++ `getenv` 读取 → JIT 编译 → 落盘」的全链路，并能区分四个 JIT 变量各自的产物与场景。

**待本地验证**：整套流程需要可编译的 CUDA 12.3+ 与 Hopper GPU；若仅做源码阅读，可跳过编译，直接打开任意一个 `kernel.<name>.<hash>/kernel.cu` 对照 [u4-l2](u4-l2-kernel-codegen.md) 理解代码生成。

## 6. 本讲小结

- DeepEP 的环境变量分**通用 / 网络 / JIT / 调试剖析与构建**四大类，README 是权威清单；Python 侧用 `os.environ.get`、C++ 侧用统一的 `get_env<T>` 模板读取同一批变量。
- **持久化变量**（`EP_JIT_CACHE_DIR`、`EP_JIT_PRINT_COMPILER_COMMAND`、`EP_NUM_TOPK_IDX_BITS`、`EP_NCCL_ROOT_DIR`）走独特的两级默认值：构建期由 `setup.py` 烘焙进动态生成的 `envs.py`，import 时由 `__init__.py` 仅在用户未设时注入 `os.environ`，C++ 侧靠进程环境变量天然共享，无需特殊代码。
- JIT 调试变量是排查运行时编译问题的主力：`EP_JIT_DEBUG` 看命令行与 PTXAS 日志，`EP_JIT_DUMP_SASS/DUMP_PTX` 落地汇编产物，`EP_JIT_PTXAS_CHECK` 守卫 local memory 溢出，`EP_JIT_CACHE_DIR` 控制缓存目录（且受持久化机制保护）。
- `EP_OVERRIDE_RDMA_SL` 把 RDMA 服务级透传给 NCCL Gin 的 `ginTrafficClass`，配合 IB 的 SL→VL 映射实现 EP 流量与其他流量的物理通道级隔离。
- 环境变量读取要小心 Python「字符串 `'0'` 为真」的陷阱；C++ 的 `get_env<int>` 会自动 `sscanf` 转换，更安全。

## 7. 下一步学习建议

- 想深入 JIT 编译流水线（代码生成、缓存、cubin 加载）：依次读 [u4-l1](u4-l1-jit-overview.md)、[u4-l2](u4-l2-kernel-codegen.md)、[u4-l3](u4-l3-jit-cache-and-load.md)、[u4-l4](u4-l4-launch-framework.md)。
- 想理解 `EP_BUFFER_DEBUG` 打印的「SM approximation / channels per SM / QPs」背后的建模：读 [u3-l3（SM/QP 解析计算）](u3-l3-sm-qp-analytical.md)。
- 想搞清 `EP_DISABLE_GIN`、`EP_REUSE_NCCL_COMM` 背后的 communicator 复用与拓扑探测：读 [u8-l2（NCCL communicator 复用与拓扑探测）](u8-l2-nccl-comm-reuse.md)。
- 想看 PTX 原语层面的细节（`EP_JIT_DUMP_SASS` 产出的 SASS 对应的源码层）：读 [u8-l1（PTX 原语：TMA、mbarrier 与 fence.proxy）](u8-l1-ptx-tma-mbarrier.md)。
- 想回顾构建期 NCCL/NVSHMEM SO 名称解析（`EP_NCCL_ROOT_DIR`/`EP_NVSHMEM_ROOT_DIR` 的用途）：回看 [u1-l3（环境依赖、安装与构建流程）](u1-l3-build-and-install.md)。
