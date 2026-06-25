# 内核入口：SM90 FP8 GEMM 1D1D

## 1. 本讲目标

从本讲开始，我们真正「下钻」到 GPU 上运行的设备 kernel。前面几讲（u1~u5）一直在讲宿主侧：Python 调用 → C++ 派发 → JIT 编译 → 启动句柄。本讲要回答的问题是：

> 当一个 cubin 被 `cuLaunchKernelEx` 推上 Hopper(SM90) 的 SM 后，**这个 kernel 内部到底长什么样**？

学完本讲，你应当能够：

1. 读懂 `sm90_fp8_gemm_1d1d_impl` 这个设备 kernel 的整体骨架——它的模板参数、`__launch_bounds__` 与线程划分。
2. 手算它把一块共享内存划分成了哪些区域（D / A / B / SFA / SFB / barrier），并写出每块的大小公式。
3. 说清楚 TMA 线程（负责搬数据）与 math 线程（负责算 WGMMA）是怎么分工、怎么用 mbarrier 做软件流水线同步的。
4. 解释 `DG_STATIC_ASSERT(BLOCK_K == 128, ...)` 这条编译期断言的根因。

本讲只聚焦 SM90 的 FP8 稠密 GEMM 内核 `sm90_fp8_gemm_1d1d.cuh`，不涉及 SM100、MoE 分组或 Mega MoE（那些留待 u6-l2/u7/u8）。

## 2. 前置知识

本讲假设你已建立以下认知（来自前置讲义，这里只做最小回顾）：

- **宿主 / 设备分层**：宿主代码（`csrc/`，CPU 侧）负责派发与 JIT 编译；设备代码（`deep_gemm/include/`，GPU 侧）是高度参数化的 `.cuh` 模板（见 u1-l3）。
- **JIT 代码生成**：宿主侧 `SM90FP8Gemm1D1DRuntime::generate_impl` 用 `fmt::format` 把 BLOCK_M/N/K 等 17 个编译期常量填进一段极薄的 `.cu`，靠取函数地址强制实例化模板，编译成 cubin（见 u3-l2）。
- **TMA 描述符**：宿主侧用 `cuTensorMapEncodeTiled` 预先构造好 A/B/SFA/SFB/CD 五个 TMA 描述符，随内核参数一起传入；设备侧只需发 `tma::copy` 即可把全局内存瓦片异步搬进共享内存（见 u4-l2）。
- **compiled_dims 覆盖**：模板里有 `SHAPE_M/N/K` 三个编译期形状，`0` 表示该维留运行时、非 `0` 表示编译期特化；设备侧用 `SHAPE_* != 0 ? SHAPE_* : shape_*` 做覆盖（见 u3-l2、u5-l3）。
- **FP8 逐块缩放**：FP8 范围窄，必须按 K 轴每 128 个通道一块、记录一个缩放因子 SF（SM90 用 FP32），最终结果要乘上 `sfa * sfb`（见 u2-l2）。

如果你对「warp（32 线程）/ warp-group（4 个 warp=128 线程）/ tensor core MMA / 共享内存 bank conflict」这些 GPU 基础概念还不熟，建议先补一下 CUDA 编程基础再读源码精读部分。

## 3. 本讲源码地图

| 文件 | 角色 | 本讲用到的部分 |
|---|---|---|
| [`deep_gemm/include/deep_gemm/impls/sm90_fp8_gemm_1d1d.cuh`](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/impls/sm90_fp8_gemm_1d1d.cuh) | **设备 kernel 主体**，本讲核心 | 全文 |
| [`csrc/jit_kernels/impls/sm90_fp8_gemm_1d1d.hpp`](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/impls/sm90_fp8_gemm_1d1d.hpp) | 宿主 Runtime，负责生成/启动 kernel | 模板参数映射、TMA 描述符构造 |
| [`deep_gemm/include/deep_gemm/mma/sm90.cuh`](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/mma/sm90.cuh) | WGMMA 指令封装与 selector | `FP8MMA` 的 `M/K/kNumAccum` 常量 |
| [`deep_gemm/include/deep_gemm/common/math.cuh`](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/common/math.cuh) | 编译期数学工具 | `constexpr_align` |
| [`deep_gemm/include/deep_gemm/common/utils.cuh`](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/common/utils.cuh) | 设备侧小工具 | `PatternVisitor` |

> 约定：本讲把 `deep_gemm/include/deep_gemm/impls/sm90_fp8_gemm_1d1d.cuh` 简称为「该 `.cuh`」，行号均对应当前 HEAD。

## 4. 核心概念与源码讲解

### 4.1 kernel 结构与 launch_bounds

#### 4.1.1 概念说明

一个 GEMM kernel 要回答三个问题：

1. **算什么**？—— `D = C + A @ B`，其中 A 是 `[M,K]`、B 是 `[N,K]`（NT 布局，B 已转置）、D/C 是 `[M,N]`，FP8 输入、FP32 累加。
2. **谁来算**？—— 一个 CTA（线程块，对应一个 block）里有几条线程，分别干什么活。
3. **算多大一块**？—— 把 `[M,N]` 切成 `BLOCK_M × BLOCK_N` 的小瓦片，K 轴切成 `BLOCK_K` 的段，一个 CTA 算一个 `(m_block, n_block)`。

DeepGEMM 的 SM90 FP8 kernel 采用 **「1 个 TMA warp-group + 若干 math warp-group」** 的经典 Hopper 设计：

- **TMA warp-group**：128 条线程，但实际只有 1 个 warp 干活，专职发 TMA 异步拷贝指令把 A/B/SF 从全局内存搬进共享内存，不参与计算。
- **math warp-group**：每 128 条线程一个 warp-group，专职发 WGMMA 指令驱动 tensor core 做矩阵乘。

所谓 `1D1D`，指的是 **A/B 的 TMA 加载盒不切分**（一个 TMA 拷贝恰好搬满一个完整 block），对应宿主侧那条 `swizzle_a_mode == block_k` 的断言（见 4.1.3）。

#### 4.1.2 核心流程

一个 CTA 的生命周期可以概括为：

```
启动(所有线程)
   ├─ 预取 TMA 描述符 (1 个 warp)
   ├─ 划分共享内存指针 (全是偏移量计算)
   ├─ 初始化 mbarrier (1 个 warp)
   ├─ 线程分裂:
   │    ├─ TMA warp-group: 循环 { 取下一个 block → 流水线发 TMA }
   │    └─ math warp-group: 循环 { 取同一个 block → 流水线发 WGMMA → 缩放累加 → 存回 D }
   └─ grid 内所有 block 通过 Scheduler 自取任务直到算完
```

关键点：TMA 线程与 math 线程 **看的是同一个 `Scheduler`、取同一个 `(m_block, n_block)`**，只是分工不同——前者喂数据，后者吃数据。它们通过 **双缓冲 mbarrier**（`full_barriers` / `empty_barriers`）握手。

#### 4.1.3 源码精读

**模板签名与 launch_bounds**（[该 `.cuh`:L30-L48](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/impls/sm90_fp8_gemm_1d1d.cuh#L30-L48)）：

```cpp
template <uint32_t SHAPE_M, uint32_t SHAPE_N, uint32_t SHAPE_K,
          uint32_t kNumGroups,
          uint32_t BLOCK_M, uint32_t BLOCK_N, uint32_t BLOCK_K,
          uint32_t kSwizzleAMode, uint32_t kSwizzleBMode,
          uint32_t kNumStages,
          uint32_t kNumTMAThreads, uint32_t kNumMathThreads,
          uint32_t kNumTMAMulticast, bool kIsTMAMulticastOnA,
          uint32_t kNumSMs,
          GemmType kGemmType, typename cd_dtype_t>
CUTLASS_GLOBAL __launch_bounds__(kNumTMAThreads + kNumMathThreads, 1) void
sm90_fp8_gemm_1d1d_impl(...) { ... }
```

逐组解释这 17 个模板参数（与宿主 `generate_impl` 的填参一一对应，见 [宿主 `.hpp`:L39-L63](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/impls/sm90_fp8_gemm_1d1d.hpp#L39-L63)）：

| 参数组 | 参数 | 含义 |
|---|---|---|
| 形状特化 | `SHAPE_M/N/K` | compiled_dims：`0`=运行时、非 `0`=编译期常量 |
| 分组 | `kNumGroups` | MoE 分组数（稠密 GEMM=1） |
| 分块 | `BLOCK_M/N/K` | 一个瓦片的 M/N/K 尺寸 |
| swizzle | `kSwizzleAMode/BMode` | A/B 的共享内存 swizzle 模式（字节数） |
| 流水线 | `kNumStages` | 软件流水线级数（多缓冲深度） |
| 线程 | `kNumTMAThreads/kNumMathThreads` | TMA / math 两组线程数 |
| cluster | `kNumTMAMulticast` | TMA 多播路数（= cluster 大小，SM90≤2） |
| cluster | `kIsTMAMulticastOnA` | 多播打在 A 还是 B（= `cluster_n>1`） |
| 调度 | `kNumSMs` | 参与计算的 SM 数 |
| 类型 | `kGemmType` | `Normal` 或 `KGroupedContiguous` |
| 类型 | `cd_dtype_t` | 输出 dtype（断言只能是 `float`） |

`__launch_bounds__(kNumTMAThreads + kNumMathThreads, 1)` 告诉编译器：每个 CTA 的线程总数 = 两组之和，且**每个 SM 最多驻留 1 个 CTA**（第二个参数 `minBlocksPerMultiprocessor=1`）。后者很关键——这个 kernel 寄存器占用极高（math warp-group 要 240 个寄存器/线程，见 4.3），一个 SM 只能放下一个 CTA，所以显式声明为 1。

**编译期断言**（[该 `.cuh`:L51-L61](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/impls/sm90_fp8_gemm_1d1d.cuh#L51-L61)）钉死了若干不变量：

```cpp
DG_STATIC_ASSERT(kNumTMAThreads == 128 and kNumMathThreads % 128 == 0, "Invalid Threads");
DG_STATIC_ASSERT(BLOCK_K == 128, "Only support per-128-channel FP8 scaling");
DG_STATIC_ASSERT(kGemmType == GemmType::Normal or kGemmType == GemmType::KGroupedContiguous, ...);
DG_STATIC_ASSERT(cute::is_same_v<cd_dtype_t, float>, "Invalid C/D data dtype");
using WGMMA = typename mma::sm90::FP8MMASelector<BLOCK_N>::type;
DG_STATIC_ASSERT(BLOCK_M % WGMMA::M == 0, "Invalid block size");
```

- `kNumTMAThreads==128`：TMA 恰好一个 warp-group；`kNumMathThreads` 必须是 128 的整数倍（若干 warp-group）。
- `BLOCK_K==128`：**根因见 4.2**，一句话——SF 的粒度就是每 128 个 K 通道一个，BLOCK_K 必须与之对齐。
- `BLOCK_M % WGMMA::M == 0`：BLOCK_M 必须是 WGMMA 的 M(=64) 的整数倍（见 4.3）。

> 「`1D1D` 不切分」的不变量其实在宿主侧兜底——[宿主 `.hpp`:L102-L103](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/impls/sm90_fp8_gemm_1d1d.hpp#L102-L103) 断言 `swizzle_a_mode == block_k` 且 `swizzle_b_mode == block_k`，即 TMA 加载盒在 K 维恰好等于一个 BLOCK_K，不做切分。这也是 4.2 中 `SMEM_A_SIZE_PER_STAGE` 能直接写成 `BLOCK_M*BLOCK_K` 的前提。

#### 4.1.4 代码实践

**目标**：把 17 个模板参数与宿主 JIT 生成代码对上号。

**步骤**：

1. 打开 [宿主 `.hpp` 的 `generate_impl`](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/impls/sm90_fp8_gemm_1d1d.hpp#L33-L64)，看 `fmt::format` 那 17 个 `{}` 占位符各自填了什么（`get_compiled_dim(...)`、`block_m/n/k`、`num_stages`、`num_tma_threads`…）。
2. 对照本讲 4.1.3 的参数表，逐行标注每个占位符对应的设备模板参数。
3. 注意第 13、14 个参数 `kNumTMAMulticast` / `kIsTMAMulticastOnA` 分别填的是 `layout.get_cluster_size()` 与 `layout.cluster_n > 1`——这说明 **cluster 配置如何变成 TMA 多播策略**。

**预期结果**：你能画出一张「宿主配置字段 → 模板参数」的映射表。**待本地验证**：在 `DG_JIT_DEBUG=1` 下跑一次 GEMM，从 `$HOME/.deep_gemm` 缓存里找到生成的 `.cu`，确认 `sm90_fp8_gemm_1d1d_impl<...>` 的实参与你推断的一致（见 u3-l1/u3-l3 的缓存定位方法）。

#### 4.1.5 小练习与答案

**Q1**：为什么 `__launch_bounds__` 的第二个参数是 `1` 而不是更大的值？
**答**：该 kernel 每个 math warp-group 要占用 240 个寄存器/线程，整 CTA 寄存器占用很高，一个 SM 物理上只能驻留 1 个 CTA。声明为 1 既符合事实，也让编译器放心地把寄存器堆满（不为多 CTA 让路）以提升单 kernel 性能。

**Q2**：`kIsTMAMulticastOnA = (cluster_n > 1)`。当 `cluster_n > 1` 时，cluster 内多个 CTA 共享同一个 m_block 还是同一个 n_block？为什么多播打在 A 上？
**答**：`cluster_n > 1` 表示 cluster 沿 N 维展开，多个 CTA 拥有相同的 `m_block`、不同的 `n_block`。因此它们需要的 A 瓦片相同（可多播共享）、B 瓦片不同，所以多播打在 A 上。

---

### 4.2 共享内存划分

#### 4.2.1 概念说明

Hopper 的 tensor core（WGMMA）**只能直接读共享内存**，不能直接读全局内存。所以 kernel 必须先用 TMA 把 A/B/SF 搬进共享内存，math 线程才能算。这块共享内存是 kernel 性能的「舞台」，它的布局直接决定：

- 能否喂饱 tensor core（数据是否对齐、swizzle 是否消除 bank conflict）；
- 能开几级软件流水线（`kNumStages` 越大，占的共享内存越多）。

DeepGEMM 用一个 `extern __shared__` 字节数组 `smem_buffer`，靠**纯偏移量计算**把这一整块内存切成若干区域，再用一个小工具 `PatternVisitor` 把「第 `i` 级 stage 的缓冲地址」封装成 `smem_a[i]` 这样的下标访问。

`PatternVisitor` 本质是「把一个 lambda 包成可下标对象」——[`utils.cuh`:L12-L22](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/common/utils.cuh#L12-L22)：

```cpp
template <typename FuncT>
struct PatternVisitor {
    FuncT func;
    auto operator[](const uint32_t& i) const { return func(i); }  // smem_a[i] == func(i)
};
```

于是 `smem_a[i]` 返回第 `i` 级 stage 的 A 缓冲首地址，`smem_a` 内部记录的只是一个「stage 索引 → 偏移量」的公式。

#### 4.2.2 核心流程

`smem_buffer` 被顺序划分为以下区域（从低地址到高地址）：

```
┌──────────────────────────────────────────────────────────┐
│ [0] TMA 描述符缓存 (仅 KGroupedContiguous，否则 0)         │  SMEM_TENSOR_MAP_SIZE
├──────────────────────────────────────────────────────────┤
│ [1] D 累加结果缓冲 (FP32)                                 │  SMEM_D_SIZE
├──────────────────────────────────────────────────────────┤
│ [2] A 缓冲 × kNumStages 级                                │  kNumStages × SMEM_A_SIZE_PER_STAGE
├──────────────────────────────────────────────────────────┤
│ [3] B 缓冲 × kNumStages 级                                │  kNumStages × SMEM_B_SIZE_PER_STAGE
├──────────────────────────────────────────────────────────┤
│ [4] SFA 缓冲 × kNumStages 级                              │  kNumStages × SMEM_SFA_SIZE_PER_STAGE
├──────────────────────────────────────────────────────────┤
│ [5] SFB 缓冲 × kNumStages 级 (对齐到 128B)                │  kNumStages × ALIGNED_SMEM_SFB_SIZE_PER_STAGE
├──────────────────────────────────────────────────────────┤
│ [6] mbarrier × 2×kNumStages (full + empty)                │  2 × kNumStages × sizeof(Barrier)
└──────────────────────────────────────────────────────────┘
```

每级 stage 的 A/B/SF 缓冲都被复制 `kNumStages` 份，构成一个**环形多缓冲队列**：TMA 往第 `i % kNumStages` 级写的同时，math 线程在读第 `(i-1) % kNumStages` 级，互不干扰。

#### 4.2.3 源码精读

**各区域的大小常量**（[该 `.cuh`:L68-L76](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/impls/sm90_fp8_gemm_1d1d.cuh#L68-L76)）：

```cpp
static constexpr uint32_t SMEM_TENSOR_MAP_SIZE = (kGemmType == GemmType::KGroupedContiguous ? sizeof(cute::TmaDescriptor) * 2 : 0);
static constexpr uint32_t SMEM_D_SIZE = BLOCK_M * BLOCK_N * sizeof(float);
static constexpr uint32_t SMEM_A_SIZE_PER_STAGE = BLOCK_M * BLOCK_K * sizeof(__nv_fp8_e4m3);
static constexpr uint32_t SMEM_B_SIZE_PER_STAGE = BLOCK_N * BLOCK_K * sizeof(__nv_fp8_e4m3);
static constexpr uint32_t SMEM_SFA_SIZE_PER_STAGE = BLOCK_M * sizeof(float);
static constexpr uint32_t SMEM_SFB_SIZE_PER_STAGE = BLOCK_N * sizeof(float);
static constexpr uint32_t ALIGNED_SMEM_SFB_SIZE_PER_STAGE = math::constexpr_align(SMEM_SFB_SIZE_PER_STAGE, 128u);
DG_STATIC_ASSERT(SMEM_SFA_SIZE_PER_STAGE % 128 == 0, "Invalid TMA alignment");
```

对应大小公式：

| 区域 | 单级大小（字节） | 说明 |
|---|---|---|
| D | `BLOCK_M * BLOCK_N * 4` | FP32 累加结果，作为 TMA store 回写的源 |
| A | `BLOCK_M * BLOCK_K * 1` | FP8(E4M3) 占 1 字节 |
| B | `BLOCK_N * BLOCK_K * 1` | 同上 |
| SFA | `BLOCK_M * 4` | **每行 1 个 FP32 缩放因子** |
| SFB | `ceil(BLOCK_N*4, 128)` | 每列 1 个 FP32，并对齐到 128B |

其中 `constexpr_align(a, b) = ceil_div(a, b) * b`（[`math.cuh`:L32-L34](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/common/math.cuh#L32-L34)），即向上取整到 128 的倍数。

**关键观察——SF 每行/每列只有一个**：`SMEM_SFA_SIZE_PER_STAGE = BLOCK_M * sizeof(float)`，意思是每级 stage、每个 M 行只对应 1 个 FP32 缩放因子。这一个 SF 要覆盖整个 `BLOCK_K=128` 个 K 通道。这正是 `DG_STATIC_ASSERT(BLOCK_K == 128, ...)` 的根因：

> **SF 的粒度由 recipe 决定是「每 128 个 K 通道一个」**（gran_k=128，见 u2-l2）。kernel 用 `sf_k_idx = k_block_idx`（一个 K block 对应一个 SF 索引，见 4.3.3）来取 SF，所以必须保证「一个 K block 恰好含 128 个 K 通道」，即 `BLOCK_K == 128`。若 `BLOCK_K` 是别的值，SF 与 K block 的对应关系就会错位，逐块缩放就错了。

同时，WGMMA 的 K 固定为 32（见 [`sm90.cuh`:L26-L29](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/mma/sm90.cuh#L26-L29)），一个 `BLOCK_K=128` 正好被切成 `128/32=4` 条 WGMMA 指令，天然对齐。

**偏移量与下标封装**（[该 `.cuh`:L93-L125](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/impls/sm90_fp8_gemm_1d1d.cuh#L93-L125)）：

```cpp
extern __shared__ __align__(1024) uint8_t smem_buffer[];
DG_STATIC_ASSERT(SMEM_D_SIZE % 1024 == 0, "Shared memory of A/B must be aligned to 1024 bytes");

auto smem_d = reinterpret_cast<float*>(smem_buffer + SMEM_TENSOR_MAP_SIZE);
auto smem_a = utils::PatternVisitor([&](const uint32_t& i) {
    return reinterpret_cast<__nv_fp8_e4m3*>(smem_buffer + (SMEM_TENSOR_MAP_SIZE + SMEM_D_SIZE + i * SMEM_A_SIZE_PER_STAGE));
});
// ... smem_b / smem_sfa / smem_sfb / full_barriers / empty_barriers 同理，逐级累加偏移
```

几个要点：

- `__align__(1024)` + `SMEM_D_SIZE % 1024 == 0`：swizzle-128B 要求共享内存基址 1024 字节对齐（128B 的 swizzle 原子在 8 行 × 128B 排布，基址需 1024 对齐）。
- `smem_a[i]` 的偏移 = `SMEM_TENSOR_MAP_SIZE + SMEM_D_SIZE + i * SMEM_A_SIZE_PER_STAGE`，即跳过 [0][1] 两区后按 stage 步进。
- SFB 的 stage 步长用的是对齐后的 `ALIGNED_SMEM_SFB_SIZE_PER_STAGE`（保证每级 stage 起点 128B 对齐，TMA 才能正确写入）。
- barrier 区起点 `SMEM_BARRIER_OFFSET` = 前面所有数据区之和；`full_barriers[i]` 与 `empty_barriers[i]` 各占 `kNumStages` 个，共 `2*kNumStages*sizeof(Barrier)`。

#### 4.2.4 代码实践

**目标**：手算一个具体配置下的共享内存总用量。

**步骤**：

1. 取一个典型配置：`BLOCK_M=128, BLOCK_N=128, BLOCK_K=128, kNumStages=4, GemmType=Normal`。
2. 逐项套用 4.2.3 的公式：
   - `SMEM_TENSOR_MAP_SIZE = 0`（Normal）
   - `SMEM_D_SIZE = 128*128*4 = 65536` B
   - `SMEM_A_SIZE_PER_STAGE = 128*128 = 16384` B
   - `SMEM_B_SIZE_PER_STAGE = 128*128 = 16384` B
   - `SMEM_SFA_SIZE_PER_STAGE = 128*4 = 512` B（恰 128 对齐）
   - `ALIGNED_SMEM_SFB_SIZE_PER_STAGE = ceil(128*4,128)*128 = 512` B
   - 数据区合计 = `65536 + 4*(16384+16384+512+512) = 65536 + 135168 = 200704` B
   - 再加 `2*4*sizeof(ClusterTransactionBarrier)`（每个 8B）= `64` B
3. 用计算器核对总字节数。

**预期结果**：数据区约 196 KB，这与 Hopper 每个 SM 228 KB 共享内存上限吻合（也印证了「一个 SM 只能驻留 1 个 CTA」）。**待本地验证**：用 `DG_JIT_DEBUG=1` 跑同一形状，确认打印的 `smem_size` 与你的手算结果一致（宿主侧 `smem_size` 由 [`pipeline_config.smem_size`](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/impls/sm90_fp8_gemm_1d1d.hpp#L128) 传入）。

#### 4.2.5 小练习与答案

**Q1**：为什么 SFB 要 `constexpr_align(..., 128)` 而 SFA 直接断言 `% 128 == 0`？
**答**：`SMEM_SFA_SIZE_PER_STAGE = BLOCK_M*4`，而 BLOCK_M 恒为 16 的倍数（受 `BLOCK_M % WGMMA::M==0` 及候选约束），所以 `BLOCK_M*4` 必然 64 对齐，再因 BLOCK_M 通常 ≥32 而 128 对齐；代码用断言确认这一点。SFB 是 `BLOCK_N*4`，BLOCK_N 可能取 24 等非 32 值（24*4=96 不被 128 整除），所以必须 `constexpr_align` 强行向上取整到 128，保证每级 stage 起点 128B 对齐以适配 TMA 写入。

**Q2**：若把 `BLOCK_K` 改成 64，哪条断言会先报错？背后意味着什么？
**答**：`DG_STATIC_ASSERT(BLOCK_K == 128, ...)` 编译期报错。背后是 SF 粒度（每 128 个 K 通道一个 SF）与 K block 尺寸必须一致；改 BLOCK_K 会破坏 `sf_k_idx == k_block_idx` 的一一对应，逐块缩放结果出错。WGMMA K=32，BLOCK_K 也应是 32 的倍数，但 SF 约束把可选值钉死在 128。

---

### 4.3 TMA/math 线程分工与 k-loop 流水线

#### 4.3.1 概念说明

有了共享内存舞台，接下来是**生产者-消费者**协作：

- **生产者**：TMA warp-group，循环发 TMA 拷贝，把下一级 stage 的 A/B/SF 灌进共享内存。
- **消费者**：math warp-group，循环发 WGMMA，把共享内存里的数据喂给 tensor core，算出部分和。

两者用 **双缓冲 mbarrier** 握手（这是 Hopper 异步拷贝的标准范式）：

- `full_barriers[s]`：生产者填满第 `s` 级 stage 后 arrive，消费者等到它才认为数据就绪。
- `empty_barriers[s]`：消费者用完第 `s` 级 stage 后 arrive，生产者等到它才认为该级缓冲可覆写。

「软件流水线」指的是：当 math 在算第 `i` 级时，TMA 已经在搬第 `i+1`、`i+2`…级（最多 `kNumStages` 级在飞），从而把全局内存延迟隐藏在计算背后。

math 线程内部还有一个 **k-loop**：对每个 `(m_block, n_block)`，沿 K 轴遍历 `num_k_blocks = ceil_div(shape_k, BLOCK_K)` 个 K 块，每块做 4 条 WGMMA（`BLOCK_K/32`），把缩放后的部分和累加进 `final_accum`，最后写回 D。

#### 4.3.2 核心流程

**流水线索引**用一个极简公式生成 stage 与 phase（[该 `.cuh`:L165-L167](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/impls/sm90_fp8_gemm_1d1d.cuh#L165-L167)）：

```cpp
const auto get_pipeline = [=](const uint32_t& iter_idx) {
    return {iter_idx % kNumStages, (iter_idx / kNumStages) & 1};  // {stage_idx, phase}
};
```

即第 `iter_idx` 次迭代落在第 `iter_idx % kNumStages` 级缓冲，phase（奇偶）每过 `kNumStages` 轮翻转一次，用于匹配 mbarrier 的奇偶等待语义。

主循环（两侧都遵循）：

```
iter_idx = 0
while scheduler.get_next_block(m_block, n_block):     # 持续自取任务
    num_k_blocks = ceil_div(shape_k, BLOCK_K)
    for k_block_idx in 0..num_k_blocks:
        {stage, phase} = get_pipeline(iter_idx++)
        ── 生产者(TMA) ──
            empty_barriers[stage].wait(phase ^ 1)       # 等消费者释放
            发 4 条 tma::copy (sfa, sfb, a, b)           # 搬这一级
            full_barriers[stage].arrive_and_expect_tx(字节总数)   # 通知就绪
        ── 消费者(math) ──
            full_barriers[stage].wait(phase)            # 等生产者填满
            读 SF → 发 4 条 WGMMA → wait → 缩放累加进 final_accum
            empty_barriers[stage].arrive()              # 通知可覆写
    把 final_accum 经 smem_d 用 TMA-reduce-add 写回全局 D
```

#### 4.3.3 源码精读

**线程分裂的判据**（[该 `.cuh`:L79-L80](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/impls/sm90_fp8_gemm_1d1d.cuh#L79-L80) 与 [L170-L175](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/impls/sm90_fp8_gemm_1d1d.cuh#L170-L175)）：

```cpp
const uint32_t warp_idx  = __shfl_sync(0xffffffff, threadIdx.x / 32, 0);
const uint32_t lane_idx  = threadIdx.x % 32;
...
if (warp_idx >= kNumMathThreads / 32) {
    // TMA warp-group（靠后的 warp）
    cutlass::arch::warpgroup_reg_dealloc<kNumTMARegisters>();   // 让出寄存器
    if (warp_idx == kNumMathThreads / 32 and cute::elect_one_sync()) { /* 真正发 TMA 的单 warp */ }
} else {
    // math warp-group
    cutlass::arch::arch::warpgroup_reg_alloc<kNumMathRegisters>();   // 多拿寄存器
    ...
}
```

- `warp_idx < kNumMathThreads/32` 的 warp 是 math；`>=` 的是 TMA。
- 只有 `warp_idx == kNumMathThreads/32` 这一个 warp（且 `elect_one_sync` 选 1 个 lane）真正发 TMA 指令——其余 TMA warp 闲置（但仍参与 reg_dealloc 让出寄存器）。
- 通过 `warpgroup_reg_dealloc/alloc` 重配寄存器：math 多拿（240 个/线程），TMA 少拿，最大化 math 的寄存器供给。寄存器数随是否展开 k-loop 而变（[`kNumPipelineUnrolls`/`kNumTMARegisters`/`kNumMathRegisters`，L151-L155](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/impls/sm90_fp8_gemm_1d1d.cuh#L151-L155)）。

**TMA 生产者 k-loop**（[该 `.cuh`:L209-L226](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/impls/sm90_fp8_gemm_1d1d.cuh#L209-L226)）：

```cpp
for (uint32_t k_block_idx = 0; k_block_idx < num_k_blocks; ++ k_block_idx) {
    CUTE_TIE_DECL(get_pipeline(iter_idx ++), stage_idx, phase);
    empty_barriers[stage_idx]->wait(phase ^ 1);                 // 等消费者释放该级

    auto& full_barrier = *full_barriers[stage_idx];
    const uint32_t k_idx = k_block_idx * BLOCK_K;
    const uint32_t sf_k_idx = scheduler.current_sf_k_cumsum + k_block_idx;   // 一个 K block 一个 SF
    tma::copy<BLOCK_M, BLOCK_K, 0>(&tensor_map_sfa, &full_barrier, smem_sfa[stage_idx], m_idx, sf_k_idx, num_tma_multicast_a);
    tma::copy<BLOCK_N, BLOCK_K, 0>(&tensor_map_sfb, &full_barrier, smem_sfb[stage_idx], n_idx, sf_k_idx, num_tma_multicast_b);
    tma::copy<BLOCK_K, BLOCK_M, kSwizzleAMode>(tensor_map_a_ptr, &full_barrier, smem_a[stage_idx], k_idx, m_idx, num_tma_multicast_a);
    tma::copy<BLOCK_K, BLOCK_N, kSwizzleBMode>(tensor_map_b_ptr, &full_barrier, smem_b[stage_idx], k_idx, n_idx, num_tma_multicast_b);
    full_barrier.arrive_and_expect_tx(SMEM_A + SMEM_B + SMEM_SFA + SMEM_SFB);   // 字节级到达
}
```

注意 `sf_k_idx = current_sf_k_cumsum + k_block_idx`：**每推进一个 K block，SF 索引也推进 1**（因为一个 K block=128 通道=1 个 SF）。这正是 4.2 中 BLOCK_K 必须=128 的直接体现。`arrive_and_expect_tx(字节数)` 是 TMA 专用：告诉 mbarrier「等收到这么多字节后才算完成」，把 TMA 拷贝完成事件编码进 barrier。

> 注意 `tma::copy<...>` 的参数顺序：A 是 `BLOCK_K, BLOCK_M`（K 在内维，K-major）、SF 是 `BLOCK_M/BLOCK_N, ...`（MN 在内维，MN-major）。这与 u4-l2 讲的「SM90 强制 K-major、SF 固定 MN-major」一致。

**math 消费者 k-loop**（[该 `.cuh`:L267-L313](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/impls/sm90_fp8_gemm_1d1d.cuh#L267-L313)）的核心：

```cpp
for (uint32_t k_block_idx = 0; k_block_idx < num_k_blocks; ++ k_block_idx) {
    CUTE_TIE_DECL(get_pipeline(iter_idx ++), stage_idx, phase);
    full_barriers[stage_idx]->wait(phase);                      // 等生产者填满

    // 1) 读 SF（必须在 WGMMA 之前，避免下一 block 污染）
    auto scale_a_0 = ptx::ld_shared(smem_sfa[stage_idx] + r_0);
    auto scale_a_1 = ptx::ld_shared(smem_sfa[stage_idx] + r_1);
    for (i ...) scales_b[i] = ptx::ld_shared(...);              // 每条 lane 读 2 个 B 的 SF

    // 2) 发 WGMMA：BLOCK_K/32 = 4 条指令
    for (i ...) ptx::warpgroup_fence_operand(accum[i]);
    ptx::warpgroup_arrive();
    for (uint32_t k = 0; k < BLOCK_K / WGMMA::K; ++ k) {        // WGMMA::K = 32 → 4 条
        auto desc_a = mma::sm90::make_smem_desc(smem_a[stage_idx] + math_wg_idx * WGMMA::M * BLOCK_K + k * WGMMA::K, 1);
        auto desc_b = mma::sm90::make_smem_desc(smem_b[stage_idx] + k * WGMMA::K, 1);
        WGMMA::wgmma(desc_a, desc_b, accum, k);                 // k==0 清零(scale_d=false)，k>0 累加
    }
    ptx::warpgroup_commit_batch();
    for (i ...) ptx::warpgroup_fence_operand(accum[i]);
    ptx::warpgroup_wait<0>();                                   // 等本 block MMA 完成

    empty_barrier_arrive(stage_idx);                           // 通知可覆写

    // 3) 缩放累加：final_accum += sfa * sfb * accum
    for (i ...) {
        final_accum[i*4+0] += scale_a_0 * scales_b[i].x * accum[i*4+0];
        final_accum[i*4+1] += scale_a_0 * scales_b[i].y * accum[i*4+1];
        final_accum[i*4+2] += scale_a_1 * scales_b[i].x * accum[i*4+2];
        final_accum[i*4+3] += scale_a_1 * scales_b[i].y * accum[i*4+3];
    }
}
```

要点：

- **SF 先于 WGMMA 读**：注释明说，所有共享内存读必须在 `warpgroup_arrive` 之前，否则下一个被调度的 block 可能污染 `smem_sfa[s]`。
- **WGMMA 分块**：WGMMA 的 `M=64, K=32, kNumAccum = 64*N/128 = N/2`（[`sm90.cuh`:L26-L29](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/mma/sm90.cuh#L26-L29)）。一个 `BLOCK_K=128` 切成 4 条 WGMMA；`math_wg_idx * WGMMA::M * BLOCK_K` 把不同 math warp-group 指向 A 缓冲的不同 64 行（支持 BLOCK_M=128 时双 warp-group 各算 64 行）。
- **缩放与累加分离**：`accum` 只存「原始 FP8 MMA 结果」（首条指令 `scale_d=false` 清零、后续累加），`wgmma_wait<0>` 后再乘 `sfa*sfb` 累加进 `final_accum`。即数学上：

  \[
  D_{m,n} = C_{m,n} + \sum_{b=0}^{\text{num\_k\_blocks}-1} \mathrm{sfa}_{m,b}\cdot \mathrm{sfb}_{n,b}\cdot \sum_{k'} A_{m,b\cdot128+k'}\,B_{n,b\cdot128+k'}
  \]

  其中内层求和（单个 K block）由 WGMMA 硬件算进 `accum`，外层跨 block 的缩放累加由软件做进 `final_accum`。

**写回 D**（[该 `.cuh`:L315-L338](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/impls/sm90_fp8_gemm_1d1d.cuh#L315-L338)）：math 线程先把 `final_accum` 经共享内存（`smem_d`）写出，再由 1 个 warp 发 `SM90_TMA_REDUCE_ADD_2D::copy` 把 `smem_d` 异步 reduce-add 回全局 D（支持带累加 C 的场景，多个 block 对同一 D 贡献时安全合并）。

**启动前的两件事**也值得一提：

- **TMA 描述符预取**（[L83-L90](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/impls/sm90_fp8_gemm_1d1d.cuh#L83-L90)）：`__grid_constant__` 传入的 5 个描述符先 `prefetch_tma_descriptor` 进常数缓存，避免首次 TMA 访问的 miss。
- **PDL 同步**（[L158](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/impls/sm90_fp8_gemm_1d1d.cuh#L158)）：`cudaGridDependencySynchronize()` 是程序化依赖启动(PDL)的同步点——当 PDL 开启时（见 u4-l1/u4-3），本 kernel 可与前一个 kernel 重叠启动，但要在此处等前一个 kernel 产出可消费的依赖后才继续。

#### 4.3.4 代码实践

**目标**：跟踪一次 TMA→mbarrier→WGMMA 的同步序列，画出时序。

**步骤**：

1. 在源码里标出生产者侧每条 `tma::copy`、`arrive_and_expect_tx`、`empty_barriers->wait`，以及消费者侧每条 `full_barriers->wait`、`wgmma`、`empty_barrier_arrive`。
2. 假设 `kNumStages=3`，写出 `iter_idx = 0,1,2,3,...` 时 `get_pipeline` 返回的 `(stage, phase)` 序列：(0,0),(1,0),(2,0),(0,1),(1,1),…
3. 据此画一条时序：TMA 在 stage 0/1/2 间循环填，math 落后若干级读，确认两者从不踩同一级缓冲。

**需要观察的现象**：stage 索引在 `[0, kNumStages)` 内循环、phase 每 `kNumStages` 轮翻转；生产者 `wait(phase^1)` 与消费者 `wait(phase)` 的奇偶正好互补，保证一方写时另一方必在等。

**预期结果**：得到一张「双缓冲流水线时序图」，能解释为何 `kNumStages` 越大越能隐藏访存延迟（但有共享内存上限约束，见 4.2.4）。**待本地验证**：用 NCU（`--set full`）抓这个 kernel 的 Stall 条目，观察增大 `kNumStages` 是否降低 `stall_long_sb`（共享内存等待）占比。

#### 4.3.5 小练习与答案

**Q1**：生产者 `empty_barriers[stage]->wait(phase ^ 1)`，消费者 `full_barriers[stage]->wait(phase)`，为何一个 `^1` 一个不 `^1`？
**答**：mbarrier 用奇偶 phase 表示「本次到达是否已完成」。生产者要等消费者「用完」的信号，该信号是上一轮（phase^1）发出的；消费者要等生产者「填满」的信号，对应本轮 phase。两者奇偶互补，确保写者与读者对同一级缓冲的访问严格错开。

**Q2**：为什么 `accum` 不直接累加成最终结果，而要先存原始 MMA 结果、再单独乘 SF 累加进 `final_accum`？
**答**：WGMMA 是 FP8×FP8→FP32 的硬件指令，它不知道每块的缩放因子。若直接累加进同一个寄存器，跨 K block 时会丢失「每块各自缩放」的信息。所以让硬件只算单块的原始乘加（进 `accum`），软件再把 `accum` 乘以该块的 `sfa*sfb` 后累加进 `final_accum`，从而正确实现逐块缩放的 GEMM。

**Q3**：`math_wg_idx * WGMMA::M * BLOCK_K` 这个偏移解决什么问题？
**答**：当 `BLOCK_M > 64`（如 128）时，一个 block 的 M 维有多个 WGMMA::M=64 的行带。用 `math_wg_idx`（= `threadIdx.x/128`，即第几个 math warp-group）把不同 warp-group 指向 A 缓冲里各自负责的 64 行，实现多 warp-group 并行切分 M 维。

## 5. 综合实践

**任务**：完成规格指定的源码阅读型实践——阅读 `sm90_fp8_gemm_1d1d_impl` 的前半部分（L30~L148），产出一份「共享内存分区表 + BLOCK_K==128 根因说明」。

**操作步骤**：

1. 打开 [该 `.cuh`](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/impls/sm90_fp8_gemm_1d1d.cuh)，通读 L30（模板签名）到 L148（barrier 初始化后的同步）。
2. 列出它划分的全部共享内存区域，每项给出：
   - 区域名（D / A / B / SFA / SFB / full_barriers / empty_barriers / tensor_map 缓存）；
   - 单级大小公式（如 `SMEM_A_SIZE_PER_STAGE = BLOCK_M*BLOCK_K*1`）；
   - 是否乘 `kNumStages`、是否对齐、起始偏移依赖哪个前置区域。
3. 解释 `DG_STATIC_ASSERT(BLOCK_K == 128, "Only support per-128-channel FP8 scaling")` 的约束来源。要求至少给出两条相互印证的理由：
   - SF 粒度：每个 SF 覆盖 128 个 K 通道（`SMEM_SFA_SIZE_PER_STAGE = BLOCK_M * 4`，每行 1 个 SF），`sf_k_idx = k_block_idx` 要求「1 个 K block = 1 个 SF」；
   - WGMMA 粒度：WGMMA 的 K=32，BLOCK_K 需为 32 的倍数；两者共同把 BLOCK_K 钉死在 128。

**预期结果**：一张 6~7 行的分区表 + 一段不少于 3 句的 BLOCK_K 根因说明，且能与本讲 4.2.3、4.3.3 的源码引用对应。**待本地验证**：若你在 SM90 机器上，可设 `DG_JIT_DEBUG=1`、`DG_PRINT_CONFIGS=1` 跑一次 FP8 GEMM，从打印里读到本次选中的 `BLOCK_M/N/K` 与 `num_stages`，把它们代入你的分区表，核对打印的 `smem_size`。

## 6. 本讲小结

- `sm90_fp8_gemm_1d1d_impl` 是一个高度参数化的设备 kernel，17 个模板参数由宿主 JIT 填充；`__launch_bounds__(总和, 1)` 表明一个 SM 只驻留 1 个 CTA。
- 它采用 **1 个 TMA warp-group + 若干 math warp-group** 的分工：TMA 线程发异步拷贝喂数据，math 线程发 WGMMA 算乘加，靠 `warp_idx` 与 `kNumMathThreads/32` 的比较来分裂。
- 共享内存被顺序切分为 D / A×stages / B×stages / SFA×stages / SFB×stages(对齐) / 2×stages 个 mbarrier，用 `PatternVisitor` 把「第 i 级地址」封装成下标访问。
- `BLOCK_K == 128` 由 **SF 每 128 个 K 通道一个**的粒度（与 WGMMA K=32）共同钉死；这是「逐块缩放」与硬件 MMA 粒度对齐的结果。
- 生产者-消费者用 `full_barriers`/`empty_barriers` 双缓冲握手，`get_pipeline` 用 `iter_idx % kNumStages` 与 phase 翻转驱动软件流水线。
- math 侧把「原始 MMA 结果（`accum`）」与「缩放后累加（`final_accum`）」分离，正确实现 \( D = C + \sum_b \mathrm{sfa}_b\,\mathrm{sfb}_b\,(\text{block}_b\text{ 的乘加}) \)。

## 7. 下一步学习建议

- **下一讲 u6-l2（MMA 抽象：WGMMA vs UMMA）**：本讲把 WGMMA 当黑盒用了（`WGMMA::wgmma`、`make_smem_desc`）。下一讲会打开 `mma/sm90.cuh` 与 `mma/sm100.cuh`，对比 SM90 的 WGMMA descriptor 与 SM100 的 UMMA/tcgen05 编程模型。
- **u6-l3（PTX 内联：TMA 与 barrier）**：本讲出现的 `tma::copy`、`warpgroup_arrive/wait`、`ClusterTransactionBarrier` 都来自 `ptx/` 与 `comm/barrier.cuh`，下一讲深入它们的 PTX 封装。
- **u6-l4（分块调度与 L2 swizzle）**：本讲的 `Scheduler::get_next_block` 只点了用法，调度器如何跨 SM 分配 block、如何做 L2 友好的 swizzle 重排，留待 u6-l4。
- **延伸阅读**：可先读 CUTLASS 官方关于 Hopper WGMMA / TMA 的文档，建立对本讲「双缓冲 mbarrier + WGMMA」范式的背景认知。
