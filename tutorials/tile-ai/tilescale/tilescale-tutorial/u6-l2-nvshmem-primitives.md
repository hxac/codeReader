# NVSHMEM 多设备通信原语

> 本讲是分布式单元（Unit 6）的第二篇，承接 [u6-l1 分布式总览与 HDA 愿景](u6-l1-distributed-overview.md) 中建立的「多进程 + NVSHMEM」务实路线，并用到 [u2-l5 数据搬运：T.copy 与 T.view](u2-l5-copy-view.md) 里关于「搬运范围由参数推导、最终发出 intrin」的认知。

## 1. 本讲目标

学完本讲，你应当能够：

1. 说清 **NVSHMEM** 是什么、为什么分布式 GPU 编程需要它，以及**对称堆（symmetric heap）**这一核心抽象。
2. 掌握 TileLang 提供的两套多设备原语：**NVSHMEM 路线**（`get_pe`/`putmem`/`signal`/`barrier`，落在 `nvshmem*`/`nvshmemx*` 调用）与 **CP-engine 路线**（`get_rank`/`put_block`/`wait_eq`，落在 `tl::cp_block` 模板），并理解它们共用同一套「对称寻址」机制。
3. 区分 `barrier_all` / `sync_all` / `quiet` / `fence` 四种同步原语在**完成（completion）**与**定序（ordering）**语义上的差别。
4. 用 `signal_op` / `signal_wait_until` / `putmem_signal*` 写出「生产者—消费者」式的远程通知，并看懂 `broadcast` / `fcollect` 集合通信原语。
5. 跟踪一条 `T.putmem_nbi_block(...)` 从 Python 到生成 CUDA 源码 `nvshmemx_putmem_nbi_block(...)` 的完整调用链。

---

## 2. 前置知识

### 2.1 为什么要 NVSHMEM

普通 CUDA 程序里，一个线程只能直接读写**本 GPU** 的显存。当数据分布在多张 GPU（多个 PE，Processing Element）上时，传统做法是在 host 端用 NCCL/Gloo 等集合通信库发起通信——**通信发生在 kernel 之外**，数据要先回到 host 调度，再发起一次 device 间的拷贝，开销大且难以和计算重叠。

**NVSHMEM** 是 NVIDIA 的 PGAS（Partitioned Global Address Space）通信库：它给每张 GPU 分配一块**对称堆**——所有 PE 上同一「对称偏移」指向逻辑上同一地址。于是 device kernel 里的一个线程，就能像访问普通指针一样，用一条 `nvshmem_put`/`nvshmem_get` 指令把数据搬到**任意远端 PE** 的对称堆里。通信被「下沉」进了 kernel，可以和计算 overlap。

> 关键直觉：NVSHMEM 把「多 GPU」抽象成一个**大的对称地址空间**，让 device 代码拥有**一侧主动（one-sided）**的远程访存能力。

### 2.2 PE 与 rank

在 TileScale 里，**PE**（NVSHMEM 术语）与 **rank**（进程术语）是同一个东西的两种叫法，都指「第几个参与通信的进程/设备」。本讲会遇到两套获取自身编号的原语，它们分属两条路线，但语义一致：

| 路线 | 获取自身编号 | 获取总数 | 生成代码 |
|------|------------|---------|---------|
| NVSHMEM 路线 | `T.get_pe()` | `T.get_pe_num()` | `nvshmem_my_pe()` / `nvshmem_n_pes()` |
| CP-engine 路线 | `T.get_rank()` | `T.get_num_ranks()` | `tl::get_rank()` / `tl::get_num_ranks()` |

### 2.3 回顾：TileLang 的原语即 intrin

[u2-l5](u2-l5-copy-view.md) 已建立认知：前端的 `T.*` 原语大多只是拼装一条 `tir.call_intrin`，真正的硬件指令要等编译 pass 才生成。本讲的分布式原语也是如此——Python 层只是「登记一个 op 名 + 传参」，真正变成 `nvshmem_*` C 调用文本的工作，发生在 [u3-l5 代码生成与目标后端](u3-l5-codegen-backends.md) 讲过的 **codegen_cuda** 这台「源码打印机」里。

---

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [tilelang/language/distributed/multi_device/nvshmem.py](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/distributed/multi_device/nvshmem.py) | **NVSHMEM 路线**的 Python 前端：`get_pe`/`putmem*`/`getmem*`/`barrier*`/`sync*`/`quiet`/`fence`/`signal*`/`broadcast`/`fcollect` |
| [tilelang/language/distributed/common.py](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/distributed/common.py) | **CP-engine 路线**的 Python 前端：`get_rank`/`put_block`/`get_block`/`put_warp`/`get_warp`/`wait_eq` 等（默认导出，不依赖 NVSHMEM 头） |
| [src/op/distributed.cc](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/op/distributed.cc) | NVSHMEM 路线 `tl.*` builtin 的 **C++ Op 注册**（`GetPE`/`PutmemNbiBlock`/`BarrierAll`/`SignalOp`…） |
| [src/op/sync.cc](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/op/sync.cc) | CP-engine 路线的 `WaitOp`（`tl.tileop.wait`）实现与 lowering，以及块级 `barrier_blocks` |
| [src/op/remote_copy.cc](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/op/remote_copy.cc) | CP-engine 路线的 `PutOp`/`GetOp`（`tl.tileop.put/get`）实现与 lowering（生成 `tl::cp_block`/`tl::cp_warp`） |
| [src/target/codegen_cuda.cc](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/target/codegen_cuda.cc) | 把 NVSHMEM builtin **逐条翻译成 `nvshmem*`/`nvshmemx*` CUDA 源码文本**，并按需 `#include <nvshmem.h>` |
| [examples/distributed/primitives/](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/examples/distributed/primitives) | `example_put_block.py` / `example_get_block.py` / `example_sync.py` 等最小可运行示例 |

---

## 4. 核心概念与源码讲解

### 4.1 NVSHMEM 与对称堆：多设备通信的根基

#### 4.1.1 概念说明

本讲所有原语都建立在两个底层抽象上：

1. **对称堆（symmetric heap）**：每个 PE 在初始化时分配一块大小、布局相同的 GPU 显存（由 [u6-l4](u6-l4-pynvshmem-launch.md) 会讲到的 `pynvshmem` / `tilelang.get_allocator(is_distributed=True)` 创建）。其不变量是：**同一个对称偏移量，在所有 PE 上都指向「逻辑同一」的位置**。
2. **一侧主动访存（one-sided）**：发起 put/get 的 PE 不需要远端 PE 配合，直接写/读远端对称堆。

TileLang 把多设备通信暴露成**两套并行的原语**，二者最终都落到 NVSHMEM 远程拷贝的底座上：

- **NVSHMEM 路线**（`multi_device/nvshmem.py`）：薄封装，1:1 映射到 NVSHMEM C API，参数语义（字节计数、对称地址、PE 号）与原生 `nvshmem_*` 完全一致；功能最全（含 signal、broadcast、fcollect）。
- **CP-engine 路线**（`common.py`）：以 **元素计数** 为单位、面向 tile 的更高层封装，底层用 `tl::cp_block`/`tl::cp_warp` 模板实现，源码注释明确写道「block-level comm based on NVSHMEM-style copy」。

> 为什么默认导出的是 CP-engine 路线？看 [tilelang/language/distributed/__init__.py:3](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/distributed/__init__.py#L3) 的注释 `# Does not import NVSHMEM related by default`——NVSHMEM 头/库较重，按需导入，避免单机用户被牵连。

#### 4.1.2 核心流程：对称寻址——从本地指针到远程指针

两套原语在 C++ lowering 时解决的是同一个问题：**kernel 里拿到的是一个本地指针 `addr`，怎么把它翻译成「目标 PE 上的等价地址」？** 答案是利用对称堆不变量，做一次偏移换算：

\[
\text{remote\_addr}(\text{pe}) \;=\; \text{base}[\text{pe}] \;+\; \bigl(\text{addr} - \text{base}[\text{me}]\bigr)
\]

其中 `base[pe]` 是 PE `pe` 的对称堆基地址，`base[me]` 是本 PE 的基地址。因为对称堆在所有 PE 上同构，括号里的「本地偏移」在远端同样有效。三步：

1. 取本 PE 的 rank：`tl::get_rank()`；
2. 取本 PE 对称堆基地址：`tl::get_remote_base_ptr(me)`，再算出本地偏移 `tl::get_uintptr_t(addr) − base[me]`；
3. 远端地址 = `tl::get_remote_base_ptr(pe) + 偏移`。

`base[*]` 这张「远程基址表」是在运行时由 `kernel.initialize(allocator=...)` 注入的（见 [u6-l1](u6-l1-distributed-overview.md) 的「远程基址表通过 kernel.initialize 注入」），编译期只生成查表调用。

#### 4.1.3 源码精读

**NVSHMEM 路线获取自身 PE**——`get_pe` 只是把 `tl.GetPE` 这个 op 包成 intrin：

```python
def get_pe():
    """Get the processing element (PE) ID."""
    return tir.call_intrin("int32", tir.op.Op.get("tl.GetPE"))
```

见 [tilelang/language/distributed/multi_device/nvshmem.py:6-8](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/distributed/multi_device/nvshmem.py#L6-L8)：注意它返回 `"int32"` 类型、零输入。

**C++ 侧注册**：`distributed.cc` 用一个宏 `TIR_DEFINE_TL_BUILTIN` 把每个 op 名注册进 TVM 的 Op 注册表，并标记调用副作用为 `kOpaque`（编译器不假设它是纯函数）：

```cpp
TIR_DEFINE_TL_BUILTIN(GetPE).set_num_inputs(0).set_attr<TCallEffectKind>(
    "TCallEffectKind", Integer(CallEffectKind::kOpaque));
```

见 [src/op/distributed.cc:28-29](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/op/distributed.cc#L28-L29)。`set_num_inputs(0)` 表示零参；带参数的 op（如 `PutmemNbiBlock`）则用 `set_num_inputs(-1)` 表示变参（见 [src/op/distributed.cc:102-105](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/op/distributed.cc#L102-L105)）。

**codegen 把 op 翻译成 nvshmem 调用**：在 `codegen_cuda.cc` 的表达式访问器里，每命中一个 NVSHMEM op，就向输出流打印对应的 `nvshmem*` 文本，并置两个标志位 `use_distributed_=true; use_nvshmem_=true;`：

```cpp
} else if (op->op.same_as(tl::GetPE())) {
  this->use_distributed_ = true;
  this->use_nvshmem_ = true;
  os << "nvshmem_my_pe()";
```

见 [src/target/codegen_cuda.cc:2756-2759](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/target/codegen_cuda.cc#L2756-L2759)。`use_nvshmem_` 会在文件头部触发 `#include <nvshmem.h>` 与 `#include <nvshmemx.h>`（[src/target/codegen_cuda.cc:297-312](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/target/codegen_cuda.cc#L297-L312)）。

**对称寻址的 lowering 实现**（CP-engine 路线的 `PutOp`，最完整地展现了上面的三步公式）：

```cpp
if (is_distributed()) {
  PrimExpr dst_addr_expr = MakeRemappedAddress(T, dst_buffer, dst_indices);
  PrimExpr local_rank  = Call(DataType::Int(64), tl::get_rank(), {});
  PrimExpr local_base  = Call(DataType::Handle(), tl::get_remote_base_ptr(), {local_rank});
  PrimExpr offset_to_base =
      Sub(Call(DataType::Handle(), tl::get_uintptr_t(), {dst_addr_expr}), local_base);
  new_args.push_back(
      Call(DataType::Handle(), tl::get_remote_base_ptr(), {dst_pe}) + offset_to_base);
}
```

见 [src/op/remote_copy.cc:105-115](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/op/remote_copy.cc#L105-L115)。这正是 §4.1.2 公式的逐行翻译。`is_distributed()` 的判定很简单——目标 PE 不是字面量 `-1` 就算分布式（[src/op/remote_copy.cc:86-89](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/op/remote_copy.cc#L86-L89)），`-1` 表示「本地拷贝」。

#### 4.1.4 代码实践：从 Python 到 nvshmem 调用链追踪

1. **实践目标**：亲眼看到 `T.get_pe()` 最终变成 CUDA 源码里的 `nvshmem_my_pe()`，并理解 op 注册三段式（Python intrin → C++ Op 注册 → codegen 打印）。
2. **操作步骤**：
   - 阅读示例 [examples/distributed/example_nvshmem.py](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/examples/distributed/example_nvshmem.py)，其中 `mype[0] = T.get_pe()`（第 41 行）就是 NVSHMEM 路线取自身 PE。
   - 用 `grep` 在 `src/op/distributed.cc` 里数一下 `TIR_DEFINE_TL_BUILTIN` 出现的次数，与 `nvshmem.py` 里 `def` 的函数数对比，理解「Python 函数 ↔ C++ op」的 1:1 关系。
3. **需要观察的现象**：`example_nvshmem.py` 的 `tilelang_callback_cuda_postproc`（第 7-28 行）手工注入了一段含 `nvshmem_my_pe()` 的 CUDA；这说明 codegen 产出的就是原生 NVSHMEM C 调用。
4. **预期结果**：能口述「`T.get_pe()` → `tl.GetPE` → codegen `nvshmem_my_pe()`，同时拉起 `#include <nvshmem.h>`」这条链。
5. 实际运行需 NVSHMEM 初始化的多卡环境；源码阅读部分**单机即可完成**，运行部分**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `distributed/__init__.py` 默认不导入 NVSHMEM 路线？
**答案**：NVSHMEM 依赖 `nvshmem.h`/`nvshmem_device` 库与对称堆初始化，较重；单机/单卡用户无需多设备通信，默认只导出 CP-engine 路线（`common.py`）以减小导入代价与依赖面。需要时再显式 `from tilelang.language.distributed.multi_device import *`。

**练习 2**：`set_num_inputs(-1)` 在 TVM Op 注册里表示什么？为什么远程拷贝类 op 用它？
**答案**：`-1` 表示变参（接受任意数量输入）。远程拷贝 op 的参数个数随变体不同而不同（普通 put 4 参、signal put 7 参），统一用变参避免逐一声明。

---

### 4.2 putmem / getmem 远程拷贝原语

#### 4.2.1 概念说明

`putmem` / `getmem` 是 NVSHMEM 路线的**块数据远程拷贝**原语，直接对应 NVSHMEM C API：

- **put**：把**本 PE** 的数据写到**远端 PE** 的对称堆；
- **get**：把**远端 PE** 的数据读回**本 PE**。

它们的参数统一为 `(dest, src, nelems, pe)`，其中 `dest`/`src` 是对称地址（用 `T.address_of(...)` 取得），`nelems` 是**字节数**（注意不是元素数），`pe` 是对端 PE 号。

#### 4.2.2 核心流程：三组正交变体

每个 put/get 都由三个正交维度组合出多个变体，理解了维度就理解了全部变体：

| 维度 | 取值 | 含义 |
|------|------|------|
| **方向** | put / get | 写远端 / 读远端 |
| **完成时机** | 阻塞 / `nbi`（non-blocking implicit） | 阻塞版等本地完成才返回；`nbi` 版立即返回，完成时机交给后续 `quiet`/`barrier`/`sync` |
| **协作粒度** | （线程）/ `block` / `warp` | 无后缀=线程级；`_block`=整个 CTA 集体调用；`_warp`=整个 warp 集体调用 |

例如 `putmem_nbi_block` = 「block 粒度、非阻塞、写远端」。`_block`/`_warp` 后缀对应 NVSHMEM 的 `nvshmemx_*` 集体接口，要求该组内**所有线程都参与调用**（collective）。

> 与 CP-engine 路线的区别：`put_block(src, dst, size, dst_pe)`（[common.py:83](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/distributed/common.py#L83)）的 `size` 是**元素数**、且底层走 `tl::cp_block` 模板；而 `putmem_*` 的 `nelems` 是**字节数**、走原生 `nvshmem*`。两者面向不同抽象层级。

#### 4.2.3 源码精读

**Python 前端**——以 `putmem_nbi_block` 为例，注意 docstring 写清了 `nelems` 单位是字节：

```python
def putmem_nbi_block(dest, src, nelems, pe):
    """Put data from local memory to remote memory at block granularity without blocking.
    Args:
        dest: Symmetric address of the destination data object.
        src:  Symmetric address of the object containing the data to be copied.
        nelems: Number of elements to be transferred (in bytes).
        pe:  The PE ID of the destination PE.
    """
    return tir.call_intrin("handle", tir.op.Op.get("tl.PutmemNbiBlock"), dest, src, nelems, pe)
```

见 [tilelang/language/distributed/multi_device/nvshmem.py:101-109](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/distributed/multi_device/nvshmem.py#L101-L109)。`getmem_nbi_block` 同构（[nvshmem.py:66-74](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/distributed/multi_device/nvshmem.py#L66-L74)）。其余 `putmem`/`putmem_nbi`/`putmem_block`/`putmem_warp`/`putmem_nbi_warp` 与 get 族都是同模式的薄封装（[nvshmem.py:97-125](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/distributed/multi_device/nvshmem.py#L97-L125)）。

**C++ Op 注册**：

```cpp
TIR_DEFINE_TL_BUILTIN(PutmemNbiBlock)
    .set_num_inputs(-1)
    .set_attr<TCallEffectKind>("TCallEffectKind", Integer(CallEffectKind::kOpaque));
```

见 [src/op/distributed.cc:102-105](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/op/distributed.cc#L102-L105)。NVSHMEM 路线的 op 在 C++ 侧**只注册、不实现 lowering 逻辑**——它们是普通 builtin，直接由 codegen 处理（区别于 CP-engine 路线的 TileOperator，后者有自己的 `Lower()` 方法）。

**codegen 翻译为 nvshmemx 调用**：

```cpp
} else if (op->op.same_as(tl::PutmemNbiBlock())) {
  this->use_distributed_ = true;
  this->use_nvshmem_ = true;
  os << "nvshmemx_putmem_nbi_block(";
  this->PrintExpr(op->args[0], os); os << ", ";   // dest
  this->PrintExpr(op->args[1], os); os << ", ";   // src
  this->PrintExpr(op->args[2], os); os << ", ";   // nelems
  this->PrintExpr(op->args[3], os);               // pe
  os << ")";
}
```

见 [src/target/codegen_cuda.cc:2774-2785](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/target/codegen_cuda.cc#L2774-L2785)。参数顺序与 NVSHMEM C API 完全一致；`GetmemNbiBlock` 同理（[codegen_cuda.cc:2797-2807](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/target/codegen_cuda.cc#L2797-L2807)）。

#### 4.2.4 代码实践：对比两条路线生成的 CUDA

1. **实践目标**：直观看到 NVSHMEM 路线（`putmem_*`）与 CP-engine 路线（`put_block`）生成的 CUDA 源码差异。
2. **操作步骤**：
   - 读 [examples/distributed/primitives/example_put_block.py](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/examples/distributed/primitives/example_put_block.py)，其 kernel（第 14-32 行）用 `T.put_block(src=..., dst=..., size=block_M, dst_pe=rank[0] ^ 1)` 把本 rank 的数据写到对端 rank（`rank ^ 1` 让 rank 0↔1 互发）。
   - 在能跑的环境下，于 `local_rank == 0` 时打印 `kernel.get_kernel_source()`（示例第 48 行已这么做），在源码里搜索 `tl::cp_block`。
   - 把同样的搬运改写成 NVSHMEM 路线：`from tilelang.language.distributed.multi_device import putmem_nbi_block`，调用 `putmem_nbi_block(dst, src, block_M * 4, rank[0] ^ 1)`（float32 → 字节数 ×4），重新打印源码，搜索 `nvshmemx_putmem_nbi_block`。
3. **需要观察的现象**：CP-engine 版生成 `tl::cp_block<N>(...)`（模板调用）；NVSHMEM 版生成 `nvshmemx_putmem_nbi_block(...)`（原生 C 调用）并多出 `#include <nvshmem.h>`。
4. **预期结果**：两条路线都能正确搬运数据，但 CUDA 源码形态不同——一个走 TileLang 设备模板，一个走 NVSHMEM 原生 API。
5. 运行需 2 卡 + NVSHMEM 初始化；源码对比可先靠 `get_kernel_source()` 在单次编译后查看，**实际执行待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：`putmem_nbi_block` 里 `nelems=block_M*4` 的 `*4` 从哪来？
**答案**：`putmem_*` 的 `nelems` 单位是**字节**；示例数据是 float32（每元素 4 字节），搬运 `block_M` 个元素需 `block_M*4` 字节。CP-engine 路线的 `put_block` 用元素数，无需乘 4——这是两条路线最易踩的坑。

**练习 2**：`_block` 后缀的调用对线程有什么要求？
**答案**：`_block`/`_warp` 对应 NVSHMEM `nvshmemx_*` 集体接口，要求整个 CTA（block）或整个 warp 的**所有线程都参与调用**，否则行为未定义。普通无后缀版是单线程（thread）粒度。

---

### 4.3 barrier / sync / quiet / fence 同步语义

#### 4.3.1 概念说明

远程拷贝大多是**异步**的（尤其 `nbi` 变体），发起后并不保证数据已落地。要安全地读到对端写来的数据，必须显式同步。NVSHMEM 路线提供四个同步原语，理解它们的关键是区分两个概念：

- **完成（completion）**：数据真的到达目的地、对端可见；
- **定序（ordering）**：保证多个操作按发起顺序生效（但不保证完成）。

| 原语 | 作用域 | 保证完成？ | 保证定序？ | 典型用途 |
|------|--------|-----------|-----------|---------|
| `barrier_all` | 所有 PE | ✅（含远程更新） | ✅ | 最强同步：等所有 PE 的本地写 **和远程写** 都完成 |
| `sync_all` | 所有 PE | ⚠️ 仅本地写 | ✅ | 比 barrier 弱：**不保证** NVSHMEM 远程更新已完成 |
| `quiet` | 单 PE | ✅（本 PE 发起的所有对称操作） | ✅ | 等本 PE 发起的所有 put/get 完成（本地阻塞） |
| `fence` | 单 PE | ❌ | ✅ | 仅保证后续操作不会超越当前操作生效（轻量定序） |

> 最易错点：`sync_all` **不等** NVSHMEM 远程写完成。若你 `putmem_nbi` 之后直接 `sync_all` 就去读，数据可能还没到——应先 `quiet()` 再 `sync_all()`，或直接用 `barrier_all()`。

#### 4.3.2 核心流程：何时用哪个

伪代码决策：

```text
若需「全 PE 都到齐 + 所有远程写都落地」      → barrier_all()      （最重）
若只是「全 PE 都到齐」（已自行 quiet 过）    → sync_all()
若只关心「我自己发起的远程写都完成了」        → quiet()            （单 PE，常配 sync_all）
若只想「给后续操作排队、不要乱序」            → fence()            （最轻，不等完成）
```

`barrier_all` / `sync_all` 也有 `_block` / `_warp` 集体变体（`barrier_all_block`/`sync_all_warp` 等），语义同 §4.2 的粒度后缀。

#### 4.3.3 源码精读

**Python 前端**——四个原语 docstring 把语义差别说得很清楚，尤其 `sync_all` 的「does not ensure completion of remote memory updates」：

```python
def barrier_all():
    """Synchronizes all processing elements (PEs),
    ensuring completion of all previously issued memory stores and remote memory updates."""

def sync_all():
    """In contrast with `barrier_all`,
    `sync_all` only ensures completion and visibility of previously issued memory stores,
    and does not ensure completion of remote memory updates issued via NVSHMEM routines."""

def quiet():
    """Ensures completion of all operations on symmetric data objects issued by the calling PE."""

def fence():
    """Ensures ordering of delivery of operations on symmetric data objects."""
```

见 [tilelang/language/distributed/multi_device/nvshmem.py:26-29](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/distributed/multi_device/nvshmem.py#L26-L29)（barrier_all）、[nvshmem.py:40-45](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/distributed/multi_device/nvshmem.py#L40-L45)（sync_all）、[nvshmem.py:56-58](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/distributed/multi_device/nvshmem.py#L56-L58)（quiet）、[nvshmem.py:61-63](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/distributed/multi_device/nvshmem.py#L61-L63)（fence）。

**codegen 翻译**——四个原语 1:1 映射到 `nvshmem_*`：

```cpp
} else if (op->op.same_as(tl::BarrierAll())) {
  this->use_distributed_ = true; this->use_nvshmem_ = true;
  os << "nvshmem_barrier_all()";
} else if (op->op.same_as(tl::SyncAll())) {
  ...
  os << "nvshmem_sync_all()";
} else if (op->op.same_as(tl::Quiet())) {
  ...
  os << "nvshmem_quiet()";
} else if (op->op.same_as(tl::Fence())) {
  ...
  os << "nvshmem_fence()";
}
```

见 [src/target/codegen_cuda.cc:2808-2831](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/target/codegen_cuda.cc#L2808-L2831)。`_block`/`_warp` 变体映射到 `nvshmemx_barrier_all_block()`/`nvshmemx_sync_all_block()` 等（[codegen_cuda.cc:2812-2823](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/target/codegen_cuda.cc#L2812-L2823)）。

**CP-engine 路线的 `fence_sys`**：在 [examples/distributed/primitives/example_get_block.py:31](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/examples/distributed/primitives/example_get_block.py#L31) 里，`get_block` 之后调了 `T.fence_sys()`——这是 CP-engine 路线的**系统域 fence**（`tl::memory_fence_sys()`，见 [codegen_cuda.cc:2876-2878](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/target/codegen_cuda.cc#L2876-L2878)），保证 get 拉回的数据在本 GPU 跨线程可见，与 NVSHMEM 路线的 `fence()` 思想一致但分属不同 op 集。

#### 4.3.4 代码实践：诊断一个「数据没到」的同步错误

1. **实践目标**：理解为什么 `sync_all` 不能替代 `barrier_all`/`quiet`。
2. **操作步骤**：
   - 设想一段 kernel：PE0 `putmem_nbi_block(..., pe=1)`，然后 PE1 立刻读 `dest`。
   - 若在 put 与读之间只放 `sync_all()`，预测会发生什么；若换成 `quiet()` + `sync_all()` 或 `barrier_all()` 呢？
   - 阅读 [examples/distributed/primitives/example_sync.py](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/examples/distributed/primitives/example_sync.py)，注意它**没有在 kernel 内发远程拷贝**，而是靠 host 端 `dist.barrier(group)` + `torch.cuda.synchronize()` 来对齐两个进程——这是「host 侧屏障」的最简形态。
3. **需要观察的现象**：`example_sync.py` 演示的是通过 `return_peers=True` 直接拿到对端对称堆的 host 视图（[example_sync.py:23](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/examples/distributed/primitives/example_sync.py#L23)），同步完全靠 `dist.barrier`。
4. **预期结果**：能说清「kernel 内异步远程写 + 仅 `sync_all`」会读到旧值，正确做法是 `quiet` 后再 `sync_all`（或 `barrier_all`）。
5. **待本地验证**（需多卡）。

#### 4.3.5 小练习与答案

**练习 1**：`quiet` 和 `fence` 都作用于单个 PE，区别是什么？
**答案**：`quiet` **阻塞**到本 PE 发起的所有对称操作**完成**（数据真正落地）；`fence` 只保证操作的**定序**（后续操作不会先于当前操作生效），**不等待完成**，因而更轻量。

**练习 2**：为什么 `example_get_block.py` 在 `get_block` 后要加 `fence_sys()`？
**答案**：`get_block` 把远端数据拉回本 GPU，但跨线程/跨 SM 的可见性需要显式内存屏障保证；`fence_sys()`（系统域 fence）确保后续读取能看到这次 get 拉回的数据。

---

### 4.4 signal / wait 远程通知与 broadcast / fcollect 集合通信

#### 4.4.1 概念说明

单纯的 put/get 只搬数据，**不告诉对端「数据到了」**。NVSHMEM 提供**信号（signal）机制**实现远程通知，这是写「生产者—消费者」overlap 内核的关键：

- **`signal_op(sig_addr, signal, sig_op, pe)`**：原子地更新远端 PE `pe` 上的信号字 `sig_addr`，`sig_op` 指定更新方式（如 SET 赋值、ADD 累加）——**纯通知，不带数据**。
- **`putmem_signal_*(..., sig_addr, signal, sig_op, pe)`**：**搬数据 + 同时通知**——数据落地的同时更新远端信号字，是最高效的「带完成通知的 put」。
- **`signal_wait_until(sig_addr, cmp, value)`**：**本端阻塞等待**，直到本 PE 的信号字 `sig_addr` 满足比较条件 `cmp value`——消费者的等待侧。

典型「生产者—消费者」时序：

```text
生产者 PE0:   putmem_signal_nbi_block(data..., sig_addr, 1, SET, pe=1)  → 数据+信号飞向 PE1
消费者 PE1:   signal_wait_until(sig_addr, EQ, 1)                          → 阻塞直到信号==1
              读 data（此刻数据必定已到）
```

此外还有两个**集合通信**原语：

- **`broadcast` / `broadcastmem_block`**：把一个 PE 的数据广播给所有 PE（根 → 全体）。
- **`fcollect`**（fence-collect）：把所有 PE 各自贡献的一段数据**按 PE 序号拼接**收集到每个 PE 的目标缓冲（类似 `allgather` 但按贡献者排序）。

三者也都有 `_warp`/`_block` 集体粒度变体。

#### 4.4.2 核心流程：put + wait 时序

本讲实践任务要求画出「两个 PE 之间一次 put+wait 的时序」。以 `putmem_signal_nbi_block` + `signal_wait_until` 为例：

```text
时间轴 →

PE0 (生产者):  [发 putmem_signal_nbi_block] ──────────── (远程写进行中) ────┐
                                                                          │ 数据+信号抵达
PE1 (消费者):  ────── [signal_wait_until(EQ,1) 阻塞等待] ────[信号==1, 解阻塞] ──→ [读 data]
                                      ▲                                    ▲
                                 同步点①：进入等待                      同步点②：条件满足，对齐完成
```

两个同步点：① 消费者开始等待（自身阻塞）；② 信号满足、数据已可见（隐式完成保证——`putmem_signal` 的信号更新发生在数据落地之后）。

#### 4.4.3 源码精读

**`signal_op`**——原子更新远端信号字：

```python
def signal_op(sig_addr, signal, sig_op, pe):
    """Atomically updates `sig_addr` with `signal` using operation `sig_op` on the specified PE."""
    return tir.call_intrin("handle", tir.op.Op.get("tl.SignalOp"), sig_addr, signal, sig_op, pe)
```

见 [tilelang/language/distributed/multi_device/nvshmem.py:163-171](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/distributed/multi_device/nvshmem.py#L163-L171)。`sig_op` 是 NVSHMEM 的信号操作码（整数常量，如 SET/ADD），透传给 `nvshmemx_signal_op`（[codegen_cuda.cc:2832-2842](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/target/codegen_cuda.cc#L2832-L2842)）。

**`putmem_signal_nbi_block`**——搬数据 + 信号，注意它比普通 put 多三个参数 `sig_addr/signal/sig_op`：

```python
def putmem_signal_nbi_block(dest, src, nelems, sig_addr, signal, sig_op, pe):
    """Put data ... and update a remote flag on delivery."""
    return tir.call_intrin("handle", tir.op.Op.get("tl.PutmemSignalNbiBlock"),
                           dest, src, nelems, sig_addr, signal, sig_op, pe)
```

见 [tilelang/language/distributed/multi_device/nvshmem.py:140-152](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/distributed/multi_device/nvshmem.py#L140-L152)。其 codegen 输出 `nvshmemx_putmem_signal_nbi_block(...)`（[codegen_cuda.cc:2786-2796](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/target/codegen_cuda.cc#L2786-L2796)）。

**`signal_wait_until`**——本端等待：

```python
def signal_wait_until(*args):
    # TODO: handle return value(which is uint*64)?
    return tir.call_intrin("int32", tir.op.Op.get("tl.SignalWaitUntil"), *args)
```

见 [tilelang/language/distributed/multi_device/nvshmem.py:174-176](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/distributed/multi_device/nvshmem.py#L174-L176)。codegen 输出 `nvshmem_signal_wait_until(...)`（[codegen_cuda.cc:2843-2853](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/target/codegen_cuda.cc#L2843-L2853)）。注意源码里有一个 `TODO`：返回值类型（uint64\*）尚未完整处理，目前按 `int32` 返回。

**CP-engine 路线的 `wait_eq` 等**——与 NVSHMEM 路线的 `signal_wait_until` 功能对应但实现不同。`wait_eq` 走 `tl.tileop.wait`（TileOperator），在 [src/op/sync.cc](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/op/sync.cc) 里实现：

```python
# common.py —— 用 BinaryRelation 枚举表达比较关系
class BinaryRelation(Enum):
    EQ = 0; NE = 1; GE = 2; LE = 3; GT = 4; LT = 5

def wait_eq(value, expected, peer=-1):
    """Wait until value == expected"""
    return tir.call_intrin("handle", tir.op.Op.get("tl.tileop.wait"),
                           BinaryRelation.EQ.value, address_of(value), expected, peer)
```

见 [tilelang/language/distributed/common.py:121-132](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/distributed/common.py#L121-L132)。其 C++ `WaitOp::Lower` 把关系整数映射成字符串后生成 `tl::wait_eq/ne/ge/le/gt/lt`，并在 `peer != -1`（分布式）时套用 §4.1 的对称寻址换算：

```cpp
const char *relation_str[] = {"eq", "ne", "ge", "le", "gt", "lt"};
ss << "tl::wait_" << relation_str[relation];
...
if (is_distributed()) {   // peer != -1
  // remote_base[peer] + (addr - remote_base[me])
  new_args.push_back(Call(..., tl::get_remote_base_ptr(), {peer}) + offset_to_base);
} else {
  new_args.push_back(addr);   // 本地等待
}
```

见 [src/op/sync.cc:126-153](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/op/sync.cc#L126-L153)。`WaitOp` 注册为 `TIR_REGISTER_TL_TILE_OP(WaitOp, wait)`（[sync.cc:172-175](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/op/sync.cc#L172-L175)）。

**集合通信 `broadcast` / `fcollect`**：

```python
def broadcast(*args):
    return tir.call_intrin("handle", tir.op.Op.get("tl.Broadcast"), *args)

def fcollect(*args):
    return tir.call_intrin("handle", tir.op.Op.get("tl.Fcollect"), *args)
```

见 [tilelang/language/distributed/multi_device/nvshmem.py:179-180](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/distributed/multi_device/nvshmem.py#L179-L180) 与 [nvshmem.py:195-196](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/tilelang/language/distributed/multi_device/nvshmem.py#L195-L196)，二者均有 `_warp`/`_block` 变体。

#### 4.4.4 代码实践：画出两个 PE 一次 put + wait 的时序（本讲核心实践）

1. **实践目标**：把「异步远程 put + 条件等待」的同步模型在脑中具象成时序图，标注同步点。
2. **操作步骤**：
   - 读 [examples/distributed/primitives/example_put_block.py](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/examples/distributed/primitives/example_put_block.py) 与 [example_get_block.py](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/examples/distributed/primitives/example_get_block.py)。注意这两个示例里 kernel **没有显式 wait**——它们靠 host 侧 `torch.cuda.synchronize()` + `torch.distributed.barrier(group)`（[example_put_block.py:53-57](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/examples/distributed/primitives/example_put_block.py#L53-L57)）来对齐。
   - 读 [example_sync.py](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/examples/distributed/primitives/example_sync.py)，看 host 侧 `dist.barrier(group)` 的用法。
   - **动手画时序图**：在纸上画两条横轴（PE0、PE1）。基于本讲 §4.4.2 的模板，把示例里的 `put_block`（PE0 发起、`dst_pe=rank^1`）画成一段「远程写」箭头从 PE0 指向 PE1；在 PE0 侧标出 host `cuda.synchronize()`（等 kernel 结束）、在两 PE 间标出 `dist.barrier(group)`（host 屏障，同步点①）、在 PE1 侧标出校验读取（同步点②）。
   - 进阶：把示例改写成「kernel 内通知」版——PE0 用 `putmem_signal_nbi_block` 发数据+信号，PE1 用 `signal_wait_until(sig_addr, EQ, 1)` 等待后再读；在时序图上把 host 屏障替换为这对 signal/wait，标注新的同步点。
3. **需要观察的现象**：原示例的同步发生在 **host 层**（kernel 外）；改写后同步下沉到 **device kernel 内**，这正是 NVSHMEM 让通信可被计算 overlap 的价值。
4. **预期结果**：得到一张含两个 PE 时间轴、远程写箭头、≥2 个同步点的时序图，并能说清每个同步点由哪个原语承担。
5. **待本地验证**：实际多卡运行需 `launch.sh`（[u6-l4](u6-l4-pynvshmem-launch.md) 会讲）启动 2 进程；时序图绘制本身**纯阅读即可完成**。

#### 4.4.5 小练习与答案

**练习 1**：`putmem_signal_nbi_block` 相比「先 `putmem_nbi_block` 再 `signal_op`」有什么优势？
**答案**：`putmem_signal_*` 把「搬数据」和「发信号」合并成一次操作，保证**信号更新发生在数据真正落地之后**（原子语义），消费者看到信号即可放心读数据。分开调用则信号可能与数据竞态，且多一次往返开销。

**练习 2**：NVSHMEM 路线的 `signal_wait_until` 和 CP-engine 路线的 `wait_eq` 有何异同？
**答案**：两者都是「等信号字满足条件」的消费者侧等待。区别在实现：`signal_wait_until` 走普通 builtin、生成原生 `nvshmem_signal_wait_until(...)`；`wait_eq` 走 TileOperator、生成 `tl::wait_eq(...)` 模板调用，且在 `peer != -1` 时用对称寻址换算成远端地址。前者与 NVSHMEM C API 一一对应，后者是 TileLang 自有封装。

**练习 3**：`fcollect` 与普通的 `getmem` 有什么本质区别？
**答案**：`getmem` 是**点对点**（本 PE ← 某个指定 PE）；`fcollect` 是**集合通信**（所有 PE 共同参与，各自贡献一段数据，按 PE 序号拼接到每个 PE 的目标缓冲），语义类似按贡献者排序的 allgather。

---

## 5. 综合实践：实现一个 kernel 内的「令牌环」通知

把本讲四块内容串起来，设计一个 2-PE（可推广到 N-PE）的令牌传递小程序：

1. **任务**：PE0 把本地一段 float32 数据（`block_M` 个元素）搬到 PE1，PE1 **在 kernel 内**等数据到达后读取，并把一个本地计数器 +1 作为回执。
2. **要求**：
   - 用 NVSHMEM 路线的 `putmem_signal_nbi_block` 发「数据 + 信号」（信号字 SET 成 1）；
   - 用 `signal_wait_until(sig_addr, EQ, 1)` 在 PE1 等待；
   - 信号字须从**对称堆**分配（用 `tilelang.get_allocator(is_distributed=True)` + `tilelang.tensor(...)`，参考 [example_put_block.py:42-51](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/examples/distributed/primitives/example_put_block.py#L42-L51)）；
   - 编译后用 `kernel.get_kernel_source()` 检查生成的 CUDA 里确实含 `nvshmemx_putmem_signal_nbi_block` 与 `nvshmem_signal_wait_until`，且有 `#include <nvshmem.h>`。
3. **验收**：
   - 画出 PE0/PE1 的时序图，标出 put 信号、wait 阻塞、信号抵达三个事件；
   - 用 `dist.all_gather` 做正确性校验（参考示例第 59-68 行的对比写法）；
   - 解释：为什么这里不需要再额外加 `quiet()`？（答：`putmem_signal_*` 的信号更新已隐含完成语义。）
4. **运行**：用 `launch.sh` 起 2 进程（见 [u6-l4](u6-l4-pynvshmem-launch.md)）。无多卡环境时，编译与 `get_kernel_source()` 检查**单机可做**，执行**待本地验证**。

---

## 6. 本讲小结

- TileScale 的多设备通信有**两套并行原语**：NVSHMEM 路线（`multi_device/nvshmem.py`，1:1 映射 `nvshmem*`/`nvshmemx*`）与 CP-engine 路线（`common.py`，走 `tl::cp_block`/`tl::cp_warp` 模板），二者共用同一套对称寻址底座。
- **对称寻址**是根基：远程地址 = `base[pe] + (本地地址 − base[me])`，由 `get_remote_base_ptr`/`get_uintptr_t`/`get_rank` 三件套在 lowering 时换算（[remote_copy.cc:105-115](https://github.com/tile-ai/tilescale/blob/4704282a16fd0e7ff2c2c13f87772b42e4dc6163/src/op/remote_copy.cc#L105-L115)）。
- `putmem_*`/`getmem_*` 由「方向 × 完成时机（nbi）× 协作粒度（block/warp）」三维度组合；`nelems` 单位是**字节**，区别于 CP-engine 路线的元素数。
- 四个同步原语按强度递减：`barrier_all`（全局 + 远程完成）> `sync_all`（全局，**不等远程**）> `quiet`（单 PE 完成）> `fence`（单 PE 定序）。
- 远程通知靠 `signal_op` / `putmem_signal_*` / `signal_wait_until` 三件套构成「生产者—消费者」模型；`broadcast`/`fcollect` 提供集合通信。
- 整条链路遵循「Python intrin → C++ Op 注册（distributed.cc/sync.cc）→ codegen 打印 nvshmem 文本（codegen_cuda.cc）」的三段式，编译期只生成查表与调用文本，远程基址表在运行时注入。

---

## 7. 下一步学习建议

- **[u6-l3 CP-engine 远程 get/put 原语](u6-l3-cpengine-remote-copy.md)**：本讲已带出 CP-engine 路线，下一讲深入 `put_warp`/`get_warp` 的 `unroll_factor` 与 `enable_aggressive_vectorize`、以及 `tl::cp_warp`/`tl::cp_block` 设备模板的实现。
- **[u6-l4 分布式运行时：pynvshmem 与启动](u6-l4-pynvshmem-launch.md)**：本讲的「对称堆」与「远程基址表」从哪来、怎么注入——`init_distributed`、`pynvshmem` 对称堆张量、`launch.sh` 多进程启动。
- **[u6-l7 分布式实战：allgather / all2all / summa](u6-l7-distributed-examples.md)**：用本讲的原语组合出经典集合通信算法，体会「通信与计算 overlap」的工程写法。
- 若想理解 codegen 如何把 `call_extern` 的模板名（如 `tl::cp_block`）链接到具体 C++ 实现，可延伸阅读 [u7-l2 CUDA 模板与 GEMM 内核族](u7-l2-cuda-gemm-templates.md) 与 [u7-l3 目标后端 codegen 深入](u7-l3-codegen-internals.md)。
