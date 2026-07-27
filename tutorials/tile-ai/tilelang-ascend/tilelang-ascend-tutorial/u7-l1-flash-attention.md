# FlashAttention 实现案例

## 1. 本讲目标

FlashAttention 是大模型里最关键的算子之一，也是 tile-lang 在昇腾上最能体现「Cube 算矩阵乘、Vector 算逐元素与归约、两核经 workspace 中转」协同范式的综合案例。学完本讲，你应当能够：

- 说清 FlashAttention 的 **online softmax** 数学原理，以及它为什么能把 \(O(N^2)\) 的显存访问压成块内 \(O(N)\)。
- 读懂昇腾上 FlashAttention 的 **两阶段数据流**：Cube 核算 \(S=QK^\top\) 与 \(O=PV\)，Vector 核做 online softmax，两核通过三块 GM workspace 中转。
- 区分本项目里的 **三种写法**：纯手写的 Expert 版（`flash_attn_bhsd.py`）、自动 CV 分离的 Developer 版（`flash_attn_bshd_developer.py`）、以及 Expert 结构 + 自动同步的 `cc_sync` 版。
- 把前面学过的 `T.gemm_v0`、`T.reduce_max`/`reduce_sum`、跨核 `cross_flag` 同步、workspace 消除、软件流水串成一个完整算子。

## 2. 前置知识

本讲是把多个机制「拼装」在一起的收口讲义，需要你已经建立以下认知（来自前置讲义）：

- **昇腾存储层级与 Cube/Vector 分工**（u1-l1、u5-l1）：Cube 核（AIC）做矩阵乘，数据落在 L1/L0A/L0B/L0C；Vector 核（AIV）做逐元素与归约，数据落在 UB；两核**不能直连**，必须经 GM/L2 workspace 中转。
- **`T.gemm_v0`**（u3-l3）：块级矩阵乘，A、B 在 L1，结果在 L0C，模板内部全包 L1→L0 搬运与累加，`init=True` 表示首段清零。
- **`T.reduce_max`/`reduce_sum`**（u3-l4）：沿某轴把 UB tile 收缩，`dim=-1` 是行归约（每行得到一个值）。
- **`T.copy` 跨 CV 搬运与 workspace 消除**（u3-l2、u5-l4）：写 `T.copy(l0c, ub)` 这种 Cube→Vector 拷贝时，物理上必须经 GM 两阶段中转，由 `AscendWorkspaceReduction` pass 自动展开。
- **核间 `cross_flag` 同步**（u4-l2、u5-l1）：`set_cross_flag`/`wait_cross_flag` 用事件编号配对，保证一核写完 workspace、另一核才能读。
- **自动 CV 分离与跨核流水**（u5-l1、u5-l2）：Developer 模式下 `CombineCV` pass 自动拆 Cube/Vector，`CrossCorePipeline` 识别同时含两核操作的 `T.Pipelined` 循环。

如果你对其中某项还陌生，建议先回看对应讲义再继续。

## 3. 本讲源码地图

本讲围绕三个实现同一算子的脚本，外加一个把数学讲清楚的 README：

| 文件 | 模式 | 关键特征 |
|------|------|---------|
| [examples/flash_attention/flash_attn_bhsd.py](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/flash_attention/flash_attn_bhsd.py) | 纯 Expert | 手写 `T.Scope("C"/"V")`、手写 `set/wait_cross_flag`、手写 `T.annotate_address`、手写 `workspace_idx`、手写 vid 切分 |
| [examples/developer_mode/flash_attn_bshd_developer.py](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/developer_mode/flash_attn_bshd_developer.py) | 纯 Developer | `alloc_shared/fragment`、`threads=2`（vid 消除）、单条 `T.Pipelined`、`pass_configs` 全开自动 CV 分离/同步/内存规划 |
| [examples/flash_attention/flash_attn_bhsd_cc_sync.py](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/flash_attention/flash_attn_bhsd_cc_sync.py) | Expert 结构 + 自动同步 | 保留 Expert 的显式 buffer 与 vid 结构，但删掉 `T.Scope` 与手写 flag，靠 `pass_configs` 自动插同步 |
| [examples/developer_mode/README.md](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/developer_mode/README.md) | 文档 | 给出 online softmax 的标准数学更新规则 |

> 命名提示：`bhsd` 指 batch-head-seq-dim 的张量布局，`bshd` 指 batch-seq-head-dim；两者只是 Q/K/V 的轴顺序不同，算法完全一致。本讲以算法为主线，布局差异不影响理解。

涉及的核心前端原语（仅引用签名，细节见前置讲义）：

- [tilelang/language/ascend.py:343-380](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/language/ascend.py#L343-L380) —— `T.gemm_v0` 块级矩阵乘。
- [tilelang/language/reduce_ascend.py:362-385](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/language/reduce_ascend.py#L362-L385) —— `T.reduce_max` 行最大归约。
- [tilelang/language/reduce_ascend.py:414-435](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/language/reduce_ascend.py#L414-L435) —— `T.reduce_sum` 行求和归约。
- [tilelang/language/__init__.py:226-228](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/language/__init__.py#L226-L228) —— `T.annotate_address` 手写地址钉死。

---

## 4. 核心概念与源码讲解

### 4.1 FlashAttention 与 online softmax 数学原理

#### 4.1.1 概念说明

标准注意力是：

\[
\mathrm{Attention}(Q,K,V)=\mathrm{softmax}\!\left(\frac{QK^\top}{\sqrt{d_k}}\right)V
\]

朴素做法要先物化整张 \(S=QK^\top\in\mathbb{R}^{N\times N}\)，序列一长就爆显存。FlashAttention 的核心思想是**分块 + 在线 softmax**：把 K、V 沿序列方向切成 block_N 大小的块，逐块算 \(S\) 的一个 \(\text{block\_M}\times\text{block\_N}\) 子矩阵，并用一组「行级」状态量增量地维护 softmax 的分子与分母，**绝不物化完整的 \(N\times N\) 矩阵**。

#### 4.1.2 核心流程

对每一行 \(i\)（即 Q 的某 block_M 行），维护两个状态：

- \(m_i\)：到目前为止见过的最大 logit（行最大值）。
- \(\ell_i\)：到目前为止的「缩放指数和」。
- \(O_i\)：到目前为止的输出累加。

每来一个新块 \(S_{\text{new}}\)（\(\text{block\_M}\times\text{block\_N}\)），执行三步在线更新（来自 [developer_mode/README.md:30-53](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/developer_mode/README.md#L30-L53)）：

1. **更新行最大值**：
   \[
   m_i^{\text{new}}=\max\!\left(m_i^{\text{old}},\ \max_j S_{\text{new}}[i,j]\right)
   \]
2. **更新指数和**（注意要把旧和按 \(e^{m_i^{\text{old}}-m_i^{\text{new}}}\) 缩放）：
   \[
   \ell_i^{\text{new}}=\ell_i^{\text{old}}\cdot e^{m_i^{\text{old}}-m_i^{\text{new}}}+\sum_j e^{S_{\text{new}}[i,j]-m_i^{\text{new}}}
   \]
3. **更新输出**（旧输出同样要缩放，再加新块的贡献）：
   \[
   O_i^{\text{new}}=O_i^{\text{old}}\cdot e^{m_i^{\text{old}}-m_i^{\text{new}}}+e^{S_{\text{new}}[i,j]-m_i^{\text{new}}}\cdot V_{\text{block}}
   \]

所有块处理完后，做一次归一化：

\[
O_i^{\text{final}}=O_i\,/\,\ell_i
\]

这套规则的关键在于：缩放因子 \(e^{m_i^{\text{old}}-m_i^{\text{new}}}\) 让旧的累加结果始终以**当前最新最大值**为基准，从而每块只需保留 block_N 宽的中间结果，显存从 \(O(N^2)\) 降到 \(O(N)\)。

#### 4.1.3 三个原语在 FA 中的角色

把上面的数学映射到昇腾硬件与 tile-lang 原语：

| 数学操作 | 硬件 / 原语 | 说明 |
|---------|-----------|------|
| \(S=QK^\top\)、\(O=PV\)（两次矩阵乘） | Cube 核 / `T.gemm_v0` | 矩阵乘是 Cube 的本职，结果落 L0C，`transpose_B=True` 直接算 \(QK^\top\) |
| \(\max_j\)、\(\sum_j\)（行归约） | Vector 核 / `T.reduce_max`、`T.reduce_sum` | 沿 `dim=-1` 把 \(\text{block\_M}\times\text{block\_N}\) 收缩成 \(\text{block\_M}\) |
| \(e^x\)、加减乘除、缩放 | Vector 核 / `T.tile.exp/sub/mul/add` | 逐元素向量指令 |
| \(S\) 与 \(O\) 在两核间流动 | GM workspace / `T.copy` 跨 CV | Cube 产 L0C，Vector 消费 UB，中间必须经 GM |

可以看到，FlashAttention 天然把「两矩阵乘给 Cube、softmax 给 Vector」的分工用到了极致，这正是它在昇腾上的标准实现范式。

#### 4.1.4 代码实践（源码阅读型）

**目标**：把数学公式和 README 对齐，确认状态量命名。

1. 打开 [examples/developer_mode/README.md:22-53](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/developer_mode/README.md#L22-L53)。
2. 对照下面的命名映射（与 4.2/4.3 源码一致）：
   - `m_i` ↔ \(m_i\)（行最大值）
   - `sumexp` ↔ \(\ell_i\)（缩放指数和）
   - `acc_o` ↔ \(O_i\)（输出累加）
   - `m_i_prev` ↔ 临时量 \(e^{m_i^{\text{old}}-m_i^{\text{new}}}\)，复用了旧 `m_i` 的存储
3. **观察现象**：你会注意到代码里 `m_i_prev` 先存旧的 `m_i`，算完新 `m_i` 后做 `sub(m_i_prev, m_i_prev, m_i)` 再 `exp`，正好得到 \(e^{m_i^{\text{old}}-m_i^{\text{new}}}\)。
4. **预期结果**：能用自己的话讲清「为什么旧 acc_o、旧 sumexp 都要乘上 `m_i_prev`」。
5. 待本地验证（无需运行）。

#### 4.1.5 小练习与答案

**练习 1**：为什么缩放因子是 \(e^{m_i^{\text{old}}-m_i^{\text{new}}}\) 而不是 \(e^{m_i^{\text{new}}-m_i^{\text{old}}}\)？
> **答**：softmax 要求所有指数都以同一个最大值为基准才数值稳定。新最大值 \(m_i^{\text{new}}\ge m_i^{\text{old}}\)，旧累加结果当初是以 \(m_i^{\text{old}}\) 为基准存的，换基准要乘 \(e^{m_i^{\text{old}}-m_i^{\text{new}}}\le 1\) 把它「降」到新基准。

**练习 2**：如果某个块的 \(S_{\text{new}}\) 全是 \(-\infty\)（mask 掉），online softmax 还正确吗？
> **答**：正确。该块 \(\max_j S_{\text{new}}=-\infty\)，不会抬高 \(m_i\)；\(e^{-\infty-m_i^{\text{new}}}=0\)，对 \(\ell_i\) 与 \(O_i\) 贡献为 0，等价于该块不存在。

---

### 4.2 Expert 模式实现：Cube/Vector 两阶段与手写同步

#### 4.2.1 概念说明

[flash_attn_bhsd.py](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/flash_attention/flash_attn_bhsd.py) 是**最显式、最贴近硬件**的版本：用户自己声明每一块 buffer 落在哪个物理存储、自己用 `T.Scope` 划分 Cube/Vector 执行域、自己手写 `set/wait_cross_flag` 控制两核时序、自己用 `workspace_idx` 声明 GM 中转区、自己用 `T.annotate_address` 钉死地址。读通它，就等于读通了「昇腾 FlashAttention 的真实数据通路」。

这个版本用 **Expert 抽象**（u4-l1）：`T.alloc_L1`/`alloc_L0C`/`alloc_ub` 把 scope 直接钉死，`T.Scope("C")`/`T.Scope("V")` 显式划分执行域。它返回 `(cid, vid)`（u2-l2），所以 UB 申请与 GM 偏移都要手动按 `block_M//2` 切分——这是 1:2 CV 配比下 Vector 子核的硬件细节泄漏到前端（u5-l3）。

#### 4.2.2 核心流程

整个 kernel 的数据流是 **Cube 与 Vector 交替握手** 的两阶段流水（注意：这里用的是 `T.serial` + cross_flag 握手，**不是** `T.Pipelined` 软件流水）。每个 K 块迭代：

```
[Cube 核 Scope("C")]
  1. copy Q → q_l1                     （循环外，只搬一次）
  2. for k in K 块:
       a. copy K_block → k_l1
       b. gemm_v0(q_l1, k_l1, acc_s_l0c, transpose_B=True)   # S = Q @ K^T
       c. copy acc_s_l0c → workspace_1[cid]                   # 把 S 写到 GM 给 Vector
       d. set_cross_flag("FIX", 0)                            # 通知 Vector: workspace_1 就绪
       e. wait_cross_flag(1)                                  # 等 Vector 把 softmax(S) 写回 workspace_2
       f. copy workspace_2[cid] → acc_s_l1                    # 取回 softmax(S)
       g. copy V_block → v_l1
       h. gemm_v0(acc_s_l1, v_l1, acc_o_l0c)                  # partial O = softmax(S) @ V
       i. copy acc_o_l0c → workspace_3[cid]                   # 把 partial O 写到 GM 给 Vector
       j. set_cross_flag("FIX", 2)                            # 通知 Vector: workspace_3 就绪
       k. wait_cross_flag(3)                                  # 等 Vector 处理完

[Vector 核 Scope("V")]
  1. acc_o=0, sumexp=0, m_i=-inf
  2. for k in K 块:
       a. wait_cross_flag(0)                                  # 等 Cube 写好 workspace_1 (S)
       b. copy workspace_1[cid, vid*...] → acc_s_ub_          # 取回 S（按 vid 取自己那一半）
       c. online softmax: reduce_max / exp / reduce_sum / 缩放  # 见 4.1.2 三步更新
       d. copy softmax(S) → workspace_2[cid, vid*...]]        # 把 softmax(S) 写回 GM 给 Cube
       e. set_cross_flag("MTE3", 1)                           # 通知 Cube: workspace_2 就绪
       f. wait_cross_flag(2)                                  # 等 Cube 写好 workspace_3 (partial O)
       g. copy workspace_3[cid, vid*...] → acc_o_ub           # 取回 partial O
       h. acc_o += acc_o_ub                                   # 累加到输出（含旧值缩放）
       i. set_cross_flag("V", 3)                              # 通知 Cube: 本块处理完
  3. acc_o /= sumexp                                          # 最终归一化
  4. copy acc_o → Output
```

三块 workspace 各司其职：

| workspace | 形状 | 谁写 | 谁读 | 携带数据 |
|-----------|------|------|------|---------|
| `workspace_1` | `[block_num, block_M, block_N]` | Cube | Vector | 原始 logit \(S=QK^\top\) |
| `workspace_2` | `[block_num, block_M, block_N]` | Vector | Cube | softmax 后的 \(P\) |
| `workspace_3` | `[block_num, block_M, dim]` | Cube | Vector | 本块部分输出 \(PV\) |

#### 4.2.3 源码精读

**入口与参数**：`workspace_idx=[4,5,6]` 声明后三个参数（即三块 workspace）由框架在运行时自动申请，对调用者透明（u5-l4）；`out_idx=[3]` 表示 Output 由运行时分配返回。

[flash_attn_bhsd.py:8](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/flash_attention/flash_attn_bhsd.py#L8) —— 装饰器声明输出与 workspace 索引。

[flash_attn_bhsd.py:32-34](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/flash_attention/flash_attn_bhsd.py#L32-L34) —— 三块 workspace 形参，首维 `block_num` 按 `cid` 隔离不同核。

**buffer 分配**：Cube 侧用 L1/L0C，Vector 侧用 UB，且 UB 全部按 `block_M//2` 切（因为 `vid` 取半）：

[flash_attn_bhsd.py:41-60](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/flash_attention/flash_attn_bhsd.py#L41-L60) —— 注意 `acc_s_ub`/`acc_o` 等第 0 维都是 `block_M // 2`。

**手写地址**：因为没有开自动内存规划，必须用 `T.annotate_address` 把每个 buffer 的字节偏移钉死，并手工安排复用（如 `acc_s_half`/`acc_o_ub`/`acc_o_half` 共用 98944）：

[flash_attn_bhsd.py:62-84](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/flash_attention/flash_attn_bhsd.py#L62-L84)

**Cube 阶段**（两次 gemm + 三次 workspace 写 + 四次 cross flag）：

[flash_attn_bhsd.py:86-114](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/flash_attention/flash_attn_bhsd.py#L86-L114) —— `T.Scope("C")` 包裹整个 Cube 域。第 93 行 `T.gemm_v0(q_l1, k_l1, acc_s_l0c, transpose_B=True, init=True)` 算 \(S=QK^\top\)；第 96 行把 `acc_s_l0c` 写到 `workspace_1`；第 98 行 `set_cross_flag("FIX", 0)` 通知 Vector；第 107 行第二次 `gemm_v0` 算 \(PV\)。

**cross flag 配对**（最关键、最易错处）。四个事件编号两两配对，Cube 的 `set` 对应 Vector 的 `wait`：

| 事件号 | Cube 侧 | Vector 侧 | 语义 |
|-------|---------|-----------|------|
| 0 | `set_cross_flag("FIX", 0)` L98 | `wait_cross_flag(0)` L130 | workspace_1（S）就绪 |
| 1 | `wait_cross_flag(1)` L100 | `set_cross_flag("MTE3", 1)` L184 | workspace_2（softmax S）就绪 |
| 2 | `set_cross_flag("FIX", 2)` L113 | `wait_cross_flag(2)` L186 | workspace_3（partial O）就绪 |
| 3 | `wait_cross_flag(3)` L114 | `set_cross_flag("V", 3)` L197 | 本块迭代完成 |

第一个参数（`"FIX"`/`"MTE3"`/`"V"`）是置位方所在流水线名（u4-l2），核间同步里它主要起标记作用，真正配对靠第二个参数——事件编号。

**Vector 阶段**（online softmax 的三步更新落地）：

[flash_attn_bhsd.py:116-145](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/flash_attention/flash_attn_bhsd.py#L116-L145) —— 第 142 行 `T.reduce_max(acc_s_ub, m_i, dim=-1)` 算新行最大值；第 145 行 `T.tile.max(m_i, m_i, m_i_prev)` 即 \(m_i^{\text{new}}=\max(m_i^{\text{old}},\max_j S_{\text{new}})\)。

[flash_attn_bhsd.py:148-169](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/flash_attention/flash_attn_bhsd.py#L148-L169) —— 第 151 行 `exp(m_i_prev)` 得到缩放因子 \(e^{m_i^{\text{old}}-m_i^{\text{new}}}\)；第 162 行 `T.reduce_sum(acc_s_ub, sumexp_i_ub, dim=-1)` 算当前块指数和；第 165 行 `mul(sumexp, sumexp, m_i_prev)` 把旧 sumexp 缩放、第 168 行 `add` 并入新块。

[flash_attn_bhsd.py:189-197](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/flash_attention/flash_attn_bhsd.py#L189-L197) —— 取回 partial O 并累加到 `acc_o`（旧 `acc_o` 的缩放在 L171-174 已做）。

[flash_attn_bhsd.py:200-209](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/flash_attention/flash_attn_bhsd.py#L200-L209) —— 最终 `acc_o /= sumexp` 归一化并写回 Output（写回时按 `vid` 偏移到正确的行）。

#### 4.2.4 代码实践（源码阅读型）

**目标**：把四个 cross flag 的配对关系在纸上画清楚。

1. 打开 [flash_attn_bhsd.py](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/flash_attention/flash_attn_bhsd.py)。
2. 用两种颜色高亮 Cube 域（L86-114）与 Vector 域（L116-209）里的所有 `set_cross_flag`/`wait_cross_flag`。
3. 按事件号 0/1/2/3 把 `set` 与 `wait` 连线，验证每对都是「一核 set、另一核 wait」。
4. **观察现象**：Cube 用 `"FIX"` 置位（Cube 的 fixpipe 流水写 GM），Vector 用 `"MTE3"`/`"V"` 置位（Vector 的搬出/计算流水）。
5. **预期结果**：得到一张四条配对线的握手时序图，与 4.2.2 的伪代码完全对应。
6. 待本地验证（无需运行）。

#### 4.2.5 小练习与答案

**练习 1**：如果删掉 Cube 侧的 `wait_cross_flag(1)`（L100），会发生什么？
> **答**：Cube 可能在 Vector 还没把 softmax(S) 写回 `workspace_2` 时就去读它，读到上一块或未初始化的数据，结果错误。cross flag 的作用正是保证「写完才能读」的跨核数据可见性。

**练习 2**：为什么 `workspace_1/2/3` 的第一维是 `block_num` 而不是 `batch*heads*seq`？
> **答**：`block_num = seq_len//block_M * heads * batch` 已经把这三个维度展平成逻辑核数（见 L24、L37-39 的 cid 解码），每个核独占 `workspace[cid]` 一份，互不干扰，无需再加锁。

**练习 3**：第 93 行 `init=True` 与第 107 行 `init=True` 都设了，意味着什么？
> **答**：每次 `gemm_v0` 都先把 L0C 累加器清零再算。这里每个 K 块的 \(S\)、\(PV\) 都是**独立**矩阵乘（不跨块累加 K），跨块的累加是 online softmax 在 Vector 侧用 `acc_o +=` 完成的，所以矩阵乘本身每块都要 `init`。

---

### 4.3 Developer 模式实现：单循环自动 CV 分离与软件流水

#### 4.3.1 概念说明

[flash_attn_bshd_developer.py](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/developer_mode/flash_attn_bshd_developer.py) 把 4.2 里**所有手写的东西都交给编译器**，是本项目推荐的「主力写法」。对比要点：

- 不写 `T.Scope`：Cube/Vector 划分由 `CombineCV` pass 自动推断（u5-l1）。
- 不写 `set/wait_cross_flag`：核间同步由 `TL_ASCEND_AUTO_CV_SYNC` 自动插入（u5-l1）。
- 不写 `workspace_idx` 与三块 GM：`T.copy(acc_s_l0c, acc_s_ub_)` 这种 L0C→UB 跨 CV 拷贝，由 `AscendWorkspaceReduction` pass 自动展开成两阶段 GM 中转（u5-l4）。
- 不写 `T.annotate_address`：地址由 `TL_ASCEND_MEMORY_PLANNING` 自动规划（u6-l5）。
- 不手写 vid 切分：`threads=2` 触发 vid 消除，UB 用完整 `block_M`、由 `AscendVidReduction` pass 自动减半（u5-l3）。
- 用 `T.Pipelined(num_stages=2)`：把 K 循环变成软件流水，且因为循环体同时含 `gemm_v0`（Cube）与 `T.tile.*`（Vector），会被 `CrossCorePipeline` 识别为**跨核流水**（u5-l2、u3-l6）。

代价是：用户必须打开一组 `pass_configs` 开关，并理解编译器替你做了什么。

#### 4.3.2 核心流程

Developer 版只有一个 `T.Pipelined` 循环，Cube 与 Vector 的操作**交错写在同一循环体里**，编译器再拆开：

```
pass_configs 全开:
  TL_ASCEND_AUTO_CV_COMBINE  → CombineCV 拆 Cube/Vector scope
  TL_ASCEND_AUTO_SYNC        → 核内自动同步
  TL_ASCEND_AUTO_CV_SYNC     → 核间自动 cross flag
  TL_ASCEND_MEMORY_PLANNING  → 自动地址规划

with T.Kernel(block_num, threads=2) as (cid):     # vid 消除，只返回 cid
  alloc_shared/fragment 全套 buffer                 # scope 由编译器推断
  fill acc_o/sumexp/m_i 初值
  copy Q → q_l1
  for k in T.Pipelined(K 块数, num_stages=2):       # 软件流水（跨核）
    # —— Cube 段 ——
    copy K_block → k_l1
    gemm_v0(q_l1, k_l1, acc_s_l0c, transpose_B=True)   # S = Q @ K^T
    copy(acc_s_l0c, acc_s_ub_)                          # L0C→UB，自动展开成 GM 中转
    # —— Vector 段（online softmax）——
    reduce_max → 缩放 → exp → reduce_sum → 更新 sumexp
    # —— Cube 段 ——
    copy softmax(S) → acc_s_l1
    copy V_block → v_l1
    gemm_v0(acc_s_l1, v_l1, acc_o_l0c)                  # partial O = P @ V
    copy(acc_o_l0c, acc_o_ub)                           # L0C→UB，自动展开
    # —— Vector 段 ——
    缩放旧 acc_o，acc_o += acc_o_ub
  归一化 acc_o /= sumexp
  copy acc_o → Output
```

关键直觉：**用户写的是「一个核顺序执行的伪代码」**，编译器再把它拆成 Cube/Vector 两份、插好同步、铺好 workspace 与地址。这与 Expert 版「用户自己写两份并手动握手」形成鲜明对比。

#### 4.3.3 源码精读

**pass_configs 全开**（这是 Developer 模式的「总开关」）：

[flash_attn_bshd_developer.py:13-21](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/developer_mode/flash_attn_bshd_developer.py#L13-L21) —— 四个开关全开，并通过 `@tilelang.jit(..., pass_configs=pass_configs)` 注入。

**threads=2 的 vid 消除**：注意这里只解构出 `cid`，UB 申请用完整 `block_M`（不除 2），GM 偏移也不带 `vid`——全部交给 `AscendVidReduction` pass：

[flash_attn_bshd_developer.py:47](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/developer_mode/flash_attn_bshd_developer.py#L47) —— `with T.Kernel(block_num, threads=2, is_npu=True) as (cid)`。

[flash_attn_bshd_developer.py:52-71](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/developer_mode/flash_attn_bshd_developer.py#L52-L71) —— 对比 Expert 版，这里 `acc_s_ub`、`acc_o` 等都用完整 `[block_M, ...]`，无需 `//2`。

**单条 T.Pipelined 跨核流水**：

[flash_attn_bshd_developer.py:78](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/developer_mode/flash_attn_bshd_developer.py#L78) —— `for k in T.Pipelined(T.ceildiv(seq_len, block_N), num_stages=2)`。循环体里既有 `gemm_v0`（落 L1/L0C，Cube scope）又有 `T.tile.*`（落 UB，Vector scope），`CrossCorePipeline` 据此判定为跨核流水并改写成外层波 + 内层 stage 两层循环（u5-l2）。

**两次矩阵乘与跨 CV 拷贝**：

[flash_attn_bshd_developer.py:80-82](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/developer_mode/flash_attn_bshd_developer.py#L80-L82) —— 第 81 行 `gemm_v0` 算 \(S\)；第 82 行 `T.copy(acc_s_l0c, acc_s_ub_)` 是 L0C→UB 的跨 CV 拷贝，编译器自动展开成「L0C→GM→UB」两阶段（u5-l4），等价于 Expert 版手写的 `workspace_1` 通路。

**online softmax 的 reduce**：

[flash_attn_bshd_developer.py:90-102](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/developer_mode/flash_attn_bshd_developer.py#L90-L102) —— 第 90 行 `T.reduce_max(acc_s_ub, m_i, dim=-1)`、第 100 行 `T.reduce_sum(acc_s_ub, sumexp_i_ub, dim=-1)`，与 Expert 版数学完全一致，只是写在一层循环里、不带 `T.barrier_all()`（同步由 `TL_ASCEND_AUTO_SYNC` 自动插）。

**第二次矩阵乘与最终归一化**：

[flash_attn_bshd_developer.py:108-122](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/developer_mode/flash_attn_bshd_developer.py#L108-L122) —— 第 109 行 `gemm_v0(acc_s_l1, v_l1, acc_o_l0c)` 算 \(PV\)；第 110 行 `T.copy(acc_o_l0c, acc_o_ub)` 又一次跨 CV 拷贝；循环外第 118-119 行做最终 `acc_o /= sumexp`。

#### 4.3.4 代码实践（生成代码对比型）

**目标**：用 `get_kernel_source()` 看 Developer 模式编译后到底生成了什么。

1. 在 [flash_attn_bshd_developer.py](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/developer_mode/flash_attn_bshd_developer.py) 末尾的 `func = flash_attention_fwd(...)` 之后加一行：
   ```python
   print(func.get_kernel_source())
   ```
   （示例代码，非项目原有）
2. 运行脚本（需真实昇腾环境或 camodel 仿真，待本地验证）。
3. **观察现象**：在生成的 C++ 里，确认以下编译器替你做的事：
   - 出现 `if ASCEND_IS_AIC` / `if ASCEND_IS_AIV`（或 `#if __DAV_*_CUBE/VEC__`）两段，即 `CombineCV` 把单循环拆成了 Cube/Vector 两份。
   - 出现 `SetCrossCoreFlag`/`WaitCrossCoreFlag`（或同类）调用，即自动核间同步。
   - 出现 GM workspace 的地址计算与两阶段 `DataCopyPad`，即 `AscendWorkspaceReduction` 自动展开的跨 CV 中转。
4. **预期结果**：你能指出「用户写的单循环」在生成代码里变成了多少处 Cube/Vector 分支与同步点。
5. 若无 NPU 环境，可改为纯源码阅读：对照 4.2 的 Expert 版，逐行标注 Developer 版省略了哪些手写内容。

#### 4.3.5 小练习与答案

**练习 1**：Developer 版没有 `workspace_idx`，那 Cube→Vector 的数据是怎么过 GM 的？
> **答**：用户写的 `T.copy(acc_s_l0c, acc_s_ub_)` 是 L0C→UB 跨 CV 拷贝。`AscendWorkspaceReduction` pass 在 lowering 时把它自动改写成「L0C→GM（写）」+ 在真正消费前插入「GM→UB（读）」两阶段，workspace 由编译器经 `auto_gm_indices` 属性自动分配（u5-l4），对用户完全透明。

**练习 2**：如果把 `TL_ASCEND_AUTO_CV_COMBINE` 关掉、只留 `TL_ASCEND_AUTO_SYNC`，会发生什么？
> **答**：`CombineCV` 不拆 scope，Cube/Vector 操作会混在同一执行域里，且 `TL_ASCEND_AUTO_CV_SYNC` 依赖 CV 拆分的产出（u5-l1），核间同步也无从插入。结果要么编译报错，要么生成的代码语义错乱。这两个开关必须**同时开**。

**练习 3**：Developer 版用 `T.Pipelined`，Expert 版用 `T.serial` + 握手，两者性能含义有何不同？
> **答**：`T.Pipelined(num_stages=2)` 是真正的软件流水，第 i 块的搬运与第 i+1 块的计算重叠（双缓冲）；Expert 版的 `T.serial`+cross_flag 是逐块严格握手、迭代间不重叠。前者吞吐更高，后者控制更细但需要手写更多同步。

---

### 4.4 三种写法对比：cc_sync 自动同步版

#### 4.4.1 概念说明

[flash_attn_bhsd_cc_sync.py](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/flash_attention/flash_attn_bhsd_cc_sync.py) 是一个**中间形态**：它保留了 Expert 版的显式 buffer 分配（`alloc_L1`/`alloc_L0C`/`alloc_ub`）、手写地址（`annotate_address`）和手写 vid 切分（返回 `(cid, vid)`、UB 用 `block_M//2`），但**删掉了 `T.Scope` 与所有手写 `cross_flag`**，改由 `pass_configs` 自动插入同步。它的价值在于演示：自动同步 pass 不挑编程模式，可以叠加在 Expert 结构之上。

#### 4.4.2 核心流程

cc_sync 版相对 Expert 版的改动可以用一张表概括：

| 维度 | Expert 版 (bhsd.py) | cc_sync 版 | 谁来做 |
|------|--------------------|-----------|--------|
| `T.Scope("C"/"V")` | 手写 | 删除（注释掉） | `CombineCV` 自动推断 scope |
| `set/wait_cross_flag` | 手写 4 对 | 全部删除 | `TL_ASCEND_AUTO_CV_SYNC` 自动插 |
| `T.barrier_all()` | 手写大量 | 大量删除 | `TL_ASCEND_AUTO_SYNC` 自动插 |
| buffer 分配 | `alloc_L1/L0C/ub`（Expert） | 同 Expert | 用户 |
| 地址规划 | `annotate_address` 手写 | 同 Expert | 用户 |
| vid 切分 | 手写 `block_M//2` | 同 Expert | 用户 |
| workspace | `workspace_idx` 手写 | 同 Expert | 用户 |

即：**结构（buffer/地址/workspace/vid）仍是 Expert 的，时序（scope/同步）交给编译器**。

#### 4.4.3 源码精读

**pass_configs（自动同步三件套）**：

[flash_attn_bhsd_cc_sync.py:12-18](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/flash_attention/flash_attn_bhsd_cc_sync.py#L12-L18) —— 开启 `AUTO_CV_COMBINE`、`AUTO_CV_SYNC`、`AUTO_SYNC`，注意**没有**开 `MEMORY_PLANNING`，所以仍需手写 `annotate_address`。

**删掉的 Scope（关键差异）**：

[flash_attn_bhsd_cc_sync.py:97-110](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/flash_attention/flash_attn_bhsd_cc_sync.py#L97-L110) —— 第 97 行 `# with T.Scope("C"):` 与第 110 行 `# with T.Scope("V"):` 都被注释掉，原本两段分开的循环体现在连续写在一起，由 `CombineCV` 按 buffer scope 自动拆分。

**删掉的 cross flag 与 barrier**：对比 Expert 版 L98/L100/L113/L114 的四处 cross flag 与满篇 `T.barrier_all()`，cc_sync 版的循环体（L99-108、L114-139）里**完全没有**同步原语，全部由自动同步 pass 补回。这正是 `TL_ASCEND_AUTO_SYNC`/`AUTO_CV_SYNC` 的价值——它跟踪 7 条流水线与数据依赖，在需要处自动插 `PipeBarrier` 与 `set/wait_flag`（u4-l3）。

#### 4.4.4 代码实践（diff 对比型）

**目标**：量化 cc_sync 版相比 Expert 版省了多少手写同步。

1. 同时打开 [flash_attn_bhsd.py](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/flash_attention/flash_attn_bhsd.py) 与 [flash_attn_bhsd_cc_sync.py](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/flash_attention/flash_attn_bhsd_cc_sync.py)。
2. 用 `git diff --no-index` 或编辑器对比两文件（只读，不改源码）。
3. **观察现象**：统计删除的 `T.barrier_all()` 与 `set/wait_cross_flag` 数量；确认 buffer 分配、`annotate_address`、`workspace_idx`、vid 切分这些**结构**部分完全一致。
4. **预期结果**：你会发现 cc_sync 版几乎只删同步、不改结构，验证了「自动同步 pass 与显式内存布局正交」。
5. 待本地验证（无需运行）。

#### 4.4.5 小练习与答案

**练习 1**：cc_sync 版为什么还保留 `annotate_address`？
> **答**：它没开 `TL_ASCEND_MEMORY_PLANNING`（对比 Developer 版 L13-18 开了）。没有自动内存规划时，必须手写地址钉死每个 buffer 的偏移（u6-l5）。

**练习 2**：既然 cc_sync 版删了 `T.Scope`，`CombineCV` 怎么知道哪段是 Cube、哪段是 Vector？
> **答**：`CombineCV` 按 buffer scope 反查（u5-l1）：操作 `alloc_L1`/`alloc_L0C` 的语句落 Cube，操作 `alloc_ub` 的语句落 Vector。即使用户不写 `T.Scope`，buffer 的物理 scope 已经把执行域隐式确定了。

---

## 5. 综合实践

把本讲的三种写法与 online softmax 数学串起来，完成下面的**数据流图绘制任务**（本讲的主实践，纯源码阅读型，无需 NPU）：

**任务**：阅读 [flash_attn_bshd_developer.py](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/developer_mode/flash_attn_bshd_developer.py)，画一张完整的「Cube↔Vector 数据流图」，要求：

1. **画出两个核**：左 Cube、右 Vector，中间用 GM workspace 连接。
2. **标注两次矩阵乘**：在 Cube 侧标出 \(S=QK^\top\)（L81）与 \(O=PV\)（L109）。
3. **标注两次跨 CV 拷贝**：即 `T.copy(acc_s_l0c, acc_s_ub_)`（L82）与 `T.copy(acc_o_l0c, acc_o_ub)`（L110），并注明它们在生成代码里会被 `AscendWorkspaceReduction` 自动展开成「L0C→GM→UB」两阶段。
4. **标注 online softmax 的三步更新**：在 Vector 侧标出 reduce_max（L90）、缩放因子（L91-93）、reduce_sum（L100）、sumexp/acc_o 缩放（L101、L114）。
5. **对照 Expert 版** [flash_attn_bhsd.py](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/flash_attention/flash_attn_bhsd.py)：在你的图上用虚线标出 Expert 版里手写的三块 `workspace_1/2/3` 与四个 cross flag，指出它们对应 Developer 版里的哪条 `T.copy` 跨 CV 拷贝（即编译器自动化的部分）。

**预期产出**：一张能同时解释「用户写的单循环」与「编译器拆出的两核 + 自动 workspace + 自动同步」的对照图。画完后，你应当能用一句话说清：Developer 版的每一条 `T.copy(l0c, ub)`，对应 Expert 版的「写 workspace + set_cross_flag + 对面 wait_cross_flag + 读 workspace」整条链路。

> 进阶（需真实环境，待本地验证）：把三种写法分别跑一遍，用 `do_bench`（Developer 版 L157-160 已有示例）对比时延，验证「自动 CV 同步 + 软件流水」相对「手写握手 + serial」的性能收益。

## 6. 本讲小结

- FlashAttention 用 **online softmax** 把 \(O(N^2)\) 显存压成块内 \(O(N)\)：每块只维护行最大值 \(m_i\)、指数和 \(\ell_i\)、输出 \(O_i\) 三个状态，靠缩放因子 \(e^{m_i^{\text{old}}-m_i^{\text{new}}}\) 把旧累加结果始终对齐到最新最大值。
- 昇腾上的标准实现是 **Cube/Vector 两阶段**：Cube 用 `T.gemm_v0` 算 \(QK^\top\) 与 \(PV\)，Vector 用 `T.reduce_max`/`reduce_sum`/`T.tile.*` 做 online softmax，两核经 GM workspace 中转。
- 三块 workspace（\(S\)、softmax(\(S\))、partial \(O\)）是 Cube↔Vector 的唯一数据通道；Expert 版用 `workspace_idx` 手写、Developer 版由 `AscendWorkspaceReduction` pass 自动展开。
- **Expert 版**（`bhsd.py`）最显式：手写 `T.Scope`、`cross_flag`、`annotate_address`、vid 切分；四个 cross flag 按事件号 0/1/2/3 两两配对，保证跨核数据可见性。
- **Developer 版**（`bshd_developer.py`）最简洁：单条 `T.Pipelined` + 四个 `pass_configs` 开关，编译器自动完成 CV 拆分、核间/核内同步、workspace 展开、内存规划、vid 消除。
- **cc_sync 版**是中间形态：保留 Expert 的显式内存布局，但把 scope 与同步交给 `AUTO_SYNC`/`AUTO_CV_SYNC`，证明自动同步 pass 与编程模式正交。

## 7. 下一步学习建议

- **u7-l2 高性能 GEMM 优化**：本讲的两次 `gemm_v0` 只用了最朴素的参数。下一讲会综合 layout/swizzle/pipeline/kL0Size 调参，把单次矩阵乘推到极致，可直接套回 FlashAttention 的 QK 与 PV 两段。
- **u7-l4 调试与性能分析**：FlashAttention 涉及两核、workspace、跨核流水，是最容易出隐蔽错误的算子。学会用 `T.dump_tensor`/`T.printf` 与 `msprof` 验证 online softmax 的中间状态。
- **阅读 `examples/flash_attention/fa_opt/`**：该目录有针对 h16/d128、h32/d512 的自动流水优化版（`flash_attn_bhsd_auto_pipeline_*.py`）与 expert 版（`flash_attn_bhsd_expert_h16_d128.py`），以及性能优化文档 `flash_attention_performance_optimization_zh.md`，是进阶调优的最佳读物。
- **阅读 `examples/developer_mode/sparse_flash_attn_developer.py`**：带 mask 的稀疏 FlashAttention，展示变长窗口下 `gemm_v0` 的 `n_actual` 参数与 reduce 的 `real_shape` 用法，是对本讲的进阶扩展。
