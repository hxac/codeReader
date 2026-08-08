# layout_utils 布局代数

## 1. 本讲目标

本讲是「核心工具层」的第二讲（承接 u3-l1 拷贝工具）。上一讲我们说「一个 `cute.Tensor` = 指针 + 布局」，并且所有 `partition_S`/`partition_D` 都依赖布局。本讲就专门拆解**布局本身**——`quack/layout_utils.py` 这一组「不真正搬数据、只改坐标映射」的布局代数工具。学完后你应该能够：

- 用一句话说清 CuTe **Layout（布局）** 是什么，并用 `shape`/`stride` 写出「逻辑坐标 → 线性偏移」的映射。
- 掌握 `transpose_view` / `select` / `expand` 三个**纯视图变换**：它们不改一字节数据，只换「怎么看待同一块存储」。
- 理解 **zero-stride（零步长）模式**：为什么一个 (4,4) 的逻辑张量可以只占 4 个寄存器，以及 `convert_layout_zero_stride` 如何把行/列向量归约在寄存器里正确累加。
- 了解 **gated（门控）输出**为何需要 `permute_gated_Cregs_b16` 这类**寄存器数据搬移**（不是纯布局，而是 warp 内 shuffle），以及它与「插值权重布局」`concat_to_interleave` 的配合。

本讲是后续 GEMM epilogue（u6）、blockscaled 量化（u7）的铺路：epilogue 的行/列向量归约、MLP 的 gated 融合、back-to-back GEMM 的累加器重排，全部建立在本讲的布局代数之上。

## 2. 前置知识

阅读本讲前，建议你已经建立以下认知（对应 u1-l4、u2-l1、u3-l1）：

- **静态值与动态值**（u1-l4）：CuTe-DSL 在编译期就要确定布局的所有 `shape`/`stride`；`const_expr(...)` 把判断标记为编译期分支，只编入命中分支。
- **CuTe 张量与布局**（u3-l1）：`cute.Tensor = 指针(iterator) + 布局(layout)`。**布局（Layout）** 是一个 `(shape, stride)` 的（可嵌套）序列，描述「逻辑坐标 → 线性偏移」的仿射映射。改布局 = 换一种坐标视角，**不动底层存储**。
- **MMA 累加器布局**：一次张量核心（MMA/WGMMA/tcgen05）算出的结果片段（fragment）有一套由指令硬件决定的、看起来很「绕」的布局，例如 SM80 的 `((2, 2), MMA_M, MMA_N)`、SM90 的 `((2, 2, V), MMA_M, MMA_N)`。人很难直接用它写 `for m: for n:` 的逐元素循环，需要先「拍平」成 `(M, N)` 形式。
- **warp / lane / shuffle**：一个 warp 32 个线程（lane 0~31）；`shuffle_sync` 让同一 warp 内的 lane 互换寄存器值，是「寄存器之间搬数据」的唯一无锁手段。

几个本讲会反复用到的术语：

| 术语 | 含义 |
|------|------|
| Layout | `(shape, stride)` 描述的「坐标 → 偏移」仿射映射 |
| zero-stride（零步长）模式 | `stride == 0` 的模式：多个逻辑坐标映射到同一存储位置（广播）|
| `cute.composition` | 把一个布局「叠加」到张量上，得到同存储的新视图 |
| `cute.flatten` | 把嵌套布局拍平成一串叶子模式 `(s0:s0', s1:s1', ...)` |
| 累加器 fragment（C-regs）| MMA 结果寄存器片段，布局由指令硬件决定 |
| colvec / rowvec | 沿 M（列向量）/ N（行向量）方向的归约目标 |

> **关于「permute（重排）」一词的说明**：本讲把 `layout_utils.py` 里的工具分成两类——一类是**纯布局视图**（`transpose_view`/`select`/`expand`/`convert_layout_*`，零数据搬移，只改坐标）；另一类是**寄存器数据搬移**（`permute_gated_Cregs_*`/`permute_Cregs_b32_*`，用 `shuffle_sync` 真的在 lane 间移动寄存器值）。最小模块里提到的「permute」特指后者，本讲 4.3 专讲。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
|------|------|-----------|
| [quack/layout_utils.py](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/layout_utils.py) | 全项目共享的布局代数工具集合 | `transpose_view`/`select`/`expand`/`concat_to_interleave`/`convert_layout_zero_stride`/`permute_gated_Cregs_*` |
| [quack/broadcast_utils.py](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/broadcast_utils.py) | 行/列向量与累加器的逐元素融合 | `reshape_acc_to_mn` 的真实调用，体会「拍平成 (M,N)」的必要性 |
| [quack/epilogue/ops.py](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/ops.py) | 可组合 epilogue 的 EpiOp 词汇 | `convert_layout_zero_stride` 的真实调用点（`colvec_reduce_accumulate` 等向量归约）|
| [quack/epilogue/quantize_out.py](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/quantize_out.py) | 量化输出 epilogue | `tile_atom_to_shape_SF_strided` 的真实调用（布局构造）|
| [quack/rmsnorm.py](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/rmsnorm.py) | RMSNorm 内核 | `expand` 的真实调用（给权重/偏置造广播视图）|

阅读建议：先看 4.1 建立「布局 = 坐标映射、视图变换不动存储」的直觉；再看 4.2 攻克本讲核心 `convert_layout_zero_stride`（含要求的 (4,4) 手算）；最后看 4.3 了解 gated 输出如何把「布局变换」与「寄存器搬移」组合起来。

## 4. 核心概念与源码讲解

### 4.1 transpose_view / select / expand：纯布局视图变换

#### 4.1.1 概念说明

CuTe 里最强大（也最易让初学者迷惑）的一点是：**改布局不等于搬数据**。给定一块已经填好数据的存储（gmem/smem/rmem 都行），你可以给它「贴」上任意多个布局视图，每个视图用不同的 `shape`/`stride` 去解释同一块字节。只要这些视图与存储相容（`cute.composition` 会校验），读取/写入就是合法的，且**零拷贝开销**——编译后就是不同的地址计算。

`layout_utils.py` 顶部三个函数就是这套「纯视图」工具：

- **`transpose_view`**：转置前两个维度。常用于「B 矩阵以 N-major 存储，但我的 MMA 想把它当 MN-major 来分块」的场景。
- **`select`**：按给定的 `mode` 序列**重排/挑选**模式（维度）。例如 `[1, 0, 2]` 把第 0、1 维互换；这在 `swap_ab`（A/B 操作数交换）等场景下用来给操作数张量换一个维度顺序。
- **`expand`**：在指定位置**插入一个 size 给定、stride=0 的维度**——也就是广播。一个长度为 N 的行向量 `expand` 到 `(M, N)` 后，每一行都指向同一份数据（零步长）。

三者共同点：都用 `cute.make_tensor(iterator, 新布局)` 重新包装**同一个 iterator（指针/存储）**，只换「坐标→偏移」的映射，数据一字未动。这正是「布局代数」最省的地方——很多看似需要新内核的操作，其实只是换个视图。

#### 4.1.2 核心流程

三个函数的「改映射」逻辑：

```text
transpose_view(a):  新 shape=(a.shape[1], a.shape[0], *a.shape[2:])
                    新 order=(1, 0, *range(2, rank))     # 交换前两个轴的访问顺序
                    return composition(a, make_ordered_layout(新shape, order))

select(a, mode):     return make_tensor(a.iterator, cute.select(a.layout, mode))
                    # 按mode重排a.layout的各模式

expand(a, dim, size): 新 shape = (*a.shape[:dim], size, *a.shape[dim:])   # 在dim插入
                      新 stride = (*a.stride[:dim], 0, *a.stride[dim:])    # 插入零步长
                      return make_tensor(a.iterator, make_layout(新shape, 新stride))
```

直觉：

- `transpose_view` 用 `order=(1,0,...)` 造一个「先走第 1 维、再走第 0 维」的有序布局，再 `composition` 回原张量——等价于把行列访问顺序对调，常用于 B 操作数。
- `select` 直接调用 CuTe 内置的 `cute.select(layout, mode)`，把布局的模式按 `mode` 序列重排（数学上是对模式的一个置换）。
- `expand` 的关键就是新维度的 `stride = 0`：沿这个维度走，偏移不增加，于是同一个物理元素被「复制」成 `size` 份逻辑元素。这正是后面 4.2 zero-stride 的雏形。

#### 4.1.3 源码精读

`transpose_view`——转置前两维，用 `composition` 把新有序布局叠回原张量（同存储、新坐标）：

[quack/layout_utils.py:L10-L14](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/layout_utils.py#L10-L14) — 构造 `shape=(N, M, ...)`、`order=(1, 0, ...)` 的有序布局，`cute.composition(a, ...)` 把它贴回 `a`，得到转置视图。注意它复用 `a.iterator`，不分配新存储。

`select`——按 `mode` 重排模式（一行实现，调 CuTe 内置 `cute.select`）：

[quack/layout_utils.py:L17-L18](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/layout_utils.py#L17-L18) — 用 `cute.select(a.layout, mode)` 交换/挑选模式后重新 `make_tensor`。真实调用如 GEMM 的 `swap_ab` 路径会把 `mA`、`mB` 用 `[1, 0, 2]` 互换前两维（见 [gemm_base.py:L162-L164](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_base.py#L162-L164)）。

`expand`——插入一个零步长（广播）维度：

[quack/layout_utils.py:L34-L37](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/layout_utils.py#L34-L37) — 在 `dim` 处插入 `size` 大小、**stride=0** 的模式。真实调用如 RMSNorm 把权重 `mW`、偏置 `mT` 这类行向量 `expand` 到 `(tiler_mn[0], N)`，让一行权重广播到 CTA 覆盖的每一行（见 [rmsnorm.py:L131-L135](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/rmsnorm.py#L131-L135)）。

> **顺带一提**：本文件里还有两族「累加器布局转换」也属于纯视图工具——`convert_layout_acc_mn`/`reshape_acc_to_mn`（[L204-L240](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/layout_utils.py#L204-L240)）把硬件的 `((2,2), MMA_M, MMA_N)` 拍平成 `(M, N)`；`convert_layout_acc_frgA`/`reshape_acc_to_frgA`（[L243-L287](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/layout_utils.py#L243-L287)）把前一个 GEMM 的累加器布局转成后一个 GEMM 的 A 输入布局（back-to-back GEMM，真实调用见 [gemm_sm90.py:L1833](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_sm90.py#L1833)）。它们都是「换个坐标视角」，与 `transpose_view` 同类，本节不展开，4.2 会用到 `reshape_acc_to_mn` 的产物。

#### 4.1.4 代码实践

**实践目标**：在源码里找到 `transpose_view` / `select` / `expand` 的真实调用，验证「换视图不搬数据」。

**操作步骤**：

1. 打开 [broadcast_utils.py:L18](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/broadcast_utils.py#L18)，看 `reshape_acc_to_mn(tCrC_f32)` 如何把硬件累加器拍平成 `(M, N)`，之后才能写 `for r: tCrC_f32_mn[r, None]` 这种逐行循环。
2. 打开 [rmsnorm.py:L131-L135](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/rmsnorm.py#L131-L135)，看 `expand(mT, dim=0, size=tiler_mn[0])` 如何把一个行向量广播成「每行都一样」。思考：如果不用 `expand` 而是真复制 `tiler_mn[0]` 份数据，会有什么浪费？
3. 打开 [gemm_base.py:L162-L167](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_base.py#L162-L167)，看 `select(mA, [1, 0, 2])` 如何在 `swap_ab` 时把 A、B 的前两维互换，使同一套内核能同时处理 `D=A@B` 与 `D=B@A`。

**需要观察的现象**：三处调用都没有 `cute.copy`、没有新分配；它们只是把同一个 iterator 包进新布局。`expand` 后的张量逻辑尺寸变大了，但 `cosize`（实际占用）没变——多出来的维度是零步长。

**预期结果**：你能解释「为什么 RMSNorm 的 weight 只要存 N 个数、却能让每个线程读到自己那一行的 weight」——靠的就是 `expand` 的零步长广播，所有线程的 (row, col) 中 `col` 走真实步长、`row` 走零步长，于是同一列的 weight 在每行重复。

> 这是源码阅读型实践；结论可在阅读上述三处后直接得出。

#### 4.1.5 小练习与答案

**练习 1**：`expand` 插入的维度 `stride=0`，从「地址计算」角度意味着什么？

**参考答案**：偏移 = Σ 坐标ᵢ × strideᵢ。新维度 stride=0，所以无论它的坐标取 0 还是 size−1，对偏移贡献都是 0；多个逻辑坐标映射到同一物理位置——即广播。这正是「一个 weight 被多个行看到」的免费实现。

**练习 2**：`transpose_view` 用 `composition(a, 新布局)` 而不是 `make_tensor(a.iterator, 新布局)`，两者有何区别？为什么转置要用 `composition`？

**参考答案**：`make_tensor(iterator, layout)` 直接用新布局解释存储，要求布局与存储「相容」（一一线性可寻址）；`composition(a, layout)` 是把 `layout` 叠加在**张量 a 已有的布局**之上，先按 a 的布局算出 a 内的偏移、再用新布局索引——适合「a 本身已有非平凡布局（如 swizzle、转置）」的场景。转置往往作用于已有 smem 布局的张量，`composition` 才能正确复合两层坐标映射。

**练习 3**：`select(a, [1, 0, 2])` 与 `transpose_view(a)` 都能「换前两维」，它们适用场景有何不同？

**参考答案**：`transpose_view` 专门转置前两维、用 `composition`、常用于 smem 上的 B 操作数；`select` 是通用的模式置换（可任意重排任意多个模式），且直接 `make_tensor(iterator, cute.select(layout, mode))`，常用于主机侧给 gmem 操作数张量换维度顺序（如 `swap_ab`、AllGather 的 shard 旋转 `[1, 2, 0]`）。一个偏「设备 smem 视图」，一个偏「张量模式重排」。

---

### 4.2 convert_layout_zero_stride：把归约写进寄存器

#### 4.2.1 概念说明

本模块是本讲核心，也是要求的实践主题。先建立一个关键直觉：**一个 (4,4) 的逻辑张量不一定要占 16 个存储位置**。如果它的某个维度是 zero-stride（零步长），那么 16 个逻辑坐标会「折叠」到更少的物理槽里。

GEMM epilogue 里有一类操作叫**向量归约（VecReduce）**：把一个 (M, N) 的累加器片段沿 N 方向求和得到「行向量」（RowVecReduce，结果长度 M），或沿 M 方向求和得到「列向量」（ColVecReduce，结果长度 N）。难点在于：

1. 累加器片段的硬件布局很「绕」（`((2,2,V), MMA_M, MMA_N)`），没法直接写 `for m: for n:`。
2. 归约目标（行/列向量）天然是**广播**的：一个行向量在与 (M,N) tile 做逐元素运算时，每一列共用同一个值——它的列维度是 zero-stride。

`convert_layout_zero_stride` 就是为解决这两点而生的。它接收一个输入布局和一个**参考布局（ref_layout）**，做一件事：

> **按 ref_layout 的步长模式，把所有模式分成两组——「非零步长组」（真实存储维度）与「零步长组」（广播维度）——重新打包成一个干净的二模布局 `(非零组, 零步长组)`。**

于是无论原始累加器布局多复杂，转换后你都能用 `frag[m, n]` 这种二维下标写归约循环：`m` 索引「真实存储」那一组、`n` 索引「广播」那一组。对归约缓冲（行/列向量），零步长组意味着写 `buf[m, n]` 与写 `buf[m, 0]` 落在同一个寄存器——归约结果自然「落点正确」。

> **辨析（重要）**：QuACK 里有两套归约。standalone 归约内核（softmax/rmsnorm）用 `quack/reduce.py` 的 `row_reduce`/`warp_reduce`/`online_softmax_reduce`（见 u2-l4），它们**不**用 `convert_layout_zero_stride`。本节讲的是 **GEMM epilogue 内的向量归约**（`epilogue/ops.py` 的 `ColVecReduce`/`RowVecReduce`），这才是 `convert_layout_zero_stride` 的用武之地。两者区别：前者在 smem 上对一行 gmem/smem 数据归约，后者在**寄存器片段**上对一个 MMA tile 归约。

#### 4.2.2 核心流程

`convert_layout_zero_stride(input, ref_layout)` 的算法：

```text
1. layout = input.layout （若是 Tensor 则取其 layout，否则 input 本身就是 Layout）
2. layout_flat     = flatten(layout)          # 拍平成叶子模式序列 (s0:s0', s1:s1', ...)
   ref_layout_flat = flatten(ref_layout)
3. nonzero_modes = [ i | ref_layout_flat[i].stride != 0 ]   # 参考布局里「真实存储」的模式
   zero_modes    = [ i | ref_layout_flat[i].stride == 0 ]   # 参考布局里「广播」的模式
4. new_shape  = ( tuple(layout_flat[i].shape  for i in nonzero_modes) or (1,),
                  tuple(layout_flat[i].shape  for i in zero_modes) )
   new_stride = ( tuple(layout_flat[i].stride for i in nonzero_modes) or (0,),
                  tuple(layout_flat[i].stride for i in zero_modes) )
5. out_layout = make_layout(new_shape, stride=new_stride)   # 恒为二模: (非零组, 零步长组)
6. 返回 out_layout（或 make_tensor(input.iterator, out_layout)）
```

四个要点：

- **用 ref_layout 决定分组、用 input 的真实 shape/stride 构造**。即「哪些模式算广播」由参考布局说了算，但每个模式的具体大小和步长取自输入本身。这样同一个工具既能让归约缓冲（自带零步长）得到 `((M), (N_broadcast))`，也能让输入片段（全非零步长）按相同分组对齐。
- **输出恒为二模（rank-2）**：模 0 = 非零步长组（真实存储，归约时沿这里「读取不同值」），模 1 = 零步长组（广播，归约结果写回这里、互相别名）。
- **全零步长边界**：若没有非零模式，非零组退化为 `(1,):(0,)`——一个占位的单元素广播，保证输出仍是合法的二模布局。
- **编译期全静态**：所有 `shape`/`stride` 在编译期已知，分组与重打包都在编译期完成，运行期零开销。

把它用在 ColVecReduce（沿 N 求和、结果按 M）上：

```text
# tDrReduce: 列向量归约缓冲（M 真实、N 广播，自带零步长）
# tRS_rInput: 输入累加器片段（M、N 都真实）
tDrReduce_mn = convert_layout_zero_stride(tDrReduce, tDrReduce.layout)   # ((M), (N_b))
tRS_rInput_mn = convert_layout_zero_stride(tRS_rInput, tDrReduce.layout) # 按 ref 分组
for m in range(size(tDrReduce_mn, mode=0)):        # 遍历真实存储维度 M
    row_sum = identity
    for n in range(size(tDrReduce_mn, mode=1)):    # 遍历广播维度 N（输入里是真值）
        row_sum = combine(row_sum, transform(tRS_rInput_mn[m, n]))
    tDrReduce_mn[m, 0] = row_sum                    # 写回；因零步长，[m,0]==[m,n]
```

因为 `tDrReduce_mn` 的模 1 是零步长，`[m, 0]`、`[m, 1]`、… 都指向同一个寄存器，所以「沿 n 求和后写一次」就正确落在该行的寄存器槽里——这就是「让列向量归约在寄存器里正确累加」。

#### 4.2.3 源码精读

`convert_layout_zero_stride` 的定义——本模块的核心：

[quack/layout_utils.py:L290-L313](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/layout_utils.py#L290-L313) — 注释「Group the modes with non-zero stride in the ref_layout together, and the modes with zero stride together」。注意 L293 的 `const_expr(isinstance(input, cute.Tensor))`：这个 `isinstance` 判断是**编译期**的（`const_expr`），所以同一函数既能吃 Tensor 也能吃 Layout；L300-L308 处理「全零步长」的边界（非零组退化为 `(1,):(0,)`）；L310-L313 按 input 是否为 Tensor 决定返回 Tensor 还是 Layout。

真实调用——epilogue 的列向量归约 `colvec_reduce_accumulate`（SM100 路径，用打包 fma）：

[quack/epilogue/ops.py:L1360-L1372](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/ops.py#L1360-L1372) — 把 `tDrReduce`（归约缓冲）与 `tRS_rInput`（输入片段）都转成 `_mn` 二模视图，ref 用 `tDrReduce.layout`；随后 `for m: ... for n:` 双层循环沿 N 累加进列向量缓冲。注意 L1377/L1383 的 `mul_packed_f32x2`/`fma_packed_f32x2`：转换后模 1 是偶数长度，可两两打包成 f32x2 做打包乘加——zero-stride 分组还顺带让打包成为可能。

行向量归约（沿 M 求和、结果按 N）走对称的另一条路径：

[quack/epilogue/ops.py:L1932-L1948](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/ops.py#L1932-L1948) — `convert_layout_zero_stride(tDrReduce, ...)` 得到 `_n` 视图，沿 M 归约、结果写回按 N 的行向量缓冲。

函数式 epilogue 前端的统计归约也复用同一工具——`fn_sink_flush` 把一个片段折成 `(num_rows, num_cols)` 后逐行求 partial：

[quack/epilogue/ops.py:L2366-L2374](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/ops.py#L2366-L2374) — `x_mn = convert_layout_zero_stride(frag, ref_layout)`，`num_rows = size(x_mn, mode=[0])`、`num_cols = size(x_mn, mode=[1])`，`for r: partial=identity; for c: partial=combine(partial, x_mn[r,c])`。这正是 4.2.2 伪代码的真实形态。

> 旁证：`broadcast_utils.vec_op`（[broadcast_utils.py:L18-L26](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/broadcast_utils.py#L18-L26)）做的是「广播向量与累加器的逐元素运算」（不是归约），它用 `reshape_acc_to_mn` 拍平成 (M,N) 后 `for r:` / `for c:` 配上 `tCrVec`。可见「把硬件布局拍平成 (M,N)」是 epilogue 一切逐元素/归约操作的共同前置，`convert_layout_zero_stride` 是其中处理「带广播」情况的那把刀。

#### 4.2.4 代码实践

**实践目标**（本讲要求的实践）：找到 `convert_layout_zero_stride` 的用法，解释它如何让行/列向量归约在寄存器里正确累加，并**手算一个 (4,4) zero-stride 布局的索引**。

**操作步骤**：

1. **定位调用**：在 [quack/epilogue/ops.py](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/ops.py) 搜索 `convert_layout_zero_stride`，确认它的两个主力场景：列向量归约（`colvec_reduce_accumulate`，[L1368-L1369](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/ops.py#L1368-L1369)）与行向量归约（[L1932](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/ops.py#L1932)）。注意两者都把「归约缓冲」当作 ref_layout 传进去。

2. **理解 ref 的作用**：归约缓冲（行/列向量）自带零步长维度。把它当 ref，等价于告诉 `convert_layout_zero_stride`「请按我的广播结构来分组」——于是输入片段也被切成同样的 `(真实维度, 广播维度)` 二模，两者下标天然对齐。

3. **手算 (4,4) zero-stride 布局**：考虑一个列向量归约缓冲，转换后的布局为

\[ L = \text{make\_layout}\big(\;((4,), (4,)),\;\text{stride}=((1,), (0,))\;\big) \]

即模 0（非零组）形状 4、步长 1；模 1（零步长组）形状 4、步长 0。对逻辑坐标 \((m, n)\)，\(m, n \in \{0,1,2,3\}\)，偏移为：

\[
\text{offset}(m, n) = m \cdot 1 + n \cdot 0 = m
\]

逐坐标手算得下表（16 个逻辑坐标折叠到 4 个物理槽 R0~R3）：

| 逻辑坐标 \((m,n)\) | 偏移 \(m\cdot1+n\cdot0\) | 落点寄存器 |
|------------------|------------------------|-----------|
| (0,0) (0,1) (0,2) (0,3) | 0 0 0 0 | 全在 R0 |
| (1,0) (1,1) (1,2) (1,3) | 1 1 1 1 | 全在 R1 |
| (2,0) (2,1) (2,2) (2,3) | 2 2 2 2 | 全在 R2 |
| (3,0) (3,1) (3,2) (3,3) | 3 3 3 3 | 全在 R3 |

4. **验证归约正确性**：设输入片段 `tRS_rInput_mn[m,n]` 在 16 个位置都是真实不同值 \(a_{m,n}\)。列向量归约要算 \(\text{out}[m] = \sum_{n=0}^{3} a_{m,n}\)。套用 4.2.2 的循环：

\[
\text{for } m \in \{0,1,2,3\}: \quad \text{out}[m] \mathrel{+}= a_{m,0}+a_{m,1}+a_{m,2}+a_{m,3}
\]

写回时 `tDrReduce_mn[m, 0] = out[m]`——但因零步长，`(m,0)/(m,1)/(m,2)/(m,3)` 都映射到寄存器 R_m，所以无论你写下标 `[m,0]` 还是 `[m,2]`，值都落在正确的 R_m。**这正是 zero-stride 让归约在寄存器里正确累加的机制**：广播维度把多个逻辑写位置别名到同一物理寄存器，归约结果自然各归各位。

**需要观察的现象**：(4,4) 逻辑张量只占 4 个寄存器；归约循环写 `out[m]` 时，编译器看到的存储位置只有 R0~R3 四个，与「列向量长度 = 4」一致。

**预期结果**：你能用一句话解释——「`convert_layout_zero_stride` 把硬件累加器的复杂布局重打包成 `(真实维度, 广播维度)`，归约缓冲的广播维度（零步长）让沿另一维度的求和结果别名落到正确的寄存器槽」。手算的偏移恒等于 \(m\)，证明 16 个逻辑坐标折叠为 4 个物理槽。

> 这是「源码阅读 + 手算」型实践，全部结论可在阅读 [layout_utils.py:L290-L313](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/layout_utils.py#L290-L313) 与 [ops.py:L1360-L1372](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/ops.py#L1360-L1372) 后得出。若本地有 B200/H100，可用 `cute.printf` 在 `colvec_reduce_accumulate` 里打印 `tDrReduce_mn[m,0]` 与 `tDrReduce_mn[m,2]` 的地址，验证它们相同（待本地验证）。

#### 4.2.5 小练习与答案

**练习 1**：把上面的 (4,4) 例子改成「行向量归约缓冲」——结果按 N（列）、沿 M（行）求和。写出它的 zero-stride 布局。

**参考答案**：行向量结果长度为 N=4，沿 M=4 求和。缓冲布局应是「N 真实、M 广播」：`make_layout(((4,), (4,)), stride=((1,), (0,)))` 形式上一样，但语义上模 0 是 N（真实存储 4 个列结果 R0~R3）、模 1 是 M（零步长，4 行共用同一列结果）。归约循环变为 `for n: out[n] = sum_m a[m,n]`，写回 `[0,n]/[1,n]/...` 因 M 零步长都落到 R_n。关键看**哪个维度被求和**：被求和维度对应输入里的真实步长、对应缓冲里的零步长（别名）。

**练习 2**：`convert_layout_zero_stride` 为什么用 `ref_layout` 的步长来分组，而不是用 input 自己的步长？

**参考答案**：为了让「归约缓冲」和「输入片段」按**同一套分组**对齐。归约缓冲自带零步长（它是向量、被广播），用它当 ref，就把「哪些模式是广播」固化下来；输入片段（全真实步长）按这套分组重打包后，模 1 虽取了输入的真实步长，但其**位置**与缓冲的广播维度一一对应，于是 `frag[m,n]` 与 `buf[m,n]` 的下标语义一致，归约循环才能正确对应。

**练习 3**：全零步长边界（`len(nonzero_modes)==0`）什么时候会出现？为何把非零组退化成 `(1,):(0,)`？

**参考答案**：当 ref_layout 的**所有**模式都是零步长时出现——例如一个纯标量广播到整个 tile。此时没有「真实存储维度」可分，但输出仍须是合法二模布局，于是非零组占位为单个广播元素 `(1,):(0,)`，保证 `size(非零组)=1`、归约循环仍能跑（结果是单个标量）。这是防止「无真实维度」导致布局非法的兜底。

---

### 4.3 concat_to_interleave 与 gated C 寄存器重排

#### 4.3.1 概念说明

本模块把「纯布局变换」与「寄存器数据搬移」两类工具组合起来，服务一个具体场景：**gated（门控）MLP 的融合输出**。

gated MLP（如 SwiGLU）的权重按 `[gate; up]` 两半**拼接（concat）**存放，前向计算 `down(act(gate·x) * up·x)`。融合内核里，`gate` 和 `up` 共用一个 GEMM 累加器 tile，但两者在 N 维上交错。要让后续的 `act` 与逐元素乘法高效，需要两件事：

1. **`concat_to_interleave`（纯布局）**：把 `[first_half; second_half]` 的 concat 布局重新解释成「两半交替」的 interleaved 布局——把大小 \(2N\) 的维度拆成层级 \((2, N)\)，使 `first_0, second_0, first_1, second_1, ...` 交错。这只改 stride，不搬数据。
2. **`permute_gated_Cregs_b16`（寄存器搬移）**：当要把累加器寄存器片段用 **STSM（共享内存的矩阵存储指令）** 写回 smem 时，硬件对寄存器的 lane 排布有特定要求（C-atom 契约）。gated 输出需要 warp 内用 `shuffle_sync` + `prmt`（字节重排）把寄存器重排成 STSM 期望的布局——这是**真的在 lane 间搬寄存器值**，不是纯视图。

两者分工：`concat_to_interleave` 管「逻辑上 gate/up 怎么交错」（主机侧权重视图），`permute_gated_Cregs_*` 管「寄存器物理排布如何满足 STSM」（设备侧存储前）。本模块还顺带介绍 `permute_Cregs_b32_for_stsm/ldsm` 这对「互逆」的寄存器重排——它们让 STSM/LDSM 指令（相比 STS.64/LDS.64）避免 bank conflict。

#### 4.3.2 核心流程

`concat_to_interleave(a, dim)` 的「拆层级」逻辑：

```text
half = size(a, mode=[dim]) // 2
新 shape  = (*a.shape[:dim], (2, half), *a.shape[dim+1:])               # 把 dim 维拆成 (2, half)
新 stride = (*a.stride[:dim], (half*stride_dim, stride_dim), *a.stride[dim+1:])
return make_tensor(a.iterator, make_layout(新shape, 新stride))
```

直觉：原来 `dim` 维是 `[g0,g1,...,g_{N-1}, u0,u1,...,u_{N-1}]`（gate 在前 half、up 在后 half，步长 1）。拆成 `(2, half)` 后：外层「2」选择 gate/up，内层 `half` 走该半内的偏移；外层步长 = `half * stride_dim`（跳到另一半）、内层步长 = `stride_dim`。于是逻辑坐标 `(0, k)` → gate 的第 k 个、`(1, k)` → up 的第 k 个；若让内层变化最快，读取顺序就变成 `gate0, up0, gate1, up1, ...`——交错。

`permute_gated_Cregs_b16` 的寄存器搬移（warp 内 4 线程一组）：

```text
recast 成 Int32（两个 b16 打包成一个 u32）
对每对 (upper, lower) 寄存器:
    quad_idx = lane_idx % 4
    按 quad_idx 选 upper_idx / lower_idx（目标 lane）
    shuffle_sync 把 upper/lower 送到目标 lane
    prmt(字节重排) 重组 4 个 b16
写回原寄存器位置
```

这是固定的 4 线程蝶形 + 字节置换，对应一条 STSM 的 C-atom 寄存器契约。`permute_Cregs_b32_for_stsm` 与 `permute_Cregs_b32_for_ldsm` 是另一对：前者把「每线程 2 元素」布局转成「STSM 友好」布局，后者是其逆（用于加载）。

#### 4.3.3 源码精读

`concat_to_interleave`——把 concat 拆成交错层级（纯布局）：

[quack/layout_utils.py:L21-L31](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/layout_utils.py#L21-L31) — docstring「Splits dimension `dim` (size 2N) into hierarchical (2, N) so that elements from the first half and second half alternate」。真实调用在默认线性 epilogue 里，把 `mRowVecBroadcast`/`mColVecBroadcast`（gate/up 拼接的偏置向量）转成交错形式（见 [gemm_default_epi.py:L120-L122](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_default_epi.py#L120-L122)），以及在 SM90/SM100 内核里对 gated 权重 `mT` 做同样变换（[gemm_sm90.py:L583](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_sm90.py#L583)、[gemm_sm100.py:L708](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_sm100.py#L708)）。

`permute_gated_Cregs_b16`——gated 输出的 16 位寄存器重排（STSM 契约）：

[quack/layout_utils.py:L76-L106](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/layout_utils.py#L76-L106) — 先 `recast_tensor` 成 Int32（两个 b16 打一个 u32），再用 `quad_idx = lane_idx % 4` 决定每个 lane 该收/发哪个寄存器，`shuffle_sync` 完成 lane 间交换、`cute.arch.prmt` 做 4 字节置换，最后写回。注意 L84-L91 的 `selector`/`upper_idx` 是「用算术模拟查表」（DSL 不支持下标索引 `[0,3,1,2][quad_idx]`，只能写成算术表达式），这是 CuTe-DSL 控制流限制（u1-l4）的真实体现。

`permute_gated_Cregs_f32`——同上但 fp32 粒度（用于 fp8/fp4 量化后激活）：

[quack/layout_utils.py:L40-L73](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/layout_utils.py#L40-L73) — docstring 解释：当存储 dtype 窄于 16 位（fp8/fp4 量化激活）时，b16 版本里「一个 prmt」对应这里的「一次 select + shuffle」，且必须在量化 convert **之前**做（重排只依赖位置、与 dtype 无关，但 prmt 无法寻址子 16 位 lane）。

真实调用——epilogue 的 `store_convert` 在量化转 dtype 前后分别调用这两个重排：

[quack/epilogue/ops.py:L1084-L1101](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/ops.py#L1084-L1101) — `gated and arch in (90,120)`：若存储 dtype 窄于 16 位，先 `permute_gated_Cregs_f32` 再量化（L1089）；若是 16 位，先量化再 `permute_gated_Cregs_b16`（L1099-L1101）。两者都是「满足 STSM C-atom 契约」的寄存器重排。

`permute_Cregs_b32_for_stsm` / `_ldsm`——一对互逆的 32 位寄存器重排（避免 bank conflict）：

[quack/layout_utils.py:L109-L149](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/layout_utils.py#L109-L149) — docstring 用 `a b | c d | e f | g h` 的图示说明：把「每线程 2 元素、4 线程」布局重排成 STSM 友好形式，从而用 STSM 代替 STS.64 存 C 寄存器、消除 bank conflict。`_ldsm`（[L152-L193](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/layout_utils.py#L152-L193)）注释明说「just the inverse of `permute_Cregs_b32_for_stsm`」，用于加载侧。

> **小结本模块的「两类工具」**：`concat_to_interleave` 是纯布局（换 stride，零搬移），解决「gate/up 逻辑交错」；`permute_gated_Cregs_*`/`permute_Cregs_b32_*` 是寄存器搬移（`shuffle_sync`+`prmt`，真移动数据），解决「寄存器物理排布满足 STSM/LDSM 契约」。两者常配合使用：先在主机侧把权重/偏置视图交错，再在设备侧存储前重排寄存器。

#### 4.3.4 代码实践

**实践目标**：手算 `concat_to_interleave` 的坐标映射，并在源码里把「concat 视图」与「寄存器重排」配对找出来。

**操作步骤**：

1. **手算交错映射**：设 gated 权重在 N 维拼接，`dim=1`，原始布局该维大小 \(2N=8\)、步长 1，内容为 `[g0,g1,g2,g3, u0,u1,u2,u3]`（gate 占 0~3，up 占 4~7）。按 `concat_to_interleave`，`half=4`，该维拆成 `(2, half)=(2,4)`，外层步长 `half*1=4`、内层步长 `1`。于是逻辑坐标 `(s, k)`（s∈{0,1}, k∈{0..3}）的偏移 = `s*4 + k*1`：
   - `(0,0)→0=g0, (0,1)→1=g1, (0,2)→2=g2, (0,3)→3=g3`
   - `(1,0)→4=u0, (1,1)→5=u1, (1,2)→6=u2, (1,3)→7=u3`

   若代码按「内层 k 变化最快」遍历 `(s,k)`：`(0,0),(1,0),(0,1),(1,0)...` → 读取顺序 `g0,u0,g1,u1,g2,u2,g3,u3`——**gate/up 交错**，正是 gated MLP 融合所需。

2. **定位寄存器重排**：在 [epilogue/ops.py:L1084-L1101](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/ops.py#L1084-L1101) 确认 `permute_gated_Cregs_f32`（窄 dtype）与 `permute_gated_Cregs_b16`（16 位）分别在量化 convert 的「前」「后」调用。思考：为什么窄 dtype 必须在 convert **之前**重排？

3. **对照 concat 视图**：在 [gemm_default_epi.py:L120-L122](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_default_epi.py#L120-L122) 看 `concat_to_interleave(d[key], 1)` 把 gate/up 拼接的偏置向量转成交错视图，让逐元素加法能与交错后的累加器对齐。

**需要观察的现象**：`concat_to_interleave` 不调 `cute.copy`、只改 layout；而 `permute_gated_Cregs_*` 里满是 `shuffle_sync`/`prmt`，是真寄存器搬移。两者作用于不同阶段（主机侧视图 vs 设备侧存储前）。

**预期结果**：你能解释「为什么 gated 输出既要在主机侧交错视图、又要在设备侧重排寄存器」——前者让 gate/up 在逻辑 N 维交错、便于融合 act 与乘法；后者让寄存器排布满足 STSM 硬件契约、避免 bank conflict。窄 dtype 必须先重排再量化，因为 `prmt` 按 16 位 lane 寻址，无法寻址子 16 位（fp8/fp4），重排须在「值还是 fp32」时完成。

> 这是「手算 + 源码阅读」型实践。若本地有 SM90/SM120 GPU，可用 `cute.printf` 在 `permute_gated_Cregs_b16` 前后打印 4 个 lane 的寄存器值，验证它们按 `upper_map=[0,3,1,2]`/`lower_map=[1,2,0,3]` 重排（待本地验证）。

#### 4.3.5 小练习与答案

**练习 1**：`concat_to_interleave` 与 `expand` 都是「只改 stride 不搬数据」，它们改 stride 的方式有何本质不同？

**参考答案**：`expand` **插入**一个 `stride=0` 的新维度（广播，多逻辑坐标映射到同一位置）；`concat_to_interleave` **不插入新维度**，而是把一个大小 \(2N\) 的现有维度**拆成层级** \((2, N)\)，外层步长 = `half * 原步长`、内层步长 = `原步长`，所有步长仍非零——它改变的是「读取顺序」（从两半拼接变成两半交错），不引入广播。

**练习 2**：`permute_gated_Cregs_b16` 里为什么用 `quad_idx // 2 if quad_idx % 2 == 0 else 3 - quad_idx // 2` 这种算术，而不是直接 `upper_map[quad_idx]`（`upper_map=[0,3,1,2]`）？

**参考答案**：CuTe-DSL 在 `@cute.jit` 函数内**不支持用运行期变量做列表下标索引**（Python 列表在 DSL 里是编译期静态结构，不能按动态 lane 索引）。所以源码注释「indexing isn't supported so we have to do arithmetic」——用算术表达式把查表 `upper_map[0..3] = [0,3,1,2]` 等价地表达出来。这是 u1-l4 控制流限制的真实后果。

**练习 3**：`permute_Cregs_b32_for_stsm` 与 `permute_Cregs_b32_for_ldsm` 互为逆操作，为什么需要这一对？

**参考答案**：STSM（store matrix to shared）和 LDSM（load matrix from shared）是 Hopper 起的高效矩阵存储/加载指令，但要求寄存器按特定 C-atom 布局排布。`for_stsm` 在**写 smem 前**把累加器寄存器重排成 STSM 友好布局（避免 STS.64 的 bank conflict）；`for_ldsm` 在**读 smem 后**做逆变换，把 LDSM 加载进来的布局还原成 MMA 能直接用的形式。两者保证「用 STSM/LDSM 提速」的同时寄存器布局始终正确。

---

## 5. 综合实践

把本讲三个模块串起来，完成一次「gated 列向量归约」的完整布局推演。

**任务**：设想一个 GEMM epilogue 要对 gated 输出做列向量归约（沿 N 求和、结果按 M），权重 gate/up 在 N 维拼接。请你：

1. **交错权重视图**：用 `concat_to_interleave(mT, 1)` 把 `[gate; up]` 拼接权重转成交错视图，写出大小 \(2N=8\) 维拆成 `(2,4)` 后，逻辑坐标 `(s,k)` 到原偏移的映射（参考 4.3.4 第 1 步）。
2. **手算归约缓冲布局**：设列向量归约缓冲转换后为 \(L = ((4,),(4,)) : ((1,),(0,))\)，列出 16 个逻辑坐标 \((m,n)\) 到物理寄存器 R0~R3 的映射（参考 4.2.4 第 3 步）。
3. **解释 ref 的作用**：为什么 `convert_layout_zero_stride(tRS_rInput, tDrReduce.layout)` 要用归约缓冲的布局当 ref，而不是用输入自己的布局？
4. **定位存储前重排**：如果该 gated 输出要以 16 位 dtype 用 STSM 写回 smem，需要在存储前调哪个函数？为什么窄于 16 位时要在量化前调另一个？

**参考要点**：

- 第 1 步：`(0,k)→k`（gate）、`(1,k)→4+k`（up），k∈{0..3}；内层 k 变化最快时读取顺序 `g0,u0,g1,u1,...` 交错。
- 第 2 步：`offset(m,n)=m`，故 (0,*)→R0、(1,*)→R1、(2,*)→R2、(3,*)→R3，16 个逻辑坐标折叠到 4 个寄存器。
- 第 3 步：归约缓冲自带零步长（N 维广播），用它当 ref 就把「哪些模式是广播」固化，使输入片段按同一套 `(真实, 广播)` 分组，`frag[m,n]` 与 `buf[m,n]` 下标语义一致，归约循环才能正确对应；用输入自己的布局则失去这个对齐基准。
- 第 4 步：16 位 dtype 调 `permute_gated_Cregs_b16`（量化后）；窄于 16 位（fp8/fp4）必须在量化前调 `permute_gated_Cregs_f32`，因为 `prmt` 按 16 位 lane 寻址、无法寻址子 16 位，重排须在值还是 fp32 时完成（见 [ops.py:L1084-L1101](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/ops.py#L1084-L1101)）。

> 提示：本综合实践为「手算 + 源码对照」型，全部结论可在阅读 layout_utils.py 与 epilogue/ops.py 后得出。

## 6. 本讲小结

- **改布局 ≠ 搬数据**。`transpose_view`/`select`/`expand` 都是纯视图：复用同一 `iterator`、只换 `(shape, stride)`，编译后就是不同的地址计算，零拷贝开销。`expand` 插入零步长维度实现广播。
- **累加器布局需要「拍平」**。硬件 MMA 结果的 `((2,2,V), MMA_M, MMA_N)` 等布局无法直接逐元素循环，`convert_layout_acc_mn`/`reshape_acc_to_mn` 把它转成 (M,N) 形式，是 epilogue 一切逐元素/归约操作的前置。
- **`convert_layout_zero_stride` 是向量归约的关键**。它按参考布局的步长把模式分成「非零步长组（真实存储）」与「零步长组（广播）」，重打包成二模 `(真实, 广播)` 视图；归约缓冲的零步长让沿另一维度的求和结果别名落到正确寄存器。它服务于 GEMM epilogue 的 ColVecReduce/RowVecReduce（与 standalone 归约内核的 `reduce.py` 是两套）。
- **(4,4) zero-stride 布局折叠为 4 寄存器**：`offset(m,n)=m·1+n·0=m`，16 个逻辑坐标映射到 R0~R3，归约结果各归各位。
- **gated 输出靠「布局 + 寄存器搬移」组合**。`concat_to_interleave`（纯布局，拆 `(2,N)` 让 gate/up 交错）管逻辑视图；`permute_gated_Cregs_b16/f32`（`shuffle_sync`+`prmt`，真搬寄存器）管 STSM 契约；窄 dtype 须在量化前用 f32 版重排。
- **DSL 限制的真实体现**：`permute_gated_Cregs_*` 里用算术模拟查表（`upper_map[quad_idx]` 写成算术），正因为 `@cute.jit` 内不支持运行期列表下标索引（u1-l4）。

## 7. 下一步学习建议

本讲把「布局代数」这个地基铺好了。接下来建议：

- **u3-l3（cute_dsl_utils 与 compile_utils）**：本讲的 `make_fake_tensor`/符号张量、dtype 映射是编译期构造这些布局的配套工具，下一讲会讲清「符号维度如何让一份布局对所有 batch 复用」。
- **u6-l2（EpiOp 词汇表）**：本讲的 `convert_layout_zero_stride` 真实用武之地在 `VecReduce` 这类 EpiOp；epilogue 系统讲义会让你看到这些布局工具如何被组合进完整的归约/存储协议。
- **u6-l5（领域 epilogue）**：`tile_atom_to_shape_SF_strided`（本讲末尾略提）在 `quantize_out` 里构造 blockscaled 的 SF 布局，量化输出讲义会展开。
- **回头重读 u3-l1（copy_utils）**：有了「布局 = 坐标映射」的清晰认识，再看 `partition_S`/`partition_D` 如何用 `tiled_copy` 切张量，会更通透——它们本质就是给每线程贴一个布局视图。
