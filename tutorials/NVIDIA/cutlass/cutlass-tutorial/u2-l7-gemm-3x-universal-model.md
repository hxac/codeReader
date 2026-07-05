# CUTLASS 3.x GEMM 通用模型

## 1. 本讲目标

本讲承接 [u2-l3 CuTe 算法：copy 与 gemm](u2-l3-cute-algorithms.md)、[u2-l4 CuTe Atoms：MMA 与 Copy 原子](u2-l4-cute-atoms.md)（你已经掌握了 `cute::copy`/`cute::gemm`、`MMA_Atom`/`TiledMma` 这些「积木」），也承接 [u2-l6 CUTLASS 2.x GEMM 分层结构](u2-l6-gemm-2x-hierarchy.md)（你已经看过 2.x 的 device→kernel→threadblock→warp→thread 五段式）。

我们这一讲要回答一个核心问题：

> CUTLASS 3.x 不再走「按架构手写偏特化森林」的老路，而是提出了一个统一的 `GemmUniversal` 模型。这个「通用模型」长什么样？它由几块组成？又是怎么靠一个策略标签（policy tag）自动选出正确的内核实现的？

读完本讲，你应当能够：

- 说清 3.x 的 **kernel（`GemmUniversal`）+ collective MMA（mainloop）+ collective epilogue + tile scheduler** 三段式（实际是「一内核 + 三部件」）架构。
- 掌握 `dispatch_policy.hpp` 的策略选择机制：**mainloop policy 携带一个 `Schedule` 标签，kernel 用它做 `enable_if` 偏特化分派**。
- 读懂 `GemmUniversalAdapter` 这个 host 句柄如何把 `Arguments` 翻译成 `Params`、再 `<<<grid>>>` 启动内核，以及它如何同时兼容 2.x/3.x 两套 API。
- 说清楚 3.x 与 2.x 在「内核组织方式」上的本质区别，为 [u2-l8 CollectiveBuilder 与主循环](u2-l8-collective-builder.md)（自动组装）与 [u3-l1 异步流水线](u3-l1-async-pipeline.md)（warp specialization 落地）打下地基。

## 2. 前置知识

- **collective（集体）**：CUTLASS 3.x 的关键词。一个 collective 不是「一个线程」或「一个 warp」的算法，而是「一群线程（通常一个 CTA 或一个 cluster）协同完成」的一段工作。`CollectiveMainloop` = 一群线程协同把 A、B 搬进来并做乘加；`CollectiveEpilogue` = 一群线程协同加载 C、做后处理、写回 D。
- **mainloop（主循环）**：GEMM 里反复「搬一块 A、B 到共享内存 → 算一段乘加 → K 维推进一格」的循环。3.x 把它整体打包成一个 collective。
- **epilogue（尾声）**：乘加结束后、写回显存前的后处理，典型如 \(D = \alpha(A \cdot B) + \beta C\)、激活函数等。
- **tile scheduler（瓦片调度器）**：决定「当前这个 CTA 负责输出矩阵的哪一块 tile」。3.x 默认走持久化（persistent）调度。
- **dispatch policy（分派策略）**：一个空的 C++ 结构体标签（如 `KernelTmaWarpSpecialized`），本身不含数据，纯粹用于在编译期「打标签、选分支」。
- **warp specialization（线程束特化）**：Hopper 起，让不同 warp group 分别扮演 producer（专门搬数据）与 consumer（专门算乘加），靠异步流水线重叠二者——u3-l1 会专门讲，本讲先知道「有这么个角色分工」即可。

一句话直觉：3.x 把一次 GEMM 看成 **「一个无状态内核，组合了两个 collective（主循环 + 尾声）和一个调度器」**。内核本身只负责把它们串起来，不做具体计算；具体计算全在 collective 内部，由上一讲学过的 CuTe 原子完成。

\[ \text{kernel::GemmUniversal} \;=\; \text{CollectiveMainloop} \;+\; \text{CollectiveEpilogue} \;(+\; \text{TileScheduler}) \]

## 3. 本讲源码地图

| 文件 | 角色 | 作用 |
| --- | --- | --- |
| `include/cutlass/gemm/kernel/gemm_universal_decl.h` | 内核声明 | `GemmUniversal` 主模板声明，注释点明「无状态内核 = mainloop + epilogue 的组合」，并解释了兼容 2.x/3.x 的「Or」命名 |
| `include/cutlass/gemm/kernel/gemm_universal.hpp` | 内核总装头 | 仅 `#include` 所有按架构/策略拆分的 `GemmUniversal` 偏特化（sm70/sm90/sm100/sm103/sm120） |
| `include/cutlass/gemm/kernel/sm90_gemm_tma_warpspecialized.hpp` | 内核实例 | `GemmUniversal` 的一个偏特化：靠 `enable_if` 匹配 `Schedule==KernelTmaWarpSpecialized` 落地 Hopper TMA + warp 特化内核 |
| `include/cutlass/gemm/dispatch_policy.hpp` | 策略字典 | 所有 kernel schedule 标签（`KernelTmaWarpSpecialized` 等）与 mainloop policy（`MainloopSm90TmaGmmaWarpSpecialized` 等）的定义 |
| `include/cutlass/gemm/device/gemm_universal_adapter.h` | host 句柄 | `GemmUniversalAdapter`：状态化句柄，管 `Params` 生命周期，按 `IsCutlass3GemmKernel` 分两个偏特化分别兼容 2.x/3.x |
| `include/cutlass/gemm/gemm.h` | 公共元函数 | `IsCutlass3GemmKernel`：靠「内核是否别名了 `ProblemShape`」判断是 2.x 还是 3.x API |
| `include/cutlass/gemm/gemm_enumerated_types.h` | 枚举 | `GemmUniversalMode`：`kGemm`/`kBatched`/`kGrouped`/`kGemmSplitKParallel` 等运行模式 |

记住这张表的核心结论：**3.x 的「内核」是一个统一名字 `GemmUniversal`，但它的真正实现散落在十几个按「架构 + 策略」命名的偏特化文件里**（如 `sm90_gemm_tma_warpspecialized*.hpp`、`sm100_gemm_tma_warpspecialized*.hpp`），靠 `dispatch_policy.hpp` 里的标签把「用户选择」和「具体实现」连起来。

## 4. 核心概念与源码讲解

### 4.1 三段式架构总览

#### 4.1.1 概念说明

CUTLASS 3.x 把一次 GEMM 内核拆成「一个无状态外壳 + 三个可替换部件」：

| 部件 | 类型 | 职责 |
| --- | --- | --- |
| **内核外壳** | `kernel::GemmUniversal` | 无状态。只负责：声明 `SharedStorage`、把 `Arguments` 翻成 `Params`、在 `operator()` 里把 mainloop 与 epilogue 串起来 |
| **主循环 collective** | `CollectiveMainloop` | 数据搬运（gmem→smem）+ 乘加计算（MMA）。它持有 `TiledMma`、`GmemTiledCopyA/B` 等 CuTe 原子 |
| **尾声 collective** | `CollectiveEpilogue` | 加载 C、做 \( \alpha,\beta \) 缩放/激活、写回 D。它持有 `GmemTiledCopyC/D` 与 `ThreadEpilogueOp` |
| **调度器** | `TileScheduler` | 决定每个 CTA 负责哪些输出 tile（持久化 / Stream-K，见 u3-l3） |

「无状态」是关键词：内核类本身**不存任何成员变量**，所有运行期数据都在传给 `operator()` 的 `Params` 与 `SharedStorage` 里。这让同一个内核类型可以在不同问题间复用、被 host 句柄反复启动。

#### 4.1.2 核心流程

一个 3.x 内核从 host 到 device 的完整链路：

```
host:   GemmUniversalAdapter::operator()(args)
          → initialize(args)
              → GemmKernel::to_underlying_arguments(args, workspace)   // Arguments → Params
              → cudaFuncSetAttribute(... MaxDynamicSharedMemorySize ...) // 申请大块 smem
          → run(params_)
              → 算 grid / block / cluster
              → device_kernel<GemmKernel><<<grid,block,smem,cluster>>>(params)   // <<<>>> 启动

device: GemmUniversal::operator()(params, smem_buf)
          // 1. 角色划分：谁是 producer（搬数据）谁是 consumer（算乘加）
          // 2. producer warp:
          CollectiveMainloop::load(...)        // TMA 把 A、B 搬进 smem 多级缓冲
          CollectiveMainloop::load_tail(...)
          CollectiveEpilogue::load(...)        // TMA 把 C 搬进 smem
          // 3. consumer warp group:
          CollectiveMainloop::mma(...)         // 反复 wgmma 乘加，结果进累加器
          CollectiveMainloop::mma_tail(...)
          CollectiveEpilogue::store(...)       // 累加器 → 后处理 → TMA 写回 D
```

注意：在 warp 特化内核里，**搬数据（load）和算乘加（mma）分属不同 warp group，靠异步流水线重叠**（u3-l1 详述）。即便不看流水线细节，你也能看出三段式的骨架——`load`/`mma`/`store` 三个动词对应三个 collective。

#### 4.1.3 源码精读

**①「无状态内核 = mainloop + epilogue 的组合」的总纲注释** —— 这是理解整个 3.x 模型的第一句话：

[gemm_universal_decl.h:36-49](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/kernel/gemm_universal_decl.h#L36-L49) 注释明确写道：`GemmUniversal` 是一个「把 GEMM 看成 collective mainloop 与 collective epilogue 组合」的无状态内核。并说明了「Or」命名规则——`ProblemShapeOrThreadblockMma_` 这种名字里，`Or` 之前是 3.x 参数序、之后是 2.x 参数序。**同一个类名 `GemmUniversal` 通过偏特化同时服务两代 API**。

主模板声明只有 5 个模板参数，结构极其朴素：

```cpp
template <
  class ProblemShapeOrThreadblockMma_,            // 3.x: ProblemShape 元组 (m,n,k,l)
  class CollectiveMainloopOrEpilogue_,            // 3.x: CollectiveMainloop
  class CollectiveEpilogueOrThreadblockSwizzle_,  // 3.x: CollectiveEpilogue
  class TileScheduler_ = void,
  class Enable = void
>
class GemmUniversal;
```

**② 内核如何「组装」两个 collective** —— 看 Hopper TMA warp 特化这个具体偏特化的类型别名与共享内存：

[sm90_gemm_tma_warpspecialized.hpp:77-117](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/kernel/sm90_gemm_tma_warpspecialized.hpp#L77-L117) 从 `CollectiveMainloop_` 提取 `TileShape`/`TiledMma`/`ArchTag`/`ElementA`/`StrideA`/`DispatchPolicy`/`ClusterShape`，从 `CollectiveEpilogue_` 提取 `ElementC`/`ElementD`/`StrideC`/`StrideD`。**内核自己几乎不定义类型，全部「借用」两个 collective 的类型**——这正是组合模型的体现。

[sm90_gemm_tma_warpspecialized.hpp:119-143](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/kernel/sm90_gemm_tma_warpspecialized.hpp#L119-L143) 定义内核级 `SharedStorage`：注意 mainloop 与 epilogue 的张量存储是一个 **`union`**（`TensorStorage`），因为「非持久化内核里两者不会并发使用 smem」。`MaxThreadsPerBlock` 由 `size(TiledMma{}) + 一个 load warp group` 算出。

**③ producer/consumer 三段式执行** —— 内核入口 `operator()` 里把三段串起来：

[sm90_gemm_tma_warpspecialized.hpp:430-465](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/kernel/sm90_gemm_tma_warpspecialized.hpp#L430-L465) producer warp 调 `collective_mainloop.load(...)`（搬 A、B）、`load_tail`，随后调 `collective_epilogue.load(...)`（搬 C）。

[sm90_gemm_tma_warpspecialized.hpp:468-509](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/kernel/sm90_gemm_tma_warpspecialized.hpp#L468-L509) consumer warp group 调 `collective_mainloop.mma(...)`（乘加）、`mma_tail`，再调 `collective_epilogue.store(...)`（后处理并写回 D）。**三个动词 `load` / `mma` / `store` 正好对应「搬入 / 计算 / 写出」三段**。

#### 4.1.4 代码实践

**实践目标**：验证「3.x 内核只是把两个 collective 串起来」这一论断，不靠 GPU，纯源码阅读。

**操作步骤**：

1. 打开 `include/cutlass/gemm/kernel/sm90_gemm_tma_warpspecialized.hpp`，定位到 `operator()`（搜索 `if (warp_group_role == WarpGroupRole::Producer)`）。
2. 列出 producer 分支调用了 `CollectiveMainloop` 的哪些静态/成员方法，consumer 分支又调用了哪些。
3. 用 `grep` 统计这个文件里 `collective_mainloop.` 与 `collective_epilogue.` 出现的次数，确认内核本身「不直接算乘加、不直接搬数据」，全部委托给两个 collective。

**需要观察的现象**：

- 内核类里**找不到任何 `wgmma` / `mma.sync` / `cp.async` 之类的 PTX 指令包装调用**——这些都在 collective 内部。
- 内核只做「角色划分（producer/consumer）+ 流水线状态机 + 调用 collective 的方法」。

**预期结果**：你会确认 `GemmUniversal` 是一个**纯组合外壳**，真正的算力在 `CollectiveMainloop::mma` 内部（它再调用 u2-l4 学过的 `TiledMma`）。

#### 4.1.5 小练习与答案

**练习 1**：`sm90_gemm_tma_warpspecialized.hpp` 里为什么把 mainloop 与 epilogue 的张量存储放在一个 `union` 里？换成持久化内核（persistent）后还能这么放吗？

> **答案**：非持久化内核里，一个 CTA 只算一块 tile——先全程跑 mainloop（占 smem），mainloop 结束释放后再跑 epilogue（再占 smem），两者**时间上不重叠**，故可 `union` 省显存。持久化内核里一个 CTA 会算很多块 tile，存在「上一块 epilogue 写回」与「下一块 mainloop 加载」重叠的需求，就不能简单 `union`，需要更复杂的 smem 分配。

**练习 2**：`GemmUniversal` 类里为什么没有成员变量（无状态）？

> **答案**：为了让内核类型可以**跨问题复用**——host 句柄只需要构造一次对象，对不同的 `Arguments` 反复 `run()`。无状态也让内核可以安全地被 `device_kernel` 包裹成裸 CUDA kernel 启动，所有运行期信息经 `Params`（常量内存般传参）传入。

---

### 4.2 dispatch_policy 策略

#### 4.2.1 概念说明

3.x 有几十种「内核实现」（Hopper TMA pingpong、Blackwell UMMA、SM120 cooperative……），用户怎么告诉 CUTLASS「我要哪一种」？答案不是 if-else，而是 **编译期标签分派（tag dispatch）**。`dispatch_policy.hpp` 就是这本「策略字典」，里面有两类东西：

1. **kernel schedule 标签**：如 `KernelTmaWarpSpecialized`、`KernelTmaWarpSpecializedPingpong`、`KernelTmaWarpSpecializedCooperative`。它们是**空结构体**，纯粹用于 `enable_if` 分支选择。注释明说「one for each kernel layer file」——**一个标签对应一个 kernel 偏特化文件**。
2. **collective mainloop policy**：如 `MainloopSm90TmaGmmaWarpSpecialized<Stages, ClusterShape, KernelSchedule>`。它是**带参数的策略结构体**，成员有 `Stages`（流水线级数）、`ClusterShape`（CTA cluster 形状）、`ArchTag`（目标架构）、`Schedule`（一个 kernel schedule 标签）。

关键机制一句话：**mainloop policy 内嵌一个 `Schedule` 标签，kernel 偏特化用 `enable_if_t<is_base_of_v<Schedule, Mainloop::DispatchPolicy::Schedule>>` 来选中自己**。于是「用户选 policy → policy 带 Schedule → Schedule 选 kernel 文件」形成一条编译期分派链。

#### 4.2.2 核心流程

```
用户:  CollectiveBuilder<..., MainloopSm90TmaGmmaWarpSpecialized<3, Shape<2,2,1>>>  // 选 mainloop policy
           │ 该 policy 内 using Schedule = KernelTmaWarpSpecialized;                 // 默认 KernelSchedule
           ▼
       CollectiveBuilder 产出  CollectiveMainloop（其 DispatchPolicy = 上面那个 policy）
           │
           ▼
       GemmUniversal<ProblemShape, CollectiveMainloop, CollectiveEpilogue, Scheduler>
           │ enable_if: is_base_of_v<KernelTmaWarpSpecialized, Mainloop::DispatchPolicy::Schedule>
           ▼
       选中 sm90_gemm_tma_warpspecialized.hpp 这个偏特化   ← 一个标签对应一个文件
```

如果你把 `KernelSchedule` 改成 `KernelTmaWarpSpecializedCooperative`，`enable_if` 就会改选 `sm90_gemm_tma_warpspecialized_cooperative.hpp`。**换一个标签 = 换一个内核实现，零运行时开销**。

#### 4.2.3 源码精读

**① kernel schedule 标签字典** —— 每个 kernel 文件对应一个标签：

[dispatch_policy.hpp:109-124](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/dispatch_policy.hpp#L109-L124) 定义基础内核调度标签，关键几个：

```cpp
struct KernelMultistage { };                  // 对应 sm70/sm80 多级 cp.async 内核
struct KernelTmaWarpSpecialized { };          // 对应 sm90_gemm_tma_warpspecialized.hpp
struct KernelTmaWarpSpecializedPingpong   { static constexpr int SchedulerPipelineStageCount = 0; };
struct KernelTmaWarpSpecializedCooperative { static constexpr int SchedulerPipelineStageCount = 0; };
```

注意 `Pingpong`/`Cooperative` 是 `KernelTmaWarpSpecialized` 之外**平级**的标签（不是子类），分别对应 Hopper 的两种 warp 特化策略：pingpong（两个 consumer 乒乓）与 cooperative（多个 warp group 协作）。后面的 FP8/MixedInput 等变体则用**继承**（如 `KernelTmaWarpSpecializedFP8FastAccum : KernelTmaWarpSpecialized`，见 [dispatch_policy.hpp:153](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/dispatch_policy.hpp#L153)），这样 `is_base_of_v` 既能精确匹配又能「归类」。

**② mainloop policy 携带 Schedule 标签** —— 这是分派链的「中间人」：

[dispatch_policy.hpp:259-270](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/dispatch_policy.hpp#L259-L270) 定义 `MainloopSm90TmaGmmaWarpSpecialized`：

```cpp
template<
  int Stages_,
  class ClusterShape_ = Shape<_1,_1,_1>,
  class KernelSchedule = KernelTmaWarpSpecializedCooperative   // ← 默认 cooperative
>
struct MainloopSm90TmaGmmaWarpSpecialized {
  constexpr static int Stages = Stages_;
  using ClusterShape = ClusterShape_;
  using ArchTag = arch::Sm90;
  using Schedule = KernelSchedule;                             // ← 暴露给 kernel 做分派
};
```

这个 policy 同时编码了「Hopper 架构 + TMA 搬运 + GMMA 计算 + warp 特化 + 流水线级数 + cluster 形状」全套信息，是 `CollectiveBuilder`（u2-l8）组装 collective 的依据。

**③ kernel 用 Schedule 做 enable_if 偏特化** —— 闭合分派链：

[sm90_gemm_tma_warpspecialized.hpp:59-72](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/kernel/sm90_gemm_tma_warpspecialized.hpp#L59-L72) 偏特化的第 5 个模板参数是：

```cpp
cute::enable_if_t<cute::is_base_of_v<
    cutlass::gemm::KernelTmaWarpSpecialized,
    typename CollectiveMainloop_::DispatchPolicy::Schedule>>
```

含义：**只有当传入 collective 的 `DispatchPolicy::Schedule` 是（或派生自）`KernelTmaWarpSpecialized` 时，这个偏特化才会被选中**。把 `KernelTmaWarpSpecializedCooperative` 换进去就不会命中它（cooperative 走另一个文件），这就是「一个标签一个文件」的实现原理。

#### 4.2.4 代码实践

**实践目标**：把「kernel schedule 标签 → kernel 文件」的对应关系亲手验证出来，建立策略字典的心智模型。

**操作步骤**：

1. 在仓库根目录执行（列出所有 kernel schedule 标签）：
   ```bash
   grep -nE '^struct Kernel[A-Za-z]+ \{ ?\};' include/cutlass/gemm/dispatch_policy.hpp | head -20
   ```
2. 选 3 个标签：`KernelTmaWarpSpecialized`、`KernelTmaWarpSpecializedPingpong`、`KernelTmaWarpSpecializedCooperative`。
3. 对每个标签，在 `include/cutlass/gemm/kernel/` 下找出它 `enable_if` 命中的那个 `.hpp` 文件：
   ```bash
   grep -rl "is_base_of_v<cutlass::gemm::KernelTmaWarpSpecializedCooperative" include/cutlass/gemm/kernel/
   ```
4. 再确认 `gemm_universal.hpp` 是否 `#include` 了你找到的这些文件。

**需要观察的现象**：

| 标签 | 命中的 kernel 文件 |
| --- | --- |
| `KernelTmaWarpSpecialized` | `sm90_gemm_tma_warpspecialized.hpp` |
| `KernelTmaWarpSpecializedPingpong` | `sm90_gemm_tma_warpspecialized_pingpong.hpp` |
| `KernelTmaWarpSpecializedCooperative` | `sm90_gemm_tma_warpspecialized_cooperative.hpp` |

**预期结果**：你会得到一张「标签 → 文件」对照表，直观验证「一个 kernel schedule 标签对应一个 kernel 偏特化文件」，且它们都通过 `gemm_universal.hpp` 的 `#include` 聚合（[gemm_universal.hpp:58-79](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/kernel/gemm_universal.hpp#L58-L79)）。若在本机无 GPU/无法编译，纯 `grep` 即可完成，结论「待本地验证」可省略。

#### 4.2.5 小练习与答案

**练习 1**：`KernelTmaWarpSpecializedFP8FastAccum` 用 `:` 继承自 `KernelTmaWarpSpecialized`（见 [dispatch_policy.hpp:153](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/dispatch_policy.hpp#L153)）。而 `KernelTmaWarpSpecializedPingpong` 却是平级独立标签。这两种写法分别带来什么效果？

> **答案**：继承让 `is_base_of_v<KernelTmaWarpSpecialized, FP8FastAccum>` 也为真——于是 FP8 内核既能命中「专属的 FP8 偏特化」（更精确），又能在没有 FP8 专属实现时「回退」到普通 TMA warp 特化分支。Pingpong/Cooperative 设为平级，是因为它们是**互斥的两种调度策略**，不该互相回退——pingpong 不该意外命中 cooperative 的文件。

**练习 2**：为什么 `Stages`、`ClusterShape` 写在 mainloop policy 里，而不写在 kernel schedule 标签里？

> **答案**：`Stages`/`ClusterShape` 是**数值/类型参数**，需要参与 collective 的内存布局、流水线深度计算；而 kernel schedule 标签只承担「选哪个 kernel 文件」的纯分派职责，保持空结构体最简单。把「策略分派」与「调优参数」分开，职责清晰。

---

### 4.3 GemmUniversalAdapter

#### 4.3.1 概念说明

到目前为止我们讲的都是**设备端**的 `kernel::GemmUniversal`。用户实际写代码时，用的是**主机端**的 `device::GemmUniversalAdapter<GemmKernel>`——它是一个**状态化、可复用的句柄**，负责：

1. 持有并管理 `kernel::Params` 的生命周期。
2. 把用户友好的 `Arguments`（指针、形状、alpha/beta）翻译成设备端紧凑的 `Params`。
3. 查询/设置共享内存上限、算 grid、用 `<<<>>>`（或 cluster launch）启动内核。
4. 同时兼容 2.x 的 `kernel::Gemm` 与 3.x 的 `kernel::GemmUniversal`——靠对 `GemmKernel` 做两个偏特化实现。

它是 2.x `device::Gemm`（见 u1-l6）在 3.x 的对应物，但内部用统一的「functor 模式」：`adapter(args)` → `initialize(args)` → `run()`。

#### 4.3.2 核心流程

```
device::GemmUniversalAdapter<GemmKernel> gemm;     // 构造（3.x 特化：基本空操作）
auto args = GemmKernel::Arguments{ mode, problem_shape, mainloop_args, epi_args, hw_info, sched_args };
gemm(args, workspace, stream);                       // operator() 一键启动
   ├─ initialize(args): params_ = GemmKernel::to_underlying_arguments(args, workspace)
   │                    + cudaFuncSetAttribute(动态 smem 上限)
   └─ run(params_):    算 grid/block/cluster → device_kernel<GemmKernel><<<...>>>(params_)
```

`IsCutlass3GemmKernel<GemmKernel>` 是分水岭：若 `GemmKernel` 别名了 `ProblemShape`（3.x 内核都别名），走 3.x 特化；否则走 2.x 特化（内部委托给 `GemmUniversalBase`，即 2.x 的老底座）。

#### 4.3.3 源码精读

**① 句柄的角色与双 API 兼容性** —— 头注释说得很清楚：

[gemm_universal_adapter.h:68-82](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/device/gemm_universal_adapter.h#L68-L82) 注释说明：`GemmUniversalAdapter` 是「围绕 `kernel::Gemm` 或 `kernel::GemmUniversal` 构建的状态化、可复用句柄」，管 `Params` 生命周期；并明说**通过对两种 kernel API 类型做偏特化来同时支持 2.x 与 3.0**，因此两个特化的行为可能不同。

**② 3.x vs 2.x 的偏特化分水岭** —— 靠一个元函数判定：

[gemm.h:134-141](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/gemm.h#L134-L141) 定义 `IsCutlass3GemmKernel`：默认 `false_type`，当且仅当 `GemmKernel` 有 `ProblemShape` 类型别名时偏特化为 `true_type`。**「有没有 `ProblemShape`」就是 3.x 与 2.x 的判定信号**。

[gemm_universal_adapter.h:122-126](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/device/gemm_universal_adapter.h#L122-L126) 3.x 偏特化的 `enable_if`：`IsCutlass3GemmKernel<...>::value` 为真才启用。对应的 2.x 偏特化见 [gemm_universal_adapter.h:629-633](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/device/gemm_universal_adapter.h#L629-L633)，条件取反（`not IsCutlass3GemmKernel`）。

**③ 3.x 特化的类型「反向映射」回 2.x 概念** —— 为兼容旧代码：

[gemm_universal_adapter.h:163-177](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/device/gemm_universal_adapter.h#L163-L177) 把 3.x 的 `TileShape` 映射回 2.x 的 `ThreadblockShape`，把 `DispatchPolicy::ClusterShape` 映射成 `ClusterShape`，把 `TiledMma::AtomShape_MNK` 映射成 `InstructionShape`。**3.x 其实没有「warp shape」这一等公民概念**（注释 [gemm_universal_adapter.h:182](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/device/gemm_universal_adapter.h#L182) 明说），这里只是尽力近似以满足老 API。

**④ Arguments → Params → run 的生命周期**：

[gemm_universal_adapter.h:311-356](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/device/gemm_universal_adapter.h#L311-L356) `initialize()`：先 `GemmKernel::initialize_workspace`，再 `params_ = GemmKernel::to_underlying_arguments(args, workspace)`（委托给内核把 `Arguments` 翻成 `Params`），然后若 smem ≥ 48KB 就 `cudaFuncSetAttribute(MaxDynamicSharedMemorySize)` 抬高上限。

[gemm_universal_adapter.h:374-375](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/device/gemm_universal_adapter.h#L374-L375) 静态 `run(Params&, ...)` 是真正启动点。它在 [gemm_universal_adapter.h:388](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/device/gemm_universal_adapter.h#L388) 起按架构分叉：`kMinComputeCapability >= 90` 走扩展启动 API（带 cluster），其中 SM90 用 `ClusterLauncher::launch`、SM100 用 `launch_with_fallback_cluster`（支持动态 cluster），更低架构走普通 `<<<>>>`。这套启动逻辑是 Hopper/Blackwell cluster 与 PDL（programmatic dependent launch）能跑起来的前提。

#### 4.3.4 代码实践

**实践目标**：理解 `GemmUniversalAdapter` 如何在「同一个类名」下，靠 `IsCutlass3GemmKernel` 自动切到 3.x 或 2.x 行为。

**操作步骤**：

1. 打开 `gemm_universal_adapter.h`，分别定位 3.x 偏特化（`enable_if_t<... IsCutlass3GemmKernel ...>::value>`，约第 122 行）与 2.x 偏特化（`not IsCutlass3GemmKernel`，约第 629 行）。
2. 对比两者的 `run()`：
   - 3.x 特化的 `run` 在哪一行算 `cluster` 形状、在哪一行调用 `device_kernel<GemmKernel>`？
   - 2.x 特化的 `run`（约第 760 行）委托给了谁？（提示：`underlying_operator_.run(...)`，即 `GemmUniversalBase`）。
3. 确认 3.x 特化里 `Arguments`/`Params` 都直接 `using = typename GemmKernel::Arguments/Params`（[gemm_universal_adapter.h:214-216](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/device/gemm_universal_adapter.h#L214-L216)），即「host 句柄不自己定义参数结构，完全借用内核的」。

**需要观察的现象**：

- 3.x 特化里**找不到** `ThreadblockSwizzle`、`Mma`、`Epilogue` 这些 2.x 概念的「真实」使用，只有为兼容而做的类型别名。
- 2.x 特化里有 `MapArguments`、`transposed_problem()`、`GemmUniversalBase`——这些是 2.x 专属逻辑。

**预期结果**：你会理解 **`GemmUniversalAdapter` 是一个「外壳的外壳」**：外层统一 API（`operator()(args)`），内层按 `IsCutlass3GemmKernel` 二分到完全不同的实现路径。若只读源码、不编译，「待本地验证」可略。

#### 4.3.5 小练习与答案

**练习 1**：为什么 3.x 特化的 `Arguments` 不自己定义字段，而是 `using Arguments = typename GemmKernel::Arguments`？

> **答案**：3.x 内核自己定义了 `Arguments`（含 `problem_shape`、`mainloop`、`epilogue`、`hw_info`、`scheduler`，见 [sm90_gemm_tma_warpspecialized.hpp:146-189](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/kernel/sm90_gemm_tma_warpspecialized.hpp#L146-L189)）。让 host 句柄直接借用它，保证「用户填的字段」与「内核读的字段」永远一致，不会因为两层各自维护而漂移。这是组合模型的又一体现：**host 句柄信任内核对自己的描述**。

**练习 2**：`run()` 里为什么要按 `kMinComputeCapability >= 90` 分叉启动方式？

> **答案**：Hopper（SM90）引入了 **thread block cluster**（多个 CTA 组成一组协同调度、共享分布式 smem）与 **PDL**（programmatic dependent launch）。普通 `<<<grid,block>>>` 无法表达 cluster，必须用扩展启动 API（`ClusterLauncher` / `cudaLaunchKernelEx`）。SM80 及以下没有 cluster，走普通启动即可。Blackwell（SM100）还支持运行期可变的动态 cluster，需带 fallback。所以启动代码必须按架构分叉。

---

### 4.4 与 2.x 的本质区别

#### 4.4.1 概念说明

有了前三节，现在可以把 3.x 与 2.x（u2-l6）做一次本质对比。两者的差异不是「多加了几层」，而是**内核组织哲学的根本转变**：

- **2.x 是「分层组装」**：device→kernel→threadblock→warp→thread 五层，每层一个类，靠 `DefaultMma`/`DefaultMmaCore` 按**架构手写偏特化森林**来配置。每加一个新架构，就要新写一批 `*_smXX.h` 配置文件。
- **3.x 是「组合 + 编译期分派」**：一个无状态内核组合两个 collective + 一个调度器；靠 **CuTe 代数（Layout/Tensor/Atom）** 做数据搬运与计算；靠 **policy 标签** 做策略选择；靠 **`CollectiveBuilder`** 自动组装。新增架构只需新增 collective 与 policy 标签，内核外壳不变。

#### 4.4.2 核心流程（对照表）

| 维度 | CUTLASS 2.x | CUTLASS 3.x |
| --- | --- | --- |
| 主入口 | `device::Gemm` | `device::GemmUniversalAdapter<kernel::GemmUniversal>` |
| 内核组织 | 五层分层类组装 | 无状态内核 = `CollectiveMainloop` + `CollectiveEpilogue` + `TileScheduler` |
| 问题表达 | 标量 `M,N,K,batch` + `TensorRef` | `ProblemShape` 元组 `(M,N,K,L)` + CuTe `Stride` |
| 配置机制 | `DefaultMma`/`DefaultMmaCore` 按架构偏特化森林 | `DispatchPolicy`（`Schedule` 标签）+ `CollectiveBuilder` 自动组装 |
| 数据搬运 | 手写 gmem→smem 迭代器 + smem→rmem | CuTe `copy`（自动选 `cp.async`/TMA）+ `TiledCopy` |
| 并行模型 | warp 内同步、单 warp group 既搬又算 | **warp specialization**：producer/consumer 分工 + 异步流水线 |
| 策略切换 | 换模板参数（如 `OpClassTensorOp`） | 换 `KernelSchedule` 标签（一个标签一个内核文件） |
| 架构覆盖 | Volta~Ampere（Hopper 部分） | Volta~Blackwell（SM100）/SM103/SM120 |

#### 4.4.3 源码精读

**① 同一个 `GemmUniversal` 类名服务两代 API** —— 这是兼容性的关键桥梁：

[gemm_universal_decl.h:42-49](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/kernel/gemm_universal_decl.h#L42-L49) 注释说明该声明支持 2.x 与 3.x：判断依据是「第一个类型是不是 `cute::tuple`」——3.x 传 `ProblemShape` 元组，2.x 传 `ThreadblockMma`（非元组）。配合 `gemm_universal_adapter.h` 里对 `IsCutlass3GemmKernel` 的偏特化，**用户层只需统一写 `GemmUniversalAdapter<...>`，底下自动分流**。

[gemm_universal.hpp:56-79](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/kernel/gemm_universal.hpp#L56-L79) 这个总装头把所有 3.x 偏特化（`sm70_gemm.hpp`、`sm90_gemm_tma_warpspecialized.hpp`、`sm100_*`、`sm103_*`、`sm120_*`）统统 `#include` 进来。**对比 2.x：2.x 的 `device::Gemm` 内部是固定的 `kernel::Gemm` + `DefaultMma` 组装链；3.x 则是「一个声明 + 一打偏特化」的扁平集合，靠 policy 标签在其中选一个**。

**② 3.x 内核不自己算乘加，2.x 内核层层下钻** —— 对比两边的 `operator()`：

- 2.x（u2-l6）：`kernel::Gemm::operator()` → 构造 `Mma mma(...)` → `mma(k_iters, accum, iterA, iterB, accum)`，层层下钻到 warp、再到 arch 指令。
- 3.x（本讲 4.1.3）：`GemmUniversal::operator()` 只调 `collective_mainloop.load/mma` 与 `collective_epilogue.store`，**乘加细节被封装进 collective**，由 collective 内部的 `TiledMma`（u2-l4）发出 `wgmma` 指令。

**③ 运行模式统一** —— 3.x 用一个枚举覆盖串行/Split-K/batched/grouped：

[gemm_enumerated_types.h:57-64](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/gemm_enumerated_types.h#L57-L64) `GemmUniversalMode` 有 `kGemm`/`kGemmSplitKParallel`/`kBatched`/`kArray`/`kGrouped`。**同一个 `GemmUniversal` 内核，靠 `mode` 与 `ProblemShape` 的 rank（是否带 L 批次维、是否是 ptr-array）就能表达普通 GEMM、batched、grouped、Split-K**——而 2.x 这些往往是不同的 device 类（`device::Gemm`、`device::GemmBatched`、`device::GemmGrouped`）。这是「universal（通用）」一名的由来。

#### 4.4.4 代码实践

**实践目标**：亲手对比 2.x 与 3.x 的内核「入口」差异，体会组织哲学的转变。

**操作步骤**：

1. 打开 2.x 内核 `include/cutlass/gemm/kernel/gemm.h`，找到 `operator()`，观察它如何构造 `Mma`（threadblock MMA）并调用 `mma(...)`——注意它**直接持有** `Mma` 与 `Epilogue` 成员类型。
2. 打开 3.x 内核 `sm90_gemm_tma_warpspecialized.hpp` 的 `operator()`（约第 430 行起），对比它只调 `collective_mainloop.*` 与 `collective_epilogue.*`。
3. 数一下两边的「模板参数个数」：2.x `kernel::Gemm` 通常十几个模板参数（含 `ThreadblockMma`/`Epilogue`/`Swizzle`/`StoreAccumulators`…），3.x `GemmUniversal` 只有 4 个实质参数（ProblemShape/Mainloop/Epilogue/Scheduler）。

**需要观察的现象**：

- 2.x 内核模板参数多、层层组装；3.x 内核模板参数少、组合两个 collective。
- 2.x 内核类型里能直接看到 `Mma mma;` 这种成员；3.x 内核是 `CollectiveMainloop collective_mainloop;`（一个 collective 对象，内部才藏着 `TiledMma`）。

**预期结果**：你会直观感到 3.x「更扁平、更组合化」——这正是它能用 `CollectiveBuilder` 自动组装、用 policy 标签快速切换的前提。

#### 4.4.5 小练习与答案

**练习 1**：为什么 3.x 能用「一个 `GemmUniversal`」覆盖 batched/grouped/Split-K，而 2.x 要拆成多个 device 类？

> **答案**：3.x 用 `ProblemShape` 元组与 `GemmUniversalMode` 表达问题维度与运行模式，`TileScheduler` 负责把任意模式映射成「CTA ↔ tile」的分配（包括 Split-K 的跨 CTA 归约、grouped 的问题数组索引）。内核主体（mainloop + epilogue）对这些模式是**不变**的，差异都吸收进 `ProblemShape`/`scheduler` 参数与调度器实现里。2.x 缺乏这套统一抽象，只能为每种模式写一个 device 类。

**练习 2**：用一句话概括 3.x 相对 2.x 的核心进步。

> **答案**：3.x 用 **CuTe 布局代数 + collective 组合模型 + policy 标签分派**，把 2.x「按架构手写偏特化森林」替换成「一套统一内核外壳 + 可自动组装的部件 + 编译期零开销策略切换」，从而能快速覆盖 Hopper/Blackwell 等新架构与新数据类型。

## 5. 综合实践

**任务**：把本讲三段式架构、policy 分派、host 句柄串成一条完整链路，画一张「从用户代码到 PTX」的端到端追踪图。

**操作步骤**：

1. 选定一个具体目标：Hopper 上 FP16、warp 特化、cooperative 调度的 GEMM。对应的 mainloop policy 是 `MainloopSm90TmaGmmaWarpSpecialized<Stages, ClusterShape, KernelTmaWarpSpecializedCooperative>`。
2. 沿下面这条链，在每个「→」处标注**涉及的源码文件与关键行号**：
   ```
   用户写 GemmUniversalAdapter<...> gemm;  gemm(args)
     →[host 句柄] GemmUniversalAdapter::operator() / initialize / run
        （gemm_universal_adapter.h，3.x 偏特化，约 122~575 行）
     →[Arguments→Params] GemmKernel::to_underlying_arguments
        （sm90_gemm_tma_warpspecialized.hpp:204-222）
     →[启动] device_kernel<GemmKernel><<<grid,block,smem,cluster>>>(params)
        （gemm_universal_adapter.h run() 内，cluster launch 分支）
     →[设备端入口] GemmUniversal::operator()
        （sm90_gemm_tma_warpspecialized.hpp，约 430 行起）
     →[policy 分派依据] DispatchPolicy::Schedule == KernelTmaWarpSpecializedCooperative
        （dispatch_policy.hpp:122-124 与 sm90_*_cooperative.hpp 的 enable_if）
     →[三段式] collective_mainloop.load / .mma + collective_epilogue.store
        （sm90_gemm_tma_warpspecialized.hpp:430-509，cooperative 版结构类似）
     →[CuTe 原子] TiledMma / TiledCopy（u2-l4）→ wgmma / TMA PTX
   ```
3. 在每个箭头旁，用一句话写清「这一步做了什么、靠哪个 policy 标签或类型别名做的选择」。

**预期结果**：你得到一张完整的「`device::GemmUniversalAdapter` → `kernel::GemmUniversal` → `CollectiveMainloop`/`CollectiveEpilogue` → CuTe 原子 → PTX」追踪图。这张图就是后续 [u2-l8 CollectiveBuilder](u2-l8-collective-builder.md)（自动填上中间几个部件）与 [u3-l1 异步流水线](u3-l1-async-pipeline.md)（展开 mainloop 内部 producer/consumer）的脚手架。若无法在本机编译运行，纯文档追踪即可，标注「待本地运行验证」。

## 6. 本讲小结

- 3.x 把一次 GEMM 内核建模为 **一个无状态的 `kernel::GemmUniversal` = `CollectiveMainloop` + `CollectiveEpilogue`(+ `TileScheduler`)**；内核本身只做组合与角色划分，不直接算乘加。
- **策略分派**靠 `dispatch_policy.hpp`：mainloop policy 内嵌一个 `Schedule` 标签，kernel 偏特化用 `enable_if_t<is_base_of_v<Schedule, ...>>` 选中自己——**一个 kernel schedule 标签对应一个 kernel 文件**，零运行时开销。
- **`GemmUniversalAdapter`** 是 host 状态化句柄，走 `operator()(args) → initialize(Arguments→Params) → run(<<<>>>)` 流程；靠 `IsCutlass3GemmKernel`（内核是否别名 `ProblemShape`）分两个偏特化，**同一个类名同时兼容 2.x/3.x**。
- 同一个 `GemmUniversal` 配合 `GemmUniversalMode`（`kGemm`/`kBatched`/`kGrouped`/`kGemmSplitKParallel`）与 `ProblemShape` 元组，**一套内核覆盖普通/batched/grouped/Split-K**，这是「universal」之名的由来。
- 与 2.x 的本质区别：**从「按架构手写偏特化森林的五层组装」转向「CuTe 代数 + collective 组合 + policy 标签分派」的统一外壳**，从而能快速扩展到 Hopper/Blackwell 等新架构。

## 7. 下一步学习建议

- **下一讲 [u2-l8 CollectiveBuilder 与主循环](u2-l8-collective-builder.md)**：本讲我们假设 `CollectiveMainloop` 已经「现成」。下一讲打开 `collective_builder.hpp`，看它如何根据数据类型/架构/`DispatchPolicy` **自动推断**出 mainloop 与 epilogue 的全部模板参数——这是用户不用手写十几个模板参数的关键。
- **[u2-l9 Hopper Warp-Specialized GEMM 实战](u2-l9-hopper-warp-specialized-gemm.md)**：用 example 49 把本讲的三段式真正跑起来，看 `CollectiveBuilder + GemmUniversal` 如何组装成完整可启动的 Hopper GEMM。
- **继续阅读源码**：精读 `sm90_gemm_tma_warpspecialized.hpp` 的 `operator()` 全文（producer/consumer 两支），再读 [u3-l1 异步流水线](u3-l1-async-pipeline.md) 展开 mainloop 内部的 producer/consumer 流水线细节。
