# 白盒知识蒸馏：教师-学生与 KL 损失

## 1. 本讲目标

本讲是参数高效训练与模型压缩的最后一站。学完本讲，你应当能够：

- 说清**黑盒蒸馏**与**白盒蒸馏**的本质区别，并能对应到 MiniMind 里 `full_sft` 数据与 `train_distillation.py` 两条路径。
- 理解**温度缩放（Temperature Scaling）**为什么能让学生模型学到教师的"暗知识（dark knowledge）"，以及为什么最终损失里要乘上一个 \(T^2\)。
- 逐行读懂 `distillation_loss` 的实现：`softmax` / `log_softmax` / `F.kl_div` / `batchmean` 每一步在算什么。
- 走读 `train_epoch` 中 **CE + KL 混合损失** 的训练循环，理解 `alpha` 如何在硬标签与软标签之间分配权重，以及 `loss_mask` 如何把蒸馏限定在 assistant 回答段。
- 理解教师/学生**双模型加载**的装配顺序（`init_model` 两次调用、教师 `eval`+`no_grad`、词表对齐切片）。

> 本讲承接 [u3-l5（CausalLM 前向与交叉熵）](u3-l5-forward-and-loss.md) 的损失知识与 [u5-l2（Full SFT）](u5-l2-full-sft.md) 的监督微调骨架，是它的"分布级"升级版。

## 2. 前置知识

在读本讲前，先在脑中复述以下几个概念（若陌生，建议先看 u3-l5 与 u5-l2）：

- **交叉熵（Cross-Entropy, CE）**：监督微调的标准损失。模型在每个位置输出一个词表分布 \(p_s\)，目标是把"正确答案"那个 token 的概率推高。它只看**一个**正确 token，称为**硬标签（hard label）**。
- **softmax**：把一组任意实数 logits \(z\) 归一化成概率分布 \(p_i = \mathrm{softmax}(z)_i = \dfrac{e^{z_i}}{\sum_j e^{z_j}}\)。
- **KL 散度**：衡量两个分布 \(P\) 与 \(Q\) 的差异，\(\mathrm{KL}(P\|Q)=\sum_i P_i\log\dfrac{P_i}{Q_i}\)。它非负、当 \(P=Q\) 时为 0。注意它**不对称**：\(\mathrm{KL}(P\|Q)\neq\mathrm{KL}(Q\|P)\)。
- **教师-学生（Teacher-Student）**：用一个已经训好的"大/强"模型（教师）去指导一个"小/弱"模型（学生），让学生用更少参数或数据逼近教师的能力。
- **`res.logits` / `res.aux_loss`**：MiniMind 的 `MiniMindForCausalLM.forward` 不传 `labels` 时 `loss=None`，会返回完整的 `logits` 和 MoE 负载均衡损失 `aux_loss`（详见 u3-l5）。蒸馏里正是不传 labels、手动从 logits 算损失。

一个直觉：**SFT/CE 只告诉学生"正确答案是什么"，白盒蒸馏额外告诉学生"教师在所有候选 token 上的偏好排序"**。后者信息量大得多，这正是本讲要拆解的东西。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [trainer/train_distillation.py](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/train_distillation.py) | 本讲主角。定义 `distillation_loss`、`train_epoch` 与 `__main__` 装配流程，是白盒蒸馏的完整参考实现。 |
| [dataset/lm_dataset.py](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/dataset/lm_dataset.py) | 复用其中的 `SFTDataset`（尤其 `generate_labels`），蒸馏数据与 Full SFT 完全一致。 |
| [trainer/trainer_utils.py](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/trainer_utils.py) | 提供 `init_model`（双模型加载）、`get_lr`（余弦学习率）、`lm_checkpoint`（断点续训）。 |
| [README.md](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/README.md) | 第 `3' 知识蒸馏` 小节给出黑/白盒蒸馏的定义与公式，是本讲概念的权威出处。 |

## 4. 核心概念与源码讲解

### 4.1 黑盒蒸馏 vs 白盒蒸馏：为什么要拟合教师分布

#### 4.1.1 概念说明

知识蒸馏（Knowledge Distillation, KD）的目标是把教师模型的能力"搬"到学生模型里。按学生能看到教师的多少信息，可分两类：

- **黑盒蒸馏**：学生只能看到教师的**最终输出文本**（答案、风格、思维链）。本质上就是拿教师生成的答案当训练数据做 SFT，损失仍是普通 CE。MiniMind 主线 `full_sft` 数据里混入的 DeepSeek-R1 / Qwen3 高质量回答、tool call、reasoning 样本，都属于黑盒蒸馏信号。
- **白盒蒸馏**：学生额外能看到教师在**输出层的 token 分布**（logits/概率），不只学"标准答案"，还学"教师在候选 token 之间的相对倾向"。

两者的关键差异可以用一句话概括：**黑盒学的是"老师说了什么"，白盒学的是"老师在不同选项间怎么权衡"。**

#### 4.1.2 核心流程

README 给出了两者的损失公式（[README.md:L741-L749](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/README.md#L741-L749)）。

黑盒蒸馏等价于对教师输出做监督微调：

\[
\mathcal{L}_{\text{blackbox}} = \mathrm{CE}\!\left(y_{\text{teacher}},\; p_{\text{student}}\right)
\]

白盒蒸馏在监督损失之外，再额外拟合教师分布：

\[
\mathcal{L}_{\text{whitebox}} = \alpha\,\mathcal{L}_{\text{CE}} + (1-\alpha)\,T^{2}\,\mathrm{KL}\!\left(p_t^{T}\;\|\;p_s^{T}\right)
\]

其中：
- \(p_t^{T}=\mathrm{softmax}(z_t/T)\) 是教师 logits 经温度 \(T\) 软化后的分布；
- \(p_s^{T}=\mathrm{softmax}(z_s/T)\) 是学生对应分布；
- \(\alpha\in[0,1]\) 平衡硬标签 CE 与软标签 KL 的权重；
- 前置的 \(T^2\) 是为了补偿温度软化导致的梯度缩小（4.3 节详解）。

#### 4.1.3 源码精读

README 明确把 `train_distillation.py` 定位为**白盒蒸馏的参考实现**：

> 仓库中提供的 `train_distillation.py` 更适合作为理解白盒蒸馏流程的参考实现：它完整展示了教师/学生双模型加载、`CE + KL` 混合损失、温度缩放、MoE 与 dense 组合蒸馏，以及断点续训和分布式训练等关键细节。（[README.md:L751](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/README.md#L751)）

这段话点出了本讲要拆解的全部要点：双模型加载、CE+KL、温度、MoE↔Dense、续训、DDP。本讲后续模块逐一对应。

#### 4.1.4 代码实践（源码阅读型）

1. **实践目标**：在脑子里建立两条蒸馏路径的对照表。
2. **操作步骤**：
   - 打开 [README.md:L735-L758](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/README.md#L735-L758)，通读"3' 知识蒸馏"小节。
   - 在 `trainer/` 目录下确认存在 `train_distillation.py`（白盒）与 `train_full_sft.py`（黑盒的主线载体）。
3. **需要观察的现象**：README 用同一组公式区分了黑/白盒，并明确"当前主线 `full_sft` 数据里已混入相当一部分"黑盒蒸馏信号。
4. **预期结果**：能口述"黑盒=对着教师输出做 CE，白盒=额外加一项 KL 拟合教师分布"。
5. 无需运行命令，纯阅读。

#### 4.1.5 小练习与答案

**练习 1**：MiniMind 的主线 `full_sft` 训练，属于黑盒蒸馏还是白盒蒸馏？
> **答**：属于黑盒蒸馏（的载体）。因为 `train_full_sft.py` 只用标准答案 token 做 CE，学生看不到教师的 logits 分布；只是数据里混入了来自强模型的回答文本。

**练习 2**：若把白盒蒸馏损失里的 \(\alpha\) 设为 1，它退化成什么？
> **答**：退化成纯 CE，即与 Full SFT 等价；KL 项权重为 0，教师分布不再发挥作用。反之 \(\alpha=0\) 则完全放弃硬标签，只用 KL 拟合教师分布。

---

### 4.2 教师/学生双模型加载与词表对齐

#### 4.2.1 概念说明

白盒蒸馏要**同时**在显存里放两个模型：一个固定不动、只跑前向出 logits 的**教师**，一个正常反向更新的**学生**。这带来三个工程问题：

1. **装配顺序**：教师必须先 `eval()` 并 `requires_grad_(False)`，否则它的参数会进计算图、占反向显存。
2. **权重定位**：教师和学生可能结构不同（如 MoE 教师 vs Dense 学生），需要靠命名规则自动找到各自的权重文件。
3. **词表对齐**：两者的 logits 必须在同一个词表维度上才能算 KL。

#### 4.2.2 核心流程

`__main__` 第 5 步的双模型装配伪代码：

```
model, tokenizer      = init_model(lm_config_student, from_student_weight)  # 学生
teacher_model, _      = init_model(lm_config_teacher, from_teacher_weight)  # 教师
teacher_model.eval()                  # 关闭 dropout 等
teacher_model.requires_grad_(False)   # 冻结，省反向显存
```

权重文件名由 `init_model` 按统一规则拼出：

\[
\texttt{weight\_path} = \texttt{\{save\_dir\}/\{from\_weight\}\_\{hidden\_size\}\{_moe?\}.pth}
\]

即 `full_sft_768.pth`（Dense 学生）与 `full_sft_768_moe.pth`（MoE 教师）会自动各取各的文件，互不干扰。

#### 4.2.3 源码精读

双模型加载的主干（[train_distillation.py:L205-L210](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/train_distillation.py#L205-L210)）：

```python
model, tokenizer = init_model(lm_config_student, args.from_student_weight, device=args.device)
Logger(f'学生模型总参数量：{sum(p.numel() for p in model.parameters()) / 1e6:.3f} M')
teacher_model, _ = init_model(lm_config_teacher, args.from_teacher_weight, device=args.device)
teacher_model.eval()
teacher_model.requires_grad_(False)
```

要点：
- 两次 `init_model` 分别按各自的 `lm_config`（Dense/MoE、不同 hidden_size）去拼权重路径并加载（`strict=False` 容错）。
- 教师拿到的 tokenizer 用 `_` 丢弃——**师生共用同一份分词器**，这是词表天然对齐的前提。
- 教师 `eval()` + `requires_grad_(False)` 是省显存的关键：冻结后 PyTorch 不会为它建计算图、不会存中间激活用于反向。

权重命名与加载细节在 [trainer_utils.py:L119-L131](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/trainer_utils.py#L119-L131)：

```python
def init_model(lm_config, from_weight='pretrain', ...):
    tokenizer = AutoTokenizer.from_pretrained(tokenizer_path)
    model = MiniMindForCausalLM(lm_config)
    if from_weight != 'none':
        moe_suffix = '_moe' if lm_config.use_moe else ''
        weight_path = f'{save_dir}/{from_weight}_{lm_config.hidden_size}{moe_suffix}.pth'
        weights = torch.load(weight_path, map_location=device)
        model.load_state_dict(weights, strict=False)
    ...
```

> 默认配置（[train_distillation.py:L163-L168](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/train_distillation.py#L163-L168)）是 `student_use_moe=0`、`teacher_use_moe=1`，即默认演示**用 MoE 教师（minimind-3-moe，约 198M-A64M）蒸馏 Dense 学生（minimind-3，约 64M）**，师生都从 `full_sft` 权重接力（[train_distillation.py:L169-L170](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/train_distillation.py#L169-L170)）。

词表对齐的防御性切片在 `train_epoch` 里（[train_distillation.py:L62-L66](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/train_distillation.py#L62-L66)）：

```python
with torch.no_grad():
    teacher_logits = teacher_model(input_ids).logits[..., :-1, :].contiguous()
    vocab_size_student = student_logits.size(-1)
    teacher_logits = teacher_logits[..., :vocab_size_student]
```

因为师生共用同一份 6400 词表的分词器，两者的 logits 最后一维本就都是 6400，这行 `[..., :vocab_size_student]` 实际是**不切任何东西的 no-op**，但它是一道防御：万一将来学生用了更小的词表，这里能安全截断教师分布的前 N 维，避免形状不匹配崩溃。

#### 4.2.4 代码实践（源码阅读型）

1. **实践目标**：确认双模型装配的顺序与命名规则。
2. **操作步骤**：
   - 阅读 [train_distillation.py:L146-L177](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/train_distillation.py#L146-L177) 的 `argparse`，找到 4 个关键参数：`student_hidden_size`/`student_num_layers`/`student_use_moe` 与对应的 teacher 三件套。
   - 阅读 [train_distillation.py:L186-L187](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/train_distillation.py#L186-L187)，看 `lm_config_student` / `lm_config_teacher` 是如何由这些参数构造的。
3. **需要观察的现象**：默认 `student_use_moe=0`、`teacher_use_moe=1`，对应文件名分别是 `full_sft_768.pth` 与 `full_sft_768_moe.pth`。
4. **预期结果**：能解释"为什么教师和学生都传 `from_weight='full_sft'` 却不会加载同一个文件"——因为 `init_model` 会按各自 `use_moe` 追加 `_moe` 后缀。
5. 无需运行命令。

#### 4.2.5 小练习与答案

**练习 1**：如果想让一个**更小**的学生（如 `hidden_size=512`）从头学起，应该怎么配置？
> **答**：设 `--student_hidden_size 512 --student_num_layers 6 --from_student_weight none`。`from_weight='none'` 会让 `init_model` 跳过 `torch.load`（[trainer_utils.py:L123](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/trainer_utils.py#L123)），随机初始化学生；否则它会去找 `full_sft_512.pth`，文件不存在会报错。

**练习 2**：为什么教师模型必须 `requires_grad_(False)`？
> **答**：教师只负责给出目标分布、不参与学习。若不冻结，反向传播会为它计算并累积梯度、占用激活显存，还会被优化器错误更新，破坏教师分布的稳定性。

---

### 4.3 distillation_loss：温度缩放与 T²KL

#### 4.3.1 概念说明

这是白盒蒸馏的数学核心。先建立两个直觉：

**直觉一：软分布里有"暗知识"。** 假设预测句子 "A cat ___" 的下一个词，教师 logits 经普通 softmax（\(T=1\)）后可能是 \(P(\text{the})=0.99,\;P(\text{a})=0.005,\;P(\text{that})=0.003\)，几乎全压在 top-1 上，其余 token 的差异肉眼难辨。但若把 logits 除以一个 \(T>1\) 再 softmax，分布会被"软化摊平"，露出 the/a/that 之间的相对排序——这就是 Hinton 称作的 **dark knowledge（暗知识）**。学生学到这层相对偏好，比只学一个硬标签 the 信息量大得多。

**直觉二：为什么要乘 \(T^2\)。** 把 logits 除以 \(T\) 后，softmax 输出对 logits 的梯度会大致缩放为 \(1/T^2\)。若直接用软化后的 KL 作为损失，反向梯度会比 CE 小很多，优化器会"偏科"——KL 项几乎不起作用。所以在 KL 前乘 \(T^2\) 把梯度量级拉回与 CE 可比，这正是 README 公式里 \(T^2\) 的来历。

#### 4.3.2 核心流程

温度软化的概率：

\[
p_t^{T} = \mathrm{softmax}\!\left(\frac{z_t}{T}\right),\qquad
p_s^{T} = \mathrm{softmax}\!\left(\frac{z_s}{T}\right)
\]

KL 散度（注意 PyTorch `F.kl_div` 的方向，4.3.3 详解）：

\[
\mathrm{KL}\!\left(p_t^{T}\;\|\;p_s^{T}\right) = \sum_i p_{t,i}^{T}\log\frac{p_{t,i}^{T}}{p_{s,i}^{T}}
\]

最终蒸馏损失：

\[
\mathcal{L}_{\text{distill}} = T^{2}\cdot\mathrm{KL}\!\left(p_t^{T}\;\|\;p_s^{T}\right)
\]

#### 4.3.3 源码精读

完整实现只有 12 行（[train_distillation.py:L25-L36](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/train_distillation.py#L25-L36)）：

```python
def distillation_loss(student_logits, teacher_logits, temperature=1.0, reduction='batchmean'):
    with torch.no_grad():
        teacher_probs = F.softmax(teacher_logits / temperature, dim=-1).detach()

    student_log_probs = F.log_softmax(student_logits / temperature, dim=-1)

    kl = F.kl_div(
        student_log_probs,
        teacher_probs,
        reduction=reduction
    )
    return (temperature ** 2) * kl
```

逐行拆解：

1. **教师分布 `teacher_probs`**：在 `torch.no_grad()` 下对教师 logits 除以 \(T\) 再 `softmax`，并 `.detach()`。教师不参与反向，所以既不建图也不算梯度。
2. **学生分布 `student_log_probs`**：对学生 logits 除以 \(T\) 再 `log_softmax`。注意这里要的是**对数概率**，因为 `F.kl_div` 的第一个参数必须是 log-probabilities。
3. **`F.kl_div(input, target)`**：PyTorch 的语义是计算 \(\sum \text{target}\cdot(\log\text{target}-\text{input})\)。把 `input=student_log_probs`、`target=teacher_probs` 代入，得到 \(\sum_i p_{t,i}\log\frac{p_{t,i}}{p_{s,i}}\)，正是 \(\mathrm{KL}(p_t\|p_s)\)——即"让学生去逼近教师"的方向，正确。
4. **`reduction='batchmean'`**：把所有元素求和后**除以 batch 维度的大小**。这里有个常见坑：若用 `'mean'` 会除以"batch×序列×词表"的总元素数，把 KL 平均得过小；`'batchmean'` 才是数学意义上"按 batch 平均"的正确 KL。本函数被调用时传入的已经是按 token 展平后的 `[num_valid_tokens, V]`，所以 `batchmean` 得到的是**每个有效 token 的平均 KL**。
5. **`return (temperature ** 2) * kl`**：补偿温度导致的梯度缩小，与 README 公式里的 \(T^2\) 一一对应。

> 一个易错点：`F.kl_div` 的参数顺序与直觉相反——它算的是 `KL(target || input)` 而不是 `KL(input || target)`。写成 `F.kl_div(student_log_probs, teacher_probs)` 是对的（= KL(teacher‖student)）；若写反方向，会变成让教师逼近学生的前向 KL，语义错误且训练不稳定。

#### 4.3.4 代码实践（无需 GPU，可本地验证）

1. **实践目标**：用一组玩具 logits 直观看到"温度软化"和"\(T^2\) 补偿"的效果。
2. **操作步骤**：在任意能 `import torch` 的环境里跑下面这段**示例代码**（非项目原码）：

   ```python
   import torch, torch.nn.functional as F
   # 假装教师、学生在某个 token 位置的 logits（词表大小=5）
   teacher_logits = torch.tensor([2.0, 1.0, 0.5, -1.0, -2.0])
   student_logits = torch.tensor([1.0, 0.8, 0.6, 0.0, -0.5])  # 还没学好

   for T in [1.0, 1.5, 2.0]:
       pt = F.softmax(teacher_logits / T, dim=-1)
       ps = F.softmax(student_logits / T, dim=-1)
       kl = F.kl_div(F.log_softmax(student_logits / T, dim=-1), pt, reduction='batchmean')
       print(f"T={T}: 教师软分布={pt.numpy().round(3)}, KL={kl.item():.4f}, T^2*KL={(T**2*kl).item():.4f}")
   ```
3. **需要观察的现象**：
   - \(T\) 越大，教师软分布越"平"（top-1 概率下降，其它上升）。
   - 裸 KL 随 \(T\) 增大而变小（梯度变弱），但乘 \(T^2\) 后被拉回同一量级。
4. **预期结果**：能看到 \(T=2\) 时软分布明显比 \(T=1\) 平坦，且 \(T^2\cdot\mathrm{KL}\) 与 \(T=1\) 的 KL 数量级接近，验证 \(T^2\) 的补偿作用。
5. 若本机无 torch，可改为手算：\(T=1\) 时 \(p_t\approx[0.42,0.23,...]\)，\(T=2\) 时更平——结论一致。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `teacher_probs` 要 `.detach()`，而 `student_log_probs` 不要？
> **答**：教师是固定的目标分布，不需要也不应该产生梯度；`.detach()` 把它从计算图剥离。学生分布才是要被优化的对象，必须保留在图里，梯度才能回传到学生权重。

**练习 2**：把 `reduction='batchmean'` 误写成 `'mean'`，损失会怎么变？
> **答**：`'mean'` 会多除以一个"词表大小（×序列长度）"，使 KL 数值被严重缩小，相当于蒸馏项权重被意外压低，优化器会几乎只看 CE 项、KL 形同虚设。

---

### 4.4 train_epoch：CE + KL 混合损失训练循环

#### 4.4.1 概念说明

`train_epoch` 是白盒蒸馏的训练主循环，本质是 [u5-l2 Full SFT 的 train_epoch](u5-l2-full-sft.md) 的"加强版"：同样在 assistant 段做位移交叉熵，**额外**把同一段的学生 logits 喂给 `distillation_loss` 去拟合教师，再用 \(\alpha\) 把两者加权求和。它一次性产出训练日志里的 4 个关键量：

- `loss`：总损失 \(\alpha\cdot\text{CE}+(1-\alpha)\cdot\text{distill}\)。
- `ce`：纯硬标签 CE（MoE 学生时不含 aux_loss）。
- `aux_loss`：MoE 负载均衡损失（Dense 学生为 0）。
- `distill`：温度 KL 蒸馏损失。

#### 4.4.2 核心流程

单个 step 的伪代码：

```
input_ids, labels = next(loader)
loss_mask = (labels[1:] != -100)            # 哪些位置是 assistant token
lr = get_lr(epoch*iters+step, ...)          # 余弦退火，手动写回 optimizer

# 学生前向（不传 labels，手动算损失，因为还要用 logits 做蒸馏）
student_logits = model(input_ids).logits[:, :-1, :]

# 教师前向（no_grad，词表对齐切片）
teacher_logits = teacher_model(input_ids).logits[:, :-1, :vocab_student]

# 1) 硬标签 CE（只在 loss_mask 位置平均）
ce = masked_mean( CE(student_logits, labels[1:]), loss_mask )
if use_moe: ce += aux_loss

# 2) 软标签 KL（只在 loss_mask 位置算）
distill = T^2 * KL( teacher | student )   over loss_mask 位置

# 3) 混合
loss = (alpha*ce + (1-alpha)*distill) / accumulation_steps
loss.backward(); 每 N 步 unscale→clip→step→update→zero_grad
```

#### 4.4.3 源码精读

**教师冻结**与**loss_mask 构造**（[train_distillation.py:L43-L51](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/train_distillation.py#L43-L51)）：

```python
if teacher_model is not None:
    teacher_model.eval()
    teacher_model.requires_grad_(False)

for step, (input_ids, labels) in enumerate(loader, start=start_step + 1):
    ...
    loss_mask = (labels[..., 1:] != -100).float()
```

`loss_mask` 取自位移后的 labels：只有"真值不是 -100"的位置（即 assistant 回答段）才为 1。它后面会同时用于 CE 的掩码平均和 KL 的 token 筛选，保证**蒸馏只发生在回答段、不污染提问和 padding**。这一段掩码逻辑直接来自 [u5-l2 讲过的 SFTDataset.generate_labels](u5-l2-full-sft.md)（[lm_dataset.py:L88-L104](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/dataset/lm_dataset.py#L88-L104)）。

**学生前向（不传 labels）**（[train_distillation.py:L57-L59](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/train_distillation.py#L57-L59)）：

```python
with autocast_ctx:
    res = model(input_ids)
    student_logits = res.logits[..., :-1, :].contiguous()
```

注意这里**没有传 labels**，所以 `res.loss is None`（回顾 [model_minimind.py:L245-L253](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/model/model_minimind.py#L245-L253) 的 forward：只有 `labels is not None` 才算 CE）。蒸馏需要自己掌控损失构成，因此手动从 `res.logits` 切出 `[:, :-1, :]`（位移，预测下一个 token）。

**硬标签 CE 的手动掩码平均**（[train_distillation.py:L68-L80](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/train_distillation.py#L68-L80)）：

```python
shift_labels = labels[..., 1:].contiguous()
loss_mask_flat = loss_mask.view(-1)
ce_loss = F.cross_entropy(
    student_logits.view(-1, student_logits.size(-1)),
    shift_labels.view(-1),
    ignore_index=-100,
    reduction='none'
)
ce_loss_raw = torch.sum(ce_loss * loss_mask_flat) / (loss_mask_flat.sum() + 1e-8)
if lm_config_student.use_moe: ce_loss = ce_loss_raw + res.aux_loss
else: ce_loss = ce_loss_raw
```

这里用 `reduction='none'` 拿到每个 token 的 CE，再用 `loss_mask_flat` 做"加权和除以有效 token 数"——等价于 u3-l5 里 `ignore_index=-100` 的效果，但显式写出是为了**复用同一份 `loss_mask_flat` 去筛 KL 的 token**。MoE 学生还要把 `res.aux_loss`（负载均衡损失，回顾 [u3-l4](u3-l4-swiglu-and-moe.md)）加进 CE，Dense 学生则原样保留。

**软标签 KL 蒸馏**（[train_distillation.py:L82-L90](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/train_distillation.py#L82-L90)）：

```python
if teacher_model is not None:
    distill_loss = distillation_loss(
        student_logits.view(-1, student_logits.size(-1))[loss_mask_flat == 1],
        teacher_logits.view(-1, teacher_logits.size(-1))[loss_mask_flat == 1],
        temperature=temperature
    )
else:
    distill_loss = torch.tensor(0.0, device=args.device)
```

关键：把展平后的 logits 用 `loss_mask_flat == 1` **只挑出 assistant token 的行**，再交给 `distillation_loss`。这样 KL 只在回答段计算。`teacher_model is None` 分支让本脚本也能退化为纯 SFT（不接教师），保持灵活性。

**混合损失与更新**（[train_distillation.py:L92-L102](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/train_distillation.py#L92-L102)）：

```python
loss = (alpha * ce_loss + (1 - alpha) * distill_loss) / args.accumulation_steps
scaler.scale(loss).backward()
if step % args.accumulation_steps == 0:
    scaler.unscale_(optimizer)
    torch.nn.utils.clip_grad_norm_(model.parameters(), args.grad_clip)
    scaler.step(optimizer)
    scaler.update()
    optimizer.zero_grad(set_to_none=True)
```

\(\alpha\) 默认 0.5（[train_distillation.py:L172](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/train_distillation.py#L172)），硬标签与软标签各占一半。除以 `accumulation_steps` 是梯度累积（回顾 [u4-l3](u4-l3-ddp-and-amp.md)），更新块顺序严格为 `unscale_ → clip_grad_norm_ → step → update → zero_grad`。学习率走 [trainer_utils.py:L40-L41](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/trainer_utils.py#L40-L41) 的余弦退火，每步手动写回 `param_groups['lr']`（[train_distillation.py:L52-L54](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/train_distillation.py#L52-L54)）。

**日志输出**（[train_distillation.py:L104-L122](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/train_distillation.py#L104-L122)）每 `log_interval` 步打印一次：

```python
Logger(f'Epoch:[{epoch + 1}/{args.epochs}]({step}/{iters}), loss: {current_loss:.4f}, '
       f'ce: {current_ce_loss:.4f}, aux_loss: {current_aux_loss:.4f}, '
       f'distill: {distill_loss.item():.4f}, learning_rate: {current_lr:.8f}, ...')
```

四个量同时可见，方便你盯着 `distill` 是否在下降。**保存**逻辑（[train_distillation.py:L124-L134](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/train_distillation.py#L124-L134)）与其它训练脚本同构：剥 `module`/`_orig_mod` 外壳、转 fp16 存 `full_dist_{dim}{_moe?}.pth`，并调 `lm_checkpoint` 写续训文件。

#### 4.4.4 代码实践

1. **实践目标**：跑若干步白盒蒸馏，观察 `ce` 与 `distill` 的数值变化，验证学生分布逐步逼近教师。
2. **操作步骤**：
   - 准备权重：确保 `out/` 下有 `full_sft_768.pth`（Dense 学生）和 `full_sft_768_moe.pth`（MoE 教师）；若缺前者，可改用 `--from_student_weight none` 随机初始化学生。
   - 确认 `dataset/sft_t2t_mini.jsonl` 存在。
   - 在 `trainer/` 目录下启动（README 给出的方式，[README.md:L753-L757](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/README.md#L753-L757)）：

     ```bash
     cd trainer
     torchrun --nproc_per_node 1 train_distillation.py \
         --epochs 1 --log_interval 10 --save_interval 200 \
         --student_hidden_size 768 --student_use_moe 0 \
         --teacher_hidden_size 768 --teacher_use_moe 1 \
         --from_student_weight full_sft --from_teacher_weight full_sft \
         --alpha 0.5 --temperature 1.5
     ```
   - （可选）想看更明显的"学生追教师"曲线，临时在日志行后追加一行打印原始温度（\(T=1\)）下的 `KL(teacher‖student)` 作为外部观测：

     ```python
     # 示例代码：插在 train_epoch 日志块里，独立度量师生分布距离
     with torch.no_grad():
         raw_kl = F.kl_div(
             F.log_softmax(student_logits, dim=-1),
             F.softmax(teacher_logits, dim=-1), reduction='batchmean').item()
         Logger(f'  [观测] raw_KL(T=1)={raw_kl:.4f}')
     ```
3. **需要观察的现象**：随着 step 推进，`distill`（温度 1.5 的 KL）应整体呈下降趋势；可选的 `raw_KL(T=1)` 也应同步下降，说明学生 logits 确实在逼近教师。
4. **预期结果**：`ce` 在低位（因学生从 `full_sft` 接力，本就会答）小幅波动；`distill` 从某个初值随训练下降；总 `loss` 受三者共同驱动。
5. **待本地验证**：本实践依赖 GPU 与 `full_sft` 权重，作者无法在此替你运行；若本机无 GPU，可把 `--device cpu --dtype float16` 勉强跑极小 batch 验证流程通畅，但速度很慢。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `train_epoch` 调用 `model(input_ids)` 时**不传 labels**，而是自己手算 CE？
> **答**：因为白盒蒸馏需要同时拿到 `student_logits` 去算 KL。若传 labels，forward 会内部算出一个 CE 但不返回完整的位移 logits 供 KL 使用；手动算 CE 能让 CE 与 KL 复用同一份 `student_logits` 和 `loss_mask`，逻辑更清晰。

**练习 2**：默认 `alpha=0.5`、`temperature=1.5`，若把 `temperature` 调到 8，会发生什么？
> **答**：分布被过度软化，教师几乎变成均匀分布，暗知识被稀释，KL 信号变弱（虽有 \(T^2=64\) 补偿量级，但信息量已贫乏）；学生更难从教师学到有用的相对偏好，蒸馏效果反而下降。README 也建议温度范围 1.0–2.0（[train_distillation.py:L173](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/train_distillation.py#L173)）。

---

## 5. 综合实践

把本讲的"双模型加载 + distillation_loss + train_epoch"串成一个端到端小任务：

**任务：用一个随机初始化的小 Dense 学生，去蒸馏一个 `full_sft` 的 MoE 教师，并验证师生分布收敛。**

1. 启动训练（在 `trainer/` 下）：

   ```bash
   torchrun --nproc_per_node 1 train_distillation.py \
       --student_hidden_size 512 --student_num_layers 6 --student_use_moe 0 \
       --from_student_weight none \
       --teacher_hidden_size 768 --teacher_use_moe 1 \
       --from_teacher_weight full_sft \
       --alpha 0.5 --temperature 1.5 --epochs 1 --log_interval 20
   ```

   - `--from_student_weight none` 让 `init_model` 随机初始化 512 维小 Dense 学生（[trainer_utils.py:L123](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/trainer_utils.py#L123)）。
   - 教师仍从 `full_sft_768_moe.pth` 加载。

2. 在日志块插入 4.4.4 的"观测 raw_KL(T=1)"示例代码。

3. **要回答的三个问题**：
   - `ce` 从随机初始化的高位降到大概什么量级？（硬标签学习）
   - `distill` 与 `raw_KL(T=1)` 是否同步下降？（软标签/分布拟合）
   - 训练结束后，用 `eval_llm.py --weight full_dist`（学生导出名为 `full_dist_512.pth`）对话，回答质量是否比随机初始化明显改善？

4. **预期结论**：`distill` 与 `raw_KL` 都应下降，说明即便学生更小、且未做过 SFT，仅靠教师的分布信号也能让它逐步习得语言能力——这就是白盒蒸馏的价值。

5. **待本地验证**：本任务需 GPU、`full_sft_768_moe.pth` 教师权重与 `sft_t2t_mini.jsonl` 数据，需在本地实跑确认数值。

## 6. 本讲小结

- 白盒蒸馏 = 在 SFT 的硬标签 CE 之外，**额外**用温度软化的 KL 去拟合教师 logits 分布；MiniMind 的 `train_distillation.py` 是其参考实现，黑盒蒸馏则由主线 `full_sft` 承载。
- 蒸馏损失为 \(\mathcal{L}=\alpha\,\mathcal{L}_{\text{CE}}+(1-\alpha)\,T^2\,\mathrm{KL}(p_t^T\|p_s^T)\)；\(T\) 软化分布暴露"暗知识"，\(T^2\) 补偿梯度缩小，\(\alpha\) 平衡硬/软标签。
- `distillation_loss` 用 `F.kl_div(student_log_probs, teacher_probs, reduction='batchmean')` 计算 \(\mathrm{KL}(\text{teacher}\|\text{student})\)，参数顺序与 reduction 都有坑，方向不能写反。
- 双模型加载靠 `init_model` 两次调用，按 `{weight}_{dim}{_moe?}.pth` 自动定位文件；教师必须 `eval()`+`requires_grad_(False)`，师生共用同一份分词器保证词表对齐。
- `train_epoch` 不传 labels、手动从 `res.logits` 切位移，用同一份 `loss_mask` 同时驱动 CE 的掩码平均和 KL 的 token 筛选，保证只在 assistant 段学习。
- 蒸馏日志同时输出 `loss/ce/aux_loss/distill`，盯 `distill` 下降即可判断学生是否在逼近教师；导出权重名为 `full_dist_{dim}{_moe?}.pth`。

## 7. 下一步学习建议

- **进入强化学习后训练**：蒸馏属于"用教师信号"训练，下一单元 [u7-l1 DPO 与 PO 统一视角](u7-l1-dpo-and-unified-rl.md) 将转向"用偏好/奖励信号"训练，建议先读它建立 RLHF 的统一框架。
- **对比阅读 LoRA**：若你想用更省显存的方式固定教师、只调学生少量参数，可回头对比 [u6-l1/u6-l2 LoRA 实现](u6-l1-lora-implementation.md)——理论上学生侧也能套 LoRA。
- **延伸阅读源码**：精读 [train_distillation.py 的 `__main__` 装配](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/train_distillation.py#L146-L248) 的 9 步流程，它与 [u5-l1 预训练](u5-l1-pretrain.md) 的装配高度同构，可作为"训练脚本模板"的又一个范例。
