# 推理分支 infer_reason_eval：生成回答并提取查询

## 1. 本讲目标

上一讲（u4-l4）我们搞清楚了「`<SEG>` 查询长什么样、从哪里切出来」；这一讲往上游走一步，回答三个问题：

1. 推理时，模型是**怎么生成出那段包含 `<p>…</p><SEG>` 的回答的**——`_generate_response` 的解码参数（贪心、重复惩罚 1.2、max_new_tokens=1024）各自起什么作用。
2. 为什么项目要**「generate 一次、再完整前向一次」**才能拿到隐状态，这两步各自承担什么。
3. 单轮 QA 与多轮对话在推理时的**处理差异**，以及每个场景的预测 JSON 到底在哪里写出、结构长什么样。

学完本讲，你应该能独立读懂 `Grounded_Decoder_Eval` 的整条推理链路，并能估算「一个场景要跑多少次 LLM 前向」这类工程账。

## 2. 前置知识

本讲是单元 4 的收官前一站，默认你已读过 u4-l3（训练分支 `infer_reason_train`）与 u4-l4（`<SEG>` 查询提取）。这里补几个解码侧的基础概念：

- **自回归生成（generate）**：语言模型逐个 token 地「读前文、算下一个 token 的概率分布、挑一个、拼回输入、再算下一个」，直到产出结束符（Qwen 系是 `<|im_end|>`）或达到 `max_new_tokens`。`transformers` 的 `model.generate(...)` 封装了这整个循环。
- **贪心解码（greedy）**：每步都选概率最大的 token，即 `do_sample=False`。没有随机性，同一输入永远得到同一输出——评估复现性的前提。
- **采样三参数**：`temperature`、`top_k`、`top_p` 只在 `do_sample=True` 时有意义。显式传 `None` 是为了把它们从生成配置里清掉，避免 transformers 校验报警。
- **重复惩罚（repetition penalty）**：对「已经生成过」的 token 的 logits 打折，抑制 "the the the" 式复读。设惩罚系数 \(\theta\)，某 token 历史 logit 为 \(s\)，惩罚后为：

  \[
  s' = \begin{cases} s / \theta & s > 0 \\ s \cdot \theta & s < 0 \end{cases}
  \]

  本项目取 \(\theta = 1.2\)。对 `<p>`、`<SEG>` 这类会高频出现的特殊 token，这个惩罚直接影响回答里能出现几处接地标记。
- **KV cache 与 teacher-forcing 前向**：`generate` 用 KV cache 增量解码（每步只算新 token）；而直接调用 `model(input_ids=完整序列)` 的「普通前向」一次算完整序列所有位置的隐状态——这正是二次前向取隐状态用的模式。
- **attention_mask**：形状与 `input_ids` 对齐的 0/1 向量，1 表示该位置是真实 token。generate 返回的 `sequences` 只含真实 token，所以拿到完整序列后要手工把 prompt 段的 mask 与「生成段全 1」拼起来。
- **autocast**：`torch.cuda.amp.autocast(dtype=...)` 让前向在混合精度下运行；`_get_target_dtype()` 决定用 bf16 还是 fp16。

另外回顾两个本讲直接用到的旧结论（详见 u4-l1、u4-l4）：

- `<|point|>` 占位 token 由前向预钩子（pre-hook）用 `masked_scatter` 注入超点嵌入，**生成阶段只在预填充（prefill）那一步注入**；
- `get_query_tokens_eval` 输入只含生成段，全局搜索 `<p>…</p>` 取指称文本隐状态的均值池化，每条 QA 至多取 2 个查询。

## 3. 本讲源码地图

| 文件 | 本讲关注点 |
| --- | --- |
| [uniseg3d/query_decoder.py](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py) | 主战场：`Grounded_Decoder_Eval` 的推理方法群（`infer_reason_eval` / `_generate_response` / `_get_hidden_states_and_query_token` / `_process_single_turn_qa` / `_process_multi_turn_qa`），以及 `forward_iter_pred` 的推理分支 |
| [configs/ground3dlmm_eval_ground3d_scannet.py](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/configs/ground3dlmm_eval_ground3d_scannet.py) | 评估配置：decoder 类型是 `Grounded_Decoder_Eval`，`image_dir` 指向帧图像目录 |
| [uniseg3d/uniseg3d.py](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/uniseg3d.py) | `predict()` 里对解码器的调用点，确认本讲在整条链路中的位置 |
| [uniseg3d/prompt_template.py](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/prompt_template.py) | 两个指令模板 `PART_GLAMM_INSTRUCTION` 与 `PART_GLAMM_INSTRUCTION_IMG`，单轮与多轮第 0 轮分别使用 |

**先澄清一个容易踩的坑**：评估配置里 decoder 的 `type` 是 `Grounded_Decoder_Eval`（[configs/ground3dlmm_eval_ground3d_scannet.py:44-74](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/configs/ground3dlmm_eval_ground3d_scannet.py#L44-L74)），而**不是**训练配置里的 `Grounded_Decoder_Joint`（u4-l2 已讲）。两个类各自持有一套几乎逐行相同的推理方法：本讲以 `Grounded_Decoder_Eval` 为主线讲，Joint 版的对应位置会在 4.1.3 给出对照。此外还有两个子类变体 `Grounded_Decoder_Eval_MultiImg`（ScanRefer/Reason3D 多视图）与 `Grounded_Decoder_Eval_Text`（纯文本 3D），留给 u7-l1。

## 4. 核心概念与源码讲解

### 4.1 推理分支总调度：infer_reason_eval

#### 4.1.1 概念说明

训练时（u4-l3），语言分支一次前向就能同时拿到语言损失和隐状态——因为 GT 回复就拼在输入里（teacher forcing）。推理时没有 GT 回复，回答得靠模型自己生成，于是语言侧分裂成「生成回答」与「提取查询」两个阶段，`infer_reason_eval` 就是这两个阶段在**每个场景**上的总调度器。

它的职责可以概括为四件事：

1. 逐场景循环：解析场景名、定位参考帧图像、把超点特征投影成 `<|point|>` 嵌入；
2. 逐 QA 分发：单轮 QA 走 `_process_single_turn_qa`，多轮对话走 `_process_multi_turn_qa`；
3. 收集每条 QA 的 `<SEG>` 查询，按场景打包返回；
4. 把文本预测写成一个 JSON 文件。

另一个与训练分支的关键差异：**推理时不做 FPS 聚合**。聚合（`max_point_tokens=2000` 的降采样）只存在于 `self.training` 分支（[uniseg3d/query_decoder.py:2533-2541](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L2533-L2541)），`infer_reason_eval` 里直接 `point2token(i_point_feat)` 全量进语言模型（[uniseg3d/query_decoder.py:2876](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L2876)）。也就是说配置里的 `max_point_tokens` 只约束训练；评估时若某场景超点数很多，`<|point|>` token 数也随之增多。

#### 4.1.2 核心流程

调用链（承接 u3-l1 讲过的 `predict()`）：

```text
Grounded_LMM_main.predict()                       # uniseg3d.py:1305
  └─ decoder(x, scene_names, qa_data, ...)        # Grounded_Decoder_Eval.forward
       └─ forward_iter_pred(...)                   # iter_pred=True（configs L58）
            └─ else 分支（非 training）             # uniseg3d/query_decoder.py:3082
                 └─ infer_reason_eval(mask_feats, qa_data, scene_names=...)
                      for 每个场景 i:
                        ├─ 解析 scene_name / frame_id，定位 image_path
                        ├─ point_embeds = point2token(i_point_feat)
                        ├─ for 每条 QA:
                        │    ├─ 单轮(dict 且 qa_keys != 'multi_turn')
                        │    │    └─ _process_single_turn_qa   → 生成 + 二次前向
                        │    └─ 多轮(list 或 qa_keys == 'multi_turn')
                        │         └─ _process_multi_turn_qa    → 逐轮 生成 + 二次前向
                        ├─ 写 {save_pred_qa_dir}/{场景名}.json
                        └─ query_tokens_concat_list.append(query_token_list)
                 ← (new_point_feat=[], language_losses=[], query_list)
            └─ 逐 QA 跑 6 层分割解码器（下一讲 u5-l1 的内容）
```

每条 QA（多轮则每一轮）都要付出「一次 generate + 一次完整前向」的成本，这是评估慢的主因，4.1.4 的实践会让读者自己算这笔账。

#### 4.1.3 源码精读

**入口调用点**——`predict()` 把 `scene_names` 与 `selected_qa_data` 一起交给解码器，随后立刻用 `qa_predict_by_feat` 消费其输出：[uniseg3d/uniseg3d.py:1305-1306](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/uniseg3d.py#L1305-L1306)。这两行说明：语言分支的产物（每条 QA 的查询）与分割分支的产物（mask）在 `forward_iter_pred` 内部被串成一条流水线，JSON 写盘发生在**分割解码之前**。

**推理分支的分发**——`forward_iter_pred` 在非训练态调用 `infer_reason_eval`：[uniseg3d/query_decoder.py:3081-3084](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L3081-L3084)。

**总调度本体**（`Grounded_Decoder_Eval` 版）：

```python
def infer_reason_eval(self, point_feats, qa_data, point_positions=None, ...):
    target_dtype = self._get_target_dtype()
    for i in range(len(point_feats)):
        i_scene_name = scene_names[i]
        ...
        scene_name, frame_id = i_scene_name.split('_frame_')
        image_path = (None if self.image_dir is None
                      else os.path.join(self.image_dir, scene_name, frame_id + '.jpg'))
        point_embeds = self.point2token(i_point_feat).to(self.reason_model.device)
        pred_qa_data = {}
        query_token_list = []
        for i_qa_data in qa_data:
            if isinstance(i_qa_data, dict) and i_qa_data.get('qa_keys') != 'multi_turn':
                self._process_single_turn_qa(...)
            else:
                self._process_multi_turn_qa(...)
        query_tokens_concat_list.append(query_token_list)
        with open(os.path.join(self.save_pred_qa_dir, f'{i_scene_name}.json'), 'w') as f:
            json.dump(pred_qa_data, f, indent=4)
    return new_point_feat, language_losses, query_tokens_concat_list
```

完整源码见 [uniseg3d/query_decoder.py:2860-2900](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L2860-L2900)。四个要点：

- 场景名形如 `scene0011_00_frame_000123`，按 `_frame_` 切出场景与帧号，去 `image_dir/<场景>/<帧号>.jpg` 找参考帧（[uniseg3d/query_decoder.py:2873-2875](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L2873-L2875)）。**3D-only 权重**的 `image_dir=None`（由 `--cfg-options` 覆盖，见 u4-l6），此时 `image_path=None`，消息中的图像条目稍后会被 `_prepare_messages_data` 过滤掉。
- 分发判据是数据侧打的标签：QA 字典且 `qa_keys != 'multi_turn'` 走单轮，否则（列表形式的多轮对话）走多轮（[uniseg3d/query_decoder.py:2883](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L2883)）。`qa_keys` 这个字段正是 u2-l4 里 `QA_Generation_v2` 打上的标签。
- JSON 每场景一份，文件名就是 `i_scene_name`（含帧号），写盘在返回查询列表之前（[uniseg3d/query_decoder.py:2896-2898](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L2896-L2898)）。
- 返回值是三元组，前两个（`new_point_feat`、`language_losses`）恒为空列表——推理时既不需要新点特征也没有语言损失，保留它们只是为了与训练分支的返回签名兼容。

**`save_pred_qa_dir` 的一个坑**：评估配置的 decoder 段**并没有**写 `save_pred_qa_dir`，构造函数默认置 `None`（[uniseg3d/query_decoder.py:2012-2016](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L2012-L2016)）。若不通过 `--cfg-options model.decoder.save_pred_qa_dir=preds` 显式打开，推理会一路跑到写盘那行才因 `os.path.join(None, ...)` 抛 `TypeError`。这正是 u1-l4 快速上手命令里必须带这个覆盖的原因。

**Joint 版对照**：`Grounded_Decoder_Joint` 的同名方法在 [uniseg3d/query_decoder.py:1686-1731](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L1686-L1731)，主体逻辑相同，差别只在图像目录：它持有 `scannet_image_dir` 与 `scannetpp_image_dir` 两个目录，按场景名是否含 `scene` 前缀选择（[uniseg3d/query_decoder.py:1701-1706](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L1701-L1706)）；而 Eval 版只有一个 `image_dir`，为 None 即退化为无图输入。

#### 4.1.4 代码实践：推理前向次数账本

**实践目标**：不经 GPU，只靠源码推演「跑完一个场景需要多少次完整 LLM 前向」，建立对推理成本的量化直觉。

**操作步骤**：

1. 打开 [uniseg3d/query_decoder.py:2881-2892](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L2881-L2892)，确认每条单轮 QA 触发一次 `_generate_response`（内含一次 generate）和一次 `_get_hidden_states_and_query_token`（内含一次完整前向）。
2. 打开 [uniseg3d/query_decoder.py:2810-2856](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L2810-L2856)，确认多轮对话对**每一轮**都重复「生成 + 二次前向」。
3. 假设某场景在 `task_level='multi_turn_qa_data'` 下取到 1 条 3 轮对话；再假设切到 `task_level='part_qa_data'` 取到 5 条单轮 QA。分别计算两种设置的完整前向次数。

**需要观察的现象**：账本公式——单轮设置为 \(2 \times N\)（N 条 QA），多轮设置为 \(2 \times T\)（T 轮）；同时多轮设置下每次 generate 的输入序列越来越长（历史在累积），单条耗时也会逐步上升。

**预期结果**：`part_qa_data` 5 条 → 10 次完整前向；`multi_turn_qa_data` 1 条 3 轮 → 6 次完整前向，且第 3 次生成最慢。这是纯源码推演，结论可直接从代码结构得出，无需本地验证。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `infer_reason_eval` 里完全见不到 u3-l1 讲过的 `batch_offsets` / `superpoint_bias`？

**答案**：这两个量用于把 batch 内多个场景的超点编号拼进全局空间，而那一步发生在模型入口 `extract_feat`（池化后已按偏移切回逐场景）。`infer_reason_eval` 收到的 `point_feats[i]` 已是场景局部张量，逐场景循环内无需再关心全局偏移。

**练习 2**：某评估场景的超点数是 3500，超过配置的 `max_point_tokens=2000`。推理时会触发 FPS 聚合吗？

**答案**：不会。聚合只写在 `if self.training and n_points > self.max_point_tokens:` 分支里（[uniseg3d/query_decoder.py:2533](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L2533)）。推理时 3500 个 `<|point|>` token 全量进入语言模型，评估大场景时要留意显存与序列长度。

**练习 3**：JSON 写盘（L2897）与 6 层分割解码器（L3087 起）谁先执行？

**答案**：JSON 先写。`infer_reason_eval` 在循环内写完 JSON 才返回，`forward_iter_pred` 拿到返回值后才逐 QA 跑分割解码器。所以即使分割解码阶段出错，文本预测 JSON 也已经落盘。

### 4.2 一步生成：_generate_response 的贪心解码与重复惩罚

#### 4.2.1 概念说明

`_generate_response` 是对 `self.reason_model.generate(...)` 的薄封装，但每个参数都有讲究：

| 参数 | 取值 | 作用 |
| --- | --- | --- |
| `do_sample` | `False` | 贪心解码，逐位取最大概率 token，结果可复现 |
| `temperature` / `top_k` / `top_p` | `None` | 显式清空采样参数，与贪心模式保持一致 |
| `repetition_penalty` | `1.2` | 抑制复读，见下文公式 |
| `max_new_tokens` | `1024` | 单条回答的 token 上限 |
| `output_hidden_states` | `True` | 请求逐步隐状态（实际未被使用，见 4.3） |
| `return_dict_in_generate` | `True` | 返回结构化对象而非裸 tensor，便于取 `.sequences` |

为什么贪心之外还要加重复惩罚？接地任务的回答是高度模板化的（`<p>seat</p><SEG>` 这类模式反复出现），贪心解码在模板化输出上特别容易陷入复读循环；而复读会把无关 token 也堆进 `<p>…</p>` 之间，污染 u4-l4 讲的「指称文本均值池化」查询。1.2 是一个温和的取值：足以打断循环，又不至于把正常重复出现的 `<p>`、`<SEG>` 标记压到出不来。

整个调用包在 `torch.cuda.amp.autocast(dtype=target_dtype)` 里。`target_dtype` 由 `_get_target_dtype()` 用三级回退决定：先看 `PeftModel.dtype`，若是 float32 再看第一个参数的 dtype，仍是 float32 则按 GPU 支持度强制 bf16/fp16（[uniseg3d/query_decoder.py:2626-2642](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L2626-L2642)）——LoRA 包裹后模型自报 float32 是常见现象，这个回退保证 Flash Attention 类算子拿到正确的半精度。

#### 4.2.2 核心流程

```text
_generate_response(data, target_dtype):
    with autocast(target_dtype):
        outputs = reason_model.generate(
            **data,                       # input_ids / attention_mask / pixel_values / point_embeds / point_mask
            do_sample=False,              # 贪心
            repetition_penalty=1.2,       # 已出现 token 的 logits 打折
            max_new_tokens=1024, ...)
    return outputs                        # .sequences = prompt + 生成段
```

生成循环内部（每个新 token 一步）：

1. 用 KV cache 前向当前步（首步为整个 prompt 的预填充，`<|point|>` 嵌入在这一步由钩子注入）；
2. 取最后一个位置的 logits，对历史出现过的 token 施加 4.2.1 的惩罚公式；
3. 贪心取 argmax，追加到序列；
4. 若产出 `<|im_end|>` 或步数达 1024 则停止。

#### 4.2.3 源码精读

`Grounded_Decoder_Eval` 版实现（无 try/except，出错即抛栈）：

```python
def _generate_response(self, data, target_dtype):
    """Run model generation"""
    with torch.cuda.amp.autocast(dtype=target_dtype):
        outputs = self.reason_model.generate(
            **data,
            do_sample=False,
            temperature=None,
            top_k=None,
            top_p=None,
            repetition_penalty=1.2,
            max_new_tokens=1024,
            output_hidden_states=True,
            return_dict_in_generate=True
        )
    return outputs
```

见 [uniseg3d/query_decoder.py:2670-2684](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L2670-L2684)。两点值得注意：

- **`output_hidden_states=True` 是「摆设」**：调用方 `_process_single_turn_qa` 只使用了 `outputs.sequences`，从未碰 `outputs.hidden_states`。真正取隐状态靠的是 4.3 的二次前向。源码里保留着一段被注释掉的「逐步隐状态拼接」方案（[uniseg3d/query_decoder.py:2765-2776](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L2765-L2776)），说明作者尝试过直接复用 generate 的隐状态，最终放弃了这条路。
- **Joint 版带裸 try/except**：[uniseg3d/query_decoder.py:1493-1510](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L1493-L1510) 用 `except: print(data)` 吞掉异常后仍 `return outputs`——若 generate 真的失败，`outputs` 未定义会接着抛 `NameError`。Eval 版没有这层包裹，排障时栈信息更干净。阅读源码时见到「宽 try + 裸 print」要警觉：它往往掩盖真实错误。

`data` 的来源是 `_prepare_messages_data`（[uniseg3d/query_decoder.py:2655-2668](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L2655-L2668)）：先过滤掉 `image=None` 的条目（3D-only 路径），再 `apply_chat_template(add_generation_prompt=True)` 生成带「assistant 起头」的 prompt，编码后附加 `point_embeds` 与 `point_mask = input_ids == point_token_id`——与训练分支 u4-l3 完全同一套契约，只是这里末尾是生成提示而非 GT 回复。

#### 4.2.4 代码实践：亲手实现一次重复惩罚

**实践目标**：用一个不依赖本仓库、纯 CPU 的最小示例理解重复惩罚如何改变贪心解码的结果。

**操作步骤**（以下为**示例代码**，非项目源码，可在任意装有 PyTorch 的环境运行）：

```python
import torch

# 模拟某步的 logits：只关心 3 个 token
tok = {0: 'the', 1: 'seat', 2: '<SEG>'}
logits = torch.tensor([2.0, 1.5, 1.0])   # 贪心会选 'the'
already_generated = {0}                   # 'the' 已经生成过

theta = 1.2
penalized = logits.clone()
for t in already_generated:
    s = penalized[t]
    penalized[t] = s / theta if s > 0 else s * theta

print('原始 logits :', logits.tolist(), '→ 选', tok[logits.argmax().item()])
print('惩罚后      :', [round(x, 3) for x in penalized.tolist()],
      '→ 选', tok[penalized.argmax().item()])
```

**需要观察的现象**：`the` 的 logit 从 2.0 降到约 1.667，与 `seat` 的 1.5 接近；本例中贪心选择仍是 `the`，把 `logits[1]` 改成 1.7 再跑一次，惩罚后选择会翻转为 `seat`。

**预期结果**：惩罚只作用于「已出现过的 token id」，未出现过的 token 不受影响；惩罚系数越大，翻转发散越容易。**待本地验证**（本环境未安装 PyTorch，未实际运行）。

进阶一步（源码级假设实验，**待本地验证**）：把 [uniseg3d/query_decoder.py:2679](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L2679) 的 `repetition_penalty=1.2` 临时改为 `1.0`，重跑一次 `scale_estimation` 子任务推理，对比 `preds/*.json` 中 `pred_answer` 是否出现整句复读或 `<p>` 标记异常堆积；再把 `max_new_tokens` 从 1024 改为 32，观察长回答是否被截断（截断的回答可能丢失 `<SEG>`，导致该条 QA 退化为零查询）。改完记得还原。

#### 4.2.5 小练习与答案

**练习 1**：`do_sample=False` 时为什么还要显式传 `temperature=None, top_k=None, top_p=None`？

**答案**：这三个参数只影响采样。若模型的生成配置里残留非默认值，transformers 在贪心模式下会告警甚至校验报错；显式置 `None` 把它们从本路径清空，保证行为确定。

**练习 2**：重复惩罚系数 \(\theta=1.2\) 时，一个历史 logit 为 \(-3.0\) 的已出现 token，惩罚后是多少？

**答案**：负 logit 走乘法分支：\(-3.0 \times 1.2 = -3.6\)，即负值变得更负——两个分支都是「把已出现 token往下压」。

**练习 3**：生成阶段的 `<|point|>` 嵌入是每一步都注入吗？

**答案**：不是。`<|point|>` 占位只存在于 prompt 中，钩子在预填充步把它们一次性替换；此后每步增量解码的新 token 里不再有 `<|point|>`，无需注入（机制见 u4-l1）。

### 4.3 二次前向取隐状态：_get_hidden_states_and_query_token

#### 4.3.1 概念说明

回答生成之后，还需要「回答里 `<p>…</p>` 位置的隐状态」来做分割查询。问题是：**generate 已经算过一遍前向，为什么还要再前向一次？**

原因在 `return_dict_in_generate=True` 时隐状态的返回结构：它是按「生成步骤 × 模型层」组织的嵌套元组——第 0 个元素是 prompt 完整前向的各层隐状态，之后每步一个元素、每层一个 `(batch, 1, hidden)` 的小张量。要用它得到「最后一层、全序列」的形态，得自己逐步逐层抽取、按序列维拼接、再小心对齐起点——源码注释区那段被废弃的代码（[uniseg3d/query_decoder.py:2765-2776](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L2765-L2776)）正是这次尝试的遗迹。

项目选择了更简单、更不易错的路线：**把「prompt + 生成段」拼成完整序列，做一次标准的 teacher-forcing 前向**（`use_cache=False`、`output_hidden_states=True`），直接得到形状规整的 `(1, L, hidden)` 最后一层隐状态。代价是每个 QA 多付一次完整前向（4.1.4 的账本里那个因子 2），换来的是实现简单、与训练分支的取法保持一致——训练时隐状态本就来自一次完整前向（u4-l3），同一来源保证查询分布在训练/推理两个阶段对齐。

#### 4.3.2 核心流程

```text
拿到 outputs.sequences（prompt+生成，长度 L = P + G）
1. prompt_mask = data['attention_mask']                  # 长度 P
2. full_mask   = concat(prompt_mask, ones(G))            # 长度 L，生成段全 1
3. forward_inputs = data 的副本，替换 input_ids=sequences、attention_mask=full_mask
   （point_embeds / point_mask 原样保留 → 钩子在此次前向再次注入点特征）
4. outs2 = reason_model(**forward_inputs, use_cache=False, output_hidden_states=True)
5. hidden = outs2.hidden_states[-1]                      # (1, L, hidden) 最后一层
6. output_hidden_states = hidden[:, -G:]                 # 只保留生成段
7. query_token = get_query_tokens_eval(生成段隐状态, 生成段 ids, 文本)
```

其中第 6 步的切片之所以成立：`output_ids = sequences[:, P:]` 长度为 G，而 hidden 的最后 G 列恰好对应生成段（单样本、无左侧填充，位置一一对应）。第 7 步就是 u4-l4 精读过的 `get_query_tokens_eval`：输入只含生成段，直接全局搜 `<p>…</p>`，无需像训练版那样按 assistant 轮次切区间。

#### 4.3.3 源码精读

**完整序列的 mask 拼接**（单轮版，多轮版逐行相同）：

```python
prompt_mask = data["attention_mask"].to(seq.device)
new_len = seq.size(1) - prompt_mask.size(1)
full_mask = torch.cat(
    [prompt_mask, torch.ones(prompt_mask.size(0), new_len, device=seq.device, dtype=prompt_mask.dtype)],
    dim=1
)
forward_inputs = dict(data)
forward_inputs["input_ids"] = seq
forward_inputs["attention_mask"] = full_mask
```

见 [uniseg3d/query_decoder.py:2780-2789](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L2780-L2789)。注意 `forward_inputs` 是 `dict(data)` 的浅拷贝再覆盖两个键——`point_embeds`、`point_mask`、图像特征等其余输入原封不动带进第二次前向，`<|point|>` 占位因此再次被钩子替换，两条前向看到的是同一份点特征。

**二次前向与查询提取**：

```python
with torch.no_grad():
    with torch.cuda.amp.autocast(dtype=target_dtype):
        outs2 = self.reason_model(
            **data, output_hidden_states=True, return_dict=True, use_cache=False)
        hidden_states = outs2.hidden_states[-1]

output_hidden_states = hidden_states[:, -output_ids.shape[1]:]
query_token = self.get_query_tokens_eval(output_hidden_states, output_ids, output_text)
```

见 [uniseg3d/query_decoder.py:2686-2705](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L2686-L2705)。要点：`torch.no_grad()`（推理不建图）、`use_cache=False`（不再生成，省下 KV cache 显存）、`hidden_states[-1]`（最后一层，与训练分支取法一致）、切片 `-output_ids.shape[1]:` 对准生成段。

**零查询的下游兜底**（衔接 u4-l4 的「零向量=无效查询」约定）：若生成段没有 `<p>…</p>`，`get_query_tokens_eval` 返回一个零向量（[uniseg3d/query_decoder.py:2419-2425](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L2419-L2425)）。随后 `forward_iter_pred` 的推理分支在跑分割解码器时，把零查询行的预测 mask 强制为常数 `1e-4`（[uniseg3d/query_decoder.py:3101](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L3101)）——接近全零又非严格零，保证后续 IoU 计算拿到的是「极低置信度 mask」而不是满 0 或 NaN。这里识别零查询用的是 `query.abs().sum(-1) == 0`（投影前的原始查询），与 u4-l4 讲过的「必须在投影前识别」一致。

#### 4.3.4 代码实践：验证 mask 拼接与切片对齐

**实践目标**：用纯 CPU 张量操作复现「prompt mask + 生成段全 1」的拼接与 `[:, -G:]` 切片，确认二者语义自洽。

**操作步骤**（**示例代码**，非项目源码）：

```python
import torch

P, G = 7, 4                                   # 假设 prompt 7 个 token、生成 4 个
prompt_ids = torch.randint(0, 100, (1, P))
gen_ids    = torch.randint(0, 100, (1, G))
seq = torch.cat([prompt_ids, gen_ids], dim=1) # 模拟 outputs.sequences

attention_mask = torch.ones(1, P, dtype=torch.long)   # 模拟 data['attention_mask']
output_ids = seq[:, attention_mask.size(1):]          # 切掉 prompt，长度 G

new_len = seq.size(1) - attention_mask.size(1)
full_mask = torch.cat(
    [attention_mask,
     torch.ones(attention_mask.size(0), new_len, dtype=attention_mask.dtype)],
    dim=1)

hidden = torch.arange(seq.size(1)).float().reshape(1, seq.size(1), 1)  # 位置编码充当隐状态
gen_hidden = hidden[:, -output_ids.shape[1]:]

print('full_mask       :', full_mask.tolist())
print('seq 长度 / gen 长度:', seq.size(1), '/', output_ids.size(1))
print('生成段对应位置   :', gen_hidden.flatten().tolist())   # 应为 [7, 8, 9, 10]
```

**需要观察的现象**：`full_mask` 是 11 个 1；`gen_hidden` 取出的位置编号恰是序列的第 7~10 位，即生成段。

**预期结果**：`output_ids` 与 `gen_hidden` 在序列维严格对齐——这正是 `_get_hidden_states_and_query_token` 里按 id 搜 `<p>`、按位置取隐状态能够配对的前提。**待本地验证**（本环境未运行）。

#### 4.3.5 小练习与答案

**练习 1**：为什么二次前向传 `use_cache=False`？

**答案**：这次前向的目的只是取各层隐状态，不再继续生成；预留下一代的 KV cache 既浪费显存又无用途，显式关掉。

**练习 2**：如果把切片写成 `hidden_states[:, -output_ids.shape[1]-1:]`（多取一位），会取到什么？

**答案**：会多带上生成段前一个位置——即 prompt 的最后一个 token（通常是 ChatML 的 assistant 起头标记）。它不在任何 `<p>…</p>` 区间内，通常不改变结果，但语义上就错了；这类「差一位」是隐状态对齐最常见的 bug 形态。

**练习 3**：二次前向携带的 `point_mask` 有什么作用？

**答案**：它标记 `input_ids` 中 `<|point|>` 占位的位置，是 u4-l1 前向预钩子的定位依据；钩子据此把 `point_embeds` masked_scatter 进 `inputs_embeds`。二次前向的输入同样包含这些占位，因此必须原样携带，两条前向的点特征注入方式完全一致。

### 4.4 单轮/多轮 QA 处理与预测 JSON 写出

#### 4.4.1 概念说明

数据管线（u2-l4）送来的 `selected_qa_data` 是一个列表，元素有两种形态：

- **单轮 QA**：字典，含 `question`、`answer`，以及 QA 采样阶段打上的 `qa_level`、`qa_key`、`qa_task_type`、`qa_keys` 标签；
- **多轮对话**：一个「轮次字典」的列表，每一轮各有 `question` / `answer`。

两类元素的推理策略不同：

| 维度 | 单轮 `_process_single_turn_qa` | 多轮 `_process_multi_turn_qa` |
| --- | --- | --- |
| 独立性 | 每条 QA 独立成对话，互不影响 | 同一对话内逐轮累积历史（LLaVA 式滚动上下文） |
| 提示模板 | `PART_GLAMM_INSTRUCTION`（无「given image」措辞，含 `<sentence>` trigger 说明） | 第 0 轮用 `PART_GLAMM_INSTRUCTION_IMG`（提到点云与图像），后续轮只有裸问题 |
| `<|point|>` 注入 | 每条 QA 的 prompt 都带 n_points 个占位 | 仅第 0 轮带占位，后续轮不重复 |
| 前向次数 | 每条 2 次（生成 + 二次前向） | 每轮 2 次，且输入随历史变长 |
| JSON 形态 | 三级键嵌套 dict | 顶层列表，逐轮记录带 `turn_idx` |
| 查询产出 | 每条 QA 一个 query tensor | 每轮一个 query tensor |

两个模板的原文见 [uniseg3d/prompt_template.py:36-43](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/prompt_template.py#L36-L43)（`PART_GLAMM_INSTRUCTION`，`{point}` 处填 `n_points` 个 `<|point|>` 字符串、`{question}` 处填问题）与 [uniseg3d/prompt_template.py:46-52](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/prompt_template.py#L46-L52)（`PART_GLAMM_INSTRUCTION_IMG`）。二者都明确要求回答按 `<p>名称</p><SEG>` 的格式标注接地目标——这是生成端与 4.3 查询提取端之间的「协议」。

#### 4.4.2 核心流程

**单轮**：

```text
取 qa_level / qa_key / qa_task_type（列表形态先取 [0]）
question, gt_answer ← i_qa_data
input_txt = PART_GLAMM_INSTRUCTION.format(point='<|point|>'*n_points, question=...)
messages = [user: [image 条目, text 条目]]
data = _prepare_messages_data(messages, point_embeds)      # 过滤 image=None → 编码 → 附加点特征
outputs = _generate_response(data)                          # 贪心生成
output_ids = sequences[:, prompt_len:]                      # 切出生成段
output_text = batch_decode(output_ids, skip_special_tokens=False)
pred_qa_data[qa_level][i_key][i_task_type].append({question, gt_answer, pred_answer})
拼 full_mask → 二次前向 → query_token_list.append(query)
```

**多轮**（对每一轮 j）：

```text
if j == 0: content = [image 条目, PART_GLAMM_INSTRUCTION_IMG 格式文本]
else:      content = [裸 question 文本]                     # 不再带 <|point|>
messages.append(user: content)
data = _prepare_messages_data(messages, point_embeds)      # 注意：全历史重新编码
outputs = _generate_response(data)
generated_ids = sequences[:, prompt_len:]
output_text = batch_decode(...)[0]
messages.append(assistant: output_text)                    # 生成回复进入历史
结果列表.append({question, gt_answer, pred_answer, turn_idx: j})
拼 full_mask → 二次前向 → query_token_list.append(query)
循环结束后 pred_qa_data['multi_turn_qa_data'].append(结果列表)
```

#### 4.4.3 源码精读

**单轮的消息构建与解码切片**——[uniseg3d/query_decoder.py:2719-2743](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L2719-L2743)：

```python
question = self._get_text(i_qa_data['question'])
gt_answer = self._get_text(i_qa_data['answer'])
input_txt = PART_GLAMM_INSTRUCTION.format(question=question, point=self.point_token * n_points)

messages = [{'role': 'user', 'content': [{'type': 'image', 'image': image_path, ...},
                                         {'type': 'text', 'text': input_txt}]}]
...
output_ids = seq[:, data['input_ids'].shape[1]:]
output_text = self.processor.batch_decode(output_ids, skip_special_tokens=False)
```

三个细节：

- `_get_text` 容错处理 answer 为列表的数据（取首个）；`qa_level` 等三个键若被打成列表也先取 `[0]` 归一成字符串（[uniseg3d/query_decoder.py:2714-2717](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L2714-L2717)）。
- 切片基准是 `data['input_ids'].shape[1]`（prompt 的 token 长度），而不是文本长度——**始终在 token 坐标系里切**，规避了 u4-l3 遗留的「图像 token 展开导致文本/ token 错位」问题。
- `skip_special_tokens=False` 刻意保留 `<p>`、`</p>`、`<SEG>`、`<|im_end|>` 等特殊标记：u6-l3 的文本指标要靠 `<p>…</p>` 抽取指称、靠 `<SEG>` 判断是否接地；副作用是 `pred_answer` 末尾常挂着 `<|im_end|>` 字样，阅读 JSON 时属正常现象。

**三级键写入**——[uniseg3d/query_decoder.py:2746-2757](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L2746-L2757)：逐层 `setdefault` 式地建立 `qa_level → qa_key → qa_task_type → [记录, ...]` 的嵌套结构，每条记录只有 `question` / `gt_answer` / `pred_answer` 三个字段，**不含任何 mask**——文本通道与分割通道（`Part3DGlamm_SegMetric` 走 data sample）彼此独立，这是 u1-l4 已建立的全局认知在源码上的落点。

**多轮的滚动历史**——[uniseg3d/query_decoder.py:2810-2843](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L2810-L2843)：第 0 轮拼 `PART_GLAMM_INSTRUCTION_IMG`（点 token + 图像 + 问题），之后每轮仅追加纯文本问题；每轮生成后 `messages.append({'role': 'assistant', 'content': ...})` 把模型自己的回答写进历史，下一轮把全部历史重新编码再生成。注释里写明这是「标准 LLaVA 式 next-token 推理：无记忆模块、无检索、无架构改动」。每轮同样执行 4.3 的二次前向提取查询（[uniseg3d/query_decoder.py:2845-2856](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L2845-L2856)），最后整段对话的结果（每轮一条、带 `turn_idx`）作为一个元素 append 到 `pred_qa_data['multi_turn_qa_data']` 列表（[uniseg3d/query_decoder.py:2858](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L2858)）。

**查询列表的结构约定**：`query_token_list` 按「QA 顺序（多轮展开为轮次顺序）」append，场景循环末尾整体挂到 `query_tokens_concat_list` 上返回。下游 `forward_iter_pred` 的推理分支按「场景 × QA」两层循环消费它（[uniseg3d/query_decoder.py:3087-3098](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L3087-L3098)），每个查询独立过 6 层解码器——这与数据侧「GT 按 QA 逐条对齐」的形状契约（u2-l4）首尾呼应。

#### 4.4.4 代码实践：核对 preds JSON 的两种形态

**实践目标**：在真实推理产物上区分单轮（三级键 dict）与多轮（列表 + turn_idx）两种 JSON 形态，并把它们与源码一一对应。

**操作步骤**：

1. 按 u1-l4 的快速上手命令跑一次推理并显式打开写盘（关键是 `--cfg-options` 里带 `model.decoder.save_pred_qa_dir=preds`，否则会在写盘处抛 `TypeError`，见 4.1.3）。
2. 切换 `task_level` 各跑一次：

   ```bash
   # 单轮：part_qa_data
   --cfg-options pipeline.2.transforms.2.transforms.2.task_level=part_qa_data \
                  model.decoder.save_pred_qa_dir=preds_part
   # 多轮：multi_turn_qa_data
   --cfg-options pipeline.2.transforms.2.transforms.2.task_level=multi_turn_qa_data \
                  model.decoder.save_pred_qa_dir=preds_mt
   ```

3. 分别打开 `preds_part/` 与 `preds_mt/` 下的任一 `*.json`，对照源码核对结构。

**需要观察的现象**：

- `preds_part/*.json`：顶层键是 `qa_level`（如 `part_qa_data`），其下是 `qa_key`，再下是 `qa_task_type`（具体子任务名），最内层是记录数组，每条含 `question` / `gt_answer` / `pred_answer`；
- `preds_mt/*.json`：顶层只有一个键 `multi_turn_qa_data`，值是「对话列表的列表」，每轮记录多一个 `turn_idx` 字段且从 0 递增；
- `pred_answer` 中能看到 `<p>…</p><SEG>` 标记与结尾的 `<|im_end|>`（`skip_special_tokens=False` 所致）；
- 抽查 grounding 类子任务（如 `functional_part_grounding`）的回答，数一数 `<p>` 对数是否 ≤ 2（与 u4-l4「每 QA 至多 2 查询」对应）。

**预期结果**：两种 JSON 形态与 4.4.3 的源码逐字段吻合。本实践依赖 GPU 与预训练权重，**待本地验证**；若暂时没有环境，可先用下面这个零依赖模拟脚本生成同构 JSON 供结构对照（**示例代码**）：

```python
import json, os
pred_qa_data = {}
# 模拟两条单轮 QA 落进三级键
for key, q, a in [('functional_part_grounding', 'Where is the seat?', '<p>seat</p><SEG>'),
                  ('scale_estimation', 'How wide?', 'about 0.5 meters')]:
    pred_qa_data.setdefault('part_qa_data', {}) \
                 .setdefault('entry_0', {}) \
                 .setdefault(key, []).append(
                     {'question': q, 'gt_answer': a, 'pred_answer': a + '<|im_end|>'})
# 模拟一条 2 轮多轮对话
pred_qa_data['multi_turn_qa_data'] = [[
    {'question': 'Q1', 'gt_answer': 'A1', 'pred_answer': 'A1<|im_end|>', 'turn_idx': 0},
    {'question': 'Q2', 'gt_answer': 'A2', 'pred_answer': 'A2<|im_end|>', 'turn_idx': 1},
]]
os.makedirs('preds_sim', exist_ok=True)
with open('preds_sim/scene0000_00_frame_000000.json', 'w') as f:
    json.dump(pred_qa_data, f, indent=4)
```

#### 4.4.5 小练习与答案

**练习 1**：多轮对话第 2 轮的 user 消息里有 `<|point|>` 吗？图像条目呢？

**答案**：都没有。点 token 与图像只在第 0 轮注入（[uniseg3d/query_decoder.py:2814-2821](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L2814-L2821)）；后续轮只追加纯文本问题，点云信息依靠历史里第 0 轮的占位与注意力间接保留。

**练习 2**：单轮 QA 写 JSON 用三级键，这三级的取值来自哪里？

**答案**：来自数据管线。`QA_Generation_Test`/`QA_Generation_v2` 在采样时把 `qa_level`（part/object/multi_turn 三层）、`qa_key`（条目 id）、`qa_task_type`（9 类任务名）打进每条 QA（u2-l4）；推理端只是把它们原样拿出来当 JSON 键，同时这三层取值也正是 u1-l4 讲过的评估 `task_level` 旋钮的取值域。

**练习 3**：为什么单轮用 `PART_GLAMM_INSTRUCTION` 而多轮第 0 轮用 `PART_GLAMM_INSTRUCTION_IMG`，但单轮的 messages 里同样带了 image 条目？

**答案**：单轮模板的正文以点云措辞为主（并额外说明了 `<sentence>` trigger 的用法），但消息里仍附参考帧图像，视觉塔照常编码；多轮第 0 轮模板则在正文里明确提及「点云数据与给定图像」。两者都是「点云为主、图像为辅」的 Joint 设计（详见 u4-l6）；对 3D-only 权重，`image_dir=None` 使 `image_path=None`，`_prepare_messages_data` 会把这些图像条目整条过滤掉，文本模板措辞的差异不再影响输入。

## 5. 综合实践

**任务：为一次评估推理编写「执行计划书」。**

给定一个假想的评估场景 `scene0000_00_frame_001234`：超点数 1800，`selected_qa_data` 含 4 条单轮 QA（2 条 `functional_part_grounding`、1 条 `scale_estimation`、1 条 `existence_verification`）加 1 条 3 轮多轮对话。请完成：

1. **调用链抄写**：从 [uniseg3d/uniseg3d.py:1305](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/uniseg3d.py#L1305) 出发，手抄到 `_get_hidden_states_and_query_token` 为止的完整方法调用链，标出每条 QA 会重复经过的段落。
2. **成本核算**：计算该场景的完整 LLM 前向次数（答案：`(4 + 3) × 2 = 14` 次，其中多轮的 3 次生成输入逐轮变长）；说明为何点 token 数恒为 1800（推理不聚合，见 4.1.1）。
3. **JSON 结构图**：参照 4.4.4 的模拟脚本，写出该场景 `preds/scene0000_00_frame_001234.json` 的完整骨架（三层键 + 多轮列表各就各位；`existence_verification` 这类无 mask 任务照样进 JSON——文本通道不区分是否有分割 GT）。
4. **查询对账**：该场景 `query_token_list` 应有几个元素、各自形状如何？（答案：7 个元素按 QA/轮次顺序排列；两条 grounding 单轮按各自 `<p>` 对数给出 1~2 行、256 维之前的 hidden_size 维张量，`scale_estimation` 与 `existence_verification` 若无 `<p>` 则是 1 行零向量；多轮每轮同规则。）
5. **（可选，待本地验证）真机复算**：在有 GPU 的环境跑一次该配置，用 `ls preds/` 与 `jq` 抽查 JSON，核对第 3、4 步的预测。

## 6. 本讲小结

- 评估配置的解码器是 `Grounded_Decoder_Eval`（Joint 的孪生兄弟，差异在图像目录策略）；`infer_reason_eval` 是逐场景、逐 QA 的推理总调度，产出「查询列表 + 每场景一个预测 JSON」。
- 每条 QA（多轮则每轮）的成本是**两次完整 LLM 前向**：一次 `_generate_response` 贪心生成（`do_sample=False`、`repetition_penalty=1.2`、`max_new_tokens=1024`，autocast 半精度），一次 teacher-forcing 二次前向取最后一层隐状态。
- 二次前向是工程取舍：放弃结构繁琐的「逐步隐状态拼接」，把 prompt+生成段重组为完整序列（手工拼 `attention_mask`），`use_cache=False`，切片 `[:, -G:]` 对准生成段，再交给 `get_query_tokens_eval` 全局搜 `<p>…</p>`。
- 单轮 QA 独立成对话、用 `PART_GLAMM_INSTRUCTION` 模板、按三级键写入 JSON；多轮对话 LLaVA 式滚动历史，点 token 与图像只在第 0 轮注入，逐轮生成并逐轮提取查询，JSON 里是带 `turn_idx` 的列表。
- 推理时不做 FPS 聚合（`max_point_tokens` 只管训练）；`save_pred_qa_dir` 配置里默认没有，必须 `--cfg-options` 显式打开，否则写盘处抛 `TypeError`；JSON 写盘先于分割解码，且与 mIoU 通道相互独立。
- `skip_special_tokens=False` 保留 `<p>`/`<SEG>`/`<|im_end|>`，是文本指标（u6-l3）能工作的前提。

## 7. 下一步学习建议

- **u4-l6（2D 图像分支）**：本讲反复出现 `image_path=None` 的过滤路径，下一讲正面讲 Joint 与 3D-only 两种变体的三字段配置差异、`max_pixels` 约束与帧图像准备工具。
- **u5-l1（查询解码器逐层前向）**：本讲止步于 `query_token_list` 交出去的地方；6 层 Cross-Attn → Self-Attn → FFN 如何把查询变成逐超点 mask、训练/推理两条循环的差异，是下一单元的开篇。
- **源码延伸阅读**：对照 [uniseg3d/query_decoder.py:3152-3269](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L3152-L3269)（`Grounded_Decoder_Eval_MultiImg`，多视图推理如何复用本讲的生成与二次前向骨架）与 [uniseg3d/query_decoder.py:3272-3382](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L3272-L3382)（`Grounded_Decoder_Eval_Text`，注意它对「生成出多余 `<|point|>`」时的零查询兜底 L3344-3347），为 u7-l1 的迁移基准预热。
