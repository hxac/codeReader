# 集群与分布式共享内存

## 1. 本讲目标

本讲聚焦 NVIDIA Hopper（sm_90+）引入的 **Thread Block Cluster（线程块集群）** 层级，以及它带来的最重磅能力：**分布式共享内存（Distributed Shared Memory，DSMEM）**。

读完本讲，你应当能够：

1. 在 `Grid → Cluster → Block → Warp → Thread` 的执行层级中正确安放 cluster，并说出「同集群的 block 必须共驻在同一 GPC 上」这一硬件约束的含义。
2. 用 `cluster::cluster_ctaidX/Y/Z`、`cluster::cluster_nctaidX/Y/Z`、`block_rank()`、`cluster_size()` 等内建函数读取集群坐标，并用 `#[cluster_launch(x,y,z)]` 在编译期固定集群形状。
3. 用 `cluster::dsmem_read_u32`（而不是 `map_shared_rank` 后解引用）跨 block 读取邻居的共享内存，并解释为什么后者会触发 `CUDA_ERROR_ILLEGAL_ADDRESS`。
4. 用 `cluster::cluster_sync()` 做集群级屏障，并把它与 u5-l3 学过的 `mbarrier` 异步屏障组合成「多播（mcast）」同步协议。
5. 说出 `#[launch_contract]` / `#[cluster_launch]` 如何在 `prepare_*` 路径里校验集群轴，以及 `LaunchContractError` 的几个集群专属变体各自拦截什么错误。

## 2. 前置知识

本讲是「高级设备能力」单元的第 4 讲，默认你已经掌握：

- **共享内存与块级屏障**（u2-l3）：`SharedArray<T,N>`、`DynamicSharedArray`、`sync_threads()`、`threadfence`。本讲的 DSMEM 本质上就是把「per-block 私有的共享内存」打开给邻居 block 看，所以你必须先理解普通共享内存。
- **mbarrier 异步屏障与 cp.async**（u5-l3）：`mbarrier_init`、`mbarrier_arrive_cluster`、`mbarrier_try_wait_parity`、`fence_proxy_async_shared_cta`。本讲的「多播」协议正是用集群范围的 mbarrier 把多个 block 的异步到达汇合到一个屏障上。
- **线程索引与越界安全**（u2-l2）：`DisjointSlice`、`ThreadIndex`。集群示例的内核签名仍然用 `DisjointSlice<u32>` 作为输出。
- **启动内核：raw 配置与类型化启动契约**（u2-l4）：raw `LaunchConfig` 是不安全的、`#[launch_contract]` 经 `prepare_*` 产出 `PreparedLaunch` 受检启动。本讲末尾会把集群轴的校验挂回这套契约。

如果你对「cluster 是什么」完全没有概念，只需先记住一句话：**cluster 是把若干个 block 强行绑定到同一组 SM（同一 GPC）上一起调度，使它们能直接读对方的共享内存——而不必绕道全局显存。**

> 硬件门槛：cluster 与 DSMEM **要求 sm_90（Hopper H100/H200）或更新**（Blackwell sm_100/120 进一步扩展）。cluster.rs 的模块文档把这一点钉在最顶部：[crates/cuda-device/src/cluster.rs:7](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-device/src/cluster.rs#L7)。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [crates/cuda-device/src/cluster.rs](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-device/src/cluster.rs) | 集群全部设备端 intrinsic 的「桩（stub）」定义：坐标读取、`cluster_sync`、`map_shared_rank`、`dsmem_read_u32`、`__cluster_config` 编译期标记。本讲的主战场。 |
| [crates/rustc-codegen-cuda/examples/cluster/src/main.rs](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/examples/cluster/src/main.rs) | 5 个测试内核 + 宿主：编译期集群配置、集群坐标、集群同步、DSMEM 环形交换、DSMEM 归约。 |
| [crates/rustc-codegen-cuda/examples/mcast_barrier_test/src/main.rs](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/examples/mcast_barrier_test/src/main.rs) | 集群多播屏障协议的最小测试：4 个 CTA 各自 arrive 到 rank 0 的 mbarrier，rank 0 用奇偶等待收尾。 |
| [crates/cuda-macros/src/lib.rs](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-macros/src/lib.rs) | `#[cluster_launch]` 过程宏：在内核体首部注入 `__cluster_config::<X,Y,Z>()` 标记；签约内核生成 `.with_cluster(...)`。 |
| [crates/cuda-core/src/launch.rs](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-core/src/launch.rs) | `LaunchContractSpec` 的 `cluster` 字段与一整套 `Cluster*` 校验错误，以及 `__prepare` 里对活设备的集群校验链。 |

补充说明：`cluster` 模块在 [crates/cuda-device/src/lib.rs:20](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-device/src/lib.rs#L20) 处 `pub mod cluster;` 导出，而 `cluster_launch` 宏则在 [crates/cuda-device/src/lib.rs:10](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-device/src/lib.rs#L10) 处从 `cuda_macros` 转出，故示例里 `use cuda_device::{cluster, cluster_launch, ...}` 一站式导入。

## 4. 核心概念与源码讲解

### 4.1 Thread Block Cluster：grid 与 block 之间的新层级

#### 4.1.1 概念说明

传统 CUDA 只有 `Grid → Block → Warp → Thread` 四级。Hopper 在 `Grid` 与 `Block` 之间插入了第五级 **Cluster**。cluster.rs 顶部的 ASCII 图把层级画得很清楚：

```text
Grid
└── Thread Block Cluster (NEW - sm_90+)
    └── Thread Block
        └── Warp
            └── Thread
```

参见 [crates/cuda-device/src/cluster.rs:18-24](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-device/src/cluster.rs#L18-L24)。

为什么需要它？普通 CUDA 里，每个 block 的共享内存是**私有**的，block 之间要交换数据只能绕道全局显存（慢、占带宽）。cluster 把最多 **8 个 block** 强制绑定到同一个 GPC（GPU Processing Cluster，含若干 SM）上协同调度，于是这些 block 的共享内存被「拼接」成一块逻辑上连续的 **分布式共享内存**，邻居 block 的 smem 可以用一条 `mapa` 指令直接寻址。最大集群规模约束是：

\[
\text{clusterDimX} \times \text{clusterDimY} \times \text{clusterDimZ} \le 8
\]

参见 [crates/cuda-device/src/cluster.rs:33-36](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-device/src/cluster.rs#L33-L36)。（注：宏侧的 doc 注释里写了「typically 16」，那是更宽松的硬件上界；以 cluster.rs 这条 ≤ 8 为准。）

#### 4.1.2 核心流程

cluster 在两个层面引入坐标，恰好和「grid/block 的坐标」对称：

| 维度 | grid/block 视角 | cluster 视角 | 含义 |
|------|----------------|-------------|------|
| 我是谁 | `threadIdx.x`（线程在 block 内） | `cluster_ctaidX()`（block 在 cluster 内） | 当前 block 在集群里的坐标 |
| 我有多大 | `blockDim.x`（block 的线程数） | `cluster_nctaidX()`（cluster 的 block 数） | 集群在该轴的尺寸 |
| 我属于谁 | `blockIdx.x`（block 在 grid 内） | `cluster_idx()`（cluster 在 grid 内） | 本 block 所属集群在 grid 里的线性号 |
| 总数 | `gridDim.x` | `num_clusters()` | grid 里的集群总数 |

由此派生出两个最常用的辅助量：

- 当前 block 在集群内的**线性秩**（linear rank）：
  \[
  \text{rank} = x + y \cdot n_x + z \cdot n_x \cdot n_y
  \]
- 集群的**总 block 数**：
  \[
  \text{size} = n_x \cdot n_y \cdot n_z
  \]

`rank` 是后续 DSMEM 寻址的「目标 block 编号」。

要真正启用 cluster，内核必须在编译期带上集群形状声明，让 PTX 入口带 `.reqnctapercluster X,Y,Z` 与 `.explicitcluster`。cuda-oxide 用 `#[cluster_launch(x,y,z)]` 属性宏完成这件事——它不改运行时逻辑，只往函数体首部塞一个编译期标记调用，由 mir-importer 识别后转成 LLVM metadata。完整流程见下一节的源码精读。

#### 4.1.3 源码精读

**坐标 intrinsic**。每个坐标读取都是 `#[inline(never)]` 的桩函数，函数体只有 `unreachable!(...)`——它不是给 CPU 跑的，而是给编译器「按名字识别」的占位符，最终由 mir-importer 译成 `dialect-nvvm` op、mir-lower 降级为读取 PTX 特殊寄存器。以 X 轴为例：

```rust
/// Lowers to: `mov.u32 %r, %cluster_ctaid.x`
#[inline(never)]
pub fn cluster_ctaidX() -> u32 {
    unreachable!("cluster_ctaidX called outside CUDA kernel context")
}
```

见 [crates/cuda-device/src/cluster.rs:80-83](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-device/src/cluster.rs#L80-L83)。`cluster_ctaidY/Z` 同构（[L92-95](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-device/src/cluster.rs#L92-L95)、[L104-107](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-device/src/cluster.rs#L104-L107)）；尺寸 `cluster_nctaidX/Y/Z` 见 [L121-144](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-device/src/cluster.rs#L121-L144)。

**派生 helper**。`block_rank()` 与 `cluster_size()` 是 `#[inline(always)]` 的纯 Rust 组合子，把上面的公式直接写出来：

```rust
#[inline(always)]
pub fn block_rank() -> u32 {
    let x = cluster_ctaidX();
    let y = cluster_ctaidY();
    let z = cluster_ctaidZ();
    let nx = cluster_nctaidX();
    let ny = cluster_nctaidY();
    x + y * nx + z * nx * ny
}
```

见 [crates/cuda-device/src/cluster.rs:188-196](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-device/src/cluster.rs#L188-L196)；`cluster_size()` 见 [L201-204](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-device/src/cluster.rs#L201-L204)。注意 `block_rank()` 没用 `nz/nx*ny` 那一项——线性秩只需要在已知 `x,y,z` 与 `nx,ny` 的情况下唯一确定，与 `nz` 无关，因为 `z < nz` 已经由 `cluster_ctaidZ()` 的取值范围保证。

**编译期集群标记**。`#[cluster_launch]` 宏的本质工作只是在函数体最前面插一句话：

```rust
let marker_call: syn::Stmt = syn::parse_quote! {
    ::cuda_device::cluster::__cluster_config::<#x, #y, #z>();
};
input.block.stmts.insert(0, marker_call);
```

见 [crates/cuda-macros/src/lib.rs:4563-4569](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-macros/src/lib.rs#L4563-L4569)。被调用的 `__cluster_config` 同样是个空体函数（[crates/cuda-device/src/cluster.rs:407-412](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-device/src/cluster.rs#L407-L412)），它的全部意义在于「带着三个 const generic 参数出现在 MIR 里」。mir-importer 扫描到 `cluster::__cluster_config` 这条调用（匹配逻辑见 [crates/cuda-macros/src/lib.rs:3715](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-macros/src/lib.rs#L3715)），抽出 `X,Y,Z`，写成 `!nvvm.annotations` 里的 `cluster_dim_x/y/z` 元数据，LLVM NVPTX 后端据此发出：

```ptx
.entry my_cluster_kernel
    .explicitcluster
    .reqnctapercluster 4, 1, 1
```

**示例：把集群坐标写回全局内存**。cluster 示例的 `test_cluster_intrinsics` 把 8 个集群特殊寄存器（含派生的 `block_rank`、`cluster_size`）按线程号映射到输出缓冲，是最直白的「集群坐标自检」：

```rust
let value = match tid {
    0 => cluster::cluster_ctaidX(),
    1 => cluster::cluster_ctaidY(),
    2 => cluster::cluster_ctaidZ(),
    3 => cluster::cluster_nctaidX(),
    4 => cluster::cluster_nctaidY(),
    5 => cluster::cluster_nctaidZ(),
    6 => cluster::block_rank(),
    7 => cluster::cluster_size(),
    _ => 0xDEADBEEF,
};
```

见 [crates/rustc-codegen-cuda/examples/cluster/src/main.rs:57-83](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/examples/cluster/src/main.rs#L57-L83)。注意这个内核**没有** `#[cluster_launch]`，所以它跑成普通网格——每个 block 自成一个 size-1 集群，`block_rank()` 恒为 0。这正是为什么 README 反复强调 `cluster_sync` / DSMEM 内核必须带 `#[cluster_launch]`。

#### 4.1.4 代码实践

1. **实践目标**：直观看到「编译期集群配置」改变了内核的 PTX 入口签名，并理解 raw `LaunchConfig` 启动为何要包在 `unsafe` 里。
2. **操作步骤**：
   - 不运行只编译：`cargo oxide build cluster`（无需 GPU，仅验证 PTX 生成）。
   - 用 `cargo oxide pipeline cluster` 找到生成的 `cluster.ptx`，打开后定位 `.entry test_cluster_compile_time`，确认它带 `.explicitcluster` 与 `.reqnctapercluster 4, 1, 1`（对照 [crates/rustc-codegen-cuda/examples/cluster/src/main.rs:34-50](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/examples/cluster/src/main.rs#L34-L50)）。
   - 同时定位 `.entry test_cluster_intrinsics`，确认它**没有** `.reqnctapercluster`——印证 4.1.3 末尾的结论。
3. **需要观察的现象**：两个 `.entry` 的指令前缀一有一无；宿主侧 [crates/rustc-codegen-cuda/examples/cluster/src/main.rs:243-254](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/examples/cluster/src/main.rs#L243-L254) 的 `module.test_cluster_compile_time(...)` 被包在 `unsafe { ... }` 中，并附 `// SAFETY:` 注释。
4. **预期结果**：PTX 里能清楚看到 `.reqnctapercluster`；宿主启动块带 `SAFETY:` 自证。若你的机器 < sm_90，运行阶段会在 [L218-222](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/examples/cluster/src/main.rs#L218-L222) 的算力守卫处提前 `return`，但 PTX 仍正常生成——「能编译」与「能运行」是两件事（见 u1-l5）。
5. 本步骤为「源码阅读 + 产物检视型实践」，运行结果待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：若把 `#[cluster_launch(4, 1, 1)]` 改成 `#[cluster_launch(2, 2, 2)]`，会发生什么？还能通过吗？

> **答案**：`2×2×2 = 8`，正好踩在 ≤ 8 的上限上，编译期形状合法，PTX 会发出 `.reqnctapercluster 2, 2, 2`。但若改成 `(3,3,1)=9` 则超过 8，会在启动契约的集群尺寸校验（4.4 节）或驱动 `cuLaunchKernelEx` 处被拒。

**练习 2**：`block_rank()` 的实现里为什么没有用到 `cluster_nctaidZ()`？

> **答案**：线性秩公式 `x + y·nx + z·nx·ny` 已经唯一编码了 `(x,y,z)`；`z` 的合法上界由 `cluster_ctaidZ() < cluster_nctaidZ()` 隐式保证，公式本身无需 `nz`。

---

### 4.2 分布式共享内存（DSMEM）

#### 4.2.1 概念说明

DSMEM 是 cluster 的「杀手锏」：在同一个集群里，block A 可以**直接**读写 block B 的共享内存，延迟远低于绕道全局显存。PTX 用一条地址翻译指令 `mapa.shared::cluster` 把「本 block 的某个 smem 地址」翻译成「目标 rank 的同一偏移地址」，再用 `ld.shared::cluster` / `st.shared::cluster` 访问。

cuda-device 暴露了三个相关 API（[crates/cuda-device/src/cluster.rs:246-369](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-device/src/cluster.rs#L246-L369)）：

| API | 返回 | 用途 |
|-----|------|------|
| `map_shared_rank(ptr, rank)` | `*const T` | 把本地 smem 指针映射成目标 block 的只读地址 |
| `map_shared_rank_mut(ptr, rank)` | `*mut T` | 同上，可写版本 |
| `dsmem_read_u32(ptr, rank)` | `u32` | **一站式**读取目标 block 的一个 u32 |

#### 4.2.2 核心流程

DSMEM 的标准使用范式是「**写本地 → 块内同步 → 集群同步 → 读邻居**」四步：

```text
每个 block 把自己的数据写入本地 smem
        │
        ▼
sync_threads()          ← 保证本 block 内所有线程都写完
        │
        ▼
cluster_sync()          ← 保证集群内所有 block 都写完（见 4.3）
        │
        ▼
dsmem_read_u32(ptr, neighbor_rank)   ← 现在可以安全读邻居的 smem
```

这里有一个**极易踩的坑**：`map_shared_rank` 返回的指针位于 `shared::cluster` 地址空间，**不能用普通的 `*ptr` 解引用来读**。普通解引用会编译成 generic load（`ld.b32`），它无法寻址远程共享内存，运行时会报 `CUDA_ERROR_ILLEGAL_ADDRESS`。正确做法是用 `dsmem_read_u32`——它在一条内联汇编里把 `mapa` 和 `ld.shared::cluster.u32` 绑在一起，PTX 如下：

```ptx
mapa.shared::cluster.u64 %rd_mapped, %rd_local, %r_rank;
ld.shared::cluster.u32  %r_result,  [%rd_mapped];
```

参见 `dsmem_read_u32` 的文档注释 [crates/cuda-device/src/cluster.rs:357-364](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-device/src/cluster.rs#L357-L364)。

#### 4.2.3 源码精读

**地址翻译**。`map_shared_rank` 是泛型函数，把任意 `*const T` 翻译成目标 rank 的同偏移地址；PTX 形式随指针宽度取 `.u32`/`.u64`：

```rust
/// Lowers to: `mapa.shared::cluster.u32 %rd_dst, %rd_src, %r_rank`
/// (or `.u64` for 64-bit pointers)
#[inline(never)]
pub unsafe fn map_shared_rank<T>(local_ptr: *const T, target_rank: u32) -> *const T {
    let _ = local_ptr;
    let _ = target_rank;
    unreachable!("map_shared_rank called outside CUDA kernel context")
}
```

见 [crates/cuda-device/src/cluster.rs:291-297](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-device/src/cluster.rs#L291-L297)。注意它把入参 `let _ = ...`「使用」掉只是为了消除桩函数的未用变量警告，真实逻辑全靠编译器识别函数名后改写。可写版本 `map_shared_rank_mut` 见 [L312-317](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-device/src/cluster.rs#L312-L317)。

**一站式读取**。`dsmem_read_u32` 是本节最重要的 API，文档明确告诫「不要用 `map_shared_rank` + 解引用」：

```rust
/// Combines `mapa.shared::cluster` (address mapping) and `ld.shared::cluster.u32` (load)
/// into a single atomic operation. This is the correct way to read DSMEM — using
/// `map_shared_rank` followed by a pointer dereference generates a generic load
/// (`ld.b32`) which cannot access remote shared memory.
#[inline(never)]
pub unsafe fn dsmem_read_u32(local_ptr: *const u32, target_rank: u32) -> u32 {
    let _ = local_ptr;
    let _ = target_rank;
    unreachable!("dsmem_read_u32 called outside CUDA kernel context")
}
```

见 [crates/cuda-device/src/cluster.rs:364-369](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-device/src/cluster.rs#L364-L369)。

**示例：DSMEM 环形交换**。cluster 示例的 `test_dsmem_ring_exchange` 把 4 个 block 排成环，每个 block 把自己的号写进本地 smem，集群同步后读「下一个」block 的值：

```rust
// Step 1: 各 block 写入独有值到本地 smem
if tid == 0 {
    unsafe { (addr_of_mut!(SHMEM) as *mut u32).write(1000 + my_rank) };
}
thread::sync_threads();
// Step 2: 集群同步，确保所有 block 都写完
cluster::cluster_sync();
// Step 3: 读邻居的 smem（环形）
if tid == 0 {
    let neighbor_rank = (my_rank + 1) % cluster_size;
    let neighbor_value =
        unsafe { cluster::dsmem_read_u32(addr_of!(SHMEM) as *const u32, neighbor_rank) };
    // ... 写回 output
}
```

见 [crates/rustc-codegen-cuda/examples/cluster/src/main.rs:128-157](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/examples/cluster/src/main.rs#L128-L157)。注意它带 `#[cluster_launch(4,1,1)]`（[L129](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/examples/cluster/src/main.rs#L129)），这是 DSMEM 生效的前提。环形的结果：block 0 读到 1001、block 1 读到 1002、…、block 3 读到 1000（绕回）。

**示例：DSMEM all-to-one 归约**。`test_dsmem_reduction` 演示更复杂的模式：每个 block 贡献一个值，rank 0 用循环依次 `dsmem_read_u32` 读所有邻居的值并求和，最后再做一次 `cluster_sync()`——因为其他 block 必须保持存活，直到 rank 0 读完它们的 smem：

```rust
if tid == 0 && my_rank == 0 {
    let mut total = unsafe { *(addr_of!(LOCAL_VAL) as *const u32) };
    let mut rank = 1u32;
    while rank < cluster_size {
        total += unsafe { cluster::dsmem_read_u32(addr_of!(LOCAL_VAL) as *const u32, rank) };
        rank += 1;
    }
    // ... 写回 output
}
// 所有 block 必须在 rank 0 读 DSMEM 期间保持存活
cluster::cluster_sync();
```

见 [crates/rustc-codegen-cuda/examples/cluster/src/main.rs:164-200](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/examples/cluster/src/main.rs#L164-L200)。末尾那次 `cluster_sync()` 是关键防线——若其他 block 提前退出，rank 0 读到的将是已释放的 smem，触发 `CUDA_ERROR_LAUNCH_FAILED`（见 cluster 示例 README 的错误表）。

#### 4.2.4 代码实践

1. **实践目标**：亲手验证「map_shared_rank + 解引用」会失败、`dsmem_read_u32` 会成功。
2. **操作步骤**：复制 `test_dsmem_ring_exchange` 内核，改名 `bad_dsmem`，把读邻居那一行换成
   ```rust
   let neighbor_ptr = unsafe { cluster::map_shared_rank(addr_of!(SHMEM) as *const u32, neighbor_rank) };
   let neighbor_value = unsafe { *neighbor_ptr };   // ← 错误用法
   ```
   在 sm_90+ 机器上 `cargo oxide run cluster`（或单独跑这个内核）。
3. **需要观察的现象**：内核启动后 `stream.synchronize()` 返回 `CUDA_ERROR_ILLEGAL_ADDRESS`，宿主侧 `ring_result` 落入 `Err` 分支（对照 [L400-428](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/examples/cluster/src/main.rs#L400-L428) 的错误处理），随后一个 CUDA context 被污染、Test 4 被跳过。
4. **预期结果**：改回 `dsmem_read_u32` 后一切正常；这印证了 4.2.2 的地址空间约束。运行结果待本地验证。
5. **说明**：这是一个「故意制造错误以理解约束」的实践；在没有 GPU 时，可只做源码阅读——在 PTX 里确认错误版生成的是 `ld.b32`、正确版生成的是 `mapa` + `ld.shared::cluster.u32`。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `dsmem_read_u32` 只支持 `u32`，而 `map_shared_rank` 是泛型的？

> **答案**：`dsmem_read_u32` 把 `mapa` + `ld.shared::cluster.u32` 写死成一条内联汇编，故只能取一个 32 位字；`map_shared_rank` 仅做地址翻译，返回的指针类型可随 `T` 变化，但它的产物**不能**用 generic load 读取，只适合传给其它已知会用 `ld.shared::cluster` 的内联汇编（如 mcast 屏障里取邻居的 mbarrier 地址，见 4.4）。

**练习 2**：`test_dsmem_reduction` 末尾的 `cluster_sync()` 删掉会怎样？

> **答案**：非 rank 0 的 block 可能在 rank 0 还没读完它们的 smem 时就退出，导致 rank 0 读到已释放的 smem，触发 `CUDA_ERROR_LAUNCH_FAILED`。这条尾部屏障是「让所有 block 活到 reader 读完」的生命周期保证。

---

### 4.3 集群级屏障 cluster_sync

#### 4.3.1 概念说明

DSMEM 要正确，必须保证「读邻居之前，邻居已经写完」。`sync_threads()` 只在**单个 block 内**有效，管不到别的 block。cluster 引入了**集群级屏障** `cluster_sync()`：集群内所有 block 的所有线程都必须到达，才能有任何线程继续——它是 `sync_threads()` 的集群版。

它与 u5-l3 学过的 `mbarrier` 异步屏障是**互补**的两件事：

| 屏障 | 粒度 | 阻塞性 | 能否感知硬件 DMA |
|------|------|--------|------------------|
| `sync_threads()` | block | 阻塞、只认线程 | 否 |
| `cluster_sync()` | cluster | 阻塞、只认线程 | 否 |
| `mbarrier` | 自定义（可 cluster） | 异步、可认事务字节 | **是**（配合 `expect_tx`/TMA） |

简单说：纯线程间同步用 `cluster_sync`；要等硬件异步搬运（cp.async / TMA）完成则必须用 mbarrier。本节的 mcast 协议会把两者叠加。

#### 4.3.2 核心流程

```text
所有 block 各自完成本地写入
        │
        ▼
sync_threads()    ← 块内汇合（写本地 smem 对块内可见）
        │
        ▼
cluster_sync()    ← 集群汇合（本地 smem 对全集群可见）
        │
        ▼
此后任何 block 用 dsmem_read_u32 读邻居都是安全的
```

**安全约束**：集群内**所有线程**必须到达同一个 `cluster_sync()`。把它放进 `if (cond) { cluster_sync(); }` 这样的条件分支、使得部分线程绕过，会直接死锁——这与 `sync_threads()` 的规则完全一致。

#### 4.3.3 源码精读

`cluster_sync()` 同样是 `unreachable!()` 桩，由编译器识别后降级为 `cluster.sync.aligned`：

```rust
/// Lowers to: `cluster.sync.aligned`
#[inline(never)]
pub fn cluster_sync() {
    unreachable!("cluster_sync called outside CUDA kernel context")
}
```

见 [crates/cuda-device/src/cluster.rs:240-243](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-device/src/cluster.rs#L240-L243)，文档与用法示例见 [L210-229](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-device/src/cluster.rs#L210-L229)。

**示例：cluster_sync 的最小自检**。`test_cluster_sync` 让每个 block 在本地 smem 写一个「与 rank 相关」的值，集群同步后再读出来写回全局内存：

```rust
#[cluster_launch(4, 1, 1)]
pub fn test_cluster_sync(mut output: DisjointSlice<u32>) {
    static mut SHMEM: SharedArray<u32, 1> = SharedArray::UNINIT;
    let tid = thread::threadIdx_x();
    let my_rank = cluster::block_rank();

    if tid == 0 {
        unsafe { (addr_of_mut!(SHMEM) as *mut u32).write(my_rank * 100 + 42) };
    }
    thread::sync_threads();      // 块内同步
    cluster::cluster_sync();     // 集群同步

    if tid == 0 {
        let idx = my_rank as usize;
        if idx < output.len() {
            let local_value = unsafe { *(addr_of!(SHMEM) as *const u32) };
            unsafe { *output.get_unchecked_mut(idx) = local_value };
        }
    }
}
```

见 [crates/rustc-codegen-cuda/examples/cluster/src/main.rs:96-121](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/examples/cluster/src/main.rs#L96-L121)。这里关键是 `#[cluster_launch(4,1,1)]`（[L97](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/examples/cluster/src/main.rs#L97)）——内核注释专门说明：没有它，网格按普通 block 启动，每个 block 自成 size-1 集群，`block_rank()` 恒为 0，只有 `output[0]` 被写入，整个测试失去意义。

#### 4.3.4 代码实践

1. **实践目标**：感受「缺 `#[cluster_launch]` 会让集群语义退化」。
2. **操作步骤**：把 `test_cluster_sync` 上的 `#[cluster_launch(4,1,1)]` 注释掉，重新 `cargo oxide run cluster`，观察 Test 2 的输出。
3. **需要观察的现象**：`sync_results` 退化为只有第 0 个元素是 `42`（`0*100+42`），其余为 `0`（`zeroed` 初值）；`sync_pass` 判定为 false，打印 `⚠ Cluster sync returned unexpected values`。
4. **预期结果**：与上面对「size-1 集群」的分析一致。运行结果待本地验证（需 sm_90+）。
5. 恢复 `#[cluster_launch]` 后再跑，确认 `[42, 142, 242, 342]` 正常出现。

#### 4.3.5 小练习与答案

**练习 1**：`cluster_sync()` 和 `mbarrier` 都能跨 block 同步，什么时候必须选 mbarrier？

> **答案**：当代码需要等待**硬件异步单元**（cp.async / TMA）的搬运完成时，`cluster_sync()` 无能为力（它只认线程），必须用 mbarrier 的 `expect_tx` 事务字节计数。纯线程间汇合则 `cluster_sync()` 更简单、无需 init。

**练习 2**：在一个 4-block 集群里，只有 rank 0 的线程 0 调用 `cluster_sync()`，其余线程不调用，会发生什么？

> **答案**：死锁。`cluster_sync()` 要求集群内**所有线程**到达，缺一个都无法放行。它必须出现在所有线程都会执行的路径上，不能放进 rank/tid 守卫的分支里。

---

### 4.4 多播（mcast）与启动契约对集群轴的校验

#### 4.4.1 概念说明

「多播（multicast）」在 Hopper 语境下有两层含义，本讲只讲**集群层 mcast 屏障协议**（TMA 硬件多播留给 u5-l5）：

- 多个 block 需要协同等待同一个事件（典型场景：TMA 把一份数据**多播**到集群内多个 block 的 smem，所有接收方都要等「这份多播搬运完成」）。要做到这一点，需要一个**全集群共享的 mbarrier**：所有参与者都 `arrive`，发起方 `wait`。
- 这个共享 mbarrier 物理上放在某个 block（通常是 rank 0）的 smem 里，其它 block 用 `map_shared_rank` 拿到它的远程地址，再用 `mbarrier_arrive_cluster` 远程到达。rank 0 则用 `mbarrier_try_wait_parity` 在本地等待。

这正是 `mcast_barrier_test` 示例和 `gemm_sol_clc` 里反复用的协议。它把 4.2（DSMEM 地址翻译）+ u5-l3（mbarrier）+ 4.3（cluster_sync）三者拧成一股绳。

与此同时，cluster 是一项「重型」硬件能力，启动侧必须严格校验。cuda-oxide 把集群形状作为 **`#[launch_contract]` 的一等公民**：`#[cluster_launch(x,y,z)]` 会让该内核的契约自动 `.with_cluster((x,y,z))` 并把最低算力提到 `(9,0)`，`prepare_*` 路径会在活设备上逐项校验。

#### 4.4.2 核心流程

**mcast 屏障协议**（双缓冲奇偶版本）：

```text
初始化：rank 0 的 smem 里放两个 mbarrier（BAR0/BAR1），各 CLUSTER_SIZE 个到达名额
所有 block 用 map_shared_rank 拿到 rank0 的 BAR0/BAR1 远程地址
cluster_sync()                             ← 确保初始化对全集群可见
loop k:
    stage = k & 1                           ← 双缓冲选哪块
    每个 block 的 thread 0: mbarrier_arrive_cluster(rank0 的 BAR{stage})
    rank 0: mbarrier_try_wait_parity(BAR{stage}, parity)  ← 等所有 block 到达
    cluster_sync()                          ← 进入下一轮前汇合
```

**启动契约的集群校验链**（在 `PreparedLaunch::__prepare` 里，活设备上一次性完成）：

```text
1. validate_cluster_support       ← 设备是否支持 cluster launch？
2. validate_required_cluster      ← 宿主声明的集群形状 == 编译进 PTX 的 .reqnctapercluster？
3. validate_shape + 整除校验      ← 每个集群轴 ≠ 0，且能整除对应 grid 轴
4. max_potential_cluster_size     ← 集群 block 数 ≤ 活设备报告的上限？
5. max_active_clusters            ← 该具体形状能否在硬件上驻留？
```

任何一步失败，`prepare_*` 都返回一个带 `kernel` 名字的 `LaunchContractError::Cluster*` 变体，调用方在启动前就知道问题。

#### 4.4.3 源码精读

**mcast 协议示例**。`mcast_barrier_test` 是这份协议的最小自包含测试，4 个 CTA 循环 N 次（4/8/16/32/64/256/1024），每次都 arrive 到 rank 0 的屏障：

```rust
#[kernel]
#[cluster_launch(4, 1, 1)]
pub unsafe fn mcast_barrier_loop(mut out: DisjointSlice<u32>, num_iters: u32) {
    unsafe {
        static mut MCAST_BAR0: Barrier = Barrier::UNINIT;
        static mut MCAST_BAR1: Barrier = Barrier::UNINIT;
        // ... rank 0 的 thread 0 初始化两个 mbarrier ...

        // 关键：每个 block 都拿到 rank 0 两个屏障的远程 smem 地址
        let rank0_bar0_addr = cluster::map_shared_rank(&raw const MCAST_BAR0, 0) as u64;
        let rank0_bar1_addr = cluster::map_shared_rank(&raw const MCAST_BAR1, 0) as u64;

        cluster::cluster_sync();
        while k < num_iters {
            let stage = k & 1;
            // 每个 CTA 的 thread 0 远程 arrive 到 rank 0 的对应屏障
            if tid == 0 {
                if stage == 0 { mbarrier_arrive_cluster(rank0_bar0_addr); }
                else           { mbarrier_arrive_cluster(rank0_bar1_addr); }
            }
            // rank 0 在本地等所有 4 个 CTA 到达
            if is_rank0 {
                while !mbarrier_try_wait_parity(&raw const MCAST_BAR{stage}, mcast_parity) {
                    nanosleep(32);
                }
            }
            cluster::cluster_sync();
            k += 1;
        }
    }
}
```

见 [crates/rustc-codegen-cuda/examples/mcast_barrier_test/src/main.rs:32-100](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/examples/mcast_barrier_test/src/main.rs#L32-L100)。这里 `map_shared_rank` 拿到的是远程 mbarrier 的地址——这正是 4.2 练习 1 里说的「`map_shared_rank` 的正确用法」：它不拿去 generic load，而是交给 `mbarrier_arrive_cluster` 这条本身就发出 `cluster` 语义指令的 intrinsic。双缓冲（BAR0/BAR1）配合奇偶等待（`mcast_parity = (k>>1) & 1`）使前后两轮不互相干扰，这是软件流水线的标准技巧。

**启动契约里的集群字段**。`LaunchContractSpec` 持有一个可选的 `cluster: Option<(u32,u32,u32)>`：

```rust
pub struct LaunchContractSpec {
    kernel_name: &'static str,
    block: BlockRequirement,
    dynamic_shared_memory: DynamicSharedMemoryRequirement,
    cluster: Option<(u32, u32, u32)>,
    cooperative: bool,
    min_compute_capability: Option<(u32, u32)>,
}
```

见 [crates/cuda-core/src/launch.rs:216-224](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-core/src/launch.rs#L216-L224)，`with_cluster` 构造器见 [L244-249](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-core/src/launch.rs#L244-L249)。宏侧 `#[cluster_launch]` 触发的契约生成会把 `.with_cluster((x,y,z))` 拼到 `SPEC` 上：

```rust
let cluster = contract.cluster_tokens(kernel.cluster_dim);
// ...
const SPEC: ::cuda_core::LaunchContractSpec =
    ::cuda_core::LaunchContractSpec::new(#kernel_name, #block, #dynamic_shared)
        #cluster        // ← 这里展开成 .with_cluster((4u32,1u32,1u32))
        #cooperative
        #compute_capability;
```

见 [crates/cuda-macros/src/lib.rs:2092-2110](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-macros/src/lib.rs#L2092-L2110)，`cluster_tokens` 的实现见 [L2115-2117](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-macros/src/lib.rs#L2115-L2117)。同时，宏一旦发现内核带集群维度，就自动把最低算力抬到 `(9,0)`——Hopper 门禁在编译期就钉死：

```rust
let min_compute_capability = match cluster_dim {
    Some(_) if args.min_compute_capability < (9, 0) => (9, 0),
    _ => args.min_compute_capability,
};
```

见 [crates/cuda-macros/src/lib.rs:1792-1795](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-macros/src/lib.rs#L1792-L1795)。

**活设备上的集群校验链**。`PreparedLaunch::__prepare` 里对 `C::SPEC.cluster` 的处理分两段。第一段做「能力 + 形状一致性」校验：

```rust
if let Some(cluster) = C::SPEC.cluster {
    validate_cluster_support(C::SPEC.kernel_name, context.supports_cluster_launch()?)?;
    validate_required_cluster(
        C::SPEC.kernel_name,
        cluster,
        function.required_cluster_dimensions()?,
    )?;
}
```

见 [crates/cuda-core/src/launch.rs:863-870](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-core/src/launch.rs#L863-L870)。第二段做「尺寸上限 + 驻留能力」校验：

```rust
if let Some(cluster) = C::SPEC.cluster {
    let max_cluster_size = function.max_potential_cluster_size(raw.grid_dim, raw.block_dim, raw.shared_mem_bytes)?;
    let cluster_blocks = shape_product(C::SPEC.kernel_name, LaunchDimension::Cluster, cluster)?;
    validate_cluster_size(C::SPEC.kernel_name, cluster_blocks, max_cluster_size)?;
    let active_clusters = match function.max_active_clusters(raw.grid_dim, raw.block_dim, raw.shared_mem_bytes, cluster) {
        Ok(active_clusters) => active_clusters,
        Err(error) if error.0 == ...CUDA_ERROR_INVALID_CLUSTER_SIZE => {
            return Err(LaunchContractError::ClusterShapeUnsupported { kernel: C::SPEC.kernel_name, cluster });
        }
        Err(error) => return Err(error.into()),
    };
    validate_cluster_residency(C::SPEC.kernel_name, cluster, active_clusters)?;
}
```

见 [crates/cuda-core/src/launch.rs:894-921](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-core/src/launch.rs#L894-L921)。形状的「整除 grid」校验则在静态校验侧（[L1044-1052](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-core/src/launch.rs#L1044-L1052)）。

这套校验把 cluster 启动的所有典型坑都变成了**带名字的、可读的错误**，而不是模糊的 `CUDA_ERROR_INVALID_VALUE`：

| 错误变体 | 拦截的故障 | 源码 |
|----------|-----------|------|
| `ClusterLaunchUnsupported` | 设备 < sm_90，硬件不支持 cluster | [L512-515](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-core/src/launch.rs#L512-L515) |
| `ClusterDoesNotDivideGrid` | 集群轴不能整除对应 grid 轴 | [L436-453](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-core/src/launch.rs#L436-L453) |
| `ClusterSizeExceeded` | 集群 block 数 > 活设备上限 | [L517-525](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-core/src/launch.rs#L517-L525) |
| `FunctionClusterShapeMismatch` | 宿主声明形状 ≠ PTX 里 `.reqnctapercluster` | [L529-535](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-core/src/launch.rs#L529-L535) |
| `RequiredClusterDimensionsMissing` | 宿主声明了集群，但 PTX 入口没有相应元数据 | [L537-543](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-core/src/launch.rs#L537-L543) |
| `ClusterShapeUnsupported` / `ClusterHasNoResidency` | 驱动拒绝该具体形状 / 无集群可驻留 | [L545-560](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-core/src/launch.rs#L545-L560) |

各 `validate_cluster_*` 辅助函数集中在 [L1163-1214](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-core/src/launch.rs#L1163-L1214)。

> 备注：本讲的示例 `cluster` 与 `mcast_barrier_test` 目前都用 raw `LaunchConfig` 启动（`unsafe { module.xxx(...) }`，见 [cluster main.rs:243-254](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/examples/cluster/src/main.rs#L243-L254) 与 [mcast_barrier_test main.rs:134](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/examples/mcast_barrier_test/src/main.rs#L134)），没有走 `prepare_*` 受检路径——也就是说，上面这些 `Cluster*` 错误在该示例里不会自动触发，错误会以驱动返回码的形式出现。要把集群校验升级为编译期/`prepare` 期受检，需配合 `#[launch_contract(...)]`，由宏把 `.with_cluster(...)` 写进 `SPEC`（u2-l1、u2-l4 已建立这套认知）。`cuda_launch!` 宏则提供了另一条声明式路径，其 `cluster_dim` 字段在 [crates/cuda-macros/src/lib.rs:5232-5283](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-macros/src/lib.rs#L5232-L5283) 处理，并与 `cooperative` 互斥。

#### 4.4.4 代码实践

1. **实践目标**：理解 mcast 协议中「`map_shared_rank` 取远程 mbarrier 地址」这一步为何不踩 4.2 的坑。
2. **操作步骤**：阅读 [mcast_barrier_test/src/main.rs:51-52](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/examples/mcast_barrier_test/src/main.rs#L51-L52)，注意它把 `map_shared_rank` 的结果**转成 `u64` 当地址数值**传给 `mbarrier_arrive_cluster`，而不是解引用。若机器允许，`cargo oxide run mcast_barrier_test`，观察 4~1024 轮迭代全部 `PASSED`。
3. **需要观察的现象**：每次循环 4 个 CTA 都正确完成、`host_out` 全等于 `num_iters`；说明 rank 0 的 `mbarrier_try_wait_parity` 确实收到了全部 4 个远程 arrive。
4. **预期结果**：与 README/注释一致。运行结果待本地验证（需 sm_90+）。
5. **思考题（源码阅读型）**：如果把 `mbarrier_arrive_cluster(rank0_bar0_addr)` 改成 `mbarrier_arrive_cluster` 一个**本地** mbarrier 地址（不经 `map_shared_rank`），会发生什么？答：只有当前 block 自己 arrive，rank 0 永远等不到其余 3 个到达，`try_wait_parity` 死循环、内核无法退出。

#### 4.4.5 小练习与答案

**练习 1**：`FunctionClusterShapeMismatch` 与 `RequiredClusterDimensionsMissing` 这两个错误分别拦截什么？给出触发场景。

> **答案**：`FunctionClusterShapeMismatch`——宿主契约声明集群 `(4,1,1)`，但 PTX 入口的 `.reqnctapercluster` 是 `(2,1,1)`（即 `#[cluster_launch]` 写的形状与 `#[launch_contract(cluster=...)]` 写的不一致）。`RequiredClusterDimensionsMissing`——宿主声明了集群维度，但 PTX 入口根本没有 `.reqnctapercluster`（例如内核忘了加 `#[cluster_launch]`，却用契约声称它是集群内核）。两者都是「宿主声明 vs 编译产物」的不一致。

**练习 2**：为什么 `LaunchContractSpec::cluster` 是 `Option`，而 `block` 是必填的 `BlockRequirement`？

> **答案**：并非所有内核都用集群——`Option` 表示「集群是 opt-in 能力」，普通内核 `cluster = None`，校验链整段跳过。而每个内核都必须有 block 形状（线程数是启动的基本参数），故 `block` 必填。

---

## 5. 综合实践

把本讲四个模块串起来，写一个 **DSMEM halo exchange（边缘交换）** 内核。

**背景**：一维模板计算里，每个 block 负责一段输出，但它需要「左右邻居 block」边界处的一个值。用 DSMEM 可以免掉一次全局显存往返。

**任务**：

1. 在一个 `#[cuda_module] mod kernels` 里写内核 `#[kernel] #[cluster_launch(4,1,1)] pub fn halo_exchange(mut out: DisjointSlice<u32>)`。每个 block 在本地 smem 写入 `my_rank`，`sync_threads` + `cluster_sync` 后，**每个 block 都读它左右两个邻居**（`(my_rank + cluster_size - 1) % cluster_size` 与 `(my_rank + 1) % cluster_size`）的值，用 `dsmem_read_u32`，把三者之和写进 `out[my_rank]`。
2. 宿主侧参照 [cluster main.rs:207-527](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/rustc-codegen-cuda/examples/cluster/src/main.rs#L207-L527)：分配长度 4 的 `DeviceBuffer<u32>`，用 raw `LaunchConfig { grid_dim: (4,1,1), block_dim: (32,1,1), shared_mem_bytes: 0 }` 在 `unsafe` 块里启动，`synchronize` 后 `to_host_vec` 取回。
3. **自检**：每个 block 的左右邻居号之和等于「除自己外全部 rank 之和」。对 4 个 block（rank 0..3），任一 rank 的左右邻居集合都是「另外三个里的两个」，所以 `out[i] = (sum of 0..=3) - i = 6 - i`。即预期 `out = [6,5,4,3]`。
4. **进阶**：把内核再加一个 `#[launch_contract(domain=1, block=(32,1,1))]`（注意它会与 `#[cluster_launch]` 叠加，宏自动 `.with_cluster((4,1,1))` 并把 min CC 设为 `(9,0)`），改用 `module.prepare_halo_exchange(LaunchConfig1D::new(...))` 受检启动；在 < sm_90 的机器上观察 `prepare_*` 返回 `ClusterLaunchUnsupported`，体会 4.4 的活设备校验。

> 若没有 sm_90+ GPU，至少完成「能编译」：`cargo oxide build` 你的示例，用 `cargo oxide pipeline` 检视 PTX 里 `.reqnctapercluster 4, 1, 1` 与 `mapa.shared::cluster` 是否出现。运行结果待本地验证。

## 6. 本讲小结

- cluster 是 Hopper sm_90+ 在 `Grid → Block` 之间新增的执行层级，把最多 **8 个 block** 绑定到同一 GPC 协同调度，使它们的共享内存拼成 **DSMEM**。
- 坐标 intrinsic 与 grid/block 对称：`cluster_ctaidX/Y/Z`（block 在集群内）、`cluster_nctaidX/Y/Z`（集群尺寸）、`cluster_idx`/`num_clusters`（集群在 grid 内）；派生量 `block_rank()` 与 `cluster_size()` 是 DSMEM 寻址的基础。
- `#[cluster_launch(x,y,z)]` 只是在函数体首部注入 `__cluster_config::<X,Y,Z>()` 编译期标记，mir-importer 识别后产出 `.reqnctapercluster X,Y,Z` + `.explicitcluster`。
- DSMEM 的正确读法是 `dsmem_read_u32`（`mapa` + `ld.shared::cluster.u32` 一条汇编）；`map_shared_rank` 后用 generic 解引用会触发 `CUDA_ERROR_ILLEGAL_ADDRESS`。`map_shared_rank` 的正确用途是取远程 mbarrier 等结构的地址。
- `cluster_sync()`（`cluster.sync.aligned`）是集群级阻塞屏障，要求所有线程到达；与 u5-l3 的 `mbarrier` 互补——前者只认线程，后者能感知硬件异步搬运。mcast 协议把二者叠加，用 `map_shared_rank` + `mbarrier_arrive_cluster` 实现全集群共享屏障。
- 集群形状是 `#[launch_contract]` 的一等公民：`LaunchContractSpec::cluster` + `with_cluster`，宏自动把集群内核的 min CC 抬到 `(9,0)`；`prepare_*` 在活设备上依次校验支持性、形状一致、整除 grid、尺寸上限、驻留能力，失败映射成带 kernel 名的 `Cluster*` 错误。

## 7. 下一步学习建议

- **u5-l5 张量内存加速器 TMA**：TMA 是 cluster 最重要的「客户」——TMA 多播把一份数据一次搬到多个 block 的 smem，接收方正是用本讲的 mcast 屏障协议等待完成。学完 TMA 你会把 `mbarrier_arrive_cluster` + `expect_tx` 真正用起来。
- **u5-l6 矩阵乘加速器**：`gemm_sol_clc` 这类高优 GEMM 把 cluster DSMEM、mcast 屏障、wgmma 三者结合，是本讲协议的工业级实战。
- **回到 u2-l4 / u7-l1**：如果你想给自己的集群内核加上「编译期 + prepare 期」的集群轴受检启动，复习 `#[launch_contract]` 的 `with_cluster` 路径，并用 compile_fail 测试固化形状契约。
- **源码延伸**：阅读 [crates/cuda-device/src/cluster.rs](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-device/src/cluster.rs) 全文与 [crates/cuda-core/src/launch.rs](https://github.com/NVlabs/cuda-oxide/blob/29396b7f643b1d42eb4d80b7347ad27bb011525a/crates/cuda-core/src/launch.rs) 的 `Cluster*` 错误与 `validate_cluster_*` 函数，对照 `gemm_sol_clc` / `mcast_barrier_test` 理解协议在真实内核里的落地。
