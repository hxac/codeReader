# 第一个内核：vector_add 逐行精读

## 1. 本讲目标

本讲以 Tilus 仓库里最小、最完整的示例 `examples/vector_add` 为对象，逐行精读一个真实内核。学完本讲，你应当能够：

- 说出 `tilus.Script` 的 `__init__` / `__call__` 双方法骨架，并区分二者各自负责什么。
- 用 `global_view` 把裸指针包装成全局张量视图，用 `load_global` / `store_global` 在全局内存与寄存器张量之间搬运数据。
- 理解 `self.attrs.blocks` 与 `self.attrs.warps` 如何决定内核的网格（grid）大小与每个线程块的线程数。
- 独立把 `vector_add` 改写成一个 `scale` 内核（`c = a * 2.0`）并用 `torch` 校验结果。

本讲只用到通用（与硬件无关）的指令，不涉及共享内存、张量核等进阶内容，是后续所有讲义的「Hello World」基石。

## 2. 前置知识

阅读本讲前，请确保已经完成 [u1-l2（安装、运行与包目录结构）](u1-l2-install-run-package-layout.md)，并理解下面几个概念：

- **tile-level 编程**：Tilus 让你站在「一个线程块整体做什么」的视角写内核，而不是像 CUDA 那样写「单个线程做什么」。
- **张量为一等公民**：内核里的基本数据单位是张量（tensor），而不是标量。`a + b` 是两个张量相加，Tilus 会自动把它分摊到线程块里的所有线程上。
- **三种张量（先记住名字即可，U4 会深入）**：
  - `GlobalTensor`：位于设备显存（DRAM）的张量，由裸指针 + 形状描述。
  - `RegisterTensor`：分布在各线程寄存器里的张量，是计算发生的地方。
  - `SharedTensor`：片上共享内存（本讲暂不涉及）。
- **缓存目录**：Tilus 会把编译产物按内容哈希缓存到 `programs/<hash>/`。建议开发时用 `tilus.option.cache_dir(...)` 指定一个临时目录，方便查看生成的 `source.cu`。

> 术语提示：`int32` 和 `~float32` 在 `__call__` 的参数标注里有特殊含义（运行时标量参数 vs 指针），本讲只做最小解释，完整规则在 [u1-l4（数据类型与指针类型）](u1-l4-datatypes-and-pointer-types.md) 详述。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
| --- | --- |
| `examples/vector_add/vector_add.py` | 最小可运行示例：一个 `tilus.Script`，完成 `c = a + b`，并自带正确性校验与带宽 benchmark。 |
| `python/tilus/lang/script.py` | 定义 `Script` 基类、`Attributes`（含 `blocks` / `warps`）与 `@autotune` 装饰器。 |
| `python/tilus/lang/instructions/root.py` | `RootInstructionGroup` 提供「通用指令」：`global_view` / `load_global` / `store_global` / `blockIdx` / `dot` / `cast` 等。 |
| `python/tilus/lang/instructions/__init__.py` | `InstructionInterface` 把通用指令与各硬件指令组（`wgmma` / `tma` / …）组合到 `self.*` 上。 |
| `python/tilus/ir/tensor.py` | `RegisterTensor` 的运算符重载（`__add__` / `__mul__` …），解释为何 `ra + rb` 能直接用。 |
| `python/tilus/utils/py.py` | `cdiv(a, b)`：向上取整除法，用于计算网格大小。 |

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

1. **Script 类骨架**：`__init__` 设超参、`__call__` 写算子逻辑。
2. **网格与线程块**：`attrs.blocks` 与 `attrs.warps`。
3. **全局内存到寄存器**：`global_view` → `load_global` → 元素级加法 → `store_global`。

### 4.1 Script 类骨架：`__init__` 与 `__call__`

#### 4.1.1 概念说明

一个 Tilus 内核就是一个继承自 `tilus.Script` 的类，它有且仅有两个关键方法：

- `__init__(self)`：**编译期 / 配置阶段**。在这里设置「调优超参」——那些会改变编译产物、但每次内核启动之间可以不同的常量，例如本讲的 `block_elems`（每个线程块处理多少个元素）。
- `__call__(self, ...)`：**内核逻辑**。用 Tilus 指令描述「一个线程块要做什么」。注意：`__call__` 的函数体**不是被当成普通 Python 执行的**，而是被 Tilus 的转译器（transpiler）遍历抽象语法树（AST），翻译成 Tilus IR，再编译成 CUDA。

> 为什么是两个方法？把「会改变编译结果的旋钮」放进 `__init__`，把「算子本身的数学逻辑」放进 `__call__`，这样 Tilus 能为同一份算子逻辑、不同超参组合各自编译一份内核，这正是自动调优的基础（见 [u2-l4](u2-l4-autotune-and-schedule-space.md)）。

#### 4.1.2 核心流程

把 `VectorAdd` 从「类定义」到「可调用」的流程概括如下：

```text
class VectorAdd(tilus.Script):          # 1. 定义类
    def __init__(self):                 # 2. 编译期：设置超参 block_elems
        ...
    def __call__(self, n, a_ptr, ...):  # 3. 内核逻辑：被转译成 IR

VectorAdd()            # 4. __new__ 拦截构造，返回一个 InstantiatedScript（已编译、可调用）
kernel(n, a, b, c)     # 5. 调用 -> 启动 CUDA 内核
```

关键点：`Script.__new__` 被重写，使得 `VectorAdd(...)` 返回的不是一个裸 `VectorAdd` 实例，而是一个已经过 JIT 编译、可直接调用的 `InstantiatedScript`。

#### 4.1.3 源码精读

先看示例的类骨架与 `__init__`：

[examples/vector_add/vector_add.py:20-25](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/examples/vector_add/vector_add.py#L20-L25) — 定义 `VectorAdd` 类，并在 `__init__` 里设置唯一超参 `self.block_elems = 1024`：

```python
class VectorAdd(tilus.Script):
    def __init__(self):
        super().__init__()
        self.block_elems = 1024
```

注意必须调用 `super().__init__()`，它会初始化 `_attrs`（内核属性容器）与 `self.cuda` 模块：

[python/tilus/lang/script.py:61-68](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/script.py#L61-L68) — `Script.__init__` 创建空的 `Attributes` 对象，并挂载 `cuda` 模块：

```python
def __init__(self) -> None:
    super().__init__()
    self._attrs: Attributes = Attributes()
    self.cuda = cuda
```

再看 `__call__` 的签名（函数体下一节展开）：

[examples/vector_add/vector_add.py:27-33](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/examples/vector_add/vector_add.py#L27-L33) — `__call__` 的参数标注决定了每个参数如何被编译/传递：

```python
def __call__(
    self,
    n: int32,        # 运行时标量参数（int32）：每次启动可能不同的值
    a_ptr: ~float32, # ~float32 = 指向 float32 的指针（设备地址）
    b_ptr: ~float32,
    c_ptr: ~float32,
):
```

- `n: int32` 表示 `n` 是一个**运行时**传入的 32 位整数（向量长度），每次调用可以不同，不触发重新编译。
- `~float32` 前缀的 `~` 表示「指针」，即 `a_ptr` 是一段 `float32` 显存的起始地址。指针参数会参与 JIT 缓存键的计算。（完整规则见 [u1-l4](u1-l4-datatypes-and-pointer-types.md)。）

然后是「构造即编译」的机制。`Script.__new__` 拦截了实例化：

[python/tilus/lang/script.py:50-59](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/script.py#L50-L59) — `VectorAdd(...)` 实际上从 `InstantiatedScriptCache` 取回（或新建并编译）一个 `InstantiatedScript`，而不是返回裸类实例：

```python
def __new__(cls, *args, **kwargs) -> InstantiatedScript:
    from tilus.lang.instantiated_script import InstantiatedScriptCache
    instantiated_script: InstantiatedScript = InstantiatedScriptCache.get(
        script_cls=cls, script_args=args, script_kwargs=kwargs,
    )
    return instantiated_script
```

`__call__` 在基类里只是一个占位，永远不会被真正执行（防止误用）：

[python/tilus/lang/script.py:70-71](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/script.py#L70-L71) — `Script.__call__` 故意抛错，提示真正的内核逻辑由子类的 `__call__` 经转译生成：

```python
def __call__(self, *args, **kwargs):
    raise RuntimeError("This method should never be called.")
```

> 这也解释了 `RegisterTensor` 上 `__add__` / `__mul__` 等运算符的奇怪行为：它们**直接调用时会抛错**（「could only be used in Tilus Script」），只有在转译器遍历 `__call__` 的 AST 时才会被翻译成真正的加法/乘法指令。参见 [python/tilus/ir/tensor.py:222-235](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/tensor.py#L222-L235)。

#### 4.1.4 代码实践

**实践目标**：亲手把 `vector_add` 跑起来，确认环境可用并理解编译即调用。

**操作步骤**：

1. 进入仓库根目录，设置一个临时缓存目录以便观察产物：

   ```bash
   python examples/vector_add/vector_add.py
   ```

   （可选）在脚本开头加上 `tilus.option.cache_dir("/tmp/tilus-vadd-cache")`，运行后查看 `/tmp/tilus-vadd-cache/programs/` 下的编译产物。

2. 在 `main()` 里定位内核被构造和调用的两行，体会「构造即编译」与「调用即启动」：

   [examples/vector_add/vector_add.py:62-72](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/examples/vector_add/vector_add.py#L62-L72) — `kernel = VectorAdd()` 触发 JIT 编译，`kernel(n, a, b, c_actual)` 启动内核，随后用 `torch.testing.assert_close` 校验正确性。

**需要观察的现象**：脚本会打印一张表，列出不同 `n` 下 `torch` 与 `tilus` 两个实现的延迟（ms）与显存带宽（GB/s）。

**预期结果**：`assert_close` 通过（无 `AssertionError`），且 `tilus` 实现的带宽应与 `torch` 处于同一量级——`vector_add` 是典型的访存密集型算子，瓶颈在显存带宽。

> 运行结果待本地验证（本讲无法在此执行 GPU 程序）。若无可用 GPU，请阅读下一小节的源码阅读型练习作为替代。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `super().__init__()` 这一行删掉，会发生什么？

**参考答案**：`self._attrs`（`Attributes`）不会被创建，后续 `self.attrs.blocks = ...` 会在访问 `self.attrs` 时抛 `AttributeError`。`super().__init__()` 是初始化内核属性容器的必经步骤。

**练习 2**：为什么 `__init__` 里写 `self.block_elems = 1024` 而不是把 `1024` 直接写进 `__call__`？

**参考答案**：`__init__` 里的属性属于「编译期超参」，改变它会改变编译产物（网格划分、每线程处理的元素数都依赖它）。把它单独放在 `__init__`，便于将来用 `@autotune`（[u2-l4](u2-l4-autotune-and-schedule-space.md)）或 `debug_schedule` 对它做搜索/固定，而 `__call__` 的算子逻辑保持不变。

### 4.2 网格与线程块配置：`attrs.blocks` 与 `attrs.warps`

#### 4.2.1 概念说明

CUDA 内核启动时需要两个维度信息：

- **网格（grid）**：一共启动多少个线程块（thread block）。`self.attrs.blocks` 设置它，类似 `<<<numBlocks, ...>>>` 的第一个参数。
- **线程块大小**：每个线程块里有多少线程。Tilus 用 **warp（32 个线程为 1 个 warp）** 为单位来配置，`self.attrs.warps` 表示每个线程块有几个 warp。

在 `vector_add` 里，整个长度为 `n` 的向量被切成若干段，每段 `block_elems = 1024` 个元素，由一个线程块处理。于是：

- 网格大小（线程块数）= `cdiv(n, block_elems)`，即 \(\lceil n / 1024 \rceil\)。
- 每个线程块有 `warps = 4` 个 warp = \(4 \times 32 = 128\) 个线程。
- 因此每个线程负责 \(1024 / 128 = 8\) 个元素。

\[ \text{num\_blocks} = \left\lceil \frac{n}{\text{block\_elems}} \right\rceil, \qquad \text{threads\_per\_block} = \text{warps} \times 32 \]

#### 4.2.2 核心流程

```text
n (运行时) ──cdiv(n, 1024)──▶ attrs.blocks = (num_blocks,)   # 1 维网格
warps = 4 (编译期常量) ─────▶ 每个线程块 128 线程
blockIdx.x ∈ [0, num_blocks)  每个线程块处理 [offset, offset+1024) 这一段
```

`blockIdx.x` 是当前线程块在网格里的 x 编号，由 `RootInstructionGroup.blockIdx` 提供（返回一个含 `.x/.y/.z` 的 `Dim3`）。

#### 4.2.3 源码精读

[examples/vector_add/vector_add.py:34-37](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/examples/vector_add/vector_add.py#L34-L37) — 设置网格与线程块，并算出当前线程块负责的数据起点：

```python
self.attrs.blocks = (cdiv(n, self.block_elems),)  # 1 维网格
self.attrs.warps = 4                                # 每块 4 个 warp = 128 线程

offset: int32 = self.block_elems * self.blockIdx.x  # 当前块的起始元素下标
```

`cdiv` 就是向上取整除法：

[python/tilus/utils/py.py:26-27](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/utils/py.py#L26-L27) — `cdiv(a, b) = (a + b - 1) // b`：

```python
def cdiv(a, b):
    return (a + (b - 1)) // b
```

`attrs.blocks` / `attrs.warps` 的类型定义在 `Attributes`：

[python/tilus/lang/script.py:29-39](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/script.py#L29-L39) — `Attributes` 描述网格、cluster 与 warp 数：

```python
class Attributes:
    blocks: Optional[Sequence[Int] | Int] = None        # 网格维度，最多 3 个
    cluster_blocks: Sequence[Int] | Int = (1, 1, 1)     # cluster 维度，默认 (1,1,1)
    warps: Optional[int] = None                          # 每块 warp 数，必须是编译期常量
```

而 `self.attrs` 只是一个返回 `self._attrs` 的属性：

[python/tilus/lang/script.py:92-98](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/script.py#L92-L98) — `attrs` 属性仅在 `__call__` 内部访问：

```python
@property
def attrs(self) -> Attributes:
    return self._attrs
```

`blockIdx` 来自通用指令组：

[python/tilus/lang/instructions/root.py:33-36](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/instructions/root.py#L33-L36) — `self.blockIdx` 返回一个 `Dim3`，其 `.x` 就是 CUDA 内建的 `blockIdx.x`：

```python
@property
def blockIdx(self) -> Dim3:
    return Dim3(blockIdx.x, blockIdx.y, blockIdx.z)
```

`Dim3` 只是把三个变量打包，方便用 `.x/.y/.z` 访问：

[python/tilus/lang/constructs/structs/dim3.py:18-22](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/constructs/structs/dim3.py#L18-L22)：

```python
class Dim3:
    def __init__(self, x: Var, y: Var, z: Var):
        self.x: Var = x
        self.y: Var = y
        self.z: Var = z
```

> 约束提醒：示例在 `main()` 里强制 `assert n % 1024 == 0`，因为本内核没有做越界处理，必须保证 `n` 恰好被 `block_elems` 整除（见 [examples/vector_add/vector_add.py:60](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/examples/vector_add/vector_add.py#L60)）。

#### 4.2.4 代码实践

**实践目标**：理解 `block_elems` 与 `warps` 如何决定每个线程的工作量。

**操作步骤**：

1. 在 `vector_add.py` 里，把 `self.block_elems = 1024` 改成 `2048`，把 `self.attrs.warps = 4` 改成 `8`。
2. 在脑中（或纸面上）重新计算：
   - 新的网格大小 `cdiv(n, 2048)`；
   - 每个线程块线程数 `8 * 32 = 256`；
   - 每个线程负责的元素数 `2048 / 256 = 8`（与原来相同！）。
3. （可选）开启 `tilus.option.debug.dump_ir()`，在缓存目录里对比修改前后生成的 `source.cu` 中 `<<<grid, block>>>` 启动配置的变化。

**需要观察的现象**：每线程元素数不变，但每个线程块搬运的数据量翻倍、网格块数减半。

**预期结果**：算子的正确性应当不受影响（`assert_close` 仍通过），但带宽/延迟数字会随分块方式变化。具体数值待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：当 `n = 1 << 20`（即 1048576），`block_elems = 1024` 时，网格里有多少个线程块？

**参考答案**：\(1048576 / 1024 = 1024\) 个线程块（恰好整除，`cdiv` 与普通除法结果一致）。

**练习 2**：为什么 `attrs.warps` 必须是「编译期常量」，而 `attrs.blocks` 可以依赖运行时参数 `n`？

**参考答案**：`warps` 决定了每个线程块里的线程数，进而决定寄存器张量在每个线程上的分布（每个线程持有的元素数 = tile 大小 / 线程数），这会影响编译产物，所以必须是编译期常量。`blocks` 只影响「启动多少个块」，是运行时启动参数，可以依赖 `n`，不需要为不同的 `n` 重新编译内核。

### 4.3 全局内存到寄存器：`global_view` → `load_global` → 元素级加法 → `store_global`

#### 4.3.1 概念说明

这是本讲的核心数据流。GPU 上「计算」只发生在寄存器里，因此必须先把数据从显存（global memory）搬到寄存器，算完再搬回去。Tilus 用三个指令完成这条链路：

- `global_view(ptr, dtype=..., shape=...)`：把一个**裸指针**包装成一个 `GlobalTensor`（全局张量视图）。它不搬运数据，只是给指针附加「形状 + 行主序排布」的元信息。
- `load_global(g, offsets=..., shape=...)`：从全局张量里切出一小块，加载到一个**新的 `RegisterTensor`**（寄存器张量）。
- `store_global(g, r, offsets=...)`：把一个寄存器张量写回全局张量的某个切片。
- 中间的 `ra + rb`：两个 `RegisterTensor` 的元素级加法，结果仍是 `RegisterTensor`。

`self.global_view` / `self.load_global` / `self.store_global` 这些方法都来自 `RootInstructionGroup`，并通过 `InstructionInterface` 挂到 `self.*` 上。

#### 4.3.2 核心流程

以 `vector_add` 的 `__call__` 为例，一个线程块内的数据流：

```text
a_ptr (裸指针)
   │  global_view(dtype=float32, shape=[n])
   ▼
ga: GlobalTensor  ──load_global(offsets=[offset], shape=[1024])──▶ ra: RegisterTensor
                                                                         │
b_ptr ──▶ gb ──load_global──▶ rb: RegisterTensor                       │
                                      │                                  │
                                      └──────────► ra + rb ◀─────────────┘
                                                       │  (元素级加法，结果 rc: RegisterTensor)
                                                       ▼
c_ptr ──▶ gc: GlobalTensor  ◀──store_global(gc, rc, offsets=[offset])──┘
```

每个线程块只处理 `[offset, offset + 1024)` 这一段；不同的 `blockIdx.x` 处理不同的段，合起来覆盖整个长度 `n` 的向量。

#### 4.3.3 源码精读

[examples/vector_add/vector_add.py:39-46](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/examples/vector_add/vector_add.py#L39-L46) — 完整的四步数据流：

```python
ga = self.global_view(a_ptr, dtype=float32, shape=[n])          # 1. 包装裸指针
gb = self.global_view(b_ptr, dtype=float32, shape=[n])
gc = self.global_view(c_ptr, dtype=float32, shape=[n])

ra = self.load_global(ga, offsets=[offset], shape=[self.block_elems])  # 2. 加载到寄存器
rb = self.load_global(gb, offsets=[offset], shape=[self.block_elems])
rc = ra + rb                                                     # 3. 元素级加法
self.store_global(gc, rc, offsets=[offset])                      # 4. 写回显存
```

下面逐个对照源码实现。

**`global_view`**：接收指针，默认按「紧凑行主序」构造布局，再交给 builder 生成 `GlobalTensor`：

[python/tilus/lang/instructions/root.py:422-470](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/instructions/root.py#L422-L470) — 关键片段：未给 `strides` 时默认行主序：

```python
def global_view(self, ptr, *, dtype, shape, strides=None):
    ...
    if strides is None:
        layout = global_row_major(*shape)   # 紧凑行主序
    else:
        layout = global_strides(shape, strides)
    return self._builder.global_view(ptr=ptr, dtype=dtype, layout=layout)
```

**`load_global`**：从全局张量切一个子块到寄存器张量。`offsets` 长度必须等于全局张量的维数，`shape` 是要加载的切片形状：

[python/tilus/lang/instructions/root.py:472-522](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/instructions/root.py#L472-L522) — 关键片段：

```python
def load_global(self, src, /, *, offsets, shape, dims=None, out=None) -> RegisterTensor:
    if len(offsets) != len(src.shape):
        raise InstructionError("The number of offsets must be equal to ...")
    return self._builder.load_global(x=src, offsets=offsets, dims=dims, shape=shape, output=out)
```

本例中 `ga` 是 1 维（形状 `[n]`），所以 `offsets=[offset]` 长度为 1，加载出形状 `[1024]` 的寄存器张量。

**元素级加法 `ra + rb`**：这是 `RegisterTensor.__add__`。它在直接调用时会抛错，只有被转译器在 `__call__` 的 AST 里识别到时，才翻译成真正的 `add` 指令（最终落到 `self._builder.add`）：

[python/tilus/ir/tensor.py:222-235](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/tensor.py#L222-L235) — `__add__` 只是占位，真正语义在转译器里：

```python
def __add__(self, other):
    ...
    raise RuntimeError("tensor + tensor could only be used in Tilus Script.")
```

> 提示：等价的显式写法是 `self.add(ra, rb)`，定义在 [python/tilus/lang/instructions/root.py:1749-1770](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/instructions/root.py#L1749-L1770)。同理 `ra * 2.0` 对应 `self.mul(...)` / `__mul__`。

**`store_global`**：把寄存器张量写回全局张量的某个切片：

[python/tilus/lang/instructions/root.py:524-566](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/instructions/root.py#L524-L566) — 关键片段：

```python
def store_global(self, dst, src, *, offsets, dims=None) -> None:
    ...
    return self._builder.store_global(dst=dst, src=src, offsets=offsets, dims=dims)
```

最后，为什么 `self.global_view` 这些方法能用？因为 `Script` 继承自 `InstructionInterface`，而它继承 `RootInstructionGroup` 并组合了硬件指令组：

[python/tilus/lang/instructions/__init__.py:30-38](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/instructions/__init__.py#L30-L38)：

```python
class InstructionInterface(RootInstructionGroup):
    tcgen05 = Tcgen05InstructionGroup()
    tma = TmaInstructionGroup()
    mbarrier = BarrierInstructionGroup()
    fence = FenceInstructionGroup()
    clc = ClusterLaunchControlInstructionGroup()
    cluster = BlockClusterInstructionGroup()
    wgmma = WgmmaInstructionGroup()
    atomic = AtomicInstructionGroup()
```

所以本讲用到的 `global_view` / `load_global` / `store_global` / `blockIdx` 都是「通用指令」（与 GPU 架构无关），而 `wgmma` / `tma` / `tcgen05` 等是与架构绑定的「硬件指令组」（后续 U7 讲）。

#### 4.3.4 代码实践

**实践目标**：用「源码阅读」方式确认 `load_global` / `store_global` 的参数校验逻辑，理解越界为何不被本内核处理。

**操作步骤**：

1. 打开 [python/tilus/lang/instructions/root.py:518-522](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/lang/instructions/root.py#L518-L522)，阅读 `load_global` 里的 `if len(offsets) != len(src.shape)` 校验。
2. 回答：如果给一个 1 维全局张量传 `offsets=[offset, 0]`（长度 2），会发生什么？
3. 跟踪 `_builder.load_global` 的调用，确认 `offsets` 只是描述「从哪开始切」，并不检查 `offset + shape` 是否超出全局张量的 `[0, n)` 范围。

**需要观察的现象**：参数维数不匹配时，Tilus 会在**转译期**就抛 `InstructionError`（而不是运行期）；但越界访问不会被告警——这正是 `main()` 里要 `assert n % 1024 == 0` 的原因。

**预期结果**：你应当能用一句话说清「为什么这个内核要求 `n` 能被 `block_elems` 整除」：因为没有边界检查，最后一个线程块若 `offset + 1024 > n` 就会越界读写显存。

#### 4.3.5 小练习与答案

**练习 1**：`global_view` 做了数据的实际搬运吗？

**参考答案**：没有。`global_view` 只是把裸指针 + 形状（+ 可选步长）包装成一个 `GlobalTensor` 视图，附加排布元信息，不产生任何内存读写。真正的搬运由 `load_global` / `store_global` 完成。

**练习 2**：如果把 `rc = ra + rb` 改写成 `rc = self.add(ra, rb)`，行为会不同吗？

**参考答案**：不会。`ra + rb` 只是 `self.add(ra, rb)` 的语法糖——转译器把 `+` 运算符翻译成同一条 `add` 指令。二者生成的 IR 完全一致。

**练习 3**：`load_global` 返回的张量位于哪种内存空间？为什么？

**参考答案**：返回的是 `RegisterTensor`，分布在各线程的寄存器里。因为 GPU 的算术运算只能直接操作寄存器，所以从显存加载进来的数据必须落到寄存器才能参与 `ra + rb` 这样的计算。

## 5. 综合实践

把本讲三个模块串起来，基于 `vector_add` 改写一个 **`scale` 内核**：\(c = a \times 2.0\)。

**任务**：新建一个脚本（例如 `examples/vector_add/scale.py`，或直接在本地副本上修改），实现并校验下面的内核。注意它只需要一个输入指针 `a_ptr` 和一个输出指针 `c_ptr`，不再需要 `b_ptr`。

```python
# 示例代码：基于 vector_add 改写的 scale 内核
import tilus
import torch
from tilus import float32, int32
from tilus.utils import cdiv


class Scale(tilus.Script):
    """c[i] = a[i] * 2.0"""

    def __init__(self):
        super().__init__()
        self.block_elems = 1024

    def __call__(self, n: int32, a_ptr: ~float32, c_ptr: ~float32):
        # 模块 4.2：网格与线程块
        self.attrs.blocks = (cdiv(n, self.block_elems),)
        self.attrs.warps = 4

        offset: int32 = self.block_elems * self.blockIdx.x

        # 模块 4.3：全局内存 -> 寄存器 -> 计算 -> 全局内存
        ga = self.global_view(a_ptr, dtype=float32, shape=[n])
        gc = self.global_view(c_ptr, dtype=float32, shape=[n])

        ra = self.load_global(ga, offsets=[offset], shape=[self.block_elems])
        rc = ra * 2.0                       # RegisterTensor * 标量，等价于 self.mul(ra, ...)
        self.store_global(gc, rc, offsets=[offset])


def main():
    n = 1 << 20  # 必须能被 1024 整除
    assert n % 1024 == 0

    kernel = Scale()
    a = torch.randn(n, dtype=torch.float32, device="cuda")
    c_actual = torch.empty(n, dtype=torch.float32, device="cuda")
    c_expect = a * 2.0
    torch.cuda.synchronize()

    kernel(n, a, c_actual)
    torch.cuda.synchronize()

    torch.testing.assert_close(c_expect, c_actual)
    print("scale 内核校验通过 ✓")


if __name__ == "__main__":
    main()
```

**操作步骤**：

1. 复制上面的代码到新文件并运行。
2. 与 `vector_add` 逐行对照，确认你改动的只有三处：删掉 `b` 相关的 `global_view` / `load_global`、把 `ra + rb` 换成 `ra * 2.0`、调整 `main()` 里的输入输出。
3. 把 `ra * 2.0` 改成等价的 `self.mul(ra, 2.0)`，再次运行，确认结果一致（呼应练习 2）。
4. 进阶：把 `2.0` 改成一个负数或 `0.0`，验证校验仍能区分正确与错误实现。

**预期结果**：打印 `scale 内核校验通过 ✓`。如果 `assert_close` 失败，请重点检查 `offset` 计算、`global_view` 的 `shape=[n]` 与 `load_global` 的 `shape=[self.block_elems]` 是否写反——这是初学者最常犯的错误。

> 运行结果待本地验证（需要可用 GPU）。关键正确性要点：每个线程块的 `offset = 1024 * blockIdx.x`，块与块之间不重叠地覆盖 `[0, n)`，因此不会出现重复写或漏写。

## 6. 本讲小结

- 一个 Tilus 内核 = 一个继承 `tilus.Script` 的类，由 `__init__`（编译期超参）和 `__call__`（被转译的内核逻辑）两个方法构成；`VectorAdd(...)` 经 `Script.__new__` 返回已编译的 `InstantiatedScript`。
- `__call__` 的参数标注决定参数性质：`int32` 是运行时标量，`~float32` 是指针（完整规则见 [u1-l4](u1-l4-datatypes-and-pointer-types.md)）。
- `self.attrs.blocks` 决定网格（线程块数，可依赖运行时 `n`），`self.attrs.warps` 决定每块线程数（必须编译期常量）；二者共同决定每个线程负责多少元素。
- 标准数据流是 `global_view`（包装指针）→ `load_global`（显存到寄存器）→ 元素级运算（如 `ra + rb`）→ `store_global`（寄存器写回显存）。
- `self.*` 上的通用指令来自 `RootInstructionGroup`，硬件指令组（`wgmma`/`tma`/…）作为属性组合其上；`RegisterTensor` 的运算符重载只在转译器内生效。
- 本内核依赖 `n % block_elems == 0`，因为它不做越界检查——这是阅读源码时需要主动留意的安全约束。

## 7. 下一步学习建议

- 想彻底搞清 `int32` vs `~float32`、以及任意位宽低精度类型？请读 [u1-l4 数据类型与指针类型](u1-l4-datatypes-and-pointer-types.md)。
- 想看更复杂的内核如何把 `register_tensor` / `dot` / `cast` 与分块循环组合起来？请读 [u1-l5 从 naive matmul 理解 Tilus Script 全貌](u1-l5-naive-matmul-tilus-script.md)。
- 对「`__call__` 是如何被翻译成 IR 的」感兴趣？这部分在 [u3-l2 Transpiler：从 Python AST 到 Tilus IR](u3-l2-transpiler-ast-to-ir.md) 深入讲解。
- 建议同时翻阅官方文档 `docs/source/programming-guides/` 下的 overview 与 instruction-groups 章节，与本讲对照阅读。
