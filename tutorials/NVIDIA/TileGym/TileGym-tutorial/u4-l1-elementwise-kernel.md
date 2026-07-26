# 逐元素内核模式：silu_and_mul

## 1. 本讲目标

本讲以 cuTile 版 `silu_and_mul` 为唯一案例，拆解 TileGym 中「逐元素（element-wise）行级内核」的标准写法。学完后你应该能够：

1. 看懂 **row-wise grid** 调度：为什么 `grid = (行数,)`、一块算一整行，且不需要 `occupancy` 与 `NUM_SM`。
2. 理解 **两半切片加载 a/b**：如何从同一个输入张量里，用两段列偏移分别取出「门控半」和「上投影半」，并在片上存储里就地融合 `silu(a) * b`。
3. 认识 **近似 sigmoid**：`ct.truediv` 配合 `flush_to_zero` 与 `rounding_mode=RMd.APPROX` 如何用精度换速度。
4. 理解 **`_ensure_contiguous` 装饰器**：为什么内核按行主序算偏移时，必须保证张量连续，以及仓库里的两种写法。

本讲只讲前向内核与主机侧启动。同一个文件里的反向内核（用重计算省显存）属于 autograd 主题，留到 [u4-l2](u4-l2-autograd-backward.md) 展开。

## 2. 前置知识

### 2.1 SiLU、SwiGLU 与 silu_and_mul 是什么

**SiLU**（SiLU = Sigmoid Linear Unit，又叫 Swish）是一种激活函数：

\[
\text{silu}(a) = a \cdot \sigma(a), \qquad \sigma(a) = \frac{1}{1 + e^{-a}}
\]

在现代大语言模型（LLM）的 MLP 块里，常用 **SwiGLU** 结构：把一个形状为 `(…, 2H)` 的张量沿最后一维劈成两半——前半 `a`（gate，门控）、后半 `b`（up，上投影），然后逐元素相乘：

\[
\text{output} = \text{silu}(a) \odot b, \qquad a = x_{[:, :H]},\ b = x_{[:, H:]}
\]

`silu_and_mul` 就是把这个「劈半 → silu → 乘」三步**融合进一个 GPU 内核**。融合的好处是：a、b 只从显存读一次，中间的 `silu(a)` 结果留在片上寄存器里，不必再写回显存再读回来。这是 LLM 推理/训练里最热的算子之一。

### 2.2 你需要先掌握的概念（来自前置讲义）

| 概念 | 出处 | 本讲如何用到 |
|---|---|---|
| `@dispatch` stub 与 `@register_impl` 注册 | u2-l1、u2-l2 | `silu_and_mul` 是 stub，本文件用 `@register_impl("silu_and_mul", backend="cutile")` 挂上实现 |
| `ct.gather` / `ct.scatter` 索引访问 | u3-l2 | 用 `(row_idx, col_offsets)` 取一整行的瓦片 |
| `ct.arange`、`ct.astype`、`check_bounds` | u3-l2 | 生成列下标、升 fp32 计算、处理非 2 的幂的尾部 |
| `ct.launch(stream, grid, kernel, args)` | u3-l3 | 主机侧提交内核 |
| 「一块一行」的多波调度 | u3-l3 | 本讲的 grid 正是这种风格 |

如果对上面任何一项不熟，建议先回看对应讲义再继续。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [src/tilegym/ops/cutile/silu_and_mul.py:L1-L263](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/silu_and_mul.py#L1-L263) | **本讲主角**。前向内核、反向内核、`_ensure_contiguous`、autograd 封装、`@register_impl` 注册的对外函数 |
| [src/tilegym/ops/ops.py:L172-L193](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/ops.py#L172-L193) | `silu_and_mul` 的统一签名 stub（只抛 `NotImplementedError`），定义输入布局 `SiLU(input[…,:H]) * input[…,H:]` |
| [src/tilegym/ops/cutile/utils.py:L36-L46](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/utils.py#L36-L46) | `next_power_of_2(n)`：把列宽向上取到 2 的幂，决定 `TILE_SIZE` |
| [tests/ops/test_silu_and_mul.py:L1-L173](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/tests/ops/test_silu_and_mul.py#L1-L173) | 正确性测试，含 PyTorch 参考实现与容差（`atol=1e-2`） |
| [src/tilegym/ops/cutile/activation/gelu.py:L74-L91](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/activation/gelu.py#L74-L91) | 综合实践的「标准答案」参考：`gelu_tanh_forward_ct` 给出 GELU tanh 近似的 cuTile 写法 |
| [src/tilegym/ops/cutile/softmax.py:L186-L191](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/softmax.py#L186-L191) | 对照样本：同样用 gather/scatter，但调度与精度策略不同 |

## 4. 核心概念与源码讲解

### 4.1 row-wise grid：一块算一行的逐元素调度

#### 4.1.1 概念说明

逐元素算子的特点是：**输出每个元素只依赖输入对应位置的元素，元素之间不需要交换数据**。这意味着不需要跨块归约，每个块可以独立算完「自己负责的那一片」。

`silu_and_mul` 把这种独立性发挥到极致：**一个块（CTA / program）负责一整行**。把任意形状的输入 `(batch, seq, 2H)` 在主机侧 `view` 成二维 `(行数, 2H)`，那么 grid 就直接设成「行数」：

\[
\text{grid} = (\text{batch} \times \text{seq},)
\]

每个块的索引 `ct.bid(0)` 就是它要处理的行号。这跟 [u3-l3](u3-l3-launch-patterns.md) 里 softmax 的「多波（multi-wave）一块一行」是同一种风格；而**不是** basic softmax 那种 `num_programs = NUM_SM × occupancy` 的静态持久化 grid-stride 风格。因此本内核的 `@ct.kernel` 装饰器**没有 `occupancy=` 参数**，启动也不读 `NUM_SM`。

> 术语：**CTA / block / program** 在 cuTile 语境里指同一个东西——一次 kernel launch 里能并行跑的一个线程组，对应硬件上的一个协作线程阵列。`ct.bid(0)` 等价于 CUDA 的 `blockIdx.x`。

#### 4.1.2 核心流程

主机侧（每个块都执行同一份内核代码）：

1. 主机把输入 `view(-1, 2H)`，得到 `batch_size`（行数）。
2. `TILE_SIZE = next_power_of_2(H)`，`grid = (batch_size,)`。
3. `ct.launch(stream, grid, kernel, (input_flat, output, TILE_SIZE, H))`。
4. 每个块内：`bid = ct.bid(0)` 取行号 → 用列偏移取 a/b 两半 → 计算 → 写回该行。

伪代码：

```text
grid = (num_rows,)              # 一块一行，不依赖 SM 数量
for bid in 0 .. num_rows:       # 每个块只跑自己那行（硬件并行）
    row = bid
    a = input[row, 0   : H  ]    # 门控半
    b = input[row, H   : 2H ]    # 上投影半
    output[row, :H] = silu(a) * b
```

#### 4.1.3 源码精读

内核定义与「一块一行」的注释——装饰器没有 `occupancy=`：

[src/tilegym/ops/cutile/silu_and_mul.py:L19-L27](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/silu_and_mul.py#L19-L27) —— 注释明确「grid = 行数，每块算整行」；`TILE_SIZE` 与 `TOTAL_HIDDEN_SIZE` 是 `ConstInt` 编译期常量。

块内取行号、生成列下标基底：

[src/tilegym/ops/cutile/silu_and_mul.py:L28-L35](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/silu_and_mul.py#L28-L35) —— `bid = ct.bid(0)` 即行号；`offsets = ct.arange(TILE_SIZE)` 是列下标基底 `[0,1,…,TILE_SIZE-1]`。

主机侧 grid 计算（autograd 前向路径）：

[src/tilegym/ops/cutile/silu_and_mul.py:L181-L188](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/silu_and_mul.py#L181-L188) —— `TILE_SIZE = next_power_of_2(hidden_size)`，`grid = (batch_size,)`，然后四参 `ct.launch`。

关键对比：softmax 的 basic 内核用的是 `num_programs = min(NUM_SM * 4, n_rows)` 的 grid-stride（[src/tilegym/ops/cutile/softmax.py:L189-L191](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/softmax.py#L189-L191)），依赖 `NUM_SM`；而 silu_and_mul 的 `grid = (batch_size,)` 不读 `NUM_SM`。原因正是 4.1.1 说的：逐元素无归约，多分一些块不会带来跨块同步开销。

#### 4.1.4 代码实践

**目标**：直观感受「grid 随行数线性增长、与 SM 数无关」。

1. 阅读上面的两处 `grid = (batch_size,)`（[src/tilegym/ops/cutile/silu_and_mul.py:L182](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/silu_and_mul.py#L182) 与 [src/tilegym/ops/cutile/silu_and_mul.py:L255](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/silu_and_mul.py#L255)）。
2. 在主机侧加一行临时日志（仅本地调试，勿提交）：在 `ct.launch` 前打印 `print("grid=", grid, "TILE_SIZE=", TILE_SIZE)`。
3. 分别用 `(8, 512, 2*512)` 与 `(8, 1024, 2*1024)` 两种形状调用 `tilegym.ops.silu_and_mul`。

**需要观察的现象**：`grid` 的第一个分量分别是 `8*512=4096` 与 `8*1024=8192`，恰好等于展平后的行数；`TILE_SIZE` 分别是 512、1024（已是 2 的幂）。

**预期结果**：grid 随 `batch×seq` 线性变化，与 GPU 的 SM 数无关。运行数值「待本地验证」（取决于机器后端是否可用）。

#### 4.1.5 小练习与答案

**练习 1**：如果把输入形状从 `(8, 512, 1024)`（即 `2H=1024`）改成 `(8, 512, 1000)`（`2H=1000`，非 2 的幂），`TILE_SIZE` 会是多少？grid 会是多少？

**答案**：`H=500`，`next_power_of_2(500)=512`，故 `TILE_SIZE=512 > H`，多出的 12 列靠 `check_bounds=True` 处理（见 4.2）；grid 仍是 `8*512=4096`，与列宽无关。

**练习 2**：为什么这个内核可以放心地用「一块一行」，而 basic softmax 用了 grid-stride？

**答案**：逐元素算子无跨元素归约，块间无需通信，块越多只会让并行度更高、不会有同步代价；softmax 需要对整行做归约（求 max、求和），块数受 `NUM_SM × occupancy` 约束以复用寄存器/共享内存资源，故用静态持久化 grid-stride。

---

### 4.2 两半切片加载 a/b 与就地融合计算

#### 4.2.1 概念说明

`silu_and_mul` 的输入是一个**已经拼好**的张量：最后一维是 `2H`，前 `H` 列是门控 `a`，后 `H` 列是上投影 `b`。内核要做的是：从同一块显存里**用两段不同的列偏移**分别把 a、b 取到片上，算完 `silu(a)*b` 再写回输出的前 `H` 列。

这里用到的就是 [u3-l2](u3-l2-data-movement.md) 讲过的 `ct.gather`/`ct.scatter`：传入一个**索引元组**，按下标广播取/放数据。关键技巧是「**基底 + 偏移**」拼列号：

- a 的列号：`offsets`（即 `[0, 1, …, TILE_SIZE-1]`）；
- b 的列号：`offsets + TOTAL_HIDDEN_SIZE`（整体平移 `H`）。

「**就地融合**」指：a、b 两个瓦片取到片上后，`silu(a)` 的中间结果不出片上存储，直接和 b 相乘，最终只写一次回显存。对比「未融合」需要 `silu(a)` 先写回、再读回来做乘法，融合省了一次往返显存。

#### 4.2.2 核心流程

```text
offsets        = arange(TILE_SIZE)            # 列下标基底 [0..TILE_SIZE-1]
a_col_idx      = offsets                       # a 在 [0, H)
b_col_idx      = offsets + H                   # b 在 [H, 2H)
a_tile = gather(input, (row, a_col_idx))       # 取门控半（一维瓦片）
b_tile = gather(input, (row, b_col_idx))       # 取上投影半
a_tile, b_tile = astype(a_tile, fp32), astype(b_tile, fp32)   # 升精度计算
result = (a_tile * sigmoid(a_tile)) * b_tile   # silu(a) * b，全在片上
result = astype(result, output.dtype)          # 降回存储精度
scatter(output, (row, offsets), result)        # 写回输出的前 H 列
```

要点：`(row_idx, a_col_idx)` 里 `row_idx` 是标量、`a_col_idx` 是瓦片，gather 把它们广播成一维瓦片（形状 `(TILE_SIZE,)`），这正是 [u3-l2](u3-l2-data-movement.md) 强调过的「标量×瓦片广播」语义。

#### 4.2.3 源码精读

两半列偏移与两次 gather：

[src/tilegym/ops/cutile/silu_and_mul.py:L33-L42](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/silu_and_mul.py#L33-L42) —— `a_col_idx = offsets`、`b_col_idx = offsets + TOTAL_HIDDEN_SIZE`；两次 `ct.gather` 分别取 a/b，随后 `ct.astype` 升到 `float32`。

就地融合计算与写回：

[src/tilegym/ops/cutile/silu_and_mul.py:L48-L54](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/silu_and_mul.py#L48-L54) —— `silu_a = a_tile * sigmoid_a`、`result = silu_a * b_tile`，中间不落显存；`ct.astype(result, output.dtype)` 降精度后 `ct.scatter` 写到输出的前 `H` 列。

输入布局由 stub 的 docstring 约定：

[src/tilegym/ops/ops.py:L172-L193](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/ops.py#L172-L193) —— `SiLU(input[…,:H]) * input[…,H:]`，输入最后一维须为 `2*H`，输出最后一维为 `H`。

正确性参考实现（测试里的「标准答案」）：

[tests/ops/test_silu_and_mul.py:L17-L21](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/tests/ops/test_silu_and_mul.py#L17-L21) —— `x1 = input[…,:hidden]`、`x2 = input[…,hidden:]`，返回 `torch.nn.functional.silu(x1) * x2`。容差见 [tests/ops/test_silu_and_mul.py:L74-L83](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/tests/ops/test_silu_and_mul.py#L74-L83)（`rtol=0.0, atol=1e-2`，比 softmax 的 `1e-7` 宽松得多——因为 4.3 会用近似 sigmoid）。

#### 4.2.4 代码实践

**目标**：理解 a/b 两半的列偏移互换会怎样。

1. 在本地副本里把 `a_col_idx` 与 `b_col_idx` 的偏移**对调**（让「a 半」去读 `[H, 2H)`、「b 半」去读 `[0, H)`）。
2. 对一个 `(4, 8, 2*64)` 的随机张量调用修改后的内核，再与 `torch.nn.functional.silu(input[…,H:]) * input[…,:H]` 对比。

**需要观察的现象**：对调后，输出恰好等于「silu 作用于**后半**、再乘以前半」。这验证了两半只是按列偏移区分、内核本身不关心哪一半是 gate。

**预期结果**：最大绝对误差应在 `1e-2` 量级内（与测试容差一致）。运行数值「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：为什么 a、b 能用**同一个** `offsets` 基底，只靠加一个常量 `H` 就能区分？

**答案**：因为 a、b 在最后一维上是**顺序拼接**的（`[0,H)` 紧接 `[H,2H)`），列下标只需整体平移 `H`；`offsets + TOTAL_HIDDEN_SIZE` 让 cuTile 在编译期生成「基底加标量」的索引瓦片。

**练习 2**：如果 `H` 不是 2 的幂（如 `H=1000`），`gather` 取到的 a 瓦片长度是 `TILE_SIZE=1024 > H`，多出的 12 个元素会怎样？

**答案**：`check_bounds=True` 会让越界位置的读取返回填充值（这里没显式给 `padding_value`，由 cuTile 默认处理），它们在后续 `scatter` 时同样因 `check_bounds=True` 而**不会被写回**，因此不影响有效输出区域。

---

### 4.3 近似 sigmoid：truediv / flush_to_zero / rounding_mode

#### 4.3.1 概念说明

silu 需要算 \(\sigma(a)=1/(1+e^{-a})\)。最直白的写法是 `1.0 / denom`（Python 的 `/` 运算符）。但本内核改用了 `ct.truediv(1.0, denom, flush_to_zero=True, rounding_mode=RMd.APPROX)`，主动选择了一条**更快但略不精确**的路径。三个要点：

- **`ct.truediv(num, den)`**：显式的「真除法」（浮点除，区别于整除/截断除）。把分子分母分开传，是为了能挂下面的精度旋钮。
- **`flush_to_zero=True`（FTZ）**：**Flush-To-Zero**。当结果（或中间值）落入「亚正规数（subnormal / denormal，即非常接近 0 的极小浮点数）」区间时，直接当作 0 处理。硬件处理亚正规数要走慢速微码，FTZ 把这条慢路径关掉，换取吞吐——代价是这些极小值会损失精度。这在 fp16/bf16 的 LLM 工作负载里是常见的加速手段。
- **`rounding_mode=RMd.APPROX`**：使用硬件的**近似取整/近似除法**指令，比默认的「最近偶数舍入（round-to-nearest-even）」更快，但结果会有微小偏差。

三者合起来就是「近似 sigmoid」：它在数值上仍接近真实 sigmoid（测试容差 `atol=1e-2` 足以通过），但在热路径上更快。`RMd` 是 `cuda.tile.RoundingMode` 的别名。

> 对照：[src/tilegym/ops/cutile/softmax.py:L42-L48](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/softmax.py#L42-L48) 里用的是朴素的 `numerator / denominator`，没有 FTZ、没有 APPROX。silu_and_mul 选了更激进的近似，因为它是 MLP 每层每 token 都要跑的极热算子。

#### 4.3.2 核心流程

\[
\text{denom} = 1 + e^{-a}, \qquad
\sigma(a) = \text{truediv}\bigl(1,\ \text{denom},\ \text{flush\_to\_zero},\ \text{APPROX}\bigr)
\]

\[
\text{silu}(a) = a \cdot \sigma(a), \qquad \text{output} = \text{silu}(a) \cdot b
\]

精度策略：加载后**升到 `float32`** 再算（`ct.astype(..., torch.float32)`），写回前**降回 `output.dtype`**（通常是 fp16/bf16）。升精度计算是为了让近似除法的误差在 fp32 宽尾数下被吸收，最终降精度时再舍入。

#### 4.3.3 源码精读

导入 `RoundingMode` 别名：

[src/tilegym/ops/cutile/silu_and_mul.py:L9](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/silu_and_mul.py#L9) —— `from cuda.tile import RoundingMode as RMd`。

近似 sigmoid 的两行核心：

[src/tilegym/ops/cutile/silu_and_mul.py:L44-L50](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/silu_and_mul.py#L44-L50) —— `denom = 1 + ct.exp(-a_tile)`；`sigmoid_a = ct.truediv(1.0, denom, flush_to_zero=True, rounding_mode=RMd.APPROX)`；随后 `silu_a = a_tile * sigmoid_a`、`result = silu_a * b_tile`。

同一文件的反向内核里，sigmoid 重计算用的是**完全相同**的近似式（[src/tilegym/ops/cutile/silu_and_mul.py:L91-L92](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/silu_and_mul.py#L91-L92)）——前向与反向必须用同一份激活近似，否则梯度会对不上。这点会在 [u4-l2](u4-l2-autograd-backward.md) 详细讲。

#### 4.3.4 代码实践

**目标**：体会「近似」带来的速度-精度权衡。

1. 在本地副本里把 `ct.truediv(1.0, denom, flush_to_zero=True, rounding_mode=RMd.APPROX)` 临时换成朴素的 `1.0 / denom`。
2. 用 `tests/ops/test_silu_and_mul.py::Test_SiLUAndMul::test_op` 的最大一组形状 `(32, 1024, 4096, fp32)` 跑一次正确性测试。
3. （可选）用 `tests/benchmark` 的脚本对比两种写法的吞吐。

**需要观察的现象**：朴素除法版本的最大绝对误差通常更小（更接近 PyTorch 参考）；近似版本仍应在 `atol=1e-2` 内通过，但更快。

**预期结果**：精度差异在 `1e-3` 量级或更小，吞吐差异「待本地验证」（取决于硬件与后端是否真正下发近似指令）。

#### 4.3.5 小练习与答案

**练习 1**：`flush_to_zero=True` 主要省的是哪类开销？它会在什么输入下影响精度？

**答案**：省的是硬件处理**亚正规数（subnormal）**的慢速微码开销；当 `denom` 或中间结果非常接近 0（即 `a` 是很大的负数，使 `exp(-a)` 极大、或 `a` 很大正数使 `exp(-a)→0`）时，亚正规区间被 flush 成 0，可能引入微小误差。

**练习 2**：为什么前向和反向必须用**同一个** sigmoid 近似？

**答案**：反向梯度公式里含有 `sigmoid(a)` 及其导数（见 [src/tilegym/ops/cutile/silu_and_mul.py:L100-L104](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/silu_and_mul.py#L100-L104)）。若前向用近似、反向用精确值，激活值与梯度不自洽，数值检查（`assertCorrectness` 带 `gradient=dy`）会失败。

---

### 4.4 `_ensure_contiguous` 装饰器

#### 4.4.1 概念说明

内核用 `offsets`、`row_idx` 算地址，默认**输入是行主序、连续**的——即「元素 i 的地址 = base + i * sizeof(dtype)」。但如果调用方传进来的是一个**非连续视图**（比如转置、切片后带 stride 的张量），这套「按位置算偏移」的逻辑就会读到错误的数据。

解决办法是在启动前对每个张量参数调 `.contiguous()`：若已是连续，`.contiguous()` 返回 `self`，**零开销**；若不连续，则新建一份连续拷贝。silu_and_mul.py 把这件事做成了一个**可复用的装饰器** `_ensure_contiguous`，自动对函数的**所有** `torch.Tensor` 参数（含位置参数和关键字参数）套上 `.contiguous()`。

> 仓库里还有另一种写法：[src/tilegym/ops/cutile/softmax.py:L186-L187](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/softmax.py#L186-L187) 是**就地手写** `input = input.contiguous()`。两种风格都合规；装饰器的好处是「一处定义、处处适用」，不必每个启动函数都手写一遍。

#### 4.4.2 核心流程

```text
def _ensure_contiguous(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        对每个 arg：  是 Tensor ? arg.contiguous() : arg
        对每个 value：是 Tensor ? value.contiguous() : value
        return fn(转换后的 args, 转换后的 kwargs)
    return wrapper
```

应用方式（注意装饰器顺序）：

```python
@register_impl("silu_and_mul", backend="cutile")   # 外层：注册到分发注册表
@_ensure_contiguous                                 # 内层：先保证连续
def silu_and_mul(input, out=None): ...
```

装饰器自下而上生效：先用 `_ensure_contiguous` 包住原始函数得到 `wrapper`，再把 `wrapper` 用 `register_impl` 注册成 `"silu_and_mul"` 在 `cutile` 后端下的实现。于是分发器查表拿到的、被调用的，正是「先保连续、再干活」的 `wrapper`。

#### 4.4.3 源码精读

装饰器定义：

[src/tilegym/ops/cutile/silu_and_mul.py:L109-L119](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/silu_and_mul.py#L109-L119) —— `maybe_to_contiguous` 只对 `torch.Tensor` 实例做 `.contiguous()`，其余原样放行；位置参数与关键字参数都被遍历。

装饰器的应用与注册：

[src/tilegym/ops/cutile/silu_and_mul.py:L205-L207](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/silu_and_mul.py#L205-L207) —— `@register_impl` 在外、`@_ensure_contiguous` 在内。因此经分发调用的实现，入口先做连续化。

一处旁证：autograd 前向 `_SiLUAndMulFunction.forward` 在 `view` 前没有再调 `.contiguous()`（[src/tilegym/ops/cutile/silu_and_mul.py:L171](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/silu_and_mul.py#L171)）——因为走到这里之前，`input` 已被 `_ensure_contiguous` 处理过（`requires_grad` 分支经 [src/tilegym/ops/cutile/silu_and_mul.py:L223-L226](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/silu_and_mul.py#L223-L226) 进入）；而独立的反向函数 `_silu_and_mul_backward` 仍**自带** `.contiguous()`（[src/tilegym/ops/cutile/silu_and_mul.py:L140-L141](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/silu_and_mul.py#L140-L141)），因为它不经过 `silu_and_mul` 这个被装饰的入口。

#### 4.4.4 代码实践

**目标**：验证装饰器对非连续输入的保护。

1. 构造一个非连续张量：`x = torch.randn(8, 16, 128, device="cuda"); x_nc = x.transpose(0, 1)`（`x_nc.is_contiguous()` 为 `False`）。
2. 构造一个「假装」需要连续的 Python 函数并套上 `_ensure_contiguous`：

   ```python
   from tilegym.ops.cutile.silu_and_mul import _ensure_contiguous
   @_ensure_contiguous
   def report_contig(t):
       return t.is_contiguous()
   ```
3. 调用 `report_contig(x_nc)`。

**需要观察的现象**：尽管传入的 `x_nc` 本身非连续，函数内部拿到的 `t` 的 `is_contiguous()` 为 `True`——装饰器在调用前已生成连续副本。

**预期结果**：返回 `True`。运行结果「待本地验证」。

#### 4.4.5 小练习与答案

**练习 1**：把上面应用处的两个装饰器**对调**（`@_ensure_contiguous` 在外、`@register_impl` 在内），会发生什么？

**答案**：自下而上，`register_impl` 先把**原始函数**注册成实现，然后 `_ensure_contiguous` 包住的是「注册后返回的函数」。结果：注册表里存的是**没有连续化**的原始函数，分发调用时**不会**做 `.contiguous()`，非连续输入可能读错数据。装饰器顺序很重要。

**练习 2**：为什么 `.contiguous()` 在「已连续」时几乎没有开销？

**答案**：PyTorch 的 `.contiguous()` 检测到张量已连续时直接返回 `self`（同一对象），不分配新内存、不拷贝数据，所以热路径上零成本。

---

## 5. 综合实践：把 silu_and_mul 改造成 gelu_and_mul（GELU tanh 近似）

**任务**：仿照 `_silu_and_mul_kernel_row_wise` 的结构，写一个 `_gelu_and_mul_kernel_row_wise`，计算 \(\text{output} = \text{gelu\_tanh}(a) \cdot b\)，并保证结果近似 `torch.nn.functional.gelu(x1, approximate='tanh') * x2`。这会把你在这四讲里学到的——row-wise grid、两半加载、近似激活、连续化——全部串起来。

### 5.1 数学公式

GELU 的 tanh 近似：

\[
\text{gelu}(x) = 0.5 \cdot x \cdot \bigl(1 + \tanh\bigl(\sqrt{2/\pi}\,(x + 0.044715\,x^3)\bigr)\bigr)
\]

其中 \(\sqrt{2/\pi} \approx 0.7978845608028654\)，`0.044715` 是经验系数。

### 5.2 操作步骤

1. 复制 `_silu_and_mul_kernel_row_wise`，改名 `_gelu_and_mul_kernel_row_wise`，保留 `bid`、`offsets`、a/b 两半的 gather、astype 升降精度、scatter 写回这些骨架**不变**（即复用 4.1、4.2、4.4 的成果）。
2. 把 4.3 的「近似 sigmoid」两行：

   ```python
   denom = 1 + ct.exp(-a_tile)
   sigmoid_a = ct.truediv(1.0, denom, flush_to_zero=True, rounding_mode=RMd.APPROX)
   silu_a = a_tile * sigmoid_a
   ```

   替换成 GELU tanh 近似。仓库已有现成的 cuTile 实现 `gelu_tanh_forward_ct` 可直接参照：[src/tilegym/ops/cutile/activation/gelu.py:L74-L91](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/activation/gelu.py#L74-L91)。它用 `ct.full`/`ct.ones` 造常量瓦片、用 `_tanh_ct`（即 `2·σ(2x)−1`，见 [src/tilegym/ops/cutile/activation/gelu.py:L26-L33](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/activation/gelu.py#L26-L33)）算 tanh。你也可以直接用 `ct.tanh`（仓库在 [src/tilegym/ops/cutile/gemma_attention_decode.py:L83](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/gemma_attention_decode.py#L83) 用过）。

   一个最小改法（示例代码，非仓库原有）：

   ```python
   # 示例代码：用 ct.tanh 实现 gelu_tanh(a)
   sqrt_2_div_pi = 0.7978845608028654
   coeff = 0.044715
   inner = sqrt_2_div_pi * (a_tile + coeff * a_tile * a_tile * a_tile)
   gelu_a = 0.5 * a_tile * (1.0 + ct.tanh(inner))
   result = gelu_a * b_tile
   ```

3. 主机侧再写一个 `gelu_and_mul(input)`：`TILE_SIZE = next_power_of_2(H)`、`grid = (batch_size,)`、`ct.launch(...)`，并套上 `@_ensure_contiguous`（与 silu 版一致）。

### 5.3 验证

```python
# 示例代码：最小验证脚本
import torch, tilegym
H = 512
x = torch.randn(4, 8, 2 * H, device="cuda", dtype=torch.float32)
x1, x2 = x[..., :H], x[..., H:]
ref = torch.nn.functional.gelu(x1, approximate="tanh") * x2
out = gelu_and_mul(x)                      # 你写的函数
print("max abs err:", (out - ref).abs().max().item())
```

**需要观察的现象**：`max abs err` 应在 `1e-2` 量级内（与 `test_silu_and_mul.py` 的容差一致；若用朴素除法而非近似，误差会更小）。

**预期结果**：通过 `torch.allclose(out, ref, rtol=0.0, atol=1e-2)`。具体数值「待本地验证」。

> 提示：如果你偷懒想跳过手写，可以直接阅读仓库已有的 GEGLU 实现 [src/tilegym/ops/cutile/activation/geglu.py:L42-L88](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/activation/geglu.py#L42-L88)——它正是 `a * gelu(b)` 的融合内核，与本实践的 `gelu(a) * b` 只差激活作用在哪一半，可作为对照答案。

## 6. 本讲小结

- **row-wise grid**：逐元素无归约，`grid = (行数,)` 一块算一整行，不读 `NUM_SM`、装饰器不带 `occupancy=`。
- **两半切片加载 a/b**：从同一输入用 `offsets` 与 `offsets + H` 两段列偏移分别取门控半与上投影半，`silu(a)*b` 全程在片上融合，只写一次显存。
- **近似 sigmoid**：`ct.truediv(1, 1+exp(-a), flush_to_zero=True, rounding_mode=RMd.APPROX)` 用 FTZ + 近似除法换速度，前向/反向须用同一近似；计算升 fp32、存储降回原精度。
- **`_ensure_contiguous`**：可复用装饰器，对所有 Tensor 参数调 `.contiguous()`；已连续时零开销；装饰器顺序必须是 `@register_impl` 在外、`@_ensure_contiguous` 在内。
- 这套「行级逐元素 + 两半融合 + 近似激活」模板，是 TileGym 里所有门控类激活内核（silu_and_mul、geglu 等）的共同骨架。

## 7. 下一步学习建议

- **[u4-l2 Autograd 集成与反向内核](u4-l2-autograd-backward.md)**：本文件里的 `_silu_and_mul_backward_kernel_row_wise` 与 `_SiLUAndMulFunction` 会在那一讲详讲，重点是用**重计算**省掉前向激活的显存、以及 `requires_grad` 分支为何要分两条路。
- **想看更多「行级逐元素」变体**：直接读 [src/tilegym/ops/cutile/activation/geglu.py:L1-L260](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/activation/geglu.py#L1-L260) 与 [src/tilegym/ops/cutile/activation/gelu.py:L1-L185](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/activation/gelu.py#L1-L185)，对照本讲的四模块自行归纳异同。
- **想理解精度旋钮的硬件背景**：在 [u5-l3 Autotuning](u5-l3-autotuning.md) 会看到更多 `flush_to_zero` / `LOAD_LATENCY` 这类「用精度/提示换性能」的工程手段，可与本讲的 FTZ 呼应。
