# 前向特性：GQA / attn_bias / causal / dropout 实现

## 1. 本讲目标

本讲承接 u4-l1（Triton 前向 generic 主循环与 online softmax）。u4-l1 讲清了「主循环骨架」——KV 块循环、Split-D、`m_i/l_i/o_accs` 的在线重缩放。但真实的注意力还有四个「横切特性」必须叠加在这个骨架上，它们正是本讲的四个最小模块：

1. **GQA/MQA 头映射**：多个 Query 头共用一组 K/V，靠 `off_hkv = off_hq // group_size` 在 kernel 内换算，零拷贝。
2. **attn_bias 零 stride 广播**：紧凑掩码 `[1,1,1,Nkv]` 永不物化成 `[B,H,Nq,Nkv]`，靠 stride=0 表达广播。
3. **causal 尾部对齐掩码**：`offs_kv <= offs_m + (Nkv - Nq)`，零开销、不物化掩码矩阵。
4. **dropout Philox 重放**：每个逻辑 score 元素消耗一个 Philox 输出，与 SDPA 逐位对齐以便反向重放。

学完本讲，你应当能够：

- 在源码里**精确定位**这四段代码并说出每行的作用；
- 解释为什么 `[1,1,1,Nkv]` 的紧凑偏置不需要展开成完整 `[B,H,Nq,Nkv]`；
- 理解 causal 的「query 对齐 KV 尾部」约定在 kernel 里就是一行 `tl.where`；
- 看懂 dropout 的「逐元素线性偏移」如何与 PyTorch SDPA 的 RNG 布局对齐。

## 2. 前置知识

本讲默认你已经掌握 u4-l1 的内容：Triton kernel 的二维网格（`program_id(0)`=Q 行块、`program_id(1)`=batch×query 头拍扁索引 `off_hb`）、KV 块主循环、online softmax 的 `m_i/l_i/alpha` 更新。同时需要 u2-l3（attn_mask 两种语义）、u2-l4（GQA/MQA 的 `group_size` 概念）的背景。

下面三个概念是本讲的「通用语言」，先一次性讲清：

- **逻辑 score 张量**：注意力分数的逻辑形状恒为 `[B, Hq, Nq, Nkv]`（batch、query 头、query 行、key 列）。本讲的四个特性都围绕「如何在 kernel 内只算该形状的一个 `[BLOCK_M, BLOCK_N]` 切片、却复用一份紧凑输入」展开。
- **stride（步长）**：多维张量里，沿第 `i` 维移动一个元素需要在底层一维缓冲里前进的元素数。**stride=0 意味着「沿这一维移动指针不动」**——这就是广播（broadcast）的本质。本讲 4.2 节的核心技巧就是用 stride=0 表达广播。
- **Philox / `philox_offset`**：Philox 是一个计数器式伪随机数发生器，给定 `(seed, counter)` 产出确定的 4 个 uint32。`philox_offset` 是一个**基地址偏移**，每次注意力调用整体推进，保证不同调用、不同 batch/头/位置的随机数互不重叠。FFPA 完全复刻 SDPA 的 Philox 布局，反向才能重放出完全相同的 dropout 掩码（见 u3-l4 的 `_reserve_large_d_dropout_rng`）。

## 3. 本讲源码地图

本讲只涉及两个文件，主角是前向 kernel 文件：

| 文件 | 作用 |
| --- | --- |
| `src/ffpa_attn/triton/_ffpa_fwd.py` | Triton 前向 kernel 全部实现：generic 路径 `_ffpa_fwd_kernel_impl`、decode 两阶段、宿主启动器，以及本讲的四个特性（GQA 指针换算、`_attn_bias_broadcast_strides`、causal `tl.where`、`_curand_uniform_from_element_offset` 与 `_apply_dropout_to_p`）。 |
| `src/ffpa_attn/triton/__init__.py` | 注册 `torch.ops.ffpa_attn._fwd_triton` 算子，把 `attn_bias/dropout/causal` 等编码为 int 传入；定义 `_attn_bias_grad_*`、`_triton_bwd_grad_tensor_like` 等反向辅助（本讲只读其前向 op 注册部分）。 |

辅助参考（实践环节会用到）：`tests/test_ffpa_fwd.py` 里有 `test_ffpa_attn_func_triton_additive_attn_mask_matches_sdpa`、`test_ffpa_attn_func_triton_bool_attn_mask_matches_sdpa` 两个现成的掩码正确性测试。

## 4. 核心概念与源码讲解

### 4.1 GQA/MQA 头映射：`off_hkv = off_hq // group_size`

#### 4.1.1 概念说明

GQA（Grouped-Query Attention）让多个 Query 头共用同一组 K/V，MQA 是 `Nh_kv == 1` 的极端特例（见 u2-l4）。朴素做法是在调用前用 `repeat_interleave` 把 K/V 复制成 `[B, Nh_q, Nkv, D]`，但这会让 K/V 的显存与算力翻 `group_size` 倍。

FFPA 的做法是**根本不复制**：kernel 内部根据「连续分组」约定 `h_kv = h_q // group_size`，用**指针偏移**直接让一组 Query 头读同一个 KV 头。例如 `Nh_q=32, Nh_kv=8`，`group_size=4`：Query 头 `0,1,2,3` 都换算出 `off_hkv=0`，去读 0 号 KV 头；Query 头 `4,5,6,7` 读 1 号 KV 头……这样 K/V 张量始终维持紧凑的 `[B, Nh_kv, Nkv, D]`，零额外显存。

#### 4.1.2 核心流程

一个 program（线程块）拿到自己的 `off_hb`（batch×query 头拍扁索引）后，做三步换算：

```
off_b   = off_hb // nheads_q        # 第几个 batch
off_hq  = off_hb %  nheads_q        # 第几个 query 头
group_size = nheads_q // nheads_kv  # 多少个 query 头共用一个 KV 头
off_hkv = off_hq // group_size      # 对应的 KV 头编号
```

随后**三组指针用不同的头索引**：

- `Q` 用 `off_hq`（每个 query 头有独立 Q）；
- `K` / `V` 用 `off_hkv`（多个 query 头共享 KV）；
- `O` / `LSE` 用 `off_hq`（输出按 query 头写）。

注意 `group_size` 必须整除——这是 u2-l4 `normalize_inputs` 校验的 `Nh_q % Nh_kv == 0`，在进 kernel 之前已经保证。

#### 4.1.3 源码精读

generic kernel 的网格与 GQA 换算（注释明确点出「query 头映射回所属 KV 头」）：

[文件路径:L363-L374](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_ffpa_fwd.py#L363-L374) —— `off_b/off_hq` 由 `off_hb` 拆出，`group_size = nheads_q // nheads_kv`、`off_hkv = off_hq // group_size` 完成 GQA 头映射；随后 `Q/O` 用 `off_hq`、`K/V` 用 `off_hkv` 做指针偏移。

decode stage1 kernel 完全套用同一套约定（说明 GQA 映射在两条路径上是一致的契约）：

[文件路径:L559-L566](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_ffpa_fwd.py#L559-L566) —— decode 路径里同样的 `group_size`、`off_hkv` 换算与 `K/V` 指针偏移，证明 GQA 不依赖具体路径。

#### 4.1.4 代码实践

1. **目标**：确认 GQA 映射确实是「连续分组」而非平铺。
2. **操作步骤**：
   - 打开 `_ffpa_fwd.py` 定位 4.1.3 的两段代码，抄下 `off_hkv` 公式。
   - 写一段 Python，模拟 `Nh_q=32, Nh_kv=8`，打印每个 `off_hq` 对应的 `off_hkv`。
3. **观察现象**：
   ```python
   nheads_q, nheads_kv = 32, 8
   gs = nheads_q // nheads_kv
   for hq in range(nheads_q):
       print(hq, "->", hq // gs)
   ```
   应看到 `0,1,2,3 -> 0`；`4,5,6,7 -> 1`……连续 4 个 query 头共享一个 KV 头。
4. **预期结果**：映射是连续分组。**对比陷阱**：如果用 `repeat`（整体平铺）而非 `repeat_interleave`（连续复制）来构造参考答案，头排列会交错，逐头对比会错位——这正是 u2-l4 强调的「必须用 `repeat_interleave`」的原因。

#### 4.1.5 小练习与答案

- **练习 1**：MQA（`Nh_kv=1`）时 `off_hkv` 恒为多少？为什么此时根本不需要 `nheads_kv` 这个维度？
  - **答**：`group_size = Nh_q // 1 = Nh_q`，任意 `off_hq // Nh_q == 0`，所以所有 query 头都读 0 号（唯一的）KV 头；K/V 张量在头维上恒为 1，可视为 `[B, 1, Nkv, D]`。
- **练习 2**：若误用 `off_hkv = off_hq % group_size` 而非 `//`，会发生什么？
  - **答**：会变成「头内循环」映射（0,1,2,3 反复映射到 0,1,2,3），query 头读错 KV 头，输出在头维上整体错乱；正确性测试会立即失败。

---

### 4.2 attn_bias 零 stride 广播：紧凑掩码免物化

#### 4.2.1 概念说明

`attn_bias` 是叠加在 score 上的可加偏置（u2-l3 已讲它由布尔/可加两种掩码归一化而来），逻辑形状为 `[B, Hq, Nq, Nkv]`。但用户常只给一个紧凑掩码，例如 ALiBi 风格的 `[1, 1, 1, Nkv]`——同一个长度 `Nkv` 的偏置向量对所有 batch、所有头、所有 query 行都一样。

朴素实现会把它 `expand` 成 `[B, Hq, Nq, Nkv]` 再传给 kernel，白白占用 `B·Hq·Nq·Nkv` 个元素的显存与带宽。FFPA 的做法是**保留紧凑形状，用 stride=0 表达广播维度**：沿广播维移动指针时步长为 0，所有元素读到同一个值，于是 `[1,1,1,Nkv]` 始终维持原大小，永不物化。

#### 4.2.2 核心流程

宿主侧先用 `_attn_bias_broadcast_strides` 把 4 维 stride 算出来，规则是「**该维大小为 1 且逻辑大小大于 1 → stride 置 0**」：

```
对每一维 d in {B, H, M(=Nq), N(=Nkv)}:
    if attn_bias.size(d) == 1 and logical_size(d) > 1:
        stride(d) = 0          # 广播：移动指针不动
    else:
        stride(d) = attn_bias.stride(d)   # 正常步长
```

kernel 内部则**始终用完整的 4 维逻辑索引公式**寻址，不区分紧凑还是完整：

```
addr = AttnBias_base
      + off_b   * stride_bb   # batch 维
      + off_hq  * stride_bh   # head 维
      + offs_m  * stride_bm   # query 行维
      + offs_kv * stride_bn   # key 列维
```

广播维的 stride 为 0，对应项自然失效，从而所有逻辑位置读到的就是广播值。**同一份指针表达式同时覆盖完整掩码与紧凑掩码**，是这段设计的精髓。

#### 4.2.3 源码精读

宿主侧的 stride 计算（注意每个维度都判 `size==1 且 logical>1`）：

[文件路径:L41-L69](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_ffpa_fwd.py#L41-L69) —— `_attn_bias_broadcast_strides`：`attn_bias` 为 `None` 时返回全 0；否则对每个维度返回 `0 if size==1 and logical>1 else stride`，把广播维编码为零步长。

generic 启动器调用它得到四元组并整体传给 kernel：

[文件路径:L909-L911](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_ffpa_fwd.py#L909-L911) —— `_ffpa_attn_forward_generic_impl` 里 `bias_strides = _attn_bias_broadcast_strides(...)`，随后四个 stride 作为标量参数传进 kernel。

kernel 内的指针基址只推进 batch 与 head 两维（M/N 维在循环里随 `offs_m/offs_kv` 推进）：

[文件路径:L377-L378](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_ffpa_fwd.py#L377-L378) —— `AttnBias += off_b * stride_bb + off_hq * stride_bh`；当 bias 是 `[1,1,1,Nkv]` 时 `stride_bb=stride_bh=0`，这一步指针根本不动。

KV 块循环内的 bias 加载（注释明确说「广播维 stride 0，故同一指针表达式既覆盖完整掩码也覆盖紧凑掩码」）：

[文件路径:L425-L433](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_ffpa_fwd.py#L425-L433) —— `bias = tl.load(AttnBias + offs_m[:,None]*stride_bm + offs_kv[None,:]*stride_bn, ...)`，`scores += bias`；`stride_bm=0` 时所有 query 行读同一行偏置，`stride_bn` 正常沿 key 列前进。

#### 4.2.4 代码实践

1. **目标**：亲手验证 `[1,1,1,Nkv]` 紧凑偏置与完整 `[B,Hq,Nq,Nkv]` 偏置给出**数值相同**的输出，且 FFPA 内部不物化。
2. **操作步骤**：运行下面脚本（CUDA 环境）：
   ```python
   # 示例代码：紧凑 vs 完整 attn_bias
   import torch, torch.nn.functional as F
   from ffpa_attn import ffpa_attn_func

   B, Hq, Nq, Nkv, D = 1, 32, 1024, 8192, 512
   dt = torch.bfloat16
   q = torch.randn(B, Hq, Nq,   D, dtype=dt, device="cuda")
   k = torch.randn(B, Hq, Nkv,  D, dtype=dt, device="cuda")
   v = torch.randn(B, Hq, Nkv,  D, dtype=dt, device="cuda")

   bias_vec = torch.randn(1, 1, 1, Nkv, dtype=dt, device="cuda")     # 紧凑 [1,1,1,Nkv]
   bias_full = bias_vec.expand(B, Hq, Nq, Nkv)                        # 物化完整版（仅用于对比）

   out_compact = ffpa_attn_func(q, k, v, attn_mask=bias_vec)
   out_full    = ffpa_attn_func(q, k, v, attn_mask=bias_full)
   ref         = F.scaled_dot_product_attention(q, k, v, attn_mask=bias_vec)
   print("compact vs full:", (out_compact - out_full).abs().max().item())
   print("compact vs sdpa:", (out_compact - ref).abs().max().item())
   ```
3. **观察现象**：`compact vs full` 应接近 0（同一份偏置逻辑等价）；`compact vs sdpa` 也应在大 D 容差内一致。
4. **解释紧凑为何不物化**：`[1,1,1,Nkv]` 的 stride 是 `(Nkv, Nkv, Nkv, 1)`，`_attn_bias_broadcast_strides` 因前三维 `size==1 且 logical>1` 把它们全改成 0，得到 `(0,0,0,1)`。kernel 里 `off_b*0 + off_hq*0 + offs_m*0 + offs_kv*1` —— batch/head/行怎么变指针都不动，只有 key 列变化时才沿这唯一的 `Nkv` 向量前进。所以 `B·Hq·Nq` 个逻辑位置共用同一行 `Nkv` 个元素，物理存储始终是 `Nkv` 个元素，**展开从不发生**。
5. **预期结果 / 待本地验证**：误差量级取决于硬件与大 D 数值精度；若手头无 CUDA 大卡，可改为只读 `_attn_bias_broadcast_strides` 并手算 `[1,1,1,Nkv]` 的四元组应为 `(0,0,0,1)`。

#### 4.2.5 小练习与答案

- **练习 1**：一个 `[B, 1, Nq, 1]` 的偏置（每个 batch、每个 query 行一个标量，对所有 key 相同）经 `_attn_bias_broadcast_strides` 后得到什么 stride 四元组？
  - **答**：`size(0)=B>1`→正常 `stride(0)`；`size(1)=1,Hq>1`→0；`size(2)=Nq>1`→正常 `stride(2)`；`size(3)=1,Nkv>1`→0。结果是 `(stride(0), 0, stride(2), 0)`，即沿 head 与 key 列广播。
- **练习 2**：为什么 `attn_bias is None` 时直接返回 `(0,0,0,0)` 而不是报错？
  - **答**：kernel 用 `HAS_ATTN_BIAS` 这个 `tl.constexpr` 守卫整个 bias 加载分支；当 `HAS_ATTN_BIAS=False` 时 bias 分支被编译期消除，`AttnBias` 指针本身也不会被解引用，故全 0 stride 不会引发越界——它只是一个占位返回值。

---

### 4.3 causal 尾部对齐掩码：`offs_kv <= offs_m + (Nkv - Nq)`

#### 4.3.1 概念说明

因果掩码（u2-l3）让 query 行 `r` 只能看 key 列 `k ≤ r + (Nkv - Nq)`——这是「query 对齐到 KV 尾部」的约定，要求 `Nkv ≥ Nq`。朴素实现会预先构造一个下三角布尔矩阵并在 softmax 前乘进去，但对长序列这会物化 `Nq·Nkv` 的掩码。

FFPA 在 kernel 内**就地**用一行 `tl.where` 实现它：把越界位置直接置 `-inf`，零额外存储。更进一步，还用一个 `end_n` 截断**整块跳过**完全被遮蔽的 KV 块，省掉无效循环。

#### 4.3.2 核心流程

记 `kv_offset = Nkv - Nq`（尾部对齐偏移）。两个优化层次：

1. **块级跳过**：query 行块 `start_m` 的最后一行能看到的最大的 key 列是 `(start_m+1)*BLOCK_M + kv_offset`。超过它的 KV 块全是 `-inf`，干脆把循环上界 `end_n` 卡到这里，整块不进。

   ```
   end_n = min(Nkv, (start_m + 1) * BLOCK_M + kv_offset)
   ```

2. **元素级遮蔽**：在进了循环的块内，仍可能有部分越界列，用逐元素比较把它们设为 `-inf`：

   ```
   causal_mask = offs_kv[None,:] <= (offs_m[:,None] + kv_offset)
   scores = where(causal_mask, scores, -inf)
   ```

   `-inf` 经过 `exp` 后为 0，对 `rowmax/rowsum` 与 PV 累加都不再有贡献，等价于「这些位置不参与注意力」。

decode 路径同理，但因 query 行极少（甚至 1 行），块级跳过意义不大，主要靠元素级遮蔽；`Nq==1` 的 GEMV 路径里 causal 退化为「key 全可见」（`offs_kv <= Nkv - 1` 恒真），因为单 query 行就在最尾部。

#### 4.3.3 源码精读

generic kernel 的 `kv_offset` 与 `end_n` 块级跳过：

[文件路径:L386-L401](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_ffpa_fwd.py#L386-L401) —— `kv_offset = seqlen_k - seqlen_q`；`IS_CAUSAL` 时把 KV 循环上界 `end_n` 截到 `(start_m+1)*BLOCK_M + kv_offset`，跳过完全遮蔽的 KV 块。

元素级 causal 掩码（注释点明「下右 causal、与 PyTorch SDPA 一致」）：

[文件路径:L436-L440](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_ffpa_fwd.py#L436-L440) —— `causal_mask = offs_kv[None,:] <= (offs_m[:,None] + kv_offset)`，越界列置 `-inf`。

decode MMA 路径套用同一公式（`Nq < 8` 窗口也能用尾部对齐）：

[文件路径:L706-L711](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_ffpa_fwd.py#L706-L711) —— decode 多行 MMA 路径里同样的 `offs_kv <= offs_m + kv_offset`，越界置 `-inf`，行越界再由 `mask_m` 兜底。

decode GEMV 路径（`Nq==1`）causal 退化为全可见：

[文件路径:L623-L625](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_ffpa_fwd.py#L623-L625) —— `causal_mask = offs_kv <= (seqlen_k - 1)`，单 query 行位于序列尾部，所有有效 key 均可见，故近乎恒真。

#### 4.3.4 代码实践

1. **目标**：复现 causal 语义，并验证 `end_n` 块级跳过确实正确（不会漏算可见列）。
2. **操作步骤**：
   - 运行 `pytest tests/test_ffpa_fwd.py -k "causal"`（或包含 causal 的子集），确认正确性。
   - 手算一个最小例子：`Nq=4, Nkv=8, BLOCK_M=4, BLOCK_N=2`，`kv_offset=4`，`start_m=0`。求 `end_n` 与 `start_m=0` 行块里 query 行 0、1、2、3 各自能看到的 key 列范围。
3. **观察现象**：`end_n = min(8, (0+1)*4 + 4) = 8`，于是 `start_m=0` 仍要遍历全部 4 个 KV 块；逐行看，query 行 0 看到 key 列 `k <= 0+4=4`（即列 0..4），query 行 3 看到 `k <= 3+4=7`（列 0..7，几乎全见）。
4. **预期结果**：手动构造的因果参考（上三角 `-inf`）与 FFPA 输出在大 D 容差内一致；`end_n` 截断不会丢掉任何「行块内至少一行能看到」的 KV 块。
5. **待本地验证**：若环境无大 D CUDA，可只做手算部分，确认 `end_n` 公式与元素级 `tl.where` 在数学上等价于标准下三角因果。

#### 4.3.5 小练习与答案

- **练习 1**：为何 `Nkv < Nq` 时 causal 直接在 `normalize_inputs` 报错，而不是进 kernel？
  - **答**：尾部对齐要求 query 序列整体落在 KV 序列范围内，`kv_offset = Nkv - Nkv` 会变负，query 头部行连第 0 个 key 都看不到、输出全 0，语义无意义；故 u2-l3 在校验阶段就拒绝（`ValueError`），不让它进 kernel。
- **练习 2**：`end_n` 块级跳过对 `start_m` 很大的尾部行块收益最大，为什么？
  - **答**：`start_m` 越大，`(start_m+1)*BLOCK_M + kv_offset` 越可能远小于 `Nkv`，于是 `end_n` 把后面大片「对该行块完全遮蔽」的 KV 块整段跳过；而 `start_m=0` 的行块几乎要看完全部 KV，跳过收益最小。

---

### 4.4 dropout Philox 重放：逐元素线性偏移

#### 4.4.1 概念说明

dropout 在训练时按概率 `p` 随机置零部分注意力权重，被保留的位置乘 `1/(1-p)`（「反向缩放 dropout」，期望保持不变）。难点在于**反向传播必须重放出与正向完全相同的掩码**，否则梯度对不上。

PyTorch SDPA 的做法是用 Philox 计数器 RNG：逻辑 score 张量 `[B, Hq, Nq, Nkv]` 的**每个元素**按行优先顺序消耗一个 Philox 输出。FFPA 完全复刻这一布局，所以给定相同的 `(seed, offset)`，FFPA 与 SDPA 产生逐位相同的掩码，反向也能重放。函数名 `_curand_uniform_from_element_offset`（注：规划里的 `_curcurand_...` 是笔误，源码实际名为 `_curand_...`）正点明「从元素线性偏移生成均匀随机数」。

#### 4.4.2 核心流程

两步：先把逻辑位置换算成**元素线性偏移**，再从偏移生成 `[0,1)` 均匀随机数并阈值化。

**线性偏移**（行优先 `[B, Hq, Nq, Nkv]`）：

\[
\text{linear} = \text{off\_hb}\cdot N_q \cdot N_{kv} + \text{offs\_m}\cdot N_{kv} + \text{offs\_n}
\]

其中 `off_hb = batch*Nq + head`（与网格第二维一致），`offs_m` 是 query 行块内行号 + 块起点，`offs_n` 是 key 列号。**关键**：`offs_n` 必须是**全局 key 列号**（decode 里要含 `chunk_start`），否则不同 chunk 的 dropout 会重叠。最终偏移还要加上一个基地址 `philox_offset`（每次注意力调用整体推进，保证跨调用不重叠）。

**从偏移到掩码**：Philox 一次产出 4 个 uint32，故偏移按 4 分组：

\[
\text{quad\_offset} = \lfloor \text{offset}/4 \rfloor,\quad \text{lane} = \text{offset} \bmod 4
\]

`randint4x(seed, quad_offset)` 给出 `r0..r3`，按 `lane` 选取一个，**位转为 uint32**（而非走有符号 int32，避免高位值差一个 ulp），再映射到 `[0,1)`：

\[
u = (u32 + 1.0) \times 2^{-32},\quad 2^{-32} \approx 2.3283064365386963\times 10^{-10}
\]

阈值化与反向缩放：

\[
\text{keep} = (u > p),\quad p_{\text{new}} = p \cdot \text{keep} \cdot \frac{1}{1-p}
\]

一个**精度要点**：dropout 只作用在用于 **O 累加**的概率 `p` 上，而 online softmax 的归一化分母 `l_i` 与最终保存的 `LSE` 用的是**未 dropout** 的 `p`。这样反向重放 dropout 时只需重新生成掩码乘到重算的 `p` 上，`LSE` 保持干净。

#### 4.4.3 源码精读

均匀随机数生成（注释解释了为何要 uint32 位转而非 int32）：

[文件路径:L80-L94](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_ffpa_fwd.py#L80-L94) —— `_curand_uniform_from_element_offset`：`quad_offset = element_offset//4`、`lane = element_offset - quad_offset*4`，`randint4x(seed, quad_offset)` 取四元组按 lane 选中，`.to(tl.uint32, bitcast=True)` 位转后乘 `2^-32` 得 `[0,1)` 均匀值。

dropout 应用（注释强调「`offs_n` 必须是全局 KV 位置，对齐 SDPA 逻辑布局」）：

[文件路径:L97-L123](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_ffpa_fwd.py#L97-L123) —— `_apply_dropout_to_p`：`linear = off_hb*seqlen_q*seqlen_k + offs_m*seqlen_k + offs_n`，调上面函数得 `rand`，`keep = rand > dropout_p`，`p = p*keep*(1/(1-p))`；`HAS_DROPOUT=False` 时直接返回原 `p`。

generic 主循环里的调用点（注意 `l_new` 用未 dropout 的 `p` 算，dropout 只改 O 累加用的 `p`）：

[文件路径:L446-L462](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_ffpa_fwd.py#L446-L462) —— `l_new = l_i*alpha + sum(p)` 在 dropout 之前；随后 `p = _apply_dropout_to_p(p, ...)`、`p = p.to(DTYPE)`；注释（L442-L445）说明 LSE 保留未 dropout 的归一化。

decode GEMV 路径里 `offs_kv` 已含 `chunk_start`，保证全局列号：

[文件路径:L631-L641](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_ffpa_fwd.py#L631-L641) —— GEMV decode 的 dropout 内联展开：`linear = off_hb*seqlen_q*seqlen_k + (q_block*BLOCK_M)*seqlen_k + offs_kv`，`offs_kv` 含 `chunk_start`，故各 chunk 的 RNG 不重叠。

#### 4.4.4 代码实践

1. **目标**：理解 dropout 的逐元素偏移布局，并确认 FFPA 与 SDPA 在相同 RNG 状态下掩码一致。
2. **操作步骤**：
   - 读 4.4.3 的三段代码，写出 `[B,Hq,Nq,Nkv]` 里元素 `(b,h,i,j)` 的线性偏移公式。
   - （CUDA 环境）构造一个 dropout 用例，对比 FFPA 与 SDPA：
     ```python
     # 示例代码：dropout 一致性（待本地验证）
     import torch, torch.nn.functional as F
     from ffpa_attn import ffpa_attn_func
     B, Hq, Nq, D = 1, 32, 8192, 512
     dt = torch.bfloat16
     q = torch.randn(B, Hq, Nq, D, dtype=dt, device="cuda")
     k = torch.randn_like(v := torch.randn_like(q))
     # 注意：需保证二者使用同一 (seed, offset)，具体传入方式见 ffpa_attn_interface / functional 的 dropout 接口
     out_ffpa = ffpa_attn_func(q, k, v, dropout_p=0.1, forward_backend="triton")
     ```
3. **观察现象**：当 RNG 状态对齐时，FFPA 与 SDPA 的 dropout 掩码应逐位相同（被保留的位置集合一致），输出在大 D 容差内一致；ROCm 上 Triton-AMD 的 RNG 与 SDPA 有差异（见 `tests/test_ffpa_fwd.py` 顶部 `IS_ROCM` 注释）。
4. **预期结果 / 待本地验证**：CUDA 上 dropout 一致；若手头无法对齐 RNG 状态或无大卡，改为「源码阅读型」——手算 `(b=0,h=0,i=3,j=7), Nq=8192, Nkv=8192` 的 `linear` 值，并解释为何 `offs_n` 在 decode 必须含 `chunk_start`（否则两个 chunk 会复用同一段 RNG，掩码错位）。

#### 4.4.5 小练习与答案

- **练习 1**：为什么用 `bitcast=True` 把 int32 位转为 uint32，而不是直接 `.to(tl.float32)`？
  - **答**：`randint4x` 返回有符号 int32；若先转 float 再处理，高位（符号位为 1）的负数会与「无符号大正数」相差一个 ulp，阈值化时正好在 `dropout_p` 附近的元素可能翻转 keep/丢弃，破坏与 SDPA 的逐位一致。位转为 uint32 后再 `(u32+1)*2^-32`，完全对齐 `curand_uniform4`。
- **练习 2**：为什么 `l_new`（softmax 分母）必须用**未 dropout** 的 `p`？
  - **答**：保存的 `LSE = m_i + log(l_i)` 要在反向重算 softmax 时复用；若 `l_i` 含 dropout，反向就得「猜」哪些位置被丢弃。让 `LSE` 保持干净、dropout 只在 O 累加时乘上去，反向只需重放掩码乘到重算的 `p`，数值与逻辑都简单可控。

---

## 5. 综合实践

把四个特性串起来：在 kernel 里做一次「代码考古」，标注四段代码的行号与职责，并用一个紧凑偏置验证「免物化」。

**任务 A — 源码标注（必做，纯阅读）**：打开 `src/ffpa_attn/triton/_ffpa_fwd.py`，在 `_ffpa_fwd_kernel_impl` 内分别定位并写下：

| 特性 | 关键代码行 | 一句话职责 |
| --- | --- | --- |
| GQA 头映射 | L369-L370（`group_size`/`off_hkv`）+ L373-L374（`K/V` 用 `off_hkv`） | query 头映射回所属 KV 头，零拷贝共享 K/V |
| attn_bias 广播 | L425-L433（bias 加载，`stride_bm/stride_bn`）+ 宿主 L41-L69 | stride=0 表达广播维，紧凑掩码不物化 |
| causal 掩码 | L386/L399-L401（`kv_offset`/`end_n`）+ L436-L440（`tl.where`） | 尾部对齐 + 块级跳过 + 元素级遮蔽 |
| dropout | L80-L94（均匀数）+ L97-L123（`_apply_dropout_to_p`）+ L450-L461（调用点） | 逐元素 Philox 偏移，与 SDPA 对齐可重放 |

**任务 B — 解释 `[1,1,1,Nkv]` 免物化（必做）**：用一段话回答——给定 `attn_bias` 形状 `[1,1,1,Nkv]`，`_attn_bias_broadcast_strides` 返回 `(0,0,0,1)`（前三维 `size==1 且 logical>1` → 0，末维正常）。kernel 寻址 `off_b*0 + off_hq*0 + offs_m*0 + offs_kv*1` 中，batch/head/行三项系数为 0，指针只随 key 列沿唯一一条 `Nkv` 向量前进；于是 `B·Hq·Nq` 个逻辑位置共用这 `Nkv` 个元素，物理存储始终是 `Nkv`，**展开为 `[B,Hq,Nq,Nkv]` 从未发生**。

**任务 C — 综合运行（可选，CUDA 环境）**：构造一个**同时**带 GQA、causal、紧凑 attn_bias、dropout 的用例，对比 FFPA 与 SDPA：

```python
# 示例代码：四特性叠加（待本地验证）
import torch, torch.nn.functional as F
from ffpa_attn import ffpa_attn_func

B, Hq, Hkv, Nq, Nkv, D = 1, 32, 8, 2048, 8192, 512   # GQA: group_size=4
dt = torch.bfloat16
q = torch.randn(B, Hq,  Nq,  D, dtype=dt, device="cuda")
k = torch.randn(B, Hkv, Nkv, D, dtype=dt, device="cuda")
v = torch.randn_like(k)
bias = torch.randn(1, 1, 1, Nkv, dtype=dt, device="cuda")   # 紧凑 attn_bias

out = ffpa_attn_func(q, k, v, attn_mask=bias, is_causal=True,
                     enable_gqa=True, dropout_p=0.0, forward_backend="triton")
```

观察：GQA 下输出形状为 `[B, Hq, Nq, D]`；causal 使 query 行只看尾部 key；紧凑 bias 不报形状错。若开启 `dropout_p>0`，正确性需对齐 RNG 状态（待本地验证）。

## 6. 本讲小结

- **GQA** 用 `off_hkv = off_hq // group_size` 在 kernel 内换算头索引，`Q/O` 用 query 头、`K/V` 用 KV 头，零拷贝共享，generic 与 decode 两条路径同一契约。
- **attn_bias** 把广播维编码为 stride=0，同一份指针表达式 `+off_b*stride_bb + off_hq*stride_bh + offs_m*stride_bm + offs_kv*stride_bn` 同时覆盖完整掩码与紧凑掩码，`[1,1,1,Nkv]` 永不物化。
- **causal** 用 `kv_offset = Nkv - Nq` 实现尾部对齐：`end_n` 块级跳过完全遮蔽的 KV 块，`tl.where(offs_kv <= offs_m + kv_offset, ...)` 元素级遮蔽越界列，零额外存储。
- **dropout** 复刻 SDPA 的 Philox 逐元素布局 `linear = off_hb*Nq*Nkv + offs_m*Nkv + offs_n`，uint32 位转保证与 `curand_uniform4` 逐位一致，反向可重放；`l_i/LSE` 保持未 dropout，掩码只作用于 O 累加。
- 四个特性都「叠加」在 u4-l1 的 online softmax 主循环上，靠 `tl.constexpr`（`HAS_ATTN_BIAS/HAS_DROPOUT/IS_CAUSAL`）编译期消除未启用分支，互不干扰。

## 7. 下一步学习建议

- 本讲的 dropout 重放依赖 `philox_offset` 的基地址推进，其完整生成逻辑在 u3-l4（`_reserve_large_d_dropout_rng` 复刻 SDPA 的 Philox offset 约定），建议结合阅读以理解「前向保存 RNG 状态 → 反向重放」的全链路。
- attn_bias 的**反向梯度**有额外的广播归约与 fp32 累加设计，见 `src/ffpa_attn/triton/__init__.py` 的 `_attn_bias_grad_needs_reduction`/`_attn_bias_grad_dtype`，这部分会在 u5-l2（dK/dV 与 dQ 的 shared-pid 设计）顺带涉及。
- causal 的块级跳过在 decode 与反向里会更复杂（decode 反向还要跨块归约 dQ），下一阶段可读 u5-l3（decode 反向与 dQ 跨块归约）。
- 若想看这四个特性在手写 CUDA 里如何对应（GQA 头映射、swizzle、Philox），可跳到 u7-l1（CUDA 前向 kernel 架构）对比 Triton 与 CUDA 的实现差异。
