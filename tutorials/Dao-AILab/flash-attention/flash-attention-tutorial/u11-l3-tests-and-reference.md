# 测试体系与参考实现

## 1. 本讲目标

FlashAttention-4（FA4）的 kernel 是「用 Python + CuTeDSL 写、运行时 JIT 编译成 PTX/CUBIN」的 GPU 代码。它的数学正确性靠什么来保证？答案就是 `tests/cute/` 下的一整套测试体系。

学完本讲，你应当能够：

1. 读懂 `attention_ref` 这个「真值实现」是如何用朴素 PyTorch 把注意力一步步算出来的，以及它为什么是所有断言的标尺。
2. 读懂 `test_flash_attn.py` 是怎么用 `@pytest.mark.parametrize` 把一个测试函数撑成上千个用例的，覆盖 dtype / head_dim / seqlen / causal / GQA / MQA 等维度。
3. 理解「架构能力守卫」类用例：例如为什么会有 `test_flash_attn_sm120_rejects_splitkv`、`test_flash_attn_invalid_head_dim` 这类「期望抛异常」的测试。
4. 掌握「两段式测试」流程：第一段用 FakeTensorMode 在不需要 GPU 内存的情况下并行编译所有 kernel，第二段用缓存好的 kernel 真正跑测试；以及测试为何要 OOM 重试。

本讲与 [u2-l1 公共 API](u2-l1-public-api.md) 直接承接：那里讲「接口有哪些参数」，这里讲「这些参数怎么被验证」。也用到 [u11-l1 JIT 与缓存](u11-l1-jit-and-cache.md) 中关于 `compile_key`、`is_fake_mode()` 的结论。

## 2. 前置知识

- **PyTorch 的 scaled-dot-product attention**：注意力公式 \(\text{softmax}(QK^\top/\sqrt{d})V\)。如果你已经读过 [u1-l1](u1-l1-what-is-flashattention.md)，这里的直觉你已经具备了。
- **pytest 参数化**：`@pytest.mark.parametrize("name", [v1, v2])` 会让被装饰的测试函数为每个值各跑一次；多个 parametrize 叠加时是笛卡尔积。
- **PyTorch FakeTensorMode**：一种「假张量」模式，张量只保留 shape/dtype 等元数据、不真正分配显存也不执行计算。FA4 借它来做「免 GPU」编译（见 [u11-l1](u11-l1-jit-and-cache.md)）。
- **架构（arch / SM 版本）**：FA4 用整数 arch（80=Ampere、90=Hopper、100=Blackwell、120=消费级 Blackwell）分发到不同 kernel 类，见 [u2-l2](u2-l2-arch-dispatch-and-config.md)。
- **相对容差断言**：FA4 测试不追求「误差为 0」，而是追求「FA4 的误差不超过朴素 PyTorch 实现误差的若干倍」。这一点很关键，后面会反复用到。

一个贯穿全讲的术语是 **参考实现（reference / ref）**：指用最直白、无优化的 PyTorch 写出来的「数学上正确」的注意力，它产出的 `out_ref` 是衡量 FA4 kernel 输出 `out` 的标尺。

## 3. 本讲源码地图

本讲只围绕三个真实文件展开：

| 文件 | 作用 |
| --- | --- |
| [flash_attn/cute/testing.py](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/testing.py) | 提供**参考实现** `attention_ref`、**数据构造** `generate_qkv` / `generate_random_padding_mask`、以及 FakeTensor 辅助 `maybe_fake_tensor_mode` / `is_fake_mode`。 |
| [tests/cute/test_flash_attn.py](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/tests/cute/test_flash_attn.py) | 主测试文件，含 `test_flash_attn_output`（巨型参数化输出测试）、架构守卫用例、反向梯度测试等。 |
| [tests/cute/conftest.py](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/tests/cute/conftest.py) | pytest 配置钩子：按 pytest-xdist worker 分配 GPU、收集测试计数。 |

另外会引用一处 kernel 侧的约束点：

| 文件 | 作用 |
| --- | --- |
| [flash_attn/cute/interface.py](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/interface.py) | 公共 API 与运行时分发；其中有一行 `assert num_splits == 1, "SM120 forward only supports num_splits=1"`，是本讲架构守卫用例的「事实依据」。 |

## 4. 核心概念与源码讲解

### 4.1 参考实现 attention_ref：FA4 的「数学真值」从哪来

#### 4.1.1 概念说明

GPU kernel 写完之后，怎么证明它算得对？最稳的办法是：另写一个**完全没有优化、完全照着数学公式来**的实现，把它当作「真值（ground truth）」，再去比较 kernel 的输出和真值之间的差距。这个真值实现就是 `attention_ref`。

`attention_ref` 的设计哲学有三点：

1. **照公式直算**：老老实实算 \(S = QK^\top \cdot \text{scale}\)、套各种掩码、做 `softmax`、再乘 \(V\)。不做分块、不做在线 softmax、不省显存——它甚至会把输入 `upcast` 到 float32 再算，目的是把浮点误差降到最低，从而当一个「尽可能准」的标尺。
2. **功能要全**：FA4 支持的因果、滑窗、softcap、块稀疏、top-k gather、learnable_sink、MLA 的 qv 项……`attention_ref` 都得能算，否则就没法给那些路径当参考。
3. **同时产出两个参考**：一个尽量准的 `out_ref`（upcast=True），一个「故意模拟 fp16/bf16 误差」的 `out_pt`（upcast=False）。后者用来定义「合理容差」。

#### 4.1.2 核心流程

`attention_ref` 的执行步骤（伪代码）：

```
输入: q, k, v, 可选 qv(MLA), 掩码参数, softcap, window_size, ...
1. (可选) upcast 到 float32                # 尽量准
2. (可选) 按 qv 把 GQA 的 k,v 复制到与 q 同头数
3. softmax_scale = 1/sqrt(d) 或 1/sqrt(d+dv)  # MLA 时分母加 dv
4. scores = einsum("bthd,bshd->bhts", q*scale, k)   # 即 QK^T
   若有 qv: scores += einsum("bthd,bshd->bhts", qv*scale, v)
5. 若 softcap>0: scores = tanh(scores/softcap)*softcap
6. 套各种掩码: key_padding / local_window / chunk / top-k gather -> -inf
7. 若有 attn_bias: scores += attn_bias
8. lse = logsumexp(scores, dim=-1)         # 参考的 LSE
9. attention = softmax(scores)              # 普通版或带 learnable_sink 版
10. 若有 dropout_mask: attention drop
11. output = einsum("bhts,bshd->bthd", attention, v)
12. 返回 (output, attention) 或 (output, attention, lse)
```

其中第 7、8 步是关键：所有掩码都在 **softmax 之前**把非法位置改成 \(-\infty\)，这正是 [u3-l1 AttentionMask](u3-l1-attention-mask.md) 讲的「掩码本质」的宿主侧镜像。

#### 4.1.3 源码精读

函数签名与开头，看它如何处理 upcast 与 MLA 的 qv 项：

[flash_attn/cute/testing.py:326-388](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/testing.py#L326-L388) —— 这里 `attention_ref` 把 q/k/v/qv 统一 upcast 到 float32，算出 `softmax_scale`（MLA 路径下分母是 `d+dv`），再用 `einsum("bthd,bshd->bhts", ...)` 直接算出 `scores`，并把 qv 项 `qv_scores` 累加进去。

softcap 与掩码应用，注意 softcap 是在 softmax 之前作用：

[flash_attn/cute/testing.py:389-429](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/testing.py#L389-L429) —— `scores = torch.tanh(scores / softcap) * softcap`（对应 [u4-l2 score_mod](u4-l2-score-mod.md) 里内置 softcap），随后 `local_mask`、`gather_kv_indices` 的 top-k 掩码都用 `masked_fill_(..., float("-inf"))` 实现，与 kernel 侧把分数置 \(-\infty\) 完全一致。

最终 softmax 与输出，并可选返回 LSE：

[flash_attn/cute/testing.py:432-465](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/testing.py#L432-L465) —— `lse = torch.logsumexp(scores, dim=-1)` 产出参考 LSE（形状 `[b, h, t]`，对应 [u2-l1](u2-l1-public-api.md) 讲的 LSE 用途），最后 `output = einsum("bhts,bshd->bthd", attention_drop, v)`。当 `return_lse=True` 时多返回一个 `lse`。

#### 4.1.4 代码实践

1. **实践目标**：亲手调用 `attention_ref`，验证它就是一个朴素 PyTorch 注意力，并体会 upcast 带来的精度差异。
2. **操作步骤**（示例代码，需在装好 FA4 的环境里运行）：

   ```python
   # 示例代码：直接调用参考实现
   import torch
   from flash_attn.cute.testing import attention_ref

   torch.manual_seed(0)
   b, s, h, d = 2, 512, 8, 64
   q = torch.randn(b, s, h, d, device="cuda", dtype=torch.bfloat16)
   k = torch.randn(b, s, h, d, device="cuda", dtype=torch.bfloat16)
   v = torch.randn(b, s, h, d, device="cuda", dtype=torch.bfloat16)

   # 尽量准的参考（upcast=True）
   out_ref, attn_ref = attention_ref(q, k, v, causal=True)
   # 故意不 upcast 的参考（模拟低精度误差）
   out_pt, attn_pt = attention_ref(q, k, v, causal=True, upcast=False, reorder_ops=True)

   print("ref vs pt max diff:", (out_ref - out_pt).abs().max().item())
   ```

3. **需要观察的现象**：`out_ref` 与 `out_pt` 之间存在一个非零的 `max diff`，这个差就是 bf16 计算引入的「数值噪声地板」。
4. **预期结果**：`max diff` 是一个小正数（量级取决于 bf16，通常在 1e-2 ~ 1e-1 量级，具体待本地验证）。这个值在下一节就是判定 FA4 kernel 是否合格的「容差基准」。
5. 若无法在本地 GPU 运行，明确标注「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `attention_ref` 默认 `upcast=True`？把它关掉（`upcast=False`）会怎样？
**答案**：upcast 到 float32 能让参考实现尽可能接近「真实数学值」，从而当一个可信标尺。关掉后，参考实现本身就在 bf16/fp16 下做矩阵乘，自身带有较大舍入误差，再拿它当标尺就不准了——但测试里恰恰需要这个「带误差的参考 `out_pt`」来定义合理容差（见 4.2）。

**练习 2**：`attention_ref` 里 `softmax_scale = 1/sqrt(d + dv)`，为什么 MLA 路径（有 `qv`）分母要加 `dv`？
**答案**：MLA 的 absorbed 形式里，得分是 \(QK^\top + Q_v V^\top\)，参与缩放的特征维度既有 q 的 d，也有 qv 的 dv，故按 \(\sqrt{d+dv}\) 缩放，对应 [u10-l2 MLA](u10-l2-mla.md) 的吸收公式。

---

### 4.2 参数化测试维度与「相对容差」断言

#### 4.2.1 概念说明

`test_flash_attn_output` 是整个 FA4 测试套件的「主战场」。它本身只是一个函数，但被十几个 `@pytest.mark.parametrize` 叠加装饰后，会展开成成百上千个独立用例。每条用例都做同一件事：

> 用同一组 q/k/v，分别跑 **(a) FA4 kernel**、**(b) 尽量准的参考 `out_ref`**、**(c) 故意带误差的参考 `out_pt`**，然后断言 **(a) 与 (b) 的误差不超过 (c) 与 (b) 的误差的某个倍数**。

这就是 FA4 测试最核心的设计——**相对容差断言**。它不追求 FA4 输出与真值「绝对接近」（那对 bf16 不现实），而是追求「FA4 的数值稳定性不输给一个朴素 PyTorch 实现」。

数据怎么来？由 `generate_qkv` 构造。它能把 `(batch, seqlen, nheads, head_dim)` 的定长 q/k/v，连同可选的 padding mask，转换成 FA4 与参考都需要的各种排布（含变长打包的 `cu_seqlens`、`unpad` 后的 1D 张量、`kvpacked`/`qkvpacked` 等）。

#### 4.2.2 核心流程

一条 `test_flash_attn_output` 用例的生命周期：

```
1. pytest 按参数化注入 (seqlen_q, seqlen_k, d, causal, softcap, mha_type, dtype, ...)
2. 构造 q_ref/k_ref/v_ref（randn，若 softcap>0 则缩小 q 以落入 cap 范围）
3. out_ref, attn_ref = attention_ref(..., upcast=True)            # 标尺
4. out_pt,  attn_pt  = attention_ref(..., upcast=False, reorder_ops=True)  # 带误差参考
5. fwd_atol = 2*(out_ref + 0.3 - 0.3 - out_ref).abs().max()       # bf16 噪声地板
6. for (pack_gqa, num_splits) in 组合:
       跳过当前架构不支持的组合 (如 SM90/SM120 跳过 num_splits>1)
       out, lse = flash_attn_func(q,k,v, causal=..., softcap=..., pack_gqa=..., num_splits=...)
       assert (out - out_ref).abs().max() <= rtol*(out_pt - out_ref).abs().max() + fwd_atol
7. (可选) 反向: torch.autograd.grad(out, (q,k,v), g) 与参考梯度对比，同样相对容差
```

第 5 步里那个看起来奇怪的 `out_ref + 0.3 - 0.3 - out_ref`，是在 bf16/fp16 张量上做一次「无意义加减」——目的是**测量该 dtype 下做一次浮点运算能引入多大的噪声**，把它当成绝对容差 `atol`。

#### 4.2.3 源码精读

巨型参数化装饰器栈，覆盖了几乎所有 FA4 的对外维度：

[tests/cute/test_flash_attn.py:100-158](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/tests/cute/test_flash_attn.py#L100-L158) —— 逐项可见：`dtype`（默认只 bf16）、`mha_type in {mha, mqa, gqa}`、`has_learnable_sink`、`deterministic`、`softcap in {0, 15}`、`local_enum in {0,1,2,3}`（滑窗四种形态）、`causal`、`d in {64,96,128,192,256}`、以及一长串 `(seqlen_q, seqlen_k)` 组合（含 `(1,1)`、`(64,1)`、`(1023,1024)` 等边界尺寸）。注意最后两个装饰器：`@retry_on_oom` 与 `@maybe_fake_tensor_mode(USE_FAKE_TENSOR)`，它们分别对应 OOM 重试与两段式测试（见 4.4）。

两份参考实现并算，定义容差基准：

[tests/cute/test_flash_attn.py:276-328](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/tests/cute/test_flash_attn.py#L276-L328) —— `out_ref` 用默认 upcast=True，`out_pt` 显式 `upcast=False, reorder_ops=True`；随后 `fwd_atol = 2 * (out_ref + 0.3 - 0.3 - out_ref).abs().max().item()` 测出 dtype 噪声地板，并打印「Pytorch max diff」即 `(out_pt - out_ref)` 作为基准。

相对容差断言的核心一行：

[tests/cute/test_flash_attn.py:375-377](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/tests/cute/test_flash_attn.py#L375-L377) —— `assert (out - out_ref).abs().max().item() <= rtol * (out_pt - out_ref).abs().max().item() + fwd_atol`。含义就是：**FA4 的最大误差 ≤ `rtol` × 朴素 PyTorch 的最大误差 + 一个 dtype 噪声地板**。`rtol` 默认 2（有 softcap 时为 3），即允许 FA4 误差是朴素实现的两到三倍。

数据构造入口 `generate_qkv` 的签名与排布分支：

[flash_attn/cute/testing.py:127-201](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/testing.py#L127-L201) —— 它根据是否传入 `query_padding_mask` / `key_padding_mask` 决定走 `unpad_input`（变长打包，产出 `cu_seqlens`）还是简单 `rearrange`（定长），并支持 `kvpacked` / `qkvpacked` 两种打包模式，返回一连串「已 detach 并 requires_grad」的张量供反向测试。

#### 4.2.4 代码实践

1. **实践目标**：跑通本讲任务指定的单个用例，亲眼看到 `max diff`，并理解它为何能通过断言。
2. **操作步骤**：

   ```bash
   # 单个用例，-x 表示遇到失败立即停止
   pytest tests/cute/test_flash_attn.py -k 'test_flash_attn_output' -x
   ```

   若想更快地只跑一个具体参数组合（例如 causal、d=128），可以用更细的 `-k`：

   ```bash
   pytest tests/cute/test_flash_attn.py -k 'test_flash_attn_output and causal and 128-128' -x -s
   ```

   `-s` 关闭输出捕获，这样能看到测试里大量 `print(...)` 打印的 `Output max diff` / `Pytorch max diff`。
3. **需要观察的现象**：每条用例会打印形如：

   ```
   Pytorch max diff: 0.0xx      # 朴素参考与真值的误差（基准）
   Output max diff: 0.0xx       # FA4 kernel 与真值的误差
   ```
4. **预期结果**：`Output max diff` ≤ `rtol × Pytorch max diff + fwd_atol`，断言通过。把这两行的数字记下来，对照 `attention_ref` 说明误差为何在容忍范围内：因为 FA4 是**精确注意力**（见 [u4-l1 在线 Softmax](u4-l1-online-softmax.md)，分块 online softmax 数学上等价于完整 softmax），它与真值的差距只来自 bf16 舍入，量级与朴素 PyTorch 实现相当，故能落入相对容差内。
5. 若本机无相应 GPU 或 FA4 未装好，相关数值「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：断言里 `fwd_atol = 2 * (out_ref + 0.3 - 0.3 - out_ref).abs().max()` 为什么要这么写？直接写 `fwd_atol = 0` 行不行？
**答案**：这是在当前 dtype（如 bf16）上测量「一次加法 + 一次减法」引入的舍入噪声，作为绝对容差地板。直接写 0 会让断言对极小数值误差过敏——因为即便 FA4 与参考在数学上等价，bf16 下一次额外的加减就可能产生非零误差，需要这个地板来吸收。

**练习 2**：`local_enum in {0,1,2,3}` 代表什么？为什么 `local and causal` 要 `pytest.skip()`？
**答案**：`local_enum` 编码滑窗（local attention）的四种形态：0=不开滑窗，1=左右窗都开 `(left,right)`，2=只有右窗 `(None, -right)`，3=只有左窗 `(-left, None)`（见 [test_flash_attn.py:255-258](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/tests/cute/test_flash_attn.py#L255-L258)）。因果掩码与滑窗在该测试里是互斥的两种掩码来源，同时开语义混乱，故跳过。

**练习 3**：为什么 `d=256 and IS_SM100` 时要跳过 `learnable_sink / local / softcap / deterministic`？
**答案**：SM100 上 head_dim=256 走的是专用 2CTA kernel（见 [u8-l4 hd256 2CTA](u8-l4-hd256-2cta-kernel.md)），它为吞吐牺牲了部分特性、当前不支持这几项，故用 skip 守住已知边界。这是「能力裁剪在测试里的体现」。

---

### 4.3 架构能力守卫用例：IS_SM120 与 SplitKV / 头维校验

#### 4.3.1 概念说明

除了「算得对」，测试还要守住另一类边界：**某些参数组合在某些架构上根本不该被支持**。比如：

- SplitKV（把 KV 切多段并行）在 SM90（Hopper）和 SM120（消费级 Blackwell）上前向**不支持**，只能 `num_splits=1`。
- 某些 head_dim（如 4、148、288）在给定架构上**非法**，应当被 `_validate_head_dims` 拒绝。

这类「期望失败/期望抛异常」的测试叫**守卫用例（guard test）**。它们用 `pytest.raises(...)` 断言「调用确实抛了预期的异常」，或用 `pytest.mark.skipif(not IS_SM120, ...)` 限定只在特定架构上运行。它们的价值是：一旦将来有人误把这些约束去掉，测试会立刻红。

测试文件顶部用一组模块级常量探测当前硬件架构，供各类守卫与跳过逻辑复用：

```python
IS_SM90  = torch.cuda.get_device_capability()[0] == 9
IS_SM100 = torch.cuda.get_device_capability()[0] == 10
IS_SM120 = torch.cuda.get_device_capability()[0] == 12
```

注意 `get_device_capability()[0]` 返回的是 major SM 版本（9、10、12），与 [u2-l2](u2-l2-arch-dispatch-and-config.md) 里 `_get_device_arch()` 探测的整数 arch（90、100、120）一致。

#### 4.3.2 核心流程

守卫用例的两种典型写法：

```
(A) 期望抛异常型（test_flash_attn_sm120_rejects_splitkv / test_flash_attn_invalid_head_dim）
    @pytest.mark.skipif(not IS_SM120, ...)        # 只在目标架构上跑
    def test_...():
        with pytest.raises(AssertionError, match="..."):
            flash_attn_func(q, k, v, num_splits=3)   # 必须抛 AssertionError

(B) 组合内跳过型（test_flash_attn_output 内部）
    if (IS_SM90 or IS_SM120) and num_splits > 1:
        continue                                   # 这一组参数在当前架构上跳过
```

(A) 是「正向守卫」：它**保证约束确实存在**；(B) 是「负向让步」：它**避免在不支持的架构上跑出假失败**。两者一起，既守住约束又不误报。

#### 4.3.3 源码精读

架构探测常量与 SM120 SplitKV 守卫用例：

[tests/cute/test_flash_attn.py:82-96](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/tests/cute/test_flash_attn.py#L82-L96) —— 注释写明「SplitKV is not supported on SM90 or SM120」；`test_flash_attn_sm120_rejects_splitkv` 用 `@pytest.mark.skipif(not IS_SM120, ...)` 限定只在 SM120 上跑，并在 `with pytest.raises(AssertionError, match="SM120 forward only supports num_splits=1")` 里调用 `flash_attn_func(q, k, v, num_splits=3)`，断言它必抛 `AssertionError`。

守卫所对应的 kernel 侧约束点（守卫的「事实依据」）：

[flash_attn/cute/interface.py:567-570](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/interface.py#L567-L570) —— `if arch / 10 == 12: assert num_splits == 1, "SM120 forward only supports num_splits=1"`。注意它是 `assert`，所以抛的是 `AssertionError`，与守卫用例 `pytest.raises(AssertionError, ...)` 完全对得上。SM90 则在 dispatch 处拦截 SplitKV（见 [u7-l2](u7-l2-splitkv-and-combine.md)）。

头维校验守卫用例：

[tests/cute/test_flash_attn.py:2091-2102](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/tests/cute/test_flash_attn.py#L2091-L2102) —— `test_flash_attn_invalid_head_dim` 对 `head_dim in {4, 148, 288}` 断言 `flash_attn_func` 必抛 `AssertionError`，`match` 用 `re.escape(f"(head_dim, head_dim_v)=({head_dim}, {head_dim}) is not supported on SM")`，对应 [u2-l2](u2-l2-arch-dispatch-and-config.md) 里 `_validate_head_dims` 的早失败护栏。

组合内跳过 SplitKV 的负向让步：

[tests/cute/test_flash_attn.py:333-339](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/tests/cute/test_flash_attn.py#L333-L339) —— `num_splits_vals` 默认含 `[1, 3]`（受 `d < 192` 等条件限制），但在循环里 `if (IS_SM90 or IS_SM120) and num_splits > 1: continue`，从而在 Hopper / 消费级 Blackwell 上自动跳过 SplitKV 组合，避免假失败。

#### 4.3.4 代码实践

1. **实践目标**：阅读并（如在 SM120 上）运行 `test_flash_attn_sm120_rejects_splitkv`，说明为何 SM120 前向拒绝 `num_splits>1`。
2. **操作步骤**：
   - 阅读上面的 [interface.py:567-570](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/interface.py#L567-L570)，确认 SM120 上 SplitKV 被 `assert` 显式拒绝。
   - 若本机为 SM120，运行：

     ```bash
     pytest tests/cute/test_flash_attn.py::test_flash_attn_sm120_rejects_splitkv -v
     ```
   - 若本机不是 SM120，则该用例会被 `skipif` 跳过（输出 `SKIPPED`）；此时只能通过阅读源码理解。
3. **需要观察的现象**：在 SM120 上该用例 PASSED（因为它成功捕获到 `AssertionError`）；在非 SM120 上 SKIPPED。
4. **预期结果**：SM120 前向拒绝 `num_splits>1` 的原因——SM120（消费级 Blackwell）复用的是 Ampere 风格（sm_80）前向 kernel（见 [u6-l1](u6-l1-ampere-forward-kernel.md) 与 [u7-l1](u7-l1-pack-gqa.md)），该路径没有实现 SplitKV，故在入口处用 `assert` 把 `num_splits` 钉死为 1，连 `num_splits_heuristic` 的自动选择也一并禁掉（对比 [interface.py:567-570](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/interface.py#L567-L570)：SM120 走 `assert`，而其它架构走 `num_splits_heuristic`）。
5. 数值结果「待本地验证」（取决于本机架构）。

#### 4.3.5 小练习与答案

**练习 1**：守卫用例用 `pytest.raises(AssertionError, match=...)`。如果将来有人把 interface.py 里那行 `assert` 改成静默 `return`，这个用例会怎样？
**答案**：`flash_attn_func(..., num_splits=3)` 不再抛 `AssertionError`，`pytest.raises` 捕获不到匹配异常，用例会 FAIL。这正是守卫用例的价值——把「约束必须存在」变成可回归的断言。

**练习 2**：为什么 `test_flash_attn_sm120_rejects_splitkv` 用 `@pytest.mark.skipif(not IS_SM120, ...)`，而 `test_flash_attn_invalid_head_dim` 不需要类似限定？
**答案**：前者验证的是「SM120 专属」的行为（拒绝 SplitKV），只在 SM120 上有意义，所以在别的架构上必须 skip。后者验证的是 `_validate_head_dims` 对非法 head_dim 的拒绝，这个校验在所有架构上都生效（只是允许的维度集合不同），所以无需按架构跳过。

**练习 3**：`match="SM120 forward only supports num_splits=1"` 这段字符串从哪来？为什么它和 interface.py 里的 assert 文案必须一致？
**答案**：它来自 [interface.py:568](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/interface.py#L568) 的 `assert ..., "SM120 forward only supports num_splits=1"`。`pytest.raises(match=...)` 用正则匹配异常消息，文案不一致就会匹配失败。这把「约束文案」也纳入了回归保护。

---

### 4.4 两段式测试（FakeTensor 编译）与 OOM 重试

#### 4.4.1 概念说明

FA4 的测试有个绕不开的痛点：**编译 dominates（主导）测试时间**。每个新的 `compile_key`（dtype/head_dim/causal/tile/arch/score_mod 哈希……，见 [u11-l1](u11-l1-jit-and-cache.md)）都要把 kernel 从 CuTeDSL JIT 编译成 PTX→CUBIN，这一步极慢，而 GPU 执行反而很快。如果上千个参数化用例串行编译，测试会跑到天荒地老。

FA4 的解法是**两段式测试（fast two-pass）**：

- **第一段（编译段）**：开 `FLASH_ATTENTION_FAKE_TENSOR=1` 和磁盘缓存 `FLASH_ATTENTION_CUTE_DSL_CACHE_ENABLED=1`，用 pytest-xdist `-n 64` 起很多 worker 并行编译。FakeTensorMode 让 kernel 走「只编译、不分配显存、不执行」的路径（见 [u11-l1](u11-l1-jit-and-cache.md) 的 `if not is_fake_mode()` 守卫），所以不需要 GPU 内存也能编译。
- **第二段（执行段）**：关掉 FakeTensor（`FLASH_ATTENTION_FAKE_TENSOR=0`），保留磁盘缓存，用缓存好的 CUBIN 直接跑测试。

测试代码里通过两个工具把「FakeTensor 开关」接进每个用例：`USE_FAKE_TENSOR` 读环境变量，`maybe_fake_tensor_mode(USE_FAKE_TENSOR)` 装饰器在 FakeTensor 开启时把整个用例包进 `FakeTensorMode()` 上下文。

另一个工程问题是 **OOM（显存溢出）**。FA4 测试参数化里有 `seqlen=4096/4224`、`nheads=128` 等大尺寸，加上编译缓存可能占用显存，容易触发 `torch.OutOfMemoryError`。`retry_on_oom` 装饰器的策略是：清空编译缓存、`gc.collect()`、`torch.cuda.empty_cache()`，然后**重试一次**。

#### 4.4.2 核心流程

两段式的环境变量配合：

```
# 第一段：并行编译，不需要 GPU 内存
FLASH_ATTENTION_FAKE_TENSOR=1 FLASH_ATTENTION_CUTE_DSL_CACHE_ENABLED=1 \
  pytest -n 64 -x tests/cute/test_flash_attn.py

# 第二段：用缓存跑，需要 GPU
FLASH_ATTENTION_FAKE_TENSOR=0 FLASH_ATTENTION_CUTE_DSL_CACHE_ENABLED=1 \
  pytest -x tests/cute/test_flash_attn.py
```

测试函数侧的开关接线：

```
USE_FAKE_TENSOR = int(os.getenv("FLASH_ATTENTION_FAKE_TENSOR", 0)) == 1

@retry_on_oom
@maybe_fake_tensor_mode(USE_FAKE_TENSOR)
def test_flash_attn_output(...):
    ...
    out, lse = flash_attn_func(...)
    if is_fake_mode():
        continue          # FakeTensor 下跳过所有数据相关的后处理与断言
    assert (out - out_ref)... <= rtol * ... + fwd_atol
```

`maybe_fake_tensor_mode(True)` 会把 `test_flash_attn_output` 的执行整体包进 `FakeTensorMode()`，于是 `flash_attn_func` 内部的 `_flash_attn_fwd` 走「只编译不执行」分支（受 `is_fake_mode()` 守卫），用例随后用 `if is_fake_mode(): continue` 跳过所有依赖真实数值的后处理。

OOM 重试的判定：

```
try: func(...)
except torch.OutOfMemoryError as e:
    if "out of memory" in str(e).lower():
        _flash_attn_fwd.compile_cache.clear()   # 释放缓存的编译产物
        _flash_attn_bwd.compile_cache.clear()
        gc.collect(); torch.cuda.empty_cache()
        return func(...)                          # 重试一次
```

#### 4.4.3 源码精读

环境变量开关与架构常量（含 `USE_FAKE_TENSOR`、`DISABLE_SPLIT`）：

[tests/cute/test_flash_attn.py:78-87](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/tests/cute/test_flash_attn.py#L78-L87) —— `USE_FAKE_TENSOR = int(os.getenv("FLASH_ATTENTION_FAKE_TENSOR", 0)) == 1`，注释点明 FakeTensorMode 让 cutedsl 在不分配显存、不跑 kernel 的情况下编译。

`retry_on_oom` 装饰器：捕获 OOM、清缓存、重试一次：

[tests/cute/test_flash_attn.py:37-53](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/tests/cute/test_flash_attn.py#L37-L53) —— 关键是它清掉 `_flash_attn_fwd.compile_cache` 与 `_flash_attn_bwd.compile_cache`（这两个是 [u11-l1](u11-l1-jit-and-cache.md) 讲的进程内 `JITCache`），再 `gc.collect()` + `torch.cuda.empty_cache()`，然后重试。注意只重试一次：若是真 OOM 而非缓存占用，第二次仍会抛出。

FakeTensor 辅助函数：

[flash_attn/cute/testing.py:468-487](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/testing.py#L468-L487) —— `maybe_fake_tensor_mode(fake)` 是个装饰器工厂：`fake=True` 时用 `FakeTensorMode()` 上下文包住被装饰函数，`fake=False` 时用 `nullcontext()`（什么都不做）；`is_fake_mode()` 通过 `active_fake_mode() is not None` 判断当前是否在 FakeTensor 上下文里。

用例里两处 FakeTensor 早退：

[tests/cute/test_flash_attn.py:363-366](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/tests/cute/test_flash_attn.py#L363-L366) 与 [tests/cute/test_flash_attn.py:400-403](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/tests/cute/test_flash_attn.py#L400-L403) —— 注释写明「no more flash_attn cutedsl calls for the rest of the loop / skip data-dependent postprocessing」：FakeTensor 下既不能做依赖真实数据的 `(out - out_ref).abs().max()`，也没必要，因为这一段的目的就是「触发编译」，编译一完成就 `continue`。

conftest.py 的 pytest-xdist worker→GPU 分配：

[tests/cute/conftest.py:32-59](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/tests/cute/conftest.py#L32-L59) —— `pytest_configure` 在每个 worker 启动时按 `worker_num % len(gpu_ids)` 给该 worker 设置 `CUDA_VISIBLE_DEVICES`，让 64 个并行 worker 均匀摊到可见 GPU 上；为避免每个 worker 都跑一次昂贵的 `nvidia-smi`，由 worker_0 统一探测一次写入 `gpu_ids.json`，其余 worker 等待并读取。这是第一段并行编译能高效跑起来的前提。

#### 4.4.4 代码实践

1. **实践目标**：体验两段式测试流程，观察「编译段不需要 GPU 内存、执行段命中缓存秒回」的现象。
2. **操作步骤**（需要装好 FA4 与至少一块 GPU；无 GPU 时只能做编译段）：

   ```bash
   # 清掉旧缓存，确保第一段真的会编译
   rm -rf /tmp/${USER}/flash_attention_cute_dsl_cache/

   # 第一段：并行编译（-n 8 按本机 CPU 调整；可不要 GPU 内存）
   FLASH_ATTENTION_FAKE_TENSOR=1 FLASH_ATTENTION_CUTE_DSL_CACHE_ENABLED=1 \
     pytest -n 8 -x tests/cute/test_flash_attn.py -k 'test_flash_attn_output and 128-128'

   # 第二段：用缓存执行（需要 GPU）
   FLASH_ATTENTION_FAKE_TENSOR=0 FLASH_ATTENTION_CUTE_DSL_CACHE_ENABLED=1 \
     pytest -x tests/cute/test_flash_attn.py -k 'test_flash_attn_output and 128-128' -s
   ```
3. **需要观察的现象**：第一段即使没有可用 GPU 内存也能跑过（因为 `is_fake_mode()` 让 kernel 不执行）；第二段命中磁盘缓存后，单条用例的「编译」几乎瞬时，主要耗时变成 GPU 执行。
4. **预期结果**：第二段的 `Output max diff` 打印与 4.2 节一致，断言通过。若把第二段的 `FLASH_ATTENTION_CUTE_DSL_CACHE_ENABLED` 关掉、又清了缓存，会看到首次调用很慢（即时编译）。
5. 若本机无 GPU，第一段行为「待本地验证」，第二段无法运行。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `retry_on_oom` 在重试前要清 `_flash_attn_fwd.compile_cache`？
**答案**：进程内 `JITCache`（见 [u11-l1](u11-l1-jit-and-cache.md)）会持有已编译 kernel 的 CUDA 模块与显存，累积多了本身就可能挤占显存导致 OOM。清掉它再 `empty_cache()` 释放显存，给重试腾出空间。注意它不清磁盘缓存，所以重试时仍能从磁盘命中编译产物，不会重新编译。

**练习 2**：`maybe_fake_tensor_mode(False)` 用的是 `nullcontext()`，这有什么用？
**答案**：让同一个测试函数在「编译段（fake=True）」和「执行段（fake=False）」之间无缝切换，无需写两套代码。`fake=False` 时 `nullcontext()` 不改变任何行为，用例就按正常路径真正跑 GPU。

**练习 3**：FakeTensor 模式下，为什么用例里 `(out - out_ref).abs().max().item()` 这类代码必须用 `if is_fake_mode(): continue` 跳过？
**答案**：FakeTensor 只保留张量的 shape/dtype 元数据，没有真实数值，任何「数据相关」的操作（`.max().item()`、`assert 误差`）都无法执行。编译段的目的只是触发 `cute.compile` 生成并缓存 CUBIN，所以一旦 kernel 调用完成（编译已发生），就应立刻跳过所有数据后处理。

---

## 5. 综合实践

把本讲四块知识串起来，完成下面这个「阅读 + 运行」综合任务：

**任务**：为 FA4 写一条新的守卫用例，验证「在 SM90 上 `flash_attn_func(..., num_splits=3)` 不会成功启用 SplitKV」。

步骤建议：

1. **阅读定位**：先读 [test_flash_attn.py:82-96](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/tests/cute/test_flash_attn.py#L82-L96) 的 `test_flash_attn_sm120_rejects_splitkv`，把它当作模板。
2. **弄清 SM90 的拦截方式**：SM120 是在 interface.py 用 `assert` 拒绝（抛 `AssertionError`），而 SM90 是在 dispatch 处拦截（见 [u7-l2](u7-l2-splitkv-and-combine.md)）。先确认 SM90 上 `num_splits>1` 到底会抛什么、还是被静默改回 1——这决定了你该用 `pytest.raises(...)` 还是断言「最终生效的 num_splits==1」。（提示：可用 `CUTE_DSL_KEEP_PTX=1` 或加日志跟踪；结论待本地验证。）
3. **写用例**：参考 4.3 的写法，用 `@pytest.mark.skipif(not IS_SM90, ...)` 限定架构，在用例体内调用 `flash_attn_func(q, k, v, num_splits=3)` 并断言期望行为。
4. **两段式跑通**：用 4.4 的两段式流程编译并执行你的新用例，确认它在 SM90 上 PASSED、在非 SM90 上 SKIPPED。
5. **对照参考**：运行 `pytest tests/cute/test_flash_attn.py -k 'test_flash_attn_output' -x -s`，挑一条 `num_splits=1` 的用例，记录 `Output max diff` 与 `Pytorch max diff`，用 4.1 / 4.2 的相对容差理论解释它为何通过。

这个任务同时用到：参考实现（4.1）、参数化与相对容差（4.2）、架构守卫（4.3）、两段式与重试（4.4）。

## 6. 本讲小结

- FA4 的数学正确性由 [testing.py](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/testing.py) 的 **`attention_ref`** 兜底：它用朴素 PyTorch、可 upcast 到 fp32、覆盖因果/滑窗/softcap/top-k/sink/MLA 全功能，产出 `out_ref`（准）与 `out_pt`（带 dtype 误差）两份参考。
- [test_flash_attn.py](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/tests/cute/test_flash_attn.py) 用十几个 `@parametrize` 把 `test_flash_attn_output` 撑成上千用例，维度覆盖 dtype / head_dim / seqlen / causal / GQA-MQA / softcap / local / deterministic 等。
- 断言采用**相对容差**：`(out - out_ref).abs().max() <= rtol * (out_pt - out_ref).abs().max() + atol`，即「FA4 误差不劣于朴素 PyTorch 实现的若干倍」，而非绝对为 0。
- **架构守卫用例**用 `pytest.raises(AssertionError, match=...)` 锁住「约束必须存在」，如 SM120 拒绝 `num_splits>1`（对应 [interface.py:567-570](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/interface.py#L567-L570)）、非法 head_dim 被 `_validate_head_dims` 拒绝。
- **两段式测试**靠 `FLASH_ATTENTION_FAKE_TENSOR` + `FLASH_ATTENTION_CUTE_DSL_CACHE_ENABLED` 把「并行编译」与「缓存执行」分开，绕开「编译 dominates 时间」的痛点；`maybe_fake_tensor_mode` / `is_fake_mode` 是测试侧的开关。
- `retry_on_oom` 在 OOM 时清进程内 `compile_cache` 并重试一次，`conftest.py` 按 xdist worker 均匀分配 GPU，两者共同保证大尺寸参数化用例能稳定跑完。

## 7. 下一步学习建议

- 想理解「缓存键怎么决定重编译」，接 [u11-l1 JIT 编译与缓存机制](u11-l1-jit-and-cache.md)，把本讲反复出现的 `compile_key`、`compile_cache`、`is_fake_mode()` 串起来。
- 想理解「为什么改 causal / head_dim 会触发重编译」，接 [u11-l2 Constexpr 特化与 @cute.jit 注入](u11-l2-constexpr-specialization.md)。
- 想看更专门的测试维度，可读 `tests/cute/test_flash_attn_varlen.py`（变长）、`tests/cute/test_mask_mod.py` / `test_score_mod.py`（用户自定义回调）、`tests/cute/test_block_sparsity.py`（块稀疏），它们的组织方式与本讲同构。
- 想理解性能如何被测量（而非正确性），接 [u11-l4 性能基准与配置搜索](u11-l4-benchmark-and-config-search.md)。
- 想深入「kernel 跑挂了怎么排查」，接 [u11-l5 GPU Kernel 调试与 PTX/SASS](u11-l5-debugging-ptx-sass.md)。
