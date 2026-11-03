# u3-l1 设备选择与贪心解码

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清 `ch02.py` 中 `get_device` 的四后端探测顺序（CUDA → MPS → XPU → CPU），以及它只在 CUDA 分支里做的 TF32 加速开关及其 PyTorch 版本兼容分支。
2. 逐行解释 `generate_text_basic` 的贪心解码循环：整段前向、取最后一个位置的 logits、`argmax` 选 token、`torch.cat` 拼接、切掉 prompt 返回。
3. 解释 `eos_token_id` 提前停止的工作方式，以及为什么检查要放在拼接之前。
4. 亲手测出「无 KV cache 生成」的耗时随 token 数超线性增长，并用求和公式算出理论比值。

本讲是单元三的第一讲。单元一、二解决了「仓库长什么样」和「模型/分词器内部如何工作」，从本讲开始我们让模型真正「跑起来」。

## 2. 前置知识

本讲只需要很少的 PyTorch 背景，遇到的概念在这里先通俗解释：

- **张量与设备（device）**：PyTorch 中所有数据都是张量（多维数组），每个张量都住在某个「设备」上——`cpu`（内存 + CPU 计算）、`cuda`（NVIDIA 显卡）、`mps`（Apple Silicon 芯片的统一内存 GPU）、`xpu`（Intel 显卡）。模型权重和输入张量必须在**同一个设备**上才能运算，所以运行前要统一选定一个设备。
- **logits**：模型前向的原始输出，形状为 `(批大小, 序列长度, 词表大小)`。它还没归一化成概率，但谁大谁就代表模型更倾向输出谁（回顾 [u2-l1]）。
- **token 与 tokenizer**：文本被切成 token（子词），每个 token 对应词表里的一个整数 id。`encode` 把文本变 id 列表，`decode` 变回文本（回顾 [u2-l6]）。
- **EOS（end-of-sequence）**：词表里一个特殊 token，训练时用作文档之间的分隔符。base 模型的 EOS 是 `<|endoftext|>`，id 为 `151643`，可从 `tokenizer.eos_token_id` 读取。
- **`model.eval()` 与 `@torch.inference_mode()`**：`eval()` 把模型切到推理模式（关闭 dropout 等训练行为）；`inference_mode()` 是装饰器/上下文管理器，让前向过程不记录梯度，省内存也更快。推理代码两者都该用。
- **贪心解码（greedy decoding）**：每一步都选 logits 最大的那个 token，不引入任何随机性，因此同一输入永远得到同一输出。它是所有解码策略中最简单的一种，第 4 章的温度采样、top-p 都是在它之上「松开确定性」。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [reasoning_from_scratch/ch02.py](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch02.py) | 本讲主角。`get_device`（L10-L39）、`generate_text_basic`（L42-L57）、EOS 检查（L51-L54），以及同族的其余生成函数与 `generate_stats`。 |
| [ch02/01_main-chapter-code/ch02_main.ipynb](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch02/01_main-chapter-code/ch02_main.ipynb) | 第 2 章教学 notebook。2.5 节定义同样的 `get_device`，2.7 节用流式版生成函数演示贪心解码与 EOS，2.8-2.9 节做加速对比，给出了多硬件 tokens/sec 基准表。 |
| [ch02/05_use_model/generate_simple.py](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch02/05_use_model/generate_simple.py) | 最小可运行推理脚本（[u1-l2] 已跑过）。本讲实践环节的加载代码以它为模板。 |
| [reasoning_from_scratch/qwen3.py](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/qwen3.py) | 提供 `Qwen3Model`、`Qwen3Tokenizer`、`download_qwen3_small`，单元二已精读，本讲只当黑盒使用。 |

## 4. 核心概念与源码讲解

### 4.1 模块一：`get_device` —— 多后端设备探测与 TF32 配置

#### 4.1.1 概念说明

同一段 PyTorch 代码要在完全不同的硬件上都能跑：Linux 服务器上的 NVIDIA 卡、Mac 上的 Apple 芯片、Intel 显卡机器、以及什么加速卡都没有的笔记本。PyTorch 为此提供了统一的设备抽象——代码不变，只换 `torch.device("cuda")` / `"mps"` / `"xpu"` / `"cpu"` 字符串，张量与算子就会被派发到对应后端。

`get_device` 就是本仓库的「设备决策函数」：按优先级探测可用后端，返回一个 `torch.device` 对象，让上层代码不必关心具体硬件。

它还顺带做了一件只在 NVIDIA 卡上有意义的事：打开 **TF32（TensorFloat-32）**。TF32 是 Ampere 及之后 NVIDIA GPU 的一种矩阵乘法模式：用 19 位（1 位符号 + 8 位指数 + 10 位尾数）的格式跑「名义上的 fp32 矩阵乘」，走 Tensor Core 专用电路，吞吐远高于真 fp32，而对大模型推理来说精度损失可以忽略。它由全局后端开关控制，不是模型结构的一部分。

#### 4.1.2 核心流程

```
get_device(enable_tensor_cores=True):
    若 torch.cuda.is_available():        # 1. NVIDIA CUDA/ROCm GPU
        device = cuda
        若 enable_tensor_cores:
            解析 torch 版本号 -> (major, minor)
            若 (major, minor) >= (2, 11): 用新 API 设 fp32_precision = "tf32"
            否则:                          用旧 API 设 allow_tf32 = True
    否则若 torch.backends.mps.is_available():  # 2. Apple Silicon GPU
        device = mps
    否则若 torch.xpu.is_available():           # 3. Intel GPU
        device = xpu
    否则:                                      # 4. 兜底
        device = cpu
    打印所选设备; 返回 device
```

三点值得注意：

1. **顺序即优先级**：if/elif 链保证只选一个后端，先到先得。CUDA 最快所以排第一，CPU 最慢所以兜底。
2. **`get_device` 不是纯函数**：在 CUDA 分支里它会**修改全局后端设置**（TF32 开关），这是它的副作用。同一进程里调用一次即可。
3. **版本分支是给 `torch.compile` 打的补丁**：源码注释指出 PyTorch 2.9/2.10 的 `torch.compile` 只读旧的 TF32 开关（`allow_tf32`），2.11 起才认新的 `fp32_precision` API，所以代码按版本号二选一。本项目 [pyproject.toml:L27](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/pyproject.toml#L27) 约束 `torch>=2.10,<3`，因此当前装 2.10 的读者走旧 API 分支，将来装 2.11+ 自动切换到新 API 分支。

#### 4.1.3 源码精读

设备探测主体：[reasoning_from_scratch/ch02.py:L10-L15](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch02.py#L10-L15) —— 函数入口：若 CUDA 可用则选 `cuda` 并打印提示，随后准备进入 TF32 配置。

版本解析与两条 TF32 分支：[reasoning_from_scratch/ch02.py:L16-L25](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch02.py#L16-L25) —— `map(int, torch.__version__.split(".")[:2])` 把版本串（如 `"2.10.0"`）切成 `(2, 10)`；`>= (2, 11)` 走 `fp32_precision = "tf32"` 新 API，否则走 `allow_tf32 = True` 旧 API。L17-L19 的注释链接到 PyTorch 官方 issue 和本仓库 issue #256，记录了这个兼容性坑的出处。

MPS / XPU / CPU 三个 fallback 分支：[reasoning_from_scratch/ch02.py:L27-L39](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch02.py#L27-L39) —— 依次探测 Apple Silicon（`torch.backends.mps`）、Intel GPU（`torch.xpu`），都不可用则选 CPU，最后返回 `torch.device` 对象。

调用方的一处细节：notebook 2.3 节的手工探测单元里，顺序是 CUDA → **XPU → MPS** → CPU（先 Intel 后 Apple），而 `get_device` 是 CUDA → **MPS → XPU** → CPU。两处顺序并不一致；由于一台机器同时具备 MPS 和 XPU 的情况几乎不存在，实际结果相同，但这个差异提醒我们：读代码时别假设「两段相似代码一定等价」。

仓库内的真实调用示例：[ch02/05_use_model/generate_simple.py:L61](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch02/05_use_model/generate_simple.py#L61) —— `device = torch.device(args.device) if args.device else get_device()`：命令行显式指定优先，未指定时自动探测。

#### 4.1.4 代码实践

1. **实践目标**：确认本机会被 `get_device` 分到哪个设备，并判断 TF32 分支是否会执行。
2. **操作步骤**（示例代码，可直接保存为 `probe_device.py` 运行）：

   ```python
   # 示例代码
   import torch
   from reasoning_from_scratch.ch02 import get_device

   print("torch 版本:", torch.__version__)
   major, minor = map(int, torch.__version__.split(".")[:2])
   print("版本元组:", (major, minor), "-> TF32 走",
         "新 API (fp32_precision)" if (major, minor) >= (2, 11) else "旧 API (allow_tf32)")

   device = get_device()
   print("返回值:", device, "| 类型:", type(device).__name__)
   ```

3. **需要观察的现象**：控制台先打印版本信息，再打印 `get_device` 内部的设备提示（如 `Using CPU` 或 `Using Apple Silicon GPU (MPS)`），最后打印返回的 `torch.device` 对象。
4. **预期结果**：无 GPU 的机器返回 `cpu`；Mac 返回 `mps`。在 2.10.x 版本下应输出「旧 API (allow_tf32)」。若在 NVIDIA 卡上，可再执行 `print(torch.backends.cuda.matmul.allow_tf32)` 验证 `get_device()` 调用后该全局开关已变为 `True`（CPU/MPS 机器此开关无意义）。

#### 4.1.5 小练习与答案

**练习 1**：为什么探测顺序是 CUDA → MPS → XPU → CPU，而不能任意排列？
**答案**：if/elif 链表达的是优先级——对本任务而言 CUDA 最快、Apple GPU 次之、Intel GPU 再次、CPU 最慢；把慢后端排前面会让快硬件被「抢答」，把 CPU 放中间则可能在无 GPU 机器上误选。

**练习 2**：在一台纯 CPU 机器上，`get_device()` 和 `get_device(enable_tensor_cores=False)` 的返回值有区别吗？
**答案**：没有区别，都返回 `cpu`。`enable_tensor_cores` 只在 CUDA 分支内部被读取，CPU 路径根本不经过那段代码。

**练习 3**：项目约束 `torch>=2.10,<3`，那 L20-L22 的新 API 分支何时才会被执行？
**答案**：当用户安装了 torch 2.11 或更高版本时。装 2.10 时走 L23-L25 旧 API——因为 torch.compile 在 2.9/2.10 下只识别旧开关，这是源码注释里写明的兼容性补丁。

### 4.2 模块二：`generate_text_basic` —— 贪心解码循环

#### 4.2.1 概念说明

语言模型是**自回归（autoregressive）**的：它一次前向其实会给序列里**每个位置**都输出一份全词表 logits，但生成下一个词时只有**最后一个位置**的 logits 有用——因为在因果掩码下（回顾 [u2-l5]），最后一个位置恰好「看完了」前面所有 token。于是最朴素的生成循环就是：

1. 把当前序列整段送进模型；
2. 取最后位置的 logits，`argmax` 挑出概率最大的下一个 token；
3. 把新 token 拼到序列末尾；
4. 回到第 1 步，直到生成了 `max_new_tokens` 个 token 或遇到 EOS。

这就像查字典接龙：每次都基于「目前写下的全部内容」决定下一个词。`generate_text_basic` 是本仓库四代生成函数中最基础的一版，之后的流式版、KV cache 版（下一讲 [u3-l2]）都从它演化而来：

| 函数 | 位置 | 特点 |
| --- | --- | --- |
| `generate_text_basic` | [ch02.py:L42-L57](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch02.py#L42-L57) | 一次性返回全部生成 token；无 cache |
| `generate_text_basic_cache` | [ch02.py:L64-L91](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch02.py#L64-L91) | 加 KV cache（保留作向后兼容，源码注释见 [L60-L63](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch02.py#L60-L63)） |
| `generate_text_basic_stream` | [ch02.py:L94-L113](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch02.py#L94-L113) | 用 `yield` 逐 token 产出 |
| `generate_text_basic_stream_cache` | [ch02.py:L116-L141](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch02.py#L116-L141) | 流式 + cache，notebook 与 `generate_simple.py` 实际使用的版本 |

#### 4.2.2 核心流程

```
generate_text_basic(model, token_ids, max_new_tokens, eos_token_id=None):
    input_length = prompt 长度                # 用于最后切掉 prompt
    model.eval()
    重复 max_new_tokens 次:
        out = model(token_ids)[:, -1]          # 整段前向，只留最后一行 -> (b, vocab)
        next_token = argmax(out)               # 贪心：取最大 logits -> (b, 1)
        若 next_token == eos_token_id: break   # 提前停止（见 4.3）
        token_ids = cat(token_ids, next_token) # 沿序列维拼接
    返回 token_ids[:, input_length:]           # 只返回新生成部分
```

**无 cache 的开销分析**（本讲的核心洞见）：设 prompt 长度为 \( L \)，要生成 \( N \) 个 token。第 \( i \) 步（\( i = 0, 1, \dots, N-1 \)）前向要处理的序列长度是 \( L + i \)，整个生成过程「按 token 计的前向总量」为

\[
\sum_{i=0}^{N-1} (L+i) = LN + \frac{N(N-1)}{2}
\]

注意那个 \( N^2 \) 项：生成量翻倍，代价超过翻倍。以 notebook 的 prompt「Explain large language models in a single sentence.」（\( L = 6 \) 个 token）为例：

- \( N = 20 \)：\( 6 \times 20 + \tfrac{20 \times 19}{2} = 120 + 190 = 310 \) 个 token 前向；
- \( N = 40 \)：\( 6 \times 40 + \tfrac{40 \times 39}{2} = 240 + 780 = 1020 \) 个 token 前向；
- 比值 \( 1020 / 310 \approx 3.3 \)——token 数翻倍，耗时约为 **3.3 倍**而不是 2 倍（注意力部分对长度还是平方级，实际比值只会更高）。

这也解释了 notebook 2.8 节的基准：无 cache 在 Mac Mini M4 CPU 上只有 5 tokens/sec，加 KV cache 后升到 29 tokens/sec。KV cache 的原理（每步只前向新 token、旧 K/V 复用）在 [u2-l5] 已精读，它对生成循环的改造在下一讲 [u3-l2] 展开。

#### 4.2.3 源码精读

函数签名与装饰器：[reasoning_from_scratch/ch02.py:L42-L45](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch02.py#L42-L45) —— `@torch.inference_mode()` 关闭梯度追踪；`input_length = token_ids.shape[1]` 在循环前记下 prompt 长度；`model.eval()` 切推理模式。

循环体三行核心：[reasoning_from_scratch/ch02.py:L47-L49](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch02.py#L47-L49) —— `model(token_ids)` 对**整段**序列做一次前向得到 `(b, T, vocab)`，`[:, -1]` 只保留最后位置变成 `(b, vocab)`；`torch.argmax(out, dim=-1, keepdim=True)` 沿词表维取最大值下标，`keepdim=True` 让结果保持 `(b, 1)` 形状以便下一步拼接。notebook 2.6 节演示了同样的操作：6 token 输入得到形状 `[6, 151936]` 的输出，对最后一行 argmax 得到 id `20286`，decode 出 `" Large"`。

拼接与返回：[reasoning_from_scratch/ch02.py:L56-L57](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch02.py#L56-L57) —— `torch.cat([token_ids, next_token], dim=1)` 把新 token 沿序列维（dim=1）接到末尾，下一轮迭代把它也喂进模型；返回时 `token_ids[:, input_length:]` 切掉 prompt，调用方拿到的纯粹是生成内容。

与 notebook 的对应关系：notebook 2.7 节使用的是流式版 `generate_text_basic_stream`（[ch02.py:L94-L113](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch02.py#L94-L113)），循环骨架与 `generate_text_basic` 完全相同，只是把「收集结果」换成 `yield next_token` 逐个吐出。看懂本函数，四个函数就都看懂了。

#### 4.2.4 代码实践

1. **实践目标**：实测无 cache 生成的耗时随 token 数超线性增长，验证 4.2.2 的求和公式。
2. **操作步骤**（示例代码；权重文件约 1.5 GB，首次运行会经 `download_qwen3_small` 下载，回顾 [u1-l3]）：

   ```python
   # 示例代码：保存为 bench_greedy.py 后运行
   import time
   from pathlib import Path
   import torch

   from reasoning_from_scratch.ch02 import get_device, generate_text_basic
   from reasoning_from_scratch.qwen3 import (
       download_qwen3_small, Qwen3Model, Qwen3Tokenizer, QWEN_CONFIG_06_B
   )

   device = get_device()                                  # 模块一
   download_qwen3_small(kind="base", tokenizer_only=False, out_dir="qwen3")

   tokenizer = Qwen3Tokenizer(tokenizer_file_path=Path("qwen3") / "tokenizer-base.json")
   model = Qwen3Model(QWEN_CONFIG_06_B)
   model.load_state_dict(torch.load(Path("qwen3") / "qwen3-0.6B-base.pth", map_location=device))
   model.to(device)

   prompt = "Explain large language models in a single sentence."
   token_ids = torch.tensor(tokenizer.encode(prompt), device=device).unsqueeze(0)
   print("prompt token 数:", token_ids.shape[1])          # 应为 6

   for n in (20, 40):
       start = time.time()
       out = generate_text_basic(model, token_ids, max_new_tokens=n)  # 不传 eos
       elapsed = time.time() - start
       print(f"N={n:2d}  耗时 {elapsed:6.2f}s  "
             f"{n / elapsed:5.1f} tokens/sec  输出形状 {tuple(out.shape)}")
   ```

3. **需要观察的现象**：两次运行的输出形状分别为 `(1, 20)` 与 `(1, 40)`；计算 40 token 与 20 token 的耗时比值。
4. **预期结果**：比值明显大于 2（按 4.2.2 的公式约 3.3，注意力开销会让它再高一点），且 40 token 那次的 tokens/sec 更低。具体秒数随硬件差异很大，**待本地验证**；可同时与 notebook 2.9 节的多硬件基准表（CPU 无 cache 仅 5 tokens/sec）对照感受量级。

#### 4.2.5 小练习与答案

**练习 1**：`out = model(token_ids)[:, -1]` 中为什么只取最后一个位置？
**答案**：因果掩码保证每个位置只能看到自己及之前的 token，因此「下一个词」的分布恰好由最后位置给出；前面位置的 logits 对续写无用（它们预测的是序列内部早已确定的 token）。

**练习 2**：argmax 解码和「从分布中采样」有什么本质区别？
**答案**：argmax 永远选最大 logits 的 token，完全确定、同一输入必得同一输出；采样按概率分布抽取，会产生多样性。第 4 章的温度与 top-p（[u5-l1]、[u5-l2]）都是采样策略。

**练习 3**：为什么 `keepdim=True` 不能省？
**答案**：argmax 不加 `keepdim` 会把形状 `(b, vocab)` 压成 `(b,)`，失去序列维后无法直接与 `(b, T)` 的 `token_ids` 在 `dim=1` 上做 `torch.cat`。

### 4.3 模块三：`eos_token_id` 提前停止

#### 4.3.1 概念说明

贪心循环若只按 `max_new_tokens` 计数，模型常常「刹不住车」：notebook 2.7 节的输出在正确的回答之后打印出 `<|endoftext|>`，然后继续生成一段离题文字。原因很简单——`<|endoftext|>` 在预训练语料里是**文档分隔符**，模型输出了它，等于说「这段话说完了」，但循环不知道，把它当普通 token 拼回去继续生成。

解决办法是把 EOS 的 id 告诉生成函数：每步 argmax 之后检查新 token 是否等于 EOS，是则 `break`。于是 `max_new_tokens` 从「必须生成这么多」降级为「最多生成这么多」——这是所有后续生成函数（第 3 章评测、第 4 章采样）共享的约定。

base 模型的 EOS 是 `<|endoftext|>`（id `151643`），notebook 2.7 节用 `tokenizer.encode("<|endoftext|>")` 与 `tokenizer.eos_token_id` 两条路都验证了这一点；reasoning 变体则是 `<|im_end|>`（回顾 [u2-l6]）。

#### 4.3.2 核心流程

```
每步循环中（argmax 之后、拼接之前）:
    若 eos_token_id 不为 None 且 next_token == eos_token_id:
        break          # EOS 不进入输出序列
返回时: token_ids[:, input_length:]   # 只含生成部分，且不含 EOS
```

#### 4.3.3 源码精读

EOS 检查：[reasoning_from_scratch/ch02.py:L51-L54](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch02.py#L51-L54) —— 条件是 `eos_token_id is not None`（允许调用方不启用提前停止）且 `next_token.item() == eos_token_id`。检查位于 `torch.cat` **之前**，所以 EOS token 本身永远不会出现在返回结果里。

两个值得推敲的细节：

1. **注释与实现不完全一致**：L51 的注释写「Stop if all sequences in the batch...」，但 `next_token` 形状是 `(b, 1)`，`.item()` 要求张量恰好含一个元素——批大小超过 1 会直接报错。因此 `generate_text_basic` 实际只支持批大小 1；而流式版用的是 `torch.all(next_token == eos_token_id)`（[L107-L108](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/reasoning_from_scratch/ch02.py#L107-L108)），语义才是「批内全部到 EOS 才停」。批量生成正是附录 E（[u10-l2]）的主题。
2. **`None` 默认值的向后兼容**：`eos_token_id=None` 时跳过检查，函数退化为纯计数循环，方便对照实验。

真实调用示例：[ch02/05_use_model/generate_simple.py:L119-L126](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch02/05_use_model/generate_simple.py#L119-L126) —— 流式生成循环把 `eos_token_id=tokenizer.eos_token_id` 传进去，并在消费每个 yield 的 token 时实时 decode 打印。

#### 4.3.4 代码实践

1. **实践目标**：对比传与不传 `eos_token_id` 的输出差异，确认提前停止生效。
2. **操作步骤**（示例代码，接 4.2.4 已加载的 `model` / `tokenizer` / `token_ids`）：

   ```python
   # 示例代码
   from reasoning_from_scratch.ch02 import generate_text_basic

   print("eos_token_id =", tokenizer.eos_token_id)      # base 模型应为 151643

   out_no_eos = generate_text_basic(model, token_ids, max_new_tokens=100)
   print("不传 eos ->", tokenizer.decode(out_no_eos[0].tolist())[:200], "...")
   print("是否包含 <|endoftext|>:", "<|endoftext|>" in tokenizer.decode(out_no_eos[0].tolist()))

   out_eos = generate_text_basic(
       model, token_ids, max_new_tokens=100, eos_token_id=tokenizer.eos_token_id
   )
   print("传 eos   ->", tokenizer.decode(out_eos[0].tolist()))
   print("实际生成 token 数:", out_eos.shape[1])
   ```

3. **需要观察的现象**：不传 eos 的解码文本里能找到字面量 `<|endoftext|>`（`decode` 保留特殊 token，回顾 [u2-l6]），其后跟着新的离题内容；传 eos 的文本干净结束且不含该标记，实际生成 token 数小于 100。
4. **预期结果**：notebook 2.7 节在相同 prompt 上的计时记录是 1.39 秒 × 29 tokens/sec ≈ 40 个 token 后遇到 EOS 停止，因此传 eos 时 `out_eos.shape[1]` 应明显小于 100（约 40 上下）。你的机器上具体生成几个 token **待本地验证**（贪心解码是确定性的，同设备同权重结果可复现；不同设备可能因数值舍入略有差异，notebook 对此有说明）。

#### 4.3.5 小练习与答案

**练习 1**：EOS 检查为什么必须放在 `torch.cat` 之前？
**答案**：放在拼接之前，EOS 永远不会被写进序列，返回结果自然不含它；若放在拼接之后，就得在返回前再想办法剔除 EOS，容易出错。

**练习 2**：base 模型和 reasoning 模型的 `tokenizer.eos_token_id` 分别指向什么？
**答案**：base 是 `<|endoftext|>`（151643），reasoning 是 `<|im_end|>`。`Qwen3Tokenizer` 按分词器文件名自动判定（回顾 [u2-l6]），所以代码统一写 `tokenizer.eos_token_id` 即可，无需手动区分。

**练习 3**：如果对批大小为 2 的输入调用 `generate_text_basic` 并启用 EOS，会发生什么？
**答案**：`next_token.item()` 会抛出 ValueError——`.item()` 只接受单元素张量，而这里形状是 `(2, 1)`。这是注释与实现不一致的体现，批量场景需要用 `torch.all` 版本的函数（附录 E）。

## 5. 综合实践

把三个模块串成一个「迷你基准测试器」。在 4.2.4 已加载的模型上，完成以下任务：

1. **复刻函数**：不看源码，凭 4.2.2 的伪代码自己实现 `my_generate_text_basic(model, token_ids, max_new_tokens, eos_token_id=None)`（提示：整段前向 → `[:, -1]` → argmax → EOS 检查 → cat → 切 prompt 返回）。
2. **一致性验证**：对同一 prompt 分别调用你的实现和库函数（均传 `eos_token_id=tokenizer.eos_token_id`），断言两者输出的 token id 完全相同：`assert torch.equal(mine, lib)`。
3. **基准表**：用库函数（或你的实现）在 `max_new_tokens = 20, 40, 80` 三档下计时，输出一张表：`N | 耗时 | tokens/sec | 理论 token 前向量 LN + N(N-1)/2`（L 取你 prompt 的实际 token 数）。
4. **分析**：回答两个问题——(a) 耗时比值是否符合「超过 2 倍」的预期，与理论比值差多少？(b) 用一句话解释为什么 KV cache（下一讲）能把每步的前向长度从 \( L+i \) 降到 1。

预期：断言通过（贪心解码确定性）；三档 tokens/sec 递减；第 (b) 问的答案应指向「旧 token 的 K/V 已缓存，无需重算」。具体数值**待本地验证**。

## 6. 本讲小结

- `get_device` 按 CUDA → MPS → XPU → CPU 的优先级探测后端并返回 `torch.device`；在 CUDA 分支顺带打开 TF32，并按 torch 版本（>= 2.11 用新 `fp32_precision` API，否则旧 `allow_tf32`）做兼容，这是给 `torch.compile` 打的补丁。
- `generate_text_basic` 是最朴素的自回归循环：整段前向 → 取最后位置 logits → `argmax` 贪心选 token → 拼接 → 循环，最后切掉 prompt 只返回生成部分；`@torch.inference_mode()` + `model.eval()` 是推理标准姿势。
- 无 cache 时总前向量为 \( LN + N(N-1)/2 \)，生成量翻倍、耗时超过翻倍（L=6 时 40 token 约为 20 token 的 3.3 倍），这是 KV cache 存在的理由。
- EOS 提前停止把 `max_new_tokens` 从「必须」变成「最多」：检查放在拼接之前，EOS 不进输出；`.item()` 的写法使该函数实际仅支持批大小 1。
- 本讲的四函数族（basic / cache / stream / stream_cache）共享同一循环骨架，差异只在 cache 与 `yield`，它们是第 3 章评测与第 4 章采样函数的直接基础。

## 7. 下一步学习建议

- 下一讲 [u3-l2]（流式生成与生成函数族）：精读 `generate_text_basic_stream` / `generate_text_basic_stream_cache` 的 generator 机制、「跳过最后一次前向」优化，以及 `generate_stats` 的耗时与显存统计——并验证 cache 版与无 cache 版输出完全一致。
- 若想先吃透 KV cache 在模型侧的支撑，可回顾 [u2-l5]（KVCache 类、`current_pos` 游标、增量拼接）。
- 对推理加速感兴趣的话，可以提前翻看 [ch02/03_optimized-LLM/compare_inference.py](https://github.com/rasbt/reasoning-from-scratch/blob/b8646024377b0a35b44eddd2ec506c1a63f1b791/ch02/03_optimized-LLM/compare_inference.py)，那是 [u3-l3]（torch.compile）与 [u10-l1]（SDPA 优化实现）的素材。
- 建议同步阅读 notebook 第 2 章 2.6-2.8 节，把本讲的代码与书中的图示（生成过程逐迭代示意、KV cache 示意）对应起来。
