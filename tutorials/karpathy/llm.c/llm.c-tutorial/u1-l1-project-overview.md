# 项目总览与定位：llm.c 是什么

## 1. 本讲目标

本讲是整本学习手册的第一篇，不写一行难懂的公式，只解决三个问题：

1. **llm.c 到底是什么**——它要做什么、为什么值得学。
2. **三套核心实现各自扮演什么角色**——根目录下的 `train_gpt2.c`、`train_gpt2.cu`、`train_gpt2.py` 有什么区别，分别适合谁读、谁跑。
3. **整个仓库的目录是怎么组织的**——`dev/`、`llmc/`、`doc/`、`scripts/` 各自负责什么，当你想找某个东西时该去哪里翻。

学完本讲，你应当能在不看答案的情况下，指着根目录下任意一个 `train_*` / `test_*` 文件说出「它是哪一套实现、用来干什么」。

## 2. 前置知识

本讲几乎不需要编程基础，但有几个名词先建立直觉，后面读源码会更顺：

- **GPT-2 / GPT-3**：OpenAI 发布的两代「生成式语言模型」。它们把一段文字切成一个个「token（词片）」，然后预测「下一个 token 是什么」。llm.c 的核心目标之一，就是**用最朴素的 C/CUDA 代码复现 GPT-2（124M）乃至 GPT-3 系列的训练过程**。
- **Transformer**：GPT-2 / GPT-3 背后的神经网络结构。它由很多层「注意力（attention）+ 前馈（MLP）」堆叠而成。本讲只关心「这个仓库怎么组织这些层的代码」，具体每层的数学留到后面几单元。
- **前向 / 反向 / 训练循环**：神经网络训练的三件套。「前向」是把数据喂进网络算出预测；「反向」是根据预测误差算每个参数该怎么调；「训练循环」就是反复「前向→反向→更新参数」。
- **几个尺寸缩写**（源码里到处都是）：`B` = batch size（一批多少条），`T` = sequence length（一条序列多少个 token），`C` = channels（每个 token 的向量维度），`V` = vocab size（词表大小），`L` = num_layers（网络层数）。
- **CUDA**：NVIDIA 显卡的并行编程模型。用 CUDA 写的代码（`.cu` 文件）可以同时在成千上万个 GPU 核心上跑，比 CPU 快得多。
- **cuBLAS / cuDNN / NCCL**：NVIDIA 提供的三类高性能库。cuBLAS 做矩阵乘，cuDNN 做深度学习算子（如 Flash Attention），NCCL 做多 GPU 通信。llm.c 在追求速度时大量调用它们。

> 如果上面某些名词还模糊，没关系——本讲只用它们做「定位」，不会展开数学。等进入第 2、3 单元再细讲。

## 3. 本讲源码地图

本讲涉及的文件都在仓库根目录与顶层文档，作用如下：

| 文件 | 作用 |
| --- | --- |
| `README.md` | 项目的「说明书」：定位、三种运行方式、测试、多卡/多节点训练、项目理念。本讲最重要的入口。 |
| `train_gpt2.c` | **CPU 参考实现**：约 1182 行纯 C（带少量 OpenMP），单文件、可读性最高，是理解 GPT-2 每一层的最佳起点。 |
| `train_gpt2.cu` | **CUDA 主线实现**：约 1904 行，是仓库真正「跑得快、能复现大模型」的生产级代码，通过 `llmc/` 头文件库组织各层。 |
| `train_gpt2.py` | **PyTorch 参考实现**：约 860 行，nanoGPT 风格，用来生成 `.bin` 权重给 C 当初始化，也是 C 实现的「正确性标尺」。 |
| `Makefile` | 构建脚本，负责自动探测环境并编译出不同的可执行文件。 |

## 4. 核心概念与源码讲解

### 4.1 项目目标与 GPT-2/GPT-3 复现定位

#### 4.1.1 概念说明

一句话定位：**llm.c 想用「简单、纯粹的 C/CUDA」来训练 GPT-2 / GPT-3 这样的语言模型，不依赖动辄几百 MB 的 PyTorch。**

这个定位包含两层意图，理解这两层意图，就理解了整个仓库为什么会这样组织：

- **教育意图**：让你看清「训练一个大语言模型」到底每一步在做什么。所以它故意保留一份**人类可读的、单文件的 C 参考**，每一层的前向/反向都写得明明白白。
- **工程意图**：它又要**真的够快**，能复现大模型训练（如 GPT-2 1.6B）。所以它在另一条线上毫不客气地调用 cuBLAS、cuDNN、NCCL 这些最快的高性能库。

这两层意图有时候会打架（要快就得用复杂代码，要可读就得保持简单）。仓库的取舍原则我们放在 4.1.3 看。

#### 4.1.2 核心流程

从「我想训练一个 GPT-2」到「模型生成出文字」，整体链路是这样的（本讲只看宏观，细节留到后续单元）：

```text
准备数据                前向计算                  评估/采样
-----------            -----------------        --------------
文本 → 分词 →         embedding → 多层         验证集 loss
token 流 (.bin)  →    Transformer →           → 采样生成
                      logits（预测）          → 打印文字
                           ↑
                     反向传播 + AdamW 更新参数（训练循环）
```

- **数据**：原始文本被分词后存成 `.bin`（一串 token id）。
- **前向**：token id 进入网络，逐层计算，最后输出「每个位置预测下一个 token 的概率」。
- **训练**：用预测误差做反向传播，用 AdamW 优化器更新参数，循环往复。
- **采样**：训练到一定步数后，根据模型给出的概率「掷骰子」选下一个 token，一段段生成文字。

llm.c 把这条链路在三种实现里都完整跑通了一遍。

#### 4.1.3 源码精读

**项目定位**——README 开篇一句话点明了「不依赖庞大框架」的初衷：

> LLMs in simple, pure C/CUDA with no need for 245MB of PyTorch or 107MB of cPython.（用简单纯粹的 C/CUDA 实现 LLM，不需要 245MB 的 PyTorch 或 107MB 的 cPython。）

[README.md:3](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/README.md#L3) 这段同时说明了当前重点是 **pretraining（预训练）**，并且要**复现 GPT-2 与 GPT-3 系列**，外加一份并行的 PyTorch 参考实现。

**项目理念（教育 vs. 速度的取舍）**——README 的 `repo` 小节把仓库的「人格」讲得很清楚：

- 教育优先：`dev/cuda` 是一个「手写、文档详尽、从简单到复杂」的内核教学库。
- 也要够快：要能复现 GPT-2 1.6B 训练，所以该用 cuBLAS / cuDNN / NCCL 就用。
- **主线代码必须简单可读**：根目录的默认文件拒绝「为了快 2% 而引入 500 行复杂代码」。

[README.md:193-201](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/README.md#L193-L201) 这一段是理解「为什么根目录有 `.c`，而 `dev/` 下又有更复杂的 `.cu`」的钥匙：根目录保简单，`dev/` 当实验场。

**CPU 参考的自我声明**——`train_gpt2.c` 文件顶部的注释直接告诉读者「我就是那个干净的最小参考」：

```c
/*
This file trains the GPT-2 model.
This version is the clean, minimal, reference. As such:
- it runs on CPU.
- it does not make the code too complex; it is readable.
...
*/
```

见 [train_gpt2.c:1-9](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L1-L9)。这段注释是整个学习路线的「锚点」——后面 7 篇前向/反向讲义几乎都以它为骨架。

#### 4.1.4 代码实践

**实践目标**：亲手在 README 里定位「项目定位」与「项目理念」，而不是停留在二手描述。

**操作步骤**：

1. 打开 `README.md`，找到第 3 行（项目自我介绍）。
2. 找到 `## repo` 小节（约第 191 行起），读完「A few more words on what I want this repo to be」整段。
3. 在 `dev/cuda` 下随便打开一个内核文件（如 `dev/cuda/layernorm_forward.cu`），扫一眼它是否如 README 所说「从简单 kernel 到复杂 kernel 都有」。

**需要观察的现象**：

- README 第 3 行同时出现了「C/CUDA」「复现 GPT-2/GPT-3」「PyTorch 参考」三件套。
- `repo` 小节明确区分了「根目录保简单」与「`dev/` 可复杂」两种态度。

**预期结果**：你能用自己的话写出：「llm.c = 用纯 C/CUDA 复现 GPT-2/GPT-3 训练，主线代码追求简单可读，速度靠 `dev/` 与高性能库来补。」

#### 4.1.5 小练习与答案

**练习 1**：README 说 llm.c 比 PyTorch Nightly 快多少？这个说法出现在哪里？
**参考答案**：README 第 3 行说 llm.c 当前比 PyTorch Nightly 快约 7%（by about 7%）。这说明「主线 CUDA 代码在追求速度」不是空话。

**练习 2**：项目作者明确表示「不希望」在这个仓库里维护什么？为什么？
**参考答案**：作者希望仓库只维护 C 和 CUDA 代码（README 第 3 行）。移植到其他语言（Rust、Go、Java……）很欢迎，但应在**独立仓库**完成，本仓库只负责链接到它们（见 README 的 notable forks 一节）。理由是保持主线聚焦、可读、可维护。

---

### 4.2 三套实现的分工与对照

#### 4.2.1 概念说明

仓库根目录下有三份「同一个 GPT-2」的实现，它们**做的事一样（训练 GPT-2），但分工完全不同**。理解它们的分工，是你后续决定「先读哪一个」的前提。

| 实现 | 语言/设备 | 角色 | 适合谁 |
| --- | --- | --- | --- |
| `train_gpt2.c` | 纯 C / CPU | **教学参考**：每一层都写得最清楚 | 想彻底搞懂每一层算法的人 |
| `train_gpt2.cu` | CUDA / GPU | **主线工程**：最快、能复现大模型、支持多卡多节点 | 想跑真训练、研究高性能的人 |
| `train_gpt2.py` | PyTorch / GPU | **对照基准**：生成 `.bin` 权重 + 正确性标尺 | 想和「标准答案」对照、或想快速实验的人 |

这三者不是竞争关系，而是一条**协作链**：`train_gpt2.py` 产出权重和「标准答案」→ `train_gpt2.c` / `.cu` 读取它们 → 用单元测试比对，确认 C/CUDA 实现没写错。

#### 4.2.2 核心流程

三套实现的协作链如下：

```text
train_gpt2.py
   │  1. 用 PyTorch 定义 GPT-2（nanoGPT 风格）
   │  2. 下载/转换 OpenAI 权重
   │  3. write_model → 写出 gpt2_124M.bin（fp32/bf16）
   │  4. write_state → 写出 debug_state.bin（标准前向/梯度）
   ▼
gpt2_124M.bin  ──►  train_gpt2.c (CPU)      ──┐
debug_state.bin ──► train_gpt2.cu (CUDA)    ──┤  test_gpt2(.c/.cu) 比对 → overall okay: 1
                  (gpt2_build_from_checkpoint) │
                                              └─ 正确性闭环
```

关键点：

- `.bin` 是 PyTorch 和 C 之间的「共同语言」——一种简单的二进制权重格式（第 4 单元会专门讲它的协议）。
- `debug_state.bin` 是一小批数据 + 期望的激活/梯度，专门用来做**单元测试的标尺**。
- `gpt2_build_from_checkpoint` 是 C/CUDA 端「按 `.bin` 把模型搭起来」的对称读取函数。

#### 4.2.3 源码精读

**PyTorch 参考：自报家门**。`train_gpt2.py` 顶部说明它既是参考实现，又承担「为 C 生成权重」的职责：

```python
"""
Reference code for GPT-2 training and inference.
Will save the model weights into files, to be read from C as initialization.
"""
```

见 [train_gpt2.py:2-3](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.py#L2-L3)。它进一步注明参考了 OpenAI 官方 TF 实现与 HuggingFace 实现（[train_gpt2.py:5-9](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.py#L5-L9)）。它用 `nn.Module` 定义模型，例如 GELU 激活 [train_gpt2.py:40-43](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.py#L40-L43) 和因果自注意力 [train_gpt2.py:48-63](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.py#L48-L63)。

**CPU 参考：结构体即「模型」**。`train_gpt2.c` 用最朴素的方式定义模型配置与参数。配置就是一个普通结构体：

```c
typedef struct {
    int max_seq_len;       // 最大序列长度，如 1024
    int vocab_size;        // 词表大小，如 50257
    int padded_vocab_size; // 填充到 %128==0，如 50304
    int num_layers;        // 层数，如 12
    int num_heads;         // 注意力头数，如 12
    int channels;          // 通道数，如 768
} GPT2Config;
```

见 [train_gpt2.c:526-533](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L526-L533)。参数则是一组指针，固定 16 个张量（`#define NUM_PARAMETER_TENSORS 16`，[train_gpt2.c:536](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L536)），激活固定 23 个（`#define NUM_ACTIVATION_TENSORS 23`，[train_gpt2.c:601](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L601)）。这里没有类、没有继承，只有「结构体 + 指针」，对初学者极其友好。

**CUDA 主线：同一份模型，换了一套地基**。`train_gpt2.cu` 的 `GPT2Config` 与 CPU 版几乎逐字相同 [train_gpt2.cu:87-94](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L87-L94)，但参数指针的类型从 `float*` 换成了 `floatX*`——这个 `floatX` 是随编译选项 `PRECISION` 在 fp32/bf16/fp16 之间切换的「精度别名」，并有一行 `static_assert` 在编译期校验「正好 16 个指针」：

```c
constexpr const int NUM_PARAMETER_TENSORS = 16;
typedef struct {
    floatX* wte;     // (V, C)
    floatX* wpe;     // (maxT, C)
    ...
} ParameterTensors;
static_assert(sizeof(ParameterTensors) == NUM_PARAMETER_TENSORS * sizeof(void*), "Inconsistent sizes!");
```

见 [train_gpt2.cu:97-116](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L97-L116)。这条 `static_assert` 是个很妙的设计：它保证「结构体里指针个数」和「后面 fill_sizes 循环用的常量」永远一致，改一边忘改另一边时编译就报错。

**CUDA 主线如何组织「层」**：`train_gpt2.cu` 不像 CPU 版那样把所有层塞在一个文件里，而是 `#include` 了一整组 `llmc/` 头文件，分四块——CPU 工具、GPU 工具、各层 CUDA 实现、多卡支持：

```c
// ----------- CPU utilities -----------
#include "llmc/utils.h"
#include "llmc/tokenizer.h"
#include "llmc/dataloader.h"
...
// ----------- GPU utilities -----------
#include "llmc/cuda_common.h"
#include "llmc/cuda_utils.cuh"
#include "llmc/cublas_common.h"
// ----------- Layer implementations in CUDA -----------
#include "llmc/encoder.cuh"
#include "llmc/layernorm.cuh"
#include "llmc/matmul.cuh"
...
// ----------- Multi-GPU support -----------
#include "llmc/zero.cuh"
```

见 [train_gpt2.cu:12-71](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L12-L71)。这段 `#include` 列表其实就是 `train_gpt2.cu` 的「目录页」——第 5 单元我们会逐个深入这些头文件。

> **旁注（fp32 legacy 版）**：根目录还有 `train_gpt2_fp32.cu`（约 1754 行），它是「CUDA 主线」冻结下来的早期版本——只用 cuBLAS、单卡 fp32、自包含（不依赖 `llmc/` 层头文件）。README 把它定位为「学 CUDA 的更简单、更可移植的入口」。它的自我声明见 [train_gpt2_fp32.cu:1-12](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2_fp32.cu#L1-L12)，且 `#include` 极简（[train_gpt2_fp32.cu:29-34](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2_fp32.cu#L29-L34)）。本讲先把它当作「第 4 单元会专门讲的过渡版」，不展开。

#### 4.2.4 代码实践

**实践目标**：用三个文件各自的「自我声明」建立第一手印象，而不是听转述。

**操作步骤**：

1. 分别打开三个文件的开头：[train_gpt2.c:1-9](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L1-L9)、[train_gpt2.cu:1-3](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L1-L3)、[train_gpt2.py:1-17](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.py#L1-L17)。
2. 填写下面这张「三行表」（每行一句话）：

| 文件 | 第一句话/注释说明了它是什么？ | 它依赖 PyTorch 吗？ |
| --- | --- | --- |
| `train_gpt2.c` |  |  |
| `train_gpt2.cu` |  |  |
| `train_gpt2.py` |  |  |

**需要观察的现象**：

- `train_gpt2.c` 顶部明确写着「runs on CPU」「clean, minimal, reference」。
- `train_gpt2.cu` 顶部只有一行「GPT-2 Transformer Neural Net training loop. See README.md for usage.」，不啰嗦，靠 `llmc/` 头文件组织复杂度。
- `train_gpt2.py` 顶部写着「Reference code ... Will save the model weights into files, to be read from C」——点明它是「产出权重的参考」。

**预期结果**：你能用一句话概括三者的关系——「`.py` 出权重和标准答案，`.c` 教你看懂每一层，`.cu` 让它跑得飞快」。

#### 4.2.5 小练习与答案

**练习 1**：`train_gpt2.c` 和 `train_gpt2.cu` 的 `GPT2Config` 内容是否基本相同？为什么参数指针类型却不同？
**参考答案**：`GPT2Config` 的字段（`max_seq_len`、`vocab_size`、`num_layers`、`num_heads`、`channels` 等）基本逐字相同（[train_gpt2.c:526-533](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L526-L533) vs [train_gpt2.cu:87-94](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L87-L94)）。但 CPU 版参数是 `float*`（固定 fp32），CUDA 版是 `floatX*`（可随 `PRECISION` 在 fp32/bf16/fp16 间切换）——因为 CUDA 主线要支持混合精度训练。

**练习 2**：为什么 `train_gpt2.cu` 里要写一行 `static_assert(sizeof(ParameterTensors) == NUM_PARAMETER_TENSORS * sizeof(void*), ...)`？
**参考答案**：这是一个编译期保险（[train_gpt2.cu:116](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L116)）。它确保「结构体里实际有多少个指针」和「后续循环 `fill_in_parameter_sizes` 用的 `NUM_PARAMETER_TENSORS` 常量」保持一致——哪天有人加了/删了一个参数张量却忘了同步常量，编译阶段就会直接报错，而不是在运行时悄悄越界。

---

### 4.3 仓库目录结构地图

#### 4.3.1 概念说明

一个稍大的项目，光看根目录的几个文件是不够的。llm.c 把代码按「角色」拆进了几个目录，记住每个目录的职责，你就能快速定位：

- **根目录**：默认/主线代码 + 构建/测试入口。作者对这里的「复杂度」最敏感，要保持简单可读。
- **`dev/`**：实验场与教学库——这里可以「局部复杂」，装着手写内核库、数据脚本、评测、测试。
- **`llmc/`**：CUDA 主线专用的**头文件库**（`.cuh`/`.h`），每个头文件负责一层或一类工具，被 `train_gpt2.cu` `#include`。
- **`doc/`**：教程文档（目前有 LayerNorm 的逐层教程）。
- **`scripts/`**：复现训练的启动脚本（各种规模的 GPT-2/GPT-3、多节点）。

#### 4.3.2 核心流程

当你在仓库里找东西时，按这张「决策树」走：

```text
我想……
├─ 跑通/复现一次训练        → scripts/run_gpt2_*.sh（先看 scripts/README.md）
├─ 看懂某一层的算法         → 根目录 train_gpt2.c（最清楚）
├─ 看某一层在 GPU 上怎么做   → llmc/<层>.cuh（如 llmc/layernorm.cuh）
├─ 对比同一层的多种优化实现  → dev/cuda/<层>.cu（多版本内核库）
├─ 准备/下载数据            → dev/data/<数据集>.py
├─ 评测模型                 → dev/eval/、dev/data/hellaswag.py、mmlu.py
├─ 入门教程                 → doc/layernorm/
└─ 搞清楚怎么编译           → Makefile（下一讲专题）
```

#### 4.3.3 源码精读

**顶层目录职责一览**（基于实际 `ls` 结果）：

| 路径 | 内容（实例） | 职责 |
| --- | --- | --- |
| 根目录 | `train_gpt2.c` `.cu` `.py`、`train_gpt2_fp32.cu`、`train_llama3.py`、`test_gpt2.c` `.cu`、`profile_gpt2.cu`、`Makefile` | 主线实现 + 构建/测试/剖析入口，须保持简单 |
| `llmc/` | `encoder.cuh`、`layernorm.cuh`、`matmul.cuh`、`attention.cuh`、`adamw.cuh`、`zero.cuh`、`schedulers.h`、`dataloader.h`、`tokenizer.h` … | CUDA 主线的头文件库（被 `train_gpt2.cu` include） |
| `dev/cuda/` | `layernorm_forward.cu`、`attention_forward.cu`、`matmul_forward.cu`、`softmax_forward.cu` … + 自带 `Makefile`/`README.md` | **手写内核教学库**：同一层多个版本（从朴素到优化）+ benchmark |
| `dev/cpu/` | `matmul_forward.c` | CPU 端的优化参考内核（如带 cache-blocking 的 matmul） |
| `dev/data/` | `tinyshakespeare.py`、`fineweb.py`、`hellaswag.py`、`mmlu.py`、`data_common.py` … | 数据下载/分词/存成 `.bin` 的脚本 |
| `dev/eval/` | 评测运行与汇总脚本 | 模型评测（HellaSwag / MMLU 等） |
| `dev/test/` | 额外测试 | 辅助测试代码 |
| `doc/layernorm/` | `layernorm.md`、`layernorm.py`、`layernorm.c` | 单层（LayerNorm）的逐步教程，C/PyTorch 对照 |
| `scripts/` | `run_gpt2_124M.sh`、`run_gpt2_350M.sh`、`run_gpt2_774M.sh`、`run_gpt2_1558M.sh`、`run_gpt3_125M.sh`、`pyrun_gpt2_124M.sh`、`multi_node/` | 复现训练脚本 + 多节点脚本 |
| `scripts/multi_node/` | `run_gpt2_124M_mpi.sh`、`run_gpt2_124M_fs.sbatch`、`run_gpt2_124M_tcp.sbatch` | 三种 NCCL 初始化方式（MPI / 共享文件系统 / TCP） |

**根目录可执行文件的「身份」对照**——这是本讲最实用的一张表：

| 文件 | 是哪一套实现 | 作用 |
| --- | --- | --- |
| `train_gpt2.c` | CPU 参考实现 | 纯 C + OpenMP，可读性最高的训练循环 |
| `train_gpt2.cu` | CUDA 主线实现 | 生产级、最快、支持混合精度/多卡/多节点 |
| `train_gpt2.py` | PyTorch 参考实现 | nanoGPT 风格，产出 `.bin` 权重 + 正确性基准 |
| `train_gpt2_fp32.cu` | fp32 legacy CUDA | 冻结的早期 CUDA 版，单卡 fp32，适合学 CUDA |
| `train_llama3.py` | PyTorch 扩展示例 | 展示如何把架构扩展到 Llama3 |
| `test_gpt2.c` | CPU 测试 | 对照 `debug_state.bin` 验证 CPU 前向/训练正确 |
| `test_gpt2.cu` | CUDA 测试 | 对照 PyTorch，覆盖 fp32 与混合精度两条路径 |
| `test_gpt2_fp32.cu` | fp32 legacy 测试 | 针对冻结版 CUDA 的正确性测试 |
| `profile_gpt2.cu` | 剖析入口 | 精简训练循环，配合 Nsight Compute 剖析单 kernel |
| `profile_gpt2cu.py` | 剖析脚本 | 解析剖析结果 |

**构建入口**——`Makefile` 把上面这些源文件编成同名的可执行程序（如 `train_gpt2.c` → `train_gpt2`，`train_gpt2_fp32.cu` → `train_gpt2fp32cu`），并自动探测 OpenMP / NCCL / MPI / cuDNN / 精度等环境变量。具体规则我们在下一讲（u1-l2）精读。

#### 4.3.4 代码实践

**实践目标**：用一次「目录漫游」把地图刻进脑子，而不是死记表格。

**操作步骤**：

1. 在仓库根目录执行一次目录浏览（只读），逐个确认下列子目录存在：`llmc/`、`dev/cuda/`、`dev/cpu/`、`dev/data/`、`doc/layernorm/`、`scripts/`。
2. 在 `dev/cuda/` 下找出至少 3 个「同一层」对应的文件（例如 `layernorm_forward.cu`、`attention_forward.cu`、`matmul_forward.cu`）。
3. 在 `scripts/` 下数一数共有几个 `run_gpt2_*.sh`（不同规模的复现脚本）。

**需要观察的现象**：

- `dev/cuda/` 里很多文件名就是 `<层>_<forward|backward>.cu`，与根目录 `train_gpt2.c` 里的函数名（如 `layernorm_forward`）遥相呼应。
- `scripts/` 同时有 GPT-2 系列（124M/350M/774M/1558M）和 GPT-3 系列（125M）的脚本。

**预期结果**：你能闭着眼说出「要找某层的手写多版本内核就去 `dev/cuda/<层>.cu`，要复现某规模训练就去 `scripts/run_<模型>_<规模>.sh`」。

> 说明：本实践的「运行」部分只是只读浏览目录，不修改任何源码；若你在本地执行 `ls` 类命令，结果应与上表一致。

#### 4.3.5 小练习与答案

**练习 1**：`dev/cuda/layernorm_forward.cu` 和 `llmc/layernorm.cuh` 都和 LayerNorm 有关，它们有什么不同定位？
**参考答案**：`llmc/layernorm.cuh` 是 **CUDA 主线实际使用的**生产级实现（被 `train_gpt2.cu` include，见 [train_gpt2.cu:49](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L49)）。`dev/cuda/layernorm_forward.cu` 则是**教学/实验用的内核库**——同一个前向会给出 kernel1、kernel2……多个由简到繁、逐步优化的版本，用于学习 CUDA 优化技巧与 benchmark。前者求「够用且简单」，后者求「展示优化演进」。

**练习 2**：如果你想多节点训练，应该去哪里找现成脚本？有几种 NCCL 初始化方式？
**参考答案**：去 `scripts/multi_node/`。共有三种 NCCL 初始化方式：MPI（`run_gpt2_124M_mpi.sh`）、共享文件系统（`run_gpt2_124M_fs.sbatch`）、TCP（`run_gpt2_124M_tcp.sbatch`）。README 明确说「没有哪一种更优，只是适配不同环境」（README 第 160-169 行）。

**练习 3**：为什么根目录同时保留 `train_gpt2.cu` 和 `train_gpt2_fp32.cu` 两个 CUDA 训练文件？
**参考答案**：`train_gpt2.cu` 是不断演进的主线（混合精度、cuDNN、多卡），代码较复杂；`train_gpt2_fp32.cu` 是「冻结在历史某一刻」的早期版本，只用 cuBLAS、单卡 fp32、自包含，更简单、更可移植。后者更适合「想学 CUDA 但不想一上来就面对主线复杂度」的读者（README quick start fp32 小节，[README.md:11-20](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/README.md#L11-L20)）。

---

## 5. 综合实践

把本讲三块内容串起来，完成下面这个「读懂 + 归档」的小任务。

**任务**：

1. **阅读**：通读 `README.md` 的开头到 `repo` 小节（约第 1–201 行）。
2. **写一段话**：用 100 字左右说明 CPU 版、CUDA 主线版、PyTorch 版**各自适合什么场景**。要求包含：谁负责产出权重、谁是正确性标尺、谁追求最快。
3. **列一张表**：列出根目录下**每个 `train_*` / `test_*` 文件**对应的实现及其作用（提示：共 8 个，参考 4.3.3 的对照表，但请用自己的话重写）。

**参考答案（完成后可对照）**：

- **场景段落（示例）**：`train_gpt2.py` 是 PyTorch 参考，负责下载/转换权重并写成 `.bin`，同时充当 C/CUDA 实现的正确性标尺；`train_gpt2.c` 是纯 C 的 CPU 参考，每一层都写得最清楚，适合学算法；`train_gpt2.cu` 是 CUDA 主线，调用 cuBLAS/cuDNN/NCCL，追求最快、支持混合精度与多卡多节点，适合真正跑训练。
- **文件归档表**：见 4.3.3 的「根目录可执行文件的身份对照」。8 个文件分别是 `train_gpt2.c`（CPU 参考）、`train_gpt2.cu`（CUDA 主线）、`train_gpt2.py`（PyTorch 参考）、`train_gpt2_fp32.cu`（fp32 legacy CUDA）、`train_llama3.py`（Llama3 扩展示例）、`test_gpt2.c`（CPU 测试）、`test_gpt2.cu`（CUDA 测试）、`test_gpt2_fp32.cu`（fp32 legacy 测试）。（注：`profile_gpt2.cu` / `profile_gpt2cu.py` 用于剖析，不属 `train_*`/`test_*`。）

> 这个任务不要求你跑训练，只要求你「读得准、归得对」。如果你能在不看本讲答案的情况下完成，本讲的目标就达成了。

## 6. 本讲小结

- llm.c 的定位是「**用简单纯粹的 C/CUDA 训练 GPT-2/GPT-3**」，同时追求**教育性（看得懂）**和**工程性（跑得快）**。
- 三套核心实现分工清晰：`train_gpt2.py`（PyTorch 参考，产出 `.bin` 权重与正确性基准）、`train_gpt2.c`（CPU 参考，最易懂）、`train_gpt2.cu`（CUDA 主线，最快、支持多卡多节点）。
- 三者通过 **`.bin` 权重 + `debug_state.bin` 标准答案 + 单元测试**形成正确性闭环（`overall okay: 1`）。
- 仓库按角色分目录：根目录保简单，`llmc/` 是 CUDA 主线头文件库，`dev/cuda` 是多版本内核教学库，`dev/data` 准备数据，`doc/` 是教程，`scripts/` 是复现脚本。
- 根目录每个 `train_*`/`test_*` 文件都对应一种明确角色；另有 `train_gpt2_fp32.cu` 作为「学 CUDA 的更简单冻结版」。
- 项目的核心取舍原则：**主线代码必须简单可读，复杂度和高性能实验放进 `dev/`**。

## 7. 下一步学习建议

- **下一讲（u1-l2）**：精读 `Makefile`，搞懂它如何自动探测 OpenMP/NCCL/MPI/cuDNN/精度，并跑通 CPU / GPU fp32 / 混合精度三条 quick start 路径。
- **再下一讲（u1-l3）**：进入 `train_gpt2.c` 的训练主循环，看懂「前向→清零梯度→反向→更新」四步调度。
- **如果想立刻动手**：执行 `./dev/download_starter_pack.sh` 下载 starter pack，然后 `make train_gpt2` 用 CPU 跑几步，亲眼看到 loss 下降——这会让你对本讲「复现 GPT-2 训练」的定位有最直观的感受。
- **延伸阅读**：想先看一个「单层」怎么从零实现，可以直接读 `doc/layernorm/layernorm.md`，它是仓库自带的最小教程。
