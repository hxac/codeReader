# 静态持久化调度与 group_gemm

## 1. 本讲目标

上一讲（u5-l1）讲的是「最干净的分块 GEMM」：`_matmul_kernel` 里**一个 CTA 算一个输出瓦片**，主机侧 grid 大小就等于输出瓦片总数。这在小矩阵上没问题，但当输出瓦片数远多于 GPU 上的 SM（流式多处理器）数量时，会暴露两个问题：grid 暴涨带来调度开销、且无法利用 SM 间的协作（CGA）。

本讲在 u5-l1 的基础上，进入 cuTile 版 matmul 的**高阶调度层**，回答五个问题：

- **持久化 grid-stride 调度**：`_static_persistent_matmul_kernel` 如何用一个 `for tile_id in range(start_bid, num_tiles, num_programs)` 的循环，让「块数少于瓦片数」时每个 CTA 连续算多个输出瓦片——即「瓦片多于 SM 时复用 CTA」。
- **`num_ctas`/CGA**：什么是 CGA（Cooperative Group Array / 线程块簇），`num_ctas` 如何把多个 CTA 聚合成一个协作组，以及它为何只在 sm90+ 上有效。
- **`replace_hints` 应用最优配置**：autotune 选出最优配置后，如何用 `kernel.replace_hints(num_ctas=..., occupancy=...)` 生成一个「烤进编译期 hint」的新内核对象，以及为什么必须把它和配置一起缓存。
- **小 tile 候选与形状适配（本轮新增）**：为什么 sm100+ 的持久化候选表里，除了 256×256 这类大 tile，还要补一条 128×128×64 的小 tile——当 GEMM 较小或呈窄长形（rectangular）时，大 tile 会把输出瓦片数 `num_tiles` 压到只有 16–32 个，持久化 grid 被 `min(NUM_SMS // num_ctas, num_tiles)` 的第二项卡死、闲置大部分 SM；小 tile 把瓦片数放大约 4 倍，让更多 SM 领到活。
- **`group_gemm` 批量矩阵乘**：如何用**一次内核启动**算完一组形状各异的矩阵乘，内核内用 `last_problem_end` 累计偏移把多个问题缝合成一条瓦片流。

学完本讲你应当能说清「静态持久化」相对「一块一瓦片」的本质差别，看懂 `_group_gemm_kernel` 这种把多问题塞进一个持久化内核的写法，并解释本轮新增的小 tile 候选为何能救活小/矩形 GEMM 上的 SM 占用。

## 2. 前置知识

本讲默认你已经学完：

- **u3-l1（cuTile 内核基础）**：`@ct.kernel`、`ConstInt`（`ct.Constant[int]`，编译期常量）、`ct.bid(0)`/`ct.num_blocks(0)` 对应 blockIdx / gridDim。
- **u3-l3（启动模式）**：主机侧计算 grid、`ct.launch(stream, grid, kernel, args)` 四参约定、SM（`multi_processor_count`）、occupancy 提示须与 launch 端一致。
- **u5-l1（分块矩阵乘）**：`_matmul_kernel` 一块算一个 `TILE_SIZE_M × TILE_SIZE_N` 输出瓦片；`_swizzle_2d` 的 super-grouping 块重排；K 方向 `for k in range(num_tiles_k)` 的分块累加；fp32 累加器 + tf32 张量核心。

本讲新引入的几个术语先给直觉：

- **持久化调度（persistent scheduling）**：启动固定数量的 CTA（通常与 SM 数量相关），让每个 CTA 在内核内部用一个循环**主动领取**多个工作单元（这里是输出瓦片），而不是让硬件调度器为每个工作单元单独派发一个 CTA。「静态」指分配方案在编译期/启动期就确定（`for tile_id in range(start, end, stride)`），不需要运行期原子计数器。
- **grid-stride 循环**：`for i in range(start_bid, total, num_programs)` 的经典写法——块 `start_bid` 处理 `start_bid, start_bid+num_programs, start_bid+2*num_programs, ...`。相邻 CTA 错开 `num_programs`（=总块数）步，正好不重叠地铺满 `[0, total)`。你已经在 u3-l1/u3-l4 的 softmax grid-stride 里见过它，本讲是它在 GEMM 上的应用。
- **CGA（Cooperative Group Array）/ 线程块簇（Thread Block Cluster）**：Hopper（sm90）起引入的硬件特性，允许 `num_ctas` 个 CTA 组成一个协作组，彼此可访问对方的分布式共享内存、可做 TMA 多播（multicast）。本讲里 `num_ctas` 就是「一个 CGA 里包含几个 CTA」。
- **`replace_hints`**：cuTile 提供的方法，对一个已有的 `@ct.kernel` 内核对象，**重新指定编译期 hint（`occupancy` 与 `num_ctas`）**，返回一个**新的、带独立 JIT 缓存**的内核对象。
- **stranded SM（闲置的 SM）**：持久化 grid 公式里有个 `min(NUM_SMS // num_ctas, num_tiles)`。当 tile 选得太大、GEMM 又偏小或偏窄长时，`num_tiles` 只有十几个，`min` 被 `num_tiles` 卡住，于是整张卡只启动十几个 CTA、绝大多数 SM 全程没活干——这就是「SM 被 strand（搁浅）」。补小 tile 候选就是为解开这个卡点。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/tilegym/ops/cutile/matmul.py](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/ops/cutile/matmul.py) | 本讲主样本。`_static_persistent_matmul_kernel`（持久化 grid-stride GEMM）、`_static_persistent_matmul_autotune_configs`（含 `num_ctas`/CGA、`LOAD_LATENCY`，以及本轮新增的 128×128 小 tile 候选）、`_cutile_autotune_static_persistent_matmul`（tune-once/cache/launch + `replace_hints`），以及作为对照的非持久化 `_matmul_kernel` 与其启动函数。 |
| [src/tilegym/ops/cutile/group_gemm.py](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/ops/cutile/group_gemm.py) | 批量矩阵乘。`_group_gemm_kernel` 用一次启动处理一组形状各异的矩阵乘，演示持久化调度如何跨多个问题复用 CTA。 |
| [src/tilegym/autotune.py](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/autotune.py) | 全局 autotune 开关 `TILEGYM_DISABLE_AUTOTUNE`，业务侧只调 `is_autotune_enabled()`。本讲的 `replace_hints` 路径受它门控。 |
| [tests/ops/test_matmul.py](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/tests/ops/test_matmul.py) | 用法与容差范例。`test_op` 把 `static_persistent` 作为参数（`True`/`False`）遍历，是验证持久化路径正确性的最权威参照。 |
| [tests/ops/test_group_gemm.py](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/tests/ops/test_group_gemm.py) | group_gemm 用法范例：`reference` 是一组 `torch.matmul` 的列表，容差 `rtol=1e-3, atol=1e-8`。 |

> 本讲不重复 u5-l1 已讲透的分块几何、swizzle、K 循环、精度链；只聚焦「调度方式」从「一块一瓦片」到「持久化 grid-stride」的差异，以及由此引入的 CGA、`replace_hints`、按形状选 tile、批量 GEMM。autotuning 框架的系统讲解（候选生成、`exhaustive_search`、tune cache、`TILEGYM_DISABLE_AUTOTUNE` 开关）属于 **u5-l3**，本讲只用到结论。

## 4. 核心概念与源码讲解

### 4.1 持久化 grid-stride 调度

#### 4.1.1 概念说明

非持久化 `_matmul_kernel`（u5-l1）的调度是「**一块一瓦片**」：输出瓦片总数 `num_tiles = num_bid_m * num_bid_n`，主机侧 grid 就开这么大，硬件调度器为每个输出瓦片派发一个 CTA。当矩阵很大（比如 `M=N=8192, TILE=128`，`num_tiles = 64*64 = 4096`）而 GPU 只有上百个 SM 时，这意味着硬件要派发、回收数千个 CTA，且 grid 大小完全随问题规模线性增长。

**持久化调度**反过来：grid 大小**与问题规模脱钩**，只与硬件资源（SM 数、occupancy、CGA）相关，通常远小于 `num_tiles`。每个 CTA 启动后在内核内部用一个 grid-stride 循环**自己领取多个输出瓦片**：

\[
\text{for } \text{tile\_id} \in [\text{start\_bid},\ \text{num\_tiles},\ \text{num\_programs})
\]

其中 `start_bid = ct.bid(0)`（本 CTA 的起始瓦片号），`num_programs = ct.num_blocks(0)`（本次启动的总 CTA 数，即步长）。于是：

- CTA 0 处理 `0, P, 2P, 3P, ...`
- CTA 1 处理 `1, P+1, 2P+1, ...`
- ……

相邻 CTA 错开 `P=num_programs` 步，全体合起来正好不重叠地覆盖 `[0, num_tiles)`。这就是「瓦片数多于 SM（多于启动的 CTA）时，复用 CTA 把多出来的瓦片也算了」。

> 为什么叫「静态」持久化？因为每个 CTA 要处理哪些瓦片，在编译期/启动期就由「起始号 + 固定步长」完全确定，不需要运行期用原子操作去抢任务。与之相对的是「动态」持久化（用 `ct.atomic_add` 领号），本讲不涉及。

#### 4.1.2 核心流程

```
主机侧：
  num_tiles = ceil(M/TILE_M) * ceil(N/TILE_N)
  grid = min(NUM_SMS // num_ctas, num_tiles) * occupancy   # 与问题规模脱钩，受 SM 数封顶
  启动 grid 个 CTA

内核内（每个 CTA）：
  start_bid = ct.bid(0)
  num_programs = ct.num_blocks(0)          # = grid，即步长
  for tile_id in range(start_bid, num_tiles, num_programs):   # grid-stride 循环
      (bid_m, bid_n) = _compute_bid(tile_id, ...)            # 超组重排得瓦片坐标
      accumulator = 0
      for k_tile in range(k_tiles):                          # 与 u5-l1 完全相同的 K 循环
          a = load(A, ...); b = load(B, ...)
          accumulator = mma(a, b, acc=accumulator)
      store(C, index=(bid_m, bid_n), tile=accumulator)
```

注意一个关键点：**grid-stride 循环每跑一轮，都要重新初始化 accumulator 并重跑一遍 K 循环**——因为每个 `tile_id` 是一个独立的输出瓦片，它的部分和不能与上一个瓦片混在一起。换句话说，持久化复用的是「CTA 这个执行体」（以及它占住的寄存器/共享内存配额），而不是计算结果。

#### 4.1.3 源码精读

先看持久化内核的签名。与非持久化 `_matmul_kernel`（[matmul.py:L144-L152](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/ops/cutile/matmul.py#L144-L152)）相比，它的参数多出一大串 `Constant`：M/N/K、转置标志、`GROUP_SIZE_M`、`LOAD_LATENCY`——这些都是编译期常量，会被烤进特化内核：

[src/tilegym/ops/cutile/matmul.py:L218-L233](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/ops/cutile/matmul.py#L218-L233) —— `_static_persistent_matmul_kernel` 签名。注意 M/N/K 这里标注为普通 `int`（运行期传入），而 `TILE_SIZE_*`、`TRANSPOSE_A/B`、`GROUP_SIZE_M`、`LOAD_LATENCY` 标注为 `ct.Constant[...]`（编译期常量）。对比非持久化内核只接收三个 `ConstInt` 瓦片尺寸、M/N 直接 `A.shape[0]` 在内核内取——持久化内核要把 M/N/K 用于 `ct.cdiv` 计算 `num_tiles`，显式传入更便于 tracer 追踪。

持久化调度的核心三行：

[src/tilegym/ops/cutile/matmul.py:L235-L249](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/ops/cutile/matmul.py#L235-L249) —— `start_bid = ct.bid(0)`（本 CTA 起始瓦片号）；`num_tiles = num_bid_m * num_bid_n`（输出瓦片总数，由 `ct.cdiv` 算出）；`num_programs = ct.num_blocks(0)`（本次启动的总 CTA 数 = grid = 步长）；`for tile_id in range(start_bid, num_tiles, num_programs)` 即 grid-stride 循环；循环内调 `_compute_bid(tile_id, ...)` 把一维瓦片号经超组重排映射成二维 `(bid_m, bid_n)`。

`_compute_bid` 与 u5-l1 讲过的 `_swizzle_2d` 是同一个超组公式，区别只在于它接收 `tile_id` 作参数、并用 `ct.minimum` 替代 Python `min`（内核内对 IR 值取 min 必须用 `ct.minimum`）：

[src/tilegym/ops/cutile/matmul.py:L38-L44](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/ops/cutile/matmul.py#L38-L44) —— `_compute_bid`。super-grouping 的含义与 u5-l1 的 `_swizzle_2d` 完全一致（`GROUP_SIZE_M` 行为一组），只是被放进 grid-stride 循环里对每个 `tile_id` 调用一次。

循环体里的 K 循环与 u5-l1 同构，此处不重复；累加器在每个 `tile_id` 开头重新 `ct.full(..., 0.0, dtype=ct.float32)` 归零：

[src/tilegym/ops/cutile/matmul.py:L251-L252](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/ops/cutile/matmul.py#L251-L252) —— 每个 `tile_id` 都重新初始化 fp32 累加器，印证「复用 CTA、不复用结果」。

#### 4.1.4 代码实践

1. **实践目标**：亲手算一遍 grid-stride 循环如何「用少量 CTA 盖住大量瓦片」，建立持久化的直觉。
2. **操作步骤**：设 `M=N=2048, TILE_SIZE_M=TILE_SIZE_N=128`，于是 `num_bid_m=num_bid_n=16`，`num_tiles=256`。再设本次启动 `num_programs = grid = 64`（即只启动 64 个 CTA）。用纸笔列出 CTA `start_bid=0,1,2,3` 各自处理的 `tile_id` 序列（前 5 项即可）。
3. **需要观察的现象**：CTA 0 处理 `0, 64, 128, 192, 256(越界停)`，即实际处理 `0, 64, 128, 192` 共 4 个瓦片；CTA 1 处理 `1, 65, 129, 193`；……；CTA 63 处理 `63, 127, 191, 255`。64 个 CTA × 每个 4 瓦片 = 256 = `num_tiles`，正好不重不漏。
4. **预期结果**：每个 CTA 处理 `ceil(256/64)=4` 个输出瓦片；全体合起来覆盖 `[0,256)`。这就是「瓦片数（256）多于启动的 CTA 数（64）时，靠 grid-stride 循环复用 CTA」。
5. **待本地验证**：`num_programs` 的真实值由主机侧 grid 公式（4.2 详述）`min(NUM_SMS // num_ctas, num_tiles) * occupancy` 决定，本练习假设 64 仅为手算方便；在真实 GPU 上把 `tilegym.ops.matmul(a, b, static_persistent=True)` 的 `a/b` 设成 `2048×2048`、打印 `torch.cuda.get_device_properties("cuda").multi_processor_count` 即可估算实际 grid。

#### 4.1.5 小练习与答案

**练习 1**：若把 `num_programs` 调成等于 `num_tiles`（即 grid = 输出瓦片数），持久化内核会退化成什么？

**参考答案**：此时 `range(start_bid, num_tiles, num_programs)` 对每个 CTA 只剩 `start_bid` 一个值（因为 `start_bid + num_programs = start_bid + num_tiles ≥ num_tiles` 越界），即每个 CTA 只处理一个瓦片——退化成 u5-l1 的「一块一瓦片」非持久化调度。这印证了持久化与非持久化是同一谱系的两个端点，区别只在「grid 是否小于瓦片数」。

**练习 2**：为什么 grid-stride 循环每轮都要重新 `accumulator = ct.full(..., 0.0, ...)`，而不是把上一个瓦片的累加器接着用？

**参考答案**：因为每个 `tile_id` 对应的是输出矩阵里**不同位置**的瓦片，它们的数学结果是彼此独立的（不同的 `C[bid_m, bid_n]`）。上一个瓦片的累加器算的是 `C[旧位置]`，新一轮要算 `C[新位置]`，必须归零重算。持久化复用的是「CTA 这个执行体」及其占住的硬件资源，不是「部分和」。

---

### 4.2 num_ctas / CGA 与按形状选 tile

#### 4.2.1 概念说明

光有持久化还不够。在 Hopper（sm90）及以后的架构上，硬件支持**线程块簇（Thread Block Cluster）**，cuTile 里叫 **CGA（Cooperative Group Array）**：把 `num_ctas` 个 CTA 绑成一个协作组，它们被硬件保证调度在相邻的 SM 上，彼此可以：

- 通过**分布式共享内存（distributed shared memory）**访问同簇其他 CTA 的共享内存；
- 使用 **TMA 多播（multicast）**，让一次张量内存加载同时送到簇内多个 CTA，省带宽。

在 GEMM 里，这意味着一个更大的逻辑输出瓦片可以由 `num_ctas` 个 CTA 协作计算，A/B 瓦片只需从显存搬一次就能喂给整个簇。`num_ctas` 就是「一个 CGA 里含几个 CTA」，`num_ctas=1` 即退化为无簇（pre-SM90 架构只能取 1）。

关键约束来自 [matmul.py:L57-L58](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/ops/cutile/matmul.py#L57-L58) 的注释——**pre-SM90 不支持 CGA，`num_ctas` 只能是 1**；sm90+ 才允许 `num_ctas ≥ 2`。

**形状适配的隐患（stranded SM）**：grid 公式里有个 `min(NUM_SMS // num_ctas, num_tiles)`。它的本意是「问题太小时别启动超过瓦片数的 CTA」（4.2.2 详解），但**反过来**也是个陷阱——当 tile 选得太大、而 GEMM 又偏小或偏窄长（rectangular）时，`num_tiles` 只有 16–32 个，于是 `min` 被 `num_tiles` 这一项卡死，持久化 grid 也就只有 16–32 个 tile-job，整张卡上大部分 SM 全程闲置（被 strand）。这就是 autotune 候选表里必须同时准备「大 tile」和「小 tile」的原因：小 tile 把每个维度的瓦片数翻倍、`num_tiles` 放大约 4 倍，让更多 SM 领到活。本轮 diff 正是为此在 sm100+ 候选表末尾补了一条 128×128×64 的小 tile（见 4.2.3）。

#### 4.2.2 核心流程

`num_ctas` 怎样影响 grid？持久化内核要「把 SM 填满」：

\[
\text{grid} = \min\!\left(\left\lfloor \frac{\text{NUM\_SMS}}{\text{num\_ctas}} \right\rfloor,\ \text{num\_tiles}\right) \times \text{occupancy}
\]

逐项解释：

- `NUM_SMS // num_ctas`：总 SM 数除以簇大小，得到「整块 GPU 上能同时容纳多少个 CGA」。`num_ctas=2` 时，每两个 SM 才能凑成一个簇，所以 CGA 数减半。
- `min(..., num_tiles)`：问题太小时，瓦片数可能少于 CGA 槽位，此时不必启动超过瓦片数的 CTA（启动了也没活干）。**但当瓦片数本身被大 tile 压到很小时，这一项也会反过来把 grid 卡死**——这就是 4.2.1 说的 stranded SM。
- `* occupancy`：每个 SM 可以并发运行 `occupancy` 个 CTA（occupancy 是「每 SM 的 CTA 数」提示）。

把这三项乘起来，就是本次启动的总 CTA 数 `num_programs`，也是 4.1 里 grid-stride 循环的步长。可以看出：grid 同时受 `num_ctas`（簇大小）、`num_tiles`（tile 大小 × GEMM 形状）和 `occupancy` 三者约束——`num_ctas` 和 tile 大小都是 autotune 候选，正是为了让 autotune 替你在这三维空间里挑出「既不浪费 SM、又能享受簇协作」的组合。

#### 4.2.3 源码精读

候选配置生成器按架构区分 `num_ctas`：

[src/tilegym/ops/cutile/matmul.py:L53-L71](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/ops/cutile/matmul.py#L53-L71) —— `_matmul_autotune_configs`。sm120/sm121 与 pre-SM90 都只产出 `num_ctas=1`（注释 [L57-L58](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/ops/cutile/matmul.py#L57-L58) 明说「Pre-SM90: num_ctas=1 (CGA unsupported)」）；只有 sm100+（Blackwell）分支才会产出 `num_ctas=2` 甚至 `num_ctas=4`（[L69-L71](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/ops/cutile/matmul.py#L69-L71)）。这正是「CGA 只在 sm90+ 有效」在搜索空间层面的体现。

持久化内核的候选同样遵循这一架构分支：

[src/tilegym/ops/cutile/matmul.py:L83-L141](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/ops/cutile/matmul.py#L83-L141) —— `_static_persistent_matmul_autotune_configs`。注意每条配置都带 `LOAD_LATENCY` 字段（[L80-L82](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/ops/cutile/matmul.py#L80-L82) 的注释说明它是 `ct.load` 的代价提示，1..10 为显式值、`-1` 表示「由编译器推断」），且 sm100+ 分支同样给出 `num_ctas=2`/`num_ctas=4`（[L126](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/ops/cutile/matmul.py#L126)/[L129](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/ops/cutile/matmul.py#L129)/[L135](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/ops/cutile/matmul.py#L135)）。

**新增的小 tile 候选（sm100+，本轮重点）**：本轮在 sm100+ 分支末尾追加了一条 128×128×64 的小 tile 候选，正是为 4.2.1 的「stranded SM」开的药方——

[src/tilegym/ops/cutile/matmul.py:L137-L141](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/ops/cutile/matmul.py#L137-L141) —— 注释直白地点明意图："Small-tile candidate for small/rectangular GEMMs, where the entries above cap the persistent grid at 16-32 tile-jobs and strand most SMs"。这条候选 `TILE_SIZE_M=128, TILE_SIZE_N=128, TILE_SIZE_K=64, num_ctas=1, occupancy=1`，相比上面几条 256×256 的大 tile（`num_ctas=2/4`），它把瓦片切得更小、簇也收回到 1，目的是在小/矩形 GEMM 上把 `num_tiles` 撑大、让持久化 grid 不被 `min(NUM_SMS // num_ctas, num_tiles)` 的第二项卡死。注意它正上方 [L134-L136](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/ops/cutile/matmul.py#L134-L136) 是一条 256×256×128、`num_ctas=2` 的大 tile，两者形成「大/小 tile」对照：autotune 会把两类候选都跑一遍，由实测耗时替你选出哪种 tile 更适合当前 GEMM 形状。

grid 公式在 autotune 的 `grid_fn` 里：

[src/tilegym/ops/cutile/matmul.py:L364-L368](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/ops/cutile/matmul.py#L364-L368) —— 持久化 grid_fn：第一维 = `min(NUM_SMS // cfg.num_ctas, ceil(M/TM)*ceil(N/TN)) * cfg.occupancy`，第二、三维为 1。`NUM_SMS` 在 [L357](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/ops/cutile/matmul.py#L357) 由 `torch.cuda.get_device_properties("cuda").multi_processor_count` 取得。这与非持久化 grid_fn（[L336](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/ops/cutile/matmul.py#L336) 的 `ceil(M/TM)*ceil(N/TN)`）形成鲜明对比：**持久化 grid 与问题规模脱钩、受 SM 数封顶；非持久化 grid 随问题规模线性增长。** 注意 `ceil(M/TM)*ceil(N/TN)` 正是 `num_tiles`——当 tile 取 256×256、GEMM 又小时，这个 `num_tiles` 会小到把整条 `min` 卡死，于是才需要 4.2.1 的小 tile 候选把它撑大。

启动时用同一个公式算出最终 grid（保证调优测量与生产启动一致）：

[src/tilegym/ops/cutile/matmul.py:L393-L400](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/ops/cutile/matmul.py#L393-L400) —— 生产启动的 grid 与 [L364-L368](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/ops/cutile/matmul.py#L364-L368) 的 `grid_fn` 用的是同一表达式，只是把 `cfg` 换成 `best_cfg`。

> 顺带区分两类「编译期常量」：`num_ctas` 与 `occupancy` 是**编译器 hint**（通过 `@ct.kernel` 装饰器或 `replace_hints` 传入，控制 SM 调度与簇聚合）；而 `TILE_SIZE_*`、`GROUP_SIZE_M`、`LOAD_LATENCY`、`TRANSPOSE_A/B` 是**内核参数**（以 `ct.Constant[...]` 形式成为内核签名的一部分，生成不同的特化内核）。两者都「编译期固定」，但注入路径不同——这点在 4.3 会再次强调。

#### 4.2.4 代码实践

1. **实践目标**：看清 `num_ctas` 如何改变 grid，体会 CGA 对并行度的折算。
2. **操作步骤**：设 `NUM_SMS = 132`（A100/H100 量级），`num_tiles = 4096`（远大于 SM 数，取 `min` 时不起作用），`occupancy = 1`。分别对 `num_ctas = 1, 2, 4` 手算持久化 grid 第一维，以及对应每个 CTA 平均要处理几个瓦片。
3. **需要观察的现象**：
   - `num_ctas=1`：grid = `132 // 1 * 1 = 132`，每 CTA 平均 `4096/132 ≈ 31` 个瓦片。
   - `num_ctas=2`：grid = `132 // 2 * 1 = 66`（66 个 CGA，每簇 2 个 CTA），每 CTA 平均 `4096/66 ≈ 62` 个瓦片。
   - `num_ctas=4`：grid = `132 // 4 * 1 = 33`，每 CTA 平均 `4096/33 ≈ 124` 个瓦片。
4. **预期结果**：`num_ctas` 越大，启动的 CTA 越少、每 CTA 承担的瓦片越多，但每个簇内有更多 CTA 可协作（共享 A/B 瓦片、TMA 多播）。这是「并行度」与「簇内协作收益」之间的权衡，正是 autotune 要替你搜的。
5. **待本地验证**：pre-SM90 GPU 上 `num_ctas` 恒为 1，上述 `num_ctas=2/4` 的场景需在 sm90+（H100/Blackwell）上才可能出现；能否跑通取决于本地硬件。

#### 4.2.5 小练习与答案

**练习 1**：为什么 pre-SM90 架构的候选配置里 `num_ctas` 必须是 1？

**参考答案**：CGA（线程块簇）是 Hopper（sm90）起才有的硬件特性，pre-SM90 GPU 没有簇的概念，无法把多个 CTA 绑成协作组、也没有分布式共享内存与 TMA 多播。所以 [matmul.py:L57-L65](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/ops/cutile/matmul.py#L57-L65) 的 pre-SM90 分支只产出 `num_ctas=1`，等价于「无簇」。

**练习 2**：grid 公式里的 `min(NUM_SMS // num_ctas, num_tiles)`，`min` 的第二项 `num_tiles` 在什么时候起作用？

**参考答案**：当问题规模很小、输出瓦片数 `num_tiles` 少于「SM 能容纳的 CGA 数」时，`min` 取 `num_tiles`，避免启动超过瓦片数的 CTA（多余的 CTA 在 grid-stride 循环里第一轮就越界、啥也不算，纯属浪费）。大矩阵时 `num_tiles` 远大于 SM 数，`min` 取第一项，问题规模不再影响 grid。但要注意：若 tile 选得太大把 `num_tiles` 压到 16–32，这个 `min` 会把 grid 一并压到 16–32、闲置多数 SM——这正是 4.2.1 说的 stranded SM，需靠小 tile 候选缓解。

**练习 3**：sm100+ 候选表里为什么要在 256×256 的大 tile 之外，再补一条 128×128×64 的小 tile（[matmul.py:L137-L141](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/ops/cutile/matmul.py#L137-L141)）？

**参考答案**：大 tile（256×256）在「大而方」的 GEMM 上单瓦片算得多、簇内协作（`num_ctas=2/4`）收益高；但在小矩阵或窄长矩形（如 `M=128, N=8192`）上，大 tile 让 `num_tiles` 仅 16–32，持久化 grid 被 `min(NUM_SMS // num_ctas, num_tiles)` 的第二项卡死，绝大多数 SM 闲置。补一条 128×128 小 tile 把 `num_tiles` 放大约 4 倍，grid 不再被卡死、更多 SM 有活；它取 `num_ctas=1` 是因为小 GEMM 上凑簇反而 overhead 不划算。两类候选都进搜索空间，由 autotune 按当前形状实测选优。

---

### 4.3 replace_hints：把最优配置「烤」进内核对象

#### 4.3.1 概念说明

autotune（详见 u5-l3）会遍历候选配置、实测每个配置的耗时、选出最快的那个。选出之后，怎么把「最优的 `num_ctas` 与 `occupancy`」真正应用到内核上？答案是 `kernel.replace_hints(num_ctas=..., occupancy=...)`。

关键事实（来自 cuTile autotune API）：

- **`replace_hints` 只接受 `occupancy` 和 `num_ctas` 两个参数**——它们是仅有的两个可通过 autotune 控制的编译器 hint。
- 它返回一个**全新的内核对象**，hint 被「烤」进其中，并带有**独立的 JIT 编译缓存**。
- 因此绝不能在每次启动的热路径上调用 `replace_hints`——那会每次触发重新编译，性能下降百倍量级。**正确做法**：调优结束后调用**一次** `replace_hints`，把返回的新内核对象和配置**一起**存进模块级缓存字典，之后每次启动直接复用缓存里的内核对象。

注意区分：`TILE_SIZE_*`、`GROUP_SIZE_M`、`LOAD_LATENCY`、`TRANSPOSE_A/B` **不是** hint，而是**内核参数**（`ct.Constant[...]`）。它们的「最优值」是通过 `exhaustive_search` 的 `args_fn` 在每次试跑时以不同参数特化内核得到的，最终最优值作为普通参数传给 `ct.launch`，**不经过** `replace_hints`。

#### 4.3.2 核心流程（tune-once / cache / launch）

```
cache_key = (M, N, K, trans_a, trans_b, dtype, device)        # 形状/类型级缓存键
if cache_key not in _tune_cache:
    result = exhaustive_search(
        configs,            # 候选 SimpleNamespace 列表
        stream,
        grid_fn,            # cfg -> grid（持久化 grid 公式）
        kernel,             # @ct.kernel 内核对象
        args_fn,            # cfg -> 内核参数元组（含 TILE_SIZE_*/LOAD_LATENCY 等 Constant）
        hints_fn,           # cfg -> {"num_ctas":..., "occupancy":...}（仅这两个 hint）
    )
    best_cfg = result.best.config
    tuned_kernel = kernel.replace_hints(num_ctas=best_cfg.num_ctas, occupancy=best_cfg.occupancy)  # 只调一次
    _tune_cache[cache_key] = (best_cfg, tuned_kernel)          # 配置 + 新内核对象 一起缓存

best_cfg, tuned_kernel = _tune_cache[cache_key]                # 热路径：一次 dict 查找
grid = grid_fn(best_cfg)
ct.launch(stream, grid, tuned_kernel, args_fn(best_cfg))       # 零额外开销
```

热路径上只有一次字典查找 + 一次 `ct.launch`，与「写死配置的内核」完全等价、零额外开销。

#### 4.3.3 源码精读

持久化 matmul 的 tune-once/cache/launch 三段式：

[src/tilegym/ops/cutile/matmul.py:L356-L391](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/ops/cutile/matmul.py#L356-L391) —— `_cutile_autotune_static_persistent_matmul`。缓存键 `(M, N, K, trans_a, trans_b, a.dtype, str(a.device))`（[L358](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/ops/cutile/matmul.py#L358)）。`exhaustive_search` 的三个回调：`grid_fn`（[L364-L368](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/ops/cutile/matmul.py#L364-L368)，持久化 grid）、`args_fn`（[L370-L384](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/ops/cutile/matmul.py#L370-L384)，把 `TILE_SIZE_*`、`TRANSPOSE_A/B`、`GROUP_SIZE_M`、`LOAD_LATENCY` 作为内核参数元组返回）、`hints_fn`（[L385](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/ops/cutile/matmul.py#L385)，**只**返回 `num_ctas` 与 `occupancy`）。

`replace_hints` 只在「未命中缓存」分支里调用**一次**，且把返回的新内核与配置**一起**存入缓存：

[src/tilegym/ops/cutile/matmul.py:L387-L391](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/ops/cutile/matmul.py#L387-L391) —— `best_cfg = result.best.config`；`_static_persistent_matmul_tune_cache[cache_key] = (best_cfg, _static_persistent_matmul_kernel.replace_hints(num_ctas=best_cfg.num_ctas, occupancy=best_cfg.occupancy))`。注意 `replace_hints` 只传了 `num_ctas` 和 `occupancy`，`TILE_SIZE_*`/`LOAD_LATENCY` 等并不在这里——它们是内核参数，通过 [L393-L417](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/ops/cutile/matmul.py#L393-L417) 的 `ct.launch` 的 args 元组逐次传入。模块级缓存字典 `_static_persistent_matmul_tune_cache` 定义在 [L17](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/ops/cutile/matmul.py#L17)。

对照非持久化路径，`replace_hints` 的用法完全一致（只是候选空间更小）：

[src/tilegym/ops/cutile/matmul.py:L341-L345](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/ops/cutile/matmul.py#L341-L345) —— `_cutile_autotune_matmul` 里 `_matmul_kernel.replace_hints(num_ctas=best_cfg.num_ctas, occupancy=best_cfg.occupancy)`，同样缓存 `(best_cfg, tuned_kernel)` 二元组。

这两个 autotune 函数由入口 `matmul` 按 `static_persistent` 分流：

[src/tilegym/ops/cutile/matmul.py:L421-L455](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/ops/cutile/matmul.py#L421-L455) —— `@register_impl("matmul", backend="cutile")` 入口。`static_persistent=True` 走 `_cutile_autotune_static_persistent_matmul`（支持 `trans_a/trans_b`），否则走 `_cutile_autotune_matmul`（并 `assert trans_a==False / trans_b==False`，[L452-L453](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/ops/cutile/matmul.py#L452-L453)）——即**转置矩阵乘只有持久化路径支持**，这是持久化内核签名里带 `TRANSPOSE_A/B` 的原因。

#### 4.3.4 代码实践

1. **实践目标**：理解「缓存 tuned_kernel 对象」而非「每次 replace_hints」的必要性。
2. **操作步骤（源码阅读型）**：对照 [matmul.py:L387-L391](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/ops/cutile/matmul.py#L387-L391)（冷路径：`replace_hints`）与 [matmul.py:L392-L417](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/ops/cutile/matmul.py#L392-L417)（热路径：dict 查找 + `ct.launch`）。注意 `replace_hints` 出现在 `if cache_key not in ...:` 分支内，热路径（`best_cfg, tuned_kernel = _static_persistent_matmul_tune_cache[cache_key]`）完全不碰 `replace_hints`。设想：若把 `replace_hints` 那一行移到缓存查找之后、对每次调用都执行，会发生什么？
3. **需要观察的现象**：`replace_hints` 每次返回一个新内核对象、各自独立 JIT 编译；若每次启动都调它，每次都要重新编译持久化内核（编译耗时秒级），性能崩溃。
4. **预期结果**：你会得出结论——必须像源码这样，`replace_hints` 只在首次调优时调一次，并把 `(best_cfg, tuned_kernel)` 一起缓存；热路径只做 dict 查找 + `ct.launch`。
5. **待本地验证**：可设 `TILEGYM_DISABLE_AUTOTUNE=1`（见 [autotune.py:L10-L33](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/autotune.py#L10-L33)）对比开关 autotune 的首次启动耗时；具体耗时数据待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：`TILE_SIZE_M` 是最优配置的一部分，为什么它不通过 `replace_hints` 应用，而是作为 `ct.launch` 的参数传入？

**参考答案**：因为 `replace_hints` 只接受 `occupancy` 和 `num_ctas` 两个编译器 hint。`TILE_SIZE_M` 是**内核参数**（标注为 `ct.Constant[int]`），它通过改变内核的特化签名生成不同的特化内核——autotune 时由 `args_fn` 把不同 `TILE_SIZE_M` 喂给试跑、选出最优值，生产启动时由 `ct.launch` 的 args 元组传入。两条注入路径不同，不能混用。

**练习 2**：缓存键为什么是 `(M, N, K, trans_a, trans_b, dtype, device)` 而不是仅 `(M, N, K)`？

**参考答案**：因为不同 `trans_a/trans_b` 会走内核里不同的 load 分支（[matmul.py:L261-L312](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/ops/cutile/matmul.py#L261-L312)），不同 dtype 决定是否走 tf32（[L315](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/ops/cutile/matmul.py#L315)），不同 device 对应不同 GPU（SM 数、架构、最优配置都不同）。这些组合各自有不同的最优 `num_ctas/occupancy/TILE_*`，必须分别调优、分别缓存，所以缓存键要覆盖全部影响因素。

---

### 4.4 group_gemm：一次启动算完一组矩阵乘

#### 4.4.1 概念说明

MoE（混合专家）等场景里，常有一组**形状各异**的小矩阵乘要算：\(C_i = A_i \times B_i,\ i=0,\dots,G-1\)，每个 \(A_i\) 的 \(M_i,K_i\)、每个 \(B_i\) 的 \(N_i\) 都可能不同。若对每个乘法单独启动一个内核，启动开销与 SM 空转会吃掉收益。**group GEMM**（批量/分组矩阵乘）的做法是：把这一组问题**拼成一条瓦片流**，用**一次内核启动**、一套持久化调度全部算完。

`_group_gemm_kernel` 接收的是矩阵**列表** `As/Bs/Cs`，内核内顺序遍历每个 group，动态读取每个 \(A_i/B_i\) 的形状算出它的瓦片数，并用一个累计偏移 `last_problem_end` 把多个问题的瓦片号缝合成一条连续区间——每个 CTA 用 grid-stride 循环在这条区间上跨步领取瓦片，领到的瓦片号落在哪个 group 的区间里，就算那个 group 的那块。

#### 4.4.2 核心流程

```
主机侧：
  grid = NUM_SMS // num_ctas * occupancy          # 纯持久化，与问题规模无关
  启动 grid 个 CTA，把 As/Bs/Cs 三个列表 + NUM_SM 传进内核

内核内（每个 CTA）：
  tile_idx = ct.bid(0)                  # 本 CTA 的起始全局瓦片号
  last_problem_end = 0                  # 已遍历 group 的累计瓦片数边界
  for g in range(group_size):
      动态算 group g 的 num_tiles（用 A_g/B_g 的真实形状）
      while tile_idx 落在 [last_problem_end, last_problem_end + num_tiles):
          算出本瓦片在 group g 内的 (tile_m_idx, tile_n_idx)
          跑 K 循环累加 → store 到 C_g
          tile_idx += NUM_SM            # grid-stride 跨步
      last_problem_end += num_tiles     # 推进到下一个 group 的边界
```

要点：

- `tile_idx` 是**跨所有 group 的全局瓦片号**，由 `ct.bid(0)` 起步、每次 `+= NUM_SM` 跨步。
- `last_problem_end` 记录「到当前 group 为止累计的瓦片数」，用来判断当前 `tile_idx` 属于哪个 group；while 循环一旦离开当前 group 的区间就退出 for 推进到下一个 group。
- group 的形状是**运行期动态**的：每个 group 的 `num_m_tiles/num_n_tiles/num_k_tiles` 用 `ct.num_tiles(Ai, ...)` 按该 group 真实形状算（[group_gemm.py:L61-L66](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/ops/cutile/group_gemm.py#L61-L66)），所以各 group 形状可以不同。

#### 4.4.3 源码精读

内核签名接收三个列表与 `NUM_SM`（作为 `ConstInt` 内核参数）：

[src/tilegym/ops/cutile/group_gemm.py:L38-L52](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/ops/cutile/group_gemm.py#L38-L52) —— `_group_gemm_kernel` 签名。`As/Bs/Cs` 是矩阵列表，`NUM_SM: ConstInt` 是 grid-stride 的步长（编译期常量）。注意它与 4.1 持久化 matmul 的细微差别：matmul 用 `num_programs = ct.num_blocks(0)`（读运行期 grid），group_gemm 把 `NUM_SM` 作为 `ConstInt` 参数显式传入（[L46](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/ops/cutile/group_gemm.py#L46)）——两者数值相同（都等于启动的 grid），只是来源不同。`tile_idx = ct.bid(0)`、`last_problem_end = 0`、`group_size = len(As)` 三行初始化见 [L49-L51](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/ops/cutile/group_gemm.py#L49-L51)。

逐 group 动态算瓦片数：

[src/tilegym/ops/cutile/group_gemm.py:L54-L68](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/ops/cutile/group_gemm.py#L54-L68) —— for 循环遍历每个 group，取出 `Ai/Bi/Ci`；`num_m_tiles = ct.num_tiles(Ai, 0, (TILE_M, TILE_K))`、`num_k_tiles = ct.num_tiles(Ai, 1, (TILE_M, TILE_K))`；`TRANSPOSE_B` 分支决定 N 维瓦片从 `Bi` 的哪一轴取（[L63-L66](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/ops/cutile/group_gemm.py#L63-L66)）；`num_tiles = num_m_tiles * num_n_tiles` 是该 group 的输出瓦片数。注意 `ct.num_tiles` 这里用**位置参数** `(Ai, 0, shape)`，与 matmul.py 用的关键字 `axis=1` 写法等价。

持久化 while 循环 + grid-stride 跨步：

[src/tilegym/ops/cutile/group_gemm.py:L71-L119](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/ops/cutile/group_gemm.py#L71-L119) —— while 条件 `tile_idx >= last_problem_end and tile_idx < last_problem_end + num_tiles` 判断当前全局瓦片号是否落在本 group 区间内；`tile_idx_in_gemm = tile_idx - last_problem_end` 换算成「group 内局部瓦片号」；`tile_m_idx = tile_idx_in_gemm // num_n_tiles`、`tile_n_idx = tile_idx_in_gemm % num_n_tiles`（朴素行主序，group_gemm 没用 super-grouping）。K 循环（[L80-L109](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/ops/cutile/group_gemm.py#L80-L109)）与 u5-l1 同构，含 `TRANSPOSE_B` 分支。store 到 `Ci` 后（[L113](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/ops/cutile/group_gemm.py#L113)），`tile_idx += NUM_SM` 做跨步（[L116](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/ops/cutile/group_gemm.py#L116)）。for 循环末尾 `last_problem_end += num_tiles`（[L119](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/ops/cutile/group_gemm.py#L119)）推进到下一个 group。

主机侧的 tune-once/cache/launch，grid 纯持久化、不含 `num_tiles` 项：

[src/tilegym/ops/cutile/group_gemm.py:L122-L167](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/ops/cutile/group_gemm.py#L122-L167) —— `_cutile_autotune_group_gemm`。grid_fn [L132](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/ops/cutile/group_gemm.py#L132) 是 `NUM_SMS // cfg.num_ctas * cfg.occupancy`——**没有 `min(..., num_tiles)`**，因为 group_gemm 把多个问题的瓦片拼成一条长流，瓦片总数几乎总是远大于 SM 数，不需要封顶。缓存键 `(group_shapes, transpose_b, dtype, device)`（[L125-L126](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/ops/cutile/group_gemm.py#L125-L126)），其中 `group_shapes` 是所有 group 的 `(A.shape, B.shape)` 元组的元组——形状组合变了的缓存失效。`NUM_SM` 作为内核参数 `NUM_SMS // cfg.num_ctas * cfg.occupancy` 传入（[L141](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/ops/cutile/group_gemm.py#L141)/[L163](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/ops/cutile/group_gemm.py#L163)），与 grid 第一维相等。`replace_hints` 同样只调一次（[L149](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/ops/cutile/group_gemm.py#L149)）。

入口（注意默认 `static_persistent=True`）：

[src/tilegym/ops/cutile/group_gemm.py:L170-L197](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/ops/cutile/group_gemm.py#L170-L197) —— `@register_impl("group_gemm", backend="cutile")`。校验 `group_A/group_B` 非空且等长（[L178-L182](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/ops/cutile/group_gemm.py#L178-L182)）；为每对 `(A, B)` 按其真实 `(M, N)` 用 `torch.empty` 预分配输出 `C`（[L188-L193](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/ops/cutile/group_gemm.py#L188-L193)），把 `group_C` 列表传入内核；返回 `group_C`（一组结果）。用法范例见 [tests/ops/test_group_gemm.py:L17-L101](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/tests/ops/test_group_gemm.py#L17-L101)，其 `reference` 是一组 `torch.matmul` 的列表（[L17-L27](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/tests/ops/test_group_gemm.py#L17-L27)），容差 `rtol=1e-3, atol=1e-8`。

#### 4.4.4 代码实践

1. **实践目标**：用最小例子跑通 group_gemm，并理解 `last_problem_end` 如何缝合多问题。
2. **操作步骤**：写一段脚本，构造一组形状各异的矩阵（参考 [test_group_gemm.py:L36-L51](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/tests/ops/test_group_gemm.py#L36-L51) 的 `group_m=[1024,512,256,128]` 等），调用 `tilegym.ops.group_gemm(group_A, group_B)`，把返回的 `group_C` 与逐个 `a @ b` 的参考逐元素比较（容差 `rtol=1e-3, atol=1e-8`）。
   ```python
   # 示例代码（调用 TileGym，需本地具备 cutile 后端与 GPU）
   import torch, tilegym
   group_A = [torch.rand((m, k), device="cuda", dtype=torch.float16) for m, k in [(1024,1024),(512,512)]]
   group_B = [torch.rand((k, n), device="cuda", dtype=torch.float16) for k, n in [(1024,1024),(512,512)]]
   group_C = tilegym.ops.group_gemm(group_A, group_B)
   for a, b, c in zip(group_A, group_B, group_C):
       ref = (a.float() @ b.float()).half()
       print(torch.allclose(c, ref, rtol=1e-3, atol=1e-8))
   ```
3. **需要观察的现象**：一次 `group_gemm` 调用返回一组结果，每个 `C_i` 形状与对应 `(A_i, B_i)` 匹配；与逐个 `a @ b` 数值一致（在容差内）。
4. **预期结果**：两个 `C_i` 都与参考 `allclose`。再用纸笔跟踪 `last_problem_end`：设 group 0 的 `num_tiles=64`、group 1 的 `num_tiles=16`，则 group 0 占全局区间 `[0,64)`、group 1 占 `[64,80)`；某 CTA 起始 `tile_idx=70` 时，for 到 group 0 因 `70 ≥ 64` 不进 while，推进到 group 1，`tile_idx_in_gemm = 70-64 = 6`，算 group 1 的第 6 号瓦片。
5. **待本地验证**：能否跑通取决于本地是否具备 cutile 后端与 CUDA GPU；若不具备，可只做 `last_problem_end` 的纸笔跟踪（第 4 步）作为源码阅读型实践。

#### 4.4.5 小练习与答案

**练习 1**：group_gemm 的 grid_fn 为什么是 `NUM_SMS // num_ctas * occupancy`，**不带** `min(..., num_tiles)`（对比 4.2 的持久化 matmul）？

**参考答案**：因为 group_gemm 把所有 group 的瓦片拼成一条很长的全局瓦片流，总瓦片数是各 group `num_tiles` 之和，几乎总是远大于 SM 数，封顶毫无意义。而单矩阵持久化 matmul 面对的可能是「很小的矩阵」，瓦片数可能少于 SM 槽位，才需要 `min` 避免启动多余 CTA。两者调度策略一致，只是 group_gemm 天然不会遇到「瓦片太少」的情形。

**练习 2**：如果某个 group 的矩阵形状变了（比如 group 0 的 M 从 1024 变成 2048），会触发重新调优吗？

**参考答案**：会。缓存键含 `group_shapes`（[group_gemm.py:L125-L126](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/ops/cutile/group_gemm.py#L125-L126)），任一 group 的形状变化都使键改变，缓存未命中即重新跑 `exhaustive_search`。这也提醒：group_gemm 的最优配置与具体的一组形状强绑定，不能跨形状组合复用。

---

## 5. 综合实践

把本讲五块知识串起来，完成下面这个「双内核对照标注」任务，它正是本讲指定的代码实践。

**任务**：打印或手抄两段源码——非持久化 [matmul.py:L327-L353](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/ops/cutile/matmul.py#L327-L353)（`_cutile_autotune_matmul`）与持久化 [matmul.py:L356-L418](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/ops/cutile/matmul.py#L356-L418)（`_cutile_autotune_static_persistent_matmul`），用四种颜色标注它们的差异，并回答「持久化如何在输出瓦片多于 SM 时复用 CTA」与「新增 128×128 候选为何能改善小/矩形 GEMM 的 SM 占用」：

1. **grid 设置（对应 4.1 + 4.2）**：圈出两个 `grid_fn`。非持久化是 `ceil(M/TM) * ceil(N/TN)`（[L336](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/ops/cutile/matmul.py#L336)，= 输出瓦片数，随问题规模线性增长）；持久化是 `min(NUM_SMS // num_ctas, num_tiles) * occupancy`（[L364-L368](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/ops/cutile/matmul.py#L364-L368)，受 SM 数封顶、与问题规模脱钩）。在旁边写明：当 `num_tiles > NUM_SMS//num_ctas * occupancy` 时，持久化启动的 CTA 数少于瓦片数，差额由内核内 [matmul.py:L246](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/ops/cutile/matmul.py#L246) 的 grid-stride 循环补上——每个 CTA 跨步 `num_programs` 处理多个瓦片，即「复用 CTA」。
2. **hint 与参数（对应 4.3）**：下划线标出两条 `replace_hints(num_ctas=..., occupancy=...)`（[L344](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/ops/cutile/matmul.py#L344)/[L390](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/ops/cutile/matmul.py#L390)）。在旁边注明：只有 `num_ctas/occupancy` 走 `replace_hints`（编译器 hint）；`TILE_SIZE_*`、`LOAD_LATENCY`、`TRANSPOSE_A/B` 是内核参数，经 `ct.launch` 的 args 传入（持久化的 args 见 [L402-L416](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/ops/cutile/matmul.py#L402-L416)）。
3. **CGA（对应 4.2）**：在持久化候选 [matmul.py:L123-L141](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/ops/cutile/matmul.py#L123-L141) 的 `num_ctas=2/4` 处打星号，注明「仅 sm90+ 支持 CGA，pre-SM90 恒为 1（[L106-L122](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/ops/cutile/matmul.py#L106-L122)）」，并解释 `NUM_SMS // num_ctas` 是「把簇大小折算成 CGA 槽位」。
4. **小 tile 候选（对应 4.2 形状适配，本轮新增）**：在持久化候选 [matmul.py:L123-L141](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/ops/cutile/matmul.py#L123-L141) 里圈出新增的 128×128×64 小 tile（[L137-L141](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/ops/cutile/matmul.py#L137-L141)），与上面几条 256×256 大 tile（[L125-L136](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/ops/cutile/matmul.py#L125-L136)）对照。在旁边算一笔账：设 `NUM_SMS=132`、`num_ctas=2, occupancy=1`，对一个 `M=N=1024` 的方阵，大 tile 256×256 给出 `num_tiles = (1024/256)² = 16`，持久化 grid = `min(132//2, 16) = min(66, 16) = 16` 个 CGA（仅 32 个 SM 干活，约 100 个 SM 闲置）；换成 128×128 小 tile（`num_ctas=1`）给出 `num_tiles = (1024/128)² = 64`，grid = `min(132//1, 64) = min(132, 64) = 64` 个 CTA 干活——SM 占用明显提升。这就是「新增 128×128 候选为何能改善小/矩形 GEMM 的 SM 占用」：小 tile 把 `num_tiles` 放大约 4 倍，挣脱了 `min` 的 `num_tiles` 卡点。

**进阶**：完成上述标注后，再对照 [group_gemm.py:L49-L119](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/ops/cutile/group_gemm.py#L49-L119) 的 `_group_gemm_kernel`，指出它把持久化调度从「单问题」推广到「多问题」的两处改造：（a）grid-stride 步长由 `NUM_SM`（ConstInt 参数）显式传入、而非 `ct.num_blocks(0)`；（b）用 `last_problem_end` 累计偏移把多个 group 的瓦片区间缝合，使每个 CTA 的 `tile_idx` 能跨 group 流动。

> 本任务全程只读源码、无需运行 GPU；若你想运行验证，可仿照 [tests/ops/test_matmul.py:L103-L150](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/tests/ops/test_matmul.py#L103-L150) 调用 `tilegym.ops.matmul(a, b, static_persistent=True)` 与 `static_persistent=False` 各跑一次并比较结果（容差 `atol=1e-2, rtol=1e-2`），但能否跑通取决于本地是否具备 cutile 后端与对应 GPU，**结果待本地验证**。

## 6. 本讲小结

- **持久化 grid-stride**：`_static_persistent_matmul_kernel` 用 `for tile_id in range(start_bid, num_tiles, num_programs)` 让每个 CTA 跨步处理多个输出瓦片；`start_bid = ct.bid(0)`、步长 `num_programs = ct.num_blocks(0)` = 启动的 grid。当瓦片数多于启动的 CTA 数时，差额由这个循环补上——即「复用 CTA」。每个瓦片都要重新归零累加器（复用执行体、不复用结果）。
- **grid 与问题规模脱钩**：非持久化 grid = `ceil(M/TM)*ceil(N/TN)`（随规模线性增长，一块一瓦片）；持久化 grid = `min(NUM_SMS // num_ctas, num_tiles) * occupancy`（受 SM 数封顶）。这是两种调度最本质的差异。
- **num_ctas / CGA**：`num_ctas` 把多个 CTA 聚合成协作组（CGA/线程块簇），`NUM_SMS // num_ctas` 把 SM 数折算成 CGA 槽位；CGA 仅 sm90+ 支持，pre-SM90 恒为 1。`num_ctas` 与 `occupancy` 是**编译器 hint**。
- **小 tile 候选与形状适配（本轮新增）**：持久化 grid 的 `min(NUM_SMS // num_ctas, num_tiles)` 第二项，在小/矩形 GEMM 上会把 grid 卡到 16–32、闲置大部分 SM（stranded SM）。sm100+ 候选表本轮新增 128×128×64 小 tile（[matmul.py:L137-L141](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/ops/cutile/matmul.py#L137-L141)），把 `num_tiles` 放大约 4 倍，与 256×256 大 tile（`num_ctas=2/4`）共同构成「按形状选 tile」的搜索空间，由 autotune 实测裁决。
- **replace_hints**：autotune 选出最优配置后，`kernel.replace_hints(num_ctas=..., occupancy=...)` 返回带独立 JIT 缓存的新内核对象；只接受 `num_ctas/occupancy` 两个 hint，**只在首次调优时调一次**，与配置一起缓存；`TILE_SIZE_*`/`LOAD_LATENCY`/`TRANSPOSE_A/B` 是内核参数，不经 `replace_hints`、由 `ct.launch` 的 args 传入。
- **group_gemm 批量**：`_group_gemm_kernel` 把一组形状各异的矩阵乘拼成一条全局瓦片流，用 `last_problem_end` 累计偏移缝合多 group 区间；grid-stride 步长以 `NUM_SM`（ConstInt 参数）显式传入；grid 纯持久化 `NUM_SMS // num_ctas * occupancy`（不带 `min`）。转置矩阵乘在 cutile 后端**只有持久化路径**支持。

## 7. 下一步学习建议

- **u5-l3（Autotuning 机制）**：本讲的 `num_ctas`/`occupancy`/`TILE_SIZE_*`/`LOAD_LATENCY` 都来自 `_static_persistent_matmul_autotune_configs()` 与 `exhaustive_search`，小 tile 候选也是在这里被加进搜索空间的。下一讲系统讲解这套调优框架——按架构产出候选、`exhaustive_search` 的实测流程、模块级 tune cache、`TILEGYM_DISABLE_AUTOTUNE` 全局开关，以及 `LOAD_LATENCY` 这类代价提示为何每条配置都得带、候选配置为何要按 GEMM 形状覆盖大/小 tile。
- **延伸阅读 1**：对照 [bmm.py](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/ops/cutile/bmm.py) 的 `_static_persistent_bmm_kernel`（[L99](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/src/tilegym/ops/cutile/bmm.py#L99) 附近），看持久化 grid-stride（`num_programs = ct.num_blocks(0)`、`for current_bid in range(bid, total_tiles, num_programs)`）如何叠加 3D 网格与 transpose，是把本讲知识融会贯通的最好样本。
- **延伸阅读 2**：cuTile autotuning 技能 [skills/tilegym-cutile-autotuning/references/api-reference.md](https://github.com/NVIDIA/TileGym/blob/7410bd8dcc7e83bc1de9d41807056fa463998a39/skills/tilegym-cutile-autotuning/references/api-reference.md) 给出了 `replace_hints`、`exhaustive_search`、tune-once/cache/launch 模式的权威说明，可作为本讲 4.3 的官方参照。
