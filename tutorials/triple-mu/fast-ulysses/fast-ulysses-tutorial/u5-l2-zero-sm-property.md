# 零 SM 性质的来源与测法

## 1. 本讲目标

u1-l1 把「零 SM 传输」列为本项目第一条特性，但那只是断言；u5-l1 读懂了 `launch_a2a_ce` 的结构——远端拷贝串行在 xfer 流、自份额走调用者流——却没有回答「为什么这样分」。本讲补上这一步：

1. 说清「零 SM」这句话的**准确边界**：它是**跨链路的远端拷贝（peer copy）**的性质，不是整个算子的性质；同设备拷贝（自份额、copy-out）不跨链路，会与计算竞争。
2. 解释 copy-out 为什么是「遗留的同设备拷贝」，以及 `out=`（来自 `empty_output()`）为什么是消除它的论据。
3. 掌握 `benchmark/bench_a2a.py --mode zerosm` 的双臂 A/B 构造：两臂各自匹配的 GEMM 链、两个判读指标（GEMM 自身慢化、成对墙钟比），以及为什么**任何一列单独看都不算数**。

## 2. 前置知识

- **SM（Streaming Multiprocessor）与 block slot**：GPU 上的计算单元。一个 CUDA kernel 以线程块（block）为单位被调度到 SM 上；如果 GEMM 已经占满了全部 SM，一个后续 kernel 拿不到 block slot，只能排队。这就是「SM-resident 集合通信」在计算密集段的困境。
- **拷贝引擎（Copy Engine，CE / DMA 引擎）**：GPU 上独立于 SM 的专用搬运硬件。`cudaMemcpyAsync` 的跨设备拷贝通常由 CE 执行，不需要 SM 参与——这是「零 SM」的物理来源。
- **同设备拷贝的竞争**：即使一次 device-to-device 拷贝本身不占 SM 块，它的读和写都落在本 rank 的内存系统（HBM/L2）里，而 GEMM 正在 saturate 同一套内存系统。所以本仓库对它的描述是「与计算**竞争**」——[include/fast_ulysses/transfer.hpp:19-21](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/include/fast_ulysses/transfer.hpp#L19-L21) 说它 "competes with it for the same memory system"，[benchmark/bench_a2a.py:31-34](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/benchmark/bench_a2a.py#L31-L34) 说它 "competes with compute for SMs"。两种措辞并存，含义相同：它不是免费的。
- **A/B 对照实验**：把「要检验的因素」之外的一切固定，只改一个变量。zerosm 模式的两臂唯一的区别是**目的地在哪里**（对端窗口 vs 本地内存）。
- **承自 u5-l1**：xfer 流（组级传输流）、调用者流、`emit`、CopyOp、ready/done 事件汇合。本讲只复用这些结论，不再重推。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [src/transfer.cu](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/transfer.cu) | 传输实现。文件头注释与流分配注释是「零 SM」性质的代码内表述（核心） |
| [include/fast_ulysses/transfer.hpp](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/include/fast_ulysses/transfer.hpp) | `launch_a2a_ce` 的文档注释，把性质归属讲得最明确 |
| [docs/design.md](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/design.md) | 设计论证：零 SM 的理由、两个遗留同设备拷贝、zerosm A/B 的方法论（核心） |
| [benchmark/bench_a2a.py](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/benchmark/bench_a2a.py) | `run_zerosm` 的完整实现与两列判读的头部说明（核心） |
| [src/bindings.cc](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc) | `copy_out` 与 `run()`：copy-out 这一遗留同设备拷贝的所在 |
| [docs/benchmark.md](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/benchmark.md) | 已发布数字；明确说明 zerosm 尚无公开测量结果 |
| [tools/exclusive.sh](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/tools/exclusive.sh) | 独占执行 wrapper：测量数字有效性的前提 |

## 4. 核心概念与源码讲解

### 4.1 远端拷贝不占 SM：这条性质的来源与归属

#### 4.1.1 概念说明

「零 SM」不是营销词，而是一条可以精确陈述的硬件性质：**跨越 NVLink 链路的 peer 拷贝由拷贝引擎（DMA）执行，不消耗 SM 资源**。它带来两层后果：

1. **不抢 block slot**：GEMM 占满所有 SM 时，一个 SM-resident 的集合通信内核（经典 NCCL 形态）拿不到块必须等待；CE 传输根本不需要块，照常进行。
2. **可与计算完全重叠**：因为传输走的是另一套硬件（CE + NVLink），计算走 SM + 本地内存，两者没有共同瓶颈。

但要立刻加上限定：这是**远端拷贝**的性质。本 rank 的调用里还混着不跨链路的同设备拷贝（4.2 节），它们不享受这条性质。

#### 4.1.2 核心流程

一次 `all_to_all_4d` 的传输阶段（u5-l1 已拆解过机制，这里按「资源占用」重新归类）：

```text
plan.ops（来自 build_plan，无指针）
   │
   ├─ peer != rank 的拷贝 ──→ 全部串行发射到 xfer 流
   │                          由 CE 执行，跨 NVLink，不占 SM
   │                          （每 rank 份额的 (ws-1)/ws 字节）
   │
   └─ peer == rank 的拷贝 ──→ 发射到调用者流
                              同设备拷贝，与本 rank 计算竞争内存系统
                              （每 rank 份额的 1/ws 字节）
```

两条流由 ready/done 事件汇合（u5-l1 的 4.4 节）。注意这个分割**正是按「是否跨链路」划的**——流的分配与资源占用的分界是同一条线。

#### 4.1.3 源码精读

`src/transfer.cu` 的文件头注释，是这条性质在代码里的最短表述：

```cpp
// CE (copy-engine) transfer path for the 4D all-to-all: pitched cudaMemcpy2D/3DAsync straight into
// the peers' window addresses. The copies that cross a link run on the DMA engines and take no
// SMs, so they proceed while compute holds every SM rather than waiting for a block slot. This
// rank's own share does not cross a link and does compete; see transfer.hpp.
```

[src/transfer.cu:1-4](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/transfer.cu#L1-L4)：跨链路的拷贝由 DMA 引擎执行、不占 SM，因此当计算占满每个 SM 时它们照常进行，而不是等一个 block slot；本 rank 自身的份额不跨链路、**会**竞争——细节见 transfer.hpp。

`include/fast_ulysses/transfer.hpp` 的文档注释把归属说得更斩钉截铁：

```cpp
/// The zero-SM property is a property of the REMOTE copies: a peer copy overlaps concurrent compute,
/// while a same-device copy competes with it for the same memory system. This rank's own share is a
/// same-device copy, and so is the copy-out on the copying path.
```

[include/fast_ulysses/transfer.hpp:19-21](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/include/fast_ulysses/transfer.hpp#L19-L21)：零 SM 是**远端**拷贝的性质；peer 拷贝可与并发计算重叠，同设备拷贝则与它竞争同一套内存系统。本 rank 自份额是一次同设备拷贝，拷贝路径上的 copy-out 也是。

`docs/design.md` 给出动机与实验事实：

> Peer windows are written with plain `cudaMemcpy2D/3DAsync` into addresses obtained from torch symmetric memory. That uses the copy engines and **no SMs**, which is the point: an SM-resident collective cannot get a block slot while GEMMs hold every SM, and this one does not need one.
>
> Under a concurrent GEMM chain of the same duration, a **peer** copy overlaps it completely while a **same-device** copy runs at close to full competition with it.

[docs/design.md:8-16](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/design.md#L8-L16)：在等长的并发 GEMM 链下，peer 拷贝与之**完全重叠**，同设备拷贝则以接近**完全竞争**的方式运行——这句就是 zerosm 基准要复现的实验事实。

流分配处的注释（u5-l1 读过结构，这里看理由）：

```cpp
// ONE STREAM for the remote copies, since they all leave through the same egress and separate
// streams would only make them contend. This rank's OWN share crosses no link, so it can run
// alongside them on the caller's stream -- hence the separate emit below.
```

[src/transfer.cu:82-85](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/transfer.cu#L82-L85)：远端拷贝共用一条 xfer 流（同一出口，多流只会互相争）；自份额不跨链路，可与之并行地跑在调用者流上。对应的发射代码在 [src/transfer.cu:102-116](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/transfer.cu#L102-L116)：XOR 枚举与非 2 幂补漏扫描把 peer 拷贝发到 `xfer`，最后一行 `emit(rank, stream)` 把自份额发到调用者流。

#### 4.1.4 代码实践

**实践目标**：把「零 SM」这句话的限定词找全，确认自己引用的是仓库的准确表述而不是转述。

**操作步骤**（纯源码阅读，无需 GPU）：

1. 打开 [include/fast_ulysses/transfer.hpp:19-21](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/include/fast_ulysses/transfer.hpp#L19-L21)，抄下这句英文原文。
2. 打开 [src/transfer.cu:1-4](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/transfer.cu#L1-L4)，对照两处表述，列出它们共同包含的限定条件。
3. 写下一行中文备注（示例）：「零 SM = 跨链路拷贝经 DMA 引擎、不占 SM block slot；限定于 remote copies；自份额与 copy_out 除外」。

**需要观察的现象**：两处注释都在「不占 SM」之后**立刻**补一句关于自份额/同设备拷贝的例外，没有一个地方只写半句。

**预期结果**：你的备注里应包含三个要素——性质属于远端拷贝、机制是 DMA 引擎、存在两类同设备例外。若漏了例外，重读 [docs/design.md:8-16](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/design.md#L8-L16)。

#### 4.1.5 小练习与答案

**练习 1**：如果对外宣传时把特性写成「这个 all-to-all 算子不占 SM」，错在哪里？

**答案**：错在把「远端拷贝的性质」扩大成了「整个算子的性质」。算子路径里还有两个屏障内核（单块 kernel，构造上就是 SM 工作）和两类同设备拷贝（自份额、拷贝路径上的 copy-out）。准确说法是传输中的 peer 拷贝不占 SM；端到端零 SM 还需 `out=` 去掉 copy-out（见 4.2 节与 [benchmark/bench_a2a.py:446-463](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/benchmark/bench_a2a.py#L446-L463) 的理由）。

**练习 2**：为什么设计文档说「SM-resident collective cannot get a block slot while GEMMs hold every SM」？这与拷贝引擎有何不同？

**答案**：以 kernel 形态实现的集合通信需要被调度成线程块才能运行；GEMM 占满全部 SM 时它只能排队，形成计算与通信的假串行。CE 传输由独立的 DMA 硬件执行，根本不参与 SM 调度，所以 GEMM 占满 SM 不影响它（[docs/design.md:8-16](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/design.md#L8-L16)）。

**练习 3**：自份额只占每 rank 份额的 \( 1/\text{ws} \)，为什么代码还要特意把它放到另一条流？

**答案**：因为它不跨链路、是同设备拷贝，与远端拷贝的资源模型不同：远端拷贝受制于链路出口（串行在 xfer 流反而避免争抢），自份额受制于本地内存系统（放到调用者流可与 xfer 流上的 CE 传输并行，见 [src/transfer.cu:82-85](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/transfer.cu#L82-L85)）。混在一条流里只会让 1/ws 的同设备拷贝阻塞在 (ws-1)/ws 的远端拷贝后面。

### 4.2 同设备拷贝竞争：自份额与 copy-out，以及 out= 的论据

#### 4.2.1 概念说明

一次调用里有两类拷贝**不**跨链路：

1. **自份额**（u5-l1 已见）：本 rank 发给自己的那 1/ws 字节，`emit(rank, stream)` 发到调用者流。这是算法定义固有的，去不掉，但只占份额的 \( 1/\text{ws} \)。
2. **copy-out**：拷贝路径的收尾——把窗口的密集前缀搬到调用者的输出张量。这不是算法定义需要的，只是「窗口是内部缓冲、调用者要自己的张量」这个默认形态带来的。**它可以被消除**：`out=` 传入 `empty_output()` 的缓冲时，peers 直写该缓冲，copy-out 整段消失（u4-l3）。

这就是 `docs/design.md` 的推理链：两个同设备拷贝里，自份额是固有开销，copy-out 是可消除开销，所以「这是 `out=` 最强的论据」。而 [docs/benchmark.md:142-145](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/benchmark.md#L142-L145) 进一步说：零拷贝路径「才是真正端到端零 SM 的那一条」——isolated 计时下的 1.14–1.28× 提速只是第一重收益，消除与并发计算的竞争是第二重，且是零 SM 叙事完整性的关键。

#### 4.2.2 核心流程

`run()` 的路径选择：

```text
run(group, input, mode, ..., out, role, stream, lend)
   │
   ├─ prepare(...)          → 校验 + 取 plan 与窗口（u6-l2 详讲）
   ├─ transfer_on_stream()  → 屏障 → launch_a2a_ce → 屏障
   │
   ├─ out_is_window == false（默认路径 / out= 普通张量）
   │      └─ copy_out(): 窗口 → output，一次平坦的 D2D 拷贝（同设备）
   └─ out_is_window == true（out= 来自 empty_output()，或 lend=True）
          └─ 无 copy_out：output 即窗口视图，peers 已直写
```

#### 4.2.3 源码精读

copy-out 的实现只有一条平坦拷贝：

```cpp
// Window -> the caller's tensor, ordered after the closing barrier on the same stream. Every
// rank's result is dense from the window base, so this is one flat device-to-device copy -- this
// rank's own share included, since it travels through the window like every peer's.
void copy_out(const Call& call, cudaStream_t stream, int rank)
{
    ULYSSES_CUDA_CHECK(cudaMemcpyAsync(call.output.data_ptr(),
                                       reinterpret_cast<const void*>(call.win->peer_ptrs[rank]),
                                       ...,
                                       cudaMemcpyDeviceToDevice,
                                       stream));
}
```

[src/bindings.cc:213-223](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L213-L223)：`cudaMemcpyDeviceToDevice`，同设备；注释说明每个 rank 的结果从窗口基址起稠密排列（u3-l2 买下的不变量），所以这只需一次平坦拷贝。

它在 `run()` 里被条件触发：

```cpp
const Call call = prepare(...);
transfer_on_stream(group, call, stream);
if (!call.out_is_window) {
    copy_out(call, stream, static_cast<int>(group->rank()));
}
return call.output;
```

[src/bindings.cc:225-241](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L225-L241)：只有当输出**不是**窗口时才 copy-out。`out=` 来自 `empty_output()`（或 `lend=True`）时 `out_is_window` 为真，这一段整体消失。

设计文档把这条线索接到零 SM 叙事上：

> Two same-device copies remain: this rank's own share of the transfer, and the copy-out on the copying path. That is the strongest argument for `out=` from `empty_output()`, which removes the second one.

[docs/design.md:12-16](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/design.md#L12-L16)：剩下两个同设备拷贝——自份额与拷贝路径上的 copy-out；`out=` 消除第二个，这是它最强的论据。基准文档的呼应见 [docs/benchmark.md:142-145](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/benchmark.md#L142-L145)：copy-out 是同设备拷贝，peer 拷贝不是；零拷贝路径才是真正端到端零 SM 的那条，并注明 `--mode zerosm` 就是那个 A/B。

#### 4.2.4 代码实践

**实践目标**：不看运行结果，仅凭源码判断四条调用路径中 copy-out 是否存在。

**操作步骤**（源码阅读型，无需 GPU）：

1. 读 [src/bindings.cc:225-241](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L225-L241) 的 `if (!call.out_is_window)`。
2. 对照 u4-l3/u4-l4 建立的路径分类，填下面这张表（每格写「有/无」）：

| 调用方式 | 有无 copy-out | 依据 |
|---|---|---|
| `all_to_all_4d(x, mode=0)`（默认） | ？ | `out` 为空 → 分配新张量 |
| `all_to_all_4d(x, mode=0, out=普通张量)` | ？ | 普通张量不是窗口 |
| `all_to_all_4d(x, mode=0, out=empty_output(...))` | ？ | `window_of` 按地址命中 |
| `all_to_all_4d_async(x, mode=0, lend=True)` | ？ | 结果是窗口密集前缀视图 |

3. 用 `Grep` 在 `src/bindings.cc` 中搜 `out_is_window` 的所有赋值点，核对答案。

**需要观察的现象**：`out_is_window` 的赋值逻辑只看「这个缓冲是不是本 group 的某条窗口、字节够不够」。

**预期结果**：四格依次为 有、有、无、无。这与 u4-l3（out= 三态）和 u4-l4（lend 出借窗口）的结论一致；也解释了 `--mode zerocopy` 基准测出的节省恰好等于 copy-out 阶段的时长（[docs/benchmark.md:111-145](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/benchmark.md#L111-L145)）。

#### 4.2.5 小练习与答案

**练习 1**：benchmark.md 说零拷贝路径的速度提升是 1.14–1.28×，又说「还有第二个理由，上面的时间看不到」。第二个理由是什么？

**答案**：copy-out 是同设备拷贝，与并发计算竞争 SM/内存系统；孤立计时只捕捉到拷贝本身的时间，捕捉不到真实训练循环里它对相邻计算内核的拖慢。消除它之后路径才「真正端到端零 SM」（[docs/benchmark.md:142-145](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/benchmark.md#L142-L145)）。

**练习 2**：既然自份额也是同设备拷贝，为什么不把自份额也设计成直写、彻底消灭同设备拷贝？

**答案**：自份额的源和目的地都在本 rank，它本来就不需要经窗口——经窗口是 u3-l2 的刻意设计（买下「窗口密集前缀即完整输出」的不变量，让 copy-out 退化成一次平坦拷贝、零拷贝路径直接返回视图）。在零拷贝路径上，自份额走窗口后落点就是最终缓冲，没有第二次搬运；拷贝本身（源→目的地这一次）是数据必须发生的移动，不属于「遗留」。

### 4.3 zerosm 基准：双臂 A/B 的构造

#### 4.3.1 概念说明

要检验「peer 拷贝不占 SM、同设备拷贝竞争」，直觉做法是：同一段拷贝分别发到对端窗口和本地内存，各压在一条 GEMM 链下面跑，看链是否变慢。`run_zerosm` 把这个直觉做成严谨实验，关键设计决定有四个：

1. **不经算子、直走 torch 对称内存**：算子路径里有两条屏障内核和一次 copy-out，全是构造上的 SM 工作，整call测量无法把任何效应归属给传输（[benchmark/bench_a2a.py:446-463](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/benchmark/bench_a2a.py#L446-L463) 的 docstring 亲自说明这一点）。两臂用同一个 `dst.copy_(src)`，唯一变量是**目的地**。
2. **两臂各自匹配链长**：同样的字节到 peer 比到本地慢好几倍，一条链无法同时匹配两臂。若共用一条链，对较短的同设备臂链会过长，而「比拷贝长的链会把慢化拉向 1.00×」——恰好废掉本该出现竞争的那一臂。
3. **拉长载荷到固定地板**：`LONG_MS = 20.0`——拷贝被拉伸到至少 20 ms，几十微秒的提交空隙只占千分之几（[benchmark/bench_a2a.py:75-81](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/benchmark/bench_a2a.py#L75-L81)）。
4. **SPMD 纪律**：拷贝次数 `creps` 与链长 `greps` 由 rank 0 算出后 `dist.broadcast`，因为每个 rank 必须发出相同的工作。

#### 4.3.2 核心流程

```text
准备
  src = symm_mem.empty(n) ; handle = symm_mem.rendezvous(src)
  两臂:
    peer 臂: dst = handle.get_buffer((rank+1)%ws, ...)   # 跨链路
    same 臂: dst = torch.empty(n)                        # 本地内存
  GEMM: lhs @ rhs, 8192×8192 fp16（GEMM_N=8192）

校准（每个 payload 循环一次）
  t_one[臂] = 单次拷贝时长
  creps = max over 臂 ( max(1, round(max(LONG_MS, t_one_gemm) / t)) )   # 按最快臂定，广播
  t_copy[臂] = creps 次拷贝独跑时长
  greps[臂] = max(1, round(t_copy[臂] / t_one_gemm))                    # 每臂一条匹配链，广播

采样（ROUNDS×INNER 交替）
  每臂: copy 独跑 c、链独跑 g、并发区 pair() → [墙钟, 链在区内的时长]
  臂与臂交替、并发与独跑交替；dist.barrier 保证全组同臂同刻

输出（仅 rank 0）
  copy | gemm | chain | pair | pair ratio | full comp | gemm slowdown
```

#### 4.3.3 源码精读

两臂的构造——唯一区别是目的地：

```python
src = symm_mem.empty(n, dtype=torch.bfloat16, device=dev)
src.fill_(1.0)
handle = symm_mem.rendezvous(src, pg)
arms = [("same", torch.empty(n, dtype=torch.bfloat16, device=dev))]
if ws > 1:
    arms.insert(0, ("peer", handle.get_buffer((rank + 1) % ws, (n,), torch.bfloat16)))
```

[benchmark/bench_a2a.py:563-570](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/benchmark/bench_a2a.py#L563-L570)：peer 臂的目的地是 rank (r+1)%ws 的对称内存窗口（拷贝跨链路），same 臂是本 rank 自己 `torch.empty` 的普通张量；`ws == 1` 时 peer 臂根本不存在。

拷贝次数的校准与广播：

```python
t_one = [median_ms(alone(enqueue(src, dst, 1)), args.iters, args.warmup) for _, dst in arms]
creps = max(max(1, round(max(LONG_MS, t_one_gemm) / t)) for t in t_one)
plan = torch.tensor([creps], device=dev, dtype=torch.int64)
dist.broadcast(plan, 0)
```

[benchmark/bench_a2a.py:579-584](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/benchmark/bench_a2a.py#L579-L584)：按**最快**的臂计算次数，保证最短的那臂也至少持续 `LONG_MS`（注释在 [benchmark/bench_a2a.py:572-578](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/benchmark/bench_a2a.py#L572-L578)：同样字节到 peer 要慢好几倍，一个次数无法让两臂等长；设计尺寸的载荷单次拷贝短于一个 GEMM，链长不能低于 1，所以两臂都要拉伸）。结果广播，保证 SPMD 一致。

每臂一条匹配链：

```python
calib = {a: median_ms(alone(c), INNER, 1) for a, c in copies.items()}
plan = torch.tensor([max(1, round(calib[a] / t_one_gemm)) for a in copies], ...)
dist.broadcast(plan, 0)
greps = dict(zip(copies, plan.tolist()))
chains = {a: gemm_chain(greps[a] if mine else 0) for a in copies}
```

[benchmark/bench_a2a.py:598-606](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/benchmark/bench_a2a.py#L598-L606)：链长按该臂拷贝独跑时长折算成 GEMM 个数，向上至少 1，同样广播。注释点明动机：链比拷贝长会把慢化拉向 1.00×，共用一条链会最狠地打击本该移动的 same 臂。

并发区 `pair()` 的计时细节：

```python
def pair(copy_go, gemm_go):
    w = Stopwatch(3)
    w.mark()
    cps.wait_stream(cur)
    gemm_go()          # 链先入队
    w.mark()           # mark 在链后：读出链在区内的自身时长
    copy_go()          # 拷贝在自己的流上
    cur.wait_stream(cps)
    w.mark()
    parts = w.read()
    return [sum(parts), parts[0]]
```

[benchmark/bench_a2a.py:498-517](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/benchmark/bench_a2a.py#L498-L517)：链在调用者流、拷贝在独立流 `cps`； Stopwatch 的第二个 mark 落在链后，于是 `parts[0]` 单独量出「链在并发区内的自身时长」（即慢化分子），`sum(parts)` 是整个并发区的墙钟。docstring（[benchmark/bench_a2a.py:490-517](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/benchmark/bench_a2a.py#L490-L517)）解释链先入队的原因：后入队者要晚启动「主机提交第一段所花的时间」，链只有几次 launch 而拷贝有几百次，链先入队使这段裸奔最小。

采样与交替：

```python
samples = {name: [] for name in copies}
for _ in range(ROUNDS):
    for name in copies:
        dist.barrier()
        c = median_ms(alone(copies[name]), INNER, 1)
        g = median_ms(chains[name], INNER, 1)
        samples[name].append([c, g] + median_of(lambda: pair(copies[name], chains[name]), INNER, 1))
```

[benchmark/bench_a2a.py:612-620](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/benchmark/bench_a2a.py#L612-L620)：每个上报数字都取自这个循环——臂与臂交替，臂内并发区与两个独跑测量交替（若基线在循环前一次性测好，整轮漂移会直接灌进慢化列）；barrier 保证全组同一时刻处于同一臂，这才让 all-pairs 行真的是 all-pairs 测量。最终打印在 [benchmark/bench_a2a.py:624-637](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/benchmark/bench_a2a.py#L624-L637)。

GEMM 的形状与地板常量：

```python
GEMM_N = 8192
LONG_MS = 20.0
ROUNDS, INNER = 5, 5
```

[benchmark/bench_a2a.py:75-81](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/benchmark/bench_a2a.py#L75-L81)：注释说明 8192² 的 fp16 方阵 GEMM 在每台机器上都是 square fp16，两臂处处可比；`LONG_MS` 是**选定的**地板而非测出的噪声水平。GEMM 链与拷贝入队的定义在 [benchmark/bench_a2a.py:464-488](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/benchmark/bench_a2a.py#L464-L488)（`gemm_chain(reps)` 反复 `lhs @ rhs`；`enqueue` 把 `dst.copy_(src)` 循环放进 `cps` 流）。设计尺寸载荷的计算在 [benchmark/bench_a2a.py:519-522](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/benchmark/bench_a2a.py#L519-L522)：`args.batch * (s_total // ws) * heads * d * 2`，即一个 rank 交给集合通信的字节数（wan-720p 形状）。

#### 4.3.4 代码实践

**实践目标**：在纸上走一遍校准算术，确认理解 `creps` 与每臂 `greps` 的来历。

**操作步骤**（纸面推演，无需 GPU）：

1. 假设（示例数据，非实测）：单次 GEMM \( t_{\text{one-gemm}} = 1.5\,\text{ms} \)；单次拷贝 peer 臂 \( 2.0\,\text{ms} \)、same 臂 \( 0.5\,\text{ms} \)。
2. 按 [benchmark/bench_a2a.py:580](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/benchmark/bench_a2a.py#L580) 计算 `creps`：对每臂算 \( \max(1,\ \mathrm{round}(\max(20.0,\ 1.5)/t)) \)，再取两臂最大值。
3. 由此得两臂拉伸后的拷贝独跑时长（次数 × 单次），再按 [benchmark/bench_a2a.py:598-606](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/benchmark/bench_a2a.py#L598-L606) 折算 `greps`。
4. 把结果填进表：

| 量 | peer 臂 | same 臂 |
|---|---|---|
| creps（两臂共用） | ? | ? |
| 拉伸后拷贝时长 | ? | ? |
| greps（链内 GEMM 数） | ? | ? |

**需要观察的现象**：`creps` 由**较快**的 same 臂决定；两臂移动的字节数相同，但链长差数倍。

**预期结果**：`creps = max(round(21.5/2.0)=11, round(21.5/0.5)=43) = 43`；peer 臂拷贝 ≈ 86 ms、`greps ≈ 57`；same 臂拷贝 ≈ 21.5 ms、`greps ≈ 14`。核对点：same 臂时长 ≥ `LONG_MS = 20`。（示例算术，实测数值待本地验证。）

#### 4.3.5 小练习与答案

**练习 1**：为什么两臂不能共用一条 GEMM 链？

**答案**：同样字节到 peer 比到本地内存慢好几倍，等字节下两臂拷贝时长不同；一条共用链必然对某一臂过长，而比拷贝长的链有一段裸奔在独跑速度上，把慢化拉向 1.00×。受害最重的恰是较短、本该显现竞争的 same 臂（[benchmark/bench_a2a.py:592-597](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/benchmark/bench_a2a.py#L592-L597) 注释、[docs/design.md:24-27](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/design.md#L24-L27)）。

**练习 2**：`creps` 与 `greps` 为什么都要 `dist.broadcast`，而不是各 rank 自行测量计算？

**答案**：基准也是 SPMD 程序，每个 rank 必须发出相同的工作；各 rank 的本地测量带噪声，round 后可能不同，不同工作会让 all-pairs 行不再是 all-pairs 测量。用 rank 0 的答案广播给全组，与 u4 系列的「共享量做决策」纪律同源。

**练习 3**：为什么实验不走 `group.all_to_all_4d` 而直走 `dst.copy_(src)` + 对称内存？

**答案**：算子路径里还有两条屏障内核和一次 copy-out，全是构造上的 SM 工作；测整个调用无法把任何效应归属给传输。两臂都用裸的 `dst.copy_(src)`，唯一差异是目的地，归属才干净（[benchmark/bench_a2a.py:447-455](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/benchmark/bench_a2a.py#L447-L455)、[docs/design.md:27-28](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/design.md#L27-L28)）。

### 4.4 两列判读法：GEMM 慢化 × 成对墙钟比

#### 4.4.1 概念说明

实验产出两个指标，记号如下（\( t \) 均为中位数）：

- **GEMM 慢化** \( S = t_{\text{under}} / t_{\text{gemm}} \)：链在拷贝之下的自身时长，除以链独跑时长。它直接陈述「计算为自己的重叠付出了什么」——比 overlap 模式的 `hidden%` 更直接，因为 `hidden%` 受「恰好有多少计算可藏」的约束（[benchmark/bench_a2a.py:457-462](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/benchmark/bench_a2a.py#L457-L462)）。
- **成对比** \( R = t_{\text{pair}} / \max(t_{\text{copy}},\ t_{\text{gemm}}) \)：并发区墙钟除以两者独跑时长的较大者。完全重叠时 \( R \to 1 \)（墙钟 ≈ 较长者），完全串行/竞争时 \( R \to F \)。
- **完全竞争参照** \( F = (t_{\text{copy}} + t_{\text{gemm}}) / \max(t_{\text{copy}},\ t_{\text{gemm}}) \)：完美竞争（等价串行）会付出的代价，作为 \( R \) 的对照上限打印出来。

判读规则（本讲标题里的「两列」）：

| 观察到的组合 | 结论 |
|---|---|
| \( S \approx 1.00 \) 且 \( R \approx 1.00 \) | **拷贝没占 SM**：完全重叠 |
| \( S \approx 1.00 \) 且 \( R \approx F \) | **拷贝被链饿死**：它从未真正跑在链下面，什么也没证明 |
| \( S > 1 \)（链变慢） | 拷贝与计算竞争，不是零 SM |

关键陷阱：**饿死的拷贝同样把 \( S \) 留在 1.00×**——它压根没获得运行机会，链自然不受影响。所以慢化列单独看会得出假阴性；必须与成对比（及 \( F \)）一起读。此外还有稀释警告：拷贝比链短时，链未被覆盖的部分以独跑速度进行，两个指标都会被拉向 1.00×——所以**先读 copy 与 gemm 两列确认等长匹配**，再读比值（[benchmark/bench_a2a.py:547-548](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/benchmark/bench_a2a.py#L547-L548)）。

#### 4.4.2 核心流程

```text
读表顺序（zerosm 输出）
  1. copy 列 vs gemm 列      → 匹配是否成立（链 ≈ 拷贝时长）？
  2. gemm slowdown 列 (S)    → 链有没有变慢？
  3. pair ratio 列 (R)
     与 full comp 列 (F) 对照 → R 靠 1 还是靠 F？
  结论:
     peer 臂: S≈1.00 且 R≈1.00  → 零 SM 性质成立
     same 臂: S>1 或 R≈F        → 同设备拷贝竞争（对照）
```

头部注释在每次运行里都打印这套说明（[benchmark/bench_a2a.py:539-548](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/benchmark/bench_a2a.py#L539-L548)），run 的 header 里那句「NEITHER COLUMN IS THE RESULT ON ITS OWN」是这套方法的座右铭。

#### 4.4.3 源码精读

模块 docstring 里的两态判读：

```python
# zerosm
#     Does a copy take SMs? ... Read the GEMM's own
#     slowdown -- its time with the copy underneath it over its time alone -- together with the pair's
#     wall clock: 1.00x at a 1.00x pair ratio is "that copy used no SMs", while 1.00x at full
#     competition is a copy the GEMM starved. Neither column decides it alone.
```

[benchmark/bench_a2a.py:47-53](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/benchmark/bench_a2a.py#L47-L53)：1.00× 慢化配 1.00× 成对比 = 「那次拷贝没用 SM」；1.00× 慢化配完全竞争 = 拷贝被 GEMM 饿死。任何一列单独都不能下结论。

`run_zerosm` docstring 对「为什么慢化列更直接但不充分」的完整论证：

> The GEMM chain's own slowdown is a more direct statement than the pair's wall clock -- it is the compute's own cost, not bounded by how much compute happens to be available, which is what --mode overlap's hidden% is. It is not sufficient on its own: a copy the chain starved never ran under it and leaves the slowdown at 1.00x too, which is why the pair ratio and the full-competition reference are printed next to it and why the header says to read both.

[benchmark/bench_a2a.py:457-462](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/benchmark/bench_a2a.py#L457-L462)：慢化是计算自身的代价、不受可用计算量约束（对比 `hidden%`）；但不充分——被饿死的拷贝同样留下 1.00×，所以要并排打印成对比与完全竞争参照。

三个量的最终计算：

```python
t_copy, t_gemm, t_pair, t_under = (statistics.median(r[k] for r in samples[name]) for k in range(4))
longer = max(t_copy, t_gemm)
full = (t_copy + t_gemm) / longer
print(f"... {t_pair:8.3f} {t_pair / longer:10.2f}x {full:9.2f}x {t_under / t_gemm:13.2f}x")
```

[benchmark/bench_a2a.py:624-637](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/benchmark/bench_a2a.py#L624-L637)：四个采样量取中位数后，`pair ratio = t_pair/longer`、`full comp = (t_copy+t_gemm)/longer`、`gemm slowdown = t_under/t_gemm`——正是 4.4.1 的 \( R \)、\( F \)、\( S \)。

设计文档对读法的呼应：

> The chain's own slowdown and the pair's wall clock are read together — a copy the chain starved reads 1.00× on the first column too — and the run's header says so. Two GPUs are the minimum.

[docs/design.md:29-31](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/design.md#L29-L31)：两列一起读；最少需要两块 GPU。`ws == 1` 时基准自己承认测不了：头部会打印「THE PEER ARM IS NOT MEASURED and this run does not test the claim」（[benchmark/bench_a2a.py:549-555](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/benchmark/bench_a2a.py#L549-L555)），此时只有 same 臂和 GEMM 基线是真实的。

**一个必须诚实交代的现状**：[docs/benchmark.md:324-329](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/benchmark.md#L324-L329) 明确写着 zerosm 模式写于上一轮测量之后，「no table here carries it」——仓库里**没有**已发布的 zerosm 数字，下一轮 `collect.sh` 才会带上它。所以任何具体数值都标「待本地验证」。

#### 4.4.4 代码实践

**实践目标**：不看机器，只看表格就能把每一行归类为「重叠 / 饿死 / 竞争」。

**操作步骤**：

1. 逐行读下面的示例表（**示例数据，纯属教学构造，非实测**），按 4.4.2 的顺序判读：

| payload | arm | copy | gemm | chain | pair | pair ratio | full comp | gemm slowdown |
|---|---|---|---|---|---|---|---|---|
| A | peer | 20.1 | 20.3 | 14 | 20.6 | 1.01× | 1.99× | 1.00× |
| B | same | 20.2 | 20.1 | 14 | 39.0 | 1.94× | 1.99× | 1.28× |
| C | same | 20.0 | 20.2 | 14 | 39.5 | 1.96× | 2.00× | 1.00× |

2. 对每行写一句话结论。
3. 用 [benchmark/bench_a2a.py:539-548](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/benchmark/bench_a2a.py#L539-L548) 的头部说明核对你的措辞。

**需要观察的现象**：A 与 C 的慢化列完全相同（1.00×），结论却相反——区分它们的是成对比靠 1 还是靠 full comp。

**预期结果**：A = 重叠（零 SM 成立）；B = 竞争（链慢化 1.28× 且墙钟接近串行和）；C = 饿死（拷贝从未真正并发，慢化列无信息量）。C 行就是「饿死的拷贝也读 1.00×」陷阱的具象。

#### 4.4.5 小练习与答案

**练习 1**：peer 臂读到 `slowdown = 1.00×`，可以直接宣布零 SM 成立吗？

**答案**：不行。若拷贝被链饿死（从未运行在链下面），慢化同样是 1.00×。必须看成对比：\( R \approx 1 \) 才是重叠，\( R \approx F \) 是饿死（[benchmark/bench_a2a.py:47-53](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/benchmark/bench_a2a.py#L47-L53)）。

**练习 2**：为什么 `hidden%`（overlap 模式）不能替代 GEMM 慢化作为零 SM 的证据？

**答案**：`hidden% = (serial − concurrent)/a2a_alone` 度量「藏住了多少通信」，但它受**可用计算量**约束——benchmark.md 的 overlap 表显示 H200 比 Blackwell 藏得多只是因为 GEMM 更长。GEMM 慢化是计算自身的代价，不依赖计算量多少，是更直接的陈述（[benchmark/bench_a2a.py:457-462](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/benchmark/bench_a2a.py#L457-L462)、[docs/benchmark.md:147-165](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/benchmark.md#L147-L165)）。

**练习 3**：同一 payload 的 same 臂出现 `slowdown = 1.02×`、`pair ratio = 1.30×`、`full comp = 1.99×`。怎么解读？

**答案**：链几乎没慢（1.02×），但并发区墙钟明显超过较长者的 1.3 倍，既不在 1 也不在 F——部分竞争/部分重叠的中间态。结合「拷贝与链时长需匹配」的检查（copy、gemm 两列），最可能是拷贝略短于链，尾部裸奔稀释了指标；应先核对两列匹配再下结论（[benchmark/bench_a2a.py:547-548](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/benchmark/bench_a2a.py#L547-L548)）。

## 5. 综合实践

把本讲全部内容串成一次完整的判读（或预测）。规格来自大纲：写出两臂各自匹配的 GEMM 链与两个判读指标，有条件时在独占卡上实测一次。

**实践目标**：对 zerosm 模式做出「结构说明 → 运行/预测 → 两列判读」的完整闭环。

**操作步骤**：

1. **结构说明**（无 GPU 必做）。写一段不超过 15 行的笔记，覆盖：
   - 两臂的目的地各来自哪里：peer 臂 `handle.get_buffer((rank+1)%ws, ...)`、same 臂 `torch.empty`（[benchmark/bench_a2a.py:563-570](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/benchmark/bench_a2a.py#L563-L570)）。
   - 两臂链长的确定：`greps[arm] = round(calib[arm]/t_one_gemm)` 并广播（[benchmark/bench_a2a.py:598-606](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/benchmark/bench_a2a.py#L598-L606)）。
   - 两个判读指标：\( S = t_{\text{under}}/t_{\text{gemm}} \) 与 \( R = t_{\text{pair}}/\max(t_{\text{copy}},t_{\text{gemm}}) \)，对照 \( F \)（[benchmark/bench_a2a.py:624-637](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/benchmark/bench_a2a.py#L624-L637)）。
2. **运行**（需 ≥2 张 NVLink GPU，最少两卡——[docs/design.md:31](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/design.md#L31)）。必须在独占 wrapper 下跑，共享 GPU 上的数字不是数字（[tools/exclusive.sh:1-12](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/tools/exclusive.sh#L1-L12)）：

   ```bash
   ./tools/exclusive.sh 0,1 -- torchrun --nproc_per_node=2 \
       benchmark/bench_a2a.py --mode zerosm
   ```

   跑完检查 wrapper 末尾的 `VERDICT`：只有 `EXCLUSIVE` 的数字可用，`CONTENDED` 直接丢弃（[tools/exclusive.sh:96-113](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/tools/exclusive.sh#L96-L113)）。
3. **预测**（无 GPU 时替代步骤 2）。按 [docs/design.md:12-16](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/design.md#L12-L16) 的断言写一张预测表：peer 臂 \( S \approx 1.00× \)、\( R \approx 1.00× \)（完全重叠）；same 臂 \( S > 1 \) 和/或 \( R \to F \)（接近完全竞争）。

**需要观察的现象**：

- 头部先打印十几行说明，包括判读口诀与「NEITHER COLUMN IS THE RESULT ON ITS OWN」。
- 每个 payload 两行（peer / same），两个 payload（设计尺寸 wan-720p/rank 与 64 MiB），各分 `flows=1`（只有 rank 0 写）与 `flows=ws`（每 rank 写邻居）两组。
- copy 与 gemm 两列应大致相等（链按拷贝匹配）；chain 列是 GEMM 个数。

**预期结果**：实测数字**待本地验证**——[docs/benchmark.md:324-329](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/benchmark.md#L324-L329) 明确说明该模式写于上一轮测量之后，仓库尚无已发布的 zerosm 数字。判读时严格按「先看 copy/gemm 匹配，再联合读 \( S \) 与 \( R \)（对照 \( F \)）」的顺序；peer 臂若同时给出 \( S \approx 1.00× \) 且 \( R \approx 1.00× \)，即可写出结论「该拷贝未占用 SM」，并把这一结论回接到 4.2 的论证：copy-out 作为同设备拷贝不享受此性质，故 `out=` 是端到端零 SM 的必要一步。

## 6. 本讲小结

- 「零 SM」是**跨链路远端拷贝**的性质：它们由拷贝引擎（DMA）执行，不需要 SM block slot，可与占满 SM 的 GEMM 完全重叠（[src/transfer.cu:1-4](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/transfer.cu#L1-L4)、[include/fast_ulysses/transfer.hpp:19-21](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/include/fast_ulysses/transfer.hpp#L19-L21)）。
- 同设备拷贝不跨链路，与计算竞争同一套内存系统。调用里残留两个：固有的自份额（1/ws 字节，走调用者流）与可消除的 copy-out（`cudaMemcpyDeviceToDevice`，[src/bindings.cc:213-241](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L213-L241)）。
- `out=` 来自 `empty_output()` 时 `out_is_window` 为真、copy-out 整段消失——这是设计文档所称「`out=` 最强的论据」，零拷贝路径才是真正端到端零 SM 的那条（[docs/design.md:12-16](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/design.md#L12-L16)、[docs/benchmark.md:142-145](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/benchmark.md#L142-L145)）。
- zerosm 基准是双臂 A/B：同一 `dst.copy_(src)`，一次写 peer 窗口、一次写本地内存；两臂各自匹配等长 GEMM 链、载荷拉伸到 ≥20 ms、次数与链长广播保 SPMD；不经算子以免屏障与 copy-out 污染归属。
- 两列判读法：\( S \approx 1.00× \) 且 \( R \approx 1.00× \) 才是零 SM；\( S \approx 1.00× \) 且 \( R \approx F \) 是拷贝被饿死。任何一列单独都不能下结论，先核对 copy/gemm 匹配再读比值。
- 仓库当前**没有**已发布的 zerosm 数字（模式写于上一轮测量之后）；实测须在 `tools/exclusive.sh` 独占验证下进行，最少两块 GPU。

## 7. 下一步学习建议

- **u5-l3（标志位屏障）**：本讲反复提到「两条屏障内核是构造上的 SM 工作」。下一讲逐行读 `src/barrier.cu` 的自旋内核与设备侧 epoch，理解这两块微秒级内核为什么是可接受的残留，以及它们与 CUDA graph 的关系。
- **u9-l4（基准方法论）**：本讲只解剖了 zerosm 一个模式；stages/overlap/zerosm/sweep/link 的完整判读法、`hidden%` 的离散度问题与独占执行纪律在那一讲系统展开。
- **延伸阅读**：[docs/design.md](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/design.md) 的「What this rests on that is not documented」一节——远端拷贝可见性假设与 zerosm 同属「本项目最依赖且厂商未写明」的领域；以及 [docs/benchmark.md](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/benchmark.md) 的「What is not measured」清单，它是理解哪些断言尚待数字支撑的地图。
