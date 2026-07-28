# MACA vs CUDA vs ROCm 差异对比

## 1. 本讲目标

本讲是 U7 Metax/MACA 后端系列的收口篇。前面四讲（u7-l1～u7-l4）分别拆解了 MACA 后端的架构、codegen、mfma intrinsics 与编译流水线；本讲换一个视角，**把 MACA 与 CUDA、ROCm 三个 GPU 后端并排放在一起横向对比**，回答三个问题：

1. 这三个后端在关键属性上有哪些差异（warp_size、target triple、MMA 命名、runtime API、执行后端）？
2. 这些差异为什么会出现——它们各自对应什么硬件事实？
3. TileLang 又是如何把这些差异**统一抽象**到一个公共分发表里，让上层编译流水线几乎不用关心后端细节？

学完后你应当能够：

- 不看资料说出三个后端的 warp_size、target triple、张量核指令前缀。
- 看懂 `target_utils` 这一层「公共分发 + 后端自有实现」的设计套路。
- 在排查「为什么同一份 kernel 在不同卡上行为不同」时，知道该去哪个文件定位。
- 为「新增一个 GPU 后端」做好心理预期：哪些点必须改，哪些点可以复用。

## 2. 前置知识

本讲假设你已经读过：

- **u5-l3 CUDA/HIP codegen 后端**：建立了「三后端结构对称、细节各有坑」的对照基线，知道了 codegen 类都 `final : public CodeGenC`、intrin_rule、`target_utils` 的存在。
- **u7-l1 MACA 后端架构总览**：MACA 是一等 target kind，`thread_warp_size=64`，`MACADeviceAPI`、`MACAModule`、`mcbin` 等概念。
- **u7-l2/u7-l3**：MACA codegen 与 mfma 指令命名（`16x16x{k_dim}{abbrv}`，`__builtin_mxc_mma_*`）。
- **u4-l2 tile 算子与 T.gemm 的分派**：GEMM 两级分派——C++ `ResolveGemmImpl(...).select_inst` 选指令键，Python 再映射到实现类。

下面只用三段话把最关键的术语复习一遍，便于对照。

**warp（线程束）** 是 GPU 上同步执行、可做 warp shuffle 的最小线程编组单位。一个线程块包含若干个 warp：\( W = \text{block\_size} / \text{warp\_size} \)。NVIDIA GPU 的 warp_size 恒为 32；AMD CDNA 与 MetaX GPU 为 64。这个数字直接决定「一个 warp 内有多少线程」「GEMM 累加器怎么切分给线程」，是后端差异里牵动最广的一个。

**target triple / mcpu** 是告诉底层编译器「为哪种指令集架构生成机器码」的描述符。CUDA 用 `arch=sm_XX`（如 `sm_90`），ROCm 用 `mcpu=gfx9XX` + `mtriple=amdgcn-amd-amdhsa`，MACA 用 `mcpu=xcoreXXXX` + `mtriple=mxc-metax-macahca`。它最终决定张量核指令、向量化宽度等能否被设备编译器接受。

**执行后端（execution backend）** 是 TileLang 把「生成的设备源码」变成「可调用 kernel」的那条胶水链路（如 `tvm_ffi`、`nvrtc`、`mcrtc`）。它和 target（编给谁）正交，但受各 target 的注册清单约束——同一个执行后端名不一定对每个 target 都登记。

## 3. 本讲源码地图

本讲对比所依据的关键源码文件如下：

| 文件 | 作用 |
| --- | --- |
| [src/maca/runtime/maca_target_kind.cc](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/maca/runtime/maca_target_kind.cc) | MACA target kind 的 C++ 注册（本仓库特有），定义 `mtriple`/`mcpu`/`thread_warp_size=64` 等属性与 canonicalizer |
| [src/cuda/target_utils.cc](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/cuda/target_utils.cc) | CUDA target 能力探测：架构代际、warp_size=32、async copy（arch≥80）、ldmatrix 等 |
| [src/rocm/target_utils.cc](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/rocm/target_utils.cc) | ROCm target 能力探测：CDNA/RDNA 判别、warp_size=64/32、async copy（gfx9≥94） |
| [src/maca/target_utils.cc](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/maca/target_utils.cc) | MACA target 能力探测：warp_size=64、async copy 恒为 true |
| [src/backend/common/target_utils.cc](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/backend/common/target_utils.cc) | **公共分发层**：`TargetHasAsyncCopy` 按 target kind 分派到上面三个实现 |
| [src/op/gemm.cc](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/op/gemm.cc) | GEMM 实现注册表与 `ResolveGemmImpl`——三后端指令分派的公共入口 |
| [src/cuda/op/gemm.cc](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/cuda/op/gemm.cc) / [src/rocm/op/gemm.cc](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/rocm/op/gemm.cc) / [src/maca/op/gemm.cc](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/maca/op/gemm.cc) | 三后端各自的 `SelectInst`——决定返回哪个 MMA 指令键 |
| [src/cuda/codegen/rt_mod_cuda.cc](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/cuda/codegen/rt_mod_cuda.cc) / [src/maca/codegen/rt_mod_maca.cc](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/maca/codegen/rt_mod_maca.cc) | `BuildTileLangCUDA`（调 nvcc 产出 ptx/cubin）与 `BuildTileLangMACA`（调 mxcc 产出 mcir/mcbin） |
| [src/maca/runtime/maca_device_api.cc](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/maca/runtime/maca_device_api.cc) | `MACADeviceAPI`：把通用 `DeviceAPI` 一一映射到 `mc*` 运行时（CUDA 对应 `cu*`、ROCm 对应 `hip*`） |
| [tilelang/cuda/execution_backend.py](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/cuda/execution_backend.py) / [tilelang/rocm/execution_backend.py](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/rocm/execution_backend.py) / [tilelang/maca/execution_backend.py](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/maca/execution_backend.py) | 三后端各自登记的执行后端清单 |

> 一个关键事实：CUDA 与 ROCm（hip）的 target kind 注册在上游 TVM 子模块（`3rdparty/tvm`）里，本仓库并不重新注册；而 **MACA 的 target kind 是本仓库新增的**，所以你能直接在 `src/maca/runtime/maca_target_kind.cc` 看到 `TVM_REGISTER_TARGET_KIND("maca", kDLMACA)`。这也是为什么 metax 分支的 diff 里 target kind 注册只出现在 maca 一侧。

## 4. 核心概念与源码讲解

### 4.1 warp_size 与 target triple：最显眼的硬件分水岭

#### 4.1.1 概念说明

`warp_size`（线程束大小）和 `target triple`/`mcpu` 是两个「写在 target 属性里、却牵动整条编译链」的值。

- **warp_size** 决定每个 warp 里有多少线程参与同步计算（warp shuffle、张量核 lane 映射、reduction 树的宽度都以此为基准）。CUDA 恒为 32；AMD CDNA（`gfx9xx`）与 MetaX 均为 64；AMD RDNA（`gfx11xx`/`gfx12xx`）又回到 32。MACA 在 target kind 注册时把默认值钉死为 64。
- **target triple / mcpu** 决定设备编译器（nvcc / hipcc / mxcc）按哪套指令集生成机器码。MACA 的 `mtriple=mxc-metax-macahca`、`mcpu=xcoreXXXX` 是本仓库的 canonicalizer 自动补齐的。

这两个值的差异不是「风格不同」，而是「硬件确实不同」：MetaX 与 AMD CDNA 的张量核以 64 线程为一组组织数据，而 NVIDIA 以 32 线程为一组。因此同一份 fragment tile 在两种卡上分给线程的方式不同（见 u7-l3 的 lane 映射）。

#### 4.1.2 核心流程

target 属性的生命周期分两步：

1. **注册默认值**：`TVM_REGISTER_TARGET_KIND` 声明 kind 支持哪些属性及其默认值。CUDA/hip 在上游 TVM 注册；MACA 在本仓库注册，默认 `thread_warp_size=64`。
2. **规范化（canonicalize）**：用户构造 target 后，canonicalizer 回调补齐缺失属性。MACA 的 `UpdateMACAAttrs` 会把 `mtriple` 钉成 `mxc-metax-macahca`、把 `mcpu` 探测成设备架构（兜底 `xcore1000`）。

至于运行时「这个 target 的 warp_size 是多少」，则由各后端的 `TargetXxxGetWarpSize` 纯函数回答，并经 FFI 暴露给 Python。

```text
Target 构造
   └─ canonicalizer（maca: UpdateMACAAttrs 补 mtriple/mcpu）
        └─ 运行时查询
             ├─ C++ TargetXxxGetWarpSize(target) → int
             └─ FFI: tl.TargetCudaGetWarpSize / tl.TargetRocmGetWarpSize / tl.TargetMacaGetWarpSize
```

#### 4.1.3 源码精读

**MACA 的 target kind 注册（本仓库特有）**——注意第 67 行把 `thread_warp_size` 默认值设为 64，第 40 行把 triple 钉死：

[src/maca/runtime/maca_target_kind.cc:59-71](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/maca/runtime/maca_target_kind.cc#L59-L71) 注册 MACA kind 与默认属性（`max_num_threads=1024`、`thread_warp_size=64`、`set_default_keys({"maca","gpu"})`）。

[src/maca/runtime/maca_target_kind.cc:38-57](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/maca/runtime/maca_target_kind.cc#L38-L57) `UpdateMACAAttrs` canonicalizer：`CheckOrSetAttr("mtriple","mxc-metax-macahca")`，并把 `mcpu` 探测成 `xcore*` 架构串（兜底 `xcore1000`）。

**三后端 warp_size 查询函数对照**——三个文件结构完全对称，差别只在返回值：

[src/cuda/target_utils.cc:92-95](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/cuda/target_utils.cc#L92-L95) CUDA 恒返回 32（连 `target` 参数都不用）。

[src/rocm/target_utils.cc:67-72](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/rocm/target_utils.cc#L67-L72) ROCm 按 CDNA 判别：CDNA 返回 64，否则 32。

[src/maca/target_utils.cc:41-44](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/maca/target_utils.cc#L41-L44) MACA 恒返回 64。

把三者压缩成一句话：**CUDA=32、MACA=64、ROCm 看 CDNA(64)/RDNA(32)**。一个线程块的 warp 数因此不同。例如 256 个线程的块：

\[ W_{\text{cuda}} = 256/32 = 8 \quad\text{个 warp},\qquad W_{\text{maca}} = 256/64 = 4 \quad\text{个 warp} \]

warp 数翻倍/减半会改变 GEMM 的 warp 划分（`GemmWarpPolicy` 切输出 tile 的粒度），这就是 u7-l3 提到的「MACA 因 warp_size=64、`k_n_per_warp=16` 得到与 CUDA 不同的 warp 划分」的根因。

#### 4.1.4 代码实践

这是一个**源码阅读型实践**（不需要 GPU）。

1. **目标**：亲手验证三后端 warp_size 的取值与取值依据。
2. **步骤**：
   - 打开本讲「源码地图」里的三个 `target_utils.cc`，分别定位 `TargetCudaGetWarpSize` / `TargetRocmGetWarpSize` / `TargetMacaGetWarpSize`。
   - 打开 [src/maca/runtime/maca_target_kind.cc:59-71](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/maca/runtime/maca_target_kind.cc#L59-L71)，确认 `thread_warp_size` 的 `DefaultValue`。
   - 用计算器算：对 `threads=128` 的 kernel，CUDA 与 MACA 各启动几个 warp。
3. **需要观察的现象**：CUDA 函数体直接 `return 32;`（参数被 `(void)target;` 显式忽略），ROCm 函数体里有 `if (TargetIsCDNA(target))` 分支，MACA 函数体直接 `return 64;`。
4. **预期结果**：CUDA=4 个 warp，MACA=2 个 warp（以 128 线程计）。这说明同一份 GEMM kernel 在两张卡上累加器切分给线程的方式不同。
5. 待本地验证：若有 MetaX 卡，可用 `mcDeviceGetAttribute(..., mcDeviceAttributeWarpSize, ...)` 打印实际 warp_size（见 4.3 的 device_api）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 CUDA 的 `TargetCudaGetWarpSize` 可以忽略 `target` 参数，而 ROCm 的不行？

> **答案**：NVIDIA 全系 GPU 的 warp_size 都是 32，是一个跨架构常量；AMD 则分 CDNA（64）与 RDNA（32）两套，必须看 `mcpu` 才能判断，所以 ROCm 函数要先 `TargetIsCDNA(target)`。

**练习 2**：如果有人想在 MACA 上「假装 warp_size=32」来复用一套 CUDA 布局推断，会出什么问题？

> **答案**：fragment 的 lane 映射（u7-l3）是按 64 线程一一分配的，强行当 32 会导致一半线程拿不到数据、张量核指令的 lane 坐标错位，结果数值错乱。warp_size 是贯穿 layout/codegen 的硬约束，不能随意改。

---

### 4.2 MMA 指令命名与分派：同一份 T.gemm，不同硬件指令

#### 4.2.1 概念说明

`T.gemm` 是 target 无关的 DSL 写法，但落到不同卡上会发射完全不同的张量核指令。TileLang 用「**指令键（instruction key）**」这一层中间表示把两者解耦：

- C++ 侧 `SelectInst` 依 target + 算子形状返回一个**字符串键**（如 `"cuda.wgmma"`、`"rocm.mfma"`、`"maca.mma"`）。
- Python 侧 `resolve_gemm_impl` 再把这个键映射到具体的发射器实现类。

三个后端的张量核指令「家族」不同，命名前缀也不同：

| 后端 | 指令家族 | SelectInst 可能返回的键 |
| --- | --- | --- |
| CUDA | MMA / WGMMA / TCGEN05 | `cuda.mma`、`cuda.wgmma`、`cuda.tcgen05` |
| ROCm | MFMA（CDNA）/ WMMA（RDNA） | `rocm.mfma`、`rocm.wmma` |
| MACA | mfma（MetaX 的矩阵乘加） | `maca.mma`（恒定） |

> 注意一个容易混淆的点：MACA 的 **SelectInst 键** 叫 `maca.mma`，但它底层发射的**硬件指令家族**是 mfma（Python 侧用 `T.tvm_mfma` builtin，C++ codegen 印成 `__builtin_mxc_mma_*`，见 u7-l3）。即「分派键名」与「硬件指令名」不必相同。CUDA 也类似：键叫 `cuda.wgmma`，硬件指令是 `wgmma`。

#### 4.2.2 核心流程

GEMM 的指令分派是一条「先选后端、再选指令」的两级链：

```text
T.gemm(A,B,C)                              # target 无关
   └─ C++ GemmNode
        └─ ResolveGemmImpl(target)         # 第 1 级：按 target 选后端实现
             │   （遍历注册表，首个 match_target 命中）
             └─ impl.select_inst(op, block_size, target)   # 第 2 级：选指令键
                  ├─ cuda: tcgen05 / wgmma / mma（看架构代际 + 算子标注）
                  ├─ rocm: mfma（CDNA）/ wmma（RDNA）
                  └─ maca: 恒为 maca.mma
                       └─ Python resolve_gemm_impl(key) → 发射器类
```

第 1 级由 `ResolveGemmImpl` 在一个全局注册表里线性查找；第 2 级是各后端自己的 `SelectInst` 静态方法。

#### 4.2.3 源码精读

**公共注册表与查找**——所有后端的 GEMM 实现都 `RegisterGemmImpl` 进同一张表，`ResolveGemmImpl` 遍历找首个匹配：

[src/op/gemm.cc:37-54](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/op/gemm.cc#L37-L54) `ResolveGemmImpl`：遍历 `GemmImplRegistry()`，对每个 `impl.match_target(target)` 命中者取首个；若多个命中或无命中则报错。

**CUDA 的 SelectInst（三选一，最复杂）**——CUDA 会综合「用户显式标注（isWgmma_/isTcgen05_）」与「硬件能力（AllowTcgen5Mma/AllowWgmma）」选 tcgen05 → wgmma → mma：

[src/cuda/op/gemm.cc:33-35](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/cuda/op/gemm.cc#L33-L35) 三个指令键常量 `cuda.mma`/`cuda.wgmma`/`cuda.tcgen05`。

[src/cuda/op/gemm.cc:266-287](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/cuda/op/gemm.cc#L266-L287) `SelectInst` 主体：先尊重显式标注，否则按「能上 tcgen05 就上、否则 wgmma、否则 mma」的能力降级顺序返回。

**ROCm 的 SelectInst（看 CDNA/RDNA）**：

[src/rocm/op/gemm.cc:102-115](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/rocm/op/gemm.cc#L102-L115) `SelectInst`：CDNA 返回 `rocm.mfma`，RDNA 返回 `rocm.wmma`，其余报错。

**MACA 的 SelectInst（恒定，最简单）**：

[src/maca/op/gemm.cc:142-144](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/maca/op/gemm.cc#L142-L144) `SelectInst` 直接 `return kMacaMMA;`（`kMacaMMA = "maca.mma"`，见同文件 [src/maca/op/gemm.cc:26](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/maca/op/gemm.cc#L26)）。

对比三者可见一条规律：**后端支持的张量核代际越多，SelectInst 越复杂；MACA 目前只有一套 mfma 指令，所以恒定返回一个键**。这也意味着 MACA 暂时没有 CUDA 那种「自动从 mma 升级到 wgmma/tcgen05」的能力降级链。

#### 4.2.4 代码实践

1. **目标**：亲手列出三后端 `SelectInst` 的返回值与触发条件。
2. **步骤**：
   - 打开上面三个 `SelectInst` 的源码片段。
   - 画一张表：「后端 → 输入条件 → 返回键」。
   - 对照 [src/op/gemm.cc:37-54](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/op/gemm.cc#L37-L54) 确认这些 `SelectInst` 是怎么被「按 target 选出来」的。
3. **需要观察的现象**：CUDA 分支最多（显式标注 + 能力降级），ROCm 两个分支，MACA 无分支。
4. **预期结果**：CUDA 三键、ROCm 两键、MACA 一键。
5. 待本地验证：用 `target={"kind":"maca"}` 编译一个 GEMM（无设备时用「只取源码」模式，见 u3-l3），在生成的源码里搜索 `__builtin_mxc_mma_`，确认指令家族是 mfma 而 SelectInst 键是 `maca.mma`。

#### 4.2.5 小练习与答案

**练习 1**：为什么 CUDA 的 `SelectInst` 里要先判断 `op.isWgmma_`/`op.isTcgen05_` 再判断硬件能力？

> **答案**：`isWgmma_`/`isTcgen05_` 来自用户显式写 `T.wgmma_gemm`/`T.tcgen05_gemm`（u4-l2），属于「用户点名要某指令」。优先尊重用户意图；若用户点名的指令在该卡上不支持，就走 `FatalWgmmaUnavailable` 报错，而不是悄悄降级。硬件能力判断只在用户没点名（普通 `T.gemm`）时介入做自动选择。

**练习 2**：`maca.mma`（键名）和 mfma（指令家族）为什么不统一？

> **答案**：键名是 TileLang 内部的分派标识，与硬件指令名是两层抽象。统一与否是命名风格问题；当前 MACA 沿用了「键名 = `<backend>.mma`」的命名惯例（与 `cuda.mma` 对称），而把 mfma 留给硬件 builtin 层。读源码时要分清自己在看哪一层。

---

### 4.3 运行时 API：`cu*` / `hip*` / `mc*` 的对称映射

#### 4.3.1 概念说明

GPU 运行时（runtime）是一组 C API，负责设备管理、显存分配、kernel 启动、流/事件同步等。三家各有各的运行时库：

- NVIDIA CUDA Runtime：函数前缀 `cu`/`cuda`（如 `cuMalloc`、`cudaMemcpy`）。
- AMD ROCm HIP：函数前缀 `hip`（如 `hipMalloc`、`hipMemcpy`），HIP 本身就是刻意模仿 CUDA 命名的设计。
- MetaX MACA：函数前缀 `mc`（如 `mcMalloc`、`mcMemcpy`、`mcModuleLaunchKernel`）。

TVM 抽象出 `DeviceAPI` 基类，把「分配显存」「拷贝」「设设备」等通用操作定义成虚函数；每个后端写一个子类把虚函数映射到自家 `cu*`/`hip*`/`mc*`。CUDA 与 ROCm 的 `DeviceAPI` 子类在上游 TVM 里，**MACA 的 `MACADeviceAPI` 是本仓库新增的**。三者结构完全对称，因此对照阅读非常直观：函数名里的前缀几乎可以一一替换。

#### 4.3.2 核心流程

运行时 API 在两个时机被调用：

1. **编译期（codegen 收尾）**：`BuildTileLang*` 把设备源码交给设备编译器，产出二进制。
   - CUDA：源码 → `nvcc` → `ptx`/`cubin`，包进 `CUDAModule`。
   - ROCm：源码 → `hiprtc`/`hipcc` → `hsaco`，包进 `ROCmModule`。
   - MACA：源码 → `mxcc` → `mcir`/`mcbin`，包进 `MACAModule`。
2. **运行期（kernel 执行）**：`DeviceAPI` 子类管理显存与流，Module 子类加载二进制并 `LaunchKernel`。

```text
设备源码 (C/C++)
   └─ BuildTileLang*  ── 设备编译器 ──▶  二进制
        CUDA: nvcc → ptx/cubin      (CUDAModule)
        ROCm: hipcc → hsaco         (ROCmModule)
        MACA: mxcc → mcir/mcbin     (MACAModule)
              └─ 运行期: DeviceAPI 子类 + Module.LaunchKernel
                  CUDA: cu*   ROCm: hip*   MACA: mc*
```

#### 4.3.3 源码精读

**MACA 设备编译链路（mxcc → mcbin）**——与 CUDA 的 `nvcc → ptx/cubin` 结构同构：

[src/maca/codegen/rt_mod_maca.cc:101-140](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/maca/codegen/rt_mod_maca.cc#L101-L140) `BuildTileLangMACA`：用 `CodeGenTileLangMACA` 印源码 → `tilelang_callback_maca_postproc` 拦截 → `tilelang_callback_maca_compile` 调 `mxcc` 编译，返回值首字符为 `/` 则是 `mcir`（源码），否则是 `mcbin`（二进制）→ `MACAModuleCreate`。

对照 [src/cuda/codegen/rt_mod_cuda.cc:97-138](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/cuda/codegen/rt_mod_cuda.cc#L97-L138) `BuildTileLangCUDA`：同样的「印码 → postproc → `tilelang_callback_cuda_compile` 调 nvcc → `ptx`/`cubin` → `CUDAModuleCreateWithFallback`」。两段代码几乎可以逐行对照，差别只在编译器名（mxcc/nvcc）、产物格式（mcbin/cubin）、Module 工厂。

**MACA 的运行时 API（`mc*`）**——`MACADeviceAPI` 把每个虚函数映射到一个 `mc*` 调用：

[src/maca/runtime/maca_device_api.cc:43-50](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/maca/runtime/maca_device_api.cc#L43-L50) `SetDevice` → `mcSetDevice`、`GetAttr` 里用 `mcGetDeviceCount`/`mcDeviceGetAttribute` 查询属性（含 `mcDeviceAttributeWarpSize`）。

[src/maca/runtime/maca_device_api.cc:150-164](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/maca/runtime/maca_device_api.cc#L150-L164) `AllocDataSpace` → `mcMalloc`（设备）/`mcMallocHost`（主机），并强制 256 字节对齐（`256 % alignment == 0`）。

把这三类调用替换前缀就是三后端的对照表：`mcSetDevice↔cudaSetDevice↔hipSetDevice`、`mcMalloc↔cudaMalloc↔hipMalloc`、`mcMemcpyAsync↔cudaMemcpyAsync↔hipMemcpyAsync`、`mcStreamSynchronize↔cudaStreamSynchronize↔hipStreamSynchronize`。HIP 之所以和 CUDA 长得几乎一样，是因为 AMD 设计 HIP 时刻意模仿了 CUDA 命名；MACA 的 `mc*` 也遵循同样的命名风格，使迁移成本最低。

> 一个 MACA 独有的细节：`MACADeviceAPI::AllocDataSpace` 里 `TVM_FFI_ICHECK_EQ(256 % alignment, 0U)` 强制 256 字节对齐（[src/maca/runtime/maca_device_api.cc:152-153](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/maca/runtime/maca_device_api.cc#L152-L153)）。这是 MetaX 硬件对显存对齐的硬要求，CUDA/ROCm 没有这条断言——它是后端差异在 runtime 层的一个具体落点。

#### 4.3.4 代码实践

1. **目标**：建立 `mc*` ↔ `cuda*` ↔ `hip*` 的运行时 API 对照表。
2. **步骤**：
   - 打开 [src/maca/runtime/maca_device_api.cc](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/maca/runtime/maca_device_api.cc)，把里面所有 `mc*` 调用列出来。
   - 对每个 `mc*`，写出对应的 `cuda*` 与 `hip*` 猜测（凭命名相似度）。
   - 重点看 `MACATimerNode`（同文件第 284-317 行）如何用 `mcEventRecord`/`mcEventElapsedTime` 测时，对照 CUDA 的 event 计时范式。
3. **需要观察的现象**：几乎所有 `mc*` 都能一一对应到 `cuda*`/`hip*`；对齐断言是 MACA 独有。
4. **预期结果**：得到一张「操作 → mc/cuda/hip 三列」的对照表（见第 5 节综合实践）。
5. 待本地验证：若有 MetaX 卡，运行 `mcGetDeviceCount`/`mcGetDeviceProperties` 打印设备名与显存。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `MACADeviceAPI` 要在 `AllocDataSpace` 里强制 256 字节对齐，而 CUDA 不用？

> **答案**：不同 GPU 对显存基地址对齐要求不同。MetaX MACA 要求 256 字节对齐（代码里用 `256 % alignment == 0` 断言），不满足会导致 `mcMalloc` 后访存越界或 kernel 启动失败；CUDA 的 `cudaMalloc` 自身保证足够对齐，故无需额外断言。这种「硬件对齐要求」差异是 runtime 层最典型的后端特化点。

**练习 2**：`mxcc` 产出的 `mcbin` 与 `nvcc` 产出的 `cubin` 在 TVM Module 体系里扮演的角色是否相同？

> **答案**：相同。两者都是「设备编译器产出的、可直接被运行时加载启动的二进制」，分别由 `MACAModule`/`CUDAModule` 持有，对外都呈现为 TVM `Module` 接口（`LaunchKernel` 等）。差异只在二进制格式与加载它的 Module 子类。

---

### 4.4 执行后端：tvm_ffi / nvrtc / mcrtc 的正交清单

#### 4.4.1 概念说明

「执行后端（execution backend）」解决的问题是：**拿到设备源码（或二进制）后，用什么方式把它变成一个可被 Python 直接调用的 kernel 对象？** 它和 target（编给谁）是两个正交维度，但每个 target 会登记一张「支持哪些执行后端」的清单。

三后端的执行后端清单（从各自的 `execution_backend.py` 读出）：

| target kind | 登记的执行后端 | 默认（首个 tvm_ffi） |
| --- | --- | --- |
| `cuda` | `tvm_ffi`、`nvrtc`、`cython`、`cutedsl` | `tvm_ffi` |
| `hip` | `tvm_ffi`、`cython` | `tvm_ffi` |
| `maca` | `tvm_ffi`、`mcrtc`、`cython`、`cutedsl` | `tvm_ffi` |

几个要点：

- 三者都以 `tvm_ffi` 为默认且始终可用（它走 TVM 的统一 FFI，无需外部 JIT 编译器）。
- **JIT 编译器后端各不相同**：CUDA 有 `nvrtc`（NVIDIA Runtime Compilation），MACA 对应有 `mcrtc`（MetaX Runtime Compilation），ROCm 的 hip 没有登记单独的 rtc 后端（依赖 `tvm_ffi` 走预编译路径）。
- 注意 target kind 名与 Python 包名不必一致：ROCm 后端的代码在 `tilelang/rocm/` 目录，但它登记的 target kind 是 `"hip"`（见 [tilelang/rocm/execution_backend.py:6-11](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/rocm/execution_backend.py#L6-L11)）。MACA 两边同名（包 `tilelang/maca/`、kind `"maca"`）。

#### 4.4.2 核心流程

执行后端的选择发生在 JIT 编译收尾：

```text
target + 用户指定的 execution_backend（或默认）
   └─ 在该 target 的登记清单里查 supports_target / is_available
        ├─ tvm_ffi：恒可用，走 TVM FFI + Module（无需外部编译器）
        ├─ nvrtc（CUDA）/ mcrtc（MACA）：调本卡 JIT 编译器即时编译源码
        └─ cython / cutedsl：备选胶水
             └─ auto：取首个 is_available 为真的后端
```

每个 `ExecutionBackendSpec` 有两个关键谓词：`supports_target`（该后端是否服务于这个 target）与 `is_available`（当前环境是否装齐了它的依赖，如 nvrtc/mcrtc 动态库）。auto 选择会取「`supports_target` 为真」里第一个「`is_available` 为真」的。

#### 4.4.3 源码精读

**MACA 登记四个执行后端**——其中 `mcrtc` 是 MACA 专属：

[tilelang/maca/execution_backend.py:34-58](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/maca/execution_backend.py#L34-L58) 依次登记 `tvm_ffi`（默认，带 `enable_host_codegen=True, enable_device_compile=True`）、`mcrtc`、`cython`、`cutedsl`。`_is_mcrtc_available` 从 `tilelang.jit.adapter.mcrtc` 探测可用性。

对照 [tilelang/cuda/execution_backend.py:34-58](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/cuda/execution_backend.py#L34-L58) CUDA 版：结构与 MACA 完全对称，只是把 `mcrtc` 换成 `nvrtc`（`_is_nvrtc_available` 从 `tilelang.jit.adapter.nvrtc` 探测）。

[tilelang/rocm/execution_backend.py:6-11](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/rocm/execution_backend.py#L6-L11) ROCm 版：只登记 `tvm_ffi` 与 `cython`，**没有独立的 rtc 后端**，target kind 写的是 `"hip"`。

> 一个来自 u3-l3 的事实值得在此复核：MACA 的 `mcrtc` 虽然被登记（`_is_mcrtc_available` 存在），但当前环境里它**通常不可用**，所以实际默认落到 `tvm_ffi`。这说明「登记」与「可用」是两回事——`register_execution_backend` 只声明支持，真正用不用还得看 `is_available` 的运行时判断。

#### 4.4.4 代码实践

1. **目标**：从源码读出三后端的执行后端清单与默认项。
2. **步骤**：
   - 打开三个 `execution_backend.py`，数每个文件里 `register_execution_backend` 被调几次、名字各是什么。
   - 对每个后端，确认它的 `supports_target` 谓词（CUDA/MACA 都区分 plain 与 cutedsl 两种 target；ROCm 不区分）。
   - 找到「默认」依据：第一个 `tvm_ffi` spec 带 `enable_host_codegen=True, enable_device_compile=True`，且无 `is_available` 门控，故始终可用。
3. **需要观察的现象**：CUDA 与 MACA 的文件几乎逐行对称（nvrtc↔mcrtc），ROCm 明显更短。
4. **预期结果**：CUDA=4 项、MACA=4 项、ROCm=2 项；默认都是 `tvm_ffi`。
5. 待本地验证：在装好 tilelang 的环境里 `import tilelang` 后，用一个 maca target 编译 kernel，打印实际选中的 execution backend（若 `mcrtc` 不可用应回退 `tvm_ffi`）。

#### 4.4.5 小练习与答案

**练习 1**：为什么 ROCm（hip）没有登记 rtc 类执行后端，而 CUDA/MACA 都有？

> **答案**：CUDA 有 `nvrtc`、MetaX 有 `mcrtc`，都是各自官方提供的「在主机端把源码即时编译成二进制」的库；ROCm 侧 TileLang 当前选择走 `tvm_ffi` 的预编译路径（用 `hipcc`/`hiprtc` 在 codegen 阶段完成编译），没再登记单独的 rtc 执行后端。是否登记取决于该后端是否需要一个独立于 `tvm_ffi` 的 JIT 胶水层。

**练习 2**：执行后端和 target 是完全正交的吗？举一个「同一执行后端名、不同 target」与「同一 target、多执行后端」的例子。

> **答案**：基本正交但受清单约束。`cython` 这个名字同时出现在 cuda/hip/maca 三张清单里（同名同机制，服务于不同 target）——这是「同名不同 target」；`maca` 这一个 target 下同时登记了 `tvm_ffi`/`mcrtc`/`cython`/`cutedsl` 四个——这是「同 target 多后端」。正交性体现在：选 target 决定「编给谁」，选 execution backend 决定「编完怎么跑」，两者可独立组合（只要清单允许）。

---

## 5. 综合实践

把四个模块串起来，完成本讲的总任务：**制作一张三后端对比表，并说明这些差异如何在 `target_utils` 的统一分发中抽象**。

### 5.1 三后端对比表（请动手填完后对照）

按下表逐项填空，填不出的回到对应模块的源码精读复查：

| 对比维度 | CUDA | ROCm（hip） | MACA |
| --- | --- | --- | --- |
| target kind 名 | `cuda` | `hip` | `maca` |
| 设备类型常量 | `kDLCUDA` | `kDLROCM` | `kDLMACA` |
| **warp_size** | 恒 32 | CDNA 64 / RDNA 32 | 恒 64 |
| target kind 注册位置 | 上游 TVM | 上游 TVM | **本仓库** `src/maca/runtime/maca_target_kind.cc` |
| triple / mcpu | `arch=sm_XX` | `mcpu=gfx9XX`，triple 来自上游 TVM | `mtriple=mxc-metax-macahca`，`mcpu=xcoreXXXX` |
| canonicalizer | 上游 TVM | 上游 TVM | `UpdateMACAAttrs`（补 mtriple/mcpu） |
| 张量核指令家族 | MMA/WGMMA/TCGEN05 | MFMA(CDNA)/WMMA(RDNA) | mfma |
| SelectInst 键 | `cuda.mma`/`cuda.wgmma`/`cuda.tcgen05` | `rocm.mfma`/`rocm.wmma` | `maca.mma`（恒定） |
| 设备编译器 | `nvcc` | `hipcc`/`hiprtc` | `mxcc` |
| 编译产物 | `ptx`/`cubin` | `hsaco` | `mcir`/`mcbin` |
| Module 子类 | `CUDAModule` | `ROCmModule` | `MACAModule` |
| runtime API 前缀 | `cu*`/`cuda*` | `hip*` | `mc*` |
| runtime 独有约束 | — | — | 显存 256 字节对齐 |
| 执行后端清单 | tvm_ffi, nvrtc, cython, cutedsl | tvm_ffi, cython | tvm_ffi, mcrtc, cython, cutedsl |
| 默认执行后端 | `tvm_ffi` | `tvm_ffi` | `tvm_ffi` |
| 异步拷贝能力探测 | arch≥80 | gfx9≥94（CDNA） | 恒 true |

### 5.2 这些差异如何被「统一分发」抽象

关键洞见：**差异本身散落在三个后端各自的 `target_utils.cc`，但「该问哪个后端」的决策被收敛到公共层 `src/backend/common/target_utils.cc`**。以异步拷贝为例：

[src/backend/common/target_utils.cc:15-26](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/backend/common/target_utils.cc#L15-L26) `TargetHasAsyncCopy`：用 `TargetIsCuda`/`TargetIsRocm`/`TargetIsMaca` 三连判，分别转给 `TargetCudaHasAsyncCopy`/`TargetRocmHasAsyncCopy`/`TargetMacaHasAsyncCopy`，最后兜底 `return false`。这是一个标准的「公共分派 + 后端自有实现」模式。

三个被分派到的实现，条件各不相同：

- [src/cuda/target_utils.cc:85-90](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/cuda/target_utils.cc#L85-L90) CUDA：`arch ≥ 80`（Ampere 起才有 cp.async）。
- [src/rocm/target_utils.cc:54-65](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/rocm/target_utils.cc#L54-L65) ROCm：解析 `mcpu` 的 `gfx9` 后两位，`≥ 94` 才算支持。
- [src/maca/target_utils.cc:35-39](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/maca/target_utils.cc#L35-L39) MACA：只要 `TargetIsMaca` 就返回 `true`（无版本门控）。

这套抽象的意义在于：**上层（pipeline pass、copy 算子）只需调用一个 `tl.TargetHasAsyncCopy(target)` FFI，不必关心三后端的判别条件差异**。同样的「公共分发」套路也出现在 GEMM 分派（`ResolveGemmImpl` 遍历注册表）、codegen 注册（`target.build.tilelang_*` 三个并列全局函数）、执行后端登记（每个 target 一张清单）。

> 关于异步拷贝的一个口径说明：`TargetMacaHasAsyncCopy` 在 C++ 能力探测层恒返回 true（声明硬件具备能力）；但该能力是否在软件流水线中**真正启用**，是 pipeline 规划阶段另行决定的——MACA 当前在规划阶段走同步拷贝（详见 u4-l4）。即「能力探测」与「是否使用」是两层，本表与公共分发描述的是前者。

### 5.3 动手验证（可选）

如果有 MetaX 硬件（无硬件则跳过）：

1. 用 `target={"kind":"maca","mcpu":"xcore1000"}` 编译一个 GEMM。
2. 打印 `kernel.get_kernel_source()`，搜索 `__builtin_mxc_mma_`（确认 mfma 家族）与 `mc` 前缀的同步原语。
3. 用 `get_profiler().do_bench()` 量延迟，对照同等形状在 CUDA 卡上的结果，体会 warp_size=64 对 warp 划分与性能的影响。

## 6. 本讲小结

- **warp_size 是最显眼的分水岭**：CUDA 恒 32，MACA 恒 64，ROCm 看 CDNA(64)/RDNA(32)；它牵动 GEMM 的 warp 划分与 fragment lane 映射。
- **MACA 的 target kind 是本仓库新增的**（`src/maca/runtime/maca_target_kind.cc`），canonicalizer `UpdateMACAAttrs` 把 triple 钉成 `mxc-metax-macahca`、mcpu 探测成 `xcoreXXXX`；CUDA/hip 的 kind 来自上游 TVM。
- **同一份 T.gemm 落到三套张量核指令**：CUDA 走 mma/wgmma/tcgen05（能力降级），ROCm 走 mfma(CDNA)/wmma(RDNA)，MACA 恒走 mfma（键名 `maca.mma`）。
- **运行时 API 一一对应**：`cu*`/`cuda*` ↔ `hip*` ↔ `mc*`；MACA 独有显存 256 字节对齐断言。
- **执行后端与 target 正交**：三者默认 `tvm_ffi`；CUDA 有 nvrtc、MACA 有 mcrtc，ROCm 不登记 rtc；ROCm 包名 `rocm` 但 target kind 是 `hip`。
- **差异被公共层收敛**：`src/backend/common/target_utils.cc` 用 `TargetIs*` 三连判把后端差异分发掉，上层只调一个 FFI（如 `tl.TargetHasAsyncCopy`），这是 TileLang 多后端架构的核心抽象手法。

## 7. 下一步学习建议

- **横向应用**：带着本讲的对比表回到 u9-l1「扩展：新增目标后端」，你会发现「新增一个后端」本质上就是把本表每一行复制一份——注册 target kind、写 target_utils 探测、登记执行后端、写 codegen + rt_mod + device_api + module。MACA 就是这份「新增后端」的最佳模板。
- **纵向深挖**：若想理解 warp_size=64 如何具体改变 fragment 布局，复习 u7-l3 的 lane 映射与 u4-l3 的 Layout Inference。
- **性能视角**：带着「MACA 当前走同步拷贝」这一结论回到 u4-l4 与 u8 系列，思考在没有异步拷贝重叠时，persistent kernel、swizzle、splitk 这些策略对 MACA 的相对价值如何变化。
- **读源码顺序建议**：`src/backend/common/target_utils.cc`（公共分发）→ 三个 `target_utils.cc`（对照差异）→ 三个 `op/gemm.cc` 的 `SelectInst`（指令分派）→ 三个 `execution_backend.py`（执行后端清单）。
