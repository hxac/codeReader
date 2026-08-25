# Step 3：空间分块与多 CTA grid 映射

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清 Step 3 相对 Step 2 到底改了什么（答案很短：只改两处——grid 形状与每个 CTA 自己的 tile 偏移）。
2. 用 `cta_id`（即 `bx`、`by`）算出任意 CTA 负责的输出 tile 起点 `m_st`、`n_st`，并写出它加载的 A/B 切片与回写的 D 切片。
3. 给定问题规模（如 \(M=N=4096\)、`BLK_M=BLK_N=128`）算出 grid 形状与 CTA 总数。
4. 识别「同 `bx` 的 CTA 重复读同一批 A tile、同 `by` 的 CTA 重复读同一批 B tile」这一事实，并说明当前内核**没有**显式共享它们——这是后续持久内核（Step 6）与 2-CTA cluster（Step 8）优化的伏笔。

## 2. 前置知识

本讲建立在前面几讲之上，先快速回顾要用到的概念：

- **GEMM 约定（u11-l1）**：全书统一 \(D = AB^{\top}\)，其中 A 是 \(M \times K\)、B 是 \(N \times K\)、D 是 \(M \times N\)，即 \(D[m,n] = \sum_k A[m,k] \cdot B[n,k]\)。B 直接按 \(N \times K\) 存储，内核读 `B[n,k]`，运行时不做转置。
- **CTA 与 grid（u2-l1）**：CTA（Cooperative Thread Array，即 CUDA 的 thread block）是驻留在一个 SM 上的线程组；一次 launch 发出的全部 CTA 构成 grid。之前我们只见过「一个 CTA」的内核，本讲第一次让 grid 变成二维。
- **数据路径（u11-l1）**：`GMEM → SMEM → TMEM → 寄存器 → GMEM`，其中最后一跳（TMEM 读回、转 fp16、写 GMEM）叫 epilogue。Step 3 不改变这条路径。
- **K 循环与相位（u11-l3）**：Step 2 用 `T.serial(K_TILES)` 把 K 切成 64 宽的 chunk，逐块累加进同一个 TMEM 累加器，`accum=(i != 0)`，每次等待后 `phase_mma ^= 1`。本讲完全沿用这套机制。
- **scope / layout / dispatch 三要素（u9-l3）**：分析任何 tile 操作都问三个问题——谁执行、数据怎么摆、走哪条硬件路径。Step 3 是一次「只改 scope（CTA 级并行）、不改 layout 与 dispatch」的优化，正好用三要素框架看清它「动的是哪一根杠杆」。

一个值得先记住的直觉：**分块（tiling）不是新机制，而是把同一份内核代码「复印」很多份，每份用不同的坐标写自己的那块输出**。Step 1/2 的内核其实从第一行起就为这一步留好了接口，本讲会指出这一点。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [chapter_gemm_basics/index.md](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_basics/index.md) | 本讲主要源码。`Building a Tiled GEMM` 一章，含 Step 1–3 三个内核与章末练习。Step 3 从 `(chap_spatial_tiling)` 一节开始。 |
| [zh/chapter_gemm_basics/index.md](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/zh/chapter_gemm_basics/index.md) | 同一章的中文镜像，结构与英文版一一对应，可对照阅读。 |
| [tirx_guide/arch/lowering_pipeline.rst](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/tirx_guide/arch/lowering_pipeline.rst) | 编译器附录。解释 `T.cta_id` 这类「抽象执行范围标识符」在 LowerTIRx 阶段如何变成 `blockIdx.x` 等真实的 kernel launch 参数与线程绑定。本讲引用它说明 grid 形状最终去了哪里。 |

本讲涉及的可运行代码全部在 `chapter_gemm_basics/index.md` 内部（`hgemm_v3` 函数），仓库中没有独立的 `.py` 文件存放它；按本书惯例，内核源码写在 Markdown 代码块中，读者需把它拷进 `.py` 文件或 notebook 单元格再运行（TIRx 依赖 Python 源码检视解析内核，不能塞进 `python -c`）。

## 4. 核心概念与源码讲解

### 4.1 空间分块：把输出切成 128×128 的 tile 网格

#### 4.1.1 概念说明

Step 2 解除了对 K 的限制（K 可以是 64 的任意倍数），但仍然要求 `M=N=128`——整个内核只算**一个**输出 tile。真实 GEMM 的 M、N 往往远大于 128。

空间分块（spatial tiling）的思路：把 \(M \times N\) 的输出矩阵沿 M、N 两个方向切成一堆 \(128 \times 128\) 的小方块，**每个 CTA 负责其中一个方块**。这样：

- 单个 CTA 的工作量不变（仍然是一块 128×128、走 Step 2 的完整 K 循环），所以前面两讲精读的所有代码几乎原样保留；
- 但成百上千个 CTA 同时开工，输出矩阵第一次被完整覆盖。

原文把这个演进表述得很清楚：Step 3 把输出沿 M、N 划分成 128×128 tile，并为每个 tile 启动一个 CTA，见 [chapter_gemm_basics/index.md:484-491](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_basics/index.md#L484-L491)。书中给的例子是 `M=N=256, K=256`：输出是 2×2 的 tile 阵列，grid 形状为 \(2 \times 2\)，共 4 个 CTA，每个 CTA 内部跑 Step 2 的 K 循环。

需要分清两种「分块」：Step 2 的 K 分块是沿**归约维**切（把一次乘加拆成多轮累加，属于「时间上的循环」）；Step 3 的空间分块是沿**输出维**切（把独立的工作分给不同 CTA，属于「空间上的并行」）。二者正交，Step 3 两者都有。

#### 4.1.2 核心流程

设 \(G_M = M / \text{BLK\_M}\)、\(G_N = N / \text{BLK\_N}\)（本书要求整除），则：

```
输出矩阵 D (M×N)
    ├─ 切成 G_M × G_N 个 128×128 tile
    └─ 每个 tile 由一个 CTA 负责

CTA (bx, by) 的工作：
    1. m_st = bx * 128 ; n_st = by * 128        # 自己的 tile 起点
    2. for i in range(K // 64):                 # Step 2 的 K 循环原样搬来
         加载 A[m_st:m_st+128, i*64:(i+1)*64] → SMEM
         加载 B[n_st:n_st+128, i*64:(i+1)*64] → SMEM
         发起 MMA，累加进自己的 TMEM 累加器
    3. epilogue：TMEM → 寄存器 → D[m_st:m_st+128, n_st:n_st+128]
```

从并行度看：每个 CTA 128 线程、占用一份 SMEM/TMEM 资源，CTA 之间**没有任何通信或同步**——各写各的 D tile、各读各的 A/B 切片，天然互不干扰。这种「无依赖即可并行」正是空间分块成立的原因：输出 tile 之间没有数据依赖（依赖只存在于 K 维，而 K 已经由每个 CTA 内部的循环解决）。

#### 4.1.3 源码精读

Step 3 的执行结构小结（scope 变、layout 与 dispatch 不变）在 [chapter_gemm_basics/index.md:493-496](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_basics/index.md#L493-L496)：二维 CTA grid、每 CTA 拥有一个 128×128 输出 tile；CTA 内部路径与 Step 2 相同。

最有教学价值的一个细节：**这套映射公式从 Step 1 起就写在内核里了**。Step 1 的内核带着一段注释，说明 1×1 grid 让 `(m_st, n_st)` 恒为零，而 Steps 3+ 会把它推广到更大的 M/N，见 [chapter_gemm_basics/index.md:201-205](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_basics/index.md#L201-L205)：

```python
# Step 1 is a single-tile kernel: M = BLK_M and N = BLK_N, so the grid
# is 1x1. Starting with a 1x1 grid keeps the per-CTA tile offsets
# (m_st, n_st) trivially zero; Steps 3+ generalise this to larger M / N.
bx, by = T.cta_id([M // BLK_M, N // BLK_N])
```

同样，Step 1 的加载切片刻意保留了 `m_st:m_st+BLK_M` 这种「带偏移的写法」，注释明说是为了让到 Step 3 的 diff 最小，见 [chapter_gemm_basics/index.md:238-242](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_basics/index.md#L238-L242)。所以 Step 3 并不是「加上分块」，而是「让早已就位的分块代码第一次真正生效」。

`hgemm_v3` 相对 `hgemm_v2` 的全部改动，原文一句话概括为「只是两处：grid 形状与每 CTA 的偏移；内部 K 循环与回写原封不动」，见 [chapter_gemm_basics/index.md:530-542](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_basics/index.md#L530-L542)。用 `git diff` 的思路看，改动可以精确到三行，其余 ~80 行完全一致。

顺带一个观察：`hgemm_v3` 的 `wg_id` 变量声明后从未使用（单 warpgroup 内核，恒为 0），这与 Step 1/2 一致，属于「为后续步骤保留的骨架」。

#### 4.1.4 代码实践

**实践目标**：不看内核代码，仅用 Python 复现空间分块的「任务划分」，验证三件事——覆盖（每个输出元素恰好被一个 CTA 写）、无重叠、CTA 数等于 tile 数。

**操作步骤**（纯 CPU、无需 GPU 和 tvm，可直接运行）：

```python
# 示例代码：用 Python 模拟 Step 3 的空间分块（非项目源码）
def simulate_grid(M, N, BLK_M=128, BLK_N=128):
    GM, GN = M // BLK_M, N // BLK_N
    owner = {}                      # (m, n) -> 负责它的 CTA
    for bx in range(GM):
        for by in range(GN):
            m_st, n_st = bx * BLK_M, by * BLK_N
            for m in range(m_st, m_st + BLK_M):
                for n in range(n_st, n_st + BLK_N):
                    assert (m, n) not in owner, "tile 重叠！"
                    owner[(m, n)] = (bx, by)
    assert len(owner) == M * N, "没有覆盖整个输出！"
    return (GM, GN), GM * GN, owner

grid_shape, n_cta, owner = simulate_grid(256, 256)
print(grid_shape, n_cta, owner[(0, 0)], owner[(127, 255)], owner[(255, 128)])
# 预期输出：(2, 2) 4 (0, 0) (0, 1) (1, 1)
```

**需要观察的现象**：把 `M=N` 换成 512、4096，`(GM, GN)` 与 CTA 数按平方增长；把 `M` 换成 100（不能被 128 整除）观察会发生什么。

**预期结果**：`M=N=256` 时 grid 为 `(2, 2)`、4 个 CTA，与书中示例一致；`M=N=4096` 时 grid 为 `(32, 32)`、1024 个 CTA（这正是本讲综合实践要分析的规模）。`M=100` 时 `100 // 128 == 0`，grid 变成 `[0, ...]`——这暴露了本内核的隐含约束：**M、N 必须是块大小的整数倍**，内核里没有边界判断。运行结果待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：Step 2 的 K 分块和 Step 3 的空间分块，哪个改变了「一个 CTA 内部要做多少工作」？哪个改变了「需要多少个 CTA」？

**答案**：K 分块改变单个 CTA 内部的工作量（K 越大，循环轮数 `K_TILES = K // BLK_K` 越多），CTA 数量不变（仍是 1 个 tile）；空间分块改变 CTA 数量（`G_M × G_N` 个），单个 CTA 内部工作量不变（始终是一个 128×128 tile 加完整 K 循环）。

**练习 2**：为什么不同输出 tile 的 CTA 之间可以完全不做同步？

**答案**：因为它们写入的 D 区域互不相交，读取的 A/B 切片在 GMEM 中也是只读的，输出 tile 之间不存在数据依赖；唯一的归约依赖（沿 K）已经被每个 CTA 内部的 K 循环和 `mma_bar` 相位机制消化掉了。同步只需要发生在 CTA 内部的交接处（`cta_sync` 与 mbarrier），这与 Step 2 完全相同。

---

### 4.2 grid 映射：二维 grid 与 `T.cta_id`

#### 4.2.1 概念说明

grid 是一次 kernel launch 发出的全部 CTA 的集合。之前几讲的 grid 都是 `[1, 1]`——只有一个 CTA。输出 tile 现在沿 M、N 两个方向排布，所以 grid 也相应变成**二维**：CTA 的坐标 `(bx, by)` 分别标识它计算的是输出 tile 阵列的第几**行**、第几**列**。

`T.cta_id([...])` 是 TIRx 里声明「grid 形状并取回自己在 grid 中的坐标」的写法：传入的列表就是各维的 CTA 数，返回值是对应维上的坐标。它是一个**抽象执行范围标识符**——在 tile 级 IR 里它只是一个符号，编译到 CUDA 时才落成真实的 `blockIdx`。这一点在编译器附录里有明确说明：

- `TilePrimitiveDispatch` 会把 `T.cta_id`、`T.thread_id` 这类抽象标识符变成 kernel launch 参数与线程绑定，见 [tirx_guide/arch/lowering_pipeline.rst:185-190](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/tirx_guide/arch/lowering_pipeline.rst#L185-L190)；
- 附录的 scale 内核示例解释了 `T.cta_id([4])` 表示沿 x 方向 4 个 CTA，此时 `bx` 还是抽象标识符，见 [tirx_guide/arch/lowering_pipeline.rst:229-231](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/tirx_guide/arch/lowering_pipeline.rst#L229-L231)；
- LowerTIRx 随后把 `bx` 绑定到 `blockIdx.x`，见 [tirx_guide/arch/lowering_pipeline.rst:233-234](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/tirx_guide/arch/lowering_pipeline.rst#L233-L234)。

也就是说，`T.cta_id([M // BLK_M, N // BLK_N])` 里传入的 grid 形状，最终会成为 CUDA launch 配置里的 `gridDim`，而 `bx`、`by` 对应 `blockIdx.x`、`blockIdx.y`。

#### 4.2.2 核心流程

grid 形状与坐标到 tile 的映射，原文用一段极简的伪代码给出，见 [chapter_gemm_basics/index.md:498-511](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_basics/index.md#L498-L511)：

```text
grid shape = [M // BLK_M, N // BLK_N]

CTA (bx, by) 负责：
    m_st = bx * BLK_M          # 输出 tile 在 M 方向的起点
    n_st = by * BLK_N          # 输出 tile 在 N 方向的起点
    D[m_st : m_st+BLK_M, n_st : n_st+BLK_N]
```

写成公式即：

\[
\text{grid} = \left( \lceil M/128 \rceil,\ \lceil N/128 \rceil \right), \qquad
\text{CTA 数} = \frac{M}{128} \times \frac{N}{128}
\]

（本内核用整数除法且要求整除，故无需取整符号。）

`(bx, by)` 与矩阵维度的对应关系来自 \(D = AB^{\top}\) 本身：`bx` 选中 A 的行块（同时是 D 的行块），`by` 选中 B 的行块；转置之后，B 的行块恰好对应 D 的列块。原文在 [chapter_gemm_basics/index.md:526](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_basics/index.md#L526) 专门强调了这句推导——理解它，映射公式就不需要死记。

#### 4.2.3 源码精读

内核里的 grid 声明只有一行，见 [chapter_gemm_basics/index.md:563-568](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_basics/index.md#L563-L568)：

```python
T.device_entry()
# 2D grid: one CTA per 128x128 output tile
bx, by = T.cta_id([M // BLK_M, N // BLK_N])
wg_id = T.warpgroup_id([1])
warp_id = T.warp_id_in_wg([4])
lane_id = T.lane_id([32])
```

这一行做了两件事：声明 grid 为 `[M // BLK_M, N // BLK_N]`，并把当前 CTA 的二维坐标取进 `bx`、`by`。对照 Step 2 的同一行（[chapter_gemm_basics/index.md:412](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_basics/index.md#L412)，注释写着 "still one output tile (M=N=128)"）可以看到：**代码一字未变，变的是喂进去的 M、N**。Step 1/2 里 `M=N=128` 使 `M // BLK_M = 1`，grid 退化成 `[1, 1]`；Step 3 让 M、N 长大，同一行代码自然长出二维 grid。这就是 4.1.3 说的「接口早已就位」。

#### 4.2.4 代码实践

**实践目标**：亲手确认「`T.cta_id` 的参数会变成 launch 配置的 gridDim、返回值会变成 `blockIdx`」。

**操作步骤**（需要 Blackwell GPU 环境；无 GPU 时做检视推演）：

1. 把书中 `hgemm_v3`（[chapter_gemm_basics/index.md:545-632](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_basics/index.md#L545-L632)）连同书中的编译脚手架（[chapter_gemm_basics/index.md:278-321](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_basics/index.md#L278-L321)）拷进一个新 `.py` 文件，把问题规模改成 `M, N, K = 256, 256, 64`。
2. 编译后打印两级代码：`kernel.show()`（lowering 前的 tile 级 IR）与 `ex.mod.imports[0].inspect_source()`（生成的 CUDA 源码）。
3. 在 CUDA 源码里搜索 `blockIdx`，数一数 `m_st`、`n_st` 的表达式里各自出现了哪个。

**需要观察的现象**：tile 级 IR 中 `bx`、`by` 还是抽象标识符；生成的 CUDA 中出现 `blockIdx.x * 128` 与 `blockIdx.y * 128` 这样的地址计算，且 launch 处 gridDim 为 `(2, 2, 1)`。

**预期结果**：`m_st = blockIdx.x * 128`、`n_st = blockIdx.y * 128`，每个 CTA 据此读写自己的 tile；数值断言输出 `PASS`。此实践依赖 sm_100a 硬件，运行结果待本地验证；无 GPU 时，可对照 [tirx_guide/arch/lowering_pipeline.rst:202-234](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/tirx_guide/arch/lowering_pipeline.rst#L202-L234) 的 scale 内核示例做同样的推演（该示例明确写出 `bx` 绑定到 `blockIdx.x`）。

#### 4.2.5 小练习与答案

**练习 1**：`M=N=256, K=256` 时 grid 是什么形状？为什么是二维而不是一维 4 个 CTA？

**答案**：grid 为 `[2, 2]`，共 4 个 CTA（见 [chapter_gemm_basics/index.md:491](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_basics/index.md#L491)）。用二维是因为输出 tile 本来就沿 M、N 两个方向排布，`(bx, by)` 分别对应 tile 的行号与列号，坐标到 `m_st`、`n_st` 的映射各自独立、一目了然；用一维也可以（把 `by` 换成 `id // G_N` 之类），但会引入额外除法，也不如下标直接对应维度清晰。

**练习 2**：把 `T.cta_id([M // BLK_M, N // BLK_N])` 里的两个维度写反（即 `[N // BLK_N, M // BLK_M]`），内核会发生什么？

**答案**：对 `M=N` 的方阵问题（书中大多数示例），grid 形状恰好相同，内核照常正确。但对 `M ≠ N` 的情形，grid 维度与输出不匹配：例如 `M=256, N=128` 时正确的 grid 是 `[2, 1]`，写反后变成 `[1, 2]`，`bx` 只能取 0，`by` 可取 0/1——于是一半输出 tile 没人算（结果错误），且 `by=1` 的 CTA 会用 `n_st = 128` 去索引只有 128 列的 D，越界访问。这是初学分块时最常见的笔误之一。

---

### 4.3 tile 起点计算：`m_st` / `n_st` 如何进入加载与回写

#### 4.3.1 概念说明

`m_st`、`n_st`（st 即 start）是本讲真正干活的两个变量：它们把「我在 grid 里的坐标」翻译成「我要读的数据、要写的区域在矩阵里的起点」。它们是**编译期常量般的标量**（对每个 CTA 是定值，在 K 循环外计算一次），随后出现在三个地方：

1. **A 的加载切片**（M 方向偏移 `m_st`）；
2. **B 的加载切片**（N 方向偏移 `n_st`）；
3. **D 的回写行/列**（epilogue 中同时用到 `m_st` 和 `n_st`）。

注意区分三个「起点」变量：`m_st`/`n_st` 是 CTA 级 tile 起点（Step 3 新增），`i*BLK_K` 是 K 循环里的 chunk 起点（Step 2 已有），`m_thr = m_st + warp_id*32 + lane_id` 是线程级的行号（Step 1 的回写里就有，只是当时 `m_st=0` 所以看不出来）。三层坐标叠在一起才定位到一个具体的 GMEM 元素。

#### 4.3.2 核心流程

每个 K 迭代加载的切片由原文 [chapter_gemm_basics/index.md:519-524](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_basics/index.md#L519-L524) 给出：

\[
A_{\text{slice}} = A[\,m_st : m_st + 128,\ \ i \cdot 64 : (i{+}1) \cdot 64\,]
\]
\[
B_{\text{slice}} = B[\,n_st : n_st + 128,\ \ i \cdot 64 : (i{+}1) \cdot 64\,]
\]

即「行块由 CTA 坐标决定、列块由 K 循环序号决定」。整个过程：

```
m_st = bx * 128, n_st = by * 128          # 循环外，一次性
for i in 0 .. K/64-1:
    A 切片: 行 [m_st, m_st+128), 列 [64i, 64i+64)   → Asmem (128×64)
    B 切片: 行 [n_st, n_st+128), 列 [64i, 64i+64)   → Bsmem (128×64)
    MMA: D_tile += Asmem · Bsmemᵀ  (i==0 时 accum=False，否则 True)
epilogue:
    每线程的输出行 m_thr = m_st + warp_id*32 + lane_id
    写 D[m_thr, n_st : n_st+128]
```

#### 4.3.3 源码精读

偏移的计算在 K 循环之前完成，见 [chapter_gemm_basics/index.md:591-595](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_basics/index.md#L591-L595)：

```python
phase_mma: T.int32 = 0

# Per-CTA tile offsets
m_st = T.meta_var(bx * BLK_M)
n_st = T.meta_var(by * BLK_N)
```

`T.meta_var` 把表达式登记为内核元变量，此处作用是给这两个标量一个名字，便于后续切片表达式引用。

加载处的变化只有「多了偏移」，对比 Step 2 的 `A[:, i*BLK_K:(i+1)*BLK_K]`，Step 3 变成 `A[m_st:m_st+BLK_M, ...]`，见 [chapter_gemm_basics/index.md:597-602](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_basics/index.md#L597-L602)：

```python
for i in T.serial(K_TILES):
    Tx.cta.copy(Asmem[:, :], A[m_st:m_st+BLK_M, i*BLK_K:(i+1)*BLK_K])
    Tx.cta.copy(Bsmem[:, :], B[n_st:n_st+BLK_N, i*BLK_K:(i+1)*BLK_K])
    T.cuda.cta_sync()
```

MMA 一段（[chapter_gemm_basics/index.md:604-611](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_basics/index.md#L604-L611)）与 Step 2 **逐字符相同**：同样的双层守卫（`warp_id == 0` 加 `elect_sync`）、同样的 `accum=(i != 0)`、同样的 `try_wait` 加 `phase_mma ^= 1`。SMEM、TMEM、寄存器的全部布局也原样保留——这正呼应了执行结构小结里「layout: unchanged」「dispatch: unchanged」。

回写处的两层坐标叠加见 [chapter_gemm_basics/index.md:613-624](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_basics/index.md#L613-L624)：

```python
Tx.wg.copy_async(Dreg_wg[:, :], tmem[:, :BLK_N])
T.ptx.tcgen05.wait.ld()
Tx.cast(Dreg_f16[:], Dreg[:])
m_thr = T.meta_var(m_st + warp_id * 32 + lane_id)
Tx.copy(D[m_thr, n_st:n_st+BLK_N], Dreg_f16[:])
```

`m_thr` 把 CTA 级起点 `m_st` 与线程级偏移 `warp_id*32 + lane_id` 相加：warpgroup 里 128 个线程恰好对应 tile 的 128 行（warp 0 写本 tile 的第 0–31 行，warp 1 写第 32–63 行，依此类推），`n_st:n_st+BLK_N` 则把整行投到本 tile 的列范围上。Step 1 里同样的代码因为 `m_st=0` 而「看不见」这一层；Step 3 之后它才开始真正做坐标平移。

#### 4.3.4 代码实践

**实践目标**：跟踪一个具体元素的完整地址链，体会「三层坐标」如何叠加。

**操作步骤**（纸笔推演即可，无需 GPU）：

1. 设 `M=N=K=256`、`BLK_M=BLK_N=128`、`BLK_K=64`。取 CTA `(bx, by) = (1, 0)`、warp 2 的 lane 5、K 循环 `i = 2`。
2. 依次写出：`m_st`、`n_st`、该线程在 epilogue 写的 `m_thr` 与 D 列区间、本次迭代加载的 A/B 切片范围。
3. 用 4.1.4 的 `owner` 字典核对：`m_thr` 与列区间 `[n_st, n_st+128)` 内的每个元素确实都归 CTA `(1, 0)` 所有。

**需要观察的现象 / 预期结果**：

- `m_st = 128`、`n_st = 0`；
- `m_thr = 128 + 2*32 + 5 = 197`，写出 `D[197, 0:128]`；
- 本迭代加载 `A[128:256, 128:192]` 与 `B[0:128, 128:192]`；
- `owner[(197, 0)] == (1, 0)`，核对通过。

**答案自查**：若把 `m_thr` 误写成 `warp_id*32 + lane_id`（漏掉 `m_st`），CTA `(1, 0)` 的线程会去写第 0–127 行——与 CTA `(0, 0)` 的写入冲突，这正是「忘加 tile 起点」这一类 bug 的典型形态（结果时对时错，取决于哪个 CTA 后写完）。

#### 4.3.5 小练习与答案

**练习 1**：`m_st` 在内核里被计算了几次？为什么放在 K 循环外面？

**答案**：一次。它是 CTA 坐标与块大小的乘积，整个内核生命周期内不变（对每个 CTA 是定值），所以放在循环外计算；每次 K 迭代真正变化的是 `i*BLK_K` 那一维。

**练习 2**：epilogue 里每线程写「一整行 128 个 fp16」，这一行数据从哪来、经过哪几个存储空间？

**答案**：来自 TMEM 中该线程对应行的累加器值（fp32），经 `Tx.wg.copy_async`（底层是 `tcgen05.ld`）读入每线程私有寄存器 `Dreg`，`wait::ld` 之后由 `Tx.cast` 转成 fp16 存进 `Dreg_f16`，最后 `Tx.copy` 写 GMEM。路径是 `TMEM → RF → GMEM`，是整条 GEMM 数据路径（`GMEM → SMEM → TMEM → RF → GMEM`）的最后两跳，即 epilogue。

---

### 4.4 重复读取的伏笔：同 `bx` 共享 A、同 `by` 共享 B

#### 4.4.1 概念说明

空间分块带来了并行度，也带来一笔「隐性账」：**不同的 CTA 会重复读取同一批数据**。原文只用两句话点破这件事（[chapter_gemm_basics/index.md:528](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_basics/index.md#L528)）：同 `bx` 的 CTA 读相同的 A tile，同 `by` 的 CTA 读相同的 B tile，而这个版本并不跨 CTA 显式共享它们。

原因可以从映射公式直接读出：CTA `(bx, by)` 在整个 K 循环里读的是 A 的**行块** \(m_st\) 与 B 的**行块** \(n_st\)。行块只由一个坐标决定——所以沿另一个坐标排列的所有 CTA 都在读同一块数据，只是各自「不知道」。

这笔账会不会成为性能问题，取决于这些重复读命中哪一层缓存（L2 在此扮演救火队员），但从架构上看它指出了三个后续优化方向：

- **Step 6（持久内核 + tile scheduler，u12-l3）**：改变 tile 的遍历顺序，让「同时活跃」的 CTA 倾向于读同一批 tile，提高 L2 命中——共享发生在缓存里，代码不显式搬数据；
- **Step 8（2-CTA cluster，u13-l2）**：用 DSMEM 让 cluster 内两个 CTA 真正互通 SMEM，显式共享部分操作数；
- **Step 9（多消费者，u13-l3）**：在单个 CTA 内部让两个 MMA 消费者共享同一批 stage 好的 B tile，复用发生在 TMEM/SMEM 的消费者之间。

本讲只需建立「账存在、当前不还」的认知。

#### 4.4.2 核心流程

对 \(M=N\)、块大小 128 的情形做一笔量化（fp16，每元素 2 字节）：

\[
\text{A 的名义总读取量} = \underbrace{\frac{M \times K \times 2}{\text{A 本身}}}_{\text{每个 A 行块}} \times \underbrace{\frac{N}{128}}_{\text{被读的次数}}
\]

即每个 A 行块会被 \(N/128\) 个 CTA 各读一遍，每个 B 行块会被 \(M/128\) 个 CTA 各读一遍。CTA 之间不存在任何 SMEM/DSMEM 层面的共享路径——每个 CTA 的 `Asmem`/`Bsmem` 都是私有的，`Tx.cta.copy` 各搬各的。

#### 4.4.3 源码精读

看加载代码就能确认「不共享」：`Tx.cta.copy` 的作用域是 CTA 内部线程（`Tx.cta.` 前缀，见 u9-l3 的 scope 分析），目的 `Asmem`/`Bsmem` 是本 CTA 的 `SMEMPool` 里分配的私有 buffer，见 [chapter_gemm_basics/index.md:570-576](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_basics/index.md#L570-L576) 与 [chapter_gemm_basics/index.md:598-600](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_basics/index.md#L598-L600)。内核中没有任何跨 CTA 的同步原语（没有 cluster、没有 DSMEM 访问），grid 声明也不带 cluster 维度。重复读取完全交给 L2 去消化。

这也解释了为什么本章开头把「CTA clusters: let two CTAs cooperate on a single, larger Blackwell MMA tile」列为后续优化路径之一（[chapter_gemm_basics/index.md:45-54](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_basics/index.md#L45-L54)）：显式共享要到那里才出现。

#### 4.4.4 代码实践

**实践目标**：量化 4096 规模下「重复读取」的倍数，把伏笔变成数字。

**操作步骤**（纯 Python，无需 GPU）：

```python
# 示例代码：统计 Step 3 中 A/B 的名义重复读取（非项目源码）
def redundant_reads(M, N, K, BLK=128, dtype_bytes=2):
    gm, gn = M // BLK, N // BLK
    a_once = M * K * dtype_bytes          # A 全量读一遍的字节数
    b_once = N * K * dtype_bytes
    a_total = a_once * gn                 # 每个 A 行块被 gn 个 CTA 读
    b_total = b_once * gm
    return (gm, gn), gm * gn, a_once, a_total, b_once, b_total

(gm, gn), ctas, a1, aN, b1, bN = redundant_reads(4096, 4096, 4096)
print(f"grid=({gm},{gn}) CTAs={ctas}")
print(f"A: 一次 {a1/2**20:.0f} MiB, 名义总量 {aN/2**30:.2f} GiB (x{gn})")
print(f"B: 一次 {b1/2**20:.0f} MiB, 名义总量 {bN/2**30:.2f} GiB (x{gm})")
```

**需要观察的现象 / 预期结果**：grid 为 `(32, 32)`、1024 个 CTA；A 单遍 32 MiB、名义总量 1 GiB（×32），B 同样。也就是说 GMEM 名义读流量被放大了 32 倍，但其中只有 \(32+32=64\) MiB 是「新数据」——其余全部是重复，能否被 L2 消化取决于 tile 遍历顺序与调度。运行结果待本地验证。

#### 4.4.5 小练习与答案

**练习 1**：CTA `(3, 5)` 与哪些 CTA 读取完全相同的 A 数据？哪些与其共享 B 数据？

**答案**：A 的切片只由 `bx` 决定（`A[m_st:m_st+128, :]`），所以与所有 `(3, *)` 的 CTA——即 `(3, 0), (3, 1), ..., (3, G_N-1)`——读完全相同的 A 行块（共 `G_N` 个 CTA，含自己）。同理与所有 `(*, 5)` 的 CTA 共享 B 行块（共 `G_M` 个）。唯一同时与它共享 A 和 B 的只有它自己——这正是「输出 tile 互不相同」的另一种说法。

**练习 2**：如果两个 CTA 各自把同一块 A 搬进自己的 SMEM，这算不算 bug？

**答案**：不算。这是空间分块刻意接受的成本：各 CTA 的 SMEM 互不可见、也无需可见，重复搬运换取的是 CTA 间完全独立、无需同步。它只是**效率**问题而非**正确性**问题，也是后续 L2 友好调度（Step 6）与 cluster 共享（Step 8）要优化的对象。

---

## 5. 综合实践

本讲综合实践就是完成章末练习 3（[chapter_gemm_basics/index.md:638](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_basics/index.md#L638)）：对 `M=N=4096`、`BLK_M=BLK_N=128` 求 grid 形状与 CTA 数，指出 CTA `(bx, by)` 与哪些 CTA 重复读同一批 A/B tile，并说明当前内核是否显式共享它们。完整解答如下。

**第 1 问：grid 形状与 CTA 数。**

\[
G_M = 4096/128 = 32, \qquad G_N = 4096/128 = 32
\]

grid 形状为 `[32, 32]`，共 \(32 \times 32 = 1024\) 个 CTA。每个 CTA 内部跑 `K_TILES = K // 64` 轮 K 循环（K=4096 时为 64 轮），因此全 grid 共发起 \(1024 \times 64 = 65536\) 次 `Tx.gemm_async`。1024 个 CTA 远多于 GPU 的 SM 数，硬件会把它们分成多个 wave 依次执行——这个观察正是 Step 6「持久内核」（用固定数量的常驻 CTA 循环认领 tile，而不是一个 tile 一个 CTA）的出发点。

**第 2 问：谁和 CTA `(bx, by)` 重复读数据。**

- 读相同 A tile（行块 `A[bx*128 : bx*128+128, :]`）的：同 `bx` 的全部 32 个 CTA `(bx, 0), (bx, 1), ..., (bx, 31)`，含自己；
- 读相同 B tile（行块 `B[by*128 : by*128+128, :]`）的：同 `by` 的全部 32 个 CTA `(0, by), (1, by), ..., (31, by)`，含自己。

可用下面的脚本逐项核对（纯 Python，无需 GPU）：

```python
# 示例代码：练习 3 核对脚本（非项目源码）
M = N = K = 4096
BLK_M = BLK_N = 128
GM, GN = M // BLK_M, N // BLK_N
bx, by = 7, 19                      # 任取一个 CTA
m_st, n_st = bx * BLK_M, by * BLK_N

print(f"grid = [{GM}, {GN}]，CTA 数 = {GM * GN}")
print(f"CTA ({bx},{by}) 算 D[{m_st}:{m_st+BLK_M}, {n_st}:{n_st+BLK_N}]")
print(f"共享 A 行块的 CTA: {[(bx, j) for j in range(GN)]}")
print(f"共享 B 行块的 CTA: {[(i, by) for i in range(GM)]}")
print(f"A 名义读流量放大 {GN}x，B 放大 {GM}x")
# 预期输出：
# grid = [32, 32]，CTA 数 = 1024
# CTA (7,19) 算 D[896:1024, 2432:2560]
# 共享 A 行块的 CTA: [(7, 0), (7, 1), ..., (7, 31)]
# 共享 B 行块的 CTA: [(0, 19), (1, 19), ..., (31, 19)]
# A 名义读流量放大 32x，B 放大 32x
```

**第 3 问：当前内核是否显式共享这些数据？**

不共享。内核中：`Asmem`/`Bsmem` 分配在本 CTA 私有的 `SMEMPool`；加载用 `Tx.cta.copy`（CTA 内线程协作，无跨 CTA 语义）；grid 声明 `T.cta_id([M // BLK_M, N // BLK_N])` 不带任何 cluster 维度；全文没有 DSMEM 访问或跨 CTA 屏障。每个 CTA 独立把需要的 A、B 行块从 GMEM 搬进自己的 SMEM，重复部分只能指望 L2 兜底。显式的共享要等到后续章节：Step 6 用 tile 遍历顺序改善 L2 局部性（隐式共享），Step 8 用 2-CTA cluster 与 DSMEM（显式共享）。

**进阶（可选，需 Blackwell GPU）**：把 `hgemm_v3` 与书中脚手架拷进 `.py` 文件，用 `M, N, K = 256, 256, 256` 编译运行，确认 `PASS`，再用 4.2.4 的方法在生成 CUDA 中找到 `blockIdx.x/y` 对应的地址计算。运行结果待本地验证。

## 6. 本讲小结

- Step 3 只改两处：grid 形状从 `[1, 1]` 变为 `[M // BLK_M, N // BLK_N]`，加载与回写加上每 CTA 自己的偏移 `m_st = bx*BLK_M`、`n_st = by*BLK_N`；K 循环、MMA、回写逻辑逐行保留。
- 这套映射接口从 Step 1 起就写在内核里（`T.cta_id` 那一行与带偏移的切片写法），Step 3 只是让 M、N 长大、使它真正生效——书中各步骤之间是刻意保持最小 diff 的。
- `T.cta_id([...])` 的参数即 grid 形状，编译后成为 launch 的 gridDim，返回的 `bx`、`by` 绑定到 `blockIdx.x`、`blockIdx.y`；`bx` 选 A/D 的行块，`by` 选 B 的行块（对应 D 的列块），对应关系由 \(D = AB^{\top}\) 直接决定。
- 地址由三层坐标叠加：CTA 级 `m_st/n_st`、K 循环级 `i*BLK_K`、线程级 `warp_id*32 + lane_id`（epilogue 的 `m_thr = m_st + warp_id*32 + lane_id`）。
- 隐性成本：同 `bx` 的 CTA 重复读同一 A 行块、同 `by` 的重复读同一 B 行块；本内核不跨 CTA 显式共享（SMEM 私有、无 cluster/DSMEM），重复读交给 L2 消化——这是 Step 6 L2 友好调度与 Step 8 cluster 共享的伏笔。
- 本内核隐含约束：M、N 必须被块大小整除（grid 用整数除法，无边界判断）。

## 7. 下一步学习建议

下一讲（u12-l1，Step 4：TMA 异步加载）将把本讲内核中由 128 线程协作的 `Tx.cta.copy` 替换为 TMA：单线程发起、TMA 引擎搬运，load 用 mbarrier 的 `expect_tx` 字节计数等待。届时你会看到本讲的 `cta_sync` 交接被「`arrive.expect_tx` 登记字节数 + `try_wait`」取代，问题规模也随之升级到 `M=N=K=4096`——正好可以用本讲综合实践的 1024-CTA 结论去对照。

建议按顺序阅读的源码：

- 下一章开头对 TMA 加载与 store 完成机制的描述（`chapter_gemm_async/index.md`），重点关注 `expect_tx` 字节数公式与本讲 tile 尺寸的关系；
- 若想巩固 grid→`blockIdx` 的编译视角，回看 [tirx_guide/arch/lowering_pipeline.rst:202-234](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/tirx_guide/arch/lowering_pipeline.rst#L202-L234) 的 scale 内核两级对照；
- 若想提前看清「重复读取」如何被调度消化，可预览 `chapter_gemm_async` 中 Step 6 持久内核一节的 tile scheduler 描述（对应 u12-l3）。
