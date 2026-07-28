# elementwise 三大伞形基类

## 1. 本讲目标

本讲聚焦 TileOPs 里「模板化复用」走到极致的一个家族——elementwise（逐元素）算子。读完本讲你应该能够：

1. 说清 `UnaryOp` / `BinaryOp` / `FusedGatedOp` 这三个 **伞形基类（umbrella base）** 各自封装了哪种「输入元数（arity）+ 张量布局」的 `forward` 流程，以及它们之间的差异。
2. 理解 **`kernel_cls` + `_op_name` 模板化**：为什么一个叶子算子类只填两三个类属性，就能白嫖整条构造、校验、`forward`、`torch.compile` 注册链路。
3. 掌握 `coalesce_broadcast_dims` 如何把 N 维广播**降维合并**，从而减少 kernel 内部的 `divmod` 次数。
4. 理解 **inplace 分发**：`_UnaryActivationMixin` + `_wrapped_inplace` 如何用 `mutates_args=("x",)` 让 `torch.compile` 正确追踪原地写回，并保证调用方看到 `y is x`。
5. 理解 **FP8 后置 cast**（`_apply_fp8_post_cast`）与三个基类各自如何给出统一的 roofline 公式。

> 本讲承接 [u11-l1 家族基类模式](u11-l1-family-base-pattern.md)（L1/L2/L3 三层、T1/T2 两种形态、codegen 契约必须 class-local）与 [u10-l2 custom_op 注册工厂](u10-l2-custom-op-registration-factory.md)（`_wrapped`、`instance_key`、`_OP_REGISTRY`、`register_fake`）。这两讲建立的结论本讲直接使用，不再重复证明。

> 关于 HEAD 变化：相较上一 HEAD，本讲涉及的 `tileops/ops/elementwise/_base.py` 与 `tileops/kernels/elementwise.py` 只做了文档字符串与常量的收敛（删除了 `_FP8_NONSAT_OUTPUT_DTYPES` / `_effective_scalar_kernel_dtype` 两个不再使用的辅助、收紧多处 docstring），**三个伞形基类的结构、签名、forward 流程均未改变**。因此本讲按当前 HEAD 从零撰写，所有行号与永久链接对齐 `2392b7e`。

## 2. 前置知识

- **伞形基类（umbrella base）**：一个位于继承树中层的基类，把一族算子**完全相同**的 `forward` 流程收拢进来；子类只填「差异点」作为类属性。它是 u11-l1 里 L2（FamilyBase）的典型形态。
- **元数（arity）**：算子输入张量的个数。elementwise 只有三种元数形态：一元（1→1）、二元（2→1，可广播）、融合门控（输入是 `(M, 2N)` 的拼接张量，输出 `(M, N)`）。
- **广播（broadcast）**：两个形状不同的张量按 NumPy/PyTorch 规则对齐到公共输出形状（如 `(3,1,5)` 与 `(4,5)` → `(3,4,5)`），其中大小为 1 的维度被「复制」。
- **divmod（整除取余）**：把一个一维线性下标还原成多维坐标的标准手段——逐维做「除 + 取余」。维数越多，kernel 里要做的 `divmod` 越多，这是广播 kernel 的主要地址计算开销。
- **FP8 的两种格式**：`e4m3fn`（无 Inf/NaN 表示，饱和转换正确）与 `e5m2`（有 Inf/NaN 表示，必须由 Op 层做最终非饱和 cast）。详见 u3-l3。
- **`torch.library.custom_op` 与 `mutates_args`**：PyTorch 注册自定义算子的机制；`mutates_args=("x",)` 显式声明「会原地改写参数 x」，`torch.compile` 据此正确追踪副作用。详见 u10-l2。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [tileops/ops/elementwise/_base.py](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/elementwise/_base.py) | **本讲主战场**。三个伞形基类 `UnaryOp`/`BinaryOp`/`FusedGatedOp`、广播工具 `coalesce_broadcast_dims`、FP8 cast 工具 `_apply_fp8_post_cast`、若干二级共享基类（`_UnaryActivationMixin`/`_ParamFreeActivationOp`/`_ParametricActivationOp`/`_AlphaScaledBinaryOp`/`_BoolOutputBinaryOp`/`_IntIdentityUnaryOp`）以及 `torch.compile` 注册工厂函数。 |
| [tileops/kernels/elementwise.py](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/kernels/elementwise.py) | Kernel 侧的三个模板基类 `UnaryKernel`/`BinaryKernel`/`FusedGatedKernel`，以及对应的策略工厂（direct / explicit_parallel / register_copy）。广播地址计算 `_compute_broadcast_offsets` 与同形快路径 `_is_contiguous_same_shape` 也在此。 |
| [tileops/ops/elementwise/arithmetic.py](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/elementwise/arithmetic.py) | 二元算子叶子：`AddFwdOp`/`MulFwdOp`/`DivFwdOp`/`PowFwdOp` 等。展示「只填类属性」的 T1 薄包装，以及 `_other_name`、`rounding_mode` 选 kernel 等差异化手法。 |
| [tileops/ops/elementwise/activations.py](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/elementwise/activations.py) | 激活叶子：`ReluFwdOp`/`GeluFwdOp`/`LeakyReluFwdOp`/`SiluAndMulFwdOp` 等，覆盖三类基类与 inplace/parametric 变体。 |
| [tileops/ops/elementwise/__init__.py](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/elementwise/__init__.py) | 包加载时跑的「注册循环」：把每个叶子类喂给对应的 `_register_*_custom_op` 工厂，挂上 `_wrapped`（与可选的 `_wrapped_inplace`）。 |
| [docs/design/ops-design.md](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/docs/design/ops-design.md) | Family-Base Refactoring 一节：scaffold 只产 T2，2–3 个 op 共享同一 `forward` 流程后才抽 L2；家族基类 **不得归一化真正的 per-op 差异**。本讲的伞形基类就是这条规则的正面教材。 |

## 4. 核心概念与源码讲解

### 4.1 三大伞形基类与 kernel_cls 模板

#### 4.1.1 概念说明

elementwise 家族有 60 多个算子，但它们的 `forward` 流程按「输入元数 + 张量布局」归类后**只有三种**：

| 伞形基类 | 元数 | 构造期形状参数 | 输入→输出布局 | 典型成员 |
| --- | --- | --- | --- | --- |
| `UnaryOp` | 1→1 | `N_total`（展平元素数） | 任意形状 → 展平 1D 调 kernel → reshape 回原形 | relu / sigmoid / abs / gelu … |
| `BinaryOp` | 2→1 | `a_shape`, `b_shape` | 两个输入按 N 维广播到 `out_shape` | add / mul / div / pow / maximum … |
| `FusedGatedOp` | 1→1（特殊） | `M`, `N`（可 lazy） | 输入 `(M, 2N)` 拆成 gate\|value，输出 `(M, N)`：`y = act(gate) * value` | silu_and_mul / gelu_and_mul … |

这三个基类就是 u11-l1 里的 **L2（FamilyBase）**。它们各自把「这一元数下完全相同的 `forward` 流程」写死在基类里，叶子算子（L3）退化为 **T1 薄包装**：只填两个类属性即可。

```python
# 叶子只需要这么多 —— forward / _eager_forward / 构造 / 注册全部继承自伞形基类
class MulFwdOp(BinaryOp):
    _op_name = "mul"
    kernel_cls = MulFwdKernel
```

模板化的两个支点是：

- **`kernel_cls`**：这个算子对应的 Kernel 类（L1 实现）。基类据此填 `default_kernel_map`、据此构造 kernel 实例。
- **`_op_name`**：算子在 manifest `source.kernel_map` 里的 dispatch key（字符串）。基类用它做 `kernel_map[_op_name]` 查表，也用它拼 `torch.library.custom_op` 的命名空间名 `top::elementwise_binary_{_op_name}`。

u10-l2 已说明：每个叶子在**包加载时**由 `__init__.py` 底部的注册循环调工厂函数，把一个 `torch.library.custom_op` 挂到类属性 `_wrapped`；运行时 `forward` 经 `type(self)._wrapped(..., instance_key)` → `_OP_REGISTRY[instance_key]` 查表 → `_eager_forward` → kernel。本讲的关注点是**这套链路如何被三个伞形基类统一承载**，使叶子零样板。

#### 4.1.2 核心流程

一条从「定义叶子类」到「调用算子」的完整时序：

1. **类定义**：`class MulFwdOp(BinaryOp): _op_name="mul"; kernel_cls=MulFwdKernel`。此刻 `BinaryOp.__init_subclass__` 触发（仅当叶子改了 `_other_name` 才重绑定 `forward` 签名）。
2. **包导入**：`tileops.ops.elementwise.__init__` 执行底部注册循环，调 `_register_binary_custom_op(MulFwdOp)`，覆写 `MulFwdOp._wrapped` 为真实 custom_op。
3. **实例化**：`op = MulFwdOp(a_shape, b_shape, dtype)` → `BinaryOp.__init__`：
   - 校验 dtype ∈ `kernel_cls.SUPPORTED_DTYPES`；
   - `coalesce_broadcast_dims(a_shape, b_shape)` 算出 `(out_shape, coalesced_shape, a_strides, b_strides)`；
   - `dispatch_kernel(kernel_map)` 安装 `kernel_map`（基类合并 `default_kernel_map` 与用户覆盖、做架构校验）；
   - `_build_kernel_instance(...)` 用 `kernel_map[_op_name](...)` 建 kernel；
   - `self._instance_key = id(self)`，把自己登记进 `_OP_REGISTRY`。
4. **调用**：`op(a, b)` → `Op.__call__` → `BinaryOp.forward`：
   - 校验 CUDA / dtype / numel；
   - `type(self)._wrapped(a, b, self._out_shape_list, self._instance_key)`；
   - custom_op 的 eager 体：`_OP_REGISTRY[key]._eager_forward(a, b)` → kernel。

三种基类在「构造参数、`forward` 签名、是否支持 lazy、是否手写 `eval_roofline`」上不同，核心差异如下表：

| 维度 | `UnaryOp` | `BinaryOp` | `FusedGatedOp` |
| --- | --- | --- | --- |
| `__init__` 形状参数 | `N_total` | `a_shape, b_shape` | `M, N`（均可 `None`） |
| lazy 构建 | 否（构造即建 kernel） | 否 | **是**（M/N/dtype 可在首次 forward 推断） |
| `forward` 签名 | `forward(input)` | `forward(input, other)` | `forward(x)` |
| `eval_roofline` | **手写**（`FLOPS_PER_ELEM * N`） | 不手写（依赖 manifest codegen，见 u8） | **手写**（`FLOPS_PER_ELEM * M * N`） |
| `default_kernel_map` | `{_op_name: kernel_cls}` | `{_op_name: kernel_cls}` | `{_op_name: kernel_cls}` |

#### 4.1.3 源码精读

先看模块顶部的全景说明与全局注册表：

> 模块 docstring 一句话点题：三个伞形基类 + 注册工厂 + 广播工具。

[_base.py:1-16](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/elementwise/_base.py#L1-L16) —— 三个伞形基类与注册工厂的全景说明。

[_base.py:34](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/elementwise/_base.py#L34) —— `_OP_REGISTRY`：以 `id(instance)` 为 int key 的 `WeakValueDictionary`，是 `forward` → custom_op → `_eager_forward` 的运行时路由表（u10-l2 已分析其与 `compile_boundary` 字符串 key 表并存、属待迁移路径）。

**`UnaryOp` 的骨架**（注意 `_build_kernel_instance` 这个可 override 的构造钩子，与「构造即建 kernel、不 lazy」）：

[_base.py:566-605](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/elementwise/_base.py#L566-L605) —— `UnaryOp` 类与 `__init__`：构造期 `dispatch_kernel` + `_build_kernel_instance` + 登记 `_OP_REGISTRY`。关键几行：

```python
self.dispatch_kernel(kernel_map)
self.kernel = self._build_kernel_instance(N_total=N_total, dtype=dtype, tune=tune)
self.output_dtype = self._resolve_output_dtype()
self._instance_key = id(self)
_OP_REGISTRY[self._instance_key] = self
```

[_base.py:607-627](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/elementwise/_base.py#L607-L627) —— `_build_kernel_instance`（默认 `kernel_map[_op_name](N_total, dtype, tune=tune)`，子类可注入额外 kwargs）与 `default_kernel_map`（`{_op_name: kernel_cls}`）。这两段是「`kernel_cls` + `_op_name` 模板化」的物证。

[_base.py:671-676](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/elementwise/_base.py#L671-L676) —— `UnaryOp.forward`：先 `_validate_input`，再 `type(self)._wrapped(input, self._instance_key)`，注册未发生则回退 `_eager_forward`。注意取的是 **类属性** `type(self)._wrapped`（而非实例属性），所以同一类的所有实例共享一个 custom_op。

**`BinaryOp` 的骨架**（多了广播 coalesce 与 dtype 校验）：

[_base.py:729-761](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/elementwise/_base.py#L729-L761) —— `BinaryOp.__init__`：先按 `kernel_cls.SUPPORTED_DTYPES` 校验 dtype，再 `coalesce_broadcast_dims(a_shape, b_shape)`，再 `dispatch_kernel` + `_build_kernel_instance`。注意它缓存了 `self._out_shape_list = list(out_shape)`，供 custom_op 热路径直接用。

[_base.py:763-774](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/elementwise/_base.py#L763-L774) —— `BinaryOp._build_kernel_instance` 与 `default_kernel_map`：构造 kernel 时传入 `(N_total, dtype, coalesced_shape, a_strides, b_strides, a_numel, b_numel, tune=...)`，比 unary 多了广播所需的形状与步长。

**`FusedGatedOp` 的骨架**（唯一支持 lazy 的基类）：

[_base.py:846-871](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/elementwise/_base.py#L846-L871) —— `FusedGatedOp.__init__`：`M`/`N`/`dtype` 都可 `None`；只有三者齐全才在构造期 `_ensure_kernel`，否则延后到首次 `forward`。这让它可以 `op = SiluAndMulFwdOp()` 不给形状先占位。

[_base.py:910-919](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/elementwise/_base.py#L910-L919) —— `_ensure_kernel`：带 `(M, N, dtype)` 缓存键的懒构建，命中即跳过。

[_base.py:946-952](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/elementwise/_base.py#L946-L952) —— `FusedGatedOp.forward`：先从 `x` 推 `(M, N)`、`_ensure_kernel`，再走 `_wrapped(x, self.M, self.N, self._instance_key)`。

**叶子算子如何只靠类属性复用一切**：

[arithmetic.py:57-61](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/elementwise/arithmetic.py#L57-L61) —— `MulFwdOp`：两行类属性，`forward`/`_eager_forward`/构造/注册全继承。

[arithmetic.py:119-129](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/elementwise/arithmetic.py#L119-L129) —— `PowFwdOp`：在 `MulFwdOp` 基础上多一个 `_other_name = "exponent"`，把第二参数重命名为 manifest 声明的名字（`__init_subclass__` 据此重绑 `forward.__signature__`）。

[activations.py:38-45](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/elementwise/activations.py#L38-L45) —— `ReluFwdOp`：继承 `_ParamFreeActivationOp`（详见 4.3），除两个类属性外只声明 `FLOPS_PER_ELEM = 1`，并注释其与 manifest `roofline.flops = "N"` 的一致性。

[activations.py:328-332](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/elementwise/activations.py#L328-L332) —— `SiluAndMulFwdOp`：`FusedGatedOp` 的叶子，同样只填类属性。

**注册循环**（把「定义」与「custom_op 注册」解耦的关键）：

[__init__.py:199-244](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/elementwise/__init__.py#L199-L244) —— 把 unary / binary / fused-gated / independent 四组叶子分别喂给 `_register_unary_custom_op` / `_register_binary_custom_op` / `_register_fused_gated_custom_op`。类体内的 `_wrapped = None` 只是**安全默认值**：若注册未发生（如某些测试专用子类），`forward` 会回退到 `_eager_forward` 而不是崩。

#### 4.1.4 代码实践

**实践目标**：亲眼确认「叶子仅靠类属性复用 `forward`」，并解释为何 `default_kernel_map` 用 `{_op_name: kernel_cls}` 而非直接 `{_op_name: kernel_cls(...)}`。

**操作步骤（源码阅读型）**：

1. 打开 `tileops/ops/elementwise/arithmetic.py`，找到 `MulFwdOp`、`AddFwdOp`、`MaximumFwdOp`、`FloorDivideFwdOp` 四个类。
2. 确认它们都没有定义 `forward`、`_eager_forward`、`__init__`（`DivFwdOp` 与 `LerpFwdOp` 例外，它们有额外构造参数）。
3. 在 `_base.py` 里查到 `BinaryOp.forward`（795 行起）与 `BinaryOp._eager_forward`（784 行起），确认这四个叶子共享的就是这两个方法。
4. 思考：`default_kernel_map` 返回的是**类**而不是**实例**，因为 kernel 实例化需要运行时形状/dtype（`N_total`、`coalesced_shape` 等），必须等到 `__init__` 才能建。

**需要观察的现象**：

- 除 `DivFwdOp`（按 `rounding_mode` 选 kernel，把 `self.kernel_cls` 重绑为实例属性）、`LerpFwdOp`（额外 `weight` 参数，自建 kernel）外，二元算子叶子几乎都是「两行类属性」。
- `_op_name` 同时是 manifest 的 dispatch key、`kernel_map` 的查表键、custom_op 命名空间的后缀——一字符串三用。

**预期结果**：你能用一句话向同伴解释「加一个新的二元 elementwise 算子，只要写一个 Kernel 子类 + 一个两行的 Op 子类 + 在 `__init__.py` 注册循环里加一个名字」。运行验证「待本地验证」（需 CUDA GPU）。

#### 4.1.5 小练习与答案

**练习 1**：`DivFwdOp` 为什么不满足「只填类属性」？它是怎么在保持 `BinaryOp.forward` 不变的前提下切换 kernel 的？

> **答案**：`torch.div` 有 `rounding_mode`（None/`"trunc"`/`"floor"`）三种语义，对应三个不同 kernel。`DivFwdOp` 在 `__init__` 里按 `rounding_mode` 把 `self.kernel_cls` 重绑为实例属性（`self.kernel_cls = _DIV_KERNEL_BY_ROUNDING_MODE[rounding_mode]`），从而让 `BinaryOp.default_kernel_map`（读 `self.kernel_cls`）与 `BinaryOp.__init__` 里的 `SUPPORTED_DTYPES` 校验（读 `self.kernel_cls.SUPPORTED_DTYPES`）都自动指向正确 kernel。`forward` 本身完全不动。

**练习 2**：`FusedGatedOp` 为什么允许 `M`/`N`/`dtype` 为 `None`，而 `UnaryOp`/`BinaryOp` 不允许？

> **答案**：`FusedGatedOp` 的输入是 `(M, 2N)` 的拼接张量，`M`、`N`、`dtype` 都能从首次 `forward` 收到的 `x` 里直接读出（`x.shape[0]`、`x.shape[1] // 2`、`x.dtype`），所以可以 lazy。而 `UnaryOp` 的 kernel 需要预先知道 `N_total`（元素总数）才能 JIT，`BinaryOp` 需要预先知道 `a_shape`/`b_shape` 才能做广播 coalesce——这些在构造期就必须确定。

**练习 3**：为什么 `forward` 里取 `type(self)._wrapped` 而不是 `self._wrapped`？

> **答案**：`_wrapped` 是类属性（由注册工厂挂在类上）。用 `type(self)._wrapped` 明确表达「读类属性」，避免实例属性遮蔽；也方便测试用一个未注册的子类（`_wrapped` 仍为 `None`）走 `_eager_forward` 回退路径。

---

### 4.2 广播 coalesce：`coalesce_broadcast_dims` 如何降维

#### 4.2.1 概念说明

`BinaryOp` 必须支持 N 维广播。最朴素的做法是：kernel 里每个线程拿到一维线性下标 `flat_idx` 后，对输出形状的**每一维**做一次 `divmod` 还原成多维坐标，再分别乘 `a_strides[d]`、`b_strides[d]` 累加出 `a_off`、`b_off`。维数 `ndim` 越大，`divmod` 越多——这是广播 kernel 的主要地址计算开销（elementwise 本身是带宽受限的，地址计算会挤占发射槽）。

`coalesce_broadcast_dims` 的核心思想：**把「广播行为相同」的相邻维合并**，把 N 维问题降到最少的「有效维」。

- 「广播行为相同」= 两个输入要么都真实存这维（stride ≠ 0）、要么都广播这维（stride = 0），且相邻维的 stride 满足连续性（`prev_stride == cur_stride * out_dim`）。
- 合并后，kernel 只需对「合并后的有效维」做 `divmod`，次数从 `ndim - 1` 降到 `len(coalesced) - 1`。
- 同时它返回两个输入在合并后坐标系下的 stride（广播维 stride = 0），让 kernel 用一套统一的 stride-based 访问。

函数签名返回四元组 `(out_shape, coalesced_shape, a_strides, b_strides)`，其中 `coalesced_shape` 才是真正喂给 kernel 的「逻辑形状」。

#### 4.2.2 核心流程

`coalesce_broadcast_dims(a_shape, b_shape)` 的步骤：

1. **标量归一**：0-dim 输入视作 `(1,)`。
2. **算输出形状**：`out_shape = torch.broadcast_shapes(a_shape, b_shape)`。
3. **左 pad 对齐**：把两个输入形状左 pad 到 `ndim = len(out_shape)`。
4. **算原始 stride**：按「行优先」算每个输入 padded 形状的 stride；再把「真广播维」（padded size==1 但 out size>1）的 stride 置 0。
5. **贪心合并**：从左到右扫，若当前维与前一组的 a、b stride 都「连续或同广播」，就合并（组大小相乘），否则开新组。
6. **去平凡组**：丢掉 size==1 的组（除非全是平凡组，保留一个 `(1, 0, 0)`）。
7. 返回 `(out_shape, [组大小], [组 a stride], [组 b stride])`。

对应地，kernel 侧有一条「同形连续快路径」：当两输入形状相同且都连续（无广播）时，`_is_contiguous_same_shape` 返回 True，kernel 直接 `y[idx] = op(a[idx], b[idx])`，**零 divmod**。这是 `coalesce` 降维的极限情形。

#### 4.2.3 源码精读

[_base.py:484-543](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/elementwise/_base.py#L484-L543) —— `coalesce_broadcast_dims`。关键合并判定：

```python
a_can = (a_raw[i] == 0 and prev_as == 0) or (
    a_raw[i] != 0 and prev_as == a_raw[i] * out_shape[i])
b_can = (b_raw[i] == 0 and prev_bs == 0) or (
    b_raw[i] != 0 and prev_bs == b_raw[i] * out_shape[i])
if a_can and b_can:
    groups[-1] = (prev_out * out_shape[i], a_raw[i], b_raw[i])
```

即「都广播」或「都连续且 stride 衔接」才能合并。

[_base.py:747-758](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/elementwise/_base.py#L747-L758) —— `BinaryOp.__init__` 调用点：把 `coalesced_shape, a_strides, b_strides` 喂给 `_build_kernel_instance`。

kernel 侧的两个搭档：

[elementwise.py:367-383](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/kernels/elementwise.py#L367-L383) —— `_compute_broadcast_offsets`：kernel 内部把一维 `flat_idx` 还原成 `a_off`/`b_off` 的「展开 divmod 链」。注释明确：除 `flat_idx` 外所有参数都是 Python 编译期常量，循环会在 kernel 构建期**展开**。`divmod` 次数 = `ndim - 1`（`ndim` 是 coalesce **之后**的有效维数）。

[elementwise.py:386-392](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/kernels/elementwise.py#L386-L392) —— `_is_contiguous_same_shape`：同形连续快路径判定（一维、两边 stride 全 1）。命中时 kernel 跳过整条广播机制。

[elementwise.py:443-459](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/kernels/elementwise.py#L443-L459) —— `_make_binary_direct` 的快路径分支：同形连续时 kernel 体只有 `y[idx] = op(a[idx], b[idx])`，无 divmod；否则走带 `_compute_broadcast_offsets` 的广播分支（461 行起）。

#### 4.2.4 代码实践

**实践目标**：亲手算一个广播例子，对比「朴素 ndim」与「coalesce 后」的 divmod 次数，理解降维收益。

**操作步骤（可在纯 host 跑）**：

`coalesce_broadcast_dims` 是纯 Python host 函数，不依赖 CUDA，可直接调用。写一段脚本：

```python
# 示例代码：仅用于观察 coalesce 行为，非项目原有代码
from tileops.ops.elementwise._base import coalesce_broadcast_dims

# 例 1：a=(3,1,5), b=(4,5) —— 广播到 (3,4,5)
print(coalesce_broadcast_dims((3, 1, 5), (4, 5)))
# 例 2：两个完全同形连续张量 a=(2,3,4), b=(2,3,4)
print(coalesce_broadcast_dims((2, 3, 4), (2, 3, 4)))
```

**需要观察的现象**：

- 例 1 中 `out_shape = (3, 4, 5)`（ndim=3），看 `coalesced_shape` 是否把可合并的维压成更少组（例如把某些相邻维合并），`a_strides`/`b_strides` 中广播维是否为 0。朴素做法要 `3-1=2` 次 divmod；coalesce 后次数 = `len(coalesced_shape) - 1`。
- 例 2 中两边同形连续，`coalesced_shape` 应退化为单一组（如 `(24,)`），divmod 次数降到 0（kernel 走 `_is_contiguous_same_shape` 快路径）。

**预期结果**：你能用具体数字说明「一个 `(3,1,5)` 与 `(4,5)` 的广播，coalesce 把它压成更少的有效维，kernel 内 divmod 次数下降」。例 2 的同形情形应触发零 divmod 快路径。具体合并结果「待本地验证」（取决于 `torch.broadcast_shapes` 与合并规则的精确交互，跑一次即可看到）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `coalesce_broadcast_dims` 要把「真广播维」的 stride 置 0，而不是保留原始 stride？

> **答案**：广播维的大小是 1，原始 stride 在物理上无意义（该维只有一个元素）。置 0 后，无论 `flat_idx` 还原出的该维坐标是多少，`coord * 0 = 0`，等价于「反复读同一个元素」——这正是广播语义。这样 kernel 用一套统一的 `coord * stride` 公式就能同时处理真实维与广播维，无需分支。

**练习 2**：如果两个输入完全同形且连续，`coalesce_broadcast_dims` 的输出会让 kernel 走哪条路径？为什么这条路径最快？

> **答案**：`coalesced_shape` 退化为单一组，`_is_contiguous_same_shape` 返回 True，kernel 走「零 divmod」快路径，线程直接用 `flat_idx` 索引 `a`/`b`/`y`。最快是因为：省掉了整条 `_compute_broadcast_offsets` 的 divmod 链，且访问是完全连续的，便于 GPU 做 128-bit 向量化装载（`register_copy` 策略还能用 fragment 进一步加速）。

**练习 3**：合并判定里 `prev_as == a_raw[i] * out_shape[i]` 这个条件在表达什么？

> **答案**：表达「前一组的 stride 恰好等于当前维 stride × 当前维大小」，即前一维在内存中跨过的距离与当前维的步长衔接——这是「两维可以线性合并成一个更大的连续维」的充要条件。只有两边都满足（`a_can and b_can`）才能合并，保证合并后两组坐标用同一套 stride 仍能正确寻址。

---

### 4.3 inplace 分发：`_UnaryActivationMixin` 与 `_wrapped_inplace`

#### 4.3.1 概念说明

许多激活函数（ReLU、SiLU、LeakyReLU…）有 `inplace=True` 模式：不分配新输出，直接把结果写回输入张量。在 eager 模式下这只需 `input.copy_(result)`；但在 `torch.compile` 下，dynamo 必须知道「这个 op 会改写参数」，否则图追踪会出错。

PyTorch 的机制是：用 `@torch.library.custom_op(..., mutates_args=("x",))` 注册一个**单独的**原地 custom_op，它返回 `None`、直接改写 `x`。TileOPs 把它挂在类属性 `_wrapped_inplace` 上。

不是所有 unary 算子都支持 inplace——只有 manifest signature 里**声明了 `inplace` 参数**的叶子（ReLU、SiLU、HardSwish、HardSigmoid、Mish、SELU、LeakyReLU、ELU、Hardtanh）才会注册 `_wrapped_inplace`。于是 TileOPs 抽了一个**二级共享基类** `_UnaryActivationMixin`，把「`inplace` 分发」这条 `forward` 流程收拢：

- `inplace=True` 且 `_wrapped_inplace` 已注册 → 调 `_wrapped_inplace(input, instance_key)`，返回原 `input`（保证 `y is x`）；
- `inplace=True` 但未注册（如测试子类）→ 回退 eager：`input.copy_(result.reshape(input.shape))`；
- `inplace=False` → 走标准 `_wrapped` / `_eager_forward`。

在此之上还有两个更窄的共享基类：

- `_ParamFreeActivationOp`：给「唯一参数就是 `inplace`」的激活（ReLU/SiLU/HardSwish/…）用的构造器，统一 `(N_total, dtype, inplace=False, *, kernel_map, tune)`。
- `_ParametricActivationOp`：给「带标量参数」（LeakyReLU 的 `negative_slope`、ELU 的 `alpha`、Hardtanh 的 `min_val/max_val`、Softplus 的 `beta/threshold`）的激活用的；因为标量名/默认值各异，叶子**自建 kernel** 后调 `_finalize_init` 来完成共享状态装配（登记 `_OP_REGISTRY`、设 `output_dtype` 等）。

#### 4.3.2 核心流程

`_UnaryActivationMixin.forward(input)` 的分支：

```
forward(input):
  _validate_input(input)
  if self.inplace:
    if _wrapped_inplace is not None:        # 已注册原地 custom_op
        _wrapped_inplace(input, instance_key)   # mutates x in place
        return input                            # y is x
    else:                                       # 未注册（测试子类）
        result = _eager_forward(input)
        input.copy_(result.reshape(input.shape))
        return input
  else:                                       # 非原地
    if _wrapped is not None:
        return _wrapped(input, instance_key)
    return _eager_forward(input)
```

`_wrapped_inplace` 由 `_register_unary_inplace_custom_op` 在包加载时创建：它内部仍调 `_eager_forward`（kernel 写入新 buffer），再把结果 `copy_` 回 `x`——kernel 本身不原地写，原地性完全由这个包装层提供。这样所有 inplace 激活共用同一个 kernel 实现。

`_ParametricActivationOp._finalize_init` 的角色：因为参数化激活的叶子各自直接 `self.kernel_map[_op_name](..., alpha=..., tune=...)` 建 kernel（绕过 `UnaryOp.__init__`），`_finalize_init` 负责把 `UnaryOp.__init__` 本会做的共享状态补齐——记录 `N_total`/`dtype`/`inplace`/`kernel`、算 `output_dtype`、登记 `_OP_REGISTRY`。

#### 4.3.3 源码精读

[_base.py:976-991](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/elementwise/_base.py#L976-L991) —— `_UnaryActivationMixin.forward`：inplace 与非 inplace 的统一分发点。

[_base.py:151-171](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/elementwise/_base.py#L151-L171) —— `_register_unary_inplace_custom_op`：注册 `mutates_args=("x",)` 的 custom_op，体里 `result = instance._eager_forward(x); x.copy_(result.reshape(x.shape))`。注意它返回 `None`。

[_base.py:1005-1015](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/elementwise/_base.py#L1005-L1015) —— `_ParamFreeActivationOp.__init__`：标准 `(N_total, dtype, inplace=False, *, kernel_map, tune)` 构造器，调 `UnaryOp.__init__` 后记 `self.inplace`。

[_base.py:1035-1060](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/elementwise/_base.py#L1035-L1060) —— `_ParametricActivationOp._finalize_init`：叶子自建 kernel 后调它，补齐 `output_dtype`（含 FP8 post-cast 语义）与 `_OP_REGISTRY` 登记。

叶子对照：

[activations.py:38-45](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/elementwise/activations.py#L38-L45) —— `ReluFwdOp(_ParamFreeActivationOp)`：参数自由，`inplace` 由 `_ParamFreeActivationOp.__init__` 默认 `False` 接住。

[activations.py:151-192](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/elementwise/activations.py#L151-L192) —— `LeakyReluFwdOp(_ParametricActivationOp)`：自带 `__init__`，校验 `negative_slope` 标量、自建 kernel、调 `_finalize_init(..., inplace=inplace)`。注意类体里 `_wrapped = None`——这只是「注册前的安全默认」，包加载时会被 `_register_unary_custom_op(LeakyReluFwdOp)` 覆写（见 `__init__.py:240-244`）。

[__init__.py:251-255](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/elementwise/__init__.py#L251-L255) —— inplace 注册循环：只对声明了 `inplace` 的 9 个激活叶子调 `_register_unary_inplace_custom_op`。

#### 4.3.4 代码实践

**实践目标**：跟踪 `LeakyReluFwdOp(N, dtype, 0.01, inplace=True)(x)` 的完整调用链，确认原地路径下 `y is x`。

**操作步骤（源码阅读型）**：

1. 从 `Op.__call__` 进入 `_UnaryActivationMixin.forward`（976 行）。
2. `self.inplace` 为 True → 取 `type(self)._wrapped_inplace`（`LeakyReluFwdOp` 在 `__init__.py:251-255` 已注册）。
3. 调 `_wrapped_inplace(input, self._instance_key)` → custom_op eager 体（166 行）：`instance._eager_forward(x)` 拿 result，再 `x.copy_(result.reshape(x.shape))`。
4. 回到 `forward`，`return input`——返回的就是传入的张量对象。

**需要观察的现象**：

- 调用前后 `x` 的 `data_ptr()` 不变（同一块内存），值被改写；返回值 `y` 与 `x` 是同一对象（`y is x` 为 True）。
- kernel 本身（`LeakyReluFwdKernel`）写入的是一个**新 buffer**，原地性完全由 `_wrapped_inplace` 的 `copy_` 提供。

**预期结果**：你能画出 `forward → _wrapped_inplace → _eager_forward → kernel → copy_` 的链路，并解释「kernel 不原地写、原地性在包装层」这个设计让所有激活共用同一 kernel 实现。运行验证「待本地验证」（需 CUDA GPU）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `_wrapped_inplace` 注册成 `mutates_args=("x",)` 而 `_wrapped`（非原地版）注册成 `mutates_args=()`？

> **答案**：非原地版把结果作为**新张量返回**，不修改任何输入，所以 `mutates_args=()`；dynamo 把它当作纯函数追踪。原地版直接改写参数 `x`、返回 `None`，必须用 `mutates_args=("x",)` 显式声明，dynamo 才能在图里插入正确的 mutation 节点，避免把「读 x 旧值」和「写 x」重排错。

**练习 2**：`SoftplusFwdOp` 也是 `_ParametricActivationOp` 的子类，但它**没有**出现在 inplace 注册循环里。这意味着什么？

> **答案**：意味着 `SoftplusFwdOp` 不支持 `inplace`（manifest signature 里没有 `inplace` 参数）。它的 `_wrapped_inplace` 保持 `None`。即便某处强行设 `self.inplace=True`，`_UnaryActivationMixin.forward` 也会走「未注册」的 eager 回退分支（`input.copy_(...)`）——但这不是设计支持的的场景。

**练习 3**：参数化激活的叶子为什么要绕过 `UnaryOp.__init__`、自建 kernel 后调 `_finalize_init`？

> **答案**：因为每个参数化激活的构造参数不同（`negative_slope` / `alpha` / `min_val,max_val` / `beta,threshold`），kernel 构造调用也各不相同（`kernel_map[_op_name](N, dtype, alpha=alpha, tune=...)` 等）。无法用一个统一的 `__init__` 签名覆盖。但**构造之后**的状态装配（记 `N_total`/`dtype`/`inplace`、算 `output_dtype`、登记 `_OP_REGISTRY`）对所有参数化激活都一样，所以抽到 `_finalize_init` 共享。

---

### 4.4 FP8 后置 cast 与统一 roofline 公式

#### 4.4.1 概念说明

这一节把两个看似不相关的共享机制并到一起讲，因为它们都体现了「伞形基类为所有叶子提供统一公式」的设计取向。

**机制一：FP8 后置 cast（`_apply_fp8_post_cast`）。**

承接 u3-l3 的「提升到 fp32 计算、在边界 cast 回存储 dtype」。FP8 有两种格式，饱和语义不同：

- `e4m3fn` **无** Inf/NaN 表示：TileLang 的 `T.Cast` 是饱和转换，把溢出钳到 ±448.0——对这个格式是**正确**的，所以 kernel 直接饱和 cast 回 `e4m3fn`。
- `e5m2` **有** Inf/NaN 表示：饱和转换会错误地把 Inf 钳到 max-finite。所以 kernel **产出 fp16**（保留 Inf/NaN），由 **Op 层**用 PyTorch 的 `.to()` 做最终**非饱和** cast 回 `e5m2`。

这条「边界 cast」逻辑统一收在两个地方：

- Kernel 侧用 `_fp8_output_dtype` 属性标记「需要 Op 层 post-cast」（仅 e5m2 会设它）；
- Op 侧用 `_apply_fp8_post_cast(result, kernel)` 在 `_eager_forward` 末尾做最终 cast，并用 `_resolve_output_dtype()` 让 `output_dtype` 反映**最终** dtype（而非 kernel 的中间 fp16）。

**机制二：统一 roofline 公式（`FLOPS_PER_ELEM`）。**

u11-l1 强调：codegen 契约（`_validate_dtypes`/`eval_roofline`）必须 class-local。`UnaryOp` 与 `FusedGatedOp` 选择**手写** `eval_roofline`，给所有叶子一个按「每元素 FLOP 系数」的统一公式：

- `UnaryOp.eval_roofline() = (FLOPS_PER_ELEM * N_total, total_memory)`
- `FusedGatedOp.eval_roofline() = (FLOPS_PER_ELEM * M * N, total_memory)`

叶子只需用类属性 `FLOPS_PER_ELEM` 表达差异（relu=1、sigmoid=4、gelu=5、gelu_tanh_and_mul=10…），并在注释里与 manifest `roofline.flops` 的系数交叉核对。`BinaryOp` 则**不**手写 `eval_roofline`，二元算子的 roofline 由 manifest 驱动的 codegen 合成（见 u8）——这是三种基类的又一个刻意不对称。

`total_memory` 也统一：read 输入 + write 输出，且输出字节数按 `output_dtype`（FP8 post-cast 后的最终 dtype）算，确保字节记账与实际 I/O 一致。

#### 4.4.2 核心流程

FP8 输出 dtype 的解析链：

```
kernel.__init__:  若 e5m2 → _fp8_output_dtype = e5m2, output_dtype = fp16
                  否则    → _fp8_output_dtype = None,   output_dtype = INPUT_DTYPE or dtype
Op.__init__:      output_dtype = _resolve_output_dtype()  # 优先 _fp8_output_dtype
_eager_forward:   result = kernel(...)
                  return _apply_fp8_post_cast(result, kernel)  # e5m2: .to(e5m2); 否则原样
```

roofline 公式：

```
UnaryOp.eval_roofline():
  flops = FLOPS_PER_ELEM * N_total
  bytes = N_total * (dtype.itemsize + output_dtype.itemsize)   # = total_memory
  return (flops, bytes)
```

#### 4.4.3 源码精读

[_base.py:546-555](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/elementwise/_base.py#L546-L555) —— `_apply_fp8_post_cast`：若 `kernel._fp8_output_dtype` 非空就 `result.to(fp8_out)`，否则原样返回。

[_base.py:617-623](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/elementwise/_base.py#L617-L623) —— `UnaryOp._resolve_output_dtype`：优先用 `_fp8_output_dtype`（最终 dtype），否则 `kernel.output_dtype`，再否则 `self.dtype`。

[_base.py:629-647](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/elementwise/_base.py#L629-L647) —— `UnaryOp.total_memory`（read x + write y，按 `output_dtype` 计输出字节）与 `eval_roofline`（`FLOPS_PER_ELEM * N_total`）。docstring 明确：sigmoid/tanh 等更高系数的叶子靠覆写 `FLOPS_PER_ELEM`。

[_base.py:877-899](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/elementwise/_base.py#L877-L899) —— `FusedGatedOp.total_memory`（read `M*2N` + write `M*N`）与 `eval_roofline`（`FLOPS_PER_ELEM * M * N`，默认 `FLOPS_PER_ELEM = 6`）。

[_base.py:649-656](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/elementwise/_base.py#L649-L656) —— `UnaryOp._eager_forward`：末尾调 `_apply_fp8_post_cast(result, self.kernel)`。

叶子 `FLOPS_PER_ELEM` 与 manifest 的对齐：

[activations.py:62-67](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/elementwise/activations.py#L62-L67) —— `GeluFwdOp.FLOPS_PER_ELEM = 5`，注释逐项拆解 `gelu = div + erf + add + mul-by-half + mul`，与 manifest `roofline.flops = "5 * N"` 对齐。

[activations.py:331-347](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/elementwise/activations.py#L331-L347) —— `SiluAndMulFwdOp`（继承 `FusedGatedOp` 默认 6）与 `GeluTanhAndMulFwdOp`（覆写为 10）。

kernel 侧的 FP8 dtype 解析：

[elementwise.py:213-226](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/kernels/elementwise.py#L213-L226) —— `_get_fp8_output_dtypes`：e5m2 返回 `(e5m2, fp16)`（需要 post-cast），其他返回 `(None, dtype)`。

[elementwise.py:654-659](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/kernels/elementwise.py#L654-L659) —— `UnaryKernel.__init__` 里设置 `_fp8_output_dtype`（仅 e5m2）。

#### 4.4.4 代码实践

**实践目标**：验证手写 `eval_roofline` 的输出与 manifest `roofline.flops` 系数一致，并理解 FP8 e5m2 的「kernel 出 fp16、Op 层 cast」两段式。

**操作步骤（源码阅读型）**：

1. 任选一个 unary 叶子，如 `GeluFwdOp`（`FLOPS_PER_ELEM = 5`）。读 `UnaryOp.eval_roofline`（634 行）：对 `N_total = 4096`、dtype=fp16，应得 `flops = 5 * 4096 = 20480`，`bytes = 4096 * (2 + 2) = 16384`。
2. 打开 `tileops/manifest/elementwise_unary_activation.yaml` 找 `gelu` 条目的 `roofline.flops`，确认系数也是 `5 * N`（manifest 是 roofline 的真相来源；代码侧的 `FLOPS_PER_ELEM` 必须与之一致）。
3. 读 `_apply_fp8_post_cast`（546 行）与 `UnaryKernel.__init__` 的 e5m2 分支（654 行）：确认 e5m2 时 `kernel.output_dtype = fp16`、`_fp8_output_dtype = e5m2`，Op 层 `.to(e5m2)` 收尾。

**需要观察的现象**：

- `eval_roofline` 返回的 `(flops, bytes)` 与 manifest 声明、与 `total_memory` 三者一致。
- e5m2 的 `output_dtype`（经 `_resolve_output_dtype`）是 `e5m2`（itemsize=1），所以 `total_memory` 的输出字节按 1 算，而非 fp16 的 2——字节记账与最终实际写出一致。

**预期结果**：你能解释「为什么 e5m2 的 kernel 产出 fp16 却不影响 roofline 的字节统计」——因为 `_resolve_output_dtype` 与 `total_memory` 都按最终 `_fp8_output_dtype`（e5m2）算。具体 manifest 数值「待本地验证」（跑一次 `load_manifest()` 读 `gelu` 条目即可）。

#### 4.4.5 小练习与答案

**练习 1**：为什么 e5m2 必须在 **Op 层**做最终 cast，而不能让 kernel 直接输出 e5m2？

> **答案**：TileLang 的 `T.Cast` 是**饱和**转换。e5m2 有 Inf/NaN 表示，饱和转换会把 Inf 错误地钳到 max-finite，丢失 Inf/NaN。所以 kernel 先在 fp16 里算完（保留 Inf/NaN），再由 Op 层用 PyTorch 的 `.to()` 做**非饱和**转换回 e5m2。e4m3fn 没有 Inf 表示，饱和转换本就正确，所以 kernel 直接饱和 cast 回 e4m3fn、无需 Op 层介入。

**练习 2**：`BinaryOp` 没有手写 `eval_roofline`，那 `AddFwdOp` 的 roofline 从哪来？

> **答案**：来自 manifest 驱动的 codegen（见 u8-l1/u8-l2）。`Op.__init_subclass__` 在类定义时按 manifest 的 `roofline` 块合成 `eval_roofline` 方法体并安装。这是 `UnaryOp`/`FusedGatedOp`（手写统一公式）与 `BinaryOp`（manifest codegen）的刻意不对称：unary/fused-gated 的每元素 FLOP 系数可以用一个 `FLOPS_PER_ELEM` 整数统一表达，而二元算子的字节记账更依赖具体语义（比较/逻辑输出 bool、add/sub 有 alpha 缩放等），交给 manifest 更合适。

**练习 3**：`total_memory` 为什么用 `output_dtype.itemsize` 而不是 `dtype.itemsize` 算输出字节？

> **答案**：因为输出 dtype 可能与输入不同。最典型的是 FP8 e5m2：输入是 e5m2（1 字节），但 kernel 中间产出 fp16，最终又 cast 回 e5m2——实际**写出**的字节按 e5m2（1 字节）算。`output_dtype` 经 `_resolve_output_dtype` 已反映最终 dtype，用它算字节才与真实 I/O 一致，roofline 的带宽估计才准。对 bool 输出的比较/逻辑算子（`output_dtype = bool`，1 字节）同理。

---

## 5. 综合实践

**任务**：假设要新增一个一元激活 `celu(x) = max(0, x) + min(0, alpha * (exp(x) - 1))`（带一个标量参数 `alpha`，支持 `inplace`）。请基于本讲三个伞形基类，**只写叶子类**完成接入，并画出从「类定义」到「`op(x)` 执行」的完整链路。

要求：

1. 判断它该继承哪个伞形基类（提示：带标量参数 + 支持 inplace → `_ParametricActivationOp`），并说明为什么**不**直接继承 `UnaryOp`。
2. 写出叶子类的骨架：`_op_name`、`kernel_cls`、`FLOPS_PER_ELEM`、`__init__`（校验 `alpha`、自建 kernel、调 `_finalize_init`）、`default_kernel_map`。说明哪些是「必须自己写」、哪些是「白嫖基类」。
3. 解释在这个新算子上，本讲四个机制各自如何生效：
   - 模板化：`forward` 来自 `_UnaryActivationMixin`/`UnaryOp`，无需自写；
   - 广播 coalesce：**不适用**（一元算子，构造期给 `N_total`，无广播），说明为什么；
   - inplace 分发：`inplace=True` 时走 `_wrapped_inplace`（前提是在 `__init__.py` 注册循环里加上这个叶子）；
   - FP8 后置 cast / roofline：`_eager_forward` 末尾自动 `_apply_fp8_post_cast`；roofline 用 `FLOPS_PER_ELEM`（按 celu 的运算拆解，如 `compare-select + exp + sub + mul = 4`，与 manifest 对齐）。
4. 指出要让 `torch.compile` 支持它，需要在 `tileops/ops/elementwise/__init__.py` 的**两个**注册循环里各加一行（普通 unary 注册 + inplace 注册），并解释为什么这两步缺一不可。

**交付物**：一段叶子类源码草拟 + 一张调用链图（`op(x) → __call__ → _UnaryActivationMixin.forward → _wrapped/_wrapped_inplace → _eager_forward → kernel`）。

这个任务把本讲的「模板化复用」「inplace 分发」「FP8/roofline 统一公式」三条主线串起来，并要求你判断「广播 coalesce 不适用」的边界——从而真正理解三个伞形基类各自负责什么、不负责什么。

## 6. 本讲小结

- elementwise 用三个 **L2 伞形基类** `UnaryOp`/`BinaryOp`/`FusedGatedOp` 分别封装「1→1」「2→1 广播」「(M,2N)→(M,N) 融合门控」三种 `forward` 流程；叶子是 T1 薄包装，只填 `_op_name` + `kernel_cls`（+ 可选 `FLOPS_PER_ELEM`/`_other_name`）。
- 模板化的两个支点是 `kernel_cls`（Kernel 类）与 `_op_name`（dispatch key + custom_op 命名后缀）；`default_kernel_map = {_op_name: kernel_cls}`，`_build_kernel_instance` 是可 override 的构造钩子。
- `coalesce_broadcast_dims` 把 N 维广播按「同广播行为」贪心合并成最少有效维，返回合并后的形状与 stride（广播维 stride=0），让 kernel 内 divmod 次数从 `ndim-1` 降到 `len(coalesced)-1`；同形连续时退化为零 divmod 快路径。
- inplace 分发由二级基类 `_UnaryActivationMixin` 统一承载：`inplace=True` 走 `_wrapped_inplace`（`mutates_args=("x",)`，返回 `input` 保证 `y is x`），kernel 本身不原地写、原地性在包装层；`_ParamFreeActivationOp`/`_ParametricActivationOp` 进一步收拢两类激活的构造差异。
- FP8 e5m2 走「kernel 出 fp16、Op 层 `_apply_fp8_post_cast` 非饱和 cast」两段式；`UnaryOp`/`FusedGatedOp` 手写 `eval_roofline`（`FLOPS_PER_ELEM` 系数），`BinaryOp` 则交给 manifest codegen——三种基类的刻意不对称。
- `_wrapped = None` 是类体内的安全默认，包加载时由 `__init__.py` 注册循环覆写为真实 custom_op；未注册则 `forward` 回退 `_eager_forward`。

## 7. 下一步学习建议

- **[u11-l3 维度参数化家族（pool / reduction）](u11-l3-dimension-parametrized-families.md)**：对比另一种 L2 抽取方式——用单一 `ndim` 泛化基类 + 表驱动命名表达 1d/2d/3d 变体，与本文「按元数分三个伞形基类」形成互补。
- **深挖二级共享基类**：本讲只详讲了三个主伞形基类。`_base.py` 还有一批更窄的二级基类值得通读——`_AlphaScaledBinaryOp`（add/sub 的 alpha 缩放）、`_BoolOutputBinaryOp`（比较/逻辑的 bool 输出 + uint8 存储 kernel 选择）、`_IntIdentityUnaryOp`（整数输入短路、不建 kernel）、`_GeluApproximateBase`（解析 `approximate` 字段）。它们展示了「在伞形基类之下再抽一层共享逻辑」的手法。
- **回顾 u8 codegen**：`BinaryOp` 不手写 `eval_roofline`，可结合 [u8-l1](u8-l1-codegen-init-subclass-hook.md) / [u8-l2](u8-l2-roofline-codegen.md) 看 manifest `roofline` 块如何被合成成 `eval_roofline` 方法体，理解「手写公式 vs codegen」两种取向的边界。
- **阅读 elementwise Kernel 侧**：`tileops/kernels/elementwise.py` 的 `UnaryKernel`/`BinaryKernel`/`FusedGatedKernel` 与三种策略工厂（direct / explicit_parallel / register_copy），看 Op 层的 `coalesced_shape`/`strides` 如何变成 kernel 里的 `_compute_broadcast_offsets` divmod 链。
