# 基准与剖析：bench_fwd、ncu 与性能报告

## 1. 本讲目标

前几讲我们读懂了 FlashKDA 的算法与两个 CUDA kernel 的实现。本讲回答工程上的最后一环：**如何量化「快」**。学完本讲你应当能够：

1. 独立运行 `benchmarks/bench_fwd.py`，读懂它输出的 `mean / min / max` 三列数字分别代表什么、为什么这样计时。
2. 说出基准覆盖的 5 个被测对象（flash_kda 三种 state 模式 + FLA `chunk_kda` + `chunk_gated_delta_rule`）与 fixed/varlen 两种模式的构造方式。
3. 使用 `benchmarks/ncu.sh` 用一条 `-k` 正则同时抓取 `_flash_kda_fwd_prepare` 与 `_flash_kda_fwd_recurrence` 两个 kernel 的完整指标（occupancy、smem、寄存器等）。
4. 理解 `generate_benchmark_md.py` 如何把基准 stdout 解析成 `BENCHMARK_H20.md` / `BENCHMARK_GB200.md` 风格的对比表格，并能自己复现一份。

## 2. 前置知识

- **cuda.Event 计时**：CUDA 的执行是异步的——Python 调用 `fn()` 后 kernel 只是**入队**，主机立刻返回。若用 `time.time()` 测墙钟，测到的是「入队耗时 + 主机抖动」而不是 GPU 真实耗时。`torch.cuda.Event(enable_timing=True)` 是打在 GPU 流（stream）上的时间戳：`start.record()` 与 `end.record()` 之间经过的 `elapsed_time(e)` 返回毫秒，精确反映设备侧两个时间戳之间的时长。
- **warmup / iters / repeats 三级采样**：第一次执行会触发 CUDA context 初始化、CUDA caching allocator 分配、驱动 JIT 等一次性开销，必须先空跑若干次「热身」；`iters` 是每轮连续计时的次数；`repeats` 是整轮重复的次数。最终样本数 = `iters × repeats`。
- **mean / min / max 的读法**：`mean` 是平均延迟；`min` 是最干净的一次（受干扰最小的下界，常用来对比 kernel 本征性能）；`max` 是最坏情况（被其他活动打扰时的上界）。`min` 与 `mean` 接近说明测量稳定。
- **ncu（Nsight Compute）**：NVIDIA 的 kernel 级剖析器。它通过「重放」（replay）多次执行同一个 kernel 来收集全部指标，代价极高，因此被剖析的运行要用极小的迭代数。`--set full` 表示采集全部指标节（Speed of Light、Launch Statistics、Occupancy、Memory Workload Analysis 等）。
- **kernel 符号名**：K1 叫 `_flash_kda_fwd_prepare`（定义于 [csrc/smxx/fwd_kernel1.cuh:L120](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel1.cuh#L120)），K2 叫 `_flash_kda_fwd_recurrence`（定义于 [csrc/smxx/fwd_kernel2.cuh:L133](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L133)）。varlen 模式还有一个辅助 kernel `_flash_kda_build_tile_prefix`（启动于 [csrc/smxx/fwd_launch.cu:L164-L167](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_launch.cu#L164-L167)）。ncu 的 `-k` 正则就是围绕这三个名字设计的。
- 承接 [u1-l5]：flash_kda 三种运行模式（无状态 / bf16 状态 / fp32 状态）由传入的 `initial_state` / `final_state` 隐式决定，在 C++ 层走不同的模板实例（[u2-l3]）。

## 3. 本讲源码地图

| 文件 | 作用 | 规模 |
|---|---|---|
| [benchmarks/bench_fwd.py](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/benchmarks/bench_fwd.py) | 基准主体：`bench_fn` 计时框架 + `run_case` 场景构造 + 5 个被测对象 + CLI | 167 行 |
| [benchmarks/bench.sh](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/benchmarks/bench.sh) | 一键脚本：装环境后直接跑 `bench_fwd.py` | 4 行 |
| [benchmarks/ncu.sh](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/benchmarks/ncu.sh) | ncu 剖析脚本：fixed / varlen 各出一份 `.ncu-rep` 报告 | 6 行 |
| [benchmarks/generate_benchmark_md.py](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/benchmarks/generate_benchmark_md.py) | 报告生成器：跑两遍基准、正则解析 stdout、写出 Markdown 对比表 | 343 行 |
| [BENCHMARK_H20.md](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/BENCHMARK_H20.md) | H20（Hopper）性能报告（生成物，被 README 引用） | 26 行 |
| [BENCHMARK_GB200.md](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/BENCHMARK_GB200.md) | GB200（Blackwell）性能报告（生成物） | 26 行 |

数据流关系：`bench_fwd.py` 打印结构化 stdout → `generate_benchmark_md.py` 用正则解析 → 渲染成 `BENCHMARK_*.md`。`ncu.sh` 则复用 `bench_fwd.py` 作为被剖析的负载，只是把迭代数调小。

## 4. 核心概念与源码讲解

### 4.1 计时框架 bench_fn

#### 4.1.1 概念说明

`bench_fn` 是全仓库唯一的计时函数，约 20 行，回答的问题是：**「一个已经入队的 GPU 工作负载，设备侧平均要跑多少毫秒？」**。它的设计要点有三个：

1. 用 cuda.Event 而不是主机墙钟，剥离异步入队与主机抖动；
2. 每轮把 `iters` 次 `(record, fn, record)` 连续入队、只在轮末同步一次，避免「每次迭代同步一次」引入的主机-设备往返空泡；
3. 输出 `mean / min / max` 三个统计量而非单一数值，让读者自己判断测量噪声。

#### 4.1.2 核心流程

```
bench_fn(fn, warmup, iters, repeats):
    1. 预热：执行 fn() 共 max(warmup, 1) 次（--warmup 0 也至少跑 1 次）
    2. torch.cuda.synchronize()          # 排干预热
    3. 重复 repeats 轮:
        a. synchronize()                 # 轮首清零
        b. 为 iters 次迭代各创建 start/end 事件对
        c. 连续入队: start[i].record() → fn() → end[i].record()
           （中间不同步，GPU 连续执行）
        d. synchronize()                 # 轮末一次性等待
        e. 收集每对的 elapsed_time → all_ms
    4. 样本排序后返回 (mean, min, max)
```

样本总数 \( n = \text{iters} \times \text{repeats} \)，默认 200 × 5 = 1000 个。三个统计量直接取自排序后的样本：\( \text{mean} = \frac{1}{n}\sum x_i \)，min 取 \( x_{(0)} \)，max 取 \( x_{(n-1)} \)。

#### 4.1.3 源码精读

热身与排空：[benchmarks/bench_fwd.py:L8-L11](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/benchmarks/bench_fwd.py#L8-L11) —— 先执行 `max(warmup, 1)` 次 fn（保证 `--warmup 0` 时也至少热身一次，ncu 场景正是传 0），再同步。注意 `max(warmup, 1)` 这个细节：ncu.sh 传 `--warmup 0` 是为了让被剖析的 kernel 实例尽量少，但 CUDA context / allocator 的初始化仍需至少一次真实调用来完成。

批量计时循环：[benchmarks/bench_fwd.py:L13-L23](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/benchmarks/bench_fwd.py#L13-L23) —— 每轮先 `synchronize`，然后**预创建** `iters` 对事件，循环内只做 `record → fn → record` 三步入队，全程无主机同步，轮末统一 `synchronize` 后逐对读 `elapsed_time`。这就是「批量记录」模式：GPU 上 200 次调用首尾相接，事件对精确切出每次调用的设备时长。

统计输出：[benchmarks/bench_fwd.py:L25-L30](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/benchmarks/bench_fwd.py#L25-L30) —— 排序后取均值、首元素（min）、尾元素（max）；空样本时回退 `nan` 防御。

一个重要的口径说明：对 flash_kda 而言，`fn()` 内一次 `flash_kda.fwd` 调用的设备侧工作 = beta 转置拷贝 kernel（u2-l2 讲过的唯一真实拷贝）+（varlen 时）tile 前缀和 kernel + K1 + K2。所以 bench 测的是**端到端包装层时间**，而 4.3 节的 ncu 测的是**单个 kernel 时间**——两者互补，对不上是正常的。

#### 4.1.4 代码实践

1. **实践目标**：体会「批量记录」与「逐次同步」两种计时口径的差异，并观察 min/mean 的稳定性。
2. **操作步骤**：把 `bench_fn` 原样抄进一个独立脚本 `timing_lab.py`（不改动仓库文件），被测函数用固定形状的 `torch.mm`（例如 2048×2048 的 bf16 矩阵乘）；再写一个对照版 `bench_fn_sync`，在每次 `fn()` 后立刻 `torch.cuda.synchronize()` 并用 `time.perf_counter()` 计时；两种口径各测 `warmup=30, iters=200, repeats=5`，打印各自的 mean/min/max。
3. **需要观察的现象**：两种口径的 mean 是否接近？逐次同步版的 max 是否明显更大、min 与 mean 的差距是否更松？
4. **预期结果**：对单个大 kernel，两口径 mean 接近；逐次同步版更容易被主机调度抖动抬高 max。具体差值**待本地验证**。
5. 若无 GPU 环境，本实践退化为源码阅读：在纸上推演 `max(warmup, 1)` 与轮首/轮末 `synchronize` 各自排除了哪类干扰。

#### 4.1.5 小练习与答案

**练习 1**：为什么不用 `time.time()` 直接包住 `fn()` 计时？
**答案**：CUDA 调用是异步的，`fn()` 返回时 kernel 可能还没开始执行，墙钟测到的主要是入队耗时与主机抖动；cuda.Event 打在设备流上，`elapsed_time` 反映的是两个时间戳之间 GPU 真实经过的毫秒数。

**练习 2**：如果把循环里的 `torch.cuda.synchronize()` 移到每次 `fn()` 之后，测量会变差在哪？
**答案**：每次同步都会让 GPU 流排空、主机-设备往返一次，下一次调用要重新填流水线；测到的样本混入空泡，且 200 次主机同步本身的抖动会抬高风险噪声明（max 偏大、分布变散）。

**练习 3**：ncu.sh 里给 bench_fwd.py 传 `--warmup 0`，热身就完全消失了吗？
**答案**：没有。`bench_fn` 用 `max(warmup, 1)` 兜底，`--warmup 0` 时仍会执行 1 次热身调用（[benchmarks/bench_fwd.py:L9-L10](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/benchmarks/bench_fwd.py#L9-L10)），保证 CUDA context 与 caching allocator 完成初始化。

### 4.2 基准场景：run_case 的输入构造与五个被测对象

#### 4.2.1 概念说明

`run_case(seq_lens, H, D, ...)` 是一个完整的基准场景：`seq_lens` 是一个长度列表——只有 1 个元素就是 fixed 模式，多于 1 个就是 varlen 模式。每个场景内依次测 5 个对象：

| 被测对象 | 模板实例（对照 u2-l3） | 说明 |
|---|---|---|
| flash_kda（bf16 state） | HasStateIn/Out=true, StateFP32=false | 传入 bf16 的 initial/final state |
| flash_kda（no state） | 两者皆 false | 不传任何 state 张量 |
| flash_kda（fp32 state） | HasStateIn/Out=true, StateFP32=true | 报告表中的口径，语义上与 chunk_kda 对照配置对齐 |
| FLA `chunk_kda` | —（Triton） | 被替换的基线，也是 u3-l11 中会自动分发到 flashkda 的那个入口 |
| FLA `chunk_gated_delta_rule` | —（Triton） | 另一种门控 delta 模型（每头标量门控），作为参照系而非同类对手 |

#### 4.2.2 核心流程

```
run_case(seq_lens, H, D):
    varlen = len(seq_lens) > 1
    T_total = sum(seq_lens);  N = len(seq_lens)
    varlen 时: cu_seqlens = [0] + cumsum(seq_lens)   (int64)
    构造输入: q/k 先 F.normalize 再转 bf16; v/g/beta 随机 bf16; A_log/dt_bias 随机 fp32
    initial_state = arange(N*H*D*D).reshape(N,H,D,D).to(bf16)   # 非零，确保状态路径真实工作
    依次 bench_fn 五个闭包并打印:
        flash_kda (bf16 state) / flash_kda (no state) / flash_kda (fp32 state)
        chunk_kda / chunk_gated_delta_rule
```

fixed 模式的 `seq_lens=[8192]` 意味着 `N=1`、batched 分支、state 形状 `[1,H,128,128]`；varlen 两个场景的 `T_total` 恰好都是 8192（`[1300,547,2048,963,271,3063]` 混合长度与 `[1024]*8` 均匀长度），与 fixed 同量纲可比。

#### 4.2.3 源码精读

模式判定与 cu_seqlens 构造：[benchmarks/bench_fwd.py:L38-L51](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/benchmarks/bench_fwd.py#L38-L51) —— `len(seq_lens) > 1` 即 varlen；cu_seqlens 用 `torch.cumsum` 前补 0，`dtype=torch.long`（u2-l2 校验链要求 int64）。两行 `print` 的格式（`shape=[T,H,D] warmup=... ` 与 `varlen shape=... seq_lens=[...] ...`）是刻意为之的结构化输出，4.4 节的正则解析完全依赖它们。

输入张量构造：[benchmarks/bench_fwd.py:L53-L64](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/benchmarks/bench_fwd.py#L53-L64) —— q/k 在 fp32 上做 L2 归一化再转 bf16，`scale_float = 1/√D`，`LOWER_BOUND = -5.0`（与 kernel 的范围论证一致，见 u3-l8）；`initial_state` 用 `arange` 生成非零值——如果传全零状态，状态加载路径虽然仍会执行，但非零初值更接近真实推理场景；`out` 预分配并由 kernel 原地写入（u1-l5），保证计时循环里没有分配抖动。

flash_kda 三种模式：[benchmarks/bench_fwd.py:L66-L93](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/benchmarks/bench_fwd.py#L66-L93) —— 三个闭包只差 `initial_state=/final_state=` 是否传入及其 dtype，正对应 u2-l3 的模板分发三档；fp32 模式直接复用 `initial_state.float()`。每次调用共享同一批预分配张量。

chunk_kda 对照配置：[benchmarks/bench_fwd.py:L96-L114](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/benchmarks/bench_fwd.py#L96-L114) —— 关键是这几个开关：`use_gate_in_kernel=True`（门控激活在 kernel 内做，因此 g 传激活前 logits，与 flash_kda 约定一致）、`use_qk_l2norm_in_kernel=True`、`use_beta_sigmoid_in_kernel=True`（beta 传 logits）、`lower_bound=-5`、`transpose_state_layout=True`（状态按 `[N,H,V,K]` 布局）。这**正是** u3-l11 中 FLA 自动分发到 flashkda 时要求的配置——基准测的就是「作为 drop-in 后端」时两条路径的公平对比。`initial_state` 传 fp32（FLA 侧的约定），这也是报告采用 flash_kda「fp32 state」模式作为口径的原因：它是状态语义对齐、且路径最重的保守口径。

chunk_gated_delta_rule 参照：[benchmarks/bench_fwd.py:L116-L132](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/benchmarks/bench_fwd.py#L116-L132) —— GDN 的门控是每头标量 `g`（形状 `(1,T,H)`、fp32），与 KDA 的每通道门控 `(1,T,H,D)` 是不同模型，报告里单列、不算同类加速比基准。

场景表与 CLI：[benchmarks/bench_fwd.py:L135-L163](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/benchmarks/bench_fwd.py#L135-L163) —— `FIXED_CASES=[[8192]]`，`VARLEN_CASES` 两组；`--mode` 选 fixed/varlen/all，默认 `--warmup 30 --iters 200 --repeats 5 --H 96 --D 128`。一键入口 [benchmarks/bench.sh:L1-L5](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/benchmarks/bench.sh#L1-L5) 只是「可编辑安装 + 安装 fla 与 matplotlib + 直接跑」。

#### 4.2.4 代码实践

1. **实践目标**：跑通一个缩小规模的基准，观察五种被测对象的输出格式与量级关系。
2. **操作步骤**（需要 SM90 GPU 与已安装环境，见 u1-l3）：
   ```bash
   bash benchmarks/bench.sh          # 完整入口（含环境安装）
   # 或先快速冒烟：
   python benchmarks/bench_fwd.py --mode fixed --H 32 --warmup 10 --iters 50 --repeats 3
   python benchmarks/bench_fwd.py --mode varlen --H 32 --warmup 10 --iters 50 --repeats 3
   ```
3. **需要观察的现象**：varlen 场景的头部行是否带 `varlen shape=[8192,32,128] seq_lens=[...]`；三种 flash_kda 模式耗时是否彼此接近（状态 I/O 相对主体计算是小头）；`chunk_kda` 是否明显更慢。
4. **预期结果**：三种模式差异在个位数百分比量级、flash_kda 快于 chunk_kda 约 1.8–2.3×（H20 口径，见 4.4.3）；本机具体数字**待本地验证**。
5. 无 GPU 时退化为阅读练习：手工验证 `[1300,547,2048,963,271,3063]` 与 `[1024]*8` 的和都是 8192，并回答「两个 varlen 场景各自使 K2 的 grid 第二维 N 等于多少」（6 与 8）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 q/k 在主机上 `F.normalize` 之后，flash_kda 那边没有等价的 `use_qk_l2norm_in_kernel` 开关？
**答案**：flash_kda 的 kernel **无条件**在 K1 里做 L2 归一化（u2-l7 的第一级流水），没有「不在 kernel 内归一化」的模式；主机侧再归一化一次近似幂等，目的是让 chunk_kda 两条路径拿到同样的单位范数输入，口径公平。

**练习 2**：fixed 模式下 `initial_state` 的形状是什么？为什么是 4 维的第 0 维等于 1？
**答案**：`[N,H,D,D] = [1,H,128,128]`。fixed 场景 `seq_lens=[8192]` → `N=1`，batched 分支下状态第 0 维就是 batch（u1-l5 的 `[B,H,V,K]`），`B=1`。

**练习 3**：`[1024]*8` 这个均匀 varlen 场景为什么值得单独测？它少了什么开销？
**答案**：每条序列恰好 16 的整数倍（1024/16=64），因此**没有尾块**——K2 的 STORE warp 全部走整块 TMA 路径（u3-l7），K1 也不会有残缺 tile；与混合长度场景对照可以隔离「尾块处理」的代价。观察 BENCHMARK_H20.md 的数据，该场景的加速比反而最高（2.29×），说明 flash_kda 在无尾块时优势更明显。

### 4.3 ncu 剖析流程

#### 4.3.1 概念说明

bench 告诉我们「整体多快」，ncu 告诉我们「每个 kernel 为什么这么快/慢」：寄存器数、smem 用量、occupancy、访存吞吐、warp 状态分布。`ncu.sh` 的核心技巧是用一条**正则**同时选中两个前向 kernel，并复用 `bench_fwd.py` 作为负载——剖析与基准共享同一套场景构造，避免「剖析的代码不是被测的代码」。

#### 4.3.2 核心流程

```
ncu.sh:
  1. 删除旧报告 report.ncu-rep / report_varlen.ncu-rep
  2. set -e; pip install -e .        # 确保剖析到的是当前源码
  3. ncu --set full \
         --kernel-name-base function \
         -k "regex:_flash_kda_fwd_(prepare|recurrence)" \
         --clock-control none --import-source yes --source-folders . \
         --export report.ncu-rep \
         python benchmarks/bench_fwd.py --mode fixed  --warmup 0 --iters 5 --repeats 1
  4. 同样参数再跑 --mode varlen → report_varlen.ncu-rep
```

实例数可以精确推算：fixed 报告里只有 3 种 flash_kda 模式会启动匹配的 kernel（`chunk_kda`/`gdn` 是 Triton kernel，不匹配正则），每种模式执行 `max(0,1)+5 = 6` 次 fwd，因此 K1 与 K2 各有 3 × 6 = **18 个被剖析实例**。varlen 报告同理，另外每轮多出的 `_flash_kda_build_tile_prefix` **不含 `fwd` 字样、被正则排除**，不会出现在报告里。

#### 4.3.3 源码精读

剖析命令本体：[benchmarks/ncu.sh:L5-L6](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/benchmarks/ncu.sh#L5-L6) —— 逐个参数解读：

- `-k "regex:_flash_kda_fwd_(prepare|recurrence)"`：按正则匹配 kernel 名，一次选中 K1/K2 两个 kernel；`--kernel-name-base function` 让匹配基于**函数名**而非完整 demangled 签名——这两个 kernel 是重模板类（22 个 TMA 描述符类型做模板参数，见 [csrc/smxx/fwd_launch.cu:L190-L199](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_launch.cu#L190-L199)），完整签名长达数行，函数名匹配是唯一可行方式。
- `--set full`：采集全部指标节。代价是每个被匹配实例都要重放多次，所以传给 bench 的迭代数收缩为 `--warmup 0 --iters 5 --repeats 1`（共 30 次 fwd 调用，其中 18 次命中正则）。
- `--clock-control none`：不把 GPU 时钟锁到基频，保留 boost 行为；绝对数字更接近真实但略 noisy。
- `--import-source yes --source-folders .`：把当前目录的源码嵌入报告，使 ncu-ui 里能做 SASS↔源码关联——这依赖编译期加的 `-lineinfo`（u1-l3 讲过的 nvcc 选项）。
- `--export report.ncu-rep`：落盘报告文件，供 `ncu-ui` 打开或 `ncu --import` 复查。

被剖析 kernel 的注册属性（在报告里核对什么）：K1 在 [csrc/smxx/fwd_kernel1.cuh:L120](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel1.cuh#L120) 带 `__launch_bounds__(NumThreads, 8)`——256 线程、目标每线程 ≤32 寄存器、8 CTA/SM（u2-l8 的 occupancy 取舍）；K2 在 [csrc/smxx/fwd_kernel2.cuh:L133](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L133) 只约束线程数（192 = 128 MMA + 32 LOAD + 32 STORE，u3-l2），其约 98KB 动态 smem 由 [csrc/smxx/fwd_launch.cu:L201](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_launch.cu#L201) 的 `cudaFuncSetAttribute` opt-in。grid 维度差异（K1 `(total_tiles,H)` vs K2 `(N,H)`，[csrc/smxx/fwd_launch.cu:L169](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_launch.cu#L169) 与 [L203](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_launch.cu#L203)）也直接体现在报告的 Launch Statistics 里。

#### 4.3.4 代码实践

1. **实践目标**：抓到两个 kernel 的 occupancy / 寄存器 / smem 指标，并与前几讲的理论推导对账。
2. **操作步骤**（需要 root/权限组才能用 ncu 的性能计数器）：
   ```bash
   bash benchmarks/ncu.sh                 # 生成 report.ncu-rep 与 report_varlen.ncu-rep
   # 免 GUI 快速浏览：
   ncu --import report.ncu-rep --page details | less
   # 只想看汇总可先试小集合（快得多）：
   ncu --set basic --kernel-name-base function -k "regex:_flash_kda_fwd_" \
       --export quick.ncu-rep python benchmarks/bench_fwd.py --mode fixed --warmup 0 --iters 1 --repeats 1
   ```
3. **需要观察的现象**：在 Launch Statistics / Occupancy 节里核对——K1 每线程寄存器是否为 32、每 SM 常驻 CTA 是否到 8；K2 的 dynamic shared memory 是否约 98KB、occupancy 是否被 smem 限制而非寄存器；fixed 与 varlen 两份报告中 K2 实例数是否相同（都是 18）而 grid 维度不同。
4. **预期结果**：K1 寄存器 32、K2 smem ~98KB 与 u2-l8/u3-l2 的推导一致；具体数值**待本地验证**（`--set basic` 输出足以覆盖上述指标）。
5. 若无 ncu 权限，替代实践：用 `--ptxas-options=-v`（u1-l3）重编译一次，从编译日志读出两个 kernel 的寄存器与 smem 用量，与 [fwd_launch.cu:L151](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_launch.cu#L151) / [L188](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_launch.cu#L188) 的 `sizeof(SharedStorage...)` 口径对照。

#### 4.3.5 小练习与答案

**练习 1**：正则为什么刻意不匹配 `_flash_kda_build_tile_prefix`？
**答案**：该正则要求名字含 `_flash_kda_fwd_`，而前缀和 kernel 叫 `_flash_kda_build_tile_prefix`（[csrc/smxx/fwd_launch.cu:L165](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_launch.cu#L165)），不含 `fwd`，故被排除——它是 32 线程的小 kernel，剖析它会稀释报告；两个主力 kernel 才是关注对象。

**练习 2**：为什么 ncu 下把 iters 从 200 降到 5？
**答案**：`--set full` 需要对每个被匹配的 kernel 实例做多次重放采集全部指标，若仍跑 200×5 次迭代，仅 fixed 模式就要剖析上千个实例，时间不可接受；降到 5 后每 kernel 只有 18 个实例，统计意义够用且耗时可控。

**练习 3**：`--clock-control none` 与 ncu 默认行为的区别是什么？
**答案**：默认 ncu 会把 GPU 核心频率锁到基频以获得可复现的数字；`none` 不锁频、保留 boost，绝对耗时更接近 bench 的真实场景，但逐次测量噪声更大。这也解释了为什么 ncu 里的 Duration 不应与 bench 的 min 直接对表。

### 4.4 基准报告生成：generate_benchmark_md.py 与 BENCHMARK_H20/GB200.md

#### 4.4.1 概念说明

`generate_benchmark_md.py` 是「基准 → 报告」的自动化管道：它以子进程跑两遍 `bench_fwd.py`（默认 H 与强制 `--H 64`），用三个正则解析 stdout，最后渲染出仓库里那份 `BENCHMARK_H20.md` 风格的 Markdown 表格。设计上有两点值得学习：

1. **报告口径收窄**：表格只取 flash_kda「fp32 state」模式的 mean 与两个 FLA 基线的 mean，其余（bf16/no state）测了但不进表；
2. **脆弱契约**：解析完全依赖 `bench_fwd.py` 的 print 格式——改一个空格，生成器就静默丢数据。这与 u3-l9 的结论同构：**参考与被参考方必须同步演进**。

#### 4.4.2 核心流程

```
main():
  args, bench_extra = parse_known_args()        # 未知参数全部转发给 bench_fwd.py
  run #1: bench_extra                 → stdout_a
  run #2: bench_extra 去掉 --H 后强制 --H 64 → stdout_b
  parse_stdout(): 正则切分 case 头 + 结果行 → dicts
  _complete_cases(): 丢弃任一均值缺失的 case
  render_markdown(): 标题(设备标签) / 生成日期(UTC) / 命令行 / 设置 / 两张表
  写出 md 文件（默认 BENCHMARK_H20.md）
```

#### 4.4.3 源码精读

三个解析正则：[benchmarks/generate_benchmark_md.py:L37-L46](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/benchmarks/generate_benchmark_md.py#L37-L46) —— `RE_HEADER_FIXED` 匹配 `shape=[T,H,D] warmup=... iters=... repeats=...`，`RE_HEADER_VARLEN` 多捕获一段 `seq_lens=[...]`，`RE_RESULT` 匹配缩进的结果行 `名字 : mean=.. ms, min=.. ms, max=.. ms`——与 4.2.3 中那两处 print 逐字符对应。

结果归类：[benchmarks/generate_benchmark_md.py:L134-L143](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/benchmarks/generate_benchmark_md.py#L134-L143) —— 名字含 `"fp32 state"` 记为 `flash_mean_ms`、等于 `"chunk_kda"` 记为 `chunk_mean_ms`、等于 `"chunk_gated_delta_rule"` 记为 `gdn_mean_ms`；bf16/no state 两行的数据被解析后丢弃。注意用的是**子串匹配** fp32 state，而另两个是全等匹配——所以 bench 里的行文措辞（`flash_kda (fp32 state)`）是生成器契约的一部分。

完整性过滤与格式化工具：[benchmarks/generate_benchmark_md.py:L205-L212](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/benchmarks/generate_benchmark_md.py#L205-L212)（三个均值任一缺失即整 case 丢弃）与 [L151-L164](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/benchmarks/generate_benchmark_md.py#L151-L164)（`[1024,...,1024]` 压缩成 `1024 x 8`）。加速比公式在 [L181-L184](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/benchmarks/generate_benchmark_md.py#L181-L184)：\( \text{speedup} = \text{chunk\_mean} / \text{flash\_mean} \)，保留两位小数加 `×` 后缀。

表格渲染：[benchmarks/generate_benchmark_md.py:L215-L235](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/benchmarks/generate_benchmark_md.py#L215-L235) —— 六列：Case、flash_kda mean、fla_chunk_kda mean、对 chunk_kda 的加速比、fla_chunk_gdn mean、对 gdn 的加速比；每个 H 一张表，表头 `### T=..., H=..., D=...`。双次运行的 H 覆盖由 [L187-L202](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/benchmarks/generate_benchmark_md.py#L187-L202) 的 `_argv_with_h` 完成：剥掉已有 `--H` 再追加 `--H 64`。

主流程与落盘：[benchmarks/generate_benchmark_md.py:L290-L339](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/benchmarks/generate_benchmark_md.py#L290-L339) —— 默认输出 `BENCHMARK_H20.md`、默认设备标签 `Hopper / H20`；`--device-label Blackwell / GB200 -o BENCHMARK_GB200.md` 就得到 GB200 版报告（其 Command 行如实记录了这串命令，见 [BENCHMARK_GB200.md:L1-L5](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/BENCHMARK_GB200.md#L1-L5)）。生成日期取 UTC 日精度（[L334](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/benchmarks/generate_benchmark_md.py#L334)）。

报告实物与读法：[BENCHMARK_H20.md:L12-L18](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/BENCHMARK_H20.md#L12-L18) —— H20、H=96、T=8192：fixed 1.85×，混合 varlen 2.06×，均匀 varlen 2.29×。两个规律：**(a)** varlen 下加速比更高，说明 flash_kda 的 varlen 路径（前缀和 + 二分 + 尾块处理）相对开销低于 Triton 基线的变长处理；**(b)** 对 GDN 的加速比明显更小（1.22–1.43×），且 [BENCHMARK_GB200.md:L20-L26](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/BENCHMARK_GB200.md#L20-L26) 中 H=64 fixed 一栏只有 0.96×——GDN 每头标量门控算法上更便宜，flash_kda 并非对所有相邻模型都占优，这是报告诚实的地方。

一个历史注脚：[BENCHMARK_H20.md:L5](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/BENCHMARK_H20.md#L5) 的 Command 行写的是 `generate_benchmark_hopper_h20_md.py`——该脚本已在 d2ff19a（"Support more architectures"）中改名为通用的 `generate_benchmark_md.py`（git 以 90% 相似度识别为 rename），H20 报告生成于改名之前。GB200 报告（2026-05-26）晚于改名，Command 行已是新名字。读旧报告时要意识到这一层演化。

#### 4.4.4 代码实践

1. **实践目标**：不跑 GPU 也能验证「stdout → Markdown」管道，理解脆弱契约。
2. **操作步骤**：
   ```bash
   python - <<'EOF'
   import sys, importlib.util
   spec = importlib.util.spec_from_file_location("gbm", "benchmarks/generate_benchmark_md.py")
   gbm = importlib.util.module_from_spec(spec); spec.loader.exec_module(gbm)
   sample = """shape=[8192,96,128] warmup=30 iters=200 repeats=5
     flash_kda (bf16 state) : mean=2.6000 ms, min=2.5900 ms, max=2.7000 ms
     flash_kda (no state)   : mean=2.5800 ms, min=2.5700 ms, max=2.6900 ms
     flash_kda (fp32 state) : mean=2.6220 ms, min=2.6100 ms, max=2.7100 ms
     chunk_kda : mean=4.8388 ms, min=4.8000 ms, max=4.9000 ms
     chunk_gated_delta_rule : mean=3.1985 ms, min=3.1000 ms, max=3.3000 ms
   """
   cases = gbm._complete_cases(gbm.parse_stdout(sample))
   print("\n".join(gbm._render_table_block(cases)))
   EOF
   ```
   然后做破坏性实验：把样例里的 `chunk_kda :` 改成 `chunk_kda:`（删一个空格），重新运行，观察该 case 是否被 `_complete_cases` 整行丢弃。
3. **需要观察的现象**：正常输入渲染出与 BENCHMARK_H20.md 同构的六列表格，加速比 = 4.8388/2.6220 ≈ 1.85×；删掉空格后 case 消失而非报错。
4. **预期结果**：与上述一致（本实践不依赖 GPU，可直接验证；`RE_RESULT` 允许 `名字后有空格` 的宽松匹配，但行内 `mean=` 等格式必须逐字匹配）。
5. 想跑真报告：`python benchmarks/generate_benchmark_md.py -o /tmp/my_bench.md`（会跑两遍完整基准，耗时数分钟；无 GPU 时跳过）。

#### 4.4.5 小练习与答案

**练习 1**：如果只想要 `BENCHMARK_H20.md` 里那种表，但机器上只有 1 小时时间，怎么裁剪？
**答案**：把裁剪参数透传给内层基准——例如 `python benchmarks/generate_benchmark_md.py --mode fixed --warmup 10 --iters 50 --repeats 3 -o /tmp/quick.md`。`parse_known_args` 会把未识别参数原样转发给 `bench_fwd.py`（[L306](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/benchmarks/generate_benchmark_md.py#L306) 与 [L317-L321](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/benchmarks/generate_benchmark_md.py#L317-L321)）；报告里的 Benchmark settings 行会如实记录小样本口径，读者能看出这不是标准配置。

**练习 2**：报告表里的 `flash_kda` 数字对应 bench 输出的哪一行？为什么选它？
**答案**：`flash_kda (fp32 state)` 行的 mean（子串匹配 `"fp32 state"`）。它是三种模式中状态 I/O 最完整的保守口径，且与 chunk_kda 对照配置（fp32 initial_state + output_final_state=True）语义对齐，加速比不占便宜。

**练习 3**：`_complete_cases` 为什么在缺任何一个均值时丢弃整个 case，而不是留空单元格？
**答案**：缺值意味着 bench 或解析环节出了问题（例如 fla 未安装、print 格式被改），此时该 case 的其余数字也不可信；整行丢弃并在 stderr 打 Warning（[L327-L332](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/benchmarks/generate_benchmark_md.py#L327-L332)），宁可少一行也不产出半真半假的表格。

## 5. 综合实践

**任务：产出一份属于你机器的迷你性能报告（BENCHMARK_H20 风格）。**

前置：按 u1-l3 完成安装，`pip install flash-linear-attention`（bench.sh 会代劳）。

1. **冒烟**：`python benchmarks/bench_fwd.py --mode fixed --H 32 --warmup 10 --iters 50 --repeats 3`，确认五种被测对象都能出数。
2. **正式采样**：`python benchmarks/bench_fwd.py --mode fixed --H 96 > /tmp/bench_fixed.txt 2>&1`（时间充裕可加 `--mode all`）。
3. **整理成表**：比照 4.4.3 的六列格式手工建表，但把 `flash_kda` 列扩成三种模式三列，并对每种模式分别计算 \( \text{speedup} = \text{chunk\_kda mean} / \text{flash mean} \)。参考模板：

   | Case | flash bf16 (ms) | flash none (ms) | flash fp32 (ms) | chunk_kda (ms) | speedup(bf16) | speedup(fp32) |
   |---|---:|---:|---:|---:|---:|---:|
   | Fixed T=8192 | … | … | … | … | …× | …× |

4. **解读**：回答三个问题——(a) 三种 state 模式差距多少个百分点，状态 I/O 的相对代价是否如预期般小？(b) min 与 mean 的差距说明测量多稳定？(c) 你的加速比落在 BENCHMARK_H20.md（1.85×）与 BENCHMARK_GB200.md（2.31×）之间吗？
5. **（可选）kernel 级验证**：跑一次 `bash benchmarks/ncu.sh`，从报告中找出 K1/K2 各自的 Duration，验证两者之和 + 包装开销 ≈ bench 测到的端到端 mean（粗对账即可）。
6. 全部数值**待本地验证**；无 GPU 的读者以第 4.4.4 的离线解析实践 + 样例数据完成表格拼装作为替代。

## 6. 本讲小结

- `bench_fn` 用 cuda.Event 批量记录（轮首同步 → iters 次连续 record/fn/record → 轮末同步），输出 mean/min/max 三个统计量；`max(warmup, 1)` 保证 `--warmup 0` 时仍有一轮真实热身。
- `run_case` 用 `len(seq_lens)>1` 区分 fixed/varlen，五个被测对象共享同一批输入；chunk_kda 的对照配置（`use_gate/qk_l2norm/beta_sigmoid_in_kernel=True`、`lower_bound=-5`、`transpose_state_layout=True`）正是 FLA 自动分发到 flashkada 的那套参数。
- bench 测的是端到端包装层（beta 转置 + 前缀和 + K1 + K2），ncu 测单个 kernel，两者互补；ncu.sh 用 `-k "regex:_flash_kda_fwd_(prepare|recurrence)"` + `--kernel-name-base function` 一次抓两个重模板 kernel，并刻意排除前缀和 kernel。
- `generate_benchmark_md.py` 以正则解析 bench 的 stdout（脆弱契约：改 print 即断链），只取 fp32 state 口径渲染六列对比表；报告显示 varlen 下加速比更高（H20 最高 2.29×），而对 GDN 优势小甚至在 GB200 H=64 fixed 落后（0.96×）。
- BENCHMARK_H20.md 的 Command 行记录的旧脚本名 `generate_benchmark_hopper_h20_md.py` 已在 d2ff19a 改名为 `generate_benchmark_md.py`——报告是历史生成物，读时要对照演化。

## 7. 下一步学习建议

- 下一讲 [u3-l11]（FLA 后端接入）正好接上本讲的伏笔：基准里 chunk_kda 的那套参数如何触发 `kda.chunk_kda -> flashkda` 的自动分发，以及 `FLA_FLASH_KDA=0` 回退对比。
- [u3-l12]（二次开发实践）会把本讲的 bench + ncu + exact-match 测试组合成「改一处 → 重编译 → 回归 → 基准」的消融流程，本讲的工具链就是其中的测量环节。
- 建议继续精读的源码：`benchmarks/ncu.sh` 抓到的两个 kernel 的注册属性源头——[csrc/smxx/fwd_kernel1.cuh:L120](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel1.cuh#L120) 的 `__launch_bounds__(NumThreads, 8)` 与 [csrc/smxx/fwd_launch.cu:L146-L216](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_launch.cu#L146-L216) 的两次启动配置，把 ncu 报告里的每个 Launch Statistics 字段对应回源码。
