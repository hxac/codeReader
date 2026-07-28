# tile 算子与 T.gemm 的分派

## 1. 本讲目标

本讲是「编译流水线」单元的第二讲，承接 u4-l1 的 lowering 主框架，钻进其中最重要的一类 tile 算子——**GEMM**（矩阵乘）。

读者学完后应该能够：

- 说清楚一句 `T.gemm(A, B, C)` 在 Python 层是如何被规整、打包成一棵 intrinsic 调用的，以及它在 C++ 层被反序列化成什么样的 `GemmNode`。
- 理解 GEMM 的**两级分派**机制：C++ 侧 `select_inst` 决定走哪条指令（`cuda.wgmma` / `cuda.mma` / `cuda.tcgen05` / `maca.mma` / `rocm.mfma` …），Python 侧 `resolve_gemm_impl` 再把指令键映射到真正的实现类。
- 区分 `GemmWarpPolicy`（`Square` / `FullRow` / `FullCol`）三种 warp 划分策略，以及它们如何随 target（尤其 MACA 的 `warp_size=64`）变化。
- 把握 tile 算子的两个核心虚方法 `Lower()` 与 `InferLayout()`，理解它们「C++ 做壳、Python 做事」的协作方式。

## 2. 前置知识

在进入本讲前，请确认你已经理解以下概念（它们在前序讲义中已建立）：

- **tile 算子（TileOperatorNode）**：TileLang 里「一块 tile 上的高级算子」抽象，每个算子都要实现 `Lower()`（下译成 TIR）和 `InferLayout()`（推断寄存器/共享内存布局）两个核心方法（见 u1-l3、u5-l1）。
- **内存层级与 fragment**：`T.copy` 在 `global`/`shared`/`fragment` 间搬运；`fragment` 的逻辑下标与物理寄存器不一一对应，由 Layout Inference Pass 自动分发到各线程（见 u2-l4）。
- **lowering 主流程**：`tilelang.engine.lower` 先跑语义检查，再按 target 取 pass 流水线降级 IR，最后拆分 host/device（见 u4-l1）。
- **TIR intrinsic 调用**：`tirx.call_intrin("handle", Op, ...args)` 是 TileLang 把高级算子挂进 TIR 的统一手段，真正的下译发生在后续 pass 里。

本讲用到的两个关键词先点出来：

- **MMA（Matrix Multiply-Accumulate）**：张量核做一次小矩阵乘加的硬件指令，不同厂商叫法不同——NVIDIA 叫 MMA/WGMMA/TCGEN5MMA，AMD 叫 MFMA/WMMA，MetaX 的 MACA 也叫 MMA（指令前缀不同）。
- **warp**：GPU 上若干线程组成的执行单元。NVIDIA 一个 warp = 32 线程，MACA/ROCm 一个 warp（wavefront）= 64 线程。这个差异直接影响 GEMM 的 warp 划分。

## 3. 本讲源码地图

本讲涉及的关键文件按职责分成四组：

| 文件 | 角色 |
|------|------|
| [tilelang/language/gemm_op.py](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/language/gemm_op.py) | DSL 表面层。定义用户写的 `T.gemm` / `T.wgmma_gemm` / `T.tcgen05_gemm`，把它们打包成 intrinsic 调用。 |
| [tilelang/tileop/base.py](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/tileop/base.py) | 定义 `GemmWarpPolicy` 枚举与 warp 划分算法。 |
| [tilelang/tileop/gemm/__init__.py](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/tileop/gemm/__init__.py) | Python 侧 `Gemm` 对象。注册 `tl.gemm.infer_layout` / `tl.gemm.lower` 两个全局函数，编排「选指令→选实现类→下译」。 |
| [tilelang/tileop/gemm/registry.py](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/tileop/gemm/registry.py) | Python 侧的 GEMM 实现注册表：`register_gemm_impl` / `resolve_gemm_impl`。 |
| [tilelang/tileop/gemm/gemm_base.py](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/tileop/gemm/gemm_base.py) | 所有 GEMM 实现类的公共基类 `GemmBase`，按 A/B 的 scope 分类（SS/SR/RS/TS/RR）。 |
| [src/op/gemm.h](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/op/gemm.h) | C++ 侧 `GemmNode` / `GemmImpl` 结构体定义。 |
| [src/op/gemm.cc](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/op/gemm.cc) | C++ 侧 `GemmNode` 的 `Lower` / `InferLayout` / 反序列化构造，以及实现注册表 `ResolveGemmImpl`。 |
| [src/cuda/op/gemm.cc](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/cuda/op/gemm.cc) | CUDA 的指令选择（WGMMA/TCGEN05/MMA 判定）。 |
| [src/maca/op/gemm.cc](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/maca/op/gemm.cc) | MACA 的指令选择（恒为 `maca.mma`）。 |
| [tilelang/maca/op/gemm/gemm_mma.py](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/maca/op/gemm/gemm_mma.py) | MACA 侧 GEMM 的真正实现类 `GemmMMA`（做 infer_layout / lower）。 |

## 4. 核心概念与源码讲解

### 4.1 Gemm 算子：从 T.gemm 到 intrinsic 调用

#### 4.1.1 概念说明

用户在 kernel 里写的是一句极简的 `T.gemm(A_shared, B_shared, C_local)`——「把 A、B 这两块 tile 相乘，累加到 C」。这句话本身**不产生任何计算**，它只是把「要做一次分块矩阵乘」这件事登记到 IR 里。真正生成什么样的张量核指令、怎么划分 warp、怎么排布寄存器，全部交给后面的分派与下译。

TileLang 一共暴露了三个同步/异步程度不同的 GEMM 入口，全部由同一个内部实现 `_gemm_impl` 收口，区别只在于传给 C++ 的 **op_key** 不同：

| DSL 入口 | op_key | 语义 |
|----------|--------|------|
| `T.gemm(...)` | `tl.tileop.gemm` | 默认同步接口。编译器自动选指令，并在 Hopper 上自动插 `warpgroup_wait`、在 Blackwell 上自动插 `mbarrier_wait_parity`。 |
| `T.wgmma_gemm(...)` | `tl.tileop.wgmma_gemm` | 显式 Hopper WGMMA，**强制走 WGMMA**，不自动插 wait，约束不满足直接编译失败。 |
| `T.tcgen05_gemm(...)` | `tl.tileop.tcgen05_gemm` | 显式 Blackwell TCGEN5MMA，强制走 TCGEN05，需提供 `mbar`。 |

#### 4.1.2 核心流程

`_gemm_impl` 做的事可以拆成五步：

1. **legalize**：把 let 绑定的变量还原成真正的 buffer。
2. **取 region**：用 `to_buffer_region` 把 A/B/C 统一成 `BufferRegion`，便于取形状/步长/偏移。
3. **形状校验**：从 A/B/C 的后两维推导出 M/N/K，断言它们自洽（含 2CTA 模式下 `N_B == N/2` 的特殊校验）。
4. **转 tile region**：把 `BufferRegion` 转成 `tl.region(...)` 调用作为 intrinsic 参数。
5. **发出 intrinsic**：`tirx.call_intrin("handle", Op.get(op_key), ...19 个参数..., annotations=...)`。

#### 4.1.3 源码精读

先看三个入口如何收口到同一个 `_gemm_impl`，注意它们传的 op_key 与几个关键默认值的差异：

默认同步接口 `T.gemm`，op_key 为 `tl.tileop.gemm`，`wg_wait=0`（[gemm_op.py:149-198](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/language/gemm_op.py#L149-L198)）：

```python
def gemm(A, B, C, transpose_A=False, transpose_B=False,
         policy: GemmWarpPolicy = GemmWarpPolicy.Square,
         clear_accum=False, k_pack=1, mbar=None):
    return _gemm_impl("tl.tileop.gemm", A, B, C, ..., 0, mbar)
```

显式异步入口 `T.wgmma_gemm`，op_key 为 `tl.tileop.wgmma_gemm`，`k_pack=1`、`wg_wait=-1`（[gemm_op.py:201-233](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/language/gemm_op.py#L201-L233)）：

```python
return _gemm_impl("tl.tileop.wgmma_gemm", A, B, C, ..., 1, -1, None)
```

显式 Blackwell 入口 `T.tcgen05_gemm`，op_key 为 `tl.tileop.tcgen05_gemm`，并往 annotations 里塞 `is_tcgen05=1`（[gemm_op.py:236-278](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/language/gemm_op.py#L236-L278)）：

```python
ann = {"is_tcgen05": 1}
if use_2cta:
    ann["use_2cta"] = 1
return _gemm_impl("tl.tileop.tcgen05_gemm", A, B, C, ..., 1, 0, mbar, annotations=ann)
```

`_gemm_impl` 的形状推导与最后的 intrinsic 发射（[gemm_op.py:85-146](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/language/gemm_op.py#L85-L146)）。注意它从 C 取 M/N、从 A 取 K，并断言 `M_A==M`、`K==K_B`、`N_B==N`：

```python
M, N = C_shape
M_A = A_shape[-1] if transpose_A else A_shape[-2]
K   = A_shape[-2] if transpose_A else A_shape[-1]
...
return tirx.call_intrin(
    "handle", tirx.op.Op.get(op_key),
    A_arg, B_arg, C_arg,
    transpose_A, transpose_B, M, N, K, policy, clear_accum,
    stride_a, stride_b, offset_a, offset_b, k_pack, wg_wait,
    mbar_arg, C_coords[0], C_coords[1], annotations=annotations)
```

> 位置约定：这一长串参数的顺序与 C++ 侧 `Gemm::Gemm(Array<PrimExpr> args, ...)` 的反序列化顺序**一一对应**，下文 4.1.3 末尾会看到。

C++ 侧的算子注册与反序列化（[src/op/gemm.cc:263-294](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/op/gemm.cc#L263-L294)）把三个 op_key 都绑到同一个 `Gemm` 类，只是用 annotations 打标记区分：

```cpp
TIR_REGISTER_TL_TILE_OP(Gemm, gemm) ...          // tl.tileop.gemm
TVM_REGISTER_OP("tl.tileop.wgmma_gemm")          // 注入 is_wgmma=1
    .set_attr<OpBuilderFunc>("TLOpBuilder", [](args, annotations){
        ann.Set("is_wgmma", IntImm(DataType::Int(32), 1));
        return Gemm(args, ann); });
TVM_REGISTER_OP("tl.tileop.tcgen05_gemm")        // 注入 is_tcgen05=1
    .set_attr<OpBuilderFunc>("TLOpBuilder", [](args, annotations){
        ann.Set("is_tcgen05", IntImm(DataType::Int(32), 1));
        return Gemm(args, ann); });
```

反序列化构造函数 `Gemm::Gemm` 按固定位置取出参数填进 `GemmNode`（[src/op/gemm.cc:83-144](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/op/gemm.cc#L83-L144)）：

```cpp
node->transA_ = args[3].as<Bool>().value();
node->m_ = args[5].as<IntImm>().value()->value;   // 对应 Python 的 M
node->n_ = args[6]...;  node->k_ = args[7]...;
node->policy_ = GemmWarpPolicy(args[8]...);        // 对应 Python 的 policy
node->clearAccum_ = args[9]...;
node->strideA_ = args[10]...; node->strideB_ = args[11]...;
...
if (auto val = annotations.Get("is_wgmma"))  node->isWgmma_  = ...;
if (auto val = annotations.Get("is_tcgen05")) node->isTcgen05_ = ...;
```

`GemmNode` 的字段在头文件里定义（[src/op/gemm.h:103-174](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/op/gemm.h#L103-L174)），其中 `isWgmma_` / `isTcgen05_` 两个布尔位是后续指令选择的关键输入。

#### 4.1.4 代码实践

**实践目标**：直观感受「同一个 `_gemm_impl` 因 op_key 与 annotations 不同而走向不同指令」。

**操作步骤**（源码阅读型，无需 GPU）：

1. 打开 [examples/gemm/example_gemm.py:22](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/examples/gemm/example_gemm.py#L22)，确认它调用的是默认同步接口 `T.gemm(A_shared, B_shared, C_local)`，未传 `policy`（故为 `Square`）。
2. 对照 [gemm_op.py:149-198](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/language/gemm_op.py#L149-L198)，写下这一次调用最终发出的 intrinsic 的 op_key 与 annotations（应为 `"tl.tileop.gemm"`、annotations 为空，即既非 wgmma 也非 tcgen05）。
3. 假设把这一行换成 `T.wgmma_gemm(A_shared, B_shared, C_local)`，根据 [gemm_op.py:201-233](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/language/gemm_op.py#L201-L233) 写出新的 op_key 与 annotations。

**需要观察的现象**：op_key 由 `tl.tileop.gemm` 变为 `tl.tileop.wgmma_gemm`，annotations 里多出 `is_wgmma=1`，二者在 C++ 侧都会构造出 `GemmNode`，但 `isWgmma_` 字段不同。

**预期结果**：三种入口的差异仅体现在 op_key 与两个布尔 annotation 上，`_gemm_impl` 的其余逻辑完全共用。

#### 4.1.5 小练习与答案

**练习 1**：`T.gemm` 的默认 `policy` 是什么？它定义在哪个文件？

> 答案：`GemmWarpPolicy.Square`，定义在 [tilelang/tileop/base.py:10](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/tileop/base.py#L10)。

**练习 2**：为什么 `T.tcgen05_gemm` 必须传 `mbar`，而 `T.gemm` 把它设成可选？

> 答案：TCGEN5MMA 是异步指令，需要 mbarrier 来通知完成；默认 `T.gemm` 在被选中走 TCGEN05 时由编译器自动处理同步，而显式的 `tcgen05_gemm` 把同步责任交给用户，因此要求显式提供 `mbar`（见 [gemm_op.py:180-181](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/language/gemm_op.py#L180-L181) 的 docstring 与 [gemm_op.py:262-264](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/language/gemm_op.py#L262-L264) 的 `ann` 构造）。

---

### 4.2 指令选择：C++ 选键 + Python 选类

#### 4.2.1 概念说明

GEMM 的分派是**两级**的，这是本讲最容易绕晕、也最关键的设计：

1. **第一级（C++）**：`GemmNode::GetGemmInstructionKey` 调用 `ResolveGemmImpl(target).select_inst(...)`，根据 **target + dtype + 形状 + scope + isWgmma/isTcgen05 标记**，返回一个**指令键字符串**，比如 `"cuda.wgmma"`、`"maca.mma"`。
2. **第二级（Python）**：Python 侧拿着这个键，去 `_GEMM_IMPLS` 注册表里 `resolve_gemm_impl(gemm_inst, target)`，找到真正干活的**实现类**（如 `GemmWGMMA` / `GemmMMA`），由它完成 `infer_layout` 与 `lower`。

为什么要分两级？因为「选哪条指令」依赖大量 C++ 侧的硬件判定（`TargetIsHopper`、`TargetIsSm100`、dtype 对齐等），而「怎么把这条指令发射成 TIR」是一大段模板化的 Python 代码（发射器、布局工具），两者天然分层。

#### 4.2.2 核心流程

C++ 侧的 `GemmImpl` 是一个函数指针结构体（[src/op/gemm.h:178-191](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/op/gemm.h#L178-L191)）：

```text
struct GemmImpl {
  name;                          // 形如 "cuda.Gemm"
  match_target(target);          // 该实现是否匹配此 target
  select_inst(op, block, target) // → 返回指令键，如 "cuda.wgmma"
  compute_warp_partition(...);   // → 返回 (m_warp, n_warp)
  reuse_existing_shared_layout(gemm_inst);
};
```

每个后端在文件加载时把自家 `GemmImpl` 推进一个全局 vector。`ResolveGemmImpl` 线性扫描，要求**恰好一个**实现匹配该 target（[src/op/gemm.cc:37-54](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/op/gemm.cc#L37-L54)）：

```text
ResolveGemmImpl(target):
    for impl in registry:
        if impl.match_target(target):
            ICHECK(此前无匹配)   // 多个匹配则报错
            matched = impl
    ICHECK(matched != null)       // 一个都没有也报错
    return matched
```

选键的优先级（CUDA，[src/cuda/op/gemm.cc:266-287](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/cuda/op/gemm.cc#L266-L287)）：

```text
SelectInst(op, block, target):
    if op.isWgmma_:   校验 AllowWgmma → "cuda.wgmma"   # 显式强制
    if op.isTcgen05_: 校验 AllowTcgen5Mma → "cuda.tcgen05"
    if AllowTcgen5Mma(...): return "cuda.tcgen05"      # 自动：Blackwell 优先
    if AllowWgmma(...):     return "cuda.wgmma"        # 自动：其次 Hopper
    return "cuda.mma"                                   # 兜底：普通 MMA
```

各后端的指令键汇总（这是本讲最该记住的一张表）：

| target | C++ select_inst 返回键 | Python 实现类 | 触发条件 |
|--------|------------------------|---------------|----------|
| CUDA（Blackwell sm100） | `cuda.tcgen05` | `GemmTCGEN5` | `AllowTcgen5Mma`：sm100 + A/B in shared、C in shared.tmem + dtype 受 `GetTCGEN5MMAMeta` 支持 |
| CUDA（Hopper sm90） | `cuda.wgmma` | `GemmWGMMA` | `AllowWgmma`：`TargetIsHopper` + `m_>=64` + `num_warps%4==0` + `CheckWgmma`（dtype/scope/对齐）|
| CUDA（其它） | `cuda.mma` | `GemmMMA` / `GemmMMASm70`(Volta) / `GemmMMASm75`(Turing) | 兜底；细分由 Python predicate 决定 |
| MACA | `maca.mma` | `GemmMMA`（maca 版） | 恒为 `maca.mma`（[src/maca/op/gemm.cc:142-144](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/maca/op/gemm.cc#L142-L144)） |
| ROCm（CDNA） | `rocm.mfma` | `GemmMFMA` | `TargetIsCDNA` |
| ROCm（RDNA） | `rocm.wmma` | `GemmWMMA` | `TargetIsRDNA` |
| CPU | `cpu.scalar` | `GemmScalar` | 恒为标量回退 |

WGMMA 的可行性校验 `CheckWgmma` 是一张精细的「dtype × 对齐 × 转置」表（[src/cuda/op/gemm.cc:37-75](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/cuda/op/gemm.cc#L37-L75)），例如：

```cpp
if (op.c_->dtype == DataType::Float(16)) {
  if (fp16 x fp16)            return op.k_ % 16 == 0;
  if (fp8 x fp8)              return (!transA) && transB && op.k_ % 32 == 0;
}
if (op.c_->dtype == DataType::Float(32)) {
  ...
  if (tf32 x tf32)            return (!transA) && transB && op.k_ % 8 == 0;
}
```

值得对比的是 **MACA 的 `CheckWgmma`**（[src/maca/op/gemm.cc:28-67](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/maca/op/gemm.cc#L28-L67)）：它虽然定义了与 CUDA 几乎一样的 dtype 对齐表（注意 MACA 多出一条 `Float(32) x Float(32) → k_%8==0`，对应其 TF32 是原生 fp32 入口），但 **MACA 的 `SelectInst` 当前并不调用它**——直接返回 `maca.mma`。也就是说 MACA 暂时只有一条 MMA 路径，`CheckWgmma` 是为未来扩展预留的。

第二级（Python）的注册表用法（[tilelang/tileop/gemm/registry.py:38-46](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/tileop/gemm/registry.py#L38-L46)）：

```python
def resolve_gemm_impl(gemm_inst: str, target: Target) -> type:
    matches = [e for e in _GEMM_IMPLS
               if e.inst_name == gemm_inst and e.predicate(target)]
    if not matches:   raise ValueError(...)
    if len(matches) > 1: raise ValueError(...)   # 同样要求唯一
    return matches[0].impl_class
```

注意 CUDA 的 MMA 在 Python 侧注册了**三条同名 `cuda.mma`**（[tilelang/cuda/op/gemm/__init__.py:34-38](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/cuda/op/gemm/__init__.py#L34-L38)），靠 predicate 互斥区分 Volta/Turing/其余：

```python
register_gemm_impl("cuda.mma",      GEMM_INST_MMA, _match_mma,       GemmMMA)       # 非 volta/turing
register_gemm_impl("cuda.mma_sm70", GEMM_INST_MMA, _match_mma_sm70,  GemmMMASm70)   # Volta
register_gemm_impl("cuda.mma_sm75", GEMM_INST_MMA, _match_mma_sm75,  GemmMMASm75)   # Turing
register_gemm_impl("cuda.wgmma",    GEMM_INST_WGMMA, _match_wgmma,   GemmWGMMA)
register_gemm_impl("cuda.tcgen05",  GEMM_INST_TCGEN05, _match_tcgen05, GemmTCGEN5)
```

而 MACA 只注册一条（[tilelang/maca/op/gemm/__init__.py:10](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/maca/op/gemm/__init__.py#L10)）：

```python
register_gemm_impl("maca.mma", GEMM_INST_MMA, target_is_maca, GemmMMA)
```

#### 4.2.3 源码精读

C++ 侧把「选键」暴露给 Python 的桥（[src/op/gemm.cc:168-170](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/op/gemm.cc#L168-L170) 与 [src/op/gemm.cc:309-313](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/op/gemm.cc#L309-L313)）：

```cpp
String GemmNode::GetGemmInstructionKey(int block_size, Target target) const {
  return ResolveGemmImpl(target).select_inst(*this, block_size, target);
}
// 反射注册，供 _ffi_api.GemmGetGemmInstructionKey 调用
refl::GlobalDef().def("tl.GemmGetGemmInstructionKey", ...);
```

Python 侧 `Gemm` 对象的两个编排方法（[tilelang/tileop/gemm/__init__.py:141-174](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/tileop/gemm/__init__.py#L141-L174)）：

```python
def _select_gemm_instruction(self, thread_nums, target):
    return str(_ffi_api.GemmGetGemmInstructionKey(self, int(thread_nums), target))

def _get_implementation_class(self, gemm_inst, target):
    return resolve_gemm_impl(gemm_inst, target)
```

`infer_layout` 与 `lower` 都先选指令、再选类、再委托（[tilelang/tileop/gemm/__init__.py:121-139](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/tileop/gemm/__init__.py#L121-L139)）：

```python
def infer_layout(self, target, thread_nums):
    gemm_inst = self._select_gemm_instruction(thread_nums, target)
    impl_class = self._get_implementation_class(gemm_inst, target)
    return impl_class(self).infer_layout(target, thread_nums)
```

#### 4.2.4 代码实践

**实践目标**：手工模拟一次「target/dtype → 指令键」的分派，把抽象表格落到具体源码。

**操作步骤**（源码阅读型）：

1. 假设 kernel 为 [example_gemm.py](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/examples/gemm/example_gemm.py)，参数 `block_M=128, block_N=128, block_K=32`，`A/B` 为 `float16`、`C_local` 为 `float32`，`threads=128`。
2. 针对 **CUDA**：算出 `num_warps = 128/32 = 4`，依次判断 [AllowTcgen5Mma](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/cuda/op/gemm.cc#L77-L85)、[AllowWgmma](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/cuda/op/gemm.cc#L87-L95)，得出：
   - 若 target 是 **Hopper（sm90）**：`m_=128>=64`、`num_warps%4==0`、`CheckWgmma` 命中 fp16×fp16→fp32 且 `k=32%16==0` → `cuda.wgmma`。
   - 若 target 是 **Ampere（sm80）**：不满足 Hopper → `cuda.mma`，Python 再由 predicate 选 `GemmMMA`。
   - 若 target 是 **Blackwell（sm100）**：但 C 在 fragment 而非 `shared.tmem` → `AllowTcgen5Mma` false → 退回 `cuda.wgmma` 或 `cuda.mma`。
3. 针对 **MACA**：直接读 [src/maca/op/gemm.cc:142-144](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/maca/op/gemm.cc#L142-L144)，无条件返回 `maca.mma`，Python 选 `tilelang/maca/op/gemm/gemm_mma.py` 的 `GemmMMA`。

**需要观察的现象 / 预期结果**：填出如下分派表（待本地用 `target=...` 编译并 `get_kernel_source()` 核对生成的指令名）：

| target | dtype(A,B,C) | num_warps | 指令键 | Python 实现类 |
|--------|--------------|-----------|--------|---------------|
| cuda sm90 | fp16,fp16,fp32 | 4 | `cuda.wgmma` | `GemmWGMMA` |
| cuda sm80 | fp16,fp16,fp32 | 4 | `cuda.mma` | `GemmMMA` |
| maca | fp16,fp16,fp32 | 2（128/64） | `maca.mma` | `GemmMMA`(maca) |
| rocm CDNA | fp16,fp16,fp32 | — | `rocm.mfma` | `GemmMFMA` |
| cpu | — | — | `cpu.scalar` | `GemmScalar` |

> 若无 GPU 可用，本步骤为纯源码阅读，结论即上表；如能在 Hopper 机器上跑 `kernel.get_kernel_source()`，应能在生成的 CUDA 源码里看到 `wgmma` 指令。

#### 4.2.5 小练习与答案

**练习 1**：为什么 CUDA 的 MMA 在 C++ 侧只返回一个键 `cuda.mma`，却在 Python 侧注册了三个同名实现？

> 答案：C++ 的 `select_inst` 只做「粗分」（mma / wgmma / tcgen05），而 Volta/Turing/其余架构的 MMA 指令细节差异由 Python 侧的 predicate（`_match_mma_sm70` / `_match_mma_sm75` / `_match_mma`）互斥地挑选不同实现类（[tilelang/cuda/op/gemm/__init__.py:34-36](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/cuda/op/gemm/__init__.py#L34-L36)）。这样把「硬件能力判定」与「发射实现」解耦。

**练习 2**：`ResolveGemmImpl` 为什么要求「恰好一个」实现匹配，多了少了都报错？

> 答案：少了说明该 target 没有任何 GEMM 实现，无法下译；多了说明注册有歧义（例如两个后端的 `match_target` 重叠），继续执行会产生非确定性结果。用 `ICHECK` 把这两种情况都变成显式编译错误（[src/op/gemm.cc:42-53](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/op/gemm.cc#L42-L53)）。

---

### 4.3 GemmWarpPolicy：warp 怎么切分输出 tile

#### 4.3.1 概念说明

GEMM 的输出 tile 大小是 `(M, N)`（如 128×128），而一个线程块里有 `num_warps` 个 warp。**warp 划分**回答的是：这 `num_warps` 个 warp 怎样瓜分这块输出——是排成 `m_warp × n_warp` 的网格，每个 warp 负责其中一个 `(M/m_warp) × (N/n_warp)` 子块。

`GemmWarpPolicy` 提供三种策略（[tilelang/tileop/base.py:5-12](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/tileop/base.py#L5-L12)）：

| 策略 | 含义 |
|------|------|
| `Square`（默认，=0） | 尽量让 warp 网格的宽高比贴合 M/N 的比例，追求「方」 |
| `FullRow`（=1） | 所有 warp 沿 M（行）方向排开 |
| `FullCol`（=2） | 所有 warp 沿 N（列）方向排开 |

#### 4.3.2 核心流程

`Square` 策略的算法（[tilelang/tileop/base.py:114-152](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/tileop/base.py#L114-L152)）：

```text
约束：每 warp 至少负责 M 方向 16 个元素、N 方向 8 个元素（CUDA 默认）
ideal_ratio = M / N
枚举 m in [1, min(max_m_warps, num_warps)]:
    n = num_warps // m
    if n 超界 or m*n != num_warps: 跳过
    balance = | (M/(m*16)) / (N/(n*8)) - ideal_ratio |
    记录 balance 最小的 (m, n)
```

直觉：它遍历所有「乘积等于 num_warps」的 `(m, n)` 组合，挑出使「每个 warp 实际负责的子块宽高比」最贴近整块宽高比的那一组，让负载尽量均衡。

> 这里有个关键差异：上面 `max_n_warps = N // 8` 里的 `8`（以及 `max_m_warps = M // 16` 里的 `16`）是**单 warp 的最小覆盖**。CUDA 非 Volta 用 `k_n_per_warp=8`，Volta 用 16；ROCm 用 16；**MACA 也固定用 16**（见下文）。

#### 4.3.3 源码精读

Python 侧的入口与 Square 枚举（[tilelang/tileop/base.py:65-158](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/tileop/base.py#L65-L158)）：

```python
def compute_warp_partition(self, M, N, num_warps):
    ...
    elif self.is_square():
        max_m_warps = M // 16
        max_n_warps = N // 8
        ideal_ratio = float(M) / N if N > 0 else 1.0
        for m in range(1, min(max_m_warps, num_warps) + 1):
            n = num_warps // m
            if n > max_n_warps or m * n != num_warps: continue
            balance = abs((M/(m*16)) / (N/(n*8)) - ideal_ratio)
            ...
```

但真正在 lowering 时被调用的是 **C++ 侧**的 `ComputeWarpPartition`，它按 target 取不同实现（[src/op/gemm.cc:172-176](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/op/gemm.cc#L172-L176)）：

```cpp
std::pair<int,int> GemmWarpPolicyNode::ComputeWarpPartition(
    int M, int N, int block_size, Target target, String gemm_inst) const {
  return ResolveGemmImpl(target).compute_warp_partition(*this, M, N, block_size, target, gemm_inst);
}
```

CUDA 的实现会按指令键分派：TCGEN05 固定 `(1, num_warps)`，WGMMA 走专门的 `ComputeWgmmaWarpPartition`（要求 `num_warps%4==0`，因为 warpgroup MMA 以 4 个 warp 为一组），普通 MMA 走 `ComputeDefaultWarpPartition`（[src/cuda/op/gemm.cc:289-303](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/cuda/op/gemm.cc#L289-L303)）：

```cpp
int num_warps = block_size / TargetCudaGetWarpSize(target);   // CUDA: /32
if (gemm_inst == kCudaTCGEN05) return {1, num_warps};
if (gemm_inst == kCudaWGMMA)   return ComputeWgmmaWarpPartition(...);
int k_n_per_warp = TargetIsVolta(target) ? 16 : 8;
return ComputeDefaultWarpPartition(policy, M, N, num_warps, k_n_per_warp);
```

**MACA 的差异点**（[src/maca/op/gemm.cc:146-152](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/maca/op/gemm.cc#L146-L152)）——这是 metax 分支的核心差异之一：

```cpp
int num_warps = block_size / TargetMacaGetWarpSize(target);   // MACA: /64
int k_n_per_warp = 16;
return ComputeDefaultWarpPartition(policy, M, N, num_warps, k_n_per_warp);
```

两个关键区别：

1. `TargetMacaGetWarpSize(target)` 返回 **64**（CUDA 是 32），所以同样的 `threads=128`，CUDA 得到 4 个 warp，MACA 只得到 2 个 warp。
2. `k_n_per_warp` 固定为 16（与 Volta 一致），不走 CUDA 的 Volta/非 Volta 分支。

这会直接传导到发射器：MACA 的 `GemmMMA._make_mma_emitter` 用 `m_warp, n_warp` 算出每个 warp 负责的 `warp_row_tiles = M // m_warp`、`warp_col_tiles = N // n_warp`（[tilelang/maca/op/gemm/gemm_mma.py:21-38](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/maca/op/gemm/gemm_mma.py#L21-L38)）。

#### 4.3.4 代码实践

**实践目标**：算出 CUDA 与 MACA 在同一份 GEMM 下的 warp 划分差异。

**操作步骤**：

1. 取 [example_gemm.py](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/examples/gemm/example_gemm.py) 的参数：`block_M=128, block_N=128`，`threads=128`，默认 `policy=Square`。
2. **CUDA（sm80，普通 MMA）**：`num_warps = 128/32 = 4`，`k_n_per_warp=8`。手算 `compute_warp_partition(128, 128, 4)`：候选 `(m,n)` 有 `(1,4),(2,2),(4,1)`，`ideal_ratio=1`，`balance` 最小的是 `(2,2)`。即每个 warp 负责 `64×64`。
3. **MACA**：`num_warps = 128/64 = 2`，`k_n_per_warp=16`。候选只有 `(1,2),(2,1)`，`ideal_ratio=1` → `(1,1)` 不满足 `m*n==2`，比较 `(1,2)` 与 `(2,1)`，`balance` 相等时取首个 `(1,2)`（待本地验证：实际代码取 `best_balance` 更小者，二者相等时保留先出现的 `(1,2)`）。
4. 把两组结果填进下表。

**需要观察的现象**：同样的 `threads=128`，CUDA 切成 4 个 warp 各管 64×64，MACA 切成 2 个 warp 各管 128×64（沿 N 平分）。

**预期结果**：

| 后端 | num_warps | (m_warp, n_warp) | 每 warp 子块 |
|------|-----------|------------------|--------------|
| CUDA sm80 | 4 | (2, 2) | 64×64 |
| MACA | 2 | (1, 2) | 128×64 |

> 待本地验证：若你想确认，可在 [gemm_mma.py:22](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/maca/op/gemm/gemm_mma.py#L22) 之后临时打印 `m_warp, n_warp`（这是源码修改建议，仅供观察，不提交）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 WGMMA 要求 `num_warps % 4 == 0`？

> 答案：Hopper 的 WGMMA 是 **warpgroup**（4 个 warp = 128 线程）粒度的指令，所以 warp 数必须是 4 的倍数（[src/cuda/op/gemm.cc:189](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/cuda/op/gemm.cc#L189)）。

**练习 2**：把 `policy` 从 `Square` 改成 `FullRow`，warp 划分会怎样变？

> 答案：`FullRow` 令 `m_warp=num_warps, n_warp=1`（若 M 不能被 `m_warp*16` 整除则尽量多给 M、剩余给 N），即所有 warp 沿行方向排开，每个 warp 负责窄而高的子块（[tilelang/tileop/base.py:84-97](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/tileop/base.py#L84-L97)）。

---

### 4.4 Lower / InferLayout：C++ 做壳，Python 做事

#### 4.4.1 概念说明

`GemmNode` 继承自 `TileOperatorNode`，必须实现两个核心虚方法（[src/op/gemm.h:161-165](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/op/gemm.h#L161-L165)）：

- **`InferLayout`**：在 Layout Inference Pass 里被调用。给定 target 与线程范围，推断 A/B/C 三个 buffer 的寄存器/共享内存布局（包括 fragment 如何绑定到线程、shared 是否做 swizzle）。
- **`Lower`**：在 `lower_tile_op` pass 里被调用。把这次 GEMM 下译成一段 TIR `PrimFunc`（通常是若干 `mma`/`wgmma`/`mfma` intrinsic 的循环）。

它们在 C++ 里只是**薄壳**：通过 `Function::GetGlobal("tl.gemm.infer_layout")` / `"tl.gemm.lower"` 反查到 Python 注册的全局函数，把活儿整体外包给 Python。这呼应了 u4-l1 提到的「Python 注册、C++ 按名调用」模式。

#### 4.4.2 核心流程

**InferLayout 流程**（[src/op/gemm.cc:223-261](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/op/gemm.cc#L223-L261)）：

```text
InferLayout(layout_args, level):
    if completed_: return {}             # 已推断过则跳过（幂等）
    调 Python tl.gemm.infer_layout(gemm, target, thread_bounds) → LayoutMap
    gemm_inst = GetGemmInstructionKey(...)
    reuse = ResolveGemmImpl(target).reuse_existing_shared_layout(gemm_inst)
    for (buf, layout) in LayoutMap:
        if reuse and buf 是 shared 且 已有布局: 跳过   # MMA 可复用既有 shared 布局
        if layout 是 Fragment: 绑定到线程范围
        else: 原样设置
    completed_ = true
```

关键点：`reuse_existing_shared_layout` 仅对**普通 MMA** 返回 true（CUDA 见 [src/cuda/op/gemm.cc:305-307](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/cuda/op/gemm.cc#L305-L307)，MACA 见 [src/maca/op/gemm.cc:154-156](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/maca/op/gemm.cc#L154-L156)）。WGMMA/TCGEN05 因为对 shared 布局有严格要求，**必须始终自己设布局**，不允许多个 gemm 复用同一 shared buffer 的不同布局。

**Lower 流程**（[src/op/gemm.cc:178-221](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/op/gemm.cc#L178-L221)）：

```text
Lower(lower_args, analyzer):
    f = Function::GetGlobal("tl.gemm.lower")
    prim_func = f(gemm, layout_map, target, thread_bounds, thread_var, mbar_phase)
    取 prim_func 的 global_symbol
    把 body 包成 SBlockRealize（带 kLexicalAllocScope=1 注解）
    返回该 block
```

Python 侧两个全局函数的注册（[tilelang/tileop/gemm/__init__.py:12-29](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/tileop/gemm/__init__.py#L12-L29)）：

```python
@tvm_ffi.register_global_func("tl.gemm.infer_layout")
def gemm_infer_layout(gemm, target, thread_bounds):
    return gemm.infer_layout(target, thread_bounds.extent)

@tvm_ffi.register_global_func("tl.gemm.lower")
def gemm_lower(gemm, layout_map, target, thread_bounds, thread_var, mbar_phase_expr):
    return gemm.lower(layout_map, target, thread_bounds, thread_var, mbar_phase_expr)
```

真正干活的是**实现类**。以 MACA 的 `GemmMMA` 为例，`infer_layout` 按 A/B 的 scope 分类（`GemmBase` 提供的 SS/SR/RS/RR 判定，见 [tilelang/tileop/gemm/gemm_base.py:53-71](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/tileop/gemm/gemm_base.py#L53-L71)），决定 shared 走 swizzle、fragment 走 MMA 布局（[tilelang/maca/op/gemm/gemm_mma.py:40-67](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/maca/op/gemm/gemm_mma.py#L40-L67)）：

```python
def infer_layout(self, target, thread_nums):
    mma_emitter = self._make_mma_emitter(target, thread_nums)
    if self.is_gemm_ss():      # A,B 都在 shared
        return {self.A: make_swizzled_layout(self.A),
                self.B: make_swizzled_layout(self.B),
                self.C: mma_emitter.make_mma_store_layout(self.C)}
    elif self.is_gemm_sr():    # A 在 shared, B 在 fragment
        ...
```

`lower` 则生成一段 `@T.prim_func` 内部宏，按 `block_K // micro_size_k` 循环发射 `mma` 指令（[tilelang/maca/op/gemm/gemm_mma.py:69-120](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/maca/op/gemm/gemm_mma.py#L69-L120)）：

```python
def lower(self, layout_map, target, thread_bounds, thread_var, mbar_phase_expr=None):
    mma_emitter = self._make_mma_emitter(target, thread_nums, thread_var=thread_var)
    ...
    if self.is_gemm_ss():
        @T.prim_func
        def _gemm_ssr():
            A_local = T.alloc_local((warp_rows * local_size_a), a_dtype)
            B_local = T.alloc_local((warp_cols * local_size_b), b_dtype)
            if clear_accum: T.clear(C_buf)
            for ki in T.serial(0, (block_K // micro_size_k)):
                # Load A/B into fragment, issue mma, accumulate into C
                ...
```

> 这段内部宏就是「一个 tile 上的矩阵乘循环」，它会被 4.2 选出的发射器（`TensorCoreIntrinEmitter`）翻译成具体的 `mfma`/`mma`/`wgmma` 调用。发射器内部细节（mfma 命名、布局变换）留待 u6-l1（intrinsics 总览）与 u7-l3（MACA mfma）展开。

#### 4.4.3 源码精读

把整条「C++ 壳 → Python 桥 → 实现类」串起来：

1. C++ `GemmNode::InferLayout`（[src/op/gemm.cc:223-261](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/op/gemm.cc#L223-L261)）→
2. Python 全局函数 `tl.gemm.infer_layout`（[__init__.py:12-15](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/tileop/gemm/__init__.py#L12-L15)）→
3. `Gemm.infer_layout`（[__init__.py:121-125](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/tileop/gemm/__init__.py#L121-L125)）：选指令→选类→委托 →
4. 实现类 `GemmMMA.infer_layout`（[gemm_mma.py:40-67](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/maca/op/gemm/gemm_mma.py#L40-L67)）。

`Lower` 同构：C++（[src/op/gemm.cc:178-221](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/op/gemm.cc#L178-L221)）→ `tl.gemm.lower`（[__init__.py:18-29](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/tileop/gemm/__init__.py#L18-L29)）→ `Gemm.lower`（[__init__.py:127-139](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/tileop/gemm/__init__.py#L127-L139)）→ `GemmMMA.lower`（[gemm_mma.py:69-120](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/maca/op/gemm/gemm_mma.py#L69-L120)）。

#### 4.4.4 代码实践

**实践目标**：跟踪一次 `T.gemm` 的完整下译调用链，确认「C++ 壳、Python 做事」。

**操作步骤**：

1. 在 [src/op/gemm.cc:223](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/op/gemm.cc#L223) 的 `InferLayout` 入口处，确认它第一步是 `Function::GetGlobal("tl.gemm.infer_layout")`，若拿不到则 `LOG(FATAL)`。
2. 在 [tilelang/tileop/gemm/__init__.py:121](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/tileop/gemm/__init__.py#L121) 的 `infer_layout` 方法里，确认它先 `_select_gemm_instruction` 再 `_get_implementation_class`，最后 `impl_class(self).infer_layout(...)`。
3. 在 [tilelang/maca/op/gemm/gemm_mma.py:40](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/maca/op/gemm/gemm_mma.py#L40) 的 `infer_layout` 里，确认它根据 `is_gemm_ss/sr/rs/rr` 返回不同的 `{buffer: layout}` 字典。
4. 画出调用链时序图（文字版即可）。

**需要观察的现象**：每一层都只做「找下一个干活的人」，真正的布局计算与 TIR 发射全部落在最末端的实现类。

**预期结果**：调用链为 `C++ InferLayout → Python tl.gemm.infer_layout → Gemm.infer_layout → resolve_gemm_impl → GemmMMA.infer_layout`，共 4 跳；`Lower` 同样 4 跳。

#### 4.4.5 小练习与答案

**练习 1**：`InferLayout` 里的 `completed_` 标志起什么作用？

> 答案：保证幂等——同一个 `GemmNode` 被多次访问时只推断一次，后续直接返回空 map（[src/op/gemm.cc:225-226](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/op/gemm.cc#L225-L226) 与 [src/op/gemm.cc:259](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/op/gemm.cc#L259)）。

**练习 2**：为什么 WGMMA/TCGEN05 不允许复用既有 shared 布局，而 MMA 允许？

> 答案：WGMMA/TCGEN05 的 descriptor 对 shared memory 的排布（swizzle 模式、leading dimension）有严格硬件要求，必须由该 gemm 自己设定；而普通 MMA 的 shared 访问较灵活，若同一 shared buffer 已被前一个算子排好布局，复用即可避免冲突。故 `ReuseExistingSharedLayout` 仅对 MMA 类指令返回 true（[src/cuda/op/gemm.cc:305-307](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/src/cuda/op/gemm.cc#L305-L307)）。

---

## 5. 综合实践

把本讲四个最小模块串成一个端到端的「分派追踪」任务。

**任务**：给定下面这份简化 GEMM（基于 [example_gemm.py](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/examples/gemm/example_gemm.py)），分别针对 `target="cuda"`（假设 sm90 Hopper）与 `target={"kind":"maca"}`，写出从 DSL 到实现类的**完整分派记录**。

```python
# 示例代码（基于 examples/gemm/example_gemm.py 改写）
@tilelang.jit
def matmul(A, B, block_M, block_N, block_K, dtype=T.float16, accum_dtype=T.float32):
    ...
    with T.Kernel(grid, grid, threads=128) as (bx, by):
        A_shared = T.alloc_shared((block_M, block_K), dtype)   # shared
        B_shared = T.alloc_shared((block_K, block_N), dtype)   # shared
        C_local  = T.alloc_fragment((block_M, block_N), accum_dtype)  # fragment
        T.clear(C_local)
        for k in T.Pipelined(T.ceildiv(K, block_K), num_stages=3):
            T.copy(A[...], A_shared); T.copy(B[...], B_shared)
            T.gemm(A_shared, B_shared, C_local)   # ← 本讲主角
        T.copy(C_local, C[...])
```

**要求产出**（一张表，逐项给出依据的源码行）：

| 维度 | CUDA (sm90) | MACA |
|------|-------------|------|
| `_gemm_impl` 发出的 op_key | `tl.tileop.gemm` | `tl.tileop.gemm` |
| `GemmNode` 的 `isWgmma_` / `isTcgen05_` | false / false | false / false |
| `ResolveGemmImpl` 命中的 C++ 实现 | `cuda.Gemm`（`MatchCudaGemmTarget`） | `maca.Gemm`（`MatchMacaGemmTarget`） |
| C++ `select_inst` 返回的指令键 | `cuda.wgmma`（经 `AllowWgmma` 判定） | `maca.mma`（恒定） |
| Python `resolve_gemm_impl` 选出的实现类 | `GemmWGMMA` | `GemmMMA`(maca) |
| `num_warps` | 128/32 = 4 | 128/64 = 2 |
| warp 划分 (m_warp, n_warp) | 见 `ComputeWgmmaWarpPartition`（m_warp=4 为一组） | 见 `ComputeDefaultWarpPartition`（k_n_per_warp=16） |
| `infer_layout` 对 A_shared 的处理 | WGMMA 强制自定 shared 布局 | `make_swizzled_layout` |
| `lower` 发射的指令族 | `wgmma.mma_async` | `mfma`/`mma`（经 `TensorCoreIntrinEmitter`） |

**验收标准**：

1. 每一格都能指向本讲引用的具体源码行（permalink）。
2. 能解释为什么 MACA 的 `num_warps` 是 CUDA 的一半（`warp_size=64`）。
3. 能说清楚「C++ 选键、Python 选类」这两级各自的输入与输出。

> 若有 Hopper / MetaX 设备，可用 `kernel.get_kernel_source()` 核对最后一行（生成的源码里应分别出现 `wgmma` 与 `mfma`/相关 intrinsic）；无设备则本任务为纯源码阅读，结论即上表。**待本地验证**指令族一列。

## 6. 本讲小结

- `T.gemm` / `T.wgmma_gemm` / `T.tcgen05_gemm` 三个 DSL 入口共用 `_gemm_impl`，差异仅在 op_key（`tl.tileop.gemm` 等）与 `is_wgmma`/`is_tcgen05` 两个 annotation；C++ 侧统一反序列化成 `GemmNode`。
- GEMM 分派是**两级**的：C++ `ResolveGemmImpl(target).select_inst(...)` 返回指令键（`cuda.wgmma`/`cuda.mma`/`cuda.tcgen05`/`maca.mma`/`rocm.mfma`/`cpu.scalar`），Python `resolve_gemm_impl(gemm_inst, target)` 再把键映射到实现类。
- 指令选择依赖一张「target × dtype × scope × 形状 × 对齐」的判定表：CUDA 按 TCGEN05→WGMMA→MMA 优先级，MACA 当前恒为 `maca.mma`（`CheckWgmma` 已预留但未启用）。
- `GemmWarpPolicy`（`Square`/`FullRow`/`FullCol`）决定 warp 如何切分输出 tile；MACA 因 `warp_size=64`、`k_n_per_warp=16`，与 CUDA（`warp_size=32`）在同样 `threads` 下得到不同的 warp 数与划分。
- `Lower` 与 `InferLayout` 在 C++ 里是薄壳，通过全局函数名 `tl.gemm.lower` / `tl.gemm.infer_layout` 把工作外包给 Python 实现类；`InferLayout` 还用 `reuse_existing_shared_layout` 仅让 MMA 复用既有 shared 布局。

## 7. 下一步学习建议

- **u4-l3（内存布局推断 Layout/Fragment）**：本讲里 `infer_layout` 返回的 `make_swizzled_layout` / `make_mma_store_layout` 究竟是什么，Fragment 如何绑定到线程范围，将在那里系统展开。
- **u6-l1（MMA intrinsics 总览）**：本讲末尾的 `TensorCoreIntrinEmitter` 是各类张量核发射器的统称，u6-l1 会把 WGMMA/TCGEN05/MMA 发射器与 `lower_intrin` pass 讲透。
- **u7-l3（MACA mfma intrinsics）**：若你关心 metax 分支的核心，u7-l3 会钻进 `tilelang/maca/intrinsics/macro/mma_macro_generator.py`，讲清 mfma 指令命名（如 `16x16x16fp16`）与 `mma_layout` 布局变换。
- **u5-l2（transform pass 体系）**：本讲的 `Lower`/`InferLayout` 是被 `lower_tile_op` pass 调用的，u5-l2 会把它们放回整条 lowering 主流水线中。
