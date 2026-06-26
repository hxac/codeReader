# 环境搭建与运行第一个模型

> 前置承接：本讲延续 [u1-l1 项目定位与整体结构](./u1-l1-project-overview.md)。在上一讲我们已经知道：本仓库是《Build a Large Language Model (From Scratch)》的官方代码库，顶层按 `ch02`–`ch07` 与 `appendix-*` 组织，技术栈精简为「PyTorch + tiktoken +（加载权重用的）TensorFlow」。本讲不再重复这些结论，而是把读者从「知道项目长什么样」推进到「能在自己机器上跑出第一段模型生成」。

## 1. 本讲目标

学完本讲，你应当能够：

1. 用至少一种方式（本地 pip、Google Colab、Docker DevContainer）把项目依赖装好。
2. 运行 `ch04/01_main-chapter-code/gpt.py` 这个独立脚本，让它自动组装出一个 124M 参数的 GPT 模型，并对 `Hello, I am` 做一次自回归文本生成。
3. 看懂脚本里的 `main()` 流程：配置字典 → 构建模型 → 编码输入 → 生成 → 解码输出。
4. 理解 `model.eval()` 关闭 dropout 的作用，以及为什么这第一个脚本「故意只跑在 CPU 上」，而 GPU/设备选择要等到第 5 章才正式登场。

## 2. 前置知识

在动手前，用最少的语言交代三个概念：

- **依赖（dependency）**：Python 项目要正常运行，需要一批第三方库（如 `torch`、`tiktoken`）。`requirements.txt` 就是这张「进货清单」，一行一个，`pip` 能照着一次性装完。
- **CPU 与 GPU**：CPU 是通用处理器，什么都能算但慢；GPU（含 Apple 的 MPS）擅长大规模并行张量运算，跑神经网络更快。本项目主章节代码「设计为在普通笔记本上可跑通」，所以**没有 GPU 也能学完整本书**，只是训练阶段（ch5–ch7）有 GPU 会快很多。
- **推理模式（inference / eval mode）**：神经网络里有些层（如 dropout）在「训练」和「预测」时行为不同。生成文本属于预测，所以要调用 `model.eval()` 把这些层切到「关闭」状态，保证每次生成结果稳定可复现。

> 一个容易踩的坑：很多人以为「跑模型就一定要 GPU」。本讲会带你证明——124M 的 GPT 在普通 CPU 上生成 10 个 token 是秒级完成的。设备选择是「锦上添花」，不是「入门门槛」。

## 3. 本讲源码地图

本讲只涉及两个关键文件，刻意保持极小：

| 文件 | 作用 |
| --- | --- |
| [`setup/README.md`](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/setup/README.md) | 官方环境搭建指南，列出本地、Colab、Docker、Lightning Studio 等多种安装/运行方式。 |
| [`requirements.txt`](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/requirements.txt) | 依赖清单，逐行标注了每个库用在哪些章节。 |
| [`ch04/01_main-chapter-code/gpt.py`](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch04/01_main-chapter-code/gpt.py) | 第一个可独立运行的脚本：把第 2~4 章的代码汇总成一个文件，`main()` 里组装 GPT 模型并生成文本。 |

为什么是 `gpt.py` 而不是 `ch04.ipynb`？因为 `gpt.py` 是一个**纯 Python 脚本**，不依赖 Jupyter，命令行 `python gpt.py` 一键就能跑，最适合作为「第一跑」的入口。`ch04.ipynb` 是配套教材的交互式 notebook，便于分步学习，但启动它需要 JupyterLab。本讲先用最快的 `gpt.py` 跑通，建立信心。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：**依赖安装方式** → **运行 gpt.py 入口** → **设备选择与推理模式**。三者构成一条完整的「装好 → 跑通 → 看懂」链路。

### 4.1 依赖安装方式

#### 4.1.1 概念说明

Python 生态里「安装依赖」几乎等价于「让 `pip` 读一份清单，把里面列的库及其兼容版本下载安装」。本项目把所有主章节用到的库收敛在根目录的 `requirements.txt` 里，并且贴心地在每一行后面**用注释标明该库服务于哪些章节**，这样读者能一眼看出「我现在学的章节到底需要哪些库」。

`setup/README.md` 还给出多种安装/运行路径，覆盖不同读者画像：

- **本地 pip**：已有 Python 环境，最省事。
- **Google Colab**：连 Python 都不想装，直接在浏览器里跑（可白嫖 GPU）。
- **Docker DevContainer**：追求环境隔离、可复现。
- **Lightning Studio**：云端持久化开发环境。

#### 4.1.2 核心流程

本地最快路径只有两步：

1. 在仓库根目录执行 `pip install -r requirements.txt`。
2. 装完即可运行任意主章节脚本/notebook。

依赖清单的关键内容（按 `requirements.txt` 的行注释归纳）：

| 库 | 用途 | 主要章节 |
| --- | --- | --- |
| `torch` | 张量计算 + 自动求导，全书核心 | 全部章节 |
| `jupyterlab` | 运行 `.ipynb` notebook | 全部（读 notebook 才需要） |
| `tiktoken` | GPT-2 的 BPE 分词器 | ch02 / ch04 / ch05 |
| `tensorflow` | **仅用于**把 OpenAI 公开的 GPT-2 权重加载进来做结构验证 | ch05 / ch06 / ch07 |
| `matplotlib` | 画损失曲线等 | ch04 / ch06 / ch07 |
| `tqdm` / `pandas` / `psutil` | 进度条 / 数据表 / 进程信息 | ch05+ |

> 注意：本讲要跑的 `gpt.py` 实际只用 `torch` 和 `tiktoken` 两个库；`tensorflow`、`pandas` 等是后面章节才用得上的。也就是说，**装好 `requirements.txt` 后跑 `gpt.py` 一定不会缺依赖**，但即便你暂时只装 `torch` + `tiktoken`，`gpt.py` 本身也能跑通。

#### 4.1.3 源码精读

setup 文档把「最快的本地安装方式」放在最显眼的 Quickstart 段落：

[setup/README.md:L8-L14](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/setup/README.md#L8-L14) — 这是官方推荐的本地一键安装命令，在仓库根目录执行 `pip install -r requirements.txt` 即可。

对没有本地 Python 环境的读者，文档给出 Colab 的等价安装方式：

[setup/README.md:L18-L20](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/setup/README.md#L18-L20) — 用 `uv` 在 Colab 单元格里安装同一份 `requirements.txt`；额外可用 `uv pip install --group bonus` 一次性装齐所有附加材料（bonus）的依赖。

文档还明确交代了硬件门槛，这对初学者很重要：

[setup/README.md:L33-L35](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/setup/README.md#L33-L35) — 说明主章节代码「专为普通笔记本设计、不需要专用硬件」，作者在 M3 MacBook Air 上测试过全部主章节；若有 NVIDIA GPU，相关章节会自动利用它。

而依赖清单本身逐行标注了「库 ↔ 章节」对应关系：

[requirements.txt:L4-L11](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/requirements.txt#L4-L11) — 例如 `tiktoken >= 0.5.1  # ch02; ch04; ch05`，行尾注释清楚标出该库服务于哪些章节。

#### 4.1.4 代码实践

1. **实践目标**：用本地 pip 装好依赖，并确认 `torch`、`tiktoken` 可正常导入。
2. **操作步骤**：
   ```bash
   cd LLMs-from-scratch          # 进入仓库根目录
   pip install -r requirements.txt
   python -c "import torch, tiktoken; print('torch', torch.__version__); print('tiktoken ok')"
   ```
3. **需要观察的现象**：安装过程会拉取 `torch`（体积较大，几百 MB，耐心等待）；最后一条命令应打印出 `torch` 版本号和 `tiktoken ok`，且无报错。
4. **预期结果**：`torch` 版本 ≥ 2.2.2（Intel macOS 有额外上限约束，见 requirements.txt 第 1 行），`tiktoken` 可导入。
5. 若 `pip` 因网络慢而失败，可改用文档推荐的 Colab 方式，或给 `pip` 配置国内镜像后再重试（具体镜像地址待本地确认）。

#### 4.1.5 小练习与答案

**练习 1**：不看答案，说出 `requirements.txt` 里哪个库「只服务于加载 OpenAI GPT-2 权重、并不参与模型训练本身」。

> **参考答案**：`tensorflow`。它仅用于解析 OpenAI 用 TensorFlow 格式发布的 GPT-2 checkpoint（详见第 5 章 `gpt_download.py`），模型的前向、训练全程都用 `torch`。

**练习 2**：如果你只想跑本讲的 `gpt.py`，最少需要装哪两个库？

> **参考答案**：`torch` 和 `tiktoken`。`gpt.py` 顶部只 `import` 了这两个第三方库（其余是 Python 标准库和 `torch` 子模块）。

---

### 4.2 运行 gpt.py 入口

#### 4.2.1 概念说明

`gpt.py` 的文件头注释就说明了它的定位：「把第 2~4 章相关代码汇总到一个可独立运行的脚本里」。换句话说，它把前面几章散落在 notebook 里的零散组件（数据集类、多头注意力、LayerNorm、FeedForward、TransformerBlock、GPTModel、生成函数）**拼装成一条完整的可执行流水线**，并在文件末尾用一个 `main()` 函数把整条链路串起来演示。

这是全书第一个「点一下就出结果」的入口，意义在于：让你在还没深入理解每个组件之前，先看到「一个从零写的 GPT 模型，确实能接收文字、吐出文字」。

#### 4.2.2 核心流程

`main()` 的执行流程用伪代码表示：

```
main():
    1. 定义配置字典 GPT_CONFIG_124M        # 词表大小、上下文长度、层数等
    2. torch.manual_seed(123)              # 固定随机种子，保证可复现
    3. model = GPTModel(配置)              # 组装 124M 参数模型
    4. model.eval()                        # 切到推理模式，关闭 dropout
    5. start_context = "Hello, I am"
    6. tokenizer = tiktoken.get_encoding("gpt2")
    7. encoded = tokenizer.encode(文本)    # 文本 -> token id 列表
    8. encoded_tensor = tensor(encoded).unsqueeze(0)   # 加一维 batch
    9. out = generate_text_simple(model, encoded_tensor, max_new_tokens=10, context_size=1024)
    10. decoded_text = tokenizer.decode(out)           # token id 列表 -> 文本
    11. 打印输入、输出张量形状、生成文本
```

关键点：

- **第 8 步的 `unsqueeze(0)`** 不可少。`GPTModel.forward` 期望输入形状是 `(batch_size, seq_len)`，而 `encode` 返回的是一维 id 列表，必须补一个 batch 维。
- **第 9 步生成 10 个新 token**：模型此时是**随机初始化、未经训练**的，所以生成的是「语法上像 token、语义上是乱码」的文本——这是正常的，本讲只是验证「流水线跑通」，真正的训练在第 5 章。

#### 4.2.3 源码精读

文件头注释点明它的「汇总 + 可独立运行」定位：

[gpt.py:L1-L3](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch04/01_main-chapter-code/gpt.py#L1-L3) — 说明本文件汇集第 2~4 章代码，可作为独立脚本运行。

`main()` 的入口由标准 Python 约定触发：

[gpt.py:L276-L277](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch04/01_main-chapter-code/gpt.py#L276-L277) — `if __name__ == "__main__": main()`，所以直接 `python gpt.py` 即会调用 `main()`。

配置字典定义了 124M 模型的全部超参：

[gpt.py:L237-L245](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch04/01_main-chapter-code/gpt.py#L237-L245) — `GPT_CONFIG_124M`：词表 50257、上下文 1024、嵌入维 768、12 头、12 层、dropout 0.1、QKV 不加偏置。这些数字正是 GPT-2 small（124M）的规格。

构建模型并切到推理模式：

[gpt.py:L247-L249](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch04/01_main-chapter-code/gpt.py#L247-L249) — 先 `torch.manual_seed(123)` 固定随机权重，再 `GPTModel(...)` 组装，最后 `model.eval()` 关闭 dropout（详见 4.3 节）。

文本→token→补 batch 维三连：

[gpt.py:L253-L255](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch04/01_main-chapter-code/gpt.py#L253-L255) — 加载 `gpt2` 分词器，`encode` 得到一维 id 列表，`unsqueeze(0)` 补成 `(1, seq_len)` 的 batch 张量。

调用生成函数：

[gpt.py:L262-L268](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch04/01_main-chapter-code/gpt.py#L262-L268) — `generate_text_simple` 生成 10 个新 token，再用 `tokenizer.decode` 把整段 token id 解码回可读文本。

生成函数本身的逻辑（贪心解码的自回归循环）：

[gpt.py:L210-L233](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch04/01_main-chapter-code/gpt.py#L210-L233) — 每轮：裁剪上下文到 `context_size` 内 → 前向得到 logits → 只取最后一个时间步 → `argmax` 选概率最高的 token → 拼接到序列末尾。循环 `max_new_tokens` 次。注意其中第 220 行用 `with torch.no_grad():` 关闭梯度计算，因为生成阶段不需要反向传播。

#### 4.2.4 代码实践

1. **实践目标**：跑通 `gpt.py`，观察「未训练模型」的生成表现，并亲手改一段起始上下文。
2. **操作步骤**：
   ```bash
   cd ch04/01_main-chapter-code
   python gpt.py
   ```
   然后用编辑器打开 `gpt.py`，把第 251 行的 `start_context = "Hello, I am"` 改成例如 `start_context = "Once upon a time"`，再次运行。
3. **需要观察的现象**：脚本先打印输入文本、`Encoded input text`（一串整数 token id）、`encoded_tensor.shape`（应为 `torch.Size([1, 4])`，即 batch=1、4 个 token）；再打印输出张量与 `Output length`（应为 `14`，即 4 + 10），最后是 `Output text`。由于模型未训练，这段文本**语义上接近乱码**。
4. **预期结果**：换不同的 `start_context` 后，输出乱码内容会随之改变；但每次用同一 `start_context` + 同一随机种子（123），输出**完全一致**（可复现）。
5. **关于输出乱码的精确文本**：因输出依赖本地运行环境与（已固定的）随机种子，具体字符串「待本地验证」，但它一定是「读起来不通顺」的。

#### 4.2.5 小练习与答案

**练习 1**：如果把 `main()` 里第 8 步的 `unsqueeze(0)` 去掉，会发生什么？

> **参考答案**：输入张量会变成一维 `(seq_len,)`。`GPTModel.forward` 里 `batch_size, seq_len = in_idx.shape` 解包会失败（一维张量只能解出一个值），直接报 `ValueError`。这印证了「模型按 batch 维组织输入」的约定。

**练习 2**：把 `max_new_tokens=10` 改成 `50`，`Output length` 会变成多少？为什么？

> **参考答案**：变成 `4 + 50 = 54`。因为输入是 4 个 token，每轮循环追加 1 个新 token，循环 50 次就增加 50 个。输出长度始终 = 初始 token 数 + `max_new_tokens`。

**练习 3**：脚本运行时会不会去联网下载模型权重？

> **参考答案**：不会。`gpt.py` 的模型是**本地随机初始化**的（`torch.manual_seed(123)` 仅固定随机性），所以无需联网。联网下载 OpenAI GPT-2 权重是第 5 章 `gpt_download.py` 才做的事。

---

### 4.3 设备选择与推理模式

#### 4.3.1 概念说明

这是本讲最容易产生误解的地方，需要分两层说清楚：

**第一层：这第一个脚本 `gpt.py` 故意只跑在 CPU 上。** 仔细读 `main()` 你会发现——它**没有**任何 `.to(device)` 或 `torch.device(...)` 调用，模型和张量都留在默认设备 CPU。原因是：124M 模型很小，只生成 10 个 token，CPU 上是秒级完成，引入设备管理反而会让「第一跑」变复杂。这是作者刻意的教学取舍：**先把流程跑通，再谈加速**。

**第二层：真正的设备选择模式出现在第 5 章。** 当进入训练（要反复前向 + 反向成千上万次）时，GPU 的价值才体现出来。那时书里会引入一句贯穿 ch5–ch7 的标准写法：

```python
device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
```

所以本节要建立的认知是：**`setup/README.md` 说的「有 NVIDIA GPU 会自动利用」指的是全书训练代码（尤其 ch5–ch7）**，而**本讲的 `gpt.py` 演示脚本本身并不使用 GPU、纯 CPU 运行**——这两者并不矛盾。

**关于 `model.eval()`（推理模式）**：模型里有一批层在训练/预测时行为不同，最典型的就是 **dropout**——训练时随机置零一部分神经元以防过拟合，预测时则应关闭、让所有神经元都参与。`model.eval()` 就是把这些层切到「预测」状态，确保生成结果稳定、可复现。

#### 4.3.2 核心流程

- **本讲 `gpt.py` 的设备处理**：全程 CPU，无需手动设置。`GPTModel.forward` 里只有一处与「设备」有关的细节——给位置编码生成索引时，让索引和输入张量待在**同一个设备**上，避免跨设备报错。
- **`model.eval()` 的作用**：进入预测模式，关闭 dropout 等训练专用行为。
- **`torch.no_grad()` 的配合**：生成阶段不需要梯度，包在 `with torch.no_grad():` 里可省内存、提速（见 `generate_text_simple` 第 220 行）。
- **后续（第 5 章）的设备选择范式**：用 `torch.device(...)` 选择 `cuda`/`cpu`，再把模型和数据 `.to(device)`。

#### 4.3.3 源码精读

`main()` 切换到推理模式的那一行：

[gpt.py:L249](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch04/01_main-chapter-code/gpt.py#L249) — `model.eval()` 关闭 dropout，保证生成结果在固定种子下可复现。

`GPTModel.forward` 里唯一的「设备一致性」处理（这是本脚本里唯一出现 `device` 字样的地方）：

[gpt.py:L198-L207](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch04/01_main-chapter-code/gpt.py#L198-L207) — 第 201 行 `torch.arange(seq_len, device=in_idx.device)` 让位置索引与输入 token 在同一设备；本讲场景下 `in_idx.device` 就是 CPU。

生成函数里关闭梯度计算的上下文管理器：

[gpt.py:L220-L225](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch04/01_main-chapter-code/gpt.py#L220-L225) — `with torch.no_grad(): logits = model(idx_cond)`，生成阶段不构建计算图，省内存、更快。

作为对比，第 5 章训练脚本才正式引入「按 GPU 是否可用选设备」的标准范式：

[ch05/01_main-chapter-code/gpt_train.py:L134](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch05/01_main-chapter-code/gpt_train.py#L134) — `device = torch.device("cuda" if torch.cuda.is_available() else "cpu")`。这正是 `setup/README.md` 所说「有 NVIDIA GPU 会自动利用」的代码落点；本讲的 `gpt.py` 并没有这一步。

#### 4.3.4 代码实践

1. **实践目标**：验证 `model.eval()` 对生成可复现性的影响，并感受 CPU 下生成的速度。
2. **操作步骤**：
   - 连续运行两次 `python gpt.py`，比对两次的 `Output text` 是否完全一致。
   - 在 `main()` 里把 `model.eval()` 注释掉，再连跑两次，观察输出是否仍然一致。
   - 在 `generate_text_simple` 调用前后各加一行时间戳（示例代码，非项目原有代码）：
     ```python
     import time
     t0 = time.time()
     out = generate_text_simple(model=model, idx=encoded_tensor, max_new_tokens=10, context_size=GPT_CONFIG_124M["context_length"])
     print("耗时(秒):", time.time() - t0)
     ```
3. **需要观察的现象**：保留 `model.eval()` 时两次输出**完全相同**；注释掉 `model.eval()` 后，由于 dropout 在预测时仍随机置零，两次输出**可能不同**。CPU 上生成 10 个 token 通常在秒级以内。
4. **预期结果**：`model.eval()` 是「可复现」的关键开关之一（另一个是 `torch.manual_seed(123)`）；CPU 速度足以支撑本讲演示。
5. 若想验证 GPU 是否被「未来的训练代码」利用，需等到第 5 章；本讲脚本本身不会触发 GPU（待本地确认你机器的 CUDA 可用性）。

#### 4.3.5 小练习与答案

**练习 1**：`setup/README.md` 说「有 NVIDIA GPU 时代码会自动利用它」，但本讲的 `gpt.py` 运行时并不会用 GPU，这两句话矛盾吗？

> **参考答案**：不矛盾。「自动利用 GPU」指的是第 5 章及之后**训练/微调代码**里的 `device = torch.device("cuda" if torch.cuda.is_available() else "cpu")` 范式（见 `gpt_train.py:134`）。`gpt.py` 是第 4 章的纯演示脚本，作者为了简单**没有**写设备迁移代码，所以它跑在 CPU 上。两段代码职责不同。

**练习 2**：`model.eval()` 和 `torch.no_grad()` 都和「不训练」有关，它们是一回事吗？

> **参考答案**：不是。`model.eval()` 改变**特定层的行为**（如关闭 dropout、让 BatchNorm 用全局统计量）；`torch.no_grad()` 关闭的是**梯度计算**（不构建计算图、省内存）。生成文本时两者都用：前者保证行为稳定，后者省资源。注意 `gpt.py` 的 `model.eval()` 在 `main()` 里，而 `torch.no_grad()` 在 `generate_text_simple` 内部。

**练习 3**：为什么 `GPTModel.forward` 里生成位置索引时要写 `device=in_idx.device`？

> **参考答案**：为了「设备一致性」。位置索引张量（`torch.arange(seq_len)`）默认创建在 CPU，若输入 `in_idx` 在 GPU 上，两者相加会因跨设备报错。写 `device=in_idx.device` 保证索引跟输入同设备。在 CPU-only 的 `gpt.py` 里它俩都是 CPU，所以这一行暂时「看不出效果」，但它是为后续 GPU 训练预留的正确写法。

## 5. 综合实践

把本讲三个模块串成一个完整小任务：**从零装好环境 → 跑通第一段生成 → 解释每一个打印行 → 改造它**。

任务步骤：

1. 按 4.1 的方式安装依赖，确认 `torch`、`tiktoken` 可导入。
2. 进入 `ch04/01_main-chapter-code/` 运行 `python gpt.py`，把控制台**完整输出**抄写或截图保存。
3. 对照 4.2 的源码精读，用你自己的话解释输出的四部分各对应 `main()` 的哪几行：输入文本、编码 id、张量形状、生成文本。
4. 做三个小改造并记录现象：
   - 把 `start_context` 改成一段更长的中文或英文句子，观察 `encoded_tensor.shape` 如何随 token 数变化。
   - 把 `max_new_tokens` 从 `10` 调到 `30`，验证 `Output length` = 初始 token 数 + 30。
   - 注释掉 `model.eval()`，连跑两次，验证输出是否仍可复现。
5. **思考题（写一句话作答）**：脚本生成的文本读不通顺，是因为「环境没装好」还是「模型没训练」？依据是什么？

> 参考结论：是「模型没训练」。`main()` 用 `torch.manual_seed(123)` 随机初始化权重、从未在任何数据上学习过；只要脚本不报错且输出了 token，就说明环境与流水线是通的。乱码的根因是缺训练，这正是第 5 章要解决的问题。

## 6. 本讲小结

- 装依赖的最快方式是在仓库根目录 `pip install -r requirements.txt`；`requirements.txt` 逐行标注了「库 ↔ 章节」对应关系。
- 第一个可运行入口是 `ch04/01_main-chapter-code/gpt.py`，它把第 2~4 章代码汇总成独立脚本，`python gpt.py` 一键出结果。
- `main()` 流程：配置字典 → `manual_seed` → 构建 `GPTModel` → `model.eval()` → tiktoken 编码 → `unsqueeze(0)` 补 batch 维 → `generate_text_simple` 生成 → 解码打印。
- 这个脚本**故意只跑在 CPU 上**，没有 `.to(device)`；GPU/设备选择的 `torch.device(...)` 范式要等到第 5 章训练才正式登场。
- `model.eval()` 关闭 dropout 以保证生成可复现；`torch.no_grad()` 在生成阶段关闭梯度计算以省内存。
- 未训练模型的生成结果是「语法像 token、语义是乱码」，属正常现象，验证的是「流水线跑通」而非「模型能力」。

## 7. 下一步学习建议

- **横向打通（强烈推荐先做）**：进入 [u1-l3 仓库阅读地图：章节约定与代码汇总机制](./u1-l3-repo-reading-map.md)，理解 `previous_chapters.py` 这套「章节间自底向上复用代码」的机制——你会明白 `gpt.py` 这种「汇总脚本」在整个项目里扮演的角色。
- **纵向深入数据层**：进入 [u2-l1 分词与词表构建](./u2-l1-tokenization-vocabulary.md)，从「文本怎么变成数字」开始，逐步拆解 `gpt.py` 里被「黑盒」掉的前端（分词、词表、滑动窗口、嵌入）。
- **关于设备与训练**：如果你急着想体验 GPU 加速和真正的训练，可以提前翻看 [ch05/01_main-chapter-code/gpt_train.py:L134](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch05/01_main-chapter-code/gpt_train.py#L134) 的设备选择范式，但系统学习建议放到 u5 单元。
