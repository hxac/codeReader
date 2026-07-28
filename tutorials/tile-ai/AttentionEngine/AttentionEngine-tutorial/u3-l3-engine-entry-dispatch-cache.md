# AttentionEngine 引擎入口：分发、编译与缓存

## 1. 本讲目标

前面几讲我们走完了「用户 API → 符号 IR → 降级（lower）→ 模板（template）」这条编译链的内部细节，但始终把一个东西当黑盒：**到底是谁，在什么时刻，根据什么规则，把用户的描述送进正确的降级函数、再把生成代码挂成一个能 `mod(q,k,v)` 调用的对象？**

这个「总调度」就是本讲的主角——`attention_engine/attn_engine/attn_engine.py` 里的 `AttentionEngine` 类。学完本讲你应该能够：

- 说清 `AttentionEngine.__init__` 如何按 `backend`（`tl` / `cute`）分流，以及两条分支产物形态的差异。
- 给定一组 `qkv_meta` 形状，**推断**引擎会调用哪一个 `lower_*` 函数（共 5 条 tl 分发路径 + 2 条 cute 路径）。
- 解释「md5 哈希缓存 + importlib 动态加载」的完整流程，知道生成代码落在哪个目录、何时复用、何时重编。
- 理解 `__call__` 如何把构造期编译好的 kernel 与运行期真实张量缝合起来，并处理 `block_mask` 的两种来源。

本讲是第三单元的收尾：它把 u3-l1（模板渲染）和 u3-l2（`lower_tl` 编排）拼装出的最终 TileLang 源码，与「外部使用者」对接起来。

## 2. 前置知识

在进入引擎之前，请确认你已建立以下认知（均来自前面讲义）：

- **编译式注意力框架的整体形状**（u1-l1）：用户写 Python 函数描述注意力，框架把它翻译成 GPU kernel。
- **`qkv_meta` 与 `meta_tensor`**（u1-l2）：`qkv_meta` 是一个三元组 `(q_meta, k_meta, v_meta)`，每个 `meta_tensor` 只存 `shape`（如 `(B, H, S, D)`）和 `dtype`，是**编译期唯一的形状来源**。引擎的形状分发完全依赖 `qkv_meta[0].shape`（q）与 `qkv_meta[2].shape`（v）。
- **`lower_tl` 主流程**（u3-l2）：降级编排函数，输入是用户描述 + 形状，输出是一段完整的 TileLang 源码字符串。
- **Jinja2 模板渲染**（u3-l1）：把降级产物灌进骨架，产出可 `exec` 的源码字符串。
- **GQA / decode / MLA 等概念**（u4 会深入，本讲只需知道 GQA 指「查询头数 > 键值头数」，decode 指「q 序列长度远小于 kv 序列长度」，MLA 是一种 kv 共享的注意力变体）。

如果你对 `qkv_meta` 的轴序 `(B, H, S, D)` 还不熟，建议先回看 u1-l2 再继续。

## 3. 本讲源码地图

本讲主要围绕**一个文件**展开，辅以若干示例脚本印证分发结果。

| 文件 | 作用 |
|------|------|
| `attention_engine/attn_engine/attn_engine.py` | **核心**。定义 `OnlineFunc` 基类与 `AttentionEngine` 类，包含 `__init__` / `_select_lower_template` / `_compile_tl` / `__call__` 四个关键方法，是整个框架对外的总入口。 |
| `attention_engine/attn_engine/__init__.py` | 包导出：`AttentionEngine`、`OnlineFunc`、`LinearAttentionEngine`。所以用户脚本里 `from attn_engine import AttentionEngine` 能直接拿到。 |
| `attn_script/mha.py` | 训练 MHA 示例，`q_seqlen==kv_len` 且 `head==head_kv`，走 `lower`。 |
| `attn_script/gqa.py` | 训练 GQA 示例，`head>head_kv`，走 `lower_gqa`。 |
| `attn_script/gqa_inference.py` | GQA 解码示例，`q_seqlen==1 < kv_len` 且 `head>head_kv`，走 `lower_decode_gqa`。 |
| `attn_script/mha_inference.py` | MHA 解码示例，`q_seqlen < kv_len` 且 `head==head_kv`，走 `lower_decode`。 |
| `attn_script/mla_decode.py` | MLA 解码示例，`kv_shared=True`，走 `lower_decode_mla`。 |

引擎内部按需 `import` 的五个降级模块（你不必现在打开它们，只需知道入口函数名）：

- `core/lower/lower.py` → `lower_tl`
- `core/lower/lower_gqa.py` → `lower_tl`
- `core/lower/lower_decode.py` → `lower_tl`
- `core/lower/lower_decode_gqa.py` → `lower_tl`
- `core/lower/lower_decode_mla.py` → `lower_tl`

以及 cute 后端的 `core/lower/lower_cute.py` → `lower_cute`。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：**backend 选择**、**形状分发**、**md5 缓存与 importlib 加载**。它们正好对应「构造一次 `AttentionEngine`」依次发生的三件事。

### 4.1 backend 选择：tl 还是 cute

#### 4.1.1 概念说明

AttentionEngine 支持两个生成后端：

- **`tl`（TileLang，默认）**：把注意力降级成 TileLang（一种 Python DSL）源码，再经 TileLang 编译器产出 GPU kernel。这是训练（前向 + 反向）与各类解码场景的主力，覆盖最全。
- **`cute`（CuTe C++，面向 Hopper）**：降级成 CuTe C++ 片段，渲染成 `.h`/`.cu`/`flash_attn_interface.py`，性能对标 FlashAttention-3，但**当前不支持反向**，且只覆盖普通注意力与 kv_shared MLA 两种子情况。

两者的输入（用户描述、`qkv_meta`）完全相同，**切换后端不需要改用户写的 `score_mod`/`online_func`**——这正是「前端与后端解耦」的体现（参见 u2-l4 的三套发射器）。区别只在于引擎把同一份符号 DAG 翻译成哪种目标语言、渲染进哪类模板。

`backend` 是 `AttentionEngine` 构造函数的一个关键字参数，默认值是 `"tl"`。

#### 4.1.2 核心流程

`AttentionEngine.__init__` 做的第一件事就是按 `backend` 字符串分流：

```text
AttentionEngine.__init__(...)
   │
   ├── if backend == "tl":   → self._compile_tl(...)        # 走完整的形状分发 + md5 缓存
   │
   └── elif backend == "cute": → lower_cute(...) 直接渲染   # 不走 _select_lower_template
                │
                ├── if not kv_shared:  渲染 flash_attn_interface.py   → flash_attn_func
                └── else (kv_shared):  渲染 flash_mla_interface.py    → flash_mla_with_kvcache
```

注意一个**关键不对称**：tl 后端会再做一次「形状分发」（4.2 节），从 5 个 `lower_*` 里选一个；而 cute 后端**不做形状分发**，只按 `kv_shared` 二选一。这是因为 cute 路径目前只实现了两种 kernel 模板。

#### 4.1.3 源码精读

构造函数签名很长，但第一个有效逻辑就是 backend 分流：

[attention_engine/attn_engine/attn_engine.py:109-137](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/attn_engine/attn_engine.py#L109-L137) —— `__init__` 把所有用户组件（`score_mod`/`mask_mod`/`online_func`/`custom_fwd_inputs`）和 `qkv_meta` 接住，并按 `backend` 调用 `self._compile_tl(...)`。

```python
# backend
if backend == "tl":
    self._compile_tl(qkv_meta, custom_fwd_inputs, score_mod, mask_mod,
                     online_func, mask_value, infer_mask=..., kv_shared=kv_shared)
elif backend == "cute":
    from core.lower.lower_cute import lower_cute
    ...
```

cute 分支里，模板目录与产物文件名由 `kv_shared` 决定：

[attention_engine/attn_engine/attn_engine.py:138-162](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/attn_engine/attn_engine.py#L138-L162) —— 非 kv_shared 用 `cute_template/` 目录、产物 `flash_attn_interface.py`；kv_shared 用 `cute_template_kvshared/` 目录、产物 `flash_mla_interface.py`，且输出目录名还会带上 `dimqk_dimv` 后缀（如 `cute_template_output_576_512`）。

之后无论哪种，都用 `importlib` 把生成的 `.py` 加载成模块，再取出可调用对象挂到 `self.attention`：

[attention_engine/attn_engine/attn_engine.py:176-184](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/attn_engine/attn_engine.py#L176-L184) —— cute 普通：`self.attention = partial(cute_attn.flash_attn_func, causal=...)`。

```python
spec = importlib.util.spec_from_file_location("cute_attn", file_path)
cute_attn = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cute_attn)
if not kv_shared:
    self.attention = partial(cute_attn.flash_attn_func,
                             causal=True if mask_mod is not None else False)
```

cute + kv_shared 分支更复杂，涉及 paged-kv 的 `block_table`、`cache_seqlens`、`tile_scheduler_metadata` 等构造，这些是 u5-l2 的主题，本讲只需知道它最终挂的是 `partial(cute_attn.flash_mla_with_kvcache, ...)`。

#### 4.1.4 代码实践

**实践目标**：从用户脚本视角确认 backend 切换的代价为零（不改用户描述）。

**操作步骤**：

1. 打开 [attn_script/mha_cute.py](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attn_script/mha_cute.py)，对照 [attn_script/mha.py](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attn_script/mha.py)。
2. 找到 `AttentionEngine(...)` 构造调用，比较两者的 `score_mod`、`online_func`、`qkv_meta` 是否一致。
3. 找出**唯一**的不同点——`backend` 参数（mha.py 省略走默认 `"tl"`，mha_cute.py 显式传 `backend="cute"`）。

**需要观察的现象**：用户描述层（四个组件）完全相同；只有引擎构造参数不同。

**预期结果**：你会在 mha_cute.py 里看到 `backend="cute"`，而 `score_mod`/`mask_mod`/`OnlineSoftmax` 与 mha.py 逐字相同。这印证了「前端与后端解耦」。

> 若本机没有 Hopper 显卡，cute 路径可能无法实际编译运行，本实践只需做**源码阅读对比**即可，不必执行。

#### 4.1.5 小练习与答案

**练习 1**：为什么 cute 后端的 `__init__` 里没有调用 `_select_lower_template`？

**参考答案**：因为 `_select_lower_template` 是 tl 后端专属的「形状分发器」，它根据 `q_seqlen`/`head`/`head_kv` 在 5 个 tl `lower_*` 之间选择；cute 后端目前只实现了普通 flash 与 kv_shared MLA 两种模板，只按 `kv_shared` 二选一，不需要形状分发。

**练习 2**：cute 分支里 `self.attention` 用 `functools.partial` 绑定了 `causal=...`，而 tl 分支里 `self.attention = tl_attn.attention` 没绑定任何参数。请猜一个原因。

**参考答案**：cute 的 `flash_attn_func` 把 `causal` 当作运行期可选参数，引擎在构造期就根据 `mask_mod` 是否为 `None` 把它固定下来，省得每次调用都传；tl 后端则把 mask 信息在**编译期**就编进了生成的 kernel 代码里（参见 u2-l8 的 mask 降级），所以运行期 kernel 函数本身不含 `causal` 参数。

---

### 4.2 形状分发：根据 qkv_meta 选对 lower_*

> 本模块只对 **tl 后端** 有效（cute 不走这条路径）。

#### 4.2.1 概念说明

即使用户描述完全相同，**不同形状**的注意力需要不同的 kernel 骨架。例如：

- 训练 MHA：`q_seqlen == kv_len`，Q 和 KV 等长，按块切 Q、循环 KV（u3-l2 讲的 `lower_tl`）。
- 训练 GQA：查询头数 `head` 大于键值头数 `head_kv`，索引时要按组映射（`groupnum`）。
- 解码（decode）：`q_seqlen` 远小于 `kv_len`（生成式推理，q 往往只有 1 个 token），分块策略完全不同，还要做 split-kv。
- MLA decode：kv 共享（`kv_shared`）的特殊解码，PE 维要拆分。

引擎不会让用户操心这些，而是**自动**根据 `qkv_meta` 携带的形状信息，把请求路由到正确的降级函数。这个路由逻辑写在 `_select_lower_template` 里。

#### 4.2.2 核心流程

`_select_lower_template` 先从 `qkv_meta` 抽出三个关键标量，再用一连串**互斥条件**逐个匹配并 `return`：

```text
抽取: q_seqlen = q.shape[2]      # qkv_meta[0].shape[2]
      kv_len   = v.shape[2]      # qkv_meta[2].shape[2]
      head     = q.shape[1]      # qkv_meta[0].shape[1]
      head_kv  = v.shape[1]      # qkv_meta[2].shape[1]

匹配顺序（先匹配先返回）:
  1. kv_shared 为真                     → lower_decode_mla     (MLA 解码)
  2. q_seqlen≠kv_len 且 head>head_kv     → lower_decode_gqa     (GQA 解码, 要求 q_seqlen==1)
  3. q_seqlen≠kv_len 且 head==head_kv    → lower_decode         (MHA 解码)
  4. q_seqlen==kv_len 且 head==head_kv   → lower                (MHA 训练/prefill)
  5. q_seqlen==kv_len 且 head>head_kv    → lower_gqa            (GQA 训练)
```

把这套规则整理成一张「形状 → 路径」对照表：

| 场景 | 条件 | 调用的降级函数 | 对应示例脚本 |
|------|------|----------------|--------------|
| MLA 解码 | `kv_shared=True`（最优先） | `lower_decode_mla` | `mla_decode.py` |
| GQA 解码 | `q_seqlen≠kv_len` 且 `head>head_kv` | `lower_decode_gqa` | `gqa_inference.py` |
| MHA 解码 | `q_seqlen≠kv_len` 且 `head==head_kv` | `lower_decode` | `mha_inference.py` |
| MHA 训练/prefill | `q_seqlen==kv_len` 且 `head==head_kv` | `lower` | `mha.py` |
| GQA 训练 | `q_seqlen==kv_len` 且 `head>head_kv` | `lower_gqa` | `gqa.py` |

注意两个**细节**（容易踩坑）：

- **匹配顺序很重要**：`kv_shared` 被最先检查。即使形状同时满足别的条件，只要 `kv_shared=True`，一定走 MLA 解码。
- **GQA 解码比 MHA 解码要求更严**：GQA 解码分支带 `assert q_seqlen == 1`，强制 q 序列长为 1；而 MHA 解码分支只 `assert q_seqlen < kv_len`，允许 `q_seqlen` 是一段（如 128，见 `mha_inference.py`）。这就是为什么 `mha_inference.py` 的 `q_seqlen=128` 也能走 `lower_decode`。

还有一个**未覆盖的角落**：`head < head_kv`，或 `q_seqlen==kv_len` 但 `head<head_kv` 等组合，没有任何分支匹配，函数会一路 `return None`（隐式），后续 `_compile_tl` 取 `tl_code` 会出错。这是当前实现的边界，使用时要保证形状落在上表五种情形之一。

#### 4.2.3 源码精读

形状抽取这一步很朴素，但它是理解一切分发的前提：

[attention_engine/attn_engine/attn_engine.py:225-232](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/attn_engine/attn_engine.py#L225-L232) —— 从 `qkv_meta` 抽取 `q_seqlen`/`kv_len`/`head`/`head_kv`。注意 q 信息取自 `qkv_meta[0]`，kv 信息取自 `qkv_meta[2]`（v），与轴序 `(B, H, S, D)` 对应。

```python
q_seqlen = qkv_meta[0].shape[2]
kv_len = qkv_meta[2].shape[2]
head = qkv_meta[0].shape[1]
head_kv = qkv_meta[2].shape[1]
```

MLA 解码分支（最高优先级，仅依赖 `kv_shared`）：

[attention_engine/attn_engine/attn_engine.py:234-250](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/attn_engine/attn_engine.py#L234-L250) —— 命中时 `import lower_decode_mla`，传入 B/head/head_kv/kv_len/dimqk/dimv 等形状，**返回 `(tl_code, None)`**（MLA 不产出 block_mask）。

GQA 解码分支（带 `q_seqlen==1` 断言，并强制 `infer_mask=True`）：

[attention_engine/attn_engine/attn_engine.py:252-271](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/attn_engine/attn_engine.py#L252-L271) —— 关键是这两行断言与强制：

```python
if q_seqlen != kv_len and head > head_kv:  # TODO: change condition
    assert (q_seqlen < kv_len)
    assert q_seqlen == 1
    infer_mask = True
    from core.lower.lower_decode_gqa import lower_tl as lower_tl_decode_gqa
    ...
    return tl_code, block_mask
```

> 注释 `# TODO: change condition` 说明作者也意识到这套 `head>head_kv` 的判定将来可能要调整。

MHA 解码、MHA 训练、GQA 训练三个分支结构同构，差别只在条件与 `import` 的模块：

[attention_engine/attn_engine/attn_engine.py:273-332](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/attn_engine/attn_engine.py#L273-L332) —— 三个 `if` 各自带 `return`，逻辑上互斥，等价于 `elif`。其中训练两个分支额外把 `tune`/`tune_file`/`tune_bwd`/`tune_file_bwd` 透传给 `lower_*`（解码分支不透传 tune 参数，因为解码通常不调优）。

#### 4.2.4 代码实践

**实践目标**：不看答案，根据 `qkv_meta` 形状推断分发路径，再用源码核验。

**操作步骤**：

1. 阅读下面四组 `qkv_meta`（轴序均为 `(B, H, S, D)`），分别填出 `q_seqlen`、`kv_len`、`head`、`head_kv` 四个值：

   - **A（来自 mha.py）**：q=`(1,128,2048,128)`、k=`(1,128,2048,128)`、v=`(1,128,2048,128)`。
   - **B（来自 gqa.py）**：q=`(1,128,8192,128)`、k=`(1,8,8192,128)`、v=`(1,8,8192,128)`。
   - **C（来自 gqa_inference.py）**：q=`(1,32,1,128)`、k=`(1,8,8192,128)`、v=`(1,8,8192,128)`。
   - **D（来自 mha_inference.py）**：q=`(1,32,128,128)`、k=`(1,32,8192,128)`、v=`(1,32,8192,128)`。

2. 对照 4.2.2 的匹配规则表，逐组判断命中哪一条分支。

3. 最后再读一遍 `_select_lower_template` 的源码核验。

**需要观察的现象**：A、B 是「等长」场景（`q_seqlen==kv_len`），C、D 是「解码」场景（`q_seqlen<kv_len`）；C 的 `head>head_kv`，D 的 `head==head_kv`。

**预期结果**：

| 组 | q_seqlen | kv_len | head | head_kv | 命中分支 | 降级函数 |
|----|----------|--------|------|---------|----------|----------|
| A | 2048 | 2048 | 128 | 128 | 训练 MHA | `lower` |
| B | 8192 | 8192 | 128 | 8 | 训练 GQA | `lower_gqa` |
| C | 1 | 8192 | 32 | 8 | GQA 解码（`assert q_seqlen==1` ✓） | `lower_decode_gqa` |
| D | 128 | 8192 | 32 | 32 | MHA 解码（仅要求 `q_seqlen<kv_len`） | `lower_decode` |

> 待本地验证：若你在本机真正构造这四个 `AttentionEngine`，可在分发函数内临时加一行 `print(__import__("inspect").currentframe().f_code.co_name)` 或在 `_select_lower_template` 开头打印 `q_seqlen, kv_len, head, head_kv`，确认运行期取值与上表一致。

#### 4.2.5 小练习与答案

**练习 1**：如果用户传 `kv_shared=True` 但形状其实满足「训练 MHA」（`q_seqlen==kv_len` 且 `head==head_kv`），会走哪个分支？

**参考答案**：走 **MLA 解码**（`lower_decode_mla`）。因为 `kv_shared` 检查在最前面，一旦为真立即返回，不再看 `q_seqlen`/`head`。这提醒我们：`kv_shared` 是一个比形状更强的「显式声明」，传 `True` 即等于告诉引擎「我要 MLA 解码路径」。

**练习 2**：`mha_inference.py` 里 `q_seqlen=128`（不是 1），为什么仍能成功走解码分支？

**参考答案**：它命中第 3 条「MHA 解码」分支，该分支只断言 `q_seqlen < kv_len`（`128 < 8192` 成立），不要求 `q_seqlen==1`。只有第 2 条「GQA 解码」分支才断言 `q_seqlen==1`。换言之，MHA 解码允许 q 是一段（如 prefill 的尾巴或 chunked prefill），GQA 解码目前只支持单 token 生成。

---

### 4.3 md5 缓存与 importlib 动态加载

#### 4.3.1 概念说明

降级 + 渲染产出的是一段**源码字符串**。要把这段字符串变成「能 `mod(q,k,v)` 调用」的对象，引擎做了两件事：

1. **缓存**：把源码以「内容指纹」为文件名落盘，避免对相同描述重复降级与编译。
2. **动态加载**：用 `importlib` 把磁盘上的 `.py` 文件加载成一个 Python 模块，取出其中名为 `attention` 的函数（TileLang kernel 入口）挂到 `self.attention`。

这里的「内容指纹」用的是 `hashlib.md5(tl_code)`——即**对最终生成的 TileLang 源码本身**取哈希，而不是对用户描述或形状取哈希。这是一个很巧妙的设计：源码已经把形状、配置、mask 判定结果全部「编进去了」，所以「源码相同」⇔「行为完全相同」，用它当缓存键既精确又无需自己设计键的结构。

> 旁注：MD5 在密码学上已不安全，但这里只用做去重指纹、不涉及安全，足够且快。

#### 4.3.2 核心流程

`_compile_tl` 的后半段是缓存与加载的核心：

```text
tl_code = _select_lower_template(...)        # 得到完整 TileLang 源码字符串
self.tl_code = tl_code                        # 保存，便于调试导出

code_hash = md5(tl_code)                      # 内容指纹
cache_dir = .../attn_engine/cache
file_path = cache_dir/<code_hash>.py
os.makedirs(cache_dir, exist_ok=True)

if not exists(file_path):                     # 仅当缓存未命中才写盘
    write(tl_code -> file_path)

spec = importlib.spec_from_file_location("tl_attn", file_path)
tl_attn = module_from_spec(spec)
spec.loader.exec_module(tl_attn)              # 执行该 .py（触发 TileLang 编译）
self.attention = tl_attn.attention            # 取出 kernel 入口

# block_mask 的归属：infer_mask 决定
self.block_mask = block_mask if infer_mask else None
```

几个要点：

- **缓存命中判定**是 `if not os.path.exists(file_path)`：只要磁盘上已有同名文件就跳过写盘，但**仍会重新 `exec_module`**（即重新触发 TileLang 把源码编成 GPU kernel）。也就是说，「md5 缓存」省的是「降级 + 渲染 + 写源码文件」这一段，而 TileLang 自身的编译缓存由 TileLang 自己管理（见 u1-l2 提到的 TileLang 编译）。
- **生成的 `.py` 必须定义一个名为 `attention` 的顶层符号**，否则 `tl_attn.attention` 会 `AttributeError`。这是引擎与模板层之间的隐式契约（u3-l1 讲的 `attn_tl.py` 骨架里 `@T.prim_func` 定义的 `attention` 即此符号）。
- **block_mask 的两副面孔**：`infer_mask=True` 时，降级会额外产出一个块级掩码张量 `block_mask`，引擎把它存到 `self.block_mask`，运行期由 `__call__` 自动追加到 kernel 调用参数里（见 4.3.5 末尾与综合实践）。

#### 4.3.3 源码精读

`_compile_tl` 先调分发、再把结果（`tl_code`）存档：

[attention_engine/attn_engine/attn_engine.py:350-365](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/attn_engine/attn_engine.py#L350-L365) —— 调 `_select_lower_template` 拿到 `tl_code, block_mask`，并把 `tl_code` 存到 `self.tl_code`（注释里有被注释掉的 `generated_tl.py` 导出代码，是调试用的，见 u5-l6）。

md5 哈希 + 落盘的核心 4 行：

[attention_engine/attn_engine/attn_engine.py:369-376](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/attn_engine/attn_engine.py#L369-L376) —— 缓存键与目录。`cache_dir` 相对当前文件，固定为 `attention_engine/attn_engine/cache/`。

```python
code_hash = hashlib.md5(tl_code.encode()).hexdigest()
cache_dir = os.path.join(os.path.dirname(__file__), "cache")
file_path = os.path.join(cache_dir, f"{code_hash}.py")
os.makedirs(cache_dir, exist_ok=True)
if not os.path.exists(file_path):
    with open(file_path, "w") as f:
        f.write(tl_code)
        f.flush()
```

importlib 动态加载 + 取 `attention` 符号：

[attention_engine/attn_engine/attn_engine.py:379-386](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/attn_engine/attn_engine.py#L379-L386) —— 这三行 `importlib` 套路是 Python 动态加载文件的标配：建 spec → 建模块 → 执行模块。

```python
spec = importlib.util.spec_from_file_location("tl_attn", file_path)
tl_attn = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tl_attn)
self.attention = tl_attn.attention
if infer_mask:
    self.block_mask = block_mask
else:
    self.block_mask = None
```

最后是运行期的 `__call__`，它把构造期编译好的 kernel 与运行期真实张量缝合：

[attention_engine/attn_engine/attn_engine.py:388-395](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/attn_engine/attn_engine.py#L388-L395) —— 若 `self.block_mask` 非空，则把它**追加**到位置参数末尾调用 kernel；否则原样透传。

```python
def __call__(self, *args, **kargs):
    if kargs.get("block_mask") is not None:
        self.block_mask = kargs["block_mask"]
    if self.block_mask is not None:
        o = self.attention(*args, self.block_mask)
    else:
        o = self.attention(*args, **kargs)
    return o
```

这里体现了 `block_mask` 的两种来源：一是构造期由 `infer_mask=True` 自动生成（存于 `self.block_mask`），二是运行期通过 `kargs["block_mask"]` 外部覆盖（如 `extern_block_mask=True` 场景，见 u2-l8）。

#### 4.3.4 代码实践

**实践目标**：亲手触发一次缓存，找到生成的 `.py` 文件，观察「同描述同形状 → 同哈希 → 命中」。

**操作步骤**：

1. 配置好 u1-l2 所述环境（`PYTHONPATH` 挂 `attention_engine/` 与 `3rd_parties/tilelang`，`LD_PRELOAD` 预加载 `libcuda.so`）。
2. 运行 `attn_script/mha.py`：

   ```bash
   cd <repo-root>
   python attn_script/mha.py
   ```
3. 运行结束后，列出缓存目录：

   ```bash
   ls -la attention_engine/attn_engine/cache/
   ```
4. 用 `head` 查看其中某个 `<md5>.py` 文件的开头，确认它是一份合法的 TileLang 源码（会看到 `import tilelang` 之类的字样，以及 `@T.prim_func` 装饰的 `attention` 函数）。
5. **再运行一次** `mha.py`，观察缓存目录文件数量与文件名是否不变（同描述同形状 ⇒ 同 `tl_code` ⇒ 同 md5 ⇒ 命中）。
6. 把 `mha.py` 里的序列长度 `S` 从 `2048` 改成 `4096` 再运行，观察是否多出一个**新的** md5 文件（形状变了 ⇒ 源码变了 ⇒ 新哈希）。

**需要观察的现象**：第一次运行后 `cache/` 出现一个或多个 `<32位十六进制>.py`；第二次运行不新增；改 `S` 后新增一个。

**预期结果**：缓存命中体现在「文件已存在则不重写」，但每次仍会 `exec_module` 重新加载。改形状会产出新文件，因为形状数值被编进了生成源码。

> 待本地验证：第 6 步是否一定新增文件，取决于你改的参数是否真的进入了 `tl_code`（如 `S` 参与 block 划分则一定进入）。若不确定，可对比 `self.tl_code` 的 md5 前后是否变化。

> **调试小技巧**：源码里 [attn_engine.py:366-368](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/attn_engine/attn_engine.py#L366-L368) 有被注释掉的 `generated_tl.py` 导出逻辑。你可以临时取消注释，或直接复制 `cache/` 里的 md5 文件来阅读完整生成代码——这是 u5-l6「分层错误定位」的关键手段。

#### 4.3.5 小练习与答案

**练习 1**：为什么缓存键用「源码的 md5」而不是「`(score_mod, mask_mod, online_func, 形状)` 元组的 md5」？

**参考答案**：因为最终决定 kernel 行为的就是「生成出来的源码」本身。用户函数是 Python 对象，难以稳定序列化（函数对象哈希不稳定）；而源码已经把形状、配置、mask 判定、降级结果全部固化成文本，是最权威、最精确的「行为指纹」。用源码当键既避免了「设计键结构」的麻烦，也保证「键相同 ⇒ 行为必然相同」，不会出现键撞车却行为不同的情况。

**练习 2**：缓存命中（文件已存在）时，引擎会跳过哪一步、又会执行哪一步？

**参考答案**：跳过「写源码文件」（`if not os.path.exists` 保护）；但仍会执行 `importlib` 的 `exec_module`，即重新加载并触发 TileLang 把源码编成 GPU kernel。换言之，md5 缓存省的是「降级 + 渲染 + 写盘」，TileLang 层的编译缓存由 TileLang 自己另行管理。

**练习 3**：`__call__` 里 `self.attention(*args, self.block_mask)` 把 `block_mask` 追加到位置参数末尾。如果用户既在构造时设了 `infer_mask=True`，又在调用时传了 `mod(q,k,v, block_mask=xxx)`，最终用哪个？

**参考答案**：用调用时传的那个。因为 `__call__` 开头先检查 `kargs.get("block_mask")`，若非空则**覆盖** `self.block_mask`，随后追加到参数末尾。运行期外部 `block_mask` 优先级高于构造期自动生成的。

---

## 5. 综合实践

**任务**：把本讲三个模块串起来，做一次「逆向追踪」——给定一个用户脚本，预测并验证它从构造到调用经过的全部环节。

**操作步骤**：

1. 选定 `attn_script/gqa_inference.py` 作为目标（这是一个 GQA 解码场景，q_seqlen=1）。
2. **预测 backend 路径**：该脚本未传 `backend`，预测走 `tl`。
3. **预测形状分发**：从 [gqa_inference.py:107-111](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attn_script/gqa_inference.py#L107-L111) 的 `qkv_meta` 抽出 `q_seqlen=1, kv_len=8192, head=32, head_kv=8`，预测命中第 2 条分支 → `lower_decode_gqa`，并触发 `assert q_seqlen==1` ✓、强制 `infer_mask=True`。
4. **预测 block_mask 行为**：因 `infer_mask=True`，预测 `self.block_mask` 非空，运行期 `__call__` 会把 `block_mask` 追加到 kernel 参数末尾。
5. **运行并核验缓存**：执行 `python attn_script/gqa_inference.py`（如本机有 GPU 与依赖），随后 `ls attention_engine/attn_engine/cache/`，找到新生成的 `<md5>.py`，打开它确认：
   - 文件内有一个 `attention` 函数（被 `self.attention = tl_attn.attention` 取走）。
   - 源码里能看到 GQA 的分组索引、decode 的 split-kv 痕迹（这些是 u4-l2/u4-l3 的内容，本实践只需确认「分发确实选了解码路径」）。
6. **画一张调用链图**：标注 `AttentionEngine.__init__` → `_compile_tl` → `_select_lower_template` → `lower_decode_gqa` → `tl_code` → `md5` → `cache/<md5>.py` → `importlib` → `self.attention`，再画出运行期 `__call__` → `self.attention(q, k, v, block_mask)`。

**预期结果**：你应能完整复述「一次构造 + 一次调用」流经的全部函数与产物，并能解释每一步的判定依据（backend 字符串、四个形状标量、`infer_mask` 标志、文件是否存在）。

> 若无法实际运行，本实践退化为「源码阅读 + 静态推断」：仅完成步骤 1–4 与步骤 6，把步骤 5 的核验标注为「待本地验证」。

## 6. 本讲小结

- `AttentionEngine.__init__` 是整个框架对外的总入口，第一件事就是按 `backend`（默认 `"tl"`）分流：tl 走 `_compile_tl`，cute 直接 `lower_cute`。
- **tl 后端做形状分发，cute 后端不做**——这是两条后端的关键不对称。cute 只按 `kv_shared` 二选一（普通 flash / kv_shared MLA）。
- 形状分发由 `_select_lower_template` 完成，依据从 `qkv_meta` 抽取的 `q_seqlen`/`kv_len`/`head`/`head_kv` 与 `kv_shared`，在 5 条路径间互斥选择：`kv_shared`→`lower_decode_mla`，GQA 解码→`lower_decode_gqa`（要求 `q_seqlen==1`），MHA 解码→`lower_decode`（仅要求 `q_seqlen<kv_len`），训练 MHA→`lower`，训练 GQA→`lower_gqa`。
- 缓存键是**生成源码的 md5**，落盘于 `attention_engine/attn_engine/cache/<md5>.py`；命中时跳过写盘，但每次仍用 `importlib` 重新 `exec_module` 加载。
- 动态加载取出的 `tl_attn.attention` 被挂到 `self.attention`，这是引擎与模板层之间「生成文件必须定义 `attention` 符号」的隐式契约。
- `__call__` 在运行期把真实张量喂给 kernel；`block_mask` 可由构造期 `infer_mask=True` 自动生成，也可由运行期 `kargs["block_mask"]` 外部覆盖，后者优先级更高。

## 7. 下一步学习建议

本讲把「编译链内部」与「外部调用」对接完毕。接下来建议：

- **横向进入第四单元**：u4-l1 讲线性注意力引擎 `LinearAttentionEngine`（与 `AttentionEngine` 平行的另一入口），u4-l2/u4-l3/u4-l4 分别深入本讲提到的 `lower_gqa` / `lower_decode` / `lower_decode_mla` 的内部结构——你可以带着本讲的分发地图，挑一条路径钻进去。
- **若对性能调优感兴趣**：u5-l3（autotuner）会讲 `_select_lower_template` 里 `tuned_config`、`tune`/`tune_file` 这些参数如何驱动配置搜索。
- **若对 CuTe 后端感兴趣**：u5-l1 / u5-l2 会展开本讲简略带过的 cute + kv_shared 分支（`block_table`、`tile_scheduler_metadata`、`flash_mla_with_kvcache`）。
- **调试技巧**：本讲提到的「导出 cache 里的 md5 文件阅读」会在 u5-l6 系统化讲授，建议届时回看本讲的 4.3.4。
