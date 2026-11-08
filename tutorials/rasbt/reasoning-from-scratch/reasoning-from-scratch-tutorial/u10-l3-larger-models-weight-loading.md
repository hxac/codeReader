# u10-l3 加载更大的 Qwen3 模型与权重映射

## 1. 本讲目标

第 2~8 章的所有实验都跑在 Qwen3 0.6B 上——它是 Qwen3 家族里最小的成员，也是唯一一个本书作者**重新打包成单个 `.pth` 文件**发布的模型。本讲解决的问题是：当你想换用官方发布的更大模型（1.7B、4B、8B、14B、32B）时，如何把它们从 Hugging Face 的 `safetensors` checkpoint 加载进本仓库从零实现的 `Qwen3Model`。

学完本讲，你应该能够：

1. 说出 `appendix_c.py` 中五个大模型配置字典与 0.6B 配置的差异维度（哪些键变了、哪些键不变），并根据配置粗算参数量与 bf16 显存下限。
2. 手工写出 HF checkpoint 的张量名（如 `model.layers.0.self_attn.q_proj.weight`）到 from-scratch 模型属性路径（如 `model.trf_blocks[0].att.W_query.weight`）的完整映射表。
3. 区分本仓库的两套权重加载器：`appendix_c.load_weights_into_qwen` 与 `qwen3.load_hf_weights_into_qwen`，并解释后者的 MoE（`num_experts`）分支与 `lm_head` 权重绑定分支分别处理什么情况。
4. 使用 `download_from_huggingface_from_snapshots` 下载并读取（含多分片的）safetensors 权重，独立走通「下载 → 实例化 → 灌权重 → 分词器 → 生成」全流程。

## 2. 前置知识

本讲是高级专题，假设你已完成单元二（模型架构）与 u8-l5（checkpoint 与 state_dict）。以下概念用通俗语言再过一遍：

- **state_dict**：PyTorch 把模型所有可学习参数按「属性路径字符串 → 张量」存进的一个字典。`model.load_state_dict(d)` 要求 `d` 的键名与模型属性路径**逐字相同**。u2-l1 讲过，本仓库 0.6B 的 `.pth` 就是本仓库自己的 state_dict，所以 chC 里可以一行 `model.load_state_dict(torch.load(...))` 完成加载。
- **HF checkpoint 命名**：Hugging Face `transformers` 库的官方 Qwen3 权重用的却是**另一套键名**（`q_proj`、`gate_proj`、`input_layernorm`……）。两套命名指向同一组数学运算，但字符串对不上，`load_state_dict` 直接报 missing/unexpected keys——这就是本讲「权重映射」函数存在的理由。
- **safetensors 与分片（shard）**：safetensors 是一种「先存元数据再存原始张量字节」的安全序列化格式，加载时不执行任意代码、且可以按需读取单个张量。大模型权重常被切成多个分片文件（`model-00001-of-00002.safetensors` 等），另配一个索引文件 `model.safetensors.index.json` 记录「每个张量住在哪个分片里」。u9-l5 讲过导出侧的 safetensors，本讲从加载侧再遇到它。
- **稠密（dense）与稀疏 MoE（Mixture of Experts）**：本讲涉及的五个大模型都是**稠密**模型——每层前馈网络只有一组 `fc1/fc2/fc3` 权重，每个 token 都要走它。MoE 模型则把前馈换成「一个路由器（router/gate）+ N 个专家（experts）」，每个 token 只激活少数专家。Qwen3 家族里的 30B-A3B 就是 MoE；本讲会读到为它预留的加载分支，但本书代码不含 MoE 前馈实现。
- **权重绑定（weight tying）**：让输出投影 `out_head` 直接复用词嵌入 `tok_emb` 的权重（词表 × 隐藏宽的大矩阵只存一份）。u2-l1 讲过 0.6B 官方口径按绑定计约 5.96 亿参数；本讲会看到加载代码如何「有独立 `lm_head` 就加载、没有就绑定」。
- **显存粗算**：bfloat16 每个参数占 2 字节，因此 \( \text{显存下限} \approx 2 \times \text{参数量} \) 字节。这是「下限」：激活值、临时缓冲、KV cache 都要额外吃显存。

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| [reasoning_from_scratch/appendix_c.py](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/appendix_c.py) | 附录 C 配套模块：五个大模型配置字典、第一套权重映射 `load_weights_into_qwen`、safetensors 下载器 `download_from_huggingface_from_snapshots` |
| [reasoning_from_scratch/qwen3.py](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3.py) | 全书模型文件：`Qwen3Model`（u2-l1 已精读）与本讲主角 `load_hf_weights_into_qwen`（含 MoE 与权重绑定分支） |
| [chD/chD_main.ipynb](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/chD/chD_main.ipynb) | 附录 D 教学流程：下载 4B-Base → 实例化 → 灌权重 → 分词器 → 生成，以及 4B reasoning 变体的切换说明 |
| [chC/01_main-chapter-code/chC_main.ipynb](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/chC/01_main-chapter-code/chC_main.ipynb) | 对照物：0.6B 走 `model.load_state_dict(torch.load(model_file))` 的「同名直装」路线 |
| [tests/test_appendix_c.py](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/tests/test_appendix_c.py) | 从 chC notebook 提取 `Qwen3Model` 定义，用玩具规模配置回归生成结果（注意：它**不测** `appendix_c.py` 的下载与映射函数） |

## 4. 核心概念与源码讲解

本讲的四个最小模块：**五个大模型配置字典**、**权重映射函数**、**MoE 与权重绑定分支**、**safetensors 下载器**。

### 4.1 五个更大的稠密配置：QWEN3_CONFIG_1_7B … 32B

#### 4.1.1 概念说明

u2-l1 的核心结论「配置驱动、结构与尺寸解耦」在本模块得到最直接的验证：`Qwen3Model` 一行代码不改，换一个配置字典就得到一个更大的同构模型。Qwen3 官方发布的稠密基座/推理模型共六个尺寸（0.6B、1.7B、4B、8B、14B、32B），`appendix_c.py` 补齐了除 0.6B 之外的五个。

关键认识是：**从 0.6B 到 32B，变的只有四个「尺寸旋钮」，架构图完全不变。**

- `emb_dim`（隐藏宽）：每层的「通道数」；
- `n_heads`（查询头数）：注意力分头数（头维 `head_dim` 恒为 128）；
- `n_layers`（层数）：Transformer 块的重复次数；
- `hidden_dim`（前馈中间宽）：SwiGLU 的中间层大小。

其余键全部与 0.6B 相同：词表 151,936、上下文 40,960、`n_kv_groups=8`（GQA 的 KV 组数）、`qk_norm=True`、`rope_base=1,000,000`、`dtype=bfloat16`。这也解释了为什么第 2 章起讲的所有机制（GQA、RoPE、QK-Norm、KV cache）在大模型上原样成立。

#### 4.1.2 核心流程

五个配置字典的尺寸演化（每个字典注释里的倍数是相对上一档）：

| 配置 | emb_dim | n_heads | n_layers | hidden_dim | head_dim | n_kv_groups | 粗算参数量（绑定口径） | bf16 权重显存下限 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 0.6B（qwen3.py） | 1024 | 16 | 28 | 3072 | 128 | 8 | ≈ 0.60B | ≈ 1.2 GB |
| 1.7B | 2048 | 16 | 28 | 6144 | 128 | 8 | ≈ 1.72B | ≈ 3.4 GB |
| 4B | 2560 | 32 | 36 | 9728 | 128 | 8 | ≈ 4.02B | ≈ 8 GB |
| 8B | 4096 | 32 | 36 | 12288 | 128 | 8 | ≈ 7.6B（粗算） | ≈ 16 GB |
| 14B | 5120 | 40 | 40 | 17408 | 128 | 8 | — | ≈ 28 GB |
| 32B | 5120 | 64 | 64 | 25600 | 128 | 8 | — | ≈ 64 GB |

（后两列的来源见 4.1.4 的实践；14B/32B 的参数量留作练习。）

参数量粗算公式——每个稠密 Transformer 层包含：注意力四个投影（Q、K、V、O）+ SwiGLU 三个线性层 + 若干可忽略的 norm 缩放向量。记 \( d \) 为 `emb_dim`、\( h \) 为 `hidden_dim`、\( n_q \) 为查询头数、\( n_{kv} \) 为 KV 组数、\( d_{\text{head}} \) 为头维（128）、\( L \) 为层数、\( V \) 为词表大小：

\[ N_{\text{layer}} = \underbrace{d \cdot n_q d_{\text{head}}}_{W\_query} + \underbrace{2 \cdot d \cdot n_{kv} d_{\text{head}}}_{W\_key + W\_value} + \underbrace{n_q d_{\text{head}} \cdot d}_{out\_proj} + \underbrace{3\, d\, h}_{fc1 + fc2 + fc3} \]

\[ N_{\text{total}} \approx L \cdot N_{\text{layer}} + \underbrace{V \cdot d}_{\text{tok\_emb（绑定时只算一次）}} \]

注意 \( L \cdot 3dh \)（前馈）通常占大头，且 GQA 让 K/V 投影只有 Q 投影的 \( n_{kv}/n_q \) 宽。

#### 4.1.3 源码精读

五个字典定义在 appendix_c.py 顶部，先看 1.7B 与 4B 两个代表：

- [reasoning_from_scratch/appendix_c.py:L11-L23](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/appendix_c.py#L11-L23)——1.7B 配置：相对 0.6B 只把 `emb_dim` 从 1024 翻到 2048、`hidden_dim` 从 3072 翻到 6144，头数（16）与层数（28）都不变。也就是说 **1.7B 是「加宽不加深」**：每层参数约翻两番，层数不变。
- [reasoning_from_scratch/appendix_c.py:L26-L38](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/appendix_c.py#L26-L38)——4B 配置：`emb_dim` 2560、`n_heads` 翻倍到 32、`n_layers` 增到 36、`hidden_dim` 9728，四个旋钮同时上调。
- [reasoning_from_scratch/appendix_c.py:L41-L53](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/appendix_c.py#L41-L53) 与 [reasoning_from_scratch/appendix_c.py:L56-L68](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/appendix_c.py#L56-L68)——8B 与 14B：继续单调上调 `emb_dim`/`n_layers`/`hidden_dim`。
- [reasoning_from_scratch/appendix_c.py:L70-L82](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/appendix_c.py#L70-L82)——32B 配置：`emb_dim` 与 14B 同为 5120，但 `n_heads` 增到 64、`n_layers` 增到 64、`hidden_dim` 增到 25600，是「加深为主」的一档。
- 全部字典中恒定不变的键（对照 [reasoning_from_scratch/qwen3.py:L15-L27](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3.py#L15-L27) 的 0.6B 配置）：`vocab_size=151_936`、`context_length=40_960`、`head_dim=128`、`n_kv_groups=8`、`qk_norm=True`、`rope_base=1_000_000.0`、`dtype=torch.bfloat16`。

chD notebook 用一张 markdown 表格汇总了同样的对应关系（模型尺寸 → 字典名），见 [chD/chD_main.ipynb:L93-L101](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/chD/chD_main.ipynb#L93-L101)（链接行号为 notebook 的 JSON 源码行号）。它在正文中同时强调两件事：这五个都是**可在单卡上跑的稠密模型**；MoE 变体不在本书代码支持范围内（若想要从零实现的 MoE，作者指向其 LLMs-from-scratch 仓库的 qwen3 MoE 章节）。

显存下限表来自 [chD/chD_main.ipynb:L165-L181](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/chD/chD_main.ipynb#L165-L181) 附近 D.2 节的说明：bf16 每参数约 2 字节，1.7B ≈ 3.4 GB、4B ≈ 8 GB、8B ≈ 16 GB、14B ≈ 28 GB、32B ≈ 64 GB；且实际运行还需额外内存（激活、缓冲、KV cache）。

#### 4.1.4 代码实践

**实践目标**：用配置字典粗算参数量，验证「配置 → 参数规模」的因果关系，并核对公开口径。

1. 阅读上面的公式，先**手算 1.7B**：每层 \( 3 \times 2048 \times 6144 = 37.7\text{M} \)（前馈）+ \( 2048 \times 2048 + 2 \times 2048 \times 1024 + 2048 \times 2048 = 12.6\text{M} \)（注意力投影）≈ 50.3M；乘 28 层 ≈ 1.409B；加词嵌入 \( 151936 \times 2048 \approx 0.311\text{B} \)（绑定口径只算一次）≈ **1.72B** ✓。
2. 再写脚本（示例代码，非项目原有文件）算三个尺寸并打印：

   ```python
   # 示例代码：参数量粗算（不含 norm 缩放向量等小项）
   import torch
   from reasoning_from_scratch.appendix_c import (
       QWEN3_CONFIG_1_7B, QWEN3_CONFIG_4B,
   )
   from reasoning_from_scratch.qwen3 import QWEN_CONFIG_06_B

   def estimate_params(cfg):
       d, h = cfg["emb_dim"], cfg["hidden_dim"]
       n_q, n_kv, dh = cfg["n_heads"], cfg["n_kv_groups"], cfg["head_dim"]
       attn = d*n_q*dh + 2*d*n_kv*dh + n_q*dh*d   # Q + K + V + O 投影
       ff = 3 * d * h                              # fc1 + fc2 + fc3
       layer = attn + ff
       total = cfg["n_layers"] * layer + cfg["vocab_size"] * d  # 绑定：emb 只算一次
       return layer, total

   for name, cfg in [("0.6B", QWEN_CONFIG_06_B),
                     ("1.7B", QWEN3_CONFIG_1_7B),
                     ("4B",  QWEN3_CONFIG_4B)]:
       layer, total = estimate_params(cfg)
       print(f"{name}: 每层 {layer/1e6:.1f}M, 总计(绑定) {total/1e9:.2f}B, "
             f"bf16 下限 {2*total/2**30:.1f} GiB")
   ```

3. 把输出与公开的参数规模核对：0.6B ≈ 0.60B（官方绑定口径 5.96 亿，u2-l1 已算过）、1.7B ≈ 1.72B、4B ≈ 4.0B。
4. 顺手做一个**实证对照**（可选，无需下载大权重）：`Qwen3Model(dict(QWEN3_CONFIG_4B, dtype=torch.float32))` 会实例化一个 4B 模型对象——**不要在内存紧张的机器上跑**；若跑了，可用 `sum(p.numel() for p in model.parameters())/1e9` 打印真实参数量，注意它含独立 `out_head`，会比绑定口径多出约 \( V \times d \approx 0.39\text{B} \)。

**需要观察的现象**：粗算值与公开规模在三位有效数字内吻合；`out_head` 是否独立计入会让 4B 模型的 `numel()` 在 4.02B 与 4.41B 之间跳动。

**预期结果**：0.6B → ≈0.60B；1.7B → ≈1.72B；4B → ≈4.02B；对应 bf16 权重显存下限 ≈ 1.2 / 3.4 / 8 GB。（步骤 4 的完整实例化在小于 20 GB 内存的机器上可能触发 swap，建议只跑粗算脚本。）

#### 4.1.5 小练习与答案

**练习 1**：8B 配置的 `emb_dim=4096`、`hidden_dim=12288`、`n_layers=36`、`n_heads=32`。粗算它的参数量，并解释为什么它比「4B 的两倍」略小。

**答案**：每层前馈 \( 3 \times 4096 \times 12288 = 151.0\text{M} \)；注意力投影 \( 4096 \times 4096 + 2 \times 4096 \times 1024 + 4096 \times 4096 = 41.9\text{M} \)；每层 192.9M × 36 层 ≈ 6.95B；加词嵌入 \( 151936 \times 4096 \approx 0.62\text{B} \) ≈ **7.57B**。「4B 的两倍」应为 8.04B，8B 略小是因为词嵌入只随 `emb_dim` 线性增长一次（0.39B → 0.62B），而层数只从 36 保持不变、`hidden_dim/emb_dim` 比例从 3.8 降到 3.0。（公开口径常报 8.2B，含独立输出头等统计差异；粗算的目的是量级校验而非精确对账。）

**练习 2**：为什么五个大模型的 `n_kv_groups` 都是 8，而 `n_heads` 从 16 涨到 64？

**答案**：GQA 中每个 KV 组被若干查询头共享（u2-l3），KV 组数直接决定 KV cache 与 K/V 投影的大小。把它钉在 8 意味着模型变大时 **K/V 投影与 KV cache 只随 `emb_dim` 线性增长**，推理侧的缓存成本可控；推理质量的提升则主要靠增加查询头与加宽加深。32B 时 64 个查询头共享 8 个 KV 组，共享比高达 8:1。

**练习 3**：14B 与 32B 的 `emb_dim` 相同（5120），为什么参数量差约 2.3 倍？

**答案**：两者差异全部来自「加深」：`n_layers` 40→64（×1.6）、`hidden_dim` 17408→25600（×1.47），每层前馈参数近似 ×1.47、层数 ×1.6，合计 ≈ ×2.35；词嵌入部分（\( 151936 \times 5120 \approx 0.78\text{B} \)）完全不变。逐项验算 14B：每层 \( 3 \times 5120 \times 17408 = 267.4\text{M} \)（前馈）+ \( 5120 \times 5120 + 2 \times 5120 \times 1024 + 5120 \times 5120 = 62.9\text{M} \)（注意力投影）≈ 330.3M，×40 层 ≈ 13.2B，加词嵌入 ≈ **14.0B** ✓；同法可得 32B：每层 \( 3 \times 5120 \times 25600 + 62.9\text{M} \times \frac{64}{40} \approx 398.1\text{M} \)，×64 层 ≈ 25.5B + 0.78B ≈ **32.4B**，两者比值 ≈ 2.3。

### 4.2 权重映射：从 HF 张量名到模型属性路径

#### 4.2.1 概念说明

官方 Qwen3 checkpoint 按 `transformers` 库的 `Qwen3ForCausalLM` 命名存放张量，与本仓库 `Qwen3Model` 的属性路径是两套词汇。加载的本质是**按字典查表、逐张量搬运**：

- 数学上同构：`q_proj` 就是 `W_query`，`input_layernorm` 就是 `norm1`，一一对应、无一遗漏；
- 命名上错位：三处系统性差异最容易踩坑——
  1. **RMSNorm 的参数名**：HF 叫 `*.weight`，本仓库的 `RMSNorm` 把它命名为 `scale`（u2-l2）；
  2. **SwiGLU 的三支路**：HF 的 `gate_proj/up_proj/down_proj` 分别对应本仓库的 `fc1/fc2/fc3`，且顺序不可交换——`fc1` 是接 SiLU 的门控支路（对应 `gate_proj`），`fc2` 是线性内容支路（`up_proj`），`fc3` 是降维投影（`down_proj`）；
  3. **命名风格**：HF 用 `model.layers.{l}.xxx` 的扁平字符串定位，本仓库用 `model.trf_blocks[{l}]` 的 ModuleList 索引定位。

本仓库有**两套**实现同一映射的函数，务必分清：

| | `appendix_c.load_weights_into_qwen` | `qwen3.load_hf_weights_into_qwen` |
| --- | --- | --- |
| 定义位置 | appendix_c.py（附录 C 模块） | qwen3.py（全书模型文件） |
| 谁在用 | 仓库内**无调用者**（教学参照实现） | chD notebook 实际使用 |
| `assign` 写法 | 新建 `Parameter`（`clone().detach()`）替换 | `no_grad` + `copy_` 原地拷贝 |
| 输出头 | 无条件权重绑定 | 有 `lm_head.weight` 则加载，否则绑定并打印 |
| MoE | 不支持 | 支持（`num_experts` 分支） |

两者映射规则完全一致，后者是功能更全的演进版。

#### 4.2.2 核心流程

`load_hf_weights_into_qwen(model, param_config, params)` 的执行流程：

```text
1. 定义 assign(left, right)：形状不同 → ValueError；相同 → no_grad 下 copy_ 原地拷贝
2. 灌词嵌入：        model.tok_emb.weight        ← params["model.embed_tokens.weight"]
3. for l in range(n_layers):
     4. 灌注意力四投影： W_query/W_key/W_value/out_proj ← q_proj/k_proj/v_proj/o_proj
     5. 灌 QK-Norm（若模块存在）： q_norm.scale / k_norm.scale ← q_norm.weight / k_norm.weight
     6. 灌 norm1：      block.norm1.scale            ← input_layernorm.weight
     7. 灌前馈：
        若 "num_experts" in param_config（MoE）：
            8a. block.ff.gate.weight          ← mlp.gate.weight（路由器）
            8b. for e in experts: fc1[e]/fc2[e]/fc3[e] ← experts.{e}.gate/up/down_proj，并 .to("cpu")
        否则（稠密）：
            8c. block.ff.fc1/fc2/fc3          ← mlp.gate_proj/up_proj/down_proj
     9. 灌 norm2：      block.norm2.scale            ← post_attention_layernorm.weight
10. 灌 final_norm：     model.final_norm.scale      ← model.norm.weight
11. 输出头：
    若 checkpoint 有 "lm_head.weight"： out_head.weight ← lm_head.weight（独立头）
    否则：                              out_head.weight = tok_emb.weight（权重绑定）
```

#### 4.2.3 源码精读

先看 chD 实际使用的 `load_hf_weights_into_qwen`：

- [reasoning_from_scratch/qwen3.py:L534-L550](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3.py#L534-L550)——函数签名与 `assign` 闭包：docstring 声明「仅在附录 D 中用于加载其他 Qwen3 变体」。`assign` 先做**形状守门**（不匹配立即 `ValueError`，把问题挡在加载期而不是留给运行期算错），再在 `torch.no_grad()` 里 `left.copy_(right)` 原地拷贝——保持 Parameter 对象身份不变，避免优化器状态失联；非张量输入会被 `torch.as_tensor` 按 `left` 的 dtype/device 转换。
- [reasoning_from_scratch/qwen3.py:L552-L578](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3.py#L552-L578)——逐层循环搬运注意力：`W_query ← q_proj`、`W_key ← k_proj`、`W_value ← v_proj`、`out_proj ← o_proj`。注意 K/V 投影天然比 Q 窄（GQA：8 个 KV 组 × 128 头维 = 1024 宽，而 4B 的 Q 是 32 × 128 = 4096 宽），形状守门在这里顺带校验了 GQA 结构。
- [reasoning_from_scratch/qwen3.py:L581-L599](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3.py#L581-L599)——QK-Norm 与第一个 RMSNorm：用 `hasattr(att, "q_norm") and att.q_norm is not None` 双重判断守护，让同一份加载代码兼容 `qk_norm=False` 的配置；`norm1.scale ← input_layernorm.weight` 是「Pre-Norm 在注意力之前」这一结构事实在命名上的体现。
- [reasoning_from_scratch/qwen3.py:L649-L656](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3.py#L649-L656)——`norm2.scale ← post_attention_layernorm.weight` 与最终的 `final_norm.scale ← model.norm.weight`。

再看附录 C 的孪生版本，重点对比 `assign` 与结尾：

- [reasoning_from_scratch/appendix_c.py:L85-L91](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/appendix_c.py#L85-L91)——`load_weights_into_qwen` 的 `assign` 用 `torch.nn.Parameter(right.clone().detach())` **构造新 Parameter 替换旧属性**，而非原地拷贝；随后同样从 `model.embed_tokens.weight` 灌入词嵌入。
- [reasoning_from_scratch/appendix_c.py:L98-L157](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/appendix_c.py#L98-L157)——逐层映射主体与 qwen3.py 版逐行同构：Q/K/V/O 投影、QK-Norm（`att.q_norm.scale ← q_norm.weight`）、`norm1/norm2`、稠密前馈的 `fc1 ← gate_proj`、`fc2 ← up_proj`、`fc3 ← down_proj`。
- [reasoning_from_scratch/appendix_c.py:L164-L168](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/appendix_c.py#L164-L168)——结尾：`final_norm.scale ← model.norm.weight`，然后**无条件**执行 `model.out_head.weight = model.tok_emb.weight` 复用嵌入权重（注释明确写着「模型使用权重绑定，故此处复用嵌入层权重」），没有 `lm_head` 探测分支。

chD notebook 中的实际调用（[chD/chD_main.ipynb:L296-L320](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/chD/chD_main.ipynb#L296-L320)，D.3 节）：

```python
model = Qwen3Model(QWEN3_CONFIG_4B)
load_hf_weights_into_qwen(
    model,
    param_config={
        "n_layers": QWEN3_CONFIG_4B["n_layers"],
        "hidden_dim": QWEN3_CONFIG_4B["hidden_dim"],
    },
    params=weights,
)
model.to(device)
model.eval()
```

一个值得注意的细节：这里传入的 `param_config` **不是完整配置字典**，只是一个临时拼的小字典。对照函数体可知，`load_hf_weights_into_qwen` 实际只消费 `param_config["n_layers"]`（与可选的 `num_experts`）；`hidden_dim` 传了但并未被使用——模型结构已由 `Qwen3Model(QWEN3_CONFIG_4B)` 决定，加载函数只负责「按层号与名字搬家」。加载完成后 notebook 打印的模型结构（36 层、`W_query: 2560→4096`、`W_key/W_value: 2560→1024`、`fc1/fc2: 2560→9728`、`fc3: 9728→2560`）正是 4.1 节配置表的实例化。

#### 4.2.4 代码实践

**实践目标**：亲手制作「HF 张量名 → from-scratch 属性路径」映射表，并验证每条映射都能在源码中找到对应行。

1. 打开 [reasoning_from_scratch/qwen3.py:L550-L656](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3.py#L550-L656)，把下表逐行对照源码填完（左列是 checkpoint 里出现的张量名模板，`{l}` 为层号）：

   | # | HF checkpoint 张量名 | from-scratch 属性路径 | 语义 |
   | --- | --- | --- | --- |
   | 1 | `model.embed_tokens.weight` | `model.tok_emb.weight` | 词嵌入（全局，一份） |
   | 2 | `model.layers.{l}.self_attn.q_proj.weight` | `model.trf_blocks[{l}].att.W_query.weight` | Q 投影（宽 \( n_q \cdot 128 \)） |
   | 3 | `model.layers.{l}.self_attn.k_proj.weight` | `model.trf_blocks[{l}].att.W_key.weight` | K 投影（宽 \( n_{kv} \cdot 128 \)） |
   | 4 | `model.layers.{l}.self_attn.v_proj.weight` | `model.trf_blocks[{l}].att.W_value.weight` | V 投影（宽 \( n_{kv} \cdot 128 \)） |
   | 5 | `model.layers.{l}.self_attn.o_proj.weight` | `model.trf_blocks[{l}].att.out_proj.weight` | 注意力输出投影 |
   | 6 | `model.layers.{l}.self_attn.q_norm.weight` | `model.trf_blocks[{l}].att.q_norm.scale` | QK-Norm 的 Q 缩放 |
   | 7 | `model.layers.{l}.self_attn.k_norm.weight` | `model.trf_blocks[{l}].att.k_norm.scale` | QK-Norm 的 K 缩放 |
   | 8 | `model.layers.{l}.input_layernorm.weight` | `model.trf_blocks[{l}].norm1.scale` | 注意力分支前的 RMSNorm |
   | 9 | `model.layers.{l}.post_attention_layernorm.weight` | `model.trf_blocks[{l}].norm2.scale` | 前馈分支前的 RMSNorm |
   | 10 | `model.layers.{l}.mlp.gate_proj.weight` | `model.trf_blocks[{l}].ff.fc1.weight` | SwiGLU 门控支路（接 SiLU） |
   | 11 | `model.layers.{l}.mlp.up_proj.weight` | `model.trf_blocks[{l}].ff.fc2.weight` | SwiGLU 内容支路（线性） |
   | 12 | `model.layers.{l}.mlp.down_proj.weight` | `model.trf_blocks[{l}].ff.fc3.weight` | 前馈降维投影 |
   | 13 | `model.norm.weight` | `model.final_norm.scale` | 最终 RMSNorm（全局，一份） |
   | 14 | `lm_head.weight`（仅部分 checkpoint 有） | `model.out_head.weight` | 独立输出头（见 4.3） |

2. 用一个小脚本统计 4B checkpoint 的张量总数来验证表的完备性（示例代码）：每层 12 个（#2~#13 中的 12 个每层项）× 36 层 + 2 个全局项 = **434 个**；若 checkpoint 未绑定，再加 1 个 `lm_head.weight`。（待本地验证——需先按 4.4 下载权重，`len(weights)` 应等于 434 或 435。）
3. 思考题自测：如果把 `gate_proj` 错接到 `fc2`、`up_proj` 错接到 `fc1` 会发生什么？（形状完全相同、加载不报错，但门控与内容支路互换，生成结果是乱码——这正是「形状守门防不住语义错位」的典型案例，也是练习 3 的主题。）

**需要观察的现象**：映射表中 12 个「每层项」在 `assign` 调用里各出现一次；`{l}` 从 0 到 `n_layers-1` 共展开 36 份（4B）。

**预期结果**：得到一张与源码逐行对得上的 14 行映射表；`len(weights) == 36*12 + 2 (+1)`。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `assign` 里「形状不匹配就抛 `ValueError`」是必要设计？它能防住哪些错误、防不住哪些？

**答案**：它把**结构错配**（配置字典与 checkpoint 尺寸不符、稠密/绑定假设不符等导致形状对不上的情况）在加载期立刻暴露，并报出具体张量名，而不是让模型带错权重运行出无人察觉的劣化结果。但它防不住**形状相同的语义错位**：比如把 `gate_proj` 接到 `fc2`（同为 `hidden_dim × emb_dim`），或层号错位（`{l}` 与 `{l+1}` 的张量形状全同）——这些只能靠映射表写对、靠加载后的生成质量抽检兜底。

**练习 2**：两套加载器的 `assign` 一个「换新 Parameter」、一个「原地 copy_」，对后续训练（如第 6 章 GRPO）各有什么含义？

**答案**：`copy_` 原地拷贝保持 Parameter 对象身份不变——若模型已注册优化器或已被 `torch.compile` 捕获，原地更新不会破坏引用关系；`clone().detach()` 替换则会换掉属性指向的对象，构造 optimizer 之后再这样加载，优化器里持有的仍是旧张量，训练会「纹丝不动」。本讲只做推理，两者皆可，但工程上原地写法更稳。

**练习 3**：设计一个最小实验，证明 `fc1↔fc2`（gate/up 互换）在加载期不报错但破坏生成。

**答案**：示例思路——用 0.6B 权重与 `Qwen3Model(QWEN_CONFIG_06_B)`：先正常灌权重生成 20 个 token 记录输出；再复制 `load_weights_into_qwen` 的逻辑、仅交换 `gate_proj` 与 `up_proj` 的目标（`fc1 ← up_proj`、`fc2 ← gate_proj`）灌一次，同样 prompt 再生成。观察：第二次加载全程无 `ValueError`（两个张量形状同为 3072×1024），但生成文本退化为乱码或重复。这说明权重映射的正确性校验最终要落在「生成质量」上，也正是 chD 在加载后立刻跑一段生成（D.3 最后一格）的原因。（待本地验证具体输出。）

### 4.3 MoE 分支与 lm_head 权重绑定

#### 4.3.1 概念说明

**MoE（混合专家）分支**。稠密前馈网络每层一组 `fc1/fc2/fc3`；MoE 把它换成「路由器 + N 个专家」：每个专家是一套独立的小前馈（同样是 gate/up/down 三个投影），路由器（HF 名 `mlp.gate`，注意与稠密的 `mlp.gate_proj` 是两回事）根据 token 表示输出各专家的选择权重，每个 token 只经过被选中的少数专家。好处是**参数量大而计算量小**（Qwen3-30B-A3B：总参数约 30B，每 token 激活仅约 3B）。本仓库的 `FeedForward` 是稠密实现（[reasoning_from_scratch/qwen3.py:L139-L150](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3.py#L139-L150)，只有三个 `nn.Linear`），因此 `load_hf_weights_into_qwen` 的 MoE 分支是为**配合 MoE 版前馈实现**预留的加载逻辑——`param_config` 里出现 `num_experts` 键才触发。chD notebook 明确说明本书代码不支持稀疏 MoE 变体，并指向作者 LLMs-from-scratch 仓库的 qwen3 MoE 章节作为从零实现的参考。

**权重绑定分支**。加载到输出头时的两条路：checkpoint 里有 `lm_head.weight` 就把它灌进 `out_head`（未绑定模型）；没有就令 `out_head.weight = tok_emb.weight`（绑定，省掉 \( V \times d \) 的一张大矩阵存储与下载）。Qwen3 官方发布的模型基本都不单独存 `lm_head.weight`，所以实际几乎总走绑定分支（chD 输出里的那句 `Model uses weight tying.` 即为凭证）。这也呼应 u2-l1 的参数口径：官方 0.6B「0.6B 参数」的说法按绑定计。

#### 4.3.2 核心流程

MoE 分支的搬运逻辑（第 `l` 层）：

```text
若 param_config 含 "num_experts" = N：
    block.ff.gate.weight        ← model.layers.{l}.mlp.gate.weight          # 路由器（1 个）
    for e in 0..N-1：
        prefix = model.layers.{l}.mlp.experts.{e}
        block.ff.fc1[e].weight  ← prefix.gate_proj.weight                  # 专家 e 的门控支路
        block.ff.fc2[e].weight  ← prefix.up_proj.weight                    # 专家 e 的内容支路
        block.ff.fc3[e].weight  ← prefix.down_proj.weight                  # 专家 e 的降维投影
        灌完立即把三个专家层 .to("cpu")                                       # 从 meta 设备落到内存
否则（稠密）：
    block.ff.fc1/fc2/fc3.weight ← mlp.gate_proj / up_proj / down_proj       # 4.2 已讲
```

命名上的小心陷阱：HF 的稠密前馈叫 `mlp.gate_proj`，MoE 路由器叫 `mlp.gate`——只差一个 `_proj` 后缀，语义完全不同（前者是 SwiGLU 门控支路，后者是专家路由器）。

输出头分支：

```text
若 "lm_head.weight" in params：   model.out_head.weight ← lm_head.weight   # 独立头
否则：                            model.out_head.weight = model.tok_emb.weight  # 绑定 + 打印提示
```

#### 4.3.3 源码精读

- [reasoning_from_scratch/qwen3.py:L602-L630](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3.py#L602-L630)——MoE 分支全文：先灌路由器 `block.ff.gate.weight ← mlp.gate.weight`；再对每个专家 `e` 用 `fc1[e]/fc2[e]/fc3[e]`（ModuleList 索引，要求 `ff.fc1` 等是长度为 `num_experts` 的 ModuleList）灌入 `experts.{e}.gate_proj/up_proj/down_proj`。注释「After assigning weights, move the expert layers from meta to CPU」透露了该分支设想的使用方式：模型先在 **meta 设备**上以空壳形式实例化（如 `accelerate` 的 `init_empty_weights`，不在内存里真正分配参数），随后逐专家灌权重并搬到 CPU——这是加载超大 MoE 模型时控制峰值内存的常见手法。
- [reasoning_from_scratch/qwen3.py:L632-L647](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3.py#L632-L647)——稠密 `else` 分支：即 4.2 已精读的 `fc1 ← gate_proj` 等三条赋值，与前一个分支互斥。
- [reasoning_from_scratch/qwen3.py:L658-L662](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3.py#L658-L662)——权重绑定分支：`if "lm_head.weight" in params` 走独立输出头；否则 `model.out_head.weight = model.tok_emb.weight` 并打印 `Model uses weight tying.`。直接把 `Parameter` 对象赋给另一属性，PyTorch 会把两者视为同一张量（state_dict 里仍会各存一份键）。
- 对照 [reasoning_from_scratch/appendix_c.py:L164-L168](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/appendix_c.py#L164-L168)——附录 C 版本没有 `lm_head` 探测，直接绑定；可以把它理解为「面向 Qwen3 全系均绑定这一事实的简化版」，而 qwen3.py 版本多一层通用性。
- 顺带确认稠密 `FeedForward` 无路由器结构：[reasoning_from_scratch/qwen3.py:L139-L150](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3.py#L139-L150) 中 `fc1/fc2/fc3` 是三个普通 `nn.Linear`——若直接对本仓库模型触发 MoE 分支，`block.ff.gate` 会在属性访问处报 `AttributeError`，而不是静默出错。

#### 4.3.4 代码实践

**实践目标**：不动大权重，用「假 checkpoint 字典」驱动加载函数，观察绑定分支与 MoE 分支的行为差异。

1. **绑定分支观察**（示例代码，CPU 可跑、内存需求约 3 GB，请酌情缩小配置）：

   ```python
   # 示例代码：用随机权重观察 lm_head 分支
   import torch
   from reasoning_from_scratch.qwen3 import Qwen3Model, load_hf_weights_into_qwen

   cfg = {"vocab_size": 1000, "context_length": 64, "emb_dim": 64,
          "n_heads": 4, "n_layers": 2, "hidden_dim": 128, "head_dim": 16,
          "qk_norm": True, "n_kv_groups": 2, "rope_base": 1_000_000.0,
          "dtype": torch.float32}
   model = Qwen3Model(cfg)

   # 按 HF 命名伪造一份「无 lm_head」的 checkpoint（随机数即可）
   params = {"model.embed_tokens.weight": torch.randn(1000, 64),
             "model.norm.weight": torch.randn(64)}
   for l in range(2):
       p = f"model.layers.{l}"
       params[f"{p}.self_attn.q_proj.weight"] = torch.randn(64, 64)   # 4 头×16
       params[f"{p}.self_attn.k_proj.weight"] = torch.randn(32, 64)   # 2 KV 组×16
       params[f"{p}.self_attn.v_proj.weight"] = torch.randn(32, 64)
       params[f"{p}.self_attn.o_proj.weight"] = torch.randn(64, 64)
       params[f"{p}.self_attn.q_norm.weight"] = torch.randn(16)
       params[f"{p}.self_attn.k_norm.weight"] = torch.randn(16)
       params[f"{p}.input_layernorm.weight"] = torch.randn(64)
       params[f"{p}.post_attention_layernorm.weight"] = torch.randn(64)
       params[f"{p}.mlp.gate_proj.weight"] = torch.randn(128, 64)
       params[f"{p}.mlp.up_proj.weight"] = torch.randn(128, 64)
       params[f"{p}.mlp.down_proj.weight"] = torch.randn(64, 128)

   load_hf_weights_into_qwen(model, {"n_layers": 2}, params)
   print(model.out_head.weight is model.tok_emb.weight)   # 绑定 → True
   params["lm_head.weight"] = torch.randn(1000, 64)       # 补上独立头
   load_hf_weights_into_qwen(model, {"n_layers": 2}, params)
   print(model.out_head.weight is model.tok_emb.weight)   # 未绑定 → False
   ```

2. **MoE 分支的失败模式观察**：给上面的 `param_config` 加上 `"num_experts": 4` 再调用一次。因为本仓库 `FeedForward` 没有 `.gate` 属性，预期在 `block.ff.gate.weight` 处抛 `AttributeError`——这印证了「MoE 分支需要 MoE 版前馈实现配合」的结论。
3. 把两次运行的异常类型与消息记录下来（`ValueError` 来自形状守门、`AttributeError` 来自结构缺失），归纳成一句结论。

**需要观察的现象**：第一次加载打印 `Model uses weight tying.`、`is` 判断为 `True`；补 `lm_head.weight` 后不再打印、`is` 为 `False`；加 `num_experts` 后抛 `AttributeError`。

**预期结果**：绑定分支让 `out_head` 与 `tok_emb` 成为**同一个对象**（`is` 为 True）；MoE 分支在稠密模型上必然失败于属性访问。若你的运行结果不同，请核对缩小配置里各张量形状是否与模型一致。

#### 4.3.5 小练习与答案

**练习 1**：`mlp.gate` 与 `mlp.gate_proj` 分别是什么？为什么说这个命名「只差一个后缀」很容易看错？

**答案**：`mlp.gate_proj` 是**稠密** SwiGLU 前馈的门控支路投影（接 SiLU 的那支），形状 `hidden_dim × emb_dim`；`mlp.gate` 是 **MoE** 的路由器，形状 `num_experts × emb_dim`（把 token 表示映射为专家选择权重）。两者语义、形状、所属结构都不同，命名却只差 `_proj`。读 HF checkpoint 的键列表时，看到 `mlp.gate.weight` 即可断定这是 MoE 模型。

**练习 2**：为什么 MoE 分支里灌完每个专家要 `.to("cpu")`？注释里说的 meta 设备是什么？

**答案**：meta 设备上的张量只有形状与步长等元信息、不占实际内存，`accelerate` 等库用它来「先搭好 30B 模型的空壳再谈装填」。空壳上的 `assign` 完成的是元信息层面的对接，真正占内存的搬运用 `.to("cpu")` 显式完成；逐专家搬运可以把峰值内存控制在一个专家的体量，而不是整层甚至整个模型。

**练习 3**：本仓库模型加载 Qwen3-0.6B 官方权重时（chC 路线 vs chD 路线），输出头分别来自哪里？

**答案**：chC 路线加载的是作者重新打包的 `.pth`（本仓库 state_dict 命名），`model.load_state_dict` 直接按 `out_head.weight` 键灌入——那份 `.pth` 里已经存好了输出头张量；chD/附录 C 路线加载官方 safetensors，里面没有 `lm_head.weight`，于是走绑定分支复用 `tok_emb.weight`。两条路线殊途同归：官方模型本来就是绑定的，`.pth` 里的 `out_head.weight` 与嵌入同源。

### 4.4 下载器 download_from_huggingface_from_snapshots

#### 4.4.1 概念说明

第 2~5 章的 `download_qwen3_small`（u1-l3）下载的是作者托管的小 `.pth`；更大的官方模型则发布在 Hugging Face Hub 上的模型仓库（如 `Qwen/Qwen3-4B-Base`）里，格式为 safetensors，且大模型几乎总是**多分片**：每个分片是一个 `.safetensors` 文件，另有一个 `model.safetensors.index.json` 索引记录「张量名 → 所在分片文件名」。

`download_from_huggingface_from_snapshots` 做两件事：

1. 调 `huggingface_hub.snapshot_download` 把**整个仓库快照**（权重、分词器、配置文件）镜像到本地目录；
2. 把权重从 safetensors 文件读出、合并成一个普通的 `{张量名: torch.Tensor}` 字典返回——无论单文件还是多分片，调用方拿到的都是同一个形状的字典，随后直接交给 `load_hf_weights_into_qwen`。

它需要**额外安装** `huggingface_hub` 与 `safetensors`——两者不在本仓库 `requirements.txt` 里（[requirements.txt](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/requirements.txt) 只有 reasoning-from-scratch、setuptools、sympy、torch、tokenizers 五项），chD notebook 里用 `uv add huggingface_hub safetensors` 或 `pip install` 补装。这也体现了该函数「按需导入」（import 写在函数体内）的风格：不装这些包时，仓库其余功能不受影响。

#### 4.4.2 核心流程

```text
输入：repo_id（如 "Qwen/Qwen3-4B-Base"）、local_dir
1. snapshot_download 镜像整个仓库 → repo_dir（若已有缓存则秒回）
2. 检查 repo_dir 下存在哪种权重布局：
   a. 有 model.safetensors.index.json（多分片）：
        for 每个出现过的分片文件名（set 去重）：
            load_file(分片) → 把该分片全部张量并入 weights_dict
   b. 否则有 model.safetensors（单文件）：
        hf_hub_download 单独取该文件 → load_file 一次性读入
   c. 都没有：raise FileNotFoundError
3. 返回合并后的 {张量名: Tensor} 字典
```

注意多分片分支循环的是 `set(index["weight_map"].values())`——索引里每个张量都记一行，`values()` 是分片文件名列表（有大量重复），`set` 去重后每个分片只读一次。

#### 4.4.3 源码精读

- [reasoning_from_scratch/appendix_c.py:L171-L175](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/appendix_c.py#L171-L175)——函数体首行的延迟导入（`huggingface_hub` 与 `safetensors.torch.load_file`）与 `snapshot_download(repo_id=..., local_dir=...)` 镜像调用。
- [reasoning_from_scratch/appendix_c.py:L177-L189](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/appendix_c.py#L177-L189)——多分片分支：读索引 JSON，对 `set(index["weight_map"].values())` 中的每个分片 `load_file` 后 `weights_dict.update(shard)` 合并。4B 模型有多个分片，走的就是这条路（chD 输出显示 `Fetching 13 files`，即整个仓库快照共 13 个文件）。
- [reasoning_from_scratch/appendix_c.py:L190-L199](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/appendix_c.py#L190-L199)——单文件分支（小模型常见）与兜底：既无索引又无 `model.safetensors` 时抛 `FileNotFoundError`，把「仓库里根本没有权重」这类配置错误显式化。
- [reasoning_from_scratch/appendix_c.py:L201](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/appendix_c.py#L201)——返回合并字典。
- chD notebook 的调用现场（D.3 节）：[chD/chD_main.ipynb:L228-L241](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/chD/chD_main.ipynb#L228-L241)——`weights = download_from_huggingface_from_snapshots(repo_id="Qwen/Qwen3-4B-Base", local_dir=Path("qwen3-4b-base"))`。

#### 4.4.4 下载与加载全流程实践

**实践目标**：在显存允许的条件下，任选一个大模型（推荐 1.7B-Base，约 3.4 GB；上限自评：bf16 权重下限 + 激活 ≈ 表中 GB 数 × 1.2）走通「下载 → 实例化 → 灌权重 → 分词器 → 生成」。

1. 安装额外依赖：`pip install huggingface_hub safetensors`（或 uv 环境 `uv add ...`）。
2. 下载并读取权重（示例代码，以 1.7B 为例；换 `Qwen/Qwen3-4B-Base` 则配 `QWEN3_CONFIG_4B`）：

   ```python
   from pathlib import Path
   from reasoning_from_scratch.appendix_c import (
       QWEN3_CONFIG_1_7B, download_from_huggingface_from_snapshots,
   )
   from reasoning_from_scratch.qwen3 import Qwen3Model, load_hf_weights_into_qwen, Qwen3Tokenizer
   from reasoning_from_scratch.ch02 import get_device, generate_text_basic_stream_cache
   import shutil, torch

   local_dir = Path("qwen3-1.7b-base")
   weights = download_from_huggingface_from_snapshots(
       repo_id="Qwen/Qwen3-1.7B-Base", local_dir=local_dir)

   device = get_device()
   model = Qwen3Model(QWEN3_CONFIG_1_7B)                # 结构由配置决定
   load_hf_weights_into_qwen(                           # 权重由 checkpoint 决定
       model, param_config={"n_layers": QWEN3_CONFIG_1_7B["n_layers"]}, params=weights)
   model.to(device).eval()

   # 分词器：HF 包里叫 tokenizer.json，Qwen3Tokenizer 约定 base 名为 tokenizer-base.json
   tokenizer_path = local_dir / "tokenizer-base.json"
   if not tokenizer_path.exists():
       shutil.copyfile(local_dir / "tokenizer.json", tokenizer_path)
   tokenizer = Qwen3Tokenizer(tokenizer_file_path=tokenizer_path)  # base 变体：不加聊天模板

   ids = torch.tensor(tokenizer.encode("Explain large language models in two sentences.")).unsqueeze(0).to(device)
   for token in generate_text_basic_stream_cache(
           model=model, token_ids=ids, max_new_tokens=64,
           eos_token_id=tokenizer.eos_token_id):
       print(tokenizer.decode(token.squeeze(0).tolist()), end="", flush=True)
   ```

3. 重复调用一次 `download_from_huggingface_from_snapshots`，观察第二次几乎瞬时完成（本地快照已存在，`snapshot_download` 直接复用）。
4. 若要加载 4B **reasoning** 变体，按 chD notebook D.4 节的说明改三处：`repo_id` 换成 `Qwen/Qwen3-4B`；分词器文件复制为 `tokenizer-reasoning.json`；`Qwen3Tokenizer(..., apply_chat_template=True, add_generation_prompt=True, add_thinking=True)`。模型侧代码零改动。

**需要观察的现象**：加载阶段打印 `Model uses weight tying.`（Qwen3 官方权重无独立 `lm_head`）；生成得到语法连贯的两三句英文；第二次下载秒回。

**预期结果**：与 chD notebook D.3 节记录的 4B-Base 输出风格一致（对 LLM 的两句定义式回答）。生成的具体文字随模型与采样设置而定，属正常现象。若在 CPU 上运行 1.7B，逐 token 生成会明显偏慢但可行；4B 以上建议 GPU。整段流程的耗时与显存占用取决于硬件，**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：为什么不直接 `load_file` 整个 `model.safetensors.index.json`，而是循环 `set(index["weight_map"].values())`？

**答案**：索引文件本身只存「张量名 → 分片文件名」的映射（JSON 文本），不含权重数据；真正的权重在各个分片 `.safetensors` 里。`weight_map` 每个张量一行、分片名大量重复，`set` 去重后按分片逐个 `load_file` 再合并，才能把所有张量收齐且每个分片只读一次。

**练习 2**：`snapshot_download` 已经把整个仓库拉到本地了，为什么单文件分支还要再调一次 `hf_hub_download`？

**答案**：这是防御性写法。`snapshot_download` 镜像整个仓库后，`model.safetensors` 理论上已在 `repo_dir` 里，但显式调用 `hf_hub_download(filename="model.safetensors", local_dir=...)` 会返回该文件的规范路径并保证命中（必要时再走一次 Hub 缓存协商），避免因仓库快照布局变化（文件在子目录等）导致路径拼接失效。对使用者的启示：依赖 Hub 下载的代码要对「文件到底落在哪」保持显式。

**练习 3**：如果加载 8B 模型时把配置字典误用成 `QWEN3_CONFIG_4B`，会在哪一步、以什么形式失败？

**答案**：在 `load_hf_weights_into_qwen` 的 `assign` 处失败，形式是 `ValueError: Shape mismatch in tensor ...`。实例化 `Qwen3Model(QWEN3_CONFIG_4B)` 会造出 36 层、emb_dim 2560 的小模型，而 8B checkpoint 的张量是 36 层、emb_dim 4096；第一个对不上形状的张量（词嵌入 `model.embed_tokens.weight`：151936×4096 对 151936×2560）就会触发形状守门报错——这正是 4.2 练习 1 说的「结构错配在加载期暴露」。

## 5. 综合实践：给「任意尺寸 Qwen3」写一张加载说明书

把本讲四个模块串起来，产出一页可复用的加载说明书（纯文档 + 一个小脚本，不改源码）：

1. **选型与预算**（4.1）：写一个函数 `budget(cfg)`，输入任一配置字典，输出「每层参数量 / 总参数量（绑定口径）/ bf16 显存下限」，对六个配置跑一遍并填成 4.1.2 的完整表格（含 8B、14B、32B 的粗算值，14B ≈ 14.0B、32B ≈ 32.4B 可作自检答案）。
2. **映射表**（4.2）：把 14 行映射表誊入说明书，并在每行末尾标注源码依据（如「qwen3.py:L557-L561」）。
3. **分支决策**（4.3）：画一张两问决策图——「checkpoint 键里有 `mlp.gate.weight` 吗？」有 → MoE 路线（本仓库需另配 MoE 前馈）；无 → 稠密路线。「有 `lm_head.weight` 吗？」有 → 独立头；无 → 绑定。
4. **端到端冒烟**（4.4）：在你能负担的最大尺寸上跑通 4.4.4 的脚本，记录：下载文件数、是否打印 `Model uses weight tying.`、首 token 延迟、总耗时与峰值显存（用 `torch.cuda.max_memory_allocated()` 或活动监视器）。
5. **回归自检**：加载后固定 prompt 生成 32 个 token，把输出贴进说明书——它是「权重映射写对了」的最直接证据（呼应 4.2 练习 3：形状校验防不住语义错位，生成质量才防得住）。

若本机跑不动任何大模型，可退而求其次：用 4.3.4 的玩具规模假 checkpoint 完成第 3、5 步（生成会是随机噪声，但流程与分支行为完全一致）。

## 6. 本讲小结

- Qwen3 从 0.6B 到 32B **架构完全同构**，只有 `emb_dim / n_heads / n_layers / hidden_dim` 四个尺寸旋钮在变；`appendix_c.py` 用五个配置字典补齐了大尺寸，`Qwen3Model` 一行不改即可实例化。
- HF checkpoint 与本仓库模型的差异只在**命名**：`q_proj/k_proj/v_proj/o_proj` ↔ `W_query/W_key/W_value/out_proj`、`gate/up/down_proj` ↔ `fc1/fc2/fc3`（顺序不可换）、`*.layernorm.weight` ↔ `norm*.scale`；两套加载器（`appendix_c.load_weights_into_qwen` 与实际使用的 `qwen3.load_hf_weights_into_qwen`）都是按这张映射表逐张量搬运，`assign` 的形状守门把结构错配挡在加载期。
- `load_hf_weights_into_qwen` 多出两个分支：`num_experts` 触发 MoE 路线（路由器 `mlp.gate` + 逐专家 `experts.{e}.*`，灌完 `.to("cpu")`，设想配合 meta 设备空壳使用，本仓库稠密 `FeedForward` 不能直接走这条路）；输出头按「有 `lm_head.weight` 则加载、无则绑定 `tok_emb`」决策，Qwen3 官方权重几乎总走绑定。
- `download_from_huggingface_from_snapshots` 把 Hub 仓库快照拉到本地，并把单文件或多分片 safetensors 统一合并成 `{张量名: Tensor}` 字典，需额外安装 `huggingface_hub` 与 `safetensors`。
- 显存预算的第一性原理是 \( \text{bytes} \approx 2 \times \text{参数量} \)（bf16），而参数量可由配置字典用 \( L(3dh + \text{attn}) + Vd \) 粗算并与公开规模核对。
- 权重映射没有独立单测（`tests/test_appendix_c.py` 测的是 chC 模型定义的玩具规模生成回归），它的正确性最终靠加载后的生成质量兜底。

## 7. 下一步学习建议

- **u10-l1（SDPA 优化实现）与 u10-l2（批量推理）**：大模型加载只是入场券，跑得动还要跑得快——下一讲讨论 `qwen3_optimized.py` 的 SDPA 融合注意力与预分配 KV cache，以及批处理吞吐优化，与本讲的显存预算直接衔接。
- **u9-l5（HF 生态互通）**：本讲是「HF → from-scratch」方向的权重搬运；那一讲讲反方向「from-scratch → HF」的 wrapper 与 export 两种方案，合起来即完整的双向互通。
- **源码延伸阅读**：若对 MoE 分支意犹未尽，按 chD notebook D.1 节给出的链接阅读作者 LLMs-from-scratch 仓库的 qwen3 MoE 从零实现（含路由器与专家 ModuleList 的前馈），再回头看 `load_hf_weights_into_qwen` 的 MoE 分支会豁然开朗。
- **实践延伸**：把你在本讲预算出的 1.7B/4B 模型接到 ch02 的评测脚本或 chG 的 Chainlit 聊天界面上，验证「换配置字典 + 换 checkpoint」即可整体升级推理能力。
