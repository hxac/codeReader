# GQA / MQA 分组查询注意力

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清楚 MHA、MQA、GQA 三者的区别，以及「分组」到底分的是什么。
- 理解 FFPA 里 `group_size = Nh_q / Nh_kv` 的含义，并知道为什么 `Nh_q` 必须是 `Nh_kv` 的整数倍。
- 解释 `enable_gqa` 为什么默认是 `False`，以及「头数不等但没有显式开启」时为什么会报 `ValueError`。
- 用 `repeat_interleave` 写出 GQA/MQA 的参考实现，并验证 FFPA 输出与 SDPA 一致。
- 独立构造一个 MQA（`Nh_kv == 1`）用例并跑通。

## 2. 前置知识

本讲在 u2-l1（`ffpa_attn_func` 签名与张量布局）之上展开，默认你已经知道：

- 输入张量是四维 `[B, Nh, N, D]`，分别表示 batch、头数、序列长度、每个头的维度（head_dim）。
- Q、K、V 三者 head_dim 必须一致，dtype 必须是 `fp16`/`bf16` 且一致。
- FFPA 在大 D、长序列时才走自研 kernel，否则会自动回退到原生 SDPA。

下面补充三个本讲用到的基础术语：

- **MHA（Multi-Head Attention，多头注意力）**：最经典的注意力，Query 头数等于 KV 头数，即 `Nh_q == Nh_kv`。每个 Query 头用自己的 K/V 头。
- **MQA（Multi-Query Attention，多查询注意力）**：所有 Query 头**共用唯一一组 K/V**，即 `Nh_kv == 1`。KV 的存储和带宽开销最小，但精度略有损失。
- **GQA（Grouped-Query Attention，分组查询注意力）**：介于两者之间。把 `Nh_q` 个 Query 头分成若干组，每组共用一组 K/V。`Nh_kv` 可以取 `1` 到 `Nh_q` 之间的值。MQA 其实就是 GQA 在 `Nh_kv == 1` 时的特例。

> 直觉：注意力计算里 Q 决定「问什么」，K/V 决定「拿什么来答」。在很多场景（尤其是解码、KV cache）下，K/V 的体积直接决定显存和带宽。让多个 Q 头共用一套 K/V，可以显著降低这部分开销——这就是「分组」的动机，也是 Llama-2/Llama-3 等模型（典型比例 `32/8`）普遍采用 GQA 的原因。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
| --- | --- |
| `src/ffpa_attn/ffpa_attn_interface.py` | 公共入口 `ffpa_attn_func`，定义 `enable_gqa` 形参，并在回退路径里把它透传给 SDPA。 |
| `src/ffpa_attn/functional.py` | `FFPAAttnMeta.normalize_inputs` 在这里做 GQA/MQA 的所有形状校验（头数整除、KV 头一致、显式开启）。 |
| `src/ffpa_attn/triton/_ffpa_fwd.py` | Triton 前向 kernel。GQA 的「头映射」`off_hkv = off_hq // group_size` 在这里实现，是 FFPA 输出能与 `repeat_interleave` 参考对齐的根因。 |
| `docs/index.md` | 官方 GQA/MQA 示例，本讲实践直接复现它。 |
| `tests/test_ffpa_fwd.py` | 前向 GQA 正确性测试，展示了 `repeat_interleave` 参考实现的标准写法。 |

## 4. 核心概念与源码讲解

### 4.1 什么是 GQA / MQA：分组到底分的是什么

#### 4.1.1 概念说明

注意力的核心运算是每个 Query 头对自己的序列做一次：

\[
O_{h_q} = \mathrm{softmax}\!\left(\mathrm{scale}\cdot Q_{h_q} K_{h_{kv}}^{\top}\right) V_{h_{kv}}
\]

关键点：上式里 Query 头下标 \(h_q\) 与 KV 头下标 \(h_{kv}\) **未必相等**。MHA 要求二者相等；GQA/MQA 允许 \(Nh_q > Nh_kv\)，于是需要一条规则来回答「第 \(h_q\) 个 Query 头，到底用第几个 KV 头？」。

FFPA（与 SDPA、FlashAttention 一致）采用**连续分组**约定：把 `Nh_q` 个 Query 头按顺序每 `group_size` 个划为一组，整组共用同一个 KV 头。也就是：

\[
\mathrm{group\_size} = \frac{Nh_q}{Nh_kv}
\]

\[
h_{kv} = h_q \,\mathbin{//}\, \mathrm{group\_size}
\]

其中 `//` 表示整除（向下取整）。于是 Query 头下标区间 \([0, \mathrm{group\_size})\) 用 KV 头 0，\([\mathrm{group\_size}, 2\cdot\mathrm{group\_size})\) 用 KV 头 1，依此类推。

注意几个特例：

- **MHA**：`group_size == 1`，`Nh_q == Nh_kv`，每个 Query 头独占一个 KV 头。
- **MQA**：`Nh_kv == 1`，`group_size == Nh_q`，所有 Query 头共用唯一的 KV 头。

#### 4.1.2 核心流程

给定输入 `[B, Nh_q, Nq, D]` 的 Q 和 `[B, Nh_kv, Nkv, D]` 的 K/V，FFPA 计算每个 Query 头输出的伪代码：

```
group_size = Nh_q // Nh_kv
for b in range(B):
    for h_q in range(Nh_q):
        h_kv = h_q // group_size          # 连续分组：定位 KV 头
        scores = scale * Q[b, h_q] @ K[b, h_kv].T   # [Nq, Nkv]
        attn   = softmax(scores + bias)            # 掩码/偏置在此叠加
        O[b, h_q] = attn @ V[b, h_kv]              # [Nq, D]
return O   # 形状 [B, Nh_q, Nq, D]
```

要点：

1. **输出形状跟 Q 走**：永远是 `[B, Nh_q, Nq, D]`，头数是 Query 头数。
2. **K、V 共享同一套头**：`h_kv` 同时决定取 K 和取 V 的哪个头，所以 K 和 V 必须有相同的 `Nh_kv` 和 `Nkv`。
3. **整除是硬约束**：因为 `h_q // group_size` 只有在 `Nh_q % Nh_kv == 0` 时才能均匀分配，否则无法定义「连续分组」。

#### 4.1.3 源码精读

上面那条 `h_kv = h_q // group_size` 的映射，在 Triton 前向 kernel 里就是 GQA 的全部实现。每个 program 负责一个 (batch, Query 头) 组合，它先用 `program_id` 反解出自己的 Query 头下标，再换算出对应的 KV 头：

[ffpa_fwd.py:367-375](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_ffpa_fwd.py#L367-L375) —— 这段先算 `group_size`，再用 `off_hq // group_size` 得到 `off_hkv`，随后把 Q/O 指针按 Query 头偏移、把 K/V 指针按 KV 头偏移。注意 O 与 LSE 都是「Query 头索引」的，而 K/V 是「KV 头索引」的，这正是分组查询的本质。

> 你现在不用理解 Split-D、online softmax 等细节（那是 u4 的事）。本讲只需记住：**FFPA 在 kernel 里没有真正去复制 K/V，而是靠这条 `off_hkv = off_hq // group_size` 的指针映射实现「多个 Q 头共享一组 K/V」**。这也解释了为什么 FFPA 的输出能和「`repeat_interleave` 复制 K/V 后再算 MHA」完全对齐——它们用的是同一条头分配规则。

#### 4.1.4 代码实践（源码阅读型）

1. **目标**：把 4.1.2 的伪代码与真实 kernel 对上号。
2. **步骤**：打开 `src/ffpa_attn/triton/_ffpa_fwd.py` 第 363–375 行，定位 `off_hq = off_hb % nheads_q`、`group_size = nheads_q // nheads_kv`、`off_hkv = off_hq // group_size` 三行。
3. **观察**：确认 Q 与 O 的指针偏移用的是 `off_hq`（Query 头），K 与 V 的指针偏移用的是 `off_hkv`（KV 头）。
4. **预期结果**：你能用自己的话说出「为什么 kernel 不需要复制 K/V，只需要换一个头下标就能实现 GQA」。
5. 行号若与上述不符（仓库后续改动），以 `group_size = nheads_q // nheads_kv` 与 `off_hkv = off_hq // group_size` 这两条语句为准——「待本地确认精确行号」。

#### 4.1.5 小练习与答案

**练习 1**：`Nh_q = 32, Nh_kv = 8` 时，`group_size` 是多少？Query 头 13 用的是第几个 KV 头？

**答**：`group_size = 32 // 8 = 4`；`h_kv = 13 // 4 = 3`，即第 3 个 KV 头。

**练习 2**：把 `Nh_kv` 从 8 改成 1，`group_size` 变成多少？这对应哪种注意力？

**答**：`group_size = 32 // 1 = 32`，所有 32 个 Query 头共用唯一一个 KV 头，这就是 **MQA**。

---

### 4.2 `enable_gqa` 为什么默认 `False`

#### 4.2.1 概念说明

FFPA 的 `ffpa_attn_func` 把 `enable_gqa` 做成了一个**显式开关**，默认 `False`：

[ffpa_attn_interface.py:79](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/ffpa_attn_interface.py#L79) —— 这是签名里的形参定义。

这套设计与 PyTorch 原生 `F.scaled_dot_product_attention` 的 `enable_gqa` **完全对齐**。设计意图有两点：

1. **与 SDPA 行为一致**：FFPA 经常会回退到原生 SDPA（见 u1-l4、u2-l1）。如果两边默认值不一致，同一个调用在「走 FFPA」和「回退 SDPA」时行为会偷偷不同，难以排查。
2. **避免误用静默通过**：头数本来相等（MHA）时，用户通常不会主动想「开 GQA」。把默认设为 `False`，意味着只有当用户真的传入了 `Nh_q != Nh_kv` 并且**明确知道自己在做什么**（显式写 `enable_gqa=True`）时，GQA 语义才生效。

> 一句话：**`enable_gqa` 不是「能不能用 GQA」的开关，而是「我承认我的 K/V 头数和 Q 不一样，请按分组语义算」的声明。**

#### 4.2.2 核心流程

`ffpa_attn_func` 收到 `enable_gqa` 后，有两条路径：

1. **回退路径**：若 `meta.fallback(...)` 判定要走 SDPA，则 `enable_gqa` 原样透传给底层 SDPA 绑定（避免递归，见 u1-l4）。
2. **FFPA kernel 路径**：通过 `normalize` 进入校验，`enable_gqa` 被用来决定「头数不等是否合法」。

两条路径都把 `enable_gqa` 当作「是否启用分组语义」的标志，但都不会因为 `enable_gqa=True` 而自动构造额外张量——分组完全在 kernel 内部用头下标映射实现。

#### 4.2.3 源码精读

回退路径里，`enable_gqa` 被透传给原生 SDPA：

[ffpa_attn_interface.py:156-168](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/ffpa_attn_interface.py#L156-L168) —— 注意末尾 `enable_gqa=enable_gqa`，这保证回退时 SDPA 也按分组语义算，与 FFPA kernel 行为一致。

docstring 里也明确写了这条默认值的设计理由：

[ffpa_attn_interface.py:131-134](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/ffpa_attn_interface.py#L131-L134) —— 说明默认 `False` 是为了「match SDPA exactly」，头数不等时必须显式传 `True`。

#### 4.2.4 代码实践（阅读型）

1. **目标**：确认「回退」与「不回退」两条路径都尊重 `enable_gqa`。
2. **步骤**：在 `ffpa_attn_interface.py` 中搜索 `enable_gqa`，分别看回退分支（约 156–168 行）与 `meta.normalize(...)` 调用（约 170–179 行）两处。
3. **观察**：回退分支把它透传给 SDPA；正常分支把它交给 `normalize` 做校验。两处都没有「自动推断成 True」。
4. **预期结果**：理解「默认 False + 显式开启」是一致贯穿两条路径的设计。

#### 4.2.5 小练习与答案

**练习**：为什么 FFPA 不直接根据 `Nh_q != Nh_kv` 自动启用 GQA，而要用户显式传 `enable_gqa=True`？

**答**：为了让行为与原生 SDPA 严格一致，并避免「用户本意是 MHA 但误传了不等头数」时被静默当成 GQA 处理。显式开关把这种潜在的形状错误变成一个清晰的 `ValueError`，更容易排错。

---

### 4.3 GQA/MQA 的输入约束与 `normalize_inputs` 校验

#### 4.3.1 概念说明

要让分组语义成立，输入张量必须满足一组硬约束。FFPA 把全部校验集中在 `FFPAAttnMeta.normalize_inputs` 里，在任何 kernel 启动之前就做完。围绕 GQA/MQA 的约束有四条：

1. **K 与 V 必须头数相同**：`Nh_k == Nh_v == Nh_kv`。因为同一个 `h_kv` 既取 K 又取 V，二者头维必须一一对应。
2. **`Nh_q` 必须是 `Nh_kv` 的整数倍**：`Nh_q % Nh_kv == 0`，否则「连续分组」无定义。
3. **K 与 V 必须序列长度相同**：`Nk == Nv == Nkv`。注意 `Nq` 可以不等于 `Nkv`（见 u2-l2 的 cross/decode）。
4. **头数不等时必须显式 `enable_gqa=True`**：否则抛 `ValueError`，提示用户要么开启 GQA，要么把头数改一致。

#### 4.3.2 核心流程

`normalize_inputs` 对 GQA 相关形状的校验顺序大致是：

```
1. 三者都必须是 4-D [B, H, N, D]
2. batch 维一致
3. Nh_k == Nh_v               # K/V 头数一致
4. Nh_q % Nh_kv == 0          # 整除约束
5. Nk == Nv                   # K/V 序列长度一致
6. D_q == D_k == D_v          # head_dim 一致
7. 若 enable_gqa == False 且 Nh_q != Nh_kv -> ValueError
```

注意第 4 步（整除）先于第 7 步（显式开关）执行。也就是说，哪怕你传了 `enable_gqa=True`，如果 `Nh_q` 不能被 `Nh_kv` 整除（比如 `Nh_q=32, Nh_kv=5`），依然会先在整除这一步报错。

#### 4.3.3 源码精读

下面这一段集中体现了 K/V 头一致、整除约束与 K/V 序列一致三件事：

[functional.py:596-609](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L596-L609) —— 三条 `if` 分别校验：`key.size(1) != value.size(1)`（K/V 头数）、`query.size(1) % key.size(1) != 0`（整除）、`key.size(2) != value.size(2)`（K/V 序列长度）。

紧接着是「显式开关」校验，也就是「头数不等但没开 GQA」的报错：

[functional.py:613-618](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L613-L618) —— `if not enable_gqa and query.size(1) != key.size(1)` 时抛 `ValueError`，并把修复建议直接写进报错信息：「Set enable_gqa=True or use matching head counts.」

> 这两条加起来，构成了 FFPA 对 GQA/MQA 输入的全部「形状合法性」校验。它们只看形状，不看 D、Nq、Nkv 是否会触发回退——回退判定在更早的 `fallback()` 里已经做完了。

#### 4.3.4 代码实践

1. **目标**：亲手触发一次「忘记开 `enable_gqa`」的报错，并理解它和「整除失败」的报错是两条不同的路径。
2. **操作步骤**（示例代码）：

   ```python
   import torch
   from ffpa_attn import ffpa_attn_func

   B, D, N = 1, 512, 1024
   Nh_q, Nh_kv = 32, 8
   q = torch.randn(B, Nh_q,  N, D, dtype=torch.bfloat16, device="cuda")
   k = torch.randn(B, Nh_kv, N, D, dtype=torch.bfloat16, device="cuda")
   v = torch.randn(B, Nh_kv, N, D, dtype=torch.bfloat16, device="cuda")

   # (A) 头数不等但没开 GQA -> 期望 ValueError
   try:
       ffpa_attn_func(q, k, v)
   except ValueError as e:
       print("A 触发预期报错:", e)

   # (B) 开了 GQA，但 Nh_q 不能被 Nh_kv 整除 -> 期望 ValueError（整除约束）
   k_bad = torch.randn(B, 5, N, D, dtype=torch.bfloat16, device="cuda")
   v_bad = torch.randn(B, 5, N, D, dtype=torch.bfloat16, device="cuda")
   try:
       ffpa_attn_func(q, k_bad, v_bad, enable_gqa=True)
   except ValueError as e:
       print("B 触发预期报错:", e)
   ```

3. **观察**：(A) 命中 613–618 行的「显式开关」报错；(B) 命中 601–605 行的「整除」报错。
4. **预期结果**：两个用例都抛 `ValueError`，且报错信息分别对应上面两段源码。
5. 若本地无 CUDA 或形状触发回退（如 D≤256），(A) 仍会先在 `normalize` 阶段抛错（校验早于回退执行），现象一致；不过最干净的现象是在大 D 上复现。其他情况标注「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：`Nh_q=32, Nh_kv=6`，传 `enable_gqa=True` 会不会通过校验？

**答**：不会。`32 % 6 == 2 != 0`，整除约束（601–605 行）会先报错，与是否开了 GQA 无关。

**练习 2**：如果只把 K 的头数改成与 V 不同（例如 `Nh_k=8, Nh_v=4`），即使 `Nh_q=32` 整除它们都成立，会通过吗？

**答**：不会。K 与 V 必须头数一致（596–600 行），`Nh_k != Nh_v` 会直接报错——因为同一个 `h_kv` 需要同时索引 K 和 V。

---

### 4.4 参考实现：用 `repeat_interleave` 对齐到全头数

#### 4.4.1 概念说明

验证 GQA/MQA 输出正确性，最稳的办法是构造一个**数值等价的的 MHA 参考实现**：把 K/V 沿头维复制成与 Q 相同的头数，再调用原生 SDPA（MHA）。

关键在于「怎么复制」才能和 FFPA 的连续分组约定对上。PyTorch 的 `repeat_interleave(group_size, dim=1)` 正好做这件事：它把每个 KV 头沿头维**连续重复** `group_size` 次，得到的头排列是 `[KV0,KV0,...,KV1,KV1,...]`——这与 `h_kv = h_q // group_size` 完全一致。

> 对比 `torch.repeat(..., dim=1)`（整体平铺）会产生 `[KV0,KV1,...,KV0,KV1,...]` 的排列，对应的是另一种「交错分组」约定，**不能**用来对齐 FFPA/SDPA 的连续分组。务必用 `repeat_interleave`。

#### 4.4.2 核心流程

参考实现的计算流程：

```
group_size = Nh_q // Nh_kv
k_ref = k.repeat_interleave(group_size, dim=1)   # [B, Nh_q, Nkv, D]
v_ref = v.repeat_interleave(group_size, dim=1)   # [B, Nh_q, Nkv, D]
ref = SDPA(q, k_ref, v_ref, scale=1/sqrt(D))     # 普通 MHA
# 比较 ffpa_attn_func(q, k, v, enable_gqa=True) 与 ref
```

由于复制后头排列与 FFPA 的头映射一一对应，二者在 fp16/bf16 容差内应当一致。

#### 4.4.3 源码精读

官方文档示例就是这套写法，K/V 用 `repeat_interleave` 复制后调 SDPA：

[docs/index.md:104-131](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/docs/index.md#L104-L131) —— 这是 `<a id="example-gqa">` 锚点下的 GQA/MQA 示例。注意它特意选了 `Nh_q=32, Nh_kv=8`（Llama-3 风格的 `group_size=4`）、`D=512`（FFPA 主攻的大 D）、`Nq=1024, Nkv=4096`（避开 `Nq<512` 或 `Nkv<512` 的回退条件），从而保证真正走 FFPA kernel 而非回退 SDPA。

仓库测试里用的是同一套参考，且把容差交给 `torch.testing.assert_close`：

[tests/test_ffpa_fwd.py:961-977](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/tests/test_ffpa_fwd.py#L961-L977) —— `test_ffpa_attn_func_triton_forward_gqa_matches_sdpa`：构造 `Nh_q=8, Nh_kv=2`（`group_size=4`），用 `k.repeat_interleave(group_size, dim=1)` 与 `v.repeat_interleave(group_size, dim=1)` 生成参考，再与 `ffpa_attn_func(..., enable_gqa=True)` 比较。这是「GQA 输出 ≡ repeat_interleave 后的 MHA」这条等价关系在 CI 里的正式锁定。

#### 4.4.4 代码实践（完整可运行）

1. **目标**：复现官方 GQA 示例，并确认 FFPA 与 `repeat_interleave` 参考的 `max_abs_err` 在 fp16/bf16 容差内。
2. **操作步骤**：

   ```python
   import torch
   import torch.nn.functional as F
   from ffpa_attn import ffpa_attn_func

   # GQA: Nh_q=32, Nh_kv=8, group_size=4；大 D=512 走 FFPA kernel
   B, D, Nq, Nkv = 1, 512, 1024, 4096
   Nh_q, Nh_kv = 32, 8
   q = torch.randn(B, Nh_q,  Nq,  D, dtype=torch.bfloat16, device="cuda")
   k = torch.randn(B, Nh_kv, Nkv, D, dtype=torch.bfloat16, device="cuda")
   v = torch.randn(B, Nh_kv, Nkv, D, dtype=torch.bfloat16, device="cuda")

   out = ffpa_attn_func(q, k, v, enable_gqa=True)   # -> (1, 32, 1024, 512)
   print(out.shape, out.dtype)

   # 参考：把 K/V 沿头维连续复制到 Nh_q 个头，再跑 MHA
   group_size = Nh_q // Nh_kv
   k_ref = k.repeat_interleave(group_size, dim=1)
   v_ref = v.repeat_interleave(group_size, dim=1)
   ref = F.scaled_dot_product_attention(q, k_ref, v_ref)
   print(f"vs SDPA max_abs_err={(out - ref).abs().max().item():.4e}")
   ```

3. **观察**：`out.shape` 应为 `(1, 32, 1024, 512)`；`max_abs_err` 应在 bf16 量级（通常 1e-2 ~ 1e-1 量级，取决于硬件与数值精度）。
4. **预期结果**：输出形状正确，误差落在 fp16/bf16 的正常容差范围内，与官方示例和 CI 测试结论一致。
5. 若本地无 CUDA 或无大 D GPU：可把 `device="cuda"` 去掉观察形状校验与报错路径，但实际数值比较「待本地验证（需 CUDA + 大 D GPU）」。

#### 4.4.5 小练习与答案

**练习 1**：如果把上面的 `repeat_interleave` 误写成 `k.repeat(group_size, dim=1)`（假设有这样语义的调用，整体平铺），结果会对齐吗？

**答**：不会。`repeat` 整体平铺得到的头排列是交错的（`KV0,KV1,...,KV0,KV1,...`），与 FFPA 的连续分组（`KV0,KV0,...,KV1,KV1,...`）不一致，逐头对应不上，`max_abs_err` 会很大。必须用 `repeat_interleave`。

**练习 2**：参考实现里为什么要 `q` 不复制、只复制 K/V？

**答**：因为 FFPA 输出的头数就是 `Nh_q`，参考实现要和它逐头对齐，Q 必须保持 `Nh_q` 个头不变；只需要把数量较少的 K/V 头「撑开」到 `Nh_q`，让每个 Query 头都能在自己的参考实现里取到对应的 KV 头。

---

## 5. 综合实践

把本讲内容串起来，完成下面这个「GQA → MQA」对比小任务：

1. **复现官方 GQA 示例**：按 4.4.4 跑通 `Nh_q=32, Nh_kv=8, D=512` 的用例，记录 `max_abs_err`。
2. **改造为 MQA**：只改 `Nh_kv = 1`（其它不变），保持 `enable_gqa=True`，再次调用 `ffpa_attn_func`。
   - 用 `repeat_interleave(32, dim=1)` 生成 MQA 参考，比较 `max_abs_err`。
   - 确认输出形状仍是 `(1, 32, 1024, 512)`。
3. **故意制造一次错误**：在 MQA 用例里去掉 `enable_gqa=True`，确认抛出 4.3 节那条 `ValueError`（`enable_gqa=False but ...`）。
4. **回答一个问题**：同样 `Nh_q=32`，GQA（`Nh_kv=8`）与 MQA（`Nh_kv=1`）相比，KV 的头数缩减到原来的几分之几？这为什么能省显存/带宽？

**参考结论**：MQA 把 KV 头数从 8 降到 1，相对 GQA 再省 8 倍 K/V 体积（相对 MHA 省 32 倍）。因为解码阶段每生成一个 token 都要把整套 K/V（KV cache）读一遍，K/V 越少，显存占用和访存带宽越小——这正是 GQA/MQA 在推理场景受欢迎的根本原因。代码运行结果「待本地验证（需 CUDA + 大 D GPU）」。

## 6. 本讲小结

- GQA/MQA 让多个 Query 头共用一组 K/V；`group_size = Nh_q / Nh_kv`，MQA 是 `Nh_kv == 1` 的特例，MHA 是 `group_size == 1` 的特例。
- FFPA 用**连续分组**约定：`h_kv = h_q // group_size`，这条映射在 Triton kernel 里直接用指针偏移实现，无需复制 K/V（`_ffpa_fwd.py:367-375`）。
- `enable_gqa` 默认 `False`，与 SDPA 严格对齐；头数不等时必须显式传 `True`，否则在 `normalize_inputs` 里抛 `ValueError`（`functional.py:613-618`）。
- 输入硬约束：K/V 头数一致、`Nh_q % Nh_kv == 0`、K/V 序列长度一致、三者 head_dim 一致（`functional.py:596-609`）。
- 参考实现标准写法：`k.repeat_interleave(group_size, dim=1)` 后跑 MHA，与 FFPA 输出逐头对齐（`docs/index.md:104-131`、`tests/test_ffpa_fwd.py:961-977`）。
- GQA 本身不触发 SDPA 回退；是否回退仍由 D、Nq、Nkv 决定（见 u2-l1、u2-l2）。构造示例时要选大 D、长序列才能真正落到 FFPA kernel。

## 7. 下一步学习建议

- 想把掩码叠加到 GQA 上？继续看 **u2-l3（因果掩码 is_causal 与可加 attn_mask）**，注意 `attn_mask` 的逻辑形状是 `[B, Nh_q, Nq, Nkv]`，头维按 Query 头数。
- 想理解 GQA 在「短 query / 长 KV」解码场景下的特殊路径？继续看 **u2-l2（self/cross/decode 注意力）**，那里讲了 Nq 很小时的 split-KV 路径。
- 想深入 kernel 如何在「不复制 K/V」的前提下高效完成分组计算？进入 **u4（Triton 后端前向）**，特别是 4.4 节会专门讲 GQA 头映射、attn_bias 广播等 kernel 内细节。
- 想了解 GQA 在 packed 变长（THD）接口里的形态？看 **u2-l5（ffpa_attn_varlen_func）**，注意 varlen 同样有 `enable_gqa` 开关，但目前仅 CuTeDSL 后端在 SM8x/SM90 大 D 上支持。
