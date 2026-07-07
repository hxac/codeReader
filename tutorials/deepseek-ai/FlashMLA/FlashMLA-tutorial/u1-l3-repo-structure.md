# 仓库目录结构与代码组织

## 1. 本讲目标

学完本讲，你应当能够：

- 说出 FlashMLA 仓库里每个顶层目录（`flash_mla/`、`csrc/`、`tests/`、`benchmark/`、`docs/`）各自负责什么。
- 理解 `csrc/` 是如何按「**架构（sm90 / sm100 / smxx）× 阶段（decode / prefill）× 稀疏性（dense / sparse）**」三个维度组织 CUDA kernel 源码的。
- 拿到一个 kernel 的功能描述后，能快速定位到对应的源码目录与入口文件。
- 看懂 `setup.py` 里的 `sources` 列表，并把每个 `.cu` 文件归到正确的「架构 / 阶段 / 稀疏性」分类里。

本讲只建立**代码空间感**，不深入任何 kernel 的内部实现——那是后面单元的任务。

---

## 2. 前置知识

在开始之前，请确认你已经理解上一讲（u1-l1）建立的几个概念：

- **四类 kernel**：FlashMLA 把注意力按「阶段 × 稀疏性」切成四类——Dense Decoding、Sparse Decoding、Dense Prefill、Sparse Prefill。
- **两类 MLA 模式**：MQA（`head_dim_k=576`、`head_dim_v=512`，用于 decode 与 sparse prefill）和 MHA（`head_dim=128/128`，用于 dense prefill）。
- **支持矩阵**：只覆盖 SM90（Hopper/H800）和 SM100（Blackwell/B200）两代 GPU，且分布不对称——Dense Decoding 仅 SM90，Dense Prefill 仅 SM100，Sparse 两架构都有。

下面这些「目录命名缩写」会在源码里反复出现，先记住它们的含义：

| 缩写 / 目录片段 | 含义 |
| :--- | :--- |
| `csrc/` | C++ / CUDA source，所有 C++ 与 CUDA 源码 |
| `sm90` | Hopper 架构（H800），编译目标 `sm_90a` |
| `sm100` | Blackwell 架构（B200），编译目标 `sm_100f` |
| `smxx` | 与具体架构无关的「通用」kernel/辅助代码（sm 是占位） |
| `decode` | 解码阶段（每个 query 序列通常只有 1~2 个 token） |
| `prefill` | 预填充阶段（query 序列较长） |
| `dense` | 标准（稠密）注意力，访问完整 KV |
| `sparse` / `sparse_fp8` | token-level 稀疏注意力 / 带 FP8 KV cache 的稀疏注意力 |
| `instantiations/` | 模板实例化文件（专门用来「生成」某一种具体配置的 kernel） |

> 名词解释：**模板实例化（instantiation）**。FlashMLA 的 kernel 大量使用 C++ 模板（例如把 `head_dim`、是否带 `topk_length` 当成模板参数）。模板代码本身不会被编译成可执行代码，必须为「每一组具体的模板参数」专门写一个 `.cu` 文件来「实例化」它。所以你会看到很多目录里都有一个 `instantiations/` 子目录，里面是一堆很薄、只负责「生成某一种配置」的 `.cu` 文件。这个模式贯穿整个 `csrc/`，第 4.2 节会反复用到。

---

## 3. 本讲源码地图

本讲主要靠「读目录树」建立认知，只精读两个文件来佐证目录含义：

| 文件 | 作用 | 本讲怎么用 |
| :--- | :--- | :--- |
| [README.md](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/README.md) | 项目说明、支持矩阵、用法 | 用它的支持矩阵对照目录划分 |
| [setup.py](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/setup.py) | 构建脚本，列出所有参与编译的 `.cu/.cpp` 源文件 | 用它的 `sources` 列表做分类练习，验证目录理解 |

此外，本讲会**提及**（但不精读）以下文件，仅用于说明目录里装了什么：

- [csrc/api/api.cpp](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/api.cpp) —— pybind 桥接层。
- [flash_mla/__init__.py](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/flash_mla/__init__.py) —— 纯 Python 包的导出。
- [csrc/sm100/decode/head128/README.md](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/decode/head128/README.md) —— 解释 SM100 上 head128 的折中方案。

---

## 4. 核心概念与源码讲解

### 4.1 顶层目录职责

#### 4.1.1 概念说明

FlashMLA 是一个「**Python 包 + CUDA 扩展**」的二合一仓库。Python 侧负责给用户一个好用的接口（做形状校验、参数拼装），CUDA 侧负责真正的算力。因此顶层目录天然分成两组：

- **面向用户的壳**：`flash_mla/`、`README.md`、`setup.py`、`LICENSE`。
- **真正的算力实现**：`csrc/`（C++/CUDA 源码）。
- **质量与文档保障**：`tests/`、`benchmark/`、`docs/`。

#### 4.1.2 核心流程

从「用户敲一行 Python」到「GPU 跑起来」，文件分工如下：

```text
用户代码
  └─ import flash_mla            ← flash_mla/__init__.py
       └─ flash_mla_interface.py ← 纯 Python：校验张量、拼参数
            └─ flash_mla.cuda.*  ← setup.py 编译出的 .so 扩展
                 └─ csrc/api/api.cpp（pybind 绑定）
                      └─ csrc/api/*.h（接口层）
                           └─ csrc/sm90|sm100|smxx/...（各 kernel）
```

`setup.py` 在编译时把 `csrc/` 下选定的 `.cu/.cpp` 文件编译进 `flash_mla.cuda` 这个扩展模块；`tests/` 和 `benchmark/` 则是验证与测速用的脚本。

#### 4.1.3 源码精读

**（1）顶层目录一览。** 仓库根目录只有 6 个有意义的条目（外加 `LICENSE` 和本讲义目录 `FlashMLA-tutorial/`）：

```text
FlashMLA/
├── README.md          # 项目说明 + 支持矩阵 + 用法
├── setup.py           # 构建/安装脚本（本讲的另一主角）
├── flash_mla/         # 纯 Python 包：对用户的接口
├── csrc/              # C++/CUDA 源码：所有算力实现
├── tests/             # 正确性测试 + 参考实现
├── benchmark/         # 性能基准
└── docs/              # 深度技术博客
```

**（2）Python 包 `flash_mla/`。** 只有两个文件：

- [flash_mla/__init__.py:3-10](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/flash_mla/__init__.py#L3-L10)：从 `flash_mla_interface` 里把 6 个公开函数（`get_mla_metadata`、`flash_mla_with_kvcache`、`flash_mla_sparse_fwd`、3 个 `flash_attn_varlen_*`）重新导出。这是用户 `from flash_mla import ...` 真正拿到的入口。
- `flash_mla/flash_mla_interface.py`：这 6 个函数的 Python 实现——做张量校验、调度元数据管理，再调用编译出的 `flash_mla.cuda.*`。

**（3）`setup.py` 是「目录到产物」的契约。** [setup.py:62-64](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/setup.py#L62-L64) 声明了扩展名 `flash_mla.cuda`；紧接着 [setup.py:65-105](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/setup.py#L65-L105) 的 `sources=[...]` 列表，**逐一列出了哪些 `.cu/.cpp` 文件会被编译进去**。这份列表是验证「目录结构理解」的最佳锚点——第 4.1.4 节的实践就是基于它。

> 小贴士：注意 `sources` 列表里**没有** `flash_mla/*.py`。Python 代码由 `setup.py` 末尾 [setup.py:148](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/setup.py#L148) 的 `find_packages(include=['flash_mla'])` 单独打包。这印证了「Python 包」和「CUDA 扩展」是两套独立的产物。

#### 4.1.4 代码实践：用 `sources` 列表反推目录职责

**实践目标**：通过阅读 `setup.py` 的 `sources` 列表，确认每个顶层/二级目录到底贡献了什么，而不是靠猜。

**操作步骤**：

1. 打开 [setup.py:65-105](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/setup.py#L65-L105)，观察 `sources` 列表按「`# 注释` 分组」的方式。
2. 数一下每个**顶层路径前缀**出现了几次：
   - `csrc/api/` 出现 1 次（`api.cpp`）。
   - `csrc/smxx/` 出现 2 次。
   - `csrc/sm90/` 出现 4 组共 11 个文件。
   - `csrc/sm100/` 出现 3 组共 10 个文件。
3. 对比发现：`csrc/kerutils/`、`csrc/cutlass/`、`flash_mla/` 都**没有**出现在 `sources` 里——前两者是头文件库（`include_dirs` 引入即可，见 [setup.py:126-132](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/setup.py#L126-L132)），后者是纯 Python。

**需要观察的现象**：`sources` 列表的分组注释（`# API`、`# sm90 dense_decode`、`# sm100 sparse prefill` …）已经把文件按「架构 / 阶段 / 稀疏性」组织好了——这正是第 4.2 节要讲的目录划分方式。

**预期结果**：你会得到一张「目录前缀 → 职责」的映射，与第 4.1.3 节的描述一致。

> 小贴士：本讲所有永久链接都以仓库当前 HEAD `9241ae3` 为 base。后续单元若 HEAD 变化，链接里的哈希需要同步更新。

#### 4.1.5 小练习与答案

**练习 1**：用户执行 `pip install .` 后，会得到几个 Python 可 import 的东西？分别叫什么？
**参考答案**：两个。一个是纯 Python 包 `flash_mla`（含 `flash_mla.get_mla_metadata` 等函数），另一个是 CUDA 扩展模块 `flash_mla.cuda`（含 `dense_decode_fwd` 等 5 个 pybind 绑定）。

**练习 2**：为什么 `csrc/kerutils/` 不出现在 `setup.py` 的 `sources` 里，代码却仍能编译？
**参考答案**：因为它是**纯头文件库**（header-only），通过 [setup.py:128](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/setup.py#L128) 的 `include_dirs` 把 `csrc/kerutils/include` 加入头文件搜索路径，其他 `.cu` 文件 `#include` 它即可，不需要单独编译。

---

### 4.2 csrc 的「架构 × 阶段 × 稀疏性」三维划分

#### 4.2.1 概念说明

`csrc/` 是整个仓库最核心、也最大的目录。它把 kernel 源码按**三个正交维度**分类放进不同的子目录：

1. **架构维度**：`sm90/`、`sm100/`、`smxx/`（通用）。
2. **阶段维度**：`decode/`、`prefill/`。
3. **稀疏性维度**：`dense/`、`sparse/`（或 `sparse_fp8/`）。

这三个维度从外到内嵌套，形成一个清晰的三层路径。例如：

```text
csrc/sm90/decode/dense/        = SM90 + 解码 + 稠密
csrc/sm100/prefill/sparse/     = SM100 + 预填充 + 稀疏
```

读到一个 kernel 路径，你几乎能直接「读」出它属于哪一类。

> 为什么不把所有 kernel 放一起？因为 SM90 和 SM100 的硬件能力差异巨大（WGMMA vs 新一代 Tensor Core、CTA cluster、DSM 等），同一算法在两代架构上的最优实现完全不同，必须分别实现。这正对应了上一讲「支持矩阵不对称」的结论。

#### 4.2.2 核心流程：csrc 的完整目录树

下面是 `csrc/` 的关键结构（省略部分深层文件，保留划分逻辑）：

```text
csrc/
├── api/                          # ① 接口/派发层：Python ↔ kernel 的中转
│   ├── api.cpp                   #    pybind 绑定（5 个函数）
│   ├── common.h                  #    Arch 检测、DISPATCH 宏、ImplBase 派发框架
│   ├── dense_decode.h            #    Dense Decoding 接口
│   ├── sparse_decode.h           #    Sparse Decoding 接口
│   ├── dense_fwd.h               #    Dense Prefill 前向接口
│   └── sparse_fwd.h              #    Sparse Prefill 前向接口
│
├── params.h                      # ② 所有 kernel 共用的参数结构体
├── defines.h / utils.h           #    基础类型与工具
│
├── sm90/                         # ③ Hopper（H800）kernel
│   ├── decode/
│   │   ├── dense/                #    SM90 Dense Decoding（核心 kernel）
│   │   │   ├── config.h / traits.h
│   │   │   ├── splitkv_mla.h / splitkv_mla.cuh
│   │   │   └── instantiations/{fp16,bf16}.cu
│   │   └── sparse_fp8/           #    SM90 Sparse Decoding（FP8 KV cache）
│   │       ├── config.h
│   │       ├── components/       #    dequant 等子组件
│   │       ├── splitkv_mla.h / splitkv_mla.cuh
│   │       └── instantiations/{model1,v32}_persistent_h{64,128}.cu
│   └── prefill/
│       └── sparse/               #    SM90 Sparse Prefill
│           ├── config.h / phase1.h / phase1.cuh / fwd.h / fwd.cu
│           └── instantiations/phase1_k{512,576}{,_topklen}.cu
│
├── sm100/                        # ④ Blackwell（B200）kernel
│   ├── decode/
│   │   ├── head64/               #    SM100 Sparse Decoding，head=64
│   │   │   ├── config.h / kernel.h / kernel.cuh
│   │   │   └── instantiations/{v32,model1}.cu
│   │   └── head128/README.md     #    head128：用 2× head64 模拟，无独立实现
│   └── prefill/
│       ├── dense/                #    SM100 Dense Prefill/Backward（基于 CUTLASS）
│       │   ├── fmha_cutlass_{fwd,bwd}_sm100.cu / .cuh / interface.h
│       │   └── device/ kernel/ collective/ common/   # CUTLASS 分层
│       └── sparse/
│           ├── common_subroutine.h
│           ├── fwd/head64/       #    head=64 phase1
│           ├── fwd/head128/      #    head=128 phase1
│           └── fwd_for_small_topk/head128/  # 小 topk 专用变体（prefill+decode 复用）
│
├── smxx/                         # ⑤ 架构无关的解码辅助 kernel
│   └── decode/
│       ├── get_decoding_sched_meta/   # tile scheduler 元数据生成
│       └── combine/                   # Split-KV 跨 split 归并
│
├── kerutils/                     # ⑥ 工具/内联函数库（header-only）
│   └── include/kerutils/
│       ├── host/                 #    host 侧：张量校验宏
│       ├── device/{sm80,sm90,sm100}/  # 各架构 intrinsics/helpers/TMA/gemm
│       ├── common/、supplemental/
│       └── device/device.cuh
│
└── cutlass/                      # ⑦ CUTLASS 子模块（NVIDIA，sm100 dense 依赖）
```

这 7 个区块的职责一句话总结：

| 区块 | 路径 | 一句话职责 |
| :--- | :--- | :--- |
| ① 接口层 | `csrc/api/` | Python 调用进来后，做校验、派发到具体 kernel |
| ② 公共参数 | `csrc/params.h` 等 | 所有 kernel 共用的参数结构体与基础类型 |
| ③ SM90 kernel | `csrc/sm90/` | Hopper 上的 dense decode、sparse decode(FP8)、sparse prefill |
| ④ SM100 kernel | `csrc/sm100/` | Blackwell 上的 dense prefill/bwd、sparse prefill、sparse decode |
| ⑤ 通用辅助 | `csrc/smxx/` | 与架构无关的解码辅助 kernel（调度元数据 + combine 归并） |
| ⑥ 工具库 | `csrc/kerutils/` | header-only 的 host/device 工具与各架构 intrinsics |
| ⑦ CUTLASS | `csrc/cutlass/` | 第三方子模块，仅 SM100 dense prefill 依赖 |

#### 4.2.3 源码精读

**（1）接口层只有 5 个绑定。** 看 [csrc/api/api.cpp:8-15](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/api.cpp#L8-L15)，`PYBIND11_MODULE` 块只注册了 5 个函数：`sparse_decode_fwd`、`dense_decode_fwd`、`sparse_prefill_fwd`、`dense_prefill_fwd`、`dense_prefill_bwd`。**整个仓库无论多大，对 Python 暴露的 C++ 入口就这 5 个**。每个绑定各自 `#include` 一个同名头文件（`dense_decode.h` 等），那些头文件才是真正「派发到具体 kernel」的地方。

**（2）支持矩阵 vs 目录，一一对应。** 对照 [README.md:61-66](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/README.md#L61-L66) 的支持矩阵：

| 支持矩阵里的 kernel | GPU 架构 | 对应的 csrc 目录 |
| :--- | :--- | :--- |
| Dense Decoding | SM90 | `csrc/sm90/decode/dense/` |
| Sparse Decoding | SM90 & SM100 | `csrc/sm90/decode/sparse_fp8/` + `csrc/sm100/decode/head64/` |
| Dense Prefill | SM100 | `csrc/sm100/prefill/dense/` |
| Sparse Prefill | SM90 & SM100 | `csrc/sm90/prefill/sparse/` + `csrc/sm100/prefill/sparse/` |

注意几个不对称点，它们正好体现了架构取舍：

- **Dense Decoding 只在 `sm90/`**，`sm100/` 下根本没有 `decode/dense/`。
- **Dense Prefill 只在 `sm100/`**，`sm90/` 下没有 `prefill/dense/`。
- **SM100 的 decode 只有 sparse**（且只有 `head64/` 有真实实现，`head128/` 用 2× head64 模拟，见 [csrc/sm100/decode/head128/README.md](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/decode/head128/README.md)）。

**（3）`smxx/`：架构无关的「公共解码基础设施」。** 解码阶段有两件事与具体 GPU 架构无关——生成 tile scheduler 元数据、把多个 split 的结果归并（combine）。所以它们被放进 `csrc/smxx/decode/`，SM90 和 SM100 的解码路径**共用**这两个 kernel。这解释了为什么 `setup.py` 把它们单列成一组 [setup.py:69-71](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/setup.py#L69-L71)（`# Misc kernels for decoding`）。

**（4）`instantiations/` 模式与文件扩展名的约定。** 在每个 kernel 目录里，文件扩展名有明确分工：

- `.h`：对外接口声明（host 可见）。
- `.cuh`：kernel 的模板实现（device + host 模板代码）。
- `config.h`：静态 tile/block 常量。
- `instantiations/*.cu`：把模板「实例化」成具体配置的薄文件，**只有 `.cu` 才会被 `setup.py` 编译**。

例如 `csrc/sm90/prefill/sparse/` 下，真正的算法在 `phase1.cuh`，而 [setup.py:84-88](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/setup.py#L84-L88) 编译的是 `fwd.cu` + 4 个 `instantiations/phase1_k{512,576}{,_topklen}.cu`——对应 `(head_dim=512 或 576) × (是否带 topk_length)` 共 4 种组合。

#### 4.2.4 代码实践：把 `sources` 里的每个 `.cu` 映射到三维分类表

这是本讲的**主实践任务**，直接对应学习目标里「能根据功能快速定位 kernel 入口文件」。

**实践目标**：把 [setup.py:65-105](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/setup.py#L65-L105) 的 `sources` 列表里每一个 `.cu` 文件，归到「架构 / 阶段 / 稀疏性」三列表格里，验证你对 4.2.2 节目录树的理解。

**操作步骤**：

1. 逐行阅读 `sources` 列表，按其分组注释把文件分成几组。
2. 对每个文件，从路径里读出三个维度：`sm90/sm100/smxx` → 架构；`decode/prefill` → 阶段；`dense/sparse(_fp8)` → 稀疏性。
3. 把结果填进下面的表格。

**参考答案表**（这就是本实践的预期产出）：

| `.cu` 文件（路径简写） | 架构 | 阶段 | 稀疏性 | 备注 |
| :--- | :--- | :--- | :--- | :--- |
| `api/api.cpp` | —（pybind） | — | — | 不是 kernel，是绑定层 |
| `smxx/.../get_decoding_sched_meta.cu` | smxx（通用） | decode | dense+sparse 共用 | tile scheduler 元数据 |
| `smxx/.../combine.cu` | smxx（通用） | decode | dense+sparse 共用 | Split-KV 归并 |
| `sm90/decode/dense/instantiations/{fp16,bf16}.cu` | sm90 | decode | **dense** | Dense Decoding |
| `sm90/decode/sparse_fp8/instantiations/*.cu`（4 个） | sm90 | decode | **sparse (FP8)** | Sparse Decoding |
| `sm90/prefill/sparse/fwd.cu` + `instantiations/*.cu`（5 个） | sm90 | prefill | **sparse** | Sparse Prefill |
| `sm100/prefill/dense/fmha_cutlass_fwd_sm100.cu` | sm100 | prefill | **dense** | Dense Prefill 前向 |
| `sm100/prefill/dense/fmha_cutlass_bwd_sm100.cu` | sm100 | prefill | **dense** | Dense Prefill 反向 |
| `sm100/prefill/sparse/fwd/head{64,128}/instantiations/*.cu`（4 个） | sm100 | prefill | **sparse** | Sparse Prefill |
| `sm100/prefill/sparse/fwd_for_small_topk/head128/.../phase1_prefill_k512.cu` | sm100 | prefill | sparse | 小 topk 专用变体 |
| `sm100/decode/head64/instantiations/{v32,model1}.cu` | sm100 | decode | **sparse** | Sparse Decoding |
| `sm100/prefill/sparse/fwd_for_small_topk/head128/.../phase1_decode_k512.cu` | sm100 | **decode** | sparse | 文件夹在 prefill 下，但服务 decode（head128 解码复用 small_topk kernel） |

**需要观察的现象与预期结果**：

- 数一下每种「(架构, 阶段, 稀疏性)」组合：你应该得到与支持矩阵一致的覆盖——dense decode 仅 sm90、dense prefill 仅 sm100、sparse 在两架构都有。
- 特别注意**最后一行**：它的路径前缀是 `sm100/prefill/sparse/fwd_for_small_topk/...`，但文件名是 `phase1_decode_k512.cu`、阶段是 **decode**。这是个有意的复用——SM100 上 head128 的解码直接复用了为 small-topk prefill 写的 kernel（与 [csrc/sm100/decode/head128/README.md](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/decode/head128/README.md) 说的「或用 2× head64 模拟」一致）。这提醒你：**路径前缀是强提示，但不是绝对真理**，文件名里的 `decode`/`prefill` 才是它的真实归属。

> 待本地验证：以上映射完全基于读源码路径与 `setup.py` 推断，未实际编译。若你在本地 `pip install -v .` 后想核实，可以观察编译日志里每个 `.cu` 的输出目标，与上表对照。

#### 4.2.5 小练习与答案

**练习 1**：如果有人问「SM90 上有没有 dense prefill 的 kernel？」，你该去哪个目录找？结论是什么？
**参考答案**：去 `csrc/sm90/prefill/` 下找。该目录只有 `sparse/`，没有 `dense/`，所以结论是「SM90 没有 dense prefill kernel」——这与支持矩阵「Dense Prefill 仅 SM100」一致。

**练习 2**：`csrc/sm90/decode/` 下为什么同时有 `dense/` 和 `sparse_fp8/`，而 `csrc/sm90/prefill/` 下只有 `sparse/`？
**参考答案**：因为 SM90 同时实现了 Dense Decoding 和 Sparse Decoding（后者用 FP8 KV cache，故命名 `sparse_fp8`）；而 SM90 的 prefill 只实现了稀疏版本，没有 dense prefill。目录结构忠实反映了「实现了什么」。

**练习 3**：`csrc/sm100/prefill/dense/` 下为什么有 `device/`、`kernel/`、`collective/`、`common/` 这种分层，而 `sm90/` 的 kernel 目录没有？
**参考答案**：因为 SM100 的 dense prefill 是**基于 CUTLASS** 实现的，CUTLASS 本身要求按 device→kernel→collective 的分层来组织代码；而 SM90 的 kernel 是手写的，不需要这种分层。这从目录结构就能一眼看出「哪条路径用了 CUTLASS」。

---

### 4.3 测试与文档目录

#### 4.3.1 概念说明

一个高质量的 kernel 库必须有「参考实现」来校验数值正确性，以及「基准」来度量性能。FlashMLA 把它们放在 `tests/` 和 `benchmark/`，技术深度博客放在 `docs/`。这三个目录不参与 kernel 编译，但对学习和二次开发极其重要——**`tests/` 里的参考实现往往是最清晰的「这个 kernel 到底在算什么」的文档**。

#### 4.3.2 核心流程：tests / benchmark / docs 的分工

```text
tests/                    # 正确性：用 PyTorch 参考实现校验 kernel 输出
├── lib.py                #   测试用例生成（TestParam / Testcase）
├── ref.py                #   纯 PyTorch 参考实现（dense/sparse decode、sparse fwd）
├── quant.py              #   FP8 量化/反量化（KV cache 656 字节布局）
├── test_flash_mla_dense_decoding.py   # Dense Decoding 正确性 + 性能
├── test_flash_mla_sparse_decoding.py  # Sparse Decoding
├── test_flash_mla_sparse_prefill.py   # Sparse Prefill
├── test_fmha_sm100.py    # Dense MHA Prefill/Backward（SM100）
└── kernelkit/            #   精度/对比/bench 工具集

benchmark/                # 性能：多实现横评（torch/flashinfer/triton/flash_mla）
├── bench_flash_mla.py
└── visualize.py

docs/                     # 深度技术博客
├── 20250422-new-kernel-deep-dive.md        # SM90 dense decode 深度解析
├── 20250929-hopper-fp8-sparse-deep-dive.md # FP8 sparse decode 深度解析
└── assets/                                  # 配图（SVG）
```

#### 4.3.3 源码精读

**（1）`tests/ref.py` 是「最权威的行为说明书」。** 当你不确定某个 kernel 在算什么时，读 `ref.py` 比读 CUDA 源码快得多——它是纯 PyTorch 的等价实现。README 甚至直接把 sparse prefill 的等价 PyTorch 代码贴在了文档里（[README.md:154-170](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/README.md#L154-L170)），告诉你返回的 `(out, max_logits, lse)` 等价于哪些 PyTorch 运算。

**（2）`tests/quant.py` 对应 FP8 KV cache 布局。** 上一讲提到的「每 token 656 字节 = 512 fp8 + 16 scale + 128 bf16 RoPE」就在这里实现（README 在 [README.md:118-123](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/README.md#L118-L123) 明确指向它）。调试 FP8 路径时，它是首选入口。

**（3）`benchmark/bench_flash_mla.py` 对比多个实现。** FlashMLA 的性能不是「自说自话」，而是与 PyTorch 原生、flashinfer、triton 横向对比得出的。这些脚本同时定义了「理论 TFlops / GB/s」的算式，是理解性能数字来源的关键。

**（4）`docs/` 的两篇深度博客是后续多个单元的理论基础。** `20250422-new-kernel-deep-dive.md` 讲 SM90 dense decode 的 seesaw 调度与 compute-bound 分析；`20250929-hopper-fp8-sparse-deep-dive.md` 讲 FP8 sparse decode 的反量化与 crossover 技术。后续单元（u3、u5）会反复引用它们。

#### 4.3.4 代码实践：用 `tests/` 验证「四类 kernel 各有对应测试」

**实践目标**：确认 `tests/` 下的测试文件与「四类 kernel」一一对应，建立「功能 → 测试文件」的快速映射。

**操作步骤**：

1. 列出 `tests/` 下所有 `test_*.py` 文件。
2. 对照支持矩阵的四类 kernel，给每个测试文件标注它测的是哪一类。
3. 用 [README.md:30-49](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/README.md#L30-L49) 给出的「Test & benchmark」命令核对（README 里直接写了每个测试对应的性能数字）。

**预期结果表**：

| 测试文件 | 测的 kernel 类别 | 对应 csrc 目录 |
| :--- | :--- | :--- |
| `test_flash_mla_dense_decoding.py` | Dense Decoding | `csrc/sm90/decode/dense/` |
| `test_flash_mla_sparse_decoding.py` | Sparse Decoding | `csrc/sm90/decode/sparse_fp8/` + `csrc/sm100/decode/` |
| `test_flash_mla_sparse_prefill.py` | Sparse Prefill | `csrc/sm90/prefill/sparse/` + `csrc/sm100/prefill/sparse/` |
| `test_fmha_sm100.py` | Dense Prefill（MHA fwd/bwd） | `csrc/sm100/prefill/dense/` |

> 待本地验证：无 GPU 环境下无法实际跑这些测试，但你可以打开任一 `test_*.py` 文件，看它 import 了 `flash_mla` 的哪个函数、构造了什么形状的张量，从而核对上表。

#### 4.3.5 小练习与答案

**练习 1**：你想知道 sparse prefill kernel 在数学上到底返回什么，最快的办法是读哪个文件？
**参考答案**：读 `tests/ref.py`（或 README 里贴的等价 PyTorch 代码 [README.md:154-170](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/README.md#L154-L170)）。CUDA 源码是优化后的实现，PyTorch 参考实现才是「行为定义」。

**练习 2**：`tests/kernelkit/` 和 `benchmark/` 都和性能有关，它们的区别是什么？
**参考答案**：`benchmark/` 是「跨实现横评」（flash_mla vs torch vs flashinfer vs triton），关心相对性能；`tests/kernelkit/` 是 kernel 开发阶段的「自用工具集」（精度比对、单 kernel bench、结果对比），更偏调试与开发。一个是面向用户的性能报告，一个是面向开发者的工程工具。

---

## 5. 综合实践：画出你自己的「功能 → 目录」速查表

把本讲三个模块串起来，完成下面这个综合任务：

**任务**：假设你接到一个需求——「在 SM100 上跑一次 Sparse Decoding，并怀疑结果不对」。请仅凭目录结构，写出你会依次打开的文件清单与理由。

**参考作答路径**：

1. **Python 入口**：`flash_mla/flash_mla_interface.py`——看 `flash_mla_with_kvcache` 在 `is_fp8_kvcache=True`、`indices` 非空时，实际调用了 `flash_mla.cuda` 的哪个绑定（应为 `sparse_decode_fwd`）。
2. **pybind 绑定**：`csrc/api/api.cpp` 第 10 行——确认 `sparse_decode_fwd` 绑到 `sparse_attn_decode_interface`。
3. **接口/派发层**：`csrc/api/sparse_decode.h`——看它如何校验、如何派发到 SM90 还是 SM100 的实现。
4. **SM100 kernel**：`csrc/sm100/decode/head64/`（`kernel.cuh` 是算法本体，`instantiations/*.cu` 是具体配置）。若 head=128 则走 `csrc/sm100/prefill/sparse/fwd_for_small_topk/head128/` 的 decode 实例化。
5. **公共解码基础设施**：`csrc/smxx/decode/get_decoding_sched_meta/` 与 `csrc/smxx/decode/combine/`——SM100 sparse decode 同样要用到 tile scheduler 元数据与 combine 归并。
6. **参考实现**：`tests/ref.py` + `tests/test_flash_mla_sparse_decoding.py`——对照数值、看测试是怎么构造数据的。

**为什么这个练习有价值**：它强迫你把「Python 壳 → pybind → 接口派发 → 具体 kernel → 公共辅助 → 测试参考」整条链路的**目录定位**都走一遍。等你后面单元深入每个文件内部时，这张地图就是你的导航。

> 进阶（可选）：把这条路径画成一张流程图（用纸笔或任意画图工具），每个节点标注文件路径。这张图会在 u2-l1「调用链全景」里被进一步细化。

---

## 6. 本讲小结

- 仓库顶层分两组：**用户壳**（`flash_mla/`、`README.md`、`setup.py`）与**算力实现**（`csrc/`），外加**质量保障**（`tests/`、`benchmark/`、`docs/`）。
- `csrc/` 按「**架构（sm90/sm100/smxx）× 阶段（decode/prefill）× 稀疏性（dense/sparse）**」三个维度嵌套组织，读路径就能猜出 kernel 类别。
- 支持矩阵的不对称直接体现在目录里：dense decode 仅 `sm90/`、dense prefill 仅 `sm100/`、sparse 两架构都有；SM100 head128 解码无独立实现，复用 small_topk prefill kernel 或 2× head64。
- `csrc/api/` 只暴露 5 个 pybind 绑定，是 Python 与全部 kernel 之间的唯一通道；`csrc/smxx/` 是两架构共用的解码辅助 kernel（调度元数据 + combine）。
- 每个 kernel 目录遵循 `.h（接口）/ .cuh（模板实现）/ config.h（常量）/ instantiations/*.cu（实例化）` 的文件分工，只有 `.cu` 会被 `setup.py` 编译。
- `tests/ref.py` 与 `tests/quant.py` 是理解 kernel 行为与 FP8 布局的最快入口；`docs/` 的两篇博客是后续单元的理论基础。

---

## 7. 下一步学习建议

你已经建立了完整的代码空间感。接下来建议：

- **u1-l4（Python 接口与最小运行示例）**：从用户视角真正跑通 `flash_mla`，把本讲看到的目录与实际函数调用对应起来。
- **u2-l1（调用链全景）**：把本讲「5. 综合实践」里画的那张路径图，细化为 Python→pybind→接口→kernel 的完整调用图。
- 之后按大纲进入各 kernel 家族：**u3**（SM90 dense decode）、**u4**（Split-KV/combine/tile scheduler，即本讲提到的 `smxx/`）、**u5**（FP8 sparse decode）、**u6**（sparse prefill）。

如果你想立刻动手巩固本讲，建议重做 4.2.4 的「`sources` 三维分类表」——能不查答案地填对，就说明目录结构已经刻进脑子了。
