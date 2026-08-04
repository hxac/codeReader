# Diffusion Pipeline 与去噪数据流

## 1. 本讲目标

本讲聚焦 vLLM-Omni 扩散子系统中最「贴近模型」的一层：**Diffusion Pipeline**。在上一讲（u5-l3）中，我们看清了 worker 子进程如何把请求送进 `pipeline.forward`；本讲要回答：**`pipeline.forward` 内部到底做了什么、噪声是如何一步步被去掉的、最终又是如何变成一张图片的**。

学完本讲你应当能够：

- 说清一次去噪的完整数据流：`encode_prompt → prepare_latents → diffuse → vae.decode → post_process`。
- 读懂任一模型的 `diffuse` 多步循环，理解 `transformer.forward`（预测噪声）与 `scheduler.step`（沿时间步推进）的分工。
- 理解 Classifier-Free Guidance（CFG）的正/负 prompt 双前向机制、CFG 合并公式，以及 `cache_branch` 缓存分支的作用。
- 掌握请求载荷 `OmniDiffusionRequest`（`prompt / sampling_params / kv_sender_info`）与进程级 `OmniDiffusionConfig` 上下文（`set_current_diffusion_config`）两个关键数据结构。

## 2. 前置知识

在进入源码前，先用一张图和几个术语把「扩散模型推理」的直觉建立起来。

扩散模型生成图像的过程可以类比「从一团纯噪声里一点点擦掉噪声，最后显出画面」。它有两个核心动作：

- **加噪 / 去噪方向**：模型不是直接预测图像，而是预测「当前这团噪声里还残留多少噪声」（即噪声预测 / noise prediction）。在流匹配（Flow Matching）框架下，这等价于预测一个「速度场」，把噪声拉向清晰样本。
- **时间步（timestep）**：从 `t=T`（几乎纯噪声）逐步走到 `t=0`（清晰图像），每走一步调用一次「去噪函数」。`num_inference_steps` 就是总共走几步，步数越多、质量越好、速度越慢。

每一步内部干两件事：

1. **预测噪声**：把当前 latent（隐变量）喂给 Transformer（DiT），得到「这一步预测的噪声/速度」。
2. **调度器步进（scheduler.step）**：用一个 ODE 求解器（如 Euler）根据预测的噪声，把 latent 沿时间轴往回推一小步，得到「下一步的 latent」。

几个本讲会反复出现的术语：

- **latent（隐变量）**：图像在 VAE 压缩空间里的表示，远小于像素空间，DiT 全程在 latent 空间运算。
- **prompt embeds（文本嵌入）**：文本经过文本编码器（text encoder）得到的向量，作为 DiT 的条件输入。
- **CFG（Classifier-Free Guidance，无分类器引导）**：同时跑「正向 prompt」和「负向 prompt」两次前向，再把结果按比例合并，从而让生成结果更贴合正向、远离负向。
- **VAE**：负责 latent 与像素空间互转的编解码器，`vae.decode` 把去噪完成的 latent 还原成图像。
- **DiT（Diffusion Transformer）**：用 Transformer 结构做噪声预测的主干网络。

> 与自回归（AR）模型的本质区别：AR 一次产一个 token，扩散是一次「修整整张 latent」，多步迭代收敛到一张图。所以扩散的核心不是「采样 token」，而是「多步去噪循环」——这正是 `diffuse` 函数干的事。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [vllm_omni/diffusion/request.py](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/request.py) | 扩散请求载荷 `OmniDiffusionRequest`，携带 prompt、采样参数、`kv_sender_info`。 |
| [vllm_omni/inputs/data.py](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/inputs/data.py) | 巨型采样参数口袋 `OmniDiffusionSamplingParams`（尺寸/步数/seed/CFG/KV 等）。 |
| [vllm_omni/diffusion/config.py](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/config.py) | 进程级全局扩散配置上下文 `set/get_current_diffusion_config`。 |
| [vllm_omni/diffusion/distributed/cfg_parallel.py](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/distributed/cfg_parallel.py) | 通用 CFG 基类 `CFGParallelMixin`：`predict_noise_maybe_with_cfg`、`combine_cfg_noise`、`scheduler_step`。 |
| [vllm_omni/diffusion/models/qwen_image/cfg_parallel.py](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/models/qwen_image/cfg_parallel.py) | Qwen-Image 的 `diffuse` 多步循环实现，串联噪声预测与调度器步进。 |
| [vllm_omni/diffusion/models/qwen_image/pipeline_qwen_image.py](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/models/qwen_image/pipeline_qwen_image.py) | Qwen-Image pipeline：`forward`、`encode_prompt`、`prepare_latents`、`_decode_latents`、步式执行四件套。 |
| [vllm_omni/diffusion/data.py](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/data.py) | 统一输出容器 `DiffusionOutput`、`OmniDiffusionConfig`。 |
| [vllm_omni/diffusion/cache/teacache/hook.py](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/cache/teacache/hook.py) | TeaCache 钩子，演示 `cache_branch`（正/负分支）如何被识别。 |
| [docs/design/module/dit_module.md](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/docs/design/module/dit_module.md) | 扩散模块总体设计，含 Diffusion Pipeline 与 Data Flow 两节。 |

---

## 4. 核心概念与源码讲解

### 4.1 请求载荷：OmniDiffusionRequest 与采样参数

#### 4.1.1 概念说明

在数据真正进入 `pipeline.forward` 之前，它必须被打包成一个**请求载荷**。扩散请求的核心是 `OmniDiffusionRequest`，它只回答三个问题：

1. **画什么** —— `prompt`（一条 `OmniPromptType`，可以是纯字符串，也可以是带 `negative_prompt`、`prompt_embeds` 等字段的字典）。
2. **怎么画** —— `sampling_params`（一个 `OmniDiffusionSamplingParams`，装着尺寸、步数、seed、CFG 强度等全部控制旋钮）。
3. **这是谁的请求 / 跨阶段要不要带 KV** —— `request_id` 和可选的 `kv_sender_info`。

`kv_sender_info` 是多阶段（Omni）场景下用于跨 stage 传递 KV 缓存元数据的字典（见 u3-4 OmniConnector）；纯单阶段文生图时它通常是 `None`。

注意一个关键设计：`OmniDiffusionSamplingParams` 是一个**代表单条 prompt 的大口袋**，它的 `batch_size` 属性直接返回 `num_outputs_per_prompt`，而不是「prompt 数」。真正的「多 prompt 批处理」由上层 scheduler 把多条请求合并进一次 `pipeline.forward(batch)` 完成（见 u5-1/u5-2）。

#### 4.1.2 核心流程

请求在 `OmniDiffusionRequest.__post_init__` 里会做三项自动归一化：

1. **seed 兜底**：调用方既没给 `generator` 也没给 `seed` 时，随机生成一个 seed，保证分布式下各 rank 能派生出相同随机状态。
2. **guidance_scale 缺省与「是否显式提供」标记**：未提供则填默认 `1.0`，并把 `guidance_scale_provided` 置为相应布尔（`0.0` 是合法值，不能误判为缺省）。
3. **CFG 开关判定**：当 `guidance_scale > 1.0` 且 prompt 里带了 `negative_prompt` 时，才打开 `do_classifier_free_guidance`。

```
OmniDiffusionRequest.__post_init__:
  ├─ 校验 request_id 非空
  ├─ 若 generator/seed 都缺 → 随机 seed
  ├─ 解析 guidance_scale（区分「缺省」与「显式 0.0」）
  └─ guidance_scale>1 且有 negative_prompt → do_classifier_free_guidance=True
```

#### 4.1.3 源码精读

请求 dataclass 的四个核心字段（[vllm_omni/diffusion/request.py:L29-L32](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/request.py#L29-L32)）：`prompt`、`sampling_params`、`request_id`、`kv_sender_info`，分别是「画什么 / 怎么画 / 谁的请求 / 跨阶段 KV」。

归一化逻辑在 [vllm_omni/diffusion/request.py:L34-L69](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/request.py#L34-L69) 中，其中 CFG 开关判定（[L54-L57](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/request.py#L54-L57)）值得记住——**只有同时满足「guidance_scale>1」和「有负向 prompt」两个条件才会触发 CFG 双前向**，否则只跑正向单前向。

采样参数口袋 `OmniDiffusionSamplingParams` 定义于 [vllm_omni/inputs/data.py:L196-L345](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/inputs/data.py#L196-L345)，关键字段速查：

| 字段（行号） | 含义 |
| --- | --- |
| `num_outputs_per_prompt`（L212）/ `seed`（L213） | 每个 prompt 出几张图 / 随机种子 |
| `do_classifier_free_guidance`（L208） | 是否启用 CFG |
| `cfg_normalize`（L221） | CFG 合并后是否做范数归一 |
| `height`/`width`（L246-L247） | 输出图像尺寸（像素空间，未缩放） |
| `num_inference_steps`（L265）/ `sigmas`（L275） | 去噪步数 / 自定义噪声调度 |
| `guidance_scale`（L266） | 引导强度（部分模型用） |
| `true_cfg_scale`（L277） | True CFG 强度（Qwen-Image 系列用） |
| `past_key_values`/`need_kv_receive`（L283-L285） | 跨阶段 KV 注入（Omni 模型用） |

其 `batch_size` 属性（[L340-L344](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/inputs/data.py#L340-L344)）只返回 `num_outputs_per_prompt`，印证了「一个 params 代表一条 prompt」的设计。

#### 4.1.4 代码实践

**实践目标**：理解 CFG 开关的两条触发条件，避免「设了 guidance_scale 却不生效」的困惑。

**操作步骤**（源码阅读型）：

1. 打开 [request.py:L48-L57](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/request.py#L48-L57)，阅读 `guidance_scale` 解析与 CFG 判定。
2. 构造两个假想请求（**示例代码，非项目原有**）：
   - A：`prompt="a cat"`，`guidance_scale=4.0`，**无** negative_prompt。
   - B：`prompt={"prompt":"a cat","negative_prompt":"blurry"}`，`guidance_scale=4.0`。

**需要观察的现象**：在 `__post_init` 执行后，A 的 `do_classifier_free_guidance` 应为 `False`，B 的应为 `True`。

**预期结果**：即便设置了 `guidance_scale>1`，没有 negative_prompt 也不会触发 CFG——这与许多初学者直觉相悖。若想强制对比效果，需显式提供 `negative_prompt`（或负向 embeds）。结果「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `guidance_scale=0.0` 不能被当成「未提供」处理？

**参考答案**：因为 `0.0` 是 API 合法值。`__post_init__` 用 `is not None` 而非真值判断来区分「缺省」与「显式 0.0」（见 [request.py:L48-L51](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/request.py#L48-L51)），否则会把 `0.0` 误填成默认 `1.0`，意外开启 CFG。

**练习 2**：`OmniDiffusionSamplingParams.batch_size` 为什么不等于「prompt 条数」？

**参考答案**：该类设计上只代表**单条 prompt**（见 [inputs/data.py:L340-L344](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/inputs/data.py#L340-L344)），其 `batch_size` 只反映 `num_outputs_per_prompt`。多 prompt 批处理由上层 scheduler 把多条 `OmniDiffusionRequest` 合并进一个 `DiffusionRequestBatch` 再喂给 `pipeline.forward`。

---

### 4.2 全局扩散配置上下文：set / get_current_diffusion_config

#### 4.2.1 概念说明

DiT 的 `Attention` 层在 `__init__` 时需要读取当前请求对应的扩散配置（`OmniDiffusionConfig`），以决定用哪个注意力后端、是否启用序列并行等（详见 u7-1）。但 `Attention.__init__` 并没有接收 `OmniDiffusionConfig` 作为参数——否则每一层都得层层透传，侵入性太大。

vLLM-Omni 借鉴 vLLM 的 `set_current_vllm_config` 模式，给出一个优雅解法：**用一个进程级全局变量保存「当前生效的扩散配置」**，并提供一个上下文管理器在模型构造期间临时设置它。模型构造一结束，全局变量自动还原。

#### 4.2.2 核心流程

```
模型构造期间：
  with set_current_diffusion_config(od_config):
      └─ 构造 DiT（各 Attention 层在 __init__ 里调用 get_current_diffusion_config() 读取配置）
  ── 退出 with ── 还原为旧值（通常是 None）

后续运行期：
  Attention 层改从 ForwardContext 读取运行时信息；
  但 get_current_diffusion_config_or_none() 仍可作为「无副作用」的安全探测。
```

要点：

- **只读单例**：模块顶层维护一个 `_current_diffusion_config`，`set` 用 `global` 改写并保存 `old` 值，`finally` 里还原——这是典型的「保存-恢复」上下文管理器。
- **强/弱两种读取**：`get_current_diffusion_config()` 在未设置时直接 `assert` 报错（强契约，用于「必须读到」的构造期）；`get_current_diffusion_config_or_none()` 返回 `None` 不报错（弱探测，用于「可能没设」的运行期）。

#### 4.2.3 源码精读

全局变量与上下文管理器见 [vllm_omni/diffusion/config.py:L18-L43](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/config.py#L18-L43)：

- `set_current_diffusion_config`（[L21-L30](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/config.py#L21-L30)）：保存 `old`、写入新值、`yield`、`finally` 还原。
- `get_current_diffusion_config`（[L33-L38](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/config.py#L33-L38)）：断言非空后返回，未设置即抛错——文件头注释（[L3-L8](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/config.py#L3-L8)）明确说明它「镜像 vLLM 的 `set_current_vllm_config` 模式」，目的是让每个 `Attention` 层无需耦合 `ForwardContext` 即可读到配置。

> 消费侧的例子：[dit_module.md:L492-L502](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/config.py) 记录了 `Attention.__init__` 通过 `get_current_diffusion_config_or_none()` 拿到 `diffusion_attention_config`，再按 role 解析后端——这正是本上下文存在的意义。

#### 4.2.4 代码实践

**实践目标**：体会「保存-恢复」语义与强/弱读取的差异。

**操作步骤**（源码阅读型 + 推理）：

1. 阅读 [config.py:L21-L43](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/config.py#L21-L43)。
2. 推理以下**示例代码**（非项目原有）在嵌套 `with` 时的行为：

```python
# 示例代码
with set_current_diffusion_config(cfgA):
    assert get_current_diffusion_config() is cfgA
    with set_current_diffusion_config(cfgB):
        assert get_current_diffusion_config() is cfgB
    assert get_current_diffusion_config() is cfgA   # 内层退出后还原为 cfgA
assert get_current_diffusion_config()   # 报错：已还原为初始 None
```

**需要观察的现象**：内层退出后能正确回到外层值；最外层退出后调用强读取会触发 `AssertionError`。

**预期结果**：嵌套安全，且未设置时强读取必然报错——这正是「构造期用强契约、运行期用弱探测」的分工依据。结果「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：为什么不直接把 `OmniDiffusionConfig` 作为参数传给每个 `Attention` 层？

**参考答案**：DiT 有几十上百个 `Attention` 层，逐层透参侵入性大、易遗漏，且 diffusers 原生模块签名固定。全局上下文让任意深层模块都能读到配置，又用「保存-恢复」保证不污染进程状态，是侵入性最小的方案。

**练习 2**：`get_current_diffusion_config` 与 `get_current_diffusion_config_or_none` 分别应在什么场景调用？

**参考答案**：前者用 `assert`，适合「必须读到」的模型构造期；后者返回 `None` 不报错，适合运行期探测或「可能未设置」的兼容路径。

---

### 4.3 diffuse 多步去噪循环：transformer.forward → scheduler.step

#### 4.3.1 概念说明

`diffuse` 是整个扩散推理中**最耗时**的部分——它是一个 `for t in timesteps` 的多步循环。每一步做两件事，对应两个核心函数：

- **预测噪声**：`predict_noise(**kwargs)` → 调用 `self.transformer(...)`，输入当前 latent + timestep + prompt embeds，输出「这一步预测的噪声/速度」。
- **调度器步进**：`scheduler.step(noise_pred, t, latents)` → 根据预测噪声把 latent 沿时间轴回推一步，得到「下一步 latent」。

设计上，vLLM-Omni 把这些通用逻辑抽到基类 `CFGParallelMixin`，而每个具体模型只实现自己的 `diffuse` 循环骨架（负责组装每一步的 transformer 输入）。这样「换模型 = 换 diffuse 骨架 + 换 transformer」，而 CFG 合并、调度器步进等公共逻辑可在基类共享。

#### 4.3.2 核心流程

以 Qwen-Image 为例的单步循环：

```
for t in timesteps:
    1. 广播 timestep 到 batch，构造 latent_model_input
    2. 组装 positive_kwargs（hidden_states / timestep / encoder_hidden_states / ...）
    3. 若 do_true_cfg：再组装 negative_kwargs（换用负向 embeds）
    4. noise_pred = predict_noise_maybe_with_cfg(...)   # 内部决定是否双前向、是否并行
    5. latents = scheduler_step_maybe_with_cfg(noise_pred, t, latents, ...)
return latents   # 去噪完成的 latent
```

数学上，单步 Euler 流匹配步进（`FlowMatchEulerDiscreteScheduler`）可写成：

\[
x_{t-1} = x_t + (t_{\text{prev}} - t)\, v_\theta(x_t, t, c)
\]

其中 \(v_\theta\) 是 DiT 预测的速度，\(c\) 是文本条件（prompt embeds）。`scheduler.step` 内部就是这类 ODE 一步求解；`diffuse` 循环反复调用它直到 \(t=0\)。

#### 4.3.3 源码精读

Qwen-Image 的 `diffuse` 实现在 [vllm_omni/diffusion/models/qwen_image/cfg_parallel.py:L28-L129](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/models/qwen_image/cfg_parallel.py#L28-L129)。关键两行——

- 预测噪声：[L115-L122](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/models/qwen_image/cfg_parallel.py#L115-L122) 调用 `predict_noise_maybe_with_cfg(...)`，拿到 `noise_pred`。
- 调度器步进：[L125](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/models/qwen_image/cfg_parallel.py#L125) 调用 `scheduler_step_maybe_with_cfg(noise_pred, t, latents, do_true_cfg)`，更新 `latents`。

正向/负向 transformer 输入的组装见 [L87-L109](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/models/qwen_image/cfg_parallel.py#L87-L109)——两者唯一区别是 `encoder_hidden_states`（正向 vs 负向 prompt embeds）。

通用基类 `CFGParallelMixin` 提供：

- `predict_noise`（[distributed/cfg_parallel.py:L383-L399](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/distributed/cfg_parallel.py#L383-L399)）：默认直接调 `self.transformer(...)`，多输出模型（如视频+音频）可重写返回 tuple。
- `scheduler_step`（[L491-L532](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/distributed/cfg_parallel.py#L491-L532)）：把输入交给 `self.scheduler.step(noise_pred, t, latents)[0]`，支持用 `per_request_scheduler` 覆盖（步式执行时每请求独享调度器）。

> 注意循环里还有一行 `self.transformer.do_true_cfg = do_true_cfg`（[qwen cfg_parallel.py:L70](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/models/qwen_image/cfg_parallel.py#L70)）——这是告诉 transformer「当前这一步是不是 CFG 模式」，缓存钩子正是靠它判断正/负分支（见 4.4）。

#### 4.3.4 代码实践

**实践目标**：在源码层面定位「预测噪声」与「调度器步进」两条语句，看清它们的输入输出。

**操作步骤**（源码阅读型）：

1. 打开 [qwen_image/cfg_parallel.py](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/models/qwen_image/cfg_parallel.py) 的 `diffuse`（L28-L129）。
2. 跟进 `scheduler_step_maybe_with_cfg` → 基类 `scheduler_step`（[distributed/cfg_parallel.py:L534-L574](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/distributed/cfg_parallel.py#L534-L574) 与 [L491-L532](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/distributed/cfg_parallel.py#L491-L532)）。
3. 填写一张表（**示例输出**）：

| 函数 | 输入 | 输出 |
| --- | --- | --- |
| `predict_noise_maybe_with_cfg` | positive/negative kwargs（latent、timestep、embeds） | `noise_pred`（预测噪声/速度张量） |
| `scheduler_step` | `noise_pred, t, latents` | 更新后的 `latents`（下一个时间步的 latent） |

**需要观察的现象**：`noise_pred` 与 `latents` 形状在步进前后保持一致（流匹配是残差式更新）；`t` 是标量时间步。

**预期结果**：每步只改变 latent 的数值、不改变形状，循环结束时 latent 已去噪完成，交给后续 `vae.decode`。结果「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：为什么把 `predict_noise` 和 `scheduler_step` 放在通用基类，而 `diffuse` 留给每个模型实现？

**参考答案**：前两者是「调 transformer / 调 scheduler」的通用动作，与具体模型无关，可共享；而 `diffuse` 负责组装**每一步的 transformer 输入**（不同模型的 kwargs 名、CFG 表达、是否有 image_latents 拼接等差异很大），必须 per-model 定制。基类把 `diffuse` 声明为 `raise NotImplementedError`（[distributed/cfg_parallel.py:L401-L464](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/distributed/cfg_parallel.py#L401-L464)）强制子类实现。

**练习 2**：`scheduler_step` 里的 `per_request_scheduler` 参数解决什么问题？

**参考答案**：请求级执行（REQUEST_BATCH）所有步骤共用 `self.scheduler`；但步式执行（STEP_BATCH）会把多条请求交错推进，每条请求必须维护各自的调度器状态，故用 `per_request_scheduler` 注入请求独享的调度器（见 [L510-L515](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/distributed/cfg_parallel.py#L510-L515) 注释）。

---

### 4.4 CFG 双前向与缓存分支

#### 4.4.1 概念说明

**Classifier-Free Guidance（CFG）** 是提升生成质量的关键技巧：在同一步里跑两次 transformer 前向——一次用**正向 prompt**（条件预测 \(\epsilon_{\text{pos}}\)），一次用**负向 prompt**（无条件/反条件预测 \(\epsilon_{\text{neg}}\)），再按下式合并：

\[
\hat{\epsilon} = \epsilon_{\text{neg}} + s \cdot (\epsilon_{\text{pos}} - \epsilon_{\text{neg}})
\]

其中 \(s\) 是 `true_cfg_scale`（>1）。直觉上：在「无条件方向」基础上，沿「正向 − 负向」的方向**外推**，让结果更贴合正向、远离负向。

Qwen-Image 等模型还做一步**范数归一化（cfg_normalize）**，避免 CFG 外推后幅值爆炸：

\[
\hat{\epsilon}_{\text{norm}} = \hat{\epsilon} \cdot \frac{\|\epsilon_{\text{pos}}\|}{\|\hat{\epsilon}\|}
\]

即把合并结果缩放到与正向预测相近的范数。

**缓存分支（cache_branch）**：当启用缓存加速（如 TeaCache）时，正/负两次前向的「可复用残差」必须分开存放，否则会互相污染。系统用 `cache_branch`（取值 `"positive"`/`"negative"`）区分这两份缓存状态。

#### 4.4.2 核心流程

`predict_noise_maybe_with_cfg` 内部有三条路径（由是否 CFG、是否 CFG 并行决定）：

```
do_true_cfg?
├─ 否 → 只跑 positive 一次前向，直接返回
└─ 是
   ├─ CFG 并行就绪 (cfg_world_size>1)?
   │   ├─ rank0 算 positive，其余 rank 算 negative
   │   ├─ all_gather 交换结果
   │   └─ 所有 rank 本地做 combine_cfg_noise（确定性，无需广播）
   └─ 否（顺序 CFG）
       ├─ 先算 positive，再算 negative
       └─ combine_cfg_noise
```

合并后，`scheduler_step_maybe_with_cfg` 在**所有 rank 上各自本地**步进——因为 `all_gather`+本地 combine 已保证各 rank 拿到相同的 `noise_pred`。

`cache_branch` 的判定（TeaCache 钩子内）：

- **CFG 并行**：`cfg_rank==0` → positive，`cfg_rank>0` → negative。
- **顺序 CFG**：用前向计数 `_forward_cnt` 的奇偶交替（偶=positive，奇=negative）。

#### 4.4.3 源码精读

**CFG 合并公式**落在 `combine_cfg_noise`（[distributed/cfg_parallel.py:L178-L222](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/distributed/cfg_parallel.py#L178-L222)），核心是 [L218](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/distributed/cfg_parallel.py#L218) 的 `comb = n + true_cfg_scale * (p - n)`；范数归一由 `cfg_normalize_function`（[L162-L176](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/distributed/cfg_parallel.py#L162-L176)）完成，即 `comb * (cond_norm/noise_norm)`。

**路径分发**在 `predict_noise_maybe_with_cfg`（[L76-L160](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/distributed/cfg_parallel.py#L76-L160)）：

- CFG 并行分支：[L107-L138](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/distributed/cfg_parallel.py#L107-L138)，按 `cfg_rank` 各算一支后 `cfg_group.all_gather`（[L127](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/distributed/cfg_parallel.py#L127)）。
- 顺序 CFG 分支：[L139-L154](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/distributed/cfg_parallel.py#L139-L154)，两次 `self.predict_noise(...)` 后合并。
- 无 CFG 分支：[L155-L160](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/distributed/cfg_parallel.py#L155-L160)，只跑正向。

**cache_branch 判定**在 TeaCache 钩子 [teacache/hook.py:L125-L137](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/cache/teacache/hook.py#L125-L137)：读取 `module.do_true_cfg` 决定分支，再拼出 `context_name = f"teacache_{cache_branch}"` 作为缓存状态命名空间。设计文档 [dit_module.md:L445-L458](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/docs/design/module/dit_module.md#L445-L458) 也以 `cache_branch="negative"` 为例说明了这一缓存感知执行机制。

> 关键设计点：CFG 并行时各 rank 只算一支，靠 `all_gather` 交换；合并与步进都是「确定性本地计算」，所以**无需再广播**，所有 rank 结果天然一致（基类注释 [L62-L73](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/distributed/cfg_parallel.py#L62-L73)）。

#### 4.4.4 代码实践（本讲主实践任务）

**实践目标**：选择一个 pipeline 的 `diffuse` 函数，画出「单步去噪」内部的张量流（含 CFG 双前向），并标注 `scheduler.step` 的输入输出。

**操作步骤**（源码阅读型）：

1. 从 `vllm_omni/diffusion/models/` 下任选一个带 `diffuse` 的实现（推荐 Qwen-Image：[qwen_image/cfg_parallel.py:L28-L129](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/models/qwen_image/cfg_parallel.py#L28-L129)；也可选 flux、wan2_2、audiox 等）。
2. 对照下面的「单步去噪张量流」模板，填入具体变量名与形状。

**单步去噪张量流（以顺序 CFG 为例）**：

```
输入：latents [B, P, C]（打包后的 latent）、t（标量时间步）、
     prompt_embeds / negative_prompt_embeds

  ┌─ positive 前向 ─────────────────────────────────┐
  │ transformer(hidden_states=latents, timestep=t/1000, │
  │            encoder_hidden_states=prompt_embeds)      │
  │   → ε_pos  (正向噪声预测，形状同 latents)            │
  └──────────────────────────────────────────────────┘
  ┌─ negative 前向 ─────────────────────────────────┐
  │ transformer(hidden_states=latents, timestep=t/1000, │
  │            encoder_hidden_states=negative_prompt_embeds) │
  │   → ε_neg  (负向噪声预测，形状同 latents)             │
  └──────────────────────────────────────────────────┘
  合并：ε̂ = ε_neg + true_cfg_scale * (ε_pos − ε_neg)
  （可选归一：ε̂ ← ε̂ * ‖ε_pos‖ / ‖ε̂‖）

  scheduler.step 的输入输出：
    输入 = (noise_pred=ε̂, t=当前时间步, latents=当前 latent)
    输出 = 下一时间步的 latent（形状不变）

输出：更新后的 latents，进入下一个 t
```

**需要观察的现象**：

- 正/负两次前向的 `hidden_states`、`timestep` 完全相同，**只有 `encoder_hidden_states` 不同**。
- `scheduler.step` 接收的是**合并后**的 `ε̂`，不是单支预测。

**预期结果**：你能指出 `predict_noise_maybe_with_cfg` 返回的 `noise_pred` 即 `ε̂`，而 `scheduler_step_maybe_with_cfg` 用它推进 latent。若选择 CFG 并行实现，应能在张量流里标出 `all_gather` 交换点替代「两次本地前向」。结果「待本地验证」。

#### 4.4.5 小练习与答案

**练习 1**：CFG 并行模式下，为什么合并和 `scheduler.step` 不需要再广播？

**参考答案**：`all_gather` 之后，每个 rank 都已持有完整的 \(\epsilon_{\text{pos}}\) 与 \(\epsilon_{\text{neg}}\)；合并公式（[L218](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/distributed/cfg_parallel.py#L218)）与调度器步进都是确定性本地计算，各 rank 输入相同、结果必然相同，故无需额外同步。

**练习 2**：若 `cfg_normalize=True`，合并结果会被如何改变？为什么需要它？

**参考答案**：合并后乘以 \(\|\epsilon_{\text{pos}}\|/\|\hat{\epsilon}\|\)（[L173-L175](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/distributed/cfg_parallel.py#L173-L175)）。CFG 外推会放大幅值，归一化把合并预测拉回与正向预测相近的范数，防止后续步进发散、保护生成质量。

---

### 4.5 端到端数据流：encode_prompt → prepare_latents → diffuse → vae.decode → post_process

#### 4.5.1 概念说明

把前几节拼起来，就得到一次完整的扩散生成数据流。它有**两条执行路径**（见 u5-1/u5-2）：

- **REQUEST_BATCH（请求级）**：一次性把整条 pipeline 跑完，对应 `pipeline.forward(batch)`，内部是 `encode_prompt → prepare_latents → diffuse → vae.decode`。
- **STEP_BATCH（步级）**：把上述过程拆成可在去噪步之间调度的四段：`prepare_encode` / `denoise_step` / `step_scheduler` / `post_decode`，便于流式输出与连续批处理。

两条路径最终都产出 `DiffusionOutput`，再经 `post_process_func`（注册表模式，按模型架构名挂载）转成 PIL 图片等最终格式。

#### 4.5.2 核心流程

完整请求流（对照 [dit_module.md:L930-L977](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/docs/design/module/dit_module.md#L930-L977)）：

```
OmniDiffusionRequest
  └─ forward(batch)  [REQUEST_BATCH 路径]
       ├─ _prepare_generation_context
       │    ├─ check_inputs / 校验尺寸
       │    ├─ encode_prompt          文本 → prompt_embeds（+ 负向）
       │    ├─ prepare_latents        随机噪声 latent（按 seed）
       │    └─ prepare_timesteps      生成时间步序列
       ├─ diffuse(...)                多步去噪循环（4.3/4.4）
       ├─ _decode_latents             unpack + 归一化 + vae.decode
       └─ split_diffusion_output_by_request  按请求切分输出

post_process_func(DiffusionOutput)     tensor → PIL.Image
```

worker 侧调用顺序（见 u5-3）：先 `cache_backend.refresh`，再 `set_forward_context`，最后 `pipeline.forward`——这三步在 [diffusion_model_runner.py:L496-L505](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/worker/diffusion_model_runner.py#L496-L505)。

#### 4.5.3 源码精读（Qwen-Image）

**请求级入口 `forward`**（[pipeline_qwen_image.py:L995-L1089](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/models/qwen_image/pipeline_qwen_image.py#L995-L1089)）：

- 先抽取 prompt/尺寸/步数/CFG 等参数（[L996-L1039](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/models/qwen_image/pipeline_qwen_image.py#L996-L1039)）。
- 调 `_prepare_generation_context`（[L1040-L1059](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/models/qwen_image/pipeline_qwen_image.py#L1040-L1059)）一次性完成编码/造噪/时间步。
- 跑 `diffuse`（[L1061-L1080](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/models/qwen_image/pipeline_qwen_image.py#L1061-L1080)）。
- `_decode_latents` + `split_diffusion_output_by_request`（[L1084-L1089](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/models/qwen_image/pipeline_qwen_image.py#L1084-L1089)）。

**预处理三件套**（由 `_prepare_generation_context` 串联，[L647-L769](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/models/qwen_image/pipeline_qwen_image.py#L647-L769)）：

- `encode_prompt`（[L499-L539](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/models/qwen_image/pipeline_qwen_image.py#L499-L539)）：用 Qwen2.5-VL 文本编码器把 prompt 转成 `prompt_embeds`，CFG 时再编码一次负向。
- `prepare_latents`（[L565-L596](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/models/qwen_image/pipeline_qwen_image.py#L565-L596)）：按 `generator`/`seed` 生成随机噪声 latent 并 `_pack_latents` 打包。
- `prepare_timesteps`（[L598-L614](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/models/qwen_image/pipeline_qwen_image.py#L598-L614)）：根据图像序列长度计算动态 shift，再 `retrieve_timesteps` 取出时间步序列。

**VAE 解码 `_decode_latents`**（[L882-L911](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/models/qwen_image/pipeline_qwen_image.py#L882-L911)）：`_unpack_latents` 还原形状 → 按 `latents_mean/std` 反归一化 → `self.vae.decode(latents)` 得到像素图像，包成 `DiffusionOutput`。

**后处理 `post_process_func`**（[L60-L91](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/models/qwen_image/pipeline_qwen_image.py#L60-L91)）：用 `VaeImageProcessor.postprocess` 把解码后的张量转成 PIL 图片，引擎以注册表方式按模型名挂载（见 u5-1 的 pre/post 处理注册表模式）。

**步级四件套**（STEP_BATCH 路径，与请求级共享同一套底层逻辑）：

- `prepare_encode`（[L771-L819](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/models/qwen_image/pipeline_qwen_image.py#L771-L819)）：复用 `_prepare_generation_context`，并把结果写进每请求的 `StepRequestState`（含 `deepcopy` 出来的请求独享 scheduler）。
- `denoise_step`（[L913-L958](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/models/qwen_image/pipeline_qwen_image.py#L913-L958)）：从 `InputBatch` 读当前 latent，调用 `predict_noise_maybe_with_cfg` 得到一步噪声预测。
- `step_scheduler`（[L960-L979](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/models/qwen_image/pipeline_qwen_image.py#L960-L979)）：用请求独享 scheduler 推进 latent，`step_index += 1`。
- `post_decode`（[L981-L993](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/models/qwen_image/pipeline_qwen_image.py#L981-L993)）：在末块/最终步解码 latent。

输出容器 `DiffusionOutput`（[data.py:L1289-L1361](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/data.py#L1289-L1361)）携带结果张量、流式分块信息（`finished`/`chunk_index`/`total_chunks`）、错误态与 `to_cpu` 跨进程标志，是贯穿 worker→executor→engine 的统一信封。

#### 4.5.4 代码实践

**实践目标**：把 `forward` 的几个阶段对应到行号，建立「读任一 pipeline 都能定位数据流」的能力。

**操作步骤**（源码阅读型）：

1. 打开 [pipeline_qwen_image.py](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/models/qwen_image/pipeline_qwen_image.py) 的 `forward`（L995-L1089）。
2. 完成下表（**示例输出**）：

| 阶段 | 函数 | 行号 | 输入 → 输出 |
| --- | --- | --- | --- |
| 文本编码 | `encode_prompt` | L499-L539 | prompt 字符串 → prompt_embeds |
| 造噪声 | `prepare_latents` | L565-L596 | seed/generator → 打包后的噪声 latent |
| 时间步 | `prepare_timesteps` | L598-L614 | num_inference_steps → timesteps 序列 |
| 去噪 | `diffuse` | L1061-L1080 | latent+embeds+timesteps → 去噪 latent |
| 解码 | `_decode_latents` | L1084 | 去噪 latent → 像素图像 DiffusionOutput |

**需要观察的现象**：`forward` 把 `_prepare_generation_context` 的产物（`ctx[...]`）原样传给 `diffuse`；解码只在去噪完成后发生一次（请求级）。

**预期结果**：你能用一句话描述「prompt 字符串 是如何变成一张 PIL 图的」。结果「待本地验证」。

#### 4.5.5 小练习与答案

**练习 1**：请求级 `forward` 与步级四件套在「文本编码」上有什么共同点？

**参考答案**：两者都复用 `_prepare_generation_context` → `encode_prompt`（[L707-L722](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/models/qwen_image/pipeline_qwen_image.py#L707-L722)）。差别在于：请求级在 `forward` 内一次跑完；步级把同样的上下文写进 `StepRequestState`（`prepare_encode`，[L803-L815](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/models/qwen_image/pipeline_qwen_image.py#L803-L815)），后续多步去噪在 `denoise_step`/`step_scheduler` 里逐步推进。

**练习 2**：为什么步级执行要为每个请求 `deepcopy` 一份 scheduler？

**参考答案**：步级（连续批处理）会把多条请求交错推进，每条请求处于不同的时间步、必须维护各自的调度器内部状态（如 `step_index`）。`prepare_encode` 里 `req_scheduler = copy.deepcopy(self.scheduler)`（[L799](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/models/qwen_image/pipeline_qwen_image.py#L799)）确保请求间状态隔离，避免互相覆盖。

---

## 5. 综合实践

**任务**：以 Qwen-Image（或你熟悉的任一 diffusion pipeline）为对象，绘制一张完整的「请求 → 图片」数据流图，要求同时覆盖**两条执行路径**与**CFG 的两种模式**。

建议步骤：

1. 从 `OmniDiffusionRequest` 出发，标出 `prompt / sampling_params / kv_sender_info` 三个字段（4.1）。
2. 画出请求级路径：`forward → _prepare_generation_context(encode_prompt/prepare_latents/prepare_timesteps) → diffuse → _decode_latents → post_process_func`（4.5）。
3. 在 `diffuse` 内部展开单步循环，分别画出「顺序 CFG（正/负两次前向）」与「CFG 并行（rank 分工 + all_gather）」两个分支，并标注 `combine_cfg_noise` 公式与 `scheduler.step` 的输入输出（4.3/4.4）。
4. 在图侧标注三处「加速/解耦」挂载点：`cache_backend.refresh`（缓存，含 `cache_branch` 正/负分支）、`set_forward_context`（并行组注入）、`kv_sender_info`（跨阶段 KV）。
5. 用一句话总结：扩散推理的耗时大头在 `diffuse` 的多步循环，CFG 使其翻倍（或靠 CFG 并行摊平），而 `encode_prompt`/`vae.decode` 是「一次性」的首尾开销。

完成后，你应当能用这张图向他人解释「vLLM-Omni 是如何把一条文本 prompt 变成一张图片的」。

## 6. 本讲小结

- 扩散请求载荷 `OmniDiffusionRequest` 只携带 `prompt / sampling_params / request_id / kv_sender_info`，并在 `__post_init__` 自动归一化 seed、guidance_scale 与 CFG 开关；`OmniDiffusionSamplingParams` 是「单 prompt」的大口袋，多 prompt 批处理由上层 scheduler 合并。
- `diffuse` 是最耗时的多步去噪循环，每步两件事：`predict_noise`（调 transformer 预测噪声）与 `scheduler.step`（沿时间轴推进 latent）；通用动作在基类 `CFGParallelMixin`，每步输入组装由各模型定制。
- CFG 通过正/负 prompt 双前向 + 合并公式 \(\hat{\epsilon}=\epsilon_{\text{neg}}+s(\epsilon_{\text{pos}}-\epsilon_{\text{neg}})\) 提升质量；CFG 并行用 `all_gather` 分摊双前向，合并与步进因确定性无需再广播。
- `cache_branch`（positive/negative）让缓存加速（TeaCache 等）的正/负两份状态互不污染，分支由 `do_true_cfg` + cfg_rank/前向计数判定。
- 完整数据流为 `encode_prompt → prepare_latents → prepare_timesteps → diffuse → vae.decode → post_process`，请求级在 `forward` 一次跑完，步级拆成 `prepare_encode/denoise_step/step_scheduler/post_decode` 四段。
- 进程级全局配置 `set/get_current_diffusion_config` 用「保存-恢复」上下文让 DiT 的 `Attention` 层在构造期无需层层透参即可读到 `OmniDiffusionConfig`。

## 7. 下一步学习建议

- **深入缓存与注意力加速**：本讲只点到 `cache_backend.refresh` 与 `cache_branch`，建议进入 u7-1（注意力后端 role 感知选择）与 u7-3（TeaCache / Cache-DiT / MagCache），看 `cache_branch` 背后的完整缓存决策机制。
- **理解并行如何叠加在 pipeline 之上**：CFG 并行只是并行之一，u7-2（Ulysses/Ring 序列并行）与 u7-4（TP/SP/DP/CFG/PP）会讲解 `set_forward_context` 注入的并行组如何被 transformer 各层使用。
- **批处理与执行模式**：本讲的 `forward(batch)` 与步级四件套对应 u5-1/u5-2 的 REQUEST_BATCH / STEP_BATCH；u7-5 会专门讲请求级与连续批处理的设计取舍。
- **接入新模型**：若想把本讲的 `diffuse` 骨架迁移到自己的 diffusers pipeline，参考 u9-1（添加新 Diffusion 模型）与 [adding_diffusion_model 指南](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/docs/contributing/model/adding_diffusion_model.md)，了解 attention role 声明与 pre/post 处理注册的完整步骤。
