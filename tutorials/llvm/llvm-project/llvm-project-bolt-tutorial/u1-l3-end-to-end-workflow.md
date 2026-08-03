# 端到端使用流程：从 perf 采集到 BOLT 优化

> 本讲是「认识 BOLT」单元的第 3 讲（u1-l3），承接 [u1-l2](u1-l2-build-and-run.md) 把 BOLT 编译出来的步骤，带你把 `bin/` 下那一堆工具真正「串」成一条完整的工作流：从给二进制加上重定位、用 `perf` 采集分支采样、用 `perf2bolt` 聚合成 fdata，最后用 `llvm-bolt` 跑出优化后的二进制，并用 `-dyno-stats` 量化收益。

## 1. 本讲目标

学完本讲，你应该能够：

1. 完整复述 BOLT 的「四步标准流程」（Step 0 链接 → Step 1 采集 → Step 2 转换 → Step 3 优化），并说清每一步要解决的子问题。
2. 写出从 `-Wl,-q` 链接、`perf record -j any,u` 采集、`perf2bolt` 转换，到 `llvm-bolt -reorder-blocks=ext-tsp -split-functions ...` 优化的完整命令序列。
3. 解释 `reorder-blocks`、`reorder-functions`、`split-functions` 这几个关键优化选项分别改的是什么，以及 `-dyno-stats` 输出里的百分比如何反映「优化前 vs 优化后」。
4. 在没有 LBR/`perf` 的环境（如虚拟机）里，知道用 `-instrument` 插桩作为替代采集方式，并说清它与采样方式的取舍。

## 2. 前置知识

在动手之前，先建立几个直觉。如果你已读完 [u1-l1](u1-l1-bolt-overview.md) 和 [u1-l2](u1-l2-build-and-run.md)，可以快速扫过本节。

- **后链接优化（post-link）**：BOLT 工作在「链接之后」，输入是一个**已经链接好**的 ELF 可执行文件，输出是另一个布局更优的 ELF。它不重新编译你的 C/C++ 源码，而是直接改写机器码的排列方式。
- **profile-guided（轮廓引导）**：BOLT 必须知道「程序实际运行时哪些代码是热的、哪些是冷的」才能做布局优化。这份运行时信息叫 **profile**，是 BOLT 发挥作用的输入。**没有 profile，BOLT 基本无法优化**——所以「采集 profile」是本讲最重要的一步。
- **分支采样（branch sampling）**：现代 CPU 有记录「最近若干条跳转」的硬件（x86 上叫 **LBR**，AArch64 上叫 **BRBE**）。`perf` 工具能把这些跳转历史采下来（`-j any,u` / `-F brstack`），这是 BOLT 最优质的 profile 来源。
- **重定位（relocations）**：链接器默认会把重定位信息丢掉。加上 `--emit-relocs`（`-q`）后，二进制里会保留 `.rela.text` 段，BOLT 才能安全地「搬动」函数位置。这一点的原理留到 [u3-l4](u3-l4-relocation-and-jumptable.md) 深讲，本讲只需记住：**Step 0 加 `-q` 是为了 Step 3 能重排函数**。
- **指令缓存（i-cache）与 MPKI**：CPU 取指令时会先查一级指令缓存（L1 i-cache），没命中（miss）就要去更慢的层级取，拖慢前端。BOLT 的核心收益就是把热代码排得更紧凑，减少这类 miss。衡量它的常用指标是 **MPKI（每千条指令的 miss 数）**：

\[ \text{MPKI} = \frac{\text{L1-icache-misses}}{\text{instructions}} \times 1000 \]

  官方经验法则是：MPKI 超过 10，就很可能从 BOLT 获益。

如果你还没把 BOLT 编出来，请先回到 [u1-l2](u1-l2-build-and-run.md) 跑通 `ninja bolt`，确保 `bin/` 下有 `llvm-bolt`、`perf2bolt` 等工具。

## 3. 本讲源码地图

本讲几乎不碰 C++ 源码，主要依据是三份**使用文档**——它们就是 BOLT 官方的「操作手册」：

| 文件 | 作用 |
| --- | --- |
| `bolt/README.md` | BOLT 的主 README，其中 `Usage` 章节用 **Step 0/1/2/3** 给出了最权威的四步流程速查。本讲的主干。 |
| `bolt/docs/GettingStarted.md` | 入门文档，结构与 README 高度一致（同样 Step 0–3），并补充了 DWARF 版本等注意事项。 |
| `bolt/docs/OptimizingClang.md` | 一份完整的实战教程：以优化 Clang 编译器本身为例，给出真实的 `perf`/`perf2bolt`/`llvm-bolt` 命令，以及优化前后 `-dyno-stats` 的真实输出。本讲引用它的真实数据。 |
| `bolt/docs/profiles.md` | 各类 profile 格式（perf.data / fdata / YAML / pre-aggregated）的总览。本讲末尾会引用它做拓展。 |

> 说明：三份文档在个别选项的默认取值上不完全一致（例如 Step 3 的 `-reorder-functions`），本讲会指出来，读者照着任一份都能跑通。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：① 四步标准流程；② 关键优化选项与 `-dyno-stats`；③ `-instrument` 插桩作为替代采集方式。

### 4.1 四步标准流程：从链接到优化的完整链路

#### 4.1.1 概念说明

BOLT 的使用不是「一条命令搞定」，而是一条**四步流水线**，每一步解决一个明确的子问题：

- **Step 0（链接准备）**：在最终链接阶段加 `--emit-relocs`（`-q`），让二进制带上重定位元数据。**没有这一步，BOLT 只能改函数内部布局，不能重排函数之间的顺序。**
- **Step 1（采集 profile）**：用 `perf record` 在真实负载上采分支采样，得到 `perf.data`。
- **Step 2（转换格式）**：用 `perf2bolt` 把庞大的 `perf.data` 聚合成更紧凑、更稳定的 fdata 文本格式。
- **Step 3（优化）**：用 `llvm-bolt` 读 fdata，反汇编重建 CFG，叠加 profile，重排代码，写出优化后的二进制。

这四步缺一不可，但 Step 1/2 在某些环境（虚拟机、无 LBR 硬件）下无法完成，这时要用 4.3 节讲的 `-instrument` 插桩替代。下图（文字版）把数据流串起来：

```text
  源码/obj            已链接 ELF (+.rela.text)         perf.data (大)        perf.fdata (紧凑)        优化后 ELF
  ───────▶ Step 0 链接 (-Wl,-q) ──▶ Step 1 perf record ──▶ Step 2 perf2bolt ──▶ Step 3 llvm-bolt ──▶ *.bolt
                                   ↑__________________  profile  __________________↑
```

#### 4.1.2 核心流程

把四步展开成可执行命令（以「命令行应用」为例，服务型程序见下方说明）：

1. **Step 0**：在应用的最终链接里加 `-Wl,-q`（也可写成 `-Wl,--emit-relocs`）。验证方式是检查二进制里有没有 `.rela.text` 段；BOLT 处理时若检测到重定位，也会打印 `BOLT-INFO: enabling relocation mode`。
2. **Step 1（For Applications）**：在程序运行命令前直接加 `perf record -e cycles:u -j any,u -o perf.data --`，跑完典型输入，得到 `perf.data`。注意 `-j any,u` 是采「用户态分支采样（brstack/LBR）」，这是 BOLT 最想要的。
3. **Step 1（For Services）**：服务型程序通常不能「跑一次就退出」，改用全机采样固定时长，例如 `perf record -e cycles:u -j any,u -a -o perf.data -- sleep 180`（采 3 分钟）。
4. **Step 2**：`perf2bolt -p perf.data -o perf.fdata <executable>`，把分支数据聚合成 fdata。这一步同时做**符号化**：需要二进制的符号表还在（`strip -g` 可以，完全 strip 不行）。
5. **Step 3**：`llvm-bolt <executable> -o <executable>.bolt -data=perf.fdata -reorder-blocks=ext-tsp -reorder-functions=... -split-functions -split-all-cold -split-eh -dyno-stats`，得到优化后的二进制。

> **服务型 vs 应用型**：区别只在 Step 1 的 `perf` 命令写法（`-a -- sleep N` vs 直接跟命令）。Step 2/3 完全一样。
>
> **采样量建议**：官方建议 profile 覆盖约 **10 亿条指令**（由 `-dyno-stats` 报告）。不够就延长 `sleep` 或调高 `perf -F<N>` 频率。
>
> **首选 cycles 事件**：官方经验上推荐用 `cycles` 事件而不是 `BR_INST_RETIRED.*`，实测效果更好。

#### 4.1.3 源码精读

README 的 `Usage` 章节就是这条流程的权威定义。Step 0 讲清了为什么需要重定位：

[bolt/README.md:118-124](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/README.md#L118-L124) —— Step 0：在最终链接加 `--emit-relocs`，并指出可用 `.rela.text` 段验证重定位是否保留；BOLT 处理时也会自动报告是否检测到重定位。

Step 1 把命令行应用和服务型程序的采集方式分开（注意服务型用的是全机 + 定时）：

[bolt/README.md:140-142](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/README.md#L140-L142) —— 命令行应用：直接在调用前加 `perf record -e cycles:u -j any,u -o perf.data -- <executable> <args>`。

[bolt/README.md:151-153](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/README.md#L151-L153) —— 服务型程序：用 `perf record -e cycles:u -j any,u -a -o perf.data -- sleep 180` 全机采样固定时长。

Step 2 用 `perf2bolt` 聚合，并提示「无 brstack 时要加 `-ba`」：

[bolt/README.md:195-201](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/README.md#L195-L201) —— `perf2bolt -p perf.data -o perf.fdata <executable>`，把 `perf.data` 的分支数据聚合成更紧凑、对二进制改动更鲁棒的格式。

[bolt/README.md:187-188](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/README.md#L187-L188) —— 关键 NOTE：可以跳过 Step 2，用实验性 `-p perf.data` 直接把 `perf.data` 喂给 `llvm-bolt`（内部仍走 `perf2bolt` 的同一套聚合逻辑，见 [u4-l1](u4-l1-profile-formats.md)）。

Step 3 给出标准优化命令（注意 README 这里用的是 `-reorder-functions=cdsort`）：

[bolt/README.md:206-213](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/README.md#L206-L213) —— Step 3：用 fdata 跑 `llvm-bolt`，含 `-reorder-blocks=ext-tsp -reorder-functions=cdsort -split-functions -split-all-cold -split-eh -dyno-stats`，并说明可加 `-update-debug-sections` 更新调试信息（会更慢）。

> **文档不一致提醒**：同一道 Step 3 命令，三份文档给的 `-reorder-functions` 取值并不统一——README 用 `cdsort`，GettingStarted 用 `hfsort`，OptimizingClang 用 `hfsort+`。这反映这些是**不同的函数排序算法**（具体差异留到 [u6-l2](u6-l2-function-reorder.md)），任选一个都能跑通；本讲关注流程，先用 README 的 `cdsort` 即可。

GettingStarted 文档补充了一个**采集质量**的硬性经验值：

[bolt/docs/GettingStarted.md:133-139](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/docs/GettingStarted.md#L133-L139) —— 建议 profile 覆盖约 1B 条指令（由 `-dyno-stats` 报告）；不够可延长 `sleep` 或用 `perf -F<N>` 提频；且推荐用 `cycles` 事件而非 `BR_INST_RETIRED.*`。

OptimizingClang 则给出了一条**端到端真实命令链**（以编译 Clang 为负载），可直接照搬骨架：

[bolt/docs/OptimizingClang.md:54-54](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/docs/OptimizingClang.md#L54-L54) —— Step 1：`perf record -e cycles:u -j any,u -- ninja clang`（负载就是「用 ninja 编译 clang」）。

[bolt/docs/OptimizingClang.md:61-61](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/docs/OptimizingClang.md#L61-L61) —— Step 2：`perf2bolt $CPATH/clang-7 -p perf.data -o clang-7.fdata -w clang-7.yaml`，注意传给 perf2bolt 的是 `clang` 真正指向的那个二进制（而非符号链接），并用 `-w` 额外输出一份 YAML。

[bolt/docs/OptimizingClang.md:67-70](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/docs/OptimizingClang.md#L67-L70) —— Step 3：`llvm-bolt clang-7 -o clang-7.bolt -b clang-7.yaml -reorder-blocks=ext-tsp -reorder-functions=hfsort+ -split-functions -split-all-cold -dyno-stats -icf=1 -use-gnu-stack`，这里用 `-b` 读 YAML（与 `-data` 读 fdata 等价）。

#### 4.1.4 代码实践

**实践目标**：用源码阅读的方式，把「四步流程」与文档里的具体行号对上号，建立「哪一步看哪段文档」的索引。

**操作步骤**：

1. 打开 `bolt/README.md`，依次定位 Step 0（约 118 行）、Step 1（约 126 行）、Step 2（约 185 行）、Step 3（约 206 行）四个小标题。
2. 对每一步，用一句话总结「它的输入是什么、输出是什么」。例如：Step 1 输入是「带 `-q` 的 ELF + 真实负载」，输出是 `perf.data`。
3. 对照 `bolt/docs/GettingStarted.md` 的同名四步，找出**两份文档唯一实质不同的命令**——Step 3 的 `-reorder-functions` 取值（README=cdsort，GettingStarted=hfsort）。

**需要观察的现象**：两份文档结构几乎逐行一致（显然是同源的），只在算法取值上有差异；这说明 `cdsort`/`hfsort` 是可互换的算法选项，而非硬性正确/错误之分。

**预期结果**：你能在不看本讲的情况下，凭 README 的四个 `### Step` 小标题复述整条流程。

#### 4.1.5 小练习与答案

**练习 1**：为什么 Step 0 的 `--emit-relocs` 是「加在链接阶段」而不是「加在编译阶段」？

> **参考答案**：因为重定位是**链接器**在拼装最终 ELF 时生成的元数据。编译器只产出 `.o`（每个都自带重定位），而普通链接会把它们解析掉、不写进最终二进制。`--emit-relocs` 是让**链接器**把这些重定位也保留到输出 ELF 的 `.rela.text` 里，所以它必须是链接阶段的选项（`-Wl,-q` 即把 `-q` 传给链接器）。

**练习 2**：服务型程序为什么要用 `-- sleep 180` 而不是直接跟一个程序名？

> **参考答案**：服务型程序常驻不退出，无法像命令行应用那样「跑完即停」。`perf record ... -- sleep 180` 让 `perf` 采满 180 秒后由 `sleep` 退出而结束采集，是一种「定时全机采样」的惯用写法（配合 `-a`）。

### 4.2 关键优化选项：reorder-blocks、reorder-functions、split-functions 与 -dyno-stats

#### 4.2.1 概念说明

Step 3 的命令里挂了一串 `-xxx` 选项，初看眼花。其实它们分两类：**做优化的**和**做观测的**。

**做优化的三大主力**（对应 BOLT 的三大收益来源）：

- `-reorder-blocks=ext-tsp`：在**函数内部**重排基本块，把热的、顺序执行的路径排到一起，让 CPU 尽量 fall-through（顺序取指），减少跳转。`ext-tsp` 是默认算法（扩展旅行商模型）。
- `-reorder-functions=...`：在**函数之间**重排，把互相调用频繁的热函数聚到一起（基于调用图聚簇），减少 i-cache/iTLB miss。
- `-split-functions`（配合 `-split-all-cold`）：把每个函数里的**冷基本块**拆到单独的 `.text.cold` 段，让热代码更紧凑地占用缓存。

**做观测的**：

- `-dyno-stats`：基于 profile 打印一份「动态指令统计」（执行了多少分支、多少跳转等），并给出优化前后的**百分比变化**。这是**不用真跑 benchmark 就能快速评估收益**的关键选项。

#### 4.2.2 核心流程

这三类优化在 BOLT 内部各自是一个 **pass**（优化阶段），Step 3 的命令行选项只是「开关」。大致顺序是：

```text
读 profile → 重建 CFG → reorder-blocks(块级) → split-functions(热冷分) → reorder-functions(函数级) → 重新发射 → 写 ELF
```

它们的算法细节（Ext-TSP 评分、HFSort/CDSort 聚簇、热冷阈值）分别属于进阶内容，留到 [u6](u6-l1-block-reorder-ext-tsp.md) 单元细讲。本讲只需建立「选项 ↔ 优化」的对应关系，并能读懂 `-dyno-stats` 输出。

#### 4.2.3 源码精读

README Step 3 的命令就是这三类选项的标准组合：

[bolt/README.md:211-213](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/README.md#L211-L213) —— 标准优化命令：`-reorder-blocks=ext-tsp -reorder-functions=cdsort -split-functions -split-all-cold -split-eh -dyno-stats`，并提示若需更新调试信息再加 `-update-debug-sections`（耗时会略增）。

OptimizingClang 给出了**真实的 `-dyno-stats` 输出**，这是本讲理解收益的最直观材料。先看 BOLT 启动时的两行关键 INFO（profile 覆盖与 ICF 折叠）：

[bolt/docs/OptimizingClang.md:74-75](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/docs/OptimizingClang.md#L74-L75) —— `enabling relocation mode`（检测到 Step 0 的 `-q`）+ `11415 functions out of 104526 simple functions (10.9%) have non-empty execution profile`（只有约一成函数有 profile 命中，这是大型程序常态）。

然后是动态统计本体——**括号里的百分比就是「优化前 → 优化后」的相对变化**：

[bolt/docs/OptimizingClang.md:81-97](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/docs/OptimizingClang.md#L81-L97) —— `-dyno-stats` 输出：冒号左是优化后的绝对计数，括号是相对原布局的变化。例如 `taken forward branches (-57.2%)` 表示「向前跳转且被采纳的分支」减少了 57.2%；`taken conditional branches (-43.4%)` 表示被采纳的条件分支减少 43.4%——这正是 BOLT 把代码「捋直」的直接证据。

读懂这张表的关键规则：

| 输出形式 | 含义 |
| --- | --- |
| `N : executed X branches (-K%)` | 优化后这类事件发生 `N` 次，比优化前**少** `K%`（负号=下降，通常是好事：跳转更少） |
| `N : ... (+K%)` | 比优化前**多** `K%`（如 `non-taken conditional branches (+12.6%)`，意味着更多条件分支被「顺序执行」而非跳转，是布局变好的副产物） |
| `N : ... (=)` | 优化前后**不变**（如 `all function calls (=)`：BOLT 不改调用次数，只改布局） |

官方对这张表的解读也点明了「BOLT 在 PGO/LTO 之上仍能捋顺代码」：

[bolt/docs/OptimizingClang.md:100-102](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/docs/OptimizingClang.md#L100-L102) —— 说明该统计基于 `cycles` 计数器采的 brstack（LBR）profile，精度受计数器影响，但 `taken conditional branches` 的相对改善仍能说明 BOLT 即便在 PGO 之后也能进一步捋直代码。

而把「i-cache miss 改善」量化成 MPKI 的对照，则来自同一篇文档的「Source of the Wins」一节：优化前约 22 misses/1000 指令，优化后降到约 15，经验阈值是 10。

#### 4.2.4 代码实践

**实践目标**：学会读 `-dyno-stats` 的百分比，并能用一个简单比例估算「优化前」的值。

**操作步骤**：

1. 读 [OptimizingClang.md:81-97](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/docs/OptimizingClang.md#L81-L97) 的统计表。
2. 选定 `taken conditional branches (-43.4%)` 这一行，已知优化后为 `100642104`，估算优化前的值。
3. 由「优化后 = 优化前 × (1 - 43.4%)」反推：优化前 ≈ `100642104 / (1 - 0.434)` ≈ 1.78 亿。

**需要观察的现象**：负号（减少）的多发生在「taken（被采纳的跳转）」「unconditional branches（无条件跳转）」上；而 `non-taken conditional branches` 反而 `+12.6%`（增加）。这正说明 BOLT 让更多条件分支走向了「顺序 fall-through」而非跳转——布局变紧凑的直接表现。

**预期结果**：你能不看参考答案，独立解释「为什么 `taken branches` 大幅下降是好事」。**待本地验证**：本机跑一遍才能得到你自己程序的精确百分比（不同 profile/程序差异很大）。

#### 4.2.5 小练习与答案

**练习 1**：`-split-functions` 和 `-reorder-functions` 都带「functions」，它们改的是同一件事吗？

> **参考答案**：不是。`-split-functions` 改的是**函数内部**——把一个函数的冷块拆到 `.text.cold`；`-reorder-functions` 改的是**函数之间**——调整多个函数在 `.text` 里的先后顺序。两者作用层级不同，常配合使用。

**练习 2**：为什么 `-dyno-stats` 里 `executed instructions (-0.6%)` 几乎没变，而 `taken branches (-41.1%)` 却大幅下降？

> **参考答案**：因为 BOLT 默认只做**布局优化**，不改算法、不删指令逻辑，所以执行的指令总数几乎不变。布局变好后，许多原本需要「跳转」的地方变成了「顺序执行（fall-through）」，于是被采纳的跳转（taken branches）大幅减少——这正是布局变优的信号，而不是程序变短了。

### 4.3 插桩（-instrument）：无 perf/LBR 时的替代采集方式

#### 4.3.1 概念说明

Step 1 依赖 CPU 的分支采样硬件（x86 LBR / AArch64 BRBE）。但有两类环境拿不到它：

- **虚拟机 / 云环境**：Hypervisor 常不暴露或禁用 LBR，`perf -j any,u` 采不到分支栈。
- **不支持的老硬件**：没有分支采样单元。

这时 BOLT 提供了**第二条采集路径——插桩（instrumentation）**：先用 BOLT 给二进制**插上计数探针**，得到一个「插桩版」二进制；运行它做负载，探针会把每个分支的执行次数写到一个文件；再用这个文件当 profile 跑 Step 3。

它和采样方式的根本区别：

| 维度 | 采样（perf + perf2bolt） | 插桩（-instrument） |
| --- | --- | --- |
| 精度 | 统计采样，有噪声 | 精确计数（无采样误差） |
| 运行开销 | 几乎为零（硬件辅助） | 较高（每分支都计数） |
| 适用环境 | 需 LBR/BRBE 硬件 | 任何环境（含 VM） |
| 产出位置 | `perf.data` → `perf.fdata` | 直接写 `/tmp/prof.fdata` |

#### 4.3.2 核心流程

插桩路径把四步压缩成三步（**跳过 Step 2**）：

```text
  ELF (+-q)  ──① llvm-bolt -instrument──▶  插桩版 ELF
                                          │ 跑负载
                                          ▼
                                  /tmp/prof.fdata  ──② llvm-bolt (Step 3)──▶  优化后 ELF
```

1. **① 插桩**：`llvm-bolt <executable> -instrument -o <instrumented-executable>`，BOLT 在每个基本块/分支插入计数代码，生成插桩版二进制。
2. **跑负载**：用插桩版跑典型工作负载，运行结束时计数被写到 `/tmp/prof.fdata`（固定路径）。
3. **② 优化**：把 `/tmp/prof.fdata` 当作 `-data` 喂给 `llvm-bolt`，跑和采样路径**完全相同**的 Step 3 命令。

因为插桩路径**直接产出 fdata**，所以不需要 `perf2bolt` 这一转换步。

#### 4.3.3 源码精读

README 在 Step 1 里专门分出「With instrumentation」小节：

[bolt/README.md:170-180](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/README.md#L170-L180) —— 用 `llvm-bolt <executable> -instrument -o <instrumented-executable>` 生成插桩版，跑负载后 profile 自动落在 `/tmp/prof.fdata`，并提示可**跳过 Step 2**；用 `-help` 的 "BOLT instrumentation options" 类别可查插桩相关开关。

README 还点明了「没有分支采样时只能退而求其次」：

[bolt/README.md:165-168](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/README.md#L165-L168) —— 若采不到带分支栈的 profile（VM 或硬件不支持），只能用纯采样事件（如 cycles），profile 质量会下降、BOLT 收益变小——这正是插桩路径要补救的场景。

插桩的「探针代码」是 BOLT 通过**运行时库（runtime library）**注入的，相关源码在 `bolt/lib/RuntimeLibs/InstrumentationRuntimeLibrary.cpp` 和 `bolt/runtime/instr.cpp`，其 freestanding 约束留到 [u8-l2](u8-l2-runtime-and-instrumentation.md) 细讲。

> **小提醒**：插桩得到的 `/tmp/prof.fdata` 与 `perf2bolt` 产出的 fdata **格式兼容**，所以 Step 3 的命令对两条路径完全一样——这正是 BOLT 把 profile 抽象成统一格式的好处（格式选型机制见 [u4-l1](u4-l1-profile-formats.md)）。

#### 4.3.4 代码实践

**实践目标**：写出插桩路径的完整命令序列，并对比它与采样路径在「步数」上的差异。

**操作步骤**：

1. 参照 [README.md:174-176](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/README.md#L174-L176)，写出插桩命令：`llvm-bolt ./hello -instrument -o ./hello.instr`。
2. 运行插桩版做负载：`./hello.instr`，结束后检查 `/tmp/prof.fdata` 是否生成。
3. 跑 Step 3（与采样路径相同）：`llvm-bolt ./hello -o ./hello.bolt -data=/tmp/prof.fdata -reorder-blocks=ext-tsp -split-functions -dyno-stats`。
4. 数一下步数：插桩路径是「插桩 → 跑 → 优化」共 3 步；采样路径是「链接 → 采集 → 转换 → 优化」共 4 步。

**需要观察的现象**：插桩版运行会比原版**明显变慢**（因为每个分支都在计数），这是精确性换来的开销。

**预期结果**：两条路径最终都产出 `hello.bolt`，且 Step 3 命令一致。**待本地验证**：插桩版的具体减速比因程序而异。

#### 4.3.5 小练习与答案

**练习 1**：插桩路径为什么可以「跳过 Step 2」？

> **参考答案**：因为 Step 2（`perf2bolt`）的作用是「把庞大的 `perf.data` 聚合成 fdata」。插桩探针是 BOLT 自己的代码，运行时**直接按 fdata 格式写** `/tmp/prof.fdata`，产物已经是 Step 3 能读的格式，无需再转换。

**练习 2**：既然插桩比采样更精确，为什么不总是用插桩？

> **参考答案**：因为插桩会显著拖慢被测程序（每条分支都计数），对长时间运行的服务/大规模负载不现实；而采样靠硬件、几乎零开销。生产环境通常优先用采样，只有在采不到分支栈（VM 等）或需要精确小负载 profile 时才用插桩。

## 5. 综合实践

**任务**：以一个最简单的 hello world 程序为例，把本讲三模块（四步流程、优化选项、插桩替代）串成两条完整命令链，并用 `-dyno-stats` 做「优化前后」对比。

**准备**（示例代码，非项目原有代码）：

```c
// hello.c —— 示例代码
#include <stdio.h>
int main(int argc, char **argv) {
  for (int i = 0; i < 1000000; ++i)   // 一个有循环的热函数，便于看到 profile 效果
    printf("hello %d\n", i);
  return 0;
}
```

**路径 A：标准采样四步流程**

```bash
# Step 0: 编译并加 -Wl,-q 保留重定位（-Wl,-q 把 -q 传给链接器）
clang -O2 -Wl,-q hello.c -o hello
# 自检：能看到 .rela.text 段
readelf -S hello | grep -E 'rela.text|\.text '

# Step 1: 采分支采样（需要 LBR；VM 上可能失败，失败就改用路径 B）
perf record -e cycles:u -j any,u -o perf.data -- ./hello > /dev/null

# Step 2: 聚合成 fdata（二进制符号表需保留）
perf2bolt -p perf.data -o perf.fdata ./hello

# Step 3a (基线观察): 只读 profile、不重排，看 profile 覆盖
llvm-bolt ./hello -o /dev/null -data=perf.fdata -dyno-stats
# Step 3b (优化): 真正重排，看 -dyno-stats 的百分比变化
llvm-bolt ./hello -o hello.bolt -data=perf.fdata \
    -reorder-blocks=ext-tsp -reorder-functions=cdsort \
    -split-functions -split-all-cold -split-eh -dyno-stats
```

**路径 B：无 LBR 时改用插桩（替代 Step 1+2）**

```bash
# 编译同上（仍需 -Wl,-q）
clang -O2 -Wl,-q hello.c -o hello

# 插桩
llvm-bolt ./hello -instrument -o hello.instr
# 跑负载，探针自动写 /tmp/prof.fdata
./hello.instr > /dev/null

# Step 3（与路径 A 完全相同，只是换数据源）
llvm-bolt ./hello -o hello.bolt -data=/tmp/prof.fdata \
    -reorder-blocks=ext-tsp -split-functions -dyno-stats
```

**对比优化前后（核心）**：

- 看 Step 3a（基线）输出的 `BOLT-INFO: N functions ... have non-empty execution profile`，确认 profile 是否覆盖到 `main`。
- 看 Step 3b（优化）输出里 `taken conditional branches`、`taken branches` 行括号内的百分比：若为负且绝对值较大（如 `(-30%~50%)`），说明布局被捋直了。
- 若本机有 `perf stat`，可对比优化前后的 `L1-icache-misses`，套用 4.2 节的 MPKI 公式量化收益。

**注意事项**：

1. `perf record -j any,u` 在纯虚拟机/无 LBR 环境会采不到分支栈，这时 `perf2bolt` 会报错或产出空 profile——**直接切到路径 B**。
2. hello world 太小可能看不出收益，可把循环次数调大或换更大的程序。
3. 以上命令的真实数值**待本地验证**，本讲不预设具体百分比。

## 6. 本讲小结

- BOLT 的使用是一条**四步流水线**：Step 0 链接加 `-Wl,-q`、Step 1 `perf record -j any,u` 采集、Step 2 `perf2bolt` 转成 fdata、Step 3 `llvm-bolt -reorder-*` 优化。
- Step 0 的 `--emit-relocs` 是为了保留 `.rela.text` 重定位元数据，没有它 BOLT 就不能在**函数之间**重排。
- Step 1 分**应用型**（直接跟命令）和**服务型**（`-a -- sleep N` 全机定时采样）两种写法；profile 质量建议覆盖约 1B 条指令，首选 `cycles` 事件。
- Step 2 可用实验性 `-p perf.data` 直接喂给 `llvm-bolt` 来跳过，内部仍是同一套聚合逻辑。
- Step 3 的三大主力选项是 `-reorder-blocks=ext-tsp`（块级）、`-reorder-functions=...`（函数级，cdsort/hfsort 三份文档取值不一）、`-split-functions`（热冷分裂）；`-dyno-stats` 的括号百分比就是「优化前→优化后」的相对变化。
- 无 LBR/VM 环境改用 `-instrument` 插桩：插桩版跑完直接产出 `/tmp/prof.fdata`，**跳过 Step 2**，Step 3 命令不变。

## 7. 下一步学习建议

- 想知道 `llvm-bolt` 内部**到底按什么顺序执行**这些优化阶段，进入 [u3-l1 RewriteInstance::run() 全流程总览](u3-l1-run-pipeline.md)，它会带你通读主链路的阶段序列。
- 想深入「profile 有哪几种格式、BOLT 如何按文件类型自动选 reader」，看 [u4-l1 Profile 格式总览与 fdata/DataReader](u4-l1-profile-formats.md)；想看 `perf2bolt` 如何解析 `perf.data`，看 [u4-l2 perf2bolt 与 DataAggregator](u4-l2-perf2bolt-aggregator.md)。
- 想弄懂 `ext-tsp`、`cdsort`/`hfsort` 这些算法背后的原理，留到 [u6 核心优化 pass](u6-l1-block-reorder-ext-tsp.md) 单元。
- 若你对「程序入口、目录结构」更感兴趣而非流程，可先读 [u1-l4 代码目录结构与程序入口](u1-l4-directory-and-entry.md)，了解 `lib/Core`、`lib/Passes`、`lib/Profile` 等目录的职责。
