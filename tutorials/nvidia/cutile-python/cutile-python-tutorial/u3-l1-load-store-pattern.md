# load/store 与 load-compute-store 范式

## 1. 本讲目标

本讲是「编写内核」单元的第一讲。读完本讲，你应该能够：

- 理解 cuTile 内核最核心的 **load–compute–store** 三段式范式：先把全局数组（`Array`）的一块搬成内核内部的 `Tile`，对 tile 做集体计算，再把结果 `Tile` 写回全局数组。
- 掌握 `ct.load` / `ct.store` 的关键参数：`index`（tile space 索引）、`shape`（tile 形状）、`padding_mode`（越界填充）。
- 理解 `ct.bid(0)` 如何给出当前 block 的编号，以及它如何参与构造 `index`，从而让每个 block 处理不同的数据块。
- 独立写出一个完整的一维内核（如 vector_add 或 scale），并在 host 端用 `ct.cdiv` 计算出 grid、用 `ct.launch` 启动。

本讲覆盖三个最小模块：**`bid`、`load`、`store`**。这三者合起来就是 cuTile 内核的「主循环骨架」。

## 2. 前置知识

在进入本讲前，请确认你已经理解下面这些来自前面讲义的概念（本讲会直接使用，不再重复解释）：

- **执行空间**：host code / SIMT code / tile code 三种空间（见 u2-l1）。本讲的 `ct.load`/`ct.store`/`ct.bid` 都只能在 tile code 里使用；`ct.launch` 只能在 host code 里使用。
- **grid 与 block**：一次 kernel 启动由 `grid`（一组 block）组成，每个 block 完整执行一遍 kernel 函数体。cuTile 表达 block 级并行，**不暴露单个线程**（见 u2-l1）。
- **Array 与 Tile 的区别**：`Array` 是 host 分配、放在全局显存、可读写、运行时 shape 的全局数组；`Tile` 是 kernel 内部不可变、编译期 shape（每维为 2 的幂）的临时数据块（见 u2-l2、u2-l3）。**load 把 Array 变成 Tile，store 把 Tile 变回 Array。**
- **`@ct.kernel` 与 `ct.launch`**：`@ct.kernel` 标记的函数不会在定义时执行，必须由 host 端的 `ct.launch` 启动（见 u1-l2、u1-l4）。
- **`Constant[int]`**：编译期常量参数，会被嵌入生成的 cubin，改值会触发重新编译（见 u1-l2、u3-l5）。

一句话回顾整体链路（见 u1-l1）：

```
AST → HIR → Tile IR →（优化 pass）→ 字节码 →（tileiras）cubin →（缓存）→ cuLaunchKernel
```

本讲关注的是**最左端的用户视角**：你用 `ct.load`/`ct.store`/`ct.bid` 写出的那段 Python 代码，到底表达了什么样的数据搬运与计算。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
|------|------|
| [_stub.py](src/cuda/tile/_stub.py#L1218-L1227) | `ct.load`、`ct.store`、`ct.bid`、`ct.cdiv` 等 stub 的**权威签名与文档**。stub 只有签名没有实现，真正的实现在后端 IR 注册系统里（见 u5-l7）。 |
| [samples/quickstart/VectorAdd_quickstart.py](samples/quickstart/VectorAdd_quickstart.py#L15-L28) | 官方快速入门示例，展示了完整的 load–compute–store 内核与 host 端启动。 |
| [test/test_copy.py](test/test_copy.py#L28-L32) | 测试套件里的纯拷贝内核（1D/2D/3D），是理解 load/store 最小、最干净的范本，还覆盖了 `padding_mode`。 |
| [_execution.py](src/cuda/tile/_execution.py#L173-L182) | `@stub` 装饰器的实现，解释了为什么 `ct.load` 里的函数体只是「签名 + 文档」。 |

> 提示：`_stub.py` 里的函数体**不是运行时实现**。`ct.load(a, ...)` 在 tile code 里调用时，会被编译前端翻译成具体的 IR 操作。这里我们只读签名和文档来理解**语义**；实现侧的注册机制留到 u5-l7 讲。

## 4. 核心概念与源码讲解

### 4.1 `bid`：当前 block 在 grid 中的坐标

#### 4.1.1 概念说明

一次 `ct.launch` 会启动 `grid` 个 block，**每个 block 独立、并行地执行一遍整个 kernel 函数体**。那么问题来了：所有 block 跑的是同一段代码，怎么让不同的 block 处理不同的数据？

答案就是 `ct.bid(axis)`：它返回**当前 block** 在 grid 第 `axis` 个维度上的坐标（编号）。这是 tile code 里「定位自己」的唯一标准手段。

- `axis` 只能取 `0`、`1`、`2`，对应三维 grid 的三个轴。
- 返回值是一个 `int32` 标量（零维 tile，见 u2-l3）。
- 它**只能在 tile code 内部使用**，host 上没有「当前 block」的概念。

#### 4.1.2 核心流程

grid 是一个形状为 `(nx, ny, nz)` 的三维 block 网格。运行时，每个 block 拿到自己的三维坐标 `(bx, by, bz)`：

```
bx = ct.bid(0)   # 第 0 轴坐标，范围 [0, nx)
by = ct.bid(1)   # 第 1 轴坐标，范围 [0, ny)
bz = ct.bid(2)   # 第 2 轴坐标，范围 [0, nz)
```

对一维问题，grid 通常形如 `(num_blocks, 1, 1)`，于是只需 `bid(0)`。配套的 `ct.num_blocks(axis)` 返回该轴上的 block 总数（`nx`/`ny`/`nz`），常用于 persistent 内核的循环上界（见 u4-l1）。

#### 4.1.3 源码精读

`bid` 的 stub 定义与文档：

```python
@stub
def bid(axis) -> int:
    """Gets the index of current block.
    ...
    Args:
        axis (const int): The axis of the block index space. Possible values are 0, 1, 2.
    Returns:
        int32:
    """
```

完整定义见 [_stub.py:L1090-L1113](src/cuda/tile/_stub.py#L1090-L1113) —— 注意它没有函数体，只有签名和文档，因为它是 stub。

在官方快速入门里，`bid(0)` 用来得到一维 block 编号，并直接作为 `load` 的 tile space 索引：

```python
@ct.kernel
def vector_add(a, b, c, tile_size: ct.Constant[int]):
    pid = ct.bid(0)              # 当前 block 编号
    a_tile = ct.load(a, index=(pid,), shape=(tile_size,))
    ...
```

见 [VectorAdd_quickstart.py:L15-L28](samples/quickstart/VectorAdd_quickstart.py#L15-L28)。这里 `pid` 既是「我是第几个 block」，也是「我要加载第几块 tile」——两者一一对应，这正是 cuTile 把 block 并行与 tile 切分对齐的关键设计。

`@stub` 装饰器本身只是给函数打上标记，并不提供实现：

```python
def stub(func=None, /, *, host=False):
    def decorate(func):
        func = function(func, host=host)
        func._cutile_python_stub = True
        return func
    ...
```

见 [_execution.py:L173-L182](src/cuda/tile/_execution.py#L173-L182)。`_cutile_python_stub = True` 这个标志会被后端注册系统识别，把 `ct.bid` 这个名字和真正的 IR 实现对接起来（详见 u5-l7）。

#### 4.1.4 代码实践

**实践目标**：直观感受「每个 block 都执行一遍函数体，且 `bid(0)` 各不相同」。

**操作步骤**：阅读 `bid` 文档里的可运行示例（[_stub.py:L1100-L1112](src/cuda/tile/_stub.py#L1100-L1112)），它打印每个 block 的三维坐标。把它改成一个一维版本：

```python
# 示例代码（基于 bid 文档示例改写，待本地验证）
import cuda.tile as ct

@ct.kernel
def hello():
    pid = ct.bid(0)
    print(f"Hello from block {pid}")

ct.launch(stream, (4, 1, 1), hello, ())   # 启动 4 个 block
```

**需要观察的现象**：每个 block 都会执行一次 `print`，且 `pid` 分别是 `0/1/2/3`。

**预期结果**：输出 4 行，分别报出 block 0~3。注意各 block 的执行顺序在硬件上**不保证**先后，cuTile 不暴露线程、也不保证 block 间顺序（见 u2-l1）。

**待本地验证**：具体输出顺序与 CUDA stream 绑定方式有关，需本地运行确认。

#### 4.1.5 小练习与答案

**练习 1**：一个 grid 形状为 `(3, 1, 1)` 的内核里，`ct.bid(0)` 可能取到哪些值？

> **答案**：`0`、`1`、`2`。第 0 轴有 3 个 block，编号从 0 开始。

**练习 2**：为什么不能在 host 代码里写 `pid = ct.bid(0)`？

> **答案**：`bid` 表达的是「当前正在执行 kernel 的 block 的坐标」，host 上根本没有 block 的概念，所以 `bid` 只在 tile code 中有定义。host 端要表达并行规模，用的是 `grid`（传给 `ct.launch`）。

---

### 4.2 `load`：从全局数组取出一块 Tile

#### 4.2.1 概念说明

`ct.load` 是内核与全局显存之间的**入口**：它把一个全局 `Array` 按 tile 切分，取出其中一块，作为内核内部不可变的 `Tile` 供后续计算使用。

这里有一个最容易踩坑的点：**`index` 不是元素下标，而是 tile space（瓦片空间）下标。** 也就是说，你不是指定「从第几个元素开始读」，而是指定「读第几块瓦片」。

`load` 的常见参数（完整签名见下文源码精读）：

- `array`：要读取的全局数组。
- `index`：在 tile space 里的坐标（元组），**不是元素坐标**。
- `shape`：tile 的形状（每维必须是 2 的幂，见 u2-l3），编译期常量。
- `padding_mode`：当 tile 部分越出数组边界时，越界元素用什么填充（默认 `UNDETERMINED`，即值不确定；见 u2-l4）。

#### 4.2.2 核心流程

给定一个形状 `(M, N)` 的二维数组，用 tile 形状 `(tm, tn)` 切分，会得到一个二维的 tile space，其大小为 `(cdiv(M, tm), cdiv(N, tn))`。tile space 坐标 `(i, j)` 取出的 tile 满足：

\[
t[x, y] = \text{array}[i \cdot tm + x,\; j \cdot tn + y], \quad 0 \le x < tm,\; 0 \le y < tn
\]

直观地说：**tile 空间索引 `i` 乘以 tile 在该维的大小 `tm`，再加上 tile 内偏移 `x`，就还原成元素下标。** 一维情形更简单：`index=(i,)`、`shape=(tm,)` 时，取出的就是元素 `[i*tm, i*tm+tm)`。

边界处理：

- **部分越界**：tile 有一部分落在数组外，越界元素按 `padding_mode` 填充（如 `ZERO` 填 0、`NAN` 填 NaN）。
- **整体越界**：tile 完全在数组之外，**行为未定义**——必须靠合理设计 grid 与 `index` 来避免。

`index` 里通常放 `ct.bid(...)`，于是「第 `i` 个 block 读第 `i` 块 tile」自然成立。

#### 4.2.3 源码精读

`load` 的完整签名：

```python
@stub
def load(array: Array, /,
         index: Shape,
         shape: Constant[Shape], *,
         order: Constant[Order] = "C",
         padding_mode: PaddingMode = PaddingMode.UNDETERMINED,
         latency: Optional[int] = None,
         allow_tma: Optional[bool] = None,
         memory_order: MemoryOrder = MemoryOrder.WEAK,
         memory_scope: MemoryScope = MemoryScope.NONE) -> Tile:
```

见 [_stub.py:L1218-L1227](src/cuda/tile/_stub.py#L1218-L1227)。注意几个关键点：

- `index: Shape` —— tile space 索引，元素个数的元组。
- `shape: Constant[Shape]` —— tile 形状，**编译期常量**（所以本讲示例里 `tile_size` 必须声明为 `ct.Constant[int]`，否则无法用作 `shape`）。
- `padding_mode` 默认 `UNDETERMINED`。
- `order` 控制轴映射（转置读取），本讲先不动它，进阶用法见 u3-l2。
- `memory_order`/`memory_scope` 涉及内存模型，见 u4-l3。

文档里给出的 tile space 寻址公式（即上面的 `t[x,y] = array[i*tm+x, j*tn+y]`）见 [_stub.py:L1233-L1245](src/cuda/tile/_stub.py#L1233-L1245)。

`load` 在真实内核里的用法（官方快速入门）：

```python
a_tile = ct.load(a, index=(pid,), shape=(tile_size,))   # pid = ct.bid(0)
```

见 [VectorAdd_quickstart.py:L21](samples/quickstart/VectorAdd_quickstart.py#L21)。`pid` 是 tile space 索引，`tile_size` 是 tile 大小（每维 2 的幂）。

测试套件里的 2D 版本清楚地展示了多轴 `index` 与多轴 `shape` 的对应：

```python
@ct.kernel
def array_copy_2d(x, y, TILE_X: ct.Constant[int], TILE_Y: ct.Constant[int]):
    bidx = ct.bid(0)
    bidy = ct.bid(1)
    tx = ct.load(x, index=(bidx, bidy), shape=(TILE_X, TILE_Y))
    ct.store(y, index=(bidx, bidy), tile=tx)
```

见 [test_copy.py:L54-L59](test/test_copy.py#L54-L59)。`index=(bidx, bidy)` 是 tile space 的二维坐标，`shape=(TILE_X, TILE_Y)` 是 tile 的二维形状，二者长度都等于数组的 `ndim`。

`padding_mode` 的用法可以从带 padding 的拷贝测试看到：

```python
tx = ct.load(x, index=(bidx, bidy), shape=(TILE_X, TILE_Y), padding_mode=padding_mode)
```

见 [test_copy.py:L112-L120](test/test_copy.py#L112-L120)，该测试在 [test_copy.py:L123-L145](test/test_copy.py#L123-L145) 里对 `UNDETERMINED / ZERO / NAN / POS_INF` 等多种模式做了参数化验证：当数组是 `(63,63)` 而 tile 是 `(64,64)` 时，最后一行/最后一列越界，按 `padding_mode` 填充。

#### 4.2.4 代码实践

**实践目标**：亲手验证 tile space 寻址公式，建立「`index` 是瓦片号不是元素号」的直觉。

**操作步骤**：

1. 设数组 `a = [0,1,2,...,9]`（共 10 个元素），`tile_size = 4`。
2. 手算：`ct.load(a, index=(0,), shape=(4,))` 取到哪些元素？`index=(1,)` 呢？`index=(2,)` 呢（注意越界）？
3. 对照 `load` 文档里的可运行示例（[_stub.py:L1284-L1303](src/cuda/tile/_stub.py#L1284-L1303)），它的输出正是 `[0,1,2,3]`、`[4,5,6,7]`、`[8,9,0,0]`（第三个用了 `padding_mode=ZERO`）。

**需要观察的现象**：

- `index=(0,)` → 元素 `0,1,2,3`
- `index=(1,)` → 元素 `4,5,6,7`
- `index=(2,)` → 元素 `8,9` + 两个越界填充值

**预期结果**：与文档示例输出一致。当不指定 `padding_mode` 时，越界值是 `UNDETERMINED`（不确定），所以**只要 tile 可能越界，就应显式指定 `padding_mode`**。

**待本地验证**：`UNDETERMINED` 模式下越界元素的具体取值未定义，本地运行不应依赖它。

#### 4.2.5 小练习与答案

**练习 1**：数组形状 `(M,) = (10,)`，`tile_size = 4`，tile space 有几块？最后一块是否越界？

> **答案**：tile space 大小为 `cdiv(10, 4) = 3`，即 3 块（编号 0/1/2）。最后一块覆盖元素 `[8, 12)`，而数组只有 10 个元素，因此元素 `10`、`11` 越界，需用 `padding_mode` 处理。

**练习 2**：把 `index` 理解成「元素起始下标」会错在哪里？举例说明。

> **答案**：若误以为 `index=(1,)` 是「从第 1 个元素读 4 个」，会期待得到 `[1,2,3,4]`；但实际 `index=(1,)` 是「读第 1 块瓦片」，得到 `[4,5,6,7]`。`index` 是瓦片号，元素下标 = `index * tile_size + tile内偏移`。

**练习 3**：为什么 `shape` 必须是编译期常量（`Constant[Shape]`），而 `index` 不必？

> **答案**：tile 的形状决定了生成的硬件指令（如一次集体加载多少元素、是否走 TMA），必须在编译期确定；而 `index`（通常是 `bid(0)`）是运行时才知道的坐标，所以它是普通的运行时标量。

---

### 4.3 `store`：把 Tile 写回全局数组

#### 4.3.1 概念说明

`ct.store` 是 `load` 的逆操作：把一个内核内部的 `Tile` 写回全局 `Array` 的某一块。它是内核与全局显存之间的**出口**。

`store` 与 `load` 几乎对称，但有两点关键不同：

1. **没有 `shape` 参数**：写回的 tile 形状直接由传入的 `tile` 参数决定（从 tile 自身的编译期 shape 推断）。
2. **越界处理是「忽略」而非「填充」**：当 tile 部分越出数组边界时，越界位置的写入被丢弃；若 tile 整体越界，行为未定义。

`store` 返回 `None`——它是一个有副作用的写操作，不产生新的 tile。

#### 4.3.2 核心流程

给定一个形状 `(tm, tn)` 的 tile `t`，写入形状 `(M, N)` 的数组，tile space 坐标 `(i, j)`：

\[
\text{array}[i \cdot tm + x,\; j \cdot tn + y] = t[x, y], \quad 0 \le x < tm,\; 0 \le y < tn
\]

这和 `load` 的寻址完全对称——同一个 tile space、同一套「瓦片号 × tile 大小 + 偏移 = 元素下标」的换算。所以一对 `load`/`store` 用相同的 `index` 时，数据会落回原位（这正是「拷贝」内核能成立的原因）。

`store` 也可以写**标量（0 维 tile）**：此时标量会广播到数组在该索引处那一整块 tile 的形状（见文档示例 [_stub.py:L1436-L1454](src/cuda/tile/_stub.py#L1436-L1454)）。

#### 4.3.3 源码精读

`store` 的完整签名：

```python
@stub
def store(array: Array, /,
          index: Shape,
          tile: TileOrScalar, *,
          order: Constant[Order] = "C",
          latency: Optional[int] = None,
          allow_tma: Optional[bool] = None,
          memory_order: MemoryOrder = MemoryOrder.WEAK,
          memory_scope: MemoryScope = MemoryScope.NONE) -> None:
```

见 [_stub.py:L1372-L1380](src/cuda/tile/_stub.py#L1372-L1380)。对比 `load`：

- 没有 `shape`（从 `tile` 推断）。
- 没有 `padding_mode`（越界是忽略，没有「填充目标」的概念）。
- 多了 `tile` 参数：要写入的值，可以是 `Tile` 或标量。
- 返回 `None`。

寻址公式与越界规则见 [_stub.py:L1386-L1397](src/cuda/tile/_stub.py#L1386-L1397)：部分越界被忽略，整体越界未定义。

真实用法（一维拷贝测试）：

```python
@ct.kernel
def array_copy_1d(x, y, TILE: ct.Constant[int]):
    bid = ct.bid(0)
    tx = ct.load(x, index=(bid,), shape=(TILE,))
    ct.store(y, index=(bid,), tile=tx)
```

见 [test_copy.py:L28-L32](test/test_copy.py#L28-L32)。注意 `load` 和 `store` 用了**相同的 `index=(bid,)`**，所以数据从 `x` 的第 `bid` 块原样搬到 `y` 的第 `bid` 块——这就是最纯粹的 load–store 范式（中间没有 compute）。

带 compute 的版本（官方快速入门）：

```python
result = a_tile + b_tile          # compute：tile 上的逐元素加法
ct.store(c, index=(pid, ), tile=result)
```

见 [VectorAdd_quickstart.py:L25-L28](samples/quickstart/VectorAdd_quickstart.py#L25-L28)。`result` 是一个新 tile（tile 不可变，算术产生新 tile，见 u2-l3、u3-l2），再被 `store` 写回数组 `c`。

#### 4.3.4 代码实践

**实践目标**：理解 `store` 的「越界忽略」与「shape 从 tile 推断」两点。

**操作步骤**：阅读 `store` 文档里的可运行示例（[_stub.py:L1419-L1434](src/cuda/tile/_stub.py#L1419-L1434)）。它对一个长度 6 的数组做两次 `store`，每次写一个形状 `(4,)` 的 tile：

```python
tile = ct.ones((4,), dtype=x.dtype)
ct.store(x, (0,), tile)      # 写到第 0 块：元素 [0,4)
ct.store(x, (1,), tile * 2)  # 写到第 1 块：元素 [4,8)，但数组只有 6 个元素
```

**需要观察的现象**：第二次 `store` 的 tile 覆盖元素 `[4, 8)`，而数组长度只有 6，所以元素 `6`、`7` 越界——这两个写入被**忽略**。

**预期结果**：`x` 变成 `[1,1,1,1,2,2]`（前 4 个是 1，后 2 个是 2，越界的两个 2 没写进去）。这与文档示例输出一致。

**待本地验证**：可用 `torch.zeros(6, dtype=torch.int32, device='cuda')` 作为 `x` 本地复现。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `store` 不需要 `shape` 参数，而 `load` 需要？

> **答案**：`load` 要新建一个 tile，必须告诉编译器 tile 的形状（决定生成什么指令）；`store` 接收的 `tile` 已经自带编译期 shape，直接拿来用即可，所以无需再传 `shape`。

**练习 2**：`load` 部分越界用 `padding_mode` 填充，`store` 部分越界怎么处理？

> **答案**：`store` 部分越界时，越界位置的写入被**忽略**（不是填充，因为没有合理的「目标填充值」语义）。整体越界则行为未定义，要靠正确的 grid 设计避免。

**练习 3**：`ct.store(c, index=(pid,), tile=result)` 这一行，`tile=result` 里为什么不能省略关键字 `tile=`？

> **答案**：`store` 的位置参数顺序是 `(array, index, tile)`，`tile` 是第三个位置参数。本例里写成 `tile=result` 是为了可读性；其实省略写成 `ct.store(c, (pid,), result)` 也可以。但注意它**不是**关键字参数，是第三个位置参数（签名里 `tile: TileOrScalar` 在 `/` 之后但在第一个 `*` 之前，是位置参数）。

---

## 5. 综合实践

把本讲的三个模块（`bid` + `load` + `store`）与 compute 串起来，完成一个完整的一维 **scale** 内核：`result = a * alpha`。

**实践目标**：独立写出一个 load–compute–store 内核，自行计算 grid，并用 `torch` 张量验证数值正确。

**操作步骤**：

1. 以 [test_copy.py 的 array_copy_1d](test/test_copy.py#L28-L32) 为模板，把中间的「直接搬运」改成「乘以 `alpha`」。
2. 在 host 端用 `ct.cdiv(N, TILE)` 计算 grid（参考 [VectorAdd_quickstart.py:L35](samples/quickstart/VectorAdd_quickstart.py#L35)）。
3. 用 `ct.launch(torch.cuda.current_stream(), grid, kernel, args)` 启动（参考 [test_copy.py:L48](test/test_copy.py#L48)）。
4. 用 `torch.allclose` 与 `a * alpha` 对比。

完整参考脚本（**示例代码**，结构模仿 `test_copy.py` 与快速入门）：

```python
# 示例代码：一维 scale 内核
import torch
import cuda.tile as ct

@ct.kernel
def scale(a, c, alpha, TILE: ct.Constant[int]):
    bid = ct.bid(0)                                  # ① 定位当前 block
    a_tile = ct.load(a, index=(bid,), shape=(TILE,)) # ② load：数组 -> tile
    result = a_tile * alpha                          # ③ compute：逐元素乘（alpha 广播）
    ct.store(c, index=(bid,), tile=result)           # ④ store：tile -> 数组

# ---- host 端 ----
N     = 4096
TILE  = 64                       # 必须是 2 的幂
alpha = 3.0

a = torch.randn(N, device='cuda')
c = torch.zeros_like(a)

grid = (ct.cdiv(N, TILE), 1, 1)  # ⑤ 用 cdiv 算 block 数量
ct.launch(torch.cuda.current_stream(), grid, scale, (a, c, alpha, TILE))

# ⑥ 数值验证
expected = a * alpha
assert torch.allclose(c, expected), "scale kernel 结果与参考不一致"
print("scale kernel passed")
```

**需要观察的现象 / 思考点**：

- ① `bid` 决定了每个 block 读第几块 tile——这正是「block 并行 ↔ tile 切分」的对齐点。
- ② `index=(bid,)` 是 tile space 索引，不是元素下标。
- ③ `alpha` 是一个 Python 浮点字面量传入，在 tile code 里成为 loosely typed 常量标量，与 tile 相乘时自动广播（见 u2-l3、u2-l4）；`a_tile * alpha` 产生一个**新** tile。
- ④ `store` 用与 `load` 相同的 `index=(bid,)`，结果写回 `c` 的对应块。
- ⑤ `TILE=64` 必须是 2 的幂，否则编译报错；`cdiv(4096, 64) = 64`，恰好整除、无越界。
- 改 `TILE`（如改成 `32` 或 `128`）会触发**重新编译**，因为 `TILE` 是 `Constant[int]`，被嵌入 cubin（见 u1-l2、u3-l5）。

**预期结果**：`c` 与 `a * alpha` 数值一致，断言通过。

**待本地验证**：本脚本依赖本地有支持 CUDA 的 torch 与已安装的 `cuda-tile[tileiras]`，需在本地实际运行确认；不同 `TILE`/`N` 组合（尤其 `N` 不被 `TILE` 整除时）的边界行为也建议本地验证。

**进阶尝试**（可选）：

- 让 `N = 4096 + 10`（不被 `TILE` 整除），观察最后一块部分越界的写入是否被正确忽略（参考 [test_copy.py 里 shape=(225,) 的用例](test/test_copy.py#L35)）。
- 把 `alpha` 改成由 `ct.Constant[float]` 传入，观察改变它是否会触发重新编译（提示：会，因为常量被嵌入）。

## 6. 本讲小结

- cuTile 内核遵循 **load–compute–store** 三段式：`ct.load` 把全局 `Array` 的一块搬成内核内的 `Tile`，对 tile 做集体计算，再 `ct.store` 写回 `Array`。
- `ct.bid(axis)` 返回当前 block 在 grid 第 `axis` 轴的坐标（`int32` 标量），是 tile code 里「定位自己」的唯一手段；它通常直接用作 `load`/`store` 的 `index`，让每个 block 处理不同的数据块。
- `ct.load(array, index, shape, ...)` 的 `index` 是 **tile space（瓦片）索引**，不是元素下标；`shape` 是编译期常量、每维为 2 的幂；部分越界按 `padding_mode` 填充，整体越界未定义。
- `ct.store(array, index, tile, ...)` 没有 `shape`（从 `tile` 推断），部分越界的写入被**忽略**，整体越界未定义；返回 `None`。
- host 端用 `ct.cdiv(N, TILE)` 向上取整算出 grid 的 block 数量，再用 `ct.launch(stream, grid, kernel, args)` 启动。
- `_stub.py` 里的 `ct.load`/`ct.store`/`ct.bid` 只有签名与文档，真正的实现由后端 IR 注册系统提供（`@stub` 打标记，见 u5-l7）。

## 7. 下一步学习建议

本讲只用了最朴素的逐元素 compute（`+`、`*`）。接下来建议：

- **u3-l2（tile 算术与形状操作）**：系统学习 tile 上的逐元素算术、`astype`、`reshape`/`permute`/`transpose`/`cat` 等，把 compute 部分写得更丰富（例如实现一个真正的 transpose 内核）。
- **u3-l3（控制流）**：学习 `if`/`for`/`while` 在 tile code 里的写法与 `range` 的 `step>0` 限制，为 persistent 内核做准备。
- **u4-l1（TiledView 与 persistent）**：当需要让一个 block 处理多块 tile 时，学习 `num_tiles`/`num_blocks` 与 persistent 循环。
- **源码延伸阅读**：想了解 `ct.load` 的 `index` 在底层如何变成具体的 IR 操作，可先看 [_stub.py:L1218-L1227](src/cuda/tile/_stub.py#L1218-L1227) 的签名，再在 u5-l7（stub 与实现注册）里追踪它如何分派到真正的 IR load 操作。
