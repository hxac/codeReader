# MLX 流式生成循环与缓存回滚

## 1. 本讲目标

上一讲（u3-l1）我们把 MLX 版草稿模型的**静态结构**讲透了：`DFlashConfig` 怎么读、`load_draft` 怎么手动加载、`DFlashAttention` 怎么把 context / proposal 拼接、`bind` 怎么向 target 借 `embed_tokens` 与 `lm_head`。但那套结构一直是「待被调用的对象」——我们还没看它怎么真正被驱动起来做一次投机解码。

本讲就打开 `dflash/model_mlx.py` 里的**生成驱动层**：`stream_generate`。它是 MLX 后端的推理主函数，负责把「草稿起草 + target 验证」一遍遍地跑下去，并**流式**地把文本吐给调用方。学完后你应该能够：

1. 读懂 `stream_generate` 的三段式结构：**prefill**（预填充）→ **decode 循环**（反复块起草 / 验证 / 接受）→ **收尾**，并说清每一轮产出了多少 token、target 与 draft 各被调了几次。
2. 理解 `_LayerHook` + `_patch_model` + `_get_layers` 如何用**钩子（hook）**捕获 target 指定层的隐藏状态——这正是 u3-l1 里 `target_hidden`（草稿的 context 输入）的真正来源。
3. 理解当一个草稿块被 target 拒绝后，`_trim_recent_cache` 如何把 target 与 draft 的 KV 缓存**回滚**到干净状态，特别是为什么 `trim = bs - accepted - 1`、以及为什么 `hidden` 要被切片到 `accepted + 1`。
4. 读懂 `GenerationResponse` 这个数据类如何把每一轮的文本、token、接受长度、吞吐、显存打包成流式结果。

> 本讲的核心命题和 u2-l1 / u2-l4（Transformers 版的 `dflash_generate`）完全一致——草稿起草、target 验证、最长公共前缀接受、每轮产出 `accepted + 1` 个 token。差别全在**工程实现**：MLX 的惰性求值、流式 generator、钩子捕获、以及一套更细粒度的缓存回滚。建议你带着「对照 u2-l4」的心态来读。

## 2. 前置知识

本讲假设你已经读过：

- **u3-l1**：MLX 版草稿模型的结构。尤其是 `DFlashAttention.__call__` 里「**只有 context 进 `update_and_fetch` 缓存，proposal 拼接后不持久化**」这一条——本讲的回滚机制正是建立在这条设计上。
- **u2-l4**：投机解码的「验证 + 接受 + 裁剪」算法——`accepted` 是草稿与 target 的最长公共前缀长度，每轮产出 `accepted + 1`（含一个永不丢失的 target 兜底 token）。

下面几个 MLX / Python 相关的概念，用通俗语言补齐：

- **生成器函数（generator）与 `yield`**：Python 里带 `yield` 的函数不是「调用即返回结果」，而是返回一个**惰性迭代器**。每次 `next()`（或 `for` 循环）执行到 `yield` 就暂停、把 `yield` 后面的值吐出来，下次再从暂停处继续。`stream_generate` 就是一个生成器——它一边解码一边把新文本 `yield` 出去，调用方可以「边收边打印」，不必等整段生成完。
- **MLX 的惰性求值与 `mx.eval` / `mx.async_eval`**：和 u3-l1 提到的一样，MLX 算子是 lazy 的——写 `a + b` 只是建图，并不立即算。`mx.eval(x)` 会**阻塞**到 `x` 真正算完；`mx.async_eval(x)` 则**只排队、不阻塞**，让 GPU 在后台算的同时 CPU 继续往下走。`stream_generate` 里反复用 `mx.async_eval` 来把「草稿前向」和「后续准备」重叠起来，这是它高效的关键之一。
- **`mx.stream(generation_stream)`**：把一段算子放进一条专用的执行流（stream）里提交，便于和主计算流解耦调度。`generation_stream` 来自 `mlx_lm.generate`。
- **`make_prompt_cache(model)`**：`mlx_lm` 提供的工具，按模型的层数一次性建好一组**逐层** KV 缓存（一个 list，每个元素是一层的 `KVCache` 或 `RotatingKVCache`）。本讲里 `target_cache` 和 `draft_cache` 都靠它生成。
- **`can_trim_prompt_cache(cache)`**：`mlx_lm` 提供的检测函数，判断这组缓存是否支持 `trim`（即从尾部删掉最近若干 token 的 K/V）。普通 `KVCache` / `RotatingKVCache` 支持，但**混合架构里 GatedDeltaNet 这类带「状态」的层不支持**——这一点直接决定了回滚要走哪条路径，是本讲与下一讲（u3-l3）的衔接点。

> 一句话定位：本讲只讲「**普通可裁剪缓存**」路径（`_target_can_trim == True`），即 Qwen3 / Gemma 这类纯注意力模型。当 target 不可裁剪（混合架构）时，回滚会改走 `_GDNStateCapture.rollback`，那是 u3-l3 的主题，本讲只在交界处点一句。

## 3. 本讲源码地图

本讲全部围绕一个文件，并适度对照第二单元的参考实现：

| 文件 | 本讲关注的内容 |
|---|---|
| `dflash/model_mlx.py` | `_LayerHook` / `_get_layers` / `_patch_model`（钩子捕获）、`stream_generate`（prefill + decode 主循环）、`_trim_recent_cache`（缓存回滚）、`GenerationResponse` / `_make_response`（流式结果） |
| `dflash/model.py`（对照） | `dflash_generate` 的 decode 循环（u2-l1 / u2-l4）、`extract_context_feature`（对照钩子捕获） |

> `model_mlx.py` 里的 `_GDNStateCapture`（L293–L397）属于混合架构的状态回滚，是 u3-l3 的主题，本讲只在 `stream_generate` 调用它的两行处一笔带过。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

1. **4.1** 钩子捕获 target 隐藏状态：`_LayerHook` / `_get_layers` / `_patch_model`。
2. **4.2** 流式生成主循环：`stream_generate` 的 prefill、块起草、target 验证、接受计算。
3. **4.3** 拒绝后的缓存回滚与结果构造：`_trim_recent_cache`、`trim = bs - accepted - 1`、`hidden` 切片、`GenerationResponse`。

---

### 4.1 钩子捕获 target 隐藏状态：_LayerHook / _get_layers / _patch_model

#### 4.1.1 概念说明

回顾 u2-l2 / u3-l1：草稿模型不是「从零」起草，而是先从 target 的若干中间层抽取**隐藏状态**（`target_hidden`），再用一个 `fc` 层把多层特征投影回 draft 的表示空间，作为起草的「上下文」。在 Transformers 版里这件事由独立函数 `extract_context_feature` 完成——显式地把 target 多层输出 `cat` 起来。

MLX 版没有这样一个独立函数，而是用一个**更轻巧、更通用**的办法：**给 target 的指定层套一层钩子（hook）**。钩子是一个「包裹」对象，外形和原层一模一样、调用时先调原层、再把原层的输出（隐藏状态）偷偷抄一份存到共享列表里。于是只要 target 正常跑一遍前向，我们要的那几层隐藏状态就自动落进了 `model._hidden_states`，再 `concat` 一下就得到了 `target_hidden`。

这样做有三个好处：

- **零侵入**：不用改 target 模型的源码，只在运行前把目标层「换装」一次。
- **天然跟随 target 的每一次前向**：prefill 时捕获 prompt 的隐藏状态、每次验证时捕获新 token 的隐藏状态——只需在调用后读 `model._hidden_states`。
- **复用任意 target 结构**：通过 `_get_layers` 适配三种常见的 target 封装层级。

#### 4.1.2 核心流程

钩子捕获的整体流程：

1. `_patch_model(model, layer_ids)` 在 target 上挂一个 `_hidden_states` 列表（长度 = 要捕获的层数），并把 `layer_ids` 指定的那些 target 层替换成 `_LayerHook` 包裹。
2. 后续任何对 target 的前向调用（`model(...)`）中，被包裹的层照常计算并返回完整输出，**同时**把输出的隐藏状态写入 `_hidden_states[idx]`。
3. 调用方用 `mx.concatenate(model._hidden_states, axis=-1)` 把多层隐藏状态沿特征维拼起来，得到 `target_hidden`。

伪代码：

```text
_patch_model(target, target_layer_ids):
    target._hidden_states = [None] * len(target_layer_ids)
    layers = _get_layers(target)            # 找到 target 的解码层列表
    for i, lid in enumerate(target_layer_ids):
        layers[lid] = _LayerHook(layers[lid], i, target._hidden_states)  # 换装

# 之后每次 target 前向：
out = target(input)                          # 被包裹层自动把 hidden 存进 _hidden_states
target_hidden = concat(target._hidden_states, axis=-1)   # 多层特征拼接
```

#### 4.1.3 源码精读

先看钩子本体 [`_LayerHook`](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/model_mlx.py#L261-L271)：

```python
class _LayerHook:
    def __init__(self, layer, idx, storage):
        self._layer, self._idx, self._storage = layer, idx, storage

    def __call__(self, *args, **kwargs):
        out = self._layer(*args, **kwargs)
        self._storage[self._idx] = out[0] if isinstance(out, tuple) else out
        return out

    def __getattr__(self, name):
        return getattr(self._layer, name)
```

要点：

- `__call__` 先调真正的层 `self._layer(...)`，拿到完整输出 `out`；MLX 里解码层通常返回**元组**（如 `(hidden_states, ...)`），所以用 `out[0] if isinstance(out, tuple) else out` 取出隐藏状态，存进 `self._storage[self._idx]`；最后**原样返回 `out`**，保证 target 的前向完全不受影响。
- `__getattr__` 把任何属性访问**转发**给原层（`getattr(self._layer, name)`）。这一步很关键：MLX / mlx_lm 的代码有时会读取层的属性（如权重、配置），钩子必须「伪装」得和原层一样，否则会 `AttributeError`。注意它存在 `self._layer`/`self._idx`/`self._storage` 这几个「真属性」是通过 `__init__` 里的直接赋值绕过 `__getattr__` 的，否则会无限递归。

再看 [`_get_layers`](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/model_mlx.py#L274-L281)，它适配三种 target 封装：

```python
def _get_layers(model):
    if hasattr(model, "model") and hasattr(model.model, "layers"):
        return model.model.layers
    if hasattr(model, "language_model") and hasattr(model.language_model, "layers"):
        return model.language_model.layers
    if hasattr(model, "layers"):
        return model.layers
    raise AttributeError(f"Cannot find layers in {type(model).__name__}")
```

三种形态分别对应：标准因果 LM（`model.model.layers`，Qwen3 / Gemma 多为这种）、某些把语言模型挂在 `language_model` 下的封装、以及直接 `model.layers` 的裸模型。这与 u3-l1 里 `bind()` 探测 `embed_tokens` 的三层兜底是同一套思路——MLX 后端要兼容各种 target 结构。

最后是挂载函数 [`_patch_model`](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/model_mlx.py#L284-L290)：

```python
def _patch_model(model, layer_ids):
    if hasattr(model, "_hidden_states"):
        return
    model._hidden_states = [None] * len(layer_ids)
    layers = _get_layers(model)
    for i, lid in enumerate(layer_ids):
        layers[lid] = _LayerHook(layers[lid], i, model._hidden_states)
```

两个细节：

- **幂等**：开头 `if hasattr(model, "_hidden_states"): return` 保证多次调用 `stream_generate` 不会重复套钩子（重复套会让 `_hidden_states` 越来越深、且层被包多层）。`stream_generate` 每次进来都会调一次 `_patch_model`，这条守卫让重复 patch 安全。
- **就地替换**：`layers[lid] = _LayerHook(...)` 直接改了 target 的层列表——这是 monkey-patch（猴子补丁），运行期动态替换对象方法。钩子和原层共用同一个 `model._hidden_states` 列表引用，所以只要 target 跑一次，列表里就填满了。

#### 4.1.4 代码实践

**实践目标**：亲眼看到 `_LayerHook` 是在 target 前向时被动触发、并把多层隐藏状态收集到 `_hidden_states` 的。

**操作步骤**（源码阅读型，需 Apple 芯片 + 已装 `.[mlx]` 才能真正运行；无环境时可只做步骤 1–3 的阅读）：

1. 读 [`_patch_model`](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/model_mlx.py#L284-L290) 与 [`stream_generate`](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/model_mlx.py#L429-L432) 的开头，确认 `_patch_model(model, draft.config.target_layer_ids)` 是生成函数的第一行。
2. 在本地写一段最小脚本（**示例代码**，非项目原码）：

   ```python
   from dflash.model_mlx import load, load_draft, _patch_model
   import mlx.core as mx

   model, tokenizer = load("Qwen/Qwen3-8B")
   draft = load_draft("z-lab/Qwen3-8B-DFlash-b16")
   _patch_model(model, draft.config.target_layer_ids)
   print("hooked?", hasattr(model, "_hidden_states"), "n_layers=", len(model._hidden_states))
   print("before forward:", [x is None for x in model._hidden_states])

   logits = model(tokenizer.encode("Hi", add_special_tokens=True).__class__(mx.array(tokenizer.encode("Hi")))[None])
   print("after forward filled:", sum(x is not None for x in model._hidden_states))
   ```

3. 对照 [`_LayerHook.__call__`](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/model_mlx.py#L265-L268)，解释为什么「调一次 `model(...)`，`_hidden_states` 里所有槽位就都非 None 了」。

**需要观察的现象**：

- patch 之后 `model._hidden_states` 是一个长度等于 `len(target_layer_ids)`、全为 `None` 的列表。
- target 前向之后，每个槽位变成一个 `mx.array`，形状是 `[1, 序列长度, hidden_size]`。

**预期结果**：钩子在 target 前向时被逐层触发，把每层隐藏状态抄进对应槽位。若本地无法运行 MLX，**待本地验证**，可改为阅读 u3-l1 的 `extract_context_feature` 对照理解「多层沿特征维拼接」这件事在这里由 `concat(..., axis=-1)` 等价完成。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `_LayerHook.__getattr__` 必须把 `self._layer` / `self._idx` / `self._storage` 排除在转发之外（即用 `__init__` 直接赋值而不是走 `__getattr__`）？

> **参考答案**：`__getattr__` 只在「常规属性查找失败」时触发。`__init__` 里 `self._layer = layer` 会把 `_layer` 写进实例的 `__dict__`，常规查找就能命中、不会进 `__getattr__`；若改成在 `__getattr__` 里返回 `_layer`，访问 `self._layer` 会触发 `__getattr__`，而它内部又要 `getattr(self._layer, '_layer')`，形成无限递归。直接赋值是规避递归的标准写法。

**练习 2**：`_patch_model` 开头的幂等守卫 `if hasattr(model, "_hidden_states"): return` 去掉会有什么后果？

> **参考答案**：`stream_generate` 每次调用都会执行 `_patch_model`；若无守卫，第二次调用会给已经包了钩子的层**再包一层**钩子，导致 `model._hidden_states` 被重置但外层钩子仍指向旧的存储、捕获层数翻倍、甚至 `idx` 越界。守卫保证「同一个 target 只 patch 一次」。

**练习 3**：对照 Transformers 版的 `extract_context_feature`（u2-l2/u3-l1），MLX 版用钩子捕获有哪些好处？

> **参考答案**：无需写专门的提取函数、无需改 target 源码；钩子自动跟随 target 的每一次前向（prefill 与每次验证都能拿到最新隐藏状态）；通过 `_get_layers` 兼容多种 target 结构，通用性更强。代价是引入了 monkey-patch 的隐式副作用。

---

### 4.2 流式生成主循环：stream_generate 的 prefill 与 decode

#### 4.2.1 概念说明

`stream_generate` 是 MLX 后端的推理主函数，也是一个 Python **生成器**。它把投机解码的「草稿起草 + target 验证」循环跑起来，每接受一批 token 就 `yield` 一个 `GenerationResponse`，调用方可以边收边打印，实现**流式输出**。

它的整体形态与 u2-l1 讲过的 Transformers 版 `dflash_generate` 一致——三阶段：

1. **prefill（预填充）**：把整段 prompt 一次性喂给 target，建立 prompt 的 KV 缓存、捕获 prompt 的隐藏状态（作为第一轮草稿的 context）、采样出**第一个 token**。
2. **decode 循环**：反复执行「块起草 → target 验证 → 计算接受长度 → 产出 → 回滚」。每轮产出 `accepted + 1` 个 token，其中 `accepted` 是草稿与 target 的最长公共前缀长度，`+1` 是 target 给的兜底 token（永不丢失）。
3. **收尾**：触发 EOS 提前结束，或达到 `max_tokens` 以 `"length"` 收尾。

和 Transformers 版相比，这里的几个 MLX 特色：

- **惰性 + 异步**：草稿前向、target 验证都用 `mx.async_eval` 排队，让 CPU/GPU 重叠工作。
- **流式**：用 `yield` 而非一次性 `return`。
- **隐藏状态靠钩子拿**：每次 target 前向后 `mx.concatenate(model._hidden_states, axis=-1)` 重新拼出 `hidden`。
- **块首锚点**：草稿块不是纯 mask，而是 `[上一个确认 token, mask, mask, …]`——第一个位置是「锚点」，给草稿一个确定的起点。

#### 4.2.2 核心流程

把 `stream_generate` 的核心流程画成伪代码（省略类型转换与流式细节）：

```text
stream_generate(model, draft, tokenizer, prompt, block_size, max_tokens, temperature):
    _patch_model(model, draft.config.target_layer_ids)      # 挂钩子
    bs0 = block_size or draft.config.block_size
    target_cache = make_prompt_cache(model)
    draft_cache  = make_prompt_cache(draft)
    draft.bind(model)                                       # 借 embed_tokens / lm_head

    # —— prefill ——
    logits = model(prompt, target_cache)                    # target 预填充，写 prompt 的 K/V
    hidden = concat(model._hidden_states, axis=-1)          # 捕获 prompt 多层隐藏状态
    token  = sample(logits[:, -1:])                         # 第一个 token
    yield 第一个 token（accepted=1）

    # —— decode 循环 ——
    while n < max_tokens:
        bs = min(bs0, max_tokens - n + 1)
        # (a) 块起草
        block = [tokens[-1]] + [mask] * (bs - 1)            # 锚点 + 一串 mask
        draft_logits = draft(block, hidden, draft_cache, logits_start=1)
        draft_tokens = sample(draft_logits)                 # bs-1 个候选
        # (b) target 验证
        verify_input = [tokens[-1]] + draft_tokens          # 长度 bs
        logits  = model(verify_input, target_cache)         # target 一次前向验证整块
        hidden  = concat(model._hidden_states, axis=-1)     # 新的隐藏状态
        target_tokens = sample(logits)                      # target 的「正解」
        # (c) 接受长度 = 最长公共前缀
        accepted = 第一个 draft_tokens[i] != target_tokens[i] 的 i
        new_tokens = draft_tokens[:accepted] + [target_tokens[accepted]]   # accepted + 1 个
        yield new_tokens
        # (d) 回滚（详见 4.3）
        trim = bs - accepted - 1
        if trim > 0: _trim_recent_cache(target_cache, trim)
        hidden = hidden[:, :accepted + 1, :]
```

#### 4.2.3 源码精读

先看 [`stream_generate` 的签名与准备段](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/model_mlx.py#L429-L459)：

```python
def stream_generate(
    model, draft, tokenizer, prompt,
    block_size=None, max_tokens=256, temperature=0.0, sampler=None,
):
    _patch_model(model, draft.config.target_layer_ids)
    block_size = block_size if block_size is not None else int(draft.config.block_size)
    sampler = sampler or make_sampler(temp=temperature)
    ...
    target_cache = make_prompt_cache(model)
    draft_cache = make_prompt_cache(draft)
    draft.bind(model)
    _target_can_trim = can_trim_prompt_cache(target_cache)
    if not _target_can_trim and not _HAS_GDN:
        raise RuntimeError(
            "This MLX model requires gated-delta rollback support, but "
            "mlx_lm.models.gated_delta is unavailable."
        )
    _capture = _GDNStateCapture() if not _target_can_trim else None
```

要点：

- 第一行就 `_patch_model`，挂钩子；`block_size` 不传则取草稿 config 里的 `block_size`（README 示例传 `block_size=16`）。
- `sampler = sampler or make_sampler(temp=temperature)`：采样器统一用 `mlx_lm` 的 `make_sampler`，温度由 `temperature` 控制（u2-l4 讲过：升温会降低草稿命中率、削弱加速，但不改变输出分布）。
- 建两套缓存：`target_cache`（target 用）、`draft_cache`（草稿用）；`draft.bind(model)` 完成 u3-l1 讲过的「两个借」。
- `_target_can_trim = can_trim_prompt_cache(target_cache)`：**关键分流**。若 target 缓存可裁剪（普通注意力模型），回滚走 `_trim_recent_cache`；若不可裁剪（GatedDeltaNet 混合层），必须依赖 `_HAS_GDN`，否则直接 `RuntimeError`。这条分流决定了本讲（4.3）讲哪条路径、u3-l3 讲哪条路径。

接着是 [prefill 段](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/model_mlx.py#L461-L489)：

```python
    tic = time.perf_counter()
    with mx.stream(generation_stream):
        logits = model(prompt[None], target_cache)
        hidden = mx.concatenate(model._hidden_states, axis=-1)
    mx.eval(logits, hidden)
    prompt_tps = prompt.size / (time.perf_counter() - tic)

    tic = time.perf_counter()
    token = sampler(logits[:, -1:])[0, 0].item()
    tokens.append(token)
    n = 1
    ...
    detokenizer.add_token(token)
    yield _make_response(detokenizer.last_segment, [token], 1, prompt.size, prompt_tps, n, tic)
```

- `model(prompt[None], target_cache)`：target 对整段 prompt 做一次前向，把 prompt 的 K/V 写进 `target_cache`；同时钩子把各层对**整段 prompt** 的隐藏状态抄进 `model._hidden_states`。
- `hidden = mx.concatenate(model._hidden_states, axis=-1)`：沿特征维拼多层 → `hidden` 形状 `[1, prompt.size, num_target_layers * hidden_size]`，这正是第一轮草稿要的 context。
- `mx.eval(logits, hidden)`：阻塞等算完，好准确测 prefill 耗时（`prompt_tps`）。
- 从 `logits[:, -1:]` 采样第一个 token，`n = 1`，`yield` 出去。注意 `accepted` 这里传 `1`（首 token 视作 1 个产出）。至此 prompt 的隐藏状态已就位、第一个 token 已产出。

接下来是 [decode 主循环](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/model_mlx.py#L491-L507) 的「块起草」段：

```python
        while n < max_tokens:
            bs = min(block_size, max_tokens - n + 1)
            if bs <= 1:
                break

            with mx.stream(generation_stream):
                block = mx.array([[tokens[-1]] + [mask_id] * (bs - 1)])
                draft_logits = draft(
                    block,
                    hidden,
                    draft_cache,
                    logits_start=1,
                )
                if (trim_n := draft_cache[0].offset - (prompt.size + n - 1)) > 0:
                    _trim_recent_cache(draft_cache, trim_n)
                draft_tokens = sampler(draft_logits)
            mx.async_eval(draft_tokens)
```

要点：

- `bs = min(block_size, max_tokens - n + 1)`：本轮块大小，到尾部会收缩；`bs <= 1` 时没有投机空间，直接退出。
- `block = [[tokens[-1]] + [mask_id] * (bs - 1)]`：**锚点 + 一串 mask**。锚点 `tokens[-1]` 是最近一个已确认 token；草稿要在这之后并行去噪还原出 `bs - 1` 个候选。
- `draft(block, hidden, draft_cache, logits_start=1)`：草稿前向。`logits_start=1` 让草稿丢弃锚点位置的 logits（u3-l1 讲过），只对 `bs - 1` 个 mask 位置出 logits → `draft_tokens` 是 `bs - 1` 个候选。
- `trim_n := draft_cache[0].offset - (prompt.size + n - 1)`：一条**防御性对齐**。草稿缓存里只存 context（u3-l1：proposal 不进缓存），理论应恰好等于「所有已确认 token 数 − 1」（减 1 是因为最新的锚点 token 放在 block 里、不进 context）。正常稳态下这个差值为 0；若因任何原因多写了，就裁掉多余部分。详见 4.3 的推导。
- `mx.async_eval(draft_tokens)`：异步排队，不阻塞，让 target 验证可以尽快开始。

接着是 [target 验证段](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/model_mlx.py#L509-L516)：

```python
            if _capture is not None:
                _capture.clear()
            with mx.stream(generation_stream):
                verify_input = mx.concatenate([mx.array([[tokens[-1]]]), draft_tokens], axis=1)
                logits = model(verify_input, target_cache)
                hidden = mx.concatenate(model._hidden_states, axis=-1)
                target_tokens = sampler(logits)
            mx.async_eval(target_tokens, hidden)
```

- `_capture.clear()`：这是混合架构（GDN）路径的清理（u3-l3），普通路径 `_capture is None` 跳过。
- `verify_input = concat([[tokens[-1]]], draft_tokens)`：把锚点和 `bs - 1` 个草稿候选拼成长度 `bs` 的序列，**一次性**喂给 target 验证——这就是投机解码用一次 target 前向并行验证多个候选的核心。
- target 前向时钩子再次捕获隐藏状态 → `hidden` 更新为本轮 `bs` 个位置的多层隐藏；`target_tokens = sampler(logits)` 是 target 在这 `bs` 个位置上的「正解」。
- `mx.async_eval(target_tokens, hidden)`：异步排队。

然后是 [接受计算与产出](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/model_mlx.py#L518-L559)：

```python
            d_list, t_list = draft_tokens[0].tolist(), target_tokens[0].tolist()
            accepted = next((i for i in range(len(d_list)) if d_list[i] != t_list[i]), len(d_list))
            new_tokens = d_list[:accepted] + [t_list[accepted]]
            new_tokens = new_tokens[:max_tokens - n]

            eos_idx = next((i for i, t in enumerate(new_tokens) if t in tokenizer.eos_token_ids), None)
            if eos_idx is not None:
                ...  # 命中 EOS：写入、finalize、yield(accepted+1, "stop")、return
            ...
            tokens.extend(new_tokens)
            n += len(new_tokens)
            ...
            yield _make_response(detokenizer.last_segment, new_tokens, accepted + 1, prompt.size, prompt_tps, n, tic)
```

逐行拆解「最长公共前缀」：

- `d_list`（长度 `bs - 1`）是草稿候选 `d₁, d₂, …, d_{bs-1}`；`t_list`（长度 `bs`）是 target 在 `verify_input = [锚点, d₁, …, d_{bs-1}]` 各位置上的下一步预测 `t₀, t₁, …, t_{bs-1}`。
- `t_i` 是 target 看到 `verify_input[i]` 之后预测的下一个 token：`t₀` 对应「锚点之后」、应与草稿的第一个候选 `d₁` 比；`t₁` 对应「`d₁` 之后」、应与 `d₂` 比……所以 `d_list[i]`（即 `d_{i+1}`）正好与 `t_list[i]`（即 `t_i`）对齐。
- `accepted = next((i for i in range(len(d_list)) if d_list[i] != t_list[i]), len(d_list))`：第一个不相等的位置；若全相等则为 `bs - 1`。这就是最长公共前缀长度。
- `new_tokens = d_list[:accepted] + [t_list[accepted]]`：前 `accepted` 个被接受的草稿候选，加上 target 在拒绝处的兜底 `t_list[accepted]`——共 `accepted + 1` 个。兜底 token 来自 target、必正确，所以**每轮至少产出 1 个 token**，永不卡死。

> 这与 u2-l4 的结论一致：因为草稿候选是贪心采样（`make_sampler` 在 `temperature=0` 时退化为 argmax），「被接受 = 与 target 一致」，被拒处写回的也是 target 的正解，因此 MLX 版输出与单独用 target 采样**完全等价**，是纯加速手段、不损失质量。

最后是 [回滚段](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/model_mlx.py#L561-L567)（细节推导放 4.3）：

```python
            trim = bs - accepted - 1
            if trim > 0:
                if _target_can_trim:
                    _trim_recent_cache(target_cache, trim)
                elif _capture is not None:
                    _capture.rollback(target_cache, accepted, trim)
            hidden = hidden[:, :accepted + 1, :]
```

- `trim = bs - accepted - 1`：本轮被 target 拒绝的候选数，也是要从 target 缓存尾部裁掉的 K/V 数量。
- `if _target_can_trim: _trim_recent_cache(...)`：普通可裁剪路径（本讲）；`elif _capture is not None: _capture.rollback(...)`：混合架构路径（u3-l3）。
- `hidden = hidden[:, :accepted + 1, :]`：把本轮捕获的 `bs` 个隐藏状态砍到前 `accepted + 1` 个，作为下一轮草稿的 context。

#### 4.2.4 代码实践

**实践目标**：在 `stream_generate` 的 decode 循环里插入打印，观察每一轮的 `bs / accepted / new_tokens` 数量关系，验证「每轮产出 `accepted + 1`、且 `accepted < bs`」。

**操作步骤**（需 Apple 芯片 + `.[mlx]` 环境；无环境请做步骤 1–2 的源码阅读）：

1. 在 [`dflash/model_mlx.py` 的 L519 与 L520 之间](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/model_mlx.py#L518-L521) 临时加一行（**示例代码**，调试用，验证后请还原，勿提交）：

   ```python
   print(f"[round] bs={bs} accepted={accepted} produced={len(new_tokens)} trim={bs - accepted - 1}")
   ```

2. 跑 README 的 MLX 示例（`stream_generate(model, draft, tokenizer, prompt, block_size=16, max_tokens=128, temperature=0.6)`），收集所有 `[round]` 行。
3. 用 `r.accepted`（`GenerationResponse.accepted`，见 4.3）对照你打印的 `accepted + 1`，确认它们一致。

**需要观察的现象**：

- 每轮 `produced == accepted + 1`（除最后一轮可能受 `max_tokens` 截断）。
- `accepted` 在 `[0, bs-1]` 之间波动；温度越高、平均 `accepted` 越低。
- `trim = bs - accepted - 1` 始终 `>= 0`。

**预期结果**：`accepted` 的平均值（加速比的关键）随 `temperature` 上升而下降；`produced` 恒为 `accepted + 1`。若本地无法运行 MLX，**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `block` 的第一个位置是 `tokens[-1]`（锚点）而不是 `mask_id`？

> **参考答案**：块扩散需要一个「确定的起点」——锚点是最近一个已被 target 确认的 token，草稿从它出发并行去噪还原后续位置。若第一位置也是 mask，草稿就失去与上下文衔接的确定锚点，无法稳定起草。`logits_start=1` 进一步丢弃锚点位置的 logits（它不是要预测的对象）。

**练习 2**：`d_list` 长度是 `bs - 1`、`t_list` 长度是 `bs`，为什么比较 `d_list[i] != t_list[i]` 是正确的对齐？

> **参考答案**：`verify_input = [锚点, d₁, …, d_{bs-1}]`，`t_i` 是 target 在 `verify_input[i]` 之后预测的下一个 token。`t₀`（锚点之后）应与草稿对「锚点之后」的预测 `d₁ = d_list[0]` 比；`t₁`（`d₁` 之后）应与 `d₂ = d_list[1]` 比……因此 `d_list[i]` 与 `t_list[i]` 天然一一对应。公共前缀长度即 `accepted`。

**练习 3**：prefill 阶段用 `mx.eval`，decode 阶段用 `mx.async_eval`，为什么？

> **参考答案**：prefill 需要准确测量耗时（`prompt_tps`），必须阻塞到算完，故 `mx.eval`。decode 里草稿前向与 target 验证之间存在依赖、但可以把「排队」与「CPU 准备下一轮」重叠，故用 `mx.async_eval` 只排队不阻塞，提升吞吐。这是 MLX 惰性求值带来的调度自由度。

---

### 4.3 拒绝后的缓存回滚与结果构造：_trim_recent_cache 与 GenerationResponse

#### 4.3.1 概念说明

投机解码有一个绕不开的善后问题：**被 target 拒绝的草稿候选，已经把它们的 K/V 写进了 target 的 KV 缓存**（因为验证时 target 对 `[锚点, d₁, …, d_{bs-1}]` 整段做了前向、整段都进了缓存）。这些「脏」K/V 对应的是 target 否决的分支，必须**回滚**（裁掉），否则下一轮 target 会基于错误的历史继续算，输出立刻错乱。

MLX 版的回滚有一个先天的优势（u3-l1 已铺垫）：**草稿缓存里只有 context、没有 proposal**，所以 `draft_cache` 几乎不需要为「拒绝」回滚——被拒的草稿候选压根没进过 draft 缓存。需要认真回滚的是 **target 缓存**：要把本轮新写入的 `bs` 个位置中、属于被拒分支的部分裁掉，只留下与「已确认 token」对应的那一段。

本模块讲三件事：

1. `_trim_recent_cache`：从一组逐层缓存的**尾部**删掉最近 `n` 个 token 的 K/V（普通 `KVCache` 走 `trim`、`RotatingKVCache` 手动切片）。
2. 为什么 `trim = bs - accepted - 1`、为什么 `hidden` 切片到 `accepted + 1`——这是本讲最需要算清楚的一处。
3. `GenerationResponse` 与 `_make_response`：把每一轮的文本、token、接受长度、吞吐、显存打包成流式结果。

#### 4.3.2 核心流程

**回滚的数量推导**（关键）。设进入某轮时：

- 已确认的生成 token 数为 `n`（含 prefill 的首 token）。
- 该轮 verify 前，target 缓存里保存的是「prompt + 已确认 token − 最新锚点」对应的 K/V，长度为 `prompt.size + n - 1`（最新锚点 `tokens[-1]` 是上一步 target 的输出、尚未喂回 target，故减 1）。

该轮 verify 喂入 `verify_input`（长度 `bs`）后，target 缓存长度变为：

\[
\text{offset}_{\text{after verify}} = (\text{prompt.size} + n - 1) + \text{bs}
\]

本轮接受 `accepted` 个草稿候选 + 1 个 target 兜底，共确认 `accepted + 1` 个新 token，因此 `n` 更新为 `n' = n + (\text{accepted} + 1)`。下一轮开始前，target 缓存的**正确**长度应为「prompt + 新的已确认数 − 新锚点」：

\[
\text{offset}_{\text{should}} = \text{prompt.size} + n' - 1 = \text{prompt.size} + n + \text{accepted}
\]

所以需要从尾部裁掉的数量为：

\[
\text{trim} = \text{offset}_{\text{after verify}} - \text{offset}_{\text{should}} = \text{bs} - \text{accepted} - 1
\]

这就是 `trim = bs - accepted - 1` 的来源——它正好等于本轮被 target 拒绝的草稿候选数（`bs - 1` 个候选里接受了 `accepted` 个，剩下 `bs - 1 - accepted` 个被拒；这些被拒候选的 K/V 占据缓存尾部，必须裁掉）。

**`hidden` 切片推导**。本轮 verify 捕获的 `hidden` 覆盖 `verify_input = [锚点, d₁, …, d_{bs-1}]` 共 `bs` 个位置。其中前 `accepted + 1` 个位置 `[锚点, d₁, …, d_{accepted}]` 对应「新锚点 `t_{accepted}` 之前的所有已确认 token」——这正是下一轮草稿需要的 context。而位置 `accepted`（即 `d_{accepted}`）的隐藏状态恰好是用来预测兜底 token `t_{accepted}`（= 新锚点）的。新锚点 `t_{accepted}` 本身还没有自己的隐藏状态（它要等下一轮 target 前向才产生），所以 context 到 `d_{accepted}` 为止、即切片 `[: accepted + 1]`。这就是「`hidden` 要切片到 `accepted + 1`」的原因。

把两者的目标并列就很清楚：

| 对象 | 回滚后的正确状态 | 为何 |
|---|---|---|
| `target_cache` 长度 | `prompt.size + n' - 1`（裁掉 `bs - accepted - 1`） | 新锚点尚未喂回 target，缓存停在「新锚点之前」 |
| `hidden` 序列长 | `accepted + 1`（切片） | 新锚点尚无隐藏状态，context 停在「新锚点之前」 |

两者其实是**同一个「停在锚点之前」**的不变量，只是一个作用于 K/V 缓存、一个作用于隐藏状态。

#### 4.3.3 源码精读

先看 [`_trim_recent_cache`](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/model_mlx.py#L243-L259)：

```python
def _trim_recent_cache(cache: List[Any], num_tokens: int) -> None:
    if num_tokens <= 0:
        return
    for c in cache:
        n = min(getattr(c, "offset", num_tokens), num_tokens)
        if n <= 0:
            continue
        if isinstance(c, RotatingKVCache) and c.keys is not None:
            c.keys = c._temporal_order(c.keys)
            c.values = c._temporal_order(c.values)
            c.keys = c.keys[..., :-n, :]
            c.values = c.values[..., :-n, :]
            c.offset -= n
            c._idx = c.keys.shape[2]
        elif hasattr(c, "trim"):
            c.trim(n)
```

要点：

- 对缓存里的**每一层**分别处理；`num_tokens` 是要从尾部删掉的数量。
- `n = min(c.offset, num_tokens)`：裁剪量不超过该层已有的 token 数（`offset`），防越界。
- **`RotatingKVCache` 分支**：滑动窗口层的 K/V 是环形存储的（u3-l1），物理顺序不等于时间顺序，所以先 `c._temporal_order(...)` 还原成时间顺序，再 `[..., :-n, :]` 切掉最后 `n` 个，并把 `offset` 减 `n`、`_idx` 重置为新长度。这是手动裁剪，因为 `RotatingKVCache` 的语义是「保留最近 `max_size` 个」，普通 `trim` 不一定符合预期。
- **普通 `KVCache` 分支**：直接调 `c.trim(n)`，由 `mlx_lm` 内部完成。

再回到 `stream_generate` 里调用它的两处。第一处是 [draft 缓存的防御性对齐](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/model_mlx.py#L504-L505)：

```python
if (trim_n := draft_cache[0].offset - (prompt.size + n - 1)) > 0:
    _trim_recent_cache(draft_cache, trim_n)
```

- `prompt.size + n - 1` 是「所有已确认 token − 当前锚点」的数量，也就是草稿 context **应有**的长度；`draft_cache[0].offset` 是实际写入量。由于每轮写入的 context 长度恰为上一轮的 `accepted + 1`（与 `n` 的增长严格同步），稳态下 `trim_n = 0`。这是一条**防御性**守卫：一旦因任何边界情况导致 context 多写，就把多余部分裁掉，保证草稿 context 前缀与「已确认 token − 锚点」严格对齐。

第二处是 [target 缓存的拒绝回滚](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/model_mlx.py#L561-L567)：

```python
trim = bs - accepted - 1
if trim > 0:
    if _target_can_trim:
        _trim_recent_cache(target_cache, trim)
    elif _capture is not None:
        _capture.rollback(target_cache, accepted, trim)
hidden = hidden[:, :accepted + 1, :]
```

- `trim = bs - accepted - 1`：被拒候选数，从 target 缓存尾部裁掉。
- `_target_can_trim` 为真走 `_trim_recent_cache`（本讲）；为假走 `_capture.rollback`（混合架构，u3-l3）。
- `hidden = hidden[:, :accepted + 1, :]`：把本轮 `bs` 个隐藏状态砍到前 `accepted + 1` 个，作为下一轮草稿的 context（推导见 4.3.2）。

最后看结果构造。[`GenerationResponse`](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/model_mlx.py#L400-L410) 是一个 dataclass：

```python
@dataclass
class GenerationResponse:
    text: str
    tokens: List[int]
    accepted: int
    prompt_tokens: int
    prompt_tps: float
    generation_tokens: int
    generation_tps: float
    peak_memory: float
    finish_reason: Optional[str] = None
```

字段含义：

- `text`：本轮 `yield` 的新文本片段（`detokenizer.last_segment`，不是累积全文）。
- `tokens`：本轮新产出的 token id 列表。
- `accepted`：本轮产出 token 数 = `accepted + 1`（注意：这里命名的 `accepted` 其实是「产出数」而非「公共前缀长度」，调用方拿到的就是这个值）。
- `prompt_tokens` / `generation_tokens`：prompt 长度 / 截至当前已生成的总 token 数 `n`。
- `prompt_tps` / `generation_tps`：prefill 吞吐 / 解码吞吐。`generation_tps = n / (now - tic)`，其中 `tic` 是 prefill 之后重置的（见 4.2.3），所以解码吞吐不含 prefill 开销，与 u2-l1 的 `time_per_output_token` 语义一致。
- `peak_memory`：`mx.get_peak_memory() / 1e9`，单位 GB。
- `finish_reason`：`"stop"`（命中 EOS）或 `"length"`（达到 `max_tokens`），仅在终止那一轮设置。

[`_make_response`](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/model_mlx.py#L413-L426) 是它的工厂：

```python
def _make_response(text, tokens, accepted, prompt_size, prompt_tps, n, tic, finish_reason=None):
    return GenerationResponse(
        text, tokens, accepted, prompt_size, prompt_tps,
        n, n / (time.perf_counter() - tic), mx.get_peak_memory() / 1e9, finish_reason,
    )
```

每次 `yield _make_response(...)` 都用「当前 `n` / 已过解码时间」重算 `generation_tps`，所以它是一个**实时刷新**的吞吐值——这也是 README 示例里 `tps = r.generation_tps` 在循环结束后取最后一条 `r` 的原因。

#### 4.3.4 代码实践

**实践目标**：亲手算一遍 `trim = bs - accepted - 1` 与 `hidden` 切片，验证它们与「target 缓存停在锚点之前」的不变量一致。

**操作步骤**（需 Apple 芯片 + `.[mlx]`；无环境做步骤 1–3 的纸笔推导）：

1. 在 [`stream_generate` 的 L561 之前](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/model_mlx.py#L561-L567) 临时加打印（**示例代码**，验证后还原）：

   ```python
   print(f"[rollback] bs={bs} accepted={accepted} trim={bs - accepted - 1} "
         f"hidden {hidden.shape[1]} -> {accepted + 1}")
   ```

2. 跑一次小生成（`block_size=16, max_tokens=64, temperature=0.0`），记录每轮的 `bs / accepted / trim`。
3. 纸笔验证：取任意一轮，确认 `trim == bs - accepted - 1`，且本轮 `produced == accepted + 1`；再确认多轮累计 `sum(accepted_i + 1) == 最终 n`（除去 EOS / 截断的最后一轮）。
4. 思考题：把 `temperature` 从 `0.0` 调到 `0.8`，重跑并对比每轮 `accepted` 的分布，解释变化。

**需要观察的现象**：

- `trim` 与 `accepted` 此消彼长，`trim + accepted + 1 == bs` 恒成立。
- `hidden` 切片后长度恰为 `accepted + 1`，与该轮产出 token 数相等。
- 温度升高 → `accepted` 整体变小、`trim` 变大 → 加速比下降。

**预期结果**：每一轮都满足 `trim = bs - accepted - 1`、`hidden.shape[1] == accepted + 1`（切片后），印证 4.3.2 的推导。若本地无法运行 MLX，**待本地验证**，可仅凭源码与 u2-l4 的算法完成纸笔推导。

#### 4.3.5 小练习与答案

**练习 1**：若忘了在拒绝后调用 `_trim_recent_cache(target_cache, trim)`，下一轮会发生什么？

> **参考答案**：被拒候选的脏 K/V 残留在 target 缓存尾部，target 在下一轮验证时会基于「错误的历史序列」计算注意力，预测的 `target_tokens` 偏离正解，导致输出内容错乱（语义崩坏）。裁剪不是优化、是正确性前提。

**练习 2**：为什么 `_trim_recent_cache` 对 `RotatingKVCache` 要先调 `_temporal_order` 再切片，而普通 `KVCache` 直接 `trim` 就行？

> **参考答案**：`RotatingKVCache` 是环形缓冲，物理存储顺序与时间顺序不一致（u3-l1），直接按物理下标切片会裁错位置；先 `_temporal_order` 还原成时间顺序，才能正确「删掉最近的 `n` 个」。普通 `KVCache` 按时间顺序追加存储，`mlx_lm` 的 `trim` 直接处理即可。

**练习 3**：`GenerationResponse.accepted` 字段被赋的值是 `accepted + 1`（产出数）而非公共前缀长度 `accepted`。这样命名会不会误导调用方？代码为什么这么写？

> **参考答案**：字段名确实容易让人以为是「被接受的草稿数」，但实际是「本轮产出 token 数 = 被接受草稿数 + 1 个兜底」。这样设计是为了让调用方直接拿到「本轮净产出」，便于算吞吐与加速比（平均 `accepted` 字段 ≈ 每轮产出 ≈ 加速比）。读代码时需留意 `yield _make_response(..., accepted + 1, ...)` 这一行才能避免误解。

---

## 5. 综合实践

把本讲三个最小模块串起来，做一次「**带仪表盘的 MLX 流式生成**」。

**任务**：基于 README 的 MLX 示例，把 `stream_generate` 包一层，边流式打印文本、边收集每一轮的运行时数据，最后输出一份小型报告。

**操作步骤**（需 Apple 芯片 + `.[mlx]`；无环境者把步骤 4 的报告改为「阅读源码后写出预期」）：

1. 复制 README「MLX (Apple Silicon)」示例代码，把 `for r in stream_generate(...)` 改造为：

   ```python
   rounds = []
   for r in stream_generate(model, draft, tokenizer, prompt,
                            block_size=16, max_tokens=256, temperature=0.6):
       print(r.text, end="", flush=True)
       rounds.append(r)
   ```

2. （可选）在 [`model_mlx.py` 的回滚段](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/model_mlx.py#L561-L567) 临时加上 4.3.4 的打印，对照 `r.accepted`。
3. 生成结束后，用 `rounds` 计算并打印：
   - 总轮数 `len(rounds)`、总产出 token 数 `rounds[-1].generation_tokens`。
   - 每轮产出 `r.accepted` 的平均值与直方图（即「每轮接受长度」分布）。
   - 最终吞吐 `rounds[-1].generation_tps`、峰值显存 `rounds[-1].peak_memory`、`finish_reason`。
4. 写一段话解释：为什么「平均每轮产出 `E[accepted+1]`」直接决定了相对纯 target 自回归的加速比；并把 `temperature` 在 `{0.0, 0.6, 1.0}` 三档下各跑一次，记录平均每轮产出与吞吐的变化。
5. **还原**你对 `model_mlx.py` 的所有临时打印（本讲禁止修改源码，调试改动勿提交）。

**预期结果**：你会清楚地看到「加速比 ≈ 平均每轮产出 token 数」，以及温度升高导致平均产出下降、吞吐下降的趋势——这把 u2-l4 的算法结论在 MLX 实现上落到了实测。若本地无法运行，**待本地验证**。

## 6. 本讲小结

- **钩子捕获是 MLX 版的 `extract_context_feature`**：`_patch_model` 把 target 的指定层换成 `_LayerHook`，target 每次前向都把多层隐藏状态抄进 `model._hidden_states`，`mx.concatenate(..., axis=-1)` 拼成草稿的 context；`_get_layers` 适配三种 target 封装，patch 幂等。
- **`stream_generate` 是流式生成器**：prefill（target 预填充 + 捕获 prompt 隐藏 + 采首 token）→ decode 循环（块起草 `[锚点, mask…]` → target 验证 `[锚点, 候选…]` → 最长公共前缀 `accepted` → 产出 `accepted+1`）→ 收尾；全程用 `mx.async_eval` 重叠草稿与验证。
- **回滚的核心公式 `trim = bs - accepted - 1`**：等于本轮被拒候选数，需从 target 缓存尾部裁掉，使缓存停在「新锚点之前」（`prompt.size + n' - 1`）；`hidden` 同步切片到 `accepted + 1`，是同一个「停在锚点之前」的不变量作用于隐藏状态。
- **草稿缓存几乎免回滚**：因为 u3-l1 的设计——只有 context 进 `draft_cache`、proposal 不持久化——被拒候选从未进过草稿缓存；那条 `trim_n` 守卫是防御性对齐，稳态下为 0。
- **`_trim_recent_cache` 区分两类缓存**：`RotatingKVCache` 先 `_temporal_order` 还原时间序再切片、普通 `KVCache` 直接 `trim`。
- **`GenerationResponse` 是流式结果包**：`accepted` 字段实为「本轮产出数 = 公共前缀长度 + 1」，`generation_tps` 实时刷新且不含 prefill，`finish_reason` 标识 `stop`/`length`；可裁剪性 `can_trim_prompt_cache` 决定走 `_trim_recent_cache` 还是 `_GDNStateCapture.rollback`。

## 7. 下一步学习建议

本讲把 MLX 后端在「**普通可裁剪缓存**」路径下的生成循环讲透了。但我们在 4.2.3 和 4.3.3 反复看到一个分流：当 `can_trim_prompt_cache(target_cache)` 为 **False**——也就是 target 里含有 **GatedDeltaNet** 这类带「递归状态」的层（Qwen3.5 等混合架构）时，KV 缓存无法用简单 `trim` 回滚，因为被拒 token 不仅写入了 K/V，还更新了层内部的**卷积状态与 delta 状态**。

下一讲 **u3-l3《混合模型的门控增量状态捕获与回滚》** 正好接上，主题是 [`_GDNStateCapture`](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/model_mlx.py#L293-L397)：

- 它如何 monkey-patch `GatedDeltaNet.__call__`，在验证前向里**捕获**卷积输入（`conv_input`）与增量更新输入（`gdn_inputs`）；
- 拒绝后如何用捕获到的输入**重放** `gated_delta_update`，重建 `conv_state` 与 delta state，实现等价于 `trim` 的回滚；
- `stream_generate` 里 `_capture.clear()` / `_capture.rollback()` 两处调用如何与本讲的 `trim` 分支对应。

建议进入 u3-l3 前先确认两件事：一是本讲 4.3.2 的 `trim` 与 `hidden` 切片推导你是否真的算通了；二是你是否理解了「为什么 GDN 层不能直接 `trim`」——答案就藏在「KV 缓存只是 GDN 层状态的一部分，另一部分是卷积与 delta 的递归状态」这一点上。如果你对评测与加速比指标更感兴趣，也可以先跳到 u3-l4 / u3-l5（benchmark 模块），再回头读 u3-l3。
