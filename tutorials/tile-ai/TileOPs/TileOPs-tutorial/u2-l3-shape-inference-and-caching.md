# 形状推断与 Kernel 缓存

## 1. 本讲目标

本讲紧接 u2-l1（Op 基类生命周期）与 u2-l2（kernel 选择与架构兼容性），把焦点收窄到一个问题：**当形状和 dtype 是「调用时才推断」的（input-inferred），TileOPs 如何决定一次调用要不要重新 JIT 编译 kernel？**

读完本讲你应该能够：

1. 解释 `_static_axes` 这个 `frozenset` 记录了什么、为什么它要在 forward 里被动态绑定。
2. 说明 `Op._cache_key` 默认实现「排除已提交轴」的投影语义，以及空 `_static_axes` + 未覆写时那条一次性 `UserWarning` 在保护什么。
3. 区分两套「kernel 复用」机制：各 op 自建的 `_kernel_cache`（真正的懒编译字典）与 `_active_sig`/`_active` 快路径；并理解 lazy build 与 eager build、fixed-rank 与 arbitrary-rank 在时序上的差异。

## 2. 前置知识

在进入源码前，先用三句话建立直觉：

- **「形状信息」分两类**：一类是**构造期已提交**的（ctor-committed，例如用户在 `__init__` 里就给出的 `normalized_shape` 或 reduction 维度的位置），另一类是**调用期才从输入张量推断**的（dynamic，例如前导维的乘积 `M`、或 batch 大小）。
- **JIT 编译很贵**：TileLang kernel 首次为某组 `(m, n, k, dtype)` 编译时，会把 GPU 源码编译成可执行 kernel，耗时远超一次 forward。因此「同一个形状反复调用时复用已编译 kernel」是性能刚需。
- **缓存 key 不能太粗也不能太细**：太粗（所有形状共用一个 key）会算错；太细（每个 distinct 输入形状各编译一份）会让缓存里堆满本可合并的 kernel——这叫 **over-fragmentation（过度碎片化）**。

> 承接 u2-l1/u2-l2：你已经知道 `__call__ → forward` 的可调用契约、`dispatch_kernel` 在构造期安装 `kernel_map`、以及 `supported_archs` 的架构 fail-fast。本讲不再重复这些，直接进入 `_static_axes`、`_cache_key`、kernel 缓存三件事。

## 3. 本讲源码地图

| 文件 | 本讲关注的内容 |
| --- | --- |
| [tileops/ops/op_base.py](https://github.com/tile-ai/TileOPs/blob/9bda1ac53758c21b0ffd25e84c6a2cfcad2aac72/tileops/ops/op_base.py) | `_static_axes` 类属性、默认 `_cache_key` 及其 over-fragmentation 警告 |
| [tileops/ops/gemm.py](https://github.com/tile-ai/TileOPs/blob/9bda1ac53758c21b0ffd25e84c6a2cfcad2aac72/tileops/ops/gemm.py) | `GemmOp._cache_key` 覆写、`_kernel_cache` 懒构建、`forward` 的 `_active_sig` 快路径 |
| [tileops/ops/bmm.py](https://github.com/tile-ai/TileOPs/blob/9bda1ac53758c21b0ffd25e84c6a2cfcad2aac72/tileops/ops/bmm.py) | `BmmFwdOp._cache_key` 另一种覆写（从形状直接抽 batch/m/n/k） |
| [tileops/ops/reduction/softmax.py](https://github.com/tile-ai/TileOPs/blob/9bda1ac53758c21b0ffd25e84c6a2cfcad2aac72/tileops/ops/reduction/softmax.py) | 任意秩算子如何在 forward 里动态绑定 `_static_axes`、并自建 `_kernel_cache` |
| [tileops/ops/reduction/cumulative.py](https://github.com/tile-ai/TileOPs/blob/9bda1ac53758c21b0ffd25e84c6a2cfcad2aac72/tileops/ops/reduction/cumulative.py) | 同上，单维 reduction 的 `_static_axes` 绑定 |
| [tileops/ops/cb_producer.py](https://github.com/tile-ai/TileOPs/blob/9bda1ac53758c21b0ffd25e84c6a2cfcad2aac72/tileops/ops/cb_producer.py) | eager build 对比组：构造期即建好 kernel，无需缓存 |
| [docs/design/manifest.md](https://github.com/tile-ai/TileOPs/blob/9bda1ac53758c21b0ffd25e84c6a2cfcad2aac72/docs/design/manifest.md) | `static_dims`（R20）spec：声明 ctor-committed 值的规约字段 |

---

## 4. 核心概念与源码讲解

### 4.1 `_static_axes`：哪些轴是「构造期已提交」的

#### 4.1.1 概念说明

`_static_axes` 是一个挂在 `Op` 类上的 `frozenset`，元素是 `(input_index, axis)` 二元组：

- `input_index`：该轴属于 `forward` 的第几个输入（对应 `_cache_key(*input_shapes)` 的位置）。
- `axis`：在该输入形状里的**非负**轴下标。

它的语义是「**这个轴已经在构造期（或更早）被提交了**，所以它对 kernel 的影响已经体现在 Op 实例本身里了」。这与 manifest 的 `static_dims`（R20）对应：任意秩算子用它声明「用户在构造时就给定的维度」。

> 为什么是 `frozenset` 而不是 `list`？因为它是集合语义（无序、去重），且要保证不可变——构造期定下来后不该被随意改写。基类默认空集，表示「没有任何轴被提交」。

#### 4.1.2 核心流程

`_static_axes` 并非总是类级常量。reduction 家族（softmax / cumsum / cumprod）的 reduction 维度**依赖于 `dim` 参数**，而 `dim` 在 PyTorch 语义里允许负数、甚至需要根据 `x.ndim` 归一化，所以「具体是哪个轴」要到 forward 里才知道。绑定流程是：

```text
forward(x)
  ├── normalize dim (dim % x.ndim，得非负轴 dim_norm)
  ├── 计算 N = x.shape[dim_norm]
  └── self._static_axes = frozenset({(0, dim_norm)})   # 第 0 个输入的第 dim_norm 轴
```

多维 reduction（logsumexp 的 `dim=list[int]`）则一次绑定多个轴。

#### 4.1.3 源码精读

基类声明默认空集，并写明它是 manifest `static_dims` 的代码侧投影：

[tileops/ops/op_base.py:51-55](https://github.com/tile-ai/TileOPs/blob/9bda1ac53758c21b0ffd25e84c6a2cfcad2aac72/tileops/ops/op_base.py#L51-L55) —— `_static_axes` 类属性：记录「ctor-committed」的 `(input_index, axis)`，默认空。

`SoftmaxFwdOp` / `CumsumFwdOp` 的实际绑定点在 forward 里。单维路径：

[tileops/ops/reduction/softmax.py:166-168](https://github.com/tile-ai/TileOPs/blob/9bda1ac53758c21b0ffd25e84c6a2cfcad2aac72/tileops/ops/reduction/softmax.py#L166-L168) —— 在 dim 归一化后，把 reduction 轴 `(0, dim)` 写入 `_static_axes`，注释点明「param-dependent（依赖 `dim`），所以在 forward 时绑定而非类级绑定」。

多维路径（`dim` 为列表时）一次绑定多个轴：

[tileops/ops/reduction/softmax.py:128-131](https://github.com/tile-ai/TileOPs/blob/9bda1ac53758c21b0ffd25e84c6a2cfcad2aac72/tileops/ops/reduction/softmax.py#L128-L131) —— `_static_axes = frozenset((0, d) for d in dims)`。

cumulative 家族走同一套：

[tileops/ops/reduction/cumulative.py:104-106](https://github.com/tile-ai/TileOPs/blob/9bda1ac53758c21b0ffd25e84c6a2cfcad2aac72/tileops/ops/reduction/cumulative.py#L104-L106) —— `dim` 归一化后绑定 `(0, dim_norm)`。

> **诚实说明**：`static_dims` 是 manifest 的 spec 字段（[docs/design/manifest.md](https://github.com/tile-ai/TileOPs/blob/9bda1ac53758c21b0ffd25e84c6a2cfcad2aac72/docs/design/manifest.md) R20，§`static_dims`），由 `scripts/validate_manifest.py` 校验。但**当前仓库里还没有任何 manifest YAML 实际声明 `static_dims`**——所以 reduction 家族是「在 forward 里命令式地绑定 `_static_axes`」，而不是从 manifest 读取。这二者表达的是同一件事实（哪个轴被提交了），只是来源不同。

#### 4.1.4 代码实践

**实践目标**：亲眼看到「`dim` 决定 `_static_axes`」的动态绑定。

**操作步骤**（需要 CUDA Hopper 机器；若无 GPU，做下方源码阅读型变体）：

1. 启动交互式 Python，`from tileops.ops import SoftmaxFwdOp`。
2. `op = SoftmaxFwdOp(dim=-1)`。
3. 先 `print(op._static_axes)`（应为空集，因为还没调用过 forward）。
4. 构造 `x = torch.randn(4, 8, 16, dtype=torch.float16, device="cuda")`，调用 `y = op(x)`。
5. 再次 `print(op._static_axes)`，应得到 `frozenset({(0, 2)})`（`-1` 归一化为轴 2）。

**预期结果**：构造时 `_static_axes` 为空；首次 forward 后变成 `frozenset({(0, 2)})`。换 `dim=1` 再调一次，应看到它变成 `frozenset({(0, 1)})`。

**源码阅读型变体（无 GPU）**：直接对照 [softmax.py:149-168](https://github.com/tile-ai/TileOPs/blob/9bda1ac53758c21b0ffd25e84c6a2cfcad2aac72/tileops/ops/reduction/softmax.py#L149-L168)，用 `x.ndim=3`、`dim=-1` 手算 `dim_norm = (-1) % 3 = 2`，确认 `(0, 2)` 就是结果。**待本地验证**运行型步骤。

#### 4.1.5 小练习与答案

**练习 1**：`SoftmaxFwdOp(dim=-1)` 对 5 维输入 `x`，forward 后 `_static_axes` 是什么？

**参考答案**：`(-1) % 5 = 4`，所以是 `frozenset({(0, 4)})`。

**练习 2**：为什么 `_static_axes` 不在类定义里写死，而要在 forward 里绑定？

**参考答案**：因为 reduction 轴依赖于构造参数 `dim`，而 `dim` 允许负值、且只有知道 `x.ndim` 后才能归一化成非负轴。类定义时还不知道运行时输入是几维，无法确定 `(input_index, axis)` 的第二个分量。

---

### 4.2 `_cache_key`：把输入形状投影到 kernel 真正依赖的维度

#### 4.2.1 概念说明

`_cache_key(*input_shapes)` 的任务是：**给定 forward 时的输入形状，返回一个 hashable 的 key，代表「需要为它专门编译一个 kernel」的最小维度集合**。它是 Op 层的「投影契约」。

默认实现的投影规则是「排除已提交轴」：凡是出现在 `_static_axes` 里的轴都被丢掉，因为它们已经体现在 Op 实例里（实例本身就已经被这组 committed 值「特化」了），再放进 key 是冗余的。形式化地：

\[
\text{key}_{\text{default}} = \big\langle\, s_{i,a} \;\big|\; (i,a) \notin \text{\_static\_axes} \,\big\rangle
\]

其中 \(s_{i,a}\) 是第 \(i\) 个输入、第 \(a\) 轴的尺寸，\(\langle\cdot\rangle\) 表示按 `(i, a)` 顺序拼成的元组。

子类可以覆写它，把任意秩的形状**投影**到 kernel 数学真正依赖的那几个维度（例如 GEMM 投影到 `(m, n, k)`、softmax 投影到 `(M, N)`），从而让「形状不同但 kernel 等价」的调用共用一份编译产物。

#### 4.2.2 核心流程

默认 `_cache_key` 做两件事：

1. **守卫**：若 `_static_axes` 为空 **且** 子类没有覆写 `_cache_key`（`type(self)._cache_key is Op._cache_key`），就发一次 `UserWarning`——因为此时默认 key 退化成「完整扁平化的输入形状」，在动态输入下会 over-fragment（每个 distinct 形状各编译一份）。这条警告用模块级集合 `_EMPTY_STATIC_DIMS_WARNED` 按 Op 子类去重，只报一次。
2. **投影**：遍历所有输入形状，挑出不在 `_static_axes` 里的轴尺寸，拼成 tuple 返回。

#### 4.2.3 源码精读

默认实现与守卫逻辑：

[tileops/ops/op_base.py:231-250](https://github.com/tile-ai/TileOPs/blob/9bda1ac53758c21b0ffd25e84c6a2cfcad2aac72/tileops/ops/op_base.py#L231-L250) —— 先判断「空 `_static_axes` + 未覆写」决定是否报警，再用生成器表达式过滤掉 static 轴。

去重集合：

[tileops/ops/op_base.py:13](https://github.com/tile-ai/TileOPs/blob/9bda1ac53758c21b0ffd25e84c6a2cfcad2aac72/tileops/ops/op_base.py#L13) —— `_EMPTY_STATIC_DIMS_WARNED` 按 Op 子类去重，避免每个实例/每次调用都刷屏。

`GemmOp` 的覆写——直接投影到 `(m, n, k, trans, dtype)`：

[tileops/ops/gemm.py:88-91](https://github.com/tile-ai/TileOPs/blob/9bda1ac53758c21b0ffd25e84c6a2cfcad2aac72/tileops/ops/gemm.py#L88-L91) —— 注意它返回的是**已经推断好的逻辑维** `self.m/n/k`，而不是原始 `a.shape/b.shape`。这正是投影的意义。

`BmmFwdOp` 的覆写——从原始 3D 形状里直接抽 `(batch, m, n, k)`：

[tileops/ops/bmm.py:94-103](https://github.com/tile-ai/TileOPs/blob/9bda1ac53758c21b0ffd25e84c6a2cfcad2aac72/tileops/ops/bmm.py#L94-L103) —— 当两个输入都是 3D 时直接拆解；否则回退到已绑定的 `self.batch/m/n/k`。

> **诚实说明**：`_cache_key` 目前是**声明式契约**——`GemmOp`/`BmmFwdOp` 都正确地覆写了它，但本仓库当前的 `forward` 并不会去调用 `op._cache_key(...)`；真正用于「按形状复用已编译 kernel」的运行时缓存 key，是各 op 在 `_get_kernel` 里自己拼的元组（见 4.3）。`_cache_key` 面向 Op 层的 introspection / codegen 消费者，与运行时 cache key 在**概念上对齐**（都表达「kernel 依赖哪些维度」）但**各自独立**。那条 over-fragmentation 警告是「契约若被调用」时的护栏。

manifest spec 把这条契约写得很明确：空 `static_dims` 时作者**必须**覆写 `_cache_key`：

[docs/design/manifest.md:210](https://github.com/tile-ai/TileOPs/blob/9bda1ac53758c21b0ffd25e84c6a2cfcad2aac72/docs/design/manifest.md#L210) —— 「When `static_dims` is empty, the Op author MUST override `_cache_key`.」

#### 4.2.4 代码实践（本讲主线实践）

**实践目标**：对比默认 `_cache_key` 与 `GemmOp._cache_key`，理解投影到 `(m, n, k, trans, dtype)` 的动机与收益。

**操作步骤（源码阅读 + 手算）**：

1. 读 [op_base.py:245-250](https://github.com/tile-ai/TileOPs/blob/9bda1ac53758c21b0ffd25e84c6a2cfcad2aac72/tileops/ops/op_base.py#L245-L250)，确认默认 key = 完整扁平化的输入形状。
2. 读 [gemm.py:88-91](https://github.com/tile-ai/TileOPs/blob/9bda1ac53758c21b0ffd25e84c6a2cfcad2aac72/tileops/ops/gemm.py#L88-L91)，确认 GemmOp 返回 `(self.m, self.n, self.k, self.trans_a, self.trans_b, str(self.dtype))`。
3. **场景推演**：假设对同一个 `GemmOp(trans_b=True)`（NT 布局）依次调用：
   - `op(a[16,64], b[32,64])` → 逻辑维 `(m=16, n=32, k=64)`
   - `op(a[8,64], b[32,64])` → 逻辑维 `(m=8, n=32, k=64)`
4. 分别写出两种 key：
   - **默认** key（若不覆写）会带上 `a.shape`、`b.shape` 的原始尺寸，于是 `[16,64],[32,64]` 与 `[8,64],[32,64]` 是两条不同的 key → 两次 JIT 编译。
   - **GemmOp 覆写** 后 key 只剩 `(m, n, k, trans, dtype)`，`m` 不同 → 仍是两条 key → 也两次编译（这是**正确**的，因为 GEMM kernel 确实按 `m` 特化）。
5. **关键收益场景**：GEMM 本身是固定 2D 秩，没有「前导维可合并」的余地；投影的真正价值在**任意秩**场景。设想一个把 `[2,3,N]` 和 `[6,N]` 都映射到 `M=6, N` 的 reduction kernel——默认 key 会因 `[2,3,N]` ≠ `[6,N]`（或 `[2,3,N]` ≠ `[1,6,N]`）而 over-fragment，而一个投影到 `(M, N)` 的覆写会让它们共用同一份编译产物。

**预期结果**：你能口述「投影 = 把原始形状坍缩到 kernel 真正特化的维度」，并解释为什么 `m` 变化时无论覆写与否都会触发新编译（这是必需的），而「前导维乘积相同」时只有覆写才能省下重复编译。

> **若无法本地运行**：本实践以源码阅读 + 手算为主，不强依赖 GPU。运行型验证见 4.3.4。

#### 4.2.5 小练习与答案

**练习 1**：默认 `_cache_key` 为什么要把 `_static_axes` 里的轴**排除**，而不是**包含**？

**参考答案**：static 轴是「构造期已提交」的，它的值已经体现在 Op 实例本身（实例被这组 committed 值特化）。把它放进 key 是冗余——同一个实例里这个轴的值恒定不变，对「区分不同 kernel」没有贡献，反而把 key 变长。排除后 key 只保留「调用间会变化」的 dynamic 维度。

**练习 2**：`GemmOp._cache_key` 返回 `(m, n, k, trans_a, trans_b, str(dtype))`。为什么 `dtype` 要转成字符串 `str(self.dtype)` 再放进 key？

**参考答案**：`torch.dtype` 对象本身是可哈希的，但跨 TileLang/JIT 边界时，字符串形式（如 `"float16"`）是更稳定、更可读的特化标识；kernel 配置（`dtype_str`）也是按字符串分派的（见 [kernel_base.py:49-56](https://github.com/tile-ai/TileOPs/blob/9bda1ac53758c21b0ffd25e84c6a2cfcad2aac72/tileops/kernels/kernel_base.py#L49-L56)）。用 `str(dtype)` 保证 key 与 kernel 的 dtype 分派口径一致。

---

### 4.3 kernel 缓存：lazy build 与「同签名」快路径

#### 4.3.1 概念说明

TileOPs 的 kernel 复用有**两套叠加**的机制，不要混淆：

| 机制 | 数据结构 | 作用 | 命中时跳过什么 |
| --- | --- | --- | --- |
| **懒构建缓存** | `self._kernel_cache: dict` | 「这组形状的 kernel 编译过没？没有就建一份」 | 重复 JIT 编译 |
| **同签名快路径** | `self._active_sig` / `self._active` | 「这次调用的形状和**上一次**完全一样吗？」 | dtype 校验 + 形状推断 + 缓存查表 |

第一层解决「跨多次不同形状调用」的复用；第二层解决「连续相同形状调用」（serving / benchmarking 的稳态）时把 Python 开销压到最低。

#### 4.3.2 核心流程

**lazy build**（以 `GemmOp` 为例）：

```text
forward(a, b)
  sig = (a.shape, b.shape, a.dtype)
  if sig != self._active_sig:          # 签名变了，走「完整路径」
      _validate_dtypes(a, b)
      m, n, k = _infer_mnk(a, b)       # 推断逻辑维
      mode, kernel = _get_kernel(m,n,k,dtype)   # ↓ 懒构建
          key = (mode/"gemm", m, n, k, dtype)
          if key not in self._kernel_cache:
              kernel = GemmKernel(m,n,k,dtype, trans_a, trans_b, tune=...)   # 真正编译
              self._kernel_cache[key] = kernel
          return kernel
      self._active = (mode, kernel, n, m)
      self._active_sig = sig           # 记住这次签名
  # 快路径（签名未变）直接落到这里
  mode, kernel, n, m = self._active
  return kernel(a, b)                  # 直接跑已编译 kernel
```

注意 `_get_kernel` 的 key 与 `_cache_key` **不是同一个东西**——前者是运行时真实用的缓存 key（含 `mode`、用 `dtype` 对象），后者是 Op 层投影契约（4.2）。两者表达同一意图但各自独立。

#### 4.3.3 源码精读

**懒构建缓存**的初始化与查询——`GemmOp._get_kernel`：

[tileops/ops/gemm.py:113-119](https://github.com/tile-ai/TileOPs/blob/9bda1ac53758c21b0ffd25e84c6a2cfcad2aac72/tileops/ops/gemm.py#L113-L119) —— 通用 GEMM 分支：`key=("gemm", m,n,k,dtype)`，缓存未命中时构造 `GemmKernel(...)` 并写回 `_kernel_cache`。

GEMV 快路径分支（`m==1` 或 `n==1`）走自己的 key：

[tileops/ops/gemm.py:100-111](https://github.com/tile-ai/TileOPs/blob/9bda1ac53758c21b0ffd25e84c6a2cfcad2aac72/tileops/ops/gemm.py#L100-L111) —— GEMV 用 `(mode, m,n,k,dtype)` 作 key，且 kernel 构造参数与通用分支不同（按 `n` 或 `m` 建）。

`_kernel_cache` 与 `_active_sig`/`_active` 的初始化在 `__init__`：

[tileops/ops/gemm.py:56-61](https://github.com/tile-ai/TileOPs/blob/9bda1ac53758c21b0ffd25e84c6a2cfcad2aac72/tileops/ops/gemm.py#L56-L61) —— 构造时空字典 + 空签名。

**同签名快路径**——`forward` 的入口判断：

[tileops/ops/gemm.py:126-146](https://github.com/tile-ai/TileOPs/blob/9bda1ac53758c21b0ffd25e84c6a2cfcad2aac72/tileops/ops/gemm.py#L126-L146) —— `sig=(a.shape,b.shape,a.dtype)`；与 `self._active_sig` 不同才重走校验/推断/取 kernel，否则直接用 `self._active`。

softmax 的同款缓存（任意秩算子的写法）：

[tileops/ops/reduction/softmax.py:222-229](https://github.com/tile-ai/TileOPs/blob/9bda1ac53758c21b0ffd25e84c6a2cfcad2aac72/tileops/ops/reduction/softmax.py#L222-L229) —— `_get_or_create_kernel` 用 `(M, N, dtype, device_index)` 作 key。注意这里**没有** `_active_sig` 快路径，因为 softmax 的 forward 还要做 dim 归一化、reshape、padding 裁剪等每次都不同的工作，收益不如 GEMM 明显。

**lazy build vs eager build**——对比组 `CBProducerOp`：

[tileops/ops/cb_producer.py:44-55](https://github.com/tile-ai/TileOPs/blob/9bda1ac53758c21b0ffd25e84c6a2cfcad2aac72/tileops/ops/cb_producer.py#L44-L55) —— 它把 `batch/num_chunks/n_groups/chunk_len/d_state` 全部作为构造参数，因此在 `__init__` 里**直接** `self.kernel = self.kernel_map["cb_producer"](...)` 就建好了，**没有 `_kernel_cache`**：一个实例对应一个固定形状的 kernel。这正是 eager build——形状在构造期就完全确定，无需延迟到首次 forward。

#### 4.3.4 代码实践

**实践目标**：用计时观察「首次编译 vs 缓存命中」的巨大差异，亲手验证 lazy build + 快路径。

**操作步骤**（需 CUDA Hopper 机器）：

1. `from tileops.ops import GemmOp`；`import torch, time`。
2. `op = GemmOp()`（NT 默认）；`a = torch.randn(512, 1024, dtype=torch.float16, device="cuda"); b = torch.randn(256, 1024, dtype=torch.float16, device="cuda")`。
3. 计时首次调用：`torch.cuda.synchronize(); t0=time.perf_counter(); d=op(a,b); torch.cuda.synchronize(); print("first:", time.perf_counter()-t0)` —— 首次会触发 JIT 编译，明显偏慢。
4. 计时第二次（同签名）：同样计时 `op(a,b)` —— 应**快得多**（缓存命中 + 快路径，跳过校验/推断/查表）。
5. 换形状：`a2 = torch.randn(1024, 1024, device="cuda", dtype=torch.float16); b2 = torch.randn(256, 1024, device="cuda", dtype=torch.float16)`，计时 `op(a2,b2)` —— 会慢一次（新形状 → 新 key → 编译），随后再调 `op(a2,b2)` 又变快。
6. **回切验证快路径**：再次 `op(a,b)` —— 因为 `_active_sig` 此时指向 `a2/b2`，这次签名又变了，会走完整路径但**命中 `_kernel_cache`**（不重新编译，但仍重做校验/推断）。仔细体会「`_kernel_cache` 命中」与「`_active_sig` 快路径命中」的区别。

**预期结果**：首次 ≫ 第二次（同形状）；换形状后第一次 > 其后同形状；在两套形状间来回切时，每个形状的「第一次」之后都不再编译，但只要不是「连续相同」，仍会重走校验/推断。

> **待本地验证**：具体耗时取决于机器与 TileLang 版本；关键是观察**相对**趋势（首次显著偏慢，同签名显著变快），而非绝对数字。无 GPU 时可改做源码阅读型实践：在 [gemm.py:126-146](https://github.com/tile-ai/TileOPs/blob/9bda1ac53758c21b0ffd25e84c6a2cfcad2aac72/tileops/ops/gemm.py#L126-L146) 上画流程图，标注「签名变/不变」「缓存命中/未命中」四个分支各自跳过的步骤。

#### 4.3.5 小练习与答案

**练习 1**：`GemmOp.autotune()` 为什么不复用基类 `Op.autotune()`，而是自己实现？

**参考答案**：基类 `Op.autotune()` 用 `dir(self)` 扫描实例属性找 `Kernel`（见 [op_base.py:199-204](https://github.com/tile-ai/TileOPs/blob/9bda1ac53758c21b0ffd25e84c6a2cfcad2aac72/tileops/ops/op_base.py#L199-L204)）。但 `GemmOp` 把 kernel 存在 `self._kernel_cache` 字典里（不是直接属性），`dir(self)` 看不到字典内部的值。所以它覆写为遍历 `self._kernel_cache.values()` 逐个 tune（见 [gemm.py:148-156](https://github.com/tile-ai/TileOPs/blob/9bda1ac53758c21b0ffd25e84c6a2cfcad2aac72/tileops/ops/gemm.py#L148-L156)）。`BmmFwdOp` 同理。

**练习 2**：lazy build（`GemmOp`）和 eager build（`CBProducerOp`）各自适合什么场景？

**参考答案**：
- **lazy build** 适合**形状在调用时才确定**（input-inferred）的算子。它为每组出现过的形状按需编译并缓存，代价是首次调用的编译延迟、以及需要维护 `_kernel_cache`。
- **eager build** 适合**所有维度都在构造期给定**的算子（如 `CBProducerOp` 的 `batch/num_chunks/...`）。形状确定 → 实例与 kernel 一一对应，没有「按形状复用」的需求，因此不需要缓存，构造完即可用。

**练习 3**（fixed-rank vs arbitrary-rank 时序）：`GemmOp` 与 `SoftmaxFwdOp` 推断「kernel 依赖的维度」的时机有何不同？

**参考答案**：
- `GemmOp`（固定 2D 秩）：一进 forward 就能用 `_infer_mnk` 从 `a.shape/b.shape` 直接推出 `(m,n,k)`，`_static_axes` 在此场景不需要（它选择**覆写 `_cache_key`** 而非设置 static axes）。
- `SoftmaxFwdOp`（任意秩）：必须先归一化 `dim`（依赖 `x.ndim`）、可能还要 `flatten_for_multidim`，才能算出 `N = x.shape[dim]` 和 `M = 前导维乘积`；`_static_axes` 也要等 `dim_norm` 确定后才能在 forward 里绑定。所以任意秩算子的「提交」动作天然延迟到调用期。

---

## 5. 综合实践

**任务**：给一个假想的任意秩 elementwise-reduce 算子「`MeanFwdOp(dim)`」，设计它的三件套——`_static_axes`、`_cache_key`、`_kernel_cache` key——并用一段伪代码串起来。

要求：

1. 假设 kernel 只依赖 `(M, N, dtype)`，其中 `N = x.shape[dim]`，`M = 其余维乘积`。
2. 写出 `_static_axes` 应在 forward 哪一步绑定、绑定成什么。
3. 写出 `forward` 里 `_active_sig` 快路径的判断与 `_get_or_create_kernel` 的 key。
4. 说明：为什么这个算子**既**要设 `_static_axes`（或覆写 `_cache_key`），**又**要自建 `_kernel_cache`？二者分别防什么？

**参考思路**（自己先写，再对照）：

- `_static_axes`：在 `dim` 归一化后 `self._static_axes = frozenset({(0, dim_norm)})`（照搬 softmax 的写法）。
- `_active_sig`：`sig = (x.shape, x.dtype)`，与上次不同才重算 `M/N` 并取 kernel。
- `_kernel_cache` key：`(M, N, dtype, device_index)`，命中即复用。
- **为何两套都要**：`_static_axes`/`_cache_key` 是 Op 层「投影契约」，告诉 introspection/codegen「kernel 只依赖 `(M,N)` 而非原始形状」——避免 over-fragmentation 警告、让 `[2,3,N]` 与 `[6,N]` 在投影层被视作等价；`_kernel_cache` 是运行时真实复用已编译 kernel 的字典。前者描述「应该按什么维度特化」，后者执行「按这些维度复用编译产物」。两者概念对齐、职责不同。

---

## 6. 本讲小结

- **`_static_axes`** 是 `frozenset[(input_index, axis)]`，记录「构造期已提交」的轴；reduction 家族因 `dim` 参数依赖，在 forward 里动态绑定。
- **`_cache_key`** 默认实现「排除 static 轴」的投影；空 `_static_axes` + 未覆写会触发一次 over-fragmentation 警告；`GemmOp`/`BmmFwdOp` 把它投影到 `(m,n,k[,batch],trans,dtype)`。
- **`_cache_key` 当前是声明式契约**：正确覆写了，但 `forward` 真正用的运行时缓存 key 是各 op 在 `_get_kernel` 里自拼的元组；二者概念对齐、各自独立。
- **kernel 复用有两层**：`_kernel_cache`（跨不同形状的懒构建复用）+ `_active_sig`/`_active`（连续同形状的快路径，跳过校验/推断/查表）。
- **lazy build vs eager build**：input-inferred 用 `_kernel_cache` 懒构建（GemmOp）；形状全在构造期给定则 `__init__` 直接建（CBProducerOp）。
- **fixed-rank vs arbitrary-rank 时序**：固定秩一进 forward 就能推断逻辑维；任意秩要等 `dim` 归一化/flatten 后才能确定 `M/N` 与 `_static_axes`。

## 7. 下一步学习建议

- **跟读一次完整 forward**：进入 u2-l4「跟读 GemmOp 完整链路」，把本讲的两层缓存放进整条 `__init__ → forward` 调用链里通读一遍，特别注意 `_active_sig` 快路径与 `eval_roofline` 的绑定时序。
- **交叉验证 autotune**：回顾 [kernel_base.py:124-155](https://github.com/tile-ai/TileOPs/blob/9bda1ac53758c21b0ffd25e84c6a2cfcad2aac72/tileops/kernels/kernel_base.py#L124-L155) 的 `autotune`，理解 `do_not_specialize` 如何防止「种子 JIT 参数污染缓存键」——与本讲的「key 该含什么」是同一主题在 Kernel 侧的延续。
- **衔接 manifest**：U4（manifest）会正式讲 `static_dims`（R20）与 `shape_rules`，届时你会看到 `_static_axes` 在规约侧的对应物，以及「空 `static_dims` 必须覆写 `_cache_key`」这条 spec 如何被 `validate_manifest.py` 守护。
