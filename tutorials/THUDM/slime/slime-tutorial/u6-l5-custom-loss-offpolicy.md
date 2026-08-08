# 自定义损失、TIS 与 off-policy 修正

## 1. 本讲目标

本讲解决一个在 RL 后训练中很容易被忽视、却会悄悄让训练崩溃的问题：**off-policy（离策略）偏差**。当你用 SGLang 在某一时刻采样、又用 Megatron 在另一时刻算损失时，数据其实已经不完全「新鲜」了。学完本讲你应当能够：

- 用 `--custom-loss-function-path`（配合 `--loss-type custom_loss`）**整体替换** slime 的损失函数，并知道它和默认 `policy_loss_function` 的接驳点。
- 理解 **TIS（Truncated Importance Sampling，截断重要性采样）**：为什么需要用「训练引擎」和「rollout 引擎」两次对数概率算出一个重要性比，以及 `vanilla_tis_function` 的 ratio 表达式。
- 掌握 `--custom-tis-function-path` 的契约，能对照 `examples/train_infer_mismatch_helper` 写出自己的 IS/RS 修正。
- 看懂 **OPSM（Off-Policy Sequence Masking）**：在「优势为负且序列 KL 过大」时整段掩码掉，并写出 `compute_opsm_mask` 的判据。
- 区分 **CISPO / GSPO** 这两种「让损失更稳定」的变体与普通 PPO clip 的差别。

本讲建立在 [u6-l4 优势估计器与 RL 算法选择](u6-l4-advantage-estimators.md) 之上——你需要先理解 advantage 是怎么算出来的、以及 `policy_loss_function` 的 PPO clip 骨架。本讲只讲「损失怎么算」之后的**自定义与修正**层。

## 2. 前置知识

### 2.1 三套对数概率（务必分清）

slime 的损失函数里同时出现三种 `log_probs`，初学者最容易混淆。本讲几乎所有的 ratio 都来自它们两两之差：

| 名称 | 来自哪里 | 是否带梯度 | 作用 |
|------|---------|-----------|------|
| `log_probs`（当前策略） | 当前 step 用 Megatron 前向算出 | **是**（唯一带梯度） | 优化目标 |
| `old_log_probs`（旧策略/近端锚） | step 开始时 Megatron 重算（`batch["log_probs"]`） | 否 | PPO clip 的「近端策略」分母 |
| `rollout_log_probs`（行为策略） | **采样当时** SGLang 记录在 Sample 里 | 否 | 真正「生成这批数据」的策略 |

理想情况下三者应当非常接近。但只要 rollout 引擎（SGLang）和训练引擎（Megatron）不完全一致，或者异步训练下样本「过期」，`rollout_log_probs` 就会偏离 `old_log_probs`——这就是 off-policy 偏差的来源。本讲的核心就是**用 `rollout_log_probs` 去修正这个偏差**。

> 提示：[u6-l4](u6-l4-advantage-estimators.md) 已经讲过 `ref_log_probs`（参考策略，用于 KL 惩罚），它和上面三者都不同，别混进来。

### 2.2 重要性采样（IS）的一句话直觉

如果你在分布 \(q\) 下采到了数据，却想在分布 \(p\) 下求期望，就得给每个样本乘一个权重 \(\rho = p/q\)。在 RL 里，\(q\) 是「实际采样策略」\(\pi_{\text{rollout}}\)，\(p\) 是「你假设的采样策略」（通常是 \(\pi_{\text{old}}\) 或 \(\pi_\theta\)）。这个 \(\rho\) 就是本讲的「重要性比」。问题在于 \(\rho\) 的方差可能爆炸，所以要做**截断/裁剪**——这就是 TIS。

### 2.3 slime 的损失分发骨架

所有损失都从 [slime/backends/megatron_utils/loss.py](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/loss.py) 的 `loss_function` 进来，它根据 `args.loss_type` 把请求分发给具体函数，最后再做 Megatron 梯度累积的缩放。本讲的所有自定义/修正都发生在这一层之下。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [slime/backends/megatron_utils/loss.py](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/loss.py) | 损失分发 `loss_function`、默认 `policy_loss_function`、`vanilla_tis_function`、`icepop_function` |
| [slime/utils/ppo_utils.py](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/utils/ppo_utils.py) | `compute_opsm_mask`、`compute_policy_loss`、`compute_cispo_loss`、`compute_gspo_kl`、`compute_approx_kl` |
| [slime/utils/arguments.py](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/utils/arguments.py) | `--loss-type`、`--custom-loss-function-path`、`--use-tis`、`--tis-clip*`、`--custom-tis-function-path`、`--use-opsm`、`--opsm-delta`、CISPO 校验 |
| [examples/train_infer_mismatch_helper/mis.py](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/examples/train_infer_mismatch_helper/mis.py) | 生产级 MIS/TIS 实现，含可在 CPU 单测的 `compute_mis_weights` |
| [examples/train_infer_mismatch_helper/run-qwen3-4b-mis.sh](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/examples/train_infer_mismatch_helper/run-qwen3-4b-mis.sh) | 端到端启用 TIS 的启动脚本 |
| [examples/train_infer_mismatch_helper/README.md](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/examples/train_infer_mismatch_helper/README.md) | 三种 off-policy 算法（标准 PPO / 绕过 / 解耦 3 策略）的数学说明 |

---

## 4. 核心概念与源码讲解

### 4.1 custom_loss_function：整体替换损失函数

#### 4.1.1 概念说明

slime 默认的 `--loss-type policy_loss` 走的是 `policy_loss_function`（PPO/GRPO 那一套）。但如果你想做一件**结构上完全不同**的事——比如新的 RL 目标、多目标优化、特殊正则项——你不必去 fork 框架，只要把 `--loss-type` 设成 `custom_loss`，再用 `--custom-loss-function-path` 指向你自己的函数即可。

这就是 slime「骨架写死、肉可替换」哲学（见 [u6-l1](u6-l1-customization-overview.md)）在**损失层**的体现：框架负责把 logits、batch、归约函数喂给你，你怎么算 loss 完全自由。

#### 4.1.2 核心流程

1. 命令行 `--loss-type custom_loss --custom-loss-function-path my_module.my_loss`。
2. `loss_function` 用 `match args.loss_type` 分发，命中 `custom_loss` 分支时 `load_function` 把 import 路径解析成函数对象。
3. 框架以**统一签名**调用你的函数：`(args, batch, logits, sum_of_sample_mean) -> (loss, metrics_dict)`。
4. 返回的标量 `loss` 被 `loss_function` 按 `num_microbatches / step_global_batch_size` 缩放，塞回 Megatron 流水线。

注意：替换的是**整个损失函数**，而不是在默认损失上「加一项」。如果你想保留 PPO 骨架只做小修正，应该用后面 4.2/4.3 的 TIS / OPSM 接口，而不是 custom_loss。

#### 4.1.3 源码精读

分发逻辑在 `loss_function` 的 `match` 块里，`custom_loss` 分支通过 `load_function` 加载用户函数：

```python
match args.loss_type:
    case "policy_loss":
        func = policy_loss_function
    case "value_loss":
        func = value_loss_function
    case "sft_loss":
        func = sft_loss_function
    case "custom_loss":
        func = load_function(args.custom_loss_function_path)
    case _:
        raise ValueError(f"Unknown loss type: {args.loss_type}")
```

——分发逻辑见 [slime/backends/megatron_utils/loss.py:1264-1274](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/loss.py#L1264-L1274)，这里根据 `loss_type` 选择损失函数，`custom_loss` 分支用 `load_function` 把字符串路径变成可调用对象。

调用点（注意可选的梯度检查点重算）：

```python
if args.recompute_loss_function:
    loss, log = checkpoint(func, args, batch, logits, sum_of_sample_mean, use_reentrant=False)
else:
    loss, log = func(args, batch, logits, sum_of_sample_mean)
```

——见 [slime/backends/megatron_utils/loss.py:1276-1279](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/loss.py#L1276-L1279)：你的函数被以 `(args, batch, logits, sum_of_sample_mean)` 四参数调用，必须返回 `(loss, log)`，其中 `log` 是指标字典。

参数声明：

——`--loss-type` 限定为 `["policy_loss", "sft_loss", "custom_loss"]`，见 [slime/utils/arguments.py:903-912](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/utils/arguments.py#L903-L912)；
——`--custom-loss-function-path` 默认 `None`，见 [slime/utils/arguments.py:913-921](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/utils/arguments.py#L913-L921)。

> 示例代码：下面是一个最小可用的 custom_loss 骨架（**不是**项目原有代码），签名必须与默认函数对齐：

```python
# 示例代码：my_loss.py
def my_loss(args, batch, logits, sum_of_sample_mean):
    # batch 含 "advantages" "loss_masks" "unconcat_tokens" 等；
    # logits 形状 [1, T, V]，是当前策略输出。
    # 你需要自己算 log_probs（可复用 slime 自带工具），
    # 然后返回 (标量 loss, {"loss": loss.detach()})
    ...
    return loss, {"loss": loss.clone().detach()}
```

#### 4.1.4 代码实践

**目标**：不写新损失，只确认 custom_loss 的接驳点与签名。

**步骤**：

1. 打开 [slime/backends/megatron_utils/loss.py:1220-1320](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/loss.py#L1220-L1320)（`loss_function` 全文）。
2. 找到 `match args.loss_type` 块，确认 `custom_loss` 分支。
3. 对照默认的 [policy_loss_function](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/loss.py#L881-L910)（docstring 在此）记录它的入参和返回结构——你的 custom_loss 必须遵守同一契约。
4. 阅读 `loss_function` 末尾的缩放逻辑：`loss * num_microbatches / step_global_batch_size * dp_world_size`（[L1290-L1298](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/loss.py#L1290-L1298)），理解为什么你返回的 `loss` 不用自己缩放。

**观察/预期**：你会看到 custom_loss 与默认 loss 走的是**完全相同**的下游缩放与指标打包路径；唯一区别是 `func` 这个变量被换成了你的函数。如果你返回的 `log` 字典里缺少某些 key（如 `pg_clipfrac`），wandb 上对应的指标面板就会空——这是正常的。

#### 4.1.5 小练习与答案

**练习 1**：如果只设了 `--custom-loss-function-path` 却忘了 `--loss-type custom_loss`，会发生什么？

**答案**：`loss_type` 仍是默认 `policy_loss`，`match` 会选中 `policy_loss_function`，你的自定义函数**根本不会被调用**；`--custom-loss-function-path` 只在 `custom_loss` 分支里被读取。这是一个静默错误——命令行不会报错。

**练习 2**：为什么 custom_loss 适合「整体替换」而不是「在 PPO 上加正则」？

**答案**：因为 `custom_loss` 分支**绕过**了 `policy_loss_function`，PPO clip、entropy、KL loss、TIS、OPSM 这些都默认不会执行。若只是想加一项正则，应在 custom_loss 内部自己把整套 PPO loss 复刻出来再加，或者干脆别用 custom_loss，改用框架内置的 `--use-kl-loss` / entropy_coef 等开关。

---

### 4.2 vanilla_tis_function：截断重要性采样修正 off-policy

#### 4.2.1 概念说明

这是本讲的**核心模块**，也是实践任务的主角。

**问题动机**：标准 PPO 的损失里，ratio 是 \(\pi_\theta/\pi_{\text{old}}\)，其中 \(\pi_{\text{old}}\) 是训练引擎在 step 开头重算的。但它默认**假设数据是 \(\pi_{\text{old}}\) 采的**。事实未必：

1. **train-infer mismatch**：SGLang 和 Megatron 是两套实现，即便「同权重」，对同一 token 给出的 logprob 也未必逐位相等。数据其实是 SGLang（\(\pi_{\text{rollout}}\)）采的。
2. **staleness（过期）**：异步训练下，采样用的权重可能是几步之前的，\(\pi_{\text{rollout}}\) 已偏离当前 \(\pi_{\text{old}}\)。

这两种情况都会让 PPO 的梯度估计**有偏**。解决办法是再补一个重要性比 \(\pi_{\text{old}}/\pi_{\text{rollout}}\)，把它乘到 pg_loss 上——这就是 **TIS（Truncated Importance Sampling）**。「Truncated」是因为这个比值的方差会爆炸（尤其是序列级），必须截断/裁剪。

slime 的 `vanilla_tis_function` 是最朴素的内置实现；`--custom-tis-function-path` 让你换成更复杂的版本（如 MIS，见 4.2.3 的例子）。

#### 4.2.2 核心流程

1. `--use-tis` 打开 TIS；进入 `policy_loss_function` 的 TIS 块。
2. 框架准备好 `tis_kwargs`，把 **`train_log_probs`**（训练引擎重算的 old logp）、**`rollout_log_probs`**（采样时 SGLang 记录的）、`loss_masks` 等打包传给 tis 函数。
3. 若设了 `--custom-tis-function-path` 用之，否则用 `vanilla_tis_function`。
4. tis 函数返回 `(pg_loss, modified_response_masks, metrics)`：其中 `pg_loss` 已被乘上 IS 权重。
5. 框架用返回的 `modified_response_masks` **重建归约函数**（剔除被拒绝的 token），再对 pg_loss 做归约。

**ratio 表达式**（本讲实践任务的核心）：

\[ \rho_t \;=\; \frac{\pi_{\text{old}}(a_t\mid s_t)}{\pi_{\text{rollout}}(a_t\mid s_t)} \;=\; \exp\!\bigl(\log\pi_{\text{old},t} - \log\pi_{\text{rollout},t}\bigr) \]

其中 \(\pi_{\text{old}}\) 来自**训练引擎（Megatron）重算**的对数概率，\(\pi_{\text{rollout}}\) 来自**rollout 引擎（SGLang）采样时**记录的对数概率——正是「两次 logp」。

把它乘到 PPO surrogate 上，得到**解耦三策略 PPO**：

\[ L = -\underbrace{\frac{\pi_{\text{old}}}{\pi_{\text{rollout}}}}_{\text{TIS 权重}\,\rho} \cdot \min\!\left(\frac{\pi_\theta}{\pi_{\text{old}}}A,\;\text{clip}\!\left(\frac{\pi_\theta}{\pi_{\text{old}}},1-\epsilon,1+\epsilon\right)A\right) \]

PPO 的 clip 仍以 \(\pi_{\text{old}}\) 为锚（控制单步更新幅度），而 \(\rho\) 在外层修正「数据其实来自 \(\pi_{\text{rollout}}\)」的偏差。

#### 4.2.3 源码精读

**内置实现 `vanilla_tis_function`**——ratio 在这里直接写出：

```python
def vanilla_tis_function(args, *, pg_loss, train_log_probs, rollout_log_probs, loss_masks, **kwargs):
    rollout_log_probs = torch.cat(rollout_log_probs, dim=0)
    old_log_probs = torch.cat(train_log_probs, dim=0)
    tis = torch.exp(old_log_probs - rollout_log_probs)        # ρ = exp(logπ_old − logπ_rollout)
    tis_abs = (torch.exp(old_log_probs - rollout_log_probs) - 1).abs()
    tis_weights = torch.clamp(tis, min=args.tis_clip_low, max=args.tis_clip)   # 截断/裁剪
    tis_clipfrac = (tis_weights != tis).float()
    metrics = {"tis": tis.clone().detach(),
               "tis_clipfrac": tis_clipfrac.clone().detach(),
               "tis_abs": tis_abs.clone().detach()}
    pg_loss = pg_loss * tis_weights                            # 把 IS 权重乘到 pg_loss 上
    return pg_loss, loss_masks, metrics
```

——见 [slime/backends/megatron_utils/loss.py:831-852](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/loss.py#L831-L852)。注意 `tis = torch.exp(old_log_probs - rollout_log_probs)` 这一行就是上面 ratio 表达式的直译：`old_log_probs` 即 `train_log_probs`（训练引擎重算的 \(\pi_{\text{old}}\)），`rollout_log_probs` 是 SGLang 记录的 \(\pi_{\text{rollout}}\)）。

**调用点与契约**——`policy_loss_function` 里 TIS 块如何打包 kwargs 并调用：

```python
if args.get_mismatch_metrics or args.use_tis:
    ...
    assert "rollout_log_probs" in batch, "rollout_log_probs must be provided for TIS"
    ois = (-ppo_kl).exp()                       # 标准PPO比 π_θ/π_old，仅作指标
    tis_kwargs = {
        "args": args,
        "pg_loss": pg_loss,
        "train_log_probs": train_log_probs_for_tis,    # 训练引擎重算
        "rollout_log_probs": batch["rollout_log_probs"],  # SGLang 记录
        "loss_masks": batch["loss_masks"],
        "total_lengths": total_lengths,
        "response_lengths": response_lengths,
    }
    if args.custom_tis_function_path is not None:
        tis_func = load_function(args.custom_tis_function_path)
    else:
        tis_func = vanilla_tis_function
    pg_loss, modified_response_masks, tis_metrics = tis_func(**tis_kwargs)
```

——见 [slime/backends/megatron_utils/loss.py:987-1015](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/loss.py#L987-L1015)。关键点：①`train_log_probs_for_tis` 取自 `batch.get("log_probs")`，即**训练引擎重算**的对数概率；②`rollout_log_probs` 取自 `batch["rollout_log_probs"]`，即**采样当时**记录的；③无论用默认还是自定义 tis 函数，签名都通过 `**tis_kwargs` 统一。

**MIS（生产级自定义实现）**——`examples/train_infer_mismatch_helper/mis.py` 的 `compute_mis_weights` 把同一个 ratio 扩展到三种粒度：

```python
raw_log_ratio_diff = train_log_prob - rollout_log_prob   # log(π_old/π_rollout)

def compute_log_ratio(raw_log_diff, mask, level):
    if level == "token":           # 逐 token：ρ_t = exp(diff_t)
        return raw_log_diff
    elif level == "sequence":      # 序列乘积：ρ = exp(Σ diff_t) = Π ρ_t
        return masked_sum(raw_log_diff, mask, expand=True)
    elif level == "geometric":     # 几何均值：ρ = exp(mean(diff_t)) = (Πρ_t)^(1/n)
        return masked_mean(raw_log_diff, mask, expand=True)
```

——见 [examples/train_infer_mismatch_helper/mis.py:192-200](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/examples/train_infer_mismatch_helper/mis.py#L192-L200)（`compute_log_ratio` 分支）与 [examples/train_infer_mismatch_helper/mis.py:216-241](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/examples/train_infer_mismatch_helper/mis.py#L216-L241)（IS 权重计算）。三种 level 共享同一个 `raw_log_ratio_diff = train_log_prob - rollout_log_prob`，差别只在 token/sequence/geometric 的聚合方式。

**带 CP（context parallel）的入口**——真正注册给 `--custom-tis-function-path` 的是 `compute_mis_weights_with_cp`：

```python
def compute_mis_weights_with_cp(args, *, pg_loss, train_log_probs, rollout_log_probs,
                                loss_masks, total_lengths, response_lengths, **kwargs):
    from slime.backends.megatron_utils.cp_utils import all_gather_with_cp, slice_log_prob_with_cp
    # 先 all_gather 拼回完整序列，算 IS 权重，再切回本 CP rank 的分片
    ...
    is_weights, modified_masks, is_metrics = compute_mis_weights(...)
    if is_weights is not None:
        is_weights = slice_cp_and_concat(is_weights, total_lengths, response_lengths)
        pg_loss = pg_loss * is_weights
    return pg_loss, modified_masks, result_metrics
```

——见 [examples/train_infer_mismatch_helper/mis.py:310-380](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/examples/train_infer_mismatch_helper/mis.py#L310-L380)。CP 模式下每个 rank 只看到序列的一片，所以先 `all_gather_with_cp` 拼整条、在整条上算 ratio、再 `slice_log_prob_with_cp` 切回本 rank；最终同样把 `is_weights` 乘到 `pg_loss` 上。

**启动脚本里的启用方式**——`run-qwen3-4b-mis.sh`：

```bash
GRPO_ARGS=( ... --use-tis )                                  # 打开 TIS
CUSTOM_ARGS=(
   --custom-config-path examples/train_infer_mismatch_helper/mis.yaml
   --custom-tis-function-path examples.train_infer_mismatch_helper.mis.compute_mis_weights_with_cp
)
```

——见 [examples/train_infer_mismatch_helper/run-qwen3-4b-mis.sh:88](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/examples/train_infer_mismatch_helper/run-qwen3-4b-mis.sh#L88)（`--use-tis`）与 [examples/train_infer_mismatch_helper/run-qwen3-4b-mis.sh:123-126](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/examples/train_infer_mismatch_helper/run-qwen3-4b-mis.sh#L123-L126)（自定义 tis 路径与 YAML 配置）。

**参数定义**：`--use-tis`/`--tis-clip`/`--tis-clip-low`/`--custom-tis-function-path` 见 [slime/utils/arguments.py:1049-1072](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/utils/arguments.py#L1049-L1072)（默认 `tis-clip=2.0`、`tis-clip-low=0`）；`use_tis` 与 `use_rollout_logprobs` 互斥、`get_mismatch_metrics` 必须配 custom tis 路径，见 [slime/utils/arguments.py:1795-1801](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/utils/arguments.py#L1795-L1801)。

#### 4.2.4 代码实践（本讲指定实践）

**目标**：对照 `examples/train_infer_mismatch_helper`，说明 `--custom-tis-function-path` 如何用「rollout 与训练两次 logp」修正 off-policy，并亲手算出 ratio。本实践**纯 CPU 可运行**。

**步骤**：

1. 用 Python 构造两条假对数概率，模拟「训练引擎重算」与「rollout 引擎记录」的差异：

```python
# 示例代码：可在任意带 torch 的 CPU 环境运行
import torch
train_log_probs  = torch.tensor([-1.0, -0.8, -1.2])   # 训练引擎(Megatron)重算 = π_old
rollout_log_probs= torch.tensor([-1.1, -0.7, -1.5])   # 采样时(SGLang)记录   = π_rollout

tis = torch.exp(train_log_probs - rollout_log_probs)  # ratio: π_old / π_rollout
print(tis)        # tensor([1.1052, 0.9048, 1.3499])
```

2. 写出 ratio 表达式并解释每一项：

\[ \rho_t = \exp(\log\pi_{\text{train}}(a_t) - \log\pi_{\text{rollout}}(a_t)) \]

   - \(\log\pi_{\text{train}}\)：训练引擎在 step 开头对这条轨迹**重算**的对数概率（`train_log_probs_for_tis`，来自 `batch["log_probs"]`）。
   - \(\log\pi_{\text{rollout}}\)：采样当时 SGLang **记录**在 Sample.rollout_log_probs 里的对数概率（`batch["rollout_log_probs"]`）。
   - 两者之差就是 off-policy 偏差；exp 后得到把期望从 \(\pi_{\text{rollout}}\) 搬到 \(\pi_{\text{old}}\) 的重要性权重。

3. （进阶）调用真实实现 `compute_mis_weights` 验证。该函数刻意不依赖 Megatron，可 CPU 单测（见 [mis.py 顶部 NOTE](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/examples/train_infer_mismatch_helper/mis.py#L5-L9)）：

```python
# 示例代码
from argparse import Namespace
from examples.train_infer_mismatch_helper.mis import compute_mis_weights
args = Namespace(use_tis=True, use_rs=False,
                 tis_level="token", tis_mode="truncate",
                 tis_upper_bound=2.0, tis_lower_bound=0.5,
                 tis_batch_normalize=False)
loss_mask = torch.ones(3)
w, mod_mask, metrics = compute_mis_weights(
    args,
    train_log_probs=[train_log_probs],
    rollout_log_probs=[rollout_log_probs],
    loss_masks=[loss_mask],
)
print(w)   # 截断到 [., 2.0] 的 IS 权重
```

**预期结果**：步骤 1 的 `tis` 约为 `[1.105, 0.905, 1.350]`——围绕 1 波动，说明两引擎差异不大；若某项远大于 1（如 >2.0），truncate 模式会把它压回 2.0，这正是「截断」控制方差的体现。`get_mismatch_metrics` 用户会发现步骤 3 仍能跑：因为 `use_tis=True` 时 `compute_mis_weights` 会算权重；若只看指标不开 TIS，它会在 [mis.py:208-209](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/examples/train_infer_mismatch_helper/mis.py#L208-L209) 提前返回只含 mismatch 指标的结果。

**若无法本地运行**：标注「待本地验证」——尤其步骤 3 需要 `examples/` 在 `PYTHONPATH` 中可 import。

#### 4.2.5 小练习与答案

**练习 1**：`vanilla_tis_function` 里 `tis = exp(old - rollout)`，为什么不是 `exp(rollout - old)`？

**答案**：因为我们要把期望从「采样分布 \(\pi_{\text{rollout}}\)」搬到「锚定分布 \(\pi_{\text{old}}\)」，IS 权重是 \(p/q = \pi_{\text{old}}/\pi_{\text{rollout}}\)，所以是 \(\exp(\log\pi_{\text{old}} - \log\pi_{\text{rollout}})\)。写反了会把偏差方向取反，让训练朝错误方向放大。

**练习 2**：`--use-tis` 和 `--use-rollout-logprobs` 为什么互斥（见 [arguments.py:1795-1796](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/utils/arguments.py#L1795-L1796)）？

**答案**：`--use-rollout-logprobs` 是「绕过」方案——直接拿 \(\pi_{\text{rollout}}\) 当 PPO 的 old 分母（ratio 变成 \(\pi_\theta/\pi_{\text{rollout}}\)），不再重算 old，也就没有 \(\pi_{\text{old}}/\pi_{\text{rollout}}\) 这一项可乘。而 TIS 恰恰需要同时有 \(\pi_{\text{old}}\) 和 \(\pi_{\text{rollout}}\) 才能算 ρ，两者逻辑冲突，故互斥。

**练习 3**：token / sequence / geometric 三种 level，哪个方差最大？

**答案**：sequence。它是 \(\prod_t \rho_t\)，token 级的小偏差会连乘放大，方差爆炸；geometric 取几何均值（\(\prod\rho_t\)^{1/n}）把量级压回 1 附近，方差最小但有偏；token 居中。这也是 MIS 提供 truncate/clip/mask 三种处理方式的原因——sequence level 尤其需要激进截断。

---

### 4.3 compute_opsm_mask：序列级 off-policy 掩码

#### 4.3.1 概念说明

TIS 用「乘权重」温和地修正偏差，而 **OPSM（Off-Policy Sequence Masking）** 更激进：直接把某些**整条序列**从损失里抹掉。

判据是一个逻辑与：当一条序列**优势为负**（模型本就该远离它）**且**它相对当前策略的**序列级 KL 已经很大**（策略已经漂移远了）时，就掩掉它。直觉是：对一个「坏」的序列，如果策略已经离它足够远，继续用力把它推得更远既无收益（梯度方向不稳）又有风险（可能把分布带偏），不如直接放弃这条样本。

OPSM 与 TIS 可以**同时开启**：TIS 在 token 级调权，OPSM 在序列级做硬掩码，二者正交。

#### 4.3.2 核心流程

1. `--use-opsm`（阈值 `--opsm-delta`，默认 `1e-4`）打开。
2. `policy_loss_function` 检测到 `args.use_opsm`，先把 log_probs 和 old_log_probs 做 `all_gather_with_cp` 拼成完整序列（CP 下必须）。
3. 调 `compute_opsm_mask` 逐序列算序列级 KL、生成 `opsm_mask`。
4. `pg_loss = pg_loss * opsm_mask` 把被判定为「该丢」的序列清零。
5. 掩码比例 `opsm_clipfrac` 作为指标上报。

判据（数学表达）：

\[ \text{seq\_kl}_i = \frac{\sum_t (\log\pi_{\text{old},t} - \log\pi_{\theta,t})\, m_t}{\max(\sum_t m_t,\,1)} \]

\[ \text{drop}_i = \mathbb{1}\bigl[A_i < 0 \;\wedge\; \text{seq\_kl}_i > \delta\bigr], \qquad \text{opsm\_mask} = 1 - \text{drop} \]

注意默认 \(\delta = 10^{-4}\) 非常小，意味着对负优势序列「几乎一漂移就掩」，比较激进。

#### 4.3.3 源码精读

`compute_opsm_mask` 的实现短而直白：

```python
def compute_opsm_mask(args, full_log_probs, full_old_log_probs, advantages, loss_masks):
    ...
    for full_log_prob, full_old_log_prob, advantage, loss_mask in zip(...):
        # 序列级 KL：对响应 token 做 mask 后求均值
        seq_kl = ((full_old_log_prob - full_log_prob) * loss_mask).sum() / torch.clamp_min(loss_mask.sum(), 1)
        # 判据：优势为负 且 seq_kl 超阈值 → 标记为该丢(mask=1)
        mask = ((advantage < 0) & (seq_kl > args.opsm_delta)).float()
        opsm_clipfrac += mask.sum() / torch.clamp_min(loss_mask.sum(), 1)
        opsm_mask_list.append(1 - mask)         # 1 - mask = 保留
    opsm_mask = torch.cat(opsm_mask_list, dim=0)
    return opsm_mask, opsm_clipfrac
```

——见 [slime/utils/ppo_utils.py:54-92](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/utils/ppo_utils.py#L54-L92)。逐行：①`seq_kl` 是序列内响应 token 的 KL 均值（用 `loss_mask` 屏蔽 padding 与工具 token）；②`mask` 在「负优势 ∧ KL 超阈值」处置 1；③返回的 `opsm_mask = 1 - mask` 在该处置 0，从而在乘到 pg_loss 时把整条序列清零。

调用点（注意它需要 full log_probs，所以会触发一次 all_gather）：

```python
need_full_log_probs = args.use_opsm or args.advantage_estimator == "gspo"
if need_full_log_probs:
    full_log_probs     = [all_gather_with_cp(...) for ...]   # CP 下拼整条
    full_old_log_probs = [all_gather_with_cp(...) for ...]
if args.use_opsm:
    opsm_mask, opsm_clipfrac = compute_opsm_mask(
        args=args, full_log_probs=full_log_probs,
        full_old_log_probs=full_old_log_probs,
        advantages=batch["advantages"], loss_masks=batch["loss_masks"])
...
if args.use_opsm:
    pg_loss = pg_loss * opsm_mask
```

——见 [slime/backends/megatron_utils/loss.py:934-961](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/loss.py#L934-L961)（计算 mask）与 [slime/backends/megatron_utils/loss.py:983-984](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/loss.py#L983-L984)（`pg_loss = pg_loss * opsm_mask`）。OPSM 在 CISPO/PPO 损失算出之后、TIS 之前应用。

参数定义：`--use-opsm` / `--opsm-delta`（默认 `1e-4`）见 [slime/utils/arguments.py:1092-1103](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/utils/arguments.py#L1092-L1103)。

#### 4.3.4 代码实践

**目标**：手算一条序列是否会被 OPSM 掩掉，理解阈值的意义。

**步骤**：

1. 假设一条响应有 3 个有效 token，已知：
   - `log_probs`（当前策略）= `[-2.0, -2.0, -2.0]`
   - `old_log_probs`（旧策略）= `[-2.0, -2.0, -1.9999]`
   - `advantage` = `-0.5`（负优势）
   - `loss_mask` = `[1, 1, 1]`，`opsm_delta = 1e-4`

2. 手算序列级 KL：

\[ \text{seq\_kl} = \frac{((-2)-(-2)) + ((-2)-(-2)) + ((-1.9999)-(-2.0))}{3} = \frac{0+0+0.0001}{3} \approx 3.3\times10^{-5} \]

3. 判据：`advantage < 0` 为真；`seq_kl > 1e-4`？\(3.3\times10^{-5} > 10^{-4}\) 为**假**。所以 `drop=0`，序列**保留**。
4. 若把 `old_log_probs` 第三个改成 `-1.99`（差 0.01），则 seq_kl ≈ 0.0033 > 1e-4，判据成立，序列被**掩掉**。

**预期结果**：你体会到 `opsm_delta=1e-4` 有多激进——只要单 token 级 logp 平均偏差超过 \(10^{-4}\)，负优势序列就会被丢。可以试着把 `--opsm-delta` 调大（如 0.01）观察 `opsm_clipfrac` 指标下降。待本地在真实训练里验证 clipfrac 数值。

#### 4.3.5 小练习与答案

**练习 1**：为什么 OPSM 只掩「负优势」序列，不掩正优势序列？

**答案**：正优势序列是模型**应当靠近**的样本，即使 KL 偏大，把它拉过来也是有利的、方向稳定的更新；而负优势序列是模型**应当远离**的样本，KL 已经很大说明策略已经远离它，再继续推远收益递减且方向不稳，故掩掉。

**练习 2**：OPSM 与 TIS 的关系是替代还是叠加？

**答案**：叠加。代码里 `pg_loss = pg_loss * opsm_mask` 在前，TIS 的 `pg_loss * tis_weights` 在后（[loss.py:983-984](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/loss.py#L983-L984) vs [loss.py:1015](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/loss.py#L1015)）。OPSM 做序列级硬掩码（清零），TIS 做 token 级软调权（乘截断后的 ρ），可以同时开。

---

### 4.4 CISPO 与 GSPO：稳定性增强变体

#### 4.4.1 概念说明

TIS/OPSM 是**额外的修正层**（叠在 PPO 之上），而 CISPO 和 GSPO 是**改写 PPO 损失本身**的算法变体，目的都是让训练更稳定。它们通过 `--advantage-estimator` 选择（见 [u6-l4](u6-l4-advantage-estimators.md)），本讲聚焦它们**损失计算**层面的差别。

- **CISPO**（来自 MiniMax-M1）：和 PPO 一样有 clip，但 ratio 被 clip 后做 **stop-gradient**，梯度改从 `log_probs` 流。结果是：**被 clip 的 token 依然贡献梯度**（PPO 里被 clip 的 token 梯度为零）。
- **GSPO**：把 PPO 的 **token 级 KL 换成 sequence 级 KL**，让 clip 在整条序列的尺度上发生，降低 token 级裁剪的高方差。

#### 4.4.2 核心流程

二者都在 `policy_loss_function` 里按 `advantage_estimator` 分支：

1. 先算 `ppo_kl`：GSPO 走 `compute_gspo_kl`（序列级，逐 token 同值）；其余走 `ppo_kl = old_log_probs - log_probs`（token 级）。
2. 算 pg_loss：`cispo` 走 `compute_cispo_loss`；其余走 `compute_policy_loss`。
3. CISPO 的 canonical 用法是单边 clip：`--eps-clip 1.0` 关掉下界，靠 `--eps-clip-high`（如 4.0）控制上界——否则会触发警告。

数学：

PPO（默认）：
\[ L_{\text{PPO}} = -\min\!\left(rA,\; \text{clip}(r,1-\epsilon,1+\epsilon_h)A\right),\quad r=\frac{\pi_\theta}{\pi_{\text{old}}} \]

CISPO：
\[ L_{\text{CISPO}} = -\text{sg}\!\left(\text{clip}(r,1-\epsilon,1+\epsilon_h)\right) \cdot A \cdot \log\pi_\theta \]
（\(\text{sg}\) 表示 stop-gradient；canonical 取 \(\epsilon\ge1\) 即禁用下界）

GSPO：把上面 \(r\) 换成由**序列级 KL** 决定的常数（序列内每个 token 同值）。

#### 4.4.3 源码精读

**CISPO 损失**——注意 `.detach()` 与乘 `log_probs`：

```python
@torch.compile(dynamic=True)
def compute_cispo_loss(ppo_kl, log_probs, advantages, eps_clip, eps_clip_high):
    """CISPO: -sg(clip(ratio)) * advantages * log_probs. 梯度走 log_probs，clipped token 仍有梯度。"""
    ratio = (-ppo_kl).exp()
    ratio_truncated = torch.clamp(ratio, min=1.0 - eps_clip, max=1.0 + eps_clip_high)
    pg_losses = -ratio_truncated.detach() * advantages * log_probs   # detach→梯度只走 log_probs
    clipfrac = (ratio_truncated != ratio).float()
    return pg_losses, clipfrac
```

——见 [slime/utils/ppo_utils.py:151-171](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/utils/ppo_utils.py#L151-L171)。对比下方 [compute_policy_loss:124-148](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/utils/ppo_utils.py#L124-L148) 的 PPO：PPO 是 `-ratio.clamp(...)*A`，ratio 带梯度，被 clamp 截断的 token 处梯度为零；CISPO 把 `ratio_truncated.detach()`，改由 `log_probs` 提供梯度，故 clipped token 仍能更新——这是它更稳定的根源。

分发与 canonical 单边 clip 的校验：

```python
if args.advantage_estimator == "cispo":
    pg_loss, pg_clipfrac = compute_cispo_loss(ppo_kl, log_probs, advantages, args.eps_clip, args.eps_clip_high)
else:
    pg_loss, pg_clipfrac = compute_policy_loss(ppo_kl, advantages, args.eps_clip, args.eps_clip_high)
```

——见 [slime/backends/megatron_utils/loss.py:978-981](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/loss.py#L978-L981)。注意 CISPO 多传了一个 `log_probs`（当前策略、带梯度）。canonical 单边设置见 [slime/utils/arguments.py:1820-1826](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/utils/arguments.py#L1820-L1826)：`eps_clip<1.0` 会警告，建议 `--eps-clip 1.0` 配 `--eps-clip-high 4.0`。

**GSPO 的序列级 KL**：

```python
def compute_gspo_kl(full_log_probs, full_old_log_probs, local_log_probs, loss_masks):
    ppo_kl = [
        ((old_logprob - log_prob) * loss_mask).sum() / torch.clamp_min(loss_mask.sum(), 1)
        for ...   # 逐序列：序列内 KL 均值（标量）
    ]
    ppo_kl = [kl.expand_as(log_prob) for ...]   # 广播回每个 token 同值
    ppo_kl = torch.cat(ppo_kl, dim=0)
    return ppo_kl
```

——见 [slime/utils/ppo_utils.py:95-121](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/utils/ppo_utils.py#L95-L121)：每条序列算一个 KL 标量，再 `expand_as` 广播到所有 token，于是下游 `ratio=exp(-ppo_kl)` 在整条序列上是同一个值，clip 也发生在序列尺度。

#### 4.4.4 代码实践

**目标**：通过对比梯度流，体会 CISPO 与 PPO 的关键差别。

**步骤**：

1. 在纸上构造一个被 clip 的 token：设某 token 的 `ratio=1.5`，`eps_clip=0.2`（上界 1.2），`advantage=+1`。
2. PPO 路径：`pg_loss = -clip(1.5,0.8,1.2)*1 = -1.2`。对 `log_probs` 求导：因为 `1.5` 被 clamp 成 `1.2`，clamp 段梯度为 0，**该 token 对 \(\theta\) 无梯度**。
3. CISPO 路径：`pg_loss = -clip(1.5,0.8,1.2).detach()*1*log_probs = -1.2 * log_probs`。对 `log_probs` 求导 = `-1.2`，**该 token 仍有梯度**，方向是「增大 \(\log\pi_\theta\)」（继续推高这个正优势 token）。
4. （阅读型）打开 [compute_policy_loss](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/utils/ppo_utils.py#L124-L148) 与 [compute_cispo_loss](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/utils/ppo_utils.py#L151-L171)，逐行对比哪一项带 `.detach()`、哪一项乘了 `log_probs`。

**预期结果**：你能说清「CISPO 让 clipped token 仍贡献梯度」这一性质；这正是它在长序列、高 clip 比例场景下比 PPO 更稳的原因。

#### 4.4.5 小练习与答案

**练习 1**：CISPO 为何推荐 `--eps-clip 1.0`（即下界 clip 阈值 `1-eps=0`）？

**答案**：CISPO 的稳定性来自「让 clipped token 仍有梯度」。若保留下界（eps<1），ratio 小于下界的 token 会被压在下界、且因 `.detach()` 不再有放大效应，等于部分恢复了 PPO 的「梯度截断」行为，削弱了 CISPO 的优势。单边（仅上界）是 canonical 用法。

**练习 2**：GSPO 和「OPSM 的序列级 KL」都用了 `\sum(...*mask)/\sum(mask)` 这个式子，目的相同吗？

**答案**：形式相同，目的不同。GSPO 用它算**损失里的 ratio 分母**（让 clip 在序列尺度发生，降方差）；OPSM 用它当**掩码判据**（KL 超 δ 且负优势就丢样本）。一个是改损失形状，一个是丢样本。

---

## 5. 综合实践

把本讲四条主线串起来，完成一次「解耦三策略 PPO + 自定义 TIS」的端到端理解。

**任务**：阅读 [examples/train_infer_mismatch_helper/](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/examples/train_infer_mismatch_helper/) 全部四个文件，回答以下问题并把结论写成一张「数据流图」：

1. **三种 logp 各在哪产生**：在 `run-qwen3-4b-mis.sh` 里找出 `--use-tis`；在 [policy_loss_function](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/loss.py#L881-L910) 里定位 `train_log_probs_for_tis = batch.get("log_probs")`（训练引擎重算）与 `batch["rollout_log_probs"]`（SGLang 记录）。
2. **ratio 怎么算**：在 [mis.py:216](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/examples/train_infer_mismatch_helper/mis.py#L216) 找到 `raw_log_ratio_diff = train_log_prob - rollout_log_prob`，写出 \(\rho=\exp(\log\pi_{\text{train}}-\log\pi_{\text{rollout}})\)。
3. **三策略如何解耦**：对照 [README.md 的「Decoupled PPO」公式](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/examples/train_infer_mismatch_helper/README.md#69-decoupled-3-policy-ppo-importance-sampling)，标注 \(\pi_\theta\)（当前，带梯度）、\(\pi_{\text{old}}\)（训练引擎重算，PPO clip 锚）、\(\pi_{\text{rollout}}\)（SGLang，行为策略）三者分别出现在损失的哪一项。
4. **CPU 验证**：用 4.2.4 的 toy 数据跑 `compute_mis_weights`，确认 IS 权重围绕 1，并把 `tis_mode` 从 `truncate` 改成 `clip`/`mask`，对比返回的 `modified_mask` 差异（mask 模式会把越界 token 的 mask 置 0，进而影响 [loss.py:1023-1029](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/loss.py#L1023-L1029) 的归约分母）。

**产出**：一张标注了「Sample.tokens → SGLang 算 rollout_log_probs → 训练引擎重算 old log_probs → compute_mis_weights 算 ρ → pg_loss×ρ → 归约」的流程图，并在 ρ 那一步写出完整 ratio 表达式。

**若无法本地运行**：步骤 4 标注「待本地验证」；前 3 步纯源码阅读，无需 GPU。

## 6. 本讲小结

- **custom_loss**：`--loss-type custom_loss` + `--custom-loss-function-path` 整体替换损失，签名 `(args, batch, logits, sum_of_sample_mean) -> (loss, metrics)`，下游缩放由框架负责；适合结构级改写，不适合「在 PPO 上加一项」。
- **off-policy 偏差**来自 \(\pi_{\text{rollout}}\)（SGLang 采样时）与 \(\pi_{\text{old}}\)（Megatron 重算）不一致或样本过期；TIS 用两次 logp 的比 \(\rho=\exp(\log\pi_{\text{old}}-\log\pi_{\text{rollout}})\) 修正。
- **vanilla_tis_function** 把 \(\rho\) 截断到 `[tis_clip_low, tis_clip]` 后乘到 pg_loss；`--custom-tis-function-path` 可换成 MIS（token/sequence/geometric 三种粒度 + truncate/clip/mask 三种处理）。
- **OPSM** 在「负优势 ∧ 序列 KL > opsm_delta」时把整条序列清零（`pg_loss *= opsm_mask`），与 TIS 正交可叠加；默认 δ=1e-4 很激进。
- **CISPO** 把 clipped ratio 做 stop-gradient、让梯度走 `log_probs`，使 clipped token 仍有梯度，canonical 用单边 clip（`--eps-clip 1.0`）；**GSPO** 把 token 级 KL 换成序列级以降方差。
- 所有自定义都用 `load_function` 把 import 路径解析成函数对象（见 [u6-l1](u6-l1-customization-overview.md)），与框架核心解耦。

## 7. 下一步学习建议

- 想看这些 off-policy 机制在**真实集群**上怎么开：继续 [u8-l3 参数体系全景](u8-l3-argument-system.md)，追踪 `--use-tis`/`--opsm-delta` 等如何从命令行流入 `policy_loss_function`。
- 想理解异步训练下「样本过期」的源头：读 [u7-l4 流式、全异步与部分回滚 rollout](u7-l4-streaming-async-partial.md)，把「过期」和本讲的「off-policy 修正」对应起来。
- 想验证你写的自定义 tis 函数签名正确：虽然 slime 目前没有 custom_loss/TIS 的专属契约测试（见 `tests/plugin_contracts/`），但可参考 [u8-l6 测试、契约测试与 CI](u8-l6-tests-contracts-ci.md) 的思路，为 `compute_mis_weights` 这类纯函数写 CPU 单测。
- 若对 KL 估计的 k1/k2/k3 细节感兴趣，回顾 [u6-l4](u6-l4-advantage-estimators.md) 里 `compute_approx_kl` 的部分——本讲的 CISPO/GSPO/TIS 都建立在它之上。
