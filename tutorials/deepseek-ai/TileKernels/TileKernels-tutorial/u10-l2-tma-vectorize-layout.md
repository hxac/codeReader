# TMA、向量化、布局与 pass_configs

## 1. 本讲目标

本讲是「硬件感知调优」单元的第二篇，承接 u10-l1（SM/SMEM 感知），把视角从「并行度怎么填满 SM」转到「一个线程实际发出的访存指令长什么样、编译器在背后改了什么」。

读完本讲你应该能：

- 说清 `get_best_vectorize_size` 为什么按 GPU 计算能力主版本（compute major）在 16/32 之间二选一，以及它如何决定一条 load/store 指令一次搬几个元素。
- 解释 `T.StridedTensor` 相比普通 `T.Tensor` 多携带了「逐维 stride」信息，并说明为什么量化、engram、转置这类算子离不开它。
- 区分两种「寄存器布局 swizzle」机制：`T.annotate_layout({fragment: T.Fragment(..., forward_fn=...)})` 与 `T.Parallel(..., loop_layout=...)`，理解它们如何把逻辑下标映射到 (线程, 本地偏移)。
- 看懂 `@tilelang.jit(pass_configs={...})` 里那一组 `TL_*` 编译开关的含义，并能据其取值推测一个 kernel 的访存特征。

本讲只读三个最小模块：`quant/common`、`per_token_cast_kernel`、`batched_transpose_kernel`，并借 engram、mhc 等 kernel 旁征博引来凑齐 pass_configs 的实例。

## 2. 前置知识

在进入源码前，先用三段话补齐本讲需要的硬件直觉。

**向量化访存（vectorized load/store）。** GPU 一次访存指令能搬运的「位宽」是固定的：SM90（Hopper）上单条全局 load 指令最宽 128 bit（16 字节），SM100（Blackwell）放宽到 256 bit（32 字节）。如果你每个元素是 1 字节（FP8），那么一条 128-bit 指令能搬 16 个元素；一条 256-bit 指令能搬 32 个。让「一个线程一条指令搬多个连续元素」叫向量化，是把带宽吃满的前提——否则指令发射率会成为瓶颈。本讲的 `get_best_vectorize_size` 就是算「一条指令搬几个元素」。

**TMA（Tensor Memory Accelerator）。** Hopper/Blackwell 提供的硬件张量搬运单元，可以由 SM 异步地把 HBM 里一块多维张量「整块」搬进共享内存，搬运过程中 SM 自由做别的计算。`T.copy` 默认会尽量走 TMA。但在两种情况下要 `disable_tma=True` 关掉它：目标是寄存器（fragment）而非共享内存时（TMA 只能进 SMEM）；或需要精细的向量化的 load/store 时。u2-l2 已讲过这一点，本讲只做承接。

**Stride（步长）与张量布局。** 一个逻辑二维张量 `x[i, j]` 在显存里的物理地址是 `base + i * stride0 + j * stride1`。最常见的是「行主序连续」：`stride1=1`、`stride0=列数`。但如果 `x` 是某个更大张量的切片（view），它的 `stride0` 可能远大于列数（行之间有空洞），此时称输入「非连续（strided）」。普通 `T.Tensor` 类型假定连续布局；`T.StridedTensor` 把每维 stride 显式写进类型，从而能描述非连续输入——这是本讲第二个主题。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| `tile_kernels/quant/common.py` | 量化基础设施。本讲只看 `get_best_vectorize_size` 这一个函数。 |
| `tile_kernels/quant/per_token_cast_kernel.py` | per-token 行级量化 kernel。本讲看它的 `pass_configs`、`StridedTensor` 形参、`annotate_layout`/`Fragment`、`disable_tma`、`num_vectorize` 推导。 |
| `tile_kernels/transpose/batched_transpose_kernel.py` | 批量转置 kernel。本讲看它的 `StridedTensor`（含运行时 stride 符号）、`loop_layout` 形式的 swizzle、`pass_configs`。 |
| `tile_kernels/engram/engram_gate_kernel.py` | （旁征）engram 门控 kernel，作为「为何需要 StridedTensor」「pass_configs 多开关组合」的例证。 |
| `tile_kernels/mhc/norm_fn_kernel.py` 等 | （旁征）提供 `TL_DISABLE_WGMMA`、`TL_PTXAS_REGISTER_USAGE_LEVEL` 等开关的实例。 |

## 4. 核心概念与源码讲解

### 4.1 向量化访存宽度：get_best_vectorize_size

#### 4.1.1 概念说明

向量化让一个线程用一条指令搬多个连续元素，是把显存带宽吃满的前提。但「一次最多搬几个」取决于两件事：

1. **硬件**：当前 GPU 的全局 load/store 指令最宽多少位（SM90=128 bit，SM100=256 bit）。
2. **dtype**：每个元素占几字节。同样的位宽下，元素越小，一次能搬的个数越多。

`get_best_vectorize_size(dtype)` 就是把这两件事合一：返回「在当前硬件上、对该 dtype，一条向量指令最多搬几个元素」。

#### 4.1.2 核心流程

\[ \text{vectorize\_size} = \frac{\text{硬件最宽位宽（字节）}}{\text{dtype.bytes}} \]

其中「硬件最宽位宽（字节）」按 compute major 取：

\[ \text{max\_bytes} = \begin{cases} 16 & \text{major} < 10 \ (\text{SM90 Hopper}) \\ 32 & \text{major} \ge 10 \ (\text{SM100 Blackwell}) \end{cases} \]

举几个例子（下表为本讲据公式推算，待本地用 `print(get_best_vectorize_size(...))` 验证）：

| dtype | bytes | SM90 (16B) | SM100 (32B) |
|---|---|---|---|
| FP8 e4m3 / int8 | 1 | 16 | 32 |
| FP4 e2m1（按 1 字节容器） | 1 | 16 | 32 |
| bf16 / fp16 | 2 | 8 | 16 |
| fp32 | 4 | 4 | 8 |

含义：同一份 FP8 数据，Hopper 上一条指令搬 16 个，Blackwell 上翻倍到 32 个——所以同一段 kernel 产物在两代硬件上行为不同。

#### 4.1.3 源码精读

[common.py:L13-L17](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/quant/common.py#L13-L17) —— 整个函数只有 5 行：探测目标硬件的 compute version，取主版本号，按 `<10` 在 16/32 间二选一，再除以 dtype 字节数。

```python
def get_best_vectorize_size(dtype: T.dtype) -> int:
    target = determine_target(return_object=True)
    ver = nvcc.get_target_compute_version(target)  # e.g. "8.6"
    major, _ = nvcc.parse_compute_version(ver)
    return (16 if major < 10 else 32) // dtype.bytes
```

这里有两点值得注意：

- `nvcc.get_target_compute_version` 返回的是**编译目标**的 compute version（如 `"9.0"`、`"10.0"`），不一定等于物理 GPU——TileLang 默认按目标硬件编译产物，故向量化宽度是「烤进编译产物」的编译期决策。
- 这个值随后进入 kernel 构造期参数（见 4.3），不同硬件会特化出不同产物。

这个函数唯一的调用点在 per_token_cast_kernel 里：

[per_token_cast_kernel.py:L44](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/quant/per_token_cast_kernel.py#L44) —— `num_vectorize` 取「硬件最佳宽度」与「每线程元素数的整除约束」的较小者。

```python
num_vectorize = min(get_best_vectorize_size(in_config.dtype), math.gcd(block_m * block_k // num_threads, 32))
```

`block_m * block_k // num_threads` 是每个线程分到的元素数；`math.gcd(..., 32)` 保证它和 32（一个 warp 的宽度上界）有良好整除关系。取 `min` 是因为：哪怕硬件支持一次搬 32 个，如果每线程只分到 8 个元素，也只能搬 8 个。

`num_vectorize` 随后被用于两段规约里的 reshape（`[block_m, block_k // num_vectorize, num_vectorize]`，见 [per_token_cast_kernel.py:L98](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/quant/per_token_cast_kernel.py#L98)）和 4.3 要讲的 `x_layout_fn`——它就是「向量化组」的大小。

#### 4.1.4 代码实践

1. **目标**：直观感受硬件差异对向量化宽度的影响。
2. **操作步骤**：
   - 在能 import tilelang 的环境里执行：
     ```python
     from tilelang import language as T
     from tile_kernels.quant.common import get_best_vectorize_size
     for dt in [T.float8_e4m3fn, T.bfloat16, T.float32]:
         print(dt, get_best_vectorize_size(dt))
     ```
   - 如果当前机器没有 GPU，可以打印 `nvcc.get_target_compute_version(determine_target(return_object=True))` 看编译目标是几。
3. **观察现象**：FP8 应得到 16（SM90）或 32（SM100）；bf16 减半；fp32 再减半。
4. **预期结果**：与本讲 4.1.2 表格一致；否则「待本地验证」当前硬件的 compute major。
5. 进一步把 `num_vectorize` 的第二个约束 `math.gcd(...)` 手算一遍（取 `block_k=128, block_m=32, num_threads=128`），体会 `min` 的钳制作用。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `get_best_vectorize_size` 不接受运行时张量，而要传 `T.dtype`？
**答**：因为返回值是编译期决策，用来特化 kernel 产物（reshape 维度、布局函数）；dtype 在编译时已知，而张量是运行时才到的。向量化宽度必须烤进产物，故只能依赖编译期信息。

**练习 2**：某 FP8 kernel 在 SM90 上 `num_vectorize=16`，搬到 SM100 上会自动变成 32 吗？
**答**：会重新编译出一份新产物（因为 `get_best_vectorize_size` 返回 32），但最终 `num_vectorize` 仍受 `min(..., gcd(...))` 钳制；若每线程元素数不足 32，则不会涨到 32。所以「硬件放宽」是必要不充分条件。

---

### 4.2 T.StridedTensor：让 kernel 吃下非连续输入

#### 4.2.1 概念说明

TileLang 里描述 kernel 形参的张量类型有两种：

- `T.Tensor[shape, dtype]`：只声明形状与 dtype，**隐含「连续布局」**——编译器假定第 d 维的 stride 等于它后面各维尺寸的乘积。
- `T.StridedTensor[shape, strides, dtype]`：在形状之外**显式写出每一维的 stride**，stride 可以是运行时符号（`T.dynamic`）。

多出来的信息就是「逐维 stride」。它解决一个问题：**当输入张量的行步长 ≠ 列数时（非连续/strided），怎么正确寻址。**

典型场景：量化、engram、转置的输入往往是某个大激活张量的切片或转置视图（view），其行间有空洞。若用 `T.Tensor`，编译器会按连续布局算地址，结果读到的全是错位的数；若强行 `.contiguous()`，则多一次显存拷贝——对带宽受限算子是致命的。`T.StridedTensor` 让 kernel 直接就地读非连续输入，零拷贝。

#### 4.2.2 核心流程

逻辑下标 `(i, j)` 到物理地址的映射：

\[ \text{addr}(i, j) = \text{base} + i \cdot \text{stride}_0 + j \cdot \text{stride}_1 \]

- 用 `T.Tensor` 时，编译器代入 `stride_0 = shape_1, stride_1 = 1`。
- 用 `T.StridedTensor` 时，`stride_0, stride_1` 由形参显式给出，可以是运行时符号。

wrapper 侧从 PyTorch 张量取真实 stride 传进去，编译期只看到「stride 是某个符号」，从而一份产物复用所有 stride 取值。

#### 4.2.3 源码精读

**例 1：转置 kernel 的输入是三维 strided。**

[batched_transpose_kernel.py:L38-L42](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/transpose/batched_transpose_kernel.py#L38-L42) —— `x` 是 `StridedTensor`，三维各自带 stride；`out` 是普通 `T.Tensor`（输出是新分配的连续张量）。

```python
def batched_transpose_kernel(
    x: T.StridedTensor[(num_batches, shape_x, shape_y), (shape_x * stride_x, stride_x, 1), dtype],
    out: T.Tensor[(num_batches, shape_y, shape_x), dtype],
):
```

注意 stride 元组 `(shape_x * stride_x, stride_x, 1)`：

- 第 2 维（最内 `shape_y`）stride=1，即最内维连续——这是转置能高效的前提。
- 第 1 维（`shape_x`）stride=`stride_x`，是一个**运行时符号**（[L28](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/transpose/batched_transpose_kernel.py#L28) 的 `stride_x = T.dynamic('stride_x')`），未必等于 `shape_y`。
- 第 0 维（batch）stride=`shape_x * stride_x`。

这正是 u3-l2 里 `twice_stride` 测试要钉死的隐藏假设：输入可以非连续，但最内维必须连续、且 `stride_x` 必须 4 对齐。kernel 用 `T.assume` 把这些约束告诉编译器：

[batched_transpose_kernel.py:L49-L51](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/transpose/batched_transpose_kernel.py#L49-L51) —— 显式声明对 stride/shape 的整除假设，供编译器优化。

```python
T.assume(shape_x % block_x == 0)
T.assume(shape_y % block_y == 0)
T.assume(stride_x % block_k == 0)
```

输出侧为何用 `T.Tensor`？因为 wrapper 里 `out = torch.empty(...)` 新分配了连续张量（见 [L115](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/transpose/batched_transpose_kernel.py#L115)），行步长必然等于 `shape_x`，无需 strided。

**例 2：量化 kernel 的输入和 SF 都带 stride。**

[per_token_cast_kernel.py:L65-L71](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/quant/per_token_cast_kernel.py#L65-L71) —— 4 个张量形参里，输入 `x`、输入 SF `x_sf`、输出 SF `out_sf` 都是 `StridedTensor`，只有输出 `out` 是普通 `Tensor`。

```python
def per_token_cast_kernel(
    x: T.StridedTensor[(num_tokens, hidden), (token_stride, 1), in_config.dtype],
    x_sf: T.StridedTensor[x_sf_shape, (in_sf_stride, 1), in_config.sf_dtype],
    out: T.Tensor[(num_tokens, hidden), out_config.dtype],
    out_sf: T.StridedTensor[sf_shape, (out_sf_stride, 1), out_config.sf_dtype],
):
```

三个 stride（`token_stride`、`in_sf_stride`、`out_sf_stride`）都是运行时符号（[L55-L57](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/quant/per_token_cast_kernel.py#L55-L57)）。为什么量化需要它？因为量化算子常作用于激活张量的 view：模型里 `x` 可能是 `(batch*seq, hidden)` 的一个切片，行步长未必等于 `hidden`；SF 张量在 col-major / packed ue8m0 布局下（u4-l1）stride 更是非平凡。wrapper 从 PyTorch 取真实 stride 传入：

[per_token_cast_kernel.py:L185-L189](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/quant/per_token_cast_kernel.py#L185-L189) —— wrapper 把 `x.stride(0)` 作为 `token_stride` 传给构造函数，于是符号在启动时被真实值绑定。

```python
kernel = get_per_token_cast_kernel(
    hidden=hidden,
    token_stride=get_logical_hidden(x.stride(0), x.dtype),
    ...
)
```

**例 3：engram 的 k、v 是 strided。**

[engram_gate_kernel.py:L60-L62](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/engram/engram_gate_kernel.py#L60-L62) —— `hidden_states`、`weight_fused`、输出等是普通 `T.Tensor`，但 `k`、`v` 是 `StridedTensor`，带 `k_stride_s/k_stride_h/v_stride_s` 三个运行时符号。

```python
hidden_states: T.Tensor[(num_tokens, hc_mult, hidden_size), T.bfloat16],
k: T.StridedTensor[(num_tokens, hc_mult, hidden_size), (k_stride_s, k_stride_h, 1), T.bfloat16],
v: T.StridedTensor[(num_tokens, hidden_size), (v_stride_s, 1), T.bfloat16],
```

为什么 k/v 需要 strided 而 hidden_states 不需要？因为 k/v 在模型里常常是 attention 缓存或转置视图（如 `(num_tokens, hc_mult, hidden)` 的某种切片），物理布局与逻辑形状不对应；而 hidden_states 是主激活流，按连续分配。engram 用 `StridedTensor` 就能直接吃这些视图，免去强制连续化。

#### 4.2.4 代码实践

1. **目标**：亲眼看到 `StridedTensor` 让 kernel 接受非连续输入、而 `T.Tensor` 不行。
2. **操作步骤**（源码阅读型 + 可选运行）：
   - 读 [per_token_cast_kernel.py:L185-L192](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/quant/per_token_cast_kernel.py#L185-L192)，确认 wrapper 用的是 `x.stride(0)` 而非 `x.shape[1]`。
   - 构造一个非连续输入：`big = torch.randn(8, 4096, device='cuda', dtype=torch.bfloat16); x = big[:, :2048]`，此时 `x.stride(0)==4096` 而 `x.shape[1]==2048`。
   - 调用 `tile_kernels.quant.per_token_cast(x, 'e4m3', 128)`，预期它正常运行（因为形参是 `StridedTensor`）。
   - 思考：若把 kernel 形参改成普通 `T.Tensor`，编译器会假定 `token_stride==hidden==2048`，从而读到 `big` 里错位的行——这就是「为何量化需要 StridedTensor」。
3. **观察现象**：非连续输入下结果仍与 PyTorch 参考对齐；若改坏形参则结果错乱。
4. **预期结果**：正确性测试通过；若无 GPU，标注「待本地验证」并完成源码阅读部分。
5. 总结一句话：**`T.StridedTensor` 多携带的信息就是「逐维 stride」，它把行步长从「编译期假定」变成「运行时符号」，使 kernel 能零拷贝读 view/切片/转置输入——这正是量化、engram 这类常作用于激活 view 的算子离不开它的原因。**

#### 4.2.5 小练习与答案

**练习 1**：转置 kernel 的 `out` 为何用 `T.Tensor` 而非 `StridedTensor`？
**答**：输出是 wrapper 新分配的连续张量（`torch.empty`），行步长恒等于 `shape_x`，编译器的连续假定成立，故无需 strided；输入才可能非连续，故用 `StridedTensor`。

**练习 2**：`T.StridedTensor` 的 stride 为什么常常写成运行时符号 `T.dynamic`，而不是编译期常数？
**答**：因为同一份编译产物要复用多种输入形状/布局（如不同 batch、不同切片）。若把 stride 烤成常数，每种 stride 都要单独编译一份，缓存爆炸；用符号则一份产物适配所有取值，由启动时的张量绑定具体值。

---

### 4.3 T.Fragment 与 annotate_layout / loop_layout：寄存器布局 swizzle

#### 4.3.1 概念说明

`T.alloc_fragment` 分配的是一组「协作布局寄存器」（u2-l2）：一块逻辑上的二维数据 `(block_m, block_k)`，由整个 block 的所有线程共同持有，每个线程手里攥着其中若干元素。**「逻辑下标 (i, j) 映射到 (哪个线程, 该线程的第几个本地寄存器)」** 这层映射，就叫 fragment 的布局（layout）。

默认布局由编译器挑，通常对常见访问模式够用。但有些场景要**手动改这层映射**，即「swizzle」：

- 让连续的 `num_vectorize` 个元素落到同一线程 → 该线程可发一条向量化的 load/store。
- 让「按列读共享内存」时的访问模式避开 bank conflict（u3-l1 的 swizzle_j）。

TileKernels 里改这层映射有两种写法，本讲都看：

1. `T.annotate_layout({fragment: T.Fragment(shape, forward_fn=fn)})` —— 给已分配的 fragment 标注自定义布局。
2. `T.Parallel(..., loop_layout=fragment_obj)` —— 给一个并行循环指定迭代→线程的映射顺序（fragment 对象当 loop_layout 用）。

#### 4.3.2 核心流程

`forward_fn(i, j) -> (thread_id, local_offset)` 是 swizzle 的核心：输入逻辑坐标，输出「线程号 + 线程内偏移」。两种机制的 `forward_fn` 语义一致，区别在挂载点（fragment 本体 vs 并行循环）。

- 量化用第 1 种：`x_layout_fn` 让连续 `num_vectorize` 元素归同一线程，对齐 4.1 的向量化宽度。
- 转置用第 2 种：`create_loop_layout_fn` 让一个 warp 写出一条连续输出行，实现合并写（u3-l1 已讲 bank conflict 那一半，这里只看 loop_layout 这一半）。

#### 4.3.3 源码精读

**写法一：annotate_layout + Fragment（量化）。**

[per_token_cast_kernel.py:L61-L63](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/quant/per_token_cast_kernel.py#L61-L63) —— 定义 swizzle 函数 `x_layout_fn`。

```python
def x_layout_fn(i: int, j: int):
    id = i * block_k + j
    return id // num_vectorize % num_threads, id // (num_vectorize * num_threads) * num_vectorize + id % num_vectorize
```

解读：把二维坐标先展平成线性 `id`，然后

- 线程号 = `id // num_vectorize % num_threads`：每 `num_vectorize` 个连续元素归同一线程，线程号在 `num_threads` 内循环复用。
- 本地偏移 = `id // (num_vectorize*num_threads) * num_vectorize + id % num_vectorize`：同一线程的多个向量化组在本地寄存器里顺序排列。

效果：第 `t` 号线程恰好持有逻辑上 `[t*num_vectorize .. (t+1)*num_vectorize)` 这一段连续元素 → 一条向量 load 指令搞定。

[per_token_cast_kernel.py:L77-L82](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/quant/per_token_cast_kernel.py#L77-L82) —— 把该 swizzle 挂到 `x_fragment` 上。

```python
T.annotate_layout({
    x_fragment: T.Fragment(
        (block_m, block_k),
        forward_fn=x_layout_fn,
    )
})
```

随后 [L85](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/quant/per_token_cast_kernel.py#L85) 的 `T.copy(x[...], x_fragment, disable_tma=True)` 就按这个布局做向量化 load（目标 fragment 故禁 TMA，u2-l2）。

**写法二：loop_layout（转置）。**

[batched_transpose_kernel.py:L7-L14](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/transpose/batched_transpose_kernel.py#L7-L14) —— 定义 loop_layout 的 forward_fn。

```python
def create_loop_layout_fn(block_x: int, num_threads: int = 256):
    def loop_layout_fn(i, j):
        elems = i * block_x + j
        forward_thread = (elems // 4) % num_threads
        forward_local = elems % 4 + elems // (num_threads * 4) * 4
        return forward_thread, forward_local
    return loop_layout_fn
```

[L36](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/transpose/batched_transpose_kernel.py#L36) 把它包成 `T.Fragment` 对象，再在 [L73](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/transpose/batched_transpose_kernel.py#L73) 作为 `loop_layout=` 传给 `T.Parallel`：

```python
loop_layout = T.Fragment((block_y, block_x), forward_fn=create_loop_layout_fn(block_x, num_threads))
...
for i, j in T.Parallel(block_y, block_x, loop_layout=loop_layout):
    out[pid_batch, pid_y * block_y + i, pid_x * block_x + j] = out_shared[i, j]
```

这里的 swizzle 管「共享→全局」的合并写（u3-l1）：让一个 warp 的线程对齐到一条连续输出行，全局写合并。注意它和 `swizzle_j`（[L67](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/transpose/batched_transpose_kernel.py#L67)，管「输入→共享」的 bank conflict）是**两套互补的映射**，别混了。

**旁证：engram 也用 Fragment 标 swizzle。**

[engram_gate_kernel.py:L304-L305](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/engram/engram_gate_kernel.py#L304-L305) —— engram 反向里给 shared→寄存器的拷贝指定 swizzle 布局，`vs=go_vec_size/x_vec_size` 即向量化组大小，与 4.1 的逻辑同源。

```python
go_copy_layout = T.Fragment((hc_mult, go_blk_d), forward_fn=partial(smem_layout, vs=go_vec_size))
x_copy_layout = T.Fragment((hc_mult, x_blk_d), forward_fn=partial(smem_layout, vs=x_vec_size))
```

#### 4.3.4 代码实践

1. **目标**：理解 `forward_fn` 如何决定「哪个线程拿哪个元素」。
2. **操作步骤**（源码阅读型）：
   - 取 `block_m=1, block_k=128, num_threads=128, num_vectorize=16`，手算 `x_layout_fn(0, 0)` 与 `x_layout_fn(0, 16)` 的返回值。
     - `id(0,0)=0` → thread `0//16%128=0`，local `0`。
     - `id(0,16)=16` → thread `16//16%128=1`，local `0`。
     - 即第 0、1 号线程分别持有逻辑 `[0..15]`、`[16..31]`，正是一条向量 load 的粒度。
   - 对照 [per_token_cast_kernel.py:L98](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/quant/per_token_cast_kernel.py#L98) 的 reshape `[block_m, block_k // num_vectorize, num_vectorize]`，理解「向量化组」在布局与规约两处的一致性。
3. **观察现象**：手算结果应满足「连续 `num_vectorize` 元素同线程」。
4. **预期结果**：与上述手算一致；若 `num_vectorize` 变化（如 SM100 上 FP8 涨到 32），重算一遍看线程映射如何变。
5. 无 GPU 也能完成本实践，纯手算。

#### 4.3.5 小练习与答案

**练习 1**：`annotate_layout` 与 `loop_layout=` 各自挂在哪里？
**答**：`annotate_layout({fragment: T.Fragment(...)})` 挂在一个已 `alloc_fragment` 的寄存器块上，改它的元素→线程映射；`loop_layout=` 是 `T.Parallel` 循环的关键字参数，改「循环迭代→线程」的分配顺序。两者 `forward_fn` 语义相同（逻辑坐标→(线程, 本地偏移)），挂载点不同。

**练习 2**：为什么量化 kernel 必须配 `disable_tma=True`？
**答**：TMA 只能把 HBM 整块搬进共享内存，而这里 `T.copy` 的目标是 fragment（寄存器），且需要按 `x_layout_fn` 的向量化布局精细落位；TMA 路径既到不了寄存器也不接受自定义布局，故禁用、走向量化 load。

---

### 4.4 pass_configs：编译期总开关

#### 4.4.1 概念说明

`@tilelang.jit(pass_configs={...})` 是给 TileLang 编译流水线的「指令清单」：哪些优化 pass 开、哪些关。它出现在每个 kernel 构造函数外层（u2-l1 讲过这层结构），取值在**编译期**生效，不同取值会编译出不同产物。

TileKernels 里出现的开关可归三类：

- **访存指令类**：`TL_ENABLE_LOWER_LDGSTG_PREDICATED`、`TL_DISABLE_VECTORIZE_256`、`TL_DISABLE_WGMMA`、`TL_DISABLE_WARP_SPECIALIZED`——决定生成什么样的 load/store/计算指令。
- **编译器检查/告警类**：`TL_DISABLE_OUT_OF_BOUND_WARNING`、`TL_DISABLE_DATA_RACE_CHECK`、`TL_DISABLE_THREAD_STORAGE_SYNC`——关掉某些静态检查或自动同步。
- **寄存器分配类**：`TL_PTXAS_REGISTER_USAGE_LEVEL`——给下游 ptxas 的提示。

> 说明：TileLang 未在本仓库文档里逐条解释这些 `TL_*` 的精确语义，下表「推测作用」一列据**开关名 + 使用该开关的 kernel 特征**推断，旨在帮助读者形成直觉；精确语义「待确认」时应以 TileLang 上游文档/源码为准。

#### 4.4.2 核心流程

每个 kernel 按自身访存特征挑开关：

- 纯逐元素/规约/scatter kernel → 关 warp specialization（用不到生产者-消费者软流水）。
- 需要异步 shared 拷贝 → 开 predicated LDGSTS。
- 大寄存器压力 / 跨层融合 → 限寄存器用量、关 256-bit 向量化以保占用率。
- 含 GEMM 但不想要 Hopper wgmma → 关 WGMMA。

#### 4.4.3 源码精读

下表汇总本讲收集到的 pass_configs 实例（行号均已核对）：

| 开关（取值） | 出处 | 推测作用 |
|---|---|---|
| `TL_DISABLE_WARP_SPECIALIZED: True` | 转置 [L17-L21](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/transpose/batched_transpose_kernel.py#L17-L21)、topk_gate、per_token_cast 等几乎所有 kernel | 关闭 warp specialization（Hopper+ 的生产者-消费者软流水）。逐元素/规约/scatter kernel 受益不大，关掉简化编译、避免无谓的 warp 角色划分。 |
| `TL_DISABLE_WARP_SPECIALIZED: False` | per_token_cast_to_e5m6（e5m6 打包 kernel） | 显式**打开** warp specialization——e5m6 的 8 值打包成 3 个 uint32 较重，软流水有收益。与上面形成对照。 |
| `TL_ENABLE_LOWER_LDGSTG_PREDICATED: True` | per_token_cast [L13-L18](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/quant/per_token_cast_kernel.py#L13-L18)、cast_back、inplace_unique_group_indices | 启用「带断言的异步 global→shared 拷贝」（LDGSTS = Load Global Store Shared，即 `cp.async`），让 shared memory 加载与计算重叠。 |
| `TL_PTXAS_REGISTER_USAGE_LEVEL: 10` | post、pre_big_fuse、pre_apply_mix、multilayer_recompute | 给 ptxas（PTX 汇编器）的寄存器用量提示，等级 10 通常用于控制每线程寄存器上界、换占用率。 |
| `TL_DISABLE_VECTORIZE_256: True` | post、pre_big_fuse、pre_apply_mix、multilayer_recompute | 关闭 256-bit（32 字节）向量化访存，退回 128-bit。这几个大 kernel 寄存器/SMEM 压力大，关 256-bit 以保占用率。 |
| `TL_DISABLE_OUT_OF_BOUND_WARNING: True` | topk_sum_and_topk_group_idx、get_fused_mapping、engram_gate_bwd [L185-L191](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/engram/engram_gate_kernel.py#L185-L191) | 关闭越界访问告警。这些 kernel 会带 padding 地越界读（如 fused 布局的对齐段、engram 持久化块覆盖末尾），告警属预期内、刷屏有害。 |
| `TL_DISABLE_THREAD_STORAGE_SYNC: True` | topk_sum_and_topk_group_idx、get_fused_mapping、engram_fwd/bwd [L11-L16](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/engram/engram_gate_kernel.py#L11-L16) | 关闭 thread-local storage 的自动同步。warp shuffle / 持久化块内已自管同步，关掉编译器自动插入的同步避免冗余。 |
| `TL_DISABLE_DATA_RACE_CHECK: True` | per_block_cast | 关闭数据竞争检查。per_block 用 replicate 布局让多线程合作写同一 SF（u4-l4），属设计内的「受控竞争」，检查器会误报。 |
| `TL_DISABLE_WGMMA: True` | norm_fn [L6-L8](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/mhc/norm_fn_kernel.py#L6-L8) | 关闭 Hopper WGMMA（Warp Group Matrix Multiply Accumulate）指令。norm_fn 的 GEMM 规模/形状不适合 wgmma，退回普通 MMA。 |

观察两个组合规律：

1. **「逐元素家族」**（转置、topk_gate、normalize_weight、swiglu_* 等）只开一个 `TL_DISABLE_WARP_SPECIALIZED: True`——它们访存简单，只需关掉软流水。
2. **「重 kernel 家族」**（post、pre_big_fuse、multilayer_recompute）三者同开 `WARP_SPECIALIZED + PTXAS_REGISTER_USAGE_LEVEL=10 + DISABLE_VECTORIZE_256`——三件套联手压低寄存器/向量化压力以保占用率，是 mhc 跨层融合 kernel 的典型配方。

#### 4.4.4 代码实践

1. **目标**：据 pass_configs 取值反推 kernel 的访存特征（本讲综合实践任务之一）。
2. **操作步骤**：
   - 用 grep 在 `tile_kernels/` 下搜 `TL_DISABLE_|TL_ENABLE_`，把每个 kernel 的开关组合抄进一张表（至少 3 个 kernel，建议覆盖逐元素、重 kernel、含 GEMM 三类）。
   - 对每个开关，按其名字和它出现的 kernel 类型，写下一句「推测作用」。
   - 重点对照三处对照点：
     - `TL_DISABLE_WARP_SPECIALIZED` 在 e5m6 是 `False`、其余多数 `True`（[per_token_cast_to_e5m6_kernel.py:L68-L70](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/quant/per_token_cast_to_e5m6_kernel.py#L68-L70)）；
     - `TL_ENABLE_LOWER_LDGSTG_PREDICATED` 与 `disable_tma=True` 常成对出现于量化反向/cast_back；
     - `TL_DISABLE_WGMMA` 只在 norm_fn 出现。
3. **观察现象**：开关组合与 kernel 复杂度正相关——越重/越跨层的 kernel 开关越多。
4. **预期结果**：得到一张「kernel → 开关组合 → 推测作用」表；精确语义部分标「待确认（以 TileLang 上游为准）」。
5. 若本地装了 tilelang，可用 `TK_PRINT_KERNEL_SOURCE=1`（[per_token_cast_kernel.py:L194-L195](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/quant/per_token_cast_kernel.py#L194-L195)）打印生成的 CUDA 源码，验证推测（如是否出现 `cp.async`、`wgmma`、`stmatrix` 等指令）。

#### 4.4.5 小练习与答案

**练习 1**：为什么几乎所有 TileKernels kernel 都设 `TL_DISABLE_WARP_SPECIALIZED: True`？
**答**：warp specialization 是给「计算重的 GEMM/注意力」做生产者-消费者软流水用的；TileKernels 多为逐元素、规约、scatter 类带宽受限算子，没有可拆分的生产/消费阶段，开了反而增加编排开销与编译复杂度，故统一关掉。e5m6 打包是少数例外。

**练习 2**：`TL_DISABLE_VECTORIZE_256` 与 4.1 的 `get_best_vectorize_size` 有何关系？
**答**：`get_best_vectorize_size` 在 SM100 上会返回 32（对应 256-bit 向量）；而 `TL_DISABLE_VECTORIZE_256: True` 在编译流水线层面禁止生成 256-bit 向量指令，相当于强制退回 128-bit。两者一个决定「想要多宽」、一个决定「允许多宽」，后者是前者的上限阀门。

**练习 3**：`TL_DISABLE_OUT_OF_BOUND_WARNING` 是「正确性开关」还是「告警静音开关」？关掉它会影响结果吗？
**答**：只是静音编译期越界告警，不改运行结果。engram_bwd、get_fused_mapping 等的越界读是 padding 设计的一部分（读到边界外不影响有效输出），告警属噪音，故静音。

---

## 5. 综合实践

把本讲四个模块串起来，给「批量转置 kernel」做一次完整的「访存特征体检」：

1. **向量化**：读 [batched_transpose_kernel.py:L17-L21](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/transpose/batched_transpose_kernel.py#L17-L21) 的 pass_configs 与 [L59-L60](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/transpose/batched_transpose_kernel.py#L59-L60) 的 `T.vectorized(block_k)`，回答：转置里的「向量化宽度」是写死的 `block_k=4`，并没有用 `get_best_vectorize_size`。解释为什么——提示：转置的瓶颈是 bank conflict 而非向量化宽度，且寄存器 4×4 块翻转天然定下 `block_k=4`。
2. **StridedTensor**：解释 [L40](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/transpose/batched_transpose_kernel.py#L40) 的 `x` 为何必须是 strided，而 [L41](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/transpose/batched_transpose_kernel.py#L41) 的 `out` 可以是普通 `T.Tensor`。
3. **布局 swizzle**：对照 [L67](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/transpose/batched_transpose_kernel.py#L67) 的 `swizzle_j`（输入→共享，消 bank conflict）与 [L36/L73](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/transpose/batched_transpose_kernel.py#L73) 的 `loop_layout`（共享→全局，合并写），说明它们是两套互补映射。
4. **pass_configs**：转置只开了 `TL_DISABLE_WARP_SPECIALIZED: True`，对比 per_token_cast 多开了 `TL_ENABLE_LOWER_LDGSTG_PREDICATED`，解释差异——提示：转置用 local+手写 shared 索引、不走 fragment 的集体式 `T.copy`，故不需要 LDGSTS。

产出：一张「转置 kernel 访存特征四象限表」（向量化 / stride / swizzle / pass_configs），每格一句话结论。

> 本实践为源码阅读型，全程无需 GPU；若想验证，可在装好 tilelang 的机器上用 `TK_PRINT_KERNEL_SOURCE=1` 跑 `batched_transpose`，核对生成的 PTX 是否与你的推测一致。

## 6. 本讲小结

- `get_best_vectorize_size(dtype) = (16 if major<10 else 32) // dtype.bytes`：按 compute major 在 16/32 字节间二选一，除以 dtype 字节数，得到「一条向量指令搬几个元素」；它是编译期决策，烤进产物。
- `T.StridedTensor[shape, strides, dtype]` 相比 `T.Tensor` 多携带「逐维 stride」，且 stride 可为运行时符号；它让 kernel 零拷贝读 view/切片/转置输入，量化、engram、转置都靠它免去 `.contiguous()`。
- 寄存器布局 swizzle 有两种挂法：`T.annotate_layout({fragment: T.Fragment(shape, forward_fn=fn)})` 标 fragment 本体；`T.Parallel(..., loop_layout=fragment)` 标并行循环。`forward_fn(i,j)->(thread,local)` 是核心，量化用它对齐向量化组，转置用它做合并写。
- `disable_tma=True` 在目标为 fragment 或需自定义向量化布局时关掉 TMA，走向量化 load。
- `pass_configs` 是编译期开关，分三类：访存指令类（`WARP_SPECIALIZED`/`LDGSTG_PREDICATED`/`VECTORIZE_256`/`WGMMA`）、检查告警类（`OOB_WARNING`/`DATA_RACE_CHECK`/`THREAD_STORAGE_SYNC`）、寄存器类（`PTXAS_REGISTER_USAGE_LEVEL`）；逐元素家族只关 warp_spec，重 kernel 家族三件套联手保占用率。
- `TL_DISABLE_VECTORIZE_256` 是 `get_best_vectorize_size` 的上限阀门——前者在编译层禁 256-bit，后者在 SM100 想要 256-bit。

## 7. 下一步学习建议

- **下一讲 u10-l3（扩展 TileKernels：新增一个算子的完整流程）**：本讲拆解了「调优旋钮」，下一讲把它们组装起来——从零写一个新算子的 TileLang kernel + wrapper + torch 参考 + pytest 测试四件套，本讲的向量化、StridedTensor、Fragment、pass_configs 都会在写新 kernel 时被用到。
- **动手实验**：挑一个逐元素 kernel（如 `normalize_weight`），把它的 `TL_DISABLE_WARP_SPECIALIZED` 改成 `False`，用 `--run-benchmark` 观察能不能跑、性能变化，体会「关掉它是安全默认、打开是少数场景优化」。
- **延伸阅读**：对照 u2-l2（存储层级与 `disable_tma`）、u3-l1（转置的 swizzle 与 bank conflict）、u4-l2（`num_vectorize` 与两段规约），把本讲的「布局」放回各自算子的数据流里理解。
- **上游对照**：若想确认 `TL_*` 开关的精确语义，查阅 TileLang 仓库的 `PassConfigKey` 定义与对应 pass 实现，把本表的「推测作用」逐条改为「确定作用」。
