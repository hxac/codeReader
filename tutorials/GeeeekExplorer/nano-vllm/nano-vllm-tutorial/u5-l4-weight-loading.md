# safetensors 权重加载与 packed_modules_mapping

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 `load_model` 是如何遍历 `.safetensors` 文件、把磁盘上的权重逐个写进模型的；
- 理解 HuggingFace（HF）checkpoint 里的权重名和 nano-vllm 模型里的参数名**并不完全相同**，并能解释 `packed_modules_mapping` 如何在两者之间做「改名 + 标记分片」；
- 掌握各并行层（`QKVParallelLinear`、`MergedColumnParallelLinear`、`RowParallelLinear`、`VocabParallelEmbedding`）挂在参数上的 `weight_loader` 钩子，如何把一份完整的 HF 权重**切片**后写入当前 rank 持有的分片区域；
- 能够手算 `shard_offset` 与 `shard_size`，画出 `q_proj/k_proj/v_proj` 三块权重装入合并张量 `qkv_proj` 的索引映射图。

本讲是 u4-l5（张量并行线性层）的下游：u4-l5 讲的是「前向计算时权重怎么切」，本讲讲的是「加载时这些切好的分片如何被正确填充」。

## 2. 前置知识

在开始之前，你需要先建立几个直觉（若已熟悉可跳过）：

- **safetensors 是什么**：HuggingFace 推广的一种「只存张量」的二进制权重格式。一个模型可能被切成多个 `.safetensors` 文件（如 `model-00001-of-00003.safetensors`），每个文件内部是一组「名字 → 张量」的映射。我们用 `safe_open(file, "pt", "cpu")` 以 PyTorch 后端、CPU 模式打开它，用 `f.keys()` 列出权重名，用 `f.get_tensor(name)` 取出张量。它的好处是**零拷贝、可秒级加载**，且避免 `torch.load` pickle 的安全风险。

- **权重名即「模块路径」**：HF checkpoint 里的一个权重名如 `model.layers.0.self_attn.q_proj.weight`，本质上就是「按 `.` 拆开」后在模型里逐层 `getattr` 走到的那个 `nn.Parameter`。PyTorch 的 `nn.Module.get_parameter("model.layers.0.self_attn.q_proj.weight")` 正是这么做的。所以「加载权重」在概念上就是：取出同名参数，把磁盘张量 `copy_` 进去。

- **为什么要合并（pack）权重**：HF 把 Q/K/V 实现成**三个独立的小线性层**（三次 matmul），而 nano-vllm 为了减少 kernel launch、提高 GEMM 利用率，把它们合并成**一个** `QKVParallelLinear`（一次 matmul 同时算出 Q、K、V），SwiGLU 的 gate/up 也同理合并成 `gate_up_proj`。于是磁盘上是 3+2=5 个矩阵，模型里却是 2 个大矩阵——加载时必须把 5 块分别塞进 2 个大矩阵的正确位置。

- **张量并行（TP）下的「分片」**：在 u4-l5 讲过，TP 把一个权重按某维度切给多张卡。加载时每张卡（每个 rank）都读**同一份完整 HF 权重**，但只把自己负责的那一片 `copy_` 进自己的分片参数里。这个「取哪一片」正是 `weight_loader` 的职责。

- **`weight_loader` 是挂在参数上的钩子**：nano-vllm 不在加载器里写一堆 `if isinstance(layer, ...)` 分支，而是给**每个参数对象**都绑一个 `weight_loader` 方法（见 [linear.py:L26](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/layers/linear.py#L26)）。加载器只管「找到参数 → 调它的 `weight_loader`」，分片策略由参数自己决定。这是一种**策略与调度解耦**的设计。

> 承接前置讲义：本讲默认你已读过 u4-l5，知道 `ColumnParallelLinear`（列并行，tp_dim=0）、`RowParallelLinear`（行并行，tp_dim=1）、`QKVParallelLinear`（用字符串 `shard_id` 区分 q/k/v）、`MergedColumnParallelLinear`（用整数 `shard_id` 区分 gate/up）的区别。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
|---|---|
| [nanovllm/utils/loader.py](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/utils/loader.py) | 加载调度中枢：`load_model` 遍历 safetensors、`default_weight_loader` 是兜底策略 |
| [nanovllm/models/qwen3.py](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/models/qwen3.py) | 声明 `packed_modules_mapping`，把 q/k/v、gate/up 指向合并模块 |
| [nanovllm/layers/linear.py](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/layers/linear.py) | 各并行线性层的 `weight_loader`：决定分片如何切、写到哪 |
| [nanovllm/layers/embed_head.py](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/layers/embed_head.py) | 词表并行的 `weight_loader`：按词表维度切分 |
| [nanovllm/engine/model_runner.py](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/model_runner.py) | 调用入口：`ModelRunner.__init__` 里那句 `load_model(self.model, config.model)` |

一句话总览链路：

```
ModelRunner.__init__  →  load_model(model, path)
                              │
                              ├─ 取 model.packed_modules_mapping
                              ├─ for 每个 *.safetensors:
                              │     for 每个权重名:
                              │        命中 packed? → 改名 + 取 shard_id → param.weight_loader(param, w, shard_id)
                              │        否则        → param.weight_loader(param, w)  # 或 default
                              └─ 各 weight_loader 按 tp_rank 切片后 copy_
```

## 4. 核心概念与源码讲解

本讲拆为三个最小模块：`load_model`（调度中枢）、`packed_modules_mapping`（改名映射）、`weight_loader`（分片写入）。

### 4.1 load_model：遍历 safetensors 的调度中枢

#### 4.1.1 概念说明

`load_model` 是整个加载流程的「调度员」。它本身**不知道**任何模型细节——不知道有几个层、不知道 Q/K/V 要合并、不知道 TP 怎么切。它只做三件通用的事：

1. 找到目录下所有 `.safetensors` 文件；
2. 对文件里的每个权重，决定它该走「合并分片路径」还是「普通直配路径」；
3. 把具体的「取哪一片、写到哪」交给参数自己绑定的 `weight_loader`。

这种「胖调度、瘦策略」的写法，让加载器可以复用于任意模型——只要模型愿意提供 `packed_modules_mapping` 和带 `weight_loader` 的参数。

#### 4.1.2 核心流程

`load_model` 的伪代码：

```
读 model.packed_modules_mapping（没有就当空字典）
for 每个 safetensors 文件 file:
    safe_open(file):
        for 每个权重名 weight_name:
            for 每个 packed 键 k:
                if k 是 weight_name 的子串:          # 命中合并模块
                    (新名 v, shard_id) = mapping[k]
                    param = 取参数(weight_name 里把 k 换成 v)
                    param.weight_loader(param, 该权重, shard_id)
                    break                            # 一个权重只走一条路
            else:                                   # for 没被 break → 没命中任何 packed
                param = 取参数(weight_name)
                loader = param.weight_loader 或 default_weight_loader
                loader(param, 该权重)
```

这里有一个**关键的 Python 语法点**：`for ... else`。`else` 子句**仅在 for 循环未被 `break` 打断、正常耗尽时**才执行。也就是说：

- 只要某个 packed 键命中（`k in weight_name`），就走合并路径并 `break`，`else` 被跳过；
- 一个 packed 键都没命中，循环正常结束，进入 `else`，走普通直配路径。

于是**每个权重恰好走一条路径**，互斥且完备。

#### 4.1.3 源码精读

加载入口在 `ModelRunner.__init__` 中，模型构造完立刻加载（[model_runner.py:L31-L32](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/model_runner.py#L31-L32)）：先用 `hf_config` 构造出带分片形状的空模型，再调用 `load_model` 填权重。

```python
self.model = Qwen3ForCausalLM(hf_config)
load_model(self.model, config.model)
```

`load_model` 本体（[loader.py:L12-L28](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/utils/loader.py#L12-L28)）：

```python
def load_model(model: nn.Module, path: str):
    packed_modules_mapping = getattr(model, "packed_modules_mapping", {})
    for file in glob(os.path.join(path, "*.safetensors")):
        with safe_open(file, "pt", "cpu") as f:
            for weight_name in f.keys():
                for k in packed_modules_mapping:
                    if k in weight_name:
                        v, shard_id = packed_modules_mapping[k]
                        param_name = weight_name.replace(k, v)
                        param = model.get_parameter(param_name)
                        weight_loader = getattr(param, "weight_loader")
                        weight_loader(param, f.get_tensor(weight_name), shard_id)
                        break
                else:
                    param = model.get_parameter(weight_name)
                    weight_loader = getattr(param, "weight_loader", default_weight_loader)
                    weight_loader(param, f.get_tensor(weight_name))
```

逐行说明：

- **L13**：`getattr(model, "packed_modules_mapping", {})` 取改名映射；若模型没定义（普通模型），按空字典处理，所有权重都走 `else` 直配。注意它取的是**最外层模型对象**的属性，所以 `packed_modules_mapping` 必须定义在 `Qwen3ForCausalLM` 上（见 4.2）。
- **L14**：`glob(... "*.safetensors")` 匹配目录下所有分片文件，顺序由文件名字典序决定，与加载正确性无关（每个权重都是独立 `copy_`）。
- **L15**：`safe_open(file, "pt", "cpu")` 以 PyTorch 后端在 CPU 打开，避免一次性把整个文件读进显存。
- **L18**：`if k in weight_name` 是**子串匹配**（不是精确等于）。这样 `q_proj` 能命中 `model.layers.12.self_attn.q_proj.weight` 里任意一层。代价是键名必须两两互不为子串（详见 4.2.4）。
- **L19-L20**：取出 `(新模块名 v, shard_id)`，并把权重名里的 `k` 替换成 `v`，得到模型里真正的参数名。
- **L22-L23**：合并路径**没有 `default_weight_loader` 兜底**——合并参数（`qkv_proj`/`gate_up_proj`）一定带自定义 `weight_loader`，否则会 `AttributeError`。这是合理的：能被合并就一定是并行层，就一定有分片策略。
- **L27**：普通路径有兜底——若参数没绑 `weight_loader`（比如未来新增的非并行层），就用 [default_weight_loader](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/utils/loader.py#L8-L9) 整体 `copy_`。

#### 4.1.4 代码实践

**实践目标**：在不跑 GPU 推理的前提下，亲手模拟 `load_model` 的「分发」逻辑，验证一个权重名会被分到哪条路径、拿到什么 `shard_id`。

**操作步骤**（以下为示例代码，可在任意 Python 环境运行，无需 GPU/NCCL）：

```python
# 示例代码：模拟 load_model 的分发逻辑（只看"改名"，不真的 copy_）
packed_modules_mapping = {
    "q_proj":   ("qkv_proj", "q"),
    "k_proj":   ("qkv_proj", "k"),
    "v_proj":   ("qkv_proj", "v"),
    "gate_proj":("gate_up_proj", 0),
    "up_proj":  ("gate_up_proj", 1),
}

def dispatch(weight_name):
    for k in packed_modules_mapping:
        if k in weight_name:
            v, shard_id = packed_modules_mapping[k]
            return ("packed", weight_name.replace(k, v), shard_id)
    return ("default", weight_name, None)

for w in [
    "model.layers.0.self_attn.q_proj.weight",
    "model.layers.0.self_attn.k_proj.weight",
    "model.layers.0.self_attn.v_proj.weight",
    "model.layers.0.self_attn.o_proj.weight",     # 期望走 default
    "model.layers.0.mlp.gate_proj.weight",
    "model.layers.0.mlp.up_proj.weight",
    "model.layers.0.mlp.down_proj.weight",         # 期望走 default
    "model.embed_tokens.weight",                   # 期望走 default
]:
    print(f"{w:55s} -> {dispatch(w)}")
```

**需要观察的现象**：

- `q_proj/k_proj/v_proj` 都被改名成 `qkv_proj`，但携带不同的 `shard_id`（`"q"`/`"k"`/`"v"`）——这正是后续 `weight_loader` 区分写入位置的依据。
- `o_proj`、`down_proj`、`embed_tokens` 走 `default` 路径，`shard_id=None`。

**预期结果**：`o_proj`/`down_proj`/`embed_tokens` 三行输出 `(default, ..., None)`，其余行输出 `(packed, ..., <shard_id>)`。若你看到 `o_proj` 被误判为 `packed`，说明对 `for-else` 或子串匹配的理解有偏差，回头重读 4.1.2。

#### 4.1.5 小练习与答案

**练习 1**：若把 `load_model` 里的 `for k in packed_modules_mapping` 改成「先收集所有命中键再逐个处理」（去掉 `break`），会发生什么错误？

**参考答案**：同一个权重名可能命中多个键（若键名设计不当），导致**重复 `copy_`**，后者覆盖前者；更严重的是，同一个 `q_proj` 权重会被当成多个不同 shard 写入，破坏参数。`break` 保证了「一个权重只匹配第一个命中的键，只走一条路径」。

**练习 2**：为什么合并路径（L22）用 `getattr(param, "weight_loader")` 不带默认值，而普通路径（L27）带 `default_weight_loader` 默认值？

**参考答案**：会出现在 `packed_modules_mapping` 里的模块，**一定是**被合并的并行层（`QKVParallelLinear`/`MergedColumnParallelLinear`），它们在构造时必然给参数绑了 `weight_loader`（见 [linear.py:L26](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/layers/linear.py#L26)），若取不到说明模型定义有 bug，应直接报错。普通路径则可能遇到未特殊处理的参数，需要兜底整体拷贝，容错性更强。

---

### 4.2 packed_modules_mapping：从「分散权重名」到「合并参数名」

#### 4.2.1 概念说明

`packed_modules_mapping` 是模型作者向加载器声明的一份「改名 + 分片」契约，定义在 `Qwen3ForCausalLM` 类上（[qwen3.py:L187-L193](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/models/qwen3.py#L187-L193)）。它的语义是：

> 「如果在磁盘权重名里看到键 `k`，说明它其实是模型里 `v` 这个**合并参数**的第 `shard_id` 块。」

它解决了「HF 用 5 个小矩阵，nano-vllm 用 2 个大矩阵」的命名鸿沟。

#### 4.2.2 核心流程

```python
packed_modules_mapping = {
    "q_proj":    ("qkv_proj", "q"),     # q_proj  -> qkv_proj 的 "q" 分片
    "k_proj":    ("qkv_proj", "k"),     # k_proj  -> qkv_proj 的 "k" 分片
    "v_proj":    ("qkv_proj", "v"),     # v_proj  -> qkv_proj 的 "v" 分片
    "gate_proj": ("gate_up_proj", 0),   # gate_proj -> gate_up_proj 的第 0 块
    "up_proj":   ("gate_up_proj", 1),   # up_proj   -> gate_up_proj 的第 1 块
}
```

数据结构是 `{ HF 键: (合并参数名后缀, shard_id) }`。`shard_id` 的类型有两种：

- **字符串**（`"q"`/`"k"`/`"v"`）：用于 `QKVParallelLinear`，因为 GQA 下 q 与 k/v 的宽度不同（详见 4.3.2），需要用名字区分而非序号。
- **整数**（`0`/`1`）：用于 `MergedColumnParallelLinear`，因为 gate 和 up 宽度相同，用序号即可。

改名发生在加载器的 `weight_name.replace(k, v)`：`model.layers.0.self_attn.q_proj.weight` → `model.layers.0.self_attn.qkv_proj.weight`。`shard_id` 则作为 `weight_loader` 的第三个参数透传下去。

#### 4.2.3 源码精读

模型里把 HF 的「三个独立投影」合并成「一个 `qkv_proj`」，发生在 `Qwen3Attention.__init__`（[qwen3.py:L42-L48](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/models/qwen3.py#L42-L48)）：

```python
self.qkv_proj = QKVParallelLinear(
    hidden_size,
    self.head_dim,
    self.total_num_heads,
    self.total_num_kv_heads,
    bias=qkv_bias,
)
```

对应地，HF checkpoint 里这一层存的是 `q_proj.weight`、`k_proj.weight`、`v_proj.weight` 三份。前向时（[qwen3.py:L77-L78](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/models/qwen3.py#L77-L78)）一次 matmul 算出 `qkv`，再 `split` 回 q/k/v：

```python
qkv = self.qkv_proj(hidden_states)
q, k, v = qkv.split([self.q_size, self.kv_size, self.kv_size], dim=-1)
```

MLP 侧同理：HF 的 `gate_proj` 与 `up_proj` 合并成 `gate_up_proj`（[qwen3.py:L100-L104](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/models/qwen3.py#L100-L104)），前向用 `SiluAndMul` 一次性门控（[qwen3.py:L113-L116](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/models/qwen3.py#L113-L116)）。

> 注意：`o_proj`、`down_proj` 没有出现在 `packed_modules_mapping` 里——它们是 `RowParallelLinear`，**没有合并**，所以走 `else` 直配路径，由 `RowParallelLinear.weight_loader` 按 tp 切分（见 4.3.3）。同理 `embed_tokens` 也走直配。

#### 4.2.4 代码实践

**实践目标**：理解「子串匹配」的隐患，验证 Qwen3 的键名设计是否安全。

**操作步骤**：思考并运行下面这段示例代码：

```python
# 示例代码：检验子串匹配是否会误伤
keys = ["q_proj", "k_proj", "v_proj", "gate_proj", "up_proj"]
names = ["model.layers.0.self_attn.q_proj.weight",
         "model.layers.0.self_attn.o_proj.weight",
         "model.layers.0.mlp.down_proj.weight",
         "model.layers.0.mlp.gate_proj.weight"]
for n in names:
    hits = [k for k in keys if k in n]
    print(f"{n:55s} -> hits={hits}")
```

**需要观察的现象**：

- `o_proj.weight` 的命中列表应当为**空**（`q_proj/k_proj/v_proj/gate_proj/up_proj` 都不是 `o_proj` 的子串）。
- `down_proj.weight` 的命中列表也应为**空**——尤其要确认 `up_proj` **不是** `down_proj` 的子串（`down_proj` 里只有 `n_proj`，没有 `up_proj`）。

**预期结果**：`o_proj` 与 `down_proj` 都命中 `[]`，从而安全走 `else` 直配路径。如果某个键名（比如把 `up_proj` 起成 `proj`）会同时命中很多权重，子串匹配就会失灵——这正是键名必须**两两互不为子串**的原因。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `q_proj/k_proj/v_proj` 用**字符串** `shard_id`，而 `gate_proj/up_proj` 用**整数** `shard_id`？

**参考答案**：因为 `QKVParallelLinear` 里 q 的宽度（`num_heads*head_dim`）与 k/v 的宽度（`num_kv_heads*head_dim`）**不相等**（GQA 下 `num_kv_heads < num_heads`），三段宽度不一旦用整数下标容易写死假设；用字符串 `"q"/"k"/"v"` 让 `weight_loader` 自己查表算偏移更清晰。而 `gate_up_proj` 的 gate 和 up 宽度相同（都是 `intermediate_size`），用整数 `0/1` 即可，`weight_loader` 里直接用 `output_sizes[shard_id]` 索引。

**练习 2**：如果一个新模型把 `o_proj` 也合并进了某个 `qkvo_proj`，你该如何修改 `packed_modules_mapping`？

**参考答案**：新增一项 `"o_proj": ("qkvo_proj", "o")`（或对应整数下标），并在该合并层的 `weight_loader` 里加上对 `"o"` 分片的 `shard_offset/shard_size` 处理。同时要确保 `"o_proj"` 与现有键名互不为子串（这里没问题），并保证模型里确实存在名为 `qkvo_proj` 的合并参数。

---

### 4.3 weight_loader：各并行层的分片写入策略

#### 4.3.1 概念说明

`weight_loader` 是绑在**每个 `nn.Parameter` 上**的方法（注意是绑在参数对象上，不是模块上）。它回答两个问题：

1. **取哪一片**：从完整的 HF 权重里，切出当前 rank 负责的那一部分；
2. **写到哪**：把切出来的片，`copy_` 进本 rank 参数张量的哪个区域（合并参数时尤其重要）。

不同层因为「切的方式」和「是否合并」不同，`weight_loader` 的实现也不同。下面分四类讲。

#### 4.3.2 核心流程：QKVParallelLinear 的 weight_loader（本讲重点）

GQA 下 q 头多、k/v 头少。设全局 `total_num_heads = H`、`total_num_kv_heads = KV`、`head_dim = D`、`tp_size = T`，则每个 rank 持有：

\[
\text{num\_heads} = H/T, \quad \text{num\_kv\_heads} = KV/T
\]

本 rank 的 `qkv_proj.weight` 形状为 `((num_heads + 2·num_kv_heads)·D, hidden_size)`，其行方向布局是**三段拼接**：

```
[ q 段 | k 段 | v 段 ]
[ num_heads·D | num_kv_heads·D | num_kv_heads·D ]
```

三段的 `shard_offset` 与 `shard_size`（[linear.py:L114-L128](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/layers/linear.py#L114-L128)）：

| shard_id | shard_size | shard_offset |
|---|---|---|
| `"q"` | `num_heads · D` | `0` |
| `"k"` | `num_kv_heads · D` | `num_heads · D` |
| `"v"` | `num_kv_heads · D` | `num_heads · D + num_kv_heads · D` |

加载 q_proj 时，`weight_loader` 做两步切片：

1. **切目标**：`param_data.narrow(0, shard_offset, shard_size)` —— 把本 rank 的 `qkv_proj.weight` 缩到 q 段那一块；
2. **切来源**：`loaded_weight.chunk(self.tp_size, 0)[self.tp_rank]` —— 把完整 HF `q_proj.weight`（共 `H·D` 行）沿行切成 T 份，取本 rank 那一份（`num_heads·D` 行）；
3. `copy_`。

**为什么要同时 narrow 目标 + chunk 来源，而 `ColumnParallelLinear`（单未合并）只 narrow 来源？** 因为合并参数里**同一行区间还细分为多个 shard**：必须先用 `shard_offset` 定位「q 段从第几行开始」，再用 `chunk` 从 HF 权重里取出「本 rank 的 q」。而未合并的 `ColumnParallelLinear` 整个参数就是单一 shard，没有内部偏移，只需从来源里切出本 rank 即可。

#### 4.3.3 源码精读：四类 weight_loader 对照

**（a）`QKVParallelLinear.weight_loader`**（[linear.py:L114-L128](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/layers/linear.py#L114-L128)）：

```python
def weight_loader(self, param, loaded_weight, loaded_shard_id):
    param_data = param.data
    assert loaded_shard_id in ["q", "k", "v"]
    if loaded_shard_id == "q":
        shard_size = self.num_heads * self.head_size
        shard_offset = 0
    elif loaded_shard_id == "k":
        shard_size = self.num_kv_heads * self.head_size
        shard_offset = self.num_heads * self.head_size
    else:  # "v"
        shard_size = self.num_kv_heads * self.head_size
        shard_offset = self.num_heads*self.head_size + self.num_kv_heads*self.head_size
    param_data = param_data.narrow(self.tp_dim, shard_offset, shard_size)
    loaded_weight = loaded_weight.chunk(self.tp_size, self.tp_dim)[self.tp_rank]
    param_data.copy_(loaded_weight)
```

注意 `output_size` 在构造时按**全局**头数计算（[linear.py:L111](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/layers/linear.py#L111)），再由父类 `ColumnParallelLinear` 除以 tp（[linear.py:L63](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/layers/linear.py#L63)），保证本 rank 的 `qkv_proj.weight` 行数 = `(num_heads + 2·num_kv_heads)·D`。

**（b）`MergedColumnParallelLinear.weight_loader`**（[linear.py:L87-L93](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/layers/linear.py#L87-L93)）：逻辑与 QKV 完全同构，只是 `shard_id` 是整数、宽度来自 `output_sizes`：

```python
def weight_loader(self, param, loaded_weight, loaded_shard_id):
    param_data = param.data
    shard_offset = sum(self.output_sizes[:loaded_shard_id]) // self.tp_size
    shard_size = self.output_sizes[loaded_shard_id] // self.tp_size
    param_data = param_data.narrow(self.tp_dim, shard_offset, shard_size)
    loaded_weight = loaded_weight.chunk(self.tp_size, self.tp_dim)[self.tp_rank]
    param_data.copy_(loaded_weight)
```

对 `gate_up_proj`，`output_sizes = [intermediate_size, intermediate_size]`（[qwen3.py:L100-L104](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/models/qwen3.py#L100-L104) 传入 `[intermediate_size]*2`）。于是 gate（id=0）落到 `[0, inter/T)`，up（id=1）落到 `[inter/T, 2·inter/T)`。

**（c）`RowParallelLinear.weight_loader`**（[linear.py:L142-L150](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/layers/linear.py#L142-L150)）：行并行沿**输入维**（dim=1）切，且对 1-D 的 bias 做特殊处理（整拷贝）：

```python
def weight_loader(self, param, loaded_weight):
    param_data = param.data
    if param_data.ndim == 1:          # bias：1-D，直接整拷贝
        param_data.copy_(loaded_weight)
        return
    shard_size = param_data.size(self.tp_dim)      # tp_dim=1 → input_size/T
    start_idx = self.tp_rank * shard_size
    loaded_weight = loaded_weight.narrow(self.tp_dim, start_idx, shard_size)
    param_data.copy_(loaded_weight)
```

注意行并行只 `narrow` 来源、不 narrow 目标——因为它的参数没有内部合并，整块就是本 rank 的分片。`o_proj`、`down_proj` 都走这条（且它们不在 `packed_modules_mapping` 里，故走 `load_model` 的 `else` 分支）。

**（d）`VocabParallelEmbedding.weight_loader`**（[embed_head.py:L27-L32](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/layers/embed_head.py#L27-L32)）：沿词表维（dim=0）切：

```python
def weight_loader(self, param, loaded_weight):
    param_data = param.data
    shard_size = param_data.size(0)              # vocab_size / tp_size
    start_idx = self.tp_rank * shard_size
    loaded_weight = loaded_weight.narrow(0, start_idx, shard_size)
    param_data.copy_(loaded_weight)
```

#### 4.3.4 四类 weight_loader 总览表

| 层（来源文件） | HF 权重 | 目标参数 | shard_id | 切来源 | 切目标 |
|---|---|---|---|---|---|
| `QKVParallelLinear` (linear.py) | q/k/v_proj | qkv_proj | str | `chunk(tp)[rank]` | `narrow(0, offset, size)` |
| `MergedColumnParallelLinear` (linear.py) | gate/up_proj | gate_up_proj | int | `chunk(tp)[rank]` | `narrow(0, offset, size)` |
| `ColumnParallelLinear` (linear.py) | — | — | 无 | `narrow(tp_dim, rank·size, size)` | 不 narrow |
| `RowParallelLinear` (linear.py) | o/down_proj | o/down_proj | 无 | `narrow(1, rank·size, size)` | 不 narrow |
| `VocabParallelEmbedding` (embed_head.py) | embed_tokens | embed_tokens | 无 | `narrow(0, rank·size, size)` | 不 narrow |

一句话规律：**合并层需要「切目标 + 切来源」，非合并层只需「切来源」**。

#### 4.3.5 tie_word_embeddings 的特殊处理

当 `config.tie_word_embeddings=True` 时，`lm_head` 与 `embed_tokens` **共享同一块权重存储**（[qwen3.py:L202-L203](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/models/qwen3.py#L202-L203)）：

```python
if config.tie_word_embeddings:
    self.lm_head.weight.data = self.model.embed_tokens.weight.data
```

此时 `lm_head.weight` 与 `model.embed_tokens.weight` 是**同一个底层张量**。加载时只要 `model.embed_tokens.weight` 被 `VocabParallelEmbedding.weight_loader` 正确填充，`lm_head` 就自动跟着对了（它们指向同一片显存）。无论 HF checkpoint 里是否额外存了 `lm_head.weight`，至多多一次无害的重复 `copy_`。

#### 4.3.6 代码实践（本讲核心实践）

**实践目标**：亲手画出 HF 的 `q_proj/k_proj/v_proj` 三块权重，如何经 `packed_modules_mapping` 改名 + `QKVParallelLinear.weight_loader` 切片，装入合并张量 `qkv_proj`。给出 `shard_offset` 与 `shard_size` 的具体索引映射。

**操作步骤**：以下示例代码**脱离 GPU/NCCL**，用纯 PyTorch 复现 `QKVParallelLinear.weight_loader` 的切片逻辑（手动设置 `num_heads/num_kv_heads`，模拟某个 rank）。先看 `tp_size=1` 的简单情形：

```python
# 示例代码：模拟 QKVParallelLinear.weight_loader 的写入（tp_size=1, rank=0）
import torch

# —— 假装我们是 QKVParallelLinear，已经构造好（tp_size=1）——
total_num_heads, total_num_kv_heads, head_dim = 16, 8, 128
num_heads, num_kv_heads = total_num_heads, total_num_kv_heads     # tp_size=1
hidden_size = 1024
output_rows = (num_heads + 2 * num_kv_heads) * head_dim           # 4096
qkv_proj = torch.zeros(output_rows, hidden_size)                  # 本 rank 的合并参数

# —— 假装这是从 safetensors 取出的三块 HF 权重 ——
q_proj_w = torch.arange(num_heads       * head_dim * hidden_size).float().reshape(num_heads       * head_dim, hidden_size)
k_proj_w = torch.arange(num_kv_heads    * head_dim * hidden_size).float().reshape(num_kv_heads    * head_dim, hidden_size) + 100000
v_proj_w = torch.arange(num_kv_heads    * head_dim * hidden_size).float().reshape(num_kv_heads    * head_dim, hidden_size) + 200000

# —— 复刻 weight_loader 的偏移表（tp_dim=0, tp_size=1, tp_rank=0）——
def load_qkv(shard_id, loaded_weight):
    if shard_id == "q":
        shard_size, shard_offset = num_heads    * head_dim, 0
    elif shard_id == "k":
        shard_size, shard_offset = num_kv_heads * head_dim, num_heads * head_dim
    else:  # "v"
        shard_size, shard_offset = num_kv_heads * head_dim, num_heads*head_dim + num_kv_heads*head_dim
    dst = qkv_proj.narrow(0, shard_offset, shard_size)
    src = loaded_weight.chunk(1, 0)[0]            # tp_size=1 → 整块
    dst.copy_(src)
    print(f"{shard_id}: 写入行 [{shard_offset}, {shard_offset+shard_size})")

# 模拟 load_model 依次调度三块权重
load_qkv("q", q_proj_w)
load_qkv("k", k_proj_w)
load_qkv("v", v_proj_w)

# —— 验证：拆回 q/k/v，应当与原始 HF 权重逐元素相等 ——
q, k, v = qkv_proj.split([num_heads*head_dim, num_kv_heads*head_dim, num_kv_heads*head_dim], dim=0)
print("q 一致:", torch.equal(q, q_proj_w))
print("k 一致:", torch.equal(k, k_proj_w))
print("v 一致:", torch.equal(v, v_proj_w))
```

**需要观察的现象与索引映射图**（`tp_size=1`，`num_heads=16, num_kv_heads=8, head_dim=128`）：

```
qkv_proj.weight 行索引布局（共 4096 行）：
┌──────────────────────────────────┬────────────────────┬────────────────────┐
│  q 段                            │  k 段              │  v 段              │
│  [0      , 2048)                 │  [2048  , 3072)    │  [3072  , 4096)    │
│  shard_offset=0, size=2048       │  offset=2048,1024  │  offset=3072,1024  │
│  ← 装 HF q_proj.weight (2048 行) │  ← HF k_proj (1024)│  ← HF v_proj (1024)│
└──────────────────────────────────┴────────────────────┴────────────────────┘
```

即：HF 的 `q_proj.weight`（`num_heads·D = 2048` 行）写入 `[0, 2048)`；`k_proj.weight`（`num_kv_heads·D = 1024` 行）写入 `[2048, 3072)`；`v_proj.weight` 写入 `[3072, 4096)`。三块拼成合并张量。

**预期结果**：打印出三行 `写入行 [...]` 区间与上图一致；最后三行校验均为 `True`。

**进阶（推演 `tp_size=2`）**：把 `num_heads=8, num_kv_heads=4`（各减半），并令 `src = loaded_weight.chunk(2, 0)[rank]`。对 `rank=0`，HF `q_proj.weight`（全局 2048 行）取 `[0:1024]` 写入本 rank `qkv_proj` 的 `[0, 1024)`；对 `rank=1`，取 `[1024:2048]` 写入 `[0, 1024)`。两张卡合起来覆盖了完整的 q。**待本地验证**：在有双卡的环境里实际构造 `QKVParallelLinear`（需 `dist.init_process_group`），分别打印两个 rank 的 `qkv_proj.weight` 切片，确认互补不重叠。

#### 4.3.7 小练习与答案

**练习 1**：在 `tp_size=2`、`total_num_heads=16`、`total_num_kv_heads=8`、`head_dim=128` 时，`rank=1` 的 `qkv_proj.weight` 共多少行？其中 v 段的 `shard_offset` 是多少？

**参考答案**：本 rank `num_heads=8, num_kv_heads=4`，行数 = `(8 + 2·4)·128 = 2048`。v 段 `shard_offset = num_heads·D + num_kv_heads·D = 8·128 + 4·128 = 1536`，`shard_size = 4·128 = 512`，即 v 占据本 rank 的 `[1536, 2048)` 行。

**练习 2**：为什么 `RowParallelLinear.weight_loader` 里要判断 `param_data.ndim == 1`？

**参考答案**：行并行的 bias 是 1-D 张量（形状 `(output_size,)`），且只在 rank 0 加一次（见 [linear.py:L153](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/layers/linear.py#L153)），不需要按 tp 切分，所以遇到 1-D 直接整体 `copy_` 并 `return`；否则才按 `tp_dim=1` 切来源。不过 Qwen3 的 `o_proj`/`down_proj` 都设了 `bias=False`（[qwen3.py:L52,L108](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/models/qwen3.py#L49-L53)），实际不会触发 bias 分支，这是为通用性预留的防护。

**练习 3**：若加载时忘记给 `qkv_proj.weight` 绑定 `weight_loader`（比如手误删了 [linear.py:L26](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/layers/linear.py#L26) 那行），`load_model` 会怎样？

**参考答案**：`load_model` 在合并路径（[loader.py:L22](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/utils/loader.py#L22)）执行 `getattr(param, "weight_loader")`（无默认值）会抛 `AttributeError`，加载直接失败。这体现了「合并层必须有自定义分片策略」的硬约束。

## 5. 综合实践

把本讲三个模块串起来，完成一次「纸面端到端加载」：

**任务**：假设有一个极简模型，只有 1 层 attention + 1 层 MLP，参数为 `hidden_size=1024, intermediate_size=4096, num_attention_heads=16, num_key_value_heads=8, head_dim=128`，`tp_size=2`，你位于 `rank=1`。

1. **列出该 rank 应持有的所有参数名**及其形状（提示：包括 `embed_tokens`、`qkv_proj`、`o_proj`、`gate_up_proj`、`down_proj`、`norm` 等，注意哪些被合并、哪些被切分）。
2. **对每一类参数，写出它的 `weight_loader` 会从 HF 权重里取哪一片、写到本 rank 的哪个区域**。例如 `gate_up_proj`：HF `gate_proj.weight`（`4096×1024`）经 `chunk(2,0)[1]` 取后 2048 行，写入本 rank `gate_up_proj.weight` 的 `[0, 2048)`；HF `up_proj.weight` 同样取后 2048 行，写入 `[2048, 4096)`。
3. **画出 `qkv_proj` 的行布局图**，标注 q/k/v 三段的 `[offset, offset+size)` 区间（参考 4.3.6 的格式）。

**自检方法**：对照 4.3.4 的总览表逐项核对——合并层（`qkv_proj`/`gate_up_proj`）必须同时「切来源 + 切目标」，非合并层（`o_proj`/`down_proj`/`embed_tokens`）只「切来源」；行并行切 dim=1，列并行与词表切 dim=0。若你的图与总览表一致，即通过。

> 提示：这是一个**源码阅读 + 推演型实践**，无需 GPU。若条件允许，可在双卡环境下用 `dist.init_process_group("nccl", ...)` 真实构造模型、加载一个小的 HF checkpoint，打印各 rank 的权重分片以验证你的纸面结论（属于「待本地验证」部分）。

## 6. 本讲小结

- `load_model` 是**调度中枢**：遍历所有 `.safetensors`，对每个权重用「`for-else` + 子串匹配」二选一地走合并路径或直配路径，具体的切片策略下放给参数自己的 `weight_loader`。
- `packed_modules_mapping` 是模型作者的**改名契约**：把 HF 的 `q_proj/k_proj/v_proj` → `qkv_proj`（字符串 shard_id），`gate_proj/up_proj` → `gate_up_proj`（整数 shard_id），并携带 `shard_id` 透传给 `weight_loader`。
- `weight_loader` 绑在**参数对象**上而非模块上，实现了「调度」与「分片策略」的解耦；合并层（QKV/Merged）需「切来源 + 切目标」，非合并层（Row/Vocab）只「切来源」。
- `QKVParallelLinear.weight_loader` 用 `shard_offset/shard_size` 表定位 q/k/v 三段：q 在 `[0, num_heads·D)`，k 在 `[num_heads·D, (num_heads+num_kv_heads)·D)`，v 在其后；GQA 使 k/v 段比 q 段窄。
- 每个 rank 读**同一份完整 HF 权重**，用 `chunk(tp_size)[tp_rank]` 或 `narrow(tp_dim, rank·size, size)` 只取自己那一片，互不重叠、合起来覆盖全局。
- `tie_word_embeddings=True` 时 `lm_head` 与 `embed_tokens` 共享存储，加载 embedding 即自动覆盖 lm_head。

## 7. 下一步学习建议

本讲把 nano-vllm 的**加载侧**讲完了。至此，从「调度执行（u5-l3 多进程 TP 运行时）」到「权重装填（本讲）」的启动链路已闭合。建议接下来：

- **横向对照 vLLM 原版**：nano-vllm 的 `load_model` 是 vLLM `weight_utils` 的极简版。可以读 vLLM 的 `ModelWeightLoader`、`DefaultModelLoader`，看它如何在此基础上增加**多/单进程加载、权重复用、量化权重映射**等工程能力。
- **纵向扩展到新模型**：尝试为另一个 HF 模型（如 Llama / Mistral）写一个 `XxxForCausalLM`，关键是正确定义 `packed_modules_mapping` 与选择合适的并行层；这是检验你是否真懂本讲的最佳方式。
- **回顾 u4-l5**：把本讲的 `weight_loader`（加载时分片）与 u4-l5 的前向计算（运行时分片）对照重读，体会「同一份切分策略，加载时写、前向时读」的对称性——这也是 Megatron 风格 TP 的设计精髓。
