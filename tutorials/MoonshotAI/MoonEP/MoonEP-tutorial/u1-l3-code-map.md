# u1-l3 代码地图：目录结构与模块职责

## 1. 本讲目标

学完本讲，你应该能够：

1. 拿到任意一个功能关键词（「规划」「去重」「预取」「梯度归约」「对称内存」），立刻说出它住在哪个文件、对外入口函数叫什么、在文件的第几行。
2. 区分 MoonEP 的**三层代码**：Python 内核层（`moonep/`，CuTe DSL 编写、运行时 JIT）、C++ 绑定层（`csrc/` → `moonep._C`）、验证层（`tests/` + `benchmarks/`），并说出每层的边界。
3. 逐个说出 `moonep/` 下 13 个 `.py` 文件的职责，以及它们之间的 import 依赖关系。
4. 通过自己动手写脚本，**验证** `api.py` 是所有子内核模块的唯一汇聚点（hub），而不是靠本讲口头断言。
5. 建立一张后续讲义通用的「文件索引」——u2 到 u6 的每一讲都会引用本讲列出的文件。

## 2. 前置知识

本讲只做「看地图」，不深入任何算法。需要几个基础概念：

- **包（package）与模块（module）**：一个含 `__init__.py` 的目录就是 Python 包。`import moonep.api` 时，Python 先执行 `moonep/__init__.py`，再执行 `moonep/api.py`。`__init__.py` 里 `from .api import Buffer` 这种写法（点号开头）叫**相对导入**，意思是「从本包的 api 模块导入」。
- **内核（kernel，GPU 语境）**：一段在 GPU 上由成千上万线程并行执行的函数。MoonEP 的内核都用 CuTe DSL（Python 写法）描述，首次调用时 JIT 编译成 GPU 代码——这在 u1-l2 已讲过，本讲只需记住：`moonep/` 里那些 `class XxxKernel` 就是内核本体。
- **`launch_*` 约定**：MoonEP 每个内核模块在文件底部提供一个 `launch_xxx(...)` 函数，负责处理参数、触发 JIT 编译并把内核提交到 CUDA 流上。它是每个内核模块的「对外入口」。
- **依赖图、邻接表、出度、汇聚点（hub）**：把每个文件看成一个节点，「A import B」画一条 A→B 的边，全体边构成**依赖图**；用「A → {B, C}」的形式记录就叫**邻接表**；一个节点指向的节点数叫**出度**；被大家共同依赖、把系统串成整体的节点叫**汇聚点**。
- **`ast` 模块**：Python 自带的源码解析器，能把 `.py` 文件解析成语法树。本讲的实践脚本用它来**读出** import 关系和函数定义，全程不需要真的 `import moonep`（也就不需要 GPU 环境）。

承接前两讲：u1-l1 建立了符号系统（S/K/E/R/B/NvS/H/H′）和三大设计；u1-l2 讲清了「两层代码」的编译链路（`csrc/` 经 setup.py 编成 `moonep/_C.so`）。本讲把视野拉到整个仓库，回答「哪个功能在哪个文件」。

## 3. 本讲源码地图

| 文件/目录 | 层 | 作用 | 本讲关注点 |
| --- | --- | --- | --- |
| `moonep/__init__.py` | Python 内核层 | 包入口，只导出 `Buffer` 与 `MoonEPCommPlan` | 9 行看懂公共 API 面 |
| `moonep/api.py` | Python 内核层 | 顶层 `Buffer` 类，汇聚所有子模块 | import 块 = 全家福 |
| `moonep/buffer.py` | Python 内核层 | 唯一消费 C++ 扩展 `moonep._C` 的模块（对称内存分配） | `from moonep._C import ...` |
| `moonep/constants.py` | Python 内核层 | 跨模块共享的位宽/常量定义 | 全文 17 行 |
| `moonep/_common.py` | Python 内核层 | 共享 PTX 原语（TMA 拷贝、跨 rank 屏障、原子操作） | 模块文档字符串里的两个 TMA 限制 |
| `moonep/planning.py` | Python 内核层 | 在线规划器 + `MoonEPCommPlan` 数据类 | 入口 `launch_planning` |
| `moonep/dispatch.py` | Python 内核层 | dispatch 内核（token 派发） | 入口 `launch_dispatch` |
| `moonep/dispatch_epilogue.py` | Python 内核层 | dispatch 之后的本地重复展开 | 入口 `launch_dispatch_epilogue` |
| `moonep/combine.py` | Python 内核层 | combine 内核（K 路求和归并） | 入口 `launch_combine` |
| `moonep/combine_prologue.py` | Python 内核层 | combine 之前的本地重复累加 | 入口 `launch_combine_prologue` |
| `moonep/prefetch.py` | Python 内核层 | 远程专家权重预取内核 | 入口 `launch_prefetch` |
| `moonep/grad_reduce.py` | Python 内核层 | 远程专家梯度归约内核 | 入口 `launch_grad_reduce` |
| `moonep/inter_rank_sync.py` | Python 内核层 | 规划前的最小跨 rank 对齐内核 | 入口 `launch_inter_rank_sync` |
| `csrc/bindings.cu` + `csrc/nvl_shared_buffer.cuh` | C++ 绑定层 | pybind11 绑定 + VMM/组播实现（u1-l2 已讲，u2 详读） | 只记位置，不重读 |
| `tests/kernel_test_utils.py` | 验证层 | `KernelCase` 参数化 + 多 rank 断言助手 | 本讲主读的测试工具 |
| `tests/conftest.py`、`tests/planning_reference.py`、`tests/generate_topk_routing.py`、六个 `test_*.py` | 验证层 | fixture、PyTorch 参考实现、路由生成器、六组内核测试 | 各自分工 |
| `benchmarks/bench_comm.py` | 验证层 | 算子级通信基准（扫描 H/K/EP/bias_ratio） | import 块 + 计时算子清单 |
| `benchmarks/bench_vs_deepep.py`、`bench_prefetch.py`、`bench_grad_reduce.py` | 验证层 | 对比基准与两个专用基准 | 各自度量对象 |
| `figure/generate_buffer_figures.py` | 辅助 | 用 matplotlib 生成 README 里的权重/梯度缓冲布局插图 | 知道存在即可 |
| `setup.py`、`README.md`、`LICENSE` | 工程 | 构建入口（u1-l2 已讲）、项目说明、许可证 | 已覆盖 |

## 4. 核心概念与源码讲解

### 4.1 仓库全景：四个代码目录与三层代码

#### 4.1.1 概念说明

整个仓库约 1.3 万行源码，分四个目录、三层职责：

```text
MoonEP/
├── moonep/        ← 第 1 层：Python 内核层（CuTe DSL，JIT 编译，约 7100 行）
├── csrc/          ← 第 2 层：C++/CUDA 绑定层（预编译成 moonep/_C.so，约 600 行）
├── tests/         ← 第 3 层：验证层——正确性（六组内核测试，约 3300 行）
├── benchmarks/    ← 第 3 层：验证层——性能（四个基准脚本，约 2000 行）
├── figure/        ← 辅助：生成 README 插图的画图脚本
├── setup.py       ← 构建入口（唯一）
└── README.md / LICENSE
```

三层的分工边界非常干净：

1. **Python 内核层**负责所有「算得快」的逻辑：规划算法、通信内核、流水线调度，全部用 CuTe DSL 写，首次调用时 JIT 成 GPU 代码。
2. **C++ 绑定层**只负责「纯 Python 做不到」的事：调 CUDA 驱动 API 分配跨 rank 对称内存、导入 NVSwitch 组播对象。它编译出的 `moonep._C` **只被一个 Python 文件消费**（后面验证）。
3. **验证层**不参与运行时，但它是理解内核行为的钥匙：每个 GPU 内核都配了一个纯 PyTorch 的「参考实现」，测试就是拿 GPU 输出和参考实现对拍。

#### 4.1.2 核心流程

用户敲下 `import moonep` 后的加载路径（Python 侧）：

```text
import moonep
  └─ 执行 moonep/__init__.py
       ├─ from .api import Buffer          → 加载 api.py
       │    ├─ from .buffer import ...     → 加载 buffer.py
       │    │    └─ from moonep._C import ...  → 加载 C++ 扩展 .so（此时必须已编译）
       │    ├─ from .planning import ...   → 加载规划器（连带 _common、constants）
       │    ├─ from .dispatch import ...   → …
       │    └─ （其余 7 个子模块同理由 api.py 牵入）
       └─ from .planning import MoonEPCommPlan
最终 moonep 命名空间里只有两个名字：Buffer、MoonEPCommPlan
```

注意两个时点：`moonep._C` 在 **import 时**就要存在（所以必须先构建，见 u1-l2）；而 CuTe DSL 内核在**首次 launch 时**才 JIT 编译。

#### 4.1.3 源码精读

**(a) 包入口：9 行决定公共 API 面。**

[moonep/__init__.py:1-9](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/__init__.py#L1-L9) —— 只从 `.api` 导入 `Buffer`、从 `.planning` 导入 `MoonEPCommPlan`，`__all__` 也只列这两个。也就是说：12 个模块、几千行代码，对外的门面只有 1 个类 + 1 个数据类。这是阅读大型库的重要信号——**入口极窄，内部分层**。

**(b) 插图脚本：`figure/` 目录。**

[figure/generate_buffer_figures.py:1-6](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/figure/generate_buffer_figures.py#L1-L6) —— 文档字符串写明它生成 `weight_buffer.png` 与 `grad_buffer.png` 两张图，解释 README 里的 `[E+B, H, H']` 权重/梯度缓冲布局。它不参与构建与运行，但当你学到 u5 单元看不懂缓冲布局时，回头跑一下这个脚本画出的图会有帮助。

**(c) 文件体量分布（先看地形再进城）。**

用 `wc -l` 统计的关键数字（本讲实践 A 会让你自己复现）：

| 排名 | 文件 | 行数 | 对应手册单元 |
| --- | --- | --- | --- |
| 1 | `moonep/planning.py` | 1316 | u3（在线规划器，整整一个单元） |
| 2 | `moonep/api.py` | 1159 | u1-l4、u2、u6（API 与编排） |
| 3 | `moonep/dispatch.py` | 984 | u4（dispatch 内核） |
| 4 | `tests/test_grad_reduce.py` | 500 | u5、u6-l4 |
| 5 | `moonep/_common.py` | 459 | u4-l1（PTX 基础设施） |

体量分布本身就是路线图：**规划器最重、dispatch 次之、combine/prologue/prefetch/grad_reduce 各自成块**——这正是手册 u3/u4/u5 三个单元的划分依据。

#### 4.1.4 代码实践

**实践 A（任何机器，无需 GPU）：清点仓库地形。**

1. **实践目标**：用只读命令生成「目录 → 文件 → 行数」清单，验证上表数字，并对仓库规模建立直觉。
2. **操作步骤**：在仓库根目录运行下面脚本（存为 `code_inventory.py`，示例代码，放在仓库外任意位置；它只读不写）：

   ```python
   # 示例代码：git ls-files 清点 + 按目录汇总行数
   import subprocess
   from collections import defaultdict

   files = subprocess.run(
       ["git", "ls-files"], capture_output=True, text=True, check=True
   ).stdout.splitlines()
   code_files = [p for p in files if p.endswith((".py", ".cu", ".cuh"))]

   by_dir = defaultdict(list)
   for p in code_files:
       by_dir[p.split("/")[0] if "/" in p else "."].append(p)

   for d in sorted(by_dir):
       total = 0
       for f in sorted(by_dir[d]):
           n = sum(1 for _ in open(f, errors="replace"))
           total += n
           print(f"{n:6d}  {f}")
       print(f"---- {d}/ 小计: {len(by_dir[d])} 个文件, {total} 行\n")
   ```

3. **需要观察的现象**：`moonep/` 有 13 个 `.py` 文件、约 7100 行；`csrc/` 只有 2 个文件、约 600 行；`tests/`（约 3300 行）与 `benchmarks/`（约 2000 行）合计约为内核层的四分之三——**验证代码的体量是内核本体量级的一半以上**，这是高性能库的常态。
4. **预期结果**：最大的三个文件依次是 `planning.py`(1316)、`api.py`(1159)、`dispatch.py`(984)。若你的输出与此不符，说明 HEAD 与本讲所基于的 commit（`2bd860b`）不同。
5. 全部命令为只读操作，本讲实践均可安全运行；无需「待本地验证」标注。

#### 4.1.5 小练习与答案

**练习 1**：`csrc/` 为什么不放进 `moonep/` 包目录里？
**答案**：`csrc/` 是编译期的 C++/CUDA 源码，由 setup.py 的 nvcc 编译；产物 `moonep/_C.*.so` 才落进 `moonep/` 包目录供运行时 import。源码与产物分离，包目录里只出现「可 import 的东西」。

**练习 2**：仓库里被 `tests/` 和 `benchmarks/` 同时使用的文件是哪个？
**答案**：`tests/generate_topk_routing.py`——`bench_comm.py` 第 78 行、`bench_vs_deepep.py` 第 53 行都 `from tests.generate_topk_routing import generate_topk_routing`，测试与基准共用同一套路由生成语义（bias_ratio 的对数正态偏置）。

**练习 3**：`figure/` 目录的脚本依赖 matplotlib，但 `setup.py` 的 `install_requires` 里没有 matplotlib（u1-l2 讲过只有 `nvidia-cutlass-dsl==4.4.2`）。这说明了什么？
**答案**：插图脚本是开发者的辅助工具，不属于 MoonEP 运行时的依赖闭包——依赖声明精确反映了「哪些代码在关键路径上」。

### 4.2 moonep/ 内核包逐模块导读

#### 4.2.1 概念说明

把 13 个文件按角色分成五组来记：

| 分组 | 文件 | 一句话职责 |
| --- | --- | --- |
| 门面 | `__init__.py`、`api.py` | `Buffer` 类：持有全部缓冲，编排所有内核的调用顺序 |
| 内存基础设施 | `buffer.py` | 对称内存（NVLink 互联的跨 rank 共享显存）的 Python 侧封装 |
| 共享工具 | `_common.py`、`constants.py` | PTX 原语库 / 位宽与常量定义 |
| 规划器 | `planning.py` | 在线均衡算法 + 规划结果数据类 `MoonEPCommPlan` |
| 通信内核 | `dispatch.py`、`dispatch_epilogue.py`、`combine.py`、`combine_prologue.py`、`prefetch.py`、`grad_reduce.py`、`inter_rank_sync.py` | 七个各自独立的 GPU 内核 |

#### 4.2.2 核心流程

先看 `Buffer` 内部如何把内核串起来——这是理解「为什么 api.py 是汇聚点」的关键。`api.py` 里有两个编排函数（`Buffer.dispatch`/`combine` 的内部实现），调度方向按序经过四个模块：

[moonep/api.py:617-661](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L617-L661) —— `_run_dispatch_on_current_stream` 的调用顺序：

```text
（可选）launch_inter_rank_sync     ← inter_rank_sync.py，先对齐各 rank 的进度
（可选）launch_planning            ← planning.py，产出 dst/plan
launch_dispatch                    ← dispatch.py，按行 TMA 写远端 + 构建去重结构
launch_dispatch_epilogue           ← dispatch_epilogue.py，本地重复行展开
（可选）边界拷贝 hidden_nvsh.copy_  ← 普通 torch op（zero_copy=True 时消失）
```

[moonep/api.py:663-696](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L663-L696) —— `_run_combine_on_current_stream` 是镜像流程：可选同步 → 可选边界拷贝 → `launch_combine_prologue`（本地重复累加）→ `launch_combine`（跨 rank 归并求和）。

而这一切读写的显存，都来自 `buffer.py` 分配的对称缓冲。**编排顺序 = 数据依赖顺序**，后续 u4 单元会逐个拆开每个内核。

#### 4.2.3 源码精读

**(a) 顶层 API 的「全家福」import 块。**

[moonep/api.py:57-72](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L57-L72) —— 这 16 行是全仓库信息密度最高的代码：从 `.buffer` 导入 5 个内存函数、从 `.constants` 导入 `DEDUP_BUILDER_WARPS`、从 `.planning` 导入 `MoonEPCommPlan`/`allocate_planning_outputs`/`launch_planning`，再依次导入 `launch_inter_rank_sync`、`launch_dispatch`、`launch_dispatch_epilogue`、`launch_combine`、`launch_combine_prologue`、`launch_prefetch`（连同 `_ELEM_TYPES`、`retile_for_prefetch`）、`launch_grad_reduce`。**除了 `_common.py`（只被内核模块间接使用），api.py import 了全部兄弟模块**——这就是「唯一汇聚点」的字面证据。

**(b) Buffer 类：门面本体。**

[moonep/api.py:440-508](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L440-L508) —— 类文档字符串写明 Buffer「拥有 NVLink/VMM 通信缓冲、局部 scratch 张量和异步通信流」；构造参数 `S, H, K, E, num_ep_ranks, num_sms, token_padding, B, group, comm_stream_priority, enable_pdl, explicitly_destroy` 的含义写在 docstring 里（u1-l4 会逐个实练）。四个业务方法的落点：

| 方法 | 位置 | 语义 |
| --- | --- | --- |
| `dispatch` | [api.py:720](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L720-L720) | 派发：token-major → 专家分组 |
| `prefetch_weight` | [api.py:860](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L860-L860) | 把热门远程专家权重搬进本地预取槽 |
| `combine` | [api.py:950](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L950-L950) | 归并：K 路结果加权求和回 token-major |
| `reduce_grad` | [api.py:1085](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L1085-L1085) | 把预取槽上算出的梯度归约回专家属主 rank |

**(c) buffer.py：C++ 扩展的唯一消费者。**

[moonep/buffer.py:10-23](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/buffer.py#L10-L23) —— `from moonep._C import ...` 一次导入 12 个符号：`nvl_dist_alloc`/`nvl_dist_map`（对称内存分配/映射）、`nvl_fabric_supported`/`nvl_release_mem_handle`（fabric 句柄）、`get_vmm_granularity`/`get_multicast_granularity`（对齐粒度查询）、`nvl_multicast_*` 四个（NVSwitch 组播对象）。全仓库只有这一处直接 import `_C`——C++ 层与 Python 内核层之间是**单点接口**。它的公开函数落点：`pad_dim0_for_alignment`(L95)、`create_nvl_dist_tensor`(L217)、`create_nvl_dist_multicast_tensor`(L244)、`create_nvl_single_owner_tensor`(L338)。

**(d) 共享工具：`_common.py` 与 `constants.py`。**

[moonep/_common.py:1-17](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/_common.py#L1-L17) —— 模块文档字符串解释了它存在的理由：绕开 `nvidia-cutlass-dsl` 4.4.2 的两个 TMA lowering 限制（单维 box 上限 256 元素；`CopyBulkG2SOp` 会在错误的共享内存空间插入 `mapa` 导致挂起），于是用 `llvm.inline_asm` 直接发 `cp.async.bulk` PTX 指令。它是 dispatch/combine/planning 共同的「驱动级工具箱」。

[moonep/constants.py:1-17](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/constants.py#L1-L17) —— 全文 17 行：`RANK_BITS=7`（打包编码里 rank 字段占 7 位）、`KIDX_BITS=7`（top-k 索引字段位宽）、`DEDUP_BUILDER_WARPS=4`（每个 dispatch CTA 的去重构建 warp 数）。注释自称是 planning/dispatch 内核与测试的「single source of truth」——跨模块约定的数字必须住在公共文件里。

**(e) 规划器与七个内核的入口表。**

[moonep/planning.py:1-11](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/planning.py#L1-L11) —— 文档字符串：单个 cooperative grid 启动在一个内核里跑完 Phase A/B/C/D，产出 `dst`（含重复项的负数编码）、`cu_seqlens`、`experts_to_copy`、`zero_fill_ranges`、`remote_stats`。数据类 `MoonEPCommPlan` 在 [planning.py:31-88](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/planning.py#L31-L88)（frozen dataclass，u3-l1 逐字段精读）。

其余六个内核的文档字符串都遵循同一格式：**是什么 → 数据怎么流 → 与相邻内核的衔接契约**。建议你现在就打开扫一遍（只需读每个文件的头 20 行）：

| 模块 | 文档字符串 | 对外入口 |
| --- | --- | --- |
| dispatch | [dispatch.py:1-13](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/dispatch.py#L1-L13)：warp 特化的 G2S/S2G 环形流水线，按行 TMA 直写远端 | `launch_dispatch` [L839](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/dispatch.py#L839-L839) |
| dispatch_epilogue | [dispatch_epilogue.py:1-18](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/dispatch_epilogue.py#L1-L18)：dispatch 之后在本地 NVL shard 上做重复行**扇出**，纯本地无跨 rank 通信 | `launch_dispatch_epilogue` [L367](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/dispatch_epilogue.py#L367-L367) |
| combine | [combine.py:1-14](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/combine.py#L1-L14)：3 段式流水线（G2S 加载 / 4 个 fp32 ACC warp / S2G 写回），把每 token 的 K 份结果加回 | `launch_combine` [L565](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/combine.py#L565-L565) |
| combine_prologue | [combine_prologue.py:1-19](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/combine_prologue.py#L1-L19)：combine 之前把重复组 fp32 **累加**回主行——epilogue 的逆操作 | `launch_combine_prologue` [L545](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/combine_prologue.py#L545-L545) |
| prefetch | [prefetch.py:1-16](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/prefetch.py#L1-L16)：持久化 2D TMA 流水线，把远程专家权重 `[E,H,H']` 拷进本地槽 `[B,H,H']`；`_ELEM_TYPES` 在 [L32-36](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/prefetch.py#L32-L36) 支持 bf16/int8/uint8 | `launch_prefetch` [L357](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/prefetch.py#L357-L357)、`retile_for_prefetch` [L342](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/prefetch.py#L342-L342) |
| grad_reduce | [grad_reduce.py:1-27](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/grad_reduce.py#L1-L27)：5 warp 分工（1 加载 + 4 累加）把各 rank 归约缓冲里的 fp32 梯度累回属主 rank；文档里记录了「远程清零被否决」的带宽权衡 | `launch_grad_reduce` [L437](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/grad_reduce.py#L437-L437) |
| inter_rank_sync | [inter_rank_sync.py:1-7](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/inter_rank_sync.py#L1-L7)：单 CTA 的最小跨 rank 屏障，在规划前对齐各 rank、消除上游流抖动 | `launch_inter_rank_sync` [L119](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/inter_rank_sync.py#L119-L119) |

#### 4.2.4 代码实践

**实践 B（无需 GPU、无需构建）：文档字符串与入口提取器。**

1. **实践目标**：不 import 任何 moonep 模块（也就不需要 `_C.so` 和 GPU），纯文本解析出每个模块「一句话职责 + 对外入口函数」，生成你自己的速查表。
2. **操作步骤**：把下面的脚本存为 `moonep_docmap.py`（示例代码），在仓库根目录运行 `python moonep_docmap.py`：

   ```python
   # 示例代码：用 ast 提取 moonep 各模块的文档字符串首行与 launch_*/create_*/allocate_* 入口
   import ast
   from pathlib import Path

   for path in sorted(Path("moonep").glob("*.py")):
       tree = ast.parse(path.read_text())
       doc = ast.get_docstring(tree)
       first = doc.strip().splitlines()[0] if doc else "(无模块文档字符串)"
       entries = []
       for node in tree.body:
           if isinstance(node, ast.FunctionDef) and node.name.startswith(
               ("launch_", "create_", "allocate_")
           ):
               entries.append(f"{node.name}()  # L{node.lineno}")
       print(f"{path.name:22s} | {first}")
       for e in entries:
           print(f"{'':24s}| 入口 {e}")
   ```

3. **需要观察的现象**：每个文件的输出由「文档字符串首行 + 若干入口行」组成；`buffer.py` 和 `constants.py` 显示「无模块文档字符串」（前者直接以 import 开头，后者是纯常量文件）；`api.py` 没有匹配的模块级函数——它的入口是 `Buffer` 类。
4. **预期结果**：能提取出 9 个入口（`pad_dim0_for_alignment`、三个 `create_nvl_*`、`allocate_planning_outputs`、7 个 `launch_*` 共计 11 个左右，取决于前缀过滤），每个入口的行号与本讲 4.2.3 (c)(e) 两张表一致。
5. 该脚本只读源码，任何环境可复现，无需「待本地验证」标注。

#### 4.2.5 小练习与答案

**练习 1**：哪个通信内核模块**没有任何 moonep 内部依赖**？这带来什么工程上的好处？
**答案**：`prefetch.py`——它只 import torch/cutlass 等外部库（见 [prefetch.py:18-29](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/prefetch.py#L18-L29)），不依赖 `_common`/`planning`。佐证：`benchmarks/bench_prefetch.py:15-16` 只 import `buffer` 的两个内存函数和 `launch_prefetch` 就能单独跑基准。

**练习 2**：`combine.py` 为什么不 import `planning`？
**答案**：`launch_combine` 的输入是 `dst` **张量**（`plan.dst`）加可选的 `output_sk`，不需要完整 plan 对象——见 [api.py:690-696](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L690-L696) 传的是 `plan.dst`，以及 [bench_comm.py:281](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/benchmarks/bench_comm.py#L281-L281) 的 `launch_combine(ctx, output, dst)`。去重结构的消费方是 epilogue/prologue，不是 combine 本体。

**练习 3**：`dispatch_epilogue.py` 与 `combine_prologue.py` 这对模块为什么单独成文件、而不并进 dispatch/combine？
**答案**：它们是「本地重复行处理」的一对镜像操作（epilogue 扇出、prologue 归约），各自有独立的 warp 布局、独立的启动配置（SM 数由 `MOONEP_NUM_SMS_DEDUP` 单独控制，见 [api.py:82-102](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L82-L102)），且生命周期与主内核不同（复用 plan 时主内核跳过部分工作而它们照常跑）。拆开文件 = 拆开调度自由度。

### 4.3 tests/：参数化测试工具与参考实现

#### 4.3.1 概念说明

验证层的设计哲学：**每个 GPU 内核配一个纯 PyTorch 参考实现，测试就是对拍**。`tests/` 下五类文件：

| 文件 | 角色 |
| --- | --- |
| `conftest.py` | pytest 配置：`dist_env` fixture（非 torchrun 则跳过）+ autouse 的 Buffer 清理 fixture（u1-l2 已精读） |
| `kernel_test_utils.py` | 测试基础设施：`KernelCase` 参数化用例、Buffer 生命周期管理、多 rank 断言助手 |
| `planning_reference.py` | 规划器的 PyTorch 参考实现（u3 单元逐行精读） |
| `generate_topk_routing.py` | 路由生成器：按 bias_ratio（对数正态 σ）造均衡/偏置路由 |
| `test_{planning,dispatch,combine,e2e,prefetch,grad_reduce}.py` | 六组内核测试，每组头部都写着 `torchrun --nproc_per_node=8 -m pytest ...` |

#### 4.3.2 核心流程

一个内核测试的骨架是「初始化 → 造输入 → GPU 内核 → 参考实现 → 对拍」：

```text
KernelCase(S=.., K=.., epn=.., H=.., num_sms=..)     ← 声明一组配置
  └─ init_case(case, R)                              ← kernel_test_utils.py
       ├─ skip_if_unsupported_world_size             ← R 不匹配就跳过
       ├─ Buffer(S, H, K, case.E(R)=R*epn, R, ...)   ← 构造被测对象并登记
       └─ 返回内部 ctx
make_topk(case, rank, R)                             ← 造路由输入
  └─ balanced/biased 随机，或 all_local/all_remote/
     single_expert/duplicate_topk 极端模式
（被测内核） vs planning_reference.py 的 PyTorch 复现
  └─ assert_tensor_equal_all_ranks / assert_close_all_ranks / ...
       └─ assert_all_ranks：任一 rank 失败则全体报错
测试结束 → conftest 的 autouse fixture 调 destroy_active_buffers()
```

#### 4.3.3 源码精读

**(a) KernelCase：一组配置 = 一个可复现实验。**

[tests/kernel_test_utils.py:24-41](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/kernel_test_utils.py#L24-L41) —— frozen dataclass，字段 `S/K/epn/H/num_sms/B/token_padding/routing/bias_ratio/seed/min_R/max_R`，方法 `E(R) = R * epn`（专家总数 = rank 数 × 每 rank 专家数）。配合 [L44-45](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/kernel_test_utils.py#L44-L45) 的 `case_params` 把每个 case 变成带 id 的 pytest 参数——测试报告里失败用例直接以 case 名标注。

**(b) init_case 与 Buffer 登记。**

[tests/kernel_test_utils.py:48-65](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/kernel_test_utils.py#L48-L65) —— 构造 `Buffer(...)` 后立刻 `ctx["_buffer"] = buffer; _ACTIVE_BUFFERS.append(buffer)`。这份登记表被 [L68-72](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/kernel_test_utils.py#L68-L72) 的 `destroy_active_buffers` 消费，而后者由 conftest 的 autouse fixture 在每个测试后调用（[tests/conftest.py:8-13](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/conftest.py#L8-L13)）——这就是 u1-l2 讲过的「进程级 VMM 资源必须显式释放」的落地机制。

**(c) make_topk：六种路由模式。**

[tests/kernel_test_utils.py:82-115](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/kernel_test_utils.py#L82-L115) —— `balanced`/`biased` 走随机生成（后者叠加 bias_ratio）；其余四种是确定性极端用例：`all_local`（全部路由到本 rank 专家）、`all_remote`（全部路由到下一 rank）、`single_expert`（全体 token 只选 0 号专家）、`duplicate_topk`（同一 token 的 K 项全落同一个远程专家——专门打**去重路径**的极端负载）。

**(d) 多 rank 断言与「语义等价」。**

[tests/kernel_test_utils.py:124-130](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/kernel_test_utils.py#L124-L130) —— `assert_all_ranks`：把本 rank 的 ok 标志 all_gather 起来，任何一个 rank 失败就让全体报错（分布式测试不能只看 rank0）。另一个关键助手 [dedup_plan_semantic_errors，L239-284](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/kernel_test_utils.py#L239-L284)：去重结构由 atomicAdd 构建、**顺序不稳定**，所以比较的是「组集合」而非逐元素相等（L169-173 的注释解释了原因）。这个设计在 u6-l4 测试方法论里是主角。

#### 4.3.4 代码实践

**实践 C（无需 GPU）：KernelCase ↔ Buffer 参数对照。**

1. **实践目标**：用 ast 同时解析 `KernelCase` 字段与 `Buffer.__init__` 形参，验证「测试配置直接映射到构造参数」。
2. **操作步骤**：存为 `case_param_map.py`（示例代码）并运行：

   ```python
   # 示例代码：对照 KernelCase 注解字段与 Buffer.__init__ 形参
   import ast
   from pathlib import Path

   def class_fields(tree, cls):
       node = next(n for n in tree.body
                   if isinstance(n, ast.ClassDef) and n.name == cls)
       fields = []
       for stmt in node.body:
           if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
               fields.append(stmt.target.id)
       return node, fields

   case_tree = ast.parse(Path("tests/kernel_test_utils.py").read_text())
   _, case_fields = class_fields(case_tree, "KernelCase")

   api_tree = ast.parse(Path("moonep/api.py").read_text())
   buf_cls, _ = class_fields(api_tree, "Buffer")
   init = next(n for n in buf_cls.body
               if isinstance(n, ast.FunctionDef) and n.name == "__init__")
   buf_params = [a.arg for a in init.args.args[1:]]   # 去掉 self

   print("KernelCase 字段 :", case_fields)
   print("Buffer 形参     :", buf_params)
   print("同名交集        :", sorted(set(case_fields) & set(buf_params)))
   ```

3. **需要观察的现象**：同名交集非空，且 `epn` 通过 `case.E(R) = R*epn` 间接对应 `E`、`min_R/max_R` 只服务于 skip 判断、`routing/bias_ratio/seed` 只服务于造输入。
4. **预期结果**：交集 = `['B', 'H', 'K', 'S', 'num_sms', 'token_padding']`（6 个）。若多出或少了字段，说明版本与 `2bd860b` 不一致。
5. 纯文本解析，任何机器可复现；无需「待本地验证」标注。

#### 4.3.5 小练习与答案

**练习 1**：六个 `test_*.py` 与 `moonep/` 的内核模块是怎么对应的？
**答案**：一一对应加一个总装测试——`test_planning.py`↔`planning.py`、`test_dispatch.py`↔`dispatch.py`+`dispatch_epilogue.py`、`test_combine.py`↔`combine.py`+`combine_prologue.py`、`test_prefetch.py`↔`prefetch.py`、`test_grad_reduce.py`↔`grad_reduce.py`、`test_e2e.py`↔`api.py` 的 `Buffer` 公共 API。

**练习 2**：`test_e2e.py` 为什么 import `create_nvl_single_owner_tensor` 和 `pad_dim0_for_alignment`（[test_e2e.py:14-15](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/test_e2e.py#L14-L15)），而不是只靠 `Buffer`？
**答案**：e2e 测试要自己构造「分布在各 rank 的专家权重池」（`make_remote_expert`，[test_e2e.py:41-60](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/test_e2e.py#L41-L60)），这类权重缓冲不由 `Buffer` 持有，得直接用 `buffer.py` 的分配工具——这正是 `buffer.py` 把 `create_nvl_*` 设计成公开函数的原因。

**练习 3**：为什么 `assert_all_ranks` 要把失败信息区分为「本 rank 失败」和「别的 rank 失败」两种？
**答案**：分布式对拍时只有本 rank 持有详细 diff（细节在各自 GPU/CPU 上）；all_gather 只同步 ok 标志。若本 rank 失败就打印本地 detail，若 ok 但别人失败则报告「failed on another rank」——避免为了搬细节做昂贵的跨 rank 通信。

### 4.4 benchmarks/：基准脚本

#### 4.4.1 概念说明

四个基准脚本回答四个不同的问题：

| 脚本 | 回答的问题 |
| --- | --- |
| `bench_comm.py` | 每个通信算子本身多快？（算子级扫描：H × K × EP × bias_ratio） |
| `bench_vs_deepep.py` | 和 DeepEP v2 elastic 路径比，端到端谁快、快多少？ |
| `bench_prefetch.py` | 预取内核单独的带宽是多少？ |
| `bench_grad_reduce.py` | 梯度归约单独的带宽是多少？ |

#### 4.4.2 核心流程

`bench_comm.py` 的工作方式（其余脚本同构）：

```text
setup() 初始化 NCCL 进程组
  └─ 按 EP_SIZES=[4,8] 预建子进程组（world 前 ep 个 rank 组成一个 EP 组）
for 每个配置 (ep, H, K, S, E, bias_ratio):
    Buffer(...)            ← 只为拿 ctx（通信缓冲与规划输出）
    generate_topk_routing  ← 造路由（与测试同一生成器！）
    launch_planning        ← 先规划一次，拿到 dst / experts_to_copy / dedup 结构
    time_gpu_op(各种 launch_*)  ← 对每个算子单独计时（默认 CUDA Graph 捕获重放）
    统计 GBps = 字节数 / 时间，按各算子的口径分别计算
```

关键设计：**基准绕过 `Buffer.dispatch` 等高层方法，直接计时 `launch_*` 内核函数**——这样才能把 planning、dispatch、epilogue、prologue、combine、prefetch、grad_reduce 各自的成本分开度量。

#### 4.4.3 源码精读

**(a) 模块文档字符串 = 一份度量说明书。**

[benchmarks/bench_comm.py:1-65](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/benchmarks/bench_comm.py#L1-L65) —— 前 65 行完整写明：扫描维度（hidden 3584/7168、topk 8/16、ep 4/8、bias_ratio 0.1/1/5）、计时的 11 类算子（planning、dispatch_fwd/bwd、epilogue_fwd/bwd、combine_prologue_fwd/bwd、combine_fwd/bwd、prefetch、grad_reduce）、每个算子的**字节计数口径**（例如 epilogue = (组数+重复数)×H×2，prefetch 按瓶颈 rank 的 `max(max_send, max_recv)×H×Hp`），以及 `MOONEP_NUM_SMS_DEDUP` 环境变量。读基准先读这段，是所有性能结论的「单位」。

**(b) import 块：基准直接调内核的证据。**

[benchmarks/bench_comm.py:78-89](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/benchmarks/bench_comm.py#L78-L89) —— 依次 import `Buffer`（只为构造与 ctx）、`buffer.py` 的两个内存工具、以及 8 个 `launch_*`（dispatch、dispatch_epilogue、combine、combine_prologue、grad_reduce、inter_rank_sync、planning、prefetch）。与 `api.py` 的 import 块对照着读：**基准测的正是 api.py 编排的原料**。

**(c) 计时内核 `time_gpu_op`。**

[benchmarks/bench_comm.py:120-165](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/benchmarks/bench_comm.py#L120-L165) —— 默认 `cudagraph=True`：把 `iters` 次 launch 捕获进 CUDA Graph 再计时重放，剥掉 Python 侧每次 launch 的开销（JIT 编排 + make_ptr），让计时只反映内核本身；各 rank 自测耗时后 all_gather 取平均。`--no-cudagraph` 可退回 eager 循环（含宿主开销口径）。

**(d) prefetch 的特殊处理。**

[benchmarks/bench_comm.py:348-356](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/benchmarks/bench_comm.py#L348-L356) —— 注释说明：prefetch 内核自身没有跨 rank 同步，所以每次计时迭代都追加一个 `launch_inter_rank_sync`，防止各 rank 的迭代错开、把串行化的功劳/代价算错。这恰好呼应 4.2 的模块表：`inter_rank_sync.py` 是个「独立小工具内核」。

#### 4.4.4 代码实践

**实践 D（无需 GPU）：算子清单与 import 对照。**

1. **实践目标**：从 `bench_comm.py` 的 import 块出发，手工建立「计时算子 → 内核模块 → api.py 编排位置」三列对照表，验证「基准 = 编排的拆解」这一论断。
2. **操作步骤**：
   1. 运行 `grep -n "^from moonep" benchmarks/bench_comm.py`，抄下 8 个 `launch_*` 与 2 个 `buffer` 工具。
   2. 打开 [bench_one 函数，L168-454](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/benchmarks/bench_comm.py#L168-L454)，找到每个 `launch_*` 被包进哪个 `_xxx_call` 闭包、闭包结果存进哪个 `*_us` 字段。
   3. 与本讲 4.2.2 的两个编排函数（api.py L617-661、L663-696）对照，标注每个算子在编排中的位置。
3. **需要观察的现象**：`_dispatch_fwd_call`（L220-221）对应编排里的「planning 可选 + dispatch + epilogue」三步，但基准把 planning 单独拆出来计时（L200-203），dispatch_fwd 只含 dispatch+builder 的时间。
4. **预期结果**：得到一张 11 行的表（11 类计时算子），每行能落到唯一模块；其中 `planning_us` 的调用者出现在 `_plan_call` 与 L210 的预热两处。
5. 纯静态阅读 + grep，无需 GPU；无需「待本地验证」标注。

#### 4.4.5 小练习与答案

**练习 1**：`bench_comm.py` 的 bias_ratio 为什么扫 0.1 / 1.0 / 5.0 三档？
**答案**：见 [bench_comm.py:9-12](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/benchmarks/bench_comm.py#L9-L12)——它是 lognormal 专家热度分布的 σ：0.1 近似均衡、1 是典型 dropless-MoE 偏斜、5 近乎退化。三档覆盖「正常—典型—病态」的路由不均衡谱系。

**练习 2**：为什么 `bench_prefetch.py` 不需要 import `Buffer`（[bench_prefetch.py:15-16](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/benchmarks/bench_prefetch.py#L15-L16) 只有两个 import），而 `bench_grad_reduce.py` 需要（[bench_grad_reduce.py:15](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/benchmarks/bench_grad_reduce.py#L15-L15)）？
**答案**：prefetch 只需要「各 rank 自有的权重池 + 本地预取缓冲」，用 `create_nvl_single_owner_tensor` 即可；grad_reduce 需要 `Buffer` 的 `meta_buf`/`grid_sync_bar`（launch_grad_reduce 的参数里有 `meta_buf=ctx['meta_buf']`，见 [bench_comm.py:358-366](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/benchmarks/bench_comm.py#L358-L366)），因为它的跨 rank 屏障建立在 Buffer 拥有的共享 meta 缓冲上。

**练习 3**：`time_gpu_op` 里 warmup 之后为什么先 `torch.cuda.synchronize()` 再 `dist.barrier()`？
**答案**：先把本 rank 的 warmup 收尾干净，再用集合通信屏障对齐所有 rank 的起点——否则某 rank 还在跑 warmup 时另一 rank 已经开始计时，跨 rank 平均就失真。

### 4.5 模块依赖图：验证 api.py 是唯一汇聚点

#### 4.5.1 概念说明

前面三节反复出现一个论断：「api.py 汇聚一切」。现在把它变成可检验的命题。把 `moonep/` 内部 import 画成有向图后，我们预期：

- `api` 的出度是 10（import 了除 `_common`、`__init__` 之外的全部兄弟模块），且是**唯一**的高出度节点；
- 其余模块出度 ≤ 3：内核模块只依赖 `_common`、`constants`、`planning`；
- `buffer` 依赖的 `moonep._C` 是编译产物（`.so`），不在 `.py` 集合里，是**跨层的边**。

为什么值得验证？因为「汇聚点」决定了改动的波及面：改 `launch_dispatch` 的签名会同时影响 `api.py` 与 `bench_comm.py`；而改 `prefetch.py` 内部实现不影响任何 moonep 模块。依赖图就是**变更影响半径表**。

#### 4.5.2 核心流程

预期得到的邻接表（`A -> B` 表示 A import B）：

```text
api               -> buffer, combine, combine_prologue, constants,
                     dispatch, dispatch_epilogue, grad_reduce,
                     inter_rank_sync, planning, prefetch     （出度 10）
dispatch          -> _common, constants, planning            （出度 3）
__init__          -> api, planning                           （出度 2）
planning          -> _common, constants                      （出度 2）
dispatch_epilogue -> _common, planning                       （出度 2）
combine_prologue  -> _common, planning                       （出度 2）
grad_reduce       -> _common, planning                       （出度 2）
combine           -> _common                                 （出度 1）
inter_rank_sync   -> _common                                 （出度 1）
buffer            -> （无 .py 内部依赖；依赖编译产物 moonep._C）
_common / constants / prefetch -> （无内部依赖）
```

用文字画成层次图（上层依赖下层）：

```text
            __init__（门面）
                │
              api ──────────────┐（编排全部内核 + buffer）
   ┌──────┬─────┼──────┬────────┼─────────┐
planning dispatch combine ...  prefetch  buffer ──> moonep._C（C++ 扩展）
   └──┬─────┴─────┴──┘                    （跨层单点）
      _common   constants
```

#### 4.5.3 源码精读

本节的「源码」就是三份 import 清单，建议亲手打开对照：

- [moonep/api.py:57-72](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L57-L72) —— 汇聚点：10 个内部 import，全仓库唯一同时出现 8 个 `launch_*` 的地方。
- [moonep/dispatch.py:27-43](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/dispatch.py#L27-L43) —— 内核模块的典型依赖三件套：`_common`（TMA/原子原语，8 个符号）、`constants`（`KIDX_BITS`、`DEDUP_BUILDER_WARPS`）、`planning`（`MoonEPCommPlan` 类型 + 复用 `warp_inclusive_scan`）。
- [moonep/buffer.py:10-23](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/buffer.py#L10-L23) —— 跨层的边：`moonep._C` 的 12 个符号。

#### 4.5.4 代码实践（本讲主实践）

**实践 E（无需 GPU）：import 邻接表扫描器 + 汇聚点验证。**

1. **实践目标**：写脚本用 ast 扫描 `moonep/` 全部 `.py` 的内部 import，输出邻接表与出度排名，并用断言验证「api 是唯一汇聚点」。
2. **操作步骤**：把下面的脚本存为 `scan_moonep_deps.py`（示例代码），在仓库根目录运行：

   ```python
   # 示例代码：扫描 moonep/ 内部 import 依赖，输出邻接表并验证汇聚点
   import ast
   from pathlib import Path

   PKG = "moonep"
   files = sorted(Path(PKG).glob("*.py"))
   names = {f.stem for f in files}
   adj = {f.stem: set() for f in files}
   ext_users = []                      # 直接 import moonep._C 的模块

   for f in files:
       for node in ast.walk(ast.parse(f.read_text())):
           if not isinstance(node, ast.ImportFrom) or node.module is None:
               continue
           if node.level >= 1:                 # 相对导入 from .xxx import ...
               target = node.module.split(".")[0]
           elif node.module.split(".")[0] == PKG:   # 绝对导入 from moonep.xxx ...
               parts = node.module.split(".")
               target = parts[1] if len(parts) > 1 else None
           else:
               continue
           if target == "_C":
               ext_users.append(f.stem)
           if target in names and target != f.stem:
               adj[f.stem].add(target)

   print("== 邻接表（按出度降序） ==")
   for m in sorted(adj, key=lambda k: (-len(adj[k]), k)):
       deps = sorted(adj[m]) or ["(无内部依赖)"]
       print(f"{m:18s} -> {', '.join(deps)}   (出度 {len(adj[m])})")

   print("\n直接依赖 C++ 扩展 moonep._C 的模块:", sorted(set(ext_users)))

   (hub, deg), = sorted(adj.items(), key=lambda kv: -len(kv[1]))[:1]
   others = sorted((len(v) for k, v in adj.items() if k != hub), reverse=True)
   print(f"\n汇聚点: {hub}（出度 {deg}），其余模块最大出度 {others[0]}")
   assert hub == "api" and deg >= 10 and others[0] <= 3, "依赖结构与本讲论断不符！"
   launchers = {m for m in names if m != "api"}
   assert launchers - adj["api"] <= {"_common", "__init__"}, "有内核模块未被 api 汇聚"
   print("验证通过：api.py 汇聚了全部子内核模块（除纯工具 _common）。")
   ```

3. **需要观察的现象**：
   - 邻接表与 4.5.2 的预期完全一致；`api` 出度 10 断层领先，第二名是 `dispatch`（出度 3）。
   - `moonep._C` 的直接依赖者只有 `buffer` 一个。
   - `from .buffer import ...`（相对导入，api.py 用这种写法）与 `from moonep._common import ...`（绝对导入，内核模块用这种写法）两种风格都被正确解析——ast 里相对导入的 `module` 不含包名，这是新手常踩的坑。
4. **预期结果**：脚本以「验证通过」结束。若断言失败，先检查你是否在仓库根目录运行、HEAD 是否为 `2bd860b`。
5. 纯 ast 文本解析，不 import moonep，不需要 GPU/构建；无需「待本地验证」标注。

#### 4.5.5 小练习与答案

**练习 1**：出度第二名的模块为什么需要恰好 3 个依赖？
**答案**：`dispatch.py`——它是唯一同时做「数据搬运（要 `_common` 的 TMA/原子原语）+ 去重构建（要 `constants` 的 `KIDX_BITS`/`DEDUP_BUILDER_WARPS` 位宽约定）+ 消费规划结果（要 `planning` 的 `MoonEPCommPlan` 与 `warp_inclusive_scan`）」的内核，见 [dispatch.py:27-43](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/dispatch.py#L27-L43)。

**练习 2**：假设你要新增一个内核模块 `moonep/foo.py`（带 `launch_foo`），按本讲的依赖约定，最少要改哪两个文件？
**答案**：`moonep/foo.py` 本身，以及 `moonep/api.py`——在 L57-72 的 import 区加 `from .foo import launch_foo` 并在某个 `Buffer` 方法里编排它。其余模块不受影响（除非 foo 需要 `_common`/`constants`，那也是 import 而非修改）。

**练习 3**：`__init__.py` 的出度是 2（api、planning），它为什么也要 import `planning`，而不是只经 `api` 转手？
**答案**：`MoonEPCommPlan` 定义在 `planning.py`（类型本体），`Buffer` 只是返回它。用户侧 `from moonep import MoonEPCommPlan` 做类型标注、`plan.clone()` 保存快照（u1-l1 讲过 plan 要跨 fwd/bwd 存活）都需要直接拿到这个类，所以门面同时再导出它。

## 5. 综合实践

把四个小实践串成一个产出物——**生成你自己的《MoonEP 代码速查手册》**：

1. **任务**：写一个脚本 `make_code_map.py`（示例代码），对仓库做三件事并汇总输出成一个 Markdown 文件（保存在仓库**外**或你的笔记目录，不要写进仓库）：
   - 目录清点（实践 A 的行数表）；
   - 模块职责（实践 B 的文档字符串首行 + 入口行号表）；
   - 依赖邻接表（实践 E 的输出）。
2. **加深一步**：在生成的手册里给每个文件加一列「对应讲义」，按本讲 4.1.3 (c) 的体量表类推——`buffer.py` → u2 单元（对称内存）、`planning.py` → u3、`dispatch*.py`/`combine*.py` → u4、`prefetch.py`/`grad_reduce.py` → u5、`api.py` 的异步与零拷贝 → u6-l1/l2、`tests/planning_reference.py` → u6-l4。
3. **检验**：用这份手册做两个自测——(a) 随机挑三个关键词（如「零填充」「fabric 句柄」「MXFP4」），先用 `Grep` 在仓库里定位，再对照手册看你的预计位置是否正确；(b) 合上手册，默写 api.py 的 10 个内部依赖模块名，然后跑实践 E 的脚本核对。
4. **预期结果**：一份 100 行以内的个人速查表 + 两个自测全对。此后阅读 u2-u6 任何一讲时，遇到文件名都能在 10 秒内回忆起它的角色与邻居。

## 6. 本讲小结

- 仓库分三层：Python 内核层 `moonep/`（CuTe DSL、JIT）、C++ 绑定层 `csrc/`（编译成 `moonep._C`）、验证层 `tests/` + `benchmarks/`；公共 API 只有 `Buffer` 与 `MoonEPCommPlan` 两个名字。
- `moonep/` 13 个文件按角色分五组：门面（`__init__`/`api`）、内存基础设施（`buffer`）、共享工具（`_common`/`constants`）、规划器（`planning`）、七个通信内核（各带一个 `launch_*` 入口）。
- `buffer.py` 是 `moonep._C` 的唯一消费者——C++ 层与 Python 层之间是单点接口；`constants.py` 是跨模块数字约定的唯一权威。
- 验证层的方法论：每个内核配 PyTorch 参考实现，`KernelCase` 参数化配置，`assert_all_ranks` 保证任一 rank 失败全体报错，去重结构因 atomicAdd 顺序不稳定只能比较集合语义。
- 基准直接计时 `launch_*` 而非 `Buffer` 高层方法，从而把 11 类算子的成本拆开度量；`bench_comm.py` 头部 65 行文档字符串是所有性能口径的说明书。
- 实践 E 用 ast 扫描验证了依赖结构：`api.py` 出度 10、唯一汇聚点；其余模块出度 ≤ 3；这张图就是改动的影响半径表。

## 7. 下一步学习建议

下一讲 **u1-l4「Buffer API 快速上手」**将第一次真正运行代码：参照 `tests/test_e2e.py` 构造 `Buffer`，完成一次 dispatch → prefetch_weight → combine 的最小调用序列，把本讲的「静态地图」变成「动态经验」。

在进入 u1-l4 之前，建议做两件轻量预习：

1. 重读 [moonep/api.py:1-48](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L1-L48) 的模块文档字符串——它给了一段完整的 fwd/bwd 使用示例，u1-l4 的实践脚本将以它为蓝本。
2. 浏览 `tests/test_e2e.py` 全文（422 行），留意 `make_inputs` 与 `make_remote_expert` 如何准备数据——这是下一讲实践的参照系。

u2 单元起将沿本讲的地图深入内存基础设施：`buffer.py` 的对称内存（u2-l2）、fabric 句柄（u2-l3）、组播与 meta 布局（u2-l4）。
