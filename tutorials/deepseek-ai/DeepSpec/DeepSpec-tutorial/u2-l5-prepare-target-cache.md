# prepare_target_cache：用 forward hook 抓取目标中间层

## 1. 本讲目标

学完本讲，你应该能够：

1. 解释 `register_forward_hook` 的工作机制，以及它如何精确截取指定 decoder 层的**原始输出**（raw decoder-layer output）。
2. 说明 `-1` 层（embedding 输出）与普通层在注册方式、语义上的区别。
3. 说出为什么本脚本**不用** `output_hidden_states` 抓中间层（关键线索在 `base_evaluator.assert_no_final_target_layer` 的注释里）。
4. 描述一条样本从 JSONL 行 → tokenizer/loss_mask → 目标模型 forward → 按 `seq_len` 切片 → 分片落盘 → 全局索引/manifest 收尾的完整写入路径。
5. 理解本脚本的容错设计：临时目录、原子落盘、`fsync`、非空目录拒绝，以及它与上一步 `generate_train_data.py --resume` 不同的「失败即整体重来」哲学。

本讲在数据流水线中的位置：输入是 [u2-l3](u2-l3-regenerate-answers.md) 产出的重生成 JSONL（如 `perfectblend_train_regen.jsonl`），输出是 [u2-l4](u2-l4-target-cache-format.md) 讲过的存储协议目录（`manifest.json` + `samples.idx` + `shard-*.bin`）。u2-l4 讲的是「协议长什么样」，本讲讲「协议是怎么被写出来的」。

## 2. 前置知识

### 2.1 回顾：为什么需要这份缓存

DeepSpec 训练的草稿模型不以 token embedding 为主要输入，而是以**目标模型若干中间层的隐状态拼接**为输入特征（见 [u1-l1](u1-l1-project-overview.md)）。如果每个训练 step 都现跑一遍 4B/14B 的目标模型，训练会被目标模型的前向彻底拖慢。因此数据阶段一次性把全训练集过一遍目标模型，把需要的隐状态落盘，训练阶段只读缓存——代价是默认配置下约 38 TB 磁盘（见 [scripts/data/README.md:115-121](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/scripts/data/README.md#L115-L121) 的存储警告）。

### 2.2 PyTorch 的模块树与 forward hook

- PyTorch 模型是一棵 `nn.Module` 树。以 Qwen3 为例，`Qwen3Model`（backbone）下挂 `embed_tokens`（词嵌入）和 `layers`（`nn.ModuleList`，装着一个个 `Qwen3DecoderLayer`）。
- `module.register_forward_hook(hook)` 会在该模块的 `forward` **返回之后**自动调用 `hook(module, inputs, output)`。我们只要给第 \( i \) 层挂上 hook，就能在不停下整模型 forward 的情况下"顺手"拿走这一层的输出。
- hook 返回一个 `RemovableHandle`，用完必须 `handle.remove()`，否则重复注册会越积越多、拖慢并污染后续 forward。
- decoder 层的返回值通常是 tuple（如 `(hidden_states, attn_weights)`），而 `nn.Embedding` 的返回值是裸张量——取张量时要区分这两种情况。

### 2.3 每卡一进程的写入视图

脚本入口与 train.py 同款（见 [u1-l3](u1-l3-entry-points.md)）：`torch.multiprocessing.spawn` 按 `torch.cuda.device_count()` 拉起进程，每个 GPU 一个 rank。**写入期各 rank 只写自己的临时目录 `_tmp/rank_<r>/`，互不干扰**；全部写完后才由 rank 0 收集汇总、统一改名合并。理解"先本地、后全局"这个两阶段结构，是读懂收尾代码的钥匙。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [scripts/data/prepare_target_cache.py](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/scripts/data/prepare_target_cache.py) | 缓存生成主脚本：加载配置与目标模型、hook 抓层、按 rank 写分片、收尾合并索引与 manifest |
| [deepspec/data/target_cache_dataset.py](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/data/target_cache_dataset.py) | 存储协议唯一权威（u2-l4 已讲读取侧），本讲精读其中的**写入器**与**收尾合并**函数 |
| [deepspec/modeling/dspark/common.py](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/common.py) | `validate_target_layer_ids`（含 `-1` 语义定义）与 `extract_context_feature`（eval 侧的对照实现） |
| [deepspec/eval/base_evaluator.py](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/base_evaluator.py) | `assert_no_final_target_layer`：解释"为什么不用 output_hidden_states"的第一手注释 |
| [config/dspark/dspark_qwen3_4b.py](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/config/dspark/dspark_qwen3_4b.py) | 运行时读的配置：`target_layer_ids=[1, 9, 17, 25, 33]`、`chat_template="qwen"`、`max_length=4096` |
| [scripts/data/README.md](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/scripts/data/README.md) | Step 3 的官方运行命令与 38 TB 存储警告 |

运行方式（摘自 README Step 3，[scripts/data/README.md:108-113](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/scripts/data/README.md#L108-L113)）：

```bash
CUDA_VISIBLE_DEVICES=0,1,2,3,4,5,6,7 python scripts/data/prepare_target_cache.py \
    --config config/dspark/dspark_qwen3_4b.py \
    --train-data-path train_datasets/qwen3_4b/perfectblend_train_regen.jsonl \
    --output-dir ${HOME}/.cache/deepspec/qwen3_4b_target_cache \
    --local-batch-size 16
```

## 4. 核心概念与源码讲解

### 4.1 register_forward_hook 捕获中间层

#### 4.1.1 概念说明

这个模块解决的问题是：**目标模型的 forward 只天然暴露最终输出，而草稿模型需要的是中间若干层的原始输出**。

备选方案是 `output_hidden_states=True`，它会让模型返回全部 \( L+1 \) 个隐状态（embedding + 每层输出）。本仓库放弃它、改用 hook，有两个原因：

1. **语义不一致（决定性原因）**：在 transformers 的 llama 系实现（Qwen3 同构）里，`hidden_states` 元组的最后一个位置存的不是最后一层的原始输出，而是**过了 final norm 之后**的隐状态（它同时就是 `last_hidden_state`）。而 target cache 协议存的是 raw decoder-layer 输出。若 `target_layer_ids` 里含最后一层，用 `output_hidden_states` 取到的特征就和缓存里的特征悄悄差了一个 norm——训练与评估会吃到不一致的输入。[deepspec/eval/base_evaluator.py:100-112](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/base_evaluator.py#L100-L112) 的断言把这条坑写成了显式错误信息（eval 侧确实用 `output_hidden_states=True`，见 [deepspec/eval/base_evaluator.py:217-223](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/base_evaluator.py#L217-L223)，所以必须禁止选最后一层）。
2. **开销**：`output_hidden_states` 会物化全部 \( L+1 \) 份全尺寸张量（Qwen3-4B 是 37 份），而 hook 只捕获配置点名的 5 层。

顺带一提 eval 侧的对照实现：评估时从 `output.hidden_states` 取特征用的映射是 `hidden_states[0 if layer_id == -1 else layer_id + 1]`，见 [deepspec/modeling/dspark/common.py:52-56](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/common.py#L52-L56)——索引整体偏移 1 是因为 `hidden_states[0]` 是 embedding 输出。这从侧面印证了两种取法的对应关系：hook 抓第 \( i \) 层 \( \Leftrightarrow \) `hidden_states[i+1]`（\( i \neq L-1 \) 时）。

#### 4.1.2 核心流程

`run_target_forward_with_hooks` 的一次调用：

```text
输入: target_model, input_ids, attention_mask, target_layer_ids（如 [1, 9, 17, 25, 33]）

1. 取 backbone（Qwen3: model.model；Gemma4: language_model）
2. captured = {}; handles = []
3. 若 -1 ∈ target_layer_ids:
     给 backbone.embed_tokens 挂 hook → 捕获到 captured[-1]
4. 对每个 layer_id ≥ 0:
     给 backbone.layers[layer_id] 挂 hook → 捕获到 captured[layer_id]
5. torch.no_grad() 下跑一次完整 forward（output_hidden_states=False, use_cache=False）
6. target_hidden_states = cat([captured[id] for id in target_layer_ids], dim=-1)
   → 形状 (B, T, K*H)，例如 Qwen3-4B: K=5, H=2560 → 12800
7. target_last_hidden_states = output.last_hidden_state
8. finally: 移除所有 hook，清空 captured
```

注意第 6 步的拼接顺序严格等于 `target_layer_ids` 的升序——这正是 u2-l4 讲过的"K 层拼接 hidden"的来源，也是 manifest 里 `target_layer_ids` 必须升序的原因。

#### 4.1.3 源码精读

**取 backbone 与 hidden_size（处理 Gemma4 多模态包装）**。[scripts/data/prepare_target_cache.py:55-70](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/scripts/data/prepare_target_cache.py#L55-L70)：Qwen3 走 `getattr(target_model, "model", target_model)`；Gemma4（含 `gemma4_unified`）可能被多模态外壳包着，要一路下钻到 `language_model`，hidden_size 也从 `config.text_config` 取。

```python
def _get_target_backbone(target_model):
    model_type = str(target_model.config.model_type)
    if model_type in ("gemma4", "gemma4_unified"):
        if hasattr(target_model, "language_model"):
            return target_model.language_model
        ...
    return getattr(target_model, "model", target_model)
```

**hook 输出的形状归一**。[scripts/data/prepare_target_cache.py:73-80](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/scripts/data/prepare_target_cache.py#L73-L80)：decoder 层返回 tuple 时取第一个张量，`embed_tokens` 返回裸张量直接用，其余类型直接报错——宁可失败也不猜。

**hook 的注册与捕获（本讲核心）**。[scripts/data/prepare_target_cache.py:96-112](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/scripts/data/prepare_target_cache.py#L96-L112)：

```python
def capture_layer(layer_id: int):
    def hook(_module, _inputs, output):
        captured_hidden_states[layer_id] = _get_hook_tensor(output).detach()
    return hook

...
if -1 in target_layer_ids:
    handles.append(backbone.embed_tokens.register_forward_hook(capture_layer(-1)))
for layer_id in target_layer_ids:
    if layer_id < 0:
        continue
    handles.append(layer_modules[layer_id].register_forward_hook(capture_layer(layer_id)))
```

要点：

- 用闭包工厂 `capture_layer(layer_id)` 给每个 hook "绑定"自己的层号，捕获结果写进同一个 dict。
- **`-1` 层挂在 `embed_tokens` 上而不是某个 decoder 层上**。`-1` 是哨兵值，语义由 [deepspec/modeling/dspark/common.py:59-75](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/common.py#L59-L75) 的校验函数写死：合法取值是 \(\{-1\} \cup [0, L-1]\)，且必须严格递增——所以 `-1` 只能排最前。它对应 `output_hidden_states[0]`。
- `.detach()` 切断与计算图的关联，配合外层 `no_grad` 确保捕获的张量不带 autograd 历史。

**forward 与拼接**。[scripts/data/prepare_target_cache.py:114-125](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/scripts/data/prepare_target_cache.py#L114-L125)：

```python
with torch.no_grad():
    target_output = target_model(
        input_ids=input_ids, attention_mask=attention_mask,
        output_hidden_states=False, use_cache=False,
    )
    target_last_hidden_states = target_output.last_hidden_state.detach()
    target_hidden_states = torch.cat(
        [captured_hidden_states[layer_id] for layer_id in target_layer_ids], dim=-1,
    )
```

显式传 `output_hidden_states=False` 呼应 4.1.1：不要那份又大又在末层位置语义不同的默认全家桶；`use_cache=False` 因为这是纯预计算，不需要 KV cache。`last_hidden_state`（过 final norm 的最终输出）单独存一份，写入缓存的 `target_last_hidden_states` 段（评估期验证草稿要用，见 u6 单元）。

**finally 清理**。[scripts/data/prepare_target_cache.py:126-129](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/scripts/data/prepare_target_cache.py#L126-L129)：无论 forward 是否抛异常都移除全部 handle 并清空 dict。hook 是**每个 batch 注册一次、用完即拆**的临时装置，不是装一次管到底。

#### 4.1.4 代码实践

**实践目标**：在不下载任何权量的前提下，亲手验证 4.1.1 的两个论断——(a) hook 抓的中间层输出与 `output_hidden_states` 在非末层位置逐位相等；(b) 在末层位置二者**不相等**（差一个 final norm）。

**操作步骤**（示例代码，仅需 `transformers` + `torch`，可离线运行）：

```python
# toy_hook_demo.py —— 示例代码：用随机权重的小 Qwen3 验证 hook vs output_hidden_states
import torch
from transformers import AutoModel, Qwen3Config

cfg = Qwen3Config(vocab_size=100, hidden_size=64, intermediate_size=128,
                  num_hidden_layers=4, num_attention_heads=4, num_key_value_heads=2)
model = AutoModel.from_config(cfg).eval()
backbone = model  # Qwen3Model 自带 .layers / .embed_tokens

captured, handles = {}, []
def capture(layer_id):
    def hook(_m, _i, output):
        t = output if isinstance(output, torch.Tensor) else output[0]
        captured[layer_id] = t.detach()
    return hook

handles.append(backbone.embed_tokens.register_forward_hook(capture(-1)))
for lid in (0, 3):                      # 3 = 最后一层（num_hidden_layers-1）
    handles.append(backbone.layers[lid].register_forward_hook(capture(lid)))

x = torch.randint(0, 100, (1, 10))
with torch.no_grad():
    out = model(input_ids=x, output_hidden_states=True, use_cache=False)
for h in handles:
    h.remove()

print("embed  hook:", captured[-1].shape)          # 期望 (1, 10, 64)
print("layer0 hook:", captured[0].shape)           # 期望 (1, 10, 64)
print("hs[0] == hook(-1): ", torch.equal(out.hidden_states[0], captured[-1]))   # 期望 True
print("hs[1] == hook(0):  ", torch.equal(out.hidden_states[1], captured[0]))    # 期望 True
print("hs[4] == hook(3):  ", torch.equal(out.hidden_states[4], captured[3]))    # 期望 False！
print("hs[4] == last_hidden_state:", torch.equal(out.hidden_states[4], out.last_hidden_state))  # 期望 True
```

**需要观察的现象**：前三组 `torch.equal` 的输出，以及末层那一组 `False`。

**预期结果**：`hidden_states[4]`（末层槽位）等于 `last_hidden_state`（norm 后），不等于 hook 抓到的第 3 层原始输出；非末层则逐位相等。若与你本地 transformers 版本的行为不符，请以本地实测为准并检查该版本的 `Qwen3Model.forward` 实现（**待本地验证**）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 hook 必须在 `finally` 里 `remove()`？如果不移除，连续跑 1000 个 batch 会发生什么？
**答案**：hook 会永久挂在模块上。每个 batch 都会触发捕获、`captured` dict 被反复覆盖，handle 列表无限增长；即便外层重建 dict，残留的 hook 仍会在每次 forward 时执行闭包逻辑，拖慢速度，且若闭包引用了已释放的对象还可能报错。注册-移除配对是使用 hook 的基本纪律。

**练习 2**：`target_layer_ids=[-1, 9, 17, 25, 33]` 合法吗？`[9, -1, 17]` 呢？
**答案**：前者合法：`-1` 表示 embedding 输出，且严格递增要求它只能排在最前（校验见 [deepspec/modeling/dspark/common.py:59-75](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/common.py#L59-L75)）。后者非法：不是严格递增，`validate_target_layer_ids` 会 assert 失败。

**练习 3**：拼接后的 `target_hidden_states` 最后一维是 \( K \times H \)。Qwen3-4B（\( H=2560 \)）取 5 层时是多少？这个数字和 u2-l4 的哪个 manifest 字段对得上？
**答案**：\( 5 \times 2560 = 12800 \)。对上 `manifest.json` 的 `hidden_size` 字段——协议里的 `hidden_size` 存的就是拼接后的宽度（训练时 `validate_train_cache` 用它对比草稿模型 `config.hidden_size`，见 [deepspec/data/target_cache_dataset.py:210-213](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/data/target_cache_dataset.py#L210-L213)）。

### 4.2 分片写入与 manifest 生成

#### 4.2.1 概念说明

这个模块解决的问题是：**hook 抓到的 GPU 张量，如何变成 u2-l4 协议里那个"manifest + 定长索引 + 分片字节流"的目录**。三个子问题：

1. **吞吐**：GPU 算力宝贵，D2H 拷贝与磁盘写入不能阻塞 forward 主循环 → 异步写线程。
2. **分片**：单文件不能无限大（默认每片上限 64 GiB）→ 滚动切分，并给每片编号。
3. **多 rank 合并**：8 个进程各写各的临时目录，最终要变成一份全局稠密、按源数据顺序编号的索引。

#### 4.2.2 核心流程

**写入期（每个 rank 独立执行）**：

```text
main() 每个 batch:
  collator 产出 padded batch（input_ids/attention_mask/loss_mask）
  → 整 batch 搬到 GPU
  → run_target_forward_with_hooks 抓 K 层 + last_hidden
  → 对 batch 内每个样本: seq_len = attention_mask.sum()
       切掉右侧 padding: tensor[:seq_len]
  → AsyncTargetCacheWriter.write_sample(...)   # 内部先转 CPU 字节再入队
后台写线程:
  从队列取 TargetCacheSampleBytes
  → 当前分片装不下? 开新分片 shard-local-XXXXX.bin
  → 五段顺序追加写入，记录五元偏移
  → 向 samples.local.idx 追加一条 56 字节索引记录
```

**收尾期（三步走，全部由 barrier 同步）**：

```text
1. 每个 rank: 写 summary.json（样本区间、分片清单）→ barrier
2. rank 0: 读全部 summary，按 source_sample_start 排序，分配全局分片号
   → broadcast shard_map 给所有 rank
   → 每个 rank 把自己的 shard-local-*.bin 原子改名为全局 shard-*.bin → barrier
3. 仅 rank 0: 按序拼接各 rank 的本地索引，重写 sample_id/shard_id 为全局值
   → samples.idx.tmp 原子替换为 samples.idx
   → 构造并原子写 manifest.json
   → 删除 _tmp 目录
```

#### 4.2.3 源码精读

**主循环：抓层、切片、入队**。[scripts/data/prepare_target_cache.py:283-334](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/scripts/data/prepare_target_cache.py#L283-L334)。关键两处：

- [L306-L311](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/scripts/data/prepare_target_cache.py#L306-L311) 调用 4.1 精读过的 hook 函数；
- [L312-L327](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/scripts/data/prepare_target_cache.py#L312-L327) 用 `attention_mask.sum(dim=1)` 得到每个样本的真实长度，把 padded 张量切回变长再写盘——**缓存里存的是无 padding 的变长样本**，padding 位置的隐状态根本不会落盘（这也是 u2-l6 读取侧要重新组 batch 的原因）。

```python
seq_lens = batch["attention_mask"].sum(dim=1).tolist()
for sample_idx_in_batch, seq_len in enumerate(seq_lens):
    writer.write_sample(
        input_ids=batch["input_ids"][sample_idx_in_batch, :seq_len],
        ...
        target_hidden_states=target_result.target_hidden_states[sample_idx_in_batch, :seq_len],
    )
```

**张量 → 字节**。[deepspec/data/target_cache_dataset.py:283-302](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/data/target_cache_dataset.py#L283-L302)：`_tensor_to_bytes` 做 `CUDA → CPU → 目标 dtype → contiguous → tobytes`；bfloat16 特殊处理（[L226-L228](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/data/target_cache_dataset.py#L226-L228)）：numpy 不认 bfloat16，于是 `view(torch.uint16)` 按 16 比特位模式无损搬运——u2-l4 讲过的约定在写入端的落点就在这里。

**同步写器：滚动分片 + 记偏移**。[deepspec/data/target_cache_dataset.py:351-387](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/data/target_cache_dataset.py#L351-L387) 的 `write_sample_bytes`：先算本样本总字节数，`_ensure_shard`（[L341-L349](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/data/target_cache_dataset.py#L341-L349)）判断"当前分片已非空且再加这份会超 `max_shard_bytes`"就开新片（单个样本超过上限时独占一片）；然后按 input_ids → attention_mask → loss_mask → target_hidden_states → target_last_hidden_states 的固定顺序五段追加，每段起点记入索引记录，最后 `pack_index_record` 追加 56 字节到 `samples.local.idx`。**只存偏移不存长度**——长度由 `seq_len` 经 `expected_target_cache_tensor_nbytes` 推导（u2-l4 讲过公式 \( 6L + 2LH(K+1) \) 字节）。

**异步写器：一条队列 + 一个线程**。[deepspec/data/target_cache_dataset.py:410-432](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/data/target_cache_dataset.py#L410-L432)：

```python
# Queue CPU byte records only; never hold CUDA tensor references here.
self.queue = queue.Queue(maxsize=int(max_queue_size))
```

注释点明设计约束：**队列里只放 CPU 字节记录，绝不持有 CUDA 张量引用**。类型转换发生在调用线程的 `write_sample`（[L470-L488](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/data/target_cache_dataset.py#L470-L488)，内部先 `build_target_cache_sample_bytes` 再 `_put`），因此排队不会 pin 住显存。背压靠有界队列：满了 `_put` 每 1 秒超时重试一次（[L461-L468](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/data/target_cache_dataset.py#L461-L468)），期间顺带检查后台线程是否已抛异常（异常被线程捕获存起来，向主线程延迟重抛，[L438-L459](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/data/target_cache_dataset.py#L438-L459)）。收尾 `close()` 投递哨兵对象、join 线程，并断言"入队条数 == 落盘条数"防止静默丢样本（[L490-L502](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/data/target_cache_dataset.py#L490-L502)）。脚本里队列容量是 `local_batch_size * 4`（[scripts/data/prepare_target_cache.py:274-278](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/scripts/data/prepare_target_cache.py#L274-L278)）。

**样本切分：连续区间而非隔行交错**。[deepspec/data/target_cache_dataset.py:231-236](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/data/target_cache_dataset.py#L231-L236)：

```python
base = num_samples // world_size; remainder = num_samples % world_size
start = rank * base + min(rank, remainder)
local_count = base + (1 if rank < remainder else 0)
```

前 `remainder` 个 rank 各多扛 1 条。连续切分使得"按 `source_sample_start` 排序 rank"就等于"按源 JSONL 顺序排列样本"，全局 `sample_id` 因此与源数据行序一致。

**收尾第一步：summary 汇总与全局分片号**。[scripts/data/prepare_target_cache.py:340-363](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/scripts/data/prepare_target_cache.py#L340-L363)：每个 rank 把 `LocalCacheWriteSummary`（定义在 [deepspec/data/target_cache_dataset.py:252-269](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/data/target_cache_dataset.py#L252-L269)，含样本区间与本地分片文件名清单）原子写入 `summary.json`；rank 0 读全部 summary 后调 `build_global_target_cache_shard_map`（[L510-L529](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/data/target_cache_dataset.py#L510-L529)）——按区间起点排序后依序累加分配全局分片号，得到 `shard_map: {rank: [全局号, ...]}`，经 `dist.broadcast_object_list` 广播。

**收尾第二步：本地分片改名**。[deepspec/data/target_cache_dataset.py:532-537](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/data/target_cache_dataset.py#L532-L537)：每个 rank 按映射把自己的 `shard-local-XXXXX.bin` 用 `os.replace` 改名为 `shard-XXXXX.bin`。`_tmp/rank_x/` 与输出根目录同一文件系统，rename 是原子操作，不发生数据搬运。

**收尾第三步：全局索引与 manifest**。[deepspec/data/target_cache_dataset.py:540-577](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/data/target_cache_dataset.py#L540-L577) 的 `finalize_target_cache_index`：按 rank 顺序逐条读本地索引记录，先 assert 本地 `sample_id` 从 0 连续（防错乱），再把 `sample_id` 重写为全局稠密编号、`shard_id` 经 `shard_map` 映射为全局分片号，写入 `samples.idx.tmp`，flush + fsync 后 `os.replace` 成正式 `samples.idx`。随后 [scripts/data/prepare_target_cache.py:157-199](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/scripts/data/prepare_target_cache.py#L157-L199) 的 `_write_manifest` 用 `build_target_cache_manifest`（[deepspec/data/target_cache_dataset.py:580-602](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/data/target_cache_dataset.py#L580-L602)）组装协议字段，并附加溯源信息：目标模型路径、源 JSONL 路径、`chat_template`、`max_length`、`min_loss_tokens`、项目/实验名、`git_sha`。`num_samples` 不是数出来的，而是把各 rank summary 里的 `num_local_samples` 加总（[L167-L174](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/scripts/data/prepare_target_cache.py#L167-L174)）。最后 `cleanup_target_cache_tmp_dir` 删掉 `_tmp`（[L609-L612](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/data/target_cache_dataset.py#L609-L612)），成功的缓存目录只剩 `manifest.json`、`samples.idx` 和一排 `shard-*.bin`。

#### 4.2.4 代码实践

**实践目标**：用同步写器 `LocalTargetCacheWriter` 在本地写两条玩具样本，直接解码它产出的 `samples.local.idx`，把"五段偏移 + 56 字节记录"从概念变成肉眼可见的数字。

**操作步骤**（示例代码，在仓库根目录运行）：

```python
# writer_demo.py —— 示例代码：直接驱动 LocalTargetCacheWriter
import struct, tempfile, os, torch
from deepspec.data.target_cache_dataset import (
    LocalTargetCacheWriter, INDEX_RECORD_STRUCT, unpack_index_record)

d = tempfile.mkdtemp()
w = LocalTargetCacheWriter(rank_dir=d, max_shard_bytes=10_000_000)  # 上限 10MB，两条样本不会切片
for sid, L in enumerate((5, 3)):
    w.write_sample(
        sample_id=sid,
        input_ids=torch.arange(L),
        attention_mask=torch.ones(L, dtype=torch.uint8),
        loss_mask=torch.zeros(L, dtype=torch.uint8),
        target_hidden_states=torch.randn(L, 2 * 8),   # K=2 层 × H=8
        target_last_hidden_states=torch.randn(L, 8),
    )
w.close()

raw = open(os.path.join(d, "samples.local.idx"), "rb").read()
print("record size:", INDEX_RECORD_STRUCT.size, "| total bytes:", len(raw))
for off in range(0, len(raw), INDEX_RECORD_STRUCT.size):
    print(unpack_index_record(raw, off))
print("shards:", os.listdir(d))
```

**需要观察的现象**：每条记录的五个 offset 是否首尾相接（样本 0 的 `target_last_hidden_states_offset` + \( 3 \times 8 \times 2 = 48 \) 字节应等于样本 1 记录的起点差）。

**预期结果**：`INDEX_RECORD_STRUCT.size == 56`；样本 0 的偏移序列形如 `0, 20, 25, 28, 108`（int32 的 input_ids 占 \( 5\times4=20 \) 字节，两个 mask 各 5 字节，hidden 段 \( 5\times16\times2=160 \) 字节，依此类推）；`shard-local-00000.bin` 出现在目录里。若把 `max_shard_bytes` 改成 `10`，会看到两个分片文件（**待本地验证**具体切分行为）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `AsyncTargetCacheWriter` 的注释强调"队列里绝不能放 CUDA 张量引用"？
**答案**：有界队列满时会积压。如果队列元素持有 CUDA 张量，积压的显存（每条是 batch 内单样本的全部隐状态，几 MB 到几十 MB）会在 GPU 上堆出一片不受优化器管理的驻留块，挤压目标模型与 batch 的显存预算，甚至 OOM。先在调用线程转成 CPU 字节串，队列积压的只是内存字节。

**练习 2**：全局 `sample_id` 为什么恰好等于源 JSONL 的行序？
**答案**：`compute_local_sample_range` 给每个 rank 分**连续**样本区间；收尾时 `finalize_target_cache_index` 按 `source_sample_start` 排序各 rank、依序重编号。两个"有序"叠加，全局编号与源顺序一致。若当初用隔行交错切分（rank 0 拿 0,8,16…），这个性质就没了。

**练习 3**：`manifest.json` 里的 `num_shards` 与磁盘上的 `shard-*.bin` 个数在什么情况下可能不一致？协议靠什么兜底？
**答案**：正常收尾后不会不一致；若收尾被中断（如 rank 0 写完 manifest 前 kill），可能出现改名了一半的状态。兜底是 `validate_target_cache_manifest`（[deepspec/data/target_cache_dataset.py:132-200](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/data/target_cache_dataset.py#L132-L200)）：加载时逐项校验分片 id 连续、文件存在、`samples.idx` 大小等于 `num_samples × 56`，任一不符直接 assert 失败，残缺缓存不可能被训练静默消费。

### 4.3 断点续跑：本脚本的容错设计

#### 4.3.1 概念说明

先澄清一个容易误解的点：**本脚本没有 `--resume` 参数，不支持从第 N 条样本续写**。这与上一步 `generate_train_data.py`（u2-l3）形成鲜明对比。它的容错哲学是"**要么产出一份完整且经过校验的缓存，要么什么正式产物都不留下**"，靠原子性与幂等的外部重跑保证正确性：

- 写入期所有正式名字（`shard-*.bin` 的全局名、`samples.idx`、`manifest.json`）要么不存在、要么一次原子操作完整出现；
- 中间产物全部圈在 `_tmp/` 里，一眼可辨；
- 重跑的代价是全部重算，但输入 JSONL、配置、模型固定时输出是确定的，重跑可完全替代续跑。

为什么这里可以接受"整体重来"而生成答案那步不行？因为 u2-l3 的每条样本要付出完整的 LLM 生成成本且互不依赖，续跑价值极高；而本步骤虽然也要重跑目标模型 forward，但工程上更怕的是**半成品缓存被当成品消费**——训练读到一份 `num_samples` 对不上的索引，浪费的是整轮训练。

#### 4.3.2 核心流程

```text
启动:  rank 0 调 prepare_target_cache_output_dir
       └─ 目录已存在且非空（哪怕只有 _tmp）→ FileExistsError，立即退出
写入:  崩溃 → 磁盘上只有 output_dir/_tmp/rank_*/{shard-local-*.bin, samples.local.idx}
       （manifest.json / samples.idx / shard-*.bin 均不存在）
收尾:  shard 改名(os.replace) → samples.idx.tmp → fsync → os.replace
       → manifest.json.tmp → fsync → os.replace → 删 _tmp
重跑:  清空输出目录（或换新目录）→ 从头再来
```

#### 4.3.3 源码精读

**非空目录拒绝**。[deepspec/data/target_cache_dataset.py:239-249](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/data/target_cache_dataset.py#L239-L249)：

```python
if os.path.exists(output_dir):
    existing = sorted(os.listdir(output_dir))
    if existing:
        raise FileExistsError(
            f"Target cache output dir is not empty: {output_dir}. "
            "Use a new output directory.")
```

错误信息直说"用新目录"。上次失败的 `_tmp` 残留也算"非空"，所以重跑前必须清理——这是刻意为之的防呆：防止新旧两轮写进同一目录后索引与分片互相错位。main 里由 rank 0 先建目录、`dist.barrier()` 后其余 rank 才继续（[scripts/data/prepare_target_cache.py:233-238](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/scripts/data/prepare_target_cache.py#L233-L238)）。

**原子 JSON 落盘**。[deepspec/data/target_cache_dataset.py:29-35](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/data/target_cache_dataset.py#L29-L35)：`atomic_json_dump` 写 `.tmp` → `flush` → `fsync` → `os.replace`。`os.replace` 在同一文件系统上是原子的：观察者要么看到旧文件、要么看到完整新文件，不存在写了一半的 `manifest.json`。`summary.json`（[scripts/data/prepare_target_cache.py:340-348](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/scripts/data/prepare_target_cache.py#L340-L348)）与 `samples.idx`（`finalize_target_cache_index` 末尾的 tmp+replace，[deepspec/data/target_cache_dataset.py:541-L576](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/data/target_cache_dataset.py#L541-L576)）走的都是同一模式。

**fsync 的作用**。`LocalTargetCacheWriter.close`（[deepspec/data/target_cache_dataset.py:317-327](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/data/target_cache_dataset.py#L317-L327)）与每次切分片时（[L329-L339](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/data/target_cache_dataset.py#L329-L339)）都先 `flush + fsync` 再关句柄——把 OS 页缓存刷到磁盘，机器断电也不至于留下截断的分片（只防截断不防"少了后几片"，后者由 manifest 校验兜底）。

**顺序即正确性**。收尾是一串 `dist.barrier()` 串起来的临界区（[scripts/data/prepare_target_cache.py:349-395](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/scripts/data/prepare_target_cache.py#L349-L395)）：summary 写完 → 才读汇总；shard 改名完 → 才拼全局索引；索引与 manifest 写完 → 才删 `_tmp`。任何一步被中断，磁盘上留下的都是"还没有 manifest"的未完成态，加载侧的第一道门 `load_target_cache_manifest` 的 `Missing target cache manifest` 断言（[deepspec/data/target_cache_dataset.py:123-129](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/data/target_cache_dataset.py#L123-L129)）会立刻拒绝它。

#### 4.3.4 代码实践

**实践目标**：亲手触发一次"非空目录拒绝"，并盘点成功/失败两种状态下目录里分别有什么。

**操作步骤**（示例代码）：

```python
# resume_demo.py —— 示例代码：观察非空目录拒绝与目录终态
import os, tempfile
from deepspec.data.target_cache_dataset import prepare_target_cache_output_dir

d = tempfile.mkdtemp()
prepare_target_cache_output_dir(d)          # 空目录：成功，建出 _tmp
print("after init:", sorted(os.listdir(d)))
os.makedirs(os.path.join(d, "_tmp", "rank_0"))  # 模拟一轮失败后的残留
try:
    prepare_target_cache_output_dir(d)      # 再跑一次：应被拒绝
except FileExistsError as e:
    print("rejected:", e)
```

**需要观察的现象**：第一次调用后目录里出现 `_tmp`；第二次调用抛 `FileExistsError`。

**预期结果**：与 4.3.3 的分析一致。然后对照 4.2.4 实践产出的目录（`shard-local-*.bin` + `samples.local.idx`，相当于 `_tmp/rank_x/` 内部视图）与 README 声明的最终产物清单（`manifest.json`、`samples.idx`、`shard-*.bin`），画出失败态与成功态两张目录树。

#### 4.3.5 小练习与答案

**练习 1**：假设收尾进行到"分片已改名、`samples.idx` 已写、manifest 还没写"时机器断电。重跑会发生什么？训练会读到坏数据吗？
**答案**：重跑时 `prepare_target_cache_output_dir` 发现目录非空（有 `shard-*.bin`、`samples.idx`、`_tmp`），抛 `FileExistsError`，操作者必须清目录或换新目录，从零重算。训练侧即使被错误指到该目录，`load_target_cache_manifest` 在第一步就因缺 `manifest.json` assert 失败——坏数据到不了训练。

**练习 2**：如果要把本脚本改造成支持续跑，最小改动清单是什么？最难的一关在哪里？
**答案**（开放题，要点）：需要 (1) 保留 `_tmp` 并允许非空目录；(2) 各 rank 能从 `summary.json`/本地索引推断已写样本数并跳过；(3) 数据集切分与遍历顺序跨运行保持确定（当前已满足）；(4) 处理"崩溃时队列里未落盘的样本"（ AsyncTargetCacheWriter 的 `num_local_samples` 与磁盘记录可能不一致）。最难的是 (4) 加上分片文件的截断判定：崩溃点可能落在任意字节边界，需要校验每个 `shard-local-*.bin` 的实际大小与索引记录推算值是否一致，否则续写会错位。

**练习 3**：`atomic_json_dump` 里如果去掉 `os.replace`、直接 `open(path, "w")` 写，会失去什么？
**答案**：失去原子性。进程在写到一半时崩溃会留下截断的 JSON，`json.load` 报解析错误还算幸运；更糟的是某些场景下截断文件"看起来能解析"（如刚好断在完整元素后），残缺 manifest 会被静默接受。tmp+replace 保证观察者只见完整版本。

## 5. 综合实践

这是本讲的完整代码实践（对应讲义规格的任务），把 4.1 的 hook 机制在**真实下载的小模型**上走一遍。

**实践目标**：加载一个小型 CausalLM（推荐 `Qwen/Qwen3-0.6B`，与仓库目标模型同族、backbone 属性名一致），仿照 `run_target_forward_with_hooks` 注册 forward hook，抓取 `embed_tokens` 与第 0 层的输出并打印形状；再用 `output_hidden_states` 做对照实验，用自己的话解释缓存生成为什么不用它。

**操作步骤**（示例代码）：

```python
# hook_practice.py —— 示例代码：在真实小模型上复现 run_target_forward_with_hooks
import torch
from transformers import AutoModel, AutoTokenizer

name = "Qwen/Qwen3-0.6B"
tok = AutoTokenizer.from_pretrained(name)
model = AutoModel.from_pretrained(name, dtype=torch.bfloat16).eval()  # AutoModel：不加载 lm_head

captured, handles = {}, []
def capture(layer_id):
    def hook(_m, _i, output):
        t = output if isinstance(output, torch.Tensor) else output[0]
        captured[layer_id] = t.detach()
    return hook

handles.append(model.embed_tokens.register_forward_hook(capture(-1)))  # -1 层：embedding 输出
handles.append(model.layers[0].register_forward_hook(capture(0)))      # 第 0 层

ids = tok("用一句话解释投机解码。", return_tensors="pt").input_ids
with torch.no_grad():
    out = model(input_ids=ids, output_hidden_states=True, use_cache=False)
for h in handles:
    h.remove()

L = model.config.num_hidden_layers
print("seq_len:", ids.shape[1], "| hidden_size:", model.config.hidden_size)
print("embed(-1) hook :", captured[-1].shape, captured[-1].dtype)
print("layer0    hook :", captured[0].shape, captured[0].dtype)
print("hs[0] == hook(-1):", torch.equal(out.hidden_states[0], captured[-1]))
print("hs[1] == hook(0) :", torch.equal(out.hidden_states[1], captured[0]))
last_hook = captured.get(L - 1)  # 未注册，应为 None —— 想验证末层差异可再挂一个 hook 对比 hs[L]
```

然后回答两个问题（写进你的笔记）：

1. **为什么不用 `output_hidden_states`？** 对照 [deepspec/eval/base_evaluator.py:100-112](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/eval/base_evaluator.py#L100-L112) 的注释组织答案，要点：(a) transformers 在 `hidden_states` 的**末层槽位**存的是过 final norm 的隐状态（等于 `last_hidden_state`），而 target cache 协议（hook 方案）存的是 raw decoder-layer 输出——若 `target_layer_ids` 含最后一层，两种取法特征不一致，所以该断言直接禁止选最后一层；(b) `output_hidden_states` 物化全部 \( L+1 \) 份张量，hook 只取配置点名的 \( K \) 层。
2. **`-1` 层和普通层差在哪？** 注册对象不同（`embed_tokens` vs `layers[i]`）、语义不同（embedding 查表结果 vs 残差流过完第 \( i \) 个 decoder 层的原始输出）、对应 `output_hidden_states` 的索引不同（`[0]` vs `[i+1]`，参照 [deepspec/modeling/dspark/common.py:52-56](https://github.com/deepseek-ai/DeepSpec/blob/005e03b81cec38b7da6399833d609ee89a2587f2/deepspec/modeling/dspark/common.py#L52-L56)）。

**需要观察的现象**：两个 hook 张量的形状均为 `(1, seq_len, 1024)`（0.6B 的 hidden_size 是 1024）、dtype 为 bfloat16；两组 `torch.equal` 为 `True`。

**预期结果**：形状与 dtype 如上；可额外给最后一层挂 hook，验证 `hidden_states[L] != hook(L-1)` 但 `== last_hidden_state`，即 4.1 结论在真实模型上成立（不同 transformers 版本的索引约定若有调整，以本地实测为准，**待本地验证**）。进阶：把三份张量按 `cat([captured[i] for i in [-1, 0]], dim=-1)` 拼接，检查最后一维变成 2048——这正是写入缓存的 `target_hidden_states` 段的形状。

## 6. 本讲小结

- `run_target_forward_with_hooks` 用 `register_forward_hook` 精确截取 `target_layer_ids` 各 decoder 层的**原始输出**，`-1` 是挂在 `embed_tokens` 上的哨兵层号，只能排最前；hook 按 batch 注册、`finally` 中拆除。
- 不用 `output_hidden_states` 的决定性原因：其末层槽位存的是 final norm 之后的隐状态，与缓存协议的 raw 层输出语义不一致（`assert_no_final_target_layer` 把这条坑显式断死），且它会物化全部 \( L+1 \) 层而非所需的 \( K \) 层。
- 写入路径：padded batch → hook forward → 按 `attention_mask.sum()` 切回变长 → 转为 CPU 字节（bfloat16 按 uint16 位模式搬运）→ `AsyncTargetCacheWriter` 有界队列 + 后台线程落盘；队列只放字节、绝不持有 CUDA 张量。
- 存储结构是两阶段的：写入期各 rank 只写 `_tmp/rank_<r>/`（滚动分片 + 56 字节本地索引 + `summary.json`）；收尾期 rank 0 汇总分配全局分片号、广播后各 rank `os.replace` 改名，再拼接重编号为全局 `samples.idx`，最后原子写 `manifest.json`（含 `git_sha` 等溯源字段）并删除 `_tmp`。
- 本脚本没有 `--resume`：靠"非空目录即拒绝 + tmp/原子替换/fsync + barrier 顺序 + 加载侧 manifest 校验"保证要么完整、要么明确失败；重跑需清目录，这是与 u2-l3 `--resume` 相反的工程取舍。

## 7. 下一步学习建议

缓存已经躺在磁盘上了，下一讲 [u2-l6：训练侧读取](u2-l6-cache-dataset-loading.md) 从反方向走同一条路：`CacheDataset` 如何用 mmap + 偏移量随机读取单条样本、`CacheCollator` 如何把变长样本重新组 batch、`CUDAPrefetcher` 如何用后台线程和独立 CUDA stream 把 H2D 拷贝藏进计算时间。阅读建议：先重读本讲的 `write_sample_bytes`（写入的五段顺序），再去读 `CacheDataset.__getitem__`（读取的五段顺序），你会发现它们是同一份协议的镜像；随后可以提前浏览 `deepspec/trainer/base_trainer.py` 中 `validate_train_cache` 的调用点，看训练如何确保"手里的缓存确实是为这个草稿模型配置生成的"。
