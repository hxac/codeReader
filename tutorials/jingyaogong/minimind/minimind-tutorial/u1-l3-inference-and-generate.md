# 第一次推理：eval_llm.py 与对话体验

## 1. 本讲目标

读完 u1-l1 和 u1-l2 之后，你已经知道 MiniMind 是什么、目录怎么分、环境怎么搭。本讲是「让模型开口说话」的第一步——**推理（Inference）**。

学完本讲你应该能够：

1. 用仓库根目录的 `eval_llm.py` 加载一份预训练或 SFT 权重，和模型完成一次对话。
2. 看懂 `--load_from` 与 `--weight` 这两个最关键的命令行参数，知道它们各自定位「哪一份权重」。
3. 区分两种权重格式：项目原生的 `torch` 权重（`.pth`）和 `transformers` 文件夹格式。
4. 初步理解 `chat_template` 的 `add_generation_prompt` 与 `open_thinking` 开关如何控制「模型要不要先思考再回答」。

本讲只讲「怎么跑起来、参数什么意思」，**不深入模型内部结构**（那是第 3 单元的事），也**不展开 generate 的采样细节**（留给 u3-l6）。

---

## 2. 前置知识

如果你对下面几个词还陌生，先花一分钟看完这段。

- **训练 vs 推理**：训练是「教模型」，让参数慢慢变好；推理是「用模型」，参数已经固定，把你的问题喂进去、让它吐出回答。本讲只涉及推理。
- **权重 / 检查点（checkpoint）**：训练得到的一大堆数字（参数），存在磁盘上就是一个文件。MiniMind 里通常长这样：`full_sft_768.pth`，文件名里 `full_sft` 是「训练阶段」，`768` 是「模型维度」。
- **tokenizer（分词器）**：把人写的句子切成一个个 token、再变成数字 id；模型只认数字。这部分细节在 u2-l1 会专门讲，本讲只要知道「它负责文本和 id 的互转」即可。
- **chat_template（对话模板）**：一段写好的「格式约定」，把多轮对话（user/assistant/system）拼成模型认识的带特殊标记的文本，例如 `<|im_start|>user\n...<|im_end|>`。本讲只用到它的两个开关：`add_generation_prompt` 和 `open_thinking`。
- **接 u1-l2**：你已经能 `import torch; print(torch.cuda.is_available())`，并且知道 `out/` 目录放训练出来的 `.pth` 权重、`model/` 目录放分词器和模型结构定义。

一句话：本讲假设你已经搭好环境、能 `cd` 进仓库根目录。

---

## 3. 本讲源码地图

| 文件 | 作用 | 本讲用到哪部分 |
|------|------|----------------|
| [eval_llm.py](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/eval_llm.py) | 仓库根目录的 CLI 推理入口，整个文件就是为「跑一次对话」服务 | 全文（仅 94 行） |
| [trainer/trainer_utils.py](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/trainer_utils.py) | 训练公共工具，本讲只借它的两个函数 | `get_model_params`、`setup_seed` |
| [model/model_minimind.py](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/model/model_minimind.py) | 模型本体定义 | `MiniMindForCausalLM.generate` 的签名 |
| [model/tokenizer_config.json](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/model/tokenizer_config.json) | 分词器配置，里面藏着 `chat_template` | `chat_template` 字段里的 `open_thinking` 分支 |
| [README.md](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/README.md) | 项目说明 | 「Ⅰ 模型推理 / 2' CLI 推理」一节 |

---

## 4. 核心概念与源码讲解

本讲对应三个最小模块：**CLI 推理章节**（参数全景）、**init_model**（权重怎么加载）、**eval_llm.main**（对话主循环）。我们一个一个拆。

### 4.1 CLI 推理：从 README 命令到参数全景

#### 4.1.1 概念说明

MiniMind 的推理有「两种身份」的权重：

1. **原生 torch 权重**：项目自己 `torch.save` 出来的 `.pth` 文件，放在 `./out/` 下，比如 `full_sft_768.pth`。加载它需要先用代码「搭出模型骨架」再把数字塞进去。
2. **transformers 文件夹格式**：一个目录，里面同时有 `config.json`、`*.safetensors` / `*.bin`、`tokenizer.json` 等，可以被 HuggingFace `transformers` 库直接 `from_pretrained` 加载。从 ModelScope/HuggingFace 下载的 `minimind-3` 就是这种格式。

README 的「CLI 推理」一节给出了两条对应命令（[README.md:L247-L254](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/README.md#L247-L254)）：

```bash
# 方式1：使用 Transformers 格式模型（下载好的文件夹）
python eval_llm.py --load_from ./minimind-3
# 方式2：基于 PyTorch 模型（确保 ./out 目录下有对应权重）
python eval_llm.py --load_from ./model --weight full_sft
```

这两条命令的区别，本质上就是「权重是文件夹还是 `.pth`」。程序怎么区分？靠的就是 `--load_from` 这个参数，下一节会看到它如何被判定。

#### 4.1.2 核心流程

`eval_llm.py` 的整体流程非常短，可以概括为 5 步：

```
1. argparse 解析命令行参数 → args
2. init_model(args)：根据 load_from 加载模型 + 分词器
3. 选择输入模式：自动跑 8 条内置 prompt，还是手动输入
4. 进入 for 循环：每条 prompt → 拼模板 → tokenize → model.generate → 解码
5. 打印回答 + 统计 tokens/s 速度
```

#### 4.1.3 源码精读

参数定义全部集中在 `main()` 里，用 `argparse` 一口气声明（[eval_llm.py:L33-L49](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/eval_llm.py#L33-L49)）。初学者不需要全背，先抓住和「找权重」直接相关的几个：

| 参数 | 默认值 | 含义 |
|------|--------|------|
| `--load_from` | `model` | 模型加载路径；`model` 走原生 torch 分支，其它路径走 transformers 分支 |
| `--save_dir` | `out` | torch 权重所在目录 |
| `--weight` | `full_sft` | 权重名前缀，如 `pretrain`、`full_sft`、`rlhf`、`ppo_actor`、`grpo` 等 |
| `--lora_weight` | `None` | 可选 LoRA 权重名，叠加在基模上 |
| `--hidden_size` | `768` | 模型维度，必须和训练时一致 |
| `--num_hidden_layers` | `8` | 层数，必须和训练时一致 |
| `--use_moe` | `0` | 是否 MoE 架构 |
| `--open_thinking` | `0` | 是否开启「显式思考」（0 否 / 1 是） |
| `--historys` | `0` | 携带历史对话轮数（偶数，0 表示不带） |
| `--temperature` / `--top_p` | `0.85` / `0.95` | 采样参数，控制随机性 |
| `--max_new_tokens` | `8192` | 最大生成长度（注意：不代表模型真实长文本能力） |

> 一个容易踩的坑：`--hidden_size` 和 `--num_hidden_layers` **必须和你那份 `.pth` 训练时用的配置完全一致**，否则 `load_state_dict` 会因为形状对不上而报错。`minimind-3` 主线默认就是 `768 / 8`，所以多数情况下不用改。

另外，`main()` 里内置了 8 条测试问句（[eval_llm.py:L51-L60](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/eval_llm.py#L51-L60)），方便你不想手打时直接看效果。

#### 4.1.4 代码实践

**实践目标**：不下载任何东西，先确认 `eval_llm.py` 能不能跑通参数解析。

**操作步骤**：

1. `cd` 到仓库根目录。
2. 执行：`python eval_llm.py --help`

**需要观察的现象**：终端会打印出所有参数及其说明（来自上一节的 `help=...` 文本）。

**预期结果**：你能看到 `--load_from`、`--weight`、`--open_thinking` 等参数的列表，说明脚本能正常解析命令行。这一步不加载模型、几乎不会失败，适合先验证环境。

#### 4.1.5 小练习与答案

**练习 1**：如果你想测一份叫 `ppo_actor_512.pth` 的权重（512 维），应该怎么写命令？

> **答案**：`python eval_llm.py --load_from model --weight ppo_actor --hidden_size 512`。注意 `--weight` 只填前缀 `ppo_actor`，维度走 `--hidden_size`，程序会自动拼出 `out/ppo_actor_512.pth`（下一节讲怎么拼）。

**练习 2**：`--max_new_tokens 8192` 是不是说模型真能稳定处理 8192 长度的上下文？

> **答案**：不是。它只是「最多允许生成这么多 token」的硬上限。模型真实的长文本能力受训练长度和位置编码限制；要真正拓展上下文，需要 `--inference_rope_scaling`（YaRN 外推，见 u3-l3），这点 README 里也专门提醒过。

---

### 4.2 init_model：两种权重加载路径

#### 4.2.1 概念说明

`init_model` 是 `eval_llm.py` 里唯一的「加载函数」，它要回答两个问题：

1. **分词器从哪来？**
2. **模型本体从哪来？**

答案都藏在 `--load_from` 里。关键在于程序用一个很朴素的判断来区分两种格式：**看 `load_from` 字符串里有没有 `model` 这个子串**。

- 有 `model`（比如默认值 `model`，或 `./model`）→ 走**原生 torch 分支**：分词器从 `./model/` 读，模型先用 `MiniMindConfig` 搭空骨架，再从 `./out/*.pth` 灌权重。
- 没有 `model`（比如 `./minimind-3`）→ 走 **transformers 分支**：分词器和模型都从这个文件夹直接 `from_pretrained`。

> 小提示：这个判定逻辑很简单也很「字符串化」，所以一般约定俗成——下载的 transformers 文件夹不要起包含 `model` 字样的名字，避免误判。日常按 README 的两种用法走就不会出错。

#### 4.2.2 核心流程

`init_model` 的伪代码如下：

```
tokenizer = AutoTokenizer.from_pretrained(load_from)   # 两种分支都要分词器
if 'model' in load_from:                                # 原生 torch 分支
    model = MiniMindForCausalLM(MiniMindConfig(...))    # 1. 搭空骨架
    ckp = f'./{save_dir}/{weight}_{hidden_size}{moe_suffix}.pth'
    model.load_state_dict(torch.load(ckp), strict=True) # 2. 灌权重
    if lora_weight != 'None':                           # 3. 可选：叠 LoRA
        apply_lora(model); load_lora(model, ...)
else:                                                   # transformers 分支
    model = AutoModelForCausalLM.from_pretrained(load_from)
get_model_params(model, model.config)                   # 打印参数量
return model.half().eval().to(device), tokenizer        # 转 fp16 + 推理模式
```

注意最后一行 `model.half().eval().to(device)`：`half()` 把模型转成半精度（fp16）省显存、加速推理；`.eval()` 关闭 dropout 等训练专用行为；`.to(device)` 搬到 GPU 或 CPU。

#### 4.2.3 源码精读

完整函数在这里（[eval_llm.py:L12-L30](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/eval_llm.py#L12-L30)）。几个关键点逐一说明：

**权重路径拼接**（[eval_llm.py:L21-L23](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/eval_llm.py#L21-L23)）：

```python
moe_suffix = '_moe' if args.use_moe else ''
ckp = f'./{args.save_dir}/{args.weight}_{args.hidden_size}{moe_suffix}.pth'
model.load_state_dict(torch.load(ckp, map_location=args.device), strict=True)
```

这就是上一节练习里「自动拼路径」的真相：文件名 = `权重前缀_维度(MoE 则加 _moe).pth`。比如 `--weight full_sft --hidden_size 768` → `./out/full_sft_768.pth`；若再加 `--use_moe 1` → `./out/full_sft_768_moe.pth`。`strict=True` 表示权重字典必须和模型结构**严丝合缝**对上，多一个少一个 key 都会报错——这也是保护你「配置写错」的最后一道防线。

**LoRA 叠加**（[eval_llm.py:L24-L26](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/eval_llm.py#L24-L26)）：

```python
if args.lora_weight != 'None':
    apply_lora(model)
    load_lora(model, f'./{args.save_dir}/{args.lora_weight}_{args.hidden_size}.pth')
```

LoRA 是一种「在基模上叠加小权重」的微调方式（细节在 u6-l1）。这里的设计是：先加载基模（`full_sft`），再 `apply_lora` 给模型装上低秩分支，最后 `load_lora` 把训练好的 LoRA 增量挂上去。于是 `--weight full_sft --lora_weight lora_identity` 就表示「`full_sft` 基模 + 自我认知 LoRA」。

**参数量统计**：第 29 行调用了 `get_model_params`，它来自 [trainer/trainer_utils.py:L18-L28](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/trainer_utils.py#L18-L28)。这个函数会把总参数量、以及 MoE 下的「激活参数量」算出来打印，对 dense 模型会输出类似 `Model Params: 64.00M`，对 MoE 输出 `Model Params: 198.00M-A64.00M`（198M 总参、64M 激活）。看到这行打印，就说明权重已经成功加载、骨架和数字对上了。

#### 4.2.4 代码实践

**实践目标**：亲手验证权重路径拼接规则，理解 `--weight` 与文件名的对应关系。

**操作步骤**（源码阅读型，不依赖 GPU）：

1. 打开 `eval_llm.py` 第 22 行，确认拼接公式。
2. 在仓库根目录写一个最小脚本（**示例代码**，非项目原有）：

   ```python
   # 示例代码：仅演示路径拼接逻辑，不加载真实模型
   save_dir, weight, hidden_size, use_moe = 'out', 'full_sft', 768, 0
   moe_suffix = '_moe' if use_moe else ''
   print(f'./{save_dir}/{weight}_{hidden_size}{moe_suffix}.pth')
   ```

3. 想象把 `weight` 换成 `pretrain`、`grpo`，把 `use_moe` 换成 `1`，分别手算预期文件名，再运行脚本对照。

**需要观察的现象**：脚本输出 `./out/full_sft_768.pth`；改参数后输出相应变化。

**预期结果**：你能准确预测 `python eval_llm.py --weight grpo --use_moe 1` 会去找 `./out/grpo_768_moe.pth`。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `--load_from model` 时，分词器要单独从 `./model/` 读，而权重却从 `./out/` 读？

> **答案**：因为 `model/` 目录放的是「结构 + 分词器定义」（`model_minimind.py`、`tokenizer_config.json` 等），而 `out/` 放的是「训练产物 `.pth`」。两者职责不同：一个描述「模型长什么样、怎么切词」，一个保存「训练学到的参数」。

**练习 2**：`strict=True` 在 `load_state_dict` 里起什么作用？如果把你把 `--hidden_size` 写错了会怎样？

> **答案**：`strict=True` 要求 state_dict 的 key 和模型结构完全一致。维度写错会导致某一层张量形状对不上，`load_state_dict` 直接抛错——这其实是好事，能在跑推理前就拦住配置错误，而不是生成一堆乱码。

---

### 4.3 eval_llm.main：对话主循环与思考开关

#### 4.3.1 概念说明

模型加载完，就进入 `main()` 的对话循环。这里有两个本讲要重点理解的概念：

- **chat_template 的 `add_generation_prompt`**：拼模板时是否在末尾补上「轮到 assistant 说话了」的起始标记 `<|im_start|>assistant\n`。推理时**必须**补，否则模型不知道该接着谁的话往下写。
- **`open_thinking` 开关**：补完起始标记后，要不要再预先塞一个 `<think>` 起始标签，诱导模型先输出一段思考过程。
  - `open_thinking=0`：模板会塞一段**空的** `<think>\n\n</think>\n\n`，相当于「想完了、啥也没想」，模型直接给答案。
  - `open_thinking=1`：模板只塞 `<think>\n`（只有开始没有结束），模型就会接着写思考内容，写完 `</think>` 再给答案。

这套机制是 `2026-04-01` 大版本后的统一设计——**不再单独训练一个「思考模型」**，而是同一个模型靠模板里的 `<think>` 标签和 `open_thinking` 开关动态切换（见 README 的「5.2 Adaptive Thinking」一节，[README.md:L859-L872](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/README.md#L859-L872)）。

另外还有一个区分点：**预训练权重 vs SFT 权重**。

- 预训练（`pretrain`）模型只会「词语接龙」，不懂对话格式，所以直接把 prompt 当纯文本续写。
- SFT 之后的模型才懂多轮对话模板，才用 `apply_chat_template`。

#### 4.3.2 核心流程

`main()` 的对话主循环（[eval_llm.py:L62-L91](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/eval_llm.py#L62-L91)）流程：

```
conversation = []
model, tokenizer = init_model(args)
输入模式选择（自动 / 手动）
for prompt in prompts:
    setup_seed(随机种子)                       # 让采样可复现又有变化
    conversation = conversation[-historys:]    # 截取最近 historys 轮
    conversation.append({"role":"user","content":prompt})
    if 'pretrain' in weight:
        inputs = bos_token + prompt            # 预训练：纯文本续写
    else:
        inputs = apply_chat_template(...,      # SFT：套对话模板
                      add_generation_prompt=True,
                      open_thinking=bool(args.open_thinking))
    inputs = tokenizer(inputs, return_tensors='pt').to(device)
    generated = model.generate(inputs, streamer=streamer, ...)
    response = decode(去掉 prompt 部分)
    conversation.append({"role":"assistant","content":response})
    打印 tokens/s
```

#### 4.3.3 源码精读

**输入模式与流式输出器**（[eval_llm.py:L64-L67](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/eval_llm.py#L64-L67)）：

```python
input_mode = int(input('[0] 自动测试\n[1] 手动输入\n'))
streamer = TextStreamer(tokenizer, skip_prompt=True, skip_special_tokens=True)
prompt_iter = prompts if input_mode == 0 else iter(lambda: input('💬: '), '')
```

`TextStreamer` 来自 `transformers`，它的作用是**一边生成一边把 token 解码打印出来**（像 ChatGPT 那样逐字蹦）。`skip_prompt=True` 表示不重复打印你输入的 prompt，`skip_special_tokens=True` 表示不把 `<|im_end|>` 这类特殊标记打印出来。

**模板分支**（[eval_llm.py:L73-L76](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/eval_llm.py#L73-L76)）：

```python
if 'pretrain' in args.weight:
    inputs = tokenizer.bos_token + prompt
else:
    inputs = tokenizer.apply_chat_template(conversation, tokenize=False,
                add_generation_prompt=True, open_thinking=bool(args.open_thinking))
```

注意判断条件 `'pretrain' in args.weight`——只要权重名里带 `pretrain` 就走纯文本续写。所以测试预训练模型时，`open_thinking` 是无效的（因为根本没套模板）。

`apply_chat_template` 把 `conversation`（一个 list of dict）渲染成模型认识的字符串。`tokenize=False` 表示先返回字符串、稍后我们自己 tokenize；`add_generation_prompt=True` 就是上一节说的「补 assistant 起始标记」。

**`open_thinking` 在模板里的真实分支**藏在 [tokenizer_config.json:L333](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/model/tokenizer_config.json#L333) 的 `chat_template` 字段（一段 Jinja 模板），关键几行展开后是：

```jinja
{%- if add_generation_prompt %}
    {{- '<|im_start|>assistant\n' }}
    {%- if open_thinking is defined and open_thinking is true %}
        {{- '<think>\n' }}              {# 只有起始标签，诱导模型写思考 #}
    {%- else %}
        {{- '<think>\n\n</think>\n\n' }} {# 空思考块，模型直接作答 #}
    {%- endif %}
{%- endif %}
```

这就是「思考开关」的物理实现：开关不同，喂给模型的「前缀文本」就差一个 `</think>`，生成行为随之不同。

**生成与解码**（[eval_llm.py:L82-L88](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/eval_llm.py#L82-L88)）：

```python
generated_ids = model.generate(
    inputs=inputs["input_ids"], attention_mask=inputs["attention_mask"],
    max_new_tokens=args.max_new_tokens, do_sample=True, streamer=streamer,
    pad_token_id=tokenizer.pad_token_id, eos_token_id=tokenizer.eos_token_id,
    top_p=args.top_p, temperature=args.temperature, repetition_penalty=1)
response = tokenizer.decode(generated_ids[0][len(inputs["input_ids"][0]):], skip_special_tokens=True)
```

`model.generate` 是项目**自定义**的生成方法（不是 transformers 默认那个），定义在 [model/model_minimind.py:L257](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/model/model_minimind.py#L257)。本讲只要知道：它逐 token 自回归地生成，直到遇到 `eos_token_id`（句末）或达到 `max_new_tokens`。`do_sample=True` 表示用 `temperature/top_p` 做随机采样（每次回答都可能不同）；深入到 KV Cache、top-k/top-p 实现细节留给 u3-l6。

解码那行 `generated_ids[0][len(inputs["input_ids"][0]):]` 的意思：生成结果里**前面那段是输入的 prompt**，要切片丢掉，只保留真正「新生成」的部分，再 decode 成文字。

**速度统计**（[eval_llm.py:L90-L91](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/eval_llm.py#L90-L91)）：用 `生成 token 数 / 耗时` 算出 `tokens/s`，对应 `--show_speed` 开关。

#### 4.3.4 代码实践

**实践目标**：亲手对比 `open_thinking` 开与关时，喂给模型的文本前缀有何不同；并在能联网下载权重时，跑一次真实对话。

**操作步骤**：

1. **先做不依赖权重的模板观察**（源码阅读型）。在仓库根目录写一段**示例代码**：

   ```python
   # 示例代码：观察 open_thinking 对 prompt 前缀的影响
   from transformers import AutoTokenizer
   tok = AutoTokenizer.from_pretrained('./model')
   conv = [{"role": "user", "content": "你好"}]
   for ot in [False, True]:
       s = tok.apply_chat_template(conv, tokenize=False,
                add_generation_prompt=True, open_thinking=ot)
       print(f'--- open_thinking={ot} ---\n{s}')
   ```

2. 运行它，对比两次输出末尾的差异。

3. **（可选，需先下载权重）真实对话**。按 README 第 1 步下载模型后，分别跑：

   ```bash
   python eval_llm.py --load_from ./minimind-3 --open_thinking 0
   python eval_llm.py --load_from ./minimind-3 --open_thinking 1
   ```

   选 `[1] 手动输入`，问同一个问题，比如「解释什么是机器学习」。

**需要观察的现象**：
- 步骤 2 中，`open_thinking=False` 的前缀结尾是 `<think>\n\n</think>\n\n`；`open_thinking=True` 的结尾只有 `<think>\n`。
- 步骤 3 中，`open_thinking=1` 时模型会先输出一段思考再给答案；`open_thinking=0` 时直接给答案。

**预期结果**：步骤 2 的文本差异几乎可以 100% 复现（只取决于模板）。步骤 3 的对话内容因采样随机会有波动，且小模型在「同时思考 + 准确作答」上未必稳定（README 也提到 toolcall 与思考同时开启时模型尚不稳定）。若本地暂无权重或无 GPU，步骤 3 标注为「待本地验证」，单做步骤 2 即可完成本讲实践。

> README 给出的 SFT 参考输出（[README.md:L722-L729](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/README.md#L722-L729)）可以作为「理想情况下 full_sft 模型该答成什么样」的对照基准。

#### 4.3.5 小练习与答案

**练习 1**：为什么测试预训练模型（`--weight pretrain`）时，加 `--open_thinking 1` 没有效果？

> **答案**：因为 `main` 在 `'pretrain' in args.weight` 时走的是 `inputs = tokenizer.bos_token + prompt` 分支，根本没有调用 `apply_chat_template`，自然也不会注入 `<think>` 标签。预训练模型只懂纯文本续写，不懂对话模板和思考标签。

**练习 2**：`conversation = conversation[-args.historys:]` 这一句（[eval_llm.py:L71](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/eval_llm.py#L71)）的作用是什么？为什么 `--historys` 要求是偶数？

> **答案**：它的作用是只保留「最近 `historys` 条消息」作为上下文，控制输入长度、省显存。要求偶数是因为对话以「一问一答」成对出现：偶数条消息意味着以一条 `user` 结尾、刚好轮到 assistant 回答；奇数会把上一轮的 assistant 回答截断，导致模板里「该谁说话」错乱。

**练习 3**：解码时为什么要写成 `generated_ids[0][len(inputs["input_ids"][0]):]` 而不是直接 decode 整个 `generated_ids`？

> **答案**：因为 `generate` 返回的序列是「输入 prompt + 新生成内容」拼在一起的完整序列。直接 decode 会把你的问题也重复打印一遍，所以要用输入长度做偏移切掉前缀，只保留模型新生成的回答。

---

## 5. 综合实践

把本讲三块知识串起来，完成下面这个端到端小任务。

**任务**：用 `eval_llm.py` 同时验证「两种加载方式」和「思考开关」。

**步骤**：

1. 确认环境（接 u1-l2）：`python -c "import torch; print(torch.cuda.is_available())"`。
2. 如果你已经训练过（或下载过）权重：
   - 走 torch 分支：`python eval_llm.py --load_from model --weight full_sft --open_thinking 1`，记下终端打印的 `Model Params: ...M` 那一行（来自 `get_model_params`），以及一次回答的 `tokens/s`。
   - 走 transformers 分支：`python eval_llm.py --load_from ./minimind-3 --open_thinking 1`，对比两次是否都能正常对话。
3. 在同一份权重上，分别用 `--open_thinking 0` 和 `--open_thinking 1` 问同一个问题，把两次输出贴在一起，**圈出 `open_thinking=1` 时多出来的 `<think>...</think>` 段落**。
4. 如果暂时没有权重：用 4.3.4 的「示例代码」直接观察模板前缀差异，并在 `eval_llm.py` 里追一遍 `main → init_model → apply_chat_template → generate` 这条调用链，画出每一步的输入输出类型（字符串 → tensor → token id 序列 → 文本）。

**验收标准**：你能用自己的话讲清楚——`--load_from` 怎么决定走哪个加载分支、`--weight` 怎么拼出 `.pth` 文件名、`open_thinking` 怎么通过模板改变模型前缀从而改变生成行为。

---

## 6. 本讲小结

- `eval_llm.py` 是仓库根目录的 CLI 推理入口，仅 94 行，流程是「解析参数 → init_model → 对话循环 → generate → 解码打印」。
- 权重有两种格式：原生 torch 的 `.pth`（放 `./out/`）和 transformers 文件夹（如 `./minimind-3`）；`init_model` 靠 `'model' in load_from` 这一字符串判断走哪个分支。
- torch 分支下，权重文件名 = `{weight}_{hidden_size}{_moe?}.pth`，由 `--weight`、`--hidden_size`、`--use_moe` 拼接得到；`--lora_weight` 可在基模上再叠加 LoRA。
- 预训练权重走纯文本续写（`bos_token + prompt`），SFT 权重才套 `apply_chat_template`；`add_generation_prompt=True` 是推理时必须开的开关。
- `open_thinking` 不是单独的模型，而是模板里 `<think>` 标签的开关：`0` 注入空 `<think>\n\n</think>`、`1` 只注入起始 `<think>\n`，从而让同一模型动态切换「直答 / 先思考」。
- `model.generate` 是项目自定义的自回归生成方法，配合 `TextStreamer` 实现逐字流式输出；采样细节（temperature/top_p/KV Cache）留给 u3-l6。

---

## 7. 下一步学习建议

到这里，你已经能让 MiniMind 说话了。接下来推荐：

- **想搞懂「模型吃什么」**：进入 u2-l1（Tokenizer、BPE 与 chat_template）和 u2-l2（数据集与标签构造），本讲里一笔带过的 `apply_chat_template`、`bos_token`、特殊标记都会在那里被彻底拆开。
- **想搞懂「模型内部怎么算」**：进入 u3 单元。从 u3-l1（Config 与骨架）开始，逐层拆到注意力、RoPE、FFN，最后在 u3-l6 回过头来精读本讲提到的 `MiniMindForCausalLM.generate`。
- **想直接训练**：可以跳到 u5-l1（预训练）先用 `pretrain_t2t_mini.jsonl` 跑一个自己的 `pretrain_768.pth`，再用本讲的 `python eval_llm.py --weight pretrain` 测试续写效果，形成「训练 → 推理」的完整闭环。
