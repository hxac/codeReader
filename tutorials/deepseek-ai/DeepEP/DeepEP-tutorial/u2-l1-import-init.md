# import deep_ep 背后做了什么：NCCL 校验与 JIT 初始化

## 1. 本讲目标

在上一讲（u1-l4）里，我们已经在单机 8 卡上跑通了 `test_ep.py`，看到了 `ElasticBuffer.dispatch/combine` 的输出。但你有没有想过：**为什么只要写一行 `import deep_ep`，后台就 silently 完成了一堆"启动准备工作"？** 这些工作如果失败，后面的 dispatch 根本无法运行。

本讲的目标是让你能逐行说清楚 `import deep_ep` 时到底发生了什么。具体地，学完后你应当掌握：

1. `import deep_ep` 触发的四步自动初始化流程，以及它们的执行顺序与依赖关系。
2. `check_nccl_so()` 为什么要校验"运行时被加载的 NCCL"与"DeepEP 链接的 NCCL"在**二进制层面**完全一致，以及 `EP_SUPPRESS_NCCL_CHECK` 如何跳过它。
3. `init_jit()` 如何把"库根目录 / CUDA home / NCCL root"三条路径注入到 C++ 侧的 JIT 编译器，为后续运行时编译 CUDA 内核做好准备。
4. `find_cuda_home()` 与 `find_nccl_root()` 各自如何定位工具链，以及持久化环境变量（如 `EP_NCCL_ROOT_DIR`）在其中的作用。

本讲只聚焦 **import 阶段**，不会深入 dispatch/combine 内核本身——那是 u5/u6 的话题。

## 2. 前置知识

阅读本讲前，你应当已经了解（来自 u1 单元）：

- **专家并行（EP）与 dispatch/combine**：DeepEP 的核心是把 token 按路由表分发到目标 expert 所在 rank（dispatch），再把 expert 输出送回原 rank 并加权归约（combine）。
- **NVLink（节点内）与 RDMA（节点间）** 两类物理通信链路。
- **DeepEP V2 的两大特征**：使用 header-only 的 **NCCL Gin 后端**（取代 V1 的 NVSHMEM），以及**运行时 JIT 编译** CUDA 内核（而不是安装期就把所有内核编进 `.so`）。
- **两条编译路径的区别**（u1-l2/u1-l3）：`csrc/*` 在 `pip install` 时编进 `deep_ep/_C.so`；而 `deep_ep/include/impls/*.cuh` 作为 header-only 模板，**运行时**才被 JIT 实例化。

本讲会反复用到两个底层概念，先在这里解释：

- **`/proc/self/maps`**：Linux 下每个进程都有这样一个虚拟文件，列出该进程当前内存空间里映射的所有共享库（`.so`）及其路径。Python 里 `import torch` 后，PyTorch 会按需 `dlopen` 加载它依赖的 `libnccl.so`，这会立刻反映到 `/proc/self/maps` 中。DeepEP 正是借它来"看"PyTorch 到底加载了哪一份 NCCL。
- **SONAME 与 `libnccl.so*`**：NCCL 的库文件通常有多个名字：`libnccl.so`（无版本号符号链接）、`libnccl.so.2`（带主版本号的 SONAME）、`libnccl.so.2.x.x`（完整版本文件）。`pip` 安装的 `nvidia-nccl-cu13` 往往**只发 SONAME 文件**（`libnccl.so.2`）而不带无版本号软链，这是 u1-l3 里 `get_nccl_lib_name` 要解决的痛点。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
| --- | --- |
| [deep_ep/__init__.py](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/__init__.py) | Python 包入口。import 时自动执行：加载持久化环境变量 → `check_nccl_so()` → `init_jit()` → 暴露 `Buffer`/`ElasticBuffer` 等 API。 |
| [deep_ep/utils/find_pkgs.py](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/utils/find_pkgs.py) | 定位 NVIDIA pip 包（NCCL/NVSHMEM）的安装根目录。`find_nccl_root` 是本讲的核心工具。 |
| [csrc/jit/api.hpp](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/jit/api.hpp) | JIT 子系统的 C++ 入口。定义 `init()` 函数（即 Python 侧的 `_C.init_jit`）和 `register_apis`。 |
| [csrc/jit/compiler.hpp](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/jit/compiler.hpp) | JIT 编译器。`prepare_init` 接收并存储三条路径；构造函数里组装 nvcc 编译标志。 |
| [csrc/jit/kernel_runtime.hpp](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/jit/kernel_runtime.hpp) | 加载编译产物 `.cubin`。`prepare_init` 接收 CUDA home（用于找 `cuobjdump`）。 |
| [csrc/jit/include_parser.hpp](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/jit/include_parser.hpp) | 解析 `<deep_ep/...>` 头文件并计算哈希，用于缓存签名。`prepare_init` 接收库根路径。 |
| [csrc/python_api.cpp](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/python_api.cpp) | pybind11 模块 `_C` 的定义。把 jit/legacy/elastic 三组 API 注册到 Python。 |
| [setup.py](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/setup.py) | 构建脚本。生成 `deep_ep/envs.py`（持久化变量），并用 `get_nccl_lib_name` 解析真实 SO 名。 |

> 提示：`deep_ep/envs.py` **不在仓库里**——它是 `pip install` 时由 `setup.py` 动态生成的，所以你在源码里 `grep` 不到它。下一节会详细讲它怎么来。

## 4. 核心概念与源码讲解

### 4.1 `import deep_ep` 的执行链总览

#### 4.1.1 概念说明

Python 的 `import` 并不只是"把名字搬进命名空间"。当一个包的 `__init__.py` 被首次加载时，**它的顶层语句会从上到下同步执行一遍**。DeepEP 把这个副作用用到了极致：它在 `__init__.py` 里安排了若干"启动自检与初始化"语句，使得 `import deep_ep` 这一行本身就完成了所有准备工作。

理解这一点非常关键：**如果你绕过 `import deep_ep`、直接去 `import deep_ep.buffer.elastic`，这些初始化仍然会先执行**（因为 Python 加载子模块前必须先执行父包的 `__init__.py`）。所以这些初始化是"必然发生"的，不是"可选的"。

#### 4.1.2 核心流程

`import deep_ep` 触发的执行链（严格自上而下）：

```text
1. 加载持久化环境变量
   └─ try: from .envs import persistent_envs
      └─ 把构建期烘焙的默认值写入 os.environ（不覆盖用户已设值）

2. 定义三个工具函数（仅定义，不执行）
   ├─ find_cuda_home()   —— 定位 CUDA 安装目录
   ├─ check_nccl_so()    —— NCCL 二进制一致性校验
   └─ init_jit()         —— 注入 JIT 工具链路径

3. 执行两个初始化调用（重点！）
   ├─ check_nccl_so()    —— 不通过则抛 AssertionError，整个 import 失败
   └─ init_jit()         —— 把三条路径传给 C++ 侧 _C.init_jit

4. 暴露公开 API
   ├─ from .buffers.legacy import Buffer
   ├─ from .buffers.elastic import ElasticBuffer, EPHandle
   ├─ from .utils.event import EventOverlap, EventHandle
   └─ from deep_ep._C import Config, topk_idx_t
```

注意第 3 步的顺序：**先 `check_nccl_so()`，后 `init_jit()`**。这意味着如果 NCCL 校验失败，JIT 根本不会被初始化——这是有意的"fail fast"：在一个 NCCL 环境不一致的系统里继续初始化没有意义。

#### 4.1.3 源码精读

先看 `__init__.py` 末尾的"执行 + 导出"段落，这是整条链的入口：

[deep_ep/__init__.py:82-95](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/__init__.py#L82-L95) —— 模块级语句：先调用两个初始化函数，**再** import 子模块暴露 API。顺序不能颠倒，因为 `buffers/elastic.py` 内部会用到 JIT 已经初始化好的状态。

再看持久化环境变量的加载（第 1 步）：

[deep_ep/__init__.py:10-18](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/__init__.py#L10-L18) —— `try/except ImportError` 包裹 `from .envs import persistent_envs`。这里用 `try` 是因为 `envs.py` 是构建期生成的；如果有人直接从源码跑（没装包），`envs.py` 不存在，`ImportError` 会被静默吞掉，相当于"没有持久化默认值"，不影响后续逻辑。关键是第 15 行的 `if key not in os.environ`——**用户在 shell 里 `export` 的值永远优先**，持久化值只作"兜底默认"。

那 `persistent_envs` 字典里到底有什么？它来自构建期生成的 `deep_ep/envs.py`。生成逻辑在 setup.py：

[setup.py:13](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/setup.py#L13) —— `persistent_env_names` 元组列出四个变量：`EP_JIT_CACHE_DIR`、`EP_JIT_PRINT_COMPILER_COMMAND`、`EP_NUM_TOPK_IDX_BITS`、`EP_NCCL_ROOT_DIR`。这四个变量有一个共同特点：**它们的合理值往往取决于集群构建环境**（比如缓存目录在哪个共享文件系统、topk 索引位宽是不是和训练脚本一致），所以在构建机器上定下来、烘焙进包，比让每个用户运行时再设要稳妥。

[setup.py:78-89](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/setup.py#L78-89) —— `generate_default_envs` 实际生成 `envs.py`。第 82-83 行遍历 `persistent_env_names`，**只有当变量在构建机的 `os.environ` 里存在时**才写进字典；否则跳过（生成空行）。所以 `envs.py` 的内容形如：

```python
# Pre-installed environment variables
persistent_envs = dict()
persistent_envs['EP_JIT_CACHE_DIR'] = '/shared/deep_ep_cache'
persistent_envs['EP_NCCL_ROOT_DIR'] = '/opt/nccl'
```

这条"构建期烘焙 → 运行期可覆盖"的链路，正是 u1-l3 提到的两级默认值机制在源码层面的落点。README 也明确列出了这四个持久化变量（见 README "Persistent Variables" 一节）。

#### 4.1.4 代码实践

**实践目标**：亲眼看到 `envs.py` 是构建期生成、且 `persistent_envs` 确实只作"兜底默认"。

**操作步骤**：

1. 在已 `pip install` 了 DeepEP 的环境里，找到安装目录下的 `envs.py`：

   ```bash
   python -c "import deep_ep, os; print(os.path.join(os.path.dirname(deep_ep.__file__), 'envs.py'))"
   ```

2. 用编辑器打开该路径下的 `envs.py`，观察里面有哪些键（取决于构建机器当时 `export` 了什么）。

3. 写一个最小脚本 `probe.py`：

   ```python
   import os
   # 在 import deep_ep 之前，先故意设一个持久化变量
   os.environ['EP_JIT_CACHE_DIR'] = '/tmp/I_SET_THIS_MYSELF'
   import deep_ep
   print('EP_JIT_CACHE_DIR =', os.environ.get('EP_JIT_CACHE_DIR'))
   print('EP_NCCL_ROOT_DIR =', os.environ.get('EP_NCCL_ROOT_DIR'))
   ```

**需要观察的现象**：

- `EP_JIT_CACHE_DIR` 仍然是你设的 `/tmp/I_SET_THIS_MYSELF`，**没有被 `envs.py` 里的值覆盖**（因为 `__init__.py:15` 的 `if key not in os.environ` 守卫）。
- `EP_NCCL_ROOT_DIR` 如果你在 shell 里没设，就会显示 `envs.py` 里烘焙的构建期值；如果构建期也没设，则为 `None`。

**预期结果**：用户运行时 `export` 的值 > 构建期烘焙值 > 空。这验证了两级默认值的优先级。

> 如果你是从源码直接跑（没有 `pip install`），`envs.py` 不存在，`probe.py` 里两个变量都可能为 `None`——这本身也验证了 `try/except ImportError` 的作用。本步骤是否能看到烘焙值「待本地验证」（取决于你是否走过 `pip install` 流程）。

#### 4.1.5 小练习与答案

**练习 1**：如果有人把 `__init__.py` 第 83-84 行的 `check_nccl_so()` 和 `init_jit()` 调用注释掉，改为懒加载（第一次 `ElasticBuffer.dispatch` 时才调用），会有什么潜在问题？

**参考答案**：NCCL 一致性问题会被推迟到第一次 dispatch 时才暴露，届时用户已经写了一大段训练代码，错误栈会深埋在 dispatch 内部、难以定位；而且 JIT 初始化（找 nvcc、算编译标志）本身有耗时，把它从 import 阶段挪到首次 dispatch，会让"第一次 dispatch"莫名其妙地慢。import 阶段 fail fast 是更友好的设计。

**练习 2**：为什么 `__init__.py:13` 用 `from .envs import persistent_envs` 而不是 `from .utils.envs import persistent_envs`？（注意两者的区别。）

**参考答案**：`deep_ep/envs.py`（包根下、构建期生成）和 `deep_ep/utils/envs.py`（仓库里已有的工具模块）是**两个不同的文件**。前者只存 `persistent_envs` 字典，后者存 `init_dist`、`get_nvlink_gbs` 等运行时工具函数。用 `.envs` 显式指向包根下那个生成文件，避免与 `utils/envs.py` 同名冲突。u1-l4 里提到的"README 把 `tests/utils/envs.py` 写成笔误"也是这个同名坑的延伸。

---

### 4.2 check_nccl_so：NCCL 运行时与链接版本一致性校验

#### 4.2.1 概念说明

这是整个 import 链里**最容易踩坑、也最容易被关掉**的一步。要理解它，得先理解一个 NCCL 生态的现实：

- **PyTorch 自带一份 NCCL**。`import torch; torch.distributed` 初始化时会 `dlopen` 加载某个 `libnccl.so`（可能来自 PyTorch wheel 捆绑的、也可能来自系统）。
- **DeepEP 链接了另一份 NCCL**。`pip install` DeepEP 时，setup.py 通过 `-l:<nccl_lib>` 把 DeepEP 的 `_C.so` 与某份 `libnccl.so` 链接在一起（见 u1-l3 的 `get_nccl_lib_name`）。

理想情况下这两份是**同一个文件**，于是进程里只有一份 NCCL 符号，一切正常。但如果它们**版本不同**（比如 PyTorch 加载了 `libnccl.so.2.27`，而 DeepEP 链接的是 `libnccl.so.2.30`），就会出现"同一进程里两份 NCCL 符号"的灾难：调用方拿到的 `ncclComm_t` 句柄、内部结构体布局可能对不上，轻则结果错误，重则段错误。这种 bug 极难排查。

`check_nccl_so()` 的职责就是：**在 import 阶段就发现这种不一致，直接报错，而不是让它在 dispatch 时以玄学方式爆炸。**

#### 4.2.2 核心流程

`check_nccl_so()` 的判定逻辑（伪代码）：

```text
if EP_SUPPRESS_NCCL_CHECK 为真:
    直接 return（跳过整段校验）

# 第一步：从 /proc/self/maps 读出"运行时实际被加载的"libnccl.so 路径
loaded_nccl_so = 扫描 /proc/self/maps 里所有含 'libnccl' 的行
    └─ 若出现多份不同的 libnccl 路径 → assert 失败（Duplicate NCCL runtime）

# 第二步：找出"DeepEP 链接的"libnccl.so 候选
linked_candidates = glob('{nccl_root}/lib/libnccl.so*') 排序后取第 0 个
    └─ 若没有候选 → assert 失败（No libnccl.so found）

# 第三步：逐字节比对两个文件
assert filecmp.cmp(loaded_nccl_so, linked_nccl_so, shallow=False)
    └─ shallow=False 表示比较文件内容而非仅 stat 元数据
```

关键点有三个：

1. **`EP_SUPPRESS_NCCL_CHECK=1` 是逃生口**。当用户确实知道自己在干嘛（比如故意混用、或在调试），可以关掉校验。
2. **`shallow=False`**。`filecmp.cmp` 默认只比文件的 stat（大小、mtime），这里强制逐字节比较——因为不同路径下的 `libnccl.so` 可能 stat 巧合相同但内容不同。
3. **"Duplicate NCCL runtime" 的独立判定**。即使每一份都是合法 NCCL，只要进程里同时映射了两份不同的 `libnccl.so`，就直接判死刑。

#### 4.2.3 源码精读

[deep_ep/__init__.py:46-68](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/__init__.py#L46-L68) —— `check_nccl_so` 全函数。

逐段看：

- **第 51-52 行**：读 `EP_SUPPRESS_NCCL_CHECK`。注意是 `int(...)`，所以 `"1"` 会被转成 `1`（真）。README 第 337 行确认了它的默认值是 `0`。
- **第 55-59 行**：打开 `/proc/self/maps`，逐行扫描含 `libnccl` 的行，取每行最后一个字段（即 `.so` 的绝对路径）。`loaded_nccl_so` 记录第一条，后续若出现不同路径则 assert。这正是"运行时实际加载的那份"。
- **第 60-62 行**：`glob(f'{find_nccl_root()}/lib/libnccl.so*')`——在 DeepEP 链接的 NCCL 根目录下找所有 `libnccl.so*`，排序取第一个。`find_nccl_root()` 的逻辑见 4.4 节。这里的"第一个"通常是 `libnccl.so`（无版本号软链）优先于 `libnccl.so.2`，因为字符串排序里 `libnccl.so` < `libnccl.so.2`。
- **第 66-68 行**：`filecmp.cmp(..., shallow=False)` 逐字节比较两个文件。错误信息里那句"please contact Chenggang or Shangyan to upgrade PyTorch NCCL version"暗示：在 DeepSeek 内部，最常见的失败场景就是 PyTorch 自带的 NCCL 落后于 DeepEP 需要的版本，需要推动 PyTorch 侧升级。

#### 4.2.4 代码实践

**实践目标**：观察 `EP_SUPPRESS_NCCL_CHECK` 开关的效果，并理解版本不一致的后果。这是一个**源码阅读 + 推理型实践**（不要求你真的制造一个会段错误的 NCCL 环境，那太危险）。

**操作步骤**：

1. **正常 import**（在 NCCL 一致的环境里），观察无输出、无异常：

   ```bash
   python -c "import deep_ep; print('import OK')"
   ```

2. **打开校验的"逃生口"** 重新 import，对比行为：

   ```bash
   EP_SUPPRESS_NCCL_CHECK=1 python -c "import deep_ep; print('import OK')"
   ```

   在一致环境里两者表现相同（都成功），因为校验本来就会通过。差异体现在**不一致**环境里：不开开关会 assert 失败并打印两个 `.so` 路径；开了开关则静默通过、把隐患留到运行时。

3. **阅读式追踪**：打开 `__init__.py:55-59`，对照 `/proc/self/maps` 的真实内容。在装了 PyTorch 的环境执行：

   ```bash
   python -c "import torch; print([l.strip().split(' ')[-1] for l in open('/proc/self/maps') if 'libnccl' in l])"
   ```

   你会看到 PyTorch 加载的那份 `libnccl.so` 的完整路径。再对照 DeepEP 的 `find_nccl_root()` 返回的路径，看二者是否指向**同一个 inode**。

**需要观察的现象**：

- 步骤 2 在一致环境里无差异（都成功）。
- 步骤 3 里 `torch` 加载的 NCCL 路径，应当与 `find_nccl_root()` 指向的 `libnccl.so` 是同一个文件（理想情况）。

**预期结果与推理**：现在回答实践任务的后半问——**如果 PyTorch 自带的 NCCL 与 `pip install nvidia-nccl-cu13` 装的 NCCL 版本不一致会发生什么？**

- 如果 PyTorch 加载的是较旧的 NCCL（比如 2.27），而 DeepEP 链接的是 2.30：进程里会同时存在两份 `libnccl` 符号。`check_nccl_so` 的第 55-59 行会检测到 `/proc/self/maps` 里（至少）一份，第 66 行 `filecmp.cmp` 会发现它与 DeepEP 链接的那份**内容不同**，于是 assert 失败，打印类似：
  ```
  AssertionError: Invalid NCCL versions: /path/to/libnccl.so.2.27 (loaded) v.s. /path/to/libnccl.so.2.30 (expected)
  ```
  整个 `import deep_ep` 中止。
- 如果用户用 `EP_SUPPRESS_NCCL_CHECK=1` 强行跳过，import 能过，但后续 dispatch/combine 在初始化 NCCL communicator、或访问 `ncclComm_t` 内部字段时，可能因为结构体布局不匹配而**得到错误结果或直接段错误**——而且这种错误几乎没有可读的报错信息。这正是该校验存在的意义。
- 修复方法：按 README 第 79 行 `pip install "nvidia-nccl-cu13>=2.30.4" --no-deps` 让两边对齐，或升级 PyTorch。

> 真实制造"双 NCCL"环境需要 root 改动系统库，本步骤不做。能否在不一致环境里复现 assert「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `check_nccl_so` 用 `filecmp.cmp(..., shallow=False)` 逐字节比较，而不是比较两个文件的 NCCL 版本号字符串？

**参考答案**：版本号字符串可能相同（都是 "2.30.4"）但二进制不同（比如一份是官方 release、另一份是带 patch 的自编译版，或 ABI 细节不同）。DeepEP 关心的是"运行时拿到的符号与链接时承诺的符号是否来自同一个二进制"，所以必须逐字节比对文件内容，而不是比对可能撒谎的版本字符串。

**练习 2**：`/proc/self/maps` 里如果**完全没有** `libnccl` 行（即 PyTorch 还没加载 NCCL），`loaded_nccl_so` 会是什么值？接下来会怎样？

**参考答案**：循环不进入，`loaded_nccl_so` 保持初始值 `None`。随后第 66 行 `filecmp.cmp(None, linked_nccl_so, ...)` 会抛 `TypeError`（因为 `None` 不是合法路径）。但实际场景里，`import deep_ep` 之前通常会先 `import torch` 并初始化分布式，所以 NCCL 一般已被加载；这个边界情况在纯 import（不初始化 distributed）时可能触发——这也是为什么很多用户报告"必须先 `import torch; torch.distributed.init_process_group` 才能 import deep_ep"的现象之一。

---

### 4.3 init_jit：把工具链路径注入 JIT 编译器

#### 4.3.1 概念说明

NCCL 校验通过后，下一步是 `init_jit()`。它的任务很纯粹：**告诉 C++ 侧的 JIT 编译器三件事**——

1. **库根目录**（`library_root_path`）：DeepEP 包安装在哪里。JIT 编译时需要 `#include <deep_ep/...>` 头文件，这些头文件就在 `library_root_path/include/` 下（见 u1-l2 提到的 header-only 模板）。
2. **CUDA home**（`find_cuda_home()`）：nvcc、cuobjdump 等工具链在哪里。JIT 要调用 nvcc 编译 `.cu`，要调用 cuobjdump 从 `.cubin` 里提取内核符号。
3. **NCCL root**（`find_nccl_root()`）：NCCL 头文件在哪里，用于编译时 `-I {nccl_root}/include`。

为什么要在 import 阶段就传过去？因为 JIT 编译发生在**运行时**（你第一次 `dispatch` 时），而 Python 侧的环境探测（找 CUDA、找 NCCL 包）在 C++ 里做很麻烦——Python 有现成的 `importlib.metadata`、`os.environ`、`subprocess`。所以分工是：**Python 负责探测路径，C++ 负责使用路径编译**。`init_jit` 就是这两者之间的桥。

#### 4.3.2 核心流程

`init_jit` 的调用链横跨 Python 与 C++：

```text
[Python] deep_ep/__init__.py: init_jit()
    │
    │  import deep_ep._C as _C
    │  library_root_path = dirname(__file__)        # 即 deep_ep/ 包目录
    │
    ▼
[pybind11] _C.init_jit(library_root_path, find_cuda_home(), find_nccl_root())
    │
    ▼
[C++]   csrc/jit/api.hpp: init(library_root, cuda_home, nccl_root)
    │
    ├─ Compiler::prepare_init(library_root, cuda_home, nccl_root)
    │      └─ 存储 5 个静态路径：library_root_path / library_include_path /
    │                        cuda_home / nccl_root / cuobjdump_path
    │
    ├─ KernelRuntime::prepare_init(cuda_home)
    │      └─ 存储静态路径 cuda_home（加载 cubin 时找 cuobjdump）
    │
    └─ IncludeParser::prepare_init(library_root)
           └─ 存储静态路径 library_include_path（解析 <deep_ep/...> 头文件哈希）
```

注意 `init` 函数本身**不做任何编译**，它只是把三组路径"钉"进三个类的静态成员变量里，留待后续真正 `build` 内核时取用。这是一种典型的 **prepare/init 两段式**设计：先喂配置，再按需懒加载。

#### 4.3.3 源码精读

**Python 侧**——

[deep_ep/__init__.py:71-80](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/__init__.py#L71-L80) —— `init_jit`。`library_root_path = os.path.dirname(os.path.abspath(__file__))`，即 `__init__.py` 所在目录，也就是 `deep_ep/` 包根。这个路径之所以重要，是因为头文件在 `deep_ep/include/deep_ep/...` 下——JIT 生成的 `.cu` 会写 `#include <deep_ep/impls/dispatch.cuh>`，编译时 nvcc 需要 `-I{library_root_path}/include` 才能找到它。

**C++ 侧桥接**——

[csrc/jit/api.hpp:9-14](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/jit/api.hpp#L9-L14) —— C++ `init` 函数，一行调用一个 `prepare_init`，分别喂给 Compiler、KernelRuntime、IncludeParser 三个类。注释里写明每个参数的用途。

[csrc/jit/api.hpp:16-18](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/jit/api.hpp#L16-L18) —— `register_apis` 把这个 `init` 函数注册为 Python 可调用的 `m.def("init_jit", &init)`。

[csrc/python_api.cpp:31-32](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/python_api.cpp#L31-L32) —— 在 pybind11 模块 `_C` 的注册入口里，`deep_ep::jit::register_apis(m)` 把 `init_jit` 暴露为 `deep_ep._C.init_jit`。

**三个 `prepare_init` 各自存了什么**——

[csrc/jit/compiler.hpp:30-39](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/jit/compiler.hpp#L30-L39) —— `Compiler::prepare_init`。注意它从三个入参**派生**出五个静态变量：`library_root_path` → `library_include_path`（拼上 `/include`）；`cuda_home` → `cuobjdump_path`（拼上 `/bin/cuobjdump`）。这些派生值在后续 `Compiler` 构造函数（[compiler.hpp:44-73](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/jit/compiler.hpp#L44-L73)）里被用来组装 nvcc 命令行，例如第 67 行 `flags += fmt::format(" -I {}/include", nccl_root.c_str())` 把 NCCL 头文件目录加进编译标志。

[csrc/jit/kernel_runtime.hpp:61-63](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/jit/kernel_runtime.hpp#L61-L63) —— `KernelRuntime::prepare_init` 只存 `cuda_home`，因为加载 `.cubin` 时需要 `cuda_home/bin/cuobjdump` 来枚举内核符号（见 [kernel_runtime.hpp:24-32](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/jit/kernel_runtime.hpp#L24-L32)）。

[csrc/jit/include_parser.hpp:43-45](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/jit/include_parser.hpp#L43-L45) —— `IncludeParser::prepare_init` 存 `library_include_path`。IncludeParser 的职责是：解析生成的 `.cu` 里 `#include <deep_ep/...>` 的头文件，递归计算它们的哈希，作为编译缓存签名的一部分（见 [include_parser.hpp:47-54](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/jit/include_parser.hpp#L47-L54)）。**这就是为什么改了 `deep_ep/include/` 下的头文件会让 JIT 缓存失效**——哈希变了，缓存目录名也变了。

#### 4.3.4 代码实践

**实践目标**：验证 `init_jit` 确实把三条路径"钉"进了 C++ 静态变量，并理解这三条路径在后续 JIT 编译里的具体用途。这是一个**阅读 + 跟踪型实践**。

**操作步骤**：

1. **打印 Python 侧探测到的三条路径**：

   ```python
   import deep_ep
   from deep_ep import find_cuda_home
   from deep_ep.utils.find_pkgs import find_nccl_root
   import os
   print('library_root =', os.path.dirname(os.path.abspath(deep_ep.__file__)))
   print('cuda_home    =', find_cuda_home())
   print('nccl_root    =', find_nccl_root())
   ```

2. **验证库根目录下确实有 JIT 要用的头文件**：

   ```bash
   ls $(python -c "import deep_ep,os;print(os.path.dirname(os.path.abspath(deep_ep.__file__)))")/include/deep_ep/impls/ | head
   ```

   应能看到 `dispatch.cuh`、`combine.cuh` 等 header-only 内核源。

3. **跟踪式阅读**：对照 [csrc/jit/compiler.hpp:30-39](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/jit/compiler.hpp#L30-L39) 与 [compiler.hpp:56-73](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/jit/compiler.hpp#L56-L73)，画出"三条 Python 路径 → Compiler 的五个静态变量 → nvcc 命令行 flag"的映射表。重点关注：
   - `library_include_path` 怎么进了 `-I{} --gpu-architecture=...`（[compiler.hpp:216-219](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/jit/compiler.hpp#L216-L219)）。
   - `nccl_root` 怎么进了 `-I {nccl_root}/include`（[compiler.hpp:67](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/jit/compiler.hpp#L67)）。
   - `cuobjdump_path`（来自 `cuda_home`）怎么被 `disassemble` 和 `KernelRuntime` 用来提取符号。

**需要观察的现象**：

- 步骤 1 打印的三条路径都非空，且 `cuda_home/bin/nvcc` 与 `cuda_home/bin/cuobjdump` 文件存在。
- 步骤 2 能列出 `dispatch.cuh` 等头文件——证明 `library_root_path/include/` 就是 JIT 编译时 `-I` 指向的目录。

**预期结果**：三条路径与 C++ 侧 `prepare_init` 存储的静态变量一一对应，任一条缺失都会让后续 JIT 编译在构造 `Compiler` 对象时 assert 失败（见 [compiler.hpp:45-49](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/jit/compiler.hpp#L45-L49) 的五个 `EP_HOST_ASSERT(not *.empty())`）。本实践在已正确安装的环境里应当全部通过；具体路径值「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：`init` 函数为什么不直接在 C++ 里探测 CUDA/NCCL 路径，而要由 Python 探测后传进来？

**参考答案**：两点原因。其一，Python 生态有成熟的包元数据查询（`importlib.metadata.distributions()`，见 4.4 节的 `find_pkg_root`），能优雅地定位 `pip` 装的 `nvidia-nccl-cu13`；C++ 里做这件事要重造轮子。其二，Python 侧的探测逻辑可以方便地用环境变量（`EP_NCCL_ROOT_DIR`、`CUDA_HOME`）覆盖，便于在异构集群里调试。把"易变的探测"留在 Python、"性能敏感的编译"留在 C++，是合理的关注点分离。

**练习 2**：如果你修改了 `deep_ep/include/deep_ep/impls/dispatch.cuh` 里的一行注释，会不会影响 JIT 缓存命中？为什么？

**参考答案**：**会**，缓存会失效。因为 [include_parser.hpp:47-73](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/jit/include_parser.hpp#L47-L73) 的 `get_hash_value_by_path` 会读取头文件的**全部内容**（包括注释）计算哈希，再拼进编译签名。改注释 → 哈希变 → 缓存目录名变 → 重新编译。这也是为什么 `init_jit` 必须正确传入 `library_root_path`：IncludeParser 依赖它定位头文件算哈希。

---

### 4.4 find_cuda_home / find_nccl_root：为 JIT 定位工具链

#### 4.4.1 概念说明

`init_jit` 的后两个参数（`find_cuda_home()` 与 `find_nccl_root()`）是两条路径探测函数。它们解决的是同一个问题：**在一台机器上，CUDA 和 NCCL 到底装在哪？** 但探测策略截然不同：

- **`find_cuda_home`**：用经典的环境变量 + `which nvcc` 兜底策略，定位 CUDA 工具链。逻辑简单，结果带缓存。
- **`find_nccl_root`**：优先看环境变量，否则**扫描 Python 包元数据**找到 `pip` 装的 NVIDIA 包根目录。这是 DeepEP 为了适配 `pip install nvidia-nccl-cu13` 这种"NCCL 也成了 pip 包"的新范式而专门写的。

为什么 NCCL 这么麻烦？因为传统上 NCCL 装在 `/usr/local/nccl` 这种系统路径，但现在 NVIDIA 把 NCCL 也发布成了 pip wheel（`nvidia-nccl-cu13`），它会被装进 Python 的 `site-packages/nvidia/nccl/` 里，没有固定路径。DeepEP 必须能在不同安装方式下都找到它。

#### 4.4.2 核心流程

**`find_cuda_home`** 的探测优先级（[__init__.py:22-43](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/__init__.py#L22-L43)）：

```text
1. 环境变量 CUDA_HOME
2. 环境变量 CUDA_PATH
3. `which nvcc` 反推（取 nvcc 路径的上两级目录）
4. 兜底 /usr/local/cuda（若存在）
5. 都没有 → cuda_home = None → 后续 assert 失败
```

注释里特别说明：**没有复用 PyTorch 的 `_find_cuda_home`**，因为某些 PyTorch 版本的实现会初始化 CUDA，而初始化 CUDA 与进程 fork 不兼容（多进程分布式训练里 `torch.multiprocessing.spawn` 会 fork）。所以 DeepEP 自己实现了一个"纯文件系统探测、不碰 CUDA 运行时"的版本。

**`find_nccl_root`** 的探测优先级（[find_pkgs.py:8-54](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/utils/find_pkgs.py#L8-L54)）：

```text
1. 环境变量 EP_NCCL_ROOT_DIR
2. 环境变量 NCCL_DIR
3. 扫描所有 Python distribution，找名为 nvidia-nccl / nvidia_nccl 的包
   ├─ 若指定 lib_name='libnccl.so'：在包文件里找含该名的文件，
   │   取其 locate() 路径，再向上回退到根（若末段是 lib 则再上一层）
   └─ 在所有候选里，按"出现在 sys.path 更靠前位置"优先
4. 都没有 → assert 失败（除非 optional=True）
```

第 3 步的"按 sys.path 优先级排序"很关键：如果系统里有多个 Python 环境（virtualenv、conda、system），`find_nccl_root` 会优先选离当前解释器最近的那个 NCCL，避免误取到别的环境的库。

#### 4.4.3 源码精读

[deep_ep/__init__.py:21-43](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/__init__.py#L21-L43) —— `find_cuda_home`。注意 `@functools.lru_cache()` 装饰，意味着同一进程内只探测一次。第 30 行注释解释了为何不复用 PyTorch 的实现。

[deep_ep/utils/find_pkgs.py:8-54](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/utils/find_pkgs.py#L8-L54) —— `find_pkg_root`，`find_nccl_root` 的通用化版本。

- **第 21-24 行**：先查 `EP_{NAME}_ROOT_DIR` 与 `{NAME}_DIR`。对 NCCL 来说就是 `EP_NCCL_ROOT_DIR` 和 `NCCL_DIR`。这是给"非 pip 安装"用户的逃生口——你可以在系统路径装 NCCL，然后用环境变量指过去。
- **第 26-27 行**：建立 `sys.path` 索引，用于后续按"路径优先级"排序候选。
- **第 29-31 行**：`dist.metadata['Name']` 拿到包名，匹配 `nvidia-nccl` 或 `nvidia_nccl`（横杠/下划线都要试，因为不同工具链规范化方式不同）。
- **第 39-45 行**：当传了 `lib_name='libnccl.so'` 时，遍历包的所有文件（`dist.files`），找到含 `libnccl.so` 的文件，用 `locate()` 拿到它在**磁盘上的绝对路径**，然后向上回退：若当前目录名是 `lib`，则再上一层（返回包根），否则直接返回 lib 目录。这样得到的 root 拼上 `/lib/libnccl.so*` 就能 glob 到库文件——这正是 `check_nccl_so` 第 60 行的用法。

[deep_ep/utils/find_pkgs.py:57-68](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/utils/find_pkgs.py#L57-L68) —— `find_nccl_root`，只是 `find_pkg_root('nccl', lib_name='libnccl.so', optional=optional)` 的一层封装，同样带 `lru_cache`。

#### 4.4.4 代码实践

**实践目标**：亲手调用两条探测函数，理解它们各自如何"看"到 CUDA 与 NCCL，并验证环境变量能覆盖自动探测结果。

**操作步骤**：

```bash
# 1. 默认探测
python -c "from deep_ep import find_cuda_home; from deep_ep.utils.find_pkgs import find_nccl_root; print('cuda:', find_cuda_home()); print('nccl:', find_nccl_root())"

# 2. 用环境变量强制覆盖 NCCL 探测
EP_NCCL_ROOT_DIR=/tmp/fake_nccl python -c "from deep_ep.utils.find_pkgs import find_nccl_root; print('nccl:', find_nccl_root())"

# 3. 用环境变量强制覆盖 CUDA 探测
CUDA_HOME=/tmp/fake_cuda python -c "from deep_ep import find_cuda_home; print('cuda:', find_cuda_home())"
```

**需要观察的现象**：

- 步骤 1：两条路径都能正确探测到真实安装位置。
- 步骤 2：直接返回 `/tmp/fake_nccl`——证明环境变量优先级最高（`find_pkg_root` 第 22-24 行直接 return，不走包扫描）。
- 步骤 3：直接返回 `/tmp/fake_cuda`——证明 `find_cuda_home` 第 31 行的环境变量优先级。

**预期结果**：环境变量覆盖生效。注意步骤 2/3 里 `find_nccl_root`/`find_cuda_home` 都带 `lru_cache`，所以**同一进程内多次调用结果一致**；要做覆盖实验必须在启动进程前就设好环境变量（命令行前缀方式），进程内 `os.environ[...] = ...` 后再调用不会重新探测（因为可能已被缓存）。具体路径值「待本地验证」。

> ⚠️ 步骤 2/3 里 `find_nccl_root` 会成功返回假路径（它不校验路径是否存在），但如果后续真去 `init_jit`，C++ 侧 `prepare_init` 只存路径、不校验；真正报错会推迟到 `check_nccl_so` 的 glob 找不到 `libnccl.so*` 时（[__init__.py:60-61](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/__init__.py#L60-L61)）。这印证了"探测在 Python、使用在 C++"的分工。

#### 4.4.5 小练习与答案

**练习 1**：`find_pkg_root` 第 41-44 行：找到 `libnccl.so` 后，为什么"若当前目录名是 `lib` 则再上一层"？

**参考答案**：因为返回的 `root` 在别处的使用约定是"拼 `/lib/...`"。比如 `check_nccl_so` 第 60 行写的是 `f'{find_nccl_root()}/lib/libnccl.so*'`——它期望 `find_nccl_root()` 返回的是**包根**（`/site-packages/nvidia/nccl`），而不是 `lib` 目录本身（`/site-packages/nvidia/nccl/lib`）。所以探测到 `lib` 目录后要再上一层，保证调用方拼 `/lib/` 时路径正确。

**练习 2**：为什么 `find_cuda_home` 与 `find_nccl_root` 都用 `@functools.lru_cache()`？

**参考答案**：两点。其一，路径探测涉及 `subprocess`（`which nvcc`）和遍历包元数据（`distributions()`），有开销，没必要每次调用都重做。其二，更重要的是**一致性**：同一进程内如果两次探测得到了不同结果（比如环境变了），会让 `check_nccl_so` 用一份 NCCL、`init_jit` 注入另一份，反而制造出"双 NCCL"的假象。缓存保证整条 import 链看到的是同一组路径。

---

## 5. 综合实践

把本讲三个模块串起来，做一个端到端的"import 链路追踪"任务。

**任务背景**：你的同事在新集群上 `import deep_ep` 报错，错误信息是 `AssertionError: Invalid NCCL versions: /opt/conda/lib/libnccl.so.2 (loaded) v.s. /site-packages/nvidia/nccl/lib/libnccl.so.2 (expected)`。请你用本讲学到的知识，复现并解释这条链路，给出排查建议。

**操作步骤**：

1. **画出 import 执行链**（在纸上或文档里）：标注 `persistent_envs 加载 → check_nccl_so → init_jit → API 导出` 四步，每一步引用对应的 `__init__.py` 行号，并写明每步"失败会发生什么"。

2. **手动模拟探测**：写一个脚本，不 import 整个 deep_ep，而是单独调用三个工具函数，打印你的环境下的探测结果：

   ```python
   import os
   # 故意模拟"PyTorch 加载了一份、DeepEP 链接了另一份"
   import torch  # 这会 dlopen 一份 libnccl
   loaded = [l.strip().split(' ')[-1] for l in open('/proc/self/maps') if 'libnccl' in l]
   print('PyTorch loaded NCCL:', loaded)

   from deep_ep.utils.find_pkgs import find_nccl_root
   from deep_ep import find_cuda_home
   print('DeepEP linked NCCL root:', find_nccl_root())
   print('DeepEP cuda home:', find_cuda_home())
   ```

3. **对照 `check_nccl_so` 解释报错**：根据步骤 2 的输出，对照 [__init__.py:55-68](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/__init__.py#L55-L68)，指出：
   - 报错发生在哪一行（第 66 行的 `filecmp.cmp` assert）。
   - `loaded` 路径来自 `/proc/self/maps`（第 55-58 行），`expected` 路径来自 `find_nccl_root()/lib/libnccl.so*` glob（第 60-62 行）。
   - 为什么这两个文件 `filecmp.cmp(..., shallow=False)` 不相等——它们是不同版本/不同构建的 NCCL 二进制。

4. **给出三条修复建议**（结合本讲源码）：
   - **对齐版本**：按 README 用 `pip install "nvidia-nccl-cu13>=2.30.4" --no-deps` 把 NCCL 升到 DeepEP 期望的版本，让 PyTorch 也加载这一份。
   - **强制覆盖路径**：用 `EP_NCCL_ROOT_DIR` 指向 DeepEP 链接的那份 NCCL，使 `find_nccl_root` 返回值与 PyTorch 加载的一致（前提是它们其实是同一份）。
   - **临时绕过**（不推荐）：`export EP_SUPPRESS_NCCL_CHECK=1` 跳过校验，但承担运行时符号错乱的风险。

**预期结果**：你能用本讲的源码知识，把同事的报错从"玄学 assert"解释成"`/proc/self/maps` 里的 NCCL 与 `find_nccl_root()` 指向的 NCCL 不是同一个二进制"，并给出可操作的修复路径。这构成了对 4.2（check_nccl_so）、4.3（init_jit 的路径注入）、4.4（find_nccl_root 探测）三个模块的综合检验。

## 6. 本讲小结

- `import deep_ep` 在 `__init__.py` 里同步执行四步：加载持久化环境变量 → `check_nccl_so()` → `init_jit()` → 暴露公开 API；顺序固定、NCCL 校验失败会 fail fast。
- 持久化环境变量（`EP_JIT_CACHE_DIR` 等）在构建期由 `setup.py` 烘焙进 `deep_ep/envs.py`，import 时作"兜底默认"，用户运行时 `export` 的值永远优先（`__init__.py:15` 的 `if key not in os.environ` 守卫）。
- `check_nccl_so()` 通过 `/proc/self/maps` 拿"运行时加载的 NCCL"，用 `find_nccl_root()/lib/libnccl.so*` 拿"DeepEP 链接的 NCCL"，再用 `filecmp.cmp(..., shallow=False)` **逐字节**比对，防止同一进程出现两份不同 NCCL 符号；`EP_SUPPRESS_NCCL_CHECK=1` 可跳过。
- `init_jit()` 把三条 Python 探测到的路径（库根 / CUDA home / NCCL root）通过 pybind11 的 `_C.init_jit` 注入 C++ 侧，分别喂给 `Compiler::prepare_init`、`KernelRuntime::prepare_init`、`IncludeParser::prepare_init`，为运行时 JIT 编译做准备。
- `find_cuda_home` 走"环境变量 → `which nvcc` → 兜底"三段式，刻意不复用 PyTorch 的实现以避免触发 CUDA 初始化（与 fork 冲突）；`find_nccl_root` 走"环境变量 → 扫描 pip 包元数据"两段式，适配 `nvidia-nccl-cu13` 这种 NCCL-as-pip-package 的新范式。
- 三条路径一旦注入错误（如 `EP_NCCL_ROOT_DIR` 指向假路径），探测阶段不会立刻报错（Python 只存路径），报错会推迟到 `check_nccl_so` 的 glob 或后续 JIT 编译——这是"Python 探测、C++ 使用"分工的副作用。

## 7. 下一步学习建议

本讲只讲了 import 阶段的"准备工作"，还没有真正编译任何内核。建议按以下顺序继续：

1. **u2-l2（创建 ElasticBuffer）**：看 import 完成后，第一个用户操作 `ElasticBuffer(...)` 如何利用已初始化的 JIT 环境分配对称内存、计算缓冲区大小。
2. **u3-l4（NCCL Gin 后端与对称内存上下文）**：深入 `check_nccl_so` 想保护的那份 NCCL communicator，是如何被复用来建立对称内存窗口的。
3. **u4-l1（JIT 系统总览）**：本讲只讲了 `init_jit` 把路径"钉"进 Compiler 静态变量；u4 单元会讲这些变量在真正 `build` 一个内核时如何被组装成 nvcc 命令行、如何命中/未命中缓存。
4. **u8-l3（环境变量体系）**：本讲涉及了 `EP_SUPPRESS_NCCL_CHECK`、`EP_NCCL_ROOT_DIR`、`CUDA_HOME` 等若干变量；u8-l3 会系统梳理全部四大类（运行时 / 网络 / JIT / 构建）环境变量。

阅读源码时，推荐带着这个问题进入 u4：**`init_jit` 注入的 `library_include_path`，是怎么最终变成 nvcc 命令行里那个 `-I` 的？** 答案就在 [compiler.hpp:216-219](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/jit/compiler.hpp#L216-L219) 的 `NVCCCompiler` 构造函数里——它会在 u4-l1 详细展开。
