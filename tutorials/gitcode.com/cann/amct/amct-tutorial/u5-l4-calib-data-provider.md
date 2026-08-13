# 校准数据加载与 PTQ 数据 Provider

## 1. 本讲目标

本讲承接 [u4-l1（extract_ptq_data）](u4-l1-extract-ptq-data.md) 与 [u4-l2（PTQ 主流程）](u4-l2-ptq-main-flow.md)：前两讲告诉我们「校准数据被录到 `--data_dir`」「ptq 阶段再读回来训练」，但中间这层「数据怎么来、怎么存、怎么又变成训练 batch」的黑盒没有打开。本讲就钻进 `amct_pytorch/common/datasets/` 模块，把这个黑盒拆开。

学完本讲你应当能够：

- 说清 `preproc.get_pileval` 如何把一段原始文本语料切成 `nsamples × [1, seq_len]` 的校准样本。
- 说清 `ptq_io` 的 `save_ptq_inps` / `save_ptq_kwargs` / `load_ptq_inps` 三组存取函数的文件命名契约与读写对称性。
- 说清 `LlmPtqDataProvider` 如何把「单元输入 + 浮点 ground truth」包装成 PyTorch `DataLoader`，供 `BlockwiseSolver` 逐 batch 训练。
- 画出校准数据从「原始文本 → 落盘 pkl → 读回 → ground truth → DataLoader」的完整生命周期。

## 2. 前置知识

阅读本讲前，建议你已经建立以下认知（来自前置讲义）：

- **PTQ 四阶段链路**（[u1-l4](u1-l4-first-quant-cli.md)）：`eval → extract_ptq_data → ptq → deploy`。`extract_ptq_data` 是「数据准备工」，`ptq` 是「逐层优化工」，两者靠 `--data_dir` 目录接力。
- **校准数据 / 中间激活**（[u4-l1](u4-l1-extract-ptq-data.md)）：量化算法需要在少量代表性输入上统计激活分布、求 scale。AMCT 的做法不是把原始 token 喂给每一层，而是先用 `extract_ptq_data` 跑一遍前向、把每个待量化子模块**紧前面的 norm 输出**（即该子模块的输入激活）录下来，后续 `ptq` 直接拿这些录好的激活复现前向，省去重复跑 embedding。
- **PtqUnit**（[u4-l2](u4-l2-ptq-main-flow.md) / [u5-l1](u5-l1-base-model-pipeline.md)）：最小量化工作单元。attn、mlp 各 1 个，MoE 每个 expert 1 个。本讲会反复用到它的 `kind`（归一化后的目标名 `attn`/`mlp`/`moe`）与 `layer_idx` 字段。
- **重建（reconstruction）目标**（[u4-l3](u4-l3-blockwise-solver.md)）：PTQ 训练让「量化子模块的输出」逼近「原始浮点子模块的输出」。后者就是本讲要生成的 **ground truth（GT）**。

两个 PyTorch 基础概念：

- `torch.save(obj, path)` / `torch.load(path)`：把任意张量（或容器）用 pickle 序列化到磁盘 / 从磁盘读回。本讲的 `.pkl` 文件就是它的产物。
- `DataLoader` + `TensorDataset`：把一组张量按 `batch_size` 切分成可迭代的小批次，是 PyTorch 训练循环的标准数据入口。

## 3. 本讲源码地图

本讲聚焦 `common/datasets/` 三个文件，并引用它们在工作流与基类中的调用点：

| 文件 | 职责 | 本讲角色 |
| --- | --- | --- |
| `amct_pytorch/common/datasets/preproc.py` | 加载并预处理校准语料（pileval/wikitext） | 数据生命周期的**起点**：原始文本 → 校准样本 |
| `amct_pytorch/common/datasets/ptq_io.py` | PTQ 中间激活与位置参数的存取 | 数据生命周期的**中转**：内存张量 ↔ 磁盘 pkl |
| `amct_pytorch/common/datasets/ptq_provider.py` | `LlmPtqDataProvider`：读回输入、生成 GT、构建 batch | 数据生命周期的**终点**：→ 训练 DataLoader |
| `amct_pytorch/common/models/llm/common/base.py` | `BaseModel`：调用上述存取函数的调用方 | 说明「谁在调」 |
| `amct_pytorch/workflows/llm_extract_ptq_data.py` | extract 阶段工作流 | 说明「何时存」 |
| `amct_pytorch/workflows/llm_ptq.py` | ptq 阶段工作流 | 说明「何时读、何时建 batch」 |
| `amct_pytorch/common/models/llm/common/ptq_units.py` | `PtqUnit` 数据类 | batch 构建时的输入参数 |
| `amct_pytorch/cli/llm/args.py` | CLI 参数定义 | `nsamples`/`seq_len`/`cali_bsz` 等默认值来源 |

> 提示：`datasets` 这一层只负责「数据的搬运与打包」，不做量化、不认识具体模型结构。它把模型相关细节都委托给 `pipeline`（即模型适配器），自己只处理张量与文件。

## 4. 核心概念与源码讲解

本讲按数据流动的自然顺序拆成三个最小模块：**取样 → 存取 → 建 batch**。

### 4.1 校准样本生成：preproc.get_pileval

#### 4.1.1 概念说明

量化算法需要「代表性输入」来统计激活分布。AMCT 默认用 **pileval**（The Pile 的验证子集）作为校准语料——它是一个通用英文文本集合，分布足够宽泛，常被 AWQ/GPTQ 等量化工作用作默认校准集。

但原始文本不能直接喂给量化流程，需要两步加工：

1. ** tokenize**：用模型的 tokenizer 把文本切成 token id。
2. **定长切块**：把所有 token 拼成一条长河，再均匀切成 `nsamples` 条长度恰为 `seq_len` 的序列。

为什么不直接用「一条原文 = 一条样本」？因为原文长短不一，直接用会引入 padding（浪费算力）或截断（丢信息）。AMCT 采用「**先收集够总 token 数、再重新定长切块**」的策略，保证每条样本都是 `[1, seq_len]`、零 padding。

#### 4.1.2 核心流程

`get_pileval` 的核心是 `pileval_awq`，它实现「收集 → 拼接 → 重切」三步：

```text
原始 pileval (validation split)
   │  shuffle(seed=42)
   ▼
逐条遍历：tokenize → 跳过过长(>seq_len)的 → 累计 token
   │  直到 total_tokens >= nsamples × seq_len
   ▼
torch.cat(所有样本, dim=1)   # 拼成一条长 token 河，shape=[1, total]
   │
   ▼
n_split = total // seq_len   # 能切出多少条
若 n_split < nsamples → 报错（语料不够）
   │
   ▼
切成 nsamples 条 [1, seq_len]   # 返回 list[Tensor]
```

设需要 `n` 条样本、每条 `s` 个 token，则至少要收集到 \( n \times s \) 个 token：

\[
\text{target\_tokens} = \text{n\_samples} \times \text{seq\_len}
\]

只有当实际收集到的 token 总数除以 `seq_len`（即 `n_split`）不小于 `n_samples` 时，才能切出足够样本，否则抛出 `ValueError`。

#### 4.1.3 源码精读

`get_pileval` 只是 `load_dataset` + `pileval_awq` 的薄封装：

[preproc.py:54-59](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/datasets/preproc.py#L54-L59) —— 从 HuggingFace Hub 拉 `mit-han-lab/pile-val-backup` 的 validation split，交给 `pileval_awq` 切块。`seq_len` 默认 512（函数签名默认值），但实际调用方会传入 CLI 的 `--seq_len`（默认 4096）。

核心逻辑在 `pileval_awq`：

[preproc.py:23-51](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/datasets/preproc.py#L23-L51) —— 收集与重切。关键几行：

- [preproc.py:27](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/datasets/preproc.py#L27) 设定 token 预算 `target_tokens = n_samples * seq_len`。
- [preproc.py:32-33](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/datasets/preproc.py#L32-L33) 跳过 tokenize 后超过 `seq_len` 的长行（这类行不适合参与「拼接重切」，因为它们本身就够长，会破坏均匀切分语义）。
- [preproc.py:43](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/datasets/preproc.py#L43) 把所有短样本沿 dim=1（序列维）拼成一条长河。
- [preproc.py:44-49](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/datasets/preproc.py#L44-L49) 校验：能切出的条数 `n_split = total // seq_len` 必须 ≥ `n_samples`，否则报错。
- [preproc.py:50](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/datasets/preproc.py#L50) 真正的切块：每条取 `[i*seq_len : (i+1)*seq_len]`，共 `n_samples` 条。

调用点在 extract 工作流：

[llm_extract_ptq_data.py:82-87](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/workflows/llm_extract_ptq_data.py#L82-L87) —— `samples = get_pileval(tokenizer, self.nsamples, seq_len=self.seq_len)` 取样后，立刻送进 `do_embedding_forward` 做 embedding 阶段前向（详见 u5-l1）。

> 备注：本文件还有 `get_wikitext2` / `get_wiki_inputs`（[preproc.py:62-80](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/datasets/preproc.py#L62-L80)），用于 eval 阶段算 PPL（困惑度），与本讲的 PTQ 校准数据通路无关，此处不展开。

#### 4.1.4 代码实践

**实践目标**：在不跑模型的前提下，读懂 `pileval_awq` 的「拼接 + 重切」并对输出形状做出预测。

**操作步骤**：

1. 打开 [preproc.py:23-51](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/datasets/preproc.py#L23-L51)。
2. 假设 `n_samples=128`、`seq_len=4096`（CLI 默认值，见 [args.py:68](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/cli/llm/args.py#L68) 与 [args.py:77](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/cli/llm/args.py#L77)），手算 `target_tokens`。
3. 追踪：若 pileval 实际累计到 600000 个可用 token，`n_split` 是多少？能否满足 128 条？
4. 写一个最小本地复现（不依赖 pileval，用随机 token 模拟「短样本列表」）：

```python
# 示例代码：模拟 pileval_awq 的拼接+重切逻辑（非项目源码）
import torch
seq_len, n_samples = 4096, 128
# 假装这是若干条 tokenize 后的短文本
short = [torch.tensor([[1, 2, 3, 4]]), torch.tensor([[5, 6]]), torch.tensor([[7, 8, 9]])]
river = torch.cat(short, dim=1)          # 对应源码 L43
n_split = river.shape[1] // seq_len       # 对应源码 L44
print("total tokens:", river.shape[1], "可切条数:", n_split)
```

**需要观察的现象**：上述示例代码因为 token 太少（远小于 `n_samples × seq_len`），按源码 L45-49 的逻辑会触发 `ValueError`（「Not enough pileval tokens」）。

**预期结果**：`target_tokens = 128 × 4096 = 524288`；600000 个 token 时 `n_split = 600000 // 4096 = 146 ≥ 128`，满足要求，最终返回 128 条形状为 `[1, 4096]` 的张量。**实际运行需联网下载 pileval 数据集，待本地验证。**

#### 4.1.5 小练习与答案

**练习 1**：为什么 `pileval_awq` 要跳过 `len(line_encoded) > seq_len` 的行，而不是把它们截断到 `seq_len`？

**参考答案**：因为后续会「拼接所有 token 再均匀重切」。如果先保留长行并截断，既会丢掉截断部分、又会让「拼接重切」的边界错乱。跳过长行、只用短行拼接，能保证重切后的每条样本都来自连续的真实语料，语义连贯。

**练习 2**：如果把 `--nsamples` 调大但 pileval 语料不够，代码会在哪一行报什么错？

**参考答案**：会在 [preproc.py:46-49](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/datasets/preproc.py#L46-L49) 抛 `ValueError`，提示 "Not enough pileval tokens to build {n_samples} samples ..."；若一条有效样本都没收集到，则更早在 [preproc.py:41-42](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/datasets/preproc.py#L41-L42) 报 "No valid pileval samples were collected."。

---

### 4.2 中间激活的落盘与读回：ptq_io

#### 4.2.1 概念说明

`ptq_io.py` 是 extract 与 ptq 两阶段之间的「**磁盘契约层**」。它定义了三类文件及其命名规则：

| 文件名 | 内容 | 何时写 | 何时读 |
| --- | --- | --- | --- |
| `block_{layer_idx}_{target}_in.pkl` | 某层某目标子模块的**输入激活**（norm 的输出） | extract 阶段逐层前向时 | ptq 阶段逐 unit 取回 |
| `position_ids.pkl` | 第 0 层捕获的位置 id | extract 的 embedding 阶段 | ptq 阶段（仅 attn 目标） |
| `position_embeddings.pkl` | 旋转/绝对位置嵌入 | 同上 | 同上 |
| `attention_mask.pkl` | 注意力掩码 | 同上 | 同上 |

后三个（位置/掩码）只对 **attn 目标**有意义——注意力计算需要位置与掩码信息；而 mlp/moe 只是前馈网络，输入只有隐状态，不需要这些 kwargs。这正是 `load_ptq_inps` 里 `if quant_target == "attn"` 分支的来源。

> 关键术语：**落盘契约（file contract）**。两阶段是独立进程（extract 在 NPU 跑、ptq 可能 CPU 暂存），不能靠内存传参，必须靠这套文件名约定对齐。这也是 [u4-l1](u4-l1-extract-ptq-data.md) 强调「extract 与 ptq 的 `--quant_target` 必须一致」的文件层根源。

#### 4.2.2 核心流程

存与读严格对称，关键是 **target 的归一化**：用户侧 `--quant_target` 取 `mlp`/`moe`/`attn-linear`/`attn-cache` 四种，但落盘前会被归并——`attn-linear` 与 `attn-cache` 都存成 `attn`，mlp/moe 保持原名。归并发生在写侧（`BaseModel.save_block_hook_inputs`），读侧（`load_ptq_inps`）收到的 `unit.kind` 也已是归一化后的 `attn`/`mlp`/`moe`，两端自然对齐。

```text
【写：extract 阶段】
act_stat[f"{hook_name}_out"]  (一个 list[Tensor])
   │ torch.cat                  # 拼成单张量
   ▼
save_ptq_inps → block_{layer_idx}_{attn|mlp|moe}_in.pkl

position_ids/embeddings/attention_mask  (embedding 阶段捕获)
   │
   ▼
save_ptq_kwargs → {position_ids,position_embeddings,attention_mask}.pkl

【读：ptq 阶段】
load_ptq_inps(data_dir, unit.kind, layer_idx)
   │ 读 block_{layer_idx}_{kind}_in.pkl
   ├─ 若 kind=="attn"：再读三个位置/掩码 pkl，组装 kwargs
   └─ 返回 (cached_inps, kwargs)
```

#### 4.2.3 源码精读

**写侧——`save_ptq_kwargs`**：

[ptq_io.py:23-32](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/datasets/ptq_io.py#L23-L32) —— 逐个判空写入三个位置/掩码张量。`os.makedirs(data_dir, exist_ok=True)` 保证目录存在。注意它对每个张量单独 `if x is not None`，因为不同模型可能没有 `position_embeddings`（如使用线性注意力的模型）。

**写侧——`save_ptq_inps`**：

[ptq_io.py:35-39](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/datasets/ptq_io.py#L35-L39) —— 先 `torch.cat(act_stat[f"{hook_name}_out"])` 把「逐样本 hook 到的输出列表」拼成单张量，再按 `block_{layer_idx}_{quant_target}_in.pkl` 命名落盘。这里传入的 `quant_target` 已是归一化后的 `attn`/`mlp`/`moe`。

归一化逻辑在调用方：

[base.py:179-187](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/llm/common/base.py#L179-L187) —— `save_block_hook_inputs` 里 `save_target = "attn" if "attn-linear" in ... or "attn-cache" in ... else self.quant_target[0]`，把两个 attn 变体归并成 `attn`。`save_ptq_kwargs` 的调用点在 embedding 阶段：

[base.py:207-213](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/llm/common/base.py#L207-L213) —— 第 0 层用 `Catcher` 捕获到 `position_ids`/`position_embeddings`/`attention_mask` 后，若 `hook_name is not None`（即处于 extract 模式）就落盘。

**读侧——`load_ptq_inps`**：

[ptq_io.py:42-63](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/datasets/ptq_io.py#L42-L63) —— 读回逻辑。几个要点：

- [ptq_io.py:44-56](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/datasets/ptq_io.py#L44-L56)：**仅当 `quant_target == "attn"`** 才尝试读三个位置/掩码文件，且每个都用 `os.path.exists` 判存在——缺失（某些模型没有）就跳过，不会报错。
- [ptq_io.py:57-59](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/datasets/ptq_io.py#L57-L59)：读主输入文件，`weights_only=True` 是 PyTorch 的**反序列化安全开关**——只允许加载纯张量，禁止执行任意 pickle 代码，防止恶意 pkl 文件攻击。
- [ptq_io.py:60-62](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/datasets/ptq_io.py#L60-L62)：文件不存在时不抛错，而是 `logger.warning` 并返回 `(None, kwargs)`——配合 ptq 阶段的断点/容错处理。

读侧的统一入口在 `BaseModel.load_unit_inputs`：

[base.py:81-82](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/llm/common/base.py#L81-L82) —— `return load_ptq_inps(data_dir, unit.kind, unit.layer_idx)`。注意它用 `unit.kind` 而非原始 `quant_target`，所以同一层 MoE 的每个 expert 都读同一个 `block_{idx}_moe_in.pkl`（共享输入），与 [u4-l2](u4-l2-ptq-main-flow.md) 讲的「MoE 各 expert 共享一份输入」一致。

#### 4.2.4 代码实践

**实践目标**：验证存取对称性，并理解 attn 目标为何多读三个文件。

**操作步骤**：

1. 阅读 [ptq_io.py:35-39](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/datasets/ptq_io.py#L35-L39) 与 [ptq_io.py:42-63](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/datasets/ptq_io.py#L42-L63)，确认「写时用的文件名格式」与「读时拼的文件名格式」完全一致。
2. 写一个最小 round-trip（不依赖模型，纯张量）：

```python
# 示例代码：手写一个 save→load 的最小往返（非项目源码）
import torch, os
data_dir = "/tmp/ptq_io_demo"
os.makedirs(data_dir, exist_ok=True)

# 模拟 save_ptq_inps：act_stat 是「逐样本 hook 输出列表」
act_stat = {"input_layernorm_out": [torch.randn(1, 4, 8), torch.randn(1, 4, 8)]}
outs = torch.cat(act_stat["input_layernorm_out"])      # 对应源码 L38
torch.save(outs, os.path.join(data_dir, "block_0_attn_in.pkl"))  # 对应源码 L39

# 模拟 load_ptq_inps 的主输入读取
cached = torch.load(os.path.join(data_dir, "block_0_attn_in.pkl"), weights_only=True)
print("读回 shape:", cached.shape, "（应为 [2,4,8]，两样本沿 dim=0 拼接）")
```

3. 思考：把上面文件名里的 `attn` 换成 `mlp`，再调用 `load_ptq_inps(data_dir, "mlp", 0)`，kwargs 会是什么？

**需要观察的现象**：round-trip 后张量形状与数值完全还原；`mlp` 目标的 `kwargs` 为空字典 `{}`（因为没有进入 `if quant_target == "attn"` 分支）。

**预期结果**：存 `[2,4,8]`、读回 `[2,4,8]`；`attn` 目标会额外尝试读三个位置/掩码文件（本示例没写，故 `kwargs` 仍为 `{}`，但会进入 attn 分支去 `os.path.exists` 判断）。**实际目录可本地验证。**

#### 4.2.5 小练习与答案

**练习 1**：为什么 `load_ptq_inps` 在文件不存在时返回 `(None, kwargs)` 而不是抛异常？

**参考答案**：因为 ptq 阶段支持按 unit 粒度的断点续跑与容错。某个 unit 的输入文件缺失（例如 extract 阶段中途退出、或用户只跑了部分层），上层工作流可以据此跳过该 unit 或给出告警，而不是让整个 ptq 崩溃。这与 [u4-l2](u4-l2-ptq-main-flow.md) 讲的「逐 unit 检查、跳过已有结果」的断点续跑机制配合。

**练习 2**：假设 extract 阶段用 `--quant_target attn-linear`，ptq 阶段误写成 `--quant_target mlp`，会在哪一步暴露问题？

**参考答案**：extract 写出的是 `block_{idx}_attn_in.pkl`（归一化为 attn），而 ptq 用 `unit.kind="mlp"` 去读 `block_{idx}_mlp_in.pkl`。在 [ptq_io.py:57-62](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/datasets/ptq_io.py#L57-L62) 会 `FileNotFoundError` → 告警并返回 `None`，上层随后因拿不到输入而失败。这正是「两阶段 quant_target 必须一致」的底层原因。

---

### 4.3 训练 batch 构建器：LlmPtqDataProvider

#### 4.3.1 概念说明

读回了输入激活还不够。`BlockwiseSolver`（[u4-l3](u4-l3-blockwise-solver.md)）的训练循环需要的是「按 batch 迭代的 (输入, 目标) 对」：每个 batch 喂进去一批输入，量化模块产出 `quant_out`，再与 `ground truth` 算重建损失。`LlmPtqDataProvider` 就是把原始张量改造成这种训练友好形式的「组装车间」。

它做三件事：

1. **`load_unit_inputs`**：委托 `pipeline.load_unit_inputs` → `load_ptq_inps`，读回某 unit 的输入（与 kwargs）。
2. **`materialize_gt`**：用**原始浮点模块**对读回的输入跑一遍前向，产出 ground truth（重建目标）。
3. **`build_unit_batch`**：把 `(inps, gts)` 包成 `TensorDataset` + `DataLoader`，连同 kwargs、元数据封进 `BlockPtqBatch`。

> 关键术语：**ground truth（GT）**。PTQ 的训练目标不是「拟合标签」，而是「让量化后的子模块输出 ≈ 没量化的子模块输出」。GT 就是后者——在 float32 下、用同一份输入算出的原始输出。它一旦在 `_prepare_unit_batch` 里算好就固定不变，训练过程只调整量化算法的可学习参数去逼近它（详见 [u4-l3](u4-l3-blockwise-solver.md) 的自归一化重建损失）。

#### 4.3.2 核心流程

`LlmPtqDataProvider` 的三个方法由 ptq 工作流的 `_prepare_unit_batch` 串联调用：

```text
_prepare_unit_batch(unit)                      # 在 llm_ptq.py:156
  │
  ├─ load_unit_inputs(unit)
  │     └─ pipeline.load_unit_inputs(data_dir, unit)
  │           └─ load_ptq_inps(data_dir, unit.kind, unit.layer_idx)
  │                 → (inps, kwargs)
  │
  ├─ set_model_to_observe(unit.module, True)   # 生成 GT 时切到 observe 态
  ├─ materialize_gt(inps, unit.module, kwargs)
  │     │  按 cali_bsz 分批、no_grad 前向
  │     └─→ gts = cat(各批输出)                # float32 重建目标
  ├─ set_model_to_observe(unit.module, False)  # 切回量化态（详见 u6-l1）
  │
  └─ build_unit_batch(unit, inps, kwargs, gts)
        │  TensorDataset(inps, gts)
        │  DataLoader(batch_size=cali_bsz)
        └─→ BlockPtqBatch(data_loader, kwargs, ...)
              │
              └─ 交给 BlockwiseSolver.solve(data_loader, forward_kwargs=kwargs)
```

说明：`set_model_to_observe` 控制 observe 通路（校准态 vs 量化态），其底层机制在 [u6-l1](u6-l1-algo-base-observe.md) 详述。本讲只需知道：生成 GT 时切到 observe 态、训练前切回，由 `try/finally` 保证即使出错也能复位。

#### 4.3.3 源码精读

**`BlockPtqBatch` 数据类**——一次 PTQ unit 训练的完整数据包：

[ptq_provider.py:29-37](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/datasets/ptq_provider.py#L29-L37) —— `data_loader` 是可迭代 batch 源；`kwargs` 是前向所需的 position/mask（attn 目标）或空字典；`num_samples` 是样本总数；`has_gts` 标记是否带 ground truth；`metadata` 透传 `unit.metadata`（MoE 场景下含 `expert_idx`）。

**`build_unit_batch`**——把张量包成 DataLoader：

[ptq_provider.py:51-71](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/datasets/ptq_provider.py#L51-L71) —— 几个要点：

- [ptq_provider.py:52-54](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/datasets/ptq_provider.py#L52-L54)：`tensors = [inps]`，有 GT 就 `append(gts)`。`TensorDataset(*tensors)` 要求两个张量第 0 维长度一致——inps 与 gts 都是按样本维（dim=0）对齐的。
- [ptq_provider.py:56-62](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/datasets/ptq_provider.py#L56-L62)：`DataLoader` 用 `batch_size=self.args.cali_bsz`（CLI 默认 4，见 [args.py:97](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/cli/llm/args.py#L97)），`shuffle=False`（保持顺序、可复现）、`num_workers=0`（单进程，避免张量拷贝开销）。

**`materialize_gt`**——跑浮点前向生成重建目标：

[ptq_provider.py:73-91](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/datasets/ptq_provider.py#L73-L91) —— 要点：

- [ptq_provider.py:74](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/datasets/ptq_provider.py#L74)：`ori_module.float().eval().to(self.device)`——把模块转 **float32**、置 eval（关 dropout/BN 更新）、搬到目标设备。用 float32 是为了让 GT 尽量接近「真值」，避免 bf16 的精度损失污染重建目标。
- [ptq_provider.py:76-78](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/datasets/ptq_provider.py#L76-L78)：同样按 `cali_bsz` 分批（与 `build_unit_batch` 用同一个 batch size），避免一次前向爆显存。
- [ptq_provider.py:80-89](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/datasets/ptq_provider.py#L80-L89)：`torch.no_grad()` 下逐批前向；输入若是浮点则转 float32；模块输出可能是 tuple/list（如返回 `(hidden_state, ...)`），统一取第 0 个；`.detach()` 切断计算图后收集。
- [ptq_provider.py:90-91](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/datasets/ptq_provider.py#L90-L91)：`torch.npu.empty_cache()` 回收显存，再 `torch.cat(gts, dim=0)` 沿样本维拼成完整 GT 张量。

**工作流侧的串联**——`_prepare_unit_batch`：

[llm_ptq.py:156-170](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/workflows/llm_ptq.py#L156-L170) —— 把三步串起来：`load_unit_inputs` 拿 `(inps, kwargs)` → `_move_to_device` 搬设备 → `try/finally` 包裹 `materialize_gt`（保证 observe 复位）→ `build_unit_batch` 组装。注意 [llm_ptq.py:158-161](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/workflows/llm_ptq.py#L158-L161) 对返回值做了兼容：`load_unit_inputs` 既可能返回 `(inps, kwargs)` 元组，也可能只返回 `inps`（此时 `kwargs={}`）。

#### 4.3.4 代码实践

**实践目标**：搞清 `(inps, gts)` 的配对关系与 DataLoader 的迭代产物。

**操作步骤**：

1. 阅读 [ptq_provider.py:51-71](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/datasets/ptq_provider.py#L51-L71) 与 [ptq_provider.py:73-91](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/datasets/ptq_provider.py#L73-L91)。
2. 用一个「假模块」模拟 `materialize_gt` + `build_unit_batch`（不依赖 NPU）：

```python
# 示例代码：模拟 Provider 的 GT 生成与 batch 构建（非项目源码）
import torch
from torch.utils.data import DataLoader, TensorDataset

cali_bsz = 4
inps = torch.randn(10, 4, 8)          # 10 个样本的输入激活
fake_module = torch.nn.Linear(8, 8)    # 假装是原始浮点子模块
fake_module.eval()

# 模拟 materialize_gt：no_grad 分批前向
gts = []
with torch.no_grad():
    for i in range(0, inps.shape[0], cali_bsz):
        gts.append(fake_module(inps[i:i+cali_bsz]).detach())
gts = torch.cat(gts, dim=0)           # 对应源码 L91

# 模拟 build_unit_batch
dataset = TensorDataset(inps, gts)    # 对应源码 L55
loader = DataLoader(dataset, batch_size=cali_bsz, shuffle=False)
for batch_idx, (x, y) in enumerate(loader):
    print(f"batch {batch_idx}: 输入 {x.shape}, 目标 {y.shape}")
```

3. 思考：`inps` 与 `gts` 的第 0 维为什么必须相等？若不等 `TensorDataset` 会怎样？

**需要观察的现象**：DataLoader 产出 3 个 batch（10 个样本按 batch_size=4 切分：4+4+2），每个 batch 的 `x` 与 `y` 第 0 维相同、且 `y` 是 `x` 经 `fake_module` 的输出。

**预期结果**：`inps.shape[0] == gts.shape[0] == 10`；若不等，`TensorDataset` 会在构造时抛 `RuntimeError`（要求所有张量第 0 维长度一致）。**上述示例可本地验证。**

#### 4.3.5 小练习与答案

**练习 1**：`materialize_gt` 为什么要用 `.float()` 把模块转成 float32，而不是保持 bf16？

**参考答案**：GT 是 PTQ 训练的「真值锚点」，训练全程都在逼近它。若 GT 本身用 bf16 算，会引入 bf16 的舍入误差，让重建目标本身就不准，相当于「用一把刻度模糊的尺子校准」。转 float32 让 GT 尽量接近真实浮点输出，剩下的误差才真正来自量化本身。

**练习 2**：`build_unit_batch` 里 `shuffle=False`，为什么 PTQ 训练不需要打乱数据？

**参考答案**：PTQ 的「一个 unit」只对应一层的一个子模块，校准样本之间没有时序依赖也没有标签分布问题，batch 顺序不影响重建损失的期望。保持 `shuffle=False` 让每次训练完全可复现，便于调试与断点续跑（与 [u4-l2](u4-l2-ptq-main-flow.md) 讲的 unit 粒度断点续跑配合）。

---

## 5. 综合实践

把本讲三个模块串起来，画出校准数据的**完整生命周期数据流图**，并用一句话标注每个节点的「产出物给谁用」。

**实践目标**：建立「原始文本 → 训练 DataLoader」的全景视图，验证自己对三段衔接的理解。

**操作步骤**：

1. 准备一张白纸（或文本编辑器），画出下面 8 个节点，用箭头连起来并标注产出文件/对象：

```text
(1) pileval 语料
      │ get_pileval / pileval_awq          产出：list[Tensor], 每条 [1, seq_len]
      ▼
(2) 校准样本 samples
      │ extract 的 do_embedding_forward     捕获第 0 层 position/mask
      ▼
(3) position_ids/embeddings/attention_mask
      │ save_ptq_kwargs                     产出：*.pkl（供 ptq 的 attn 目标读回）
      ▼
(4) 逐层 do_block_forward + hook
      │ save_ptq_inps                        产出：block_{idx}_{attn|mlp|moe}_in.pkl
      ▼
(5) 磁盘上的中间激活文件（extract 与 ptq 的接力棒）
      │ ptq 的 load_unit_inputs → load_ptq_inps
      ▼
(6) (inps, kwargs) 读回内存
      │ materialize_gt（observe=True，float32 前向）
      ▼
(7) ground truth gts
      │ build_unit_batch（TensorDataset + DataLoader）
      ▼
(8) BlockPtqBatch.data_loader
      │ 交给 BlockwiseSolver.solve 训练
```

2. 对每个节点写一句话：「它的产出给下一阶段（或哪个函数）用什么」。例如节点 (5) → 「给 ptq 阶段的 `load_unit_inputs` 用，按 `unit.kind` 定位文件」。
3. 进阶（可选）：在图上标出三个「契约约束」——(a) extract 与 ptq 的 `quant_target` 必须一致；(b) `attn-linear`/`attn-cache` 归一化为 `attn`；(c) `inps` 与 `gts` 第 0 维必须对齐。

**需要观察的现象**：你能不查源码地说出每一步对应的函数名与文件名格式。

**预期结果**：得到一张可独立讲解的数据流图。若某一步说不出函数名或文件名，回到对应小节（4.1 / 4.2 / 4.3）的源码精读复习。这是后续学习 [u6-l1（observe 通路）](u6-l1-algo-base-observe.md) 与 [u7-l1（QuantLinear）](u7-l1-quant-modules.md) 的数据层基础。

## 6. 本讲小结

- `preproc.get_pileval` 用「收集 token → 拼成长河 → 定长重切」三步，把 pileval 文本变成 `nsamples × [1, seq_len]` 的零 padding 校准样本，token 预算为 \(\text{n\_samples} \times \text{seq\_len}\)。
- `ptq_io` 是 extract 与 ptq 两阶段间的**磁盘契约层**：`save_ptq_inps` 写 `block_{idx}_{target}_in.pkl`，`save_ptq_kwargs` 写三个位置/掩码文件；`attn-linear`/`attn-cache` 在写侧归一化为 `attn`，读侧用 `unit.kind` 对齐。
- `load_ptq_inps` 仅对 `attn` 目标额外读位置/掩码 kwargs，主输入缺失时返回 `(None, kwargs)` 而非报错，配合断点续跑；`weights_only=True` 保证反序列化安全。
- `LlmPtqDataProvider.materialize_gt` 用 float32、eval 态、no_grad 跑原始模块前向，产出重建目标 GT；`build_unit_batch` 把 `(inps, gts)` 包成 `batch_size=cali_bsz`、`shuffle=False` 的 DataLoader，封进 `BlockPtqBatch`。
- 整条链路是「**数据搬运与打包**」：datasets 层不做量化、不认识模型结构，模型相关操作都委托给 `pipeline`，自己只处理张量与文件。
- MoE 场景下同一层所有 expert 共享同一个 `block_{idx}_moe_in.pkl`（因 `unit.kind` 都是 `moe`），但各有独立 GT 与 `.pt` 结果。

## 7. 下一步学习建议

本讲把「数据怎么流」讲透了，但还有两个相邻问题待解：

- **observe 通路到底怎么切换？** `materialize_gt` 外层的 `set_model_to_observe(module, True/False)` 是如何让同一个模块在校准态（统计）与量化态（伪量化）间切换的？请继续学习 [u6-l1：QuantAlgorithmBase 与 is_observe 通路](u6-l1-algo-base-observe.md)。
- **GT 喂进求解器后怎么训练？** `BlockPtqBatch.data_loader` 被 `BlockwiseSolver.solve` 消费后，重建损失如何反传、可学习参数如何更新？回顾 [u4-l3：块级重建优化 BlockwiseSolver](u4-l3-blockwise-solver.md)，并接着读 [u7-l1：QuantLinear 与量化器模块](u7-l1-quant-modules.md) 看量化模块的 forward 如何消费这些 batch。
- 若想看「数据如何被烘焙进部署权重」，可跳读 [u4-l4：部署导出 deploy](u4-l4-deploy-export.md) 了解 GT 与训练参数在 deploy 阶段的归宿。
