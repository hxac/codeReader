# u3-l6 状态输入输出：bf16 直通、fp32 转换与零初始化路径

## 1. 本讲目标

Kernel 2（`_flash_kda_fwd_recurrence`）的主循环——双 GEMM、delta 修正、状态更新——在前几讲已经拆完。本讲把镜头对准主循环**之前和之后**的两段「边缘代码」：初始状态如何进入 `state_acc`，最终状态如何离开 `state_acc`。这些路径完全由 `HasStateIn` / `HasStateOut` / `StateFP32` 三个模板布尔在编译期裁剪出 3 种输入组合与 2 种输出组合。

学完本讲你应该能够：

1. 说出三条状态输入路径（bf16 TMA 直通、fp32 中转 + smem 转换、零初始化）各自的执行者、同步原语与围栏方向。
2. 解释 `FP32StateSmemLayout` 选用 `Layout_K_SW32_Atom<float>` 且与 bf16 的 `K_INTER` 原子同构（`Swizzle<0,0,3>`）的妙处：fp32↔bf16 转换退化为「按 8x8 原子、每线程 2 元素」的纯类型搬运。
3. 手工推演 `smem_cvt_fp32_to_bf16` / `smem_cvt_bf16_to_fp32` 的线程-数据映射，验证 16384 个元素被 192 线程完整覆盖。
4. 读懂输出路径上的同步链：为什么 bf16 状态输出**不需要**显式 `fence_view_async_shared`，而 fp32 输出路径需要「两次 `__syncthreads` 夹一次 fence」。
5. 通过实验认识一个反直觉结论：**fp32 状态模式并不改变片上计算精度**——片上 `state_acc` 永远是 bf16，`StateFP32` 只改变 gmem 侧的 I/O dtype。

## 2. 前置知识

### 2.1 状态张量在 K2 中的角色

KDA 的递推状态 \( S \in \mathbb{R}^{V \times K} \)（本项目中 \( V = K = D = 128 \)）是跨 tile 串行传递的「记忆」。K2 以（序列, head）为并行轴（grid 为 `(N, H)`），每个 CTA 拥有**一份私有状态**，常驻于 shared memory 的 `state_acc`（128×128 个 bf16，32768 字节），随 tile 递推逐块更新（u3-l5 的 Phase 6）。

状态的 gmem 侧形状是 `[N, H, D, D]`（batched 模式 N=B；varlen 模式 N=cu_seqlens 段数，见 u1-l5）。launch 层把它折叠成一维视图：

```cpp
auto state_gmem_layout = make_layout(make_shape(N * H, D, D), LayoutRight{});
```

kernel 内以 `seq_idx * H + head_idx` 这个单一坐标索引——N 和 H 两维被压平，正好对应 grid 的两个维度。

### 2.2 两个代理与三道围栏（快速复习）

SM90 上读写的发起方分属两个「代理（proxy）」：

- **generic 代理**：普通 `LD`/`ST` 指令（包括 LDSM/STSM），即所有 CUDA C++ 代码的默认路径。
- **async 代理**：TMA 引擎（`cp.async.bulk`），独立于 SM 的搬运单元。

两个代理对同一块 smem 的写互相**不保证立即可见**，需要围栏打通（u2-l6 已建立这一模型）。本讲涉及三道：

| 围栏 | 方向 | 本讲场景 |
|---|---|---|
| `fence_barrier_init()` | generic → async | `state_acc_tma_barrier.init()` 之后，让 TMA 完成机制能看到 barrier 初值 |
| `fence_view_async_shared()`（TMA 写之后） | async → generic | TMA 载入 state 后，让 MMA warp 的 LDSM 读到数据 |
| `fence_view_async_shared()`（generic 写之后） | generic → async | 零初始化 / fp32 转换之后，让 TMA store 读到数据 |

另外回顾 mbarrier 的「事务字节」语义（u3-l3）：`arrive_and_expect_tx(n)` 一次登记 \( n \) 字节的预期写入，TMA 完成时硬件扣减计数，字节计数与到达计数都归零才翻转 phase。**少算会提前放行（静默错误），多算则永久挂死**——所以本讲会逐字节核对两条路径的事务字节数。

### 2.3 模板三布尔与编译期路径选择

回顾 u2-l3：host 侧从可选张量推导出 `has_state_in` / `has_state_out` / `state_fp32` / `is_varlen` 四个布尔，七分支分发到 14 份 kernel 实例。本讲关注的三个布尔在 K2 里裁剪出：

- 输入：`HasStateIn && !StateFP32`（bf16 直通）、`HasStateIn && StateFP32`（fp32 中转）、`!HasStateIn`（零初始化）——三选一，`if constexpr` 编译期决定；
- 输出：`HasStateOut && !StateFP32`（bf16 直通 TMA store）、`HasStateOut && StateFP32`（smem 转换后 TMA store）。

注意一个容易忽略的事实：`StateFP32` 在 K2 的**主循环里没有任何分支**——无论哪种模式，片上 `state_acc` 都是 bf16 数组，MMA/LDSM/STSM 的布局与指令完全相同。`StateFP32` 只影响「状态的进出口」。

## 3. 本讲源码地图

| 文件 | 本讲涉及内容 | 行号范围 |
|---|---|---|
| `csrc/smxx/fwd_kernel2.cuh` | `FP32StateSmemLayout` 定义、`state_fp32_buf` union、三条输入路径、两条输出路径、MMA 收尾 fence 链 | L59-69, L99-111, L239-317, L733-741, L786-835 |
| `csrc/smxx/utils.cuh` | `smem_cvt_fp32_to_bf16` / `smem_cvt_bf16_to_fp32` 两个转换函数及原子同构注释、`bf16_to_f32` | L55-59, L371-437 |
| `csrc/smxx/fwd_launch.cu` | `make_state_tma` 按 `StateFP32` 选择 bf16/fp32 描述符（无状态用哑指针）、state 的 gmem 布局 | L49-53, L118-144 |
| `tests/test_fwd.py` | `run_fla_gold_reference`（fp64 金标）与输入构造方式，综合实践复用 | L71-141, L222-243 |
| `flash_kda/__init__.py` | `fwd` 的 Python 签名（综合实践调用） | L5-41 |

## 4. 核心概念与源码讲解

### 4.1 模块一：三条状态输入路径

#### 4.1.1 概念说明

主循环开始前，每个 CTA 必须让 `state_acc` 处于正确初值。三种情况对应三段互斥代码：

1. **bf16 直通**：`initial_state` 本身是 bf16，与片上表示同 dtype 同布局——TMA 从 gmem 搬进 smem 即完成，零转换。
2. **fp32 中转**：`initial_state` 是 fp32。TMA 先把它搬进一块 fp32 临时缓冲（`state_fp32_buf`，与流水线缓冲 union 复用），再由**全部 192 线程**做 fp32→bf16 的 smem 内转换写入 `state_acc`。
3. **零初始化**：没有 `initial_state`（stateless 模式），全体线程跨步循环写零。

为什么 bf16 可以直通而 fp32 不行？因为 TMA 是**逐比特搬运**，不改变 dtype 与排布；`state_acc` 的元素类型是 bf16，fp32 数据直接 TMA 进去在类型上就不成立，必须经过一次显式转换。而转换本身又引出布局问题——这正是模块二的主题。

#### 4.1.2 核心流程

三条路径的统一骨架（差异在中间）：

```text
            ┌─ HasStateIn && !StateFP32 ──→ LOAD warp 单线程:
            │                                init barrier → fence_barrier_init
            │                                → arrive_and_expect_tx(32768)
            │                                → 发起 TMA(gmem → state_acc)
            │                             全体: __syncthreads → wait(0)
            │                                    → fence_view_async_shared
            │
初始状态 ────┼─ HasStateIn && StateFP32 ──→ LOAD warp 单线程:
            │                                同上, 但目标是 state_fp32_buf
            │                                事务字节 65536, 布局 K_SW32
            │                             全体: __syncthreads → wait(0)
            │                                    → fence_view_async_shared
            │                             全体: smem_cvt_fp32_to_bf16
            │                                    → __syncthreads
            │
            └─ !HasStateIn ──────────────→ 全体线程跨步循环清零 state_acc
                                         → fence_view_async_shared
                                         → __syncthreads
```

两条 TMA 路径用的是**专用 barrier** `state_acc_tma_barrier`（`init(1)`，即 1 个到达者），与主循环 `load_pipeline` 的 barrier 家族无关——因为状态装载只发生一次，不值得动用多级流水线设施。

#### 4.1.3 源码精读

**路径 A：bf16 直通**（[csrc/smxx/fwd_kernel2.cuh:241-266](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L241-L266)）：

```cpp
if constexpr (HasStateIn && !StateFP32) {
    // BF16 state: TMA load directly into state_acc
    if (warp_role == WarpRole::LOAD_QKG && lane_predicate) {
        using BarrierType = cutlass::arch::ClusterTransactionBarrier::ValueType;
        constexpr uint32_t kStateTransactionBytes = cute::cosize_v<StateSmemLayout> * sizeof(BF16);

        shared_storage.state_acc_tma_barrier.init(1);
        cutlass::arch::fence_barrier_init();  // generic init -> visible to async proxy (TMA complete-tx)
        shared_storage.state_acc_tma_barrier.arrive_and_expect_tx(kStateTransactionBytes);

        Tensor g_init = tma_load_initial_state.get_tma_tensor(make_shape(N * H, D, D));
        auto init_off = g_init.layout()(seq_idx * H + head_idx, 0, 0);
        ...
        cute::copy(tma_load_initial_state.with(...barrier...), ...);
    }
    __syncthreads();
    shared_storage.state_acc_tma_barrier.wait(0);
    cutlass::arch::fence_view_async_shared();
}
```

要点：

- 只有 LOAD warp 的 leader lane（`lane_predicate = elect_one_sync()`，见 L237）执行 init / expect_tx / 发 TMA 三件事；`init(1)` 声明该 barrier 只等 1 次线程到达。
- 事务字节数 \( = \text{cosize}(\text{StateSmemLayout}) \times 2 = 16384 \times 2 = 32768 \) 字节，恰好是整个 128×128 bf16 矩阵——一次 TMA 装完。
- gmem 侧坐标 `seq_idx * H + head_idx` 把 grid 两维压回 `(N*H, D, D)` 布局的第一维。
- **同步收尾由全体线程完成**：`__syncthreads()` 先让全块到齐（也保证 barrier init 对所有线程可见），`wait(0)` 等 TMA 完成，最后 `fence_view_async_shared()` 把 async 代理的写对 generic 代理（MMA warp 接下来的 LDSM 读）打通。

**路径 B：fp32 中转**（[csrc/smxx/fwd_kernel2.cuh:267-304](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L267-L304)）：

```cpp
} else if constexpr (HasStateIn && StateFP32) {
    ...
    constexpr uint32_t kFP32StateTransactionBytes = cute::cosize_v<StateSmemLayout> * sizeof(float);
    ...
    Tensor s_fp32 = make_tensor(
        make_smem_ptr(reinterpret_cast<float*>(shared_storage.state_fp32_buf)),
        TMAFP32StateSmemLayout{});
    ...
    __syncthreads();
    shared_storage.state_acc_tma_barrier.wait(0);
    cutlass::arch::fence_view_async_shared();

    // All threads: convert fp32 -> bf16 with layout transformation
    smem_cvt_fp32_to_bf16<FP32StateSmemLayout, StateSmemLayout, D, NumThreads>(
        reinterpret_cast<float*>(shared_storage.state_fp32_buf),
        shared_storage.state_acc.begin(),
        threadIdx.x);
    __syncthreads();
}
```

差异点：

- TMA 目标换成 `state_fp32_buf`（65536 字节的 fp32 缓冲，来自 SharedStorageK2 的匿名 union，见 4.2.3），事务字节翻倍为 65536。
- smem 侧布局换成 `TMAFP32StateSmemLayout`（对应 `Layout_K_SW32_Atom<float>` 铺满 (128,128)）。
- wait + fence 之后多出一步：**全部线程**参与 `smem_cvt_fp32_to_bf16`（fp32 缓冲 → `state_acc`），再 `__syncthreads()` 确保转换完成，主循环才开跑。这一步是 generic 代理内的普通读写，块内 `__syncthreads` 已足够——不需要再对 async 代理 fence，因为接下来只有 MMA warp 用 generic 读它。

**路径 C：零初始化**（[csrc/smxx/fwd_kernel2.cuh:305-317](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L305-L317)）：

```cpp
} else {
    // No state in: zero-initialize state_acc
    {
        BF16* buf = shared_storage.state_acc.begin();
        constexpr int kTotal = cute::cosize_v<StateSmemLayout>;
        for (int i = threadIdx.x; i < kTotal; i += NumThreads) {
            buf[i] = BF16(0);
        }
    }
    // generic writes -> visible to async proxy (TMA state store covers t_tiles==0)
    cutlass::arch::fence_view_async_shared();
    __syncthreads();
}
```

两个细节值得咀嚼：

- 16384 个元素 / 192 线程 ≈ 每线程 86 次跨步写。这是纯 generic 写，为什么也要 `fence_view_async_shared`？源码注释给出答案：**"TMA state store covers t_tiles==0"**——varlen 模式允许空序列（`cu_seqlens[i+1] == cu_seqlens[i]`，校验链不禁止），此时 `t_tiles == 0`、主循环不执行、MMA warp 从不触碰 `state_acc`，但若 `HasStateOut`，输出路径的 TMA store（async 代理读 smem）仍会把 `state_acc` 写回 gmem。零初始化的 generic 写必须先对 async 代理可见，这次 fence 就是为这条边角路径买的保险；非空序列时它无害（fence 是低成本指令）。
- fence 在 `__syncthreads()` **之前**：每个线程先为自己「之前发出的写」建立可见性，再同步；顺序反过来会留下窗口（某线程的写在 sync 后才建立可见性，而别的代理可能已开始读）。

**launch 侧的描述符配合**（[csrc/smxx/fwd_launch.cu:118-144](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_launch.cu#L118-L144)）：

```cpp
auto make_state_tma = [&]() {
    if constexpr (StateFP32) {
        auto m_initial_fp32 = make_tensor(
            make_gmem_ptr(static_cast<float const*>(initial_state_ptr)), state_gmem_layout);
        ...
        auto tma_load = make_tma_copy(SM90_TMA_LOAD{}, m_initial_fp32, TMAFP32StateSmemLayout{});
        auto tma_store = make_tma_copy(SM90_TMA_STORE{}, m_final_fp32, TMAFP32StateSmemLayout{});
        return cute::make_tuple(tma_load, tma_store);
    } else {
        auto state_ptr_load = HasStateIn
            ? static_cast<BF16 const*>(initial_state_ptr)
            : reinterpret_cast<BF16 const*>(out_ptr);  // dummy, never used
        ...
        auto tma_load = make_tma_copy(SM90_TMA_LOAD{}, m_init, TMAStateSmemLayout{});
```

`StateFP32` 在这里是**类型级差异**：fp32 与 bf16 的 TMA 描述符、smem 布局是不同的 C++ 类型，无法运行时切换（u2-l3 已解释为何用模板分发）。无状态实例用 `out_ptr` 充当哑指针——描述符的类型必须存在，但对应的加载代码被 `if constexpr` 裁掉，永不被使用。

#### 4.1.4 代码实践：空序列的零初始化与「状态不动」性质

**实践目标**：验证路径 C 的注释——空序列（`t_tiles == 0`）时状态原样返回，从而同时覆盖「零初始化 + 直通输出」两条边角路径。

**操作步骤**（示例代码，保存为 `zero_state_check.py`）：

```python
import torch, flash_kda

torch.manual_seed(0)
T, H, D = 1024, 8, 128
LOWER_BOUND = -5.0
scale = D ** -0.5

B, T_seq, H_ = 1, T, H
q = torch.randn(B, T_seq, H_, D, dtype=torch.bfloat16, device='cuda')
k = torch.randn(B, T_seq, H_, D, dtype=torch.bfloat16, device='cuda')
v = torch.randn(B, T_seq, H_, D, dtype=torch.bfloat16, device='cuda')
g = torch.randn(B, T_seq, H_, D, dtype=torch.bfloat16, device='cuda')
beta = torch.randn(B, T_seq, H_, dtype=torch.bfloat16, device='cuda')
A_log = torch.rand(H_, dtype=torch.float32, device='cuda')
dt_bias = torch.rand(H_, D, dtype=torch.float32, device='cuda')
out = torch.zeros_like(v)

# 第一段长度为 0：seq 0 是空序列，seq 1 占满 1024
cu_seqlens = torch.tensor([0, 0, T], dtype=torch.int64, device='cuda')
N = 2

# bf16 状态直通：初始状态用「bf16 可精确表示」的值，如 0.5 的整数倍
h0 = (torch.randn(N, H_, D, D, dtype=torch.float32, device='cuda') * 0.25).to(torch.bfloat16)
final = torch.zeros_like(h0)

flash_kda.fwd(q, k, v, g, beta, scale, out, A_log, dt_bias, LOWER_BOUND,
              initial_state=h0, final_state=final, cu_seqlens=cu_seqlens)
torch.cuda.synchronize()

# 空序列的状态应原样返回：TMA 直通进出，逐比特相等
print("seq0 state unchanged (bitwise):", torch.equal(final[0], h0[0]))
# 对照：无初始状态的空序列，final_state[0] 应为全零（零初始化路径）
final2 = torch.zeros(N, H_, D, D, dtype=torch.bfloat16, device='cuda')
flash_kda.fwd(q, k, v, g, beta, scale, out, A_log, dt_bias, LOWER_BOUND,
              final_state=final2, cu_seqlens=cu_seqlens)
torch.cuda.synchronize()
print("seq0 zero-init state all zero:", bool((final2[0] == 0).all()))
```

**需要观察的现象**：两个布尔输出。

**预期结果**：两者均为 `True`——空序列下 TMA 载入 `h0[0]` 后主循环零次迭代，输出路径原样存回；无初始状态时零初始化的全零状态被存回。若把状态换成 fp32 模式（`h0`/`final` 用 `torch.float32`），第一条应变为「数值相等但非逐比特」——因为路径 B 多了 fp32→bf16→fp32 两次转换，超出 bf16 尾数精度的初值会被舍入。完整运行结果**待本地验证**（需 SM90 GPU + 已安装 flash_kda）。

#### 4.1.5 小练习与答案

**练习 1**：路径 A 中，如果把 `fence_view_async_shared()` 挪到 `wait(0)` 之前，程序可能出什么问题？

**答案**：fence 只对「调用时刻之前已完成的写」建立代理间可见性。放在 `wait` 之前时 TMA 尚未完成（数据还没落进 smem），即便 fence 通过，之后 MMA warp 的 LDSM 仍可能读到旧值。正确顺序是 `wait(0)`（确认字节到齐）→ `fence`（async→generic 可见性）→ 读。注意路径 C 恰好相反（先 fence 后 sync），因为那里的写是本线程自己发出的 generic 写，fence 针对的是「自己之前的写」，时序语义不同。

**练习 2**：路径 A 的事务字节是 32768，路径 B 是 65536。如果把路径 B 的 `arrive_and_expect_tx` 误写成 32768（照抄路径 A），会发生什么？

**答案**：mbarrier 的事务字节计数在 TMA 完成时硬件扣减。实际搬运 65536 字节而只登记 32768，计数会提前到零并翻转 phase，`wait(0)` 在**前一半数据未到**时放行，转换读到的后半是垃圾——静默数值错误，不报错不挂死。反向（登记 65536、实搬 32768）则计数永不归零，`wait` 永久挂死。这正是 u3-l3 强调「事务字节必须逐字节相等」的原因。

**练习 3**：为什么零初始化让全部 192 线程跨步写，而不是让 LOAD warp 一个 warp 写完？

**答案**：16384 元素（32768 字节）。单 warp 32 线程要写 512 元素/线程，而全块只需约 86 元素/线程；跨步循环的合并写入（coalesced）带宽随参与线程数提升，且这只是启动阶段的一次性开销，摊到整个序列的递推里可以忽略。

### 4.2 模块二：fp32↔bf16 的 smem 布局转换

#### 4.2.1 概念说明

路径 B 的核心问题是：TMA 载入的 fp32 数据躺在 `state_fp32_buf`（K_SW32 布局），而 `state_acc` 需要 bf16 的 K_INTER 布局（MMA 的 LDSM/STSM 依赖它，见 u2-l4、u3-l5）。两块缓冲的**逻辑形状**都是 128×128，但「逻辑坐标 → 物理偏移」的映射是两套函数。转换要同时完成两件事：

1. **数值**：fp32 → bf16 的 RNE 舍入（`BF16(float)`）或反向扩展（`bf16_to_f32`，内联 PTX `cvt.f32.bf16`，见 [csrc/smxx/utils.cuh:55-59](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/utils.cuh#L55-L59)）。
2. **布局**：数据从 K_SW32 排布搬到 K_INTER 排布。

关键设计在布局选择上。先看定义（[csrc/smxx/fwd_kernel2.cuh:59-69](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L59-L69)）：

```cpp
// FP32 state layout (K_SW32 atom, same 8x8 atom structure as K_INTER bf16)
using FP32StateSmemLayout = decltype(tile_to_shape(
    GMMA::Layout_K_SW32_Atom<float>{},
    make_shape(Int<D>{}, Int<D>{}),
    LayoutLeft{}
));
```

源码注释（[csrc/smxx/utils.cuh:371-376](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/utils.cuh#L371-L376)）点破了妙处：

> Both FP32 (K_SW32) and BF16 (K_INTER) layouts resolve to the same 8x8 atom structure with Swizzle<0,0,3>.

即：fp32 的 K_SW32 原子与 bf16 的 K_INTER 原子（各自 `tile_to_shape` 铺满 128×128 后）解析为**完全相同的 8×8 原子拼装结构**，且 swizzle 都是 `Swizzle<0,0,3>`——第一位 B=0 意味着 0 个比特参与异或，等价于**没有 swizzle**，原子内部是朴素的 8×8 行主排布。

「同构」带来一个决定性简化：逻辑坐标 \((r, c)\) 在两个布局中落到的**原子及其原子内槽位完全一致**。于是转换不需要任何跨原子搬运——以 \((r, c)\) 为锚，从 fp32 视图读、向 bf16 视图写，物理上数据只在「同一逻辑位置的宽度变化」（4 字节 → 2 字节）中移动。如果 fp32 侧随便选一个朴素行主布局 `(128,128):(128,1)`，TMA 固然也能搬（朴素布局可作 TMA 目标），但它与 K_INTER 的原子结构不同构，转换就得做真正的数据重排，索引计算复杂且带宽不友好。

顺带回答「为什么不干脆让 fp32 状态直接用朴素布局」的另一半：K_SW32 的 16 字节粒度（4×fp32）满足 TMA 对 smem 布局的粒度约束，是 GMMA 家族里「与 K_INTER 同构」的那个合法选择。

#### 4.2.2 核心流程

转换函数按 8×8 原子组织并行（`D=128`，`NumThreads=192`）：

- 原子网格：\( 16 \times 16 = 256 \) 个 8×8 原子；
- **每个 warp 领取一个原子**：`blk` 从 `warp_id` 出发、步进 `num_warps=6`；
- **每个 lane 转换 2 个元素**：原子内线性下标 \( e_0 = 2 \cdot \text{lane} \)、\( e_1 = e_0 + 1 \)，映射到原子内坐标
  \[ r = r_b + \lfloor e / 8 \rfloor, \qquad c = c_b + (e \bmod 8) \]
  其中 \((r_b, c_b)\) 是原子左上角；
- 读 `fp32_view(r, c)`、写 `bf16_view(r, c)`——同一逻辑坐标、两个布局视图。

覆盖性核算：每原子 64 元素 = 32 lane × 2；256 原子 / 6 warp = 42.67，即 warp 0~3 各处理 43 个原子、warp 4~5 各 42 个（\(6 \times 42 = 252\)，余 4 个给 warp 0~3）。

#### 4.2.3 源码精读

**正向转换**（[csrc/smxx/utils.cuh:378-407](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/utils.cuh#L378-L407)）：

```cpp
template <class FP32Layout, class BF16Layout, int D, int NumThreads>
__device__ void smem_cvt_fp32_to_bf16(
    float* __restrict__ fp32_smem,
    cutlass::bfloat16_t* __restrict__ bf16_smem,
    int tid
) {
    ...
    auto fp32_view = make_tensor(make_smem_ptr(fp32_smem), FP32Layout{});
    auto bf16_view = make_tensor(make_smem_ptr(bf16_smem), BF16Layout{});

    int warp_id = tid / kWarpSize;
    int lane_id = tid % kWarpSize;
    int num_warps = NumThreads / kWarpSize;

    for (int blk = warp_id; blk < kTotalBlocks; blk += num_warps) {
        int br = (blk / kBlocksPerDim) * kBlock;
        int bc = (blk % kBlocksPerDim) * kBlock;
        int e0 = lane_id * 2;
        int e1 = lane_id * 2 + 1;
        int r0 = br + e0 / kBlock, c0 = bc + e0 % kBlock;
        int r1 = br + e1 / kBlock, c1 = bc + e1 % kBlock;
        bf16_view(r0, c0) = BF16(fp32_view(r0, c0));
        bf16_view(r1, c1) = BF16(fp32_view(r1, c1));
    }
}
```

注意**读和写用同一个 \((r, c)\)**——这就是原子同构的直接体现：函数体里没有任何"跨原子"的坐标换算。`blk` 的行主展开（`blk / kBlocksPerDim` 为原子行）与 `tile_to_shape(..., LayoutLeft{})` 的原子铺装方向一致。

**逆向转换**（[csrc/smxx/utils.cuh:409-437](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/utils.cuh#L409-L437)）结构完全对称，仅两点不同：

```cpp
fp32_view(r0, c0) = bf16_to_f32(bf16_view(r0, c0));
fp32_view(r1, c1) = bf16_to_f32(bf16_view(r1, c1));
```

一是方向反过来（模板参数顺序也换为 `<BF16Layout, FP32Layout>`）；二是数值方向用了 `bf16_to_f32`（单条 `cvt.f32.bf16` PTX）而非 `float(...)`——bf16→fp32 本身无损（只是左移 16 位补零），用哪种写法数值都一样，选 PTX 内联是编译器友好习惯。

**union 复用**（[csrc/smxx/fwd_kernel2.cuh:99-107](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L99-L107)）：

```cpp
// Anonymous union: pipeline buffers share space with fp32 state conversion buffer.
// FP32 state load/store happens before/after the pipeline loop, so no overlap.
union {
    struct {
        InputStorage input[InputStages];
        OutputStorage output[OutputStages];
    };
    alignas(128) char state_fp32_buf[cute::cosize_v<StateSmemLayout> * sizeof(float)];
};
```

fp32 中转缓冲 65536 字节；流水线侧为 3×17984（input[3]）+ 2×4096（output[2]）= 62144 字节。union 取 max = 65536。生命周期错开保证安全：fp32 **输入**转换发生在流水线启动前，fp32 **输出**转换发生在流水线排空后（主循环结束、所有 stage 已释放，见 4.3.3 的第一次 `__syncthreads`）。这也解释了 u3-l2 提过的「union 钳制效应」——`state_fp32_buf` 的大小使得减流水线级数未必真正省 smem。

#### 4.2.4 代码实践：用 Python 复现转换的线程-数据映射

**实践目标**：验证 4.2.2 的覆盖性论断——256 个原子 × 64 元素被 6 个 warp 无重叠完整覆盖；同时产出一张「warp → 原子集合」的分配表。

**操作步骤**（示例代码，保存为 `cvt_map.py`，纯 CPU 可运行）：

```python
D, NumThreads, kBlock = 128, 192, 8
kBlocksPerDim = D // kBlock            # 16
kTotalBlocks = kBlocksPerDim ** 2      # 256
num_warps = NumThreads // 32           # 6

covered = {}                            # (r, c) -> (warp, lane)
for tid in range(NumThreads):
    warp, lane = tid // 32, tid % 32
    for blk in range(warp, kTotalBlocks, num_warps):
        br = (blk // kBlocksPerDim) * kBlock
        bc = (blk % kBlocksPerDim) * kBlock
        for e in (lane * 2, lane * 2 + 1):
            coord = (br + e // kBlock, bc + e % kBlock)
            assert coord not in covered, f"overlap at {coord}"
            covered[coord] = (warp, lane)

assert len(covered) == D * D
from collections import Counter
per_warp_atoms = Counter(
    blk for blk in range(kTotalBlocks) for w in [blk % num_warps] if True
)
print("total covered:", len(covered))
print("atoms per warp:", {w: sum(1 for b in range(kTotalBlocks) if b % num_warps == w)
                          for w in range(num_warps)})
# 额外核对：warp 0 的 lane 5 处理的原子 0 内的两个元素
print("warp0/lane5 in atom0 ->", [(0 // 8, (10) % 8), (0, 11)])
```

**需要观察的现象**：`assert` 全部通过；打印出的每 warp 原子数为 `{0: 43, 1: 43, 2: 43, 3: 43, 4: 42, 5: 42}`。

**预期结果**：16384 个坐标被精确覆盖一次、无重叠；warp 0 的 lane 5 在原子 0 内负责 \((0, 10)\) 与 \((0, 11)\)（\(e_0=10, e_1=11\)，均落在原子第 0 行）。此脚本不依赖 GPU，可直接运行验证；打印格式若与手推不同，请以 assert 结论为准。

#### 4.2.5 小练习与答案

**练习 1**：`StateSmemLayout` 与 `FP32StateSmemLayout` 的 cosize 各是多少？对应的字节数呢？

**答案**：两者 cosize 都是 16384 元素（128×128，swizzle 不改变 cosize）。字节数分别为 16384×2 = 32768（bf16）与 16384×4 = 65536（fp32）。后者即 `state_fp32_buf` 的大小，也是路径 B 的事务字节数。

**练习 2**：如果未来支持 `D=64`（假设其余机制不变），转换函数里 `kTotalBlocks` 变为多少？6 个 warp 下每个 warp 处理几个原子？

**答案**：`kBlocksPerDim = 64/8 = 8`，`kTotalBlocks = 64`。64 = 6×10 + 4，warp 0~3 各 11 个、warp 4~5 各 10 个。（实际上本项目 D=128 是硬约束，支持新 head_dim 还要动布局、TMA、MMA 与 workspace 全链，见 u3-l12。）

**练习 3**：转换函数里读写都用逻辑坐标 `(r, c)`，却声称「物理上数据只在原子内移动」。请解释这句话为何成立。

**答案**：两个布局的原子铺装方式相同（`tile_to_shape` 同为 `LayoutLeft`、原子网格同为 16×16），且原子内部都是无 swizzle 的 8×8 行主结构（`Swizzle<0,0,3>`，B=0 无异或）。因此逻辑坐标 \((r,c)\) → （第几个原子, 原子内第几个元素）的分解在两个布局中逐位一致；差异只在每个元素占 2 字节还是 4 字节。搬运自然被限制在原子内部，不存在跨原子的乱序重排。

### 4.3 模块三：两条状态输出路径与围栏时序

#### 4.3.1 概念说明

主循环排空后，`state_acc` 里躺着最终状态。把它写回 gmem 的 `final_state[N, H, D, D]` 有两条路径：

1. **bf16 直通**：STORE warp 单线程发起 TMA store，`state_acc`（bf16、K_INTER 布局）→ gmem，逐比特直拷。紧接在 out 循环之后，仍由 STORE warp 顺手完成。
2. **fp32 转换**：全体线程先 `__syncthreads`（等流水线缓冲彻底释放，union 可复用），再全块做 `smem_cvt_bf16_to_fp32`（`state_acc` → `state_fp32_buf`），fence + sync 后由 STORE warp 发 TMA store（fp32、K_SW32 布局）→ gmem。

两条路径的可见性保障方式截然不同：bf16 路径**复用主循环已建立的 fence 链**，fp32 路径**自己显式 fence**。这个对比是本模块的重点。

#### 4.3.2 核心流程

```text
主循环结束（每个 tile 收尾时 MMA warp 已做过 compute_barrier.arrive_and_wait
                                → fence_view_async_shared → producer_commit）
        │
        ├─ HasStateOut && !StateFP32 ──→ STORE warp(单线程):
        │         g_final 坐标 (seq_idx*H+head_idx, 0, 0)
        │         TMA store(state_acc → gmem, K_INTER 布局)
        │         tma_store_arrive()
        │
        └─ HasStateOut && StateFP32 ──→ 全体: __syncthreads   ← union 复用前哨
                  全体: smem_cvt_bf16_to_fp32(state_acc → state_fp32_buf)
                  全体: fence_view_async_shared   ← generic→async
                  全体: __syncthreads             ← 转换完成
                  STORE warp(单线程): TMA store(state_fp32_buf → gmem, K_SW32 布局)
                                      tma_store_arrive()
        │
        └─ 全体: __syncthreads（kernel 末尾）
```

#### 4.3.3 源码精读

**先看 MMA warp 的收尾**（[csrc/smxx/fwd_kernel2.cuh:733-741](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L733-L741)）——它是 bf16 输出路径免 fence 的依据：

```cpp
compute_barrier.arrive_and_wait();      // NamedBarrier(128): 4 个 MMA warp 会师

#ifndef TMA_DISABLE_ALL
    cutlass::arch::fence_view_async_shared();   // MMA 的 generic 写 → async 可见
    store_pipeline.producer_commit(out_write);  // 放行 STORE warp
    load_pipeline.consumer_release(load_read);  // 归还 input stage
    ++load_read;
    ++out_write;
#endif
```

每个 tile 结束时，MMA warps 先用 `NamedBarrier(128)` 会师（保证 Phase 6 对 `state_acc` 的 STSM 全部落地，u3-l4/u3-l5 已析），随后**先 fence 再 commit**。STORE warp 对最后一个 tile 的 `consumer_wait` 被 `producer_commit` 放行时，本次迭代写出的 out 与整个 `state_acc`（它由所有 tile 累积更新，最后一次更新发生在最后一个 tile 的 Phase 6）都已通过 fence 对 async 代理可见。

**bf16 直通输出**（[csrc/smxx/fwd_kernel2.cuh:786-801](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L786-L801)）：

```cpp
if constexpr (HasStateOut && !StateFP32) {
    // BF16 state: TMA store directly from state_acc
    Tensor g_final = tma_store_final_state.get_tma_tensor(make_shape(N * H, D, D));
    auto state_off = g_final.layout()(seq_idx * H + head_idx, 0, 0);
    ...
    Tensor s_state = make_tensor(make_smem_ptr(shared_storage.state_acc.begin()), TMAStateSmemLayout{});

    auto cta_tma_store_state = tma_store_final_state.get_slice(Int<0>{});
    cute::copy(tma_store_final_state,
        cta_tma_store_state.partition_S(s_state),
        cta_tma_store_state.partition_D(g_final_tile));
    tma_store_arrive();
}
```

这段在 STORE warp 的 out 循环之后执行（仍在 `warp_role == STORE` 分支内）：TMA（async 代理）读 `state_acc` 的正确性由上面的 fence 链背书——STORE warp 能走到这里，意味着最后一个 tile 的 `consumer_wait` 已返回，而那次放行晚于 MMA 的 `fence_view_async_shared`。`tma_store_arrive()` 向 TMA 的完成队列登记（group commit 语义）；与 out 循环里每个 tile 的 `tma_store_wait<0>()`（为归还 output stage，L781）不同，state store 是 kernel 的收尾动作之一，其后只余 `__syncthreads()`（L837）与 kernel 退出，无需再等待。

**fp32 转换输出**（[csrc/smxx/fwd_kernel2.cuh:804-835](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L804-L835)）：

```cpp
if constexpr (HasStateOut && StateFP32) {
    // FP32 state: all threads sync, convert bf16->fp32, then STORE warp does TMA
    ...
    __syncthreads();  // all warps sync — pipeline smem now free

    smem_cvt_bf16_to_fp32<StateSmemLayout, FP32StateSmemLayout, D, NumThreads>(
        shared_storage.state_acc.begin(),
        reinterpret_cast<float*>(shared_storage.state_fp32_buf),
        threadIdx.x);
    cutlass::arch::fence_view_async_shared();  // generic-proxy writes -> visible to async proxy (TMA)
    __syncthreads();  // conversion complete

    if (warp_role == WarpRole::STORE && lane_predicate) {
        ...
        Tensor s_fp32 = make_tensor(
            make_smem_ptr(reinterpret_cast<float*>(shared_storage.state_fp32_buf)),
            TMAFP32StateSmemLayout{});
        cute::copy(tma_store_final_state, ..., cta_tma_store_state.partition_D(g_final_tile));
        tma_store_arrive();
    }
}
```

注意这个 `if constexpr` 块在 warp 角色分支**之外**——前三步（两次 sync + 转换 + fence）由全部 192 线程执行，只有最后的 TMA 发起收窄到 STORE warp 的 leader lane。逐条理解围栏时序：

1. **第一次 `__syncthreads()`**：注释 "pipeline smem now free"。此时 LOAD warp 已过 `producer_tail`、MMA warps 已出主循环、STORE warp 已消费完所有 output stage——union 的另一侧（`input[]`/`output[]`）不再被任何人读写，写入 `state_fp32_buf` 才不会互相践踏。
2. **转换**：全块 generic 读写（模块二的逆向函数）。
3. **`fence_view_async_shared()`**：转换是 generic 写，接下来的读者是 TMA（async 代理）——必须打通 generic→async。与路径 A/C 中的同款 fence 同方向（generic 写在先、async 读在后）。
4. **第二次 `__syncthreads()`**：等所有线程的转换与 fence 都完成，STORE warp 才能安全发起 TMA。

对照之下可以总结一条通用判据：**TMA 写 smem 之后、generic 读之前，需要 fence（async→generic）；generic 写 smem 之后、TMA 读之前，也需要 fence（generic→async）；两次都常伴随 `__syncthreads` 划定块的统一进度点。** bf16 直通输出之所以免 fence，是因为这条链已在主循环的每次 commit 里由 MMA warp 代办——不是不需要，而是已经做过。

#### 4.3.4 代码实践：同步链追踪表

**实践目标**：把本讲所有输入/输出路径的同步原语整理成一张可对照源码的时序表，训练「读围栏知方向」的能力。

**操作步骤**：

1. 打开 [csrc/smxx/fwd_kernel2.cuh:239-317](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L239-L317) 与 [csrc/smxx/fwd_kernel2.cuh:786-835](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L786-L835)，为下表每行填入「执行者 / 围栏方向 / 若缺失的后果」：

| 路径 | 关键原语（按序） | 执行者 | 围栏方向 | 缺失后果 |
|---|---|---|---|---|
| 输入 A（bf16） | `fence_barrier_init` | ？ | ？ | ？ |
| 输入 A（bf16） | `wait(0)` → `fence_view_async_shared` | ？ | ？ | ？ |
| 输入 B（fp32） | 转换后 `__syncthreads` | ？ | —— | ？ |
| 输入 C（零初始化） | `fence_view_async_shared` → `__syncthreads` | ？ | ？ | ？ |
| 输出 bf16 | （无显式 fence） | ？ | ？ | —— |
| 输出 fp32 | `__syncthreads` → 转换 → fence → `__syncthreads` | ？ | ？ | ？ |

2. 完成后与 4.1.3 / 4.3.3 的讲解互相校对，把有分歧的格子标记出来，回到源码确认。

**需要观察的现象**：自己填写的表与讲义正文是否在每个格子上一致；重点自查「输入 C 的 fence 为谁服务」（答案不是主循环，而是 `t_tiles==0` 时的 state store）。

**预期结果**：能独立复述六条路径的同步链；特别是「输出 bf16 无显式 fence」一格能写出依据（MMA 侧 L736 的 fence + commit 先于 STORE warp 的 consumer_wait 返回）。

#### 4.3.5 小练习与答案

**练习 1**：fp32 输出路径第一次 `__syncthreads()`（L809）若被删除，最直接的受害者是谁？

**答案**：union 的另一侧。若 LOAD/MMA/STORE 任何一个 warp 还没跑完自己的流水线尾段（例如 STORE warp 还在消费最后一个 output stage），全块就开始向 `state_fp32_buf` 写转换结果——两者是同一块物理内存，轻则 final_state 数据被流水线写污染，重则 out 数据被破坏。它不是围栏问题，而是**生命周期**问题。

**练习 2**：`t_tiles == 0`（空序列）且 `HasStateOut && StateFP32` 时，输出路径会发生哪些「白做」的量？

**答案**：状态仍是初始值（或零初始化值），但流程照走：bf16→fp32 转换要写满 65536 字节的 `state_fp32_buf`，TMA store 再搬 65536 字节回 gmem。即空序列也付出一次完整状态 I/O。这也是 4.1.4 实验能工作的机制保障。

**练习 3**：为什么 bf16 直通输出放在 STORE warp（out 循环之后）里，而 fp32 输出的 TMA 虽然也由 STORE warp 发起，转换却由全块做？

**答案**：bf16 直通只是「单线程发一条 TMA」的轻活，STORE warp 顺手完成且天然满足时序（它最后离开 out 循环）；fp32 路径必须先把 16384 个元素从 `state_acc` 转换到 `state_fp32_buf`——单 warp 干要 512 元素/线程，且此时全块空闲，让 192 线程分摊（86 元素/线程）既快又不占额外资源。转换后的 TMA 发起仍是单线程动作，维持「TMA 只由一个 lane 发起」的项目惯例（u2-l6、u3-l3）。

## 5. 综合实践：state_precision.py——bf16 与 fp32 状态模式的精度对拍

**实践目标**：用 fla 的 fp64 `fused_recurrent_kda` 作金标，量化 bf16 / fp32 两种状态模式下 `final_state` 的误差；并通过两种模式互相对比，验证本讲的核心论断——`StateFP32` 只改变 I/O 精度，不改变片上计算精度，因此两种模式的数值结果应几乎一致。

**操作步骤**（示例代码，保存为 `state_precision.py`；依赖 `pip install flash-linear-attention`，见 u1-l3）：

```python
import math
import torch
import torch.nn.functional as F
import flash_kda

T, H, D, LOWER_BOUND = 8192, 32, 128, -5.0
scale = 1.0 / math.sqrt(D)
torch.manual_seed(0)

# ---- 输入构造（仿 tests/test_fwd.py::test_fwd）----
q = F.normalize(torch.randn(1, T, H, D, dtype=torch.float32, device='cuda'), p=2, dim=-1).to(torch.bfloat16)
k = F.normalize(torch.randn(1, T, H, D, dtype=torch.float32, device='cuda'), p=2, dim=-1).to(torch.bfloat16)
v = torch.randn(1, T, H, D, dtype=torch.bfloat16, device='cuda')
g = torch.randn(1, T, H, D, dtype=torch.bfloat16, device='cuda')
beta = torch.randn(1, T, H, dtype=torch.bfloat16, device='cuda')
A_log = torch.rand(H, dtype=torch.float32, device='cuda')
dt_bias = torch.rand(H, D, dtype=torch.float32, device='cuda')

h0_fp32 = torch.randn(1, H, D, D, dtype=torch.float32, device='cuda') * 0.1

def run(mode):
    out = torch.zeros(1, T, H, D, dtype=torch.bfloat16, device='cuda')
    if mode == 'bf16':
        init, final = h0_fp32.to(torch.bfloat16), torch.zeros(1, H, D, D, dtype=torch.bfloat16, device='cuda')
    else:
        init, final = h0_fp32.clone(), torch.zeros(1, H, D, D, dtype=torch.float32, device='cuda')
    flash_kda.fwd(q, k, v, g, beta, scale, out, A_log, dt_bias, LOWER_BOUND,
                  initial_state=init, final_state=final)
    torch.cuda.synchronize()
    return final.float()

final_bf16 = run('bf16')
final_fp32 = run('fp32')

# ---- fp64 金标（复刻 tests/test_fwd.py::run_fla_gold_reference 的调用方式）----
from fla.ops.kda import fused_recurrent_kda
g_act = LOWER_BOUND * torch.sigmoid(
    torch.exp(A_log.double().view(1, 1, H, 1)) * (g.double() + dt_bias.double().view(1, 1, 1, D)))
beta_act = torch.sigmoid(beta.double())
tri, tri_ht = fused_recurrent_kda(
    q=q.double(), k=k.double(), v=v.double(),
    g=g_act, beta=beta_act,
    A_log=None, dt_bias=None, scale=scale,
    initial_state=h0_fp32.double(), output_final_state=True,
    use_qk_l2norm_in_kernel=True, use_gate_in_kernel=False,
    lower_bound=None, transpose_state_layout=True)
gold = tri_ht.float()

for name, x in [('bf16-state', final_bf16), ('fp32-state', final_fp32)]:
    diff = (gold - x).abs()
    print(f"{name}: max_err={diff.max().item():.6f}  mean_err={diff.mean().item():.3e}")

# ---- 核心验证：两种模式互相对比 ----
cross = (final_bf16 - final_fp32).abs()
print(f"bf16 vs fp32 mode: max_diff={cross.max().item():.3e}")
print("bitwise identical:", torch.equal(final_bf16, final_fp32.to(torch.bfloat16)))
```

**需要观察的现象**：

1. 两种模式相对 fp64 金标的 `max_err` / `mean_err`（预期同为 1e-2 量级——片上 bf16 状态递推 512 个 tile 的累积量化是主要误差源，与 u3-l8 的精度地图一致）；
2. 两种模式之间的 `max_diff`（预期接近 0：bf16 模式 host 侧 `.to(bf16)` 与 fp32 模式 kernel 内 `BF16(fp32)` 都是 RNE 单次量化，理论上传给 `state_acc` 的初始比特相同，后续计算完全同路径）；
3. `bitwise identical` 是否为 `True`。

**预期结果**：两种模式误差同数量级、互相差异远小于各自对金标的误差，从而实证「fp32 状态模式不改片上精度，其价值在于 gmem 接口精度与生态兼容（如 fla 的 fp32 状态约定）」。若 `bitwise identical` 为 `False`，请检查差值是否恰为单个 bf16 ULP（1/128 相对量级）——个别元素在 host 与 kernel 的舍入实现差异下可能差 1 ULP。完整数值**待本地验证**。

**扩展**：把 `T` 改为 1024 / 8192 / 32768 各跑一次，观察误差随 tile 数增长的趋势，验证「状态量化误差随递推长度累积」；再用 `initial_state=None`（零初始化路径）重跑，观察短序列下两种模式差异是否消失。

## 6. 本讲小结

- K2 的状态进出口由 `HasStateIn` / `HasStateOut` / `StateFP32` 编译期裁剪：输入三选一（bf16 TMA 直通 / fp32 中转+全块转换 / 零初始化），输出二选一（bf16 直通 TMA store / fp32 转换后 TMA store）。
- 两条 TMA 装载路径用**专用** `ClusterTransactionBarrier`（`init(1)`），事务字节必须逐字节精确：bf16 路径 32768、fp32 路径 65536。
- `FP32StateSmemLayout` 选 `Layout_K_SW32_Atom<float>` 的妙处：与 bf16 的 `K_INTER` 原子同构（同为 8×8 原子、`Swizzle<0,0,3>` 即无异或），fp32↔bf16 转换退化为「按 8×8 原子、每线程 2 元素」的逐逻辑坐标类型搬运，256 个原子由 6 个 warp 分摊（43/43/43/43/42/42）。
- 围栏方向的通用判据：TMA 写 smem 后 generic 读之前要 fence（async→generic）；generic 写 smem 后 TMA 读之前也要 fence（generic→async）。零初始化路径的 fence 专为 `t_tiles==0` 时「TMA store 读全零状态」这条边角路径服务。
- bf16 状态输出**不显式 fence** 的原因：MMA warp 每个 tile 收尾都「先 `fence_view_async_shared` 再 `producer_commit`」，STORE warp 走到状态存储时可见性链早已建立；fp32 输出则需「`__syncthreads`（释放 union）→ 全块转换 → fence → `__syncthreads`」四步。
- **反直觉结论**：`StateFP32` 不改变片上计算精度——`state_acc` 永远是 bf16，主循环对该模板参数零分支；fp32 模式改变的是 gmem I/O 的 dtype 与接口语义（综合实践可实证两种模式数值几乎一致）。

## 7. 下一步学习建议

本讲补完了 K2 的最后一块拼图（状态进出口），至此两个 kernel 的全部代码路径都已覆盖。建议接下来：

1. **u3-l7（STORE warp 与尾块处理）**：同在 STORE warp 的 out 循环里，讲尾块为何必须绕过 TMA 改用逐元素写——与本讲的 state store 相邻，是 varlen 正确性的另一关键分支。
2. **u3-l8（数值精度总账）**：把本讲「片上状态恒为 bf16、I/O 按模式选精度」放进全 kernel 的 bf16/fp32 分工地图，理解量化点分布与 `CHUNK=16` 的动态范围论证。
3. **u3-l9（测试方法学）**：本讲综合实践用到的 fp64 金标对拍正是 `tests/test_fwd.py` 的方法；下一讲系统化地讲 bit-exact 参考与参数扫描。
4. 想动手验证围栏时序的读者，可以结合 u3-l12 的消融方法：注释掉 `fwd_kernel2.cuh` 中某条 `fence_view_async_shared` 后重编译，跑 `tests/test_fwd.py` 观察是静默出错还是挂死（预期：删 async→generic 的 fence 更可能表现为数值错误，删 generic→async 的 fence 在 `t_tiles>0` 时可能侥幸通过、空序列用例才暴露）——注意这是实验性改动，请在副本分支上进行。
