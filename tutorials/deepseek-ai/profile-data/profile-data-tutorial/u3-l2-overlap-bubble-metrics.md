# 通信-计算重叠率与气泡量化

## 1. 本讲目标

学完本讲，你应该能够：

1. 把轨迹中的事件列表转换成 `(start, end)` 时间区间集合，并按流（stream）分组。
2. 手写区间合并（merge）与区间求交（intersect）两个算法，用它们算出「通信忙时」与「计算忙时」的交集总长度。
3. 准确定义并计算三个层次的指标：**重叠率**、**暴露通信**、**气泡 / 空闲**。
4. 解释一个关键现象：对 decode.json 直接套用「通信流 vs 计算流求交」会得到约等于 0 的重叠率——这不是失败，而是 decode「通信不占 SM」这一设计在指标语义上的必然反映（README 第 30 行的原话），本讲会给出正确的替代口径。

本讲是 u3-l1（`analyze_trace.py`）的直接续篇：u3-l1 回答「谁最耗时」，本讲回答「通信和计算在时间上到底有没有并行」。

## 2. 前置知识

本讲默认你已掌握前置讲义的以下结论，这里只做简要回顾：

- **事件区间**：只有 `ph == "X"` 的完成事件才有 `dur`；每条事件占据微秒时间轴上的左闭右开区间 \([ts,\ ts+dur)\)（u3-l1）。`ts` 是 Unix 纪元微秒，`traceEvents` 数组不按时间排序，分析前必须按 `ts` 排序（u1-l3）。
- **流的识别**：GPU 内核事件（`cat == "kernel"`）的 `tid` 就是流号，`args.stream` 与之相等（u1-l2、u3-l1）。train.json 里 kernel 分布在三条流上——本讲用 grep 重新核实：共 963 个 kernel，stream 7 有 907 个（主计算流）、stream 27 有 40 个（DeepEP 通信流）、stream 23 有 16 个（MoE 分组 GEMM 专用流）。
- **CUDA 流的顺序语义**：同一条流上的内核按提交顺序串行执行，因此同流内核的区间互不重叠（至多首尾相接）；不同流的内核才可能在时间上并行。这是本讲全部度量的物理基础。
- **SM 与 decode 的特殊语义**：SM 是 GPU 的流式多处理器（u2-l5）。README 明确说明：解码阶段的 all-to-all 通信**不占用 GPU SM**——RDMA 消息发出后所有 SM 被释放，等计算做完才回头等待通信完成。u2-l5 已实测：decode.json 中 `dispatch_ll`/`combine_ll` 与计算内核**同在 stream 7**。
- **集合直觉**：把每条内核看成数轴上的一条线段，「通信与计算是否并行」就变成两组线段有没有公共部分——一个纯区间代数问题。

> 解读前提（贯穿全手册）：采集时模拟了绝对均衡的 MoE 路由（[README.md:L3](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/README.md#L3)），本讲所有数字都只反映理想负载下的调度行为。

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| [train.json](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/train.json#L1) | 训练轨迹（EP64，[L70](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/train.json#L70) 的 `world_size: 64`）。多行格式，可精确引用行号。本讲的「双流真并行」样本：通信在 stream 27、计算在 stream 7/23。 |
| [decode.json](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/decode.json#L1) | 解码轨迹（EP128，`world_size: 128`）。**单行压缩大 JSON**（第 1 行超过 3MB），只能整体引用 `#L1`，文中引用的事件均为该行内的真实数据摘录。本讲的「同流 + SM 释放」样本。 |
| [README.md](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/README.md#L1) | 被本讲定量验证的三条声明的出处：训练的 DualPipe 重叠（[L11-L12](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/README.md#L11-L12)）、预填充双微批重叠（[L22](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/README.md#L22)）、解码通信不占 SM（[L30](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/README.md#L30)）。 |
| `profile-data-tutorial/u3-l1-quantitative-analysis.md` | 承接讲义：本讲的加载、按 `cat` 过滤、按 `tid`/名称聚合等套路都来自它。 |

## 4. 核心概念与源码讲解

### 4.1 流时间轴区间运算

#### 4.1.1 概念说明

轨迹查看器里那条彩色的「时间线」，本质是一个**数轴上的区间集合**：

- 每条 `ph == "X"` 的事件 \(e\) 占据区间 \( I_e = [ts_e,\ ts_e + dur_e) \)，单位微秒。
- 一条流在一段时间的「忙」，是它上面所有内核区间的**并集**；「闲」，是并集在观测窗口里的**补集**。
- 两组区间的「并行时间」，是两个并集的**交集**总长度。

为什么必须先取并集再运算，而不是直接把 `dur` 加起来？两个原因：

1. **避免重复计数**：跨流合并时（比如把 stream 7 和 stream 23 都算作「计算」），两条流上的内核可能同时执行，直接求和会把同一微秒数两遍。
2. **同流校验**：同一条流的内核区间理应互不重叠——如果你合并同流区间后发现总长度小于 `dur` 之和，说明数据里混进了不该在同流的东西（或时间戳异常），这本身就是一个数据质量检查。

#### 4.1.2 核心流程

三个纯函数构成全部底座（均为示例代码，可加进 u3-l1 的脚本）：

```python
# 示例代码：区间三件套
def to_intervals(events):
    """ph=='X' 的事件 -> [ts, ts+dur) 区间列表（微秒）"""
    return [(e["ts"], e["ts"] + e["dur"])
            for e in events if e.get("ph") == "X" and e.get("dur")]

def merge(iv):
    """排序 + 扫描合并重叠/相接区间，返回不重叠的有序区间列表"""
    out = []
    for s, e in sorted(iv):
        if out and s <= out[-1][1]:          # 与上一段重叠或相接
            out[-1] = (out[-1][0], max(out[-1][1], e))
        else:
            out.append((s, e))
    return out

def intersect(a, b):
    """双指针求两组区间的交集（输入需已合并排序）"""
    a, b, out = merge(a), merge(b), []
    i = j = 0
    while i < len(a) and j < len(b):
        s = max(a[i][0], b[j][0])
        e = min(a[i][1], b[j][1])
        if s < e:
            out.append((s, e))
        if a[i][1] < b[j][1]: i += 1
        else: j += 1
    return out

def total_len(iv):
    return sum(e - s for s, e in iv)
```

复杂度：合并是 \( O(n \log n) \)（排序主导），求交是 \( O(n + m) \)（双指针各走一遍）。对 decode.json 的 3437 个 kernel 来说毫秒级完成。

两条流「有没有并行」的判定流程：

```
按 tid 把 kernel 分组 -> 每组 to_intervals -> merge
通信组并集 C，计算组并集 K
并行时间 = total_len(intersect(C, K))
```

一个重要的先验结论：**同一条流自己跟自己求交，结果必为 0**——同流内核串行执行，区间互不重叠。这个「废话」在 4.2 节会变成解读 decode 的钥匙。

#### 4.1.3 源码精读

**（1）train.json 的三条流，实测构成。** 用 grep 核实（本讲实测）：963 个 kernel 中，`"tid": 7` 907 个、`"tid": 27` 40 个、`"tid": 23` 16 个。stream 27 上 40 个内核按名称清点是：`internode::dispatch` ×8、`internode::combine` ×8、`notify_dispatch` ×4、`cached_notify` ×12、`get_dispatch_layout` ×4、`per_token_cast_to_fp8_with_channels` ×4（量化内核，为 dispatch 准备 FP8 数据，恰好挂在通信流上）。

先看通信流的代表——`combine` 内核（专家结果聚合，u2-l3）：

[train.json:L35712-L35719](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/train.json#L35712-L35719) —— 一条完整的 `dpsk::ep::internode::combine` 完成事件：`pid: 0, tid: 27`（GPU 0 的 27 号流），`ts: 1740461679483541, dur: 6549`（微秒），`args` 里 `stream: 27`、`correlation: 46454`。它占据区间 \([1740461679483541,\ 1740461679490090)\)，长 6.549ms——这正是 u2-l3 分析过的那个 6.5ms 大通信窗口。

[train.json:L31324-L31329](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/train.json#L31324-L31329) —— 同流的 `internode::dispatch`（token 发往专家）：`ts: 1740461679472450, dur: 3320`，约 3.3ms，也远长于普通计算内核。

再看同一时间窗里**别的流**在干什么（时间单位统一截取后 6 位微秒）：

| 行号 | 流 | 内核 | 区间 | 与 combine 窗口 \([483541, 490090)\) 的关系 |
| --- | --- | --- | --- | --- |
| [L36040-L36041](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/train.json#L36040-L36041) | 7 | 分组 GEMM（fp8_dptp128c_acc） | \([483214,\ 483879)\) | 跨左端，交 338µs |
| [L36200-L36201](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/train.json#L36200-L36201) | 7 | per_channel_cast_and_transpose（FP8 量化转置） | \([484223,\ 484545)\) | 完全包含 |
| [L37204-L37205](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/train.json#L37204-L37205) | 7 | 分组 GEMM（m_grouped_fp8_ptp128c） | \([488525,\ 490264)\) | 跨右端，交 1565µs |
| [L35984-L35985](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/train.json#L35984-L35985) | 23 | 分组 GEMM（fp8_dptp128c_acc） | \([482860,\ 483562)\) | 跨左端，交 21µs |

这四行就是「通信流跑 combine 的同时，计算流 7 与 GEMM 流 23 都在内执行」的原始证据——区间求交算法要量化的正是这类公共部分。注意三个经典情形都出现了：完全包含、跨左端、跨右端。

**（2）decode.json 的同流证据。** decode.json 是单行文件，以下为 [decode.json:L1](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/decode.json#L1) 中的真实数据摘录（截取自该行）：

```json
{..."name": "void dpsk::ep::internode::dispatch_ll<true, 3, 10, 7168>(void*, float*, int*, long*, ...)",
 "pid": 0, "tid": 7, "ts": 1742522672421493, "dur": 18, ...}
{..."name": "void dpsk::ep::internode::dispatch_ll<true, 3, 10, 7168>(...)",
 "pid": 0, "tid": 7, "ts": 1742522672421614, "dur": 1603, ...}
{..."name": "void dpsk::ep::internode::dispatch_ll<true, 3, 10, 7168>(...)",
 "pid": 0, "tid": 7, "ts": 1742522672423753, "dur": 17, ...}
```

三个要点直接可读：

- `dispatch_ll` 的 `tid` 是 **7**——与计算内核同一条流（u2-l5 的结论，这里是一手复核）。
- `dur` 呈**双峰**：18µs / 17µs（发送段）与 1603µs（这一条是轨迹开头的冷启动等待，稳态等待段为 60~80µs，见 u2-l5）。
- 第 1 条结束于 \(1742522672421493+18=1742522672421511\)，第 2 条开始于 \(1742522672421614\)——中间 103µs 的空档就是**另一个微批的计算内核在填充**（u2-l5 的「发送→填隙→等待」三段式）。

另外注意数据卫生：decode.json 里也存在极少量挂在 `tid: 16` 上的 `dispatch_ll`/`combine_ll`（摘录：`"tid": 16, "ts": 1742522672508622, "dur": 202` 与 `"tid": 16, "ts": 1742522672509385, "dur": 362`，位于轨迹末尾，是 CUDA Graph 之外的零散启动）。所以脚本**不能假设「所有 kernel 都在 stream 7」**，要按 `tid` 实际分组汇报。

#### 4.1.4 代码实践

1. **实践目标**：实现区间三件套，并用 train.json 验证「同流互不重叠、跨流才会重叠」。
2. **操作步骤**：
   - 把 4.1.2 的四个函数存进 `intervals.py`；按 u3-l1 的方式 `json.load` train.json 并按 `ts` 排序。
   - 取出 `cat == "kernel"` 的 963 个事件，按 `tid` 分成 `s7 / s23 / s27` 三组。
   - 对每组：比较 `sum(dur)` 与 `total_len(merge(to_intervals(组)))`——同流二者应几乎相等。
   - 再把 stream 7 与 stream 23 合并成「计算」：比较 `total_len(s7) + total_len(s23)` 与 `total_len(merge(s7 + s23))`，差值就是两条计算流彼此重叠的时间。
3. **需要观察的现象**：同流合并前后长度几乎不变；跨流合并后长度变小。
4. **预期结果**：`merge` 后 `total_len` ≤ `Σdur`，同流取等（可能有微秒级边界误差），跨流严格变小。具体差值**待本地验证**。
5. 顺带打印三条约束：`len(s7)==907`、`len(s23)==16`、`len(s27)==40`（本讲 grep 已核实），不符则说明过滤条件写错了。

#### 4.1.5 小练习与答案

**练习 1**：为什么任意一条流「自己与自己」求交的总长度必为 0？
**答案**：CUDA 流按提交顺序串行执行内核，后一个内核最早在前一个结束时启动，因此同流内核的 \([ts, ts+dur)\) 区间互不重叠（至多相接）；一组互不重叠的区间与自身的交集就是自身……但「求交」指的是两组**不同**事件的区间——同一组当然等于自身。准确说法是：把一条流按「内核名」拆成两个子集（比如通信内核 vs 计算内核）再求交，由于所有区间都在同一条串行流上，任意两个不同内核的区间不相交，交集必为空。这正是 decode 会遇到的情况。

**练习 2**：`total_len(merge(A))` 什么时候等于 `Σdur`？
**答案**：当 A 内所有区间互不重叠（允许相接）时。相接（前一个的 end 等于后一个的 start）不影响总长度。一旦存在并行执行的区间（跨流），合并会「压扁」重叠部分，总长度变小。

**练习 3**：如果把 CPU 侧 `cuda_runtime` 事件的区间与 GPU 侧 `kernel` 事件的区间求交，预计得到什么？这说明什么？
**答案**：交集接近 0（只会捡到 CPU API 调用尚未返回、GPU 内核已开始的微小边界）。原因是 CPU 提交与 GPU 执行异步：GPU 内核的 `ts` 晚于对应 CUDA API 的 `ts`（u2-l1 用 Δ=ts_kernel−ts_api 量化过：train 微秒级、prefill 呈约 16ms 深排队）。这提醒我们：**重叠分析必须限定在同一执行域（GPU 侧）内**，CPU 墙钟与 GPU 执行不可混算（u3-l1 的教训）。

### 4.2 重叠率定义与计算

#### 4.2.1 概念说明

设 \( C \) 为通信内核区间的并集，\( K \) 为计算内核区间的并集（均已在 GPU 侧、均已 merge）。核心指标一族：

\[
R_{\text{comm}} = \frac{|C \cap K|}{|C|}, \qquad
R_{\text{comp}} = \frac{|C \cap K|}{|K|}
\]

- \( R_{\text{comm}} \)（通信重叠率）：通信忙时里，有多大比例同时有计算在跑——衡量**通信被计算隐藏**的程度。
- \( R_{\text{comp}} \)（计算并行率）：计算忙时里，有多大比例与通信同时发生——它受通信总时长约束，通常天然偏小，解读时要看分母。
- **暴露通信** \( E = |C| - |C \cap K| \)：没有任何计算遮挡的通信时间，是真正「卡住步进」的净损失候选。

两个口径问题必须先想清楚，否则数字没有意义：

1. **「通信」按流分还是按名称分？** train.json 里 stream 27 挂着 4 个量化内核（`per_token_cast_to_fp8`），它们是计算但恰在通信流上。按流分会把它们算进 \( C \)；按名称分（`name` 含 `dpsk::ep::`）则不会。两个口径都合法，差几个百分点，**报告时必须写明**。
2. **decode 不能按流分。** `dispatch_ll`/`combine_ll` 与计算内核同在 stream 7，按流分会把「通信集」和「计算集」定义成同一条流——由 4.1 的先验结论，交集必为 0。decode 必须按名称分（`name` 含 `dispatch_ll` 或 `combine_ll`），且要重新理解 0 的含义（见 4.2.3）。

#### 4.2.2 核心流程

计算流水线：

```
kernel 事件 -> (按流或按名称) 分成 C 组 / K 组
C_iv = merge(to_intervals(C组));  K_iv = merge(to_intervals(K组))
ov  = total_len(intersect(C_iv, K_iv))
输出: R_comm = ov/total_len(C_iv)
      R_comp = ov/total_len(K_iv)
      暴露通信 E = total_len(C_iv) - ov        （换算成毫秒）
```

先用 4.1.3 的真实数据做一个**手算样例**（可当单元测试）：

- \( C = \{[483541,\ 490090)\} \)（combine，|C|=6549µs）；
- \( K' = \{[483214,\ 483879),\ [488525,\ 490264)\} \)（L36040 与 L37204 两个计算内核）；
- 交集：\([483541,\ 483879)\) 长 338µs，\([488525,\ 490090)\) 长 1565µs；
- \( |C \cap K'| = 1903\mu s \)，\( R_{\text{comm}} = 1903 / 6549 \approx 29.1\% \)。

注意这只是「2 个计算内核」的样本；把 stream 7 上落在该窗口里的全部内核（grep 已见十余个，u2-l3 统计为 17 个）都加进 \( K \)，u2-l3 实测该窗口计算覆盖率达 80.6%。

#### 4.2.3 源码精读

**（1）train：双流真并行的完整证据链。** 上节表格（4.1.3）已经给出区间级证据；这里补齐「量级」：u2-l3 实测 train.json 中 36 个 `dpsk::ep::` 内核占 GPU kernel 总时长的 **46.5%**，全部位于 stream 27；在最大的那个 combine 窗口（[train.json:L35712-L35713](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/train.json#L35712-L35713)）内，计算流跑了 17 个内核、覆盖率 80.6%。这正是 README 对训练场景的声明——「a pair of individual forward and backward chunks in DualPipe」（[README.md:L11-L12](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/README.md#L11-L12)）——在时间轴上的定量形态：**两条物理流各自忙碌，交集就是省下来的时间**。

**（2）decode：重叠率语义的切换。** README 对解码的原话（[README.md:L30](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/README.md#L30)）：

> "the all-to-all communication during decoding does not occupy GPU SMs: after RDMA messages are issued, all GPU SMs are freed, and the system waits for the all-to-all communication to complete after the computation has finished."

三个_clause_ 分别对应轨迹里的三个可测事实：

| README 声明 | 轨迹证据 |
| --- | --- |
| 消息发出后 SM 全部释放 | `dispatch_ll` 发送段只有约 17~18µs（4.1.3 摘录的 `dur: 18` / `dur: 17`） |
| 计算照常进行 | 两个 `dispatch_ll` 之间约 103µs 的空档由另一微批的计算内核填充（u2-l5/u2-l6 实测） |
| 计算结束后才等待通信完成 | 时长双峰里的长尾（冷启动 1603µs、稳态 60~80µs）是显式的等待段 |

于是「重叠率」在 decode 里的含义发生了切换：train 的 \( R_{\text{comm}} \) 度量**资源级并发**（两条流同时占用 SM）；decode 里通信与计算共享一条串行流，内核区间交集恒为 0，但**消息飞行时间**与**另一微批的计算**是重叠的——这是**延迟隐藏级**的重叠。硬套 train 的公式得到的 0 不是「没有重叠」，而是「重叠不再表现为区间相交」。正确的 decode 口径在 4.3 给出。

顺带一提 prefill 作为对照：u2-l4 实测其通信流（stream 16）**91.2%** 的忙时与计算并行——介于 train 与 decode 之间的「双微批 + 独立通信流」形态，README 的对应声明在 [README.md:L22](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/README.md#L22)。

#### 4.2.4 代码实践

1. **实践目标**：对 train.json 算出全局 \( R_{\text{comm}} \)、\( R_{\text{comp}} \)、暴露通信，并复算手算样例。
2. **操作步骤**：
   - 写 `overlap.py`：加载 train.json → 取 `cat=="kernel"` → 按两种口径分别计算——
     - 流口径：\( C \) = `tid==27` 全部内核，\( K \) = `tid==7` 与 `tid==23` 全部内核；
     - 名称口径：\( C \) = `name` 含 `dpsk::ep::` 的内核，\( K \) = 其余 kernel。
   - 输出 `total_len(C)`、`total_len(K)`、`ov`、两个比率与 `E`（毫秒）。
   - 单元自检：截取 4.2.2 的三个区间硬编码进测试，断言 `ov == 1903`。
3. **需要观察的现象**：两种口径的 \( R_{\text{comm}} \) 接近但不相等（差值来自 stream 27 上那 4+4 个量化/布局内核）；\( R_{\text{comp}} \) 明显小于 \( R_{\text{comm}} \)，因为 train 里计算总时长远大于通信总时长。
4. **预期结果**：\( R_{\text{comm}} \) 为高位（参考：单个 6.5ms 窗口内 80.6%；全局值**待本地验证**）；暴露通信集中在每个 MoE 层循环的边缘（dispatch/combine 首尾无人遮挡的片段）。若得 0 或 100%，先查口径：得 0 多半是把 CPU 事件混了进来，得 100% 多半是 \( C \)、\( K \) 取了同一集合。

#### 4.2.5 小练习与答案

**练习 1**：\( R_{\text{comm}} = 100\% \) 且暴露通信为 0，能否得出「通信免费」的结论？
**答案**：不能。区间交只度量**时间共现**，不度量资源冲突：train 的通信内核与计算内核同时占用 SM（u2-l3 实测通信内核仅占约 15% 的 SM，仍有挤占）、共享显存带宽与 NVLink/RDMA 网络带宽；通信还会拖慢与之并行的计算内核。100% 只说明「没有裸露的通信空等」，不说明零代价。

**练习 2**：为什么 prefill 的 \( R_{\text{comm}} \)（91.2%）能比 train 更高？
**答案**：prefill 用两个微批交错调度（[README.md:L22](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/README.md#L22)）：微批 A 做 all-to-all 时微批 B 恰在做计算，且推理预填充没有反向、调度更规整；train 的 DualPipe 虽也成对交错前反向 chunk，但前反向负载不均，chunk 边缘更容易露出通信。

**练习 3**：用 4.2.2 的手算样例，把 \( K' \) 换成「L36040 + L37204 + L36200」三个内核，重算 \( R_{\text{comm}} \)。
**答案**：L36200 的区间 \([484223,\ 484545)\) 完全落在 combine 窗口内，贡献 322µs；交集变为 \(338 + 322 + 1565 = 2225\mu s\)，\( R_{\text{comm}} = 2225/6549 \approx 34.0\% \)。可见 \( R_{\text{comm}} \) 对 \( K \) 的覆盖程度敏感——全局统计时必须把窗口内**全部**计算内核计入。

### 4.3 气泡/空闲指标定义

#### 4.3.1 概念说明

前两个指标只关心 \( C \) 与 \( K \) 的关系；第三个维度问：**观测窗口本身被用满了吗？**

设观测窗口 \( W = [w_0, w_1) \)（对 train 可取 kernel 的最早 `ts` 到最晚 `ts+dur`，或直接取 1F1B 注解区间——u2-l2 实测约 112ms；对 decode 可取首尾 kernel 围成的区间）。定义：

\[
\text{bubble} = |W \setminus (C \cup K)|, \qquad
\text{cov}_{\text{comp}} = \frac{|K|}{|W|}, \qquad
\text{cov}_{\text{comm}} = \frac{|C|}{|W|}
\]

- **气泡（bubble）**：窗口内既无计算也无通信内核的空窗——纯粹的浪费（对单流视角而言）。
- **计算覆盖率** \( \text{cov}_{\text{comp}} \)：窗口里计算内核在跑的比例，是「这条 GPU 忙不忙」的第一指标。

对 decode，还要再加一个该场景特有的指标——**等待段暴露**。`dispatch_ll`/`combine_ll` 的双峰时长（4.1.3）把每次调用分成两类：

- **发送段**（短，约 17~18µs）：把 RDMA 消息发出去，发完即释放 SM；
- **等待段**（长，稳态 60~80µs，冷启动可达 1.6ms）：回头等对端数据到齐，这期间内核仍占着流与 SM。

以 30µs 为阈值切分双峰谷底，**等待段总时长**就是 decode 通信的真实可见成本：它不是气泡（有内核在跑），但也不是有效计算。于是 decode 的账本变成：

\[
|W| \approx |K| + \underbrace{\text{发送段}}_{\text{占流但极短}} + \underbrace{\text{等待段}}_{\text{显式成本}} + \text{bubble}
\]

#### 4.3.2 核心流程

```
W   = (min(ts), max(ts+dur))            # 或注解区间
all = merge(to_intervals(全部 kernel))
bubble = (W1 - W0) - total_len(all)
cov_comp = total_len(K) / (W1 - W0)

# decode 专属：按阈值切分通信内核
comm = [k for k in kernels if ("dispatch_ll" in k["name"] or "combine_ll" in k["name"])]
send = [k for k in comm if k["dur"] <= 30]     # 发送段
wait = [k for k in comm if k["dur"] >  30]     # 等待段
```

> 提示：train 的正常（normal）通信内核是毫秒级大块、独占 stream 27；decode 的 `_ll` 内核是几十微秒小颗粒、与计算同流。同一套「通信」词，在两份轨迹里是两种完全不同的时间形态——这正是 u2-l5 所说「两代内核」的定量表现。

#### 4.3.3 源码精读

**（1）decode 的节奏基准。** u2-l6 实测：decode.json 共 3437 个 kernel，稳态呈约 724µs 的「槽」节拍，每槽 1 次注意力 + 2 次 `dispatch_ll` + 2 次 `combine_ll`，116 个槽 = 58 个 MoE 层 × 2 微批；`dispatch_ll`/`combine_ll` 各 235 次、合计占 kernel 总时长的 **19.0%**，注意力家族（`flash_fwd_splitkv_mla_kernel` + combine）占 **34.9%**。这组数字就是本节账本的「已知项」：约 19% 的通信 + 约 35% 的注意力 + 其余约 46% 的小内核（fp8_gemm、layer_norm、rotary、swiglu、top2_sum_gate 等，u2-l6）。

**（2）等待段的原始形态。** 回看 4.1.3 的 decode 摘录（[decode.json:L1](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/decode.json#L1)）：

```
[1742522672421493, 1742522672421511)  dispatch_ll  发送段 18µs
        ↓ 中间 103µs：另一微批的计算内核（stream 7 上其它 kernel）
[1742522672421614, 1742522672423217)  dispatch_ll  等待段 1603µs（冷启动）
```

「发送 → 填隙 → 等待」三段式在区间层面一目了然：**等待段被有意排在计算之后**，这正是 [README.md:L30](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/README.md#L30) 「waits for the all-to-all communication to complete **after** the computation has finished」的轨迹实现。

**（3）train 侧的对照。** train 的通信内核（如 [train.json:L35712-L35713](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/train.json#L35712-L35713) 的 combine，6.5ms）不切成两段：它从头到尾占着 stream 27 与一部分 SM，靠**另一条流**（stream 7）同时跑计算来隐藏。所以 train 的账本是「两条流各自的覆盖率 + 交集」，decode 的账本是「一条流的覆盖率 + 通信颗粒的成分分解」。同一套指标框架，两种填写方式。

#### 4.3.4 代码实践

1. **实践目标**：给 decode.json 建立完整账本：计算覆盖率、气泡、通信发送/等待分解；再对 train.json 重复，产出对比。
2. **操作步骤**：
   - 写 `decode_metrics.py`：加载 decode.json（单行大文件，用 `json.load`，u3-l1）→ `cat=="kernel"` → 按名称分出 `comm`（含 `dispatch_ll`/`combine_ll`）与 `K`（其余）。
   - 先做「反面实验」：`total_len(intersect(to_intervals(comm), to_intervals(K)))`——预计约为 0，因为同流串行。
   - 再算 \( W \)、bubble、\( \text{cov}_{\text{comp}} \)、`total_len(send)`、`total_len(wait)`（阈值 30µs），并打印 `len(send)`、`len(wait)`。
   - 对 train.json 用 4.2 的 `overlap.py` 输出同名列，拼成一张两行对比表。
3. **需要观察的现象**：
   - decode：内核区间交集 ≈ 0（可能有微秒级边界噪声，以及末尾 `tid==16` 的零散 `_ll` 内核与主流的微弱相交——记得按 `tid` 分组检查）；`len(send)+len(wait) == 470`（235+235，u2-l5）；等待段总时长接近通信总时长的主体。
   - train：交集显著大于 0；`cov_comp` 与 `cov_comm` 之和可以超过 100%（两条流并行），而 decode 的两个覆盖率之和不超过 100%（同一条流）。
4. **预期结果**：decode 的 `cov_comp` 加上通信占比（19.0%）再加气泡应近似凑满窗口；train 的 \( R_{\text{comm}} \) 高、且 \( \text{cov}_{\text{comp}} + \text{cov}_{\text{comm}} > 100\% \)。**覆盖率与气泡的具体数值待本地验证**——本讲只锚定了可复核的锚点（19.0%、34.9%、46.5%、91.2%、80.6%）。

#### 4.3.5 小练习与答案

**练习 1**：decode 的「等待段」应该计入通信、气泡，还是计算？
**答案**：计入**通信（等待成分）**。它有真实内核在执行（区间非空、占流占 SM），所以不是气泡；但它不做有效功，是 RDMA 往返的显式代价。气泡专指窗口内连内核都没有的空窗。把等待段从通信里剔掉、再把气泡单列，三者相加才能完整解释窗口。

**练习 2**：把 train 的观测窗口从「首尾 kernel」改成 1F1B 注解区间（约 112ms，u2-l2），各指标会怎么变？
**答案**：窗口变小、更贴近被评估的调度对象，首尾的暖机/收尾空转被剔除，bubble 与覆盖率的分母更公平；\( C \)、\( K \) 本身不变（kernel 都落在注解区间内），所以交集与 \( R_{\text{comm}} \)、\( R_{\text{comp}} \) 基本不变。窗口选择是口径问题，报告时必须注明。

**练习 3**：decode 由 CUDA Graph 回放（3437 个 kernel 共享同一 correlation，u2-l5），这对本讲的区间运算有影响吗？
**答案**：没有。区间运算只用 `ts`/`dur`/`tid`/`name`，与 correlation 无关，CUDA Graph 只是把回放的内核照常记录进轨迹。受影响的是 u2-l1 的「内核反查 CPU 算子」链路（correlation 失去区分度）——这也是为什么 decode 的双微批结构只能靠计数与间隔交替来证实（u2-l6 的做法）。

## 5. 综合实践

把三个模块串成一个可复用工具 `compare_overlap.py`（示例代码骨架）：

```python
# 示例代码：compare_overlap.py —— 两条轨迹同口径对比
import json, sys
from intervals import to_intervals, merge, intersect, total_len   # 4.1 的三件套

def load(path):
    with open(path) as f:
        ev = json.load(f)["traceEvents"]
    ks = [e for e in ev if e.get("cat") == "kernel" and e.get("dur")]
    ks.sort(key=lambda e: e["ts"])
    return ks

def is_comm(k):                       # 名称口径：对 train/decode 都成立
    n = k["name"]
    return "dpsk::ep::" in n or "dispatch_ll" in n or "combine_ll" in n

def report(path, wait_th=30):
    ks = load(path)
    comm = [k for k in ks if is_comm(k)]
    comp = [k for k in ks if not is_comm(k)]
    C, K = to_intervals(comm), to_intervals(comp)
    ov   = total_len(intersect(C, K))
    w0   = min(k["ts"] for k in ks); w1 = max(k["ts"] + k["dur"] for k in ks)
    wait = sum(k["dur"] for k in comm if k["dur"] > wait_th)
    print(f"{path}: |C|={total_len(C)/1e3:.1f}ms |K|={total_len(K)/1e3:.1f}ms "
          f"ov={ov/1e3:.1f}ms R_comm={ov/total_len(C):.1%} R_comp={ov/total_len(K):.1%} "
          f"bubble={(w1-w0-total_len(merge(C+K)))/1e3:.1f}ms "
          f"cov_comp={total_len(K)/(w1-w0):.1%} wait={wait/1e3:.2f}ms")

for p in sys.argv[1:]:
    report(p)
```

任务：对 `train.json` 与 `decode.json`（有余力加 `prefill.json`）各运行一次，产出下表并补全数字（**除锚点外均待本地验证**）：

| 指标 | train.json（EP64） | decode.json（EP128） |
| --- | --- | --- |
| world_size | 64 | 128 |
| kernel 总数 | 963 | 3437 |
| 通信定位方式 | stream 27 / `dpsk::ep::` 名称 | **必须**用 `dispatch_ll`/`combine_ll` 名称（同流） |
| \|C\| 占 kernel 总时长 | ≈46.5%（u2-l3） | ≈19.0%（u2-l5） |
| 内核区间交集 ov | 显著 > 0 | ≈ 0（同流串行） |
| R_comm | 高（单窗口参考 80.6%） | 失去资源级含义 → 看 wait 分解 |
| 重叠的实现机制 | 双流并行（DualPipe 成对 chunk） | SM 释放 + 消息飞行与计算重叠 |
| README 对应声明 | L11-L12 | L30 |

最后用三到五句话写下你的结论，应当能回答：为什么两个场景的 \( R_{\text{comm}} \) 数字不可直接比较？各自的「重叠」分别隐藏了什么成本？气泡在两边各出现在什么位置？

## 6. 本讲小结

- 事件即区间：`ph=="X"` 的内核占据 \([ts,\ ts+dur)\)（微秒），merge/intersect/total_len 三个纯函数构成全部定量底座，复杂度 \( O(n\log n) \)。
- 重叠率一族：\( R_{\text{comm}}=|C\cap K|/|C| \)（通信被隐藏程度）、\( R_{\text{comp}}=|C\cap K|/|K| \)（计算与通信并行程）、暴露通信 \( E=|C|-|C\cap K| \)——分母口径（按流 vs 按名称）必须写明。
- train（EP64）是**双流真并行**：通信独占 stream 27（36 个 DeepEP 内核 ≈46.5% kernel 时长），与 stream 7/23 的计算区间相交，单 6.5ms combine 窗口内计算覆盖 80.6%（u2-l3）。
- decode（EP128）是**同流 + SM 释放**：`dispatch_ll`/`combine_ll` 与计算同在 stream 7，内核区间交集必 ≈0；重叠靠「发送段约 17µs → 另一微批计算填隙约 103µs → 等待段」实现，重叠率从资源级并发变成延迟隐藏，需改用等待段分解与覆盖率账本。
- 气泡 = 窗口内 \( C\cup K \) 之外的空窗；decode 账本 \( |W|\approx|K|+\text{发送}+\text{等待}+\text{bubble} \)，train 账本 \( \text{cov}_{comp}+\text{cov}_{comm} \) 可超 100%。
- 一切数字都以「模拟绝对均衡 MoE 路由」为前提（[README.md:L3](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/README.md#L3)）。

## 7. 下一步学习建议

- 下一讲 **u3-l3（三种配置横向对比）** 会把本讲的 `compare_overlap.py`、u3-l1 的 `analyze_trace.py` 与 u2 系列的内核族清单合成一张三轨迹总表（train/prefill/decode 的 world_size、通信内核族占比、重叠率、Top 内核），本讲的表格就是它的雏形——先把数字跑出来。
- 建议重读三个引用点的原始措辞，体会「声明 → 指标 → 证据」的闭环：[README.md:L11-L12](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/README.md#L11-L12)（DualPipe）、[README.md:L22](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/README.md#L22)（双微批）、[README.md:L30](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/README.md#L30)（SM 释放）。
- 想深挖通信机制本身，可对照 [DualPipe](https://github.com/deepseek-ai/dualpipe) 与 [DeepEP](https://github.com/deepseek-ai/DeepEP) 仓库阅读 `internode::dispatch/combine`（normal）与 `*_ll`（low-latency）两代实现的差异——u3-l4 将用 torch.profiler 自制轨迹，把本讲的方法闭环到你自己的模型上。
