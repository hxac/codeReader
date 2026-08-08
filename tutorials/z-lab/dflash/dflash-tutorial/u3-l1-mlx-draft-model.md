# MLX 草稿模型与配置

## 1. 本讲目标

前两个单元我们一直在读 `dflash/model.py`——那是一份基于 PyTorch + Transformers 的**参考实现**：草稿模型 `DFlashDraftModel` 继承 `Qwen3PreTrainedModel`，靠 `from_pretrained` 加载权重，注意力里把 context 和 noise 拼成一条 key/value 序列。

但 DFlash 还有一条**完全独立的实现分支**：`dflash/model_mlx.py`。它面向 Apple 芯片（Apple Silicon），用 MLX 框架重写，不依赖 Transformers、也不依赖 PyTorch。它要解决同一类问题（块扩散投机解码），却因为运行环境不同，在**配置、加载、注意力、缓存**这四件事上都采取了不同的工程选择。

本讲就带你走进这条 MLX 分支。学完后你应该能够：

1. 读懂 MLX 版的配置类 `DFlashConfig`（一个 dataclass），并说清它和 Transformers 版 `Qwen3Config` 的字段对应关系。
2. 读懂 `load_draft` 如何**手动**下载权重、解析 JSON、加载模型——以及为什么 MLX 不能像 Transformers 那样「继承一下就有了 `from_pretrained`」。
3. 理解 MLX 版注意力 `DFlashAttention.__call__` 的 **context keys / proposal keys 拼接逻辑**，并把它和 u2-l3 讲过的 `Qwen3DFlashAttention`（context / noise）逐点对照。
4. 说清**滑动窗口层**为什么必须用 `RotatingKVCache` 而不是普通 `KVCache`。
5. 理解 `bind()` 如何把 target 的 `embed_tokens` / `lm_head`「绑」到草稿上（MLX 版的「两个借」），`make_cache()` 如何按层类型分发不同缓存，以及 `__call__` 为什么直接返回 logits（而 Transformers 版返回隐藏状态）。

本讲的主题是「**同一算法的另一种实现**」——我们会反复对照 `model.py` 与 `model_mlx.py`，让你看清哪些是算法本质（两份代码一致），哪些是工程取舍（两份代码不同）。

## 2. 前置知识

本讲假设你已经读过：

- **u2-l2**：`DFlashDraftModel` 的内部结构（layers / norm / fc / hidden_norm）、`build_target_layer_ids` 的等距采样、以及 `fc` 这个「翻译器」的作用。
- **u2-l3**：Transformers 版 `Qwen3DFlashAttention` 的块扩散注意力——query 只来自噪声、key/value 由「上下文在前、噪声在后」拼接、`is_causal=False` 让块内双向可见。

下面几个 MLX 相关的概念，用通俗语言补齐，不默认你熟悉：

- **MLX**：Apple 官方开源的、运行在 Apple 芯片（M 系列统一内存架构）上的数组计算与神经网络框架，定位类似「苹果版 NumPy + PyTorch」。它的算子是**惰性求值**（lazy）的——你写 `a + b` 不会立刻计算，要调用 `mx.eval()` 或在下次用到结果时才真正执行。`mx.fast.scaled_dot_product_attention` 是它内置的高效注意力算子。
- **`dataclass`**：Python 标准库的装饰器，用来声明「主要是字段」的类。写 `@dataclass` 后，Python 会自动帮你生成 `__init__`、`__repr__` 等方法，省去手写一堆 `self.x = x`。MLX 版的配置 `DFlashConfig` 就是一个 dataclass。
- **`KVCache` / `RotatingKVCache`**：MLX 生态（`mlx_lm`）提供的两类 KV 缓存。普通 `KVCache` 会**无限增长**，把每个历史 token 的 key/value 都存下来；`RotatingKVCache` 则有**容量上限** `max_size`，超出后自动丢弃最老的 key/value（像环形缓冲区）。后者专为滑动窗口注意力设计。
- **`snapshot_download`**：`huggingface_hub` 提供的函数，把一个 Hub 仓库的文件下载到本地缓存目录并返回路径。MLX 版加载草稿时，靠它把 `config.json` 和 `*.safetensors` 拉下来。
- **`make_prompt_cache` / `update_and_fetch`**：`mlx_lm` 里给整条模型快速建一组逐层缓存、以及「把新的 key/value 写入缓存并返回（含历史）的完整 key/value」的操作。本讲会在注意力里看到 `cache.update_and_fetch(ctx_keys, ctx_values)`。

> 一句话定位：本讲只覆盖**单次草稿前向**涉及的「配置 → 加载 → 注意力 → 模型外壳」四层；至于流式生成主循环、target 隐藏状态的钩子捕获、以及拒绝 token 后的 KV 缓存回滚，全部留到 **u3-l2**。

## 3. 本讲源码地图

本讲围绕一个文件展开，并频繁对照另一个文件：

| 文件 | 本讲关注的内容 |
|---|---|
| `dflash/model_mlx.py` | `DFlashConfig` 数据类、`load_draft` 手动加载、`DFlashAttention.__call__`（ctx/prop 拼接、sliding_window）、`DFlashDraftModel.bind / make_cache / __call__` |
| `dflash/model.py`（对照） | `Qwen3DFlashAttention.forward`（ctx/noise 拼接）、`DFlashDraftModel.forward`（返回隐藏状态）、`extract_context_feature`、`build_target_layer_ids` |

> 说明：`model_mlx.py` 里还有 `stream_generate`、`_LayerHook`、`_trim_recent_cache`、`_GDNStateCapture` 等内容，它们属于**生成循环与回滚**，是 u3-l2 与 u3-l3 的主题，本讲不展开，只在需要时一笔带过。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

1. **4.1** `DFlashConfig` 与 `load_draft`——用 dataclass 重建配置，手动下载并加载权重。
2. **4.2** `DFlashAttention.__call__`——context / proposal 拼接，以及滑动窗口层。
3. **4.3** `DFlashDraftModel`——`bind` 借用、`make_cache` 按层建缓存、`__call__` 返回 logits。

---

### 4.1 DFlashConfig 与 load_draft：重建配置与手动加载

#### 4.1.1 概念说明

在 Transformers 版里，草稿模型的「配置」是一等公民：你继承 `Qwen3Config`，Transformers 自动帮你读 `config.json`、做字段校验、`from_pretrained` 还顺便把权重也加载了。**这一切 MLX 都没有。** MLX 是一个纯数值框架，不带任何「读 JSON 配置 → 实例化模型 → 下载权重」的基础设施。

于是 `model_mlx.py` 必须自己把这套链路重新搭起来，分两步：

- **`DFlashConfig`**：一个手写的 dataclass，把草稿模型需要的所有超参数显式列成字段，充当「MLX 版的 Qwen3Config」。
- **`load_draft`**：一个手写的函数，负责下载文件、读 JSON、构造 `DFlashConfig`、加载权重，相当于「MLX 版的 `from_pretrained`」。

这两个东西在 Transformers 版里几乎是「白送的」，在 MLX 版里必须一行行写。理解了这一点，你就能看懂为什么 `model_mlx.py` 顶部要 import `dataclasses`、`json`、`pathlib`、`huggingface_hub`。

#### 4.1.2 核心流程：load_draft 做了什么

`load_draft(draft_id)` 的执行流程可以用下面这段伪代码概括：

```text
load_draft(draft_id):
    1. snapshot_download(draft_id)            # 下载 *.json + *.safetensors 到本地
    2. 读 config.json → cfg (dict)
    3. 解析 layer_types（可选，缺省全 full_attention）并校验
    4. 用 cfg 的字段构造 DFlashConfig(...)
    5. 遍历所有 *.safetensors，合并成一张 {参数名: 张量} 的权重表
    6. DFlashDraftModel(config) 建空模型
    7. model.load_weights(...)  把权重灌进去
    8. return model
```

注意第 3 步的**校验**和第 4 步的**字段映射**，是 `load_draft` 比 Transformers 版 `from_pretrained` 多干、且必须显式干的活。

#### 4.1.3 源码精读

先看配置类本身。`DFlashConfig` 是一个 `@dataclass`，字段一目了然：

[dflash/model_mlx.py:29-48](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/model_mlx.py#L29-L48) 用 dataclass 声明草稿模型的全部超参数，`block_size`、`target_layer_ids`、`num_target_layers` 是 DFlash 专属字段：

```python
@dataclass
class DFlashConfig:
    hidden_size: int
    num_hidden_layers: int
    num_attention_heads: int
    num_key_value_heads: int
    head_dim: int
    intermediate_size: int
    vocab_size: int
    rms_norm_eps: float
    rope_theta: float
    max_position_embeddings: int
    block_size: int                       # DFlash：块大小（b16 即 16）
    target_layer_ids: Tuple[int, ...]     # DFlash：从 target 哪几层取特征
    num_target_layers: int                # DFlash：target 的总层数
    mask_token_id: int = 0
    rope_scaling: Optional[Dict[str, Any]] = None
    layer_types: Tuple[str, ...] = field(default_factory=tuple)
    sliding_window: Optional[int] = None
    final_logit_softcapping: Optional[float] = None
```

把它和 u2-l2 讲过的 Transformers 版配置对照，字段几乎一一对应，但**组织方式不同**。下表是关键映射：

| 概念 | Transformers 版（`model.py`） | MLX 版（`DFlashConfig`） |
|---|---|---|
| 配置载体 | 复用 `Qwen3Config`（一个 Transformers 类） | 手写 dataclass |
| `num_hidden_layers` | `config.num_hidden_layers`（草稿自身层数，**极易混淆**） | 同名字段（仍是草稿自身层数） |
| `num_target_layers` | `config.num_target_layers`（target 层数） | 同名字段 |
| `block_size` | `config.block_size`（config 顶层） | `block_size`（顶层） |
| `target_layer_ids` | `config.dflash_config["target_layer_ids"]`，**可缺省**（缺省时用 `build_target_layer_ids` 算） | `target_layer_ids`，**必填** |
| `mask_token_id` | `config.dflash_config["mask_token_id"]`，缺省 `None` | `mask_token_id`，缺省 `0` |
| `sliding_window` / `layer_types` | Qwen3 自带字段 | 顶层可选字段 |
| `final_logit_softcapping` | **无**（Qwen3 不需要） | 有（Gemma 风格 logit 软截断） |

这里有两个**容易踩坑的差别**，务必记住：

1. **`target_layer_ids` 在 MLX 版是必填的。** Transformers 版允许 `config.json` 里不写，运行时用 `build_target_layer_ids` 现算；MLX 版直接读 `cfg["dflash_config"]["target_layer_ids"]`，没有兜底逻辑——所以 MLX 草稿模型的 `config.json` **必须**显式写出这个字段。
2. **`final_logit_softcapping` 只有 MLX 版有。** 这是 Gemma 系列模型特有的「logit 软截断」技巧（用 `tanh` 把 logit 压进 \([-c, c]\) 区间）。README 提到 MLX 后端测过 Gemma-4，所以这字段是为 Gemma 准备的；纯 Qwen3 草稿用不到，留空即可。

接着看 `load_draft` 如何把这些字段从 JSON 读出来。先看它对 `layer_types` 的解析与校验：

[dflash/model_mlx.py:209-216](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/model_mlx.py#L209-L216) 把 `layer_types` 缺省为「全是 `full_attention`」，并校验长度与取值合法性：

```python
layer_types = tuple(cfg.get("layer_types") or ["full_attention"] * cfg["num_hidden_layers"])
if len(layer_types) != cfg["num_hidden_layers"]:
    raise ValueError("Draft config layer_types length must match num_hidden_layers.")
unknown_layer_types = set(layer_types) - {"full_attention", "sliding_attention"}
if unknown_layer_types:
    raise ValueError(f"Unsupported draft layer_types: {sorted(unknown_layer_types)}.")
if "sliding_attention" in layer_types and cfg.get("sliding_window") is None:
    raise ValueError("Draft config must define sliding_window for sliding_attention layers.")
```

这段是「防御式编程」的典型：`layer_types` 是一个**逐层**描述注意力类型的元组（比如某层是全注意力 `full_attention`，某层是滑动窗口 `sliding_attention`），它的长度必须和草稿层数对齐，取值只能是这两种之一；只要声明了滑动窗口层，就**必须**同时给出 `sliding_window`。这些校验在 Transformers 版里由 config 类自动做，这里必须手写。

再看字段如何映射成 `DFlashConfig`：

[dflash/model_mlx.py:217-236](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/model_mlx.py#L217-L236) 把 JSON 顶层字段和 `dflash_config` 子字典分别取出，构造 `DFlashConfig`（注意 `target_layer_ids`、`mask_token_id` 来自 `dflash_config`，其余来自顶层）：

```python
config = DFlashConfig(
    hidden_size=cfg["hidden_size"],
    num_hidden_layers=cfg["num_hidden_layers"],
    ...
    block_size=cfg["block_size"],
    target_layer_ids=tuple(cfg["dflash_config"]["target_layer_ids"]),  # 必填，直接取
    num_target_layers=cfg["num_target_layers"],
    mask_token_id=cfg["dflash_config"]["mask_token_id"],
    rope_scaling=cfg.get("rope_scaling"),
    layer_types=layer_types,
    sliding_window=cfg.get("sliding_window"),
    final_logit_softcapping=cfg.get("final_logit_softcapping"),
)
```

注意这个 **JSON 结构约定**：`block_size`、`num_target_layers` 在 `config.json` 顶层；而 `target_layer_ids`、`mask_token_id` 藏在 `dflash_config` 这个子字典里——这和 Transformers 版把两者都放进 `dflash_config` 的写法略有不同，是 MLX 版自己的约定。

最后是「手动加载权重」的部分：

[dflash/model_mlx.py:237-240](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/model_mlx.py#L237-L240) 遍历所有 `*.safetensors` 合并成权重表，建空模型后 `load_weights` 灌入：

```python
weights = {k: v for f in path.glob("*.safetensors") for k, v in mx.load(str(f)).items()}
model = DFlashDraftModel(config)
model.load_weights(list(weights.items()))
return model
```

这就是 MLX 版的「`from_pretrained`」：手动 glob 所有 safetensors 文件、用 `mx.load` 逐个读成字典、合并、再交给模型的 `load_weights`（MLX 的 `nn.Module` 自带的方法）按名字对位灌入。整个过程没有框架魔法，每一步都看得见。

#### 4.1.4 代码实践

> 这是一个**源码阅读型实践**（MLX 需要 Apple 芯片，本机未必能跑），目标是在不运行的情况下把配置链路读通。

1. **实践目标**：在不运行 MLX 的前提下，验证你对 `DFlashConfig` 字段映射的理解。
2. **操作步骤**：
   - 打开 Hugging Face Hub 上任意一个 DFlash 草稿模型的 `config.json`（例如 `z-lab/Qwen3-8B-DFlash-b16` 或 README 里用到的 `z-lab/Qwen3.5-4B-DFlash`）。
   - 找到 `config.json` 里的 `block_size`、`num_target_layers`、`num_hidden_layers`，以及 `dflash_config` 子字典里的 `target_layer_ids`、`mask_token_id`。
   - 回到本讲的 4.1.3，逐字段核对这些值会落到 `DFlashConfig(...)` 的哪个参数上。
   - 如果该模型是 Qwen3.5（混合架构），看它的 `config.json` 里有没有 `layer_types` 和 `sliding_window`；如果是纯 Qwen3，这两个字段大概率缺失——结合 4.1.3 的缺省逻辑，推断此时 `layer_types` 会被填成什么。
3. **需要观察的现象**：`target_layer_ids` 的长度应该等于草稿的 `num_hidden_layers`（因为每层草稿都要从 target 取一组特征）；`num_target_layers` 应该明显大于 `num_hidden_layers`（草稿比 target 浅得多）。
4. **预期结果**：你能画出一张「`config.json` 字段 → `DFlashConfig` 参数」的对照表，并说清哪些字段是必填、哪些可缺省。
5. 如果无法访问 Hub 或找不到对应 `config.json`，明确标注「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 MLX 版 `load_draft` 要自己写 `layer_types` 的长度校验，而 Transformers 版的 `from_pretrained` 不用？

> **参考答案**：Transformers 版的配置是 `Qwen3Config`，它内置了字段类型与长度的校验逻辑；而 MLX 版的 `DFlashConfig` 只是普通 dataclass，`load_draft` 从裸 JSON（`dict`）手动取值，没有任何框架帮忙校验，所以必须自己检查 `layer_types` 长度等于 `num_hidden_layers`、取值合法、滑动窗口层配了 `sliding_window`，否则会在后续前向时才以难以定位的方式出错。

**练习 2**：如果一份 MLX 草稿模型的 `config.json` 里**没有**写 `target_layer_ids`，调用 `load_draft` 会怎样？和 Transformers 版相比行为有何不同？

> **参考答案**：MLX 版会直接抛 `KeyError`（`cfg["dflash_config"]["target_layer_ids"]` 是直接下标访问，无兜底）；而 Transformers 版会在 `DFlashDraftModel.__init__` 里用 `config.dflash_config.get("target_layer_ids", build_target_layer_ids(...))` 现算一个缺省值。所以同一份「残缺」的 `config.json`，Transformers 后端能跑、MLX 后端会报错——这是两条实现的容错策略差异。

---

### 4.2 DFlashAttention.__call__：context/prop 拼接与滑动窗口

#### 4.2.1 概念说明

这是本讲最核心的一节。u2-l3 我们拆过 Transformers 版的 `Qwen3DFlashAttention`：它有两路输入——来自 target 的干净上下文（`target_hidden`）和本块待去噪的噪声（`hidden_states`），query 只来自噪声，key/value 由「上下文在前、噪声在后」拼接。

MLX 版的 `DFlashAttention` 干的是**同一件事**，但命名和实现细节不同。它把两路输入叫做：

- **context（ctx）**：来自 target 的干净上下文，对应 Transformers 版的 `target_hidden`。
- **proposal（prop）**：本块待去噪的 token，对应 Transformers 版的 `hidden_states` / noise。

> 命名上的小提示：Transformers 版叫 `noise`（噪声，强调「待去噪」），MLX 版叫 `prop`（proposal，提案，强调「这是草稿提出来给 target 验证的候选」）。**两者是同一个东西**，只是视角不同——一个是「扩散模型的去噪视角」，一个是「投机解码的起草视角」。

除了命名，MLX 版还多了一个 Transformers 版没有显式处理的特性：**逐层的滑动窗口**。某些层（`sliding_attention`）只让每个 query 看最近 `sliding_window` 个 key，而不是看全部历史。这对超长序列是省显存的关键。

#### 4.2.2 核心流程：一次草稿注意力的内部

把 `DFlashAttention.__call__(x, x_ctx, rope, cache)` 的执行过程画成流程：

```text
输入：x（proposal 块，长度 L）、x_ctx（context，长度 S）、rope、cache
  │
  ├─ 若是 sliding 层 且 S 过长：截掉 x_ctx 最前面若干 token，并 cache.offset += skip
  │
  ├─ 投影：q = q_proj(x)            ← query 只来自 proposal
  │        ctx_keys/ctx_values ← k_proj/v_proj(x_ctx)   ← 上下文支路
  │        prop_keys/prop_values ← k_proj/v_proj(x)     ← 提案支路
  │
  ├─ reshape + q_norm/k_norm（ctx 与 prop 分别归一化）
  │
  ├─ RoPE：q     用 offset = cache.offset + S
  │        ctx_k 用 offset = cache.offset
  │        prop_k用 offset = cache.offset + S
  │
  ├─ 缓存：keys, values = cache.update_and_fetch(ctx_keys, ctx_values)  ← 只缓存 context！
  │
  ├─ 拼接：keys   = concat([keys,   prop_keys],   序列维)   ← proposal 接在 context 之后
  │        values = concat([values, prop_values], 序列维)
  │
  ├─ mask：全注意力层 → None；滑动层 → "causal" 或 windowed mask
  │
  └─ sdpa(queries, keys, values, mask) → o_proj → 输出
```

这里有**一个贯穿全讲的洞察**，请重点记住：

> **在 MLX 版注意力里，只有 context（ctx）会被写进 KV 缓存；proposal（prop）是在「取回缓存」之后才拼接上去的，从不持久化。**

这一点和 Transformers 版有本质区别（详见 4.2.4 的对照）。它的后果是：草稿每轮起草产生的那些「提案 token」的 key/value 是**一次性的**，用完即弃——这恰好为「target 拒绝某些草稿后丢弃它们」做好了铺垫（回滚机制见 u3-l2）。

至于滑动窗口，关键在于：滑动层的 context 只保留最近 `sliding_window - 1` 个 token，配合 `RotatingKVCache`（见 4.3）把缓存容量也限死，从而让注意力的 key 序列长度永远是 \(O(\text{sliding\_window})\)，与总序列长度无关。

#### 4.2.3 源码精读

先看构造函数里滑动窗口相关的设定：

[dflash/model_mlx.py:73-74](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/model_mlx.py#L73-L74) 按 `layer_types` 判断本层是否滑动窗口层，若是则记录 `sliding_window`，否则置 `None`：

```python
self.is_sliding = config.layer_types[layer_idx] == "sliding_attention"
self.sliding_window = config.sliding_window if self.is_sliding else None
```

进入 `__call__`。第一步是滑动层对过长 context 的截断：

[dflash/model_mlx.py:85-91](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/model_mlx.py#L85-L91) 滑动窗口层若 context 长度超过 `sliding_window-1`，则只保留最近的，并把 cache 偏移前移：

```python
if self.is_sliding:
    keep_ctx = self.sliding_window - 1
    if S > keep_ctx:
        skip = S - keep_ctx
        x_ctx = x_ctx[:, skip:]
        S = x_ctx.shape[1]
        cache.offset += skip
```

为什么要 `cache.offset += skip`？因为 RoPE 的位置编码依赖 `cache.offset`（已经累积了多少个 token 的位置）。我们丢掉了最前面 `skip` 个 context token，对应的「逻辑位置」也要前移，否则后续 RoPE 的位置编号会对不上。

接着是三路投影 + 归一化：

[dflash/model_mlx.py:92-101](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/model_mlx.py#L92-L101) query 来自 proposal，ctx 与 prop 分别走 k/v 投影，再各自 reshape + k_norm：

```python
queries = self.q_proj(x)
ctx_keys = self.k_proj(x_ctx);   ctx_values = self.v_proj(x_ctx)
prop_keys = self.k_proj(x);      prop_values = self.v_proj(x)
queries     = self.q_norm(queries.reshape(...)).transpose(0, 2, 1, 3)
ctx_keys    = self.k_norm(ctx_keys.reshape(...)).transpose(0, 2, 1, 3)
ctx_values  = ctx_values.reshape(...).transpose(0, 2, 1, 3)
prop_keys   = self.k_norm(prop_keys.reshape(...)).transpose(0, 2, 1, 3)
prop_values = prop_values.reshape(...).transpose(0, 2, 1, 3)
```

注意：**ctx 和 prop 各自独立做 `k_norm`，是在拼接之前。** 后面 4.2.4 会指出这和 Transformers 版（拼接之后再统一 `k_norm`）在数学上等价、但写法不同。

然后是 RoPE，三组用不同 offset：

[dflash/model_mlx.py:102-104](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/model_mlx.py#L102-L104) query 与 proposal 都排在 context 之后（offset+S），context 用基础 offset：

```python
queries  = rope(queries,  offset=cache.offset + S)
ctx_keys = rope(ctx_keys, offset=cache.offset)
prop_keys= rope(prop_keys,offset=cache.offset + S)
```

这段体现了块扩散在位置轴上的布局：context 占据位置 \([0, S)\)，proposal 紧随其后占据 \([S, S+L)\)。query 也从 \(S\) 开始——query 来自 proposal，所以它的位置和 proposal 对齐。

接下来是**全讲最关键的两行**——缓存与拼接：

[dflash/model_mlx.py:105-108](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/model_mlx.py#L105-L108) 只把 context 写入缓存并取回（含历史），再把 proposal 拼在后面；proposal 不进缓存：

```python
keys, values = cache.update_and_fetch(ctx_keys, ctx_values)
ctx_len = keys.shape[2]
keys = mx.concatenate([keys, prop_keys], axis=2)
values = mx.concatenate([values, prop_values], axis=2)
```

`update_and_fetch(ctx_keys, ctx_values)` 做两件事：把本轮 context 的 key/value **追加**进缓存（并前移 offset），然后**返回包含历史的完整 key/value**。`ctx_len` 是取回后的 context 总长（可能大于本轮 S，因为含历史）。随后 proposal 的 key/value 被拼在 context 之后——但它们**没有**写进缓存。

最后是 mask 与注意力计算：

[dflash/model_mlx.py:109-116](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/model_mlx.py#L109-L116) 全注意力层 mask 为 None（块内双向可见）；滑动层用 `"causal"` 或 windowed mask；最终调 MLX 内置 sdpa：

```python
mask = None
if self.is_sliding:
    mask = (
        "causal" if ctx_len + L <= self.sliding_window
        else create_causal_mask(L, offset=ctx_len, window_size=self.sliding_window)
    )
output = mx.fast.scaled_dot_product_attention(queries, keys, values, scale=self.scale, mask=mask)
return self.o_proj(output.transpose(0, 2, 1, 3).reshape(B, L, -1))
```

全注意力层（`full_attention`）的 `mask = None`，意味着 `queries`（proposal 块内的每个 token）可以**无掩码地**看到 `keys` 里的全部 context 和全部 proposal——这正是「块内双向可见、一次前向并行起草整块」的实现方式，和 u2-l3 讲的 `is_causal=False` 是同一个意思，只是 MLX 用「mask=None」来表达。

#### 4.2.4 代码实践（对照 u2-l3）

> 这是本讲的主实践任务：把 MLX 版 `DFlashAttention` 和 u2-l3 的 `Qwen3DFlashAttention` 放在一起对照，整理出 context/proposal 拼接逻辑的异同。

1. **实践目标**：用一张表说清两份 attention 在「context 与 proposal（noise）拼接」这件事上的相同点与不同点。
2. **操作步骤**：
   - 同时打开 [dflash/model_mlx.py:82-116](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/model_mlx.py#L82-L116)（MLX 版 `__call__`）和 [dflash/model.py:211-255](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/model.py#L211-L255)（Transformers 版 `forward`）。
   - 在你的笔记里照下面这张「参考对照表」逐行核对，并用自己的话补全每一条的理由。
3. **需要观察的现象**：
   - 两份代码的 query 都**只**来自本块 token；context 都排在 noise/prop 之前。
   - 「进缓存的差异」是最显眼的不同：MLX 只缓存 context，Transformers 把 context+noise 一起缓存。
4. **预期结果**：你能产出下面这张表（这里给出参考答案，请对照源码确认）。

**参考对照表：context 与 proposal/noise 的拼接**

| 维度 | MLX 版 `DFlashAttention` | Transformers 版 `Qwen3DFlashAttention` | 是否本质差异 |
|---|---|---|---|
| 本块输入命名 | `x` / proposal | `hidden_states` / noise | 仅命名不同 |
| context 来源 | `x_ctx`（已在 model 层过 fc+hidden_norm） | `target_hidden`（同样已过 fc+hidden_norm） | 相同 |
| query 来源 | 只来自 proposal `x` | 只来自 noise `hidden_states` | 相同 |
| k/v 拼接顺序 | context 在前、proposal 在后 | context 在前、noise 在后 | 相同 |
| 总长度 | \(S + L\)（context + 块大小） | \(\text{ctx\_len} + \text{q\_len}\) | 相同 |
| **谁进缓存** | **只有 context** 进 `update_and_fetch`，proposal 后接、不持久化 | **context + noise 一起**进 `past_key_values.update` | **是（核心差异）** |
| `k_norm` 时机 | ctx、prop **分别** k_norm 后拼接 | 拼接后对整体 k_norm | 否（RMSNorm 逐 token，数学等价） |
| RoPE 写法 | q / ctx_k / prop_k **分别**指定 offset | 统一 cos/sin 覆盖全位置、q 取末尾 | 否（等价的不同写法） |
| 块内可见性 | 全注意力层 mask=None | `is_causal=False` | 相同 |
| 滑动窗口 | 显式截断 ctx 到 `window-1` + windowed mask | 设 `is_causal=False` 并把 `sliding_window` 透传给 attn 函数 | 实现方式不同 |
| 注意力算子 | `mx.fast.scaled_dot_product_attention` | 经 `ALL_ATTENTION_FUNCTIONS` 派发 flash/sdpa/eager | 工程差异 |

5. 如果想动手验证「k_norm 先后不影响结果」：理论上对单 token，RMSNorm \( \text{RMSNorm}(x) = \frac{x}{\text{RMS}(x)}\cdot \gamma \) 只依赖该 token 自身的特征维，因此沿序列维拼接前/后做归一化结果一致——\(\text{RMSNorm}(\text{concat}(a,b)) = \text{concat}(\text{RMSNorm}(a),\text{RMSNorm}(b))\)。你不必运行，记住这个性质即可。

#### 4.2.5 小练习与答案

**练习 1**：在 MLX 版注意力里，如果把 `prop_keys` 也写进缓存（即改成 `cache.update_and_fetch(concat([ctx_keys, prop_keys]), ...)`），会对后续的投机解码回滚带来什么麻烦？

> **参考答案**：proposal 是「草稿提出来待 target 验证」的候选，target 很可能只接受其中一部分、拒绝其余。如果 proposal 被写进缓存，那么被拒绝的 token 的脏 key/value 就会污染缓存，下一轮起草会读到错误的历史——必须显式回滚。而当前实现让 proposal **不进缓存**，拒绝后它们自然消失（只存在于本轮临时拼接的 keys/values 里），下一轮 context 仍然干净，回滚逻辑因此大大简化。这正是把回滚设计建立在「proposal 不持久化」之上的原因。

**练习 2**：滑动窗口层在 `S > sliding_window - 1` 时要 `cache.offset += skip`。如果不加这一句，会出现什么问题？

> **参考答案**：`cache.offset` 是 RoPE 推算位置编号的基准。截掉了最前面 `skip` 个 context token 后，剩余 token 的「逻辑位置」整体前移了 `skip`；若不同步前移 `cache.offset`，RoPE 会给它们分配错误的位置编号（偏大），导致注意力的位置编码与实际序列错位，输出失真。所以「截断 context」和「前移 offset」必须成对出现。

**练习 3**：为什么全注意力层的 `mask = None` 就能实现「块内 token 彼此双向可见」？这和 `create_causal_mask` 的区别是什么？

> **参考答案**：`mask = None` 表示对 `queries` 与 `keys` 的任意一对位置都不施加任何遮蔽，于是 proposal 块内的任意两个 token 可以互相 attend（双向），这正是块扩散「并行起草整块」所需的。而 `create_causal_mask` 会强制上三角遮蔽（未来的 token 看不到当前），那是**自回归**解码所需的单向可见性——块扩散要的是双向，所以全注意力层绝不能用 causal mask。

---

### 4.3 DFlashDraftModel：bind 借用、make_cache 建缓存、__call__ 返回 logits

#### 4.3.1 概念说明

u2-l2 我们讲过草稿模型是「残缺」的：没有自己的 `embed_tokens` 和 `lm_head`，生成时向 target 借。在 Transformers 版里，「借」是发生在 `dflash_generate` 内联代码里的——直接写 `target.model.embed_tokens(...)` 和 `target.lm_head(...)`。

MLX 版换了一种更「面向对象」的写法：提供一个 `bind(target_model)` 方法，**一次性**把 target 的 `embed_tokens` 和 `lm_head` 绑定到草稿对象的属性上；之后草稿的 `__call__` 就能像完整模型一样，自己 embed、自己出 logits。这更符合 MLX 生态「模型自洽」的风格。

此外，MLX 版还多了一个 `make_cache()` 方法，负责按**每一层的类型**（全注意力 vs 滑动窗口）建不同种类的缓存。这一步在 Transformers 版里由 `DynamicCache` 统一包办，MLX 版则要显式区分。

最后，`__call__` 的返回值也和 Transformers 版不同：**MLX 版草稿直接返回 logits**（已经过 `lm_head`），而 Transformers 版草稿的 `forward` 返回的是**隐藏状态**（norm 的输出，`lm_head` 在外部调用方 `dflash_generate` 里才应用）。

#### 4.3.2 核心流程：bind → make_cache → __call__ 三步

把草稿模型「从加载好到能产出 logits」的链路理一下：

```text
1. load_draft(draft_id)            → 得到一个「没绑 embed/lm_head」的 DFlashDraftModel
2. draft.bind(target_model)        → 把 target 的 embed_tokens / lm_head 绑到草稿上
3. draft_cache = draft.make_cache()→ 按层类型建 KVCache / RotatingKVCache 列表
4. draft(block, hidden, draft_cache, logits_start=1)
   ├─ h     = embed_tokens(block) * embed_scale        ← 用「借来的」嵌入层
   ├─ h_ctx = hidden_norm(fc(hidden))                  ← 翻译 target 特征进 draft 空间
   ├─ for layer, c in zip(layers, cache): h = layer(h, h_ctx, rope, c)
   ├─ h = h[:, logits_start:]                          ← 丢掉锚点 token，只留待去噪位置
   ├─ logits = lm_head(norm(h))                        ← 用「借来的」输出层出 logits
   └─（可选）logit 软截断                               ← Gemma 才有
   → 返回 logits
```

这条链路里，「两个借」（`embed_tokens`、`lm_head`）在 `bind` 之后就在草稿内部使用了，不像 Transformers 版散落在外部生成函数里。

#### 4.3.3 源码精读

先看构造函数——注意它**显式**把 `embed_tokens` 和 `lm_head` 初始化为 `None`，标记自己是「残缺」的：

[dflash/model_mlx.py:138-151](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/model_mlx.py#L138-L151) 草稿有 fc/hidden_norm/layers/norm/rope，但 `embed_tokens`、`lm_head` 留空待 `bind`：

```python
concat_dim = len(config.target_layer_ids) * config.hidden_size
self.fc = nn.Linear(concat_dim, config.hidden_size, bias=False)
self.hidden_norm = nn.RMSNorm(config.hidden_size, eps=config.rms_norm_eps)
self.layers = [DFlashDecoderLayer(config, i) for i in range(config.num_hidden_layers)]
self.norm = nn.RMSNorm(config.hidden_size, eps=config.rms_norm_eps)
self.rope = _build_rope(...)
self.embed_tokens = None
self.lm_head = None
self.embed_scale = 1.0
```

`fc` 的输入维 `concat_dim = len(target_layer_ids) * hidden_size` 和 u2-l3 讲的 `extract_context_feature` 拼接维度一致——把多层 target 特征沿特征维拼起来，再投影回 `hidden_size`。

接着是本节的第一个重点——**`bind`**。它要在 target 模型里找到 `embed_tokens`，但不同模型的封装层级不同（有的 target 直接有 `embed_tokens`，有的藏在 `model.embed_tokens`，多模态模型甚至藏在 `language_model.model.embed_tokens`），所以要做一层层的探测：

[dflash/model_mlx.py:153-168](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/model_mlx.py#L153-L168) 在 target 的多种封装层级里找 `embed_tokens`，绑定它和 `lm_head`（含权重绑定 `as_linear` 的兜底）：

```python
def bind(self, target_model):
    if hasattr(target_model, "embed_tokens"):
        inner = target_model
    elif hasattr(target_model, "model") and hasattr(target_model.model, "embed_tokens"):
        inner = target_model.model
    elif (hasattr(target_model, "language_model") and ...):
        inner = target_model.language_model.model
    else:
        raise AttributeError(f"Cannot find embed_tokens in {type(target_model).__name__}")
    self.embed_tokens = inner.embed_tokens
    self.embed_scale = getattr(self.embed_tokens, "embed_scale", getattr(inner, "embed_scale", 1.0))
    lm = getattr(target_model, "language_model", target_model)
    self.lm_head = getattr(target_model, "lm_head", None) or getattr(lm, "lm_head", None) or self.embed_tokens.as_linear
    return self
```

两个细节值得注意：

- **`embed_scale`**：某些 MLX 量化模型在嵌入时会乘一个缩放因子（`embed_scale`），`bind` 用 `getattr(..., "embed_scale", 1.0)` 兜底，确保草稿用的嵌入缩放和 target 完全一致。
- **`self.embed_tokens.as_linear`**：这是「权重绑定」（weight tying）的兜底。有些模型不单独存 `lm_head`，而是让嵌入矩阵兼任输出层（`embed_tokens.as_linear` 把嵌入层当线性层用）。`bind` 依次尝试 `target_model.lm_head` → `language_model.lm_head` → `embed_tokens.as_linear`，覆盖三种情况。

第二个重点是 **`make_cache`**——按层类型分发不同缓存：

[dflash/model_mlx.py:170-179](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/model_mlx.py#L170-L179) 滑动窗口层用 `RotatingKVCache`（容量 = `sliding_window-1`），全注意力层用普通 `KVCache`：

```python
def make_cache(self):
    caches = []
    for layer_type in self.config.layer_types:
        if layer_type == "sliding_attention":
            if self.config.sliding_window is None:
                raise ValueError("Draft config must define sliding_window for sliding_attention layers.")
            caches.append(RotatingKVCache(max_size=self.config.sliding_window - 1, keep=0))
        else:
            caches.append(KVCache())
    return caches
```

**为什么滑动窗口层要用 `RotatingKVCache` 而不是普通 `KVCache`？** 这是本讲的第二个核心问题，单独展开：

- 普通 `KVCache` 会把**所有**历史 token 的 key/value 都存下来，缓存大小随序列长度线性增长。对于全注意力层这是必要的——因为每个 query 都可能 attend 到任意历史位置。
- 但滑动窗口层的定义就是「每个 query 只看最近 `sliding_window` 个 key」。既然只看局部，存历史全部 key/value 就是**纯粹的浪费**——既爆显存（统一内存），又拖慢 `update_and_fetch`。
- `RotatingKVCache(max_size=sliding_window - 1, keep=0)` 是一个**有上限的环形缓存**：超过 `max_size` 后自动丢弃最老的 key/value。这把缓存的容量死死钉在 \(O(\text{sliding\_window})\)，与总序列长度无关，完美匹配滑动窗口的语义。参数 `keep=0` 表示不做「保留最前面若干 token」的特殊处理。

> 把 4.2 和 4.3 串起来看：滑动窗口层的「省显存」是**双保险**实现的——注意力里截断 context 到 `window-1`（控制输入），`make_cache` 里用 `RotatingKVCache` 容量 = `window-1`（控制存储）。两边都是 `sliding_window - 1`，相互呼应。

最后是 **`__call__`**——草稿的前向，直接返回 logits：

[dflash/model_mlx.py:181-198](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/model_mlx.py#L181-L198) 内部完成「嵌入 → 翻译 context → 逐层 → 切片 → lm_head → 软截断」，返回 logits：

```python
def __call__(self, inputs, target_hidden, cache, logits_start: int = 0):
    h = self.embed_tokens(inputs) * self.embed_scale
    h_ctx = self.hidden_norm(self.fc(target_hidden))
    for layer, c in zip(self.layers, cache):
        h = layer(h, h_ctx, self.rope, c)
    if logits_start:
        h = h[:, logits_start:]
    logits = self.lm_head(self.norm(h))
    if self.config.final_logit_softcapping is not None:
        cap = self.config.final_logit_softcapping
        logits = mx.tanh(logits / cap) * cap
    return logits
```

几个要点：

- **`h = self.embed_tokens(inputs)`**：这里 `inputs` 是**原始 token id**，嵌入在草稿内部完成（用 `bind` 借来的 `embed_tokens`）。而 Transformers 版是在 `dflash_generate` 外部嵌入好再把 `noise_embedding` 传进来——MLX 版更内聚。
- **`logits_start`**：把块的第一个 token 的隐藏状态切掉。块的第一个 token 是「锚点」（上一轮的真实 token，用于给后续 mask token 提供上下文），它本身**不需要**出 logits；要出 logits 的是后面那 `block_size - 1` 个待去噪位置。所以 `stream_generate` 调用时传 `logits_start=1`，把锚点丢掉。
- **直接返回 logits**：和 Transformers 版 `DFlashDraftModel.forward` 返回 `norm` 后的隐藏状态（`lm_head` 在 `dflash_generate` 里外部应用）形成对照。MLX 版把 `lm_head` 也包进了草稿，返回值可以直接喂给采样器。
- **`final_logit_softcapping`**：Gemma 风格的 logit 软截断 \( \text{tanh}(\text{logit}/c) \cdot c \)，把 logit 压进 \( (-c, c) \)。只有 config 里设了才生效，纯 Qwen3 草稿走不到这个分支。

#### 4.3.4 代码实践

> 这仍是**源码阅读型实践**（运行需 Apple 芯片 + 已下载的草稿/target 权重）。目标是把 `bind / make_cache / __call__` 三者关系读通。

1. **实践目标**：在笔记里画出 `load_draft → bind → make_cache → __call__` 的完整调用链，并标注「两个借」发生在哪里。
2. **操作步骤**：
   - 对照 [dflash/model_mlx.py:153-168](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/model_mlx.py#L153-L168)（`bind`）和 [dflash/model_mlx.py:188-194](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/model_mlx.py#L188-L194)（`__call__` 里用到 `embed_tokens`、`lm_head` 的两行），指出「借」在哪一步生效、在哪一步使用。
   - 再对照 README 的 MLX 示例（`draft = load_draft(...)` 之后 `stream_generate(...)` 内部会调 `draft.bind(model)`），确认 `bind` 的调用时机是在生成开始时、由 `stream_generate` 代为完成的（你不必自己手动调）。
   - 假设一份草稿的 `layer_types` 是 `["full_attention"] * 10`，预测 `make_cache()` 会返回什么；若改成前 8 层 `full_attention`、后 2 层 `sliding_attention`（且 `sliding_window=1024`），预测缓存列表里第 9、10 个元素分别是什么类型、容量多少。
3. **需要观察的现象**：`bind` 之后 `draft.embed_tokens` 不再是 `None`；`make_cache()` 返回的列表长度等于 `num_hidden_layers`，且元素类型随 `layer_types` 变化。
4. **预期结果**：
   - 第一种情况：返回 10 个普通 `KVCache()`。
   - 第二种情况：前 8 个是 `KVCache()`，第 9、10 个是 `RotatingKVCache(max_size=1023, keep=0)`。
5. 若手头没有 Apple 芯片机器，以上为「阅读推导」结论，标注「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：MLX 版草稿的 `__call__` 返回 logits，Transformers 版草稿的 `forward` 返回隐藏状态。为什么会有这个差别？它对调用方有什么影响？

> **参考答案**：差别源于「`lm_head` 在哪一层被调用」。MLX 版把「借来的」`lm_head` 绑在草稿对象上，于是草稿干脆自己出 logits，调用方（`stream_generate`）拿到 logits 直接采样即可，草稿是一个「自洽」的黑盒。Transformers 版的 `lm_head` 在外部 `dflash_generate` 手里，草稿 `forward` 只负责到 `norm`（隐藏状态），`lm_head` 由外部应用——这样草稿和 target 共用同一个 `lm_head` 调用点，写法更扁平。两种都是合理的工程取舍：MLX 版强调模型自洽，Transformers 版强调 target/draft 资源共享的内联性。

**练习 2**：`bind` 方法里寻找 `embed_tokens` 时为什么要写三层 `if/elif`？去掉后两层会有什么后果？

> **参考答案**：因为不同模型的封装层级不一致：普通语言模型直接是 `model.embed_tokens`；Transformers 式 CausalLM 包了一层 `model.model.embed_tokens`；多模态模型可能再包一层 `language_model.model.embed_tokens`（README 提到 MLX 后端测过 Gemma-4 等多种模型）。如果去掉后两层探测，就只能支持第一种封装，遇到 CausalLM 或多模态 target 时会直接抛 `AttributeError("Cannot find embed_tokens ...")`，无法绑定。这三层 `if/elif` 正是为了适配多种 target 结构。

**练习 3**：`make_cache` 里 `RotatingKVCache(max_size=sliding_window - 1, keep=0)` 的 `keep=0` 是什么意思？如果改成 `keep=4` 会怎样？

> **参考答案**：`RotatingKVCache` 的 `keep` 参数表示「即使超出 `max_size`，也永久保留最前面 `keep` 个 token 的 key/value」——常用于让滑动窗口层始终能看到 prompt 开头的少量 token（类似「attention sink」）。`keep=0` 意味着**不保留任何**老 token，纯粹的最近 `max_size` 窗口，是标准滑动窗口行为。若改成 `keep=4`，则缓存里会始终保留序列最前面 4 个 token，注意力窗口变成「最近 `max_size` 个 + 最前 4 个」，与草稿训练时的注意力模式不一致，可能导致生成质量下降——所以这里刻意用 `keep=0` 以匹配训练设定。

---

## 5. 综合实践

把本讲三个模块串起来，完成下面这个**贯穿性阅读任务**（无需运行，目标是建立对 MLX 分支的整体心智模型）。

**任务：为 MLX 版草稿模型写一份「加载与单次前向」说明书。**

1. **读 `load_draft`（4.1）**：从 [dflash/model_mlx.py:206-240](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/model_mlx.py#L206-L240) 出发，用自己的话写一份「输入一个 draft_id，到拿回一个可用草稿对象」的步骤清单，标注每一步用到的关键函数（`snapshot_download` / `json.loads` / `mx.load` / `load_weights`）。
2. **对照 Transformers 版**：列出 `load_draft` 相比 `DFlashDraftModel.from_pretrained` **多做了**哪些事（提示：layer_types 校验、字段映射、手动合并权重）。
3. **读 `DFlashAttention`（4.2）**：画出一次 `__call__` 的数据流图，重点标出「只有 context 进缓存、proposal 后接不持久化」这一步，并用一句话解释为什么这对投机解码的回滚是友好的。
4. **读 `bind / make_cache / __call__`（4.3）**：在图上标出「两个借」（`embed_tokens`、`lm_head`）的绑定位置与使用位置；标注全注意力层和滑动窗口层分别拿到哪种缓存。
5. **收尾提问**：如果某天 DFlash 要支持一个「前半全注意力、后半滑动窗口」的混合草稿，`model_mlx.py` 里哪些部分**不需要改**（因为已经按层类型分发了），哪些部分**需要确认**（比如 `load_draft` 的 `layer_types` 校验、`make_cache` 的缓存分发）？

**预期产出**：一张数据流图 + 一份步骤清单。这份说明书将直接服务于下一讲 u3-l2——因为 `stream_generate` 的生成主循环正是建立在本讲的 `bind / make_cache / __call__` 之上的。

## 6. 本讲小结

- **MLX 版没有配置基础设施**：`DFlashConfig` 是手写 dataclass，`load_draft` 手动 `snapshot_download` + 解析 JSON + `mx.load` 合并权重 + `load_weights`，相当于「手搓版 `from_pretrained`」；`target_layer_ids`、`mask_token_id` 在 MLX 版是**必填**（无 `build_target_layer_ids` 兜底），`final_logit_softcapping` 是 MLX 版独有（为 Gemma 准备）。
- **注意力两路输入命名不同、本质相同**：MLX 版叫 context / proposal，Transformers 版叫 context / noise；都是「query 只来自本块、context 在前、块内双向可见、总长 = context + block_size」。
- **核心差异在「谁进缓存」**：MLX 版**只把 context 写进 KV 缓存**，proposal 在 `update_and_fetch` 之后拼接、**不持久化**；这让被 target 拒绝的草稿候选天然可弃，为回滚铺路。Transformers 版则把 context+noise 一起缓存。
- **滑动窗口层用 `RotatingKVCache`**：普通 `KVCache` 无限增长，滑动层只需看最近 `sliding_window` 个 key，故用容量为 `sliding_window-1` 的 `RotatingKVCache` 把存储钉在 \(O(\text{window})\)；注意力里还会同步截断 context、前移 `cache.offset`。
- **`bind` 是 MLX 版的「两个借」**：一次性探测并绑定 target 的 `embed_tokens`（含 `embed_scale`）和 `lm_head`（含权重绑定 `as_linear` 兜底），支持三种 target 封装层级。
- **`__call__` 返回 logits（非隐藏状态）**：MLX 版把 `lm_head` 包进草稿、内部完成嵌入与出 logits，比 Transformers 版（`forward` 只到 `norm`、`lm_head` 在外部）更自洽；`logits_start` 用于切掉块首锚点 token。

## 7. 下一步学习建议

本讲把 MLX 版草稿模型的**静态结构**（配置、加载、注意力、模型外壳）讲透了，但它一直是个「待被调用的对象」——我们还没看它怎么被真正驱动起来做投机解码。下一讲 **u3-l2《MLX 流式生成循环与缓存回滚》** 正好接上：

- `stream_generate` 的块起草 / 验证 / 接受主循环，以及它如何流式产出文本；
- `_LayerHook` + `_patch_model` 如何用钩子**捕获 target 指定层的隐藏状态**（本讲里 `target_hidden` 的来源）；
- 拒绝 token 后 `_trim_recent_cache` 如何回滚 target / draft 的 KV 缓存——本讲「proposal 不进缓存」的设计在这里兑现。

建议你在进入 u3-l2 前，先回头确认两件事：一是本讲 4.2 的「只有 context 进缓存」你是否真的理解了；二是 4.3 的 `bind / make_cache / __call__` 调用链是否能默写出来——u3-l2 的生成循环完全建立在这三者之上。如果你对混合架构（Qwen3.5 的 GatedDeltaNet）感兴趣，还可以预告性地跳读 [dflash/model_mlx.py:293-397](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/model_mlx.py#L293-L397) 的 `_GDNStateCapture`，那是 u3-l3 的主题。
