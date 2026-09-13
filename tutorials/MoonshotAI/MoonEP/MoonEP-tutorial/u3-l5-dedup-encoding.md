# 去重编码：负数 dst 与重复组结构

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清楚 **`-raw_dst - 1` 负数编码**的判别与解码规则：为什么 `-1` 偏移能保证编码值恒为负、为什么它是 `int32` 非负整数到负整数的双射。
2. 理解 **为什么需要去重**：同一个 token 的多个 top-k 条目落到同一目的 rank 时，`H` 个 bf16 的 payload 只需在 NVLink 上传一次，权重（4 字节）仍逐条传输——并能量化这笔带宽节省。
3. 掌握 **`dup_groups` / `dup_loffs` / `dup_counts` 三件套的数据契约**：每行的三个字段各是什么、为什么只有紧凑前缀有效、为什么顺序不保证稳定（atomicAdd 到达顺序决定）。
4. 用纯 Python 写出与参考实现 `tests/planning_reference.py` 语义等价的 `simulate_dedup(topk, dst)`。

本讲是规划器（单元 3）的最后一环：u3-l4 已经算出了每个 token 条目的目的槽位 `dst` 与溯源数组 `src_info`，本讲处理"收尾时的两个发现"——发现重复、为重复建立可复用的数据结构。

## 2. 前置知识

### 2.1 回顾：dst 与 src_info 的 rank-stride 编码

u3-l4 讲过，规划器 passB 为源 rank 上第 `s` 个 token 的第 `k` 个 top-k 条目写出：

\[ \text{dst} = d \cdot \text{NvS} + \text{loff}, \qquad d \in [0, R),\ \text{loff} \in [0, \text{NvS}) \]

即**高段是目的 rank、低段是该 rank 接收缓冲内的行偏移**。同时向目的 rank 的 `SRC_INFO` 区发布镜像编码 `src_rank * NvS + offv`（`offv = s*K + k` 是源 rank 上的 flat top-k 偏移），空槽用 `-1` 哨兵。

### 2.2 位运算三件

本讲源码大量使用三个位技巧，先建立直觉：

| 技巧 | 形式 | 用途 |
|------|------|------|
| 按位取反 | `-v - 1`（即 C 的 `~v`） | 把非负整数一一对应搬进负整数区间 |
| 位图（bitset） | `seen |= 1 << d`，判重 `seen & (1 << d)` | 用一个整数的第 `d` 位记录"rank d 见过没有" |
| popcount / ctz | `popc(mask)`、`ctz(mask)` | 数位图里 1 的个数 / 找最低位的 1 |

### 2.3 "重复"是怎么产生的

dispatch 的传输单位是**行**：把 token 的 hidden 状态（`[H]` 个 bf16）拷到目的 rank 的某一行。注意，一个 token 的 `K` 个 top-k 条目**内容是同一行 hidden**——如果这 `K` 个条目中有 2 个以上的目的 rank 相同（比如两个 top-k 专家恰好驻留在同一 rank，MoonEP 的预取机制把热门远程专家聚合到本地预取槽后，这种情况非常常见），那么传 2 行一模一样的 payload 是纯浪费。

关键观察：**去重组的判别维度是目的 rank，不是专家**。两个 top-k 条目哪怕选中的专家不同，只要它们的目的 rank 相同，payload 就完全相同（都是该 token 的 hidden），就可以只传一次。专家分组信息由 `cu_seqlens` 的段结构承载，与行内容无关。

## 3. 本讲源码地图

| 文件 | 本讲涉及的部分 | 作用 |
|------|----------------|------|
| `moonep/planning.py` | `MoonEPCommPlan` 去重字段、规范化循环、`allocate_planning_outputs`、`_check_dedup_encoding_bounds` | 产出规范化 `dst`，声明并分配去重三件套 |
| `moonep/constants.py` | `RANK_BITS` / `KIDX_BITS` / `DEDUP_BUILDER_WARPS` | 打包编码位宽的单一事实来源 |
| `moonep/dispatch.py` | consumer warp 的负值解码、builder warps 的三遍扫描、`launch_dispatch` 的 `build_dedup_map` 开关 | 消费负数 dst；从 `src_info` 物化去重三件套 |
| `moonep/api.py` | builder scratch 分配 | `primary_packed` / `kmask` / `kidx_to_loff` 的宿主侧分配 |
| `moonep/dispatch_epilogue.py`、`moonep/combine_prologue.py` | 模块/类文档字符串 | 去重三件套的两个下游消费者 |
| `tests/planning_reference.py` | Part 3 参考实现 | 确定性顺序的 PyTorch 参照，本讲实践的对照物 |

## 4. 核心概念与源码讲解

### 4.1 dst 规范化：`-raw_dst - 1` 负数编码

#### 4.1.1 概念说明

passB 结束时，`dst` 里还是"原始值"（raw dst）：每个条目都是非负的 `d * NvS + loff`。规范化的任务是：**在每个 token 的 K 个条目内部，对同一目的 rank 只保留第一次出现的条目为非负，其余改写为**

\[ \text{enc}(v) = -v - 1 \]

被改写的条目语义变化是：

- **不再拷贝 payload**（`H` 个 bf16 的 hidden 行不传了）；
- **仍然散射路由权重**（每个 top-k 条目有自己的 fp32 权重，combine 时要按条目加权求和，所以原始 `v` 必须可恢复）。

为什么选 `-v - 1` 而不是简单的 `-v`？两个原因：

1. **恒负性**。对任意 \( v \ge 0 \)，有 \( -v - 1 \le -1 < 0 \)。如果只用 `-v`，那么 \( v = 0 \)（目的 rank 0、行偏移 0）编码后还是 `0`，判别 `dst >= 0` 就失效了。
2. **双射性**。\( \text{enc} \) 是 \( \mathbb{Z}_{\ge 0} \to \mathbb{Z}_{-} \) 的一一映射，解码 \( \text{dec}(w) = -w - 1 \) 无歧义、也不需要额外标志位——"符号位"本身就是标志位。

这与 C/C++ 的按位取反 `~v` 完全等价（补码表示下 `~v == -v-1`），也和 `src_info` 用 `-1` 作空槽哨兵的语义自洽：`-1` 解码回 `v = 0`。

#### 4.1.2 核心流程

规范化发生在 planning 内核的**最后一步**，视角是**源 rank**（每个 rank 处理自己 S 个 token 的 dst）：

```
前置：passB 已写完 raw dst（dst[s*K + k] = d*NvS + loff）
      并经 cross_rank_barrier 发布了所有 rank 的 src_info

for s in [0, S):                       # 每个 CTA 分一段 token
    读入 v_k = dst[s*K + k],  d_k = v_k // NvS   (k = 0..K-1)
    seen_lo = seen_hi = 0              # 两个 i64 位图，覆盖 128 个 rank
    for k in [0, K):                   # 按 k 升序 ⇒ "第一次出现"= 最小 k
        if d_k < 64: dup = 第 d_k 位已在 seen_lo? ; 置位 seen_lo
        else:       dup = 第 (d_k-64) 位已在 seen_hi? ; 置位 seen_hi
        if dup: dst[s*K + k] = -v_k - 1
```

配套的**边界检查**（宿主侧，launch 前执行）保证编码不越界：

- `R <= 128`：位图只有两个 i64，128 位；
- `K <= 32`：下游 `kmask` 是单字 b32 位图；
- `K <= 127`（`KIDX_BITS = 7`）且 `NvS <= 2^24 - 1`：下游 `primary_packed = (kidx << 24) | loff` 的两段字段各不溢出（最高位留作符号位）；
- `S*K <= NvS` 与 `R*NvS <= int32_max`：`src_info` 的 rank-stride 编码不回绕。

#### 4.1.3 源码精读

**第一步：passB 产出 raw dst 并发布 src_info**（承接 u3-l4，这是规范化的输入）：

[moonep/planning.py:1060-1067](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/planning.py#L1060-L1067)——二分查找定位目的 rank `lo` 后，写出非负的原始编码 `dst = lo * NvS + bo + (global_rank - pc)`。

[moonep/planning.py:1071-1073](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/planning.py#L1071-L1073)——同时把 `src_rank * NvS + offv` 写进目的 rank 的 `SRC_INFO` 槽位，这是后续 dispatch builder 物化去重结构的原料。

**第二步：跨 rank 屏障之后再规范化**：

[moonep/planning.py:1074-1077](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/planning.py#L1074-L1077)——注释说明了屏障的必要性：所有 rank 的 `src_info` 写入必须先对全体可见，任何 rank 的 fresh dispatch builder 才能安全读取本地 `src_info` 切片；屏障之后才进入规范化循环。

**第三步：规范化循环本体**。先看契约注释：

[moonep/planning.py:1079-1082](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/planning.py#L1079-L1082)——官方一句话契约："每个目的 rank 的第一个 top-k 条目保持非负并拷贝 payload；后续条目编码为 `-raw_dst - 1`，只携带权重"。

[moonep/planning.py:1085-1094](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/planning.py#L1085-L1094)——把 token `s` 的 K 个 dst 值和目的 rank 读进寄存器数组（`dst_vals` / `dests`），注意循环按 `k` 升序展开，"第一次出现"因此就是"最小的 k"。

[moonep/planning.py:1095-1113](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/planning.py#L1095-L1113)——位图判重与写入：`seen_lo`（rank 0–63）和 `seen_hi`（rank 64–127）两个 i64；先测试位是否已置（`dup`），再置位；重复条目写 `-(dst_vals[k]) - 1`。这就是 4.1.2 伪代码的逐行落地。

**第四步：dispatch 端的判别与解码**。规范化后的 `dst` 被 dispatch 的 consumer warp 消费：

[moonep/dispatch.py:406-415](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/dispatch.py#L406-L415)——判别 `store_token = dst_val >= Int32(0)`；负值时解码 `raw_dst = -dst_val - Int32(1)`，再照常拆出 `drank = raw_dst // NvS`、`loff = raw_dst % NvS`。注释点明关键：**负值保留了原始目的地址用于权重散射，只是把 payload 标记为重复**。

[moonep/dispatch.py:417-433](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/dispatch.py#L417-L433)——条件执行：`if store_token` 才发起 `cp.async.bulk` S2G 的整行 payload 拷贝（L417-427）；而权重散射（L429-433）在 `with_weights` 时**无条件执行**——把 `w_tensor[sK + k]`（fp32 视作 int32 的 4 字节 gather）写到目的 rank 的 `meta_buf` 权重槽 `drank * meta_stride + weights_off + loff`。每个 top-k 条目都有自己的权重槽，这就是"raw dst 必须可恢复"的原因。

**第五步：宿主侧边界检查**：

[moonep/planning.py:1267-1291](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/planning.py#L1267-L1291)——`_check_dedup_encoding_bounds` 的六条断言，把 4.1.2 列出的所有限制（`S*K <= NvS`、`R*NvS <= int32_max`、`R <= 128`、`K <= 127`、`NvS <= 2^24 - 1`、`K <= 32`）在 `launch_planning` 入口全部挡下，见 [moonep/planning.py:1309-1310](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/planning.py#L1309-L1310) 的调用点。

位宽常量的"单一事实来源"在：

[moonep/constants.py:2-10](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/constants.py#L2-L10)——文件头注释明确写着这些常量是 planning / dispatch 内核与测试三方共用的；`RANK_BITS = 7`（\(2^7 = 128\) 个 rank 足够主流 EP 配置）、`KIDX_BITS = 7`（kidx 字段为未来非位图编码留余量）。

#### 4.1.4 代码实践

**实践目标**：用纯 Python 验证 `-raw_dst - 1` 编码的三个性质（恒负、双射、解码恢复），并复现位图判重逻辑。

**操作步骤**（以下为示例代码，可保存为 `dedup_codec.py` 用 `python dedup_codec.py` 运行，仅需 Python 标准库）：

```python
# 示例代码：编码性质的单元验证 + 位图判重模拟
def enc(v): return -v - 1          # planning.py L1113 的编码
def dec(w): return -w - 1          # dispatch.py L413 的解码

# 1) 恒负性：v >= 0 时 enc(v) <= -1（若用 -v，v=0 时会漏判）
assert all(enc(v) < 0 for v in range(1 << 20))
# 2) 双射性：dec(enc(v)) == v，且编码值互不相同
vals = list(range(1 << 20))
assert all(dec(enc(v)) == v for v in vals)
assert len({enc(v) for v in vals}) == len(vals)
# 3) 位图判重（模拟 planning.py L1095-1111，K=8、rank 上限 128）
def canonicalize(dst_row):          # dst_row: 一个 token 的 K 个 raw dst
    out, seen = [], 0               # 单个 i64 足够 R <= 64；源码用两个覆盖 128
    for v in dst_row:
        d = v // NVS
        dup = (seen >> d) & 1
        seen |= 1 << d
        out.append(enc(v) if dup else v)
    return out
NVS = 4096
row = [3*NVS+100, 3*NVS+200, 1*NVS+50, 3*NVS+300]  # rank3 出现 3 次
print(canonicalize(row))
```

**需要观察的现象**：

1. 前两条 assert 全部通过（无异常）。
2. `canonicalize` 的输出形如 `[3*NVS+100, -(3*NVS+200)-1, 1*NVS+50, -(3*NVS+300)-1]`——只有第一次出现的 rank 3 条目保持非负。

**预期结果**：三条性质均成立；对照 [moonep/planning.py:1112-1113](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/planning.py#L1112-L1113)，你的 `canonicalize` 与内核行为逐条目一致。

#### 4.1.5 小练习与答案

**练习 1**：`raw_dst = 0`（目的 rank 0、行偏移 0）编码后的值是多少？如果编码规则改成 `-v` 会出什么问题？

**答案**：`enc(0) = -1`。改成 `-v` 则 `enc(0) = 0`，dispatch consumer 的判别 `dst_val >= 0`（dispatch.py L410）会把这个重复条目误判为"要拷 payload 的主条目"，去重失效。`-1` 偏移保证编码值恒 `<= -1`。

**练习 2**：dispatch consumer 读到 `dst_val = -12345`，`NvS = 4096`。解出的目的 rank 和行偏移是多少？

**答案**：`raw_dst = -(-12345) - 1 = 12344`；`drank = 12344 // 4096 = 3`；`loff = 12344 % 4096 = 56`。该条目不拷 payload，但权重仍写到 rank 3 的 meta 权重槽 56。

**练习 3**：为什么规范化循环放在 planning 内核末尾，而不是挪到 dispatch 内核里做？

**答案**：因为规范化的 `dst` 是**canonical（规范形）**，会被存进 `MoonEPCommPlan` 跨前向/反向复用（combine bwd、dispatch bwd 都直接用保存的 plan）。在 planning 里一次算定，dispatch/epilogue/prologue/combine 及所有反向路径就都能直接读，无需每条路径重算；且规范化必须在所有 rank 的 `src_info` 发布之后（planning.py L1074-1077 的屏障语义），放在 planning 末尾天然满足这一时序。

### 4.2 去重结构契约：dup_groups / dup_loffs / dup_counts

#### 4.2.1 概念说明

负数 dst 只解决了"发送端少传"，还有一个对称问题：**接收端的行没有着落**。目的 rank 的接收缓冲是按 `cu_seqlens` 分段组织的静态形状——每个 top-k 条目在分段里都有自己的行（loff），专家 GEMM 会读这些行。发送端只传了一行，其余重复行的内容谁来补？

答案是两个方向各配一个"本地修补"内核：

- **dispatch epilogue**（前向入口侧）：dispatch 结束后，在本 rank shard 上把主行（真正传过来的那行）**扇出**到同组的所有重复槽位；
- **combine prologue**（前向出口侧）：combine 之前，把每个重复组的多行输出以 fp32 **累加回**主行，让 combine 只需传输主行（combine 是 dispatch 的对偶：多发一行的权重贡献不如先本地求和再传一行）。

两个内核都需要一份"哪些槽位属于同一个重复组"的清单，这就是去重三件套：

| 张量 | 形状 | 语义 |
|------|------|------|
| `dup_groups` | `[NvS, 3]` | 每行一个重复组的头三元组 `(primary_loff, dup_start, dup_n)`：主槽行号、它在 `dup_loffs` 里的起始下标、重复槽数 |
| `dup_loffs` | `[NvS]` | 扁平的重复槽行号表，按组连续存放 |
| `dup_counts` | `[2]` | `[n_groups, n_dup_loffs]`：`dup_groups` 与 `dup_loffs` 的有效前缀长度 |

三条契约要点（来自 `MoonEPCommPlan` 的字段注释）：

1. **只有紧凑前缀有效**：容量按最坏情况 `NvS` 分配，实际组数通常远小，`dup_counts` 给出真实长度；
2. **顺序不保证稳定**：组在 `dup_groups` 里的先后由 builder 的 atomicAdd 到达顺序决定，逐次运行可能不同——测试必须比较**组的集合**而非序列；
3. **视角是目的 rank**：`primary_loff` / 重复槽都是**本 rank 接收 shard 内的行号**（`loff`），所以 epilogue/prologue 都是纯本地内核，不做跨 rank 通信。

还有一个精妙的**两端一致性**：planning 端保留"最小 k 的条目"为非负主条目（4.1 的按 `k` 升序扫描）；builder 端用 `atom_min` 在 `(kidx << 24) | loff` 上选举主槽——kidx 在高位，最小 packed 就是**最小 kidx**。两端独立实现，选出的主槽却是同一个，这保证了 dispatch 实际传输的那行恰好就是 `dup_groups` 里登记的 `primary_loff` 行。

#### 4.2.2 核心流程

去重结构的物化不在 planning 内核里，而在 **fresh dispatch 的 builder warps** 中（planning 只负责产出 canonical dst 和 src_info）。视角换成**目的 rank**，扫自己的 `NvS` 个 `src_info` 槽位：

```
初始化（每次 fresh dispatch 都做，不信任上次残留）:
    primary_packed[key] = INT32_MAX   # key = src_rank*S + token，共 R*S 个
    kmask[key] = 0                     # 该 token 组出现过的 kidx 位图
    dup_counts = [0, 0]

Pass 1 — 选举主槽（扫 loff ∈ [0, NvS)）:
    info = src_info[loff];  info < 0 跳过（空槽哨兵）
    (src_rank, offv) = 解码 info;  token = offv // K;  kidx = offv % K
    key = src_rank*S + token
    atom_min(primary_packed[key], (kidx << 24) | loff)   # 最小 kidx 当主槽
    atom_or(kmask[key], 1 << kidx)                        # 记录组内出现的 k
    kidx_to_loff[key*K + kidx] = loff                     # k → 槽位映射

Pass 2a — 统计（重扫同一区间）:
    对每个主槽 loff == primary_loff:  dup_n = popc(kmask) - 1
    仅 dup_n > 0 计入：lane 级累计组数 lane_grp_n 与重复数 lane_dup_n

预留 — warp 聚合 atomicAdd（每 warp 仅 2 次，不是每记录 1 次）:
    warp 内前缀和得到各 lane 的输出区间
    lane 0: grp_base = atomicAdd(dup_counts[0], 组数)
            dup_base = atomicAdd(dup_counts[1], 重复数)

Pass 2b — 发射（再扫一遍，写入预留区间）:
    dup_groups[g]   = (primary_loff, my_dup, dup_n)
    dup_loffs[my_dup + j] = 组内其余 kidx 升序的 loff   (经 kidx_to_loff 查表)
```

三遍扫描之间用 CTA 内 named barrier + 跨 CTA 的 `cross_warp_sync` 分层同步隔开。`DEDUP_BUILDER_WARPS = 4`：builder 的扫描是延迟瓶颈（每 SM 一条依赖加载链），把每个 CTA 的区间拆给 4 个 warp 并行扫描，正好把构建时间藏在 dispatch 的 NVLink 传输之下。

#### 4.2.3 源码精读

**契约的权威文本**在数据类定义处：

[moonep/planning.py:44-50](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/planning.py#L44-50)——`MoonEPCommPlan` 的字段注释：三件套"由 dispatch builder 写入、被 dispatch epilogue / combine prologue 消费；`dup_counts = [n_groups, n_dup_loffs]`；只有紧凑前缀有效；顺序由 builder 的 atomicAdd 决定、不保证稳定"。模块头文档 [moonep/planning.py:6-7](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/planning.py#L6-L7) 补充了另一半契约：fresh dispatch 从 `dst` 和 `src_info` 物化它们，**plan 复用路径直接复用**。

三个张量与 scratch 的宿主侧分配：

[moonep/planning.py:1229-1233](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/planning.py#L1229-L1233)——`allocate_planning_outputs` 用 `_round4` 过量分配后切片，得到 `(NvS, 3)` / `(NvS,)` / `(2,)` 三个 int32 张量；注释（L1205-1207）说明它们在此分配只为让返回的 plan 完整，**内容留给 dispatch builder 填充**。

[moonep/api.py:385-392](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L385-L392)——`_create_context` 分配 builder scratch：`primary_packed[R*S]`、`kmask[R*S]`、`kidx_to_loff[R*S*K]`（注释：后者把每个 (源 rank, token, topk index) 键映射到展开重复组时要用的 NvS 槽位）。

内核侧的编码常量：

[moonep/dispatch.py:306-308](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/dispatch.py#L306-L308)——`NvS_BITS = 32 - 1 - KIDX_BITS = 24`、`NvS_MASK = (1 << 24) - 1`、`INT32_MAX`：`primary_packed` 的高 7 位放 kidx、低 24 位放 loff、最高位留作符号位，正是 4.1 边界检查的对偶。

**Pass 0：scratch 初始化**：

[moonep/dispatch.py:527-542](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/dispatch.py#L527-L542)——每个 fresh dispatch 都把 `primary_packed` 清成 `INT32_MAX`、`kmask` 清零、leader 把 `dup_counts` 清零。注释解释了为什么不做"尾部清理"优化：调用方通常复用同一个 Buffer 跑很多轮，无法保证上一轮 dispatch 已彻底完成，残留状态不可信。

**Pass 1：选举主槽**：

[moonep/dispatch.py:552-576](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/dispatch.py#L552-L576)——从本地 `src_info` 切片解码 `(src_rank, offv)`，拆出 `token = offv // K`、`kidx = offv - token * K`（L558-564）；组装 `packed = (kidx << NvS_BITS) | loff` 后用 `atom_min_relaxed_gpu_s32` 选举（L565-569）——最小 kidx 胜出，与 planning 端"保留第一次出现"精确对齐；同时 `atom_or` 更新 kmask、写 `kidx_to_loff`（L570-576）。

**Pass 2a + warp 聚合预留**：

[moonep/dispatch.py:593-610](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/dispatch.py#L593-L610)——重扫区间，`dup_count = popc_b32(mask) - 1`，只对本槽是主槽（`loff == primary_loff`）且 `dup_count > 0` 的条目累计组数/重复数。设计说明（L586-592）：这一遍刻意不含 warp 集合操作，让编译器能把 `info -> election` 的依赖加载流水起来。

[moonep/dispatch.py:618-641](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/dispatch.py#L618-L641)——warp 内 inclusive scan 得到各 lane 的输出偏移，lane 0 用**每 warp 仅 2 次**的 atomicAdd（分别对 `dup_counts[0]`、`dup_counts[1]`）预留整个 warp 的紧凑区间。注释给出了取舍：逐记录 atomicAdd 的方案会串行化并主导 dispatch 延迟（旧时代的实测教训）。

**Pass 2b：发射**：

[moonep/dispatch.py:643-681](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/dispatch.py#L643-L681)——再扫一遍，把 `dup_groups[g] = (loff, my_dup, dup_count)` 写入预留位置（L665-668），再用 `ctz_b32` 按位逐个取出组内其余 kidx、经 `kidx_to_loff` 查表填 `dup_loffs`（L669-679）。L644-647 的注释就是"顺序不稳定"契约的出处：紧凑前缀的跨 warp 顺序取决于 atomicAdd 到达顺序，消费者必须按下标迭代、测试必须比较集合。

**开关：只有 fresh planning 才构建**：

[moonep/dispatch.py:856-864](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/dispatch.py#L856-L864)——`launch_dispatch` 的文档：`build_dedup_map` 仅在紧随 fresh planning 之后为真；复用与反向路径传 `False`，以免用**过期的 `src_info`**（下轮 planning 会覆盖它）重建出错误的 dedup 结构。

[moonep/dispatch.py:943-945](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/dispatch.py#L943-L945)——`build_dedup_map=False` 时干脆把 `plan.dst` 当占位指针传给三个 scratch 参数（内核不会解引用），省去绑定真实 scratch 的开销。

**下游消费者**（本讲只看契约，实现在单元 4/5 精读）：

[moonep/dispatch_epilogue.py:53-56](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/dispatch_epilogue.py#L53-L56)——epilogue 的驱动数据正是三件套：`dup_groups` 列出 `(primary_loff, dup_start, dup_n)` 头、`dup_loffs` 是扁平重复槽、**`dup_counts[0]` 在设备端读取**（避免宿主同步）。类文档（L58-60）还规定了组批次 round-robin 到 CTA 的映射。

[moonep/combine_prologue.py:44-46](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/combine_prologue.py#L44-L46)——prologue 消费同一份结构做镜像的归约方向。

**确定性参考实现**（实践的对照物）：

[tests/planning_reference.py:239-295](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/planning_reference.py#L239-L295)——参考实现的 Part 3 与内核逐语义对齐：按 (src_rank, token) 遍历、按目的 rank 分组、`group_loffs[0]` 当主槽、其余编码 `-dst - 1`（L292-295）；组按遍历顺序写入（确定性），注释（L242-244）说明这是为了和 builder 的**语义**比较——顺序不同没关系，`kernel_test_utils.dedup_plan_semantic_errors` 比较的是集合。L246-251 的注释还回答了"为什么权重仍要逐条传"：每个 token-topk 条目有自己的权重，dispatch/combine 的权重散射/收集需要 raw dst。

#### 4.2.4 代码实践

**实践目标**：读懂参考实现 Part 3 的分组逻辑，并写出它的纯 Python 等价物，作为综合实践（第 5 节）`simulate_dedup` 的铺垫。

**操作步骤**：

1. 打开 [tests/planning_reference.py:263-295](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/planning_reference.py#L263-L295)，找出三件事的代码位置：分组用的字典（`groups` / `indices`）、主槽的定义（`group_loffs[0]`）、负数编码的写入（`dst[idx] = -dst[idx] - 1`）。
2. 回答：为什么参考实现先 all_gather 所有 rank 的 dst（L256-261），却只在 `src_rank == rank` 时改写 `dst`（L292-295），而 `dup_groups` 却按 `dest` 维度写（L283-285）？
3. 对照 [tests/kernel_test_utils.py:239-257](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/kernel_test_utils.py#L239-L257)（`dedup_plan_semantic_errors` 开头），确认它先比 dtype/shape，再以 `dup_counts` 为前缀长度做后续比较——**它从不比较元素的先后顺序**。

**需要观察的现象**：参考实现里"谁持有 dup 结构"与"谁的 dst 被改写"是两个不同视角——前者按目的 rank（`dup_groups_by_rank[dest]`，本 rank 测试只取 `[rank]`，L301），后者按源 rank（只改写自己的 `dst`）。

**预期结果**：能说出"规范化是发送端视角（省传输），dup 结构是接收端视角（补行/归约）"这句话，并在代码里指出对应的两处下标。若暂时无法运行测试环境，此实践为源码阅读型，不需要 GPU。

#### 4.2.5 小练习与答案

**练习 1**：`dup_counts[1]` 与 `dup_groups` 第三列（`dup_n`）之间有什么数量关系？

**答案**：`dup_counts[1] = Σ dup_groups[g][2]`（对所有有效组求和）。每个组向 `dup_loffs` 贡献恰好 `dup_n` 个条目（L680-681 中 `my_dup += dup_count` 与发射循环一一对应），warp 聚合预留时两个计数器就是成对递增的（L631-639）。

**练习 2**：假设某 token 的 K=8 个条目全部落到同一目的 rank。dispatch 在 NVLink 上为这个 token 传几行 payload、几个权重？相比不去重省了多少字节（H=2048，bf16）？

**答案**：传 1 行 payload（主条目）+ 8 个权重（每条目一个，4 字节 fp32）。不去重要传 8 行；省下 7 行 × 2048 × 2 B = **28672 字节**，而权重开销只有 8 × 4 = 32 字节。这正是负数编码"只传权重不传 payload"的收益来源。

**练习 3**：为什么 `dup_groups` 的构建放在 dispatch 内核里（与数据传输并行），而不是放进 planning 内核或单独一个内核？

**答案**：三个理由。① builder 的扫描是延迟瓶颈（依赖加载链），藏在 dispatch 的 NVLink 传输之下等于免费（[moonep/constants.py:12-17](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/constants.py#L12-L17) 给出了选 4 个 warp 的实测依据）；② `src_info` 是 planning 的暂存 scratch，会被下一轮 planning 覆盖，只有紧随其后的 fresh dispatch 能安全消费（`build_dedup_map` 开关的由来）；③ 单独内核需要一次额外的跨 rank 屏障与启动开销，而 dispatch 本来就有 exit 屏障可复用。

## 5. 综合实践

**任务**：编写纯 Python 函数 `simulate_dedup(topk, dst)`，完整复现"检测每个 token 落到相同目的 rank 的重复 top-k 项"这一链路，输出与 `MoonEPCommPlan` 字段定义对齐的去重结构，并用手工构造的用例自检。

**实践目标**：把 4.1 的编码与 4.2 的契约串成一条链：raw dst → 规范化 → 按目的 rank 组织 dup 三件套。

**操作步骤**（示例代码，保存为 `simulate_dedup.py`，`python simulate_dedup.py` 运行）：

```python
# 示例代码：单源 rank 视角的去重模拟（对齐 tests/planning_reference.py Part 3）
from collections import defaultdict

def simulate_dedup(topk, dst, NvS, S, K, R):
    """topk: [S, K] 专家 id（仅用于展示，不参与编码）；
    dst:   长度 S*K 的 raw dst 列表（d*NvS + loff）。
    返回 (dst_canonical, dup_groups, dup_loffs, dup_counts)。"""
    dst = list(dst)
    # dup 结构按"目的 rank"组织（接收端视角），规范化只改自己的 dst（发送端视角）
    dup_groups = [[] for _ in range(R)]   # 每项 (primary_loff, dup_start, dup_n)
    dup_loffs  = [[] for _ in range(R)]
    for s in range(S):
        groups = defaultdict(list)        # dest -> [(k, loff), ...] 按 k 升序
        for k in range(K):
            v = dst[s * K + k]
            groups[v // NvS].append((k, v % NvS))
        for dest, items in groups.items():
            primary_k, primary_loff = items[0]          # 最小 k = 主槽（与内核两端一致）
            if len(items) > 1:
                dup_groups[dest].append(
                    (primary_loff, len(dup_loffs[dest]), len(items) - 1))
                dup_loffs[dest].extend(loff for _, loff in items[1:])
            for k, _ in items[1:]:                      # 其余条目编码 -raw-1
                dst[s * K + k] = -dst[s * K + k] - 1
    dup_counts = [[len(g), len(l)] for g, l in zip(dup_groups, dup_loffs)]
    return dst, dup_groups, dup_loffs, dup_counts

# ---- 自检用例：S=2, K=4, NvS=64, R=2 ----
NvS, S, K, R = 64, 2, 4, 2
topk = [[10, 11, 12, 13], [20, 21, 22, 23]]
# 手工构造：token0 的 4 条目中 3 条去 rank1（重复），token1 中 2 条去 rank0（重复）
raw = [1*NvS+5, 0*NvS+9, 1*NvS+6, 1*NvS+7,   0*NvS+1, 1*NvS+8, 0*NvS+2, 0*NvS+3]
dst, groups, loffs, counts = simulate_dedup(topk, raw, NvS, S, K, R)
print("canonical dst:", dst)
print("rank0 groups :", groups[0], "loffs:", loffs[0])   # 期望 (1,0,2) / [2,3]
print("rank1 groups :", groups[1], "loffs:", loffs[1])   # 期望 (5,0,2) / [6,7]
print("counts       :", counts)                          # 期望 [[1,2],[1,2]]
```

**需要观察的现象与预期结果**：

1. `canonical dst` 中，token0 的第 0/2/3 条目（rank1）只有第 0 条保持非负 `69`，第 2/3 条变为 `-71`、`-72`（即 `-(1*NvS+6)-1`、`-(1*NvS+7)-1`）；token1 的第 4/6/7 条目（rank0）中第 4 条保持 `1`，第 6/7 条变为 `-3`、`-4`。逐项与 4.1 的 `enc` 核对。
2. `rank1` 收到一个组 `(5, 0, 2)`：主槽 `loff=5`、重复槽 `[6, 7]`——对应 [moonep/dispatch.py:665-668](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/dispatch.py#L665-L668) 写入的三元组；`dup_loffs` 长度 2 与 `dup_n=2` 一致（练习 1 的关系）。
3. 输出的三件套与 `MoonEPCommPlan` 的契约对齐方式：把 `dup_groups[dest]` 摆进 `(NvS, 3)` 张量的前 `dup_counts[dest][0]` 行、`dup_loffs[dest]` 摆进 `(NvS,)` 的前 `dup_counts[dest][1]` 个元素即可，其余是未定义的尾部。
4. 进阶自检（可选）：把本函数输出与 `tests/planning_reference.py` Part 3 在同一份随机输入上对拍（需要多卡环境运行参考实现；无卡则此步标注**待本地验证**）。

## 6. 本讲小结

- **负数编码**：同一 token 的多个 top-k 条目落到同一目的 rank 时，只有第一次出现（最小 k）保持非负并拷贝 payload，其余写 `-raw_dst - 1`——恒负、双射、符号位即标志位；dispatch consumer 用 `dst >= 0` 判别、`-v - 1` 解码，权重散射始终执行。
- **省的是 payload 不是权重**：每个 top-k 条目有自己的路由权重，combine 加权求和需要逐条目的权重槽，所以 raw dst 必须可恢复；省下的传输量按整行 hidden 计（H=2048 时每行 4 KB）。
- **契约三件套**：`dup_groups[NvS,3]` 存 `(primary_loff, dup_start, dup_n)` 组头，`dup_loffs[NvS]` 是扁平重复槽表，`dup_counts[2]` 是两个紧凑前缀长度；只有前缀有效，顺序由 atomicAdd 到达顺序决定、不保证稳定。
- **视角分离**：规范化是发送端视角（省 NVLink 传输），dup 结构是接收端视角（epilogue 本地扇出补行、prologue 本地累加回主行），两者都只操作本 rank shard 的 `loff`。
- **构建时机**：三件套在 fresh dispatch 的 builder warps 里从 `src_info` 物化（`atom_min` 按 `(kidx<<24)|loff` 选举主槽，与 planning 端"保留最小 k"两端一致），plan 复用路径直接复用，`build_dedup_map=False` 时不重建。
- **边界即契约**：`R <= 128`（位图）、`K <= 32`（kmask）、`kidx 7 位 / loff 24 位`（primary_packed）等限制由 `_check_dedup_encoding_bounds` 在 launch 前挡下，位宽常量集中在 `moonep/constants.py`。

## 7. 下一步学习建议

去重编码讲完，单元 3（在线规划器）就闭环了。下一讲 **u4-l1（CuTe DSL 与 PTX 基础设施）** 将转向实现层：本讲反复出现的 `atom_min_relaxed_gpu_s32`、`atom_or_relaxed_gpu_b32`、`popc_b32`、`ctz_b32` 等原语都封装在 `moonep/_common.py` 的 inline PTX 里。带着本讲的问题去读会更有针对性：relaxed 内存序为什么够用（builder 之后有分层屏障兜底）？

后续阅读线索：

- **u4-l2 / u4-l3**：dispatch 内核全景与本讲 builder warps 所在的 warp 特化布局；
- **u4-l4 / u4-l5**：`dup_groups` 的两个消费者——dispatch epilogue（扇出）与 combine prologue（累加）的流水线实现；
- **u6-l4**：`dedup_plan_semantic_errors` 的集合比较方法论，是理解"顺序不稳定契约"如何被测试保障的最佳材料。
