# LoRA 支持（导出 + 运行时）

## 1. 本讲目标

LoRA（Low-Rank Adaptation，低秩适配）是大模型微调的主流手段：用极少参数把一个「能力」烧进模型。本讲的目标是把 LoRA 在 TensorRT Edge-LLM 里的**全链路**讲透——从 Python 导出端在 ONNX 图里「凿」出 LoRA 输入、把 HuggingFace adapter 权重转成运行时 sidecar、构建期给 LoRA 维度配优化 profile，一直到 C++ 运行时按请求里的 `loraWeightsName` 在多个 adapter 之间零拷贝切换。

学完本讲你应当能够：

- 说清 EdgeLLM 里 LoRA 的**两种接入路径**（动态运行时 LoRA vs 静态合并）以及各自的适用场景。
- 读懂 `lora.py` 的三大函数：`insert_lora_and_save`（图插入）、`process_lora_weights_and_save`（权重转换）、`merge_lora_and_save`（静态合并）。
- 说清三个 CLI——`tensorrt-edgellm-insert-lora` / `-process-lora` / `-merge-lora`——分别对应哪个函数、产出什么文件。
- 解释运行时 `LoRAManager` 如何用「换指针 + rank=1 dummy」实现 O(1) 的 adapter 切换，以及为什么 CUDA graph 要为每个 adapter 各录一张。
- 说明为什么 Phi-4-Multimodal 必须在量化前用 `merge-lora` 静态合并它的 `vision-lora`。

## 2. 前置知识

在进入本讲前，你需要熟悉以下概念（对应前置讲义）：

- **三段式流水线与 sidecar 契约**（u1-l2、u2-l6）：导出在 x86 上产出 ONNX 图与若干 sidecar 文件，构建器把 ONNX 编成 engine，运行时加载 engine 推理。本讲里 LoRA 权重就是一种运行时 sidecar。
- **导出端的自定义算子与 ONNX 图**（u2-l5）：理解一张 ONNX 图由节点（Node）和图输入（graph inputs）组成，本讲会在图上动态追加输入。
- **量化线性层的多种格式**（u2-l3、u3-l3）：FP16 MatMul、FP8 Q/DQ、NVFP4、INT4 AWQ/GPTQ 在图里长得不一样，LoRA 插入必须能识别它们。
- **运行时的引擎执行器与 TensorMap**（u5-l1、u5-l3）：`EngineExecutor` 靠 `TensorMap`（名字→`Tensor*` 的地址表）把引擎输入输出绑定到运行时张量。LoRA 切换就是改写这张表里的 LoRA 条目。
- **解码策略层**（u5-l4）：`handleRequest` 的主循环与具体解码算法解耦，LoRA 切换发生在 prefill 准备阶段。

**LoRA 数学直觉**：全量微调更新的是权重矩阵 \(W\in\mathbb{R}^{n\times k}\)。LoRA 假设这个更新是低秩的，用两个小矩阵 \(B\in\mathbb{R}^{n\times r}\)、\(A\in\mathbb{R}^{r\times k}\)（秩 \(r\ll \min(n,k)\)）去近似它：

\[
W_{\text{new}} = W + \Delta W = W + \frac{\alpha}{r} B A
\]

其中 \(\alpha\) 是缩放系数。前向变成 \(y = Wx + \frac{\alpha}{r} B(Ax)\)：先让输入过 \(A\) 降到 \(r\) 维，再过 \(B\) 升回 \(n\) 维，加到原线性层输出上。训练时只更新 \(A,B\)（参数量 \(r(n+k)\)），\(W\) 冻结。

关键观察：**这个 \(\frac{\alpha}{r}\) 缩放是常数**，可以预先乘进 \(B\)。EdgeLLM 正是在权重转换阶段把它烤进 `lora_B`，于是运行时图里只需两个普通 MatMul，不带显式 scale 节点。

## 3. 本讲源码地图

本讲横跨 Python 导出端与 C++ 运行时，关键文件如下：

| 文件 | 作用 | 所属阶段 |
|------|------|---------|
| `tensorrt_edgellm/lora/lora.py` | LoRA 全部库逻辑：ONNX 图插入、adapter 权重转换、静态合并 | 导出 |
| `tensorrt_edgellm/scripts/insert_lora.py` | `insert-lora` 命令入口，薄封装 | 导出 |
| `tensorrt_edgellm/scripts/process_lora_weights.py` | `process-lora` 命令入口，薄封装 | 导出 |
| `tensorrt_edgellm/scripts/merge_lora.py` | `merge-lora` 命令入口，薄封装 | 导出 |
| `tensorrt_edgellm/lora/phi4mm_utils.py` | Phi-4-Multimodal 加载/合并辅助 | 导出 |
| `cpp/builder/llmBuilder.cpp` | `setupLoraProfiles`：给 LoRA 维度配优化 profile、选 `lora_model.onnx` | 构建 |
| `cpp/runtime/state/loraManager.cpp` / `.h` | `LoRAManager`：多 adapter 管理 + 零拷贝切换 | 运行时 |
| `cpp/common/bindingNames.h` | LoRA 绑定名约定（`lora_A` / `lora_B` 前缀） | 跨阶段 |
| `examples/utils/requestFileParser.cpp` | 从输入 JSON 解析 `available_lora_weights` 与每请求的 `lora_name` | 示例 |
| `docs/source/user_guide/features/lora.md` | 官方用户文档（命令流程示例） | 文档 |

> 命名提示：在 u1-l4 里讲过，命令名用连字符（`tensorrt-edgellm-insert-lora`），脚本模块名用下划线（`process_lora_weights.py`）。`process-lora` 对应的脚本正是 `process_lora_weights.py`。

---

## 4. 核心概念与源码讲解

### 4.1 LoRA 在 EdgeLLM 中的两种接入路径

#### 4.1.1 概念说明

EdgeLLM 的 LoRA 不是单一开关，而是**两条互斥的路径**，官方文档 `lora.md` 一开篇就点明了这个二分（见 [docs/source/user_guide/features/lora.md:5-7](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/docs/source/user_guide/features/lora.md#L5-L7)）：

- **动态运行时 LoRA（dynamic）**：导出时在 ONNX 图里给每个可适配的 GEMM 凿出两个额外输入 `lora_A` / `lora_B`；adapter 权重转成 sidecar 文件在运行时按名字加载；推理时通过请求里的 `lora_name` **逐请求切换** adapter。适合「同一个 base 模型要服务多个能力（法语、医学、客服……）」的场景。
- **静态合并（static merge）**：在量化/导出**之前**，用 PEFT 的 `merge_and_unload` 把 adapter 永久焊进 base 权重，产出一份新的 HF 检查点。之后走的是普通流水线，运行时没有任何 LoRA 概念。适合「adapter 永远需要、不需切换」的场景——典型就是 Phi-4-Multimodal 的 `vision-lora`。

一句话区分：**动态 LoRA 换的是运行时绑定的指针；静态合并改的是检查点权重本身。**

#### 4.1.2 核心流程

两条路径的全景如下：

```
【动态运行时 LoRA】
  export base ──► model.onnx
     │
     ├── insert-lora ─────────────► lora_model.onnx      (图里多了 lora_A*/lora_B* 输入)
     ├── process-lora (per adapter)► processed_adapter_model.safetensors  (运行时 sidecar)
     │
     └── llm_build --maxLoraRank R ─► engine              (LoRA 维度进了优化 profile)
            │
            └── llm_inference + input.json (lora_name) ──► LoRAManager.switchWeights 逐请求切换

【静态合并】
  merge-lora ──► merged HF checkpoint (权重已焊死)
     │
     └── (可选) quantize ──► export ──► build ──► 普通 inference (无 LoRA)
```

注意动态路径里一个微妙之处：构建期 `--maxLoraRank` 决定的是引擎「能容纳的最大秩」，运行时所有 adapter 的实际秩都必须 ≤ 它（见 [docs/source/user_guide/features/lora.md:165](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/docs/source/user_guide/features/lora.md#L165)）。

#### 4.1.3 源码精读

两种路径的入口分别是 `lora.py` 里的三个函数。动态路径用前两个，静态路径用第三个：

- `insert_lora_and_save`：[tensorrt_edgellm/lora/lora.py:400-488](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/lora/lora.py#L400-L488) — 读取 `model.onnx`，在图里插入 LoRA 子图，写出 `lora_model.onnx`。
- `process_lora_weights_and_save`：[tensorrt_edgellm/lora/lora.py:605-657](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/lora/lora.py#L605-L657) — 把 HuggingFace 的 PEFT adapter 转成运行时可消费的 `processed_adapter_model.safetensors`。
- `merge_lora_and_save`：[tensorrt_edgellm/lora/lora.py:536-602](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/lora/lora.py#L536-L602) — 用 PEFT 合并 adapter 到 base 检查点。

#### 4.1.4 代码实践

1. **实践目标**：在动手前先建立全局认知，分清两条路径。
2. **操作步骤**：阅读官方文档 `docs/source/user_guide/features/lora.md` 的「Dynamic Runtime LoRA」与「Static LoRA Merge」两节。
3. **观察现象**：注意两节给出的命令序列差异——动态节有 `insert-lora` / `process-lora` / `--maxLoraRank`，静态节没有，取而代之的是 `merge-lora` 之后再走普通 `quantize` / `export`。
4. **预期结果**：能用一句话回答「如果我有一个始终需要的 vision-lora，该选哪条路径；如果我要在线切换 10 个语言 adapter，又该选哪条」。
5. 结论：始终需要 → 静态合并；在线切换 → 动态运行时 LoRA。

#### 4.1.5 小练习与答案

- **练习 1**：动态 LoRA 的 adapter 权重存在哪里、由谁加载？
  - **答案**：转成 `processed_adapter_model.safetensors` 作为 sidecar，由运行时的 `LoRAManager::loadWeights` 在构造期加载（不是构建期烧进 engine）。
- **练习 2**：静态合并后，运行时还需要 `--maxLoraRank` 吗？
  - **答案**：不需要。静态合并产出的是普通检查点，走普通流水线，运行时没有 LoRA 概念，`lora.md` 第 164 行明确「Static merge produces a single checkpoint and does not require runtime LoRA flags」。

---

### 4.2 导出期：在 ONNX 图里插入 LoRA 输入（`insert-lora`）

#### 4.2.1 概念说明

动态 LoRA 的第一步是让 ONNX 图「认识」LoRA。`insert_lora_and_save` 干的就是这件事：对图里每一个可适配的 GEMM（线性层），在它的输出上**并联**一条 `input → MatMul(lora_A) → MatMul(lora_B) → Add` 的旁路，并把 `lora_A` / `lora_B` 作为**新的图输入**暴露出来。这样权重不在图里（不是常量 initializer），而是运行时由 `LoRAManager` 填进去——这正是「动态」的含义。

难点在于：EdgeLLM 支持多种量化格式（见 u2-l3、u3-l3），FP16 / FP8 / NVFP4 / INT4 在图里的节点形态完全不同，必须逐类匹配，否则会漏掉某些线性层的 LoRA 槽位。

#### 4.2.2 核心流程

`insert_lora_and_save` 的主循环逻辑（伪代码）：

```
graph = import_onnx(model.onnx)
gemm_infos = _match_gemm_infos(graph)     # 识别所有可适配 GEMM，分 5 类
for g in gemm_infos:
    if "lm_head" in g.name: continue       # lm_head 不挂 LoRA
    # 追加两个图输入
    lora_a = graph.input(f"{stem}.lora_A.weight", shape=[k, rank])
    lora_b = graph.input(f"{stem}.lora_B.weight", shape=[rank, n])
    # 旁路：mid = input @ lora_a ; out = mid @ lora_b
    # 最终：final = original_gemm_output + out，并把原输出的消费者改接到 final
graph.cleanup().toposort().fold_constants()
save(lora_model.onnx)
```

5 类 GEMM 的匹配分别由独立函数负责，调度入口是 `_match_gemm_infos`：

- FP8：找带 FP8 `output_dtype` 的 `QuantizeLinear`，沿 `Q→DQ→MatMul` 扇出收集所有 MatMul。
- NVFP4：找 `TRT_FP4DynamicQuantize` 节点。
- INT4：找 `Int4GroupwiseGemmPlugin` 节点（权重直接挂在 `inputs[1]`）。
- MXFP8：找 `TRT_MXFP8DynamicQuantize` 节点。
- FP16：找权重为常量的普通 `MatMul`。

#### 4.2.3 源码精读

**主入口与图插入循环**：[tensorrt_edgellm/lora/lora.py:400-488](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/lora/lora.py#L400-L488)。关键片段（旁路构造与消费者改接）：

```python
# 第一个 MatMul: input @ lora_A
graph.layer(..., op="MatMul", inputs=[input_tensor, lora_a], outputs=[lora_mid])
# 第二个 MatMul: (input @ lora_A) @ lora_B
graph.layer(..., op="MatMul", inputs=[lora_mid, lora_b], outputs=[lora_out])
# 把 LoRA 输出加到原 GEMM 输出上
graph.layer(..., op="Add", inputs=[output_tensor, lora_out], outputs=[final_output])
```

注意 L463-475 的消费者改接很小心：只改接「原本消费 GEMM 输出」的那些节点，且保持输入下标顺序，避免给多输入算子（如 Reshape 的 data/shape）插错位置造成图环或形状推断失败。

**5 类匹配的调度**：[tensorrt_edgellm/lora/lora.py:300-310](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/lora/lora.py#L300-L310) — `_match_gemm_infos` 把五类结果拼成一个列表。

**命名派生（关键设计）**：dynamo 导出器给 MatMul 起的名字是机械的 `node_MatMul_N`，无法对应回模块路径。于是 EdgeLLM 反向追踪 MatMul 权重侧到 `_model.<...>.weight` 这个 initializer，剥出模块路径 stem，再编码成路径式 GEMM 名，见 `_stem_from_weight_init` [tensorrt_edgellm/lora/lora.py:152-169](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/lora/lora.py#L152-L169) 与 `_synth_gemm_name` [tensorrt_edgellm/lora/lora.py:142-149](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/lora/lora.py#L142-L149)。这个 stem 必须和下一节 `process-lora` 产出的权重 key 严格对齐——它就是 Python 端与运行时之间的命名契约。

**命令入口**：[tensorrt_edgellm/scripts/insert_lora.py:34-62](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/scripts/insert_lora.py#L34-L62) — `main()` 只有一个参数 `--onnx_dir`，转调 `insert_lora_and_save`。这正是 u1-l4 讲过的「薄封装 CLI」模式。

#### 4.2.4 代码实践

1. **实践目标**：直观看到一个 LoRA 槽位长什么样。
2. **操作步骤**：假设你已按 u1-l5 用 `tensorrt-edgellm-export` 导出了某模型的 `model.onnx`。运行：
   ```bash
   tensorrt-edgellm-insert-lora --onnx_dir /tmp/onnx_output/llm
   ```
   然后用 Python 检查产出的 `lora_model.onnx` 的图输入：
   ```python
   import onnx
   m = onnx.load("/tmp/onnx_output/llm/lora_model.onnx")
   lora_inputs = [i.name for i in m.graph.input if "lora_" in i.name]
   print(len(lora_inputs), lora_inputs[:4])
   ```
3. **观察现象**：图输入里多出成对的 `...lora_A.weight` / `...lora_B.weight`；原 `model.onnx` 不变，新图写到 `lora_model.onnx`。
4. **预期结果**：LoRA 输入数量 = （可适配线性层数 × 2），且不应出现 `lm_head` 相关条目（见 L429-430 的过滤）。
5. 如无 GPU/无法导出真实模型，可改为「源码阅读型实践」：在 `_match_gemm_infos` 里数一数它调用了几个 `_match_*_gemm` 函数，分别对应哪类量化。

#### 4.2.5 小练习与答案

- **练习 1**：为什么 LoRA 输入要做成「图输入」而不是图里的常量？
  - **答案**：做成图输入后，权重可以在运行时由 `LoRAManager` 动态填入，从而支持不重建 engine 就切换 adapter；若是常量就得每次合并/重建。
- **练习 2**：`lm_head` 为什么不挂 LoRA？
  - **答案**：`insert_lora_and_save` 在 L429-430 显式 `if "lm_head" in gemm_name: continue` 跳过；词表投影层通常不需要按 adapter 适配，且它维度巨大、挂 LoRA 收益小开销大。

---

### 4.3 adapter 权重处理与静态合并（`process-lora` / `merge-lora`）

#### 4.3.1 概念说明

这一节覆盖 `lora.py` 里另外两个函数，它们服务于不同的路径：

- **`process_lora_weights_and_save`（动态路径）**：HuggingFace 的 PEFT adapter 用一套自己的命名（`base_model.model.<...>.lora_A.weight`）、自带 `lora_alpha` 与 `r`、可能是 bf16、`lora_A`/`lora_B` 的轴序也可能和运行时期望的不一致。这个函数把 adapter「翻译」成运行时 sidecar：重命名 key、乘入 \(\alpha/r\)、校正形状、转 fp16。产物 `processed_adapter_model.safetensors` 的 key 必须与 4.2 里插入图时的 stem **逐字对齐**，否则运行时绑定不上。
- **`merge_lora_and_save`（静态路径）**：调用 `peft.PeftModel.merge_and_unload`，把 \(\Delta W = \frac{\alpha}{r}BA\) 直接加进 base 权重，落盘成一份新检查点。对 Phi-4-Multimodal 这种「建模代码内置了 vision_lora/speech_lora 配置」的模型，合并后还要把这些配置字段置空。

#### 4.3.2 核心流程

`process_lora_weights_and_save` 的处理管线（[tensorrt_edgellm/lora/lora.py:605-657](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/lora/lora.py#L605-L657)）：

```
读 adapter_config.json → (lora_alpha, r)
for 每个 tensor key:
    若是 norm / lm_head → 丢弃            (_should_keep_tensor)
    剥掉 base_model.model. 前缀，补齐 model. (_process_tensor_name)
    处理张量：
        lora_B *= alpha / r                (把缩放烤进 B)
        校正 lora_A/lora_B 轴序使其符合 [k,r]/[r,n]
        转 fp16 + contiguous
写出 processed_adapter_model.safetensors + config.json
```

`merge_lora_and_save` 的流程（[tensorrt_edgellm/lora/lora.py:536-602](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/lora/lora.py#L536-L602)）：安全校验输出目录 → 加载 base 模型（Phi4MM 走专用加载器）→ `PeftModel.from_pretrained` → `merge_and_unload` → Phi4MM 置空 vision/speech_lora → `save_pretrained` → 拷贝分词器与 processor。

#### 4.3.3 源码精读

**缩放与形状校正**：[tensorrt_edgellm/lora/lora.py:363-396](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/lora/lora.py#L363-L396)。

```python
if 'lora_B.weight' in key:
    tensor = tensor * (lora_alpha / r)     # 预乘缩放，运行时图里就不用 scale 节点
if 'lora_A.weight' in key:
    if tensor.shape[-1] != r: tensor = tensor.transpose(-2, -1)
elif 'lora_B.weight' in key:
    if tensor.shape[0] != r: tensor = tensor.transpose(-2, -1)
tensor = tensor.to(torch.float16).contiguous()
```

这就是 4.1 里「\(\alpha/r\) 烤进 B」的落点。运行时图（4.2 插入的两个 MatMul）因此不需要任何 scale 节点。

**key 过滤与改名**：`_should_keep_tensor` [tensorrt_edgellm/lora/lora.py:346-360](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/lora/lora.py#L346-L360)（丢 norm / lm_head）与 `_process_tensor_name` [tensorrt_edgellm/lora/lora.py:329-343](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/lora/lora.py#L329-L343)（剥前缀）。

**静态合并的输出目录安全护栏**：`_prepare_merge_output_dir` [tensorrt_edgellm/lora/lora.py:507-533](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/lora/lora.py#L507-L533) 会拒绝删除根目录、家目录、当前目录，以及任何「包含输入模型或 adapter 目录」的输出路径——因为合并会 `rmtree` 已存在的输出目录，这个护栏防误删。

**Phi-4-Multimodal 合并的特殊处理**：[tensorrt_edgellm/lora/lora.py:559-581](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/lora/lora.py#L559-L581)。它先用 `load_phi4mm_model`（[tensorrt_edgellm/lora/phi4mm_utils.py:144-165](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/lora/phi4mm_utils.py#L144-L165)）加载模型，合并后把 `merged_model.config.vision_lora = None`、`speech_lora = None`。这点是 4.5 综合实践的核心，详见那里。

**命令入口**：
- `process-lora`：[tensorrt_edgellm/scripts/process_lora_weights.py:33-64](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/scripts/process_lora_weights.py#L33-L64)，参数 `--input_dir`（含 `adapter_config.json` 与 `adapter_model.safetensors`）/ `--output_dir`。
- `merge-lora`：[tensorrt_edgellm/scripts/merge_lora.py:26-59](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/scripts/merge_lora.py#L26-L59)，参数 `--model_dir` / `--lora_dir` / `--output_dir` / `--device` / `--torch-dtype`。

#### 4.3.4 代码实践

1. **实践目标**：验证 process-lora 产物的 key 与 insert-lora 的图输入对齐。
2. **操作步骤**：准备一个 PEFT adapter 目录（含 `adapter_config.json`、`adapter_model.safetensors`），运行：
   ```bash
   tensorrt-edgellm-process-lora \
     --input_dir /path/to/adapter1 \
     --output_dir /tmp/onnx_output/llm/lora_weights/adapter1
   ```
   然后对比产物 key 与 4.2.4 里 `lora_model.onnx` 的图输入名。
   ```python
   from safetensors import safe_open
   with safe_open("/tmp/onnx_output/llm/lora_weights/adapter1/processed_adapter_model.safetensors", framework="pt") as f:
       keys = list(f.keys())
   print(keys[:4])
   ```
3. **观察现象**：产物 key 形如 `model.layers.0.self_attn.q_proj.lora_A.weight`，应与 ONNX 图输入一一对应；`processed_adapter_model.safetensors` 全是 fp16。
4. **预期结果**：两边 key 集合一致（除秩维度的符号差异）；若不一致，运行时 `LoRAManager::refreshTensorMap` 会因名字不匹配而落到 dummy 张量（见 4.4.3），adapter 形同虚设。
5. 若拿不到真实 adapter，可做源码阅读型实践：对照 `_process_tensor_name` 与 4.2 的 `_synth_gemm_name`，手工推演一个 `base_model.model.layers.0.self_attn.q_proj.lora_A.weight` 最终会变成什么 key，确认两边能对上。

#### 4.3.5 小练习与答案

- **练习 1**：为什么 `process-lora` 要把 `lora_B` 乘以 `alpha/r`，而运行时图里没有 scale 节点？
  - **答案**：把常数缩放预乘进权重，简化运行时图（只需两个 MatMul）；这与 4.2 插入的图（无 scale 节点）是配套设计。
- **练习 2**：`merge-lora` 与 `process-lora` 产出的检查点，哪个还能再被量化？
  - **答案**：`merge-lora` 产出的是完整 HF 检查点，可继续 `quantize`/`export`；`process-lora` 产出的是 runtime sidecar（只有 lora_A/B 碎片），不能再当检查点量化。

---

### 4.4 运行时动态切换 adapter（`LoRAManager`）

#### 4.4.1 概念说明

动态 LoRA 的运行时核心是 `LoRAManager`（`cpp/runtime/state/loraManager.{h,cpp}`）。它的职责用一个字概括就是**「换指针」**：所有 adapter 在构造期一次性加载到显存，之后切换 adapter 只是改一个 `mActiveAdapterName` 字符串，把 `TensorMap` 里 LoRA 条目指向新 adapter 的张量地址——**零 GPU 拷贝、O(1)**。类注释把这点说得很清楚（[cpp/runtime/state/loraManager.h:41-52](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/state/loraManager.h#L41-L52)）。

要理解运行时切换，还要补一块构建期的桥接知识：`--maxLoraRank` 让构建器（a）选择解析 `lora_model.onnx` 而非 `model.onnx`，（b）用 `setupLoraProfiles` 给 LoRA 维度配优化 profile。没有这一步，engine 根本不会有 `lora_A*`/`lora_B*` 输入，运行时也就无从绑定。

#### 4.4.2 核心流程

**启动期（构造 runtime）**：

```
parseInputFile(input.json)
  ├─ available_lora_weights: {name → path}   (requestFileParser.cpp L134-146)
  └─ 每个 request 的 lora_name                (L198-214，校验名字存在、同批必须同名)
LLMInferenceRuntime(engineDir, ..., loraWeightsMap, stream)
  └─ SharedResources::create (... loraWeightsMap ...)
       └─ if maxSupportedLoraRank>0: LoRAManager + 对每个 (name,path) loadWeights  (sharedResources.cpp L130-142)
  └─ LoRAManager::initializeEngineBindings(baseExecutor)   (llmInferenceRuntime.cpp L243)
       └─ 从引擎 I/O 收集 lora_* 绑定名，为每个造 rank=1 零值 dummy
  └─ LoRAManager::refreshTensorMap(baseTensorMap)           (L244)
```

**每请求（handleRequest → setUpForPrefillExecution）**：

```
if context.loraWeightsName 为空:  resetWeights()   (绑定 dummy，等价无 LoRA)
else:                              switchWeights(name)
refreshTensorMap(baseTensorMap)    —— 把 lora_* 条目重指向当前 adapter 或 dummy
```

**CUDA graph 捕获**：因为 graph 把「绑定地址」录死，每个 adapter（含「无 adapter」）必须各录一张图，见 `captureBaseGraphWithLoraFanout`。

#### 4.4.3 源码精读

**绑定名约定**：[cpp/common/bindingNames.h:584-598](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/common/bindingNames.h#L584-L598) 定义 `kLoraAPrefix="lora_A"`、`kLoraBPrefix="lora_B"`，并约定形状：`lora_A` 为 `[k, rank]`、`lora_B` 为 `[rank, n]`。判定函数 `isLoraBinding` 见 [cpp/common/bindingNames.h:736-739](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/common/bindingNames.h#L736-L739)。

**adapter 加载**：[cpp/runtime/state/loraManager.cpp:56-74](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/state/loraManager.cpp#L56-L74)。`loadWeights` 用 `safetensors::loadSafetensors` 把每个张量按其名字（即绑定名）存进 `mAdapters[name]`。构造期由 `SharedResources` 对 `loraWeightsMap` 逐项调用，见 [cpp/runtime/state/sharedResources.cpp:130-142](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/state/sharedResources.cpp#L130-L142)（仅当 `maxSupportedLoraRank > 0` 才创建 manager）。

**切换 / 复位**：`switchWeights` [loraManager.cpp:82-88](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/state/loraManager.cpp#L82-L88) 只改 `mActiveAdapterName`；`resetWeights` [loraManager.cpp:90-94](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/state/loraManager.cpp#L90-L94) 把它清空。两者都不碰显存。

**rank=1 dummy 的来由**：`initializeEngineBindings` [loraManager.cpp:170-208](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/state/loraManager.cpp#L170-L208)。它从引擎收集所有 `lora_*` I/O 名，按 profile 的 max 形状为每个绑定造一个 **rank=1 的零值 dummy**（`lora_A` 末维设 1、`lora_B` 首维设 1，L182-191），并 `cudaMemset` 清零。rank=1 是因为 LoRA 旁路是 `A[k,1] @ B[1,n]`，零值的 1 秩矩阵乘出来仍是零，等价于「无 LoRA」且不破坏形状契约。

**刷新 TensorMap（含融合层的兜底）**：`refreshTensorMap` [loraManager.cpp:210-230](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/state/loraManager.cpp#L210-L230)。对每个引擎 LoRA 绑定名：若当前 adapter 有对应权重则绑定它，否则绑定 dummy。注释（L214-218）解释了一个真实坑：融合引擎用 `qkv_proj.*` 命名，而 adapter 可能是分开的 `q_proj.*`/`k_proj.*`/`v_proj.*`，名字对不上时就落到 dummy——这就是 4.3 强调「key 必须逐字对齐」的运行时后果。

**请求里的 loraWeightsName 字段**：请求结构 `LLMGenerationRequest::loraWeightsName` 定义在 [cpp/runtime/llmRuntimeUtils.h:132](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/llmRuntimeUtils.h#L132)，默认空串表示不用 LoRA；它被拷进解码上下文 `DecodingInferenceContext::loraWeightsName`（[cpp/runtime/state/decodingInferenceContext.h:95](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/state/decodingInferenceContext.h#L95)）。

**每请求切换的调用点**：`setUpForPrefillExecution` [cpp/runtime/llmInferenceRuntime.cpp:1623-1643](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/llmInferenceRuntime.cpp#L1623-L1643)。仅当 `maxSupportedLoraRank > 0` 且 manager 存在时执行：名字空则 `resetWeights`，否则 `switchWeights`，最后 `refreshTensorMap`，并用 try/catch 把切换失败降级为返回 false。

**CUDA graph 按 adapter 分图**：`captureBaseGraphWithLoraFanout` [cpp/runtime/llmInferenceRuntime.cpp:1497-1528](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/llmInferenceRuntime.cpp#L1497-L1528)。先为「无 adapter」录一张，再遍历 `getAdapterNames()` 为每个 adapter 各录一张（L1519-1525）。这与官方文档「CUDA graphs are captured separately for each LoRA configuration」（[lora.md:166](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/docs/source/user_guide/features/lora.md#L166)）一致——这与 u5-l3 讲过的「绑定哈希含权重地址」机制是一回事：每个 adapter 的权重地址不同，哈希不同，自然各占一张图。

**构建期桥接**：`--maxLoraRank > 0` 时，构建器解析 `lora_model.onnx`（[cpp/builder/llmBuilder.cpp:197-200](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/builder/llmBuilder.cpp#L197-L200)），并调用 `setupLoraProfiles`（[llmBuilder.cpp:1041-1115](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/builder/llmBuilder.cpp#L1041-L1115)）。profile 的秩维度三元组是 `min=0 / opt=maxLoraRank/2 / max=maxLoraRank`（L1072-1096），让 TensorRT 为最常出现的秩选最优 kernel；若图里找不到任何 LoRA 输入则报错并提示「是否忘了 insert-lora」（L1101-1106）。

#### 4.4.4 代码实践

1. **实践目标**：跟踪一次 adapter 切换在运行时的完整调用链。
2. **操作步骤**：在 [cpp/runtime/llmInferenceRuntime.cpp:1623-1643](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/llmInferenceRuntime.cpp#L1623-L1643) 设断点（或加日志），用如下 `input.json` 跑两次连续请求：
   ```json
   {
     "available_lora_weights": {
       "french": "/abs/path/.../french/processed_adapter_model.safetensors",
       "medical": "/abs/path/.../medical/processed_adapter_model.safetensors"
     },
     "requests": [
       {"messages":[{"role":"user","content":"Translate: Hello"}], "lora_name":"french"},
       {"messages":[{"role":"user","content":"What is aspirin?"}], "lora_name":"medical"}
     ]
   }
   ```
   命令：`./build/examples/llm/llm_inference --engineDir engines --inputFile input.json --outputFile out.json`（构建时记得带 `--maxLoraRank 64`）。
3. **观察现象**：第一条请求进入 prefill 时打印 `switched to adapter 'french'`，第二条打印 `switched to adapter 'medical'`；`getActiveWeight` 全程不分配新显存，只换指针。注意 `requestFileParser` 会强制同一 batch 内所有请求同名（[requestFileParser.cpp:213-214](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/examples/utils/requestFileParser.cpp#L213-L214)），所以切换发生在不同请求/不同 batch 之间。
4. **预期结果**：两次输出风格不同（法语翻译 vs 医学问答），且切换日志显示零拷贝。
5. 如无 GPU：做源码阅读型实践——从 `requestFileParser.cpp:198-208`（设 `lora_name`）追到 `batchRequest.loraWeightsName`（[L406-408](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/examples/utils/requestFileParser.cpp#L406-L408)），再到 `llm_inference.cpp:538-588`（`loraWeightsMap` 传给 runtime 构造），画出数据流图。

#### 4.4.5 小练习与答案

- **练习 1**：为什么「无 adapter」时不直接不绑定 LoRA 输入，而要绑定 rank=1 的零值 dummy？
  - **答案**：TensorRT engine 的输入必须全部绑定（见 `engineExecutor.cpp:188-203` 的校验）；用 rank=1 零值 dummy 既满足绑定契约，又让旁路乘出零、等价于无 LoRA。
- **练习 2**：`switchWeights` 之后必须立刻调一次什么？为什么？
  - **答案**：必须调 `refreshTensorMap`。因为 `TensorMap` 里记录的是地址，`switchWeights` 只改了 manager 内部「当前 adapter」状态，`TensorMap` 里的 LoRA 条目不会自动更新，必须刷新才能让执行器绑到新 adapter 的地址。`setUpForPrefillExecution` 与 `captureBaseGraphWithLoraFanout` 都遵循这个「switch → refresh」成对调用。

---

## 5. 综合实践

本任务把本讲两条主线串起来，对应规格里的核心问题。

### 任务 A：为什么 Phi-4-Multimodal 必须在量化前用 `merge-lora` 静态合并？

1. **阅读建模代码的内置 LoRA 线索**：看 [tensorrt_edgellm/lora/phi4mm_utils.py:85-86](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/lora/phi4mm_utils.py#L85-L86)——它用 `config_dict.get("vision_lora")` / `speech_lora` 是否非空来判断模型「是否内置了 LoRA」。这说明 Phi-4-Multimodal 的配置里**本来就声明了 vision-lora / speech-lora**，它们是模型视觉/语音能力的**必需组件**，不是可选的运行时能力切换。
2. **看合并后如何处理这两个字段**：[lora.py:578-581](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/lora/lora.py#L578-L581) 在 `merge_and_unload` 之后把 `vision_lora`/`speech_lora` 显式置 `None`。合并把它们焊进权重后，配置就不再「声明有 LoRA」。
3. **得出结论**：Phi-4-Multimodal 的 `vision-lora` 是**始终需要、不可在运行时按请求切换**的（每个请求都需要视觉能力）。动态运行时 LoRA 是为「逐请求选不同 adapter」设计的，用在这里既无意义又增加图复杂度与显存。因此正确做法是静态合并：先 `merge-lora` 焊死 → 再 `quantize`（见 [quantize.py:207-209](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/quantization/quantize.py#L207-L209)，量化阶段也会用 `load_phi4mm_model` 加载）→ 再 `export`。官方流程见 [lora.md:52-90](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/docs/source/user_guide/features/lora.md#L52-L90)。
4. **写出你的解释**：用 3-5 句话讲清「内置必需 adapter vs 运行时可选 adapter」的区别，以及为什么顺序必须是 merge → quantize → export（提示：量化是对权重的校准，必须在权重已包含 LoRA 贡献之后进行）。

### 任务 B：同一 runtime 上用不同 `loraWeightsName` 连续推理时 adapter 如何切换？

用一张时序图描述两个连续请求（`french` → `medical`）在运行时的切换，要求覆盖以下要点（答案对应 4.4 的源码）：

- 构造期：两个 adapter 的 `processed_adapter_model.safetensors` 都被 `loadWeights` 加载进 `mAdapters`（[sharedResources.cpp:134-141](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/state/sharedResources.cpp#L134-L141)），各绑定的 rank=1 dummy 也已造好（[loraManager.cpp:170-208](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/state/loraManager.cpp#L170-L208)）。
- 请求 1（`french`）：`setUpForPrefillExecution` 读到 `context.loraWeightsName="french"` → `switchWeights("french")` → `refreshTensorMap` 把 `lora_*` 条目指向 french 的张量地址 → 执行命中「french 那张」CUDA graph。
- 请求 2（`medical`）：同样路径，`switchWeights("medical")` → `refreshTensorMap` 重指向 medical 的地址 → 命中「medical 那张」CUDA graph。
- 全程**零 GPU 拷贝**：`switchWeights` 只改字符串，地址早在构造期就固定了。

把这张时序图与「切换 = 换指针 + 刷新 TensorMap + 选对应 CUDA graph」的结论写下来，即完成本任务。

> 待本地验证：任务 B 的日志现象需在有 GPU、已构建带 `--maxLoraRank` 的 engine、且备好两个真实 adapter 的环境下才能观察到。无此环境时，按 4.4.4 的源码阅读型实践完成调用链追踪即可。

## 6. 本讲小结

- EdgeLLM 的 LoRA 有**两条互斥路径**：动态运行时 LoRA（图插入 + sidecar + 逐请求切换）与静态合并（焊进检查点后走普通流水线）。
- `insert_lora_and_save` 给图里每一类量化 GEMM（FP16/FP8/NVFP4/INT4/MXFP8）并联一条 `MatMul(lora_A) → MatMul(lora_B) → Add` 旁路，并把权重暴露为图输入；GEMM 名由权重 initializer 反推 stem，是 Python 与运行时的命名契约。
- `process_lora_weights_and_save` 把 PEFT adapter 翻译成 runtime sidecar：剥前缀、丢 norm/lm_head、把 \(\alpha/r\) 预乘进 `lora_B`、校正轴序、转 fp16；产物 key 必须与图输入逐字对齐。
- `merge_lora_and_save` 用 PEFT `merge_and_unload` 永久合并 adapter，对 Phi-4-Multimodal 还会把 `vision_lora`/`speech_lora` 配置置空。
- 运行时 `LoRAManager` 用「构造期一次性加载 + 换指针 + rank=1 零值 dummy」实现 O(1) 零拷贝切换；切换后必须 `refreshTensorMap`，且每个 adapter 各录一张 CUDA graph。
- Phi-4-Multimodal 的 `vision-lora` 是内置必需组件，必须在量化前静态合并，不能用动态运行时 LoRA。

## 7. 下一步学习建议

- **系统提示 KV 缓存与 LoRA 的交互**：本讲已埋下一个钩子——系统提示缓存的键里含有 `loraWeightsName`（`keySystemPromptWithLoraWeights`，见 [llmInferenceRuntime.cpp:1661](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/llmInferenceRuntime.cpp#L1661)），即不同 adapter 的系统提示缓存是隔离的。下一讲 **u9-l2（系统提示 KV 缓存与流式）** 会展开这一点。
- **深入 C++ 执行器与 TensorMap**：若想彻底理解 `refreshTensorMap` 改的到底是什么，回看 **u5-l3（引擎执行器与张量注册表）**，重点是 LoRA 权重为何「不进 TensorRegistry 而走 TensorMap 兜底绑定」。
- **接入新模型时的 LoRA 考量**：**u9-l4（接入一个新模型架构）** 会讲到新增模型时要保持权重 key 与 safetensors 兼容——这正是 LoRA 命名契约能成立的前提，可结合本讲的 stem 派生逻辑一起读。
- **扩展阅读源码**：直接通读 [tensorrt_edgellm/lora/lora.py](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/lora/lora.py) 与 [cpp/runtime/state/loraManager.cpp](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/cpp/runtime/state/loraManager.cpp)，两个文件加起来不足千行，是理解全链路最直接的入口。
