# 默认线性 epilogue：D = α·D + β·C + rowvec + colvec

## 1. 本讲目标

GEMM 的核心计算（矩阵乘 \(A @ B\)）做完之后，几乎从不直接把结果写回显存。真实场景里你往往还想：乘一个缩放系数 α、加一个偏置（bias）、加一个残差矩阵 C、对输出做量化……这些「乘加 + 向量广播」的后处理统称为 **epilogue（尾声）」。

QuACK 提供了一套可组合的 epilogue 系统（见前置讲义 u6-l1、u6-l2），其中**最常用、最基础**的一种就是「默认线性 epilogue」。本讲只讲清楚这一种，读完你应该能够：

- 读懂 `apply_linear_epilogue` 的逐项数学公式与实现细节；
- 理解 `GemmDefaultEpiMixin` 如何通过「mixin + 各 SM 内核类」的组合方式，把同一套线性数学接到 Hopper / Blackwell / GeForce 上；
- 逐字段理解 `EpilogueArguments` 这个 `NamedTuple`，知道每个字段控制什么、哪些是编译期常量；
- 解释 α/β 为什么必须用 `Scalar` 这个 EpiOp 来表示，以及线性 epilogue 如何在 split-K 归约中被复用以保证 **逐位一致（bitwise-identical）」。

## 2. 前置知识

本讲默认你已经理解以下概念（均在更早的讲义中建立）：

- **Epilogue 与 EpiOp 生命周期**（u6-l1）：epilogue 把每种张量资源（标量、广播向量、输出块）抽成一个独立的 `EpiOp`，由 `ComposableEpiMixin` 在固定时刻依次调度；声明是全集（`_epi_ops`），执行是子集（运行期过滤掉 `None` 的）。
- **EpiOp 词汇表**（u6-l2）：`Scalar`（标量）、`RowVecLoad`/`ColVecLoad`（广播向量）、`TileStore`（辅助输出）、`DStore`（主输出 D，主机侧管线由内核拥有，**不在** `_epi_ops`）。
- **epilogue 驱动 store 循环**（u5-l1）：基类 `GemmBase.epilogue` 固定编排 `store_convert`（dtype 转换）→ `store_r2s`（寄存器→共享内存）→ TMA 存走的流程，数学阶段则调用子类的 `epi_visit_subtile`。
- **公共 GEMM API**（u4-l3）：`gemm` 的语义是 \(D = \alpha(A@B) + \text{bias}\)，bias 即本讲的 rowvec/colvec。
- **编译期常量与参数容器**（u3-l3）：`cutlass.Constexpr[T]` 字段在 trace 期烘焙进 cubin、不产生运行期参数；`mlir_namedtuple` 把 `NamedTuple` 变成可跨 FFI 传递的 JIT 参数容器。

一个一句话直觉：**线性 epilogue 就是「累加器片段上的一串就地乘加」**，所有项都对同一个 `tRS_rD` 寄存器片段做就地修改，最后才由独立的 store 路径转 dtype 并写回显存。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [quack/gemm_default_epi.py](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_default_epi.py) | 本讲主角：`apply_linear_epilogue` 函数、`GemmDefaultEpiMixin` 基类、各 SM 的 `GemmDefaultSmXX` 组合类、`EpilogueArguments` 定义，全部集中在这个不到 170 行的文件里。 |
| [quack/epilogue/ops.py](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/ops.py) | `Scalar`、`RowVecLoad`、`ColVecLoad` 等 EpiOp 的实现，是 `_epi_ops` 里各项的来源。 |
| [quack/epilogue/mixin.py](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/mixin.py) | `ComposableEpiMixin`：自动生成 `EpilogueParams`、过滤 `_epi_ops`、驱动各生命周期钩子。 |
| [quack/gemm_base.py](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_base.py) | epilogue 驱动循环，把数学（`epi_visit_subtile`）与存储（`store_convert`/`store_r2s`）串起来。 |
| [quack/split_k_reduce.py](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/split_k_reduce.py) | split-K 的 SEPARATE 模式归约内核，**复用** `apply_linear_epilogue`。 |
| [quack/gemm_symmetric.py](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_symmetric.py) | `GemmSymmetricMixin`，展示如何「在默认线性 epilogue 之上扩展」的范例。 |

## 4. 核心概念与源码讲解

### 4.1 `apply_linear_epilogue` 的逐项数学

#### 4.1.1 概念说明

「线性 epilogue」指的是：对 GEMM 累加器得到的每个输出元素 \(D_{mn}\)，施加一组**线性运算**（乘法与加法），再叠加两个**广播向量**。数学上：

\[
D_{mn} \;\leftarrow\; \alpha \cdot D_{mn} \;+\; \beta \cdot C_{mn} \;+\; r_{n} \;+\; c_{m}
\]

其中：

- \(D_{mn}\)：GEMM 累加器里位置 \((m,n)\) 的元素（fp32，存在寄存器片段 `tRS_rD` 中，**就地修改**）。
- \(\alpha\)：对整个 D 的标量缩放（可缺省，缺省即「不缩放」）。
- \(C_{mn}\)：可选的「残差 / source」矩阵，与 D 同形状。
- \(\beta\)：对 C 的标量缩放（缺省时等价于 \(\beta=1.0\)，即「直接加 C」）。
- \(r_n\)：**行向量（row vector）**，长度为 N，沿 M 维广播——这就是常见的 bias（每列加同一个偏置）。
- \(c_m\)：**列向量（col vector）**，长度为 M，沿 N 维广播。

> 注意向量命名：QuACK 把「沿 N 排列、沿 M 广播」的向量叫 **row vector**（因为它对应输出的一「行」方向上的偏置，即 `RowVecLoad`，stride `(0,1)`）；「沿 M 排列、沿 N 广播」的叫 **col vector**。这是 u6-l2 建立的约定。

这套数学之所以重要，是因为它是**所有 GEMM 调用最通用的后处理**：神经网络里的 `Linear = α(A@B) + bias`、残差连接的 `+ C`、以及量化的归一化常数，都能套进这一个公式。

#### 4.1.2 核心流程

`apply_linear_epilogue` 是一个 `@cute.jit` 设备函数，对**一个 epilogue 子块（subtile）」** 的寄存器片段做就地修改。伪代码如下：

```
function apply_linear_epilogue(tRS_rD, tRS_rC, alpha, beta, tDrRowVec, tDrColVec):
    # 1) α 缩放（若 alpha 提供）
    if alpha is not None:
        a = load_scalar_or_pointer(alpha)   # 立即数 or 解引用设备指针
        tRS_rD = tRS_rD.load() * a          # 原地写回

    # 2) β·C 项（若 C 提供）
    if tRS_rC is not None:
        if beta is None and C 是 16-bit and D 是 f32:   # 特殊快路径
            for i in unroll(size(tRS_rD)):
                tRS_rD[i] = tRS_rD[i] + C[i].to(f32)    # 加宽融进加法
        elif beta is None:                              # β 缺省 = 直接加 C
            tRS_rD += C.load().to(f32)
        else:                                           # 有 β
            b = load_scalar_or_pointer(beta)
            tRS_rD += b * C.load().to(f32)

    # 3) 行向量 / 列向量广播加
    if tDrRowVec is not None:
        for i in unroll(size(tDrRowVec)):
            tRS_rD[i] += tDrRowVec[i]
    if tDrColVec is not None:
        for i in unroll(size(tDrColVec)):
            tRS_rD[i] += tDrColVec[i]
```

几个关键点：

1. **就地修改**：所有项都写回同一个 `tRS_rD`，不分配新片段。顺序固定为 α → β·C → rowvec → colvec。
2. **编译期分支**：每个 `if const_expr(...)` 都是**编译期判断**。当某个项为 `None`（缺省），整个分支在 trace 期被折叠掉，**不会出现在最终 cubin 里**。这是「声明是全集、执行是子集」的体现。
3. **特殊快路径**：当 β 缺省、C 是 16 位、D 是 fp32 时，走逐元素标量循环而不是张量化表达式，目的是让「16 位 → 32 位的加宽」**融入加法指令**（详见 4.1.3）。
4. **标量与指针的统一**：α/β 既可能是立即数（host 常量），也可能是设备指针（per-call 变化），由 `load_scalar_or_pointer` 统一处理。

#### 4.1.3 源码精读

整个函数定义在 [quack/gemm_default_epi.py:21-67](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_default_epi.py#L21-L67)。先看签名与文档：

```python
@cute.jit
def apply_linear_epilogue(
    tRS_rD: cute.Tensor,
    tRS_rC: Optional[cute.Tensor],
    alpha,
    beta,
    tDrRowVec: Optional[cute.Tensor],
    tDrColVec: Optional[cute.Tensor],
) -> None:
    """The default (linear) epilogue math: D = alpha * D + beta * C + rowvec + colvec.

    tRS_rD is mutated in place (acc dtype). ... Shared by GemmDefaultEpiMixin and the
    split-K staged reduction kernel (quack/split_k_reduce.py) so the two apply
    bitwise-identical math.
    """
```

文档里那句「Shared by ... so the two apply bitwise-identical math」是本讲后面 split-K 复用的关键伏笔。

**α 分支**——[第 38-41 行](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_default_epi.py#L38-L41)：

```python
if const_expr(alpha is not None):
    a = utils.load_scalar_or_pointer(alpha)
    rD = tRS_rD.load() * a
    tRS_rD.store(rD)
```

`load_scalar_or_pointer` 在 [quack/utils.py:20-24](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/utils.py#L20-L24)：若 `alpha` 是 `cute.Pointer`（设备指针），就包成单元素张量并取值；否则（立即数）直接返回。

```python
def load_scalar_or_pointer(x, dtype=Float32):
    if const_expr(isinstance(x, cute.Pointer)):
        return dtype(cute.make_tensor(x, cute.make_layout(1))[0])
    else:
        return x
```

**β·C 分支**——[第 43-61 行](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_default_epi.py#L43-L61)。这里有一段值得细读的特殊快路径：

```python
if const_expr(tRS_rC is not None):
    if const_expr(
        beta is None and tRS_rC.element_type.width == 16 and tRS_rD.element_type == Float32
    ):
        # Plain 16-bit C add: scalar adds so the widen folds into the add
        # (PTX add.rn.f32.{f16,bf16} -> FHADD on SM100/SM120 — exact, so
        # bitwise-identical to cvt+add; ...)
        for i in cutlass.range(cute.size(tRS_rD), unroll_full=True):
            tRS_rD[i] = tRS_rD[i] + tRS_rC[i].to(tRS_rD.element_type)
    else:
        rD = tRS_rD.load()
        if const_expr(beta is None):
            rD += tRS_rC.load().to(tRS_rD.element_type)   # β 缺省 = 直接加 C
        else:
            b = utils.load_scalar_or_pointer(beta)
            rD += b * tRS_rC.load().to(tRS_rD.element_type)
        tRS_rD.store(rD)
```

注释里的关键含义：当 C 是 16 位、D 是 32 位时，「先把 C 加宽成 32 位、再加」在 Blackwell 上可以被编译成单条融合指令 `FHADD`（加宽融进加法），其结果**与「先 cvt 后 add」逐位相同**，且在 Blackwell 之前的架构上也退化成同样的 `cvt+FADD`。这就是为什么注释反复强调 **bitwise-identical**——同一份代码在不同架构、不同表达式写法下产生完全一样的二进制位。

**行/列向量广播加**——[第 62-67 行](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_default_epi.py#L62-L67)：

```python
if const_expr(tDrRowVec is not None):
    for i in cutlass.range(cute.size(tDrRowVec), unroll_full=True):
        tRS_rD[i] += tDrRowVec[i]
if const_expr(tDrColVec is not None):
    for i in cutlass.range(cute.size(tDrColVec), unroll_full=True):
        tRS_rD[i] += tDrColVec[i]
```

这里用 `cutlass.range(..., unroll_full=True)` 完全展开循环。向量片段已经由 `RowVecLoad`/`ColVecLoad` 用 zero-stride 布局广播到与 `tRS_rD` 元素对齐（见 u3-l2、u6-l2），所以逐元素加即可完成「每行加同一个 rowvec / 每列加同一个 colvec」的广播语义。

> 小结这一节的直觉：`apply_linear_epilogue` 是一段**纯数学、纯就地、纯编译期分支**的设备函数，它只关心寄存器片段上的乘加，不关心数据从哪来、写到哪去——后者全部交给 store 路径。

#### 4.1.4 代码实践

这是一个**源码阅读型实践**（不依赖 GPU），目标是把「α 缺省即不缩放」和「C 加宽融进加法」两件事在代码里坐实。

1. **实践目标**：确认缺省项会被编译期折叠，并理解 16 位 C 加法的特殊路径。
2. **操作步骤**：
   - 打开 [quack/gemm_default_epi.py:38-67](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_default_epi.py#L38-L67)。
   - 假设一次调用只传了 `alpha` 和 `tDrRowVec`，其余三项（`tRS_rC`、`beta`、`tDrColVec`）都是 `None`。
   - 用纸笔模拟 trace：哪些 `if const_expr(...)` 为真、哪些为假？最终 cubin 里只会留下哪两段代码？
3. **需要观察的现象**：
   - `beta is None` 仍可能为真，但由于外层 `if const_expr(tRS_rC is not None)` 为假，整段 C 处理（含 β 分支）都被折叠掉。
   - colvec 那段循环整体消失。
4. **预期结果**：最终 cubin 等价于 `tRS_rD = tRS_rD * a; for i: tRS_rD[i] += tDrRowVec[i]`，没有任何 C/beta/colvec 的指令。
5. 再读 [第 44-52 行](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_default_epi.py#L44-L52) 的注释，用自己的话回答：为什么作者要为「16 位 C + fp32 D」单独写一个标量循环，而不是统一走下面的张量化表达式？

#### 4.1.5 小练习与答案

**练习 1**：公式里 β 缺省时等价于 β = 1.0，但代码里却没有出现「乘以 1.0」，而是直接 `rD += C`。这样做对数值结果有什么影响？

> **答案**：直接 `rD += C` 避免了一次「乘 1.0」的浮点运算。虽然 1.0 作为乘数在 IEEE-754 下是精确的（不会改变结果），但省掉一次 FMUL 不仅更快，也消除了任何潜在的指令调度差异，有助于保证逐位一致。

**练习 2**：如果把 `tDrRowVec` 的循环改成普通 `cutlass.range`（不带 `unroll_full=True`），会对生成的代码有什么影响？

> **答案**：不带 `unroll_full` 时，循环会被翻译成一条运行期 IR 循环（带分支与计数器），而不是全部展开成顺序指令。对于这种定长（编译期已知 size）的小片段，完全展开能省掉循环开销、便于指令调度与寄存器分配。注意：因为循环体内没有提前 `break`，改写不会改变正确性，只影响性能。

---

### 4.2 `GemmDefaultEpiMixin` 与各 SM 类的组合

#### 4.2.1 概念说明

`apply_linear_epilogue` 只是「数学函数」。要让它真正跑起来，还需要：

- 一组 EpiOp 来**准备参数**（把 α/β 的模式、rowvec/colvec 的 TMA 描述符、量化 codec 等烘焙成 `EpilogueParams`）；
- 一个 `epi_visit_subtile` 方法，在驱动循环的「数学阶段」调用 `apply_linear_epilogue`；
- 把这一切接到**每一种架构的 GEMM 内核**上（SM80/SM90/SM100/SM120）。

`GemmDefaultEpiMixin` 就是完成这三件事的「默认 epilogue 装配器」。它是 u6-l1 讲的 `ComposableEpiMixin` 的子类，也是 QuACK 的**手写 epilogue 逃生出口**：当你想表达的东西超出函数式 `@gemm_epilogue` 前端（u6-l4）的契约时，就写一个 mixin 子类。默认线性 epilogue 是最基础的一个，其它手写 mixin（如对称 GEMM）都在它之上扩展。

#### 4.2.2 核心流程

装配流程可以这样理解：

```
ComposableEpiMixin           ← 自动生成 EpilogueParams、过滤 _epi_ops、驱动生命周期
   ↑
GemmDefaultEpiMixin          ← 声明 _epi_ops（默认线性 + 量化输出）、
   ↑                           定义 EpilogueArguments、重写 epi_visit_subtile
GemmDefaultSm90/100/120/80   ← (GemmDefaultEpiMixin, GemmSmXX) 多继承，pass
```

数据在**主机侧**与**设备侧**的分工：

- **主机侧**（在 `epi_to_underlying_arguments` 里）：把用户传入的 `EpilogueArguments` 经 `_epi_ops_to_params_dict` 过滤 + 转换，产出 `EpilogueParams`（含 TMA atom、smem 布局等设备侧需要的描述符）。
- **设备侧**（在 `epi_visit_subtile` 里）：每个子块调用一次 `apply_linear_epilogue`，读 `params.alpha`/`params.beta` 与 `epi_loop_tensors.get(...)` 里的向量片段。

#### 4.2.3 源码精读

**类声明与 `_epi_ops` 全集**——[quack/gemm_default_epi.py:70-83](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_default_epi.py#L70-L83)：

```python
class GemmDefaultEpiMixin(ComposableEpiMixin):
    _epi_ops = (
        Scalar("alpha"),
        Scalar("beta"),
        Scalar("sr_seed", dtype=Int32),
        RowVecLoad("mRowVecBroadcast"),
        ColVecLoad("mColVecBroadcast"),
        # D quantize codecs (at most one active): the driver's store loop runs
        # the active codec on the final D fragment right before DStore's convert
        BlockScaleFactorStore("mSFD"),
        BlockScaleFactorStore("mSFDCol", direction="col"),
    )
```

注意三点：

1. **没有 `DStore`**：主输出 D 的主机侧管线（TMA atom、staged smem 布局、split-K 工作区）由内核拥有，不在 `_epi_ops` 里——这是 u6-l2 讲过的关键区分。`DStore` 只在设备侧作为无状态对象参与 `store_convert`/`store_r2s`（见 [quack/epilogue/ops.py:1111-1124](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/ops.py#L1111-L1124)）。
2. **`mSFD`/`mSFDCol` 是量化 codec**：当输出要被量化成 MXFP8/NVFP4 等，这两个 `BlockScaleFactorStore`（来自 [quack/epilogue/quantize_out.py](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/quantize_out.py)）在驱动 store 循环里、于 `store_convert` 之前对最终 D 片段做量化并写出 scale factor。它的细节属于 u6-l5 的范围，本讲只需知道「它是 D 的量化输出 codec」。
3. **`sr_seed`**：随机舍入（stochastic rounding）的种子，配合 `rounding_mode` 使用。

**额外的 split-K 参数字段**——[第 90-93 行](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_default_epi.py#L90-L93)：

```python
_extra_param_fields = (
    ("split_k_semaphore", Optional[cute.Tensor], None),
    ("split_k_workspace", Optional[cute.Tensor], None),
)
```

这两个字段不是某个 EpiOp 的参数，而是 split-K 合并所需的「每块完成标志」与「原始 fp32 partial 工作区」。`_extra_param_fields` 会被 `ComposableEpiMixin.__init_subclass__` 拼进自动生成的 `EpilogueParams`（见 [quack/epilogue/mixin.py:67-78](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/mixin.py#L67-L78)）。

**主机侧参数转换**——[第 117-125 行](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_default_epi.py#L117-L125)：

```python
def epi_to_underlying_arguments(self, args, *, loc=None, ip=None):
    self.rounding_mode = args.rounding_mode
    d = self._epi_ops_to_params_dict(args)
    for key in ("mRowVecBroadcast", "mColVecBroadcast"):
        if key in self.concat_layout and key in d:
            d[key] = layout_utils.concat_to_interleave(d[key], 1)
    d["split_k_semaphore"] = getattr(args, "split_k_semaphore", None)
    d["split_k_workspace"] = getattr(args, "split_k_workspace", None)
    return self.EpilogueParams(**d)
```

`_epi_ops_to_params_dict`（[mixin.py:92-103](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/mixin.py#L92-L103)）做两件事：先 `_filter_epi_ops` 把 `None` 的 op 从实例级 `_epi_ops` 里剔除，再对每个存活 op 调 `to_params`。`concat_layout` 那段是 gated MLP 的拼接权重免拷贝优化（见 u4-l3），非 gated 场景不触发。

**设备侧数学入口**——[第 127-152 行](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_default_epi.py#L127-L152)：

```python
@cute.jit
def epi_visit_subtile(self, params, epi_loop_tensors, tRS_rD, tRS_rC=None):
    # Use .get(): inactive ops are filtered out of epi_loop_tensors.
    # Under split-K this runs exactly once per output tile, on the finalizing
    # entity, with the fully reduced accumulator in tRS_rD — no per-split gating.
    apply_linear_epilogue(
        tRS_rD,
        tRS_rC,
        params.alpha if const_expr(hasattr(params, "alpha")) else None,
        params.beta if const_expr(hasattr(params, "beta")) else None,
        epi_loop_tensors.get("mRowVecBroadcast"),
        epi_loop_tensors.get("mColVecBroadcast"),
    )
    return ()
```

两个细节：

- **`.get()` 而不是 `[...]`**：因为 inactive op 已被过滤出 `epi_loop_tensors`，用 `.get(name)` 在缺省时返回 `None`，正好喂给 `apply_linear_epilogue` 的 `const_expr(... is not None)` 判断。
- **返回 `()`**：默认线性 epilogue 没有辅助输出（aux output），所以返回空元组。对比 `GemmBase` 的基类默认实现 [quack/gemm_base.py:892-899](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_base.py#L892-L899)（也返回 `()`），`GemmDefaultEpiMixin` 在此之上注入了线性数学。注释还点出：split-K 下它**只在最终合并的那一方运行一次**，对已经完全归约好的累加器施加 epilogue，没有「每个 split 都跑」的门控。

**与各 SM 内核的组合**——[第 155-168 行](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_default_epi.py#L155-L168)：

```python
class GemmDefaultSm80(GemmDefaultEpiMixin, GemmSm80):
    pass

class GemmDefaultSm90(GemmDefaultEpiMixin, GemmSm90):
    pass

class GemmDefaultSm100(GemmDefaultEpiMixin, GemmSm100):
    pass

class GemmDefaultSm120(GemmDefaultEpiMixin, GemmSm120):
    pass
```

这是经典的 **mixin 多继承**：`GemmDefaultEpiMixin` 提供 epilogue 行为，`GemmSmXX` 提供该架构的 mainloop（MMA 指令、TMA 加载、累加器布局）。MRO（方法解析顺序）让 `epi_visit_subtile` 等方法优先取 mixin 的版本，而 mainloop 等方法取 SM 子类的版本。

这四个组合类才是真正被调度使用的内核——见 [quack/gemm.py:94-98](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm.py#L94-L98) 的分发表：

```python
{
    8:  GemmDefaultSm80,
    9:  GemmDefaultSm90,
    10: GemmDefaultSm100,
    11: GemmDefaultSm100,   # SM110 也走 SM100 路径
    12: GemmDefaultSm120,
}
```

主机侧构造 `EpilogueArguments` 的现场在 [quack/gemm.py:632-649](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm.py#L632-L649)，把 α/β、rowvec/colvec bias、split-K 缓冲、SFD 全部填进去。

**扩展范例：对称 GEMM**——[quack/gemm_symmetric.py:54-80](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_symmetric.py#L54-L80) 展示了「在默认线性 epilogue 之上加东西」的标准做法：

```python
class GemmSymmetricMixin(GemmDefaultEpiMixin):
    _epi_ops = GemmDefaultEpiMixin._epi_ops + (
        TileStore("mAuxOut", store_pred_fn=_symmetric_offdiag_pred),
    )
    ...
    @cute.jit
    def epi_visit_subtile(self, params, epi_loop_tensors, tRS_rD, tRS_rC=None):
        GemmDefaultEpiMixin.epi_visit_subtile(self, params, epi_loop_tensors, tRS_rD, tRS_rC)
        # The mirrored output IS the (linear-epilogue) D values.
        return (tRS_rD,)
```

它在 `_epi_ops` 末尾追加一个 `TileStore`（辅助输出），并在 `epi_visit_subtile` 里**先调父类的线性 epilogue**，再把已经过线性处理的 `tRS_rD` 作为辅助输出返回。这正是「手写 mixin 是逃生出口」的活样本。

#### 4.2.4 代码实践

1. **实践目标**：验证「mixin 组合不改 mainloop，只插桩 epilogue」的边界。
2. **操作步骤**：
   - 在 [quack/gemm_default_epi.py:155-168](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_default_epi.py#L155-L168) 确认四个组合类的函数体都只有 `pass`。
   - 用搜索（`Grep`）在仓库里查找 `GemmDefaultSm90`、`GemmDefaultSm100`、`GemmDefaultSm120` 的引用，确认它们只出现在 [gemm.py:94-98](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm.py#L94-L98) 的分发表与 [blockscaled/utils.py:749-752](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/blockscaled/utils.py#L749-L752)。
   - 对比 [quack/gemm_symmetric.py:76-80](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_symmetric.py#L76-L80) 中 `GemmSymmetricMixin.epi_visit_subtile` 如何显式调用父类。
3. **需要观察的现象**：组合类本身没有任何 mainloop 代码；epilogue 行为完全来自 mixin，MMA/加载行为完全来自 SM 子类。
4. **预期结果**：你能向别人解释「要给某个架构加一种新 epilogue，只需写一个继承 `GemmDefaultEpiMixin`（或 `ComposableEpiMixin`）的 mixin，再 `class XxxSm100(XxxMixin, GemmSm100): pass`」，无需改动任何 SM 内核源码。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `_epi_ops` 里没有 `DStore`，却又能把 D 存回显存？

> **答案**：D 的主机侧管线（TMA atom、staged smem 布局、split-K 工作区、`add_to_output`）由内核拥有，因此不在 `_epi_ops`。驱动循环 [quack/gemm_base.py:305-308](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_base.py#L305-L308) 直接用内核构建好的 `tiled_copy_r2s`/`tRS_sD`/`copy_D` 组装 D 的 store context，并放入一个无状态的 `DStore()` 对象负责设备侧的 convert/r2s。`_epi_ops` 里的 `mSFD`/`mSFDCol` 只是 D 的量化 codec，在 convert 之前运行。

**练习 2**：`GemmDefaultSm100` 同时继承 `GemmDefaultEpiMixin` 和 `GemmSm100`。如果两者都定义了某个同名方法，Python 会用哪一个？

> **答案**：用 `GemmDefaultEpiMixin` 的版本，因为多继承 `class C(A, B)` 的 MRO 先解析 A。这正是 mixin 模式的设计意图：mixin 排在前，用来覆盖或插桩宿主类（`GemmSm100`）的 epilogue 相关钩子（如 `epi_visit_subtile`），而 mainloop 等只存在于 SM 子类的方法不受影响。

---

### 4.3 `EpilogueArguments` 字段

#### 4.3.1 概念说明

`EpilogueArguments` 是**用户面对的接口**：主机侧调用方把 α/β、bias 向量、量化输出、split-K 缓冲等全部塞进这个 `NamedTuple`，内核据此决定编译哪种 epilogue、运行时传哪些张量。理解它的每个字段，就理解了「默认线性 epilogue 能配置什么」。

它是用 `@mlir_namedtuple` 装饰的 `NamedTuple`（见 u3-l3）。这种容器按字段的类型注解把字段分成两类：

- **静态（编译期）字段**：注解为 `cutlass.Constexpr[...]` 的字段在 trace 期被烘焙进 cubin，**不会产生运行期参数**，调用时传 `None`。
- **动态（运行期）字段**：普通注解的字段跨 FFI 在运行期传递。

这一点至关重要：`add_to_output` 和 `rounding_mode` 是编译期开关，它们的不同取值会特化出**结构不同的 cubin**。

#### 4.3.2 核心流程

字段一览（按源码顺序）：

| 字段 | 类型 | 默认 | 作用 |
| --- | --- | --- | --- |
| `alpha` | `Optional[Float32 \| cute.Tensor]` | None | D 的标量缩放（立即数或设备指针） |
| `beta` | `Optional[Float32 \| cute.Tensor]` | None | C 的标量缩放 |
| `mRowVecBroadcast` | `Optional[cute.Tensor]` | None | row 向量（长度 N，沿 M 广播）= bias |
| `mColVecBroadcast` | `Optional[cute.Tensor]` | None | col 向量（长度 M，沿 N 广播） |
| `add_to_output` | `Constexpr[bool]` | False | **编译期**：是否在 epilogue 之后把 D 原值再加一次 |
| `rounding_mode` | `Constexpr[int]` | RN | **编译期**：舍入模式（RN / RS 随机舍入） |
| `sr_seed` | `Optional[Int32 \| cute.Tensor]` | None | 随机舍入种子 |
| `split_k_semaphore` | `Optional[cute.Tensor]` | None | split-K 每块完成标志 |
| `split_k_workspace` | `Optional[cute.Tensor]` | None | split-K 原始 fp32 partial 工作区 |
| `mSFD` | `Optional[cute.Tensor]` | None | 沿 N 的 SF 向量（量化输出，row 方向） |
| `sfd_norm_const` | `Optional[Float32 \| cute.Tensor]` | None | 折进 SF 的 fp32 归一化常数 |
| `mSFDCol` | `Optional[cute.Tensor]` | None | 沿 M 的 SF 向量（col 方向，给反向消费者） |

注意 `mSFD` 与 `mSFDCol` **至多激活一个**——注释 [第 106-113 行](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_default_epi.py#L106-L113) 说明：前者按 `(L, rm, rk, 32, 4, 4)` 在 N 上分块，后者按 `(L, rn, rm_k, 32, 4, 4)` 在 (N,M) 上分块，服务于沿此输出 M 维做收缩的反向算子。

#### 4.3.3 源码精读

完整定义在 [quack/gemm_default_epi.py:95-113](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_default_epi.py#L95-L113)：

```python
@mlir_namedtuple
class EpilogueArguments(NamedTuple):
    alpha: Optional[Float32 | cute.Tensor] = None
    beta: Optional[Float32 | cute.Tensor] = None
    mRowVecBroadcast: Optional[cute.Tensor] = None
    mColVecBroadcast: Optional[cute.Tensor] = None
    add_to_output: cutlass.Constexpr[bool] = False
    rounding_mode: cutlass.Constexpr[int] = RoundingMode.RN
    sr_seed: Optional[Int32 | cute.Tensor] = None
    split_k_semaphore: Optional[cute.Tensor] = None
    split_k_workspace: Optional[cute.Tensor] = None
    mSFD: Optional[cute.Tensor] = None
    sfd_norm_const: Optional[Float32 | cute.Tensor] = None
    mSFDCol: Optional[cute.Tensor] = None
```

逐组理解：

**α / β / 两个向量**（[第 97-100 行](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_default_epi.py#L97-L100)）——这四项直接喂给 `apply_linear_epilogue`。它们都缺省为 `None`，缺省时对应的数学项被编译期剔除（见 4.1）。`Float32 | cute.Tensor` 表示既可传 Python 标量（立即数），也可传单元素 CUDA 张量（设备指针）。

**两个编译期开关**（[第 101-102 行](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_default_epi.py#L101-L102)）：

```python
add_to_output: cutlass.Constexpr[bool] = False
rounding_mode: cutlass.Constexpr[int] = RoundingMode.RN
```

- `add_to_output=True` 时，epilogue 项算完之后会把 D 的**原值**再无缩放地加一次——用于原地累加（如梯度累加）。它的实际消费点在 split-K 归约内核 [quack/split_k_reduce.py:227-228](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/split_k_reduce.py#L227-L228)。
- `rounding_mode` 决定 `store_convert` 时 fp32→存储 dtype 用确定性舍入（RN）还是随机舍入（RS）；后者会消费 `sr_seed`。因为它们是 `Constexpr`，不同取值会编译出不同的 cubin。

**split-K 缓冲**（[第 104-105 行](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_default_epi.py#L104-L105)）：`split_k_semaphore` 是每块完成标志 `(ntile_m, ntile_n, L)` 的 Int32 张量，`split_k_workspace` 是原始 fp32 partial 工作区。它们由 [quack/gemm.py:619-627](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm.py#L619-L627) 的 `_split_k_buffers` 在主机侧分配。

**量化输出**（[第 111-113 行](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_default_epi.py#L111-L113)）：`mSFD`/`mSFDCol` 提供量化的 scale factor 输出张量，`sfd_norm_const` 是可选的归一化常数。这三个字段对应 `_epi_ops` 里的两个 `BlockScaleFactorStore`，细节在 u6-l5、u7-l2 展开。

最后看一眼主机侧如何填充它——[quack/gemm.py:632-649](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm.py#L632-L649)：

```python
epi_args = GemmDefaultEpiMixin.EpilogueArguments(
    alpha=scalar_arg(alpha, plan.alpha_mode),
    beta=scalar_arg(beta, plan.beta_mode),
    mRowVecBroadcast=rowvec_bias,
    mColVecBroadcast=colvec_bias,
    add_to_output=None,          # Constexpr 字段，调用时传 None
    rounding_mode=None,          # Constexpr 字段，调用时传 None
    sr_seed=scalar_arg(sr_seed, plan.sr_seed_mode, dtype=Int32),
    split_k_semaphore=(... ),
    split_k_workspace=(... ),
    mSFD=SFD,
    sfd_norm_const=scalar_arg(sfd_norm_const, plan.sfd_norm_const_mode),
    mSFDCol=SFDCol,
)
```

注意 `add_to_output=None` 和 `rounding_mode=None`：因为它们是 `Constexpr` 字段，**值在 trace 期已被烘焙**，调用期传 `None` 占位即可（这是 u3-l3 讲过的 TVM-FFI 补丁行为）。`scalar_arg` 根据每个标量的「mode」（缺省 / 立即数 / 指针）转换成对应的运行期实参——这正是下一节「为什么用 Scalar op」要展开的。

#### 4.3.4 代码实践

1. **实践目标**：建立「字段名 ↔ `_epi_ops` ↔ 数学项」的三方对应。
2. **操作步骤**：
   - 把 [第 95-113 行](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_default_epi.py#L95-L113) 的 `EpilogueArguments` 字段，与 [第 71-83 行](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_default_epi.py#L71-L83) 的 `_epi_ops`，以及 4.1 的公式 \(\alpha D + \beta C + r + c\) 三者列成一张对照表。
3. **需要观察的现象**：
   - `alpha`/`beta`/`sr_seed` 各对应一个 `Scalar` op；
   - `mRowVecBroadcast`/`mColVecBroadcast` 各对应一个 `RowVecLoad`/`ColVecLoad` op；
   - `mSFD`/`mSFDCol` 各对应一个 `BlockScaleFactorStore` op；
   - `add_to_output`、`rounding_mode` **没有**对应的 EpiOp（它们是编译期开关，由内核直接消费）。
4. **预期结果**：你能说出每个字段由哪个 EpiOp 负责主机侧描述符、哪些字段根本不经过 EpiOp。

#### 4.3.5 小练习与答案

**练习 1**：`add_to_output` 为什么用 `Constexpr[bool]` 而不是普通的 `bool`？

> **答案**：`add_to_output` 决定是否多加一次 D 原值，这会改变 epilogue 的**指令结构**（多一段加载与加法）。用 `Constexpr[bool]` 让它在 trace 期就被烘焙：`False` 时那段代码被编译期折叠出 cubin，`True` 时才生成。若做成运行期 `bool`，cubin 里就必须保留两套分支，既慢又浪费。

**练习 2**：`mSFD` 和 `mSFDCol` 能否同时非 `None`？

> **答案**：不能（语义上「at most one of the two」，见 [第 110 行](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_default_epi.py#L106-L113) 注释）。它们是同一输出的两种量化方向（沿 N 或沿 M 分块的 scale factor），一次输出只能选一种。运行期过滤会保留激活的那个 `BlockScaleFactorStore`。

## 5. 综合实践

把本讲的三块知识（数学、mixin 装配、参数字段）串成一个综合的**源码追踪任务**：

> **任务：追踪一次带 α、bias、split-K 的 GEMM 调用，说清 α/beta 为何用 `Scalar` op，以及线性 epilogue 如何在 split-K staged 归约中被复用以保证逐位一致。**

请按顺序完成：

1. **读 `_epi_ops`**：在 [quack/gemm_default_epi.py:71-83](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_default_epi.py#L71-L83) 确认 `alpha`、`beta` 各自是一个 `Scalar("alpha")` / `Scalar("beta")`。
2. **解释为何用 `Scalar` op**：打开 [quack/epilogue/ops.py:518-601](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/ops.py#L518-L601) 的 `Scalar` 类，重点看 `host_arg_key`（[第 564-570 行](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/ops.py#L564-L570)）与 `config_key`（[第 527-528 行](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/ops.py#L527-L528)）。α/β 有三种形态：**缺省**（不缩放）、**立即数**（host 常量）、**设备指针**（per-call 变化）。`Scalar.host_arg_key` 把这三种形态编码成编译键（`absent`/`immediate`/`pointer`），不同形态会编译出**结构不同的 cubin**：缺省 → `const_expr(alpha is not None)` 为假、整段 α 分支被剔除；指针 → 生成一次 gmem 加载（`load_scalar_or_pointer`）；立即数 → 常量折叠。同时 `config_key` 把 dtype 也纳入键。把 α/β 做成 EpiOp 而不是普通字段，正是为了让「缺省即剔除」「形态即编译键」这两件事自动发生，并让 `α == 1.0` 这类中性折叠能直接退化为「缺省」。
3. **追踪 split-K 复用**：
   - SERIAL / PARALLEL 模式下，epilogue 由 GEMM 内核在**最终合并的那一方 split** 上运行一次，调用 `epi_visit_subtile` → `apply_linear_epilogue`（见 [第 142-143 行](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_default_epi.py#L142-L143) 的注释）。
   - SEPARATE（staged）模式下，GEMM 只写**原始 fp32 partial**（不做任何 epilogue 数学），再由 [quack/split_k_reduce.py](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/split_k_reduce.py) 的归约内核按固定升序求和，并在 [第 225 行](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/split_k_reduce.py#L225) 调用**同一个** `apply_linear_epilogue`。
   - 读 [quack/split_k_reduce.py:1-9](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/split_k_reduce.py#L1-L9) 的模块文档，它明确声明：「epilogue math is shared with the GEMM mixin via `gemm_default_epi.apply_linear_epilogue`」「the reduction order is deterministic, so results are bitwise reproducible run to run」。
4. **得出结论**：因为 GEMM 内核路径与 split-K 归约路径**调用同一个 `apply_linear_epilogue` 函数**，α 缩放、β·C 加宽融进加法、rowvec/colvec 广播加的**顺序与表达式完全相同**，所以无论走哪条 split-K 模式，输出都**逐位一致、可复现**。

> 提示：这一题的答案无法靠「跑命令」得到（需要 H100/B200 级硬件），属于**源码阅读型综合实践」。重点是能用自己的话把「α/β 是带编译键的 Scalar op」与「两路径共用同一数学函数 → 逐位一致」这两条因果链讲清楚。

## 6. 本讲小结

- 默认线性 epilogue 的数学是 \(\,D \leftarrow \alpha D + \beta C + r_n + c_m\,\)，全部在 `apply_linear_epilogue`（[quack/gemm_default_epi.py:21-67](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_default_epi.py#L21-L67)）里就地、按固定顺序、用编译期分支实现。
- `GemmDefaultEpiMixin` 用一组 `_epi_ops`（`Scalar`/`RowVecLoad`/`ColVecLoad`/`BlockScaleFactorStore`）声明 epilogue，重写 `epi_visit_subtile` 注入线性数学，并通过 `(GemmDefaultEpiMixin, GemmSmXX)` 多继承接到各架构——mainloop 归 SM 子类，epilogue 归 mixin。
- `EpilogueArguments` 是用户接口；`add_to_output` 与 `rounding_mode` 是 `Constexpr` 编译期开关，`alpha`/`beta`/向量/量化/Split-K 字段是运行期参数；D 的主机侧管线不在 `_epi_ops`（由内核拥有）。
- α/β 用 `Scalar` EpiOp 表示，是为了让「缺省即编译期剔除」「立即数/指针形态进编译键」「α=1.0 退化为缺省」自动成立。
- 同一个 `apply_linear_epilogue` 被 GEMM 内核的最终 split 与 split-K staged 归约内核共用，因此不同 split-K 模式的输出**逐位一致、可复现**。
- 这个手写 mixin 是「逃生出口」：`GemmSymmetricMixin` 展示了在它之上追加 `TileStore` 辅助输出的标准扩展方式。

## 7. 下一步学习建议

- **u6-l4（`@gemm_epilogue` 函数式创作）」：去对比「函数式前端」与本讲的「手写 mixin」两种创作 epilogue 的取舍，理解为什么 `GemmSymmetricMixin`「选择留在 mixin 而非 fn」。
- **u6-l5（领域 epilogue）」：深入 `quantize_out.py` 里 `BlockScaleFactorStore` 的量化数学，把本讲一笔带过的 `mSFD`/`mSFDCol` codec 彻底搞懂。
- **u8-l3（Split-K 归约）」：系统学习 SERIAL/PARALLEL/SEPARATE 三种模式，理解本讲提到的「最终 split 运行 epilogue」「staged 模式写 raw partial」的全貌。
- **继续阅读源码」：建议顺着 [quack/gemm_base.py:370-477](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_base.py#L370-L477) 的 epilogue 驱动循环，把「`epi_visit_subtile`（数学）→ `_epi_store_quant`（量化）→ `store_convert`（转 dtype）→ `store_r2s`（写 smem）」的完整时序在脑中走一遍，把本讲的数学与 u5-l1 的存储路径缝合。
