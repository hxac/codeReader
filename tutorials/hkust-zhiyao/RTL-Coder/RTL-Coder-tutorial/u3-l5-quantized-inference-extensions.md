# 讲义：量化推理与二次开发扩展

## 1. 本讲目标

本讲是专家层（u3）的收尾，也是整个学习手册的最后一篇。前 17 篇讲义带你读懂了 RTL-Coder 的「数据生成 → 训练 → 评测」三大主链路，但默认的推理与训练脚本都假设你有一张显存充裕的 GPU，且只生成论文既定的数据集与基准。本讲要回答两个工程问题：

1. **没有大显存 GPU（甚至只有 CPU）怎么跑 RTLCoder？** —— 掌握项目发布的两个量化模型：GPU 上的 GPTQ-4bit、CPU 上的 GGUF-4bit。
2. **如何把 RTL-Coder 用到自己的场景？** —— 掌握两条扩展通道：改 `p_example.txt` 的 `#method#` 造自己的数据集；改 `design_list` 与基准数据适配新评测。

学完后你应该能够：

- 说出 GPTQ 与 GGUF 两种 4-bit 量化的定位差异、各自的加载入口与适用硬件。
- 写出 GGUF CPU 推理脚本，并能用 `gpu_layers` 在「纯 CPU / 部分 offload」之间切换。
- 看懂 `p_example.txt` 中 `#method#` 的作用，设计一条新的变异策略来扩展数据集。
- 理解 `test_on_rtllm.py` 里 `design_list` 的子串匹配落盘机制，能为新设计添加关键字并讨论其误分类风险。

## 2. 前置知识

本讲依赖三篇前置讲义，这里只做最小承接，不重复细节：

- **u1-l3 快速上手推理**：建立了「AutoTokenizer + AutoModelForCausalLM + `model.generate` 采样」的 fp16 GPU 推理范式，以及 `temperature` / `top_p` / `max_length` 的含义。本讲的量化推理只是把「加载模型」这一步换掉，采样逻辑完全一致。
- **u2-l2 变异提示词模板**：建立了 `p_example.txt` 的三段结构（角色设定 / `#method#` / 单样本示例），以及 `#...#`（段落标记）与 `{...}`（字段占位符）两套符号的区别。本讲第三模块直接在其上做扩展。
- **u2-l8 基准评测推理脚本**：建立了 `test_on_rtllm.py` 的「读 `rtllm-1.1.json` → 拼指令+骨架 → 生成 → 抽取 → 落盘」骨架，以及 `design_list` 关键字匹配的初步印象。本讲第四模块打开它的扩展点。

本讲会用到的两个量化术语，先给直觉解释：

- **量化（Quantization）**：把模型权重从 16 位浮点（FP16）压成 4 位整数（INT4），显存/内存占用降到约 1/4，代价是精度小幅下降。RTLCoder 的底座 Mistral-7B 在 FP16 下约需 13–14 GB，4-bit 量化后约 4–5 GB。
- **后训练量化（PTQ, Post-Training Quantization）**：不重新训练，只在已有模型上做一次性压缩。GPTQ 就是一种 PTQ 方法。

## 3. 本讲源码地图

本讲围绕三个文件展开，外加 RTLLM 基准数据：

| 文件 | 作用 | 本讲用它做什么 |
|------|------|----------------|
| `README.md` | 项目总入口，含四种模型的推理示例 | 提供 GPTQ / GGUF 两段量化推理示例代码（模块 1、2） |
| `data_generation/p_example.txt` | 数据生成的变异提示词模板 | 改 `#method#` 扩展数据集方向（模块 3） |
| `data_generation/instruction_gen.py` | 指令生成主循环，加载并使用 `p_example.txt` | 说明模板如何被读入、如何换成自定义模板（模块 3） |
| `benchmark_inference/test_on_rtllm.py` | RTLLM 基准推理脚本 | 改 `design_list` 适配新设计/新基准（模块 4） |
| `benchmark_inference/rtllm-1.1.json` | RTLLM 29 道题的指令+骨架 | 新增基准题目的落点（模块 4） |

永久链接 base（当前 HEAD）：

```
https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/
```

## 4. 核心概念与源码讲解

### 4.1 GPTQ 4-bit：GPU 上的显存压缩推理

#### 4.1.1 概念说明

RTL-Coder 发布了四个 HuggingFace 模型，定位各不相同（[README.md:50-56](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/README.md#L50-L56)）：

1. `RTLCoder-Deepseek-v1.1`：基于 DeepSeek-coder-6.7b，精度最高、推理较慢、不会自动停止。
2. `RTLCoder-v1.1`：基于 Mistral-v0.1，默认推荐。
3. `RTLCoder-v1.1-gptq-4bit`：上者的 **GPTQ 4-bit 量化版**，本模块主角。
4. `RTLCoder-v1.1-gguf-4bit`：**GGUF 4-bit 量化版**，可跑 CPU，模块 2 主角。

**GPTQ** 是一种基于二阶信息（Hessian）的后训练量化算法。直觉上：逐列量化权重时，用量化误差除以一个「该列权重的敏感度」（由校准数据 \(X\) 算出的 Hessian 近似给出），优先在敏感度高的方向补偿误差。其优化目标可写成：

\[
\arg\min_{\hat{W}} \; \| W X - \hat{W} X \|_F^2
\]

其中 \(W\) 是原始权重，\(\hat{W}\) 是量化后权重，\(X\) 是少量校准样本的激活。GPTQ 不在本仓库实现，由 [AutoGPTQ](https://github.com/AutoGPTQ/AutoGPTQ) 库提供；仓库只给出**消费侧**的加载与推理代码。

GPTQ 模型仍然跑在 **GPU** 上（`device="cuda:0"`），只是显存占用从约 13 GB 降到约 4–5 GB，让单张消费级显卡（如 8 GB 的 RTX 4060 / 3070）也能推理。这与 GGUF「完全脱离 GPU」是两条不同的省钱路线。

#### 4.1.2 核心流程

GPTQ 推理流程相比 u1-l3 的 fp16 推理，只替换了「模型加载」一步：

1. `AutoTokenizer.from_pretrained(..., use_fast=True)`：加载分词器（注意显式 `use_fast=True`）。
2. `AutoGPTQForCausalLM.from_quantized(repo, device="cuda:0")`：从 HF Hub 直接拉量化好的模型并放到 GPU。
3. `model.eval()`：关闭 dropout。
4. `tokenizer(prompt, return_tensors="pt").to(0)`：编码并搬到 0 号卡。
5. `model.generate(**inputs, max_length=512, ...)`：采样生成。
6. `tokenizer.decode(sample[0])`：解码回文本。

采样参数 `temperature=0.5, top_p=0.9` 与 u1-l3 完全一致；同样地，这里没写 `do_sample=True`，严格意义上仍是贪心解码（温度被忽略）。后处理（`endmodule` 截断）也与 u1-l5 一致，此处 README 示例为简洁省略了。

#### 4.1.3 源码精读

GPTQ 推理示例在 README 的 Inference demo 末段（[README.md:188-202](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/README.md#L188-L202)）：

```python
from transformers import AutoTokenizer
from [auto_gptq](...) import AutoGPTQForCausalLM, BaseQuantizeConfig   # ← 这一行有问题，见下
prompt = "Please act as a professional verilog designer and provide a half adder. ..."

tokenizer = AutoTokenizer.from_pretrained("ishorn5/RTLCoder-v1.1-gptq-4bit", use_fast=True)
model = AutoGPTQForCausalLM.from_quantized("ishorn5/RTLCoder-v1.1-gptq-4bit", device="cuda:0")
model.eval()
inputs = tokenizer(prompt, return_tensors="pt").to(0)
sample = model.generate(**inputs, max_length=512, temperature=0.5, top_p=0.9)
print(tokenizer.decode(sample[0]))
```

关键点解读：

- 第 196 行 `from_quantized(..., device="cuda:0")` 是 GPTQ 的核心入口：它读取仓库里预先量化好的 `.safetensors` 权重与 `quantize_config.json`，在加载时反量化到 GPU。`BaseQuantizeConfig` 在这段消费代码里其实没被用到，导入它是为了在「自己量化」场景备用。
- 第 195 行 `use_fast=True`：GPTQ 推理推荐 fast tokenizer，与 fp16 默认路径略有差异。

> ⚠️ **README 有两处笔误**（[README.md:188-191](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/README.md#L188-L191)），运行前必须手动修正：
>
> 1. **import 行是坏的**：`from [auto_gptq](https://github.com/marella/ctransformers) import ...` 是 Markdown 链接渲染残留，且链接还错指向了 ctransformers。正确写法是 `from auto_gptq import AutoGPTQForCausalLM, BaseQuantizeConfig`。
> 2. **依赖提示链接错位**：README 正文写「please make sure to have the package [ctransformers](AutoGPTQ)」，链接文字与目标对不上，应指向 `https://github.com/AutoGPTQ/AutoGPTQ`。
>
> 这两处都是 README 排版 bug，不代表 GPTQ 本身有问题。修正后即可正常运行。

#### 4.1.4 代码实践

**实践目标**：把 README 里无法直接运行的 GPTQ 示例改成可运行版本，并在一张小显存 GPU 上验证它能出 Verilog 代码。

**操作步骤**：

1. 安装依赖（待本地验证确切版本）：`pip install auto_gptq transformers torch`。
2. 把上面的 import 行修正为 `from auto_gptq import AutoGPTQForCausalLM, BaseQuantizeConfig`。
3. 保留 `device="cuda:0"`，运行脚本。

**需要观察的现象**：模型首次加载会从 HF Hub 下载约 4–5 GB 的量化权重；显存占用峰值应明显低于 fp16 版的 13 GB。

**预期结果**：打印出一段以 `module half_adder` 开头、以 `endmodule` 结尾的 Verilog 代码（可能包含 prompt 回显，可用 `sample[0][len(inputs.input_ids[0]):]` 切掉前缀，与 u1-l3 / u2-l8 的做法一致）。

**若无法确定运行结果**：显存是否足够、AutoGPTQ 与 transformers 的版本兼容性，标注「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：GPTQ-4bit 模型和 fp16 模型跑在同样的 GPU 上，最大的区别是什么？
**答案**：显存占用。GPTQ 把权重量化到 4-bit，约省 3/4 权重显存（Mistral-7B 从 ~13 GB 降到 ~4–5 GB），使小显存 GPU 也能加载；精度会有小幅损失，但对 Verilog 生成这种结构化任务影响通常较小。

**练习 2**：README 的 GPTQ 示例里 `temperature=0.5, top_p=0.9` 真的生效了吗？
**答案**：严格说没有。因为没有传 `do_sample=True`，HuggingFace 默认走贪心解码，温度与 top_p 被忽略。要真正采样需显式加 `do_sample=True`（与 u1-l3 同一坑）。

---

### 4.2 GGUF 4-bit：CPU 上的轻量推理

#### 4.2.1 概念说明

如果说 GPTQ 是「让小 GPU 也能跑」，那么 **GGUF 是「连 GPU 都不需要」**。README 的原话是（[README.md:132](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/README.md#L132)）：

> If you don't have a GPU with more than 4 GB memory, please try the quantized 4-bit version which could run on CPU.

**GGUF**（GPT-Generated Unified Format）是 `llama.cpp` / `ctransformers` 生态使用的模型文件格式，把权重、词表、超参全打包进单个 `.gguf` 文件。RTLCoder 的 GGUF 版用的是 `q4_0` 量化（文件名 `ggml-model-q4_0.gguf`），是最早、最快的 4-bit 量化方案之一：每 32 个权重共用一个缩放因子，简单高效。

本仓库不实现量化转换，GGUF 文件由模型作者离线转换后上传到 `ishorn5/RTLCoder-v1.1-gguf-4bit`，仓库只给消费侧代码。运行时依赖 [ctransformers](https://github.com/marella/ctransformers)——一个把 `llama.cpp` C++ 后端包装成 HuggingFace 风格 API 的 Python 库。

GGUF 路径最大的工程价值是 **`gpu_layers` 参数**：它允许把模型的若干层 offload 到 GPU、其余留在 CPU，从而在「纯 CPU 慢但零门槛」与「全 GPU 快但需显存」之间连续调节。`gpu_layers=0` 即纯 CPU。

#### 4.2.2 核心流程

GGUF 推理流程与前面的 transformers 范式**形似而神不同**：

1. `from ctransformers import AutoModelForCausalLM`：注意是从 **ctransformers** 导入，不是 transformers（同名类，不同库）。
2. `AutoModelForCausalLM.from_pretrained(model_path, model_type="mistral", gpu_layers=0, ...)`：一次性把生成超参（`max_new_tokens` / `context_length` / `temperature` / `top_p`）传进加载函数。
3. `llm(prompt)`：**直接调用模型对象**即可生成，无需 `tokenizer()` + `model.generate()` 两步——ctransformers 内部封装了分词与采样。

注意几个与 transformers 路径的差异：

- 超参在**加载时**传入（`from_pretrained` 的 kwargs），而非 `generate` 时。
- 上下文长度参数叫 `context_length`（这里是 6048，一个不太规整的值），对应 transformers 的 `max_length`。
- 没有显式的 `return_tensors` / `device`，ctransformers 自己管张量与设备。

#### 4.2.3 源码精读

GGUF 推理示例在 README 的 Inference demo 开头（[README.md:134-143](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/README.md#L134-L143)）：

```python
from ctransformers import AutoModelForCausalLM
model_path = 'ggml-model-q4_0.gguf'
# Set gpu_layers to the number of layers to offload to GPU. Set to 0 if no GPU acceleration is available.
llm = AutoModelForCausalLM.from_pretrained(
    model_path, model_type="mistral", gpu_layers=0,
    max_new_tokens=2000, context_length=6048, temperature=0.5, top_p=0.95,)
prompt = "Please act as a professional verilog designer and provide a half adder. \nmodule half_adder\n..."
print(llm(prompt))
```

关键点解读：

- `model_path = 'ggml-model-q4_0.gguf'`：指向单个 GGUF 文件（需从 HF 仓库 `ishorn5/RTLCoder-v1.1-gguf-4bit` 下载到本地）。`q4_0` 即 4-bit 量化类型。
- `model_type="mistral"`：告诉 ctransformers 后端用 Mistral 的网络结构（因为 RTLCoder-v1.1 基于 Mistral-v0.1，见 u1-l1 / [README.md:54](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/README.md#L54)）。这与 GPTQ 路径隐式从 `config.json` 读结构不同，GGUF 需要显式声明。
- `gpu_layers=0`：纯 CPU；改成具体层数（如 10、20）即可部分 offload 到 GPU。Mistral-7B 有 32 层，`gpu_layers=32` 即全 GPU。
- `max_new_tokens=2000`、`context_length=6048`：生成上限与上下文窗口都设得较大，适合可能输出较长 Verilog 的场景。
- `llm(prompt)`：直接调用即生成，返回字符串。

> 💡 **为什么 GGUF 的 prompt 里要自带 `module half_adder(...)` 骨架？** 与 u1-l3 同理：项目统一范式是「自然语言描述 + 模块签名骨架」，骨架帮模型对齐端口、减少幻觉。

#### 4.2.4 代码实践

**实践目标**：写一个最小的 GGUF CPU 推理脚本，并体会 `gpu_layers` 的调节作用。

**操作步骤**：

1. 从 `https://huggingface.co/ishorn5/RTLCoder-v1.1-gguf-4bit` 下载 `ggml-model-q4_0.gguf` 到本地。
2. `pip install ctransformers`。
3. 用上面的示例代码，`gpu_layers=0` 跑一次半加器（half_adder）prompt。
4. （可选，若机器有 GPU）把 `gpu_layers` 从 0 逐步调大到 32，对比同一 prompt 的生成耗时。

**需要观察的现象**：

- 纯 CPU（`gpu_layers=0`）下生成较慢（每秒若干 token），但确实在产出 Verilog。
- 调大 `gpu_layers` 后生成速度提升、显存占用上升。

**预期结果**：打印出半加器 Verilog 代码；`gpu_layers` 增大时延迟下降。

**若无法确定运行结果**：ctransformers 对新版 Mistral 结构的兼容性、CPU 指令集差异可能影响速度，标注「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：GPTQ 和 GGUF 都叫「4-bit 量化」，它们面向的硬件与运行库分别是什么？
**答案**：GPTQ 面向 **GPU**，用 AutoGPTQ 库 + `device="cuda:0"`；GGUF 面向 **CPU（可部分 offload 到 GPU）**，用 ctransformers 库 + `gpu_layers` 控制 offload 层数。两者都是消费别人量化好的模型，本仓库不做转换。

**练习 2**：为什么 GGUF 路径要在 `from_pretrained` 里显式写 `model_type="mistral"`，而 GPTQ 路径不用？
**答案**：GGUF 单文件里网络结构的元信息不如 HF 的 `config.json` 完整自动识别，ctransformers 需要用户显式告诉它用哪种结构（mistral/llama/...）；GPTQ 沿用 HF `config.json`，能自动推断结构。

---

### 4.3 自定义 `p_example.txt` 的 `#method#` 扩展数据集

#### 4.3.1 概念说明

模块 1、2 解决「省硬件跑现有模型」，模块 3、4 解决「让 RTL-Coder 长出新的能力」。先看数据集扩展。

回顾 u2-l2：RTL-Coder 用 GPT-3.5 自动造出约 2.7 万条「指令-代码」对（Resyn27k），核心驱动力是变异提示词模板 `p_example.txt`。README 明确指出了扩展入口（[README.md:79-86](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/README.md#L79-L86)）：

> You can design your own prompting method by modifying the file **"p_example.txt"** and **"instruction_gen.py"**. You can expand the existing dataset by running `python instruction_gen.py`.

模板的心脏是 `#method#` 占位符——它一句话定义了「怎么把一道已知题变异成新题」。shipped 版的策略是（[p_example.txt:1-3](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/data_generation/p_example.txt#L1-L3)）：

> The rewritten task should achieve **different circuit functionality** but requiring **similar methods or components** in the `#given prompt#`.

即「功能不同、方法/器件相似」。想造不同风味的数据集，**改这一句**是最小入口。例如：

- 「相同功能、不同位宽」：把 8 位加器变 16 位加器，扩充位宽维度。
- 「相同功能、不同时序风格」：组合逻辑变流水线，扩充时序维度。
- 「相同功能、不同编码」：二进制变 BCD/Gray，扩充编码维度。

配合 `#method#` 的还有三条格式约束（[p_example.txt:4-6](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/data_generation/p_example.txt#L4-L6)）：给 `{Instruction}` 加专业设计师前缀、限 100 词、`{Input}` 不写注释。这些约束保证产出仍能被 `post_process_gpt3_response_uniq` 用 `{Instruction}` / `{Input}` 切分（见 u2-l2 / u2-l4），所以**扩展时不能动这两处字面量标记**，只能动 `#method#` 描述与示例。

#### 4.3.2 核心流程

扩展数据集的标准流程：

1. **复制模板**：复制 `p_example.txt` 为 `p_example_bitwidth.txt`（保留原文件）。
2. **改 `#method#`**：把第 3 行换成你的新变异策略一句话描述。
3. **改示例**：把第 8–44 行的 given→rewritten 示例换成符合新策略的一对（示例是 GPT 学习的「格式样本」，必须与新策略一致）。
4. **切换模板**：在 `instruction_gen.py` 第 15–16 行把 `evolv_dic` 指向新文件名。
5. **跑生成**：`python instruction_gen.py`（注意 u2-l4 警告：该文件无 `__main__` 守卫，import 即触发，本地实验需先用 monkey patch 替换 `utils.askGPT35`，否则会真实调用 OpenAI 计费）。

#### 4.3.3 源码精读

模板如何被读入与使用，全部在 `instruction_gen.py`：

**模板注册**（[instruction_gen.py:15-16](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/data_generation/instruction_gen.py#L15-L16)）：

```python
evolv_dic = ['p_example.txt']  # list of mutation method
evo_type = evolv_dic[0]        # choose a mutation method
```

这是一个「变异方法列表」设计：`evolv_dic` 设计成可放多个模板文件，但当前只 shipped 一个。要换模板，改 `evolv_dic[0]` 的文件名即可。`evo_type` 是当前选中的模板路径，全文件都通过它引用。

**主循环里的模板拼接**（[instruction_gen.py:122](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/data_generation/instruction_gen.py#L122)）：

```python
prompt = open(evo_type).read() + "\n"
```

shipped 主循环直接把整个模板文件原文拼成 prompt 发给 GPT（如 u2-l2 / u2-l4 所述，当前并未用 `encode_prompt_uniq` 注入种子）。这意味着：**你改 `#method#` 后，新策略会被原样塞进每一轮 GPT 请求**，立即生效。

**种子注入的备用钩子**（[instruction_gen.py:36-47](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/data_generation/instruction_gen.py#L36-L47)）：

```python
def encode_prompt_uniq(prompt_instructions):
    prompt = open(evo_type).read() + "\n"
    for idx, task_dict in enumerate(prompt_instructions):
        (instruction, input) = task_dict["Instruction"], task_dict["Input"]
        ...
        # prompt += '#given prompt#\n'          # 第 41 行被注释
        prompt += '\n{Instruction}\n' + instruction
        prompt += '\n{Input}\n' + input
    prompt += '\n#Rewritten prompt#\n'
    return prompt
```

这个函数把**种子题**填进模板末尾的第二个 `#given prompt#` 钩子（对应 `p_example.txt` 末尾 [p_example.txt:45-46](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/data_generation/p_example.txt#L45-L46) 的空钩子）。虽然 shipped 主循环没用它，但它是「自定义种子驱动变异」的入口——当你想用自己的一道题作为变异起点时，就走这个函数。

> 🔑 **设计要点**：`#method#` 是「策略旋钮」，`{Instruction}` / `{Input}` 是「格式契约」。扩展时只拧策略旋钮、不碰格式契约，就能既改风味又兼容下游解析。

#### 4.3.4 代码实践

**实践目标**：设计一条「相同功能、不同位宽」的新变异策略，观察它会如何引导数据集方向。

**操作步骤**：

1. 复制 `data_generation/p_example.txt` 为 `p_example_bitwidth.txt`。
2. 把第 3 行的 `#method#` 描述改为：
   > The rewritten task should achieve **the same circuit functionality** but with a **different data bit-width** (e.g., double or half the width) compared to the `#given prompt#`.
3. 把第 8–44 行的 given→rewritten 示例换成一对「同功能不同位宽」的样本（例如 8 位加器 → 16 位加器，保持端口结构、只改位宽）。
4. 在 `instruction_gen.py` 第 15 行改为 `evolv_dic = ['p_example_bitwidth.txt']`。
5. 用 monkey patch 把 `utils.askGPT35` 替换成一个返回固定 fake 回复的本地函数（避免真实计费，方法见 u2-l3 / u2-l4），再 `python instruction_gen.py`。

**需要观察的现象**：

- 切换模板前后，发给 GPT 的 prompt 头部（`#method#` 描述）不同。
- 若放开真实 GPT，产出指令会偏向「位宽变体」（如 `adder_8bit` 变 `adder_16bit` 风格）。

**预期结果**：本地 mock 跑通后，`new_instructions.json` 落盘且每条记录含 `Instruction` / `Input` 字段（真实 GPT 产出方向「待本地验证」）。

#### 4.3.5 小练习与答案

**练习 1**：扩展 `#method#` 时，为什么不能把模板里的 `{Instruction}` 改成 `{任务}`？
**答案**：因为下游 `post_process_gpt3_response_uniq`（见 u2-l4）用 `response.split('{Instruction}', 1)` 和 `split('{Input}', 1)` 这两个字面量来切分 GPT 回复。改了占位符，GPT 即便照抄也切不出来，整条记录会被 `try/except` 丢弃。

**练习 2**：`evolv_dic` 设计成列表（`['p_example.txt']`）有什么扩展含义？
**答案**：它允许注册多个变异模板，通过 `evo_type = evolv_dic[0]` 切换当前用哪个。你可以维护一组不同 `#method#` 的模板文件，按需切换或（改造后）混合采样，实现多策略数据增强。

---

### 4.4 新增 `design_list` / 适配新基准

#### 4.4.1 概念说明

模块 3 扩展「训练数据」，模块 4 扩展「评测基准」。两者是 RTL-Coder 二次开发的两条并行通道。

回顾 u2-l8：`test_on_rtllm.py` 的职责是**让模型把 Verilog 写出来并落盘成 `.v` 文件**，评分（编译、仿真、统计通过率）交给外部 RTLLM 官方仓库。落盘的关键是 `design_list`——一个关键字列表，脚本用**子串匹配**决定一段生成代码该存成哪个文件名。

当前 `design_list` 有 29 个关键字，与 `rtllm-1.1.json` 的 29 道题一一对应（[test_on_rtllm.py:30-35](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/benchmark_inference/test_on_rtllm.py#L30-L35)）：

```python
design_list = ['accu', 'adder_8bit', 'adder_16bit', 'adder_32bit', 'adder_pipe_64bit',
               'asyn_fifo', 'calendar', 'counter_12', 'edge_detect', 'freq_div', 'fsm',
               'JC_counter', 'multi_16bit', 'multi_booth_8bit', 'multi_pipe_4bit',
               'multi_pipe_8bit', 'parallel2serial', 'pe_single', 'pulse_detect', 'radix2_div',
               'RAM_single', 'right_shifter', 'serial2parallel', 'signal_generator',
               'synchronizer', 'alu', 'div_16bit', 'traffic_light', 'width_8to16']
```

要适配新基准或新增设计，有两件事要做：

1. **加题目**：往 `rtllm-1.1.json` 追加一行 `{Instruction, Input}`（与 u1-l4 的格式一致）。
2. **加关键字**：往 `design_list` 追加一个与该题模块名相关的关键字。

#### 4.4.2 核心流程

`test_on_rtllm.py` 的落盘循环（[test_on_rtllm.py:107-113](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/benchmark_inference/test_on_rtllm.py#L107-L113)）：

```python
for result in result_list:
    for keyword in design_list:
        if keyword in result:                              # 子串匹配
            with open(os.path.join(save_path, '{}.v'.format(keyword)), 'w') as f:
                f.write(result)
            break                                          # 命中即停，只存第一个
```

而 `result` 的构造在 [test_on_rtllm.py:104](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/benchmark_inference/test_on_rtllm.py#L104)：

```python
result_list.append(inp_list[res_i] + s)      # Input 骨架 + 抽取后的生成代码
```

其中 `inp_list[res_i]` 是题目的 `Input` 字段（`module accu(...)` 骨架），`s` 是 u1-l5 的 Mistral 版抽取结果。所以匹配对象是「骨架 + 生成代码」的拼接文本。

由此推出扩展与风险：

- **扩展新设计**：新题的 `Input` 里写 `module my_fifo(...)`，就在 `design_list` 加 `'my_fifo'`，落盘成 `my_fifo.v`。
- **子串匹配的误分类风险**（u2-l8 已点出）：`design_list` 是**顺序敏感**的，`for keyword in design_list` 遇到第一个命中就 `break`。短关键字（如 `'alu'`、`'accu'`、`'fsm'`）容易误命中——例如一道含 `accumulator` 字样的题会被 `'accu'` 抢先匹配，存成 `accu.v`。新增关键字时应：① 尽量用足够长、足够特异的名字（如 `fifo_my` 而非 `fifo`）；② 把更特异的关键字排在 `design_list` 前面。

#### 4.4.3 源码精读

题目的读取与 prompt 构造（[test_on_rtllm.py:68-69](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/benchmark_inference/test_on_rtllm.py#L68-L69)）：

```python
dic = bench_data[id - 1]
prompt = dic['Instruction'] + '\n' + dic['Input'] + '\n'
```

每道题来自 `rtllm-1.1.json` 的一行，字段是 `{Instruction, Input}`（与 u1-l4 / [rtllm-1.1.json:1](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/benchmark_inference/rtllm-1.1.json#L1) 一致）。新增题目只需保证这两个字段、且 `Input` 里含你想匹配的模块名关键字。

候选数 `n` 与目录结构（[test_on_rtllm.py:57-61](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/benchmark_inference/test_on_rtllm.py#L57-L61)）：外层循环 `n` 次，每次建一个 `test_{i}/` 目录，对应 pass@k 的 k 个候选。

> 🔑 **适配新基准的完整清单**：
> 1. 准备新基准的 `{Instruction, Input}` JSONL（仿 `rtllm-1.1.json`）。
> 2. 把 `bench_path = 'rtllm-1.1.json'`（[test_on_rtllm.py:44](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/benchmark_inference/test_on_rtllm.py#L44)）指向新文件。
> 3. 把 `design_list` 换成新基准的所有模块名关键字（注意顺序与特异性）。
> 4. （可选）对接外部评分脚本时，确保 `.v` 文件名与评分脚本的 `design_list` 一致。

#### 4.4.4 代码实践

**实践目标**：往 RTLLM 加一道新设计（例如一个 `priority_encoder`），验证 `design_list` 子串匹配的落盘行为。

**操作步骤**：

1. 往 `benchmark_inference/rtllm-1.1.json` 末尾追加一行（示例代码）：
   ```json
   {"Instruction": "Please act as a professional verilog designer.\n\nImplement a 4-bit priority encoder...\n\nGive me the complete code.\n",
    "Input": "module priority_encoder(\n    input  [3:0] in,\n    output reg [1:0] out\n);"}
   ```
2. 在 `test_on_rtllm.py` 的 `design_list` 末尾追加 `'priority_encoder'`。
3. 用 monkey patch 把 `model.generate` 替换成返回固定 fake Verilog 的本地函数（避免需要真实 GPU），`--n 1 --temperature 0.2`，`--output_dir test_pe`。
4. 检查 `test_pe/test_1/` 下是否生成了 `priority_encoder.v`。

**需要观察的现象**：

- 新关键字命中后，生成代码存成 `priority_encoder.v`。
- 若 fake 代码里不含字符串 `priority_encoder`（比如模型把模块名写成了别的），则不会落盘——这揭示了「匹配依赖模块名出现在拼接文本里」的隐性契约。

**预期结果**：`test_pe/test_1/priority_encoder.v` 存在且内容为骨架+生成代码。

**额外观察（误分类）**：故意把新关键字设为很短的 `'pe'`，看它会不会误匹配到既有题目（如 `pe_single`），体会 u2-l8 提到的「短关键字 + 顺序敏感」风险。

#### 4.4.5 小练习与答案

**练习 1**：`design_list` 的匹配用的是 `if keyword in result`，为什么短关键字（如 `'alu'`）有风险？
**答案**：`in` 是子串匹配。`'alu'` 会命中任何含 `alu` 子串的文本（如 `accumulator`、`calculate`、`logical`），导致一道本不是 ALU 的题被错误存成 `alu.v`。加上 `for ... break` 顺序敏感，短关键字若靠前还会「抢走」本该匹配更特异关键字的题目。

**练习 2**：如果想评测一个全新基准（如 VerilogEval-2），最少要改 `test_on_rtllm.py` 的哪几处？
**答案**：① `bench_path`（第 44 行）指向新基准 JSONL；② `design_list`（第 30–35 行）换成新基准的模块名关键字；③ 若新基准字段名不是 `Instruction`/`Input`，还要改第 68–69、71 行的字段访问。采样与抽取逻辑（u1-l5）可复用。

---

## 5. 综合实践（二选一）

把本讲四个模块串起来，完成下面两个任务之一。

### 任务 A：编写 GGUF CPU 推理脚本跑通 RTLCoder-v1.1-gguf-4bit

**要求**：

1. 从 `ishorn5/RTLCoder-v1.1-gguf-4bit` 下载 `ggml-model-q4_0.gguf`。
2. 编写脚本，用 `ctransformers.AutoModelForCausalLM` 加载，`gpu_layers=0` 纯 CPU 运行（模块 2）。
3. 输入一个自定义的 Verilog 设计 prompt（含自然语言描述 + 模块签名骨架，范式见 u1-l3）。
4. 用 u1-l5 的 `endmodule` 截断逻辑后处理输出，打印干净代码。
5. （加分）若机器有 GPU，对比 `gpu_layers=0` 与 `gpu_layers=32` 的生成耗时。

**验收**：能在一台无 GPU 的机器上打印出可仿真的 Verilog 模块。

### 任务 B：设计一个新的 `p_example` `#method#` 变异策略并讨论新关键字

**要求**：

1. 复制 `data_generation/p_example.txt` 为新模板，改写 `#method#` 为一条新变异策略（如「相同功能、不同时序风格」组合逻辑↔流水线）（模块 3）。
2. 给出新策略下的 given→rewritten 示例。
3. 讨论：这种新策略会让数据集多出哪一类设计？这些新设计如果进 RTLLM 评测，需要往 `design_list` 加哪些**足够特异**的新关键字（模块 4）？给出 2–3 个示例关键字，并说明如何排序以避开短关键字误匹配。

**验收**：交付新模板文件 + 一段讨论（说明新数据方向、对应新 `design_list` 关键字及其排序理由）。

## 6. 本讲小结

- RTL-Coder 发布四个模型，其中 **GPTQ-4bit** 走 GPU 省 3/4 权重显存、**GGUF-4bit** 走 CPU 零 GPU 门槛，两者都只消费别人量化好的权重、本仓库不做转换。
- GPTQ 用 `AutoGPTQForCausalLM.from_quantized(device="cuda:0")`，README 的 import 行有笔误需修正；GGUF 用 `ctransformers.AutoModelForCausalLM`，靠 `gpu_layers` 在纯 CPU 与全 GPU 间连续调节。
- 数据集扩展的最小入口是 `p_example.txt` 的 `#method#`（一句「怎么变异」），改它即可换风味；`{Instruction}` / `{Input}` 是格式契约不能动，模板通过 `instruction_gen.py` 的 `evolv_dic` / `evo_type` 注册与切换。
- 评测扩展靠 `test_on_rtllm.py` 的 `design_list` 子串匹配落盘；新增设计需同时加题目（`rtllm-1.1.json`）与关键字（`design_list`），并警惕短关键字 + 顺序敏感的误分类风险。
- 二次开发的两条通道——改 `#method#` 造数据、改 `design_list` 适配评测——共同构成把 RTLCoder 迁移到自有场景的工程闭环。

## 7. 下一步学习建议

本讲是学习手册的最后一篇，至此你已读完全部 18 篇讲义，覆盖了 RTL-Coder 的数据生成、三种训练方案、评测推理、量化部署与二次开发。接下来建议：

1. **动手跑通一条端到端链路**：用模块 2 的 GGUF 脚本生成代码 → 用 `test_on_rtllm.py` 落盘 → 接外部 RTLLM 仓库评分，体验「推理 → 落盘 → 评分」全流程。
2. **精读三篇论文**（README 的 Papers 段，[README.md:18-26](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/README.md#L18-L26)）：TCAD 版（数据生成 + 评分训练 + 显存优化，对应 u2/u3）、LAD 版（数据集 + 轻量方案，对应 u1/u2-l6/u2-l7）、ICCAD OpenLLM-RTL（开放数据集与基准，对应 u2-l8）。论文能补上源码未体现的实验对比与消融。
3. **尝试二次开发**：按综合实践任务 B 设计一条新 `#method#`，造一批自有方向的指令，再用 u2-l6/u2-l7 的 SFT 管线微调出一个属于你自己的 RTLCoder 变体。
4. **关注显存优化的系统侧补充**：结合 u3-l3（梯度切分）、u3-l4（DeepSpeed ZeRO-2 + offload）与本讲的量化，理解「算法侧 / 系统侧 / 部署侧」三个层面如何协同压低 RTL-Coder 的硬件门槛。
