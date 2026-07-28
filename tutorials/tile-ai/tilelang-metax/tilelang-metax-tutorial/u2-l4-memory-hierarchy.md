# 内存层级与显存分配

## 1. 本讲目标

本讲关注 TileLang kernel 里「数据放在哪、怎么搬、怎么初始化」这三件事。读完本讲你应该能够：

- 说清 GPU 的 **global / shared / fragment / local** 四级显存层级，以及 TileLang 用哪几个 API 把 buffer 显式放到对应层级。
- 用 `T.alloc_shared`、`T.alloc_fragment`、`T.alloc_local` 分配不同作用域的 buffer，并理解 fragment 为什么会被 **layout 推断** 自动改写。
- 用 `T.copy` 在 global ↔ shared ↔ fragment 之间搬运数据块（tile），并理解「编译器会按 target 自动选择 cp.async / TMA / 普通循环」这件事在源码里如何发生。
- 用 `T.clear` / `T.fill` 把一个 buffer 清零或填充为指定值。

本讲承接 u2-l2（`T.Kernel` 启动上下文）与 u2-l3（循环与控制流）：前两讲解决了「怎么启动、怎么循环」，本讲解决「循环里读写的那些 buffer 从哪来、放哪去」。

## 2. 前置知识

在进入源码前，先用三段话建立直觉。

**为什么 GPU 要分这么多层显存？** 现代 GPU 的存储是一个金字塔：越靠顶越快但越小、越靠底越大但越慢。以 NVIDIA GPU 为例：

| 层级 | 物理对应 | 可见范围 | 速度 | 容量 |
|------|---------|---------|------|------|
| global | 显存（HBM/GDDR） | 所有线程、主机 | 慢 | 大（GB 级） |
| shared | 片上共享内存（SM 内） | 同一个线程块 | 快 | 小（每 SM 通常几十 KB） |
| fragment / local | 寄存器文件 | 单个线程私有 | 最快 | 极小 |

一个高性能 kernel 的核心套路就是：**把数据从慢而大的 global 分批搬进快而小的 shared/寄存器，算完再搬回 global**。这正是 u1-l4 GEMM 示例里「搬进来—算—搬出去」那三步的物理本质。

**什么是「作用域（scope）」？** 在 TVM/TIR 里，每个 buffer 都带一个字符串标签，标明它住在哪一层显存，比如 `"global"`、`"shared.dyn"`、`"local.fragment"`、`"local"`。TileLang 的分配 API 其实就是「带上特定 scope 去申请 buffer」的语法糖。

**什么是 fragment？为什么它和 local 不一样？** 两者最终都会落到寄存器上，区别在于「谁决定每个线程拿到哪几个寄存器」：

- `alloc_local`（scope `"local"`）：你自己写 `for` 循环、自己按下标访问，每个线程看到的就是自己那份，编译器基本不插手布局。
- `alloc_fragment`（scope `"local.fragment"`）：你把它当一个「整块 tile」来用（比如 `(BM, BN)` 的累加器），但物理上这块 tile 会被**拆分**到线程块的所有线程上。具体怎么拆，由编译器的 **Layout Inference Pass（布局推断）** 自动推导。这就是本讲要重点理解的关键差异。

数学上，如果一个 fragment 形状为 \((M, N)\)，线程块共有 \(P\) 个线程，那么布局推断要解出一个映射，决定第 \(t\) 号线程（\(0 \le t < P\)）负责 fragment 里的哪些 \((i, j)\) 元素，且满足总元素数守恒：

\[
\sum_{t=0}^{P-1} \bigl|\text{owned}(t)\bigr| = M \times N
\]

这个映射不是你写的，是 pass 推出来的——所以 fragment 的下标访问语义和物理寄存器并不是一一对应。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
|------|------|
| [tilelang/language/allocate.py](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/language/allocate.py) | DSL 层的显存分配 API：`alloc_shared` / `alloc_local` / `alloc_fragment` / `alloc_var` / `alloc_global` 等 |
| [tilelang/language/copy_op.py](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/language/copy_op.py) | `T.copy` 及其变体（`async_copy` / `tma_copy` 等）的前端实现，把拷贝下译成 `tl.tileop.copy` intrinsic |
| [tilelang/language/fill_op.py](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/language/fill_op.py) | `T.fill` / `T.clear` 的前端实现，下译成 `tl.tileop.fill` |
| [src/op/copy.cc](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/op/copy.cc) | C++ 侧 `Copy` 算子：scope 层级判定、SIMT 循环构造、按 target 分派到各后端实现 |
| [src/cuda/op/copy.cc](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/cuda/op/copy.cc) | CUDA 后端的 copy lowering，决定用 TMA / cp.async / LDSM / 普通循环 |
| [src/transform/layout_inference.cc](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/transform/layout_inference.cc) | fragment/shared 的布局推断 pass |
| [examples/elementwise/example_elementwise_add.py](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/examples/elementwise/example_elementwise_add.py) | 一个把 global→shared→fragment→shared→global 完整走一遍的可运行示例 |
| [docs/get_started/overview.md](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/docs/get_started/overview.md) | 官方对「显式硬件内存分配」的说明 |
| [docs/programming_guides/language_basics.md](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/docs/programming_guides/language_basics.md) | 官方对 memory scope 与 `T.copy` 的语法说明 |

记忆口诀：**allocate.py 管「放哪」、copy_op.py 管「怎么搬」、fill_op.py 管「初始值」**，三者在 C++ 侧分别对应 `tl.tileop.copy` / `tl.tileop.fill` 这类被注册的 tile 算子。

## 4. 核心概念与源码讲解

### 4.1 alloc_shared：共享内存与作用域总览

#### 4.1.1 概念说明

`T.alloc_shared(shape, dtype)` 在**片上共享内存**里申请一块 buffer。共享内存的特点是：同一个线程块（thread block）内的所有线程都能读写它，速度远快于 global，因此它是 kernel 内部「线程间共享中间数据」的中转站——典型用法是 GEMM 里把矩阵分块从 global 预取到 shared，再让所有线程复用。

在 TVM 的 scope 体系里，共享内存的 scope 名是 `"shared.dyn"`（动态分配的共享内存段）。`alloc_shared` 的默认 scope 正是它。

要建立完整的层级直觉，记住这四级 scope 字符串：

| TileLang API | 默认 scope | 含义 |
|--------------|-----------|------|
| `T.Tensor(...)` 参数 / `alloc_global` | `"global"` | 显存，跨线程块可见 |
| `T.alloc_shared` | `"shared.dyn"` | 共享内存，块内可见 |
| `T.alloc_fragment` | `"local.fragment"` | 寄存器 tile，由布局推断分发到各线程 |
| `T.alloc_local` | `"local"` | 线程私有寄存器，你自己按下标管理 |

#### 4.1.2 核心流程

`alloc_shared` 的执行流程非常短：

1. 接收 `shape`、`dtype`，可选 `scope`（默认 `"shared.dyn"`）。
2. 特判 `bool` 类型：因为 tilelang 的合并共享内存 pass 暂不支持 bool，所以把 bool 的 scope 退化成静态的 `"shared"`。
3. 调用 TVM 脚本层的 `T.sblock_alloc_buffer(shape, dtype, scope=scope)`，产出一个带对应 scope 的 `Buffer` 对象返回。

`alloc_local` 与之同构，只是 scope 默认为 `"local"`。

#### 4.1.3 源码精读

`alloc_shared` 的实现：

[tilelang/language/allocate.py:34-49](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/language/allocate.py#L34-L49) —— 定义分配函数，默认 `scope="shared.dyn"`；对 `bool` 做特殊处理（退化为 `"shared"`），最后统一交给 `T.sblock_alloc_buffer`。

关键片段（示例代码摘录，非完整文件）：

```python
def alloc_shared(shape, dtype, scope="shared.dyn") -> Buffer:
    if dtype == "bool":
        scope = "shared"   # bool 暂不支持动态合并，退化为静态 shared 段
    return T.sblock_alloc_buffer(shape, dtype, scope=scope)
```

`alloc_local` 紧随其后，结构完全一致，只是 scope 换成 `"local"`：

[tilelang/language/allocate.py:52-63](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/language/allocate.py#L52-L63) —— 线程私有 local 内存分配。

scope 的「层级高低」在 C++ 侧也有明确定义，是 copy 算子决定循环范围的依据：

[src/op/copy.cc:307-315](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/op/copy.cc#L307-L315) —— `scope_level` lambda 把 scope 映射成层级整数：`local.fragment`/`local` 为 2，`shared`/`shared.dyn`/`shared.tmem` 为 1，其余（global）为 0。这正是本讲层级表的源头。

```cpp
auto scope_level = [](const Buffer &b) -> int {
    String s = b.scope();
    if (s == "local.fragment" || s == "local")        return 2;
    if (s == "shared" || s == "shared.dyn" || s == "shared.tmem") return 1;
    return 0;  // global
};
```

官方文档对 `alloc_shared` 的定位：

[docs/get_started/overview.md:80](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/docs/get_started/overview.md#L80) —— 说明 shared 内存用于块内缓存中间数据、降低 global 带宽压力。

#### 4.1.4 代码实践

**目标**：亲手声明一块 shared 内存，并观察它生成的设备代码里确实落在共享内存段。

**步骤**：

1. 打开 `examples/elementwise/example_elementwise_add.py`，定位到 kernel body 里的 `A_shared = T.alloc_shared((block_M, block_N), in_dtype)`。
2. 把 `block_M=32, block_N=32` 改成 `block_M=64, block_N=64`（在 `main()` 的调用处改），重新运行 `python examples/elementwise/example_elementwise_add.py`。
3. 用 `get_kernel_source()` 打印生成的 CUDA 源码（参考 u1-l4 的用法），搜索 `__shared__` 关键字。

**需要观察的现象**：生成的源码里应出现形如 `__shared__ ... A_shared[...]` 的声明；改大 block 后，该数组的元素数应相应翻 4 倍。

**预期结果**：shared buffer 在设备代码里对应 `__shared__` 修饰的静态数组。运行期数值正确性以 `torch.testing.assert_close` 通过为准。

> 说明：本实践需要可用的 CUDA（或 HIP/MACA）环境与 GPU。若无设备，可只做「打印 kernel source 并确认 `__shared__`」的源码阅读部分，运行结果待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `alloc_shared` 要把 `bool` 类型的 scope 改成 `"shared"` 而不是用默认的 `"shared.dyn"`？

**参考答案**：源码注释写明「tilelang 的 merge smem pass 当前无法合并 bool 类型」。`"shared.dyn"` 是动态合并的共享段，会经过该 pass；bool 走动态合并会出问题，所以退化为静态 `"shared"` 段绕开它。

**练习 2**：`alloc_local` 和 `alloc_shared` 的 API 签名几乎一样，区别只在默认 scope。请说出两者在「可见范围」上的差别。

**参考答案**：`shared` 对整个线程块可见（块内线程可共享），`local` 仅对单个线程可见（线程私有）。

---

### 4.2 alloc_fragment：寄存器 tile 与 layout 推断

#### 4.2.1 概念说明

`T.alloc_fragment(shape, dtype)` 申请一块 **fragment** buffer，scope 为 `"local.fragment"`。它在源码层面长得像一块普通的二维数组（比如 GEMM 里 `(BM, BN)` 的累加器 `C_local`），但物理上会被**拆分到线程块内所有线程的寄存器**上。

关键点：**fragment 的「逻辑下标」与「物理寄存器」之间不是一一对应**。你写 `C_local[i, j]`，编译器并不会真的给每个线程都开一个 `(BM, BN)` 的大数组——那会爆寄存器。它靠 **Layout Inference Pass** 推导出一个 `Fragment` 布局，决定「第 t 号线程负责 `C_local` 的哪些 `(i,j)`」。这也是为什么 fragment 特别适合做张量核（MMA/WGMMA/MFMA）的累加器：那些指令本身就要求操作数按特定布局分布在 warp 的寄存器上。

`alloc_local`（scope `"local"`）则不经过这种布局分发，是「你写什么下标就是什么」的普通线程私有数组，适合放标量或小的线程局部缓冲。

#### 4.2.2 核心流程

1. `alloc_fragment(shape, dtype)` 调用 `T.sblock_alloc_buffer(shape, dtype, scope="local.fragment")`，产出 Buffer。
2. 在后续 pass 中，`layout_inference` 扫描到 scope 为 `local.fragment`（或 `shared`）的 buffer，调用相关算子的 `InferLayout()`，推导出一个 `Fragment`/`Layout` 对象。
3. 该布局把 buffer 的逻辑形状重写（rewrite）成「每线程持有若干元素」的物理形态，并把所有 `C_local[i,j]` 的访问改写成对应线程的寄存器访问。
4. 对 copy/gemm 等算子，布局还会带出 swizzle（混淆）以避免 shared memory bank conflict（见 u4-l3）。

#### 4.2.3 源码精读

`alloc_fragment` 的实现：

[tilelang/language/allocate.py:66-77](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/language/allocate.py#L66-L77) —— 与 `alloc_shared` 几乎相同，只是 scope 固定为 `"local.fragment"`。

布局推断 pass 的职责（文件头注释即点明）：

[src/transform/layout_inference.cc:1-4](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/transform/layout_inference.cc#L1-L4) —— 「infer the fragment/shared memory layout」，即推断 fragment 与 shared 的布局。

官方文档对 fragment 与布局推断的说明：

[docs/get_started/overview.md:82](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/docs/get_started/overview.md#L82) —— 明确指出 fragment 对应寄存器文件，且「TileLang uses a Layout Inference Pass during compilation to derive a Layout object `T.Fragment`，决定如何为每个线程分配对应寄存器」。

语言基础文档里 fragment 的典型用法（累加器清零）：

[docs/programming_guides/language_basics.md:128-133](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/docs/programming_guides/language_basics.md#L128-L133) —— `C_local = T.alloc_fragment((BM, BN), 'float32')` 紧跟 `T.clear(C_local)`。

#### 4.2.4 代码实践

**目标**：体会「fragment 是逻辑 tile、物理上被分发」这件事——通过对比 fragment 与 local 在生成代码里的差异。

**步骤**：

1. 写一个最小 kernel（见下方「示例代码」），分别用 `T.alloc_fragment((4,4),'float32')` 和 `T.alloc_local((4,4),'float32')` 各声明一个 buffer，用 `T.Parallel` 给每个元素赋值。
2. 编译并用 `get_kernel_source()` 打印设备源码。
3. 在两份生成代码里数一数：fragment 版本里每个线程实际持有的元素数 vs local 版本里每个线程持有的元素数。

**示例代码（非项目原有，仅供练习）**：

```python
import tilelang
import tilelang.language as T

@tilelang.jit
def frag_demo(N: int, BM: int = 4, BN: int = 4):
    @T.prim_func
    def main(A: T.Tensor((N,), 'float32'), C: T.Tensor((N,), 'float32')):
        with T.Kernel(1, threads=32) as bx:
            f = T.alloc_fragment((BM, BN), 'float32')   # 会被布局推断分发
            l = T.alloc_local((BM, BN), 'float32')      # 线程私有，不分发
            T.clear(f)
            for i, j in T.Parallel(BM, BN):
                l[i, j] = 1.0
            # 仅用于触发代码生成
    return main
```

**需要观察的现象**：fragment 版本里，`f` 的访问通常会被改写成与线程 id 相关的表达式，单线程只持有 `(BM*BN)/threads` 个元素；local 版本则每个线程都持有一份完整的 `(BM,BN)`。

**预期结果**：fragment 的物理寄存器占用远小于 `BM*BN*threads`，而 local 是 `BM*BN*threads`。具体生成代码形态与 target 相关，详细数值**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：GEMM 示例里，累加器 `C_f` 为什么用 `alloc_fragment` 而不是 `alloc_local`？

**参考答案**：因为 `T.gemm` 会把 `C_f` 作为张量核指令（mma/wgmma/mfma）的累加器，这些指令要求操作数按特定布局分布在 warp 寄存器上；`alloc_fragment` 配合 layout 推断正好提供这种「整块 tile 自动分发到线程」的语义，而 `alloc_local` 不会做布局分发。

**练习 2**：如果一块 fragment 形状是 `(128, 128)`、线程块有 128 个线程，且布局推断决定「均匀拆分」，平均每个线程大约持有多少个 float32 元素？

**参考答案**：\(128 \times 128 / 128 = 128\) 个元素（实际布局未必均匀，但数量级如此）。

---

### 4.3 T.copy：跨层级数据搬运

#### 4.3.1 概念说明

`T.copy(src, dst)` 把一块数据从源 buffer 搬到目的 buffer，是 TileLang 里**唯一的主力数据搬运原语**。它的强大之处在于：

- **作用域无关**：你只写 `T.copy(A[...], A_shared)`，至于用 `cp.async`、TMA、还是普通 `for` 循环逐元素搬，由编译器按 target 与 scope 自动选择。
- **自动并行**：它会生成一个并行循环，让线程块内线程协作完成搬运，并自动做向量化与合并访存（coalescing）。
- **形状推断**：两端形状可以不完全写死，`T.copy` 会从 buffer 或 buffer region 推断搬运范围（extent）。

它还有几个**显式变体**用于需要精细控制同步的场景：

| API | 用途 |
|-----|------|
| `T.copy` | 通用搬运，自动选指令，自动插同步 |
| `T.async_copy` | 显式异步 global→shared（cp.async），**不自动插 wait**，需手动 `T.ptx_wait_group` |
| `T.tma_copy` | 显式 TMA，只发 `expect_tx + load`，靠 barrier 手动同步 |
| `T.maca_async_copy` | MACA 后端的 `memcpy_async`（本 fork 专属，见 u7） |

对初学者，绝大多数情况用 `T.copy` 就够了；`async_copy`/`tma_copy` 在 u4-l4（软件流水线）才会用到。

#### 4.3.2 核心流程

`T.copy` 在前端做的事：

1. **规整两端 region**：`_normalize_copy_regions(src, dst)` 把 `Buffer`/`BufferRegion`/`BufferLoad` 三种输入统一成带范围的读写 region；若两端都是标量 `BufferLoad`，直接降级成一句 `BufferStore`（`dst[...] = src[...]`）。
2. **收集标注**：把 `coalesced_width`、`disable_tma`、`eviction_policy`、`prefer_instruction`、`loop_layout` 等关键字参数打包进 `annotations` 字典。
3. **下译成 intrinsic**：发出一个 `tirx.call_intrin("handle", Op.get("tl.tileop.copy"), src, dst, annotations=...)`。

之后由 C++ 侧的 `Copy` 算子接手：构造循环、做布局推断、按 target 分派到具体后端的 lowering。在 CUDA 后端，`Copy::Lower` 会调用 `SelectInst(...)` 在一组候选里挑指令：TMA（bulk）、LDSM/STSM、cp.async、或普通 SIMT 循环。

#### 4.3.3 源码精读

`copy` 函数本体与 intrinsic 下译：

[tilelang/language/copy_op.py:53-133](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/language/copy_op.py#L53-L133) —— 负责规整 region、收集 annotation、最后发出 `tl.tileop.copy` intrinsic。其中第 [133](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/language/copy_op.py#L133) 行是关键的下译点：

```python
return tirx.call_intrin("handle", tirx.op.Op.get("tl.tileop.copy"), src, dst, annotations=ann if ann else None)
```

region 规整逻辑（决定「搬多大」）：

[tilelang/language/copy_op.py:16-50](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/language/copy_op.py#L16-L50) —— `_normalize_copy_regions`：两端都是 buffer 时断言形状相等；缺失范围的一端按另一端长度补齐（有限的广播糖）；最终用 `to_buffer_region` 编码成 `tl.region`。

C++ 侧 `Copy` 算子构造与 target 分派：

[src/op/copy.cc:270-291](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/op/copy.cc#L270-L291) —— 从 intrinsic 参数构造 `CopyNode`，拆出 `src`/`dst` buffer 及其范围。
[src/op/copy.cc:92-107](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/op/copy.cc#L92-L107) —— `ResolveCopyImpl` 在注册表里按 `match_target` + `priority` 选出最高优先级的后端 copy 实现（这就是「按 target 分派」的机制）。

CUDA 后端的指令选择（「自动选 cp.async/TMA」的真相）：

[src/cuda/op/copy.cc:707-749](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/cuda/op/copy.cc#L707-L749) —— `Copy::Lower` 先用 `SelectInst(...)` 选出一个 `CopyInst`，再用一串 `if/else` 分派到 `LowerBulk`（TMA）、`LowerLDSM`、`LowerCPAsync`、或 `LowerNormal`。这就是同一句 `T.copy` 能落到不同指令的根源。

普通 SIMT 拷贝的循环构造：

[src/op/copy.cc:31-83](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/op/copy.cc#L31-L83) —— `LowerNormalCopy`：构造 SIMT 并行循环、融合、向量化；对 CPU target 或涉及 local buffer 的情况走向量化路径，否则做布局推断后 lowering。

官方文档对 `T.copy` 的说明：

[docs/programming_guides/language_basics.md:135-157](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/docs/programming_guides/language_basics.md#L135-L157) —— 给出 global→shared、fragment→global 的典型写法，并指出 `T.copy` 在编译期做合并访存与 scope 相关 lowering。

#### 4.3.4 代码实践

**目标**：在一个真实可运行示例里跟踪一次完整的「global→shared→fragment→global」搬运链。

**步骤**：

1. 打开 `examples/elementwise/example_elementwise_add.py`，阅读 [第 25–30 行](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/examples/elementwise/example_elementwise_add.py#L25-L30)：

   ```python
   T.copy(A[by * block_M, bx * block_N], A_shared)   # global -> shared
   T.copy(B[by * block_M, bx * block_N], B_shared)   # global -> shared
   for local_y, local_x in T.Parallel(block_M, block_N):
       C_local[local_y, local_x] = A_shared[local_y, local_x] + B_shared[local_y, local_x]
   T.copy(C_local, C_shared)                          # fragment -> shared
   T.copy(C_shared, C[by * block_M, bx * block_N])    # shared -> global
   ```

2. 在第 1、4、5 行的 `T.copy` 调用处，分别判断它的 `src`/`dst` 各属于哪个 scope（global / shared / fragment）。
3. 运行 `python examples/elementwise/example_elementwise_add.py`，确认 `torch.testing.assert_close` 通过。

**需要观察的现象**：四条 `T.copy` 分别覆盖了 global↔shared 与 fragment↔shared 的搬运；其中 `A[by*block_M, ...]` 是对一个 global `T.Tensor` 取子区域（buffer load with region），`A_shared` 是整块 shared buffer，两端形状由规整逻辑对齐。

**预期结果**：输出与 `ref_program(a, b) = a + b` 数值一致。运行结果**待本地验证**（需 GPU）。

#### 4.3.5 小练习与答案

**练习 1**：在 `example_elementwise_add.py` 里，`T.copy(C_local, C_shared)` 的两端 scope 分别是什么？为什么这里要先搬到 shared 再搬到 global，而不是 fragment 直接写 global？

**参考答案**：`C_local` 是 `local.fragment`，`C_shared` 是 `shared.dyn`。先 fragment→shared 再 shared→global 是为了把「跨线程的寄存器聚合」与「写回显存」两步分开，便于合并写回（coalesced store）；fragment 直接写 global 也可以，但经过 shared 中转通常更利于生成高效的合并访存代码。

**练习 2**：`T.copy` 和 `T.async_copy` 的关键差别是什么？

**参考答案**：`T.copy` 是语义完整的同步搬运（编译器自动选指令并插入必要的同步）；`T.async_copy` 显式走 cp.async 且**不自动插入 wait**，用户必须自己用 `T.ptx_wait_group` 等显式同步后才能消费目的 buffer——它用于手动控制软件流水线的场景。

---

### 4.4 T.clear / T.fill：buffer 初始化

#### 4.4.1 概念说明

- `T.clear(buffer)`：把一整块 buffer 清零（等价于填 0）。
- `T.fill(buffer, value)`：把一整块 buffer（或其子区域）填充为指定值。

它们最常见的用途是**初始化累加器**：GEMM 里 `C_f` 在累加前必须先清零，否则会带上未初始化的垃圾值。这两个 API 在源码层非常薄——`clear` 本质上就是 `fill(buffer, 0)`。

#### 4.4.2 核心流程

1. `fill(buffer, value)`：先把 `buffer`（可能是 `Var`、`Buffer`、`BufferRegion`、`BufferLoad`）归一化，取出其形状作为 extents，再用 `to_buffer_region(buffer, access_type="w", extents=...)` 编码成可写 region，最后发出 `tl.tileop.fill` intrinsic。
2. `clear(buffer)`：若是带 let 绑定的 `Var`，先解出底层 region；否则直接 `fill(buffer, 0)`。
3. C++ 侧 `Fill` 算子（`src/op/fill.cc`）按 target 分派到具体 lowering，逐元素写入。

#### 4.4.3 源码精读

`fill` 实现：

[tilelang/language/fill_op.py:10-37](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/language/fill_op.py#L10-L37) —— 根据 buffer 的不同形态取 extents，下译为 `tl.tileop.fill`：

```python
return tirx.call_intrin("handle", tirx.op.Op.get("tl.tileop.fill"),
                        to_buffer_region(buffer, access_type="w", extents=extents), value)
```

`clear` 实现：

[tilelang/language/fill_op.py:40-63](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/language/fill_op.py#L40-L63) —— 解开 let 绑定后，最终都落到 `fill(buffer, 0)`。

官方文档里 clear/fill 的定位：

[docs/get_started/overview.md:84](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/docs/get_started/overview.md#L84) —— 「hardware-specific buffers can be initialized using `T.clear` or `T.fill`」。

语言基础文档里的典型用法（清零累加器）：

[docs/programming_guides/language_basics.md:131-132](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/docs/programming_guides/language_basics.md#L131-L132) —— `C_local = T.alloc_fragment((BM, BN), 'float32')` 紧跟 `T.clear(C_local)`。

#### 4.4.4 代码实践

**目标**：验证「累加器不清零会导致结果错误」，体会 `T.clear` 的必要性。

**步骤**：

1. 复制 `examples/gemm/example_gemm.py` 为一份练习用脚本（不要改原文件）。
2. 在 GEMM kernel 里找到累加器分配与清零处（参考 language_basics.md 的 GEMM 骨架：`C_f = T.alloc_fragment(...)` 后紧跟 `T.clear(C_f)`）。
3. 注释掉 `T.clear(C_f)` 这一行，重新编译运行，与 `torch` 参考实现对比。

**需要观察的现象**：注释掉 `T.clear` 后，结果应出现明显的数值错误（因为累加器初始值不确定）。

**预期结果**：保留 `T.clear` 时数值正确；去掉后数值错误。运行结果**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：`T.clear(buf)` 在源码层面等价于什么调用？

**参考答案**：等价于 `T.fill(buf, 0)`（见 `fill_op.py` 中 `clear` 的实现，最终都调用 `fill(buffer, 0)`）。

**练习 2**：`T.fill` 接受的 `buffer` 参数可以是哪几种形态？

**参考答案**：可以是 `tirx.Buffer`（取其 shape 作为 extents）、`tirx.BufferRegion`（取各维 extent）、或 `tirx.BufferLoad`（从 load 推断 region）；若是带 let 值的 `Var`，会先解出底层对象。

---

## 5. 综合实践

把本讲四个模块串起来，完成下面这个**贯穿型任务**：

> 写一个 ReLU kernel：输入矩阵 `A`，按 tile 从 **global** 拷到 **shared**，再拷到 **fragment**，在 fragment 上做一次 ReLU（`max(x, 0)`），最后写回 **global** 的输出 `C`。运行并与 `torch.relu` 对比验证。

**参考骨架（示例代码，非项目原有文件，需自行保存为脚本运行）**：

```python
import torch
import tilelang
import tilelang.language as T

@tilelang.jit
def relu_kernel(M: int, N: int, BM: int, BN: int, threads: int, dtype="float32"):
    @T.prim_func
    def main(
        A: T.Tensor((M, N), dtype),
        C: T.Tensor((M, N), dtype),
    ):
        with T.Kernel(T.ceildiv(N, BN), T.ceildiv(M, BM), threads=threads) as (bx, by):
            A_shared = T.alloc_shared((BM, BN), dtype)      # shared 中转
            A_frag   = T.alloc_fragment((BM, BN), dtype)     # fragment 计算
            C_shared = T.alloc_shared((BM, BN), dtype)

            T.copy(A[by * BM, bx * BN], A_shared)            # global -> shared
            T.copy(A_shared, A_frag)                         # shared  -> fragment
            for i, j in T.Parallel(BM, BN):                  # ReLU on fragment
                A_frag[i, j] = T.max(A_frag[i, j], 0.0)
            T.copy(A_frag, C_shared)                         # fragment -> shared
            T.copy(C_shared, C[by * BM, bx * BN])            # shared   -> global
    return main

# 主机侧验证（需 GPU，否则待本地验证）
M = N = 1024
a = torch.randn(M, N, device="cuda", dtype=torch.float32)
out = relu_kernel(M, N, BM=32, BN=32, threads=128, dtype="float32")(a)
torch.testing.assert_close(out, torch.relu(a), rtol=1e-5, atol=1e-5)
print("ReLU kernel passed")
```

**验收清单**：

- [ ] 用到了 `alloc_shared` 与 `alloc_fragment` 两种作用域。
- [ ] `T.copy` 至少完成 3 次跨层级搬运（global→shared、shared→fragment、fragment→shared、shared→global）。
- [ ] 在 fragment 上做了 ReLU 计算（`T.Parallel` + `T.max`）。
- [ ] 数值与 `torch.relu` 一致。

**进阶**：把 fragment 直接写回 global（跳过 `C_shared` 中转），对比两版延迟，思考为什么官方示例偏好「fragment→shared→global」两步走（提示：合并写回）。

## 6. 本讲小结

- TileLang 把 GPU 显存抽象成几级 **scope**：`global`（显存）、`shared.dyn`（共享内存）、`local.fragment`（寄存器 tile）、`local`（线程私有），分别由 `T.Tensor`/`alloc_global`、`alloc_shared`、`alloc_fragment`、`alloc_local` 申请。
- `alloc_shared`/`alloc_local`/`alloc_fragment` 在源码层都极薄，核心是带特定 scope 调 `T.sblock_alloc_buffer`；scope 的层级高低在 C++ `scope_level` 里被固化（fragment/local=2，shared=1，global=0）。
- **fragment 与 local 的本质区别**：fragment 会被 Layout Inference Pass 自动分发到各线程的寄存器（逻辑下标 ≠ 物理寄存器），适合做张量核累加器；local 不做布局分发，是普通线程私有数组。
- `T.copy(src, dst)` 是跨层级搬运的主力原语，前端只规整 region 并下译成 `tl.tileop.copy` intrinsic；具体用 TMA/cp.async/普通循环由 C++ 侧按 target 与 scope 自动选择（CUDA 后端在 `Copy::Lower` 里分派）。
- `T.copy` 还提供 `async_copy`/`tma_copy`/`maca_async_copy` 等显式变体，用于需要手动同步的软件流水线场景。
- `T.clear` / `T.fill` 负责 buffer 初始化，`clear` 本质就是 `fill(buf, 0)`；累加器在累加前必须清零。

## 7. 下一步学习建议

- **进入编译流水线**：本讲多次提到「layout 推断」「copy 自动选指令」，它们的内部机制在 **u4-l1（lowering 流程）** 与 **u4-l3（Layout/Fragment 布局推断）** 详细展开，建议接着读。
- **软件流水线**：`T.async_copy` / `T.tma_copy` 的真正用武之地是和 `T.Pipelined` 配合隐藏访存延迟，见 **u4-l4（软件流水线与异步拷贝）**。
- **后端差异**：同样的 `T.copy` 在 MACA 后端会走 `maca_async_copy`（memcpy_async + barrier），见 **u7（Metax/MACA 后端）**。
- **直接读源码**：想看「copy 到底生成了什么指令」，最快的方法是给 kernel 调 `get_kernel_source()` 打印设备代码，再对照本讲的 C++ 入口（`src/op/copy.cc`、`src/cuda/op/copy.cc`）理解每一段的来历。
