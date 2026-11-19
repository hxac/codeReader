# fast-ulysses 是什么：用拷贝引擎搬运的 Ulysses all-to-all

## 1. 本讲目标

读完本讲，你应该能够：

1. 说出 fast-ulysses 解决的核心问题：长序列视频 DiT（Diffusion Transformer）训练中，Ulysses 序列并行的两次 all-to-all 集合通信是关键路径，本项目把这一次 4D all-to-all 做成 torch 自定义算子。
2. 解释它的三条关键特性：**零 SM 传输**（peer 拷贝走拷贝引擎，不占计算单元）、**重排即步长**（序列/头的排列被编码成拷贝的源/目的步长，两个 permute 内核消失）、**不均匀分片是一般路径**（偶数均分只是特例）。
3. 对照 README 的限制清单（NVLink 单机、`world_size` 1–8、dtype 白名单与 16 字节对齐），判断自己的场景是否适用。
4. 按 docs/benchmark.md 的方法学读懂性能对比表，并亲手复算其中几个数字。

本讲不要求你写任何 CUDA 代码，也不需要多张 GPU——唯一需要跑代码的实践在单进程（CPU 或单卡）即可完成。

## 2. 前置知识

用最通俗的语言过一遍本讲用到的概念，已有基础的读者可以跳过。

- **DiT（Diffusion Transformer）**：一类用 Transformer 做去噪的扩散模型。视频生成的序列长度动辄数万 token（本项目的基准形状之一是 75827），注意力计算和通信都按序列长度增长。
- **序列并行（sequence parallelism）**：把一个长序列切成若干段，每张 GPU（每个 rank）持有其中一段。与按 batch 切分不同，序列并行让单卡放不下的长序列得以训练。
- **rank / world_size / 进程组**：分布式训练里每个参与进程是一个 rank，进程总数是 world_size（下文记作 ws）。`torch.distributed` 的进程组（process group）是它们互相通信的载体。本项目的算子是**集合通信**（collective）：所有 rank 一起调用同一个操作。
- **all-to-all**：每个 rank 都向其他所有 rank 发送一段数据、也从所有 rank 接收一段数据的通信模式。Ulysses 用它来完成「序列分片 ⇄ 头分片」的交换。
- **Ulysses 序列并行**：注意力前，一次 all-to-all 把「每卡一段序列、全部头」换成「全部序列、每卡一部分头」——注意力只依赖每个头内的序列，于是每卡可以独立算完注意力；再用第二次 all-to-all 换回来。两次通信把注意力计算包在中间。下一讲（u1-l2）会详细展开。
- **张量的形状（shape）与步长（stride）**：PyTorch 张量是一块一维内存加上「每个维度走一步跨多少元素」的描述，即步长。`x.permute(...)` 只改步长，不搬数据，几乎零成本（返回的是视图 view）；`x.contiguous()` 则要把数据按新布局**真正搬一遍**，分配新内存并启动一个拷贝内核。理解这一区别是理解本项目第二条特性的钥匙。
- **SM（Streaming Multiprocessor）**：GPU 上真正执行计算内核的单元，可以粗略理解为「GPU 的 CPU 核」。GEMM（矩阵乘）等计算内核会把 SM 吃满。
- **拷贝引擎（Copy Engine，CE，也叫 DMA 引擎）**：GPU 上独立于 SM 的专用搬运单元。`cudaMemcpyAsync` 一类的拷贝可以由 CE 执行，**完全不消耗 SM**——这就是本项目「计算和通信可以完全重叠」的硬件基础。
- **NVLink**：NVIDIA GPU 之间的高速互连总线，带宽远高于 PCIe。本项目的传输方式（跨步拷贝）在 NVLink 上高效，在 PCIe 上会严重退化，因此项目明确只支持单机 NVLink。
- **对称内存（symmetric memory）**：torch 提供的一种「每张卡都分配一块、且所有卡都能直接通过 NVLink 读写对方那块」的缓冲区。fast-ulysses 的传输就是拷贝引擎直接写进 peer 的对称内存地址。细节在第 4 单元展开，本讲只需知道这个名词。
- **bf16**：brain float 16，每个元素 2 字节，是本项目基准使用的 dtype。

## 3. 本讲源码地图

| 文件 | 角色 | 本讲使用方式 |
| --- | --- | --- |
| [README.md](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/README.md) | 项目门面：定位、三条特性、性能表、快速开始、API 一览、Limits、安装 | 精读特性与 Limits 两节 |
| [docs/benchmark.md](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/benchmark.md) | 全部性能数字的出处与测量方法学 | 精读方法节与四张主表 |
| [benchmark/bench_a2a.py](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/benchmark/bench_a2a.py) | 基准脚本，含 BASE（`torch.distributed` 路径）的真实实现 | 只读 `baseline_padded` 一段，作为实践任务的依据 |
| [docs/design.md](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/design.md) | 设计论证（为什么代码长这样） | 只读开头关于步长拷贝与不均匀分片的两段 |

顺带一提仓库顶层结构（u1-l4 会专门画阅读地图）：`python/` 是 Python 门面（`UlyssesGroup` 所在），`src/` 与 `include/` 是 C++/CUDA 实现，`test/` 分主机布局测试与多卡 worker，`benchmark/` 与 `tools/` 是基准与独占执行工具，`docs/` 是全部文档。本讲只触及 `docs/` 与 `benchmark/`。

## 4. 核心概念与源码讲解

### 4.1 零 SM 传输

#### 4.1.1 概念说明

传统上，集合通信由 NCCL 这类库用**运行在 SM 上的内核**实现。问题在于：长序列 DiT 训练里，SM 同时也是 GEMM/注意力拼命抢占的资源——通信内核一启动，计算就得让路，两者无法完全重叠。

fast-ulysses 的做法：不写通信内核，直接用带步长的 `cudaMemcpy2D/3DAsync` 把数据拷进各 peer 的对称内存地址。这类拷贝由**拷贝引擎（CE）**执行，而 CE 是独立于 SM 的硬件单元，所以通信期间 SM 仍然 100% 留给计算内核——README 把这条列为第一条特性「No SMs」。

有两个必须分清的细节：

1. **零 SM 是「peer 拷贝」的性质**。同设备（device-to-device）的普通拷贝依然由 SM 执行、会和计算竞争。本库 copying 路径最后的 `copy_out`（窗口 → 调用者张量）就是这样一段同设备拷贝，因此「端到端零 SM」要靠 `out=`（从 `empty_output()` 拿缓冲）去掉 copy_out 才成立。
2. 这个论断不是口号，项目专门写了 `--mode zerosm` 的 A/B 基准去测量它（见 4.1.3）。

#### 4.1.2 核心流程

把 BASE（`torch.distributed` 路径）与 fast-ulysses 各自的一次调用按「谁在执行」拆开：

```text
BASE（torch.distributed / sglang usp.py 路径）        fast-ulysses
──────────────────────────────────────────          ──────────────────────────────────────
① permute + contiguous   —— SM 上的拷贝内核          ① 屏障 barr_in（极短的自旋内核）
② all_to_all_single      —— 通信内核，也占 SM        ② peer 拷贝 ×(ws−1)  —— 拷贝引擎 CE，不占 SM
③ permute + contiguous   —— SM 上的拷贝内核             （本 rank 份额走调用者流，不跨链路）
                                                    ③ 屏障 barr_out
                                                    ④ copy_out —— 同设备拷贝，占 SM（out= 可去除）
```

注意对比的不对称性：BASE 的每一步都在 SM 上；fast-ulysses 只有屏障和 copy_out 碰 SM，而真正搬几百 MB 数据的 ② 完全在 CE 上。

#### 4.1.3 源码精读

README 开头一段话就是问题陈述：Ulysses 序列并行需要两次 all-to-all，长序列视频 DiT 下这两次集合通信就是关键路径——见 [README.md:L11-L15](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/README.md#L11-L15)，其中说明本库把它做成 `torch.ops.fast_ulysses.all_to_all_4d`，跑在 NVLink 单机上。

第一条特性原文在 [README.md:L19-L22](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/README.md#L19-L22)：peer 传输是直接射向 peer 对称内存地址的带步长 `cudaMemcpy2D/3DAsync`，因此跑在拷贝引擎上、计算内核保有全部 SM；括号里还给出了实测结论——peer 拷贝可与并发 GEMM 完全重叠，同设备拷贝不行，这正是 `out=` 更快的原因。

「零 SM 端到端」的精确表述在 [docs/benchmark.md:L142-L145](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/benchmark.md#L142-L145)：`copy_out` 是同设备拷贝，会和计算抢 SM，而 peer 拷贝不会；所以零拷贝路径（`out=`）才是真正端到端零 SM 的那条。

这个性质如何被测量：[benchmark/bench_a2a.py:L47-L53](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/benchmark/bench_a2a.py#L47-L53) 是 `zerosm` 模式的说明——同样字节、同样次数的 `dst.copy_(src)`，一次写进 peer 的对称内存窗口、一次写本卡内存，各压一条按自己时长配平的 GEMM 链，同时读「GEMM 自身的慢化」和「两臂墙钟比」两个指标。设计文档 [docs/design.md:L25-L31](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/design.md#L25-L31) 进一步说明为什么两臂要各配各的 GEMM 链：同样的字节到 peer 比到本机慢好几倍，一条链配不平两臂。

#### 4.1.4 代码实践

**实践目标**：看清自己硬件上「SM 数量」与「互连拓扑」这两个概念对应的实物，为后续所有讨论建立直观。

**操作步骤**（示例代码，有 GPU 时运行，无 GPU 时做阅读部分）：

```python
# 示例代码：查看本机 GPU 的 SM 数与显存
import torch

if torch.cuda.is_available():
    p = torch.cuda.get_device_properties(0)
    print(p.name, "| SM 数:", p.multi_processor_count, "| 显存 GB:", p.total_memory / 1e9)
else:
    print("本机无 CUDA 设备：跳过，改做下面的阅读任务")
```

再运行（或想象运行）`nvidia-smi topo -m`，找到 GPU 之间的 `NV#` 链路标记。项目自带的 `fast-ulysses doctor` 命令（见 [README.md:L95](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/README.md#L95)）打印的就是构建信息、设备与 NVLink 矩阵。

**需要观察的现象**：一张 H100 有 132 个 SM、H200 同为 132、B200 有 148（具体数值因卡而异，「待本地验证」）；`topo -m` 中 NVLink 直连或 NVSwitch 全互连的卡对会标 `NV`，仅有 `PIX/PHB`（走 PCIe）的卡对本库会拒绝。

**预期结果**：你把「SM = 计算单元」「CE = 独立搬运单元」「NVLink = 卡间高速路」三件事对上了号；并且理解为什么通信不占 SM 在「SM 被 GEMM 吃满」的训练里价值最大。

#### 4.1.5 小练习与答案

1. **问：peer 拷贝能与 GEMM 完全重叠，同设备拷贝不能，根本原因是什么？**
   答：peer 拷贝由拷贝引擎（CE）执行，CE 是独立于 SM 的硬件单元；同设备拷贝走 SM 上的内核，与 GEMM 竞争同一批执行资源和内存系统。依据 [README.md:L19-L22](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/README.md#L19-L22)、[docs/benchmark.md:L142-L145](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/benchmark.md#L142-L145)。
2. **问：既然传输零 SM，为什么说 copying 路径「不是端到端零 SM」？怎么补齐？**
   答：因为最后还有一步 `copy_out`（窗口 → 调用者张量）是同设备拷贝，占 SM。用 `empty_output()` 预先拿对称缓冲并经 `out=` 传入，peers 直写调用者缓冲，copy_out 消失，才是端到端零 SM。依据 [docs/benchmark.md:L142-L145](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/benchmark.md#L142-L145) 与 [README.md:L93](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/README.md#L93)。
3. **问：`zerosm` 基准为什么不能只用「两臂墙钟比」一个指标下结论？**
   答：因为被拷贝「饿瘦」的 GEMM 也可能恰好读到 1.00×；必须把 GEMM 自身慢化和成对墙钟比放在一起读，两列各自都不能单独定案。依据 [benchmark/bench_a2a.py:L47-L53](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/benchmark/bench_a2a.py#L47-L53)。

### 4.2 重排即步长

#### 4.2.1 概念说明

Ulysses 的 4D all-to-all 不是「把一整块连续内存原样发过去」：输入是 \((b, s_{\text{local}}, n_{\text{global}}, d)\)——本 rank 的序列段、全部的头；输出是 \((b, s_{\text{global}}, n_{\text{local}}, d)\)——全部序列、本 rank 的头。数据在内存里的排布必须随之重排。

BASE 路径的做法分三步：先 `permute + contiguous` 把数据物理重排成「按目的 rank 分段、段内连续」的平坦缓冲，再 `all_to_all_single` 原样搬运，收完又 `permute + contiguous` 重排回目标布局。两次重排各要把整个张量**完整读写一遍**，且都跑在 SM 上。

fast-ulysses 的洞察：`cudaMemcpy2D/3DAsync` 这类「带步长拷贝」（pitched copy）本身就允许源和目的各配各的步长——搬运引擎在逐行搬运时**顺带完成了重排**。于是序列/头的排列被表达成每个拷贝操作的源/目的步长，两个 permute 内核整个消失。README 第二条特性「Relayout for free」说的就是这件事，并量化为「约是 `torch.distributed` 路径开销的一半」。

#### 4.2.2 核心流程

先建立带步长拷贝的直觉。一次 2D 拷贝的参数大致是：每行拷 `width` 字节，共 `rows` 行，源每前进一行跨 `spitch` 字节、目的每前进一行跨 `dpitch` 字节：

\[ \text{src: } \text{addr} + r \cdot \text{spitch} \;\longrightarrow\; \text{dst: } \text{addr}' + r \cdot \text{dpitch}, \quad r = 0, 1, \dots, \text{rows}-1 \]

选取不同的 \(\text{spitch}\) 与 \(\text{dpitch}\)，同样的字节流落进完全不同的布局——重排于是成为搬运的「副产品」，不再需要独立的内核。

两条路径对比如下：

```text
BASE:   x ──permute+contiguous(SM)──> 平坦缓冲 ──all_to_all_single──> 平坦接收
        ──reshape+permute+contiguous(SM)──> 输出

OURS:   预先（纯主机）把交换算成一组 CopyOp{src_offset, dst_offset, pitch, width, rows}
        ──> 每个-op 变成一次 cudaMemcpy2D/3DAsync（CE 执行，重排内嵌在步长里）
```

关键差别：OURS 把「该搬哪些字节、按什么步长」的全部寻址**提前在主机上算完**（实现在 `src/a2a_plan.cc`，无 CUDA、无 torch，可在无 GPU 机器上测试），设备侧只剩纯搬运。

#### 4.2.3 源码精读

BASE 路径在项目里有真实可运行的实现——基准脚本里的 `baseline_padded`，见 [benchmark/bench_a2a.py:L140-L148](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/benchmark/bench_a2a.py#L140-L148)：

```python
def baseline_padded(x, pg, ws, recv) -> None:
    """usp.py mode 0: permute, flat all_to_all_single, permute. ..."""
    b, s_local, h_global, d = x.shape
    h_local = h_global // ws
    y = x.permute(2, 0, 1, 3).contiguous().flatten()        # 重排①：头提前，落成平坦发送缓冲
    dist.all_to_all_single(recv, y, group=pg)               # 搬运：平坦字节，不做重排
    z = recv.reshape(ws, h_local, b, s_local, d)
    z.permute(2, 0, 3, 1, 4).contiguous().reshape(b, s_local * ws, h_local, d)  # 重排②
```

对照文档对这条路径的定义：[docs/benchmark.md:L64-L66](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/benchmark.md#L64-L66) —— `BASE` 是 `torch.distributed` 的「permute、`all_to_all_single`、permute」；`raw` 是单独的 `all_to_all_single`（同样字节、无重排、结果布局不对，因此只是传输下限而非可选项）。

本库一侧的论述在 [docs/design.md:L33-L35](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/design.md#L33-L35)：序列/头的重排被表达为这些拷贝的源与目的步长，因此除了本来就必须发生的传输之外不花任何代价——这就是 BASE 的两个 permute 内核在本侧完全不存在的原因。README 特性原文见 [README.md:L23-L25](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/README.md#L23-L25)。

规模结论在 [README.md:L41-L43](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/README.md#L41-L43)：BASE 的 47–60% 是重排，在本路径中代价为零。设计文档还点出分层收益：[docs/design.md:L37-L39](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/design.md#L37-L39) —— 全部寻址都在 `src/a2a_plan.cc`，它不含 CUDA、torch 与通信库，所以布局契约可以在无 GPU 的机器上测试。

顺带看一眼「本侧」调用的样子，来自 README 快速开始（[README.md:L63-L68](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/README.md#L63-L68)）：

```python
b, s_local, heads, d = 1, 4096, 8, 128
ws = dist.get_world_size()
x = torch.randn(b, s_local, heads * ws, d, dtype=torch.bfloat16, device="cuda")

y = group.all_to_all_4d(x, mode=0)        # (b, s_local * ws, heads, d)
z = group.all_to_all_4d(y, mode=1)        # back to x's shape
```

一次调用，没有可见的 permute——它已经沉到拷贝步长里了。

#### 4.2.4 代码实践

**实践目标**：在单进程环境（CPU 或单卡）复现 BASE 路径的两次形状重排，实测 `permute + contiguous` 的真实开销，并亲手验证「permute 免费、contiguous 才搬数据」。

**操作步骤**（示例代码，保存为 `practice_relayout.py` 运行；只需 torch）：

```python
# 示例代码：单进程复现 BASE 的形状变换并计时
# 依据 benchmark/bench_a2a.py:140-148 的 baseline_padded
import time
import torch

ws = 8
b, s_local, h_global, d = 1, 4096, 40, 384   # 任务指定的形状；h_global=40 可被 ws=8 整除
h_local = h_global // ws
dev = "cuda" if torch.cuda.is_available() else "cpu"
x = torch.randn(b, s_local, h_global, d, dtype=torch.bfloat16, device=dev)
nbytes = x.numel() * x.element_size()
print(f"字节数: {nbytes/1e6:.1f} MB  (设备: {dev})")

def relayout_in(x):                       # BASE 重排①（bench_a2a.py:145）
    return x.permute(2, 0, 1, 3).contiguous().flatten()

def relayout_out(recv):                   # BASE 重排②（bench_a2a.py:147-148）
    z = recv.reshape(ws, h_local, b, s_local, d)
    return z.permute(2, 0, 3, 1, 4).contiguous().reshape(b, s_local * ws, h_local, d)

# 形状自检（不涉及通信，只验证布局逻辑）
y = relayout_in(x)
print("重排①后:", x.permute(2, 0, 1, 3).shape, "-> flatten:", tuple(y.shape))
out = relayout_out(y)
print("重排②后输出:", tuple(out.shape), "  期望: (1, 32768, 5, 384)")

# 计时：permute 只是改步长的视图，contiguous 才真正搬数据
def timed(fn, iters=50, warmup=8):
    for _ in range(warmup):
        fn()
    if dev == "cuda":
        torch.cuda.synchronize()
    t0 = time.perf_counter()
    for _ in range(iters):
        fn()
    if dev == "cuda":
        torch.cuda.synchronize()
    return (time.perf_counter() - t0) / iters * 1e3   # ms

t_view  = timed(lambda: x.permute(2, 0, 1, 3))
t_cont  = timed(lambda: x.permute(2, 0, 1, 3).contiguous())
print(f"permute(纯视图)      {t_view:.4f} ms")
print(f"permute+contiguous   {t_cont:.4f} ms   <- BASE 每次要付两次的开销")

# 下面这段注释是本实践要求你写下的结论：
#
# 「在 fast-ulysses 中，上面这两次 permute+contiguous 内核完全不存在（docs/design.md:33-35）：
#  序列/头的重排被表达为 peer 拷贝的源/目的步长，由拷贝引擎在'本来就要发生的传输'里顺带完成。
#  BASE 中它们约占整次调用的 47-60%（README.md:41-43），而且是 SM 上的内核，
#  会与训练中的 GEMM/注意力竞争计算资源。」
```

**需要观察的现象**：`t_view` 与 `t_cont` 相差几个数量级（permute 几乎为 0，contiguous 是毫秒级的数据搬运）；`重排①` 打印出 `(40, 1, 4096, 384)` 与 `(62914560,)`，`重排②` 输出 `(1, 32768, 5, 384)`。

**预期结果**：在 H100 一类的卡上，单次 permute+contiguous 约 0.1–0.2 ms 量级（125.8 MB，见 4.4 的 sweep 表 `s_local=4096` 行即此尺寸）；在 CPU 上同一操作会慢一个数量级以上，具体数值**待本地验证**——但「视图免费、contiguous 昂贵」的比值关系在任何设备上都成立。

#### 4.2.5 小练习与答案

1. **问：把 `x.permute(2,0,1,3).contiguous().flatten()` 里的 `contiguous()` 去掉，会省掉这次数据搬运吗？**
   答：不会。`flatten()` 对非连续张量无法返回视图，只能隐式拷贝一次——数据搬运一次不少，只是从显式变成隐藏。BASE 里显式写 `contiguous()` 是为了语义清楚，不是多余开销。
2. **问：为什么 `all_to_all_single` 之前必须把发送数据重排成「按目的 rank 分段且段内连续」的平坦缓冲？**
   答：`all_to_all_single` 的契约是收发两端各一个平坦张量，且发送缓冲按目的 rank 的顺序切成等长（偶均分时）段。4D 张量在原始布局下「发往同一 rank 的元素」并不连续，所以必须先物理重排。
3. **问：纸面推演：H200 上按 stages 表的比例，125.8 MB 的张量做一次 permute 大约多久？**
   答：H200 wan-720p 行（291 MB/rank）`perm_in` 0.358 ms，即约 0.358/291 ≈ 1.23 µs/MB；125.8 MB ≈ 0.155 ms。这只是按表内比例的外推，非实测（依据 [docs/benchmark.md:L72](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/benchmark.md#L72)）。

### 4.3 不均匀分片

#### 4.3.1 概念说明

序列并行要把全局序列长度 \(s\) 切成 ws 份。\(s\) 不一定能被 ws 整除——恰恰相反，真实工作负载几乎总不能：本项目基准的三个真实形状 \(s\) 都带着同一条 227 token 的文本尾巴。

BASE 路径用的 `all_to_all_single`（平坦、等长段模式）要求每个 rank 的分片等长，所以必须把序列**补齐（padding）到 ws 的倍数**。补进来的假 token 不只是浪费通信字节：它们要跟着穿过**每一层的注意力和两次集合通信**，还要占激活内存。

fast-ulysses 把这件事倒过来：调用方直接给出每个 rank 的 `seq_splits`（以及头轴的 `head_splits`），分片可以两两相差一个 token；**偶均分只是 `seq_splits = [s/P] * P` 的特例**，没有第二条代码路径。于是调用方可以直接扔掉 padding。

#### 4.3.2 核心流程

给定全局序列长 \(s\) 与 \(ws\) 个 rank，一种惯用的不均匀切法：

\[ \text{base} = \left\lfloor s / ws \right\rfloor, \quad \text{rem} = s \bmod ws, \quad \text{seq\_splits}[i] = \text{base} + [i < \text{rem}] \]

- 校验：\(\sum_i \text{seq\_splits}[i] = s\)，任意两份至多相差 1。
- BASE 的替代方案：\(s' = \lceil s / ws \rceil \cdot ws\)，补 \(s' - s \le ws - 1\) 个假 token，每 rank 拿 \(s'/ws\)。
- mode 0 的输入按 `seq_splits` 切序列轴、按 `head_splits` 合并头轴；输出形状由两组 splits 共同决定（细节在 u2-l3）。

#### 4.3.3 源码精读

特性原文在 [README.md:L26-L27](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/README.md#L26-L27)：per-rank 的 `seq_splits` / `head_splits` 正是让调用方得以丢弃序列 padding 的机制；偶均分是特例，不是单独的路径。

设计文档一句话给出了架构含义，见 [docs/design.md:L41-L42](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/design.md#L41-L42)：不均匀分片是一般情况，偶均分就是 `seq_splits = [s/P] * P`；只有一条代码路径，所以只需要把一件事做对。

padding 问题的动机与量级在 [docs/benchmark.md:L168-L171](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/benchmark.md#L168-L171)：把序列补到组大小的倍数正是让 BASE 留在平坦路径上的前提，而补的 token 会在每一步、每一层跟着穿过注意力和集合通信；per-rank `seq_splits` 接受相差一个 token 的分片；补齐量至多 \(ws - 1\) 个 token。

实测代价见 [docs/benchmark.md:L188-L191](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/benchmark.md#L188-L191)：本路径去掉 padding 免费（每个形状 1.00–1.03×），而 BASE 要付 5–11%——因为分片一旦不等长它就回不去平坦的 `all_to_all_single`，得换成带 split sizes 的调用加逐 peer 的 reshape 和 `cat`。API 表中两个参数的位置见 [README.md:L91](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/README.md#L91)。

#### 4.3.4 代码实践

**实践目标**：为 docs/benchmark.md 的三个真实形状算出不均匀 splits 与 BASE 所需的 padding，发现它们共同的模式。

**操作步骤**（示例代码，任意 Python 3 即可，无需 torch/GPU）：

```python
# 示例代码：不均匀分片与 padding 的纸面推演
def splits(total, ws):                     # 惯用切法：前 rem 个 rank 各多 1
    base, rem = divmod(total, ws)
    return [base + 1 if i < rem else base for i in range(ws)]

SHAPES = [("wan-720p", 75827, 40), ("wan-480p", 32987, 40), ("h3-t2va-5s", 38051, 56)]
for name, s, heads in SHAPES:
    ws = 8
    sp = splits(s, ws)
    pad = (-s) % ws                        # BASE 需要补齐的 token 数
    print(f"{name}: s={s}  seq_splits={sp}")
    print(f"  校验 sum={sum(sp)}=={s}  最大差={max(sp)-min(sp)}"
          f"  head_splits={splits(heads, ws)}  BASE 需 pad={pad} 个 token")
```

**需要观察的现象**：三个形状的 `seq_splits` 都是「3 个 rank 多 1、5 个 rank 少 1」；`pad` 全部等于 5；`head_splits` 全部均分。

**预期结果**：输出的 `sum` 与 `s` 相等、最大差为 1；pad=5 ≤ \(ws-1=7\)。三个形状 pad 相同并非巧合——它们的图像 token 数（75600/32760/37824）都能被 8 整除，而共同的 227 token 文本尾巴满足 \(227 \bmod 8 = 3\)，于是 \(\text{pad} = 8 - 3 = 5\)。这正是 [docs/benchmark.md:L32](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/benchmark.md#L32) 「s 含一条 227 token 的文本尾巴、因此不被组大小整除」的出处。

#### 4.3.5 小练习与答案

1. **问：ws=4、s=75827 时 `seq_splits` 是什么？BASE 要补多少 token？**
   答：\(75827 = 4 \times 18906 + 3\)，所以 `seq_splits = [18907, 18907, 18907, 18906]`；BASE 补 \(4 - 3 = 1\) 个 token。
2. **问：为什么去掉 padding 对 BASE 反而要付 5–11%，而本路径几乎免费（1.00–1.03×）？**
   答：BASE 一旦分片不等长，就不能再用平坦 `all_to_all_single`，得换成带 split sizes 的调用、逐 peer reshape 再 `cat`，多出一轮重排；本路径的寻址本来就是按 per-rank splits 算的拷贝步长，不均分与均分走同一条代码路径。依据 [docs/benchmark.md:L188-L191](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/benchmark.md#L188-L191)、[docs/design.md:L41-L42](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/design.md#L41-L42)。
3. **问：不均匀分片除了省通信字节，还省什么？**
   答：省掉假 token 穿过每层注意力与两次集合通信的计算与激活内存（[docs/benchmark.md:L330-L331](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/benchmark.md#L330-L331) 明确说这些是微基准看不见的端到端收益）。

### 4.4 性能对比表

#### 4.4.1 概念说明

性能数字只有配合方法学读才有意义。本项目的基准文档有几个值得学习的纪律：**独占执行**（被共享的 GPU 上测不出数字）、**同一二进制**（一个 wheel 装到所有机器，排除重编译差异）、**先过正确性再测性能**（测试不过的构建出的数字「只是长得像数据」）、**每型号两台节点**（v0.1 曾因第二台节点差 5× 而撤回过一行已发布的数字）。另有一个必须先读的脚注：文档所有数字是 0.2.0 版本的，0.2.1 新增的 staging 池与 `lend=True` **尚无数字**。

主表的四个关键读法：

- `vs base`：整次调用墙钟之比（1.60–2.17×）。
- `relayout%`：BASE 花在两个 permute 上的时间占比（46.9–60.0%），即本路径免费拿走的部分。
- `raw/CE`：本路径传输对裸 `all_to_all_single`（传输下限）之比（1.16–1.36×，即本路径传输反而更快）。
- `of the fabric`：传输达到平坦 peer 拷贝带宽上限的百分比（90–99%）。

#### 4.4.2 核心流程

一行数字的诞生过程（[docs/benchmark.md:L40-L44](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/benchmark.md#L40-L44)）：

```text
tools/exclusive.sh <GPUs> -- torchrun --nproc_per_node=8 benchmark/bench_a2a.py --mode stages
   │ exclusive.sh 拒绝在 GPU 被占用时启动，全程采样并打印 EXCLUSIVE/CONTENDED
   ▼
每个形状：8 次预热 + 25 次迭代取中位数（bf16, mode=0）
   ▼
CUDA 事件在 BASE 与 OURS 各阶段之间打点 → 汇成 stages 表
```

判读顺序建议：先看方法节确认可比性 → 主表看 `vs base` → 用 `relayout%` 解释差距来源 → 用 `of the fabric` 判断还有没有优化空间（90–99% 意味着「没什么可调度的了」）。

#### 4.4.3 源码精读

README 首页性能表与摘要见 [README.md:L29-L43](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/README.md#L29-L43)：8 卡、每行独占整机、同一 wheel；H200 1.82×、B200 2.17×、B300 2.16×、H100 1.66×；47–60% 的 BASE 时间是此处免费的重排；`out=` 再拿 1.14–1.28×。取样纪律（每型号两台节点，H100 除外）在 [README.md:L45-L46](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/README.md#L45-L46)。

版本脚注：[docs/benchmark.md:L7-L11](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/benchmark.md#L7-L11) —— 全部数字是 0.2.0 的、仍描述 0.2.1 未触碰的传输路径；0.2.1 新增的 staging 池与 `lend=True` 经过正确性测试但没有数字。

方法学与形状定义：[docs/benchmark.md:L13-L17](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/benchmark.md#L13-L17)（exclusive.sh、单一 wheel、先过测试、每型号两台节点）与 [docs/benchmark.md:L30-L38](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/benchmark.md#L30-L38)（bf16、mode=0、8 次预热后 25 次取中位数；三个真实形状 qkv 打包、末维 \(3 \times \text{head\_dim}\)、s 含 227 token 文本尾）。

完整 stages 表与其摘要：[docs/benchmark.md:L70-L87](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/benchmark.md#L70-L87)，结论一句：三代 NVLink 上形态一致——对 `torch.distributed` 路径 1.60–2.17×，其中 46.9–60.0% 是此处免费的重排，传输单独比裸 `all_to_all_single` 还快 1.16–1.36×。

带宽上限对照：[docs/benchmark.md:L95-L109](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/benchmark.md#L95-L109) 用「经 torch 对称内存的平坦 64 MiB peer 拷贝」独立建立天花板，再算传输占其百分比——8 条流并发时每流带宽不降，说明 fabric 不是瓶颈，90–99% 意味着没有剩余可调度空间。

适用边界（小消息）：[docs/benchmark.md:L216-L262](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/benchmark.md#L216-L262) —— 屏障开销与负载无关，其占比说明多大消息该换工具：每 rank 0.5 MB 时握手占 33–54%，291 MB 时 5.1–8.4%；本算子为每 rank 数百 MB 的长序列视频 DiT 场景而生。

为什么拒绝 PCIe：[docs/benchmark.md:L264-L298](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/benchmark.md#L264-L298) —— 两台 RTX PRO 6000 节点的整机结果相差 5×（OURS 22.85 对 122.25 ms），而平坦 peer 拷贝带宽两台几乎相同（54.6 对 55.9 GB/s）；差别在本库依赖的跨步拷贝模式在 PCIe 上（且随交换机拓扑）严重退化，20% 与 4% 的上限利用率对比 NVLink 的 90–99%。这就是 README Limits 里构造函数直接拒绝非 NVLink 组的实测依据。

限制清单原文见 [README.md:L102-L121](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/README.md#L102-L121)：NVLink 单机 ws∈[1,8]（含奇数大小）；dtype 白名单 `float16/bfloat16/float32/float8_e4m3fn/float8_e5m2/int8/uint8` 且 \(d \times \text{elem\_size}\) 需 16 字节对齐（bf16 即 `d % 8`、float8 即 `d % 16`）；不可被 `torch.compile` trace；forward AD 可用、reverse functorch 与 vmap 显式拒绝；async 与 `out=` 不可微；CUDA graph 仅覆盖已预热形状上的 sync 调用。环境要求 PyTorch 2.10+、CUDA 12.8+ 或 13（[README.md:L125-L126](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/README.md#L125-L126)）。

#### 4.4.4 代码实践

**实践目标**：亲手复算表里三个数字，证明你能从原始列推导出派生列——这是「读懂数据」而不是「相信数据」的检验。

**操作步骤**（纸面计算器或 Python 均可）：

```python
# 示例代码：复算 docs/benchmark.md 的派生列
# 原始数据取自 stages 表（docs/benchmark.md:72）与 fabric 表（docs/benchmark.md:102）
rows = {  # GPU: (perm_in, a2a, perm_out, BASE, transfer, OURS, single_flow_GBps, perflow8_GBps)
    "H200": (0.358, 0.826, 0.385, 1.569, 0.680, 0.860, 378.7, 376.0),
    "B200": (0.347, 0.478, 0.369, 1.194, 0.410, 0.550, 691.7, 689.4),
}
MB_crossed = 291 * 7 / 8          # 实际跨链路的字节：本 rank 数据的 7/8 要发给 peer
for gpu, (p_in, a2a, p_out, base, transfer, ours, sf, pf8) in rows.items():
    print(gpu)
    print(f"  vs base 复算 {base/ours:.2f}x")
    print(f"  relayout% 复算 {(p_in + p_out)/base*100:.1f}%")
    gbps = (MB_crossed / 1e3) / (transfer / 1e3)      # MB / ms -> GB/s
    print(f"  传输带宽复算 {gbps:.0f} GB/s, 占单流上限 {gbps/sf*100:.0f}%, 占 8 流每流 {gbps/pf8*100:.0f}%")
```

**需要观察的现象**：复算值与表格打印值一一吻合。

**预期结果**：H200 行 `1.82× / 47.3% / 约 374–375 GB/s`，B200 行 `2.17× / 60.0% / 621 GB/s`，占上限约 99% 与 90%——与 [docs/benchmark.md:L72](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/benchmark.md#L72)、[docs/benchmark.md:L102-L103](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/benchmark.md#L102-L103) 一致（H200 的带宽复算因 MB 取整与表内 375 GB/s 可差约 1 GB/s）。注意一个细节：README 表的 H100「of the fabric」写 94%、benchmark.md 写 93%，因为分母分别取了「8 流并发时每流 377.6」与「单流 381.4」两个不同的平坦拷贝数——两份文档各自成立。

#### 4.4.5 小练习与答案

1. **问：`transfer` 已经只有 0.680 ms（H200），为什么还说「没什么可优化的了」？**
   答：因为它已达平坦 peer 拷贝同一机器所能达到带宽的约 99%（[docs/benchmark.md:L95-L109](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/benchmark.md#L95-L109)），且 8 流并发不互相拖慢——fabric 不是受争用的资源，上限就是天花板，没有剩余调度空间。
2. **问：复算 B300 wan-720p 行的 `relayout%`（perm_in 0.344、perm_out 0.366、BASE 1.184）。**
   答：\((0.344 + 0.366) / 1.184 = 59.97\% \approx 59.9\%\)，与表格一致（[docs/benchmark.md:L78](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/benchmark.md#L78)）。
3. **问：每 rank 只有 0.5 MB 的短序列场景该用这个算子吗？**
   答：不合适。屏障开销与负载无关，0.5 MB 时握手占调用的 33–54%（[docs/benchmark.md:L253-L262](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/benchmark.md#L253-L262)）；该算子为每 rank 数百 MB 的长序列视频 DiT 设计。

## 5. 综合实践

把本讲四个模块串起来的任务：**给你的机器写一份「重排税」小报告**。

1. **实践目标**：量化 BASE 路径在「你的硬件」上付出的重排税，并与项目在 H200/B200 上的 `relayout%` 对话；同时判断你的场景是否落在本算子的适用区间。
2. **操作步骤**：
   1. 运行 4.2.4 的脚本，记录 \(t_{\text{view}}\)、\(t_{\text{cont}}\) 与张量字节数（125.8 MB）。
   2. 推算：BASE 每次调用要付两次 contiguous（`perm_in + perm_out`），以你的 \(t_{\text{cont}}\) 估算 \(2 t_{\text{cont}}\)；如果你有 8 卡 NVLink 机器可真跑 BASE（`torchrun --nproc_per_node=8` 下实现 4.2.3 引用的 `baseline_padded`），否则用文档值。
   3. 用 4.3.4 的脚本算出你的序列长度（例如沿用 wan-720p 的 75827）在目标 ws 下的 `seq_splits` 与 BASE 需要的 padding。
   4. 用 4.1 的方法查你的 SM 数与 `nvidia-smi topo -m` 拓扑，对照 [README.md:L104-L106](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/README.md#L104-L106) 判断是否在支持范围（NVLink 单机、ws 1–8）。
   5. 把以上写成不超过 10 行的结论文本。
3. **需要观察的现象**：`t_view / t_cont` 的比值；你的带宽（字节 ÷ \(t_{\text{cont}}\)）与 H200 的 ~1.23 µs/MB 相比差几倍；拓扑矩阵里有无 `NV` 链路。
4. **预期结果**：报告中应能回答三个问题——(a) 重排税在你的机器上占比多少（量级即可）；(b) 你的序列要不要 padding、要补几个 token；(c) 你的机器能否用这个库（若只有 PCIe 或跨 socket，结论应指向「不能用，构造函数会拒绝」，依据 4.4.3 的 PCIe 一节）。若在共享 GPU 或 CPU 上完成，注明数字不可与文档表直接比较（**待本地验证**：独占 GPU 上的数值）。

## 6. 本讲小结

- fast-ulysses 把 Ulysses 序列并行的 4D all-to-all 做成 `torch.ops.fast_ulysses.all_to_all_4d`：长序列视频 DiT 中这两次集合通信正是关键路径。
- **零 SM 传输**：peer 拷贝是直写 peer 对称内存地址的带步长 `cudaMemcpy2D/3DAsync`，由拷贝引擎执行、不占 SM，可与 GEMM 完全重叠；端到端零 SM 还需用 `out=` 去掉占 SM 的 copy_out。
- **重排即步长**：序列/头排列被编码为拷贝的源/目的步长，BASE 的两个 permute 内核整个消失——这占 BASE 开销的 47–60%。
- **不均匀分片是一般路径**：per-rank `seq_splits`/`head_splits` 让调用方直接丢掉序列 padding（BASE 为此多付 5–11%，本路径 1.00–1.03×）。
- **性能表要按方法学读**：独占执行、单一 wheel、每型号两节点；1.60–2.17× vs BASE，传输达 fabric 上限的 90–99%；小消息下屏障占比高，本算子面向每 rank 数百 MB 的场景；PCIe 上跨步拷贝退化且随拓扑剧变，故构造函数直接拒绝非 NVLink 组。
- 适用边界同样明确：NVLink 单机、ws∈[1,8]、七个 dtype 且 \(d \times \text{elem\_size}\) 需 16 字节对齐、不可被 `torch.compile` trace、`out=`/async 不可微。

## 7. 下一步学习建议

- 下一讲 **u1-l2「Ulysses 序列并行原理」**：把本讲一笔带过的 mode 0 / mode 1 形状变换 \((b, s_{\text{local}}, n_{\text{global}}, d) \leftrightarrow (b, s_{\text{global}}, n_{\text{local}}, d)\) 讲透，并动手用 `split/cat` 实现等价变换。
- 想先把环境搭起来、亲手跑一次 `group.all_to_all_4d`，可先跳到 **u1-l3「安装、构建与第一次运行」**，对照 [README.md:L48-L74](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/README.md#L48-L74) 的快速开始。
- 延伸阅读（本讲只节选了两段）：[docs/design.md](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/design.md) 的其余部分论及未文档化的硬件假设与被否决的替代方案，第 9 单元会回头精读；[benchmark/bench_a2a.py](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/benchmark/bench_a2a.py) 文件头的七种模式说明是理解性能文档的钥匙。
