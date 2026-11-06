# 与 Hugging Face 生态互通与蒸馏 checkpoint

## 1. 本讲目标

到上一讲为止，我们已经能自己训练蒸馏模型并把 checkpoint 存成 `.pth` 文件。本讲解决最后一个工程问题：**如何让 from-scratch 模型走进 Hugging Face（HF）生态**——用 `model.generate(...)`、`AutoModelForCausalLM`、`transformers.Trainer` 这些业界标准工具来加载和推理我们自己训练的模型。

学完本讲，你应该能够：

1. 用 `download_qwen3_distill_checkpoints` 下载作者发布的蒸馏 checkpoint，并说清它的「家族 × 步数」校验逻辑。
2. 读懂 **wrapper 路线**：不改权重文件，把 `Qwen3Model` 包进一个薄壳 `PreTrainedModel`，直接吃 `.pth`。
3. 读懂 **export 路线**：把 `.pth` 转存成带 `config.json`、`model.safetensors`、分词器文件的标准 HF 模型目录，用 `AutoModelForCausalLM` 加载。
4. 对比两条路线的优劣，并能为自己的场景做出选型决策。

## 2. 前置知识

本讲不引入新的模型数学，主要是「接口工程」。需要几个新概念：

- **state_dict**：PyTorch 把模型所有可学习参数（权重张量）按属性路径（如 `trf_blocks.0.att.W_query.weight`）收进的一个字典。`.pth` 文件就是序列化的 state_dict。第 8 章训练脚本按 epoch 存的就是纯权重 state_dict（见 u9-l4），没有任何架构信息——**加载时必须先重建同架构模型再灌权重**。
- **Hugging Face transformers**：业界最流行的模型库。它的核心约定是「模型目录」：一个文件夹里放 `config.json`（架构超参）、权重文件（通常是 `model.safetensors`）、分词器文件，然后 `AutoModelForCausalLM.from_pretrained(目录)` 一行加载。`PreTrainedModel` 是所有 HF 模型的基类，实现它就能接入 `generate()` 与 `Trainer`。
- **safetensors**：一种只存张量、不允许任意代码执行的序列化格式，比 pickle（`.pth` 用的格式）安全，是 HF 的默认权重格式。
- **trust_remote_code=True**：当模型的 `model_type` 不是 transformers 内置类型时，HF 会执行模型目录里附带的 `.py` 模块来构建模型。这个开关就是「允许执行这份远程/本地代码」的授权。
- **回顾**（前面讲义已建立）：`QWEN_CONFIG_06_B` 配置字典完全决定模型结构（u2-l1）；RoPE 的 cos/sin 表在 qwen3.py 中注册为**非持久 buffer**，不进 state_dict（u2-l1/u2-l4）；reasoning 分词器挂聊天模板、eos 为 `<|im_end|>`，base 分词器是裸文本、eos 为 `<|endoftext|>`（u2-l6）；蒸馏训练用的是 reasoning 分词器 + `<think>` 包装（u9-l2）。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [reasoning_from_scratch/qwen3.py](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3.py) | 全书地基：`Qwen3Model`、分词器，以及三个下载入口；本讲关注 `download_qwen3_distill_checkpoints` |
| [ch08/05_download_training_checkpoints/usage_example.py](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch08/05_download_training_checkpoints/usage_example.py) | 蒸馏 checkpoint 的「原厂用法」：下载 → 重建模型 → 灌权重 → 流式生成 |
| [ch08/06_use_via_huggingface/wrapper_approach/hf_wrapper.py](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch08/06_use_via_huggingface/wrapper_approach/hf_wrapper.py) | wrapper 路线核心：把 `Qwen3Model` 包成 HF `PreTrainedModel` |
| [ch08/06_use_via_huggingface/wrapper_approach/hf_inference.py](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch08/06_use_via_huggingface/wrapper_approach/hf_inference.py) | wrapper 路线推理脚本：`model.generate(...)` |
| [ch08/06_use_via_huggingface/export_approach/hf_qwen3.py](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch08/06_use_via_huggingface/export_approach/hf_qwen3.py) | export 路线的自定义 HF 架构：`Qwen3FromScratchConfig` + `Qwen3FromScratchForCausalLM` |
| [ch08/06_use_via_huggingface/export_approach/hf_export.py](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch08/06_use_via_huggingface/export_approach/hf_export.py) | export 路线转换器：`.pth` → HF 模型目录 |
| [ch08/06_use_via_huggingface/README.md](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch08/06_use_via_huggingface/README.md) | 两条路线的官方定位与选型建议 |

两个子目录还各有 `hf_trainer.py`（用 `transformers.Trainer` 继续训练）和各自的 README，本讲以推理为主线，训练脚本思路相同。

## 4. 核心概念与源码讲解

### 4.1 蒸馏 checkpoint 的获取：`download_qwen3_distill_checkpoints`

#### 4.1.1 概念说明

第 8 章训练一个蒸馏模型动辄几十分钟到数小时。作者把自己训好的两组蒸馏 checkpoint（分别以 DeepSeek-R1 和 Qwen3-235B-A22B 为教师）发布到了 Hugging Face Hub，读者可以直接下载复用——这就是 u9-l1 反复强调的「教师即学生上限」：两个家族对应两条不同推理风格的蒸馏线。

与 `download_qwen3_small`（下载固定两个文件）不同，这个函数面对的是一个**二维目录**：`家族（distill_type）× 训练步数（step）`，每个组合对应一个具体文件名，所以它采用了「mapper 字典本地校验 + 模板拼 URL」的策略（与 u1-l3 讲过的 `download_qwen3_grpo_checkpoints` 同款设计）。

#### 4.1.2 核心流程

```text
输入 distill_type, step, out_dir
  ├─ distill_type 不在 {deepseek_r1, qwen3_235b_a22b} → ValueError（就地拒绝）
  ├─ step 转字符串，纯数字则 zfill(5) 补零（6682 → "06682"）
  ├─ 补零后的 step 不在该家族的 mapper 里 → ValueError
  ├─ primary URL = HF 仓库路径 folder_map[distill_type]/mapper[distill_type][step]
  └─ download_file(primary, out_dir) → 返回本地文件路径
```

#### 4.1.3 源码精读

函数全貌（[reasoning_from_scratch/qwen3.py:496-531](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3.py#L496-L531)）：先看 mapper——`deepseek_r1` 家族提供 epoch 1/2/3 的三个步数档位（06682/13364/20046），`qwen3_235b_a22b` 家族是另外三档（05746/11492/17238），文件名内嵌训练步数与 epoch 号。

合法性双重校验（[reasoning_from_scratch/qwen3.py:517-526](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3.py#L517-L526)）：家族非法与步数非法都**在本地立即抛 ValueError**，而不是拼出一个不存在的 URL 去网上撞 404——报错信息里直接列出所有合法取值。`str(step).zfill(5)` 让用户传 `6682` 或 `"06682"` 都能命中。

拼 URL 并下载（[reasoning_from_scratch/qwen3.py:528-531](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3.py#L528-L531)）：主源是 HF 的 `rasbt/qwen3-from-scratch-distill-checkpoints` 仓库，按 `folder_map` 的子目录定位文件。注意这里**没有传 backup_url**——与 `download_qwen3_small`（[reasoning_from_scratch/qwen3.py:440-457](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3.py#L440-L457)）的主备双通道不同，checkpoint 族只有 HF 单源。函数返回本地落盘路径，供上层直接 `torch.load`。

原厂用法示范在 [ch08/05_download_training_checkpoints/usage_example.py:79-90](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch08/05_download_training_checkpoints/usage_example.py#L79-L90)（这段代码演示了 checkpoint 的标准消费方式）：下载拿到路径 → `Qwen3Model(QWEN_CONFIG_06_B)` 从配置字典重建架构 → `torch.load` + `load_state_dict` 灌权重 → `to(device)` + `eval()`。**`.pth` 里没有架构信息，重建模型的配置必须与训练时一致**——这里硬编码 0.6B 配置，正说明这批蒸馏 checkpoint 都是 0.6B 架构。分词器侧（[ch08/05_download_training_checkpoints/usage_example.py:24-31](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch08/05_download_training_checkpoints/usage_example.py#L24-L31)）固定用 reasoning 家族并开聊天模板 + thinking——呼应 u9-l2：蒸馏训练时 prompt 就是这么编码的，推理时必须同分布。生成则复用第 2 章的流式缓存函数（[ch08/05_download_training_checkpoints/usage_example.py:104-111](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch08/05_download_training_checkpoints/usage_example.py#L104-L111)）。

#### 4.1.4 代码实践

1. **实践目标**：确认下载入口的参数校验与缓存行为。
2. **操作步骤**（在仓库根目录，虚拟环境中）：

```python
from reasoning_from_scratch.qwen3 import download_qwen3_distill_checkpoints

# ① 合法调用：下载 deepseek_r1 家族 epoch1 checkpoint（约 1.5 GB）
p = download_qwen3_distill_checkpoints(distill_type="deepseek_r1", step="06682", out_dir="qwen3")
print(p)

# ② 整数 step 也能命中（zfill 补零）
p2 = download_qwen3_distill_checkpoints(distill_type="deepseek_r1", step=6682, out_dir="qwen3")

# ③ 非法步数：本地 ValueError
download_qwen3_distill_checkpoints(distill_type="deepseek_r1", step="00001")
```

3. **需要观察的现象**：①首次调用出现分块下载进度条（u1-l3 讲过的 `download_file` 进度打印）；②第二次调用命中缓存、几乎瞬时返回相同路径；③抛出 `ValueError`，报错文案列出全部合法步数。
4. **预期结果**：三步行为分别如上；具体缓存提示文案「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：为什么要把「非法参数」放在本地校验，而不是让 URL 去 404？  
**答案**：本地 mapper 校验能立刻给出「合法取值清单」，报错快且信息完整；网上 404 只有 HTTP 状态码，用户无从知道支持哪些家族/步数。这是 u1-l3 总结过的门面模式：非法输入就地拒绝。

**练习 2**：`download_qwen3_distill_checkpoints` 没有备用 URL，这与 `download_qwen3_small` 不同，会带来什么影响？  
**答案**：单源意味着 HF 不可达（网络/代理问题）时该函数无法换源重试，只能等 HF 恢复或手动下载后放入 `out_dir`（`download_file` 的缓存按文件大小判断，手动放好同名文件即可跳过下载）。

### 4.2 wrapper 路线：给 `Qwen3Model` 套一层 HF 壳

#### 4.2.1 概念说明

wrapper 路线的哲学是**「模型不动，接口适配」**：`.pth` 权重文件保持原样，`Qwen3Model` 一行不改，另写一个继承 `PreTrainedModel` 的壳类把前者包起来，骗过 HF 的 API 检查。这样 `model.generate(...)` 和 `transformers.Trainer` 就能直接用了。

要接入 HF，需要满足两个接口契约：

- 一个 `PretrainedConfig` 子类：HF 用 config 对象携带模型元数据（词表大小、eos id 等），且要求能存成 JSON。
- `forward(input_ids, attention_mask, labels, ...)` 签名与 `get/set_input_embeddings` 等方法：`generate()` 和 `Trainer` 靠这些方法驱动模型。

#### 4.2.2 核心流程

```text
build_model_and_tokenizer(tokenizer_kind, model_path, ...)
  ├─ build_tokenizer：无路径则 download_qwen3_small(tokenizer_only=True)
  │    reasoning → Qwen3Tokenizer(聊天模板+生成提示+thinking)；base → 裸 Qwen3Tokenizer
  ├─ build_model：无路径则 download_qwen3_small 下载 .pth
  │    ├─ torch.load(state_dict)
  │    ├─ Qwen3LocalWrapperConfig(把 qwen_cfg 转成 JSON 友好形态, pad/eos id)
  │    ├─ Qwen3LocalWrapperForCausalLM(config)   # 内部 self.model = Qwen3Model(...)
  │    └─ model.model.load_state_dict(state_dict)  # 权重灌进被包裹的裸模型
  └─ 返回 (model, tokenizer)

model.generate(input_ids, ...) 调用链：
  HF GenerationMixin → prepare_inputs_for_generation → forward → self.model(input_ids)
```

#### 4.2.3 源码精读

**配置壳**（[hf_wrapper.py:49-80](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch08/06_use_via_huggingface/wrapper_approach/hf_wrapper.py#L49-L80)）：`Qwen3LocalWrapperConfig` 最巧的设计是把整份 from-scratch 配置字典 `qwen_cfg` 原样塞进 config（L63），同时按 HF 惯例导出 `vocab_size`、`hidden_size`、`num_attention_heads` 等标准字段（L65-70）。因为 HF 的 config 最终要存 JSON，而 `torch.bfloat16` 这类 dtype 对象不可序列化，所以先经 `_qwen_cfg_to_jsonable`（[hf_wrapper.py:36-39](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch08/06_use_via_huggingface/wrapper_approach/hf_wrapper.py#L36-L39)）把 dtype 转成字符串 `"bfloat16"`；反向 `_qwen_cfg_from_jsonable` 用 `getattr(torch, ...)` 转回来（[hf_wrapper.py:42-46](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch08/06_use_via_huggingface/wrapper_approach/hf_wrapper.py#L42-L46)）。L71 顺手弹掉 `tie_word_embeddings`——from-scratch 模型输出头独立，不与嵌入绑定（u2-l1 的参数口径结论）。

**模型壳**（[hf_wrapper.py:83-92](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch08/06_use_via_huggingface/wrapper_approach/hf_wrapper.py#L83-L92)）：`Qwen3LocalWrapperForCausalLM` 继承 `PreTrainedModel` + `GenerationMixin`（后者提供 `generate`），`__init__` 里 `self.model = Qwen3Model(...)` 完成真正的组装；L90-92 把 pad/eos id 写进 `generation_config`，`use_cache=False` 是本路线的关键伏笔。

**forward 的「接收但忽略」策略**（[hf_wrapper.py:106-138](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch08/06_use_via_huggingface/wrapper_approach/hf_wrapper.py#L106-L138)）：L115 `del attention_mask, use_cache, kwargs` 一行删掉了 HF 会传来的所有额外参数——from-scratch `Qwen3Model` 的 forward 只吃 token 序列（必要时才收 cache，见 [reasoning_from_scratch/qwen3.py:63-102](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3.py#L63-L102) 中无 cache 分支：内部自建严格上三角因果掩码），既不支持 padding 掩码也没有 HF 风格的 KV cache 接口。L122-129 补了 HF 风格的 labels 损失（错位一位 + `ignore_index=-100`，与 u9-l3/u9-l4 的答案掩码语义同构），让 `Trainer` 拿得到 loss；L135-138 返回标准的 `CausalLMOutputWithPast`。

**生成适配**（[hf_wrapper.py:140-150](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch08/06_use_via_huggingface/wrapper_approach/hf_wrapper.py#L140-L150)）：`prepare_inputs_for_generation` 每一步都把**完整前缀** `input_ids` 原样喂回模型。源码注释说得很直白：与其把 from-scratch 的 KVCache 改造成 HF 的 cache 类，不如每步重算全前缀。这正是 u3-l1 分析过的无缓存自回归——总前向计算量随生成长度呈平方增长，代价是**生成速度明显慢于**本书自带的 `generate_text_basic_stream_cache`。

**装配函数**（[hf_wrapper.py:175-210](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch08/06_use_via_huggingface/wrapper_approach/hf_wrapper.py#L175-L210)）：`build_model` 的 L197-200 先尝试 `torch.load(..., weights_only=True)`、老版本 torch 抛 `TypeError` 再退回旧签名——版本兼容小技巧。L202-209 是路线灵魂：先用 from-scratch 配置建 HF 壳、再把 `.pth` state_dict **原封不动**灌进壳里的 `self.model`（`Qwen3Model`）——权重没有任何改名或变形。`build_model_and_tokenizer`（[hf_wrapper.py:213-230](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch08/06_use_via_huggingface/wrapper_approach/hf_wrapper.py#L213-L230)）把分词器与模型装配收口成一个函数；分词器包装规则在 [hf_wrapper.py:153-172](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch08/06_use_via_huggingface/wrapper_approach/hf_wrapper.py#L153-L172)（reasoning 开聊天模板+thinking，base 裸文本）。

**推理脚本**（[wrapper_approach/hf_inference.py:69-105](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch08/06_use_via_huggingface/wrapper_approach/hf_inference.py#L69-L105)）：`--model_path` 可指向 base/reasoning `.pth` 或第 6-8 章任意 checkpoint（默认自动下载对应基座）；L88-95 用 HF 的 `model.generate(do_sample=False)` 做贪心解码——本书代码第一次把生成循环完全交给 HF；L97 从输出里切掉 prompt 长度只留新 token，这与我们手写循环时「返回生成部分」的约定一致。

#### 4.2.4 代码实践

1. **实践目标**：用 wrapper 路线加载 4.1 下载的蒸馏 checkpoint 并用 `model.generate` 生成。
2. **操作步骤**（需先 `pip install transformers`）：

```bash
cd ch08/06_use_via_huggingface/wrapper_approach
python hf_inference.py \
  --tokenizer_kind reasoning \
  --model_path ../../../qwen3/qwen3-0.6B-distill-step06682-epoch1.pth \
  --prompt "If x + 7 = 19, what is x?" \
  --max_new_tokens 200 --device cpu
```

3. **需要观察的现象**：终端打印设备与 `tokenizer_kind` 信息后逐段输出回答；蒸馏模型应先输出 `<think>...</think>` 思考流，再给出带 `\boxed{12}` 的正式作答（模板由 reasoning 分词器自动包装，u2-l6 的四种形态之一）。
4. **预期结果**：答案为 x = 12；同参数重复运行输出完全一致（贪心解码确定性）。CPU 上生成 200 token 明显偏慢（每步重算全前缀）——这就是 `use_cache=False` 的代价，属预期现象而非故障。具体生成速度「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `Qwen3LocalWrapperForCausalLM.forward` 里要 `del attention_mask`，这带来了什么限制？  
**答案**：from-scratch `Qwen3Model` 的掩码在 forward 内部自建（无 cache 时是严格上三角因果掩码），不接受外部 padding 掩码；`del` 是为了消化 HF 接口必然传来的参数。限制是该路线无法做带 padding 的批量推理。

**练习 2**：wrapper 的 `prepare_inputs_for_generation` 与 export 路线（见 4.3.3）相比少做了什么？  
**答案**：wrapper 每步回传完整 `input_ids` 重算全前缀（无 KV cache）；export 路线在有 `past_key_values` 时只切出最后一个 token `input_ids[:, -1:]` 送入增量前向，复杂度从平方降为线性。

### 4.3 export 路线：把 `.pth` 转成标准 HF 模型目录

#### 4.3.1 概念说明

export 路线的哲学是**「架构重述，权重原样」」**：在 `hf_qwen3.py` 里用 HF 的命名规范**重新实现一遍** Qwen3 架构（`PretrainedConfig` + `PreTrainedModel`），然后用 `hf_export.py` 把 `.pth` 权重灌进去、以 HF 目录格式（`config.json` + `model.safetensors` + 分词器文件 + `generation_config.json`）落盘。之后任何 HF 环境都能 `AutoModelForCausalLM.from_pretrained(目录, trust_remote_code=True)` 加载。

注意 README 的澄清：导出**不是**翻译成 HF 官方的 `Qwen3ForCausalLM` 实现，也**不改动**任何学习到的权重——只是给同一套参数换了一身 HF 制服。

#### 4.3.2 核心流程

```text
export_qwen3_to_hf(output_dir, state_dict | model_path, tokenizer_kind, ...)
  ├─ 二选一取权重：直接给 state_dict，或给 model_path 走 torch.load
  ├─ build_hf_tokenizer：PreTrainedTokenizerFast 包 tokenizer-*.json
  │    （reasoning 时挂 REASONING_CHAT_TEMPLATE，eos 按 family 选）
  ├─ build_hf_config：QWEN_CONFIG_06_B 的键名 → HF 键名（emb_dim→hidden_size 等）
  ├─ register_for_auto_class("AutoConfig"/"AutoModelForCausalLM")
  ├─ 建 Qwen3FromScratchForCausalLM → load_state_dict(strict=True)
  │    （setdefault 补 cos/sin 两张 RoPE 表）
  ├─ model.save_pretrained(safe_serialization=True) + tokenizer.save_pretrained
  ├─ GenerationConfig(eos, pad).save_pretrained
  └─ 写 export_info.json，返回目录
```

#### 4.3.3 源码精读

**双命名的 config**（[hf_qwen3.py:15-101](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch08/06_use_via_huggingface/export_approach/hf_qwen3.py#L15-L101)）：`Qwen3FromScratchConfig` 的形参表用 HF 惯用名（`hidden_size`、`num_attention_heads`、`rope_theta`、`max_position_embeddings`...），同时保留 from-scratch 别名入参（L49-64：`emb_dim`、`n_layers`...传了就覆盖对应 HF 字段），并且 L83-90 把两套名字**同时**存为属性——`self.emb_dim = hidden_size`。这样同一份 config 既满足 HF 的序列化约定，又对 from-scratch 风格的代码保持可读。L92 同样强制 `tie_word_embeddings=False`。

**完整重述的模型**（[hf_qwen3.py:262-297](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch08/06_use_via_huggingface/export_approach/hf_qwen3.py#L262-L297)）：`Qwen3FromScratchForCausalLM` 按配置重建 tok_emb、28 层 `TransformerBlock`、final_norm、out_head——子模块（[hf_qwen3.py:114-259](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch08/06_use_via_huggingface/export_approach/hf_qwen3.py#L114-L259)）结构与 qwen3.py 完全一致（SwiGLU、GQA、QK-Norm、RoPE），连属性名都不改，所以 `.pth` 的键能直接对上。两个真正的差别：一是 L294-295 把 RoPE 的 cos/sin 注册为**持久** buffer（qwen3.py 里是非持久，u2-l1），让它们随 safetensors 一起保存；二是 L299-309 补了 `_init_weights`——HF 从头建模型时的标准初始化钩子（本场景随即被 `load_state_dict` 覆盖，但让类行为符合 HF 规范）。

**支持 HF KV cache 的 forward**（[hf_qwen3.py:351-443](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch08/06_use_via_huggingface/export_approach/hf_qwen3.py#L351-L443)）：与 wrapper 最大的功能差异在这里。L366-378 兼容两类缓存：HF 新式 `DynamicCache`（有 `update`/`get_seq_length` 方法）与旧式「每层一个 (k, v) 元组」的 legacy 缓存（正是 from-scratch `KVCache` 的存储形态）；GQA 内部也做了双分支适配（[hf_qwen3.py:207-218](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch08/06_use_via_huggingface/export_approach/hf_qwen3.py#L207-L218)）。`_build_causal_mask`（[hf_qwen3.py:323-349](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch08/06_use_via_huggingface/export_approach/hf_qwen3.py#L323-L349)）用「键位置 > 查询位置」重建因果掩码（与 u2-l5 的切片语义一致），并在传入 `attention_mask` 时额外叠加 padding 掩码（L348-349）——这正是批量推理需要的。`prepare_inputs_for_generation`（[hf_qwen3.py:445-466](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch08/06_use_via_huggingface/export_approach/hf_qwen3.py#L445-L466)）检测到有历史缓存时只送最后一个 token，实现真正的增量解码。

**导出器**（[hf_export.py:122-212](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch08/06_use_via_huggingface/export_approach/hf_export.py#L122-L212)）：`export_qwen3_to_hf` 的关键几步——L132-133 强制 `state_dict` 与 `model_path` 二选一（训练代码内存里有 state_dict 时免落盘中转）；L176-177 把自定义 config/model 类注册到 AutoClass，这是 `AutoConfig`/`AutoModelForCausalLM` 能认识 `model_type = "qwen3_from_scratch"` 的前提；**L180-182 是本路线最微妙的两行**：

```python
state_dict.setdefault("cos", model.cos)
state_dict.setdefault("sin", model.sin)
```

from-scratch 检查点不含这两张 RoPE 表（非持久 buffer，u2-l4 讲过「表只算一次、不进 state_dict」），而导出模型把 cos/sin 注册为持久 buffer，`load_state_dict(strict=True)` 要求键必须齐全——于是用新建模型现场算出的表补上缺键，既通过 strict 检查，又把表固化进 safetensors，重载时数值完全一致。L183-188 的 strict 加载再兜底：任何键不匹配直接报错，绝不让错位权重静默通过。L190-200 依次保存模型（`safe_serialization=True` 即 safetensors 格式）、分词器与 `GenerationConfig`（eos/pad id），L202-210 写一份 `export_info.json` 元数据。

**分词器与配置桥**（[hf_export.py:84-119](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch08/06_use_via_huggingface/export_approach/hf_export.py#L84-L119)）：`build_hf_tokenizer` 用 HF 的 `PreTrainedTokenizerFast` 直接包 tokenizer JSON 文件，按家族选 eos（L29-32：base → `<|endoftext|>`，reasoning → `<|im_end|>`，与 u2-l6 的 eos 决策规则同源）；reasoning 时挂上 Jinja 聊天模板（[hf_export.py:36-39](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch08/06_use_via_huggingface/export_approach/hf_export.py#L36-L39)）。`build_hf_config` 完成 `QWEN_CONFIG_06_B` → HF 键名的逐项翻译，dtype 经 `torch_dtype_to_name`（[hf_export.py:42-50](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch08/06_use_via_huggingface/export_approach/hf_export.py#L42-L50)）转字符串以适配 JSON。

**推理脚本**（[export_approach/hf_inference.py:60-107](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch08/06_use_via_huggingface/export_approach/hf_inference.py#L60-L107)）：L64-75 是教科书式的 HF 三连——`AutoTokenizer`/`AutoConfig`/`AutoModelForCausalLM.from_pretrained(model_dir, trust_remote_code=True)`；L14-25 的 `wrap_prompt` 依据导出时记录的 `config.tokenizer_kind` 决定是否套聊天模板（有模板用模板，没模板手拼 `<|im_start|>` 包裹）；L86 分词器调用传 `add_special_tokens=False`，避免 HF 分词器自作主张加特殊 token（from-scratch 的 encode 本来就不加）。

#### 4.3.4 代码实践

1. **实践目标**：把 4.1 下载的蒸馏 checkpoint 导出为 HF 目录，并用 `AutoModelForCausalLM` 加载推理。
2. **操作步骤**（需 `pip install transformers`）：

```bash
cd ch08/06_use_via_huggingface/export_approach

# ① 导出（蒸馏 checkpoint 必须配 reasoning 分词器）
python hf_export.py \
  --output_dir hf-qwen3-distill \
  --model_path ../../../qwen3/qwen3-0.6B-distill-step06682-epoch1.pth \
  --tokenizer_kind reasoning

# ② 检查目录内容
ls hf-qwen3-distill
head -40 hf-qwen3-distill/config.json

# ③ 标准 HF 推理
python hf_inference.py --model_dir hf-qwen3-distill \
  --prompt "If x + 7 = 19, what is x?" --max_new_tokens 200 --device cpu
```

3. **需要观察的现象**：①终端输出 `Exported Hugging Face model folder ...`；②目录应含 `config.json`、`model.safetensors`、`generation_config.json`、`tokenizer.json`、`tokenizer_config.json`、`export_info.json` 及一个供 `trust_remote_code` 加载的自定义 `.py` 模块；`config.json` 里能看到 `"model_type": "qwen3_from_scratch"`、`"tokenizer_kind": "reasoning"`、`"hidden_size": 1024`、`"num_hidden_layers": 28` 等字段；③输出与 4.2.4 同为「思考流 + \boxed{12}」。
4. **预期结果**：三个步骤分别如上；导出过程只需数秒（不训练、只转存）。目录内文件清单「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：`state_dict.setdefault("cos", model.cos)` 删掉这两行会发生什么？  
**答案**：`load_state_dict(strict=True)` 会报「missing keys: cos, sin」——导出模型把它们注册为持久 buffer，state_dict 必须含这两个键；且不补表的话导出目录里也不会有 RoPE 表，重载后数值一致性失去保障。

**练习 2**：导出的模型在另一台没有装 `reasoning_from_scratch` 的机器上能直接用吗？  
**答案**：不能。`trust_remote_code=True` 加载的自定义模块 `import reasoning_from_scratch.qwen3`（复用 RMSNorm、apply_rope 等），目标环境必须先安装本仓库的包；README「Loading the export elsewhere」一节明确了这个前提。

**练习 3**：`hf_inference.py`（export 版）为什么传 `add_special_tokens=False`？  
**答案**：HF 的 `PreTrainedTokenizerFast` 默认可能往开头加 BOS 等特殊 token，而 from-scratch 训练时 `Qwen3Tokenizer.encode` 从不加；两边必须同分布，否则 prompt 编码漂移会拉低生成质量。

### 4.4 两条路线对比与选型

#### 4.4.1 概念说明

两条路线解决同一个问题（用 HF 工具驱动 from-scratch 权重），但在「复用性 ↔ 简洁性」之间做了相反的取舍。README 给出了一句话选型（[ch08/06_use_via_huggingface/README.md:38-42](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch08/06_use_via_huggingface/README.md#L38-L42)）：学习与本地轻量集成选 wrapper；要产出可分发的 HF 模型包、追求计算性能选 export。

#### 4.4.2 核心流程（对照表）

| 维度 | wrapper 路线 | export 路线 |
|---|---|---|
| 权重形态 | 保持 `.pth`，运行时灌入 | 转存为 `model.safetensors` |
| 模型代码 | 复用 `Qwen3Model`，只加薄壳 | 在 `hf_qwen3.py` 重述架构 |
| 代码量 | 最小（单文件约 250 行） | 较大（架构重写一遍） |
| 加载方式 | `build_model_and_tokenizer(...)` | `AutoModelForCausalLM.from_pretrained(目录, trust_remote_code=True)` |
| 分词器 | 仓库自有 `Qwen3Tokenizer` | HF `AutoTokenizer`（目录内文件） |
| KV cache | 不支持：每步重算全前缀（平方复杂度） | 支持 `DynamicCache` 与 legacy 元组：增量解码（线性复杂度） |
| padding 掩码 | 忽略 `attention_mask`，不能批量 | `_build_causal_mask` 叠加 padding 掩码，可批量 |
| 可分发性 | 无模型目录，仅限本仓库环境 | 标准目录，可拷贝/上传 Hub（对方仍需装本包） |
| 典型场景 | 学习、本地实验、快速加载第 6-8 章 checkpoint | 发布模型包、性能敏感推理、接入 HF Trainer 工作流 |

#### 4.4.3 源码精读（差异的证据）

两处代码差异最能体现取舍：wrapper 的 `use_cache=False` 与整段 `del`（[hf_wrapper.py:90-92](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch08/06_use_via_huggingface/wrapper_approach/hf_wrapper.py#L90-L92)、[hf_wrapper.py:115](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch08/06_use_via_huggingface/wrapper_approach/hf_wrapper.py#L115)）是「能用就行」的极简主义；export 的动态缓存检测与增量切片（[hf_qwen3.py:366-369](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch08/06_use_via_huggingface/export_approach/hf_qwen3.py#L366-L369)、[hf_qwen3.py:458-459](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch08/06_use_via_huggingface/export_approach/hf_qwen3.py#L458-L459)）是「对齐 HF 全功能」的工程主义。两者对 HF 生态的接入深度不同，但权重语义完全一致——这保证了同一路 prompt + 贪心解码下两条路线输出高度一致（数值差异仅可能来自 dtype 与后端舍入）。

#### 4.4.4 代码实践

1. **实践目标**：量化两条路线在同一 prompt 下的输出一致性与速度差异。
2. **操作步骤**：在 4.2.4 与 4.3.4 已各生成一次的基础上，把两次输出文本各存一份，逐字符比对；同时用 `time` 命令粗测两次运行耗时（如 `time python hf_inference.py ...`）。
3. **需要观察的现象**：两段文本（含 `<think>` 与 `\boxed{12}`）应基本逐字一致；export 版因 KV cache 应明显快于 wrapper 版。
4. **预期结果**：文本一致或仅有个别 token 因浮点舍入不同；速度差距倍数「待本地验证」（CPU 上两者都慢，差距在 GPU 上更显著）。

#### 4.4.5 小练习与答案

**练习**：如果你只想给第 7 章训的 GRPO checkpoint 做一次快速问答验证，选哪条路线？如果想把它发布给没用过本仓库的同学用，选哪条？  
**答案**：快速验证选 wrapper——`--model_path` 直接指向 GRPO `.pth` 即可（注意 GRPO 训的是 reasoning 变体，`--tokenizer_kind reasoning`）；发布选 export——产出标准 HF 目录，对方 `AutoModelForCausalLM` 即可加载，但需提醒安装 `reasoning_from_scratch` 包。

## 5. 综合实践

**任务：给作者发布的蒸馏 checkpoint 做一次「三段式」通关——下载、原生推理、导出后 HF 推理，并对比两条路线。**

1. **下载**：调用 `download_qwen3_distill_checkpoints(distill_type="deepseek_r1", step="06682", out_dir="qwen3")` 拿到 checkpoint 路径（若 4.1.4 已完成可复用）。
2. **原生推理（对照组）**：运行 `python ch08/05_download_training_checkpoints/usage_example.py --distill_type deepseek_r1 --step 06682 --max_new_tokens 200 --device cpu`，记录输出与耗时。这是 u3-l2 流式缓存生成的原厂用法。
3. **wrapper 推理**：按 4.2.4 的命令运行，记录输出与耗时。
4. **export 推理**：按 4.3.4 的两步命令先导出再推理，记录输出与耗时。
5. **分析报告**（写在你的笔记里）：
   - 三段输出的文本是否一致？不一致处出现在哪（思考流内还是最终答案）？
   - 三段耗时排序如何？能否用「有无 KV cache / 重算全前缀」解释？
   - 用 3 句话总结 wrapper 与 export 的实现差异（提示：权重形态、接口深度、生成策略三个维度各一句）。

预期结论：三者文本高度一致（同权重、同贪心解码、同分词器配置）；耗时上「原生 stream_cache ≈ export（有缓存）< wrapper（无缓存）」，且生成长度越长差距越大。具体数值「待本地验证」。

## 6. 本讲小结

- `download_qwen3_distill_checkpoints` 以「mapper 本地校验 + 模板拼 URL」提供两个教师家族 × 三个 epoch 档位的蒸馏 checkpoint 下载，非法参数就地 `ValueError`，只有 HF 单源、无备用 URL。
- `.pth` checkpoint 是纯 state_dict：消费它的标准姿势永远是「配置字典重建 `Qwen3Model` → `load_state_dict` → eval」，分词器必须与训练时同家族同配置（蒸馏线配 reasoning + 聊天模板 + thinking）。
- wrapper 路线不改权重、用薄壳 `PreTrainedModel` 复用 `Qwen3Model`，代价是忽略 `attention_mask`、`use_cache=False`（每步重算全前缀），换来最少代码量。
- export 路线以 HF 命名重述架构并转存为标准目录（safetensors + config.json + 分词器 + generation_config），`AutoModelForCausalLM(..., trust_remote_code=True)` 即可加载；`setdefault("cos"/"sin")` 补齐 from-scratch 检查点缺失的 RoPE 表，保证 strict 加载与数值一致。
- export 模型兼容 HF 的 `DynamicCache` 与 legacy 元组缓存，`prepare_inputs_for_generation` 只送最后一个 token 实现增量解码，还支持 padding 掩码批量推理——这是它「全功能」的实质。
- 选型口诀：本地实验与学习用 wrapper；要可分发的 HF 模型包与计算性能用 export；两条路线权重语义一致，输出应高度一致。

## 7. 下一步学习建议

本讲是单元九（第 8 章）的收官。三个自然的延伸方向：

1. **训练侧互通**：读 [ch08/06_use_via_huggingface/wrapper_approach/hf_trainer.py](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch08/06_use_via_huggingface/wrapper_approach/hf_trainer.py) 与 [ch08/06_use_via_huggingface/export_approach/hf_trainer.py](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch08/06_use_via_huggingface/export_approach/hf_trainer.py)，看 `transformers.Trainer` 如何承载第 8 章「只对答案算损失」的目标（与 u9-l4 的掩码语义对照）。
2. **进入单元十的工程专题**：推荐先读 u10-l1（`qwen3_optimized.py` 的 SDPA 与预分配 KV cache，理解本讲 wrapper 因「重算全前缀」损失的性能在优化版中如何弥补）与 u10-l2（批量推理，`_build_causal_mask` 的 padding 思路在那里被全书正式展开）。
3. **大模型加载**：若想加载 HF 官方 1.7B-32B Qwen3 权重并理解权重名映射（`q_proj` → `W_query` 等），接着读 u10-l3 与 `reasoning_from_scratch/appendix_c.py`——那里处理的问题与本讲相反：把 HF 权重灌进 from-scratch 模型。
