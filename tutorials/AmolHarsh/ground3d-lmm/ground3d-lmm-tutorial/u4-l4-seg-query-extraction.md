# u4-l4 `<SEG>` 查询提取:从隐状态到分割查询

## 1. 本讲目标

上一讲(u4-l3)我们跟踪了训练语言分支 `infer_reason_train`:它把 QA 对组成多轮 ChatML 对话、编码成 `input_ids`、用 `preprocess_chatml` 生成只在 assistant 回复上计算的语言损失。本讲接着回答一个关键问题:

> **模型回答里的 `<p>…</p><SEG>` 标记,是如何变成 Mask2Former 式解码器可以使用的 256 维分割查询的?**

学完本讲,你应该能够:

1. 说清 `get_query_tokens`、`get_query_tokens_id`(训练版)与 `get_query_tokens_eval`(推理版)三个方法的分工与差异。
2. 解释「按 assistant 回复切分 token 区间」的两种坐标系:文本坐标系(字符/子词计数)与 id 坐标系(input_ids 定位),以及为什么后者更稳。
3. 描述 `<p>` 与 `</p>` 之间的隐状态如何经均值池化、再经 `query_proj` 投影成 256 维分割查询。
4. 理解无效查询(没有 `<p>…</p>` 的 QA、超出的轮次、空 qa_data)的零向量/NaN 兜底如何在解码器、损失函数两处被识别并跳过。

一个先泼冷水的澄清(本讲最重要的精读发现之一):**虽然项目语境口语化地称之为「`<SEG>` 查询」,但源码实际提取的并不是 `<SEG>` 这个 token 的隐状态,而是 `<p>` 与 `</p>` 之间内容(即被指称的物体/部件名称文本)的隐状态均值**。细节见 4.1.3。

## 2. 前置知识

- **隐状态(hidden states)**:Transformer 每一层对每个输入 token 输出一个向量。取「最后一层的隐状态」即 `output.hidden_states[-1]`,形状为 `(batch=1, seq_len, hidden_size)`——序列里每个 token 位置对应一行向量,它可视为该位置上「模型读完全部上文后」的语义表示。
- **ChatML 对话格式**:Qwen 系列模型的多轮对话模板,形如 `<|im_start|>user\n…<|im_end|>\n<|im_start|>assistant\n…<|im_end|>`。`<|im_start|>`/`<|im_end|>` 是特殊 token,各占一个 id。本仓库中模板分隔符定义在 [uniseg3d/conversation.py:55-56](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/conversation.py#L55-L56):`roles = ('\n<|im_start|>user\n', '\n<|im_start|>assistant\n')`,`seps = ('<|im_end|>', '<|im_end|>')`。
- **接地标记**:`<p>object name</p><SEG>` 表示「这里提到的 object name 需要给出对应的点云分割」。模板原文见 [uniseg3d/prompt_template.py:36-43](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/prompt_template.py#L36-L43)。
- **teacher forcing**:训练时 assistant 回复是喂给模型的**标准答案(GT)**,模型逐 token 读到正确答案;因此 `<p>…</p>` 一定出现在输入序列中,其隐状态是「模型读到正确指称」时的状态。推理时则换成模型自己生成的回答(u4-l5)。
- **零向量 / NaN 哨兵**:数据侧(u2-l4)用 NaN 行占位「取不到有效 GT mask」的查询;解码器侧则用**零向量**占位「提取不到 `<p>…</p>`」的查询。两者都是「无效行」的哨兵(sentinel),下游靠 `sum != 0` / `isnan` 识别。
- **坐标系的错位问题**(承接 u4-l3 的遗留隐患):`processor` 编码时,消息里的图像条目会被展开成成百上千个视觉 patch token,导致「文本字符串的 token 计数」与「实际 `input_ids` 的长度」不再一一对应。凡是靠文本长度推算 token 区间的逻辑,在 Joint(带图)变体上都有错位风险。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [uniseg3d/query_decoder.py](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py) | 本讲主战场:`Grounded_Decoder_Joint` 的三个查询提取方法、`query_proj` 投影层、`_get_queries` 调用点、无效查询兜底 |
| [uniseg3d/prompt_template.py](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/prompt_template.py) | `SEG_TOKEN`/`POINT_TOKEN` 常量定义、含 `<p>…</p><SEG>` 指令模板 |
| [uniseg3d/text_prompt_instance_criterion.py](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/text_prompt_instance_criterion.py) | 损失侧如何用「投影前的原始查询」过滤零向量/NaN 无效行 |
| [configs/ground3dlmm_train.py](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/configs/ground3dlmm_train.py) | 解码器构造参数:`d_model=256`、`num_instance_queries=0`(无可学习查询,查询全部来自语言侧) |
| [uniseg3d/conversation.py](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/conversation.py) | ChatML 模板的 `roles`/`seps`,决定 assistant 区间的切分规则 |

## 4. 核心概念与源码讲解

### 4.1 从回答文本到分割查询:机制总览

#### 4.1.1 概念说明

Ground3D-LMM 是「一个模型同时输出自然语言回答与点云分割」的架构。语言塔(Qwen3-VL)负责生成回答;分割解码器(Mask2Former 式,6 层、256 维)负责输出逐超点的 mask。两者之间需要一座桥:**回答里每提到一个要接地的物体/部件,分割解码器就需要一个对应的查询向量(query)**。

这座桥不引入任何额外的检测头,而是直接**复用语言塔最后一层隐状态**:回答里 `<p>…</p>` 标记包住了被指称的对象,这段位置的隐状态已经「知道」指称的是什么、在点云中大致对应哪里——把它取出来、投影到解码器维度,就是分割查询。这样语言与分割在同一前向内对齐,这也是 u1-l1 所说「`<SEG>` 接地机制」的具体实现层。

#### 4.1.2 核心流程

三个方法共享同一段「提取内核」,差别只在**如何确定搜索范围**:

```text
训练(teacher forcing,序列 = prompt + 全部 GT 回复):
  get_query_tokens(文本坐标系)   按 ChatML 文本轮切分累计 token 长度,推算每条 QA 的 assistant 区间
  get_query_tokens_id(id 坐标系)  在 input_ids 中找 <|im_start|>+assistant … <|im_end|> 得到区间   ← 实际使用
推理(序列 = prompt + 模型生成):
  get_query_tokens_eval            输入只有生成段,无需切轮,全局搜 <p>…</p>

提取内核(三者相同):
  对每个 <p> 起点:
      找其后第一个未占用的 </p> 终点
      取 hidden[p_start+1 : p_end]          # <p> 与 </p> 之间内容的隐状态
      行均值池化 → 1 个查询向量
  每条 QA 最多保留 2 个查询(qa_queries[:2],与数据侧 <p> 个数封顶 2 的契约对齐)
  该 QA 无 <p>…</p> / 轮次缺失 → 补 1 行零向量(注释写 NaN,实现是 zeros,见 4.4)
输出:各行按「QA 顺序 × 每 QA 内 <p> 顺序」拼接成 (总查询数, hidden_size) 张量
```

均值池化的含义:设某 `<p>…</p>` 之间有 \(k\) 个 token,其隐状态为 \(h_1,\dots,h_k \in \mathbb{R}^{D_h}\),则查询向量为

\[ q = \frac{1}{k}\sum_{i=1}^{k} h_i \]

#### 4.1.3 源码精读

**(1)先看常量:`SEG_TOKEN` 到底有哪些 token。**

[uniseg3d/prompt_template.py:2-4](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/prompt_template.py#L2-L4) 定义了特殊 token 清单:

```python
COT_TOKENS = ["<think>", "</think>", "<answer>", "</answer>"]
POINT_TOKEN = ["<|point|>"]
SEG_TOKEN = ["<p>", "</p>", "<SEG>", "<name>", "<sentence>"]
```

`SEG_TOKEN` 是一个**列表**,前两个元素是 `<p>` 与 `</p>`。它在 `_init_reason_model` 中被注册进词表并转成 id:

[uniseg3d/query_decoder.py:980-994](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L980-L994) 注册特殊 token、扩容词表,并缓存 id:

```python
self.seg_tokens = SEG_TOKEN
self.point_token = POINT_TOKEN[0]
special_tokens = SEG_TOKEN + POINT_TOKEN
new_tokens = self.processor.tokenizer.add_special_tokens(...)
...
self.seg_tokens_ids = self.processor.tokenizer.convert_tokens_to_ids(self.seg_tokens)
self.point_token_id = self.processor.tokenizer.convert_tokens_to_ids(self.point_token)
```

**(2)再看提取内核到底取了什么。** 三个方法里的关键三行(以 `get_query_tokens_id` 为例,[uniseg3d/query_decoder.py:1249-1253](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L1249-L1253)):

```python
if p_end > p_start + 1:
    between_embeddings = hidden_flat[p_start + 1 : p_end]
    avg_embedding = between_embeddings.mean(dim=0, keepdim=True)
    qa_queries.append(avg_embedding)
```

`p_start` 是 `<p>` 的位置,`p_end` 是 `</p>` 的位置,切片 `[p_start+1 : p_end]` 取的正是**两个标记之间的内容**(即物体/部件名称那段文本)的隐状态,然后取行均值。`<SEG>` 这个 token 本身的隐状态并未被使用——「`<SEG>` 查询」是沿用 GLaMM 系工作的一种口语称呼,源码层面提取的是 `<p>…</p>` 区间均值。另外注意 `if p_end > p_start + 1`:若 `<p>` 与 `</p>` 相邻(中间没有内容),则什么都不取。

**(3)配置侧:为什么查询只能来自语言侧。**

[configs/ground3dlmm_train.py:47-63](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/configs/ground3dlmm_train.py#L47-L63) 中解码器配置了 `num_instance_queries=0, num_semantic_queries=0`。父类 `QueryDecoder.__init__` 里,只有当两者之和大于 0 时才创建可学习查询嵌入([uniseg3d/query_decoder.py:481-483](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L481-L483));本配置下不存在 `self.query`,解码器的查询**全部**由本讲的方法从隐状态提取。

#### 4.1.4 代码实践

**实践:用肉眼数一遍「查询行数」。** 不运行任何代码,只读数据流约定:

1. 实践目标:建立「回答文本 → 查询行数」的直觉。
2. 操作步骤:
   - 阅读指令模板 [uniseg3d/prompt_template.py:36-43](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/prompt_template.py#L36-L43),确认回答约定为 `<p>object name</p><SEG>`。
   - 假设一个 batch 内某场景采样到 3 条 QA(u2-l4 的 `num_qa` 采样),GT 回答分别是:
     - QA1:`The <p>table top</p><SEG> is about 0.8 meters high.`(1 个 `<p>`)
     - QA2:`You should choose <p>left chair seat</p><SEG> and <p>right chair back</p><SEG>.`(2 个 `<p>`)
     - QA3:`There are four chairs in the room.`(0 个 `<p>`)
   - 手推 `get_query_tokens_id` 的输出行数与每行来源。
3. 需要观察的现象:QA1 → 1 行(区间 `table top` 的均值);QA2 → 2 行(封顶恰好是 2);QA3 → 1 行**零向量**。
4. 预期结果:总行数 = 1 + 2 + 1 = 4 行,形状 `(4, hidden_size)`;第 4 行全零。第 4 行随后会在损失侧被 `sum != 0` 过滤(见 4.4)。

#### 4.1.5 小练习与答案

**练习 1**:为什么查询取「`<p>…</p>` 区间均值」而不是 `<SEG>` 单个 token 的隐状态?
**答案**:源码事实是前者(4.1.3 第 (2) 点)。设计动机可理解为:名称可能由多个 token 组成(如 `left chair seat`),取区间均值能得到一个稳定的「指称语义」向量;而 `<SEG>` 在训练数据里是紧跟名称后的固定标记,其隐状态更多承载「该接地了」的触发信息。注意:不要在论文复现时想当然地取 `<SEG>` 位置的向量——以代码为准。

**练习 2**:每条 QA 最多取 2 个查询(`qa_queries[:2]`),这个 2 与数据侧的什么契约对齐?
**答案**:与 u2-l4 讲过的形状契约对齐——数据管线里 GT 分割 `sp_gt_seg` 的形状由答案中 `<p>` 标签个数决定,且封顶 2(`(num_p_tags, 超点数)`)。查询行与 GT 行按行序一一对应,超过 2 的 `<p>` 既不产生 GT 也不产生查询。

### 4.2 训练版提取:`get_query_tokens` 与 `get_query_tokens_id`

#### 4.2.1 概念说明

训练时序列是「指令 + `<|point|>` 占位 + 全部 GT 回复」拼成的整条 ChatML 文本,多条 QA 的回答**都在同一条序列里**。要按 QA 分组提取查询,必须先知道「第 i 条 QA 的 assistant 回复落在 `input_ids` 的哪个区间」。

两个版本给了两种定位方式:

- `get_query_tokens`:**文本坐标系**。沿用 `preprocess_chatml` 的轮次切分,用 tokenizer 对每个文本轮重新计数,累计出区间边界。它是历史版本,保留在文件中(`Grounded_Decoder_Eval` 里还有一份副本,见 [uniseg3d/query_decoder.py:2155](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L2155) 起)。
- `get_query_tokens_id`:**id 坐标系**。直接在 `input_ids` 里找 `<|im_start|>` 后跟 `assistant` 的位置与其后的 `<|im_end|>`,得到区间。这是 `infer_reason_train` **实际调用**的版本([uniseg3d/query_decoder.py:1434](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L1434))。

它解决的正是 u4-l3 末尾留下的隐患:Joint 变体带图像条目,`processor` 会把图像占位展开成大量视觉 patch token,文本 token 计数与 `input_ids` 实际长度错位;凡是靠「数文本」推算的位置都可能偏移。id 坐标系天然免疫这个问题,因为 `<p>`/`</p>`/`<|im_start|>`/`<|im_end|>` 本身就是 input_ids 里的真实 id。

#### 4.2.2 核心流程

`get_query_tokens_id` 的流程:

```text
输入:hidden_states (1, L, D_h)、input_ids (1, L)、num_qa
1. 在 input_ids 中定位所有 <p>、</p>、<|im_start|>、<|im_end|> 的下标
2. 构造 assistant 区间列表:
     对每个 <|im_start|> 位置 s:
       若其后 2 个 token 内出现 'assistant' 这个 token:
         content_start = s + 1 + 'assistant' 的偏移 + 1     # 跳过 "assistant"
         assistant_end = content_start 之后第一个 <|im_end|> 的位置
         记录区间 (content_start, assistant_end)
3. 对第 i 条 QA(i < num_qa):
     i 超出区间数 → 补 1 行零向量
     否则在区间内按顺序找 <p>…</p>(每个 </p> 只许配对一次):
       区间均值池化 → 查询行;最多 2 行
     一行都没有 → 补 1 行零向量
4. 所有行 cat 成 (总查询数, D_h) 返回
```

`get_query_tokens` 与之只在第 2 步不同:它用 `text.split(seps[0])` 切轮、`tokenizer(轮文本)` 计数、`cur_len` 累计,得到 `assistant_start = cur_len + ins_len`、`assistant_end = cur_len + rou_len`——全部是**文本计数**推出来的位置。

#### 4.2.3 源码精读

**(1)id 坐标系的 assistant 区间定位。**

[uniseg3d/query_decoder.py:1183-1224](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L1183-L1224) 先取四个特殊 token 的 id 与全部出现位置,再扫描 `<|im_start|>`:

```python
im_start_id = tokenizer.convert_tokens_to_ids('<|im_start|>')
im_end_id = tokenizer.convert_tokens_to_ids('<|im_end|>')
...
assistant_token_id = tokenizer.convert_tokens_to_ids('assistant')

assistant_ranges = []  # [(start, end), ...]
for im_start_pos in im_start_positions:
    # the usual format is <|im_start|>assistant\n
    check_end = min(im_start_pos + 3, len(input_ids_flat))
    following_ids = input_ids_flat[im_start_pos + 1 : check_end].tolist()

    if assistant_token_id in following_ids:
        assistant_content_start = im_start_pos + 1 + following_ids.index(assistant_token_id) + 1
        assistant_end = None
        for im_end_pos in im_end_positions:
            if im_end_pos > assistant_content_start:
                assistant_end = im_end_pos
                break
        if assistant_end is not None:
            assistant_ranges.append((assistant_content_start, assistant_end))
```

这段代码做的事情:ChatML 中 assistant 回复总是以 `<|im_start|>assistant\n` 开头,所以「`<|im_start|>` 后两三个 token 内出现 `assistant`」即判定为回复头;回复体从 `assistant` 的下一个 token 开始,到其后第一个 `<|im_end|>` 结束(开区间,不含 `<|im_end|>` 本身)。注意 `assistant` 是普通词元而非特殊 token,`convert_tokens_to_ids('assistant')` 取到的是它在词表中的正常 id。

**(2)逐 QA 提取与零向量兜底。**

[uniseg3d/query_decoder.py:1228-1263](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L1228-L1263):

```python
for i in range(num_qa):
    if i >= len(assistant_ranges):
        # pad with zero vectors (consistent with get_query_tokens)
        all_query_tokens.append(torch.zeros(1, hidden_dim, device=device))
        continue

    assistant_start, assistant_end = assistant_ranges[i]
    ...
    if len(qa_queries) > 0:
        all_query_tokens.append(torch.cat(qa_queries[:2], dim=0))  # 最多 2 行
    else:
        # this QA has no <p>...</p>, fill with zero vector
        all_query_tokens.append(torch.zeros(1, hidden_dim, device=device))

all_query_tokens = torch.cat(all_query_tokens, dim=0)
```

要点:第 i 条 QA 对应第 i 个 assistant 区间(顺序对齐,不做内容匹配);`used_end_positions` 集合保证每个 `</p>` 只被配对一次,避免嵌套/交错标记时重复取区间。

**(3)文本坐标系版的区间推算(对照)。**

[uniseg3d/query_decoder.py:1082-1121](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L1082-L1121) 中,`get_query_tokens` 用与 `preprocess_chatml`(u4-l3)完全相同的轮次切分与 `cur_len` 累计,然后:

```python
sep = conv.seps[0] + conv.roles[1]  # '<|im_end|>\n<|im_start|>assistant\n'
...
ins = sep.join(parts[:-1]) + sep
ins_len = tokenizer(ins, return_length=True).length[0]
assistant_start = cur_len + ins_len
assistant_end = cur_len + rou_len
```

注意一个微妙的不一致:这里用 `text.split` 切出的轮次计数,但 `<p>` 的候选位置 `p_start_positions` 却来自 `input_ids`(见 [uniseg3d/query_decoder.py:1078-1079](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L1078-L1079))——两个坐标系混用,一旦图像展开使二者错位,`assistant_start/end` 就可能落偏,查询行会被算到错误的 QA 头上。这就是它被 `get_query_tokens_id` 取代的原因。

**(4)训练中的实际调用点。**

[uniseg3d/query_decoder.py:1430-1435](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L1430-L1435),在 `infer_reason_train` 内:

```python
output = self.reason_model(**data.to(self.reason_model.device), labels=labels,
                           output_hidden_states=True)
lang_loss = output.loss

hidden_states = output.hidden_states[-1]
query_tokens = self.get_query_tokens_id(hidden_states, data['input_ids'], len(qa_pairs))
query_list.append(query_tokens)
```

一次前向同时产出语言损失(`output.loss`,由 `labels` 驱动)与最后一层隐状态;`num_qa = len(qa_pairs)` 是本场景展平后的 QA 条数。`query_list` 逐场景收集,之后交给 `forward_iter_pred` 的训练分支(见 4.4.3 第 (2) 点)。

#### 4.2.4 代码实践

**实践:离线驱动 `get_query_tokens_id`,用「常量隐状态」验证区间均值提取。** 本实践不需要下载 Qwen 权重、不需要 GPU,只需 u1-l2 装好的 Python 环境(torch 可用即可)。

1. 实践目标:亲手验证 (a) 提取的确实是 `<p>…</p>` 区间均值;(b) 无 `<p>` 的 QA 落入零向量分支;(c) `num_qa` 超出 assistant 区间数时用零向量填充。
2. 操作步骤:在**仓库外**(如 `~/probe/`)新建 `probe_seg_query.py`,内容如下(示例代码,逻辑逐行取自 [uniseg3d/query_decoder.py:1159-1263](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L1159-L1263),仅以假对象替换 `self`):

   ```python
   # ~/probe/probe_seg_query.py  (示例代码)
   import sys, types, torch
   sys.path.insert(0, '/path/to/ground3d-lmm')   # 改成你的仓库根目录
   from uniseg3d.query_decoder import Grounded_Decoder_Joint

   D_H = 8                                  # 用小维度模拟 text hidden_size
   ID = dict(p_s=101, p_e=102, im_s=103, im_e=104, asst=105, txt=99)

   class FakeTokenizer:                     # 只需实现 convert_tokens_to_ids
       def convert_tokens_to_ids(self, t):
           return {'<p>': ID['p_s'], '</p>': ID['p_e'],
                   '<|im_start|>': ID['im_s'], '<|im_end|>': ID['im_e'],
                   'assistant': ID['asst']}[t]

   fake_self = types.SimpleNamespace(
       seg_tokens_ids=[ID['p_s'], ID['p_e']],
       processor=types.SimpleNamespace(tokenizer=FakeTokenizer()))

   def tok(*xs):                            # 把伪 id 序列拼成一维张量
       return torch.tensor(sum(([x] if isinstance(x, int) else x for x in xs), []))

   # 两条 QA:QA1 两个 <p> 区间;QA2 无 <p>
   seq = tok(
       ID['im_s'], ID['asst'],                       # user 头(非 assistant 判定应为 False? 注意 asst 紧跟 im_s 会被当成 assistant!)
   )
   # ↑ 上面一行故意演示陷阱:构造时 user 轮的 <|im_start|> 后不能直接跟 'assistant'
   seq = tok(
       ID['im_s'], ID['txt'], ID['txt'], ID['im_e'],          # user 轮(普通文本)
       ID['im_s'], ID['asst'],                                  # QA1 assistant 头
       ID['txt'], ID['p_s'], 111, 112, ID['p_e'],               # <p> A B </p>
       ID['p_s'], 113, ID['p_e'], ID['txt'], ID['im_e'],        # <p> C </p>
       ID['im_s'], ID['txt'], ID['im_e'],                       # user 轮
       ID['im_s'], ID['asst'],                                  # QA2 assistant 头
       ID['txt'], ID['txt'], ID['im_e'],                        # 无 <p>
   )
   L = seq.numel()
   hidden = torch.randn(1, L, D_H)
   for a, b in ((111, 112), (113,)):        # 在两个 <p> 区间埋入可识别的常量向量
       hidden[0, L - len(seq) + 0:0] = 0    # (占位,下面直接按下标写)
   idx = (seq == 111).nonzero().item()
   hidden[0, idx] = torch.tensor([1., 0, 0, 0, 0, 0, 0, 0])
   idx = (seq == 112).nonzero().item()
   hidden[0, idx] = torch.tensor([1., 0, 0, 0, 0, 0, 0, 0])
   idx = (seq == 113).nonzero().item()
   hidden[0, idx] = torch.tensor([0, 2., 0, 0, 0, 0, 0, 0])

   out = Grounded_Decoder_Joint.get_query_tokens_id(
       fake_self, hidden, seq.unsqueeze(0), num_qa=2)
   print('shape =', tuple(out.shape))
   print(out)
   print('第 1 行(区间 A,B 均值):', out[0].tolist())
   print('第 2 行(区间 C 均值):  ', out[1].tolist())
   print('第 3 行(QA2 无 <p>):   ', out[2].tolist(),
         '全零?', bool((out[2] == 0).all()))
   ```

   依赖说明:`import uniseg3d.query_decoder` 会连带 import transformers/peft 等库(u1-l2 环境),但**不会**触发权重下载。把脚本里的埋值三行整理干净后运行:`python ~/probe/probe_seg_query.py`(本讲义受限于撰写环境未能代跑,脚本行为**待本地验证**;埋值部分写得冗余,请自行整理为按下标直接赋值)。
3. 需要观察的现象:输出形状 `(3, 8)`;第 1 行约等于 `[1,0,...]`(两个 `[1,0,...]` 常量的均值)、第 2 行约等于 `[0,2,0,...]`、第 3 行全零。
4. 预期结果:验证 4.2.2 的三条结论。若把 `num_qa` 改成 3,应得到 `(4, 8)` 且第 4 行全零(区间数不足,零向量填充)——同样待本地验证。另外注意脚本第一处 `seq = tok(...)` 注释演示的陷阱:`<|im_start|>` 后紧跟 `assistant` 才算回复头,构造 user 轮时勿让普通文本恰好是 `assistant` 词元。

#### 4.2.5 小练习与答案

**练习 1**:`get_query_tokens` 与 `get_query_tokens_id` 各自如何得到第 i 条 QA 的 assistant 区间?为什么训练实际用后者?
**答案**:前者按 ChatML 文本切轮并对每轮重新分词计数、`cur_len` 累计推算(文本坐标系);后者在 `input_ids` 里找 `<|im_start|>`+`assistant` 头与其后第一个 `<|im_end|>`(id 坐标系)。后者更稳,因为 processor 会把图像条目展开成大量视觉 token,文本计数与 input_ids 长度错位,文本坐标系推算的区间会偏移;`<p>` 等特殊 token 在 input_ids 中有唯一 id,定位天然准确。

**练习 2**:如果模型生成的回答里出现三个 `<p>…</p>`,训练分支会提取几行查询?多余的 `<p>` 如何处理?
**答案**:提取前 2 行(`qa_queries[:2]`),第三个 `<p>…</p>` 直接丢弃。这与数据侧 GT 构造「`<p>` 个数封顶 2」的契约一致,保证查询行与 `sp_gt_seg` 的 GT 行按行序对齐。

**练习 3**:`get_query_tokens_id` 中 `used_end_positions` 集合的作用是什么?
**答案**:保证每个 `</p>` 终点只被配对一次。扫描按 `<p>` 起点顺序进行,每个起点贪心取「其后第一个未占用的 `</p>`」,避免同一个 `</p>` 被两个 `<p>` 重复使用导致区间重叠。

### 4.3 推理版提取:`get_query_tokens_eval`

#### 4.3.1 概念说明

推理(u4-l5 详讲)时没有 GT 回复可喂,回答由 `generate` 逐 token 生成;提取查询用的是**只含生成段的序列**——`input_ids` 里只有模型自己写出的答案文本。此时不存在「多条 QA 拼在一条序列里」的问题(多轮对话也是一轮一轮生成、逐轮提取),所以**不需要解析多轮结构**,全局搜 `<p>…</p>` 即可。这就是 `get_query_tokens_eval` 的简化前提。

#### 4.3.2 核心流程

```text
输入:hidden_states (1, G, D_h)  只含生成段(G = 生成 token 数)
     input_ids    (1, G)        生成段的 id
1. 定位所有 <p>、</p>
2. 顺序配对(每个 </p> 只用一次),区间均值池化 → 查询行
3. 有查询 → 取前 2 行;一个都没有 → 1 行零向量
输出:(查询数, D_h)
```

与训练版的差异只有两点:无区间限定(整段都是回答)、无 `num_qa` 对齐义务(每次调用只服务当前这一轮回答)。

#### 4.3.3 源码精读

[uniseg3d/query_decoder.py:1268-1322](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L1268-L1322),docstring 明确说明了它的定位——"For inference/evaluation, the input contains only the generated answer (without prompt). No need to parse multi-turn dialogue":

```python
for p_start in p_start_positions:
    for p_end in p_end_positions:
        if p_end > p_start and p_end not in used_end_positions:
            used_end_positions.add(p_end)
            if p_end > p_start + 1:
                between_embeddings = hidden_flat[p_start + 1 : p_end]
                avg_embedding = between_embeddings.mean(dim=0, keepdim=True)
                qa_queries.append(avg_embedding)
            break

all_query_tokens = []
if len(qa_queries) > 0:
    all_query_tokens.append(torch.cat(qa_queries[:2], dim=0))   # 最多前 2 个
else:
    # no <p>...</p>, return NaN   ← 注释写 NaN,实现是零向量,见 4.4
    all_query_tokens.append(torch.zeros(1, self.reason_mode_config.text_config.hidden_size, device=device))
return torch.cat(all_query_tokens, dim=0)
```

调用点在推理辅助方法 `_get_hidden_states_and_query_token`:

[uniseg3d/query_decoder.py:1512-1531](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L1512-L1531) 对「prompt + 生成段」整条序列做一次二次前向拿隐状态,再切出生成段:

```python
outs2 = self.reason_model(**data, output_hidden_states=True,
                          return_dict=True, use_cache=False)
hidden_states = outs2.hidden_states[-1]

output_hidden_states = hidden_states[:, -output_ids.shape[1]:]
query_token = self.get_query_tokens_eval(output_hidden_states, output_ids, output_text)
```

`hidden_states[:, -output_ids.shape[1]:]` 取尾部「生成段长度」个位置,与 `output_ids` 逐位对齐——这样 `<p>` 在 `output_ids` 里的下标才能直接用来索引 `hidden_states`。(为什么需要「二次前向」而不是直接用 `generate` 过程的隐状态,是 u4-l5 的主题,这里只确认接口。)

#### 4.3.4 代码实践

**实践:单轮回答的提取行为对照(源码阅读 + 手推)。**

1. 实践目标:确认推理版「整段全局搜索、无需切轮」的行为边界。
2. 操作步骤:
   - 阅读 [uniseg3d/query_decoder.py:1299-1310](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L1299-L1310) 的配对循环,注意它不像训练版那样有 `p_end <= assistant_end` 的上界,唯一的约束是 `p_end > p_start` 且未被占用。
   - 手推两个生成段:① `<p>chair</p><SEG> and <p>desk</p><SEG> and <p>lamp</p><SEG>`;② `I cannot find it.`
3. 需要观察的现象:① 产出 3 个查询但只保留前 2 行;② 产出 1 行零向量。
4. 预期结果:① `(2, D_h)`,lamp 的区间被丢弃;② `(1, D_h)` 全零。若想真机验证,可在跑通 u1-l4 的推理后,在 [uniseg3d/query_decoder.py:1530](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L1530) 前打印 `output_ids.shape` 与 `(output_ids[0] == p_start_id).sum()` 观察命中数(待本地验证)。

#### 4.3.5 小练习与答案

**练习 1**:为什么 `get_query_tokens_eval` 不需要 `<|im_start|>`/`<|im_end|>` 定位?
**答案**:它的输入只有生成段(`output_ids`),整段都属于同一条回答,不存在把多条 QA 区分开的需求;直接全局配对 `<p>…</p>` 即可。训练版输入是「prompt + 多条 GT 回复」拼成的长序列,必须先切轮。

**练习 2**:推理时模型「没有按要求输出 `<p>` 标记」会发生什么?
**答案**:`get_query_tokens_eval` 返回 1 行零向量;分割解码器仍会跑完这轮查询,但其 mask 会在 [uniseg3d/query_decoder.py:1932](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L1932) 被替换为常数 `1e-4`(见 4.4.3),得到一个确定的「无效 mask」,不会产生随机结果。

### 4.4 `query_proj` 投影与无效查询的全链路兜底

#### 4.4.1 概念说明

提取出的查询行生活在语言塔的隐空间,维度是 \(D_h\)=`text_config.hidden_size`(Qwen3-VL-4B 约 2560,**待本地验证**);而分割解码器工作在 `d_model=256`(配置见 [configs/ground3dlmm_train.py:56](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/configs/ground3dlmm_train.py#L56))。`query_proj` 就是把 \( \mathbb{R}^{D_h} \to \mathbb{R}^{256} \) 的投影桥(u4-l2 从初始化视角讲过它,本讲从数据流视角收尾)。

「兜底」指两个问题:① 无效查询行(零向量)如何保证不污染损失与评估;② 一个容易被忽略的数学事实——**零向量经过带偏置的投影层后不再是零**,所以「识别无效行」必须在投影前的原始隐空间做。这解释了代码里 `original_queries` 的存在。

#### 4.4.2 核心流程

```text
训练分支 forward_iter_pred(training=True):
  queries = infer_reason_train 返回的 query_list        # 每场景 (总查询数, D_h),含零向量行
  original_queries = [q.clone() for q in queries]       # ★ 投影前留档
  queries = self._get_queries(queries, len(x))          # query_proj: D_h → 256
  ... 6 层 Cross-Attn/Self-Attn/FFN 解码 ...
  返回 dict(original_queries=..., masks=..., language_losses=...)
损失侧 get_layer_loss(aux_outputs, insts, origin_queries):
  有效行 = (origin_query.sum(dim=1) != 0) ∧ (GT 行非 NaN)
  只对有效行算 BCE + Dice
推理分支 forward_iter_pred(training=False):
  对每轮查询:零向量行(query.abs().sum(-1) == 0)的 pred_mask 替换为常数 1e-4
```

#### 4.4.3 源码精读

**(1)`query_proj` 的结构。**

[uniseg3d/query_decoder.py:944-947](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L944-L947):

```python
self.query_proj = nn.Sequential(
        Qwen3VLTextRMSNorm(self.reason_mode_config.text_config.hidden_size),
        nn.Linear(self.reason_mode_config.text_config.hidden_size, d_model), nn.GELU(),
        nn.Linear(d_model, d_model))
```

RMSNorm 预归一化 → 线性降维 \( D_h \to 256 \) → GELU → 256→256 精调。它**无条件重建**,覆盖了父类 `QueryDecoder` 在 `num_instance_queries == 0` 时创建的旧 `32→256` 版本([uniseg3d/query_decoder.py:484-487](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L484-L487)),因为父类版本假设输入是 3D 特征(in_channels=32),而这里的输入是语言隐状态。

**(2)`_get_queries`:投影发生的地方,以及 `original_queries` 留档。**

[uniseg3d/query_decoder.py:513-536](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L513-L536):

```python
result_queries = []
for i in range(batch_size):
    result_query = []
    if hasattr(self, 'query'):                 # 本配置下不存在(num_*_queries=0)
        result_query.append(self.query.weight)
    if queries is not None:
        result_query.append(self.query_proj(queries[i]))   # D_h → 256 在这里发生
    result_queries.append(torch.cat(result_query))
return result_queries
```

训练分支的调用与留档见 [uniseg3d/query_decoder.py:1874-1882](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L1874-L1882):先 `original_queries = [q.clone() for q in queries]` 保住**投影前**的张量,再 `_get_queries` 投影,并顺手为每场景构造「禁止自己注意自己」的 `interaction_masks`(单位阵取反)。返回字典把 `original_queries` 一路带回给损失函数([uniseg3d/query_decoder.py:1905-1910](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L1905-L1910))。

为什么留档必须在投影前?设零查询 \( q = \mathbf{0} \):RMSNorm 无偏置,输出仍为 \(\mathbf{0}\);但 `nn.Linear` 带偏置 \(b\),输出为 \( W\mathbf{0} + b = b \neq \mathbf{0}\)。也就是说,投影后的「零向量行」已经变成了非零常数向量,`sum != 0` 这类检查会**全部失灵**。唯一可靠的哨兵检查点是投影前的原始隐空间。

**(3)损失侧:零向量 ∧ NaN 的双重过滤。**

[uniseg3d/text_prompt_instance_criterion.py:776-821](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/text_prompt_instance_criterion.py#L776-L821) 的 `get_layer_loss` 逐场景过滤:

```python
none_zero_mask_index = (i_origin_query.sum(dim=1) != 0)   # 查询行非零向量(解码器侧哨兵)
none_nan_gt_index = ~torch.isnan(inst).all(dim=1).to(device)  # GT 行非 NaN(数据侧哨兵)
valida_ids = none_zero_mask_index & none_nan_gt_index     # 交集才有效
if valida_ids.sum() == 0:
    continue
pred_mask = mask[valida_ids]
tgt_mask = inst.to(device)[valida_ids]
```

这正是 u2-l4「取不到有效 mask 的 GT 行用 NaN 占位而非删除」的下游:两套哨兵(查询侧零向量、GT 侧 NaN)在损失处汇合,同一布尔掩码同时作用于预测与 GT,保证行序不错位,无效行永不产生梯度。注意 `origin_queries` 形如 `(总查询数, D_h)`,与 `mask` 的行数相同——投影前后行数不变,只有维度变。

**(4)注释与实现的不一致:「NaN」其实大多是零向量;真 NaN 只有一处。**

三个提取方法的兜底注释都写着 "return NaN" / "pad with NaN"(如 [uniseg3d/query_decoder.py:1146-1147](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L1146-L1147)、[uniseg3d/query_decoder.py:1259-1260](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L1259-L1260)、[uniseg3d/query_decoder.py:1319-1320](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L1319-L1320)),但实现是 `torch.zeros(1, hidden_dim)`。真正的 NaN 只出现在 `infer_reason_train` 的空数据早退分支:

[uniseg3d/query_decoder.py:1336-1340](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L1336-L1340),当场景没有任何 QA 时,直接为每个场景造一行 `float('nan')` 查询返回:

```python
if qa_data == []:
    queies = [
        torch.full((1, self.reason_mode_config.text_config.hidden_size), float('nan'),
                   device=point_feats[0].device)
        for _ in range(len(point_feats))]
    return new_point_feat, language_losses, output_logits, queies
```

下游对 NaN 也有防御:`SelfAttentionLayer` 在自注意力前把含 NaN 的查询行替换为 0 再计算([uniseg3d/query_decoder.py:398-402](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L398-L402)),避免 NaN 经 softmax 传染整批注意力输出(残差支路仍加回原值,NaN 信息不丢,交给损失侧过滤):

```python
nan_mask = torch.isnan(y).any(dim=-1)
y_safe = y.clone()
y_safe[nan_mask] = 0.0
z, _ = self.attn(y_safe, y_safe, y_safe, attn_mask=interaction_masks[i])
z = self.dropout(z) + y
```

**(5)推理分支的 `1e-4` 兜底。**

[uniseg3d/query_decoder.py:1929-1932](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L1929-L1932)(每个解码层后都重复一次,另见 [uniseg3d/query_decoder.py:1942](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L1942)):

```python
queries = self._get_queries(queries, 1)
pred_score, pred_mask, attn_mask = self._forward_head(queries, qa_data, mask_feats, last_flag=False)
pred_mask[0][query.abs().sum(-1) == 0] = torch.ones_like(pred_mask[0][query.abs().sum(-1) == 0]) * 1e-4
```

注意判据 `query.abs().sum(-1) == 0` 用的是 `for query in batch_queries` 循环里的**原始查询**(投影前),再次印证 4.4.3 第 (2) 点。对零查询行,把预测 mask(logit)整体替换为常数 `1e-4`:这保证了无效查询的输出是**确定的常数**而非未初始化内存或随机值,后续 mIoU 指标(u6-l1)拿到的无效行结果是稳定可复现的。

#### 4.4.4 代码实践

**实践:验证「零向量经 `query_proj` 后不再为零」——理解 `original_queries` 为什么必须留档。**

1. 实践目标:用最小可运行实验证明 4.4.3 第 (2) 点的数学论证,从而理解 `original_queries` 的设计必然性。
2. 操作步骤:在仓库外新建 `probe_query_proj.py`(示例代码,结构复刻 [uniseg3d/query_decoder.py:944-947](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L944-L947),维度缩小):

   ```python
   # ~/probe/probe_query_proj.py  (示例代码)
   import torch, torch.nn as nn

   D_H, D_MODEL = 16, 8
   class RMSNorm(nn.Module):                      # 简化的 RMSNorm(无偏置)
       def __init__(self, d):
           super().__init__()
           self.weight = nn.Parameter(torch.ones(d))
       def forward(self, x):
           return x / (x.pow(2).mean(-1, keepdim=True) + 1e-6).sqrt() * self.weight

   torch.manual_seed(0)
   query_proj = nn.Sequential(
       RMSNorm(D_H),
       nn.Linear(D_H, D_MODEL), nn.GELU(),
       nn.Linear(D_MODEL, D_MODEL))

   q = torch.zeros(1, D_H)                        # 一行零向量(无效查询哨兵)
   out = query_proj(q)
   print('投影前 sum =', q.sum().item())           # 预期 0.0
   print('投影后     =', out.detach().numpy().round(4))
   print('投影后 sum =', out.sum().item())          # 预期 ≠ 0(等于两层 Linear 偏置的组合)
   print('零检查失灵?', bool((out.sum(-1) != 0).all()))
   ```

   运行:`python ~/probe/probe_query_proj.py`。
3. 需要观察的现象:投影前 `sum == 0`;投影后向量非零(由 `nn.Linear` 的偏置项决定,与输入无关)。
4. 预期结果:打印 `零检查失灵? True`。这说明若在投影后的查询上做 `sum != 0` 过滤,零向量哨兵会全部漏网——因此损失侧([uniseg3d/text_prompt_instance_criterion.py:798](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/text_prompt_instance_criterion.py#L798))与推理侧([uniseg3d/query_decoder.py:1932](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L1932))都坚持在**投影前**空间做零检查。(本讲义撰写环境无法运行 Python,具体数值**待本地验证**;结论由 `Linear(0)=bias` 的定义保证。)

#### 4.4.5 小练习与答案

**练习 1**:把 `original_queries` 换成投影后的 `queries` 传给损失函数,会发生什么?
**答案**:零向量行经 `query_proj` 变成非零常数行,`(queries.sum(dim=1) != 0)` 全为 True,无效查询被当成有效查询参与 BCE/Dice,与「全零/NaN 的 GT 行」求交后虽可能仍被 GT 侧 NaN 过滤掉一部分,但对「GT 有效而查询无效」的行(如模型漏答 `<p>`)会引入完全错误的监督来源——总之哨兵机制失灵,训练信号被污染。

**练习 2**:无效查询在训练与推理两处的最终归宿分别是什么?
**答案**:训练:损失侧 `get_layer_loss` 用 `origin_queries.sum != 0 ∧ GT 非 NaN` 过滤,无效行不产生梯度;推理:零查询行的 `pred_mask` 被替换为常数 `1e-4`,产生确定的无效 mask 进入指标计算,不污染其它 QA 的结果。

**练习 3**:`SelfAttentionLayer` 为什么把 NaN 行替换为 0 之后还要 `+ y`(加回原值)?
**答案**:替换只为让 `nn.MultiheadAttention` 的 softmax 不被 NaN 传染(NaN 进入注意力权重会让整批输出变 NaN);残差支路 `z + y` 保留原始 NaN 信息,使损失侧仍能用 `isnan` 识别并跳过该行。防御与哨兵两不误。

## 5. 综合实践

**任务:端到端手推一条「两 QA 样本」的查询链路,并核对损失侧行对齐。**

设定某场景 `num_qa=2`:QA1 的 GT 回答含两个 `<p>`(如 `…<p>seat</p><SEG>… <p>back</p><SEG>…`),其中第二个 `<p>` 对应的 GT mask 在数据侧因实例过小被置为 NaN 行(u2-l4);QA2 是纯文本数值回答,无 `<p>`。请依次完成:

1. **提取**:写出 `get_query_tokens_id` 的输出形状与每一行的语义(提示:`(3, D_h)`:行 1 = seat 区间均值,行 2 = back 区间均值,行 3 = 零向量)。
2. **对齐**:写出数据侧 `sp_gt_seg` 的形状(提示:`(3, 超点数)`,行序与查询行一致)。
3. **过滤**:套用 [uniseg3d/text_prompt_instance_criterion.py:798-804](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/text_prompt_instance_criterion.py#L798-L804) 的双重过滤,指出哪些行参与损失(提示:仅行 1——行 2 被 GT 的 NaN 过滤,行 3 被零向量过滤)。
4. **验证**(可选,待本地验证):把 4.2.4 的脚本扩展成 `num_qa=2`、两个 assistant 区间的版本,并在 `hidden` 中给 seat/back 区间埋不同常量,核对输出的行序与数值;再用 4.4.4 的 `query_proj` 复刻版接在后面,确认投影后行数仍为 3、零行变为非零常数。

完成本任务后,你就把 u2-l4(GT 形状契约)→ u4-l3(训练序列构造)→ 本讲(查询提取与兜底)→ u5-l2(损失过滤)四讲的数据流真正串成了一条线。

## 6. 本讲小结

- 三个提取方法共享同一内核:在 assistant 回复范围内配对 `<p>…</p>`,取**区间内隐状态的行均值**为查询;**并不使用 `<SEG>` token 本身的隐状态**——「`<SEG>` 查询」只是沿袭的口语称呼。
- 训练用 `get_query_tokens_id`(id 坐标系定位 `<|im_start|>assistant … <|im_end|>` 区间),取代了文本坐标系的 `get_query_tokens`,根因是 Joint 变体的图像条目被 processor 展开后文本计数与 `input_ids` 错位;推理用 `get_query_tokens_eval`(输入只有生成段,全局搜索、无需切轮)。
- 每条 QA 最多 2 个查询(`qa_queries[:2]`),与数据侧「`<p>` 个数封顶 2」的 GT 形状契约逐行对齐;查询行与 `sp_gt_seg` 行序一致,是语言侧与分割侧的对齐纽带。
- `query_proj`(`RMSNorm → Linear(D_h→256) → GELU → Linear(256→256)`)在 `_get_queries` 中把隐状态投到解码器 256 维空间;因 Linear 带偏置,**零向量哨兵的识别必须在投影前的 `original_queries` 上做**,这是训练损失与推理 `1e-4` 兜底共同的细节。
- 无效查询的双重哨兵:解码器侧零向量(`sum != 0` 检查)+ 数据侧 NaN(`isnan` 检查),在 `get_layer_loss` 处求交过滤;注释里的 "NaN" 多为历史遗留,实现是 `torch.zeros`,真 NaN 仅存在于 `qa_data == []` 的早退分支。
- 训练与推理的本质差异:训练靠 teacher forcing 从「模型读到正确回答」的隐状态提查询;推理的回答由 `generate` 生成、再经一次二次前向取隐状态(细节在 u4-l5)。

## 7. 下一步学习建议

下一讲 **u4-l5《推理分支 infer_reason_eval:生成回答并提取查询》**将补全语言侧的最后一块: `_generate_response` 的贪心解码与重复惩罚、为什么要做「一次 generate + 一次二次前向」、单轮与多轮 QA 的处理差异、以及预测 JSON 的写盘。阅读时请带着本讲的问题:推理版提取的输入 `output_ids` 与隐状态切片 `hidden_states[:, -output_ids.shape[1]:]` 是如何在 `_get_hidden_states_and_query_token` 中被逐位对齐的。

之后进入单元 5:u5-l1 讲本讲产出的查询如何进入 6 层解码器(Cross/Self Attention 与 masked attention),u5-l2 讲语言损失与分割损失如何加权合并——本讲的 `original_queries` 过滤正是 u5-l2 的入口。
