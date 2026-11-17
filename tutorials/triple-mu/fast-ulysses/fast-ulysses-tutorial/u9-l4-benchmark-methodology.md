# 基准方法论：stages/overlap/zerosm 与独占执行

## 1. 本讲目标

学完本讲，你应该能够：

1. 对 `bench_a2a.py` 的七种模式（stages / overlap / padding / zerocopy / sweep / link / zerosm）各写出一句话的问题定义，并说出各自的判读方法。
2. 解释两个归一化指标的确切含义：`hidden%`（异步调用藏进计算的比例）与 `of the fabric`（传输达到裸 fabric 天花板的比例）。
3. 解释为什么「barrier 列是 floor（地板），不是待优化项」。
4. 复述 `tools/exclusive.sh` 的三段式工作方式与 `EXCLUSIVE` / `CONTENDED` 两种判定的语义，说明为什么共享 GPU 上得到的数字不是数字。
5. 说明 `benchmark/collect.sh` 为什么要在任何数字之前先记录环境指纹（机器、commit、驱动、时钟、拓扑）。

本讲是「测量的方法」课，不是「新机制」课：u5 已经讲清了零 SM 的物理来源，本讲讲的是**如何用代码把这些性质变成可信的数字**。

## 2. 前置知识

- **CUDA 事件计时**：`torch.cuda.Event(enable_timing=True)` 在流上打时间戳，`elapsed_time` 给出两个戳之间的毫秒数。打一个戳只花主机时间、不占设备时间——这是「分段测量之和等于整体」的前提。
- **中位数而非均值**：GPU 计时的分布有长尾（偶发的调度抖动、时钟升降），均值会被尾巴拖走，中位数不会。本基准的所有数字都是中位数。
- **拷贝引擎（CE）与 SM 的分工**（u5-l1/u5-l2）：跨 NVLink 的远端拷贝由拷贝引擎执行、不占 SM；同设备拷贝与计算竞争同一套内存系统。
- **一次调用的五段流水线**（u6-l2）：prepare 校验 → 开路屏障 → CE 传输 → 收尾屏障 → copy_out。本讲的 stages 分解就是给这四段计时。
- **集合通信由最慢的 rank 定节奏**：屏障要把所有 rank 汇齐，一个迟到的 rank 会把所有人的 `barr_in` 都拉长——这既是 sweep 模式「barrier 是 floor」的原因，也是共享 GPU 破坏测量结果的机制。
- **torchrun / rank / world_size**：基准用 `torchrun --nproc_per_node=N` 启动 N 个进程，每进程绑一张卡。
- **nvidia-smi**：可以查询每张 GPU 上正在运行的进程（`--query-compute-apps`）和当前 SM 时钟（`--query-gpu=clocks.sm`）——`exclusive.sh` 的巡检就用这两个查询。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| `benchmark/bench_a2a.py` | 基准主程序：七种模式、计时基建（Stopwatch / median / submit_us）、每种模式的测量与打印 |
| `tools/exclusive.sh` | 独占执行守卫：先等 GPU 空闲、运行期间持续采样、最后给出 EXCLUSIVE / CONTENDED 判定 |
| `benchmark/collect.sh` | 一台机器的完整测量编排：环境指纹 + 正确性门 + 按序跑全部模式 |
| `docs/benchmark.md` | 已发表的数字与完整方法论声明（机器、二进制、节点一致性） |
| `src/bindings.cc` | `all_to_all_4d_timed` 算子：stages 模式的计时来源 |
| `python/fast_ulysses/group.py` | `_timed()`：把四个阶段时长包成带名字的字典 |
| `docs/design.md` | zerosm A/B 的设计论证（本讲只引用、已在 u5-l2 详述） |

## 4. 核心概念与源码讲解

### 4.1 stages 分解：让各阶段相加等于整次调用

#### 4.1.1 概念说明

一个微基准只有能**归因**（attribution）才有价值：「整次调用快了 1.8×」是一个数字，「基线把 47% 的时间花在本库根本没有的重排上」才是一个论证。stages 分解把两条路径各自拆成相加等于整体的阶段，于是列与列之间可以同口径对比，而不是只能相关。

七种模式各自回答的问题，全部写在模块 docstring 里：

| 模式 | 一句话问题 | 判读方法 |
|---|---|---|
| `stages` | 两条路径的时间各去了哪，相加等于整次调用 | `a2a` 对 `transfer` 是同口径对；`relayout%`、`copyout%` 是归因列 |
| `overlap` | 异步调用能藏多少进并发的 GEMM 链 | `hidden%`，但分布有真实散布，读作区间 |
| `padding` | 不均分路径是否与均分同价 | `ours cost ≈ 1.00×` 则丢弃 padding 在这里是免费的 |
| `zerocopy` | `out=`（来自 `empty_output()`）值多少 | `saved` 应约等于 stages 模式量出的 `copy_out` 阶段 |
| `sweep` | 各阶段如何随消息尺寸变化 | barrier 份额是一条曲线；读作 floor，不是待优化项 |
| `link` | fabric 裸拷贝的天花板是多少 | `transfer` 对照单流天花板即得利用率 |
| `zerosm` | 一次拷贝占不占 SM | 两列联读（见 4.3），单独任何一列都不足为凭 |

#### 4.1.2 核心流程

计时的核心不变量是：**事件按严格顺序排在同一条流上，所以分段之和等于整段**。stages 模式对两条路径分别这么做：

1. 基线侧（`baseline_stages`）：`permute → all_to_all_single → permute` 三段，四个事件戳出三个阶段。
2. 本库侧（`all_to_all_4d_timed` 算子）：`开路屏障 → CE 传输 → 收尾屏障 → copy_out` 四段，五个事件戳出四个阶段。
3. 另测 `raw`（裸 `all_to_all_single`，同字节、无重排、结果布局是错的）作为**传输地板**——它是下界参照而不是备选方案。
4. 每个数字都是多次迭代的中位数；逐阶段分别取中位数（`median_of`）。

#### 4.1.3 源码精读

**Stopwatch：分段计时器**。[benchmark/bench_a2a.py:84-98](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/benchmark/bench_a2a.py#L84-L98) 定义了一个「预先建好 N 个事件、依次打戳、最后一次性读出相邻差值」的计时器。它的 docstring 说出关键性质：打戳只花主机时间、不花设备时间，所以**分了段的运行和不分的运行测的是同一个总时间**——分段不引入测量偏置。

```python
class Stopwatch:
    """CUDA events between stages, read once at the end. A mark costs host time and no device
    time, so a subdivided run measures the same total as an undivided one."""
```

**逐阶段中位数**。[benchmark/bench_a2a.py:101-107](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/benchmark/bench_a2a.py#L101-L107) 的 `median_of` 对函数返回的**每个下标分别**取多次运行的中位数，返回一条阶段中位数向量。注意 [benchmark/bench_a2a.py:110-123](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/benchmark/bench_a2a.py#L110-L123) 的 `median_ms` 在计时循环前有一次 `dist.barrier()`——先把各 rank 对齐，避免上一次迭代的到达偏斜漏进下一次的计时窗口。

**计时来自库内一个专用算子**。stages 模式量本库路径用的是 `group._timed(x, mode=0)`，它转发到 C++ 的 `all_to_all_4d_timed`：[src/bindings.cc:624-656](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L624-L656)。五个事件把四段夹开，注释（[src/bindings.cc:621-623](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L621-L623)）说明了一个容易误读的细节：`transfer` 阶段**包含本 rank 自份额**——自份额走调用者流、在远端拷贝下面运行，无法单独计时，所以被算进 transfer。Python 侧把四个数包成带名字的字典：[python/fast_ulysses/group.py:251-265](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/python/fast_ulysses/group.py#L251-L265)。

```python
names = ("barrier_in", "transfer", "barrier_out", "copy_out")
return out, dict(zip(names, (float(v) for v in stages), strict=True))
```

**run_stages 的测量与三列派生指标**。[benchmark/bench_a2a.py:197-213](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/benchmark/bench_a2a.py#L197-L213) 依次量基线三段、本库四段、`raw` 和提交耗时，然后打印：

- `raw/CE = raw / transfer`：裸 `all_to_all_single` 比本库的 CE 传输慢多少倍（基线传输不如本库传输的直接证据）；
- `relayout% = (perm_in + perm_out) / BASE`：基线花在重排上的份额——本库一侧这一列根本不存在；
- `copyout% = copy_out / OURS`：本库 copying 路径花在拷出上的份额——zerocopy 模式要消除的对象。

**submit_us：主机侧提交耗时**。[benchmark/bench_a2a.py:126-137](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/benchmark/bench_a2a.py#L126-L137) 用 `time.perf_counter` 量**发起**一次调用花的主机时间。docstring 点明它的用途：批折叠（u3-l1 的 `push_batched`）想把什么保持不涨，量的就是这个。

**四个派生模式是 stages 思想的变体**：

- **zerocopy**（[benchmark/bench_a2a.py:327-364](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/benchmark/bench_a2a.py#L327-L364)）：对同一输入量 `copying`（默认路径）与 `zerocopy`（`out=` 来自 `empty_output()` 的缓冲，[benchmark/bench_a2a.py:350-353](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/benchmark/bench_a2a.py#L350-L353)）两种整调用时间，再用 `_timed` 量出 `copy_out` 阶段做归因——`saved` 应当约等于那个阶段。这是**交叉验证**：两种独立测量互相印证省的确实是 copy-out 而不是别的。
- **sweep**（[benchmark/bench_a2a.py:367-399](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/benchmark/bench_a2a.py#L367-L399)）：固定头数与 d，扫 `s_local`，输出四阶段对消息尺寸的曲线。跨链路字节数的算法在 [benchmark/bench_a2a.py:391-392](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/benchmark/bench_a2a.py#L391-L392)：

  \[ \text{crossed MB} = \text{MB/rank} \times \frac{ws - 1}{ws} \]

  即「除本 rank 自份额外、真正过链路的字节」，GB/s 一列就是用它除以 transfer 时间。
- **link**（[benchmark/bench_a2a.py:402-443](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/benchmark/bench_a2a.py#L402-L443)）：绕开本算子，直接用 torch 对称内存做平坦 64 MiB peer 拷贝，量单流与全流两种带宽——**天花板必须独立于被评判的对象建立**（docstring 原话）。结尾的打印（[benchmark/bench_a2a.py:442-443](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/benchmark/bench_a2a.py#L442-L443)）明说了用法：stages 的 `transfer` 按跨链路字节折算后对照这里的单流数字，就是「用了多少 fabric」。
- **padding**（[benchmark/bench_a2a.py:262-306](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/benchmark/bench_a2a.py#L262-L306)）：对基线与本库各量「补齐到 ws 倍数」与「每 rank 差一个 token 的不均分」两种输入。注意 [benchmark/bench_a2a.py:309-324](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/benchmark/bench_a2a.py#L309-L242) 的 `_baseline_unpadded` 是**重写的基线**（带 split sizes、逐 peer reshape、`cat`），而不是拿 `dist.all_to_all` 列表版凑数——docstring 说得直白：拿另一个基线对比就是在量两个不同的基线，不是在量 padding 的代价。

**docstring 的七模式自述**。[benchmark/bench_a2a.py:10-53](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/benchmark/bench_a2a.py#L10-L53) 是每个模式的权威一句话定义。其中 stages 一节（L10-16）给出了最重要的对比关系：`raw` 是传输地板而非备选；`a2a` 对 `transfer` 是同口径对；「其余各列是每条路径围绕它必须做的事，而**这整个论证就建立在这里**——基线的重排是要与本库藏进去的计算抢 SM 的 SM 工作」。

#### 4.1.4 代码实践

**实践目标**：为 stages 模式的输出写一列一句的判读模板，把「每列回答什么问题」固化成你自己的速查表。

**操作步骤**：

1. 精读 [benchmark/bench_a2a.py:168-214](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/benchmark/bench_a2a.py#L168-L214)（`run_stages` 全函数），对照表头（L175-181）确认每列由哪个表达式产生。
2. 对照 [docs/benchmark.md:62-87](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/benchmark.md#L62-L87) 的已发表数字，检验你的模板能否逐列解释这些数。
3. 写出模板（见下面「预期结果」的参考版）。

**需要观察的现象**：表头列名与打印表达式的一一对应；`BASE` 与 `OURS` 各自等于其分段之和。

**预期结果**（参考模板）：

| 列 | 回答的问题 | 怎么读 |
|---|---|---|
| `perm_in` / `perm_out` | 基线为进/出集合通信各付一次重排多少 | 本库一侧没有对应列——这就是 `relayout%` 的来源 |
| `a2a` | 基线的集合通信本体多少 | 与 `transfer` 构成同口径对 |
| `BASE` | 基线整调用多少 | 三段之和 |
| `barr_in` / `barr_out` | 两道屏障各多少 | 大部分是 rank 到达偏斜；是 floor 不是待优化项 |
| `transfer` | 本库 CE 传输（含自份额）多少 | 与 `a2a` 对比；按跨链路字节折算后对照 link 天花板 |
| `copy_out` | copying 路径的拷出多少 | `out=` 可消除的对象；对照 zerocopy 模式的 `saved` |
| `OURS` | 本库整调用多少 | 四段之和；`vs BASE` 即加速比 |
| `raw` | 裸 `all_to_all_single`（同字节、错布局）多少 | 传输地板，不是备选 |
| `raw/CE` | 地板比本库传输慢几倍 | >1 说明带步长 CE 传输胜过平坦 NCCL 路径 |
| `relayout%` | 基线时间中重排占几成 | 本库为 0，故这是纯增益来源 |
| `copyout%` | 本库时间中拷出占几成 | zerocopy 的收益上限 |
| `submit us` | 主机提交一次调用多少微秒 | 批折叠想压平的量 |

（写好模板后，若有 ≥2 GPU，可运行 `./tools/exclusive.sh 0,1 -- torchrun --nproc_per_node=2 benchmark/bench_a2a.py --mode stages` 对照真实输出；单卡亦可 `--nproc_per_node=1` 跑通流程。实际数字：待本地验证。）

#### 4.1.5 小练习与答案

**练习 1**：为什么 `raw` 被称为「传输地板」而不是「第三条路径」？如果拿 `raw` 当对比对象会犯什么错？

答案：`raw` 是 `all_to_all_single(recv, send)`——同样字节在传输，但结果布局是错的（没有重排），不能作为可用路径。它的作用是给「纯传输最少要多少」立一个下界：本库的 `transfer` 若比 `raw` 还快（`raw/CE > 1`），说明带步长拷贝不仅没比重排+平坦传输贵，反而更便宜。拿它当对比对象会高估本库优势，因为基线必须付重排才能交付正确布局的输出。

**练习 2**：`median_of` 为什么对每个下标分别取中位数，而不是先对整条向量取均值再分段？

答案：分布的长尾对不同阶段不成比例——屏障阶段偶发的到达偏斜可能把某一次运行的 `barr_in` 拉高一个量级而其他阶段不变。逐下标取中位数让每个阶段各自抵抗自己的尾巴；对整条向量取均值会把一个阶段的尾巴扩散进所有阶段的数字里。

**练习 3**：sweep 模式里 `crossed` 为什么要乘 `(ws-1)/ws`？

答案：每 rank 发出的字节中有 `1/ws` 是自己的份额、根本不过链路（自份额走调用者流的同设备拷贝，u5-l1）。用总 MB 折算带宽会低估真实过链路的带宽；`(ws-1)/ws` 恰好扣除自份额，得到的 GB/s 才能与 link 模式的单流天花板同口径对比。

### 4.2 overlap 隐藏率：hidden% 的定义与陷阱

#### 4.2.1 概念说明

`hidden%` 回答「异步调用有多少时间消失在并发计算下面」。这是零 SM 设计存在的**目的**：u5-l2 讲过，远端拷贝不占 SM，所以它可以与满载 GEMM 完全重叠；overlap 模式就是把这个性质变成一个可测的量。

定义：

\[ \mathrm{hidden\%} = \frac{t_{\mathrm{serial}} - t_{\mathrm{concurrent}}}{t_{\mathrm{a2a,alone}}} \times 100\% \]

其中 `serial` 是「先同步 a2a 再跑 3-GEMM 链」的墙钟，`concurrent` 是「发起 `all_to_all_4d_async`、跑 GEMM 链、再 `wait()`」的墙钟，`a2a_alone` 是异步调用单独跑的时间。GEMM 链故意做成 `to_q/k/v` 的形状（三个同尺寸矩阵乘）。

两个陷阱：其一，这个指标**有真实散布**——docs/benchmark.md 记录同型号两个节点可差 35 个百分点，所以只能读作区间；其二，它**受可用计算量约束**：GEMM 不够长时，就算集合通信完全让路也藏不了多少（并发时间不可能低于 GEMM 自身时间）。

#### 4.2.2 核心流程

1. 量 `gemm_alone` 与 `a2a_alone` 作参照。
2. 定义两种排布：`serial`（先同步调用后 GEMM）与 `concurrent`（异步调用 + GEMM + wait）。
3. **交替**测量：8 轮，每轮各取 8 次迭代的中位数，最后取 8 轮的中位数。
4. 打印 hidden% 以及两组 8 样本的最小-最大范围（spread）。

#### 4.2.3 源码精读

**两种排布的定义**。[benchmark/bench_a2a.py:228-242](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/benchmark/bench_a2a.py#L228-L242)：`gemms()` 是三次 `a @ w`；`serial()` 先 `group.all_to_all_4d(x, mode=0)` 再 `gemms()`；`concurrent()` 发 `all_to_all_4d_async`、跑 GEMM、`h.wait()`。

**交替测量及其理由**。[benchmark/bench_a2a.py:244-252](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/benchmark/bench_a2a.py#L244-L252) 的注释是本模式的灵魂：

```python
# Alternate the two arrangements and compare medians: the GEMM window drifts run to run by
# about as much as the a2a itself, and alternating cancels the drift.
```

GEMM 的时长逐轮漂移的幅度与 a2a 本身相当——如果先测完所有 serial 再测所有 concurrent，两种排布会被安排在漂移曲线的不同位置，系统性偏差直接进 hidden%。交替排布让漂移对两种排布平等，取中位数后抵消。

**打印**。[benchmark/bench_a2a.py:253-259](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/benchmark/bench_a2a.py#L253-L259) 输出参照、两种排布的墙钟与各自的 spread、以及 hidden%。

**已发表的读法**。[docs/benchmark.md:147-165](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/benchmark.md#L147-L165)：四台机器 hidden 47%–95%，且明说「This metric has real spread… Read it as a range」；末段解释 H200 藏得更多是因为「GEMM 更长、有更多可藏的地方」，指标**受可用计算量约束，而不只取决于集合通信让不让路**。v0.1 在 A100 上甚至测出过 ~105%——读作完全隐藏：串行排布多付了一笔并发排布避免的启动开销（[docs/benchmark.md:306-308](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/benchmark.md#L306-L308)）。

#### 4.2.4 代码实践

**实践目标**：不跑代码，用已发表数字做一次 hidden% 的手工核算与边界推导。

**操作步骤**：

1. 从 [docs/benchmark.md:155-160](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/benchmark.md#L155-L160) 取 H200 行：gemm_alone = 1.793，a2a_alone = 0.876，serial = 2.971，concurrent = 2.320。
2. 代入公式核算 hidden%。
3. 推导理论上界：并发时间最少是多少？由此 hidden% 最多是多少？
4. 回答：若把 GEMM 链换成 10 倍长，hidden% 会怎么变？为什么文档说这个指标「bounded by how much compute is available」？

**需要观察的现象**：核算值与发表值一致；上界推导与「H200 藏得更多」的解释吻合。

**预期结果**：(2.971 − 2.320) / 0.876 ≈ 74.3% ≈ 发表的 74%。上界：concurrent ≥ gemm_alone（GEMM 必须跑完），故 hidden ≤ (serial − gemm_alone) / a2a_alone ≈ (a2a + gemm − gemm) / a2a = 100%；但 concurrent 还必须 ≥ 链尾未被覆盖的通信部分，所以实际低于该界。GEMM 变长后，可藏的「坑」更深，hidden% 趋向上界——这正是 H200（a2a/gemm 比为 0.49，低于 B200 的 0.55 与 B300 的 0.62）藏得更多的原因。

#### 4.2.5 小练习与答案

**练习 1**：为什么不把 `wait()` 放在 GEMM 之前测一个「假并发」做对照？

答案：那等于把异步调用变回同步调用（wait 在 GEMM 前意味着通信必须先完成），测出来就是 serial 排布本身，不提供新信息。真正的对照是 serial 与 concurrent 这一对，它们的差才是被藏掉的时间。

**练习 2**：`median_ms` 在计时前做 `dist.barrier()`（L114），如果去掉它，overlap 模式的哪种排布受害更大？

答案：concurrent。各 rank 到达时刻不齐时，先到的 rank 发起异步调用后开始跑 GEMM，后到的 rank 才让传输真正开始——到达偏斜会直接吃掉可重叠的窗口，使 concurrent 系统性变慢、hidden% 被低估。serial 排布里通信本来就独占，偏斜只影响第一轮。stages 模式的 `barr_in` 列同样会吸收这种偏斜，这也是 barrier 是 floor 的原因之一。

### 4.3 zerosm A/B：两列联读与校准

#### 4.3.1 概念说明

zerosm 模式在 u5-l2 已经从「结论侧」讲过：零 SM 仅属远端拷贝，同设备拷贝与计算竞争内存系统。本讲从**方法侧**补齐：这个结论是怎么被测出来的。

设计是一个 A/B 实验：**同样的字节、同样的 `dst.copy_(src)` 调用**，唯一变量是目的地在哪——一次写进 peer 的对称内存窗口（跨链路），一次写本 rank 自己的内存（同设备）。每条臂各自配一条与该臂拷贝等长的 GEMM 链在旁边跑，同时各自单独跑。

两条判读指标（数学定义）：

\[ \mathrm{gemm\ slowdown} = \frac{t_{\mathrm{chain,under\ copy}}}{t_{\mathrm{chain,alone}}}, \qquad \mathrm{pair\ ratio} = \frac{t_{\mathrm{pair}}}{\max(t_{\mathrm{copy}}, t_{\mathrm{gemm}})} \]

外加完全竞争参照 \(\ \text{full comp} = (t_{\mathrm{copy}} + t_{\mathrm{gemm}}) / \max(t_{\mathrm{copy}}, t_{\mathrm{gemm}})\ \)，即「一点不重叠」的墙钟比。

两列联读的判据：**1.00× 的 GEMM 慢化 + 1.00× 的成对比 = 那次拷贝没占 SM**；**1.00× 的慢化 + 成对比逼近 full comp = 拷贝被 GEMM 饿死了**（拷贝根本没在链下跑过，链自然没慢）。单看任何一列都无法区分这两种状态——这是整个模式存在的理由。

#### 4.3.2 核心流程

1. 建两条臂：peer（目的地是 rank (r+1)%ws 的窗口）与 same（本 rank 内存）；ws=1 时 peer 臂不存在（打印明示本次运行不检验该主张）。
2. **字节校准**：在最快的一条臂上量单次拷贝，取 `creps = max(1, round(max(LONG_MS, t_one_gemm)/t))`，把两臂都拉伸到至少 20 ms——20 ms 下几十微秒的启动间隙只是千分之一，这是**选定的地板**而非实测噪声（[benchmark/bench_a2a.py:75-81](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/benchmark/bench_a2a.py#L75-L81)）。rank 0 的答案广播给全组——每个 rank 必须发出相同的功。
3. **链长校准**：每条臂各自量自己的拷贝时长，`greps = round(拷贝时长 / 单个 GEMM 时长)`——同字节到 peer 与到本地差好几倍，一条链配不上两条臂。
4. 采样：ROUNDS×INNER（5×5）轮，臂交替、臂内「并发区与两个单独测量」交替；每轮 `dist.barrier()` 保证全组同臂同时。
5. 打印每臂的 copy / gemm / chain / pair / pair ratio / full comp / gemm slowdown。

#### 4.3.3 源码精读

**为什么绕开算子、直连对称内存**。[benchmark/bench_a2a.py:447-463](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/benchmark/bench_a2a.py#L447-L463) 的 docstring 给出两层理由：与 link 相同的理由（天花板/判据须独立于被评判对象），外加一条——算子的路径还含两个屏障内核和一个 copy-out，**它们本来就是 SM 工作**，整调用的测量无法把任何东西归因给传输。两条臂只差一件事：目的地在哪。

**pair()：先把链入队**。[benchmark/bench_a2a.py:498-517](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/benchmark/bench_a2a.py#L498-L517) 的并发区在调用者流排 GEMM 链、在 `cps` 流排拷贝，链后打一个戳单独读出链在并发区内的时长。注释（L500-507）解释为什么链先入队：后入队者要晚「提交前者花的主机时间」才开始；链是几次 launch、拷贝是几百次，让链后入队会把更大的裸露段留给链，把慢化拉向 1.00×。

**字节校准的注释**。[benchmark/bench_a2a.py:572-584](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/benchmark/bench_a2a.py#L572-L584)：`creps` 按最快臂校准，让最短的那条臂也至少持续 LONG_MS；两个载荷（设计形状与 64 MiB）都被拉伸——设计形状单次拷贝比一个 GEMM 还短，而链不能少于 1 个，不拉伸则两臂都越过自己的拷贝、两种待区分状态都不可读。rank 0 的答案经 `dist.broadcast` 下发。

**采样循环与漂移防御**。[benchmark/bench_a2a.py:606-620](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/benchmark/bench_a2a.py#L606-L620)：注释明说「循环前一次性测的基线会把整轮运行的漂移全灌进慢化列」；`dist.barrier()` 让同臂的每个 rank 同步进行，这正是让「全流行」那行成为全流测量的机制。

**表头把判读方法印在输出里**。[benchmark/bench_a2a.py:539-548](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/benchmark/bench_a2a.py#L539-L548) 逐行写明两列的定义与「NEITHER COLUMN IS THE RESULT ON ITS OWN」的警告——连「overlapped 是 1.00×+1.00×，starved 是 1.00×+full comp」的区分都印在每次运行的输出里。`full comp` 的计算在 [benchmark/bench_a2a.py:625-635](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/benchmark/bench_a2a.py#L625-L635)。

**ws=1 的诚实声明**。[benchmark/bench_a2a.py:549-556](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/benchmark/bench_a2a.py#L549-L556)：单卡时打印「THE PEER ARM IS NOT MEASURED and this run does not test the claim」——两块 NVLink 互连的 GPU 是这个对比的最低配置。这与 [docs/benchmark.md:324-329](https://github.com/triple-mu/fast-ulysses/blob/ad1a6a06a42d8129a278f010b1d67a6e206d38e6/docs/benchmark.md#L324-L329) 的「What is not measured」一致：zerosm 是在那轮测量之后写的，故文档中没有它的表。

#### 4.3.4 代码实践

**实践目标**：用纸面数据演练两列联读，确保你能正确区分「真零 SM」与「拷贝被饿死」。

**操作步骤**：

1. 假设四次测量得到：peer 臂 `t_copy=20.1ms, t_gemm=19.8ms`（链单独），并发区墙钟 `t_pair=20.3ms`、链在区内 `t_under=19.9ms`。计算 pair ratio、full comp、gemm slowdown。
2. 再假设 same 臂：`t_copy=6.0ms, t_gemm=19.8ms, t_pair=25.1ms, t_under=24.9ms`。同样计算三量。
3. 分别写出结论。
4. 精读 [benchmark/bench_a2a.py:572-584](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/benchmark/bench_a2a.py#L572-L584)，回答：若 `creps` 改为按**最慢**臂（peer）校准，same 臂会发生什么？

**需要观察的现象**：两组纸面数字在三量上的截然不同形态。

**预期结果**：peer 臂：slowdown = 19.9/19.8 ≈ 1.01×，pair ratio = 20.3/max(20.1, 19.8) ≈ 1.01×，full comp = 39.9/20.1 ≈ 1.98× —— 1.00× 配 1.00×，零 SM。same 臂：slowdown = 24.9/19.8 ≈ 1.26×（链明显变慢），pair ratio = 25.1/19.8 ≈ 1.27×，接近 full comp = 25.8/19.8 ≈ 1.30× —— 拷贝与链几乎完全竞争，同设备拷贝占用了计算资源。第 4 步：same 臂单拷贝只有 peer 的几分之一长，按 peer 校准的 `creps` 会让 same 臂总时长同样达到 20 ms——这没问题；真正的问题是反过来（按最慢臂会让最快臂过短）不对——正确答案是：按最慢臂校准时 `creps` 偏小，**最快臂（same）被拉伸得不够、可能仍短于 20 ms 地板**，几十微秒的启动间隙在几毫秒的窗口里就不再是千分之一，两列都被稀释。若在有 ≥2 GPU 的机器上实测：待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：为什么每条臂的 GEMM 链要「配自己的拷贝」而不是两臂共用一条链？

答案：同字节到 peer 与到本地内存耗时差好几倍（注释原话「several times longer」）。共用一条链时，链要么长于 same 臂的拷贝、要么短于 peer 臂的拷贝；链长于拷贝时，裸露的链尾以单独速度运行，把慢化拉向 1.00×，而 same 臂（等字节下更短的那条）受害最重——恰恰是要测出「变慢」的那条臂被稀释。

**练习 2**：zerosm 的输出里为什么把 `full comp` 也打印出来？

答案：它是「完全不重叠」的理论墙钟比，给 pair ratio 提供一个锚点。没有它，「pair ratio = 1.3×」只是个孤立的数；有了它，「1.3× 对 full comp 1.30×」立刻说明几乎完全竞争，「1.3× 对 full comp 1.98×」则是部分重叠。参照列让判读无需 memorize 任何经验数字。

**练习 3**：文档声明 zerosm「not in this document」（无已发表数字），但 `collect.sh` 会跑它。这体现了什么方法论？

答案：方法与数据分离的诚实性：文档只发表它实际测到、且通过独占与双节点一致性检查的数字；一个没有数字的模式仍然进 `collect.sh`，是为了下一轮测量自动补上。反例是把没有校准或没有判据的数字先行发表。

### 4.4 独占执行纪律：exclusive.sh 与 collect.sh

#### 4.4.1 概念说明

一切方法论的前提是：**共享 GPU 上得到的数字不是数字**（exclusive.sh 头注释原话「a number from a shared GPU is not a number」）。威胁有两层：

1. **看起来像测量、其实是错的**：跑起来时 GPU 空闲，三十秒后邻居作业来了，输出依然像正常数据。
2. **部分竞争不可见**：4 条 rank 里 2 条被拖慢，集合基准被偏斜的方式在输出里看不出来——因为屏障会把最慢 rank 的延迟均摊给所有人，你无法从结果列里分辨「我的代码慢」还是「邻居抢了我的卡」。

`tools/exclusive.sh` 因此做三件事：开跑前**等待**GPU 空闲（等不到就拒绝）；运行期间**持续采样**进程数与 SM 时钟；结束后给出**判定**——`EXCLUSIVE`（数字可用）或 `CONTENDED`（明说「Discard these numbers」，退出码 4）。`benchmark/collect.sh` 则在一切数字之前写**环境指纹**：机器、commit、驱动、torch/构建信息、前后 SM 时钟、拓扑、doctor 输出——因为没有归因环境的数字既无法复现也无法撤回。

#### 4.4.2 核心流程

exclusive.sh 三段式：

1. **等空闲**：轮询 `nvidia-smi --query-compute-apps`，把请求的 GPU（按 UUID 匹配）上的进程数数到 0 为止；超过 `WAIT_SECS` 则 `REFUSED` 退出（码 3）。
2. **边跑边采样**：后台子 shell 每 `SAMPLE_SECS`（默认 5 秒）记一行「进程数 + 各卡最低 SM 时钟」；同时把 `CUDA_VISIBLE_DEVICES` 绑定为请求的设备表——巡检的与运行的是同一组卡。
3. **判定**：采样中出现过的最大进程数若超过「每卡 EXPECT_PER_GPU（默认 1）× 卡数」，判 `CONTENDED`（退出码 4）；否则 `EXCLUSIVE` 并报告采样数与最低 SM 时钟。

collect.sh 流程：写指纹头 → **正确性门**（`pytest test/` 不过就不测任何数）→ 按序跑 `stages zerocopy sweep link zerosm overlap padding`（每个模式都经 exclusive.sh）→ 卡数 ≥4 时加跑 `nproc=4` 的 stages → 结束再记一次时钟，任何模式失败则整场 `INCOMPLETE` 并明说「Publish nothing from this run until they are explained」。

#### 4.4.3 源码精读

**头注释：为什么必须独占**。[tools/exclusive.sh:1-12](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/tools/exclusive.sh#L1-L12) 完整陈述了威胁模型（作业不请自来、部分竞争不可见），并给出用法与 `WAIT_SECS` 环境变量。

**外部进程计数**。[tools/exclusive.sh:26-39](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/tools/exclusive.sh#L26-L39)：`uuid_of` 把 CUDA 序号换成 UUID，`foreign_count` 数请求的卡上有多少进程——`nvidia-smi --query-compute-apps=gpu_uuid` 只报 UUID，序号对不上，这一步是必须的桥。`EXPECT_PER_GPU=1` 的注释（L21-22）说明语义：「每卡一个进程是我们自己的运行贡献的；超出即邻居」。

**等待与拒绝**。[tools/exclusive.sh:41-54](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/tools/exclusive.sh#L41-L54)：忙则每 10 秒重查，超时打印 `REFUSED: … Not measuring on shared GPUs.` 并退出 3——**拒绝测量**而不是勉强给出一个数字。

**采样器与清理陷阱**。[tools/exclusive.sh:56-94](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/tools/exclusive.sh#L56-L94)：后台子 shell 记录每轮「外部进程数 + 请求卡中的最低 SM 时钟」，且以 `kill -0 parent` 自终止，防止孤儿采样器存活；[tools/exclusive.sh:61-68](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/tools/exclusive.sh#L61-L68) 的 trap 捕获 `EXIT INT TERM HUP`——注释点明缘由：断掉的 ssh 只送这些信号，不清理会留下陈旧采样器，而**陈旧采样器会被下一次运行当成「外部进程」**，把好的运行误判为 CONTENDED。

**绑定设备**。[tools/exclusive.sh:84-88](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/tools/exclusive.sh#L84-L88)：`export CUDA_VISIBLE_DEVICES="${DEVICES}"`。注释讲清了一个微妙 bug：不绑定的话，「脚本巡检一组设备、运行落在另一组」——torch 默认用 0..N-1——一个 EXCLUSIVE 判定对数字的来源**什么都不说明**。

**判定段**。[tools/exclusive.sh:96-113](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/tools/exclusive.sh#L96-L113)：读采样文件，取最大进程数与最低有效时钟；超过期望则 `VERDICT: CONTENDED … Discard these numbers.` 退出 4，否则 `VERDICT: EXCLUSIVE (N samples, max X/Y procs, min SM clock Z MHz)`，退出码透传命令自己的状态。

**collect.sh 的归因哲学**。[benchmark/collect.sh:1-21](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/benchmark/collect.sh#L1-L21) 头注释第一句就是论点：「基准的价值最多等于它归因的价值」——同型号两个节点的分歧可以超过被测量的任何东西，只有每个数字都说清自己来自哪台机器、哪个构建、哪个 commit 才可追赶。[benchmark/collect.sh:35-72](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/benchmark/collect.sh#L35-L72) 写指纹头：日期、主机、卡表、commit（无 .git 时回退 `COMMIT` 文件，两者皆无则打「UNKNOWN -- this run is not attributable, do not publish from it」，[benchmark/collect.sh:43-51](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/benchmark/collect.sh#L43-L51)）、驱动、torch 与 `_C.build_info()`、前后 SM 时钟、`topo -m`、`doctor`。

**正确性门**。[benchmark/collect.sh:102-113](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/benchmark/collect.sh#L102-L113)：注释是一句方法论宣言——「一个通不过自己测试的构建给出的数字比没有数字更糟，因为它长得像数据」。门不过，全场退出，不测任何数。

**模式编排与失败记账**。[benchmark/collect.sh:115-127](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/benchmark/collect.sh#L115-L127) 按序跑七模式（每模式都包 exclusive.sh），≥4 卡加跑 ws=4 的 stages——注释解释为何没有奇数 world_size 档：形状是真实模型的 40/56 头，mode 0 散射头轴，都除不尽 3；为一个凑出来能被 3 整除的头数测性能，数字与表里任何东西都不可比。[benchmark/collect.sh:93-99](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/benchmark/collect.sh#L93-L99) 的 `PIPESTATUS[0]` 细节保证 grep 没选中行不会把成功的模式记成失败；[benchmark/collect.sh:129-139](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/benchmark/collect.sh#L129-L139) 任何模式失败则整场 `INCOMPLETE`。

**已发表的纪律声明**。[docs/benchmark.md:13-28](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/benchmark.md#L13-L28)：每个数字都在 exclusive.sh 下取得且全部报告 EXCLUSIVE；整机 8 卡独占；**同一个 wheel**（一个二进制，全部机器不重编）；每台测量前先过 `pytest test/`；**每型号两节点**，一致则报一个数并注明 spread，不一致则两个都报、绝不平均（v0.1 曾因第二节点 5× 分歧撤回过已发布的行）。

#### 4.4.4 代码实践

**实践目标**：亲手对比「独占包裹」与「裸跑」两种条件下同一测量的离散度，体会 CONTENDED 数字为何不可用。

**操作步骤**：

1. 找一台 ≥2 GPU、且你能容忍短暂占用的机器。选定卡表如 `0,1`。
2. 裸跑三次：
   `torchrun --nproc_per_node=2 benchmark/bench_a2a.py --mode stages --iters 25`
   记录每行的 `transfer`、`barr_in`、`barr_out`。
3. 包裹跑三次：
   `./tools/exclusive.sh 0,1 -- torchrun --nproc_per_node=2 benchmark/bench_a2a.py --mode stages`
   （注意 exclusive.sh 已设 `CUDA_VISIBLE_DEVICES`，裸跑时需自设同样的卡表才可比。）
4. 分别计算两组内 `transfer` 与两道屏障的极差/中位数。若机器上恰有邻居作业，观察包裹组的 VERDICT 行。
5. 写一段结论：哪一列在两组间离散度差异最大？为什么屏障列对邻居最敏感？

**需要观察的现象**：独占组的 `barr_in/barr_out` 离散度显著小于裸跑组（若裸跑期间真有邻居）；`transfer` 列相对稳定——邻居抢的是 SM 与到达时刻，fabric 本身未必被抢。

**预期结果**：裸跑组若遭遇邻居，屏障列出现不可解释的尖峰且无任何告警；包裹组同样情况会直接判 `CONTENDED` 并退出 4，数字被显式废弃。若两组都安静，离散度接近——这本身就是独占判定的含义：EXCLUSIVE 证明的是「这组数字里没有邻居的贡献」，而不是「数字更大」。**实际对比数字：待本地验证**（需要 ≥2 GPU 与可控的邻居负载；无多卡环境时，改为纸面推演：对 [tools/exclusive.sh:97-109](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/tools/exclusive.sh#L97-L109) 写出「采样到 3 个进程、期望 2」时的完整判定路径与退出码）。

#### 4.4.5 小练习与答案

**练习 1**：exclusive.sh 为什么在等待阶段之后还要绑定 `CUDA_VISIBLE_DEVICES`？不绑定会破坏哪个具体保证？

答案：等待阶段按物理序号巡检的是 DEVICES 那组卡，而 torchrun/torch 默认落在 0..N-1。不绑定时可能出现「巡检了空闲的 4,5,6,7、实际跑在忙碌的 0..3」——VERDICT: EXCLUSIVE 对数字真正的来源一言未发，判定与数字脱钩。绑定后巡检的与运行的是同一组卡（子进程看到的是重编号后的 0..N-1），判定才覆盖数字的出处。

**练习 2**：采样器为什么用 `kill -0 parent` 循环自杀、还要 trap `INT TERM HUP` 而不只是 `EXIT`？

答案：两个都是防「孤儿采样器」。断掉的 ssh 连接送来的是 TERM/HUP 而不一定走 EXIT 路径；不清理的话，(a) `/tmp` 里的采样文件泄漏，(b) 更糟的是陈旧采样器自身成为一个占用 GPU 视角的进程（它持续调用 nvidia-smi），下一次运行会把它数进 `foreign_count`，把本来独占的运行误判为 CONTENDED——测量仪器污染了被测系统。

**练习 3**：docs/benchmark.md 为什么坚持「每型号两节点，不一致则两个都报、绝不平均」？

答案：两个同型号节点分歧 5× 的实测教训（PCIe 机型）说明「节点间一致性」本身是被检验的性质而不是可假设的性质。平均两个分歧的数会制造一个两个节点都没出现过的第三种数字，掩盖问题；两个都报则把分歧本身变成数据——正是靠这个纪律才发现 PCIe 的失步来自交换机布局差异而非带宽。

## 5. 综合实践

**任务**：给一台假想的 4×H200 测量节点写一份《测量前检查单》，并用它完整走一遍 stages 模式的计划。

1. **列检查单**（每条注明对应的源码依据）：
   - 正确性门先行：`pytest test/` 必须绿（collect.sh:102-113 的理由）；
   - 独占三段式：先等空闲、跑中采样、拿 VERDICT（exclusive.sh 三段）；
   - 卡表绑定：`CUDA_VISIBLE_DEVICES` 与巡检一致（exclusive.sh:84-88）；
   - 环境指纹：date/host/commit/driver/torch/build_info/前后时钟/topo/doctor（collect.sh:35-72）；
   - 中位数纪律：默认 `--iters 25 --warmup 8`，不自行改小；
   - 归因列齐全：`raw` 地板、`relayout%`、`copyout%`、`submit us` 一起读（bench_a2a.py:197-213）。
2. **写出将要执行的命令**：collect.sh 的单机等价形式——`./tools/exclusive.sh 0,1,2,3 -- torchrun --nproc_per_node=4 benchmark/bench_a2a.py --mode stages`，以及为什么要补一遍 `--nproc_per_node=8`（ws 改变切分形状，relayout% 才是跨组可比的列，见 docs/benchmark.md:193-214）。
3. **预填判读模板**：把 4.1.4 的列模板抄在检查单末尾，并为每列写上「若该列异常变大，先查什么」——例如 `barr_in` 变大先查邻居与到达偏斜（floor），`transfer` 变小先查是否忘了独占、fabric 是否被并发流占用。
4. （有 ≥2 GPU 时）实际执行第 2 步并按第 3 步模板逐列判读；无 GPU 时全案为纸面推演，标注「待本地验证」。

**预期产出**：一页检查单 + 一份逐列判读模板。它应该让你在没有作者的机器上，产出与 docs/benchmark.md 同等可信度的数字。

## 6. 本讲小结

- **stages 分解的可加性**：CUDA 事件严格排序在同一条流上，分段之和等于整次调用；`a2a` 对 `transfer` 是同口径对，`raw` 是传输地板而非备选，`relayout%` 与 `copyout%` 把加速比归因到「基线有而本库没有的阶段」。
- **两个归一化指标**：`hidden% = (serial − concurrent) / a2a_alone` 度量异步调用藏进计算的比例，受可用计算量约束、必须读作区间；`of the fabric = transfer 折算带宽 / link 单流天花板` 度量传输对裸 fabric 的利用率，90–99% 即「没什么可再调度的了」。
- **barrier 是 floor 不是待优化项**：其成本与载荷无关、大头是 rank 到达偏斜——任何同步都要在某处付这笔钱；sweep 模式的 barriers 份额曲线（0.5 MB 时 33–54%、291 MB 时 5–8.4%）说的是「何种尺寸下该换工具」。
- **zerosm 两列联读**：GEMM 慢化与成对比必须一起读——1.00×+1.00× 是零 SM，1.00×+逼近 full comp 是拷贝被饿死；每臂链长按自己的拷贝校准、字节按最快臂拉伸到 20 ms 地板。
- **独占是数字有效的前提**：共享 GPU 的数字「看起来像测量、其实是错的」，部分竞争在集合基准的输出里不可见；exclusive.sh 用等空闲、边跑边采样、EXCLUSIVE/CONTENDED 判定三段式守住这一前提，collect.sh 再用环境指纹与正确性门保证数字可归因、可复现、可撤回。

## 7. 下一步学习建议

下一讲 u9-l5「设计反思」将把视角从「怎么测」转向「设计押了哪些注」：docs/design.md 中未文档化的 CE 写入可见性假设、为何拒绝 PCIe（本讲 benchmark.md 的 5× 节点分歧正是证据）、被移除的方案清单（其中「closing flag 走 CE 写」一项的弃用判据就来自本讲 overlap 模式量出的 +34% → −28%）。建议继续精读 `docs/design.md` 全文与 `docs/migration.md`，并把本讲的判读模板带到 u9-l5 的设计评审练习里——每一个「被删掉的方案」背后都应有一个像本讲这样的测量。
