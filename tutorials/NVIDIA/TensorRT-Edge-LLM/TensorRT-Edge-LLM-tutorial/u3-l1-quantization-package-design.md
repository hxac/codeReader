# 量化包设计与配方

## 1. 本讲目标

本讲是「量化（u3）」单元的第一篇。学完后你应当能够：

1. 说清 `tensorrt_edgellm.quantization` 这个包**为什么必须和 ONNX 导出解耦**，以及它产出的唯一东西是什么。
2. 读懂 `quantization_configs.py` 的「配方（recipe）」结构，知道一次量化由 backbone / lm_head / kv_cache / visual / audio / cp 六个轴组成，并能说出各轴支持哪些方法。
3. 跟着 `quantize.py` 的 `quantize_and_export()` 走完一遍 **load → quant → save** 主流程，看懂模型加载、校准（calibration）、检查点写出与 sidecar 拷贝分别发生在哪里。
4. 了解量化对 draft 模型（EAGLE3 / DFlash / MTP）的支持方式。
5. 独立写出一个 NVFP4 量化配方的字段，并描述量化后检查点与原始 HF 检查点在目录结构上的异同。

> 承接：u2-l1 讲的是**导出侧**如何用 `QuantConfig` 读取量化元数据（`hf_quant_config.json` / 内嵌 `quantization_config`）。本讲讲的是这些元数据**从哪里被生产出来**——即量化包如何写出导出侧要读的那份检查点。两者是「生产者 ↔ 消费者」关系。

## 2. 前置知识

### 2.1 为什么边缘设备要量化

大模型推理的显存与带宽瓶颈几乎都在权重和 KV 缓存上。量化（quantization）把高精度浮点（FP16/FP32）权重压成低位宽（INT4 / FP8 / NVFP4），在精度损失可控的前提下换来更小的显存占用和更快的 GEMM。对 Jetson、DRIVE 这类显存极有限的边缘设备，量化几乎是把大模型「塞进去」的前提。

### 2.2 训练后量化（PTQ）与校准（calibration）

本包做的是 **训练后量化（Post-Training Quantization, PTQ）**：不动训练流程，直接对一个已经训好的 FP16 检查点做量化。PTQ 需要一个**校准（calibration）**步骤——用一小批真实数据跑前向，统计每个量化器（quantizer）看到激活值的最大幅值 `amax`，从而算出量化比例 `scale`。最基本的公式（对称量化）为：

\[
q = \mathrm{clip}\!\left(\mathrm{round}\!\left(\frac{x}{s}\right),\, -m,\, m\right), \quad s = \frac{\mathrm{amax}(x)}{m}
\]

其中 \(m\) 是目标整数类型的 `maxbound`（例如 INT8 为 127，FP8-E4M3 为 448）。后面在 `quantize.py` 里你会看到一行 `scale = amax / maxbound`，就是这个公式的落地。

### 2.3 ModelOpt 是什么

本包底层依赖 NVIDIA **ModelOpt**（`modelopt.torch.quantization as mtq`）。ModelOpt 负责最脏的活：把量化器插进 `nn.Module`、跑校准前向收集 `amax`、把权重量化并打包。本包做的事情是「**组织 ModelOpt 的配置（配方）+ 编排 load→quant→save 流程 + 写出统一 HF 检查点**」，而不是自己实现量化算法。

### 2.4 与 u2-l1 的衔接

- u2-l1 的 `QuantConfig` 用九个常量描述格式：`fp16`、`fp8`、`mxfp8`、`nvfp4`、`int4_awq`、`int4_awq_modelopt`、`int4_gptq`、`int8_sq`、`mixed_precision`。
- 本包**只生产其中一部分**：backbone 支持 `fp8 / int4_awq / nvfp4 / mxfp8 / int8_sq`。`int4_awq_modelopt`、`int4_gptq` 不在本包生产，它们由外部工具（ModelOpt 自带导出、GPTQ 训练等）产出，但导出侧照样能读。**生产者产一个子集，消费者读一个全集**——这是理解两个包常量差异的关键。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [tensorrt_edgellm/quantization/README.md](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/quantization/README.md) | 量化包的设计说明、支持的方法矩阵、用法与端到端工作流 |
| [tensorrt_edgellm/quantization/quantization_configs.py](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/quantization/quantization_configs.py) | 配方预设：`build_quant_config()` 把方法名拼装成一张 ModelOpt 量化配置 |
| [tensorrt_edgellm/quantization/quantize.py](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/quantization/quantize.py) | 编排主流程 `quantize_and_export()`：加载 → 量化 → 校准 → 写检查点 |
| [docs/source/developer_guide/software-design/quantization-design.md](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/docs/source/developer_guide/software-design/quantization-design.md) | 官方设计文档：设计目标、包布局、流程、产物契约 |
| [tensorrt_edgellm/scripts/quantize.py](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/scripts/quantize.py) | `tensorrt-edgellm-quantize` 命令行入口（`llm` / `draft` / `qwen3-omni` 子命令） |

## 4. 核心概念与源码讲解

### 4.1 解耦设计与产物契约

#### 4.1.1 概念说明

最早的时候，量化逻辑是「混」在导出流程里的：它 import 了模型专属的加载器、自定义解码层、ONNX 插件、甚至 TensorRT 原生算子。这带来一个麻烦——量化本质上是一个 **GPU + PyTorch 专属** 的步骤，把它和导出绑死，会让整个导出环境变得又重又脆（依赖一大堆本不该出现在导出环境的包）。

`tensorrt_edgellm.quantization` 包的诞生就是为了**把量化彻底拆出来**：它只读 FP16 检查点、只依赖 `torch` + `transformers` + `modelopt`，跑完量化后写出一份**统一的 HuggingFace 风格检查点**，然后就停手。导出（ONNX）、引擎构建、推理一律不归它管。

一句话记住核心动机：**量化是「产出一份带量化元数据的 HF 检查点」，仅此而已；导出和运行时只读这份检查点，不碰量化的实现细节。**

#### 4.1.2 核心流程

整个流水线如下（来自设计文档）：

```text
FP16 检查点
  -> tensorrt_edgellm.quantization   （本包：量化 + 校准）
  -> 量化的 safetensors + 量化元数据   （产物：仍是 HF 风格检查点）
  -> tensorrt_edgellm                （导出侧：读检查点 → ONNX + sidecar）
```

本包的边界**精确地停在「写出检查点」这一步**。也就是说：

| 归本包管 | 不归本包管 |
|---|---|
| 加载模型、配配方、校准、量化、写 safetensors | ONNX 导出、TensorRT engine 构建、C++ 运行时 |
| 写 `hf_quant_config.json` 等量化元数据 | `QuantConfig`（u2-l1）如何解析这些元数据 |

#### 4.1.3 源码精读

README 里有一张「问题 → 方案」的设计图，把旧路径的耦合与新包的解耦画得很清楚：

[tensorrt_edgellm/quantization/README.md:10-37](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/quantization/README.md#L10-L37) —— 描述旧路径为何耦合（导入模型专属加载器/ONNX 插件/TRT 算子），以及新方案如何用一个独立包只产出「统一检查点」。

官方设计文档列了四条设计目标，其中最关键的两条是「与 ONNX 导出解耦」「保持 HF 兼容的检查点布局」：

[docs/source/developer_guide/software-design/quantization-design.md:7-13](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/docs/source/developer_guide/software-design/quantization-design.md#L7-L13) —— 设计目标：量化与导出分离、保持 HF 检查点布局、把量化元数据写进配置文件、本地量化与下载的预量化检查点共享同一份产物契约。

设计文档还明确划了「包布局」表，告诉你哪个文件干哪件事：

[docs/source/developer_guide/software-design/quantization-design.md:15-24](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/docs/source/developer_guide/software-design/quantization-design.md#L15-L24) —— 包布局：`scripts/quantize.py` 是用户命令、`quantize.py` 负责加载/配置/校准/写出、`quantization_configs.py` 是共享配方、`config.py` 和 `loader.py` 则属于**导出侧**（消费这份检查点）。

**产物契约（Artifact Contract）** 是本包与导出侧之间最重要的约定——输出目录必须包含哪些文件：

[docs/source/developer_guide/software-design/quantization-design.md:38-47](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/docs/source/developer_guide/software-design/quantization-design.md#L38-L47) —— 产物契约：输出目录必须含 `config.json`（架构字段）、量化元数据（`quantization_config` / `hf_quant_config.json`）、一个或多个 `.safetensors` 分片、以及 tokenizer/processor 文件；导出侧据此选择量化线性层、repack 权重、在 KV 标 `fp8` 时自动启用 FP8 KV 缓存（无需单独的导出 flag）。

最后，设计文档坦白了边界：

[docs/source/developer_guide/software-design/quantization-design.md:76-79](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/docs/source/developer_guide/software-design/quantization-design.md#L76-L79) —— 限制：不包含属于 ONNX 导出的模型专属 workaround；本包只产检查点，不构建 ONNX 或 TensorRT engine。

#### 4.1.4 代码实践

**实践目标**：亲手确认「产物契约」与「目录结构」。

1. 打开 [README 的端到端工作流小节](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/quantization/README.md#L160-L180)，对照「step 1 量化 → step 2 导出 → step 3/4 在边缘设备」的四步划分。
2. 列出「产物契约」要求输出目录必须包含的 4 类文件（架构 config、量化元数据、safetensors 分片、tokenizer/processor）。
3. **待本地验证**：如果你本机有 GPU，可执行下面命令（无 GPU 则只组装命令并解释参数即可）：

   ```bash
   tensorrt-edgellm-quantize llm \
       --model_dir /data/Qwen3.5-0.8B \
       --output_dir /tmp/qwen35-nvfp4 \
       --quantization nvfp4 --lm_head_quantization nvfp4
   ```

   **需要观察的现象**：`/tmp/qwen35-nvfp4/` 下出现 `config.json`、`*.safetensors`、`hf_quant_config.json`（或内嵌在 config 里）、tokenizer 文件，但**没有** `.onnx` / `.engine`。
   **预期结果**：输出目录在文件类型上和原始 HF 检查点几乎一样（仍是 safetensors + config），区别只在于权重被量化、且多了量化元数据。

#### 4.1.5 小练习与答案

**Q1**：为什么量化包不直接产出 ONNX，而要先停下、写成 HF 检查点？

**参考答案**：为了让量化（GPU + PyTorch 专属步骤）与导出彻底解耦；同时让「本地量化」与「下载别人量化好的」共享同一份产物契约，导出侧只需认检查点、不必关心量化的实现。

**Q2**：导出侧靠什么文件知道「KV 缓存该用 FP8」？

**参考答案**：靠量化检查点里的元数据（`hf_quant_config.json` / `quantization_config`）。当检查点把 KV 标为 `fp8` 时，导出侧自动启用 FP8 KV 缓存，不需要单独的导出 flag。

---

### 4.2 配方预设：quantization_configs.py 与 build_quant_config

#### 4.2.1 概念说明

ModelOpt 量化一个模型，需要给它一张**配置（quant config）**：一张「通配符 pattern → 量化器设置」的映射表。比如 `*weight_quantizer` 表示「所有线性层的权重量化器」，`*lm_head.weight_quantizer` 表示「lm_head 的权重量化器」。每条设置包含位数 `num_bits`、缩放轴 `axis`、是否启用 `enable` 等字段。

在本包里，这样一张配置被称为**配方（recipe）**。`quantization_configs.py` 的核心就是 `build_quant_config()`：把人类能懂的方法名（`nvfp4`、`fp8`……）翻译成 ModelOpt 能吃的配置表。

一个配方由**六个轴**组合而成：

| 轴 | CLI 参数 | 控制范围 | 支持的方法 |
|---|---|---|---|
| backbone | `--quantization` | 主干所有 Linear | fp8 / int4_awq / nvfp4 / mxfp8 / int8_sq |
| lm_head | `--lm_head_quantization` | lm_head 投影 | fp8 / int4_awq / nvfp4 / mxfp8 |
| kv_cache | `--kv_cache_quantization` | 注意力的 K/V 缓存 | fp8 |
| visual | `--visual_quantization` | 视觉塔 | fp8 |
| audio | `--audio_quantization` | 音频塔 | fp8 |
| cp | `--cp_quantization` | Qwen3-Omni CodePredictor | fp8 |

backbone 与 lm_head 可任意组合，从而支持混合精度，例如「NVFP4 backbone + FP8 lm_head」。

#### 4.2.2 核心流程

`build_quant_config()` 的拼装顺序非常讲究，因为 ModelOpt 的 pattern 匹配是「**后写覆盖先写（last-writer-wins）**」——同一条 pattern 后出现的设置会覆盖前面的。所以「先全局禁用、再按需打开」的顺序决定了最终哪些模块被量化。流程为：

1. **选 backbone 基底**：从 `_BACKBONE_CFG_MAP` 取一份对应方法的预设，**深拷贝**（因为 ModelOpt 预设是模块级单例，浅拷贝会让内层 dict 被别名共享、污染下一次调用）。若 `quantization=None`，则用 `{"default": {"enable": False}}` 把所有量化器关掉（这就是「只量化 lm_head、backbone 保持 fp16」的实现方式）。
2. **叠加 lm_head 覆盖**：先把基底里所有含 `lm_head` 的量化器删掉，再把对应方法的 lm_head 预设 merge 进去。
3. **叠加 kv_cache**：若为 `fp8`，merge ModelOpt 的 `FP8_KV_CFG` 与本包的 `FP8_ATTN`（Q/K/V 的 BMM 量化器）。
4. **默认禁用非 LLM 组**：把 code2wav（永远禁用）、以及未显式要求的 cp / visual / audio 组的 pattern 全部 `enable=False`。
5. **按需叠加 cp / visual / audio 覆盖**：因为第 4 步已经把它们禁用，这里的覆盖排在后面，所以「特定 pattern」能盖过「全局禁用」。

#### 4.2.3 源码精读

backbone 与 lm_head 的方法→预设映射表是两张字典：

[tensorrt_edgellm/quantization/quantization_configs.py:303-316](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/quantization/quantization_configs.py#L303-L316) —— `_BACKBONE_CFG_MAP`（backbone 五法，指向 `mtq.FP8_DEFAULT_CFG` 等 ModelOpt 预设）与 `_LM_HEAD_CFG_MAP`（lm_head 四法，指向本包自定义的 `FP8_LM_HEAD` 等）。注意 lm_head 少了 `int8_sq`。

lm_head 的预设只启用 `*lm_head.*` 的量化器。下面是 NVFP4 的 lm_head 配方（权重与激活都量化）：

[tensorrt_edgellm/quantization/quantization_configs.py:60-83](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/quantization/quantization_configs.py#L60-L83) —— `NVFP4_LM_HEAD`：input/weight 量化器都设 `num_bits=(2,1)`、`block_sizes={-1:16, type:"dynamic", scale_bits:(4,3)}`、`axis=None`、`enable=True`。即「4 位权重 + 每 16 元素一块的 E4M3 缩放」。

> 直觉解释 NVFP4：把权重压到约 4 位（ModelOpt 用 `(2,1)` 编码表示），每 16 个权重共享一个 FP8（E4M3，即 `scale_bits:(4,3)`）的块缩放因子。这种「块缩放」让低比特权重保持较好精度，且与 C++ 侧 NVFP4 GEMM 插件一一对应。对比之下，纯 FP8 配方（`FP8_LM_HEAD`）是 per-tensor、`num_bits=(4,3)` 的 E4M3。

拼装主函数 `build_quant_config()` 的关键几段：

[tensorrt_edgellm/quantization/quantization_configs.py:470-481](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/quantization/quantization_configs.py#L470-L481) —— 选 backbone 基底：`None` 时关掉所有量化器；否则深拷贝对应预设；不支持的方法直接 `raise ValueError`。

[tensorrt_edgellm/quantization/quantization_configs.py:483-492](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/quantization/quantization_configs.py#L483-L492) —— lm_head 覆盖：先 `_remove_lm_head_quantizers` 删掉基底里的 lm_head 项，再 merge 指定方法的 lm_head 预设。

[tensorrt_edgellm/quantization/quantization_configs.py:500-524](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/quantization/quantization_configs.py#L500-L524) —— 先默认禁用非 LLM 组（code2wav 永远禁；cp/visual/audio 在未要求时禁），再把 cp 覆盖叠在后面，保证「特定 pattern 胜过全局禁用」。

这里有一个**血泪教训式注释**值得细读——它解释了为什么 lm_head 预设里**绝不能**写全局 `{"default": {"enable": False}}`：

[tensorrt_edgellm/quantization/quantization_configs.py:20-30](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/quantization/quantization_configs.py#L20-L30) —— 警告：lm_head 覆盖层里只能启用 lm_head 量化器，绝不能加全局 `default:{enable:False}`；否则它会被 merge 成排在 backbone 启用项之后的 `quantizer_name:"*"` 禁用项，**静默地**把整个主干 Linear 的权重量化器关掉——这正是历史上让 100+ 个检查点「名字带 -LMFP4 但 body 仍是 fp16」的生产者侧 bug。

视觉塔还展示了一个「前缀展开」技巧——不同 VLM 家族用不同的模块前缀（`visual.*` / `vision_tower.*` / `mlp1.*` …），单写一个通配符会漏：

[tensorrt_edgellm/quantization/quantization_configs.py:128-165](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/quantization/quantization_configs.py#L128-L165) —— `_VISUAL_PREFIXES` 列出 7 个视觉前缀，`_visual_quant_cfg()` 为每个前缀各生成一条 input/weight pattern，避免单前缀通配符漏掉其他家族的视觉模块。

#### 4.2.4 代码实践

**实践目标**：在 Python 里亲手拼一个 NVFP4 配方，看它含哪些字段。

1. 在装好 `tensorrt_edgellm`（含 `tools` 可选依赖，即 `nvidia-modelopt`）的环境里，进 REPL：

   ```python
   from tensorrt_edgellm.quantization.quantization_configs import build_quant_config
   import json
   cfg = build_quant_config(quantization="nvfp4", lm_head_quantization="nvfp4")
   print(json.dumps(cfg, indent=2, default=str)[:3000])
   ```

2. **需要观察的现象**：
   - `cfg` 是一个 dict，含键 `"quant_cfg"`（pattern→设置映射）和 `"algorithm"`（来自 ModelOpt 的 NVFP4 预设）。
   - `quant_cfg` 里能看到 backbone 的 `*weight_quantizer` / `*input_quantizer` 已启用，且带 `num_bits:(2,1)` 的 NVFP4 设置。
   - `*lm_head.*` 的量化器单独存在（NVFP4_LM_HEAD 注入）。
   - `*visual*` / `*audio*` / `*code2wav*` 等 pattern 的 `enable` 为 `False`（因为没要求量化它们）。

3. **预期结果 / 待本地验证**：上述字段确实出现；若把 `lm_head_quantization=None`，则看不到任何 `*lm_head*` 的 NVFP4 启用项（被 `_remove_lm_head_quantizers` 清掉、又没补回来，主干基底里 lm_head 默认也是被 NVFP4 预设覆盖的——具体以本地打印为准）。

#### 4.2.5 小练习与答案

**Q1**：若想让 backbone 保持 fp16、只把 lm_head 量化成 fp8，该怎么调 `build_quant_config`？它的内部机制是什么？

**参考答案**：调 `build_quant_config(quantization=None, lm_head_quantization="fp8")`。机制：`quantization=None` 会用 `{"default":{"enable":False}}` 关掉所有量化器作为基底，然后 lm_head 覆盖只把 `*lm_head.*` 重新启用。

**Q2**：为什么 `build_quant_config` 在取 backbone 预设时要用 `copy.deepcopy` 而不是 `.copy()`？

**参考答案**：因为 `mtq.NVFP4_DEFAULT_CFG` 等是模块级单例，`.copy()` 只复制外层映射，内层 `quant_cfg` 仍与全局别名共享，后续 merge 会污染下一次调用。`deepcopy` 才能彻底切断别名。

---

### 4.3 量化编排主流程：quantize.py 的 load → quant → save

#### 4.3.1 概念说明

`quantize_and_export()` 是本包的总入口（也被 `__init__.py` 导出为公共 API，见 [tensorrt_edgellm/quantization/__init__.py:28](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/quantization/__init__.py#L28)）。CLI（`tensorrt-edgellm-quantize llm`）、Python API、以及各种 draft / omni 流程最终都汇聚到它。它像一个项目经理：负责「加载模型 → 配配方 → 校准 → 量化 → 写检查点 → 拷贝 sidecar」的全链路编排，但不亲自实现量化算法（交给 ModelOpt）。

#### 4.3.2 核心流程

`quantize_and_export()` 的伪代码主流程：

```text
def quantize_and_export(model_dir, output_dir, quantization, lm_head_quantization, ...):
    1. model, tokenizer, processor = _load_model(model_dir, dtype, device)
    2. （可选）若检测到 MTP draft 层：先量化 MTP draft
    3. 若模型已量化（is_quantized）→ 跳过
       否则：
         a. quant_cfg = build_quant_config(quantization, lm_head_quantization, ...)
         b. 按「哪个模态要量化」选校准分支：
              - CP 联合校准 / ASR 多模态 / 视觉多模态 / 纯文本
         c. mtq.quantize(model, quant_cfg, forward_loop=<校准前向>)
    4. 修补 generation_config、tied_weights 等导出兼容性 WAR
    5. （可选）为未量化的 MTP 加载权重用于统一导出
    6. 收集 attention Q-BMM 校准 scale（供导出 prefill FP8 Q 用）
    7. export_hf_checkpoint(model, export_dir=output_dir, extra_state_dict=...)
    8. 清理陈旧分片索引
    9. tokenizer.save_pretrained(...)；processor.save_pretrained(...)
   10. 拷贝 preprocessor/processor/chat_template 等配置文件
   11. （Qwen3-ASR 专属）后处理回 qwen3_asr 布局
```

#### 4.3.3 源码精读

入口函数签名与文档串（注意它接受的六个量化轴参数与 4.2 一一对应）：

[tensorrt_edgellm/quantization/quantize.py:618-650](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/quantization/quantize.py#L618-L650) —— `quantize_and_export()` 的签名与文档：六个量化参数（backbone/lm_head/visual/cp/kv_cache/audio）+ 数据集参数；文档强调 `visual_quantization` 需要多模态校准，否则视觉量化器的激活 scale 未初始化。

**步骤 1：加载模型**。`_load_model()` 用一串 `Auto*` 工厂按「最具体优先」的顺序尝试加载，这个顺序有讲究：

[tensorrt_edgellm/quantization/quantize.py:229-262](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/quantization/quantize.py#L229-L262) —— 工厂链 `TextToWaveform > ImageTextToText > CausalLM > AutoModel`：`ImageTextToText` 必须排在 `CausalLM` 前，否则 Qwen3-VL 这类同时注册了纯文本 CausalLM 与多模态架构的检查点会被解析成纯文本、丢掉视觉塔；只有「识别失败（ValueError/KeyError）」才回退，避免掩盖 ImportError。

**步骤 3a：配配方**（调用 4.2 讲过的函数）：

[tensorrt_edgellm/quantization/quantize.py:711-718](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/quantization/quantize.py#L711-L718) —— 把六个 CLI 参数原样传给 `build_quant_config()` 拼出 `quant_cfg`。

**步骤 3b/3c：校准 + 量化**。默认的纯文本分支：

[tensorrt_edgellm/quantization/quantize.py:801-812](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/quantization/quantize.py#L801-L812) —— 纯文本校准：用 `resolve_dataset` 按名字解析文本数据集（默认 `cnn_dailymail`），构 DataLoader，调 `mtq.quantize(model, quant_cfg, forward_loop=_calibrate)`；`int4_awq` 用 `batch_size=16`，其余用 `batch_size=1`。

`_calibrate()` 就是前面 2.2 说的校准前向——逐 batch 喂数据让 ModelOpt 收集 `amax`：

[tensorrt_edgellm/quantization/quantize.py:374-382](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/quantization/quantize.py#L374-L382) —— `_calibrate()`：遍历 DataLoader，把 batch 搬到 device 后跑 `model(data)`；Phi-4MM 需要额外的 `input_mode=0`。

量化完后，`_collect_attention_q_scales_for_export()` 把校准得到的 Q-BMM `amax` 换算成 `scale` 写进导出——这里就是 2.2 那个公式的真实代码：

[tensorrt_edgellm/quantization/quantize.py:411-421](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/quantization/quantize.py#L411-L421) —— `scale = amax / maxbound`，即 \(s=\mathrm{amax}/m\)，校验它是 per-tensor 且有限正数，存成 `<layer>.q_proj.q_scale`，供导出侧把 prefill 的 Q 量化成 E4M3 而不饱和。

**步骤 7：写出检查点**。核心就一行 ModelOpt 调用：

[tensorrt_edgellm/quantization/quantize.py:841-848](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/quantization/quantize.py#L841-L848) —— 在 `torch.inference_mode()` 与 hybrid-resmooth WAR 上下文里调 `export_hf_checkpoint(model, export_dir=output_dir, extra_state_dict=...)`，把量化的权重 + 量化元数据写成 safetensors + `hf_quant_config.json`；`extra_state_dict` 携带前面收集的 MTP 张量与 attention Q scale。

**步骤 9/10：拷贝 sidecar**。`export_hf_checkpoint` 只写模型 + 量化元数据，processor 等配置必须显式拷：

[tensorrt_edgellm/quantization/quantize.py:850-866](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/quantization/quantize.py#L850-L866) —— 先 `tokenizer.save_pretrained` / `processor.save_pretrained`，再把 `preprocessor_config.json`、`processor_config.json`、`video_preprocessor_config.json`、`chat_template.jinja` 从源目录拷到输出目录，让下游 C++ 视觉 builder 能找到 patch_size / image_mean 等预处理参数。

#### 4.3.4 代码实践

**实践目标**：理解「校准分支选择」逻辑，并对照产物契约检查输出目录。

1. 阅读 [quantize.py:686-812](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/quantization/quantize.py#L686-L812) 的 `if/elif` 链，列出五种校准分支的触发条件：
   - 已量化 → 跳过；
   - `cp_quantization` 非空且有 CodePredictor → CP 联合校准；
   - `is_qwen3_asr_model` → ASR 多模态校准；
   - `visual_quantization` 非空 → 视觉多模态校准；
   - 其余 → 纯文本校准。
2. **待本地验证**：跑一遍 4.1.4 的命令后，用 `ls /tmp/qwen35-nvfp4` 检查输出目录，确认同时存在 `config.json`、`*.safetensors`、tokenizer 文件，以及量化元数据（`hf_quant_config.json` 或内嵌于 config）。
3. **预期结果**：输出目录与原始 HF 检查点的文件**类型基本相同**（都是 safetensors + config + tokenizer），差异在于：权重张量已是量化布局、`config.json`/`hf_quant_config.json` 多了量化元数据、`*.safetensors` 可能因量化变小而合并成更少的分片（甚至单文件，此时陈旧的 `model.safetensors.index.json` 会被 `_remove_stale_safetensors_index` 清掉，见 [quantize.py:448-461](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/quantization/quantize.py#L448-L461)）。

#### 4.3.5 小练习与答案

**Q1**：为什么 `ImageTextToText` 工厂要排在 `CausalLM` 之前？

**参考答案**：Qwen3-VL 等检查点同时注册了纯文本 CausalLM 与多模态架构；若 `CausalLM` 在前，会把检查点解析成纯文本模型、丢掉视觉塔，导致后续 `visual_quantization` 失效。

**Q2**：`export_hf_checkpoint` 写完检查点后，为什么还要单独拷贝 `preprocessor_config.json` 等文件？

**参考答案**：`export_hf_checkpoint` 只写模型权重 + 量化元数据，不写 processor 元数据；而下游 C++ 视觉 builder 需要 `patch_size` / `image_mean` 等预处理参数，这些只存在于源 HF 目录，必须显式拷贝。

---

### 4.4 量化对 draft 模型的支持（EAGLE3 / DFlash / MTP）

#### 4.4.1 概念说明

投机解码（speculative decoding）用一个小的 **draft 模型**快速猜若干 token，再用大 base 模型验证。Edge-LLM 支持三类 draft：EAGLE3、DFlash、MTP（详见 u7 单元）。这些 draft 模型同样需要量化。

draft 量化的难点在于：有些 draft 架构（如 EAGLE3）**在 HuggingFace `transformers` 里没有现成实现**，无法直接 `AutoModelForCausalLM` 加载。本包的做法是在 `quantization/models/` 下提供**独立的纯 PyTorch 校准模型**（只用 `RMSNorm` / `SwiGLUMLP` / `RotaryEmbedding` 这些基础件，不依赖 transformers 的模型类），只实现校准用的 `forward`——完整的带 KV-cache 推理 forward 留给导出层。

#### 4.4.2 核心流程

draft 量化由 `tensorrt-edgellm-quantize draft` 子命令驱动，分两种自动检测的路径：

```text
draft 子命令
  ├─ 读 draft_model_dir/config.json，看有没有 dflash_config
  │     ├─ 有  -> DFlash draft 流程（quantize_and_export_dflash_draft）
  │     └─ 无  -> EAGLE3 draft 流程（quantize_and_export_draft）
```

MTP 则不同：它**不是独立检查点**，而是 base 模型 config 里的 `mtp_num_hidden_layers` 字段。因此 MTP 量化发生在**普通 `llm` 命令**内部——`quantize_and_export()` 检测到 MTP 层后，先用 base 模型量化出 MTP draft，再统一导出。

#### 4.4.3 源码精读

CLI 的 draft 分发逻辑：

[tensorrt_edgellm/scripts/quantize.py:187-218](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/scripts/quantize.py#L187-L218) —— `draft` 子命令：先 `_is_dflash_draft()`（读 `config.json::dflash_config`）判断，是则走 DFlash，否则走 EAGLE3；DFlash 默认 `nvfp4`，EAGLE3 默认 `fp8`。

DFlash 的一个重要细节：它的 `fc`（target-hidden 投影器）**故意排除在量化之外**：

[docs/source/developer_guide/software-design/quantization-design.md:68-74](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/docs/source/developer_guide/software-design/quantization-design.md#L68-L74) —— DFlash 用 base 模型仅提供校准 hidden state 与（draft 无自己的 lm_head 时的）lm_head fallback；`fc` 投影器不量化，因为导出器要让该投影走全 FP32 累加路径以保精度。

MTP 的检测与「先量化 draft」逻辑在 `quantize_and_export()` 开头：

[tensorrt_edgellm/quantization/quantize.py:654-683](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/quantization/quantize.py#L654-L683) —— 读 `text_config.mtp_num_hidden_layers`，若 >0 且要量化，则先 `quantize_mtp_from_base()` 量化 MTP draft，把结果存进 `mtp_state_dict`，稍后随 base 一起统一导出。

若检测到 MTP 层但**不量化**（如只跑 lm_head 量化），则走「为统一导出加载未量化 MTP 权重」的兜底：

[tensorrt_edgellm/quantization/quantize.py:819-823](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/quantization/quantize.py#L819-L823) —— 未量化的 MTP 通过 `_load_mtp_weights_for_unified_export()` 加载，保证导出产物里仍包含 MTP 层。

#### 4.4.4 代码实践

**实践目标**：追踪一条 draft 量化命令的执行路径。

1. 阅读命令示例 [README:96-117](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/quantization/README.md#L96-L117)（EAGLE3 fp8 与 DFlash nvfp4）。
2. 对照 [scripts/quantize.py:187-218](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/scripts/quantize.py#L187-L218) 说明：一条 `draft` 命令在什么条件下会走 DFlash、什么条件下走 EAGLE3、各自默认方法是什么。
3. **待本地验证**：若有 base+draft 检查点，跑 EAGLE3 draft 量化，观察输出目录同样产出 safetensors + `hf_quant_config.json`（与普通 LLM 量化产物契约一致）。

#### 4.4.5 小练习与答案

**Q1**：EAGLE3 draft 为什么不能直接用 `AutoModelForCausalLM` 加载？

**参考答案**：因为 EAGLE3 是投机解码专用的 draft 架构，HuggingFace `transformers` 没有现成实现；本包在 `quantization/models/eagle3_draft.py` 里用纯 PyTorch 基础件重建了一个仅含校准 forward 的独立模型。

**Q2**：MTP draft 的量化入口和 EAGLE3/DFlash 有什么不同？

**参考答案**：EAGLE3/DFlash 是独立检查点，走 `draft` 子命令、由 `dflash_config` 自动区分；MTP 不是独立检查点，而是 base 模型 config 的 `mtp_num_hidden_layers` 字段，因此其量化发生在普通 `llm` 命令的 `quantize_and_export()` 内部、先于 base 模型量化。

---

## 5. 综合实践

把本讲三个核心模块串起来，完成规格里指定的综合任务：**写出一个 NVFP4 量化的配方字段，并说明量化后检查点与原始 HF 检查点在目录结构上的异同。**

### 5.1 任务一：NVFP4 配方字段

针对「NVFP4 backbone + NVFP4 lm_head」的完整量化，请写出：

1. **CLI 层面**需要指定的参数（最少两项核心 + 可选的 KV/visual/audio/cp）。
2. **配方对象层面**（`build_quant_config("nvfp4","nvfp4")` 的返回）必然包含的结构：
   - 顶层键：`"quant_cfg"`（pattern→设置映射）、`"algorithm"`（来自 ModelOpt NVFP4 预设）。
   - backbone 的 `*weight_quantizer` / `*input_quantizer`：启用、`num_bits=(2,1)`、块缩放 `block_sizes={-1:16, scale_bits:(4,3)}`。
   - `*lm_head.*` 的 input/weight 量化器：同上 NVFP4 设置（来自 `NVFP4_LM_HEAD`）。
   - 未要求的 `*visual*` / `*audio*` / `*code2wav*`：`enable=False`。

> 参考答案见 4.2.3 与 4.2.4；可用 4.2.4 的 REPL 命令本地验证。

### 5.2 任务二：目录结构异同

假设原始检查点目录为 `/data/Qwen3.5-0.8B/`，量化输出为 `/tmp/qwen35-nvfp4/`。请填表：

| 文件 | 原 HF 检查点 | 量化后检查点 | 说明 |
|---|---|---|---|
| `config.json` | 有（架构字段） | 有（架构字段 + 量化元数据） | 量化元数据可能内嵌于此 |
| `hf_quant_config.json` | 一般无 | 有（或内嵌进 config） | 导出侧 `QuantConfig` 读它 |
| `*.safetensors` | 多个 FP16 分片 | 更少/更小的量化分片 | 可能合并成单文件并清掉陈旧 index |
| tokenizer 文件 | 有 | 有（`save_pretrained` 拷出） | 一致 |
| `preprocessor_config.json` 等 | 有（VLM 才有） | 有（显式拷贝） | C++ 视觉 builder 需要 |
| `.onnx` / `.engine` | 无 | **无** | 本包不产出，归导出/构建 |

**关键结论**：量化后检查点在**文件类型与目录布局上与原 HF 检查点几乎一致**（仍是 safetensors + config + tokenizer），本质区别是「权重被量化 + 多了量化元数据」——这正是「保持 HF 兼容布局」设计目标的体现，也是 u2-l1 的 `QuantConfig` 能无差别消费本地量化与下载预量化检查点的原因。

## 6. 本讲小结

- `tensorrt_edgellm.quantization` 是一个**与导出解耦**的独立包，唯一产物是「带量化元数据的统一 HF 检查点」，不碰 ONNX/engine。
- 一次量化由 **backbone / lm_head / kv_cache / visual / audio / cp** 六个轴组合；backbone 与 lm_head 可任意搭配，支持混合精度。
- `build_quant_config()` 用「**深拷贝 backbone 预设 → 删并补 lm_head → 禁用非 LLM 组 → 叠加特定覆盖**」的有序合并，依赖 ModelOpt「last-writer-wins」的 pattern 匹配语义。
- `quantize_and_export()` 是总编排：`Auto*` 工厂链加载模型 → 配配方 → 按模态选校准分支 → `mtq.quantize` → `export_hf_checkpoint` → 拷 sidecar。
- draft 量化分两路：EAGLE3/DFlash 走 `draft` 子命令（由 `dflash_config` 自动区分）；MTP 不是独立检查点，在 `llm` 命令内部先于 base 量化。
- 本包生产的方法是导出侧 `QuantConfig`（u2-l1）所读全集的一个**子集**（不含 `int4_awq_modelopt`、`int4_gptq`）。

## 7. 下一步学习建议

- **u3-l2 量化 CLI 与支持的格式**：从命令行视角系统过一遍 `tensorrt-edgellm-quantize` 的 `llm` / `draft` / `qwen3-omni` 子命令，以及 fp8 / nvfp4 / int4_awq / mxfp8 / int8_sq 在精度与显存上的权衡。
- **u3-l3 量化权重格式与 sidecar**：深入量化检查点的张量布局（AWQ 列打包、NVFP4 分组+FP8 scale）以及导出阶段的 repack，承接本讲的「产物」到 u2-l4 的「加载」。
- 想回顾量化元数据如何被消费，回看 **u2-l1** 的 `QuantConfig._parse_quant`；想了解整体流水线定位，回看 **u1-l2**。
