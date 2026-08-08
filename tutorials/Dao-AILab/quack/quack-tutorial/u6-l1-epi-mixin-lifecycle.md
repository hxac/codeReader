# ComposableEpiMixin 与 EpiOp 生命周期

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 QuACK「可组合 epilogue」的整体设计理念：为什么把 epilogue 拆成一个个 `EpiOp`，再用一个 `ComposableEpiMixin` 把它们组合成内核钩子。
- 描述一个 `EpiOp` 的**两套钩子**：主机侧（host）负责「这个张量长什么样、要不要编进内核」，设备侧（device）负责「在内核运行时按生命周期加载/计算/写回」。
- 画出 epilogue 在每个 CTA tile 内的生命周期：`begin → begin_loop（每个 subtile）→ end_loop → end`，并指出驱动这段循环的代码在哪里。
- 解释 `EpiContext` 如何把跨 op 共享的上下文（tile 坐标、partition 函数、线程号等）打包传递。
- 讲明白一个核心机制：**inactive op（用户没传的张量）如何被「过滤出编译产物」**——同一个 epilogue 类，按实际传入的参数特化出不同的 cubin。

本讲依赖 [u5-l1 GemmBase 共享主循环与 epilogue 驱动]：那里讲过 mainloop 把累加器凑齐后交给 `epilogue_split_k` 与基类的 **epilogue 驱动循环**，本讲就钻进这段循环背后「驱动的是谁、被驱动的是谁」。

## 2. 前置知识

### 2.1 什么是 epilogue

GEMM 的核心计算 \(D = A @ B\) 之后，通常还要做一堆逐元素或归约的收尾工作：缩放 \( \alpha D \)、加偏置 \( \beta C \)、加行/列广播向量、量化输出、甚至旋转位置编码。这一整段「乘完之后、写回显存之前」的处理就叫 **epilogue**（收尾）。

传统做法是把 epilogue 的数学和搬运逻辑全写死在一个巨型内核里。QuACK 的做法相反：把每种「张量资源」抽成一个独立对象 `EpiOp`（一个标量、一根广播向量、一块待写输出、一次向量归约……），再用一个 mixin 把它们像积木一样拼起来。

### 2.2 你需要已经熟悉的概念

- **`@cute.jit` / `@cute.kernel`**（见 [u1-l4]）：`@cute.jit` 标注的函数会被 CuTe-DSL 编译，既能在主机侧编排，也能在设备侧当内联辅助函数。
- **`const_expr`**（见 [u1-l4]、[u2-l4]）：把一个判断标记为编译期分支，只编入命中的那一支。本讲里它是「过滤 inactive op」落到机器码上的关键。
- **主机侧 / 设备侧之分**：主机侧（host）跑在 CPU 上，负责编译、构造 fake 张量、把运行期参数摊平；设备侧（device）跑在 GPU 上，是真正并行执行的内核代码。一个 `EpiOp` 同时在两侧出现，但做的事完全不同。
- **tile / subtile**：GEMM 把输出切成 CTA tile（一个线程块算一块），epilogue 又把一个 CTA tile 切成更小的 subtile 逐块处理。
- **jit_cache / 计划缓存**（见 [u2-l6]、[u4-l1]）：编译产物 `.o` 和启动计划会被缓存，「形状进计划 key、结构进编译期」。

### 2.3 一个直觉比喻

把 epilogue 想成一条流水线：

- `EpiOp` 是流水线上的**工位**，每个工位只管一类零件（一根向量、一块输出）。
- `ComposableEpiMixin` 是**流水线调度员**，它不关心具体零件，只负责在固定的时刻（开工、每个工位节拍、收工）依次叫每个工位干活。
- 用户这次没带某类零件？调度员就把对应工位**从流水线上撤掉**，连这台机器都不造。

下面我们就按「调度员 → 工位 → 共享工具箱」的顺序展开。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲用来 |
| --- | --- | --- |
| [quack/epilogue/mixin.py](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/mixin.py) | `ComposableEpiMixin`：把 `_epi_ops` 组合成标准 epilogue 钩子方法 | 讲「组合机制」与生命周期驱动 |
| [quack/epilogue/ops.py](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/ops.py) | `EpiOp` 基类与全部具体 op 词汇表 | 讲「生命周期钩子协议」与 `EpiContext` |
| [quack/gemm_base.py](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_base.py) | `GemmBase` 的 epilogue 驱动循环 | 看 `begin/begin_loop/end_loop/end` 被谁、在什么时刻调用 |
| [quack/gemm_default_epi.py](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_default_epi.py) | `GemmDefaultEpiMixin`：手写 epilogue 的标准范例 | 看一个具体子类如何声明 `_epi_ops` |
| [quack/gemm_runtime/host.py](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_runtime/host.py) | 泛化的主机侧 plan/compile/launch 层 | 看 `host_arg_key/host_fake_arg/host_call_arg` 三件套如何被驱动 |

> 阅读提示：`ops.py` 有 3000 多行、十几个 op 类。本讲**只精读 `EpiOp` 基类和最简单的几个 op**（`Scalar`、`VecLoad`），把生命周期协议讲透；其它 op 的差异放到 [u6-l2 EpiOp 词汇表]。

---

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

1. **4.1 ComposableEpiMixin 组合机制**——调度员怎么把一堆 op 拼成钩子、怎么过滤 inactive op。
2. **4.2 EpiOp 生命周期钩子**——每个 op 的主机侧与设备侧两套钩子协议。
3. **4.3 EpiContext 共享上下文**——打包传递的共享工具箱。

### 4.1 ComposableEpiMixin 组合机制

#### 4.1.1 概念说明

`ComposableEpiMixin` 是一个 **mixin（混入类）**。它本身不包含任何 GEMM 主循环逻辑，只提供一组「epilogue 钩子方法」：`epi_begin`、`epi_begin_loop`、`epi_end_loop`、`epi_end`、`epi_get_smem_tensors`、`epi_smem_bytes` 等。一个具体的 GEMM 内核类通过多继承把它「混入」，再配合各 SM 的基类（如 `GemmSm90`），就获得了完整的 epilogue 能力。

它的核心设计是：

- 子类用**类级属性 `_epi_ops`** 声明这个 epilogue **可能用到**的全部 op，这是一份**静态 schema（模式）**，不是运行期实例。
- mixin 在 `__init_subclass__` 里自动把这份 schema 翻译成一个 `EpilogueParams` dataclass（内核参数容器）。
- 在真正编译/启动时，mixin 会把 schema **过滤**成「这次调用实际激活的 op」，之后所有迭代（主机侧、设备侧）都只遍历激活集。

一句话总结理念：**「声明」是全集，「执行」是子集**。这让我们能用一个类表达一族 epilogue（带偏置 / 不带偏置 / 带量化输出……），而每种组合各自编译出一份精简的 cubin。

#### 4.1.2 核心流程

下面是 `ComposableEpiMixin` 在「类定义 → 编译 → 启动 → 内核运行」四个阶段做的事：

```text
① 类定义期（import 时）
   子类声明 _epi_ops = (Scalar("alpha"), RowVecLoad("bias"), ...)
        │
        ▼  __init_subclass__ 触发
   _make_epi_ops() 把每个 op 的 param_fields() 汇总
        │
        ▼
   自动生成 EpilogueParams dataclass（含全部字段，inactive 的默认 None）

② 计划/编译期（主机侧，首次某组形状）
   host.py 对每个 op 调 host_arg_key(torch_value)
        │  返回 None ⇒ 这个 op 不进编译键 ⇒ 不进编译产物
        ▼
   只把 key 非 None 的 op 纳入 jit_cache 键 → 编译出「精简」cubin

③ 启动构建期（主机侧，epi_to_underlying_arguments）
   _filter_epi_ops(args) 把 self._epi_ops 影子覆盖成「只含激活 op」
        │
        ▼
   _epi_ops_to_params_dict() 对每个激活 op 调 to_params()
        │
        ▼
   构造 EpilogueParams(**d)，inactive 字段保持 None

④ 内核运行期（设备侧）
   epi_begin / epi_begin_loop / epi_end_loop / epi_end
   全都只遍历 self._epi_ops（已被过滤为激活集）
   ⇒ 每个 op 的钩子都能假设自己的 param / arg_tensor 非 None
```

注意 ② 和 ③ 两处「过滤」的层次不同：

- ② 是**编译键层面**的过滤：`host_arg_key` 返回 `None` 时，op 根本不进编译键，于是生成的 cubin 里没有它的任何代码。
- ③ 是**实例属性层面**的过滤：把 `self._epi_ops` 从「类级全集」替换成「实例级激活集」，让设备侧循环不再遍历它。

两层配合，才真正实现「inactive op 被过滤出编译产物」。

#### 4.1.3 源码精读

**(a) 类级 schema 与 `__init_subclass__`**

[quack/epilogue/mixin.py:60-78](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/mixin.py#L60-L78) 定义了 mixin 的骨架：`_epi_ops = ()` 是空 schema，`__init_subclass__` 在子类创建时自动生成 `EpilogueParams`。

关键点是 `_extra_param_fields` 也会触发生成——一个「op-less」的 epilogue（比如纯 identity）仍可能带 split-k 参数字段，否则会错误地回退到 `GemmBase` 的空 `EpilogueParams`。

**(b) `_make_epi_params`：从 schema 生成参数容器**

[quack/epilogue/mixin.py:45-57](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/mixin.py#L45-L57) 用 `dataclasses.make_dataclass` 动态构造 `EpilogueParams`。它把每个 op 的 `param_fields()` 汇总，并按「必填字段在前、可选字段在后」排序：

```python
required, optional = [], []
for op in epi_ops:
    for name, typ, default in op.param_fields():
        (required if default is MISSING else optional).append((name, typ, default))
```

> 注意：这里遍历的是**全集** `epi_ops`，所以生成的 dataclass 含全部字段。inactive op 的字段因为有默认值（`None`）而被归入 optional，构造时可以不传——这正是「全集 schema + 过滤执行」能共存的根基。

**(c) `_filter_epi_ops`：影子覆盖**

[quack/epilogue/mixin.py:82-90](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/mixin.py#L82-L90) 是过滤的核心：

```python
def _filter_epi_ops(self, args):
    self._epi_ops = tuple(
        op for op in type(self)._epi_ops if getattr(args, op.name, None) is not None
    )
```

它做了一件微妙的事：把实例属性 `self._epi_ops` **覆盖**成只含激活 op 的元组，而 `type(self)._epi_ops`（类级属性）保持全集不变。此后任何 `self._epi_ops` 的迭代都只看到激活集，但类级 schema 依然完整。注释里明确点出：过滤之后，op 的钩子方法可以假设自己的 `param`/`arg_tensor` 非 None。

**(d) 两个「必须提前过滤」的钩子**

并非所有主机侧钩子都能等 `_filter_epi_ops` 运行完再过滤。`resolve_epi_m_major` 和 `epi_smem_bytes` 必须在 `epi_to_underlying_arguments` **之前**运行——因为 `epi_m_major` 决定 `epi_tile`，smem 预算决定 stage 数，而 `epi_to_underlying_arguments` 又依赖这些（典型的「鸡生蛋」）。于是这两个钩子直接对类级全集 `type(self)._epi_ops` 做**内联过滤**：

[quack/epilogue/mixin.py:105-116](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/mixin.py#L105-L116)（`resolve_epi_m_major`）：

```python
for op in type(self)._epi_ops:
    arg = getattr(args, op.name, None)
    if arg is not None:
        score += op.epi_m_major_score(arg, self)
```

[quack/epilogue/mixin.py:120-133](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/mixin.py#L120-L133)（`epi_smem_bytes`，classmethod，同理内联过滤）。注释在 [quack/epilogue/mixin.py:14-23](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/mixin.py#L14-L23) 详细解释了这段「鸡生蛋」顺序。

**(e) smem 结构装配**

[quack/epilogue/mixin.py:135-157](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/mixin.py#L135-L157) 把每个激活 op 的 `smem_struct_field()` 汇总成一个 `cute.struct`。两个细节值得记：

- 若所有激活 op 都不需要 smem（例如只有 `Scalar`），返回零字节占位 `cute.struct.MemRange[Int32, 0]`，因为 `cute.struct` 拒绝空注解。
- 字段按字节大小**升序排序**后再装，让小字段排在大高对齐字段前面，减少对齐 padding 浪费的 smem。

**(f) 设备侧驱动钩子（先看 `epi_begin`）**

[quack/epilogue/mixin.py:237-278](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/mixin.py#L237-L278) 是 `@cute.jit` 设备函数 `epi_begin`，每个 op 只调一次：

```python
ctx = EpiContext(self, epi_tile, ...)
results = {
    op.name: op.begin(self, getattr(params, op.name), epi_smem_tensors.get(op.name), ctx)
    for op in self._epi_ops
}
```

注意三个细节：① 结果是**按 op 名字索引的 dict**；② 用 `epi_smem_tensors.get(op.name)`（`.get` 而非 `[]`）取 smem 张量，因为 inactive op 不在 dict 里；③ 末尾有一段 `const_expr` 守卫的异步栅栏——只要任何一个激活 op 声明 `needs_async_fence()`（比如 cp.async 加载的广播向量），就要 `commit_group/wait_group` 加屏障。

**(g) 两阶段 flush 的 `epi_end_loop`**

[quack/epilogue/mixin.py:311-356](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/mixin.py#L311-L356) 是本讲在「组合机制」里最值得品味的设计——**两阶段 flush**：

```text
阶段一：每个 op 先做 intra-warp 归约 + 写到自己的 smem 暂存区（互不相交）
阶段二：一道「共享 barrier」给所有暂存写排序
阶段三：每个 op 再做 inter-warp 合并 + 写 gmem
```

注释（[quack/epilogue/mixin.py:325-330](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/mixin.py#L325-L330)）点明收益：一个多 sink 的 epilogue 每次 flush 只付**一道** `arrive_and_wait`，而不是每个 sink 一道。这道共享 barrier 比「每 sink 一道 barrier」是**严格更弱**的同步。

#### 4.1.4 代码实践

**实践目标**：亲手追踪「schema 全集」如何变成「激活子集」，并验证 `EpilogueParams` 含全部字段。

**操作步骤**（源码阅读型实践，无需 GPU）：

1. 打开 [quack/gemm_default_epi.py:70-83](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_default_epi.py#L70-L83)，读 `GemmDefaultEpiMixin._epi_ops`。数一下它声明了几个 op（答案：7 个——3 个 `Scalar`、`RowVecLoad`、`ColVecLoad`、2 个 `BlockScaleFactorStore`）。
2. 打开 [quack/gemm_default_epi.py:117-125](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_default_epi.py#L117-L125) 的 `epi_to_underlying_arguments`，确认它第一步就是 `self._epi_ops_to_params_dict(args)`（内部会 `_filter_epi_ops`）。
3. 对照 [quack/epilogue/mixin.py:82-90](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/mixin.py#L82-L90)：假设用户这次只传了 `alpha`、没传 `mRowVecBroadcast`、`mColVecBroadcast`、`mSFD`、`mSFDCol`，写出过滤后 `self._epi_ops` 里还剩哪几个 op。

**需要观察的现象**：

- `EpilogueParams`（自动生成）依然**同时**有 `alpha`、`mRowVecBroadcast`、`mSFD` 等全部字段——因为它是从全集 schema 生成的。
- 但 `self._epi_ops`（实例属性）被覆盖后只剩激活的那些，所以设备侧 `epi_begin` 循环不会去碰没传的向量。

**预期结果**：过滤后约剩 `Scalar("alpha")`、`Scalar("beta")`（若 beta 也未传则进一步剔除）、`Scalar("sr_seed")`（若未传则剔除）；`mRowVecBroadcast` 等张量类 op 全部因为对应 `args` 字段为 `None` 而被移出。

> 本实践为源码阅读型，无需运行；若要运行验证，可在本地 `python -c` 里 import 类后打印 `GemmDefaultEpiMixin._epi_ops` 与 `_make_epi_params(...)` 生成的字段名（待本地验证）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `_make_epi_params` 遍历的是**全集** `epi_ops` 而不是已过滤的激活集？如果只遍历激活集会出什么问题？

> **答案**：`EpilogueParams` 必须能容纳「任意子集激活」的调用。如果它只含激活字段，那么当另一次调用激活了不同子集时，同一个 dataclass 就无法表示——而编译期我们需要一个稳定的参数容器类型。全集 schema 生成全集字段（inactive 的默认 `None`），才能让一份 `EpilogueParams` 适配所有组合。

**练习 2**：`resolve_epi_m_major` 为什么不能像别的方法那样依赖 `_filter_epi_ops`，而要「内联过滤」？

> **答案**：`resolve_epi_m_major` 运行在 `epi_to_underlying_arguments`（也就是 `_filter_epi_ops` 的调用点）**之前**，因为它要算出 `epi_m_major`，而 `epi_m_major` 又驱动 `epi_tile` / smem 布局，后者才是 `epi_to_underlying_arguments` 的输入。这是「鸡生蛋」顺序，所以它必须直接对类级全集做内联过滤。

---

### 4.2 EpiOp 生命周期钩子

#### 4.2.1 概念说明

`EpiOp` 是所有 epilogue 操作的基类。每个具体 op（`Scalar`、`RowVecLoad`、`TileStore`、`ColVecReduce`……）封装**一种张量资源**在整个 epilogue 生命周期的行为。

它的钩子分成两大组，读者务必分清：

| 钩子组 | 运行位置 | 职责 | 本组代表方法 |
| --- | --- | --- | --- |
| **主机侧 torch-arg schema** | CPU，编译/计划期 | 描述「这个张量长什么样、要不要编进内核、运行期传什么」 | `host_arg_key` / `host_fake_arg` / `host_call_arg` |
| **主机侧 args→params + smem** | CPU，启动构建期 | 把 torch 张量转成内核参数、申报 smem | `param_fields` / `to_params` / `smem_bytes` / `smem_struct_field` |
| **设备侧生命周期** | GPU，内核运行期 | 在固定时刻加载/计算/写回 | `begin` / `begin_loop` / `end_loop_stage` / `end_loop_finish` / `end` |

设备侧这五个方法构成一条清晰的**生命周期**，由 `ComposableEpiMixin` 的对应钩子按固定顺序调用。

#### 4.2.2 核心流程

设备侧一个 CTA tile 内的生命周期如下（驱动代码在 [u5-l1] 讲过的 `gemm_base.py` epilogue 循环里）：

```text
每个 CTA tile（一次）:
  ┌─ epi_begin       → 对每个 op 调 op.begin() 一次
  │                    （加载广播向量到 smem、分配归约寄存器、建坐标分区…）
  │                    返回 state（按 op 名存入 dict）
  │
  │  每个 subtile（循环）:
  │    ├─ epi_begin_loop → op.begin_loop(state, epi_coord)
  │    │                  切出本 subtile 的寄存器片段（如广播向量的一行）
  │    ├─ epi_visit_subtile → 用户数学（D = αD + βC + rowvec + colvec）
  │    └─ epi_end_loop → 两阶段 flush：
  │         阶段1 op.end_loop_stage()：归约 + 写 smem 暂存
  │         共享 barrier
  │         阶段2 op.end_loop_finish()：合并 + 写 gmem
  │         （对 store 类 op，另有 store_convert / store_r2s / TMA store）
  │
  └─ epi_end         → 对每个 op 调 op.end()（全部 subtile 后的收尾）
```

驱动代码精确定位：

- `epi_begin` 调用：[quack/gemm_base.py:319-330](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_base.py#L319-L330)
- subtile 循环里 `epi_begin_loop` / `epi_visit_subtile` / `epi_end_loop`：[quack/gemm_base.py:396-430](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_base.py#L396-L430)
- `epi_end` 收尾：[quack/gemm_base.py:503](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_base.py#L503)

#### 4.2.3 源码精读

**(a) `EpiOp` 基类骨架与主机侧 schema 三件套**

[quack/epilogue/ops.py:298-300](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/ops.py#L298-L300) 起是基类定义。先看主机侧 torch-arg schema 的三件套（注释见 [quack/epilogue/ops.py:367-372](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/ops.py#L367-L372)）：

- [host_arg_key](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/ops.py#L373-L379)：`torch 值 → 可 pickle 的描述符`；**返回 `None` 表示这个 op 缺席，会被过滤出编译产物**。基类默认实现返回 `(dtype, ndim)`：

```python
def host_arg_key(self, value):
    if value is None:
        return None
    return (torch2cute_dtype_map[value.dtype], value.ndim)
```

- [host_fake_arg](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/ops.py#L381-L385)：`描述符 → 编译期 fake 张量`。基类默认返回 `None`（缺席）。`host.py` 在 [host.py:9-11](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_runtime/host.py#L9-L11) 与 [host.py:196](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_runtime/host.py#L196) 用它构造喂给 `cute.compile` 的符号张量。
- [host_call_arg](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/ops.py#L387-L389)：`torch 值 → 运行期实际参数`。基类默认原样返回。

这三个方法构成了「**值的三态**」：torch 值（用户给）→ 描述符（进编译键）→ fake 张量（编译时用）→ 运行期参数（启动时用）。

**(b) 设备侧生命周期五件套**

设备侧钩子的协议见 [quack/epilogue/ops.py:457-515](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/ops.py#L457-L515)。基类都给了「安全空实现」，子类按需覆盖：

```python
@cute.jit
def begin(self, gemm, param, smem_tensor, ctx):
    """One-time per-tile setup. Returns state for begin_loop."""
    return None

def begin_loop(self, gemm, state, epi_coord):
    """Per-subtile extraction. Returns value for epi_visit_subtile."""
    return state

def end_loop_stage(self, gemm, param, state, epi_coord, epi_tile, ...):
    """Per-subtile flush phase 1: intra-warp reduce + smem staging.
       返回 None 表示本次无需 flush；否则返回 (needs_barrier, finish_state)。"""

def end_loop_finish(self, gemm, param, staged, tile_coord_mnkl, varlen_manager):
    """Per-subtile flush phase 2 (在驱动共享 barrier 之后): 合并 + 写 gmem。"""

def end(self, gemm, param, state, epi_tile, ...):
    """Cleanup after all subtiles (reductions, direct writes)."""
```

注意 `begin` / `end_loop_stage` / `end` 用了 `@cute.jit`（会被编译进内核），而 `begin_loop` 没有——它通常是被 `epi_begin_loop` 这个 `@cute.jit` 钩子内联调用的普通方法。

**(c) 用 `Scalar` 看最简单的 op**

[quack/epilogue/ops.py:518-602](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/ops.py#L518-L602) 是 `Scalar`，它演示了主机侧 schema 的「三态」如何编码多形态：

`host_arg_key`（[ops.py:564-570](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/ops.py#L564-L570)）把标量分成三种模式：

- `0 = absent`（`None`）：缺席 → 不进编译产物；
- `1 = immediate`（Python 数）：主机常量 → 编译期烘焙；
- `2 = pointer`（CUDA 张量）：设备指针 → 运行期传递。

```python
def host_arg_key(self, value):
    if value is None:
        return self.host_key_for_mode(0)          # absent
    if hasattr(value, "data_ptr"):
        self._validate_pointer_value(value)
        return self.host_key_for_mode(2)          # pointer
    return self.host_key_for_mode(1)              # immediate
```

于是 `alpha` 这一个 op 就能编译出三份不同的代码（缺席 / 常量 / 指针），这正是「inactive op 被过滤出编译产物」的一个具体实例。它的 `begin`（[ops.py:597-601](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/ops.py#L597-L601)）在设备侧用 `utils.load_scalar_or_pointer` 把参数读成标量。

**(d) 用 `VecLoad` 看「需要 smem + 异步栅栏」的 op**

[quack/epilogue/ops.py:604-717](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/ops.py#L604-L717) 是广播向量加载基类 `VecLoad`（子类 `RowVecLoad`/`ColVecLoad`）。它演示了完整的 smem 生命周期：

- `smem_bytes`（[ops.py:638-641](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/ops.py#L638-L641)）：申报一块 `unstaged` 的 smem（一根向量大小）。
- `smem_struct_field` / `get_smem_tensor`（[ops.py:643-654](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/ops.py#L643-L654)）：声明 struct 字段、从 storage 取出张量。
- `needs_async_fence`（[ops.py:656-657](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/ops.py#L656-L657)）返回 `True`——它用 cp.async 加载，所以 `epi_begin` 末尾的那段 `const_expr` 守卫栅栏会为它打开。
- `begin`（[ops.py:669-700](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/ops.py#L669-L700)）：用 `tiled_copy_1d(is_async=True)` 把向量从 gmem 拷到 smem，再 partition 出寄存器视图，返回 `[tDsV, tDrV_cvt]` 作为 state。
- `begin_loop`（[ops.py:702-717](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/ops.py#L702-L717)）：按 `epi_coord` 切出本 subtile 的寄存器片段，并在「同行首个 subtile」才真正加载（避免重复加载）。

**(e) store 类 op 的特殊路径**

`TileStore`（[ops.py:796-1109](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/ops.py#L796-L1109)）和 `DStore`（[ops.py:1111-1158](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/ops.py#L1111-L1158)）走的是「store 路径」而不是 `begin/begin_loop/end_loop`：它们用 `store_setup` / `store_convert` / `store_r2s` 三件套，由 `ComposableEpiMixin.epi_setup_aux_out`（[mixin.py:187-214](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/mixin.py#L187-L214)）和 `gemm_base.epilogue` 驱动。这里有一个重要区别（见 `DStore` 的 docstring，[ops.py:1111-1124](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/ops.py#L1111-L1124)）：**主输出 D 的主机侧管线由内核自己拥有，所以 `DStore` 不在 `_epi_ops` 里**，驱动循环直接从内核构建的部件组装它的 store context。这部分细节留到 [u6-l2]。

**(f) 缓存身份：`config_key` / `cache_key`**

[quack/epilogue/ops.py:340-365](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/ops.py#L340-L365) 定义 op 的缓存身份。基类 `config_key` 是**fail-closed**（默认拒绝）：只要 op 有除 `name` 外的实例属性，就要求子类显式实现 `config_key()`，否则抛 `NotImplementedError`。这是为了防止「两个语义不同的 epilogue 因为漏报配置而在持久 JIT 缓存里别名」。`cache_key` 拼接 `(模块, qualname, name, config_key)`，`__quack_semantic_key__` 直接复用它作为 fn 前端的语义指纹。

#### 4.2.4 代码实践（本讲必做的核心实践）

**实践目标**：在 `EpiOp` 中列出主机侧与设备侧两套钩子，并解释 inactive op 如何被过滤出编译产物。

**操作步骤**（源码阅读 + 画表型实践）：

1. 打开 [quack/epilogue/ops.py](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/ops.py)，在 `EpiOp` 基类（[L298-L515](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/ops.py#L298-L515)）里找出并填下面这张表的两套钩子：

   | 侧 | 钩子名 | 行号 | 作用 |
   | --- | --- | --- | --- |
   | 主机侧 | `host_arg_key` | L373 | torch 值→描述符，None=缺席 |
   | 主机侧 | `host_fake_arg` | L381 | 描述符→编译期 fake 张量 |
   | 主机侧 | `host_call_arg` | L387 | torch 值→运行期参数 |
   | 主机侧 | `param_fields` | L404 | 生成 EpilogueParams 的字段 |
   | 主机侧 | `to_params` | L409 | args→param dict |
   | 主机侧 | `smem_bytes` | L419 | 申报 smem（EpiSmemBytes） |
   | 设备侧 | `begin` | L458 | 每 tile 一次的 setup |
   | 设备侧 | `begin_loop` | L463 | 每 subtile 切片 |
   | 设备侧 | `end_loop_stage` | L472 | flush 阶段 1 |
   | 设备侧 | `end_loop_finish` | L493 | flush 阶段 2 |
   | 设备侧 | `end` | L502 | 全部 subtile 后收尾 |

2. 然后追踪「过滤出编译产物」的**两层**：
   - **编译键层**：读 [host.py:9-11](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_runtime/host.py#L9-L11) 与 [host.py:416](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_runtime/host.py#L416)：`epi_keys` 由 `(op_name, op.host_arg_key(value))` 组成；当 `host_arg_key` 返回 `None`，该 op 不进键、不进编译产物。
   - **实例属性层**：读 [mixin.py:82-90](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/mixin.py#L82-L90)：`_filter_epi_ops` 把 `self._epi_ops` 影子覆盖成激活集，使设备侧循环不再遍历它。

3. 用一段话写出你的解释（参考答案见 4.2.5）。

**需要观察的现象**：同一份 `_epi_ops` 全集，在不同调用下因传参不同，会编译出**结构不同**的 cubin（激活 op 数量不同），且运行期 `self._epi_ops` 长度也不同。

**预期结果**：你应该能区分「op 不在编译产物里」（编译键层）和「op 不在本次设备循环里」（实例属性层）这两个层次，并指出它们是**同一过滤意图的两道落地**。

#### 4.2.5 小练习与答案

**练习 1**：`Scalar.host_arg_key` 返回的三种模式里，哪一种对应「inactive op 被过滤出编译产物」？为什么 `Scalar` 即便「缺席」也返回一个非 `None` 的键，而 `EpiOp` 基类的 `host_arg_key` 在 `value is None` 时返回 `None`？

> **答案**：模式 `0 = absent` 对应过滤。基类返回 `None` 表示「整 op 缺席、不进编译键」；`Scalar` 返回 `host_key_for_mode(0)`（一个非 `None` 描述符）是为了让「缺席」这个事实本身进入编译键——这样一份 cubin 里 `alpha` 是「缺席」分支（`const_expr(alpha is None)` 命中、不生成缩放代码），而另一份 cubin 里 `alpha` 是「指针」分支。两种是不同 cubin，缓存键必须区分它们。

**练习 2**：`begin_loop` 没有 `@cute.jit` 装饰器，而 `begin` 有。这会出问题吗？

> **答案**：不会。`begin_loop` 是被 `epi_begin_loop`（带 `@cute.jit` 的钩子，见 [mixin.py:280-283](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/mixin.py#L280-L283)）内联调用的普通方法，DSL 在编译 `epi_begin_loop` 时会把它的源码一并解析进去。装饰器只需打在最外层的编译入口上。

**练习 3**：`DStore` 为什么不在 `_epi_ops` 里，却仍实现了 `store_convert` / `store_r2s`？

> **答案**：主输出 D 的主机侧管线（TMA atom、staged smem 布局、`sD` struct 字段）被内核直接拥有，用于 tile/stage 大小决策和 split-K workspace 重指向，所以不走 `EpiOp` 的主机侧钩子。但 D 的**设备侧存储流程**（dtype 转换、寄存器→smem 拷贝）和其它输出一样，所以 `DStore` 复用同一套 `store_convert` / `store_r2s` 钩子，由驱动循环（[gemm_base.py:296-308](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_base.py#L296-L308)）直接构造它的 store context。

---

### 4.3 EpiContext 共享上下文

#### 4.3.1 概念说明

`EpiContext` 是一个**纯数据容器**（用 `__slots__` 声明字段），它把 `op.begin()` 需要的一堆公共参数打包成一个对象，避免每个 op 的 `begin` 签名都拖着十几个参数。

它的角色是「**共享工具箱**」：

- 算了几何量（`tile_M`、`tile_N`、`batch_idx`、`num_epi_threads`）；
- 预备了关键的 **partition 函数**（`partition_for_epilogue_fn`），让每个 op 用同一种方式把 tile 切分到线程；
- 透传拷贝描述符（`tiled_copy_t2r`、`tiled_copy_r2s`）、坐标（`tile_coord_mnkl`）、变长序列管理器（`varlen_manager`）、屏障（`epilogue_barrier`）、线程号（`tidx`）。

#### 4.3.2 核心流程

`EpiContext` 在 `epi_begin` 开头被构造一次，然后传给每个 op 的 `begin`：

```text
epi_begin(self, params, epi_smem_tensors, epi_tile, ...)
    │
    ├── ctx = EpiContext(self, epi_tile, tiled_copy_t2r, tiled_copy_r2s,
    │                    tile_coord_mnkl, varlen_manager, epilogue_barrier,
    │                    tidx, tRS_rD_layout)
    │       │
    │       └── 内部预计算:
    │            tile_M, tile_N   ← cta_tile_shape_mnk[0/1]
    │            batch_idx        ← tile_coord_mnkl[3]
    │            num_epi_threads  ← num_epi_warps * WARP_SIZE
    │            partition_for_epilogue_fn ← partial(partition_for_epilogue, ...)
    │
    └── for op in self._epi_ops:
            op.begin(self, getattr(params, op.name),
                     epi_smem_tensors.get(op.name), ctx)   # ← 把 ctx 传进去
```

#### 4.3.3 源码精读

**(a) 字段与构造**

[quack/epilogue/ops.py:121-175](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/ops.py#L121-L175) 定义了 `EpiContext`。`__slots__`（[ops.py:129-143](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/ops.py#L129-L143)）列出了全部字段。构造函数（[ops.py:145-175](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/ops.py#L145-L175)）做了几件实事：

```python
self.tile_M = gemm.cta_tile_shape_mnk[0]
self.tile_N = gemm.cta_tile_shape_mnk[1]
self.batch_idx = tile_coord_mnkl[3]
self.num_epi_threads = gemm.num_epi_warps * cute.arch.WARP_SIZE
self.partition_for_epilogue_fn = partial(
    partial_for_epilogue,
    epi_tile=epi_tile,
    tiled_copy=tiled_copy_t2r if tiled_copy_t2r is not None else tiled_copy_r2s,
    tidx=tidx,
    reference_src=tiled_copy_t2r is None,
)
```

`partition_for_epilogue_fn` 是工具箱里最重要的工具：它用 `functools.partial` 把 `partition_for_epilogue`（来自 [quack/sm90_utils.py](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/sm90_utils.py)）的 `epi_tile`、`tiled_copy`、`tidx`、`reference_src` 都预先绑好，op 调用时只需传「要 partition 什么张量」。

**(b) op 如何使用它**

以 `VecReduce.begin`（[ops.py:1603-1627](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/ops.py#L1603-L1627)）为例，它用 `ctx.partition_for_epilogue_fn` 把广播布局和坐标 identity 张量都切分到线程，读 `ctx.tile_M`、`ctx.tile_N` 得到 tile 尺寸，读 `ctx.tile_coord_mnkl` 得到当前 tile 坐标——所有这些都不用单独传参，全从 `ctx` 取。

`tRS_rD_layout` 字段（[ops.py:124-127](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/ops.py#L124-L127)）是给 `TileLoad` 用的：它是乘积输出 tile 的寄存器布局，`TileLoad.begin` 用它来让自己的寄存器 tile 与 `tRS_rD` 逐元素对齐（详见 [u6-l2]）。

#### 4.3.4 代码实践

**实践目标**：理解 `partition_for_epilogue_fn` 这个 partial 的作用。

**操作步骤**（源码阅读型）：

1. 读 [quack/epilogue/ops.py:169-175](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/ops.py#L169-L175)，记下 `partition_for_epilogue_fn` 预绑了哪些参数（`epi_tile`、`tiled_copy`、`tidx`、`reference_src`）。
2. 在 `ops.py` 内搜索 `ctx.partition_for_epilogue_fn(`（用编辑器/Grep），看它在 `VecLoad.begin`、`VecReduce.begin`、`GroupedColStatsBase.stats_begin` 等处分别 partition 什么张量。

**需要观察的现象**：多个 op 复用同一个 partition 函数，但 partition 的输入张量不同（广播向量、归约寄存器、坐标张量）。

**预期结果**：你应该能解释「为什么把 partition 函数预绑参数放进 `EpiContext`」——因为它让所有 op 用统一的方式把 tile 切到线程，又不用每个 op 的 `begin` 签名都重复这些参数。

#### 4.3.5 小练习与答案

**练习 1**：`EpiContext` 用 `__slots__` 而不是普通类属性，有什么好处？

> **答案**：`__slots__` 固定了字段集合，禁止动态新增属性，既省内存（无实例 `__dict__`），也防止 op 钩子误写上下文字段造成隐蔽 bug。对于这种「纯数据传递容器」是合适的。

**练习 2**：`partition_for_epilogue_fn` 的 `reference_src` 参数是怎么决定的？为什么？

> **答案**：`reference_src = tiled_copy_t2r is None`（[ops.py:174](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/ops.py#L174)）。`reference_src` 控制 `_get_lane_warp_layouts` 用源（寄存器，SM90）还是目标（smem，SM100）布局来推导 lane/warp 几何（见 [ops.py:178-221](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/ops.py#L178-L221) 的注释）。当有 `tiled_copy_t2r`（tile→register）时，SM90 走寄存器布局；没有时退到 `tiled_copy_r2s` 并用 smem（目标）布局，匹配 SM100 的几何推导。

---

## 5. 综合实践

把三个最小模块串起来，完成下面这个**端到端追踪任务**。它要求你把「声明 → 过滤 → 编译 → 设备生命周期」整条链走通。

**任务**：以 `GemmDefaultEpiMixin` 为对象，回答以下问题，并把答案整理成一张「调用链时序图」。

1. **声明层**：[gemm_default_epi.py:71-83](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_default_epi.py#L71-L83) 声明了哪 7 个 op？其中哪些会产生 smem（提示：看哪些 op 覆盖了 `smem_bytes` 返回非零值）？
2. **参数层**：`EpilogueArguments`（[gemm_default_epi.py:95-113](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_default_epi.py#L95-L113)）与自动生成的 `EpilogueParams` 有什么关系？为什么 `EpilogueArguments` 是手写的而 `EpilogueParams` 是自动生成的？
3. **过滤层**：假设一次调用只传了 `alpha`、`beta`、`mRowVecBroadcast`，没传其余。请写出：
   - 编译键层面：哪些 op 的 `host_arg_key` 返回非 `None`（进编译产物）？
   - 实例属性层面：`_filter_epi_ops` 后 `self._epi_ops` 剩哪些？
4. **设备生命周期层**：在 [gemm_base.py:319-503](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_base.py#L319-L503) 的驱动循环里，`epi_begin` / `epi_begin_loop` / `epi_end_loop` / `epi_end` 分别在第几行被调用？对激活的 `RowVecLoad`，它的 `begin` / `begin_loop` 分别在哪个阶段执行、各做什么？
5. **共享上下文层**：这次调用里 `EpiContext.partition_for_epilogue_fn` 预绑的 `tiled_copy` 是 `tiled_copy_t2r` 还是 `tiled_copy_r2s`？依据是什么？

**参考答案要点**：

1. 7 个 op：`Scalar("alpha")`、`Scalar("beta")`、`Scalar("sr_seed", dtype=Int32)`、`RowVecLoad("mRowVecBroadcast")`、`ColVecLoad("mColVecBroadcast")`、`BlockScaleFactorStore("mSFD")`、`BlockScaleFactorStore("mSFDCol", direction="col")`。会产生 smem 的是两个 `VecLoad`（`unstaged` 一根向量）和两个 `BlockScaleFactorStore`；`Scalar` 不产生 smem（`epi_get_smem_tensors` 显式排除 `Scalar`，见 [mixin.py:159-164](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/mixin.py#L159-L164)）。
2. `EpilogueArguments` 是面向用户的 NamedTuple（带 `Constexpr` 字段如 `rounding_mode`、`add_to_output`），手写因为含编译期开关；`EpilogueParams` 是面向内核的参数容器，字段完全由 op 的 `param_fields()` + `_extra_param_fields` 决定，所以能自动生成。
3. 编译键层面进产物：`alpha`、`beta`、`mRowVecBroadcast`（这三个 `host_arg_key` 非 `None`）；`sr_seed`、`mColVecBroadcast`、`mSFD`、`mSFDCol` 缺席。实例属性层面 `self._epi_ops` 剩 `Scalar("alpha")`、`Scalar("beta")`、`RowVecLoad("mRowVecBroadcast")`。
4. 调用行：`epi_begin` L319、`epi_begin_loop` L396、`epi_end_loop` L419、`epi_end` L503。`RowVecLoad.begin` 在「每 tile 一次」阶段把广播向量 cp.async 拷进 smem 并 partition 出寄存器视图；`RowVecLoad.begin_loop` 在「每 subtile」切出本 subtile 的寄存器片段，且仅在同行首个 subtile 真正加载（[ops.py:702-717](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/ops.py#L702-L717)）。
5. 取决于 `tiled_copy_t2r is None`：若 SM90 走 `tiled_copy_t2r`（tile→register）则预绑它、`reference_src=True`；若 `tiled_copy_t2r is None`（如 SM100）则退到 `tiled_copy_r2s`、`reference_src=False`。

> 说明：本实践为源码阅读型，结论可直接从源码得出，无需 GPU；若要运行验证（例如打印编译键），需在真实环境且待本地验证。

## 6. 本讲小结

- **可组合 epilogue 的理念**是把每种张量资源抽成一个 `EpiOp`，用 `ComposableEpiMixin` 把它们组合成标准钩子；子类只需声明 `_epi_ops` 全集，mixin 自动生成 `EpilogueParams` 和全部 `epi_*` 钩子。
- **「声明是全集、执行是子集」**：`_epi_ops` 是静态 schema，运行期 `_filter_epi_ops` 把它影子覆盖成激活集；`EpilogueParams` 仍含全集字段（inactive 默认 `None`）。
- **inactive op 被过滤出编译产物有两层落地**：编译键层（`host_arg_key` 返回 `None` 则不进键、不进 cubin）+ 实例属性层（`self._epi_ops` 被覆盖，设备循环不再遍历）。
- **设备侧生命周期**是 `begin → begin_loop（每 subtile）→ end_loop（两阶段 flush）→ end`，由 `gemm_base.py` 的 epilogue 驱动循环按固定时刻调用；`epi_end_loop` 用「共享 barrier」让多 sink epilogue 每次 flush 只同步一次。
- **`EpiOp` 有两套钩子**：主机侧 torch-arg schema（`host_arg_key/host_fake_arg/host_call_arg` + `param_fields/to_params/smem_*`）与设备侧生命周期（`begin/begin_loop/end_loop_*/end`）；store 类 op（含 `DStore`）另走 `store_setup/store_convert/store_r2s`。
- **`EpiContext` 是共享工具箱**：预计算几何量并预备 `partition_for_epilogue_fn`，让所有 op 用统一方式切 tile，又不用每个钩子签名都拖一长串参数。

## 7. 下一步学习建议

- **下一步读 [u6-l2 EpiOp 词汇表]**：系统过一遍 `Scalar`、`VecLoad`、`TileStore`/`DStore`、`TileLoad`、`VecReduce`（`ColVecReduce`/`RowVecReduce`/`OnlineLSEReduce`）等具体 op 的差异，重点是 `VecReduce` 的跨 lane/warp 蝶形归约协议与 `TileStore` 的 store 路径细节。
- **再读 [u6-l3 默认线性 epilogue]**：看 `apply_linear_epilogue` 如何实现 \(D = \alpha D + \beta C + \text{rowvec} + \text{colvec}\)，以及 `GemmDefaultEpiMixin` 如何作为「手写 epilogue 的逃生出口」。
- **之后读 [u6-l4 @gemm_epilogue 函数式创作]**：看 `@gemm_epilogue` 前端如何用 `fn_port` 值端口协议（本讲提到的 `"row"`/`"col"`/`"scalar"`/`"value"`/`"apply"`/`"sink"`）把 op 接入逐元素数据流，自动生成 minted kernel 类。
- **想动手验证**：可参照 [u8-l5 测试方法] 写一个最小用例，调用 `quack.linear` 并对比「传/不传 bias」两次编译的 cubin 是否不同（通过 `QUACK_CACHE` 目录或 jit_cache 日志，待本地验证）。

> 推荐继续精读的源码：[quack/epilogue/mixin.py](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/mixin.py)（组合机制全文）、[quack/epilogue/ops.py](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/ops.py) 的 `EpiOp` 基类与 `EpiContext`、[quack/gemm_base.py](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_base.py) 的 `epilogue` 方法（驱动循环）。
