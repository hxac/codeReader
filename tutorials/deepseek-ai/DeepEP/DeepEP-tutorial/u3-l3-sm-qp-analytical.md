# SM 与 QP 数量的解析式计算（无需 auto-tuning）

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清楚 DeepEP V2 为什么用「带宽建模 + 解析式公式」来决定 dispatch/combine 内核用多少个 SM，而不像 V1 那样做 auto-tuning。
- 读懂 `ElasticBuffer.get_theoretical_num_sms` 里的 `sm_read / sm_write / rdma_traffic / nvlink_traffic` 四个归一化流量是怎么累加出来的，并能复现最后的 `num_sms` 公式。
- 理解「expected top-k」的组合数公式，明白它是建立在「均衡门控（balanced gate）」假设之上的期望跨 rank 数。
- 读懂 `get_theoretical_num_qps` 在 direct 模式与 hybrid 模式下分别如何分配 RDMA 队列对（QP），以及它和构造期 `num_allocated_qps` 上限的关系。
- 自己设置 `EP_BUFFER_DEBUG=1`，把内部流量打印出来，并解释最终 `num_sms` 是怎么算出来的。

## 2. 前置知识

本讲假设你已经学过 **u3-l1（物理域与逻辑域）**，知道以下几个事实：

- `num_ranks` 是全局进程数；物理域分为 `num_rdma_ranks`（节点间 RDMA 域）和 `num_nvlink_ranks`（节点内 NVLink 域），恒满足 `num_ranks = num_rdma_ranks × num_nvlink_ranks`。
- 逻辑域分为 `num_scaleout_ranks` 和 `num_scaleup_ranks`。`num_scaleout_ranks == 1` 时是 **direct 模式**（直接单级通信）；`> 1` 时是 **hybrid 模式**（scaleout 用 RDMA、scaleup 用 NVLink 的两级通信）。
- MoE 的 dispatch 把每个 token 按它的 top-k 专家发到对应 rank；combine 是逆过程。

下面三个通俗概念是本讲的基础：

1. **SM（Streaming Multiprocessor）**：GPU 上执行 kernel 的「核心」。一块 H100 有 132 个 SM。DeepEP 的通信内核只占用其中**一部分** SM，把剩下的留给用户的计算 kernel，从而实现「通信-计算重叠」。
2. **QP（Queue Pair）**：RDMA 网卡上的发送/接收队列对。每多用一组 QP，就能让更多 RDMA 操作并行下发，但 QP 太多会带来「门铃（doorbell）抖动」开销。DeepEP 让每个 channel 拥有独立 QP 来榨干 RDMA 并行度。
3. **门控（gate）**：MoE 里给每个 token 选 top-k 专家的路由器。本讲的数学只对**均衡门控**成立——即每个 token 的 top-k 选择在专家集合上均匀分布。代码注释明确指出：DeepSeek-V3 的 group-limited gate 不适用本函数。

> 关键直觉：DeepEP 要回答的问题是——「给定专家数、top-k、拓扑、链路带宽，我应该用几个 SM、几个 QP，才能让通信 kernel 既不浪费 SM、又不被某条物理链路卡住？」V1 的答案是「跑一遍 benchmark 找最优 config」（auto-tuning）；V2 的答案是「用一个公式直接算出来」。本讲就是这个公式。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [`deep_ep/buffers/elastic.py`](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/buffers/elastic.py) | `ElasticBuffer` 的 Python 接口。本讲的核心 `get_theoretical_num_sms`（L728–L834）与 `get_theoretical_num_qps`（L836–L853）都在这里；构造期 `num_allocated_qps` 自动分配在 L326–L335；`dispatch`/`combine` 里「0 表示自动」的调用点分别在 L926–L930 与 L1085–L1088。 |
| [`deep_ep/utils/envs.py`](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/utils/envs.py) | 探测物理带宽的工具函数：`get_nvlink_gbs`（L192–L219，解析 `nvidia-smi nvlink -s`）、`get_rdma_gbs`（L245–L268，解析 `ibstat`）、`check_fast_rdma_atomic_support`（L222–L242，判断 NIC 是否为 MT4131 及更新型号）。 |
| [`deep_ep/utils/semantic.py`](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/utils/semantic.py) | `weak_lru` 装饰器（L9–L27），让 `get_theoretical_num_sms` 的结果按 `(self, 参数)` 缓存且不泄漏内存。 |

调用方向：`dispatch/combine`（用户 API）→ 看到 `num_sms==0` → 调 `get_theoretical_num_sms` → 内部调 `get_rdma_gbs/get_nvlink_gbs` 探测带宽 → 算出 `num_sms` → 再调 `get_theoretical_num_qps(num_sms)` → 把 `num_sms, num_qps` 传给 C++ runtime 启动内核。

## 4. 核心概念与源码讲解

### 4.1 SM 建模：用带宽均衡推导最优 SM 数

#### 4.1.1 概念说明

dispatch/combine 内核是一个「数据搬运」kernel：它从 HBM 读 token、写 send buffer、经 NVLink/RDMA 发到对端、对端再写入 recv buffer。整个过程中有三类「带宽资源」在竞争：

- **每 SM 的 HBM 读带宽** `sm_read_gbs`（默认 200 GB/s）；
- **每 SM 的 HBM 写带宽** `sm_write_gbs`（默认 50 GB/s）；
- **物理链路带宽**：NVLink 带宽 `nvlink_gbs` 与 RDMA 带宽 `rdma_gbs`。

如果 SM 太少，HBM 读写会成为瓶颈，内核「喂不饱」链路；如果 SM 太多，又会和用户的计算 kernel 抢 SM，破坏通信-计算重叠。**最优 SM 数 = 让「HBM 搬运时间」恰好等于「物理链路传输时间」的那个点**。这就是「带宽均衡（bandwidth balancing）」。

V1（NVSHMEM 后端）用 auto-tuning：实际跑若干组 config，选最快的。代价是首次启动慢、且要把结果存进 `config_map`。V2 改成解析式：直接根据带宽建模算出 SM 数，**无需任何预热运行**。这就是本讲标题里「无需 auto-tuning」的含义。

#### 4.1.2 核心流程

整个推导分四步：

**第一步：归一化流量单位。** 定义「1 个单位」等于 dispatch copy epilogue 的 HBM 读总量：

\[
V \;=\; \text{num\_tokens} \times \text{num\_expected\_topk} \times \text{data\_size\_per\_token}
\]

即「所有 rank 收到的 token 总字节数」。后面所有流量都是这个单位的**分数**，这样不同规模的问题可以套同一个公式。注意：这里的 `num_expected_topk` 不是用户传的 `num_topk`，而是「一个 token 平均会落到几个不同 rank 上」的期望值（见 4.2）。

**第二步：累加四个归一化流量。** 用 `sm_read`、`sm_write` 记录整个作业的 HBM 读/写总量占 V 的比例；用 `rdma_traffic`、`nvlink_traffic` 记录跨 rank 的链路流量占 V 的比例。direct 与 hybrid 模式的累加项不同（见 4.1.3）。

**第三步：找瓶颈链路。** 比较 RDMA 与 NVLink 谁更「堵」：

\[
(\text{bounded\_traffic},\ \text{bounded\_gbs}) \;=\; \arg\max_{(\text{link})}\frac{\text{traffic}_{\text{link}}}{\text{gbs}_{\text{link}}}
\]

即流量/带宽比最大的那条链路就是瓶颈。单节点时 RDMA 流量为 0，瓶颈自然是 NVLink。

**第四步：令 HBM 时间 = 链路时间，解出 num_sms。** 总读流量 `sm_read × V` 平摊到 `num_sms` 个 SM 上，每个 SM 以 `sm_read_gbs` 读取，读时间为 `sm_read × V / (num_sms × sm_read_gbs)`；瓶颈链路传输 `bounded_traffic × V` 字节，耗时 `bounded_traffic × V / bounded_gbs`。令二者相等：

\[
\frac{\text{sm\_read} \times V}{\text{num\_sms} \times \text{sm\_read\_gbs}} \;=\; \frac{\text{bounded\_traffic} \times V}{\text{bounded\_gbs}}
\quad\Longrightarrow\quad
\text{num\_sms} \;=\; \frac{\text{bounded\_gbs}}{\text{bounded\_traffic}} \cdot \frac{\text{sm\_read}}{\text{sm\_read\_gbs}}
\]

可以看到 V 被约掉了——所以代码里**根本不需要知道真实的 token 数或 hidden**，只用归一化分数即可。对写带宽同理，取两者较大者：

\[
\text{num\_sms} \;=\; \max\!\left(
\frac{\text{bounded\_gbs}}{\text{bounded\_traffic}} \cdot \frac{\text{sm\_read}}{\text{sm\_read\_gbs}},\ \
\frac{\text{bounded\_gbs}}{\text{bounded\_traffic}} \cdot \frac{\text{sm\_write}}{\text{sm\_write\_gbs}}
\right)
\]

最后做四步工程调整：① 乘 1.25 安全系数；② 下限 4 个 SM；③ 用 `align(..., 2)` 取偶数（Hopper 上 SM 成对分配更友好）；④ 若 `prefer_overlap_with_compute=False` 则强制至少 64 个 SM（既然不和计算重叠，就放开跑满）；⑤ 不超过设备总 SM 数。

#### 4.1.3 源码精读

先看整体签名与带宽探测。`weak_lru` 让结果按参数缓存（重复 dispatch 同一组 `num_experts/num_topk` 不会重算）；`rdma_gbs/nvlink_gbs` 默认 0 表示自动探测，单节点（`num_rdma_ranks<=1`）时不探测 RDMA：

[deep_ep/buffers/elastic.py:L728-L768](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/buffers/elastic.py#L728-L768) —— `get_theoretical_num_sms` 的签名、断言「V3.0 group-limited gate 不适用」、以及自动调 `get_rdma_gbs()/get_nvlink_gbs()` 探测带宽，并初始化 `sm_read/sm_write/rdma_traffic/nvlink_traffic` 四个累加器为 0。

接着是 expected top-k 的计算（详见 4.2）：

[deep_ep/buffers/elastic.py:L770-L778](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/buffers/elastic.py#L770-L778) —— 内嵌 `get_expected_topk(num_groups)` 用组合数算「期望跨组数」，并据此求 `num_expected_scaleout_topk`（跨 scaleout rank）与 `num_expected_topk`（跨全部 rank）。

然后是 direct 模式（`num_scaleout_ranks == 1`）的流量累加，对应 4.1.2 第二步：

[deep_ep/buffers/elastic.py:L796-L806](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/buffers/elastic.py#L796-L806) —— direct 模式：读 token 一次（`sm_read += 1/num_expected_topk`）；若多节点则写 send buffer；NVLink 流量按「除本 rank 外的 nvlink 对端占比」累加，RDMA 流量按「非 nvlink rank 占比」累加。

hybrid 模式（`num_scaleout_ranks > 1`）的累加更复杂，因为它要分别建模 scaleup warps、scaleout warps、forward warps 三类 warp 的 HBM 读写：

[deep_ep/buffers/elastic.py:L784-L795](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/buffers/elastic.py#L784-L795) —— hybrid 模式：scaleout 流量走 RDMA（含本地旁路 `1 - 1/num_scaleout_ranks` 因子），scaleup 转发走 NVLink，forward warps 既要读又要写。

瓶颈判定与最终公式，正是 4.1.2 第三、四步的代码化：

[deep_ep/buffers/elastic.py:L808-L826](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/buffers/elastic.py#L808-L826) —— 选出 `(bounded_traffic, bounded_gbs)`，套用 `num_sms = max(bounded_gbs/bounded_traffic * sm_read/sm_read_gbs, bounded_gbs/bounded_traffic * sm_write/sm_write_gbs)`，再 1.25×、`align(...,2)`、`prefer_overlap_with_compute` 三态调整、上限设备 SM 数。

最后是 `EP_BUFFER_DEBUG=1` 时打印的那行调试摘要——这正是本讲代码实践要观察的输出：

[deep_ep/buffers/elastic.py:L828-L833](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/buffers/elastic.py#L828-L833) —— 打印 `sm_read / sm_write / rdma_traffic / nvlink_traffic / rdma_gbs / nvlink_gbs / num_expected_scaleout_topk / num_expected_topk / bounded_traffic / bounded_gbs / num_sms`，让你能逐项复核公式。

带宽探测函数本身在 `envs.py`：`get_nvlink_gbs` 解析 `nvidia-smi nvlink -s`、对每条链路速率求和并乘 0.9 效率因子；`get_rdma_gbs` 解析 `ibstat` 的 `Rate`（单位是 Gb/s，所以 `/8` 换成 GB/s）：

[deep_ep/utils/envs.py:L192-L219](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/utils/envs.py#L192-L219) —— `get_nvlink_gbs`：正则抓取第一条 GPU 的所有 `Link N: X GB/s`，求和乘 `factor=0.9`；失败返回 0。

[deep_ep/utils/envs.py:L245-L268](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/utils/envs.py#L245-L268) —— `get_rdma_gbs`：从 `ibstat` 读 NIC 的 `Rate`（Gb/s），`/8` 转 GB/s；这两个函数都带 `@functools.lru_cache`，进程内只探测一次。

#### 4.1.4 代码实践

1. **实践目标**：在单机 8 卡上构造 `ElasticBuffer`，调用 `get_theoretical_num_sms`，借助 `EP_BUFFER_DEBUG=1` 把内部四个流量和最终 `num_sms` 打印出来，并逐项验证 4.1.2 的公式。
2. **操作步骤**：在 `tests/elastic/test_ep.py` 同目录写一个最小脚本（**示例代码**，非项目原有文件）：
   ```python
   # 示例代码：在 test_ep.py 的 test_loop 里，构造 buffer 之后插入
   import os
   os.environ['EP_BUFFER_DEBUG'] = '1'      # 打开 SM 近似调试输出
   num_experts, num_topk = 256, 8
   n = buffer.get_theoretical_num_sms(num_experts, num_topk)
   print('final num_sms =', n)
   ```
   然后像 u1-l4 那样用 `torch.multiprocessing.spawn` 在单机 8 卡上跑一次。
3. **需要观察的现象**：stderr/stdout 会出现一行 `EP SM approximation: sm_read=..., sm_write=..., rdma_traffic=..., nvlink_traffic=..., rdma_gbs=..., nvlink_gbs=..., num_expected_scaleout_topk=..., num_expected_topk=..., bounded_traffic=..., bounded_gbs=..., num_sms=...`。
4. **预期结果**：单机时 `rdma_traffic=0`、`bounded_traffic=nvlink_traffic`、`bounded_gbs=nvlink_gbs`；手动代入 `bounded_gbs/bounded_traffic * max(sm_read/sm_read_gbs, sm_write/sm_write_gbs)`，再乘 1.25、取整到偶数，应与打印的 `num_sms` 一致；且因 `prefer_overlap_with_compute=True`（默认），`num_sms` 通常是个位数（如 4/6/8），远小于 132。
5. **若无法在真实集群运行**：明确「待本地验证」——你仍可在源码里把 L819–L823 的表达式抄出来，用 Python 单独算（手动给 `nvlink_gbs` 一个估值，例如 450），验证公式逻辑。

#### 4.1.5 小练习与答案

**练习 1**：为什么代码在 L766–L768 把 `sm_read/sm_write/rdma_traffic/nvlink_traffic` 初始化为 0，并且注释写「we don't count HBM traffic」？这里的「HBM traffic」指什么？

> **答案**：指 epilogue 的 HBM 读写已被「1 个单位 V」隐含表示——V 本身就是 epilogue 的读总量。代码要算的是 dispatch/combine 主内核**额外**产生的、相对于 V 的流量比例，所以从 0 开始累加；epilogue 自身的读写不需要再单独计入 `sm_read/sm_write`。

**练习 2**：把 `prefer_overlap_with_compute` 从 `True` 改成 `False`，`num_sms` 会怎样变化？为什么？

> **答案**：会变成 `max(num_sms, 64)`（L824）。因为不与计算重叠时，通信内核独占 GPU，没有理由省 SM，于是放开用至少 64 个 SM 把链路喂到最满；反之重叠模式下要给计算流留出 SM，所以宁可少用。

**练习 3**：单节点（`num_rdma_ranks==1`）时，`rdma_traffic` 为什么是 0？这会让 L818 的 `if bounded_traffic > 0` 走哪个分支？

> **答案**：单节点没有跨节点 RDMA 流量，L806 的 `(num_ranks - num_nvlink_ranks)/num_ranks` 为 0，故 `rdma_traffic=0`；瓶颈链路必然是 NVLink（`bounded_traffic=nvlink_traffic>0`），仍正常计算 `num_sms`。只有当 `bounded_traffic==0`（例如 EP=1、没有任何跨 rank 流量）时才退化成 `num_sms=num_device_sms`。

### 4.2 expected top-k：组合数与均衡门控假设

#### 4.2.1 概念说明

4.1 反复用到一个量 `num_expected_topk`——它**不是**用户传进来的 `num_topk`，而是「在均衡门控下，一个 token 的 top-k 选择平均会落到几个不同的 rank 上」。

为什么要算这个？因为 dispatch 的链路流量取决于「一个 token 要发往几个不同 rank」，而不是「选了几个专家」。如果一个 token 的 top-8 专家恰好都在同一个 rank 上，那它只需发 1 份；如果分散在 8 个 rank，就要发 8 份。在「每个 token 的 top-k 在专家集合上均匀随机」的假设下，这个「期望跨 rank 数」可以用组合数精确算出来——这正是 V2 能做到「解析式、无需运行」的关键之一。

#### 4.2.2 核心流程

把 `E = num_experts` 个专家均匀分到 `G` 个组（每组 `E/G` 个，对应一个 rank 上的专家数）。一个 token 从 `E` 个专家里均匀选 `K = num_topk` 个。设 \(X\) 为「被选中的专家覆盖了多少个不同组」，则：

\[
\mathbb{E}[X] \;=\; \sum_{g=1}^{G} \Pr(\text{第 } g \text{ 组至少被选中一次})
\;=\; G \cdot \left(1 - \frac{\binom{E - E/G}{K}}{\binom{E}{K}}\right)
\]

其中 \(\binom{E - E/G}{K}/\binom{E}{K}\) 是「K 个选择全部落在其余 \(E - E/G\) 个专家里、即完全没碰到第 g 组」的概率。代码里 `G` 取两次：一次取 `num_scaleout_ranks` 得 `num_expected_scaleout_topk`（跨节点），一次取 `num_ranks` 得 `num_expected_topk`（跨全部 rank）。

> 这也解释了 `assert num_experts % num_groups == 0`（L771）：均衡分组要求专家数能被组数整除。同时 `assert num_scaleout_topk == 0`（L757）保留给未来的 balanced/group-limited gate 扩展，当前必须为 0。

#### 4.2.3 源码精读

[deep_ep/buffers/elastic.py:L770-L778](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/buffers/elastic.py#L770-L778) —— 内嵌函数 `get_expected_topk(num_groups)`：正是上式 \(G \cdot (1 - \binom{E-E/G}{K}/\binom{E}{K})\) 的直接翻译，用 `math.comb` 算组合数。注意 `num_scaleout_ranks==1` 时直接取 0（单节点不存在跨 scaleout 流量）。

注意该期望值随后被当作归一化单位 V 的「倍数」使用：V = num_tokens × num_expected_topk × data_per_token。`num_expected_topk` 越大（token 越分散），单位流量 V 越大，但因为它在公式里被约掉，影响最终 `num_sms` 的是它出现在各个流量分数里的**比例关系**。

#### 4.2.4 代码实践

1. **实践目标**：脱离分布式环境，单独验证组合数公式的数值，建立直觉。
2. **操作步骤**：在任意 Python 解释器里跑（**示例代码**）：
   ```python
   import math
   def expected_topk(E, K, G):
       return G * (1 - math.comb(E - E // G, K) / math.comb(E, K))
   # 256 专家、top-8、单节点 8 卡（G=8）、双节点 16 卡（G=16）
   print(expected_topk(256, 8, 8))   # 单节点：跨 rank 数
   print(expected_topk(256, 8, 16))  # 跨 scaleout
   ```
3. **需要观察的现象**：`G=8` 时结果接近但小于 8（因为两个 top-k 可能撞到同一 rank）；`G` 越大、结果越接近 `K`。
4. **预期结果**：例如 `expected_topk(256,8,8)` 约为 7.78 左右（待本地验证精确值），说明 top-8 在 8 rank 间几乎不撞车；这与代码注释「NOTES: this is for balanced gate」一致。
5. **若无法运行**：明确「待本地验证」，但公式本身可直接手算。

#### 4.2.5 小练习与答案

**练习 1**：若 `num_experts=8, num_topk=1, G=8`，`get_expected_topk` 等于多少？物理含义是什么？

> **答案**：`8 * (1 - C(7,1)/C(8,1)) = 8 * (1 - 7/8) = 1`。即 top-1 只选 1 个专家，必然只落到 1 个 rank，期望跨 rank 数就是 1。

**练习 2**：为什么 V3.0 的 group-limited gate 不能用本函数？（提示：看 L755–L757 的注释）

> **答案**：group-limited gate 会限制每个 token 在每个 expert group 里最多选的专家数，破坏了「top-k 在专家集合上均匀随机」的假设，于是组合数公式不再成立。代码 `assert num_scaleout_topk == 0` 并注释「please do not use this function」，留待未来支持。

### 4.3 QP 分配：direct 与 hybrid 模式

#### 4.3.1 概念说明

QP（Queue Pair）是 RDMA 网卡上的发送/接收队列对。直觉上，多开 QP 能让更多 RDMA 操作并行下发，提升吞吐；但 QP 越多，CPU/网卡处理「门铃（doorbell，即通知网卡有新工作要做的机制）」的开销越大（代码注释称之为「DB ringing overhead」）。因此 QP 数量要在「并行度」和「门铃开销」之间权衡。

DeepEP 的策略很清晰：

- **direct 模式**：少 QP。每 SM 最多用 1 个 QP，且总量不超过 8，再加 1 个给 notify warps，即 `min(num_sms, 8) + 1`。
- **hybrid 模式**：多 QP。每 SM 拥有 16 个 channel，每个 channel 一个独立 QP，再加 1 个给 notify warps，即 `num_sms * 16 + 1`。

无论算出多少，最终都被构造期分配的 `num_allocated_qps` 上限封顶（构造时一次性向网卡申请好，运行期不再扩容）。

#### 4.3.2 核心流程

```
get_theoretical_num_qps(num_sms):
    if direct 模式 (allow_hybrid_mode == False):
        num_qps = min(num_sms, 8) + 1        # 少 QP，省门铃开销
    if hybrid 模式 (allow_hybrid_mode == True):
        num_qps = num_sms * 16 + 1           # 每 channel 一个 QP
    return min(num_qps, num_allocated_qps)   # 不超过构造期上限
```

构造期的 `num_allocated_qps` 自动分配规则（`num_allocated_qps=0` 时）：

| 模式 | 条件 | 分配上限 |
| --- | --- | --- |
| direct | — | 17 |
| hybrid | 支持 fast RDMA atomic（NIC ≥ MT4131） | 65 |
| hybrid | 不支持 fast RDMA atomic | 129 |

> 这三个数字不是随便选的。hybrid 下 `num_sms * 16 + 1`：重叠模式下 `num_sms` 常为 4（→65）或 8（→129），正好对上表里的两个上限。也就是说，构造期分配的 QP 数恰好够覆盖「最可能用到的 SM 数」，不会浪费。

#### 4.3.3 源码精读

[deep_ep/buffers/elastic.py:L836-L853](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/buffers/elastic.py#L836-L853) —— `get_theoretical_num_qps`：direct 先取 `min(num_sms, 8+1)`，hybrid 改写为 `num_sms*16+1`，最后 `min(num_qps, self.num_allocated_qps)` 封顶。注释解释 direct 鼓励少 QP「to reduce DB ringing overhead」，hybrid 鼓励「every channel (and notify) to have an independent QP」。

构造期的 QP 上限分配：

[deep_ep/buffers/elastic.py:L326-L335](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/buffers/elastic.py#L326-L335) —— `num_allocated_qps == 0` 时：hybrid + fast atomic → 65，hybrid + 否则 → 129，direct → 17；注释「The extra QP is for notify warps」解释了 `+1` 的来源。

fast RDMA atomic 的探测：

[deep_ep/utils/envs.py:L222-L242](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/utils/envs.py#L222-L242) —— `check_fast_rdma_atomic_support`：解析 `ibstat`，若 NIC 的 `CA type` 是 `MT4131`（BlueField-3 / ConnectX-7 一代）则返回 True。支持 fast atomic 时 QP 需求量减半（129→65）。

最后，看 `dispatch`/`combine` 是怎么把这两个函数串起来的：

[deep_ep/buffers/elastic.py:L926-L930](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/buffers/elastic.py#L926-L930) —— `dispatch`：`num_sms==0` 时自动调 `get_theoretical_num_sms`，再 `num_qps==0` 时调 `get_theoretical_num_qps(num_sms)`，并断言 `num_qps <= num_allocated_qps`。

[deep_ep/buffers/elastic.py:L1085-L1088](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/buffers/elastic.py#L1085-L1088) —— `combine`：注意它**复用** dispatch handle 里存的 `num_sms`（`handle.num_sms`，L1086），只单独算 QP，因为 combine 的 SM 数应与 dispatch 一致（互为逆过程）。

#### 4.3.4 代码实践

1. **实践目标**：观察 direct 与 hybrid 模式下 QP 数量的差异，并验证「构造期上限」封顶逻辑。
2. **操作步骤**：在构造好 `buffer` 之后（**示例代码**）：
   ```python
   # 示例代码
   for n in [4, 8, 16, 32]:
       print(n, buffer.get_theoretical_num_qps(n))
   print('allocated cap =', buffer.num_allocated_qps)
   ```
   分别在 `allow_hybrid_mode=True` 与 `False` 构造的 buffer 上各跑一次（单机两种模式逻辑域相同，但 QP 公式分支不同，仍可观察公式）。
3. **需要观察的现象**：direct 模式下结果随 `n` 增长到 9 后封顶（`min(n,9)`）；hybrid 模式下结果为 `n*16+1`，但被 `num_allocated_qps`（65 或 129）封顶。
4. **预期结果**：例如 hybrid、fast-atomic、`num_allocated_qps=65` 时：`n=4 → 65`（4*16+1=65，正好用满），`n=8 → 65`（8*16+1=129 > 65，被封顶）。direct 时 `n=4 → 5`、`n=8 → 9`、`n=16 → 9`。具体数值「待本地验证」。
5. **若无法运行多节点**：单机也可验证 direct 分支与封顶逻辑；hybrid 的 65/129 差异需要能跑 `ibstat` 的真实 RDMA 环境判定 fast-atomic，否则 `check_fast_rdma_atomic_support` 返回 False，会走 129 分支。

#### 4.3.5 小练习与答案

**练习 1**：hybrid 模式下，为什么公式是 `num_sms * 16 + 1`，那个 `16` 是什么？

> **答案**：`16` 是每个 SM 拥有的 channel 数（即 `num_channels_per_sm`）。hybrid dispatch 把每个 SM 的工作切成多个 channel，每个 channel 独立持有一个 QP 以最大化 RDMA 并行度；`+1` 是给 notify warps 单独留的 QP（见 L330 注释）。

**练习 2**：direct 模式为什么「鼓励少 QP」？多用几个 QP 不是更并行吗？

> **答案**：direct 模式 token 直接经 NVLink 发到对端，RDMA 用得少；QP 过多反而让网卡频繁处理 doorbell（DB ringing），增加开销却换不来吞吐。所以代码用 `min(num_sms, 8) + 1` 主动限制 QP 数。

**练习 3**：`assert num_qps <= self.num_allocated_qps`（L930）会在什么情况下失败？

> **答案**：当用户手动传了较大的 `num_sms`（让 `get_theoretical_num_qps` 算出超过构造期 `num_allocated_qps` 的值，且未被 `min` 封顶——实际上 `get_theoretical_num_qps` 内部已经 `min` 过，所以这条断言主要兜底「用户直接传了过大的 `num_qps`」或「构造时手动把 `num_allocated_qps` 设得很小」的情形）。

## 5. 综合实践

把 4.1～4.3 串起来：写一个「SM/QP 规划表」生成器，给定一组 `(num_experts, num_topk)`，自动打印每个配置在 direct 与 hybrid 模式下推荐的 SM 数与 QP 数，并标注瓶颈链路。

```python
# 示例代码：SM/QP 规划表（放在 test_loop 里、构造好 buffer 之后）
import os
os.environ['EP_BUFFER_DEBUG'] = '1'

print(f'{"E":>4} {"K":>3} {"mode":>8} {"sms":>4} {"qps":>4} {"cap":>4}')
for num_experts in [64, 128, 256]:
    for num_topk in [4, 8]:
        sms = buffer.get_theoretical_num_sms(num_experts, num_topk)
        qps = buffer.get_theoretical_num_qps(sms)
        mode = 'hybrid' if buffer.allow_hybrid_mode else 'direct'
        print(f'{num_experts:>4} {num_topk:>3} {mode:>8} {sms:>4} {qps:>4} {buffer.num_allocated_qps:>4}')
```

完成后请回答：

1. 哪种配置算出的 `sms` 最大？瓶颈链路（`bounded_*`）是 RDMA 还是 NVLink？
2. 把 `prefer_overlap_with_compute` 设为 `False` 重跑，`sms` 列怎么变？为什么？
3. hybrid 模式下 `qps` 是否经常等于 `cap`？这说明什么？

> 本实践在单机 8 卡即可完成 direct 分支与公式的验证；hybrid 的真实链路流量需多节点环境，单机 `rdma_traffic=0`。涉及真实带宽数字的部分若无法在集群运行，请标注「待本地验证」，但公式逻辑可在源码层面完整复现。

## 6. 本讲小结

- V2 用「带宽建模 + 解析式公式」取代 V1 的 auto-tuning，`get_theoretical_num_sms` 一次函数调用就能定下 SM 数，无需任何预热运行。
- 核心是**带宽均衡**：令「HBM 搬运时间 = 瓶颈链路传输时间」，解出 `num_sms = (bounded_gbs/bounded_traffic) · max(sm_read/sm_read_gbs, sm_write/sm_write_gbs)`，真实 token 数与 hidden 在公式里被约掉。
- 四个归一化流量 `sm_read/sm_write/rdma_traffic/nvlink_traffic` 都以「epilogue 读总量 V」为单位，direct 与 hybrid 模式的累加项不同。
- `num_expected_topk` 用组合数 \(G(1 - \binom{E-E/G}{K}/\binom{E}{K})\) 算「均衡门控下 token 的期望跨 rank 数」，是整个建模的关键输入；它只在 balanced gate 下成立。
- `get_theoretical_num_qps`：direct 用 `min(num_sms,8)+1` 省 QP、hybrid 用 `num_sms*16+1` 给每 channel 独立 QP，最后都被构造期 `num_allocated_qps`（direct 17 / hybrid 65 或 129）封顶。
- `prefer_overlap_with_compute` 是「省 SM 与满性能」的总开关：开则保个位数 SM 给计算流让路，关则强制至少 64 个 SM 跑满链路。

## 7. 下一步学习建议

- 这套 SM/QP 计算的**结果**会作为编译期常量被烘焙进 JIT 生成的 kernel 模板参数。建议接着学 **u4-l2（内核代码生成：模板实例化的 .cu 注入技巧）**，看 `num_sms` 是怎么从 Python 一路传到 `dispatch_impl<...>` 的模板参数里的。
- 想看 SM 数如何决定内核内部的 warp 划分与共享内存占用，请学 **u5-l1（直接模式 Dispatch：notify 与 dispatch warps 的协作）**。
- 想理解 hybrid 模式里「channel」到底是什么、为什么每 channel 要独立 QP，请学 **u5-l2（Hybrid Dispatch：scaleout + scaleup 两级通信）**。
- 想了解 fast RDMA atomic（MT4131）如何让 QP 需求减半，可顺带阅读 **u3-l1** 中关于 hybrid 模式与 `railedGinType` 的部分。
