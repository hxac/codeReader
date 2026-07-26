# 目录结构与包入口

## 1. 本讲目标

学完本讲后，你应该能够：

- 画出 `tile_kernels` 包的目录树，并标注每个子模块（算子层、torch 参考层、modeling 层、testing 层以及 `config`/`utils` 基础设施）的职责。
- 理解 `tile_kernels/__init__.py` 如何通过「导入子模块 + 选择性再导出」来聚合整个包的对外接口。
- 跟踪一条真实调用路径，从包入口一路走到某个算子的 wrapper 函数（以 `tile_kernels.transpose.transpose` 为例）。
- 掌握 `config`（SM 数量与共享内存探测）与 `utils`（整除、对齐、2 的幂判断）这两个基础设施的用途。

本讲不深入任何一个算子的内部实现，只解决「项目长什么样、入口在哪里、东西放在哪」的问题，为后续逐模块深入打下导航基础。

## 2. 前置知识

阅读本讲前，建议你已完成 **u1-l1（项目定位与价值）**，知道 TileKernels 是一个用 TileLang DSL 写的高性能 GPU 算子库，并理解以下概念：

- **Python 包（package）**：一个含有 `__init__.py` 的目录，导入它时会先执行 `__init__.py`。
- **子模块（submodule）**：包目录下的 `.py` 文件或子目录。
- **GPU 上的 SM（Streaming Multiprocessor，流式多处理器）**：GPU 的核心调度单元，一块 GPU由若干个 SM 组成。算子能同时跑在多少个 SM 上，直接影响并行度。
- **共享内存（shared memory，smem）**：每个 SM 内部的高速小容量存储，kernel 通过它做线程间数据交换。后面 `config` 会探测每个 SM 能用多少 smem。
- **TileLang DSL**：用 Python 语法描述 GPU kernel 的一种领域专用语言，由 `tilelang` 库提供。本讲只把它当作「写 kernel 的语言」，不展开语法（那是 u2 单元的事）。

如果你对「调用路径」「再导出」「模块导入」等 Python 机制不熟也不用担心，本讲会结合真实代码逐步讲解。

## 3. 本讲源码地图

| 文件 / 目录 | 作用 |
| --- | --- |
| [`README.md`](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/README.md) | 项目说明，其中有一段官方目录结构示意，是理解包布局的起点。 |
| [`tile_kernels/__init__.py`](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/__init__.py) | 包入口，导入所有子模块并选择性再导出部分基础函数。 |
| [`tile_kernels/config.py`](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/config.py) | 硬件探测基础设施：SM 数量、每 SM 最大共享内存。 |
| [`tile_kernels/utils.py`](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/utils.py) | 通用小工具：整除向上取整、对齐、判断 2 的幂。 |
| [`tile_kernels/transpose/__init__.py`](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/transpose/__init__.py) | 转置算子子包入口，演示「子包如何再导出 wrapper」。 |
| [`tile_kernels/transpose/batched_transpose_kernel.py`](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/transpose/batched_transpose_kernel.py) | 转置算子：TileLang kernel + Python wrapper（`transpose`/`batched_transpose`），是本讲要追踪的目标函数所在。 |

## 4. 核心概念与源码讲解

### 4.1 包的四层结构概览

#### 4.1.1 概念说明

一个算子库要同时回答好几个问题：算子怎么跑得快？怎么验证算得对？怎么让上层模型（带自动求导）方便地调用？TileKernels 把这些职责分成不同目录，互不混杂。这种「分目录即分职责」的设计，就是本讲要建立的包结构心智模型。

#### 4.1.2 核心流程

README 的「Project Structure」段给出了官方的目录划分：

```txt
tile_kernels/
├── moe/        # Mixture of Experts routing related kernels
├── quant/      # FP8/FP4/E5M6 quantization
├── transpose/  # Batched transpose
├── engram/     # Engram gating kernels
├── mhc/        # Manifold HyperConnection kernels
├── modeling/   # High-level autograd modeling layers (engram, mHC)
├── torch/      # PyTorch reference implementations
└── testing/    # Test and benchmark utilities
```

可以把它们归并为「四层 + 两件基础设施」：

| 层 | 目录 | 职责 | 谁依赖谁 |
| --- | --- | --- | --- |
| 算子层（底层 kernel） | `transpose/`、`moe/`、`quant/`、`engram/`、`mhc/` | 用 TileLang DSL 写的高性能 GPU kernel，外加一个 Python wrapper 启动它 | 是地基，被其他层调用 |
| torch 参考层 | `torch/` | 用纯 PyTorch 写的「参考实现」，用来和 kernel 输出对拍验证正确性 | 不参与生产，只供测试对照 |
| modeling 层 | `modeling/` | 用 `torch.autograd.Function` 把底层 kernel 封装成可自动求导的 PyTorch 层 | 依赖算子层 |
| testing 层 | `testing/` | 测试与基准工具（参数生成、数值断言、带宽计时） | 被测试代码依赖 |
| 基础设施 | `config.py`、`utils.py` | 硬件探测、通用小工具 | 被各算子按需调用 |

理解这条依赖链很关键：**算子层在最底，modeling 在算子之上，torch 参考与 testing 横向服务于验证**。后续每个单元深入某一层时，你都能在这个表里定位它的位置。

> 注意：`transpose/`、`moe/`、`quant/`、`engram/`、`mhc/` 这 5 个目录虽然各自是一个算子家族，但它们的内部组织方式高度一致——都是「TileLang kernel + Python wrapper + 子包 `__init__` 再导出」。掌握了 `transpose` 的结构，看其他家族就能举一反三。

#### 4.1.3 源码精读

README 的目录结构直接来自源码，是可信的地图：

- [README.md:58-68](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/README.md#L58-L68)：官方目录结构说明，每个目录一行注释，是理解包布局的第一手资料。

我们也可以用只读 git 命令直接核对目录确实存在（无需手动 `ls`）：

```bash
git ls-files tile_kernels/ | head
```

它会列出包内所有受版本控制的文件，验证 README 描述的目录都是真实存在的。

#### 4.1.4 代码实践

**实践目标**：亲手画出 `tile_kernels` 的目录树并标注职责，而不是只看 README。

**操作步骤**：

1. 在仓库根目录执行 `git ls-files tile_kernels/`，观察输出。
2. 用 `find tile_kernels -maxdepth 2 -type d`（或等价只读命令）列出所有子目录。
3. 把结果整理成一棵树，每个叶子目录旁写一句话职责（可参考上表）。

**需要观察的现象**：你会看到 5 个算子家族目录（`moe/quant/transpose/engram/mhc`）以及 `modeling/`、`torch/`、`testing/`，并且 `modeling/` 下面还有 `engram/` 与 `mhc/` 两个子目录（说明 modeling 层目前封装了这两个家族）。

**预期结果**：得到一张与 README 一致、但由你自己核对过的包结构图。

#### 4.1.5 小练习与答案

**练习 1**：`modeling/` 目录下有 `engram/` 和 `mhc/` 两个子目录，但没有 `transpose/` 或 `quant/`。这说明什么？

**参考答案**：说明目前 modeling 层（可自动求导的高层封装）只为 `engram` 和 `mhc` 这两个家族做了 `autograd` 封装；`transpose`、`quant` 等家族目前以底层算子 + torch 参考的形式存在，暂未提供 modeling 层封装。

**练习 2**：`torch/` 参考层和算子层是什么关系？它会参与生产推理吗？

**参考答案**：`torch/` 是纯 PyTorch 参考实现，与算子层实现的是**同一个数学运算**，用于在测试中与 kernel 输出对拍验证正确性。它通常不参与生产推理（生产用的是算子层的高性能 kernel）。

---

### 4.2 `__init__.py` 的聚合导出机制

#### 4.2.1 概念说明

一个 Python 包被 `import` 时，解释器会先执行它的 `__init__.py`。TileKernels 把 `tile_kernels/__init__.py` 当成「总目录」：它不写任何算子逻辑，只负责**导入所有子模块**，让用户可以用 `tile_kernels.transpose.transpose`、`tile_kernels.moe.topk_gate` 这样的点号路径访问每个算子。同时它把少量最常用的基础函数（如 `get_num_sms`）「提升」到顶层，方便直接 `from tile_kernels import get_num_sms`。

这种「子模块各自组织、顶层只做聚合」的设计，是阅读大型 Python 库时最常见的入口模式。

#### 4.2.2 核心流程

包入口做两件事：

1. **导入子模块**：用 `from . import (...)` 把所有子包/基础设施拉进来。这样 `tile_kernels` 对象上就挂载了 `transpose`、`moe`、`config` 等属性。
2. **选择性再导出**：把 `config` 里的三个函数提到顶层命名空间。

每个**算子子包内部**也有一个 `__init__.py`，做的是「子包级聚合」：把该家族对外想暴露的 wrapper 函数从具体 `.py` 文件里再导出。于是用户访问路径就是：

```
tile_kernels.transpose.transpose
└── 包入口(__init__) 导入子包 transpose
    └── 子包 __init__ 再导出 transpose 函数
        └── 来自 batched_transpose_kernel.py
```

#### 4.2.3 源码精读

包入口的全部内容只有 15 行，但信息量很大：

- [tile_kernels/__init__.py:3-13](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/__init__.py#L3-L13)：一次性导入 9 个子模块/文件——`config, engram, mhc, modeling, moe, quant, transpose, torch, testing`。注意这里既导入了算子家族，也导入了 `config`（基础设施）和 `torch`/`modeling`/`testing`。从这一行就能看出包的四层结构。
- [tile_kernels/__init__.py:15](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/__init__.py#L15)：`from .config import get_num_sms, get_device_num_sms, set_num_sms`——把硬件探测相关的 3 个函数提升到 `tile_kernels` 顶层，方便直接导入。

再看子包级的聚合，以 transpose 为例：

- [tile_kernels/transpose/__init__.py:1](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/transpose/__init__.py#L1)：`from .batched_transpose_kernel import transpose, batched_transpose`——把 wrapper 函数从 `batched_transpose_kernel.py` 再导出。这正是 `tile_kernels.transpose.transpose` 能解析到目标函数的原因。

其他算子家族的子包 `__init__` 遵循同样模式，例如 `moe` 一次性再导出了 11 个 wrapper：

- [tile_kernels/moe/__init__.py:1-11](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/moe/__init__.py#L1-L11)：把 `topk_gate`、`expand_to_fused`、`group_count` 等所有 MoE 相关 wrapper 都从各自 `*_kernel.py` 再导出。

> 一个重要观察：**算子层对外暴露的是 wrapper 函数，而不是 TileLang kernel 对象本身**。wrapper 负责「分配输出张量 + 启动 kernel」，是用户真正该调用的入口。这条规律适用于所有家族。

#### 4.2.4 代码实践

**实践目标**：完整跟踪 `tile_kernels.transpose.transpose` 这一条调用路径，体会「包入口 → 子包 `__init__` → wrapper」的三级解析。

**操作步骤**：

1. 打开 [tile_kernels/__init__.py:10](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/__init__.py#L10)：确认包入口导入了 `transpose` 子包。
2. 打开 [tile_kernels/transpose/__init__.py:1](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/transpose/__init__.py#L1)：确认子包把 `transpose` 这个名字再导出。
3. 打开 [tile_kernels/transpose/batched_transpose_kernel.py:79](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/transpose/batched_transpose_kernel.py#L79)：这就是 `tile_kernels.transpose.transpose` 最终指向的 wrapper 函数。
4. 阅读该 wrapper 内部：它在 [第 88-90 行](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/transpose/batched_transpose_kernel.py#L88-L90) 通过 `unsqueeze(0)` → 调 `batched_transpose` → `squeeze(0)`，把二维转置复用成三维批量转置。

**需要观察的现象**：三级路径 `tile_kernels.transpose.transpose` 中，第一个 `transpose` 是子包，第二个 `transpose` 是 wrapper 函数，两者同名但完全不同（一个是目录，一个是函数）。

**预期结果**：你能在纸上画出这条解析链：`tile_kernels`（包）→ `.transpose`（子包，来自包入口导入）→ `.transpose`（函数，来自子包 `__init__` 再导出）。

> 提示：本实践是「源码阅读型实践」，不需要 GPU。如果你想确认这个解析逻辑，可以在装有 `tile_kernels` 的环境里执行 `python -c "import tile_kernels; print(tile_kernels.transpose.transpose)"`，预期打印出函数对象（待本地验证）。

#### 4.2.5 小练习与答案

**练习 1**：包入口的 `from .config import get_num_sms, ...` 为什么不写成 `from . import *`？

**参考答案**：显式导入更安全、更清晰。`import *` 会把 `config` 模块的全部公开名字都拉进顶层，可能造成命名污染且难以追踪来源；显式列出 3 个函数则精确控制了顶层命名空间，读代码的人一眼就知道顶层对外暴露了什么。

**练习 2**：如果用户写 `tile_kernels.transpose.batched_transpose`，它指向哪里？

**参考答案**：同样由 [tile_kernels/transpose/__init__.py:1](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/transpose/__init__.py#L1) 再导出，指向 [batched_transpose_kernel.py:94](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/transpose/batched_transpose_kernel.py#L94) 定义的 `batched_transpose` 函数——即三维批量转置的 wrapper（`transpose` 的底层实现）。

---

### 4.3 `config`：硬件探测与可配置覆盖

#### 4.3.1 概念说明

高性能 GPU kernel 在启动前，常常需要知道两件硬件信息：**这块卡有多少个 SM**（决定能铺多少并行块），以及**每个 SM 有多少共享内存**（决定 kernel 能开多大 tile）。`config.py` 就是封装这两件事的基础设施。

它还提供一个「可配置覆盖」能力：允许人为把可用 SM 数限制成更小的值，用于实验「算子在更少并行度下的表现」。这在调优和 benchmark 时很有用（u9/u10 单元会用到）。

#### 4.3.2 核心流程

`config.py` 的逻辑可以概括为：

```
真实硬件 SM 数  =  get_device_num_sms()      # 探测一次，缓存
可用 SM 数      =  get_num_sms()             # 默认 = 真实数；若 set_num_sms 设过，则用设定值
每 SM 最大 smem =  get_max_smem_per_sm()      # 探测一次，缓存
```

关键设计：

- **探测函数带 `@lru_cache`**：硬件属性在一次运行中不变，重复探测是浪费，所以用 `functools.lru_cache(maxsize=None)` 缓存第一次的结果。
- **覆盖用一个模块级全局变量 `_num_sms`**：默认为 `0` 表示「未覆盖」，此时 `get_num_sms` 退回到真实硬件数；一旦 `set_num_sms(n)` 设过，就返回 `n`。
- **断言保护**：`set_num_sms` 会断言 `0 < num_sms <= get_device_num_sms()`，不允许设成 0 或超过物理上限。

#### 4.3.3 源码精读

- [tile_kernels/config.py:4](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/config.py#L4)：`_num_sms = 0`——覆盖用的全局变量，`0` 是「未覆盖」的哨兵值。
- [tile_kernels/config.py:7-10](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/config.py#L7-L10)：`get_device_num_sms` 用 `torch.cuda.get_device_properties(...).multi_processor_count` 读取物理 SM 数，并用 `lru_cache` 缓存。这是探测真实硬件的那一步。
- [tile_kernels/config.py:13-16](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/config.py#L13-L16)：`set_num_sms` 写入全局 `_num_sms`，并用断言保证设定值合法（严格大于 0、不超过物理上限）。
- [tile_kernels/config.py:19-23](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/config.py#L19-L23)：`get_num_sms` 是对外的查询入口——`_num_sms == 0` 时返回真实硬件数，否则返回覆盖值。算子和 benchmark 都应该调用它而不是直接读硬件。
- [tile_kernels/config.py:26-29](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/config.py#L26-L29)：`get_max_smem_per_sm` 读取每 SM 最大共享内存（`shared_memory_per_multiprocessor`），同样 `lru_cache` 缓存。它被 engram 等需要精确估算占用的 kernel 使用（见 u6/u10 单元）。

> 概念解释：`torch.cuda.get_device_properties(device)` 返回一个结构体，包含 `name`、`major/minor`（算力架构）、`multi_processor_count`（SM 数）、`shared_memory_per_multiprocessor` 等字段。CUDA 程序里的 `cudaDeviceProp` 就是同一个东西。

#### 4.3.4 代码实践

**实践目标**：理解「探测 → 覆盖 → 查询」三步，并动手验证覆盖逻辑。

**操作步骤**：

1. 阅读 [config.py:19-23](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/config.py#L19-L23) 的 `get_num_sms`，确认它「`_num_sms` 为 0 就返回硬件数，否则返回覆盖值」的分支。
2. 在装有 GPU 与 `tile_kernels` 的环境执行：

   ```python
   import tile_kernels
   from tile_kernels.config import get_num_sms, get_device_num_sms, set_num_sms
   print("device SMs:", get_device_num_sms())
   print("usable SMs (default):", get_num_sms())
   set_num_sms(8)
   print("usable SMs (after set_num_sms(8)):", get_num_sms())
   ```

**需要观察的现象**：默认时「usable SMs」应等于「device SMs」；调用 `set_num_sms(8)` 后「usable SMs」变为 8。

**预期结果**：覆盖值生效；若尝试 `set_num_sms(0)` 或超过物理数的值，会触发断言报错（待本地验证具体 SM 数）。

> 注意：本实践需要 CUDA GPU。若无 GPU，至少完成步骤 1 的源码阅读，并推断「`_num_sms` 初值为 0 时 `get_num_sms` 必返回硬件数」这一结论。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `get_device_num_sms` 和 `get_max_smem_per_sm` 用 `lru_cache`，而 `get_num_sms` 不用？

**参考答案**：前两者读取的是**不变的硬件属性**，缓存可避免重复探测开销且结果永远一致；`get_num_sms` 的返回值依赖可变的全局 `_num_sms`（`set_num_sms` 会改写它），若加缓存会返回过时结果，所以不能缓存。

**练习 2**：如果调用 `set_num_sms(get_device_num_sms() + 1)` 会发生什么？

**参考答案**：[config.py:15](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/config.py#L15) 的断言 `0 < num_sms <= get_device_num_sms()` 不成立，会抛出 `AssertionError`。

---

### 4.4 `utils`：对齐与整除工具

#### 4.4.1 概念说明

GPU kernel 几乎处处需要对齐和整除：分块大小必须是 2 的幂、张量维度要能被 tile 整除、地址要按若干字节对齐。`utils.py` 把这些高频小运算集中成三个纯函数，供所有算子家族复用。它们没有副作用、不依赖任何运行时状态，是整个项目里最简单的「地基」。

#### 4.4.2 核心流程

三个函数的语义：

| 函数 | 含义 | 典型用途 |
| --- | --- | --- |
| `ceil_div(x, y)` | \(\lceil x / y \rceil\)（向上整除） | 计算需要多少个 tile 才能覆盖 `x` 个元素 |
| `align(x, y)` | 把 `x` 向上取整到 `y` 的倍数 | 把维度对齐到分块粒度 |
| `is_power_of_two(x)` | `x` 是否为 2 的幂 | 校验 `block`/`tile` 大小是否合法 |

数学上，`align` 就是 `ceil_div` 的延伸：

\[
\text{align}(x, y) = \lceil x / y \rceil \cdot y
\]

`ceil_div` 用整数运算实现，避免浮点误差：

\[
\lceil x / y \rceil = \lfloor (x + y - 1) / y \rfloor = (x + y - 1) // y
\]

`is_power_of_two` 用的是经典位运算技巧：2 的幂在二进制中只有一个 1 比特，`x & (x - 1)` 会清除最低位的 1，若结果为 0 则说明原来只有一个 1 比特。

#### 4.4.3 源码精读

- [tile_kernels/utils.py:1-2](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/utils.py#L1-L2)：`ceil_div(x, y)` 用 `(x + y - 1) // y` 实现向上整除，纯整数运算。
- [tile_kernels/utils.py:5-6](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/utils.py#L5-L6)：`align(x, y)` 直接复用 `ceil_div(x, y) * y`，体现「小函数组合大函数」的写法。
- [tile_kernels/utils.py:9-10](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/utils.py#L9-L10)：`is_power_of_two(x)` 返回 `x > 0 and (x & (x - 1)) == 0`。注意 `x > 0` 用来排除 0（0 不是 2 的幂）。

> 举个小例子理解位运算：`x = 8`（二进制 `1000`），`x - 1 = 7`（二进制 `0111`），`1000 & 0111 = 0000`，为 0，故 `8` 是 2 的幂；`x = 6`（`110`），`x - 1 = 5`（`101`），`110 & 101 = 100`，非 0，故 `6` 不是 2 的幂。

#### 4.4.4 代码实践

**实践目标**：动手验证三个函数的行为，确认公式与直觉一致。

**操作步骤**：

1. 在任何能运行 Python 的环境（**不需要 GPU**）执行：

   ```python
   from tile_kernels.utils import ceil_div, align, is_power_of_two
   print(ceil_div(10, 3))          # 预期 4
   print(align(10, 8))             # 预期 16
   print(is_power_of_two(8))       # 预期 True
   print(is_power_of_two(6))       # 预期 False
   print(is_power_of_two(0))       # 预期 False
   ```

2. 用手算核对每一行的结果（套用上面的公式与位运算规则）。

**需要观察的现象**：`ceil_div(10,3)` 因为 \(10/3 = 3.33\) 向上取整为 4；`align(10,8)` 因为 10 在 8 的倍数中向上对齐到 16。

**预期结果**：输出依次为 `4`、`16`、`True`、`False`、`False`。

> 说明：这是本讲唯一不依赖 GPU 的实践，强烈建议亲手跑一遍，建立对三个工具函数的肌肉记忆。

#### 4.4.5 小练习与答案

**练习 1**：`align(0, 8)` 的结果是什么？这个结果在分块场景下合理吗？

**参考答案**：`align(0, 8) = ceil_div(0, 8) * 8 = 0 * 8 = 0`。结果为 0，表示「0 个元素对齐后仍是 0」，在「不需要任何 tile」的语义下是合理的（比如空批次）。

**练习 2**：如果把 `is_power_of_two` 里的 `x > 0 and` 去掉，只留 `(x & (x - 1)) == 0`，会对哪些输入给出错误结果？

**参考答案**：`x = 0` 时 `0 & (-1)` 在二进制补码下等于 `0`，会误判 0 为「2 的幂」；此外对负数也无法正确处理。`x > 0` 既排除了 0，也排除了负数，是必要的保护。

---

## 5. 综合实践

把本讲的知识串起来，完成下面这个「导航 + 追踪」小任务：

**任务**：选定任意一个**非 transpose** 的算子家族（推荐 `moe` 或 `quant`），完成以下三步：

1. **画结构树**：用 `git ls-files tile_kernels/<家族>/` 列出该家族的所有文件，画出一棵小树，标注「哪些是 TileLang kernel 文件、哪个是 `__init__`」。
2. **追调用路径**：参照本讲 4.2 的方法，跟踪一个该家族的 wrapper 调用路径，写出它的三级解析链。例如对 `moe`，可以追踪 `tile_kernels.moe.topk_gate`：
   - 包入口导入子包 `moe`（[__init__.py:3-13](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/__init__.py#L3-L13) 中的一项）；
   - 子包 `__init__` 再导出 `topk_gate`（见 [moe/__init__.py:10](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/moe/__init__.py#L10)）；
   - 最终指向 `topk_gate_kernel.py` 里的 `topk_gate` wrapper。
3. **定位基础设施使用**：在该家族的源码里搜索 `get_num_sms`、`ceil_div`、`align`、`is_power_of_two` 是否被用到，记录调用位置。这能帮你体会 `config`/`utils` 作为「全包共享地基」的角色。

**预期产出**：一张家族文件树 + 一条三级调用链 + 一份基础设施使用清单。

**说明**：这个任务不需要 GPU，是纯源码阅读与整理，目的是让你在进入具体算子实现之前，先在导航层面「逛」遍整个包。

## 6. 本讲小结

- `tile_kernels` 包由「四层 + 两件基础设施」组成：算子层（`transpose/moe/quant/engram/mhc`）、torch 参考层（`torch/`）、modeling 层（`modeling/`）、testing 层（`testing/`），外加基础设施 `config.py` 与 `utils.py`。
- [`tile_kernels/__init__.py`](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/__init__.py) 只做聚合：一次性导入所有子模块，并把 `config` 里的 3 个函数提升到顶层。
- 每个算子家族的子包 `__init__` 遵循同一模式——把 wrapper 函数从具体 `*_kernel.py` 再导出；用户调用的入口是 wrapper，而非 TileLang kernel 对象。
- `tile_kernels.transpose.transpose` 的解析链是：包入口导入子包 → 子包 `__init__` 再导出函数 → `batched_transpose_kernel.py` 中的 wrapper。
- [`config.py`](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/config.py) 提供 SM 数量与每 SM 共享内存的硬件探测（带 `lru_cache`），并支持用 `set_num_sms` 限制可用 SM 数；算子应通过 `get_num_sms()` 查询而非直接读硬件。
- [`utils.py`](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/utils.py) 提供无副作用的小工具 `ceil_div`/`align`/`is_power_of_two`，服务于分块、对齐、合法性校验。

## 7. 下一步学习建议

到这里你已经能在包结构层面导航整个项目。接下来：

- 想看懂任何一个算子家族的内部实现，进入 **u2-l1（TileLang 算子解剖：jit + prim_func + 动态符号）**，学习 TileLang 的标准骨架。本讲追踪到的 `batched_transpose_kernel.py` 里的 `@tilelang.jit` + `@T.prim_func` + `T.dynamic`，正是 u2-l1 要展开的内容。
- 如果你想先看看算子家族的「全貌」，可以随手读 [`tile_kernels/transpose/batched_transpose_kernel.py`](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/transpose/batched_transpose_kernel.py) 的 wrapper 部分（`transpose`/`batched_transpose`），对照本讲的包结构图建立「入口→启动」的直觉，把 TileLang 语法细节留给 u2 单元。
- 后续 u4/u5/u6/u7 等单元会频繁用到本讲的 `config`（SM/smem 探测）和 `utils`（整除对齐），届时可回看本讲作为参照。
