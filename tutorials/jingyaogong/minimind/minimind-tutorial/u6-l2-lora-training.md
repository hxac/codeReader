# LoRA 训练流程与垂域适配

## 1. 本讲目标

上一讲（u6-l1）我们从 0 实现了 LoRA 模块本身：`LoRA` 低秩分支、`apply_lora` 的 monkey-patch 注入、`save_lora`/`load_lora`/`merge_lora` 的存读合并。本讲要回答的下一个问题是——**如何把这些 LoRA 模块真正跑成一个训练任务**。

学完本讲，你应当能够：

1. 说清 `train_lora.py` 是怎么「冻结 base、只把含 `lora` 的参数喂给优化器」的，以及为什么这是双重保险。
2. 看懂 `train_epoch` 与全参 SFT（u5-l2）几乎逐行同构，差异只在「哪些参数更新」与「学习率大小」。
3. 掌握 LoRA 权重的命名规则 `lora_xxx_{dim}{_moe?}.pth`，以及如何用 `eval_llm.py --weight full_sft --lora_weight lora_identity` 做「基模 + LoRA」组合推理。
4. 解释为什么 `train_lora.py` 会在检测到 `--use_compile 1` 时自动把它关掉——即 `torch.compile` 与 monkey-patch forward 不兼容的根因。
5. 准备一份最小的垂域（自我认知 / 医学）数据并完成「训练 + 组合推理」的闭环。

---

## 2. 前置知识

本讲默认你已经掌握以下内容（来自前置讲义），这里只做一句话回顾：

- **LoRA 的数学结构（u6-l1）**：冻结基模权重 \(W\)，在它旁边并联一个低秩增量 \(BA\)，前向变为 \(y = Wx + BAx\)；\(A\) 高斯初始化、\(B\) 全 0，保证训练起点 \(\Delta W = 0\)。
- **apply_lora 的注入方式（u6-l1）**：遍历所有**方阵** `nn.Linear`（默认配置下恰好是每层的 `q_proj` 与 `o_proj`），给它们挂一个 `.lora` 子模块，并用 monkey-patch 把 `module.forward` 替换为「原输出 + 旁路输出」。
- **Full SFT 的训练循环（u5-l2 / u4-l3）**：`init_distributed_mode` → 混合精度 `autocast` → 前向得 `res.loss + res.aux_loss` → 除以梯度累积步数 → 反向 → 每 N 步 `unscale_ → clip_grad_norm_ → step → update → zero_grad`。

如果你对这些还陌生，建议先回到 u6-l1 和 u5-l2。本讲的核心洞察其实很朴素：**LoRA 训练 = Full SFT 训练循环 + 「只放开 LoRA 参数」+ 「换更大的学习率」**。

> 关键术语速查：
> - **requires_grad**：PyTorch 张量的属性，为 `True` 时该参数才会在 `backward` 中累计梯度。
> - **垂域（vertical domain）适配**：在保留模型通用能力的前提下，让它更适合某个特定场景（如医疗问答、自我认知）。
> - **monkey-patch**：在运行时替换某个对象的方法（这里是 `module.forward`）。

---

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| `trainer/train_lora.py` | LoRA 训练主脚本（186 行） | 全部讲解围绕它展开：装配、冻结、训练循环、保存 |
| `model/model_lora.py` | LoRA 模块与存读合并实现 | 重点复习 `apply_lora` / `save_lora`，理解它如何与训练脚本对接 |
| `eval_llm.py` | CLI 推理入口 | 第 24–26 行的「基模 + LoRA」组合加载逻辑 |
| `dataset/lm_dataset.py` | 数据集类 | `SFTDataset`（train_lora 直接复用，读取 `conversations` 字段） |
| `README.md` | 项目说明 | 第 760–815 行的 LoRA 章节给出数据格式与运行命令 |

---

## 4. 核心概念与源码讲解

本讲拆成 5 个最小模块：**(1) 应用 LoRA、冻结 base 与过滤可训练参数**；**(2) `train_epoch` 训练循环**；**(3) `save_lora` 与命名规则**；**(4) 垂域数据与组合推理**；**(5) 为什么自动禁用 `torch.compile`**。

### 4.1 应用 LoRA、冻结 base 与过滤可训练参数

#### 4.1.1 概念说明

LoRA 训练最关键的一步，不是「加上 LoRA 模块」，而是「**保证只有 LoRA 模块在更新**」。如果这一步做错，比如忘了冻结 base，那么 `apply_lora` 注入的低秩分支就形同虚设——整个模型会被全参数微调，既浪费显存也违背 LoRA 的初衷。

`train_lora.py` 用**两道独立保险**来确保这一点：

1. **冻结（freeze）**：把所有名字里不含 `lora` 的参数的 `requires_grad` 置为 `False`，使它们在反向传播时根本不计算梯度。
2. **过滤（filter）**：构造优化器时只把名字里含 `lora` 的参数列表 `lora_params` 传给 `AdamW`，即使有漏网的梯度，优化器也不会去更新它们。

两者叠加，从「梯度计算」和「梯度应用」两个环节都堵住了对 base 权重的修改。这是工程上很稳健的写法——任何一道失灵，另一道仍能兜底。

#### 4.1.2 核心流程

```text
init_model(from_weight='full_sft')     # 1. 加载基模（默认 full_sft）
        │
        ▼
apply_lora(model)                        # 2. 给每个方阵 Linear 挂 .lora 旁路
        │
        ▼
统计参数：total / lora_count / 占比       # 3. 打印，确认 LoRA 参数占比极小
        │
        ▼
for name, param in model.named_parameters():
    if 'lora' in name:
        param.requires_grad = True        # 4a. 放开 LoRA 参数
        lora_params.append(param)         #     并收集进列表
    else:
        param.requires_grad = False       # 4b. 冻结 base 参数
        │
        ▼
optimizer = AdamW(lora_params, ...)       # 5. 只对 LoRA 参数建优化器
```

#### 4.1.3 源码精读

第 5 步装配里，先加载基模，再注入 LoRA：

[trainer/train_lora.py:128-130](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/train_lora.py#L128-L130) —— `init_model` 默认从 `full_sft` 权重加载基模，紧接着调用 `apply_lora(model)` 给所有方阵 `Linear` 注入低秩旁路。

随后打印三类参数统计，让你直观看到「LoRA 参数只占零头」：

[trainer/train_lora.py:132-137](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/train_lora.py#L132-L137) —— 统计 `total_params`、只数名字含 `lora` 的 `lora_params_count`，并打印占比。默认配置下（`hidden_size=768`、`rank=16`、8 层），方阵只有每层的 `q_proj` 与 `o_proj`，共 16 个 LoRA 模块，每个参数量为 \(2 \times 768 \times 16\)，总计约 0.39M，占 64M 总参数的约 0.6%。

然后是核心的「冻结 + 收集」循环：

[trainer/train_lora.py:139-146](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/train_lora.py#L139-L146) —— 遍历所有命名参数：名字含 `lora` 的置 `requires_grad=True` 并追加进 `lora_params`；其余（即 base 权重）置 `requires_grad=False`。这一段就是上面说的「第一道保险」。

最后，优化器只吃 `lora_params`（「第二道保险」）：

[trainer/train_lora.py:152](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/train_lora.py#L152) —— `optim.AdamW(lora_params, lr=args.learning_rate)`，注意它的学习率默认是 `1e-4`（见 [第 84 行](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/train_lora.py#L84)），是 Full SFT（`1e-5`）的 10 倍。原因正是 LoRA 可训练参数极少，用更大的学习率才能让旁路权重学到东西。

> 旁路如何参与前向？复习 [model/model_lora.py:21-32](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/model/model_lora.py#L21-L32) 的 `apply_lora`：它把每个方阵 `Linear` 的 `forward` 替换成 `layer1(x) + layer2(x)`，其中 `layer2` 就是新挂的 `LoRA` 旁路。因为这条新前向里引用了 `lora.A` / `lora.B` 的参数，所以反向时梯度能顺着这条新增支路流回 LoRA 参数；而 base 权重由于 `requires_grad=False`，即便出现在前向图里也不会被更新。

#### 4.1.4 代码实践

**实践目标**：亲眼确认「冻结 + 过滤」生效，LoRA 可训练参数占比极小。

**操作步骤**（源码阅读 + 可选运行）：

1. 打开 `trainer/train_lora.py`，定位第 139–152 行，确认 `lora_params` 列表与 `AdamW(lora_params, ...)` 的对应关系。
2.（可选运行）在有 `full_sft_768.pth` 权重的前提下，进入 `trainer` 目录执行：
   ```bash
   cd trainer && python train_lora.py --epochs 1 --log_interval 1
   ```
3. 观察训练开始前打印的三行 `Logger`：`LLM 总参数量`、`LoRA 参数量`、`LoRA 参数占比`。

**需要观察的现象**：`LoRA 参数占比` 应在 1% 以下（约 0.6%）；若你看到占比接近 100%，说明冻结逻辑没生效（多半是误删了第 139–146 行）。

**预期结果**：类似 `LLM 总参数量: 64.XXX M`、`LoRA 参数量: 0.393 M`、`LoRA 参数占比: 0.61%`。

> 若本地没有权重或无 GPU，可只做第 1 步源码阅读，标注为「待本地验证」。

#### 4.1.5 小练习与答案

- **练习 1**：如果只做「过滤」（把 `lora_params` 传给优化器）但不做「冻结」（删掉 `requires_grad=False` 那行），训练结果会变吗？
  - **参考答案**：从「权重是否更新」的角度看不会变——优化器只更新 `lora_params`，base 参数不会被 `step`。但显存和速度会变差：因为 `requires_grad` 仍为 `True`，PyTorch 会为所有 base 参数计算并保留梯度，白白多占一份 `.grad` 显存。所以冻结的主要意义是**省显存、省计算**，过滤的主要意义是**限定更新对象**，两者职责不同，缺一不可。
- **练习 2**：为什么 LoRA 的默认学习率（`1e-4`）比 Full SFT（`1e-5`）大一个数量级？
  - **参考答案**：LoRA 只更新占比不到 1% 的旁路参数，且这些参数从 \(B=0\) 起步，需要更大的步长才能在有限步数内学到有效增量；而 Full SFT 动的是全部 64M 参数，学习率过大会破坏好不容易预训练 + SFT 出来的通用能力，所以必须更保守。

---

### 4.2 `train_epoch`：只更新 LoRA 参数的训练循环

#### 4.2.1 概念说明

`train_lora.py` 的 `train_epoch` 与 `train_full_sft.py` 的 `train_epoch` **几乎逐行相同**——都是「余弦学习率 → 混合精度前向 → 梯度累积反向 → 定期更新 → 定期保存」的标准模板（见 u4-l3）。唯一的实质性差异只有两处：

1. **梯度裁剪只作用于 `lora_params`**：`torch.nn.utils.clip_grad_norm_(lora_params, ...)`，而不是裁剪 `model.parameters()`。
2. **保存时调用 `save_lora`**：只把 LoRA 增量写到磁盘，而不是整个模型。

认识到这一点很重要：**理解了 Full SFT 训练循环，就等于理解了 LoRA 训练循环**。LoRA 不是一套新的训练算法，它只是在已有骨架上换了「谁更新」「存什么」。

#### 4.2.2 核心流程

```text
for step, (input_ids, labels) in enumerate(loader):
    1. 取当前学习率并写回 optimizer        # 余弦退火，手动写 param_groups
    2. autocast 下前向：res = model(input_ids, labels)
       loss = (res.loss + res.aux_loss) / accumulation_steps
    3. scaler.scale(loss).backward()        # 反向，梯度累积
    4. 若 step % accumulation_steps == 0：
         unscale_ → clip(lora_params) → step → update → zero_grad
    5. 定期打印 loss / logits_loss / aux_loss / lr
    6. 定期保存：save_lora(只存增量) + lm_checkpoint(存续训状态)
# 循环结束后，对尾部不足 accumulation_steps 的剩余梯度补一次更新
```

#### 4.2.3 源码精读

整个 `train_epoch` 的定义与循环骨架：

[trainer/train_lora.py:25-41](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/train_lora.py#L25-L41) —— 每个 step：用 `get_lr(epoch*iters+step, ...)` 算余弦退火学习率并写回 `optimizer.param_groups`；在 `autocast_ctx` 下前向得到 `res.loss + res.aux_loss`，除以 `accumulation_steps` 后反向。这一段与 Full SFT 一模一样。

更新块——注意 `clip_grad_norm_` 的对象是 `lora_params`：

[trainer/train_lora.py:43-48](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/train_lora.py#L43-L48) —— 标准的 `unscale_ → clip_grad_norm_(lora_params) → step → update → zero_grad` 五连。由于 base 参数 `requires_grad=False`，它们根本没有 `.grad`，所以这里裁剪 `lora_params` 和裁剪 `model.parameters()` 在数值上是等价的，但写 `lora_params` 更明确地表达了「只关心 LoRA 梯度」的意图。

日志行——Dense 模型下 `aux_loss` 恒为 0，应盯 `logits_loss`：

[trainer/train_lora.py:50-58](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/train_lora.py#L50-L58) —— 打印 `loss / logits_loss / aux_loss / lr`，其中 `current_logits_loss = current_loss - current_aux_loss`（即纯语言建模损失）。

保存块——两个文件一起写：

[trainer/train_lora.py:60-67](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/train_lora.py#L60-L67) —— 每 `save_interval` 步：先算 `lora_save_path = f'{args.save_dir}/{args.lora_name}_{lm_config.hidden_size}{moe_suffix}.pth'`（命名规则见 4.3），再 `save_lora(model, lora_save_path)` 只存 LoRA 增量；接着 `lm_checkpoint(...)` 存续训状态。`model.eval()` / `model.train()` 包裹保存段是为了避免保存时影响 dropout 等训练态。

尾部补更新——处理「最后一批梯度不满 `accumulation_steps`」的情况：

[trainer/train_lora.py:71-76](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/train_lora.py#L71-L76) —— 当 `last_step % accumulation_steps != 0` 时，循环结束时还残留未应用的累积梯度，这里补一次完整的 `unscale_ → clip → step → update → zero_grad`。

#### 4.2.4 代码实践

**实践目标**：通过 diff 对照，确认 `train_lora` 与 `train_full_sft` 的 `train_epoch` 仅在「裁剪对象」和「保存函数」上不同。

**操作步骤**：

1. 用 `git diff --no-index` 或并排阅读对比 `trainer/train_lora.py` 与 `trainer/train_full_sft.py` 的 `train_epoch` 函数。
2. 标出所有差异行，按「裁剪对象」「保存方式」「其余」三类归类。

**需要观察的现象**：除两处实质差异（`clip_grad_norm_(lora_params, ...)` vs `clip_grad_norm_(model.parameters(), ...)`；`save_lora` vs 整模型 `torch.save`）外，其余前向、反向、学习率、梯度累积逻辑应完全一致。

**预期结果**：你应当得出结论——LoRA 训练循环是 Full SFT 训练循环的「最小改写」，没有引入任何新的训练算法。

#### 4.2.5 小练习与答案

- **练习 1**：既然 base 参数没有梯度，为什么 `clip_grad_norm_(lora_params, ...)` 不写成 `clip_grad_norm_(model.parameters(), ...)`？
  - **参考答案**：两者数值等价（无梯度的参数被跳过），但传 `lora_params` 更省一次遍历、也更清晰地表达意图。这是一种「可读性优先」的写法。
- **练习 2**：日志里的 `aux_loss` 在 Dense LoRA 训练中通常是 0，为什么还要保留？
  - **参考答案**：因为同一份 `train_epoch` 代码也兼容 MoE 模型（`--use_moe 1`）。MoE 下 `res.aux_loss` 是专家负载均衡损失，非 0；保留这一项让 Dense/MoE 共用同一段日志与损失逻辑，避免分支。

---

### 4.3 `save_lora` 与 `lora_xxx_{dim}.pth` 命名规则

#### 4.3.1 概念说明

LoRA 训练产出的不是「一个新模型」，而是「一份增量补丁」。这决定了它的两个特点：

1. **文件极小**：只存 LoRA 旁路的 `A`/`B` 权重（约 0.39M 参数 → fp16 下不到 1MB），而不是整个 64M 模型。
2. **命名规则化**：文件名形如 `lora_xxx_{dim}{_moe?}.pth`，其中 `xxx` 是垂域名（`identity`/`medical`/...），`{dim}` 是 `hidden_size`，MoE 模型追加 `_moe`。这样 `eval_llm.py` 才能凭 `--lora_weight` 与 `--hidden_size` 自动定位文件。

这种「基模权重 + LoRA 补丁」分离存储的设计，让你可以**一份基模挂多份不同的 LoRA**，按场景切换，互不干扰。

#### 4.3.2 核心流程

```text
保存（save_lora）：
  for 每个挂了 .lora 的方阵 Linear:
      收集它的 lora.state_dict()   # 只有 A.weight / B.weight
      转 fp16、剥掉 module./_orig_mod 外壳
      按 "{clean_name}.lora.{k}" 命名
  torch.save(state_dict, lora_save_path)

命名（在 train_epoch 内拼接）：
  lora_save_path = f'{save_dir}/{lora_name}_{hidden_size}{_moe if use_moe else ""}.pth'
  # 例：lora_identity_768.pth、lora_medical_768_moe.pth
```

#### 4.3.3 源码精读

`save_lora` 的完整实现——只挑 `.lora.` 参数，转 fp16，剥外壳：

[model/model_lora.py:45-53](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/model/model_lora.py#L45-L53) —— 先用 `getattr(model, '_orig_mod', model)` 剥掉 `torch.compile` 产生的外壳（虽然 LoRA 训练实际禁用了 compile，这里仍保留兼容）；再遍历所有挂了 `.lora` 的模块，把它们的 `state_dict` 以 `{clean_name}.lora.{k}` 的键名收进字典；每个张量 `.cpu().half()` 转成 fp16 节省空间；最后 `torch.save`。注意它**只遍历有 `lora` 属性的模块**，所以天然不会存到 base 权重。

命名拼接在训练循环的保存段：

[trainer/train_lora.py:62-65](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/train_lora.py#L62-L65) —— `moe_suffix = '_moe' if lm_config.use_moe else ''`，路径为 `f'{args.save_dir}/{args.lora_name}_{lm_config.hidden_size}{moe_suffix}.pth'`。默认 `--lora_name lora_medical`、`hidden_size=768`，所以产出 `../out/lora_medical_768.pth`。

续训检查点走另一套命名（基于 `lora_name`）：

[trainer/train_lora.py:66](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/train_lora.py#L66) —— `lm_checkpoint(lm_config, weight=args.lora_name, ...)` 用 `weight=lora_name` 作为续训字典的键，把 model/optimizer/scaler/epoch/step/wandb_id 一并存进 `../checkpoints`（详见 u4-l2）。注意它存的是**完整训练状态**（含 base 权重引用），与 `save_lora` 存的**纯推理增量**是两份不同的文件。

> ⚠️ 一个值得留意的不对称：训练侧的 LoRA 文件名**带 `_moe` 后缀**（如 `lora_medical_768_moe.pth`），但推理侧 `eval_llm.py` 加载时**不带**后缀。见 [eval_llm.py:24-26](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/eval_llm.py#L24-L26)：路径是 `f'./{args.save_dir}/{args.lora_weight}_{args.hidden_size}.pth'`。这意味着若你训练了一个 MoE LoRA，直接用 `eval_llm.py` 组合推理会因文件名对不上而找不到权重，需要手动重命名。Dense 模型则无此问题。

#### 4.3.4 代码实践

**实践目标**：在不运行的前提下，预测你的训练会产出什么文件名。

**操作步骤**：

1. 阅读 [train_lora.py:81](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/train_lora.py#L81)（`--lora_name` 默认值）、[第 92 行](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/train_lora.py#L92)（`--hidden_size` 默认值）、[第 95 行](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/train_lora.py#L95)（`--use_moe` 默认值）。
2. 阅读命名拼接 [第 62-63 行](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/train_lora.py#L62-L63)。
3. 分别预测以下三种命令产出的文件名：
   - 默认参数（`python train_lora.py`）
   - `--lora_name lora_identity --hidden_size 512`
   - `--lora_name lora_medical --use_moe 1`

**需要观察的现象 / 预期结果**：
- 默认：`../out/lora_medical_768.pth`
- 第二种：`../out/lora_identity_512.pth`
- 第三种：`../out/lora_medical_768_moe.pth`

**验证**：若本地可运行，跑 `--save_interval` 很小（如 5）的极短训练，去 `out/` 目录确认文件名是否与预测一致（待本地验证）。

#### 4.3.5 小练习与答案

- **练习 1**：为什么 `save_lora` 要把每个张量 `.cpu().half()`？
  - **参考答案**：训练时参数可能在 GPU 上、且是 fp32/bf16；转 `.cpu()` 是为了脱离设备依赖（让权重文件能在任意机器加载），转 `.half()`（fp16）是为了把已经不到 1MB 的增量文件再压缩一半，同时 fp16 精度对这点旁路权重的推理影响可忽略。
- **练习 2**：`save_lora` 怎么保证不会误存 base 权重？
  - **参考答案**：它只遍历 `hasattr(module, 'lora')` 的模块，且只取 `module.lora.state_dict()`——也就是每个 LoRA 旁路的 `A.weight` / `B.weight`。base 的 `Linear` 自身权重根本不在遍历范围内，天然被排除。

---

### 4.4 垂域数据与「基模 + LoRA」组合推理

#### 4.4.1 概念说明

LoRA 最典型的用途是**垂域适配**：基模（`full_sft`）已经具备通用对话能力，但可能在某个领域（医疗、自我认知、客服……）回答得不够好。与其全参数微调（容易过拟合、损伤通用能力、需要谨慎混合数据），不如只在它上面叠一份针对该领域的小 LoRA 补丁。

MiniMind 内置两个最经典的垂域示例：

1. **`lora_medical`（医学）**：让模型更好地回答健康/疾病类问题。
2. **`lora_identity`（自我认知）**：让模型知道「我叫 MiniMind，由 Jingyao Gong 开发」，而不是幻觉成别的身份。

关键点：**垂域数据的格式与 SFT 完全一致**（`conversations` 字段的多轮对话），`train_lora.py` 直接复用 `SFTDataset`（见 [train_lora.py:149](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/train_lora.py#L149)）。区别只在于：这些数据被用来训练占比不到 1% 的旁路参数，而不是动整个模型。

推理时则是「基模权重 + LoRA 增量」组合加载：`eval_llm.py --weight full_sft --lora_weight lora_identity`，先加载基模，再 `apply_lora` 注入旁路结构，最后 `load_lora` 把训练好的增量填进去。

#### 4.4.2 核心流程

```text
训练：
  准备 dataset/lora_xxx.jsonl（conversations 格式）
  → cd trainer && python train_lora.py --lora_name lora_xxx --data_path ../dataset/lora_xxx.jsonl
  → 产出 out/lora_xxx_768.pth

推理（组合加载）：
  eval_llm.py --weight full_sft --lora_weight lora_xxx
    1. init_model: 加载 full_sft_768.pth 作为基模
    2. apply_lora(model): 注入空的 LoRA 旁路结构
    3. load_lora(model, 'out/lora_xxx_768.pth'): 把训练好的增量填进旁路
    4. 正常 generate
```

#### 4.4.3 源码精读

垂域数据格式（README 给出的样例）：

[README.md:779-792](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/README.md#L779-L792) —— 给出两类样例：垂域（医学问答）与自我认知（「你叫什么名字」「你是谁」）。每条都是 `{"conversations": [{"role":"user",...},{"role":"assistant",...}]}`，与 SFT 数据同构，所以 `SFTDataset` 能直接读。

`eval_llm.py` 的组合加载逻辑——这是本模块的核心：

[eval_llm.py:24-26](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/eval_llm.py#L24-L26) —— 当 `--lora_weight != 'None'` 时：先 `apply_lora(model)` 给基模注入旁路结构（此时旁路是随机/零初始化），再 `load_lora(model, f'./{args.save_dir}/{args.lora_weight}_{args.hidden_size}.pth')` 把训练好的增量权重填进去。这样基模的通用能力原封不动，只叠加了垂域增量。

对应的命令行参数：

[eval_llm.py:36-37](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/eval_llm.py#L36-L37) —— `--weight`（默认 `full_sft`）指定基模类型，`--lora_weight`（默认 `None`）指定要叠加的 LoRA 名称。README 特别提醒：`--weight` 必须与训练时 `--from_weight` 用的基模一致（默认都是 `full_sft`），否则维度对不上。

`load_lora` 如何把增量填回去——复习：

[model/model_lora.py:35-42](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/model/model_lora.py#L35-L42) —— 遍历所有挂了 `.lora` 的模块，从 `state_dict` 里挑出属于它的键，剥掉前缀后 `module.lora.load_state_dict(lora_state)`。注意它先做了 `module.` 前缀剥离（第 37 行），兼容 DDP 保存的权重。

> 想把 LoRA 永久合并进基模、导出成无分支的完整模型？用 `merge_lora`（[model/model_lora.py:56-65](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/model/model_lora.py#L56-L65)），它把 \(W \mathrel{+}= BA\) 后另存，详见 u6-l1 与 u8-l1 的 `convert_merge_base_lora`。

#### 4.4.4 代码实践

**实践目标**：体验「同一份基模，切换不同 LoRA 得到不同身份/能力」。

**操作步骤**：

1. 确认 `out/` 下有 `full_sft_768.pth`，以及训练好的 `lora_medical_768.pth`、`lora_identity_768.pth`（若没有，先按 4.4.2 训练）。
2. 在仓库根目录分别运行：
   ```bash
   # 不挂 LoRA（纯基模）
   python eval_llm.py --weight full_sft
   # 挂自我认知 LoRA
   python eval_llm.py --weight full_sft --lora_weight lora_identity
   # 挂医学 LoRA
   python eval_llm.py --weight full_sft --lora_weight lora_medical
   ```
3. 选择「[1] 手动输入」，分别问「你和 OpenAI 是什么关系？」与「我最近经常头晕，可能是什么原因？」。

**需要观察的现象**：
- 纯基模下，问身份可能得到含糊或幻觉回答。
- 挂 `lora_identity` 后，问身份应稳定回答「我是 MiniMind，由 Jingyao Gong 开发」（对应 README 第 803–805 行的样例）。
- 挂 `lora_medical` 后，问头晕应给出更具体的医学列举（对应 README 第 798–800 行样例）。

**预期结果**：同一个基模文件，仅靠切换 `--lora_weight` 即可改变模型的垂域表现，验证「基模 + LoRA 补丁」的可插拔设计。

> 若本地无权重，可改为阅读 [eval_llm.py:24-30](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/eval_llm.py#L24-L30) 与 README 样例输出，标注为「待本地验证」。

#### 4.4.5 小练习与答案

- **练习 1**：为什么垂域 LoRA 数据可以直接用 `SFTDataset` 加载，而不需要新建一个 `LoRADataset`？
  - **参考答案**：因为 LoRA 训练的目标函数与 SFT 完全相同——都是对 `assistant` 段做位移交叉熵（见 u2-l2 的 `generate_labels`）。LoRA 改变的只是「更新哪些参数」（旁路而非全模型），不改「数据怎么变张量、loss 怎么算」。所以数据侧完全可以复用 `SFTDataset`。
- **练习 2**：若训练时用 `--from_weight full_sft`，推理时却用 `--weight pretrain`，会发生什么？
  - **参考答案**：会出错或得到垃圾输出。`--weight` 决定基模权重文件（`pretrain_768.pth`），而 LoRA 增量是在 `full_sft` 基模上训练出来的；换成 `pretrain` 基模，两者的隐藏状态分布不一致，叠加的旁路增量不仅无效还可能有害。所以 README 强调 `--weight` 必须与训练时的 `--from_weight` 一致。

---

### 4.5 为什么 `train_lora.py` 自动禁用 `torch.compile`

#### 4.5.1 概念说明

你可能在 u4-l3 注意到，预训练脚本用 `torch.compile` 加速训练。但 `train_lora.py` 里有一段「自废武功」的代码：如果你传 `--use_compile 1`，它会**强制改成 0** 并打印一条警告。原因在于 `apply_lora` 的注入方式与 `torch.compile` 的工作机制天生冲突。

理解冲突需要分清两件事：

- **`torch.compile` 的工作方式**：它在第一次前向时**追踪（trace）**模型的计算图——即按模块的类定义里的 `forward` 方法，把算子拼成一张静态图，再做内核融合。它认的是「**类定义层面**的方法」。
- **`apply_lora` 的工作方式**：它在运行时给每个方阵 `Linear` 的**实例**绑定一个全新的 `forward` 函数（`module.forward = forward_with_lora`），这是「**实例层面**的 monkey-patch」。

问题就出在这里：`torch.compile` 追踪的是类定义里的原始 `forward`（不含 LoRA 旁路），而实例上被替换的新 `forward`（含旁路）不在它追踪的静态图里。于是编译后的图会**漏掉 LoRA 分支**——要么旁路不参与前向（训练了个寂寞），要么干脆因为图不一致报错。`train_lora.py` 选择最稳妥的做法：直接禁用 compile。

#### 4.5.2 核心流程

```text
if args.use_compile == 1:
    args.use_compile = 0           # 强制关闭
    Logger('monkey-patch forward 与 torch.compile 不兼容，use_compile 已自动关闭')
# 之后正常的 DDP 包装、训练循环，不再走 compile 分支
```

#### 4.5.3 源码精读

自动禁用 compile 的代码：

[trainer/train_lora.py:163-166](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/train_lora.py#L163-L166) —— 这是 `__main__` 的第 8 步「编译和分布式包装」。一旦检测到 `use_compile==1`，立即置 0 并打日志。注意它不像预训练那样真的去调 `torch.compile`，而是直接放弃。

回顾冲突的根源——`apply_lora` 的 monkey-patch：

[model/model_lora.py:26-32](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/model/model_lora.py#L26-L32) —— `original_forward = module.forward` 保存原方法，然后 `module.forward = forward_with_lora` 把实例方法替换成「原输出 + 旁路输出」。正是这行实例赋值，让 `torch.compile` 的静态追踪看不见 LoRA 分支。

作为对比，`eval_llm.py` 同样调用 `apply_lora`（[第 25 行](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/eval_llm.py#L25)），但推理脚本本来就不 compile，所以推理侧用 LoRA 没有这个限制。

#### 4.5.4 代码实践

**实践目标**：触发自动禁用警告，验证「即便用户强制开启 compile 也会被关掉」。

**操作步骤**：

1. 在 `trainer` 目录运行（哪怕没有数据，只要能跑到第 8 步即可；若担心报错，可临时把 `--epochs 0` 或准备极小数据）：
   ```bash
   cd trainer && python train_lora.py --use_compile 1 --log_interval 1
   ```
2. 观察启动日志。

**需要观察的现象**：日志中应出现一行 `[LoRA] monkey-patch forward 与 torch.compile 不兼容，use_compile 已自动关闭`。

**预期结果**：即便命令行传了 `--use_compile 1`，程序仍按「不 compile」的方式继续，证明这是脚本主动的安全降级，而非用户可选项。

> 若本地无法运行到该步，直接阅读 [第 163-166 行](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/trainer/train_lora.py#L163-L166) 即可确认行为（待本地验证）。

#### 4.5.5 小练习与答案

- **练习 1**：如果不做这层自动禁用，强行对一个 LoRA 模型 `torch.compile`，最可能出现哪种问题？
  - **参考答案**：编译期追踪到的是类定义里的原始 `Linear.forward`（不含旁路），所以编译后的前向**不会执行 LoRA 分支**——反向时 LoRA 参数根本拿不到梯度，训练等于没训；或者因为实例 `forward` 与追踪图不一致直接抛错。两种情况都使 LoRA 失效。
- **练习 2**：如果想让 LoRA 也能享受 `torch.compile` 加速，可以怎么改？
  - **参考答案**（开放）：放弃 monkey-patch，改用**子类化 / 注册为新模块**的方式，把 `y = Wx + BAx` 写进模块的类定义 `forward` 里，让 compile 能在类层面追踪到完整图。代价是 `model_lora.py` 的实现要从「运行时改写实例」重构成「定义新 Linear 子类并替换」，侵入性更大。这是工程上「灵活性 vs 可编译性」的典型取舍。

---

## 5. 综合实践：训练一个「自我认知」LoRA 并组合推理

本任务把本讲五个模块串起来：准备数据 → 训练 → 组合推理 → 观察效果。

### 5.1 实践目标

让一个 `full_sft` 基模通过叠加极小的 LoRA 补丁，学会稳定的自我认知（「我是 MiniMind，由 Jingyao Gong 开发」），并验证「基模 + LoRA」可插拔。

### 5.2 操作步骤

1. **准备数据**。在 `dataset/` 下新建 `lora_identity.jsonl`，写入若干条自我认知对话（格式严格遵循 [README.md:786-792](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/README.md#L786-L792)），示例（示例代码，非项目原有文件）：
   ```jsonl
    {"conversations": [{"role": "user", "content": "你叫什么名字？"}, {"assistant": true, "role": "assistant", "content": "您好，我名叫 MiniMind，是由 Jingyao Gong 开发的人工智能助手。"}]}
   ```
   > 注意：实际写入时去掉示例里多余的 `"assistant": true` 字段，保持与 README 一致的 `{"role","content"}` 两字段结构。建议至少准备 20–50 条多样化的问法（「你是谁」「介绍下你自己」「你和 ChatGPT 什么关系」等）。
2. **训练**。在 `trainer` 目录运行：
   ```bash
   cd trainer && python train_lora.py \
       --lora_name lora_identity \
       --data_path ../dataset/lora_identity.jsonl \
       --from_weight full_sft \
       --epochs 10 --learning_rate 1e-4 \
       --save_interval 100
   ```
   训练前确认日志打印的 `LoRA 参数占比` 在 1% 以下（验证 4.1）；训练中观察 `logits_loss` 下降（验证 4.2）。
3. **确认产物**。检查 `out/lora_identity_768.pth` 是否生成（验证 4.3 命名规则）。
4. **组合推理**。回仓库根目录：
   ```bash
   python eval_llm.py --weight full_sft --lora_weight lora_identity
   ```
   选择「[1] 手动输入」，问「你是谁」「你叫什么名字」。

### 5.3 需要观察的现象

- 训练日志：`LoRA 参数占比` ≈ 0.6%；`logits_loss` 随 step 下降。
- 推理效果：挂 `lora_identity` 后，模型对身份问题稳定回答「我是 MiniMind，由 Jingyao Gong 开发」类内容；而不挂 LoRA（`python eval_llm.py --weight full_sft`）时回答可能含糊。
- 切换实验：用同一基模再挂 `lora_medical`（若已训练），问医学问题，观察身份回答是否被「冲掉」——理论上不会，因为两份 LoRA 互不干扰（验证「可插拔」）。

### 5.4 预期结果

得到一份不到 1MB 的 `lora_identity_768.pth`，叠加在 `full_sft` 基模上即可改变模型的自我认知回答，且不破坏基模的通用对话能力。

> 若本地无 GPU 或无 `full_sft_768.pth` 权重：可把 `--device cpu` 跑极少量数据（README 第 766 行注明 LoRA 在 CPU 上通常也能轻快完成），或退化为「源码阅读型实践」——逐行走读 `train_lora.py` 第 128–181 行，在纸上画出从 `apply_lora` 到 `save_lora` 的完整数据流，并标注每个模块对应的行号。相关步骤标注为「待本地验证」。

---

## 6. 本讲小结

- `train_lora.py` 用**双重保险**确保只更新 LoRA 参数：`requires_grad=False` 冻结 base（省显存），`AdamW(lora_params)` 只把旁路参数喂给优化器（限定更新对象）。
- `train_epoch` 与 Full SFT 的 `train_epoch` **几乎逐行同构**，差异只有两处：梯度裁剪对象是 `lora_params`、保存调用 `save_lora`。LoRA 不是新算法，而是已有训练骨架的最小改写。
- LoRA 默认学习率 `1e-4`，是 Full SFT（`1e-5`）的 10 倍——因为可训练参数不到 1%（约 0.39M / 64M ≈ 0.6%），需要更大步长。
- 权重命名规则为 `lora_xxx_{dim}{_moe?}.pth`；`save_lora` 只存旁路的 `A`/`B` 权重（fp16、剥外壳），文件极小；推理时由 `eval_llm.py --weight full_sft --lora_weight lora_xxx` 组合加载。
- 垂域数据复用 `SFTDataset`，格式与 SFT 完全一致（`conversations`）；典型场景是 `lora_medical`（医学）与 `lora_identity`（自我认知）。
- `torch.compile` 与 `apply_lora` 的 monkey-patch forward 不兼容（静态追踪看不见实例级新 `forward`），`train_lora.py` 检测到 `--use_compile 1` 会**自动关闭**并告警。

---

## 7. 下一步学习建议

- **若想把 LoRA 永久合并进基模导出**：阅读 u8-l1（模型格式转换与生态对接），重点看 `scripts/convert_model.py` 的 `convert_merge_base_lora`，它调用的正是本讲提到的 [merge_lora](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/model/model_lora.py#L56-L65)。
- **若想进入「后训练」阶段**：LoRA 是参数高效微调的终点之一，之后项目进入强化学习后训练——建议接着学 u6-l3（白盒知识蒸馏）作为从监督学习到 RL 的过渡，或直接进入 u7-l1（DPO 与策略优化统一视角）。
- **源码延伸阅读**：对比 `trainer/train_lora.py` 与 `trainer/train_full_sft.py` 的 diff，是巩固「LoRA = Full SFT 骨架 + 只放开旁路」这一结论的最佳练习；再读 [model/model_lora.py](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/model/model_lora.py) 全文，把 u6-l1 的「实现」与本讲的「训练」在脑中连成一体。
