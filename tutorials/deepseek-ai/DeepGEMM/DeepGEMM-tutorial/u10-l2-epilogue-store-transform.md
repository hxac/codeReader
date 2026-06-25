# Epilogue 与存储变换

## 1. 本讲目标

矩阵乘 \(D = C + A @ B\) 的核心计算（tensor core 上的乘加）只完成了一半工作。计算结束后，累加器里的结果还停留在 GPU 片上寄存器/TMEM 里，必须经过一道**「收尾（epilogue）」**流程才能写回到全局显存。本讲专门拆解 DeepGEMM 设备 kernel 的这道收尾环节。学完后你应该掌握：

1. 理解 **EpilogueHeadSplits** 如何用一条整数算术公式，在「按 N 维存储」时把连续的列号重映射成「带 mid 空洞」的真实输出坐标，从而服务于 `fp8_gemm_nt_skip_head_mid`。
2. 掌握 SM100 上 **C/D store** 的两条路径——`sm100_store_cd` 与 `sm100_store_cd_swap_ab`——如何把 TMEM 里的累加结果经共享内存（带 swizzle）搬出，再用 TMA 异步写回全局显存，以及 `swap_ab` 对应的转置存储布局。
3. 看懂 **epilogue 作为「编译期模板策略（policy）」** 如何被注入到 GEMM kernel：宿主只决定一个**类型名字符串**，JIT 代码生成把它烤成设备 kernel 的模板参数，由编译器把索引重映射内联/优化掉，普通 GEMM 用恒等策略 `EpilogueIdentity` 零开销。

本讲承接 u6-l1（SM90 FP8 GEMM 1D1D 的内核结构与共享内存划分）、u6-l2（SM100 UMMA 把累加器搬进 TMEM）与 u10-l1（SM100 1D1D kernel 的整体差异）。

## 2. 前置知识

- **epilogue（收尾）**：tensor core 算完乘加后、把结果写回全局显存之前的那段处理。它可以包含类型转换（FP32→BF16）、缩放、非线性、以及**输出坐标重映射**。本讲的 epilogue 不做数值变换，只做「写到哪里」的重映射。
- **TMEM（tensor memory）**：SM100 新增的片上专用存储，UMMA 的累加结果落在 TMEM 里（见 u6-l2）。store 的第一步就是用 `SM100_TMEM_LOAD_*` 把结果从 TMEM 读出来。
- **共享内存 swizzle**：用可逆的地址 XOR 重排，消除共享内存 bank conflict（见 u4-l2）。store epilogue 要求 C/D 在共享内存里必须 swizzle（`kSwizzleCDMode > 0`），以满足 TMA store 的布局契约。
- **TMA store**：与 TMA load 对称的异步全局显存写入，由 `cute::SM90_TMA_STORE_2D/3D` 封装；带累加时换成 `SM90_TMA_REDUCE_ADD_2D/3D`（原子累加回原地址）。
- **NT 布局与原生 kernel**：输出 D 始终是行主序（N-major），nt 是唯一原生 kernel（见 u2-l1）。
- **JIT 模板实例化**：宿主运行时把编译期常量（含 epilogue 类型名）填进 `.cu` 模板，靠「取函数地址」强制实例化（见 u3-l2）。epilogue 类型就是这一机制的典型应用。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [`deep_gemm/include/deep_gemm/epilogue/transform.cuh`](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/epilogue/transform.cuh) | 定义两个 epilogue **策略类型**：`EpilogueIdentity`（恒等，普通 GEMM 用）与 `EpilogueHeadSplits`（N 维索引重映射，`skip_head_mid` 用）。 |
| [`deep_gemm/include/deep_gemm/epilogue/sm100_store_cd.cuh`](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/epilogue/sm100_store_cd.cuh) | SM100 **非转置**存储 epilogue：TMEM→swizzle 共享内存→TMA store。 |
| [`deep_gemm/include/deep_gemm/epilogue/sm100_store_cd_swap_ab.cuh`](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/epilogue/sm100_store_cd_swap_ab.cuh) | SM100 **swap-ab 转置**存储 epilogue：把 M/N 对调的累加结果按转置布局写回。 |
| [`csrc/apis/attention.hpp`](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/apis/attention.hpp) | `fp8_gemm_nt_skip_head_mid` 的宿主实现：校验 head_splits、构造 epilogue 类型字符串、按架构派发。 |
| [`csrc/jit_kernels/impls/epilogue.hpp`](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/impls/epilogue.hpp) | `get_default_epilogue_type`：未指定 epilogue 时回退到 `EpilogueIdentity`。 |
| [`csrc/jit_kernels/impls/sm100_fp8_fp4_gemm_1d1d.hpp`](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/impls/sm100_fp8_fp4_gemm_1d1d.hpp) | SM100 1D1D 宿主 Runtime：把 epilogue 字符串经代码生成注入设备模板。 |
| [`deep_gemm/include/deep_gemm/impls/sm100_fp8_fp4_gemm_1d1d.cuh`](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/impls/sm100_fp8_fp4_gemm_1d1d.cuh) | SM100 1D1D 设备 kernel：在收尾阶段按 `kSwapAB` 选择两条 store 路径。 |
| [`deep_gemm/include/deep_gemm/impls/sm90_fp8_gemm_1d2d.cuh`](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/impls/sm90_fp8_gemm_1d2d.cuh) | SM90 1D2D 设备 kernel：其 TMA store 同样调用 `apply_index_n` 做 N 维重映射。 |
| [`tests/test_attention.py`](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/tests/test_attention.py) | `test_gemm_skip_head_mid` 与参考实现 `apply_skip_head_mid`，用于验证重映射正确性。 |

## 4. 核心概念与源码讲解

### 4.1 EpilogueHeadSplits：N 维索引重映射

#### 4.1.1 概念说明

先说清楚这个 epilogue 要解决什么问题。注意力（attention）场景里，每个 head 的输出常常被拆成「左半 left + 右半 right」两段（例如 QK^T 的一部分与另一部分），中间预留一段 `mid` 的「空洞」留给后续填充（典型是 attention sink 或预留位）。但 GEMM 本身只算「实」的部分——也就是把每个 head 看作 `left + right` 列连续排布，GEMM 的 N 维就是 `num_heads * (left + right)`。

可是用户拿到的输出张量 `D` 是带空洞的：形状是

\[
N_{\text{out}} = N + \frac{N}{\text{left}+\text{right}} \times \text{mid}
\]

也就是每个 head 占 `left + mid + right` 列，`mid` 那段是零。如果让 kernel 先算出紧凑的 `[M, N]` 再在宿主侧插入零段，就要多一次显存读写。DeepGEMM 的做法是：**kernel 内部仍按紧凑 N 计算**，但在**存储那一刻**，用一个 `apply_index_n` 把「紧凑列号」实时翻译成「带空洞的真实列号」，直接把结果写到 `D` 的正确位置。这样省掉了额外的拷贝/插入 kernel，且空洞段天然保持零（只要调用方预先清零，或不需要关心其值）。

这个「紧凑列号 → 真实列号」的翻译，就是一个 epilogue 策略要做的事。

#### 4.1.2 核心流程

设 head 划分为 `(left, mid, right)`，紧凑输出共 \(N = H \cdot (\text{left}+\text{right})\) 列（\(H\) 为 head 数）。紧凑列号 \(n\) 属于 head \(h = \lfloor n / (\text{left}+\text{right}) \rfloor\)，head 内偏移 \(p = n \bmod (\text{left}+\text{right})\)。带空洞张量里，head \(h\) 占据列区间 \([h\cdot(\text{left}+\text{mid}+\text{right}),\ (h+1)\cdot(\text{left}+\text{mid}+\text{right}))\)，其中：

- 前 `left` 列是 head 的「左半」实数据（\(p < \text{left}\)）；
- 中间 `mid` 列是空洞；
- 后 `right` 列是 head 的「右半」实数据（\(\text{left} \le p < \text{left}+\text{right}\)）。

因此真实列号应为：

\[
\text{real}(n) = n + h \cdot \text{mid} \quad (p < \text{left})
\]

\[
\text{real}(n) = n + (h+1) \cdot \text{mid} \quad (\text{left} \le p < \text{left}+\text{right})
\]

DeepGEMM 把这两种情况合并成**一条**无分支整数公式：

\[
\text{real}(n) = n + \left\lfloor \frac{n + \text{right}}{\text{left}+\text{right}} \right\rfloor \cdot \text{mid}
\]

**为什么 `+right` 能把两种情况统一？** 关键在于整数除法的「跳变点」。我们希望除法结果在 head 内的左/右分界处（紧凑列号 \(n = h\cdot(\text{left}+\text{right}) + \text{left}\)）正好从 \(h\) 跳到 \(h+1\)。考察 \(\lfloor (n+\text{right})/(\text{left}+\text{right}) \rfloor\) 跳变为 \(h+1\) 的条件是 \(n+\text{right} \ge (h+1)\cdot(\text{left}+\text{right})\)，即 \(n \ge h\cdot(\text{left}+\text{right}) + \text{left}\)，恰好就是左/右分界。于是：

- \(p < \text{left}\)：除法得 \(h\)，结果 \(= n + h\cdot\text{mid}\)；
- \(p \ge \text{left}\)：除法得 \(h+1\)，结果 \(= n + (h+1)\cdot\text{mid}\)。

`+right` 这个偏移量正是为了让整数除法的边界对齐到 head 内部 left/right 的切分点。整条公式无分支、纯整数运算，非常适合 GPU。

#### 4.1.3 源码精读

策略类型定义在 `transform.cuh`，极其简短：

```cpp
// [transform.cuh:7-L12] 恒等策略：普通 GEMM 的默认 epilogue，apply_index_n 原样返回列号
struct EpilogueIdentity {
    template <uint32_t STORE_BLOCK_N>
    CUTLASS_DEVICE static uint32_t apply_index_n(const uint32_t& n_idx) {
        return n_idx;
    }
};
```

[transform.cuh:7-12](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/epilogue/transform.cuh#L7-L12) 定义 `EpilogueIdentity`：`apply_index_n` 原样返回 `n_idx`，即「不重映射」。普通 GEMM 走它，编译后这段调用会被完全内联消除，零开销。

```cpp
// [transform.cuh:14-L22] HeadSplits 策略：把紧凑列号映射到带 mid 空洞的真实列号
template <uint32_t kLeft, uint32_t kMid, uint32_t kRight>
struct EpilogueHeadSplits: EpilogueIdentity {
    template <uint32_t STORE_BLOCK_N>
    CUTLASS_DEVICE static uint32_t apply_index_n(const uint32_t& n_idx) {
        DG_STATIC_ASSERT(kLeft % STORE_BLOCK_N == 0 and kMid % STORE_BLOCK_N == 0 and
                         kRight % STORE_BLOCK_N == 0, "Invalid head splits config");
        return n_idx + (n_idx + kRight) / (kLeft + kRight) * kMid;
    }
};
```

[transform.cuh:14-22](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/epilogue/transform.cuh#L14-L22) 定义 `EpilogueHeadSplits<kLeft, kMid, kRight>`，正是上文推导的公式 `n_idx + (n_idx + kRight) / (kLeft + kRight) * kMid`。两个要点：

1. **继承自 `EpilogueIdentity`**：只是为了复用/约定接口形状（提供同名 `apply_index_n`），实际重写了逻辑。
2. **`DG_STATIC_ASSERT(kLeft % STORE_BLOCK_N == 0 ...)`**：要求 left/mid/right 都能被「存储块宽」整除。这是因为 `apply_index_n` 是**对一个 STORE_BLOCK_N 宽的整块**算一个真实起点坐标（见 4.2），随后 TMA 一次写连续 STORE_BLOCK_N 列。若一个存储块横跨了 left/mid 或 mid/right 的边界，块内就会出现空洞、坐标就不连续了。该断言在编译期保证：存储块永远不会骑在空洞边界上，每个块要么全在 left 段、要么全在 right 段、要么本身就是 mid 空洞段（不写）。`STORE_BLOCK_N` 作为模板参数传入，所以这个约束是**按具体 kernel 实例逐个校验**的。

宿主侧怎么决定用哪个策略？看 `attention.hpp`：

```cpp
// [attention.hpp:48-L49] 校验 head_splits 与输出形状的自洽性
const auto [left, mid, right] = head_splits;
DG_HOST_ASSERT(n % (left + right) == 0 and n_ == n + n / (left + right) * mid);
```

[attention.hpp:48-49](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/apis/attention.hpp#L48-L49) 校验：GEMM 的 N 必须被 `(left+right)` 整除（head 数为整数），且输出张量的列数 \(n_\) 必须精确等于 \(n + n/(left+right)\cdot mid\)——即「每个 head 插一段 mid」。这与 4.1.2 的推导完全一致。

```cpp
// [attention.hpp:63] 把 (left, mid, right) 拼成 epilogue 类型名字符串
const auto epilogue_type = fmt::format("epilogue::transform::EpilogueHeadSplits<{}, {}, {}>", left, mid, right);
```

[attention.hpp:63](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/apis/attention.hpp#L63) 用 `fmt::format` 把三个整数拼成一段**合法的 C++ 类型名**字符串 `epilogue::transform::EpilogueHeadSplits<128, 64, 128>`。这个字符串就是策略的「身份证」——它随后会被 JIT 代码生成原样塞进设备 kernel 的模板参数列表（见 4.3）。注意：参数被烤进类型名，意味着**不同的 `(left, mid, right)` 组合会编译出不同的 kernel 实例**（这也是为何它进缓存键）。

#### 4.1.4 代码实践

**实践目标**：脱离 GPU，纯 Python 复现 `EpilogueHeadSplits::apply_index_n` 的映射，验证它与参考实现 `apply_skip_head_mid` 在「实数据列」上的位置完全一致。

**操作步骤**（源码阅读型 + 可选运行）：

1. 打开 [`tests/test_attention.py:19-31`](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/tests/test_attention.py#L19-L31)，阅读 `apply_skip_head_mid`：它把紧凑 `[M, N]` 的结果按 head 拆成 `d_left`/`d_right`，中间插入零 `d_mid`，再 `cat` 回去——这就是「带空洞」输出的参考构造。
2. 用 Python 写一个 `apply_index_n(n_idx, left, mid, right)`，直接照抄 C++ 公式 `n_idx + (n_idx + right)//(left + right) * mid`。
3. 取 `head_splits = (128, 64, 128)`，对一组连续紧凑列号 `n_idx ∈ [0, N)`（例如 `N = 256 * num_heads`，`num_heads` 取 2 或 3），逐个调用 `apply_index_n`，得到真实列号集合 `real_cols`。
4. 对比：`apply_skip_head_mid` 产生的输出里，**非零列**的列号集合应精确等于 `real_cols`（因为只有实数据列被 kernel 写入，mid 段保持零）。

**需要观察的现象**：

- `apply_index_n(0,128,64,128) == 0`（head 0 的左半起点不变）；
- `apply_index_n(127,...) == 127`（左半最后一列，仍连续）；
- `apply_index_n(128,...) == 128 + 64 == 192`（head 0 的右半第一列，跳过了 64 列 mid 空洞）；
- `apply_index_n(255,...) == 319`（head 0 右半最后一列）；
- `apply_index_n(256,...) == 320`（head 1 左半起点：256 + 2*64 = 384？请亲手算验证，留意 head 1 的偏移）。

> 提示：按公式 `256 + (256+128)//256 * 64 = 256 + 1*64 = 320`。head 1 左半起点应是 `1*(128+64+128) = 320`，吻合。

**预期结果**：`real_cols` 与 `apply_skip_head_mid` 输出的非零列号集合完全相等，证明「紧凑列号实时重映射」等价于「先算紧凑再插入空洞」。若手算与公式不符，回头检查整数除法的跳变点。

> 待本地验证：若你已按 u1-l2 装好环境且在 SM90/SM100 机器上，可直接运行 `pytest tests/test_attention.py::test_gemm_skip_head_mid -s`，观察 `calc_diff(d, ref_d) < 0.001` 通过；否则上面的纯 Python 推导即足以验证映射正确性。

#### 4.1.5 小练习与答案

**练习 1**：把 head_splits 改成 `(64, 32, 64)`，STORE_BLOCK_N 为 32 时断言是否通过？为 64 呢？

**答案**：left/mid/right = 64/32/64 都能被 32 整除，STORE_BLOCK_N=32 通过；也都能被 64 整除？32 不能被 64 整除（`32 % 64 != 0`），故 STORE_BLOCK_N=64 时 `kMid % STORE_BLOCK_N == 0` 失败，**编译期断言失败**。这正说明存储块宽必须能整除所有三段。

**练习 2**：为什么公式里是 `+kRight` 而不是 `+kLeft`？

**答案**：整数除法的跳变点要落在 head 内「left 段结束、right 段开始」的边界，即紧凑列号 \(h\cdot(L+R)+L\)。`+right` 使除法恰在 \(n \ge h\cdot(L+R)+L\) 时从 \(h\) 跳到 \(h+1\)（见 4.1.2 推导）。若换成 `+left`，跳变点会错位，左半会被错误地多加一个 `mid`。

**练习 3**：若 `mid = 0`，`EpilogueHeadSplits<L,0,R>` 退化成什么？

**答案**：公式变为 `n_idx + (...)*0 = n_idx`，与 `EpilogueIdentity` 完全等价——没有空洞即无需重映射。

### 4.2 C/D store 与 swap-ab：把累加结果写回全局内存

#### 4.2.1 概念说明

`apply_index_n` 只回答「写哪一列」。真正的「怎么写」由两个 store 函数完成。SM100 上累加结果落在 **TMEM** 里，要写回全局显存需要三段搬运：

1. **TMEM → 共享内存**：用 `SM100_TMEM_LOAD_*` 把累加器读出，必要时做 FP32→BF16 的类型转换与打包（`cast_into_bf16_and_pack`），并按 swizzle 规则写进共享内存；
2. **共享内存 → 全局显存**：由一个被选中的线程（`elect_one_sync()`）发起 **TMA store**（带累加时换成 TMA reduce-add），把整块异步写出；
3. **流水线握手**：用 `NamedBarrier` 在 epilogue 线程间同步、用 `tmem_empty_barrier` 通知「TMEM 这一格已腾空」可被下一轮 MMA 复用，用 `tma_store_wait/fence/arrive` 管理异步写出的在途深度（`kNumTMAStoreStages`）。

DeepGEMM 提供两条 store 路径，区别在于输出是否需要转置：

- **`sm100_store_cd`（非转置）**：累加结果在 TMEM 里以「M 行 × N 列」的自然朝向存储，直接按行写出。
- **`sm100_store_cd_swap_ab`（swap-ab 转置）**：当 kernel 用了 `swap_ab` 优化（见 u5-l2/u10-l1，常用于小 M 大 N，把 M、N 角色对调以填满 128 行 TMEM），累加结果在 TMEM 里是「转置朝向」的，store 时要把它**转回去**再写，使全局显存里的 D 仍是正确的 `[M, N]`。

#### 4.2.2 核心流程

非转置路径 `sm100_store_cd` 的流程（按 M 波 × N 块双重循环）：

```text
for w in 0 .. BLOCK_M/STORE_BLOCK_M:        # 遍历 M 方向的若干 wave
  for s in 0 .. BLOCK_N/STORE_BLOCK_N:      # 遍历 N 方向的若干 store 块
    tma_store_wait                          # 等共享内存上一轮腾空
    NamedBarrier.sync                       # epilogue 线程集合点
    m_idx = base_m + w*STORE_BLOCK_M
    n_idx = apply_index_n(base_n + s*STORE_BLOCK_N)   # ← 4.1 的重映射！
    for i in 块内每个 bank group:
        从 TMEM 读 values   (FP32: 4 个; BF16: 8 个并打包)
        按 swizzle 算 smem 偏移，st_shared 写入共享内存
    最后一格: tmem_empty_barrier.arrive      # 通知 TMEM 可复用
    tma_store_fence + NamedBarrier.sync
    elect_one: TMA_STORE_2D/3D(smem, n_idx, m_idx)   # 异步写出
    tma_store_arrive
```

转置路径 `sm100_store_cd_swap_ab` 的关键不同：

- **遍历对象反过来**：外层按 `effective_m / STORE_BLOCK_M` 个 M 块推进（因为 swap_ab 后「N 方向」其实是物理 TMEM 的行方向）；
- **STSM 转置指令**：BF16 路径用 `SM90_U32x4_STSM_T`（带 `_T` 后缀的 store matrix，硬件完成 8×8 转置）把数据转着写进共享内存；
- **断言 `STORE_BLOCK_N == 128`**：转置 epilogue 要求一个完整 warpgroup 读满 TMEM 的 128 行；
- **多个 TMA atom**：内层再按 `STORE_BLOCK_N_ATOM` 切成多个原子，每个原子各发一次 TMA store，每个都单独过一遍 `apply_index_n`。

#### 4.2.3 源码精读

非转置 store 的签名与重映射调用点：

```cpp
// [sm100_store_cd.cuh:13-L28] 非转置存储 epilogue 的完整参数：块大小、swizzle、流水线深度、
// 是否带累加、C/D 数据类型、epilogue 策略类型、共享内存 pattern
template <uint32_t BLOCK_M, uint32_t BLOCK_N,
          uint32_t STORE_BLOCK_M, uint32_t STORE_BLOCK_N,
          uint32_t kSwizzleCDMode, uint32_t kNumTMAStoreStages, uint32_t kNumUMMAStoreThreads,
          GemmType kGemmType, bool kWithAccumulation,
          typename cd_dtype_t, typename epilogue_type_t, typename pattern_cd_t>
CUTLASS_DEVICE void
sm100_store_cd(... const uint32_t& base_m_idx, const uint32_t& base_n_idx, ...);
```

[sm100_store_cd.cuh:13-28](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/epilogue/sm100_store_cd.cuh#L13-L28) 暴露了 epilogue 的全部「配置面」：块粒度（`STORE_BLOCK_M/N`）、swizzle 模式、TMA store 流水线深度（`kNumTMAStoreStages`，默认 2）、是否原子累加（`kWithAccumulation`）、C/D 类型（FP32 或 BF16）、以及最关键的 `epilogue_type_t` 策略类型——它就是 4.1 那个 `EpilogueIdentity`/`EpilogueHeadSplits`。

```cpp
// [sm100_store_cd.cuh:57-L59] 计算本轮 store 块的真实坐标：M 不变，N 经 epilogue 策略重映射
const auto m_idx = base_m_idx + w * STORE_BLOCK_M;
const auto n_idx = epilogue_type_t::apply_index_n<STORE_BLOCK_N>(base_n_idx + s * STORE_BLOCK_N);
```

[sm100_store_cd.cuh:57-59](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/epilogue/sm100_store_cd.cuh#L57-L59) 是 epilogue 策略的**唯一调用点**（非转置路径）。`STORE_BLOCK_N` 作为模板实参传给 `apply_index_n`，供 `EpilogueHeadSplits` 做编译期整除断言。注意 M 维坐标 `m_idx` 不参与重映射——空洞只在 N（列）方向。

TMEM→共享内存的搬运与类型转换（BF16 打包）：

```cpp
// [sm100_store_cd.cuh:93-L107] BF16 路径：从 TMEM 读 8 个值，两两打包成 BF16 再 st_shared
} else {
    DG_STATIC_ASSERT(kNumElemsPerBankGroup == 8 and ...);
    cute::SM100_TMEM_LOAD_32dp32b8x::copy(tmem_addr, values[0..7]);
    cutlass::arch::fence_view_async_tmem_load();
    ptx::st_shared(smem_ptr,
        math::cast_into_bf16_and_pack(values[0], values[1]),
        math::cast_into_bf16_and_pack(values[2], values[3]),
        math::cast_into_bf16_and_pack(values[4], values[5]),
        math::cast_into_bf16_and_pack(values[6], values[7]));
}
```

[sm100_store_cd.cuh:93-107](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/epilogue/sm100_store_cd.cuh#L93-L107) 展示 BF16 输出路径：`SM100_TMEM_LOAD_32dp32b8x` 一次读 8 个 FP32 累加值，`cast_into_bf16_and_pack` 把两个 FP32 转成两个 BF16 并打包进一个 32 位寄存器，最后 `st_shared` 写入 swizzle 后的共享内存地址。FP32 路径（`sm100_store_cd.cuh:86-92`）则直接读 4 个、原样写。共享内存里的 swizzle 由 `row`/`col` 的 XOR 计算（`sm100_store_cd.cuh:65-82`）实现，满足 TMA store 的布局契约。

异步 TMA 写回全局显存（带累加时变 reduce-add）：

```cpp
// [sm100_store_cd.cuh:117-L131] 同步后由单个线程发起 TMA store / reduce-add，2D 或 3D
cute::tma_store_fence();
cutlass::arch::NamedBarrier::sync(kNumUMMAStoreThreads, 0);
if (epilogue_warp_idx == 0 and cute::elect_one_sync()) {
    if constexpr (kGemmType == GemmType::Batched) {
        using cute_tma_t = cute::conditional_t<kWithAccumulation,
                                cute::SM90_TMA_REDUCE_ADD_3D, cute::SM90_TMA_STORE_3D>;
        cute_tma_t::copy(&tensor_map_cd, smem_base_ptr, n_idx, m_idx, batch_idx);
    } else {
        using cute_tma_t = cute::conditional_t<kWithAccumulation,
                                cute::SM90_TMA_REDUCE_ADD_2D, cute::SM90_TMA_STORE_2D>;
        cute_tma_t::copy(&tensor_map_cd, smem_base_ptr, n_idx, m_idx);
    }
    cute::tma_store_arrive();
}
```

[sm100_store_cd.cuh:117-131](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/epilogue/sm100_store_cd.cuh#L117-L131) 是真正的写回：`elect_one_sync()` 选出一个线程发起 TMA，坐标就是前面算好的 `(n_idx, m_idx)`（注意 CuTe TMA 的参数顺序是 `(ptr, coord_n, coord_m, [batch])`，N 在前）。`kWithAccumulation` 为真（即 `D = C + A@B` 且 C≠D 同址）时用 `TMA_REDUCE_ADD`（原子累加回原地址），否则用普通 `TMA_STORE`。`Batched`（分组/批量）多一个 `batch_idx` 维度走 3D 描述符。

转置路径 `sm100_store_cd_swap_ab` 的两个标志点：

```cpp
// [sm100_store_cd_swap_ab.cuh:30-L32] 转置 epilogue 要求 STORE_BLOCK_N 恰为 128（满 TMEM 行）
DG_STATIC_ASSERT(STORE_BLOCK_N == 128, "STORE_BLOCK_N must be 128 to match TMEM rows");
```

[sm100_store_cd_swap_ab.cuh:30-32](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/epilogue/sm100_store_cd_swap_ab.cuh#L30-L32) 锁死 `STORE_BLOCK_N == 128`：swap_ab 时一个完整 warpgroup 必须读满 TMEM 的 128 行才能正确转置。

```cpp
// [sm100_store_cd_swap_ab.cuh:121-L136] 转置路径：每个 N 原子各发一次 TMA，坐标都过 apply_index_n
for (uint32_t i = 0; i < STORE_BLOCK_N / STORE_BLOCK_N_ATOM; ++ i) {
    auto smem_ptr = smem_cd[tma_stage_idx] + i * STORE_BLOCK_M * STORE_BLOCK_N_ATOM;
    uint32_t m_idx = base_m_idx + s * STORE_BLOCK_M;
    uint32_t n_idx = epilogue_type_t::apply_index_n<STORE_BLOCK_N_ATOM>(base_n_idx + i * STORE_BLOCK_N_ATOM);
    if constexpr (kGemmType == GemmType::Batched) { ...TMA_REDUCE_ADD_3D / TMA_STORE_3D... }
    else { ...TMA_REDUCE_ADD_2D / TMA_STORE_2D with (n_idx, m_idx)... }
}
```

[sm100_store_cd_swap_ab.cuh:121-136](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/epilogue/sm100_store_cd_swap_ab.cuh#L121-L136) 表明转置路径把一个 128 宽的块再切成若干 `STORE_BLOCK_N_ATOM` 原子，**每个原子都单独调用一次 `apply_index_n`**——所以 HeadSplits 的整除断言在这里针对的是更小的 `STORE_BLOCK_N_ATOM = kSwizzleCDMode / sizeof(cd_dtype_t)`，约束更细。

设备 kernel 里按 `kSwapAB` 选择两条路径：

```cpp
// [sm100_fp8_fp4_gemm_1d1d.cuh:495-L518] 收尾阶段：swap_ab 走转置 store，否则走非转置 store
if constexpr (kSwapAB) {
    epilogue::sm100_store_cd_swap_ab<BLOCK_M, BLOCK_N, STORE_BLOCK_M, STORE_BLOCK_N,
        kSwizzleCDMode, kNumTMAStoreStages, kNumUMMAStoreThreads,
        kGemmType, kWithAccumulation, cd_dtype_t, epilogue_type_t>(...);
} else {
    epilogue::sm100_store_cd<BLOCK_M, BLOCK_N, STORE_BLOCK_M, STORE_BLOCK_N,
        kSwizzleCDMode, kNumTMAStoreStages, kNumUMMAStoreThreads,
        kGemmType, kWithAccumulation, cd_dtype_t, epilogue_type_t>(...);
}
```

[sm100_fp8_fp4_gemm_1d1d.cuh:495-518](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/impls/sm100_fp8_fp4_gemm_1d1d.cuh#L495-L518) 是 kernel 内的派发：`if constexpr (kSwapAB)` 在编译期二选一，`epilogue_type_t` 作为最后一个模板参数透传给 store 函数。注意 `STORE_BLOCK_M/N` 本身也随 `kSwapAB` 变化（见下）。

`STORE_BLOCK_M/N` 怎么定？看 kernel 头部的常量推导：

```cpp
// [sm100_fp8_fp4_gemm_1d1d.cuh:76-L80] 存储块粒度随 swap_ab 切换：swap 时整块 N 一次存，非 swap 时整块 M
constexpr uint32_t STORE_BLOCK_M =        kSwapAB ? 16      : cute::min<uint32_t>(BLOCK_M, LAYOUT_AD_M);
constexpr uint32_t STORE_BLOCK_N =        kSwapAB ? BLOCK_N : kSwizzleCDMode / sizeof(cd_dtype_t);
constexpr uint32_t kNumUMMAStoreThreads = kSwapAB ? kNumEpilogueThreads: STORE_BLOCK_M;
```

[sm100_fp8_fp4_gemm_1d1d.cuh:76-80](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/impls/sm100_fp8_fp4_gemm_1d1d.cuh#L76-L80) 给出非转置路径的 `STORE_BLOCK_N = kSwizzleCDMode / sizeof(cd_dtype_t)`（如 128B swizzle + BF16 ⇒ 64 列；+ FP32 ⇒ 32 列）。这正是 4.1.3 里 `EpilogueHeadSplits` 断言所依据的 `STORE_BLOCK_N`，所以 left/mid/right 必须能被 64（BF16）或 32（FP32）整除——这也解释了为何测试用例的 `(128, 64, 128)` 同时满足两种 dtype。

最后看 SM90 的对应调用，证明这条「N 维重映射」是跨架构通用的：

```cpp
// [sm90_fp8_gemm_1d2d.cuh:430-L434] SM90 1D2D 的 TMA store 同样用 apply_index_n 重映射 N 坐标
if (threadIdx.x < BLOCK_N / TMA_D_BLOCK_N) {
    auto in_block_n_offset = threadIdx.x * TMA_D_BLOCK_N;
    auto smem_ptr = smem_d + in_block_n_offset * BLOCK_M;
    auto n_idx = epilogue_type_t::apply_index_n<TMA_D_BLOCK_N>(n_block_idx * BLOCK_N + in_block_n_offset);
    auto m_idx = scheduler.get_global_idx<...>(shape_m, BLOCK_M, m_block_idx);
```

[sm90_fp8_gemm_1d2d.cuh:430-434](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/impls/sm90_fp8_gemm_1d2d.cuh#L430-L434) 显示 SM90 的 WGMMA 路径里，store 同样调用 `epilogue_type_t::apply_index_n<TMA_D_BLOCK_N>(...)`，只是块宽常量名换成 `TMA_D_BLOCK_N`。SM90 没有 TMEM/swap_ab，故只有这一条非转置路径，但重映射机制完全一致。

#### 4.2.4 代码实践

**实践目标**：用 `DG_PRINT_CONFIGS=1` / `DG_JIT_DEBUG=1` 观察 SM100 kernel 选出的 `STORE_BLOCK_N`，并验证它与 `(128, 64, 128)` 的整除关系。

**操作步骤**：

1. 确认已在 SM100 机器上构建好 DeepGEMM（见 u1-l2）。
2. 写一个最小脚本，构造 FP8 的 `a [M,K]`、`b [N,K]` 与 BF16 输出 `d [M, N_out]`，其中 `N_out = N + N/256 * 64`，调用 `deep_gemm.fp8_gemm_nt_skip_head_mid(a, b, d, (128, 64, 128))`。
3. 设环境变量 `DG_JIT_DEBUG=1` 运行，在 stderr 中找到 SM100 1D1D kernel 的配置打印，记录 `swizzle_cd_mode` 与 `block_n`。
4. 推算 `STORE_BLOCK_N = swizzle_cd_mode / sizeof(bfloat16) = swizzle_cd_mode / 2`。
5. 验证 `128 % STORE_BLOCK_N == 0`、`64 % STORE_BLOCK_N == 0`。

**需要观察的现象**：若 `swizzle_cd_mode = 128`，则 `STORE_BLOCK_N = 64`，三段 128/64/128 都能被 64 整除，编译期断言通过、kernel 正常运行；若启发式选出的 swizzle 使 `STORE_BLOCK_N` 不能整除 mid（理论不会发生，但可人为构造），则会在 JIT 编译阶段触发 `DG_STATIC_ASSERT` 报错。

**预期结果**：kernel 成功编译并产出与 `apply_skip_head_mid` 参考一致的输出（`calc_diff < 0.001`），且日志显示的 `STORE_BLOCK_N` 与手算一致。

> 待本地验证：本实践依赖 SM100 GPU 与可运行环境；若不可用，可退化为源码阅读——直接读 `sm100_store_cd.cuh:57-59` 与 `transform.cuh:18-19`，论证「`STORE_BLOCK_N` 必须整除 left/mid/right」这一不变量。

#### 4.2.5 小练习与答案

**练习 1**：非转置路径里，`m_idx` 为什么不需要过 `apply_index_n`？

**答案**：空洞（mid 段）只插在 N（列）方向、按 head 切分；M（行）方向所有行都是实数据、无跳过，故 M 坐标直接线性推进即可。

**练习 2**：`kWithAccumulation` 为真时，store 用 `TMA_REDUCE_ADD` 而非 `TMA_STORE`，为什么？

**答案**：`D = C + A@B` 且 C 与 D 不同址时，多个线程块/多次写入可能落到同一输出地址（如分组或 split-K），需用原子 reduce-add 把部分和累加进去；纯覆盖写（C/D 同址）则用普通 store。这与 u9-l3 HyperConnection 的 split-K 部分和、u7 的 psum 同源。

**练习 3**：`sm100_store_cd_swap_ab` 为什么要求 `STORE_BLOCK_N == 128`？

**答案**：swap_ab 把累加结果在 TMEM 里以转置朝向存放，转置回写需用一个完整 warpgroup（128 线程）读满 TMEM 的 128 行才能用 STSM 转置指令正确还原布局，少于 128 行无法对齐。

### 4.3 epilogue 作为模板策略如何被注入到 GEMM kernel

#### 4.3.1 概念说明

前两节看到了「策略做什么」（4.1）和「策略在哪用」（4.2）。本节回答：**这个策略是怎么从用户的一次 Python 调用，跑到 GPU 上的设备代码里的？** 答案是经典的 **policy/strategy 模板策略模式 + JIT 代码生成**：

- 设备 kernel 把 epilogue 写成一个**类型模板参数 `epilogue_type_t`**，而不是一个运行时 if/else 分支；
- 宿主不传「策略对象」，而是传一个**类型名字符串**；
- JIT 代码生成把这个字符串原样填进 `.cu` 源码的模板实参列表，编译器据此实例化出对应 kernel；
- 由于是编译期类型，`apply_index_n` 会被内联，普通 GEMM 的 `EpilogueIdentity` 编译后什么都不剩，**真正零开销**。

这种「宿主决定类型名、设备用类型做策略」的设计，让 DeepGEMM 能在不增加运行时分支的前提下，灵活支持「普通 GEMM」和「带 head 空洞的 attention GEMM」两种截然不同的写回语义。

#### 4.3.2 核心流程

epilogue 类型从 Python 到 GPU 的完整数据流：

```text
Python: deep_gemm.fp8_gemm_nt_skip_head_mid(..., head_splits=(128,64,128))
   │
   ▼  pybind11
C++ API: attention::fp8_gemm_nt_skip_head_mid
   │  fmt::format 拼出类型名 "epilogue::transform::EpilogueHeadSplits<128,64,128>"
   ▼
Host Runtime: sm100_fp8_fp4_gemm_1d1d(..., epilogue_type=该字符串)
   │  存入 args.epilogue_type (optional<string>)
   ▼
generate_impl: get_default_epilogue_type(args.epilogue_type)  // nullopt ⇒ "EpilogueIdentity"
   │  作为最后一个 fmt 实参填进设备模板实参列表
   ▼
JIT 编译: sm100_fp8_fp4_gemm_1d1d_impl<..., epilogue_type_t = EpilogueHeadSplits<128,64,128>>
   │  取函数地址 ⇒ 强制模板实例化
   ▼
设备 kernel: 收尾阶段调用 sm100_store_cd<..., epilogue_type_t>(...)
   │  apply_index_n 被内联进 store 循环
   ▼
GPU: 结果按重映射后的坐标写回 D
```

未指定 epilogue 时（绝大多数普通 GEMM），`epilogue_type` 为 `std::nullopt`，`get_default_epilogue_type` 回退到 `EpilogueIdentity`，于是 `apply_index_n` 是恒等函数，编译消除。

#### 4.3.3 源码精读

宿主 API 构造类型名并派发：

```cpp
// [attention.hpp:61-L73] 按架构 + SF dtype 派发，把 epilogue_type 字符串透传给底层 Runtime
const auto arch_major = device_runtime->get_arch_major();
const auto epilogue_type = fmt::format("epilogue::transform::EpilogueHeadSplits<{}, {}, {}>", left, mid, right);
if (arch_major == 9 and sfa.scalar_type() == torch::kFloat and std::get<1>(recipe.value()) != 1) {
    sm90_fp8_gemm_1d2d(..., compiled_dims, epilogue_type);
} else if (arch_major == 10 and sfa.scalar_type() == torch::kInt) {
    sm100_fp8_fp4_gemm_1d1d(..., compiled_dims, epilogue_type);
} else {
    DG_HOST_UNREACHABLE("Unsupported architecture or scaling factor types");
}
```

[attention.hpp:61-73](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/apis/attention.hpp#L61-L73) 显示：epilogue 字符串对所有支持的架构路径（SM90 1D2D / SM100 1D1D）都透传同一个 `epilogue_type`，因此 N 维重映射语义跨架构一致。这与 u2-l3 的「架构 + SF dtype 双条件派发」模式相同。

Runtime 类把字符串存进 args，并在代码生成时取默认值：

```cpp
// [sm100_fp8_fp4_gemm_1d1d.hpp:24-L25] epilogue_type 作为 optional<string> 挂在 args 上（标注 TODO 待并入 descriptor）
const std::optional<std::string> epilogue_type;
```

[sm100_fp8_fp4_gemm_1d1d.hpp:24-25](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/impls/sm100_fp8_fp4_gemm_1d1d.hpp#L24-L25) 表明 epilogue 类型目前是一个「挂在 args 上的可选字符串」，作者注释 `TODO: move into descriptor` 说明它尚未正式并入 `GemmDesc`，而是作为旁路参数存在。

```cpp
// [sm100_fp8_fp4_gemm_1d1d.hpp:78-L80] 代码生成：epilogue 类型作为最后一个模板实参填进设备 kernel
        to_string(args.gemm_desc.a_dtype), to_string(args.gemm_desc.b_dtype), to_string(args.gemm_desc.cd_dtype),
        get_default_epilogue_type(args.epilogue_type));
}
```

[sm100_fp8_fp4_gemm_1d1d.hpp:78-80](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/impls/sm100_fp8_fp4_gemm_1d1d.hpp#L78-L80) 是注入点：`get_default_epilogue_type(args.epilogue_type)` 的返回值作为 `fmt::format` 的最后一个实参，填进设备 kernel 模板实参列表的最末位（即 `epilogue_type_t`）。这正是 u3-l2「模板实例化代码生成」在 epilogue 上的具体应用。

默认值回退逻辑：

```cpp
// [epilogue.hpp:8-L10] 未指定 epilogue 时回退到恒等策略 EpilogueIdentity
static std::string get_default_epilogue_type(const std::optional<std::string>& epilogue_type) {
    return epilogue_type.value_or("epilogue::transform::EpilogueIdentity");
}
```

[epilogue.hpp:8-10](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/impls/epilogue.hpp#L8-L10) 是「普通 GEMM 用恒等策略」的来源：`value_or` 在 `nullopt` 时返回 `EpilogueIdentity` 的全限定类型名。于是普通 `fp8_gemm_nt` 等接口根本不传 epilogue，自动得到恒等策略。

设备 kernel 接收该类型参数：

```cpp
// [sm100_fp8_fp4_gemm_1d1d.cuh:29-L32] epilogue_type_t 是设备 kernel 的最后一个模板参数
          GemmType kGemmType, bool kWithAccumulation,
          typename a_dtype_t, typename b_dtype_t, typename cd_dtype_t,
          typename epilogue_type_t>
CUTLASS_GLOBAL void __launch_bounds__(kNumNonEpilogueThreads + kNumEpilogueThreads, 1)
sm100_fp8_fp4_gemm_1d1d_impl(...);
```

[sm100_fp8_fp4_gemm_1d1d.cuh:29-32](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/impls/sm100_fp8_fp4_gemm_1d1d.cuh#L29-L32) 确认 `epilogue_type_t` 是 kernel 的末位模板参数。它由 JIT 生成的源码用那个字符串实参实例化（如 `...EpilogueHeadSplits<128, 64, 128>`），随后在收尾阶段透传给 `sm100_store_cd` / `sm100_store_cd_swap_ab`（见 4.2.3 的 `sm100_fp8_fp4_gemm_1d1d.cuh:495-518`）。

#### 4.3.4 代码实践

**实践目标**：追踪 `epilogue_type` 字符串在缓存键中的作用，理解「不同 head_splits 编译出不同 kernel」。

**操作步骤**：

1. 设置 `DG_JIT_DEBUG=1` 与缓存目录可见（默认 `$HOME/.deep_gemm`）。
2. 先运行一次普通 `deep_gemm.fp8_gemm_nt(...)`，记下生成的 kernel 源码（`DG_JIT_DEBUG` 会打印或可从缓存目录 `kernel.*` 下找到 `.cu` 文件），确认末位模板实参是 `epilogue::transform::EpilogueIdentity`。
3. 再运行一次 `deep_gemm.fp8_gemm_nt_skip_head_mid(..., (128, 64, 128))`，对比新生成的 `.cu`，确认末位实参变成了 `epilogue::transform::EpilogueHeadSplits<128, 64, 128>`，且 `__instantiate_kernel` 取地址的目标函数名（mangled name）不同。
4. 检查缓存目录：两次调用产生**两个不同的 `kernel.<digest>` 目录**——因为 epilogue 类型名进了生成源码 `code`，而 `code` 是缓存键四元组 `(name, signature, flags, code)` 的一员（见 u3-l3），故类型不同 ⇒ digest 不同 ⇒ 独立编译/缓存。

**需要观察的现象**：

- 普通 GEMM 的 `.cu` 末位实参为 `EpilogueIdentity`；
- skip_head_mid 的 `.cu` 末位实参为 `EpilogueHeadSplits<128, 64, 128>`；
- 两者缓存目录名（digest）不同。

**预期结果**：两份源码除末位模板实参外几乎一致（共享同一套计算主体），印证 epilogue 是「正交的策略插拔点」——改 epilogue 不动计算逻辑，只改写回坐标。这也说明 DeepGEMM 用模板策略 + JIT 复用了一份 kernel 主体。

> 待本地验证：若无法运行，可直接在缓存目录 `find $HOME/.deep_gemm -name '*.cu'` 后人工比对；或在源码层阅读 `sm100_fp8_fp4_gemm_1d1d.hpp:55-80` 的 `fmt::format` 模板，确认 `get_default_epilogue_type(...)` 位于实参列表末尾。

#### 4.3.5 小练习与答案

**练习 1**：如果把 `epilogue_type` 设计成运行时 `int` 枚举（0=Identity, 1=HeadSplits）而非编译期类型，会有什么代价？

**答案**：每次 store 都要多一个运行时分支判断 epilogue 种类，且 `EpilogueHeadSplits` 的 `kLeft/kMid/kRight` 无法作为编译期常量参与整除断言与循环展开，性能与可读性都下降。编译期类型让编译器把策略内联消除，是零开销的关键。

**练习 2**：为何 `get_default_epilogue_type` 用 `value_or("...EpilogueIdentity")` 而不是直接要求调用方必传？

**答案**：绝大多数 GEMM（`fp8_gemm_nt`、分组、einsum 等）根本不需要 N 维重映射，强制传 epilogue 会污染所有调用点。用 `optional` + 默认恒等策略，让「普通 GEMM」的代码路径完全无感，只有 attention 的 `skip_head_mid` 才显式指定。

**练习 3**：epilogue 类型名进了 JIT 缓存键，这带来什么好处和副作用？

**答案**：好处是不同 head_splits 自动得到独立编译并缓存，互不污染、且类型名变化能正确触发重编译（与 u3-l3 的 include hash 机制一致）。副作用是 `(left, mid, right)` 组合越多，缓存的 kernel 实例越多——但 attention 场景下 head_splits 通常固定，故实际组合很少。

## 5. 综合实践

把本讲三块知识串起来：实现一个「等价于 `fp8_gemm_nt_skip_head_mid` 写回语义」的纯宿主侧参考，并用它解释设备侧重映射的每个环节。

1. **构造问题**：取 `num_heads = 2`，`head_splits = (128, 64, 128)`，则紧凑 GEMM 的 \(N = 2 \times 256 = 512\)，带空洞输出列数 \(N_{\text{out}} = 512 + 512/256 \times 64 = 640\)。
2. **实现两个函数**（参考 `tests/test_attention.py:19-31` 的 `apply_skip_head_mid`）：
   - `apply_index_n(n_idx, L, M, R)`：照抄 `transform.cuh:20` 的公式；
   - `apply_skip_head_mid(d, head_splits)`：按 head 插零段。
3. **验证映射自洽**：对全部 `n_idx ∈ [0, 512)`，用 `apply_index_n` 得到真实列号集合 `S`；再对一份随机的紧凑 `d [M, 512]` 跑 `apply_skip_head_mid` 得到 `D [M, 640]`，断言 `D` 中所有非零列的列号集合恰为 `S`，且每列数值等于紧凑 `d` 对应列。
4. **画出设备侧对应关系**：在一张图上标注——
   - `transform.cuh:20` 公式 ↔ 你实现的 `apply_index_n`；
   - `sm100_store_cd.cuh:57-59` 调用点 ↔ 每个 `STORE_BLOCK_N` 块的起点重映射；
   - `attention.hpp:63` 类型名 ↔ 你传给 `apply_index_n` 的 `(L,M,R)` 参数；
   - `epilogue.hpp:8-10` 默认值 ↔ 普通 GEMM 不重映射。
5. **（可选，需 SM100 环境）**运行 `tests/test_attention.py::test_gemm_skip_head_mid`，确认设备侧结果与你的宿主参考一致（`calc_diff < 0.001`）。

**交付物**：一份 Python 脚本 + 一张映射关系图，能向他人讲清「一次 `fp8_gemm_nt_skip_head_mid` 调用，结果是如何按 head 重映射写到带空洞的 D 里的」。

## 6. 本讲小结

- **EpilogueHeadSplits** 用一条无分支整数公式 `n_idx + (n_idx + kRight)/(kLeft + kRight) * kMid`，把紧凑列号实时映射到「每个 head 插 mid 空洞」的真实输出列号；`+kRight` 的作用是让整数除法的跳变点对齐 head 内的 left/right 边界。
- 重映射只发生在 **N（列）方向**，M 方向不变；且要求 `kLeft/kMid/kRight` 都能被存储块宽 `STORE_BLOCK_N` 整除，以保证一个存储块不骑在空洞边界上（编译期断言）。
- **C/D store** 把 TMEM 累加结果经 swizzle 共享内存搬出，再由 TMA 异步写回；带累加时用 `TMA_REDUCE_ADD`，覆盖写时用 `TMA_STORE`；SM100 有非转置 `sm100_store_cd` 与转置 `sm100_store_cd_swap_ab`（后者要求 `STORE_BLOCK_N==128` 并用 STSM 转置指令）两条路径。
- epilogue 是一个**编译期模板策略**：宿主只决定一个类型名字符串，JIT 代码生成把它烤成设备 kernel 的末位模板参数 `epilogue_type_t`，`apply_index_n` 被内联；普通 GEMM 用 `EpilogueIdentity`（`get_default_epilogue_type` 回退）零开销。
- epilogue 类型名进入 JIT 生成的源码 `code`，因而进入缓存键 `(name, signature, flags, code)`——不同 head_splits 编译/缓存为独立 kernel 实例，互不污染。
- 这条「N 维重映射」机制跨架构通用：SM90 `sm90_fp8_gemm_1d2d.cuh:433` 与 SM100 `sm100_store_cd.cuh:59` 都调用同一个 `apply_index_n`。

## 7. 下一步学习建议

- **u9-l3（HyperConnection 与 Einsum）**：那里用到了 `kWithAccumulation` 的 `TMA_REDUCE_ADD` 与 split-K 部分和归约，是本讲 store epilogue「带累加」语义的进阶应用，可对照阅读 `sm100_store_cd.cuh:121-131`。
- **u10-l1（FP4 GEMM 与 FP8xFP4 路径）**：补全 SM100 1D1D kernel 的整体结构（TMEM、2-CTA cluster、UTCCP），本讲的 store epilogue 是其中的收尾一环。
- **u6-l2（MMA 抽象：WGMMA vs UMMA）**：理解累加结果为何落在 TMEM、`SM100_TMEM_LOAD_*` 为何这样用，需要先掌握 UMMA 的 TMEM 累加器模型。
- **延伸阅读**：自行搜索 CUTLASS 3.x 的「Epilogue Fusion」与 CuTe 的 `SM90_TMA_STORE`/`SM90_TMA_REDUCE_ADD` 语义，对比 DeepGEMM 这种「纯坐标重映射 epilogue」与 CUTLASS「数值融合 epilogue」的设计取舍。
