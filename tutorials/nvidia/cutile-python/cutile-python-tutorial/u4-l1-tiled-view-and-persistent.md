# TiledView 与 persistent 遍历

## 1. 本讲目标

本讲承接 [u3-l1（load/store 与 load-compute-store 范式）](u3-l1-load-store-pattern.md)。在 u3-l1 里，我们用 `ct.load(array, index, shape)` 直接以「瓦片索引」定位一块数据——`index=(bidx, bidy)`、`shape=(tm, tn)`。这种写法把「数组被切成瓦片网格」这件事隐式地藏在每一次 `load`/`store` 调用里。

本讲要把这层隐含结构**显式化**为一个对象——`TiledView`，并围绕它解决四个问题：

1. **TiledView 是什么**：它如何把「一个数组 + 一种瓦片形状」凝固成一个可复用的「瓦片空间」视图，让我们用 `tv.load(i)` / `tv.store(i, tile)` 反复读写。
2. **num_tiles**：如何在内核**内部**查询「这个数组在某个轴上一共有多少块瓦片」，从而写出不依赖 host 传入循环上界的循环。
3. **traversal_steps**：如何让相邻瓦片**重叠**（滑动窗口 / 卷积）或**留间隔**（跨步采样）。
4. **num_blocks 与 persistent 内核循环**：当瓦片数远多于 GPU 的 SM 数时，如何用 `num_blocks` + `range` 让**每个 block 串行处理多块瓦片**，减少调度开销。

学完后你应当能够：

- 用 `Array.tiled_view(...)` 创建视图，并理解它与裸 `ct.load`/`ct.store` 的等价关系。
- 区分**元素空间（element space）**、**瓦片空间（tile space）**与**块空间（block space）**三个层面。
- 写出在内核内用 `num_tiles` / `num_blocks` 自洽推导循环范围的内核，而不是把范围从 host 硬塞进来。
- 把一个「一个 block 处理一块瓦片」的内核，改写成「少量 block 各处理多块瓦片」的 **persistent 内核**。

## 2. 前置知识

本讲默认你已经掌握以下内容（来自前置讲义）：

- **grid / block / 执行空间**（u2-l1）：一次 `ct.launch` 会启动组织成 1D/2D/3D 的 grid；`ct.bid(axis)` 是当前 block 在第 `axis` 轴的坐标；cuTile 只表达 block 级并行，不暴露单个线程。
- **全局数组 Array 与 strided 布局**（u2-l2）：数组放在全局显存，由 host 分配，`shape`/`strides` 是运行时 `int32`。
- **Tile 是不可变、编译期 shape、每维为 2 的幂**的多维集合（u2-l3）。
- **load-compute-store 范式**（u3-l1）：`ct.load(array, index, shape)` 的 `index` 是**瓦片索引**而非元素下标。
- **控制流子集**（u3-l3）：tile code 支持 `if/for/while`，`for` 目前只接受 `range`，且 `range` 的 step 必须 > 0。

补一个本讲会用到的术语直觉：

| 概念 | 是什么 | 在哪一层 |
| --- | --- | --- |
| element space | 数组元素本身构成的多维空间 | 数据 |
| tile space | 「用某种瓦片形状去切数组」得到瓦片网格 | 数据视图 |
| block space | `ct.launch` 时实际启动的 block 网格 | 执行 |

本讲的全部内容，本质上就是在讲这三层空间如何对应、以及当它们**不再一一对应**时（瓦片数 > block 数）该怎么编程。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/cuda/tile/_stub.py](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_stub.py) | 用户 API 的「签名侧」。定义了 `Array.tiled_view`、`TiledView` 类（`dtype`/`tile_shape`/`num_tiles`/`traversalsteps`/`load`/`store`）、自由函数 `num_tiles`、`num_blocks`、`bid`。这些都是 `@stub`，真正的 IR 实现由后端注册系统提供。 |
| [docs/source/data.rst](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/docs/source/data.rst) | 官方数据模型文档。其中的 *Element & Tile Space* 与 *Tiled Views* 两节是本讲概念的权威定义来源。 |
| [test/test_tiled_view.py](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/test/test_tiled_view.py) | `TiledView` 的行为测试。包含普通拷贝、`num_tiles` 校验、`traversal_steps` 滑动窗口、版本门控等大量可读用例，是本讲实践的依据。 |
| [samples/MatMul.py](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/samples/MatMul.py) | 同时给出 GEMM 的**非 persistent** 版本（`matmul_kernel`）与 **persistent** 版本（`persistent_matmul_kernel`），是讲解 persistent 循环的最佳真实样本。 |

## 4. 核心概念与源码讲解

### 4.1 元素空间、瓦片空间与 TiledView

#### 4.1.1 概念说明

在 u3-l1 中，`ct.load(A, index=(bidx, k), shape=(tm, tk))` 这一次调用其实同时携带了三件事：

- 「从数组 `A` 里取」——操作哪个数组；
- 「`shape=(tm, tk)`」——按多大的瓦片去切；
- 「`index=(bidx, k)`」——取第几块瓦片。

当我们只 load 一次时这样写很紧凑；但当同一个数组要在循环里被**反复** load 不同瓦片（例如 GEMM 沿 K 维循环累加），每次都要重复写 `shape=(tm, tk)`、重复算 `num_tiles`，既啰嗦又容易写错。

`TiledView` 就是把这「数组 + 瓦片形状 + 填充模式 + 步长」**凝固成一个对象**的抽象。文档里这样定义它：

> A *tiled view* represents the tile space of a global array.
> —— [docs/source/data.rst:159-185](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/docs/source/data.rst#L159-L185)

理解 TiledView 的关键，是先理解它所「代表」的那个**瓦片空间（tile space）**。

#### 4.1.2 核心流程

先把「元素空间」与「瓦片空间」这两个层面区分清楚（见 [docs/source/data.rst:130-155](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/docs/source/data.rst#L130-L155)）：

- **元素空间（element space）**：数组元素本身构成的多维空间。例如一个 `(12, 16)` 的数组，元素空间就是 12 行 × 16 列的真实数据。
- **瓦片空间（tile space）**：用某种瓦片形状去切这个数组后，得到的「瓦片网格」。例如用 `(2, 4)` 的瓦片去切 `(12, 16)`，瓦片空间就是 `6 × 4 = 24` 块瓦片。

瓦片索引 `(i, j)` 与元素下标的默认映射（无 `traversal_steps`、行优先）：

\[ \text{元素范围}_i = \big[\, i \cdot \text{tile\_shape}_i,\ (i+1) \cdot \text{tile\_shape}_i \,\big) \]

即第 `i` 块瓦片覆盖元素 `[i*ts : (i+1)*ts]`。以 `(12,16)` 数组 + `(2,4)` 瓦片为例：

```
元素空间 (12 x 16):
  瓦片(0,0) 瓦片(0,1) 瓦片(0,2) 瓦片(0,3)     <- 每块 2 行 4 列
  瓦片(1,0) 瓦片(1,1) 瓦片(1,2) 瓦片(1,3)
  ...
  瓦片(5,0) 瓦片(5,1) 瓦片(5,2) 瓦片(5,3)     <- 共 6 x 4 块

瓦片空间: shape (6, 4)，共 24 块瓦片
```

`TiledView` 的使用流程是「**先建视图，再反复按瓦片索引读写**」：

```text
1. host: 把数组 x 传进内核
2. tile code: tv = x.tiled_view(tile_shape)          # 建立瓦片空间视图
3. tile code: tile = tv.load((i, j))                 # 按瓦片索引取一块
4. tile code: ... 对 tile 做计算 ...
5. tile code: tv_out.store((i, j), result)           # 按瓦片索引写回
```

它与 u3-l1 的裸 `ct.load`/`ct.store` **完全等价**，只是把 `shape` 从每次调用里提了出来、绑在视图上。

#### 4.1.3 源码精读

**① 建立视图的工厂方法** `Array.tiled_view`：

[src/cuda/tile/_stub.py:236-294](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_stub.py#L236-L294) —— 这是 `Array` 上的一个 `@stub` 方法，签名是：

```python
def tiled_view(self, tile_shape: Constant[Shape], *,
               padding_mode: PaddingMode = PaddingMode.UNDETERMINED,
               traversal_steps: Optional[Constant[Shape]] = None) -> "TiledView":
```

读签名要注意三件事：

- `tile_shape` 是 `Constant[Shape]`，即**编译期常量**——这与 u2-l3「tile 每维必须是编译期已知的 2 的幂」一致。
- `tile_shape` 的秩（维数）必须等于数组的秩，否则报 `TileTypeError`（见 4.1.4 的测试）。
- `traversal_steps` 默认 `None`，此时等价于「相邻瓦片严丝合缝」；4.3 节会专门讲它。

**② `TiledView` 类本身**：

[src/cuda/tile/_stub.py:762-805](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_stub.py#L762-L805) —— 暴露了三个编译期只读属性：

| 成员 | 类型 | 含义 |
| --- | --- | --- |
| `dtype` | `DType`（常量） | 视图里元素的类型，等于数组的 dtype |
| `tile_shape` | `tuple[const int, ...]` | 每次读写产出的 tile 形状 |
| `traversalsteps` | `tuple[const int, ...]` | 相邻瓦片原点之间相隔的元素数（默认 = `tile_shape`） |

**③ 按瓦片索引读写的 `load` / `store`**：

[src/cuda/tile/_stub.py:807-889](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_stub.py#L807-L889) ——

- `tv.load(index)`：`index` 是**瓦片空间**里的索引，返回的 tile 形状恒为 `tile_shape`；部分越界按视图的 `padding_mode` 填充，**整体**越界未定义。
- `tv.store(index, tile)`：`tile` 的形状须能广播到 `tile_shape`；部分越界写入被忽略，整体越界未定义。

注意：这两个方法的 `index` 是**瓦片索引**，不是元素下标——这正是「瓦片空间」与「元素空间」分层的体现。

#### 4.1.4 代码实践

**实践目标**：用 `TiledView` 写一个 2D 拷贝内核，并验证它与 `ct.load`/`ct.store` 等价。

**操作步骤**（参考 [test/test_tiled_view.py:59-86](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/test/test_tiled_view.py#L59-L86) 的 `test_tiled_view_copy_2d`）：

1. 准备一个 `(192, 134)` 的 `float32` torch 张量 `x`（非 tile 整数倍，故意制造部分越界）。
2. 写内核：对每个 block，用 `x.tiled_view((TILE_M, TILE_N))` 建视图，`tv_y.store((bidm, bidn), tv_x.load((bidm, bidn)))`。
3. host 端 grid 用 `ct.cdiv` 算：`grid = (cdiv(192, TILE_M), cdiv(134, TILE_N))`。

**需要观察的现象**：

- 内核内 `tv_x.tile_shape` 应等于 `(TILE_M, TILE_N)`，`tv_x.dtype` 应等于 `x.dtype`（参考 `check_tiled_view_properties`，[test/test_tiled_view.py:28-31](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/test/test_tiled_view.py#L28-L31)）。
- 把 `tile_shape` 写错秩（如对 1D 数组传 `(1,2)`）会抛 `TileTypeError: Expected shape length to be 1, got 2`（参考 [test/test_tiled_view.py:122-130](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/test/test_tiled_view.py#L122-L130) 的 `test_tiled_view_rank_mismatch`）。

**预期结果**：输出 `y` 与输入 `x` 逐元素相等。**待本地验证**（本讲不在沙箱里执行 GPU 内核）。

#### 4.1.5 小练习与答案

**练习 1**：一个 `(100,)` 的 1D 数组用 `tiled_view(30)` 建视图，瓦片空间有几块瓦片？最后一块瓦片覆盖哪些元素？

**答案**：`cdiv(100, 30) = 4` 块。前三块各覆盖 `[0:30)`、`[30:60)`、`[60:90)`；第 4 块（索引 3）覆盖 `[90:120)`，其中 `[100:120)` 越界、按 `padding_mode` 填充。

**练习 2**：为什么 `tile_shape` 必须是 `Constant`，而数组的 `shape` 是运行时 `int32`？

**答案**：`tile_shape` 决定**编译期生成的 tile 类型**（tile 每维须为 2 的幂、编译期已知，见 u2-l3），故必须是常量；而数组 `shape` 是 host 传入的运行时值，cuTile 用 `int32` 存以提升性能（见 u2-l2），二者处在不同层面。

---

### 4.2 num_tiles：在内核内查询瓦片数

#### 4.2.1 概念说明

u3-l1 的 vector_add 里，循环上界 `num_tiles_k` 这种值是 host 算好、作为 `Constant` 传进来的。但很多时候我们希望**内核自己知道「沿某个轴一共有多少块瓦片」**，从而：

- 不必把循环上界从 host 硬塞进来（减少 `Constant` 参数、减少 JIT 特化）；
- 让内核对不同的数组 shape 自洽。

cuTile 提供了两种查询瓦片空间尺寸的入口，都叫 `num_tiles`：

1. **自由函数** `ct.num_tiles(array, axis, shape)`：在不建 `TiledView` 的情况下，临时问一句「如果用 `shape` 去切 `array`，沿 `axis` 有几块？」。
2. **方法** `tv.num_tiles(axis)`：在已经建好的 `TiledView` 上查询它某个轴的瓦片数。

#### 4.2.2 核心流程

无论哪种入口，`num_tiles` 的语义都是向上取整除法：

\[ \text{num\_tiles}(\text{axis}) = \left\lceil \frac{\text{shape}[\text{axis}]}{\text{traversal\_steps}[\text{axis}]} \right\rceil \]

默认 `traversal_steps == tile_shape`，所以最常见的形式退化为：

\[ \text{num\_tiles}(\text{axis}) = \left\lceil \frac{\text{shape}[\text{axis}]}{\text{tile\_shape}[\text{axis}]} \right\rceil = \text{ct.cdiv}(\text{shape}[\text{axis}],\ \text{tile\_shape}[\text{axis}]) \]

**注意**：`num_tiles` 计入那些**部分越界**的瓦片（它们会被 `padding_mode` 填充）。例如 `(100,)` 数组 + `tile_shape=30`：`num_tiles = cdiv(100,30) = 4`，第 4 块部分越界但仍算一块。

典型用法流程：

```text
方式 A（自由函数，临时查询）:
  num_tiles_k = ct.num_tiles(A, axis=1, shape=(tm, tk))   # 沿 K 轴的瓦片数

方式 B（方法，已有视图）:
  tv = x.tiled_view((tm, tn))
  for i in range(tv.num_tiles(0)):                        # 用方法做循环上界
      ...
```

#### 4.2.3 源码精读

**① 自由函数 `ct.num_tiles`**：

[src/cuda/tile/_stub.py:1177-1211](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_stub.py#L1177-L1211) —— 签名：

```python
def num_tiles(array: Array, /, axis: int,
              shape: Constant[Shape],
              order: Constant[Order] = "C") -> int:
```

文档字符串里给了一个直观例子：对 `(42, 64)` 的数组用 `shape=(4, 8)` 切，`num_tiles(x, 0, ...)` 返回 `cdiv(42,4)=11`，`num_tiles(x, 1, ...)` 返回 `cdiv(64,8)=8`，即「11 行 × 8 列瓦片」。`order` 参数控制轴映射顺序（与 `load` 的 `order` 一致）。

**② 方法 `TiledView.num_tiles`**：

[src/cuda/tile/_stub.py:783-793](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_stub.py#L783-L793) —— 在已建好的视图上，只需给 `axis`（因为 `tile_shape` 已绑在视图上），返回 `int32`。

**③ 真实用法：GEMM 的 K 维循环上界**：

[samples/MatMul.py:66](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/samples/MatMul.py#L66) —— `matmul_kernel` 里这样算 K 维瓦片数：

```python
num_tiles_k = ct.num_tiles(A, axis=1, shape=(tm, tk))
```

随后 `for k in range(num_tiles_k):` 沿 K 维循环累加（[samples/MatMul.py:80](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/samples/MatMul.py#L80)）。这里用自由函数而不是方法，是因为 A、B 两个数组各自要按不同瓦片形状（`(tm,tk)` 与 `(tk,tn)`）切，建两个视图不如直接问一句方便。

**④ 测试中的方法用法**：

[test/test_tiled_view.py:71-74](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/test/test_tiled_view.py#L71-L74) —— `test_tiled_view_copy_2d` 里用方法查询并把结果写回一个标量数组，与 host 端 `cdiv` 的参考值比对：

```python
nt1, nt2 = tv_x.num_tiles(0), tv_x.num_tiles(1)
```

host 端参考值为 `[ct.cdiv(shape[0], tile_size[0]), ct.cdiv(shape[1], tile_size[1])]`（[test/test_tiled_view.py:79-81](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/test/test_tiled_view.py#L79-L81)），这正是上面公式的直接印证。

#### 4.2.4 代码实践

**实践目标**：用 `num_tiles` 让一个 1D 拷贝内核**完全自洽**——host 只传数组和瓦片大小，循环上界由内核自己推导。

**操作步骤**（参考 [test/test_tiled_view.py:212-229](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/test/test_tiled_view.py#L212-L229) 的 `test_tiled_view_helper_func`）：

```python
# 示例代码：基于 test_tiled_view_helper_func 改写
@ct.kernel
def kernel(x, y, TILE: ConstInt):
    tv_x = x.tiled_view(TILE)
    tv_y = y.tiled_view(TILE)
    for i in range(tv_x.num_tiles(0)):     # 循环上界来自内核内查询
        tv_y.store(i, tv_x.load(i))
```

启动时只用 **1 个 block**（`grid=(1,)`），让这唯一一个 block 串行遍历所有瓦片。

**需要观察的现象**：

- 对 `shape=(128,)`、`TILE=64`，循环应跑 2 次（`cdiv(128,64)=2`）。
- 即便 host 不把「2」作为参数传入，内核也能正确拷贝全部数据。

**预期结果**：`y` 与 `x` 逐元素相等。**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：对 `(42, 64)` 的数组，`ct.num_tiles(x, 0, shape=(4, 8))` 与 `ct.num_tiles(x, 1, shape=(4, 8))` 各返回多少？

**答案**：分别是 `cdiv(42,4)=11` 与 `cdiv(64,8)=8`（与源码 docstring 的 testoutput 一致，[src/cuda/tile/_stub.py:1209-1210](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_stub.py#L1209-L1210)）。

**练习 2**：既然 `num_tiles == cdiv(shape, tile_shape)`，为什么不直接在内核里写 `ct.cdiv(x.shape[0], TILE)` 而要用 `num_tiles`？

**答案**：两者数值相等，但 `num_tiles` 把「按某种瓦片形状切」的语义显式表达出来，并且在引入 `traversal_steps`（4.3 节）与 `order` 后，分母会变成 `traversal_steps` 而非 `tile_shape`，此时只有 `num_tiles` 能给出正确值。另外 `num_tiles` 直接对应瓦片空间概念，可读性更好。

---

### 4.3 traversal_steps：重叠与间隔的滑动窗口

#### 4.3.1 概念说明

到目前为止，相邻瓦片都是「严丝合缝」的：第 `i` 块覆盖 `[i*ts:(i+1)*ts]`，第 `i+1` 块紧接着从 `(i+1)*ts` 开始。但有两类常见场景需要打破这种紧邻：

- **重叠瓦片（overlap）**：卷积、滑动窗口、stencil 计算——相邻窗口共享一部分元素。需要 `traversal_steps < tile_shape`。
- **间隔瓦片（gaps / strided）**：跨步采样、下采样——跳过一部分元素。需要 `traversal_steps > tile_shape`。

`tiled_view` 的 `traversal_steps` 参数就是控制「相邻瓦片原点之间相隔多少个元素」。文档原文（[docs/source/data.rst:170-174](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/docs/source/data.rst#L170-L174)）：

> Specifying `traversal_steps` to `Array.tiled_view` changes the advance per step to `traversal_steps[i]`, producing overlapping tiles when `traversal_steps[i] < tile_shape[i]` or gapped tiles when `traversal_steps[i] > tile_shape[i]`.

> ⚠️ `traversal_steps` 是 **CTK 13.3（tileiras `V_13_3`）** 才支持的特性，旧版本会抛 `TileUnsupportedFeatureError`（见 4.3.4）。

#### 4.3.2 核心流程

引入 `traversal_steps` 后，瓦片索引 `i` 与元素范围的映射变为：

\[ \text{元素范围}(i) = \big[\, i \cdot \text{step},\ \min(i \cdot \text{step} + \text{tile\_shape},\ \text{shape}) \,\big) \]

超出 `shape` 的部分按 `padding_mode` 填充。三种情形对比（1D，`shape=16`，`tile_shape=4`）：

| `traversal_steps` | 相邻瓦片关系 | 瓦片索引 0..3 覆盖的元素 | `num_tiles` |
| --- | --- | --- | --- |
| `=4`（默认） | 紧邻无重叠 | `[0:4] [4:8] [8:12] [12:16]` | `cdiv(16,4)=4` |
| `=2`（< tile） | **重叠** 2 个元素 | `[0:4] [2:6] [4:8] [6:10] …` | `cdiv(16,2)=8` |
| `=8`（> tile） | **间隔** 4 个元素 | `[0:4] [8:12]`（跳过 `[4:8]`） | `cdiv(16,8)=2` |

注意：`num_tiles` 的分母变成了 `traversal_steps`，而不是 `tile_shape`——这是 4.2 节公式里分母写成 `traversal_steps` 的原因。

伪代码（滑动窗口拷贝）：

```text
tv     = x.tiled_view(TILE, traversal_steps=STEP)
tv_out = out.tiled_view(TILE, traversal_steps=STEP)
for i in range(tv.num_tiles(0)):           # = cdiv(N, STEP)
    tv_out.store(i, tv.load(i))            # 每块瓦片从 i*STEP 开始
```

#### 4.3.3 源码精读

**① `tiled_view` 的 `traversal_steps` 形参**：

[src/cuda/tile/_stub.py:251-262](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_stub.py#L251-L262) —— docstring 明确给出三种取值的语义，并标注 `(Since CTK 13.3)`。

**② `TiledView.traversalsteps` 属性**：

[src/cuda/tile/_stub.py:795-805](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_stub.py#L795-L805) —— 返回实际生效的步长，默认等于 `tile_shape`；当 `tile_shape == ()`（零维 tile / 标量视图）时，`traversalsteps` 被广播为 `(1,) * 秩`。

**③ 滑动窗口测试**：

[test/test_tiled_view.py:290-304](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/test/test_tiled_view.py#L290-L304) —— `test_tiled_view_traversal_steps_sliding_window` 用三组参数覆盖三种情形：

```python
@pytest.mark.parametrize("tile_size,step,n", [
    (4, 2, 8),   # traversal_steps < tile_shape: overlapping tiles
    (4, 8, 16),  # traversal_steps > tile_shape: strided tiles with gaps
    (4, 3, 12),  # traversal_steps is not a power of two
])
```

注意第三组 `(4, 3, 12)`：`traversal_steps` **不要求是 2 的幂**（只有 `tile_shape` 要求是 2 的幂）。host 端参考实现用纯 Python 复现滑动窗口语义（[test/test_tiled_view.py:301-303](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/test/test_tiled_view.py#L301-L303)）：

```python
for start in range(0, n, step):
    ref[start:start + tile_size] = x[start:start + tile_size]
```

这正是「瓦片 `i` 从 `i*step` 开始」的可执行定义。

**④ `num_tiles` 与 `traversal_steps` 的关系测试**：

[test/test_tiled_view.py:336-352](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/test/test_tiled_view.py#L336-L352) —— `test_tiled_view_traversal_steps_num_tiles`：`N=16, TILE=4, STEP=2`，断言 `tv.num_tiles(0) == ct.cdiv(16, 2) == 8`，直接验证「分母是 step 而非 tile」。

#### 4.3.4 代码实践

**实践目标**：用 `traversal_steps` 实现一个 2D box-filter（盒滤波）的滑动窗口，体会「重叠瓦片」。

**操作步骤**（参考 [test/test_tiled_view.py:307-333](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/test/test_tiled_view.py#L307-L333) 的 `test_tiled_view_2d_conv_no_padding`）：

```python
# 示例代码：2D box-filter，窗口 (KH,KW)，步长 (SH,SW)
@ct.kernel
def kernel(x, out, KH: ConstInt, KW: ConstInt, SH: ConstInt, SW: ConstInt,
           OUT_H: ConstInt, OUT_W: ConstInt):
    tv = x.tiled_view((KH, KW), traversal_steps=(SH, SW))   # 重叠窗口
    out_tv = out.tiled_view(())
    for i in range(OUT_H):
        for j in range(OUT_W):
            tile = tv.load((i, j))                           # 取 (KH,KW) 窗口
            out_tv.store(i * OUT_W + j, ct.sum(tile))        # 窗口求和 -> 标量
```

host 端：`H=W=6`、`KH=KW=2`、`SH=SW=1`，输出 `out_h = (H-KH)//SH + 1 = 5` 个有效窗口。

**需要观察的现象**：

- 这里循环上界用的是 host 传入的 `OUT_H/OUT_W`（**有效**窗口数 = `shape - tile + 1`），**不是** `tv.num_tiles(0)`（= `cdiv(6,1)=6`，含一个部分越界窗口）。体会两者的差别：`num_tiles` 数「步长能迈几次」，而卷积的「有效」窗口数还要扣掉窗口自身宽度。
- 若把 `traversal_steps` 改成 `None`，瓦片不再重叠，结果就不再是卷积。

**预期结果**：`out` 与 `x.unfold(0,KH,SH).unfold(1,KW,SW).sum(dim=(-2,-1)).flatten()` 一致。**待本地验证**。

> **版本门控**：若你的环境 tileiras 版本低于 13.3，本实践的内核会在编译期抛 `TileUnsupportedFeatureError: traversal_steps requires tileiras 13.3`。对应的回归测试在 [test/test_tiled_view.py:381-399](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/test/test_tiled_view.py#L381-L399)（`test_tiled_view_traversal_steps_version_error`）。此外，`traversal_steps` 的秩必须等于数组秩、且每维必须为正，否则抛 `TileTypeError`（见 [test/test_tiled_view.py:408-437](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/test/test_tiled_view.py#L408-L437)）。

#### 4.3.5 小练习与答案

**练习 1**：`shape=16`、`tile_shape=4`、`traversal_steps=2` 时，瓦片索引 0、1、2 各覆盖哪些元素？一共有几块瓦片？

**答案**：索引 0→`[0:4]`、1→`[2:6]`、2→`[4:8]`，相邻瓦片重叠 2 个元素；共 `cdiv(16,2)=8` 块（最后一块索引 7→`[14:18)`，`[16:18)` 越界按 padding 填充）。

**练习 2**：`traversal_steps` 是否必须为 2 的幂？

**答案**：不必。只有 `tile_shape` 必须每维为 2 的幂（u2-l3）。`traversal_steps` 可以是任意正整数，例如 `(4, 3, 12)` 这组测试里 `step=3` 完全合法（[test/test_tiled_view.py:285-289](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/test/test_tiled_view.py#L285-L289)）。

---

### 4.4 num_blocks 与 persistent 内核循环

#### 4.4.1 概念说明

到目前为止，我们的内核都是「**一个 block 处理一块瓦片**」：host 启动的 block 数 = 瓦片总数。对于小内核这没问题，但当输出瓦片数很大时（比如大 GEMM 有成千上万块输出瓦片），会启动成千上万个 block，带来可观的 **调度 / 启动开销**，且 block 之间的负载可能不均。

**persistent kernel（持久内核）**的思路是反过来的：

- 只启动**与 SM 数量相当**的少量 block（让 GPU 一次铺满）；
- 每个 block 在一个**循环**里串行处理**多块**输出瓦片。

这样把「瓦片数」与「block 数」解耦：瓦片空间可以很大，但 block 空间固定为 SM 数。

要写 persistent 内核，需要两个新工具：

- **`ct.num_blocks(axis)`**：在内核内查询「本次启动沿 `axis` 一共有几个 block」——也就是 host 传给 `ct.launch` 的 grid 尺寸。这是**块空间**的尺寸，区别于「瓦片空间」的 `num_tiles`。
- 一个步长为 `num_blocks` 的 `range` 循环，让 block `bid` 认领瓦片 `bid, bid+num_blocks, bid+2*num_blocks, …`。

#### 4.4.2 核心流程

persistent 循环的标准惯用法（来自 [samples/MatMul.py:148-149](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/samples/MatMul.py#L148-L149)）：

```text
bid            = ct.bid(0)                      # 我是第几个 block
upper_bound    = num_bid_m * num_bid_n          # 输出瓦片总数
num_tile_blocks = ct.num_blocks(0)              # 一共启动了多少个 block
for current_bid in range(bid, upper_bound, num_tile_blocks):
    # 处理第 current_bid 块输出瓦片
```

`range(start, stop, step)` 的语义是：`current_bid` 依次取 `bid, bid+num_blocks, bid+2*num_blocks, …`，直到 `>= upper_bound`。因此：

- block 0 处理瓦片 `0, B, 2B, …`；
- block 1 处理瓦片 `1, B+1, 2B+1, …`；
- ……
- block `B-1` 处理瓦片 `B-1, 2B-1, …`。

（其中 `B = num_blocks`。）这是一种**循环切块（round-robin / cyclic）**的瓦片→block 映射。

host 侧配套（[samples/MatMul.py:233-237](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/samples/MatMul.py#L233-L237)）：

```text
grid_size = num_bid_m * num_bid_n               # 输出瓦片总数
if persistent:
    NUM_SMS  = 设备的 multi_processor_count
    grid_size = min(NUM_SMS, grid_size)         # 不超过 SM 数
grid = (grid_size, 1, 1)
```

注意三个空间的最终关系：

| 空间 | persistent 时的大小 | 关系 |
| --- | --- | --- |
| 瓦片空间（输出） | `num_bid_m * num_bid_n` | 由数组 shape 与 tile_shape 决定 |
| 块空间（grid） | `min(NUM_SMS, 瓦片数)` | host 决定，≤ SM 数 |
| 每 block 处理瓦片数 | `cdiv(瓦片数, 块数)` | 由循环自动消化 |

> **与 u3-l3 的衔接**：`range(bid, upper_bound, num_tile_blocks)` 是三参数 `range`，step = `num_tile_blocks`。u3-l3 讲过 `range` 的 step 必须 > 0；这里 `num_blocks ≥ 1` 恒成立，故合法。step 是运行时 `int32`（来自 `num_blocks`），不是编译期常量——cuTile 允许运行时 step，只要它在运行时为正。

#### 4.4.3 源码精读

**① `ct.num_blocks` 与 `ct.bid`**：

[src/cuda/tile/_stub.py:1142-1174](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_stub.py#L1142-L1174) —— `num_blocks(axis)` 返回本次 launch 沿 `axis` 的 block 数，取值就是 host 传给 `ct.launch` 的 `grid`。docstring 的例子：`ct.launch(stream, (2,3,4), kernel, ())` 时，`num_blocks(0/1/2)` 分别返回 `2/3/4`。

[src/cuda/tile/_stub.py:1116-1139](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_stub.py#L1116-L1139) —— `bid(axis)` 返回当前 block 的坐标。两者合起来给出「我在第几个 block / 一共有几个 block」。

> 区分 `num_blocks`（块空间）与 `num_tiles`（瓦片空间）：非 persistent 内核里它们常常相等（grid = 瓦片数），但 persistent 内核里 `num_blocks < num_tiles`，**正是这个差让循环有了意义**。

**② 非 persistent GEMM**（对照基准）：

[samples/MatMul.py:33-101](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/samples/MatMul.py#L33-L101) —— `matmul_kernel` 里，每个 block 只算**一块**输出瓦片：

```python
bidx, bidy = swizzle_2d(M, N, tm, tn, GROUP_SIZE_M)   # 把 1D bid 映射到 2D 瓦片
num_tiles_k = ct.num_tiles(A, axis=1, shape=(tm, tk))
accumulator = ct.full((tm, tn), 0, dtype=ct.float32)
for k in range(num_tiles_k):                           # 仅沿 K 维循环
    a = ct.load(A, index=(bidx, k), shape=(tm, tk), ...).astype(dtype)
    b = ct.load(B, index=(k, bidy), shape=(tk, tn), ...).astype(dtype)
    accumulator = ct.mma(a, b, accumulator)
ct.store(C, index=(bidx, bidy), tile=accumulator.astype(C.dtype))
```

host 侧 `grid_size = grid_x * grid_y`（输出瓦片总数，[samples/MatMul.py:230-232](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/samples/MatMul.py#L230-L232)），即「一个 block 一块瓦片」。

**③ persistent GEMM**（目标形态）：

[samples/MatMul.py:104-176](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/samples/MatMul.py#L104-L176) —— `persistent_matmul_kernel` 的关键差异在前 10 行：

```python
bid = ct.bid(0)
M, N = A.shape[0], B.shape[1]
num_tiles_k = ct.num_tiles(A, axis=1, shape=(tm, tk))
...
num_bid_m = ct.cdiv(M, tm)
num_bid_n = ct.cdiv(N, tn)
upper_bound = num_bid_m * num_bid_n            # 输出瓦片总数
num_tile_blocks = ct.num_blocks(0)             # 启动的 block 数
for current_bid in range(bid, upper_bound, num_tile_blocks):
    accumulator = ct.full((tm, tn), 0, dtype=ct.float32)
    bidx, bidy = swizzle_2d_from_bid(M, N, tm, tn, GROUP_SIZE_M, current_bid)
    for k in range(num_tiles_k):
        ...                                    # 与非 persistent 版完全相同的 K 维累加
    ct.store(C, index=(bidx, bidy), tile=accumulator.astype(C.dtype))
```

也就是说：**K 维的内层循环一字未改**，只是外面**套了一层「认领多块输出瓦片」的循环**，并把原来的 `bidx, bidy = swizzle_2d(...)`（用 `ct.bid(0)`）改成 `swizzle_2d_from_bid(..., current_bid)`（用循环变量 `current_bid`）。

host 侧把 grid 压到 SM 数（[samples/MatMul.py:233-237](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/samples/MatMul.py#L233-L237)）：

```python
if persistent:
    NUM_SMS = torch.cuda.get_device_properties("cuda").multi_processor_count
    grid_size = min(NUM_SMS, grid_size)
```

并按开关选用内核（[samples/MatMul.py:248](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/samples/MatMul.py#L248)）：`kernel = persistent_matmul_kernel if persistent else matmul_kernel`。

> **不要混淆**：[docs/source/performance.rst:107](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/docs/source/performance.rst#L107) 里提到的 `nvidia-smi -pm 1`「persistent mode」是**GPU 硬件层的持久化模式**（让驱动常驻、避免初始化开销），与本讲的 **persistent kernel**（内核层、block 复用处理多瓦片）是完全不同的两个概念，仅是同名。

#### 4.4.4 代码实践

**实践目标**：在不看 `persistent_matmul_kernel` 的前提下，亲手把非 persistent 的 `matmul_kernel` 改写成 persistent 版，再与样本对照。

**操作步骤**：

1. 复制 [samples/MatMul.py:33-101](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/samples/MatMul.py#L33-L101) 的 `matmul_kernel`，重命名为 `my_persistent_matmul`。
2. 把函数体里 `bidx, bidy = swizzle_2d(M, N, tm, tn, GROUP_SIZE_M)` 这一行**连同它之后到 `ct.store` 为止的整段**，包进一个 persistent 循环：
   - 在循环前算 `num_bid_m = ct.cdiv(M, tm)`、`num_bid_n = ct.cdiv(N, tn)`、`upper_bound = num_bid_m * num_bid_n`、`num_tile_blocks = ct.num_blocks(0)`。
   - 用 `for current_bid in range(bid, upper_bound, num_tile_blocks):` 包住那段代码。
   - 把段内的 `swizzle_2d(...)` 改成 `swizzle_2d_from_bid(M, N, tm, tn, GROUP_SIZE_M, current_bid)`（`bid` 已经在循环外取过，见 [samples/MatMul.py:128](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/samples/MatMul.py#L128)）。
3. host 侧（参考 `cutile_matmul`）按 `persistent=True` 分支把 grid 压到 `NUM_SMS`。

**需要观察的现象**：

- 改写后的内核与样本里的 `persistent_matmul_kernel` 应**逐行等价**——这正好用来检验你是否理解了那几步变换。
- 用 `python samples/MatMul.py --correctness-check` 跑 Test Case 4，应打印 `Correctness check passed`。

**预期结果**：persistent 版的输出与非 persistent 版、与 `A @ B` 数值一致（容差同 u3-l6 的 tf32 设置）。**待本地验证**（需要 GPU 与 tileiras）。

> 进阶观察：用 Nsight Compute 或 `ct.tune`（见 [u8-l3](u8-l3-autotuning.md)）对比 persistent 与非 persistent 版的执行时间。当输出瓦片数远大于 SM 数时，persistent 版通常因调度开销减少而更快；当瓦片数 ≤ SM 数时两者基本无差（`min(NUM_SMS, grid_size)` 退化为 `grid_size`）。

#### 4.4.5 小练习与答案

**练习 1**：persistent 内核里，若 host 启动了 `B = num_blocks(0)` 个 block、输出瓦片总数为 `T`，那么 block `bid` 会处理哪几块瓦片？每个 block 最多处理几块？

**答案**：block `bid` 处理瓦片 `bid, bid+B, bid+2B, …`（cyclic 映射）。每个 block 最多处理 `cdiv(T, B)` 块（当 `T` 不被 `B` 整除时，前 `T mod B` 个 block 会多处理一块）。

**练习 2**：为什么 persistent 内核要把 grid 压到 `min(NUM_SMS, T)` 而不是随便取一个比 `T` 小的数？

**答案**：取 `NUM_SMS` 是为了「**正好铺满 GPU 的所有 SM**」——少于 SM 数会有 SM 空闲浪费；多于 SM 数则多余的 block 仍要排队复用 SM，并不能真正并行，反而增加调度开销。因此 `NUM_SMS` 是「块数足够并行、又不过度排队」的天然上限（当 `T < NUM_SMS` 时显然不能启动比瓦片还多的 block，故取 `min`）。

**练习 3**：在 persistent 内核中，`num_blocks(0)` 是编译期常量还是运行时值？为什么这很重要？

**答案**：运行时 `int32`——它等于 host 传入的 grid 尺寸，每次 launch 可能不同。正因为它不是编译期常量，persistent 循环的步长与迭代次数**不会被烘焙进 cubin**，同一份 cubin 可以服务不同的 grid 大小（只要瓦片形状等编译期参数不变）。这与 `Constant` 参数（会触发重新编译，见 u3-l5）形成对照。

---

## 5. 综合实践

把本讲四个模块串起来，完成一个 **persistent + 滑动窗口** 的小内核。

**任务**：实现一个 1D **滑动窗口拷贝 + persistent 调度**的内核 `sliding_copy_persistent`：

- 输入 `x` 形状 `(N,)`，输出 `out` 形状 `(N,)`。
- 窗口 `tile_shape = TILE`、`traversal_steps = STEP`（`STEP < TILE`，相邻窗口重叠）。
- 用 **persistent** 方式：host 只启动 `min(NUM_SMS, num_tiles)` 个 block，每个 block 在循环里认领多个窗口，把每个窗口原样拷到 `out` 的对应位置。

**要求把本讲四个模块都用上**：

1. 用 `x.tiled_view(TILE, traversal_steps=STEP)` 与 `out.tiled_view(TILE, traversal_steps=STEP)` 建立**滑动窗口视图**（模块 4.1 + 4.3）。
2. 用 `tv.num_tiles(0)` 推导**瓦片总数**作为循环上界的一部分（模块 4.2）。
3. 用 `ct.num_blocks(0)` + `range(bid, upper_bound, num_blocks)` 写 **persistent 循环**（模块 4.4）。

**参考骨架**（示例代码，需自行补全 host 侧）：

```python
# 示例代码：persistent + 滑动窗口拷贝
@ct.kernel
def sliding_copy_persistent(x, out, TILE: ConstInt, STEP: ConstInt):
    bid = ct.bid(0)
    tv_x   = x.tiled_view(TILE, traversal_steps=STEP)
    tv_out = out.tiled_view(TILE, traversal_steps=STEP)
    upper_bound = tv_x.num_tiles(0)            # = cdiv(N, STEP)
    num_tile_blocks = ct.num_blocks(0)
    for i in range(bid, upper_bound, num_tile_blocks):
        tv_out.store(i, tv_x.load(i))
```

**host 侧要点**：

- `num_tiles_host = cdiv(N, STEP)`；`grid_size = min(NUM_SMS, num_tiles_host)`；`grid = (grid_size,)`。
- 参考输出可仿照 [test/test_tiled_view.py:301-303](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/test/test_tiled_view.py#L301-L303) 用纯 Python 的 `for start in range(0, N, STEP): ref[start:start+TILE] = x[start:start+TILE]` 生成。

**验证**：

- 数值：`out` 与参考实现逐元素相等（注意越界部分的 padding 不参与比较）。
- 行为：把 `grid_size` 从 `NUM_SMS` 改成 `1`，结果应**完全不变**（因为循环会自动让这 1 个 block 处理所有窗口）——这验证了「瓦片数与 block 数解耦」。

**预期结果**：拷贝结果与参考一致；改 grid 大小不影响正确性。**待本地验证**（需 tileiras ≥ 13.3 以支持 `traversal_steps`）。

## 6. 本讲小结

- **TiledView** 把「数组 + 瓦片形状 + 填充 + 步长」凝固成一个对象，`tv.load(i)` / `tv.store(i, tile)` 与裸 `ct.load`/`ct.store` 等价，但更适于在循环里反复读写同一数组的瓦片。
- 关键是分清三个层面：**元素空间**（真实数据）、**瓦片空间**（`num_tiles`，由 `cdiv(shape, traversal_steps)` 决定）、**块空间**（`num_blocks`，由 host 的 grid 决定）。
- **`num_tiles`** 让内核**自己**推导循环上界，避免把循环范围硬塞成 `Constant`；分母是 `traversal_steps` 而非 `tile_shape`。
- **`traversal_steps`** 让相邻瓦片**重叠**（`< tile_shape`，卷积/stencil）或**留间隔**（`> tile_shape`，跨步采样），是 CTK 13.3 引入的特性。
- **persistent 内核**用 `num_blocks` + `range(bid, upper_bound, num_blocks)` 把「瓦片数」与「block 数」解耦：host 只启动 SM 数量级个 block，每个 block 串行认领多块瓦片，减少调度开销。
- `samples/MatMul.py` 同时给出 GEMM 的非 persistent 与 persistent 两版，两者仅差一层「认领多块输出瓦片」的外层循环，是理解 persistent 模式的最佳真实样本。

## 7. 下一步学习建议

- **下一讲 [u4-l2](u4-l2-gather-scatter-advanced-indexing.md)**：把瓦片按**任意索引**读写——`gather`/`scatter` 与 `load_advanced_indexing`，从「规则瓦片网格」走向「不规则访存」。
- **[u4-l3](u4-l3-memory-model-and-atomics.md)**：当多个 block 处理同一输出（如直方图）时，会需要原子操作与内存序——persistent 内核让这种竞争更激烈，理解内存模型很重要。
- **回到源码**：本讲的 `@stub`（`tiled_view`/`num_tiles`/`num_blocks`）都只是签名，真正的 IR 实现见 [src/cuda/tile/_ir/ops.py](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_ir/ops.py) 与注册系统（[u5-l7](u5-l7-stub-and-impl-registry.md)）；想理解 `traversal_steps` 如何被编译进 TMA load，可继续读后端字节码生成（[u7-l1](u7-l1-ir-to-bytecode.md)）。
- **调优实践**：写好 persistent 内核后，用 [u8-l3 的 `ct.tune.exhaustive_search`](u8-l3-autotuning.md) 在若干 `(tm, tn, tk)` 组合里搜最优 tile 尺寸。
