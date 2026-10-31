# 点云 token 注入：PointQwen3VL 的实现技巧

## 1. 本讲目标

上一讲（u3-l3）结束时，我们已经得到每个场景的逐超点特征：形状为 `(超点数, 32)` 的张量，它「既作 LMM 的 point token 也作分割解码器的 K/V」。本讲解决前半个去向：**这批 3D 特征是怎么"塞进"Qwen3-VL 这个本来只会读文本和图像的语言模型的？**

读完本讲，你应该能够：

1. 解释 `add_point_embedding` 前向预钩子（forward pre-hook）修改 `inputs_embeds` 的原理——为什么挂钩子就能改掉模型输入、为什么返回 `((), kwargs)` 是关键一步。
2. 说明 `point_mask` 与 `point_token_id` 的对应关系：文本里写多少个 `<|point|>`，就注入多少行超点特征，这个「数量契约」由谁检查、在哪一步对齐。
3. 对比 `point_qwen3_vl.py` 与 `point_qwen2_5_vl.py` 两代实现：一个是「钩子叠加式」最小改造，一个是「手动重写 embedding 组装」的重改造，各自适合什么场景。

本讲全部代码实践都可以只用 PyTorch 完成，不需要下载 4B 模型权重。

## 2. 前置知识

### 2.1 input_ids 与 inputs_embeds：LLM 前向的两条入口

语言模型前向接受两种「输入表示」：

- `input_ids`：形状 `(B, L)` 的整数张量，每个整数是词表中的一个 token 编号。
- `inputs_embeds`：形状 `(B, L, D)` 的浮点张量，直接给出每个位置的向量。

二者只取其一。若只给 `input_ids`，模型内部会先查嵌入矩阵 `embed_tokens`：`inputs_embeds = embed_tokens(input_ids)`，把整数变成向量。**这意味着：如果在 `embed_tokens` 查表之后、Transformer 层计算之前，把某些位置的向量替换成我们自己的 3D 特征，模型就会"以为"那些位置是普通 token——这就是点云注入的全部原理。** 多模态模型（如 Qwen-VL 系列）接收图像，本质上也是同一招：文本里放一串 `<|image_pad|>` 占位 token，再把视觉特征散射（scatter）到这些位置。

### 2.2 PyTorch 的 forward pre-hook

`nn.Module.register_forward_pre_hook(fn)` 注册的函数会在该模块 `forward` 执行**之前**被调用：

```python
# 示例代码：pre-hook 的两种签名
def hook(module, args):                 # 不带 kwargs
    ...
def hook(module, args, kwargs):         # with_kwargs=True 时
    ...
    return (), modified_kwargs          # 返回非 None → 用返回值替换 (args, kwargs)
```

普通 pre-hook 只能"看"不能"改"；但传 `with_kwargs=True` 并**返回一个 `(args, kwargs) 元组**时，返回值会直接替换掉模块即将使用的输入。本讲的 `add_point_embedding` 正是靠这一机制改写 `inputs_embeds`。

### 2.3 masked_scatter：按掩码填充

`tensor.masked_scatter(mask, source)` 把 `source` 中的元素按**行优先顺序**逐个填进 `mask` 为 `True` 的位置，其余位置保持原值。本讲中 `inputs_embeds` 形状 `(1, L, D)`、掩码形状同为 `(1, L, D)`（每个 `<|point|>` 位置整行 `D` 维都为 True）、`point_embeds` 形状 `(N, D)`——恰好把第 `k` 个超点特征填进第 `k` 个 `<|point|>` 位置。

### 2.4 transformers 的 Auto 类注册

`AutoConfig` / `AutoModelForImageTextToText` / `AutoProcessor` 按 `config.model_type` 字符串查找对应类。我们定义 `model_type = 'point_qwen3_vl'` 并调用 `register`，之后 `from_pretrained` 就能识别这个"新模型"。这与 u1-l3 讲过的 mmdet3d 注册表思想同源：**字符串 → 类的映射，让配置系统能找到你的自定义实现**。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [uniseg3d/point_qwen3_vl.py](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/point_qwen3_vl.py) | 本讲主角：Qwen3-VL 的点云注入补丁（钩子 + 子类 + 注册），文件头标注改编自 Ye Liu 的 BSD-3-Clause 实现 |
| [uniseg3d/point_qwen2_5_vl.py](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/point_qwen2_5_vl.py) | 对照组：Qwen2.5-VL 时代的同类补丁，实现思路完全不同 |
| [uniseg3d/query_decoder.py](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py) | 消费方：`_init_reason_model` 完成加载/扩词表/注册特殊 token，`point2token` 投影，训练/推理分支把 `point_embeds` 塞进输入字典 |
| [uniseg3d/prompt_template.py](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/prompt_template.py) | 定义 `POINT_TOKEN`、`SEG_TOKEN` 常量与含 `{point}` 占位的指令模板 |
| [configs/ground3dlmm_train.py](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/configs/ground3dlmm_train.py) | 训练配置：`base_model='qwen3_vl'`、`qwen_model_path='Qwen/Qwen3-VL-4B-Instruct'`、`max_point_tokens=2000` 等 |

## 4. 核心概念与源码讲解

### 4.1 add_point_embedding 前向钩子

#### 4.1.1 概念说明

Qwen3-VL 是一个冻结的预训练模型（u4-l2 会讲 LoRA，本讲先记住"底座权重不动"）。让冻结的文本模型接收 3D 特征，最干净的办法不是复制粘贴几千行 `modeling_qwen3_vl.py` 再改，而是**在语言模型子模块（`language_model`）的入口处挂一个 pre-hook**：父类照常算好 `inputs_embeds`（包括图像特征的散射），钩子在最后一步把点云特征散射到 `<|point|>` 位置。模型主体代码零改动、升级 transformers 版本时的合并成本最小。

为什么挂在这个位置？因为到 `language_model` 这一层时，`inputs_embeds` 已经是"文本 + 图像"融合完毕的最终嵌入，钩子只需处理点云这一种模态，与图像注入互不干扰——这正是 Joint（3D+2D）变体可行的基础（u4-l6 展开）。

#### 4.1.2 核心流程

```text
decoder 调用 reason_model(input_ids, pixel_values, point_embeds, ...)
  └─ PointQwen3VLForConditionalGeneration.forward
       ├─ 重新计算 point_mask = (input_ids == config.point_token_id)
       └─ super().forward(..., point_embeds=…, point_mask=…)
            └─ 父类组装 inputs_embeds（embed_tokens 查表 + 图像特征散射）
            └─ 调用 self.model(..., point_embeds=…, point_mask=…, inputs_embeds=…)
                 └─ 调用 language_model(...)  ← 钩子在此之前触发
                      ├─ 1. 从 kwargs 中 pop 出 inputs_embeds / point_embeds / point_mask / input_ids
                      ├─ 2. 若 point_embeds 与 point_mask 均存在且序列长 > 1：
                      │     校验 point_mask 的 True 总数 == point_embeds 行数，否则抛 ValueError
                      │     把 point_mask (B,L) 扩展成 (B,L,D)
                      │     inputs_embeds = inputs_embeds.masked_scatter(mask, point_embeds)
                      ├─ 3. 训练模式下强制 inputs_embeds.requires_grad = True
                      └─ 4. 回写 kwargs['inputs_embeds']，返回 ((), kwargs) → 替换模块输入
```

用数学语言描述 `masked_scatter` 语义：设嵌入张量为 \(e \in \mathbb{R}^{B \times L \times D}\)，掩码 \(m \in \{0,1\}^{B \times L}\)，点特征 \(p \in \mathbb{R}^{N \times D}\)，\(k(b,i)\) 表示位置 \((b,i)\) 在全部掩码位置按行优先排序下的序号，则

\[
\text{out}[b,i,:] =
\begin{cases}
p_{\,k(b,i)} & m[b,i]=1 \\
e[b,i,:] & m[b,i]=0
\end{cases}
\qquad \text{要求 } \sum_{b,i} m[b,i] = N
\]

第 2 步的数量校验保证 \(k(b,i)\) 恰好取遍 \(0..N-1\)：不多（缺特征）、不少（越界）。

#### 4.1.3 源码精读

钩子本体，注意四个参数都是**从 kwargs 中 pop 出来**的（pop 意味着取出后从字典删除，`input_ids` 被摘掉不回传——既然已决定走 `inputs_embeds` 路径，就不让二者同时下发）：

[uniseg3d/point_qwen3_vl.py:L21-L35](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/point_qwen3_vl.py#L21-L35) —— 从 kwargs 弹出四个输入；当 `point_embeds`、`point_mask` 都存在且序列长度大于 1 时，先校验「掩码 True 数 == 特征行数」，再扩展掩码到嵌入维度并 `masked_scatter` 注入点特征。

[uniseg3d/point_qwen3_vl.py:L43-L49](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/point_qwen3_vl.py#L43-L49) —— 训练模式下强制 `inputs_embeds.requires_grad = True`（防止 `embed_tokens` 被冻结后整个嵌入张量不带梯度，导致 `point_embeds` 的梯度断流）；最后**把修改后的 `inputs_embeds` 写回 kwargs 并返回 `((), kwargs)`**——这是 pre-hook 能真正改掉模块输入的关键一步，缺了它钩子只是白算一遍。

钩子的注册位置在构造函数最后一行（`with_kwargs=True` 让钩子能拿到并改写关键字参数）：

[uniseg3d/point_qwen3_vl.py:L70-L82](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/point_qwen3_vl.py#L70-L82) —— 构造函数：先走父类初始化，再替换 `self.model` 为 `PointQwen3VLModel`、重建 `lm_head`；随后注册三个 pre-hook：`merger.linear_fc1` 保存视觉 token（`visual_tokens`）、语言模型最终 `norm` 保存最后一层隐状态（`last_hidden_state`）、`language_model` 挂上 `add_point_embedding`。

一个诚实的技术注释：`save_hidden_state` 捕获的两个属性（`visual_tokens`、`last_hidden_state`）在当前 `query_decoder.py` 中**并没有被读取**（全仓 grep 无 `.visual_tokens` / `.last_hidden_state` 消费点），解码器实际用的是模型返回值里的 `output.hidden_states[-1]`（如 [uniseg3d/query_decoder.py:L1433](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L1433)）。这两个钩子更像上游实现留下的便捷通道，读代码时不要误以为它们是 `<SEG>` 查询的来源。

`inputs_embeds.shape[1] > 1` 这个条件的作用：自回归生成的第 1 步之后，每步前向只有 1 个新 token（序列长为 1），不可能包含成片的 `<|point|>`，跳过注入是安全的双重保险（另一重见 4.3 的 `prepare_inputs_for_generation`）。

#### 4.1.4 代码实践

**实践目标**：用 30 行纯 PyTorch 代码复现 `masked_scatter` 注入语义，验证「掩码位置的向量被精确替换、其余位置原样保留」。

**操作步骤**：

1. 新建脚本 `replay_hook.py`（放在仓库外的任意目录，不要写进项目源码树）：

```python
# 示例代码：replay_hook.py
import torch

torch.manual_seed(0)
B, L, D, N = 1, 12, 8, 3          # 1 条样本、12 个 token、8 维嵌入、3 个超点
point_token_id = 151665            # 模拟 <|point|> 的 token id（仅示意）

input_ids = torch.randint(0, 1000, (B, L))
input_ids[0, [2, 5, 9]] = point_token_id          # 手工放置 3 个占位 token
inputs_embeds = torch.randn(B, L, D)               # 模拟 embed_tokens 查表结果
point_embeds = torch.randn(N, D)                   # 模拟 point2token 输出

point_mask = input_ids == point_token_id           # (B, L) 布尔掩码
assert point_mask.sum() == N, "数量契约被破坏"

# ↓↓↓ 逐行复刻 add_point_embedding 的核心三行 ↓↓↓
mask_expanded = point_mask.unsqueeze(-1).expand_as(inputs_embeds)
out = inputs_embeds.masked_scatter(mask_expanded, point_embeds)

# 验证 1：占位位置 == point_embeds（按行优先顺序）
assert torch.equal(out[0, 2], point_embeds[0])
assert torch.equal(out[0, 5], point_embeds[1])
assert torch.equal(out[0, 9], point_embeds[2])
# 验证 2：非占位位置原样保留
keep = ~mask_expanded
assert torch.equal(out[keep], inputs_embeds[keep])
print("all assertions passed")
```

2. 运行 `python replay_hook.py`。

**需要观察的现象**：断言全部通过，没有任何 `RuntimeError`（掩码元素数与源元素数不一致时 `masked_scatter` 才会报错）。

**预期结果**：打印 `all assertions passed`。第 2、5、9 位的向量被顺序替换为 `point_embeds` 的三行，其余位置逐元素相等。（待本地验证）

**附加实验**：把 `input_ids[0, [2, 5, 9]]` 改成只放 2 个占位 token 再运行，观察 `assert point_mask.sum() == N` 先于 `masked_scatter` 报错——这正是源码中 [L27-L28](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/point_qwen3_vl.py#L27-L28) 那个 `ValueError` 存在的意义：把「数量不匹配」从 PyTorch 底层的模糊报错变成带数值的诊断信息。

#### 4.1.5 小练习与答案

**练习 1**：如果钩子最后忘记 `return (), kwargs`，会发生什么？

**答案**：pre-hook 返回 `None` 时 PyTorch 视为"不修改输入"，`masked_scatter` 的结果被丢弃，`language_model` 仍用原始 `inputs_embeds` 前向——模型正常跑、不报错，但点云信息完全没进模型，训练上表现为语言损失对 3D 输入不再敏感。这是最隐蔽的一类 bug。

**练习 2**：为什么 `point_embeds` 要先 `.to(inputs_embeds.device, inputs_embeds.dtype)` 再 scatter？

**答案**：模型以 `torch_dtype=torch.bfloat16` 加载（见 [uniseg3d/query_decoder.py:L971](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L971)），而 `point2token` 投影（u4-l2 详述）可能以其他精度/设备计算；`masked_scatter` 要求源与目标同设备同 dtype，否则直接抛错。这行是把「解码器侧 3D 世界」对齐到「LLM 侧世界」的类型边界转换。

**练习 3**：钩子里为什么判断 `model.training`（外层模型）而不是 `module.training`？

**答案**：钩子签名是 `add_point_embedding(model, module, args, kwargs)`，其中 `model` 是 `partial(add_point_embedding, self)` 绑定的外层 `PointQwen3VLForConditionalGeneration` 实例。用它判断训练态语义更直接；实践中外层与 `language_model` 子模块的训练标志由 `model.train()/eval()` 递归设置，通常一致，写成 `module.training` 也能工作，但外层实例是钩子闭包里最可靠的状态来源。

### 4.2 `<|point|>` 特殊 token 注册与词表扩容

#### 4.2.1 概念说明

注入要有"坑位"：`inputs_embeds` 里哪些位置属于点云？答案由文本决定。项目在指令模板里写 `{point}` 占位符，运行时用 `"<|point|>" * 超点数` 填充——于是问题变成：**tokenizer 如何保证 `<|point|>` 被切成恰好一个 token，而不是被当成普通文本碎成多个？**

答案是 `add_special_tokens`：注册为特殊 token 后，分词器按"整串匹配"处理它，一个 `<|point|>` 对应一个 `point_token_id`。特殊 token 会占用新的词表编号，因此嵌入矩阵也要 `resize_token_embeddings` 扩容，新行用什么值初始化则直接影响训练起点——这里用了"全体旧嵌入的均值"这一温和初始化。

#### 4.2.2 核心流程

```text
_init_reason_model（构造解码器时执行一次）
  ├─ 1. from_pretrained 加载 PointQwen3VLForConditionalGeneration + Config + Processor
  ├─ 2. reason_model.requires_grad_(False)          # 底座全冻结（LoRA 之后另加）
  ├─ 3. tokenizer.add_special_tokens(['<p>','</p>','<SEG>','<name>','<sentence>','<|point|>'])
  ├─ 4. 若新增了 token：resize_token_embeddings(len(tokenizer))
  │      新增行（输入嵌入与输出 lm_head 两份）← 旧嵌入的均值
  ├─ 5. point_token_id = tokenizer.convert_tokens_to_ids('<|point|>')
  └─ 6. config.point_token_id = point_token_id      # 写回配置，forward 里要用

运行时（数量契约闭环）：
  n_points = 超点数（训练超限时先经 FPS 聚合降采样）
  文本模板填充 point = '<|point|>' * n_points   → n_points 个占位
  point_embeds = point2token(超点特征)           → (n_points, hidden_size)
  钩子校验 point_mask.sum() == point_embeds.shape[0]
```

#### 4.2.3 源码精读

常量定义——注意 `SEG_TOKEN` 里的 `<p>`/`<SEG>` 等与 `<|point|>` 一起注册，一次性扩容：

[uniseg3d/prompt_template.py:L3-L5](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/prompt_template.py#L3-L5) —— 定义 `POINT_TOKEN = ["<|point|>"]` 与 5 个分割/命名用特殊 token，是词表扩容的唯一来源。

[uniseg3d/prompt_template.py:L36-L43](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/prompt_template.py#L36-L43) —— 3D-only 指令模板 `PART_GLAMM_INSTRUCTION`：`{point}` 占位符在正文中被替换为成串的 `<|point|>`（L41），同时说明 `<p>名字</p><SEG>` 的回答格式约定。

扩容与初始化的完整实现：

[uniseg3d/query_decoder.py:L978-L996](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L978-L996) —— 先整体冻结底座；调用 `add_special_tokens` 注册 6 个特殊 token；若有新增且词表变长，则 `resize_token_embeddings` 扩容，并把输入嵌入与输出嵌入的**最后 `new_tokens` 行都初始化为旧嵌入的逐行均值**（均值初始化让新 token 从"词表平均语义"起步，避免随机值破坏冻结模型的行为）；最后把 `<|point|>` 换算成 id，同时存到 `self.point_token_id` 与 `config.point_token_id` 两处。

文本侧的占位符生成（训练分支首轮消息）：

[uniseg3d/query_decoder.py:L1395-L1400](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L1395-L1400) —— 首轮 user 消息用 `PART_GLAMM_INSTRUCTION_IMG.format(question=question, point=self.point_token * n_points)` 填充：字符串乘法把 `<|point|>` 重复 `n_points` 次。`n_points` 来自经 FPS 聚合后的超点数（[L1360-L1368](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L1360-L1368)，`max_point_tokens` 配置为 2000），与 `point_embeds = self.point2token(i_point_feat)`（[L1370](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L1370)）的行数严格一致。

编码后把点特征与掩码挂到输入字典上：

[uniseg3d/query_decoder.py:L1420-L1425](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L1420-L1425) —— `apply_chat_template` + `processor(...)` 编码出 `input_ids` 后，向 `data` 附加 `point_embeds` 与 `point_mask = (input_ids == self.point_token_id)`；推理分支的同一逻辑见 [uniseg3d/query_decoder.py:L1486-L1490](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L1486-L1490)。

一个容易误读的细节：`PointQwen3VLForConditionalGeneration.forward` 收到 `point_mask` 参数后**并不使用它**，而是自己重算——[uniseg3d/point_qwen3_vl.py:L125](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/point_qwen3_vl.py#L125) 用 `input_ids == self.config.point_token_id` 作为下发掩码（`config.point_token_id` 正是上面 L996 写回的）。也就是说 qwen3 路径中解码器侧的 `data['point_mask']` 是冗余但无害的，模型内部以 `input_ids` 为唯一事实来源——这一设计让掩码不可能与实际输入错位。

#### 4.2.4 代码实践

**实践目标**：源码阅读型实践——画出从「常量定义」到「钩子校验通过」的数量契约链，并回答：如果某场景有 3500 个超点，训练时编码进文本的 `<|point|>` 有多少个？

**操作步骤**：

1. 依次打开三处代码并抄下关键行：[prompt_template.py:L3](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/prompt_template.py#L3)、[query_decoder.py:L1360-L1370](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L1360-L1370)、[query_decoder.py:L984-L987](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L984-L987)。
2. 画出链条：`POINT_TOKEN 常量 → add_special_tokens → resize_token_embeddings → convert_tokens_to_ids → point_token * n_points → input_ids → 钩子校验`。
3. 回答上述 3500 超点问题，并用配置 [configs/ground3dlmm_train.py:L76](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/configs/ground3dlmm_train.py#L76) 佐证。

**需要观察的现象**：链条上每一步的数量（词表增量、token id、占位符个数、特征行数）都能一一对应，没有一步靠"猜"。

**预期结果**：3500 > `max_point_tokens=2000`，训练时先经 FPS 聚合（有位置信息走 `pos_aggregator`，否则 `point_aggregator`）降到 2000，再生成 2000 个 `<|point|>` 占位与 2000 行 `point_embeds`；词表扩容只发生一次（新增 6 个 token），与超点数无关。

**可选上机实验（需要联网拉取 Qwen tokenizer，无需模型权重）**：`AutoTokenizer.from_pretrained('Qwen/Qwen3-VL-4B-Instruct')` 后调用 `add_special_tokens({'additional_special_tokens': ['<|point|>']})`，检查 `convert_tokens_to_ids('<|point|>')` 返回值变化、`len(tokenizer)` 增量，以及编码 `"<|point|>"*3` 得到的 id 序列长度是否恰为 3。**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：为什么新 token 的嵌入初始化用均值而不是 `torch.randn`？

**答案**：底座已冻结（除 LoRA 与 `embed_tokens`/`lm_head`，见 u4-l2），随机大幅向量作为输入会让第一层注意力立刻产生异常激活，语言损失初期混乱；均值嵌入位于原词表分布的中心，前向输出接近"平均语义"，训练从平稳的起点逐步把 `<|point|>`（的 LoRA 邻域）拉向 3D 语义。

**练习 2**：`resize_token_embeddings` 之后，为什么输入嵌入和输出嵌入（`lm_head`）都要初始化新行？

**答案**：新 token 既要被"读"（输入嵌入：出现在 prompt 里）也要被"写"（输出嵌入：模型可能在回答中生成 `<p>`、`<SEG>` 等新 token，logits 由 `lm_head` 给出）。两份矩阵是不同的参数（未 tied 时），只初始化一份会造成读写出起点不对称——例如模型永远倾向于不生成 `<SEG>`，直接影响 u4-l4 要讲的查询提取。

**练习 3**：`convert_tokens_to_ids` 对未注册的 token 返回什么？这在本项目里会引发什么连锁反应？

**答案**：返回 unk token 的 id（如 `<|endoftext|>` 类未知标记，具体取决于 tokenizer 配置）。若 `<|point|>` 忘记注册，成串占位文本会被碎成多个普通子词 token，`input_ids == point_token_id` 的掩码几乎全 False，`point_mask.sum()` 与 `point_embeds.shape[0]` 不再相等——钩子里的 `ValueError`（L27-L28）立刻暴露问题；更糟的情况是恰好相等（数量巧合），此时点特征被散射到错误位置、训练静默退化。这也是 4.1 校验存在的兜底价值。

### 4.3 PointQwen3VLForConditionalGeneration 的组装与注册

#### 4.3.1 概念说明

`point_qwen3_vl.py` 的其余部分回答两个问题：**子类化了什么**、**怎么让 transformers 认识它**。策略是"三件套 + 最小覆写"：`PointQwen3VLConfig`（声明新 `model_type`）、`PointQwen3VLModel`（指定 config 类）、`PointQwen3VLForConditionalGeneration`（挂钩子、接收新参数）；最后把三者注册进 Auto 体系，`from_pretrained` 即可加载。覆写的 `forward` 只做三件事：搬设备、重算 `point_mask`、把点参数透传给父类，其余逻辑全部复用上游——这正是 u7-l3 要评价的"钩子叠加式"改造风格。

#### 4.3.2 核心流程

```text
注册阶段（import uniseg3d.query_decoder 时随 import 副作用执行）
  MODEL_FOR_VISION_2_SEQ_MAPPING_NAMES['point_qwen3_vl'] = 'PointQwen3VLForConditionalGeneration'
  AutoConfig.register(...) / AutoModelForImageTextToText.register(...) / AutoProcessor.register(...)

训练前向（decoder 调 reason_model(**data, labels=..., output_hidden_states=True)）
  forward(input_ids, pixel_values, point_embeds, point_mask, ...)
    ├─ input_ids 搬到 embed_tokens 所在设备（兼容 auto device map）
    ├─ point_mask = input_ids == config.point_token_id   # 内部重算，事实来源唯一
    ├─ use_cache = not self.training                     # 训练关 KV cache
    └─ super().forward(..., point_embeds=…, point_mask=…, **kwargs)
         └─ 父类组装 inputs_embeds（含图像散射）→ language_model → 钩子注入点特征

生成阶段（reason_model.generate(**data, ...)）
  prepare_inputs_for_generation(..., cache_position=…)
    ├─ 首步（cache_position[0]==0）：透传 point_embeds / point_mask
    └─ 后续步：置 None → 钩子条件不满足，自动跳过注入
```

#### 4.3.3 源码精读

前向签名与透传——`point_embeds`、`point_mask` 是新增的两个参数，其余与上游一致：

[uniseg3d/point_qwen3_vl.py:L84-L129](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/point_qwen3_vl.py#L84-L129) —— `forward` 先把 `input_ids` 移到嵌入矩阵所在设备（自动 device map 下二者可能异卡）；随后调用父类前向，其中两处定制：`use_cache=not self.training`（训练时关闭 KV 缓存，保证梯度与显存行为可控）与 `point_mask=input_ids == self.config.point_token_id`（忽略调用方传入的掩码、以 `input_ids` 重算，见 4.2.3 的讨论）。

生成期间的注入节流：

[uniseg3d/point_qwen3_vl.py:L131-L143](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/point_qwen3_vl.py#L131-L143) —— `prepare_inputs_for_generation` 在 `cache_position[0] == 0`（预填充步）时透传 `point_embeds` 与 `point_mask`，其余解码步一律置 `None`；与钩子里的 `shape[1] > 1` 条件构成双重保险，确保点云只在预填充时注入一次，后续每步生成不再携带 3D 特征。

Auto 体系注册三连：

[uniseg3d/point_qwen3_vl.py:L146-L151](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/point_qwen3_vl.py#L146-L151) —— 手工把新 `model_type` 写进 `MODEL_FOR_VISION_2_SEQ_MAPPING_NAMES`，并注册 `AutoConfig`、`AutoModelForImageTextToText`、`AutoProcessor`；此后 [query_decoder.py:L971-L973](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L971-L973) 的 `PointQwen3VLForConditionalGeneration.from_pretrained('Qwen/Qwen3-VL-4B-Instruct', ...)` 就能从普通 Qwen3-VL 权重目录加载出带钩子的模型，`PatchedQwen3VLProcessor` 则缓存原始文本供 `preprocess_chatml`（u4-l3）使用。

Processor 小补丁：

[uniseg3d/point_qwen3_vl.py:L52-L56](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/point_qwen3_vl.py#L52-L56) —— `PatchedQwen3VLProcessor` 只重写 `_check_special_mm_tokens`，把处理过的文本存进 `self.cache_text` 再调父类；这是一条"旁路记录"，供后续逻辑拿到 processor 编码前的原始文本。

另可顺带留意：文件顶部 `random`、`hydra`、`nncore` 等 import 在本文件中并未使用（[L3-L15](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/point_qwen3_vl.py#L3-L15)），它们是这份代码从上游实现（文件头注明 Ye Liu 的 BSD-3-Clause 许可）移植时留下的痕迹——读开源代码时，无用的 import 常常是"出身"的线索。

#### 4.3.4 代码实践

**实践目标**：用玩具模块验证 `with_kwargs=True` 的 pre-hook 确实能改写模块收到的 kwargs——这是整个注入方案的机制基石。

**操作步骤**：

1. 新建 `hook_mechanism.py`（仓库外）：

```python
# 示例代码：hook_mechanism.py
import torch.nn as nn

class Toy(nn.Module):
    def forward(self, inputs_embeds=None, point_embeds=None, **kw):
        return inputs_embeds          # 模拟 language_model：只消费 inputs_embeds

def add_point_embedding(model, module, args, kwargs):
    inputs_embeds, point_embeds = (
        kwargs.pop(k, None) for k in ('inputs_embeds', 'point_embeds'))
    if point_embeds is not None:
        inputs_embeds = inputs_embeds + point_embeds.sum()   # 简化版"注入"
    kwargs['inputs_embeds'] = inputs_embeds
    return (), kwargs                                          # 关键：返回替换值

toy = Toy()
toy.register_forward_pre_hook(add_point_embedding, with_kwargs=True)

base, extra = 10.0, 5.0
out = toy(inputs_embeds=base, point_embeds=extra)  # point_embeds 会被 pop 掉，不会传进 forward
print("output:", out)
```

2. 运行 `python hook_mechanism.py`，记下输出。
3. 把 `return (), kwargs` 改为 `return None`（或不写 return），再运行对比。
4. 恢复代码，把 `with_kwargs=True` 删掉再运行，观察报错。

**需要观察的现象**：第 2 步输出 `15.0`（注入生效）；第 3 步输出 `10.0`（注入被静默丢弃）；第 4 步钩子签名不匹配，抛 `TypeError`。

**预期结果**：与上述三处描述一致；特别体会第 3 步——**不返回替换值时程序完全正常，只是改动没生效**，对应 4.1 练习 1 的隐患。（待本地验证）

#### 4.3.5 小练习与答案

**练习 1**：为什么 `forward` 里要写 `use_cache=not self.training`，而不是沿用调用方给的 `use_cache`？

**答案**：训练时整段序列一次性前向并需要完整隐状态计算损失，KV 缓存既无意义又占显存（长序列下缓存可观数 GB），所以训练强制关闭；推理生成则必须开缓存否则每步重算全序列。在模型内部固化这一策略，避免调用方（训练循环/生成循环）忘记传对。

**练习 2**：`AutoModelForImageTextToText.register` 与手工改 `MODEL_FOR_VISION_2_SEQ_MAPPING_NAMES` 为什么都做？只做后者行不行？

**答案**：`register` 让 `Auto*` 类的注册表能按 config 类找到实现；`MODEL_FOR_VISION_2_SEQ_MAPPING_NAMES` 是 transformers 内部（如 `_from_config`/自动映射路径）使用的另一张映射表。只改一张可能在某些加载路径（如框架内部按 model_type 反查）返回原生 `Qwen3VLForConditionalGeneration`——没有钩子的版本，注入静默失效。两张都写是防御性做法。

**练习 3**：生成阶段如果把 `point_embeds` 在所有解码步都透传（即去掉 `cache_position[0] == 0` 判断），会发生什么？

**答案**：预填充步之后的每步序列长度为 1，`point_mask.sum() == 0` 而 `point_embeds.shape[0] == N`，钩子里的数量校验抛 `ValueError`，生成中断。该判断与钩子的 `shape[1] > 1` 条件一起把注入严格限制在预填充一步。

### 4.4 对比 point_qwen2_5_vl：两代实现的差异

#### 4.4.1 概念说明

`point_qwen2_5_vl.py` 是同一目标的上一代实现，配置里 `base_model='qwen2_5_vl'` 分支（[query_decoder.py:L961-L969](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L961-L969)）仍在维护。它代表另一种工程取舍：**不用钩子，直接在子类 `forward` 里手动重写整个 embedding 组装流水线**（查表、图像散射、视频散射、点散射全部自己来），代价是必须放弃视觉编码器（`self.visual = None`），换来对输入组装的完全控制。对比两份文件能看清"最小侵入"与"完全接管"两种改造路线。

#### 4.4.2 核心流程

```text
point_qwen2_5_vl 前向（无钩子）
  mode = training / caching（首次无 KV）/ generating（有 KV 的生成步）
  if inputs_embeds is None:
      inputs_embeds = embed_tokens(input_ids)
      if pixel_values:      图像特征 → masked_scatter 到 <|image_pad|> 位置（自带 assert 校验）
      if pixel_values_videos: 视频特征 → 同法散射
      if point_embeds:
          if mode in (training, caching): 数量校验 → masked_scatter 到 <|point|> 位置
          elif mode == generating:       跳过（生成步不含占位 token）
  训练态强制 requires_grad
  super().forward(inputs_embeds=…, …)      # 不再传 point_embeds 给父类
  if mode in (training, caching): self.hidden_cache = outputs.hidden_states
```

#### 4.4.3 源码精读

视觉编码器被显式丢弃：

[uniseg3d/point_qwen2_5_vl.py:L37-L46](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/point_qwen2_5_vl.py#L37-L46) —— `PointQwen2_5_VLModel.__init__` 把 `self.visual = None`（注释注明"visual encoder is not used for now"），语言模型改由 `Qwen2_5_VLTextModel._from_config` 构建；因此该路线天然只有 3D 输入，图像散射代码虽在（见下）却因无视觉塔而无从产生特征。

手动组装 embedding 的完整流水线：

[uniseg3d/point_qwen2_5_vl.py:L84-L137](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/point_qwen2_5_vl.py#L84-L137) —— 用 `past_key_values` 是否为空把运行状态判为 `training/caching/generating` 三态；`inputs_embeds is None` 时自行查表，依次对图像（L92-L105）、视频（L107-L120）、点云（L122-L137）做与 4.1 相同的「数量校验 → 扩展掩码 → masked_scatter」，但全部写在 `forward` 里而非钩子里；点注入仅在 `training/caching` 两态发生。

`hidden_cache` 缓存与训练态梯度保障：

[uniseg3d/point_qwen2_5_vl.py:L139-L165](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/point_qwen2_5_vl.py#L139-L165) —— 同样保留"训练态强制 `requires_grad`"的防御（L140-L141），调用父类前向后把 `hidden_states` 存入 `self.hidden_cache`（L162-L163），供外部按需读取隐状态——这是无钩子路线下获取中间结果的替代方案。

生成参数透传的差异：

[uniseg3d/point_qwen2_5_vl.py:L175-L185](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/point_qwen2_5_vl.py#L175-L185) —— `prepare_inputs_for_generation` 无条件透传 `point_embeds`（不像 qwen3 版只在首步传），靠 `forward` 内的三态判断避免重复注入——同一目标（只注入一次）的两种实现位置：qwen3 放在"参数供给端"，qwen2_5 放在"消费端状态机"。

注册方式：

[uniseg3d/point_qwen2_5_vl.py:L188-L193](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/point_qwen2_5_vl.py#L188-L193) —— 同样手工注册 `MODEL_FOR_VISION_2_SEQ_MAPPING_NAMES`，但 Auto 入口用的是 `AutoModel.register`（qwen3 版为 `AutoModelForImageTextToText.register`），反映两代 transformers API 的演进。

两版差异速查表：

| 维度 | point_qwen3_vl（本项目主力） | point_qwen2_5_vl（备选底座） |
| --- | --- | --- |
| 注入位置 | `language_model` 的 forward pre-hook | 子类 `forward` 内联重写 |
| 视觉输入 | 保留，父类处理图像散射，3D/2D 可共存（Joint 变体基础） | `self.visual = None`，仅 3D |
| `point_mask` 来源 | 模型内 `input_ids == config.point_token_id` 重算 | `forward` 内 `input_ids == config.point_token_id` |
| 生成期防重复注入 | `prepare_inputs_for_generation` 首步后才传 + 钩子 `shape[1] > 1` | `training/caching/generating` 三态判断 |
| lm_head | 子类重建 `nn.Linear`（L74） | 沿用父类 |
| 隐状态获取 | `save_hidden_state` 钩子存属性（当前未被消费） | `self.hidden_cache` 存 `hidden_states` |
| Auto 注册 | `AutoModelForImageTextToText` | `AutoModel` |

#### 4.4.4 代码实践

**实践目标**：源码阅读型实践——用「同一问题，两种解法」的清单法精读两份文件，训练对比式代码阅读能力。

**操作步骤**：

1. 并排打开 [point_qwen3_vl.py:L21-L49](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/point_qwen3_vl.py#L21-L49) 与 [point_qwen2_5_vl.py:L88-L137](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/point_qwen2_5_vl.py#L88-L137)。
2. 对下面 5 个问题在两份代码中各找到对应行号：① 数量校验在哪；② masked_scatter 在哪；③ 训练态 requires_grad 保障在哪；④ 生成步如何跳过注入；⑤ 视觉特征散射在哪。
3. 核对 4.4.3 的速查表，找出表中任何一条与你观察不符的地方（以源码为准）。

**需要观察的现象**：`masked_scatter` 那三行（扩展掩码 → 对齐设备与 dtype → 散射）在两份文件里几乎逐字相同——**被复用的核心算法不变，变的是它的安放位置**。

**预期结果**：① qwen3 在钩子 L27-L28、qwen2_5 在 L124-L127；② qwen3 L30-L35、qwen2_5 L129-L135；③ qwen3 L43-L44、qwen2_5 L140-L141；④ qwen3 靠 L139（首步判断）+ L25（长度判断）、qwen2_5 靠 L123/L136 的三态；⑤ qwen3 版没有（由父类完成），qwen2_5 版在 L92-L105。

#### 4.4.5 小练习与答案

**练习 1**：为什么 Joint（3D+2D）变体必须建立在 qwen3 路线上？

**答案**：Joint 需要点云与图像特征同时进入序列，各自占据 `<|point|>` 与图像 token 位置。qwen3 版保留视觉塔并让父类完成图像散射，钩子只在其结果上叠加点注入，两模态天然兼容；qwen2_5 版 `self.visual = None` 丢弃了视觉编码器，图像路径不存在，只能做 3D-only。

**练习 2**：qwen2_5 版把注入逻辑写进 `forward` 有什么好处和代价？

**答案**：好处是对输入组装全流程可见、行为完全确定、不依赖钩子语义（旧版 transformers 钩子 API 不稳定时更保险），且能用三态机精细控制 caching/generating。代价是代码与上游 `forward` 强耦合——上游一改（如新增模态、改 KV 接口）就要手工同步，且为避开视觉塔被迫放弃 2D；qwen3 版钩子路线则几乎零耦合，升级 transformers 时代价小得多。

**练习 3**：两份文件都保留 `if self.training and not inputs_embeds.requires_grad: inputs_embeds.requires_grad = True`，这行防御为什么在两条不同路线里都不能省？

**答案**：两条路线都冻结了底座（`requires_grad_(False)`，u4-l2）。若 `embed_tokens` 冻结，查表得到的 `inputs_embeds` 默认不追踪梯度，而点特征是从它"散射覆盖"进去的——上游张量不带梯度时链式法则无法把损失传回 `point2token` 与 3D 主干。显式置 `requires_grad = True` 把注入点变成叶子级的可微入口，是两条路线共同的生命线。

## 5. 综合实践

**任务：编写一个 60 行以内的「点云注入回放器」，把本讲三个机制串起来。**

回放器要在纯 PyTorch（不加载任何模型权重）环境中完整模拟「文本占位 → 词表扩容 → 嵌入查表 → 钩子注入 → 校验」链路：

1. **模拟词表与扩容**：建一个 `(V, D)` 的嵌入矩阵；模拟 `add_special_tokens` 把 `point_token_id = V`（新编号），`resize_token_embeddings` 后矩阵变为 `(V+1, D)`，新行按源码语义用「旧嵌入均值」初始化（对应 [query_decoder.py:L988-L991](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L988-L991)）。
2. **模拟文本编码**：构造 `input_ids`，把 `n_points` 个位置设为 `point_token_id`（对应 `point_token * n_points` 与 [L1398-L1399](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L1398-L1399)）。
3. **挂真钩子**：定义一个 `Toy(nn.Module)`，其 `forward` 原样返回 `inputs_embeds`；用 4.1 精读的**原版 `add_point_embedding` 逻辑**（可整段照抄，去掉 print）注册 `with_kwargs=True` 的 pre-hook（对应 [point_qwen3_vl.py:L80](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/point_qwen3_vl.py#L80)）。
4. **注入并断言**：`point_embeds = randn(n_points, D)`，调用玩具模型，断言 (a) 所有 `point_token_id` 位置的输出等于 `point_embeds` 对应行；(b) 其余位置等于查表结果；(c) 新 token 行初始化等于旧嵌入均值。
5. **破坏性测试**：把 `point_embeds` 减少一行再调用，确认钩子抛出带两个数值的 `ValueError`（对应 [L27-L28](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/point_qwen3_vl.py#L27-L28)）。

**预期结果**：正常路径所有断言通过；破坏路径报 `ValueError`，报错信息形如 `point_mask.sum(N-1) != n_point_features(N)`。完成后你就拥有了一个可断点调试的最小沙盘——之后阅读 u4-l3（训练分支如何组织 messages）与 u4-l5（生成 + 二次前向）时，任何关于「点特征到底进没进模型」的疑惑都可以回到这个沙盘验证。**待本地验证**（脚本涉及随机张量，具体数值每次不同，断言行为应稳定成立）。

## 6. 本讲小结

- 点云注入的本质是把 `point2token` 投影后的超点特征 `(N, hidden_size)` 用 `masked_scatter` 覆盖到 `inputs_embeds` 中 `<|point|>` 占位 token 的位置——与多模态 LLM 接收图像是同一机制。
- `add_point_embedding` 是挂在 `language_model` 上的 forward pre-hook：从 kwargs 弹出输入、校验「掩码 True 数 == 特征行数」、散射、回写 `inputs_embeds` 并返回 `((), kwargs)` 完成输入替换；`requires_grad` 保障是被冻结底座下梯度回传的生命线。
- 数量契约闭环：`add_special_tokens` 注册 `<|point|>`（一个 token 一个 id）→ 模板填充 `point_token * n_points` → `point_embeds` 行数同为 `n_points`（超限时先经 FPS 聚合到 `max_point_tokens`）→ 钩子校验兜底；词表随之扩容，新 token 行以旧嵌入均值初始化。
- `PointQwen3VLForConditionalGeneration` 采用"最小覆写 + Auto 注册"：`forward` 只重算 `point_mask`、固化 `use_cache`、透传点参数；`prepare_inputs_for_generation` 保证只在预填充步注入。
- 与 `point_qwen2_5_vl` 的路线之争：钩子叠加式（保留视觉塔、3D+2D 共存、零耦合）vs 前向重写式（三态状态机、无视觉塔、全流程可控）——Joint 变体只能走前者。

## 7. 下一步学习建议

本讲解决了「3D 特征进入 LLM」的问题，下一讲 **u4-l2（Grounded_Decoder_Joint 初始化与 LoRA 配置）** 从解码器视角补齐另一半：`point2token` 投影层的具体结构（RMSNorm + GELU + 两级线性层如何把 32 维超点特征映射到 `hidden_size`）、`get_peft_model` 的 LoRA 注入细节，以及 `embed_tokens`/`lm_head` 为何在冻结底座中被单独解冻。建议顺带精读的源码：[uniseg3d/query_decoder.py:L959-L1014](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L959-L1014)（`_init_reason_model` 全文），以及为 u4-l3 预热的 [preprocess_chatml 调用点](https://github.com/AmolHarsh/ground3d-lmm/blob/4b2c733e5326a4607920616adf61ce8468f52395/uniseg3d/query_decoder.py#L1428)。
