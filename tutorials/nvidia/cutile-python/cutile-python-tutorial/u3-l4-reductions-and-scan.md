# 归约与扫描：sum/max/cumsum/scan

## 1. 本讲目标

在前几讲里，我们已经能用 `ct.load` 把一块全局数组搬成 tile、用算术运算产生新 tile、再 `ct.store` 写回。这些操作都是**逐元素**的——输出 tile 的每个元素只依赖输入 tile 对应位置的元素。但很多 GPU 内核（LayerNorm、softmax、直方图、前缀和）都需要把一个轴上的多个元素**合并成一个**，或把它们**累加成一串**。这就是本讲要讲的两类集体运算：

- **归约（reduction）**：沿某个轴把多个元素压缩成更少的元素，例如 `ct.sum`、`ct.max`、`ct.min`、`ct.argmax`、`ct.argmin`。
- **前缀扫描（prefix scan）**：沿某个轴计算前缀和/前缀积，输出与输入同形，例如 `ct.cumsum`、`ct.cumprod`，以及可注入任意二元函数的 `ct.scan`。

学完本讲你应当能够：

1. 掌握 `sum / max / min / argmax / argmin` 的 `axis`、`keepdims` 参数，理解 `axis=None`、单轴、多轴（tuple）的区别。
2. 理解归约与扫描在底层都映射到**带 body 的 IR 操作**（`tile_reduce` / `tile_scan`），body 是一个「合并两个标量」的函数。
3. 理解 `cumsum / cumprod / scan` 的**前缀语义**（inclusive、`reverse`、`rounding_mode`、`flush_to_zero`），以及 `ct.scan` 对 body 的限制。
4. 写出一个真实的**行归约内核**（按行求均值）。

## 2. 前置知识

在进入本讲前，请确认你已经理解以下概念（它们来自前置讲义）：

- **load–compute–store 范式**（u3-l1）：内核先用 `ct.load(array, index, shape)` 把数组的一块搬成 tile，计算后再 `ct.store` 写回。`index` 是 **tile space（瓦片）索引**，`shape` 是编译期常量、每维为 2 的幂。
- **tile 不可变 + SSA**（u2-l3、u3-l2）：tile 是 kernel 内部不可变的值，所有运算都产生**新 tile**，底层是 SSA 风格的 Tile IR。这一点对归约尤其重要——归约的「累加器」沿循环流动时，本质是携带值（carried values），见 u3-l3。
- **shape 是编译期常量**（u2-l3）：tile 每一维大小在编译期就固定，所以「沿哪条轴归约」「归约后剩多少维」编译器全都能在编译期算出来。
- **DType 与算术性**（u2-l4）：只有**可算术（arithmetic）**类型才能做归约/扫描；`float8_e4m3fn`、`tfloat32` 这类 RestrictedFloat 不可算术，会被拒绝。
- **block 是执行单元**（u2-l1）：一个归约/扫描操作发生在**单个 block 内部**，由整个 block 集体完成，block 内部隐式同步。

一个关键直觉：在 cuTile 里，「归约」和「扫描」不是特殊的语法糖，而是两个**带嵌套 body 的 IR 操作**。你提供的「怎么合并两个元素」的逻辑，会被编译进 `tile_reduce` / `tile_scan` 的 body 块里。`ct.sum` 只是把 body 钉死成「加法」、`ct.cumsum` 钉死成「加法前缀」而已；而 `ct.reduce` / `ct.scan` 允许你自己写 body。理解了这一点，本讲的所有 API 都是一回事。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
|------|------|
| [src/cuda/tile/_stub.py](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/src/cuda/tile/_stub.py) | 用户 API 的**签名层**：`sum/prod/max/min/argmax/argmin/reduce/cumsum/cumprod/scan` 全部声明在这里（`@stub` 装饰，函数体为空）。 |
| [src/cuda/tile/_ir/ops.py](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/src/cuda/tile/_ir/ops.py) | 上述 API 的**实现层**：`@impl` 注册把 stub 连到真正的 IR 构建，定义了 `TileReduce`、`TileScan` 两个核心操作及 `reduce / raw_reduce / scan / raw_scan` 编排函数。 |
| [samples/LayerNorm.py](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/samples/LayerNorm.py) | 真实示例：LayerNorm 前向/反向内核，大量使用 `ct.sum(..., axis=...)` 做行归约，是本讲「行归约」实践的样板。 |
| [test/test_scan.py](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/test/test_scan.py) | scan 的测试集：覆盖 `cumsum/cumprod` 的 `reverse`、`rounding_mode`、`flush_to_zero`，以及 `ct.scan` 自定义 body 与各种限制。 |

## 4. 核心概念与源码讲解

本讲拆成 4 个最小模块：

- **4.1 数值归约 `sum / prod / max / min`**——最常用的归约，讲透 `axis` 与 `keepdims`。
- **4.2 索引归约 `argmax / argmin`**——归约的变体，返回的是「下标」而非「值」。
- **4.3 前缀扫描 `cumsum / cumprod`**——输出与输入同形的前缀运算。
- **4.4 自定义 `ct.reduce / ct.scan`**——把合并函数交给用户，并讲清 body 的限制。

### 4.1 数值归约：sum / prod / max / min

#### 4.1.1 概念说明

归约回答的问题是：**沿某条轴，把多个元素合并成一个**。比如一个 shape 为 `(2, 4)` 的 tile，沿 `axis=1` 做 `sum`，就把每一行的 4 个元素加起来，得到 shape `(2,)` 的结果。

cuTile 提供 4 个数值归约：

| API | 合并方式 | 单位元（identity） |
|-----|---------|------------------|
| `ct.sum` | 加法 `a + b` | `0` |
| `ct.prod` | 乘法 `a * b` | `1` |
| `ct.max` | 取大 `max(a, b)` | `-inf`（浮点）/ 类型最小值（整数） |
| `ct.min` | 取小 `min(a, b)` | `+inf`（浮点）/ 类型最大值（整数） |

单位元（identity）是归约的起点：归约从单位元开始，依次把每个元素「合并」进去。求和从 0 开始累加；求最大值从 `-inf` 开始（任何有限值都比它大）。理解单位元，就理解了空轴归约的结果，也理解了 `max/min` 为何对浮点用 `±inf`。

两个关键参数：

- **`axis`**：要沿哪条轴归约。三种取值：
  - `axis=None`（默认）：归约**所有**轴，结果是一个标量（0 维 tile）。
  - `axis=int`：归约**单条**轴，支持负数（`-1` 表示最后一条轴）。
  - `axis=(int, int, ...)`：归约**多条**轴（tuple）。
- **`keepdims`**：归约后是否保留被归约的轴（保留成大小为 1 的轴）。
  - `keepdims=False`（默认）：被归约的轴**消失**，维度数减少。
  - `keepdims=True`：被归约的轴**保留为 1**，维度数不变，便于后续广播。

举一个来自 stub 文档的例子，输入 `[[0,1,2,3],[4,5,6,7]]`（shape `(2,4)`）：

- `ct.sum(tx, None)` → `28`（全部求和，标量）。
- `ct.sum(tx, 1)` → `[6, 22]`（每行求和，shape `(2,)`）。
- `ct.sum(tx, 1, keepdims=True)` → `[[6],[22]]`（shape `(2,1)`，保留轴便于广播）。
- `ct.sum(tx, (1,2))`（3 维时）→ 同时归约两条轴。

#### 4.1.2 核心流程

归约在底层不是「一个特殊的循环」，而是**一个带 body 的 IR 操作 `tile_reduce`**。它的执行模型可以用下面这段伪代码理解：

```
# tile_reduce：沿 axis 把 shape[..., D, ...] 归约成 shape[..., ...]（去掉 axis）
result = identity                         # 从单位元开始
for each element e along axis:            # 由整个 block 集体并行完成
    result = body(result, e)              # body 是「合并两个标量」的函数
# 对 sum：body = lambda a,b: a+b，identity = 0
# 对 max：body = lambda a,b: max(a,b)，identity = -inf
```

关键点：

1. **body 接收两个 0 维 tile（标量）**，返回一个 0 维 tile。`ct.sum` 把 body 钉成加法，`ct.max` 钉成取大。
2. **多轴归约是「逐轴归约」**：归约 tuple `(1, 2)` 等价于先归约轴 2、再归约轴 1，每归约一次产生一个中间 tile。
3. **`keepdims` 只影响最终 reshape**：归约本身先去掉轴，最后若 `keepdims=True` 再把那些轴补回成 1。
4. 整个过程发生在**单个 block 内**，由编译器映射到硬件上的 warp 归约 / 张量核等集体运算，**不需要你写任何同步**。

#### 4.1.3 源码精读

**① 用户 API 签名**（`_stub.py`）。所有数值归约共享同一套 `axis / keepdims` 形参，`sum/prod` 额外有 `rounding_mode`，`max/min` 额外有 `flush_to_zero`：

[`sum` 的签名] [src/cuda/tile/_stub.py:2650-2651](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/src/cuda/tile/_stub.py#L2650-L2651) —— `def sum(x, /, axis=None, *, keepdims=False, rounding_mode=None, flush_to_zero=False)`。函数体为空，真正实现在 IR 层。

`_doc_reduce_op` 这个文档装饰器统一说明了 `axis` 的三种取值，并明确指出 **`argmin/argmax` 不支持 tuple 轴**：

[归约 axis/keepdims 的统一语义] [src/cuda/tile/_stub.py:2634-2638](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/src/cuda/tile/_stub.py#L2634-L2638) —— `axis=None` 归约所有元素；`keepdims=True` 保留维度数。

其余三个数值归约签名同构：[`max`](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/src/cuda/tile/_stub.py#L2702-L2702)、[`min`](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/src/cuda/tile/_stub.py#L2754-L2754)、[`prod`](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/src/cuda/tile/_stub.py#L2805-L2806)。

**② 实现层：把 stub 连到 IR**（`ops.py`）。`@impl` 装饰器把 `ct.sum / ct.prod` 注册到同一个实现 `reduce_impl_with_rd_and_ftz`，并用 `fixed_args=["add"]` / `["mul"]` 把合并方式钉死：

[sum/prod 的实现注册] [src/cuda/tile/_ir/ops.py:2380-2389](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/src/cuda/tile/_ir/ops.py#L2380-L2389) —— 解析 `axis`（可以是 int/None/tuple）、`keepdims`、`rounding_mode`、`flush_to_zero`，然后调用 `reduce_simple(fn, ...)`。

`max/min` 类似，只是没有 `rounding_mode`：[max/min 的实现注册] [src/cuda/tile/_ir/ops.py:2392-2399](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/src/cuda/tile/_ir/ops.py#L2392-L2399)。

**③ `reduce_simple`：选单位元 + 构造 body**。这是理解「归约 = body + identity」的核心：

[reduce_simple 的单位元与 body] [src/cuda/tile/_ir/ops.py:2325-2351](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/src/cuda/tile/_ir/ops.py#L2325-L2351)

```python
# 仅保留关键部分
if not datatype.is_arithmetic(x_type.dtype):
    raise TileTypeError(f"Non-arithmetic dtype {x_type.dtype} is unsupported for reduction")
if datatype.is_boolean(x_type.dtype):
    x = astype(x, datatype.default_int_type)        # bool 先转 int32
match fn:
    case "add": id_val = 0
    case "mul": id_val = 1
    case "min": id_val = _get_min_max(x_type.dtype)[1]   # +inf / 类型最大值
    case "max": id_val = _get_min_max(x_type.dtype)[0]   # -inf / 类型最小值

async def body(lhs, rhs):                              # 合并两个标量
    [lhs], [rhs] = lhs, rhs
    return (binary_arithmetic_tensorlike(fn, lhs, rhs, ...),)

[ret] = await reduce((x,), (id_val,), axis, keepdims, body)
```

注意三件事：非算术类型直接报错；布尔先转 `int32`；`min/max` 的单位元来自 `_get_min_max`，浮点是 `±inf`：

[_get_min_max 的单位元] [src/cuda/tile/_ir/ops.py:2357-2367](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/src/cuda/tile/_ir/ops.py#L2357-L2367) —— 浮点 `(-inf, +inf)`；有符号整数用 `-(1<<(bw-1))` 与 `(1<<(bw-1))-1`。

**④ `reduce`：多轴归约的编排**。这是「tuple 轴 = 逐轴归约」的真相：

[reduce 编排函数] [src/cuda/tile/_ir/ops.py:2190-2226](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/src/cuda/tile/_ir/ops.py#L2190-L2226)

```python
# 仅保留关键部分
if axis is None:
    axis = tuple(range(len(common_input_shape)))   # None → 归约所有轴
else:
    axis = sorted(normalize_axis(a, ...) for a in axis)  # 支持负数、排序
    # 重复轴会报错
xs = tuple(broadcast_to(x, common_input_shape) for x in xs)
for i, a in enumerate(axis):
    xs = await raw_reduce(xs, identities, a - i, body)   # 每次消掉一条轴
result_shape = _get_reduction_shape(common_input_shape, axis, keepdims)
return tuple(reshape(x, result_shape) for x in xs)        # keepdims 在这里生效
```

注意 `a - i`：每归约掉一条轴，后续轴的索引要前移一位。`keepdims` 只在最后一步 reshape 时把被消掉的轴补回成 1。

**⑤ `TileReduce`：带 body 的 IR 操作**。归约最终落到这个操作上，它把 body 编码进一个嵌套块：

[TileReduce 操作定义] [src/cuda/tile/_ir/ops.py:2066-2070](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/src/cuda/tile/_ir/ops.py#L2066-L2070) —— 持有 `identities`（单位元）、`axis`、输入 `xs`、以及一个嵌套 `body` 块。

body 块的参数是 `2N` 个 0 维 tile：前 N 个是「左操作数（累加器）」，后 N 个是「右操作数（当前元素）」，这正是 `_get_reduce_scan_body_block` 构造的结构（见 [src/cuda/tile/_ir/ops.py:2140-2171](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/src/cuda/tile/_ir/ops.py#L2140-L2171)）。

#### 4.1.4 代码实践

**实践目标**：用 `ct.sum` 的 `axis` 与 `keepdims` 理解归约后的形状变化，并对照 numpy 行为。

**操作步骤**：

1. 阅读上面的 stub 文档例子，确认你理解 `(2,4)` tile 在不同 `axis` 下的结果。
2. 写一个最小内核，对一个 `(2,4)` tile 分别做 `axis=None`、`axis=1`、`axis=1, keepdims=True` 三种归约，把结果 `ct.store` 到不同数组。
3. 用 numpy/torch 在 host 端算同样的结果做对照。

```python
# 示例代码（仅供参考，未在本机运行）
import torch, cuda.tile as ct

@ct.kernel
def reduce_demo(X, Y_all, Y_row, Y_row_keep):
    t = ct.load(X, index=(0,), shape=(2, 4))            # (2,4) tile，grid 只需 1 个 block
    ct.store(Y_all,      index=(0,), tile=ct.sum(t, None))             # 标量 → 存到 (1,)
    ct.store(Y_row,      index=(0,), tile=ct.sum(t, 1))                # (2,)
    ct.store(Y_row_keep, index=(0,), tile=ct.sum(t, 1, keepdims=True)) # (2,1)

x = torch.arange(8, dtype=torch.float32, device="cuda").reshape(2, 4)
# 预先分配好 Y_all:(1,), Y_row:(2,), Y_row_keep:(2,1)
# ct.launch(torch.cuda.current_stream(), (1,), reduce_demo, (x, Y_all, Y_row, Y_row_keep))
```

**需要观察的现象**：三种归约输出的 shape 分别是 `(1,)`、`(2,)`、`(2,1)`。

**预期结果**：`Y_all=[28]`、`Y_row=[6,22]`、`Y_row_keep=[[6],[22]]`，与 `torch.tensor([[0,1,2,3],[4,5,6,7]]).sum()`、`.sum(1)`、`.sum(1, keepdim=True)` 一致。

**待本地验证**：本实践未实际运行，请在本地分配好输出张量并启动内核后核对数值。

#### 4.1.5 小练习与答案

**练习 1**：对一个 shape 为 `(4, 8, 16)` 的 tile，`ct.sum(t, (1, 2))` 后结果 shape 是什么？`keepdims=True` 时呢？

**答案**：归约轴 1 和轴 2 后剩轴 0 和轴 2（原轴 2 在归约轴 1 后前移为轴 1）。`keepdims=False` → `(4, 16)`；`keepdims=True` → `(4, 1, 1, 16)`。注意原始 3 维在 `keepdims=True` 时维度数不变，被归约的两轴各变成 1。

**练习 2**：为什么 `ct.max` 在 `float32` 上的单位元是 `-inf`，而在 `int32` 上是 `-2147483648`？

**答案**：归约从单位元开始「取大」，单位元必须比任何真实元素都小，这样第一个真实元素才能胜出。`float32` 的最小值是 `-inf`；`int32` 的最小值是 `-(1<<31) = -2147483648`。这正是 `_get_min_max` 返回的值。

**练习 3**：对一个 `float8_e4m3fn` 类型的 tile 调 `ct.sum` 会发生什么？

**答案**：报 `TileTypeError`，提示 `Non-arithmetic dtype float8_e4m3fn is unsupported for reduction`。RestrictedFloat 不可算术（见 u2-l4），需先用 `astype` 转成 `float32` 再归约。

---

### 4.2 索引归约：argmax / argmin

#### 4.2.1 概念说明

`argmax / argmin` 是归约的一个变体：它们归约的不是「值」，而是**「值最大/最小的那个元素的下标」**。比如 `[[0,1,2,3],[4,5,6,7]]` 沿 `axis=1` 做 `argmax`，得到 `[3, 3]`——每一行最大值都在第 3 个位置。

它与数值归约的关键区别：

1. **返回的是 `int32` 下标**，不是原 dtype 的值。
2. **平局取最小下标**：如果多个元素并列最大/最小，返回其中下标最小的那个。
3. **不支持 tuple 轴**：`axis` 只能是 `int` 或 `None`，不能是 `(1, 2)`。
4. 没有 `rounding_mode / flush_to_zero`（它比较的是值，不做算术运算）。

#### 4.2.2 核心流程

`argmax/argmin` 的巧妙之处在于：它**复用了数值归约的机制**，只是把「值」和「下标」捆绑在一起归约。可以这样理解：

```
# argmax 沿 axis：
best_val = -inf ; best_idx = 0
for idx, e in enumerate(elements_along_axis):
    if e > best_val  or  (e == best_val and idx < best_idx):   # 严格更优，或平局且下标更小
        best_val, best_idx = e, idx
return best_idx        # 只返回下标
```

实现上，编译器会生成一个与输入同形的「下标 tile」（用 `ct.arange` 填充 0,1,2,...），然后**同时**对「值 tile」和「下标 tile」做归约，body 里既比较值、又在平局时比较下标。这正是 4.4 节「元组并行归约」的内部用法。

#### 4.2.3 源码精读

**① 签名与限制**：[`argmax`](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/src/cuda/tile/_stub.py#L2842-L2842) / [`argmin`](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/src/cuda/tile/_stub.py#L2896-L2896) —— 文档明确写有 "Reduce over tuple of axes is not supported for argmax/argmin"。

**② `argmax_argmin` 实现**——这是「值+下标捆绑归约」的真相：

[argmax/argmin 的捆绑归约] [src/cuda/tile/_ir/ops.py:2402-2447](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/src/cuda/tile/_ir/ops.py#L2402-L2447)

```python
# 仅保留关键部分
if axis is None:                               # None → 展平成一维再归约
    if keepdims: final_shape = (1,) * x.get_type().ndim
    x = reshape(x, (-1,)); axis = 0
else:
    axis = normalize_axis(axis, x.get_type().ndim)

indices = arange(x_type.shape[axis], datatype.default_int_type)   # 0,1,2,... 的下标 tile
indices = reshape(indices, tuple(-1 if i == axis else 1 for i in range(x_type.ndim)))  # 广播形状

# body：同时携带 (值, 下标)，比较值，平局比下标
async def body(lhs, rhs):
    lhs_val, lhs_idx = lhs ; rhs_val, rhs_idx = rhs
    val_strict          = compare_tensorlike_raw(cmp, lhs_val, rhs_val)     # 严格更优
    val_equal           = compare_tensorlike_raw("eq", lhs_val, rhs_val)
    index_lt            = compare_tensorlike_raw("lt", lhs_idx, rhs_idx)
    cond = (val_strict) | (val_equal & index_lt)                            # 平局取小下标
    return where_raw(cond, lhs_val, rhs_val), where_raw(cond, lhs_idx, rhs_idx)

[_, ret] = await reduce((x, indices), (id_val, 0), axis, keepdims, body)   # 只取下标
```

注意 `[_, ret]`：归约同时产出「最优值」和「最优下标」，`argmax` 只保留下标（`ret`）。`indices` 用 `arange` 生成、再 reshape 成可广播的形状，随值一起进入归约 body。

#### 4.2.4 代码实践

**实践目标**：验证 `argmax` 的「平局取小下标」行为。

**操作步骤**：

1. 构造一个含并列最大值的 tile，例如 `[0,0,0,0,1,1,1,1]`，沿 `axis=0` 求 `argmax`。
2. 根据 stub 文档，结果应为 `4`（第一个 `1` 的位置）。

```python
# 示例代码（未运行）
@ct.kernel
def argmax_demo(X, Y):
    t = ct.load(X, index=(0,), shape=(8,))
    ct.store(Y, index=(0,), tile=ct.argmax(t, 0))   # 期望下标 4
```

**预期结果**：`Y=[4]`。这正是 [stub 文档里 argmax 平局的例子] [src/cuda/tile/_stub.py:2879-2887](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/src/cuda/tile/_stub.py#L2879-L2887) 的断言。**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：`ct.argmax(t, None)` 与 `ct.argmax(t, 0)`（`t` 是一维 tile）结果一样吗？

**答案**：值一样（都返回全局最大值的下标），但 `axis=None` 时若 `keepdims=True` 会把结果 reshape 成 `(1,)*ndim`（见 `argmax_argmin` 的 `final_shape` 分支），而 `axis=0` 的 `keepdims` 行为遵循通用规则。

**练习 2**：为什么 `argmax` 不能用 tuple 轴？

**答案**：因为它内部把「值」和「下标」捆绑成一个二元组归约，下标只在**单条轴**上有意义（沿该轴编号 0,1,2,...）。同时归约多条轴时「下标」语义不明确，所以设计上只允许 `int` 或 `None`，`require_optional_constant_int` 会拒绝 tuple。

---

### 4.3 前缀扫描：cumsum / cumprod

#### 4.3.1 概念说明

前缀扫描（prefix scan）与归约的区别在于：**归约把一串元素压成一个，扫描则把一串元素变成另一串同长的元素**，每个位置存放「从头到当前位置的累积结果」。

- `ct.cumsum`：前缀和（prefix sum）。`[0,1,2,3]` → `[0, 1, 1+2=3, 1+2+3=6]`，即 `[0,1,3,6]`。这是 **inclusive（包含当前元素）** 扫描。
- `ct.cumprod`：前缀积（prefix product）。`[2,2,2,2]` → `[2,4,8,16]`。

关键参数：

- **`axis`**：默认是 `0`（注意：与归约的默认 `None` **不同**）。扫描的 `axis` 必须是 `int`（支持负数），**不能是 `None`、不能是 tuple**。
- **`reverse`**：是否反向扫描。`cumsum(t, 1, reverse=True)` 从右往左累加，等价于「后缀和」。`[0,1,2,3]` 反向前缀和 → `[6,6,5,3]`。
- **`rounding_mode` / `flush_to_zero`**：与 `cumsum/cumprod` 的浮点累加相关（`cumsum` 是加法、`cumprod` 是乘法），语义同 u2-l4 的 `RoundingMode`；`flush_to_zero` 仅 `float32` 可用。

一个重要性质：**扫描保持形状不变**。输入 `(T, N)`，沿 `axis=1` 扫描，输出仍是 `(T, N)`——它没有「压扁」任何轴，只是把每个位置替换成了累积值。

#### 4.3.2 核心流程

扫描在底层是**带 body 的 IR 操作 `tile_scan`**，与 `tile_reduce` 几乎同构，区别在于它沿轴「逐步累积」而非「压成一个」：

```
# tile_scan：沿 axis 做 inclusive 前缀扫描，输出与输入同形
acc = identity                                  # 从单位元开始
for i in range(len(axis)):                      # 由整个 block 集体并行完成
    acc = body(acc, x[i])                       # body 合并「累加器」与「当前元素」
    out[i] = acc
# reverse=True 时从右往左走
```

`cumsum` 的 body 是加法、identity 是 0；`cumprod` 的 body 是乘法、identity 是 1。注意扫描的 body 含义和归约**完全一样**（都是「合并两个标量」），差别只在 IR 操作把结果沿轴展开成一串，而不是压成一个。

数学上，inclusive 前缀和可写作：

\[
\mathrm{out}[k] = \sum_{j=0}^{k} x[j], \quad k=0,1,\dots,n-1
\]

反向前缀和（`reverse=True`）为：

\[
\mathrm{out}[k] = \sum_{j=k}^{n-1} x[j]
\]

#### 4.3.3 源码精读

**① 用户 API**：[`cumsum`](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/src/cuda/tile/_stub.py#L3001-L3002) / [`cumprod`](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/src/cuda/tile/_stub.py#L3039-L3040) —— `axis` 默认 0，`reverse` 默认 `False`。文档示例清楚展示了 inclusive 与 reverse 两种输出。

**② 实现注册**：`cumsum/cumprod` 共享 `scan_impl_with_rd_and_ftz`，`fixed_args` 分别钉死成 `"add"` / `"mul"`：

[cumsum/cumprod 的实现注册] [src/cuda/tile/_ir/ops.py:2621-2630](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/src/cuda/tile/_ir/ops.py#L2621-L2630) —— 解析 `axis`（用 `require_constant_int`，**必须是整数**，不接受 None/tuple）、`reverse`、`rounding_mode`、`flush_to_zero`，再调 `scan_simple`。

**③ `scan_simple`：选单位元 + 构造 body**，结构与 `reduce_simple` 高度对称：

[scan_simple 的单位元与 body] [src/cuda/tile/_ir/ops.py:2534-2566](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/src/cuda/tile/_ir/ops.py#L2534-L2566)

```python
# 仅保留关键部分
if not datatype.is_arithmetic(x_type.dtype):
    raise TileTypeError(f"Non-arithmetic dtype {x_type.dtype} is unsupported for prefix scans")
if datatype.is_boolean(x_type.dtype):
    x = astype(x, datatype.default_int_type)        # bool 先转 int32
match fn:
    case "add": id_val = 0
    case "mul": id_val = 1
axis = normalize_axis(axis, len(x_shape))           # 支持负数
[ret] = await raw_scan((x,), (id_val,), axis, reverse, body)
```

**④ `raw_scan` 与 `TileScan`**：扫描保持形状不变，并把 `reverse` 作为操作属性：

[raw_scan：保持形状、携带 reverse] [src/cuda/tile/_ir/ops.py:2523-2531](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/src/cuda/tile/_ir/ops.py#L2523-L2531) —— `result_types` 用 `input_shape`（同形），`TileScan` 属性含 `axis`、`reverse`、`identities`、`xs`、嵌套 `body`。

[TileScan 操作定义] [src/cuda/tile/_ir/ops.py:2458-2464](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/src/cuda/tile/_ir/ops.py#L2458-L2464) —— 注意它比 `TileReduce` 多一个 `reverse: bool` 属性。

**⑤ 测试佐证语义**。reverse 的参考实现用 `torch.cumsum(x.flip(1),1).flip(1)`：

[reverse 前缀和的测试参考] [test/test_scan.py:35-35](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/test/test_scan.py#L35) —— 翻转两次等价于「后缀和」。非算术类型报错见 [test/test_scan.py:67-75](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/test/test_scan.py#L67-L75)。

#### 4.3.4 代码实践

**实践目标**：用 `ct.cumsum` 实现前缀和，并验证 `reverse` 的「后缀和」语义。

**操作步骤**：阅读并运行 `test_cumsumf` 的内核 `cumsum_axis1`，它沿 `axis=1` 做 cumsum：

[测试中的 cumsum 内核] [test/test_scan.py:18-24](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/test/test_scan.py#L18-L24)

```python
@ct.kernel
def cumsum_axis1(input, output, reverse: ct.Constant[bool], T, N):
    px = ct.bid(0)
    tile = ct.load(input, index=(px, 0), shape=(T, N))
    out = ct.cumsum(tile, axis=1, reverse=reverse)
    ct.store(output, index=(px, 0), tile=out)
```

1. 用一个 `(32,32)` 的随机 `float32` 张量启动该内核（`grid=(1,1,1)`，因为整个矩阵用一个 `(32,32)` tile 覆盖）。
2. 分别用 `reverse=False` 和 `reverse=True` 跑两次。
3. 与 `torch.cumsum(x, 1)` 和 `torch.cumsum(x.flip(1),1).flip(1)` 对照。

**需要观察的现象**：`reverse=False` 时每行是从左到右的累加；`reverse=True` 时每行是从右到左的累加（后缀和）。

**预期结果**：与 torch 参考在 `atol=1e-5, rtol=1e-6`（float32）内一致。**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：`ct.cumsum(t, axis=None)` 会发生什么？

**答案**：报 `TileTypeError`，提示 "Expected an integer constant, but given value has type None"。扫描的 `axis` 必须是整数，因为扫描沿「一条具体的轴」逐步推进，`None`（全部轴）没有明确语义。见 `scan_impl_with_rd_and_ftz` 里的 `require_constant_int`，以及测试 [test/test_scan.py:291-303](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/test/test_scan.py#L291-L303)。

**练习 2**：对一个 `bfloat16` tile 调 `ct.cumsum`，能用 `flush_to_zero=True` 吗？

**答案**：不能。`flush_to_zero` 只支持 `float32`，其他浮点类型会报 "Flush to zero can only be used for float32 type"。见测试 [test/test_scan.py:184-193](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/test/test_scan.py#L184-L193)。

---

### 4.4 自定义归约与扫描：ct.reduce / ct.scan

#### 4.4.1 概念说明

`ct.sum / ct.max / ct.cumsum` 这些内置算子把合并方式（body）写死了。如果你想用**任意的二元函数**做归约或扫描——比如「累加异或」、「并行 cummax」、「同时算 cumsum 和 cumprod」——就该用更通用的 `ct.reduce` 和 `ct.scan`：

- `ct.reduce(x, axis, func, identity, *, keepdims=False)`：沿 `axis` 用 `func` 归约。
- `ct.scan(x, axis, func, identity, *, reverse=False)`：沿 `axis` 用 `func` 做前缀扫描。

其中 `func` 是一个**接收两个 0 维 tile、返回一个 0 维 tile** 的 Python 函数（lambda、`def`、`operator.add` 都行），`identity` 是 `func` 的单位元常量。例如 `ct.scan(t, axis=1, func=lambda a,b: a+b, identity=0)` 就等价于 `ct.cumsum(t, axis=1)`。

`ct.reduce` 与 `ct.scan` 还支持**元组模式**：传入一个 tile 元组 `(x1, x2)`，`func` 接收 `2N` 个参数（前 N 个是一组累加器、后 N 个是另一组），返回 N 个结果——这样可以**一次扫描同时维护多个累积量**。测试里有一个精彩例子：用一次 `ct.scan` 同时算出 cumsum、cumprod、cummax 和累加异或。

#### 4.4.2 核心流程

自定义归约/扫描的执行模型与内置版完全一致——它们都是 `tile_reduce` / `tile_scan`，只是 body 由用户提供：

```
# ct.reduce(t, axis, func, identity)：func 既比较/合并，identity 是 func 的单位元
acc = identity
for e along axis:
    acc = func(acc, e)        # 用户提供的 func

# ct.scan 同理，但沿轴展开成一串（inclusive）
```

但用户 body **不是什么都能写**。因为 body 会被编译进 `tile_reduce` / `tile_scan` 的嵌套块，而这些操作的语义要求 body 是**纯函数式的标量合并**，所以下列写法会被拒绝：

| 限制 | 报错信息 |
|------|---------|
| body 内有 `if/else` 分支 | "Branching inside scan body is not supported" |
| body 内有 `for/while` 循环 | "Loops inside scan body are not supported" |
| body 内有 `ct.load / ct.store / ct.printf` 等访存 | "Operations with memory effects are not supported inside scan body" |
| body 内嵌套 `ct.scan / ct.reduce` | "Nested scan/reduction is not supported" |

这些限制对 `ct.reduce` 同样适用（两者共用同一个 body 构造器 `_get_reduce_scan_body_block`，它一开始就检查嵌套）。

#### 4.4.3 源码精读

**① 用户 API**：[`ct.reduce`](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/src/cuda/tile/_stub.py#L2949-L2970) / [`ct.scan`](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/src/cuda/tile/_stub.py#L3060-L3082) —— 文档说明 `func` 在单 tile 时接收 2 个参数、元组时接收 `2N` 个参数，`identity` 必须是常量标量（或元组）。

**② 实现注册**：`ct.reduce` 走 `reduce_impl`，`ct.scan` 走 `scan_impl`，两者结构对称——都先判断是否元组模式、解析 `axis`/`func`/`identity`、构造 body，最后调 `raw_reduce` / `raw_scan`：

[ct.scan 的实现] [src/cuda/tile/_ir/ops.py:2569-2618](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/src/cuda/tile/_ir/ops.py#L2569-L2618) —— 注意它把多个输入 tile 广播到公共形状（`broadcast_shapes2`），`identity` 在元组模式下必须是等长标量元组。

[ct.reduce 的实现] [src/cuda/tile/_ir/ops.py:2274-2309](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/src/cuda/tile/_ir/ops.py#L2274-L2309) —— 元组模式下 `identity` 数量必须与输入 tile 数量一致，否则报错。

**③ body 构造与限制**：`_make_reduce_scan_body` 把用户的 `func` 包成一个 `body(lhs, rhs)`，并校验返回值必须是 0 维 tile 且 dtype 匹配；`_get_reduce_scan_body_block` 在最开头检查嵌套：

[禁止嵌套归约/扫描] [src/cuda/tile/_ir/ops.py:2137-2138](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/src/cuda/tile/_ir/ops.py#L2137-L2138) —— 若已在 `ReduceScanRestriction` 下，直接抛 "Nested scan/reduction is not supported"。

**④ 测试中的真实用法**。自定义 cumsum（三种 `func` 写法等价）：

[用 ct.scan 实现自定义 cumsum] [test/test_scan.py:210-232](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/test/test_scan.py#L210-L232) —— `lambda a,b: a+b`、`def f(a,b): return a+b`、`operator.add` 三者等价。

元组并行扫描（同时算 cumsum 和 cumprod，输入广播到公共形状）：

[两元素元组并行扫描] [test/test_scan.py:399-428](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/test/test_scan.py#L399-L428) —— `combine(prev_a, prev_b, curr_a, curr_b)` 返回 `(prev_a+curr_a, prev_b+curr_b)`。

body 限制的负面用例集合：分支 [test/test_scan.py:306-323](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/test/test_scan.py#L306-L323)、循环 [test/test_scan.py:326-342](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/test/test_scan.py#L326-L342)、访存 [test/test_scan.py:345-377](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/test/test_scan.py#L345-L377)、嵌套 [test/test_scan.py:380-396](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/test/test_scan.py#L380-L396)。

#### 4.4.4 代码实践

**实践目标**：用 `ct.scan` 的元组模式，在**一次扫描里同时**算出一行数据的 cumsum 和 cummax。

**操作步骤**：

```python
# 示例代码（未运行）
import cuda.tile as ct

@ct.kernel
def sum_and_max(X, OutSum, OutMax):
    t = ct.load(X, index=(0, 0), shape=(16, 16))
    def combine(prev_sum, prev_max, cur_sum, cur_max):
        return (prev_sum + cur_sum, ct.maximum(prev_max, cur_max))
    s, m = ct.scan(t, axis=1, func=combine,
                   identity=(0, float("-inf")))   # 加法单位元 0；取大单位元 -inf
    ct.store(OutSum, index=(0, 0), tile=s)
    ct.store(OutMax, index=(0, 0), tile=m)
```

1. 体会 `identity=(0, -inf)`：sum 用 0、max 用 `-inf`，与 4.1 节的单位元规则一致。
2. 体会 `combine` 接收 4 个参数（2 个累加器 + 2 个当前值）、返回 2 个值。
3. 与 `torch.cumsum(x,1)` 和 `torch.cummax(x,1).values` 对照。

**需要观察的现象**：一次内核启动同时得到前缀和与前缀最大值，无需两次扫描。

**预期结果**：`OutSum` 等于行向前缀和，`OutMax` 等于行向前缀最大值。**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：把 `ct.scan(t, axis=1, func=operator.add, identity=0)` 换成 `def f(a,b): if a>0: return a+b; else: return a*b`，会发生什么？

**答案**：编译失败，报 "Branching inside scan body is not supported"。scan/reduce 的 body 必须是无分支的纯标量合并函数（见测试 [test/test_scan.py:306-323](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/test/test_scan.py#L306-L323)）。需要条件逻辑时，应改用 `ct.where`（它本身是逐元素的，不含控制流分支）。

**练习 2**：`ct.reduce((x, w), axis=-1, func=combine, identity=(0,0))` 中，`x` 和 `w` 形状不同会怎样？

**答案**：只要两者可广播到同一形状即可（见 `scan_impl`/`reduce_impl` 里的 `broadcast_shapes2`）。例如 `x` 是 `(16,)`、`w` 是 `(64,16)`，会广播成 `(64,16)` 后一起归约。这正是测试 [test/test_scan.py:399-428](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/test/test_scan.py#L399-L428) 的场景。若不可广播，则报 "Input shapes ... are not broadcastable to a common shape"。

---

## 5. 综合实践：行归约内核（按行求均值）

把本讲的知识串起来，完成本讲的核心任务：**对 `(M, N)` 矩阵按行求均值，写回 `(M,)` 结果数组**。这个任务综合了「load–compute–store 范式 + 循环累加器（u3-l3 的 carried values）+ `ct.sum(..., axis=...)` 归约」，是 LayerNorm 前向里求 `mean` 的简化版。

### 设计思路

当 `N` 很大、单个 tile 装不下整行时，不能一次 `load` 一整行。正确做法参考 [LayerNorm 前向求 mean] [samples/LayerNorm.py:36-42](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/samples/LayerNorm.py#L36-L42)：

1. 每个 block 负责一行（`bid_m = ct.bid(0)`）。
2. 用 `ct.full((1, TILE_N), 0, dtype=ct.float32)` 建一个**累加器 tile**，沿列方向循环 `ct.num_tiles(X, axis=1, shape=(1,TILE_N))` 次，每次 `load` 一块 `(1, TILE_N)` 的行片段累加进去。
3. 循环结束后，累加器是「整行求和但还按 tile 切成 `(1, TILE_N)`」的形状；用 `ct.sum(acc, axis=1)` 把列方向归约成 `(1,)`，再除以 `N` 得到均值。
4. `ct.store` 写回 `(M,)` 数组的第 `bid_m` 个位置。

> 注：`mean = ct.sum(mean, axis=1) / N`（[samples/LayerNorm.py:41](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/samples/LayerNorm.py#L41-L41)）正是这套模式的范本。

### 参考实现

```python
# 示例代码（未在本机运行，仅供学习）
import torch
import cuda.tile as ct

@ct.kernel
def row_mean(X, Out, TILE_N: ct.Constant[int]):
    bid_m = ct.bid(0)                                   # 当前 block 负责第 bid_m 行
    N = X.shape[1]                                      # 运行时取列数

    acc = ct.full((1, TILE_N), 0, dtype=ct.float32)     # 累加器：(1, TILE_N)
    num_tiles = ct.num_tiles(X, axis=1, shape=(1, TILE_N))
    for j in range(num_tiles):                          # 沿列方向循环吞下整行
        tx = ct.load(X, index=(bid_m, j), shape=(1, TILE_N),
                     padding_mode=ct.PaddingMode.ZERO)  # 行末不足一个 tile 补 0
        acc = acc + tx                                  # tile 不可变 → 产生新累加器

    row_sum = ct.sum(acc, axis=1)                       # (1, TILE_N) → (1,)，沿列归约
    mean = row_sum / N                                  # 标量均值
    ct.store(Out, index=(bid_m,), tile=mean)            # 写回 (M,) 的第 bid_m 个

# --- host 端启动与验证 ---
M, N, TILE_N = 512, 2048, 1024
x = torch.randn(M, N, dtype=torch.float32, device="cuda")
out = torch.empty(M, dtype=torch.float32, device="cuda")
ct.launch(torch.cuda.current_stream(), (M,), row_mean, (x, out, TILE_N))

ref = x.mean(dim=1)
torch.testing.assert_close(out, ref, atol=1e-4, rtol=1e-4)
print("row_mean 内核与 torch.mean(dim=1) 一致")
```

### 需要观察的现象

- grid 是 `(M,)`——每个 block 处理一行，行与行之间完全独立，天然并行。
- 内层 `for j in range(num_tiles)` 把一行切成多个 `(1, TILE_N)` 片段累加；`acc` 作为携带值在循环中流动（呼应 u3-l3）。
- 最后的 `ct.sum(acc, axis=1)` 是本讲主角：把列方向压成 `(1,)`。

### 预期结果

`out` 与 `x.mean(dim=1)` 数值一致（容差内）。`padding_mode=ZERO` 保证了行末不足 `TILE_N` 的部分按 0 参与求和、不影响均值（因为除以的是真实 `N`，而补零不改变总和）。**待本地验证**：本实践未实际运行，请在本地确认 `N` 能否被 `TILE_N` 整除两种情况下结果都正确。

### 进阶变式（选做）

- 把 `row_sum = ct.sum(acc, axis=1)` 同时改成求**行均值**和**行方差**（参考 [LayerNorm 求 var] [samples/LayerNorm.py:44-52](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/samples/LayerNorm.py#L44-L52)），输出 `(M, 2)`。
- 把均值替换成 `ct.cumsum(acc, axis=1)`，观察输出形状变为 `(1, TILE_N)`——体会「归约压扁、扫描保形」的区别。

## 6. 本讲小结

- 归约（`sum/prod/max/min`）沿 `axis` 把多个元素合并成一个；`axis` 支持 `None`（全归约）、单 `int`、`tuple`；`keepdims` 控制是否保留被归约的轴为 1。归约从**单位元**开始（sum→0、prod→1、max→`-inf`、min→`+inf`）。
- `argmax/argmin` 是「值+下标」捆绑归约，返回 `int32` 下标，平局取最小下标；不支持 tuple 轴。
- 前缀扫描（`cumsum/cumprod`）输出与输入**同形**，是 inclusive 前缀运算；`axis` 必须是整数（默认 0，**非** None），`reverse=True` 给出后缀版本；非算术类型会被拒绝。
- 归约与扫描在底层都是**带 body 的 IR 操作** `tile_reduce` / `tile_scan`，body 是「合并两个标量」的纯函数；`ct.sum` 等只是把 body 钉死，`ct.reduce/ct.scan` 允许自定义 `func`，还支持元组模式并行维护多个累积量。
- 自定义 body 有严格限制：**不能**含分支、循环、访存（load/store/printf）、嵌套归约/扫描。
- 行归约内核的标准模式：`ct.full` 建累加器 → 循环 `load` 累加 → `ct.sum(axis=...)` 压扁 → `store`，LayerNorm 是其工业级范本。

## 7. 下一步学习建议

- **横向巩固**：阅读完整的 [samples/LayerNorm.py](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/samples/LayerNorm.py)，它把本讲的 `ct.sum(axis=...)` 与 u3-l3 的循环累加器、u4-l3 的 `atomic_cas` 锁结合在一起，是综合训练的最佳样本。
- **进入归约的对立面**：下一讲 u3-l5 讲「常量嵌入与编译期求值」，归约里的 `N`、`TILE_N` 如何成为编译期常量、`static_eval` 如何在编译期算出标量，都将在那里展开。
- **后续深入 IR**：若你想知道 `tile_reduce` / `tile_scan` 的 body 最终如何编码成字节码，可在 U7（后端：字节码与 cubin 生成）中阅读 `TileReduce.generate_bytecode` / `TileScan.generate_bytecode`（[src/cuda/tile/_ir/ops.py:2088-2125](https://github.com/nvidia/cutile-python/blob/dc83f62035193b0b7b0475a9706f80456015d5f8/src/cuda/tile/_ir/ops.py#L2088-L2125)）。
- **张量核方向**：归约是 GEMM 中 K 维累加的基础，学完本讲后可直接进入 u3-l6（矩阵乘与张量核），体会「累加器沿 K 维流动」与归约的内在联系。
