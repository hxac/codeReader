# 速度基准 ds4-bench

## 1. 本讲目标

本讲讲解 ds4 自带的专用吞吐基准工具 `ds4-bench`。它和上一单元的 `ds4-eval`（能力回归套件）不同：`ds4-eval` 关心「模型答得对不对」，`ds4-bench` 只关心「引擎跑得快不快」。

学完本讲，你应当能够：

- 理解 `ds4-bench` 为什么测量「上下文前沿（frontier）处的瞬时吞吐」，而不是整段平均。
- 说清它在每个前沿「保存快照 → 生成探测 → 恢复快照 → 继续 prefill」这一循环的必要性。
- 掌握 KV 内存快照（snapshot）这一探针的工作原理，以及它与磁盘 KVC 序列化的同源关系。
- 看懂 CSV 输出的六列含义，并用 `speed-bench/plot_speed.py` 把它画成双 Y 轴 SVG。

## 2. 前置知识

阅读本讲前，你应当已经建立以下认知（来自前置讲义）：

- **prefill 与 decode 是两件事**（u1-l5）：prefill 一次性填 KV 缓存，决定首 token 延迟；decode 自回归生成，决定后续 token 速度。二者吞吐量级差十几倍，本讲要分别测它们。
- **分块 prefill 与 chunk 边界**（u6-l1）：长 prompt 被切成 `DS4_METAL_PREFILL_CHUNK`（默认 4096）大小的块逐块填，chunk 边界同时是 KV checkpoint 推进点、logit 写出点。`ds4-bench` 测量「前沿增量」正是建立在这套增量 prefill 之上。
- **session 的 checkpoint 与 sync**（u2-l3）：`ds4_session_sync` 当活 checkpoint 是目标 prompt 的前缀时，只评估后缀（增量 prefill），否则整段重建。
- **DSV4 payload**（u8-l3）：session 的 KV 状态可以序列化成一段字节流，写盘即 `.kv` 文件的核心 payload。本讲的「内存快照」用的就是同一段序列化逻辑。

几个本讲会用到的术语：

- **frontier（前沿）**：一个目标上下文长度，如 2048、4096。基准会沿一组递增的前沿逐点测量。
- **瞬时吞吐（instantaneous throughput）**：只测「从上一个前沿到当前前沿这一小段」的 t/s，而不是从头平均。
- **内存快照（in-memory snapshot）**：把活 session 的 KV 状态序列化进一块内存缓冲，用于事后恢复。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| `ds4_bench.c` | 基准工具的全部前端逻辑，约 680 行 | 主循环、前沿步进、快照探针、CSV 输出 |
| `ds4.c` | 引擎核心，含快照实现 | `ds4_session_save_snapshot` / `load_snapshot` |
| `ds4.h` | 引擎公共边界 | snapshot 结构体与 sync 的增量语义 |
| `speed-bench/README.md` | 基准使用说明 | 运行命令与绘图命令 |
| `speed-bench/plot_speed.py` | 纯标准库 SVG 绘图脚本 | 双 Y 轴折线图 |
| `speed-bench/m4_max.csv` | 一份真实基准数据样本 | 观察速度随上下文的衰减曲线 |

构建关系上，`ds4-bench` 由 `ds4_bench.o + ds4_help.o + CORE_OBJS` 链接而成（见 [Makefile:61-62](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/Makefile#L61-L62)），其中 `CORE_OBJS` 就是 u1-l3 讲过的引擎核心对象集。也就是说 `ds4-bench` 复用了完整的推理引擎，只是换了一个「只测速、不聊天」的前端。

---

## 4. 核心概念与源码讲解

### 4.1 前沿步进与增量测量

#### 4.1.1 概念说明

一个朴素的基准会把一段长 prompt 从头 prefill 到尾，再用总时间算一个「平均 t/s」。这种做法有两个问题：

1. **平均会掩盖衰减**。上下文越长，attention 要扫描的 KV 越多，prefill 和 decode 都会变慢。把 2k 和 64k 的耗时混在一起平均，你就看不到这条衰减曲线——而这条曲线恰恰是工程师调优时最关心的东西。
2. **整段重建昂贵**。每次都从 0 重新 prefill 到 64k，绝大部分算力花在「已经测过」的前缀上。

`ds4-bench` 的解法是：**只加载一次模型，沿一组递增的前沿走同一条固定 token 序列，每个前沿只测「最新加入的这一小段」**。文件头注释把这件事说得很清楚：

> The benchmark walks one fixed token sequence to configurable context frontiers, measuring only the newest prefill interval at each frontier.

参见 [ds4_bench.c:5-13](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_bench.c#L5-L13)。

「只测最新一段」之所以成立，靠的是 `ds4_session_sync` 的增量语义：当上一个前沿的 checkpoint 恰好是当前 prompt 的前缀时，sync 只评估后缀（见 [ds4.h:246-249](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.h#L246-L249)）。于是第 N 个前沿的 prefill 工作量 = `frontier_N - frontier_{N-1}` 个 token，而不是 `frontier_N` 个。

#### 4.1.2 核心流程

前沿步进有两种模式，由 `next_frontier` 决定（[ds4_bench.c:427-440](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_bench.c#L427-L440)）：

- **线性步进**（默认 `--step-mul 1.0`）：`next = cur + step_incr`。`--step-incr 2048` 就得到 2048、4096、6144… 这样均匀的栅格。
- **几何步进**（`--step-mul F > 1`）：`next = ceil(cur * F)`。适合一次扫描覆盖小上下文到大上下文（例如 1k→1M），既不漏掉小上下文的细节，又不在大上下文处采样过密。

主循环的形状如下（伪代码）：

```text
previous = 0
for frontier = ctx_start; ; frontier = next_frontier(frontier):
    prefix = prompt[0 : frontier]                 # 只是切前缀，不复制内容
    prefill_sec = 计时( ds4_session_sync(prefix) ) # 增量 prefill，只算后缀
    prefill_tokens = frontier - previous          # 本前沿真正新填的 token 数

    （见 4.2：保存快照 → 生成探测 → 恢复快照）

    prefill_tps = prefill_tokens / prefill_sec    # 瞬时 prefill 吞吐
    输出一行 CSV
    previous = frontier
    if frontier >= ctx_max: break
```

瞬时吞吐的定义就是一个简单的除法：

\[
\text{prefill\_tps}=\frac{\text{prefill\_tokens}}{\text{prefill\_sec}},\qquad
\text{prefill\_tokens}=\text{frontier}-\text{previous}
\]

注意 `prefill_tokens` 是「区间长度」而非「累计长度」，这正是「瞬时」二字的本意。

#### 4.1.3 源码精读

默认参数在 `parse_options` 的结构体初始化里（[ds4_bench.c:168-178](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_bench.c#L168-L178)）：`ctx_start=2048`、`ctx_max=32768`、`step_incr=2048`、`gen_tokens=128`、`step_mul=1.0`、默认模型 `ds4flash.gguf`。这些就是 README 示例命令之外、不传任何参数时的行为。

主循环里 prefill 的计时与 token 计数是本模块的核心（[ds4_bench.c:594-609](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_bench.c#L594-L609)）：

```c
for (int frontier = cfg.ctx_start; ; frontier = next_frontier(&cfg, frontier)) {
    ds4_tokens prefix = { .v = prompt.v, .len = frontier, .cap = frontier };

    const double prefill_t0 = bench_now_sec();
    if (ds4_session_sync(session, &prefix, err, sizeof(err)) != 0) { ... }
    const double prefill_t1 = bench_now_sec();
    const double prefill_sec = prefill_t1 - prefill_t0;
    const int prefill_tokens = frontier - previous;
```

这里有两个关键细节：

- `prefix` 用的是 `prompt.v`（同一块缓冲），只改 `len/cap`，零拷贝切前缀。
- 计时窗口 `prefill_t0..prefill_t1` 严格包裹 `ds4_session_sync`。注意快照保存/恢复（4.2 讲）被故意放在这个窗口**之外**，不会污染 prefill 测量——文件头注释明确写了「Snapshot save/restore time is intentionally outside both timing windows」（[ds4_bench.c:11-13](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_bench.c#L11-L13)）。

prompt 文件必须足够长：基准要求分词后的 token 数 ≥ `--ctx-max`，否则直接报错退出（[ds4_bench.c:546-554](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_bench.c#L546-L554)）。仓库自带的 `speed-bench/promessi_sposi.txt`（一部公版小说）就是为了提供足够长的 token 序列。

#### 4.1.4 代码实践

**实践目标**：用仓库自带的真实数据，验证「prefill 速度随上下文衰减」这条曲线，并理解为何它一定是单调下降的。

**操作步骤**：

1. 直接阅读真实样本 [speed-bench/m4_max.csv](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/speed-bench/m4_max.csv)（这是一台 M4 Max 上用默认 `--step-incr 2048 --gen-tokens 128` 跑出来的，列含义见 4.3）。
2. 观察第 2 列 `prefill_tokens` 恒为 2048（每个前沿只测最新一段），第 1 列 `ctx_tokens` 从 2048 步进到 65536。
3. 观察第 3 列 `prefill_tps`：从 2048 上下文处的 **343.76**，一路降到 65536 处的 **204.96**。

**需要观察的现象**：

- `prefill_tokens` 在每一行都等于 `step_incr`，证实了「只测区间、不测累计」。
- `prefill_tps` 随 `ctx_tokens` 增长单调下降——因为 attention 代价随上下文增长，且 prefill chunk 内每填一个 token 都要对已积累的 KV 做注意力。

**预期结果**：prefill t/s 大约从 ~344 跌到 ~205，衰减约 40%。

**关于亲自运行 `ds4-bench`**：它需要加载真实 GGUF（`ds4flash.gguf`）并占用一台 GPU/大内存机器，本环境无法运行，**待本地验证**。运行命令见 [speed-bench/README.md:7-15](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/speed-bench/README.md#L7-L15)。在没有模型时，你可以用 `--help` 干跑参数解析，确认默认值与 [ds4_bench.c:168-178](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_bench.c#L168-L178) 一致。

#### 4.1.5 小练习与答案

**练习 1**：为什么基准用 `prefill_tokens = frontier - previous` 而不是 `frontier` 来算 t/s？用 `previous` 变量的生命周期解释。

> **答**：上一个前沿结束时 `previous` 被更新为该前沿值（[ds4_bench.c:673](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_bench.c#L673)），且活 session 的 checkpoint 此时正好停在该前缀（因为 4.2 的快照恢复把它「倒回」了前沿）。于是下一个前沿的 sync 只评估 `frontier - previous` 个新 token。若用 `frontier` 当分母，会把已经测过的前缀也计入，得到的是「平均」而非「瞬时」。

**练习 2**：默认 `--step-mul 1.0` 时，`--step-incr` 必须为正（见 [ds4_bench.c:306-309](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_bench.c#L306-L309)）；但 `--step-mul 2` 时 `--step-incr 0` 却合法。为什么？

> **答**：几何步进完全由 `ceil(cur * step_mul)` 推进（[ds4_bench.c:433-437](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_bench.c#L433-L437)），不依赖 `step_incr`，所以 `step_incr` 此时被忽略，置 0 也无妨。线性步进则必须靠 `step_incr` 才能前进，故必须为正。

---

### 4.2 KV 内存快照探针

#### 4.2.1 概念说明

要测某个前沿处的「生成（decode）速度」，必须在那个前沿上跑 `gen_tokens` 步自回归生成。但问题是：**生成会把活 session 的 checkpoint 往前推进 `gen_tokens` 步**，超出当前前沿。下一个前沿要做「增量 prefill」时，要求活 checkpoint 恰好停在前沿（作为前缀），现在它却被生成过程推走了。

解决方案有两种：

- **(a) 内存快照**：生成前把活 KV 状态序列化进一块内存缓冲，生成后再恢复回去，把 checkpoint「倒回」前沿。
- **(b) 重新 sync**：直接再次 `ds4_session_sync(prefix)`，让引擎把多出来的 `gen_tokens` 个 token 回退掉。

单机基准用 (a)，因为它是一次纯内存的字节级恢复，不重新跑 GPU 图，既快又干净。这就是本模块要讲的「快照探针」。

关键认知：**这个内存快照，和写到磁盘 `.kv` 文件里的 DSV4 payload 是同一段序列化字节**。换句话说，`ds4-bench` 把 u8-l3 讲的「会话序列化」当作一个临时探针来用：保存即序列化、恢复即反序列化，只不过目标是内存缓冲而不是磁盘文件。

#### 4.2.2 核心流程

每个前沿的完整处理顺序（[ds4_bench.c:601-671](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_bench.c#L601-L671)）是：

```text
1. 计时开始 → ds4_session_sync(prefix) [增量 prefill] → 计时结束   ← prefill 测量窗口
2. ds4_session_save_snapshot(snap)        ← 把前沿处 KV 存进内存（窗口外）
3. 计时开始 → 重复 gen_tokens 次 argmax+eval [生成探测] → 计时结束 ← gen 测量窗口
4. ds4_session_load_snapshot(snap)        ← 把 KV 倒回前沿（窗口外）
5. 输出一行 CSV（含 prefill_tps、gen_tps、kvcache_bytes）
6. previous = frontier；进入下一前沿
```

两个时序要点：

- **快照存/取都在两个计时窗口之外**，所以再慢也不影响 prefill/gen 数字。这一点文件头注释专门强调（[ds4_bench.c:11-13](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_bench.c#L11-L13)）。
- **生成探测用 `argmax_excluding(eos)`**：贪婪取最大 logit 的 token，但排除 EOS，保证探测不会中途「自然结束」，必定跑满 `gen_tokens` 步。这也是确定性的，所以跨运行可比。

还有两个特例分支（[ds4_bench.c:646-660](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_bench.c#L646-L660)）：

- **`--gen-tokens 0`（纯 prefill 基准）**：跳过快照、生成、恢复三步，活 session 直接停在前沿，下一前沿继续增量 prefill。
- **分布式 coordinator**：快照尚不支持（见下文），改用方案 (b)——再次 `ds4_session_sync(prefix)` 把状态「回放」回前沿。

#### 4.2.3 源码精读

快照本身是个很简单的结构体，就是一段字节缓冲（[ds4.h:136-140](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.h#L136-L140)）：

```c
typedef struct {
    uint8_t *ptr;
    uint64_t len;
    uint64_t cap;
} ds4_session_snapshot;
```

保存实现（[ds4.c:24849-24890](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L24849-L24890)）的核心是：先按需扩容 `snap->ptr`，再用 `fmemopen` 把这块内存当成「文件」交给 `ds4_session_save_payload`——也就是写盘 `.kv` 用的同一个序列化函数：

```c
FILE *fp = fmemopen(snap->ptr, (size_t)bytes, "wb");
const int rc = ds4_session_save_payload(s, fp, err, errlen);
...
snap->len = bytes;
```

恢复实现（[ds4.c:24892-24917](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L24892-L24917)）是镜像操作：`fmemopen(..., "rb")` + `ds4_session_load_payload`。这证实了「内存快照 == DSV4 payload」的同源关系——保存到磁盘和保存到内存，序列化字节完全一致。

分布式分支明确不支持快照（[ds4.c:24854-24856](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L24854-L24856)），所以基准在分布式时退回到重新 sync 的回放路径。

回到主循环，生成探测循环本身（[ds4_bench.c:624-642](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_bench.c#L624-L642)）值得一看：

```c
const double gen_t0 = bench_now_sec();
for (int i = 0; i < cfg.gen_tokens; i++) {
    if (ds4_session_pos(session) + 1 >= ds4_session_ctx(session)) { ... 超出 ctx 报错 ... }
    const int token = ds4_session_argmax_excluding(session, eos);  // 贪婪且排除 EOS
    if (ds4_session_eval(session, token, err, sizeof(err)) != 0) { ... }
}
const double gen_t1 = bench_now_sec();
```

`pos + 1 >= ctx` 的检查保证生成不会写出分配的上下文窗口——这也是为什么默认 `ctx_alloc = ctx_max + gen_tokens + 1`（[ds4_bench.c:314](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_bench.c#L314)）：要在最大前沿处还留出 `gen_tokens` 的生成空间。

顺带一提：CSV 的最后一列 `kvcache_bytes` 用的就是 `snap.len`（[ds4_bench.c:670](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_bench.c#L670)）。也就是说快照不止用于恢复，还顺手让基准成了 **KV 体积 profiler**——你能直接看到 KV 随上下文线性增长。

#### 4.2.4 代码实践

**实践目标**：用真实数据验证「KV 体积随上下文线性增长」，并理解为何生成速度也随上下文衰减（虽然比 prefill 慢得多）。

**操作步骤**：

1. 再次打开 [speed-bench/m4_max.csv](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/speed-bench/m4_max.csv)。
2. 看最后一列 `kvcache_bytes`：2048 处约 **52 MiB**，65536 处约 **926 MiB**。
3. 算一下：上下文从 2048→65536 放大 32 倍，KV 从 52→926 MiB 也大约放大 18 倍——注意不是纯线性 32 倍，因为压缩 KV 行是固定窗口/低频写入的（回顾 u4-l2 的 raw 滑窗 + 压缩行设计）。
4. 看第 5 列 `gen_tps`：从 **26.76** 缓慢降到 **22.92**，衰减约 14%。

**需要观察的现象**：

- 生成速度远低于 prefill（~25 t/s vs ~250 t/s），量级差约 10 倍。这正是 4.3 要用双 Y 轴绘图的原因。
- 生成速度的衰减（14%）比 prefill（40%）温和，因为每步 decode 只处理 1 个新 token，attention 代价随上下文增长但不像 prefill 那样每步都填一整块。

**预期结果**：能口述「prefill 跌得多、gen 跌得少、KV 大致线性涨」这三条规律。

**关于 `kvcache_bytes` 在分布式时为 0**：见 [ds4_bench.c:670](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_bench.c#L670) 的三元 `distributed ? 0 : snap.len`——因为分布式不存快照，没有 `snap.len` 可报。

#### 4.2.5 小练习与答案

**练习 1**：如果取消「先存快照再生成」，直接生成后进入下一前沿，会发生什么？

> **答**：生成会把 checkpoint 从 `frontier` 推到 `frontier + gen_tokens`。下一前沿（如 `frontier + 2048`）的 `prefix` 长度大于当前 checkpoint，且二者前缀关系不再是「checkpoint 是 prefix 的前缀」——`ds4_session_sync` 的前缀复用会失配，可能整段重建，prefill 测量就失真了。快照恢复把 checkpoint 精确倒回 `frontier`，才能保证下一前沿走干净的增量 prefill。

**练习 2**：为什么 `--gen-tokens 0` 模式下连快照都不用存？

> **答**：不生成就不推进 checkpoint，活 session 自然停在前沿，下一前沿直接增量 prefill 即可，没有「倒回」的需求（[ds4_bench.c:646-647](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_bench.c#L646-L647)）。这也意味着纯 prefill 模式下 `kvcache_bytes` 列会是 0（没有快照）。

**练习 3**：内存快照和磁盘 `.kv` 文件的 payload 有什么关系？

> **答**：字节完全一致。两者都由 `ds4_session_save_payload` 产出、`ds4_session_load_payload` 消费，差别只在落点：快照用 `fmemopen` 写进内存缓冲（[ds4.c:24877](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L24877)），`.kv` 文件用普通 `fopen` 写盘。所以 `kvcache_bytes` 列也等于对应磁盘检查点的 payload 字节数。

---

### 4.3 CSV 输出与绘图

#### 4.3.1 概念说明

基准的输出是一份流式 CSV，每个前沿一行。流式（每行写完就 `fflush`）的好处是：长时间扫描时你可以实时 `tail` 看进度，或者中途 Ctrl+C 也能保留已测的行。

CSV 共六列（表头见 [ds4_bench.c:584](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_bench.c#L584)）：

| 列 | 含义 |
| --- | --- |
| `ctx_tokens` | 当前前沿的上下文长度 |
| `prefill_tokens` | 本前沿新填的 token 数（= 区间长度） |
| `prefill_tps` | 本前沿 prefill 瞬时吞吐（token/秒） |
| `gen_tokens` | 生成探测的 token 数（= `--gen-tokens`） |
| `gen_tps` | 本前沿生成瞬时吞吐 |
| `kvcache_bytes` | 快照字节数（= KV payload 体积；分布式为 0） |

`plot_speed.py` 把这份 CSV 画成 SVG。它的设计哲学和基准一致——「保持同样直接」：一条 prefill 线、一条 generation 线，因为两者量级差十几倍，**用两条独立的 Y 轴**（左轴 prefill、右轴 generation），否则生成线会被压扁成贴底的一条平线。

#### 4.3.2 核心流程

绘图脚本只依赖 Python 标准库（`csv`、`html`、`math`），不需要 matplotlib，流程为：

```text
read_points(csv):
    要求列 {ctx_tokens, prefill_tps, gen_tps} 存在，且至少 2 行
    按 ctx_tokens 排序，返回 [(ctx, prefill_tps, gen_tps), ...]

render_svg(rows):
    左轴最大值 = nice_ceil(max(prefill_tps) * 1.05)   ← 预留 5% 顶部留白
    右轴最大值 = nice_ceil(max(gen_tps) * 1.05)       ← 独立刻度
    把两组点分别投影成两条 polyline，左轴用蓝、右轴用红
    生成 SVG 字符串，写盘
```

`nice_ceil` / `nice_step` 把轴的最大值和刻度间距向上取整到「人类友好」的数字（1/2/2.5/5/10 × 10^k），避免出现 926 这样的丑刻度。

#### 4.3.3 源码精读

脚本的模块文档字符串直接说明了双 Y 轴的理由（[speed-bench/plot_speed.py:1-8](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/speed-bench/plot_speed.py#L1-L8)）：

> one line for incremental prefill t/s, one line for greedy generation t/s, and separate y axes because the two values live on very different scales.

读取校验要求三列必须存在、且至少两行数据，否则报错退出（[speed-bench/plot_speed.py:58-81](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/speed-bench/plot_speed.py#L58-L81)）——这和基准「至少要两个前沿才有折线意义」相符。

两个 Y 轴各自取最大值并向上取整（[speed-bench/plot_speed.py:121-122](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/speed-bench/plot_speed.py#L121-L122)）：

```python
prefill_max = nice_ceil(max(prefill_values) * 1.05)
gen_max = nice_ceil(max(gen_values) * 1.05)
```

两条折线分别用蓝（`#2563eb`，prefill）和红（`#dc2626`，generation）绘制（[speed-bench/plot_speed.py:136-139](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/speed-bench/plot_speed.py#L136-L139)、[speed-bench/plot_speed.py:179-180](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/speed-bench/plot_speed.py#L179-L180)）。水平网格线和左轴刻度用 prefill 尺度，右轴刻度单独标注 generation 尺度。

运行方式见 [speed-bench/README.md:21-29](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/speed-bench/README.md#L21-L29)：默认在 CSV 旁边生成 `<stem>_ts.svg`。仓库里已经收录了几份真实结果，如 `speed-bench/m4_max_ts.svg`、`speed-bench/pro_model_m3_ultra_ts.svg`。

CSV 的写出代码（[ds4_bench.c:662-671](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_bench.c#L662-L671)）用了一个小防御：分母为 0 时吞吐记为 0，避免除零：

```c
prefill_sec > 0.0 ? (double)prefill_tokens / prefill_sec : 0.0,
```

#### 4.3.4 代码实践

**实践目标**：亲手把仓库自带的 CSV 画成 SVG，验证双 Y 轴折线图的形状，并对照衰减规律。

**操作步骤**：

1. 确保有 Python 3（脚本只用标准库，无需 `pip install`）。
2. 运行：
   ```sh
   python3 speed-bench/plot_speed.py speed-bench/m4_max.csv --title "M4 Max t/s"
   ```
3. 打开生成的 `speed-bench/m4_max_ts.svg`（或在浏览器里查看）。

**需要观察的现象**：

- 上面一条（蓝，左轴）是 prefill，从 ~344 跌到 ~205；下面一条（红，右轴）是 generation，从 ~27 跌到 ~23。
- 两条线都随 `ctx size` 单调下降，但 prefill 跌得更陡。
- 左轴刻度（数百）和右轴刻度（数十）量级不同，正好各自舒展开。

**预期结果**：得到一张清晰的双 Y 轴折线图，能看到 prefill 与 generation 各自的衰减曲线。这条命令本身**只读 CSV、不碰模型**，可以在任何装了 Python 3 的机器上运行——本环境未实际执行，**待本地验证**，但脚本逻辑（[speed-bench/plot_speed.py:208-223](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/speed-bench/plot_speed.py#L208-L223)）保证它一定产出 SVG 文件。

#### 4.3.5 小练习与答案

**练习 1**：为什么脚本坚持用双 Y 轴，而不是把两条线画在同一个 Y 轴上？

> **答**：prefill（数百 t/s）和 generation（数十 t/s）差约 10 倍。同轴时 generation 线会被压到接近底部，看不出细节（[speed-bench/plot_speed.py:6-7](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/speed-bench/plot_speed.py#L6-L7)）。双轴让两者各自填满纵向空间。

**练习 2**：脚本要求至少 2 行数据（[speed-bench/plot_speed.py:77-78](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/speed-bench/plot_speed.py#L77-L78)）。如果基准只跑了一个前沿（`--ctx-start == --ctx-max`），绘图会怎样？

> **答**：CSV 只有 1 行数据，`read_points` 抛 `SystemExit` 报错「need at least two data rows」。这也提示你：基准的意义在于看「曲线」，单个点是画不出趋势的。

---

## 5. 综合实践

把本讲三个模块串起来，完成下面这个「读图说话」任务：

1. **读数据**：打开 [speed-bench/m4_max.csv](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/speed-bench/m4_max.csv)，挑出 `ctx_tokens=2048`、`32768`、`65536` 三行。
2. **画图**：用 `python3 speed-bench/plot_speed.py speed-bench/m4_max.csv --title "M4 Max t/s"` 生成 SVG 并查看。
3. **解释快照**：对照 [ds4_bench.c:601-671](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_bench.c#L601-L671)，写一段话说明：为什么在每个前沿都要「保存快照 → 生成 128 token → 恢复快照」？如果省掉快照直接进入下一前沿，第 2 个前沿的 `prefill_tps` 会失真成什么（提示：sync 会怎样处理一个被生成过程推走的 checkpoint）？
4. **读规律**：用你画出的图和挑出的三行数据，用一两句话总结 prefill 速度、generation 速度、KV 体积三者随上下文的变化趋势，并解释为什么 generation 的衰减比 prefill 温和。
5. **（进阶，待本地验证）** 在有模型和 GPU 的机器上，分别用 `--step-incr 2048`（线性）和 `--step-mul 2`（几何）各跑一次到 `--ctx-max 65536`，对比两份 CSV 的前沿分布密度，并思考几何步进在扫描 1M 上下文时的优势。

预期产出：一份三行数据表 + 一张 SVG + 一段关于「快照为何不可省」的解释 + 三条趋势规律。

## 6. 本讲小结

- `ds4-bench` 测量的是**前沿处的瞬时吞吐**而非整段平均：每个前沿只测 `frontier - previous` 这一小段的 prefill，靠 `ds4_session_sync` 的增量前缀复用实现。
- 前沿步进支持**线性**（`--step-incr`）与**几何**（`--step-mul`）两种模式，分别适合均匀栅格和大跨度扫描。
- 生成探测前用**内存快照**保存活 KV、探测后恢复，把 checkpoint 精确「倒回」前沿，保证下一前沿的增量 prefill 干净；快照存/取都在计时窗口外，不污染测量。
- 内存快照与磁盘 `.kv` 文件的 DSV4 payload **同源**——同一段序列化字节，只是落点是内存还是磁盘；`kvcache_bytes` 列因此兼作 KV 体积 profiler。
- 生成探测用 `argmax_excluding(eos)` 的**贪婪确定性**路径，跨运行可比；分布式因不支持快照而退回重新 sync 的回放路径。
- 输出是六列**流式 CSV**，`plot_speed.py` 用纯标准库把它画成**双 Y 轴 SVG**，因为 prefill 与 generation 量级差约十倍。

## 7. 下一步学习建议

- **回到能力侧**：本讲只测速，下一站建议读 u10-l4（`ds4-eval`），看 ds4 如何用「能力回归套件」保证改内核后「答得对」——它与本讲的「跑得快」一起，构成贡献前的两条回归底线（见 u11-l4）。
- **深挖快照底层**：4.2 把快照当作黑盒探针。若想理解 `ds4_session_save_payload` 到底写了哪些字节，读 u8-l3（DSV4 payload 序列化），它会拆开 13 个 u32 头、checkpoint tokens、logits、逐层 KV 与 compressor frontier。
- **分布式基准**：本讲提到分布式不支持快照、退回回放。完整的分布式测量姿势见 README 的分布式章节与 u9-l4，理解 `ds4-bench` 作为 coordinator 时为何要 `wait_distributed_route`（[ds4_bench.c:459-485](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_bench.c#L459-L485)）。
- **动手贡献数据**：若你有一台 README 速度表里没列出的机器，按 [speed-bench/README.md:17-19](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/speed-bench/README.md#L17-L19) 跑一次并以 `你的硬件.csv` 命名提交 PR，这是 ds4 社区欢迎的低门槛贡献。
