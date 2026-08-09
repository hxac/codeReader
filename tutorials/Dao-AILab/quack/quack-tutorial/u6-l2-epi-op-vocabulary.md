# u6-l2 EpiOp 词汇表

## 1. 本讲目标

上一讲（u6-l1）我们看清了可组合 epilogue 的「调度框架」：`ComposableEpiMixin` 在固定时刻依次调用每个 `EpiOp` 的生命周期钩子，并用「声明是全集、执行是子集」把未激活的 op 从编译产物里过滤掉。但当时我们只把 `EpiOp` 当成一个抽象基类，没有回答最关键的问题：

> 一个 GEMM epilogue 到底有哪些种「张量资源」？标量、广播向量、输出块、归约结果——它们各自需要什么样的加载/存储/同步协议？

本讲就来填上这张「词汇表」。读完本讲你应当能够：

1. 说出 `Scalar`、`RowVecLoad`/`ColVecLoad`、`TileStore`、`DStore`、`TileLoad`、`ColVecReduce`/`RowVecReduce` 各自负责哪一种张量资源。
2. 解释标量与广播向量是如何经 `cp.async` 灌进 smem 再广播到寄存器的。
3. 理解 tile 级存储 op 与 `DStore` 的本质区别——为什么主输出 D 的主机侧管线「由内核拥有」而不像其它输出那样挂在 `_epi_ops` 上。
4. 描述 `ColVecReduce` 跨 lane / 跨warp 的两阶段蝶形归约协议，以及它如何借助 `swap_shuffle_reduce` 把复杂度从 \(O(E\log_2 L)\) 降到 \(O(E)\)。

本讲只读一个文件——[quack/epilogue/ops.py](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/ops.py)，但它有 3000 多行，是整个 epilogue 系统的核心。

## 2. 前置知识

本讲默认你已经掌握以下内容（对应前置讲义）：

- **GEMM 主循环与 epilogue 驱动**（u5-l1）：知道 accumulator（累加器）、`tRS_rD`（D 的寄存器片段）、`store_convert` / `store_r2s` 这些 store 钩子，以及 `gemm_base.epilogue` 这个驱动循环的存在。
- **copy_utils 与 cp.async / TMA**（u3-l1）：知道 `tiled_copy`、`cp.async`、TMA 两条异步拷贝路径，以及 `predicate_k`、`fill_oob` 边界处理。
- **layout_utils 的 zero-stride 布局**（u3-l2）：知道 `convert_layout_zero_stride` 如何把「真实存储 + 广播」重打包成两模布局，让行/列向量在寄存器里正确累加。这是本讲 VecReduce 的数学基础。
- **EpiOp 生命周期与 ComposableEpiMixin**（u6-l1）：知道 `_epi_ops` 是类级 schema、`_filter_epi_ops` 把它影子覆盖成激活集，以及 `EpiContext` 这个共享工具箱。

几个反复出现的术语，先用一句话锚定：

- **op（操作）/ EpiOp**：一种张量资源在 epilogue 生命周期里的「自管单元」，自己声明主机侧 schema、自己分配 smem、自己实现设备侧钩子。
- **value port / fn_port**：op 参与「函数式 epilogue」逐元素数据流的方式标签（`row`/`col`/`tile`/`scalar`/`value`/`apply`/`sink`）。这是 op 的「词性」。
- **CTA tile / epi tile**：一个 CTA 负责的输出块（如 128×128）会被 epilogue 切成若干更小的 **epi tile**（如 16×128）逐子块处理。
- **lane / warp**：一个 warp 有 32 个 lane；多个 warp 组成一个 CTA。归约协议的关键就是把数据在 lane 间、warp 间搬运、合并。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [quack/epilogue/ops.py](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/ops.py) | **本讲主角**。定义全部 `EpiOp` 词汇：基类 `EpiOp`、`Scalar`、`VecLoad`/`RowVecLoad`/`ColVecLoad`、`TileStore`/`DStore`/`TileLoad`、`VecReduce`/`ColVecReduce`/`RowVecReduce`。 |
| [quack/epilogue/mixin.py](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/mixin.py) | `ComposableEpiMixin`：把 op 组合成 epilogue 钩子；本讲引用它的 `_filter_epi_ops`、`_epi_store_ops`、`epi_setup_aux_out`、两阶段 flush 的 `epi_end_loop`。 |
| [quack/gemm_base.py](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_base.py) | `GemmBase.epilogue`：实际驱动 store_convert/store_r2s 的设备侧循环；本讲用它说明 `DStore` 如何被装配进 `store_ctxs`。 |
| [quack/gemm_default_epi.py](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_default_epi.py) | `GemmDefaultEpiMixin`：一个「真实」epilogue 如何用具体 op 实例声明 `_epi_ops`，是本讲的最佳范例。 |
| [quack/reduce.py](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/reduce.py) | `swap_shuffle_reduce`：VecReduce 蝶形归约的高效实现。 |

## 4. 核心概念与源码讲解

### 4.1 EpiOp 基类协议与 fn_port 值端口

#### 4.1.1 概念说明

`EpiOp` 是所有 epilogue 操作的抽象基类。它的设计哲学是「**一种张量资源 = 一个自管单元**」：

- 一个标量（如 α、β）是一种资源 → `Scalar`。
- 一条广播到每行的向量（如 bias）是一种资源 → `RowVecLoad`。
- 一块要写回显存的输出（如激活后的 postact）是一种资源 → `TileStore`。
- 一个要对某条轴做归约的结果（如每行的和）是一种资源 → `ColVecReduce` / `RowVecReduce`。

每个 op 对外提供两套接口：**主机侧 schema**（描述「我的参数长什么样、要多少 smem」）和**设备侧生命周期**（描述「在内核里每个阶段做什么」）。框架（`ComposableEpiMixin` 和 `gemm_base.epilogue`）只负责在正确的时刻调用这些钩子，绝不 `isinstance` 分发——它只是按 op 顺序遍历。

每个 op 还有一个 **`fn_port`** 类属性，它是 op 的「词性」：声明这个 op 如何接入「函数式 epilogue」（`@gemm_epilogue` fn 前端，u6-l4 会详讲）的逐元素数据流。

#### 4.1.2 核心流程

一个 op 从声明到执行的完整旅程：

```text
类级声明:  _epi_ops = (Scalar("alpha"), RowVecLoad("bias"), ...)
              │
              ▼  (u6-l1: _filter_epi_ops 过滤掉 arg 为 None 的)
激活集:    _epi_ops = (仅含本次调用实际传入张量的 op)
              │
   ┌──────────┴─────────── 主机侧 schema ──────────────────┐
   ▼                                                        ▼
host_arg_key    从用户 torch 张量抽出可哈希的编译键(dtype, ndim, ...)
host_fake_arg   从键重建编译期 fake 张量 (用 sym_int)
host_call_arg   每次调用把 torch 张量转成运行期参数 (指针/标量)
param_fields    声明 EpilogueParams 里的字段 (供 make_dataclass 自动生成)
to_params       把 args 转成 params dict (含 TMA atom、smem 布局等烘焙产物)
smem_bytes      报告自己需要多少 smem (EpiSmemBytes: unstaged/d_stage/c_stage)
smem_struct_field / get_smem_tensor   声明并取出自己的 smem 缓冲
              │
   ┌──────────┴─────────── 设备侧生命周期 ─────────────────┐
   ▼                                                        ▼
begin            每 CTA tile 一次性初始化, 返回 state
begin_loop       每 epi 子块取出一份数据 (广播向量片段 / 归约槽位)
end_loop_stage   flush 第 1 阶段: warp 内归约 + 写自己的 smem
end_loop_finish  flush 第 2 阶段 (共享 barrier 之后): 跨 warp 合并 + 写 gmem
end              所有子块结束后收尾
```

#### 4.1.3 源码精读

`EpiOp` 基类把上述两套接口都定义成可重写的钩子，默认实现大多是「空操作」，子类按需覆盖。[quack/epilogue/ops.py:298-521](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/ops.py#L298-L521) 定义了基类。其中最有信息量的是 `fn_port` 的文档串：

[quack/epilogue/ops.py:301-321](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/ops.py#L301-L321) — 这是 op 的「词性表」。它把 op 分成 7 种端口：

| fn_port | 含义 | 代表 op |
|---------|------|---------|
| `"row"` | fn 收到一个逐元素的「行广播值」 | `RowVecLoad` |
| `"col"` | fn 收到一个逐元素的「列广播值」 | `ColVecLoad` |
| `"tile"` | fn 收到一个与累加器同形的 tile 输入 | `TileLoad` |
| `"scalar"` | fn 收到一个标量 | `Scalar` |
| `"value"` | 自定义「值源」op，fn 收到逐元素值 | `GroupedColStatsBase` |
| `"apply"` | fn 收到一个**可调用对象**（如 `y=rope(acc)`） | rotary（领域 op） |
| `"sink"` | fn **返回**值，前端收集后交给 op 的 `fn_sink_flush` | `VecReduce` |
| `None` | 不参与函数式前端（仅供手写 mixin） | `DStore` |

注意：这套 `fn_port` 协议服务于「函数式 epilogue」（u6-l4），而本讲重点的「资源生命周期」（smem/TMA/flush）是另一套正交的协议。一个 op 可以同时拥有两者——例如 `ColVecLoad` 既声明 `fn_port="col"`，又实现了 `begin`/`begin_loop` 的 cp.async 加载。

主机侧 schema 钩子的默认实现见 [quack/epilogue/ops.py:367-401](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/ops.py#L367-L401)。`host_arg_key` 是其中最关键的：它把用户的 torch 值压成一个**可哈希、可 pickle 的描述符**，这个描述符既是 JIT 磁盘缓存键的一部分，也用来在 `host_fake_arg` 里重建编译期 fake 张量。默认实现 `return (torch2cute_dtype_map[value.dtype], value.ndim)`——只看 dtype 和维度数。**返回 `None` 表示这个 op 缺席，会被编译产物剔除**（这是「声明全集、执行子集」的编译键层落点）。

设备侧生命周期的默认实现见 [quack/epilogue/ops.py:437-515](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/ops.py#L437-L515)：`is_tile_load`/`is_tile_store` 默认 `False`，`begin`/`end` 默认空操作。一个「纯标量」op 不需要 smem、不需要 TMA、不需要 flush，几乎全部沿用默认实现。

`config_key` 也很重要（[quack/epilogue/ops.py:340-360](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/ops.py#L340-L360)）：它是「fail-closed」的——如果一个 op 有影响代码生成的实例属性却没实现 `config_key`，基类会**主动抛错**，防止两个语义不同的 epilogue 在 JIT 缓存里撞键。

#### 4.1.4 代码实践

**实践目标**：用真实 epilogue 的 `_epi_ops` 声明，把每个 op 映射到它的 `fn_port` 词性。

**操作步骤**：

1. 打开 [quack/gemm_default_epi.py:70-83](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_default_epi.py#L70-L83)，阅读 `GemmDefaultEpiMixin._epi_ops`：

   ```python
   _epi_ops = (
       Scalar("alpha"),
       Scalar("beta"),
       Scalar("sr_seed", dtype=Int32),
       RowVecLoad("mRowVecBroadcast"),
       ColVecLoad("mColVecBroadcast"),
       BlockScaleFactorStore("mSFD"),
       BlockScaleFactorStore("mSFDCol", direction="col"),
   )
   ```

2. 对照本讲 4.1.3 的词性表，给每个 op 标注 `fn_port`。

**需要观察的现象**：注意 `BlockScaleFactorStore`（量化输出 codec，[quack/epilogue/quantize_out.py:135](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/quantize_out.py#L135)）没有出现在 fn_port 表里——它是一个「量化编解码器」，附属于某个 store op 而非独立的值端口。

**预期结果**：

| op | fn_port |
|----|---------|
| `Scalar("alpha")` | `"scalar"` |
| `Scalar("beta")` | `"scalar"` |
| `Scalar("sr_seed", dtype=Int32)` | `"scalar"` |
| `RowVecLoad("mRowVecBroadcast")` | `"row"` |
| `ColVecLoad("mColVecBroadcast")` | `"col"` |
| `BlockScaleFactorStore(...)` | `None`（codec，不是值端口） |

（运行命令本身不必要，这是源码阅读型实践。）

#### 4.1.5 小练习与答案

**练习 1**：为什么 `EpiOp.config_key` 在发现「有未声明的实例属性」时要主动抛错，而不是默默忽略？

**参考答案**：因为 `config_key` 是持久化 JIT 缓存的语义指纹的一部分。如果一个有状态 op（如带 `gated=True` 的 `TileStore`）漏写了 `config_key`，那么「gated」和「非 gated」两个语义不同的 epilogue 会算出相同的缓存键，导致第二次调用错误地命中第一次的 cubin——一个会静默产出错误结果的 bug。fail-closed 把这种隐患变成显式的启动期错误。

**练习 2**：`host_arg_key` 返回 `None` 和返回一个非空描述符，分别意味着什么？

**参考答案**：返回 `None` 表示该 op 的参数缺席（用户没传这个张量），op 会被从编译产物里剔除——它既不进入编译键、也不进入运行期参数。返回非空描述符表示 op 激活，该描述符进入磁盘缓存键、并在 `host_fake_arg` 里被还原成编译期 fake 张量。

---

### 4.2 Scalar 与广播向量加载 op

#### 4.2.1 概念说明

epilogue 里有两类「小」资源，本讲合在一节讲，因为它们都不写显存、只读：

- **`Scalar`**：一个标量值（如 α、β）或一个指向设备的标量指针。每 tile 加载一次，不占 smem。典型用途是 GEMM 的 `D = α(A@B) + βC` 里的 α、β。
- **`VecLoad` 家族**：一条「广播向量」——`RowVecLoad` 是形状 `(N,)` 的行向量，广播到每一行（如行偏置 bias）；`ColVecLoad` 是形状 `(M,)` 的列向量，广播到每一列。它们通过 `cp.async` 灌进 smem，再在 `begin_loop` 里广播到寄存器片段。

二者的关键差异：`Scalar` 零 smem、零异步拷贝；`VecLoad` 需要一块 smem 缓冲、需要 `cp.async` 提交/等待的 fence。

#### 4.2.2 核心流程

**Scalar 的三态模式**。`Scalar.host_arg_key` 把标量值编码成三种**编译期模式**之一（[quack/epilogue/ops.py:564-570](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/ops.py#L564-L570)）：

| 模式 | 含义 | host_call_arg 返回 | 对应 cubin |
|------|------|-------------------|-----------|
| `0 / "absent"` | 缺席（op 被编译掉） | `None` | 不含这一项的特化版本 |
| `1 / "immediate"` | 主机常量（如 α=1.0 烘焙进内核） | `dtype(value)` | 标量已是字面量 |
| `2 / "pointer"` | 设备指针（每次调用可能变） | `value.data_ptr()` | 内核里发一次 `load` |

把「缺省/常量/指针」编码进**编译键**而不是只进**调用参数**，意味着三种形式对应结构不同的 cubin——常量形式能让编译器做常量折叠（如 α=1 直接消去乘法）。

**VecLoad 的 gmem→smem→register 三段式**（基类 [quack/epilogue/ops.py:604-717](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/ops.py#L604-L717)）：

```text
begin (每 tile 一次):
  用 tiled_copy_1d(is_async=True) 把整条向量从 gmem cp.async 到 smem
  用 partition_for_epilogue 把 smem 张量按 (tile_M, tile_N) 切片,
     套上广播步幅 (0,1) 或 (1,0)
  预分配复用寄存器 tDrV_cvt

begin_loop (每 epi 子块):
  只在「第一次扫到非广播轴」时做 smem→register 拷贝 (避免重复加载):
     RowVec: epi_m_major 时 epi_coord[0]==0 才加载
     ColVec: 非 m_major 时 epi_coord[1]==0 才加载
  把寄存器值转换到 acc_dtype 返回
```

广播步幅是精髓：`RowVecLoad` 用 `(0,1)`——M 维步长 0，意味着所有行共享同一份 N 向量数据；`ColVecLoad` 用 `(1,0)`——N 维步长 0，所有列共享同一份 M 向量。这正好是 u3-l2 讲过的 zero-stride 广播在 epilogue 里的直接应用。

#### 4.2.3 源码精读

`Scalar.begin` 极简，[quack/epilogue/ops.py:597-601](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/ops.py#L597-L601) 只是把参数解包成一个标量值（无论是 immediate 还是 pointer 都由 `utils.load_scalar_or_pointer` 统一处理）：

```python
@cute.jit
def begin(self, gemm, param, smem_tensor, ctx):
    if const_expr(self.dtype is not None):
        return utils.load_scalar_or_pointer(param, dtype=self.dtype)
    return utils.load_scalar_or_pointer(param)
```

注意它**没有 smem**（`smem_bytes` 沿用基类的空 `EpiSmemBytes`），也没有 `needs_async_fence`——标量不需要异步同步。`Scalar` 因此是「最轻」的 op。

`VecLoad.begin` 是 cp.async 加载的主体，[quack/epilogue/ops.py:669-700](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/ops.py#L669-L700)。关键片段：

```python
thr_copy = copy_utils.tiled_copy_1d(
    dtype, ctx.num_epi_threads, num_copy_elems, is_async=True
).get_slice(ctx.tidx)
mVec = self._get_gmem_vec(param, ctx)              # 取出本 tile 对应的向量段
gVec = cute.local_tile(mVec, (tile_dim,), (coord_idx,))
tVgV = thr_copy.partition_S(gVec)                   # gmem 源分区
tVsV = thr_copy.partition_D(smem_tensor)            # smem 目标分区
...
for m in cutlass.range(cute.size(tVsV.shape[1]), unroll_full=True):
    if tVcV[0, m] < tile_dim:
        pred = cute.make_rmem_tensor(1, Boolean)
        pred[0] = tVcV[0, m] < limit                # 边界谓词
        cute.copy(thr_copy, tVgV[None, m], tVsV[None, m], pred=pred)
```

这正是 u3-l1 的 `tiled_copy` + `cp.async` + 边界谓词三件套。`limit` 用来处理「向量长度不是 tile 整数倍」的尾巴。注意 `VecLoad.needs_async_fence` 返回 `True`（[ops.py:656-657](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/ops.py#L656-L657)），所以 `mixin.epi_begin` 末尾会为所有 `VecLoad` 发一次 `cp_async_commit_group` + `wait_group(0)` + barrier（见 u6-l1 的 `epi_begin`）。

`VecLoad.begin_loop` 用 `should_load` 门控避免重复加载，[quack/epilogue/ops.py:702-717](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/ops.py#L702-L717)。以 `RowVecLoad`（M 广播）为例：当 epilogue 沿 M-major 遍历时，每个 epi_m 都对应同一份行向量，只需在 `epi_coord[0]==0` 时加载一次：

```python
if const_expr(self.dim == 1):
    if const_expr(gemm.epi_m_major):
        should_load = epi_coord[0] == 0
...
if should_load:
    tDsV_cur = ...[None, None, None, epi_coord]
    tDrV = cute.make_rmem_tensor(...)
    cute.autovec_copy(cute.filter_zeros(tDsV_cur), cute.filter_zeros(tDrV))
    tDrV_cvt.store(tDrV.load().to(gemm.acc_dtype))
return tDrV_cvt
```

`ColVecLoad`（[ops.py:727-786](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/ops.py#L727-L786)）覆盖了 `_get_gmem_vec` 与 `begin` 来支持 **varlen_m（变长序列）**：通过 `cute.domain_offset` 按 `cu_seqlens_m` 偏移到当前序列，并用 `varlen_manager.len_m` 计算边界 `limit`。这是 `ColVecLoad` 比基类多出的能力。

还有一个有意思的主机侧细节：`VecLoad.epi_m_major_score`（[ops.py:659-661](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/ops.py#L659-L661)）返回 `4 if dim==1 else -1`——`RowVecLoad` 强烈偏好 M-major（正值），`ColVecLoad` 偏好 N-major（负值）。这个分数被 `ComposableEpiMixin.resolve_epi_m_major` 汇总，决定 epilogue 子块的遍历顺序，从而让「只需加载一次」的门控最大化命中。

#### 4.2.4 代码实践

**实践目标**：跟踪 `RowVecLoad` 的一次完整数据旅程，标注它如何把一条 `(N,)` 向量广播成 `(tile_M, tile_N)` 寄存器片段。

**操作步骤**：

1. 在 [quack/epilogue/ops.py:720-723](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/ops.py#L720-L723) 确认 `RowVecLoad` 的 `dim = 1`、`fn_port = "row"`。
2. 在 [ops.py:628-630](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/ops.py#L628-L630) 读 `_broadcast_stride`：`dim==1` 返回 `(0, 1)`。
3. 在 `begin`（[ops.py:669-700](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/ops.py#L669-L700)）里找到把 smem 张量套上 `(tile_M, tile_N, stride=(0,1))` 布局的那一行（约 689-694 行），确认 M 维步长为 0。
4. 在 `apply_linear_epilogue`（[gemm_default_epi.py:62-64](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_default_epi.py#L62-L64)）看消费端如何把广播片段加到累加器上。

**需要观察的现象**：广播不是真的复制数据，而是靠**步长为 0 的布局**让同一个寄存器/存储单元被多个逻辑坐标引用——这与 u3-l2 的 zero-stride 完全一致。

**预期结果**：一条 `N` 元素向量在 smem 里只占 `tile_N × sizeof(dtype)` 字节（`_tile_size` = `cta_tile_shape_mnk[1]`，见 [ops.py:638-641](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/ops.py#L638-L641)），广播到 `tile_M` 行不增加任何存储。（待本地验证：可在 `begin` 处加 `cute.printf` 打印 `cute.size(smem_tensor)` 与 `tDsV` 的形状。）

#### 4.2.5 小练习与答案

**练习 1**：`Scalar` 的 immediate 模式和 pointer 模式分别适合什么场景？为什么不给所有标量都用 pointer？

**参考答案**：immediate 适合值在编译期就固定、且能触发常量折叠的场景（如 α=1.0 时整个乘法可被消去）；pointer 适合每次调用值都可能变、但又不想重新编译的场景（如运行时动态 α）。全用 pointer 会丧失常量折叠机会，且每次调用多一次设备 `load`，所以默认按值是否变化来选。

**练习 2**：`ColVecLoad` 的 `epi_m_major_score = -1`，`RowVecLoad` 是 `+4`。如果一个 epilogue 同时有 row bias 和 col bias，遍历顺序会偏向哪边？为什么 row 的权重更大？

**参考答案**：分数相加 `+4 + (-1) = +3 ≥ 0`，倾向 M-major。权重不对称是因为（注释说明）把 rowvec 留在寄存器里比 colvec 大约贵 4 倍寄存器，所以更愿意顺 row 的「便宜方向」遍历以省寄存器。

---

### 4.3 TileStore、DStore 与 TileLoad：tile 级存储与加载

#### 4.3.1 概念说明

本节是本讲的重心，也是本讲的代码实践任务所在。三种 tile 级 op 都处理「整块 M×N 张量」，但角色截然不同：

- **`TileStore`**：一块**辅助输出**（如激活后的 postact、辅助统计），经 TMA 存回显存。它**完整拥有自己的设备存储路径**：register→smem 拷贝、dtype 转换、（可选的）gated 折半、量化 codec、smem→gmem 的 TMA 拷贝。
- **`DStore`**：GEMM 的**主输出 D** 的设备存储路径。它和 `TileStore` 实现同一套 `store_convert` / `store_r2s` 钩子，但**没有主机侧钩子、不在 `_epi_ops` 里**——D 的 TMA atom、staged smem 布局、`sD` 结构字段由内核/SM 类直接构建。
- **`TileLoad`**：一块**辅助输入**（如需要和 D 做逐元素运算的另一个 tile），走和 GEMM 的 C 操作数一样的 staged gmem→smem→register 流水线，但暴露成 `epi_loop_tensors[name]` 而非 `tRS_rC`。

核心问题（也是实践任务）：**为什么 D 的主机侧管线由内核拥有，而其它输出交给 `_epi_ops`？**

#### 4.3.2 核心流程

存储路径由 `gemm_base.epilogue` 统一驱动（见 [quack/gemm_base.py:251-490](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_base.py#L251-L490)）。它先把所有存储输出组装成一个**存储上下文元组列表** `store_ctxs`：

```text
store_ctxs = (D 的上下文) + (各 TileStore 输出的上下文)
每个上下文 = (op, quant, tiled_copy_r2s, tRS_s, copy_fn, store_pred)
                     │       │                │       │         │
                     │       │                │       │         └─ 是否跳过本 tile 的存储(对称GEMM对角块)
                     │       │                │       └─ smem→gmem 的 TMA 拷贝函数
                     │       │                └─ 该输出的 smem 暂存区
                     │       └─ 可选的量化 codec (BlockScaleFactorStore)
                     └─ 拥有 store_convert / store_r2s 的 op (DStore 或 TileStore)
```

随后对每个 epi 子块，驱动循环对**所有输出**（D 在前，aux 在后）一视同仁地跑：

```text
1. (可选) quant.quantize(...)   # 量化 codec 对最终 fragment 做 rescale
2. op.store_convert(...)        # acc_dtype → 存储_dtype, 含随机舍入/寄存器重排
3. (acquire smem stage)
4. op.store_r2s(...)            # register → smem
5. (fence + barrier)
6. (TMA 把 smem 存回 gmem, 受 store_pred 门控)
```

关键设计：**D 和 aux 走完全相同的设备钩子序列**，差异只在「上下文从哪来」——D 的几件套（`tiled_copy_r2s, tRS_sD, copy_D`）由内核/SM 类直接构建，aux 的几件套由 `TileStore.store_setup` 构建。

#### 4.3.3 源码精读

先看 `TileStore`。它的 docstring 一句话点题：「Owns the whole device store path for its tensor」（[quack/epilogue/ops.py:796-824](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/ops.py#L796-L824)）。构造函数（[ops.py:826-840](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/ops.py#L826-L840)）携带四件可选配置：

- `epi_tile_fn`：自定义 epi tile 形状（如 gated 折半）。
- `gated`：把相邻 N lane 配对的「半 GEMM-N」输出（门控 MLP 的 postact）。
- `rounding`：本 op 专属的舍入模式（覆盖内核全局模式）。
- `store_pred_fn`：每 CTA tile 评估一次的布尔谓词，False 则跳过本 tile 的显存写（对称 GEMM 用它跳过对角块的镜像写）。
- `quant`：附属于本输出的量化 codec（`BlockScaleFactorStore`），声明处绑定，由驱动在 convert 前运行。

`TileStore` 的主机侧是完整的：`param_fields` 声明 5 个字段（[ops.py:903-912](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/ops.py#L903-L912)），`to_params`（[ops.py:914-945](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/ops.py#L914-L945)）调用 `setup_epi_tensor` 构造 TMA atom、staged smem 布局、epi tile，全部烘焙进 params。它的 smem 计入 **`d_stage`**（每个 store 阶段一份，[ops.py:947-955](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/ops.py#L947-L955)）。

设备侧的 `store_setup`（[ops.py:1043-1074](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/ops.py#L1043-L1074)）在每 CTA tile 调用一次，产出存储上下文元组的尾部 `(tiled_copy_aux_r2s, tRS_sAux, copy_aux, pred)`；`store_convert`（[ops.py:1076-1102](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/ops.py#L1076-L1102)）负责 dtype 转换 + gated 寄存器重排 + 随机舍入；`store_r2s`（[ops.py:1104-1108](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/ops.py#L1104-L1108)）做 register→smem 拷贝。

再看 `DStore`。它的 docstring 直接回答了我们的核心问题（[quack/epilogue/ops.py:1111-1124](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/ops.py#L1111-L1124)）：

> D's host plumbing stays kernel-owned — the TMA atom, the staged smem layout, and the `sD` struct field feed tile/stage sizing, split-K's workspace re-pointing, and `add_to_output` — so unlike TileStore this op has no host hooks and does not live in `_epi_ops`.

翻译过来就是：D 的 TMA atom、staged smem 布局、`sD` 结构字段，**参与 tile/stage 大小推算、split-K 工作区重指向、`add_to_output`**——这些是内核基础设施关心的、与 epilogue 组合无关的事情。如果把 D 也做成 `_epi_ops` 里的 op，就会造成「D 的几何信息要从 op 里反向查询给内核基础设施」的循环依赖。所以 D 的主机侧管线由内核（各 SM 类 + `gemm_base`）直接构建，只在**设备侧**把 `DStore()` 实例塞进 `store_ctxs`，让它和 aux 一样走 `store_convert`/`store_r2s`。

证据就在驱动里。[quack/gemm_base.py:296-308](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_base.py#L296-L308)：

```python
store_ctxs = self.epi_setup_aux_out(...)        # 来自 _epi_ops 里的 TileStore
if const_expr(has_D):
    store_ctxs = (
        (DStore(), self._epi_store_quant("D"), tiled_copy_r2s, tRS_sD, copy_D, None),
    ) + store_ctxs
```

注意 `DStore()` 是**现场 new 出来的、无状态单例**，它的几件套（`tiled_copy_r2s, tRS_sD, copy_D`）全是内核构建好的。`DStore` 类本身只有两个设备钩子：`store_convert`（[ops.py:1129-1146](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/ops.py#L1129-L1146)，内核全局舍入 + D 的随机舍入种子）和 `store_r2s`（[ops.py:1148-1158](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/ops.py#L1148-L1158)，含 SM90 fp32 的 pair-XOR STS.32 特殊路径）——没有任何 `host_arg_key`/`to_params`/`smem_bytes` 等主机钩子。

最后看 `TileLoad`。它是 `TileStore` 的「镜像」——一个辅助**输入**，走 C 操作数那条 staged 流水线。它的 smem 计入 **`c_stage`**（[ops.py:1252-1260](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/ops.py#L1252-L1260)），主机侧 schema（`host_arg_key`/`host_fake_arg`）直接复用 `TileStore` 的实现（[ops.py:1218-1220](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/ops.py#L1218-L1220)）。它的 `load_g2s_copy_fn`（[ops.py:1283-1302](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/ops.py#L1283-L1302)）返回 TMA producer 拷贝函数，`load_s2r`（[ops.py:1321-1327](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/ops.py#L1321-L1327)）发 smem→register 拷贝，`begin_loop` 返回的寄存器 tile 在 `epi_loop_tensors[name]` 里供自定义 epilogue 消费。`fn_port = "tile"` 让它也能被函数式前端使用。

#### 4.3.4 代码实践（本讲核心实践任务）

**实践目标**：对比 `TileStore` 与 `DStore`，亲手验证「D 的主机侧管线由内核拥有」这一论断，并解释原因。

**操作步骤**：

1. **看 D 不在 `_epi_ops` 里**。打开 [quack/gemm_default_epi.py:71-83](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_default_epi.py#L71-L83)，确认 `_epi_ops` 里只有 `Scalar`/`RowVecLoad`/`ColVecLoad`/`BlockScaleFactorStore`，**没有**任何代表 D 的 store op。

2. **看 DStore 无主机钩子**。在 [quack/epilogue/ops.py:1111-1158](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/ops.py#L1111-L1158) 通读 `DStore` 类体，确认它只定义了 `__init__`（写死 `name="D"`）、`store_convert`、`store_r2s`，**没有** `host_arg_key`/`host_fake_arg`/`to_params`/`param_fields`/`smem_bytes`/`smem_struct_field`/`get_smem_tensor`。

3. **对比 TileStore 的主机钩子**。在 [quack/epilogue/ops.py:903-978](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/ops.py#L903-L978) 确认 `TileStore` 同时拥有这 7 个主机钩子——它「自给自足」地构造 TMA atom 和 smem 布局。

4. **看 D 的几件套从哪来**。在 [quack/gemm_base.py:305-308](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_base.py#L305-L308) 看 D 的存储上下文：`tiled_copy_r2s`、`tRS_sD`、`copy_D` 是 `epilogue` 方法的**形参**，由各 SM 内核类（如 `GemmSm90`/`GemmSm100`）在调用 `epilogue` 前构建并传入。

5. **回答 why**。结合 [ops.py:1111-1124](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/ops.py#L1111-L1124) 的 docstring，列出 D 的主机侧资源「喂给了哪些内核基础设施」。

**需要观察的现象**：D 的几何信息（TMA atom、smem 布局）参与的事情——tile/stage 大小推算、split-K 工作区重指向、`add_to_output`——全部是**内核级**关注点，而非「epilogue 组合」关注点。

**预期结果（参考答案）**：

> D 是 GEMM 的主输出，它的 TMA descriptor、staged smem 布局、`sD` 结构字段在内核启动前就被各 SM 类用来推算 tile/stage 大小、为 split-K 分配 partials 工作区并做指针重指向、以及实现 `add_to_output`（原地累加到用户 D）。这些依赖发生在 epilogue 组装**之前**，且与「本次 epilogue 有哪些 op」无关。若把 D 也做成 `_epi_ops` 里的 op，内核基础设施就得反向查询 op 才能拿到 D 的几何信息，形成循环依赖。因此 D 的主机侧管线由内核直接拥有；`DStore` 仅作为设备侧无状态对象，让 D 和 aux 共享同一套 `store_convert`/`store_r2s` 钩子和同一个量化 seam。aux 输出（postact 等）没有这些内核基础设施依赖，是「纯 epilogue」资源，所以完整地住在 `_epi_ops` 里。

#### 4.3.5 小练习与答案

**练习 1**：`TileStore` 和 `TileLoad` 的 smem 分别计入 `EpiSmemBytes` 的哪个分量？为什么不同？

**参考答案**：`TileStore` 计入 `d_stage`（每个 D/store 阶段一份），`TileLoad` 计入 `c_stage`（每个 C/load 阶段一份）。因为它们走两条不同的 staged 流水线——store 路径与 D 共享 smem stage，load 路径与 C 操作数共享 smem stage。分开记账让 `_compute_stages` 能独立决定 store/load 各开几级缓冲。

**练习 2**：`TileStore` 的 `gated=True` 会改变什么？为什么 SM90 上要求 `tile_N % 32 == 0`？

**参考答案**：`gated` 把输出 N 维折半（`_gated_epi_tile_fn`），并触发 STSM 寄存器重排（`permute_gated_Cregs_b16`/`_f32`），让一对门控输出（gate/up）按交错顺序免拷贝读取。SM90 的 STSM 存储指令按 16 位 lane 寻址，需要 `tile_N` 是 32 的倍数才能让重排后的 lane 覆盖折半的 tile。

---

### 4.4 VecReduce 行列归约与蝶形协议

#### 4.4.1 概念说明

前面三类 op 都是「逐元素」资源——输入/输出与累加器 tile 逐元素对齐。`VecReduce` 家族则负责**沿某条轴做归约**，产出一个向量：

- **`ColVecReduce`（dim=0）**：沿 **N 轴**归约，产出形状 `(M,)` 的列向量（如每行的和、每行的 max）。每个 CTA tile 产出 `(tile_M,)` 个 partial，最终在主机侧 `host_finalize` 把多个 N-tile 的 partial 求和。
- **`RowVecReduce`（dim=1）**：沿 **M 轴**归约，产出形状 `(N,)` 的行向量（如每列的和）。

`combine` 选择归约算子：`"add"`（默认，单位元 0）、`"max"`（单位元 −∞）、`"max_abs"`（最大绝对值）。`fn_port = "sink"` 表示它是函数式前端的「汇」——fn 把逐元素值返回，由 `fn_sink_flush` 折叠进寄存器累加器。

本节的难点（也是实践任务第二部分）是它的**两阶段蝶形归约协议**：先把每行的 partial 在 warp 内沿 N 轴归约，再（如果多个 warp 沿 N 排列）经 smem 做跨 warp 合并。

#### 4.4.2 核心流程

`ColVecReduce` 在每个 CTA tile 内对一个 `(tile_M, tile_N)` 块沿 N 归约。线程几何由 epilogue 的 `tiled_copy` 决定：32 个 lane 和若干 warp 被铺成 `(lanes_in_M, lanes_in_N) × (warps_in_M, warps_in_N)`。归约分三层：

```text
层 0 — 寄存器累加 (在 epi_visit_subtile 里, 由用户/fn 驱动):
   每个 lane 在自己拥有的 N 元素上累加 → tDrReduce (寄存器)

层 1 — warp 内沿 N lane 蝶形 (end_loop_stage, flush 第 1 阶段):
   若 lanes_in_N > 1, 用 shuffle 蝶形把同行的 lane partial 合并:
     优化路径 (swap_shuffle_reduce): 复杂度 O(E), 结果分布在不同 lane
     退化路径 (shuffle_sync_bfly): 复杂度 O(E·log₂(lanes_in_N)), 结果只在 leader lane
   (若 warps_in_N > 1) 把 partial 写进 smem 交换区 sExch[row, warp_n-1, k]

   ─── 驱动发 1 次共享 epilogue_barrier (所有 reduce op 共用, 非 per-sink) ───

层 2 — 跨 warp 合并 + 写 gmem (end_loop_finish, flush 第 2 阶段):
   warp_n==0 的 lane 从 smem 读其它 warp 的 partial, merge
   把最终标量写回 gmem 的列向量
```

蝶形的代价对比：朴素 butterfly 每个元素做 \(\log_2(\text{lanes\_in\_N})\) 轮 shuffle-merge，总开销 \(O(E\cdot\log_2 L)\)；`swap_shuffle_reduce`（u2-l4 / [quack/reduce.py:371](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/reduce.py#L371)）通过「交换而非复制」让每轮把不同切片分到不同 lane，最终每个 lane 只持有自己那一片结果，总开销 \(O(E)\)，且 gmem 写入天然分散在多个 lane 上而非 leader 串行写。

#### 4.4.3 源码精读

`VecReduce` 基类（[quack/epilogue/ops.py:1438-1638](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/ops.py#L1438-L1638)）定义协议骨架。`fn_sink_flush`（[ops.py:1531-1540](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/ops.py#L1531-L1540)）是函数式前端的汇入口：先（可选）做 OOB 掩码，再调 `colvec_reduce_accumulate` 或 `rowvec_reduce_accumulate` 把一个 fragment 折叠进寄存器累加器 `tDrReduce`。这两个 accumulate 函数（[ops.py:1341-1435](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/ops.py#L1341-L1435)）正是 u3-l2 的 `convert_layout_zero_stride` 的主要消费者——它们把累加器和输入都重打包成二模 `(真实, 广播)` 布局，让「同一列/行的不同元素」在寄存器里正确累加。

`sink_alloc_shape`（[ops.py:1483-1501](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/ops.py#L1483-L1501)）是「per-CTA-tile partial 缓冲形状」的唯一真相源——校验、eager 分配、fake 张量、autotune 上界全部调它。`ColVecReduce`（dim=0）的缓冲是 `(…, m, n_tiles)`：沿 N 切了几个 tile 就有几份 partial。

`ColVecReduce.end_loop_stage`（[quack/epilogue/ops.py:1680-1792](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/ops.py#L1680-L1792)）是蝶形协议的核心。先用 `_lane_warp_info_n`（[ops.py:251-270](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/ops.py#L251-L270)）拿到 N 方向的 lane/warp 几何，并断言 `lanes_in_N` 是 2 的幂（蝶形协议的前提）。然后判断能否走优化路径：

```python
use_swap_shuffle = const_expr(
    lanes_in_N > 1
    and E % num_slices == 0
    and num_slices == 1 << int(math.log2(num_slices))
)
```

走 `swap_shuffle_reduce` 时（[ops.py:1739-1746](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/ops.py#L1739-L1746)），结果**分布**在 `lane_g < num_slices` 的各 lane 上，于是 smem 暂存也按 lane 分片写（[ops.py:1764-1776](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/ops.py#L1764-L1776)）。退化路径则用经典蝶形（[ops.py:1747-1758](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/ops.py#L1747-L1758)），结果只在 `is_lane_n_leader` 上。

`end_loop_finish`（[ops.py:1794-1858](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/ops.py#L1794-L1858)）在驱动共享 barrier 之后运行：`warp_n==0` 的 lane 从 smem 读其它 warp 的 partial，`_merge` 合并，再 `_finalize`（`max_abs` 时清一次符号）写回 gmem。注意 `_finalize` 对 `max_abs` 的处理（[ops.py:1673-1678](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/ops.py#L1673-L1678)）：两输入 `max.xorsign.abs` 保最大幅值但会带异或衍生符号，所以全 fold 完后清一次。

两层 flush 由驱动的「共享 barrier」编排，见 [quack/epilogue/mixin.py:311-356](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/mixin.py#L311-L356)：所有 reduce op 先各自 stage 进**自己的**不相交 smem 区，然后**一次** `epilogue_barrier.arrive_and_wait()` 覆盖所有 op，最后各自 finish。注释强调这是「multi-sink epilogue 每 flush 同步一次，而非每 sink 同步一次」，因为各 sink 的 staging smem 互不相交，一个共享 barrier 是比 per-sink barrier 更弱的同步。

`RowVecReduce`（[ops.py:1861-2111](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/ops.py#L1861-L2111)）是 `ColVecReduce` 的 M/N 镜像：沿 M 归约、跨 M lane/warp 蝶形，缓冲形状 `(…, m_tiles, n)`。它还多一个 `host_finalize_varlen`（[ops.py:1874-1891](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/ops.py#L1874-L1891)）处理变长序列的 per-segment 折叠（图安全的「分段求和」）。

#### 4.4.4 代码实践

**实践目标**：用具体数字推演 `ColVecReduce` 的跨 lane / 跨 warp 蝶形归约协议，验证「swap_shuffle 优化让结果分布、gmem 写分散」。

**操作步骤**：

1. **假设一个线程几何**。设一个 CTA tile 有 `lanes_in_N = 4`、`warps_in_N = 2`（即沿 N 方向有 2 个 warp，每个 warp 内 4 个 lane 持有同一行的不同 N 切片），某行有 `E = 8` 个待归约元素分布在 4 个 lane 上（每 lane 2 个）。

2. **跟踪优化路径**。在 [ops.py:1730-1746](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/ops.py#L1730-L1746) 读 `use_swap_shuffle` 判定：`num_slices = min(4, 8) = 4`，是 2 的幂且 `8 % 4 == 0`，故走 `swap_shuffle_reduce(num_lanes=4, lane_stride=1, slice_elems=2)`。

3. **理解「分布」语义**。读 [ops.py:1764-1776](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/ops.py#L1764-L1776)：`warp_n_idx > 0 and lane_g < num_slices` 的 lane 各自把**自己那一片**（slice_elems=2 个元素）写进 `sExch[row, warp_n-1, k]`。

4. **跟踪 finish**。在 [ops.py:1808-1817](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/ops.py#L1808-L1817) 看 `warp_n==0` 的 lane 如何从 smem 读 warp 1 的 partial 合并，并在 [ops.py:1841-1847](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/ops.py#L1841-L1847) 看 gmem 写如何按 `lane_g` 分散。

**需要观察的现象**：优化路径下，8 个元素的 gmem 写**不是**由单个 leader lane 串行写完，而是由 4 个 lane 各写自己的 2 个元素——天然并行。

**预期结果**：

- **层 1（warp 内）**：4 个 lane 各持 2 个 partial，`swap_shuffle_reduce` 后每个 lane 仍持 2 个元素，但已是「全 4 个 lane 跨该切片的合并结果」。复杂度约 \(E = 8\) 次操作（而非蝶形的 \(8 \cdot \log_2 4 = 16\)）。
- **层 1.5（smem 暂存）**：`warp_n=1` 的 4 个 lane 把各自的 2 个结果写进 `sExch[row, 0, 0..1]`，4 个 lane 并行写、互不冲突（行索引绝对、不相交）。
- **层 2（跨 warp + gmem）**：共享 barrier 后，`warp_n=0` 的 4 个 lane 各自从 smem 读 warp 1 的对应切片、合并，然后 4 个 lane 各写 gmem 的 2 个元素。

（本实践为源码阅读型，数值结果待本地验证：可在 `end_loop_finish` 写 gmem 前加 `cute.printf` 打印 `lane_idx()` 与 `row_idx`，确认写入按 lane 分散。）

#### 4.4.5 小练习与答案

**练习 1**：`VecReduce` 的 `combine="max"` 在 ragged（变长）边界上有何陷阱？`check_oob` 是做什么的？

**参考答案**：OOB 的累加器元素是谓词加载产生的 0。0 是 add 和 max_abs 的单位元，但**不是** max 的单位元（会污染最大值）。所以 `combine="max"` 时，`_mask_oob` 会按元素坐标把越界元素改成 −∞。`check_oob=True`（max 默认开）发射这个掩码；`check_oob=False` 在归约轴已知整除时可编译掉省开销。注意 varlen 下非 add 的 combine 会被主机侧拒绝（分段 max 不是图安全的）。

**练习 2**：为什么 multi-sink epilogue 的两阶段 flush 只用**一个**共享 barrier，而不是每个 reduce op 一个？

**参考答案**：因为每个 reduce op 的 staging smem 区互不相交（行/列索引是绝对 CTA-tile 坐标），所以一个覆盖所有 op 的共享 `arrive_and_wait` 就能保证「所有 staging 写都完成后才开始读」——这比 per-sink 的多个 barrier 同步更弱（开销更低），而正确性等价。注释原话：「a multi-sink epilogue syncs once per flush, not once per sink」。

---

## 5. 综合实践

**任务**：假设你要为一个新的 GEMM epilogue 声明如下行为——

> `D = α(A@B) + bias_row + bias_col`，同时**额外**输出一个激活后的 `postact` 块（辅助输出），并计算一个**每行求和**的列向量 `row_sum`。

请完成：

1. **列出你会在 `_epi_ops` 里声明哪些 op**（给出类名与 name），并标注每个的 `fn_port`。
2. **画出 `store_ctxs` 的顺序**：D 和 aux 各自排第几？为什么 D 永远在最前？
3. **预测 smem 开销**：哪些 op 贡献 `unstaged`、哪些贡献 `d_stage`、哪些贡献 `c_stage`？`row_sum` 在什么条件下贡献 0？

**参考解答**：

1. 声明（仿照 [gemm_default_epi.py:71-83](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_default_epi.py#L71-L83)）：

   | op | name | fn_port | 作用 |
   |----|------|---------|------|
   | `Scalar` | `"alpha"` | `"scalar"` | α |
   | `RowVecLoad` | `"bias_row"` | `"row"` | 行偏置 |
   | `ColVecLoad` | `"bias_col"` | `"col"` | 列偏置 |
   | `TileStore` | `"postact"` | `None`（可设 `epi_tile_fn` 等） | 辅助输出块 |
   | `ColVecReduce` | `"row_sum"` | `"sink"` | 沿 N 求和的列向量 |

   注意：**D 不在这个列表里**——它是 `DStore`，由内核拥有。

2. `store_ctxs = (D 的上下文) + (postact 的上下文)`。D 在最前，因为 [gemm_base.py:305-308](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_base.py#L305-L308) 用 `(DStore(), ...) + store_ctxs` 把 D 拼到由 `epi_setup_aux_out`（遍历 `_epi_store_ops`，即 `TileStore` op）产出的 aux 列表之前。`row_sum` 不是 store op（`is_tile_store()` 为 False），**不进** `store_ctxs`——它走的是 `end_loop` 的两阶段 flush 路径。

3. smem 记账：
   - `Scalar("alpha")`：无 smem（沿用基类空 `EpiSmemBytes`）。
   - `RowVecLoad`/`ColVecLoad`：各贡献 `unstaged`（一块 `tile_N` 或 `tile_M` 大小的向量缓冲，[ops.py:638-641](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/ops.py#L638-L641)）。
   - `TileStore("postact")`：贡献 `d_stage`（每个 store 阶段一份的 tile 缓冲，[ops.py:947-955](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/ops.py#L947-L955)）。
   - `ColVecReduce("row_sum")`：当 `warps_in_N == 1` 时 `_smem_warps` 返回 0，**贡献 0**（[ops.py:1571-1584](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/ops.py#L1571-L1584)）；只有沿 N 排了多个 warp 时才需要 smem 交换区做跨 warp 合并。

## 6. 本讲小结

- `EpiOp` 是「一种张量资源 = 一个自管单元」的抽象：主机侧 schema（`host_arg_key`/`host_fake_arg`/`host_call_arg`/`param_fields`/`to_params`/`smem_bytes`）描述参数与 smem，设备侧生命周期（`begin`/`begin_loop`/`end_loop_*`/`end`）描述内核行为；`fn_port` 是它接入函数式前端的「词性」。
- `Scalar` 是最轻的 op：零 smem、零异步 fence，把标量编码成 absent/immediate/pointer 三种**编译期模式**；`VecLoad` 家族（`RowVecLoad`/`ColVecLoad`）用 cp.async 把广播向量灌进 smem，再借 zero-stride 步幅 `(0,1)`/`(1,0)` 广播到寄存器。
- `TileStore` 完整拥有辅助输出的设备存储路径（含 gated 折半、量化 codec、store 谓词）；`DStore` 只实现设备侧 `store_convert`/`store_r2s`，**主机侧管线由内核拥有、不在 `_epi_ops`**，因为 D 的几何信息要喂给 tile/stage 推算、split-K 工作区、`add_to_output` 等内核基础设施；二者共享同一套 store 钩子序列。
- `TileLoad` 是 `TileStore` 的输入镜像，走 C 操作数那条 staged 流水线，smem 计入 `c_stage`。
- `ColVecReduce`/`RowVecReduce` 沿 N/M 轴归约：warp 内用 `swap_shuffle_reduce`（\(O(E)\)、结果分布）或退化蝶形（\(O(E\log_2 L)\)），跨 warp 经不相交 smem 区 + 一次共享 barrier 合并；OOB 元素对 `max` 需掩码成 −∞。

## 7. 下一步学习建议

- **u6-l3 默认线性 epilogue**：看 `apply_linear_epilogue` 如何把本讲的 `Scalar`/`RowVecLoad`/`ColVecLoad` 组装成 `D = αD + βC + rowvec + colvec`，以及 `GemmDefaultEpiMixin` 如何用本讲的词汇表声明一个完整 epilogue——这是本讲 op 的「最大客户」。
- **u6-l4 `@gemm_epilogue` 函数式前端**：本讲的 `fn_port` 词性表在那里被消费——看 `fn_port="row"` 如何把一个 `RowVecLoad` 接入逐元素数据流，`fn_port="sink"` 如何把 `VecReduce` 接成汇。
- **u6-l5 领域 epilogue**：看 `GroupedColStatsBase`（`fn_port="value"`）、rotary（`fn_port="apply"`）、`quantize_out` 的 `BlockScaleFactorStore` 等更专门的 op 如何在本讲词汇表之上构建。
- **继续阅读**：[quack/epilogue/ops.py](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/ops.py) 末尾的 `GroupedColStatsBase`（约 L2114 起）与 [quack/reduce.py:371](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/reduce.py#L371) 的 `swap_shuffle_reduce` 实现，深入理解蝶形归约的几何。
