# 维度参数化家族（pool / reduction）

## 1. 本讲目标

本讲承接 u11-l1（家族基类与三层继承 T1/T2）与 u11-l2（elementwise 三大伞形基类），把「家族复用」推进到一类更难的场景：**同一个算子族在「空间维数」上天然分裂为 1d/2d/3d 三个变体**。

学完本讲你应当掌握：

1. 理解 **ndim 泛化基类**——如何用一个挂载 `ndim: ClassVar[int]` 类属性的基类，把 MaxPool/AvgPool 的 1d/2d/3d 三个变体合并成同一套 `forward` 流水线，差异只落在类属性与查表上。
2. 理解 **变体类属性**（`_kernel_slot`、`_returns_indices`、`_generic_slot`/`_spatial_slot`）如何区分同一族内的不同变体，以及本轮重构为何把 indexed max-pool 的 `forward` 从「`__init_subclass__` 工厂自动生成」改回「子类显式声明」（剥离过度设计）。
3. 理解 **表驱动命名**——kernel 构造关键字（`kernel_w`/`stride_h`/`pad_d`…）由按 ndim 索引的字典生成、永不走位置参数，以及为何 kernel-cache key 与构造关键字名在重构中必须保持不变。
4. 能对照 reduction 家族的 `_multidim` / `_primitives` 共享原语，说出「任意秩」与「固定空间秩」两种参数化策略的取舍。
5. 说清 `MeanPoolingForwardOp` 为何从 attention 家族迁入 pool 家族（路径与归属），却又不参与 ndim 泛化机制。

---

## 2. 前置知识

本讲默认你已建立以下心智模型（来自前置讲义）：

- **Op(L2)/Kernel(L1) 双层分离**（u1-l1）：Op 是主机侧无状态入口，Kernel 是 TileLang GPU 实现。
- **可调用契约与 dispatch_kernel**（u1-l4、u2-l1）：`op = XxxOp(...)` → `op(*inputs)`，`__call__` 转发 `forward`，构造期经 `dispatch_kernel` 安装 `kernel_map`。
- **编译边界不变量**（u10-l1）：被 dynamo 追踪的 `forward` 不得构造 Kernel；pool 家族用 `compile_boundary` 的 weak 字符串-key 注册表把「查 cache → 构造 → launch」藏进 custom_op 的 eager 体，`forward` 收敛成一行分发 `_pool_fwd(input, self._instance_key)`。
- **family-base 重构的两个维度**（u11-l1）：继承位置 L1/L2/L3（Op → 可选 FamilyBase → ConcreteOp），重构形态 T1/T2。两个 codegen 契约 `_validate_dtypes` 与 `eval_roofline` 必须 **class-local**（落在具体类 `__dict__`），不能靠继承传递。
- **伞形基类的模板化复用**（u11-l2）：`kernel_cls` + `_op_name` 类属性让叶子类退化为薄包装。

本讲新增的核心直觉是：**当一个家族的差异恰好是「维数」时，最高效的参数化不是继承更多层，而是把维数塞进一个类属性 `ndim`，让流水线代码用 `nd = self.ndim` 统一驱动**。这与 elementwise 的「伞形基类按算子语义分（Unary/Binary/FusedGated）」是正交的另一条复用轴。

### 关键术语速查

| 术语 | 含义 |
|------|------|
| ndim 泛化基类 | 用 `ndim: ClassVar[int]` 类属性参数化空间秩的家族基类，子类只填 `ndim=1/2/3` |
| 变体类属性 | `_kernel_slot`、`_returns_indices` 等类属性，用来在共享基类内区分同一族的不同变体 |
| 表驱动命名 | kernel 构造关键字名由按 ndim 索引的字典（如 `_POOL_DIM_NAMES`）查表生成 |
| indexed max-pool | `return_indices=True` 的 max-pool，输出 `(value, argmax)` 二元组 |
| 归属（belonging） | 算子在目录/家族上的组织归属，区别于是否参与某种机制 |

---

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [tileops/ops/pool.py](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/pool.py) | pool 家族的全部 Op：两个 ndim 泛化基类（`_AvgPoolFwdOpBase`/`_MaxPoolFwdOpBase`）+ 9 个具体叶子 + 迁入的 `MeanPoolingForwardOp` + 两个 compile-boundary custom_op |
| [tileops/ops/reduction/_multidim.py](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/reduction/_multidim.py) | reduction 家族「任意秩」共享原语：`normalize_dim` / `flatten_for_multidim` / `restore_multidim_shape` |
| [tileops/kernels/reduction/_primitives.py](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/kernels/reduction/_primitives.py) | reduction kernel 层共享原语：对齐常量、`compute_tile_n`、`make_welford_update`/`make_softmax_epilogue`/`make_cumulative_scan` 等 `T.macro` 工厂 |
| [tileops/ops/reduction/reduce.py](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/reduction/reduce.py) | reduction 家族的类属性 + hooks 基类 `_ReduceOpBase`，与 pool 的 ndim 策略相对照 |
| [tileops/ops/op_base.py](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/op_base.py) | `dispatch_kernel` 内调用 `register_instance`，把 `id(op)` 字符串 key 写入 weak 注册表（编译边界） |
| [tileops/ops/compile_boundary.py](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/compile_boundary.py) | `_OP_REGISTRY`（WeakValueDictionary）与 `register_instance`/`get_instance` |
| [tileops/manifest/pool.yaml](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/manifest/pool.yaml) | pool 家族的 spec-driven 契约来源（注意 `MeanPoolingForwardOp` 不在其中） |

---

## 4. 核心概念与源码讲解

### 4.1 ndim 泛化基类：用一个类属性统一 1d/2d/3d

#### 4.1.1 概念说明

PyTorch 把池化按空间维数拆成三个独立函数：`avg_pool1d` / `avg_pool2d` / `avg_pool3d`、`max_pool1d` / `max_pool2d` / `max_pool3d`。它们的数学语义完全一致（在滑动窗口上做 reduce），只在三处随维数变化：

1. **输入张量的秩**：1d 是 NCL（秩 3），2d 是 NCHW（秩 4），3d 是 NCDHW（秩 5），即秩 = `ndim + 2`。
2. **池化参数的长度**：`kernel_size`/`stride`/`padding` 是长度为 ndim 的元组。
3. **kernel 构造关键字的名字**：1d 叫 `kernel_w`，2d 叫 `kernel_h`/`kernel_w`，3d 叫 `kernel_d`/`kernel_h`/`kernel_w`。

如果为每个变体都写一份完整的 `forward`，就会有 6 份几乎相同的代码。TileOPs 的做法是：**把维数抽成一个类属性 `ndim`，让一份流水线代码用 `nd = self.ndim` 驱动这三处差异**。这就是「ndim 泛化基类」——它不是用继承层数表达差异（u11-l1 的 L2），而是用一个标量类属性表达差异。

> 直觉：差异是「几维」这种**连续可参数化**的量时，类属性比继承更省；差异是「完全不同的 forward 流程」时，才抽 L2 家族基类。

pool 家族有**两个**这样的泛化基类：`_AvgPoolFwdOpBase`（avg 多一条 spatial 快路径）与 `_MaxPoolFwdOpBase`。它们的形状几乎对称，本节以两者共有的骨架讲解。

#### 4.1.2 核心流程

一次 `op(input)` 调用（以 max-pool 为例）的执行链：

```
op.__call__(input)
  └─ forward(input)                                   # 被 dynamo 追踪，只做一行分发
       └─ _pool_fwd(input, self._instance_key)        # torch.library.custom_op（compile 边界）
            eager 体 → get_instance(key)._eager_forward(input)
              └─ _eager_forward(input):
                   1. _resolve_input(input)            # 校验秩 == nd+2、dtype、算 out_dims
                   2. _get_kernel(n, c_in, in_dims, dtype, dev)  # 表驱动构造 + 缓存
                   3. self.kernel = kernel             # 挂载，供 eval_roofline 读 spec
                   4. return kernel(input)             # 真正 launch
            fake 体 → _infer_output_shapes(shape)      # fullgraph 的 meta 推导
```

关键点：被追踪的 `forward` 永不构造 Kernel（编译边界不变量，见 u10-l1）；构造藏在 `_eager_forward` 里，由 custom_op 在 eager 模式下调起。整条链里**唯一随 ndim 变化的是 `_resolve_input`/`_get_kernel` 内的维度处理**，其余完全共享。

#### 4.1.3 源码精读

**(1) 基类用 `ndim: ClassVar[int]` 声明参数化轴。** 两个基类的开头一致：类文档明确说「parametrized by class-attribute `ndim`」，并提醒两个 codegen 契约必须留在子类体内（承接 u11-l1）。

[tileops/ops/pool.py:445-457](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/pool.py#L445-L457) —— `_MaxPoolFwdOpBase` 的类属性声明（`ndim`/`_kernel_slot`/`_returns_indices` 三个开关）。

```python
class _MaxPoolFwdOpBase(Op):
    """Generic max-pooling forward, parametrized by class attributes.

    Concrete subclasses set ``ndim`` / ``_kernel_slot`` / ``_returns_indices``,
    supply ``default_kernel_map``, and keep ``eval_roofline`` /
    ``_validate_dtypes`` in their own class body so manifest codegen resolves
    them per concrete class.
    """
    ndim: ClassVar[int]
    _kernel_slot: ClassVar[str] = ""
    _returns_indices: ClassVar[ False
```

`(2) 构造期用 `nd = self.ndim` 驱动一切**：归一化池化参数到 nd 元组、初始化 `*_in` 形状槽位。

[tileops/ops/pool.py:458-497](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/pool.py#L458-L497) —— `__init__` 把标量参数 `_normalize_pool_dims(..., nd)` 展开为 nd 元组，并按 `_POOL_DIM_NAMES[nd]` 给每个空间轴建一个 `f"{name}_in"` 槽位。

```python
nd = self.ndim
for name in _POOL_DIM_NAMES[nd]:
    setattr(self, f"{name}_in", None)
self.kernel_size = _normalize_pool_dims("kernel_size", kernel_size, nd)
self.stride = self.kernel_size if stride is None else _normalize_pool_dims("stride", stride, nd)
```

**(3) `_resolve_input` 用 `nd + 2` 校验输入秩并算输出维。** 这是 ndim 参数化最直接的体现：1d 期望秩 3（NCL），3d 期望秩 5（NCDHW）。

[tileops/ops/pool.py:499-526](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/pool.py#L499-L526) —— `if input.ndim != nd + 2` 报错信息直接引用 `_POOL_LAYOUTS[nd]`（`"NCL"`/`"NCHW"`/`"NCDHW"`），并用 `pool_output_dim(...)` 逐轴算输出尺寸。

**(4) 被 dynamo 追踪的 `forward` 收敛成一行。** 基类提供具体的 `forward`，把活儿全交给编译边界后的 `_eager_forward`。

[tileops/ops/pool.py:590-611](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/pool.py#L590-L611) —— `forward` 一行分发；`_eager_forward` 才真正解析输入、取 kernel、launch。

```python
def forward(self, input: torch.Tensor) -> torch.Tensor:
    return _pool_fwd(input, self._instance_key)

def _eager_forward(self, input):
    resolved = self._resolve_input(input)
    ...
    kernel = self._get_kernel(n, c_in, in_dims, dtype, _device_index(input))
    self.kernel = kernel
    ...
    return kernel(input)
```

**(5) 具体叶子只填类属性。** 三个 max-pool 叶子（不带 indices）的差异仅是 `ndim` 与 `_kernel_slot` 两个类属性，`forward`/`__init__`/`_get_kernel` 全部继承自基类。

[tileops/ops/pool.py:774-809](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/pool.py#L774-L809) —— `MaxPool3dFwdOp` 整个类体几乎只有 `ndim = 3`、`_kernel_slot = "max_pool3d_kernel"`、`default_kernel_map` 与一行 `eval_roofline` 转发。

> 这正是 T1「瘦叶子」形态（u11-l1）：基类是 L2 家族基类，叶子只填类属性。与 u11-l2 elementwise 的 `kernel_cls` + `_op_name` 模板化是同一种思想，只是这里参数化的是「维数」而非「算子名」。

#### 4.1.4 代码实践

**实践目标**：确认 1d/2d/3d 三个 max-pool 共享同一份 `forward`/`_eager_forward`/`_get_kernel`，差异只落在 `ndim`。

**操作步骤（源码阅读型，无需 GPU）**：

1. 打开 [tileops/ops/pool.py](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/pool.py)，定位 `MaxPool1dFwdOp`、`MaxPool2dFwdOp`、`MaxPool3dFwdOp`（行 614、694、774 附近）。
2. 对每个类，列出它在类体内**自己定义**的方法（排除继承来的）。预期你会看到：只有 `__init__`、`default_kernel_map`、`eval_roofline`，以及两个类属性 `ndim`/`_kernel_slot`。三者都没有重写 `forward`/`_eager_forward`/`_get_kernel`/`_resolve_input`。
3. 在 `_MaxPoolFwdOpBase._resolve_input` 里找到 `input.ndim != nd + 2` 这一行，分别代入 `nd=1/2/3`，验证它要求输入秩为 3/4/5。

**需要观察的现象**：三个类的「自身体积」极小——所有重活都在基类里，子类只是「填一张配置表」。

**预期结果**：你能用一句话描述三个类的差异——「`ndim` 与 `_kernel_slot` 不同，其余完全继承」。

#### 4.1.5 小练习与答案

**练习 1**：为什么不把 1d/2d/3d 写成三层继承（`MaxPoolBase → MaxPool2dBase → MaxPool3d`）？

**参考答案**：因为差异是一个**可参数化的标量**（维数），不是「完全不同的流程分支」。三层继承会把 `nd+2`、`_POOL_DIM_NAMES[nd]` 这种本可用一个变量表达的东西硬编码到每一层，反而增加重复。类属性 `ndim` 用一个变量驱动所有差异，是最小表达。

**练习 2**：`_resolve_input` 报错信息里用 `_POOL_LAYOUTS[nd]` 输出 `"NCL"`/`"NCHW"`/`"NCDHW"`。这是「表驱动命名」的一个例子，请在本节再找一处类似的查表。

**参考答案**：`_POOL_DIM_NAMES[nd]` 在 `__init__` 与 `_eager_forward` 里用来生成 `l_in`/`h_in`/`w_in`/`d_in` 与 `out_*` 槽位名（pool.py:130-131、295-298）；`_MAX_POOL_PARAM_SUFFIXES[nd]` 在 `_get_kernel` 里用来生成 `kernel_w`/`stride_h`/`pad_d` 等 kernel 关键字（见 4.3 节）。

---

### 4.2 变体类属性、indexed max-pool 显式化与 mean_pooling 归属

#### 4.2.1 概念说明

同一族内除了「维数」差异，还有「变体」差异。pool 家族有两类变体：

- **max-pool 是否返回 indices**：`MaxPool2dFwdOp`（只输出 value）vs `MaxPool2dIndicesFwdOp`（输出 `(value, argmax)`）。这影响 `forward` 的返回类型与调用的 custom_op。
- **avg-pool 是否走 spatial 快路径**：`_generic_slot`（通用 kernel，支持 ceil_mode/divisor_override）vs `_spatial_slot`（仅在无 ceil_mode、`count_include_pad`、无 `divisor_override` 时启用的更快 kernel）。

表达「变体差异」有两种风格：

- **风格 A（类属性 + 共享基类）**：基类写一份 `forward`，子类用 `_returns_indices` 等类属性 + 必要时显式 override 来区分。
- **风格 B（元类 / `__init_subclass__` 工厂）**：基类在子类定义时用工厂函数**自动生成**不同的 `forward` 并塞进子类。

本轮重构（commit 涉及 pool.py 的改动）把 max-pool **从风格 B 改回了风格 A**——这正是讲义主题里说的「pool 剥离过度设计后 indexed max-pool forward 显式化」。理由是：风格 B 用 `__init_subclass__` 动态生成方法，对读者不透明（要看工厂才知道某个类的 `forward` 长什么样），且与 codegen 的「方法必须 class-local」约定（u8-l1、u11-l1）有潜在摩擦；风格 A 让每个 indexed 子类**显式**写一行 `forward`，所见即所得。

本节还要回答一个归属问题：`MeanPoolingForwardOp` 在本轮从 attention 家族**迁入** pool 家族，但它既不参与 ndim 泛化、也没有 manifest 条目——这是为什么。

#### 4.2.2 核心流程

**indexed max-pool 的「显式化」前后对照**：

```
【改前（风格 B）】
_MaxPoolFwdOpBase:
    def __init_subclass__(cls):
        if "forward" not in cls.__dict__:
            cls.forward = _make_max_pool_forward(cls._returns_indices)  # 工厂动态生成
# MaxPool1dIndicesFwdOp 只设 _returns_indices=True，自身没有 forward 定义

【改后（风格 A，当前 HEAD）】
_MaxPoolFwdOpBase:
    def forward(self, input):                  # 基类提供「单 tensor」版本
        return _pool_fwd(input, self._instance_key)
# MaxPool1d/2d/3dIndicesFwdOp 显式 override：
    def forward(self, input) -> Tuple[Tensor, Tensor]:
        return _pool_fwd_with_indices(input, self._instance_key)
```

两个 custom_op（`_pool_fwd` 与 `_pool_fwd_with_indices`）是模块级的编译边界出口，indexed 变体直接选另一个出口。返回类型注解（`Tensor` vs `Tuple[Tensor, Tensor]`）现在写在每个叶子类上，与 manifest `outputs` 声明一一对应，所见即所得。

**mean_pooling 迁移**（commit 582291a「[Refactor] Move mean pooling to pool family」）的物理动作：

```
Op 类:    tileops/ops/attention/deepseek_nsa.py  →  tileops/ops/pool.py
Kernel:   tileops/kernels/attention/mean_pooling.py  →  tileops/kernels/pool/mean_pooling.py
Workload: workloads/attention/mean_pooling.py  →  workloads/mean_pooling.py
Test:     tests/ops/attention/test_mean_pooling.py  →  tests/ops/test_mean_pooling.py
Bench:    benchmarks/ops/attention/bench_mean_pooling.py  →  benchmarks/ops/bench_mean_pooling.py
```

即整条 Op/Kernel/workload/test/bench 链路从 `attention/` 子目录平移到 pool 家族顶层。

#### 4.2.3 源码精读

**(1) max-pool 基类现在有具体的「单 tensor」forward。**

[tileops/ops/pool.py:590-591](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/pool.py#L590-L591) —— 基类 `forward` 一行，调用 `_pool_fwd`（单输出 custom_op）。

**(2) 每个 indexed 叶子显式 override forward，返回二元组。** 这是「显式化」的核心：不再由 `__init_subclass__` 工厂注入，而是写在类体里。

[tileops/ops/pool.py:652-691](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/pool.py#L652-L691) —— `MaxPool1dIndicesFwdOp`：设 `_returns_indices = True`、`_kernel_slot = "max_pool1d_with_indices_kernel"`，并**显式**写 `forward` 调用 `_pool_fwd_with_indices`。

```python
class MaxPool1dIndicesFwdOp(_MaxPoolFwdOpBase):
    ndim = 1
    _kernel_slot = "max_pool1d_with_indices_kernel"
    _returns_indices = True
    _validate_dtypes = _validate_pool_input_dtypes
    ...
    def forward(self, input: torch.Tensor) -> Tuple[torch.Tensor, torch.Tensor]:
        return _pool_fwd_with_indices(input, self._instance_key)
```

> 注意 `_returns_indices` 类属性**仍然保留**——它不是给 `__init_subclass__` 用的了（工厂已删），而是给 `_infer_output_shapes` 用来决定是否多产出一个 `indices` 输出（见 pool.py:578-588），以及给 `_max_pool_roofline` 用来决定字节记账是否加 `out_elems * 8`（argmax 是 int64）。类属性的用途从「驱动工厂」变成了「驱动形状推导与 roofline」。

**(3) 两个 custom_op 是模块级编译边界出口。**

[tileops/ops/pool.py:915-943](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/pool.py#L915-L943) —— `_pool_fwd`（单输出）与 `_pool_fwd_with_indices`（双输出）各自的 `custom_op` + `register_fake`。fake 体调 `_infer_output_shapes` 推 meta，正是 fullgraph 的命脉（u10-l1）。

**(4) `_instance_key` 来自构造期注册。**

[tileops/ops/op_base.py:197](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/op_base.py#L197) —— `dispatch_kernel` 内 `self._instance_key = register_instance(self)`，用 `str(id(op))` 作 key 写入 weak 字典。

[tileops/ops/compile_boundary.py:17-29](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/compile_boundary.py#L17-L29) —— `_OP_REGISTRY: WeakValueDictionary`，`register_instance`/`get_instance` 用字符串 key 查实例。字符串 key（而非 int）是为了躲开 dynamo 把 int 泛化为不可哈希 SymInt 的坑（详见 u10-l1）。

**(5) `MeanPoolingForwardOp` 是迁入的「异类」。** 它直接继承 `Op`（不继承任何 pool 泛化基类），在 `__init__` 里**立即构造** kernel（eager build），完全不走 ndim 机制。

[tileops/ops/pool.py:44-78](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/pool.py#L44-L78) —— `MeanPoolingForwardOp`：构造参数是 attention 专属的（`batch_size`/`seq_len`/`heads`/`dim`/`chunk_size`/`chunks_per_bacth`/`seq_num`/`use_offsets`），`forward` 直接 `self.kernel(x, offsets, indices=indices)`，没有 compile 边界、没有 ndim、没有形状推断。

```python
class MeanPoolingForwardOp(Op):
    def __init__(self, batch_size, seq_len, heads, dim, chunk_size,
                 chunks_per_bacth, seq_num, use_offsets, dtype, accum_dtype,
                 tune=False, kernel_map=None):
        ...
        self.dispatch_kernel(kernel_map)
        self.kernel = self.kernel_map["mean_pooling_fwd_kernel"](**params)  # eager 构造

    def forward(self, x, offsets, indices):
        return self.kernel(x, offsets, indices=indices)
```

**为何迁入 pool 家族却又不参与 ndim 机制？** 这是「归属」与「机制」的分离：

- **归属依据（为何进 pool）**：mean_pooling 在语义上是「对序列分块做均值归约」，是一种 pooling。它原本寄生在 attention 的 `deepseek_nsa.py` 里（Native Sparse Attention 的内部步骤），但它的本质操作是池化而非注意力，留在 attention 目录是历史包袱。迁到 pool 家族是**代码组织上的正名**——让目录名匹配算子语义。
- **机制边界（为何不进 ndim 基类）**：pool 的 ndim 泛化基类是**为 PyTorch `nn.functional.avg/max_poolNd` 这套 spec-driven 契约服务的**（固定 NCL/NCHW/NCDHW 布局、`kernel_size`/`stride`/`padding` 参数、compile 边界）。`MeanPoolingForwardOp` 是一个**自定义融合 kernel**（无 PyTorch 参考实现、无 manifest 条目——可自行 `grep -rln MeanPooling tileops/manifest/` 验证为空），它的形状/参数空间完全不同，强行套 ndim 基类只会引入无意义的适配代码。所以它以「独立 Op」的身份住在 pool.py 里，享受目录归属，但不沾染泛化机制。

> 一句话：**归属是「这个算子属于哪个家族」的组织判断；机制是「它复用哪套基类」的工程判断**。两者可以、也应当独立。

#### 4.2.4 代码实践

**实践目标**：用 git 证据还原 indexed max-pool 的「显式化」与 mean_pooling 的「迁移」，并解释归属。

**操作步骤**：

1. 在仓库根目录运行（只读 git，不改代码）：
   ```bash
   git log --oneline -3 -- tileops/ops/pool.py
   git show 582291a --stat --oneline          # mean_pooling 迁移提交
   grep -rln "MeanPooling" tileops/manifest/  # 预期：无输出
   ```
2. 打开当前 [tileops/ops/pool.py](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/pool.py)，确认 `_MaxPoolFwdOpBase` 里**没有** `__init_subclass__`（已被删），且 `MaxPool1d/2d/3dIndicesFwdOp` 各自类体内有一行 `def forward(...): return _pool_fwd_with_indices(...)`。
3. 对比 `MeanPoolingForwardOp`（pool.py:44-78）与 `MaxPool1dFwdOp`（pool.py:614）的 `__init__` 与 `forward`，列出前者「没有」而后者「有」的三样东西。

**需要观察的现象**：

- `git show 582291a --stat` 里能看到 `{attention => pool}/mean_pooling.py`、`{attention => }/test_mean_pooling.py` 等重命名行，以及 `attention/deepseek_nsa.py` 减少 39 行、`pool.py` 增加 39 行——Op 类整体平移的铁证。
- `grep -rln "MeanPooling" tileops/manifest/` 无输出——确认它无 manifest 条目。
- `MeanPoolingForwardOp` 缺少的「三样东西」：① 不继承 `_AvgPoolFwdOpBase`/`_MaxPoolFwdOpBase`（无 `ndim`）；② 构造期立即建 kernel（无 lazy `_get_kernel` + `_kernel_cache`）；③ `forward` 直接调 `self.kernel(...)`（无 compile 边界 `_pool_fwd` + `_instance_key` 分发）。

**预期结果**：你能口头复述「indexed 变体现在显式写 forward；mean_pooling 物理上进了 pool.py，但工程上仍是独立 eager Op」。

#### 4.2.5 小练习与答案

**练习 1**：`_returns_indices` 类属性在「显式化」之后还有用吗？用在哪？

**参考答案**：有用。它不再驱动 `__init_subclass__` 工厂（工厂已删），但仍在两处发挥作用：① `_infer_output_shapes`（pool.py:578-588）根据它决定是否多输出一个 `indices` 形状；② `_max_pool_roofline`（pool.py:440-441）根据它决定字节数是否加上 `out_elems * 8`（int64 argmax）。即从「驱动 forward 工厂」转为「驱动形状推导与 roofline 记账」。

**练习 2**：如果要让 `MeanPoolingForwardOp` 也获得 `torch.compile(fullgraph=True)` 支持，至少要补哪些东西？

**参考答案**：至少三样：① 一个模块级 `custom_op`（如 `_mean_pool_fwd`）+ `register_fake`，把 `self.kernel(...)` 藏进 eager 体；② 经 `register_instance` 拿到 `_instance_key`，让被追踪的 `forward` 只做 `_mean_pool_fwd(x, offsets, indices, self._instance_key)` 一行分发；③ 一个 `_infer_output_shapes` 供 fake 体推导输出 meta。这正是当前 pool 泛化基类已经具备、而 `MeanPoolingForwardOp` 暂缺的编译边界设施——这也反过来说明它目前还不是 spec-driven、fullgraph 就绪的算子。

---

### 4.3 表驱动命名与 kernel-cache key 不变量

#### 4.3.1 概念说明

ndim 泛化把「几维」参数化了，但还有一个落地问题：TileLang kernel 的构造函数接收的是**带轴后缀的关键字参数**——`MaxPool3dKernel(kernel_d=3, kernel_h=3, kernel_w=3, stride_d=2, ...)`。这些名字随 ndim 变化，如果用 `if ndim == 1: ... elif ndim == 2: ...` 硬写，每个基类都要重复三份几乎相同的构造代码。

**表驱动命名**是解法：把「轴后缀」存进按 ndim 索引的字典，构造 kernel 时遍历字典生成关键字。pool.py 顶部有四张这样的表：

| 表 | 索引 | 内容 | 用途 |
|----|------|------|------|
| `_POOL_LAYOUTS` | ndim | `{"NCL","NCHW","NCDHW"}` | 报错信息与文档 |
| `_POOL_DIM_NAMES` | ndim | `{("l",),("h","w"),("d","h","w")}` | 形状槽位 `*_in`/`out_*`、kernel 的 `*_in` 入参 |
| `_AVG_POOL_PARAM_SUFFIXES` | ndim | 同 `_POOL_DIM_NAMES` | avg kernel 的 `kernel_*`/`stride_*`/`pad_*` 后缀 |
| `_MAX_POOL_PARAM_SUFFIXES` | ndim | `{("w",),("h","w"),("d","h","w")}` | max kernel 的同名后缀（注意 1d 用 `w`，历史命名） |

与之配套的铁律是 **kernel-cache key 与构造关键字名在重构中必须保持不变**。原因有二：

1. **cache key 是元组**：它把「哪些值会触发重新 JIT 编译」显式列出来。重构（比如把一个 helper 方法内联）绝不能悄悄增删 key 字段，否则要么缓存失效（性能退化）、要么不同配置命中同一缓存（正确性 bug）。
2. **关键字名是契约**：kernel 类的构造签名（`kernel_w`/`stride_h`/`pad_d`）是 Op 层与 kernel 层之间的接缝。重构 Op 层时若改了名字，kernel 层会静默用错默认值。

> 本轮重构有一个「内联化」改动正好示范这条铁律：avg-pool 的 `_kernel_cache_key` 辅助方法（及其在 `AvgPool1d`/`AvgPool2d` 的 override）被删除，key 计算直接内联进 `_get_kernel`，但 key 的**字段集合不变**——这正是「剥离过度设计」但不破坏不变量的范例。

reduction 家族走的是另一条参数化路子——**任意秩**（arbitrary rank）而非「固定空间秩」。它的「表驱动」体现在 `dim` 规范化与形状重塑的共享原语上（`_multidim`），以及 kernel 层的 `T.macro` 工厂（`_primitives`）。

#### 4.3.2 核心流程

**pool 的表驱动 kernel 构造**（以 max-pool `_get_kernel` 为例）：

```
key = (n, c_in, *in_dims, kernel_size, stride, padding, dilation, ceil_mode, dtype, device_index, tune)
if key not in self._kernel_cache:
    kwargs = {n=, c_in=, ceil_mode=, dtype=, tune=}
    for k, name in _POOL_DIM_NAMES[ndim]:      # h_in / w_in ...
        kwargs[f"{name}_in"] = in_dims[k]
    for k, name in _MAX_POOL_PARAM_SUFFIXES[ndim]:  # kernel_h / stride_w / pad_d ...
        kwargs[f"kernel_{name}"] = kernel_size[k]
        kwargs[f"stride_{name}"] = stride[k]
        kwargs[f"pad_{name}"]   = padding[k]
        kwargs[f"dilation_{name}"] = dilation[k]
    cache[key] = kernel_map[_kernel_slot](**kwargs)   # 永远关键字，永不位置参数
return cache[key]
```

`kwargs` 是 dict、kernel 用 `**kwargs` 调用——**永不位置参数**。这保证：轴顺序、维数变化都不会错位。

**reduction 的任意秩共享原语**（`_multidim`，对照 pool 的固定秩）：

```
dim (int | list | None)
  └─ normalize_dim(dim, ndim)          # 归一化为升序非负 dim 列表（表驱动：支持 int/list/None）
       └─ flatten_for_multidim(x, dims)   # 把待归约轴 permute 到末尾、flatten 成一维 → (M, N)
            └─ [单维 kernel 在末维归约]
                 └─ restore_multidim_shape(y, orig, dims, keepdim)  # 恢复成任意秩输出
```

reduction 没有 `ndim` 类属性——它的输入秩是**任意的**，所以不能用「填类属性」表达，而要在 `forward` 里**运行时**把任意 `dim` 归一化、把任意秩 reshape 成二维 (M, N) 再复用同一个单维 kernel。两种策略的取舍见 4.3.5。

#### 4.3.3 源码精读

**(1) pool 顶部的四张命名表。**

[tileops/ops/pool.py:85-95](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/pool.py#L85-L95) —— `_POOL_LAYOUTS`/`_POOL_DIM_NAMES`/`_AVG_POOL_PARAM_SUFFIXES`/`_MAX_POOL_PARAM_SUFFIXES`，注释点出 1d max-pool 历史上把池化轴叫 `w`（而非 `l`），故 avg 与 max 用不同后缀表。

```python
_POOL_LAYOUTS: Dict[int, str] = {1: "NCL", 2: "NCHW", 3: "NCDHW"}
_POOL_DIM_NAMES: Dict[int, Tuple[str, ...]] = {1: ("l",), 2: ("h", "w"), 3: ("d", "h", "w")}
_AVG_POOL_PARAM_SUFFIXES: Dict[int, Tuple[str, ...]] = _POOL_DIM_NAMES
_MAX_POOL_PARAM_SUFFIXES: Dict[int, Tuple[str, ...]] = {1: ("w",), 2: ("h", "w"), 3: ("d", "h", "w")}
```

**(2) 表驱动构造 kernel + cache key（max-pool，内联版）。**

[tileops/ops/pool.py:528-561](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/pool.py#L528-L561) —— `_get_kernel`：先组 key 元组（缓存判定），miss 时按两张表遍历生成 `kwargs`，最后 `self.kernel_map[self._kernel_slot](**kwargs)`。

```python
key = (n, c_in, *in_dims, self.kernel_size, self.stride, self.padding,
       self.dilation, self.ceil_mode, dtype, device_index, self.tune)
if key not in self._kernel_cache:
    kernel_kwargs: Dict[str, object] = dict(n=n, c_in=c_in, ceil_mode=self.ceil_mode,
                                            dtype=dtype, tune=self.tune)
    for k, name in enumerate(_POOL_DIM_NAMES[self.ndim]):
        kernel_kwargs[f"{name}_in"] = in_dims[k]
    for k, name in enumerate(_MAX_POOL_PARAM_SUFFIXES[self.ndim]):
        kernel_kwargs[f"kernel_{name}"] = self.kernel_size[k]
        kernel_kwargs[f"stride_{name}"] = self.stride[k]
        kernel_kwargs[f"pad_{name}"] = self.padding[k]
        kernel_kwargs[f"dilation_{name}"] = self.dilation[k]
    self._kernel_cache[key] = self.kernel_map[self._kernel_slot](**kernel_kwargs)
```

**(3) 「内联化但保 key 不变」的范例（avg-pool）。**

[tileops/ops/pool.py:214-257](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/pool.py#L214-L257) —— `_AvgPoolFwdOpBase._get_kernel`：key 直接在方法体内组（旧版的 `_kernel_cache_key` 辅助方法已删），字段集合与改前一致；miss 时按 `_AVG_POOL_PARAM_SUFFIXES` 生成 `kernel_*`/`stride_*`/`pad_*`。

> 对照 diff：旧代码有 `def _kernel_cache_key(self, kernel_name, use_spatial_fast_path, n, c_in, in_dims, dtype, device_index)`，且 `AvgPool1d`/`AvgPool2d` 各自 override 它（2d 还把 `variant="spatial"/"general"` 编进 key）。现在这些 override 全删，key 内联、字段不变。这就是「剥离过度设计」——删掉了一个只为「组元组」存在、却被多态 override 的辅助方法，同时**保持 key 字段集合**，确保缓存语义零变化。

**(4) reduction 的「任意秩」共享原语 `_multidim`。**

[tileops/ops/reduction/_multidim.py:27-86](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/reduction/_multidim.py#L27-L86) —— `normalize_dim`：把 `int`/`list`/`None` 统一归一化为升序非负 dim 列表，支持负索引、查重、空列表策略（`reject`/`full`/`noop`）。这是 reduction 的「表驱动」——`dim` 的多种写法由一个函数统一处理，调用方永不写 `if isinstance(dim, int)` 分支。

[tileops/ops/reduction/_multidim.py:89-159](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/reduction/_multidim.py#L89-L159) —— `flatten_for_multidim`（permute + flatten 成 (M, N)）与 `restore_multidim_shape`（按 keepdim 恢复任意秩）。这套原语让**同一个单维 kernel** 能服务任意秩、任意 `dim` 组合的归约——这是 reduction 选择的参数化轴（「归约轴」），与 pool 选择的轴（「空间维数」）正交。

**(5) reduction kernel 层的 `T.macro` 工厂 `_primitives`。**

[tileops/kernels/reduction/_primitives.py:201-232](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/kernels/reduction/_primitives.py#L201-L232) —— `make_reduce_epilogue(op_kind)`：按 `op_kind`（`sum`/`max`/`min`）返回不同的 `T.macro`，是 kernel 层的「表驱动工厂」。

[tileops/kernels/reduction/_primitives.py:235-298](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/kernels/reduction/_primitives.py#L235-L298) —— `make_welford_update(block_m, N_padded)`：单遍 Welford 均值+方差更新的 `T.macro`，被 std/var/var_mean kernel 共享，避免每个 kernel 重写归约逻辑。

[tileops/kernels/reduction/_primitives.py:107-192](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/kernels/reduction/_primitives.py#L107-L192) —— `compute_tile_n`：按共享内存预算与对齐约束算列 tile 尺寸，被所有分块 reduction kernel（softmax/logsumexp/var 等）共用。它是 reduction 家族「共享原语」的典型——一处计算逻辑，多处复用。

**(6) reduction Op 层的类属性 + hooks（对照 pool 的 ndim）。**

[tileops/ops/reduction/reduce.py:58-114](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/reduction/reduce.py#L58-L114) —— `_ReduceOpBase` 用 `_op_kind`/`_kernel_key`/`_kernel_cls`/`_kernel_handles_padding`/`_empty_dim_policy` 一组类属性 + `_validate_dim`/`_pad_value`/`_build_kernel_kwargs`/`_pre_kernel`/`_post_kernel` 一组 hooks 来参数化。这与 pool 的 `ndim`/`_kernel_slot` 是同一种思想（类属性驱动共享流水线），只是 reduction 的「变体」是「归约算子种类 + dim 策略」而非「空间维数」。

#### 4.3.4 代码实践

**实践目标**：验证「表驱动命名」与「cache key 不变量」，并对照两种参数化策略。

**操作步骤（源码阅读型）**：

1. 在 [tileops/ops/pool.py](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/pool.py) 的 `_MaxPoolFwdOpBase._get_kernel`（行 528-561）里，把 `self.ndim=3` 代入，手写出 `_MAX_POOL_PARAM_SUFFIXES[3]` 展开后 `kernel_kwargs` 的完整键集合（预期含 `d_in/h_in/w_in`、`kernel_d/h/w`、`stride_d/h/w`、`pad_d/h/w`、`dilation_d/h/w`）。
2. 运行 `git diff 9bda1ac5..2392b7e -- tileops/ops/pool.py`，定位 `_kernel_cache_key` 被删除的 hunk，对比「旧 key 字段」与「新内联 key 字段」（pool.py:224-238），确认字段集合一致（avg-pool 仍含 `kernel_size/stride/padding/ceil_mode/count_include_pad/divisor_override/dtype/device_index/tune`）。
3. 打开 [tileops/ops/reduction/_multidim.py](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/reduction/_multidim.py)，用 `normalize_dim([0,2], ndim=4)`、`normalize_dim(-1, ndim=3)`、`normalize_dim(None, ndim=3)` 三个例子手算预期返回值。

**需要观察的现象**：

- 步骤 1：键集合里**没有任何位置参数**，且轴顺序严格由表 `_MAX_POOL_PARAM_SUFFIXES[3] = ("d","h","w")` 决定——若有人误把表写成 `("w","h","d")`，`kernel_d` 就会拿到 `kernel_size[2]`，这正是「关键字名是契约」的脆弱点。
- 步骤 2：旧 `_kernel_cache_key` 与新内联 key 的字段**完全一致**（avg-pool 三处 override 删掉后，1d 不再有「不带 divisor_override」的特殊 key——但 `AvgPool1d` 的 `divisor_override` 恒为 `None`，故 key 等价）。
- 步骤 3：`normalize_dim([0,2],4) → [0,2]`；`normalize_dim(-1,3) → [2]`；`normalize_dim(None,3) → [0,1,2]`。

**预期结果**：你能说清「pool 用固定 ndim + 命名表，reduction 用运行时 dim 归一化 + flatten」是针对「固定空间秩」与「任意秩」两种问题的两套解，且两者都恪守「关键字驱动、cache key 字段稳定」。

**关于运行验证**：步骤 3 的 `normalize_dim` 是纯 Python 函数，可在装好 tileops 的环境里 `from tileops.ops.reduction._multidim import normalize_dim` 直接调用验证（无需 GPU）。步骤 1、2 为源码阅读，标注为「待本地对照 diff 确认」即可。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `_get_kernel` 用 `**kwargs` 调 kernel，而不是位置参数 `kernel(n, c_in, ..., kernel_size[0], ...)`？

**参考答案**：两个理由。① **轴顺序与维数可变**：1d 只有 `w`，3d 有 `d/h/w`，位置参数会因维数不同而错位，关键字参数对轴命名、对维数免疫。② **关键字名是 Op↔kernel 的契约**：kernel 构造签名可能演化（增删可选参数），关键字调用让缺失参数走 kernel 默认值，不会因位置错配而静默用错。这与 code-style.md「永不位置参数」的精神一致。

**练习 2**：pool 与 reduction 都用「类属性 + 共享基类」，但参数化的轴不同。请各举一个「类属性」。

**参考答案**：pool 参数化「空间维数」→ 类属性 `ndim`（1/2/3）；reduction 参数化「归约算子种类 + dim 策略」→ 类属性 `_op_kind`（`sum`/`mean`/`amax`…）与 `_empty_dim_policy`（`reject`/`full`/`noop`）。两者都是「把家族差异塞进类属性，让一份流水线代码驱动」的同一种思想，差异只在「差异的语义」。

**练习 3**：若把 `_MAX_POOL_PARAM_SUFFIXES[1]` 从 `("w",)` 改成 `("l",)`，会发生什么？

**参考答案**：Op 层会向 1d max-pool kernel 传 `kernel_l`/`stride_l`/`pad_l`/`dilation_l`，但 kernel 构造签名里这些参数叫 `kernel_w`/…（历史命名，见 pool.py:89-90 注释）。结果关键字不匹配，要么 kernel 用默认值（静默错误结果）、要么直接 `TypeError`。这示范了「关键字名是契约」——命名表与 kernel 签名必须一致，改名是跨层改动。

---

## 5. 综合实践

**任务**：给「维度参数化家族」画一张完整的复用地图，并用 git 证据支撑你的每个判断。

请完成以下子任务，结果整理成一份一页笔记：

1. **ndim 泛化**：在 [tileops/ops/pool.py](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/pool.py) 里画出 `_MaxPoolFwdOpBase` 与它的 6 个叶子（1d/2d/3d × 普通/indexed）的继承树，标注每个叶子**自身定义**的成员。确认「不带 indices 的三个叶子不重写 forward」「带 indices 的三个叶子各显式重写 forward 返回二元组」。

2. **显式化证据**：运行 `git diff 9bda1ac5..2392b7e -- tileops/ops/pool.py`，找出被删除的 `_make_max_pool_forward` 工厂与 `__init_subclass__` hunk，写出「改前如何自动生成 forward、改后如何显式声明 forward」的对照。

3. **归属判断**：运行 `git show 582291a --stat` 与 `grep -rln MeanPooling tileops/manifest/`，用证据回答三个问题：
   - `MeanPoolingForwardOp` 的 Op/Kernel/test/bench 从哪迁到哪？
   - 它有 manifest 条目吗？
   - 它继承 `_MaxPoolFwdOpBase`/`_AvgPoolFwdOpBase` 吗？为什么（用「归属 vs 机制」解释）？

4. **cache key 不变量**：对比 avg-pool 删除 `_kernel_cache_key` 前后的 key 字段集合，确认「字段不变、仅内联」。指出若有人误把 `divisor_override` 从 key 里删掉，会导致什么正确性或性能后果（提示：两个 `divisor_override` 不同、其余全相同的 2d avg-pool 会命中同一缓存）。

5. **策略对照**：写一段话对比 pool（固定 ndim + 命名表）与 reduction（任意秩 + `_multidim` flatten）两套参数化，说明它们各自适合什么样的家族。

**预期产物**：一张继承树图 + 一份 diff 证据清单 + 一段归属论证 + 一段策略对比。全部基于真实源码与 git，不运行 GPU 也能完成。

---

## 6. 本讲小结

- **ndim 泛化基类**：pool 的 1d/2d/3d 用一个 `ndim: ClassVar[int]` 类属性参数化，一份 `forward`/`_eager_forward`/`_get_kernel`/`_resolve_input` 流水线用 `nd = self.ndim` 驱动所有差异（输入秩 = nd+2、参数 nd 元组、轴后缀查表）。差异是「可参数化标量」时，类属性比多层继承更省。
- **变体类属性**：`_kernel_slot`/`_returns_indices`/`_generic_slot`/`_spatial_slot` 在共享基类内区分变体。本轮把 indexed max-pool 的 `forward` 从 `__init_subclass__` 工厂自动生成（风格 B）改回每个 indexed 叶子**显式声明**（风格 A），即「剥离过度设计后 indexed max-pool forward 显式化」；`_returns_indices` 转为驱动 `_infer_output_shapes` 与 roofline 字节记账。
- **mean_pooling 归属**：`MeanPoolingForwardOp` 整条链从 attention（`deepseek_nsa.py`）迁入 pool 家族（commit 582291a），享受目录归属；但它是无 manifest 条目的自定义 eager-build Op，不继承 ndim 基类——「归属（组织）」与「机制（复用）」相互独立。
- **表驱动命名**：四张按 ndim 索引的字典（`_POOL_LAYOUTS`/`_POOL_DIM_NAMES`/`_AVG_POOL_PARAM_SUFFIXES`/`_MAX_POOL_PARAM_SUFFIXES`）驱动 kernel 构造关键字，永远 `**kwargs`、永不位置参数；关键字名是 Op↔kernel 契约。
- **cache key 不变量**：avg-pool 的 `_kernel_cache_key` 辅助方法被内联删除，但 key 字段集合不变——重构可以删过度设计，但绝不能悄悄改 cache key 字段（否则缓存失效或错误命中）。
- **reduction 对照**：reduction 参数化的是「任意归约轴」而非「固定空间秩」，故用 `_multidim`（`normalize_dim`/`flatten_for_multidim`/`restore_multidim_shape`）运行时把任意秩压成 (M,N) 复用单维 kernel，kernel 层用 `_primitives`（`T.macro` 工厂、`compute_tile_n`）共享原语；两套策略都恪守「类属性驱动 + 关键字驱动 + cache key 稳定」。

---

## 7. 下一步学习建议

- **向深（复杂多 kernel 协作）**：本讲的 pool 是「一个 Op 一个（或两个）kernel」；想看「一个 Op 编排多个 kernel」的进阶形态，进入 u12-l1（Attention 家族）与 u12-l2（MoE 家族），以及 u3-l4（cumsum 三阶段并行扫描）。
- **向广（更多家族基类模式）**：对照 u11-l1（T1/T2 与三层继承）与 u11-l2（elementwise 三大伞形基类），把「伞形基类（按算子语义分）」「ndim 泛化（按维数分）」「类属性 + hooks（reduction，按归约种类分）」三种复用轴并排比较，建立「何时抽哪种基类」的决策树。
- **向机制（codegen 契约）**：本讲反复强调 `eval_roofline`/`_validate_dtypes` 必须 class-local。若想彻底理解「为何不能靠继承传递」，回看 u8-l1（`__init_subclass__` 钩子）与 u8-l3（dtype 校验 codegen），那里解释了 `maybe_install_validator` 只看 `cls.__dict__` 的不对称。
- **动手（建议的源码阅读顺序）**：先通读 [tileops/ops/pool.py](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/pool.py) 的两个泛化基类与 9 个叶子；再读 [tileops/ops/reduction/reduce.py](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/reduction/reduce.py) 的 `_ReduceOpBase` hooks 体系；最后用 `git log --oneline -- tileops/ops/pool.py` 复盘本轮「剥离过度设计」的演化轨迹。
