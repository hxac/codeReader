# copy_utils 与 TMA / cp.async 拷贝

## 1. 本讲目标

上一讲（u5-l1）我们解决了循环缓冲流水线的**状态机**：谁来记录「现在该用第几个 SRAM 缓冲槽、转过第几圈」。但流水线要真正跑起来，还差一块拼图——**数据到底是怎么从一个存储层级搬到另一个存储层级的**。

本讲就聚焦这块拼图，主角是 `flash_attn/cute/copy_utils.py`（配合 `cute_dsl_utils.py` 与 `paged_kv.py`）。FA4 在前向/反向 kernel 里反复执行三种搬运：

- **gmem → smem**：把一块 Q/K/V 从显存(HBM)搬到共享内存(SRAM)，这是流水线的「生产者」动作，量大、是带宽瓶颈。
- **smem → rmem**：把 SRAM 里的数据加载到寄存器，供 MMA 矩阵乘消费。
- **带类型转换的拷贝**：源和目标元素类型不同（例如累加器是 fp32、要存成 fp16），搬的同时顺便转类型。

学完本讲你应当能够：

1. 区分 FA4 里两类核心拷贝原子（copy atom）——Ampere 上的 `cp.async`（`CopyG2SOp`）与 Hopper/Blackwell 上的 **TMA**（`cp.async.bulk`），并理解它们为何是异步的、靠 mbarrier 通知完成。
2. 读懂 `cvt_copy`、`load_s2r`、`get_copy_atom`、`tma_get_copy_fn`、`tma_producer_copy_fn` 等工具函数，知道它们各自封装了哪种搬运。
3. 理解 `assume_strides_aligned` 这类**对齐假设**如何向编译器声明不变量，从而让 TMA / 向量化拷贝合法且高效。

本讲不展开 TMA 描述符的字段细节（那是 u8-l3 Blackwell 专用主题），也不进 kernel 主循环的业务逻辑（u6）；我们只讲「搬运原子的构造与复用」。

## 2. 前置知识

读懂本讲前，建议先建立以下直觉（对应 u5-l1、u3、u4）：

- **GPU 三级存储与延迟差距**：数据从慢到快依次是 gmem（HBM/显存）→ smem（SRAM，片上共享内存）→ rmem（寄存器）。前向 kernel 把 K/V 一块块地从 gmem 搬进 smem（生产者），再从 smem 读进 rmem 做 MMA（消费者）。详见 u5-l1 的「旋转寿司店」比喻。
- **同步拷贝 vs 异步拷贝**：最朴素的「全局加载」是同步的，线程发起后要干等数据到达。Ampere 引入 `cp.async`（copy async），Hopper 引入 **TMA**（Tensor Memory Accelerator），它们能异步发起一整块拷贝，线程立刻继续干别的活，拷贝完成后由 **mbarrier**（内存屏障）发出「complete」通知。这正是流水线能隐藏 HBM 延迟的硬件基础。
- **流水线阶段（pipeline stage）**：u5-l1 讲过，循环缓冲有 `stages` 个槽，每个 gmem→smem 拷贝写入某个槽号（`smem_pipe_write`），消费者从某个槽号读（`smem_pipe_read`）。本讲的拷贝函数都会带一个「写到第几个 stage」的索引参数。
- **CuTeDSL 的张量抽象**：FA4 用 CUTLASS 的 `cute.Tensor` 描述一块数据，它有 `memspace`（gmem/smem/rmem）、`element_type`、`shape`、`stride`。拷贝原子 `cute.CopyAtom` 描述「一次最小搬运的粒度与硬件指令」，而 `cute.copy(atom, src, dst)` 把原子作用到一对张量上。
- **在线 softmax**（u4）：消费者每消化一个 K/V 块就更新 `row_max/row_sum`，消费节奏由搬运节奏供料。

一个形象的比喻：如果流水线（u5-l1）是「旋转寿司店的座位调度」，那么本讲讲的就是「传送带本身」——传送带用的是电动履带（TMA）还是人工手递（`cp.async`），以及传送时怎么把冷冻寿司（fp32 累加器）现加热成现做寿司（fp16 输出）。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `flash_attn/cute/copy_utils.py` | 本讲主角。提供类型转换拷贝 `cvt_copy`、shared→register 加载 `load_s2r`、拷贝原子工厂 `get_copy_atom`、1D/2D tiled copy、`cp.async.bulk`（TMA）的内联 PTX 封装 `cpasync_bulk_g2s` / `cpasync_bulk_s2cluster`、以及把 TMA 与流水线 stage 绑定的 `tma_get_copy_fn` / `tma_producer_copy_fn`。 |
| `flash_attn/cute/cute_dsl_utils.py` | 提供 `assume_strides_aligned` / `assume_tensor_aligned`，把 torch 张量的 stride 对齐信息「喂」给 CuTe 编译器；还有 torch↔cute dtype 映射表。是「对齐假设」模块的核心。 |
| `flash_attn/cute/paged_kv.py` | `PagedKVManager`：分页 KV cache 的搬运管理器。它**复用**了 copy_utils 里的 `cp.async` 原子思路（`CopyG2SOp` + tiled copy），但用页表把不连续的页拼成逻辑序列，是「拷贝原子被复用」的最佳现场。 |
| `flash_attn/cute/flash_fwd.py` | 前向 kernel 基类与 SM80 实现。本讲综合实践在此取证：`load_K` 函数和主循环里 K tile 的 gmem→smem 拷贝。 |

## 4. 核心概念与源码讲解

### 4.1 类型转换拷贝（cvt_copy）

#### 4.1.1 概念说明

在很多场景里，「搬运」和「转类型」是绑在一起的：

- **反向 epilogue**：MMA 的累加器 `acc_O` 是 `Float32`，但输出张量 `mO` 是 `Float16/BFloat16`。把累加器搬回 smem/gmem 时必须顺带转类型。
- **score_mod 之后的 P**：在线 softmax 算出的概率 `P = softmax(S)` 在 fp32 累加器里，喂给第二段 MMA（`PV`）时又需要转回 fp16。
- **fp8 路径**：输入是 `Float8E4M3FN`，计算时升到 fp16/fp32。

朴素做法是两步：先开一个临时寄存器做类型转换，再发一条拷贝。但两步意味着多一次寄存器往返。`cvt_copy` 把「需要转类型时先转、不需要时直接拷」封装成一个统一入口，并让**是否转类型成为编译期分支**（`const_expr`），从而特化出「纯拷贝」或「拷贝+转换」两种无冗余 kernel。

#### 4.1.2 核心流程

`cvt_copy(atom, src, dst, pred=None)` 的决策流程：

```text
入口：src（源张量）、dst（目标张量）、atom（拷贝原子）
  ├─ 断言 src 在寄存器空间(rmem)且迭代器是 Pointer
  ├─ 编译期判断：src.element_type != dst.element_type ?
  │     是 ─→ 新建一个 fragment(src_cvt, dtype=dst.element_type)
  │           src_cvt.store( src.load().to(dst.element_type) )   # 转类型
  │           src = src_cvt                                       # 之后用转换后的张量
  │     否 ─→ 直接用原 src
  └─ cute.copy(atom, src, dst, pred=pred)                          # 统一下发拷贝
```

关键点：

1. `const_expr(src.element_type != dst.element_type)` 是**编译期**判断。若类型相同，整段 `if` 在编译时被裁掉，`cvt_copy` 退化成一条纯 `cute.copy`，零额外开销。
2. 类型转换发生在**寄存器里**（`src.load().to(...)` 先把寄存器数据 load 出来再 `.to`），所以 `src` 必须在 rmem（函数开头断言）。这意味着 `cvt_copy` 适合「累加器→输出」这类 smem/rmem 之间的收尾搬运。

#### 4.1.3 源码精读

[flash_attn/cute/copy_utils.py:16-32](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/copy_utils.py#L16-L32) —— `cvt_copy` 的全部实现，核心就是「编译期判类型 → 必要时在寄存器转类型 → 统一下发 `cute.copy`」。注意装饰器 `@dsl_user_op` 把它注册成可在 CuTeDSL kernel 内调用的用户算子。

与之配套的 shared→register 加载见 [flash_attn/cute/copy_utils.py:35-39](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/copy_utils.py#L35-L39) —— `load_s2r`：从一个 smem 张量 `src` 加载到一个同类型的新 rmem fragment，内部用 `cute.autovec_copy` 自动选择向量化宽度。这是「smem → rmem」最朴素的同步搬运（不涉及异步/TMA）。

在 SM100 epilogue 里能看到 `cvt_copy` 的真实用法：累加器结果（fp32）要先转成 fp16 写进 smem 中转区，再用 TMA 存回 gmem。例如 [flash_attn/cute/flash_fwd_sm100.py:2800](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/flash_fwd_sm100.py#L2800) 处 `copy_utils.cvt_copy(tiled_smem_store, tOrO_frg, tOsO_r2s_i)` 就是把寄存器里的 O 片段（fp32）类型转换后拷进 smem。

#### 4.1.4 代码实践

**实践目标**：理解「类型相同走纯拷贝、类型不同走转换」这条编译期分支。

**操作步骤**（源码阅读型）：

1. 打开 `flash_attn/cute/copy_utils.py`，定位 `cvt_copy`（L16-32）。
2. 注意第 28 行的 `if const_expr(src.element_type != dst.element_type):`。`const_expr` 是 CUTLASS DSL 的编译期求值器——它要求括号里的表达式在编译时全部已知。`element_type` 在 kernel 特化时已确定，所以这条 `if` 不会出现在最终 PTX 里。
3. 追踪 `src.load().to(dst.element_type)`：`load()` 把寄存器 fragment 的值取成 Python 端可操作的值，`.to(...)` 插入一条类型转换，`.store(...)` 写回新 fragment。

**需要观察的现象 / 预期结果**：

- 当你在 kernel 里写 `cvt_copy(atom, fp32_acc, fp16_smem)`，编译产物里会出现一条「fp32→fp16 转换 + 拷贝」；写 `cvt_copy(atom, fp16_a, fp16_b)` 时，转换代码段消失，只剩拷贝。
- 由于运行 FA4 需要 GPU 且首次调用要 JIT 编译，本步骤为「源码阅读型」，**待本地验证**：可在启用 `CUTE_DSL_KEEP_PTX=1` 后（见 u11-l5）导出 PTX，搜索类型转换指令（如 `cvt.rn.f16.f32`）确认上述分支裁剪。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `cvt_copy` 开头要断言 `src.memspace == rmem`（源必须在寄存器）？如果把 src 放在 smem 会怎样？

**参考答案**：因为类型转换 `src.load().to(...)` 依赖先把数据取到线程私有空间做算术。smem 是多线程共享、需经 tiled copy 按线程分区访问；在 smem 上直接 `.to()` 无法表达「哪个线程转哪一份」。所以约定 src 必须在 rmem，转换在寄存器里完成后再拷贝。若硬把 smem 张量传进来，断言会触发（或编译失败）。

**练习 2**：`cvt_copy` 用 `const_expr` 而不是普通 `if` 来判类型，目的是什么？

**参考答案**：让「是否转类型」成为编译期常量分支，编译器可整段消除不成立的分支，特化出两种 kernel（纯拷贝 / 拷贝+转换），运行期零分支开销。普通 `if` 会把判断和两条路径都编进 PTX，引入运行期分支与冗余代码。

---

### 4.2 TMA / cp.async copy atom

#### 4.2.1 概念说明

这是本讲最重要的模块。GPU 上「gmem → smem」的搬运有两条技术路线，对应两代硬件：

| 路线 | 硬件 | 指令 | 特点 |
| --- | --- | --- | --- |
| `cp.async` | Ampere (SM80) 起 | `cp.async.cg/ca` | 线程束(warp)级异步拷贝，每线程搬一小段（如 16B），靠 `cp.async.commit_group/wait_group` 跟踪完成。 |
| **TMA** (`cp.async.bulk`) | Hopper (SM90) 起 | `cp.async.bulk` | 一个线程发起**整块**（多维 tile）拷贝，硬件自己拆解地址，靠 **mbarrier** 的 `complete_tx::bytes` 计数指定字节数后发完成通知。 |

在 CUTLASS CuTeDSL 里，这两条路线被统一抽象成 **CopyAtom**（拷贝原子）：一个 `CopyAtom` 描述「用什么硬件指令、搬多少 bit、什么类型」。`cute.copy(atom, src, dst)` 把原子作用到一对张量上，编译器据此生成对应的 PTX。

- Ampere `cp.async` 原子用 `cpasync.CopyG2SOp()`（Global→Shared 异步操作）。
- 同步/通用拷贝用 `cute.nvgpu.CopyUniversalOp()`（不强求异步，可用于 smem→rmem 或 gmem store）。
- TMA 原子则由 `cute.nvgpu.make_tiled_tma_atom_*` 构造（见 u8），其「下发」最终落到本讲的 `cpasync_bulk_g2s` 内联 PTX。

为什么 TMA 更快？因为它**一个线程描述一整块拷贝**，释放了其余线程，且硬件地址生成器比线程束手算地址高效得多；同时 mbarrier 的 `complete_tx::bytes` 机制让你精确声明「这块要搬 N 字节，搬够就通知」，省去逐元素同步。

#### 4.2.2 核心流程

**(a) 拷贝原子工厂 `get_copy_atom`**

[flash_attn/cute/copy_utils.py:42-48](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/copy_utils.py#L42-L48) —— 工厂函数，按 dtype、每次拷贝元素数、是否异步，选 `CopyG2SOp`（异步 `cp.async`）或 `CopyUniversalOp`（通用同步），并限制单次拷贝不超过 128 bit：

\[
\text{num\_copy\_bits} = \min\!\big(128,\; \text{num\_copy\_elems} \times \text{dtype.width}\big)
\]

128 bit 是 `cp.async` 单次拷贝的上限（对 fp16 即 8 个元素）。这正好呼应 u1-l3 里「head_dim 需 16 字节对齐」的约束——16 字节 = 128 bit，保证每次拷贝都是满粒度。

**(b) 1D / 2D tiled copy**

光有原子不够，还要决定**哪些线程搬哪些元素**（线程布局）。`tiled_copy_1d` / `tiled_copy_2d` 把「原子 + 线程布局 + 值布局」粘成一个 `TiledCopy`：

- [flash_attn/cute/copy_utils.py:81-89](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/copy_utils.py#L81-L89) —— 1D：线程平均分担 `num_copy_elems`。
- [flash_attn/cute/cute_dsl_utils.py](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/cute_dsl_utils.py) 同目录的 [flash_attn/cute/copy_utils.py:92-106](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/copy_utils.py#L92-L106) —— 2D：按「主维(major mode)」大小算出每行多少线程、每线程搬几个元素，要求 `num_threads` 能被「每行线程数」整除。前向 kernel 里 K/V 的 gmem→smem 拷贝正是 2D tiled copy（行=序列维，列=head_dim）。

**(c) TMA bulk 拷贝的内联 PTX**

TMA 的底层是 PTX 指令 `cp.async.bulk`，CuTeDSL 直接用内联汇编封装：

[flash_attn/cute/copy_utils.py:242-263](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/copy_utils.py#L242-L263) —— `cpasync_bulk_g2s`：发起一次 gmem→smem 的 bulk 拷贝，把字节数 `size` 登记到 mbarrier `tma_bar_ptr` 上。核心 PTX 是

```ptx
cp.async.bulk.shared::cta.global.mbarrier::complete_tx::bytes [smem], [gmem], size, [mbar];
```

含义：从 gmem 搬 `size` 字节到 smem，搬够字节数就触发 mbarrier 的 tx（transfer）计数完成。这条指令就是 TMA 的「一锤子搬运」。2CTA（cluster 内两个 CTA 协作）时还有 smem→smem 的 [flash_attn/cute/copy_utils.py:210-239](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/copy_utils.py#L210-L239) `cpasync_bulk_s2cluster`，把数据从一个 CTA 的 smem 搬到 peer CTA 的 smem（见 u8-l4）。

**(d) 把 TMA 绑到流水线 stage**

`cpasync_bulk_get_copy_fn` / `tma_get_copy_fn` 是「拷贝闭包工厂」：它们预先把 src/dst 张量按流水线 stage 维度切好，返回一个只需传 `src_idx/dst_idx` 的闭包，让主循环代码很干净：

[flash_attn/cute/copy_utils.py:324-360](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/copy_utils.py#L324-L360) —— `tma_get_copy_fn`：用 `cpasync.tma_partition` 把 smem 和 gmem 张量按 TMA 原子分区，返回 `(copy_tma, s, g)`。`copy_tma(src_idx, dst_idx, tma_bar_ptr=...)` 就是「把第 src_idx 块 gmem 搬进第 dst_idx 个 smem stage，并登记到给定 mbarrier」。

[flash_attn/cute/copy_utils.py:363-372](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/copy_utils.py#L363-L372) —— `tma_producer_copy_fn`：再包一层，把 `dst_idx` 绑成 `producer_state.index`（即写入循环缓冲的哪个槽）、把 mbarrier 绑成 `pipeline.producer_get_barrier(producer_state)`。这样生产者只需 `copy_fn(src_idx, producer_state)` 就完成「搬数据 + 登记到对应 stage 的 mbarrier」——完美对接 u5-l1 的流水线状态机。

#### 4.2.3 源码精读（拷贝原子如何在 kernel 里被构造）

SM80 前向 kernel 构造 gmem→smem 拷贝原子的现场见 [flash_attn/cute/flash_fwd.py:246-298](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/flash_fwd.py#L246-L298)。关键几行：

```python
universal_copy_bits = 128
async_copy_elems = universal_copy_bits // self.dtype.width          # fp16 → 8 个元素
atom_async_copy = cute.make_copy_atom(
    cpasync.CopyG2SOp(cache_mode=cpasync.LoadCacheMode.GLOBAL),    # cp.async，全局缓存策略
    self.dtype,
    num_bits_per_copy=universal_copy_bits,                          # 单次 128 bit
)
...
self.gmem_tiled_copy_K = cute.make_tiled_copy_tv(atom_async_copy, tK_layout, vQKV_layout)
```

这里 `atom_async_copy` 就是上一节 (a) 描述的「`cp.async` 原子」，用 `make_tiled_copy_tv` 配上线程布局 `tK_layout`（哪些 warp 哪些线程搬）和值布局 `vQKV_layout`（每线程搬 8 个 fp16）组成 K 的 `TiledCopy`。SM100 则改用 TMA：[flash_attn/cute/flash_fwd_sm100.py:598-607](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/flash_fwd_sm100.py#L598-L607) 用 `make_tiled_tma_atom_B` 为 K、V 构造 TMA 原子（`tma_atom_K` / `tma_atom_V`）。

**分页 KV cache 复用 cp.async 原子**：`paged_kv.py` 是「拷贝原子被复用」的最佳示例。它同样用 `CopyG2SOp` + 128-bit 粒度构造 `gmem_tiled_copy_KV`，但因为 KV 在显存里是按页(page)分散存放的，它**先用页表把每行数据的真实 gmem 地址算出来**（`compute_X_ptr`），再用同一个 tiled copy 原子搬运：

[flash_attn/cute/paged_kv.py:66-88](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/paged_kv.py#L66-L88) —— 与 flash_fwd 几乎一样的原子构造（`universal_copy_bits=128`、`CopyG2SOp(GLOBAL)`），证明分页拷贝复用了同一套拷贝原子基础设施。

[flash_attn/cute/paged_kv.py:187-207](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/paged_kv.py#L187-L207) —— `_copy_row_async`：对一行 K/V 的所有 k-tile 发起 `cp.async`（`cute.copy(self.gmem_tiled_copy_KV, ...)`），源地址来自页表算出的 `mX_paged_cur_copy_ki`。这就是「相同的原子、不同的地址生成」。

#### 4.2.4 代码实践

**实践目标**：区分 `cp.async`（SM80）与 TMA（SM100）两类拷贝原子在 kernel 里的构造方式。

**操作步骤**（源码阅读型 + 可选运行）：

1. 在 [flash_attn/cute/flash_fwd.py:249-253](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/flash_fwd.py#L249-L253) 确认 SM80 走 `cpasync.CopyG2SOp`（cp.async）。
2. 在 [flash_attn/cute/flash_fwd_sm100.py:598-607](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/flash_fwd_sm100.py#L598-L607) 确认 SM100 走 `make_tiled_tma_atom_B`（TMA）。
3. （可选，需 GPU）对比两种原子：在支持两代架构的卡上，用 `FLASH_ATTENTION_ARCH=sm_80` 与 `FLASH_ATTENTION_ARCH=sm_100`（见 u2-l2）各跑一次前向，导出 PTX（`CUTE_DSL_KEEP_PTX=1`），搜索 `cp.async` 与 `cp.async.bulk` 指令，验证二者使用的拷贝指令确实不同。

**预期结果**：SM80 PTX 里出现大量 `cp.async.cg.shared.global`（每线程 16B），SM100 PTX 里出现 `cp.async.bulk`（整块）。两者数学结果一致（都是把同一块 K 搬进 smem），只是搬运机制不同。若无可运行 GPU，则本步骤为源码阅读型，**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：`get_copy_atom` 里为什么把单次拷贝限制为 `min(128, ...)` bit？超过会怎样？

**参考答案**：`cp.async` 单条指令最大 128 bit（16 字节）。超过硬件单指令上限会无法编码或被拆成多条，失去「一次原子搬运」的意义。128 bit 同时也匹配 head_dim 的 16 字节对齐要求（u1-l3），保证满粒度、无零头。

**练习 2**：TMA 用 mbarrier 的 `complete_tx::bytes` 而不是普通 `bar.sync` 来通知完成，原因是什么？

**参考答案**：`cp.async.bulk` 是异步且按字节数计量的：你声明「这块要搬 N 字节」，硬件每搬一段就把 mbarrier 的 tx 计数减，减到 0 才发完成信号。普通 `bar.sync` 只能数「到达的线程数」，无法表达「数据字节数够了」，故 TMA 必须用 `complete_tx::bytes` 这种字节级完成语义。

**练习 3**：`paged_kv.py` 的分页拷贝和 `flash_fwd.py` 的连续拷贝，共用的是哪一层抽象、又各自定制了什么？

**参考答案**：共用「拷贝原子 + tiled copy」这一层（都是 `CopyG2SOp` + 128-bit + `make_tiled_copy_tv`）。定制的是**源地址生成**：连续拷贝的源是线性 gmem 张量直接分区；分页拷贝先用页表 `compute_X_ptr` 算出每个线程要读的页内地址，再把该地址喂给同一个 tiled copy 原子。这正是「拷贝原子被复用」的体现。

---

### 4.3 对齐假设（assume_strides_aligned）

#### 4.3.1 概念说明

TMA 和 `cp.async` 都对地址有**对齐要求**：一次 128-bit（16 字节）拷贝要求源地址是 16 字节对齐的。编译器在生成向量化/TMA 指令时，如果**不知道**地址对齐，就只能保守地生成逐元素、非向量的慢指令。

问题在于：CuTeDSL 的张量在 kernel 里是「动态 layout」（stride 是运行期值，见 u3-l3 的 `mark_layout_dynamic`）。编译器看不到「这个 stride 一定是 8 的倍数」，于是无法做向量化优化。

解决办法是 `assume_strides_aligned` / `assume_tensor_aligned`：它们用 `cute.assume(stride, divby=N)` 向编译器**声明**「我保证除了最后一维，所有 stride 都能被 N 整除」。这是一份**程序员对编译器的契约**——声明后编译器即可放心生成 128-bit 向量拷贝和 TMA 指令；但若实际输入不满足，行为未定义（可能段错误或读错数据）。

为什么是「除最后一维」？因为最后一维通常 stride=1（head_dim 连续），对齐由首地址和 `head_dim` 自身保证（见 u1-l3 的 16 字节对齐校验）；而前面的 batch/seqlen/head 维的 stride 是「行宽」的倍数，需要显式声明对齐。

#### 4.3.2 核心流程

`assume_strides_aligned(t)` 的逻辑：

```text
divby = 128 // t.element_type.width        # fp16 → 8；即「stride 至少是 8 个元素」
对 t.stride[:-1] 的每个 stride s：
    若 s 是 Python int（静态，如 GQA expand 的 stride=0）→ 保持原样
    否则 → cute.assume(s, divby=divby)        # 向编译器声明 s % divby == 0
最后一维 stride 原样返回
```

数学上，它声明的不变量是：对每个非末维 stride \( s_i \)，有

\[
s_i \bmod \text{divby} = 0, \qquad \text{divby} = \frac{128}{\text{dtype.width}}
\]

`assume_tensor_aligned` 再用这些被「assume 过」的 stride 重建一个同形状的张量 layout，供后续拷贝/TMA 使用。

#### 4.3.3 源码精读

[flash_attn/cute/cute_dsl_utils.py:44-52](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/cute_dsl_utils.py#L44-L52) —— `assume_strides_aligned` 全文。注意两点：

1. `divby = 128 // t.element_type.width`：fp16（width=16）得 8，fp32（width=32）得 4。即「stride 必须是 128-bit 的元素数倍数」。
2. Python int 的 stride（如 GQA expand 产生的静态 0）被原样保留——它们本就是编译期常量，无需再 assume。

[flash_attn/cute/cute_dsl_utils.py:55-59](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute_dsl_utils.py#L55-L59) —— `assume_tensor_aligned`：用 assume 过的 stride 重建张量，`None` 直接透传。

这套假设在输入张量进入 kernel 前被调用。配合 u1-l3 的 `_validate_head_dims`（运行期断言 head_dim 是 8 的倍数），FA4 把「对齐」拆成两道关：**运行期校验 head_dim** + **编译期 assume stride**，共同保证 128-bit 拷贝与 TMA 合法。

#### 4.3.4 代码实践

**实践目标**：理解 `divby` 随 dtype 变化，以及对齐假设与运行期校验的分工。

**操作步骤**（源码阅读型）：

1. 读 [flash_attn/cute/cute_dsl_utils.py:44-52](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute_dsl_utils.py#L44-L52)，手算三种 dtype 的 `divby`：

   | dtype | width (bit) | divby = 128/width |
   | --- | --- | --- |
   | Float16 / BFloat16 | 16 | 8 |
   | Float32 | 32 | 4 |
   | Float8E4M3FN | 8 | 16 |

2. 联系 u1-l3：`_validate_head_dims` 要求 head_dim 整除 alignment（fp16=8）。这与本讲的 `divby=8` 一致——head_dim 对齐 = 末维 stride 对齐，二者协同。
3. 思考：若用户传入一个 stride 不是 8 倍数的张量（例如某种非标准 reshape），`assume` 仍会声明对齐，但实际拷贝会读到错误数据。这就是为什么 FA4 在公共 API 层（interface.py）用 `maybe_contiguous` 强制布局、用 `_validate_head_dims` 校验——把「契约」的守门员放在入口。

**预期结果**：能口述「fp16 的对齐单位是 8 个元素 = 16 字节」，并解释 `assume` 是程序员契约、不检查只声明。本步骤无需 GPU，纯阅读即可。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `assume_strides_aligned` 只 assume 非末维 stride，末维不动？

**参考答案**：末维通常 stride=1（数据在 head_dim 上连续），其「对齐」由首地址和 head_dim 长度保证（head_dim 经 `_validate_head_dims` 校验为 8 的倍数），无需也无法用 stride 倍数表达。而非末维 stride 等于「后面所有维大小的乘积」，需要显式声明它是 8 的倍数，编译器才敢按 128-bit 向量跨行拷贝。

**练习 2**：`cute.assume(s, divby=8)` 之后，编译器得到了什么、又失去了什么保证？

**参考答案**：得到了一个编译期不变量 \(s \bmod 8 = 0\)，据此可生成 128-bit 向量/TMA 指令、省去逐元素回退。失去的是**正确性兜底**：assume 只声明不检查，若实际 stride 不满足，拷贝会读错地址且无报错。所以对齐的真实保证必须靠 API 层的运行期校验（`_validate_head_dims`、`maybe_contiguous`）。

**练习 3**：GQA 里用 `expand` 产生的张量某维 stride=0，`assume_strides_aligned` 会怎么处理它？为什么这样设计？

**参考答案**：stride=0 是 Python int（静态），函数里 `s if isinstance(s, int) else cute.assume(...)` 会把它原样保留，不再 assume。因为静态 0 stride 在编译期就已知（表示广播），CuTe 已能正确处理，无需额外声明对齐。这也避免了「assume 一个静态值」这种无意义操作（见函数 docstring）。

---

## 5. 综合实践

把本讲三类知识（cp.async 原子、流水线 stage、对齐假设）串起来，完成规格里指定的任务：

> **任务**：在 `flash_fwd.py` 的 SM80 前向主循环中，定位一次 K tile 的 gmem→smem 加载，指出它使用的拷贝原子、对应的 pipeline stage，并画出数据路径。

### 步骤 1：定位拷贝原子

SM80 前向 kernel 在构造期为 K 准备了 gmem→smem 的 tiled copy：

[flash_attn/cute/flash_fwd.py:297](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/flash_fwd.py#L297) —— `self.gmem_tiled_copy_K`，由 [flash_attn/cute/flash_fwd.py:249-253](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/flash_fwd.py#L249-L253) 的 `atom_async_copy`（`cpasync.CopyG2SOp(GLOBAL)`，128-bit）拼上线程/值布局而成。

**结论**：K tile 用的是 **`cp.async` 拷贝原子**（`CopyG2SOp`），单次 128-bit、全局缓存策略。

### 步骤 2：定位主循环里的 K 加载与 pipeline stage

K 的实际搬运函数是 `load_K`：

[flash_attn/cute/flash_fwd.py:482-526](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/flash_fwd.py#L482-L526) —— 注意第 524 行把数据写入 `tKsK[...][smem_pipe_write if num_stages > 1 else 0]`，即**写入流水线的第 `smem_pipe_write` 个 stage**；搬运谓词 `pred=tKpK` 处理 head_dim 越界。

主循环里两种调用现场：

- 预热阶段（prologue）：[flash_attn/cute/flash_fwd.py:982](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/flash_fwd.py#L982) —— `load_K(n_block - stage, smem_pipe_write=stage, ...)`，为每个 stage 预搬一块 K，写进对应槽号。
- 稳态阶段：[flash_attn/cute/flash_fwd.py:1163-1166](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/flash_fwd.py#L1163-L1166) —— `load_K_next()` 搬下一块 K 进 `smem_pipe_write`，紧跟 `cp_async_commit_group()` 把这批 cp.async 打包成一个组。

每次搬运后都用 `cp_async_commit_group()` 提交；消费者侧在 [flash_attn/cute/flash_fwd.py:1112-1114](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/flash_fwd.py#L1112-L1114) 用 `cp_async_wait_group(num_stages*2-2)` + `barrier()` 等待数据落盘进 smem——这正是 u5-l1 流水握手的「consumer_wait」落点。

### 步骤 3：画出数据路径

把上面串联起来，一次 K tile 搬运的完整路径：

```text
gmem(HBM)
  │   mK: (batch, seqlen_k, num_heads, head_dim)
  │   tKgK = gmem_tiled_copy_K.partition_S(mK)        ← 按线程分区源
  ▼   cp.async (CopyG2SOp, 128-bit, cache=GLOBAL)     ← 本讲的拷贝原子
smem(SRAM)
  │   sK[stage]: 写入第 smem_pipe_write 个循环缓冲槽   ← 对接 u5-l1 流水线
  │   tKsK = gmem_tiled_copy_K.partition_D(sK)
  ▼   consumer: cp_async_wait_group + barrier          ← 等待搬运完成
rmem(寄存器)
  │   tSrK: sm80_utils.gemm 内 smem→rmem 拷贝（load_s2r 风格）
  ▼   MMA: S = QK^T                                       ← 交给 u6/u4
```

### 步骤 4：标注对齐假设的作用

在路径的「cp.async」这一跳，编译器要敢发 128-bit 向量指令，依赖 K 张量的 stride 满足对齐——这正是 4.3 节 `assume_strides_aligned` 在 kernel 入口处声明的不变量（fp16 下 `divby=8`）。没有它，`gmem_tiled_copy_K` 会退化成逐元素慢拷贝。

### 交付物

把上面四步整理成一张一页文档（文字 + 数据流图），要求：

1. 指明拷贝原子类型（`cp.async` / `CopyG2SOp` / 128-bit）。
2. 指明它写入的 pipeline stage 字段（`smem_pipe_write`）与提交/等待原语（`cp_async_commit_group` / `cp_async_wait_group`）。
3. 画出 gmem→smem→rmem→MMA 的数据路径。
4. 标注对齐假设在哪一跳起作用。

> 若要进一步对比，可重复本任务于 SM100 kernel（`flash_fwd_sm100.py`），把「`cp.async` 原子」换成「TMA 原子（`tma_atom_K` + `tma_get_copy_fn` + `tma_producer_copy_fn`）」，体会两代硬件在「同一个数据路径」上换了一种搬运指令。SM100 路径**待本地验证**（需 Blackwell GPU）。

## 6. 本讲小结

- FA4 的搬运基础设施集中在 `copy_utils.py`，三类搬运各有封装：**类型转换拷贝** `cvt_copy`（编译期判类型，转好再拷）、**smem→rmem 加载** `load_s2r`、**gmem→smem 异步拷贝**（`cp.async` 原子 `CopyG2SOp` 或 TMA bulk）。
- 拷贝原子（CopyAtom）是「指令 + 粒度 + 类型」的三元组：`get_copy_atom` 选 `CopyG2SOp`（异步 cp.async）或 `CopyUniversalOp`（同步通用），单次封顶 128-bit；`tiled_copy_1d/2d` 再配上线程布局组成 `TiledCopy`。
- TMA（`cp.async.bulk`）是 Hopper+ 的「一锤子整块搬运」，靠 mbarrier 的 `complete_tx::bytes` 按字节数通知完成；`cpasync_bulk_g2s` 是其内联 PTX 封装，`tma_get_copy_fn`/`tma_producer_copy_fn` 把它与循环缓冲 stage（u5-l1）绑定。
- `paged_kv.py` 复用同一套 cp.async 原子，只是把源地址换成页表查出来的页内地址——「相同原子、不同地址生成」。
- 对齐假设 `assume_strides_aligned`（`divby = 128/width`，fp16=8）向编译器声明 stride 不变量，使 128-bit 向量拷贝和 TMA 合法高效；它只声明不检查，真实对齐靠 API 层的 `_validate_head_dims` 与 `maybe_contiguous` 兜底。

## 7. 下一步学习建议

- **进入前向主循环**：本讲的拷贝原子是「传送带」，下一讲 u6-l1（Ampere 前向 Kernel 全景）会把它接进完整主循环，看清 Q 常驻、K/V 流水、MMA、在线 softmax 的全流程。建议带着本讲的「数据路径图」去读 u6。
- **TMA 描述符细节**：本讲只用到 TMA 的「搬运」一面。TMA 描述符（TMA descriptor）如何编码多维 tile 地址、SM100 的 `mma_sm100_desc.py` 字段，留到 u8-l3（UMMA Descriptor 与 Blackwell Helpers）。
- **2CTA 与 cluster 拷贝**：本讲提到的 `cpasync_bulk_s2cluster` 是 2CTA 协作的基础，完整死锁排查见 u8-l4（hd256 2CTA 专用 Kernel）与 u11-l5 的 `AI/DEBUG_2CTA.md`。
- **命名屏障**：本讲的 `cp_async_wait_group`/mbarrier 与 warp 同步的关系，在 u5-l3（命名屏障与 warp 同步）展开。
