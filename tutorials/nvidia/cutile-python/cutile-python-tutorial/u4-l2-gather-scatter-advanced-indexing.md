# gather/scatter 与高级索引

## 1. 本讲目标

在 u3-l1 中，我们建立了 cuTile 内核最核心的 **load–compute–store** 三段式范式：`ct.load(array, index, shape)` 用一个**瓦片索引（tile space index）**从数组里取出一整块连续、对齐的 tile，`ct.store` 是它的逆操作。这种「规则瓦片」访问覆盖了 GEMM、归约、卷积等绝大多数高性能场景。

但有一类访问模式规则瓦片表达不了：**按下标随机读写**。例如：

- **embedding 查表**：给定一组 token id，从权重表里把对应行「抽」出来。
- **稀疏矩阵**：按行列索引数组取散落的非零元。
- **scatter 累加**：把一组计算结果按索引散播到全局输出数组的任意位置（如 top-k、直方图、反向梯度）。
- **2D 数组中「行随机、列连续」**：例如按行索引取若干整行（advanced indexing）。

学完本讲，你应当能够：

1. 掌握 **`ct.gather` / `ct.scatter`**：用一/多维「索引 tile」按元素下标随机读写数组，理解索引广播、`mask`、`check_bounds`、`padding_value` 的语义。
2. 掌握 **`ct.load_advanced_indexing` / `ct.store_advanced_indexing`**：在多维数组上做「某一维稀疏、其余维稠密连续切片」的混合索引，理解 `ct.Slice(start, length)` 的作用与「恰好一个稀疏维」的约束。
3. 从底层 IR 理解这两组 API 的差异：`gather`/`scatter` 把所有索引降级为一个**扁平指针 + 标量掩码**走 `LoadPointer`/`StorePointer`；advanced indexing 走的是结构化的 `TileLoad` 视图，可能落到 TMA。从而理解它们在**对齐与性能**上的不同含义。

---

## 2. 前置知识

本讲默认你已掌握（来自 u2、u3）：

- **全局数组 Array**：放在 GPU 全局显存、由 host 分配、带 `shape`/`strides`/`dtype` 的多维数组（u2-l2）。地址公式为：

  \[
  \text{addr}(\mathbf{i}) = \text{base} + \text{sizeof}(dt)\cdot\sum_{k}\text{stride}_k \cdot i_k
  \]

- **tile space vs element space**：常规 `ct.load` 的 `index` 是**瓦片索引**——`array[i*tm + x]`；本讲里的「索引 tile」则是**元素下标**——直接指向 `array[i]`。这是最关键的概念区分，请时刻留意。

- **tile 不可变、广播规则与 NumPy 一致**（u2-l3）：`gather` 的多组索引会按 NumPy 广播规则合并出结果形状。

- **PaddingMode**（u2-l4）：越界填充模式，advanced indexing 沿用了它。

- **stub 与 @impl 注册机制**（u5-l7 会深入，本讲只需知道）：`ct.gather` 等是「签名在前端、实现在后端」的 stub，真正的逻辑在 `_ir/ops.py` 的 `gather_impl` 等函数里。

一个直觉性的对比：

| API | 索引含义 | 访问模式 | 典型用途 |
|---|---|---|---|
| `ct.load/store` | 瓦片索引 | 规则、对齐、整块 | GEMM、卷积、归约 |
| `ct.gather/scatter` | 元素下标 tile | 完全随机、逐元素 | embedding、top-k、稀疏 |
| `ct.load/store_advanced_indexing` | 一维稀疏 + 其余稠密切片 | 半结构化 | 取若干整行/整列 |

---

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [src/cuda/tile/_stub.py](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_stub.py) | 公共 API 的签名与文档：`Slice`、`load_advanced_indexing`、`store_advanced_indexing`、`gather`、`scatter`。 |
| [src/cuda/tile/_ir/ops.py](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_ir/ops.py) | 上述 stub 的 IR 实现：`gather_impl`/`scatter_impl`/`load_advanced_impl`、辅助函数 `_gather_scatter_pointer_and_mask`/`_parse_advanced_index`、底层 `LoadPointer`/`StorePointer` 操作。 |
| [test/test_gather_scatter.py](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/test/test_gather_scatter.py) | `gather`/`scatter` 的端到端测试：1D/2D 复制、标量、自定义 padding、bounds 检查开关、自定义/broadcast/scalar mask、类型校验。 |
| [test/test_load_store_advanced_indexing.py](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/test/test_load_store_advanced_indexing.py) | advanced indexing 的端到端测试：非连续行 gather/scatter、动态/常量 Slice start、越界零填充、重复索引、各类错误用例。 |

> 阅读建议：先看 `_stub.py` 里的文档字符串建立语义直觉，再看 `test/*` 里的真实内核确认行为，最后下钻到 `_ir/ops.py` 的 impl 理解底层做了什么。

---

## 4. 核心概念与源码讲解

本讲拆为三个最小模块：**4.1 gather**、**4.2 scatter**、**4.3 load/store_advanced_indexing**。三者都解决「按元素下标而非瓦片索引访问数组」的问题，但抽象层级与适用形态不同。

### 4.1 gather：按下标 tile 读取

#### 4.1.1 概念说明

`ct.gather(array, indices)` 从 `array` 中按「**元素下标**」读取数据，返回一个 tile。这里的 `indices` 不再是 u3-l1 里的瓦片索引，而是**与结果同形的下标 tile**——每个元素直接指出「我要取 `array` 的第几个元素」。

关键点：

- `indices` 必须是一个 **tuple**，长度等于 `array` 的秩（rank）。例如 2D 数组要传 `(ind0, ind1)`。
- 元组里每个分量是**整数 tile 或标量**，形状不必相同，但必须能按 NumPy 规则**广播**到一个公共形状；结果 tile 的形状就是这个广播形状。
- 1D 数组的特例：`indices` 可以直接传一个 tile，等价于长度为 1 的 tuple。
- 负下标被当作**越界**处理，不遵循 Python 的负索引约定。

`gather` 还提供三个控制读安全的参数：

- `mask`：布尔 tile/标量，`False` 处不读取、改用 `padding_value`。
- `padding_value`：掩码掉或越界时返回的值（默认 0），可以是标量或可广播 tile。
- `check_bounds`：是否做越界检查，默认 `True`。设为 `False` 时调用者自负其责，越界是**未定义行为**（但省掉 mask 计算开销）。

#### 4.1.2 核心流程

设数组秩为 \(r\)，索引分量为 \(I_0,\dots,I_{r-1}\)，公共广播形状为 \(S\)。则结果 tile \(T\) 满足：

\[
T[\mathbf{j}] = \text{array}[I_0[\mathbf{j}],\; I_1[\mathbf{j}],\; \dots,\; I_{r-1}[\mathbf{j}]],\quad \forall \mathbf{j} \in S
\]

底层把每个下标按对应 stride 折叠成一个**线性偏移**，加到基地址上得到一个「散落指针 tile」，再走逐元素 load。若 `check_bounds=True`，会逐维生成「下标 < 该维长度」的布尔掩码，再 AND 起来作为 load 的 mask。整体伪代码：

```
final_offset = Σ_k  (astype(I_k, uint64) * stride_k)         # 广播到公共形状 S
final_mask   = AND_k (I_k < array.shape[k])    if check_bounds
final_mask   = final_mask AND custom_mask      若还传了 mask
result       = LoadPointer(ptr=base+offset, mask=final_mask, pad=padding_value)
```

这就是「逐元素随机读」的本质：**一个带掩码的散落地址 load**。

#### 4.1.3 源码精读

公共签名与文档：[src/cuda/tile/_stub.py:L1597-L1644](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_stub.py#L1597-L1644) 定义了 `gather` 的参数与语义，文档里明确：结果形状等于索引的广播形状；`mask` 与 `check_bounds` 同时存在时取逻辑 AND；负下标视为越界。

底层实现在 [src/cuda/tile/_ir/ops.py:L1208-L1230](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_ir/ops.py#L1208-L1230) 的 `gather_impl`。它的核心只有两步：

```python
pointer, final_mask = _gather_scatter_pointer_and_mask(array, indices, check_bounds, mask)
...
result, _token = load_pointer(pointer, final_mask, padding_value, latency)
```

即把 `array + indices` 折算成一个**指针 + 掩码**，再调用通用的 `load_pointer`。

指针与掩码的折算在 [src/cuda/tile/_ir/ops.py:L1308-L1383](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_ir/ops.py#L1308-L1383) 的 `_gather_scatter_pointer_and_mask`。逐维累加偏移、逐维生成越界掩码的关键片段（节选）：

```python
ind = astype(ind, datatype.uint64)
ind = broadcast_to(ind, common_shape)
if check_bounds:
    array_size = array_val.shape[dim]
    dim_mask = compare_tensorlike("lt", ind, array_size)   # 该维越界掩码
    mask = ... and_ ...                                     # 跨维 AND
offset_delta = binary_arithmetic_tensorlike("mul", ind, stride)
offset = ... + offset_delta                                 # 累加成线性偏移
```

最终落到的 `LoadPointer` IR 操作见 [src/cuda/tile/_ir/ops.py:L1116-L1136](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_ir/ops.py#L1116-L1136)：它有 `pointer`/`mask`/`padding_value`/`token` 四个 operand，对应上面算出的散落指针与掩码。

> **性能含义**：因为地址完全随机、不保证对齐，`gather` 通常无法走 TMA（Tensor Memory Accelerator），访存效率低于规则 `ct.load`。它适合「数据本身随机」的场景，不要用它来做本可以用瓦片覆盖的规则访问。

#### 4.1.4 代码实践

**实践目标**：用一个 1D `gather`+`scatter` 实现数组拷贝，并验证越界处填默认 0。

**操作步骤**：

1. 阅读测试 [test/test_gather_scatter.py:L24-L30](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/test/test_gather_scatter.py#L24-L30) 中的 `array_copy_1d` 内核：

   ```python
   @ct.kernel
   def array_copy_1d(x, y, TILE: ct.Constant[int]):
       bid = ct.bid(0)
       indices = ct.arange(TILE, dtype=np.int64)
       indices += bid*TILE
       tx = ct.gather(x, indices)
       ct.scatter(y, indices, tx)
   ```

   注意：这里 `indices` 是**元素下标**（`bid*TILE + 局部偏移`），与常规 `ct.load(x, (bid,))` 的瓦片索引写法不同，但语义等价。

2. 自行写一个最小内核：长度为 6 的数组，用 `ct.arange(8)`（8 个下标，最后两个越界）做 `gather`，`padding_value` 用默认值，再 `store` 到输出。

**需要观察的现象**：

- 输出前 6 个元素等于输入，最后 2 个元素为 0（默认 `padding_value`）。
- 这与 [test/test_gather_scatter.py:L90-L103](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/test/test_gather_scatter.py#L90-L103) 的 `custom_padding_constant` 行为一致（那里把 pad 换成自定义常量）。

**预期结果**：输入 `[100,101,102,103,104,105]` → 输出 `[100,101,102,103,104,105,0,0]`。

> 若无法本地运行 GPU，标记「待本地验证」，仅做源码阅读理解。

#### 4.1.5 小练习与答案

**练习 1**：对一个 shape `(4,4)` 的 2D 数组，写一个 gather 取出主对角线 4 个元素。索引该怎么传？

**答案**：两个下标分量都取 `[0,1,2,3]`，即 `ind = ct.arange(4, dtype=ct.int32); t = ct.gather(x, (ind, ind))`。结果 shape 为 `(4,)`，`t[i] = x[i,i]`。可对照 [test/test_gather_scatter.py:L49-L56](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/test/test_gather_scatter.py#L49-L56) 的 2D 广播写法（那里用 `ind_x[:, None]` 与 `ind_y` 广播成 `(TILE_X, TILE_Y)`）。

**练习 2**：`gather` 同时传 `mask=m` 和 `check_bounds=True`，某处 `m` 为 `False`、且下标越界，结果取什么？

**答案**：取 `padding_value`。因为有效掩码是「自定义 mask AND 越界 mask」，两者其一为假即不读取（见 stub 文档 [src/cuda/tile/_stub.py:L1634-L1636](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_stub.py#L1634-L1636)）。

---

### 4.2 scatter：按下标 tile 写入

#### 4.2.1 概念说明

`ct.scatter(array, indices, value)` 是 `gather` 的对偶：把 `value` 里的每个元素按下标写到 `array` 的对应位置。语义为：

\[
\text{array}[I_0[\mathbf{j}],\dots,I_{r-1}[\mathbf{j}]] = \text{value}[\mathbf{j}]
\]

`indices` 的约定与 `gather` 完全一致（元组长度等于秩、可广播、1D 可省略 tuple）。`value` 可以是标量或可广播到公共形状的 tile。`mask` 与 `check_bounds` 的语义也一致：`False` 或越界处**不写入**。

**关键差异与陷阱**：

- `gather` 的越界是「读不到→填 pad」，**安全**；`scatter` 的越界是「不写」，但若**多个下标指向同一位置**（重复索引），则是**数据竞争 / 未定义行为**——因为没有原子语义（要原子请用 `ct.atomic_*`）。
- 因此 scatter 适合「写出位置互不重叠」的场景。

#### 4.2.2 核心流程

```
pointer, final_mask = _gather_scatter_pointer_and_mask(array, indices, check_bounds, mask)
value               = astype/broadcast 到 pointer 形状
StorePointer(ptr=pointer, value=value, mask=final_mask)
```

与 gather 共用同一个 `_gather_scatter_pointer_and_mask`，只是把 `LoadPointer` 换成 `StorePointer`。

#### 4.2.3 源码精读

公共签名：[src/cuda/tile/_stub.py:L1647-L1689](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_stub.py#L1647-L1689)。文档明确写出赋值语义 `array[ind0[i,j,0], ind1[i,0,k]] = value[j,k]`，以及「重复下标的写顺序未指定」的隐含风险。

底层实现 [src/cuda/tile/_ir/ops.py:L1233-L1245](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_ir/ops.py#L1233-L1245) 的 `scatter_impl` 与 `gather_impl` 结构对称：复用同一指针/掩码折算，最后调用 `store_pointer`。

最终的 `StorePointer` IR 操作见 [src/cuda/tile/_ir/ops.py:L1153-L1172](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_ir/ops.py#L1153-L1172)，operand 为 `pointer`/`value`/`mask`/`token`。

一个能验证「越界不写」的测试是 [test/test_gather_scatter.py:L172-L180](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/test/test_gather_scatter.py#L172-L180)：对一个只覆盖前 5 元素的 slice 视图写 8 个元素，后 3 个越界，原数组后 3 个元素保持不变。而 [test/test_gather_scatter.py:L183-L194](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/test/test_gather_scatter.py#L183-L194) 的 `copy_8_unchecked` 展示了 `check_bounds=False` 时 IR 里 mask 为 `None`（可由 [test/test_gather_scatter.py:L201-L216](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/test/test_gather_scatter.py#L201-L216) 的 IR 断言验证：`store_ops[0].mask is not None` 仅在 checked 模式成立）。

#### 4.2.4 代码实践

**实践目标**：用 scatter 实现一个「带 mask 的选择性写入」，理解 mask 过滤。

**操作步骤**：

1. 阅读 [test/test_gather_scatter.py:L288-L311](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/test/test_gather_scatter.py#L288-L311) 的 `scatter_with_custom_mask`：

   ```python
   mask_tile = ct.gather(mask_array, indices)        # 从数组读 mask
   values    = ct.gather(x, indices)
   ct.scatter(y, indices, values, mask=mask_tile, check_bounds=False)
   ```

2. 运行后对照断言：`mask` 为 `False`（奇数位）的位置保持 0，偶数位写入对应值。

**需要观察的现象**：输出为 `[100, 0, 102, 0, 104, 0, 106, 0]`——mask 直接控制了哪些位置被写入。

**预期结果**：与上述断言一致。

#### 4.2.5 小练习与答案

**练习 1**：如果想把多个梯度**累加**到同一个输出位置（如反向传播的 scatter-add），能直接用 `ct.scatter` 吗？

**答案**：不能。`scatter` 对重复下标是数据竞争，不是累加。应改用 `ct.atomic_add` 等原子 RMW 操作（u4-l3 会讲原子族；`atomic_cas` 等遵循与 gather/scatter 相同的下标约定，见 [src/cuda/tile/_stub.py:L1708-L1713](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_stub.py#L1708-L1713)）。

**练习 2**：`ct.scatter(array, ind, value)` 与 `ct.store(array, (k,), tile)` 都把数据写到数组，本质区别是什么？

**答案**：`store` 的索引是**瓦片索引**（一个标量 `k` 表示「第 k 个瓦片」，写到 `array[k*tm : (k+1)*tm]`），写整块连续区域；`scatter` 的索引是**元素下标 tile**，每个元素各自决定写往何处，位置可散落、可越界。前者规则高效，后者灵活但可能不对齐、可能冲突。

---

### 4.3 load/store_advanced_indexing：稀疏维 + 稠密切片

#### 4.3.1 概念说明

`gather` 要求**每一维**都给下标 tile，结果是逐元素随机。但很多真实场景是**半结构化**的：「我想取若干整行，每行内部是连续的」。例如 embedding 查表——行号随机、列连续。

`ct.load_advanced_indexing(array, indices)` 正是为这种形态设计的。它的 `indices` 是一个长度等于 `array.ndim` 的 tuple，其中：

- **恰好一个**分量是 **1D 整数 tile**（称为**稀疏维 sparse dim**），给出要取的若干个切片编号；
- **其余分量**都是 `ct.Slice(start, length)`（称为**稠密维 dense dim**），表示该维上 `[start, start+length)` 这段连续区间。

结果 tile 的形状为 `(len_0, ..., len_{n-1})`：稀疏维的长度 = 索引 tile 的长度，稠密维的长度 = 对应 `Slice.length`。

`ct.Slice(start, length)` 的语义见 [src/cuda/tile/_stub.py:L948-L967](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_stub.py#L948-L967)：`start` 是元素空间的运行时偏移；`length` 是 tile 大小，**必须是 2 的幂且为编译期常量**。

`store_advanced_indexing` 是其对偶，约定完全一致，且要求 tile 形状与 indices 隐含的形状严格相等。

**约束**（来自 stub 文档与实现）：

1. tuple 长度必须等于数组秩，否则报错。
2. **恰好一个**稀疏维——零个或多个都报错。
3. 稀疏维必须是 **1D** 整数 tile（2D 报错）。
4. 每个稠密维 `Slice.length` 必须 > 0 且为 2 的幂。
5. 数组秩必须 ≥ 2（1D 数组请直接用 `gather`）。
6. 需要 `BytecodeVersion.V_13_3`（即较新的 tileiras）。

#### 4.3.2 核心流程

设数组为 \(A\)，稀疏维为 \(d\)，稀疏索引 tile \(I\)（长度 \(L_d\)），其余维 \(k\neq d\) 为稠密切片 \([s_k, s_k+\ell_k)\)。则结果 tile：

\[
T[j_0,\dots,j_{r-1}] = A\big[\;\text{if }k=d\text{ then }I[j_d]\text{ else }s_k + j_k\;\big]_{k=0}^{r-1}
\]

其中 \(0\le j_d < L_d\)，\(0\le j_k < \ell_k\)（\(k\neq d\)）。本质上：稀疏维做 gather，稠密维做规则 load，二者在视图层融合。

底层会构造一个 **gather/scatter 视图**（`make_gather_scatter_view`），再发一条结构化的 `TileLoad` 操作（与常规 `ct.load` 同族），因此**有机会落到 TMA**，性能优于纯 `gather`。

> **越界语义**：稠密维越界元素被忽略（store）或按 `padding_mode` 填充（load，默认 `UNDETERMINED`）；稀疏维越界同理，整体越界为未定义行为。重复的稀疏索引在 load 是良定义的（重复读同一行），在 store 是数据竞争。

#### 4.3.3 源码精读

公共签名与示例：

- `load_advanced_indexing`：[src/cuda/tile/_stub.py:L1489-L1549](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_stub.py#L1489-L1549)。文档给出典型用法 `ct.load_advanced_indexing(x, (row_indices, ct.Slice(col_start, 4)))`——行稀疏、列稠密。
- `store_advanced_indexing`：[src/cuda/tile/_stub.py:L1552-L1594](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_stub.py#L1552-L1594)。

实现入口 [src/cuda/tile/_ir/ops.py:L3279-L3298](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_ir/ops.py#L3279-L3298) 的 `load_advanced_impl`：

```python
if array_ty.ndim < 2:
    raise TileTypeError("... use ct.gather() for 1D arrays")
sparse_dim, tile_shape, gs_index = _parse_advanced_index(indices, array_ty.ndim)
...
view = make_gather_scatter_view(array, tile_shape, sparse_dim, padding_mode_val)
result, _token = add_operation_variadic(TileLoad, ..., view=view, index=gs_index, ...)
```

注意它生成的是 `TileLoad`（结构化、可 TMA），而非 `LoadPointer`（散落指针）——这是它与 `gather` 在底层最本质的区别。

索引的解析与校验在 [src/cuda/tile/_ir/ops.py:L3213-L3276](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_ir/ops.py#L3213-L3276) 的 `_parse_advanced_index`：遍历每个分量，区分 `TileTy`（稀疏维，记录其维度号）与 `IndexSliceTI`（稠密维，校验 length 为编译期常量、为正、为 2 的幂），最后强制「恰好一个稀疏维」并校验所有维度为 2 的幂。

`ct.Slice` 对象本身的构造实现在 [src/cuda/tile/_ir/ops.py:L3205-L3210](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_ir/ops.py#L3205-L3210)，它要求 start/length 都是 0D 整数 tile，产出 `IndexSliceTI` 类型。

行为层面的端到端验证：

- 非连续行 gather：[test/test_load_store_advanced_indexing.py:L41-L55](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/test/test_load_store_advanced_indexing.py#L41-L55) 用 `indices = arange(ROWS)*2` 取偶数行，等价于 PyTorch 的 `x[::2, :y_cols]`。
- 稀疏维部分越界零填充：[test/test_load_store_advanced_indexing.py:L166-L181](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/test/test_load_store_advanced_indexing.py#L166-L181)，索引 `[6,7,8,9]` 对 8 行数组，后两个越界行被填 0。
- 错误用例：2D tile 当稀疏维报「1D」、零/多个稀疏维报「exactly one」、tuple 长度不等于秩、`Slice.length` 非 2 的幂——见 [test/test_load_store_advanced_indexing.py:L243-L323](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/test/test_load_store_advanced_indexing.py#L243-L323)。

#### 4.3.4 代码实践

**实践目标**：理解 `Slice.length` 的「2 的幂」约束，并对比常量 vs 动态 start。

**操作步骤**：

1. 阅读 [test/test_load_store_advanced_indexing.py:L315-L323](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/test/test_load_store_advanced_indexing.py#L315-L323)：传 `ct.Slice(0, 3)`（length=3，非 2 的幂）应抛 `TileTypeError`，match 字符串 `"power of two"`。
2. 阅读 [test/test_load_store_advanced_indexing.py:L103-L116](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/test/test_load_store_advanced_indexing.py#L103-L116) 的常量 start 版本，与 [test/test_load_store_advanced_indexing.py:L79-L95](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/test/test_load_store_advanced_indexing.py#L79-L95) 的动态 start 版本对比：`Slice` 的 `start` 可以是运行时变量（如参数 `col_start`），但 `length` 必须是编译期常量。

**需要观察的现象**：

- length=3 时编译期报错，错误信息明确提到 `power of two`。
- 动态 start 改变时，取出的列窗口随之平移，但 `length` 不变。

**预期结果**：常量 start 测试取出 `x[:, 2:6]`；动态 start=2 时取出 `x[:, 2:4]`。若本地无 GPU/新版 tileiras，标记「待本地验证」（advanced indexing 需 `BytecodeVersion.V_13_3`）。

#### 4.3.5 小练习与答案

**练习 1**：对一个 `(8, 8)` 数组，想取出第 `[7, 4, 2, 3]` 行、每行前 4 列，怎么写？

**答案**：稀疏维放行索引 tile，列维放稠密 Slice：

```python
indices = ... # 形状 (4,)，值为 [7,4,2,3]，可用 ct.where 构造
tile = ct.load_advanced_indexing(x, (indices, ct.Slice(0, 4)))
```

结果 shape `(4, 4)`，等价于 PyTorch 的 `x[[7,4,2,3], :4]`。可对照 [test/test_load_store_advanced_indexing.py:L124-L138](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/test/test_load_store_advanced_indexing.py#L124-L138)。

**练习 2**：能否用 `load_advanced_indexing` 取「行连续、列稀疏」？稀疏维能在列上吗？

**答案**：能。稀疏维可以是任意一维（实现里用 `sparse_dim` 记录其维度号，见 `_parse_advanced_index`）。例如取若干列、所有行：`(ct.Slice(0, ROWS), col_indices)`，此时列是稀疏维。约束仍是「恰好一个稀疏维」。

---

## 5. 综合实践

**任务：实现一个 embedding gather 内核。**

给定：

- 权重表 `W`，shape `(vocab_size, embed_dim)`，float32；
- token id 数组 `ids`，shape `(num_tokens,)`，int32，每个值 ∈ `[0, vocab_size)`；
- 输出 `out`，shape `(num_tokens, embed_dim)`。

要求把每个 token 对应的权重行写到 `out`。即 `out[i] = W[ids[i]]`。

**实现要点**：

1. 这正是「行稀疏（按 id 取行）、列稠密（取整行）」的半结构化访问，**首选 `load_advanced_indexing`**。
2. `embed_dim` 必须 tile 化，且 `Slice.length` 须为 2 的幂——若 `embed_dim` 不是 2 的幂，取最近的上界 2 的幂并用 `padding_mode` 处理越界列，或在 host 端把 `embed_dim` 补齐到 2 的幂。
3. token 数量 `num_tokens` 可能不是 2 的幂——稀疏维 tile 的长度（即一次取多少 token）需要是 2 的幂，用 persistent 循环（u4-l1）或分块覆盖全部 token。

**参考内核骨架**（示例代码，非项目原有代码）：

```python
import cuda.tile as ct

@ct.kernel
def embedding_gather(W, ids, out,
                     EMBED: ct.Constant[int],    # embed_dim，须为 2 的幂
                     TOKENS: ct.Constant[int]):  # 本 block 处理的 token 数（2 的幂）
    bid = ct.bid(0)
    # 本 block 负责的 token 下标：bid*TOKENS .. bid*TOKENS+TOKENS
    row_idx = ct.arange(TOKENS, dtype=ct.int32) + bid * TOKENS
    # 行稀疏：按 row_idx 取若干行；列稠密：取 [0, EMBED) 整行
    tile = ct.load_advanced_indexing(W, (row_idx, ct.Slice(0, EMBED)))
    # 写到 out 的对应行块（规则瓦片写，用普通 store 即可）
    ct.store(out, (bid, 0), tile)
```

启动：`grid = (cdiv(num_tokens, TOKENS), 1, 1)`，参数 `(W, ids, out, embed_dim, TOKENS)`。

**进阶对比**：用纯 `ct.gather` 也写得出来，但需要把列下标也凑成索引 tile：

```python
ind_row = row_idx[:, None]                      # (TOKENS, 1)
ind_col = ct.arange(EMBED, dtype=ct.int32)      # (EMBED,)
tile = ct.gather(W, (ind_row, ind_col))         # 广播成 (TOKENS, EMBED)
```

功能等价，但底层走的是 `LoadPointer`（散落地址、不走 TMA），通常比 `load_advanced_indexing` 慢。

**验证**：用 PyTorch 写 `expected = W[ids]`，与 cuTile 输出逐元素比较（注意 `num_tokens`/`embed_dim` 的 2 的幂处理边界）。

> 若本地无 GPU 或 tileiras 版本低于 V_13_3，可只做源码阅读：跟踪 `load_advanced_indexing` 在 `_parse_advanced_index` → `make_gather_scatter_view` → `TileLoad` 的调用链，标注每一步处理的是「稀疏维」还是「稠密维」。

---

## 6. 本讲小结

- `ct.gather` / `ct.scatter` 用**元素下标 tile** 随机读写数组，索引元组长度等于数组秩、各分量按 NumPy 规则广播；底层都折算成一个**散落指针 + 越界/自定义掩码**，落到 `LoadPointer`/`StorePointer`。
- `mask` 与 `check_bounds` 同时存在时有效掩码取逻辑 AND；gather 越界返回 `padding_value`，scatter 越界/被掩码处不写入；**scatter 对重复下标是数据竞争**（要原子语义用 `ct.atomic_*`）。
- `ct.load_advanced_indexing` / `ct.store_advanced_indexing` 解决「**一维稀疏 + 其余维稠密连续切片**」的半结构化访问，用 `ct.Slice(start, length)` 表达稠密维（`length` 须为 2 的幂且编译期常量），稀疏维必须是 1D 整数 tile 且**恰好一个**。
- advanced indexing 底层走结构化 `TileLoad`（与常规 load 同族），**可能落到 TMA**，性能优于纯 gather；需要 `BytecodeVersion.V_13_3`。
- 选型直觉：规则整块用 `load/store`；完全随机逐元素用 `gather/scatter`；行/列稀疏+其余连续用 `advanced_indexing`。

---

## 7. 下一步学习建议

- **u4-l3 内存模型与原子操作**：当 scatter 的写出位置可能重叠（如直方图、scatter-add），需要 `ct.atomic_add`/`ct.atomic_cas`，它们遵循与 gather/scatter 相同的下标约定，是本讲的自然延续。
- **u4-l1 TiledView 与 persistent 遍历**：综合实践里 token 数非 2 的幂时的分块/persistent 处理，与 `num_tiles`/`traversal_steps` 直接相关。
- **源码延伸阅读**：想深入了解 advanced indexing 的视图如何构造，可阅读 `_ir/ops.py` 中 `make_gather_scatter_view` 的实现，以及 `TileLoad` 操作的定义，理解「稀疏维 gather 与稠密维规则 load 在视图层融合」的具体机制（对应 u5-l5/u5-l6 的 IR 核心与类型系统）。
