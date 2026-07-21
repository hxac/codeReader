# 快速上手：加载预训练模型做推理

> 本讲属于入门层（u1），依赖 [u1-l2 仓库结构与依赖](u1-l2-repo-structure-dependencies.md)。
> 读完 u1-l2 你已经知道 `benchmark_inference/` 目录负责推理；本讲就带你真正把一个 RTLCoder 模型跑起来，生成第一段 Verilog 代码。

## 1. 本讲目标

学完本讲，你应当能够：

- 用 `AutoTokenizer` / `AutoModelForCausalLM` 从 HuggingFace 加载 RTLCoder 模型，并理解 `torch_dtype=torch.float16` 与 `device_map` 的作用。
- 构造一段「专业的 Verilog 设计师」输入 prompt，知道一段好 prompt 应包含哪些要素。
- 调用 `model.generate` 完成一次采样生成，理解 `temperature`、`top_p`、`max_length` 三个关键参数的含义与坑。
- 区分 `RTLCoder-v1.1`（Mistral 底座，会自动停止）与 `RTLCoder-Deepseek-v1.1`（需要后处理截断）两种加载方式的差异。

本讲只读一个文件：`README.md`。它已经把「最小可运行推理脚本」直接写在文档里，我们围绕这段真实代码逐行讲透，并在结尾让你亲手跑一遍。

## 2. 前置知识

在进入代码前，先用通俗语言澄清几个概念：

- **CausalLM（因果语言模型）**：也就是「续写型」语言模型。给它一段开头，它预测下一个 token，一个一个往后接。RTLCoder 就是一个 CausalLM——你给它一段电路需求描述，它续写出 Verilog 代码。`AutoModelForCausalLM` 是 HuggingFace `transformers` 提供的「自动加载因果语言模型」的统一入口。
- **Tokenizer（分词器）**：模型不认识文字，只认数字 token id。Tokenizer 负责「文本 ↔ token id」的双向转换。`AutoTokenizer.from_pretrained(模型名)` 会加载与该模型配套的分词器表。
- **fp16（半精度浮点）**：用 16 位浮点数存模型权重，相比 32 位（fp32）显存占用大约减半，推理精度损失却很小。RTLCoder 是 6~7B 参数级别的大模型，显存吃紧，所以官方推理脚本一律用 `torch.float16`。
- **device_map**：告诉 `transformers`「把模型放到哪张卡上」。RTLCoder 的脚本里它通常是一个整数（如 `0`），表示整模型放在 0 号 GPU。
- **采样（sampling）**：模型每一步都输出一个「下一个 token 的概率分布」。贪心解码（greedy）永远挑概率最大的那个，结果确定但容易重复；采样则按概率随机抽，用 `temperature`、`top_p` 调节随机程度，生成更多样。

如果你对 `transformers` 完全陌生，只需记住一句话：**「加载分词器和模型 → 把 prompt 转成 token id → 模型 generate → 把 token id 解码回文字」**，这就是本讲的全部主线。

## 3. 本讲源码地图

| 文件 | 在本讲中的作用 |
| --- | --- |
| [README.md](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/README.md) | 唯一精读文件。包含三段官方推理代码（标准版 / GGUF-CPU 版 / GPTQ 版）与一段「专业 prompt 模板」示例。 |
| `benchmark_inference/test_on_rtllm.py` | 补充参考。真实评测脚本如何加载模型、如何拼 prompt、如何调用 generate。 |
| `benchmark_inference/test_on_verilog-eval.py` | 补充参考。另一个基准的推理脚本，展示了批量生成与 `do_sample` 的写法。 |

> 说明：本讲的「最小可运行脚本」完全来自 README，我们只是把它拆开讲清楚；两个 `benchmark_inference/` 脚本作为「真实工程里怎么写」的佐证，会在需要时引用其行号。

## 4. 核心概念与源码讲解

本讲按三个最小模块展开：① 加载分词器与模型；② 构造专业的 Verilog prompt；③ 调用 `model.generate` 采样生成。

### 4.1 模型与分词器加载（AutoTokenizer / AutoModelForCausalLM、fp16、device_map）

#### 4.1.1 概念说明

跑一次推理，第一步永远是「把模型和分词器请进显存」。RTLCoder 的四个发布模型（Deepseek / Mistral / GPTQ-4bit / GGUF-4bit）都托管在 HuggingFace，加载方式高度统一：

- `AutoTokenizer.from_pretrained("ishorn5/RTLCoder-v1.1")`：按模型名（或本地路径）下载/加载配套分词器。
- `AutoModelForCausalLM.from_pretrained(...)`：加载与该模型配套的因果语言模型权重。

两个 `Auto*` 类的好处是：你不必关心底层到底是 Mistral 还是 DeepSeek 架构，`Auto*` 会读模型 `config.json` 自动选对实现。这一点对 RTLCoder 尤其重要，因为它的两个主力模型底座不同（Mistral-v0.1 与 DeepSeek-coder-6.7b），但对外调用方式一模一样。

#### 4.1.2 核心流程

加载流程可以用三步概括：

1. 设定要用哪张卡（`gpu_name`，一个整数）。
2. 加载分词器：`AutoTokenizer.from_pretrained(模型名)`。
3. 加载模型：`AutoModelForCausalLM.from_pretrained(模型名, torch_dtype=torch.float16, device_map=gpu_name)`，随后 `model.eval()` 切到推理模式（关闭 dropout）。

其中 `device_map=gpu_name`（整数）的作用是「把整模型放到指定单卡」。README 的注释明确说明：当你机器上有多张卡时，用这个整数来**指定要用哪一张**。

#### 4.1.3 源码精读

README 给出的标准推理脚本（默认演示的是 Deepseek 版）核心如下：

[README.md:L145-L160](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/README.md#L145-L160) —— 这段代码完成了「import → 定义 prompt → 加载分词器和模型 → 采样 → 解码」的全流程，是本讲的主干。

其中加载部分三行最关键：

[README.md:L152-L156](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/README.md#L152-L156) —— 逐行说明：

- `gpu_name = 0`：选定 0 号 GPU。
- `tokenizer = AutoTokenizer.from_pretrained("ishorn5/RTLCoder-Deepseek-v1.1")`：加载与 Deepseek 版配套的分词器。
- `model = AutoModelForCausalLM.from_pretrained("ishorn5/RTLCoder-Deepseek-v1.1", torch_dtype=torch.float16, device_map=gpu_name)`：以 fp16 精度加载模型并放到 0 号卡。
- `model.eval()`：切换到推理模式。

**关于 device_map 的补充说明（多卡场景）**：README 在该行上方注释道——「*With multiple gpus, you can specify the GPU you want to use as gpu_name (e.g. int(0))*」。也就是说，仓库代码里 `device_map` 传的是一个**整数**，用来在多卡机器上**挑选一张卡**整模型放上去，而不是把模型切分到多张卡。两个基准脚本的做法完全一致：

[benchmark_inference/test_on_rtllm.py:L50-L52](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/benchmark_inference/test_on_rtllm.py#L50-L52) —— 同样用 `device_map=args.gpu_name`（`--gpu_name` 是 `int` 类型参数）把整模型放到指定单卡。

> 拓展（标准 `transformers` 行为，非本仓库代码）：如果你遇到一张卡放不下的更大模型，可以把 `device_map` 改成 `"auto"` 或 `"balanced"`，让 HF 自动把模型层切分到多张卡上。RTLCoder 的 6~7B 体量在 fp16 下约 13~15 GB，单张 16 GB 以上显存的卡即可整模型放下，所以官方脚本都用最简单的「单卡整数」写法。

#### 4.1.4 代码实践

**实践目标**：动手加载一个 RTLCoder 模型，确认分词器与模型对象能正常创建（本步先不生成，聚焦加载）。

**操作步骤**：

1. 确认环境里有 `requirements.txt` 锁定的依赖（`torch==2.1.0`、`transformers==4.34.0`），首次运行需联网从 HuggingFace 拉模型。
2. 新建 `load_check.py`，写入下面这段（这是**示例代码**，基于 README 的加载片段裁剪而来，仅做加载自检）：

   ```python
   import torch
   from transformers import AutoTokenizer, AutoModelForCausalLM

   gpu_name = 0
   tokenizer = AutoTokenizer.from_pretrained("ishorn5/RTLCoder-v1.1")
   model = AutoModelForCausalLM.from_pretrained(
       "ishorn5/RTLCoder-v1.1",
       torch_dtype=torch.float16,
       device_map=gpu_name,
   )
   model.eval()
   print("参数量(约):", sum(p.numel() for p in model.parameters()) / 1e9, "B")
   print("模型所在的 device:", next(model.parameters()).device)
   ```

3. 运行 `python load_check.py`。

**需要观察的现象**：打印出的参数量应为约 7B 量级；`device` 应显示 `cuda:0`（即 0 号卡）。

**预期结果**：脚本能跑完不报 OOM，说明模型成功进入显存。

**待本地验证**：本环境没有可用 GPU，以上为「预期」而非「已运行」结果；请在你本地 GPU 机器上验证。

#### 4.1.5 小练习与答案

**练习 1**：把 `device_map=gpu_name` 中的 `gpu_name` 改成 `1`，模型会去哪张卡？如果机器只有 1 张卡会怎样？
**答案**：模型会被放到 1 号 GPU；若机器只有 1 张卡（只有 `cuda:0`），运行时会因为找不到 `cuda:1` 而报错。这正是 README 强调「指定你**想用的那张卡**」的原因。

**练习 2**：为什么官方脚本统一加 `torch_dtype=torch.float16`？去掉它会怎样？
**答案**：fp16 让 6~7B 模型的显存占用从约 28 GB（fp32）降到约 14 GB，使其能在单张 16 GB 卡上运行；去掉则会默认以 fp32 加载，显存翻倍，容易 OOM。

---

### 4.2 构造专业的 Verilog 设计 prompt

#### 4.2.1 概念说明

README 在推理示例前有一句重要提醒：**「The input prompt may have a great influence on the generation quality.」**（输入 prompt 对生成质量影响很大）。理想情况下，prompt 应当无歧义地把电路的「端口（IO）」和「行为」描述清楚。为此 README 提供了一个**专业模板**和一个**极简模板**：

- **极简模板**：一句话需求 + 模块签名骨架。适合简单电路（如半加器）。
- **专业模板**：结构化分节描述——角色声明、功能描述、模块名、输入端口、输出端口、实现细节、收尾指令、模块签名骨架。适合复杂电路（如位宽转换）。

#### 4.2.2 核心流程

一段「专业 prompt」通常由以下要素拼成（顺序也很重要）：

1. **角色声明**：`Please act as a professional verilog designer.`
2. **功能行为描述**：用自然语言说清电路做什么、时序如何。
3. **Module name**：模块名。
4. **Input ports**：逐个列出输入端口及其含义（含时钟、复位等）。
5. **Output ports**：逐个列出输出端口及其含义。
6. **Implementation**：实现层面的细节（如上升沿触发、复位值、拼接顺序）。
7. **收尾指令**：`Give me the complete code.`
8. **模块签名骨架**：给出 `module xxx(...);` 的端口声明开头，让模型「续写」内部实现。

> 这个结构并非随意——它和训练数据里的 `{Instruction, Input}` 格式是对齐的：前 7 项大致对应 `Instruction`，第 8 项对应 `Input`。关于这一点，会在 [u1-l4 数据格式详解](u1-l4-data-formats.md) 详细展开。本讲你只需记住「描述 + 骨架」的拼法。

#### 4.2.3 源码精读

README 给出的专业模板示例（一个 8 位转 16 位的位宽转换电路）：

[README.md:L94-L130](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/README.md#L94-L130) —— 注意它从 `Please act as a professional verilog designer.` 开头，依次给出行为描述、Module name、Input ports、Output ports、Implementation、`Give me the complete code.`，最后以 `module width_8to16(...);` 端口声明收尾，留给模型续写 `always` 块等内部逻辑。

而推理脚本里实际使用的**极简版** half_adder prompt 是一行字符串：

[README.md:L149](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/README.md#L149) —— 即：
```python
prompt = "Please act as a professional verilog designer and provide a half adder. \nmodule half_adder\n(input a, \ninput b, \noutput sum, \n output carry);\n"
```
它把「角色 + 需求」压成一句，再紧跟 `module half_adder(...)` 的端口骨架，让模型从分号后开始续写。

两个基准脚本里，prompt 的拼接遵循同样的「描述 + 输入骨架」思路，只是从 JSON 字段里取：

- RTLLM 脚本：`prompt = dic['Instruction'] + '\n' + dic['Input'] + '\n'`（[test_on_rtllm.py:L69](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/benchmark_inference/test_on_rtllm.py#L69)）。
- VerilogEval 脚本：`prompt = dic['description'] + '\n' + dic['prompt'] + '\n'`（[test_on_verilog-eval.py:L72](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/benchmark_inference/test_on_verilog-eval.py#L72)）。

可以看到，「自然语言描述 + 模块骨架」就是 RTLCoder 全项目统一的 prompt 范式。

#### 4.2.4 代码实践

**实践目标**：体会「prompt 质量 → 生成质量」的关系。

**操作步骤**：

1. 准备两段 prompt（**示例代码**）：
   - 简陋版：`prompt_bad = "写一个半加器"`（中文、无端口骨架）。
   - 专业版：直接用 README 的 half_adder prompt（见 4.2.3）。
2. 分别送入同一个已加载的模型，`max_length=512, temperature=0.5, top_p=0.9` 生成。
3. 对比两者输出：哪一段更像合法 Verilog？哪一段端口名和预期一致？

**需要观察的现象**：简陋版很可能端口名随意、甚至夹杂解释性文字；专业版通常直接给出 `assign sum = a ^ b; assign carry = a & b;` 之类的标准实现。

**预期结果**：专业版生成的代码更接近可综合、可仿真通过的半加器。

**待本地验证**：生成结果带有随机性（采样），请在本地多次运行观察。

#### 4.2.5 小练习与答案

**练习 1**：把 README 专业模板里的「Output ports」整段删掉再生成，预测会发生什么。
**答案**：模型失去对输出端口语义的约束，可能自创端口名或漏掉关键输出（如 `valid_out`），生成代码与题意不符的概率上升。这印证了 README「prompt 应清晰描述 IO 与行为」的建议。

**练习 2**：为什么 prompt 末尾要给出 `module xxx(...);` 的骨架，而不是让模型自己起模块名？
**答案**：因为评测脚本常以**模块名**作为关键字去保存/识别生成的 `.v` 文件（见 RTLLM 脚本里的 `design_list` 匹配逻辑）。固定模块名能保证生成的代码可被下游基准正确接收。

---

### 4.3 model.generate 采样生成（temperature / top_p / max_length）

#### 4.3.1 概念说明

加载好模型、写好 prompt 后，剩下的就是「让模型续写」。核心调用是 `model.generate(...)`。三个最关键的参数：

- **temperature（温度）**：控制概率分布的「尖锐程度」。温度越低，模型越倾向选概率最高的 token（趋近确定）；温度越高，分布越平摊，生成越发散。
- **top_p（核采样）**：只在累计概率达到 `p` 的那部分候选 token 里采样，把长尾低概率 token 排除掉，兼顾多样性与合理性。
- **max_length**：生成序列的**总长度上限**（prompt + 续写），超过即截断。

此外还有一个**容易被忽略的开关 `do_sample`**：只有 `do_sample=True` 时，`temperature` / `top_p` 才真正生效（采样）；否则是贪心解码。README 的极简脚本里没有显式写 `do_sample`，而两个基准脚本都显式写了——这是一个值得注意的差异（见 4.3.4）。

#### 4.3.2 核心流程

带温度的采样，本质是对模型 logits 做一次「除以温度再 softmax」：

\[ p_i = \frac{\exp(z_i / T)}{\sum_{j} \exp(z_j / T)} \]

其中 \(z_i\) 是第 \(i\) 个 token 的 logit，\(T\) 是温度。

- \(T \to 0\)：最大 logit 对应的 \(p_i \to 1\)，退化为贪心。
- \(T = 1\)：标准 softmax。
- \(T > 1\)：分布变平，小概率 token 也有机会被选中。

`top_p`（nucleus）则是在上述分布上再做一次裁剪：选最小的 token 集合，使其累计概率 \(\ge p\)，只在该集合内按归一化后的概率采样。RTLCoder 推理常用 `temperature=0.5`、`top_p=0.9`（README 极简脚本）或 `top_p=0.95`（基准脚本）。

一次生成的主流程：

1. `input_ids = tokenizer(prompt, return_tensors="pt").input_ids.to(gpu_name)`：prompt 转成张量并放到 GPU。
2. `sample = model.generate(input_ids, max_length=..., temperature=..., top_p=..., ...)`：生成。
3. `tokenizer.decode(sample[0])`：解码回文字。

#### 4.3.3 源码精读

README 极简脚本里，生成与解码这两行是核心：

[README.md:L158-L160](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/README.md#L158-L160) —— 逐行说明：

- `input_ids = tokenizer(prompt, return_tensors="pt").input_ids.to(gpu_name)`：把 prompt 编码成 PyTorch 张量并搬到 0 号卡（与模型同卡，否则会报设备不一致）。
- `sample = model.generate(input_ids, max_length=512, temperature=0.5, top_p=0.9)`：最多生成到总长 512；温度 0.5、核采样 0.9。
- `s_full = tokenizer.decode(sample[0])`：把生成结果（**包含 prompt 回显**）解码成字符串。

**一个关键细节——max_length 包含 prompt**：`max_length=512` 是「prompt + 续写」的总上限。对 half_adder 这种短 prompt（约 30 token）足够；但如果用 4.2 的专业模板（动辄数百 token），512 会很快截断，导致代码生成不完。两个基准脚本的处理方式更稳妥——把生成预算设成「prompt 长度 + N」：

[test_on_rtllm.py:L75-L77](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/benchmark_inference/test_on_rtllm.py#L75-L77) —— 用 `max_length=len(inputs[0]) + 2048`，保证在任意 prompt 长度下都留出 2048 token 的续写空间；同时显式写了 `do_sample=True`、`temperature=args.temperature`、`top_p=0.95`。

**另一个细节——停止行为**：README 明确指出两个版本的差异。

[README.md:L178-L186](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/README.md#L178-L186) —— Mistral 版（`ishorn5/RTLCoder-v1.1`）「在完成编码任务后会自动停止生成 token」，因此只需 `print(tokenizer.decode(sample[0]))` 即可；而 Deepseek 版「即使所需输出已经完成也可能不停止」，所以才需要 [README.md:L161-L176](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/README.md#L161-L176) 那一大段基于 `endmodulemodule` / `endmodule` 关键字的截断后处理（这部分会在 [u1-l5 输出后处理与代码抽取](u1-l5-output-extraction.md) 专讲）。

#### 4.3.4 代码实践

**实践目标**：亲手跑通一次 half_adder 生成，并观察 temperature 对结果的影响。

**操作步骤**：

1. 在 4.1 能成功加载模型的基础上，新建 `gen_half_adder.py`（**示例代码**，改编自 README，把默认的 Deepseek 版换成练习要求的 Mistral 版）：

   ```python
   import torch
   from transformers import AutoTokenizer, AutoModelForCausalLM

   prompt = ("Please act as a professional verilog designer and provide a half adder. \n"
             "module half_adder\n(input a, \ninput b, \noutput sum, \n output carry);\n")

   gpu_name = 0
   # 注意：练习要求用的是 Mistral 版（会自动停止），不是 README 默认演示的 Deepseek 版
   tokenizer = AutoTokenizer.from_pretrained("ishorn5/RTLCoder-v1.1")
   model = AutoModelForCausalLM.from_pretrained(
       "ishorn5/RTLCoder-v1.1", torch_dtype=torch.float16, device_map=gpu_name)
   model.eval()

   input_ids = tokenizer(prompt, return_tensors="pt").input_ids.to(gpu_name)
   # 建议显式加 do_sample=True，确保 temperature/top_p 真正生效（基准脚本就是这样写的）
   sample = model.generate(input_ids, do_sample=True,
                           max_length=512, temperature=0.5, top_p=0.9)
   print(tokenizer.decode(sample[0]))
   ```

2. 运行 `python gen_half_adder.py`，观察输出。
3. 把 `temperature` 依次改成 `0.2`、`0.5`、`0.8`，各跑一次（`top_p` 保持 0.9），对比输出。

**需要观察的现象**：

- 输出里会**回显 prompt**（因为 `decode(sample[0])` 解码的是整段），紧跟着模型续写的半加器实现，通常包含 `assign sum = a ^ b; assign carry = a & b;` 与 `endmodule`。
- Mistral 版应在 `endmodule` 后自行停止，不会再啰嗦。
- `temperature=0.2` 输出更稳定趋同；`temperature=0.8` 更容易出现不同写法甚至小错误。

**预期结果**：得到一段以 `endmodule` 结尾、结构完整的半加器 Verilog。

**关于 `do_sample` 的说明**：README 极简脚本未显式写 `do_sample=True`。在 `transformers==4.34.0`（项目锁定版本）下，仅传 `temperature`/`top_p` 而不开 `do_sample` 的确切行为以你本地实测为准；为确保采样生效，本实践按基准脚本的写法**显式加了 `do_sample=True`**。这一点**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：把 `max_length` 从 512 调到 100，再用 4.2 的「专业模板（位宽转换）」prompt 跑一次，会发生什么？为什么？
**答案**：因为专业模板本身就有几百 token，`max_length=100` 连 prompt 都装不下，要么报错要么生成几乎没有续写就被截断。这印证了 `max_length` 是「prompt + 续写」总长，长 prompt 必须相应调大（基准脚本用 `len(inputs[0]) + 2048` 正是这个道理）。

**练习 2**：README 极简脚本用 `temperature=0.5, top_p=0.9`，而基准脚本用 `top_p=0.95`。若把 `temperature` 设为非常大的值（如 5.0），输出会变成什么样？
**答案**：温度过高会让 softmax 分布极度平摊，模型几乎「等概率」乱选 token，输出会变成不可读的乱码或语法错误的 Verilog。温度应取适中值（0.2~0.8 常见）。

**练习 3**：`tokenizer.decode(sample[0])` 的输出里为什么包含 prompt 本身？基准脚本是怎么避开这一点的？
**答案**：`sample[0]` 是「prompt + 续写」的完整序列，所以 decode 会回显 prompt。基准脚本用 `output[len(inputs[0]):]` 跳过前缀（见 [test_on_rtllm.py:L79](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/benchmark_inference/test_on_rtllm.py#L79)），只解码真正新生成的部分。

## 5. 综合实践

把本讲三个模块串起来，完成一次「端到端」推理小任务：

1. **加载**：用 `AutoTokenizer` / `AutoModelForCausalLM` 以 fp16 加载 `ishorn5/RTLCoder-v1.1` 到指定 GPU。
2. **构造 prompt**：参考 4.2 的专业模板，自己写一段「2 选 1 数据选择器（mux_2to1）」的专业 prompt——包含角色声明、行为描述、Module name、Input/Output ports、Implementation、`Give me the complete code.`，并以 `module mux_2to1(...);` 骨架收尾。
3. **生成**：调用 `model.generate`，参数用 `do_sample=True, max_length=600, temperature=0.5, top_p=0.9`。
4. **解码并截取**：模仿基准脚本，只取 `output[len(input_ids[0]):]` 部分解码，再用 `rsplit('endmodule', 1)` 在末尾补一个 `endmodule`，打印干净的 Verilog。
5. **反思**：把 `temperature` 改成 0.2 再跑一次，对比两次输出的稳定性，写一句话结论。

> 这个任务把「加载、prompt、generate、解码后处理」四件事走了一遍；其中的后处理细节（`endmodule` 截断）会在 [u1-l5](u1-l5-output-extraction.md) 系统讲解，本讲你只要照做即可。

## 6. 本讲小结

- RTLCoder 的最小推理脚本就藏在 README 里：`AutoTokenizer` + `AutoModelForCausalLM` 加载，`model.generate` 生成，`tokenizer.decode` 解码，四步即可跑通。
- 加载时一律用 `torch_dtype=torch.float16` 省显存；`device_map` 传一个整数来在多卡中**挑选一张卡**整模型放置。
- prompt 质量决定生成质量。项目统一范式是「自然语言描述（含 IO/行为）+ 模块签名骨架」，复杂电路用 README 的分节专业模板，简单电路用一行极简模板。
- `model.generate` 三个关键参数：`temperature` 控制随机性、`top_p` 做核采样裁剪、`max_length` 是「prompt + 续写」总长上限；要真正采样需显式 `do_sample=True`（基准脚本的写法）。
- 两个主力模型行为不同：Mistral 版（`RTLCoder-v1.1`）会自动停止；Deepseek 版可能不停止，需要关键字截断后处理。
- `max_length` 包含 prompt、`decode(sample[0])` 会回显 prompt——这两个坑在长 prompt / 批量评测时尤其要注意。

## 7. 下一步学习建议

- 接下来读 [u1-l4 数据格式详解](u1-l4-data-formats.md)：理解训练数据里的 `{Instruction, Input, Response}`，你会更明白本讲的 prompt 为什么长这样。
- 再读 [u1-l5 输出后处理与代码抽取](u1-l5-output-extraction.md)：系统学习 `endmodule` 截断、testbench 剔除、Deepseek 版 `endmodulemodule` 特殊处理，把本讲结尾那段后处理彻底搞懂。
- 进阶后可读 [u2-l8 基准评测推理脚本](u2-l8-benchmark-inference.md)：看真实评测脚本如何把本讲的「单条推理」扩展成 VerilogEval / RTLLM 上的批量评测。
