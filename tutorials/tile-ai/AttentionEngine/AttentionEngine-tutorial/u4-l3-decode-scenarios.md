# 解码（decode）场景：MHA / GQA 解码

## 1. 本讲目标

前面几讲我们看的都是「训练 / prefill」场景：Query 序列很长，注意力 kernel 沿 Query 方向天然就有大量并行度。本讲转向推理中最常见的「解码（decode）」场景——每一步只生成 **1 个新 token**，即 `q_seqlen == 1`。

学完本讲，你应当能够：

1. 说出解码场景与训练场景在**并行结构**上的根本差异，理解为什么解码必须引入 **split-kv**。
2. 掌握 `num_split` 的作用：它如何把一维的长 KV 序列切成多块、用 grid 的第三维换并行度。
3. 解释解码模板里 `final_rowscales`（如 `lse`）的全局形状为何多出一个 `num_split` 维度，以及多个 split 的部分结果如何被 **combine kernel** 合并。
4. 理解解码场景下 `block_mask` 的生成与施加方式，以及它与训练因果掩码优化的区别。

本讲聚焦 `lower_decode_gqa.py`（GQA 解码），并对照 `lower_decode.py`（MHA 解码），把「解码分发 → split-kv → final_rowscales 多 split 维度 → combine 合成」这条线讲透。

## 2. 前置知识

在进入本讲前，你需要先建立以下认知（来自前面几讲）：

- **AttentionEngine 的形状分发**（u3-l3）：引擎在构造时从 `qkv_meta` 抽取 `q_seqlen / kv_len / head / head_kv`，按互斥优先级分发到不同的 `lower_*` 函数。本讲专门看其中 `q_seqlen < kv_len` 的两条解码分支。
- **Online 算法与 final_rowscales**（u2-l6、u3-l2）：注意力用「逐 KV 块递推」的在线算法，跨块维护行最大值 `m`、指数和 `r`，循环结束后把 `lse = log(r) + m` 这类**行级标量**落盘到 `final_rowscales`，供反向复用。本讲的关键就是：解码里这个「落盘」多了一个 `num_split` 维度。
- **GQA 的 head 分组**（u4-l2）：`head > head_kv`，多个 Q 头共享同一组 K/V；kernel 里 `groups = heads // head_kv`，模板参数 `groups` 是「组大小」（每组几个 Q 头），与脚本里的 `groupnum`（KV 头数）互为倒数关系。
- **模板渲染**（u3-l1）：降级产出的字符串片段经 Jinja2 灌进带 `{{...}}` 占位符的骨架。本讲的 `attn_gqa_decode_tl.py` 是一份**较为静态、手工写好的** GQA 解码骨架，占位符比训练模板少——这一点我们会特别点出。

> 名词速查：
> - **decode（解码）**：自回归生成时，用历史的全部 KV（很长）去注意当前这 1 个新 Query。
> - **split-kv**：把 KV 序列沿序列方向切成 `num_split` 段，每段由一个独立 threadblock 处理。
> - **combine kernel**：把各 split 的部分输出与部分 LSE 合并成最终输出的第二个 kernel。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `attention_engine/attn_engine/attn_engine.py` | 引擎总入口，`_select_lower_template` 负责把解码形状分发到 `lower_decode_gqa` / `lower_decode`。 |
| `attention_engine/core/lower/lower_decode_gqa.py` | **GQA 解码降级**主体。定义 KV 分块的下标映射、`lower_online_func`（含多 split 的 `final_rowscales` 落盘）、`lower_tl` 主编排。 |
| `attention_engine/core/lower/lower_decode.py` | **MHA 解码降级**，与 GQA 版同构，但额外有 `lower_combine`（把用户 `combine` 方法降级）。 |
| `attention_engine/core/template/tl_template/attn/attn_gqa_decode_tl.py` | GQA 解码 TileLang 骨架：`flash_attn_split`（分块计算 + 落盘部分结果）、`combine`（合并多 split）、`main_split`/`main_no_split`。 |
| `attention_engine/core/template/tl_template/attn/attn_decode_tl.py` | MHA 解码骨架（对照用）。 |
| `attention_engine/core/transform/core.py` | `create_mask` / `create_block_mask`，用于生成解码掩码。 |
| `attn_script/gqa_inference.py` | GQA 解码示例脚本：`q_seqlen=1`、`OnlineSoftmax.combine`、`do_bench_attention(seqlenq=1, groupnum=G)`。 |

核心调用链（解码）：

```
AttentionEngine.__init__  (attn_engine.py)
   └─ _compile_tl
        └─ _select_lower_template      # 形状分发
             └─ lower_decode_gqa.lower_tl   # q_seqlen==1 且 head>head_kv
                  ├─ lower_custom_inputs
                  ├─ lower_online_func   # final_rowscales 多 num_split 维
                  ├─ lower_kernel
                  └─ TlAttnTemplate(attn_gqa_decode_tl.py)()  # 渲染
```

## 4. 核心概念与源码讲解

### 4.1 解码场景分发：什么时候走 decode

#### 4.1.1 概念说明

「解码」与「训练」的根本区别在于 **Query 序列长度**：

- **训练 / prefill**：`q_seqlen == kv_len`，Query 本身就是一长串，kernel 沿 Query 方向（`block_M`）切出很多块，天然并行。
- **解码**：`q_seqlen < kv_len`（典型 `q_seqlen == 1`），只有 1 个 Query，沿 Query 方向几乎没有并行度，必须另想办法把 GPU 喂饱。

引擎不要求用户显式声明「我在解码」，而是**根据 `qkv_meta` 的形状自动判断**。判断所用的四个量是：

| 量 | 来源 | 含义 |
| --- | --- | --- |
| `q_seqlen` | `qkv_meta[0].shape[2]` | Query 序列长 |
| `kv_len` | `qkv_meta[2].shape[2]` | KV 序列长 |
| `head` | `qkv_meta[0].shape[1]` | Query 头数 |
| `head_kv` | `qkv_meta[2].shape[1]` | KV 头数 |

#### 4.1.2 核心流程

`_select_lower_template` 用一组**互斥优先级**的 `if` 完成分发（先后顺序很重要）：

```text
if kv_shared:                          → lower_decode_mla      # MLA，下一讲
elif q_seqlen != kv_len and head > head_kv:   → lower_decode_gqa  # 本讲主角
elif q_seqlen != kv_len and head == head_kv:  → lower_decode      # MHA 解码
elif q_seqlen == kv_len and head == head_kv:  → lower             # 训练 MHA
elif q_seqlen == kv_len and head > head_kv:   → lower_gqa         # 训练 GQA
```

也就是说：**只要 `q_seqlen != kv_len`，就走解码路径**；再根据 `head` 与 `head_kv` 是否相等区分 GQA 解码与 MHA 解码。其中 GQA 解码分支还额外断言 `q_seqlen == 1`，并强制 `infer_mask = True`（解码掩码总是由引擎生成）。

#### 4.1.3 源码精读

分发逻辑位于引擎入口：

[attention_engine/attn_engine/attn_engine.py:252-271](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/attn_engine/attn_engine.py#L252-L271) —— **GQA 解码分发**。读这段代码时注意三件事：

1. 判定条件 `q_seqlen != kv_len and head > head_kv`：Query 头多于 KV 头，且 Query 比 KV 短，正是「GQA 解码」。
2. 两条断言 `assert q_seqlen < kv_len` 与 `assert q_seqlen == 1`：GQA 解码目前只支持单 token 解码。
3. 调用 `lower_tl_decode_gqa(...)` 时**实参顺序**：第 5、6 个位置分别是 `head`（Q 头数 → 形参 `headq`）和 `head_kv`（KV 头数 → 形参 `head`）。注意这里的**命名倒置**：降级函数里名叫 `head` 的形参，接的其实是 KV 头数（即组数 `groups`）。

紧邻其下是 MHA 解码分支：

[attention_engine/attn_engine/attn_engine.py:273-290](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/attn_engine/attn_engine.py#L273-L290) —— **MHA 解码分发**。它只要求 `q_seqlen != kv_len and head == head_kv`，**没有** `q_seqlen == 1` 的硬断言——也就是说 MHA 解码允许 `q_seqlen` 是一个较小的值（chunked prefill / 多 token 解码也能走这里）。

GQA 解码示例的形状声明在脚本里很直观：

[attn_script/gqa_inference.py:105-111](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attn_script/gqa_inference.py#L105-L111) —— `qkv_meta` 三元组。Query 形状 `(B, H, 1, D)`（第 3 维为 1），K/V 形状 `(B, G, S, D/DV)`，其中 `H=32 > G=8`、`S=8192`。把这组形状代入上面的判定，自然命中 GQA 解码分支。

#### 4.1.4 代码实践

1. **实践目标**：通过手工代入形状，验证分发逻辑。
2. **操作步骤**：
   - 打开 `attn_engine.py` 的 `_select_lower_template`，对照 `gqa_inference.py` 的 `B,H,G,S,D,DV = 1,32,8,8192,128,128`。
   - 依次回答：`kv_shared`？`q_seqlen`(=1) 与 `kv_len`(=8192) 是否相等？`head`(=32) 与 `head_kv`(=8) 谁大？
3. **观察现象**：应命中 `q_seqlen != kv_len and head > head_kv`，进入 `lower_decode_gqa`。
4. **预期结果**：分发到 `lower_tl_decode_gqa`，返回 `(tl_code, block_mask)`，其中 `block_mask` 非 `None`（见 4.4）。
5. 把 `gqa_inference.py` 里的 `G` 从 8 改成 32（令 `head == head_kv`），重新推断：应改为命中 **MHA 解码** `lower_decode`。（此项为「待本地验证」——你只需口述推断结果即可。）

#### 4.1.5 小练习与答案

**练习 1**：如果用户传入 `q_seqlen == kv_len` 但 `head > head_kv`，会走哪条路径？  
**答案**：会跳过两个解码分支（因 `q_seqlen != kv_len` 不成立），命中最后的「训练 GQA」`lower_gqa`（即上一讲 u4-l2 的内容）。

**练习 2**：为什么 GQA 解码分支要单独 `assert q_seqlen == 1`，而 MHA 解码不强制？  
**答案**：GQA 解码模板 `attn_gqa_decode_tl.py` 的 grid 与索引（`block_H` 切头、单 token）是针对 `q_seqlen == 1` 硬写的；MHA 解码模板沿用了 `block_M` 切 Query 的结构（grid 第一维 `ceildiv(seq_len, block_M)`），允许 `q_seqlen` 为不大于 `kv_len` 的任意值。

---

### 4.2 split-kv 与 num_split：把长 KV 切成多块并行

#### 4.2.1 概念说明

解码时只有 1 个 Query，沿 Query 切块得不到并行度。如果让单个 threadblock 串行扫完整条 KV 序列（可能上千上万 token），会有两个问题：

- **并行度不足**：grid 只有 `batch × heads` 个块，远不足以填满 GPU 的上百个 SM。
- **串行过长**：KV 循环迭代次数很多，单块耗时高。

**split-kv** 的思路是：把一维的 KV 序列沿序列方向均匀切成 `num_split` 段，**每段交给一个独立的 threadblock**。这样 grid 多出一个维度（`num_split`），并行度立刻乘以 `num_split`。代价是：每个 split 只看到一部分 KV，算出的是**部分**注意力输出和**部分** LSE，需要一个后续的 combine kernel 把它们合并（见 4.3）。

> 类比：训练时是「很多人（Q 块）各扫完整条街（KV）」；解码时是「一条街被切成几段，每段派一个人扫，最后把各自买的东西合并」。

#### 4.2.2 核心流程

设 KV 序列长 `L`、切 `P = num_split` 段、每块 `block_N` 个 token：

```text
第 sid 个 split（sid ∈ [0, P)）负责的 KV 区间：
    start = (L // P) * sid
    end   = (L // P) * (sid + 1)
    该 split 内部仍按 block_N 循环：
        for k in range(ceildiv(L // P, block_N)):
            token 区间 = [start + k*block_N, start + (k+1)*block_N)
```

GQA 解码的 grid 三维分别是：

```text
T.Kernel(batch, heads // valid_block_H, num_split) as (bx, by, bz)
   bx = bid            （batch 维）
   by = hid            （Q 头按 block_H 分组——因为单 token，改为「切头」换并行）
   bz = sid            （split 维 ← split-kv 的并行来源）
```

注意 GQA 解码把「切 Query」换成了「**切 Query 头**」（`block_H` 个头一组），因为 Query 只有 1 个 token，没有 Q 序列可切；而把 KV 序列切成 `num_split` 段提供主要并行度。

#### 4.2.3 源码精读

**① KV 分块下标映射**——这是 split-kv 在降级层的数学表达：

[attention_engine/core/lower/lower_decode_gqa.py:25-32](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/lower/lower_decode_gqa.py#L25-L32) —— `shape_idx_map`。关键一行是 `seq_len_kv` 的映射：

```python
"seq_len_kv": "(seq_len_kv // num_split) * sid + k * block_N : (seq_len_kv // num_split) * sid + (k + 1) * block_N",
```

它精确表达了「第 `sid` 个 split 内、第 `k` 个 block_N 块」对应的 KV 全局下标区间。对比 MHA 解码里的同一映射（[lower_decode.py:24-31](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/lower/lower_decode.py#L24-L31)），形式完全一致——split-kv 的下标语义在两个解码降级里是共享的。

**② 模板里的 grid 与 split 循环**：

[attention_engine/core/template/tl_template/attn/attn_gqa_decode_tl.py:141-181](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/template/tl_template/attn/attn_gqa_decode_tl.py#L141-L181) —— `flash_attn_split` 宏。看三处：

- 第 150-151 行：`T.Kernel(batch, heads // valid_block_H, num_split, ...) as (bx, by, bz)`，`bz = sid` 就是 split 维。
- 第 176 行：`loop_range = T.ceildiv((seqlen_kv // num_split), block_N)`——每个 split 只扫 `seqlen_kv // num_split` 个 token。
- 第 178-184 行：加载 K 与 mask 时，下标写成 `(seqlen_kv // num_split) * sid + k*block_N : ...`，与上面的 `shape_idx_map` 完全对应。

**③ num_split 是可调参数**：

[attention_engine/core/template/tl_template/attn/attn_gqa_decode_tl.py:19-34](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/template/tl_template/attn/attn_gqa_decode_tl.py#L19-L34) —— `get_configs()` 把 `num_split` 列入 autotuner 搜索空间 `[2, 4, 8]`。说明 split 数并非写死，而是随形状/硬件调优的。

> 小贴士：注意 `attn_gqa_decode_tl.py` 是一份**手工写好的静态骨架**——它的 online softmax、scale、combine 逻辑都是直接写死的 `T.exp2(...)`，并不依赖降级产出的 `{{online_func_def}}` 等占位符（这些占位符在该模板里被注释掉了）。换句话说，GQA 解码目前更像一份「标准 softmax 解码参考实现」，降级层产出的 online_func 字段在这里并未全部注入。这一点和 MHA 解码（`attn_decode_tl.py`，真用 `{{online_func_def}}`）不同，阅读时要留意。

#### 4.2.4 代码实践

1. **实践目标**：理解 `num_split` 如何改变每个 split 的工作量。
2. **操作步骤**：在 `attn_gqa_decode_tl.py` 的 `flash_attn_split` 中，定位 `loop_range = T.ceildiv((seqlen_kv // num_split), block_N)`。
3. **观察现象**：以 `seqlen_kv=8192`、`block_N=64` 为例，计算 `num_split=1/2/4/8` 时单个 split 的循环次数。
4. **预期结果**：分别为 `128 / 64 / 32 / 16` 次。`num_split` 翻倍，单 split 迭代数减半，但总 split 数翻倍——并行度与合并开销同时上升。
5. **待本地验证**：若环境可用，运行 `gqa_inference.py` 并尝试不同 `num_split`（需走 tune 路径或手改 `cached` 调用），观察延迟变化。

#### 4.2.5 小练习与答案

**练习 1**：split-kv 为什么**不能**直接把各 split 的 `acc_o` 相加作为最终输出？  
**答案**：各 split 用的是**各自的局部最大值 `m_s`** 做 online softmax 归一化，尺度不一致；必须先用各 split 的 `lse` 做全局重缩放（combine），才能相加。

**练习 2**：GQA 解码的 grid 第一维为什么是 `heads // valid_block_H` 而不是 `q_seqlen // block_M`？  
**答案**：因为 `q_seqlen == 1`，沿 Query 序列切块没有意义；于是改为沿「Query 头」方向（`block_H` 个头一组）切，用头并行度弥补 Query 并行度的缺失。

---

### 4.3 多 split 维度的 final_rowscales 与 combine 合并

这是本讲的核心，也是本讲的实践任务所在。

#### 4.3.1 概念说明

回忆 online softmax：对单个（行）Query，跨所有 KV 块递推得到全局行最大值 `m`、指数和 `r`，最终 `lse = log(r) + m`，输出 `o = acc_o / r`。训练时一个 threadblock 独占整条 KV，所以 `lse` 落盘形状是 `[batch, heads, seq_len]`——每个 Query 行一个标量。

split-kv 打破了「一个块独占整条 KV」的前提：第 `sid` 个 split 只扫一段 KV，算出的只是**这段的**局部 `m_s`、`r_s`、`acc_o_s`。要把它们合并回全局结果，每个 split 必须把自己的局部 `lse_s` 先存下来。于是落盘张量多出一个 `num_split` 维度：

```text
训练：     final_rowscales 形状 = [batch, heads, seq_len]
解码：     final_rowscales 形状 = [batch, heads, num_split, seq_len]
                                       ↑↑↑↑↑↑↑↑↑↑ 多出来的 split 维
```

合并（combine）就是把 `num_split` 个局部结果沿 split 维归约掉。

#### 4.3.2 核心流程

设共 `P` 个 split，第 `s` 个 split 的局部行最大值 `m_s`、局部指数和 `r_s`、局部未归一化累加 `acc_o_s`。合并 online softmax 的正确公式为：

\[
m^\* = \max_{s} m_s
\]

\[
r^\* = \sum_{s} r_s \cdot \exp(m_s - m^\*)
\]

\[
\mathrm{lse}^\* = \log r^\* + m^\*
\]

\[
o^\* = \frac{\sum_{s} \exp(m_s - m^\*) \cdot acc_o_s}{r^\*}
\]

即：先用各 split 的 `lse` 求全局最大，再把每个 split 的输出按 \(\exp(lse_s - lse^\*)\) 重缩放后求和。模板里存的是 log2 域的 `lse`，故用 `exp2` 替代 `exp`（与 u2-l4 的换底技巧一致）。

> 用户侧的等价描述见 `gqa_inference.py` 的 `OnlineSoftmax.combine`：`lse_max → exp → sum → log → o_scale = exp(lse - lse_sum)`，与本节公式一一对应。

合并分两步：

1. **flash_attn_split kernel**：每个 split 算完自己的 KV 段，把 `lse_s` 写入 `glse[batch, heads, num_split]`，把未归一化的部分输出写入 `Output_partial[batch, heads, num_split, dimv]`。
2. **combine kernel**：读 `glse` 与 `Output_partial`，按上式归约，写出最终 `Output[batch, heads, dimv]`。

#### 4.3.3 源码精读

**① final_rowscales 落盘时多出 num_split 维——这是本讲实践任务的答案所在**：

[attention_engine/core/lower/lower_decode_gqa.py:251-263](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/lower/lower_decode_gqa.py#L251-L263) —— 逐项看：

```python
kernel_options.add_output_tensor(
    v.varname, v.shape, False,
    ["batch", "heads", "num_split", "seq_len"],   # ← 全局形状多了 num_split
    v.dtype,
    f"g_{k}",
    [sp.simplify(ii) for ii in ["bid", "hid", "sid", "mid * block_M"]],  # 全局下标
    [3,]   # global_dim_map：仅最后一维(seq_len)被 tile
)
torch_alloc_final_rowscales += f"g_{k} = torch.empty([BATCH, H, num_split, N_CTXQ], dtype=torch.float, device=q.device)\n"
```

三个要点：

- **全局形状**是逻辑名 `["batch", "heads", "num_split", "seq_len"]`，对应实际分配的 `[BATCH, H, num_split, N_CTXQ]`——这就是实践任务问的那个形状。
- **全局下标** `[bid, hid, sid, mid*block_M]`：`sid` 正是 split 维索引，说明「第 `sid` 个 split 写入自己的那一页」。
- **`global_dim_map=[3]`**：只有第 3 维（`seq_len`）是「分块写入」的循环维（由 `mid` 驱动），`batch/heads/num_split` 三维都是直接的网格索引。

**② 对照训练的 final_rowscales（没有 num_split 维）**：

[attention_engine/core/lower/lower.py:386-391](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/lower/lower.py#L386-L391) —— 训练路径里同一个落盘调用，全局形状只有 `["batch", "heads", "seq_len"]`，下标里也没有 `sid`。这一对照清晰说明了：**`num_split` 维是 split-kv 专有的**。

**③ combine kernel 的合并实现**：

[attention_engine/core/template/tl_template/attn/attn_gqa_decode_tl.py:224-266](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/template/tl_template/attn/attn_gqa_decode_tl.py#L224-L266) —— `combine` 宏，逐段对应本节公式：

- 第 253 行 `T.reduce_max(lse_local, lse_max_local, dim=0, clear=True)`：沿 split 维求 \(m^\*\)。
- 第 254-256 行 `lse_logsum_local += T.exp2(lse_local_split - lse_max_local)`：累加 \(\exp(lse_s - m^\*)\)，得 \(r^\*\)（log2 域）。
- 第 257 行 `lse_logsum_local = T.log2(...) + lse_max_local`：得全局 \(\mathrm{lse}^\*\)。
- 第 258-264 行：对每个 split 计算 `scale = exp2(lse_s - lse*)` 并加权累加 `o_accum += po * scale`，即 \(o^\*\) 的分子；因每个 split 的 `Output_partial` 已是「未归一化、按各自 `m_s` 缩放」的部分和，加权后即为最终输出。

**④ combine 的入口与输出索引**：

[attention_engine/core/template/tl_template/attn/attn_gqa_decode_tl.py:268-296](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/template/tl_template/attn/attn_gqa_decode_tl.py#L268-L296) —— `main_split` 串联 `flash_attn_split` 与 `combine`；当 `num_split == 1` 时退化成 `main_no_split`（无需合并，单 kernel 直出）。`cached(program, [6], ...)` 中的 `[6]` 表示第 6 号缓冲（`Output`）才是最终要取回的输出，中间的 `glse`/`Output_partial` 只是临时量。

**⑤ 宿主侧分配**：

[attention_engine/core/template/tl_template/attn/attn_gqa_decode_tl.py:338-349](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/template/tl_template/attn/attn_gqa_decode_tl.py#L338-L349) —— `_attention.forward`。注意第 346 行 `{{torch_alloc_final_rowscales | indent(8)}}` 正好把 4.3.3① 里生成的 `g_lse = torch.empty([BATCH, H, num_split, N_CTXQ], ...)` 注入此处；第 348 行 `{{final_rowscales_list}}` 把 `g_lse,` 拼进 kernel 调用实参。

> 补充（MHA 解码的对照）：MHA 解码 `lower_decode.py` 不走静态模板，而是用 `lower_combine` 把用户写的 `combine` 方法真正降级成 combine kernel 代码（见 [lower_decode.py:287-313](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/lower/lower_decode.py#L287-L313)）。GQA 解码则把 combine 直接写死在模板里。两者解决的是同一个问题，工程化程度不同。

#### 4.3.4 代码实践（本讲主实践）

1. **实践目标**：跟读 `lower_decode_gqa`，说清 `final_rowscales` 全局形状为何是 `[batch, heads, num_split, seq_len]`，以及多 split 如何合并。
2. **操作步骤**：
   - 打开 [lower_decode_gqa.py:251-263](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/lower/lower_decode_gqa.py#L251-L263)，确认 `add_output_tensor` 的 `global_shape` 第 3 维是 `"num_split"`。
   - 打开 [attn_gqa_decode_tl.py:224-266](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/template/tl_template/attn/attn_gqa_decode_tl.py#L224-L266)，把每行代码对应到 4.3.2 的四条公式。
   - 对照 [gqa_inference.py:73-81](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attn_script/gqa_inference.py#L73-L81) 的 `OnlineSoftmax.combine`，确认用户侧描述与 kernel 实现一致。
3. **观察现象 / 需要回答的问题**：
   - 为什么多出 `num_split` 维？→ 因为每个 split 只扫一段 KV，必须各自落盘局部 `lse`，combine 才能按正确尺度合并。
   - 多 split 如何合并？→ `lse_max → exp2(lse - lse_max) 求和 → log2 + lse_max 得全局 lse → 每个 split 按 exp2(lse_s - lse*) 加权累加部分输出`。
4. **预期结果**：能用一句话回答——「`num_split` 维用于存放每个 split 的局部 LSE，combine kernel 沿该维做 online softmax 式的归约合并」。
5. **待本地验证**：在 `gqa_inference.py` 中给 `OnlineSoftmax` 的 `combine` 加日志或在 combine kernel 处设断点，观察 `glse` 形状确实是 `[B, H, num_split]`。

#### 4.3.5 小练习与答案

**练习 1**：如果把 `num_split` 设为 1，`final_rowscales` 的 `[batch, heads, num_split, seq_len]` 会退化成什么？还需要 combine kernel 吗？  
**答案**：`num_split=1` 时 split 维大小为 1，等价于不切；落盘张量实际只有一页有效数据。模板里 `num_split > 1` 才走 `main_split`，否则走 `main_no_split`（见第 293-296 行），**不需要** combine kernel。

**练习 2**：combine 公式里为什么必须先求 `lse_max` 再做差，而不是直接把各 split 的 `exp(lse_s)` 相加？  
**答案**：直接 `exp(lse_s)` 会因 `lse_s` 较大而数值溢出；先减去 `lse_max` 把指数控制在 ≤ 0，保证 `exp2(lse_s - lse_max) ∈ (0, 1]`，这是 online softmax 数值稳定的关键技巧。

---

### 4.4 decode 的 block_mask 处理

#### 4.4.1 概念说明

解码场景的掩码有两个特点：

1. **Query 只有 1 行**，不存在训练里「下三角因果」那种「按 Q 块整块跳过」的块级稀疏结构——掩码退化为「沿 KV 序列的一维布尔向量」。
2. 解码通常是**非因果**的（要 attend 到全部历史 KV），所以 `gqa_inference.py` 里 `mask_mod=None`、`causal=False`。

因此解码模板对掩码的处理比训练「朴素」：生成一份**稠密的 per-KV-token** 掩码，在 KV 循环内逐 token 用 `T.if_then_else` 施加。

#### 4.4.2 核心流程

```text
lower_decode_gqa.lower_tl(block_mask, ...):
   if block_mask is not None:                              # 用户给了 mask_mod
       block_mask = create_mask(mask_mod, Batch, head,
                                Q_LEN=1, seqlenkv, ...)     # 生成 (B, H, 1, seqlenkv) 的掩码
   else:                                                    # 无掩码
       block_mask = torch.ones((Batch, head, 1, seqlenkv), uint8)  # 全 1
   # 返回 (template, block_mask)

运行期 __call__：把 block_mask 作为额外实参喂给 kernel
kernel 内（flash_attn_split）：每个 KV 块加载 mask_local[block_N]
   acc_s[i,j] = T.if_then_else(mask_local[j]!=0, acc_s[i,j], -inf)
```

注意：解码里调用的是 `create_mask`（生成**稠密**掩码张量），而不是训练 causal 路径里的 `create_block_mask`（块级稀疏）。

#### 4.4.3 源码精读

**① 掩码生成**：

[attention_engine/core/lower/lower_decode_gqa.py:390-396](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/lower/lower_decode_gqa.py#L390-L396) —— 解码掩码分支。两点：

- 用 `create_mask(..., 1, seqlenkv, ...)`——`QLen=1`，所以产物形状是 `(B, H, 1, seqlenkv)`。
- `block_mask is None` 时退化为 `torch.ones(..., uint8)`——即「全开放」，不丢弃任何 KV。

`create_mask` 的实现是用四层 `torch.vmap` 把标量 `mask_mod(b,h,q_idx,kv_idx)` 真正跑成完整布尔张量：

[attention_engine/core/transform/core.py:352-398](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/transform/core.py#L352-L398) —— 注意它产出的 `mask` 形状是 `(B, H, Q_LEN, KV_LEN)`，解码下 `Q_LEN=1`。

**② kernel 内施加掩码**：

[attention_engine/core/template/tl_template/attn/attn_gqa_decode_tl.py:182-194](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/template/tl_template/attn/attn_gqa_decode_tl.py#L182-L194) —— `flash_attn_split` 内：

```python
T.copy(mask[bid, cur_kv_head, 0, (seqlen_kv//num_split)*sid + ...], mask_local)
...
for i, j in T.Parallel(block_H, block_N):
    acc_s[i, j] = T.if_then_else(mask_local[j] != 0, acc_s[i, j], -T.infinity(accum_dtype))
```

掩码是按 **KV token**（`mask_local[j]`，`j` 沿 `block_N`）施加的——这与「Query 只有 1 行」一致：掩码本质上是一维的。同时注意，加载掩码时也带上了 split 偏移 `(seqlen_kv//num_split)*sid + k*block_N`，确保每个 split 只读自己那段的掩码。

**③ 运行期注入**：

[attention_engine/attn_engine/attn_engine.py:388-395](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/attn_engine/attn_engine.py#L388-L395) —— `__call__`：当 `self.block_mask is not None` 时，把它追加到实参末尾 `self.attention(*args, self.block_mask)`。解码分支 `infer_mask=True`，故 `self.block_mask` 在构造期已被设为上面生成的稠密掩码。

#### 4.4.4 代码实践

1. **实践目标**：观察解码掩码的形状与施加粒度。
2. **操作步骤**：在 `lower_decode_gqa.py:396` 处看到 `torch.ones((Batch, head, 1, seqlenkv), uint8)`；在模板第 192-194 行看到逐 token 的 `T.if_then_else`。
3. **观察现象**：掩码第 2 维（Query 维）大小为 1；施加时只索引 `mask_local[j]`（KV 方向）。
4. **预期结果**：解码掩码是一维稠密 uint8 向量（按 KV token），与训练的二维块级稀疏掩码（`create_block_mask`）形成对照。
5. **待本地验证**：给 `gqa_inference.py` 传一个非平凡 `mask_mod`（例如「只 attend 最近若干 token」的滑动窗口），观察 `create_mask` 产出的 `(B,H,1,S)` 张量中 `True/False` 的分布。

#### 4.4.5 小练习与答案

**练习 1**：解码为什么不用 `create_block_mask`（块级稀疏）？  
**答案**：块级稀疏的意义在于「整块跳过」，需要 Query 方向有多个块可跳；解码 `q_seqlen=1`，掩码沿 Query 维退化为一行，没有块级跳过的收益，故直接用稠密 `create_mask` + 逐 token `if_then_else`。

**练习 2**：`mask_mod=None` 时为什么还要生成一个全 1 的 `block_mask`？  
**答案**：因为解码模板的 kernel 签名固定含 `mask` 形参，且 `__call__` 在 `self.block_mask is not None` 时会把它追加进实参；生成全 1 掩码可让「无掩码」也走同一条 kernel 路径，`if_then_else` 对全 1 等价于不遮蔽。

---

## 5. 综合实践

把本讲四条线索串起来，完成一次「解码形状 → kernel 结构」的完整推断。

**任务**：给定 `gqa_inference.py` 的形状 `B=1, H=32, G=8, S=8192, D=DV=128, q_seqlen=1`，回答下列问题并相互印证：

1. **分发**：代入 `_select_lower_template`，确认走 `lower_decode_gqa`。
2. **并行结构**：写出 grid 三维各是什么（`batch / heads//block_H / num_split`），说明哪一维提供主要并行度（`num_split`）。
3. **KV 切分**：若 `num_split=8`、`block_N=64`，每个 split 扫多少个 token、循环多少次？（答：`8192//8=1024` 个 token，`1024//64=16` 次。）
4. **落盘形状**：`lse` 的全局张量 `g_lse` 形状是什么？为什么？（答：`[1, 32, 8, 1]`，即 `[BATCH, H, num_split, N_CTXQ=1]`——多出 `num_split` 维，因为每个 split 各存一份局部 LSE。）
5. **合并**：复述 combine kernel 的四步（`lse_max → exp2 差求和 → log2+max 得全局 lse → 按 exp2(lse_s-lse*) 加权累加`）。
6. **掩码**：`mask_mod=None` 时 kernel 收到的 mask 是什么形状、怎么施加？（答：`(1, 32, 1, 8192)` 的 uint8 全 1 张量，逐 KV token `if_then_else`。）

进阶：把 `G` 改为 32（令 `head == head_kv`），重新走一遍上述 6 步——这次应进入 **MHA 解码** `lower_decode.py`，并注意它的 grid 第一维变成 `ceildiv(seq_len, block_M)`、combine 由 `lower_combine` 真正降级（而非静态模板）。

## 6. 本讲小结

- **解码的本质特征**是 `q_seqlen < kv_len`（GQA 解码更断言 `q_seqlen == 1`）；引擎按 `qkv_meta` 形状自动分发到 `lower_decode_gqa`（`head>head_kv`）或 `lower_decode`（`head==head_kv`）。
- 解码沿 Query 方向没有并行度，故引入 **split-kv**：把 KV 序列切成 `num_split` 段，用 grid 的第三维 `sid` 换并行度；GQA 解码则额外用「切 Query 头」（`block_H`）补充并行。
- 因为每个 split 只扫一段 KV，`final_rowscales`（`lse`）必须**各自落盘**，全局形状从训练的 `[batch, heads, seq_len]` 变为 **`[batch, heads, num_split, seq_len]`**——多出的 `num_split` 维是 split-kv 的直接产物。
- **combine kernel** 沿 split 维做 online softmax 式归约：`lse_max → exp2 差求和 → 全局 lse → 加权累加部分输出`，把多个 split 合并成最终输出；`num_split==1` 时退化为单 kernel。
- 解码掩码因 `q_seqlen=1` 退化为一维稠密向量，用 `create_mask`（非 `create_block_mask`）生成，在 KV 循环内逐 token `if_then_else` 施加。
- GQA 解码模板 `attn_gqa_decode_tl.py` 偏「静态手写」，combine 与 softmax 直接写死；MHA 解码 `attn_decode_tl.py` 则真用降级占位符并把用户 `combine` 经 `lower_combine` 降级——两者同构、工程化程度不同。

## 7. 下一步学习建议

- **下一讲 u4-l4**：MLA 解码与 combine kernel。MLA 是 `kv_shared` 路径（`lower_decode_mla`），同样用 split-kv 与 combine，但结构更复杂（PE 维拆分、paged-kv）。本讲掌握的「`num_split` 维 + combine 合并」正是理解 MLA combine 的基础。
- **横向巩固**：重读 u3-l3 的 `_select_lower_template` 全部 5 条分支，把本讲的 2 条解码分支与训练分支（u4-l2）对照，形成完整的「形状 → 降级函数」速查表。
- **源码延伸**：阅读 `attn_decode_tl.py`（MHA 解码模板），对比它与 `attn_gqa_decode_tl.py` 的 grid 第一维（`ceildiv(seq_len, block_M)` vs `heads//block_H`），体会「单 token 解码」与「多 token 解码」在并行结构上的取舍。
- **调优延伸**（可结合 u5-l3）：`num_split` 是 autotuner 搜索空间 `[2,4,8]` 的一员，思考「`num_split` 越大，并行度越高但 combine 开销与 `Output_partial` 显存也越大」这一折中如何随 `seqlen_kv` 变化。
