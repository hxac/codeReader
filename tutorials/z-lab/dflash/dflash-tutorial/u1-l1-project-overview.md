# DFlash 项目概览：块扩散投机解码是什么

## 1. 本讲目标

本讲是整本学习手册的第一篇，目标只有三个：

1. 理解**投机解码（speculative decoding）**的基本动机——用一个小而快的「草稿模型」起草、再用大而准的「目标模型」验证。
2. 理解**块扩散（block diffusion）**与传统的「逐 token 自回归起草」有什么本质区别，以及 DFlash 为什么用它来起草。
3. 通过 `README.md` 和 `pyproject.toml` 这两个文件，认识 DFlash **支持的模型清单**和**四种运行后端（Transformers / SGLang / vLLM / MLX）**，为后面动手运行打下基础。

读完本讲，你不必看懂任何算法细节，但你应该能向别人说清楚：**DFlash 是什么、为什么能加速、能加速哪些模型、有几种用法。**

## 2. 前置知识

本讲面向零基础读者，但有几个名词先铺垫一下，后面读起来会更顺：

- **LLM（大语言模型）生成**：模型一次输出一个 token（最小文本单元），下一个 token 依赖前面所有 token，所以是一步步「自回归」地往下写。
- **prefill（预填充）与 decode（解码）**：处理输入提示词的阶段叫 prefill，之后逐个吐出新 token 的阶段叫 decode。
- **KV cache（键值缓存）**：为了避免每生成一个 token 就把整段历史重新算一遍，模型会把每层注意力的 key/value 缓存下来，这就是 KV cache。
- **草稿模型（draft model）与目标模型（target model）**：前者小而快，负责「猜」接下来可能的 token；后者大而准，负责「判」这些猜测对不对。
- **token / batch / 后端（backend）**：token 见上；batch 是一次送进去处理多少条样本；后端在这里指「用哪套推理引擎来跑模型」。

如果你对上面的词还很陌生，没关系——本讲会结合 DFlash 的真实说明再讲一遍。

## 3. 本讲源码地图

本讲只涉及两个文件，它们是认识 DFlash 的「入口文档」：

| 文件 | 作用 |
|---|---|
| `README.md` | 项目的门面：一句话定位、支持的模型清单、安装方式、四种后端的快速启动示例、评测方式。 |
| `pyproject.toml` | Python 包定义：核心运行依赖、四个可选依赖分组（对应四种后端）、包发现规则。 |

> 提示：`dflash` 包本体只有四个源码文件（`__init__.py` / `model.py` / `model_mlx.py` / `benchmark.py`），它们的具体职责会在「u1-l3 包结构与模块导出」中讲，本讲先不展开。

## 4. 核心概念与源码讲解

本讲拆成 5 个最小模块：先建立投机解码与块扩散的直觉（4.1、4.2），再落到 DFlash 真实支持的模型（4.3）和四种后端（4.4），最后看依赖配置（4.5）。

### 4.1 投机解码的直觉：草稿 + 验证

#### 4.1.1 概念说明

先回答一个朴素问题：**为什么大模型生成慢？**

对大多数部署在 GPU 上的 LLM，decode 阶段每次只生成 1 个 token，而且这一步主要受**显存带宽**限制——每算一个 token，都要把整个模型权重从显存搬一遍。算力（FLOPS）其实大量闲置。这种现象叫**显存带宽受限（memory-bound）**。

投机解码的核心想法很聪明：

- 用一个**小而快的草稿模型**，先「猜」出接下来可能的若干个 token；
- 再让**大而准的目标模型**把这些猜测**一次性**拿去验证（forward 一次就能并行验证多个 token，因为 GPU 的算力本来就富余）；
- 目标模型接受了多少个、就一次性推进多少个 token。

于是，原本「一个一个吐」的串行过程，变成了「一把一把走」，只要草稿猜得够准，就能在不损失生成质量的前提下显著提速。

#### 4.1.2 核心流程

投机解码的一个循环大致如下（伪代码）：

```text
1. 草稿模型 起草 K 个候选 token：t1, t2, ..., tK
2. 目标模型 一次性 forward，得到这 K 个位置上「正确」的概率分布
3. 逐个比对：从 t1 开始，只要草稿 token 被目标模型认可就接受
   一旦遇到第一个不认可的 token，就停止，并用目标模型在该位置的分布采样一个新 token
4. 把接受（含新采样的 1 个）的 token 写入输出，更新 KV cache
5. 回到第 1 步，继续起草
```

关键在于第 2 步：**目标模型一次 forward 能并行验证 K 个候选**。这就是投机解码能省时间的根本——它把目标模型闲置的算力用起来了。

加速效果取决于「平均接受长度」，即每个循环里目标模型平均接受了几个草稿 token。直觉上，接受长度越大，平均推进的 token 数越多，加速比越高。

#### 4.1.3 源码精读

DFlash 的定位，就写在 README 的开头第一句：

[README.md:L1-L8](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/README.md#L1-L8)

> 这段说明 DFlash 是一个**为投机解码而设计的轻量级块扩散模型**，强调它能「高效且高质量地**并行起草**（parallel drafting）」。

注意三个关键词：

- **lightweight（轻量级）**：草稿模型本身很小、很快，否则起草成本会吃掉加速收益。
- **for speculative decoding（为投机解码而设计）**：它不是用来单独生成文本的，而是配合目标模型做加速的「副驾驶」。
- **parallel drafting（并行起草）**：起草方式是并行的——这正是下一节「块扩散」要讲的核心。

#### 4.1.4 代码实践

这是一个**源码阅读型实践**，目的是强化「草稿 + 验证」的直觉：

1. **实践目标**：把投机解码的循环用你自己的话复述一遍。
2. **操作步骤**：
   - 重读上面 4.1.2 的伪代码。
   - 在笔记本上画出「草稿模型」「目标模型」「输出 token」「KV cache」四个方框，把一个循环里的数据流连起来。
3. **需要观察的现象**：你会注意到「目标模型一次 forward 验证多个 token」是这个机制能省时间的关键节点。
4. **预期结果**：你能讲清楚——为什么草稿模型必须「小而快」、为什么目标模型「一次验证多个」反而能加速。

#### 4.1.5 小练习与答案

**练习 1**：如果草稿模型猜得完全不准（接受长度为 0），投机解码相比普通解码会变快还是变慢？为什么？

> **参考答案**：会**变慢一点点**。因为每轮还得额外花时间起草，而目标模型每次只接受了 1 个（即新采样的那个），相当于「白起草」。所以草稿模型必须有基本的准确率，投机解码才有正收益。

**练习 2**：投机解码会改变生成结果的质量吗？

> **参考答案**：不会（在正确实现下）。因为最终保留哪些 token 是由**目标模型的分布**决定的，草稿只是「提议」，质量基准始终是目标模型。

### 4.2 块扩散：一次起草一整块

#### 4.2.1 概念说明

4.1 讲的「草稿 + 验证」里，草稿模型**怎么起草**其实有多种做法。最经典的是**逐 token 自回归起草**：草稿模型也一个 token 一个 token 地往后写，写 K 个 token 要做 K 次串行的前向计算。这种起草方式本身又回到了「一个一个吐」的老问题。

**块扩散（block diffusion）**换了个思路：草稿模型**一次性**对一个「整块」位置进行去噪（denoising），把这些位置上的噪声/掩码 token 同时还原成有意义的 token——也就是说，**起草 K 个 token 可以在很少几次（甚至一次）前向里并行完成**。

打个比方：

- **逐 token 自回归起草**像「口述接力」——一个人说一个字，下一个人接一个字，串成一句话，必须按顺序。
- **块扩散起草**像「填空」——把一句话里所有空位同时摆出来，大家一起填，填完整句一起交卷。

这正是 README 里说的 **parallel drafting（并行起草）**。它让草稿模型本身也能充分利用 GPU 的并行算力，起草速度更快，从而让整个投机解码循环转得更勤、加速比更高。

#### 4.2.2 核心流程

块扩散起草的概念流程（伪代码，仅用于建立直觉，具体实现在后续讲义）：

```text
给定来自目标模型的上下文特征（context feature）：
1. 在草稿模型里准备一个长度为 block_size 的「块」，初始为噪声/掩码 token
2. 让这些位置彼此可见、并且都能看到上下文特征
3. 经过（少数几次）去噪步骤，整块位置同时被还原成候选 token
4. 把这 block_size 个候选 token 整块交给目标模型验证
```

和逐 token 起草相比，关键区别是：起草的 K 个 token **不是串行产生的，而是作为一个块并行还原出来的**。具体的「噪声 token」「上下文特征如何参与注意力」「去噪如何实现」等细节，会在 **u2-l3（DFlash 注意力与块扩散机制）** 里结合真实代码深入讲解，本讲只建立直觉。

#### 4.2.3 源码精读

回到 README 的同一句定位，但这次重点看「block diffusion」这个修饰语：

[README.md:L4](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/README.md#L4)

> "**DFlash** is a lightweight **block diffusion** model designed for speculative decoding. It enables efficient and high-quality parallel drafting."

这句话把「是什么（block diffusion 模型）」「为什么（为投机解码而设计）」「效果（高效高质量的并行起草）」三件事一次讲完。本篇的三个学习目标，基本就是对这句话的逐词展开。

> 说明：README 里还有一张架构图（`DFlash Architecture`）和一段演示视频链接 [README.md:L6-L8](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/README.md#L6-L8)，建议在浏览器打开 README 对照看，对建立直观印象很有帮助（本讲义是纯文本，不嵌入图片）。

#### 4.2.4 代码实践

1. **实践目标**：用一句话说清块扩散相对传统自回归起草的优势。
2. **操作步骤**：对比 4.2.2 的伪代码和 4.1.2 的循环，思考「起草 K 个 token 这一步」在两种方式下的串行/并行差异。
3. **需要观察的现象**：块扩散把「起草」这一步从「K 次串行」压缩为「少数几次并行」。
4. **预期结果**：你能写出类似「块扩散一次并行还原一整块 token，避免了逐 token 起草的串行开销，让草稿模型也快起来」这样一句话。

#### 4.2.5 小练习与答案

**练习 1**：为什么说「逐 token 起草」会拖累投机解码的整体加速？

> **参考答案**：因为草稿模型自己又退回了串行生成，起草 K 个 token 要 K 次前向，起草开销变大，每轮循环变慢，单位时间内能推进的 token 数变少。

**练习 2**：块扩散「填空」式的起草，会不会影响最终生成质量？

> **参考答案**：不会。无论草稿怎么产生，最终保留哪些 token 仍由目标模型的验证决定；块扩散只改变「草稿怎么来得快」，不改变质量基准。

### 4.3 支持的目标模型与草稿模型清单

#### 4.3.1 概念说明

DFlash 是「副驾驶」，它需要和一个具体的「主模型（目标模型）」配对使用。对每个目标模型，z-lab 都训练并发布了对应的 **DFlash 草稿模型**。

需要注意两个概念：

- **目标模型（target model）**：你想加速的那个大模型，比如 `Qwen/Qwen3.5-27B`。
- **DFlash 草稿模型（draft model）**：专门为这个目标模型训练的块扩散草稿，名字里通常带 `-DFlash`，比如 `z-lab/Qwen3.5-27B-DFlash`。

也就是说，**草稿模型是「一对一」为目标模型训练的**，不能随便拿一个草稿去配另一个目标模型。

#### 4.3.2 核心流程

DFlash 的使用模式可以概括为「选目标 → 选对应草稿 → 选后端 → 启动」：

```text
1. 从支持清单里选定一个目标模型（如 Qwen3.5-27B）
2. 找到它对应的 DFlash 草稿（如 z-lab/Qwen3.5-27B-DFlash）
3. 选一种后端（vLLM / SGLang / Transformers / MLX）
4. 用该后端的启动命令，把目标模型和草稿模型都配置进去
```

#### 4.3.3 源码精读

README 用一张表列出了所有支持的目标模型及其对应的 DFlash 草稿：

[README.md:L10-L38](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/README.md#L10-L38)

> 这张表左列是**目标模型**，右列是对应的 **DFlash 草稿模型**（带 Hugging Face 链接）。

表格要点（节选，便于你建立印象，完整清单以源码为准）：

| 目标模型（示例） | 对应 DFlash 草稿 |
|---|---|
| Qwen3.5-27B | z-lab/Qwen3.5-27B-DFlash |
| Qwen3-8B（non-thinking） | z-lab/Qwen3-8B-DFlash-b16 |
| Llama-3.1-8B-Instruct | z-lab/LLaMA3.1-8B-Instruct-DFlash-UltraChat |
| gemma-4-26B-A4B-it | z-lab/gemma-4-26B-A4B-it-DFlash |

表格末尾还有几行标注 `Coming soon`（如 DeepSeek-V4-Flash、GLM-5.1）[README.md:L34-L36](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/README.md#L34-L36)，表示这些模型的草稿还在准备中。

表后有一句重要说明 [README.md:L38](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/README.md#L38)：可以开 GitHub issue 请求支持新模型，而且训练 recipe 后续会开源，让你**能为任意 LLM 训练自己的 DFlash 草稿**。

#### 4.3.4 代码实践

1. **实践目标**：从清单里挑出「目标模型 + 对应草稿」的一对例子。
2. **操作步骤**：打开 [README.md:L10-L38](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/README.md#L10-L38)，挑一个你最熟悉或最感兴趣的模型族（比如 Qwen3 或 Llama）。
3. **需要观察的现象**：每个目标模型都精确对应一个名字里带 `-DFlash` 的草稿；草稿名通常 = `z-lab/<目标名>-DFlash`（少数会有后缀，如 `-b16`、`-UltraChat`）。
4. **预期结果**：你记录下一对例子，例如「目标 `Qwen/Qwen3-8B` ↔ 草稿 `z-lab/Qwen3-8B-DFlash-b16`」，并理解这是一一对应的。

#### 4.3.5 小练习与答案

**练习 1**：清单里某目标模型标注为 `Coming soon`，意味着什么？你现在能用 DFlash 加速它吗？

> **参考答案**：意味着对应草稿模型还没发布。目前不能直接用——但可以按 README 提示，等训练 recipe 开源后自己训练一个草稿，或开 issue 请求支持。

**练习 2**：能不能用 `z-lab/Qwen3.5-27B-DFlash` 去加速 `Qwen/Qwen3-8B`？为什么？

> **参考答案**：不能。草稿模型是为特定目标模型训练的（架构、层数、词表都要对齐），跨目标模型配对会导致上下文特征对不上，失去意义。

### 4.4 四种后端与 Quick Start 速览

#### 4.4.1 概念说明

DFlash 的同一套算法，可以通过**四种推理引擎（后端）**来使用，各有适用场景：

| 后端 | 适用场景 | 特点 |
|---|---|---|
| **Transformers** | 研究、调试、二次开发 | 用 HuggingFace Transformers 直接加载模型；只支持 Qwen3 和 LLaMA-3.1 系列 |
| **vLLM** | 生产级高吞吐服务 | vLLM v0.20.1+ 内置 DFlash 支持；适合 GPU 服务器 |
| **SGLang** | 生产级高吞吐服务 | 通过 SGLang 的 speculative algorithm 接入；适合 GPU 服务器 |
| **MLX** | Apple 芯片（Mac）本地 | 适合在 M 系列芯片上本地体验 |

三种「服务型」后端（vLLM / SGLang）的共同点是把 DFlash 配置成一个 **speculative（投机）参数**；而 Transformers / MLX 则更像「库」的调用方式，直接在 Python 里调函数。

#### 4.4.2 核心流程

四种后端的启动方式略有不同，但配置「目标 + 草稿」的核心参数是一致的。以 vLLM 为例，关键是一个 `--speculative-config` JSON：

```text
--speculative-config '{"method": "dflash", "model": "<草稿模型>", "num_speculative_tokens": <块大小>}'
```

- `method`：投机方法，这里固定为 `dflash`。
- `model`：DFlash 草稿模型的路径/名。
- `num_speculative_tokens`：一次起草多少个 token（即块大小）。

SGLang 则把同样的信息拆成几个 CLI 参数：`--speculative-algorithm DFLASH`、`--speculative-draft-model-path <草稿>`、`--speculative-num-draft-tokens <块大小>`。

Transformers / MLX 则直接在 Python 代码里传 `target=...`、`block_size=...` 这类参数（具体见 u1-l4）。

#### 4.4.3 源码精读

**安装**：README 的安装表概括了四种后端的安装命令 [README.md:L40-L49](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/README.md#L40-L49)。

> 注意 vLLM 较特殊：常规模型用 `uv pip install -e ".[vllm]"` 即可 [README.md:L51-L54](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/README.md#L51-L54)；而 Gemma4 DFlash 目前需要 z-lab 的临时 vLLM 构建，推荐用 Docker 镜像 [README.md:L56-L59](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/README.md#L56-L59)。

**vLLM 启动**（非 Gemma4 模型）：见 [README.md:L95-L101](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/README.md#L95-L101)。

> 这里用 `vllm serve Qwen/Qwen3.5-27B` 加 `--speculative-config` 把草稿模型接进去，`num_speculative_tokens: 15` 表示一次起草 15 个 token。

**SGLang 启动**：见 [README.md:L103-L124](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/README.md#L103-L124)。

> 这里 `--speculative-algorithm DFLASH` + `--speculative-draft-model-path` + `--speculative-num-draft-tokens 16` 就是「方法 + 草稿 + 块大小」三件套。

**Transformers 启动**：见 [README.md:L126-L142](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/README.md#L126-L142)。

> 代码里分别加载草稿（`AutoModel.from_pretrained(...)`）和目标模型（`AutoModelForCausalLM.from_pretrained(...)`），然后调用 `draft.spec_generate(input_ids=..., target=target, ...)`。README 也明确：**只有 Qwen3 和 LLaMA-3.1 模型支持 Transformers 后端** [README.md:L128](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/README.md#L128)。

**MLX 启动**：见 [README.md:L144-L161](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/README.md#L144-L161)。

> 用 `from dflash.model_mlx import load, load_draft, stream_generate`，在 Apple 芯片上以流式方式生成，`block_size=16` 即块大小。

#### 4.4.4 代码实践

1. **实践目标**：不看后续讲义，仅凭 README 判断「给定一个目标模型，该用哪种后端」。
2. **操作步骤**：打开 [README.md:L73-L161](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/README.md#L73-L161)，把四种后端的启动方式各自的核心参数抄下来。
3. **需要观察的现象**：你会看到 vLLM/SGLang 是「服务 + HTTP」模式，Transformers/MLX 是「Python 库调用」模式；四者都围绕「目标 + 草稿 + 块大小」三个信息。
4. **预期结果**：你能填出下表（示例答案见练习答案）：

| 后端 | 关键参数/形式 |
|---|---|
| vLLM | ? |
| SGLang | ? |
| Transformers | ? |
| MLX | ? |

> 注：本讲先不要求你真的启动服务——真正动手跑通在 **u1-l2（多后端安装与运行）** 和 **u1-l4（动手跑通第一次生成）**。

#### 4.4.5 小练习与答案

**练习 1**：填全上面那张「后端 ↔ 关键参数」表。

> **参考答案**：
> - vLLM：`--speculative-config '{"method":"dflash","model":...,"num_speculative_tokens":...}'`
> - SGLang：`--speculative-algorithm DFLASH --speculative-draft-model-path ... --speculative-num-draft-tokens ...`
> - Transformers：Python 里 `draft.spec_generate(input_ids=..., target=target, ...)`
> - MLX：Python 里 `stream_generate(model, draft, tokenizer, prompt, block_size=..., ...)`

**练习 2**：为什么 README 说 Transformers 后端「只支持 Qwen3 和 LLaMA-3.1」？

> **参考答案**：Transformers 后端是 DFlash 仓库自己提供的 Python 参考实现，它复用了这两个模型族的层结构/权重加载机制（具体在 u2-l5 讲）。其他模型族需要走 vLLM/SGLang 这些已有内置支持的服务后端。

### 4.5 核心依赖与可选依赖分组

#### 4.5.1 概念说明

`pyproject.toml` 是 Python 包的「身份证 + 配方」。它告诉我们：

- 这个包叫什么、版本、要求的 Python 版本；
- **核心依赖（dependencies）**：无论用哪种后端，都要装的库；
- **可选依赖分组（optional-dependencies）**：按后端分组的「扩展包」，用 `[后端名]` 的形式安装，避免把四种引擎全装进来互相冲突。

这种设计的好处是：**DFlash 把「通用部分」和「与具体后端绑定的重型依赖」分开**，你只需要为你实际用的那一种后端安装一组额外依赖，保持环境干净。

#### 4.5.2 核心流程

安装 DFlash 的通用模式是：

```text
uv pip install -e ".[<后端分组>]"
```

其中 `<后端分组>` 取自 `[project.optional-dependencies]` 里的键名：`transformers` / `sglang` / `vllm` / `mlx`。选不同的分组，就会把对应后端需要的引擎（torch、transformers、sglang、vllm、mlx 等）一并装进来。

#### 4.5.3 源码精读

包的基本信息和核心依赖：

[pyproject.toml:L1-L14](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/pyproject.toml#L1-L14)

> 这段说明包名 `dflash`、版本 `0.1.0`、要求 Python `>=3.10`；核心依赖是 `rich` / `loguru` / `numpy` / `tqdm` / `datasets` / `requests` / `huggingface-hub`。

注意：**核心依赖里没有 torch、vllm、mlx 这些重型引擎**——它们都被挪到了可选分组里。核心依赖大多是「工具型」库：`rich`/`loguru` 负责漂亮的终端输出和日志，`tqdm` 负责进度条，`datasets`/`huggingface-hub`/`requests` 负责下载数据和模型，`numpy` 是数值基础。

四个可选依赖分组：

[pyproject.toml:L19-L38](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/pyproject.toml#L19-L38)

> 这段把四种后端的依赖分别打包成 `transformers` / `sglang` / `vllm` / `mlx` 四组。

各分组要点：

- `transformers`：装 `torch`、`transformers==4.57.1`（**版本被钉死**，说明对 Transformers 版本敏感）、`accelerate` 等。
- `sglang`：直接从 GitHub 拉取一个特定分支的 `sglang[all]`（对应 README 里 SGLang 需要的特定支持）。
- `vllm`：装 `vllm`、`datasets>=3,<4`、`huggingface-hub<1`（带版本上界，避免不兼容）。
- `mlx`：钉死 `mlx==0.31.2`、`mlx-lm==0.31.3`（Apple 芯片的 MLX 框架）。

这也是 README 反复强调「**每个后端用独立的虚拟环境，避免冲突**」[README.md:L42](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/README.md#L42) 的原因——四个分组彼此可能不兼容（比如不同版本的 transformers、不同的 CUDA 栈）。

> 补充：包发现规则 `[tool.setuptools.packages.find]` 把 `dflash*` 包含进来 [pyproject.toml:L16-L17](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/pyproject.toml#L16-L17)，保证 `pip install` 后能 `import dflash`。这块在 u1-l3 会详讲。

#### 4.5.4 代码实践

1. **实践目标**：看懂「安装命令里的 `[xxx]`」和 `pyproject.toml` 里的分组是怎么对应的。
2. **操作步骤**：把 README 安装表 [README.md:L40-L49](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/README.md#L40-L49) 和 `pyproject.toml` 的可选依赖 [pyproject.toml:L19-L38](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/pyproject.toml#L19-L38) 并排对照。
3. **需要观察的现象**：`uv pip install -e ".[vllm]"` 里的 `vllm` 正好对应 `[project.optional-dependencies]` 下的 `vllm = [...]` 那一组。
4. **预期结果**：你能解释「为什么 `.[transformers]` 会同时装上 torch 和 transformers，而 `.[mlx]` 不会」——因为它们在不同的可选分组里。

> 注：这一步只做「阅读对照」，暂不真的安装。真正动手安装、并理解「不同后端用不同环境」的实操，在 **u1-l2**。

#### 4.5.5 小练习与答案

**练习 1**：为什么 `transformers` 分组要把 `transformers==4.57.1` 钉死成精确版本？

> **参考答案**：DFlash 的 Transformers 后端参考实现依赖 Transformers 的特定 API/层结构，版本漂移可能破坏权重加载或注意力实现，所以用精确版本锁住，保证可复现。

**练习 2**：如果不加任何 `[后端]`，只执行 `uv pip install -e .`，能跑起来 vLLM 服务吗？

> **参考答案**：不能。那只装了核心依赖（rich/loguru/numpy 等），没有 `vllm` 本身。必须用 `.[vllm]` 把对应分组一起装上。

## 5. 综合实践

把本讲的三个概念（投机解码、块扩散、模型清单）和两类配置（后端、依赖）串起来，完成下面这个小任务：

**任务**：你是一个新同学，想用 DFlash 加速一个模型。请产出一个简短的「选型笔记」（写在本地文本文件或笔记本里即可），内容包含：

1. **一对模型**：从 [README.md:L10-L38](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/README.md#L10-L38) 里挑一个目标模型及其对应的 DFlash 草稿模型，各列一个。
2. **一句话优势**：用你自己的话，解释「块扩散」相对「逐 token 自回归起草」的一个核心优势。
3. **一个候选后端**：结合你的设备（有没有 NVIDIA GPU、是不是 Apple 芯片的 Mac、是想本地调试还是起服务），选出你最想用的那一种后端，并写出它对应的安装命令（从 [README.md:L40-L49](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/README.md#L40-L49) 取），再说明这条命令会安装哪一组依赖（对应 [pyproject.toml:L19-L38](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/pyproject.toml#L19-L38)）。
4. **一个愿望清单**：记下「你最想用 DFlash 加速的模型」是哪一个；如果它还不在清单里（或标注 `Coming soon`），写下你打算怎么跟进（关注 issue / 等训练 recipe / 自己训练）。

**参考作答片段**（你可以照此格式，但模型/后端换成你自己真实的选择）：

```text
目标模型：Qwen/Qwen3-8B
草稿模型：z-lab/Qwen3-8B-DFlash-b16
块扩散优势：一次并行还原一整块 token，省去逐 token 起草的串行开销，让草稿也快。
后端选择：Transformers（我在带 GPU 的机器上做研究/调试）
安装命令：uv pip install -e ".[transformers]"
对应依赖分组：[project.optional-dependencies] transformers = [torch, transformers==4.57.1, ...]
最想加速的模型：Qwen3-Coder-30B-A3B（若可用）；若暂不可用则关注训练 recipe 开源。
```

> 完成后，这份笔记会直接帮你衔接 **u1-l2（多后端安装与运行）**——下一讲就会让你真的把所选后端装起来、跑起来。

## 6. 本讲小结

- **DFlash 的定位**：一个轻量级的**块扩散模型**，专门作为**投机解码的草稿模型**使用，用来高效并行起草。
- **投机解码的本质**：「小而快的草稿起草 + 大而准的目标验证」，把目标模型闲置的算力用起来，在不损失质量的前提下提速。
- **块扩散的优势**：相比逐 token 自回归起草，块扩散**一次性并行还原一整块 token**，让草稿模型也摆脱串行瓶颈。
- **模型清单**：每个目标模型都**一一对应**一个名字带 `-DFlash` 的草稿模型，部分模型标注 `Coming soon`；训练 recipe 后续会开源。
- **四种后端**：Transformers（仅 Qwen3/LLaMA-3.1，适合调试）、vLLM、SGLang（适合 GPU 生产服务）、MLX（Apple 芯片本地）；核心都是配置「目标 + 草稿 + 块大小」。
- **依赖结构**：`pyproject.toml` 把通用核心依赖与四种后端的可选依赖**分组**，安装时用 `.[后端]` 选用，建议每个后端用独立环境避免冲突。

## 7. 下一步学习建议

本讲只是「认识 DFlash」，接下来的路线建议：

1. **下一讲 u1-l2（多后端安装与运行）**：动手选一种后端，按 README 真的把 DFlash 服务或环境装起来、启动起来——这是从「读」到「跑」的跨越。
2. **u1-l3（包结构与模块导出）**：打开 `dflash/__init__.py`，看懂包体只有四个源码文件、以及懒加载导出机制，建立对代码整体结构的认知。
3. **u1-l4（动手跑通第一次生成）**：照 README 的 Transformers 或 MLX 示例，完整跑通一次加速生成，观察 `spec_generate` 的入口签名。
4. 进入**第二单元（进阶）** 后，再从 `dflash/model.py` 开始，深入阅读 DFlash 的核心算法实现。

如果你暂时没有合适的 GPU 或 Mac，也可以先跳过「跑」，直接进 u1-l3 读包结构——阅读型学习同样能跟上后续节奏。
