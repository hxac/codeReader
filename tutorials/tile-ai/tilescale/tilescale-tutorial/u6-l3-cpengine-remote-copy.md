# CP-engine 远程 get/put 原语

## 1. 本讲目标

本讲是分布式单元的第三讲，承接 u6-l2（NVSHMEM 多设备通信原语）。学完本讲，你应当能够：

- 说清 CP-engine 路线与 NVSHMEM 路线的差别，知道它们各自用哪一组 Python 原语、落到哪一段 C++ 代码。
- 掌握 `put_block / put_warp / get_block / get_warp` 四个搬运原语的参数含义，尤其是 `dst_pe / src_pe`、`size`、`unroll_factor`、`enable_aggressive_vectorize`。
- 理解 `unroll_factor` 与 aggressive vectorize（`int4` 16 字宽）如何影响拷贝吞吐。
- 理解对称堆（symmetric heap）寻址的数学本质，看懂 `wait_eq / wait_ne` 等条件等待是如何在远程地址上自旋的。
- 基于示例写出一个可校验的 ring-shift kernel。

## 2. 前置知识

本讲默认你已经读完：

- **u6-l1**：知道 PE 与 rank 同义，知道 TileScale 真正落地的是「多进程 + NVSHMEM 对称堆」务实路线，远程基址表通过 `kernel.initialize(allocator=...)` 注入。
- **u6-l2**：知道对称堆的「同一对称偏移在所有 PE 上指向逻辑同一地址」这一核心抽象，知道 `barrier_all / sync_all / quiet / fence` 的同步强度差别。
- **u2-l2 / u2-l5**：知道 `T.alloc_local` 分配的是线程私有寄存器，知道 `T.address_of` 取一个 buffer 元素的地址。

### CP-engine 路线 vs NVSHMEM 路线（一句话回顾）

u6-l2 讲的是 **NVSHMEM 路线**：Python 端 `T.putmem_* / T.getmem_*` → C++ `tl.Putmem*` 等内置算子（注册在 `src/op/distributed.cc`）→ codegen 打印 `nvshmem*` / `nvshmemx*` C API 调用文本。它直接复用 NVSHMEM 运行时库。

本讲讲的是 **CP-engine 路线**：Python 端 `T.put_block / put_warp / get_block / get_warp` → `tl.tileop.put / tl.tileop.get` 这一类 **TileOperator**（注册在 `src/op/remote_copy.cc`）→ Lower 阶段降级成 `tl::cp_warp` / `tl::cp_block` **设备模板**（定义在 `src/tl_templates/cuda/copy.h`）。它不直接调 NVSHMEM 的 `nvshmem_putmem`，而是用 CUDA 线程自己去「load + store」远端对称堆地址，靠 GPU 的全局内存子系统完成跨 PE 搬运。

> 命名提醒：`tilelang/language/distributed/multi_device/cpengine.py` 这个文件名容易误导——它目前只暴露了一个 `cpengine_cpasync` 薄封装，真正的 CP-engine 风格 `put/get/wait` 原语全部定义在 `tilelang/language/distributed/common.py` 里，并在 `src/op/remote_copy.cc` / `src/op/sync.cc` 中降级。

### 两条路线的「rank」概念口径

- NVSHMEM 路线用 `T.get_pe() / T.get_pe_num()`（PE 口径）。
- CP-engine 路线用 `T.get_rank() / T.get_num_ranks()`（rank 口径）。

二者数值上是同一个东西（都等于进程在通信组里的序号），但分属两套内置算子，**不要在同一个 kernel 里混用**。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `tilelang/language/distributed/common.py` | Python 端 CP-engine 原语：`put_warp / get_warp / put_block / get_block / wait_eq / wait_ne / ...` 与 `get_rank / get_num_ranks` |
| `src/op/remote_copy.cc` | C++ 端 `PutOp / GetOp` TileOperator：解析参数、判定是否分布式、把地址重映射成远端地址、选择 `cp_warp / cp_block` 模板 |
| `src/op/sync.cc` | C++ 端 `WaitOp`：`wait_eq / wait_ne / ...` 条件等待的降级 |
| `src/tl_templates/cuda/copy.h` | 设备模板 `cp_warp / cp_warp_impl / cp_block / nvshmem_cp_threadgroup`（真正干活的 load/store 循环） |
| `src/tl_templates/cuda/sync.h` | 设备模板 `wait_eq / wait_ne / wait_ge / ...`（volatile 自旋） |
| `src/tl_templates/cuda/distributed.h` | 设备端 `get_rank / get_num_ranks / get_remote_base_ptr / get_uintptr_t`，读取 `__constant__` 基址表 `meta_data` |
| `src/op/distributed.cc` | 上述内置算子的 TVM Op 注册 |
| `src/target/codegen_cuda.cc` | codegen 把内置算子打印成 `tl::get_rank()` 等文本，并按需 `#include` 分布式头文件 |
| `tilelang/jit/kernel.py` | `JITKernel.initialize()` 把 allocator 的基址表拷进设备 `meta_data` |
| `examples/distributed/primitives/example_put_block.py` 等 | 最小可运行示例 |

## 4. 核心概念与源码讲解

本讲分三个最小模块：

1. **4.1** `put_block / get_block / put_warp / get_warp` 与 `dst_pe / src_pe`：搬运原语的参数与「本地 vs 远程」判定。
2. **4.2** `unroll_factor` 与 aggressive vectorize：warp 级搬运的吞吐旋钮。
3. **4.3** `wait_eq / wait_ne` 条件等待与对称堆寻址：远程地址如何被算出来、`wait` 如何在它上面自旋。

### 4.1 put_block / get_block / put_warp / get_warp 与 dst_pe / src_pe

#### 4.1.1 概念说明

CP-engine 路线提供四个搬运原语，两两成对（put 写远端、get 读远端），又按「参与搬运的线程粒度」分成两档：

| 原语 | 方向 | 粒度 | 典型场景 |
| --- | --- | --- | --- |
| `put_block` | 本地 src → 远端 dst | 整个 block | 一个 block 块搬一整段 |
| `get_block` | 远端 src → 本地 dst | 整个 block | 一个 block 块拉一整段 |
| `put_warp` | 本地 src → 远端 dst | 单个 warp（32 线程） | 多个 warp 各搬自己那一段 |
| `get_warp` | 远端 src → 本地 dst | 单个 warp | 多个 warp 各拉自己那一段 |

每个原语的核心参数是「源地址、目的地址、元素个数、对端 PE」。当对端 PE 为 `-1` 时，它退化成一次**本地拷贝**（仍走 `cp_warp/cp_block` 模板，但不做远程地址重映射）；当对端 PE 是某个具体 rank 时，编译器会把目的/源地址重映射到那个 PE 的对称堆上。

> 关键认知：这四个 Python 函数在前端**都只是拼装一条 `tir.call_intrin`**，真正的「远程地址怎么算、模板怎么选」全部发生在 C++ 的 `PutOp::Lower / GetOp::Lower` 里。这点和 u2-l3 讲过的「`T.gemm` 只是 intrin，指令在 lowering 生成」完全一致。

#### 4.1.2 核心流程

以 `put_block` 为例，从 Python 到设备代码的链路是：

1. **Python**：`put_block(src, dst, size, dst_pe)` 调用 `tir.call_intrin("handle", Op.get("tl.tileop.put"), src, dst, size, dst_pe, 0, "block", True)`，把 7 个参数与 scope 字符串 `"block"` 打包成一条 intrin。
2. **C++ 构造**：`PutOp::PutOp` 解析这 7 个参数，校验 `src/dst` 必须是 `address_of(BufferLoad)` 形式，并算出源/目的相对各自 buffer 起点的字节偏移。
3. **分布式判定**：`is_distributed()` 看 `dst_pe` 是否为整型常量 `-1`。不是 `-1` → 远程拷贝。
4. **Lower**：`PutOpNode::Lower` 选模板（`scope=="block"` → `tl::cp_block<N>`），并按 `is_distributed()` 决定是否把目的地址换成远端基址 + 偏移。
5. **codegen**：把这条 `call_extern("tl::cp_block<N>", 远端地址, 本地源地址)` 打印成 CUDA 源码文本。
6. **设备**：`cp_block<N>` 模板里每个线程按自己的 `threadIdx` 去搬运一片元素。

`get_*` 链路完全对称，只是源/目的角色互换，参数里是 `src_pe` 而非 `dst_pe`。

#### 4.1.3 源码精读

**Python 端四个原语**——注意 `put_block / get_block` 把 scope 写死成 `"block"`，而 `put_warp / get_warp` 写死成 `"warp"` 并额外带 `unroll_factor / enable_aggressive_vectorize`：

- [`tilelang/language/distributed/common.py:21-49`](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/distributed/common.py#L21-L49) — `put_warp`：7 个参数依次是 `src, dst, size, dst_pe, unroll_factor, "warp", enable_aggressive_vectorize`，`dst_pe` 默认 `-1`（本地）。
- [`tilelang/language/distributed/common.py:83-99`](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/distributed/common.py#L83-L99) — `put_block`：固定传 `unroll_factor=0`、`scope="block"`、`enable_aggressive_vectorize=True`。注释说明 block 级通信基于 NVSHMEM 风格拷贝，`unroll_factor` 在此不生效。

> 为什么 `put_block` 的 `unroll_factor` 写 0？因为 block 级走的是 `cp_block → nvshmem_cp_block`，它按 16B/8B/4B/2B/1B 自动分块（见 4.2.3），不受 `unroll_factor` 控制；`unroll_factor` 只对 warp 级的 `cp_warp_impl` 有意义。

**C++ 端 `PutOp` 构造与分布式判定**：

- [`src/op/remote_copy.cc:54-84`](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/op/remote_copy.cc#L54-L84) — 解析 7 个参数：`args[0/1]` 是 `address_of` 包裹的源/目的，`args[2]=copy_size`，`args[3]=dst_pe`，`args[4]=unroll_factor`，`args[5]=scope`，`args[6]=enable_aggressive_vectorize`。
- [`src/op/remote_copy.cc:86-89`](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/op/remote_copy.cc#L86-L89) — `is_distributed()`：仅当 `dst_pe` 是整型常量 `-1` 时返回 `false`（本地拷贝）。

**`PutOpNode::Lower`——模板选择 + 远程地址重映射**（本讲最关键的一段）：

- [`src/op/remote_copy.cc:91-122`](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/op/remote_copy.cc#L91-L122) — `scope=="warp"` 拼出 `tl::cp_warp<copy_size, unroll_factor, aggressive>`，`scope=="block"` 拼出 `tl::cp_block<copy_size>`；分布式分支里把目的地址换成 `get_remote_base_ptr(dst_pe) + offset_to_base`。

`GetOp` 与之完全对称，只是远程重映射作用在 `src` 上、且模板调用的实参顺序是「先 dst 后 src」（见代码注释 `// Always dst first in tl_templates`）：

- [`src/op/remote_copy.cc:200-233`](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/op/remote_copy.cc#L200-L233) — `GetOpNode::Lower`。

**Op 注册**——把 `tl.tileop.put / tl.tileop.get` 绑到 `PutOp / GetOp`，固定 7 输入：

- [`src/op/remote_copy.cc:382-390`](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/op/remote_copy.cc#L382-L390) — `TIR_REGISTER_TL_TILE_OP(PutOp, put)` / `(GetOp, get)`。

**可运行示例**（最小两 rank 互拷）：

- [`examples/distributed/primitives/example_put_block.py:25-30`](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/examples/distributed/primitives/example_put_block.py#L25-L30) — 用 `T.get_rank() / T.get_num_ranks()`，`dst_pe=rank[0] ^ 1`（与配对 rank 互换数据），`put_block` 搬运 `block_M` 个元素。

#### 4.1.4 代码实践

**实践目标**：把「本地拷贝」和「远程拷贝」区分清楚，亲眼看到 `dst_pe=-1` 时 lowering 不做远程重映射。

**操作步骤**：

1. 打开 `examples/distributed/primitives/example_put_block.py`，把 `dst_pe=rank[0] ^ 1` 临时改成 `dst_pe=-1`。
2. 单 rank（`--num-processes 1`）跑不起对称堆，所以这次只做**源码阅读**：调用 `kernel.get_kernel_source()`，在生成的 CUDA 里搜索 `cp_block`。
3. 对比改前/改后两份源码里 `cp_block` 调用的第二个实参（目的地址）：
   - `dst_pe=-1`：直接是本地 `dst` 的 `address_of`。
   - `dst_pe=rank^1`：变成 `tl::get_remote_base_ptr(...) + (tl::get_uintptr_t(本地dst地址) - tl::get_remote_base_ptr(本地rank))`。

**需要观察的现象**：`dst_pe` 是否为 `-1`，直接决定 lowering 是否插入 `get_remote_base_ptr / get_uintptr_t` 这两个查表调用。

**预期结果**：`dst_pe=-1` 时生成的代码不含 `get_remote_base_ptr`；`dst_pe=具体 rank` 时含。实际多 GPU 运行结果**待本地验证**（本讲编写环境无多卡）。

#### 4.1.5 小练习与答案

**练习 1**：`put_block` 和 `put_warp` 在 C++ 里是同一个 Op 吗？靠什么区分？

> **答案**：是同一个 Op（`tl.tileop.put` → `PutOp`）。靠 `args[5]` 的 `scope` 字符串（`"block"` / `"warp"`）区分，`Lower` 据此选 `cp_block<N>` 或 `cp_warp<N, unroll, vec>` 模板。

**练习 2**：如果把 `dst_pe` 传成一个**运行时才确定**的变量（如 `rank[0] ^ 1`），`is_distributed()` 还能正确返回 `true` 吗？

> **答案**：能。`is_distributed()` 的判定是「`dst_pe` 不是整型常量 `-1`」。运行时变量不是 `IntImm(-1)`，所以返回 `true`，走远程重映射分支。这正是示例里 `dst_pe=rank[0] ^ 1` 能工作的原因。

---

### 4.2 unroll_factor 与 aggressive vectorize

#### 4.2.1 概念说明

`put_warp / get_warp` 比块级版本多了两个吞吐旋钮：

- **`unroll_factor`**（默认 4）：每个 lane 每轮迭代搬运多少个元素。它把 warp 内 32 个 lane 的搬运展开成「一次取 `32 * unroll_factor` 个元素」的循环，减少循环开销、提高指令级并行。
- **`enable_aggressive_vectorize`**（默认 `False`）：开启后把元素指针 reinterpret 成 `int4`（16 字节宽），按 16 字节粒度搬运，单条 load/store 吞吐更高。前提是源/目的地址 16 字节对齐、且 `N * sizeof(dtype)` 是 16 的倍数。

> 直觉：warp 搬运的本质是「32 个 lane 协作搬一段连续内存」。`unroll_factor` 横向铺开（每个 lane 多搬几个），`aggressive vectorize` 纵向加宽（每次搬 16 字节而不是 1 个元素）。二者都能提高带宽利用率，但加宽对对齐有硬性要求。

#### 4.2.2 核心流程

`cp_warp` 模板的搬运循环（伪代码，以 `enable_aggressive_vectorize=false` 为例）：

```
lane_id = threadIdx.x % 32
kLoopStride = 32 * UNROLL_FACTOR          # 一轮覆盖的元素数
for i in [lane_id, (N/kLoopStride)*kLoopStride, step=kLoopStride):
    # 展开 UNROLL_FACTOR 次：每个 lane 这一轮搬 UNROLL_FACTOR 个元素
    for j in [0, UNROLL_FACTOR):
        unrolled[j] = LD(src + i + j*32)   # 先集中读
    for j in [0, UNROLL_FACTOR):
        ST(dst + i + j*32, unrolled[j])    # 再集中写
# 处理不能被 kLoopStride 整除的尾巴
for i in [尾起点+lane_id, N, step=32):
    ST(dst+i, LD(src+i))
```

开 `aggressive vectorize` 时，`N` 个 `dtype_t` 被换算成 `N_int4 = sizeof(dtype_t)*N/16` 个 `int4`，循环结构不变，只是搬运单位从「1 个元素」变成「16 字节」。

`cp_block` 不走这条路，而是走 `nvshmem_cp_threadgroup`：先尝试 16B 对齐成块搬，再 8B、4B、2B、1B 逐级降级，直到搬完。`myIdx` 是线程在 block 内的线性编号、`groupSize` 是整个 block 的线程数。

#### 4.2.3 源码精读

**`cp_warp_impl`——warp 级展开搬运的核心循环**：

- [`src/tl_templates/cuda/copy.h:201-220`](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/tl_templates/cuda/copy.h#L201-L220) — `constexpr int kLoopStride = 32 * UNROLL_FACTOR;`，先 `#pragma unroll` 集中读 `UNROLL_FACTOR` 个值，再集中写；尾巴用步长 32 的循环兜底。注意它用 `LD_FUNC=ld_nc_global`（non-coherent load，绕 L1 cache 的流式读）和 `ST_FUNC=st_na_global`（non-allocating store），这正是 CP-engine 路线的「自己 load/store」本色。

**`cp_warp`——aggressive vectorize 分支**：

- [`src/tl_templates/cuda/copy.h:222-242`](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/tl_templates/cuda/copy.h#L222-L242) — 文档注释明确写了「需要源/目的 16 字节对齐、`N*sizeof(dtype)` 是 16 的倍数」；开启时把指针 cast 成 `int4*`、把元素数换算成 `N_int4 = sizeof(dtype_t)*N/16`，再调同一个 `cp_warp_impl`。

**`cp_block` → `nvshmem_cp_block` → `nvshmem_cp_threadgroup`——块级多级分块搬运**：

- [`src/tl_templates/cuda/copy.h:277-370`](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/tl_templates/cuda/copy.h#L277-L370) — `nvshmem_cp_threadgroup` 按 16B→8B→4B→2B→1B 逐级尝试对齐成块搬运，每级 `for (i = myIdx; i < nelems; i += groupSize)`；代码注释指向了 NVSHMEM 上游的 `nvshmemi_memcpy_threadgroup`，说明 block 级策略就是借鉴 NVSHMEM 的线程组搬运算法。
- [`src/tl_templates/cuda/copy.h:380-404`](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/tl_templates/cuda/copy.h#L380-L404) — `cp_block<N>` 直接转发给 `nvshmem_cp_block<N>`，后者把 `myIdx=整个block线性tid`、`groupSize=block线程数` 传进 `nvshmem_cp_threadgroup`。注意 `cp_block` 有三个重载（双指针 / 远端是 uint64 / 本地是 uint64），分别对应 put/get 的本地端、远端两种组合。

**Python 端带 unroll 的示例**：

- [`examples/distributed/primitives/example_put_warp.py:28-34`](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/examples/distributed/primitives/example_put_warp.py#L28-L34) — 先用 `T.get_thread_binding(0) // 32` 算出 warp 编号，再把 `block_M` 按 warp 数等分，每个 warp 调一次 `put_warp(..., size=warp_copy_size, unroll_factor=4)`。

#### 4.2.4 代码实践

**实践目标**：理解 `unroll_factor` 改变的是「每个 lane 每轮搬几个」，而不是搬运总量。

**操作步骤**：

1. 阅读上面 `cp_warp_impl`，确认 `kLoopStride = 32 * UNROLL_FACTOR`。
2. 在 `example_put_warp.py` 里把 `unroll_factor=4` 改成 `unroll_factor=1` 和 `unroll_factor=8`，分别 `kernel.get_kernel_source()` 看生成的 `cp_warp<N, 1, ...>` / `cp_warp<N, 8, ...>` 模板实参。
3.（可选，需多卡）用 `kernel.get_profiler().do_bench()` 对比三者的延迟。

**需要观察的现象**：模板第二个参数随 `unroll_factor` 变化；搬运的元素总数不变（都是 `warp_copy_size`）。

**预期结果**：`unroll_factor` 越大，循环轮数越少、指令级并行越高，但寄存器占用也越高（`unrolled_values[UNROLL_FACTOR]` 数组变大）。性能甜点通常在 4 附近。实测数据**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：`put_block` 的 `unroll_factor` 写成 0，会编译报错吗？

> **答案**：不会。因为 `scope=="block"` 走的是 `tl::cp_block<N>` 模板，**根本不读 `unroll_factor`**（见 `remote_copy.cc:98-99`，block 分支的 `ss` 流里没有 `unroll_factor`）。`unroll_factor` 只在 `scope=="warp"` 拼出的 `cp_warp<N, unroll_factor, vec>` 里用到。

**练习 2**：开启 `enable_aggressive_vectorize` 后，搬运 `N` 个 `float32` 元素，实际进入 `cp_warp_impl` 的「元素数」是多少？

> **答案**：`N_int4 = sizeof(float32) * N / 16 = 4N/16 = N/4`。即每个 `int4`（16 字节）打包 4 个 float32，元素数缩为原来的 1/4，但搬运的字节总量不变。

---

### 4.3 wait_eq / wait_ne 条件等待与对称堆寻址

#### 4.3.1 概念说明

`put_block` 是「发射即返回」的非阻塞搬运——线程把数据 store 进远端地址后就继续往下跑，**不保证对端已经看到**。要让消费端安全读到数据，需要一个「完成通知」机制。CP-engine 路线提供的条件等待原语就是干这个的：

- `wait_eq(value, expected, peer=-1)`：自旋直到 `*value == expected`。
- `wait_ne(value, expected, peer=-1)`：自旋直到 `*value != expected`。
- 另有 `wait_ge / wait_le / wait_gt / wait_lt` 对应 `>= / <= / > / <`。

典型「生产者—消费者」模式：生产者 `put` 数据后，再往消费端的 `flag` 写一个约定值；消费端 `wait_eq(flag, 约定值)` 自旋到值出现，就知道数据到了。

`wait_*` 同样支持 `peer`：`peer=-1` 等本地地址、`peer=具体 rank` 等远端地址（把地址重映射到对端对称堆）。

#### 4.3.2 核心流程：对称堆寻址的数学

CP-engine 路线能跨 PE 寻址，依赖一个不变量：**所有 PE 的对称堆是同构的**——同一段逻辑数据在每个 PE 上的「相对自己堆基址的偏移」完全相同。因此：

设本地堆基址为 `base[me]`，对端堆基址为 `base[peer]`，某个本地地址 `addr` 相对本地基址的偏移为：

\[
\text{offset} = \text{addr} - \text{base}[\text{me}]
\]

那么它在 `peer` 上的对应地址就是：

\[
\text{remote\_addr} = \text{base}[\text{peer}] + \text{offset}
\]

代码里用三个内置算子实现：

- `get_rank()`：取自己的 rank（即 `me`）。
- `get_remote_base_ptr(r)`：取 rank `r` 的堆基址（查 `meta_data` 表）。
- `get_uintptr_t(ptr)`：把任意指针 cast 成 `uint64_t`，用来做减法算偏移。

于是远程地址的 lowering 公式就是：

\[
\text{remote\_addr} = \text{get\_remote\_base\_ptr}(\textit{peer}) + \big(\text{get\_uintptr\_t}(\text{addr}) - \text{get\_remote\_base\_ptr}(\text{get\_rank}())\big)
\]

`wait_*` 在拿到这个远程地址后，用一条 `volatile` 全局 load 在它上面自旋（spin-loop），直到条件满足。

#### 4.3.3 源码精读

**对称堆基址表（设备端）**：

- [`src/tl_templates/cuda/distributed.h:5-18`](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/tl_templates/cuda/distributed.h#L5-L18) — `extern __constant__ uint64_t meta_data[1024]`；`get_rank()→meta_data[0]`、`get_num_ranks()→meta_data[1]`、`get_remote_base_ptr(rank)→meta_data[2+rank]`、`get_uintptr_t(ptr)` 就是 `reinterpret_cast<uint64_t>(ptr)`。

**PutOp 的远程地址重映射**（4.1.3 已贴过 Lower，这里聚焦公式）：

- [`src/op/remote_copy.cc:105-118`](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/op/remote_copy.cc#L105-L118) — `offset_to_base = get_uintptr_t(dst_addr) - get_remote_base_ptr(local_rank)`；`new_args.push_back(get_remote_base_ptr(dst_pe) + offset_to_base)`。这正是上面公式的逐行落地。

**WaitOp 的构造、判定与 Lower**：

- [`src/op/sync.cc:112-119`](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/op/sync.cc#L112-L119) — `WaitOp` 解析 4 个参数：`relation`（EQ/NE/GE/LE/GT/LT 的 int 编号）、`addr`、`expected`、`peer`。
- [`src/op/sync.cc:121-124`](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/op/sync.cc#L121-L124) — `is_distributed()`：`peer != -1` 即分布式，与 PutOp 同样的判定口径。
- [`src/op/sync.cc:126-153`](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/op/sync.cc#L126-L153) — `WaitOpNode::Lower`：把 `relation` 映射成字符串 `eq/ne/ge/le/gt/lt`，拼出 `tl::wait_eq` 等；分布式分支用**完全相同**的偏移公式把 `addr` 重映射到 `peer` 的对称堆。
- [`src/op/sync.cc:172-175`](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/op/sync.cc#L172-L175) — `TIR_REGISTER_TL_TILE_OP(WaitOp, wait)` 注册，4 输入。

**Python 端 `wait_eq / wait_ne` 与 `BinaryRelation` 枚举**：

- [`tilelang/language/distributed/common.py:121-127`](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/distributed/common.py#L121-L127) — `BinaryRelation` 把 EQ=0、NE=1、…、LT=5 编号，对应 C++ `relation_str[]`。
- [`tilelang/language/distributed/common.py:130-137`](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/distributed/common.py#L130-L137) — `wait_eq / wait_ne`：注意它**自己**对 `value` 调了 `address_of(value)`，所以调用时应传 `value=flag[0]`（一个 `BufferLoad`），而不是 `address_of(flag[0])`。

**设备端 `wait_*` 自旋实现**：

- [`src/tl_templates/cuda/sync.h:187-195`](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/tl_templates/cuda/sync.h#L187-L195) — `wait_eq`：`#pragma unroll 1 while (ld_volatile_global(flag_ptr) != val);`。`unroll 1` 故意阻止编译器把自旋优化掉，`ld_volatile_global` 保证每次都真正去全局内存读（否则可能被缓存「卡」在旧值上）。
- [`src/tl_templates/cuda/sync.h:197-245`](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/tl_templates/cuda/sync.h#L197-L245) — `wait_ne / wait_ge / wait_le / wait_gt / wait_lt` 同构，只改循环条件。

**基址表的注入（主机端 → 设备 `meta_data`）**：

- [`tilelang/utils/allocator.py:95-98`](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/utils/allocator.py#L95-L98) — 注释写明表布局：`[local_rank, num_local_ranks, peer0_base_ptr, peer1_base_ptr, ...]`，与设备端 `meta_data[0/1/2+rank]` 一一对应。
- [`tilelang/utils/allocator.py:157-161`](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/utils/allocator.py#L157-L161) — 每个 PE 用 `cudaMalloc` 建对称堆，通过 IPC handle 互开到本地（`buffer_ptrs`），填进 `self._table`。
- [`tilelang/jit/kernel.py:465-485`](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/jit/kernel.py#L465-L485) — `JITKernel.initialize(allocator=...)` 调 adapter 的 `init_table(table.data_ptr(), table_size, stream)`，把这张表拷进设备 `__constant__ meta_data`。**没有这一步，`get_remote_base_ptr` 查到的全是 0，远程拷贝必错。**

**codegen 端**：只有当 kernel 用到分布式算子时才 `#include` 分布式头、声明 `meta_data`：

- [`src/target/codegen_cuda.cc:326-331`](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/target/codegen_cuda.cc#L326-L331) — `if (use_distributed_)` 时 include `distributed.h / sync.h / ldst.h` 并 `extern "C" __constant__ uint64_t meta_data[1024];`。
- [`src/target/codegen_cuda.cc:2879-2898`](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/target/codegen_cuda.cc#L2879-L2898) — 把 `get_rank / get_num_ranks / get_remote_base_ptr / get_uintptr_t` 打印成 `tl::xxx(...)` 文本；注释说明 `put/get/wait` 是 TileOperator，经 `remote_copy.cc` 降级成 `call_extern("tl::cp_warp/cp_block/...", ...)`，由 `CodeGenC` 通用路径打印。

#### 4.3.4 代码实践

**实践目标**：用源码阅读验证「远程地址 = 对端基址 + 本地偏移」，并确认 `wait_eq` 是 volatile 自旋。

**操作步骤**：

1. 在 `example_put_block.py` 里 `if local_rank == 0: print(kernel.get_kernel_source())`，运行（或直接静态编译看源码），在生成的 CUDA 里找到 `put_block` 对应的 `tl::cp_block<...>(...)` 调用。
2. 把它的第二个实参（远端目的地址）抄下来，对照 `remote_copy.cc:105-118` 逐项标注：哪段是 `get_remote_base_ptr(peer)`、哪段是 `get_uintptr_t(dst)-get_remote_base_ptr(me)`。
3. 在 `src/tl_templates/cuda/sync.h:187-195` 确认 `wait_eq` 用的是 `ld_volatile_global` + `#pragma unroll 1`。

**需要观察的现象**：远端地址表达式里同时出现 `get_remote_base_ptr(peer)` 和 `get_remote_base_ptr(get_rank())`，二者相减正好消掉本地基址、留下纯偏移。

**预期结果**：源码中远程地址恒可拆成「对端基址 + 偏移」两部分，与公式吻合。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `wait_eq` 的自旋循环要用 `ld_volatile_global` 而不是普通 load？为什么加 `#pragma unroll 1`？

> **答案**：普通 load 可能被编译器或硬件缓存「锁」在旧值上（flag 是别的 PE 异步写的，本地 cache 不知道何时失效），导致永远自旋。`ld_volatile_global` 强制每次真去全局内存取最新值。`#pragma unroll 1` 禁止展开，避免编译器把循环优化成只读一次。

**练习 2**：忘记调 `kernel.initialize(allocator=...)` 就直接跑分布式 kernel，会发生什么？

> **答案**：设备 `meta_data` 表未初始化（全 0），`get_remote_base_ptr(peer)` 返回 0，远程地址算出来是 `0 + 偏移`，几乎必然是非法地址，kernel 会段错误或写飞。`initialize` 是 CP-engine 路线**必须**的一步。

---

## 5. 综合实践：实现一个 ring-shift

**任务**：仿照 `examples/distributed/example_simple_shift.py` 的思路（每个 PE 把本地一段数据发给「下一个」PE），但改用 **CP-engine 路线**（`put_block` + `wait_eq`）实现，并在设备端用 `wait_eq` 等待完成。

> 注意：`example_simple_shift.py` 实际用的是 NVSHMEM 路线的 `T.putmem_nbi_block`（见 [`example_simple_shift.py:21`](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/examples/distributed/example_simple_shift.py#L21)）。本实践要求你换成 CP-engine 路线，并加上设备端 `wait_eq` 通知。

### 5.1 设计

- 每个 PE 持有三段对称显存：`src`（要发出的数据）、`dst`（接收缓冲）、`flag`（1 元素的完成标志，`uint64`）。
- 数据流：PE `r` 把 `src` 发给 PE `(r+1) % n` 的 `dst`，再把一个信号值（自己 rank）写给 `(r+1)` 的 `flag`。
- 等待：PE `r` 用 `wait_eq(flag, expected=(r-1+n)%n)` 自旋，直到上一个 PE 把它的 rank 写进自己的 `flag`，此时 `dst` 已就绪。
- 收尾：`T.fence_sys()` 保证 store 全局可见后再读 `dst`。

### 5.2 参考实现（示例代码）

以下 kernel 基于现有原语编写，**结构正确、可编译**，但多 GPU 实测数值**待本地验证**：

```python
# 示例代码：ring_shift_kernel，基于 example_put_block.py 改写
import tilelang
import tilelang.language as T


def ring_shift_kernel(M, num_rank, block_M, threads):
    @T.prim_func
    def main(
        src: T.Tensor((M), "float32"),
        dst: T.Tensor((M), "float32"),
        flag: T.Tensor((1), "uint64"),   # 完成标志，初值 0
    ):
        with T.Kernel(T.ceildiv(M, block_M), threads=threads) as (bx):
            rank = T.alloc_local([1], "uint64")
            npe = T.alloc_local([1], "uint64")
            nxt = T.alloc_local([1], "uint64")
            prv = T.alloc_local([1], "uint64")
            rank[0] = T.get_rank()
            npe[0] = T.get_num_ranks()
            nxt[0] = (rank[0] + 1) % npe[0]            # 下一个 PE
            prv[0] = (rank[0] + npe[0] - 1) % npe[0]   # 上一个 PE（期望信号值）

            # (1) 数据：本地 src -> 下一个 PE 的 dst
            T.put_block(
                src=T.address_of(src[bx * block_M]),
                dst=T.address_of(dst[bx * block_M]),
                size=block_M,
                dst_pe=nxt[0],
            )
            # (2) 信号：把'期望值=prv'写给下一个 PE 的 flag
            #     用一个对称的 sig 缓冲携带本 rank，见主机端注释
            #     这里简化：直接把 prv[0]（即下一个 PE 期望看到的值）写过去
            T.put_block(
                src=T.address_of(prv[0]),   # = 下一个 PE 的 prv
                dst=T.address_of(flag[0]),
                size=1,
                dst_pe=nxt[0],
            )
            # (3) 等待：等上一个 PE 把'我的 prv'写进我的 flag
            T.wait_eq(
                value=flag[0],              # helper 内部会 address_of
                expected=prv[0],
            )
            T.fence_sys()

    return main
```

主机端骨架（参考 `example_put_block.py` 的 allocator / initialize / all_gather 校验段）：

```python
# 示例代码：主机端
from tilelang.distributed import init_dist
import torch, torch.distributed as dist

rank, num_ranks, group = init_dist(local_rank, num_local_ranks)
allocator = tilelang.get_allocator(
    size=2**25, device="cuda", is_distributed=True,
    local_rank=local_rank, num_local_ranks=num_local_ranks, group=group,
)
kernel = tilelang.compile(ring_shift_kernel(M, num_ranks, BLOCK_M, threads))
kernel.initialize(allocator=allocator)          # 必须注入基址表

src = tilelang.tensor((M,), torch.float32, allocator=allocator).normal_()
dst = tilelang.tensor((M,), torch.float32, allocator=allocator)
flag = tilelang.tensor((1,), torch.uint64, allocator=allocator)   # 初值 0

dist.barrier(group)
kernel(dst, src, flag)
torch.cuda.synchronize(); dist.barrier(group)

# 校验：dst 应等于'上一个 PE'的 src
dst_torchs = [torch.empty_like(src) for _ in range(num_local_ranks)]
dist.all_gather(dst_torchs, src, group)
expected = dst_torchs[(local_rank + num_local_ranks - 1) % num_local_ranks]
assert torch.allclose(expected, dst, atol=1e-6, rtol=1e-6)
```

### 5.3 操作步骤与预期

1. **理解信号语义**：PE `r` 写给下一个 PE 的值是「下一个 PE 的 `prv`」，即 `(r+1-1+n)%n = r`……等一下，仔细算：下一个 PE 是 `r+1`，它的 `prv = (r+1-1+n)%n = r`。所以 PE `r` 应该把 **`r`**（自己 rank，也即下一个 PE 的 `prv`）写过去。上面 kernel 里 `prv[0]` 在 PE `r` 上等于 `(r-1+n)%n`，**这不是**下一个 PE 想看到的值。请作为思考题修正：源端应写 `rank[0]`（自己），目的端应期望 `prv[0]`。
2. **修正后**：把第 (2) 步的 `src=T.address_of(prv[0])` 改成 `src=T.address_of(rank[0])`，第 (3) 步 `expected=prv[0]` 保持不变。此时语义自洽：PE `r` 写 `r` 给 `r+1` 的 flag；PE `r` 等 flag 变成 `prv=(r-1+n)%n`，即等 `r-1` 写过来。
3. 在双卡环境（`--num-processes 2`）运行，2 个 PE 构成一个最小环（`0→1, 1→0`）。
4. 观察 `dst == 上一个 PE 的 src` 是否成立。

**预期结果**：每个 PE 的 `dst` 收到的是**上一个** PE 的 `src`（环移一位），`wait_eq` 返回后数据已就绪。实测数值与多卡拓扑相关，**待本地验证**。

> 如果你没有多卡环境，可退化为「源码阅读型实践」：静态编译上面的 kernel（`tilelang.compile` + `kernel.get_kernel_source()`），在生成的 CUDA 里找到 `tl::cp_block`（两次：数据 + 信号）与 `tl::wait_eq`，标注出远程地址表达式中的 `get_remote_base_ptr(nxt)` 与偏移项，验证 4.3.2 的寻址公式。

## 6. 本讲小结

- CP-engine 路线与 NVSHMEM 路线**并行存在**：前者 Python 端是 `put_block/put_warp/get_block/get_warp` + `wait_*`，C++ 端是 `PutOp/GetOp/WaitOp`（`remote_copy.cc` / `sync.cc`），设备端是 `tl::cp_warp / cp_block / wait_*` 模板；后者直接复用 `nvshmem*` C API。二者共用对称堆与基址表 `meta_data`。
- 四个搬运原语靠 `scope`（`"block"` / `"warp"`）区分；`dst_pe/src_pe == -1` 即本地拷贝、否则远程。`is_distributed()` 仅凭「是否 `IntImm(-1)`」判定。
- 远程地址的统一公式：`remote_addr = get_remote_base_ptr(peer) + (get_uintptr_t(addr) - get_remote_base_ptr(get_rank()))`，本质是「对端基址 + 本地偏移」，依赖所有 PE 对称堆同构。
- `unroll_factor` 只对 warp 级 `cp_warp` 生效（`kLoopStride = 32*UNROLL_FACTOR`）；`enable_aggressive_vectorize` 把搬运单位换成 16 字节 `int4`；块级 `cp_block` 走 `nvshmem_cp_threadgroup` 的 16B/8B/4B/2B/1B 自动分块。
- `wait_eq/wait_ne/...` 是 volatile 自旋（`ld_volatile_global` + `#pragma unroll 1`），配合 `put` 实现「生产者写数据+写信号、消费者等信号」的完成通知。
- `kernel.initialize(allocator=...)` 是 CP-engine 路线的必经一步：把 allocator 构造的基址表 `[rank, num_ranks, peer 基址...]` 拷进设备 `__constant__ meta_data`，漏了它远程寻址全错。

## 7. 下一步学习建议

- **u6-l4（分布式运行时：pynvshmem 与启动）**：本讲的 `init_dist / get_allocator / kernel.initialize` 都属于运行时层，下一讲会系统讲 `launch.sh` 多进程启动、`pynvshmem` 对称堆张量与 `init_distributed` 的返回结构，把本讲的主机端骨架补全。
- **u6-l5（IPC 张量与 tilescale_ext 内存管理）**：本讲多次提到「IPC handle 互开」「对称堆基址表」，下一讲深入 `tilescale_ext` 的 `_create_ipc_handle / _sync_ipc_handles` 与 `tensor_from_ptr` 所有权模型，看清基址表是怎么「攒」出来的。
- **u6-l7（分布式实战：allgather/all2all/summa）**：把本讲的 `put_block / get_block / wait_eq` 组合起来，就能搭出 allgather、SUMMA 等经典集合通信 kernel，建议结合 `examples/distributed/example_allgather.py`、`example_summa.py` 阅读。
- 若想横向对照，可重读 **u6-l2** 的 NVSHMEM 原语表，体会「同一对称堆抽象、两套搬运实现」的设计取舍。
