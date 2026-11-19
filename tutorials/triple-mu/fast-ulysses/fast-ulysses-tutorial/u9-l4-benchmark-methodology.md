# 基准方法论：stages/overlap/zerosm 与独占执行

## 1. 本讲目标

fast-ulysses 的 README 里有一堆数字——1.60–2.17× 于 `torch.distributed` 路径、传输达 fabric 上限 90–99%、hidden% 等。本讲要回答的问题是：**这些数字是怎么测出来的，以及为什么这样测才可信**。学完本讲，你应该能够：

1. 对 `benchmark/bench_a2a.py` 的七种模式各写出一句话的问题定义与判读方法，重点掌握 stages 分解中每一列回答什么问题。
2. 解释 `hidden%` 与 `of the fabric` 两个归一化指标的定义、分子分母各自量的是什么。
3. 复述 zerosm A/B 的双列联读法，以及代码里为让这两列可信所付出的全部设计代价。
4. 说明独占执行（EXCLUSIVE/CONTENDED）为何是一切数字有效的前提，并掌握 `tools/exclusive.sh` 的三段式工作流。

## 2. 前置知识

- **CUDA 事件计时 vs 主机墙钟**：`torch.cuda.Event(enable_timing=True)` 在流上打时间戳，度量的是设备侧两件事之间的时间；`time.perf_counter()` 度量的是主机提交代码的耗时。二者回答不同问题——本讲会看到两个都用的地方。
- **中位数与预热**：GPU 上首次调用含缓存分配、JIT、驱动懒初始化等一次性成本，所以每次测量都先 warmup 再取多次运行的中位数（对离群邻居干扰更稳）。
- **集合通信的计时要对齐 rank**：一次 all-to-all 的耗时由最慢的 rank 决定。若各 rank 的 warmup 次数不同，一个 rank 的预热残留会落进另一个 rank 的计时窗口，故计时前要 `dist.barrier()`。
- **共享 GPU 与邻居进程**：基准机往往是共享的，任务随时到来。邻居进程占用 SM、带宽、还会让 SM 时钟掉频——这些都不受你控制，却全部落进你的计时窗口。
- **fabric 带宽**：NVLink 互联（fabric）能搬运的每秒字节数有物理上限。「用了 fabric 的百分之多少」是判断「还有没有调度空间」的判据。
- **承接前讲**：u6-l2 讲过一次调用是「prepare 校验 → 开路屏障 → CE 传输 → 收尾屏障 → copy_out」五段；u5-l2 讲过零 SM 仅属跨链路远端拷贝、zerosm 用双臂 A/B 检验。本讲把这些性质变成**可测、可判读的数字**。

## 3. 本讲源码地图

| 文件 | 行数 | 作用 |
|---|---|---|
| `benchmark/bench_a2a.py` | 681 | 七种测量模式的主脚本；模块 docstring 本身就是一份判读手册 |
| `tools/exclusive.sh` | 113 | 独占执行守门人：等空闲 → 边跑边采样 → 打出 EXCLUSIVE/CONTENDED 判决 |
| `benchmark/collect.sh` | 139 | 一台机器的完整测量流程：指纹头 → 正确性门 → 依次跑全部模式 |
| `docs/benchmark.md` | 336 | 已发表数字与全部方法论纪律（两台机器、单一 wheel、撤回记录、未测量清单） |
| `python/fast_ulysses/group.py` | — | `_timed()`：基准专用的分段计时探针（L251–L265） |
| `src/bindings.cc` | — | `all_to_all_4d_timed` 算子：在四段之间记录 CUDA 事件（L624–L654） |

## 4. 核心概念与源码讲解

### 4.1 stages 分解：一次调用的时间去哪了

#### 4.1.1 概念说明

stages 模式回答的问题是：**「两边各自的时间都花在哪了」**。它把两条路径各自拆成若干段，使各段**相加等于整个调用**，而不是仅仅与总和相关。这一点是整个 fast-ulysses 性能论证的地基：

- BASE 是 sglang `usp.py` 的路径：permute → `all_to_all_single` → permute，拆成三段；
- OURS 是本库的调用，拆成四段：`barr_in / transfer / barr_out / copy_out`（transfer 里含本 rank 自份额）；
- 另测一个 `raw`：单独跑平坦的 `all_to_all_single`——同样的字节数、不做任何重排、结果是错误布局，所以它是**传输下界**，不是可选方案。

拆段之后，「基线的重排内核占了多少」「我们的握手占了多少」从猜测变成算术。模块 docstring 说得很直白：`a2a` 对 `transfer` 是同类项对比（like-for-like pair），其余各段是每条路径围绕它必须多付的——「这整个就是论证本身，因为基线的重排是 SM 工作，会和这个设计想要藏身的计算相竞争」。

#### 4.1.2 核心流程

`run_stages` 对 `SHAPES` 里每个真实模型形状（wan-720p / wan-480p / h3-t2va-5s）执行：

1. 头数不能整除 world_size 则跳过该形状（mode 0 散射头轴）。
2. `s_total` 向下取整到 ws 的倍数——这是基线需要的「padded shard」。
3. 分配输入 `x` 与等长的平坦 `send`/`recv`。
4. `base = median_of(baseline_stages(...))` → 基线三段的中位数。
5. `ours = median_of(group._timed(...))` → 本库四段的中位数。
6. `raw = median_ms(all_to_all_single(recv, send))` → 平坦传输下界。
7. `submit = submit_us(...)` → 主机提交一次同步调用的微秒数。
8. rank 0 打印一行，`dist.barrier()` 对齐后进入下一形状。

计时基建的三个关键设计：

- **Stopwatch**：CUDA 事件只在流上按序记录，最后一次读出相邻事件差。打点花的是主机时间、不占设备时间，所以「细分过的运行测出的总和与不细分的相同」。
- **median_of**：对每次运行收集各段耗时列表，再**对每个段位独立取中位数**。注意一个细节：表中 BASE/OURS 总计是各段中位数之和，不是「总时的中位数」——各段无交叠、无空隙（同一条流上严格排序），所以任何时间都被归因到某一段。
- **median_ms**：预热后先 `dist.barrier()` 再计时（对齐各 rank 起跑线）；**baseline_stages 只在 run_stages 里用**，因为它的 `read()` 会主机同步，落进任何外层计时区都会污染测量。

#### 4.1.3 源码精读

计时的底层是基准专用算子：[python/fast_ulysses/group.py:L251-L265](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/python/fast_ulysses/group.py#L251-L265)——`_timed()` 调用 `all_to_all_4d_timed` 算子，把返回的四个数贴上 `("barrier_in", "transfer", "barrier_out", "copy_out")` 的名字。docstring 明说「各段之和等于整个调用，因为它们在一条流上严格有序；读事件会同步」。

事件打在哪里：[src/bindings.cc:L624-L654](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L624-L654)——五个事件把复制路径（`prepare(..., std::nullopt, kSyncWindow, lend=false)`，即不带 out= 的普通调用）切成四段：`fast_barrier` → `launch_a2a_ce` → `fast_barrier` → `copy_out`，与 u6-l2 的流水线一一对应。注释特意说明事件用默认标志（不禁用计时），因为 `cudaEventElapsedTime` 需要时间戳。

Stopwatch 与三个中位数工具：[benchmark/bench_a2a.py:L84-L137](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/benchmark/bench_a2a.py#L84-L137)——`Stopwatch.mark()` 只记录事件、`read()` 在末尾统一读出差值；`median_of` 按段位取中位数；`median_ms` 在预热后加 `dist.barrier()`；`submit_us` 用 `perf_counter` 测主机提交耗时（批折叠要压平的正是这个量）。

基线的两种形态：[benchmark/bench_a2a.py:L140-L165](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/benchmark/bench_a2a.py#L140-L165)——`baseline_padded` 无插桩（可被整体计时），`baseline_stages` 在三段间打事件。注意 L153–L154 的 docstring：`read()` 会主机同步，故仅 `run_stages` 使用。

主表与全部派生列：[benchmark/bench_a2a.py:L168-L214](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/benchmark/bench_a2a.py#L168-L214)——表头在 L175–L181，四组测量在 L197–L202，打印在 L204–L213。三个派生列的计算就在打印行里：

- `raw/CE = raw / ours[1]`（L209）——平坦 `all_to_all_single` 除以本库 transfer 段；
- `relayout% = (base[0] + base[2]) / base_total`（L210）——基线两个 permute 占其总时间的份额；
- `copyout% = ours[3] / ours_total`（L211）——copy_out 占本库总时间的份额。

**读表陷阱**：表中的 `a2a` 列是**基线的** `all_to_all_single` 段，不是本库调用；本库的对应列叫 `transfer`。这一对才是 like-for-like。

#### 4.1.4 代码实践

**实践目标**：为 stages 输出的每一列写一行判读模板，并用 `docs/benchmark.md` 已发表的一行数字手工验证派生列的算术。

**操作步骤**：

1. 精读 [benchmark/bench_a2a.py:L10-L16](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/benchmark/bench_a2a.py#L10-L16)（stages 模式 docstring）与 L175–L181 的表头。
2. 抄下 `docs/benchmark.md` H200 行（wan-720p）：perm_in 0.358、a2a 0.826、perm_out 0.385、BASE 1.569、transfer 0.680、copy_out 0.143、OURS 0.860、raw 0.859。
3. 手工复算三个派生列并与表中 47.3%、1.26×、16.6% 对照。
4. 写出你的判读模板表（下面答案给出参考版）。

**需要观察的现象 / 预期结果**：

- `relayout% = (0.358 + 0.385) / 1.569 = 47.35% ≈ 47.3%` ✓
- `raw/CE = 0.859 / 0.680 = 1.263 ≈ 1.26×` ✓
- `copyout% = 0.143 / 0.860 = 16.6%` ✓（且与 benchmark.md 零拷贝表中 H200 wan-720p 的「copy_out 占计时各段 16.6%」互相印证）

三列全部对上即证明你对列语义的理解与代码一致。有 GPU 时可运行（待本地验证）：

```bash
./tools/exclusive.sh 0,1,2,3,4,5,6,7 -- torchrun --nproc_per_node=8 \
    benchmark/bench_a2a.py --mode stages
```

#### 4.1.5 小练习与答案

**练习 1**：`raw` 列为什么是「传输下界」而不是「替代方案」？

**答案**：`raw` 只跑平坦的 `all_to_all_single`，搬运同样的字节但完全不做重排，结果处于错误布局（L12–L14、L172–L174）。真实调用必须把数据放回正确布局，所以 raw 不是能用的路径；它只回答「光搬这些字节最少要多久」，用来给 transfer 列定标尺。

**练习 2**：`submit us` 列测的是什么？它想守住哪个设计？

**答案**：测主机提交一次调用的微秒数（`submit_us` 的 docstring，L126–L127）。CE 传输会把一次交换拆成许多条 `cudaMemcpy2D/3DAsync`，若每条的提交开销在主机侧累积，卡就会饿。批折叠（把 batch 维折叠进 3D 拷贝）要压平的就是这个量——它跟设备侧时间无关。

**练习 3**：为什么 `median_of` 对每个段位独立取中位数，而不是先取每次运行的总和再取中位数？

**答案**：这里的目标是**归因**而非单点估计：每个段位的中位数对邻居干扰等离群值最稳，而各段在同一条流上无交叠、无空隙，段位中位数之和仍是总时间的一个稳健估计。两种算法给出的「总」可能略有差别，但归因结论（哪段占多少）不因这个选择翻转。

### 4.2 overlap 隐藏率与「of the fabric」：两个归一化指标

#### 4.2.1 概念说明

两个归一化指标把「快」翻译成可比较的命题：

- **hidden%**：异步调用在并发 GEMM 下消失的份额。定义：
  \[ \text{hidden\%} = \frac{t_{\text{serial}} - t_{\text{concurrent}}}{t_{\text{a2a,alone}}} \times 100\% \]
  分子是「先通信后计算」与「通信计算并发」的墙钟差，分母是通信自身单独跑的时间。这是零 SM 设计**存在意义**所系的命题（L18–20）。
- **of the fabric**：transfer 段实际用掉的 fabric 带宽占上限的百分比。上限由 link 模式独立测得——**绕开本算子**、直接用 torch 对称内存做平坦 64 MiB peer 拷贝，「让天花板独立于被审判的对象建立」（L42–L45）。

归一化的意义在于分离混杂因素：hidden% 把「GEMM 本身多慢」从分子分母中约掉；of-the-fabric 把「消息多大」约掉，只剩「调度还有没有空间」——80% 以上就没有什么可调度的了，30% 则有。

#### 4.2.2 核心流程

**overlap 模式**（[benchmark/bench_a2a.py:L217-L259](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/benchmark/bench_a2a.py#L217-L259)）：

1. 用 `SHAPES[0]`（wan-720p），配三个 `a @ w`（k=n=5120）模拟 to_q/k/v 的形状。
2. 单独测 `t_gemm`、`t_a2a`。
3. `serial()` = 同步 a2a 然后 GEMM 链；`concurrent()` = `all_to_all_4d_async` + GEMM 链 + `wait()`。
4. **两种安排交替执行**各 8 轮、每轮内部再取 8 次的中位数，最后取轮间中位数——因为「GEMM 窗口逐轮漂移的幅度和 a2a 本身差不多，交替能抵消漂移」（L244–L245）。
5. 打印 hidden% 及两边的 min–max 展布。

**link 模式**（[benchmark/bench_a2a.py:L402-L443](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/benchmark/bench_a2a.py#L402-L443)）：

1. 经 `torch.distributed._symmetric_memory` 分配 64 MiB，rendezvous 后取 `(rank+1)%ws` 的窗口。
2. `one_pair`：只有 rank 0 拷贝 → fabric 上单一流；`all_pairs`：每 rank 都拷贝 → ws 条流。
3. 打印单流 GB/s 与聚合 GB/s，末尾说明判读方法：stages 的 `transfer` 除以**真正过链路的字节**（总量 × (ws−1)/ws），再除以单流上限，就是「用了 fabric 的百分之多少」。

sweep 模式里的同一归一化：[benchmark/bench_a2a.py:L390-L397](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/benchmark/bench_a2a.py#L390-L397)——`crossed = mb * (ws-1) / ws / 1e3`（本 rank 自份额不过链路），`GB/s = crossed / transfer`。

#### 4.2.3 源码精读

交替采样的实现：[benchmark/bench_a2a.py:L246-L259](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/benchmark/bench_a2a.py#L246-L259)——8 轮循环里 `ts.append(median_ms(serial, 8, 1))` 与 `tc.append(median_ms(concurrent, 8, 1))` 相邻执行，排序后取中位数，并打印两边的展布区间。`hidden` 在 L259 打印。

link 的两臂与打印：[benchmark/bench_a2a.py:L419-L443](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/benchmark/bench_a2a.py#L419-L443)——`one_pair` 里 `if rank == 0: dst.copy_(src)`（只有 rank 0 写，fabric 只载一条流）；单流与全对的 GB/s 都按搬运字节数除以时间算出；L442–L443 直接把判读方法印在输出里。

`docs/benchmark.md` 的发表版：[docs/benchmark.md:L93-L109](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/benchmark.md#L93-L109)——四台机器单流 377–692 GB/s，本库 a2a 达 356–621 GB/s，即**上限的 90–99%**；并观察到「八条流同时跑时每流带宽不降——fabric 不是被竞争的资源」。

hidden% 的发表版与告警：[docs/benchmark.md:L147-L164](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/benchmark.md#L147-L164)——「这个指标有真实展布」，同型号两节点差最多 35 个百分点，要「当作区间读」；且指标受可用计算量约束（H200 能藏更多是因为 GEMM 更长），不是纯粹的通信性质。

#### 4.2.4 代码实践

**实践目标**：用纸笔验证两个归一化指标的全部算术，理解分子分母各自来自哪次测量。

**操作步骤**：

1. 从 [docs/benchmark.md:L155-L160](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/benchmark.md#L155-L160) 抄出四台机器的 gemm alone / a2a alone / serial / concurrent，按公式复算 hidden%。
2. 从 [docs/benchmark.md:L100-L105](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/benchmark.md#L100-L105) 取 H200 行，用 stages 表的 transfer 0.680 ms 与「291 MB/rank、7/8 过链路」复算「of the ceiling」。

**需要观察的现象 / 预期结果**：

- hidden%：H200 (2.971−2.320)/0.876 = **74.3%**；B200 (1.633−1.312)/0.535 = **60.0%**；B300 (1.469−1.219)/0.533 = **46.9%**；H100 (3.050−2.451)/0.925 = **64.8%**——与表中 74/60/47/65 全部一致。
- of the ceiling：过链路字节 = 291 × 7/8 = 254.6 MB；254.6 MB / 0.680 ms = **374.4 GB/s**；374.4 / 378.7（单流上限）= **98.9% ≈ 99%**——与表中一致。

无 GPU 也能完成；有 GPU 时用 `--mode overlap` / `--mode link` 重跑并核对量级（待本地验证）。

#### 4.2.5 小练习与答案

**练习 1**：hidden% 为什么可能超过 100%？超过时怎么读？

**答案**：serial 安排要付并发安排避免掉的启动/串行化成本，因此差值可以略大于 a2a 自身时间。`docs/benchmark.md` L306–L308 记录了 v0.1 在 A100 上约 105%：「读作完全隐藏」。

**练习 2**：link 模式为什么用 torch 对称内存直接拷贝，而不调用本库算子？

**答案**：天花板必须独立于被审判的对象（L403–L406）。若经过本算子，测得的天花板就混入了本算子的屏障、copy_out 等开销，再拿它给 transfer 定标就成了循环论证。

**练习 3**：为什么「of the fabric」的分母用**单流**带宽而不是八流聚合带宽？

**答案**：因为比较对象是单次 a2a 中每条链路上的实际流量：transfer 段里每对 rank 之间就是一条流。拿它对齐「单条流能跑多快」才是在问「这条链路被用满了吗」；聚合带宽回答的是整机吞吐，是另一个问题。

### 4.3 zerosm A/B：一次只差一个变量的实验

#### 4.3.1 概念说明

zerosm 模式回答：**「一次拷贝到底占不占 SM」**。u5-l2 已建立判读法——GEMM 慢化与成对比（pair ratio）**两列联读**：`1.00× 慢化 + 1.00× 成对比` 才是零 SM；`1.00× 慢化 + 成对比贴满完全竞争参照` 则是拷贝被 GEMM 饿死。本讲从「怎么让这两列可信」的角度精读实验装置本身，它有三个原则：

1. **两臂只差一个变量**：peer 臂（目的在对端的对称内存窗口，拷贝过链路）vs same 臂（目的是本 rank 自己的内存）——同样的字节、同样的 `dst.copy_(src)` 调用。
2. **绕开本算子**：算子路径自带两个屏障内核和一个 copy-out，它们**按构造就是 SM 工作**，整调用测出的任何东西都无法归因给传输。
3. **每臂配自己的 GEMM 链**：同样字节到 peer 和到本地内存耗时不同（差数倍），共享一条链会让较短的那臂（same 臂——正是应该显出慢化的臂）被拉向 1.00×，实验就瞎了。

#### 4.3.2 核心流程

对两个 payload（设计工作点的字节数、64 MiB）× 两种流数（单流：仅 rank 0 动；ws 流：全员各写邻座）：

1. **拷贝次数校准**：对每臂单测一次单拷贝时长，取最快臂为基准，把拷贝重复次数 `creps` 定到「最短臂也至少持续 LONG_MS = 20 ms」；由 rank 0 算出后 `dist.broadcast`——每个 rank 必须发出相同的工作。
2. **每臂链长校准**：再单测每臂 creps 次拷贝的时长，链上 GEMM 个数 = 该时长 / 单个 GEMM 时长（取整、至少 1），同样广播。
3. **交替采样**：ROUNDS=5 轮 × 每轮内两臂交替；臂内「并发区」与它作除数的两个 alone 测量也交替。注释说明：若基线只在开头测一次，整轮漂移会全部灌进慢化列。`dist.barrier` 保证各 rank 同处一臂——这是「ws 流行真是 ws 流测量」的前提。
4. **并发区构造**（`pair()`）：先入队 GEMM 链再入队拷贝——链是几次 launch、拷贝是几百次，后入队的会晚起跑，先入队链把这段暴露压到最小；在链后打一个 mark，把「链自身在并发区内的时长」单独读出。
5. 每臂输出四列：copy（单独）、gemm（链单独）、pair（并发区墙钟）、gemm slowdown（链在并发区内时长 / 链单独时长），外加 pair ratio 与 full comp 两个派生。

判读用的两个派生量：
\[ \text{pair ratio} = \frac{t_{\text{pair}}}{\max(t_{\text{copy}},\ t_{\text{gemm}})}, \qquad \text{full comp} = \frac{t_{\text{copy}} + t_{\text{gemm}}}{\max(t_{\text{copy}},\ t_{\text{gemm}})} \]

#### 4.3.3 源码精读

模式的完整自述：[benchmark/bench_a2a.py:L447-L463](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/benchmark/bench_a2a.py#L447-L463)——「两臂只差目的地一件事」「链被饿死的拷贝从没在链下跑过，慢化同样停在 1.00×，所以成对比与完全竞争参照印在旁边」。头注释 L543–L546 更是把结论印在输出里：**NEITHER COLUMN IS THE RESULT ON ITS OWN**。

拷贝次数的校准与广播：[benchmark/bench_a2a.py:L572-L584](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/benchmark/bench_a2a.py#L572-L584)——「按最快臂校准，使最短臂也持续 LONG_MS」；两个 payload 都要拉伸，因为设计 payload 单拷贝比一个 GEMM 还短。广播的原因写明：「每个 rank 必须发出相同的工作」。

每臂链长的校准：[benchmark/bench_a2a.py:L592-L606](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/benchmark/bench_a2a.py#L592-L606)——「一条链每臂一条，配该臂自己的拷贝，这就是 docs/design.md 描述的实验：链『等长于』其下的拷贝」；共享链会最狠地打击 same 臂（较短的那臂）。

交替采样循环：[benchmark/bench_a2a.py:L607-L621](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/benchmark/bench_a2a.py#L607-L621)——每个报告的数字都采自这个循环：臂与臂交替、臂内并发区与两个 alone 交替；屏障让所有 rank 同处一臂。

并发区与打印：[benchmark/bench_a2a.py:L498-L517](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/benchmark/bench_a2a.py#L498-L517)（`pair()` 先链后拷贝、链后打 mark）与 [benchmark/bench_a2a.py:L624-L637](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/benchmark/bench_a2a.py#L624-L637)（`full = (t_copy + t_gemm) / longer`，打印 pair ratio、full comp、gemm slowdown 三列）。

发表状态：[docs/benchmark.md:L324-L329](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/benchmark.md#L324-L329)——zerosm 写于本轮测量**之后**，所以本文档没有任何它的表；`collect.sh` 会跑它，下一轮测量将补上。这是「没有数字就不声称数字」的示范。

#### 4.3.4 代码实践

**实践目标**：用数值推演把「零 SM」与「被饿死」两种状态的四列各自算出来，证明只有双列联读才分得开它们。

**操作步骤**：

1. 假设某臂测得：copy 单独 = 20.0 ms，GEMM 链单独 = 20.0 ms（链长校准的目标状态）。
2. 情形 A（零 SM / 完全重叠）：并发区墙钟 pair = 20.4 ms，链在并发区内时长 = 20.1 ms。
3. 情形 B（完全竞争 / 拷贝被饿死）：拷贝在链下被拖到约 40 ms，pair = 39.6 ms，链自身时长仍 ≈ 20.1 ms。
4. 对两情形各算 gemm slowdown、pair ratio、full comp 三列。
5. 对照 [benchmark/bench_a2a.py:L543-L546](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/benchmark/bench_a2a.py#L543-L546) 的判读语检查你的结论。

**需要观察的现象 / 预期结果**：

| 情形 | gemm slowdown | pair ratio | full comp | 判读 |
|---|---|---|---|---|
| A 零 SM | 20.1/20.0 = **1.01×** | 20.4/20.0 = **1.02×** | 2.00× | 零 SM ✓ |
| B 被饿死 | 20.1/20.0 = **1.01×** | 39.6/20.0 = **1.98× ≈ full comp** | 2.00× | 拷贝与 GEMM 完全竞争 |

两行的 **gemm slowdown 完全相同**——单看这一列两种现实不可区分；分开它们的只有 pair ratio。有 ≥2 张 NVLink 互连 GPU 时可实跑 `--mode zerosm` 验证 peer 臂落在 A、same 臂落在 B 附近（待本地验证）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `creps` 校准「以最快臂为基准」，而不是让两臂等长？

**答案**：同样的字节到 peer 与到本地内存耗时差数倍（L573–575），一个拷贝次数不可能让两臂等长。校准只保证**最短**的臂也持续 LONG_MS=20 ms——在这个地板上，几十微秒的 launch 间隙只占千分之一，两臂的测量才都有意义。

**练习 2**：`pair()` 里为什么先入队 GEMM 链、后入队拷贝？

**答案**：后入队的工作要等主机提交完先入队的工作才开始跑。链是几次 launch、拷贝是几百次，若拷贝先入队，链会晚起跑「提交拷贝的那段时间」，这段链以单独速度裸奔，把慢化拉向 1.00×。先入队链把这段暴露压到两种顺序中较小的一种（L500–507）。

**练习 3**：world_size=1 时这个模式还能用吗？

**答案**：不能作为零 SM 证明。代码在 L549–L555 明说：没有 peer，**peer 臂不被测量**，本次运行不检验该主张；same 臂与 GEMM 基线是真的。比较这一模式存在意义所需的最低配置是两张 NVLink 互连的 GPU。

### 4.4 独占执行纪律：exclusive.sh 的三段式

#### 4.4.1 概念说明

`bench_a2a.py` 模块 docstring 第一句就是纪律：「**在 tools/exclusive.sh 下运行——来自共享 GPU 的数字不是数字**」（L3–5）。为什么：

1. **邻居负载是随机变量**：共享机上任务不请自来，30 秒前空闲的 GPU 现在就可能有人；干扰直接落进计时窗口，结果「看起来像真实测量——只是是错的」。
2. **部分竞争不可见**：4 个 rank 里 2 个被邻居拖慢，集合通信的耗时由最慢 rank 决定，整个数字被抬高，而输出里**没有任何痕迹**指明原因（exclusive.sh 头注释 L5–8）。
3. **掉频是第二个混杂**：邻居负载会压低 SM 时钟，所有 SM 工作（GEMM、permute、屏障内核）整体变慢，比例失真——所以采样器还记录最低 SM 时钟。

因此脚本的工作流是三段式：**等空闲 → 边跑边采样 → 判决**，判决只有两种：EXCLUSIVE（数字可用）或 CONTENDED（**丢弃这些数字**，退出码 4）。

#### 4.4.2 核心流程

1. **等空闲**：`foreign_count` 用 `nvidia-smi --query-compute-apps` 按 GPU UUID 数出请求的每张卡上的进程数；非零则每 10 秒重试，超过 `WAIT_SECS`（默认 0）则 `REFUSED` 退出——「不在共享 GPU 上测量」。
2. **边跑边采样**：后台每 `SAMPLE_SECS`（默认 5 秒）记录一次「进程数 + 请求卡集中的最低 SM 时钟」；采样器监视父进程存活，父亡则止——孤儿采样器会在下一次运行里被当成「外来进程」。`trap cleanup EXIT INT TERM HUP` 保证断连时杀掉采样器并删临时文件。
3. **绑定设备**：`export CUDA_VISIBLE_DEVICES="${DEVICES}"`——不绑定的话「脚本监视一组设备而运行落在另一组」（torch 默认用 0..N-1），一个 EXCLUSIVE 判决就对数字的出处一言不发。
4. **判决**：整个运行期间见过的最大进程数 `max_seen` 若超过 `expected = GPU 数 × EXPECT_PER_GPU`（默认 1，即每卡一个进程是我们自己的贡献，多出来的都是邻居）→ `CONTENDED`；否则 `EXCLUSIVE`，附样本数、最大进程数、最低 SM 时钟。

#### 4.4.3 源码精读

问题陈述：[tools/exclusive.sh:L1-L12](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/tools/exclusive.sh#L1-L12)——头注释完整陈述了「部分竞争（4 卡中 2 卡变慢）以输出中不可见的方式扭曲集合基准」这一动机，以及两种用法（立即测量 / `WAIT_SECS` 等空闲）。

外来进程统计：[tools/exclusive.sh:L29-L39](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/tools/exclusive.sh#L29-L39)——先用 `uuid_of` 把物理卡号换成 UUID，再数 `compute-apps` 里落在这些 UUID 上的进程。用 UUID 而不是卡号，是因为 compute-apps 报告的是 UUID。

等待、采样与设备绑定：[tools/exclusive.sh:L41-L54](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/tools/exclusive.sh#L41-L54)（空闲循环与 REFUSED）、[tools/exclusive.sh:L60-L88](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/tools/exclusive.sh#L60-L88)（信号 trap、采样循环、`CUDA_VISIBLE_DEVICES` 绑定及其注释）。

判决：[tools/exclusive.sh:L96-L113](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/tools/exclusive.sh#L96-L113)——遍历样本求 `max_seen` 与 `min_clock`；超出 expected 打 `CONTENDED -- Discard these numbers` 退出 4；否则 `EXCLUSIVE (样本数, max procs, min SM clock)` 并透传命令自身退出码。

`docs/benchmark.md` 的落地：[docs/benchmark.md:L13-L18](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/benchmark.md#L13-L18)——「每个数字都在 tools/exclusive.sh 下取得……以下每次运行都报告了 EXCLUSIVE」。

#### 4.4.4 代码实践

**实践目标**：不依赖 GPU 完成对脚本的静态验证与判决推演；有 ≥2 GPU 时完成「包裹 vs 裸跑」的离散度对比。

**操作步骤**：

1. `bash -n tools/exclusive.sh` 做语法检查（无 GPU 也能跑）。
2. 纸面推演：请求 4 张卡、`EXPECT_PER_GPU=1`，样本序列为 `(4, 1700), (5, 1650), (4, 1710)`——`max_seen`、`expected`、判决各是什么？若样本是 `(4, 900), (4, 850)` 呢？
3. （≥2 GPU，待本地验证）同一模式跑两次：
   ```bash
   ./tools/exclusive.sh 0,1 -- torchrun --nproc_per_node=2 benchmark/bench_a2a.py --mode stages
   torchrun --nproc_per_node=2 benchmark/bench_a2a.py --mode stages   # 裸跑对照
   ```
   各跑 3 遍，记录 transfer/OURS 列的极差（max−min）。
4. 写下你对「为什么 CONTENDED 数字不可用」的四点论证（答案见下）。

**需要观察的现象 / 预期结果**：

- 推演 2：`expected = 4×1 = 4`；样本一 `max_seen=5 > 4` → **CONTENDED，丢弃**；样本二进程数虽合法，但 `min_clock ≈ 850 MHz` 被记录——判决仍是 EXCLUSIVE，但时钟信息暴露了掉频，判读数字时必须带上它。
- 步骤 3 的预期：包裹组的极差显著小于裸跑组（独占下 H200 两节点间 transfer 段一致到 2% 以内，见 `docs/benchmark.md` L89–L91）；裸跑组若机器正巧空闲也可能一致——这恰是危险所在：**你不能事后分辨一个裸跑数字当时是否被干扰过**，而包裹组每个数字自带判决。

#### 4.4.5 小练习与答案

**练习 1**：`EXPECT_PER_GPU` 为什么默认是 1？什么情况下要改？

**答案**：我们自己的运行对每张卡贡献恰好一个进程（torchrun 每 rank 一卡）；任何多出来的进程都是邻居（L21–L22）。若某个待测命令本身一卡起多进程（如单卡双进程的推理服务），才需要调大它，否则会永远 REFUSED/CONTENDED。

**练习 2**：采样器为什么要有「父进程死了就退出」和信号 trap 两层防护？

**答案**：ssh 断连只送部分信号，一个漏杀的孤儿采样器既留下临时文件、又会作为 compute-apps 之外的一个 `nvidia-smi` 调用者持续存在——更重要的是下一次运行会把它数进 `foreign_count`，让自己的守门脚本被自己人误判（L60–68 的注释）。

**练习 3**：EXCLUSIVE 判决能否完全保证数字有效？

**答案**：不能，它只是必要条件。`docs/benchmark.md` 记录了反例：两台同型号 RTX PRO 6000 在**都判 EXCLUSIVE** 的运行里相差 5×（L57、L276–279）——差异来自 PCIe 拓扑而非邻居干扰。独占排除的是「共享干扰」这一种混杂；机器差异、驱动、时钟策略还要靠指纹头与两台机器互证来覆盖。

### 4.5 可归因的数字：collect.sh 与 benchmark.md 的方法纪律

#### 4.5.1 概念说明

一个基准数字的价值等于它的**可归因性**（attribution）：知道它来自哪台机器、哪个 commit、哪个构建、哪套环境，数字才能被复现、被比较、被撤回。`collect.sh` 的头注释说得直接：「同型号 GPU 的两个节点可以相差超过被测的任何东西，而这只有当每个数字都说清自己来自哪台机器、哪个构建、哪个 commit 时才可捕捉」。它把一次完整测量组织成：

1. **指纹头**：任何数字之前先记录环境——日期、主机名、GPU 列表、仓库、commit、驱动、torch/CUDA 版本、GPU 型号与数量、fast_ulysses 版本与构建信息、测量前的 SM 时钟/温度/功率上限、`nvidia-smi topo -m` 拓扑、`doctor` 输出。
2. **正确性门**：先跑 `pytest test/`，「来自一个连自己的测试都过不了的构建的数字比没有数字更糟，因为它看起来像数据」。失败则整个流程终止，一个测量都不做。
3. **全部模式依次过 exclusive.sh**：顺序 stages → zerocopy → sweep → link → zerosm → overlap → padding，全部包在 `tools/exclusive.sh` 里；GPU ≥ 4 时追加 `stages` 的 ws=4 一遍（切分形状随 world size 变化）。
4. **失败清算**：用 `PIPESTATUS[0]` 取运行自身的退出码（而非管道的），失败的模式记名，结尾打印 `INCOMPLETE` 与「在解释清楚之前不要从中发表任何东西」。

`docs/benchmark.md` 再补三条发表纪律：**单一 wheel**（同一 sha256 的二进制装到所有机器，跨代际之间不重编译）、**每型号两台机器**（两台一致才展示一台并注明展布；不一致则两台都列、绝不平均——v0.1 曾因第二台同型号机器差 5× 而撤回已发表的一行）、**未测量就声明未测量**（0.2.1 新增的暂存池与 lend 无数字；zerosm 写于测量之后故无表）。

#### 4.5.2 核心流程

`collect.sh <label> [gpu-list]` 的执行流：

```
tee 到 ${OUTDIR}/${LABEL}.log
├── 指纹头（date/host/gpus/repo/commit/driver/torch/GPU/版本）
├── clocks(before) + topo -m + fast-ulysses doctor
├── 正确性门：pytest test/ ──失败──→ "GATE FAILED"，exit 1
├── for mode in stages zerocopy sweep link zerosm overlap padding:
│      tools/exclusive.sh ${GPUS} -- torchrun ... bench_a2a.py --mode ${mode}
│      （PIPESTATUS 记失败模式名）
├── NPROC ≥ 4 时再跑一遍 stages@4
├── clocks(after)
└── 有失败 → "INCOMPLETE ... Publish nothing"；否则 "DONE"
```

几个细节：commit 的获取有三层回退——git、集群 rsync 同步（无 `.git`）时改读 `COMMIT` 文件、都没有则打 `UNKNOWN -- this run is not attributable, do not publish from it`；不做奇数 world size 的性能测量（40/56 头不整除 3，为整除而发明的头数与表中任何数字都不可比，奇数的正确性归 pytest 管）；`ALLOW_NON_NVLINK=1` 用于测量被构造函数拒绝的机器——「拒绝本身就是结果」。

#### 4.5.3 源码精读

归因哲学与用法：[benchmark/collect.sh:L1-L21](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/benchmark/collect.sh#L1-L21)——头注释与 `OUTDIR`/`ITERS`/`SKIP_TESTS`/`ALLOW_NON_NVLINK` 四个环境变量的语义。

指纹头与 commit 回退：[benchmark/collect.sh:L35-L62](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/benchmark/collect.sh#L35-L62)——从 `date -Is` 到 `fast_ulysses._C.build_info()`；L44–L51 的三层 commit 回退，`UNKNOWN` 分支直接说「此运行不可归因，不要从中发表」。

时钟、拓扑与 doctor：[benchmark/collect.sh:L63-L71](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/benchmark/collect.sh#L63-L71)——测量**前**的 clocks 与 topo；结尾 L130–L131 还有测量**后**的 clocks，前后对照能看出热节流。

模式循环与失败记账：[benchmark/collect.sh:L83-L100](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/benchmark/collect.sh#L83-L100)（`run()` 与 PIPESTATUS 注释）、[benchmark/collect.sh:L115-L139](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/benchmark/collect.sh#L115-L139)（七模式循环、`stages@4`、奇数 world size 的不测理由、`INCOMPLETE` 收尾）。

正确性门：[benchmark/collect.sh:L102-L113](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/benchmark/collect.sh#L102-L113)。

发表纪律原文：[docs/benchmark.md:L19-L28](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/benchmark.md#L19-L28)——整节点独占、单一 wheel、单一镜像、correctness first、每型号两台；以及 [docs/benchmark.md:L324-L335](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/benchmark.md#L324-L335) 的「未测量清单」。

#### 4.5.4 代码实践

**实践目标**：从 `collect.sh` 提取完整的归因字段清单，并找出 `docs/benchmark.md` 中三处「诚实声明」。

**操作步骤**：

1. 通读 [benchmark/collect.sh:L35-L72](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/benchmark/collect.sh#L35-L72)，把指纹头打印的每一类信息列成清单，并为每项写一句「没有它会发生什么」。
2. 在 `docs/benchmark.md` 中找出：(a) 0.2.1 未测量特性的声明；(b) H100 单节点的 caveat；(c) v0.1 撤回又复现的记录。
3. 回答：为什么 `run()` 里必须用 `PIPESTATUS[0]` 而不是管道退出码？

**需要观察的现象 / 预期结果**：

- 指纹清单至少含：日期、主机名（长/短）、GPU 列表与 nproc、仓库路径、commit（含修改文件数）、驱动版本、torch 与 CUDA 版本、GPU 名称与数量、fast_ulysses 版本与 build_info、before/after 时钟、拓扑、doctor 输出。
- 三处诚实声明：(a) L7–L11 暂存池与 lend「两者都没有数字」；(b) L4–L5 H100 两样本来自同一节点、15% 展布是运行间方差而非节点间一致性；(c) L294–L298 v0.1 撤回的 119.2 ms 在本轮复现为 122.25 ms——「两种模式都是真的」。
- `PIPESTATUS` 问题：管道里 `grep -vE` 若一行都没选中会以 1 退出，`pipefail` 下会把成功的模式误记为失败（L93–L95 注释）；而模式真死掉时又容易在长日志里漏看，所以要显式取第一个命令的退出码并记账。

本实践无需 GPU，全部可离线完成。

#### 4.5.5 小练习与答案

**练习 1**：为什么 `collect.sh` 把 `pytest test/` 放在一切测量之前，而且默认无法跳过？

**答案**：性能数字的说服力依赖于「测的是正确的实现」。一个连正确性测试都过不了的构建跑出的数字仍然光滑、可复现，但它测量的是错误行为的性能——比没有数字更糟（L102–104）。`SKIP_TESTS=1` 存在但注释写明「不要用于要发表的数字」。

**练习 2**：两台同型号机器的数字不一致时，`docs/benchmark.md` 的处理办法是什么？为什么不平均？

**答案**：两台都展示、注明展布、绝不平均（L26–28）。平均会制造一个两台机器都没验证过的第三种状态，把「机器间差异」这个事实藏起来；PCIe 一节正是靠**不平均**才暴露出「同一型号相差 5×、差异在拓扑而非 fabric」的结论。

**练习 3**：`collect.sh` 为什么在 ws=4 之外不测奇数 world size（如 3）？

**答案**：L120–L124：形状是真实模型的（40/56 头），mode 0 散射头轴，都不整除 3；为了整除而发明的头数得到的数字与表中任何数字不可比。奇数 world size 的**正确性**由 pytest 覆盖（`test_distributed.py` 每个 worker 都在 3 上跑），性能测量只做可比较的配置。

## 5. 综合实践

**任务：为一个已发表的声明重建完整的测量与判读链。**

取 README/benchmark.md 的核心声明：「对 `torch.distributed` 路径快 1.60–2.17×，其中传输单独比裸 `all_to_all_single` 快 1.16–1.36×，且达 fabric 上限的 90–99%」。完成以下四步：

1. **拆解声明到列**：写出每个数字区间分别来自 stages 表的哪些列、哪些归一化公式（`vs BASE = BASE/OURS`、`raw/CE = raw/transfer`、`of the ceiling = crossed_bytes/transfer ÷ 单流带宽`）。
2. **设计运行方案**：写出完整命令——先 `benchmark/collect.sh b200-node1`（含正确性门与指纹头），确认每个模式都经 `tools/exclusive.sh` 包裹；若手动跑，写出 `./tools/exclusive.sh 0,...,7 -- torchrun --nproc_per_node=8 benchmark/bench_a2a.py --mode stages` 并说明你会在输出里找哪一行判决。
3. **交叉验证**：用第 2 步（或文档表）的一行数字，手工复算 `relayout%`、`copyout%`、`raw/CE`、`of the ceiling` 四个派生量；再用 overlap 表复算一个 `hidden%`。全部对上才算理解了列语义。
4. **写判读笔记**：回答——两节点展布落在屏障（到达偏斜）而非传输时，说明什么？（答：被搬的东西没变，偏斜只是挪到了两道握手之间，见 `docs/benchmark.md` L89–L91。）CONTENDED 的运行能靠「多跑几遍取中位数」补救吗？（答：不能——干扰是时变且不对称的，中位数只能抵抗离群值，不能移除系统性抬升；且你无法事后分辨哪个数字被污染。）

无 GPU 时第 2 步写为纸面方案并标注「待本地验证」；第 1、3、4 步全部可离线完成（本文 4.1.4、4.2.4 已给出可对答案的样例）。

## 6. 本讲小结

- **stages 分解**让各段时间相加等于整个调用：BASE 三段、OURS 四段；`a2a` 对 `transfer` 是 like-for-like，`raw` 是传输下界而非替代方案；`relayout%`、`copyout%`、`raw/CE` 三个归一化列把「快」拆成可归因的命题。
- **两个归一化指标**：`hidden% = (serial − concurrent)/a2a_alone` 度量异步调用藏进计算的份额（有真实展布，当区间读，且受可用计算量约束）；`of the fabric = 过链路字节/transfer ÷ 单流上限` 度量调度剩余空间（90–99% 即无事可调度），上限由绕开本算子的 link 模式独立建立。
- **zerosm A/B** 的可信度来自装置：两臂只差目的地、绕开算子（屏障与 copy-out 按构造是 SM 工作）、每臂配自己等长的链、拷贝次数按最快臂校准并广播、臂与基线全部交替采样；判读必须**双列联读**——单看 gemm slowdown 无法区分「零 SM」与「被饿死」。
- **独占执行是前提**：共享 GPU 上的邻居负载是时变、不对称、且在输出中不可见的混杂；`exclusive.sh` 三段式（等空闲 → 边跑边采样进程数与最低 SM 时钟 → EXCLUSIVE/CONTENDED 判决）把「数字是否有效」变成显式输出；CONTENDED 的数字丢弃而非补救。
- **可归因性**：`collect.sh` 的指纹头（含 commit 的三层回退与 UNKNOWN 即禁发表）、正确性门、PIPESTATUS 失败记账，加上 benchmark.md 的单一 wheel、每型号两台、不一致就都列不平均、未测量就声明未测量——共同保证数字可复现、可比较、可撤回。
- 基准的每个模式是一个**问题**而不是一个数字：stages 问时间去哪了、overlap 问藏了多少、padding/zerocopy 问两条特性路径的代价、sweep 问何时失效、link 问天花板在哪、zerosm 问占不占 SM。

## 7. 下一步学习建议

- 下一讲 **u9-l5（设计反思）**：把本讲的实测证据接到 `docs/design.md` 的假设复盘上——CE 写入可见性假设、为何拒绝 PCIe（本讲 PCIe 一节的两台 5× 分歧正是论据）、被移除的方案表。
- 继续阅读源码：`benchmark/bench_a2a.py` 的 `run_padding`（L262–L306）与 `_baseline_unpadded`（L309–L324）——「对照必须测同一件事」的又一范例（不能拿两个不同的基线互比）；以及 `docs/benchmark.md` 的 "Alternatives tried and not adopted" 一表，每行都是一个被测量否决的设计。
- 若有条件，读 `tools/preflight_gpu.sh` 与 `.github/workflows/ci.yml`（结合 u9-l1），理解「CI 证明编译与布局、preflight 证明多卡行为」的证明边界如何与本讲的独占纪律衔接。
