# flash_attn_func 公共 API 详解

## 1. 本讲目标

学完本讲后，你应当能够：

- 说出 `flash_attn_func` 与 `flash_attn_varlen_func` 各参数的语义、默认值与取值约束；
- 区分「面向用户的参数」与「内部 tile/线程配置」的边界，理解 tile 尺寸如何被自动选定；
- 准确描述返回值 `(out, lse)` 的形状、dtype，以及 `lse`（log-sum-exp）何时为 `None`、何时被分配；
- 用 `lse` 手动还原 softmax 的归一化因子，并验证它与输出 `out` 的一致性；
- 判断哪些参数的改变会触发 kernel 的重新编译（进入缓存键），哪些只是运行时标量。

## 2. 前置知识

本讲假设你已学完 [u1-l3 安装并第一次调用 FA4](u1-l3-install-and-first-run.md)，知道：

- FA4 的对外入口只有两个：`flash_attn_func` 与 `flash_attn_varlen_func`，从 `flash_attn.cute` 导入；
- 调用形式是 `out, lse = flash_attn_func(q, k, v, ...)`，**始终返回二元组**；
- 输入张量布局为 `(batch, seqlen, nheads, head_dim)`，最后一维连续、16 字节对齐。

补充三个本讲要用到的基础概念：

- **log-sum-exp（LSE）**：对一个向量 \(x\)，定义 \(\mathrm{lse}(x)=\log\sum_j \exp(x_j)\)。它就是 softmax 分母的对数，数值稳定，是 online softmax 的天然产物。FA 把每一行 Q 的 LSE 一并返回，供反向传播与 SplitKV 合并使用。
- **torch.autograd.Function**：PyTorch 自定义可微算子的标准容器，含静态方法 `forward` / `backward`。FA 的两个公共函数都是对 `FlashAttnFunc.apply(...)` / `FlashAttnVarlenFunc.apply(...)` 的薄包装，这样它们才能挂进 autograd 图参与反向。
- **JIT 编译缓存键**：FA4 的 kernel 在运行时由 CuTeDSL 编译，昂贵的编译结果按一个 `compile_key` 元组缓存。哪个参数进了 `compile_key`，改变它就会重编译；没进的（如 `softmax_scale`）只是运行时传进去的标量，改它不重编译。这条规律贯穿全讲。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `flash_attn/cute/__init__.py` | 子包入口，导出两个公共函数并声明 `__all__`。 |
| `flash_attn/cute/interface.py` | 本讲主角：公共 API、autograd Function、内部 `_flash_attn_fwd` 调度器与编译缓存键。 |
| `flash_attn/cute/testing.py` | 提供 `attention_ref` 参考实现，本讲代码实践用它做数值对照。 |

> 说明：`interface.py` 是一个大文件（约 3000 行），但「公共 API」只占末尾一小段（`flash_attn_func` / `flash_attn_varlen_func`）。它们都委托给同一套内部前向函数 `_flash_attn_fwd`，因此本讲会顺着这条调用链往下读几层。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

1. **标准与变长接口签名**——两个函数长什么样、差在哪。
2. **关键参数语义**——逐个参数讲清含义、默认值、约束与组合规则。
3. **返回值 O 与 LSE**——形状、dtype、`None` 条件，以及 LSE 与输出的数学一致性。

### 4.1 标准与变长接口签名

#### 4.1.1 概念说明

FA4 对外只暴露两个函数，二者**数学等价**，区别只在「batch 维怎么组织」：

- `flash_attn_func`：处理**等长定长**输入，每条序列长度相同，Q/K/V 都是 4D `(batch, seqlen, nheads, head_dim)`。
- `flash_attn_varlen_func`：处理**变长 / 分页**输入。多条不等长序列可紧凑拼成 1D（用 `cu_seqlens` 描述每条的起止），还支持 `page_table` 分页 KV cache。它是个「超集」——把 `flash_attn_func` 能做的事全都做了，还多出 varlen/paged 的开关。

公共层非常薄：两个函数都只是把参数转发给对应的 `torch.autograd.Function`，由后者在 `forward` 里调用内部 `_flash_attn_fwd`。这条「用户函数 → autograd Function → 内部前向」的三层结构，是理解 FA4 调用链的起点。

#### 4.1.2 核心流程

```
flash_attn_func(q, k, v, ...)                # 用户层：只是参数转发
        │  return FlashAttnFunc.apply(...)
        ▼
FlashAttnFunc.forward(ctx, q, k, v, ...)      # autograd 层：保存反向所需上下文
        │  out, lse, p, row_max = _flash_attn_fwd(...)
        │  ctx.save_for_backward(q, k, v, out, lse, ...)
        ▼
_flash_attn_fwd(q, k, v, ...)                # 调度层：校验、选架构、选 tile、JIT 编译、启动 kernel
        │  return out, lse, p, row_max
```

变长版本把同一张图里的 `FlashAttnFunc` 换成 `FlashAttnVarlenFunc`，并多传 `cu_seqlens_*` / `page_table` 等。

#### 4.1.3 源码精读

**子包导出**——`flash_attn.cute` 只导出这两个名字：

[flash_attn/cute/__init__.py:10-18](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/__init__.py#L10-L18) — 从 `.interface` 导入两个公共函数并写入 `__all__`，确认了「对外只有这两个」。

**标准接口签名**——`flash_attn_func` 的全部公开参数：

[flash_attn/cute/interface.py:2709-2731](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/interface.py#L2709-L2731) — 注意它**没有** `m_block_size` / `n_block_size` / `num_threads`，这些 tile/线程配置由内部按架构自动选定（见 4.2.3）。

**变长接口签名**——多了 `cu_seqlens_*`、`page_table`、`max/min_seqlen` 等：

[flash_attn/cute/interface.py:2757-2786](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/interface.py#L2757-L2786) — 参数比标准版多一截，正是这些「序列长度描述符」让一个 batch 能容纳不等长序列。

**变长版的张量形状文档**（强烈建议读这段 docstring）：

[flash_attn/cute/interface.py:2787-2814](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/interface.py#L2787-L2814) — 逐一张量写明了 varlen（`total_q` 前缀）与定长（`batch, seqlen` 前缀）两种布局，以及 `lse` 返回形状的两种排列。

**autograd 转发**——标准版 `forward` 如何调内部前向并保存上下文：

[flash_attn/cute/interface.py:2453-2488](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/interface.py#L2453-L2488) — `forward` 把 `window_size` 拆成 `window_size_left/right` 传给 `_flash_attn_fwd`，用 `save_for_backward` 存下反向所需张量，最终 `return out, lse`。这就是「始终返回二元组」的来源。

**内部前向签名**——两个公共函数最终汇聚到的地方：

[flash_attn/cute/interface.py:297-335](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/interface.py#L297-L335) — 这里才会出现 `tile_mn`、`num_threads`、`_arch` 等「内部旋钮」，公共 API 不直接暴露它们。

#### 4.1.4 代码实践（源码阅读型）

**实践目标**：亲眼确认「用户函数 → autograd Function → 内部前向」三层调用链，并区分公共 API 与内部签名各自暴露的参数集合。

**操作步骤**：

1. 打开 `flash_attn/cute/interface.py`，分别定位 `flash_attn_func`（约 2709 行）与 `_flash_attn_fwd`（约 297 行）。
2. 把两个函数的**参数名**抄成两列，做一次差集对照。
3. 回答：哪些参数只在 `_flash_attn_fwd` 里出现、却被公共 API「藏」起来了？

**需要观察的现象**：公共 API 缺少 `tile_mn`、`mma_pv_is_rs`、`intra_wg_overlap`、`num_threads`、`_arch`、`return_lse` 之外的多个内部旋钮——它们都由调度层按架构与 hdim 自动决定（见 4.2.3）。

**预期结果**：你应得到一张「公共参数 vs 内部参数」对照表；公共层做的是「语义」，内部层做的是「性能旋钮」。

> 待本地验证：若你在交互式 Python 里 `import inspect; inspect.signature(flash_attn_func)`，打印出的签名应与源码 2709–2731 行一致，且确实不含 tile/线程参数。

#### 4.1.5 小练习与答案

**练习 1**：`flash_attn_func` 和 `flash_attn_varlen_func` 谁是另一个的超集？为什么定长场景也能用变长版？

> **答案**：变长版是超集。把 `cu_seqlens` 等置空、给一条「假变长」输入即可退化成定长；定长版只是去掉了 varlen/paged 相关参数的便捷入口。

**练习 2**：为什么两个公共函数都要包一层 `torch.autograd.Function`，而不是直接调 `_flash_attn_fwd`？

> **答案**：`autograd.Function` 提供 `forward`/`backward`，让 FA 能挂进 PyTorch 的自动微分图，在 `loss.backward()` 时自动调用 FA 的反向 kernel。直接调 `_flash_attn_fwd` 则只做前向、不会产生梯度。

---

### 4.2 关键参数语义

#### 4.2.1 概念说明

公共 API 参数虽多，但可以归成五类，记起来不乱：

| 类别 | 参数 | 一句话 |
| --- | --- | --- |
| 输入张量 | `q, k, v, qv, gather_kv_indices` | Q/K/V 及可选的 MLA 吸收式 `qv`、top-k 稀疏索引 |
| 形状/缩放 | `softmax_scale` | 缩放系数，缺省 \(1/\sqrt{d}\) |
| 掩码族 | `causal`, `window_size`, `mask_mod` | 因果、滑窗、自定义位置掩码（三选一组合） |
| 打分修改 | `softcap`, `score_mod`, `score_mod_bwd` | 对注意力分数做非线性变换 |
| 性能/特性 | `num_splits`, `pack_gqa`, `deterministic`, `return_lse`, `block_sparse_tensors` | SplitKV、GQA 打包、确定性反向、是否返回 LSE、块稀疏 |

关键心智模型：**`softcap` 与 `score_mod` 是同一回事的两种写法**——传 `softcap` 会在内部生成一个等价的 `score_mod`，二者不能同时给。

#### 4.2.2 核心流程

公共函数收到参数后，在 `_flash_attn_fwd` 里依次做：

1. **连续性修复**：`maybe_contiguous` 把最后一维不连续的张量静默转成连续。
2. **形状与 dtype 校验**：断言布局、对齐、dtype 一致、`num_head % num_head_kv == 0`。
3. **掩码归一化**：`_resolve_causal_local_window` 把 `causal` + `window_size` 折算成规范的 `(causal, local, window_left, window_right)` 四元组。
4. **softcap 折叠**：若给了 `softcap`，转成内部 `score_mod`。
5. **自动补默认**：`softmax_scale`、`pack_gqa` 等若为 `None` 按规则补值。
6. **算 cache key**：把影响编译的参数组装成元组 `compile_key`，查表决定是否重编译。
7. **启动 kernel**：按架构分发到 SM80/SM90/SM100/SM110/SM120 的前向 kernel。

#### 4.2.3 源码精读

**`softmax_scale` 默认值**——标准注意力用 \(1/\sqrt{d}\)，MLA 吸收式用 \(1/\sqrt{d+d_v}\)：

[flash_attn/cute/interface.py:452-456](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/interface.py#L452-L456) — 注意 `softmax_scale` **不进** `compile_key`（见 4.2.3 末尾），改它只是换运行时标量、不重编译。

**`pack_gqa` 默认值**——只要 Q 头数多于 KV 头数（GQA/MQA）就自动开启：

[flash_attn/cute/interface.py:460-461](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/interface.py#L460-L461) — 因此多数情况下你不用显式传 `pack_gqa`。

**`softcap` 折叠成 `score_mod`**——互斥且 `softcap=0.0` 等价于不限制：

[flash_attn/cute/interface.py:605-610](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/interface.py#L605-L610) — `softcap` 用 `utils.create_softcap_scoremod` 造一个 `@cute.jit` 回调，并断言不能与用户 `score_mod` 同时出现；SM8x 上自定义 `score_mod` 会直接抛 `NotImplementedError`。

**掩码归一化**——把 `causal` + `window_size` 折成规范四元组：

[flash_attn/cute/interface.py:275-295](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/interface.py#L275-L295) — 三条规则值得记：(a) `mask_mod` 非空时强制 `causal=local=False`；(b) `causal=True` 时把 `window_size_right` 置 0；(c) 只给 `window_size_left` 且 `window_size_right=0` 时退化成因果。这就是为什么「`window_size=(N, 0)`」与「`causal=True`」在因果语义上等价。

**SplitKV 启停**——`num_splits < 1` 触发启发式自动选择，否则用用户值：

[flash_attn/cute/interface.py:567-568](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/interface.py#L567-L568) 调用 [flash_attn/cute/interface.py:260-272](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/interface.py#L260-L272) — `num_splits=1`（默认）即不切分；`num_splits=0` 或负值会按 `num_SMs // total_mblocks` 自动选个合理值。`num_n_blocks <= 4` 时强制不切（短 KV 切了反而浪费）。

**tile 尺寸自动选择**——`FwdConfig` 与 SM90 的查表函数：

[flash_attn/cute/interface.py:115-121](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/interface.py#L115-L121) 定义 `FwdConfig`；[flash_attn/cute/interface.py:123-156](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/interface.py#L123-L156) 是 `_tile_size_fwd_sm90`，按 `head_dim` / `causal` / `local` 查表给出 `(tile_m, tile_n, mma_pv_is_rs, intra_wg_overlap)`。公共 API 把 `tile_mn` 留作内部参数，等于「FA 已经替你调好了 tile，一般无需手改」。

**编译缓存键**——哪些参数会触发重编译：

[flash_attn/cute/interface.py:718-765](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/interface.py#L718-L765) — 这个超长元组就是 `compile_key`。可总结出「**值进键 vs 布尔进键**」的规律：

- **整个值进键**：`dtype`、`head_dim`、`head_dim_v`、`qhead_per_kvhead`、`causal`、`tile_m`、`tile_n`、`num_threads`、`arch`、`score_mod_hash`、`mask_mod_hash`——改这些会重编译。
- **只把「是否为 None」进键**：`cu_seqlens_q/k`、`seqused_q/k`、`page_table`、`window_size_left/right`、`learnable_sink`、各种 `descale`、`lse is None`——即「开关一次」会重编译，但开关打开后**改具体数值不重编译**（数值作为运行时参数传入）。
- **不在键里**：`softmax_scale`（运行时标量）。注意 `num_splits` 进键的是 `is_split_kv`（布尔），所以 `num_splits=1` 与 `>1` 是两个 kernel，但 `num_splits=2` 和 `num_splits=4` 复用同一个前向 kernel（差别在 combine kernel）。

#### 4.2.4 代码实践（实验型）

**实践目标**：用一组对照实验，直观感受「哪些参数改了会重编译、哪些只是换运行时值」。

**操作步骤**（示例代码，标注为示例代码）：

```python
# 示例代码：观察编译耗时差异（需要 GPU 与已安装的 flash-attn-4）
import time, torch
from flash_attn.cute import flash_attn_func

def make():  # batch, seqlen, nheads, hdim
    return [torch.randn(2, 1024, 8, 64, dtype=torch.float16, device="cuda") for _ in range(3)]

def timed(**kw):
    q, k, v = make()
    t0 = time.perf_counter()
    out, lse = flash_attn_func(q, k, v, return_lse=True, **kw)
    torch.cuda.synchronize()
    return time.perf_counter() - t0

print("首次(causal=False):", timed(causal=False))   # 含 JIT 编译，慢
print("复用(causal=False):", timed(causal=False))   # 命中缓存，快
print("切换 causal=True :", timed(causal=True))      # causal 进键 → 重编译，慢
print("改 softmax_scale :", timed(causal=True, softmax_scale=0.2))  # 不在键里 → 复用，快
```

**需要观察的现象**：

- 第 1、3 次明显慢（编译）；第 2、4 次明显快（缓存命中）。
- `causal` 从 `False` 切到 `True` 触发重编译；而改 `softmax_scale` 不触发。

**预期结果**：与 4.2.3 中 `compile_key` 的组成完全吻合——`causal` 进键、`softmax_scale` 不进键。

> 待本地验证：具体毫秒数取决于 GPU 与驱动，但「编译 vs 缓存」的数量级差异（秒级 vs 毫秒级）应稳定可复现。

#### 4.2.5 小练习与答案

**练习 1**：把 `window_size=(128, 0)` 改成 `window_size=(256, 0)`，会不会重编译？为什么？

> **答案**：不会。`compile_key` 里只存 `window_size_left is not None` 与 `window_size_right is not None` 两个布尔；窗口大小的具体值作为运行时参数传给已编译的 kernel。

**练习 2**：`softcap=0.5` 与 `softcap=0.8`，会用同一个编译产物吗？

> **答案**：通常不会。`softcap` 会被 `create_softcap_scoremod` 折叠成一个闭包，闭包的 cap 值进入 `score_mod_hash`（哈希含闭包捕获的值），所以改 cap 会改变哈希、触发重编译。（这与窗口大小「不进键」形成对比，原因是 softcap 被编译期内联进了 kernel。）

---

### 4.3 返回值 O 与 LSE

#### 4.3.1 概念说明

两个公共函数都返回二元组 `(out, lse)`：

- **`out`**：注意力输出，形状与 Q 的前几维一致 `(batch, seqlen_q, nheads, head_dim_v)`（注意是 `head_dim_v`，可与 `head_dim` 不同，用于 MLA、DeepSeek 形状）。
- **`lse`**：每行 Q 的 log-sum-exp，float32。

`lse` 的意义来自 online softmax：对第 \(i\) 行 Q，kernel 在分块累加时维护行最大值 \(m_i\) 与（去最大值后的）指数和，二者合成

\[
\mathrm{lse}_i \;=\; m_i + \log\sum_j \exp(s_{ij}-m_i) \;=\; \log\sum_j \exp(s_{ij}),
\]

其中 \(s_{ij}=\text{scale}\cdot q_i k_j^\top\) 是缩放后的分数。于是 softmax 权重 \(P_{ij}=\exp(s_{ij}-\mathrm{lse}_i)\)，输出

\[
O_i \;=\; \sum_j P_{ij}\, v_j.
\]

归一化因子 \(\sum_j \exp(s_{ij}) = \exp(\mathrm{lse}_i)\) 正是「LSE 还原出的分母」。这条等式是本讲实践的验证依据。

#### 4.3.2 核心流程

`lse` 张量的「生与死」由一条简单规则控制：

```
requires_grad ← 任一输入(q/k/v/qv) 的 requires_grad
need_lse     ← requires_grad  或  return_lse=True
lse          ← need_lse ? 分配 float32 张量 : None
```

也就是说：**纯推理（无 grad）且 `return_lse=False` 时，`lse` 是 `None`**——FA 不会浪费显存去算它。想要拿到 LSE，要么让输入 `requires_grad=True`，要么显式 `return_lse=True`。

`lse` 的形状取决于是否变长、是否有 `qv`：

| 模式 | `lse` 形状 |
| --- | --- |
| 定长、无 `qv`（标准） | `(batch, nheads, seqlen_q)` |
| 变长、无 `qv` | `(nheads, total_q)` ← 注意头维在前 |
| 定长、有 `qv`（MLA 吸收） | `(batch, seqlen_q, nheads)` |
| 变长、有 `qv` | `(total_q, nheads)` |

注意无 `qv` 时头维排在前是为了让 `nheads` 维连续，方便后续 kernel 向量化访问。

#### 4.3.3 源码精读

**LSE 形状决定**——按是否变长、是否有 `qv` 分四种：

[flash_attn/cute/interface.py:471-475](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/interface.py#L471-L475) — 对照上表逐行看源码即可。

**LSE 是否分配**——`requires_grad or return_lse` 才分配，否则 `None`：

[flash_attn/cute/interface.py:484-491](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/interface.py#L484-L491) — 这就是「纯推理 + 不传 `return_lse` 会拿到 `lse=None`」的根因。dtype 固定 `torch.float32`，保证数值精度。

**SplitKV 的部分 LSE**——切分时每个 split 各产一份 LSE，再由 combine kernel 合并：

[flash_attn/cute/interface.py:580-583](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/interface.py#L580-L583) — `out_partial`（fp32）与 `lse_partial` 按 split 维堆叠；最终在 [flash_attn/cute/interface.py:1090-1099](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/interface.py#L1090-L1099) 调用 `_flash_attn_fwd_combine` 用 log-sum-exp 合成全局 `out`/`lse`。

**最终返回**——`_flash_attn_fwd` 返回四元组，但公共 API 只取前两个：

[flash_attn/cute/interface.py:1099](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/interface.py#L1099) 返回 `(out, lse, p, row_max)`；而 `FlashAttnFunc.forward` 在 [flash_attn/cute/interface.py:2488](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/interface.py#L2488) 只 `return out, lse`。后两个 `p`/`row_max` 仅 MLA top-k 路径反向时用到。

#### 4.3.4 代码实践（运行型）

**实践目标**：调用 `flash_attn_func` 打印 LSE 的形状与数值范围，再用 LSE 还原 softmax 归一化因子，验证与参考实现及输出 `out` 的一致性。

**操作步骤**（示例代码）：

```python
# 示例代码：LSE 形状、范围与一致性验证（需 GPU + flash-attn-4）
import torch
from flash_attn.cute import flash_attn_func

torch.manual_seed(0)
batch, seqlen, nheads, hdim = 2, 512, 8, 64
q = torch.randn(batch, seqlen, nheads, hdim, dtype=torch.float16, device="cuda")
k = torch.randn(batch, seqlen, nheads, hdim, dtype=torch.float16, device="cuda")
v = torch.randn(batch, seqlen, nheads, hdim, dtype=torch.float16, device="cuda")

# 1) 必须显式 return_lse=True，否则纯推理下 lse 为 None
out, lse = flash_attn_func(q, k, v, causal=True, return_lse=True)
print("out.shape:", tuple(out.shape))   # 预期 (2, 512, 8, 64)
print("lse.shape:", tuple(lse.shape))   # 预期 (2, 8, 512)  ← (batch, nheads, seqlen_q)
print("lse.dtype:", lse.dtype)          # 预期 torch.float32
print("lse range:", lse.min().item(), lse.max().item())

# 2) fp32 参考实现：scores = scale * QK^T，加因果掩码
scale = 1.0 / (hdim ** 0.5)
qf, kf, vf = q.float(), k.float(), v.float()
scores = torch.einsum("bthd,bshd->bhts", qf, kf) * scale          # (b, h, sq, sk)
mask = torch.triu(torch.full((seqlen, seqlen), float("-inf"), device="cuda"), diagonal=1)
scores = scores + mask                                            # 因果掩码
ref_lse = torch.logsumexp(scores, dim=-1)                         # (b, h, sq)

# 3) 用 LSE 还原归一化因子：exp(lse) == sum_j exp(scores_ij)
denom_from_lse = torch.exp(lse)                                   # = sum_j exp(scores_ij)
denom_direct = torch.exp(scores).sum(dim=-1)
print("max |exp(lse) - sum exp(scores)|:", (denom_from_lse - denom_direct).abs().max().item())

# 4) 验证输出：O = softmax(scores) @ V
P = torch.softmax(scores, dim=-1)                                 # (b, h, sq, sk)
v_h = vf.permute(0, 2, 1, 3).contiguous()                         # (b, h, sk, d)
ref_out = (P @ v_h).permute(0, 2, 1, 3).contiguous()              # (b, sq, h, d)
print("max |lse - ref_lse|:", (lse - ref_lse).abs().max().item())
print("max |out - ref_out|:", (out.float() - ref_out).abs().max().item())
```

**需要观察的现象**：

- `lse.shape == (2, 8, 512)`，dtype 为 `float32`；范围大致在个位数的负值附近（取决于输入分布）。
- `exp(lse)` 与 `sum exp(scores)` 的最大差应接近 0（fp16 输入下的舍入量级，通常 < 1e-2）。
- `out` 与 `ref_out` 的最大差也在 fp16 舍入量级（约 1e-2 ~ 1e-1）。

**预期结果**：LSE 与 `logsumexp` 参考一致，且 `exp(lse)` 精确还原 softmax 分母——这正是 online softmax 在数学上等价于完整 softmax 的实证。

> 待本地验证：上述脚本未在本环境执行；具体误差量级随 GPU、驱动、随机种子变化，但「LSE ≈ ref_lse」「exp(lse) ≈ 分母」两条结论应稳定成立。参考实现可对照 [flash_attn/cute/testing.py:326-351](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/testing.py#L326-L351) 的 `attention_ref`。

**一个常见坑**：若去掉 `return_lse=True`，上面 `lse.min()` 会报 `AttributeError: 'NoneType'`，因为纯推理下 FA 不分配 LSE。这也解释了为何 `compile_key` 里专门有 `lse is None` 这一项——是否计算 LSE 会编译出不同的 kernel。

#### 4.3.5 小练习与答案

**练习 1**：定长、无 `qv` 时，`lse` 形状为何是 `(batch, nheads, seqlen_q)` 而不是 `(batch, seqlen_q, nheads)`？

> **答案**：让 `nheads` 维连续排在最后的前一维，便于后续 kernel（反向、combine）按头做向量化访存。MLA 吸收式（有 `qv`）才是 `(batch, seqlen_q, nheads)`，因为那里头数与 KV 的关系不同。

**练习 2**：`lse.dtype` 为什么强制 float32，即便输入是 fp16？

> **答案**：LSE 既参与反向梯度，又被 combine kernel 用来做跨 split 的 log-sum-exp 合并，需要远高于 fp16 的动态范围与精度，因此单独用 float32 存。

**练习 3**：为什么 `lse is None` 要进 `compile_key`？

> **答案**：是否产出 LSE 决定 kernel 是否需要写回一行额外的 float32 统计量，影响寄存器/共享内存布局与 epilogue 代码路径，所以 FA 为「算 LSE」与「不算 LSE」各编译一个特化版本。

---

## 5. 综合实践

把三个模块串起来：写一个小脚本，对同一组 fp16、`hdim=64`、`seqlen=1024` 的输入，分别用「纯推理 + `return_lse=False`」「纯推理 + `return_lse=True`」「`requires_grad=True`（反向）」三种方式调用 `flash_attn_func`，完成下面三件事：

1. **接口层**：确认三种调用返回的都是二元组，且第一项 `out` 形状一致；记录每种调用下 `lse` 是张量还是 `None`，并与 4.3.2 的规则核对。
2. **参数层**：在「反向」那次调用里，把 `causal` 从 `True` 切成 `False` 再调一次，用 `time` 记录两次的耗时——验证 `causal` 进 `compile_key` 会触发重编译；再把 `softmax_scale` 改一个值，验证它不触发重编译。
3. **返回值层**：对「`return_lse=True`」那次，按 4.3.4 的方法用 `lse` 还原归一化因子、对照 `attention_ref`，确认一致性。

完成后，你应当能用一句话回答：「`flash_attn_func` 的哪些参数影响正确性、哪些影响性能、哪些影响是否重编译」。

> 提示：综合实践无需提交，重点是亲手跑出「编译 vs 缓存」「LSE ≈ logsumexp」这两个对比现象。运行环境与依赖见 [u1-l3](u1-l3-install-and-first-run.md)。

## 6. 本讲小结

- FA4 对外只有 `flash_attn_func`（定长）与 `flash_attn_varlen_func`（变长/分页超集）两个入口，都是对 `FlashAttnFunc` / `FlashAttnVarlenFunc` 的薄包装，二者数学等价。
- 公共 API **不暴露** tile 尺寸与线程数——`FwdConfig` / `_tile_size_fwd_sm90` 等按架构与 hdim 自动选定；`softmax_scale` 缺省 \(1/\sqrt{d}\)。
- 掩码三件套 `causal` / `window_size` / `mask_mod` 经 `_resolve_causal_local_window` 折成统一四元组；`softcap` 会被折叠成内部 `score_mod`，与用户 `score_mod` 互斥。
- 返回值恒为 `(out, lse)`：`out` 形状同 Q 前几维、用 `head_dim_v`；`lse` 是 float32，定长无 `qv` 时为 `(batch, nheads, seqlen_q)`。
- **`lse` 在「纯推理 + `return_lse=False`」时为 `None`**——是否计算 LSE 进了 `compile_key`，会编译出不同 kernel。
- 改参数是否重编译看 `compile_key`：`causal`、`tile_m/n`、`num_threads`、dtype、hdim 等进键会重编译；`softmax_scale`、窗口具体大小不进键则不重编译。

## 7. 下一步学习建议

- 想深入了解「为何这么分架构」，进入 [u2-l2 架构分发与 tile 配置选择](u2-l2-arch-dispatch-and-config.md)，看 `_get_device_arch`、`_validate_head_dims` 与 tile 查表的完整逻辑。
- 想搞清 `mask_mod` / `score_mod` 的回调签名与注入方式，进入 [u3-l1 AttentionMask](u3-l1-attention-mask.md) 与 [u4-l2 score_mod](u4-l2-score-mod.md)。
- 想理解 LSE 如何在 SplitKV 合并中发挥作用，进入 [u7-l2 SplitKV 与 Combine Kernel](u7-l2-splitkv-and-combine.md)。
- 想看清 `lse` 在反向里怎么被复用，进入 [u9-l1 反向算法与 Sm80 反向 Kernel](u9-l1-backward-algorithm-sm80.md)。
- 阅读建议：先对照本讲把 `interface.py` 中 `flash_attn_func` → `FlashAttnFunc.forward` → `_flash_attn_fwd` 这条链用编辑器跳转走一遍，再带着「`compile_key` 里有什么」的视角读后续每一篇。
