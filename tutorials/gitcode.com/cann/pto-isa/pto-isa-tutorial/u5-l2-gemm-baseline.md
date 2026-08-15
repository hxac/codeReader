# GEMM 基线：从零写一个分块矩阵乘

## 1. 本讲目标

学完本讲，你应该能够：

1. 独立写出（或修改）一个**朴素分块 GEMM** 的 PTO kernel：把 `C = A × B` 分解为「核间切分 M/N + 核内 K 迭代」两层循环。
2. 说清 kernel 中每一类 tile（`Mat` / `Left` / `Right` / `Acc`）的角色，以及 ping-pong 双缓冲如何让 `TLOAD → TMOV → TMATMUL` 三段流水重叠。
3. 从搬运量、计算量与同步结构三个角度，定位这个基线实现离「高性能 GEMM」还差什么，为下一讲 `gemm_performance` 的优化做铺垫。

## 2. 前置知识

阅读本向前，请确认你已理解（对应前置讲义）：

- **TMatmul 指令族**（u5-l1）：`TMATMUL` 首轮计算、`TMATMUL_ACC` 累加续算（split-K 累加），以及 `TileLeft`（L0A）/`TileRight`（L0B）/`TileAcc`（累加器）/`TileType::Mat`（L1）四类 tile 的位置约束。
- **数据搬运链路**（u3-l1 / u3-l3）：`TLOAD` 把 GM 数据搬进 L1 的 Mat tile，`TMOV` 在片上层级间复制（含分形重打包），`TSTORE` 把累加器写回 GM。
- **事件同步**（u2-l3）：一个 flag 由 `(srcPipe, dstPipe, eventId)` 三元组确定；GEMM 里会频繁出现 `PIPE_MTE2`（GM→L1 搬入）、`PIPE_MTE1`（L1→L0 搬入）、`PIPE_M`（Cube 矩阵乘）、`PIPE_FIX`（累加器写回）四条流水线。
- **缓冲绑定**（u3-l2）：Manual 模式下 `TASSIGN` 负责把片上偏移绑给 Tile；多块缓冲的偏移排布由开发者手工保证不重叠。
- **CPU 仿真运行方式**（u1-l3）：`python3 tests/run_cpu.py --demo gemm --verbose`。

一个术语提醒：**分块（tiling）**指把大矩阵切成固定形状的小块（tile）逐块处理；**乒乓（ping-pong / double buffer）**指为同一份数据准备两块缓冲交替使用，让「搬第 i+1 块」与「算第 i 块」同时进行。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [demos/baseline/gemm_basic/csrc/kernel/gemm_basic_custom.cpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/demos/baseline/gemm_basic/csrc/kernel/gemm_basic_custom.cpp) | 基线 GEMM 的 NPU kernel：核间切分、K 迭代、双缓冲全在这里（本讲主角） |
| [demos/baseline/gemm_basic/README.md](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/demos/baseline/gemm_basic/README.md) | 示例说明：固定形状 `[512, 2048, 1536]`、24 核 4×6 切分、baseM/baseK/baseN 参数表 |
| [demos/baseline/gemm_basic/csrc/host/my_gemm_basic.cpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/demos/baseline/gemm_basic/csrc/host/my_gemm_basic.cpp) | host 侧 PyTorch 算子封装：shape 检查、24 核启动 |
| [docs/coding/tutorials/gemm.md](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/coding/tutorials/gemm.md) | 官方 GEMM 教程：tile 角色与单 tile 骨架，指出真实 GEMM 需补齐「K 循环 + 事件重叠」 |
| [demos/cpu/gemm_demo/gemm_demo.cpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/demos/cpu/gemm_demo/gemm_demo.cpp) | CPU 仿真下的单 tile GEMM demo，本讲代码实践的运行载体（编译宏 `__CPU_SIM __PTO_AUTO__`） |
| [tests/cpu/st/testcase/tmatmul/tmatmul_kernel.cpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/cpu/st/testcase/tmatmul/tmatmul_kernel.cpp) | TMATMUL 的 ST 用例，其中 `RunTMATMUL_SPLIT_K` 是「K 分块 + TMATMUL_ACC」的最小参照实现 |

注意一个关键差异：`gemm_basic` 是 **NPU 真机示例**（依赖 24 核 `get_block_idx()` 切分与 `__CCE_AICORE__ == 220` 编译分支），CPU 仿真不能直接编译它；但它的每条指令（TLOAD/TMOV/TMATMUL/TMATMUL_ACC/TSTORE）在 CPU 仿真后端都有同名实现，因此「K 分块循环」的结构可以在 `demos/cpu/gemm_demo` 上复现验证——这正是本讲实践的路线。

## 4. 核心概念与源码讲解

### 4.1 分块循环：GEMM 的两层分解

#### 4.1.1 概念说明

一个朴素 GEMM \( C_{M \times N} = A_{M \times K} \times B_{K \times N} \) 无法一次装进片上存储：以本例 `M=512, K=2048, N=1536`、fp16 输入计，A 就有 2MB，远超 L1/L0 容量。因此必须分块，且分两层：

1. **核间切分（空间维）**：把输出 C 按 M×N 切成 24 份（4×6），每核负责一块 `singleCoreM × singleCoreN` 的子矩阵。这一层不需要任何同步——各核的输出区域互不重叠。
2. **核内 K 迭代（累加维）**：每核拿到的子任务仍是 `128 × 2048 × 256`，K 维依然装不下，于是再把 K 切成 `kLoop = singleCoreK / baseK` 段，每段做一次 `baseM × baseK × baseN`（128×64×256）的小矩阵乘，用 `TMATMUL_ACC` 累加进同一个 `TileAcc`。

为什么 M/N 层用「各算各的」而 K 层必须用「累加」？因为 M/N 切分后各块输出独立，天然可并行；而 K 切分后各段贡献的是**同一块输出的部分和**，只能累加，不能并行写。

\[ \text{每核计算量} = 2 \cdot singleCoreM \cdot K \cdot singleCoreN = 2 \times 128 \times 2048 \times 256 \approx 1.34 \text{ GFLOP} \]

#### 4.1.2 核心流程

以 `gemm_basic` 为例，整个 kernel 的控制流可以写成：

```text
gemm_basic_custom(a, b_dn, out):
    # 第 0 层：核间切分（M/N）
    mIterIdx = block_idx % 4      # 本核负责哪个 M 块
    nIterIdx = block_idx / 4      # 本核负责哪个 N 块
    由 mIterIdx/nIterIdx 算出 A/B/C 三块数据的 GM 起始地址

    # 第 1 层：核内 K 迭代
    for kIter in 0 .. (singleCoreK/baseK - 1):     # 2048/64 = 32 次
        TLOAD  A/B 的第 kIter 段 → L1 的 Mat tile（乒乓 cur = kIter % 2）
        TMOV   L1 Mat tile → L0 的 Left/Right tile（乒乓）
        kIter == 0 ? TMATMUL(c, a, b)              # 首轮：清零累加
                  : TMATMUL_ACC(c, c, a, b)        # 续算：累加
    TSTORE cTile → GM
```

注意分块循环的顺序：M/N 完全外移到核间，核内只剩 K 一层循环。这是最简单也最「朴素」的组织方式——它意味着每个 tile 的搬运次数由 K 循环次数唯一决定。

#### 4.1.3 源码精读

**入口常量**集中在一个 `constexpr` 块中，这正是本讲实践要改的「分块参数面板」：

[demos/baseline/gemm_basic/csrc/kernel/gemm_basic_custom.cpp:L137-L150](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/demos/baseline/gemm_basic/csrc/kernel/gemm_basic_custom.cpp#L137-L150)

这段代码定义了 `M/K/N=512/2048/1536`、`singleCoreM/K/N=128/2048/256`、`baseM/K/N=128/64/256`，并调用模板函数 `runGEMMBASIC`。M、N 的核间切分比例（4×6=24 核）是编译期写死的，与 host 侧 `blockDim = 24` 对应。

**核间切分**只用了三行地址运算，没有任何通信：

[demos/baseline/gemm_basic/csrc/kernel/gemm_basic_custom.cpp:L79-L87](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/demos/baseline/gemm_basic/csrc/kernel/gemm_basic_custom.cpp#L79-L87)

`mIterIdx = get_block_idx() % mIter`、`nIterIdx = get_block_idx() / mIter`：24 个核按「先 M 后 N」的顺序编号，各自算出 A、B、C 三块的 GM 偏移。A 按 `mIterIdx * singleCoreM * K` 平移（跳过前面的行），B 按 `nIterIdx * K * singleCoreN` 平移，C 偏移 = 两者组合。

**K 循环次数**与循环体调用：

[demos/baseline/gemm_basic/csrc/kernel/gemm_basic_custom.cpp:L116-L125](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/demos/baseline/gemm_basic/csrc/kernel/gemm_basic_custom.cpp#L116-L125)

`kLoop = singleCoreK / baseK = 2048 / 64 = 32`。循环前先「预放」四个 flag（L118-L121），让第一次迭代里消费者 `wait_flag` 不会死等——这是事件驱动流水的标准起手式；循环结束后再收尾等待（L126-L129）。

**K 迭代内的累加分派**：

[demos/baseline/gemm_basic/csrc/kernel/gemm_basic_custom.cpp:L66-L70](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/demos/baseline/gemm_basic/csrc/kernel/gemm_basic_custom.cpp#L66-L70)

`kIter == 0` 走 `TMATMUL`（硬件此时会初始化累加器），之后 31 次全部走 `TMATMUL_ACC`。这就是 u5-l1 讲过的 split-K 累加协议在 kernel 层的落点。

**官方教程视角**：[docs/coding/tutorials/gemm.md:L48-L58](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/coding/tutorials/gemm.md#L48-L58) 明确说单 tile 骨架要变成真实 GEMM，通常要加两样东西：对 M/N/K 的分块（K 上循环 + `TMATMUL_ACC`）和用于重叠的事件 + ping-pong 缓冲。`gemm_basic` 正是这份清单的最小完整实现。

#### 4.1.4 代码实践

**实践目标**：理解「改分块大小」到底要改哪几处，并验证分块变化不改变计算结果（分块只是执行策略，不是算法）。

**操作步骤**（源码阅读 + 推演，不实际改源码）：

1. 打开 [gemm_basic_custom.cpp:L139-L147](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/demos/baseline/gemm_basic/csrc/kernel/gemm_basic_custom.cpp#L139-L147)，假设把 `baseK` 从 64 改成 128。
2. 逐项推演哪些量会自动跟着变、哪些需要手工同步修改：
   - `kLoop` 由 32 变 16（`singleCoreK / baseK` 自动计算，无需改）；
   - L1 缓冲占用：`aMatTile`/`bMatTile` 各两块，字节数随 `baseM*baseK`、`baseK*baseN` 翻倍（L98-L101 的 `TASSIGN` 偏移表达式也自动按 `baseK` 缩放）；
   - host 侧 [my_gemm_basic.cpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/demos/baseline/gemm_basic/csrc/host/my_gemm_basic.cpp) 与 GM shape **完全不用改**——分块参数对上层不可见。
3. 再推演一个会**出错**的改法：把 `baseM` 改成 200（不整除 `singleCoreM=128`）。此时 `M / singleCoreM` 与 tile 边界不再对齐，`kIter * baseK` 的地址平移会越过本核子矩阵边界，读到别的核的数据。

**需要观察的现象 / 预期结果**：整除且缓冲装得下的改动（如 baseK 64→128）只影响性能与占用，不影响数值结果；不整除的改动会产生错误数据甚至越界。结论应是一条纪律：**baseM/baseK/baseN 必须整除 singleCoreM/K/N，且总缓冲 ≤ 片上容量**。本实践为纯推演，运行验证放到 4.2.4 与综合实践。

#### 4.1.5 小练习与答案

**练习 1**：本例中为什么把 M/N 的切分放到核间、K 的切分放到核内，而不是反过来（核间切 K、核内循环 M/N）？

**答案**：核间切 K 意味着多个核各算一部分和，最后必须做跨核累加（写同一块 C 或二次归约），引入核间同步与通信；核间切 M/N 则各核输出区域互不重叠，零通信零同步即可完成。所以「输出归属」维度优先切到核间，「累加」维度留在核内由 `TMATMUL_ACC` 处理，是最朴素也最安全的方案。

**练习 2**：`kLoop` 在代码里是 `singleCoreK / baseK`。若 `singleCoreK = 2048`、`baseK = 96`，会发生什么？

**答案**：`2048 / 96 = 21`（整除截断），21 × 96 = 2016，剩余 32 的 K 尾段被静默丢弃，结果错误。当前基线没有任何断言检查整除性，整除约束完全靠开发者保证（这正呼应 4.1.4 的纪律）。

### 4.2 tile 复用：四类 tile 的数据通路与乒乓双缓冲

#### 4.2.1 概念说明

GEMM kernel 中「tile 复用」有两层含义：

1. **同一 tile 存储被多轮 K 迭代反复复用**：L1 的 `aMatTile[2]/bMatTile[2]`、L0 的 `aTile[2]/bTile[2]` 都是容量固定的缓冲，32 轮迭代反复写入同一组地址，靠事件保证「上一轮的消费者用完才允许下一轮的生产者覆盖」。
2. **数据通路上的层级复用**：同一份数据从 GM 出发，先落 L1（`TileType::Mat`），再经 `TMOV` 搬到 L0A/L0B（`TileLeft`/`TileRight`），结果进 `TileAcc`。每一层 tile 是「为下一层喂料」的中转站。

乒乓（double buffer）的动机：如果只有一块缓冲，第 i+1 轮的 `TLOAD` 必须等第 i 轮的 `TMATMUL` 完全结束才能开始；有两块缓冲（`cur = kIter % 2` 交替）后，Cube 在算第 i 块的同时，MTE2/MTE1 可以提前搬第 i+1 块，搬运时间被计算时间「藏」掉。

#### 4.2.2 核心流程

一轮 K 迭代（`ProcessKIteration`）内的生产-消费链，按流水线画成：

```text
GM ──TLOAD──▶ L1 Mat tile[cur] ──TMOV──▶ L0 Left/Right tile[cur] ──TMATMUL(_ACC)──▶ Acc ──TSTORE──▶ GM
     MTE2            │                      MTE1                     PIPE_M                FIXPIPE
                     │                          │                       │
   wait(MTE1→MTE2, cur)  ←上一轮 MTE1 用完这块缓冲   wait(M→MTE1, cur)        set(M→MTE1, cur)
```

事件三元组（本函数里只用了 `EVENT_ID0`/`EVENT_ID1` 与 `cur` 两个编号）配对关系：

- `wait_flag(MTE1, MTE2, cur)` / 上一轮的 `set_flag(MTE1, MTE2, cur)`：L0 搬运用完了 L1 的 `cur` 号缓冲，MTE2 才能覆盖它。
- `set_flag(MTE2, MTE1, EVENT_ID0/1)`：MTE2 宣告 A/B 的新数据已进 L1，MTE1 可以 `TMOV` 了。
- `wait_flag(M, MTE1, cur)` / `set_flag(M, MTE1, cur)`：Cube 用完 L0 的 `cur` 号缓冲，MTE1 才能覆盖；反向的 `set_flag(MTE1, M, cur)` 宣告 L0 新数据就绪。

由于缓冲编号只有 2 个（cur 取 0/1），相邻两轮使用不同编号，重叠深度恰好为一轮——这就是「乒乓」。

#### 4.2.3 源码精读

**四类 tile 的声明与 TASSIGN 偏移排布**：

[demos/baseline/gemm_basic/csrc/kernel/gemm_basic_custom.cpp:L93-L114](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/demos/baseline/gemm_basic/csrc/kernel/gemm_basic_custom.cpp#L93-L114)

这段代码先声明 L1 层的两块 A Mat tile 与两块 B Mat tile，并用 `TASSIGN` 手工排偏移（L98-L101：A 两块、B 两块在 L1 上依次相邻，偏移由 `baseM*baseK*sizeof(U)`、`baseK*baseN*sizeof(U)` 算出）；再声明 L0 层的 `TileLeft aTile[2]`、`TileRight bTile[2]`、`TileAcc cTile` 并各自 `TASSIGN`。注意 L1 与 L0 是不同存储，两处的 `0x0` 偏移互不冲突；而同层内四块缓冲的偏移必须手工错开——u3-l2 讲过，TASSIGN 不查重叠，排布是开发者的责任。

**一轮 K 迭代的完整事件编排**：

[demos/baseline/gemm_basic/csrc/kernel/gemm_basic_custom.cpp:L47-L61](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/demos/baseline/gemm_basic/csrc/kernel/gemm_basic_custom.cpp#L47-L61)

`cur = kIter % 2` 选缓冲；随后 `TLOAD` A/B 到 L1（L52/L54），`set_flag(MTE2, MTE1, ...)` 各挂一次牌；接着 `TMOV` 把 L1 数据搬进 L0（L59/L61）。注意 `TMOV` 前的三个 `wait_flag`（L57-L60）：既要等 Cube 释放 L0 的 cur 缓冲，也要等 MTE2 宣告 L1 数据就绪。

**搬运与计算的数据量对比**（用于体会 tile 复用的收益）：每轮 K 迭代搬运 \( (128 \times 64 + 64 \times 256) \times 2\,\text{B} = 48\,\text{KB} \)，计算 \( 2 \times 128 \times 64 \times 256 = 4.19\,\text{M FLOP} \)——每搬 1 字节就能换约 85 FLOP，这正是 Cube 指令的高算术密度，也是后面「用流水线把搬运藏进计算」值得做的原因。

**CPU 仿真侧的最小参照**——`demos/cpu/gemm_demo` 用单 tile 骨架（无 K 循环、无乒乓）验证同一套指令：

[demos/cpu/gemm_demo/gemm_demo.cpp:L109-L121](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/demos/cpu/gemm_demo/gemm_demo.cpp#L109-L121)

`TLOAD → TMOV → TMATMUL → TSTORE`，与 NPU 版指令序列同构，只是没有事件（CPU 仿真的事件是空桩，u2-l3 讲过）与双缓冲。它的 CMake 用 `__CPU_SIM __PTO_AUTO__` 编译（见 [demos/cpu/gemm_demo/CMakeLists.txt](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/demos/cpu/gemm_demo/CMakeLists.txt)），所以 demo 里的 `TASSIGN` 全是空操作、缓冲由编译器自动分配。

**带 K 累加的 ST 用例参照**：`RunTMATMUL_SPLIT_K` 展示了最纯粹的「K 分块 + TMATMUL_ACC」循环：

[tests/cpu/st/testcase/tmatmul/tmatmul_kernel.cpp:L143-L171](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/cpu/st/testcase/tmatmul/tmatmul_kernel.cpp#L143-L171)

循环体内 `i == 0` 走 `TMATMUL`、其余走 `TMATMUL_ACC`，与 `gemm_basic` 的分派逻辑一字不差——这说明基线 GEMM 的「K 累加骨架」在 CPU 仿真上是可直接验证的。

#### 4.2.4 代码实践

**实践目标**：在 CPU 仿真下亲手实现「K 分块 + TMATMUL_ACC」循环，验证分块大小改变不影响结果。

**操作步骤**：

1. 复制 demo 目录，避免改动源码树：`cp -r demos/cpu/gemm_demo /tmp/gemm_tiled_demo`（仓库本身不修改）。
2. 修改 `/tmp/gemm_tiled_demo/gemm_demo.cpp`（示例代码，基于原 demo 改写）：
   - 把 `kK` 从 16 改为 64，新增 `constexpr int kBaseK = 16;`（K 分块大小，可整除 kK）；
   - 把 `GlobalB` 与 B 的 Mat tile 形状改为按 `kBaseK` 切的视图（B 的 GM stride 不变，tile 形状换成 `kK→kBaseK` 的分块版本），参考 `gemm_basic` 中 `TileShape2D`（tile 尺寸）与 `BaseShape2D`（整核形状）分离的写法（[gemm_basic_custom.cpp:L39-L45](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/demos/baseline/gemm_basic/csrc/kernel/gemm_basic_custom.cpp#L39-L45)）；
   - 把单次 `TMATMUL` 换成循环（示例代码）：

   ```cpp
   for (int kIter = 0; kIter < kK / kBaseK; ++kIter) {
       // 每轮构造指向 B 第 kIter 段的 GM 视图（A 的 K 等于 kK 时 A 也可同样分段）
       GlobalB bGlobalK(B.data() + kIter * kBaseK * kN);   // 示例代码：按 K 段平移
       TLOAD(bMatTile, bGlobalK);
       TMOV(bTile, bMatTile);
       if (kIter == 0) {
           TMATMUL(cTile, aTile, bTile);
       } else {
           TMATMUL_ACC(cTile, cTile, aTile, bTile);
       }
   }
   ```

   （demo 用 `__PTO_AUTO__` 编译，tile 缓冲自动分配，无需为乒乓另做 TASSIGN。）
3. 构建运行：`python3 tests/run_cpu.py --demo gemm --verbose` 只会构建原版 demo；改的是副本，因此请手动构建：

   ```bash
   cmake -S /tmp/gemm_tiled_demo -B /tmp/gemm_tiled_demo/build \
         -DCMAKE_CXX_STANDARD=20 && cmake --build /tmp/gemm_tiled_demo/build
   /tmp/gemm_tiled_demo/build/gemm_demo
   ```

4. 再把 `kBaseK` 在 `16 / 8 / 4` 之间切换，各跑一次。

**需要观察的现象**：程序输出 `gemm_demo: M=.. K=.. N=..`、`max_abs_diff=...` 与 `perf: avg_ms=... gflops=...`（输出格式见 [gemm_demo.cpp:L131-L141](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/demos/cpu/gemm_demo/gemm_demo.cpp#L131-L141)）。

**预期结果**：所有 `kBaseK` 取值下 `max_abs_diff` 均 `< 1e-3`（退出码 0），证明分块只改执行策略、不改数值；`gflops` 可能随分块粒度略有波动（CPU 仿真不模拟流水线，波动主要来自缓存局部性，不能据此推断 NPU 性能）。若构建报 C++ 标准错误，请确认编译器为 GCC ≥ 13 或 Clang ≥ 15（u1-l3 的环境要求）。本实践的完整运行结果**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：`ProcessKIteration` 里 `TMOV` 之前要等两个方向的 flag（`wait_flag(PIPE_M, PIPE_MTE1, cur)` 和 `wait_flag(PIPE_MTE2, PIPE_MTE1, EVENT_ID0/1)`），分别防的是什么错误？

**答案**：前者防止**写覆盖**——上一轮 Cube 还在读 L0 的 cur 号缓冲，MTE1 就把新数据 TMOV 进去会破坏正在参与计算的操作数；后者防止**读未就绪**——MTE2 还没把新数据写进 L1，MTE1 就去搬会读到上一轮的旧数据。

**练习 2**：为什么 `TileAcc cTile` 只需要一块，而 A、B 的 Mat/Left/Right tile 都要两块？

**答案**：cTile 是 K 循环的唯一累积终点，`TMATMUL_ACC` 每轮「读它又写它」，同一轮内没有第二方生产者需要与之交替，不存在覆盖竞争；A/B 缓冲则要在「本轮被 Cube 消费」与「下轮被 MTE2/MTE1 写入」之间交替，所以必须乒乓。（代价是：这使累加天然串行，Cube 无法用两份累加器并行两轮——这是基线实现的一个结构限制。）

### 4.3 基线瓶颈分析：朴素分块差在哪

#### 4.3.1 概念说明

「基线」的意思是：功能完整、结构最简，但每一类资源都只用了一遍。分析 tile 级 kernel 瓶颈的通用框架是问三个问题：

1. **搬运效率**：GM→L1 的搬运次数是否已达数据量的下限？有没有重复搬运？
2. **重叠深度**：乒乓只有一级（深度 1），搬运能否被计算完全覆盖？
3. **复用半径**：一个 tile 搬进来后被用了几次才被换出？（复用次数 = 搬运量的倒数）

#### 4.3.2 核心流程

对 `gemm_basic` 逐项检视：

- **B tile 的跨核重复搬运**。核间按 M×N 切成 4×6 后，同一个 B 子块（K×256）会被 4 个不同 M 组的核各自完整搬运一遍；同理 A 子块被 6 个 N 组重复搬运。全局搬运量：

\[ \text{GM 搬入总量} = 4 \times 6 \times K \times (singleCoreM + singleCoreN) \times 2\,\text{B} = 24 \times 2048 \times 384 \times 2 \approx 37.5\,\text{MB} \]

  而数据本身只有 \( (512+1536) \times 2048 \times 2\,\text{B} \approx 8.4\,\text{MB} \)，**放大系数约 4.5×**。基线没有利用 L2 让相邻核共享搬运。

- **乒乓深度只有 1**。`cur = kIter % 2` 意味着 MTE2 最多领先 Cube 一个 baseK 段；若某段搬运因 GM 带宽抖动变慢，Cube 立刻空转。基线也没有把 `TSTORE` 与下一 tile 的计算重叠（写回在 K 循环结束才发生一次，本例影响小，但 M/N 需要核内循环时就会暴露）。

- **baseK=64 偏小**。每段 64 的 K 使 `TMOV`（L1→L0）指令次数 = 2×32×24 核 = 1536 次，MTE1 的固定开销（指令发射、分形重打包）被摊薄得不够。

- **单 tile 的算术强度倒不是瓶颈**：4.2.3 已算出每轮 85 FLOP/字节，Cube 吃得饱的前提是搬运能跟上——所以基线的短板集中在**搬运侧（MTE2/MTE1）与重叠结构**，而不是 Cube 本身。

#### 4.3.3 源码精读

**核间切分导致 B 重复搬运的根源**在切分公式本身：

[demos/baseline/gemm_basic/csrc/kernel/gemm_basic_custom.cpp:L79-L84](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/demos/baseline/gemm_basic/csrc/kernel/gemm_basic_custom.cpp#L79-L84)

`currentGmOffsetB = nIterIdx * K * singleCoreN` 只依赖 nIterIdx：`block_idx` 为 0 和 6 的两个核（不同 mIterIdx、相同 nIterIdx）会搬**完全相同**的 B 数据，却各自从 GM 走一遍 MTE2。基线没有任何跨核共享机制。

**乒乓深度 1 的证据**：

[demos/baseline/gemm_basic/csrc/kernel/gemm_basic_custom.cpp:L47-L49](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/demos/baseline/gemm_basic/csrc/kernel/gemm_basic_custom.cpp#L47-L49)

`cur = kIter % 2`，`GlobalDataSrcA gmA(currentSrc0 + kIter * baseK)`——第 kIter 轮只能 prefetch 到第 kIter+1 段（等 cur 缓冲释放），无法更早。缓冲数组大小 `[2]`（L34-L36 的函数形参）就是深度的硬编码。

**README 对平台与切分的说明**（也是性能讨论的语境）：

[demos/baseline/gemm_basic/README.md:L50-L61](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/demos/baseline/gemm_basic/README.md#L50-L61)

README 写明 24 核 4×6 切分、baseK=64 的选择理由是「per-core tile 超出 L0 容量」——即分块大小目前由**容量约束**（装得下）决定，而不是由**性能约束**（搬运/计算平衡）决定，这正是基线与 `gemm_performance`（u5-l3）的分水岭。

**官方教程对「下一步」的提示**：

[docs/coding/tutorials/gemm.md:L48-L58](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/coding/tutorials/gemm.md#L48-L58)

「events to order memory and compute pipelines, ping-pong buffers to reuse tile storage safely」——基线只做到了教程要求的最小集；更深的多级缓冲、L2 复用、多核协同留给了性能版本。

#### 4.3.4 代码实践

**实践目标**：亲手量化「跨核重复搬运」这一瓶颈，建立对切分方案与搬运量关系的直觉。

**操作步骤**（纸笔 + 计算器即可）：

1. 以 `gemm_basic` 的参数（M=512, K=2048, N=1536, fp16，24 核 4×6 切分）计算：
   - 数据总量（A+B）：`(512×2048 + 2048×1536) × 2` 字节；
   - 全核 GM 搬入总量：`24 × 2048 × (128 + 256) × 2` 字节（每核搬 singleCoreM+singleCoreN 行/列宽的 K 段）；
   - 放大系数 = 后者 ÷ 前者。
2. 换两种切分重算：`2×12`（singleCoreM=256, singleCoreN=128）与 `1×24`（singleCoreM=512, singleCoreN=64）。
3. 推演理想下界：若存在完美 L2 缓存（同一 B 子块只从 GM 搬一次），搬入量应为多少。

**需要观察的现象 / 预期结果**：三种切分的放大系数分别为 4.5、约 3.7（`24×(256+128)/(512+1536)`≈4.5——请实际计算核对）、以及更小但单核负载不均衡的方案；理想下界就是数据总量 8.4MB。结论：**在总核数固定时，切分比例会改变放大系数；彻底消除冗余需要跨核共享（L2/多核协同）**——这恰是 `kernels/manual/a2a3/gemm_performance` 要解决的问题，下一讲逐行对照验证。

#### 4.3.5 小练习与答案

**练习 1**：基线 kernel 是 CUBE Bound 还是 MTE Bound？给出论证。

**答案**：结构性判断是 MTE 侧风险更大。单轮迭代的算术强度约 85 FLOP/字节，Cube 需求侧看完全吃得饱；但 MTE2 有约 4.5× 的跨核冗余搬运，且乒乓深度只有 1，GM 带宽或延迟抖动会直接让 Cube 停转。不过最终结论应以真机 profiling 为准——这也是 PTO 文档推荐「CPU 仿真验证功能、真机验证性能」流程（u1-l1）的原因。

**练习 2**：把 `baseK` 从 64 增大到 256，4.3.2 列出的哪些瓶颈会缓解、哪些不会？

**答案**：缓解：MTE1 次数与分形重打包开销降为 1/4，单条指令的搬运粒度更大、效率更高。不会缓解：跨核 B/A 重复搬运（由核间切分决定，与 baseK 无关）；乒乓深度仍为 1；同时 baseK=256 会占更多 L1/L0 缓冲，可能顶到容量上限——所以 baseK 存在一个「摊薄开销 vs 缓冲容量」的最优点，需要扫描确定。

## 5. 综合实践

**任务：在 CPU 仿真下复现 `gemm_basic` 的 K 分块骨架，并做一次分块扫描。**

1. 以 `demos/cpu/gemm_demo/gemm_demo.cpp` 为底版复制出 `/tmp/gemm_tiled_demo`（不要改仓库源码），按 4.2.4 的步骤加入 K 分块循环（`TMATMUL` 首轮 + `TMATMUL_ACC` 续算），参照 [tmatmul_kernel.cpp 的 RunTMATMUL_SPLIT_K](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/cpu/st/testcase/tmatmul/tmatmul_kernel.cpp#L114-L173) 的写法。
2. 把矩阵规模放大到 `kM=64, kK=128, kN=64`，K 分块分别取 64 / 32 / 16 跑三组，记录每组的 `max_abs_diff` 与 `gflops`。
3. 检查清单（把答案写在笔记里）：
   - 三组结果是否全部 `max_abs_diff < 1e-3`？（分块不变性）
   - `gflops` 随分块变小如何变化？在 CPU 仿真上能否据此推断 NPU 性能？（不能——说明理由：CPU 仿真的同步是空桩、单线程按序执行）
   - 对照 [gemm_basic_custom.cpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/demos/baseline/gemm_basic/csrc/kernel/gemm_basic_custom.cpp) 列出：你的 CPU 版缺少了 NPU 版的哪三样结构？（多核切分、事件同步、乒乓双缓冲）
4. 预期成果：一张「kBaseK → max_abs_diff / gflops」三行表格，加一段「基线 → 性能版」待改进点清单（对应 4.3 的三个瓶颈）。运行数据**待本地验证**。

## 6. 本讲小结

- 朴素分块 GEMM = **核间切分 M/N（零同步）+ 核内 K 迭代（TMATMUL 首轮、TMATMUL_ACC 累加）** 两层分解；分块参数（baseM/baseK/baseN）对 host 侧完全透明。
- kernel 是一条 **GM →(TLOAD/MTE2)→ L1 Mat tile →(TMOV/MTE1)→ L0 Left/Right tile →(TMATMUL/M)→ Acc →(TSTORE/FIXPIPE)→ GM** 的四级数据通路，四类 tile 各守一级。
- 「tile 复用」靠 **`[2]` 大小的乒乓缓冲 + `(srcPipe, dstPipe, eventId)` 事件配对**实现：编号 `cur = kIter % 2` 隔离相邻两轮，写覆盖与读未就绪各由一个方向的 flag 防护。
- 分块大小由**容量约束**（装得下、整除）先定，性能最优需扫描；不整除的 baseK 会被整除截断静默丢尾段。
- 基线三大瓶颈：**跨核 A/B 重复搬运（约 4.5×，无 L2 共享）、乒乓深度仅 1、baseK 偏小导致 MTE1 开销摊薄不足**；算术强度（约 85 FLOP/B）本身不是短板。
- CPU 仿真可用同一套指令验证 K 分块累加骨架的**功能正确性**，但同步为空桩、无流水线，**不能**据此推断性能。

## 7. 下一步学习建议

本讲结束，你已经能写出正确的分块 GEMM。下一讲 **u5-l3「高性能 GEMM：gemm_performance 优化全解」** 将逐条消解本讲 4.3 列出的瓶颈——建议先浏览 [kernels/manual/a2a3/gemm_performance/README.md](https://github.com/gitcode.com/cann-pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/gemm_performance/README.md)，带着「它比基线多了什么」的问题去读 [gemm_performance_kernel.cpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/gemm_performance/gemm_performance_kernel.cpp)。此外可同步阅读 `docs/coding/pipeline-parallel.md` 与 `docs/coding/opt.md`（u6-l2/u6-l3 的主材料），把本讲的事件编排知识升级为系统性的流水线设计方法。
