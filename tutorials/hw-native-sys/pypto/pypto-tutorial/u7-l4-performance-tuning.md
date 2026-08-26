# u7-l4 性能优化实践

## 1. 本讲目标

学完本讲，你应该能够：

1. 掌握两类核心优化手法：**Split-K**（把归约维 K 摊给多个核）与 **AutoTileMatmulL0 自动分块**（编译器按「放置 × K 策略」矩阵自动选 (m, n, k)）。
2. 理解影响性能的三个维度：**分块形状**（tile 多大）、**内存驻留**（中间量留在片上还是落回 DDR）、**流水化**（跨核通道深度与软件流水）。
3. 会解读**泳道图**（swimlane）与**关键路径分析**：分清静态 CPM 与观测路径，读懂 `data-wait` / `core-wait` / `front-gap` 三种 stall 归因，并按 verdict 表判断负载是依赖受限还是资源串行受限。
4. 能对同一个算子做「基线 → 优化 A → 优化 B」的对照实验，并解释每步优化改变了哪个硬件行为。

本讲与 u5-l6（Tile 后端降级链）是姊妹篇：u5-l6 讲 AutoTileMatmulL0 **在编译器内部怎么实现**，本讲站在**使用者视角**讲它**什么时候帮你、你还能自己做什么**。

## 2. 前置知识

- **makespan（总时长）**：一批任务从第一个开始到最后一个结束的墙钟时长。性能优化的目标就是缩小它。
- **输出瓦片并行度**：一个 \([M,N]\) 的 matmul，能拆出的独立输出 tile 数是 \(\lceil M/m \rceil \cdot \lceil N/n \rceil\)。并行度来自输出 tile——每个 tile 互相独立，可以一核一个。
- **L0 是稀缺资源**：Ascend910B 上 cube 累加器 L0c 只有 128 KB，操作数缓冲 L0a/L0b 同样有限。tile 太大放不下，太小又喂不饱计算单元——这就是「分块形状」维度。
- **原子加（atomic add）**：多核同时往同一块全局内存累加时，硬件保证每次加法不可分割。代价是加法顺序不固定，浮点结果在 ulp 级别不可复现。
- **静态 CPM（Critical Path Method）**：把任务图当作「每条边串行、每层内并行」的 DAG，取最长的一条「时长加权链」。它给出的是**核数无限多时的时延下界**。
- **stall（停顿）**：任务在核上「本可以开始却没开始」的时间。本讲把它拆成三类：等数据（上游生产者迟到）、等核（分到的核忙别的）、等发射（整个流水的前置开销）。

## 3. 本讲源码地图

| 文件 | 作用 |
| ---- | ---- |
| [examples/advanced/01_split_k.py](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/examples/advanced/01_split_k.py) | Split-K 手写示例：`pl.parallel` 切 K + 原子累加 |
| [examples/advanced/02_auto_tile_matmul.py](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/examples/advanced/02_auto_tile_matmul.py) | 自动分块示例：放置 × K 策略的六个内核 |
| [docs/en/api/optimizations.md](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/docs/en/api/optimizations.md) | `pl.at(..., optimizations=[...])` 接受的优化条目清单 |
| [python/pypto/language/optimizations.py](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/python/pypto/language/optimizations.py) | `pl.split` / `pl.cross_core_slot` 的定义（跨核拆分与通道深度） |
| [docs/en/user/tutorials/05-scheduling-tuning.md](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/docs/en/user/tutorials/05-scheduling-tuning.md) | 调度调优循环：四个观测点 + 环形缓冲旋钮 |
| [docs/en/user/performance/00-swimlane.md](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/docs/en/user/performance/00-swimlane.md) | 泳道图采集/解读 + 关键路径分析与 verdict 表 |
| [examples/intermediate/01_fused_linear.py](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/examples/intermediate/01_fused_linear.py) | 综合实践的基线算子（手工 64×64 分块的融合线性层） |
| [src/ir/transforms/auto_tile_matmul_l0_pass.cpp](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/transforms/auto_tile_matmul_l0_pass.cpp) | 自动分块 Pass 的实现（本讲只看入口分派，内部见 u5-l6） |

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：两种优化手法（4.1、4.2）、观测工具链（4.3）、关键路径归因（4.4）。

### 4.1 Split-K：把归约维摊给多个核

#### 4.1.1 概念说明

matmul \(C_{M \times N} = A_{M \times K} \cdot B_{K \times N}\) 的并行度天然来自输出 tile。但当 **M、N 小而 K 大**时（比如 \(64 \times 512 @ 512 \times 64\)），输出只有 \(64 \times 64\)——一两个 tile 就装下了，剩下的核全部空闲。

Split-K 的思路：既然输出 tile 不够分，就把 **K 维切开分给多个核**。每个核算一个 \([M, K/S]\times[K/S, N]\) 的部分积，再把 \(S\) 份部分积**原子累加**进同一块输出：

\[
C = \sum_{s=0}^{S-1} A_{[:,\,sK_s:(s+1)K_s]} \cdot B_{[sK_s:(s+1)K_s,\,:]}
\]

代价有两笔：输出必须先清零（否则原子加从一个未知的旧值起步）；原子加的顺序不固定，浮点结果在 ulp 级别不可复现。收益是把单核的 K-loop 长度除以 \(S\)，同时把空闲核变成生产力。

#### 4.1.2 核心流程

```text
1. zero_init 作用域：c ← full([M, N], 0.0)          # 必须先于并行循环
2. for ks in pl.parallel(0, SPLIT):                  # 每个核领一段 K
3.     k0 = ks * KS                                  # 本核 K 切片起点
4.     partial = a[:, k0:k0+KS] @ b[k0:k0+KS, :]     # [M, KS] x [KS, N]
5.     c ← assemble(c, partial, [0, 0], atomic=Add)  # 原子累加进同一输出
```

关键在于第 1 步与第 2 步之间的**依赖边**：zero_init 写 `c`，并行循环读改 `c`，所以编译器自动推出「先清零后累加」的顺序——这不是用户手工同步，而是缓冲区依赖推导（回顾 u7-l1：RAW/WAW 自动追踪）。

#### 4.1.3 源码精读

文件头部的 docstring 把动机、做法和「浮点非确定性」的注意事项都写清楚了：

- [examples/advanced/01_split_k.py:L10-L32](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/examples/advanced/01_split_k.py#L10-L32) —— 说明 Split-K 适用于「大 K、小 M/N」的形态，并列出本例引入的三个概念：`pl.parallel` 切 K、`atomic=pl.AtomicType.Add` 原子累加、内核内先清零。

形状常量决定了问题的形态：

- [examples/advanced/01_split_k.py:L38-L42](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/examples/advanced/01_split_k.py#L38-L42) —— `M = 64, N = 64, K = 512, SPLIT = 4`，即每核负责 `KS = 128` 宽的 K 切片。输出只有 64×64（一个 tile），不切 K 就只有 1 个核在干活。

内核本体只有十行：

- [examples/advanced/01_split_k.py:L54-L55](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/examples/advanced/01_split_k.py#L54-L55) —— 清零段：`pl.assemble(c, pl.full([M, N], 0.0), [0, 0])`，把输出写成全零。注意它在一个**独立命名的作用域**里，与后面的并行段分开。
- [examples/advanced/01_split_k.py:L56-L62](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/examples/advanced/01_split_k.py#L56-L62) —— 并行段：`pl.parallel(0, SPLIT)` 让 4 个核各自领一个 `ks`；每个核切出自己的 K 子矩阵，做一次 `pl.matmul`（FP32 累加），最后 `pl.assemble(..., atomic=pl.AtomicType.Add)` 原子累加。

注意第 59–60 行的切片：`a[:, k0:k0+KS]` 与 `b[k0:k0+KS, :]` 共享同一个 `k0`，这就是「同一核负责同一段 K」的对应关系写法。第 61 行 `out_dtype=pl.FP32` 沿用了 u2-l4 讲过的规则——cube matmul 的浮点结果 dtype 由累加器固定为 FP32。

main 函数用 `torch.matmul` 做金标准对照，容差 `rtol=1e-3, atol=1e-3`——比通常略宽，因为原子加的顺序不固定：

- [examples/advanced/01_split_k.py:L66-L77](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/examples/advanced/01_split_k.py#L66-L77) —— 构造输入、以 `c = torch.zeros(...)` 作为 `pl.Out` 实参传入、断言对照。

#### 4.1.4 代码实践

**实践目标**：直观感受 SPLIT 对任务图形状的影响（核的占用数），并确认数值正确性不随 SPLIT 变化。

**操作步骤**：

1. 复制 `examples/advanced/01_split_k.py` 到临时目录（不要改动源文件），依次把 `SPLIT` 改为 `1`、`2`、`4`，各运行一次：

   ```bash
   python examples_copy/01_split_k.py
   ```

2. 打开依赖导出，看任务图里并行任务的个数：

   ```python
   cfg = RunConfig(enable_dep_gen=True, save_kernels=True)
   ```

   运行后用 `python -m simpler_setup.tools.deps_viewer <work_dir>/dfx_outputs/deps.json --format html` 查看图。

**需要观察的现象**：`SPLIT=1` 时并行段只有一个任务；`SPLIT=4` 时是 4 个互相无依赖的兄弟任务（它们只共同依赖 zero_init）。`deps.json` 里的边数随 SPLIT 增加而增加，但**链深不变**。

**预期结果**：三种 SPLIT 都打印 `OK`（数值一致，容差内）；任务图中 `split_k` 族的兄弟任务数分别为 1/2/4。具体的墙钟耗时差异**待本地验证**——在模拟器平台上耗时反映的是调度形状，不是真实硬件时延。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `SPLIT` 改成 `8`，会发生什么？

**答案**：`KS = K // SPLIT = 64`，每核 K 切片变窄，8 个核并行。数值仍然正确（对照 torch）。但要注意 `K` 必须能被 `SPLIT` 整除（整数除法会截断切片，`k0 + KS` 会越界或漏算）；同时 8 份部分积对同一块 64×64 输出的原子竞争更集中，收益递减。

**练习 2**：为什么 zero_init 必须在并行循环**之前**，而不是之后？

**答案**：原子加是「读-改-写」，它从一个初始值起步。若输出未清零，累加起点是内存中的旧值，结果错误。顺序由依赖推导保证：zero_init 写 `c`，并行段读改 `c`，形成 RAW/WAW 边，调度器不会乱序。这也是 docstring 第 51–52 行说的「the zero-init is sequenced before the parallel loop because its result feeds the loop」。

**练习 3**：Split-K 和 4.2 要讲的 AutoTileMatmulL0 的 split-K 有什么本质区别？

**答案**：本例的 Split-K 是**多核间**的切分——每个核是一个独立任务，靠全局内存原子加合并；AutoTileMatmulL0 的 split-K 是**单核内**的 K-loop 切分——一个核内部的 K 分成多段循环累加到同一个 L0c 累加器，不涉及跨核原子操作。前者解决「核不够用」，后者解决「K 超出 L0a/L0b 容量」。

### 4.2 AutoTileMatmulL0：放置 × K 策略矩阵

#### 4.2.1 概念说明

当 matmul 的 \([M, N]\) 输出**超出 L0c（128 KB）**时，编译器的 AutoTileMatmulL0 Pass（默认流水线第 15 个）会自动把输出切成 \([m, n]\) 子 tile。它沿两个**正交**的轴做决策：

**轴一：放置（placement）——子 tile 的结果放哪？**

| 放置 | 触发条件 | 行为 |
| ---- | -------- | ---- |
| **DDR 直存** | 结果被 `pl.store` 到 DDR 张量 | 每个子 tile 直接 `store` 到 `out[mi:, ni:]` |
| **Mat/L1 暂存** | 结果被**另一个 matmul** 在片上消费 | 每个子 tile 经 Acc→Mat 的 `tile.assemble` 拼进一块 L1/Mat 暂存区，中间量**不落 DDR** |

**轴二：K 策略——归约维怎么走？**

| 策略 | 触发条件 | 行为 |
| ---- | -------- | ---- |
| **full-K** | 整个 K 一次放进 L0a/L0b（\(k = K\)） | M/N 网格是带循环变量偏移的流水嵌套（`BuildFullKPipelined`） |
| **split-K** | K 跨 ≥ 2 个 L0 块 | 每个 \([m, n]\) 子 tile 自带一条流水化 K-loop（`BuildSplitKGrid`） |

两个轴都由**形状与消费者**驱动，用户不写任何分块代码。此外还有一个「fits-L0c」特例：当链式中间量**装得下** L0c 时，不做 M/N 切分，整窗一次 Acc→Mat 拼装，并且能把 `pl.cast(c, bf16, mode="rint")` **折叠**进这一次 cube FIXPIPE 写回（`pto.tinsert`）——否则独立的 cast 会降级成 Vector 的 `pto.tcvt`，造成 cube→vector→cube 的往返。

#### 4.2.2 核心流程

```text
用户写: c = pl.matmul(a, b, out_dtype=FP32)      # 不写任何分块
        ↓ (Pass 15: AutoTileMatmulL0)
判断: [M, N] > L0c ?  ── 否 → 单 tile（可能折叠 cast）
                      └─ 是 → 切 [m, n] 子 tile
判断: 消费者是谁？  ── store 到 DDR → DDR 直存
                      └─ 另一 matmul → Mat/L1 暂存（不落 DDR）
判断: K ≤ k_max ?    ── 是 → full-K 流水嵌套
                      └─ 否 → 每子 tile 一条 split-K K-loop
```

决策入口在 Pass 内部按 `k == K` 分派到两个发射器之一（`BuildFullKPipelined` / `BuildSplitKGrid`），见 [src/ir/transforms/auto_tile_matmul_l0_pass.cpp:L2420-L2450](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/transforms/auto_tile_matmul_l0_pass.cpp#L2420-L2450)。这两个发射器怎么选 (m, n, k)、怎么生成谓词化 K-loop，是 u5-l6 的内容，本讲不展开。

#### 4.2.3 源码精读

示例文件的 docstring 本身就是一份决策矩阵文档：

- [examples/advanced/02_auto_tile_matmul.py:L10-L49](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/examples/advanced/02_auto_tile_matmul.py#L10-L49) —— 讲清两轴矩阵、`K = 128`（FP32 直存）/`K = 192`（BF16 暂存）如何让两个规划器都选 `k = 64`，以及 fits-L0c 时 cast-fold 只对 `mode="rint"` 生效的原因（FIXPIPE 固定按 round-half-to-even 收缩）。

DDR 直存的两种 K 策略（同一段代码，只有 K 不同）：

- [examples/advanced/02_auto_tile_matmul.py:L56-L64](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/examples/advanced/02_auto_tile_matmul.py#L56-L64) —— `ddr_split_k`：`[256,128] @ [128,256] = [256,256]`，输出超 L0c、被 store 消费 → M/N 切分 + 直存 DDR，`K=128` 走 split-K。
- [examples/advanced/02_auto_tile_matmul.py:L67-L74](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/examples/advanced/02_auto_tile_matmul.py#L67-L74) —— `ddr_full_k`：仅把 K 换成 32（`[256,32] @ [32,256]`），K 一次放进 L0 → full-K 流水嵌套。**用户代码一个字没改**，K 策略由形状驱动。

Mat/L1 暂存的链式 matmul：

- [examples/advanced/02_auto_tile_matmul.py:L77-L97](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/examples/advanced/02_auto_tile_matmul.py#L77-L97) —— `mat_split_k`：`(a @ b) @ e`，`[256,256]` 中间量超 L0c 但被第二个 matmul 在片上消费 → 拼进 L1/Mat 暂存。第 94 行 `pl.cast(c, pl.BF16, mode="rint")` 的 `mode="rint"` 是**必需**的——cast 默认模式是 ties-away，与 FIXPIPE 的 tie-even 不一致，折叠不会触发。

fits-L0c 的 cast-fold 特例：

- [examples/advanced/02_auto_tile_matmul.py:L113-L124](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/examples/advanced/02_auto_tile_matmul.py#L113-L124) —— `fits_l0c_full_k`：`[128,128]` 中间量恰好装进 L0c，不切 M/N，cast 折叠进**单次**全窗 Acc→Mat `tile.assemble`。
- [examples/advanced/02_auto_tile_matmul.py:L127-L138](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/examples/advanced/02_auto_tile_matmul.py#L127-L138) —— `fits_l0c_split_k`：同样的 `[128,128]` 中间量，但 `K=512` 超出 L0a/L0b → 生产者是 K-loop。注释明确说了：cast-fold 与 K 切分**相互独立**。

main 里的验证细节值得注意——不同内核用不同的容差策略：

- [examples/advanced/02_auto_tile_matmul.py:L141-L182](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/examples/advanced/02_auto_tile_matmul.py#L141-L182) —— BF16 链的金标准显式建模了 FIXPIPE 的收缩（第 162 行 `(a.float() @ b.float()).to(torch.bfloat16).float()`）；fits-L0c 两例改用 **Frobenius 相对误差**（第 179 行）而非逐元素 allclose，注释解释了原因：BF16 链在 K=512 时存在近似抵消的元素，逐元素 atol 会在数值正确的结果上误报。

#### 4.2.4 代码实践

**实践目标**：亲眼看到「用户代码不变、形状变了、编译器换了降级路径」。

**操作步骤**：

1. 运行示例确认全绿：

   ```bash
   python examples/advanced/02_auto_tile_matmul.py
   ```

2. 打开逐 Pass 导出，观察第 15 个 Pass（AutoTileMatmulL0）前后。`dump_passes` 接受 `PassDumpLevel` 枚举或布尔值（`True` 等于 `CONCISE`），三档级别：`NONE` / `CONCISE`（默认，最适合逐 Pass diff）/ `EXPLICIT`（全量布局解析，见 [python/pypto/ir/pass_manager.py:L95-L113](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/python/pypto/ir/pass_manager.py#L95-L113)）：

   ```python
   from pypto.ir import PassDumpLevel
   cfg = RunConfig(dump_passes=PassDumpLevel.CONCISE)
   ```

3. 在导出目录里对比 `ddr_split_k` 与 `ddr_full_k` 两个函数在 pass 15 之后的 IR：数一数 K-loop 是否存在、`tile.create` 的形状是什么。

**需要观察的现象**：`ddr_split_k` 的 IR 里每个子 tile 周围出现一条以 `ko` 为循环变量的 K-loop（或者谓词化的两段流水）；`ddr_full_k` 没有 K-loop，只有带循环变量偏移的 M/N 网格。`mat_split_k` 里能看到 `tile.assemble` 写往 Mat 空间的暂存，而不是 `tile.store` 到 DDR。

**预期结果**：示例打印 `OK`；IR 对比符合上面的描述。具体的 (m, n, k) 选择**待本地验证**——它由 Pass 内部的 roofline 穷举决定，不同后端参数下可能不同。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `mat_split_k` 要显式写 `pl.cast(c, pl.BF16, ...)`，而不是让第二个 matmul 直接吃 FP32 的 `c`？

**答案**：注释（第 87–89 行）给了三个理由：cube 在 L0C 以 FP32 累加；FIXPIPE 写回 L1 时收缩为 BF16/FP16（这是 A2/A3 上唯一的偏移型 Acc→Mat 通路）；BF16 也是 cube matmul 操作数的原生精度。显式 cast 让源码与硬件通路对齐，且 `mode="rint"` 使它能被折叠进这次写回，省掉一次 Vector 往返。

**练习 2**：如果把 `pl.cast(c, pl.BF16, mode="rint")` 改成 `pl.cast(c, pl.BF16)`（默认 mode），会发生什么？

**答案**：默认 mode 是 `"round"`（ties away），与 FIXPIPE 固定的 tie-even 不一致，折叠条件不满足 → cast 降级为独立的 Vector `pto.tcvt`，出现 cube→vector→cube 往返。在 `[128,128]` 尺寸下这个 Vector 往返还会撑爆 Vec buffer（docstring 第 40–42 行）。数值上仍正确，但性能和资源都变差。

**练习 3**：`ddr_split_k` 与 4.1 的 `matmul_split_k` 输出都是 `[256,256]`/`[64,64]` 的多核参与吗？

**答案**：不是。`ddr_split_k` 的「split-K」是**单核内**的 K-loop（`BuildSplitKGrid`），整个 `pl.at` 作用域仍是一个任务；`matmul_split_k` 的 SPLIT 是 `pl.parallel` 展开的**多任务**，每核一个任务靠原子加合并。名字相同、层次不同——前者受 L0 容量驱动，后者受核占用率驱动。

### 4.3 观测工具链：先测量，再动手

#### 4.3.1 概念说明

调优没有测量就是猜测。PyPTO 提供四个互相独立的观测点，每个是一个 `RunConfig` 开关、各写一个文件：

| 问题 | 开关 | 产物 |
| ---- | ---- | ---- |
| 任务图长什么样？ | `enable_dep_gen=True` | `deps.json` |
| 任务真的并行了吗？ | `enable_chip_swimlane=<level>` | `chip_swimlane_records.json` |
| 运行时环形缓冲快满了吗？ | `enable_scope_stats=True` | `scope_stats/scope_stats.jsonl` |
| 哪条流水线是瓶颈？ | `enable_pmu=2` | `pmu.csv` |

它们可以**叠加**在一次运行里。编译期还有一份免费的线索：构建输出里的 `report/perf_hints.log`，记录编译器自己注意到的问题（它说的是「编译器怀疑什么」，泳道说的是「实际发生了什么」）。

除了观测，DSL 层还有一组**优化提示条目**挂在 `pl.at(..., optimizations=[...])` 上，控制跨核数据通道的形状——这是「流水化」维度的用户旋钮。

#### 4.3.2 核心流程

调优循环（来自 scheduling-tuning 教程）：

```text
deps.json   → 图对不对？        → 修边（u7-l1 的依赖管理）
swimlane    → 真的并行了吗？    → 修粒度
scope_stats → 环满了吗？        →  调大那个环
pmu.csv     → 哪条 pipe 到顶？  → 修内核
```

每改一处、重新测一次——四个观测互不独立，同时改两处会无法归因。

#### 4.3.3 源码精读

四个观测点的总表与「按顺序取用」的纪律：

- [docs/en/user/tutorials/05-scheduling-tuning.md:L16-L24](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/docs/en/user/tutorials/05-scheduling-tuning.md#L16-L24) —— 每个问题对应一个 flag 和一个产物文件。
- [docs/en/user/tutorials/05-scheduling-tuning.md:L145-L152](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/docs/en/user/tutorials/05-scheduling-tuning.md#L145-L152) —— 上述调优循环的正文表述。

环形缓冲三个高水位指标与匹配的旋钮：

- [docs/en/user/tutorials/05-scheduling-tuning.md:L76-L89](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/docs/en/user/tutorials/05-scheduling-tuning.md#L76-L89) —— `task_window`（在飞任务槽位）、`heap`（输出存储，单位是**字节**）、`tensormap` 三个环的峰值检测。
- [docs/en/user/tutorials/05-scheduling-tuning.md:L91-L109](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/docs/en/user/tutorials/05-scheduling-tuning.md#L91-L109) —— 对应的每调用旋钮 `ring_task_window` / `ring_heap` / `ring_dep_pool` / `aicpu_thread_num`，可逐调用覆盖、无需重编译。文档特别提醒 `ring_heap` 用字节而 `ring_task_window` 用槽位数——这是最容易犯的错。

DSL 层的优化条目定义（`pl.split` 与 `pl.cross_core_slot`）：

- [python/pypto/language/optimizations.py:L72-L90](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/python/pypto/language/optimizations.py#L72-L90) —— `Split` 数据类：跨核数据搬运的拆分方向（`UP_DOWN` / `LEFT_RIGHT`），设置 `ScopeStmt::split_`，由 `ExpandMixedKernel` Pass 消费。
- [python/pypto/language/optimizations.py:L114-L133](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/python/pypto/language/optimizations.py#L114-L133) —— `CrossCoreSlot`：跨核通道的**环深**（slot 数）。注释说明默认每个活跃方向深度为 2（恰好双缓冲交接），加深让生产核可以跑得更靠前；若作用域最终没有跨核算子，该值被忽略。
- [docs/en/api/optimizations.md:L1-L10](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/docs/en/api/optimizations.md#L1-L10) —— API 文档页：`pl.at(..., optimizations=[...])` 接受的条目清单，条目可自由组合。

采集等级模型（泳道是**分级累积**的，不是 verbosity 开关）：

- [docs/en/user/performance/00-swimlane.md:L52-L74](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/docs/en/user/performance/00-swimlane.md#L52-L74) —— 等级 1 只有 AICore 起止，等级 2 加 AICPU 派发/完成时间戳，等级 3 加调度器主循环相位，等级 4 加编排器相位。等级 1 下派发时间戳**从未被打上**，事后无法恢复；`RunConfig.enable_chip_swimlane` 直接就是这个等级（`True` 等于 4）。

#### 4.3.4 代码实践

**实践目标**：走通「图 → 泳道 → 环」三步观测，熟悉产物位置。

**操作步骤**：

1. 用一次运行同时打开前两个观测点：

   ```python
   from pypto.runtime import RunConfig
   cfg = RunConfig(platform="a2a3sim",
                   enable_chip_swimlane=4,   # 等级 4：全量采集
                   enable_dep_gen=True,      # 泳道记录不含后继边，需要 deps.json 来 join
                   save_kernels=True)        # 保留输出目录
   ```

2. 在产物目录 `<work_dir>/dfx_outputs/` 下确认 `chip_swimlane_records.json` 与 `deps.json` 同时存在。
3. 用 `python -m simpler_setup.tools.swimlane_converter "$RECORDS" --deps-json "$DEPS_JSON" -o out.json` 生成可拖入 [ui.perfetto.dev](https://ui.perfetto.dev) 的轨迹。

**需要观察的现象**：模拟器平台上只有 `chip_swimlane_records.json`，**没有**合并轨迹 `merged_swimlane_*.json`——文档（第 80–86 行）说明模拟器尚未提供转换器所需的任务元数据。真实板上同一开关会**跑两遍**负载（先 dep_gen 取图，再干净跑一遍取时序），所以**绝不从开了泳道的板端运行读墙钟**。

**预期结果**：`dfx_outputs/` 下出现两个文件；Perfetto 中能看到每个任务的四时间戳（dispatch → start → end → finish）。板端双跑行为**待本地验证**（需要板卡环境）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `enable_dep_gen` 和 `enable_chip_swimlane` 要一起开？

**答案**：泳道记录本身**故意不记后继边**（保持设备热路径干净），依赖边只写在 `deps.json` 里，join 在宿主侧事后完成。缺了 `deps.json`，泳道里的任务就是一排孤立的条，看不出谁等谁。

**练习 2**：`ring_heap=64` 为什么被拒绝？

**答案**：`ring_heap` 的单位是**字节**而不是缓冲区个数，64 字节低于下限 1024，直接被拒。这是 [05-scheduling-tuning.md:L107-L108](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/docs/en/user/tutorials/05-scheduling-tuning.md#L107-L108) 点名的「最容易犯的错」。

**练习 3**：`pl.cross_core_slot(slot_num=4)` 改变的是哪个维度？它和 `pl.split(mode)` 的关系是什么？

**答案**：它改变**流水化**维度——把自动插入的跨核数据通道的环深从默认 2 加深到 4，让生产核能领先消费核更多个槽位。它与 `pl.split` 正交：`split` 决定数据**怎么切方向**（上/下、左/右），`cross_core_slot` 决定通道**多深**；两者可在同一个 `optimizations=[...]` 列表里自由组合。

### 4.4 关键路径分析：makespan 到底去哪了

#### 4.4.1 概念说明

`sched_overhead_analysis` 回答一个窄问题（「空闲核上有就绪未派发的工作吗」），而**关键路径分析**回答更宽的一个：*依赖决定的下界是多少？其余的时间被谁花掉了？*

它输出两条路径，**两者的差值就是结论**：

| 路径 | 含义 |
| ---- | ---- |
| **静态 CPM** | 时长加权最长链 = 核数无限时的时延下界 |
| **观测路径** | 从最后完成的任务向回走、按实际执行时间得到的路径 |

观测路径上，每个任务的「计算时长 + 它前面的停顿」正好铺满 makespan。停顿被归因为三类：

- **`data-wait`**：上游生产者迟到（依赖等待）；
- **`core-wait`**：分到的核在忙别的（资源串行化）；
- **`front-gap`**：任何任务开跑之前的发射/派发延迟。

由此得到 verdict 表，整章性能文档按它分支：

| 读数 | 结论 | 去向 |
| ---- | ---- | ---- |
| 静态 CPM 接近 makespan | 依赖受限——加核无用 | 依赖管理 + 任务粒度 |
| 静态 CPM 远低于，`core-wait` 占主导 | 资源串行受限 | 任务粒度 |
| 静态 CPM 远低于，`front-gap` 大 | 发射与派发开销 | 运行时开销 + 宿主侧 |
| 计算高、停顿低 | 真正的计算受限 | InCore 内核优化 |

#### 4.4.2 核心流程

```text
1. 采集: RunConfig(enable_chip_swimlane>=3, enable_dep_gen=True, save_kernels=True)
2. 运行: python -m simpler_setup.tools.critical_path "$RUN_DIR"
        # 自动发现每个含 records + deps + name_map 的目录，逐 rank 出报告
3. 校验: 报告里的 tiling check 必须是 exact，否则归因不成立
4. 读数: 对比 静态CPM vs makespan → 看 stall 三分类占比 → 查 verdict 表
```

三个静默失效点必须在引用数字前排查：tiling check 非 `exact`；family 名出现 `unknown` 或 `cid<N>`（name map 没解析，family 级结论作废）；多轮采集只覆盖**第一轮**（makespan 含预热）。另外，**一次采集只是一个样本**——同一负载两次采集的 stall 占比可差好几个点，绝不能各采一次就对比两种配置。

#### 4.4.3 源码精读

调度开销分析（ narrower 的那个问题）与两条关键定义：

- [docs/en/user/performance/00-swimlane.md:L161-L187](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/docs/en/user/performance/00-swimlane.md#L161-L187) —— `sched_overhead_analysis` 需要等级 ≥ 3 的采集；报告输出每引擎与全系统的开销占比、pickup 成本分布、AICPU 调度循环预算。第 182–186 行给出两条必须内化的定义：**无就绪工作的空闲不是开销**（依赖图强制的，归低并行度），**有就绪未派发工作的空闲才是开销**（调度器跟不上）。

关键路径分析与 verdict 表（本版本新增的一节）：

- [docs/en/user/performance/00-swimlane.md:L189-L213](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/docs/en/user/performance/00-swimlane.md#L189-L213) —— 工具用法、两条路径的定义表，以及「每任务计算 + 前置停顿恰好铺满 makespan」的归因模型（`data-wait` / `core-wait` / `front-gap`）。
- [docs/en/user/performance/00-swimlane.md:L214-L221](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/docs/en/user/performance/00-swimlane.md#L214-L221) —— verdict 表正文：四种读数各自导向的优化方向。

引用数字前的三条校验与「一次采集一个样本」：

- [docs/en/user/performance/00-swimlane.md:L223-L231](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/docs/en/user/performance/00-swimlane.md#L223-L231) —— tiling check 必须 `exact`、`unknown`/`cid<N>` family 名意味着 name map 未解析、多轮采集只覆盖第一轮；以及不要用单次采集对比两种配置。

读图基本功——四时间戳模型：

- [docs/en/user/performance/00-swimlane.md:L138-L157](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/docs/en/user/performance/00-swimlane.md#L138-L157) —— `dispatch → start → end → finish` 各段的含义与所需采集等级；`[start, end]` 是内核本身（唯一能被 InCore 优化缩小的段），`[dispatch, start]` 是取件延迟。「读空隙，不读条」——条窄空隙宽不是内核问题，是粒度或派发问题。

#### 4.4.4 代码实践

**实践目标**：对一个真实运行做关键路径归因，并按 verdict 表给出结论。

**操作步骤**：

1. 以等级 4 采集一个运行（用 4.1 的 Split-K 或任意你自己的内核）：

   ```python
   cfg = RunConfig(enable_chip_swimlane=4, enable_dep_gen=True, save_kernels=True)
   ```

2. 对整个运行树（而不是单个 rank 目录）运行分析：

   ```bash
   RUN_DIR="outputs/<run>"
   python -m simpler_setup.tools.critical_path "$RUN_DIR"
   ```

3. 在报告里依次确认：tiling check 是否 `exact`；family 名是否都是可读的内核名；静态 CPM 与 makespan 的比值；三分类 stall 的占比。

**需要观察的现象**：报告写在 `chip_swimlane_records.json` 旁边；每个 rank 一份。Split-K 例子里，4 个并行任务之间的空隙应主要落在 `front-gap`/`core-wait`（它们互相无依赖，理论上没有 `data-wait`——除了对 zero_init 的那条共同前驱边）。

**预期结果**：tiling check 为 `exact`；verdict 判定结果取决于具体平台与负载，**待本地验证**。若你观察到静态 CPM 远小于 makespan 且 `core-wait` 占主导，结论就是「资源串行受限」，下一步去调任务粒度而不是加依赖。

#### 4.4.5 小练习与答案

**练习 1**：静态 CPM 等于 makespan 意味着什么？加核有用吗？

**答案**：意味着负载**依赖受限**——时间都花在依赖链的串行等待上，核数无限时的下界就是当前 makespan。加核没有帮助；要去缩短关键链本身（拆依赖、合并小任务、或用 u7-l1 的 phase fence 压缩 fan-in 边数）。

**练习 2**：`data-wait` 和 `core-wait` 都表现为「任务没开始」，怎么区分？

**答案**：按原因分。`data-wait` 是**上游生产者**还没把它需要的数据算完（依赖未满足）；`core-wait` 是数据早就绪、但它被分到的那颗核还在跑别的任务（资源冲突）。前者去修依赖图，后者去修任务的粒度与放置。

**练习 3**：为什么报告里必须先看 `tiling check: exact` 这一行才能引用任何归因数字？

**答案**：归因模型的前提是「每任务的计算 + 前置停顿**恰好铺满** makespan」。若这一恒等式不成立（差值非零），说明回走路径没有正确覆盖 makespan，逐任务的 stall 归因就建立在错误的分母上——数字看起来正常但整体不可信。

## 5. 综合实践

以 [examples/intermediate/01_fused_linear.py](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/examples/intermediate/01_fused_linear.py) 的 `fused_matmul_bias`（`c = a @ b + bias`，手工 64×64 分块）为基线，做三版本对照实验。

**第一步：基线测量。** 基线的 matmul 内核是全手写的 `pl.load → pl.move → pl.matmul → pl.store`（[examples/intermediate/01_fused_linear.py:L32-L41](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/examples/intermediate/01_fused_linear.py#L32-L41)），两个 InCore 内核经 `pl.create_tensor` 的中间缓冲串成链（[L65-L71](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/examples/intermediate/01_fused_linear.py#L65-L71)）。记录一次运行的耗时（**单独跑**，不要开泳道——采集会扰动时序）。

**第二步：套用自动分块。** 写一个变体，把手工 matmul 内核换成 Tensor 级一行 `c = pl.matmul(a, b, out_dtype=pl.FP32)` 放进 `pl.at` 作用域，让 AutoTileMatmulL0 接管分块（形态参照 02 号示例的 `ddr_*`）。此时中间量是否落 DDR 由**消费者**决定——如果你把 bias-add 写成对同一输出的逐元素算子，它不是 matmul，中间量走 DDR 直存。

**第三步：套用 Split-K。** 再写一个变体，参照 01 号示例，把 K 维用 `pl.parallel` 切给多个核、原子累加进输出，bias-add 串在其后。注意先把形状改成「小 M/N、大 K」的形态（例如 \(64 \times 2048 @ 2048 \times 64\)），否则 Split-K 没有用武之地。

**第四步：三版本对照。** 对三个版本各做：数值对照（torch 金标准）→ 耗时记录 → `enable_chip_swimlane=4 + enable_dep_gen=True` 的采集 → `critical_path` 报告。填一张表：

| 版本 | makespan | 静态 CPM | 主导 stall | verdict |
| ---- | -------- | -------- | ---------- | ------- |
| 基线（手工 64×64） | 待测 | 待测 | 待测 | 待测 |
| auto tile matmul | 待测 | 待测 | 待测 | 待测 |
| Split-K | 待测 | 待测 | 待测 | 待测 |

**第五步：解释硬件行为。** 对每一步优化写一句话回答「它改变了哪个硬件行为」：自动分块改变的是**分块形状**（(m, n, k) 由 roofline 选出，替代手写 64×64）与**内存驻留**（中间量可留在 L1/Mat 或落 DDR）；Split-K 改变的是**任务图形状**（1 个长任务变 S 个短任务，核占用率上升，代价是原子加）；`pl.cross_core_slot` 若被你用上，改变的是**流水化**（通道环深）。最后根据第三列的 verdict 判断：你的负载是依赖受限（去修图）还是资源串行受限（去修粒度）还是计算受限（回 u5-l6/u6 优化内核本身）。

> 提醒：三份耗时必须来自**未开采集**的干净运行；归因报告来自**另一次**开了采集的运行。同一个配置不要只采一次就比较。

## 6. 本讲小结

- **Split-K** 解决「输出 tile 太少、核空闲」：用 `pl.parallel` 把 K 摊给多个核、`atomic=Add` 合并部分积；代价是必须先清零、浮点结果 ulp 级不可复现。
- **AutoTileMatmulL0** 解决「输出超 L0c / K 超 L0」：沿**放置**（DDR 直存 vs Mat/L1 暂存，由消费者决定）与 **K 策略**（full-K vs split-K，由形状决定）两个正交轴自动选 (m, n, k)；fits-L0c 时还能把 `mode="rint"` 的 cast 折叠进单次 cube 写回。
- 性能三维度：**分块形状**、**内存驻留**（中间量不落 DDR）、**流水化**（`pl.cross_core_slot` 环深、跨核拆分方向）。
- 观测先于修改：`deps.json` 看图、泳道看并行、`scope_stats` 看环、`pmu.csv` 看流水线；每改一处重新测一次。
- **关键路径分析**给出 makespan 去向：静态 CPM 是核数无限的下界，观测路径上的 stall 归因为 `data-wait` / `core-wait` / `front-gap`，verdict 表决定下一步去修依赖、修粒度还是修内核。
- 引用任何归因数字前先过三道校验：tiling check 为 `exact`、family 名可解析、注意多轮采集只覆盖第一轮；且**一次采集只是一个样本**。

## 7. 下一步学习建议

- **模型级开发（u7-l5）**：把本讲的优化手法组合进完整的多内核模型（FFN、FlashAttention），体会「编排级组织数据流 + 计算热点下沉 InCore」的生产范式。
- **调试与性能剖析（u7-l6）**：当优化让结果变错而不是变慢时，用 `dump_passes` 与 IR 降级轨迹定位是哪个 Pass 改坏了东西。
- 继续阅读源码：[docs/en/user/performance/01-task-granularity.md](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/docs/en/user/performance/01-task-granularity.md)、[02-runtime-overhead.md](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/docs/en/user/performance/02-runtime-overhead.md)、[03-dependencies.md](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/docs/en/user/performance/03-dependencies.md)、[04-incore.md](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/docs/en/user/performance/04-incore.md)——verdict 表的四个分支各有一整页展开；[src/ir/transforms/auto_tile_matmul_l0_pass.cpp](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/transforms/auto_tile_matmul_l0_pass.cpp) 配合 u5-l6 精读，理解 roofline 选块的实现。
