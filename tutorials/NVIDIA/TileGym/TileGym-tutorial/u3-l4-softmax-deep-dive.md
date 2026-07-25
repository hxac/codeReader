# softmax 内核全解：四种实现与 autograd 封装

## 1. 本讲目标

本讲以 cuTile 版 `softmax` 为完整案例，把前面三讲建立的「内核骨架（u3-l1）—数据搬运原语（u3-l2）—启动模式（u3-l3）」三块知识一次性串起来。

学完本讲，你应该能够：

- 说清 softmax **数值稳定**的原理（减最大值），并指出它在内核里对应的几个 cuTile 归约/算术调用。
- 区分同一 softmax 算法的**四种实现**：basic、TMA、chunked（三遍）、multi-wave，并说清它们在「加载原语 / 归约轴 / 调度方式」上的差异。
- 根据 `use_tma / use_chunked / use_multi_wave` 三个开关，追踪 `_Softmax.forward` 的四路分发，并解释 TMA 在 `compute capability < 9` 时为何回退。
- 读懂 `tests/ops/test_softmax.py` 的参数化结构，并能用它逐个验证四种变体的触发条件。

本讲**不**重复讲解 `@ct.kernel`、`ConstInt`、`ct.gather/load`、`ct.launch` 四参签名等已建立的概念，只在用到时简要点名，重点放在「同一算法为什么要有四种写法」。

## 2. 前置知识

在进入源码前，先用两段直觉把 softmax 讲透。

**数学定义。** 给定向量 \(x = (x_1, \dots, x_n)\)，softmax 把它归一成一组正数且和为 1 的概率：

\[
\text{softmax}(x_i) = \frac{\exp(x_i)}{\sum_{j=1}^{n} \exp(x_j)}
\]

**数值稳定。** 直接套公式会爆：当某个 \(x_i\) 较大时 \(\exp(x_i)\) 会溢出到 `inf`。标准做法是先减去该行最大值 \(m = \max_j x_j\)：

\[
\text{softmax}(x_i) = \frac{\exp(x_i - m)}{\sum_{j=1}^{n} \exp(x_j - m)}
\]

因为 \(x_i - m \le 0\)，所以 \(\exp(x_i - m) \in (0, 1]\)，永远不溢出。减同一个常数 \(m\) 不改变 softmax 结果（分子分母同乘 \(\exp(-m)\) 约掉）。这一步在本讲四个内核里**完全一致**，差别只在于「怎么把一行数据搬进片上存储」和「怎么调度多行」。

**术语回顾（来自 u3-l1/u3-l2/u3-l3，不再展开）：**

| 术语 | 一句话回顾 |
|---|---|
| `ct.gather` / `ct.scatter` | 用「索引数组」按下标取/放，结果是一维瓦片，归约轴为 `0` |
| `ct.load` / `ct.store` | 用「锚点 `index` + 矩形 `shape`」取/放，结果是多维瓦片，贴合 TMA 硬件 |
| 静态持久化调度 | `for row_idx in range(pid, N_ROWS, num_programs)` 的 grid-stride 循环 |
| 多波（multi-wave）调度 | `row_idx = ct.bid(0)`，一块一行，grid 等于行数 |
| occupancy | 编译期提示，launch 的 `num_programs = min(NUM_SM * occupancy, n_rows)` 必须与之一致 |
| TMA | Tensor Memory Accelerator，Hopper（compute capability ≥ 9）起的硬件异步拷贝单元 |

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [src/tilegym/ops/cutile/softmax.py](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/softmax.py) | 本讲主样本：四个内核、四个 `_launch_*` 函数、`_Softmax` 封装、注册实现 |
| [src/tilegym/ops/cutile/utils.py](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/utils.py) | `next_power_of_2`，决定 `TILE_SIZE` |
| [src/tilegym/experimental.py](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/experimental.py) | `@experimental_kernel` 装饰器，给 chunked 内核打一次性告警 |
| [tests/ops/test_softmax.py](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/tests/ops/test_softmax.py) | 参数化测试，遍历四种变体与多种形状/精度 |
| [tests/common.py](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/tests/common.py) | `assertCorrectness`，决定梯度是否参与校验 |

---

## 4. 核心概念与源码讲解

### 4.1 basic：数值稳定的 grid-stride softmax

#### 4.1.1 概念说明

这是最朴素也最通用的实现：一个 block 用 grid-stride 循环处理多行，每行用 `ct.gather` 把整行搬进片上存储，做「减最大值 → exp → 求和 → 归一」四步。它是其余三种变体的**参照基准**——TMA/chunked/multi-wave 改的是「加载方式」和「调度方式」，**数学公式完全相同**。

`-inf` 填充是这里的一个关键技巧：当 `n_cols` 不是 `TILE_SIZE` 的整数倍时，越界元素被填成 \(-\infty\)，于是 \(\exp(-\infty) = 0\)，既不污染 `max`（真最大值一定 ≥ 越界值），也不污染求和（贡献为 0），从而**省掉专门的边界分支**。

#### 4.1.2 核心流程

```
pid = ct.bid(0); num_programs = ct.num_blocks(0)
for row_idx in [pid, pid+num_programs, ..., N_ROWS):       # grid-stride
    row = gather(input, (row_idx, offsets), pad=-inf)       # 一维瓦片
    row = fp32(row)
    m   = max(row, axis=0)                                  # 减最大值
    num = exp(row - m)
    den = sum(num, axis=0)
    out = fp32→input.dtype(num / den)
    scatter(output, (row_idx, offsets), out)
```

#### 4.1.3 源码精读

装饰器 `@ct.kernel(occupancy=4)` 与 grid-stride 循环（occupancy=4 表示编译期提示每 SM 可驻留 4 个块，launch 端要与之对齐）：

[softmax.py:18-31](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/softmax.py#L18-L31) —— 定义内核签名（`output/input` 张量 + `N_ROWS/TILE_SIZE/DIM_COLS` 三个 `ConstInt`），用 `ct.gather(..., padding_value=-math.inf)` 加载一维行瓦片，并进入 `for row_idx in range(pid, N_ROWS, num_programs)` 的静态持久化循环。

数值稳定四步（减最大值 → exp → 求和 → 归一），归约轴为 `0`（因 `gather` 返回一维瓦片）：

[softmax.py:37-50](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/softmax.py#L37-L50) —— `row_max = ct.max(row, 0, keepdims=True)`，`numerator = ct.exp(row - row_max)`，`denominator = ct.sum(numerator, 0, keepdims=True)`，最后 `ct.astype(..., input.dtype)` 降回原精度后 `ct.scatter` 写回。

#### 4.1.4 代码实践

**目标**：验证「减最大值」对结果无影响、对数值稳定有必要。

**步骤**（纯源码阅读 + 心算，无需 GPU）：

1. 读 [softmax.py:38-48](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/softmax.py#L38-L48)，确认四步顺序。
2. 思考：若把 `row_max` 这一步删掉、直接 `exp(row)`，对一个含 `x_i = 100` 的 fp32 行会发生什么。
3. 再思考：`padding_value=-math.inf` 改成 `0`，对 `n_cols=1009`（非 2 的幂）的行，`denominator` 会偏大还是偏小？

**预期结果**：删 `row_max` 会 `exp(100)≈2.7e43`，fp32 仍能表示但若值更大（如 200）则溢出 `inf` 导致 `inf/inf=NaN`；padding 改 `0` 后越界处 `exp(0)=1` 被计入分母，`denominator` 偏大，softmax 概率被系统性压低——结果错误。

#### 4.1.5 小练习与答案

**练习 1**：basic 内核里 `ct.max(row, 0, ...)` 的归约轴为什么是 `0` 而不是 `1`？
**答案**：`ct.gather(input, (row_idx, offsets))` 按索引数组广播取值，返回的是形状为 `(TILE_SIZE,)` 的一维瓦片（见 u3-l2），唯一的维度就是轴 `0`，故沿轴 `0` 归约。

**练习 2**：`@ct.kernel(occupancy=4)` 里的 `4` 在哪里被消费？
**答案**：在对应的 `_launch_softmax_kernel` 里，`num_programs = min(NUM_SM * 4, n_rows)`——launch 系数必须与装饰器提示一致（见 4.4.3）。

---

### 4.2 TMA：单 tile 加载整行

#### 4.2.1 概念说明

TMA 版换了一种加载原语：用 `ct.load(index=(row_idx, 0), shape=(1, TILE_SIZE))` 一次性把整行当成一个 `(1, TILE_SIZE)` 的二维瓦片搬进来。这条路径贴合 **Hopper 起（compute capability ≥ 9）的 TMA 硬件**——它用专用的异步拷贝引擎搬矩形数据块，比通用 `gather`（走 LDG 路径）更高效。代价是：老架构没有 TMA 硬件，`ct.load` 只能被软件模拟（emulated），既无收益又多开销，因此代码会在低架构上自动回退。

#### 4.2.2 核心流程

```
pid = ct.bid(0); num_programs = ct.num_blocks(0)
for row_idx in [pid, ..., N_ROWS):                          # 仍是 grid-stride
    row = load(input, index=(row_idx,0), shape=(1,TILE_SIZE), pad=NEG_INF)  # 二维瓦片
    row = fp32(row)
    m   = max(row, axis=1)                                  # 注意：轴 1
    num = exp(row - m)
    den = sum(num, axis=1)
    out = fp32→input.dtype(num / den)
    store(output, index=(row_idx,0), tile=out)
```

与 basic 唯二的差别：① 加载/写回用 `load/store` 而非 `gather/scatter`；② 归约轴从 `0` 变成 `1`（因为瓦片多了一维）。这正是 u3-l2 强调的「换加载原语就要换归约轴」。

#### 4.2.3 源码精读

TMA 加载与 axis-1 归约：

[softmax.py:92-107](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/softmax.py#L92-L107) —— `ct.load(input, index=(row_idx, 0), shape=(1, TILE_SIZE), padding_mode=ct.PaddingMode.NEG_INF)` 加载二维瓦片，随后 `ct.max(row, 1, keepdims=True)` 与 `ct.sum(numerator, 1, keepdims=True)` 沿轴 `1` 归约（注释明确要求 `TILE_SIZE >= n_cols`，即整行装进一个 tile）。

写回用 `ct.store`：

[softmax.py:113-114](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/softmax.py#L113-L114) —— `ct.astype` 降精度后 `ct.store(output, index=(row_idx, 0), tile=softmax_output)`。

#### 4.2.4 代码实践

**目标**：对照 basic 与 TMA 两个内核，体会「同一公式、不同加载原语」。

**步骤**：

1. 并排打开 [softmax.py:31-50](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/softmax.py#L31-L50)（basic）和 [softmax.py:92-114](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/softmax.py#L92-L114)（TMA）。
2. 列一张三行表：加载调用、归约轴、写回调用。
3. 回答：TMA 版为何不需要 `ct.arange` 生成列偏移？

**预期结果**：TMA 用 `index=(row_idx, 0)` 锚定左上角、`shape=(1, TILE_SIZE)` 描述矩形，硬件自己展开列范围，所以不需要手写 `arange` 偏移；basic 用 `gather` 按下标取，必须显式给出 `offsets`。

#### 4.2.5 小练习与答案

**练习 1**：为什么 TMA 内核的注释要求 `TILE_SIZE >= n_cols`？
**答案**：`ct.load` 一次只搬一个 `(1, TILE_SIZE)` 矩形；若 `TILE_SIZE < n_cols` 就装不下整行，归约会漏掉部分元素。因此 launch 端 `TILE_SIZE = next_power_of_2(n_cols)` 保证整行入瓦片。

**练习 2**：TMA 内核仍用 grid-stride 循环（`occupancy=2`），它和 multi-wave 的「一块一行」有何不同？
**答案**：TMA 的 `num_programs = min(NUM_SM*2, n_rows)`，块数可能远小于行数，每个块跨步处理多行；multi-wave 的 grid 恒等于行数，一块处理一行（见 4.4）。

---

### 4.3 chunked：分块三遍算法

#### 4.3.1 概念说明

当一行的列数 `n_cols` 非常大（比如 32768），单瓦片装不下或装得下也不划算，就把一行切成多个 `TILE_SIZE` 大小的块，分批处理。难点在于：softmax 的分母 \(\sum_j \exp(x_j - m)\) 依赖**全局**最大值 \(m\)，而 \(m\) 要遍历整行才能确定。所以 chunked 必须**三遍**扫过整行：

- **Pass 1**：扫所有块，求全局行最大值 \(m\)。
- **Pass 2**：再扫一遍，用 \(m\) 算出分母 \(\sum_j \exp(x_j - m)\)。
- **Pass 3**：第三遍，写出 \(\exp(x_i - m) / \text{denominator}\)。

这就是经典的「离线（offline）三遍 softmax」，与 attention 里用的「在线（online）softmax」（u6 会讲）相对——离线版需要多次读同一行，在线版一趟搞定但逻辑更复杂。

因为这块逻辑由外部贡献、尚未被核心团队充分验证，内核被 `@experimental_kernel` 标记，首次启动会打印一次性告警（见 [experimental.py:68-73](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/experimental.py#L68-L73)）。

#### 4.3.2 核心流程

```
m   = -inf ; den = 0 ; num_chunks = ceil(N_COLS / TILE_SIZE)
for k in range(num_chunks):           # Pass 1: 全局 max
    chunk = gather(row, cols_k, pad=-inf)
    m = maximum(m, max(chunk))
for k in range(num_chunks):           # Pass 2: 分母
    chunk = gather(row, cols_k, pad=-inf)
    den += sum(exp(chunk - m))
for k in range(num_chunks):           # Pass 3: 写出
    chunk = gather(row, cols_k, pad=-inf)
    scatter(out, cols_k, exp(chunk - m) / den)
```

每个块的列下标用 `ct.full((TILE_SIZE,), chunk_start) + col_offsets_base` 拼出（`chunk_start = chunk_idx * TILE_SIZE`），这样能在编译期常量 `TILE_SIZE` 下动态覆盖任意列段。

#### 4.3.3 源码精读

累加器初始化与 `num_chunks`：

[softmax.py:132-135](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/softmax.py#L132-L135) —— `row_max = ct.full((1,), -inf, ...)`、`denominator = ct.full((1,), 0.0, ...)`，`num_chunks = (N_COLS + TILE_SIZE - 1) // TILE_SIZE`。

Pass 1 求全局最大值（用 `ct.maximum` 做「跨块滚动最大」）：

[softmax.py:138-144](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/softmax.py#L138-L144) —— `chunk_max = ct.max(chunk, 0, keepdims=True)` 后 `row_max = ct.maximum(row_max, chunk_max)`。

Pass 2 求分母：

[softmax.py:147-155](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/softmax.py#L147-L155) —— `denominator = denominator + ct.sum(ct.exp(chunk - row_max), 0, keepdims=True)`。

Pass 3 写出（带 `check_bounds=True` 避免把 padding 的 0 写回）：

[softmax.py:158-169](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/softmax.py#L158-L169) —— 第三遍 gather 后 `softmax_output = numerator / denominator`，`ct.scatter(..., check_bounds=True)`。

#### 4.3.4 代码实践

**目标**：用纸笔算出「何时 chunked 才真正分块」。

**步骤**：

1. 读 launch 函数 [softmax.py:284-293](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/softmax.py#L284-L293)，确认默认 `TILE_SIZE=8192`。
2. 读 `_Softmax.forward` 对 chunked 的调用 [softmax.py:346](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/softmax.py#L346)：`TILE_SIZE=min(next_power_of_2(n_cols), 8192)`。
3. 对测试里的 `n = 256 / 2048 / 32768`，分别算 `num_chunks`。

**预期结果**：

| n_cols | next_power_of_2 | TILE_SIZE | num_chunks | 是否真分块 |
|---|---|---|---|---|
| 256 | 256 | 256 | 1 | 否（退化为单块三遍） |
| 2048 | 2048 | 2048 | 1 | 否 |
| 32768 | 32768 | 8192 | 4 | 是 |

只有 `n_cols > 8192` 时 chunked 才真正切成多块；测试里只有 `(256, 1024*32)` 这一例触发多块。

#### 4.3.5 小练习与答案

**练习 1**：chunked 为什么要读整行三遍，而 basic 只读一遍？
**答案**：basic 的 `TILE_SIZE >= n_cols`，整行一次性进瓦片，max/分母/写出可在一遍内串行完成；chunked 把行切成多块，全局 max 必须先遍历完所有块（Pass 1）才能算分母（Pass 2）和写出（Pass 3），故三遍。

**练习 2**：Pass 3 的 `ct.scatter(..., check_bounds=True)` 若改成 `check_bounds=False` 会怎样？
**答案**：最后一个块若越界，会把 padding 处算出的值（padding=-inf → exp=0 → 写出 0）写回 output 的越界位置，可能越界写显存或污染不该写的数据；`check_bounds=True` 保证只写真列。

---

### 4.4 multi-wave 与 autograd 封装

#### 4.4.1 概念说明

**multi-wave 内核**走另一个极端：不做 grid-stride，而是「一块一行」，grid 恒等于行数。它利用了一个编译期优化——当 `n_cols` 恰好是 2 的幂时，`TILE_SIZE == n_cols`，边界检查 `check_bounds` 可在编译期被消去（`TILE_SIZE != N` 为常量 `False`），省掉每元素的分支。内核名 `_softmax_kernel_multi_wave_full_row_reg_cached_ldg` 本身就是一份「配置清单」：`scheduling=multi_wave / coverage=full_row / load=ldg / caching=reg_cached`。

**autograd 封装 `_Softmax`** 是把四个内核收口的地方：它继承 `torch.autograd.Function`，在 `forward` 里根据三个开关做四路分发。需要特别说明：**当前源码里 `_Softmax` 只定义了 `forward`，没有定义 `backward`**；测试中输入张量 `requires_grad=False`，所以 `assertCorrectness` 走的是仅前向路径（见 [common.py:265](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/tests/common.py#L265) 的 `if ... test_out.requires_grad:` 判断）。换言之，本讲的 autograd 封装目前是「前向分发壳」，反向尚需后续补充（对比 u4-l2 的 `silu_and_mul` 才是带 `backward` 的完整 autograd 案例）。

#### 4.4.2 核心流程

multi-wave 内核（一块一行）：

```
row_idx = ct.bid(0)                       # 没有 grid-stride
offsets = arange(TILE_SIZE)
check_bound = (TILE_SIZE != N)            # 编译期常量，pow2 时被消去
row = gather(input, (row_idx, offsets), check_bounds=check_bound, pad=-inf)
... (与 basic 相同的四步) ...
scatter(output, (row_idx, offsets), out, check_bounds=check_bound)
```

`_Softmax.forward` 的四路分发：

```
assert not (use_tma and use_chunked)                     # 互斥
if use_tma and compute_capability < 9: use_tma = False   # TMA 回退 + 警告
TILE_SIZE = next_power_of_2(n_cols); MAX_TILE_SIZE = 8192
y = empty_like(x)
if use_multi_wave:   launch_multi_wave(TILE_SIZE)
elif use_chunked:    launch_chunked(min(TILE_SIZE, 8192))
elif use_tma:        launch_tma()
else:                launch_basic(TILE_SIZE)
```

#### 4.4.3 源码精读

multi-wave 内核，一块一行 + 编译期边界消去：

[softmax.py:64-76](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/softmax.py#L64-L76) —— `row_idx = ct.bid(0)`（无 grid-stride），`check_bound = TILE_SIZE != N`，`gather/scatter` 都用这个编译期常量做 `check_bounds`。

四个 launch 函数的 grid 设置（注意 occupancy 系数与内核装饰器对齐）：

- basic：[softmax.py:189-191](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/softmax.py#L189-L191) —— `num_programs = min(NUM_SM * 4, n_rows)`（对齐 `occupancy=4`）。
- multi-wave：[softmax.py:212](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/softmax.py#L212) —— `grid = (n_rows, 1, 1)`，不依赖 NUM_SM。
- TMA：[softmax.py:252-254](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/softmax.py#L252-L254) —— `TILE_SIZE = next_power_of_2(n_cols)`，`num_programs = min(NUM_SM * 2, n_rows)`（对齐 `occupancy=2`）。
- chunked：[softmax.py:291-293](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/softmax.py#L291-L293) —— `num_programs = min(NUM_SM * 4, n_rows)`（对齐 `occupancy=4`）。

`_Softmax.forward` 的互斥断言、TMA 回退、TILE_SIZE 与四路分发：

[softmax.py:321-332](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/softmax.py#L321-L332) —— `assert not (use_tma and use_chunked)`；当 `use_tma` 且 `torch.cuda.get_device_capability(x.device)[0] < 9` 时发 `UserWarning` 并把 `use_tma` 改回 `False`（注释点明「TMA may be emulated on this arch」）。

[softmax.py:334-352](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/softmax.py#L334-L352) —— `TILE_SIZE = next_power_of_2(n_cols)`、`MAX_TILE_SIZE = 8192`、`y = torch.empty_like(x)`，随后按 `use_multi_wave → use_chunked → use_tma → basic` 四路分发（chunked 用 `min(TILE_SIZE, MAX_TILE_SIZE)` 强制分块）。

注册实现：把 kwargs 里的 `use_chunked/use_multi_wave` 取出，交给 `_Softmax.apply`：

[softmax.py:356-383](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/softmax.py#L356-L383) —— `@register_impl("softmax", backend="cutile")` 把本实现挂到分发注册表的 `"softmax"` 键下（u2-l2 讲过的机制）；`use_tma` 是显式形参，`use_chunked/use_multi_wave` 从 `**kwargs` 取。

`next_power_of_2` 的位运算实现（决定 `TILE_SIZE`，是 TMA「整行入瓦片」与 chunked「分块」的前提）：

[utils.py:36-46](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/utils.py#L36-L46) —— 经典「减 1 → 逐位 OR 传播最高位 → 加 1」写法，返回 `>= n` 的最小 2 的幂。

#### 4.4.4 代码实践

**目标**：用一个最小脚本在同一张量上跑全部四种变体，确认它们结果一致。

**操作步骤**（示例代码，需在装有可用 cutile 后端的 GPU 环境运行；若环境不具备则标注「待本地验证」）：

```python
# 示例代码：对比四种 softmax 变体
import torch, tilegym
tilegym.set_backend("cutile")

x = torch.rand(256, 2048, device="cuda", dtype=torch.float32)
ref = torch.nn.functional.softmax(x, dim=-1)

for kw in [
    {},                                              # basic
    {"use_tma": True},                               # TMA（需 sm>=9，否则自动回退）
    {"use_chunked": True},           # chunked（n_cols=2048<=8192 退化为单块）
    {"use_multi_wave": True},                        # multi-wave
]:
    y = tilegym.ops.softmax(x, **kw)
    err = (y - ref).abs().max().item()
    print(f"{kw} -> max abs err = {err:.2e}")
```

**需要观察的现象**：四条输出的最大绝对误差都应在 fp32 容差内（`rtol=1e-5, atol=1e-7`，见 [test_softmax.py:64-67](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/tests/ops/test_softmax.py#L64-L67)）；在 sm<9 的卡上，`use_tma=True` 会触发一条 `UserWarning` 且结果与 basic 一致。

**预期结果**：四种变体数值一致；TMA 在低架构回退。具体运行数值待本地验证。

#### 4.4.5 小练习与答案

**练习 1**：`_Softmax.forward` 的分发顺序是 `use_multi_wave → use_chunked → use_tma → basic`。若调用 `softmax(x, use_tma=True, use_chunked=True)` 会发生什么？
**答案**：第 [softmax.py:321](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/softmax.py#L321) 行的 `assert not (use_tma and use_chunked)` 直接抛 `AssertionError`，两者互斥。

**练习 2**：multi-wave 内核没有写 `occupancy`，它的 grid 是怎么定的？
**答案**：multi-wave 是一块一行，grid 直接 `= (n_rows, 1, 1)`（[softmax.py:212](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/softmax.py#L212)），不依赖 `NUM_SM * occupancy`，所以装饰器不需要 occupancy 提示。

**练习 3**：为何说本讲的 `_Softmax` 目前只是「前向分发壳」？
**答案**：源码里 `_Softmax` 只定义了 `forward`（四路分发），没有 `backward`；且测试输入 `requires_grad=False`，`assertCorrectness` 的梯度分支不触发，所以当前只校验前向。带反向的完整 autograd 案例见 u4-l2 的 `silu_and_mul`。

---

## 5. 综合实践

**任务**：用 `tests/ops/test_softmax.py` 把四种变体的「触发条件」逐一摸清，并解释 TMA 在 `compute capability < 9` 时回退的原因。

**步骤 1：读懂参数化矩阵。** 读 [test_softmax.py:24-46](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/tests/ops/test_softmax.py#L24-L46)。它把 `(m, n, dtype)`、`backend`、`(use_tma, use_chunked, use_multi_wave)`（带测试 id `baseline/use_tma/use_chunked/use_multi_wave`）做笛卡尔积。参考实现是 `torch.nn.functional.softmax(x, dim=-1)`（[test_softmax.py:15-17](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/tests/ops/test_softmax.py#L15-L17)）。

**步骤 2：跑指定变体。** 用 pytest 的 `-k` 过滤测试 id，逐个观察：

```bash
# 只跑 baseline（basic）
pytest tests/ops/test_softmax.py -k "baseline" -v
# 只跑 TMA
pytest tests/ops/test_softmax.py -k "use_tma" -v
# 只跑 chunked
pytest tests/ops/test_softmax.py -k "use_chunked" -v
# 只跑 multi-wave
pytest tests/ops/test_softmax.py -k "use_multi_wave" -v
```

（若本机无可用 cutile 后端，相关用例会被 `pytest.skip` 跳过，具体结果待本地验证。）

**步骤 3：填触发条件表。** 结合本讲 4.1–4.4 的源码，完成下表（「触发条件」一列要写清内核被选中的规则）：

| 测试 id | 选中的内核 | 触发条件（来自 `_Softmax.forward`） |
|---|---|---|
| `baseline` | `_softmax_kernel` | 三个开关皆 `False`，走 else 分支 |
| `use_tma` | `_softmax_kernel_tma`（sm≥9）/ `_softmax_kernel`（sm<9 回退） | `use_tma=True` 且 `compute_capability≥9` |
| `use_chunked` | `_softmax_kernel_chunked` | `use_chunked=True`；`n_cols>8192` 才真分块 |
| `use_multi_wave` | `_softmax_kernel_multi_wave_full_row_reg_cached_ldg` | `use_multi_wave=True` |

**步骤 4：解释 TMA 回退。** 读 [softmax.py:323-332](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/softmax.py#L323-L332)。回答：为什么 `compute capability < 9` 时 `use_tma=True` 会被改回 `False`？

**参考答案**：TMA（Tensor Memory Accelerator）是 Hopper（sm90，compute capability 9.0）才引入的硬件异步拷贝单元。`ct.load` 的 TMA 路径在低架构上没有对应硬件，只能被软件模拟（emulated），既无性能收益又多开销；因此代码用 `get_device_capability(x.device)[0] < 9` 检测，发一条 `UserWarning` 后回退到 basic（gather 路径），保证结果正确且不白白付出模拟开销。

**步骤 5（进阶，可选）**：在 `n=1024*32` 的用例上，对比 `baseline` 与 `use_chunked` 触发的内核，确认 chunked 在大列宽下才真正分块（见 4.3.4 的 `num_chunks` 表）。

## 6. 本讲小结

- 四种 softmax 实现共享同一套「减最大值 → exp → 求和 → 归一」的数值稳定公式，差别只在**加载原语**（`gather` 一维 vs `load` 二维）与**调度方式**（grid-stride vs 一块一行 vs 分块）。
- **basic** 用 `gather` + grid-stride，是最通用的基准；**TMA** 用 `ct.load` 贴合 Hopper 硬件，低架构自动回退；**chunked** 用三遍算法处理超大列宽，被 `@experimental_kernel` 标记；**multi-wave** 一块一行，靠 `TILE_SIZE != N` 做编译期边界消去。
- 换加载原语必须同步换**归约轴**：`gather`（一维）用轴 `0`，`load`（二维 `(1,TILE_SIZE)`）用轴 `1`。
- `occupancy` 提示与 launch 端 `num_programs = min(NUM_SM * occupancy, n_rows)` 必须一致（basic/chunked=4，TMA=2，multi-wave 不用）。
- `_Softmax.forward` 按 `use_multi_wave → use_chunked → use_tma → basic` 四路分发，TMA 与 chunked 互斥；`TILE_SIZE = next_power_of_2(n_cols)`，chunked 再与 `8192` 取 min。
- 当前 `_Softmax` 只实现 `forward`，是「前向分发壳」，测试仅校验前向（输入 `requires_grad=False`）；带 `backward` 的完整 autograd 案例留待 u4-l2。

## 7. 下一步学习建议

- **进入 u4（逐元素与归一化内核、Autograd 集成）**：u4-l1 的 `silu_and_mul` 会重复本讲的 row-wise grid 模式但更简单；u4-l2 会给出**带 `backward`** 的完整 autograd 案例（含反向重计算策略），补上本讲 `_Softmax` 缺失的那一半。
- **对比阅读**：把本讲的 `softmax.py` 与 [src/tilegym/ops/cutile/rms_norm.py](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/rms_norm.py) 并排读，体会「跨列归约 + 仿射」这一类内核的共性与 `mode` 调度选择（u4-l3）。
- **远期（u6）**：本讲的「离线三遍 softmax」是理解 attention 里「在线 softmax」的最佳铺垫——u6-l1 的 FMHA 会展示如何用 `m/l` 在线更新把三遍压成一趟。
