# u8-l4 量化产物组装与 INT8 服务部署

## 1. 本讲目标

前两讲（u8-l2、u8-l3）我们看清了 jointfix 如何逐层校准、搜索平滑参数并写出 INT8 权重，但 runner 的产物只是散落的 `layer_NNNN.safetensors` 中间文件——**vLLM 并不能直接加载它们**。本讲补上最后一公里：

1. 解释 **compressed-tensors** 这一量化模型「装箱标准」是什么，`config.json` 里 `quantization_config` 每个字段的含义；
2. 精读 `finalize_model`，理解它如何把逐层产物**组装**回原分片布局、生成数据驱动的 `ignore` 列表，并避开 MTP 共享专家的量化陷阱；
3. 逐行 diff **w8a8 模板与 BF16 模板**，理解 INT8 部署到底改了什么、为什么不需要任何 `--quantization` 参数；
4. 完成从 BF16 权重到 INT8 服务的端到端流程，并量化对比显存占用与首 token 延迟。

学完本讲，你就打通了「BF16 权重 → jointfix 量化 → 可部署模型 → 生产服务」的完整链路，jointfix 单元到此收官。

## 2. 前置知识

### 2.1 W8A8 快速回顾（承接 u8-l1 / u8-l3）

- **W8A8** = 权重 8bit + 激活 8bit。权重用 **per-output-channel 静态**量化（离线算好 scale 随权重存储），激活用 **per-token 动态**量化（推理时每个 token 现场算 scale）。
- jointfix 的价值在于：量化前对每个线性层联合搜索平滑参数 \((a,b)\)，把量化难度在激活侧与权重侧之间二维分摊，从而在 INT8 下保住精度。压缩比约 **1.9×**（不是 2×，因为一部分层必须留在 BF16，见下文 ignore 机制）。

两种量化策略的数学形式（对称量化，Scale = 行最大绝对值 / 127）：

权重 per-channel（对权重矩阵 \(W \in \mathbb{R}^{C_{out} \times C_{in}}\) 的每一行）：

\[ s_c = \frac{\max_j |W_{c,j}|}{127}, \qquad Q_{c,j} = \mathrm{clamp}\big(\mathrm{round}(W_{c,j} / s_c),\ -128,\ 127\big) \]

激活 per-token（对输入 \(x \in \mathbb{R}^{T \times d}\) 的每个 token 行）：

\[ s_t = \frac{\max_d |x_{t,d}|}{127} \]

反量化就是 \(Q \cdot s\)，推理时在 NPU 融合算子内部完成（u3-l3 讲过 MoE 回收阶段融合反量化）。

### 2.2 safetensors 分片与索引

大模型权重不会塞进一个文件，而是切成多个 `model-00001-of-000XX.safetensors` 分片，由 `model.safetensors.index.json` 里的 `weight_map` 记录「张量名 → 所在分片文件」。加载器先读索引，再按需打开分片。**finalize 的核心决策就是：复用原模型的分片布局**，而不是发明新布局。

### 2.3 compressed-tensors 是什么

[compressed-tensors](https://github.com/neuralmagic/compressed-tensors) 是一个开源的**量化/稀疏模型格式标准**（含 Python 库与 vLLM 插件）。它的思路是：把「哪些层量化了、几 bit、什么粒度」全部描述性地写进 `config.json` 的 `quantization_config` 字段，加载框架（vLLM）读这份「装箱单」就知道每个 Linear 该用什么反量化核。好处是**部署零参数**——不需要在启动命令里指定量化方式，换模型不换启动脚本。

### 2.4 本讲的三个角色

| 角色 | 文件 | 职责 |
|------|------|------|
| 生产者 | `jointfix`（deploy.py） | 写出符合 compressed-tensors 规范的模型目录 |
| 标准 | compressed-tensors 格式 | `config.json` 里的 `quantization_config` 装箱单 |
| 消费者 | vLLM + omni-npu | 读装箱单，为每个量化的 Linear 换上 INT8 计算核 |

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [tools/quant/jointfix/jointfix/core/deploy.py](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/quant/jointfix/jointfix/core/deploy.py) | 本讲主战场：组装可部署模型、写 `quantization_config` 与 ignore 列表 |
| [tools/quant/jointfix/jointfix/cli.py](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/quant/jointfix/jointfix/cli.py) | `quantize`/`finalize` 两个子命令；skip 名单三级合成；MTP 陷阱修正 |
| [tools/quant/jointfix/jointfix/backends/pangu.py](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/quant/jointfix/jointfix/backends/pangu.py) | pangu 后端：skip 名单（indexer/mhc）、`layer_NNNN.safetensors` 逐层落盘 |
| [tools/quant/jointfix/jointfix/backends/base.py](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/quant/jointfix/jointfix/backends/base.py) | `ModelBackend` 抽象：`skip_patterns()` / `save_quantized()` 接口契约 |
| [tools/quant/jointfix/jointfix/core/primitives.py](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/quant/jointfix/jointfix/core/primitives.py) | `UNIVERSAL_SKIP_PATTERNS`、`should_quantize`、`rtn_quantize`（finalize 的 RTN 兜底用它） |
| [tools/quant/jointfix/docs/quantize_openpangu_w8a8.md](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/quant/jointfix/docs/quantize_openpangu_w8a8.md) | 量化→组装→部署的官方操作手册 |
| [README_INT8.md](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/README_INT8.md) | INT8 权重的 PD 分离部署入口文档 |
| [tools/ansible/92B/omni_infer_server_template_performance1P1D_92B_w8a8_open.yml](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_server_template_performance1P1D_92B_w8a8_open.yml) | INT8 服务部署模板（与 BF16 模板 diff 是 4.3 节的素材） |
| [components/omni-npu/src/omni_npu/layers/quantization/compressed_tensors/compressed_tensors.py](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/layers/quantization/compressed_tensors/compressed_tensors.py) | 消费侧：NPU 版 compressed-tensors 适配器，识别装箱单并分发 INT8 核 |

## 4. 核心概念与源码讲解

### 4.1 compressed-tensors：量化模型的「装箱标准」

#### 4.1.1 概念说明

一个 BF16 模型目录里，`config.json` 只有结构超参；而一个 compressed-tensors 量化模型目录里，`config.json` 会**多出一个 `quantization_config` 字段**，声明性地回答四个问题：

1. **谁来解析**：`quant_method: compressed-tensors`——告诉加载框架用哪套解析器；
2. **量化什么**：`config_groups.group_0.targets: ["Linear"]`——所有 nn.Linear 形状的模块；
3. **怎么量化权重**：`weights` 字典——8bit、对称、per-channel、静态（scale 预先算好）；
4. **谁被豁免**：`ignore` 列表——**必须留在 BF16 的模块名单**（embedding、router gate、indexer、共享专家……）。

`ignore` 不是可有可无的备注：vLLM 加载时逐层查这张表，命中者走未量化路径、读 BF16 权重；未命中者走 INT8 路径、读 int8 权重 + `weight_scale`。**如果某个没量化的层漏进了 targets 而不在 ignore 里，加载时找不到 `weight_scale` 就会直接报错**——这正是 ignore 列表必须绝对准确的原因。

#### 4.1.2 核心流程

部署链路上，一份量化模型被识别的过程：

```text
config.json 的 quantization_config
        │  quant_method == "compressed-tensors"
        ▼
vLLM 查注册表找解析器
        │  omni-npu 的 override_quantization_method 拦截：
        │  NPU 环境下把 "compressed-tensors" 改写为 "npu-compressed-tensors"
        ▼
NPUCompressedTensorsConfig 解析 config_groups
        │  逐层 get_scheme：名字命中 ignore → None（BF16 直通）
        │                    否则 → NPUCompressedTensorsW8A8Int8 方案
        ▼
模型构建时每个 Linear / FusedMoE 换上对应 INT8 计算方法
```

关键点：**部署命令里没有任何 `--quantization` 参数**——整条链路由 `config.json` 单向驱动，换 BF16 权重与 INT8 权重只需要改 `MODEL_PATH` 指向。

#### 4.1.3 源码精读

**生产侧：`build_quantization_config` 构造装箱单。**

[tools/quant/jointfix/jointfix/core/deploy.py:L27-L58](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/quant/jointfix/jointfix/core/deploy.py#L27-L58) 是装箱单的**唯一生成点**，逐字段解读：

| 字段 | 值 | 含义 |
|------|-----|------|
| `quant_method` | `"compressed-tensors"` | 格式归属，vLLM 据此选解析器 |
| `quantize` | `"w8a8_dynamic"` | 量化方案名：W8A8 + 动态激活量化 |
| `format` | `"int-quantized"` | 整数量化（区别于 fp8 等浮点量化） |
| `quantization_status` | `"compressed"` | 已是压缩终态（区别于训练中 `quantized_compressed`） |
| `targets` | `["Linear"]` | 按模块类型圈定量化对象 |
| `weights` | 8bit / symmetric / **channel** / `dynamic: False` | 权重静态 per-channel 量化，`observer: minmax` 表示 scale 取行最大绝对值 |
| `input_activations` | 8bit / symmetric / **token** / `dynamic: True` | 激活动态 per-token 量化，`observer: None`（不落盘统计） |
| `ignore` | 调用方传入 | 不量化的模块基名列表，4.2 节讲它如何被**推导**出来 |

其中 weights 与 input_activations 两个字典的具体构造在 [deploy.py:L39-L50](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/quant/jointfix/jointfix/core/deploy.py#L39-L50)，与上表一一对应；`global_compression_ratio` 和 `kv_cache_scheme` 两个参数在本调用链里传 `None`（[deploy.py:L56-L57](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/quant/jointfix/jointfix/core/deploy.py#L56-L57)），即权重压缩与 KV Cache 压缩是两件独立的事。

**消费侧：omni-npu 如何接住这张装箱单。**

omni-npu 在 vLLM 的 compressed-tensors 实现之上包了一层 NPU 适配，注册了 `npu-compressed-tensors` 配置类（[compressed_tensors.py:L35-L44](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/layers/quantization/compressed_tensors/compressed_tensors.py#L35-L44)），并从同包导入 W8A8 的 Linear 方案与 MoE 方案（[compressed_tensors.py:L26-L32](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/layers/quantization/compressed_tensors/compressed_tensors.py#L26-L32)：`NPUCompressedTensorsW8A8Int8` 与 `NPUCompressedTensorsW8A8Int8MoEMethod`）。

最关键的桥接在 [compressed_tensors.py:L181-L187](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/layers/quantization/compressed_tensors/compressed_tensors.py#L181-L187)：`override_quantization_method` 发现 NPU 可用且 `quant_method == 'compressed-tensors'` 时，把它**改写**为自己的注册名 `npu-compressed-tensors`。于是 jointfix 写出的标准格式在 GPU 机器上走 vLLM 原生路径、在昇腾机器上无缝切到 NPU 适配路径，产物本身保持厂商中立。

分发时逐层查 ignore 的逻辑在 [compressed_tensors.py:L72-L80](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/layers/quantization/compressed_tensors/compressed_tensors.py#L72-L80)：`get_scheme` 先用 vLLM 的 `should_ignore_layer` 检查层名，命中即返回 `None`（不量化），随后才按 target 匹配 W8A8 方案。这与 4.2 节生产侧「数据驱动 ignore」正好是一枚硬币的两面。

官方文档对「无需 `--quantization` 参数」的说明见 [tools/quant/jointfix/docs/quantize_openpangu_w8a8.md:L100](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/quant/jointfix/docs/quantize_openpangu_w8a8.md#L100)。

#### 4.1.4 代码实践

**实践目标**：不动任何模型，纯离线感受「装箱单」的结构与 ignore 的语义。

**操作步骤**（示例代码，任何装了 Python 的机器可跑，无需 NPU / torch / 权重——`build_quantization_config` 只拼 dict）：

```python
# 示例代码：inspect_qconfig.py
from jointfix.core.deploy import build_quantization_config

cfg = build_quantization_config(
    ignore_list=["lm_head", "model.embed_tokens",
                 "model.layers.3.mlp.gate", "model.layers.3.indexer.wq_b"],
)
import json
print(json.dumps(cfg["config_groups"]["group_0"], indent=2))
print("ignore =", cfg["ignore"])
print("quant_method =", cfg["quant_method"], "| quantize =", cfg["quantize"])
```

**需要观察的现象**：

1. `targets` 里只有 `"Linear"`——这意味着 ignore 才是唯一的豁免通道；
2. `weights.dynamic` 为 `False` 而 `input_activations.dynamic` 为 `True`，正好对应「权重静态、激活动态」；
3. ignore 里放的是**模块基名**（`lm_head` 而非 `lm_head.weight`）。

**预期结果**：打印出一个 group_0 字典，含 targets/weights/input_activations 三块；ignore 原样回显。若你在没有装 jointfix 的环境，也可以照 [deploy.py:L30-L58](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/quant/jointfix/jointfix/core/deploy.py#L30-L58) 手抄这份 dict 做同样的事。具体打印格式待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `input_activations` 的 `strategy` 从 `"token"` 改成 `"channel"`，部署侧会发生什么？

**参考答案**：部署侧会认为激活按通道静态量化，进而要求配置里有校准得到的激活 scale（observer 产物）；而 jointfix 根本没有落盘任何激活统计，加载时要么报缺 scale、要么静默走出错的反量化路径。per-token 动态策略的意义恰恰是**无需任何校准产物**，推理时现场对每个 token 求 amax。

**练习 2**：为什么 `targets` 是 `["Linear"]` 这种模块类型，而不是具体层名列表？

**参考答案**：MoE 模型有 256 个路由专家 × 每专家 3 个线性层，逐层枚举既冗长又易漏。按类型圈定 + ignore 反向豁免，是「白名单粗、黑名单准」的组合：类型匹配交给 vLLM 的 `find_matched_target`/`should_ignore_layer`（含 fused 模块映射），豁免名单由 finalize 从数据推导（见 4.2.3）。

**练习 3**：`quantization_status: compressed` 与 `quantized_compressed` 有何区别？

**参考答案**：compressed 表示「量化已经完成、推理用」；quantized_compressed 用于 QAT 场景（量化感知训练的中间状态）。这里产物是 PTQ 终态，所以写 compressed。装错状态可能导致加载器选择带假量化节点的路径而非纯反量化路径。

### 4.2 部署组装：finalize_model 把逐层产物拼回可加载模型

#### 4.2.1 概念说明

回顾 u8-l2：runner 每处理完一层，就让后端 `save_quantized` 把该层张量写进 `layer_NNNN.safetensors`（pangu 后端实现见 [pangu.py:L169-L179](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/quant/jointfix/jointfix/backends/pangu.py#L169-L179)，注释明确说「部署格式的重组装是后续 finalization 步骤」；接口契约在 [base.py:L67-L70](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/quant/jointfix/jointfix/backends/base.py#L67-L70)）。这种「逐层落盘、最后组装」的两段式有三个动机：

1. **断点续跑**：每层独立成文件，重跑时跳过已完成的层；
2. **解耦**：runner 不必关心 vLLM 的分片布局与 config 规范；
3. **可检查**：中间产物能单独做层级别正确性验证。

`finalize_model` 做的就是把这些散件按**原模型的分片布局**拼回去：原第 5 个分片里有哪些层，输出目录的第 5 个分片里就是这些层的量化版本。这一函数要解决四个子问题：未校准层怎么办（RTN 兜底）、非层内张量怎么办（透传）、ignore 怎么求（数据驱动）、辅助文件怎么办（拷贝）。

#### 4.2.2 核心流程

`finalize_model` 的主循环伪代码（对应 [deploy.py:L72-L177](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/quant/jointfix/jointfix/core/deploy.py#L72-L177)）：

```text
输入: orig(BF16 模型目录), quant_dir(layer_*.safetensors 目录), skip_patterns

1. layer_map = 扫描 quant_dir 全部 layer_*.safetensors
       → {张量名: 所在层文件}           # 校准覆盖面
2. orig_wmap = 读原模型 index.json 的 weight_map
       by_shard = 按分片文件名把张量名分组   # 复用原布局
3. for 每个 shard:
     a. 若 rtn_uncalibrated=False 且该 shard 无任何校准张量 → 跳过（快速测试模式）
     b. for 该 shard 每个张量名:
          - 在 layer_map 中 → 直接取 int8 权重；有配对 .weight_scale 一并写入
          - 不在 → 读原 BF16 张量:
              * should_quantize() 为真 → rtn_quantize() 现场量化（int8 + scale）
              * 否则 → BF16 原样透传
     c. save_file 写出同名分片；登记 new_index
4. 拷贝辅助文件（config.json / tokenizer / modeling *.py），不覆盖已存在的
5. 写新的 model.safetensors.index.json
6. ignore = 所有 2D .weight 模块基名 − 实际带 .weight_scale 的模块基名
   config.json 注入 quantization_config(ignore)
```

注意第 6 步的集合差：**「看起来是 Linear」减去「真的被量化」就是「必须豁免」**。

#### 4.2.3 源码精读

**（1）skip 名单的三级合成。**`should_quantize` 是「谁不量化」的裁判：只量化名字以 `.weight` 结尾、二维、且不命中任何 skip 模式的张量（[primitives.py:L34-L47](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/quant/jointfix/jointfix/core/primitives.py#L34-L47)）。skip 名单分三级：

- **通用级**：[primitives.py:L23-L31](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/quant/jointfix/jointfix/core/primitives.py#L23-L31) 的 `UNIVERSAL_SKIP_PATTERNS`——embedding、`lm_head`、MLA 低秩投影（`q_a_proj`/`kv_a_proj`/`kv_b_proj`）、router gate（`mlp.gate.` 带尾部点，避免误伤 `gate_proj`）；
- **模型级**：pangu 后端追加 indexer 三件套与 mhc phi（[pangu.py:L22-L27](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/quant/jointfix/jointfix/backends/pangu.py#L22-L27)），并由 `skip_patterns()` 拼接返回（[pangu.py:L91-L92](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/quant/jointfix/jointfix/backends/pangu.py#L91-L92)）。抽象契约要求每个后端都必须提供这份名单（[base.py:L47-L51](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/quant/jointfix/jointfix/backends/base.py#L47-L51)）；
- **方法级**：`--skip-shared-experts` 追加 `mlp.shared_experts`（见下）。

三级合成的胶水在 cli.py 的 `_deploy_skip_patterns`（[cli.py:L76-L85](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/quant/jointfix/jointfix/cli.py#L76-L85)）。

**（2）RTN 兜底。**未校准的可量化权重走 [primitives.py:L84-L89](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/quant/jointfix/jointfix/core/primitives.py#L84-L89) 的 `rtn_quantize`：per-row amax / 127 得 scale，取整 clamp 成 int8，scale 存 bf16。这就是 2.1 节的公式。调用点在 [deploy.py:L137-L140](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/quant/jointfix/jointfix/core/deploy.py#L137-L140)，不满足量化条件的张量走 [deploy.py:L142](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/quant/jointfix/jointfix/core/deploy.py#L142) 的 BF16 透传。

**（3）数据驱动的 ignore（本函数最精彩的设计）。**[deploy.py:L165-L170](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/quant/jointfix/jointfix/core/deploy.py#L165-L170) 不用任何名字模式去「猜」ignore，而是**盘点实际写出的张量**：凡是产生了 `.weight_scale` 兄弟张量的模块记入 `quantized_bases`（收集逻辑在 [deploy.py:L145-L150](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/quant/jointfix/jointfix/core/deploy.py#L145-L150)），凡是二维 `.weight` 的模块记入 `linear_bases`，两者相减即 ignore。源码注释直说了动机：旧版按名字模式生成 ignore，**漏掉了方法级 skip（如 `--skip-shared-experts`），导致 vLLM 加载失败**——数据驱动后，后端 skip、方法 skip、普通透传三种「没量化」被统一覆盖。ignore 最终随 config 写盘（[deploy.py:L171-L175](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/quant/jointfix/jointfix/core/deploy.py#L171-L175)）。

**（4）MTP 共享专家陷阱。**校准循环只跑 `range(num_hidden_layers)`，**MTP 层（层号 ≥ num_hidden_layers，见 u3-l5）从不被校准**。于是 finalize 的 RTN 兜底会把 MTP 层里的 `mlp.shared_experts` 量化掉——与 quantize 阶段 `--skip-shared-experts` 的意图相悖。修复方式见 [cli.py:L76-L85](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/quant/jointfix/jointfix/cli.py#L76-L85)：finalize 侧若带 `--skip-shared-experts`（[cli.py:L56-L59](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/quant/jointfix/jointfix/cli.py#L56-L59)），把 `mlp.shared_experts` 也加进 skip 名单。**因此 finalize 的该标志必须与 quantize 时一致**，否则产物不一致。

**（5）一步还是两步。**`jointfix quantize` 默认在量化完成后立即原地组装（[cli.py:L123-L132](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/quant/jointfix/jointfix/cli.py#L123-L132)，`--no-finalize` 可停在中间产物）；`jointfix finalize` 子命令（[cli.py:L47-L61](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/quant/jointfix/jointfix/cli.py#L47-L61)）支持事后组装，其中 `--calibrated-only`（[cli.py:L53-L55](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/quant/jointfix/jointfix/cli.py#L53-L55)）只写含校准张量的分片、跳过对未校准层的海量 RTN——92B 模型全量 RTN 是「几十 GB 的 CPU 工作」（[deploy.py:L81-L86](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/quant/jointfix/jointfix/core/deploy.py#L81-L86) docstring 原话），做格式验证时务必加它。

**（6）辅助文件与索引。**[deploy.py:L153-L159](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/quant/jointfix/jointfix/core/deploy.py#L153-L159) 拷贝 tokenizer、建模代码等（`not dst.exists()` 保证不覆盖已写的分片），[deploy.py:L161-L163](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/quant/jointfix/jointfix/core/deploy.py#L161-L163) 用累计的 `new_index` 重写 `model.safetensors.index.json`。原分片布局的读取兼容「多分片 + index」与「单文件」两种形态（[deploy.py:L61-L69](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/quant/jointfix/jointfix/core/deploy.py#L61-L69)）。

#### 4.2.4 代码实践

**实践目标**：不用 NPU、不用真模型，在 CPU 上构造一个**微型假模型**跑通 `finalize_model`，亲眼验证 ignore 列表的数据驱动推导。

**操作步骤**（全部为示例代码；前置：`cd tools/quant/jointfix && pip install -e .`，CPU 环境可直接装，NPU 环境务必用 README 的 `--no-deps` 方式）：

```python
# 示例代码：step1_make_tiny.py —— 造一个 5 个张量的微型 BF16 模型
import json, torch
from pathlib import Path
from safetensors.torch import save_file

root = Path("tiny_bf16"); root.mkdir(exist_ok=True)
t = {
  # 2D 但命中 skip：embed / lm_head / mlp.gate.
  "model.embed_tokens.weight":            torch.randn(100, 16, dtype=torch.bfloat16),
  "lm_head.weight":                       torch.randn(100, 16, dtype=torch.bfloat16),
  "model.layers.0.mlp.gate.weight":       torch.randn(4, 16, dtype=torch.bfloat16),
  # 正常应被量化的层 0 线性层（将由"校准产物"覆盖）
  "model.layers.0.self_attn.q_b_proj.weight": torch.randn(32, 16, dtype=torch.bfloat16),
  # 层 1 的线性层：没有任何 layer 文件覆盖 → 走 RTN 兜底
  "model.layers.1.mlp.down_proj.weight":  torch.randn(16, 32, dtype=torch.bfloat16),
}
save_file(t, str(root / "model.safetensors"))
(root / "config.json").write_text(json.dumps(
    {"architectures": ["TinyForCausalLM"], "hidden_size": 16}))
```

```python
# 示例代码：step2_make_layer.py —— 模拟 runner 写出的 layer_0000.safetensors
import torch
from pathlib import Path
from safetensors.torch import save_file
from jointfix.core.primitives import rtn_quantize

qd = Path("tiny_quant"); qd.mkdir(exist_ok=True)
q, s = rtn_quantize(torch.randn(32, 16))     # 与 runner 相同的 int8+scale 形态
save_file({"model.layers.0.self_attn.q_b_proj.weight": q,
           "model.layers.0.self_attn.q_b_proj.weight_scale": s},
          str(qd / "layer_0000.safetensors"))
```

```python
# 示例代码：step3_finalize.py —— 组装并检查产物
import json
from jointfix.core.deploy import finalize_model

out = finalize_model("tiny_bf16", "tiny_quant", "tiny_w8a8",
                     skip_patterns=["embed", "lm_head", "mlp.gate."],
                     rtn_uncalibrated=True)
qc = json.loads((out / "config.json").read_text())["quantization_config"]
print("ignore =", qc["ignore"])
wmap = json.loads((out / "model.safetensors.index.json").read_text())["weight_map"]
print("tensors =", sorted(wmap))
```

**需要观察的现象**：

1. 输出目录 `tiny_w8a8/` 出现 `model.safetensors`、`model.safetensors.index.json`、带 `quantization_config` 的 `config.json`；
2. `q_b_proj.weight` 变成 int8 且多出 `q_b_proj.weight_scale`；`down_proj.weight` 被 RTN 兜底同样带 scale；
3. `embed_tokens / lm_head / mlp.gate` 三者**没有** scale 兄弟张量（BF16 透传）。

**预期结果**（依源码逻辑推导，具体打印格式待本地验证）：

```text
ignore = ['lm_head', 'model.embed_tokens', 'model.layers.0.mlp.gate']
tensors = ['lm_head.weight', 'model.embed_tokens.weight',
           'model.layers.0.mlp.gate.weight',
           'model.layers.0.self_attn.q_b_proj.weight',
           'model.layers.0.self_attn.q_b_proj.weight_scale',
           'model.layers.1.mlp.down_proj.weight',
           'model.layers.1.mlp.down_proj.weight_scale']
```

ignore 恰好是「2D .weight 全集 − 带 scale 者」：`down_proj` 虽未校准但被 RTN 量化，所以**不进** ignore——这验证了 4.2.3（3）的集合差逻辑。

#### 4.2.5 小练习与答案

**练习 1**：把 step3 的 `rtn_uncalibrated` 改为 `False` 再跑，ignore 会变成什么？为什么？

**参考答案**：`down_proj` 不再被 RTN，保持 BF16 透传，但它仍是二维 `.weight`，于是进入 `linear_bases` 且不在 `quantized_bases`，ignore 变成四项（多出 `model.layers.1.mlp.down_proj`）。这正是 `--calibrated-only` 模式的语义：只保证「有校准层的分片」正确，未覆盖层的豁免状态是测试性产物，不可部署。

**练习 2**：为什么 `finalize_model` 里 layer 文件中的 indexer 权重可以直接透传，而不用查 skip 名单？

**参考答案**：skip 名单的过滤发生在两个入口：quantize 阶段决定「谁被量化」，finalize 的 RTN 分支决定「未覆盖的谁补量化」。而 layer_NNNN.safetensors 里已经是 runner 的**终态产物**——量化的带 scale、跳过的以 BF16 存于同一文件，finalize 对它们只做搬运（[deploy.py:L129-L134](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/quant/jointfix/jointfix/core/deploy.py#L129-L134)），无权也不需要再判 skip。

**练习 3**：如果不小心在 finalize 时漏了 `--skip-shared-experts`（quantize 时加过），哪一层最先出问题？表现是什么？

**参考答案**：MTP 层（层号 ≥ `num_hidden_layers`）的 `mlp.shared_experts.*` 最先出问题——它们从未被校准（校准循环只扫主层），会被 RTN 兜底量化成 int8+scale，从而**不在 ignore 里**；而 monolith 语义要求共享专家保持 BF16。产物层面的征兆是：量化 trace 与最终 config 对共享专家的记载不一致；推理层面则表现为投机解码输出质量劣化（共享专家每个 token 必经，误差全局累积，u8-l3 讲过原因）。

### 4.3 INT8 服务部署：w8a8 模板与 BF16 模板逐行 diff

#### 4.3.1 概念说明

有了可加载的 INT8 模型目录，部署侧的工作出人意料地少：**w8a8 模板与 BF16 模板的骨架完全一致**（u1-l4 讲过的 run_docker → run_server → run_proxy 三段式原样适用），模板里也**没有** `--quantization` 之类的开关——把 environment 里的 `MODEL_PATH` 指向 finalize 输出目录，剩下的交给 `config.json` 自动识别。

真正的差异是**一组因「显存富余」而放开的容量参数**：权重从 BF16 压到 INT8 后，每卡省出的 HBM 被兑换成更大的 prefill 批量、更高的并发与 INT8 KV Cache。理解这点后，w8a8 模板的每处改动都不再需要死记。

#### 4.3.2 核心流程

INT8 服务从量化产物到可用服务的全流程：

```text
finalize 输出目录（W8A8 权重）
        │  放到 P/D 各机器相同路径
        ▼
改 README_INT8 指定的两个文件
        ├── inventory：P/D/C 三组机器 IP（与 BF16 部署共用同一份 inventory）
        └── w8a8 模板 environment：LOG_PATH / MODEL_PATH / DOCKER_IMAGE_ID / 容器名
        ▼
--tags run_docker          # 建 P/D/C 三容器（NPU 三要素：设备透传/驱动挂载/--net=host）
        ▼
--tags run_server,run_proxy # P 侧 TP16 kv_producer + D 侧 16×DP kv_consumer + C 侧 nginx
        ▼
curl 向 C 节点 7000 端口发请求验证（model 字段 = SERVED_MODEL_NAME）
```

#### 4.3.3 源码精读

**（1）部署入口与必改项。**[README_INT8.md:L54-L59](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/README_INT8.md#L54-L59) 指明 1P1D 的两个文件：inventory 与 w8a8 模板；[README_INT8.md:L77-L100](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/README_INT8.md#L77-L100) 列出 environment 必改项（`LOG_PATH`、`MODEL_PATH`、`DOCKER_IMAGE_ID`、三个容器名；`DECODE_TENSOR_PARALLEL_SIZE: "1"` 对应 D 侧 DP 形态）。量化工具的衔接点在 [README_INT8.md:L130-L132](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/README_INT8.md#L130-L132)：INT8 权重的生成统一指向 jointfix README（即 u8-l1～u8-l3 的内容）。启动与拉服务的命令分别在 [README_INT8.md:L106-L115](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/README_INT8.md#L106-L115) 与 [README_INT8.md:L134-L145](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/README_INT8.md#L134-L145)；验证请求的 curl 模板在 [README_INT8.md:L160-L184](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/README_INT8.md#L160-L184)（`model` 必须等于 `SERVED_MODEL_NAME`，默认 `openPangu-2.0-Flash`）。

**（2）两模板逐行 diff。**对 [omni_infer_server_template_performance1P1D_92B_bf16_open.yml](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_server_template_performance1P1D_92B_bf16_open.yml) 与 [omni_infer_server_template_performance1P1D_92B_w8a8_open.yml](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_server_template_performance1P1D_92B_w8a8_open.yml) 做 diff，**全部差异只有 8 行**，集中在 4 个位置：

| 位置（w8a8 模板行号） | BF16 | W8A8 | 解读 |
|---|---|---|---|
| P 侧 `EXTRA_ARGS`（[L92](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_server_template_performance1P1D_92B_w8a8_open.yml#L92)） | `--max-num-batched-tokens 16384 --max-num-seqs 4` | `32768` / `12` | 权重省约一半显存 → prefill 单步 token 上限翻倍、并发序列 ×3 |
| P 侧（同上） | 无 | `--kv-cache-dtype li_int8_ds_mla` | KV Cache 也降为 INT8 存储进一步省显存（与权重量化独立） |
| P 侧 `GPU_UTIL`（[L95](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_server_template_performance1P1D_92B_w8a8_open.yml#L95)） | `0.8` | `0.85` | 允许 vLLM 占用更高显存水位 |
| D 侧 `EXTRA_ARGS`（[L202](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_server_template_performance1P1D_92B_w8a8_open.yml#L202)） | `--max-num-seqs 3`，capture/compile sizes `12`，无 KV dtype | `4`，`16`，`li_int8_ds_mla` | decode 并发 +1；ACL Graph 捕获批尺寸随之对齐到 16（u5-l2 讲过图捕获尺寸须覆盖批结构） |
| proxy 上限（[L290-L291](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_server_template_performance1P1D_92B_w8a8_open.yml#L290-L291)） | `prefill 4 / decode 3` | `12 / 4` | 转发侧限流与引擎侧并发同步放开，否则 proxy 会先掐住流量 |

同样值得注意的是**没变的部分**，它们澄清了两个常见误解：

- 两模板都保留 `--dtype bfloat16`（w8a8 模板 P 侧 [L92](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_server_template_performance1P1D_92B_w8a8_open.yml#L92)、D 侧 [L202](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_server_template_performance1P1D_92B_w8a8_open.yml#L202)）——`--dtype` 指定的是**计算与非线性层的精度**，W8A8 只作用于被量化的 Linear 权重，二者不冲突（u1-l3 也提过「w8a8 权重计算精度仍为 bfloat16」）；
- `CUSTOM_MODEL_CONFIG_PATH` 仍指向 **bf16** 命名的最佳实践 json（P 侧 [L83](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_server_template_performance1P1D_92B_w8a8_open.yml#L83)、D 侧 [L172](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_server_template_performance1P1D_92B_w8a8_open.yml#L172)）——u5-l1 讲过这套配置按「模型+硬件+精度+形态」路由，精度维度看的就是计算精度而非权重量化格式。

**（3）量化产物的官方验收方式。**[quantize_openpangu_w8a8.md:L73-L100](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/quant/jointfix/docs/quantize_openpangu_w8a8.md#L73-L100) 给出 finalize 后 `config.json` 应有的 `quantization_config` 骨架，并强调 vLLM 自动识别、无需 `--quantization`——把它当作 4.2.4 实践产物的「标准答案」来对照。权重来源侧的完整命令（Step 1 quantize / Step 2 finalize）在 [quantize_openpangu_w8a8.md:L24-L55](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/quant/jointfix/docs/quantize_openpangu_w8a8.md#L24-L55)。

#### 4.3.4 代码实践

**实践目标**：在两台 A3 机器上完成「BF16 → INT8」的同拓扑对照实验，量化收益用数字说话。

**操作步骤**：

1. **准备两份权重**（16 卡 NPU 机器上，命令出自 [quantize_openpangu_w8a8.md:L24-L55](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/quant/jointfix/docs/quantize_openpangu_w8a8.md#L24-L55)）：
   ```bash
   source /usr/local/Ascend/ascend-toolkit/set_env.sh
   export ASCEND_RT_VISIBLE_DEVICES=0,1,2,3,4,5,6,7,8,9,10,11,12,13,14,15
   jointfix quantize --backend pangu --method jointfix \
       --model /data/openPangu-2.0-Flash-bf16 \
       --output /data/omni_w8a8_mid \
       --calib-data examples/data/wikitext_train.parquet \
       --n-samples 32 --seq-len 1024 --num-iterations 2 --iter-ab-tol 0.05 \
       --num-devices 16 --device npu \
       --objective output-recon --write-quant gptq --skip-shared-experts
   # 默认一步到底；若用了 --no-finalize，再跑：
   jointfix finalize --model /data/openPangu-2.0-Flash-bf16 \
       --quantized /data/omni_w8a8_mid --output /data/openPangu-2.0-Flash-w8a8 \
       --skip-shared-experts          # 必须与 quantize 时一致（4.2.3 (4)）
   ```
2. **验收产物**：`python -c "import json;print(json.load(open('/data/openPangu-2.0-Flash-w8a8/config.json'))['quantization_config']['ignore'])"`，确认 indexer/mhc/gate/shared_experts 各类都在；再抽查一个分片里 int8 权重确有 `weight_scale` 兄弟张量。
3. **BF16 基线**：按 u1-l4 流程用 bf16 模板拉起服务，就绪后 `npu-smi info` 记录 P 机器 16 卡 HBM 占用，并 `curl -N` 发一个 `stream=true` 的长请求记录首块到达时间。
4. **切换 INT8**：停服后只改 w8a8 模板 environment 的 `MODEL_PATH=/data/openPangu-2.0-Flash-w8a8`，用 [README_INT8.md:L110](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/README_INT8.md#L110) 与 [L140](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/README_INT8.md#L140) 的命令依次 `--tags run_docker`、`--tags run_server,run_proxy`，重复同样的测量。

**需要观察的现象**：

- INT8 侧 P 节点 HBM 占用明显下降，server 日志可用 KV Cache 容量增大（具体日志字段待本地验证）；
- 同一请求的 TTFT：可对比 omni-proxy 访问日志的 `ttft` 字段（u6-l1 讲过十阶段埋点），或 `curl -N` 手测首块时间；
- 注意对照要公平：BF16 模板 `max-num-seqs 4` vs w8a8 模板 `12`，批量参数不同，TTFT 差异混合了「权重变小」与「并发放开」两个因素，报告中应分开归因。

**预期结果**：权重显存约降一半、可承载并发显著提高；单请求 TTFT 在低并发下差异不大（prefill 是算力瓶颈），高并发下 INT8 因批量上限提高而更稳。具体数值待本地验证（取决于机器型号与负载）。

#### 4.3.5 小练习与答案

**练习 1**：部署 INT8 服务时，启动命令里完全没提「int8」，vLLM 是怎么知道要按 W8A8 加载的？

**参考答案**：三步：读 `config.json` 的 `quantization_config`，`quant_method: compressed-tensors` 告诉 vLLM 用 compressed-tensors 解析器；omni-npu 的 `override_quantization_method`（[compressed_tensors.py:L181-L187](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/layers/quantization/compressed_tensors/compressed_tensors.py#L181-L187)）在 NPU 上把它改写到 NPU 适配配置类；随后逐层 `get_scheme`，ignore 之外的所有 Linear 领取 `NPUCompressedTensorsW8A8Int8` 方案。BF16 模型的 config 没有该字段，自然走未量化路径——所以换权重只改 `MODEL_PATH`。

**练习 2**：w8a8 模板的 `--kv-cache-dtype li_int8_ds_mla` 与 jointfix 的 W8A8 是一回事吗？

**参考答案**：不是。W8A8 量化**权重**（离线 PTQ，scale 落盘）；`--kv-cache-dtype` 是**运行时 KV Cache 的存储精度**（在线降精度，作用于 DSA/MLA 的 latent KV），由 vLLM 启动参数控制。二者机制独立、都省显存，w8a8 模板把两者一起开，是为了把省下的显存全部兑换成批量与并发；BF16 模板不开 KV 量化，纯粹是因为显存预算不允许它再放大批量。

**练习 3**：为什么 proxy 侧的 `--omni-proxy-prefill-max-num-seqs` 必须跟着引擎侧 `--max-num-seqs` 一起改？

**参考答案**：proxy 是流控上游：它限制同时转发给 prefill/decode 上游的请求数。若引擎侧放大到 12 而 proxy 仍限 4，多余并发会在 proxy 排队，引擎的容量白放；反之 proxy 放开而引擎没放大，请求会堆在引擎队列里推高时延。两侧参数是同一容量预算在两层的表达，必须同步（diff 中 [L290-L291](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_server_template_performance1P1D_92B_w8a8_open.yml#L290-L291) 与引擎侧参数成对变化正是这个原因）。

## 5. 综合实践

**综合任务：产出一份《92B INT8 上线验收单》**，把本讲三个模块串成一条流水线。前提：两台 A3 机器 + BF16 权重 + 16 卡 NPU 量化机。

1. **量化段**（4.2）：跑 `jointfix quantize`（参数照 4.3.4 步骤 1），先抽两层做小规模验证——用 `--start-layer 0 --end-layer 2 --no-finalize` 只量化 2 层，再 `jointfix finalize --calibrated-only` 快速检查 config.json 骨架（省掉几十 GB 的全量 RTN）；确认无误后删中间目录重跑全量。
2. **验收段**（4.1）：写一个 10 行的 Python 验收脚本，断言三件事——`quant_method` 为 `compressed-tensors`；`weights.strategy == "channel"` 且 `input_activations.strategy == "token"`；ignore 中能找到 `indexer.`、`mhc_module.phi`、`mlp.gate`、`shared_experts` 四类条目（若 quantize 时带了 skip 标志）。任一断言失败即停止上线。
3. **部署段**（4.3）：inventory 复用 BF16 实验的那份，w8a8 模板只改 `MODEL_PATH` 指向新目录，依次 `run_docker`、`run_server,run_proxy`，以 `server_0.log` 出现 `Application startup complete` 为就绪判据（u1-l5）。
4. **对照段**：同请求、同并发（分别测 1/4/12 并发）记录 BF16 与 INT8 的 HBM 占用与 TTFT（proxy 访问日志 `ttft` 字段），输出一张对比表，并对每项差异标注归因（权重体积 / 批量放开 / KV 量化）。
5. **回退段**：写明回退动作——停服、`MODEL_PATH` 改回 BF16 目录、重跑 `run_server,run_proxy` 即可；量化目录与中间目录分开保存，方便重跑 finalize 试验不同 skip 配置。

产出物：验收脚本、对比表、回退步骤，三者合入团队部署文档。

## 6. 本讲小结

- **compressed-tensors 是装箱单**：`config.json` 的 `quantization_config` 声明式描述「量化谁、几 bit、什么粒度、谁豁免」；`ignore` 列表是豁免通道，漏一项就会加载失败。omni-npu 通过 `override_quantization_method` 把标准格式无缝改写到 NPU 适配路径，部署命令零量化参数。
- **`finalize_model` 复用原分片布局**：逐层 `layer_NNNN.safetensors` 按 `weight_map` 归位，未校准的可量化权重 RTN 兜底，其余 BF16 透传，最后重写索引并注入 `quantization_config`。
- **ignore 是推导出来的不是写出来的**：「二维 `.weight` 全集 − 实际带 `weight_scale` 者」的数据驱动集合差，天然覆盖后端 skip、方法 skip 与透传三种情况，修复了旧版名字模式的漏报。
- **finalize 与 quantize 的 skip 标志必须一致**：MTP 层从不被校准，漏带 `--skip-shared-experts` 会让它的共享专家被 RTN 悄悄量化。
- **w8a8 与 BF16 模板仅差 8 行**：全部是「显存富余 → 容量放开」的兑换（批量、并发、GPU 水位、INT8 KV Cache、图捕获尺寸、proxy 限流），`--dtype bfloat16` 与 bf16 命名的最佳实践配置都不变。

## 7. 下一步学习建议

本讲结束后 jointfix 单元（u8）完结，两条去路：

1. **向上走综合实战**：u10-l4「生产综合实战：505B 全特性部署方案设计」——把本讲的 INT8 部署与 u7 的 OmniCache、u6 的分组调度组合进 4P81D16 大规模拓扑，做一份完整生产方案；其中「从 OmniCache 切换前释放大页内存」的注意事项（u7-l3）与本讲的回退段直接相关。
2. **横向补消费侧细节**：如果想深究 INT8 权重在 NPU 上到底怎么算，回读 u3-l3（MoE 三段式前向中量化方法只替换中间段、回收阶段融合反量化）与 omni-npu 源码 [components/omni-npu/src/omni_npu/layers/quantization/compressed_tensors/schemes/compressed_tensors_w8a8_int8.py](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/layers/quantization/compressed_tensors/schemes/compressed_tensors_w8a8_int8.py)，对照本讲的装箱单看每个字段落到哪个执行分支。
