# u4-l3 训练分支 infer_reason_train：构造多轮对话与标签

## 1. 本讲目标

学完本讲，你应该能够：

1. 复述训练时语言分支的完整数据流：`selected_qa_data` → `qa_pairs` → `messages` → `apply_chat_template` 产出 ChatML 文本 → processor 编码为 `input_ids`。
2. 解释 `point_mask = input_ids == point_token_id` 这行代码如何把「文本里的占位 token」与「超点特征行」对齐，以及这条数量契约为什么必须成立。
3. 逐行读懂 `preprocess_chatml`：它如何按 ChatML 轮次切分文本、把 system/user/角色头全部置为 `IGNORE_INDEX(-100)`，使语言损失**只计算在 assistant 回复上**。
4. 说明当场景超点数超过 `max_point_tokens` 时，FPS 聚合器如何把送入 LMM 的点 token 数压到预算内，以及它为什么不影响分割解码器的 K/V。

本讲只聚焦**训练**语言分支（`infer_reason_train`）。推理分支（`infer_reason_eval`）在 u4-l5 讲，`<SEG>` 查询提取的完整细节在 u4-l4 讲。

## 2. 前置知识

### 2.1 已经学过的内容（本讲的起点）

- **u2-l4 / u2-l5**：数据管线的 `QA_Generation_v2` 会把三层嵌套 QA 展平、随机采样 `num_qa` 条，产出 `selected_qa_data`（逐样本的 QA 列表）；`Pack3DDetInputs_Part_Glamm` 把它作为前向输入打包，训练时经 `batch_inputs_dict['selected_qa_data']` 进入 `loss()`。
- **u3-l3**：3D 主干把几十万点压缩为逐超点特征（`mask_feats`，256 维），并可在训练时提供超点质心 `point_positions`。
- **u4-l1**：`PointQwen3VL` 通过 forward pre-hook（`add_point_embedding`）把点特征 `masked_scatter` 到 `<|point|>` token 位置；底座冻结，靠 hook 内 `requires_grad = True` 保住梯度。
- **u4-l2**：`Grounded_Decoder_Joint.__init__` 构建了 `point2token`（256 维 → LLM hidden_size）投影和两个 FPS 聚合器。

### 2.2 本讲的新概念

| 术语 | 通俗解释 |
|---|---|
| ChatML | Qwen 系列模型使用的对话文本格式，每条消息形如 `<\|im_start\|>role\n内容<\|im_end\|>`，角色交替出现 |
| teacher forcing | 训练时把**标准答案（GT 回答）**直接拼进输入序列，让模型逐 token 学习「在看到问题时下一个 token 应该是什么」，而不是让它自己生成 |
| labels / IGNORE_INDEX | 因果语言模型的监督张量，形状与 `input_ids` 相同；不想计算损失的位置填 `-100`，PyTorch 的交叉熵会自动忽略 |
| 因果语言模型损失 | 对每个位置 \(i\)，用前缀 \(x_{<i}\) 预测 \(x_i\) 的交叉熵 |
| apply_chat_template | transformers 提供的方法，把 `[{role, content}, ...]` 结构化消息渲染成模型特定的 ChatML 字符串；`add_generation_prompt=True` 会在结尾追加一个「assistant 起始头」提示模型开始回答 |
| 最远点采样（FPS） | 每次选距离已选集合最远的点，最终得到空间上尽量均匀分布的子集 |

语言损失的数学形式（忽略 `-100` 位置）：

\[
\mathcal{L}_{\text{lang}} = -\frac{1}{|\{i:\, l_i \neq -100\}|}\sum_{i:\, l_i \neq -100} \log p_\theta\left(x_i \mid x_{<i}\right)
\]

本讲要回答的核心问题就是：**这份 `labels` 是怎么造出来的，为什么模型只学「回答」而不学「提问」**。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [uniseg3d/query_decoder.py](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py) | 本讲主角：`infer_reason_train`、`preprocess_chatml`、两个 FPS 聚合器、`forward_iter_pred` 训练分支 |
| [uniseg3d/transforms_3d.py](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/transforms_3d.py) | `QA_Generation_v2` 产出 `selected_qa_data` 的数据结构（复盘） |
| [uniseg3d/prompt_template.py](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/prompt_template.py) | 指令模板 `PART_GLAMM_INSTRUCTION_IMG` 与特殊 token 常量 |
| [uniseg3d/conversation.py](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/conversation.py) | `get_conv('chatml')`：ChatML 的 roles/seps/system 定义，`preprocess_chatml` 靠它切轮次 |
| [uniseg3d/point_qwen3_vl.py](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/point_qwen3_vl.py) | `add_point_embedding` 钩子（复盘 u4-l1，重点看数量校验） |
| [uniseg3d/uniseg3d.py](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/uniseg3d.py) | `Grounded_LMM_main.loss()`：调用解码器、把 `language_losses` 交给 criterion |
| [configs/ground3dlmm_train.py](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/configs/ground3dlmm_train.py) | `max_point_tokens=2000`、`num_qa=10` 等训练参数 |

先给出全链路鸟瞰图（本讲四个模块在图中的位置）：

```text
batch_inputs_dict['selected_qa_data']          (u2-l5 打包，每样本一个 list)
        │
        ▼  ── 模块 1：多轮 messages 构造 ──────────────────────
qa_pairs = [(question, gt_answer), ...]        展平（单轮 dict / 多轮 list 统一处理）
messages = [user(图像+指令+n 个 <|point|>), assistant(GT), user, assistant, ...]
        │
        ▼  ── 模块 2：processor 编码与 point_embeds 附加 ───────
text = apply_chat_template(messages, add_generation_prompt=True)
data = processor(text, images)                  # input_ids / attention_mask / pixel_values
data['point_embeds'] = point2token(超点特征)     # (N, hidden_size)
data['point_mask']   = input_ids == point_token_id
        │
        ▼  ── 模块 3：preprocess_chatml 标签构造 ───────────────
labels = preprocess_chatml(input_ids, text, tokenizer)   # 仅 assistant 回复保留
output = reason_model(**data, labels=labels)     # lang_loss 只落在 assistant 段
        │
        ▼  ── （u4-l4 详讲）<SEG> 查询提取 ────────────────────
query_tokens = get_query_tokens_id(hidden_states, input_ids, num_qa)
        │
        ▼
forward_iter_pred 训练分支 → query_proj → 分割解码器 → 与 language_losses 一起进 criterion
```

其中**模块 4（FPS 聚合降采样）**发生在模块 1 之前：如果超点数超过预算，先把特征降采样，再决定 `<|point|>` 重复多少次。

## 4. 核心概念与源码讲解

### 4.1 多轮 messages 构造

#### 4.1.1 概念说明

训练语言分支的输入不是原始 QA JSON，而是 u2-l4 产出的 `selected_qa_data`：每个样本（场景帧）一个列表，列表里的元素有两种形态：

- **dict**：一条单轮 QA（来自 `part_qa_data` / `object_qa_data` 层），含 `question` / `answer` / `qa_keys` / `qa_type` 等键；
- **list[dict]**：一整段多轮对话（来自 `multi_turn_qa_data` 层），每个 dict 是一轮问答。

`infer_reason_train` 的第一件事是把这两种形态**统一展平**成 `qa_pairs = [(question, gt_answer), ...]`，然后按 ChatML 的 `user → assistant → user → assistant…` 顺序拼成 `messages`。注意一个关键差异：**多轮对话在数据侧是一整个 list 元素，在这里被拆成连续的多条 message**——也就是说，同一段多轮对话的上下文被完整保留在同一个序列里；而若干条独立的单轮 QA 也会被拼进同一个 `messages`（每条单轮 QA 变成一对 user/assistant 消息）。训练时一次前向看到的是这个场景采到的全部 QA（`num_qa=10` 条）串成的长对话。

#### 4.1.2 核心流程

```text
对 batch 中每个场景 i：
  1. 从 scene_names[i] 拆出 scene_name / frame_id，
     按场景名是否含 'scene' 选择 scannet 或 scannetpp 的图像目录，
     拼出 image_path（Joint 训练用）
  2. （若 self.training 且超点数超预算）先做 FPS 聚合 —— 见 4.4
  3. point_embeds = point2token(超点特征)   # (n_points, hidden_size)
  4. 展平 qa_pairs：
       dict     → 追加 (question, answer)
       list     → 逐轮追加 (question, answer)
  5. 组装 messages：
       第 0 轮 user：image 条目 + 文本（指令模板，{point} 替换为 n_points 个 <|point|>）
       其余轮 user：纯文本 question
       每轮之后紧跟 assistant：content = gt_answer（GT 文本，teacher forcing）
  6. text = processor.apply_chat_template(messages, add_generation_prompt=True)
```

#### 4.1.3 源码精读

入口与场景循环。`infer_reason_train` 先处理空 QA 兜底，再逐场景取特征、拆场景名、拼图像路径：

- [uniseg3d/query_decoder.py:1336-1340](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L1336-L1340) —— 若 `qa_data` 为空列表，语言分支直接跳过：语言损失为空，查询用全 NaN 张量占位（与 u2-l4 建立的「NaN 契约」一致，下游识别 NaN 并跳过）。注意源码里返回变量名拼写为 `queies`，引用时请保持原样。
- [uniseg3d/query_decoder.py:1343-1358](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L1343-L1358) —— 逐场景取 `i_point_feat`；用 `i_scene_name.split('_frame_')` 把形如 `scene0000_00_frame_0001` 的帧名拆成场景名与帧号；`'scene' in scene_name` 为真走 ScanNet 图像目录，否则走 ScanNet++ 目录（两类数据集场景命名约定不同），拼出 `image_path`。

展平 `qa_pairs`：

- [uniseg3d/query_decoder.py:1373-1390](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L1373-L1390) —— 内嵌的 `get_text` 兜底处理 `question`/`answer` 是 list 的情况（取首个元素）；随后 dict 形态追加一对，list 形态（多轮）逐轮 extend。这里**丢弃了** `qa_keys`/`qa_type` 等元信息——训练分支不需要任务标签，只有评估分支（u4-l5）用它们组织输出 JSON。

组装 `messages`（本模块最核心的一段）：

- [uniseg3d/query_decoder.py:1392-1419](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L1392-L1419) —— 第 0 轮 user 消息带两个 content 条目：`{'type': 'image', 'image': image_path, 'max_pixels': 448*448, 'min_pixels': 224*224}` 和 `{'type': 'text', 'text': user_text}`；其余轮只有纯文本。`user_text` 来自指令模板：

```python
user_text = PART_GLAMM_INSTRUCTION_IMG.format(
    question=question, point=self.point_token * n_points)
```

  模板见 [uniseg3d/prompt_template.py:46-52](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/prompt_template.py#L46-L52)：

```python
PART_GLAMM_INSTRUCTION_IMG = '''
You are an AI visual assistant analyzing a 3D point-cloud scene. ...
The point-cloud data is {point}. Don't change the order of the objects or parts, follow the order in the question.
The question is: {question}.
'''
```

  其中 `{point}` 被 `self.point_token * n_points` 替换——即字符串 `'<|point|>'` 重复 n_points 次（常量定义在 [uniseg3d/prompt_template.py:2-4](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/prompt_template.py#L2-L4)：`POINT_TOKEN = ["<|point|>"]`，`SEG_TOKEN = ["<p>", "</p>", "<SEG>", "<name>", "<sentence>"]`）。**这一行就是「点云进入对话」的全部机关**：文本里预埋 n_points 个占位 token，等待 4.2 节的特征覆盖。

  assistant 消息的 `content` 直接是 **GT 回答字符串**（含 `<p>物体名</p><SEG>` 接地标记），这就是 teacher forcing。注意 assistant content 用的是裸字符串而非 content 条目列表——processor 的 chat template 两种都接受。

渲染为 ChatML 文本：

- [uniseg3d/query_decoder.py:1420](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L1420) —— `text = self.processor.apply_chat_template(messages, add_generation_prompt=True)`。`add_generation_prompt=True` 会在结尾追加 assistant 起始头（如 `<|im_start|>assistant\n`），这个头**永远不被监督**（4.3 会看到专门的处理）。

#### 4.1.4 代码实践

**实践目标**：不动手跑模型，纯阅读+手工推导一次 messages 结构，把「数据侧结构 → 对话结构」的映射画出来。

**操作步骤**：

1. 阅读 [uniseg3d/transforms_3d.py:427-472](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/transforms_3d.py#L427-L472) 的 `flatten_qa_data`，确认 `part_qa_data`/`object_qa_data` 的条目被打上 `qa_keys='part'/'object'`、多轮被打上 `qa_keys='multi_turn'`（[uniseg3d/transforms_3d.py:432-464](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/transforms_3d.py#L432-L464)）。
2. 假设某帧采到 `selected_qa_data = [ {单轮part QA}, [多轮第1问, 多轮第2问], {单轮object QA} ]`（示意结构），在纸上写出 [uniseg3d/query_decoder.py:1392-1419](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L1392-L1419) 循环产出的 messages（共 4 对 user/assistant，注意只有第 0 轮带图像与 `<|point|>` 占位）。
3. 核对 [uniseg3d/transforms_3d.py:540-541](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/transforms_3d.py#L540-L541)：`sp_gt_seg` 与 `selected_qa_data` 是**同索引对应**的列表——第 k 条 QA 的分割 GT 就是 `sp_gt_seg[k]`。这解释了为什么训练分支丢掉元信息也不会乱：分割侧的对齐靠列表顺序。

**需要观察的现象**：messages 中 assistant 的回答顺序、`<p>` 标签个数与 `sp_gt_seg[k]` 行数（由 [uniseg3d/transforms_3d.py:406-414](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/transforms_3d.py#L406-L414) 的 `count_p_tags` 封顶 2）一一对应。

**预期结果**：能画出 8 条消息的对话图，并说出每条 assistant 消息对应哪一段超点级 GT。本实践为源码阅读型，无需运行。

#### 4.1.5 小练习与答案

**练习 1**：为什么第 0 轮之外的 user 消息不再重复 `<|point|>` 占位与图像？

**答案**：ChatML 对话是同一个序列，第 0 轮注入的点 token 与视觉 token 通过注意力对后续所有轮可见；重复注入只会浪费序列长度。这也和推理侧多轮处理一致（u4-l5 的 `_process_multi_turn_qa` 同样只在 turn 0 注入）。

**练习 2**：两条独立的单轮 QA 被拼进同一个 `messages`，模型会不会把第二条的问题当成第一条的上下文？

**答案**：会「看到」但不能说是错误设计——这就是把一批 QA 组织成多轮对话的训练方式；模型学到的是「针对最新一条 user 消息作答」。由于监督只落在 assistant 段（4.3），损失并不会强迫模型复述前面的对话。数据侧 `num_qa=10`（[configs/ground3dlmm_train.py:138-139](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/configs/ground3dlmm_train.py#L138-L139)）限制了单序列长度。

**练习 3**：`qa_data == []` 的兜底分支（1336-1340 行）返回的查询为什么用 NaN 而不是零向量？

**答案**：NaN 是 u2-l4 建立的全局「无效 GT」哨兵值——下游损失与指标侧显式识别 NaN 行并跳过；零向量是合法数值，无法与「真实预测出全零」区分（对比：`get_query_tokens_id` 对「该轮无 `<p>`」用零向量占位，因为那是可跳过的空查询而非缺失 GT，见 u4-l4）。

### 4.2 processor 编码与 point_embeds 附加

#### 4.2.1 概念说明

有了 `messages`，下一步是把它变成模型真正的输入张量。这段代码做了三件事：

1. `process_vision_info(messages)` 从 messages 里抽出图像条目并按 `max_pixels/min_pixels` 约束读取、缩放；
2. `processor(text=[text], images=images, ...)` 把 ChatML 文本分词成 `input_ids`、把图像变成 `pixel_values`（Qwen 视觉侧的图像占位 token 会在这一步被 processor 展开为若干视觉 token）；
3. 往 `data` 里**手工塞进两个额外键**：`point_embeds`（投影后的超点特征）与 `point_mask`（布尔掩码）。这两个键不是 processor 产出的，是本项目为点云注入扩展的接口，最终由 u4-l1 讲过的 forward pre-hook 消费。

`point_mask = input_ids == point_token_id` 是本讲的第二核心：**用 token id 相等性在序列里定位所有 `<|point|>` 占位**。因为指令模板里 `<|point|>` 恰好重复了 n_points 次、且它在 u4-l2 的 `_init_reason_model` 里已注册为单 token（id 写回 `config.point_token_id`），所以 `point_mask.sum() == n_points == point_embeds.shape[0]` 必须严格成立——钩子里显式校验这一点，失配立即报错。

#### 4.2.2 核心流程

```text
text ──processor──> input_ids (1, L)          # L 含 n_points 个 <|point|> 占位 + 展开的视觉 token
images ──────────> pixel_values

point_embeds = point2token(i_point_feat)      # (n_points, hidden_size)
point_mask   = (input_ids == point_token_id)  # (1, L) bool，True 共 n_points 个
                 │
                 ▼  reason_model.forward 时
add_point_embedding 钩子（u4-l1）：
   assert point_mask.sum() == point_embeds.shape[0]
   inputs_embeds.masked_scatter(mask_expanded, point_embeds)
```

#### 4.2.3 源码精读

- [uniseg3d/query_decoder.py:1370](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L1370) —— `point_embeds = self.point2token(i_point_feat).to(self.reason_model.device)`：把 256 维超点特征投影到 LLM hidden_size 并搬到模型设备。`point2token` 的结构在 u4-l2 已拆解（RMSNorm → Linear → GELU → Linear）。
- [uniseg3d/query_decoder.py:1420-1422](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L1420-L1422) —— 三连：chat template 渲染、`process_vision_info`（来自 `qwen_vl_utils`，返回图像与视频 kwargs）、processor 编码，得到 `BatchFeature` 类型的 `data`。
- [uniseg3d/query_decoder.py:1423-1425](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L1423-L1425) —— 附加两个键：

```python
data['point_embeds'] = point_embeds
data['point_mask'] = data['input_ids'] == self.point_token_id
```

  钩子侧的对应消费逻辑（复盘 u4-l1，本讲只需关注数量契约）：

- [uniseg3d/point_qwen3_vl.py:21-35](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/point_qwen3_vl.py#L21-L35) —— `add_point_embedding` 从 kwargs 中取出四个键；若 `point_mask.sum() != point_embeds.shape[0]` 立即 `ValueError`；否则把 `point_embeds` 按 mask `masked_scatter` 进 `inputs_embeds`。**只有预填充步（`inputs_embeds.shape[1] > 1`）注入**，逐 token 生成时不注入。
- [uniseg3d/point_qwen3_vl.py:43-44](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/point_qwen3_vl.py#L43-L44) —— 训练态强制 `inputs_embeds.requires_grad = True`：底座 embed 层被冻结后，这是梯度回到 `point2token` 的生命线。

一个容易忽略的顺序细节：**FPS 聚合（1360-1368 行）发生在 `point_embeds` 计算（1370 行）之前**，因此 `n_points` 已是聚合后的 token 数；随后 1399 行 `self.point_token * n_points` 用的也是同一个更新过的 `n_points`。两处引用同一个变量，契约天然成立——若把聚合挪到投影之后而不更新 `n_points`，钩子校验就会当场报错。

#### 4.2.4 代码实践

**实践目标**：验证「`<|point|>` 重复次数 == `point_mask.sum()` == 特征行数」这条数量契约，不加载 4B 权重，只用 tokenizer。

**操作步骤**（示例代码，非项目自带脚本）：

1. 确认环境已按 u1-l2 装好 transformers；
2. 在仓库根目录新建临时脚本（可放在 `/tmp`，不要写进仓库）：

```python
# 示例代码：check_point_mask.py —— 仅用于理解数量契约
from transformers import AutoTokenizer

tok = AutoTokenizer.from_pretrained('Qwen/Qwen3-VL-4B-Instruct')  # 只下载 tokenizer 文件
n_points = 7  # 随便一个小的数，模拟聚合后的超点数

# 模拟 _init_reason_model 的注册流程（u4-l2）
new = tok.add_special_tokens(dict(additional_special_tokens=['<|point|>']))
point_token_id = tok.convert_tokens_to_ids('<|point|>')
print('new tokens:', new, 'point_token_id:', point_token_id)

# 模拟模板替换（这里只保留占位部分）
text = 'The point-cloud data is ' + '<|point|>' * n_points + '. The question is: ...'
input_ids = tok(text, return_tensors='pt')['input_ids']          # (1, L)
point_mask = input_ids == point_token_id                          # 对应 query_decoder.py:1425
print('n_points =', n_points, '| point_mask.sum() =', int(point_mask.sum()))
assert point_mask.sum() == n_points
```

3. 运行 `python check_point_mask.py`。

**需要观察的现象**：`point_mask.sum()` 恰等于 `n_points`；`point_token_id` 是一个大于原始词表大小的 id（新注册 token 追加在词表尾部）。

**预期结果**：断言通过，证明「文本重复 n 次 → n 个占位 token → mask 计数一致」。若把 `add_special_tokens` 那行注释掉，`convert_tokens_to_ids` 会返回 unknown id，契约破裂——这正是钩子校验存在的意义。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `point_mask` 要在 processor 编码之后、由 `input_ids` 重算，而不是在构造 messages 时手工记录位置？

**答案**：processor 会改写序列（图像占位展开、可能的文本规范化），手工记录的位置索引在编码后不可靠；用「token id 相等」做定位与位置无关，天然免疫前面内容的长度变化。

**练习 2**：钩子里 `inputs_embeds.shape[1] > 1` 这个条件排除了什么情况？

**答案**：排除逐 token 生成步（序列长度为 1、走 KV cache 的增量解码）。生成时 `<|point|>` 已在预填充步被替换为点特征并写入 cache，不需要也不应该重复注入（u4-l1 详述）。

**练习 3**：图像的 `max_pixels=448*448` 约束（1405 行）对语言分支意味着什么？

**答案**：限制单帧图像被展开成的视觉 token 数量，控制序列长度与显存。点 token 侧的同类预算机制是 `max_point_tokens`（4.4 节）——两条预算共同决定一次前向的序列规模。

### 4.3 preprocess_chatml：标签构造与语言损失

#### 4.3.1 概念说明

现在模型有了输入（`data`）和标准答案（messages 里的 GT 回复），还缺一份 `labels`：形状与 `input_ids` 相同、告诉损失函数「每个位置要不要算损失」的张量。`preprocess_chatml` 是一个从 LLaVA 系训练代码演化来的 ChatML 标签构造函数，它的原则只有一句话：

> **只有 assistant 回复正文参与损失；system 段、user 提问、角色头（`<|im_start|>assistant\n`）、结尾的生成提示，全部置为 `IGNORE_INDEX = -100`。**

直觉上：我们不想让模型学着复述用户的问题（那会浪费容量在错误目标上），只想让它学「给定问题，如何作答、如何停」。

#### 4.3.2 核心流程

ChatML 文本（`qwen_model='qwen3_vl'` 时无 system 段）形如：

```text
\n<|im_start|>user\n{指令+问题}<|im_end|>
\n<|im_start|>assistant\n{GT回答}<|im_end|>
\n<|im_start|>user\n{问题2}<|im_end|>
\n<|im_start|>assistant\n{GT回答2}<|im_end|>
\n<|im_start|>assistant\n          ← 生成提示（add_generation_prompt=True 的产物）
```

`preprocess_chatml` 的算法：

```text
1. 按 sep='<|im_end|>' 把 text 切成 rounds（每段补回 sep）
   → 每段 = 一条完整消息；最后一段是生成提示，单独取出（last_invalide_text）并从监督轮次中剔除
2. 分组（qwen3_vl 无 system）：每两段一组 = (user 轮, assistant 轮)
   分组（qwen2_5_vl 有 system）：前三段一组（system+user+assistant），之后每两段一组
3. labels = input_ids.clone()，逐组推进游标 cur_len：
     组内分隔符 sep2 = '<|im_end|>' + '\n<|im_start|>assistant\n'
     ins_len = 分词(组头至 assistant 头) 的长度
     labels[cur_len : cur_len+ins_len] = -100        # 屏蔽提问与 assistant 头
     cur_len += 分词(整组) 的长度                     # 游标跳到下一组
4. 兜底：若 labels 全为 -100 则报错
5. 末尾生成提示：labels[-(len(last_invalide_text)) + 1 :] = -100
```

第 3 步的关键是**用「对子字符串单独分词的长度」作为游标步长**——它假设「整段分词长度 == 各段分词长度之和」，即分词器在轮次边界（`<|im_end|>` 特殊 token）处可加性地切分。对 Qwen 的 BPE 这是成立的（特殊 token 是原子 token）。

#### 4.3.3 源码精读

函数全文在 [uniseg3d/query_decoder.py:33-84](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L33-L84)，`IGNORE_INDEX = -100` 定义于 [uniseg3d/query_decoder.py:30](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L30)。逐段看：

- [uniseg3d/query_decoder.py:50-55](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L50-L55) —— 取 chatml 模板并切轮次。模板定义见 [uniseg3d/conversation.py:38-72](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/conversation.py#L38-L72)：`seps = ('<|im_end|>', '<|im_end|>')`、`roles = ('\n<|im_start|>user\n', '\n<|im_start|>assistant\n')`；`qwen3_vl` 的 `system=None`（60-61 行），`qwen2_5_vl` 有 system 段（59 行）——这就是下面分组分支的依据。
- [uniseg3d/query_decoder.py:57-60](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L57-L60) —— 分组：无 system 时两条一组（user+assistant 恰好一对 QA）；有 system 时前三段一组。**注意 `last_invalide_text = rounds[-1]` 是在补 sep 之后取的**，即它末尾带一个 `<|im_end|>`，长度比纯生成提示多 1 个 token——这与最后一步的 `+1` 回退配套（见下）。
- [uniseg3d/query_decoder.py:62-76](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L62-L76) —— mask 主循环：

```python
sep = conv.seps[0] + conv.roles[1]      # '<|im_end|>' + '\n<|im_start|>assistant\n'
for rou in rounds:
    ins = sep.join(rou.split(sep)[:-1]) + sep     # 组内 assistant 头之前的全部内容
    rou_len = tokenizer(rou,  return_length=True).length[0]
    ins_len = tokenizer(ins, return_length=True).length[0]
    labels[:, cur_len:cur_len + ins_len] = IGNORE_INDEX   # 屏蔽提问 + assistant 头
    cur_len += rou_len                                     # 游标推进整组长度
```

  于是每组内被保留的监督区间是 `[cur_len+ins_len, cur_len+rou_len)`——**assistant 回复正文 + 它结尾的 `<|im_end|>`**。保留终止符意味着模型同时学习「何时停止回答」。因果 LM 内部做 shift（logits 的第 \(i\) 位预测第 \(i+1\) 个 token），`-100` 位置被交叉熵忽略，因此损失只落在「看到问题后逐 token 生成回答」上。
- [uniseg3d/query_decoder.py:78-79](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L78-L79) —— 兜底：整条序列全被 mask（如空对话、模板异常）时抛 `ValueError`，避免静默产出零损失。
- [uniseg3d/query_decoder.py:81-82](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L81-L82) —— `labels[:, -last_invalide + 1:] = IGNORE_INDEX`。算术上：`last_invalide` = 生成提示 token 数 G + 1（多算的那个补上的 `<|im_end|>`），回退 1 位后 mask 的恰是最后 G 个 token——**正好是生成提示本身，而上一轮 assistant 的 `<|im_end|>`（作为停止符监督目标）得以保留**。

随后回到 `infer_reason_train` 的调用侧：

- [uniseg3d/query_decoder.py:1427-1431](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L1427-L1431) —— `labels = preprocess_chatml(...)` 后立刻前向：`output = self.reason_model(**data.to(...), labels=labels, output_hidden_states=True)`，`lang_loss = output.loss`。这是 HF 模型内置的移位交叉熵（即本节开头的公式）；`language_losses` 列表逐场景收集。
- [uniseg3d/query_decoder.py:1433-1437](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L1433-L1437) —— 取最后一层隐状态，调 `get_query_tokens_id` 抽取 `<SEG>` 查询（u4-l4 详讲），`output_text = None`（训练分支不生成文本）。
- 汇合点在模型主类：[uniseg3d/uniseg3d.py:1169-1172](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/uniseg3d.py#L1169-L1172) 的 `loss()` 把 `selected_qa_data`、`scene_name`、`point_positions` 交给解码器（`forward_iter_pred` 训练分支在 [uniseg3d/query_decoder.py:1871-1872](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L1871-L1872) 调用 `infer_reason_train`）；语言损失在 [uniseg3d/uniseg3d.py:1173-1184](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/uniseg3d.py#L1173-L1184) 与分割预测一起传给 `Part3dGlammCriterion_v2` 加权合并（u5-l2 详讲）。

**一个值得警惕的对齐问题**：`preprocess_chatml` 用「对文本片段分词的长度」推进游标，即**文本坐标系**；而 processor 会把每个图像占位展开成数十到数百个视觉 token，使 `input_ids` 比 `tokenizer(text)` 更长——两个坐标系可能错位。作者在查询提取侧明确意识到了这一点：[uniseg3d/query_decoder.py:1159-1168](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L1159-L1168) 的 `get_query_tokens_id` docstring 写明它「比 text 版更准确，当额外 image token 等导致 text 与 token 错位时」。而 `preprocess_chatml` 仍是文本版。错位是否实际发生、影响多大，取决于 processor 展开行为，**待本地验证**（下方实践任务专门验证这一点）。

#### 4.3.4 代码实践

**实践目标**：亲手构造一份 ChatML 标签，把「未被 mask 的 token」解码出来，直观看到**监督只落在 assistant 回复**；并考察加入图像占位后的坐标系问题。

**操作步骤**（示例代码，非项目自带脚本；放在 `/tmp` 运行，勿写入仓库）：

```python
# 示例代码：preprocess_chatml_demo.py
# 依赖：transformers（u1-l2 已装）。GPU 与模型权重都不需要。
import torch
from transformers import AutoTokenizer
from uniseg3d.query_decoder import preprocess_chatml   # 需要 mmengine 等依赖可导入；
                                                       # 若导入失败，可把 33-84 行函数
                                                       # 与 conversation.py 一起复制进脚本

tok = AutoTokenizer.from_pretrained('Qwen/Qwen3-VL-4B-Instruct')

# 模拟两条 QA（teacher forcing 的 GT 回复含 <p>...</p><SEG>）
qa_pairs = [
    ('What is the leftmost object on the desk?',
     'The leftmost object is <p>lamp</p><SEG>.'),
    ('How far is it from the camera?', 'About 1.2 meters.'),
]

# 手工按 qwen3_vl 的 chatml 拼文本（等价于 apply_chat_template 的产物）
msgs = ''
for q, a in qa_pairs:
    msgs += f'\n<|im_start|>user\n{q}<|im_end|>'
    msgs += f'\n<|im_start|>assistant\n{a}<|im_end|>'
text = msgs + '\n<|im_start|>assistant\n'   # add_generation_prompt=True 的效果

input_ids = tok(text, return_tensors='pt')['input_ids']
labels = preprocess_chatml(input_ids, text, tok, qwen_model='qwen3_vl')

kept = labels[0] != -100
print('总 token 数:', labels.shape[1], '| 被监督 token 数:', int(kept.sum()))
print('被监督内容拼接:')
print(tok.decode(input_ids[0][kept]))
```

**需要观察的现象**：

1. 打印出的「被监督内容」应当是两段 assistant 回复（含 `<p>lamp</p><SEG>.`、`About 1.2 meters.`），不含任何 user 提问与 `<|im_start|>assistant\n` 头；
2. 每段末尾应包含 `<|im_end|>`（停止符也是监督目标）；
3. 结尾的生成提示 `<|im_start|>assistant\n` 不在监督内容里。

**预期结果**：监督 token 数 ≈ 两条回答的 token 数 + 2 个 `<|im_end|>`；其余位置全部为 -100。若出现 `ValueError('No valid labels found')`，说明手工拼的模板与 `get_conv` 的切分假设不一致（检查行首 `\n` 与 seps）。

**进阶（待本地验证）**：把第 0 轮 user 文本中插入一个图像占位（如 `'<|vision_start|><|image_pad|><|vision_end|>'`），再用「processor 展开后的 input_ids」与「未展开的 text」一起喂给 `preprocess_chatml`，检查 mask 边界是否漂移——这就是 4.3.3 末尾讨论的坐标系问题。真实训练走 `processor(text, images)`，请在本地用真 processor 确认（需要下载视觉配置，无需权重）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 assistant 回复结尾的 `<|im_end|>` 要保留为监督目标，而 assistant 头 `<|im_start|>assistant\n` 要 mask？

**答案**：因果 LM 学习的是「预测下一个 token」。头是输入侧提示（回答尚未开始，没有可预测内容）；而 `<|im_end|>` 是回答的最后一个 token，监督它等价于教模型「何时停止生成」——不学停止，推理时就无法正常收尾。

**练习 2**：若 `tokenizer(rou)` 的长度与该轮在 `input_ids` 中的实际长度不一致，会发生什么？项目里哪个函数是为此设计的？

**答案**：`cur_len` 游标会累计漂移，后续轮次的 mask 区间整体错位，可能把 user 提问漏进监督或把回答开头 mask 掉。图像占位被 processor 展开成多个视觉 token 正是不一致的来源之一；查询提取侧的 `get_query_tokens_id`（[uniseg3d/query_decoder.py:1159-1263](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L1159-L1263)）改从 `input_ids` 里的 `<|im_start|>assistant` 特殊 token 定位轮次区间，就是为了规避 text 坐标错位（u4-l4 详讲）。

**练习 3**：`infer_reason_train` 返回的四个值里，`new_point_feat` 恒为空列表、`output_text` 恒为 None（训练分支）。为什么还要返回它们？

**答案**：为了让训练/推理两条分支共用同一个返回签名（`(new_point_feat, language_losses, output_text, query_list)`），调用方 `forward_iter_pred` 按固定顺序解包（[uniseg3d/query_decoder.py:1871-1872](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L1871-L1872) 与 [1913](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L1913)）。这是接口对齐的常见取舍，代价是返回值语义随模式变化，阅读时需留意。

### 4.4 超点数超限时的 FPS 聚合降采样

#### 4.4.1 概念说明

一个室内场景常有几千到上万个超点，而每多一个 `<|point|>` token，语言模型的序列就长一格、注意力开销平方级增长。训练配置把预算定为 `max_point_tokens=2000`（[configs/ground3dlmm_train.py:76](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/configs/ground3dlmm_train.py#L76)）：超点数不超限就直接全用；超限就用**最远点采样（FPS）**选出一个空间/特征上尽量均匀覆盖的子集送入 LMM。

两个聚合器都在 u4-l2 初次亮相，本讲看它们的**触发时机**：

- `FPSPositionAggregator`（首选）：按**米制质心位置**做 FPS——在物理空间里均匀取点，符合「场景覆盖」直觉；
- `FPSAggregator`（兜底）：按**特征 L2 距离**做 FPS，仅在位置信息缺失/不匹配时使用。

两者都是**零可学习参数**模块（`in_channels` 形参仅为接口兼容而保留），采样不引入任何新训练参数。最关键的一点：**聚合只作用于送入 LMM 的点 token，分割解码器的 K/V 仍使用全量超点特征**——`infer_reason_train` 收到的 `point_feats` 与外部 `forward_iter_pred` 里的 `mask_feats` 是同一批张量，但 FPS 结果只写进局部变量 `i_point_feat`，不改外部引用。语言侧省预算，分割侧不丢精度。

FPS 本身：随机起点，重复「计算所有点到已选集合的最近距离、取最远者」。设超点数为 \(N\)、目标 \(K\)：

\[
S_{k+1} = \arg\max_{i \notin S} \; \min_{j \in S} d(i, j), \qquad |S| = K
\]

#### 4.4.2 核心流程

```text
if self.training and n_points > self.max_point_tokens:
    if 位置可用（point_positions 非 None 且行数 == n_points）:
        i_point_feat = pos_aggregator(i_point_feat, i_point_pos)   # 按质心位置 FPS
    else:
        i_point_feat = point_aggregator(i_point_feat)              # 按特征距离 FPS
    n_points = i_point_feat.shape[0]      # 更新！后续 <|point|> 重复次数与之一致
else:
    不聚合（推理时即使超限也不聚合）
```

FPS 内部（两个聚合器实现一致，仅距离矩阵来源不同）：

```text
1. 若 N <= K：原样返回
2. dist_matrix = torch.cdist(x, x)        # (N, N) 两两距离
3. 随机选起点 current
4. 循环 K-1 次：
     distances = min(distances, dist_matrix[current])   # 每点到已选集的最近距离
     current = distances.argmax()                        # 取最远者
```

#### 4.4.3 源码精读

- 触发逻辑：[uniseg3d/query_decoder.py:1360-1368](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L1360-L1368) —— `self.training` 是 `nn.Module` 的模式标志：训练分支才聚合。位置可用性校验 `i_point_pos.shape[0] == n_points` 防御质心与特征行数不匹配的异常数据。
- 聚合器构建：[uniseg3d/query_decoder.py:952-955](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L952-L955) —— `max_point_tokens` 存为属性，同时实例化 `point_aggregator` 与 `pos_aggregator`（out_tokens 都等于预算）。
- `FPSPositionAggregator.forward`：[uniseg3d/query_decoder.py:240-253](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L240-L253) —— 校验二维形状与行数一致；`n <= out_tokens` 直接返回；否则按位置 FPS 取索引、返回 `feats[indices]`（**特征被索引，位置本身被丢弃**——LMM 只吃特征）。其 `fps_sample` 在 [uniseg3d/query_decoder.py:255-289](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L255-L289)：`torch.cdist` 一次性算两两距离（\(O(N^2)\) 内存），循环内只做 `minimum` 与 `argmax`，起点 `torch.randint` 随机。
- `FPSAggregator`：[uniseg3d/query_decoder.py:162-222](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L162-L222) —— 接口只吃特征，距离矩阵用特征本身算，算法与上面完全同构。
- 位置从哪来：[uniseg3d/uniseg3d.py:1165-1167](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/uniseg3d.py#L1165-L1167) —— `Grounded_LMM_main.loss()` 对原始米制坐标按超点 scatter-mean 求质心（u3-l3 讲过 `sp_centroids` 的推导），再按 `batch_offsets` 切回逐场景，作为 `point_positions` 传入解码器。**质心只在训练路径计算**，这与「只有训练才聚合」配套。
- 聚合后 token 契约：1370 行投影、1399 行 `point_token * n_points` 都使用更新后的 `n_points`（1368 行），保证 4.2 节的数量契约。

#### 4.4.4 代码实践

**实践目标**：用零依赖的小脚本理解 FPS 的「最远」贪心，并观察 `max_point_tokens` 对序列长度的影响路径。

**操作步骤**：

1. 阅读型：对照 [uniseg3d/query_decoder.py:255-289](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L255-L289) 手工模拟 5 个一维点 `[0, 1, 2, 10, 11]`、K=3 的采样（设起点为下标 0）：第 2 个点应是 10（离 0 最远），第 3 个在剩余点中取「到 {0,10} 最近距离」最大者——是 2 吗？请逐步写出 `distances` 的演化。
2. 示例代码（可本地跑，仅需 torch）：

```python
# 示例代码：fps_demo.py —— 复现 FPSPositionAggregator.fps_sample 的贪心
import torch

points = torch.tensor([[0.], [1.], [2.], [10.], [11.]])
K = 3
n = points.shape[0]
dist_matrix = torch.cdist(points, points)
indices = torch.zeros(K, dtype=torch.long)
current = torch.randint(0, n, (1,)).item()
indices[0] = current
distances = torch.ones(n) * float('inf')
for i in range(1, K):
    distances = torch.minimum(distances, dist_matrix[current])
    current = distances.argmax().item()
    indices[i] = current
print('选中索引:', indices.tolist(), '对应位置:', points[indices].flatten().tolist())
```

3. 配置观察：在 [configs/ground3dlmm_train.py:76](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/configs/ground3dlmm_train.py#L76) 把 `max_point_tokens` 调小（如 512）再发起训练，训练日志/显存会怎么变（不必真跑大型训练，说明预期即可）。

**需要观察的现象**：步骤 2 选中的 3 个点在数轴上尽量分散（覆盖两端）；多次运行起点不同、结果可能不同（起点随机）。步骤 3 预期 `input_ids` 中 `<|point|>` 个数被压到 ≤512，序列变短、语言分支显存下降、单步变快，但每个超点 token 承载的信息变粗。

**预期结果**：待本地验证（步骤 3 需要真实数据与训练环境；步骤 1/2 可立即验证，答案：0 → 10 → 2 或依起点等价分散组合）。

#### 4.4.5 小练习与答案

**练习 1**：为什么推理（`infer_reason_eval`）不做 FPS 聚合？

**答案**：推理时序列只走一次前向且没有梯度/优化器状态，显存压力远小于训练；同时保留全部超点 token 让 LMM 看到最完整的场景信息，利于答案与 `<SEG>` 查询质量。训练则受 batch 梯度显存约束，需要预算控制。

**练习 2**：FPS 采样后，`<|point|>` 占位数、`point_embeds` 行数、被 `masked_scatter` 覆盖的位置数三者关系如何维持？

**答案**：1368 行把 `n_points` 更新为聚合后的行数；1370 行投影聚合后的特征（行数即 n_points）；1399 行模板重复 `n_points` 次占位。三者在同一次循环迭代内使用同一变量，钩子校验 `point_mask.sum() == point_embeds.shape[0]`（[uniseg3d/point_qwen3_vl.py:27-28](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/point_qwen3_vl.py#L27-L28)）兜底。

**练习 3**：`torch.cdist` 一次性算 \(N \times N\) 距离矩阵，N 很大时有什么风险？

**答案**：内存与时间都是 \(O(N^2)\)。对本项目 N 约几千（超点数）尚可承受；若直接对几十万原始点做 FPS 则不可行——这也是「先超点池化、再 FPS」两级压缩的一个理由（先由 scatter_mean 把点压到超点级，见 u3-l3）。

## 5. 综合实践

**任务：手工走通一个训练语言分支的最小闭环（无 GPU）**。

把 4.2 与 4.3 的实践合并升级：写一个脚本（示例代码，建议放 `/tmp`），模拟 `infer_reason_train` 在**单场景、两条 QA（第 1 条含 `<p>椅背</p><SEG>` 接地、第 2 条纯测量问答）**下的语言分支全流程（不含 4B 权重前向）：

1. 用 `AutoTokenizer.from_pretrained('Qwen/Qwen3-VL-4B-Instruct')` + `add_special_tokens` 注册 `<|point|>`、`<p>`、`</p>`、`<SEG>`（模拟 [uniseg3d/query_decoder.py:980-994](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L980-L994) 的注册）；
2. 设 `n_points = 5`，按 4.1 的消息结构手工拼 ChatML 文本（含 5 个 `<|point|>`）；
3. 计算 `input_ids`、`point_mask`，断言 `point_mask.sum() == 5`；
4. 调 `preprocess_chatml` 得 labels，统计并 decode 监督段；
5. 回答三个检查问题：(a) 监督段 decode 出的第一段文本是什么？(b) `<p>`、`</p>`、`<SEG>` 是否在监督段内（它们应该被监督——模型要学会输出接地标记）？(c) 生成提示是否被排除？

**预期结果**：(a) 第 1 条 GT 回复全文（含 `<p>椅背</p><SEG>`）；(b) 是——这正是 `<SEG>` 位置的隐状态能承载分割查询的前提（u4-l4 的出发点）；(c) 是。若三项全对，你就把本讲的四个模块串成了一条完整链路。运行结果待本地验证。

## 6. 本讲小结

- **messages 构造**：`infer_reason_train` 把 `selected_qa_data` 展平为 `qa_pairs`，拼成多轮 ChatML messages——仅第 0 轮注入图像与 `n_points` 个 `<|point|>` 占位（模板 `{point}` 替换），assistant 消息放 GT 回复（teacher forcing）。
- **编码与注入**：`processor` 产出 `input_ids` 后，手工附加 `point_embeds`（`point2token` 投影）与 `point_mask = input_ids == point_token_id`；钩子校验「mask 计数 == 特征行数」后 `masked_scatter` 覆盖占位——对齐完全由 token id 相等性保证。
- **标签与损失**：`preprocess_chatml` 按 `<|im_end|>` 切轮、两条一组（qwen3_vl 无 system），用「子串分词长度」推进游标，把提问与角色头置 -100，只留 assistant 正文与其终止符；末尾生成提示经「+1 回退」恰好整体排除。语言损失由 HF 内置移位交叉熵给出，只落在 assistant 回复上。
- **FPS 降采样**：训练且超点数 > `max_point_tokens`(2000) 时，优先按质心位置（`FPSPositionAggregator`）、否则按特征距离做零参数 FPS；只压语言侧 token，分割解码器 K/V 仍用全量超点特征。
- **坐标系隐患**：`preprocess_chatml` 工作在文本坐标系，图像占位被 processor 展开后可能与 `input_ids` 错位；查询提取侧已有基于 input_ids 的 `get_query_tokens_id` 规避，标签侧是否受影响待本地验证。

## 7. 下一步学习建议

下一讲 **u4-l4「<SEG> 查询提取：从隐状态到分割查询」**将深入本讲埋下的伏笔：`get_query_tokens_id` 如何用 `<|im_start|>assistant` 特殊 token 在 `input_ids` 里定位每轮回复区间、抽取 `<p>…</p>` 之间隐状态的均值作为分割查询、以及无效查询的兜底策略。建议提前重读 [uniseg3d/query_decoder.py:1159-1263](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L1159-L1263)，并对照本讲 4.3 的「文本坐标系 vs input_ids 坐标系」问题思考：为什么训练分支选了 id 版而不是 text 版（[uniseg3d/query_decoder.py:1434](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L1434) 调用的正是 `get_query_tokens_id`）。
