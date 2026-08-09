# spec 层：TMA、MMA、TMEM、TensorSpec

## 1. 本讲目标

本讲是 GEMM 设备侧系列的收束篇。前几讲（u5-l1 ~ u5-l4）我们看到了 SM90 / SM100 / SM120 内核各自如何直接调用底层工具（`sm90_utils`、`sm100_utils`）来构造 TMA 描述符、`TiledMma` 和 SMEM/TMEM 布局。本讲我们要看的 `quack/spec/` 子包，是把这些「构造描述符」的散装逻辑，抽象成一套**声明式（declarative）描述符层**的尝试。

学完后你应该能够：

- 理解 `TensorSpec` 如何用一组字段（dtype / shape / stage / layout / cta_group）声明式地描述一个「分阶段存放的 tile」，并由它派生出 SMEM 布局、TMA atom、MMA 配置。
- 读懂 `spec/tma.py` 如何从一个显式的「CTA-value map」构造 TMA 描述符，理解 SM100 2-CTA 场景下 peer CTA 拥有的是「指令面板」而非连续半块。
- 读懂 `spec/mma.py` 的架构分发 `make_tiled_mma_for_arch`，理解 `atom_layout_mnk` 与 `cta_group` 如何决定 MMA atom 布局。
- 读懂 `spec/tmem.py` 如何描述 TMEM（张量内存）的列寻址布局，以及 `TmemStruct` 如何把多个 TMEM 字段背靠背打包。
- 准确区分 **TensorSpec**（运行期 tile 的存放/搬运/计算描述）与 **fake tensor**（编译期符号张量，驱动 `cute.compile`）。

> 重要事实：截至当前 HEAD，`quack/spec/` 包**尚未**被 `quack/spec/` 之外的任何模块导入。它是作者正在演进的原型抽象层（模块文档明确写着「this is a prototype and the API could change rapidly」）。生产 GEMM 内核目前仍直接使用底层 `cutlass.utils.*_helpers`。因此本讲的目标是**理解这套抽象的设计思想与各 helper 的源码**，而不是去生产内核里找它的调用点。这一点会在第 7 节再强调。

## 2. 前置知识

本讲默认你已经具备以下认知（来自前置讲义）：

- **TMA（Tensor Memory Accelerator）**：Hopper 起引入的、用「描述符（descriptor）」驱动整块 GMEM↔SMEM 拷贝的异步搬运单元，靠 mbarrier 的 `complete_tx::bytes` 信用收尾（见 u3-l1、u5-l2）。
- **WGMMA（warpgroup MMA）**：SM90 的矩阵乘指令，累加器在寄存器，操作数可来自 SMEM（SS）或寄存器（RS）；**tcgen05 MMA**：SM100 的矩阵乘指令，累加器直接写进 **TMEM**（张量内存），操作数可来自 SMEM（SS）或 TMEM（TS）（见 u5-l2、u5-l3）。
- **cluster 与 2-CTA MMA**：Blackwell 上 `cta_group=2` 时一对相邻 CTA 协作算一个更大的 tile，每个 CTA 只负责 `mma_tiler_M/2` 行，即「tile 折半」（见 u5-l3）。
- **fake tensor / 符号维度**：用 `cute.sym_int()` 构造的编译期张量，让一份 cubin 复用于一族形状（见 u3-l3、u4-l4）。
- **CuTe Layout**：`(shape, stride)` 的坐标映射，改布局≠搬数据（见 u3-l2）。

一个贯穿本讲的核心直觉：**硬件单元（TMA / MMA / TMEM）各有自己的「寻址契约」**——TMA 要 descriptor、MMA 要 atom 布局、TMEM 要列寻址。`spec/` 层的使命，就是用一份声明式的 operand 描述，去自动满足这三套互不相同的契约。

## 3. 本讲源码地图

本讲涉及的关键文件及其职责：

| 文件 | 行数 | 作用 |
|------|------|------|
| `quack/spec/__init__.py` | 4 | 子包入口，仅一句 docstring |
| `quack/spec/tensor_spec.py` | ~1387 | **核心**：`TensorSpec`（声明式 operand）+ `MatmulSpec`（`A @ B` 的角色视图）+ `BoundMMA`/`BoundMMASm100`（绑定后的 MMA 句柄）|
| `quack/spec/tma.py` | ~278 | TMA 描述符构造：从「CTA-value map」造 TMA CopyAtom，含 SM100 2-CTA 面板映射 |
| `quack/spec/mma.py` | ~168 | 架构分发的 `TiledMma` 构造器 `make_tiled_mma_for_arch`、`operand_leading_atom`、`resolve_mma_inst_k` |
| `quack/spec/smem.py` | ~161 | SMEM 布局构造：K-major / MN-major atom + arch 选择（SM90 `warpgroup` / SM100 `tcgen05`）|
| `quack/spec/tmem.py` | ~301 | TMEM 列寻址布局 `make_tmem_layout`、`TmemAcc`/`TmemOperandA` 字段、`TmemStruct` 打包 |

依赖关系（`tensor_spec.py` 是顶层，其余四件是它的「建材」）：

```
tensor_spec.py ──┬─▶ tma.py ──▶ mma.py
                ├─▶ smem.py ─▶ mma.py
                ├─▶ tmem.py ─▶ (tensor_spec.BoundMMASm100, 类型注解)
                └─▶ mma.py
```

注意 `tma.py`、`smem.py` 都依赖 `mma.py` 的 `operand_leading_atom` / `resolve_mma_inst_k`——这两个纯函数是「operand 几何」的共享真相源（将在 4.3 详述）。

## 4. 核心概念与源码讲解

### 4.1 TensorSpec：声明式 operand 描述与 spec 层总设计

#### 4.1.1 概念说明

`spec/` 层的设计哲学由 `tensor_spec.py` 的模块文档一句话点明：

> `TensorSpec` is a declarative description of a staged tile (dtype, shape, SMEM stage, layout) that drives SMEM layout creation, TMA atom construction, and TMA pipelines. The spec is **storage-only and MMA/epilogue-role agnostic**.

翻译过来：`TensorSpec` 是对「一个被分阶段（staged）存放的 tile」的**声明式描述**，由它来驱动 SMEM 布局创建、TMA atom 构造和 TMA 流水线。关键定语是 **storage-only and role-agnostic（只管存储、不区分 MMA/epilogue 角色）**。

为什么强调「role-agnostic」？因为同一个 SMEM 物理块，既可能被当矩阵乘的 A 操作数，也可能当 B 操作数；A 沿 M 切、B 沿 N 切，但在「存储约定」里两者都是 `(MN, K)` tile 的 mode-0。如果 SMEM 布局和 TMA atom 要知道「这是 A 还是 B」，整个抽象就绑死在 GEMM 上。`TensorSpec` 的选择是：**只描述存储事实**（dtype、tile 形状、主序、stage、是否 2-CTA 分片），把「A/B 角色」这件事推迟到 `MatmulSpec`（由 `A @ B` 得到）里再处理。

由此 spec 层形成两层抽象：

- **`TensorSpec`**：storage-only。派生 SMEM 布局、TMA atom、TMEM 存储布局——这些都是「怎么存、怎么搬」。
- **`MatmulSpec`**（`TensorSpec.__matmul__` 的产物）：role-aware。派生 `tiled_mma`、操作数主序、role-nested 的 SMEM 视图——这些是「怎么算」。

一个非常重要的工程细节：`TensorSpec` 被设计成可以**作为一个参数跨 `@cute.kernel` 边界**传递。它的静态字段（dtype/shape/stage/...）从主机侧模板保留；只有携带 cute 对象的字段（`tma`、`gmem_raw`）通过 MLIR marshaling 协议（`__extract_mlir_values__` / `__new_from_mlir_values__`）序列化；`smem`/`tmem` 则在内核内通过 `with_smem()` / `with_tmem()` 绑定，活在 JIT-local 作用域，无需 marshaling。

> 准确性提示：如前所述，这套 `TensorSpec` API 目前是**原型**，生产内核（`gemm_sm90.py` 等）尚未切换到它。本讲把它作为「理解硬件描述符如何被参数化生成」的范本来读。

#### 4.1.2 核心流程

一个 `TensorSpec` 的生命周期（伪代码）：

```
# ① 主机侧：声明存储描述（role-agnostic）
A_spec = TensorSpec(dtype=BF16, shape=(M, K), stage=3,
                    layout=ROW_MAJOR, cta_group=1)

# ② 内核内：把 GMEM/SMEM/TMEM 绑定上去（仍是 role-agnostic）
A_spec = A_spec.with_smem(sA_field)            # 绑定 SMEM 张量
A_spec = A_spec.with_tma_load(gA_tma_tensor)   # 构造并绑定 TMA atom

# ③ 角色：两个 spec 做 @ 得到 MatmulSpec（role-aware）
mma_spec = A_spec @ B_spec
tiled_mma = mma_spec.tiled_mma(source="SS")    # 派生 TiledMma

# ④ 绑定 MMA 句柄，拿到分区后的 fragment / acc
bound = mma_spec.bind_mma(thr=tidx, sA=A_spec.smem, sB=B_spec.smem)
```

四种「绑定」方法是构建管线的砖块：

| 方法 | 作用 | 产出 |
|------|------|------|
| `with_tma_load(gmem)` | 从 GMEM 张量造 G2S TMA atom 并绑定 | 带 `tma` 的新 spec |
| `with_smem(storage)` | 在内核内把 SMEM 存储字段物化成张量 | 带 `smem` 的新 spec |
| `with_tmem(ptr)` | 把 TMEM 指针按本 spec 的存储布局物化 | 带 `tmem` 的新 spec |
| `__matmul__(other)` | `A @ B` → `MatmulSpec` | 角色视图 |

#### 4.1.3 源码精读

**`TensorSpec` 的字段定义**——这是理解整个 spec 层的入口。注意 `shape` 是**完整逻辑 tile**，`cta_group=2` 时每个 CTA 只存/搬「一半」（见 `storage_shape`）：

声明式字段与构造校验见 [quack/spec/tensor_spec.py:59-115](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/spec/tensor_spec.py#L59-L115)，其中：

- `shape`：完整逻辑 tile。2D 是矩阵乘操作数 `(rows, cols)`；1D 是「带 stage 的向量」辅助操作数（scale/bias/gamma）。
- `stage`：SMEM 流水线级数；`stage=None` 表示这个 tile 在寄存器里（`in_rmem` 为真，没有 SMEM 布局、没有 TMA）。
- `layout`：物理存储主序（`ROW_MAJOR` / `COL_MAJOR`）；`transposed` 只是逻辑 `.T` 视图标志，**不改变底层存储**。
- `cta_group`：tcgen05 2-CTA MMA 的 peer 分片标志；`cta_group=2` 时存储主维（MN）跨 peer 对半切。
- `tma` / `gmem_raw` / `smem` / `tmem`：四种「绑定」产物，初始为 `None`。

`__post_init__` 校验 `cta_group` 合法性并要求 2D 见 [quack/spec/tensor_spec.py:370-373](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/spec/tensor_spec.py#L370-L373)。

**完整 tile vs 每-CTA 存储 tile**——这是 2-CTA 折半规则在 spec 层的体现：

```python
# quack/spec/tensor_spec.py:384-394
@property
def storage_shape(self):
    full = self.full_storage_shape
    if self.rank == 1 or self.cta_group == 1:
        return full
    assert full[0] % self.cta_group == 0, ...
    return (full[0] // self.cta_group, full[1])
```

[quack/spec/tensor_spec.py:384-394](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/spec/tensor_spec.py#L384-L394) 这段说明：`shape` 始终是完整逻辑 tile（如 `(256, 64)`），但 `cta_group=2` 时 `storage_shape` 把存储主维对半切成 `(128, 64)`。所有 SMEM 布局、TMA atom、TMEM 布局都从这个**每-CTA 分片**派生——而 MMA 构造时再把 `cta_group` 读回来（见 4.3）。

**跨 kernel 边界的 marshaling**——只序列化 cute 对象字段：

```python
# quack/spec/tensor_spec.py:117-133
def __extract_mlir_values__(self):
    values = []
    self._n_tma = 0
    self._n_gmem = 0
    if self.tma is not None:
        v = cutlass.extract_mlir_values(self.tma)
        values += v
        self._n_tma = len(v)
    if self.gmem_raw is not None:
        ...
    return values
```

[quack/spec/tensor_spec.py:117-147](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/spec/tensor_spec.py#L117-L147)：`tma`（`TmaInfo` 自己实现 marshaling 协议）和 `gmem_raw` 被摊平成 MLIR 值列表送过边界；`smem` 在内核内绑定、活在本作用域，cute 按引用传递无需 marshaling。`__new_from_mlir_values__` 用 `dataclasses.replace` 重建一个绑定了新值的新 spec。

**`A @ B` → `MatmulSpec`**：

```python
# quack/spec/tensor_spec.py:598-599
def __matmul__(self, other: "TensorSpec") -> "MatmulSpec":
    return MatmulSpec(self, other)
```

[quack/spec/tensor_spec.py:593-599](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/spec/tensor_spec.py#L593-L599)：`MatmulSpec.__init__`（[第 993-1003 行](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/spec/tensor_spec.py#L993-L1003)）校验 `A.shape[1]==B.shape[0]`、`A.cta_group==B.cta_group`，记录完整逻辑 `M/K/N` 与 `cta_group`。它**不再存 shape**，只存派生量，因为 role 视图是「编译期」的。

#### 4.1.4 代码实践

**实践目标**：在源码层面追踪 `TensorSpec` 的「存储事实」如何与「角色」解耦，并验证 `cta_group=2` 的折半规则。

**操作步骤**（纯源码阅读型实践，无需 GPU）：

1. 打开 [quack/spec/tensor_spec.py:59-115](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/spec/tensor_spec.py#L59-L115)，在头脑里（或纸上）构造一个 `TensorSpec(dtype=BF16, shape=(256,64), stage=3, layout=ROW_MAJOR, cta_group=2)`。
2. 手算它的 `full_storage_shape` 与 `storage_shape`：`ROW_MAJOR` 且不转置时，`full_storage_shape = shape = (256, 64)`；`cta_group=2` → `storage_shape = (128, 64)`。
3. 阅读 `_storage_major`（[第 1377-1387 行](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/spec/tensor_spec.py#L1377-L1387)），推导：同一个 `ROW_MAJOR` 存储，作 A 操作数时主序是 `K`、作 B 操作数时主序是 `MN`——这正说明「存储布局不区分 A/B，主序由角色推导」。

**需要观察的现象**：`storage_shape` 把存储主维对半切，但 `shape`（喂给 MMA 的完整逻辑 tile）保持 `(256,64)` 不变。这两者的分离是 2-CTA 折半能「对 A 沿 M 切、对 B 沿 N 切、却共用同一份存储约定」的关键。

**预期结果**：你能用自己的话解释——为什么 SMEM 布局和 TMA atom 可以做到 role-agnostic（因为它们只看 `storage_shape`），而 MMA 构造必须 role-aware（因为它要把完整 `M/N` 和 `cta_group` 一起读回来）。

#### 4.1.5 小练习与答案

**练习 1**：`TensorSpec` 的 `transposed=True` 和直接把 `shape` 写成转置形状，有什么区别？

**答案**：`transposed` 只是一个逻辑 `.T` 视图标志，**不改变底层存储的字节布局**（`layout` 字段不变）。同一个物理存储既可作未转置操作数、又可作转置操作数，省一次拷贝。而把 `shape` 写成转置形状会改变逻辑维度语义，且无法表达「同一块存储的两种视图」。参见 `.T` 属性 [第 353-357 行](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/spec/tensor_spec.py#L353-L357)。

**练习 2**：为什么 `stage=None` 的 spec 不能调用 `smem_layout()`？

**答案**：`stage=None` 表示 tile 在寄存器里（`in_rmem=True`），没有 SMEM 存储，自然没有 SMEM 布局。`smem_layout()` 开头有 `assert not self.in_rmem`（[第 414 行](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/spec/tensor_spec.py#L414)）。

---

### 4.2 TMA descriptor 构造（spec/tma.py）

#### 4.2.1 概念说明

TMA（Tensor Memory Accelerator）不用「基地址 + 每元素算地址」的传统方式搬数据，而是吃一个**描述符（descriptor）**，描述符里编码了张量的形状、步长、swizzle 模式等。CPU 在 launch 前把描述符造好，GPU 端 TMA 单元按描述符整块搬运，极大减少地址计算开销（见 u3-l1、u5-l2）。

`tma.py` 解决的核心问题是：**给定一个 GMEM 张量和一个 SMEM 布局，如何造出一个能让硬件理解整块拷贝的 TMA CopyAtom？** 它的答案是一个叫 **CTA-value map（cta_v_map）** 的中间表示。

CTA-value map 回答的问题：在一个 CTA 合作搬运的 tile 里，**每个 CTA 拥有其中的哪些 value（元素）？** 对于单 CTA（`cta_group=1`），答案平凡——一个 CTA 拥有整块的连续存储，map 就是恒等布局。但对于 SM100 的 2-CTA，答案非平凡：两个 peer CTA 协作搬一个 512 行的大 tile，**每个 CTA 拥有的是若干「指令面板（instruction panel）」，而不是连续的半块**。`tma.py` 的模块文档说得很清楚：

> SM100 2-CTA dense loads need an explicit tcgen05 map because a peer CTA owns instruction panels, not a contiguous half tile.

`tma.py` 同时支持四种 TMA 拷贝语义：G2S 单播加载、G2S 多播加载、S2G 存储、S2G 归约存储。

#### 4.2.2 核心流程

TMA atom 构造的统一入口是 `_make_tiled_tma_atom_from_cta_v_map`，它接受「op 语义 + GMEM 张量 + SMEM 布局 + CTA-value map」四元组，派发到底层 MLIR 构造器：

```
输入: op ∈ {G2S, G2S-Multicast, S2G, S2G-Reduce}
      gmem_tensor   (TMA 坐标张量)
      smem_layout   (SMEM 布局，可能带 swizzle 的 ComposedLayout)
      cta_v_map     (CTA-value map，描述每个 CTA 拥有哪些 value)

  ├─ 若 internal_type 给定 (亚字节 dtype): 计算 tma_format (unpack 与否)
  └─ 按 op 类型派发到 _cute_nvgpu_ir.atom_make_non_exec_tiled_tma_{load|store|reduce}
     → 返回 TmaInfo(CopyAtom, tma_tensor, smem_layout)
```

`TensorSpec._make_tma_atom` 在其上做了一层 role-agnostic 的封装，按 `cta_group` 选择 cta_v_map：

- `cta_group==2`：用 `_sm100_dense_tma_flat_cta_v_map`（面板映射）。
- `cta_group==1`（含 1D）：用 `cute.composition(identity_layout(gmem_shape), storage_shape)`——标准的「连续 flat」映射，与 CuTe 通用 tile TMA 助手一致。

#### 4.2.3 源码精读

**模块文档**点明了单 CTA 与 2-CTA 的本质区别，见 [quack/spec/tma.py:3-8](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/spec/tma.py#L3-L8)。

**统一构造器** `_make_tiled_tma_atom_from_cta_v_map` 是本文件的核心。先看它如何处理「SMEM 布局可能多一维（stage）」与「亚字节 dtype 的 tma_format」：

```python
# quack/spec/tma.py:52-76
smem_rank = cute.rank(smem_layout)
map_rank = cute.rank(cta_v_map)
if smem_rank == map_rank + 1:
    smem_layout = cute.select(smem_layout, mode=list(range(map_rank)))  # 去掉 stage 维

...
tma_format = None
if internal_type is not None:
    use_unpack = (
        itype.width == 8
        and isinstance(gmem_tensor.element_type, NumericMeta)
        and gmem_tensor.element_type.width < 8   # GMEM 是 <8bit，SMEM 按 8bit 存
    )
    internal_mlir_type = gmem_tensor.element_type.mlir_type if use_unpack else itype.mlir_type
    tma_format = _cute_nvgpu_ir.TmaDataFormat(...)
```

[quack/spec/tma.py:52-76](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/spec/tma.py#L52-L76)：`smem_rank == map_rank + 1` 处理 SMEM 布局带了 stage 维而 cta_v_map 没带的情况（TMA 描述符只描述单 stage 的几何）。`internal_type` + `use_unpack` 处理 NVFP4 这类 4-bit 量化：GMEM 按 4-bit 存、SMEM 按 8-bit 解包存放，`tma_format` 把这个「解包格式」编码给硬件。

随后按 op 类型派发，以 G2S 单播加载为例：

```python
# quack/spec/tma.py:78-97
if isinstance(op, cpasync.CopyBulkTensorTileG2SOp):
    if num_multicast != 1:
        raise ValueError(...)
    res = _cute_nvgpu_ir.atom_make_non_exec_tiled_tma_load(
        cast(Any, gmem_tensor).value,   # ← 把 shape/stride/swizzle 编码进去
        smem_for_ir,
        cta_v_map,                       # ← CTA-value 映射
        op._to_ir(),
        num_multicast=num_multicast,
        tma_format=tma_format,
        loc=loc, ip=ip,
    )
    return TmaInfo(
        cute_atom.CopyAtom(op, CopyBulkTensorTileG2SNonExecTrait(res[0])),
        res[1],                          # tma_tensor (TMA 坐标张量)
        stored_smem_layout,
    )
```

[quack/spec/tma.py:78-97](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/spec/tma.py#L78-L97)：注意第一个实参 `gmem_tensor.value`——**TMA descriptor 的形状/步长/swizzle 信息正是从这个 GMEM 张量的 MLIR 值编码进硬件描述符的**。`atom_make_non_exec_tiled_tma_load` 在底层把张量的几何与 SMEM 布局、CTA-value map 一起烘焙成一个不可执行的 TMA CopyAtom（"non-exec" 表示它只描述拷贝几何、不含线程绑定，执行时再 `get_slice` 分区）。

**SM100 2-CTA 的面板映射**——这是本文件最精妙处。看 flat 版的 cta_v_map 构造：

```python
# quack/spec/tma.py:209-230
def _sm100_dense_tma_flat_cta_v_map(shape, cta_group=1):
    """CTA-value map for a flat role-free SM100 TMA storage view.
    For a 2-CTA full leading tile of 512, each CTA owns two 128-wide
    instruction panels: CTA0 maps rows 0..127 and 256..383, while CTA1
    maps the complementary panels. The gap belongs in the CTA-value map;
    the SMEM descriptor can stay the normal flat (local_leading, k) layout."""
    rows, cols = shape
    atom_rows, rest_rows = spec_mma.operand_leading_atom(rows, cta_group)
    leading_panel_stride = rows * cute.E(0) if rest_rows > 1 else 0
    return cute.coalesce(
        cute.make_layout(
            ((atom_rows, rest_rows), cols),
            stride=((cute.E(0), leading_panel_stride), cute.E(1)),
        ),
        target_profile=(1, 1),
    )
```

[quack/spec/tma.py:209-230](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/spec/tma.py#L209-L230)：关键在 stride。`cute.E(0)` 是「编译期零步长」（广播），`cute.E(1)` 是「编译期单位步长」。这段布局的语义是：值的第一维按 `((atom_rows, rest_rows), ...)` 组织，相邻 atom 行之间步长为 0（连续）、不同 panel 组之间步长为 `leading_panel_stride`。**正是这个步长把「CTA0 拥有第 0..127 行和第 256..383 行」这种非连续所有权编码进了 TMA 描述符**——而 SMEM 描述符反而能保持平凡的连续 flat 布局。注释明确指出：「The gap belongs in the CTA-value map; the SMEM descriptor can stay the normal flat layout」。

> `operand_leading_atom`（见 4.3）把每-CTA 行数拆成 `(atom_rows, rest_rows)`：`cta_group=2` 时若全 tile 512 行，则每 CTA 256 行、`atom_rows=128`、`rest_rows=2`——每个 CTA 拥有两个 128 行的面板。

**`TensorSpec._make_tma_atom` 的 role-agnostic 封装**：

```python
# quack/spec/tensor_spec.py:448-470
elif self.rank == 2 and self.cta_group == 2:
    assert (isinstance(op, (cpasync.CopyBulkTensorTileG2SOp,
                            cpasync.CopyBulkTensorTileG2SMulticastOp))
            and op.cta_group == tcgen05.CtaGroup.TWO), ...
    # SMEM 描述符用平凡 flat 每-CTA 存储视图; 只有 GMEM 坐标映射非连续
    tma_smem_layout = cute.select(self.smem_layout(), mode=[0, 1])
    cta_v_map = spec_tma._sm100_dense_tma_flat_cta_v_map(self.storage_shape, cta_group=2)
else:
    ...
    tma_smem_layout = cute.select(self.smem_layout(), mode=modes)
    cta_v_map = cute.composition(
        cute.make_identity_layout(gmem_tensor.shape),
        self.storage_shape,
    )
```

[quack/spec/tensor_spec.py:448-470](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/spec/tensor_spec.py#L448-L470)：`cta_group==1` 时 cta_v_map 是 `identity_layout(gmem_shape)` 与 `storage_shape` 的 composition——这是 CuTe 通用 tile TMA 助手用的同一个 flat 映射。`cta_group==2` 才走面板映射。

**`slice_tma_tile_by_mma_cta`** 处理「从完整 MMA 操作数 tile 选出本 CTA 的切片」：[quack/spec/tma.py:233-278](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/spec/tma.py#L233-L278)。`cta_group==1` 时直接返回（一个 CTA 拥有全部）；`cta_group==2` 时用 `cute.flat_divide` 沿行切，可选地用 `_sm100_dense_tma_cta_v_map_from_shape` 重塑成 `partition_A/B` 产生的嵌套布局（`exact_layout=True`）。

#### 4.2.4 代码实践

**实践目标**（即任务书指定的实践）：在 `spec/tma.py` 中找到 TMA descriptor 的构造方式，解释它如何把张量的 shape/stride/swizzle 编码给硬件，并说明 TensorSpec 与 fake tensor 的区别。

**操作步骤**：

1. 打开 [quack/spec/tma.py:78-97](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/spec/tma.py#L78-L97)，定位 `atom_make_non_exec_tiled_tma_load` 的四个关键实参。
2. 追踪每个实参的来源：
   - `gmem_tensor.value` → 张量的 **shape/stride**（来自主机侧 `cute.make_tensor` 构造，swizzle 若有则编码在它的 layout 里）。
   - `smem_for_ir` → **SMEM 布局**（含 swizzle atom，见 4.3 的 `make_smem_layout`）。
   - `cta_v_map` → **CTA-value 映射**（编码「每个 CTA 拥有哪些元素」，见上面的面板映射）。
   - `op._to_ir()` → 拷贝**语义**（G2S / 多播 / S2G / 归约）与 `cta_group`。
3. 阅读 `_sm100_dense_tma_flat_cta_v_map`（[第 209-230 行](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/spec/tma.py#L209-L230)），手算：`cta_group=2`、`storage_shape=(256,64)` 时，`atom_rows=128`、`rest_rows=2`，CTA0 拥有行 `0..127` 与 `256..383`（在完整 512-行 tile 里）。

**TensorSpec 与 fake tensor 的区别**（任务书要求）：

| 维度 | fake tensor（u3-l3、u4-l4） | TensorSpec（本讲） |
|------|------------------------------|---------------------|
| **本质** | 编译期**符号张量**，用 `cute.sym_int()` 让 batch/M/N/K 为符号维 | 运行期 tile 的**声明式描述**（dtype/shape/stage/layout/cta_group）|
| **服务对象** | `cute.compile()`，让一份 cubin 复用于一族形状 | 内核内的 SMEM 布局/TMA atom/MMA 配置派生 |
| **关心什么** | 形状的可整除性（`divisibility`）、对齐、连续维 | 存储主序、stage 级数、2-CTA 分片、TMA/TMEM 绑定 |
| **关系** | 是**编译签名**的输入 | 可在内核内由 fake 张量的 gmem 视图构建（`with_tma_load(gA)`）|

一句话：**fake tensor 解决「编译期形状符号化」，TensorSpec 解决「运行期 tile 如何存/搬/算」**。它们互补而非替代——你完全可以用一个 fake 张量做 GMEM 视图，喂给 `TensorSpec.with_tma_load()`。

**预期结果**：你能复述「TMA descriptor 的几何信息从 `gmem_tensor.value`（shape/stride/swizzle）和 `smem_for_ir` 编码，CTA 所有权从 `cta_v_map` 编码，语义从 `op` 编码」，并说清 TensorSpec 与 fake tensor 的分工。

#### 4.2.5 小练习与答案

**练习 1**：为什么 2-CTA 场景下「缝隙（gap）」要放进 CTA-value map，而 SMEM 描述符可以保持 flat？

**答案**：因为 TMA 加载的源是 GMEM，两个 peer CTA 在 GMEM 中确实拥有非连续的面板（CTA0 拿第 0..127、256..383 行），这个非连续性必须在 GMEM 侧的 CTA-value map 里表达。但加载目的地是各自 CTA 私有的 SMEM，每个 SMEM 里存的是**连续的**本地 `(128, k)` 分片——所以 SMEM 描述符保持平凡 flat 布局即可。非连续性被「推」到了 GMEM 坐标映射这一侧。

**练习 2**：`use_unpack=True` 何时成立，含义是什么？

**答案**：当 `internal_type.width==8` 且 GMEM 元素 `<8` bit（如 NVFP4 是 4-bit）时成立。含义是 TMA 从 GMEM 按 4-bit 读，但在 SMEM 里按 8-bit 解包存放，`tma_format` 把这个解包格式告诉硬件，避免后续 MMA 再做位拆分。见 [第 68-76 行](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/spec/tma.py#L68-L76)。

---

### 4.3 MMA 指令封装与 atom 布局（spec/mma.py + spec/smem.py）

#### 4.3.1 概念说明

矩阵乘指令（MMA）在三代架构上是三套完全不同的指令：

- **SM80/SM120**：warp 级 `mma.sync`（指令 `(16,8,16)`），操作数经 `ldmatrix` 从 SMEM 取到寄存器。
- **SM90（Hopper）**：warpgroup 级 **WGMMA**，操作数可来自 SMEM（SS）或寄存器（RS），累加器在寄存器。
- **SM100（Blackwell）**：**tcgen05 MMA**，操作数可来自 SMEM（SS）或 TMEM（TS），累加器写进 TMEM。

`spec/mma.py` 的 `make_tiled_mma_for_arch` 把这三套指令封装成一个**架构分发的统一构造器**，输入是 `MatmulSpec`（携带完整 M/N/K、`cta_group`、操作数 dtype 与存储主序），输出是一个 `cute.TiledMma`（描述「一次 MMA 指令算多大盘、怎么分区到 warp/warpgroup」的对象）。

这里的 **atom 布局**（`atom_layout_mnk`）是一个关键旋钮：它描述「在 tile 内部，沿 M/N/K 各重复多少个 MMA atom」。在 SM90 上它直接对应 u5-l2 讲过的 coop / pingpong 两种 warp 分工（`atom_layout_mnk[1]` 把 N 维切给多个 warp-group）。

本模块还连带讲 `spec/smem.py`：因为 MMA atom 选定后，**SMEM 布局必须与 MMA atom 的读取契约匹配**（swizzle 模式、主序）。`smem.py` 提供了与 atom 配套的 SMEM 布局构造。

#### 4.3.2 核心流程

`make_tiled_mma_for_arch` 的分发逻辑（按 `arch.major`）：

```
arch.major == 9 (SM90 WGMMA):
   要求 cta_group==1, source ∈ {SS, RS}
   a_major: RS 强制 K-major; SS 取 spec 存储主序
   tiler_mn = (64, N // atom_layout_mnk[1])   ← atom_layout_mnk[1] 把 N 切给 warp-group
   → sm90_utils.make_trivial_tiled_mma(...)

arch.major ∈ {8, 12} (warp MMA):
   要求 cta_group==1, source ∈ {SS, RS}
   mma_inst_mnk = (16,8,16); fp16/bf16 用 MmaF16BF16Op
   permutation_mnk 默认把 N ×2 以利用 ldmatrix.x4
   → cute.make_tiled_mma(op, tC, permutation_mnk=...)

arch.major ∈ {10, 11} (Blackwell tcgen05):
   source ∈ {SS, TS}; cta_group ∈ {1,2}
   n_inst = N if N<=256 else N//2          ← 大 N 时指令 N 折半
   a_source: TS→TMEM, SS→SMEM
   → sm100_utils.make_trivial_tiled_mma(..., cta_group_enum, (M, n_inst), a_source)
```

两个被 `tma.py`/`smem.py`/`tensor_spec.py` 共享的纯函数是「operand 几何」的真相源：

- `resolve_mma_inst_k(dtype)` = `256 // dtype.width`：MMA 指令的 K 维元素数。fp16→16，fp8→32。
- `operand_leading_atom(rows, cta_group)` → `(atom_rows, rest_rows)`：把每-CTA 主维行数拆成「指令 atom 行数」与「重复次数」，镜像 u5-l3 的 2-CTA 折半规则。

#### 4.3.3 源码精读

**`operand_leading_atom`——2-CTA 折半的几何真相源**：

```python
# quack/spec/mma.py:44-55
def operand_leading_atom(rows: int, cta_group: int) -> Tuple[int, int]:
    """Return (atom_rows, rest_rows) for a per-CTA MMA operand tile."""
    assert cta_group in (1, 2), ...
    full_rows = rows * cta_group               # 还原成完整 tile 行数
    inst_rows = full_rows if full_rows <= 256 else full_rows // 2
    assert full_rows % inst_rows == 0, ...
    assert inst_rows % cta_group == 0, ...
    return inst_rows // cta_group, full_rows // inst_rows
```

[quack/spec/mma.py:44-55](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/spec/mma.py#L44-L55)：注意它先把「每-CTA 行数 `rows`」乘回 `full_rows = rows * cta_group` 得到完整 tile 行数，再决定指令行数 `inst_rows`（>256 时折半），最后返回每-CTA 的 `(inst_rows/cta_group, full_rows/inst_rows)`。这正是 u5-l3 讲的「`mma_tiler_M ∈ {128,256}` 时 per-CTA M tile 折半」在 spec 层的纯函数实现。`resolve_mma_inst_k` 见 [第 58-62 行](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/spec/mma.py#L58-L62)。

**架构分发 `make_tiled_mma_for_arch`**——先看 SM90 分支：

```python
# quack/spec/mma.py:82-107
if arch.major == 9:  # Hopper — WGMMA
    assert cta_group == 1, ...
    assert source in ("SS", "RS"), ...
    # WGMMA RS 的物理 A 是寄存器片段, 约定为 K-major, 与 spec SMEM 布局无关
    a_major = "K" if source == "RS" else spec._operand_major(spec.A, is_A=True)
    b_major = spec._operand_major(spec.B, is_A=False)
    mode = {"K": OperandMajorMode.K, "MN": OperandMajorMode.MN}
    a_source = warpgroup.OperandSource.RMEM if source == "RS" else warpgroup.OperandSource.SMEM
    return sm90_utils.make_trivial_tiled_mma(
        spec.A.dtype, spec.B.dtype, mode[a_major], mode[b_major], acc_dtype,
        atom_layout_mnk=atom_layout_mnk,
        # atom_layout_mnk[1] 把逻辑/物理 N tile 切给 warp-group;
        # 每个 warpgroup 的 N 幅度 = full N / atom-layout N 因子
        tiler_mn=(64, spec.N // atom_layout_mnk[1]),
        a_source=a_source,
    )
```

[quack/spec/mma.py:82-107](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/spec/mma.py#L82-L107)：两处要点。其一，**RS 源强制 A 为 K-major**——因为寄存器片段的布局约定是 K-major，与 spec 的 SMEM 存储主序无关（这正是 u5-l2 讲过「`transform_a` 强制走 RS」的根因）。其二，`tiler_mn=(64, N // atom_layout_mnk[1])`：WGMMA atom 每 warpgroup 的 M 固定 64，N 由 `atom_layout_mnk[1]` 切分——`atom_layout_mnk[1]=2` 即两个 warpgroup 各算 N/2，对应 u5-l2 的 coop 模式。

**SM100 分支**（与 u5-l3 呼应）：

```python
# quack/spec/mma.py:131-165
elif arch.major in [10, 11]:  # Blackwell tcgen05
    assert source in ("SS", "TS"), ...
    cta_group_enum = tcgen05.CtaGroup.TWO if cta_group == 2 else tcgen05.CtaGroup.ONE
    m_full, n_full = spec.M, spec.N
    n_inst = n_full if n_full <= 256 else n_full // 2     # 大 N 折半
    if source == "TS":
        # TMEM A 是新物化的物理操作数, 忽略 transposed, S.T 当行主序 (D,N) TS-A tile
        a_major = OperandMajorMode.K if spec.A.layout == LayoutEnum.ROW_MAJOR else OperandMajorMode.MN
    else:
        a_major = OperandMajorMode.K if spec._storage_major(spec.A, is_A=True) == "K" else OperandMajorMode.MN
    ...
    a_source = tcgen05.OperandSource.TMEM if source == "TS" else tcgen05.OperandSource.SMEM
    return sm100_utils.make_trivial_tiled_mma(
        spec.A.dtype, spec.B.dtype, a_major, b_major, acc_dtype,
        cta_group_enum, (m_full, n_inst), a_source,
    )
```

[quack/spec/mma.py:131-165](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/spec/mma.py#L131-L165)：SM100 的两个关键差异。其一，`cta_group` 直接读自 `spec.cta_group`（存储属性），变成 `tcgen05.CtaGroup` 枚举传给底层。其二，`n_inst = n_full if n_full<=256 else n_full//2`——大 N 时指令 N 折半，与 `operand_leading_atom` 的 `inst_rows` 折半是同一个几何对称（M 侧与 N 侧各有一份）。`source="TS"` 时 A 来自 TMEM，且主序直接看 `layout` 字段（因为 TMEM A 是新物化的物理操作数，`.T` 要真正物化成 `(D,N)` 而非复用存储）。

**SMEM 布局与 atom 配套**（`spec/smem.py`）。SMEM 布局由 dtype、tile 形状、主序、stage 决定，arch 自动选 atom（SM90 用 `warpgroup.make_smem_layout_atom`，SM100 用 `tcgen05.make_smem_layout_atom`）：

```python
# quack/spec/smem.py:133-160
arch = cutlass.base_dsl.BaseDSL._get_dsl().get_arch_enum()
if arch.major not in [10, 11]:
    smem_layout_atom = warpgroup.make_smem_layout_atom(
        sm90_utils.get_smem_layout_atom(layout, dtype, major_mode_size), dtype)
else:  # Blackwell
    major_mode = OperandMajorMode.MN if layout.is_m_major_c() else OperandMajorMode.K
    smem_layout_atom = tcgen05.make_smem_layout_atom(
        sm100_utils.get_smem_layout_atom_ab(major_mode, dtype, tile), dtype)
...
# coalesce 去掉 swizzle-atom 因子, 给调用者一个规范的 (M,N[,stage]) 视图;
# MMA 专用的嵌套操作数视图由 MatmulSpec 另造
return cute.coalesce(smem_layout_staged, target_profile=...)
```

[quack/spec/smem.py:119-160](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/spec/smem.py#L119-L160)：核心思想是「swizzle 是存储事实、与 A/B 角色无关」。`TensorSpec.smem_layout()` 调它得到一个**role-free 的存储视图**（`coalesce` 把 swizzle atom 折叠进 outer，但寻址不变）；而 role-nested 的 `partition_A/B` 视图由 `MatmulSpec.smem_view_A/B`（[第 1283-1305 行](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/spec/tensor_spec.py#L1283-L1305)）另造——这正是 4.1 讲的「存储与角色分离」在 SMEM 侧的体现。

> `smem.py` 还提供 `make_smem_layout_kmajor` / `make_smem_layout_mnmajor`（[第 17-109 行](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/spec/smem.py#L17-L109)），是 Blackwell 专用、显式 K-major / MN-major 的 staged SMEM 布局构造器，内部用 `operand_leading_atom` 拆 atom——同样的几何真相源。

#### 4.3.4 代码实践

**实践目标**：跟踪 `atom_layout_mnk` 与 `cta_group` 如何分别影响 SM90 与 SM100 的 MMA atom 布局，并验证 `operand_leading_atom` 的折半。

**操作步骤**：

1. 打开 [quack/spec/mma.py:82-107](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/spec/mma.py#L82-L107)。假设 `spec.N=128`、`atom_layout_mnk=(1,2,1)`，手算 SM90 的 `tiler_mn=(64, 128//2)=(64,64)`——即两个 warpgroup 各算 64 列（coop）。
2. 打开 [quack/spec/mma.py:44-55](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/spec/mma.py#L44-L55)。设每-CTA `rows=128`、`cta_group=2`：`full_rows=256`，`inst_rows=256`（≤256 不折半），返回 `(256//2, 256//256)=(128, 1)`。再设每-CTA `rows=256`、`cta_group=2`：`full_rows=512`，`inst_rows=256`（>256 折半），返回 `(256//2, 512//256)=(128, 2)`——后者正是「完整 512 tile、每 CTA 两个 128 行面板」。
3. 对比 SM90 与 SM100 分支：SM90 的 `cta_group` 被 `assert==1` 锁死，`atom_layout_mnk` 直接进 `tiler_mn`；SM100 的 `cta_group` 从 spec 读、变 `CtaGroup` 枚举，`atom_layout_mnk` 不进 tiler（tiler 用 `(M, n_inst)`）。

**需要观察的现象**：`atom_layout_mnk` 是 SM90 coop/pingpong 的旋钮（u5-l2），而 `cta_group` 是 SM100 2-CTA 的旋钮（u5-l3）——同一套 spec 字段，在两代架构上驱动了**不同的分区机制**。

**预期结果**：你能解释为什么 SM100 分支不需要 `atom_layout_mnk`（因为 tcgen05 的 2-CTA 协作由 `cta_group` 在指令层面原生支持，而非靠 atom 布局切 warp-group），以及 `operand_leading_atom` 如何同时服务 TMA 面板映射（4.2）和 SMEM 布局（smem.py）。

#### 4.3.5 小练习与答案

**练习 1**：SM90 的 RS 源为什么要把 A 强制设为 K-major？

**答案**：RS（register source）表示 A 操作数从寄存器片段喂给 WGMMA，而寄存器片段的布局约定是 K-major。这与 spec 的 SMEM 存储主序无关——物理 A 操作数在寄存器里永远是 K-major。见 [第 91 行](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/spec/mma.py#L91) 注释。

**练习 2**：`resolve_mma_inst_k(Float16)` 和 `resolve_mma_inst_k(Float8E4M3FN)` 各返回多少？

**答案**：`256 // 16 = 16`；`256 // 8 = 32`。dtype 越窄，单条 MMA 指令的 K 维元素越多（一条指令塞进更多窄元素）。见 [第 58-62 行](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/spec/mma.py#L58-L62)。

**练习 3**：SM120（`arch.major==12`）走哪个分支？为什么 N 维要 `×2`？

**答案**：走 warp-level MMA 分支（`arch.major in [8,12]`）。`permutation_mnk` 的 N 维 `×2` 是为了利用 `ldmatrix.x4`（一次加载 4 倍宽度），与参考实现 `blackwell_geforce/dense_gemm.py` 对齐。见 [第 121-129 行](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/spec/mma.py#L121-L129)。

---

### 4.4 TMEM 布局与 TmemStruct（spec/tmem.py + TensorSpec 的 TMEM 侧）

#### 4.4.1 概念说明

**TMEM（Tensor Memory）** 是 Blackwell（SM100）独有的、紧贴张量核心的一块专用存储：**128 lane × 512 column**，每个 cell 是 32-bit，**按列寻址**。tcgen05 MMA 把累加器直接写进 TMEM（而不是寄存器），独立的 epilogue warp 再用 `tcgen05.ld` 从 TMEM 取回寄存器（见 u5-l3）。

TMEM 的寻址与 SMEM/GMEM 截然不同，它的地址编码是：

\[
\text{TMEM 地址} = (\text{dp\_lane} \ll 16)\ \|\ \text{column}
\]

其中 `dp_lane ∈ [0,128)` 是 data-path lane，`column ∈ [0,512)` 是列号，都以 **32-bit 字**为单位。当用更窄的 dtype（如 bf16）视图时，每个 32-bit cell 装多个元素，于是「以元素为单位」的列号更密、DP lane 步长更大。

`tmem.py` 的职责：给定一个 dtype 和 tile 形状，**构造 TMEM 的逻辑布局**（CuTe Layout），让累加器、TMEM-resident 的 A 操作数能正确地映射到这片 128×512 的列寻址存储。它还提供 `TmemStruct`——把一个内核用到的多个 TMEM 字段（累加器、A 操作数、别名区）背靠背打包，并算出总列数、校验不超容量。

#### 4.4.2 核心流程

TMEM 布局的核心函数 `make_tmem_layout(dtype, shape, stage, interleaved=False)`：

```
输入: dtype (≤32bit), shape=(rows, cols), stage ∈ {1,2,3,4}

elems_per_col = 32 // dtype.width      # 每 32-bit cell 装几个元素
dp_stride     = (1<<16) * (32 // dtype.width)   # DP lane 步长(元素单位)
stage_stride  = 向上取整到 elems_per_col 的倍数的 cols

if rows == 128:  # 用满全部 128 DP lane, 线性
    layout = ((128, cols, stage), stride=(dp_stride, 1, stage_stride 或 0))
if rows == 64:   # 半子分区: 行按 (16,4) 分组映射到 DP [0:16],[32:48],[64:80],[96:112]
    layout = (((16,4), cols, stage), stride=((dp_stride, 32*dp_stride), 1, ...))
```

`TmemStruct` 的打包流程：

```
对每个字段 (name, field):
    col = field.num_cols()         # 该字段占多少 TMEM 列(经 tcgen05.find_tmem_tensor_col_offset)
    offset += col                  # 背靠背累加
total = 向上取整到 2 的幂
assert total <= get_max_tmem_alloc_cols("sm_100")   # 不超 512 列容量
bind(base_ptr) → 每个字段在 base_ptr + offset 处物化视图
```

#### 4.4.3 源码精读

**模块文档**精确刻画了 TMEM 与 SMEM 的类比与差异，见 [quack/spec/tmem.py:18-26](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/spec/tmem.py#L18-L26)：

> TMEM is column-addressed: 128 lanes × 512 columns of 32-bit cells, a field's footprint is its column count ... every field spans all 128 lanes, and offsets are added to the (32-bit-typed) TMEM base pointer. Field layouts come from a tiled_mma, not from (dtype, size)...

关键点：①字段 footprint 按「列数」计；②每个字段横跨全部 128 lane；③字段布局来自 `tiled_mma`（而非纯 dtype+size），因为 TMEM 累加器布局由 MMA 指令决定。

**DP 步长与列密度**——TMEM 地址编码在元素单位的体现：

```python
# quack/spec/tmem.py:28-32
def _tmem_dp_stride(dtype: type[cutlass.Numeric]) -> int:
    assert dtype.width <= 32 and 32 % dtype.width == 0, ...
    return (1 << 16) * (32 // dtype.width)
```

[quack/spec/tmem.py:28-32](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/spec/tmem.py#L28-L32)：物理地址是 `(dp<<16)|col`（32-bit 字）。`tmem_ptr<T>` 会按 dtype 宽度做亚字缩放，所以**元素单位**的 DP 步长是 `(1<<16) * (32/width)`——bf16(width=16) 时是 `2*(1<<16)`，f32(width=32) 时是 `1*(1<<16)`。列在元素单位保持连续（步长 1）。这段是理解 TMEM 布局的数学基础。

**`make_tmem_layout`——M=128 与 M=64 两种物理形**：

```python
# quack/spec/tmem.py:43-98（节选 M=64 半子分区分支）
elems_per_col = 32 // dtype.width
dp_stride = _tmem_dp_stride(dtype)
stage_stride = ((cols + elems_per_col - 1) // elems_per_col) * elems_per_col
if rows == 64:
    ...
    return cute.make_layout(
        ((16, 4), cols, stage),
        stride=((dp_stride, 32 * dp_stride), 1, 0 if stage == 1 else stage_stride),
    )
return cute.make_layout(   # M=128 用满 128 lane
    cute.append(shape, stage),
    stride=(dp_stride, 1, 0 if stage == 1 else stage_stride),
)
```

[quack/spec/tmem.py:43-98](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/spec/tmem.py#L43-L98)：M=128 时行线性映射到 128 个 DP lane（步长 `dp_stride`）。M=64 时 tcgen05 用「半子分区」——行按 `(16,4)` 分组，4 组分别映射到 DP lane `[0:16]`、`[32:48]`、`[64:80]`、`[96:112]`，所以内层 4 的步长是 `32*dp_stride`（跳 32 个 lane）。`stage_stride` 让多级累加器在列方向错开；`stage==1` 时步长为 0（单级无需错开）。`interleaved=True`（[第 74-90 行](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/spec/tmem.py#L74-L90)）是 1SM SS 累加器的特殊 packing，stage3 被拒绝（非矩形仿射）。

**`TmemAcc` 与 `TmemOperandA` 字段**——两类 TMEM 字段。`TmemAcc` 的列数由累加器 fragment 决定：

```python
# quack/spec/tmem.py:117-137（节选）
@dataclass
class TmemAcc(_TmemFieldBase):
    """Accumulator region: staged (MMA, MMA_M, MMA_N[, STAGE]) TMEM tensor."""
    mma: "BoundMMASm100"
    stages: Optional[int] = None
    def num_cols(self) -> int:
        return tcgen05.find_tmem_tensor_col_offset(self._make_frag())
    def view(self, base_ptr, col_offset):
        return cute.make_tensor(base_ptr + col_offset, self._make_frag().layout)
```

[quack/spec/tmem.py:117-137](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/spec/tmem.py#L117-L137)：累加器布局来自 `mma._make_acc_frag()`（由 `tiled_mma` 决定），列数由 `tcgen05.find_tmem_tensor_col_offset` 算出。`TmemOperandA`（[第 140-202 行](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/spec/tmem.py#L140-L202)）是 TMEM-resident 的 A 操作数（用于 `source="TS"` 的 MMA），它的 `view` 显式调用 `make_tmem_layout`，并处理 `swap_AB`（swap 时物理 A 其实是逻辑 `B.T`）。

**`TmemStruct`——多字段背靠背打包**：

```python
# quack/spec/tmem.py:245-266（节选）
class TmemStruct:
    """Named TMEM regions for a kernel, packed back-to-back in declaration order."""
    def __init__(self, **fields):
        self._fields = fields
        field_cols = {name: 0 if field is None else field.num_cols()
                      for name, field in fields.items()}
        offset = 0
        for name, num_cols in field_cols.items():
            self._offsets[name] = offset
            offset += num_cols
        num_cols = 32
        while num_cols < offset:
            num_cols *= 2          # 向上取整到 2 的幂
        max_cols = cute.arch.get_max_tmem_alloc_cols("sm_100")
        assert num_cols <= max_cols, ...
        self.num_cols = num_cols
```

[quack/spec/tmem.py:245-301](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/spec/tmem.py#L245-L301)：每个字段按声明顺序背靠背分配列偏移，总列数向上取整到 2 的幂（硬件分配要求），并断言不超 SM100 的最大可分配列数（512）。`bind(base_ptr)` 在取回的 TMEM 基址上把每个字段物化成视图。这是 SM100 内核「分配一次 TMEM、多个区域共用」的机制——和 SMEM 的 `SharedStorage` 结构体类比，但 TMEM 是列寻址、按列计数。

**TensorSpec 的 TMEM 侧**——`tmem_layout()` 与 `with_tmem()`：

```python
# quack/spec/tensor_spec.py:296-320（节选）
def tmem_layout(self):
    """Role-free flat TMEM storage layout for this spec. ..."""
    assert not self.in_rmem, ...
    assert self.rank == 2, ...
    rows, cols = self.storage_shape
    if self.cta_group == 2:
        assert rows in (64, 128), ...
        local_tmem_rows = 128          # 2CTA TS-A: 每-CTA M=64/128 都占满本地 128 lane
    else:
        local_tmem_rows = rows
    return spec_tmem.make_tmem_layout(self.dtype, (local_tmem_rows, cols), self.stage)
```

[quack/spec/tensor_spec.py:296-320](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/spec/tensor_spec.py#L296-L320)：TensorSpec 的 TMEM 存储布局是 role-free 的 `(rows, cols[, stage])`，与 SMEM 侧对称。`cta_group==2` 时，无论每-CTA M 是 64 还是 128，本地 TMEM 都用满 128 lane（per-CTA M=64 会被复制进全部 128 DP lane）。`with_tmem()`（[第 322-351 行](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/spec/tensor_spec.py#L322-L351)）把裸指针按此布局物化，`m64_partition='upper'` 可选另一半子分区。

> 注意 `TmemOperandA.view` 与 `TensorSpec.tmem_layout()` 的分工：前者是「新物化的物理 tcgen05 A tile」（`.T` 要真正变成 `(D,N)`），后者是「存储视图」（`.T` 复用同一份存储）。注释在 [第 198-202 行](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/spec/tmem.py#L198-L202) 明确警告不要混用。

#### 4.4.4 代码实践

**实践目标**：理解 TMEM 的列寻址与 2 的幂分配，手算一个累加器的 TMEM 占用。

**操作步骤**：

1. 打开 [quack/spec/tmem.py:43-98](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/spec/tmem.py#L43-L98)。设 `dtype=Float32`、`shape=(128, 128)`、`stage=1`：
   - `elems_per_col = 32//32 = 1`；`dp_stride = (1<<16)*1 = 65536`。
   - M=128 分支：布局 `((128,128,1), stride=(65536, 1, 0))`——128 行各占一个 DP lane，128 列连续，单级无 stage 步长。
2. 打开 [quack/spec/tmem.py:245-266](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/spec/tmem.py#L245-L266)。假设一个内核有两个字段，`num_cols` 分别为 96 和 160：`offset=256`，向上取整到 2 的幂仍是 256，≤512 容量断言通过。
3. 思考：若两个字段 `num_cols` 分别为 200 和 200，`offset=400`，取整到 512——刚好不超容量，但余量很小。

**需要观察的现象**：TMEM 容量是 512 列硬上限（`get_max_tmem_alloc_cols("sm_100")`），且分配必须 2 的幂。一个累加器占的列数由 MMA 指令决定（`find_tmem_tensor_col_offset`），而非简单等于逻辑 N。

**预期结果**：你能解释为什么 TMEM 地址是 `(dp<<16)|col`、为什么 bf16 视图下 DP 步长翻倍、以及 `TmemStruct` 为什么要把总列数取整到 2 的幂。若想确认 `get_max_tmem_alloc_cols` 的具体返回值，**待本地在 SM100 环境验证**。

#### 4.4.5 小练习与答案

**练习 1**：为什么 TMEM 字段的 footprint 用「列数」而不是「字节数」？

**答案**：TMEM 是列寻址存储（128 lane × 512 column，32-bit cell），每个字段横跨全部 128 lane，所以一个字段的空间占用由它占多少**列**决定，与 lane 维度无关。偏移加到（32-bit 类型的）TMEM 基址上即可。见 [第 18-26 行](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/spec/tmem.py#L18-L26)。

**练习 2**：M=64 时为什么行按 `(16,4)` 分组、映射到 `[0:16],[32:48],[64:80],[96:112]` 这四段 DP lane？

**答案**：tcgen05 的 M=64 MMA 使用「半子分区」——只用 128 lane 中的 64 个，但不是连续的 64 个，而是上述四段（每段 16 lane，共 64）。这是硬件 MMA 指令对 M=64 操作数的物理 lane 映射约定，布局必须与之匹配。`32*dp_stride` 的步长正是「跳 32 个 lane 到下一段」。见 [第 56-65 行](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/spec/tmem.py#L56-L65) 与 [第 91-94 行](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/spec/tmem.py#L91-L94)。

**练习 3**：`TmemStruct` 为什么把总列数向上取整到 2 的幂？

**答案**：TMEM 硬件分配要求列数是 2 的幂（分配粒度约束）。`while num_cols < offset: num_cols *= 2` 实现这一取整，随后断言不超过 512 列容量上限。见 [第 258-265 行](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/spec/tmem.py#L258-L265)。

---

## 5. 综合实践

**任务**：用一个完整的「声明式 GEMM 操作数」场景，把本讲四个模块串起来——从 `TensorSpec` 声明，到 TMA 加载、SMEM 视图、MMA 构造、TMEM 累加器，画出整条「描述符派生链」。

**背景**：假设你要在 SM100 上写一个 2-CTA 的密集 GEMM，A 操作数 `(M=512, K=64)` bf16、3 级流水线。请按下列步骤完成一份**源码阅读 + 数据流图**的综合实践（纯阅读型，无需 GPU）：

1. **声明存储**：构造 `TensorSpec(dtype=BF16, shape=(512,64), stage=3, layout=ROW_MAJOR, cta_group=2)`。手算 `full_storage_shape=(512,64)`、`storage_shape=(256,64)`（[4.1.3](#413-源码精读)）。
2. **TMA 加载**：调用 `with_tma_load(gA)`。说明 `_make_tma_atom` 因 `cta_group==2` 走 `_sm100_dense_tma_flat_cta_v_map`，CTA0 拥有 GMEM 行 `0..127` 与 `256..383`，SMEM 描述符保持 flat（[4.2.3](#423-源码精读)）。
3. **SMEM 视图**：`with_smem(sA_field)` 后，`smem_layout()` 经 `smem.py` 选 `tcgen05` atom、coalesce 给出 role-free 存储视图；`MatmulSpec.smem_view_A` 另造 role-nested 的 `partition_A` 视图（[4.3.3](#433-源码精读)）。
4. **MMA 构造**：`(A_spec @ B_spec).tiled_mma(source="SS")` 走 SM100 分支，`cta_group_enum=TWO`，`n_inst` 由 N 决定（[4.3.3](#433-源码精读)）。
5. **TMEM 累加器**：`BoundMMASm100.acc(tmem_ptr)` 在 TMEM 基址物化累加器，布局由 `tiled_mma.partition_shape_C` 决定；多个 TMEM 字段经 `TmemStruct` 打包、总列数取 2 的幂、断言 ≤512（[4.4.3](#443-源码精读)）。
6. **画图**：画一张数据流图，标注 `shape (完整) → storage_shape (每-CTA) → cta_v_map (TMA) / smem_layout (SMEM) / tiled_mma (MMA) / tmem_layout (TMEM)`，并在每个箭头旁注出「role-agnostic 还是 role-aware」。

**交付物**：

- 一张数据流图（手画或文字描述均可）。
- 一段话总结：哪些派生是 role-agnostic（SMEM 布局、TMA atom、TMEM 存储布局），哪些是 role-aware（MMA 主序、role-nested SMEM 视图、累加器布局）。

**预期结果**：你能清晰说出 spec 层的设计主轴——**用一份 role-agnostic 的存储描述（TensorSpec）派生出所有 role-agnostic 的硬件描述符（TMA/SMEM/TMEM 存储侧），把 role 信息推迟到 MatmulSpec 才介入（MMA 主序与 role-nested 视图）**。这张图也是你日后阅读 `tensor_spec.py` 全文的导航。

## 6. 本讲小结

- `quack/spec/` 是一套**声明式描述符层**原型，用 `TensorSpec`（dtype/shape/stage/layout/cta_group）声明一个分阶段 tile，派生出 SMEM 布局、TMA atom、MMA 配置、TMEM 布局——核心定语是 **storage-only and role-agnostic**。
- `MatmulSpec`（`A @ B`）是 role-aware 的那一层：派生 `tiled_mma`、操作数主序、role-nested 的 SMEM/TMEM 视图；`cta_group` 是操作数的存储属性，由 MMA 构造读回。
- `spec/tma.py` 用 **CTA-value map** 构造 TMA CopyAtom：张量的 shape/stride/swizzle 编码进 `gmem_tensor.value` 与 `smem_layout`，CTA 所有权编码进 `cta_v_map`；SM100 2-CTA 时 peer CTA 拥有非连续的「指令面板」，缝隙放进 map 而 SMEM 描述符保持 flat。
- `spec/mma.py` 的 `make_tiled_mma_for_arch` 按 `arch.major` 分发到 WGMMA（SM90）/ warp MMA（SM8x,SM12x）/ tcgen05（SM100）；`atom_layout_mnk` 是 SM90 coop/pingpong 旋钮，`cta_group` 是 SM100 2-CTA 旋钮；`operand_leading_atom`/`resolve_mma_inst_k` 是共享的几何真相源。
- `spec/tmem.py` 描述 TMEM 的列寻址布局（地址 `(dp<<16)|col`），M=128 用满 128 lane、M=64 走 `(16,4)` 半子分区；`TmemStruct` 把多字段背靠背打包、总列数取 2 的幂、断言 ≤512。
- **TensorSpec ≠ fake tensor**：前者是运行期 tile 的存/搬/算描述，后者是编译期符号张量驱动 `cute.compile`，二者互补。
- **截至当前 HEAD，`quack/spec/` 尚未被 `quack/spec/` 之外的模块导入**，是演进中的原型；生产内核仍直接用底层 `cutlass.utils.*_helpers`。本讲重在理解其设计思想与各 helper 源码。

## 7. 下一步学习建议

- **回看生产内核的「非 spec」写法**：对比 [quack/gemm_sm90.py](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_sm90.py) 与 [quack/gemm_sm100.py](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_sm100.py) 里直接调用 `sm90_utils`/`sm100_utils` 构造 TMA、`TiledMma`、SMEM 布局的代码，体会 spec 层想抽象掉的「散装样板」到底是什么——这能加深你对 spec 层价值的理解。
- **进入可组合 epilogue 系统（u6-l1 起）**：spec 层的 `BoundMMASm100.acc` / `t2r_C` 产出的累加器与寄存器片段，正是 epilogue 系统 `EpiOp` 的输入。建议接着读 u6-l1（ComposableEpiMixin 与 EpiOp 生命周期）。
- **若想跟踪 spec 层的演进**：用 `git log --oneline -- quack/spec/` 观察这个原型子包的提交历史；它的 API 文档自承「could change rapidly」，未来可能被接入 `quack/gemm_runtime/` 的主机侧计划，值得留意。
- **深入 Blackwell TMEM 细节**：若你有 SM100 硬件，可在内核里用 `cute.printf` 打印 `tcgen05.find_tmem_tensor_col_offset` 的返回值与 `TmemStruct.num_cols`，验证 4.4 的手算结果（本地验证）。
