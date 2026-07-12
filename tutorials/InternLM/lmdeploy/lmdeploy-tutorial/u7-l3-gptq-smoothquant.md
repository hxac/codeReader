# GPTQ 与 SmoothQuant

## 1. 本讲目标

U7 单元到目前为止已经讲过两条量化路线：u7-l1 的「校准」基础设施，以及 u7-l2 的 AWQ 权重量化。本讲把 lmdeploy `lite` 模块剩余两条量化路线一次性讲清楚。读完本讲，你应当能够：

1. 说清 **GPTQ** 与 **SmoothQuant** 这两种算法各自要解决什么问题、量化对象是什么（只压权重，还是权重+激活一起压）。
2. 看懂 `lite/apis/gptq.py` 与 `lite/apis/smooth_quant.py` 两个入口函数的全部参数与执行步骤，理解它们为何一个「外包」给第三方库、一个「自研」复用校准产物。
3. 认识 `lite/modeling/` 下逐模型 GPTQ 实现的作用：为什么要为每个模型族写一个类，类里那四个属性各代表什么。
4. 能够对比 `auto_awq`、`auto_gptq`、`smooth_quant` 三个 CLI 子命令的参数差异，并解释差异背后的原因。

---

## 2. 前置知识

本讲是 **advanced** 阶段，默认你已读过 u7-l1（校准流程）与 u7-l2（AWQ）。我们快速回顾三件承接过来的关键事实：

- **校准（calibration）≠ 量化**。`calibrate()` 只跑前向、用 forward hook 收集每层 Linear 的输入激活统计量（`absmax`/`absmean`），写到 `work_dir/inputs_stats.pth`，不改任何权重。它是 AWQ 和 SmoothQuant 的共同前置步骤。
- **平滑（smoothing）** 是 AWQ 和 SmoothQuant 共用的数学技巧：把激活里的「离群点（outlier）」难度迁移到权重上。核心恒等式为

  \[ y = Wx = \bigl(W \cdot \mathrm{diag}(s)\bigr)\bigl(\mathrm{diag}(s)^{-1}x\bigr), \qquad s_j = \frac{\max(|x_j|)^{\alpha}}{\max(|W_{:,j}|)^{1-\alpha}} \]

  其中 \(\alpha\) 默认 0.5。`awq.py` 的 `smooth_layers` / `smooth_ln_fcs` / `smooth_fc_fcs` 就是这条公式的实现。
- **量化对象分两类**：
  - **weight-only**（如 AWQ、GPTQ）：只把权重压成 4bit，激活仍用高精度（FP16）。省显存、不省算力。
  - **weight + activation**（如 SmoothQuant）：权重和激活都压成 8bit，能用 INT8/FP8 GEMM 算矩阵乘，省算力、提吞吐。

把这三件事记牢，下面三种算法的位置就能对齐：

| 算法 | 入口 | 量化对象 | 校准框架 | 实现归属 |
|---|---|---|---|---|
| AWQ | `auto_awq` | weight-only 4bit | lmdeploy 自研 `CalibrationContext` | lmdeploy 原生 |
| **GPTQ** | `auto_gptq` | weight-only 4bit | **第三方 `auto-gptq` 库** | 外包 |
| **SmoothQuant** | `smooth_quant` | W8A8 | lmdeploy 自研 `CalibrationContext` | lmdeploy 原生 |

GPTQ 是这张表里的「异类」——它不碰 lmdeploy 的校准框架，而是把整个量化过程委托给第三方 `auto-gptq` 库，lmdeploy 只负责「告诉 auto-gptq 怎么认识 InternLM 模型」。这正是本讲三个最小模块的内在逻辑。

---

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [lmdeploy/lite/apis/gptq.py](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/lite/apis/gptq.py) | GPTQ 量化入口 `auto_gptq`，薄包装第三方 `auto-gptq` 库 |
| [lmdeploy/lite/apis/smooth_quant.py](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/lite/apis/smooth_quant.py) | SmoothQuant（W8A8）量化入口 `smooth_quant`，lmdeploy 原生 |
| [lmdeploy/lite/modeling/internlm2_gptq.py](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/lite/modeling/internlm2_gptq.py) | 为 InternLM2 注册到 auto-gptq 的模型描述类 |
| [lmdeploy/lite/modeling/internlm3_gptq.py](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/lite/modeling/internlm3_gptq.py) | 为 InternLM3 注册到 auto-gptq 的模型描述类 |
| [lmdeploy/cli/lite.py](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/lite/../../lmdeploy/cli/lite.py) | CLI 子命令注册，`auto_gptq` / `smooth_quant` 的参数都在这里定义 |
| [lmdeploy/lite/apis/auto_awq.py](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/lite/apis/auto_awq.py) | AWQ 入口，用于对比「自研」路线 |
| [lmdeploy/lite/quantization/awq.py](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/lite/quantization/awq.py) | `smooth_layers` / `skipped_module` 等 SmoothQuant 复用的工具函数 |
| [lmdeploy/pytorch/models/q_modules.py](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/models/q_modules.py) | `QLinear` / `QRMSNorm`：SmoothQuant 产出的 W8A8 算子 |

---

## 4. 核心概念与源码讲解

### 4.1 GPTQ 入口：外包给第三方库的权重量化

#### 4.1.1 概念说明

GPTQ（**G**eneralized **P**ost-**T**raining **Q**uantization）出自 2022 年同名论文，是一种 **weight-only** 训练后量化算法，和 AWQ 一样把权重压成 4bit、激活保持 FP16。但它的「怎么压」与 AWQ 完全不同：

- AWQ 的核心是**平滑**——先把激活离群点吸收进权重，再用简单的分组量化（`pseudo_quantize_tensor`）压权重。
- GPTQ 的核心是**误差补偿**——逐列（per output channel）量化权重，每压完一列，就用二阶信息（海森矩阵的逆）去修正还没压的那些列，让总输出误差最小。

直觉上，GPTQ 是「贪心 + 反馈」：量化第 \(f\) 列会引入误差 \(\delta_f = w_f - \hat{w}_f\)，这个误差会让后续输入经过剩余列时产生偏差，于是提前把偏差从剩余列里扣掉，相当于「这一列压错了，剩下的列帮我兜底」。

关键的数学表达：设某层在校准数据上的输入为 \(X\)（形状 \([d_{in}, n]\)），海森矩阵取

\[
H = X X^{\mathsf{T}} \in \mathbb{R}^{d_{in}\times d_{in}},
\]

记其逆为 \(H^{-1}\)。对第 \(f\) 列权重量化得到 \(\hat{w}_f\) 后，对**尚未量化的列集合** \(\mathcal{C}\setminus\{f\}\) 做如下补偿更新：

\[
W_{:,\,\mathcal{C}\setminus f} \;\leftarrow\; W_{:,\,\mathcal{C}\setminus f} \;-\; \frac{w_f - \hat{w}_f}{[H^{-1}]_{ff}} \cdot [H^{-1}]_{f,\;\mathcal{C}\setminus f}
\]

分母 \([H^{-1}]_{ff}\) 是个标量，分子 \([H^{-1}]_{f,\;\mathcal{C}\setminus f}\) 是一行向量，决定了「误差往哪些列、按多大比例回灌」。GPTQ 论文还用 \(H^{-1}\) 的 Cholesky 分解来保证数值稳定。你不必背公式，只需记住一句话：**GPTQ 用二阶（海森）信息决定每一列量化后如何补偿剩余列，所以量化顺序很重要**——这正是 4.3 节 `inside_layer_modules` 存在的理由。

#### 4.1.2 核心流程

`auto_gptq` 的流程非常短，因为它把脏活全交给了 `auto-gptq` 库：

```
1. 尝试 import auto_gptq；失败则报「请 pip install auto-gptq」
2. 把 InternLM2 / InternLM3 注册进 auto-gptq 的支持表（SUPPORTED_MODELS + GPTQ_CAUSAL_LM_MODEL_MAP）
3. 读 tokenizer，用 get_calib_loaders 准备校准样本（每个样本是 {input_ids, attention_mask}）
4. 构造 BaseQuantizeConfig(bits, group_size, desc_act=False, sym=True)  ← lmdeploy 只支持这两种取值
5. AutoGPTQForCausalLM.from_pretrained(...) 加载未量化模型到 GPU
6. model.quantize(examples, batch_size)              ← auto-gptq 在这里跑海森 + 逐列量化
7. model.save_quantized(work_dir) + tokenizer.save_pretrained(work_dir)
```

注意它与 AWQ/SmoothQuant 的根本区别：**它不调用 lmdeploy 的 `calibrate()`**，也没有 `CalibrationContext`。海森矩阵的收集与逐列量化，全在 `model.quantize(...)` 这一句里由 auto-gptq 自己完成。

#### 4.1.3 源码精读

入口函数签名与校准参数，注意默认 `w_bits=4`、`w_group_size=128`，和 AWQ 完全一致：

- [lmdeploy/lite/apis/gptq.py:L11-L21](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/lite/apis/gptq.py#L11-L21) — `auto_gptq` 函数签名。这里有个**值得注意的文档错误**：它的 docstring 第 22 行写的是 `Perform weight quantization using AWQ algorithm.`，是从 `auto_awq.py` 复制时忘改的——实际跑的是 GPTQ。阅读源码时不要被这句误导。

第一步，强制依赖第三方库：

- [lmdeploy/lite/apis/gptq.py:L41-L45](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/lite/apis/gptq.py#L41-L45) — `from auto_gptq import AutoGPTQForCausalLM, BaseQuantizeConfig` 包在 try/except 里，失败就抛 `ImportError` 提示 `pip install auto-gptq`。这说明 GPTQ 是**可选依赖**，不装这个库，`auto_gptq` 根本无法运行（AWQ/SmoothQuant 则不需要它）。

第二步，把 InternLM 注册进 auto-gptq：

- [lmdeploy/lite/apis/gptq.py:L51-L60](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/lite/apis/gptq.py#L51-L60) — 把 `'internlm2'` / `'internlm3'` 追加进 `SUPPORTED_MODELS`，并把 4.3 节要讲的 `InternLM2GPTQForCausalLM` / `InternLM3GPTQForCausalLM` 注册进 `GPTQ_CAUSAL_LM_MODEL_MAP`。这是 lmdeploy 对 auto-gptq 做的唯一「实质性贡献」。

第四步，量化配置里硬编码了两条约束：

- [lmdeploy/lite/apis/gptq.py:L71-L76](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/lite/apis/gptq.py#L71-L76) — `BaseQuantizeConfig(bits=w_bits, group_size=w_group_size, desc_act=False, sym=True)`，注释明确写 `lmdeploy only supports False` / `lmdeploy only supports True`。这意味着：
  - `sym=True`（对称量化）写死——所以 CLI 里**没有** `--w-sym` 这个开关（对比 auto_awq 有）。
  - `desc_act=False`（不按激活大小重排列）写死——这是 TurboMind/PyTorch 后端加载 GPTQ 权重时的硬约束。

第六、七步，全部交给库：

- [lmdeploy/lite/apis/gptq.py:L86-L99](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/lite/apis/gptq.py#L86-L99) — `from_pretrained` 加载、`model.quantize(examples, batch_size)` 跑量化、`save_quantized` 写盘。这一段在 lmdeploy 视角是「黑盒」，全部行为来自 auto-gptq。

#### 4.1.4 代码实践

> **实践目标**：确认 GPTQ 是「外包」路线，并定位它对 auto-gptq 库的硬依赖。

**操作步骤**：

1. 在未安装 `auto-gptq` 的环境里，运行：
   ```bash
   python -c "from lmdeploy.lite.apis.gptq import auto_gptq; auto_gptq('不存在的路径')"
   ```
2. 观察报错信息是否为 `To use auto_gptq, please install auto-gptq by pip install auto-gptq`。
3. 打开 [lmdeploy/lite/apis/gptq.py:L51-L60](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/lite/apis/gptq.py#L51-L60)，确认 lmdeploy 只往 `SUPPORTED_MODELS` 追加了 `internlm2` / `internlm3` 两个名字。

**预期现象**：第 1 步必然在第 41–45 行的 try/except 处抛出 `ImportError`，证明 GPTQ 的算法本体不在 lmdeploy 仓库内。

> 说明：本实践未真正运行量化（需要 GPU + 模型权重 + auto-gptq），属于**源码阅读型实践**。若你有完整环境，可进一步用 `lmdeploy lite auto_gptq <模型路径> --work_dir ./gptq_out` 实跑，观察 `work_dir` 下生成的是标准 HF 格式 + GPTQ `quantization_config` 的权重。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `auto_gptq` 的 CLI 没有 `--w-sym` 和 `--search-scale` 这两个 auto_awq 有的开关？

**参考答案**：`--w-sym` 缺失是因为 GPTQ 在 lmdeploy 里被硬编码为 `sym=True`（gptq.py L75，注释 `lmdeploy only supports True`），对称/非对称不可选，故无需暴露参数。`--search-scale` 缺失是因为它属于 AWQ 的逐层比例网格搜索逻辑（`awq_layers`），GPTQ 走的是完全不同的海森误差补偿算法，没有「搜索 ratio」这一步。

**练习 2**：GPTQ 与 AWQ 同为 weight-only 4bit 量化，产出物会在哪里被区分？

**参考答案**：在写盘时塞进 `config.json` 的 `quantization_config` 字段。auto_gptq 由 auto-gptq 库写入 `quant_method='gptq'`，而 AWQ 写入 `quant_method='awq'`。推理引擎（TurboMind/PyTorch）加载时正是读这个字段来决定走哪条反量化路径。

---

### 4.2 SmoothQuant 入口：复用校准的 W8A8 量化

#### 4.2.1 概念说明

SmoothQuant 解决的是 **W8A8**（权重 8bit + 激活 8bit）场景下的「激活难量化」问题。

weight-only 量化（AWQ/GPTQ）之所以能压到 4bit 还不掉精度，是因为权重是**静态**的——分布固定、可以离线精挑细选缩放因子。但激活是**动态**的，每个 token 都不一样，而且大模型的激活里有少数「离群点通道」幅值远大于其他通道（可能大几十倍）。如果直接对激活做逐 tensor 的 INT8 量化，这些离群点会把整个 scale 撑大，导致绝大多数正常通道被压成几个低位值，精度崩塌。

SmoothQuant 的解法和 AWQ 的平滑是**同一个思想**：既然离群点难压，就别硬压激活——把离群点「搬」到权重上去（权重好压）。具体地，对相邻的两个模块，引入逐通道缩放因子 \(s\)：

\[
y = W x = \bigl(W \cdot \mathrm{diag}(s)\bigr)\bigl(\mathrm{diag}(s)^{-1}x\bigr) = W' x'
\]

选 \(s_j = \dfrac{\max(|x_j|)^{\alpha}}{\max(|W_{:,j}|)^{1-\alpha}}\)（默认 \(\alpha=0.5\)）：让缩放后的激活 \(x' = x/s\) 幅值更均匀（好量化），代价是权重 \(W'=W\cdot\mathrm{diag}(s)\) 变得稍不均匀——但权重是静态的、能精确量化，这笔交易划算。当 \(\alpha=0.5\) 时两边各担一半难度，正是 `smooth_layers`（[awq.py:L349](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/lite/quantization/awq.py#L349)）的默认行为，SmoothQuant 与 AWQ 在这一步用的是同一段代码。

平滑之后，激活和权重都足够「平滑」，于是两边都可以压成 INT8/FP8，矩阵乘走 INT8 GEMM，吞吐显著高于 FP16。这就是 SmoothQuant 与 AWQ 的本质区别：**AWQ 平滑完只压权重（4bit），SmoothQuant 平滑完权重和激活都压（8bit）**。

#### 4.2.2 核心流程

`smooth_quant` 是 lmdeploy 原生实现，复用 u7-l1 的校准基础设施：

```
1. 解析 quant_dtype（int8 / fp8 → torch.int8 / torch.float8_e4m3fn），断言其位数 == w_bits（默认 8）
2. 调 calibrate()：加载模型 + 收集激活统计量 → work_dir/inputs_stats.pth（arch, vl_model, model, tokenizer, work_dir）
3. 读 inputs_stats.pth 的 absmax 作为激活缩放尺度 act_scales
4. 查 LAYER_TYPE_MAP / NORM_TYPE_MAP 拿到当前模型的 decoder 层类型与 norm 类型
5. 用 NORM_FCS_MAP / FC_FCS_MAP（与 AWQ 共用的层结构表）调 smooth_layers 做平滑
6. 遍历所有 Linear → 替换为 QLinear（W8A8）；遍历所有 RMSNorm → 替换为 QRMSNorm
   跳过 lora / 模型 rebuilder 声明的 skipped_modules
7. 写 quantization_config(quant_method='smooth_quant', quant_dtype=...) 进 config.json，save_pretrained
```

注意第 2、3、5 步：SmoothQuant 与 AWQ **共用同一套校准产物与平滑代码**，差异只在第 6 步——AWQ 产出 `WeightOnlyQLinear`（4bit 权重），SmoothQuant 产出 `QLinear` + `QRMSNorm`（8bit 权重 + 8bit 激活）。

#### 4.2.3 源码精读

入口签名，注意独有的 `quant_dtype` 与默认 `w_bits=8`：

- [lmdeploy/lite/apis/smooth_quant.py:L18-L31](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/lite/apis/smooth_quant.py#L18-L31) — `smooth_quant(model, ...)`。关键字段：
  - `w_bits=8`（与 AWQ/GPTQ 的 4bit 区分开）。
  - `quant_dtype` 取值 `'int8'` / `'fp8'` / `'float8_e4m3fn'` / `'float8_e5m2'`，这是 SmoothQuant 独有参数（AWQ/GPTQ 都没有）。

第一步，把字符串 dtype 解析成 `torch.dtype` 并校验位数：

- [lmdeploy/lite/apis/smooth_quant.py:L33-L42](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/lite/apis/smooth_quant.py#L33-L42) — `'fp8'` 被归一化为 `'float8_e4m3fn'`；用 `torch.finfo` / `torch.iinfo` 取位数，`assert q_dtype_info.bits == w_bits` 防止「要 8bit 却传了 4bit」之类的错配。

第二、三步，复用校准：

- [lmdeploy/lite/apis/smooth_quant.py:L49-L65](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/lite/apis/smooth_quant.py#L49-L65) — 调 `calibrate(...)`（注意 `w_group_size=-1`，因为 W8A8 是逐通道量化、不分组），再从 `inputs_stats.pth` 取 `absmax` 作为 `act_scales`。这正是 u7-l1 讲过的「校准产物是量化输入」。

第四、五步，查层结构表 + 平滑（与 AWQ 同源）：

- [lmdeploy/lite/apis/smooth_quant.py:L67-L89](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/lite/apis/smooth_quant.py#L67-L89) — `LAYER_TYPE_MAP` / `NORM_TYPE_MAP` 把「模型最外层类名」映射到 decoder 层类型与 norm 类型；`FC_FCS_MAP` / `NORM_FCS_MAP`（来自 `awq.py`）描述每层的「前驱 fc → 后继 fc」「norm → 下游 fc」拓扑。然后 `search_scale=False` 时走 `smooth_layers`（absmax + 固定 α=0.5），`True` 时走 `awq_layers`（absmean + 网格搜索）。这段与 AWQ 完全一致—— SmoothQuant 直接 `from lmdeploy.lite.quantization.awq import ... smooth_layers`。

第六步，**SmoothQuant 真正不同于 AWQ 的地方**——把 Linear/RMSNorm 换成 W8A8 算子：

- [lmdeploy/lite/apis/smooth_quant.py:L91-L118](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/lite/apis/smooth_quant.py#L91-L118) — 遍历 `((fcs, QLinear), (rmsnorms, QRMSNorm))` 两组：用 `skipped_module(name, patterns)` 判断是否跳过（lora 永远跳过，模型 `rebuilder.skipped_modules()` 声明的也跳过），否则 `module.to(device)` 上卡 → `q_cls.from_float(module, quant_dtype=quant_dtype)` 量化替换 → 下卡 + `torch.cuda.empty_cache()`。`from_float` 是逐通道量化：`QLinear.from_float` 调 `per_channel_quant` 把权重压成 int8 并算 scale（[q_modules.py:L104-L125](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/models/q_modules.py#L104-L125)），`QRMSNorm.from_float` 则在 forward 里把 RMSNorm 与「逐 token 动态量化激活」融合成一个 kernel（[q_modules.py:L61-L71](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/models/q_modules.py#L61-L71)）。注意：激活是**推理时动态量化**的（在 `QRMSNorm` 里），校准只负责给平滑提供 `absmax`。

第七步，写 `quantization_config`：

- [lmdeploy/lite/apis/smooth_quant.py:L121-L126](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/lite/apis/smooth_quant.py#L121-L126) — `quant_method='smooth_quant'`、`quant_dtype='int8'`（或 `float8_e4m3fn`），若跳过了模块则追加 `modules_to_not_convert`。这是推理引擎识别 W8A8 的依据。

#### 4.2.4 代码实践

> **实践目标**：在源码层面追踪 SmoothQuant 与 AWQ「分道扬镳」的位置。

**操作步骤**：

1. 打开 [smooth_quant.py:L91-L118](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/lite/apis/smooth_quant.py#L91-L118)，记录替换用的类名 `QLinear` / `QRMSNorm`。
2. 对比 [auto_awq.py:L130](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/lite/apis/auto_awq.py#L130)（`quant_weights(model, fcs, w_bits, w_sym, arch, w_group_size, device)`），AWQ 用的是 `WeightOnlyQLinear`（4bit）。
3. 打开 [q_modules.py:L74-L125](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/models/q_modules.py#L74-L125)，找到 `QLinear.from_float` 里 `per_channel_quant(mod.weight.detach(), quant_dtype)`，确认权重被压成 `quant_dtype`（int8/fp8）并保留逐输出通道的 `scale`。

**预期结果**：你能用一句话概括二者差异——「校准 + 平滑」是公共前缀，分叉点在替换算子：AWQ 换成 4bit 的 `WeightOnlyQLinear`，SmoothQuant 换成 8bit 的 `QLinear`+`QRMSNorm`。

> 说明：本实践为源码阅读型，未运行量化。`w_bits` 与 `quant_dtype` 的对应关系（`assert q_dtype_info.bits == w_bits`）可在阅读 L33-L42 时确认。若本地有环境，可用 `lmdeploy lite smooth_quant <模型路径> --quant_dtype int8` 实跑，观察输出目录 `config.json` 里出现 `quant_method: smooth_quant`。

#### 4.2.5 小练习与答案

**练习 1**：SmoothQuant 的激活量化是「离线」还是「在线」完成的？依据是哪段代码？

**参考答案**：在线（推理时动态）。校准阶段只收集激活 `absmax` 用于**平滑**（把离群点搬进权重），并不产生激活的量化参数。真正的激活量化发生在推理时 `QRMSNorm.forward` 里的 `rms_norm_dynamic_quant`（[q_modules.py:L67-L71](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/models/q_modules.py#L67-L71)），函数名里的 `dynamic_quant` 即「逐 token 动态量化」。

**练习 2**：为什么 `smooth_quant` 调 `calibrate` 时传 `w_group_size=-1`，而 `auto_awq` 传 `w_group_size=128`？

**参考答案**：`w_group_size` 控制**权重**量化的分组粒度。AWQ 是 4bit 分组量化（每 128 个权重共享一组 scale/zeros），所以传 128。SmoothQuant 是 8bit **逐通道**量化（每个输出通道一个 scale，见 `QLinear` 的 `scale` 形状 `(out_features, 1)`），不分组，故传 `-1`（`-1` 在 `awq.py` 各函数里表示「不分组」）。

---

### 4.3 模型特定 GPTQ 实现：lite/modeling

#### 4.3.1 概念说明

回到 4.1 留的问题：GPTQ 是逐列量化 + 海森误差补偿，**量化顺序很重要**。但 auto-gptq 是个通用库，它不认识 InternLM 的内部结构——不知道 decoder 层叫什么、Linear 模块藏在哪个属性路径下、哪些该一起量化。

于是 lmdeploy 在 `lite/modeling/` 下为每个需要 GPTQ 支持的模型族写一个**描述类**，继承 auto-gptq 的 `BaseGPTQForCausalLM`，用四个类属性把模型结构「翻译」给 auto-gptq：

| 属性 | 含义 |
|---|---|
| `layer_type` | decoder 层的**类名**（如 `'InternLM2DecoderLayer'`），auto-gptq 据此在模型里定位「一层」 |
| `layers_block_name` | decoder 层列表的**属性路径**（如 `'model.layers'`） |
| `outside_layer_modules` | **不在** decoder 层内的模块（embedding、最终 norm），最后单独处理 |
| `inside_layer_modules` | 每个 decoder 层**内部** Linear 模块的分组顺序，是一个「列表的列表」 |

`inside_layer_modules` 是核心：auto-gptq 按**组**遍历，组内模块共享同一批校准输入、一起算海森。把共享输入的模块（如 q/k/v 共享 hidden state 输入；gate/up 共享同一输入）放进同一组，海森统计更准、效率更高。这正是 GPTQ 对「结构信息」的依赖。

#### 4.3.2 核心流程

```
1. auto_gptq 把 'internlm2' 注册进 GPTQ_CAUSAL_LM_MODEL_MAP（值 = InternLM2GPTQForCausalLM）
2. AutoGPTQForCausalLM.from_pretrained 读模型 config，按 model_type 选对应的 *GPTQForCausalLM 子类
3. auto-gptq 用该子类的 layer_type / layers_block_name 定位每个 decoder 层
4. 对每个 decoder 层，按 inside_layer_modules 的分组顺序，逐组跑海森 + 逐列 GPTQ 量化
5. outside_layer_modules（embedding、norm）按 auto-gptq 默认策略处理
6. 量化完的权重写回，save_quantized
```

#### 4.3.3 源码精读

InternLM2 的描述类，整文件仅 14 行：

- [lmdeploy/lite/modeling/internlm2_gptq.py:L5-L14](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/lite/modeling/internlm2_gptq.py#L5-L14) — `InternLM2GPTQForCausalLM`：
  - `layer_type = 'InternLM2DecoderLayer'`（L6）
  - `layers_block_name = 'model.layers'`（L7）
  - `outside_layer_modules = ['model.tok_embeddings', 'model.norm']`（L8）——注意 InternLM2 的 embedding 叫 `tok_embeddings`（沿袭了其早期结构）。
  - `inside_layer_modules`（L9-L14）共四组：
    - `['attention.wqkv']` —— InternLM2 的 QKV 已经**融合**成一个 `wqkv`，所以是单元素组。
    - `['attention.wo']` —— 输出投影单独一组。
    - `['feed_forward.w3', 'feed_forward.w1']` —— gate/up 共享输入，放一组；InternLM2 的 FFN 名为 `feed_forward`、gate/up 名为 `w1`/`w3`（非 HF 标准的 `gate_proj`/`up_proj`）。
    - `['feed_forward.w2']` —— down 投影单独一组。

对比 InternLM3，能看到「同一框架，不同命名」：

- [lmdeploy/lite/modeling/internlm3_gptq.py:L5-L14](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/lite/modeling/internlm3_gptq.py#L5-L14) — `InternLM3GPTQForCausalLM`：
  - `layer_type = 'InternLM3DecoderLayer'`（L6）
  - `outside_layer_modules = ['model.embed_tokens', 'model.norm']`（L8）—— InternLM3 改用了 HF 标准命名 `embed_tokens`。
  - `inside_layer_modules`（L9-L14）四组用的是 **HF 标准命名**：`self_attn.q_proj/k_proj/v_proj`（这里 QKV **未融合**，所以是三元素组）、`self_attn.o_proj`、`mlp.up_proj/gate_proj`、`mlp.down_proj`。

这两个文件的对比恰好说明 `lite/modeling/` 的全部意义：**为每个模型族声明「我的层叫什么、Linear 在哪、谁和谁一起量化」**。结构不同（QKV 是否融合、FFN 命名、embedding 字段名），描述就不同。`modeling/__init__.py`（[此处](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/lite/modeling/__init__.py)）目前为空，因为这两个类是在 `gptq.py:L55-L56` 里被**直接按路径导入**的，无需注册表。

#### 4.3.4 代码实践

> **实践目标**：把两个 `*_gptq.py` 的差异填成一张表，理解「模型特定 GPTQ 需要改写哪些层」。

**操作步骤**：

1. 打开 [internlm2_gptq.py:L5-L14](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/lite/modeling/internlm2_gptq.py#L5-L14) 和 [internlm3_gptq.py:L5-L14](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/lite/modeling/internlm3_gptq.py#L5-L14)。
2. 逐项对比四个属性，填入下表（已给出 InternLM2 列作为示范）：

   | 属性 | InternLM2 | InternLM3 |
   |---|---|---|
   | `layer_type` | `InternLM2DecoderLayer` | `InternLM3DecoderLayer` |
   | `layers_block_name` | `model.layers` | `model.layers` |
   | `outside_layer_modules` | `tok_embeddings`, `norm` | `embed_tokens`, `norm` |
   | QKV 是否融合 | 是（`attention.wqkv`） | 否（`q/k/v_proj` 三元素组） |
   | FFN 命名 | `feed_forward.w1/w2/w3` | `mlp.gate/up/down_proj` |

3. 回答：如果要给一个新模型（比如 Qwen3）写 GPTQ 描述类，你需要从它的 HF 模型代码里查清哪几件事？

**预期结果**：你需要查清——(a) decoder 层的类名；(b) 层列表的属性路径；(c) embedding 与最终 norm 的属性名；(d) attention 的 QKV 是否融合、各投影的属性名；(e) FFN 各投影的属性名。把这五件事填进四个类属性，就是 `lite/modeling/xxx_gptq.py` 的全部工作。

> 说明：本实践为纯源码阅读型，无需运行。若想验证 `inside_layer_modules` 的属性名是否正确，可在 Python 里 `from transformers import AutoModel; m = AutoModel.from_pretrained(...); print([n for n,_ in m.model.layers[0].named_modules()])` 打印某层的全部子模块名来核对。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `inside_layer_modules` 把 `q_proj/k_proj/v_proj`（或 `w3/w1`）放在**同一个内层列表**里，而不是各占一个列表？

**参考答案**：同一个内层列表的模块会被 auto-gptq 当作「一组」：它们共享同一份输入激活，一起计算海森矩阵、一起按 GPTQ 顺序逐列量化。QKV 三个投影（或 gate/up 两个投影）的输入正是同一个 hidden state，放一组能让海森统计覆盖到这批共享输入、也减少重复前向。若拆成独立列表，每组都要单独跑前向收集输入，既慢又割裂了它们共享输入的事实。

**练习 2**：`lite/modeling/` 下目前只有 `internlm2` 和 `internlm3` 两个文件，说明什么？

**参考答案**：说明 auto-gptq 库**自身已经内置**了大部分主流模型（Llama、Qwen、Mistral 等）的描述类，lmdeploy 只需为 auto-gptq 原生不支持的模型（如 InternLM 系列，因 QKV 融合、非标准 FFN 命名等历史原因）补写描述类并注册。这也呼应了 4.1 的结论：lmdeploy 对 GPTQ 的贡献仅是「让 auto-gptq 认识 InternLM」。

---

## 5. 综合实践：三个 CLI 子命令的参数对照表

本讲最重要的「能带走」的产出，是一张 `auto_awq` / `auto_gptq` / `smooth_quant` 三个 CLI 的参数对照表。CLI 参数定义全部在 [lmdeploy/cli/lite.py](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/cli/lite.py)：

- `auto_awq` 参数定义：[cli/lite.py:L17-L42](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/cli/lite.py#L17-L42)
- `auto_gptq` 参数定义：[cli/lite.py:L44-L65](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/cli/lite.py#L44-L65)
- `smooth_quant` 参数定义：[cli/lite.py:L86-L108](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/cli/lite.py#L86-L108)

**任务**：运行下面三条命令，把帮助输出与源码定义对照，填出表格并解释每个「差异」的原因。

```bash
lmdeploy lite auto_awq --help
lmdeploy lite auto_gptq --help
lmdeploy lite smooth_quant --help
```

**对照表（请自行补全「原因」列）**：

| 参数 | auto_awq | auto_gptq | smooth_quant | 差异原因（请你填） |
|---|---|---|---|---|
| `model`（位置参数） | ✅ | ✅ | ✅ | 三者都需要 HF 模型路径 |
| `--w-bits` | ✅ 默认 4 | ✅ 默认 4 | ❌ | ? |
| `--w-sym` | ✅ | ❌ | ❌ | ? |
| `--w-group-size` | ✅ 默认 128 | ✅ 默认 128 | ❌ | ? |
| `--calib-search-scale` | ✅ | ❌ | ✅ | ? |
| `--device` | ✅ | ❌ | ✅ | ? |
| `--download-dir` | ✅ | ❌ | ✅ | ? |
| `--quant-dtype` | ❌ | ❌ | ✅ | ? |

**参考答案（核对用）**：

- `--w-bits`：auto_gptq 默认 4（weight-only）；smooth_quant 没有，因为 W8A8 固定 8bit，位数由 `--quant-dtype` 隐式决定（int8=8bit）。
- `--w-sym`：只有 auto_awq 有。GPTQ 在 lmdeploy 里硬编码 `sym=True`（gptq.py L75）；SmoothQuant 走逐通道 int8/fp8，对称与否由 `QLinear` 内部决定，不暴露。
- `--w-group-size`：只有 weight-only 的两者需要（4bit 分组量化）；SmoothQuant 逐通道不分组。
- `--calib-search-scale`：auto_awq 与 smooth_quant 都有，因为二者都走 `smooth_layers`/`awq_layers`，可选用网格搜索 α；auto_gptq 走完全不同的海森算法，没有这一步。
- `--device`：auto_awq 与 smooth_quant 是 lmdeploy 原生、支持 cuda/npu（`try_import_deeplink`）；auto_gptq 全程在 auto-gptq 库内、默认 `.cuda()`，不暴露设备选择。
- `--download-dir`：auto_awq 与 smooth_quant 在本地找不到模型时会 `get_model` 下载，需要指定目录；auto_gptq 直接用 `model` 路径，不内置下载逻辑。
- `--quant-dtype`：SmoothQuant 独有，因为 W8A8 要区分 int8 还是 fp8（e4m3fn/e5m2）；weight-only 的两者只压 4bit，无需此参数。

完成这张表，你就把本讲的三个最小模块串成了一张可查阅的速查卡。

---

## 6. 本讲小结

- **GPTQ（`auto_gptq`）** 是 weight-only 4bit 量化，但它是「外包」路线——把海森误差补偿算法委托给第三方 `auto-gptq` 库，lmdeploy 只贡献 InternLM 模型注册。它**不**用 lmdeploy 的 `calibrate()`。
- **GPTQ 算法本质**：逐列量化权重，用海森逆 \(H^{-1}=（XX^{\mathsf{T}}）^{-1}\) 把每列的量化误差回灌补偿给剩余列，量化顺序由 `inside_layer_modules` 决定。
- **SmoothQuant（`smooth_quant`）** 是 W8A8 量化（权重+激活都压 8bit），lmdeploy 原生实现，复用 `calibrate()` 收集的 `absmax` 与 AWQ 的 `smooth_layers` 做平滑，然后把 `Linear`/`RMSNorm` 换成 `QLinear`/`QRMSNorm`。
- **SmoothQuant 与 AWQ 的分叉点**在替换算子：AWQ 换成 4bit 的 `WeightOnlyQLinear`，SmoothQuant 换成 8bit 的 `QLinear`+`QRMSNorm`；激活是**推理时动态量化**的（`rms_norm_dynamic_quant`）。
- **`lite/modeling/xxx_gptq.py`** 用四个类属性（`layer_type` / `layers_block_name` / `outside_layer_modules` / `inside_layer_modules`）把模型结构翻译给 auto-gptq；目前仅 InternLM2/3 需要，因为其他主流模型 auto-gptq 已内置。
- **三个 CLI 的参数差异**背后是算法差异：weight-only vs W8A8、外包 vs 自研、是否分组、是否可搜索 α、是否可选设备——理解差异比记参数更重要。

---

## 7. 下一步学习建议

本讲结束后，U7 量化单元的四条路线（校准 / AWQ / GPTQ / SmoothQuant）已全部讲完。建议：

1. **横向收尾**：回头读 [lmdeploy/lite/quantization/weight/quantizer.py](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/lite/quantization/weight/quantizer.py) 与 `activation/observer.py`（u7-l4 主题），把 AWQ/SmoothQuant 共用的底层量化器与观察器补齐，形成「算法层（apis）→ 工具层（quantization）」的完整图景。
2. **纵向衔接推理**：三种量化产出的 `quantization_config`（`quant_method` = awq/gptq/smooth_quant）会在加载时被谁识别？跳到 U3（PyTorch 模型 patch）的 `nn/linear/awq.py`、`nn/linear/w8a8.py`（u5-l2），看推理侧如何按 `quant_method` 选线性层实现，闭合「量化产出 → 推理消费」的环。
3. **实跑验证**：如果本地有 GPU + 一个小模型（如 `Qwen/Qwen2.5-0.5B`），分别跑 `auto_awq` 与 `smooth_quant`，用 `lmdeploy pipeline` 对比量化前后的生成质量与显存，把本讲的纸面结论变成手感。
