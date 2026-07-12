# Lite 模块概览与校准流程

## 1. 本讲目标

本讲是量化压缩单元（U7）的第一篇。读完本讲，你应当能够：

- 说清 `lmdeploy lite` 这条命令链的总体形状：**加载 HF 模型 → 跑校准数据收集激活统计 → 算缩放 → 写出量化权重**，并理解「校准（calibrate）」只是其中的「测量」环节，本身并不产出量化模型。
- 看懂 [calibrate.py](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/lite/apis/calibrate.py) 里 `calibrate()` 与 `load_model_and_tokenizer()` 的主流程，理解校准数据集、采样数、序列长度等参数如何影响校准结果。
- 认识 [calibration.py](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/lite/quantization/calibration.py) 里 `CalibrationContext` 这个上下文管理器，理解它如何靠 PyTorch 的 forward hook「不动模型代码」地收集每一层 Linear 的输入/输出统计量。
- 掌握 `lmdeploy lite calibrate` / `lmdeploy lite auto_awq` 等子命令的参数来源（[cli/lite.py](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/cli/lite.py)）。

本讲承接 [u1-l5 命令行工具体系](./u1-l5-cli-toolchain.md)：那里讲的是「敲下 `lmdeploy lite xxx` 之后参数如何被装配、派发」，本讲则进入派发的终点——`calibrate()` 函数内部到底做了什么。

---

## 2. 前置知识

在进入源码前，先用大白话建立三个直觉。

### 2.1 为什么量化需要「校准」

把一个 FP16 权重压成 4bit（INT4），就像把一张高清照片压成 JPEG：信息会丢。问题是，**不同通道的权重对误差的容忍度不同**。如果一刀切地按全局最大值去缩放，那些数值本来就很小的通道会被「噪声」淹没，模型质量暴跌。

业界发现一个关键现象：**激活值（activation，即每一层 Linear 的输入）的幅值分布很不均衡**，少数通道幅值特别大（称为 outlier 离群点），绝大多数通道幅值很小。如果直接按激活的最大值定量化范围，那绝大多数小通道就只能用很少的有效比特，精度损失严重。

解决办法是「平滑（smoothing）」：给第 \(i\) 个输入通道乘一个缩放 \(s_i\)，同时给同一列的权重除以 \(s_i\)，使得

\[
y = x \cdot W = (x \oslash s) \cdot (s \odot W)
\]

（\(\odot,\oslash\) 表示逐通道乘除）。这样数学上结果不变，但激活的动态范围被压小了、权重的被略微放大了，两边都更容易量化。**而 \(s_i\) 该取多大，需要看真实数据跑出来的激活统计量来定**——这就是「校准」要做的事：**用一小批代表性数据跑一遍前向，记录每一层 Linear 输入/输出的最大值、绝对最大值（absmax）、绝对均值（absmean）等统计量，供后续计算缩放使用。**

> 一句话记忆：**校准 = 用校准集跑前向，给每一层 Linear 拍一张「激活幅值快照」**。它不改模型权重，只产出一个统计文件。

### 2.2 三个常被混淆的概念

| 术语 | 全称 | 含义 |
|------|------|------|
| **calibrate（校准）** | calibration | 跑前向收集激活统计，产出 `inputs_stats.pth`。**不碰权重。** |
| **quantize（量化）** | quantization | 根据统计算缩放、平滑、并把权重压成低比特。**改权重。** |
| **export（导出）** | export | 把校准统计写盘，供量化阶段读取。 |

`auto_awq` / `smooth_quant` / `gptq` 这些命令内部都会**先调一次 `calibrate()`**（或读取已有统计），再做量化。所以本讲的 `calibrate` 是所有量化算法的共同前置步骤。

### 2.3 PyTorch 的 hook 机制

`CalibrationContext` 的核心技巧是 PyTorch 的 forward hook：

- `register_forward_pre_hook(fn)`：在模块前向**之前**调用 `fn(module, input)`，可拿到输入张量。
- `register_forward_hook(fn)`：在模块前向**之后**调用 `fn(module, input, output)`，可拿到输出张量。

这样就能在**完全不修改模型源码**的前提下，「旁路」地把每一层 Linear 的输入/输出喂给一个统计观察器（Observer）。这是本讲最需要理解的机制。

---

## 3. 本讲源码地图

| 文件 | 作用 | 本讲用到哪部分 |
|------|------|----------------|
| [lmdeploy/lite/apis/calibrate.py](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/lite/apis/calibrate.py) | 校准主入口：模型加载、数据准备、驱动校准上下文 | `calibrate()`、`load_model_and_tokenizer()`、三张映射表 |
| [lmdeploy/cli/lite.py](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/cli/lite.py) | `lmdeploy lite` 子命令的参数装配与派发 | `SubCliLite`、`add_parser_*`、派发函数 |
| [lmdeploy/lite/quantization/calibration.py](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/lite/quantization/calibration.py) | 校准上下文管理器：挂 hook、收集统计、导出 | `CalibrationContext`、`CalibrationContextV2` |
| [lmdeploy/lite/quantization/activation/observer.py](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/lite/quantization/activation/observer.py) | 激活统计观察器 | `ActivationObserver` |
| [lmdeploy/lite/utils/calib_dataloader.py](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/lite/utils/calib_dataloader.py) | 校准数据集加载与分词 | `get_calib_loaders()` |
| [lmdeploy/lite/utils/load.py](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/lite/utils/load.py) | HF 模型加载（含免初始化技巧） | `load_hf_from_pretrained()`、`LoadNoInit` |
| [lmdeploy/lite/apis/auto_awq.py](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/lite/apis/auto_awq.py) | AWQ 量化入口（演示 calibrate 如何被消费） | `auto_awq()` 里调用 `calibrate` 的片段 |

---

## 4. 核心概念与源码讲解

本讲拆成三个最小模块，按「自顶向下」顺序讲解：

1. **4.1 calibrate 主流程**：顶层编排，先看全貌。
2. **4.2 load_model_and_tokenizer**：校准的第一步——加载模型并做设备分桶准备。
3. **4.3 CalibrationContext**：校准的核心——如何收集激活统计。

### 4.1 calibrate：校准主流程

#### 4.1.1 概念说明

`calibrate()` 是 `lmdeploy lite` 量化链路里最关键的一个函数。它回答的问题是：**给我一个 HF 模型路径，帮我跑完校准，把每一层 Linear 的激活统计写到磁盘。**

它本身**不量化任何权重**，产出物只有两个文件：

- `work_dir/inputs_stats.pth`：所有目标 Linear **输入**的统计（max/min/mean/absmax/absmean）。
- `work_dir/outputs_stats.pth`：所有目标 Linear 与 Norm **输出**的统计。

后续的 `auto_awq` / `smooth_quant` 会读取 `inputs_stats.pth` 里的 `absmax` 或 `absmean` 字段来计算平滑缩放。所以可以把 `calibrate` 理解成一个「独立的测量工序」——它甚至可以单独用 `lmdeploy lite calibrate` 命令运行。

#### 4.1.2 核心流程

`calibrate()` 的执行步骤可以画成下面这条流水线：

```
calibrate(model, calib_dataset, calib_samples, calib_seqlen, ...)
  │
  ├─① load_model_and_tokenizer(model, dtype, work_dir, trust_remote_code)
  │      → arch, vl_model, model, tokenizer, model_type, work_dir
  │
  ├─② （仅 Mixtral 等 MoE）update_moe_mapping 展开专家占位符
  │
  ├─③ 查表得到 layer_type / norm_type
  │      LAYER_TYPE_MAP[model_type] → 如 'LlamaDecoderLayer'
  │      NORM_TYPE_MAP[model_type]  → 如 'LlamaRMSNorm'
  │
  ├─④ _prepare_for_calibrate(model, layer_type, head_name, device)
  │      把 decoder 层和 lm_head 搬到 CPU，其余留 GPU（省显存）
  │
  ├─⑤ get_calib_loaders(calib_dataset, tokenizer, nsamples, seqlen)
  │      → 一组已分词的 token 张量，每条长度 = seqlen
  │
  ├─⑥ 构造 calib_ctx：
  │      search_scale=True  → CalibrationContextV2（搜索最佳缩放比例）
  │      search_scale=False → CalibrationContext（只收统计）
  │
  ├─⑦ with calib_ctx:
  │        all_data = torch.cat(calib_loader).to(device)
  │        calib_ctx.calibrate(all_data)   # 真正跑前向，hook 收集统计
  │
  └─⑧ calib_ctx.export(work_dir)          # 写 inputs_stats.pth / outputs_stats.pth
```

**记忆要点**：① 装载 → ③④ 把模型「摆好姿势」→ ⑤ 准备尺子（数据）→ ⑥⑦ 测量 → ⑧ 记录结果。

#### 4.1.3 源码精读

先看函数签名与参数默认值（这些默认值与 CLI 完全一致）：

[calibrate.py:283-294](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/lite/apis/calibrate.py#L283-L294) —— `calibrate()` 的签名。`calib_dataset='wikitext2'`、`calib_samples=128`、`calib_seqlen=2048`、`w_bits=4`、`w_group_size=128`、`search_scale=False`。

接下来三段是函数体的关键：

```python
# calibrate.py:325-328  校验数据集白名单
assert calib_dataset in ['wikitext2', 'c4', 'pileval',
                         'gsm8k', 'neuralmagic_calibration', 'open-platypus', 'openwebtext'], \
    'Support only ...'
```

[calibrate.py:330-339](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/lite/apis/calibrate.py#L330-L339) —— 步骤 ①②③④：加载模型、按 `type(model).__name__` 查 `LAYER_TYPE_MAP`/`NORM_TYPE_MAP` 得到层类型与 norm 类型，再调 `_prepare_for_calibrate` 做设备分桶。

```python
# calibrate.py:345-361  根据 search_scale 选两个上下文之一
if search_scale:
    calib_ctx = CalibrationContextV2(model, tokenizer, layer_type=layer_type,
                                     norm_type=norm_type, device=device,
                                     w_bits=w_bits, w_group_size=w_group_size,
                                     batch_size=batch_size, search_scale=search_scale)
else:
    calib_ctx = CalibrationContext(model, tokenizer, layer_type=layer_type,
                                   norm_type=norm_type, batch_size=batch_size,
                                   device=device)
```

[calibrate.py:363-367](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/lite/apis/calibrate.py#L363-L367) —— 步骤 ⑦⑧：进入上下文（`with` 会触发 `__enter__` 挂 hook），把所有校准样本拼成一个大 batch 跑一次前向，`calibrate()` 方法内部用 `torch.inference_mode()` 包裹；退出后调 `export` 写盘。

注意一个细节：**`with calib_ctx:` 的进入与退出，正是 hook 的挂载与卸载时机**。退出 `with` 块后，模型恢复原状（hook 被移除、forward 被还原），但统计量已经留在 `calib_ctx` 内部，由 `export` 落盘。

#### 4.1.4 代码实践

**实践目标**：理解「校准只产统计文件、不产量化模型」这一关键事实。

**操作步骤**（源码阅读型实践，无需 GPU）：

1. 打开 [calibrate.py](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/lite/apis/calibrate.py)，定位 `calibrate()` 函数末尾（第 367–369 行），确认它的产出只有 `calib_ctx.export(work_dir)` 和返回值。
2. 打开 [auto_awq.py:95-106](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/lite/apis/auto_awq.py#L95-L106)，看到 `auto_awq` 内部确实调用了 `calibrate(...)`。
3. 再看 [auto_awq.py:118-128](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/lite/apis/auto_awq.py#L118-L128)，确认量化阶段是从 `work_dir/inputs_stats.pth` 里读出 `absmax`（非搜索）或 `absmean`+`ratios`（搜索）。

**需要观察的现象**：`calibrate` 与「量化权重」之间唯一的耦合点，就是 `inputs_stats.pth` 这个文件。

**预期结果**：你能用自己的话回答——「校准和量化是两个独立工序，校准的产出是一个 `.pth` 统计文件」。运行命令本身**待本地验证**（需要可下载的模型与 GPU）。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `calib_samples` 设为 0，会发生什么？（提示：看 [auto_awq.py:89-94](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/lite/apis/auto_awq.py#L89-L94)）

> **答案**：`auto_awq` 里若 `calib_samples == 0`，会走 `load_model_and_tokenizer` 而不是 `calibrate`，即「data-free 量化」——跳过统计收集，直接用默认策略量化权重（精度通常更差，但无需校准数据）。

**练习 2**：`calibrate()` 函数里为什么要把校准数据用 `torch.cat` 拼成 `all_data` 再喂给 `calib_ctx.calibrate`，而不是一条一条喂？

> **答案**：因为 `CalibrationContext._wrap_decoder_layers` 会按 `batch_size` 把大 batch 切成小批逐层处理（见 4.3），这样做既能一次性把数据送进 GPU，又能让上下文内部的「逐层上卡」逻辑统一处理分批，简化控制流。

---

### 4.2 load_model_and_tokenizer：模型加载与分桶准备

#### 4.2.1 概念说明

这是校准链路的第一步。它做三件事：

1. **判定任务类型**：这个模型是纯文本 LLM 还是多模态 VLM？两者加载路径不同。
2. **加载模型与分词器**：用 `transformers` 把 HF 权重读进来，按 `dtype` 决定精度。
3. **查表登记**：根据模型类名（如 `LlamaForCausalLM`），从三张映射表里查出它对应的「decoder 层类名」「norm 类名」「输出头属性名」，这些是后续挂 hook 的目标。

这里出现一个贯穿全讲的术语：**model_type**。它指的是 `type(model).__name__`，即模型最外层类的名字（如 `LlamaForCausalLM`、`Qwen2ForCausalLM`）。它是校准流程的「身份证」——所有目标层的定位都靠它查表。

#### 4.2.2 核心流程

```
load_model_and_tokenizer(model, dtype, work_dir, trust_remote_code)
  │
  ├─① get_task(backend, model_path) → 'llm' 或 'vlm'
  │      内部调 check_vl_llm 看 architectures 是否在多模态白名单
  │
  ├─② tokenizer = AutoTokenizer.from_pretrained(model)
  │
  ├─③ arch, original_config = get_model_arch(model)   # 读 config.json
  │
  ├─④ 按 model_type 分支加载：
  │      'llm' → load_hf_from_pretrained(model, dtype)        # 纯文本
  │      'vlm' → load_vl_model(...).vl_model/language_model   # 多模态
  │
  ├─⑤ model_type = type(model).__name__
  │      校验 model_type 必须在 LAYER_TYPE_MAP / NORM_TYPE_MAP 里，否则报错
  │
  └─⑥ 创建 work_dir，返回 arch, vl_model, model, tokenizer, model_type, work_dir
```

三张映射表的关系：

| 表名 | 键 | 值 | 用途 |
|------|----|----|------|
| `LAYER_TYPE_MAP` | `LlamaForCausalLM` | `LlamaDecoderLayer` | 找到 decoder 层（要逐层上卡） |
| `NORM_TYPE_MAP` | `LlamaForCausalLM` | `LlamaRMSNorm` | 找到归一化层（smooth 的锚点） |
| `HEAD_NAME_MAP` | `LlamaForCausalLM` | `lm_head` | 找到输出头（搬 CPU 省 VRAM） |

#### 4.2.3 源码精读

三张映射表的开头：

[calibrate.py:14-32](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/lite/apis/calibrate.py#L14-L32) —— `LAYER_TYPE_MAP`，把每个模型类名映射到它的 decoder 层类名。可以看到支持 InternLM2/3、Qwen2/3/3.5、Llama、Phi3、ChatGLM、Mixtral、Mistral 等一大族。

[calibrate.py:54-72](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/lite/apis/calibrate.py#L54-L72) —— `HEAD_NAME_MAP`。注意一个有意思的差异：InternLM2/3 和 ChatGLM 的输出头叫 `output`/`output_layer`，而绝大多数新模型用 `lm_head`。

任务判定函数：

[calibrate.py:102-110](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/lite/apis/calibrate.py#L102-L110) —— `get_task`：先读 HF `config.json` 拿到 `arch`，再调 `check_vl_llm` 判断是否多模态，是则返回 `'vlm'`，否则默认 `'llm'`。注意这里的 `get_task` 与 [archs.py](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/archs.py) 里同名函数是**两套独立实现**（本讲的在 lite 里，服务于量化加载）。

加载分支：

[calibrate.py:247-268](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/lite/apis/calibrate.py#L247-L268) —— `load_model_and_tokenizer` 的加载核心。`llm` 分支直接 `load_hf_from_pretrained`；`vlm` 分支更复杂：先 `load_vl_model(with_llm=True)` 拿到完整 VLM，再根据它是 `language_model`（deepseek-vl 等）还是 `llm`（MiniCPMV 等）属性取出真正的语言模型骨干，并强制 `use_cache=False`。

校验与报错：

[calibrate.py:270-280](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/lite/apis/calibrate.py#L270-L280) —— 取 `model_type`，若不在映射表里就抛 `RuntimeError` 并列出所有支持的模型。这是「为什么有的模型能 AWQ、有的不能」的根因——**不是量化算法不支持，而是校准阶段缺它的层映射表**。

顺带看一眼 `load_hf_from_pretrained` 里的免初始化技巧：

[load.py:73-82](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/lite/utils/load.py#L73-L82) —— 用 `with LoadNoInit():` 包裹 `from_pretrained`。`LoadNoInit`（[load.py:9-44](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/lite/utils/load.py#L9-L44)）临时把所有 `torch.nn.init.*` 替换成空操作，**避免加载大模型时先随机初始化一遍参数再覆盖**，省下大量无谓的内存与时间。

#### 4.2.4 代码实践

**实践目标**：搞清「校准阶段如何根据模型类名定位目标层」。

**操作步骤**（源码阅读型实践）：

1. 在 [calibrate.py:14-72](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/lite/apis/calibrate.py#L14-L72) 三张表里，找出 `Qwen2ForCausalLM` 对应的 layer/norm/head 三个值。
2. 假设你要支持一个新模型 `MyModelForCausalLM`，它的 decoder 层叫 `MyDecoderLayer`、norm 叫 `MyRMSNorm`、输出头是 `lm_head`。写出需要往三张表各加一行的伪代码。

**需要观察的现象**：三张表的键完全一致（都是同一批模型类名），新增模型必须**三张表同时加**。

**预期结果**：

```
Qwen2ForCausalLM → layer: Qwen2DecoderLayer, norm: Qwen2RMSNorm, head: lm_head
```

新增模型伪代码（示例代码，非项目原有）：

```python
LAYER_TYPE_MAP['MyModelForCausalLM'] = 'MyDecoderLayer'
NORM_TYPE_MAP['MyModelForCausalLM'] = 'MyRMSNorm'
HEAD_NAME_MAP['MyModelForCausalLM'] = 'lm_head'
```

#### 4.2.5 小练习与答案

**练习 1**：为什么 `load_hf_from_pretrained` 要强制 `model.config.use_cache = False`？

> **答案**：校准只是跑前向收集统计，不需要也不应该生成 KV cache（会浪费显存且无意义），所以关闭 `use_cache`。

**练习 2**：`_prepare_for_calibrate` 把 decoder 层和 `lm_head` 搬到 CPU、其余模块留在 GPU，目的是什么？

> **答案**：校准时采用的是「逐层上卡」策略（见 4.3 的 `_wrap_decoder_layers`）：先把所有 decoder 层放 CPU 待命，轮到某层计算时才把它 `.to(device)` 搬上 GPU、算完再搬回 CPU。这样**同一时刻 GPU 上只占一层 decoder 的显存**，让大模型能在显存有限的卡上完成校准。`lm_head` 搬 CPU 是因为它在校准阶段用不到（不计算最终 logits）。源码见 [calibrate.py:113-180](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/lite/apis/calibrate.py#L113-L180)。

---

### 4.3 CalibrationContext：激活统计的收集器

#### 4.3.1 概念说明

`CalibrationContext` 是整个校准流程的灵魂。它是一个**上下文管理器**（实现了 `__enter__` / `__exit__`），用法是：

```python
with calib_ctx:
    calib_ctx.calibrate(all_data)   # 跑前向，期间 hook 自动收集统计
calib_ctx.export(work_dir)          # 退出后导出
```

它的核心思想是：**「不动模型一行代码」，靠 PyTorch 的 forward hook 把每一层 Linear 的输入/输出截下来，喂给一个统计观察器。** 进入 `with` 时挂 hook、退出时卸 hook，模型在 `with` 块外完全恢复原状。

`CalibrationContext` 还有一个子类 `CalibrationContextV2`，多了一个能力：**搜索最佳平滑缩放比例（search_scale）**。两者的区别放在最后讲。

#### 4.3.2 核心流程

`CalibrationContext` 的生命周期：

```
__init__（构造期，不碰模型前向）
  │  ├─ collect_target_modules 收集 name2layer / name2fc / name2norm
  │  ├─ bimap_name_mod 建 模块↔名字 双向映射
  │  └─ 为每个 Linear 的输入/输出、每个 Norm/Linear 的输出 创建 ActivationObserver
  │
__enter__（进入 with，开始「武装」模型）
  │  ├─ _insert_input_observers   → 每个 Linear 挂 register_forward_pre_hook
  │  ├─ _insert_output_observers  → 每个 Norm/Linear 挂 register_forward_hook
  │  └─ _wrap_decoder_layers      → 包装 decoder 层 forward，做「逐层上卡 + 分批」
  │
calibrate(data)
  │  └─ torch.inference_mode 下 model(data)  → hook 被触发，Observer 累积统计
  │
__exit__（退出 with，「解除武装」）
  │  ├─ 移除所有 hook
  │  └─ 还原 decoder 层的原始 forward
  │
export(out_dir)
  │  ├─ collect_inputs_stats  → 聚合所有输入 Observer 的 max/min/absmax/absmean
  │  └─ collect_outputs_stats → 聚合所有输出 Observer 的统计
  │  写出 inputs_stats.pth / outputs_stats.pth
```

**统计量的数学定义**（在 `ActivationObserver.observe` 里）：对输入张量 \(x\)（形状 `(batch, seqlen, dim)`），先 `flatten(0,1)` 成 `(N, dim)`，再沿第 0 维（所有 token） reductions：

\[
\text{absmax}_d = \max_{n} |x_{n,d}|, \quad
\text{absmean}_d = \frac{1}{N}\sum_{n} |x_{n,d}|
\]

其中 \(d\) 是通道维。这些 per-channel 的统计量，就是后续计算平滑缩放 \(s_d\) 的原料。

#### 4.3.3 源码精读

**构造期——收集目标模块**：

[calibration.py:66-80](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/lite/quantization/calibration.py#L66-L80) —— 用 `collect_target_modules` 把所有 decoder 层（`name2layer`）、层内的所有 `nn.Linear`（`name2fc`）、所有 norm（`name2norm`）收集起来，再用 `bimap_name_mod` 建立「模块 ↔ 名字」双向映射（hook 里要靠模块反查名字，再找到对应的 Observer）。然后为每个目标模块创建 `ActivationObserver`。

**挂 hook——核心技巧**：

[calibration.py:114-129](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/lite/quantization/calibration.py#L114-L129) —— `_insert_input_observers`：给每个 Linear 注册 `register_forward_pre_hook(_input_hook)`。`_input_hook` 拿到模块的输入，用 `mod2name[mod]` 反查名字，再用 `ActivationObserver.find(name, group)` 找到对应观察器，调 `obs.observe(inp[0])`。

```python
def _input_hook(mod, inp):
    m_name = self.mod2name[mod]                       # 模块 → 名字
    obs = ActivationObserver.find(m_name, group=...)  # 名字 → 观察器
    obs.observe(inp[0])                               # 累积统计
```

[calibration.py:131-146](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/lite/quantization/calibration.py#L131-L146) —— `_insert_output_observers` 同理，只是用 `register_forward_hook` 拿输出。

> 这里用到一个跨模块的「全局注册表」机制：`ActivationObserver.global_available(name, group)` 把观察器登记进一个按 `group`（`'inputs'`/`'outputs'`）分组的全局表，hook 里用 `find`/`find_group` 取回。这就是 `GlobalAvailMixin` 提供的能力（见 [observer.py](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/lite/quantization/activation/observer.py)），让 hook 闭包能跨函数找到对应 Observer。

**逐层上卡——省显存的关键**：

[calibration.py:148-176](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/lite/quantization/calibration.py#L148-L176) —— `_wrap_decoder_layers` 把每个 decoder 层的 `forward` 替换成自定义的 `_forward`：进入时 `.to(device)` 上卡，按 `batch_size` 把入参切成小批（`split_decoder_layer_inputs`）逐批跑原始 forward，跑完 `concat_decoder_layer_outputs` 拼回，最后 `.to('cpu')` 下卡并 `torch.cuda.empty_cache()`。这就是「同一时刻 GPU 只占一层」的实现。

**真正跑前向**：

[calibration.py:226-235](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/lite/quantization/calibration.py#L226-L235) —— `calibrate(data)`：在 `torch.inference_mode()` 下跑一次 `model(data)`。注意它取的是 `self.model.model`（即 transformer 主体），跳过 `lm_head`（前面已搬 CPU），所以只算到隐藏态，省掉 logits 的巨大显存。这一步触发所有 hook，统计被累积进各 Observer。

**导出统计**：

[calibration.py:209-224](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/lite/quantization/calibration.py#L209-L224) —— `export(out_dir)`：调 `collect_inputs_stats` / `collect_outputs_stats`，把每个 Observer 的 `max_val/min_val/mean_val/absmax_val/absmean_val` 聚合成字典，分别存成 `inputs_stats.pth` 与 `outputs_stats.pth`。

**统计观察器的内部**：

[observer.py:88-124](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/lite/quantization/activation/observer.py#L88-L124) —— `ActivationObserver.observe`：核心就是上文那几个 reductions，注意 `absmax_val`/`max_val` 用 `torch.maximum` 做**跨 batch 的逐步累积**（每来一个 batch 取更大值），而 `mean_val`/`absmean_val` 用「累积和除以批数」做**增量平均**。`num_batches_tracked` 记录看过几个 batch。

**进阶：CalibrationContextV2 与 search_scale**：

[calibration.py:345-461](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/lite/quantization/calibration.py#L345-L461) —— `CalibrationContextV2` 继承 `CalibrationContext`，多搜一个 `ratio`（平滑缩放强度）。它包装的 `_wrap_decoder_layers_for_search` 在每层前向时，额外调 [auto_scale_block](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/lite/quantization/calibration.py#L262-L342)：对每个 norm 后接的 Linear 组，在 `ratio ∈ [0, 1)` 上网格搜索 20 档，挑量化误差最小的比例。导出时 `inputs_stats.pth` 会多一个 `ratios` 字段。这是 AWQ 原论文「自动搜索缩放比例」的实现，细节留到 [u7-l2 AWQ 量化原理与实现](./u7-l2-awq-quantization.md) 展开。

#### 4.3.4 代码实践

**实践目标**：亲手验证「hook 能在不改模型的前提下收集统计」。

**操作步骤**（最小可运行示例——可在纯 CPU 上跑，不需要真实大模型）：

1. 写一个只有 2 层 Linear 的玩具模型，模仿 `CalibrationContext` 的思路给它挂 forward pre-hook，在 hook 里打印输入的 `absmax`。
2. 跑一次随机输入前向，观察 hook 是否被触发、统计是否被记录。

示例代码（**非项目原有代码**，仅为演示 hook 机制）：

```python
import torch
from torch import nn

class ToyModel(nn.Module):
    def __init__(self):
        super().__init__()
        self.fc1 = nn.Linear(8, 8)
        self.fc2 = nn.Linear(8, 4)
    def forward(self, x):
        return self.fc2(torch.relu(self.fc1(x)))

model = ToyModel()
stats = {}

def hook(mod, inp):
    name = {model.fc1: 'fc1', model.fc2: 'fc2'}[mod]
    stats[name] = inp[0].abs().max().item()
    print(f'{name} input absmax = {stats[name]:.4f}')

model.fc1.register_forward_pre_hook(hook)
model.fc2.register_forward_pre_hook(hook)

with torch.no_grad():
    out = model(torch.randn(3, 5, 8))   # batch=3, seqlen=5, dim=8
print('collected:', stats)
```

**需要观察的现象**：前向过程中，`fc1` 与 `fc2` 的 hook 依次被触发，`stats` 字典被填充。

**预期结果**：打印出两个非零的 `absmax` 值，且 `fc1` 的输入正是你喂入的随机张量的 absmax，`fc2` 的输入是 relu 后的 absmax。这就复现了 `CalibrationContext` 用 hook 收集 `inputs_stats` 的核心机制。本示例可在 CPU 上直接运行验证。

#### 4.3.5 小练习与答案

**练习 1**：`CalibrationContext` 为什么要区分 `inp_obs_group='inputs'` 和 `out_obs_group='outputs'` 两组 Observer？

> **答案**：因为同一个 Linear 模块，它的**输入**统计（用于 AWQ/SmoothQuant 算平滑缩放）和**输出**统计（用于别的分析或 W8A8）是两套独立的量。分成两个 group，hook 才能精确地把「输入」喂给输入组 Observer、「输出」喂给输出组 Observer，互不污染。

**练习 2**：`calibrate()` 方法里取 `self.model.model` 而不是 `self.model`，为什么？

> **答案**：`self.model` 是 `XxxForCausalLM`（含 transformer 主体 + `lm_head`），而 `self.model.model` 是纯 transformer 主体。`lm_head` 已被 `_prepare_for_calibrate` 搬到 CPU，且校准不需要最终 logits，所以只跑主体到隐藏态即可，省掉 logits 巨大的显存开销。ChatGLM 是特例（取 `self.model.transformer`），见 [calibration.py:229-232](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/lite/quantization/calibration.py#L229-L232)。

---

## 5. 综合实践

把本讲三个模块串起来，完成一个**「追踪一条数据从命令行到统计文件」的完整源码阅读任务**。

**任务**：假设用户敲下

```bash
lmdeploy lite auto_awq Qwen/Qwen2.5-7B-Instruct \
    --calib-dataset c4 --calib-samples 64 --calib-seqlen 2048 --w-bits 4
```

请按下面顺序在源码里标出每一步的文件与行号，画出数据流转图：

1. **CLI 装配**：[cli/lite.py:18-42](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/cli/lite.py#L18-L42) 里 `auto_awq` 子命令如何把 `--calib-dataset` 等参数挂上去（注意它们大多来自 `ArgumentHelper.calib_*`，见 [cli/utils.py:405-453](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/cli/utils.py#L405-L453)）。
2. **派发**：[cli/lite.py:111-115](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/cli/lite.py#L111-L115) 的 `SubCliLite.auto_awq(args)` 如何用 `convert_args` 把命名空间转成 kwargs，再调 `apis.auto_awq.auto_awq(**kwargs)`。
3. **进入校准**：[auto_awq.py:95-106](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/lite/apis/auto_awq.py#L95-L106) 里 `calib_samples != 0`，于是调 `calibrate(...)`。
4. **加载模型**：[calibrate.py:330-331](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/lite/apis/calibrate.py#L330-L331) → `load_model_and_tokenizer`（4.2）。
5. **准备数据**：[calibrate.py:342](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/lite/apis/calibrate.py#L342) → `get_calib_loaders('c4', tokenizer, nsamples=64, seqlen=2048)`，进入 [calib_dataloader.py:128-155](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/lite/utils/calib_dataloader.py#L128-L155) 的 `get_c4`，返回 64 条长度 2048 的 token 张量。
6. **挂 hook 跑前向**：[calibrate.py:363-365](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/lite/apis/calibrate.py#L363-L365) 进入 `CalibrationContext`（4.3），hook 收集每一层 Linear 输入的 absmax/absmean。
7. **写盘**：[calibrate.py:367](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/lite/apis/calibrate.py#L367) → `export`，产出 `work_dir/inputs_stats.pth`。
8. **消费统计**：回到 [auto_awq.py:121-128](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/lite/apis/auto_awq.py#L121-L128)，读出 `absmax` 喂给 `smooth_layers`。

**产出物**：一张包含上述 8 个节点的流程图（手绘或用文字箭头即可），每个节点标注 `文件:行号` 与一句话职责。

**预期结果**：你能指着图说——「`c4` 数据集的 64 条样本，经过 tokenizer 分词后，在 `CalibrationContext` 的 hook 下被转成每一层 Linear 的 absmax 统计，落进 `inputs_stats.pth`，最后被 AWQ 平滑层消费」。

---

## 6. 本讲小结

- **校准 ≠ 量化**：`calibrate()` 只跑前向收集激活统计，产出 `inputs_stats.pth` / `outputs_stats.pth`；真正的权重压制定量发生在 `auto_awq` / `smooth_quant` / `gptq` 里，它们读取统计来算平滑缩放。
- **`calibrate()` 主流程**七步：加载模型 → 查层映射表 → 设备分桶 → 准备校准数据 → 构造上下文 → `with` 跑前向 → 导出统计。
- **`load_model_and_tokenizer`** 是加载入口：先用 `get_task` 区分 LLM/VLM，再用 `load_hf_from_pretrained`（含 `LoadNoInit` 免初始化技巧）加载；模型类名（`model_type`）是查 `LAYER_TYPE_MAP`/`NORM_TYPE_MAP`/`HEAD_NAME_MAP` 三张表的身份证。
- **`CalibrationContext`** 是核心：靠 PyTorch forward hook「不动模型代码」地收集统计，`__enter__` 挂 hook、`__exit__` 卸 hook；`_wrap_decoder_layers` 实现「逐层上卡」省显存。
- **统计量**是 per-channel 的 `max/min/mean/absmax/absmean`，由 `ActivationObserver.observe` 跨 batch 累积；`absmax` 给 SmoothQuant、`absmean`+`ratios` 给 AWQ。
- **`CalibrationContextV2`** 在 V1 基础上多搜一个最佳平滑比例 `ratio`（`auto_scale_block` 网格搜索 20 档），导出的统计多一个 `ratios` 字段。

---

## 7. 下一步学习建议

本讲建立了「校准 = 收集激活统计」的全局图景。建议接下来：

1. **[u7-l2 AWQ 量化原理与实现](./u7-l2-awq-quantization.md)**：看 `auto_awq` 如何消费本讲产出的 `inputs_stats.pth`，用 `NORM_FCS_MAP`/`FC_FCS_MAP` 逐层算平滑缩放、做激活感知权重量化。这是本讲的直接下游。
2. **[u7-l3 GPTQ 与 SmoothQuant](./u7-l3-gptq-smoothquant.md)**：对比 W8A8（SmoothQuant）与 GPTQ 两条不同的量化路线，理解它们如何共用同一个校准产物。
3. **[u7-l4 权重与激活量化器](./u7-l4-weight-activation-quantizers.md)**：深入 `ActivationObserver` 与权重量化器的底层接口。
4. 想了解量化后的权重如何在推理引擎里被加载使用，可跳到 [u5-l2 线性层与权重量化变体](./u5-l2-linear-quant-variants.md)，看 `AwqLinear`/`W8A8Linear` 如何读取量化权重做前向。
