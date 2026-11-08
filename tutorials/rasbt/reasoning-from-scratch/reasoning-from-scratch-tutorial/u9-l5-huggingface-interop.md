# 与 Hugging Face 生态互通与蒸馏 checkpoint

## 1. 本讲目标

学完本讲，你应该能够：

1. 使用 `download_qwen3_distill_checkpoints` 下载作者训练好的第 8 章蒸馏 checkpoint，并知道它「是纯 PyTorch `state_dict`、不是 HF Transformers checkpoint」这一关键事实。
2. 读懂 **wrapper 方案**：`hf_wrapper.py` 如何用一层薄薄的 `PreTrainedModel` 壳把 `Qwen3Model` 包进 Hugging Face 生态，让 `model.generate(...)` 与 `transformers.Trainer` 直接可用。
3. 读懂 **export 方案**：`hf_export.py` + `hf_qwen3.py` 如何把 `.pth` 权重转存成带 `config.json`、`model.safetensors`、分词器文件的标准 HF 模型文件夹，并用 `AutoModelForCausalLM` 加载推理。
4. 说清两条路线在「权重去向、KV cache 支持、Auto 类加载、可分发性」上的取舍，并按需选择。
5. 独立完成：下载一个蒸馏 checkpoint → 走读两条路线的加载代码 → 任选一条路线在本地加载模型生成回答。

## 2. 前置知识

### 2.1 checkpoint 就是 state_dict（复习）

u9-l4 已经建立：`train_distillation` 按 epoch 把**纯权重**（`state_dict`，即「参数名 → 张量」的字典）存成 `.pth` 文件；u8-l5 也验证过 GRPO checkpoint 同样如此。因此「复用一个 checkpoint」永远是两步：**先按同样的配置新建模型，再 `load_state_dict` 灌权重**。本讲反复出现这一模式。

### 2.2 Hugging Face 生态最小常识

- **`transformers` 库**：业界最流行的模型加载/训练/推理框架。它的核心协议是 `XXX.from_pretrained(目录或仓库名)`——从一个文件夹里读 `config.json`（结构超参）、权重文件、分词器文件，自动重建模型。
- **`PreTrainedModel` / `PretrainedConfig`**：HF 模型类与配置类的基类。继承它们即可获得 `save_pretrained` / `from_pretrained` / `generate(...)`（生成循环）与 `Trainer`（训练器）的兼容性。
- **`GenerationMixin`**：提供 `model.generate(...)` 的混入类。生成循环每步会回调你模型上的 `prepare_inputs_for_generation(...)`，由你决定「这一步喂什么」。
- **Auto 类与 `trust_remote_code`**：`AutoModelForCausalLM` 按 `config.json` 里的 `auto_map` 字段找到自定义模型类。自定义代码意味着「加载时要执行别人写的 Python」，所以 HF 要求显式传 `trust_remote_code=True` 才会执行——**只对自己信任的来源打开**。
- **safetensors**：一种只存张量、不可执行代码的序列化格式，比 pickle 更安全，是 HF 当前默认的权重格式（`model.safetensors`）。
- **`PreTrainedTokenizerFast`**：HF 的分词器包装类，可从 `tokenizer.json` 构造，支持 `apply_chat_template`、`save_pretrained` 等。
- **聊天模板（chat template）**：一段 Jinja 字符串，把 `[{role, content}, ...]` 消息列表渲染成带 `<|im_start|>` 等特殊 token 的模型输入。u2-l6 里是我们在 `Qwen3Tokenizer` 里手写的 `_wrap_chat`，本讲会看到 HF 风格的等价物。

### 2.3 一个关键差异预告：KV cache 的「接口」

u2-l5 建立的 from-scratch `KVCache` 是「每层一个槽位的 Python 列表」；而 HF `generate` 内部有自己的缓存对象（如 `DynamicCache`，带 `update(...)` / `get_seq_length(...)` 方法）。两条互通路线对这个矛盾的处理截然不同——wrapper 干脆**不用缓存**（每步重算全前缀），export 则**完整适配** HF 缓存接口。这是本讲最重要的对比点。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [reasoning_from_scratch/qwen3.py](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3.py) | 全书地基：`Qwen3Model`、`Qwen3Tokenizer` 与三个下载入口；本讲关注 `download_qwen3_distill_checkpoints` |
| [ch08/05_download_training_checkpoints/README.md](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch08/05_download_training_checkpoints/README.md) | 说明蒸馏 checkpoint 家族、配套分词器与用法 |
| [ch08/05_download_training_checkpoints/usage_example.py](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch08/05_download_training_checkpoints/usage_example.py) | 「下载 → 建模型 → 灌权重 → 流式生成」的最小完整示例 |
| [ch08/06_use_via_huggingface/README.md](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch08/06_use_via_huggingface/README.md) | 两条互通路线的定位与选型建议 |
| [ch08/06_use_via_huggingface/wrapper_approach/hf_wrapper.py](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch08/06_use_via_huggingface/wrapper_approach/hf_wrapper.py) | wrapper 方案核心：`PreTrainedModel` 薄壳 + 构建函数 |
| [ch08/06_use_via_huggingface/wrapper_approach/hf_inference.py](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch08/06_use_via_huggingface/wrapper_approach/hf_inference.py) | 用 wrapper 跑 `model.generate(...)` 的命令行脚本 |
| [ch08/06_use_via_huggingface/export_approach/hf_qwen3.py](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch08/06_use_via_huggingface/export_approach/hf_qwen3.py) | export 方案的自定义 HF 架构：`Qwen3FromScratchConfig` + `Qwen3FromScratchForCausalLM` |
| [ch08/06_use_via_huggingface/export_approach/hf_export.py](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch08/06_use_via_huggingface/export_approach/hf_export.py) | 把 `.pth` 转存为标准 HF 模型文件夹的导出器 |
| [ch08/06_use_via_huggingface/export_approach/hf_inference.py](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch08/06_use_via_huggingface/export_approach/hf_inference.py) | 用 `AutoModelForCausalLM` 加载导出文件夹并生成 |

两个文件夹里还各有 `hf_trainer.py`（用 `transformers.Trainer` 继续训练），本讲只点到为止。

## 4. 核心概念与源码讲解

本讲的三个最小模块：

1. `download_qwen3_distill_checkpoints`——拿到「别人训练好的权重」。
2. wrapper 方案（`hf_wrapper.py`）——**不动权重、不落盘新格式**，给现有模型套 HF 接口的壳。
3. export 方案（`hf_export.py` + `hf_qwen3.py`）——**转存成标准 HF 模型文件夹**，换取完整生态兼容与性能。

### 4.1 模块一：download_qwen3_distill_checkpoints——复用作者训练的蒸馏 checkpoint

#### 4.1.1 概念说明

u9-l4 的 `train_distillation` 每个 epoch 存一个 `.pth`。作者把自己跑完的 6 个蒸馏 checkpoint（两位教师 × 三个 epoch）发布到了 Hugging Face Hub 的 `rasbt/qwen3-from-scratch-distill-checkpoints` 仓库，`download_qwen3_distill_checkpoints` 就是它的下载入口——底层复用 u1-l3 精读过的 `utils.download_file`（分块下载、进度条、大小比对缓存）。

两个必须记住的事实：

- **这些 checkpoint 是纯 PyTorch `state_dict`，不是 HF Transformers checkpoint**（[ch08/05 README](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch08/05_download_training_checkpoints/README.md#L3-L6) 明确强调）。想用 HF 生态加载它们，得走本讲模块二或模块三。
- **两个家族都必须配 reasoning 分词器**（[ch08/05 README:L39-L44](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch08/05_download_training_checkpoints/README.md#L39-L44)）——因为蒸馏数据是用聊天模板 + `<think>` 包装训练的（u9-l2）。

#### 4.1.2 核心流程

```
download_qwen3_distill_checkpoints(distill_type, step, out_dir)
 1. mapper 字典本地校验 distill_type（非法 → 就地 ValueError）
 2. step 转字符串并补零到 5 位（"6682" → "06682"）
 3. step 必须在 mapper[distill_type] 里（非法 → ValueError 列出合法值）
 4. 模板拼接主 URL：
    huggingface.co/rasbt/qwen3-from-scratch-distill-checkpoints/
      resolve/main/{文件夹}/{文件名}
 5. download_file(primary, out_dir) → 返回本地路径（已有同尺寸文件则跳过）
```

与 `download_qwen3_small` 不同，这里**没有备用 URL**（Backblaze 通道），也没有 `tokenizer_only` 开关——它只管权重文件，分词器仍由 `download_qwen3_small(kind="reasoning", tokenizer_only=True)` 另行获取。

#### 4.1.3 源码精读

**mapper：家族 × 步数的白名单。** [reasoning_from_scratch/qwen3.py:L496-L516](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3.py#L496-L516) 定义了两张表：`mapper` 把「教师 × 步数」映射到具体文件名（文件名里带着 epoch 信息，如 `qwen3-0.6B-distill-step06682-epoch1.pth`），`folder_map` 把教师映射到仓库名。这一设计沿用 u1-l3 总结的「mapper 本地校验 + 模板拼 URL」模式：

```python
mapper = {
    "deepseek_r1": {
        "06682": "qwen3-0.6B-distill-step06682-epoch1.pth",
        "13364": "qwen3-0.6B-distill-step13364-epoch2.pth",
        "20046": "qwen3-0.6B-distill-step20046-epoch3.pth",
    },
    "qwen3_235b_a22b": {
        "05746": "qwen3-0.6B-distill-step05746-epoch1.pth",
        ...
    },
}
```

**校验与 URL 拼接。** [reasoning_from_scratch/qwen3.py:L517-L531](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3.py#L517-L531)：先校验 `distill_type`，再把 `step` 字符串化并 `zfill(5)`（所以传 `"6682"` 或 `6628` 的数字形式都能对上 `"06682"`），随后校验 step 合法性，最后拼主 URL 并调用 `download_file`（注意结尾没有 `backup_url` 参数）并返回本地路径。

**消费端：usage_example.py 的四步流水线。** [ch08/05_download_training_checkpoints/usage_example.py:L79-L90](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch08/05_download_training_checkpoints/usage_example.py#L79-L90) 是「重建模型 → 灌权重」的标准姿势：

```python
checkpoint_path = download_qwen3_distill_checkpoints(...)   # 1. 下载
tokenizer = build_tokenizer(args.local_dir)                 # 2. reasoning 分词器
model = Qwen3Model(QWEN_CONFIG_06_B)                        # 3. 新建模型
state_dict = torch.load(checkpoint_path, map_location=device)
model.load_state_dict(state_dict)                           # 4. 灌权重
```

其中 [build_tokenizer（L24-L31）](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch08/05_download_training_checkpoints/usage_example.py#L24-L31) 用 `download_qwen3_small(kind="reasoning", tokenizer_only=True)` 取分词器，并打开 `apply_chat_template=True, add_generation_prompt=True, add_thinking=True`——即 u2-l6 的「模型自写 `<think>`」形态。[L104-L111](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch08/05_download_training_checkpoints/usage_example.py#L104-L111) 再用第 2 章的 `generate_text_basic_stream_cache` 流式贪心生成（KV 缓存版），EOS 由分词器决定（reasoning → `<|im_end|>`）。

#### 4.1.4 代码实践

**实践目标**：验证下载、缓存与参数校验三件事。

**操作步骤**：

1. 在仓库根目录起 Python（确保已按 u1-l2 安装好 `reasoning_from_scratch`）：

```python
from reasoning_from_scratch.qwen3 import download_qwen3_distill_checkpoints

# 第一次：真实下载（约 1.5 GB 量级：0.75B 参数 × 2 字节 bfloat16，具体以实际为准）
p = download_qwen3_distill_checkpoints(distill_type="deepseek_r1", step="06682", out_dir="qwen3")
print("saved to:", p)

# 第二次：重复调用，观察缓存行为
p2 = download_qwen3_distill_checkpoints(distill_type="deepseek_r1", step="06682", out_dir="qwen3")
print("second call returned:", p2)

# 第三次：非法 step，观察报错
try:
    download_qwen3_distill_checkpoints(distill_type="deepseek_r1", step="99999", out_dir="qwen3")
except ValueError as e:
    print("ValueError:", e)
```

2. 不想下载权重时，第三步可单独做（`99999` 会在任何下载发生之前就被拦下）。

**需要观察的现象**：

- 第一次调用打印分块下载进度；第二次调用应**秒回且不再下载**（u1-l3：本地大小与远端 `Content-Length` 相等即命中缓存）。
- 非法 step 抛出的 `ValueError` 信息里**列出合法步数集合**。

**预期结果**：两次返回的路径相同（`qwen3/qwen3-0.6B-distill-step06682-epoch1.pth`）；报错信息形如 `only step in dict_keys(['06682', '13364', '20046']) are supported for deepseek_r1`。缓存命中的具体提示文字「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：为什么这个函数没有备用 URL，而 `download_qwen3_small` 有？

**答案**：备用通道（Backblaze）是为「跑通全书主线」的最常用文件准备的容灾；蒸馏 checkpoint 属于可选的复用产物，下载人数与优先级都更低，作者只托管在 HF Hub 一处。对比 [download_qwen3_small:L453-L457](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3.py#L453-L457)（每文件都带 `backup_url`）与 [本函数:L531](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3.py#L531)（无）即可确认。

**练习 2**：`step="6682"`（少一个零）会发生什么？

**答案**：不会报错。`str("6682").isdigit()` 为真，`zfill(5)` 把它补成 `"06682"`，正好命中 mapper 里的键（[L520-L522](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3.py#L520-L522)）。补零设计就是为了容忍这种书写差异。

**练习 3**：下载后的 checkpoint 能否直接 `AutoModelForCausalLM.from_pretrained("qwen3/qwen3-0.6B-distill-step06682-epoch1.pth")` 加载？

**答案**：不能。它是一个**单文件 state_dict**，不是包含 `config.json`、权重文件、分词器文件的 HF 模型**文件夹**；`from_pretrained` 的协议是「目录/仓库 → 自动重建模型」。要进 HF 生态必须走模块二（运行时包装）或模块三（导出成文件夹）。

### 4.2 模块二：wrapper 方案——给 from-scratch 模型套一层 HF 接口的壳

#### 4.2.1 概念说明

wrapper 方案的直觉是：**权重一个字节都不动、也不产生新文件夹**。`Qwen3Model` 原样作为子模块嵌进一个继承 `PreTrainedModel` 的壳类，壳类负责说「HF 语言」——实现 `forward` 签名、暴露 embedding 读写方法、回答 `generate` 每步的「喂什么」问题。这样 `model.generate(...)` 和 `transformers.Trainer` 就能直接驱动它（[wrapper README:L5-L10](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch08/06_use_via_huggingface/wrapper_approach/README.md#L5-L10)）。

代价是一个坦率的取舍（源码注释与 README 都写明了）：**不把 from-scratch 的 KV cache 适配到 HF 的 cache 类**，生成时每步都对完整前缀重算前向。按 u3-l1 的结论，无缓存生成的总前向 token 量约为 \( L\cdot N + N(N-1)/2 \)（\(L\) 为 prompt 长度、\(N\) 为生成 token 数），随 \(N\) 平方增长——所以 wrapper 定位是「本仓库内的轻量实验」，不是高性能推理。

#### 4.2.2 核心流程

构建侧（`build_model_and_tokenizer`）：

```
build_model_and_tokenizer(tokenizer_kind, model_path, ...)
 ├─ build_tokenizer：未指定路径 → download_qwen3_small(tokenizer_only=True)
 │    reasoning → Qwen3Tokenizer(apply_chat_template=True,
 │                            add_generation_prompt=True, add_thinking=True)
 └─ build_model
      ├─ 未指定 model_path → download_qwen3_small 下载默认 .pth
      ├─ torch.load(state_dict)
      ├─ Qwen3LocalWrapperConfig(qwen_cfg=QWEN_CONFIG_06_B, pad/eos ids)
      ├─ Qwen3LocalWrapperForCausalLM(config)   # 内部新建 Qwen3Model
      └─ model.model.load_state_dict(state_dict)   # 灌进内层 scratch 模型
```

生成侧（`model.generate` 的每步回调链）：

```
model.generate(input_ids, max_new_tokens, do_sample=False, eos/pad)
  → 每步 prepare_inputs_for_generation(input_ids)
       返回 {"input_ids": 完整序列, "use_cache": False}
  → forward(input_ids)
       del attention_mask/use_cache/kwargs（一概忽略）
       logits = self.model(input_ids)     # 无 cache、全前向
  → argmax 取下一个 token → 拼回 input_ids → 直到 EOS 或达上限
```

#### 4.2.3 源码精读

**配置类：scratch 配置的 HF 翻译器。** [hf_wrapper.py:L49-L80](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch08/06_use_via_huggingface/wrapper_approach/hf_wrapper.py#L49-L80) 的 `Qwen3LocalWrapperConfig` 继承 `PretrainedConfig`，做三件事：把 `qwen_cfg` 存成 **JSON 可序列化**的形式（`torch.bfloat16` → `"bfloat16"` 字符串，辅助函数在 [L30-L46](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch08/06_use_via_huggingface/wrapper_approach/hf_wrapper.py#L30-L46)——因为 HF 配置最终要写成 JSON）；把 scratch 键名抄送成 HF 惯用字段（`emb_dim`→`hidden_size`、`n_heads`→`num_attention_heads` 等）；用 `tie_word_embeddings=False` 声明「输出头独立、不与嵌入绑定」（呼应 u2-l1：scratch 实例有独立 `out_head`）。

**壳类：协议方法 + 极简 forward。** [hf_wrapper.py:L83-L104](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch08/06_use_via_huggingface/wrapper_approach/hf_wrapper.py#L83-L104)：`__init__` 里 `self.model = Qwen3Model(...)` 就是那个被包装的 scratch 模型，同时把 pad/eos 写进 `generation_config`、宣告 `use_cache=False`；`get/set_input_embeddings` 与 `get/set_output_embeddings` 把 HF 的「词嵌入协议」转发到内层的 `tok_emb` / `out_head`（`Trainer` 与部分工具会调用它们）。

[forward（L106-L138）](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch08/06_use_via_huggingface/wrapper_approach/hf_wrapper.py#L106-L138) 只做两件事：

```python
del attention_mask, use_cache, kwargs        # 有意忽略：不支持 padding 与缓存
logits = self.model(input_ids)               # 一步到 logits
# labels 存在时：logits/labels 错位一位，交叉熵，ignore_index=-100
```

`labels` 分支就是 HF 的标准「错位 + `-100` 掩码」损失——与 u9-l3/u9-l4 手写的答案区掩码同一思想，这让 wrapper 也能直接接 `Trainer`（配套的 `hf_trainer.py` 里 `AnswerOnlyDataCollator` 负责把 prompt 位置标成 `-100`，见 [hf_trainer.py:L18-L42](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch08/06_use_via_huggingface/wrapper_approach/hf_trainer.py#L18-L42)）。

**每步喂什么：generate 的适配点。** [prepare_inputs_for_generation（L140-L150）](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch08/06_use_via_huggingface/wrapper_approach/hf_wrapper.py#L140-L150) 是本模块的灵魂：

```python
# This wrapper keeps things simple and recomputes from the full prefix
# during generation instead of adapting the scratch KV cache to HF's
# cache classes.
return {"input_ids": input_ids, "use_cache": False}
```

对比模块三会看到的 export 版（有缓存时只喂 `input_ids[:, -1:]`），这里**原样返回全序列**——正确性有保证（每次都是完整因果前向），代价是平方级计算量。

**构建函数：加载权重的兼容性小技巧。** [build_model（L175-L210）](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch08/06_use_via_huggingface/wrapper_approach/hf_wrapper.py#L175-L210) 里 `torch.load` 先试 `weights_only=True`（新版 torch 的安全默认），老版本不认识该参数抛 `TypeError` 再退回普通调用（[L197-L200](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch08/06_use_via_huggingface/wrapper_approach/hf_wrapper.py#L197-L200)）；随后 `model.model.load_state_dict(state_dict)`——注意是灌进**内层** scratch 模型，壳本身无可训练参数。文件名常量表（[L19-L27](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch08/06_use_via_huggingface/wrapper_approach/hf_wrapper.py#L19-L27)）覆盖 base/reasoning 两种默认权重，但 `model_path` 可以指向**任何**第 6–8 章产出的兼容 `.pth`（GRPO、蒸馏均可，README L97 明确说明）。

**推理脚本：generate 的完整调用现场。** [wrapper_approach/hf_inference.py:L69-L97](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch08/06_use_via_huggingface/wrapper_approach/hf_inference.py#L69-L97)：构建 wrapper → `model.to(device); model.eval()` → `render_prompt`（第 3 章 \boxed 模板）→ `tokenizer.encode` → 调 `model.generate(input_ids=..., max_new_tokens=..., do_sample=False, eos_token_id=..., pad_token_id=...)` → `output_ids[0, input_ids.shape[1]:]` 切掉 prompt 只解码生成段。注意分词器用的是仓库自己的 `Qwen3Tokenizer`，不是 HF 的 `AutoTokenizer`——这正是 wrapper 路线的边界之一（[wrapper README:L48-L49](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch08/06_use_via_huggingface/wrapper_approach/README.md#L48-L49)）。

#### 4.2.4 代码实践

**实践目标**：用 wrapper 把模块一下载的蒸馏 checkpoint 跑起来，亲眼看 `model.generate` 驱动 scratch 模型。

**操作步骤**：

1. 安装额外依赖（仓库本身不自带 transformers）：

```bash
pip install transformers accelerate
```

2. 进入 wrapper 目录运行（需先完成 4.1.4 的下载；非 uv 用户把 `uv run` 换成 `python`）：

```bash
cd ch08/06_use_via_huggingface/wrapper_approach

# 蒸馏 checkpoint + reasoning 分词器
uv run hf_inference.py \
  --tokenizer_kind reasoning \
  --model_path ../../../qwen3/qwen3-0.6B-distill-step06682-epoch1.pth \
  --prompt "If x + 7 = 19, what is x?" \
  --max_new_tokens 256
```

3. 若想对比未训练的起点，把 `--model_path` 去掉、`--tokenizer_kind` 换成 `base`（会自动下载 0.6B base 权重）。

**需要观察的现象**：

- 脚本头部打印设备与 `tokenizer_kind`，随后输出贪心生成的文本。
- 蒸馏 checkpoint 应产出**分步推理 + `\boxed{}` 答案**风格的回答（它正是被这样训练的）；base 模型则倾向直接报答案且常出错。
- 记录耗时：注意每步生成都在重算全前缀，CPU 上 256 个 token 可能明显偏慢。

**预期结果**：默认题 `x + 7 = 19` 的答案为 `x = 12`（可对照 `\boxed{12}`）；两代输出质量差异的具体文本「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：wrapper 的 `forward` 里 `del attention_mask` 意味着什么能力缺失？

**答案**：意味着**不支持 batch 内 padding**——`attention_mask` 正是 HF 用来标记「哪些位置是填充」的输入，忽略它就等于只支持单条、无填充的序列。模块三的 `_build_causal_mask` 会把它与因果掩码做 OR（[hf_qwen3.py:L348-L349](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch08/06_use_via_huggingface/export_approach/hf_qwen3.py#L348-L349)），形成对比。

**练习 2**：为什么壳类要实现 `get_input_embeddings` / `get_output_embeddings`？

**答案**：这是 HF 的协议方法。`Trainer` 保存 checkpoint、`generate` 的部分流程以及权重绑定检查等都会经由它们定位词嵌入与输出头；不实现就无法被当成「标准因果 LM」对待。wrapper 把它们转发到内层 `self.model.tok_emb` / `self.model.out_head`（[L94-L104](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch08/06_use_via_huggingface/wrapper_approach/hf_wrapper.py#L94-L104)）。

**练习 3**：wrapper 方案能否把模型 `save_pretrained` 成一个文件夹给别人用？

**答案**：实践中不适合。它没有 `register_for_auto_class`、没有配套的 HF 分词器文件与 safetensors 导出流程，即使勉强 `save_pretrained`，对方也没有可靠的 `AutoModelForCausalLM` 入口（README 的 Limitations 一节，[L48-L52](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch08/06_use_via_huggingface/wrapper_approach/README.md#L48-L52)）。要分发就用 export 方案。

### 4.3 模块三：export 方案——转存为标准 HF 模型文件夹

#### 4.3.1 概念说明

export 方案回答另一个问题：**如果想要一个「正常的」HF 模型文件夹呢？**——有 `config.json`、`model.safetensors`、分词器文件、`generation_config.json`，任何机器上都能 `AutoModelForCausalLM.from_pretrained(...)` 加载，还能上传 Hub。

它的做法不是把权重翻译成官方 `Qwen3ForCausalLM` 的参数名，而是**在 HF 侧重新实现一份同构架构**（`hf_qwen3.py` 的 `Qwen3FromScratchForCausalLM`，结构与 `Qwen3Model` 完全一致），让权重**原名、原形**地 `load_state_dict` 进去，再用 `save_pretrained(safe_serialization=True)` 落盘（[export README:L70-L77](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch08/06_use_via_huggingface/export_approach/README.md#L70-L77)）。「自定义架构 + `trust_remote_code=True`」是 HF 官方支持的扩展机制。

两个精妙的细节：

1. **cos/sin 缓冲区从「非持久」变「持久」**。scratch 模型里 RoPE 表注册为 `persistent=False`、不进 state_dict（u2-l1）；但导出的文件夹必须自包含，所以 export 版把它注册成**持久 buffer** 并在导出时手动补进 state_dict（README L86 专门解释了这一点）。
2. **仍然依赖 `reasoning_from_scratch` 包**。`hf_qwen3.py` 只重写了「架子」，`RMSNorm`、`apply_rope`、`compute_rope_params` 依旧 import 自包内（[hf_qwen3.py:L12](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch08/06_use_via_huggingface/export_approach/hf_qwen3.py#L12)），因此导出文件夹拷到别处后，**目标环境也必须安装本包**（README L90）。

两条路线的完整对比：

| 维度 | wrapper 方案 | export 方案 |
| --- | --- | --- |
| 权重去向 | 留在 `.pth`，运行时灌入 | 转存为文件夹内 `model.safetensors` |
| 模型代码 | 复用包内 `Qwen3Model` 作子模块 | HF 侧重写架构，仅复用 RMSNorm/RoPE 等零件 |
| Auto 类加载 | 不支持（直接构造类） | 支持，需 `trust_remote_code=True` |
| KV cache | 不适配，每步重算全前缀 | 适配 HF `DynamicCache`，增量解码 |
| padding/batch | 不支持（忽略 attention_mask） | 支持（causal mask 与 padding mask 合并） |
| 分词器 | 仓库 `Qwen3Tokenizer` | HF `PreTrainedTokenizerFast` + Jinja 聊天模板 |
| 代码量/定位 | 最少；本地实验、学习 | 较多；打包分发、性能（README 选型建议 [L38-L41](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch08/06_use_via_huggingface/README.md#L38-L41)） |

#### 4.3.2 核心流程

导出侧（`export_qwen3_to_hf`，七步）：

```
export_qwen3_to_hf(output_dir, model_path | state_dict, tokenizer_kind)
 1. 来源二选一（同时给 state_dict 和 model_path → ValueError）
    缺文件 → resolve_source_paths 自动下载
 2. torch.load 得到 state_dict
 3. build_hf_tokenizer：PreTrainedTokenizerFast 包装 repo 的 tokenizer.json
    （reasoning 时挂 Jinja 聊天模板）
 4. build_hf_config：QWEN_CONFIG_06_B 逐键映射成 HF 命名
 5. register_for_auto_class("AutoConfig" / "AutoModelForCausalLM")
    → save_pretrained 会在 config.json 写 auto_map 并复制 .py 模块进文件夹
 6. new Qwen3FromScratchForCausalLM(config)
    state_dict.setdefault("cos"/"sin", ...) → load_state_dict(strict=True)
 7. save_pretrained(safe_serialization=True) + tokenizer.save_pretrained
    + GenerationConfig.save_pretrained + export_info.json
```

加载/生成侧（Auto 类链路）：

```
AutoTokenizer / AutoConfig / AutoModelForCausalLM
    .from_pretrained(model_dir, trust_remote_code=True)
  → 读 config.json 的 auto_map → 执行文件夹内的自定义模块
  → 反序列化 model.safetensors → 得到 Qwen3FromScratchForCausalLM
model.generate(**inputs)
  → prepare_inputs_for_generation：有 past 时只喂 input_ids[:, -1:]
  → forward：DynamicCache.update(...) 增量写 KV → 只算新 token 的 logits
```

#### 4.3.3 源码精读

**config：一套参数、两套名字。** [hf_qwen3.py:L15-L101](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch08/06_use_via_huggingface/export_approach/hf_qwen3.py#L15-L101) 的 `Qwen3FromScratchConfig` 以 HF 命名为**主名**（`hidden_size`、`num_attention_heads`、`rope_theta`、`use_qk_norm`…），同时接受 scratch 风格的**别名参数**（`emb_dim`、`n_heads`、`rope_base`、`qk_norm`…，[L49-L66](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch08/06_use_via_huggingface/export_approach/hf_qwen3.py#L49-L66) 做换算），并在实例上**两套属性都存**（[L83-L90](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch08/06_use_via_huggingface/export_approach/hf_qwen3.py#L83-L90)）——这样既吃 HF 生态的配置，又能直接复用按 scratch 键写逻辑的模型代码。注意它的**默认值就是 0.6B 配置**（vocab 151936、28 层、16 头、8 KV 组…），且默认 `use_cache=True`。

**架构重写：与 scratch 逐块对齐。** [FeedForward（L114-L140）](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch08/06_use_via_huggingface/export_approach/hf_qwen3.py#L114-L140) 与 [GroupedQueryAttention（L143-L229）](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch08/06_use_via_huggingface/export_approach/hf_qwen3.py#L143-L229) 的数学与 u2-l2/u2-l3 精读过的版本完全一致（SwiGLU 三投影、QK-Norm、`repeat_interleave` 展开、`masked_fill` + softmax 缩放），区别只在：从 `config` 对象取超参、可指定 `dtype`、以及 `forward` 多了 `layer_idx` 参数以适配 HF 缓存。看 [L207-L216](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch08/06_use_via_huggingface/export_approach/hf_qwen3.py#L207-L216)：`cache` 若有 `update` 方法（HF `DynamicCache`）就调 `cache.update(keys_new, values_new, layer_idx)`；若是旧式元组缓存就 `torch.cat`；都没有就裸算——一份代码兼容三类缓存形态。

**模型主体：HF 签名的完整实现。** [Qwen3FromScratchForCausalLM（L262-L481）](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch08/06_use_via_huggingface/export_approach/hf_qwen3.py#L262-L481) 与 wrapper 壳的关键差异逐一对应：

- `__init__` 末尾 [register_buffer("cos", cos)](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch08/06_use_via_huggingface/export_approach/hf_qwen3.py#L289-L295)（无 `persistent=False`）→ cos/sin **会**进 state_dict 与 safetensors；scratch 版见 [qwen3.py:L53-L54](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3.py#L53-L54) 的对照。
- [_build_causal_mask（L323-L349）](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch08/06_use_via_huggingface/export_approach/hf_qwen3.py#L323-L349)：用「query 位置 vs key 位置」的比较构造因果掩码（等价于 u2-l5 的切片技巧，但面向 `past_len + seq_len` 的总长度），再与 `attention_mask` 推出的 **key padding mask** 做 OR——这就是它支持批量 padding 生成的地方。
- [forward（L351-L443）](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch08/06_use_via_huggingface/export_approach/hf_qwen3.py#L351-L443)：先探测缓存类型（`hasattr update + get_seq_length` 判定 `DynamicCache`，[L366-L369](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch08/06_use_via_huggingface/export_approach/hf_qwen3.py#L366-L369)），取 `past_len` 供 RoPE 的 `start_pos` 与掩码使用；逐层调用 block；`labels` 走错位交叉熵；最后把 `past_key_values` 原样装回 `CausalLMOutputWithPast` 返回。
- [prepare_inputs_for_generation（L445-L466）](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch08/06_use_via_huggingface/export_approach/hf_qwen3.py#L445-L466)：**只要有 past，就只喂最后一个 token** `input_ids[:, -1:]`——与 wrapper 的「全序列」形成镜像对照，这就是增量解码的入口。
- [_reorder_cache（L468-L481）](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch08/06_use_via_huggingface/export_approach/hf_qwen3.py#L468-L481)：为 beam search 按候选重排缓存，HF 生成协议的可选插槽。

**导出器：七步流水线的落地。** [hf_export.py:L122-L212](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch08/06_use_via_huggingface/export_approach/hf_export.py#L122-L212)：

- 入口校验 `state_dict` 与 `model_path` 互斥（[L132-L133](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch08/06_use_via_huggingface/export_approach/hf_export.py#L132-L133)）；缺源时 [resolve_source_paths（L53-L81）](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch08/06_use_via_huggingface/export_approach/hf_export.py#L53-L81) 自动下载默认权重/分词器。
- [build_hf_tokenizer（L84-L97）](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch08/06_use_via_huggingface/export_approach/hf_export.py#L84-L97) 用 `PreTrainedTokenizerFast` 直接加载仓库的 `tokenizer.json`，设 pad（`<|endoftext|>`）与按家族选择的 eos（[EOS_TOKENS，L29-L33](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch08/06_use_via_huggingface/export_approach/hf_export.py#L29-L33)）；reasoning 家族额外挂上 Jinja 聊天模板（[REASONING_CHAT_TEMPLATE，L36-L39](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch08/06_use_via_huggingface/export_approach/hf_export.py#L36-L39)）——它渲染出的 `<|im_start|>user\n...<|im_end|>\n<|im_start|>assistant\n` 与 u2-l6 手写的 `_wrap_chat` 等价，只是住在分词器里、由 HF 的 `apply_chat_template` 驱动。
- [build_hf_config（L100-L119）](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch08/06_use_via_huggingface/export_approach/hf_export.py#L100-L119) 把 `QWEN_CONFIG_06_B` 的 12 个键逐一映射成 HF 命名（`vocab_size`、`hidden_size`、`intermediate_size`、`rope_theta`、`use_qk_norm`…，dtype 经 [torch_dtype_to_name（L42-L50）](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch08/06_use_via_huggingface/export_approach/hf_export.py#L42-L50) 字符串化以便进 JSON）。
- [L176-L183](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch08/06_use_via_huggingface/export_approach/hf_export.py#L176-L183) 是导出的「心脏」：先 `register_for_auto_class`（随后 `save_pretrained` 会在 `config.json` 登记 `auto_map` 并把 `hf_qwen3.py` 复制进输出文件夹，README L67 确认了这份「copied custom Python module」的存在）；再 `state_dict.setdefault("cos", model.cos)` / `setdefault("sin", model.sin)` 把 scratch checkpoint 里缺失的 RoPE 表补上，然后 **`load_state_dict(strict=True)`**——因为两边架构同构、参数名一致，严格模式正好充当「一个都没搬错」的验收；`missing_keys`/`unexpected_keys` 非空直接 `RuntimeError`。
- [L190-L210](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch08/06_use_via_huggingface/export_approach/hf_export.py#L190-L210)：`model.save_pretrained(output_dir, safe_serialization=True)` 落盘 `model.safetensors`，分词器与 `GenerationConfig`（eos/pad）各自 `save_pretrained`，最后写一份 `export_info.json` 备忘（来源路径、tokenizer_kind 等）。

**加载侧：Auto 三件套现场。** [export_approach/hf_inference.py:L60-L96](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch08/06_use_via_huggingface/export_approach/hf_inference.py#L60-L96)：`AutoTokenizer` / `AutoConfig` / `AutoModelForCausalLM` 三个 `from_pretrained(model_dir, trust_remote_code=True)` 完成加载；`config.tokenizer_kind`（自定义配置字段）顺着 AutoConfig 流出来，决定 [wrap_prompt（L14-L25）](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch08/06_use_via_huggingface/export_approach/hf_inference.py#L14-L25) 是否套聊天模板；注意 `tokenizer(prompt, add_special_tokens=False)`——因为特殊 token 已由模板手工拼好。`model.generate(**inputs, do_sample=False, ...)` 走贪心，输出同样按 prompt 长度切片解码。

#### 4.3.4 代码实践

**实践目标**：把 4.1.4 下载的蒸馏 checkpoint 导出成标准 HF 文件夹，并用 `AutoModelForCausalLM` 推理；与 wrapper 路线比速度。

**操作步骤**：

1. 导出（在 export_approach 目录内运行，`hf_export.py` 直接 `import hf_qwen3`）：

```bash
cd ch08/06_use_via_huggingface/export_approach

uv run hf_export.py \
  --output_dir hf-qwen3-distill \
  --model_path ../../../qwen3/qwen3-0.6B-distill-step06682-epoch1.pth \
  --tokenizer_kind reasoning
```

2. 检查产物清单：

```bash
ls hf-qwen3-distill
# 预期看到：config.json  generation_config.json  model.safetensors
#           tokenizer.json 等分词器文件  复制进来的自定义 .py 模块  export_info.json
```

3. 用 Auto 类推理：

```bash
uv run hf_inference.py \
  --model_dir hf-qwen3-distill \
  --prompt "If x + 7 = 19, what is x?" \
  --max_new_tokens 256
```

4. （可选加分项）对同一 prompt、同一 `max_new_tokens`，给步骤 3 与 4.2.4 的 wrapper 各计一次墙钟时间，算加速比。

**需要观察的现象**：

- `config.json` 里有 `model_type: "qwen3_from_scratch"`、`auto_map` 字段与 `tokenizer_kind: "reasoning"`；`generation_config.json` 里 eos/pad 已写好。
- 推理输出与 wrapper 路线应是**同一权重、同一贪心解码**，文本应基本一致（浮点路径差异可能引起个别 token 分歧，属正常）。
- export 路线应明显快于 wrapper（增量 KV 解码 vs 全前向重算）。

**预期结果**：答案同样落在 `\boxed{12}`；两条路线的具体文本与加速比「待本地验证」（依赖设备）。

#### 4.3.5 小练习与答案

**练习 1**：为什么导出时必须 `state_dict.setdefault("cos", ...)`，而 wrapper 加载时不需要？

**答案**：scratch checkpoint 只存可训练权重，RoPE 的 cos/sin 在 `Qwen3Model` 里是 `persistent=False` 的 buffer、从不入 state_dict（u2-l1）。wrapper 里被包装的就是 `Qwen3Model`，它**构造时自己重算** cos/sin；而 export 模型把 cos/sin 注册为**持久 buffer**、期望它们出现在 state_dict 里，且 `load_state_dict(strict=True)` 要求键完全匹配，所以必须手动补键（[hf_export.py:L180-L183](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch08/06_use_via_huggingface/export_approach/hf_export.py#L180-L183)）。这也是 README L86 说的「保证重载数值一致」。

**练习 2**：`trust_remote_code=True` 到底执行了什么？有什么风险？

**答案**：它让 `from_pretrained` 按 `config.json` 的 `auto_map` 去加载并**执行模型文件夹里携带的自定义 Python 模块**（由 `register_for_auto_class` + `save_pretrained` 复制进去的 `hf_qwen3.py`），用其中的类重建模型。风险在于这等于运行第三方代码——只应对可信来源打开；本仓库的导出文件夹还依赖环境中已安装的 `reasoning_from_scratch` 包（README L90）。

**练习 3**：同一份 `.pth` 走两条路线，生成结果理论上应当相同，为什么？

**答案**：两条路线最终执行的是**同一套架构数学**（同样的 GQA、RMSNorm、RoPE、因果掩码）与同样的贪心 argmax 解码，权重也逐键一致（export 用 `strict=True` 验收）。差别只在工程路径：wrapper 无缓存全前向、export 增量解码。浮点结合顺序不同可能引入极小数值差，偶尔改变临界 token 的选择，但分布上应当一致。

## 5. 综合实践

把本讲三个模块串成一条完整链路：**下载 → 走读 → 加载生成**。

**任务**：

1. 调用 `download_qwen3_distill_checkpoints(distill_type="deepseek_r1", step="06682")` 下载蒸馏 checkpoint（4.1.4 已完成可直接复用），并另下载 reasoning 分词器。
2. 通读两条路线的加载代码：wrapper 的 [build_model（hf_wrapper.py:L175-L210）](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch08/06_use_via_huggingface/wrapper_approach/hf_wrapper.py#L175-L210) 与 export 的 [export_qwen3_to_hf（hf_export.py:L122-L212）](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch08/06_use_via_huggingface/export_approach/hf_export.py#L122-L212)，各写 3 句话总结实现差异。
3. 任选一条路线在本地加载该 checkpoint，对 `Solve: If x + 7 = 19, what is x?` 生成一段回答，并记录耗时。

**参考差异总结**（供对照，鼓励用自己的话写）：

- wrapper（3 句）：① 权重始终留在 `.pth` 文件里，运行时 `torch.load` 后灌进作为子模块的 from-scratch `Qwen3Model`，不产生任何新文件夹；② 通过继承 `PreTrainedModel` + `GenerationMixin` 说 HF 语言，但 `prepare_inputs_for_generation` 每步返回**完整序列**并宣告 `use_cache=False`，即不做 KV 缓存、全前缀重算；③ 不注册 Auto 类、不导出分词器文件，只能在安装了 `reasoning_from_scratch` 的本仓库环境里直接构造使用。
- export（3 句）：① 在 HF 侧重写一份同构架构 `Qwen3FromScratchForCausalLM`，权重**原名原形**经 `strict=True` 灌入后由 `save_pretrained` 转存为含 `config.json` 与 `model.safetensors` 的标准文件夹；② `register_for_auto_class` 让 `AutoConfig`/`AutoModelForCausalLM` 凭 `auto_map` + `trust_remote_code=True` 复原模型，分词器也换成 `PreTrainedTokenizerFast` 并携带 Jinja 聊天模板；③ 完整适配 HF 的 `DynamicCache`（`prepare_inputs_for_generation` 有 past 时只喂最后一个 token）与 padding 掩码，因此生成是增量解码、支持批量，性能与生态兼容性更好，代价是代码量更大且导出文件夹仍依赖本包安装。

**验收标准**：能展示（a）checkpoint 本地路径，（b）两段各 3 句的总结，（c）一段模型生成文本（含 `\boxed{}` 格式答案）与耗时记录。生成内容与耗时因设备而异，「待本地验证」。

## 6. 本讲小结

- 蒸馏 checkpoint 是**纯 `state_dict` `.pth` 文件**而非 HF checkpoint：`download_qwen3_distill_checkpoints` 用「mapper 白名单本地校验 + 模板拼 HF Hub URL」下载两位教师 × 三个 epoch 共 6 个档位，且必须配 reasoning 分词器。
- **wrapper 方案**把 `Qwen3Model` 当子模块塞进 `PreTrainedModel` 薄壳：权重零搬运、代码最少，`generate`/`Trainer` 即刻可用；代价是不适配 HF 缓存（每步重算全前缀，平方级开销）、忽略 `attention_mask`、无 Auto 类加载。
- **export 方案**在 HF 侧重写同构架构并把权重**原名原形**转存为标准模型文件夹（`config.json` + `model.safetensors` + 分词器 + `generation_config.json` + 自定义模块），经 `trust_remote_code=True` 被 Auto 类复原。
- export 的两个关键细节：cos/sin 从非持久 buffer 变**持久**（导出时 `setdefault` 补键以过 `strict=True`）；自定义模块仍 import `reasoning_from_scratch`，故目标环境必须装包。
- 性能分水岭在 `prepare_inputs_for_generation`：wrapper 返回全序列（无缓存），export 在有 past 时只喂 `input_ids[:, -1:]`（增量 KV 解码）——接口上的一行差异对应平方级与线性级的前向总量之别。
- 选型：本仓库内学习与轻量实验用 wrapper；要打包分发、上传 Hub、追求生成吞吐用 export。

## 7. 下一步学习建议

- **下一讲 u10-l1（GPU 优化实现：SDPA 与预分配 KV Cache）**：本讲看到「缓存接口决定性能」，下一讲正面研究缓存与注意力的工程优化——`qwen3_optimized.py` 用 `scaled_dot_product_attention` 融合内核与预分配式 KVCache 替代手写实现。
- **u10-l3（加载更大的 Qwen3 模型与权重映射）**：本讲是「from-scratch → HF 方向」的包装与导出；附录 C 的 `load_hf_weights_into_qwen` 是反方向「官方 HF 权重 → from-scratch 模型」的逐层映射，两者互为镜像，对照阅读会加深对参数名体系的理解。
- **延伸实践**：跑一跑两条路线各自的 `hf_trainer.py`，体会「`labels` 错位 + `-100` 掩码」如何把 u9-l3 的答案区损失搬进 `transformers.Trainer`；再把 `hf_export.py` 导出的文件夹上传到自己的 HF Hub 仓库，验证「加载他处」流程（README L170-L185）。
