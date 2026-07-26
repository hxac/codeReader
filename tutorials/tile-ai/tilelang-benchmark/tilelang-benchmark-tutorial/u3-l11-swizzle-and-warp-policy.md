# swizzle、warp 策略与调优旋钮

## 1. 本讲目标

学完本讲，你应该能够：

- 说清 `T.use_swizzle(panel_size, enable)` 做的「栅格化（rasterization）」优化是什么、为什么能提升 L2 命中率。
- 区分 `T.GemmWarpPolicy.Square` 与 `from_warp_partition` 两种 warp 切分策略，理解它们只改 warp 级工作分配、不改数值结果。
- 解释 `block_M/N/K`、`num_stages`、`thread_num`、`policy`、`enable_rasteration` 这七个调优旋钮各自控制内核的哪一项决策，以及它们对显存占用、占用率（occupancy）、bank conflict 的影响。
- 读懂一份序列化的最佳配置 `test_config.json`，并能把它的字段（`BLOCK_SIZE_M/N/K`、`num_stages`、`atomic_mode` 等）逐项映射回内核决策——同时认识到它其实是 **GemLite/Triton** 的配置缓存，字段名与 TileLang 不同但概念一一对应。

## 2. 前置知识

本讲在 u3-l8（内核骨架）、u3-l9（块级 GEMM 五要素）、u3-l10（Roller 自动调优）之上继续。开始前请确认你已理解：

- **config（配置）**：一个 dict，描述「这个 block 有多大、分几段流水线、开不开栅格化」等调度选择；autotune 会遍历一堆 config 选最快的。本内核的 7 个旋钮见 [benchmark_tilelang_matmul.py:93-103](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.py#L93-L103)。
- **block / warp / thread**：一个 block 是网格里的一个并发块，内部由若干 warp 组成，每个 warp 32 个线程。`thread_num` 是 block 内线程总数，`thread_num // 32` 就是 warp 数。
- **TensorCore MMA**：GPU 上做小矩阵乘加的硬件指令（如 fp16 的 16×16×16、int8 的 16×8×32）。`T.gemm` 最终生成的是 MMA 指令。
- **L2 缓存复用**：多个 block 读取同一块全局内存时，先到的把数据带进 L2，后到的就能命中。第 6 单元第 2 讲（u2-l6）讲的 Triton「grouped pid / `GROUP_SIZE_M`」就是为提升 L2 命中率而重排 block 调度顺序——本讲的栅格化是同一思想在 TileLang 里的对应物。
- **bank conflict**：共享内存分 32 个 bank，同 warp 内多个线程访问同一 bank 会串行化、降低带宽。详见 4.1.2。

> 提醒：本内核**注释与代码不符**——注释写「half-precision」，实际 `dtype = "int8"`、`accum_dtype = "int32"`（[第 188-189 行](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.py#L188-L189)）。栅格化、warp 策略与精度无关，所以不影响本讲结论，但读源码时请始终「以代码为准」。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.py](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.py) | TileLang dense matmul 内核。本讲关注其中的 `T.use_swizzle`、`T.gemm(policy=...)`、`get_configs` 暴搜的七个旋钮，以及 `best_result.config` 的打印。 |
| [test_config.json](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/test_config.json) | 仓库根目录下**唯一一条**序列化最佳配置。它是 **GemLite/Triton** 的 autotune 缓存（不是 TileLang 配置），用来做跨框架字段对照。 |
| [hopper_benchmark/dequantize_matmul/1.triton-benchmark/benchmark_gemlite.py](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dequantize_matmul/1.triton-benchmark/benchmark_gemlite.py) | 读写 `test_config.json` 的驱动脚本（`GemLiteLinearTriton.load_config` / `cache_config`），用来理解序列化 config 是怎么产生与被消费的。 |

## 4. 核心概念与源码讲解

### 4.1 T.use_swizzle：栅格化（rasterization）调度

#### 4.1.1 概念说明

栅格化优化回答一个问题：**当网格里有成千上万个 block 时，它们按什么顺序被送上 SM 执行？**

朴素做法是把 block 索引按行优先（或列优先）一对一映射到输出 C 的子块坐标 `(bx, by)`。问题在于相邻 block 可能各算各的、互不共享数据，于是先加载进 L2 的 A/B 子块还没被第二次复用就被挤出去了，L2 命中率低。

栅格化（也叫 swizzle 调度）**重排 `block 索引 → (bx, by)` 的映射**，让一组（panel）共享同一块 A 子块（或 B 子块）的 block 在时间上紧挨着执行，从而在 L2 里「趁热」复用。这与 u2-l6 讲过的 Triton `GROUP_SIZE_M` super-blocking 是同一招：只改调度顺序，不改数值。

在 TileLang 里，这一招用一行开启：

```python
T.use_swizzle(panel_size=10, enable=enable_rasteration)
```

- `panel_size=10`：swizzle 的「面板」粒度，控制多少个输出子块被编进一个共享组（类比 Triton 的 `GROUP_SIZE_M`）。具体几何约定是 TileLang 编译器内部细节，但语义就是「按这个粒度分组成片，组内共享 A/B 提升命中」。
- `enable`：布尔开关。`True` 启用栅格化调度，`False` 退回朴素行优先。

#### 4.1.2 核心流程

为什么栅格化能提升 L2 命中？设输出被切成 `grid = (ceildiv(N,block_N), ceildiv(M,block_M))` 个子块。

- 任意 block `(by, bx)` 需要读取 `A` 的第 `by` 个 M 行带、`B` 的第 `bx` 个 N 列带。
- 所有 **`by` 相同** 的 block 共享同一份 A 行带。若它们被连续调度，A 行带就能在 L2 里被反复命中。
- 朴素行优先调度下，`by` 相同的 block 在 pid 序列里被 `grid.x` 个 step 隔开，等轮回到时 A 行带可能已被淘汰。
- swizzle 把 pid 重新打包：让 `panel_size` 个相邻 pid 落到同一个 `by`（或按面板规则交错），命中窗口被压缩进 L2 的热数据期。

> 两个「swizzle」别混淆：
> 1. **本讲的 block 调度 swizzle**（`T.use_swizzle`）——改的是网格层面 block 的执行顺序，目标是 **L2 命中**。
> 2. **共享内存布局 swizzle**（`T.copy`/`T.gemm` 内部处理）——改的是一个 block 内 shared memory 的存储交错，目标是消除 **bank conflict**。
>
> bank conflict 是另一类访存瓶颈：shared memory 分 32 个 bank（每 bank 4 字节宽），同一 warp 内多个线程同周期访问同一 bank 会串行化。若 `B_shared` 的跨步正好是 bank 周期的整数倍，整列访问会全打到同一 bank。TileLang 在 `T.copy`/`T.gemm` 里用布局 swizzle 来打散这种对齐；而 `block_N`/`block_K` 选成 bank 周期的「非整数倍友好」值能从源头减轻冲突。本讲的旋钮主要影响访存跨步，从而间接影响 bank conflict。

#### 4.1.3 源码精读

启用栅格化的那一行，紧跟在 shared/fragment 分配之后、`T.clear` 之前：

```python
# Enable (or disable) swizzling optimization
T.use_swizzle(panel_size=10, enable=enable_rasteration)
```

→ [benchmark_tilelang_matmul.py:221-222](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.py#L221-L222)：`enable` 直接绑定到 config 里的 `enable_rasteration` 字段。

该字段在暴搜空间里被设为开关二选一：

```python
enable_rasterization = [True, False]
```

→ [benchmark_tilelang_matmul.py:81](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.py#L81)：autotune 会把栅格化开/关各试一遍，因为收益依赖 shape 与硬件，无法事前断定。

> 命名瑕疵：变量名拼成 `enable_rasteration`（少一个 `i`，正确应为 `rasterization`）。代码注释写明「keep param name for backward-compat」以保持向后兼容 → [第 101 行](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.py#L101)。这也是「以代码为准」的又一例。

Roller 路径里，`enable_rasteration` 由 hint 推导：

```python
config["enable_rasteration"] = hint.rasterization_plan is not NoRasterization
```

→ [benchmark_tilelang_matmul.py:69](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.py#L69)：当 Roller 推出的栅格化方案不是「不栅格化」哨兵 `NoRasterization` 时，就打开 swizzle。

#### 4.1.4 代码实践

**实践目标**：感受 `enable_rasteration` 开/关对大 shape 的影响。

**操作步骤**（源码阅读型，无需 GPU 也能完成推理部分）：

1. 打开 [benchmark_tilelang_matmul.py:209-244](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.py#L209-L244)，确认 `T.Kernel` 网格大小是 `(ceildiv(N,block_N), ceildiv(M,block_M))`，block 数量随 M、N 增大而激增。
2. 假设 M=N=16384、block_M=block_N=128，则 grid = 128×128 = 16384 个 block。计算：朴素行优先下，`by` 相同的 128 个 block 之间在 pid 序列里相隔多远？这个间隔是否远大于典型 L2 容量能驻留的 A 行带数？
3. 若本机有 H100/A100 且已装 tilelang，可分别把 `get_configs` 里 `enable_rasterization` 临时改成 `[True]` 与 `[False]` 各跑一次（缩小搜索空间到只变这一个旋钮），对比 `Best TFlops`。

**需要观察的现象**：大 shape 下 `enable_rasteration=True` 通常更快（L2 命中提升）；极小 shape（grid 很小）时两者接近，甚至关闭略快（swizzle 本身有少量开销）。

**预期结果**：待本地验证。结论方向——栅格化收益随 grid 规模增大而增大。

#### 4.1.5 小练习与答案

**练习 1**：栅格化改变了 GEMM 的数值结果吗？为什么？
**答**：不改。它只重排 block 的执行顺序与 `(bx,by)` 映射，每个 block 仍计算同一块 C 子块、读同样的 A/B 数据，浮点累加顺序也不变。

**练习 2**：`panel_size` 越大越好吗？
**答**：不一定。面板越大，组内共享越充分，但也要求 L2 能同时容纳一整组 A 行带；超过 L2 容量后反而互相淘汰、命中率下降。最佳值依赖 shape 与 L2 大小，故交给 autotune 决定。

### 4.2 GemmWarpPolicy：warp 切分策略

#### 4.2.1 概念说明

一个 block 算的是 `block_M × block_N` 的输出子块，但这个子块不是「一个 warp 一口气算完」的——它被进一步切成若干 **warp 子块**，每个 warp 用 TensorCore MMA 指令算自己那一片。`GemmWarpPolicy` 决定的就是「block 子块如何切给 warp」。

本文件出现两种策略：

- **`T.GemmWarpPolicy.Square`**：把 `block_M × block_N` 切成近似方形的 warp 子块网格（如 4 个 warp 切 2×2）。
- **`T.GemmWarpPolicy.from_warp_partition(block_rows, block_cols)`**：按显式给出的 `block_rows = block_M // warp_m`、`block_cols = block_N // warp_n` 来切，允许非方形。

关键认识：`policy` 只影响 **warp 级工作分配**——哪个 warp 算哪个 MMA fragment、累加器如何摆放。它**不改变 `(bx, by)` 网格映射、不改变数值结果**，只影响 TensorCore 利用率、寄存器占用与 shared 访问模式。

#### 4.2.2 核心流程

warp 切分的推导链（Roller 路径）：

1. Roller hint 给出 `hint.warp = (warp_m, warp_n)`：单个 warp 负责的子块尺寸。
2. 由 block 尺寸反推 warp 网格：`block_rows = block_M // warp_m`、`block_cols = block_N // warp_n`（即 M/N 方向各放几个 warp 子块）。
3. warp 总数 = `block_rows × block_cols`；`thread_num = warp 总数 × 32`。
4. 把 `(block_rows, block_cols)` 传给 `from_warp_partition` 生成 policy，再喂给 `T.gemm`。

举一个数字例子（仅作示意）：`block_M=block_N=128`、hint 给 `warp=(64,64)`，则 `block_rows=block_cols=2`，共 4 个 warp、`thread_num=128`，policy 为 2×2 方形分割——这其实等价于 `Square`。若 hint 给 `warp=(128,32)`，则 `block_rows=1, block_cols=4`，4 个 warp 排成 1×4 长条，是非方形分割，`Square` 表达不出来，只能用 `from_warp_partition`。

#### 4.2.3 源码精读

暴搜路径只搜方形策略：

```python
policy = [T.GemmWarpPolicy.Square]
```

→ [benchmark_tilelang_matmul.py:80](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.py#L80)：暴搜固定方形，搜索空间小但放弃了非方形可能。

Roller 路径根据 hint 推非方形：

```python
block_m, block_n = hint.block
warp_m, warp_n = hint.warp
block_rows, block_cols = block_m // warp_m, block_n // warp_n
config["thread_num"] = block_rows * block_cols * 32
config["policy"] = T.GemmWarpPolicy.from_warp_partition(block_rows, block_cols)
```

→ [benchmark_tilelang_matmul.py:59-68](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.py#L59-L68)：由 warp 维度算出 thread_num 与 policy——这是 Roller 能产出暴搜列表之外配置（非方形 policy、非 {128,256} 的 thread_num）的根源。

policy 最终传给 `T.gemm`：

```python
T.gemm(
    A_shared,
    B_shared,
    C_local,
    transpose_B=True,
    policy=policy,
)
```

→ [benchmark_tilelang_matmul.py:235-241](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.py#L235-L241)：`policy` 决定这次 MMA 的 warp 子块切法；`transpose_B=True` 是因为 B 以 `(N,K)` 存储而 `T.gemm` 做 `C += A @ B^T`（见 u3-l9）。

#### 4.2.4 代码实践

**实践目标**：把 Roller hint 的 `warp` 字段换算成 thread_num 与 policy。

**操作步骤**：

1. 读 [第 59-68 行](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.py#L59-L68)，记下换算公式。
2. 假设某 hint 为 `block=(128,256)`、`warp=(64,64)`，手算 `block_rows`、`block_cols`、`thread_num`，并判断它等价于方形还是长条。
3. 若本机能 `--with_roller` 运行，观察打印的 config 列表（[第 71-72 行](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.py#L71-L72) 的 `print(config)`），核对其中 `thread_num` 是否都满足「`thread_num // 32 = block_rows × block_cols`」。

**预期结果**：上例 `block_rows=2, block_cols=4, thread_num=256`，是 2×4 长条（非方形）。`Square` 策略无法表达，正是 Roller 路径的价值。

#### 4.2.5 小练习与答案

**练习 1**：`thread_num=256` 一定对应方形 warp 分割吗？
**答**：不一定。256 线程 = 8 warp。方形分割是 8=2×4 或不规则的近似方形；也可能是 1×8、2×4、4×2、8×1 等。具体由 `(block_rows, block_cols)` 决定，故用 `from_warp_partition` 才能精确表达。

**练习 2**：把 `policy` 从 `Square` 换成另一个值会改变 `C` 的最终数值吗？
**答**：不会。policy 只改 warp 间工作分配与累加器布局，每个输出元素仍是同一组 A、B 数据的同一组乘加（MMA 指令本身是确定性的）。

### 4.3 调优旋钮：block 尺寸、流水线、线程数

#### 4.3.1 概念说明

把前两节加上 u3-l9 的内容汇总，本内核的 config 一共七个旋钮：

| 旋钮 | 取值（暴搜） | 控制的内核决策 |
|---|---|---|
| `block_M` | {64,128,256} | 输出子块的 M 边长 |
| `block_N` | {64,128,256} | 输出子块的 N 边长 |
| `block_K` | {64,128,256} | 每轮 K 循环搬运的 K 长度 |
| `num_stages` | {0,1,2,3} | `T.Pipelined` 软件流水深度 |
| `thread_num` | {128,256} | 每 block 线程数（= warp 数×32） |
| `policy` | {Square} | warp 子块切分 |
| `enable_rasteration` | {True,False} | 是否启用栅格化 swizzle |

暴搜空间大小：

\[ 3 \times 3 \times 3 \times 4 \times 2 \times 1 \times 2 = 432 \]

即 `get_configs(with_roller=False)` 会产出 **432 个** config 供 autotune 遍历（u3-l10 已指出 Roller 路径约 10 个、缩减比约 43:1）。

#### 4.3.2 核心流程

每个旋钮都在「算得快」与「资源占用」之间权衡：

- **`block_M`/`block_N`**：越大 → 单 block 算术强度越高（更多算术 / 同样一次取指针开销）、grid 数越少；但 shared 内存占用（尤其 `C_shared = block_M×block_N×accum_dtype`）线性增长，占用率下降。
- **`block_K`**：越大 → K 循环轮数 `ceildiv(K,block_K)` 越少；但每轮 shared 占用 `block_M×block_K + block_N×block_K` 增大。
- **`num_stages`**：>0 时 `T.Pipelined` 把后续轮的取数与当前轮的计算重叠，隐藏全局内存延迟；代价是 shared 占用≈按 stage 数翻倍（多缓冲）。0 表示不流水。
- **`thread_num`**：要与 block 尺寸、policy 匹配。太少则单线程工作过多、并行度不足；太多则寄存器/shared 压力大、占用率下降。
- **`enable_rasteration`**：大 grid 提 L2 命中；小 grid 收益小（见 4.1）。

**shared 内存约束**（决定一个 config 是否合法）。单个 block 的 shared 占用（int8 路径，dtype=1B、accum int32=4B）约为：

\[ S \approx \text{num\_stages} \times (b_M b_K + b_N b_K) \times 1\text{B} \;+\; b_M b_N \times 4\text{B} \]

（`A_shared`/`B_shared` 被流水线多缓冲，`C_shared` 不缓冲只写一次。）autotune 在编译期会把超过硬件 shared 上限的 config 直接判非法并剔除——这就是为什么不是所有 432 个组合都能跑。

> 示例（仅作数值演示）：`b_M=b_N=b_K=128`、`num_stages=3`：
> \( S \approx 3 \times (128\cdot128 + 128\cdot128)\times1 + 128\cdot128\times4 = 98304 + 65536 \approx 160\text{KB} \)。
> 这在 Hopper（单 SM 可配 ~228KB）能放下，在 Ampere（~164KB 上限）就接近极限、可能被剔除。同一 config 跨架构合法性不同，正是第 7 单元「跨架构适配」要处理的问题。

#### 4.3.3 源码精读

旋钮定义在暴搜分支：

```python
block_M = [64, 128, 256]
block_N = [64, 128, 256]
block_K = [64, 128, 256]
num_stages = [0, 1, 2, 3]
thread_num = [128, 256]
policy = [T.GemmWarpPolicy.Square]
enable_rasterization = [True, False]
```

→ [benchmark_tilelang_matmul.py:75-81](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.py#L75-L81)：七个旋钮的取值域。

旋钮如何在内核里被消费：

- `block_M/N/K` 决定 `T.Kernel` 网格与所有 alloc 尺寸 → [第 209-219 行](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.py#L209-L219)。
- `thread_num` 传给 `T.Kernel(..., threads=thread_num)` → [第 210 行](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.py#L210)。
- `num_stages` 传给 `T.Pipelined(..., num_stages=num_stages)` → [第 228 行](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.py#L228)。
- `policy` 传给 `T.gemm(..., policy=policy)` → [第 240 行](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.py#L240)。
- `enable_rasteration` 传给 `T.use_swizzle(..., enable=enable_rasteration)` → [第 222 行](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.py#L222)。

autotune 选出的最优 config 最终通过 `best_result.config` 取出并打印：

```python
best_config = best_result.config
...
print(f"Best config: {best_config}")
```

→ [benchmark_tilelang_matmul.py:273-280](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.py#L273-L280)：注意 TileLang 在本仓库里**只打印**最优 config、不落盘成 JSON；这与下一节 GemLite 把 config 缓存到文件形成对比。

#### 4.3.4 代码实践

**实践目标**：估算一组 config 的 shared 内存占用，判断它是否可能被 autotune 剔除。

**操作步骤**：

1. 选 `block_M=block_N=block_K=256`、`num_stages=3`，代入 4.3.2 的公式算 S。
2. 与 Ampere（~164KB/SM）、Hopper（~228KB/SM）的 shared 上限比较。
3. 打开 [第 75-81 行](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.py#L75-L81)，确认这个组合确实在搜索空间里——预测它在 Ampere 上大概率被判非法。

**预期结果**：\( S \approx 3\times(256\cdot256\times2) + 256\cdot256\times4 = 393216 + 262144 \approx 640\text{KB} \)，远超两代架构上限，必然被剔除。这说明暴搜的 432 个组合里有相当一部分是「占位但跑不起来」的，实际有效搜索空间更小。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `num_stages` 越大，shared 占用越大？
**答**：`T.Pipelined` 用多缓冲实现软件流水——为重叠「取下一轮 tile」与「算当前 tile」，需要同时保留 `num_stages` 份 `A_shared`/`B_shared` 缓冲，故 shared 随 stage 数近似线性增长。

**练习 2**：`thread_num` 能不能任意大于 `block_M×block_N` 对应的 warp 需求？
**答**：不能任意。`thread_num` 必须与 block 尺寸、policy 自洽（`thread_num//32 = block_rows×block_cols`），否则线程没有合理的 MMA 工作量分配，要么 idle、要么寄存器溢出。autotune 在合法空间内选最优。

### 4.4 序列化 config：test_config.json 跨框架对照

#### 4.4.1 概念说明

本节是本讲的关键对照点。仓库根目录的 `test_config.json` **不是 TileLang 配置**，而是 **GemLite（基于 Triton 的量化 GEMM 库）** 的 autotune 缓存。之所以放在本讲，是因为它把「栅格化、warp 切分、tile 尺寸、流水线」这些**通用 GEMM 调优概念**用另一套字段名序列化了出来——读懂它，你就能在 TileLang 与 Triton 系之间自由翻译。

先看它的结构（根目录这份只有一条记录）：

```json
{
  "GEMV": {},
  "GEMV_REVSPLITK": {
    "(1, 4096, 4096, 128, 8)": {
      "BLOCK_SIZE_M": 1, "BLOCK_SIZE_N": 512, "BLOCK_SIZE_K": 8,
      "A_load_order": 1, "meta_evict_policy": "", "atomic_mode": "relaxed",
      "dot_prod_mode": 0, "num_warps": 4, "num_ctas": 1, "num_stages": 2,
      "num_buffers_warp_spec": 0, "num_consumer_groups": 0,
      "reg_dec_producer": 0, "reg_inc_consumer": 0
    }
  },
  "GEMV_SPLITK": {}, "GEMM_SPLITK": {}, "GEMM": {}
}
```

→ [test_config.json:1](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/test_config.json#L1)：最外层五个键是 **GemLite 的内核变体**——按 shape 自动选用：M 很小走 `GEMV*`，M 较大走 `GEMM*`。每个变体里，**键是 shape 元组 `(M, N, K, group_size, W_nbits)`**，**值是该 shape 的最优 config**。

#### 4.4.2 核心流程

**GemLite 如何消费这份文件**（在 `benchmark_gemlite.py` 里）：

1. `GemLiteLinearTriton.load_config('test_config.json')` → 启动时读入缓存，命中 shape 就直接用记录的最优 config，**跳过耗时的 autotune**。
2. 跑完 benchmark 后 `GemLiteLinearTriton.cache_config('test_config.json')` → 把新调出来的最优 config 写回文件。

→ [benchmark_gemlite.py:43](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dequantize_matmul/1.triton-benchmark/benchmark_gemlite.py#L43) 与 [第 247 行](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dequantize_matmul/1.triton-benchmark/benchmark_gemlite.py#L247)：load/cache 一对。

这条记录的 shape `(1, 4096, 4096, 128, 8)` 含义：`M=1`（GEMV）、`N=4096`（输出特征）、`K=4096`（输入特征）、`group_size=128`、`W_nbits=8`（int8 权重）。变体名 **`GEMV_REVSPLITK`** 指「反向 split-K」：对 GEMV（M=1）不切 K 维，而是把 **N 输出维**切成多块，每块算一部分输出列，再用 atomic 加合并——这能更好地填满 GPU。

#### 4.4.3 源码精读

逐字段映射到内核决策（直接回答实践任务）：

| 字段（GemLite/Triton） | 本记录值 | 对应内核决策 | TileLang 对应物 |
|---|---|---|---|
| `BLOCK_SIZE_M` | 1 | 每 block 的 M tile；=1 因 GEMV 单向量 | `block_M` |
| `BLOCK_SIZE_N` | 512 | 每 block 算的输出列数（N tile） | `block_N` |
| `BLOCK_SIZE_K` | 8 | K 内层循环每轮归约的长度 | `block_K` |
| `num_stages` | 2 | 软件流水深度，重叠下轮取数与当前计算 | `num_stages`（`T.Pipelined`） |
| `num_warps` | 4 | 每 block 的 warp 数（=线程数/32） | `thread_num // 32` |
| `num_ctas` | 1 | Hopper 多 CTA 协作组数；1=关闭 | （TileLang 无直接对应） |
| `atomic_mode` | "relaxed" | split-N 合并时 atomic 加的内存序；relaxed 低开销 | （TileLang 无直接对应） |
| `A_load_order` | 1 | 激活矩阵 A 的加载/排布顺序，改善缓存命中 | `enable_rasteration`（概念对应） |
| `meta_evict_policy` | "" | scale/zero 元张量的 L2 驱逐策略；空=默认 | （无直接对应） |
| `dot_prod_mode` | 0 | 点积实现变体选择（0=默认 dp4a 路径） | （无直接对应） |
| `num_buffers_warp_spec` / `num_consumer_groups` / `reg_dec_producer` / `reg_inc_consumer` | 全 0 | Hopper warp specialization（生产-消费 warp）；全 0=关闭 | （无直接对应） |

→ 完整记录见 [test_config.json:1](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/test_config.json#L1)。

**一句话对照**：`BLOCK_SIZE_M/N/K` ↔ TileLang 的 `block_M/N/K`（tile 尺寸）；`num_stages` 两边同名同义（流水深度）；`num_warps×32` ↔ `thread_num`；`A_load_order`/栅格化 ↔ `enable_rasteration`（调度 swizzle 提 L2 命中）；`atomic_mode`、`num_ctas`、warp_spec 系列是 Triton/GemLite 特有（split 合并、Hopper 协作组与生产-消费 warp），TileLang 在本 dense matmul 内核里没有直接对应旋钮。

#### 4.4.4 代码实践（对应总实践任务）

**实践目标**：在 `test_config.json` 里定位 `BLOCK_SIZE_M/N/K`、`num_stages`、`atomic_mode` 等字段，逐句解释对应内核决策。

**操作步骤**：

1. 打开 [test_config.json](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/test_config.json)，定位唯一一条记录（键 `(1, 4096, 4096, 128, 8)`）。
2. 按下表逐字段写一句话解释（参考 4.4.3 的映射表）：
   - `BLOCK_SIZE_M=1`：每个 block 只处理 1 行 query，因为这是 GEMV（M=1）。
   - `BLOCK_SIZE_N=512`：每个 block 负责 512 个输出列，把 N 维并行铺开。
   - `BLOCK_SIZE_K=8`：K 归约每轮算 8 个元素，GEMV 下 A 很短、K tile 取小。
   - `num_stages=2`：两级软件流水，下一轮取 B 与当前计算重叠。
   - `atomic_mode="relaxed"`：反向 split-K 合并各 block 的列结果时，用最低开销的松弛内存序做 atomic 加。
3. 打开 [benchmark_gemlite.py:43](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dequantize_matmul/1.triton-benchmark/benchmark_gemlite.py#L43) 与 [第 247 行](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dequantize_matmul/1.triton-benchmark/benchmark_gemlite.py#L247)，确认这份 JSON 是 `load_config` 读、`cache_config` 写，从而理解它是「调优结果的持久化缓存」。
4. （延伸）对比 [cdna_benchmark/dequantize_matmul/1.triton-benchmark/test_config.json](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/cdna_benchmark/dequantize_matmul/1.triton-benchmark/test_config.json)，它记录了多个 shape，注意不同 shape 下 `BLOCK_SIZE_N`/`num_warps`/`num_stages` 取值不同——这正是「shape 感知」调优的体现。

**需要观察的现象**：同一变体内，大 K 往往配更大 `num_stages`（更多流水隐藏延迟）；M 增大后变体从 `GEMV_REVSPLITK` 切到 `GEMM_SPLITK`/`GEMM`，`BLOCK_SIZE_M` 随之从 1 变成 16/32/64。

**预期结果**：能复述每个字段的一句话决策映射，并能解释为何 `GEMV_REVSPLITK` 用「切 N + atomic」而非「切 K」。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `GEMV_REVSPLITK` 里 `BLOCK_SIZE_M=1` 而 `BLOCK_SIZE_N=512` 这么不对称？
**答**：GEMV 的 M=1，M 维没有并行可挖，只能把并行度全压到 N 维（输出列），故 N tile 取大、M tile 取 1。

**练习 2**：`atomic_mode="relaxed"` 相对更严格的内存序省了什么？
**答**：省的是内存栅栏（fence）开销。反向 split-K 各 block 写的是**不同的输出列**（互不重叠），合并时只需 atomic 加保证写不丢失，不需要全局顺序，故用最松的 relaxed 序即可，减少同步等待。

**练习 3**：能否把根目录 `test_config.json` 的字段直接喂给 TileLang 内核？
**答**：不能直接喂——字段名与语义都不完全对应（如 `num_warps` 要 ×30 才是 `thread_num` 的概念、`atomic_mode` 在本 TileLang GEMM 里无对应）。需按 4.4.3 的映射表手工翻译；且该 config 是为 W4/W8 **量化** GEMV 调的，与 dense int8 GEMM 的最优 config 未必相同。

## 5. 综合实践

把本讲四个模块串起来，做一次「跨框架 config 翻译」：

**任务**：给定根目录 `test_config.json` 里 `GEMV_REVSPLITK` 的那条记录，把它「翻译」成一份等价的 TileLang `get_configs` 单元素列表，并标注哪些字段能翻、哪些翻不了。

**步骤**：

1. 从记录读出 `BLOCK_SIZE_M=1, BLOCK_SIZE_N=512, BLOCK_SIZE_K=8, num_stages=2, num_warps=4`。
2. 翻译：
   - `block_M=1, block_N=512, block_K=8`
   - `num_stages=2`
   - `thread_num = num_warps × 32 = 128`
   - `policy`：4 warp，按方形近似取 `T.GemmWarpPolicy.Square`
   - `enable_rasteration`：`A_load_order=1`（非默认 0）暗示开了某种排布优化，可近似设 `True`
3. 标注无法翻译的：`atomic_mode`、`num_ctas`、`dot_prod_mode`、`meta_evict_policy`、四个 warp_spec 字段——这些在本 TileLang dense GEMM 内核没有对应旋钮。
4. 反思：翻译后的 config 在 TileLang dense GEMM 上**未必最优**，因为原始 config 是为「W8 量化 GEMV + 反向 split-K」调的，shape（M=1）也与 dense benchmark 的 M=16384 完全不同。本练习的意义是练「字段语义映射」，不是复用数值。

**预期结果**：产出一份带注释的 config dict，并写出至少 3 条「无法直接翻译」的字段及原因。

## 6. 本讲小结

- `T.use_swizzle(panel_size, enable)` 做**网格层 block 调度 swizzle（栅格化）**，重排 block→输出子块的映射以提升 L2 命中率；与 Triton 的 `GROUP_SIZE_M` 同源，不改数值。
- `GemmWarpPolicy` 决定 **warp 级子块切分**：`Square` 是方形、`from_warp_partition` 允许由 Roller hint 推出的非方形；只改工作分配，不改数值。
- 七个调优旋钮（`block_M/N/K`、`num_stages`、`thread_num`、`policy`、`enable_rasteration`）在「算得快」与「shared/寄存器占用、占用率、bank conflict」间权衡；暴搜空间 432 个，但受 shared 上限约束，有效组合更少。
- `test_config.json` 是 **GemLite/Triton** 的 autotune 缓存，字段名（`BLOCK_SIZE_*`、`num_warps`、`atomic_mode`、`A_load_order`）与 TileLang 不同但概念一一对应；它是「shape→最优 config」的持久化表，由 `load_config`/`cache_config` 读写。
- 跨框架对比的要点：**调优概念是通用的**（tile 尺寸、流水深度、调度 swizzle、warp 切分），各框架只是用不同名字序列化它们；翻译 config 时务必核对单位（`num_warps` vs `thread_num`）与是否有对应旋钮。
- 持续「以代码为准」：`enable_rasteration` 是 `rasterization` 的拼写错误（保留以向后兼容）、注释与 dtype 不符，都是历史遗留。

## 7. 下一步学习建议

- **横向**：进入第 4 单元（u4-l12 起多精度与量化 matmul），看 `dtype`/`accum_dtype` 如何切换 int8/fp16 路径，以及 int8 走 dp4a/TensorCore 对峰值 TFlops 的影响——本讲的旋钮在低精度下取值会不同。
- **纵向**：若对 swizzle 与 warp 切分的底层 MMA 指令映射感兴趣，建议在能跑的环境里用 `best_result.kernel.get_kernel_source()`（[第 275 行](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.py#L275)）打印生成的 CUDA 源码，对照本讲的旋钮观察 `wmma`/`mma.sync` 指令与 shared 布局。
- **配套阅读**：`benchmark_gemlite.py` 完整的 load/cache 流程，以及 cdna 版 `test_config.json` 的多 shape 记录，巩固「shape 感知调优」的直觉，为第 7 单元（u7-l23 基线生态、u7-l24 跨架构适配）做铺垫。
