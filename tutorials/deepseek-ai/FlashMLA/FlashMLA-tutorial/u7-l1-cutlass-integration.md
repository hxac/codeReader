# CUTLASS 集成与分层结构

## 1. 本讲目标

本讲是专家层 Unit 7 的第一篇，带你进入 FlashMLA 在 SM100（Blackwell）上的 **dense MHA prefill** 实现。

学完后你应该能够：

1. 说清 FlashMLA 为什么把 dense prefill 建立在 CUTLASS 之上，以及 CUTLASS 3.x 的 **device → kernel → collective → common** 四层各自干什么。
2. 从 pybind 绑定 `dense_prefill_fwd` / `dense_prefill_bwd` 一路定位到 `FMHACutlassSM100FwdRun` / `FMHACutlassSM100BwdRun` 两个入口 `.cu`，并理解入口处如何按 `head_dim` 把运行时值编译期化。
3. 读懂 device 层 `FMHA<Kernel>` 模板「构造 Arguments → 转 Params → cluster launch」的统一套路。
4. 理解 **MLA 开关**：一个 `IsMla` 编译期布尔如何同时切换 tile 形状、mainloop、kernel schedule 与 problem shape，以及 MLA「K/V 同源」在代码里如何体现。

本讲只讲分层与开关，**不深入 mainloop 内部的 GEMM/softmax 流水**（那是 u7-l2 的任务），也不展开 tile scheduler 的跳块细节（u7-l3）。

## 2. 前置知识

阅读本讲前，建议你已经建立以下认知（来自前置讲义）：

- **调用链全景（u2-l1）**：Python 函数 → pybind 绑定 `flash_mla.cuda` → `csrc/api/*.h` 接口函数 → kernel 命名空间。dense prefill 是唯一不经过 `ImplBase` feature 派发器的路径，它直接调用 CUTLASS 的 `FMHACutlassSM100*Run` 入口。
- **DISPATCH 宏（u2-l3）**：把运行时值（如 `head_dim`）编译期化成模板常量，从而为每个合法取值实例化一份特化 kernel。本讲入口 `.cu` 里的 `if (head_dim_qk == ...) call_run_fmha_fwd(...)` 是同一种思想的「手写版」。
- **MLA 背景（u1-l1）**：MLA（多头潜在注意力）只缓存压缩后的潜在向量，K 与 V 同源。在 dense prefill 里 MLA 用 `head_dim_qk=192`（128 latent + 64 rope）、`head_dim_vo=128`（仅 latent）；普通 MHA 用 `128/128`。

如果你对 CUTLASS 完全陌生，只需先记住一句话：**CUTLASS 是 NVIDIA 的高性能矩阵乘 / GEMM 模板库，3.x 版本用「device 包 kernel 包 collective」的分层把 host 端启动逻辑与 device 端计算逻辑解耦**。本讲会把这个分层在 FMHA 场景下的具体形态讲清楚。

此外需要了解几个 Blackwell（SM100）硬件概念（u6-l3 已铺垫，这里复习）：

- **TMEM（Tensor Memory）**：SM100 新增的、靠近 Tensor Core 的片上存储，替代 Hopper 的寄存器累加器角色，存放 MMA 的输入/累加结果。
- **UMMA / UTCCP**：SM100 上操作 TMEM 的新指令族（ Unified MMA / Tensor Memory Copy ）。
- **CTA cluster**：把多个 CTA 编成一组、可共享 Distributed Shared Memory 的硬件单位（Hopper 起有，SM100 延续）。
- **WarpSpecialized（warp 专用化）**：一个 CTA 内的不同 warp 各司其职（Load / MMA / Softmax / Epilogue），用 pipeline barrier 串联，而不是所有 warp 跑同一份代码。

## 3. 本讲源码地图

本讲涉及的关键文件都在 `csrc/sm100/prefill/dense/` 下，目录本身就是一张分层图：

```
csrc/sm100/prefill/dense/
├── interface.h                       对外 C++ 接口声明（2 个 Run 函数）
├── fmha_cutlass_fwd_sm100.cu         fwd 入口（setup.py 编译单元）
├── fmha_cutlass_fwd_sm100.cuh        fwd 入头的模板装配（FwdRunner + run_fmha_fwd）
├── fmha_cutlass_bwd_sm100.cu         bwd 入口
├── fmha_cutlass_bwd_sm100.cuh        bwd 入头的模板装配（BwdRunner + run_fmha_bwd）
├── device/
│   ├── fmha.hpp                      device 层通用模板 FMHA<Kernel>
│   └── fmha_device_bwd.hpp           device 层 Sm100FmhaBwd（串联 3 个 kernel）
├── kernel/
│   ├── fmha_options.hpp              Tag/Option/find_option_t 选项机制
│   ├── fmha_tile_scheduler.hpp       通用 Persistent/Individual TileScheduler
│   ├── fmha_causal_tile_scheduler.hpp Causal 版 TileScheduler（含 TileQ/TileH）
│   ├── sm100_fmha_fwd_kernel_tma_warpspecialized.hpp        fwd kernel（operator()）
│   ├── sm100_fmha_bwd_kernel_tma_warpspecialized.hpp        bwd 主 kernel
│   ├── sm100_fmha_bwd_mla_kernel_tma_warpspecialized.hpp    bwd MLA 主 kernel
│   ├── fmha_kernel_bwd_sum_OdO.hpp   bwd 辅助：求 sum(O⊙dO)
│   └── fmha_kernel_bwd_convert.hpp   bwd 辅助：fp32→bf16 回写
├── collective/
│   ├── fmha_common.hpp               collective 公共类型
│   ├── fmha_fusion.hpp               mask 类型（Causal/Residual...）+ fusion
│   ├── sm100_fmha_load_tma_warpspecialized.hpp           普通 MHA 的 load
│   ├── sm100_fmha_mla_load_tma_warpspecialized.hpp       MLA 的 load
│   ├── sm100_fmha_fwd_mainloop_tma_warpspecialized.hpp   普通 MHA 的 mainloop
│   ├── sm100_fmha_mla_fwd_mainloop_tma_warpspecialized.hpp MLA 的 mainloop
│   └── sm100_fmha_fwd_epilogue_tma_warpspecialized.hpp   epilogue（写 O / LSE）
└── common/
    ├── mask.cuh                      MaskMode 枚举（kNone/kCausal/kCustom）
    ├── pipeline_mla.hpp              MLA 专用 pipeline
    └── utils.hpp / pow_2.hpp / helper.h / gather_tensor.hpp
```

一句话记忆：**入口在顶层（`.cu`/`.cuh`/`interface.h`），device 层管启动，kernel 层管一个 CTA 的调度与 warp 分工，collective 层管一段计算（load/mainloop/epilogue），common 层放共享小工具**。被 `setup.py` 实际编译的只有两个 `.cu`（fwd/bwd），其余都是 header-only 模板。

## 4. 核心概念与源码讲解

### 4.1 CUTLASS 分层总览

#### 4.1.1 概念说明

CUTLASS 3.x 把一个 GEMM（这里推广为 FMHA）拆成自上而下的四层：

| 层 | 命名空间 | 职责 | 本讲对应文件 |
|---|---|---|---|
| **device** | `cutlass::fmha::device` | host 端：校验、算 workspace、算 grid、设置 smem、cluster launch | `device/fmha.hpp`、`device/fmha_device_bwd.hpp` |
| **kernel** | `cutlass::fmha::kernel` | device 端一个 CTA 的总调度：warp 分工、SharedStorage 布局、调 tile scheduler、调 collective | `kernel/sm100_fmha_fwd_kernel_tma_warpspecialized.hpp` 等 |
| **collective** | `cutlass::fmha::collective` | 一段可复用的计算：load（TMA 取数）、mainloop（QK/PV 的 MMA + softmax）、epilogue（写 O/LSE） | `collective/sm100_fmha_*_tma_warpspecialized.hpp` |
| **common** | 全局/`common` | 跨层共享的小工具：mask 枚举、pipeline、对齐辅助 | `common/mask.cuh`、`common/pipeline_mla.hpp` |

这和你在 FlashMLA 解码侧看到的「一个 `.cuh` 写完整个 kernel」风格很不一样。解码 kernel（u3/u5）是手写 CUDA，作者直接控制每条指令；而 dense prefill 走 CUTLASS，**用模板参数把「做什么计算」与「怎么启动」分离**，换来的是：换一个 mask、换一个 head_dim、甚至换 MLA/MHA，只需要换模板参数，device 层启动代码原封不动。

#### 4.1.2 核心流程

从 host 到 device 的一次 fwd 调用，分层流程如下：

```
FMHACutlassSM100FwdRun(.cu, host)
  └─ run_fmha_fwd<>(.cuh, host)            按 head_dim 选 IsMla/Mask 模板
       └─ FwdRunner<...>::run(host)        装配 problem_shape / stride / Arguments
            └─ Operation = FMHA<Kernel>    (device 层)
                 ├─ op.can_implement(args)  → Kernel::can_implement
                 ├─ op.initialize(args)     → Kernel::to_underlying_arguments → Params
                 └─ op.run(stream)          → ClusterLauncher::launch(grid,cluster,block,smem)
                      └─ device_kernel<Kernel><<<...>>>(params)   (进入 device 端)
                           └─ Kernel::operator()(params, smem)     (kernel 层)
                                ├─ TileScheduler 遍历 tile
                                ├─ CollectiveMainloop::load / mma / softmax
                                └─ CollectiveEpilogue::store
```

关键点：**device 层完全泛型**，它不知道 FMHA 细节，只通过 `Kernel::` 的一组静态接口（`can_implement`/`get_workspace_size`/`get_grid_shape`/`to_underlying_arguments`/`SharedStorageSize`）与 kernel 层对话；kernel 层又把具体的 MMA/softmax 委托给 collective 层。这种「层层只通过静态接口对话」正是 CUTLASS 可组合的基础。

#### 4.1.3 源码精读

device 层的泛型模板 `FMHA<Kernel_>` 是分层的总枢纽。它的类型别名把 kernel 层的 `Arguments`/`Params` 透传出来，对外提供统一 API：

[device/fmha.hpp:L55-L65](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/dense/device/fmha.hpp#L55-L65) — device 层 `FMHA` 类模板，把 `Kernel::Arguments`/`Kernel::Params` 暴露为 User/Kernel 两套 API。

[device/fmha.hpp:L86-L108](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/dense/device/fmha.hpp#L86-L108) — `can_implement` / `get_workspace_size` / `get_grid_shape` 三个静态方法，全部转发给 `Kernel::` 的同名接口，device 层本身不含任何 FMHA 业务逻辑。

注意它的版权头：`Copyright (c) 2024 - 2025 NVIDIA CORPORATION`——这是从 CUTLASS 上游搬来的通用 device 层，FlashMLA 没有改动它的核心结构，只是在它之上「装」了自己的 kernel/collective。

#### 4.1.4 代码实践

**实践目标**：用肉眼确认「device 层是泛型的、不含 FMHA 细节」。

**操作步骤**：

1. 打开 `csrc/sm100/prefill/dense/device/fmha.hpp`。
2. 全文搜索 `fmha` / `attention` / `softmax` / `Q` / `K` / `V` 等业务词汇。
3. 观察该文件出现的所有类型都来自 `Kernel::`（如 `Kernel::Arguments`、`Kernel::Params`、`Kernel::SharedStorageSize`、`Kernel::MaxThreadsPerBlock`、`Kernel::ClusterShape`）。

**需要观察的现象**：除了命名空间 `cutlass::fmha::device` 和文件名带 `fmha`，**类体内几乎不出现任何 attention 专属逻辑**——它只是一个「持有 `Params`、能 launch 一个 `Kernel`」的通用容器。

**预期结果**：你会确认 device 层可以被任意 CUTLASS kernel 复用；FMHA 的全部个性都在 kernel/collective 层。这就是分层的价值：换 kernel 不动 device。

#### 4.1.5 小练习与答案

**练习 1**：如果要把这个 device 层复用到一个全新的 GEMM kernel，需要 kernel 层提供哪些静态接口？

**参考答案**：至少需要 `Arguments`、`Params`、`SharedStorageSize`、`MaxThreadsPerBlock`、`ClusterShape`、`ArchTag`、`can_implement`、`get_workspace_size`、`initialize_workspace`、`to_underlying_arguments`、`get_grid_shape`、`get_block_shape`。这些正是 device 层 `FMHA<Kernel>` 在 `run()`/`initialize()` 里引用到的全部 `Kernel::` 名字。

**练习 2**：device 层 `run()` 里有一段 `if constexpr(Kernel::ArchTag::kMinComputeCapability >= 90)`，为什么 dense prefill 走的是 cluster launch 分支？

**参考答案**：dense prefill 的 kernel `ArchTag = cutlass::arch::Sm100`（见 4.3.3），`Sm100` 的最小计算能力远 ≥ 90，故 `if constexpr` 命中 cluster launch 分支，用 `ClusterLauncher::launch` 而非普通 `<<<grid,block,smem,stream>>>`，以支持 CTA cluster。

---

### 4.2 fwd / bwd 入口与 head_dim 派发

#### 4.2.1 概念说明

`interface.h` 是这个子目录对外的唯一 C++ 门面，只声明两个自由函数——一个 fwd、一个 bwd。它们被 `csrc/api/dense_fwd.h` 直接 include，再由 pybind 注册成 Python 绑定 `dense_prefill_fwd` / `dense_prefill_bwd`（参见 u2-l1 的五个绑定清单）。

入口 `.cu` 文件做三件事：

1. **dtype 门禁**：当前只放行 `bf16 → bf16`。
2. **mask × varlen 二维派发**：用 `MaskMode`（`kCausal` / 否则 `ResidualMask`）和 `is_varlen` 组合成 4 个分支，靠「立即调用的 lambda」把运行时布尔编译期化（和 u2-l3 的 DISPATCH 宏同构）。
3. **head_dim 派发 → MLA 开关**：`head_dim_qk==192 && head_dim_vo==128` 走 MLA（`true_type`），`128/128` 走普通 MHA（`false_type`）。

bwd 入口几乎与 fwd 对称，额外差别是 bwd 的 `TileShape` 也随 `IsMla` 切换。

#### 4.2.2 核心流程

fwd 入口的派发是一个「双层 if + lambda」结构：

```
FMHACutlassSM100FwdRun(q,k,v,...,mask_mode_code,is_varlen):
  guard = CUDAGuard(q.device)          # 多 GPU 安全
  assert q.dtype == k.dtype
  if in==bf16 and out==bf16:
    apply_config = λfn:
      mask   = (mask_mode==kCausal) ? CausalMask<false>{} : ResidualMask{}
      varlen = is_varlen ? true_type{} : false_type{}
      fn(mask, varlen, Element{}, ElementOut{})
    apply_config( λ(mask, varlen, in, out):
      if head_dim_qk==192 and head_dim_vo==128:   # MLA
        call_run_fmha_fwd(..., true_type{},  ...)   # Mla=true_type
      elif head_dim_qk==128 and head_dim_vo==128: # 普通 MHA
        call_run_fmha_fwd(..., false_type{}, ...)   # Mla=false_type
      else: cout << "No kernel instantiated ..." )
  else: FLASH_MLA_ASSERT(false)
```

`call_run_fmha_fwd` 把 `Mla` 这个「空类型标签」转成 `static constexpr bool IsMla`，再连同 `Mask` 一起传给模板 `run_fmha_fwd<...>`——至此运行时信息全部「固化」成模板参数，后续每条路径都生成一份独立特化。

#### 4.2.3 源码精读

对外门面只有两个声明：

[interface.h:L5-L8](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/dense/interface.h#L5-L8) — `FMHACutlassSM100FwdRun` 声明，参数含 `mask_mode_code`、`softmax_scale`、`max_seqlen_q/kv`、`is_varlen`，注意 `mask_mode_code` 是 `int`（枚举的整数形式），到入口内部才 `static_cast<MaskMode>`。

[interface.h:L10-L14](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/dense/interface.h#L10-L14) — `FMHACutlassSM100BwdRun` 声明，比 fwd 多出 `d_o / dq / dk / dv` 四个梯度张量。

pybind 侧把它们注册成绑定（u2-l1 已建立的全景图）：

[api.cpp:L13-L14](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/api/api.cpp#L13-L14) — `m.def("dense_prefill_fwd", &FMHACutlassSM100FwdRun)` 与 `dense_prefill_bwd`，两个绑定一一对应 interface.h 的两个函数。

fwd 入口的「标签→常量」转换与 MLA/MHA 分支：

[fmha_cutlass_fwd_sm100.cu:L19-L28](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/dense/fmha_cutlass_fwd_sm100.cu#L19-L28) — `call_run_fmha_fwd` 把 `Mla`/`Mask`/`Varlen` 三个空类型标签转成 `static constexpr bool IsMla/IsVarlen/IsCausalMask`，并按 `IsCausalMask||IsVarlen` 选 `Option<Tag::kIsPersistent, false_type>`（否则 `true_type`），再调用模板 `run_fmha_fwd`。

[fmha_cutlass_fwd_sm100.cu:L49-L78](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/dense/fmha_cutlass_fwd_sm100.cu#L49-L78) — `apply_config` 完成 mask×varlen 二维派发，内层 lambda 按 `head_dim_qk/head_dim_vo` 二选一：`192/128 → true_type`（MLA），`128/128 → false_type`（MHA），其余打印 `No kernel instantiated`。这正是「运行时 head_dim 编译期化」的手写版 DISPATCH。

bwd 入口与 fwd 同构，但 `TileShape` 也由 `IsMla` 决定：

[fmha_cutlass_bwd_sm100.cu:L21](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/dense/fmha_cutlass_bwd_sm100.cu#L21) — bwd 的 `TileShape = conditional_t<IsMla, Shape<_64,_128,_192,_128>, Shape<_128,_128,_128,_128>>`。MLA 的 4 元 tile 形状 `(M=64, N=128, D_qk=192, D_vo=128)` 直接编码了「QK 用 192 维、PV 用 128 维」的 MLA 非对称。

注意 bwd 与 fwd 都只支持 `head_dim_qk∈{192,128}` 且 `head_dim_vo==128`；传别的维度只会触发 `std::cout << "No kernel instantiated"`（不会 abort，但下游 `can_implement` 通常会失败）。

#### 4.2.4 代码实践

**实践目标**：亲手追一遍「Python 一个参数 → 入口一个模板分支」的映射。

**操作步骤**：

1. 在 `flash_mla/flash_mla_interface.py` 中找到 `flash_attn_varlen_func`（或其 fwd 入口），看它传给 `flash_mla_cuda.dense_prefill_fwd` 的 `mask_mode_code` 与 `is_varlen` 来源。
2. 打开 `fmha_cutlass_fwd_sm100.cu` 的 `FMHACutlassSM100FwdRun`。
3. 假设一次调用是 `q.shape=[T, H, 192]`、`v.shape=[T, H_kv, 128]`、`mask_mode=kCausal`、`is_varlen=True`，沿代码画出它命中哪条分支。

**需要观察的现象**：`head_dim_qk = q.size(-1) = 192`、`head_dim_vo = v.size(-1) = 128` → 命中 MLA 分支 → `call_run_fmha_fwd(..., true_type{}, ...)` → `IsMla=true`。

**预期结果**：你得到一条确定的模板实例化路径 `run_fmha_fwd<bf16, bf16, kIsVarlen=true, kIsMla=true, CausalMask<false>, Option<kIsPersistent,false_type>>`。后续 4.4 会看到这条路径对应 `MainloopMla` + `Sm100MlaFwdCtxKernelWarpspecializedSchedule`。

**待本地验证**：若你手头有 SM100 机器并可编译，可在 `call_run_fmha_fwd` 入口加一行 `printf("IsMla=%d IsVarlen=%d\n", (int)IsMla, (int)IsVarlen);` 实测；无 GPU 时按上面静态推导即可。

#### 4.2.5 小练习与答案

**练习 1**：为什么入口用「空类型标签 `true_type{}`/`false_type{}`」而不是直接传 `bool`？

**参考答案**：因为 `bool` 是运行时值，无法做模板参数；`std::true_type`/`std::false_type` 是两种**不同类型**，可以作为模板非类型/类型参数，从而在 `call_run_fmha_fwd` 里用 `std::is_same_v<Mla, true_type>` 转成 `static constexpr bool IsMla`，把分支下沉到编译期，为每个 (MLA, Mask, Varlen) 组合生成一份独立特化 kernel。

**练习 2**：bwd 入口里 `head_dim_qk==128 && head_dim_vo==128` 走的是 `false_type`（MHA），但 bwd 暂不支持 GQA（u7-l4 会讲）。结合 `TileShape` 你能看出 MLA bwd 的 tile 是什么形状吗？

**参考答案**：MLA bwd 的 `TileShape = Shape<_64, _128, _192, _128>`，即 M=64、N=128、D_qk=192、D_vo=128；普通 MHA bwd 是 `Shape<_128, _128, _128, _128>`。MLA 的 D_qk=192（含 64 维 rope）而 D_vo=128（仅 latent），与 fwd 的非对称一致。

---

### 4.3 device 层：Arguments → Params → cluster launch

#### 4.3.1 概念说明

device 层是 CUTLASS 的「启动器」。它做四件事，顺序固定：

1. **can_implement**：让 kernel 自检参数是否合法（如对齐、维度上限）。
2. **get_workspace_size / initialize_workspace**：算临时显存需求并初始化（fwd 当前返回 0，bwd 需要工作区，见下）。
3. **initialize**：把用户友好的 `Arguments` 转成 kernel 友好的 `Params`（`Kernel::to_underlying_arguments`），并按需 `cudaFuncSetAttribute` 抬高动态 smem 上限。
4. **run**：算 grid/block/smem，用 `ClusterLauncher` 启动 kernel。

bwd 的 device 层 `Sm100FmhaBwd` 在此基础上多一步：**反向不是一个 kernel，而是三个 kernel 串联**——先 `sum_OdO`（求 Σ O⊙dO 和 scaled_lse），再主 bwd kernel（算 dQ/dK/dV），最后 `convert`（把 fp32 的 dQ_acc 下转回 bf16 写回 dQ）。device 层负责把它们按依赖串起来。

#### 4.3.2 核心流程

fwd device 层一次 `op.run()` 的内部流程：

```
op.run(args, workspace, stream):
  initialize(args, workspace, stream):
    Kernel::initialize_workspace(args, workspace, stream)   # fwd: no-op
    params_ = Kernel::to_underlying_arguments(args, workspace)
    if smem_size >= 48KB: cudaFuncSetAttribute(MaxDynamicSharedMemorySize)
  run(params_, stream):
    block = Kernel::get_block_shape()
    grid  = get_grid_shape(params)            # = Kernel::get_grid_shape
    if grid 任何一维==0: return Success        # 空问题直接跳过
    smem_size = Kernel::SharedStorageSize
    if ArchTag::kMinComputeCapability >= 90:   # SM100 命中
      cluster = size<0/1/2>(Kernel::ClusterShape)
      ClusterLauncher::launch(grid, cluster, block, smem_size, stream, &device_kernel<Kernel>, {&params})
    else:
      device_kernel<Kernel><<<grid,block,smem,stream>>>(params)
```

bwd device 层 `Sm100FmhaBwd::run(params, stream)` 的三段串联：

```
op_sum_OdO.run(stream)              # kernel 1: 求 sum(O⊙dO) 与 scaled_lse
cudaMemsetAsync(dQ_acc, 0, ...)     # 清零 fp32 dQ 累加器
op.run(stream)                      # kernel 2: 主 bwd（写 dK/dV、累加 dQ_acc）
op_convert.run(stream)              # kernel 3: dQ_acc(fp32) → dQ(bf16)
```

#### 4.3.3 源码精读

fwd device 层 `run()` 与 cluster launch：

[device/fmha.hpp:L205-L242](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/dense/device/fmha.hpp#L205-L242) — 静态 `run(Params&, stream)`：先 `grid==0` 早退，再按 `ArchTag::kMinComputeCapability>=90` 选 `ClusterLauncher::launch`（SM100 走这条）或普通三尖括号启动。

[device/fmha.hpp:L153-L187](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/dense/device/fmha.hpp#L153-L187) — `initialize`：`Kernel::to_underlying_arguments` 把 Arguments 转 Params；若 `smem_size >= 48KB` 调 `cudaFuncSetAttribute` 抬高动态共享内存上限（FMHA 的 smem 通常很大，几乎必命中）。`is_initialized` 用静态 bool 保证 `cudaFuncSetAttribute` 只设一次。

[device/fmha.hpp:L249-L262](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/dense/device/fmha.hpp#L249-L262) — 便捷重载 `run(args, workspace, stream)` / `operator()`：先 `initialize` 再 `run(params_)`，是 host 侧最常用的调用形式（fwd `.cuh` 里 `op.run(...)` 走的就是它）。

bwd device 层的三段串联与 workspace：

[device/fmha_device_bwd.hpp:L286-L312](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/dense/device/fmha_device_bwd.hpp#L286-L312) — `Sm100FmhaBwd::run`：依次 `op_sum_OdO.run` → `cudaMemsetAsync(dQ_acc)` → `op.run` → `op_convert.run`，三段之间靠 stream 顺序保证依赖。

[device/fmha_device_bwd.hpp:L220-L234](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/dense/device/fmha_device_bwd.hpp#L220-L234) — `get_workspace_size`：workspace 三块——`sum_OdO` 向量 `B*H*Q`、`scaled_lse` 向量 `B*H*Q`、fp32 的 `dQ_acc` 矩阵 `B*H*Q*D`，均按 `ElementAccumulator=float` 计字节数。u7-l4 会据此讲 workspace 字节计算。

[device/fmha_device_bwd.hpp:L103-L115](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/dense/device/fmha_device_bwd.hpp#L103-L115) — bwd 的 MLA 开关也在 device 层落地：`OperationMha` 用 `Sm100FmhaBwdKernelTmaWarpSpecialized`，`OperationMla` 用 `Sm100FmhaBwdMlaKernelTmaWarpSpecialized`，由 `IsMla` 选其一。这与 fwd 在 `.cuh` 里选 mainloop/kernel 是同一种「条件模板」模式。

> 说明：fwd 的 device 层 `FMHA<Kernel>` 是 CUTLASS 上游原样通用模板；bwd 的 `Sm100FmhaBwd` 是 FlashMLA 自己在 device 层做的薄封装（多包了一层「三 kernel 串联」）。两者都最终复用同一个 `FMHA<Kernel>` 来启动每一个子 kernel。

#### 4.3.4 代码实践

**实践目标**：确认 fwd 与 bwd 在 device 层「启动一个 kernel」的调用完全一致。

**操作步骤**：

1. 打开 `fmha_cutlass_fwd_sm100.cuh` 的 `FwdRunner::run` 末尾。
2. 打开 `fmha_cutlass_bwd_sm100.cuh` 的 `BwdRunner::run` 末尾。
3. 对比两处的「三件套」调用顺序。

**需要观察的现象**：两处都是 `CUTLASS_CHECK(op.can_implement(arguments));` → `CUTLASS_CHECK(op.initialize(arguments, ...));` → `CUTLASS_CHECK(op.run(at::cuda::getCurrentCUDAStream()));`。

**预期结果**：fwd 传 `nullptr` 作 workspace（因为 fwd `get_workspace_size` 返回 0），bwd 传 `workspace_buffer.data_ptr()`（bwd 需要三块 workspace）。两者共用同一套 device 层 API，差异只在「workspace 是否非空」。

#### 4.3.5 小练习与答案

**练习 1**：fwd 为什么可以传 `nullptr` 给 `initialize` 的 workspace？

**参考答案**：因为 fwd kernel 的 `get_workspace_size` 恒返回 0（见 `sm100_fmha_fwd_kernel_tma_warpspecialized.hpp` 的 `static size_t get_workspace_size(...) { return 0; }`），`initialize_workspace` 也是 no-op，所以 device 层 `initialize` 不会访问 workspace 指针，传 `nullptr` 安全。fwd `.cuh` 里也有注释 `// we don't use workspace in current version.`。

**练习 2**：bwd 的 `cudaMemsetAsync(dQ_acc, 0, ...)` 为什么出现在 `op_sum_OdO.run` 之后、主 `op.run` 之前？

**参考答案**：主 bwd kernel 对 dQ 是「原子累加」进 fp32 的 `dQ_acc`（多条 KV tile 都会贡献 dQ），因此必须先清零再启动主 kernel；而 `sum_OdO` 的产物（sum_OdO、scaled_lse）是主 kernel 的**输入**，必须先算出来。故顺序只能是 sum_OdO → memset(dQ_acc) → 主 bwd → convert。

---

### 4.4 MLA 开关：模板条件与 K/V 同源

#### 4.4.1 概念说明

MLA 与普通 MHA 在数学上的差别，在 dense prefill kernel 里被编码成一连串**条件模板**（`std::conditional_t`）。一个 `IsMla` 编译期布尔同时驱动五件事：

1. **tile 形状**：MLA 用 `Shape<_256, _128, HeadDim>`，其中 `HeadDim = Shape<_128, _64>`（latent+rope 复合维度）；MHA 用 `Shape<_256, _128, _128>`。
2. **mainloop**：`MainloopMla`（`Sm100MlaFwdMainloopTmaWarpspecialized`）vs `MainloopFmha`（`Sm100FmhaFwdMainloopTmaWarpspecialized`）。
3. **kernel schedule**：`Sm100MlaFwdCtxKernelWarpspecializedSchedule` vs `Sm100FmhaCtxKernelWarpspecializedSchedule`（寄存器配额不同）。
4. **problem shape**：MLA 的 head_dim 槽是 `tuple<dl, dr>`，MHA 是单个 `int`。
5. **load**：MLA 用 `Sm100MlaFwdLoadTmaWarpspecialized`，把 Q/K 的 latent 与 rope 分别处理。

**K/V 同源**的代码体现：在 MLA 路径里，`MlaOptions.dl`（latent 维）直接取自 `v.size(-1)`，`dr`（rope 维）取自 `q.size(-1) - v.size(-1)`。也就是说 **V 的维度就是 latent 维度**，rope 只挂在 Q/K 上、不进入 PV——这正是「V 是 K 的 latent 部分」在接口装配层的落地。

#### 4.4.2 核心流程

fwd `.cuh` 里 `FwdRunner` 的类型装配链（编译期）：

```
HeadDim       = Shape<_128, _64>                      # (latent=128, rope=64)
TileShapeMla  = Shape<_256, _128, HeadDim>            # MLA tile
TileShapeFmha = Shape<_256, _128, _128>               # MHA tile
TileShape     = conditional_t<kIsMla, Mla, Fmha>

MainloopMla = Sm100MlaFwdMainloopTmaWarpspecialized<..., TileShapeMla, ...>
OperationMla= FMHA<Sm100FmhaFwdKernelTmaWarpspecialized<
                    ProblemShape, MainloopMla, Epilogue, TileScheduler,
                    Sm100MlaFwdCtxKernelWarpspecializedSchedule>>   # MLA schedule
MainloopFmha = Sm100FmhaFwdMainloopTmaWarpspecialized<..., TileShapeFmha, ...>
OperationFmha= FMHA<Sm100FmhaFwdKernelTmaWarpspecialized<
                    ProblemShape, MainloopFmha, Epilogue, TileScheduler>>  # 默认 schedule

Mainloop = conditional_t<kIsMla, MainloopMla, MainloopFmha>
Operation = conditional_t<kIsMla, OperationMla, OperationFmha>
```

运行期装配 `MlaOptions`：

```
MlaOptions options;
options.b  = cu_seqlen_q.size(0) - 1;
options.h  = q.size(1);
options.h_k= k.size(1);
options.q  = q.size(0) / b;
options.k  = k.size(0) / b;
options.dl = v.size(-1);                  # latent = V 的维度（K/V 同源）
options.dr = q.size(-1) - v.size(-1);     # rope  = Q 多出来的维度
```

#### 4.4.3 源码精读

`FwdRunner` 的条件模板链与 problem shape 差异：

[fmha_cutlass_fwd_sm100.cuh:L52-L56](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/dense/fmha_cutlass_fwd_sm100.cuh#L52-L56) — `HeadDimLatent=_128`、`HeadDim=Shape<_128,_64>`、`TileShapeMla=Shape<_256,_128,HeadDim>`、`TileShapeFmha=Shape<_256,_128,_128>`，`TileShape` 由 `kIsMla` 选。注意 MLA 的 head_dim 是**复合类型**（latent+rope 两段）。

[fmha_cutlass_fwd_sm100.cuh:L58-L71](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/dense/fmha_cutlass_fwd_sm100.cuh#L58-L71) — `ProblemShapeRegular/Varlen`：MLA 的第 2 槽是 `tuple<int,int>`（dl,dr），MHA 是单个 `int`；varlen 时前两槽换成 `VariableLength`。problem shape 是 kernel 层理解「这次算什么」的唯一契约。

MLA vs MHA 的 mainloop / Operation 装配：

[fmha_cutlass_fwd_sm100.cuh:L96-L121](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/dense/fmha_cutlass_fwd_sm100.cuh#L96-L121) — `MainloopMla`/`OperationMla`（含 `Sm100MlaFwdCtxKernelWarpspecializedSchedule`）与 `MainloopFmha`/`OperationFmha`（用默认 schedule），最后两行 `Mainloop`/`Operation` 用 `conditional_t<kIsMla,...>` 二选一。MLA 的 Operation 显式传了第 5 个模板参数 `KernelSchedule`，MHA 不传（用默认 `Sm100FmhaCtxKernelWarpspecializedSchedule`）。

K/V 同源在 `MlaOptions` 装配处的体现：

[fmha_cutlass_fwd_sm100.cuh:L300-L310](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/dense/fmha_cutlass_fwd_sm100.cuh#L300-L310) — MLA 分支：`options.dl = v.size(-1)`（latent 维直接取自 V），`options.dr = q.size(-1) - v.size(-1)`（rope 维是 Q 比 V 多出来的部分）。对照 MHA 分支只有一个 `options.d = q.size(-1)`。这就是「V 即 K 的 latent 段」在代码里的硬证据。

MLA mainloop 与 load 的 head_dim 拆分：

[collective/sm100_fmha_mla_fwd_mainloop_tma_warpspecialized.hpp:L65-L90](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/dense/collective/sm100_fmha_mla_fwd_mainloop_tma_warpspecialized.hpp#L65-L90) — `Sm100MlaFwdMainloopTmaWarpspecialized` 从 `ComposedTileShape` 里拆出 `HeadDimLatent`/`HeadDimRope`，合成 `HeadDimQK = HeadDimLatent + HeadDimRope`。MLA mainloop 内部对 latent 与 rope 分别处理（rope 只进 QK 不进 PV），这是普通 `Sm100FmhaFwdMainloopTmaWarpspecialized` 没有的逻辑（详见 u7-l2）。

[collective/sm100_fmha_mla_load_tma_warpspecialized.hpp:L63-L91](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/dense/collective/sm100_fmha_mla_load_tma_warpspecialized.hpp#L63-L91) — `Sm100MlaFwdLoadTmaWarpspecialized`：声明 `TMA_Q/TMA_K/TMA_V` 三个 TMA 描述符与对应 `Params`。MLA 的 load 把 problem shape 的 `(dl,dr)` 合成回 `dl+dr` 喂给 QK 的 TMA（见该文件 `get<2,0>+get<2,1>` 拼接），而 V 的 TMA 只取 latent 段。

kernel 层的 `IsMla` 反射与 schedule 差异：

[kernel/sm100_fmha_fwd_kernel_tma_warpspecialized.hpp:L91-L127](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/dense/kernel/sm100_fmha_fwd_kernel_tma_warpspecialized.hpp#L91-L127) — `Sm100MlaFwdCtxKernelWarpspecializedSchedule`：与普通版 `Sm100FmhaCtxKernelWarpspecializedSchedule`（L52-L88）相比，warp 角色划分相同（Softmax0/1、Correction、MMA、Load、Epilogue），但**寄存器配额不同**——MLA 的 `NumRegsSoftmax=184`（普通版 192）、`NumRegsOther=48`（普通版 32）。这是 MLA 因 head_dim 更大、TMEM 压力不同而做的微调。

[kernel/sm100_fmha_fwd_kernel_tma_warpspecialized.hpp:L162](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/dense/kernel/sm100_fmha_fwd_kernel_tma_warpspecialized.hpp#L162) — kernel 用 `IsMla = std::is_same_v<KernelSchedule, Sm100MlaFwdCtxKernelWarpspecializedSchedule>` 反推出布尔，用于在 `operator()` 内做 `if constexpr` 分支（如 epilogue storage 复用策略）。

[kernel/sm100_fmha_fwd_kernel_tma_warpspecialized.hpp:L219-L221](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/dense/kernel/sm100_fmha_fwd_kernel_tma_warpspecialized.hpp#L219-L221) — `MaxThreadsPerBlock = NumWarps*32 = 16*32 = 512`，`ArchTag = cutlass::arch::Sm100`。这决定了 device 层走 cluster launch 分支。

> 旁注：mask 也是一层「开关」。`MaskMode`（`common/mask.cuh`）在入口被映射成 collective/fusion 里的 `CausalMask<false>` 或 `ResidualMask`（[fmha_fusion.hpp:L83](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/dense/collective/fmha_fusion.hpp#L83) 与 [L191](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/dense/collective/fmha_fusion.hpp#L191)），并进一步决定 `TileScheduler` 选 persistent 还是 causal-individual（见下条）。mask 的细节留待 u7-l3。

[kernel/fmha_options.hpp:L60-L83](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/dense/kernel/fmha_options.hpp#L60-L83) — `Tag` 枚举与 `Option<Tag,Value>` / `find_option_t`：这是 CUTLASS 风格的「编译期选项包」。`FwdRunner` 用 `Option<Tag::kIsPersistent, true/false_type>` 携带「是否 persistent」开关，`find_option_t` 在可变参数模板包里按 tag 查找默认值，从而让调用方以 `KernelOptions...` 形式传可选配置。

[fmha_cutlass_fwd_sm100.cuh:L82-L90](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/dense/fmha_cutlass_fwd_sm100.cuh#L82-L90) — `TileScheduler` 的选择：`kIsPersistent` 为真时按 mask 选 `CausalPersistentTileScheduler` 或 `PersistentTileScheduler`；否则按 `kIsMaskTileSchedulerValid` 选 `CausalIndividualTileScheduler` 或 `IndividualTileScheduler`。

[fmha_cutlass_fwd_sm100.cuh:L325-L334](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/dense/fmha_cutlass_fwd_sm100.cuh#L325-L334) — 运行期决定 `kIsMaskTileSchedulerValid`：当 `h % CausalIndividualTileScheduler::TileH(=8) == 0` 且 mask 是 causal 时，用更高效的 `CausalIndividualTileScheduler`（`TileQ=16, TileH=8`，见 [fmha_causal_tile_scheduler.hpp:L45-L49](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/dense/kernel/fmha_causal_tile_scheduler.hpp#L45-L49)），否则退回 `IndividualTileScheduler`。

#### 4.4.4 代码实践

**实践目标**：把 4.4 讲的「一个 IsMla 同时切换五件事」落成一张可核对的表。

**操作步骤**：

1. 打开 `fmha_cutlass_fwd_sm100.cuh`，定位 `FwdRunner` 模板参数 `kIsMla`。
2. 分别在 `kIsMla=true` 与 `kIsMla=false` 两种假设下，填下面这张表（每格写类型名/文件名）：

| 切换项 | MLA (`kIsMla=true`) | MHA (`kIsMla=false`) |
|---|---|---|
| `TileShape` | `Shape<_256,_128,Shape<_128,_64>>` | `Shape<_256,_128,_128>` |
| `ProblemShape` head_dim 槽 | `tuple<int,int>` (dl,dr) | `int` |
| `Mainloop` | `Sm100MlaFwdMainloopTmaWarpspecialized` | `Sm100FmhaFwdMainloopTmaWarpspecialized` |
| `KernelSchedule` | `Sm100MlaFwdCtxKernelWarpspecializedSchedule` | `Sm100FmhaCtxKernelWarpspecializedSchedule` |
| load collective | `Sm100MlaFwdLoadTmaWarpspecialized` | `Sm100FmhaLoadTmaWarpspecialized` |
| options 结构 | `MlaOptions`(dl,dr) | `FmhaOptions`(d) |

3. 在每格对应的源码行号处打勾核对。

**需要观察的现象**：六行切换项的「MLA 列」全部带 `Mla` 字样且来自独立文件，「MHA 列」则不带；它们都被 `conditional_t<kIsMla, ...>` 统一在 `FwdRunner` 内部。

**预期结果**：你会清楚看到——**MLA 不是运行时 if 分支，而是编译期就分裂成两套完全独立的模板实例**，运行时只走其中一条。这与 u2-l3 讲的 DISPATCH 宏思想完全一致，只是这里手写成 `conditional_t`。

**待本地验证**：若有可编译环境，可在 `FwdRunner` 内加 `static_assert(IsMla == kIsMla);` 之类断言确认；无 GPU 时静态核对即可。

#### 4.4.5 小练习与答案

**练习 1**：为什么 MLA 的 `head_dim` 要用复合类型 `Shape<_128, _64>` 而不是一个 `192`？

**参考答案**：因为 MLA 的 192 维内部结构不对称——前 128 维 latent 既参与 QK 又参与 PV（作为 V），后 64 维 rope 只参与 QK。复合类型让 mainloop/load collective 能在编译期分别取 `HeadDimLatent` 和 `HeadDimRope`，对两段做不同处理（如 V 的 TMA 只取 latent 段）。一个朴素的 `192` 无法表达这种「同一段 head_dim 内职责不同」的信息。

**练习 2**：`MlaOptions` 里 `dl = v.size(-1)`、`dr = q.size(-1) - v.size(-1)`。若用户误传 `q.size(-1)=192`、`v.size(-1)=192`，会发生什么？

**参考答案**：则 `dr=0`，`dl=192`。这与入口 `.cu` 的 MLA 分支判定（`head_dim_qk==192 && head_dim_vo==128`）不符——`head_dim_vo = v.size(-1) = 192 != 128`，于是不会命中 MLA 分支，而是落到 `else` 打印 `No kernel instantiated`。换言之，入口的 head_dim 校验本身就是「K/V 同源（V 是 K 的 latent 128 段）」的守门员。

**练习 3**：MLA 与 MHA 的 `KernelSchedule` 寄存器配额不同（`NumRegsSoftmax` 184 vs 192）。为什么 MLA 反而更小？

**参考答案**：MLA 的 head_dim_qk=192 比 MHA 的 128 大，mainloop 内每 warp 需要容纳更多 K/V 数据，TMEM 与寄存器压力更大；通过把 Softmax warp 的寄存器配额从 192 降到 184、`NumRegsOther` 从 32 提到 48，重新在 warp 间分配寄存器预算，避免 spill 并平衡 softmax/mma 两类 warp 的吞吐。寄存器配额是手调的产物（与 u8-l3 讲的 NVCC `register-usage-level` 调优同源）。

## 5. 综合实践

**综合任务**：画出 `csrc/sm100/prefill/dense` 的 include 依赖树，并标注每层职责——把本讲四节串成一张图。

请按以下要求完成：

1. **以 `interface.h` 为根**，向下画 include 关系，至少覆盖以下节点：
   - `interface.h`
   - `fmha_cutlass_fwd_sm100.cu` / `.cuh`
   - `device/fmha.hpp`
   - `kernel/sm100_fmha_fwd_kernel_tma_warpspecialized.hpp`、`kernel/fmha_options.hpp`、`kernel/fmha_causal_tile_scheduler.hpp`
   - `collective/sm100_fmha_mla_fwd_mainloop_tma_warpspecialized.hpp`、`collective/sm100_fmha_mla_load_tma_warpspecialized.hpp`、`collective/sm100_fmha_fwd_mainloop_tma_warpspecialized.hpp`、`collective/sm100_fmha_fwd_epilogue_tma_warpspecialized.hpp`、`collective/fmha_fusion.hpp`
   - `common/mask.cuh`
2. **在每个节点旁标注职责**（一句话，如「device 层：cluster launch」）。
3. **用虚线标出 MLA 开关**：在 `fmha_cutlass_fwd_sm100.cuh` 节点处，画出 `kIsMla=true` 走 `MainloopMla`/`MlaLoad`/`MlaSchedule`、`kIsMla=false` 走 `MainloopFmha`/普通 load/普通 schedule 的两条分支。
4. **标出 host/device 边界**：在 `.cu`（host）与 kernel `operator()`（device）之间画一条横线，注明「cluster launch 跨越此线」。

参考画法（文字版依赖树，你可以转成任意画图工具）：

```
interface.h  ──(被 api/dense_fwd.h include, api.cpp 注册为 dense_prefill_fwd/bwd)
  │
  ├── fmha_cutlass_fwd_sm100.cu          [入口 host: dtype门禁 + mask×varlen×head_dim 派发]
  │     └── fmha_cutlass_fwd_sm100.cuh   [装配 FwdRunner + run_fmha_fwd]
  │           ├── collective/fmha_fusion.hpp            [mask 类型 + fusion]
  │           ├── collective/sm100_fmha_fwd_epilogue_tma_warpspecialized.hpp  [写 O/LSE]
  │           ├── collective/sm100_fmha_fwd_mainloop_tma_warpspecialized.hpp  [普通 MHA mainloop] ─┐ MLA
  │           ├── collective/sm100_fmha_mla_fwd_mainloop_tma_warpspecialized.hpp [MLA mainloop] ───┤ 开关
  │           │     └── collective/sm100_fmha_mla_load_tma_warpspecialized.hpp  [MLA load, 拆 latent/rope]
  │           ├── kernel/fmha_options.hpp                [Tag/Option/find_option_t]
  │           ├── kernel/fmha_causal_tile_scheduler.hpp  [CausalIndividual/Persistent]
  │           ├── kernel/fmha_tile_scheduler.hpp         [Individual/Persistent]
  │           ├── kernel/sm100_fmha_fwd_kernel_tma_warpspecialized.hpp  [kernel: operator(), warp 分工]
  │           │     └── collective/fmha_common.hpp, common/pipeline_mla.hpp
  │           └── device/fmha.hpp                        [device: can_implement/initialize/run + cluster launch]
  │
  └── (bwd 对称: fmha_cutlass_bwd_sm100.cu/.cuh → device/fmha_device_bwd.hpp → kernel/sm100_fmha_bwd{_mla}_kernel_*.hpp
                + kernel/fmha_kernel_bwd_sum_OdO.hpp + kernel/fmha_kernel_bwd_convert.hpp)

common/mask.cuh  [MaskMode 枚举, 被入口 .cu 与 fusion 共用]

==== host / device 边界（ClusterLauncher::launch 跨越此线）====
device 端: Kernel::operator()(params, smem) → TileScheduler 遍历 → CollectiveMainloop{load,mma,softmax} → CollectiveEpilogue{store}
```

**验收标准**：

- 树中每条 include 边都能在对应文件的 `#include` 区找到（例如 `fmha_cutlass_fwd_sm100.cuh` 顶部确实 include 了 `device/fmha.hpp`、`kernel/sm100_fmha_fwd_kernel_tma_warpspecialized.hpp` 等，见 [fmha_cutlass_fwd_sm100.cuh:L3-L15](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/dense/fmha_cutlass_fwd_sm100.cuh#L3-L15)）。
- MLA 开关的两条分支指向不同文件（带 `mla` 字样 vs 不带）。
- 你能指着横线说清「host 在此构造 Params 并 launch，device 在此进入 `operator()`」。

> 无需运行 GPU；本实践是纯源码阅读型，目的是让你把「分层 + 开关」内化为一张空间地图，为 u7-l2（mainloop 内部）、u7-l3（tile scheduler + mask）、u7-l4（autograd + bwd workspace）打底。

## 6. 本讲小结

- FlashMLA 的 SM100 dense prefill 建立在 CUTLASS 3.x 之上，按 **device → kernel → collective → common** 四层组织：device 管启动、kernel 管 CTA 调度与 warp 分工、collective 管 load/mainloop/epilogue、common 放共享小工具。
- 入口是 `interface.h` 的 `FMHACutlassSM100FwdRun` / `FMHACutlassSM100BwdRun` 两个函数，被 pybind 注册为 `dense_prefill_fwd` / `dense_prefill_bwd`；入口 `.cu` 用「空类型标签 + 立即调用 lambda」把 `mask × varlen × head_dim` 编译期化。
- device 层 `FMHA<Kernel>` 是 CUTLASS 上游通用模板，固定走 `can_implement → initialize(Arguments→Params) → run(cluster launch)`；bwd 的 `Sm100FmhaBwd` 在其上多包了一层「sum_OdO → 主 bwd → convert」三 kernel 串联。
- **MLA 开关**是一个 `IsMla` 编译期布尔，同时切换 tile 形状、problem shape、mainloop、kernel schedule、load collective 五件事；MLA 的 head_dim 用复合类型 `Shape<_128,_64>` 表达「latent+rope」非对称。
- **K/V 同源**在代码里体现为 `MlaOptions.dl = v.size(-1)`（V 即 latent 段）、`dr = q.size(-1) - v.size(-1)`（rope 只挂 Q/K），入口的 `head_dim_vo==128` 校验正是这一约束的守门员。
- 被 `setup.py` 实际编译的只有 fwd/bwd 两个 `.cu`，其余全是 header-only 模板——分层让「换 mask / 换 head_dim / 换 MLA」只换模板参数，device 层启动代码不动。

## 7. 下一步学习建议

下一讲 **u7-l2 Mainloop collective 与 MLA fusion** 将钻进 collective 层内部，讲清：

- `Sm100FmhaFwdMainloopTmaWarpspecialized` 的 warp-specialized 流水（Load/MMA/Softmax/Epilogue 各自怎么跑）；
- `Sm100MlaFwdMainloopTmaWarpspecialized` 相对普通版独有的处理（latent/rope 分别装载、K/V 同源的 MMA 装配）；
- `fmha_fusion.hpp` 里 softmax 与 mask 的融合细节。

建议在进入 u7-l2 前，先回头对照本讲 §4.4 的「MLA vs MHA 切换表」，确认你能凭空列出两套 mainloop/load 的类名与文件位置——那样 u7-l2 读内部实现时不会迷路。如果想先换口味，也可以跳到 **u7-l3** 看 tile scheduler 与 mask 如何跳过上三角 tile，或 **u7-l4** 看 Python 端 `FlashAttnVarlenFunc` 的 autograd 上下文与 bwd workspace 字节计算。
