# CuTe 算法：copy 与 gemm

## 1. 本讲目标

前两讲（u2-l1、u2-l2）我们依次建立了 CuTe 的两块基石：**Layout = (Shape, Stride)** 这个「坐标→下标」的纯函数，以及 **Tensor = (Engine, Layout)** 这个「数据 + 布局」的组合。到目前为止，我们**还只是在张量上读写单个元素**，没有真正在张量之间搬运数据，更没有做任何矩阵运算。

本讲要让 CuTe **动起来**——学完本讲你应该能够：

1. 说出 `cute::copy` 家族（`copy` / `copy_if` / `copy_aligned`）的设计：为什么同一个函数名能既做「逐元素朴素拷贝」，又能在 Hopper 上自动编译成 `cp.async` / TMA 这种硬件指令。
2. 读懂 `cute::gemm` 的「**三元组参数模型**」`D = A * B + C`，以及它如何根据**每个张量的维数（rank）和内存空间**在 5 种分发重载里自动选一个，连遍历顺序（serpentine 蛇形遍历）都替你优化好。
3. 用 `cute::fill` / `cute::clear` 这类辅助算法初始化张量，并理解它们背后 `prefer` 的「优先匹配更优实现」技巧。
4. 用一句话讲清「**算法与张量解耦**」的威力：同一份 `copy` / `gemm` 源码，能不加修改地跑通 gmem→smem、smem→rmem、rmem→rmem 各种组合——这正是前两讲把 Layout、Engine、内存空间标签都做对的好处兑现。

---

## 2. 前置知识

进入本讲前，请确认你已经掌握 u2-l1 与 u2-l2 的关键结论：

- **Layout 是纯函数**：`layout(coord) → offset`，本身不含数据（u2-l1）。
- **Tensor = (Engine, Layout)**：Engine 给 `begin()` 迭代器（数据在哪），Layout 给坐标映射（怎么取）；访问元素 = `data()[layout(coord)]`（u2-l2）。
- **内存空间是编译期标签**：`gmem_ptr` / `smem_ptr` / `rmem_ptr` / `tmem_ptr` 是零开销包装，**未加标签的普通指针默认被当成 rmem**（「非 gmem 且非 smem」的排除式判定）。这一条在本讲至关重要——`copy` 和 `gemm` 的很多重载就是靠 `is_gmem` / `is_smem` / `is_rmem` 来决定编译成哪条硬件指令的（u2-l2）。
- **`operator()` 与切片**：坐标里出现下划线 `_` 时做零拷贝切片（降维），否则取元素（u2-l2）。

此外会用到 u1-l4 的 `half_t` 等数值类型概念。一个热身问题：既然 `Tensor` 已经能逐元素读写了，那「把 gmem 里一段数据搬到 smem」「把 smem 里的两个小矩阵乘起来累加到寄存器」这两件事，难道还要我们手写双层 `for` 循环吗？答案是：**不用**。CuTe 把它们抽象成了 `copy` 和 `gemm` 两个泛型算法，你只要把「源张量」「目标张量」喂给它，剩下的（向量化、对齐、硬件指令选择、遍历优化）它替你办。本讲就来拆开这两个算法看个究竟。

---

## 3. 本讲源码地图

本讲的核心文件都在 `include/cute/algorithm/` 下，它们是 CuTe 的「标准算法库」：

| 文件 | 作用 |
| --- | --- |
| `include/cute/algorithm/copy.hpp` | **本讲主战场之一**。定义 `copy` / `copy_if` / `copy_aligned` 的全部重载：朴素谓词拷贝、`Copy_Atom` 指令拷贝、`AutoCopyAsync`（自动选 `cp.async`）、`AutoVectorizingCopy`（自动向量化 + 重投类型）、SM90 TMA bulk copy、以及把 `TiledCopy` 降级成 `Copy_Atom` 的桥子。 |
| `include/cute/algorithm/gemm.hpp` | **本讲另一主战场**。定义 `gemm` 的「三元组模型」`D=A*B+C` 及其 5 种 rank 分发重载，含蛇形遍历（serpentine）寄存器复用优化、默认 MMA（`UniversalFMA`）、以及 smem→rmem 的「先 copy 再 gemm」变体。 |
| `include/cute/algorithm/clear.hpp` | `clear(tensor)`：把张量清零，内部调用 `fill(tensor, T{})`。本讲辅助算法。 |
| `include/cute/algorithm/fill.hpp` | `fill(tensor, value)`：把张量每个元素赋为 `value`，用 `prefer<1>/prefer<0>` 优先匹配「整段 memset 式」的更优实现。 |
| `include/cute/arch/mma.hpp` | `UniversalFMA`：默认 MMA 原子，软件 FMA（`d = a*b + c`），host/device 通用，是 `gemm` 不传 MMA 时的兜底计算单元。 |

> 旁支参考：`algorithm/` 目录里还有 `axpby.hpp`（`Y = α·X + β·Y`）、`cooperative_copy.hpp` / `cooperative_gemm.hpp`（多线程协作版）、`tensor_algorithms.hpp`、`tensor_reduce.hpp` 等。它们与本讲三个算法同属一套设计哲学，学完本讲你就能举一反三去读。

> 小贴士：和 tensor 一样，CuTe 源码里 `tensor.hpp` 这个「胖入口」会在底部把这些算法头一并 `#include` 进来。用户代码通常只需 `#include <cute/tensor.hpp>` 即可拿到 `copy`/`gemm`/`fill`。

---

## 4. 核心概念与源码讲解

本讲拆成 4 个最小模块：**① `cute::copy` 与谓词拷贝**、**② `cute::gemm` 乘加模型**、**③ fill/clear 等辅助算法**、**④ 算法与张量解耦的好处**。

### 4.1 cute::copy 与谓词拷贝

#### 4.1.1 概念说明

「拷贝」听起来最朴素不过：把源张量 `src` 的每个元素写到目标张量 `dst`。但 GPU 上的拷贝水很深——同一个逻辑动作，在「gmem→smem」时最好用 Ampere 的 `cp.async` 异步指令、在「gmem↔smem 大块」时最好用 Hopper 的 TMA、在「smem→rmem」时则是 `ldmatrix`……如果每种都让用户手写，代码会膨胀成噩梦。

CuTe 的解法是：**用一个统一的函数名 `copy(src, dst)`，靠「重载分发」自动挑指令**。分发依据有两个：

1. **第一个参数（拷贝策略 / policy）**：可以是一个 `Copy_Atom`（你显式指定的硬件指令封装）、`AutoCopyAsync`（让库自动挑异步拷贝指令）、`AutoVectorizingCopyWithAssumedAlignment<N>`（自动向量化）、`TiledCopy`（多线程划分后的拷贝），或者**干脆省略**（走最通用的自动向量化路径）。
2. **两个张量的 Engine 类型与 Layout**：`is_gmem` / `is_smem` 等编译期判定，决定能否用某条硬件指令；Layout 是否静态、是否对齐，决定能否向量化。

此外还有一个带「谓词（predicate）」的版本 `copy_if(pred, src, dst)`：只有 `pred(i)` 为真的元素才拷贝。这在处理**边界 tile**（矩阵尺寸不是 tile 整数倍时，边缘有一圈越界元素）时不可或缺——用一个布尔张量当掩码，越界处置假，就能安全地复用同一份 tile 代码。

#### 4.1.2 核心流程

最朴素的谓词拷贝逻辑可以这样概括（伪代码）：

```
copy_if(pred, src, dst):
    对 dst 的每个线性下标 i (0 .. size(dst)-1):
        若 pred(i) 为真:
            dst(i) = static_cast<DstType>(static_cast<SrcType>(src(i)))
```

注意它做了**两次类型转换**：先把 `src(i)` 转成源类型 `SrcType`，再转成目标类型 `DstType`。这看似多余，其实是给「子字节类型 / 代理引用」留的接口——`src(i)` 返回的可能是一个 `SubbyteReference` 而非裸值，先 `static_cast<SrcType>` 把它「物化」成具体数值，再转成目标类型写入。

而无策略的 `copy(src, dst)` 则会根据 Layout 是否静态、是否对齐，自动套上一层 `AutoVectorizingCopyWithAssumedAlignment`：先算出 src/dst 的「最大公共向量元素数」，若能把多个元素重投（recast）成一个更宽的整数类型（如 4 个 `half_t` 重投成一个 `uint64_t`），就走宽向量拷贝，否则退化为逐元素。这就是 CuTe 「**写一遍、自动向量化**」的关键。

当策略是 `AutoCopyAsync` 时，分发会进一步看内存空间：若 `is_gmem<Src> && is_smem<Dst>` 且元素大小是 4/8/16 字节，就选用 SM80 的 `cp.async` 指令（`SM80_CP_ASYNC_CACHEALWAYS` / `CACHCGLOBAL`），否则退化为通用的 `UniversalCopy`。这正是「同一个 `copy`，在 Hopper/Ampere 上自动变成 `cp.async`」的源头。

#### 4.1.3 源码精读

**最朴素的谓词拷贝**——一切 `copy` 的逻辑终点，逐元素、带谓词、双转型：

[include/cute/algorithm/copy.hpp:44-62](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cute/algorithm/copy.hpp#L44-L62) —— 这是 `copy_if(PrdTensor, src, dst)` 的「裸」实现：`CUTE_UNROLL` 循环遍历 `size(dst)` 个元素，`pred(i)` 为真才写，写时做 `DstType(SrcType(src(i)))` 双转型。无策略的 `copy` 最终都会被引到这里（或它的向量化变体）。

**无策略 `copy` 的对齐分发**——省略策略时怎么挑实现：

[include/cute/algorithm/copy.hpp:308-326](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cute/algorithm/copy.hpp#L308-L326) —— `copy(src, dst)` 根据 Layout 是否静态分三条路：两个 Layout 都静态（如寄存器片段）→ 假定 128b 对齐；只有 shape 静态 → 假定 8b 对齐但可过滤；都动态 → 不假定对齐。分别套不同的 `AutoVectorizingCopyWithAssumedAlignment<N>`。

**自动向量化**——把多个元素拼成宽整数一次拷：

[include/cute/algorithm/copy.hpp:242-274](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cute/algorithm/copy.hpp#L242-L274) —— 先用 `max_common_vector(src,dst)` 算「公共向量元素数」，再和「src/dst 的最大对齐位数」取 gcd 得到 `vec_bits`；若 `vec_bits` 是 8 的倍数且大于单元素位宽，就把 src/dst `recast<uint_bit_t<vec_bits>>` 重投成宽整数张量再 `copy_if`，等价于一次拷多个元素。否则退回逐元素。

**`AutoCopyAsync` 自动选 `cp.async`**——gmem→smem 的硬件加速入口：

[include/cute/algorithm/copy.hpp:122-163](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cute/algorithm/copy.hpp#L122-L163) —— 内部一个 `copy_op` lambda：在 `CUTE_ARCH_CP_ASYNC_SM80_ENABLED` 下，若 `is_gmem<Src> && is_smem<Dst>` 且 size 匹配 4/8/16 字节，返回 `SM80_CP_ASYNC_CACHEALWAYS`（只读且 16 字节时用 `CACHCGLOBAL`）；否则返回通用 `UniversalCopy`。这段就是「同一份 `copy` 在 Ampere+ 上变成异步拷贝指令」的根。

**SM90 TMA bulk copy**——Hopper 大块异步拷贝：

[include/cute/algorithm/copy.hpp:369-400](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cute/algorithm/copy.hpp#L369-L400) —— 当策略是 `SM90_BULK_COPY_AUTO` 时，先用 `max_common_layout` 找 src/dst 的公共子张量 `tiler`，要求至少 128 位；再按方向选 `SM90_BULK_COPY_G2S`（gmem→smem）或 `S2G`（smem→gmem），最后 `logical_divide` 后用具体宽度的 bulk atom 拷贝。这是 TMA 在 CuTe 算法层的落地（指令细节留到 u3-l2 专讲）。

**TiledCopy → Copy_Atom 的降级**——多线程拷贝的入口：

[include/cute/algorithm/copy.hpp:420-444](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cute/algorithm/copy.hpp#L420-L444) —— `copy(TiledCopy, src, dst)` 只是把 `TiledCopy` 静态转回它内含的 `CopyAtom` 再调用对应的 `copy`/`copy_if`。也就是说，「多线程划分」是 `TiledCopy` 在构造时一次性算好的（哪些线程负责哪些元素），真正拷贝时仍落到本讲的原子拷贝路径上。

**兜底断言**——防止传错策略：

[include/cute/algorithm/copy.hpp:470-494](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cute/algorithm/copy.hpp#L470-L494) —— 一个 catch-all 模板，对任何「未识别的 CopyPolicy」触发 `static_assert(dependent_false<CopyPolicy>, "Unrecognized CopyPolicy.")`。这是 C++ SFINAE 友好的报错手段：编译期就告诉你策略传错了，而不是链接时才挂。

#### 4.1.4 代码实践

**实践目标**：亲眼看到「同一个 `cute::copy` 调用，因 src/dst 的内存空间不同而走不同实现」。我们将构造一段 gmem→smem 的拷贝，并观察它能否正确搬运数据。

**操作步骤**：

1. 写一段最小内核（**示例代码，不要写进源码树**），用一个 gmem 张量作源、一个 smem 张量作目的，调用无策略的 `cute::copy(g_src, s_dst)`：

```cpp
// 示例代码：观察 cute::copy 的 gmem -> smem 拷贝
#include <cute/tensor.hpp>
using namespace cute;

template<int N>
__global__ void copy_demo_kernel(float const* gmem_in, float* gmem_out) {
  __shared__ float smem_buf[N];

  // 视图型张量：gmem 源、smem 目的，都是 (N,) 一维
  Tensor g_src = make_tensor(make_gmem_ptr(gmem_in), make_layout(make_shape(Int<N>{})));
  Tensor s_dst = make_tensor(make_smem_ptr(smem_buf), make_layout(make_shape(Int<N>{})));
  Tensor g_out = make_tensor(make_gmem_ptr(gmem_out), make_layout(make_shape(Int<N>{})));

  copy(g_src, s_dst);   // 期望编译成 cp.async / 向量化 load-store
  copy(s_dst, g_out);   // smem -> gmem，回写以便 host 校验

  if (threadIdx.x == 0) {
    printf("copy done, first elem = %f\n", gmem_out[0]);  // 占位观察
  }
}
```

2. 用 `nvcc -std=c++17 -arch=sm_80 -I$CUTLASS/include copy_demo.cu -o copy_demo` 编译（≥ SM80 才能触发 `cp.async` 路径；SM90 可观察 TMA）。可用 `cuobjdump --dump-sass copy_demo | grep -E 'cp.async|LDG|STS'` 粗看是否生成了异步拷贝指令。

**需要观察的现象**：

- `gmem_out` 的内容应与 `gmem_in` 完全一致，证明 `copy` 正确搬运。
- SASS 里 gmem→smem 段应出现 `cp.async` 类指令（SM80+），而非朴素 `LD.E`+`STS`；这是 `AutoCopyAsync` 分发的结果。
- 把 `make_smem_ptr` 换成普通 `float*`（即 untagged，被当成 rmem），SASS 会变成寄存器中转的 load/store——**印证「内存空间标签决定指令」**。

**预期结果**：拷贝结果正确（与输入逐元素相等）；具体 SASS 指令随架构不同。指令级精确输出标注为 **待本地验证**。若你当前无 GPU/NVCC，可改为纯 host 模拟：把两个张量都用普通指针构造（untagged），`copy` 会走 `AutoVectorizingCopy` 逐元素（或向量化）拷贝，依然能验证数据正确性。

#### 4.1.5 小练习与答案

**练习 1**：`copy_if(pred, src, dst)` 里为什么要写 `dst(i) = static_cast<DstType>(static_cast<SrcType>(src(i)))` 两层转型，而不是直接 `dst(i) = src(i)`？

**参考答案**：因为 `src(i)` 对子字节类型返回的是**代理引用**（如 `SubbyteReference`），不是裸值。先 `static_cast<SrcType>` 把它物化成真实数值，再 `static_cast<DstType>` 做跨类型转换（如 `half_t→float`），保证语义正确且触发数值转换的舍入逻辑；直接赋值可能走隐式转换，绕开 CUTLASS 定义的精确转换路径。

**练习 2**：无策略的 `copy(src, dst)` 在什么条件下会做「向量化」（把多个元素拼成宽整数一次拷）？

**参考答案**：当 src/dst 的「最大公共向量元素数」`common_elem > 1`，且 `common_elem × sizeof_bits(value_type)` 与「src/dst 最大对齐位数、假定对齐位数 `MaxVecBits`」的 gcd 得到的 `vec_bits` 是 8 的倍数且大于单元素位宽时，会把张量 `recast<uint_bit_t<vec_bits>>` 后再拷。静态 Layout 默认假定 128b 对齐，更容易触发宽向量。

---

### 4.2 cute::gemm 乘加模型

#### 4.2.1 概念说明

`cute::gemm` 是 CuTe 的「矩阵乘加」泛型算法。它的标准形式是**四元组**：

\[ D = A \times B + C \]

即把 `A·B` 的结果累加到 `C` 上、写到 `D`。为了写法简洁，还提供了**三元组**糖衣 `gemm(A, B, C)`，语义是 `C = A·B + C`（D 和 C 同一个张量）。你可以显式传一个 `MMA_Atom`（硬件乘加指令封装，下一讲 u2-l4 专讲），也可以省略——省略时默认用 `UniversalFMA`，一个纯软件的 `d = a*b + c`，host/device 都能跑，非常适合学习和 CPU 模拟。

`gemm` 最巧妙的地方是：**它不靠「矩阵形状」分派，而是靠「每个张量的维数 rank + 内存空间」分派**。源码头部的注释列出了 5 种 canonical 形态（见下），任何合法的 `gemm` 调用最终都会被归约成其中一种。这种「按 rank 分发」的设计，让同一个 `gemm` 既能表达「两个向量点乘」「两个矩阵相乘」，也能表达「带值域 V 的批量矩阵乘」——全看你怎么切张量。

#### 4.2.2 核心流程

`gemm` 的 5 种 canonical 分发形态（直接译自源码注释 [gemm.hpp:42-56](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cute/algorithm/gemm.hpp#L42-L56)）：

| # | 形态 | 含义 | 归约到 |
| --- | --- | --- | --- |
| 1 | `(V) × (V) => (V)` | 两个向量逐元素乘加（点积的一拍） | 直接调 MMA/FMA |
| 2 | `(M) × (N) => (M,N)` | 两向量外积 | 归约到 [3]，新增 K=(1) |
| 3 | `(M,K) × (N,K) => (M,N)` | 两矩阵乘（K 归约） | 归约到 [5]，V=(1) |
| 4 | `(V,M) × (V,N) => (V,M,N)` | 批量外积 | 对每个 (m,n) 归约到 [1] |
| 5 | `(V,M,K) × (V,N,K) => (V,M,N)` | 批量矩阵乘 | 对每个 k 归约到 [4] |

这里 `V` 是「**值域（value mode）**」——MMA 指令一次处理的元素数（即 `MMA_Atom` 的第 0 维），`M/N/K` 是矩阵的行/列/约简维。CuTe 的约定是：**张量的第 0 维若是 V，就代表「一条 MMA 指令内部」的并发维度**。

分发逻辑（伪代码）：

```
gemm(mma_atom, D, A, B, C):    # 4 元组
    根据 D/A/B/C 各自的 rank 与 is_rmem/is_smem 选重载：
      若 A,B,C,D 都是一维 (V)         => dispatch [1]: mma.call(D,A,B,C)
      若 A,B 一维、C,D 二维 (M,N)     => dispatch [2]: 给 A,B 补一维 K=1，转 [3]
      若 A,B,C,D 都二维 (M,K)(N,K)    => dispatch [3]: 给所有补 V=1，转 [5]
      若 A,B 二维 (V,M)(V,N)、C,D 三维 => dispatch [4]: 蛇形遍历 (m,n) 反复调 [1]
      若 A,B,C,D 都三维 (V,M,K)(V,N,K) => dispatch [5]: for k: 调 [4]
```

最值得一提的是 **dispatch [4] 里的「蛇形遍历（serpentine）」**：遍历 `(m,n)` 输出平面时，不是简单的行/列主序，而是**每一行（或列）走完后反向折回**（`ns = (m & 1) ? N-1-n : n`）。这样做是为了最大化**寄存器复用**：相邻两次 MMA 共享同一行 A 或同一列 B 的寄存器值，蛇形路径让「上次刚用过的寄存器」紧接着被下一次复用，减少寄存器装卸。源码还按 A/B 的「位宽组合」（64-bit+64-bit、32-bit+32-bit、混合）选不同的蛇形/kinked 变体，可谓把寄存器调度做到了极致。

此外还有一个 **smem 变体**：当 A、B 在共享内存（`is_smem`）而 C、D 在寄存器（`is_rmem`）时，`gemm` 不能直接让 MMA 读 smem（除 wgmma 外），于是它**先用 `make_fragment_A/B` 在寄存器里建好同形片段，`copy` 一份 smem→rmem，再做寄存器 gemm**——也就是「拷贝 + 乘加」的小流水线，就藏在 `gemm` 内部。

#### 4.2.3 源码精读

**三元组糖衣**——`gemm(A,B,C)` 即 `C = A*B + C`：

[include/cute/algorithm/gemm.hpp:65-75](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cute/algorithm/gemm.hpp#L65-L75) —— 三参 `gemm(A,B,C)` 直接转发到四参 `gemm(C, A, B, C)`，让 C 同时充当输入累加器和输出。带 `MMA_Atom` 的三参版本 [gemm.hpp:77-89](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cute/algorithm/gemm.hpp#L77-L89) 同理。

**默认 MMA = UniversalFMA**——不传 MMA 时的兜底：

[include/cute/algorithm/gemm.hpp:155-172](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cute/algorithm/gemm.hpp#L155-L172) —— 无 MMA 的四参 `gemm(D,A,B,C)` 用 `D/A/B/C` 的 `value_type` 实例化一个 `MMA_Atom<UniversalFMA<...>>`，再转发到带 MMA 的版本。`UniversalFMA` 本体见 [include/cute/arch/mma.hpp:45-62](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cute/arch/mma.hpp#L45-L62)，它每个寄存器只含 1 个元素，`fma(d,a,b,c)` 调用 `cute::fma`（即 `d = a*b + c`），host/device 通用——所以 `gemm` 在纯 CPU 上也能跑。

**dispatch [3]：矩阵乘 `(M,K)×(N,K)=>(M,N)`**——最常见的「小矩阵乘」入口：

[include/cute/algorithm/gemm.hpp:228-261](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cute/algorithm/gemm.hpp#L228-L261) —— 先用 `CUTE_STATIC_ASSERT_V` 校验 `AM==CM, BN==CN, AK==BK` 等形状一致性，再断言这是「1-value MMA」（`LayoutC_TV` 等第 1 维为 1），最后给四个张量都 `prepend<3>` 补一个最外层 `V=1` 维，转交 dispatch [5]。也就是说，[3] 是 [5] 在 `V=1` 时的特例。

**dispatch [5]：批量矩阵乘 `(V,M,K)×(V,N,K)=>(V,M,N)`**——K 维归约循环：

[include/cute/algorithm/gemm.hpp:388-416](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cute/algorithm/gemm.hpp#L388-L416) —— 对 K 维 `for (k=0; k<K; ++k)` 反复调用 dispatch [4]，每次取 `A(_,_,k)`、`B(_,_,k)`（K 维切片）做一次「批量外积累加」。这就是矩阵乘最内层 K 归约的真相。

**dispatch [4]：批量外积 + 蛇形遍历**——寄存器复用优化所在：

[include/cute/algorithm/gemm.hpp:263-386](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cute/algorithm/gemm.hpp#L263-L386) —— 按 `size<0>(A)*sizeof(A_elem)` 的位宽（64/32/混合）选不同的遍历策略，核心是 `int ns = (m & 1) ? N-1-n : n;` 这类蛇形坐标，让相邻 MMA 复用 A 的行/B 的列寄存器。每个 `(m,ns)` 调一次 dispatch [1]（即 `mma.call`）。这是 CuTe 把「指令调度」也算法化、自动化的典范。

**smem→rmem 变体**——gemm 内部藏的「拷贝 + 乘加」：

[include/cute/algorithm/gemm.hpp:462-498](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cute/algorithm/gemm.hpp#L462-L498) —— 当 A/B 是 smem、C/D 是 rmem 时，先用 `MMA_Atom::make_fragment_A/B` 在寄存器里建片段 `rA/rB`，然后 K 循环里 `copy(A(_,_,k), rA(_,_,k)); copy(B(_,_,k), rB(_,_,k)); gemm(mma, D, rA(_,_,k), rB(_,_,k), C);`——**先 copy 再 gemm**，把本讲的两个算法串成了一条小流水线。

#### 4.2.4 代码实践

**实践目标**：用默认 MMA（`UniversalFMA`）的 `cute::gemm` 在 CPU 上算一个小矩阵乘，验证 `D = A·B + C` 的语义，并体会「按 rank 分发」如何自动选择 dispatch [3]→[5]→[4]→[1]。

**操作步骤**：

1. 写一段纯 host 代码（**示例代码**），用未加标签的张量（被当成 rmem）构造 `A:(M,K)`、`B:(N,K)`、`C/D:(M,N)`，调用 `gemm(D, A, B, C)`：

```cpp
// 示例代码：CPU 上用 cute::gemm 做小矩阵乘（默认 UniversalFMA）
#include <cute/tensor.hpp>
#include <iostream>
using namespace cute;

int main() {
  constexpr int M = 4, N = 4, K = 4;
  float a_data[M*K], b_data[N*K], c_data[M*N], d_data[M*N];
  for (int i = 0; i < M*K; ++i) a_data[i] = float(i);          // 随便填
  for (int i = 0; i < N*K; ++i) b_data[i] = float((i % 5) - 2);
  for (int i = 0; i < M*N; ++i) { c_data[i] = 1.0f; d_data[i] = 0.0f; }

  // 视图型张量（普通指针 => 默认 rmem），行列主序自定
  auto layout_MK = make_layout(make_shape(M, K));   // (M,K) 行主序
  auto layout_NK = make_layout(make_shape(N, K));   // (N,K)
  auto layout_MN = make_layout(make_shape(M, N));   // (M,N)

  Tensor A = make_tensor(a_data, layout_MK);
  Tensor B = make_tensor(b_data, layout_NK);
  Tensor C = make_tensor(c_data, layout_MN);
  Tensor D = make_tensor(d_data, layout_MN);

  clear(D);                 // 先清零（见 4.3）
  gemm(D, A, B, C);         // D = A*B + C，默认 UniversalFMA

  for (int i = 0; i < M; ++i) {
    for (int j = 0; j < N; ++j) std::cout << D(i,j) << '\t';
    std::cout << '\n';
  }
}
```

2. 用支持 C++17 的 host 编译器编译（无需 NVCC，因为 `UniversalFMA` 和这些张量操作都是 `CUTE_HOST_DEVICE` 且走 host 路径）：`g++ -std=c++17 -I$CUTLASS/include gemm_demo.cpp -o gemm_demo && ./gemm_demo`。

**需要观察的现象**：

- 输出 `D` 应满足 `D(i,j) = sum_k A(i,k)*B(j,k) + C(i,j)`（注意 `B` 的布局是 `(N,K)`，即「B 的第 j 行」与「A 的第 i 行」点积，这正是 dispatch [3] 的 ` (M,K)×(N,K)` 约定）。
- 把 `gemm(D,A,B,C)` 换成 `gemm(A,B,C)`（三元组），应等价于 `C += A*B`，即 C 的每个元素被加上同样的矩阵积。

**预期结果**：与手算的 `A·Bᵀ + C`（按上述布局）逐元素相等。由于本环境无法运行编译，精确数值标注为 **待本地验证**；你可用一个朴素三重循环对照核对。

#### 4.2.5 小练习与答案

**练习 1**：调用 `gemm(D, A, B, C)` 时，A 是 `(M,K)`、B 是 `(N,K)`、C/D 是 `(M,N)`，这会经过哪几层 dispatch？为什么 B 用 `(N,K)` 而不是 `(K,N)`？

**参考答案**：经过 dispatch [3] → 补 `V=1` 转 [5] → K 循环里调 [4] → 每个 `(m,n)` 调 [1]（`mma.call`）。B 用 `(N,K)` 是因为 CuTe 的约定：A 与 B 在 K 维上对齐，且 `size<0>(A)==M==size<0>(C)`、`size<0>(B)==N==size<1>(C)`，源码用 `CUTE_STATIC_ASSERT_V(size<0>(A)==size<0>(C)); size<0>(B)==size<1>(C); size<1>(A)==size<1>(B);` 强制（[gemm.hpp:246-248](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cute/algorithm/gemm.hpp#L246-L248)）。这是一种「行列皆以 K 为最后一维」的紧凑表示，便于切片 `A(_,_,k)`、`B(_,_,k)`。

**练习 2**：dispatch [4] 里的 `int ns = (m & 1) ? N-1-n : n;` 蛇形坐标，相比朴素行主序 `n`，好处是什么？

**参考答案**：寄存器复用。行主序遍历 `(m,n)` 时，相邻两次 MMA 的 `m` 不变但 `n` 变，复用的是 A 的一行；当一行走完进入下一行（`m+1`）时，B 的列寄存器要全部重载。蛇形路径让下一行**反向**遍历 B，使「上一行末尾刚加载的 B 列」正好是下一行开头要用的，从而把 B 的寄存器复用率最大化，减少 smem→rmem 的装卸次数。

---

### 4.3 fill / clear 等辅助算法

#### 4.3.1 概念说明

除了 `copy` 和 `gemm`，CuTe 还有一组「辅助算法」处理张量的初始化与简单运算。最常用的两个是：

- **`fill(tensor, value)`**：把张量每个元素赋为 `value`。
- **`clear(tensor)`**：把张量清零，等价于 `fill(tensor, T{})`（`T{}` 即该类型的零值）。

它们看起来简单，但实现里藏着一个 C++ 技巧：**`prefer<1>/prefer<0>` 优先级匹配**。思路是——如果能调用一个「更优的实现」（比如直接对底层 `data()` 指针做 `memset`/`fill`），就用它；否则退回到「逐元素赋值」的默认实现。`prefer<N>` 本质是一个「带优先级的空 tag」，重载解析时 `prefer<1>` 比 `prefer<0>` 更特化，因此编译器会优先选择带 `prefer<1>` 的版本；只有当那个版本的签名不合法（SFINAE 失败，比如 Engine 没有 `data()` 成员）时，才回退到 `prefer<0>` 版本。这是一种在 CuTe 里反复出现的「**优先匹配更优实现，否则兜底**」模式。

同目录的 `axpby.hpp` 还提供 `axpby(alpha, X, beta, Y)`（计算 `Y = α·X + β·Y`），`cooperative_*.hpp` 提供多线程协作版的 copy/gemm——它们与 fill/clear 同属一套泛型算法风格，读懂本节后可举一反三。

#### 4.3.2 核心流程

`clear` 的流程极简：

```
clear(tensor):
    T zero = T{};          // 类型的零值（int->0, float->0.0, half_t->0）
    fill(tensor, zero);
```

`fill` 的流程（带 prefer 分发）：

```
fill(tensor, value):
    detail::fill(tensor, value, prefer<1>{});   # 先试更优版本
        # prefer<1> 版本：若 fill(tensor.data(), value) 合法 => 直接对指针操作
        # 否则 SFINAE 失败，回退 ↓
    detail::fill(tensor, value, prefer<0>{});   # 默认版本
        # for i in 0..size(tensor): tensor(i) = value;
```

#### 4.3.3 源码精读

**`clear` 的全部实现**：

[include/cute/algorithm/clear.hpp:54-62](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cute/algorithm/clear.hpp#L54-L62) —— 取 `value_type T`，调用 `fill(tensor, T{})`。`T{}` 对数值类型就是零值。就这几行，clear 是 fill 的极薄包装。

**`fill` 的 prefer 分发**：

[include/cute/algorithm/fill.hpp:56-85](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cute/algorithm/fill.hpp#L56-L85) —— 两个 `detail::fill` 重载：`prefer<1>` 版本签名带 `-> decltype(fill(tensor.data(), value))`，即「若 Engine 的 `data()` 能被 fill 处理就用它」（典型如 `ArrayEngine` 的裸指针，可整段赋值）；`prefer<0>` 版本是兜底，`CUTE_UNROLL` 逐元素 `tensor(i) = value`。外层 `fill` 用 `detail::fill(tensor, value, prefer<1>{})` 触发优先级选择。

> 这种 `prefer` 技巧在 `copy`、`gemm` 的某些扩展里也会出现，是读 CuTe 源码的「基础词汇」之一。

#### 4.3.4 代码实践

**实践目标**：在 4.2.4 的 gemm 示例里我们已经用过 `clear(D)`。这里把它单独拎出来观察：用 `fill` 给一段数据赋初值，再用 `clear` 清零，验证二者互为逆操作。

**操作步骤**：

```cpp
// 示例代码：fill 与 clear 互逆
#include <cute/tensor.hpp>
#include <iostream>
using namespace cute;

int main() {
  constexpr int N = 8;
  float buf[N];
  Tensor t = make_tensor(buf, make_layout(make_shape(Int<N>{})));
  fill(t, 3.14f);                 // 全填 3.14
  std::cout << "after fill: " << t(0) << ',' << t(7) << '\n';   // 期望 3.14,3.14
  clear(t);                       // 清零
  std::cout << "after clear: " << t(0) << ',' << t(7) << '\n';  // 期望 0,0
}
```

**需要观察的现象**：`fill` 后所有元素为 3.14；`clear` 后归零。

**预期结果**：如上。精确输出 **待本地验证**。

#### 4.3.5 小练习与答案

**练习**：为什么 `fill` 要用 `prefer<1>/prefer<0>` 两个重载，而不是直接写一个版本？

**参考答案**：为了「**能用更快的实现就用，不能用就兜底**」。有些 Engine（如 `ArrayEngine`）暴露了连续的 `data()` 指针，对它 `fill(data, value)` 可以整段赋值/向量化；但有些 Engine（如某些视图或代理 Engine）没有可 `fill` 的 `data()`，这时 `prefer<1>` 版本 SFINAE 失败，编译器自动选 `prefer<0>` 的逐元素兜底版。这样既不牺牲性能，又保证了通用性，无需用户手写特化。

---

### 4.4 算法与张量解耦的好处

#### 4.4.1 概念说明

回头看本讲三个算法，你会发现一个共同模式：**它们的函数签名只接受 `Tensor<Engine, Layout>`，从不关心 Engine 具体是 gmem/smem/rmem，也不关心 Layout 是行主序、列主序还是 swizzled**。所有「具体怎么做」的决定——用哪条硬件指令、是否向量化、按什么顺序遍历——都推迟到**重载分发**和**编译期类型推断**里。

这种「算法」与「张量（数据+布局）」的彻底解耦，是 CuTe 最核心的设计红利，体现在三个层面：

1. **代码复用**：一份 `copy` 源码，覆盖 gmem→smem（`cp.async`）、smem→rmem（`ldmatrix` 式）、rmem→rmem、smem↔gmem（TMA）等几乎所有数据搬运组合。你不需要为每种组合写一个 `copy_g2s`、`copy_s2r`。
2. **性能自动适配**：换一张 GPU（Ampere→Hopper→Blackwell）、换一种数据布局（加 swizzle），你的 kernel 源码**一个字都不用改**，CuTe 在编译期根据新的 Engine 类型/Layout/架构宏自动选用最优指令。
3. **可读性与可演进**：高层的 GEMM kernel 读起来就是「`copy` 搬数据 → `gemm` 做乘加 → `copy` 写回」三段式，硬件细节被算法签名吃掉了。当新架构带来新指令（如 Blackwell 的 UMMA），只需在 `arch/` 加一个新 atom、在算法分发里加一条路径，上层 kernel 无感升级。

这正是为什么前两讲要花大力气把 Layout、Engine、内存空间标签都设计成「编译期类型」：**只有数据与布局都是类型，算法才能靠重载/`if constexpr` 在编译期自动挑出最优实现**。本讲的 `copy`/`gemm` 就是这套设计兑现的红利。

#### 4.4.2 核心流程

解耦的「分发三要素」可概括为：

```
对一次 copy/gemm 调用，编译期依次判定：
  ① 策略参数（Copy_Atom / MMA_Atom / Auto* / 省略）   => 选算法族
  ② 各张量 Engine 的内存空间 (is_gmem/is_smem/is_rmem) => 选硬件指令
  ③ 各张量 Layout 的 rank、静态性、对齐                => 选向量化/遍历方式
三者皆类型 => 全部在编译期决定，零运行时开销。
```

#### 4.4.3 源码精读

**内存空间决定指令**——`AutoCopyAsync` 选 `cp.async` 的判定：

[include/cute/algorithm/copy.hpp:137-149](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cute/algorithm/copy.hpp#L137-L149) —— `if constexpr (is_gmem<SrcEngine>::value && is_smem<DstEngine>::value && sizeof(SrcType)==sizeof(DstType))` 才用 `SM80_CP_ASYNC_*`，否则 `UniversalCopy`。**同一行 `copy(...)`，因 Engine 不同而编译成不同指令**——解耦的实证。

**rank + 内存空间决定 gemm 分发**——smem 变体的触发条件：

[include/cute/algorithm/gemm.hpp:468-471](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cute/algorithm/gemm.hpp#L468-L471) —— `__CUTE_REQUIRES(... is_rmem<TD> ... is_smem<TA> ... is_smem<TB> ... is_rmem<TC>)` 这个 `requires` 子句让「A/B 在 smem、C/D 在 rmem」的调用自动匹配到「先 copy 再 gemm」的变体 [gemm.hpp:462-498](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cute/algorithm/gemm.hpp#L462-L498)；若 A/B 也是 rmem，则匹配纯寄存器版 [gemm.hpp:388-416](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cute/algorithm/gemm.hpp#L388-L416)。**调用方写的是同一个 `gemm`，分发全靠 Engine 类型**。

#### 4.4.4 代码实践

**实践目标**：用一个「对照实验」亲证解耦——同一段 `copy` 调用，分别作用于「gmem→smem」与「rmem→rmem」两组张量，观察生成的指令/行为不同，但**源代码完全相同**。

**操作步骤**：

1. 复用 4.1.4 的内核版 `copy_demo_kernel`（gmem→smem），编译后在 SASS 里确认有 `cp.async`。
2. 再写一个 host 版（4.1.4 末尾提到的纯指针版，untagged 即 rmem），`copy` 走 `AutoVectorizingCopy` 逐元素/向量化。
3. 对比：**两份代码里 `copy(src, dst)` 这一行一字不差**，差别只在 src/dst 的构造（`make_gmem_ptr`/`make_smem_ptr` vs 裸指针）。

**需要观察的现象**：GPU 版 SASS 出现异步拷贝指令；host 版只做普通内存读写。**算法源码不变，行为/指令随张量类型自适应**。

**预期结果**：如上。指令级证据 **待本地验证**（依赖具体架构与编译选项）。

#### 4.4.5 小练习与答案

**练习**：为什么 CuTe 强调「算法与张量解耦」是建立在「Layout 和 Engine 都是编译期类型」之上的？如果 Layout 是运行时变量，会丢失什么？

**参考答案**：因为算法的指令选择、向量化、遍历优化都依赖 `if constexpr` 和重载分发，而这些**只能在编译期、基于类型**进行。若 Layout 退化为运行时变量（如动态 shape + 动态 stride），编译器就无法在编译期算出「公共向量元素数」「是否对齐」「rank 是几」，于是 `if constexpr` 分支无法裁剪，向量化、`cp.async` 选择、gemm 的 rank 分发都会失效或退化成运行时分支，性能大幅下降。这就是 CuTe 极力推崇「**静态 Layout**」（`is_static<Layout>`）的根本原因——静态才能换来编译期最优分发。

---

## 5. 综合实践

本讲的综合实践，把 4 个模块串成一条完整链路：**先用 `cute::copy` 把一个 gmem 张量搬到 smem，再用 `cute::gemm`（默认 `UniversalFMA`）在 CPU/寄存器上完成一个 8×8 的小矩阵乘**，从而一次性体会「拷贝 + 乘加」这两个 CuTe 最重要的动作，以及它们对张量类型的自适应。

### 5.1 背景：一次最小的「数据搬运 + 计算」

真实的 GEMM kernel，最内层无非反复做两件事：**把 A/B 的 tile 从 gmem 搬到 smem（甚至 rmem），再做一次乘加**。本讲我们已经看到，`copy` 负责前者、`gemm` 负责后者，而且 gemm 的 smem 变体内部还自带一次 smem→rmem 的 copy。综合实践就把这条链路手动搭一遍，让你对「copy + gemm」形成肌肉记忆。

### 5.2 操作步骤

**步骤 1：写一段完整的 host 端示例**（**示例代码，不要写进源码树**）。它用普通指针（untagged，默认 rmem）演示 gmem→「smem 概念」的搬运与 8×8 gemm；在没有 GPU 时，gmem/smem 都用普通数组模拟，关键是验证 `copy` 与 `gemm` 的**语义正确性**与**调用方式**：

```cpp
// 示例代码：copy 搬运 + gemm 8x8 乘加（host 模拟）
#include <cute/tensor.hpp>
#include <iostream>
using namespace cute;

int main() {
  constexpr int M = 8, N = 8, K = 8;

  // 「gmem」源数据
  float A_g[M*K], B_g[N*K];
  for (int i = 0; i < M*K; ++i) A_g[i] = float((i % 7) - 3);
  for (int i = 0; i < N*K; ++i) B_g[i] = float((i % 5) - 2);

  // 「smem」缓冲（host 上就是普通数组）
  float A_s[M*K], B_s[N*K];
  // 累加器 / 输出
  float D_data[M*N];

  auto lay_MK = make_layout(make_shape(M, K));
  auto lay_NK = make_layout(make_shape(N, K));
  auto lay_MN = make_layout(make_shape(M, N));

  Tensor gA = make_tensor(A_g, lay_MK);   // gmem 视图
  Tensor gB = make_tensor(B_g, lay_NK);
  Tensor sA = make_tensor(A_s, lay_MK);   // smem 视图
  Tensor sB = make_tensor(B_s, lay_NK);
  Tensor D  = make_tensor(D_data, lay_MN);

  // (1) copy：把 gmem 的 A/B 搬到 smem（host 上即数组间拷贝）
  copy(gA, sA);
  copy(gB, sB);

  // (2) clear 累加器，再 gemm：D = sA * sB（C 用清零的 D 兜底）
  clear(D);
  Tensor C0 = make_tensor(D_data, lay_MN);   // C 与 D 共享存储，充当 C=A*B+C 的 C（已清零）
  gemm(D, sA, sB, C0);                        // D = sA·sB + C0

  // (3) 打印左上角 4x4 核对
  for (int i = 0; i < 4; ++i) {
    for (int j = 0; j < 4; ++j) std::cout << D(i,j) << '\t';
    std::cout << '\n';
  }
}
```

**步骤 2：编译运行**（无需 NVCC）：`g++ -std=c++17 -I$CUTLASS/include demo.cu -o demo && ./demo`（文件名可 `.cpp`）。

**步骤 3（可选，有 GPU 时）**：把上面的 host 逻辑改写成一个 `__global__` 内核：gmem 用 `make_gmem_ptr`、smem 用 `__shared__` + `make_smem_ptr`，A/B 装载后 `__syncthreads()`，再用一个真实的 `MMA_Atom`（下一讲 u2-l4 学）替换默认 `UniversalFMA` 来做 8×8 gemm。届时 `copy` 会自动变成 `cp.async`/TMA，`gemm` 会变成真正的 Tensor Core 指令——**源码结构不变，只是张量类型与 atom 换了**，这正是 4.4 解耦红利的最佳演示。

### 5.3 需要观察的现象

- `copy(gA, sA)` 后，`sA` 的内容与 `gA` 完全一致（可加打印核对）。
- `gemm` 后，`D(i,j) = sum_k sA(i,k)*sB(j,k)`（B 同样按 `(N,K)` 布局，行对行点积）。与朴素三重循环手算结果一致。
- 整个流程里 `copy` 和 `gemm` 的调用**完全没有提及内存空间或指令**——这些都由张量类型决定。

### 5.4 预期结果与运行说明

- 若成功编译运行，终端打印 8×8（示例只打印左上 4×4）的结果矩阵，与手算一致。
- 若无编译环境，结果数值标注为 **待本地验证**；可在纸上用小规模（如 M=N=K=2）手算对照。
- 关键不变量：无论 host 还是 GPU、无论默认 FMA 还是真实 MMA atom，**`copy` 与 `gemm` 的调用形式不变**——这是本讲最该带走的一句话。

---

## 6. 本讲小结

- **`cute::copy` 是统一的数据搬运入口**：靠重载分发自动挑指令——无策略时走 `AutoVectorizingCopy`（按对齐与公共向量自动 recast 向量化），`AutoCopyAsync` 时在 SM80+ 对 gmem→smem 自动选 `cp.async`，SM90 还能走 TMA bulk copy；`copy_if` 加谓词掩码用于边界 tile。
- **`cute::gemm` 是 `D = A*B + C` 的乘加泛型算法**：靠「各张量 rank + 内存空间」分发到 5 种 canonical 形态（`(V)`、`(M,N)`、`(M,K)/(N,K)`、`(V,M)/(V,N)`、`(V,M,K)/(V,N,K)`），不传 MMA 时默认用软件 `UniversalFMA`（host 也能跑）。
- **gemm 内部自带寄存器复用与 smem→rmem 拷贝优化**：dispatch [4] 用蛇形遍历最大化 A 行/B 列的寄存器复用；smem 变体会先 `make_fragment_A/B` + `copy` 把 smem 搬到 rmem 再做寄存器 gemm——「copy + gemm」小流水线就藏在 gemm 里。
- **`fill` / `clear` 是初始化辅助算法**：`clear` 即 `fill(tensor, T{})`；`fill` 用 `prefer<1>/prefer<0>` 优先匹配「对 `data()` 整段赋值」的更优实现，否则退回逐元素——这是 CuTe 反复出现的「优先更优、否则兜底」模式。
- **算法与张量彻底解耦是 CuTe 的核心红利**：同一份 `copy`/`gemm` 源码，因 Engine 内存空间（gmem/smem/rmem）与 Layout（静态/rank/对齐）不同，在编译期自动选用不同硬件指令与遍历策略，零运行时开销——这正是前两讲把 Layout/Engine/空间标签都做成「编译期类型」的回报。
- **静态 Layout 是解耦的前提**：只有 Layout 静态，编译期才能算出向量化、对齐、rank 分发；Layout 退化成运行时变量会让这些优化全部失效。

---

## 7. 下一步学习建议

本讲让张量真正「动」了起来——能搬（copy）、能算（gemm）、能初始化（fill/clear）。但本讲的 `gemm` 大多用了默认的 `UniversalFMA`（软件 FMA），还没真正用上 Tensor Core 硬件指令。自然的下一步是：

- **u2-l4（CuTe Atoms：MMA 与 Copy 原子）**：学习 `MMA_Atom` / `Copy_Atom` 如何把一条具体的硬件指令（如 SM80 的 `mma.m16n8k16`、SM90 的 `wgmma`、`cp.async`、TMA）封装成一个可复用的「原子」，以及 `TiledMma` / `TiledCopy` 如何把一个原子按线程划分给整个 warp。届时你就能把本讲的 `gemm(mma_atom, ...)` 里的 `mma_atom` 换成真正的 Tensor Core 原子，让 8×8 乘加跑在硬件加速路径上。
- **u2-l5（CUTLASS arch：指令级 MMA）**：往下看一层，了解 `cute/arch` 与 `cutlass/arch` 如何封装从 Volta 到 Hopper/Blackwell 各代 `mma`/`wgmma`/`umma` 指令，理解 atom 与底层 PTX 的衔接。
- **进阶伏笔**：本讲 4.2 提到的「smem 变体先 copy 再 gemm」、4.1 提到的 TMA bulk copy，分别在 u3-l1（异步流水线）和 u3-l2（TMA）展开——它们是 Hopper kernel 性能的关键，届时你会看到本讲这两个算法如何被编排进 producer/consumer 流水线。

继续阅读建议：先把 `include/cute/algorithm/gemm.hpp` 顶部的 5 形态注释和 dispatch [4] 的蛇形遍历吃透，再扫一眼 `axpby.hpp` / `cooperative_gemm.hpp`，你会发现自己已经能读懂大半个 CuTe 算法库。
