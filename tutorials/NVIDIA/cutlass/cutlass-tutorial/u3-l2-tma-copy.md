# TMA 异步张量拷贝

## 1. 本讲目标

本讲打开 u3-l1（异步流水线）里被当作「黑盒」使用的 TMA，讲清它的硬件机制与 CuTe 封装。读完本讲你应当能够：

- 说清 TMA（Tensor Memory Accelerator）是什么、它解决了哪些「逐元素 load」办不到的问题；
- 看懂一个 **TMA 描述符（TmaDescriptor / CUtensorMap）** 里装了哪些字段，以及这些字段必须满足的对齐与取值约束；
- 追踪 CuTe 是如何用 `Copy_Traits` + `Copy_Atom` 把「一条 PTX 指令」包装成可被 `cute::copy` 调用、可被 `TiledCopy` 切分的对象的；
- 解释为什么 TMA 描述符里要专门存一个 **swizzle 模式**，以及它和 swizzled 共享内存是怎么配合的。

本讲是 u3-l1 的「向下」延伸：u3-l1 讲流水线如何用 mbarrier 同步搬算；本讲讲那条被同步的搬运指令本身是怎么构造与发射的。

## 2. 前置知识

本讲假设你已掌握以下内容（来自前置讲义）：

- **CuTe Layout / Tensor**（u2-l1、u2-l2）：Layout 是坐标→下标的纯函数 `(Shape, Stride)`；Tensor = `(Engine, Layout)`；指针带 gmem/smem/rmem/tmem 空间标签，编译期分发指令。
- **CuTe copy 算法**（u2-l3）：`cute::copy(src, dst)` 是统一搬运入口，按张量的内存空间与 Layout 在编译期自动选硬件指令（如 SM80+ 的 `cp.async`）。
- **Copy_Atom / TiledCopy**（u2-l4）：`Copy_Atom` 封装单条指令，`TiledCopy` 把它沿线程/数值铺开，`partition_S/D` 把张量切到每个线程。
- **异步流水线**（u3-l1）：Hopper 用 `mbarrier` 的 `expect_tx`（按字节）语义让 Producer 发 TMA、Consumer 等待。本讲把当时的「TMA 硬件自动 complete_transaction」打开来看。

补充两个本讲会用到的硬件术语：

- **mbarrier（共享内存屏障）**：一块放在 shared memory 的 64 位数据，支持「按到达线程数」与「按传输字节数（transaction）」两种计数。TMA 完成搬运后会硬件翻转它，这就是「搬算重叠」的同步基础。
- **cp.async.bulk.tensor**：Hopper 引入的 PTX 指令族，即 TMA 的底层实现。`bulk` 表示「一整块」，`tensor` 表示「按多维坐标索引」。

## 3. 本讲源码地图

本讲围绕三个 `cute/arch` 与 `cute/atom` 文件展开，外加一个真实测试作为实践蓝本：

| 文件 | 作用 |
| --- | --- |
| [include/cute/arch/copy_sm90_desc.hpp](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cute/arch/copy_sm90_desc.hpp) | TMA 描述符的类型别名、枚举（swizzle/OOB/L2）、数据类型映射、mbarrier 辅助 PTX、以及描述符的设备端修改原语。 |
| [include/cute/arch/copy_sm90_tma.hpp](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cute/arch/copy_sm90_tma.hpp) | 裸 PTX 封装：`SM90_TMA_LOAD_*` / `SM90_TMA_STORE_*` / `SM90_TMA_LOAD_MULTICAST_*` / `SM90_BULK_COPY_*` 等 struct，每个 struct 把一条 `cp.async.bulk.tensor` 指令包成 `static copy(...)`。 |
| [include/cute/atom/copy_traits_sm90_tma.hpp](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cute/atom/copy_traits_sm90_tma.hpp) | CuTe 的 TMA 上层封装：`Copy_Traits` 把裸指令 + 描述符 + mbarrier 组成可执行对象，`make_tma_copy` 在主机端构造描述符并产出 `TiledCopy`。 |
| [include/cute/atom/copy_traits_sm90_tma_swizzle.hpp](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cute/atom/copy_traits_sm90_tma_swizzle.hpp) | 把 CuTe 的 `Swizzle<B,M,S>` 翻译成 TMA 描述符里的 swizzle 枚举。 |
| [test/unit/cute/hopper/tma_load_testbed.hpp](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/test/unit/cute/hopper/tma_load_testbed.hpp) | 一个完整、可编译的 TMA load 用例：构造描述符→初始化 mbarrier→`copy`→`wait_barrier`，是本讲综合实践的依据。 |

一条贯穿主线：**裸 PTX（`_tma.hpp`）→ 描述符（`_desc.hpp`）→ Traits/Atom 封装（`copy_traits_*.hpp`）→ `make_tma_copy` 工厂 → 使用方（testbed / collective mainloop）**。

## 4. 核心概念与源码讲解

### 4.1 TMA 机制与优势

#### 4.1.1 概念说明

在 Hopper 之前，把一块数据从 global memory 搬到 shared memory 的典型做法是：让一个线程块里的每个线程各自算出自己的 gmem 地址，发 `ld.global`/`cp.async`，再写进 smem。这条路径有两个开销：

1. **地址计算开销**：每个线程都要做乘加算地址，占用寄存器与指令发射槽；
2. **边界谓词开销**：tile 末尾可能越界，需要 `if (valid)` 之类的谓词分支。

**TMA（Tensor Memory Accelerator）** 是 Hopper 引入的专用搬运单元，它把「算地址 + 搬数据」整体下沉到硬件。一条 TMA 指令可以一次性搬走一个 1~5 维的「张量盒（box）」，硬件内部自己生成所有元素地址。官方文档 [0z_tma_tensors.md](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/media/docs/cpp/cute/0z_tma_tensors.md) 对它的概括是：

> A single TMA instruction can copy an entire tile of data all at once. As a result, the hardware no longer needs to compute individual memory addresses and issue a separate copy instruction for each element of the tile.

TMA 的关键设计是：它**不直接接收 gmem 指针**，而是接收一个预先在主机端构造好的**描述符（descriptor）**，外加一份**坐标（coordinates）**指向盒子的左上角。描述符里固化了「这块 gmem 张量长什么样」，运行时只需给坐标即可。

TMA 相对逐元素搬运的优势可以归纳为四点：

| 优势 | 说明 |
| --- | --- |
| 单指令搬整块 | 一条指令搬走整个 box，释放线程去算 MMA。 |
| 硬件管 OOB | 描述符带 OOB fill 策略，越界部分硬件自动填零/NaN，**省掉谓词**。 |
| 原生 multicast | 一条 `TMA_LOAD_MULTICAST` 可把同一块数据投递到 cluster 内多个 CTA 的 smem（见 u3-l1 的分布式搬运）。 |
| 异步 + mbarrier | 下单后硬件自动 `complete_transaction`，配合 mbarrier 实现「搬算重叠」（warp specialization）。 |

#### 4.1.2 核心流程

一次 TMA load 的生命周期分两段，主机一段、设备一段：

```text
[Host, 内核启动前]
  gmem 张量 + smem 布局 + cta_tile
        │  make_tma_copy(...)            ← 调驱动 cuTensorMapEncodeTiled 打包描述符
        ▼
  TiledCopy (内含 128 字节 TmaDescriptor)  ← 作为 grid_constant 传给内核

[Device, 内核内]
  mbarrier 初始化 + 设置 expect_tx 字节数
        │  copy(tma.with(mbar), src_coord_tensor, smem_tensor)
        ▼
  发射 cp.async.bulk.tensor (坐标 crd0..crd4)   ← 硬件搬数据，完成后翻转 mbarrier
        │  wait_barrier(mbar, phase)
        ▼
  数据已在 smem，可被 wgmma 消费
```

注意描述符是「全内核共享、只读」的常量；坐标才是每个 CTA、每次迭代变化的量。这正是 u3-l1 把 TMA 当黑盒时那句「下单后硬件自动 complete_transaction」的具体含义。

#### 4.1.3 源码精读

TMA load 的最底层是 struct `SM90_TMA_LOAD_1D`，它的 `copy` 就是把一条 `cp.async.bulk.tensor.1d` PTX 包起来。关键参数有四个：smem 目的地址、描述符、坐标、mbarrier：

```cpp
// include/cute/arch/copy_sm90_tma.hpp:47-79
struct SM90_TMA_LOAD_1D {
  CUTE_HOST_DEVICE static void
  copy(void const* desc_ptr, uint64_t* mbar_ptr, uint64_t cache_hint,
       void      * smem_ptr,
       int32_t const& crd0) {
    ...
    asm volatile (
      "cp.async.bulk.tensor.1d.shared::cluster.global.mbarrier::complete_tx::bytes.L2::cache_hint"
      " [%0], [%1, {%3}], [%2], %4;"
      :
      : "r"(smem_int_ptr), "l"(gmem_int_desc), "r"(smem_int_mbar),
        "r"(crd0), "l"(cache_hint)
      : "memory");
  }
};
```

[include/cute/arch/copy_sm90_tma.hpp:L47-L79](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cute/arch/copy_sm90_tma.hpp#L47-L79) —— 这条 PTX 的四个操作数分别对应：`[%0]` smem 目的、`[%1, {coord}]` 描述符+坐标、`[%2]` mbarrier、`%4` L2 cache hint。指令名里的 `complete_tx::bytes` 表示「TMA 硬件在搬完后自动按字节给 mbarrier 记一笔 transaction」，这正是异步等待得以成立的原因。

维度不同时用不同的 struct（`_2D/_3D/_4D/_5D`），坐标个数随之变化。为方便上层，CuTe 提供了一个按坐标个数分发的聚合 struct：

[include/cute/arch/copy_sm90_tma.hpp:L327-L363](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cute/arch/copy_sm90_tma.hpp#L327-L363) —— `SM90_TMA_LOAD` 用一组重载把 `copy(..., crd0)` ~ `copy(..., crd0..crd4)` 分别转发到对应的 `_1D.._5D`，所以上层只需写 `SM90_TMA_LOAD::copy(...)` 传任意个坐标即可。

反向（smem→gmem）的 store 指令不需要 mbarrier，而是用 `bulk_group` 提交、`wait_group` 等待：

[include/cute/arch/copy_sm90_tma.hpp:L957-L1001](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cute/arch/copy_sm90_tma.hpp#L957-L1001) —— `SM90_TMA_STORE_2D` 发的是 `cp.async.bulk.tensor.2d.global.shared::cta.bulk_group`，之后需要 `tma_store_arrive()`（`commit_group`）和 `tma_store_wait<0>()` 来保证写回完成。

#### 4.1.4 代码实践

**实践目标**：通过阅读 PTX 字符串，建立「TMA = 描述符 + 坐标 + mbarrier」的直觉。

**操作步骤**：

1. 打开 [include/cute/arch/copy_sm90_tma.hpp](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cute/arch/copy_sm90_tma.hpp)。
2. 用编辑器搜索 `cp.async.bulk.tensor`，分别定位 `_1D` 的 load（约 L60-74）、`_2D` 的 store（约 L991-996）、以及 `SM90_TMA_LOAD_MULTICAST_2D`（约 L670-677）。
3. 对比这三条 PTX 的修饰词差异。

**需要观察的现象**：

- load 指令含 `mbarrier::complete_tx::bytes`，store 指令含 `bulk_group` —— 二者同步模型不同；
- multicast load 多了一个 `%3` 即 `multicast_mask`（`h` 寄存器，16 位掩码），用于指定 cluster 内哪些 CTA 接收数据；
- 所有 TMA 指令都没有 gmem 地址操作数，gmem 信息全在描述符里。

**预期结果**：你能用一句话分别说清 load / store / multicast 三类指令「靠什么完成同步」。结果待本地验证（本实践为纯阅读型，无需运行）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `SM90_TMA_STORE` 没有 mbarrier 参数，而 `SM90_TMA_LOAD` 有？

**参考答案**：load 是 gmem→smem，CTA 需要知道「数据何时到位」才能开始算，故用 mbarrier 的 `expect_tx` 字节计数做同步；store 是 smem→gmem，写回方向上 CTA 只需保证「我写完之前别覆盖 smem」，用 `bulk_group + wait_group` 即可，不需要跨搬算的细粒度同步。

**练习 2**：`SM90_TMA_LOAD_MULTICAST_2D::copy` 比 `SM90_TMA_LOAD_2D::copy` 多了哪个参数？它语义上代表什么？

**参考答案**：多了 `uint16_t multicast_mask`。它是一个 16 位掩码，每一位对应 cluster 内一个 CTA，决定这次搬运的数据被投递到哪些 CTA 的 smem（即「一发多收」）。

---

### 4.2 TMA 描述符构造

#### 4.2.1 概念说明

TMA 描述符（CuTe 里别名成 `TmaDescriptor`，底层即 CUDA Driver 的 `CUtensorMap`）是一个 **128 字节、64 字节对齐**的打包结构，固化了一块 gmem 张量的「身份信息」加上若干 smem 行为开关。它的字段可以分成三组：

1. **gmem 侧**：基址指针、元素数据类型、每维大小（globalDim）、每维字节步长（globalStride，最内层维步长隐含为 1 个元素）；
2. **smem 侧**：每个 CTA 要搬的盒子大小（boxDim）、盒子内元素步长（boxStride，TMA 里恒为 1）、swizzle 模式；
3. **行为开关**：OOB 填充策略、L2 提升策略、交错模式（interleave）。

官方文档明确指出描述符「must be created on the host before kernel execution」并「shared between all thread blocks」。这是因为构造描述符需要调用 CUDA Driver API `cuTensorMapEncodeTiled`，它会对字段做合法性校验并打包成硬件可识别的 128 字节。

#### 4.2.2 核心流程

CuTe 把「从 CuTe 张量造描述符」的复杂逻辑收敛在一个工厂函数链里：

```text
make_tma_copy(copy_op, gtensor, slayout, cta_tiler, cluster_size)   ← 公共入口
        │
        ▼  detail::make_tma_copy_tiled
make_tma_copy_atom(...)        ← 推断 tma_gbasis（TMA 维 ↔ gmem 维的映射）
        │
        ▼
make_tma_copy_desc(...)        ← 真正算 gmem shape/stride、smem box、调驱动打包
        │  fill_tma_gmem_shape_stride(...)   ← 把 gmem 的多维 shape/stride 折叠进最多 5 维
        │  cuTensorMapEncodeTiled(...)        ← 驱动 API 打包成 128B 描述符
        ▼
返回 (TmaDescriptor, AuxParams)，再包成 Copy_Atom / TiledCopy
```

其中两段最关键的「数学」是：

- **gmem shape/stride 折叠**：gmem 张量可能有任意嵌套维度，但 TMA 最多 5 维。CuTe 用一组「basis 步长」追踪「哪些 gmem 模式合并进同一个 TMA 维」，再用递推公式算出合并后的等效 shape 与 stride。
- **步长转字节**：TMA 描述符存的是**字节步长**，且必须是 16 的倍数；而 CuTe Layout 里是元素步长，需要乘以 `sizeof_bits<T>/8`。

#### 4.2.3 源码精读

先看描述符类型别名与几个关键枚举，它们定义了描述符「行为开关」的取值空间：

[include/cute/arch/copy_sm90_desc.hpp:L291-L297](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cute/arch/copy_sm90_desc.hpp#L291-L297) —— `TmaDescriptor` 在能用 CUDA Driver 时就是 `CUtensorMap`，否则退化为 `alignas(64) { char bytes[128]; }`，后者保证了「128 字节、64 对齐」的物理形态不变。

[include/cute/arch/copy_sm90_desc.hpp:L150-L184](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cute/arch/copy_sm90_desc.hpp#L150-L184) —— `OOBFill`（ZERO / CONSTANT）、`L2Promotion`（DISABLE/B64/B128/B256）等枚举，正是描述符行为开关的取值。它们最终由下面的映射函数转成 Driver API 的常量。

数据类型与 swizzle 的映射也是在这里完成的，例如 `to_CUtensorMapDataType<T>()` 把 CuTe 类型（`half_t`/`float`/`float_e4m3_t`…）翻译成 `CU_TENSOR_MAP_DATA_TYPE_*`：

[include/cute/arch/copy_sm90_desc.hpp:L204-L237](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cute/arch/copy_sm90_desc.hpp#L204-L237) —— 注意 FP8（`float_e4m3_t`/`float_e5m2_t`）被映射成 `UINT8`，因为它们在存储上占 1 字节；而 FP4（`float_e2m1_t` 等）在更新的 CUDA 版本下走 `16U4_ALIGN8B` 之类专用枚举。

**描述符打包的核心**是 `make_tma_copy_desc`，它先准备 gmem 形状/步长，再调驱动 API：

[include/cute/atom/copy_traits_sm90_tma.hpp:L950-L984](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cute/atom/copy_traits_sm90_tma.hpp#L950-L984) —— 这段把 gmem 的 shape/stride 算好后，立刻用一组 `assert` 写死了硬件约束。逐条读这些 assert，就等于读 TMA 描述符的「硬性要求」：

```cpp
assert((reinterpret_cast<uint64_t>(gmem_address) & 0b1111) == 0);  // 基址必须 16B 对齐
...
assert(gmem_prob_stride[0] == 1 && "Majorness ...");              // 最内层维步长恒为 1 元素
// convert strides to byte strides
for(uint64_t& stride : gmem_prob_stride) {
  stride = (stride * sizeof_bits_v<TmaInternalType>) / 8;
}
assert((gmem_prob_stride[1]) < (uint64_t(1) << 40));               // 步长 < 2^40
assert((gmem_prob_stride[1] & 0b1111) == 0);                       // 步长必须是 16B 的整数倍
```

把这些约束提炼成一张表，就是 TMA 描述符 gmem 侧的全部规则：

| 字段 | 取值范围 / 约束 |
| --- | --- |
| gmem 基址 | 16 字节对齐 |
| 每维 size（globalDim） | \([1,\ 2^{32}]\) |
| 最内层维步长 | 隐含为 1 个元素（不存入描述符） |
| 其余维字节步长（globalStride） | \([0,\ 2^{40})\) 且为 16 字节的整数倍 |
| 描述符本身 | 128 字节、64 字节对齐 |

接着是 smem 盒子与真正的驱动调用：

[include/cute/atom/copy_traits_sm90_tma.hpp:L990-L1013](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cute/atom/copy_traits_sm90_tma.hpp#L990-L1013) —— `smem_box_shape` 由 `tma_gbasis` 各维 size 决定，每维被约束在 \([1,\ 256]\)（boxDim 上限 256）。`smem_box_stride` 这里恒为 1。

[include/cute/atom/copy_traits_sm90_tma.hpp:L1047-L1059](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cute/atom/copy_traits_sm90_tma.hpp#L1047-L1059) —— 真正的打包调用 `cuTensorMapEncodeTiled`，按顺序传入：描述符输出、数据类型、维数、gmem 基址、globalDim、`globalStrides + 1`（跳过隐含的第 0 步长）、boxDim、boxStride、interleave、swizzle、l2promotion、oobFill。

> 一个易被忽略的细节：`gmem_prob_stride.data() + 1` 显式跳过了第 0 维步长，因为「最内层维步长隐含为 1」（见 [copy_sm90_desc.hpp:L385](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cute/arch/copy_sm90_desc.hpp#L385) 注释 `// Strides must be a multiple of 16. Also, stride for the intermost dimension is implicitly 1`）。

最后，公共入口 `make_tma_copy` 对外提供简洁签名，内部处理 im2col 特例并下沉到 `detail::make_tma_copy_tiled`：

[include/cute/atom/copy_traits_sm90_tma.hpp:L1324-L1354](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cute/atom/copy_traits_sm90_tma.hpp#L1324-L1354) —— 文件顶部该函数前的长注释里给了多个用法示例（2D、GMMA swizzled、3D、cuTENSOR 4D），是理解「gtensor + slayout + cta_tile 三者如何决定一个 TMA」的最佳入口。

#### 4.2.4 代码实践

**实践目标**：找到 TMA 描述符的构造代码，说清它如何表达一个多维张量盒与步长，并指出对齐要求。

**操作步骤**：

1. 打开 [include/cute/atom/copy_traits_sm90_tma.hpp](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cute/atom/copy_traits_sm90_tma.hpp)，定位 `make_tma_copy_desc`（约 L929）。
2. 阅读它在调用 `cuTensorMapEncodeTiled` 之前对 `gmem_prob_shape`、`gmem_prob_stride`、`smem_box_shape` 三组数组的填充与断言（L950-L1013）。
3. 对照 `fill_tma_gmem_shape_stride`（L858-L901），看它如何用递推公式把多个 gmem 模式折叠成一个 TMA 维。
4. 写一段 100~200 字的说明：**描述符如何表达「盒」与「步长」，以及有哪些对齐要求**。

**需要观察的现象**：

- gmem 侧用 `globalDim`（每维元素数）+ `globalStride`（每维**字节**步长）刻画整块张量；
- smem 侧用 `boxDim`（每维元素数 ≤ 256）刻画「一次搬多大」；
- 三处硬约束：基址 16B 对齐、非最内层步长须为 16B 整数倍且 < 2^40、描述符 64B 对齐。

**预期结果**：你能写出类似下面这段话——

> TMA 描述符用 `globalDim` 描述整块 gmem 张量每维的元素个数，用 `globalStride` 描述第 1~4 维的**字节**步长（第 0 维步长隐含为 1 个元素），用 `boxDim` 描述每次搬运的盒子大小（每维 ≤256）。对齐上，要求 gmem 基址 16B 对齐、所有非最内层字节步长为 16B 的整数倍且小于 2^40，描述符自身 64B 对齐、占 128 字节。

本实践为源码阅读型，**待本地验证**（可选：在构造描述符处临时加一条 `printf` 打印 `gmem_prob_shape`/`gmem_prob_stride`，再用 `CUTLASS_NVCC_ARCHS=90a` 编译 `test_unit` 观察输出）。

#### 4.2.5 小练习与答案

**练习 1**：描述符里为什么不存第 0 维（最内层维）的步长？

**参考答案**：TMA 假定最内层维在内存中紧密排布，步长恒为 1 个元素，因此描述符不存它，构造时直接 `globalStrides + 1` 跳过。代码里用 `assert(gmem_prob_stride[0] == 1)` 强制这一前提（[L969](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cute/atom/copy_traits_sm90_tma.hpp#L969)）。

**练习 2**：若一块 gmem 张量最内层维步长不是 1（即非「major」方向），CuTe 会怎么处理？

**参考答案**：`make_tma_copy_desc` 内部通过 `tma_gbasis` 把 gmem 的模式重新排列，使进入 TMA 描述符的最内层维步长为 1；也就是说它会把「逻辑上的非 major 维」通过 basis 映射换到 TMA 的某一维，保证描述符的 major 约束被满足。若实在无法满足会触发断言。

---

### 4.3 copy_traits 的 TMA 封装

#### 4.3.1 概念说明

裸 PTX struct（如 `SM90_TMA_LOAD_2D`）只是一条指令，不能直接喂给 `cute::copy`。CuTe 在 u2-l4 引入过 `Copy_Traits` / `Copy_Atom` / `TiledCopy` 三段式，TMA 是这套机制的典型用户。对 TMA 而言，`Copy_Traits` 多承担了一项职责：**携带描述符**。

这里有一个设计要点：描述符（常量、全内核共享）与 mbarrier（每个 stage、每块缓冲都不同）的生命周期完全不同。CuTe 的解法是把 TMA load 的 traits 分成两种：

- **不可执行版** `Copy_Traits<SM90_TMA_LOAD, ...>`：只持描述符，`copy_unpack` 被 `= delete` 禁掉，不能直接 `copy`；
- **可执行版** `Copy_Traits<SM90_TMA_LOAD_OP, ...>`：额外持 mbarrier 指针与 cache hint，真正能发指令。

二者用 `.with(mbar)` 衔接：运行时拿「描述符 + 当前 stage 的 mbarrier」临时拼出一个可执行 traits。这正是 u3-l1 流水线里 `producer_get_barrier` 之后那条 `copy` 调用的内部机制。

#### 4.3.2 核心流程

```text
make_tma_copy(...) 返回的 TiledCopy 内含 Copy_Atom< Copy_Traits<SM90_TMA_LOAD, Bits, Aux> >
                                                          │ 持 tma_desc_，不可执行
                                                          ▼  .with(mbar, multicast_mask, cache_hint)
                                          Copy_Traits<SM90_TMA_LOAD_OP, Bits>
                                                          │ 额外持 (desc*, mbar*, cache) 三元组
                                                          ▼  cute::copy(tma.with(mbar), src, dst)
                                                          ▼  触发 copy_unpack
                                          取 src(Int<0>) 当坐标、取 dst.data() 当 smem 地址
                                                          ▼  CallCOPY<SM90_TMA_LOAD_OP>
                                          SM90_TMA_LOAD::copy(desc, mbar, cache, smem, crd0..crd4)
                                                          ▼
                                          发射 cp.async.bulk.tensor PTX
```

`copy_unpack` 是 `cute::copy` 与具体指令之间的「粘合层」（u2-l4 已介绍其角色）：它从 CuTe 张量里抽出指令真正需要的操作数（对 TMA 而言是坐标与 smem 指针），再连同 traits 自带的 `opargs_`（描述符、mbar、cache）一起喂给 PTX。

#### 4.3.3 源码精读

先看不可执行版 traits，重点是它如何「持描述符」并提供 `.with()`：

[include/cute/atom/copy_traits_sm90_tma.hpp:L102-L134](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cute/atom/copy_traits_sm90_tma.hpp#L102-L134) —— `Copy_Traits<SM90_TMA_LOAD, NumBitsPerTMA, AuxParams>` 把描述符存为成员 `tma_desc_`，并提供：

```cpp
// 不可执行版：copy_unpack 被 delete，必须先 .with()
CUTE_HOST_DEVICE constexpr
Copy_Traits<SM90_TMA_LOAD_OP, NumBitsPerTMA>
with(uint64_t& tma_mbar, uint16_t const& multicast_mask = 0,
     TMA::CacheHintSm90 const& cache_hint = ...) const {
  return {&tma_desc_, &tma_mbar, static_cast<uint64_t>(cache_hint)};
}
```

注意它的 `ThrID = Layout<_1>`、`SrcLayout = DstLayout = Layout<Shape<_1, NumBitsPerTMA>>`：这说明 TMA 是「单线程（thr=0）发指令、一次搬 NumBitsPerTMA 位」的操作，所有线程划分都在外层 `TiledCopy` 完成，traits 层只描述「这一个线程发的这一条指令」。

可执行版的 `copy_unpack` 才是真正发射的地方，它继承自 `TMA_LOAD_Unpack`：

[include/cute/atom/copy_traits_sm90_tma.hpp:L67-L92](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cute/atom/copy_traits_sm90_tma.hpp#L67-L92) —— 这里有两个关键动作：

```cpp
static_assert(is_smem<TD>::value, "SM90_TMA_LOAD requires the destination be shared memory.");
auto src_coord = src(Int<0>{});                       // 从 src 张量取出坐标元组
void* dst_ptr  = cute::raw_pointer_cast(dst.data());  // 取 smem 目的地址
return detail::explode_tuple(detail::CallCOPY<CopyOp>{},
    traits.opargs_, ..., make_tuple(dst_ptr), seq<0>{},
    src_coord, ...);
```

- **`is_smem<TD>` 断言**：TMA load 的目的必须是 smem 张量（这是 u2-l2 空间标签的用武之地，编译期保证）；
- **`src(Int<0>)`**：src 是一个「坐标张量」，对下标 0 取值得到一个坐标元组（即 `crd0..crd4`），这正是 4.1 里 PTX 需要的坐标；
- **`explode_tuple(CallCOPY)`**：把 `opargs_`（desc, mbar, cache）与坐标、smem 指针按位置拼起来，调用 `SM90_TMA_LOAD_OP::copy(...)`，最终落到 4.1 的 ND 分发。

`make_tma_copy_atom` 把这一切组装成一个 `Copy_Atom`：

[include/cute/atom/copy_traits_sm90_tma.hpp:L1139-L1194](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cute/atom/copy_traits_sm90_tma.hpp#L1139-L1194) —— 它先调 `detail::make_tma_copy_desc` 拿到描述符与 aux 参数，再用 `num_bits_per_tma = tma_gbasis_size * sizeof_bits<TmaInternalType>` 算出「单次 TMA 搬的位数」，最后 `return Atom{tma_traits}`。store 与 multicast 的 traits 结构完全对称，只是 opargs 多/少了 multicast_mask、mbar。

#### 4.3.4 代码实践

**实践目标**：追踪「描述符 + mbarrier → PTX」这条调用链，确认 `cute::copy` 最终如何变成一条 `cp.async.bulk.tensor`。

**操作步骤**：

1. 从 [tma_load_testbed.hpp:L143](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/test/unit/cute/hopper/tma_load_testbed.hpp#L143) 的 `copy(tma.with(tma_load_mbar[0]), tAgA(_,stage), tAsA(_,0))` 出发。
2. `tma.with(...)` 命中 [copy_traits_sm90_tma.hpp:L125-L134](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cute/atom/copy_traits_sm90_tma.hpp#L125-L134)，返回可执行 traits。
3. `copy(...)` 触发可执行 traits 的 `copy_unpack`（[L67-L92](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cute/atom/copy_traits_sm90_tma.hpp#L67-L92)）。
4. 最终落到 [copy_sm90_tma.hpp:L327-L363](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cute/arch/copy_sm90_tma.hpp#L327-L363) 的 `SM90_TMA_LOAD::copy`。

**需要观察的现象**：每一步分别贡献了什么操作数——`.with` 贡献 mbar，`copy_unpack` 贡献坐标与 smem 地址，`SM90_TMA_LOAD::copy` 贡献 PTX。

**预期结果**：你能画出这条链路上四个角色（`make_tma_copy`、`with`、`copy_unpack`、`SM90_TMA_LOAD::copy`）各自负责什么的表格。本实践为源码阅读型，**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：为什么把 TMA load 的 traits 拆成「不可执行」与「可执行」两种，而不是直接让一个 traits 既持描述符又持 mbar？

**参考答案**：描述符是全内核共享的常量，只构造一次；mbarrier 随每个流水线 stage、每块缓冲变化。若把 mbar 绑死在 traits 里，每换个 stage 就得重建整个 traits。用 `.with(mbar)` 在运行时「描述符 + 当前 mbar」临时拼出可执行对象，既复用了描述符，又能灵活配对不同 mbar。

**练习 2**：`TMA_LOAD_Unpack::copy_unpack` 里那句 `static_assert(is_smem<TD>::value)` 体现了 u2-l2 哪个设计？

**参考答案**：体现了「指针带 gmem/smem/rmem/tmem 编译期空间标签」。`is_smem<TD>` 在编译期判定目的张量是否在共享内存，从而在编译期就把「TMA load 必须写到 smem」这一硬件要求变成类型检查，零运行时开销。

---

### 4.4 TMA + swizzle 共享内存

#### 4.4.1 概念说明

回顾 u2-l2：为了让 wgmma 读 smem 时不撞 bank conflict，CUTLASS 常给 smem 套一个 **swizzle**（如 `Swizzle<3,3,3>`），通过 XOR 重排物理地址，使访问模式与 bank 错开。问题来了：TMA 是硬件搬运，它写进 smem 的物理地址由谁决定？

答案是：**TMA 描述符里也存了一个 swizzle 模式**。当你给 TMA 指定了 swizzle，硬件在搬数据时就会按这个 swizzle 把元素写到「错位」的 smem 地址；之后 wgmma 用同一套 swizzle 布局去读，二者天然匹配。也就是说，swizzle 这件事被 TMA 与 wgmma **共享同一份约定**，描述符就是这份约定的载体。

这带来一个关键约束：**描述符里的 swizzle 模式必须与 smem Layout 的 swizzle 完全一致**，否则 TMA 写入的物理排布与 wgmma 期望的排布对不上，数据会错位。

#### 4.4.2 核心流程

CuTe 用 `Swizzle<B,M,S>` 三参数描述 swizzle（B=异或位数、M=基底位、S=起始位）。把它翻译成 TMA 描述符枚举分两步：

```text
Swizzle<B,M,S>                         ← CuTe 布局里的 swizzle
      │  get_tma_swizzle_bits(...)     ← B → SmemSwizzleBits  (B128/B64/B32/DISABLE)
      │  get_tma_swizzle_base(...)     ← M → SmemSwizzleBase  (16B/32B/64B 基底)
      ▼
TMA::to_CUtensorMapSwizzle(bits, base) ← 组合成 Driver 枚举 CU_TENSOR_MAP_SWIZZLE_*B
      ▼
作为 smem_swizzle 传给 cuTensorMapEncodeTiled
```

CuTe 与硬件对 swizzle 的「合法取值」有一套严格约定（见下方源码），不是任意 `<B,M,S>` 都能用 TMA 搬。

#### 4.4.3 源码精读

描述符侧先定义 swizzle 枚举的取值空间，并把 CuTe 选定的 swizzle 写进描述符：

[include/cute/arch/copy_sm90_desc.hpp:L134-L148](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cute/arch/copy_sm90_desc.hpp#L134-L148) —— `SmemSwizzleBits`（DISABLE/B32/B64/B128）表示「异或几位」，`SmemSwizzleBase`（16B/32B/64B…）表示「在哪个基底上异或」。

[include/cute/arch/copy_sm90_desc.hpp:L239-L263](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cute/arch/copy_sm90_desc.hpp#L239-L263) —— `to_CUtensorMapSwizzle(bits, base)` 把二者组合映射到 Driver 枚举：`B128+16B → 128B`、`B64+16B → 64B`、`B32+16B → 32B`、`DISABLE → NONE`，更新的 CUDA 还支持 `128B_ATOM_32B` 等组合。

CuTe 的 `Swizzle<B,M,S>` 到这两枚举的翻译在专门的 swizzle 头里：

[include/cute/atom/copy_traits_sm90_tma_swizzle.hpp:L45-L67](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cute/atom/copy_traits_sm90_tma_swizzle.hpp#L45-L67) —— `get_tma_swizzle_bits(Swizzle<B,M,S>)` 的核心是「当 `M==4`（16B 基底）时，`B` 直接决定异或位宽」：

```cpp
if constexpr (M == 4) {
  if constexpr (B == 3) { return TMA::SmemSwizzleBits::B128; }
  if constexpr (B == 2) { return TMA::SmemSwizzleBits::B64; }
  if constexpr (B == 1) { return TMA::SmemSwizzleBits::B32; }
  if constexpr (B == 0) { return TMA::SmemSwizzleBits::DISABLE; }
}
```

[include/cute/atom/copy_traits_sm90_tma_swizzle.hpp:L76-L105](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cute/atom/copy_traits_sm90_tma_swizzle.hpp#L76-L105) —— `get_tma_swizzle_base` 则把 `M` 映射到基底：`M==4 → 16B`、`M==5 → 32B`、`M==6 → 64B`，并对 `S` 也做了断言（如 `M==4` 时要求 `S==3`）。

把这套映射接回描述符构造，就在 `make_tma_copy_desc` 里：

[include/cute/atom/copy_traits_sm90_tma.hpp:L1044-L1059](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cute/atom/copy_traits_sm90_tma.hpp#L1044-L1059) —— 注意它从 `make_tma_copy_atom` 传入的 `smem_swizzle`（即 `get_swizzle_portion(slayout)`，由 smem Layout 自动提取）得到 bits/base，再转成 Driver 枚举塞进描述符。**因为 swizzle 取自 slayout，所以「描述符 swizzle = smem Layout swizzle」是自动保证的**，这就是 4.4.1 提到的「同一份约定」。

#### 4.4.4 代码实践

**实践目标**：验证「描述符里的 swizzle 来自 smem Layout」，并理解它如何与 wgmma 的读取对齐。

**操作步骤**：

1. 在 [copy_traits_sm90_tma.hpp:L1149-L1152](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cute/atom/copy_traits_sm90_tma.hpp#L1149-L1152) 确认 `make_tma_copy_atom` 用 `get_swizzle_portion(slayout)` 与 `get_nonswizzle_portion(slayout)` 把 smem Layout 拆成「swizzle 部分」与「纯布局部分」。
2. 跟踪 `smem_swizzle` 如何经 `make_tma_copy_desc` → `get_tma_swizzle_bits/base` → `to_CUtensorMapSwizzle` 写进描述符（[L1044-L1046](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cute/atom/copy_traits_sm90_tma.hpp#L1044-L1046)）。
3. 写一段说明：**为什么 TMA 写入的物理排布恰好能被 wgmma 正确读取**。

**需要观察的现象**：swizzle 既不在 TMA 指令参数里，也不在 wgmma 指令参数里，而是分别藏在「描述符」和「smem Layout」里，而二者由同一个 `slayout` 派生。

**预期结果**：你能写出类似——

> 因为 `make_tma_copy` 从同一个 `slayout` 抽取 swizzle 写进描述符，TMA 硬件搬运时就按该 swizzle 重排物理地址；而 wgmma 侧也用同一个 `slayout` 构造 smem 张量并解读地址。二者共享同一份 swizzle 约定，所以 TMA 写入的物理排布与 wgmma 期望完全一致，无需额外转置。

本实践为源码阅读型，**待本地验证**（可选：在 `CUTLASS_NVCC_ARCHS=90a` 下用一个带 `Swizzle<3,3,3>` 的 smem Layout 跑 `tma_load_testbed`，对比无 swizzle 版本的输出，应完全一致——证明 swizzle 不改变逻辑结果，只改变物理排布）。

#### 4.4.5 小练习与答案

**练习 1**：`Swizzle<3,4,3>` 对应 TMA 描述符里哪种 swizzle？为什么？

**参考答案**：`M==4` 走 16B 基底分支；`B==3` 对应 `SmemSwizzleBits::B128`；`S==3` 满足 `M==4` 时 `S==3` 的断言。所以描述符 swizzle 枚举为 `CU_TENSOR_MAP_SWIZZLE_128B`（在 16B 基底上异或 128 位）。

**练习 2**：如果你给 `make_tma_copy` 传一个 smem Layout 带 swizzle，却让 wgmma 用「无 swizzle」的布局去读同一块 smem，会发生什么？

**参考答案**：数据会错位。TMA 按描述符里的 swizzle 把元素写到「错位」的物理地址，而 wgmma 按无 swizzle 的线性地址解读，两者对地址的约定不一致，于是读到的元素排列与逻辑张量不符。这正是 4.4.1 强调的「描述符 swizzle 必须与 smem Layout swizzle 一致」。

---

## 5. 综合实践

把本讲四个模块串起来，完成一次「**阅读型**」端到端追踪，目标是画出一张从 gmem 张量到 PTX 指令的完整 TMA load 流程图。

**任务**：以 [test/unit/cute/hopper/tma_load_testbed.hpp](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/test/unit/cute/hopper/tma_load_testbed.hpp) 为蓝本，回答下列问题并整理成一张表/图。

1. **主机端构造描述符**：找到 [tma_load_testbed.hpp:L187](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/test/unit/cute/hopper/tma_load_testbed.hpp#L187) 的 `make_tma_copy<TmaType>(copy_op, gA, smem_layout, cta_tile, Int<1>{})`。说明它的 5 个参数分别是什么；它内部最终调用了哪个 Driver API（提示：`cuTensorMapEncodeTiled`），这个调用发生在主机还是设备？

2. **描述符如何传入内核**：观察 [tma_load_testbed.hpp:L56-L59](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/test/unit/cute/hopper/tma_load_testbed.hpp#L56-L59) 的 `CUTE_GRID_CONSTANT TiledCopy const tma`。为什么描述符要用 `grid_constant`（`__grid_constant__`）修饰？

3. **设备端同步四件套**：阅读 [tma_load_testbed.hpp:L130-L149](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/test/unit/cute/hopper/tma_load_testbed.hpp#L130-L149) 的循环体，标注出这四步分别对应本讲哪个源码点：
   - `initialize_barrier`（[copy_sm90_desc.hpp:L62-L73](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cute/arch/copy_sm90_desc.hpp#L62-L73)）
   - `set_barrier_transaction_bytes`（[L76-L87](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cute/arch/copy_sm90_desc.hpp#L76-L87)）
   - `copy(tma.with(mbar), ...)`（`.with` 见 [copy_traits_sm90_tma.hpp:L125-L134](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cute/atom/copy_traits_sm90_tma.hpp#L125-L134)）
   - `wait_barrier`（[copy_sm90_desc.hpp:L89-L110](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cute/arch/copy_sm90_desc.hpp#L89-L110)）

   并解释 `kTmaTransactionBytes`（[L134](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/test/unit/cute/hopper/tma_load_testbed.hpp#L134)）为什么必须与「这次 TMA 实际搬运的字节数」一致。

4. **手绘流程图**：把上述步骤画成一张包含「Host / Device / 硬件 TMA 单元」三个泳道的流程图，标出描述符、坐标、mbarrier 三类信息各自的流动路径。

**预期产物**：一张表（5 个参数 + Driver API + 主机/设备）+ 一段对 `grid_constant` 的解释 + 一张三泳道流程图。

**进阶（可选运行）**：若本机有 Hopper（SM90）且 CUDA ≥ 12，用 `CUTLASS_NVCC_ARCHS=90a` 编译 `test_unit`，过滤 `tma_load` 相关用例运行，确认测试通过（说明描述符构造与搬运逻辑正确）。无硬件时此项标注「待本地验证」。

> 安全提示：综合实践只阅读与（可选地）编译现有测试，不修改任何源码。

## 6. 本讲小结

- **TMA 是硬件搬运单元**：一条 `cp.async.bulk.tensor` 指令搬走整个 1~5 维 box，硬件自算地址、自管 OOB、支持 multicast，配合 mbarrier 实现搬算重叠（4.1）。
- **描述符是 gmem 张量的「身份证」**：128 字节、64B 对齐，在主机端由 `cuTensorMapEncodeTiled` 打包；gmem 基址须 16B 对齐、非最内层字节步长须为 16B 的整数倍且 < 2^40，每维 size 与 box 每维分别受 2^32 与 256 上限约束（4.2）。
- **`make_tma_copy` 是 CuTe 的 TMA 工厂**：吃 gmem 张量 + smem Layout + cta_tile，自动算出 gmem shape/stride、smem box、swizzle，产出含描述符的 `TiledCopy`（4.2、4.3）。
- **`Copy_Traits` 用「不可执行/可执行」双形态**分离描述符与 mbar 的生命周期：`.with(mbar)` 在运行时把二者拼成可执行 traits，再经 `copy_unpack` 抽出坐标与 smem 地址、发射 PTX（4.3）。
- **swizzle 是 TMA 与 wgmma 的共享约定**：描述符里的 swizzle 直接取自 smem Layout，故硬件搬运的物理排布天然匹配 wgmma 的读取，二者由同一份 `slayout` 保证一致（4.4）。
- **全链路**：`make_tma_copy` → `Copy_Atom/TiledCopy` → `.with(mbar)` → `copy_unpack` → `SM90_TMA_LOAD::copy` → `cp.async.bulk.tensor` PTX，配合 `initialize_barrier/set_barrier_transaction_bytes/wait_barrier` 完成异步搬运（综合实践）。

## 7. 下一步学习建议

- **回到流水线**：带着本讲对描述符与 `expect_tx` 字节计数的理解，重读 u3-l1 的 `PipelineTmaAsync`，你会看清 `producer_get_barrier` + TMA + `consumer_wait` 是如何精确对应本讲的 `set_barrier_transaction_bytes` + `copy(tma.with(mbar))` + `wait_barrier` 的。
- **看真实内核如何用 TMA**：阅读 [include/cutlass/gemm/collective/sm90_mma_tma_warpspecialized.hpp](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/collective/sm90_mma_tma_warpspecialized.hpp)，观察 `CollectiveBuilder` 自动推断出的 `make_tma_copy` 调用如何被 producer warp group 在主循环里反复 `.with(mbar)` 发射。
- **进阶到 Blackwell**：本讲的 `SM90_TMA_*` 是 Hopper 版；Blackwell（SM100）扩展了 gather/scatter 等新 TMA 形态，可对照 `include/cute/arch/copy_sm100_tma.hpp`（若存在）与 4.2 里 `make_tma_copy_atom` 对 `SM100_TMA_LOAD_2D_GATHER4` 的特判（[L1153-L1155](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cute/atom/copy_traits_sm90_tma.hpp#L1153-L1155)）继续学习，承接 u3-l7。
- **设备端改描述符**：若对 grouped/ptr-array GEMM 感兴趣，阅读 [copy_sm90_desc.hpp:L324-L417](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cute/arch/copy_sm90_desc.hpp#L324-L417) 的 `tensormap.replace.*` 系列 PTX，了解如何在内核内动态替换描述符的地址/维度/步长，这是 u3-l4 Grouped GEMM 的底层基础。
