# 张量内存加速器 TMA

## 1. 本讲目标

本讲深入 Hopper（sm_90+）与 Blackwell（sm_100a）引入的 **TMA（Tensor Memory Accelerator，张量内存加速器）**。TMA 是一颗独立的硬件 DMA 引擎：当线程发起一次 TMA 拷贝后，地址计算、边界检查、缓存管理与真正的数据搬运全部由硬件在后台完成，线程被彻底解放出来去做计算。学完本讲后，读者应该能够：

- 说清 TMA 与上一讲的 `cp.async`、`mbarrier` 是什么关系，以及它为什么能把「搬运」与「计算」真正重叠起来。
- 在宿主侧用 CUDA Driver API 创建一个 128 字节的 `TmaDescriptor`，并把它作为参数传进内核。
- 在设备侧用 `cp_async_bulk_tensor_*_g2s` / `*_s2g` 发起张量块（tile）的异步拷贝，并用 `mbarrier` 完成同步。
- 用 `cp_async_bulk_tensor_2d_g2s_multicast` 在一个集群（cluster）内把同一块数据**多播**到多个 CTA 的共享内存，理解它如何节省全局显存带宽。
- 跟踪一次 TMA 调用从 `cuda-device` 桩函数 → `dialect-nvvm` op → `mir-lower` LLVM intrinsic/内联 PTX 的完整翻译路径。

本讲承接 u5-l3（异步屏障与异步拷贝）与 u5-l4（集群与分布式共享内存）：TMA 的完成同步复用 u5-l3 的 `mbarrier`，多播则复用 u5-l4 的 cluster 层级。

## 2. 前置知识

在进入源码前，先用三段话建立直觉。

**第一，为什么要 TMA。** 传统 CUDA 拷贝要靠线程：每个线程用 `cp.async` 或普通 load/store 搬一小段数据，整个 warp 都在为搬运服务，地址计算、边界处理也压在 ALU 上。TMA 把这件事交给一颗专用 DMA 引擎——线程只负责「下单」（发一条 `cp.async.bulk.tensor` 指令，告诉硬件描述符地址、tile 坐标、目的共享内存地址），剩下的硬件自己做，完成后通过 mbarrier 自动回报。于是线程可以在硬件搬运的同时做矩阵乘等计算，这就是「搬运-计算重叠」。`cuda-device/src/tma.rs` 顶部的 ASCII 图把这种对比画得很直白（[crates/cuda-device/src/tma.rs:14-28](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-device/src/tma.rs#L14-L28)）。

**第二，描述符（descriptor）是什么。** TMA 不让线程在指令里手写基地址、步长、tile 大小这些布局信息，而是要求宿主预先用 `cuTensorMapEncodeTiled` 把整套布局烘焙成一个 128 字节的**不透明描述符**，内核只拿一个指向它的指针。硬件在搬运时读这个描述符，自行算地址、做越界处理。这样做的好处是：布局只算一次，循环里每次拷贝只换坐标即可，且硬件可以针对描述符做高效寻址。

**第三，完成检测靠 mbarrier。** TMA 是异步的，线程下单后不能立刻读目的共享内存。Hopper 的 `mbarrier`（异步屏障）能同时跟踪「线程到达数」和「事务字节数」（transaction bytes）两个条件，TMA 硬件在 DMA 完成时自动给屏障补上字节数，于是 mbarrier 天然能感知 TMA 的完成——这是 u5-l3 讲过的 `mbarrier_arrive_expect_tx` 的用武之地。

> 名词速查：
> - **tile（块）**：多维张量里一个矩形小份，如 64×64 个 f32。
> - **CTA**：即 thread block；一个 cluster 由多个 CTA 组成。
> - **proxy（代理）**：GPU 上不同的内存访问通路。普通线程走 *generic proxy*，TMA/cp.async 走 *async proxy*，二者之间需要 `fence.proxy.async` 搭桥。
> - **G2S / S2G**：Global→Shared、Shared→Global 的缩写，对应 TMA 的两个方向。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [crates/cuda-device/src/tma.rs](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-device/src/tma.rs) | 设备侧 TMA 用户 API：`TmaDescriptor` 类型 + 一族 `unreachable!()` 桩函数，编译器按函数名识别后翻译。 |
| [crates/cuda-device/src/barrier.rs](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-device/src/barrier.rs) | `Barrier`、`mbarrier_init/arrive/arrive_expect_tx/try_wait`、`fence_proxy_async_shared_cta`——TMA 完成同步的积木（u5-l3 已讲，本讲复用）。 |
| [crates/cuda-device/src/shared.rs](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-device/src/shared.rs) | `SharedArray<T,N,ALIGN>`，第三参数 `ALIGN=128` 专门满足 TMA 目标地址的对齐要求。 |
| [crates/cuda-device/src/cluster.rs](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-device/src/cluster.rs) | `block_rank()`、`cluster_sync()`——多播示例里区分 CTA 与做集群级前置同步（u5-l4 已讲）。 |
| [crates/dialect-nvvm/src/ops/tma.rs](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/dialect-nvvm/src/ops/tma.rs) | TMA 在中间表示层的 op 定义：G2S/S2G/多播/commit_group/wait_group。 |
| [crates/mir-importer/src/translator/terminator/intrinsics/tma.rs](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-importer/src/translator/terminator/intrinsics/tma.rs) | 把 TMA 桩函数调用翻译成 `dialect-nvvm` op 的「翻译机」。 |
| [crates/mir-lower/src/convert/intrinsics/tma.rs](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-lower/src/convert/intrinsics/tma.rs) | 把 `dialect-nvvm` TMA op 降级为 LLVM intrinsic 或内联 PTX 的「转换器」。 |
| [crates/rustc-codegen-cuda/examples/tma_copy/src/main.rs](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/examples/tma_copy/src/main.rs) | sm_90+ 单 CTA 的 TMA 2D 拷贝端到端示例。 |
| [crates/rustc-codegen-cuda/examples/tma_multicast/src/main.rs](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/examples/tma_multicast/src/main.rs) | sm_100a 的 TMA 多播示例：一次 load 喂饱集群内 4 个 CTA。 |

## 4. 核心概念与源码讲解

### 4.1 TMA 张量描述符

#### 4.1.1 概念说明

`TmaDescriptor` 是一个由**宿主创建、设备消费**的不透明 128 字节结构体。它的内容描述了某段全局显存里张量的完整布局——基地址、每个维度的尺寸、相邻维度的步长（字节）、tile 盒子大小、元素步长、交错/缠绕/L2 提升/越界填充等策略。内核只持有一个 `*const TmaDescriptor`，硬件在执行 `cp.async.bulk.tensor` 时自行解码它。

为什么不让线程自己拼这些参数？因为：(1) 布局信息量大（128 字节），每次拷贝都过 `__param` 太浪费；(2) 硬件描述符可以被 TMA 引擎高速解码；(3) 宿主侧 `cuTensorMapEncodeTiled` 会校验布局合法性（如步长必须是 16 字节倍数、tile 边界对齐等），把错误前移到启动前。

#### 4.1.2 核心流程

宿主创建描述符 → `DeviceBuffer::from_host` 把描述符字节也搬到显存 → 取设备指针 `*const TmaDescriptor` → 作为内核参数传入。流程图：

```
host: cuTensorMapEncodeTiled(全局张量基地址, 维度, 步长, tile盒, ...)
        │  写入 CUtensorMap (128B)
        ▼
host: DeviceBuffer::from_host(stream, &tensor_map.opaque[..])
        │  描述符本身也搬到设备显存
        ▼
device: kernel(tensor_map: *const TmaDescriptor, ...)
        │  cp.async.bulk.tensor 用这个指针寻址
        ▼
hardware: TMA 引擎解码描述符 → 计算 tile 地址 → DMA
```

#### 4.1.3 源码精读

设备侧的描述符类型定义（注意对齐 64 字节、128 字节载荷、`Copy+Clone` 但内容不透明）：

```rust
#[repr(C, align(64))]
#[derive(Copy, Clone)]
pub struct TmaDescriptor {
    /// Opaque 128-byte descriptor data (16 x u64)
    _opaque: [u64; 16],
}
```
[crates/cuda-device/src/tma.rs:137-142](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-device/src/tma.rs#L137-L142) —— 这段定义了设备侧「描述符长什么样」。模块文档进一步说明尺寸与对齐：CUDA 12.x 为 128 字节、64 字节对齐，CUDA 13.0+ 为 128 字节对齐（[crates/cuda-device/src/tma.rs:121-142](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-device/src/tma.rs#L121-L142)）。

宿主侧用 Driver API 创建描述符（`tma_copy` 示例的辅助函数）：

```rust
let mut tensor_map = MaybeUninit::<CUtensorMap>::uninit();
let global_dim: [u64; 2] = [width, height];
let global_strides: [u64; 1] = [width * std::mem::size_of::<f32>() as u64];
let box_dim: [u32; 2] = [tile_width, tile_height];
let element_strides: [u32; 2] = [1, 1];

let result = unsafe {
    cuTensorMapEncodeTiled(
        tensor_map.as_mut_ptr(),
        CUtensorMapDataType_enum_CU_TENSOR_MAP_DATA_TYPE_FLOAT32,
        tensor_rank,            // 2
        global_address,         // 设备指针
        global_dim.as_ptr(),    // 张量每个维度大小
        global_strides.as_ptr(),// 维度间步长（字节）
        box_dim.as_ptr(),       // tile 盒子大小
        element_strides.as_ptr(),
        CUtensorMapInterleave_enum_CU_TENSOR_MAP_INTERLEAVE_NONE,
        CUtensorMapSwizzle_enum_CU_TENSOR_MAP_SWIZZLE_NONE,
        CUtensorMapL2promotion_enum_CU_TENSOR_MAP_L2_PROMOTION_NONE,
        CUtensorMapFloatOOBfill_enum_CU_TENSOR_MAP_FLOAT_OOB_FILL_NONE,
    )
};
```
[crates/rustc-codegen-cuda/examples/tma_copy/src/main.rs:425-447](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/examples/tma_copy/src/main.rs#L425-L447)

要点解读：

- `global_dim = [width, height]`：256×256 的张量；`box_dim = [tile_width, tile_height]`：每次拷 64×64 的 tile。
- `global_strides` 只有 *rank−1* 项（这里是 1 项），描述第 1 维（行）起点之间的字节距离——即一行 256 个 f32 = 1024 字节。最内层维度（最稠密那维）步长恒为元素大小，故省略。
- `element_strides = [1,1]`：相邻元素在 tile 内的步长，现代 TMA 几乎总是 1（非 1 会显著降速）。
- 返回值非 `CUDA_SUCCESS` 即报错退出（[crates/rustc-codegen-cuda/examples/tma_copy/src/main.rs:449-451](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/examples/tma_copy/src/main.rs#L449-L451)）。

接着把描述符字节搬到设备，并取其设备指针：

```rust
let dev_tensor_map = DeviceBuffer::from_host(stream, &tensor_map.opaque[..])?;
// ...
let tensor_map_ptr = dev_tensor_map.cu_deviceptr() as *const TmaDescriptor;
```
[crates/rustc-codegen-cuda/examples/tma_copy/src/main.rs:285](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/examples/tma_copy/src/main.rs#L285)、[crates/rustc-codegen-cuda/examples/tma_copy/src/main.rs:301](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/examples/tma_copy/src/main.rs#L301)

> 注意：`CUtensorMap` 与 `TmaDescriptor` 是同一块 128 字节的两种表述——前者是宿主侧 Driver API 的句柄，后者是设备侧的不透明视图。两者 `#[repr(C)]` 布局一致，故可直接转指针。

#### 4.1.4 代码实践

**实践目标**：理解描述符的「盒子里装什么」。

**操作步骤**：

1. 打开 [crates/rustc-codegen-cuda/examples/tma_copy/src/main.rs:418-454](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/examples/tma_copy/src/main.rs#L418-L454) 的 `create_tma_descriptor`。
2. 在 `main` 里把 `TENSOR_WIDTH/HEIGHT` 改成 512×512，`TILE_WIDTH/HEIGHT` 维持 64×64，**只**改这两处常量。
3. 跟着 `global_dim`、`global_strides`、`box_dim` 三行手算：步长应为 512×4=2048 字节。

**需要观察的现象**：步长随 `TENSOR_WIDTH` 线性增长，`box_dim` 与全局尺寸独立——这正是「全局布局」与「每次搬运的 tile 大小」解耦的体现。

**预期结果**：你应当能说清「为什么 `global_strides` 的长度是 rank−1 而不是 rank」：最内层维度步长恒为元素大小，省略。**待本地验证**：在 sm_90+ 机器上 `cargo oxide run tma_copy` 应仍输出 `✓ All 4096 values match!`。

#### 4.1.5 小练习与答案

**练习 1**：把 `box_dim` 设成 `[128, 64]`（宽 128 高 64），但内核里的 `SharedArray<f32, TILE_SIZE>` 仍是 4096。会发生什么？

**答案**：`box_dim` 决定单次 TMA 搬运的字节数 = 128×64×4 = 32768 字节 = 4096 个 f32，与 `SharedArray` 容量恰好匹配，仍能工作。但内核里的 `mbarrier_arrive_expect_tx` 用 `TILE_BYTES` 显式声明了期望字节数（[crates/rustc-codegen-cuda/examples/tma_copy/src/main.rs:60-61](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/examples/tma_copy/src/main.rs#L60-L61)），若改了 box 维度必须同步改 `TILE_BYTES`，否则 mbarrier 永远等不到匹配的事务字节数而死锁。

**练习 2**：为什么 `TmaDescriptor` 标了 `align(64)` 而不是更常见的 16？

**答案**：TMA 硬件要求描述符本身 64 字节对齐（CUDA 13 起 128），且其内部布局对驱动来说是定长的 128 字节；`align(64)` 是与 Driver API 的 `CUtensorMap` 布局约定一致的最小对齐保证。

---

### 4.2 TMA 异步拷贝（G2S / S2G）

#### 4.2.1 概念说明

TMA 拷贝分两个方向：

- **G2S（Global→Shared）**：`cp.async.bulk.tensor.<dim>.shared::cluster.global.tile.mbarrier::complete_tx::bytes`，把全局显存的一块 tile 搬进共享内存，完成后**自动**给指定的 mbarrier 补上事务字节数。这是「加载」路径。
- **S2G（Shared→Global）**：`cp.async.bulk.tensor.<dim>.global.shared::cta.tile`，把共享内存的一块写回全局显存。S2G **不**用 mbarrier，完成靠 `cp.async.bulk.commit_group` + `cp.async.bulk.wait_group` 这对组管理指令（与 u5-l3 讲过的 `cp.async` 的 commit/wait 同源）。

维度支持 1D 到 5D，覆盖向量、矩阵、批量矩阵乃至更高阶张量。cuda-oxide 把每个 (方向, 维度) 组合暴露成一个独立的 `unsafe fn`。

#### 4.2.2 核心流程

一个最小 G2S 的设备侧时序（单 CTA 内）：

```
tid==0: mbarrier_init(BAR, block_size)      // 期望 block_size 个线程到达
tid==0: fence_proxy_async_shared_cta()      // 让 generic proxy 的 init 对 async proxy 可见
all:    sync_threads()                       // 全块看见初始化后的 BAR
tid==0: cp_async_bulk_tensor_2d_g2s(dst, desc, x, y, &BAR)   // 下单 TMA，硬件补字节
tid==0: token = mbarrier_arrive_expect_tx(&BAR, 1, TILE_BYTES) // 线程0到达+声明期望字节
tid>0:  token = mbarrier_arrive(&BAR)        // 其余线程只到达
all:    while !mbarrier_try_wait(&BAR, token) {}  // 等待 (到达数齐 ∧ 字节数齐)
all:    sync_threads()                        // 之后共享内存可安全读
```

关键不变量：mbarrier 的完成条件是「期望到达数」与「期望事务字节」**两者同时满足**。线程侧贡献到达数，TMA 硬件贡献字节数，缺一不可——这就是为什么 `tid==0` 必须 `arrive_expect_tx` 把字节数告诉屏障。

#### 4.2.3 源码精读

设备侧的 2D G2S 桩函数（注意它只是个占位符，真正的工作靠编译器按函数名识别）：

```rust
#[inline(never)]
pub unsafe fn cp_async_bulk_tensor_2d_g2s(
    dst: *mut u8,
    tensor_map: *const TmaDescriptor,
    coord0: i32,
    coord1: i32,
    barrier: *mut Barrier,
) {
    let _ = (dst, tensor_map, coord0, coord1, barrier);
    // Lowered to: @llvm.nvvm.cp.async.bulk.tensor.g2s.tile.2d(...)
    unreachable!("cp_async_bulk_tensor_2d_g2s called outside CUDA kernel context")
}
```
[crates/cuda-device/src/tma.rs:239-250](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-device/src/tma.rs#L239-L250)

文档注释里贴出了对应的 PTX（`cp.async.bulk.tensor.2d.shared::cluster.global.tile.mbarrier::complete_tx::bytes`，见 [crates/cuda-device/src/tma.rs:234-238](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-device/src/tma.rs#L234-L238)）。同族还有 1D/3D/4D/5D（[crates/cuda-device/src/tma.rs:184-194](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-device/src/tma.rs#L184-L194)、[crates/cuda-device/src/tma.rs:340-397](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-device/src/tma.rs#L340-L397)）。

`tma_copy` 示例里完整的内核就是上面时序的真实代码。先看共享内存与屏障的声明：

```rust
const TILE_SIZE: usize = 64 * 64;
const TILE_BYTES: u32 = (TILE_SIZE * 4) as u32;
// TMA destinations require 128-byte alignment
static mut TILE: SharedArray<f32, TILE_SIZE, 128> = SharedArray::UNINIT;
// Barriers use natural alignment (8 bytes for i64)
static mut BAR: Barrier = Barrier::UNINIT;
```
[crates/rustc-codegen-cuda/examples/tma_copy/src/main.rs:60-65](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/examples/tma_copy/src/main.rs#L60-L65)

注意 `SharedArray<f32, TILE_SIZE, 128>` 的第三个泛型 `128`：TMA 目标地址必须 128 字节对齐。`SharedArray` 的文档明确写了这个约定（[crates/cuda-device/src/shared.rs:83-97](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-device/src/shared.rs#L83-L97)），结构体定义见 [crates/cuda-device/src/shared.rs:114-121](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-device/src/shared.rs#L114-L121)。

初始化与下单：

```rust
if tid == 0 {
    unsafe {
        mbarrier_init(&raw mut BAR, block_size);
        // CRITICAL: Fence to make barrier init visible to TMA async proxy!
        fence_proxy_async_shared_cta();
    }
}
thread::sync_threads();

if tid == 0 {
    unsafe {
        cp_async_bulk_tensor_2d_g2s(
            &raw mut TILE as *mut u8,
            tensor_map, tile_x, tile_y, &raw mut BAR,
        );
    }
}
```
[crates/rustc-codegen-cuda/examples/tma_copy/src/main.rs:72-92](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/examples/tma_copy/src/main.rs#L72-L92)

`fence_proxy_async_shared_cta` 是 TMA 模式下**极易漏掉**的一步。它对应 PTX `fence.proxy.async.shared::cta`，作用是让 generic proxy（线程写的 `mbarrier_init`）对 async proxy（TMA 引擎）可见。注释直呼 `CRITICAL`，源码文档解释了为什么——两个 proxy 是分离的内存通路（[crates/cuda-device/src/barrier.rs:525-577](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-device/src/barrier.rs#L525-L577)）。

完成同步：

```rust
let token = unsafe {
    if tid == 0 {
        mbarrier_arrive_expect_tx(&raw const BAR, 1, TILE_BYTES)
    } else {
        mbarrier_arrive(&raw const BAR)
    }
};
unsafe {
    while !mbarrier_try_wait(&raw const BAR, token) {
        // Hardware may briefly suspend thread while waiting
    }
}
```
[crates/rustc-codegen-cuda/examples/tma_copy/src/main.rs:97-110](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/examples/tma_copy/src/main.rs#L97-L110)

- `mbarrier_arrive_expect_tx(&BAR, _tx_count, bytes)`（[crates/cuda-device/src/barrier.rs:415-420](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-device/src/barrier.rs#L415-L420)）：线程 0 既到达、又把「期望 TMA 搬运字节数」告诉屏障。
- `mbarrier_arrive`（[crates/cuda-device/src/barrier.rs:184](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-device/src/barrier.rs#L184)）：其余线程只贡献到达数。
- `mbarrier_try_wait`（[crates/cuda-device/src/barrier.rs:286-290](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-device/src/barrier.rs#L286-L290)）：带调度提示的探测等待，文档称其为「TMA 同步的首选等待操作」。

**翻译路径**。这个 `cp_async_bulk_tensor_2d_g2s` 桩函数最终怎么变成 PTX？三步：

1. mir-importer 识别函数名，调 `emit_tma_g2s(..., dims=2)`，把调用翻译成 `nvvm.cp_async_bulk_tensor_g2s_tile_2d` op。注意它做了**操作数重排**——Rust ABI 是 `(dst, tensor_map, coords..., barrier)`，但 LLVM intrinsic 要求 `(dst, barrier, tensor_map, coords..., cta_mask, cache_hint)`，所以翻译机把 `barrier` 提前、并补上默认的 `cta_mask=0` 与 `cache_hint=0` 两个常量（[crates/mir-importer/src/translator/terminator/intrinsics/tma.rs:95-190](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-importer/src/translator/terminator/intrinsics/tma.rs#L95-L190)）。
2. mir-lower 的 `convert_g2s_impl` 把这个 op 降级为对 LLVM intrinsic `@llvm.nvvm.cp.async.bulk.tensor.g2s.tile.2d` 的 `call`。intrinsic 名按维度拼出（[crates/mir-lower/src/convert/intrinsics/tma.rs:133](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-lower/src/convert/intrinsics/tma.rs#L133)），并把目的地址转到 cluster 共享地址空间（addrspace 7）、屏障地址转到共享地址空间（addrspace 3）（[crates/mir-lower/src/convert/intrinsics/tma.rs:116-117](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-lower/src/convert/intrinsics/tma.rs#L116-L117)）。
3. LLVM NVPTX 后端把 intrinsic 翻成最终的 `cp.async.bulk.tensor.2d...` PTX。

**S2G 方向**则不同：它的桩函数 `cp_async_bulk_tensor_2d_s2g` 不收 barrier（[crates/cuda-device/src/tma.rs:445-454](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-device/src/tma.rs#L445-L454)），完成靠 `cp_async_bulk_commit_group()`（[crates/cuda-device/src/tma.rs:530-534](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-device/src/tma.rs#L530-L534)）与 `cp_async_bulk_wait_group(0)`（[crates/cuda-device/src/tma.rs:565-570](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-device/src/tma.rs#L565-L570)）。mir-lower 里这两个组管理指令走的是**内联 PTX**（`cp.async.bulk.commit_group;` / `cp.async.bulk.wait_group $0;`，带 `~{memory}` clobber），见 [crates/mir-lower/src/convert/intrinsics/tma.rs:33-73](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-lower/src/convert/intrinsics/tma.rs#L33-L73)。

#### 4.2.4 代码实践

**实践目标**：把 G2S 时序里的某一步故意拿掉，观察死锁或读脏数据。

**操作步骤**：

1. 复制 `tma_copy` 示例到自己的实验目录（或在原示例上临时改）。
2. 实验 A（删 fence）：注释掉 [crates/rustc-codegen-cuda/examples/tma_copy/src/main.rs:76](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/examples/tma_copy/src/main.rs#L76) 的 `fence_proxy_async_shared_cta();`。
3. 实验 B（删 expect_tx）：把第 99 行的 `mbarrier_arrive_expect_tx(&raw const BAR, 1, TILE_BYTES)` 改成普通 `mbarrier_arrive(&raw const BAR)`。

**需要观察的现象**：

- 实验 A：TMA 引擎可能看不到 `mbarrier_init` 的结果，信号丢失，`mbarrier_try_wait` 永远不返回真——内核挂死，`stream.synchronize()` 超时。
- 实验 B：屏障只知道「线程到齐」，永远不知道「字节数」，同样永远不翻相位——内核挂死。

**预期结果**：两个实验都应让内核无法正常完成（要么挂死、要么结果全错）。**待本地验证**（需要 sm_90+ 真机，且因为可能挂死，建议在 `cuda oxide run` 外层加超时）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 G2S 用 mbarrier，而 S2G 用 commit_group/wait_group？

**答案**：G2S 的目标是共享内存，等待方是本块的多个线程，用 mbarrier 可以让「所有线程」一起等到搬运完成，且 mbarrier 的 `complete_tx::bytes` 机制天然对接 TMA 的事务字节。S2G 的目标是全局显存，等待方通常是发起写的同一组线程（之后还要继续发起新的 S2G），用 commit/wait 的「组」模型更轻量，不必在共享内存里放屏障。

**练习 2**：`mbarrier_arrive_expect_tx` 的第二个参数 `_tx_count`（值为 1）在示例里被下划线忽略，它原本语义是什么？

**答案**：从签名 `mbarrier_arrive_expect_tx(bar, _tx_count: u32, bytes: u32)`（[crates/cuda-device/src/barrier.rs:415](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-device/src/barrier.rs#L415)）看，`_tx_count` 应是「期望的事务笔数」，`bytes` 是「期望的总字节数」。当前实现把它忽略、只下推 `bytes`，对应 PTX `mbarrier.arrive.expect_tx.shared.b64 token, [addr], bytes`（[crates/cuda-device/src/barrier.rs:412](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-device/src/barrier.rs#L412)），即硬件按字节数判定完成。

---

### 4.3 TMA 多播（Multicast）

#### 4.3.1 概念说明

多播是 TMA 在集群（cluster）上的杀手锏：**一次** `cp.async.bulk.tensor.2d...multicast::cluster` 指令，把同一块 tile 同时投递到集群内**多个** CTA 的共享内存。这在 GEMM 里极有价值——同一片 A 或 B tile 往往要被多个 CTA 读取，传统做法是每个 CTA 各自从全局显存搬一次，浪费带宽；多播让它们共享一次全局读。

多播指令多了一个 `cta_mask`（u16）操作数：第 *i* 位置 1 表示「投递给集群内 rank *i* 的 CTA」。例如 `0b1111` = 投递给 rank 0–3。

硬件阶梯：
- 基础 TMA：sm_90（Hopper）。
- TMA **多播**：sm_100a（Blackwell 数据中心，B100/B200/GB200）。**不**支持消费级 Blackwell（sm_120）也不支持 Hopper（sm_90）——这点示例顶部的注释强调过（[crates/rustc-codegen-cuda/examples/tma_multicast/src/main.rs:11-20](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/examples/tma_multicast/src/main.rs#L11-L20)）。

#### 4.3.2 核心流程

`tma_multicast` 示例的时序（集群 = 4 个 CTA）：

```
every CTA tid==0: mbarrier_init(本CTA的 BAR, block_size) + fence
every CTA:        sync_threads()
all CTAs:         cluster_sync()          // 关键：所有 CTA 的 BAR 必须先就绪
all threads:      token = (tid==0) ? arrive_expect_tx(BAR,1,BYTES) : arrive(BAR)
rank0 tid==0:     cp_async_bulk_tensor_2d_g2s_multicast(dst, desc, x, y, &BAR, 0b1111)
                                        // 一次下单，4 个 CTA 的共享内存都收到
every CTA:        while !mbarrier_try_wait(&BAR, token) {}   // 各自等自己的 BAR
every CTA:        sync_threads()
every CTA:        把本地 TILE 写到 out 的不重叠区段，供宿主校验
```

关键点：**只有 rank 0、tid 0 一条线程真正下单**，但因为多播，每个 CTA 的 `TILE` 都被填好；每个 CTA 各自等自己的 `BAR`——多播指令会向掩码里的每个 CTA 的屏障都补上事务字节。

为什么多播前要先 `cluster_sync()`？因为指令要写「所有目标 CTA」的共享内存并信号它们的屏障，所以所有 CTA 的 `BAR` 必须先完成 `mbarrier_init`，否则某 CTA 的屏障还没就绪，TMA 给它信号就会丢失。

#### 4.3.3 源码精读

多播内核的声明，注意 `#[cluster_launch(4,1,1)]`——这个属性告诉宏「本内核按 4-CTA 集群启动」：

```rust
#[kernel]
#[cluster_launch(4, 1, 1)]
pub fn tma_multicast_test(
    tensor_map: *const TmaDescriptor,
    mut out: DisjointSlice<f32>,
    tile_x: i32,
    tile_y: i32,
) { ... }
```
[crates/rustc-codegen-cuda/examples/tma_multicast/src/main.rs:56-63](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/examples/tma_multicast/src/main.rs#L56-L63)

每个 CTA 自己初始化屏障，然后全集群同步：

```rust
let rank = cluster::block_rank();
if tid == 0 {
    unsafe {
        mbarrier_init(&raw mut BAR, block_size);
        fence_proxy_async_shared_cta();
    }
}
thread::sync_threads();
// Cluster-wide barrier: all CTAs must have their mbarrier initialized
// before the multicast TMA fires.
cluster::cluster_sync();
```
[crates/rustc-codegen-cuda/examples/tma_multicast/src/main.rs:69-85](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/examples/tma_multicast/src/main.rs#L69-L85)

`cluster::block_rank()`（[crates/cuda-device/src/cluster.rs:189](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-device/src/cluster.rs#L189)）返回当前 CTA 在集群内的秩；`cluster::cluster_sync()`（[crates/cuda-device/src/cluster.rs:241](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-device/src/cluster.rs#L241)）是要求所有 CTA 到达的集群阻塞屏障——这正是 u5-l4 讲过的集群层级屏障，此处用来保证多播下单前所有目标 `BAR` 就绪。

下单：

```rust
if rank == 0 && tid == 0 {
    let cta_mask: u16 = 0b1111; // deliver to all 4 CTAs
    unsafe {
        cp_async_bulk_tensor_2d_g2s_multicast(
            &raw mut TILE as *mut u8,
            tensor_map, tile_x, tile_y, &raw mut BAR, cta_mask,
        );
    }
}
```
[crates/rustc-codegen-cuda/examples/tma_multicast/src/main.rs:96-109](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/examples/tma_multicast/src/main.rs#L96-L109)

设备侧 API 与普通 2D G2S 几乎一样，只多了一个 `cta_mask: u16` 参数（[crates/cuda-device/src/tma.rs:271-282](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-device/src/tma.rs#L271-L282)）。文档注释贴出的 PTX 多了 `multicast::cluster` 限定符与 `cta_mask` 操作数（[crates/cuda-device/src/tma.rs:259-264](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-device/src/tma.rs#L259-L264)）。

宿主侧启动同样用 raw `LaunchConfig`（包在 `unsafe` 里）：

```rust
let cfg = LaunchConfig {
    grid_dim: (CLUSTER_SIZE as u32, 1, 1),
    block_dim: (block_size, 1, 1),
    shared_mem_bytes: 0,
};
// SAFETY: launch shape/resources match the kernel; buffers cover its accesses.
unsafe {
    module.tma_multicast_test((stream).as_ref(), cfg, tensor_map_ptr, &mut dev_output, tile_x, tile_y)
}?;
```
[crates/rustc-codegen-cuda/examples/tma_multicast/src/main.rs:230-248](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/examples/tma_multicast/src/main.rs#L230-L248)

> 这里 `grid_dim.0 = CLUSTER_SIZE = 4` 与 `#[cluster_launch(4,1,1)]` 一致；集群形状由宏注入的编译期标记决定，mir-importer 据此产出 `.reqnctapercluster`（详见 u5-l4）。raw 配置启动本身是 `unsafe` 的——形状/资源匹配由调用方自证（这是 #318 的安全边界，u1-l4/u2-l4 已述）。

**翻译路径的不同点**。普通 G2S 在 mir-importer 里把 `cta_mask` 硬编码为常量 0；多播则把用户传入的 `cta_mask` 作为真实操作数翻译，并选用 `CpAsyncBulkTensorG2sTile2dMulticastOp`（[crates/mir-importer/src/translator/terminator/intrinsics/tma.rs:375-529](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-importer/src/translator/terminator/intrinsics/tma.rs#L375-L529)）。到了 mir-lower，两者**共用同一条 LLVM intrinsic** `@llvm.nvvm.cp.async.bulk.tensor.g2s.tile.2d`，区别只在调用时给 intrinsic 的 `use_cta_mask` 立即数置真——`convert_g2s_impl` 第 143 行 `let use_cta_mask = create_i1_const(ctx, rewriter, multicast);`（[crates/mir-lower/src/convert/intrinsics/tma.rs:143](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-lower/src/convert/intrinsics/tma.rs#L143)）。NVPTX 后端看到 `use_cta_mask=true` 就会吐出 `multicast::cluster` 限定符。dialect 层的注释也讲清了这一点（[crates/dialect-nvvm/src/ops/tma.rs:117-135](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/dialect-nvvm/src/ops/tma.rs#L117-L135)）。

还有一条 `cg2`（cta_group::2）变体 `cp_async_bulk_tensor_2d_g2s_multicast_cg2`（[crates/cuda-device/src/tma.rs:306-317](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-device/src/tma.rs#L306-L317)），面向 TPC 配对的硬件级跨 CTA 协调，与 `tcgen05` 配合使用——它对应独立的 dialect op（[crates/dialect-nvvm/src/ops/tma.rs:174-187](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/dialect-nvvm/src/ops/tma.rs#L174-L187)）与独立的 lowerer `convert_g2s_multicast_cg2`（[crates/mir-lower/src/convert/intrinsics/tma.rs:160-167](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-lower/src/convert/intrinsics/tma.rs#L160-L167)）。

#### 4.3.4 代码实践

**实践目标**：在不改多播指令的前提下，通过改 `cta_mask` 观察「哪些 CTA 收到数据」。

**操作步骤**：

1. 打开 [crates/rustc-codegen-cuda/examples/tma_multicast/src/main.rs:98](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/examples/tma_multicast/src/main.rs#L98)，把 `let cta_mask: u16 = 0b1111;` 改成 `let cta_mask: u16 = 0b0101;`（只投递给 rank 0 和 rank 2）。
2. 读 [crates/rustc-codegen-cuda/examples/tma_multicast/src/main.rs:258-280](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/examples/tma_multicast/src/main.rs#L258-L280) 的校验循环，预测哪几个 CTA 的输出会变成 0（未收到 tile）。

**需要观察的现象**：rank 0 和 rank 2 的输出段正确，rank 1 和 rank 3 的输出段保持 `zeroed` 初始的 0；不过它们的 `mbarrier_try_wait` 仍在等待——因为它们的 `BAR` 被自己 `arrive` 了却没收到 TMA 字节，**会死锁**。

**预期结果**：直接改掩码会死锁（被跳过的 CTA 永远等不到字节）。这个实验反过来说明：`cta_mask` 必须覆盖所有已经 `arrive_expect_tx`/`arrive` 并 `try_wait` 的 CTA。**待本地验证**（sm_100a 真机；若无机可在 sm_90 上读 `tma_copy` 替代）。

#### 4.3.5 小练习与答案

**练习 1**：多播示例里只有 rank 0 下单，为什么 rank 1/2/3 也能在自己的 `TILE` 里读到数据？

**答案**：因为 `cp.async.bulk.tensor.2d...multicast::cluster` 是硬件多播——TMA 引擎读一次全局显存，按 `cta_mask` 把同一份数据 DMA 到掩码里每个 CTA 的共享内存，并向每个 CTA 的 `BAR` 各自补上事务字节。所以「下单一次」≠「搬运一次」，每个目标 CTA 都独立收到。

**练习 2**：`cta_mask` 是 u16，最多表示 16 个 CTA。这暗示集群大小的上限是多少？

**答案**：Hopper/Blackwell 的 cluster 大小上限是 8 个 CTA（GPC 限制），u16 的 16 位是 PTX ISA 预留的位宽，远大于实际集群上限，所以位宽不是瓶颈、集群的 GPC 约束才是。

---

### 4.4 完成同步：mbarrier + commit/wait + proxy fence

#### 4.4.1 概念说明

TMA 的「完成」有三套互不相干的机制，分别对应不同场景：

| 场景 | 同步机制 | 关键 API |
| --- | --- | --- |
| G2S，多线程等同一块搬运 | mbarrier 的 `complete_tx::bytes` | `mbarrier_arrive_expect_tx` + `mbarrier_try_wait` |
| S2G，写回全局 | commit/wait 组管理 | `cp_async_bulk_commit_group` + `cp_async_bulk_wait_group` |
| 任何 TMA/cp.async | generic↔async proxy 桥接 | `fence_proxy_async_shared_cta` |

第三项不是「等完成」，而是「让 generic proxy 的写对 async proxy 可见」——它必须在 `mbarrier_init` 之后、TMA 下单之前出现，否则 TMA 引擎可能看到一个未初始化的屏障。这三者共同构成 TMA 的同步语义。

#### 4.4.2 核心流程

把三种同步放进同一条时间轴（以 G2S 为例）：

```
       generic proxy                 async proxy
T0: mbarrier_init(BAR)  ──写──┐
T0: fence_proxy_async ────────┼──搭桥──►  BAR 可见
all: sync_threads             │
T0: cp.async.bulk.tensor ─────┼──────────► 下单 DMA
T0: arrive_expect_tx(BAR,B) ──┘           │ 完成时硬件补字节
all: try_wait(BAR, token) ◄───────────── 完成
all: sync_threads                          现在可读 TILE
```

mbarrier 完成的布尔条件可形式化为：

\[ \text{complete} \iff (\text{arrived} \geq \text{expected\_arrivals}) \;\land\; (\text{tx\_bytes\_received} \geq \text{expected\_tx\_bytes}) \]

两个条件由不同主体贡献：线程贡献 `arrived`，TMA 硬件贡献 `tx_bytes_received`。任一条件不满足，`try_wait` 返回 false，线程继续自旋。

#### 4.4.3 源码精读

`Barrier` 类型本身是 8 字节硬件状态（[crates/cuda-device/src/barrier.rs:87-92](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-device/src/barrier.rs#L87-L92)）。三个核心桩函数：

```rust
pub unsafe fn mbarrier_init(bar: *mut Barrier, expected_count: u32) { ... unreachable!() }
pub unsafe fn mbarrier_arrive(bar: *const Barrier) -> u64 { ... unreachable!() }
pub unsafe fn mbarrier_arrive_expect_tx(bar: *const Barrier, _tx_count: u32, bytes: u32) -> u64 { ... unreachable!() }
pub unsafe fn mbarrier_try_wait(bar: *const Barrier, token: u64) -> bool { ... unreachable!() }
```
[crates/cuda-device/src/barrier.rs:142](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-device/src/barrier.rs#L142)、[crates/cuda-device/src/barrier.rs:184](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-device/src/barrier.rs#L184)、[crates/cuda-device/src/barrier.rs:415-420](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-device/src/barrier.rs#L415-L420)、[crates/cuda-device/src/barrier.rs:286-290](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-device/src/barrier.rs#L286-L290)

`mbarrier_try_wait` 的文档明确说它是「TMA 同步的首选等待操作」，对应 PTX `mbarrier.try_wait.shared.b64 pred, [addr], token;`——相比 `test_wait`，它给硬件一个调度提示「这个线程可能要等一会，可以先挂起」，省得空转烧电（[crates/cuda-device/src/barrier.rs:247-289](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-device/src/barrier.rs#L247-L289)）。

proxy fence 的注释解释了为什么它「CRITICAL」：

> NVIDIA GPUs have separate memory "proxies": Generic Proxy（普通线程访存）与 Async Proxy（TMA/cp.async）。没有这道 fence，generic proxy 的 `mbarrier.init` 写可能对 async proxy 不可见，TMA 给屏障信号时就会丢失。
> [crates/cuda-device/src/barrier.rs:550-577](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-device/src/barrier.rs#L550-L577)

组管理（S2G 专用）的内联 PTX 在 mir-lower 里这样生成：

```rust
// commit_group
inline_asm_convergent(ctx, rewriter, void_ty.into(), vec![],
    "cp.async.bulk.commit_group;", "~{memory}");

// wait_group
let asm = if is_read { "cp.async.bulk.wait_group.read $0;" } else { "cp.async.bulk.wait_group $0;" };
inline_asm_convergent(ctx, rewriter, void_ty.into(), vec![n], asm, "n,~{memory}");
```
[crates/mir-lower/src/convert/intrinsics/tma.rs:33-73](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/mir-lower/src/convert/intrinsics/tma.rs#L33-L73)

注意两点：(1) 两条指令都带 `~{memory}` clobber，告诉编译器「这会动内存，别把前后访存重排到它两边」；(2) `wait_group` 用 `$0` 占位符把「至多剩余 N 个组」的 N 内联进去，约束串里的 `n` 表示该操作数是立即数。

dialect-nvvm 层把整套 TMA 工作流总结成 7 步（host 建描述符 → init → fence → arrive_expect_tx → 下单 → commit → try_wait），见 [crates/dialect-nvvm/src/ops/tma.rs:24-34](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/dialect-nvvm/src/ops/tma.rs#L24-L34)，与本讲的时序一致。

#### 4.4.4 代码实践

**实践目标**：把 `mbarrier_try_wait` 自旋换成阻塞性的 `mbarrier_wait`，对比行为。

**操作步骤**：

1. 阅读 [crates/cuda-device/src/barrier.rs:286-290](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-device/src/barrier.rs#L286-L290)（`mbarrier_try_wait`）与 [crates/cuda-device/src/barrier.rs:358](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-device/src/barrier.rs#L358)（`mbarrier_wait`）的文档。
2. 在 `tma_copy` 内核里把：
   ```rust
   unsafe { while !mbarrier_try_wait(&raw const BAR, token) {} }
   ```
   换成：
   ```rust
   unsafe { mbarrier_wait(&raw const BAR, token) }
   ```
3. 重新 `cargo oxide run tma_copy`。

**需要观察的现象**：功能上仍正确（结果匹配），但阻塞版让硬件无法在等待期间调度别的 warp 占用该 SM，吞吐略低；自旋的 `try_wait` 给了硬件挂起提示，更适合 TMA 这种「等一会就好」的场景。

**预期结果**：两种写法在 `tma_copy` 这种单次拷贝上结果一致；在「搬运-计算重叠」的双缓冲流水线里，`try_wait` 才是正解。**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：`commit_group` / `wait_group` 的参数 `n` 取 0 是什么含义？

**答案**：`wait_group(0)` 表示「等到所有已 commit 的组都不再挂起」才返回，即等全部 S2G 完成。`wait_group(N)` 则允许至多还有 N 个组未完成，用于多缓冲：发起多组写、只等到只剩 N 组，从而让写与写重叠（[crates/cuda-device/src/tma.rs:536-570](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-device/src/tma.rs#L536-L570)）。

**练习 2**：`fence_proxy_async_shared_cta` 能不能放在 `sync_threads` 之后、下单之前？为什么示例偏偏放在 `mbarrier_init` 紧后面？

**答案**：放在 `sync_threads` 之后、下单之前也「能工作」（fence 的语义是「让 fence 之前的 generic proxy 写对 async proxy 可见」），但语义上更精确的位置是紧贴 `mbarrier_init`——因为 fence 要保护的就是 init 那条写。示例把 fence 关进 `if tid == 0` 块里紧跟 init，是让「init 与 fence 同属一次 generic proxy 写序列」这个意图最清楚，也避免被误删（[crates/rustc-codegen-cuda/examples/tma_copy/src/main.rs:72-78](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/examples/tma_copy/src/main.rs#L72-L78)）。

## 5. 综合实践

把本讲的四个模块串起来，设计一个「TMA 多缓冲加载器」骨架（源码阅读 + 设计型，不强求跑通）：

**任务**：参考 `tma_copy` 与 `tma_multicast`，写一个**伪代码设计**（标注你引用的真实 API 与行号），描述一个内核：用两个 tile 缓冲 `TILE0` / `TILE1`，循环 K 次，每次让 TMA 把下一块搬进「另一个」缓冲，同时线程对「当前」缓冲做计算，靠 mbarrier 做生产-消费同步。

**要求覆盖**：

1. **描述符**：列出 `create_tma_descriptor` 的参数选择（全局尺寸、tile 尺寸）——参考 [crates/rustc-codegen-cuda/examples/tma_copy/src/main.rs:418-454](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/examples/tma_copy/src/main.rs#L418-L454)。
2. **共享内存声明**：两个 `SharedArray<f32, TILE_SIZE, 128>`（128 对齐）与两个 `Barrier`——参考 [crates/rustc-codegen-cuda/examples/tma_copy/src/main.rs:60-65](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/examples/tma_copy/src/main.rs#L60-L65)。
3. **初始化时序**：`mbarrier_init` + `fence_proxy_async_shared_cta` + `sync_threads`——参考 [crates/rustc-codegen-cuda/examples/tma_copy/src/main.rs:72-79](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/examples/tma_copy/src/main.rs#L72-L79)。
4. **预热**：循环开始前先 `cp_async_bulk_tensor_2d_g2s` 第一块到 `TILE0`。
5. **循环体**：下单第 k+1 块到 `TILE{(k+1)%2}`、`arrive_expect_tx`、`try_wait` 等 `TILE{k%2}` 完成、计算 `TILE{k%2}`。
6. **收尾**：等最后一块、计算之。

**交付物**：一份带行号引用的伪代码 + 一段说明，指出哪些位置若漏掉会死锁（fence、expect_tx、try_wait 的目标缓冲与下单的目标缓冲必须配套）。

**参考思路**：双缓冲的关键不变量是「下单的目标缓冲」与「try_wait 的屏障」必须一一对应，且第一次 try_wait 必须在第一次下单之后。把这套写对，就掌握了 TMA 在真实 GEMM 流水线里的用法。

## 6. 本讲小结

- TMA 是 Hopper（sm_90+）的硬件 DMA 引擎，线程只「下单」（描述符 + tile 坐标），搬运、寻址、越界处理全由硬件后台完成，从而释放线程做计算。
- 张量布局烘焙成宿主创建的 128 字节 `TmaDescriptor`（`cuTensorMapEncodeTiled`），内核只持 `*const TmaDescriptor`；`CUtensorMap` 与 `TmaDescriptor` 是同一段内存的宿主/设备两种视图。
- G2S 用 mbarrier 的 `complete_tx::bytes` 做完成检测：线程贡献到达数、TMA 硬件贡献事务字节，两者齐才翻相位；`fence_proxy_async_shared_cta` 是让 `mbarrier_init` 跨 generic/async proxy 可见的必备桥。
- S2G 不用 mbarrier，改用 `commit_group` / `wait_group(0)` 组管理，对应内联 PTX。
- 多播（sm_100a）让一次 G2S 同时投递到集群内多个 CTA 的共享内存，`cta_mask` 选目标；下单前必须 `cluster_sync` 保证所有目标 CTA 的屏障已就绪。
- 翻译链路：`cuda-device` 的 `unreachable!()` 桩 → mir-importer 翻成 `dialect-nvvm` 的 `cp_async_bulk_tensor_*` op（普通版硬编码 cta_mask=0，多播版带真实掩码）→ mir-lower 降级为 LLVM intrinsic（`use_cta_mask` 立即数控制是否 emit `multicast::cluster`）或内联 PTX（commit/wait）。

## 7. 下一步学习建议

- **阅读 mma/wgmma/tcgen05**：TMA 的真正用武之地是喂矩阵乘加速器。建议接着学 u5-l6（矩阵乘加速器），看 `gemm`/`tcgen05` 示例如何把 TMA 加载与 wgmma 计算组成流水线。
- **读 dialect-nvvm/src/ops/tma.rs 全文**：[crates/dialect-nvvm/src/ops/tma.rs](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/dialect-nvvm/src/ops/tma.rs) 把 1D–5D、G2S/S2G、多播、cg2、commit/wait 全部 op 列在一起，是查「某个组合存在与否」的最佳索引。
- **跟踪一条完整 lowering**：仿照 u6-l3 的方法，从 `cp_async_bulk_tensor_2d_g2s_multicast` 出发，依次读 mir-importer 的 `emit_tma_g2s_multicast`、mir-lower 的 `convert_g2s_impl`，验证 `use_cta_mask` 立即数是如何从 dialect op 一路传到 NVPTX backend 的。
- **扩展练习**：仿照 u6-l4 的「新增 intrinsic 模板」，规划把一个 TMA 目前未覆盖的 PTX 变体（如 `cp.async.bulk.tensor.g2s` 的 `evict_first` 缓存提示版本）落地到 cuda-device → dialect-nvvm → mir-importer → mir-lower 四层。
