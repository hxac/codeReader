# AWQ 量化原理与实现

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 **AWQ（Activation-aware Weight Quantization，激活感知权重量化）** 到底想解决什么问题，以及它为什么「只量化权重、不动激活」。
- 读懂 `auto_awq` 这条入口的完整调用链：从校准产物出发，到逐层平滑（smoothing），再到 4bit 权重打包写盘。
- 看懂 `awq.py` 里两张关键映射表 `NORM_FCS_MAP` / `FC_FCS_MAP` 是如何用「纯数据」描述每个模型族的「哪一层 norm 喂给了哪些 Linear、哪个前驱 Linear 喂给了哪些后继 Linear」。
- 手推一遍平滑缩放 `smooth_ln_fcs` / `smooth_fc_fcs` 的数学等价变换，并能在源码里把每一行公式对应起来。

本讲是 U7（Lite 量化压缩）单元的第二篇，紧承 [u7-l1 校准流程](u7-l1-lite-calibration-flow.md)：上一讲我们得到的是「不改权重、只收集激活统计」的 `inputs_stats.pth`，本讲就把这份统计真正用来改造权重。

## 2. 前置知识

### 2.1 线性层与权重量化速成

一个线性层做的事是：

\[ y = W x + b \]

其中 \(W \in \mathbb{R}^{o \times i}\) 是权重（`out_features` 行、`in_features` 列），\(x\) 是输入激活，\(b\) 是偏置。FP16 下 \(W\) 的每个元素占 16 bit。

**权重量化（weight-only quantization）** 的思路是：把 \(W\) 压成 4bit（每个元素占 4 bit，省 4 倍显存），推理时把 4bit 权重「反量化」回 FP16 再和 FP16 激活做矩阵乘。这样精度损失主要来自权重的舍入，而激活全程保持高精度，因此叫「weight-only」。

朴素地把整个 \(W\) 用同一个 scale 缩放成 4bit，叫做 per-tensor 量化，精度损失大。AWQ 用的是 **per-group（分组）量化**：把 \(W\) 沿输入维按 `group_size`（通常 128）切组，每组各算一个 scale 和 zero point，从而显著降低误差。

### 2.2 为什么需要「激活感知」

权重的不同输入通道（列）重要性天差地别：有的通道对应的激活幅值很大（「显著通道」，salient channels），把它们量化错会显著拉低输出质量；有的通道激活几乎为零，量化错也无所谓。

朴素量化对所有通道一视同仁。AWQ 的洞察是：**给显著通道一个更大的动态范围**，让它量化后保留更多有效比特。但直接改激活会破坏数值范围，于是 AWQ 把这个缩放「吸收」进上一层的 LayerNorm，做到数学上严格等价。这就是本讲的核心。

### 2.3 LayerNorm / RMSNorm 的可吸收性

RMSNorm 的形式是：

\[ \text{RMSNorm}(h) = \gamma \odot \frac{h}{\text{RMS}(h)}, \quad \text{RMS}(h) = \sqrt{\frac{1}{d}\sum_j h_j^2} \]

其中 \(\gamma\) 是可学习的逐通道缩放向量。关键性质：RMSNorm 输出再乘以一个逐通道的对角矩阵 \(D = \text{diag}(s)\)，等价于把 \(D\) 吸收进 \(\gamma\)：

\[ D \cdot (\gamma \odot \widehat{h}) = (D \gamma) \odot \widehat{h} \]

也就是说，「在 norm 输出处把激活乘以 \(s\)」与「把 norm 的 \(\gamma\) 乘以 \(s\)」对下游完全等价。同理「把激活除以 \(s\)」等价于「\(\gamma\) 除以 \(s\)」。这就是 AWQ 能无损迁移 scale 的数学基础。本讲后续的 `smooth_ln_fcs` 就是利用这条性质。

### 2.4 承接 u7-l1：校准产物长什么样

上一讲的 `calibrate()` 把模型逐层上卡跑前向，用 `ActivationObserver` 收集每个 Linear 输入激活的统计量，最后导出 `work_dir/inputs_stats.pth`。这份文件是一个字典，本讲要用到它的两个键：

- `absmax`：每个 Linear 输入的逐通道**绝对值最大**幅值（供 `search_scale=False` 默认路径使用）。
- `absmean` + `ratios`：每个 Linear 输入的逐通道**绝对值平均**幅值，以及网格搜索得到的最优 \(\alpha\) 比例（供 `search_scale=True` 路径使用）。

本讲要回答的问题就是：拿到这些统计量后，如何把它变成一份 4bit 的 AWQ 权重。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲角色 |
| --- | --- | --- |
| `lmdeploy/lite/apis/auto_awq.py` | AWQ 量化的入口函数 `auto_awq`，串起「校准→平滑→打包→写盘」全流程 | 主线，最小模块 1 |
| `lmdeploy/lite/quantization/awq.py` | AWQ 的算法核心：两张映射表、`smooth_*` 平滑函数、`quant_weights` 打包 | 算法心脏，最小模块 2 与 3 |
| `lmdeploy/lite/apis/calibrate.py` | 上一讲的校准入口；本讲只借用其中的 `LAYER_TYPE_MAP` 与校准产物约定 | 衔接上游 |
| `lmdeploy/cli/lite.py` | CLI 子命令 `auto_awq` 的参数定义与派发 | 命令行入口 |
| `lmdeploy/lite/quantization/weight/quantizer.py` | `WeightQuantizer`，per-group 量化参数计算 | 打包阶段被调用 |
| `lmdeploy/lite/utils/cal_qparams.py` | `QParams` 及各种 per-group/per-channel scale 计算函数 | 量化数学底座 |

## 4. 核心概念与源码讲解

### 4.1 auto_awq 入口与整体调用链

#### 4.1.1 概念说明

`auto_awq` 是用户接触 AWQ 的唯一函数入口（CLI 命令 `lmdeploy lite auto_awq` 最终也是调用它）。它是一个「编排函数（orchestrator）」：自己不做任何量化数学，只负责把几个阶段按顺序拼起来：

1. **校准（可选）**：跑前向收集激活统计，产出 `inputs_stats.pth`；若 `calib_samples=0` 则跳过校准、走 data-free 路径。
2. **收集目标层**：用 `LAYER_TYPE_MAP` 把「模型最外层类名」翻译成「decoder 层类名」，再用 `collect_target_modules` 找出所有 decoder 层及其内部的所有 `nn.Linear`。
3. **平滑（smoothing）**：核心算法步骤，用激活统计驱动逐层缩放，把显著通道的动态范围撑大。分两条子路径（`search_scale` 开关）。
4. **打包**：把每个 Linear 的权重伪量化成 4bit，替换成 `WeightOnlyQLinear` 模块。
5. **写盘**：把 AWQ 配置写进 `model.config.quantization_config`，保存为新的 HF 权重目录。

理解这条链路，就理解了「AWQ 量化」这件整事在 lmdeploy 里是怎么落地的。

#### 4.1.2 核心流程

```text
auto_awq(model, w_bits=4, w_group_size=128, search_scale=False, ...)
   │
   ├── calib_samples == 0 ?
   │     ├── 是 → load_model_and_tokenizer（不校准，data-free）
   │     └── 否 → calibrate(...)（产 inputs_stats.pth，见 u7-l1）
   │
   ├── layer_type = LAYER_TYPE_MAP[模型最外层类名]
   ├── layers     = collect_target_modules(model, layer_type)   # 所有 decoder 层
   ├── fcs        = 收集所有层内的 nn.Linear                    # 待量化的全连接
   │
   ├── if calib_samples != 0:
   │     input_stats = torch.load(inputs_stats.pth)
   │     ├── search_scale=True  → awq_layers(...absmean, ratios...)   # 真·AWQ
   │     └── search_scale=False → smooth_layers(...absmax...)         # 固定 0.5 比例
   │
   ├── skipped = quant_weights(model, fcs, w_bits, ...)          # 4bit 打包
   ├── model.config['quantization_config'] = {awq, gemm, 4, 128, zero_point}
   └── model.save_pretrained(work_dir)                           # 写盘
```

注意第 3 步的两个分支：默认 `search_scale=False` 走的是 `smooth_layers`（固定 \(\alpha=0.5\)，用 absmax），它其实是 SmoothQuant 风格的平滑；只有显式打开 `search_scale=True` 才会走 `awq_layers`，用 absmean + 逐层网格搜索的最优比例。这两条路径**共用同一套 `smooth_ln_fcs` / `smooth_fc_fcs` 底层函数**，区别仅在「用哪个统计量」与「\(\alpha\) 取多少」。这一点是阅读源码的关键认知，否则容易困惑「AWQ 为什么会调用 smooth」。

#### 4.1.3 源码精读

先看入口签名与文档串，它定义了全部可调参数：

[lmdeploy/lite/apis/auto_awq.py:41-81](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/lite/apis/auto_awq.py#L41-L81) —— `auto_awq` 的形参表与 docstring。重点关注 `w_bits=4`、`w_group_size=128`、`w_sym=False`、`search_scale=False` 四个默认值，它们决定了产出的 AWQ 模型规格。

校准分支：当 `calib_samples != 0` 时调用 `calibrate`（上一讲的主角），否则走 data-free：

[lmdeploy/lite/apis/auto_awq.py:89-106](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/lite/apis/auto_awq.py#L89-L106) —— 校准或直接加载模型的二选一分支。`calib_samples=0` 表示「数据无关（data-free）量化」，此时不收集统计、直接跳到打包阶段。

收集目标层与全连接：

[lmdeploy/lite/apis/auto_awq.py:108-116](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/lite/apis/auto_awq.py#L108-L116) —— `LAYER_TYPE_MAP` 把模型类名翻成 decoder 层类名；`collect_target_modules` 两次调用，第一次定位所有 decoder 层，第二次在每层内找 `nn.Linear`，汇总成 `fcs` 字典（键是全限定名，值是模块对象）。`rebuilder = MODELS.get(arch)` 是可选的模型特定权重改造钩子。

平滑阶段的双分支（本讲算法核心入口）：

[lmdeploy/lite/apis/auto_awq.py:118-128](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/lite/apis/auto_awq.py#L118-L128) —— 加载校准统计后，按 `search_scale` 选择 `awq_layers`（absmean + ratios）或 `smooth_layers`（absmax）。`FC_FCS_MAP[layer_type]` 与 `NORM_FCS_MAP[layer_type]` 两张表负责告诉算法「这个模型族的层结构」（见 4.2）。

打包与写盘：

[lmdeploy/lite/apis/auto_awq.py:130-144](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/lite/apis/auto_awq.py#L130-L144) —— `quant_weights` 把每个 Linear 的权重伪量化并替换成 `WeightOnlyQLinear`，返回被跳过（不量化）的模块名；随后把 `quant_method='awq'`、`version='gemm'`、`bits`、`group_size`、`zero_point` 写进 `quantization_config`，这正是后续 TurboMind / PyTorch 引擎加载 AWQ 模型时要识别的配置块（见 u6-l3 的 `model_format` 解析）。`vl_model` 分支会额外拷贝视觉预处理文件。

最后看 CLI 这一侧如何把命令行参数喂进 `auto_awq`：

[lmdeploy/cli/lite.py:111-115](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/cli/lite.py#L111-L115) —— `SubCliLite.auto_awq` 延迟导入 `auto_awq` 函数，用 `convert_args` 把 argparse 命名空间转成关键字参数后转发。参数定义在同模块的 `add_parser_auto_awq`（[lite.py:17-42](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/cli/lite.py#L17-L42)），其中 `--w-bits` 默认 4、`--w-group-size` 默认 128、`--w-sym` 默认关闭。

#### 4.1.4 代码实践

**实践目标**：跑通 `auto_awq` 对一个小模型做 4bit 量化（或读流程理解），并验证产出的 `config.json` 里写入了 AWQ 配置。

**操作步骤**：

1. 阅读上面的调用链源码，确认默认参数下走的是 `smooth_layers`（absmax）路径。
2. 在有 GPU 的环境（需安装含 lite 的 lmdeploy 与 GPU 版 torch），选一个小模型（如 `Qwen/Qwen2.5-0.5B-Instruct`），执行：

```bash
lmdeploy lite auto_awq Qwen/Qwen2.5-0.5B-Instruct \
    --work-dir ./work_dir/qwen0.5b-awq \
    --calib-samples 32 --calib-seqlen 512 --w-bits 4 --w-group-size 128
```

3. 完成后查看 `./work_dir/qwen0.5b-awq/config.json`，找到 `quantization_config` 字段。

**需要观察的现象 / 预期结果**：

- 控制台会逐层打印 `xxx smooth weight done. max gpu memory: ... GB` 与 `xxx weight packed.`。
- 产出的 `config.json` 中 `quantization_config` 应形如：

```json
"quantization_config": {
  "quant_method": "awq",
  "version": "gemm",
  "bits": 4,
  "group_size": 128,
  "zero_point": true
}
```

- 权重文件从 FP16 的 `*.safetensors` 变成体积约为原来 1/3 ~ 1/4 的 AWQ 权重（4bit 打包进 int32）。

> 若本机无 GPU 或不想下载模型，可改为「源码阅读型实践」：对照 [auto_awq.py:89-144](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/lite/apis/auto_awq.py#L89-L144)，把每一步的输入产物、输出产物列成表格（如「步骤 / 输入 / 输出 / 关键函数」四列）。**待本地验证**：上述实际命令的显存占用与耗时取决于具体显卡。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `--calib-samples 0` 传给 `auto_awq`，整个流程会有哪一步被跳过？产出还是 AWQ 模型吗？

> **答案**：会跳过 `calibrate()` 与其后的 `smooth_layers`/`awq_layers` 平滑阶段（见 [auto_awq.py:89-94](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/lite/apis/auto_awq.py#L89-L94) 与 [118 行的 `if calib_samples != 0`](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/lite/apis/auto_awq.py#L118)），直接进入 `quant_weights` 打包。产出的仍然是 AWQ 格式模型（`quantization_config` 照写），但由于没有激活感知的平滑，精度会比校准后的差——这就是「data-free 量化」。

**练习 2**：`quantization_config` 里的 `version='gemm'` 字段是给谁看的？

> **答案**：给推理后端（TurboMind / PyTorch）的权重加载器看的，用来选择 AWQ 的 GEMM 反量化 kernel（区别于 `gemv` 等其他实现）。它本身不参与量化算法，只是产出模型的「自描述标签」，被 u6-l3 的 `model_format` 解析读取。

---

### 4.2 AWQ 逐层结构映射：NORM_FCS_MAP 与 FC_FCS_MAP

#### 4.2.1 概念说明

平滑算法（4.3 节）需要回答两个结构性问题：

1. **每个 LayerNorm 的输出喂给了哪些 Linear？** —— 因为缩放要吸收进 norm，norm 的 scale 必须和它下游所有 Linear 的权重同步调整，否则数值就不等价了。
2. **哪个前驱 Linear 的输出直接喂给哪个后继 Linear（中间无 norm）？** —— 比如 `v_proj` 的输出经 attention 后进 `o_proj`，`up_proj` 的输出经激活后进 `down_proj`，这种「Linear→Linear」直连也能迁移 scale。

不同模型族（Llama / Qwen / InternLM2 / Phi3 / GLM …）的层结构千差万别：有的把 Q/K/V 融合成 `wqkv`（InternLM2），有的用 `W_pack`（旧 Qwen），有的把 gate/up 融合成 `gate_up_proj`（Phi3）。AWQ 不写 if-else 区分它们，而是用**两张纯数据的查表**来描述结构：

- `NORM_FCS_MAP`：键是 decoder 层类名，值是 `{norm 子模块名: [它喂给的 fc 子模块名列表]}`。
- `FC_FCS_MAP`：键是 decoder 层类名，值是 `{前驱 fc 子模块名: [后继 fc 子模块名列表]}`。

这样算法主体只认「norm→fcs」「pre_fc→post_fcs」两种抽象关系，与具体模型解耦；新增模型只要在表里加一行即可，无需改算法代码。这是「数据驱动配置」的经典用法。

#### 4.2.2 核心流程

以 `LlamaDecoderLayer`（Llama / Qwen2 / Qwen3 / Mistral 等共用）为例，标准 Transformer decoder 层的结构是：

```text
LlamaDecoderLayer
 ├── input_layernorm ──────────────────► self_attn.{q,k,v}_proj
 ├── post_attention_layernorm ─────────► mlp.{gate,up}_proj
 ├── self_attn:  v_proj ──(attention)──► o_proj
 └── mlp:        up_proj ──(silu)──────► down_proj
```

于是对应到两张表：

```text
NORM_FCS_MAP['LlamaDecoderLayer'] = {
  'input_layernorm':          ['self_attn.k_proj','self_attn.q_proj','self_attn.v_proj'],
  'post_attention_layernorm': ['mlp.gate_proj','mlp.up_proj'],
}
FC_FCS_MAP['LlamaDecoderLayer'] = {
  'self_attn.v_proj': ['self_attn.o_proj'],   # 只对 V 做缩放（Q/K 不进 o_proj 的加权求和）
  'mlp.up_proj':      ['mlp.down_proj'],
}
```

注意 `FC_FCS_MAP` 里 attention 那条用的是 `v_proj` 而非 `q_proj`/`k_proj`：因为 attention 输出是 \(\sum_v \text{score}(q,k)\cdot v\)，只有 V 的通道会线性地传到 `o_proj`，Q/K 经过 softmax 后是非线性的，无法无损迁移 scale。

对于融合层（如 InternLM2 的 `wqkv`、Phi3 的 `qkv_proj`/`gate_up_proj`、旧 Qwen 的 `c_attn`），表里直接写融合后的名字，再由 4.3 节的 `smooth_fc_fcs` 用「形状整除判定」把缩放只施加到对应的子段（比如融合 QKV 里只缩放 V 那段）。

#### 4.2.3 源码精读

`NORM_FCS_MAP` 的完整定义：

[lmdeploy/lite/quantization/awq.py:6-68](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/lite/quantization/awq.py#L6-L68) —— 每个 key 是一种 decoder 层类名，value 是「norm → 其下游 fc 列表」的映射。可对比 Llama 与 InternLM2：后者 norm 叫 `attention_norm`/`ffn_norm`、attention 用融合的 `wqkv`、mlp 用 `feed_forward.w1/w3`，命名不同但拓扑一致。

`FC_FCS_MAP` 的完整定义：

[lmdeploy/lite/quantization/awq.py:70-126](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/lite/quantization/awq.py#L70-L126) —— 每个 key 是一种 decoder 层类名，value 是「前驱 fc → 后继 fc 列表」。注意 `MixtralDecoderLayer` 用了占位符 `block_sparse_moe.experts.{i}.w3`，运行前由 `update_moe_mapping` 展开成每个专家的实际路径（见下）。

MoE 模型的占位符展开：

[lmdeploy/lite/apis/calibrate.py:201-230](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/lite/apis/calibrate.py#L201-L230) —— `update_moe_mapping` 先数出专家数量，再把 `block_sparse_moe.experts.{i}.w3` 这种带 `{i}` 的模板展开成 `[experts.0.w3, experts.1.w3, ...]`，就地改写 `FC_FCS_MAP` / `NORM_FCS_MAP`，使后续平滑能遍历到每个专家。这是「模板 + 运行期展开」处理 MoE 的简洁手法。

`check_awq_supported` 是这两张表的「支持性校验」：

[lmdeploy/lite/quantization/awq.py:279-303](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/lite/quantization/awq.py#L279-L303) —— 给定 decoder 层类名，若它既不在 `NORM_FCS_MAP` 也不在 `FC_FCS_MAP`，就抛 `NotImplementedError`。即「模型支不支持 AWQ」完全由这两张表的键集合决定，是单一事实来源。

#### 4.2.4 代码实践

**实践目标**：为一个具体模型族解读它的两张映射表，并对照模型实际结构验证。

**操作步骤**：

1. 在 [awq.py:6-126](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/lite/quantization/awq.py#L6-L126) 中找到 `Phi3DecoderLayer`，写出它的 `NORM_FCS_MAP` 与 `FC_FCS_MAP`。
2. 回答：Phi3 的 attention 用了融合的 `qkv_proj`，mlp 用了融合的 `gate_up_proj`。这两张表是如何表达「融合」的？
3. （可选）用如下「示例代码」打印一个真实 HF 模型的 decoder 层结构，与表中名字逐一对照：

```python
# 示例代码：打印模型 decoder 层的子模块名，用于和映射表对照
from transformers import AutoModelForCausalLM
import torch
model = AutoModelForCausalLM.from_pretrained("Qwen/Qwen2.5-0.5B", torch_dtype=torch.float16)
# Qwen2 的 decoder 层类名是 Qwen2DecoderLayer
for name, child in model.model.layers[0].named_children():
    print(f"{name}: {type(child).__name__}")
    for sub_name, _ in child.named_children():
        print(f"    {sub_name}")
```

**需要观察的现象 / 预期结果**：

- `Phi3DecoderLayer` 的表为 `'input_layernorm': ['self_attn.qkv_proj']`、`'post_attention_layernorm': ['mlp.gate_up_proj']`，`FC_FCS_MAP` 为 `'self_attn.qkv_proj': ['self_attn.o_proj']`、`'mlp.gate_up_proj': ['mlp.down_proj']`。
- Phi3 通过直接把融合后的名字写进表来表达融合，具体的「只缩放 V 那段」「只缩放 up 那段」由 4.3 的 `smooth_fc_fcs` 按 shape 自动判定。
- 上述示例代码打印出的子模块名应与 `Qwen2DecoderLayer` 在表中的 `self_attn.q_proj` 等名字完全对得上。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `FC_FCS_MAP['LlamaDecoderLayer']` 里 attention 那条的前驱是 `self_attn.v_proj`，而不是 `q_proj` 或 `k_proj`？

> **答案**：attention 的输出是 \(\text{softmax}(QK^T/\sqrt{d}) \cdot V\)，再过 `o_proj`。只有 \(V\) 是线性进入的（乘以 score 后相加），其通道缩放可以无损迁移到 `o_proj`；而 \(Q\)、\(K\) 经过 softmax 这种非线性运算，缩放它们会改变注意力分布，无法等价吸收。因此只能对 V 链做平滑。

**练习 2**：`GLMBlock` 在 `FC_FCS_MAP` 里的值是空字典 `{}`（见 [awq.py:106-109](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/lite/quantization/awq.py#L106-L109)，注释掉了两条）。这意味着 GLM 的 AWQ 量化少了哪一步？还能正常量化吗？

> **答案**：少了 `smooth_fc_fcs` 这步「Linear→Linear」直连平滑（GLM 的 query_key_value/dense、dense_h_to_4h/dense_4h_to_h 没做缩放迁移），只保留了 `NORM_FCS_MAP` 对应的 `smooth_ln_fcs`（norm→fc）平滑。仍能正常量化，因为 `quant_weights` 打包阶段不依赖 `FC_FCS_MAP`，它只影响精度优化的覆盖范围。

---

### 4.3 缩放计算：get_weight_scale 与 smooth_ln_fcs / smooth_fc_fcs

#### 4.3.1 概念说明

这是 AWQ 的数学心脏。目标是给每个输入通道 \(j\) 算一个缩放因子 \(s_j\)，使得：

- 显著通道（激活大、权重也重要）的 \(s_j\) 偏大，量化后保留更多有效比特；
- 同时通过等价变换，保证整个网络的前向输出**完全不变**。

设线性层 \(y = Wx\)，对每个输入通道 \(j\) 引入对角缩放 \(s_j\)，可做恒等变形：

\[ y = W x = \sum_j W_{:,j} x_j = \sum_j \left(\frac{W_{:,j}}{s_j}\right) (s_j x_j) = \widetilde{W} \, \tilde{x} \]

即把权重列除以 \(s_j\)、激活乘以 \(s_j\)，结果不变。但我们要的是反过来——把显著通道的权重**放大**（而不是缩小）以获得更好的量化精度。因此实际约定是：**激活除以 \(s\)，权重乘以 \(s\)**：

\[ \widetilde{W}_{:,j} = s_j \cdot W_{:,j}, \quad \tilde{x}_j = x_j / s_j \]

「激活除以 \(s\)」这一步，若该激活来自 LayerNorm，就可以无损吸收进 norm 的 \(\gamma,\beta\)（见 2.3 节）。于是得到两个核心操作：

- **吸收到 norm**（`smooth_ln_fcs`）：\(\gamma \leftarrow \gamma / s\)、\(\beta \leftarrow \beta / s\)，下游每个 fc 的 \(W \leftarrow W \cdot s\)（按输入通道乘）。
- **迁移给前驱 fc**（`smooth_fc_fcs`）：当前 fc 的 \(W \leftarrow W \cdot s\)（输入通道乘），前驱 fc 的 \(W \leftarrow W / s\)（输出通道除），二者抵消。

那 \(s_j\) 怎么取？AWQ 论文给出兼顾「激活幅值」和「权重幅值」的形式：

\[ s_j = \frac{(|x_j|_{\text{scale}})^{\alpha}}{(|W_{:,j}|_{\text{scale}})^{1-\alpha}}, \quad \alpha \in [0,1] \]

直觉：\(\alpha\) 越大越偏向「按激活幅值缩放」（\(\alpha=1\) 时纯按激活），\(\alpha=0\) 时纯按权重。默认 \(\alpha=0.5\) 取折中。分母用权重幅值是为了防止那些「激活大但权重本来就大」的通道被过度放大。

#### 4.3.2 核心流程

`smooth_ln_fcs` 的执行步骤（吸收 scale 到 LayerNorm）：

```text
1. 找出 ln.weight 中为 0 的通道（zero_positions）——这些通道 norm 输出恒为 0，缩放无意义。
2. w_scales = get_weight_scale(拼接所有下游 fc 的权重)   # 每个输入通道的权重幅值
3. scales = act_scales^α / w_scales^(1-α)              # AWQ 缩放公式
4. 归一化：scales /= sqrt(max(scales) * min(scales))   # 防止某些通道缩放幅度过大
5. zero_positions 处的 scales 置 1（跳过）
6. ln.weight /= scales；ln.bias /= scales               # 激活除以 s，吸收进 norm
7. 对每个 fc：fc.weight *= scales（按输入通道）          # 权重乘以 s，抵消激活的除
```

`smooth_fc_fcs` 与之同构，但把「除以 s」施加在前驱 fc 的**输出通道**上，并需处理两种特殊情况：

- **GQA**（`v_proj` 输出通道 < 激活维度）：`o_proj` 的输入是多头拼接，逐通道缩放对不齐，直接 `return` 跳过（见 [awq.py:233-234](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/lite/quantization/awq.py#L233-L234)）。
- **融合 QKV / gate_up**（`pre_fc` 输出通道 = 激活维度的 2 或 3 倍）：只对最后一段（V 段或 up 段）做缩放。

数学上，整个变换严格保持 \(\text{out} = W_{\text{post}}(W_{\text{pre}} x)\) 不变，因此**平滑不改变模型输出，只改变权重的数值分布**，使其更适合 4bit 量化。

#### 4.3.3 源码精读

先看权重幅值的计算 `get_weight_scale`：

[lmdeploy/lite/quantization/awq.py:144-157](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/lite/quantization/awq.py#L144-L157) —— 把权重按 `group_size` 切组，每组除以其 absmax 做归一化（`abs_weight / abs_weight_amax`），再沿输出维取均值得到逐输入通道的权重重要性 `w_scales`。`amax.min()==0` 时夹到 1e-4 防止除零。这就是公式里的 \(|W_{:,j}|_{\text{scale}}\)。

核心的 `smooth_ln_fcs`：

[lmdeploy/lite/quantization/awq.py:160-210](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/lite/quantization/awq.py#L160-L210) —— 逐行对应公式：[185 行](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/lite/quantization/awq.py#L185)算 `w_scales`；[192 行](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/lite/quantization/awq.py#L192)算 `scales = act_scales.pow(alpha) / w_scales.pow(1-alpha)`，正是 \(s_j\) 公式；[194 行](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/lite/quantization/awq.py#L194)做 `max·min` 几何归一化；[196 行](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/lite/quantization/awq.py#L196)跳过零通道；[198-203 行](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/lite/quantization/awq.py#L198-L203)做吸收——`ln.weight.div_(scales)` + `fc.weight.mul_(scales.view(1,-1))`。注意 `view(1,-1)` 表示沿**输入通道维**（列）广播，即对 \(W\) 的第 \(j\) 列乘以 \(s_j\)。

`smooth_fc_fcs` 处理 Linear→Linear 直连：

[lmdeploy/lite/quantization/awq.py:213-276](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/lite/quantization/awq.py#L213-L276) —— [229-234 行](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/lite/quantization/awq.py#L229-L234)是 GQA 跳过判定（`v_proj` 输出通道 < 激活维度）；[253-256 行](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/lite/quantization/awq.py#L253-L256)是融合 QKV/gate_up 的「只缩放最后一段」判定（`pre_fc.weight[-size_a:]`）；[262 与 268 行](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/lite/quantization/awq.py#L260-L268)分别是前驱 fc 除以 `s`（输出通道，`view(-1,1)`）和后继 fc 乘以 `s`（输入通道，`view(1,-1)`）。

两个外层驱动函数：

[lmdeploy/lite/quantization/awq.py:349-374](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/lite/quantization/awq.py#L349-L374) —— `smooth_layers`：遍历每个 decoder 层，先上卡，按 `NORM_FCS_MAP` 调 `smooth_ln_fcs`（固定 \(\alpha=0.5\)），再按 `FC_FCS_MAP` 调 `smooth_fc_fcs`，再下卡并打印峰值显存。这是默认 `search_scale=False` 路径。

[lmdeploy/lite/quantization/awq.py:406-435](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/lite/quantization/awq.py#L406-L435) —— `awq_layers`：与 `smooth_layers` 结构相同，但 `alpha` 改用校准阶段网格搜索得到的逐层 `a_ratios`（[414 与 422-424 行](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/lite/quantization/awq.py#L414-L424)），即「真 AWQ」。

激活统计量的来源（衔接 u7-l1）：

[lmdeploy/lite/quantization/activation/observer.py:88-124](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/lite/quantization/activation/observer.py#L88-L124) —— `ActivationObserver.observe` 累积每个 Linear 输入的 `absmax_val`（[108 行](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/lite/quantization/activation/observer.py#L108)，供默认路径）与 `absmean_val`（[109-121 行](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/lite/quantization/activation/observer.py#L109-L121)，供 search_scale 路径），它们正是 `smooth_*` 里 `act_scales` 的来源。

#### 4.3.4 代码实践

**实践目标**：用一个最小例子验证 `smooth_ln_fcs` 的等价性——平滑前后，给定相同输入，`norm → fc` 的输出应几乎完全一致。

**操作步骤**：

1. 复制下面的「示例代码」到一个 `.py` 文件运行（需 torch + cuda 或改为 cpu）：

```python
# 示例代码：验证 smooth_ln_fcs 不改变 LayerNorm→Linear 的前向输出
import torch
from lmdeploy.lite.quantization.awq import smooth_ln_fcs

torch.manual_seed(0)
dim = 16
ln = torch.nn.LayerNorm(dim).eval()
fc = torch.nn.Linear(dim, 8, bias=True).eval()

x = torch.randn(2, 5, dim)                 # 模拟 LayerNorm 的输入
y_before = fc(ln(x))                       # 平滑前的输出

# 构造一个假激活 scale（逐通道），alpha=0.5
act_scales = torch.rand(dim) * 5 + 0.1
smooth_ln_fcs(ln, [fc], act_scales, group_size=-1, alpha=0.5)

y_after = fc(ln(x))                        # 平滑后的输出
print('max abs diff:', (y_before - y_after).abs().max().item())
print('ln.weight changed:', not torch.equal(ln.weight, torch.ones_like(ln.weight)))
```

2. 改动 `alpha` 为 0.0 和 1.0，观察等价性是否仍成立（应仍成立，因为等价性与 \(\alpha\) 取值无关，\(\alpha\) 只影响「数值分布好不好量化」）。

**需要观察的现象 / 预期结果**：

- `max abs diff` 应在 1e-6 量级（FP32 下）或 1e-3 量级（FP16 下），证明平滑是数值等价变换。
- `ln.weight changed` 为 `True`，说明 norm 的权重确被修改（吸收了缩放）。
- 改 `alpha` 不改变等价性，只改变 `ln.weight` 的具体数值——这印证「\(\alpha\) 是精度旋钮，不是数值正确性开关」。**待本地验证**：具体 diff 数值取决于 dtype 与硬件。

#### 4.3.5 小练习与答案

**练习 1**：`smooth_ln_fcs` 第 [194 行](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/lite/quantization/awq.py#L194)做 `scales /= sqrt(max(scales)*min(scales))`。为什么需要这步归一化？去掉会怎样？

> **答案**：归一化让 scales 的几何均值约为 1，防止个别通道的缩放幅度过大或过小。若某通道 \(s_j\) 极大，会导致 `fc.weight` 对应列被放得过大，后续 4bit 量化时该通道反而撑爆量程；若 \(s_j\) 极小则该列权重被压到接近 0，丢失信息。归一化把整体动态范围控制在合理区间。去掉它不会破坏「严格等价」，但会损害量化后的精度。

**练习 2**：默认路径（`search_scale=False`）用 `absmax`，AWQ 路径（`search_scale=True`）用 `absmean`。两者作为「激活幅值」的估计，各有何侧重？

> **答案**：`absmax` 反映通道的**峰值**，对离群点（outlier）敏感——这正是 SmoothQuant 思路，专门压制少数极大的异常通道；`absmean` 反映通道的**平均**幅值，对离群点不敏感但更稳健，配合网格搜索的 \(\alpha\) 能更整体地优化量化误差，这是 AWQ 论文的做法。lmdeploy 把两种都实现了，用 `search_scale` 开关切换。

---

### 4.4 权重打包：quant_weights 与 pseudo_quantize_tensor

平滑只是「调权重的数值分布」，真正把它压成 4bit 是 `quant_weights` 这一步。它虽不在本讲三个核心模块里，但完整理解 `auto_awq` 链路必须看它。

#### 4.4.1 概念说明

`quant_weights` 遍历前面收集到的所有 `nn.Linear`，逐个做两件事：

- **跳过判定**：若该层名字命中「跳过模式」（模型特定 `skipped_modules` + 内置的 `lora`），不量化、原样保留，并把模式记入 `modules_to_not_convert`。
- **伪量化 + 打包**：否则用 `pseudo_quantize_tensor` 把权重压成 4bit，连同 scale/zero 一起塞进 `WeightOnlyQLinear` 模块，再用 `setattr` 把原 `nn.Linear` 替换掉。

「伪量化（pseudo quantization）」指：把浮点权重 round 到整数再反量化回浮点，模拟真实 4bit 舍入误差。最终保存时再由 `WeightOnlyQLinear` 把这些值真正打包成 int32 紧凑存储。

#### 4.4.2 核心流程

`pseudo_quantize_tensor` 的 per-group min-max 量化步骤：

```text
1. 把权重 reshape 成 (-1, group_size)，每组独立处理。
2. 每组求 max_val、min_val；scale = (max-min) / (2^bits - 1)，clamp 防 0。
3. zero = round(-min/scale)，把最小值映射到整数 0（非对称量化）。
4. q_w = clamp(round(w/scale) + zero, 0, 2^bits-1)     # 量化到 [0, 15]（4bit）
5. w_dequant = (q_w - zero) * scale                    # 反量化回浮点（伪量化结果）
6. 返回 q_w（整数）、scales、zeros（供打包）。
```

注意 lmdeploy 的 AWQ 用**非对称**量化（`min_int=0`，带 zero point），这与 `auto_awq` 默认 `w_sym=False`、写盘 `zero_point=true` 一致。若传 `--w-sym` 则走对称量化、不存 zero point。

#### 4.4.3 源码精读

`quant_weights` 的主循环：

[lmdeploy/lite/quantization/awq.py:306-346](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/lite/quantization/awq.py#L306-L346) —— [327 行](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/lite/quantization/awq.py#L327)调 `skipped_module` 判定跳过；[334-339 行](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/lite/quantization/awq.py#L334-L339)对不跳过的层构造 `WeightQuantizer`、调 `pseudo_quantize_tensor` 拿到 (q_w, scales, zeros)，再用 `WeightOnlyQLinear.from_linear` 替换原模块；[340 行](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/lite/quantization/awq.py#L340)的 `setattr(parent, child_name, q_linear)` 是真正发生「换模块」的地方。

跳过判定：

[lmdeploy/lite/quantization/awq.py:128-141](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/lite/quantization/awq.py#L128-L141) —— `skipped_module` 用「子串包含」判断模块名是否命中跳过模式，永远把 `'lora'` 加入跳过列表（LoRA 层不参与权重量化）。

伪量化数学：

[lmdeploy/lite/quantization/awq.py:377-403](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/lite/quantization/awq.py#L377-L403) —— [384-385 行](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/lite/quantization/awq.py#L384-L385)求 per-group min/max；[388 行](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/lite/quantization/awq.py#L388)算 scale；[389 行](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/lite/quantization/awq.py#L389)算 zero point；[393 行](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/lite/quantization/awq.py#L393)的 `clamp(round(w/scale)+zeros, 0, 2^bits-1)` 是量化核心。

`WeightOnlyQLinear` 的权重布局（最终存储形态）：

[lmdeploy/lite/quantization/modules/linear.py:50-69](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/lite/quantization/modules/linear.py#L50-L69) —— 4bit 权重被打包进 `int32` buffer：`w_pack_oc = out_features // (32//w_bit)`（4bit 时 8 个权重挤进一个 int32）；scales 形状为 `(in//group_size, out)`；非对称时还有同形状的 `qzeros`。这就是最终落盘、并被 TurboMind/PyTorch 引擎的 AWQ 线性层（见 u5-l2 的 `AwqLinear`）读取的格式。

#### 4.4.4 代码实践

**实践目标**：用 `pseudo_quantize_tensor` 直观感受 per-group 量化的精度，对比不同 `group_size` 的误差。

**操作步骤**：

```python
# 示例代码：观察 per-group 量化误差随 group_size 变化
import torch
from lmdeploy.lite.quantization.awq import pseudo_quantize_tensor

torch.manual_seed(0)
w = torch.randn(64, 512) * 0.1       # 模拟一个线性层权重
for gs in [512, 128, 64]:
    q = pseudo_quantize_tensor(w.clone(), w_bit=4, w_group_size=gs)
    err = (q - w).abs().mean().item()
    print(f'group_size={gs:4d}  平均绝对误差={err:.6e}')
```

**需要观察的现象 / 预期结果**：

- `group_size` 越小，每组越小、scale 越精细，平均误差越小（128 比 512 小，64 比 128 小）。
- 但 group_size 越小，scales/zeros 张量越大（存储与反量化开销上升）。这就是为什么默认取折中的 128。**待本地验证**：具体误差数值取决于随机权重。

#### 4.4.5 小练习与答案

**练习**：`quant_weights` 里 LoRA 层会被自动跳过（`SKIPPED_MODULE = ['lora']`）。为什么 LoRA 层不能做 AWQ 权重量化？

> **答案**：LoRA 是「低秩适配器」，本身参数量小且按 FP16 训练，其低秩矩阵（A、B）的数值分布与稠密权重不同，强行 4bit 量化会破坏低秩近似、精度崩坏；且 LoRA 设计上就是「可插拔、可热切换」的，量化后就不便动态加载/切换了。因此 lmdeploy 把它列入固定跳过名单，只量化 base 权重。

---

## 5. 综合实践

把本讲四个模块串起来，完成一次「手算 + 代码验证」的端到端理解：

**任务**：选取 `Qwen2DecoderLayer`（覆盖最广，Llama/Qwen2/Qwen3/Mistral 共用同一张表），手动追踪它在一层内的完整 AWQ 处理过程。

**步骤**：

1. **结构对照**（模块 4.2）：从 [awq.py 的两张表](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/lite/quantization/awq.py#L6-L126) 写出该层的 `NORM_FCS_MAP` 与 `FC_FCS_MAP`，画出「两个 norm 各自喂哪些 fc、哪两个 fc 之间有直连」的拓扑图。
2. **公式推导**（模块 4.3）：对 `input_layernorm → {q,k,v}_proj` 这组，写出 \(s_j = \text{act\_scales}^{\alpha} / \text{w\_scales}^{1-\alpha}\)，并指出代码里哪几行（[awq.py:185-203](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/lite/quantization/awq.py#L185-L203)）分别对应「算 w_scales、算 scales、归一化、吸收进 ln、乘到 fc」。
3. **特殊路径**（模块 4.3）：该层的 `v_proj → o_proj` 是 GQA（假设 num_kv_heads < num_heads），指出 [smooth_fc_fcs 的 229-234 行](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/lite/quantization/awq.py#L229-L234)会如何处理（直接 return 跳过）。
4. **等价性验证**（模块 4.3）：用 4.3.4 的示例代码，把一个 `LayerNorm → [q_proj, k_proj, v_proj]` 的小组送进 `smooth_ln_fcs`，验证平滑前后输出一致。
5. **端到端**（模块 4.1 + 4.4）：若有 GPU，用 `lmdeploy lite auto_awq` 跑一个 Qwen2 小模型的 4bit 量化，检查产出的 `config.json` 的 `quantization_config` 字段，并用 `lmdeploy pipeline` 加载该量化模型跑一次推理（见 u1-l4），确认它能正常出文。

**预期成果**：一份拓扑图 + 一份公式到代码行的对应表 + 一份等价性验证的 diff 数值 + （可选）一次可运行的 AWQ 模型推理。

## 6. 本讲小结

- **AWQ = 激活感知的 weight-only 量化**：核心不是「怎么压成 4bit」，而是「先把显著通道的权重数值分布调好，再压」，从而在同样 4bit 下精度更高。
- **入口 `auto_awq`** 是编排函数，串起「校准 → 收集层 → 平滑 → 打包 → 写盘」五步；默认 `search_scale=False` 走的是 `smooth_layers`（absmax + 固定 \(\alpha=0.5\)），`True` 才走真正的 `awq_layers`（absmean + 逐层搜索比例）。
- **两张映射表 `NORM_FCS_MAP` / `FC_FCS_MAP`** 用纯数据描述每个模型族的层结构，让算法与具体模型解耦；模型是否支持 AWQ 完全由这两张表的键集合决定（`check_awq_supported`）。
- **平滑的数学本质是恒等变换** \(y = Wx = (W\cdot\text{diag}(s))(\text{diag}(s)^{-1}x)\)：把缩放吸收进 LayerNorm 的 \(\gamma,\beta\)（`smooth_ln_fcs`）或迁移给前驱 Linear（`smooth_fc_fcs`），输出数值不变，只改善权重的可量化性。
- **缩放因子公式** \(s_j = |x_j|^{\alpha}/|W_{:,j}|^{1-\alpha}\) 平衡激活与权重幅值，\(\alpha\) 是精度旋钮；`smooth_fc_fcs` 还要特殊处理 GQA（跳过）与融合 QKV（只缩最后一段）。
- **打包阶段** `pseudo_quantize_tensor` 做 per-group 非对称量化，`WeightOnlyQLinear` 把 4bit 权重紧凑打包进 int32；产出的 `quantization_config` 是后续推理引擎加载 AWQ 模型的自描述标签。

## 7. 下一步学习建议

- **横向对比 GPTQ**：读 [u7-l3 GPTQ 与 SmoothQuant](u7-l3-gptq-smoothquant.md)，对比 AWQ（weight-only，激活感知平滑）与 GPTQ（基于二阶 Hessian 信息的权重量化）在思路上的根本差异。
- **向下看量化底座**：阅读 [lmdeploy/lite/quantization/weight/quantizer.py](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/lite/quantization/weight/quantizer.py) 的 `WeightQuantizer` 与 [cal_qparams.py](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/lite/utils/cal_qparams.py) 的 `cal_qparams_per_group_minmax`，理解 per-tensor / per-channel / per-group 三种粒度与对称/非对称的实现差异（对应 u7-l4）。
- **向上看推理侧如何用 AWQ**：阅读 u5-l2 的 `AwqLinear`（W4A16 实现），看本讲产出的 int32 打包权重 + scales + qzeros 是如何被推理引擎高效反量化做矩阵乘的；以及 u6-l3 的 `model_format='awq'` 如何在 TurboMind 加载时被识别。
- **真·AWQ 搜索**：若对 `search_scale=True` 感兴趣，可顺藤摸瓜读 `CalibrationContextV2`（在 `lmdeploy/lite/quantization/` 下），看它是如何在若干候选 \(\alpha\) 上做网格搜索、并按层把最优 ratio 存进 `inputs_stats.pth['ratios']` 的。
