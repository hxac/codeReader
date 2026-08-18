# u2-l6 decode 的 split-KV MLA 注意力与双微批结构

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 `flash_fwd_splitkv_mla_kernel` 与 `flash_fwd_splitkv_mla_combine_kernel` 这对「两段式」注意力内核各自的职责，并能用 split-KV 的 online softmax 合并公式解释为什么需要第二段。
2. 用时间戳数据复原解码阶段的「槽」（slot）结构：约 724µs 一个周期，每槽恰好 1 次注意力 + 2 次 `dispatch_ll` + 2 次 `combine_ll`，从而验证 README 所说的「解码同样用两个微批重叠计算与 all-to-all 通信」。
3. 统计出注意力家族占全部 GPU 内核时长的比例（本讲实测约 34.9%），并理解「解码步的GPU时间是注意力和通信主导的」。
4. 列举解码侧其余计算内核家族（fp8_gemm、swiglu、rotary、per_token_cast_to_fp8、layer_norm、top2_sum_gate 等）的次数与典型时长，理解「解码 = 大量微小子任务 + 少数大内核」的形态。

## 2. 前置知识

### 2.1 解码步（decode step）与 KV cache

推理分两个阶段（参见 u1-l1）：

- **预填充（prefill）**：一次吃进整条 prompt（本仓库配置为 4K token），算出所有位置的注意力，并把每层的 K/V 写入 **KV cache**。
- **解码（decode）**：此后每一步只为「最新生成的 1 个 token」做前向，输出下一个 token。注意力变成「1 个 query 对着最长 4K 的 Key/Value 做attenstion」——query 极短、KV 极长。

这带来两个解码特有的性能特征：

- 单个 query 无法填满 GPU 的并行度，注意力是**访存受限**（memory-bound）的——瓶颈在把几百 MB 的 KV cache 从显存搬进 SM；
- 每步都要走完整个模型的所有层，任何一层里的 all-to-all 通信延迟都会直接拖慢步长，所以通信必须被「藏」进计算里。

### 2.2 MLA 复习

DeepSeek 的 MLA（Multi-head Latent Attention）把每层的 K/V 压缩成低秩潜向量：KV cache 里每个 token 只存 512 维压缩向量 + 64 维 RoPE 部分。本讲会看到一个漂亮的数字印证：注意力内核模板参数里的 `576`，恰好等于 \(512 + 64\)（推断，与 DeepSeek-V3 的 `kv_lora_rank=512`、`qk_rope_head_dim=64` 吻合）。

### 2.3 split-KV：为什么解码注意力要拆成两段

把一条长 KV 序列切成 \(S\) 段，多个 CUDA block 并行处理各自的段，每段 \(s\) 产出**部分**结果：部分最大值 \(m_s\)、部分指数和 \(l_s\)、未归一化的输出 \(O_s\)。最后需要一个内核把各段合并成全局 softmax：

\[
m = \max_{s} m_s,\qquad
l = \sum_{s} e^{m_s - m}\, l_s,\qquad
O = \frac{\sum_{s} e^{m_s - m}\, O_s}{l}
\]

- 第一段内核（main）：负责每段的 \(q k^\top\)、\(e^{m_s}\) 缩放与累积，访存密集、时长较长；
- 第二段内核（combine）：只做上式的归并，纯计算、极短。

这是 FlashAttention 系列处理长序列解码的标准手法（split-KV / flash-decoding），本讲的重点就是这对内核在真实轨迹里的形态。

### 2.4 双微批与 CUDA Graph（承接 u2-l4 / u2-l5）

- u2-l4 看到 prefill 用两个微批把 DeepEP 通信藏进计算；README 明说解码也这么做（下文精读原文）。
- u2-l5 已确认：decode.json 的整条解码前向由**一次 CUDA Graph 回放**完成——本讲抽检的三个不同家族内核，`args.correlation` 全部等于 7077，一个 `cudaGraphLaunch` 负责了全部 3647 个内核。因此**无法用 correlation 或内核参数区分微批**，只能靠时间结构。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `decode.json` | EP128 解码轨迹，4.66MB。**单行压缩 JSON**，全部内容在第 1 行，所以本讲对它的永久链接都指向 `#L1-L1`；必须用脚本解析，不能直接阅读 |
| `README.md` | 第 24–30 行是 Decoding 小节：EP128、4K prompt、128 requests/GPU、双微批、RDMA 发出后释放 SM |
| `assets/decode.jpg` | README 配套截图，可对照本讲复原的时间线 |

先回顾这份文件的整体画像（u1-l3、u2-l5 已建立，本讲用只读命令重新核实过）：

- `distributedInfo` = `{"backend": "nccl", "rank": 0, "world_size": 128}`，`traceName` = `./decode-debug-rank0.json`，采集机器 1 块 NVIDIA H800（132 SM，参见 [decode.json:1](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/decode.json#L1-L1)）。
- `traceEvents` 共 19417 条（X 6852、f 11582、s 943、M 38、i 2）；其中 `cat="kernel"` 3647 条、`cpu_op` 2674、`cuda_runtime` 452、`ac2g` 12525、`gpu_memcpy` 66。
- CPU 侧注解只有 3 条 `nccl:all_reduce`——**没有** train.json 里那种 `ProfilerStep#1`/`1F1B` 注解，所以解码只能「从 GPU 内核反推结构」，这正是本讲的方法论。

## 4. 核心概念与源码讲解

### 4.1 flash_fwd_splitkv_mla 两段式内核

#### 4.1.1 概念说明

decode.json 的 GPU 时间线上，最长的周期性内核就是这对注意力内核：

- `flash::flash_fwd_splitkv_mla_kernel`：split-KV 主内核，逐段做 MLA 注意力；
- `flash::flash_fwd_splitkv_mla_combine_kernel`：归并内核，把各段部分结果合成最终输出。

它们解决的就是 2.3 节的问题：128 个请求 × 每请求 1 个新 token 的 query 太少，而每个请求背着最长 4K token 的 KV cache；不切分的话 GPU 大部分 SM 只能围观少数几个 block 慢慢读显存。

#### 4.1.2 核心流程

一次注意力调用的执行过程：

```text
main 内核（grid [4,1,33]，132 个 block，每 block 256 线程，占满 132 个 SM）
  ├─ 每个block认领一段 KV（分块读取 KV cache）
  ├─ 计算部分 m_s / l_s / O_s（online softmax 累积）
  └─ 写回部分结果
        ↓ 间隔约 1µs（流水线无缝衔接）
combine 内核（grid [16384,1,1]，128 线程/block）
  ├─ 按 (请求, 注意力头) 归并各段：套用 2.3 节合并公式
  └─ 写最终注意力输出
```

两段的负载天差地别：main 要搬完整 KV，**访存受限**；combine 只对已缩小的部分结果做归并，**又小又快**。

#### 4.1.3 源码精读

**内核签名与一次真实调用。** 在 [decode.json:1](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/decode.json#L1-L1) 中，两个内核各出现 120 次，完整签名如下（为排版截短了模板参数）：

```text
void flash::flash_fwd_splitkv_mla_kernel<Flash_fwd_kernel_traits_mla<
    576, 64, 64, 8, cutlass::bfloat16_t, 512, true, 0, ...>, true, ...>(Flash_fwd_mla_params)
void flash::flash_fwd_splitkv_mla_combine_kernel<cutlass::bfloat16_t, float, long, 512, 64>(
    Flash_fwd_mla_params)
```

main 内核 traits 里的 `576` 与 \(512+64\) 吻合，正是 MLA 的「压缩 KV 维 + RoPE 维」打包（推断，待确认）。一次真实事件的 `args` 字段（ts=1742522672424808）：

```json
{"stream": 7, "correlation": 7077, "registers per thread": 248,
 "shared memory": 230400, "blocks per SM": 1, "warps per SM": 8,
 "grid": [4, 1, 33], "block": [256, 1, 1]}
```

三个值得停一停的数字：

- `grid = 4×33 = 132` 个 block，而 H800 恰好有 132 个 SM，`blocks per SM = 1`——这个内核被刻意铺成「一 SM 一块」（推断为设计意图），且 230400 字节共享内存几乎用满 Hopper 每.SM 上限，所以也塞不下第二块；
- combine 内核的 `grid = [16384,1,1]`，而 \(16384 = 128 \times 128\)——README 说每 GPU 128 个请求、V3 每层 128 个注意力头，**每个 (请求, 头) 一个 block** 做归并（推断，与配置数字严丝合缝）；
- `correlation: 7077`——本讲抽检 main、combine、dispatch_ll 三个家族得到同一个值，印证 u2-l5 的结论：整条前向是 CUDA Graph 一次回放。

**时长分布（实测直方图）。** 对 [decode.json:1](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/decode.json#L1-L1) 全量统计：

| 内核 | 次数 | 时长分布 | 总时长 | 占全部内核时长 |
| --- | --- | --- | --- | --- |
| splitkv_mla_kernel（main） | 120 | 116 次 240–245µs + 4 次 465–467µs | 29,942µs | 33.3% |
| splitkv_mla_combine_kernel | 120 | 116 次 10–11µs + 4 次 24–25µs | 1,366µs | 1.5% |
| **注意力家族合计** | **240** | — | **31,308µs** | **34.9%** |

（分母 89,786µs 是全部 3647 个 kernel 事件 dur 之和，统计方法见 4.1.4。）

两个结构信号：

1. **两段严丝合缝**：逐对核验，main 结束后约 1µs combine 立即启动，例如 424092+245=424337，combine 的 ts=424338。split-KV 的「部分结果→归并」在时间线上就是紧贴的双脉冲。
2. **4 个约两倍时长的边界事件**：窗口开头的 3 个 main（466/466/465µs）与结尾的 1 个（467µs），combine 同样有 4 个 24–25µs 的对应事件。它们处在稳态流水线建立之前/结束之后，具体成因标注**待确认**（见 4.1.5 练习 3）。

**与 prefill 的对比**：u2-l4 里 prefill 的 MLA 注意力是 `flash::compute_attn_ws`（整条 4K prompt 一起算，计算受限）；decode 换成了 `splitkv_mla` 两段式（query 少、KV 长，访存受限）。**同一套 MLA，两种内核形态**——这是区分两份轨迹最快的指纹之一。

#### 4.1.4 代码实践

**实践 1：统计两段式注意力内核并观察它们的贴身关系**（以下为示例代码，仓库本身不含代码文件）。

1. 实践目标：统计两个注意力内核的次数、总时长、占全部 kernel 时长的比例；按 ts 排序输出前 20 个事件，观察「两段紧贴 + 均匀心跳」。
2. 操作步骤：

```python
import json
from collections import defaultdict

events = json.load(open("decode.json"))["traceEvents"]
kern = [e for e in events if e.get("ph") == "X" and e.get("cat") == "kernel"]
total = sum(e["dur"] for e in kern)

att = [e for e in kern if "flash_fwd_splitkv_mla" in e["name"]]
for key in ("splitkv_mla_kernel<", "splitkv_mla_combine_kernel<"):
    grp = [e for e in att if key in e["name"]]
    s = sum(e["dur"] for e in grp)
    print(f"{key:35s} n={len(grp):4d}  sum={s:7d}us  share={s/total:.1%}")

att.sort(key=lambda e: e["ts"])
t0 = att[0]["ts"]
for e in att[:20]:
    tag = "main" if "combine" not in e["name"] else "comb"
    print(f"{tag}  +{(e['ts']-t0)/1e0:8.0f}us  dur={e['dur']:4d}us")
```

3. 需要观察的现象：`main` 与 `comb` 是否成对出现且时间差 ≈ 前一个的 `dur`；相邻 main 的间隔是否稳定。
4. 预期结果（本讲作者在仓库 decode.json 上实测）：

```text
splitkv_mla_kernel<                n= 120  sum=  29942us  share=33.3%
splitkv_mla_combine_kernel<        n= 120  sum=   1366us  share=1.5%
main  +       0us  dur= 466us     # ↓ 窗口开头的 3 个“慢”事件
comb  +     467us  dur=  24us
main  +     923us  dur= 466us
comb  +    1390us  dur=  24us
main  +    2997us  dur= 465us
comb  +    3463us  dur=  25us
main  +    3937us  dur= 245us     # ↓ 从此进入稳态心跳
comb  +    4183us  dur=  11us
main  +    6175us  dur= 245us
comb  +    6421us  dur=  11us
main  +    6888us  dur= 245us
comb  +    7134us  dur=  11us
main  +    7604us  dur= 245us
comb  +    7850us  dur=  11us
main  +    8313us  dur= 242us
comb  +    8557us  dur=  11us
main  +    9028us  dur= 244us
comb  +    9273us  dur=  11us
main  +    9734us  dur= 243us
comb  +    9980us  dur=  11us
```

   稳态区间相邻 main 的平均间隔 \((504454-421141)/115 \approx 724\)µs——这就是 4.2 节要解剖的「槽周期」。

> 方法提示（作者实测时踩过的坑）：若用 shell 正则统计内核，注意很多 PyTorch 内核名里含 `{lambda...#1}` 花括号（本文件里有 213 个），用 `[^}]*` 匹配事件体会漏掉它们，应改用 `"name": "[^"]*"` 锚定。Python 的 `json.load` 不受影响。

#### 4.1.5 小练习与答案

1. **练习**：为什么 combine 内核的 grid 是 16384 而主内核只有 132 个 block？从「每单位工作量」角度解释。
   **答案**：main 是访存受限——总共只有一整份 KV cache 要读，切成 ~132 段后每 SM 一块已把带宽用满，更多 block 也无活可分；combine 是纯计算——归并量与 (请求×头) 数成正比，\(128 \times 128 = 16384\) 个小任务天然大规模并行。
2. **练习**：如果请求的 KV 长度翻倍（prompt 从 4K 到 8K），两个内核的时长分别会怎么变？
   **答案**：main 大约线性增长（要读的 KV cache 翻倍，访存受限下时长≈搬运量/带宽）；combine 几乎不变（段数与部分结果规模由 split 数决定，与序列总长基本无关）。这正是 240µs vs 10µs 比例背后的物理。
3. **练习**：找出 4 个慢 main 事件（465–467µs）的 ts，检查它们前后各 2ms 内有哪些内核，提出一个成因假设。
   **答案**：3 个位于窗口最前端（ts=...417204/418127/420201）、1 个位于最尾（...507985），前后是 NCCL all-reduce、随机数、采样类等非稳态内核。合理假设：它们是采集窗口边界上尚未进入（或刚离开）双微批稳态流水的注意力调用，无法与另一微批的计算错峰，时长约为稳态的两倍；确切成因待本地用完整上下文验证。

### 4.2 解码阶段的双微批交替

#### 4.2.1 概念说明

README 对解码的描述（[README.md:24-30](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/README.md#L24-L30)）：

> For decoding, the profile employs EP128, TP1, and a prompt length of 4K ..., with a batch size of 128 requests per GPU. **Similar to prefilling, decoding also leverages two micro-batches for overlapping computation and all-to-all communication.** However, unlike in prefilling, the all-to-all communication during decoding does not occupy GPU SMs: after RDMA messages are issued, all GPU SMs are freed, and the system waits for the all-to-all communication to complete after the computation has finished.

关键句在 [README.md:30](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/README.md#L30-L30)。本节的任务：**只用时间戳，把「两个微批」从轨迹里挖出来**。难点在 2.4 节说过——CUDA Graph 回放让所有内核共享 correlation、参数也相同，内核层面「匿名」，只能靠节奏辨认。

#### 4.2.2 核心流程

把 4.1 节的注意力心跳（周期 ≈724µs）当作格子，把 u2-l5 的 DeepEP 低延迟内核放进格子里，得到稳定的「槽」结构。以 ts=1742522672424808 附近两个槽为例（相对槽起点取整）：

```text
槽（≈716µs，全部在 stream 7 上串行执行）
├─ +0    splitkv_mla_kernel          245µs   ← 微批 X 的注意力（读 KV，占满 SM）
├─ +246  splitkv_mla_combine_kernel   10µs   ← 归并
├─ +354  combine_ll                   ~19µs  ← 微批 Y 的 MoE 结果回收（发送段）
├─ +376  dispatch_ll（发送段）         17µs  ← 微批 Y 的 token 发往专家：RDMA 消息发出后即退出
├─        ……约 106µs 计算内核填充……            ← SM 被释放，先干别的活
├─ +499  dispatch_ll（等待段）       60–80µs  ← 等待/收取对端数据
├─        ……约 132µs 计算内核填充……
├─ +696  combine_ll                   ~17µs
└─ +716  下一槽 splitkv_mla_kernel   245µs   ← 微批 Y 的注意力（角色互换）
```

自洽性检查（全部为实测计数）：稳态区间共 116 个槽，每槽恰好 1 个 main + 1 个 combine + 2 个 `dispatch_ll` + 2 个 `combine_ll`：

- 注意力 main 120 = 稳态 116 + 边界 4；
- `dispatch_ll` 235 = 稳态 232 + 尾部 3；`combine_ll` 同样 235 = 232 + 3；
- 稳态槽 116 = 58 个 MoE 层 × 2 微批——每个槽完成**一个微批的一层**：本槽的注意力属于微批 X 时，槽内的 dispatch/combine 属于微批 Y，下一槽互换。相邻注意力间隔 ≈724µs = 「半层」时间，61 层 × 2 微批 × 724µs ≈ 88ms，与窗口总跨度 95.9ms（含首尾非稳态区）吻合：**窗口约覆盖一个解码步**。

#### 4.2.3 源码精读

**证据一：dispatch_ll 的双峰 = 「发送→释放 SM→先算→再等」的行为指纹。** [decode.json:1](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/decode.json#L1-L1) 中 235 个 dispatch_ll 的时长直方图严格分成两簇：

| 簇 | 次数 | 时长 | 含义 |
| --- | --- | --- | --- |
| 发送段 | 117 | 17–19µs | 把 RDMA 消息写出去，做完就退出，SM 释放 |
| 等待段 | 118 | 37–86µs | 旋转等待对端数据到位 |
| 尾巴 | 3 | 134 / 202 / 1603µs | 窗口尾部的非稳态事件 |

一次逻辑 dispatch = 「发送段 + 等待段」两次同名内核启动，中间隔着 ~106µs 的计算内核——这正是 README 那句 "after RDMA messages are issued, all GPU SMs are freed, and the system waits ... after the computation has finished" 的直接数据体现。combine_ll 同理：119 次 16–18µs（发送段）+ 113 次 19–23µs（回收段）+ 3 个尾巴。

**证据二：combine_ll 间隔的长短交替。** 相邻 combine_ll 的 ts 差在 ~375µs 与 ~340µs 之间严格交替（383, 339, 375, 342, 375, 334, 373, 339, …）。周期为 2 的交替 = 两个错相半周期的序列交织在一起——「两个微批」在时间轴上的脚印。

**证据三：计数整除性。** 注意力 120 ≈ 61 层 × 2 微批（窗口裁掉 ~2 次）；dispatch_ll 逻辑次数 116 ≈ 58 个 MoE 层 × 2 微批。若只有一个微批，这些数字都无法对上。

**为什么解码不像 prefill 那样分通信流？** u2-l4 里 prefill 的 DeepEP 内核在独立 stream 16 上与计算流真并行；u2-l5 已实证 decode 的 ll 内核与计算同在 stream 7 串行排队——重叠不靠多流，靠「RDMA 飞行时间不占 SM」。本讲的槽结构进一步显示：**被藏进等待段里的，正是另一个微批的注意力**（245µs 的 main 恰好横跨多个发送/等待段之间的空隙）。

#### 4.2.4 代码实践

**实践 2：测量槽周期与 combine_ll 的交替相位**（示例代码）。

1. 实践目标：用脚本量化「槽周期 ≈724µs」与「combine_ll 间隔长短交替」，验证双微批结构。
2. 操作步骤：

```python
import json, statistics

events = json.load(open("decode.json"))["traceEvents"]
kern = sorted((e for e in events
               if e.get("ph") == "X" and e.get("cat") == "kernel"),
              key=lambda e: e["ts"])

mains = [e for e in kern if "splitkv_mla_kernel<" in e["name"]]
cll   = [e for e in kern if "internode::combine_ll" in e["name"]]

# 1) 槽周期：稳态区间相邻 main 的间隔
gaps = [b["ts"] - a["ts"] for a, b in zip(mains, mains[1:])]
steady = [g for g in gaps if 500 < g < 1200]          # 剔除窗口边界
print("slots:", len(steady) + 1, "mean gap:", statistics.mean(steady))

# 2) combine_ll 间隔的奇偶交替
cg = [b["ts"] - a["ts"] for a, b in zip(cll, cll[1:])]
odd  = statistics.mean([g for i, g in enumerate(cg) if i % 2 == 0 and g < 500])
even = statistics.mean([g for i, g in enumerate(cg) if i % 2 == 1 and g < 500])
print(f"combine_ll gaps: half-cycle A ~{odd:.0f}us, half-cycle B ~{even:.0f}us")
```

3. 需要观察的现象：稳态间隔是否聚成一个很窄的簇；奇偶两组均值是否明显不同但之和 ≈ 槽周期。
4. 预期结果（实测）：稳态 main 间隔 116 个、均值 ≈724µs（个体在 705–730µs 之间）；combine_ll 半周期 A ≈375µs、B ≈340µs，A+B ≈715µs ≈ 槽周期。两组半周期之和就是一整个槽——交替的两相拼出稳态心跳。
5. 若本地无法运行：以上数字来自作者在仓库 decode.json 上的实测（提取 ts 后排序求差），**待本地验证**的只有你自己复现时的浮点尾数。

#### 4.2.5 小练习与答案

1. **练习**：每槽有 2 个 dispatch_ll（发送段+等待段），为什么说逻辑上只有「一次 dispatch」？
   **答案**：117 个发送段与 118 个等待段几乎一一配对（总数 235 ≈ 2×116+3），且时间上总是「短(17µs)→隔~106µs→长(60–80µs)」成对出现在同一槽内。这是同一逻辑操作的两个阶段：DeepEP 的低延迟实现先让内核把 RDMA 消息发出去并退出（释放 SM），再由后续内核完成等待/收取——「一次 dispatch、两次启动」。
2. **练习**：如果解码改用单微批（128 个请求一起走），槽结构会怎么变？哪个数字会暴露问题？
   **答案**：没有另一半微批的计算可填，dispatch 发出后 SM 只能空转，等待段的 ~106µs 填充会变成空闲气泡，步长近似变成「注意力 + 暴露的通信时间」。暴露的观测信号是：main 间隔不再 ≈724µs 而明显拉长，且 dispatch_ll 等待段与注意力不再交错。
3. **练习**：为什么本讲说「无法从内核名或 args 区分两个微批」，而 u2-l2 讲 train 时却能分辨 `attn(F)`/`attn(B)`？
   **答案**：train 的 CPU 侧有框架主动打的 `user_annotation`（1F1B 调度注解）；decode 的整条前向被录进 CUDA Graph，回放时 CPU 只有一次 `cudaGraphLaunch`（内核共享 correlation=7077），没有任何逐层注解，双微批只能靠计数整除性与时间相位推断。

### 4.3 解码计算内核清单

#### 4.3.1 概念说明

把注意力（34.9%）和 DeepEP 通信（19.0%，u2-l5 实测）拿掉，还有约 46% 的 GPU 时间由 ~3300 个**小内核**分食——它们的单体时长多在 1–55µs 之间。解码的形态是「少数大内核打拍子，大量小内核填缝隙」。认识这些家族，你才能读懂 decode 时间线上密密麻麻的细条。

#### 4.3.2 核心流程

一个微批的一层在槽内的计算链条（按家族归纳，顺序为示意）：

```text
_layer_norm_kernel（输入归一化，1–6µs）
  → per_token_cast_to_fp8（激活转 FP8，2–9µs）
  → dpsk::gemm::fp8_gemm_kernel（DeepGEMM 矩阵乘，12–55µs）
  → vllm::rotary_embedding_with_kv_cache（RoPE，7–9µs）
  → flash_fwd_splitkv_mla 两段式（本章 4.1）
  → swiglu_forward_with_weight...（激活+量化的融合 SwiGLU，4–33µs）
  → top2_sum_gate（MoE 路由门控，6–7µs）
  → dispatch_ll / combine_ll（all-to-all，4.2 节）
  → splitKreduce / cutlass GEMM（分组专家计算，15–26µs）
```

#### 4.3.3 源码精读

下表为 [decode.json:1](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/decode.json#L1-L1) 的全量家族统计（3647 个内核；「典型时长」为实测直方图的主峰区间）：

| 家族（名称摘要） | 次数 | 典型时长 | 角色 |
| --- | --- | --- | --- |
| `_layer_norm_kernel` | 364 | 2–4µs | RMSNorm，≈61 层×2 微批×3 处 |
| `cuda::per_token_cast_to_fp8_with_channels` | 360 | 2–5µs | 激活逐 token 转 FP8 |
| `dpsk::ep::internode::dispatch_ll` | 235 | 17–19 / 37–86µs | MoE 发送（两段式，见 4.2） |
| `dpsk::ep::internode::combine_ll` | 235 | 16–23µs | MoE 回收（两段式） |
| `vllm::rotary_embedding_with_kv_cache_kernel` | 120 | 7–9µs | RoPE 位置编码（与注意力 1:1） |
| `flash_fwd_splitkv_mla_kernel` / `..._combine` | 120 / 120 | 240–245 / 10–11µs | split-KV MLA 注意力（4.1） |
| `get_mla_metadata_kernel` | 4 | — | 注意力 tile 布局元信息（推断，待确认） |
| `dpsk::gemm::fp8_gemm_kernel`（7 个主形状） | 合计 840 | 12–55µs | DeepGEMM FP8 矩阵乘；形状如 `<2112,7168>`（12–14µs，注意力的 q/kv 升维，\(2112=1536+576\)，推断）、`<7168,16384>`（48–55µs）、`<24576,1536>` 等 |
| `cutlass::Kernel<cutlass_80_tensorop_bf16_s16816gemm...>` | 120/120/117 | 15–26µs | bf16 GEMM（三种排布） |
| `cuda::swiglu_forward_with_weight_and_per_token_cast...` | 120 | 4–33µs | 稠密路径 SwiGLU+量化融合 |
| `cuda::batched_swiglu_forward_and_per_token_cast...` | 118 | — | MoE 路径批量 SwiGLU |
| `splitKreduce_kernel` | 117 | — | cuBLAS split-K 归约 |
| `cuda::top2_sum_gate` | 117 | 6–7µs | Top-2 路由门控求和 |
| `vllm::topk_kernel` / `mask_top_p` / `apply_penalty` 等 | 各 3 | — | 采样（推测位于窗口边界区域，待验证） |
| `ncclDevKernel_AllReduce_*`（stream 13） | 3 | 106–245µs | 唯一的 NCCL 内核；另有 2 个内核在 stream 16（身份待确认） |

几个值得玩味的计数指纹：

- `rotary` 120 与注意力 main 120 严格 1:1——每个注意力前都做一次 RoPE；
- MoE 家族计数（117/118/116）都聚在 \(58 \times 2 = 116\) 附近，注意力家族聚在 \(61 \times 2 = 122\) 附近减去窗口裁剪——两套整除性再次指向「61 层 × 2 微批」；
- 名为 `normal_...` 的随机数内核出现 117 次，恰与 MoE 层数同量级——推测与「模拟绝对均衡 MoE 路由」（[README.md:3](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/README.md#L3-L3)）的模拟过程有关（推断，待确认）。

**与 prefill/train 的家族差异**（连接 u2-l3、u2-l4）：没有 `notify_dispatch`/`get_dispatch_layout`/`clean_and_count_expert` 等 normal 版内核，路由链大幅缩短——低延迟路径把布局计算移出了每步热路径；注意力从 `compute_attn_ws` 换成 `splitkv_mla`；GEMM 从 groupedmasked 形态换成连续小形状的 `fp8_gemm_kernel`。

#### 4.3.4 代码实践

**实践 3：生成解码内核家族清单，并与 prefill 对比**（示例代码）。

1. 实践目标：按名称前缀聚合内核家族，输出「次数+总时长」清单，再对 prefill.json 运行同一脚本，比较两份轨迹的家族构成。
2. 操作步骤：

```python
import json
from collections import defaultdict

def family(name):
    for pat in ("flash_fwd_splitkv_mla", "dispatch_ll", "combine_ll",
                "fp8_gemm_kernel", "swiglu", "rotary_embedding",
                "per_token_cast_to_fp8", "layer_norm", "top2_sum_gate",
                "compute_attn_ws", "internode::dispatch", "internode::combine"):
        if pat in name:
            return pat
    return "other"

def report(path):
    ev = json.load(open(path))["traceEvents"]
    kern = [e for e in ev if e.get("ph") == "X" and e.get("cat") == "kernel"]
    agg = defaultdict(lambda: [0, 0])
    for e in kern:
        agg[family(e["name"])][0] += 1
        agg[family(e["name"])][1] += e["dur"]
    total = sum(e["dur"] for e in kern)
    print(f"== {path}: {len(kern)} kernels, {total} us ==")
    for k, (n, s) in sorted(agg.items(), key=lambda kv: -kv[1][1]):
        print(f"{k:28s} n={n:5d}  {s:8d}us  {s/total:6.1%}")

report("decode.json")
report("prefill.json")
```

3. 需要观察的现象：decode 里 `flash_fwd_splitkv_mla` 与两个 `*_ll` 家族的时间占比；prefill 里对应位置换成 `compute_attn_ws` 与 `internode::dispatch/combine`。
4. 预期结果（decode 侧实测）：`flash_fwd_splitkv_mla` ≈34.9%（33.3%+1.5%），`dispatch_ll`+`combine_ll` = 17,059µs ≈19.0%，其余家族均个位数百分比；prefill 侧见 u2-l4（通信 ≈48%、`fp8_gemm_kernel` 842 次、`compute_attn_ws` 122 次）。若你的百分比与此相差 ±1pp 以内即正常（聚合口径差异）。

#### 4.3.5 小练习与答案

1. **练习**：`fp8_gemm_kernel<2112,7168>` 的 `2112` 怎么来的？
   **答案**：\(2112 = 1536 + 576\)，即 q_lora_rank（1536）与 MLA 压缩 KV+RoPE 维（512+64=576）拼接后的输出宽度——这个形状把 q 升维与 kv 升维融合成了一次 GEMM（推断，与 V3 维度吻合；可在 DeepGEMM/推理引擎源码中核实）。
2. **练习**：为什么解码步里 LayerNorm（1–6µs）多达 364 次，却几乎不占时间？
   **答案**：364 ≈ 61 层×2 微批×3 个归一化位置，单体最长 6µs，总计约 1.2ms，只占 89.8ms 总内核时长的 ~1.3%。解码的小内核贵在「次数」，不在「单体」——这也是为什么 CUDA Graph（消除逐内核启动开销）对解码如此关键。
3. **练习**：统计 `vllm::topk_kernel` 的 3 次调用的 ts，判断它们落在稳态区间内还是边界区域。
   **答案**：用实践 1 的排序方法取出 ts，与稳态区间（首尾注意力 ts 421141–504454 的范围）比对。预期它们位于窗口首尾的非稳态段（采样发生在解码步末尾）；具体数值待本地验证。

## 5. 综合实践

**任务：为 decode.json 产出一份一页式「解码步内核图谱」报告。** 把本讲三个实践合并：

1. 用实践 1 的脚本得到注意力家族的次数、时长分布与占比，附前 20 事件表；
2. 用实践 2 的脚本得到槽周期、dispatch_ll 双峰分界（以 25µs 为界数一下两簇）、combine_ll 交替半周期；
3. 用实践 3 的脚本得到完整家族清单；
4. 在报告末尾用 3–5 句话回答：
   - 这一步解码里，GPU 时间花在哪三个大头上（应为：注意力 ≈35%、all-to-all ≈19%、其余计算 ≈46%）？
   - 哪个数据结构特征证明了「两个微批」（计数整除性 + combine_ll 交替相位）？
   - 「RDMA 发出后释放 SM」体现在哪个测量上（dispatch_ll 发送段 17µs 后隔着 ~106µs 计算才出现等待段）？

参考结论（实测）：窗口 ≈ 一个解码步（95.9ms，其中内核忙时 89.8ms，busy ≈93.7%）；61 层 × 2 微批以 ≈724µs 的槽节拍推进；注意力是唯一的数百微秒级大内核，通信靠两段式 ll 内核与计算错峰。

## 6. 本讲小结

- 解码注意力是**两段式**的：`flash_fwd_splitkv_mla_kernel`（240–245µs，grid 132 块铺满 132 个 SM，访存受限）+ `flash_fwd_splitkv_mla_combine_kernel`（10–11µs，grid 16384=128 请求×128 头），两段间隔仅 ~1µs；家族合计 31,308µs，占全部内核时长 **34.9%**。
- 解码步呈 **≈724µs 的槽节拍**：每槽 1 次注意力 + 2 次 dispatch_ll（发送段 17–19µs / 等待段 37–86µs）+ 2 次 combine_ll；稳态 116 槽 = 58 个 MoE 层 × **2 个微批**，注意力计数 120 ≈ 61 层 × 2 微批（窗口裁剪）。
- 双微批的判据不是内核名（CUDA Graph 回放下全部匿名，correlation 同为 7077），而是**计数整除性 + combine_ll 间隔的 ~375/~340µs 交替**。
- README 的「RDMA 发出后释放 SM、计算结束后再等」直接写在时间线上：dispatch_ll 发送段与等待段之间隔着 ~106µs 的其他计算内核。
- 解码的计算侧是「小内核海洋」：layer_norm 364 次（1–6µs）、per_token_cast 360 次、rotary 120 次、fp8_gemm 840 次（12–55µs）、swiglu 238 次、top2_sum_gate 117 次（6–7µs）。
- 分析单行大 JSON 的工程坑：C++ lambda 内核名含花括号，shell 正则 `[^}]*` 会漏事件（本文件漏 213 个），务必用 `"name": "[^"]*"` 锚定或直接 `json.load`。

## 7. 下一步学习建议

- 下一讲 u3-l1「用 Python 做轨迹定量分析」：把本讲的临时脚本沉淀成可复用的 `analyze_trace.py`（按 cat/名称/流聚合、Top-N 内核），你会需要它处理 prefill.json 那种 17MB 的文件。
- 之后 u3-l2 把本讲的「槽结构」升级为严格的区间运算：计算通信与计算的重叠率、暴露通信时间与计算气泡，用数字而不是节奏来刻画重叠质量。
- 延伸阅读：把本讲的 `splitkv_mla` 内核与 DeepSeek 开源的 FlashMLA、`dispatch_ll/combine_ll` 与 [DeepEP](https://github.com/deepseek-ai/DeepEP)、`fp8_gemm_kernel` 与 DeepGEMM 的实现对照（README [README.md:30](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/README.md#L30-L30) 给出了 DeepEP 链接），验证本讲标注「推断/待确认」的模板参数含义。
