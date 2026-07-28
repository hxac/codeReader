# ModelArgs 超参与 DeepSeek-V3.2 / GLM-5 双模型差异

## 1. 本讲目标

学完本讲，你应当能够：

- 读懂 `ModelArgs` 数据类里每一个字段对**模型结构**和**显存布局**的影响，并能说出几个字段如何直接决定某个临时张量（temp_var）的形状。
- 区分 **DeepSeek-V3.2** 与 **GLM-5** 在注意力维度、MoE 路由评分函数、专家分组、RoPE 基频上的具体取值差异。
- 理解 `arch_name` 这个字符串如何作为**分发键**，在「后端 .so 选择」「算子算法白名单校验」「权重转换分支」三处发挥作用。
- 独立完成一张双模型超参对比表，并解释 NSA 稀疏索引参数 `index_topk` 与 `max_seq_len` 的关系。

## 2. 前置知识

在进入本讲前，你需要先建立以下认知（来自前面几讲）：

- **TileRT 的双后端架构**：DeepSeek-V3.2 与 GLM-5 各自编译为一个独立 `.so` 后端，单进程只能加载一个（见 u1-l2、u1-l3）。
- **Generator 生命周期**：构造生成器时会传入一个 `model_args` 参数，它是模型所有超参的来源（见 u1-l5）。
- **TileRTModule 抽象体系**：所有算子都继承自 `TileRTModule`，每个算子持有一份 `self.model_args`，并据此决定自己的权重形状与算法（见 u2-l1）。

本讲要解释的核心问题是：**这些算子手里的 `model_args` 到底长什么样？里面每个数字意味着什么？为什么 DeepSeek 和 GLM 两套模型能共用同一份算子代码，却跑出不同的形状？**

两个关键术语先澄清：

- **MLA（Multi-head Latent Attention，多头潜注意力）**：DeepSeek 系列使用的注意力变体，把 Q/K/V 压到低秩潜空间再投影，KV 缓存只存低秩压缩向量，从而省显存。
- **NSA（Native Sparse Attention，原生稀疏注意力）**：TileRT 在 MLA 之上加的一层稀疏索引——先用一个轻量「索引头」给所有历史位置打分，再只挑出得分最高的若干个位置做真正注意力，让长上下文的注意力开销不随序列长度爆炸。`index_topk` 就是「挑多少个位置」。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
| --- | --- |
| [tilert/models/deepseek_v3_2/model_args.py](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/model_args.py) | DeepSeek-V3.2 的 `ModelArgs` 数据类，带详尽字段注释，是讲解字段含义的主参考。 |
| [tilert/models/glm_5/_dsa_v32/model_args.py](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/glm_5/_dsa_v32/model_args.py) | GLM-5 包内部对同一份 DeepSeek 架构超参的**副本**，作为 `ModelArgsGLM5` 的基类，供 `_dsa_v32` 共享算子导入。 |
| [tilert/models/glm_5/model_args.py](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/glm_5/model_args.py) | `ModelArgsGLM5`，继承上面的 `ModelArgs`，覆盖 GLM-5 专属取值。 |
| [tilert/models/base.py](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/base.py) | `TileRTModule`，其 `set_algorithm` 用 `model_args.arch_name` 做算法白名单校验。 |
| [tilert/models/glm_5/_dsa_v32/ops/rmsnorm_projx_wqakis.py](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/glm_5/_dsa_v32/ops/rmsnorm_projx_wqakis.py) | 一个典型的共享算子，按 `arch_name` 分支选择不同的权重打包与计算核。 |

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：**ModelArgs 全字段含义**、**GLM-5 与 DSv3.2 的关键差异**、**arch_name 在分发中的作用**。

### 4.1 ModelArgs 全字段含义

#### 4.1.1 概念说明

`ModelArgs` 是一个普通的 Python `@dataclass`，但它实际上是整个模型的**单一事实来源（single source of truth）**：模型有多少层、每层多大、注意力头怎么切、MoE 有几个专家、KV 缓存怎么压、序列最长多少……全部写死在它的字段默认值里。

它有两个重要特性：

1. **它是带默认值的 dataclass**，可以无参构造 `ModelArgs()`，于是任何算子在没显式传 `model_args` 时都能拿到一份合法配置（这正是 [base.py 第 92 行](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/base.py#L92) `model_args if model_args is not None else ModelArgs()` 的兜底逻辑，见 u2-l1）。
2. **它的字段值直接决定张量形状**。后端的临时变量表（temp_vars）是一组扁平张量，每个张量的某一维几乎都来自 `ModelArgs` 的某个字段。改一个字段，下游几十个张量的形状都要跟着变。

#### 4.1.2 核心流程

`ModelArgs` 字段 → 下游形状/行为的映射，可以归成几组：

- **规模类**：`dim`（隐藏维）、`n_layers`（总层数）、`n_dense_layers`（前几层是稠密层）、`n_heads`（注意力头数）。`n_layers` 与 `n_dense_layers` 一起决定「前 3 层走 MLP、其余层走 MoE」的分界（见 u2-l4 的层循环）。
- **MLA 低秩类**：`q_lora_rank`、`kv_lora_rank`、`qk_nope_head_dim`、`qk_rope_head_dim`、`v_head_dim`。这些决定 Q/K/V 的压缩与解压维度，也决定 KV 缓存每个 token 存多少元素。
- **MoE 路由类**：`n_routed_experts`、`n_activated_experts`、`n_shared_experts`、`n_expert_groups`、`n_limited_groups`、`score_func`、`route_scale`。决定每层激活几个专家、按什么函数给专家打分。
- **NSA 稀疏索引类**：`index_n_heads`、`index_head_dim`、`index_topk`、`max_seq_len`。决定稀疏注意力的索引头规模与最终参与注意力的位置数。
- **RoPE 与数值类**：`rope_theta`、`rope_factor`、`original_seq_len`、`beta_fast`/`beta_slow`、`mscale`、`eps`、`block_size`、`kv_cache_pad`。

举一个最直观的「字段 → 形状」例子（NSA 索引相关，来自 GLM-5 的 temp_vars 构造）：

- `IDX_LOGITS`（每个历史位置的索引得分）的最后一维 = `max_seq_len + kv_cache_pad`。
- `IDX_SELECTS`（被选中的位置下标）的最后一维 = `index_topk`。
- `Q`（低秩查询）的最后一维 = `q_lora_rank`。
- `KV`（低秩 KV 缓存）的最后一维 = `kv_lora_rank`。
- `KI`（索引 Key）的最后一维 = `index_head_dim`。

也就是说，`ModelArgs` 不是一份「给人看」的配置文档，而是**编译期/装配期喂给张量分配器的尺寸表**。

#### 4.1.3 源码精读

先看 DeepSeek-V3.2 的 `ModelArgs`，它带详尽注释，最适合作为字段字典。类的开头声明了架构名与基本运行参数：

[tilert/models/deepseek_v3_2/model_args.py:L51-L57](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/model_args.py#L51-L57) —— 定义 `arch_name = "deepseek_v3_2"`，以及 `max_batch_size=1`、`max_seq_len=160*1024`、`dtype="fp8"`、`scale_fmt=None`。注意 `max_batch_size=1` 印证了 u1-l1 讲过的「TileRT 面向 bs=1 超低延迟」定位。

规模与 MoE 字段：

[tilert/models/deepseek_v3_2/model_args.py:L58-L72](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/model_args.py#L58-L72) —— `vocab_size`、`dim=7168`、`inter_dim`（稠密 MLP 中间维）、`moe_inter_dim=2048`（每个专家的中间维）、`n_layers=61`、`n_dense_layers=3`、`n_heads=128`；MoE 部分 `n_routed_experts=256`、`n_activated_experts=8`、`n_expert_groups=8`、`n_limited_groups=4`、`score_func="softmax"`、`route_scale=2.5`。

MLA 低秩维度：

[tilert/models/deepseek_v3_2/model_args.py:L74-L78](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/model_args.py#L74-L78) —— `q_lora_rank=1536`、`kv_lora_rank=512`、`qk_nope_head_dim=128`、`qk_rope_head_dim=64`、`v_head_dim=128`。

NSA 稀疏索引与尾部参数：

[tilert/models/deepseek_v3_2/model_args.py:L87-L95](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/model_args.py#L87-L95) —— `index_n_heads=64`、`index_head_dim=128`、`index_topk=2048`、`kv_cache_pad=8`、`block_size=128`、`eps=1e-6`。

> 还有一组 RoPE 长上下文扩展参数 [L80-L85](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/deepseek_v3_2/model_args.py#L80-L85)：`original_seq_len=4096`、`rope_theta=10000.0`、`rope_factor=40`、`beta_fast=32`、`beta_slow=1`、`mscale=1.0`。它们对应 YaRN 式的长序列外推（用 `rope_factor` 把基频缩放、用 `beta_fast/beta_slow` 做高低频不同校正），不在本讲重点，先记住 DSv3.2 开启了长上下文扩展、而 GLM-5 没开（见 4.2）。

接下来是一个**容易踩坑的点**：仓库里有**两份几乎一样的 DeepSeek 基类 `ModelArgs`**。一份是上面这份 `deepseek_v3_2/model_args.py`，被真正的 DeepSeek 模型路径使用；另一份是 GLM-5 包内的副本 `glm_5/_dsa_v32/model_args.py`，专门给 `_dsa_v32` 这套**两模型共享的 DSA 算子**导入用，避免 GLM-5 反向依赖 DeepSeek 包：

[tilert/models/glm_5/_dsa_v32/model_args.py:L15-L21](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/glm_5/_dsa_v32/model_args.py#L15-L21) —— 同样 `arch_name = "deepseek_v3_2"`，字段取值与上一份基本一致，但多了一个 `fp8_kv_cache: bool = False`。这正是 u1-l2 提到的「`glm_5/_dsa_v32` 是复用 DeepSeek 架构的 DSA 算子副本」在配置层的体现——连超参基类都复制了一份。

#### 4.1.4 代码实践

**实践目标**：亲手实例化 `ModelArgs`，验证字段默认值，并计算几个会被多卡切分派生的形状，建立「字段 → 形状」的直觉。

**操作步骤**（在已安装 TileRT wheel 的容器里执行；`ModelArgs` 是纯 dataclass，**不需要 GPU、也不会加载后端**）：

```python
# 示例代码
from tilert.models.deepseek_v3_2.model_args import ModelArgs
from tilert.models.glm_5.model_args import ModelArgsGLM5

ma = ModelArgs()
print("arch_name      :", ma.arch_name)
print("dim            :", ma.dim)
print("n_layers       :", ma.n_layers, "(dense:", ma.n_dense_layers, "moe:", ma.n_layers - ma.n_dense_layers, ")")
print("n_heads        :", ma.n_heads)

num_devices = 8
# 每个 MoE 专家中间维在 8 卡间均分
print("moe_inter_dim /device:", ma.moe_inter_dim // num_devices)
# 非首卡的本地头数（粗略：头数均分）
print("n_heads   /device:", ma.n_heads // num_devices)
# 词表在 8 卡间均分（lm_head 列切）
print("vocab_size/device:", ma.vocab_size // num_devices)
```

**需要观察的现象**：`arch_name` 为 `deepseek_v3_2`；61 层中 dense 3 层、MoE 58 层；`moe_inter_dim // 8 = 256`，`n_heads // 8 = 16`，`vocab_size // 8 = 16160`。

**预期结果**：你能看到「全局字段」是如何被 `// num_devices` 切成「每卡字段」的——这正是权重转换器（u1-l6）做 `device_sharding` 的依据。

> 若你的环境里 `import tilert` 因为缺 torch 等依赖失败，可改为直接打开两个 `model_args.py` 文件人工核对字段值，不影响本实践目标。

#### 4.1.5 小练习与答案

**练习 1**：DSv3.2 的 `kv_lora_rank=512`、`qk_rope_head_dim=64`。一次前向中，KV 缓存里**每个 token** 大约要存多少个 bf16 元素（忽略 pad）？

**参考答案**：KV 缓存存的是低秩压缩向量，每 token 存一份 K 压缩与对应的 RoPE 解耦部分。按本仓库 MLA 的设计，每 token 的 KV 缓存维度为 `kv_lora_rank + qk_rope_head_dim = 512 + 64 = 576` 个元素（bf16 即 1152 字节）。这正是 MLA 相比传统 MHA 显著省显存的原因。

**练习 2**：`n_dense_layers=3`、`n_layers=61`。如果有人想做一个「全部用稠密 MLP」的缩小版，应该改哪个字段、改成多少？

**参考答案**：把 `n_dense_layers` 改成等于 `n_layers`（即 61）。层循环里 `if layer_idx < n_dense_layers` 才会走 `MlpBlock`，否则走 `MoeBlock`（见 u2-l4）；让 `n_dense_layers == n_layers` 即可让所有层都走稠密路径。

---

### 4.2 GLM-5 与 DSv3.2 的关键差异

#### 4.2.1 概念说明

GLM-5 在 TileRT 里并不是一套从零写的独立算子，而是**复用了 DeepSeek 架构的 DSA 算子骨架**（即 `glm_5/_dsa_v32/` 下那份副本），只通过两个手段表达自己的差异：

1. **继承 + 覆盖**：`ModelArgsGLM5` 继承 `_dsa_v32` 的 `ModelArgs`，只覆盖需要变的字段。
2. **运行时按 `arch_name` 分支**：共享算子内部读到 `arch_name == "glm_5"` 时，走不同的权重打包方式或计算核。

这是一种典型的「**一套代码、两套配置**」设计：算子结构（哪些子算子串联、权重别名怎么命名）两模型一致，所以可以共享；而维度、路由、数值细节由 `ModelArgs` 与 `arch_name` 分支拉开差异。

#### 4.2.2 核心流程

`ModelArgsGLM5` 只需 `from ... import ModelArgs` 再覆盖差异字段：

[tilert/models/glm_5/model_args.py:L6-L14](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/glm_5/model_args.py#L6-L14) —— 第 6 行 `from tilert.models.glm_5._dsa_v32.model_args import ModelArgs`，第 14 行 `class ModelArgsGLM5(ModelArgs)`。继承关系一目了然：GLM-5 的超参基类就是那份 DeepSeek 副本。

被覆盖的关键字段可以分四组理解：

- **架构标识**：`arch_name = "glm_5"`（这是触发所有运行时分发的钥匙，见 4.3）。
- **规模与注意力维度更大或不同**：`dim=6148`（< 7168）、`n_layers=78`（> 61）、`n_heads=64`（< 128）；但 `qk_nope_head_dim=192`（> 128）、`v_head_dim=256`（> 128）、`q_lora_rank=2048`（> 1536）——GLM-5 层数更多、单头维度更大，但总头数更少。
- **MoE 路由方式不同**：`score_func="sigmoid"`（DSv3.2 是 `"softmax"`）、`n_expert_groups=1`、`n_limited_groups=1`（DSv3.2 分别是 8、4）。
- **RoPE 完全不同**：`rope_theta=1000000.0`（DSv3.2 是 `10000.0`），且 `original_seq_len=None`、`rope_factor=None`、`beta_fast=None`、`beta_slow=None`——GLM-5 **不启用** YaRN 长上下文扩展，靠超大 `rope_theta` 原生支持长序列；`max_seq_len=202752`（> DSv3.2 的 163840）。

注意有些字段两模型**故意保持一致**：`moe_inter_dim=2048`、`kv_lora_rank=512`、`qk_rope_head_dim=64`、`index_head_dim=128`、`index_topk=2048`、`route_scale=2.5`、`n_routed_experts=256`、`n_activated_experts=8`、`n_shared_experts=1`。这些「不变量」正是两模型能共享同一套算子骨架的基础。

#### 4.2.3 源码精读

GLM-5 的全部覆盖字段集中在很短一段里：

[tilert/models/glm_5/model_args.py:L17-L59](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/glm_5/model_args.py#L17-L59) —— 逐行覆盖了 `arch_name`、`max_seq_len`、`vocab_size`、`dim`、`inter_dim`、`n_layers`、`n_heads`、MoE 分组与 `score_func`、MLA 维度、RoPE、`index_n_heads=32`、`eps=1e-5` 等。

两个值得单独点出的细节：

- 第 37 行 `score_func: Literal["softmax", "sigmoid"] = "sigmoid"`：类型注解甚至比基类更窄（基类还允许 `"sqrtsoftplus"`），把 GLM-5 不支持的路由函数在类型层就排除掉。
- 第 53 行 `index_n_heads: int = 32`（DSv3.2 是 64）：GLM-5 的 NSA 索引头数量减半，但 `index_topk` 仍是 2048（见 4.2.4）。

#### 4.2.4 代码实践

**实践目标**：用程序自动对比两个 `model_args`，产出差异表；并用自己的话解释 `index_topk=2048` 与 `max_seq_len` 的关系。

**操作步骤**：

```python
# 示例代码：自动生成 DSv3.2 vs GLM-5 差异表
from dataclasses import asdict
from tilert.models.deepseek_v3_2.model_args import ModelArgs
from tilert.models.glm_5.model_args import ModelArgsGLM5

dsv = asdict(ModelArgs())      # 注意：arch_name 是类属性，asdict 不含它
glm = asdict(ModelArgsGLM5())
dsv["arch_name"] = ModelArgs.arch_name
glm["arch_name"] = ModelArgsGLM5.arch_name

keys = ["dim", "n_layers", "n_heads", "qk_nope_head_dim", "v_head_dim",
        "score_func", "rope_theta", "max_seq_len", "index_n_heads", "index_topk"]
print(f"{'field':<18}{'DeepSeek-V3.2':<16}{'GLM-5':<16}")
for k in keys:
    print(f"{k:<18}{str(dsv[k]):<16}{str(glm[k]):<16}")
```

**需要观察的现象 / 预期结果**：你会得到如下对比表（关键行）：

| 字段 | DeepSeek-V3.2 | GLM-5 |
| --- | --- | --- |
| `dim` | 7168 | 6144 |
| `n_layers` | 61 | 78 |
| `n_heads` | 128 | 64 |
| `qk_nope_head_dim` | 128 | 192 |
| `v_head_dim` | 128 | 256 |
| `score_func` | softmax | sigmoid |
| `rope_theta` | 10000.0 | 1000000.0 |
| `max_seq_len` | 163840 | 202752 |
| `index_n_heads` | 64 | 32 |
| `index_topk` | 2048 | 2048 |

**解释 `index_topk=2048` 与 `max_seq_len` 的关系**：

NSA 的两步走可以这样看（以 GLM-5 的 temp_vars 为例）：

1. **打分**：先用索引头对**所有历史位置**打分，得分张量 `IDX_LOGITS` 的最后一维是 `max_seq_len + kv_cache_pad`（见 [dsa.py:L159-L161](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/glm_5/modules/dsa.py#L159-L161)）。
2. **选 top-k**：从这些得分里挑出最高的 `index_topk=2048` 个位置，把它们的下标写入 `IDX_SELECTS`，形状最后一维就是 `index_topk`（见 [dsa.py:L162](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/glm_5/modules/dsa.py#L162)）。
3. **稀疏注意力**：真正算注意力时，只对这 2048 个被选中的位置算，而不是全部 `max_seq_len` 个位置。

因此：

- 约束上，`index_topk` 必须 \(\leq\) `max_seq_len`——你不可能从不到 2048 个位置里选出 2048 个。本例两模型都满足（\(2048 \leq 163840\) 与 \(2048 \leq 202752\)）。短序列时有效 topk 会被实际长度截断。
- 意义上，正因为注意力开销被 `index_topk` 钉死在约 2k，序列从 4k 涨到 160k，单步注意力代价**不随之线性增长**，这是 TileRT 能在 160k+ 上下文仍保持低 TPOT 的关键之一。

一个补充观察：接收 0 卡广播的稀疏选择结果用的缓冲区大小是 `max_seq_len * topk * 2`（见 [dsa.py:L49-L51](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/glm_5/modules/dsa.py#L49-L51)），说明 `topk` 还会出现在多卡通信缓冲的容量计算里——改它会影响通信显存。

#### 4.2.5 小练习与答案

**练习 1**：GLM-5 的 `score_func="sigmoid"`，DSv3.2 的 `score_func="softmax"`。这两种路由评分函数在数学上和工程上各有什么差别？

**参考答案**：`softmax` 对所有候选专家的得分做归一化，得分互相挤压，选出的专家概率之和为 1；适合「在固定专家池里选 top-k 且希望概率可比」的场景。`sigmoid` 对每个专家独立做 \(1/(1+e^{-x})\)，得分互不影响、不归一化，更适合「每个专家独立决定是否被激活」的稀疏路由。工程上两者还配合 `route_scale=2.5` 做温度缩放。GLM-5 选 sigmoid 与它 `n_expert_groups=1`（不分组限流）的路由策略是配套的。

**练习 2**：GLM-5 把 `rope_theta` 从 10000 提到 1000000，同时又把 `rope_factor`/`beta_fast`/`beta_slow` 都设成 `None`。这说明 GLM-5 用什么策略支持长上下文？

**参考答案**：GLM-5 不用 YaRN 式的外推校正（那些 `None` 就意味着关闭），而是直接用一个**非常大的 RoPE 基频** `rope_theta=1e6`。基频越大，位置编码的频率越低、相邻位置区分度变化更平缓，从而让模型在训练长度内就天然覆盖很长的相对距离，不需要推理时再做缩放校正。

---

### 4.3 arch_name 在分发中的作用

#### 4.3.1 概念说明

`arch_name` 是 `ModelArgs` 上一个**类属性**字符串（`"deepseek_v3_2"` 或 `"glm_5"`），它不参与张量形状计算，却是整个系统的**分发键**。它在三个层面被用到：

1. **加载层**：CLI/Generator 根据 `model_type` 决定加载哪个 `.so` 后端（见 u1-l3 的 `load_backend`）。这是「进程级」的选择。
2. **算子算法白名单**：每个算子类用 `_SUPPORTED_ALGORITHMS`（一个以 `arch_name` 为键的字典）声明「本架构支持哪些融合算法」，`set_algorithm` 会据此校验。
3. **算子内部分支**：共享算子在初始化和权重转换时，按 `arch_name` 走不同代码路径（不同计算核、不同权重打包）。

第 2、3 层正是「一套算子代码服务两个模型」的实现机制，也是本模块重点。

#### 4.3.2 核心流程

算子算法校验的链路：

1. 每个算子类定义类变量 `_SUPPORTED_ALGORITHMS = {"deepseek_v3_2": [...], "glm_5": [...]}`。
2. 调用 `set_algorithm(algo)` 时，[base.py](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/base.py) 先取 `arch = self.model_args.arch_name`，再 `get_supported_algorithms(arch)` 查表，若 `algo` 不在表里就抛 `ValueError`。
3. 算子初始化时也读 `self.model_args.arch_name`，据此选 `compute_kernel_type`（如 GLM-5 用 `fp8mma_68cta`，DSv3.2 用 `fp8mma`）。
4. 权重转换器同样读 `arch_name`，在 `convert_dsv32(...)` 与 `convert_glm5_68cta(...)` 之间二选一。

这样，`arch_name` 就像算子内部的一个「开关寄存器」：同一份 Python 代码，配 `ModelArgs()`（arch=deepseek_v3_2）跑 DeepSeek 行为，配 `ModelArgsGLM5()`（arch=glm_5）跑 GLM 行为。

#### 4.3.3 源码精读

算法白名单校验逻辑（base.py）：

[tilert/models/base.py:L56-L64](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/base.py#L56-L64) —— `get_supported_algorithms(arch_name)` 直接用 `arch_name` 作为键去查 `cls._SUPPORTED_ALGORITHMS`，键不存在就报错并列出支持的架构。

[tilert/models/base.py:L140-L148](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/base.py#L140-L148) —— `set_algorithm` 里 `arch = self.model_args.arch_name`，再校验 `algorithm` 是否属于该 arch 的支持列表。这就是「字段 → 行为」的精确落点：同一个 `arch_name` 字段串起了 ModelArgs 与算子算法校验。

一个典型共享算子的 `_SUPPORTED_ALGORITHMS` 表与 `arch_name` 分支：

[tilert/models/glm_5/_dsa_v32/ops/rmsnorm_projx_wqakis.py:L102-L121](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/glm_5/_dsa_v32/ops/rmsnorm_projx_wqakis.py#L102-L121) —— `RMSNormProjxWqakisAlgorithm` 枚举有 `FP8MMA`、`W8A16HMMA` 两个成员；`_SUPPORTED_ALGORITHMS` 同时列出 `"deepseek_v3_2"` 与 `"glm_5"` 两个键，各自允许这两个算法。这正是「共享算子」的声明：两种架构都受支持。

算子初始化里按 `arch_name` 选计算核：

[tilert/models/glm_5/_dsa_v32/ops/rmsnorm_projx_wqakis.py:L170-L173](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/glm_5/_dsa_v32/ops/rmsnorm_projx_wqakis.py#L170-L173) —— `if self.arch_name == "glm_5": self.compute_kernel_type = "fp8mma_68cta"`，否则用 `"fp8mma"`。同一个算子类，GLM-5 走 68-CTA 的 FP8 MMA 核，DSv3.2 走普通 FP8 MMA 核。

权重转换器里按 `arch_name` 选打包方式：

[tilert/models/glm_5/_dsa_v32/ops/rmsnorm_projx_wqakis.py:L32-L44](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/glm_5/_dsa_v32/ops/rmsnorm_projx_wqakis.py#L32-L44) —— `convert_to_decoupled` 里读 `arch_name`，DSv3.2 调 `ProjxWqakiWeightsConverter.convert_dsv32(...)`，GLM-5 调 `convert_glm5_68cta(...)`，未知架构抛 `ValueError`。这是离线权重转换（u1-l6）能服务两套模型的根因。

#### 4.3.4 代码实践

**实践目标**：用一个最小自造的算子骨架，亲手体验 `arch_name` 如何驱动 `_SUPPORTED_ALGORITHMS` 查表与校验，不依赖 GPU、不加载后端。

**操作步骤**：

```python
# 示例代码：模拟 arch_name 分发（不依赖 tilert 后端）
from enum import Enum

class Algo(Enum):
    FP8MMA = "fp8mma"
    W8A16HMMA = "w8a16_hmma"

class FakeOp:
    # 模拟 rmsnorm_projx_wqakis 的支持表
    _SUPPORTED_ALGORITHMS = {
        "deepseek_v3_2": [Algo.FP8MMA, Algo.W8A16HMMA],
        "glm_5":         [Algo.FP8MMA, Algo.W8A16HMMA],
    }

    def __init__(self, arch_name):
        self.arch_name = arch_name
        self.algorithm = None

    def set_algorithm(self, algo):
        supported = self._SUPPORTED_ALGORITHMS[self.arch_name]   # 对应 base.py get_supported_algorithms
        if algo not in supported:
            raise ValueError(f"{algo} not supported for arch '{self.arch_name}'")
        self.algorithm = algo

for arch in ["deepseek_v3_2", "glm_5"]:
    op = FakeOp(arch)
    op.set_algorithm(Algo.FP8MMA)            # 两个架构都支持，应通过
    print(arch, "->", op.algorithm)

# 演示白名单拒绝：构造一个只支持 glm_5 新算法的表
class StrictOp(FakeOp):
    _SUPPORTED_ALGORITHMS = {"glm_5": [Algo.W8A16HMMA]}   # deepseek_v3_2 不在键里

try:
    StrictOp("deepseek_v3_2").set_algorithm(Algo.W8A16HMMA)
except ValueError as e:
    print("rejected:", e)
```

**需要观察的现象**：前两个 `set_algorithm` 调用成功，打印两个架构的算法；最后一段抛 `ValueError`，提示 `deepseek_v3_2` 不在支持表里。

**预期结果**：你直观看到 `arch_name` 就是一张查找表的键——配什么 `model_args`，就走什么算法集合与分支。把 `FakeOp` 换成真实的 `RMSNormProjxWqakis`，逻辑完全一致（见 4.3.3 引用的源码）。

#### 4.3.5 小练习与答案

**练习 1**：如果把 `RMSNormProjxWqakis._SUPPORTED_ALGORITHMS` 里 `"glm_5"` 这个键删掉，但仍然用 `ModelArgsGLM5()` 去构造并 `set_algorithm`，会发生什么？

**参考答案**：`get_supported_algorithms("glm_5")` 会在 [base.py:L59-L63](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/base.py#L59-L63) 抛 `ValueError`，提示该算子不支持 `glm_5`、并列出当前支持的架构。这说明「共享算子」必须**显式声明**它支持哪些架构，声明遗漏会被校验拦截，而不是静默跑错。

**练习 2**：为什么 `arch_name` 要做成 `ModelArgs` 的**类属性**（`arch_name = "deepseek_v3_2"`），而不是带类型的实例字段（`arch_name: str = "deepseek_v3_2"`）？

**参考答案**：作为类属性，它在子类（如 `ModelArgsGLM5`）里被同名覆盖时，是**改了类的身份标识**，语义上更接近「这个配置类代表哪个架构」的常量；而实例字段会鼓励运行时去改它，容易和算子里基于它做的静态分发（`_SUPPORTED_ALGORITHMS` 查表、`convert_dsv32`/`convert_glm5_68cta` 分支）产生不一致。把它设成不可变的类属性，是在表达「架构是配置类的固有属性，不应被实例随意修改」。

## 5. 综合实践

把本讲三个模块串起来，完成一个「**用 ModelArgs 推导一个真实 temp_var 的形状，并说明它为何在两模型下不同**」的小任务。

1. 打开 [tilert/models/glm_5/modules/dsa.py:L149-L162](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/models/glm_5/modules/dsa.py#L149-L162)，挑出 `Q`、`KV`、`KI`、`IDX_LOGITS`、`IDX_SELECTS` 五个 temp_var。
2. 对每个槽，写出它最后一维来自 `ModelArgs` 的哪个字段（答案：`q_lora_rank`、`kv_lora_rank`、`index_head_dim`、`max_seq_len+kv_cache_pad`、`index_topk`）。
3. 分别代入 `ModelArgs()` 与 `ModelArgsGLM5()` 的取值，算出 GLM-5 下这些维度各是多少（例如 `Q` 最后一维：DSv3.2=1536，GLM-5=2048；`IDX_LOGITS` 最后一维：DSv3.2=163848，GLM-5=202760）。
4. 最后回答：**如果有人想新增第三个模型（比如 arch_name="glm_5_1"），需要改动哪几处？** 参考答案：(a) 新建一个继承 `ModelArgs` 的子类，覆盖差异字段并设 `arch_name`；(b) 在 `tilert/__init__.py` 的 `_BACKENDS` 字典里注册新 `.so`（见 u1-l2）；(c) 在每个要共享的算子的 `_SUPPORTED_ALGORITHMS` 里加这个新键，并在算子 `__init__`/转换器的 `arch_name` 分支里补上新路径——这正是 `arch_name` 作为分发键的全部落点。

## 6. 本讲小结

- `ModelArgs` 是模型超参的**单一事实来源**，带默认值可无参构造；它的字段直接决定 temp_vars、KV 缓存、权重分片等几十个张量的形状。
- 仓库里有**两份几乎相同的 DeepSeek 基类 `ModelArgs`**（`deepseek_v3_2/` 与 `glm_5/_dsa_v32/`），后者是给共享算子用的副本，体现「GLM-5 复用 DeepSeek 架构骨架」。
- `ModelArgsGLM5` 用**继承 + 覆盖**表达差异：层数更多、注意力单头维度更大但总头数更少、MoE 路由用 sigmoid、RoPE 用超大基频且不启用 YaRN 外推。
- 两模型保持 `index_topk=2048`、`moe_inter_dim=2048`、`kv_lora_rank=512` 等不变量，是共享算子骨架的基础。
- `index_topk` 是 NSA 稀疏注意力最终参与计算的位置数（\(\leq\) `max_seq_len`），它把长上下文注意力开销钉在约 2k，不随 `max_seq_len` 线性增长。
- `arch_name` 是分发键，在「算法白名单校验」「算子初始化选核」「权重转换分支」三处被读取，是「一套代码、两套配置」的关键。

## 7. 下一步学习建议

本讲只讲了**配置层**。接下来应当看这些字段如何驱动**装配层**与**执行层**：

- **u2-l3（ShowHandsDSALayer）**：看 `ModelArgs` 如何驱动 8 卡多线程权重加载与 `prepare_money` 把张量交给后端。
- **u2-l4（DSA 层组装）**：看 `n_layers`/`n_dense_layers` 如何变成 `register_op` 的层循环与键名前缀。
- **u2-l5（三层张量执行契约）**：把本讲 4.1.2 里提到的 temp_vars 形状推导，放到完整的 params/temp_vars/caches 契约里去理解。
- **u2-l6（MLA 与稀疏选择）**：深入本讲提到的 NSA 稀疏索引在 0 卡与其余卡之间的分工。

建议在进入 u2-l3 前，先把本讲的综合实践做完——能熟练地从 `ModelArgs` 字段推出 temp_var 形状，是读懂后续所有 modules 代码的前提。
