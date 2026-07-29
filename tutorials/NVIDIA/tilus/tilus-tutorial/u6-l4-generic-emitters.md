# 通用发射器：elementwise/reduce/ldst/shared_ldst

## 1. 本讲目标

本讲是「后端与代码生成」单元的第四讲。在前三讲里，我们已经知道 `FunctionCodegen` 会把每条 Tilus 指令经全局注册表 `REGISTRY` 派发给一个发射器（emitter），发射器负责把这条张量级指令翻译成一组 Hidet IR 语句（u6-1）；发射器近乎无状态，跨指令的共享状态集中住在 `EmitContexts` 的九个上下文里（u6-3）。但「翻译」二字到底是怎么发生的？本讲就打开四类最常见、最具代表性的**通用发射器**，逐行看清它们如何工作：

- **elementwise**：逐元素一元/二元运算如何按线程局部布局展开成每线程的标量循环。
- **reduce**：跨线程规约如何拆成「线程内 → warp 内 → 跨 warp」三段式。
- **ldst**：全局内存的 load/store 如何做向量化。
- **shared_ldst**：共享内存的 load/store 如何优先用 `ldmatrix`/`stmatrix` 硬件指令、不行再回退到通用向量化搬运。

学完本讲，你应当能够：

1. 说出**所有通用发射器的共同套路**：用 `layout.get_global/get_local` 把张量布局翻译成「每个线程读写哪些标量」，再用 `for_range` + `buffer_store` 把这套逻辑写出来。
2. 手动推演一个 `RegisterLayout` 下、每个线程具体要执行哪些标量运算。
3. 理解 reduce 的三段式拆解与 warp shuffle / 共享内存两条同步路径。
4. 读懂访存发射器的**向量化分析**（`analyze_vectorization`）以及 `ldmatrix`/`stmatrix` 的兼容性判定与回退策略。

## 2. 前置知识

本讲默认你已经掌握以下概念（它们都来自前面的讲义，这里只做一句话唤醒，不重复展开）：

- **发射器（emitter）与 `emit(inst)`**（u6-2）：每类 Tilus 指令对应一个发射器类，`visit_Instruction` 调用它；它继承自 `BaseInstEmitter`，而 `BaseInstEmitter` 又继承自 `StmtBuilder`，因此发射器代码读起来就像在「用 Python 语句构造器一行行写 Hidet IR」。
- **`StmtBuilder` 构造器 API**（u6-1/u6-2）：`with self.for_range(extent) as i:` 生成一个 for 循环；`self.buffer_store(buf, indices, value)` 生成一次数组写；`self.declare_var(name, tp, init)` 声明一个标量变量；`with self.if_then(cond): ... with self.otherwise(): ...` 生成分支。这些方法最终产出的都是 Hidet IR 节点。
- **`BaseInstEmitter` 的通用能力**（u6-2）：`self.get_or_allocate_var(tensor)` 把张量惰性映射成一个 Hidet `Var`（寄存器张量→一维数组 `regs[local_size]`）；`self.tensor2var` 是同一张映射表；`self.current_thread` 是当前线程在线程组内的线性编号。
- **`EmitContexts`**（u6-3）：`self.contexts.smem_alloc_ctx`（动态共享内存分配）、`self.sync()`（按线程组规模选择同步原语）等跨指令状态。
- **`RegisterLayout`**（u4-2/u4-4）：一个寄存器张量的布局由 `shape / mode_shape / spatial_modes / local_modes` 唯一确定；`spatial_size` 是线程数，`local_size` 是每线程持有的元素数；`get_global(spatial_index, local_index)` 给出「某线程的某个局部槽」对应的全局逻辑坐标，`get_local(global_indices)` 是其逆。
- **`lower_load_store` 的产物**（u5-4）：高层访存指令经降级后变成带「标量指针 `ptr` + 符号偏移 `offset` + 掩码 `mask`」的 `LoadGlobalGenericInst` / `StoreGlobalGenericInst`，以及单纯的 `LoadSharedInst` / `StoreSharedInst`——这些正是本讲 ldst / shared_ldst 发射器的输入。
- **`Analysis`（标量分析）**（u5-3）：`self.analysis` 提供每个变量的整除性（`divisibility`）与上下界（`lower_bound`/`upper_bound`），是访存向量化分析的关键输入。

一句话概括本讲的「心法」：**通用发射器没有魔法，它就是把「张量布局」这张「逻辑索引 → 物理位置」的映射表（u4-4），翻译成「每个线程的标量地址与标量运算」。**

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [emitter.py](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/backends/emitter.py) | `BaseInstEmitter` 基类：提供 `get_or_allocate_var`、`current_thread`、`sync` 等所有发射器共用的能力。 |
| [elementwise.py](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/backends/emitters/elementwise.py) | 一元/二元逐元素运算发射器，本讲「逐线程展开」的范本。 |
| [reduce.py](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/backends/emitters/reduce.py) | `ReduceInst` 发射器，三段式跨线程规约。 |
| [ldst.py](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/backends/emitters/ldst.py) | 全局 load/store 发射器，含向量化分析。 |
| [shared_ldst.py](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/backends/emitters/shared_ldst.py) | 共享内存 load/store 发射器，优先 `ldmatrix`/`stmatrix`，否则回退通用向量化。 |
| [register_layout.py](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/layout/register_layout.py) | `RegisterLayout` 的 `get_global`/`get_local`/`local_size` 等访问器，是逐线程翻译的核心。 |
| [generic.py](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/instructions/generic.py) | 本讲涉及的指令定义：`ElementwiseBinaryBaseInst`、`ReduceInst`、`LoadGlobalGenericInst`、`LoadSharedInst` 等。 |

## 4. 核心概念与源码讲解

### 4.1 共同套路：把张量布局翻译成每线程的标量运算

#### 4.1.1 概念说明

在 CUDA 里，一个 kernel 是「每个线程各自跑一段标量代码」。但在 Tilus 里，你写的是「一个线程块对一整块张量做什么」。于是**后端必须把「张量视角」翻译回「单线程视角」**——这正是通用发射器的核心职责。

这个翻译的钥匙是 `RegisterLayout`（u4-2）：它本身就是一张「逻辑（全局）坐标 ↔ 物理位置（哪个线程的哪个局部槽）」的映射表。发射器只要做两件事：

1. **确定每个线程要处理哪些全局元素**：用 `layout.get_global(spatial_index=self.current_thread, local_index=i)` 把「线程号 + 局部循环下标」还原成全局坐标。
2. **生成一段围绕 `local_size` 次循环的标量代码**：每次循环算一个元素的地址与运算，用 `buffer_store` 写回。

无论 elementwise、reduce 还是访存，套路都一样：**外层是「对 local_size 的 for_range 循环」，循环体里先用 `get_global` 算坐标、再算地址/掩码、最后做运算或搬运。**

#### 4.1.2 核心流程

通用发射器的标准骨架（伪代码）：

```
def emit(self, inst):
    out = inst.output              # 输出张量（RegisterTensor）
    out_buf = self.get_or_allocate_var(out)        # 映射为 hidet 数组 regs[local_size]
    with self.for_range(out.local_size) as i:       # 每个线程跑 local_size 次标量迭代
        g_idx = out.layout.get_global(
            spatial_index=self.current_thread,      # 当前线程号
            local_index=i)                           # 本线程第 i 个局部槽
        # ……用 g_idx 算地址、读输入、做运算……
        self.buffer_store(out_buf, [i], value)       # 写到本线程第 i 个槽
```

三个关键 API：

- `self.current_thread`：当前线程的线性编号（见 [emitter.py:102-106](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/backends/emitter.py#L102-L106)）。它是 `get_global` 的 `spatial_index`。
- `self.get_or_allocate_var(tensor)`：把张量映射成一个一维 hidet 数组（寄存器张量是 `regs[local_size]`，共享张量是 `smem[size]` 指针），见 [emitter.py:81-100](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/backends/emitter.py#L81-L100)。
- `layout.get_global / get_local`：见 [register_layout.py:146-178](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/layout/register_layout.py#L146-L178)。

`get_global` 的数学含义是把线程空间坐标与局部空间坐标「合成」回全局坐标。对一个被 `compose(spatial, local)` 构造出的 1 维布局（mode 0 是空间、mode 1 是局部），设空间大小 \(S\)、局部大小 \(L\)，全局元素总数 \(N = S \cdot L\)，则：

\[
\mathrm{global}(t,\,\ell) = t \cdot L + \ell,\qquad t\in[0,S),\ \ell\in[0,L)
\]

即「线程 \(t\) 持有全局第 \(tL \dots tL+L-1\) 号元素」。这正是 `get_global` 在最简情形下的行为，也是后续手动推演的依据。

#### 4.1.3 源码精读

先看发射器共用的「张量 → 数组」映射。`get_or_allocate_var` 对寄存器张量声明一个形状为 `[local_size]` 的一维数组，并登记进 `tensor2var`，保证同一张量只声明一次（[emitter.py:81-100](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/backends/emitter.py#L81-L100)）：

```python
def get_or_allocate_var(self, tensor, name=None):
    if tensor in self.tensor2var:
        return self.tensor2var[tensor]
    ...
    elif isinstance(tensor, RegisterTensor):
        var = self.declare(
            tensor_var(name, shape=[tensor.local_size], dtype=tensor.dtype),
            scope=DeclareScope.Register)
    ...
    self.tensor2var[tensor] = var
    return var
```

注意形状用的是 `tensor.local_size`（每线程持有元素数），**不是** `size`（全局元素总数）——这一点至关重要：每个线程的寄存器数组只装得下它自己负责的那一份。

再看 `get_global` 如何把 `(线程号, 局部下标)` 还原成全局多维坐标（[register_layout.py:161-178](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/layout/register_layout.py#L161-L178)）：它先把 `spatial_index` 与 `local_index` 各自拆成各自 mode 的下标，分别填进 `mode_indices`，再按维度把同一维的若干 mode 重新打包（`index_serialize`）成该维的全局坐标。`get_local` 是逆过程。

> 顺带一提，`index_serialize(indices, shape)`（默认 ranks）就是把多维下标按行优先展平成线性下标，`index_deserialize` 是其逆——reduce 发射器里大量用到这对函数来做「线性下标 ↔ 多维下标」的互转。

#### 4.1.4 代码实践（源码阅读型）

**实践目标**：建立「布局 → 每线程数组」的直觉。

1. 打开 [emitter.py:81-100](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/backends/emitter.py#L81-L100)，确认寄存器张量被声明成 `shape=[local_size]` 的一维数组。
2. 打开 [register_layout.py:98-108](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/layout/register_layout.py#L98-L108)，确认 `local_size = prod(local_shape)`、`spatial_size = prod(spatial_shape)`。

**需要观察的现象 / 预期结果**：你会看到「线程数 = spatial_size」「每线程元素数 = local_size」，二者乘积（在无复制时）等于 `size`。这正是逐线程展开的算术约束。待本地验证：用一个 `register_tensor` 打印其 `.layout.local_size` 与 `.layout.spatial_size`。

#### 4.1.5 小练习与答案

- **练习 1**：为什么 `get_or_allocate_var` 给寄存器张量分配的数组形状是 `[local_size]` 而不是 `[size]`？
  - **答案**：寄存器是线程私有的，每个线程只能看到、也只能存自己负责的那 `local_size` 个元素；全局 `size` 个元素是所有线程**合起来**才覆盖的。
- **练习 2**：`get_global` 的 `spatial_index` 参数在发射器里通常传什么？
  - **答案**：传 `self.current_thread`（当前线程的线性编号），表示「从当前线程的视角」去反查它持有的元素对应的全局坐标。

### 4.2 elementwise 发射器：按线程局部布局逐元素展开

#### 4.2.1 概念说明

`elementwise.py` 是所有逐元素运算（加、减、乘、取负、abs、clip……）的统一发射器。它注册在**基类** `ElementwiseUnaryBaseInst` / `ElementwiseBinaryBaseInst` 上，于是所有子类（`AddInst`、`MulInst`、`NegInst`……）共用同一个发射器，差异只体现在指令自己实现的 `f_compute` 回调里——这是一处优雅的「模板方法」设计：发射器管「怎么遍历」，指令管「算什么」。

#### 4.2.2 核心流程

二元运算 `z = x ⊙ y` 的展开流程：

1. 取出 `x_buf / y_buf / z_buf`（三个一维寄存器数组）。
2. 对 `z.local_size` 跑一个循环 `i`：
   - 先算输出元素的全局坐标 `z_indices = z.layout.get_global(local_index=i, spatial_index=current_thread)`。
   - 用 `broadcast_indices` 把输出坐标「降维」回 `x`/`y` 的坐标（处理 NumPy 风格的广播，比如某维为 1）。
   - 再用 `x.layout.get_local(x_indices)` 把坐标转成 `x` 的**局部下标** `x_local`（因为每个线程只能按局部下标访问自己的数组）。
   - 读 `x_buf[x_local]`、`y_buf[y_local]`，用 `inst.f_compute(lhs, rhs)` 算结果，写到 `z_buf[i]`。

#### 4.2.3 源码精读

一元发射器最简洁，是理解「逐线程展开」的最佳入口（[elementwise.py:22-32](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/backends/emitters/elementwise.py#L22-L32)）：

```python
@register_emitter(ElementwiseUnaryBaseInst)
class ElementwiseUnaryInstEmitter(BaseInstEmitter):
    def emit(self, inst):
        x_tensor = inst.inputs[0].as_register_tensor()
        y_tensor = inst.register_output
        x_buf = self.tensor2var[x_tensor]
        y_buf = self.get_or_allocate_var(y_tensor)
        with self.for_range(extent=y_tensor.local_size) as i:
            v = self.declare_var("v", tp=x_tensor.dtype, init=x_buf[i])
            self.buffer_store(buf=y_buf, indices=[i], value=inst.f_compute(v))
```

要点：

- 输入 `x` 此前一定已被别的发射器映射过（`tensor2var` 里有），所以直接取；输出 `y` 用 `get_or_allocate_var` 惰性创建（这是 u6-2 里强调的「发射器要为 `inst.output` 建立 `tensor2var`」）。
- 循环次数是 `local_size`，**每个线程各自跑这一段**，因此 `i` 是「本线程的局部下标」。
- `inst.f_compute(v)` 把运算逻辑交给指令自己（如 `NegInst.f_compute` 返回 `-v`），见 [generic.py:330-337](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/instructions/generic.py#L330-L337)。

二元发射器多了「坐标映射 + 广播」一节（[elementwise.py:35-53](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/backends/emitters/elementwise.py#L35-L53)）：

```python
with self.for_range(extent=z_tensor.local_size) as i:
    z_indices = z_tensor.layout.get_global(local_index=i, spatial_index=self.current_thread)
    x_indices = broadcast_indices(out_indices=z_indices, shape=x_tensor.shape, out_shape=z_tensor.shape)
    y_indices = broadcast_indices(out_indices=z_indices, shape=y_tensor.shape, out_shape=z_tensor.shape)
    x_local = x_tensor.layout.get_local(x_indices)
    y_local = y_tensor.layout.get_local(y_indices)
    lhs = self.declare_var("lhs", tp=x_tensor.dtype, init=x_buf[x_local])
    rhs = self.declare_var("rhs", tp=y_tensor.dtype, init=y_buf[y_local])
    self.buffer_store(buf=z_buf, indices=[i], value=inst.f_compute(lhs, rhs))
```

这里出现了一次「正向（输出→全局）」再用两次「逆向（全局→输入局部）」的布局查询。`broadcast_indices` 的规则见 [broadcast_utils.py:123-132](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/hidet/ir/utils/broadcast_utils.py#L123-L132)：把输出坐标对齐到输入形状后，凡输入某维为 1 的就置 0，从而实现广播复用。

> 注意：一元发射器里**没有**坐标映射，直接 `x_buf[i]`。这是因为一元的输入输出形状必然相同，且按 u4-5 布局推理会给它们**同一个布局**，于是 `i` 同时是 `x` 与 `y` 的局部下标。二元之所以要绕一圈 `get_global→broadcast→get_local`，是因为输入间可能存在广播、布局未必逐位相同。

#### 4.2.4 代码实践（手动推演，本讲的主实践）

**实践目标**：对照发射器与一个具体 `RegisterLayout`，手算每个线程要执行的标量运算。

设有一个逐元素加法 `z = x + y`，三个张量形状均为 `[4]`（4 个元素），布局为 `spatial(2).local(2)`，即用 2 个线程、每线程持有 2 个元素。该布局的语义是：mode 0（大小 2）是空间维（线程号），mode 1（大小 2）是局部维。

按 [elementwise.py:44-53](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/backends/emitters/elementwise.py#L44-L53) 的逻辑，逐步推演：

1. `z.local_size = 2`，循环 `i ∈ {0, 1}`。
2. `get_global(local_index=i, spatial_index=t)`：由 4.1.2 的公式，全局坐标 \(= t\cdot 2 + i\)。
3. 无广播（形状相同），`x_indices = y_indices = z_indices`。
4. `get_local(global=[t·2+i])`：模 2 取局部下标 \(= (t\cdot 2+i)\bmod 2 = i\)。

于是得到每个线程的标量运算表（\(x_g\) 表示全局第 \(g\) 号元素）：

| 线程 \(t\) | \(i=0\) | \(i=1\) |
| --- | --- | --- |
| 0 | `z[0] = x[0] + y[0]`（读 x_buf[0]、y_buf[0]） | `z[1] = x[1] + y[1]`（读 x_buf[1]、y_buf[1]） |
| 1 | `z[2] = x[2] + y[2]`（读 x_buf[0]、y_buf[0]） | `z[3] = x[3] + y[3]`（读 x_buf[1]、y_buf[1]） |

**需要观察的现象 / 预期结果**：线程 0 计算全局 0、1 号元素，线程 1 计算全局 2、3 号元素；两个线程都只访问各自的 `x_buf[0..1]`、`y_buf[0..1]`（局部下标），但合起来覆盖了全部 4 个全局元素。这正是「逐线程展开」的精髓。

**动手验证（待本地验证）**：写一个最小 Tilus 加法内核，开 `tilus.option.debug.dump_ir()`，在缓存目录里找到生成的 `source.cu`，确认每个线程的 for 循环体确实只读写自己的 `regs[i]`，且 `get_global` 的结果（经优化后）对应到上表的全局坐标。

#### 4.2.5 小练习与答案

- **练习 1**：为什么 elementwise 发射器只注册在 `ElementwiseBinaryBaseInst` 基类上，就能服务 `AddInst`、`MulInst` 等所有子类？
  - **答案**：发射器把「怎么遍历」固定下来，把「算什么」委托给 `inst.f_compute`；而 u6-2 的派单用 `issubclass` 匹配，子类没有自己的发射器就会命中基类的发射器（派单按继承链取首个命中）。
- **练习 2**：若把上面例子的线程数从 2 改成 4（布局 `spatial(4).local(1)`），每个线程算几个元素？
  - **答案**：`local_size=1`，每个线程只算 1 个全局元素（线程 \(t\) 算全局第 \(t\) 号），循环只跑 1 次。
- **练习 3**：一元发射器里为什么没有 `broadcast_indices` / `get_local`？
  - **答案**：一元的输入输出同形同布局，局部下标 `i` 直接通用，无需坐标换算。

### 4.3 reduce 发射器：三段式跨线程规约

#### 4.3.1 概念说明

`ReduceInst` 沿某个维度 `dim` 做 `sum/max/min/any/all` 规约。难点在于：被规约的元素**散落在多个线程**里（既可能在同一线程的不同局部槽，也可能跨 lane、甚至跨 warp）。reduce 发射器把这次规约拆成三个粒度，逐级合并：

1. **线程内（intra-thread）**：每个线程先把属于自己的、落在规约维上的若干局部元素合并。
2. **warp 内（intra-warp）**：用 warp shuffle（蝴蝶规约）合并同一 warp 内、映射到规约维的 lane。
3. **跨 warp（inter-warp）**：若规约维还跨越了多个 warp，则借助共享内存合并；否则用 shuffle 把 warp 内的结果广播回所有 lane。

#### 4.3.2 核心流程

`emit` 直接调用 `efficient_reduce`（[reduce.py:343-357](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/backends/emitters/reduce.py#L343-L357)），其顺序是：

```
intra_thread_reduce          # 先合并本线程局部元素
intra_warp_reduce            # 再用 shfl_down 蝴蝶合并 warp 内
if requires_inter_warp_reduction(inst):
    inter_warp_reduce        # 跨 warp，用共享内存
else:
    intra_warp_broadcast     # 不跨 warp，用 shfl_up 把结果广播回各 lane
```

**蝴蝶规约的数学**：warp 内用 `shfl_down_sync(var, delta=2^k, width=2^(k+1))`。第 \(k\) 轮（\(k=0..4\)，因为 warp 有 32 个 lane，\(2^5=32\)）让每个 lane 收到「落后 \(2^k\) 个 lane」的值并合并，等价于一棵二叉树规约：

\[
a_\ell \leftarrow a_\ell \oplus a_{\ell + 2^k}
\]

只有当第 \(k\) 位属于「规约维」时这一轮才有效——这正是 `check_whether_spatial_bit_reduced` 判定的。

**跨 warp 的难点**：哪些 mode 是「warp 维」（决定 warp_id）、哪些是「lane 维」（决定 lane_id），由 `analyze_modes` 把每个空间 mode 归类为 `replicated`/`reduced`/`spatial`，并在跨越 warp/lane 边界处把一个 mode 拆成两段。

#### 4.3.3 源码精读

**运算语义**由两个表函数承担（[reduce.py:38-68](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/backends/emitters/reduce.py#L38-L68)）：`scalar_init_value` 给单位元（sum→0、max→最小值、any→false），`scalar_reduce` 给两元合并（sum→`+`、any→按位或）。注意 `any/all` 刻意用 `bitwise_or/and` 而非逻辑或/与，避免短路求值影响 IR。

**线程内规约**（[reduce.py:70-93](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/backends/emitters/reduce.py#L70-L93)）先把输出清零成单位元，再遍历输入的每个局部槽：把输入的局部多维下标去掉规约维后映射到输出的局部下标，逐个合并：

```python
with self.for_range(src.layout.local_size, attr="u") as src_local:
    src_local_indices = index_deserialize(src_local, shape=src_local_shape)
    dst_local_indices = [i for d, i in enumerate(src_local_indices) if d not in reduced_local_dims]
    dst_local = index_serialize(dst_local_indices, shape=dst_local_shape)
    self.buffer_store(dst_buf, [dst_local],
                      value=self.scalar_reduce(dst_buf[dst_local], src_buf[src_local], inst.op))
```

> 这里用 `attr="u"` 提示编译器展开循环（参见 [stmt.py:77-92](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/hidet/ir/stmt.py#L77-L92)）：`"u"` 是 unroll 提示，`"u+"` 是由 hidet 显式展开。

**warp 内规约**（[reduce.py:124-149](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/backends/emitters/reduce.py#L124-L149)）对 5 个 lane 位逐一判定「这一位是否属于规约维」，是则做一次 `shfl_down` 合并；`intra_warp_broadcast`（[reduce.py:151-169](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/backends/emitters/reduce.py#L151-L169)）反向用 `shfl_up` 把结果复制回所有 lane（因为规约后只有部分 lane 持有正确结果，但下游可能要求每个 lane 都拿到）。

**跨 warp 规约**（[reduce.py:263-341](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/backends/emitters/reduce.py#L263-L341)）借助共享内存，流程是 u6-3 提到的 `smem_alloc_ctx` 申请一块 workspace：先把各 warp 的局部结果写进共享内存（按 `[warp, lane, local]` 三维布局，见 `determine_shared_layout`），`self.sync()` 同步，再让每个 warp/lane 从共享内存读回合并后的值。其中 `analyze_modes`（[reduce.py:185-225](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/backends/emitters/reduce.py#L185-L225)）用 `@lru_cache` 缓存了 mode 分类结果，避免重复分析。

#### 4.3.4 代码实践（源码阅读型）

**实践目标**：理解三段式的判定与同步插入。

1. 读 [reduce.py:171-183](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/backends/emitters/reduce.py#L171-L183) 的 `requires_inter_warp_reduction`：当规约维的某个空间 mode 落在「warp 维」且规模不足 `num_warps` 时返回 `True`。
2. 读 [reduce.py:263-341](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/backends/emitters/reduce.py#L263-L341)，找出两处 `self.sync()` 各自夹在哪两步之间。

**需要观察的现象 / 预期结果**：你会看到「写共享内存 → sync → 读共享内存 → sync」的经典模式，且 `self.sync()` 实际调用的同步原语由 u6-3 的 `sync_ctx` 按线程组规模决定。待本地验证：对一个沿列规约的内核开 `dump_ir`，在 IR 里定位 `shfl_down_sync` 与 `__syncthreads`。

#### 4.3.5 小练习与答案

- **练习 1**：`any/all` 为什么用 `bitwise_or/and` 而不是 `logical_or/and`？
  - **答案**：逻辑运算有短路语义，在 IR/PTX 层会引入控制流分支；按位运算对所有 lane 一致执行，更适合 SIMT。
- **练习 2**：`intra_warp_broadcast` 什么时候会被调用？
  - **答案**：当 `requires_inter_warp_reduction` 为 `False`（规约维不跨 warp）时，`efficient_reduce` 走 else 分支，用 `shfl_up` 把 warp 内已规约的结果广播回所有 lane。

### 4.4 ldst 发射器：全局访存与向量化

#### 4.4.1 概念说明

`ldst.py` 负责全局内存的 `LoadGlobalGenericInst` / `StoreGlobalGenericInst`。它的输入是 u5-4 降级后的产物：一个标量指针 `ptr`、一组符号坐标 `axes`、一个用 `axes` 表达的偏移表达式 `offset`、以及一个越界掩码 `mask`。发射器要做的是：对每个线程、每个局部元素，算出它该访问的全局地址与掩码，再生成 load/store。

这里多了一项 elementwise 没有的优化：**向量化（vectorization）**——如果连续若干元素「同一线程持有、全局地址连续、局部存储连续、掩码相同」，就把它们合并成一次更宽的访存（如 4 字节、8 字节、16 字节），大幅减少访存指令数。

#### 4.4.2 核心流程

`emit` 先调 `analyze_vectorization` 判断能否向量化（[ldst.py:33-90](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/backends/emitters/ldst.py#L33-L90)）。它逐维取一个 `max_vector_elements`，等于以下五个量的**最大公约数**（`gcd`），任意一个不满足都会把向量长度压下来：

1. `offset_info[i].divisibility`——偏移对该向量长度的整除性（地址必须对齐）。
2. `offset_info[i].continuity`——全局地址沿该维连续。
3. `layout_info[i].continuity`——局部存储沿该维连续（即这些元素归同一线程）。
4. `mask_info[i].constancy`——掩码对这些元素相同。
5. `layout.local_size`——局部槽总数能被向量长度整除。

若得到 `max_vector_elements > 1` 且总位宽是字节的整数倍，就选定该维向量化，按向量长度把循环改成「一次搬若干字节」。

#### 4.4.3 源码精读

向量化分支（[ldst.py:105-141](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/backends/emitters/ldst.py#L105-L141)）的核心思路：

```python
vectorization = self.analyze_vectorization(inst)
if vectorization:
    vectorize_dimension, vector_bytes = vectorization
    total_nbytes = layout.local_size * dtype.nbits // 8
    with self.for_range(extent=total_nbytes // vector_bytes) as vec_i:
        start_i = vec_i * vector_bytes * 8 // dtype.nbits          # 本向量首个局部元素
        global_indices = layout.get_global(local_index=start_i, spatial_index=self.current_thread)
        rewrite_map = {axis: as_expr(g) for axis, g in zip(inst.axes, global_indices)}
        offset = rewrite(inst.offset, rewrite_map=rewrite_map)     # 把符号坐标替换成具体坐标
        mask    = rewrite(inst.mask,  rewrite_map=rewrite_map) if inst.mask is not None else boolean.true
        # 把向量拆成 1/2/4/8/16 字节的 unit，按 unit_dtype 搬运
        unit_bytes = gcd(vector_bytes, 16)
        unit_dtype = {1: uint8, 2: uint16, 4: uint32, 8: uint32x2, 16: uint32x4}[unit_bytes]
        ...
```

要点：

- 又一次出现 `get_global(local_index, spatial_index=current_thread)`——elementwise 的同款套路，只是这里把得到的坐标代回 `inst.offset`（一次符号 `rewrite`）算出真实地址。
- 向量被进一步切成 1/2/4/8/16 字节的「unit」，用对应的整数/向量类型（`uint32x4` 即一次 16 字节）做搬运；这呼应了 CLAUDE.md 提到的「任意位宽低精度」——sub-byte 类型也能凑成整字节向量。
- load 时掩码为假要写零（`unit_dtype.zero`），store 时掩码为假直接跳过（见 [ldst.py:142-152](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/backends/emitters/ldst.py#L142-L152) 的 else 分支）。

不能向量化时退化为逐元素版本（[ldst.py:142-152](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/backends/emitters/ldst.py#L142-L152)），结构同 elementwise 二元：循环 + `get_global` + `rewrite` + `buffer_store`。

> 两个发射器装饰器叠用（`@register_emitter(LoadGlobalGenericInst)` 与 `@register_emitter(StoreGlobalGenericInst)` 装饰同一个类，[ldst.py:93-95](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/backends/emitters/ldst.py#L93-L95)）让 load 与 store 共用一份向量化分析与地址计算逻辑，差异只在搬运方向。

#### 4.4.4 代码实践（源码阅读型）

**实践目标**：理解向量化的五个约束如何共同决定向量长度。

1. 读 [ldst.py:74-90](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/backends/emitters/ldst.py#L74-L90)，对照 `analyze_vectorization` 里 `gcd(...)` 的五个参数。
2. 思考：若某维 `mask` 逐元素不同（边界 tile），`mask_info[i].constancy` 会是多少？

**需要观察的现象 / 预期结果**：边界处 `constancy=1`，于是该维无法向量化，`analyze_vectorization` 会尝试其它维或返回 `None` 走逐元素路径。待本地验证：对齐尺寸（如 `n % block == 0`）的 load 生成的 `source.cu` 应出现 `uint4`/`float4` 宽访存，而非对齐时退化为标量 load。

#### 4.4.5 小练习与答案

- **练习 1**：向量化要求「元素归同一线程」对应五个约束里的哪一个？
  - **答案**：`layout_info[i].continuity`（局部存储沿该维连续，意味着这些全局元素由同一线程持有）。
- **练习 2**：load 在掩码为假时写零、store 在掩码为假时跳过，为何策略不同？
  - **答案**：load 的输出寄存器必须有一个确定值供下游使用，越界时只能填零；store 越界则不能写任何地址，直接用 `if(mask)` 守卫跳过。

### 4.5 shared_ldst 发射器：通用搬运与 ldmatrix/stmatrix 硬件加速

#### 4.5.1 概念说明

`shared_ldst.py` 负责共享内存与寄存器之间的搬运（`LoadSharedInst` / `StoreSharedInst`）。它比 ldst 多一条「快路径」：NVIDIA GPU 提供专门的矩阵搬运指令——`ldmatrix`（sm_75+，把共享内存里的一小片矩阵按 MMA 所需的 lane 分布一次性加载进寄存器）和 `stmatrix`（sm_90+，反向）。如果寄存器布局恰好匹配这些指令要求的「原子布局」，就用硬件指令一次搬一小片；否则回退到与 ldst 同款的通用向量化搬运。

这种「先试硬件指令，不行再回退」的设计，正是 u6-2 讲的「同一指令按 target 挂不同发射器」的体现：`LoadSharedInst` 在 `nvgpu_sm75` 上挂的是带 `ldmatrix` 快路径的发射器，默认 target 上挂的是纯通用发射器。

#### 4.5.2 核心流程

`LoadSharedInstLdmatrixEmitter.emit` 的决策树（[shared_ldst.py:279-298](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/backends/emitters/shared_ldst.py#L279-L298)）：

```
config = _get_load_matrix_config(dtype, register_layout)   # 布局能否整除某 ldmatrix 原子布局？
if config is not None and _check_shared_alignment_and_contiguity(...):   # 共享内存对齐且连续？
    self._emit_ldmatrix(inst, config)                      # 走硬件快路径
else:
    _emit_generic_load_shared(self, inst)                  # 回退通用向量化
```

`_get_load_matrix_config` 用布局代数的 `divide`（u4-4）判定：把寄存器布局除以候选的 `ldmatrix_layout`，能整除（不抛 `LayoutOperationError`）就说明整片布局可由若干 `ldmatrix` 原子拼成。候选原子布局见 [ldmatrix.py:30-38](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/layout/cuda/ldmatrix.py#L30-L38)，如 fp16 的 `spatial(8,4).local(1,2)`。

#### 4.5.3 源码精读

**通用回退** `_emit_generic_load_shared`（[shared_ldst.py:167-219](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/backends/emitters/shared_ldst.py#L167-L219)）与 ldst 几乎同构：同样先 `_analyze_vectorization_for_shared`（[shared_ldst.py:119-164](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/backends/emitters/shared_ldst.py#L119-L164)）做向量化分析，差别只在于地址来自 `shared_tensor.layout(*axes)`（共享布局把逻辑坐标映射成共享内存偏移），而非 `inst.offset`。

**`ldmatrix` 快路径** `_emit_ldmatrix`（[shared_ldst.py:300-343](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/backends/emitters/shared_ldst.py#L300-L343)）则完全不同，它直接发射 PTX 的 `ldmatrix` 原语：

```python
smem_base_addr = self.declare_var("smem_addr", int32,
                    init=self.shared_tensor_shared_space_addr[shared_tensor])   # 共享内存基址
byte_offset = shared_tensor.layout.byte_offset(*axes, nbytes=dtype.nbytes)      # 字节偏移
with self.for_range(num_vectors, attr="u+") as vec_i:
    regs = [cast(~regs_buf[...], ~uint32) for i in range(vector_size)]          # 每线程提供 4 个 u32 寄存器
    lane_id = self.current_thread % 32
    warp_id = self.current_thread // 32
    lhs_indices = lhs_layout.get_global(local_index=..., spatial_index=warp_id) # 哪一小片
    rhs_indices = vector([lane_id % 8, 0])                                       # 片内行
    shared_indices = list(lhs_indices * rhs_shape + rhs_indices)
    smem_addr = smem_base_addr + rewrite(byte_offset, {axis: idx for ...})
    self.append(ldmatrix(regs=regs, smem_addr=smem_addr, shared_space_addr=True, trans=config.trans))
```

要点：

- `ldmatrix` 是**warp 级**指令：一个 warp 的 32 个 lane 协作，每个 lane 提供一个共享内存地址（指向它负责的那 16 字节行），硬件按 MMA 所需分布把数据散布到各 lane 的寄存器。所以这里用 `lane_id % 8` 选片内行、`warp_id` 选片。
- `attr="u+"` 表示由 hidet **显式展开**这个循环（每条 `ldmatrix` 处理一小片，整片布局由若干条拼成）。
- `stmatrix` 路径（[shared_ldst.py:382-439](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/backends/emitters/shared_ldst.py#L382-L439)）是 `ldmatrix` 的镜像，注释明确指出它**只适用于非 swizzle 的共享布局**（因为 `stmatrix` 从给定地址顺序写 16 字节，见 [shared_ldst.py:423-424](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/backends/emitters/shared_ldst.py#L423-L424)）。

`_check_shared_alignment_and_contiguity`（[shared_ldst.py:84-116](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/backends/emitters/shared_ldst.py#L84-L116)）是第二道闸门：即便布局匹配，也要保证共享内存那片数据是 16 字节对齐且连续的（`ldmatrix` 要求 16 字节对齐），否则仍回退通用路径。

#### 4.5.4 代码实践（源码阅读型）

**实践目标**：看清「硬件快路径 → 通用回退」的双层结构。

1. 读 [shared_ldst.py:275-298](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/backends/emitters/shared_ldst.py#L275-L298) 与 [shared_ldst.py:346-351](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/backends/emitters/shared_ldst.py#L346-L351)，注意同一个 `LoadSharedInst` 挂了两个发射器：`nvgpu_sm75` 上的带快路径、默认的纯通用。
2. 对照 [ldmatrix.py:30-38](https://github.com/NVIDIA/tilus/blob/9a22de0aac52beb8343c1f2511135f351848afbd/python/tilus/ir/layout/cuda/ldmatrix.py#L30-L38)，理解 `ldmatrix_layout` 的形状（如 `spatial(8,4)`）正是 MMA 所需的 lane 分布。

**需要观察的现象 / 预期结果**：在 sm_80 target 上编译一个带共享内存加载的 matmul（如 `examples/matmul/matmul_v3.py`），开 `dump_ir`，应在 `source.cu` 中看到 `ldmatrix.sync.aligned` 指令；若把布局改成 ldmatrix 不兼容的形状，则应看到退化为普通 `st`/`ld` 的向量化搬运。待本地验证。

#### 4.5.5 小练习与答案

- **练习 1**：为什么 `LoadSharedInst` 要挂两个不同的发射器（一个带 `target=nvgpu_sm75`，一个默认）？
  - **答案**：`ldmatrix` 只在 sm_75 及以上可用；u6-2 的 `match_target` 会按当前 target 选算力最高且支持的发射器，于是新架构走快路径、旧架构或非 NVIDIA target 走纯通用回退。
- **练习 2**：`stmatrix` 为什么只适用于非 swizzle 的共享布局？
  - **答案**：`stmatrix` 从给定地址**顺序**写 16 字节，而 swizzle（u4-3）会打乱字节到 bank 的映射，顺序写会破坏 swizzle 排布，故必须回退。
- **练习 3**：`_get_load_matrix_config` 用哪个布局运算判定兼容性？
  - **答案**：用 `divide(register_layout, ldmatrix_layout)`（u4-4 的布局除法），能整除即兼容。

## 5. 综合实践

把本讲四类发射器串起来，做一个「读 IR、对代码」的端到端练习：

1. **选目标**：在 `examples/matmul/` 里挑一个用到共享内存的版本（如 `matmul_v3.py`），它天然包含 elementwise（`cast`）、reduce（若有）、global load/store、shared load（`ldmatrix`）四类指令。
2. **开调试**：设置 `tilus.option.cache_dir("my-cache")` 与 `tilus.option.debug.dump_ir()` 后运行。
3. **定位发射器产物**：在 `my-cache/.../source.cu` 里分别找出：
   - 一段「逐线程 for 循环 + 标量运算」——多半来自 elementwise 或通用 ldst；
   - 一段 `ldmatrix.sync`——来自 shared_ldst 的快路径；
   - 一段宽访存（如 `float4`/`uint4`）——来自 ldst 的向量化分支。
4. **手动推演对照**：选其中一个逐线程循环，按 4.2.4 的方法，用内核实际的 `block_m/block_n` 与线程数推演「线程 0、i=0」应访问的全局坐标，再与 `source.cu` 里的地址表达式比对。
5. **扰动观察（待本地验证）**：把分块改成 ldmatrix 不兼容的形状，重新编译，确认 `ldmatrix` 消失、退化为通用搬运，体会「快路径 → 回退」的切换。

这个练习把「布局 → 每线程标量运算」「向量化」「硬件指令快路径」三条主线一次性走通。

## 6. 本讲小结

- 通用发射器的**共同套路**：用 `layout.get_global(spatial_index=current_thread, local_index=i)` 把张量布局翻译成「每个线程处理哪些全局元素」，外层套一个 `local_size` 次的 `for_range`，循环体里算地址、做运算、`buffer_store` 写回。
- **elementwise** 把「遍历」与「运算」解耦：发射器管遍历，`inst.f_compute` 管运算；一元直接按局部下标，二元多一次 `broadcast_indices` + `get_local` 的坐标换算。
- **reduce** 按粒度三段式：线程内合并局部元素 → warp 内 `shfl_down` 蝴蝶规约 →（视情况）跨 warp 共享内存规约或 `shfl_up` 广播。
- **ldst** 在共同套路上加了**向量化分析**：用 `gcd` 综合地址整除性/连续性、局部连续性、掩码恒定性、`local_size` 五个约束定出最大向量长度，再用 1/2/4/8/16 字节 unit 做宽访存。
- **shared_ldst** 是「硬件快路径 + 通用回退」的样板：布局能被 `ldmatrix`/`stmatrix` 原子布局整除且共享内存对齐连续时走 PTX 矩阵搬运指令，否则回退到与 ldst 同构的向量化搬运；同指令按 target 挂多个发射器。

## 7. 下一步学习建议

- **U7 架构实践**：本讲的 `ldmatrix`/`stmatrix`、`shfl` 规约、向量化访存都是「零件」；U7 把它们组装成 Ampere/Hopper/Blackwell 上的完整 matmul，建议接着读 `examples/matmul/matmul_v3.py`（ldmatrix + MMA）与 `examples/hopper_matmul/`（wgmma + cp_async）。
- **硬件专用发射器**：本讲只覆盖「通用」发射器；`python/tilus/backends/emitters/cuda/` 下的 `mma_dot.py`、`wgmma.py`、`cp_async*.py`、`tcgen05/` 是张量核与异步搬运的专用发射器，是 U7 的源码底座，可作为进阶阅读。
- **布局系统回看**：若你对 `get_global`/`divide`/`cover` 还不熟，建议回看 u4-2/u4-4——本讲所有「逐线程翻译」的可读性都建立在布局代数之上。
- **动手扩展**：尝试仿照 elementwise 发射器，写一个只读不改的 `IRVisitor`，统计某内核里各类发射器各贡献了多少条标量语句，作为理解编译产物的练习。
