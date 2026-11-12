# u1-l4 仓库结构与阅读地图

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 `include/`、`src/`、`python/`、`test/`、`benchmark/`、`tools/`、`docs/` 每个目录的职责，以及每个目录下代表性文件的作用。
2. 解释 `src/a2a_plan.cc` 为什么刻意保持「无 CUDA、无 torch、无通信库」，以及这一分层决策带来的两个直接好处（无 GPU 可测布局、Meta 内核复用同一份形状规则）。
3. 建立从 `group.all_to_all_4d(x)` 出发，经 `src/bindings.cc`、`src/group.cc`、`src/a2a_plan.cc`，到达 `src/transfer.cu` / `src/barrier.cu` 的完整调用链骨架，并在每个节点标注文件与函数名。
4. 知道开发工具链的入口在哪：pre-commit（ruff + clang-format）、pytest 分层标记、setup.py → CMake → nvcc 的构建链条、`tools/` 下的发布三件套。

本讲是「地图课」：不深入任何一层的机制细节（那是 u3–u9 的事），但要让你之后阅读任何一篇讲义时，都知道代码在仓库的哪个角落、沿着哪条链走。

## 2. 前置知识

本讲需要的前置概念在前三讲已经建立，这里只做一句话回顾，另补两个新术语：

- **torch 自定义算子扩展**：本仓库的 C++/CUDA 代码最终编译成一个名为 `_C` 的共享库（`.so`），Python 侧 `import fast_ulysses` 时通过 `from . import _C` 触发加载，算子在加载时注册进 torch 的 dispatcher（u1-l1、u1-l3）。
- **mode 0 / mode 1**：Ulysses 两次 all-to-all 的方向——mode 0 用序列分片换头分片，mode 1 是其逆（u1-l2）。
- **翻译单元（Translation Unit, TU）**：C++ 里一个 `.cc` / `.cu` 源文件加上它包含的头文件，经编译器单独编译成一个目标文件。说「a2a_plan.cc 这个 TU 无 CUDA、无 torch」，指的是它 `#include` 的所有头里都没有 torch 或 CUDA 的东西——因此它可以在任何有 C++ 编译器的环境里编译和测试。
- **torchbind 自定义类**：通过 `TORCH_LIBRARY` 的 `m.class_<...>()` 注册、可以在 Python 里以 `torch.classes.命名空间.类名` 形式持有的 C++ 对象。本项目的 `UlyssesGroup`（C++ 侧的有状态对象）就是这样暴露给 Python 的。

## 3. 本讲源码地图

| 文件 | 在本讲中的角色 |
|---|---|
| [docs/develop.md](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/develop.md) | 官方开发文档：目录树、lint、测试分层、发布流程 |
| [CMakeLists.txt](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/CMakeLists.txt) | 把 `src/*.cc` + `src/*.cu` 编译成 `_C` 扩展的 CMake 脚本 |
| [pyproject.toml](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/pyproject.toml) | 包元数据：包目录在 `python/` 下、CLI 入口、pytest 标记、ruff 配置 |
| [setup.py](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/setup.py) | setuptools 入口：调用 cmake 完成 `_C` 的编译 |
| [src/a2a_plan.cc](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/a2a_plan.cc) | 纯主机寻址层：把 4D all-to-all 翻译成一组带步长的字节拷贝 |
| [include/fast_ulysses/a2a_plan.hpp](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/include/fast_ulysses/a2a_plan.hpp) | `A2ADims` / `CopyOp` / `A2APlan` 三个数据结构的定义与契约 |
| [python/fast_ulysses/group.py](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/python/fast_ulysses/group.py) | Python API 门面：构造、两个集合调用、输出分配器 |
| [src/bindings.cc](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc) | torch 算子层：校验、autograd、Meta 内核、op 注册 |
| [src/group.cc](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/group.cc) | 有状态层：对称内存窗口、plan 缓存、暂存池 |
| [src/transfer.cu](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/transfer.cu) | 设备侧传输：把 `CopyOp` 发射为 `cudaMemcpy2D/3DAsync` |
| [src/barrier.cu](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/barrier.cu) | 设备侧屏障：单块自旋内核发布/等待标志位 |
| [src/plan_bindings.cc](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/plan_bindings.cc) | 纯主机 plan 与 torch 唯一的「相遇点」：`a2a_plan_debug` 算子 |

## 4. 核心概念与源码讲解

### 4.1 目录职责：一张仓库地图

#### 4.1.1 概念说明

fast-ulysses 是一个「小而深」的仓库：不含文档与测试，核心代码只有约 10 个 C++/CUDA 文件加一个 Python 门面。目录划分遵循一条清晰的分层原则——**谁依赖 torch，谁依赖 CUDA，谁两者都不依赖**：

- `python/` 只做薄封装，几乎所有跨调用存活的状态（对称内存窗口、plan 缓存、暂存缓冲）都在 C++ 侧的 `UlyssesGroup` 对象里；
- `src/` 里再分三层：torch 算子层（`bindings.cc`）、有状态层（`group.cc`）、纯算术层（`a2a_plan.cc`）与设备层（`transfer.cu`、`barrier.cu`）；
- `include/fast_ulysses/` 是各层对外的契约（带 Doxygen 注释），同层或下层实现 `.cc` 与之对应。

#### 4.1.2 核心流程

一次阅读仓库时，目录间的依赖方向是：

```
python/fast_ulysses/            group.py、cli.py、_diagnose.py（API 门面 + 工具）
        │  torch.ops / torch.classes
        ▼
src/bindings.cc                 torch 算子层：校验 → 屏障 → 传输 → 屏障 → copy_out
src/plan_bindings.cc            （旁路）纯主机 plan 的调试出口
        │
        ▼
src/group.cc                    有状态层：窗口分配、plan 缓存、staging 池
        │                                │
        ▼                                ▼
src/a2a_plan.cc                src/transfer.cu + src/barrier.cu
（纯主机寻址，无 CUDA 无 torch）  （设备侧，不知道 peer 地址从哪来）
```

支撑目录不参与这条链：`test/`（四层测试）、`benchmark/`（基准）、`tools/`（构建与发布脚本）、`docs/`（文档）、`examples/`（示例）。

#### 4.1.3 源码精读

官方文档 [docs/develop.md:L13-L28](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/develop.md#L13-L28) 给出了目录树，并特别强调了一条分层纪律：**`src/a2a_plan.cc` 不含 CUDA、不含 torch、不含通信库，请保持这样——这正是布局契约可以脱离 GPU 测试的原因**（4.2 节详解）。

结合实际文件清单（用 `git ls-files` 或文件树浏览可得），完整的目录职责表如下：

| 目录 / 根文件 | 职责 | 代表文件 |
|---|---|---|
| `include/fast_ulysses/` | 公共头文件，数据结构与契约的 Doxygen 文档 | `a2a_plan.hpp`（寻址结构）、`group.hpp`（组与窗口）、`transfer.hpp`（设备接口）、`common.hpp`（错误检查宏与 `Event` RAII）、`work.hpp`、`nvlink.hpp` |
| `src/` | 全部 C++/CUDA 实现，编译成唯一的扩展 `_C` | `a2a_plan.cc`、`group.cc`、`bindings.cc`、`plan_bindings.cc`、`transfer.cu`、`barrier.cu`、`nvlink.cc`、`work.cc` |
| `python/fast_ulysses/` | Python 门面与工具 | `group.py`（API surface）、`cli.py`（`fast-ulysses doctor` / `wheel-url`）、`_diagnose.py`（导入失败归因）、`wheel_matrix.json` |
| `test/` | host-only 测试 + torchrun worker | `test_plan.py`（无 GPU 布局测试）、`test_distributed.py`（包装器）、`test_wheel_matrix.py`、`distributed/*.py`（8 个 worker） |
| `benchmark/` | 基准七模式 | `bench_a2a.py`、`collect.sh` |
| `tools/` | 构建与发布工程 | `exclusive.sh`（GPU 独占）、`build_wheels.sh`、`check_wheel.py`、`preflight_gpu.sh`、`make_index.py` |
| `docs/` | 全部文档 | `install.md`、`quickstart.md`、`api.md`、`design.md`、`develop.md`、`benchmark.md`、`migration.md` |
| `examples/` | 可运行示例 | `dit_attention.py`（u2-l4 精读） |
| 根目录 | 构建入口与项目元数据 | `setup.py`、`CMakeLists.txt`、`pyproject.toml`、`VERSION`、`.pre-commit-config.yaml`、`.clang-format` |

两个容易忽略的根文件值得点名：

- [pyproject.toml:L29-L34](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/pyproject.toml#L29-L34) 声明包目录在 `python/` 下（`package-dir = {"" = "python"}`），并把 `wheel_matrix.json`、`py.typed` 作为包数据打包——这就是为什么源码树里包不在常见的 `src/` 或根目录下。
- [pyproject.toml:L20-L21](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/pyproject.toml#L20-L21) 注册了命令行入口 `fast-ulysses = "fast_ulysses.cli:main"`，u1-l3 用过的 `fast-ulysses doctor` / `wheel-url` 由此而来。

加载侧的入口在 [python/fast_ulysses/__init__.py:L3-L14](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/python/fast_ulysses/__init__.py#L3-L14)：先 `import torch`（让 libtorch 先于 `_C` 的 dlopen 加载），再 `from . import _C` 触发 `TORCH_LIBRARY` 注册；若失败则交给 `_diagnose.explain` 归因。这个文件是「Python 世界与 C++ 世界缝合处」，只有十几行，值得整读。

#### 4.1.4 代码实践

**实践目标**：不借助本讲义，独立建立目录→职责的映射，并验证「`src/` 下所有源文件编进同一个扩展」这一事实。

**操作步骤**：

1. 在仓库根目录执行 `git ls-files`（或浏览文件树），把输出按首段目录分组，数一数每组文件数。
2. 打开 [CMakeLists.txt:L126-L129](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/CMakeLists.txt#L126-L129)，确认 `file(GLOB SOURCES "src/*.cc" "src/*.cu")` 把 `src/` 下全部 8 个源文件收进唯一的共享库目标 `_C`——仓库里没有第二个二进制目标。
3. 对照上表，给 `git ls-files` 输出里你还不认识的每个文件写一句话职责（猜不出来就打开文件看头三行注释——本仓库几乎所有文件都以一段「为什么」的注释开头）。

**需要观察的现象**：`src/` 恰好 8 个文件（6 个 `.cc` + 2 个 `.cu`）；`include/fast_ulysses/` 恰好 6 个 `.hpp`；`test/distributed/` 有 8 个 worker 脚本，与 [docs/develop.md:L65-L74](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/develop.md#L65-L74) 的表格逐行对应。

**预期结果**：你手里有了一张自己的文件清单，之后任何讲义提到某个文件，你都能立刻定位它属于哪一层。

#### 4.1.5 小练习与答案

**练习 1**：`src/nvlink.cc` 和 `src/work.cc` 在 4.1.2 的依赖方向图里没有出现。它们挂在哪条链上？

<details><summary>参考答案</summary>

两者都是旁路：`nvlink.cc` 实现构造期的 NVLink 探测（Python 侧 `_require_nvlink` 调 `_C.check_nvlink`，u2-l1 详述）；`work.cc` 实现异步调用的完成事件注册（`_C.register_stream_completion`，u7-l3 详述）。它们不参与一次 sync `all_to_all_4d` 的数据通路，所以不在主链上。
</details>

**练习 2**：为什么 `python/fast_ulysses/` 里几乎没有状态（窗口、plan 缓存都在 C++ 侧），而 `group.py` 的模块注释却说「留在这里的是 C++ 没有等价物的东西」？具体是哪三样？

<details><summary>参考答案</summary>

见 [python/fast_ulysses/group.py:L1-L6](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/python/fast_ulysses/group.py#L1-L6)：进程组的名字（torch Python 对象的概念）、comm 流（`torch.cuda.Stream` 的高优先级创建与上下文管理）、`AsyncCollectiveTensor`（torch 的 Python 包装子类）。凡是能活在 C++ 里的状态都放进了 `UlyssesGroup`，Python 只留必需。
</details>

### 4.2 无 CUDA、无 torch 的 plan 层

#### 4.2.1 概念说明

整个仓库最核心的分层决策是：**把「4D all-to-all 应该搬哪些字节」这件纯算术的事，单独放进 `src/a2a_plan.cc`，并且让它不依赖 CUDA、不依赖 torch、不依赖任何通信库**。

为什么值得这么做？回忆 u1-l1 的第二条特性「重排即步长」：本库不做 `permute` 重排内核，而是把序列/头的排列直接编码成每次拷贝的源/目的步长。于是「一次交换搬运得对不对」完全等价于「plan 里这组 `CopyOp` 算得对不对」——而这是一道纯整数算术题，不需要 GPU 也能验证。把这一层隔离干净，等于把**正确性里最大的一块**从「需要 8 张卡」降级成「需要一台能跑 Python + numpy 的机器」。

这一决策带来两个直接收益：

1. **无 GPU 的布局测试**：`test/test_plan.py` 用 numpy 字节缓冲逐条重放 `CopyOp`，与 `all_to_all_single + permute` 的参照做位精确对照——CI 没有 GPU 也能跑（[docs/develop.md:L50-L52](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/develop.md#L50-L52)），u3-l3 精读。
2. **Meta 内核零重复**：torch 的 FakeTensor/导出路径需要纯形状传播。本库的 Meta 内核直接调用 `group->plan()` 拿输出形状，而不是在 Python 里重写一遍不均匀分片的形状规则——[src/bindings.cc:L276-L280](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L276-L280) 的注释明说：在 Python 重写会让「test_plan.py 够不到的第二份副本」出现，**这正是 a2a_plan.cc 作为一个无 torch、无 CUDA 翻译单元存在的全部理由**。

#### 4.2.2 核心流程

plan 层的输入输出可以画成：

```
输入                          纯主机计算                       输出
A2ADims                       build_plan(dims, mode,           A2APlan
  b, d                          elem_size)                       output_shape   ← 本 rank 收到的形状
  rank, world_size                                                window_numel   ← 所有 rank 中最大的接收量
  seq_splits[ws]                                                  ops[]          ← 本 rank 发出的拷贝列表
  head_splits[ws]                                                   每个 CopyOp: peer, src/dst_offset,
                                                                        src/dst_pitch, width,
                                                                        rows, depth, src/dst_slice
```

`CopyOp` 的字段设计刻意做到「同一结构既描述一次 `cudaMemcpy2D/3DAsync`，也描述一次测试里的主机重放」——偏移都以字节计、相对各自缓冲的基址（[include/fast_ulysses/a2a_plan.hpp:L36-L51](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/include/fast_ulysses/a2a_plan.hpp#L36-L51)）。这就是「寻址的纯主机表达」：设备层只管发射，不算偏移。

#### 4.2.3 源码精读

**证据一：include 列表干净。** [src/a2a_plan.cc:L1-L6](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/a2a_plan.cc#L1-L6) 只包含自己的头与四个标准库头；而 [include/fast_ulysses/a2a_plan.hpp:L7-L8](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/include/fast_ulysses/a2a_plan.hpp#L7-L8) 只有 `<cstdint>` 和 `<vector>`。整条 include 链上没有 torch、没有 CUDA。

**证据二：头文件的自我声明。** [include/fast_ulysses/a2a_plan.hpp:L2-L6](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/include/fast_ulysses/a2a_plan.hpp#L2-L6) 开头注释写明设计意图：寻址用 pitched copy 表达、纯主机算术、无 CUDA 无 torch 无通信库，因此布局契约可以独立测试；并声明**不均匀分片是一般情况，偶均分只是特例**（`seq_splits = [s/P]*P`），所以只有一条代码路径需要写对。

**证据三：build_plan 的主体。** [src/a2a_plan.cc:L143-L216](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/a2a_plan.cc#L143-L216)：先 `validate()`，再算 `seq_total` / `head_total` 与两组前缀和，然后对每个 peer 发射一个 `CopyOp`——mode 0 分支「读跨步、写连续」，mode 1 分支「读连续、写跨步」。具体每条拷贝的推导是 u3-l2 的主题，本讲只需要认识到：**这里没有任何一行涉及设备、句柄或 torch 张量**。

**证据四：相遇点被隔离在一个独立 TU。** [src/plan_bindings.cc:L1-L4](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/plan_bindings.cc#L1-L4) 的注释点破：`a2a_plan.cc` 自己保持无 torch 无 CUDA，**这个文件是两者相遇的唯一地点**。它把 `build_plan` 包成调试算子 `a2a_plan_debug`，返回 `(output_shape, window_numel, ops 张量)`，注册在 [src/plan_bindings.cc:L57-L62](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/plan_bindings.cc#L57-L62)。

#### 4.2.4 代码实践

**实践目标**：亲眼确认 plan 层的「纯净」，并用调试算子看到一份真实的 plan。

**操作步骤**：

1. 打开 `src/a2a_plan.cc`，抄下它的 5 行 `#include`；再打开它 include 的头，确认递归下去也没有 torch/CUDA。
2. 若你已在 u1-l3 完成构建，运行以下脚本（不需要 GPU、不需要进程组；若尚未构建，标注「待本地验证」并只做纸面推演）：

```python
# 示例代码：打印 ws=4 下 rank 0 的 mode 0 plan
import torch
from fast_ulysses import _C  # 触发算子注册

shape, window_numel, ops = torch.ops.fast_ulysses.a2a_plan_debug(
    b=1, d=64, rank=0, world_size=4,
    seq_splits=[5, 3, 0, 7], head_splits=[10, 10, 10, 10],
    mode=0, elem_size=2,
)
print("output_shape:", shape)
print("window_numel:", window_numel)
print("ops:\n", ops)
```

3. 对照 [src/plan_bindings.cc:L14-L50](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/plan_bindings.cc#L14-L50)：`ops` 张量每行 10 列，列序正是注释里写的 `peer, src_off, dst_off, src_pitch, dst_pitch, width, rows, depth, src_slice, dst_slice`。

**需要观察的现象**：`seq_splits` 里有一个 0（rank 2 没有序列），`ops` 里却不会出现 `peer == 2` 的行——零长度分片直接不发射拷贝；`window_numel` 等于所有 rank 中最大接收量（这里是 rank 3 收 `b × 15 × 10 × 64` 个元素——全部序列拼起来 × rank 3 的头数）。请按 [src/a2a_plan.cc:L166-L173](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/a2a_plan.cc#L166-L173) 的循环手算一遍再比对。

**预期结果**：你能说出「plan 是纯函数：同样的 `A2ADims + mode + elem_size` 永远给出同样的 `ops`」，并且窗口尺寸为什么取最大（对端偏移只有在所有 rank 同尺寸时才对得齐，头文件 [include/fast_ulysses/a2a_plan.hpp:L54-L58](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/include/fast_ulysses/a2a_plan.hpp#L54-L58) 的注释给出了完整论证）。

#### 4.2.5 小练习与答案

**练习 1**：如果不小心在 `a2a_plan.cc` 里加了一行 `#include <torch/extension.h>`（比如为了用 `TORCH_CHECK` 做校验），会立刻坏掉什么？不会坏掉什么？

<details><summary>参考答案</summary>

立刻坏掉：`test_plan.py` 的前提是这个 TU 无 torch（其实测试经由 `plan_bindings.cc` 调用，真正坏掉的是「布局规则只此一份、且被 host-only 测试覆盖」的保证——一旦 TU 依赖 torch，任何想单独编译/复用它的人都被迫拖入整个 libtorch）。不会坏掉的：编译扩展本身（`_C` 本来就链着 torch）和运行时行为。防御性校验在该文件里是用 `std::invalid_argument`（[src/a2a_plan.cc:L12-L15](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/a2a_plan.cc#L12-L15) 的 `fail()`）实现的，torch 的 `TORCH_CHECK` 反而会引入依赖。
</details>

**练习 2**：`plan_bindings.cc` 的注释说「独立 TU 在这里是安全的，不像 bindings.cc 里的算子 schema」。为什么 `bindings.cc` 的注册不能这么随意？

<details><summary>参考答案</summary>

见 [src/plan_bindings.cc:L2-L4](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/plan_bindings.cc#L2-L4)：`bindings.cc` 的 schema 引用了自定义类（`__torch__.torch.classes.fast_ulysses.UlyssesGroup`），跨 TU 的静态初始化顺序会影响类定义是否先于引用它的 schema 就绪；而 `a2a_plan_debug` 的 schema 不含自定义类，所以没有顺序问题。这是一条 C++ 静态初始化顺序的实战教训。
</details>

**练习 3**：Meta 内核复用 `group->plan()` 而不在 Python 重写形状规则，除了「避免第二份副本」，还有一个更直接的安全理由，是什么？

<details><summary>参考答案</summary>

plan() 是纯主机代码、不在设备上分配任何东西（[src/bindings.cc:L277-L279](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L277-L279) 的注释），所以在 FakeTensor/Meta 这种「没有真设备」的路径里调用它是安全的；同时它在路上把形状、dtype、splits 的全部检查又跑了一遍。
</details>

### 4.3 调用链骨架：从 Python 到 CUDA 拷贝

#### 4.3.1 概念说明

现在把各层串起来。一次最普通的调用 `y = group.all_to_all_4d(x)`（sync 路径、`mode=0`、无 `out=`、无 splits）在仓库里要穿过 6 个文件。理解这条链是本讲的核心交付：之后 u4–u7 的每一讲，都是把链上某个节点放大。

调用链的五个阶段（bindings.cc 文件头注释 [src/bindings.cc:L1-L2](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L1-L2) 的一句话总结）：**校验张量 → 从组里取 plan 和窗口 → 屏障 → 传输 → 屏障 → copy out**。

#### 4.3.2 核心流程

```
y = group.all_to_all_4d(x, mode=0)                                  ← 用户代码
│
① python/fast_ulysses/group.py :: UlyssesGroup.all_to_all_4d
│    无 out= 分支直接转发：
│    torch.ops.fast_ulysses.all_to_all_4d(self._handle, x, mode, seq, head)
│    （_handle 是 C++ UlyssesGroup 的 torchbind 对象）
│        │  进入 torch dispatcher（Autograd key → 计算内核）
│        ▼
② src/bindings.cc :: all_to_all_4d_autograd → all_to_all_4d
│    require_cuda + CUDAGuard 后调 run(...)
│        ▼
③ src/bindings.cc :: run
│    ├─ prepare(...)                     ← 全部校验 + 取 plan/窗口（必须先于第一个屏障）
│    │    ├─ group->check_alive() / group->prune_owned()          [src/group.cc]
│    │    ├─ require_cuda / require_group_device
│    │    ├─ group->plan(sizes, mode, dtype, seq, head)           [src/group.cc]
│    │    │    ├─ make_dims(...)                                  [src/group.cc] 形状与 splits 交叉校验
│    │    │    └─ ulysses::build_plan(dims, mode, elem_size)      [src/a2a_plan.cc] ← 纯主机寻址
│    │    ├─ 输出三态：caller 的 out / at::empty 新分配
│    │    └─ group->window(kSyncWindow, dtype, window_numel)      [src/group.cc]
│    │         （首次或需增长时 → allocate()：对称内存分配 + rendezvous）
│    ├─ transfer_on_stream(...)
│    │    ├─ fast_barrier(stream, win->flag_ptrs, rank)           [src/barrier.cu]  开局屏障
│    │    ├─ launch_a2a_ce(x.data_ptr(), win->peer_ptrs, plan,    [src/transfer.cu]
│    │    │                  xfer_stream, rank, stream)
│    │    │    └─ 对 plan.ops 每个 CopyOp: issue_copy(...)       [src/transfer.cu]
│    │    │         → cudaMemcpy2DAsync / cudaMemcpy3DAsync
│    │    └─ fast_barrier(...)                                    [src/barrier.cu]  收尾屏障
│    └─ copy_out(call, stream, rank)      [src/bindings.cc]      copying 路径才有：
│         窗口 → 输出张量的一次平坦 D2D 拷贝
│        ▼
④ 返回 y（zero-copy 路径下 y 就是窗口本身，copy_out 被跳过）
```

三条贯穿性的设计约束（后面各讲反复引用）：

1. **校验全部先于第一个屏障**：被拒绝的参数不能让本 rank 停在屏障上等一个根本不会来的对端（[src/bindings.cc:L87-L88](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L87-L88)）。
2. **设备层不算偏移、不知道地址从哪来**：`launch_a2a_ce` 的偏移全部来自 `plan.ops`，peer 地址由调用者传入——这正是 transfer.hpp 头注释 [include/fast_ulysses/transfer.hpp:L1-L5](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/include/fast_ulysses/transfer.hpp#L1-L5) 说的「以此把所有通信库挡在这个翻译单元之外」。
3. **状态都在 C++ 组对象里**：plan 缓存（`std::map<PlanKey, A2APlan>`）、窗口表、暂存池都是 `UlyssesGroup` 的成员，Python 侧每次调用只是无状态转发。

#### 4.3.3 源码精读

**第①跳：Python 转发。** [python/fast_ulysses/group.py:L111-L132](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/python/fast_ulysses/group.py#L111-L132) 定义 `all_to_all_4d`；无 `out=` 时它只有一行实质调用——把 `_handle`（构造时经 [python/fast_ulysses/group.py:L87-L89](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/python/fast_ulysses/group.py#L87-L89) 创建的 `torch.classes.fast_ulysses.UlyssesGroup`）连同张量与参数交给注册算子。

**第②跳：算子注册与 autograd 入口。** 算子 schema 注册在 [src/bindings.cc:L718-L726](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L718-L726)：`all_to_all_4d` 注册了 `CompositeExplicitAutograd`（计算）、`Autograd`（[src/bindings.cc:L584-L605](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L584-L605)，处理反向与前向 AD）和 `Meta` 三个 key。无梯度需求时 autograd 入口直接走 [src/bindings.cc:L253-L270](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L253-L270) 的 `all_to_all_4d`，它取当前 CUDA 流后调 `run`。

**第③跳 run 的三段式。** [src/bindings.cc:L225-L241](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L225-L241)：`run = prepare + transfer_on_stream + (可选) copy_out`。

- `prepare`（[src/bindings.cc:L89-L174](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L89-L174)）：校验链 + 从组里取 plan 与窗口，并把「输入/输出与窗口的字节区间是否重叠」也查掉。
- `transfer_on_stream`（[src/bindings.cc:L194-L211](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L194-L211)）：开局屏障（写者等读者）→ `launch_a2a_ce` → 收尾屏障。
- `copy_out`（[src/bindings.cc:L216-L223](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L216-L223)）：一次 `cudaMemcpyAsync`（D2D），把窗口内容搬到调用者持有的输出张量。

**第③跳内：plan 的来路。** [src/group.cc:L167-L196](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/group.cc#L167-L196) 的 `UlyssesGroup::plan` 以 `(sizes, seq, head, mode, dtype)` 为缓存键，未命中时先经 [src/group.cc:L45-L136](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/group.cc#L45-L136) 的 `make_dims`（形状 4D、dtype 白名单、16 字节对齐、splits 交叉校验、偶均分特例），再调 `build_plan`（[src/group.cc:L191-L195](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/group.cc#L191-L195)）——从这里进入 4.2 的纯主机层。

**第③跳内：窗口的来路。** [src/group.cc:L255-L267](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/group.cc#L255-L267) 的 `window` 按 `(role, dtype)` 查缓存，尺寸不够则「先删旧再分配」，最终落到 [src/group.cc:L207-L253](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/group.cc#L207-L253) 的 `allocate`：torch 对称内存分配 + rendezvous 收集每个 peer 的地址 + 清零握手区（u4-l1 的主题）。

**第③跳底：两次设备调用。** 传输与屏障是一对：

- [src/transfer.cu:L68-L126](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/transfer.cu#L68-L126) `launch_a2a_ce`：peer 按 `rank ^ k` 异或序枚举（非 2 幂 world_size 再补一次扫描），远端拷贝串行发在 `xfer` 专用流上，本 rank 自份额发在调用者流上，最后用事件把两流汇合。每个 `CopyOp` 经 [src/transfer.cu:L26-L52](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/transfer.cu#L26-L52) 的 `issue_copy` 变成一次 `cudaMemcpy2DAsync`（`depth<=1`）或 `cudaMemcpy3DAsync`（batch 折叠）。
- [src/barrier.cu:L53-L67](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/barrier.cu#L53-L67) `fast_barrier`：`world_size==1` 直接返回（u1-l3 见过的「ws=1 跳过屏障」就在这行）；否则发射 [src/barrier.cu:L31-L49](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/barrier.cu#L31-L49) 的单块自旋内核 `barrier_kernel`，用内联 PTX 的 release/acquire 指令发布与等待 epoch（u5-l3 逐行精读）。

#### 4.3.4 代码实践

**实践目标**：亲手把 4.3.2 的骨架补成一张「每个节点带文件 + 函数名 + 行号」的调用链卡片，并用两个无 GPU 手段交叉验证其中两跳。

**操作步骤**：

1. 从 [python/fast_ulysses/group.py:L129-L132](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/python/fast_ulysses/group.py#L129-L132) 出发，用编辑器的「跳转到定义」或 Grep 依次追每一跳，把 4.3.2 的图抄录成自己的版本，并在每个节点补上行号。
2. 验证「plan 跳」：运行 4.2.4 的 `a2a_plan_debug` 脚本，确认它输出的 `ops` 与主链上 `group->plan()` 缓存的是同一份计算（同一个 `build_plan`）。
3. 验证「ws=1 跳过屏障」：读 [src/barrier.cu:L55-L57](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/barrier.cu#L55-L57)，确认 `ws == 1` 时 `fast_barrier` 直接 return——这解释了 u1-l3 里「world_size=1 时哪些行为仍然成立」的观察。
4. 在图中用两种颜色（或标记）区分**每次调用都要走的节点**（prepare、两个屏障、launch_a2a_ce）与**只有首次/增长时才走的节点**（`make_dims`+`build_plan` 未命中、`allocate`）。

**需要观察的现象**：plan 命中缓存时 `make_dims` / `build_plan` 完全不执行；窗口已存在且够大时 `allocate` 不执行。即稳态调用只剩「校验 + 两次屏障 + 拷贝发射 + copy_out」。

**预期结果**：一张你自己画的、带文件/函数/行号标注的调用链文本图（综合实践会把它变成正式产出）。

#### 4.3.5 小练习与答案

**练习 1**：链上哪一步是「集体操作」（所有 rank 必须同时到达）？哪一步不是？

<details><summary>参考答案</summary>

两个 `fast_barrier` 之间夹着的传输是集体的：屏障本身要求全组到达，所以 `launch_a2a_ce` 实际上被开局/收尾屏障变成了集合点。`plan` / `build_plan` 是纯本地计算（但输入 splits 全 rank 一致，所以结果一致）；`allocate` 是集合性的（rendezvous）；`copy_out` 是纯本地拷贝。校验必须先于第一个屏障，否则会挂起而非报错（u2-l2 的契约）。
</details>

**练习 2**：`prepare` 里为什么要同时检查「输入与窗口重叠」和「输出与窗口重叠」两种情况，而 `out=` 来自 `empty_output()` 时又豁免？

<details><summary>参考答案</summary>

见 [src/bindings.cc:L158-L172](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L158-L172)：输入或输出若与窗口共享字节，会在「每个 peer 都在写窗口」时被同时读/写。`out` **本身是**窗口（zero-copy 路径）是唯一合法的重叠，在 [src/bindings.cc:L134-L138](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L134-L138) 被 `out_is_window` 标记后跳过检查。
</details>

**练习 3**：本 rank 自己的那份数据走窗口吗？证据在哪一行？

<details><summary>参考答案</summary>

走。`launch_a2a_ce` 里 `emit(rank, stream)` 把自份额也发射到调用者流上（[src/transfer.cu:L116](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/transfer.cu#L116)）；plan 侧的依据是 [include/fast_ulysses/a2a_plan.hpp:L62-L64](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/include/fast_ulysses/a2a_plan.hpp#L62-L64)——`ops` 覆盖全部 `world_size` 个目的地，包括自己，这让 zero-copy 与 copy-out 都成为可能（u3-l2 详解）。
</details>

### 4.4 开发工具链

#### 4.4.1 概念说明

一张地图还要标出「营地设施」：改代码前后你会用什么、跑什么。本仓库的工具链刻意做得入口极少：

- **唯一 lint 入口**是 pre-commit：Python 用 ruff（行宽 100、py310+），C++/CUDA 用 clang-format 并**钉死 v15.0.7**——版本漂移会重排整棵树（[docs/develop.md:L32-L40](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/develop.md#L32-L40)）。
- **唯一测试入口**是 pytest，用 `multigpu` 标记把「需要 ≥2 卡的 torchrun worker」与「host-only 测试」分开。
- **唯一构建入口**是 `pip install -e .` → `setup.py` → `cmake` → `nvcc`，持久化 `build/` 目录让增量编译只重编改动的翻译单元（[docs/develop.md:L10-L11](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/develop.md#L10-L11)）。

#### 4.4.2 核心流程

构建链条与各文件的分工：

```
pip install -e . --no-build-isolation
  └─ setup.py :: CMakeBuild.build_extension      [setup.py:L91-L136]
       ├─ 读 FAST_ULYSSES_CUDA_ARCH → 设 TORCH_CUDA_ARCH_LIST
       ├─ cmake -S . -B build（configure）
       │    └─ CMakeLists.txt：找 torch、探测 c10d::register_work、
       │       GLOB src/*.cc + src/*.cu → add_library(_C SHARED ...)   [CMakeLists.txt:L126-L129]
       ├─ cmake --build build -jN（受内存预算约束的并行度）            [setup.py:L31-L51]
       └─ 把构建事实写成 fast_ulysses/_build_meta.py                   [setup.py:L149-L171]
```

测试入口的分层（[docs/develop.md:L44-L48](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/develop.md#L44-L48)）：

| 命令 | 前提 | 覆盖 |
|---|---|---|
| `pytest -m "not multigpu"` | 只需已构建的 `_C`，无需 GPU、无需进程组 | `test_plan.py`（布局重放）+ `test_wheel_matrix.py` |
| `pytest -m multigpu` | ≥2 GPU | `test_distributed.py` 拉起 `distributed/` 下 8 个 worker |
| 直接 `torchrun --nproc_per_node=N test/distributed/<worker>.py` | 同上 | 调试单个 worker 的最短路径 |

发布三件套（本讲只记位置，u8 详解）：`tools/build_wheels.sh`（一条 (torch, CUDA) 行出一个 wheel）、`tools/check_wheel.py`（ELF/元数据门禁）、`tools/preflight_gpu.sh`（打 tag 前必须在真多卡机上跑）；基准必须包在 `tools/exclusive.sh` 里，它打印 `EXCLUSIVE` 或 `CONTENDED`——「CONTENDED 的数字不是数字」（[docs/develop.md:L99-L100](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/develop.md#L99-L100)）。

#### 4.4.3 源码精读

- [pyproject.toml:L36-L41](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/pyproject.toml#L36-L41)：pytest 的 `testpaths = ["test"]` 与 `multigpu` 标记定义——上一节的两个命令就来自这里。
- [.pre-commit-config.yaml:L17-L23](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/.pre-commit-config.yaml#L17-L23)：clang-format 钉在 `v15.0.7`，且 `files: ^(include|src)/` 只管 C++/CUDA。
- [setup.py:L59-L83](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/setup.py#L59-L83)：`_requires()` 解释了 u1-l3 遇到的两件事——torch 是唯一运行时依赖且**精确钉到 minor**（`.so` 里嵌着 c10d 的 vtable 与 pybind11 模块，minor 一升就 ABI 断裂）；以及为什么构建不能在 pip 的 build isolation 里跑（隔离环境会装另一份 torch，扩展就链错对象）。
- [CMakeLists.txt:L126-L129](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/CMakeLists.txt#L126-L129)：源文件收集方式——`file(GLOB ...)` 全量收进一个 `_C` 目标，这也是 4.1.4 实践的证据。
- [setup.py:L149-L171](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/setup.py#L149-L171)：`_write_build_meta` 把版本、torch ABI、CUDA 版本、arch 列表、`HAS_WORK_REGISTRY` 写成 Python 模块——u1-l3 里 `doctor` 打印的构建信息、u8-l3 里 `_diagnose` 的归因都读它。

#### 4.4.4 代码实践

**实践目标**：跑通本机可行的工具链入口，确认「哪些检查不需要 GPU」。

**操作步骤**：

1. 读 [docs/develop.md:L30-L48](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/develop.md#L30-L48)，列出三条你会用到的命令。
2. 若已按 u1-l3 构建：运行 `pytest -m "not multigpu"`，记录通过/跳过的用例数；再运行 `pre-commit run --all-files`（需先 `pip install -e ".[dev]"` 与 `pre-commit install`）。
3. 打开 `.github/workflows/ci.yml`，找出 CI 在无 GPU runner 上实际执行的步骤，并与 [docs/develop.md:L82-L84](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/develop.md#L82-L84) 的自述对照：CI 证明的是「四架构编译、按预期链接、目标 torch 下可加载、`test_plan.py` 通过」，而**不是**多卡行为。

**需要观察的现象**：host-only 测试不需要 `torchrun`、不需要初始化进程组，几秒内跑完；`multigpu` 用例在少卡环境显示 skipped 而非 failed。

**预期结果**：你明确了日常改动的反馈环——改 C++ 后 `pip install -e . --no-build-isolation` 增量重编，`pytest -m "not multigpu"` 验布局，多卡验证留给 `torchrun` worker。（若本机未构建，以上以源码阅读完成并标注「待本地验证」。）

#### 4.4.5 小练习与答案

**练习 1**：clang-format 为什么要钉版本？换成 v19 会发生什么？

<details><summary>参考答案</summary>

[docs/develop.md:L38-L40](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/develop.md#L38-L40)：代码是用 v15.0.7 排版的，另一个版本会重排整棵树——一次无关的格式化提交污染所有 diff。这也是 `.pre-commit-config.yaml` 把 rev 钉死的原因。
</details>

**练习 2**：`tools/exclusive.sh` 为什么「refuses to start until the requested GPUs are free」？它防的是什么错误？

<details><summary>参考答案</summary>

防的是被污染的基准数字：若其他进程占着 GPU，测得的延迟/带宽混合了别人的负载。脚本先确认 GPU 空闲，再打印 `EXCLUSIVE` 或 `CONTENDED`，让「CONTENDED 数字不可用」成为显式事实（[docs/develop.md:L99-L100](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/develop.md#L99-L100)，u9-l4 详述）。
</details>

**练习 3**：CI 没有 GPU runner，那 `test/distributed/` 下的 8 个 worker 在 CI 里如何被覆盖？

<details><summary>参考答案</summary>

不被 CI 覆盖——CI 只证明编译/链接/加载/host-only 布局（[docs/develop.md:L82-L84](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/develop.md#L82-L84)）；多卡行为靠本地 `pytest -m multigpu` 与发布前的 `tools/preflight_gpu.sh` 在真机上把关，最老的 torch 行甚至只凭「编译+加载」证据就发布（u9-l1 的「CI 证明边界」）。
</details>

## 5. 综合实践

把本讲全部内容串成一个产出物：**一张带完整标注的调用链地图**。

任务：对 `y = group.all_to_all_4d(x, mode=0)`（sync、无 `out=`、偶均分）完成以下三步——

1. **画链**：以 4.3.2 的骨架为底稿，独立重画一遍调用链文本图，每个节点标注「文件 :: 函数名（可附行号）」，至少覆盖：`group.py` 的转发、dispatcher 上 `all_to_all_4d` 的三个注册 key、`run` 的三段式、`plan()` → `make_dims` → `build_plan`、`window()` → `allocate`、`fast_barrier`（两处）、`launch_a2a_ce` → `issue_copy`、`copy_out`。画完后与 4.3.3 的引用逐条核对。
2. **标状态**：在图上区分三类节点——①每次调用必经（校验、屏障、拷贝发射、copy_out）；②未命中缓存才走（plan 构建）；③首次/增长才走（`allocate` 的对称内存 rendezvous）。
3. **无 GPU 验证**：运行 4.2.4 的 `a2a_plan_debug` 脚本，把打印出的 `ops` 行数与你在图上 `build_plan` 节点旁预测的数量对上（提示：偶均分、无零长度分片时，mode 0 下本 rank 给每个 peer 各发一个 `CopyOp`，batch 折叠可能把它们并成每 peer 一个 3D 拷贝——`depth` 列会告诉你折叠是否发生）。若未构建扩展，此步标注「待本地验证」，但预测必须写在纸面上。

延伸（选做）：把 `out=` 来自 `empty_output()` 的 zero-copy 路径画成第二条链，指出它与 copying 路径的唯一分叉点在 `run` 的最后一行（[src/bindings.cc:L237-L239](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L237-L239)），以及 `prepare` 里 `out_is_window` 的判定（[src/bindings.cc:L134-L138](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L134-L138)）。

## 6. 本讲小结

- 仓库按「依赖 torch 与否、依赖 CUDA 与否」分层：`python/` 是薄门面，`src/bindings.cc` 是 torch 算子层，`src/group.cc` 是有状态层，`src/a2a_plan.cc` 是纯主机寻址层，`src/transfer.cu` + `src/barrier.cu` 是设备层；`test/`、`benchmark/`、`tools/`、`docs/` 在链外支撑。
- `src/a2a_plan.cc` 刻意无 CUDA、无 torch、无通信库，换来两个收益：布局正确性可以无 GPU 测试（`test_plan.py` 的 numpy 重放），以及 Meta 内核直接复用 `plan()` 而不产生第二份形状规则；torch 与 plan 的唯一相遇点被隔离在 `src/plan_bindings.cc`。
- 一次 sync 调用的骨架是 `prepare → 屏障 → 传输 → 屏障 → copy_out`：校验全部先于第一个屏障，plan 与窗口按 `(shape, mode, dtype, splits)` / `(role, dtype)` 缓存，稳态调用只剩校验、两次屏障、拷贝发射与 copy_out。
- 设备层不算偏移、不知道地址来源：`launch_a2a_ce` 只消费 `CopyOp` 与传入的 peer 地址，`fast_barrier` 只消费 flag 地址；`ws == 1` 时屏障整体跳过。
- 工具链入口极少：pre-commit（ruff + 钉版 clang-format）管 lint，pytest 用 `multigpu` 标记分两层，构建走 `setup.py` → CMake → nvcc 且必须 `--no-build-isolation`；CI 无 GPU，只证明编译/链接/加载/host-only 布局。

## 7. 下一步学习建议

下一讲进入单元 2（Python API 使用）：u2-l1「UlyssesGroup 构造与 NVLink 检查」会把本讲链上的 `_handle` 创建过程展开——`symm_mem.enable_symm_mem_for_group`、`torch.classes.fast_ulysses.UlyssesGroup` 构造与 `_require_nvlink` 的失败开放语义。

想继续按图索骥读源码的话，推荐两个方向：

- 顺着本讲的 plan 层往下：精读 [include/fast_ulysses/a2a_plan.hpp](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/include/fast_ulysses/a2a_plan.hpp)（67 行，全仓库最值得先读完的头文件）与 [src/a2a_plan.cc](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/a2a_plan.cc)，为 u3 做准备。
- 把 `test/test_plan.py` 通读一遍：它是「无 GPU 验证布局」这一分层的现场示范，也是 u3-l3 的主角。
