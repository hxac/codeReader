# 自定义层：mHC、RMSNorm、线性层与词表并行

## 1. 本讲目标

前面三讲我们把 `pangu_v2_moe.py` 的骨架（u3-l1）、稀疏注意力（u3-l2）和 MoE 层（u3-l3）都读完了。但 DecoderLayer 里还剩下一批「配角」：RMSNorm、激活函数、各种 Linear、Embedding/lm_head，以及一个贯穿整个 block 的残差流机制 mHC。它们单看都不复杂，却回答了两个关键问题：

1. **omni-npu 是怎么把 vLLM 的通用层换成 NPU 高性能实现的？**（替换的「姿势」有几种，分别在什么时机生效）
2. **openPangu-2.0 特有的 mHC 多头残差流，在 block 之间是怎么传递的？**

读完本讲你应当能够：

1. 画出 mHC（manifold-constrained hyper connection）三段式 `mhc_pre → mhc_sinkhorn → mhc_post` 的数据流，写出新旧残差流的更新公式，并说出 `NPUmHC` 在两种硬件（Ascend950 与非 950）上分别调用哪族融合算子。
2. 区分 omni-npu 接入自定义层的两条路径：`@register_oot` 隐式全局替换（`NPURMSNorm`、`NPUSiluAndMul`）与模型文件显式 import（`NPUVocabParallelEmbedding`、`NPUParallelLMHead`、`NPULogitsProcessor`），并指出前者的注册触发点在源码中的确切位置。
3. 解释 `NPURMSNorm` 为什么用 `npu_add_rms_norm` 融合核、`NPUSiluAndMul` 的 `quant_symbol` 字典协议如何服务 W8A8 量化。
4. 说出 `FlashCommLinear` 家族相对 vLLM 原生 Linear 的四个 NPU 特有改动：权重转置、FRACTAL_NZ 布局、按层配置的 x/y 通信变换、权重预取。
5. 描述 logits 计算的两种通信路径（TP all-gather 与 DP/local 分片 all_to_all），并会估算二者的通信量。
6. 仿照 `layers/activation.py` 的写法独立新增一个 NPU 自定义激活层，并知道把它注册到哪里才能被模型构建时使用。

## 2. 前置知识

### 2.1 替换一个层，有几种「姿势」？

回顾 u2-l1/u2-l4 的结论：本仓库不含 vLLM 源码（部署镜像里是 `vllm 0.14.0+empty` 空壳 + omni-npu 插件），所有 NPU 能力都是「零侵入」挂上去的。对「层（layer）」这种粒度，omni-npu 实际用了两种互补的姿势：

| 姿势 | 代表类 | 模型代码里怎么写 | 生效时机 |
|---|---|---|---|
| **隐式全局替换**：用 vLLM `CustomOp.register_oot` 装饰器把 NPU 子类登记为某 vLLM 层的 out-of-tree 实现 | `NPURMSNorm`、`NPUSiluAndMul`、各 rotary embedding | 照旧写 `RMSNorm(...)`、`SiluAndMul()`，完全看不出被换过 | 插件被 import 时装饰器执行、登记；模型构建/调用时分发到 NPU 子类的 `forward_oot` |
| **显式替换**：模型文件（本来就是 omni-npu 自己写的）直接 import NPU 类 | `NPUVocabParallelEmbedding`、`NPUParallelLMHead`、`NPULogitsProcessor` | `pangu_v2_moe.py` 里直接 `NPUVocabParallelEmbedding(...)` | 构建即生效，无全局副作用 |

注意第二种姿势的源文件里 `register_oot` 装饰器是被**注释掉**的（`vocab_parallel_embedding.py` 第 71、203 行，`logits_processor.py` 第 17 行）——这是一个很醒目的提示：这三个类的构造函数需要 `local_parallel`、`dp_parallel` 等额外参数、且分片组可变，做成全局替换反而危险，所以只在自家模型里点名使用。

隐式替换的注册触发点是一条很隐蔽的链，值得先记住，本讲 4.2.3 会精读：

```text
vLLM 启动早期
  → NPUPlatform.pre_register_and_update()          # vLLM 平台钩子
    → from omni_npu import layers                   # platform.py:141
      → layers/__init__.py 依次 import 各 NPU 层
        → 每个文件的 @Xxx.register_oot 装饰器执行 → 登记替换
```

### 2.2 RMSNorm 在算什么

RMSNorm 是去掉均值的 LayerNorm，Transformer 的「三明治」结构里到处都是它：

\[
\mathrm{RMSNorm}(x) = \frac{x}{\sqrt{\dfrac{1}{H}\sum_{i=1}^{H} x_i^2 + \varepsilon}} \odot \gamma
\]

- \( \gamma \)：可学习的逐通道缩放（即代码里的 `self.weight`）。
- 推理引擎里它通常还兼任「残差汇聚点」：`forward(x, residual)` 一步完成 `residual = x + residual; out = RMSNorm(residual)`。昇腾提供了融合核 `torch_npu.npu_add_rms_norm`，把加法和归一化打进一个 kernel，省一次显存往返——这就是 `NPURMSNorm` 存在的主要动机。

### 2.3 mHC：从「一条残差流」到「S 条残差流」

经典 Transformer 的残差流是一条线：\( x_{l+1} = x_l + F(x_l) \)。mHC（manifold-constrained hyper connection，流形约束超连接）把 hidden states 复制成 \( S \) 条并行的流（`mhc_num_stream`，代码里记作 `num_stream`），每个 block 前后由一个很小的路由网络决定「怎么混合」：

- **block 前**（`mhc_pre`）：把 \( S \) 条流加权合成一条输入 \( \hat{x} = \sum_s h^{\text{pre}}_s x_s \)，同时算出 block 输出的缩放系数 \( h^{\text{post}} \) 和流间混合矩阵 \( h_{\text{res}} \in \mathbb{R}^{S\times S} \)。
- **block 后**（`mhc_post`）：\( \text{new}_i = h^{\text{post}}_i \cdot F(\hat{x}) + \sum_j h_{\text{res}}[i,j] \cdot x_j \)。
- **Sinkhorn 归一化**（`mhc_sinkhorn`）：对 \( h_{\text{res}} \) 做交替行/列归一化，把它约束到近似双随机矩阵附近，防止多流残差在几十层堆叠后数值爆炸。

好处是：不同层、不同流可以学到不同的「有效残差强度」，比单一残差更稳；代价是每层多出一个小线性层 `phi` 和一堆逐元素运算，因此 NPU 侧把它全部融合成 `npu_mhc_pre` / `npu_mhc_sinkhorn` / `npu_mhc_post` 三个融合算子。

### 2.4 词表并行（vocab parallel）与 lm_head 的 TP 困境

词表很大（如 15 万+），embedding 矩阵与 lm_head 是显存大户，所以按**词表维度**切到 TP 各卡：

- 每卡只持有 \( V/\text{TP} \) 行；查表时本卡没有的 token 位置被 mask 成 0，最后 AllReduce 求和拼出完整 embedding。
- lm_head 反过来：每卡算出自己那段词表的 logits（部分和），需要汇聚全词表才能采样。

DP（数据并行）部署下还有新问题：各 DP rank 的 token 数不同，all_gather 前要补齐 padding；每个 rank 只需要**自己那份 token** 的全词表 logits，全量广播很浪费——这就是本讲 4.3 里 all_to_all 路径的动机。

### 2.5 与前面讲义的衔接

- u3-l1 讲过 DecoderLayer 的「三明治 RMSNorm」和 mHC 模块的挂载位置（`attn_mhc_module` / `mlp_mhc_module`），本讲钻进 `NPUmHC` 内部。
- u3-l3 讲过 W8A8 量化在 MoE 里的融合反量化；本讲 4.2 会看到它的「上游接口」——`NPURMSNorm` 与 `NPUSiluAndMul` 用字典 `{"x_int8", "pertoken_scale"}` 在层间传递量化张量。
- u2-l2 讲过 `NPUPlatform` 的各种钩子；本讲补上 `pre_register_and_update` 这个此前没展开的钩子。
- u2-l4 讲过 PatchManager；本讲 4.1.3 会遇到一个真实补丁 `patch_process_weights_after_loading`，它让没有 `quant_method` 的普通模块也能享受加载后处理。

## 3. 本讲源码地图

| 文件 | 角色 |
|---|---|
| [components/omni-npu/src/omni_npu/layers/mhc/npu_mhc.py](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/layers/mhc/npu_mhc.py) | mHC 的 NPU 实现：`NPUmHC` 三段式前向 + 权重融合后处理，含 naive 参考实现 |
| [components/omni-npu/src/omni_npu/layers/npu_rms_norm.py](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/layers/npu_rms_norm.py) | `NPURMSNorm`（融合 add+norm）、`NPUMiniMaxText01RMSNormTP`（TP 分片 RMSNorm） |
| [components/omni-npu/src/omni_npu/layers/activation.py](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/layers/activation.py) | `NPUSiluAndMul`：本讲综合实践的模板（全文仅 32 行） |
| [components/omni-npu/src/omni_npu/v1/layers/linear.py](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/v1/layers/linear.py) | `FlashCommLinear` 家族：带通信变换/NZ 布局/预取的 NPU 线性层 |
| [components/omni-npu/src/omni_npu/v1/layers/vocab_parallel_embedding.py](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/v1/layers/vocab_parallel_embedding.py) | `NPUVocabParallelEmbedding`、`NPUParallelLMHead`（含 DP/local 分片支持） |
| [components/omni-npu/src/omni_npu/v1/layers/logits_processor.py](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/v1/layers/logits_processor.py) | `NPULogitsProcessor`：logits 汇聚的两种通信路径 |
| [components/omni-npu/src/omni_npu/v1/models/pangu/pangu_v2_moe.py](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/v1/models/pangu/pangu_v2_moe.py) | 消费方：DecoderLayer 挂载 mHC/RMSNorm，ForCausalLM 挂载 embedding/lm_head/logits_processor |
| [components/omni-npu/src/omni_npu/platform.py](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/platform.py) | `pre_register_and_update`：触发全部 `register_oot` 注册的入口 |
| [components/omni-npu/src/omni_npu/vllm_patches/patches/models/pangu_v2_moe/patch_process_weights_after_loading.py](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/vllm_patches/patches/models/pangu_v2_moe/patch_process_weights_after_loading.py) | 让 `NPUmHC`/`NPURMSNorm` 等普通模块也能被调用 `process_weights_after_loading` 的运行时补丁 |
| [components/omni-npu/tests/unit/layers/test_activation.py](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/tests/unit/layers/test_activation.py) | 无需 NPU 的单测范式（mock `torch_npu` 算子），本讲实践的验收模板 |

## 4. 核心概念与源码讲解

本讲按三个最小模块展开：**残差流 mHC**（4.1）、**算子替换层**（4.2，含 RMSNorm、激活函数与 FlashCommLinear 家族及两条注册链路）、**logits 计算**（4.3）。

### 4.1 残差流 mHC

#### 4.1.1 概念说明

mHC 解决的问题是：单条残差流对所有层、所有样本「一视同仁」，而实际训练中发现不同深度需要不同的残差强度。mHC 维护 \( S \) 条流，每层前后用一个共享小网络 `phi` 动态生成混合系数，并用 Sinkhorn 归一化约束混合矩阵的尺度，等价于给残差流加了一个「可学习的、逐层的流量阀门」。

在 openPangu-2.0 里它的存在感很强：DecoderLayer 的注意力与 FFN 各配一个独立的 `NPUmHC` 实例（权重不同），模型最外层还有一个 `merge_mhc_module` 负责「多流合一」。u3-l1 已讲过挂载位置，本讲专注 `NPUmHC` 类本身。

`NPUmHC` 的关键设计点：

1. **三段式 API**：`mhc_pre` / `mhc_sinkhorn` / `mhc_post` 是三个独立方法而非一个大 `forward`。原因见 u3-l2 的伏笔——sinkhorn 可以被搬到 side stream 上与主算子重叠（`pangu_v2_moe.py` 中 `enable_mhc_multistream` 打开时的 cube-side 任务），拆开才能调度。
2. **`pre_only` 模式**：模型尾部的 `merge_mhc_module` 只需要把 \( S \) 条流合成一条，不需要再产生 \( h^{\text{post}}/h_{\text{res}} \)（后面没有 block 了），此时参数和输出都减半。
3. **双硬件分支**：非 Ascend950（如 910C/A3）走 `torch.ops.custom.*`（由 `omni_training_custom_ops` 包提供的定制算子），Ascend950（A5）走 `torch_npu` 内置的 `npu_mhc_*` 算子。文件顶部的 try/except import（`npu_mhc.py` 第 15-20 行）保证算子包缺失时只告警不崩溃。

#### 4.1.2 核心流程

一次完整 block 的 mHC 数据流（\( S \) 为流数，\( H \) 为 hidden_size）：

```text
输入 hidden_states: (N, S*H)
        │
        ▼
mhc_pre ─────────────────────────────────────────────┐
  1. reshape 成 (N, S, H)，fp32 化                     │
  2. RMS 归一化后乘 norm_gamma                          │
  3. 过 phi 线性层 → 输出 (S+2)*S 维                    │
  4. 切成三份:                                          │
     h_pre  (N,S)    = sigmoid(α_pre·z + β_pre) + ε    │ ← 每条流的输入权重
     h_post (N,S)    = 2·sigmoid(α_post·z + β_post)    │ ← block 输出缩放
     h_res  (N,S,S)  = z_res·α_res + β_res             │ ← 流间混合矩阵
  5. block 输入 x̂ = Σ_s h_pre_s · x_s   → (N, H)       │
        │                                              │
        ▼                                              │
[注意力或 FFN block 在 x̂ 上计算]                        │
        │                                              │
        ▼                                              ▼
mhc_post:  new_i = h_post_i · F(x̂) + Σ_j h_res[i,j] · x_j   → (N, S*H)
                        ▲
mhc_sinkhorn: h_res ← softmax(h_res) 后交替做
              (mhc_recur_norm - 1) 轮 [行归一化 → 列归一化]
```

写成公式：

\[
\hat{x} = \sum_{s=1}^{S} h^{\text{pre}}_s \odot x_s, \qquad
x'_i = h^{\text{post}}_i \cdot F(\hat{x}) + \sum_{j=1}^{S} h_{\text{res}}[i,j] \cdot x_j
\]

注意维度陷阱：`mhc_pre` 的输入输出维度不同——进 `(N, S*H)`、出 `(N, H)`（合流给 block）；`mhc_post` 反过来——进 block 输出 `(N, H)` 和旧流 `(N, S*H)`、出新流 `(N, S*H)`。Sinkhorn 归一化只动 `h_res`，不动 hidden states，因此可以被安排到别的流上异步执行。

`phi` 的输出维度是 `(S+2)*S` 而不是 `3S`，因为 `h_res` 是 \( S \times S \) 的矩阵展平。

#### 4.1.3 源码精读

**① 构造与参数**（[components/omni-npu/src/omni_npu/layers/mhc/npu_mhc.py:L23-L66](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/layers/mhc/npu_mhc.py#L23-L66)）：`NPUmHC.__init__` 从 config 读取 `mhc_num_stream`/`rms_norm_eps`/`mhc_recur_norm`，声明 `branch_alpha`（3 个标量：pre/post_res 两用）、`branch_beta`（\( S(S+2) \) 个标量）、`phi`（一个 vLLM `ReplicatedLinear`，fp32，输入 \( S \cdot H \)、输出 `(S+2)S`）和 `norm_gamma`（\( S \cdot H \)）。全部参数刻意用 fp32——mHC 的系数是 sigmoid 加权的敏感量，低精度会放大误差。

**② 权重融合后处理**（[components/omni-npu/src/omni_npu/layers/mhc/npu_mhc.py:L68-L78](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/layers/mhc/npu_mhc.py#L68-L78)）：`process_weights_after_loading` 把 `phi.weight * norm_gamma` 预乘成 `phi_weight`，并预先切出 `phi_weight_pre` / `phi_weight_post_res` 三个切片，同时把 `branch_alpha/beta` 也按用途切好。这是典型的「加载后一次性代数重排，换取前向零额外乘法」手法。注意它没有 `quant_method` 属性，vLLM 默认不会调用它——靠下面的补丁补齐：

**③ 触发补丁**（[components/omni-npu/src/omni_npu/vllm_patches/patches/models/pangu_v2_moe/patch_process_weights_after_loading.py:L44-L70](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/vllm_patches/patches/models/pangu_v2_moe/patch_process_weights_after_loading.py#L44-L70)）：vLLM 加载器只对带 `quant_method` 的模块做加载后处理；这个 pangu_v2_moe 专属补丁在原有循环之后追加了一个循环，让 `NPUPanguSparseAttention`、`NPUmHC`、`NPURMSNorm` 三类普通模块也执行 `process_weights_after_loading()`。这是 u2-l4「四要素法」（注册名、目标、符号、动机）的又一实例：目标是 model_loader 的 `process_weights_after_loading`，动机是「无 quant_method 的自定义层也要做权重重排」。

**④ naive 参考实现**（[components/omni-npu/src/omni_npu/layers/mhc/npu_mhc.py:L80-L137](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/layers/mhc/npu_mhc.py#L80-L137)）：`_mhc_pre_naive` / `_mhc_sinkhorn_naive` / `_mhc_post_naive` 用纯 torch 把 4.1.2 的公式逐行写了一遍，是与融合算子对齐数值的「规格书」。例如 `_mhc_post_naive` 的核心一行（第 133-136 行）：

```python
hidden_states = (
    h_post.unsqueeze(-1) * hidden_states.unsqueeze(-2)
    + torch.sum(h_res.unsqueeze(-1) * residual.unsqueeze(-2), dim=-3)
).to(hidden_states.dtype)
```

正好实现 \( x'_i = h^{\text{post}}_i F(\hat{x}) + \sum_j h_{\text{res}}[i,j] x_j \)。读融合算子拿不准语义时，先读这三个 naive 方法。

**⑤ 融合前向的三段**（[components/omni-npu/src/omni_npu/layers/mhc/npu_mhc.py:L139-L193](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/layers/mhc/npu_mhc.py#L139-L193)）：`mhc_pre` 在 `pre_only` 时走纯 torch 路径（小尾层不值得融合）；否则按 `on_ascend950()` 二选一——非 950 调 `torch.ops.custom.npu_manifold_constrained_hyper_connection_pre`（注意传入的是预乘后的 `self.phi_weight`），950 调 `torch_npu.npu_mhc_pre`（传入原始 `self.phi.weight` 与 `gamma`，融合由算子内部完成）。`mhc_sinkhorn`（[L195-L216](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/layers/mhc/npu_mhc.py#L195-L216)）与 `mhc_post`（[L218-L246](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/layers/mhc/npu_mhc.py#L218-L246)）同理二选一，`mhc_post` 出口处把输出 reshape 回 `(N, S*H)` 交给下一层。

**⑥ 消费方**（[components/omni-npu/src/omni_npu/v1/models/pangu/pangu_v2_moe.py:L1445-L1461](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/v1/models/pangu/pangu_v2_moe.py#L1445-L1461)）：`mhc_head` 展示了标准三连调用——`mhc_pre` → （block）→ `mhc_sinkhorn` → `mhc_post`。模型层 `OpenPanguV2Model` 的 forward 里还有 `merge_mhc_module` 合流（[L2084-L2091](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/v1/models/pangu/pangu_v2_moe.py#L2084-L2091)）：embedding 先 `.repeat(1, mhc_num_stream, 1)` 复制成 \( S \) 条流，跑完所有层后由 `merge_mhc_module`（`pre_only=True` 实例，见 [L2001-L2008](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/v1/models/pangu/pangu_v2_moe.py#L2001-L2008)）汇成单条流交给 final norm。

#### 4.1.4 代码实践（源码阅读 + CPU 复现型）

naive 实现不依赖 NPU，可以在任何装了 torch 的机器上手工复现，用来验证你对公式的理解。

1. **实践目标**：用纯 torch 在 CPU 上复现 `mhc_pre`（pre_only 分支）与 `mhc_sinkhorn_naive`，确认输出 shape 与公式一致。
2. **操作步骤**：
   - 构造随机参数与输入（示例代码，非项目原有代码，可在容器外任意 Python 环境运行）：

     ```python
     import torch
     import torch.nn.functional as F

     S, H, N, eps = 3, 8, 5, 1e-6          # num_stream / hidden / tokens / hc_eps
     x = torch.randn(N, S, H)               # S 条残差流
     phi_w = torch.randn(S, S * H)          # pre_only: phi 只需 S 行
     alpha, beta = torch.randn(1), torch.randn(S)
     gamma = torch.randn(S * H)

     # 复现 mhc_pre 的 pre_only 分支（对照 npu_mhc.py L143-L167）
     flat = x.reshape(N, S * H).float()
     rstd = torch.rsqrt(flat.square().mean(-1, keepdim=True) + eps)
     z = F.linear(flat * rstd * gamma, phi_w)
     h_pre = torch.sigmoid(z * alpha + beta.view(1, S))
     out = (h_pre.view(N, S, 1) * x).sum(1)     # (N, H)
     print("mhc_pre out:", out.shape)           # 期望 torch.Size([5, 8])

     # 复现 _mhc_sinkhorn_naive（对照 npu_mhc.py L107-L124）
     h_res = torch.randn(N, S, S).softmax(-1) + eps
     h_res = h_res / (h_res.sum(-2, keepdim=True) + eps)      # 列归一化
     for _ in range(3 - 1):                                   # mhc_recur_norm=3
         h_res = h_res / (h_res.sum(-1, keepdim=True) + eps)  # 行
         h_res = h_res / (h_res.sum(-2, keepdim=True) + eps)  # 列
     print("row sums:", h_res.sum(-1))         # 每行和都应接近 1
     ```
   - 再打开 [pangu_v2_moe.py:L1799-L1820](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/v1/models/pangu/pangu_v2_moe.py#L1799-L1820)，对照看模型里 `mhc_pre` → `maybe_register_mhc_task` → `mhc_post` 的真实调用顺序。
3. **需要观察的现象**：`out` 的 shape 是 `(N, H)`；Sinkhorn 迭代后每行和收敛到 1 附近（列和不一定为 1，因为循环次数有限）。
4. **预期结果**：与 4.1.2 公式逐项吻合。若想进一步验证与融合算子的一致性（naive vs `torch.ops.custom.*`），需要 NPU 环境，**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：`phi` 的输出维度是 `(S+2)*S` 而不是 `3S`，多出来的部分是什么？`pre_only=True` 时为什么缩小成 `S`？

**答案**：`h_res` 是 \( S \times S \) 的流间混合矩阵，展平后占 \( S^2 \) 维，加上 `h_pre`、`h_post` 各 \( S \) 维，共 \( S^2 + 2S = (S+2)S \)。`pre_only` 模式（模型尾部 merge 模块）后面不再有 block，不需要 `h_post` 与 `h_res`，只需 `h_pre` 的 \( S \) 维，见 `npu_mhc.py` 第 55-62 行的构造与第 70 行的切片。

**练习 2**：为什么 `mhc_sinkhorn` 要拆成独立方法，而不是并进 `mhc_pre`？

**答案**：两个原因：(1) Sinkhorn 只依赖 `h_res`、不依赖 hidden states，是纯粹的系数归一化，可搬到 side stream 与 block 主计算重叠（`enable_mhc_multistream` 打开时由 `cube_side_task_ops` 调度，`pangu_v2_moe.py` 第 1415-1418 行把 mHC 模块挂到子模块上就是为了这个查找）；(2) 模型代码需要在 `mhc_pre` 与 `mhc_post` 之间插入注意力/FFN，融合在一起就没有插入点了。

**练习 3**：`process_weights_after_loading` 里预乘 `phi.weight * norm_gamma` 为什么是安全的优化？什么情况下这种「常量折叠」不安全？

**答案**：`norm_gamma` 是加载后不再变化的推理常量，`phi.weight` 也是常量，两者逐元素相乘的结果在前向中每次都相同，提前算一次可把每步前向的 \( S \cdot H \) 次乘法省掉。若权重会被动态修改（如 EPLB 重排专家、RL 在线更新权重、sleep/wake 卸载后重建），预乘结果就会与源权重脱钩，必须重算——u3-l3 的 EPLB 重排就是需要警惕此类缓存的场景。

### 4.2 算子替换层

#### 4.2.1 概念说明

本模块讲「怎么换层」，是三个模块里方法论价值最高的。omni-npu 的替换对象分三档：

| 档位 | 对象 | 手法 | 本讲例子 |
|---|---|---|---|
| 计算核替换 | vLLM 层内部的数学 | 继承 vLLM 类、覆写 `forward_oot`、`@register_oot` 全局登记 | `NPURMSNorm`、`NPUSiluAndMul` |
| 结构替换 | 整个线性层容器 | 自建类体系，模型显式使用 | `FlashCommLinear` 家族 |
| 后处理钩子 | 权重加载完成后的重排 | 运行时补丁扩展 vLLM 加载器循环 | `patch_process_weights_after_loading`（4.1.3 已读） |

替换的动机高度一致：**昇腾 Cube（矩阵）单元与量化体系要求张量以特定布局、特定 dtype 流动**。具体到本模块：

- `NPURMSNorm`：用 `npu_add_rms_norm` 把「残差相加 + 归一化」融成一个核；顺带支持归一化后立即 AllGather（`y_transform="AG"`）与立即动态量化（`quant_symbol`），都是「把逐元素算子往邻居身上贴」的融合。
- `NPUSiluAndMul`：普通路径用 `npu_swiglu` 融合核；W8A8 路径用 `npu_dequant_swiglu_quant` 一个核完成「反量化 → SwiGLU → 再量化」，输入输出都是 int8 字典。
- `FlashCommLinear`：把「通信变换」做进线性层——每个层可配置进入端（x）与离开端（y）做 AllReduce/ReduceScatter/AllGather/All2All，从而支持 FlashComm 类的通信-计算重叠；同时把权重转置成 `[in, out]` 并（可选）转成 FRACTAL_NZ 布局喂给 Cube 单元。

#### 4.2.2 核心流程

**`register_oot` 的装配链**（隐式替换的生命周期）：

```text
vLLM 启动
  → 按平台插件机制加载 NPUPlatform（u2-l1）
  → vLLM 调用 NPUPlatform.pre_register_and_update()      # 在全局配置初始化前
      → from omni_npu import layers                       # platform.py:141
        → layers/__init__.py import NPURMSNorm / NPUSiluAndMul / ...
          → 各文件模块级 @RMSNorm.register_oot / @SiluAndMul.register_oot 执行
            → vLLM CustomOp 登记表中记下「基类 → NPU 子类」
  → 模型构建：pangu_v2_moe.py 写的是 RMSNorm(...) / SiluAndMul()
  → 调用时分发到 NPURMSNorm.forward_oot / NPUSiluAndMul.forward_oot
```

**`NPURMSNorm.forward_oot` 的三条分支**：

```text
输入 x（可能有 residual）
  ├─ 模型配置 omni_disable_npu_add_rms_norm = True
  │    → 先 x += residual，再 npu_rms_norm（退化为非融合路径，用于对拍/规避算子问题）
  ├─ residual 存在（主路径）
  │    → npu_add_rms_norm(x, residual, weight, eps)  一个核返回 (out, _, new_residual)
  │    → 可选 y_transform=="AG"：立刻 all_gather
  │    → 可选 quant_symbol=True：npu_dynamic_quant → 返回 {"x_int8", "pertoken_scale"}
  └─ residual 为 None → npu_rms_norm(x, weight, eps)
```

**量化字典协议**：W8A8（u3-l3/u8）要求进线性层的是 int8 张量加 per-token scale。为此 `NPURMSNorm` 和 `NPUSiluAndMul` 约定：当上一层以字典 `{"x_int8": …, "pertoken_scale": …}` 传递激活时，自己输出同样格式的字典，让「归一化 → 量化 → matmul → 反量化 → 激活 → 再量化」在层间以 int8 贯通，fp16/bf16 只在融合核内部短暂存在。

**`FlashCommLinear` 的前向**（以 Row 为例）：

```text
forward(input_)
  1. input_is_parallel=False 时先按 last dim 切出本 rank 分片
  2. quant_method.apply(layer, x, bias, x_transform, x_dim)
       └─ apply 内部先做 x 侧通信变换（layer_parallel_communication_op）
          再 torch.matmul / addmm（权重已转置成 [in, out]）
  3. 可选：对 next_layer 的权重做 npu_prefetch（按 attn_prefetch MiB）
  4. y 侧通信变换（AllReduce / ReduceScatter / AllGather / ALL2ALL）
```

x/y 变换从哪来？`FlashCommLinearBase.__init__` 用 `get_layer_transform_type(层内名, "x"/"y")` 查一张按层名索引的通信配置表（`_LAYER_COMM_DICT`），查不到就是 `NoOp`。也就是说**每个线性层可以拥有独立于全局 TP/DP 的通信方案**，这是为「按层定制并行策略」留的扩展缝。

#### 4.2.3 源码精读

**① 注册触发点**（[components/omni-npu/src/omni_npu/platform.py:L126-L141](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/platform.py#L126-L141)）：`pre_register_and_update` 是 vLLM 平台钩子，文档字符串明说是给 out-of-tree 平台「在全局 VllmConfig 初始化之前注册东西」用的；它只有一行实体——`from omni_npu import layers`。而 [layers/__init__.py:L13-L14](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/layers/__init__.py#L13-L14) 正是 `NPURMSNorm`、`NPUSiluAndMul` 等的聚集 import。**新增 `register_oot` 层却忘记加进这个 `__init__.py`，是这类插件最常见的「写了没生效」事故**（模块没被 import，装饰器根本没执行）。

**② `NPUSiluAndMul`**（[components/omni-npu/src/omni_npu/layers/activation.py:L11-L32](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/layers/activation.py#L11-L32)）：全文只有一个类两个分支——`quant_symbol=True` 且输入是字典时，从字典取 `x_int8/out_scale/in_scale/pertoken_scale` 组参数调 `torch_npu.npu_dequant_swiglu_quant`，返回 `{"x_int8": h, "pertoken_scale": pertoken_scale}`；否则直接 `torch_npu.npu_swiglu(x)`。这就是综合实践要仿写的模板。

**③ `NPURMSNorm`**（[components/omni-npu/src/omni_npu/layers/npu_rms_norm.py:L186-L224](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/layers/npu_rms_norm.py#L186-L224)）：`@RMSNorm.register_oot` 装饰（L186）+ `process_weights_after_loading` 缓存 fp32 权重（L188-189）+ `forward_oot` 四分支（详见 4.2.2 流程图）。注意第 198 行读的是 `model_extra_config.operator_opt_config.omni_disable_npu_add_rms_norm`——u5-l1 将系统讲的模型最佳实践配置，在这里第一次以「算子开关」面目出现。同文件上方的 `NPUMiniMaxText01RMSNormTP`（[L22-L183](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/layers/npu_rms_norm.py#L22-L183)）演示了更复杂的变体：TP 分片的 q/k RMSNorm 通过「本地均方 + AllReduce 求全局均方」修正缩放，是 register_oot 手法的进阶样例。

**④ 消费方无感知**（[components/omni-npu/src/omni_npu/v1/models/pangu/pangu_v2_moe.py:L1364-L1388](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/v1/models/pangu/pangu_v2_moe.py#L1364-L1388)）：DecoderLayer 里五个 `RMSNorm(...)` 全部用的是 vLLM 原名——运行时实际类型是 `NPURMSNorm`（验证方法见 4.2.4）。同理 [L172](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/v1/models/pangu/pangu_v2_moe.py#L172) 的 `self.act_fn = SiluAndMul()` 实际是 `NPUSiluAndMul`。

**⑤ `FlashCommLinear` 的构造与通信配置**（[components/omni-npu/src/omni_npu/v1/layers/linear.py:L146-L192](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/v1/layers/linear.py#L146-L192)）：`FlashCommLinearBase.__init__` 用 `get_last_two_parts(prefix)` 取「块内层名」（如 `...self_attn.q_b_proj` → `self_attn.q_b_proj`），再查 x/y 变换与维度（L171-175）；TP rank/size 也按层查询（`get_layer_parallel_rank`），支持每层不同并行度。分发函数 [layer_parallel_communication_op（L44-L58）](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/v1/layers/linear.py#L44-L58) 是一个五路 switch：`ALL2ALL/AllReduce/ReduceScatter/AllGather/NoOp`。

**⑥ NZ 布局与转置**（[components/omni-npu/src/omni_npu/v1/layers/linear.py:L110-L126](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/v1/layers/linear.py#L110-L126)）：`UnquantizedFlashCommLinearMethod.process_weights_after_loading` 先 `weight.t().contiguous()` 把 PyTorch 惯例的 `[out, in]` 转成 `[in, out]`（这样前向直接 `torch.matmul(x, W)`）；若模型配置 `unquant_bmm_nz` 打开且不是被排除的 `kv_b_proj`，再 `npu_format_cast(..., FRACTAL_NZ)` 转成昇腾矩阵单元偏好的分块布局。此后每个 `weight_loader`（如 [L359-L385](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/v1/layers/linear.py#L359-L385)）都要「NZ→ND、转回、按 TP 窄切、copy、再转置、再 NZ」往返搬运，并在最后 `set_aclgraph_recapture(True)` 提示 ACL Graph 权重布局变了需要重捕获（u5-l2 的伏笔）。

**⑦ 权重预取**（[components/omni-npu/src/omni_npu/v1/layers/linear.py:L837-L842](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/v1/layers/linear.py#L837-L842)）：`RowParallelFlashCommLinear.forward` 接受 `next_layer` 参数，在算当前层时对下一层权重调 `torch_npu.npu_prefetch`，预取量由模型配置 `attn_prefetch`（MiB，L839 换算成字节）控制——把 HBM 带宽的空闲窗口利用起来。

**⑧ 家族全景与消费方**：`ColumnParallelFlashCommLinear`（[L294](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/v1/layers/linear.py#L294)，输出维切分）、`QKVParallelFlashCommLinear`（[L419](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/v1/layers/linear.py#L419)，其 ALL2ALL 模式要求权重交错重排，注释 [L545-L561](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/v1/layers/linear.py#L545-L561) 画了头重排示意）、`MergedColumnParallelFlashCommLinear`（[L657](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/v1/layers/linear.py#L657)、gate/up 合并）、`RowParallelFlashCommLinear`（[L728](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/v1/layers/linear.py#L728)、输入维切分）与 `ShardedLinear`（[L985](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/v1/layers/linear.py#L985)，按字节分片 + all_gather 重组的全权重懒聚集）。消费方两处最典型：MLA 注意力的 `q_b_proj/kv_b_proj/o_proj`（[npu_pangu.py:L793-L849](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/v1/layers/attention/npu_pangu.py#L793-L849)）与共享专家 MLP 的 `gate_up_proj/down_proj`（[fused_mlp/layer.py:L133-L149](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/v1/layers/fused_mlp/layer.py#L133-L149)）。

#### 4.2.4 代码实践（验证替换确实发生了）

1. **实践目标**：证明「模型代码写 `RMSNorm`，运行时是 `NPURMSNorm`」这条隐式替换链在真实部署里生效。
2. **操作步骤**（在已按 u1-l4 拉起的容器内，或任何装好镜像的 NPU 环境；本实践需要真机，**待本地验证**）：
   - 用可编辑安装（u1-l2 讲过 `bash build/build.sh -m omni-npu` 即 `pip install -e .`）确保改码即生效。
   - 在 `NPUSiluAndMul.forward_oot` 入口临时加一行日志（练习性质，验证后还原）：

     ```python
     logger.info("NPUSiluAndMul forward_oot called, quant_symbol=%s", quant_symbol)
     ```

     （需在文件头部补 `from vllm.logger import init_logger; logger = init_logger(__name__)`。）
   - 或者不改码，直接在 Python 里检查替换关系（容器内执行）：

     ```bash
     python -c "
     import omni_npu.layers  # 触发 register_oot
     from vllm.model_executor.layers.layernorm import RMSNorm
     from omni_npu.layers.npu_rms_norm import NPURMSNorm
     print(NPURMSNorm.__mro__[1])   # 应打印 vLLM 的 RMSNorm
     "
     ```

   - 发起一次推理（u1-l5 的 curl 请求），观察日志。
3. **需要观察的现象**：每次前向都出现 `NPUSiluAndMul forward_oot called` 日志；而 `pangu_v2_moe.py` 里没有任何 `NPUSiluAndMul` 字样。
4. **预期结果**：替换链 `pre_register_and_update → import layers → @register_oot → forward_oot` 全程闭合。若日志不出现，第一嫌疑就是模块没被 `layers/__init__.py` import，第二嫌疑是 `VLLM_PLUGINS` 未包含 omni-npu 插件（u2-l1）。

#### 4.2.5 小练习与答案

**练习 1**：`NPURMSNorm.forward_oot` 里 `quant_symbol=True` 时返回字典，这对下游线性层意味着什么？和 u3-l3 的哪个机制呼应？

**答案**：意味着下游必须按 W8A8 量化路径执行——`x_int8` 进量化 matmul、`pertoken_scale` 做反量化缩放。这与 u3-l3 讲的 MoE 回收阶段「融合反量化」、u8 jointfix 产出的 W8A8 权重是一套体系：激活侧 per-token 动态量化由 `npu_dynamic_quant` 完成，权重侧 per-channel 静态量化由量化权重自带，两层配合把整段计算留在 int8 域。

**练习 2**：`FlashCommLinear` 把权重存成 `[in, out]` 转置形状，为什么 `weight_loader` 里要先 `.t_()` 转回去再 copy，copy 完又转回来？

**答案**：checkpoint 里权重是 `[out, in]` 的原始布局，而参数张量被 `process_weights_after_loading` 整成了转置（可能还是 FRACTAL_NZ）布局。加载时若不先还原成同构形状，`narrow`（按 output_dim 切 TP 分片）与 `copy_` 的坐标就对不上；所以流程是「NZ→ND、转置还原 → 按 TP 窄切拷贝 → 再转置、再 NZ」。这是「为算子优化存储布局」给「为框架兼容加载流程」付的税。

**练习 3**：如果只写了一个新的 `register_oot` 层文件，却忘了挂到 `layers/__init__.py`，会发生什么？为什么不报 ImportError？

**答案**：什么都不会发生——该文件从未被 import，装饰器从未执行，vLLM 继续用原生实现，模型照常跑（只是没加速/没适配），属于静默失效。不报错是因为 Python 模块只有被 import 才执行，文件躺在目录里没有任何副作用；这正是 4.2.4 实践要用日志验证替换的原因。

### 4.3 logits 计算

#### 4.3.1 概念说明

logits 计算是「最后一步、最容易被忽视、但在 DP 部署下通信量惊人」的环节。它由三个类接力：

- `NPUVocabParallelEmbedding`：按词表维切分的查表层，输出经 AllReduce（或序列并行时 ReduceScatter）汇聚。
- `NPUParallelLMHead`：继承前者，是「反向查表」——hidden_states 乘词表分片矩阵得到本卡那段 logits；它比父类多出 `dp_parallel` / `local_lmhead_parallel` 两种分片组选择，以及类属性 `_dp_pad_n`（DP all_gather 的补齐目标）。
- `NPULogitsProcessor`：决定「怎么把分片 logits 汇聚成每 rank 采样所需的全词表 logits」，是通信策略的真正所在。

两条通信路径的动机对比：

| 路径 | 触发条件 | 步骤 | 通信量（\( W \) 为组内 rank 数） |
|---|---|---|---|
| TP 路径 | 普通部署（无 DP/local lm_head 分片） | 各卡算自己 token 的全部？不——各卡对全部 token 算自己词表段，再 all_gather 词表维 | 每 rank 收发 \( O(N \cdot V) \) |
| DP/local 路径 | `ena_dp_lmhead_parallel` 或 `ena_local_lmhead_parallel` 打开 | hidden_states 补齐到 `_dp_pad_n` 后 all_gather → 各卡对**全部** token 算自己词表段 → all_to_all 把「token 维 × 词表段」矩阵转置回「本卡 token × 全词表」 | 每 rank 收发 \( O(N \cdot V) \)，但 all_to_all 走「各取所需」的点对点模式，且 hidden_states 的 all_gather 只有 \( O(N \cdot H) \)，\( H \ll V \) |

DP 路径的本质：DP 各 rank 的 token 互不相同，词表又被 lm_head 分片切开了；all_to_all 恰好完成「行=token、列=词表段」的双重分发，让每个 rank 最终只持有自己 token 的完整 logits，避免任何 rank 物化全量 \( N_{\text{global}} \cdot V \) 矩阵。

#### 4.3.2 核心流程

`NPULogitsProcessor._get_logits` 的完整流程：

```text
输入: hidden_states (n_local, H)，lm_head（词表分片）
  1. 判断 use_local_comm / use_dp_comm（看 lm_head 的构造标志）
  2. 若是 DP/local 路径:
     a. 取 comm_group（local world group 或 DP group）
     b. 记录 local_n；若 local_n < lm_head._dp_pad_n 则补零行
     c. comm_group.all_gather(hidden_states)      # 凑齐全部 DP rank 的 token
  3. logits = lm_head.quant_method.apply(lm_head, hidden_states, bias)
       # 每 rank 得到 (n_all, V/W)：全部 token × 本卡词表段
  4. 汇聚:
     DP/local 路径:
       - 环境变量 OMNI_NPU_USE_DEVICE_COMM_A2A=1 时走 device_communicator.all_to_all
       - 否则 view(W, n, V/W) → torch.distributed.all_to_all_single
         → transpose 回 (n, V) → 截取前 local_n 行
     TP 路径: tensor_model_parallel_all_gather(logits)
  5. 裁掉词表 padding: logits[..., :org_vocab_size]
```

`_dp_pad_n` 的供给链（这是「类属性当跨实例信道」的小技巧）：

```text
每步 forward 前，NPUModelRunner.set_forward_context
  → _capture_dp_pad_target(forward_context)          # npu_model_runner.py:887
  → 读 dp_metadata.max_tokens_across_dp_cpu（CPU 张量，取 int 零开销）
  → 写 NPUParallelLMHead._dp_pad_n = ...             # 所有实例共享同一值
  → compute_logits 里 _get_logits 直接读类属性，免去每次通信求最大值
```

`NPUVocabParallelEmbedding.forward` 则是查表侧的镜像流程：mask 掉不属于本卡词表段的 token → 查表 → 越界位置清零 → `enable_scatter=True` 时 ReduceScatter（序列并行，每 rank 拿回自己 token 段的完整 embedding），否则 AllReduce。

#### 4.3.3 源码精读

**① 词表 mask**（[components/omni-npu/src/omni_npu/v1/layers/vocab_parallel_embedding.py:L41-L68](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/v1/layers/vocab_parallel_embedding.py#L41-L68)）：`get_masked_input_and_mask` 计算每个 token 是否落在本卡 `[org_vocab_start, org_vocab_end)` 或 added 段内，并把 id 改写成本卡局部行号。第 54-57 行的优化很典型：没有 added vocab 时跳过第二个 mask 的构造，省一次全量比较。

**② 分片组可切换的构造**（[components/omni-npu/src/omni_npu/v1/layers/vocab_parallel_embedding.py:L74-L95](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/v1/layers/vocab_parallel_embedding.py#L74-L95)）：`NPUVocabParallelEmbedding.__init__` 显式调用 `torch.nn.Module.__init__(self)` 而不是走父类构造（父类构造按 TP 组切分，逻辑不同），然后按 `local_parallel` 决定用 local world group 的 local_rank 还是 TP rank 来切词表。注意文件第 71 行被注释掉的 `@VocabParallelEmbedding.register_oot`——它走的是显式替换路线。

**③ 查表前向**（[components/omni-npu/src/omni_npu/v1/layers/vocab_parallel_embedding.py:L163-L200](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/v1/layers/vocab_parallel_embedding.py#L163-L200)）：`forward` 的 `enable_scatter` 分支先把 token 数向上取整补齐到 TP 的倍数（ReduceScatter 要求各 rank 等长，L165-170），查表清零后按 `local_parallel` 选择 `reduce_scatter_local`（local 组）或 `tensor_model_parallel_reduce_scatter`（TP 组）返回（L193-197）；默认分支 AllReduce（L200）。

**④ `NPUParallelLMHead` 的再分片**（[components/omni-npu/src/omni_npu/v1/layers/vocab_parallel_embedding.py:L204-L278](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/v1/layers/vocab_parallel_embedding.py#L204-L278)）：类属性 `_dp_pad_n` 的注释（L206-210）解释了为什么放类上——每步的补齐目标对所有 DP 分片的 lm_head 实例相同，写类属性避免逐实例更新。构造函数里 `dp_parallel` 用 DP 组、`local_lmhead_parallel` 用 local world 组重算 `shard_indices` 并重建权重（L239-278）。第 302-304 行的 `weight_loader` 末尾把权重 `npu_format_cast(param.data, 29)`——29 即 FRACTAL_NZ 的 ACL 格式枚举值（可在容器内用 `int(torch_npu.Format.FRACTAL_NZ)` 核对，**待本地验证**），与 4.2.3 的线性层 NZ 布局动机相同：lm_head 也是一个大 matmul。

**⑤ `NPULogitsProcessor` 主战场**（[components/omni-npu/src/omni_npu/v1/layers/logits_processor.py:L18-L68](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/v1/layers/logits_processor.py#L18-L68)）：第 26-41 行判路 + 补齐 + all_gather；第 43 行一行完成 lm_head matmul（复用 lm_head 自带的 quant_method，天然支持 W8A8 的 lm_head）；第 45-63 行 all_to_all 两条实现（`OMNI_NPU_USE_DEVICE_COMM_A2A` 环境变量切换设备侧融合版与 `torch.distributed.all_to_all_single` 通用版），转置后截取 `[:local_n]`；第 64-65 行 TP 路径的 all_gather；第 66-68 行统一裁剪到原始词表长。

**⑥ pad 目标的写入方**（[components/omni-npu/src/omni_npu/worker/npu_model_runner.py:L887-L909](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/worker/npu_model_runner.py#L887-L909)）：`_capture_dp_pad_target` 的文档字符串讲清了设计：从 `forward_context.dp_metadata` 读 CPU 张量取 `int()` 零同步开销，写一次类属性，`compute_logits` 不再自己做「DP 尺寸 all_gather + host 同步」。第 912-915 行的注释还提到空闲 DP rank 要通过 `_dummy_run` 触发同样的 collective，避免集合通信挂死——DP 集合通信的经典坑。

**⑦ 消费方**（[components/omni-npu/src/omni_npu/v1/models/pangu/pangu_v2_moe.py:L2177-L2200](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/v1/models/pangu/pangu_v2_moe.py#L2177-L2200)）：`OpenPanguV2ForCausalLM.__init__` 按 `parall_config.ena_local_lmhead_parallel / ena_dp_lmhead_parallel`（u5-l1 的 ModelParallelConfig）决定 `local_lmhead/dp_lmhead` 布尔量，实例化 `NPUParallelLMHead` 与 `NPULogitsProcessor`；embedding 侧则是 [L1985](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/v1/models/pangu/pangu_v2_moe.py#L1985) 的 `NPUVocabParallelEmbedding`。

#### 4.3.4 代码实践（通信量估算 + 源码追踪）

1. **实践目标**：用具体数字体会 DP lm_head 分片 + all_to_all 的收益，并追踪 `_dp_pad_n` 的完整供给链。
2. **操作步骤**：
   - 纯纸面推演（无需环境）。设词表 \( V = 151{,}936 \)、hidden \( H = 7{,}168 \)、DP 组 \( W = 4 \)、各 rank token 数 \( n = 1024 \)、dtype 为 bf16（2 字节）：
     - **TP all-gather 方案**（每卡算自己 token 的词表段再汇聚）：每 rank 需要收到其他 3 个 rank 的词表段，通信量 \( \approx 3 \times n \times V \times 2\,\text{B} \approx 3 \times 1024 \times 151936 \times 2 \approx 0.87\,\text{GB} \) 量级（按 all_gather 总收发口径）；
     - **DP 分片 + all_to_all 方案**：hidden all_gather 通信量 \( \approx 3 \times n \times H \times 2\,\text{B} \approx 3 \times 1024 \times 7168 \times 2 \approx 42\,\text{MB} \)，all_to_all 收发 \( \approx 3 \times n \times (V/4) \times 2\,\text{B} \approx 0.22\,\text{GB} \)。
     - 对比结论写下来：hidden 维 all_gather 把大头从 \( V \) 降到 \( H \)。
   - 源码追踪：从 `npu_model_runner.py` 的 `_capture_dp_pad_target`（[L887-L909](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/worker/npu_model_runner.py#L887-L909)）出发，找到它被 `set_forward_context` 调用的位置，再跟到 `NPULogitsProcessor._get_logits` 读 `_dp_pad_n` 的那一行，画出「谁写、谁读、何时写」的时序小图。
3. **需要观察的现象**：纸面估算中两条路径的通信量差一个数量级左右（\( H \ll V \) 是根因）；时序图上 `_dp_pad_n` 的写入发生在每步 forward context 设置时、读取发生在 compute_logits。
4. **预期结果**：能口头复述「DP lm_head 分片省通信」的两级结构：先用廉价的 hidden all_gather 换掉昂贵的 logits all_gather 前置条件，再用 all_to_all 让每 rank 只拿自己的 token。真机上的端到端收益对比（开/关 `ena_dp_lmhead_parallel` 的 decode 延迟）**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：`NPULogitsProcessor._get_logits` 为什么要先把 hidden_states 补零到 `_dp_pad_n` 再 all_gather？

**答案**：集合通信要求所有参与 rank 的张量形状完全一致，而 DP 各 rank 每步的 token 数几乎必然不同（batch 不齐）。`_dp_pad_n` 是 runner 从 `dp_metadata` 拿到的「全部 DP rank 中的最大 token 数」，补齐到它保证 all_gather 不会因形状不匹配报错；补进去的零行算出的 logits 最终被 `[:local_n]` 截掉，不影响结果。

**练习 2**：`NPUVocabParallelEmbedding.forward` 里 `enable_scatter=True` 与默认分支返回的张量有什么区别？各适合什么场景？

**答案**：默认分支 AllReduce，每 rank 拿到**全部 token** 的完整 embedding，适合普通 TP（后面每卡的注意力需要看本卡全部 token）；`enable_scatter=True` 分支 ReduceScatter，每 rank 只拿回自己在序列维度上那一段 token 的完整 embedding，适合全局序列并行（后续计算本身也按序列切分）的场景。代价是 RS 前要先把 token 数补齐到 TP 倍数（L165-170 的取整补零）。

**练习 3**：embedding 层和 lm_head 层的权重都可以 `tie_weights` 共享，本仓实现里 tie 的条件是什么（[vocab_parallel_embedding.py:L293-L300](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/v1/layers/vocab_parallel_embedding.py#L293-L300) 与 [pangu_v2_moe.py:L2196-L2197](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/v1/models/pangu/pangu_v2_moe.py#L2196-L2197)）？

**答案**：`tie_weights` 直接把 `self.weight` 指向 embed_tokens 的 weight（GGUF 量化除外）。但模型侧只有在 `config.tie_word_embeddings` 为真**且** lm_head 没走 local/dp 分片（`local_lmhead`/`dp_lmhead` 都为 False）时才 tie——因为分片模式下两者的切分组和布局（lm_head 权重还做了 NZ cast）不同，直接共享指针会算错。

## 5. 综合实践

**任务**：仿照 `layers/activation.py` 中 `NPUSiluAndMul` 的写法，为 GeLU-and-Mul 结构实现一个 NPU 适配层 `NPUGeluAndMul`，走完「实现 → 注册 → 单测」三步，并回答注册点问题。

### 5.1 实践目标

把 4.2 学到的替换套路完整走一遍：一个新层从零到「被 vLLM 模型构建时分发」，中间要经过哪几个文件、哪几行代码。

### 5.2 操作步骤

**步骤 1：确认基类存在**（容器内，**待本地验证**）：

```bash
python -c "from vllm.model_executor.layers.activation import GeluAndMul; print(GeluAndMul)"
```

若镜像中的 vLLM 版本没有 `GeluAndMul`，改用任何存在的 `CustomOp` 激活基类即可，套路不变。

**步骤 2：编写实现**。在 `components/omni-npu/src/omni_npu/layers/` 下新建 `activation_exercise.py`（以下为**示例代码**，练习性质，验证后请删除，勿提交）：

```python
from typing import Any

import torch
import torch_npu
from vllm.model_executor.layers.activation import GeluAndMul


@GeluAndMul.register_oot
class NPUGeluAndMul(GeluAndMul):
    def forward_oot(
        self,
        x: torch.Tensor | dict[str, Any],
        quant_symbol: bool = False,
    ) -> torch.Tensor | dict[str, Any]:
        if quant_symbol and isinstance(x, dict):
            # 真实适配时应替换为 torch_npu 提供的融合量化 GeLU 算子（若存在）；
            # 这里保持与 NPUSiluAndMul 相同的字典协议即可。
            raise NotImplementedError("exercise: fused quant path left blank")

        # 未量化路径：拆成两半，gelu(x1) * x2。
        # 真实适配时优先查 torch_npu 是否有融合版（如 npu_gelu 系列），有则替换。
        x1, x2 = x.chunk(2, dim=-1)
        return torch.nn.functional.gelu(x1) * x2
```

与模板 `NPUSiluAndMul`（[activation.py:L11-L32](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/layers/activation.py#L11-L32)）逐项对照：装饰器换成目标基类、`forward_oot` 签名保持 `(x, quant_symbol)`、字典分支保留协议但可以暂不实现。

**步骤 3：注册**。这是本题的第二问，答案有两层：

- **必须做**：在 [layers/__init__.py](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni_npu/layers/__init__.py#L14) 第 14 行 `NPUSiluAndMul` 的 import 旁边追加一行：

  ```python
  from omni_npu.layers.activation_exercise import NPUGeluAndMul
  ```

  原因：注册的唯一触发链是 `NPUPlatform.pre_register_and_update → from omni_npu import layers`（[platform.py:L141](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni_npu/platform.py#L141)），文件不被 import 装饰器就不执行（见练习 4.2.5-3）。
- **二选一的替代方案**：如果只想让 pangu 模型用、不想全局替换（像 `NPUParallelLMHead` 那样），则**不**加 register_oot，改为在模型文件里显式 `from omni_npu.layers.activation_exercise import NPUGeluAndMul` 并把 `OpenPanguV2MLP` 第 172 行的 `SiluAndMul()` 换成 `NPUGeluAndMul()`。适合需要额外构造参数、或替换影响面必须可控的层。

**步骤 4：补单测**。仿照 [tests/unit/layers/test_activation.py](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/tests/unit/layers/test_activation.py)，在 `tests/unit/layers/` 下新建 `test_activation_exercise.py`（**示例代码**）。原测试的精髓是用 `unittest.mock.patch` 把 `torch_npu.npu_swiglu` 换成 `MagicMock` 再断言调用参数（[test_activation.py:L25-L29](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/tests/unit/layers/test_activation.py#L25-L29)），因此不需要 NPU。我们的练习版同理 mock 掉 `torch.nn.functional.gelu`：

```python
import unittest
from unittest.mock import patch, MagicMock
import torch
from omni_npu.layers.activation_exercise import NPUGeluAndMul


class TestNPUGeluAndMul(unittest.TestCase):
    def test_forward_splits_and_multiplies(self):
        layer = NPUGeluAndMul()
        x = torch.randn(4, 16)
        with patch("torch.nn.functional.gelu") as gelu_mock:
            gelu_mock.side_effect = lambda t: t * 0.5   # 可预测的假 gelu
            out = layer(x)
        x1, x2 = x.chunk(2, dim=-1)
        self.assertTrue(torch.allclose(out, (x1 * 0.5) * x2))
        gelu_mock.assert_called_once()


if __name__ == "__main__":
    unittest.main()
```

**步骤 5：运行与验证**（容器内或本机均可，无 NPU 依赖；具体命令输出**待本地验证**）：

```bash
cd components/omni-npu
pytest tests/unit/layers/test_activation_exercise.py -v      # 或 ./tests/run_tests.sh unit
```

### 5.3 需要观察的现象

1. 单测通过，`gelu_mock` 恰好被调用一次，输出等于 `gelu(x1) * x2`。
2. 在容器里 `python -c "import omni_npu.layers; ..."` 后，构造 `GeluAndMul()` 类实例并调用，分发到的是 `NPUGeluAndMul.forward_oot`（可在 `forward_oot` 里临时加 `logger.info` 验证，方法同 4.2.4）。

### 5.4 预期结果

- 掌握判断标准：**新增 `register_oot` 层的三件套 = 实现文件 + `layers/__init__.py` 挂 import + mock 式单测**，缺一不可。
- 能说清两条注册路线的取舍：全局替换（零模型改动、影响所有模型）vs 显式替换（只影响点名模型、可带额外参数）。
- 练习完成后删除 `activation_exercise.py` 与测试文件，恢复 `layers/__init__.py`，保持工作区干净。

## 6. 本讲小结

- **mHC 残差流**：`NPUmHC` 以 `mhc_pre → mhc_sinkhorn → mhc_post` 三段式管理 \( S \) 条并行残差流，\( \hat{x} = \sum_s h^{\text{pre}}_s x_s \)、\( x'_i = h^{\text{post}}_i F(\hat{x}) + \sum_j h_{\text{res}}[i,j] x_j \)；Sinkhorn 把混合矩阵约束到近双随机；naive 方法就是规格书，融合算子按 `on_ascend950()` 分成 `torch.ops.custom.*` 与 `torch_npu.npu_mhc_*` 两族；`process_weights_after_loading` 预乘 `phi.weight * norm_gamma`，靠 pangu 专属补丁触发。
- **两条替换路线**：`@register_oot` 隐式全局替换（`NPURMSNorm`、`NPUSiluAndMul`，触发链在 `platform.py:141` 的 `from omni_npu import layers`）与模型显式 import（`NPUVocabParallelEmbedding`、`NPUParallelLMHead`、`NPULogitsProcessor`，其 `register_oot` 装饰器在源码中就是注释状态）。
- **算子替换层**：`NPURMSNorm` 用 `npu_add_rms_norm` 融合「残差相加 + 归一化」，并可选融合 AllGather 与 `npu_dynamic_quant`；`NPUSiluAndMul` 以 `{"x_int8", "pertoken_scale"}` 字典协议贯通 W8A8；`FlashCommLinear` 家族在 vLLM 线性层之上加了权重转置、FRACTAL_NZ 布局、按层 x/y 通信变换与 `npu_prefetch` 预取。
- **logits 计算**：`NPULogitsProcessor._get_logits` 有 TP all-gather 与 DP/local 分片 all_to_all 两条路径；后者先做廉价的 hidden all_gather（补齐到 runner 写入的类属性 `_dp_pad_n`），再用 all_to_all 让每 rank 只持有自己 token 的全词表 logits，把通信大头从 \( V \) 维降到 \( H \) 维。
- **方法论**：新增自定义 NPU 层的三件套（实现 + `layers/__init__.py` 挂 import + mock 式单测），以及「忘了挂 import 就静默失效」这个最常见事故的验证手段。

## 7. 下一步学习建议

- **u3-l5（MTP 投机解码与采样器）**：本讲的 `NPULogitsProcessor` 产出的 logits 正是采样器的输入，MTP draft 层还会复用 `NPUVocabParallelEmbedding`/`NPUParallelLMHead`——下一讲看它们如何被多次调用。
- **u5-l1（模型最佳实践配置）**：本讲多次撞见 `model_extra_config.operator_opt_config`（`omni_disable_npu_add_rms_norm`、`unquant_bmm_nz`、`attn_prefetch`、`use_mhc_fusion_op`）和 `parall_config.ena_dp_lmhead_parallel`，u5-l1 会讲清这张配置表怎么按模型自动加载、怎么用 `CUSTOM_MODEL_CONFIG_PATH` 覆盖。
- **u5-l2（图编译）**：本讲埋了两颗种子——`set_aclgraph_recapture(True)`（NZ 布局变更后要求重捕获图）与 `enable_mhc_multistream` 的 side stream 调度，都将在 ACL Graph 讲义中展开。
- **u8（jointfix W8A8）**：本讲的字典量化协议是量化链路的推理侧接口，u8 讲权重侧如何被量化产出。
- **延伸阅读源码**：`layers/rotary_embedding/` 目录下七八个 `register_oot` 实现是最干净的模仿素材；`tests/unit/layers/st/` 下的同名测试展示了带真实通信的单测写法。
