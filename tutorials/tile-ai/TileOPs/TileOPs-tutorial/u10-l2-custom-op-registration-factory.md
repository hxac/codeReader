# u10-l2 custom_op 注册工厂

## 1. 本讲目标

学完本讲，你应当能够：

- 说清楚为什么每个 elementwise 算子（几十个）只需要写「一个类」，就能在**包加载时**自动获得一个 `torch.library.custom_op` 注册；
- 画出 `forward → _wrapped(custom_op) → instance._eager_forward` 这条被 `torch.compile` 追踪的调用链，并解释 `instance_key` 如何在运行时把「静态注册的 custom_op」路由到「动态构造的 Op 实例」；
- 区分 in-place 路径（`_wrapped_inplace`，`mutates_args=("x",)`）与普通 out-of-place 路径，理解为何 in-place 必须单独注册；
- 解释 `register_fake`（fake/meta 函数）如何为 `torch.compile(fullgraph=True)` 推导输出 shape/dtype，以及为何多输入算子必须在 fake 里用 `torch.broadcast_shapes`。

本讲承接 [u10-l1 编译边界不变量](u10-l1-compile-dispatch-boundary.md)。u10-l1 讲了「把不可追踪的 kernel 构造藏进 custom_op 的 eager 体」这条**不变量**与规范范本（pool/batch_norm 走 `compile_boundary` 字符串 key）；本讲进入 `tileops/ops/elementwise/_base.py`，看 elementwise 家族那套**更早**的注册工厂与 int-key 注册表的全貌，以及它们与规范范本的关系。

## 2. 前置知识

本讲默认你已经建立以下心智模型（来自 u1 / u2 / u10-l1）：

- **Op(L2)/Kernel(L1) 双层分离**：Op 是主机侧无状态入口，Kernel 是 TileLang GPU 实现。`Op.forward` 是用户调用的入口。
- **可调用契约**：`op(*inputs)` 经 `__call__` 转发给 `forward`。
- **`torch.compile` 的两条体**：当一个 op 被 `torch.compile(op, fullgraph=True)` 时，`torch.dynamo` 会用 **FakeTensor**（只有 shape/dtype、没有真实数据的「meta 张量」）去追踪 `forward`。一旦追踪路径里出现「构造 Kernel / 进入 TileLang builder」这类 dynamo 不认识的 Python 副作用，图就会断裂（graph break）。解决办法是把这些不可追踪的操作包进 `torch.library.custom_op`：
  - **eager 体**：编译关闭或在 eager fallback 时真正执行（这里是「查注册表 → 取实例 → 跑 kernel」的不可追踪路径）；
  - **fake 体**（`register_fake`）：编译追踪时被调用，**只负责返回一个 shape/dtype 正确的 FakeTensor**，告诉 dynamo 输出长什么样。
- **`mutates_args`**：`custom_op` 必须声明它修改了哪些参数（in-place 算子改 `x`，就要写 `mutates_args=("x",)`），否则 `torch.compile` 会把这次修改当作不存在，导致图追踪出错。

> 名词速查：**custom_op** 指 `torch.library.custom_op` 注册的原子算子，有自己的命名空间（如 `top::elementwise_binary_add`）；**fake 函数**指 `@custom_op.register_fake` 装饰的元数据函数；**eager 体**指 custom_op 装饰器下那个真正跑计算的函数。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [tileops/ops/elementwise/_base.py](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/elementwise/_base.py) | 本讲主角：三个伞形基类 `UnaryOp`/`BinaryOp`/`FusedGatedOp`、十几个 `_register_*_custom_op` 工厂、elementwise 自己的 int-key `_OP_REGISTRY`、`_eager_forward` 与 `forward` 分发。 |
| [tileops/ops/elementwise/__init__.py](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/elementwise/__init__.py) | 包加载时把工厂套到每个叶子算子类上，完成 `custom_op` 注册（`_wrapped` / `_wrapped_inplace` 的赋值发生在这里）。 |
| [tileops/ops/compile_boundary.py](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/compile_boundary.py) | 规范范本的**字符串 key** 注册表（u10-l1 主角）。本讲拿它和 elementwise 的 int-key 注册表做对比。 |
| [tileops/ops/op_base.py](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/op_base.py) | `Op.dispatch_kernel`：所有合规 `__init__` 的公共注册点，会调 `compile_boundary.register_instance`（字符串 key）。 |
| [tests/ops/test_elementwise_compile.py](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tests/ops/test_elementwise_compile.py) | 对每个注册算子跑 `torch.compile(op, fullgraph=True)` 的冒烟测试，是「注册成功 + fake shape 正确」的回归守卫。 |
| [tests/compile_contract.py](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tests/compile_contract.py) | fullgraph 证据登记表：编译测试在这里登记它担保的 op 类，manifest 的 `torch_compile_fullgraph` 声明必须与之对齐。 |

---

## 4. 核心概念与源码讲解

### 4.1 custom_op 工厂：把「写一个类」变成「注册一个算子」

#### 4.1.1 概念说明

elementwise 家族有几十个算子（relu、add、eq、where、clamp……）。如果每个算子都要手写一段 `torch.library.custom_op(...)` 注册代码，会重复到无法维护。`_base.py` 的做法是用**工厂函数**把重复逻辑抽出来：`_register_unary_custom_op`、`_register_binary_custom_op`、`_register_fused_gated_custom_op` 等。每个工厂接收一个 Op 子类，读它的 `_op_name`，**在类定义时**为它生成一个 `custom_op`，并把这个 `custom_op` 挂到 `op_cls._wrapped` 上。

关键在于「**注册一次，按 key 路由多次**」：`custom_op` 是挂在**类**上的，所有该类的实例共享同一个 `_wrapped`；真正区分「是哪个实例」的，是调用时传入的 `instance_key` 参数。这样几十个算子 + 每个算子任意多个运行时实例，都能被一套统一的注册表分派。

#### 4.1.2 核心流程

以 unary 为例，包加载时 `__init__.py` 调 `_register_unary_custom_op(ReluFwdOp)`，工厂内部：

1. 取 `op_name = op_cls._op_name`（如 `"relu"`）；
2. 用 `@torch.library.custom_op("top::elementwise_unary_relu", mutates_args=())` 定义一个 `_wrapped(x, instance_key) -> Tensor`，eager 体为「查注册表 → 调 `instance._eager_forward(x)`」；
3. 用 `@_wrapped.register_fake` 定义 fake 体，返回一个同 shape 的空张量；
4. `op_cls._wrapped = _wrapped`，让运行时 `forward` 能拿到它。

```text
包加载(import)
   │
   ▼
_register_*_custom_op(LeafOp)   ← 工厂，对每个叶子类调用一次
   │  读 LeafOp._op_name
   │  生成 custom_op  →  挂到 LeafOp._wrapped（类属性）
   ▼
运行时: op = LeafOp(...)        ← 构造实例，登记到 _OP_REGISTRY[id(op)]
        op(x)
         └─ forward(x)
              └─ type(op)._wrapped(x, op._instance_key)   ← 调用类级 custom_op
                   └─ _OP_REGISTRY[instance_key]._eager_forward(x)
```

> 注意时序：`_wrapped` 在**类定义/import** 时就有了（每个类一个）；`instance_key` 在**实例构造**时才有（每个实例一个）。两者解耦，是「工厂 + 注册表」模式能 scale 的关键。

#### 4.1.3 源码精读

**工厂本体**：[_base.py:129-148](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/elementwise/_base.py#L129-L148) 是 `_register_unary_custom_op`。它构造 custom_op、注册 fake、再把结果挂到 `op_cls._wrapped`：

```python
@torch.library.custom_op(f"top::elementwise_unary_{op_name}", mutates_args=())
def _wrapped(x: torch.Tensor, instance_key: int) -> torch.Tensor:
    instance = _OP_REGISTRY[instance_key]
    return instance._eager_forward(x)

@_wrapped.register_fake
def _(x: torch.Tensor, instance_key: int) -> torch.Tensor:
    out_dtype = output_dtype_override if output_dtype_override is not None else x.dtype
    return torch.empty_like(x, dtype=out_dtype)

op_cls._wrapped = _wrapped
```

binary 工厂 [_base.py:174-203](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/elementwise/_base.py#L174-L203) 多了一个 `out_shape: List[int]` 参数（用于 fake，见 4.3）和 `output_bool` 开关（比较/逻辑算子输出 bool）：

```python
@_wrapped.register_fake
def _(a, b, out_shape, instance_key) -> torch.Tensor:
    out_dtype = torch.bool if output_bool else a.dtype
    return a.new_empty(out_shape, dtype=out_dtype)
```

fused-gated 工厂 [_base.py:454-481](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/elementwise/_base.py#L454-L481) 的 fake 用 `(M, N)` 构造输出（输入是 `(M, 2N)`，输出是 `(M, N)`，见 FusedGatedOp 类文档 [_base.py:822-839](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/elementwise/_base.py#L822-L839)）。

**命名空间设计**：custom_op 用 `top::elementwise_<族>_<op_name>` 命名，多输入变体用后缀区分以避免碰撞——例如标量版 `masked_fill` 与张量值版 `masked_fill_tensor_value`（[_base.py:327-358](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/elementwise/_base.py#L327-L358)）、标量版 `lerp` 与张量版 `lerp_tensor`（[_base.py:262-294](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/elementwise/_base.py#L262-L294)）。同一命名空间重复注册会在 import 时直接报错，所以必须错开。

**包加载时的批量注册**：[__init__.py:198-255](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/elementwise/__init__.py#L198-L255) 用几个 `for _cls in [...]: _register_*_custom_op(_cls)` 循环把工厂套到全部叶子类上。注释把算子按「float 保留输出 / bool 输出 / 同 dtype 二元 / bool 输出二元 / fused-gated / 独立 unary / inplace 同伴」分组，每一组用一个工厂变体。这段代码就是「import 即注册」的发生地。

#### 4.1.4 代码实践

**实践目标**：确认「注册发生在 import 时、且每个类只有一个 `_wrapped`」。

1. 打开一个装好 TileOPs 的 Python（需 CUDA）：
   ```python
   import tileops.ops.elementwise as ew   # 触发 __init__.py 的批量注册
   from tileops.ops.elementwise import AddFwdOp, ReluFwdOp
   # _wrapped 是类属性（custom_op 对象），两个不同实例共享同一个
   a = AddFwdOp(a_shape=(8,), b_shape=(8,), dtype=__import__('torch').float16)
   b = AddFwdOp(a_shape=(8,), b_shape=(8,), dtype=__import__('torch').float16)
   print(type(a)._wrapped is type(b)._wrapped)   # True：类级共享
   print(a._wrapped is None, ReluFwdOp._wrapped is None)  # False False：已注册
   ```
2. **需要观察的现象**：第一行打印 `True`（证明 `_wrapped` 挂在类上、跨实例共享）；第二行两个都 `False`（证明 import 后所有叶子都已注册）。
3. **预期结果**：`True` / `False False`。
4. 若环境无 GPU 或 import 失败，则改为**源码阅读型实践**：在 [__init__.py:216-223](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/elementwise/__init__.py#L216-L223) 数一数「同 dtype 二元」那组有几个算子（应为 13 个），并解释它们为何共用同一个 `_register_binary_custom_op` 调用。运行结果待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `_wrapped` 是**类属性**（`type(self)._wrapped`）而不是**实例属性**（`self._wrapped`）？

> **答案**：`custom_op` 是按「算子种类」注册的全局符号（命名空间 `top::elementwise_*`），一个类只需要注册一次。若每个实例都注册一个，会重复注册同名 custom_op 而报错，且把「类的注册」与「实例的状态」混在一起。把 `_wrapped` 放类上，实例只额外贡献一个 `instance_key`，职责清晰。

**练习 2**：`masked_fill` 的标量版与张量值版为什么要用不同的命名空间？

> **答案**：两者输入签名不同（标量版 `x, mask`；张量值版 `input, mask, value`），若共用 `top::elementwise_masked_fill`，第二次注册会因命名空间冲突在 import 时抛错。所以张量值版用 `..._tensor_value` 后缀错开（[_base.py:336-338](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/elementwise/_base.py#L336-L338)）。

---

### 4.2 int-key 注册表与 `_wrapped_inplace` 分发

#### 4.2.1 概念说明

custom_op 的 eager 体是**静态**的——它在 import 时就被定义，无法捕获某个具体实例。但每个算子的真正计算逻辑（kernel、`out_shape`、dtype）都在**实例**上。桥梁是一个**全局注册表** `_OP_REGISTRY`，它把「`instance_key` → 实例」存起来；eager 体拿到 `instance_key` 后去查表，再把活派给 `instance._eager_forward`。

这里有一个**关键细节**，也是本讲最容易踩坑的地方：elementwise 家族用的是它**自己模块级**的 int-key 注册表（[_base.py:34](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/elementwise/_base.py#L34)），key 是 `id(self)`（int）。这与 u10-l1 讲的规范范本——`compile_boundary._OP_REGISTRY` 的**字符串 key**（`str(id(op))`）——是两张不同的表。elementwise 这套 int-key 机制更早，u10-l1 已把它标注为「待迁移」状态。

in-place 算子（如 `relu(..., inplace=True)`）是另一条路：它原地改写输入 `x`，必须用 `mutates_args=("x",)` 单独注册一个 `_wrapped_inplace`，fake 体返回 `None`（因为函数没有返回值，副作用是改 `x`）。

#### 4.2.2 核心流程

**实例登记**（构造期）：每个伞形基类的 `__init__` 末尾都有两行，把实例存进 elementwise 自己的注册表（int key）：

```python
self._instance_key = id(self)
_OP_REGISTRY[self._instance_key] = self
```

注意它**覆盖**了 `dispatch_kernel` 里 `self._instance_key = register_instance(self)` 设的字符串 key（见 4.2.3）。所以 elementwise 实例最终持有的 `_instance_key` 是 **int**，编译路径用的也是这张 int-key 表。

**运行时分派**（调用期），以 binary 为例：

```text
op(a, b)
 └─ BinaryOp.forward(a, b)                      # 校验 dtype/numel
      └─ type(op)._wrapped(a, b, out_shape, op._instance_key)   # 类级 custom_op
           └─ instance = _OP_REGISTRY[instance_key]              # int 查表
           └─ return instance._eager_forward(a, b)               # 真正跑 kernel
                └─ self.kernel(a.view(-1), b.view(-1)).reshape(self.out_shape)
```

**in-place 分派**（`_UnaryActivationMixin.forward`）：

```text
op(x)  且 self.inplace=True
 └─ if type(op)._wrapped_inplace is not None:
       _wrapped_inplace(x, op._instance_key)     # mutates_args=("x",)
            └─ result = instance._eager_forward(x)
            └─ x.copy_(result.reshape(x.shape))  # 把结果写回 x
       return x                                  # 调用方看到 y is x
```

#### 4.2.3 源码精读

**两张注册表的并存**：规范的字符串 key 表在 [compile_boundary.py:17](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/compile_boundary.py#L17)，由 [op_base.py:192-197](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/op_base.py#L192-L197) 的 `dispatch_kernel` 统一登记：

```python
def dispatch_kernel(self, kernel_map=None):
    self._install_kernel_map(kernel_map)
    # Conforming __init__s all pass through here — the zero-boilerplate
    # registration point for the compile dispatch boundary.
    self._instance_key = register_instance(self)   # 字符串 key，str(id(self))
```

而 elementwise 的 int-key 表 [_base.py:34](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/elementwise/_base.py#L34)：

```python
_OP_REGISTRY: weakref.WeakValueDictionary = weakref.WeakValueDictionary()
```

每个伞形基类的 `__init__` 都先调 `self.dispatch_kernel(kernel_map)`（走规范字符串 key 登记），随后又用 int key 覆盖登记——见 `UnaryOp` [_base.py:604-605](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/elementwise/_base.py#L604-L605)、`BinaryOp` [_base.py:760-761](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/elementwise/_base.py#L760-L761)、`FusedGatedOp` [_base.py:870-871](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/elementwise/_base.py#L870-L871)。覆盖之后，`self._instance_key` 变回 `id(self)`（int），于是 elementwise 的 `_wrapped` 在 [_base.py:140](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/elementwise/_base.py#L140) 用 int 查的就是这张 elementwise 自己的表。

> 这就是 u10-l1 所说的「elementwise 仍用旧式 int key 表，待迁移」。代码侧注释 [_base.py:30-32](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/elementwise/_base.py#L30-L32) 写的是「key 用普通 int，好让 dynamo 追踪 forward 时不碰到不支持的 Python 副作用」；而 `compile_boundary` 改用字符串 key 的理由见 [compile_boundary.py:8-12](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/compile_boundary.py#L8-L12)：dynamo 把字符串 custom_op 参数当静态常量，而 int 在同一帧里出现第二个实例时会被泛化成不可哈希的 `SymInt`。两种说法各自成立：单实例图中 int 能过；多实例共享一帧时 int 会出问题，字符串才稳健。

**`_eager_forward`：custom_op eager 体委托的对象**。unary 版 [_base.py:649-656](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/elementwise/_base.py#L649-L656)：把输入拉平、跑 kernel、reshape 回原形、做 fp8 后置 cast。binary 版 [_base.py:784-793](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/elementwise/_base.py#L784-L793) 同理。这两段就是「不可追踪的 kernel 调用」，被刻意放在 custom_op 的 eager 体里、藏在 dynamo 视线之外。

**in-place 注册** [_base.py:151-171](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/elementwise/_base.py#L151-L171)：

```python
@torch.library.custom_op(
    f"top::elementwise_unary_{op_name}_inplace", mutates_args=("x",),
)
def _wrapped_inplace(x: torch.Tensor, instance_key: int) -> None:
    instance = _OP_REGISTRY[instance_key]
    result = instance._eager_forward(x)
    x.copy_(result.reshape(x.shape))
op_cls._wrapped_inplace = _wrapped_inplace
```

注意三点：① 返回类型是 `None`，fake 体不写（in-place 无输出张量，靠 `mutates_args` 表达副作用）；② 真正的 kernel 仍写新 buffer，这里再 `copy_` 回 `x`，使调用方看到 `y is x`；③ 没有 `@_wrapped_inplace.register_fake`，因为 `mutates_args` 已经告诉 dynamo「`x` 会被改写」，不需要再产出输出 meta。

**in-place 的 forward 分派**集中在 `_UnaryActivationMixin.forward` [_base.py:976-991](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/elementwise/_base.py#L976-L991)：`inplace=True` 时走 `_wrapped_inplace`（编译友好），并保留一条「未注册（如测试用子类）就回退到直接 `copy_`」的兜底（[_base.py:983-987](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/elementwise/_base.py#L983-L987)）。哪些叶子注册了 inplace 同伴，见 [__init__.py:251-255](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/elementwise/__init__.py#L251-L255)（relu/silu/hardswish/...等 9 个声明了 `inplace` 的激活）。

> 补充：参数化激活（LeakyReLU、ELU 等）不走 `UnaryOp.__init__`，而是在叶子 `__init__` 里直接构造 kernel 后，经 `_finalize_init` [_base.py:1059-1060](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/elementwise/_base.py#L1059-L1060) 统一完成同样的 int-key 登记。

#### 4.2.4 代码实践

**实践目标**：画出 `forward → _wrapped → _eager_forward` 与 inplace 的 `forward → _wrapped_inplace → copy_` 两条链，并验证 in-place 让 `y is x`。

1. 阅读以下三段，在纸上画出调用链（标出每一步发生在「类级 / 实例级 / custom_op eager 体」）：
   - `BinaryOp.forward` [_base.py:795-819](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/elementwise/_base.py#L795-L819)
   - `_register_binary_custom_op` 的 eager 体 [_base.py:183-191](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/elementwise/_base.py#L183-L191)
   - `_register_unary_inplace_custom_op` [_base.py:151-171](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/elementwise/_base.py#L151-L171)
2. （需 CUDA）跑一个 in-place 观察：
   ```python
   import torch
   from tileops.ops.elementwise import ReluFwdOp
   x = torch.randn(8, device="cuda", dtype=torch.float16)
   x_ptr = x.data_ptr()
   op = ReluFwdOp(N_total=8, dtype=torch.float16, inplace=True)
   y = op(x)
   print(y is x, y.data_ptr() == x_ptr)   # 期望 True True：原地改写
   ```
3. **需要观察的现象**：`y is x` 为 `True`（in-place 路径返回原张量），`data_ptr` 不变（数据写回原 buffer）。
4. **预期结果**：`True True`。运行结果待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：`ReluFwdOp(N_total=8, dtype=torch.float16)` 这个实例，最终被登记在几张表里？分别用什么 key？

> **答案**：两张。① `compile_boundary._OP_REGISTRY`，key 是 `str(id(op))`（由 `dispatch_kernel` → `register_instance` 登记，规范范本，但 elementwise 的编译路径不读它）；② elementwise 自己的 `_base._OP_REGISTRY`，key 是 `id(op)`（int，由 `UnaryOp.__init__` 覆盖登记，编译路径实际查的是它）。两张表的 value 是同一个对象。

**练习 2**：为什么 `_wrapped_inplace` 不需要 `register_fake`？

> **答案**：它是 in-place 算子，声明了 `mutates_args=("x",)`，dynamo 据此知道 `x` 会被原地改写、函数没有返回张量，因此不需要一个产出输出 meta 的 fake 函数。out-of-place 的 `_wrapped` 才需要 fake 来告诉 dynamo 输出的 shape/dtype。

---

### 4.3 register_fake：为 fullgraph 推导输出形状

#### 4.3.1 概念说明

`torch.compile(op, fullgraph=True)` 时，dynamo 用 FakeTensor 追踪 `forward`。当追踪碰到 custom_op `wrapped(...)`，dynamo **不会**执行 eager 体（那样会真的跑 kernel、还会触发不可追踪的副作用），而是调用你注册的 **fake 函数**，用它返回的 FakeTensor 作为这个算子的输出 meta。后续算子就接着这个 meta 往下推。

所以 fake 函数的唯一职责是：**给定输入的 shape/dtype，返回一个 shape/dtype 正确的空张量**。它算错 shape，dynamo 的形状推理就会错，下游要么图断裂、要么报「返回了非 Tensor」或 shape 不匹配。这就是为什么多输入算子（where / clamp / masked_fill）的 fake 必须用 `torch.broadcast_shapes` 算广播输出——它们支持不同 shape 的输入，输出 shape 取决于广播结果。

#### 4.3.2 核心流程

```text
torch.compile(op, fullgraph=True)(x, ...)
 └─ dynamo 用 FakeTensor 追踪 op.forward
      └─ 碰到 type(op)._wrapped(x, ..., instance_key)   # custom_op 调用
           └─ dynamo 改调 register_fake 函数（不进 eager 体）
                └─ 用 torch.broadcast_shapes(...) 算出 out_shape
                └─ return input.new_empty(out_shape, dtype=out_dtype)
      └─ 把返回的 FakeTensor 当作本算子输出，继续往下推
```

不同输入 shape 的广播：例如 `where(cond[4,1], x[1,8], y[1,])` 的输出应是 `[4,8]`。fake 必须算对，否则 `fullgraph=True` 失败。

#### 4.3.3 源码精读

**broadcast-aware 的 fake** 以 where 为例 [_base.py:249-257](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/elementwise/_base.py#L249-L257)：

```python
@_wrapped.register_fake
def _(cond, x, y, instance_key) -> torch.Tensor:
    out_shape = torch.broadcast_shapes(cond.shape, x.shape, y.shape)
    return x.new_empty(out_shape)
```

同理还有 masked_fill [_base.py:315-322](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/elementwise/_base.py#L315-L322)、张量值 masked_fill [_base.py:348-356](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/elementwise/_base.py#L348-L356)、clamp（含 `Optional` 上下界）[_base.py:386-399](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/elementwise/_base.py#L386-L399)、lerp_tensor [_base.py:284-292](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/elementwise/_base.py#L284-L292)。clamp 的 fake 尤其值得看：它要处理 `min`/`max` 可能是 `None` 的情况，只把非 `None` 的张量 shape 放进广播列表（[_base.py:393-398](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/elementwise/_base.py#L393-L398)）。`Optional[torch.Tensor]` 注解会被 `custom_op` 推断成 schema 里的 `Tensor?`（见工厂 docstring [_base.py:361-371](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/elementwise/_base.py#L361-L371)）。

**binary 的 `out_shape` 参数**：[_register_binary_custom_op](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/elementwise/_base.py#L174-L203) 的 eager 体**不用** `out_shape`（kernel 内部读 `instance.out_shape`），但它出现在签名里，是为了让 fake 体 [_base.py:193-201](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/elementwise/_base.py#L193-L201) 能直接拿构造期就算好的广播 shape 去 `new_empty`。`forward` 把它从缓存 `self._out_shape_list` 透传进来（[_base.py:818](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/elementwise/_base.py#L818)、缓存见 [_base.py:751](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/elementwise/_base.py#L751)）。把列表作为 custom_op 参数，dynamo 会把它当作烘焙好的静态常量，fake 就能稳定地产出正确 meta。

**回归守卫**：[test_elementwise_compile.py](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tests/ops/test_elementwise_compile.py) 里专门有 `*_broadcast` 用例（where [test_elementwise_compile.py:714-727](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tests/ops/test_elementwise_compile.py#L714-L727)、clamp [test_elementwise_compile.py:772-785](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tests/ops/test_elementwise_compile.py#L772-L785)、masked_fill_scalar [test_elementwise_compile.py:913-930](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tests/ops/test_elementwise_compile.py#L913-L930)）。这些用例的注释明确写着它们是「register_fake 改成 broadcast-aware 后」的回归——历史上 fake 曾只按输入原 shape 推导，导致广播输入在 fullgraph 下失败，这些测试就是防止退回老逻辑。还有断言输出 dtype/shape 的测试（bool 比较 [test_elementwise_compile.py:353-360](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tests/ops/test_elementwise_compile.py#L353-L360)、fused_gated shape [test_elementwise_compile.py:372-379](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tests/ops/test_elementwise_compile.py#L372-L379)），直接验证 fake 产出的 meta 正确。

#### 4.3.4 代码实践

**实践目标**：亲眼看到「fake 算对广播 shape，fullgraph 才不裂」。

1. 阅读 [_register_where_custom_op](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/elementwise/_base.py#L230-L259) 与对应广播测试 [test_where_compile_broadcast](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tests/ops/test_elementwise_compile.py#L713-L727)。
2. （需 CUDA）运行该广播用例的等价脚本：
   ```bash
   pytest tests/ops/test_elementwise_compile.py::test_where_compile_broadcast -q
   ```
3. **需要观察的现象**：测试通过，输出 shape 恰为 `(4, 8)`（`cond[4,1]` 与 `x[1,8]` 的广播结果）。
4. **思想实验**：若把 where 的 fake 改成 `return x.new_empty(x.shape)`（不广播），预测 `fullgraph=True` 会怎样？
   - **预期结果**：dynamo 推出的输出 meta 变成 `x` 的 shape（`(1,8)`），与真实广播输出 `(4,8)` 不符；下游 shape 推理出错，触发 graph break 或 shape 校验失败，`fullgraph=True` 不再成立。这正说明 broadcast-aware 的 fake 对 fullgraph 必要。
5. 运行结果待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：unary 的 fake 用 `torch.empty_like(x)`，binary 的 fake 却把 `out_shape` 当参数传进来。为什么 unary 不用传？

> **答案**：unary 输出 shape 恒等于输入 shape，`empty_like(x)` 就够了；binary 支持广播，输出 shape 是 `a`、`b` 广播后的结果，不等于任一输入，所以构造期用 `coalesce_broadcast_dims` 算好 `out_shape`，再作为 custom_op 参数透传给 fake（eager 体则不读它，直接用 `instance.out_shape`）。

**练习 2**：`test_masked_fill_scalar_compile_broadcast`（[test_elementwise_compile.py:913-930](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tests/ops/test_elementwise_compile.py#L913-L930)）的注释提到「register_fake 现在是 broadcast-aware」。如果它退回非广播版，会出什么具体错误？

> **答案**：广播输入（`input[4,8]`、`mask[1,8]`）下，非广播 fake 会按 `input` 或 `mask` 单边 shape 推 meta，与真实广播输出 shape 不一致；dynamo 在 fullgraph 模式下要么图断裂、要么在形状校验处报错，使原本应通过的全图编译失败。

---

## 5. 综合实践

把三个模块串起来：为 elementwise 的编译链路补一份「调用链 + 注册证据」的小报告。

1. **画调用链**（贯穿 4.1–4.3）：以 `AddFwdOp` 为例，画出从 `torch.compile(op, fullgraph=True)(a, b)` 出发的两条路径：
   - **追踪期**（FakeTensor）：`forward → _wrapped → register_fake`（用 `out_shape` 产 meta）；
   - **执行期**（真张量）：`forward → _wrapped(eager 体) → _OP_REGISTRY[id] → _eager_forward → kernel`。
   在图上标注：哪些是**类级**（`_wrapped`、`register_fake`），哪些是**实例级**（`_instance_key`、`_eager_forward`、`kernel`）。
2. **再加一条 in-place 分支**：对 `ReluFwdOp(..., inplace=True)` 画出 `forward → _wrapped_inplace → _eager_forward → x.copy_`，标出 `mutates_args=("x",)` 在哪起作用。
3. **解释设计动机**：用一句话回答——为什么 elementwise 选择「类级 custom_op + int-key 注册表」而不是「每个实例一个 custom_op」？（提示：注册次数、命名空间冲突、实例路由。）
4. **对比规范范本**：读 [compile_boundary.py](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/compile_boundary.py) 与 u10-l1，写一段话说明 elementwise 的 int-key 表为何是「待迁移」：`dispatch_kernel` 已经把它登记进了规范的字符串 key 表，但 elementwise 自己又覆盖回了 int key，编译路径实际只认 int 那张表——这是一种历史路径，未来收敛到 `compile_boundary` 后 `_wrapped` 的查表应改用 `get_instance(key)`。
5. **运行验证**（需 CUDA）：挑一个广播算子跑 `pytest tests/ops/test_elementwise_compile.py -k broadcast -q`，确认全部通过；这同时验证了注册、fake 广播、kernel 三者都正确。

> 无法运行时，把第 1–4 步做成纯源码阅读报告即可，标注「运行结果待本地验证」。

## 6. 本讲小结

- elementwise 用**工厂函数**（`_register_unary/binary/fused_gated/..._custom_op`）在**包加载时**给每个叶子算子类注册一个 `torch.library.custom_op`，挂到类属性 `_wrapped`（[_base.py:129-481](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/elementwise/_base.py#L129-L481)），批量注册在 [__init__.py:198-255](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/elementwise/__init__.py#L198-L255)。
- 运行时分派靠**注册表 + instance_key**：custom_op 是类级的、静态的；每个实例构造时把 `id(self)` 存进 elementwise 自己的 int-key `_OP_REGISTRY`，eager 体据此查到实例再调 `_eager_forward`（[_base.py:34](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/elementwise/_base.py#L34)、[_base.py:140](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/elementwise/_base.py#L140)）。
- in-place 走单独的 `_wrapped_inplace`，声明 `mutates_args=("x",)`，eager 体把 kernel 结果 `copy_` 回 `x`、返回 `None`，使调用方看到 `y is x`（[_base.py:151-171](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/elementwise/_base.py#L151-L171)、[_base.py:976-991](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/elementwise/_base.py#L976-L991)）。
- `register_fake` 是 fullgraph 的命脉：追踪期 dynamo 只调 fake、不进 eager 体；多输入算子必须在 fake 里用 `torch.broadcast_shapes` 算输出 shape，否则广播输入下全图编译失败（[_base.py:249-257](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/elementwise/_base.py#L249-L257)，回归测试见 [test_elementwise_compile.py](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tests/ops/test_elementwise_compile.py)）。
- elementwise 的 int-key 表与 u10-l1 的规范字符串 key 表（`compile_boundary`）并存：`dispatch_kernel` 已把实例登记进字符串 key 表，但 elementwise 又覆盖回 int key、只认自己的表，这是「待迁移」的历史路径。

## 7. 下一步学习建议

- **回到 u10-l1** 对比规范范本：读 [compile_boundary.py](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/compile_boundary.py) 的字符串 key 与 `WeakValueDictionary`，理解「字符串 key 为何对多实例同帧更稳」，思考 elementwise 迁移过去需要改哪几处（`_wrapped` 查表、`_instance_key` 取值）。
- **进入 u11**：本讲只讲了 elementwise 的注册工厂；[u11-l2 elementwise 三大伞形基类](u11-l2-elementwise-umbrella-bases.md) 会讲 `UnaryOp`/`BinaryOp`/`FusedGatedOp` 如何用 `kernel_cls` + `_op_name` 模板化数十个算子，以及 `coalesce_broadcast_dims` 如何把 N 维广播降成最少有效维（与本讲的 `out_shape` 同源）。
- **横向对照**：读 `tileops/ops/pool.py`、`tileops/ops/batch_norm.py` 看规范范本（字符串 key）是怎么写 `forward` 收敛成「一行单次分发」的，与本讲的 int-key 多分支 `forward` 形成对照。
- **验证你的理解**：试着在本地加一个最小新 unary 算子（只设 `_op_name` + `kernel_cls`），把它加进 [__init__.py](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/ops/elementwise/__init__.py) 的注册循环，确认它自动获得 `_wrapped` 且能过 `torch.compile(op, fullgraph=True)`。
