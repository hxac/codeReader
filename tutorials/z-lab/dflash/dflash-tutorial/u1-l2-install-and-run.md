# 多后端安装与运行

## 1. 本讲目标

学完本讲，你应该能够：

- 说出 DFlash 四种后端（Transformers / SGLang / vLLM / MLX）各自的**安装方式**与**适用场景**，并能根据机器环境（GPU / Apple 芯片 / 仅想调试）选择正确的后端。
- 读懂 `pyproject.toml` 的 `optional-dependencies` 分组，理解为什么 README 反复强调「每种后端要用独立虚拟环境安装」。
- 读懂 vLLM 的 `--speculative-config` 与 SGLang 的 `--speculative-algorithm` 等关键启动参数，知道 `method` / `model` / `num_speculative_tokens` 各自控制什么。
- 用 vLLM 或 SGLang 启动一次 DFlash 加速服务，并用 `curl` 发一条 chat 请求验证服务可用。

## 2. 前置知识

本讲承接 [u1-l1](u1-l1-project-overview.md)，你已经知道 DFlash 是「草稿模型（draft）+ 目标模型（target）」的投机解码方案。这里补充三个运行层面的概念：

- **推理后端（serving backend）**：把目标模型跑起来、接收请求、并借助草稿模型加速生成的「运行壳」。DFlash 不是自己写了一个推理引擎，而是把加速能力**集成进现有的四种后端**。
- **服务型 vs 库型**：vLLM / SGLang 是「起一个 HTTP 服务」，客户端用网络请求调用；Transformers / MLX 是「在 Python 里直接 import 调用」，没有网络层。这决定了它们的安装与启动方式完全不同。
- **可调的草稿规模**：草稿一次起草的 token 越多，目标一次前向可能「白捡」的 token 就越多，但起草太长也会让接受率下降。这个规模由参数 `num_speculative_tokens`（vLLM）或 `num_speculative_tokens`（SGLang）控制。

一个直觉性的加速公式（投机解码通用原理，非 DFlash 独有）：

若一次验证中目标接受了 \(a\) 个草稿 token，则这一步目标前向实际产出 \(a+1\) 个 token（多出的 1 个是目标自己补的「修正 token」）：

\[
\text{每步平均产出} = \mathbb{E}[a] + 1
\]

所以「让 \(\mathbb{E}[a]\) 尽量大」就是加速的关键，而 `num_speculative_tokens` 正是决定「最多能起草多少」的上限旋钮。

## 3. 本讲源码地图

本讲只涉及两个文件，但它们几乎承载了「安装 + 运行」的全部信息：

| 文件 | 作用 |
|---|---|
| [README.md](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/README.md) | 安装命令表、各后端 Quick Start 启动命令与可运行示例代码 |
| [pyproject.toml](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/pyproject.toml) | 核心依赖与四种后端的 `optional-dependencies` 分组 |

## 4. 核心概念与源码讲解

### 4.1 四种后端的定位与适用场景

#### 4.1.1 概念说明

DFlash 的加速逻辑（草稿起草 + 目标验证）需要被「嵌进」某个推理后端才能跑起来。README 给出四种后端，它们的定位差异很大：

| 后端 | 调用方式 | 适用场景 | 关键限制 |
|---|---|---|---|
| **Transformers** | Python 库内调用 | 学习算法、调试、读源码 | 仅支持 Qwen3 与 LLaMA-3.1 |
| **vLLM** | 起 HTTP 服务 | GPU 生产服务，OpenAI 兼容接口 | v0.20.1+ 才内置 DFlash |
| **SGLang** | 起 HTTP 服务 | GPU 生产服务 | 需社区贡献分支 |
| **MLX** | Python 库内调用 | Apple 芯片本地运行 | 仅 Apple Silicon |

> 注意：选择哪种后端主要看你的**硬件**和**用途**——要部署服务就选 vLLM/SGLang，要读算法源码就选 Transformers，在 Mac 上玩就选 MLX。

#### 4.1.2 核心流程

四种后端虽然形态不同，但「配置 DFlash」的思路是统一的，都需要三要素：

1. **目标模型**（target）：你要加速的那个大模型。
2. **草稿模型**（draft）：名字带 `-DFlash` 的小模型，与目标一一对应。
3. **草稿规模 / 块大小**：一次起草多少个 token（即 `num_speculative_tokens` 等）。

差别只在于「三要素写在哪里」：vLLM 写进一段 JSON、SGLang 写成几个 CLI flag、Transformers/MLX 写成函数参数。

#### 4.1.3 源码精读

README 在 Quick Start 开头就点明了各后端的边界。Transformers 后端的支持范围（仅两个模型家族）：

[README.md:L128-L128](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/README.md#L128) — 明确「Only Qwen3 and LLaMA-3.1 models support the Transformers backend」，说明 Transformers 后端是受限的参考实现，不是通用生产路径。

MLX 后端的测试范围与硬件说明：

[README.md:L146-L146](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/README.md#L146) — 标注「tested on an Apple M5 Pro with Qwen3, Qwen3.5 and Gemma-4 models」，告诉你 MLX 后端面向 Apple 芯片且已测过哪些模型。

#### 4.1.4 代码实践（源码阅读型）

1. **目标**：建立「后端 → 调用方式」的心智映射。
2. **操作**：打开 README 的 Quick Start 章节，对四个小节各找一条信息——它是用 CLI 起服务，还是 Python `import`？
3. **观察**：vLLM/SGLang 小节都是 shell 命令且绑定端口（`-p 8000` / `launch_server`）；Transformers/MLX 小节都是 `python` 代码块。
4. **预期结果**：你应能在不看答案的情况下，说出任意一个后端属于「服务型」还是「库型」。

#### 4.1.5 小练习与答案

**练习 1**：哪种后端是「库内调用」而不是「起服务」？

> **答案**：Transformers 和 MLX。它们直接在 Python 中 `import` 后调用函数，没有 HTTP 层；vLLM 和 SGLang 则需要先启动一个监听端口的服务。

**练习 2**：如果你的同事只想最快读懂 DFlash 的算法实现，你会推荐哪个后端？为什么？

> **答案**：Transformers。它纯 PyTorch 实现、仅支持两个模型家族，依赖最轻，最适合作为「参考实现」来读源码（这正是第二单元 u2 的主线）。

---

### 4.2 安装：用 optional-dependencies 分组隔离环境

#### 4.2.1 概念说明

四种后端的依赖会**互相打架**：vLLM 要特定版本的 `torch`，MLX 要 `mlx==0.31.2`（只能在 Apple 上装），SGLang 要从一个 git PR 拉代码，而 Transformers 锁死 `transformers==4.57.1`。如果都装进同一个环境，几乎必然冲突。

Python 的解法是 `optional-dependencies`（可选依赖分组）：把每个后端的依赖写成独立分组，安装时用 `.[分组名]` 指定只装哪一组。再用**独立虚拟环境**（venv）保证每组互不污染——这正是 README 第一句强调的事。

#### 4.2.2 核心流程

```text
创建虚拟环境 A → uv pip install -e ".[vllm]"      # 只装 vLLM 那组
创建虚拟环境 B → uv pip install -e ".[sglang]"    # 只装 SGLang 那组
创建虚拟环境 C → pip install -e ".[mlx]"          # 只装 MLX 那组
```

`-e` 表示「可编辑安装」（editable），把当前项目目录挂到环境里，方便改了 `dflash/` 源码立即生效。`.[vllm]` 的含义是：安装当前目录 `.` 的项目，并额外带上 `vllm` 这一组可选依赖。

#### 4.2.3 源码精读

先看核心依赖——**无论选哪个后端都会装**的基础包（日志、进度条、数据集等，主要服务于 benchmark 与公共逻辑）：

[pyproject.toml:L1-L14](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/pyproject.toml#L1-L14) — 含 `rich`/`loguru`/`numpy`/`tqdm`/`datasets`/`requests`/`huggingface-hub`，并要求 `requires-python = ">=3.10"`。还要注意 [pyproject.toml:L16-L17](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/pyproject.toml#L16-L17) 的 `packages.find` 用 `include = ["dflash*"]` 把 `dflash` 包纳入打包范围。

再看四个可选分组：

[pyproject.toml:L19-L38](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/pyproject.toml#L19-L38) — 四组 optional-dependencies，每组特点不同：

| 分组 | 关键依赖 | 特点 |
|---|---|---|
| `transformers` | `transformers==4.57.1`、`torch`、`accelerate` | 锁死 transformers 版本 |
| `sglang` | `sglang[all] @ git+...@refs/pull/23000/head` | 从特定 git PR 拉取 |
| `vllm` | `vllm`（**未锁版本**）、`datasets>=3,<4`、`huggingface-hub<1` | 不锁 vllm 版本 |
| `mlx` | `mlx==0.31.2`、`mlx-lm==0.31.3` | 精确锁版本，仅 Apple 可装 |

> **一个值得注意的细节**：`vllm` 分组里并没有写版本号，但 README 明确要求「vLLM v0.20.1+」。也就是说，版本下限是写在 README 文档里、而不是 `pyproject.toml` 里的。安装时如果 `pip` 解析到旧版 vLLM，DFlash 支持就不会被激活——这是踩坑高发点。

对应 README 的安装命令表：

[README.md:L44-L49](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/README.md#L44-L49) — Transformers / SGLang / MLX 各对应一条 `.[分组]` 命令；vLLM 那一栏写的是「See below」，因为它有多个分支情况。

vLLM 的特殊情况（这是本讲最容易混淆的地方）：

- [README.md:L51-L54](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/README.md#L51-L54) — **大多数模型**：vLLM v0.20.1+ 已内置 DFlash，标准安装 `.[vllm]` 即可。
- [README.md:L56-L59](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/README.md#L56-L59) — **Gemma4** DFlash：暂时需要专用的 vLLM Gemma4 构建，推荐用 Docker 镜像 `ghcr.io/z-lab/vllm-openai:gemma4-dflash-cu130`。
- [README.md:L61-L65](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/README.md#L61-L65) — Gemma4 的**源码兜底**：从 vLLM 的 PR `41703` 拉取构建。
- [README.md:L67-L71](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/README.md#L67-L71) — **较新的非 Gemma4、带 SWA（滑动窗口）的草稿模型**：从 PR `40898` 的 SWA 支持分支拉取。

#### 4.2.4 代码实践（源码阅读型）

1. **目标**：把「文档要求」与「打包声明」对齐，发现差异。
2. **操作**：打开 `pyproject.toml`，回答：`.[vllm]` 到底会装哪些包？然后打开 README，回答：vLLM 的版本下限是多少？
3. **观察**：你会发现版本下限只在 README，不在 `pyproject.toml`。
4. **预期结果**：理解为什么「按 `.[vllm]` 装完仍可能跑不起来」——需要再确认实际装上的 vLLM 版本 ≥ 0.20.1。

#### 4.2.5 小练习与答案

**练习 1**：四个分组中，哪一组从 git PR 拉取依赖？哪一组精确锁死了版本？

> **答案**：`sglang` 从 git PR（`refs/pull/23000/head`）拉取 `sglang[all]`；`mlx` 精确锁死版本（`mlx==0.31.2`、`mlx-lm==0.31.3`）。

**练习 2**：为什么 README 说「Use a separate virtual environment for each」？

> **答案**：因为四个后端的依赖（torch 版本、transformers 版本、vllm、mlx、sglang）彼此冲突，装进同一环境会互相覆盖。`optional-dependencies` 只是让你「按需装某一组」，真正隔离还要靠独立虚拟环境。

---

### 4.3 vLLM 后端启动与 speculative-config

#### 4.3.1 概念说明

vLLM 是 GPU 生产服务的首选后端。它通过一个 JSON 字符串参数 `--speculative-config` 把 DFlash 三要素（method / model / num_speculative_tokens）一次性传进去。

#### 4.3.2 核心流程

```text
vllm serve <目标模型> \
  --speculative-config '{"method": "dflash", "model": "<草稿模型>", "num_speculative_tokens": N}' \
  --attention-backend <后端> \
  --max-num-batched-tokens 32768
```

#### 4.3.3 源码精读

非 Gemma4 模型的标准启动命令：

[README.md:L97-L101](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/README.md#L97-L101) — 解析 `--speculative-config` 里的 JSON：

| JSON 字段 | 示例值 | 含义 |
|---|---|---|
| `method` | `"dflash"` | 指定用 DFlash 作为投机解码方法（vLLM 还支持其它 method，这里固定 dflash） |
| `model` | `"z-lab/Qwen3.5-27B-DFlash"` | 草稿模型的路径 / 仓库名 |
| `num_speculative_tokens` | `15` | 草稿一次最多起草多少个 token 供目标验证 |

另外两个 flag 也很关键：`--attention-backend flash_attn` 指定目标模型的注意力后端；`--max-num-batched-tokens 32768` 给批处理留足显存预算（DFlash 一次验证一整块 token，比普通解码吃更多 batch token）。

Gemma4 模型则更复杂，需要 Docker + 特殊注意力后端：

[README.md:L79-L93](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/README.md#L79-L93) — 注意它的 `--speculative-config` 里**还多了一个 `"attention_backend": "flash_attn"`** 字段（控制草稿自身的注意力后端），并在外面又用 `--attention-backend triton_attn` 控制目标后端，还加了 `--trust-remote-code`。这是 Gemma4 特有的组合。

> **小提示**：本讲不要求你跑 Gemma4。初学时优先用「非 Gemma4」那条命令，依赖最少、最容易成功。

#### 4.3.4 代码实践（源码阅读型）

1. **目标**：把 vLLM 的 JSON 参数逐字段拆懂。
2. **操作**：对照 [README.md:L97-L101](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/README.md#L97-L101)，在笔记里把 `method` / `model` / `num_speculative_tokens` 各写一句中文解释。
3. **思考**：如果把 `num_speculative_tokens` 从 15 调到 30，根据第 2 节的加速公式 \(\mathbb{E}[a]+1\)，加速比一定上升吗？
4. **预期结论**：不一定。起草更多 token 可能让接受率 \(\mathbb{E}[a]/\text{num\_spec}\) 下降，需要实测权衡。（具体数值待本地验证）

#### 4.3.5 小练习与答案

**练习 1**：`--speculative-config` 里的 `model` 字段填的是目标模型还是草稿模型？

> **答案**：草稿模型。命令行第一个位置参数（如 `Qwen/Qwen3.5-27B`）才是目标模型；JSON 里的 `model` 是与之配对的、带 `-DFlash` 后缀的草稿模型。

**练习 2**：为什么 DFlash 场景下 `--max-num-batched-tokens` 要设得比较大（如 32768）？

> **答案**：因为块扩散一次起草并验证一整块 token，单步处理的 token 数远多于普通逐 token 解码，需要更大的 batch token 预算才不会卡住。

---

### 4.4 SGLang 后端启动与 speculative-algorithm

#### 4.4.1 概念说明

SGLang 是另一个 GPU 生产服务后端，DFlash 的支持由社区贡献。与 vLLM 把配置塞进一段 JSON 不同，SGLang 用**一组独立的 CLI flag** 来描述同一组信息。

#### 4.4.2 核心流程

```text
export SGLANG_ALLOW_OVERWRITE_LONGER_CONTEXT_LEN=1
python -m sglang.launch_server \
    --model-path <目标模型> \
    --speculative-algorithm DFLASH \
    --speculative-draft-model-path <草稿模型> \
    --speculative-num-draft-tokens N \
    ...
```

#### 4.4.3 源码精读

SGLang 的完整启动命令：

[README.md:L106-L124](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/README.md#L106-L124) — 把它和 vLLM 的 JSON 做个映射对比：

| 含义 | vLLM（JSON 字段） | SGLang（CLI flag） |
|---|---|---|
| 用 DFlash 方法 | `method: "dflash"` | `--speculative-algorithm DFLASH` |
| 草稿模型 | `model: "..."` | `--speculative-draft-model-path "..."` |
| 草稿规模 | `num_speculative_tokens: 15` | `--speculative-num-draft-tokens 16` |

> **注意两个差异**：
> 1. **参数命名不同**：vLLM 叫 `num_speculative_tokens`，SGLang 叫 `num-draft-tokens`。
> 2. **示例数值不同**：README 里 vLLM 用 `15`、SGLang 用 `16`。两个框架对「起草 token 数」的计数约定略有差别，精确语义以各自文档为准；本讲只需理解**两者都表示「一次起草/验证的草稿规模」**。

SGLang 命令里还有一些值得了解的 flag：

- 环境变量 `SGLANG_ALLOW_OVERWRITE_LONGER_CONTEXT_LEN=1`（[L106](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/README.md#L106)）：允许覆盖更长的上下文长度上限。
- `--tp-size 1`：张量并行规模（多卡时可调大）。
- `--attention-backend trtllm_mha` 与 `--speculative-draft-attention-backend fa4`：分别控制**目标**与**草稿**的注意力后端——SGLang 把这两者拆成了两个独立 flag（vLLM 的 Gemma4 命令也有类似拆分）。
- 三行被注释掉的 `SGLANG_ENABLE_*`（[L109-L111](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/README.md#L109-L111)）：实验性的调度重叠开关，README 注明「may not be stable」，初学时保持注释即可。

#### 4.4.4 代码实践（源码阅读型）

1. **目标**：能在两套参数体系间互译。
2. **操作**：把 [README.md:L97-L101](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/README.md#L97-L101)（vLLM）和 [README.md:L113-L123](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/README.md#L113-L123)（SGLang）并排，手写一张「vLLM 字段 ↔ SGLang flag」对照表。
3. **观察**：两者的「目标注意力后端」分别由哪个参数控制？
4. **预期结果**：vLLM 用 `--attention-backend flash_attn`；SGLang 用 `--attention-backend trtllm_mha`，并额外用 `--speculative-draft-attention-backend fa4` 单独控制草稿。

#### 4.4.5 小练习与答案

**练习 1**：要把 SGLang 的草稿规模从 16 改成 32，应改哪个 flag？

> **答案**：`--speculative-num-draft-tokens 32`。

**练习 2**：SGLang 命令里 `--speculative-draft-model-path` 和 `--model-path` 分别填什么？

> **答案**：`--model-path` 填目标模型（如 `Qwen/Qwen3.5-35B-A3B`），`--speculative-draft-model-path` 填与之配对的草稿模型（如 `z-lab/Qwen3.5-35B-A3B-DFlash`）。

---

### 4.5 Transformers 与 MLX：库内调用后端

#### 4.5.1 概念说明

Transformers 和 MLX 都不起服务，而是在 Python 里直接调用。它们的「DFlash 配置」不是 CLI 参数，而是**函数调用参数**。这两个后端的入口函数会在后续讲义（[u1-l4](u1-l4-first-generation.md)、u2、u3）深入，本讲只让你认识它们的「长相」，建立完整地图。

#### 4.5.2 核心流程

```text
Transformers:  draft = AutoModel.from_pretrained(<草稿>)
               target = AutoModelForCausalLM.from_pretrained(<目标>)
               draft.spec_generate(input_ids, target=target, ...)

MLX:           model, tokenizer = load(<目标>)
               draft = load_draft(<草稿>)
               stream_generate(model, draft, tokenizer, prompt, block_size=..., ...)
```

#### 4.5.3 源码精读

Transformers 后端示例（注意它的入口是 `draft.spec_generate`，把 target 当参数传进去）：

[README.md:L130-L141](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/README.md#L130-L141) — `spec_generate` 的关键参数：`input_ids`（输入）、`max_new_tokens`（生成长度）、`temperature`（采样温度）、`target`（目标模型）、`stop_token_ids`（停止条件）。这里的「草稿规模」由模型配置里的 `block_size` 决定，而不是函数参数（详见 u2-l2）。

MLX 后端示例（入口是 `stream_generate`，流式产出）：

[README.md:L149-L160](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/README.md#L149-L160) — 与 Transformers 不同，MLX 的 `stream_generate` 把 `block_size=16` 直接作为函数参数传入，并用 `for r in ...` 逐块流式拿到文本与吞吐（`r.generation_tps`）。

> **对照点**：同样是「块大小」，Transformers 写在模型 config 里，MLX 写在调用参数里——这反映了两个后端实现风格的不同（u3 会详细讲 MLX）。

#### 4.5.4 代码实践（源码阅读型）

1. **目标**：对比两种库型后端的入口函数签名。
2. **操作**：分别读 [README.md:L140](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/README.md#L140) 和 [README.md:L157](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/README.md#L157)，列出两个入口函数各自接收哪些参数。
3. **观察**：`spec_generate` 的 target 是位置在哪？`stream_generate` 的 block_size 在哪？
4. **预期结果**：能说出 `spec_generate(input_ids, max_new_tokens, temperature, target, stop_token_ids)` 与 `stream_generate(model, draft, tokenizer, prompt, block_size, max_tokens, temperature)` 的参数差异。

#### 4.5.5 小练习与答案

**练习 1**：Transformers 后端为什么不支持所有模型？

> **答案**：因为它是一个「参考实现」，只针对 Qwen3 与 LLaMA-3.1 两个模型家族做了适配（见 [README.md:L128](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/README.md#L128)）。要跑其它模型应改用 vLLM / SGLang / MLX。

**练习 2**：MLX 的 `stream_generate` 返回什么？为什么用 `for` 循环接收？

> **答案**：它返回一个流式迭代器，每次产出一个含 `.text` 和 `.generation_tps` 的响应对象。用 `for` 是为了**边生成边打印**（流式），而不是等全部生成完再一次性返回。

---

## 5. 综合实践

现在把知识串起来：用 **vLLM**（或 SGLang）启动一个 DFlash 加速服务，再用 `curl` 验证它能正常生成。这是本讲的主实践。

> **前提**：你需要一块可用 GPU，并已按 4.2 节在独立虚拟环境里装好 `.[vllm]`，且确认 vLLM 版本 ≥ 0.20.1。若没有 GPU，可把本实践当作「命令演练」，把每一步的预期现象记下来。

### 步骤 1：安装（独立虚拟环境）

```bash
# 建议先建独立 venv
uv pip install -e ".[vllm]"
# 安装后确认版本（关键，README 要求 v0.20.1+）
python -c "import vllm; print(vllm.__version__)"
```

### 步骤 2：启动 DFlash 加速服务（非 Gemma4 模型）

```bash
vllm serve Qwen/Qwen3.5-27B \
  --speculative-config '{"method": "dflash", "model": "z-lab/Qwen3.5-27B-DFlash", "num_speculative_tokens": 15}' \
  --attention-backend flash_attn \
  --max-num-batched-tokens 32768
```

- **观察**：启动日志里应出现加载目标模型 `Qwen3.5-27B` 与草稿模型 `Qwen3.5-27B-DFlash` 的过程，最后监听在 `http://127.0.0.1:8000`。
- **预期**：服务进入 `Uvicorn running on ...` 的就绪状态。（具体日志待本地验证）

### 步骤 3：用 curl 发一条 chat 请求

vLLM 启动后会暴露一个 **OpenAI 兼容**的 `/v1/chat/completions` 接口，因此可以用标准的 OpenAI 风格请求调用（`model` 字段要与服务里的目标模型名一致）：

```bash
curl http://127.0.0.1:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "Qwen/Qwen3.5-27B",
    "messages": [{"role": "user", "content": "How many positive whole-number divisors does 196 have?"}],
    "max_tokens": 256
  }'
```

- **预期**：返回一个 JSON，`choices[0].message.content` 里是模型的回答。（这是 vLLM 标准 OpenAI 兼容行为；DFlash 在服务端透明加速，对客户端接口无影响。具体返回待本地验证）

### 步骤 4（进阶）：对比有无 DFlash

把启动命令里的 `--speculative-config` 整段去掉重起服务，再发同一条 curl，对比两次的吞吐/延迟。这就是 DFlash 加速效果的最直观验证。（数值待本地验证）

> **若用 SGLang 替代**：把步骤 2 换成 4.4 节的 `python -m sglang.launch_server ...`（默认端口 `30000`），curl 里的 URL 改成 `http://127.0.0.1:30000`，路径和请求体同样遵循该后端的 OpenAI 兼容接口。

## 6. 本讲小结

- DFlash 通过**集成进现有后端**运行：vLLM / SGLang 是服务型（HTTP），Transformers / MLX 是库型（Python import）。
- 四种后端的依赖互相冲突，必须用 `pyproject.toml` 的 `optional-dependencies` 分组 + **独立虚拟环境**隔离安装。
- vLLM 把 DFlash 配置塞进 `--speculative-config` JSON（`method` / `model` / `num_speculative_tokens`）；SGLang 用一组独立 flag（`--speculative-algorithm DFLASH` / `--speculative-draft-model-path` / `--speculative-num-draft-tokens`）。两者是同一信息的两种写法。
- vLLM 安装有多个分支：大多数模型用标准 `.[vllm]`（需 v0.20.1+），Gemma4 需专用 Docker 构建，带 SWA 的较新草稿模型需 SWA 支持分支。
- 库型后端把「草稿规模」写在函数/config 里：Transformers 的 `block_size` 在模型 config，MLX 的 `block_size` 在 `stream_generate` 参数。
- 客户端侧（如 curl）用的是后端的 OpenAI 兼容接口，DFlash 加速在服务端透明完成，不改变调用方式。

## 7. 下一步学习建议

- 想搞清楚 `import dflash` 时到底加载了什么？继续学 [u1-l3 包结构与模块导出](u1-l3-package-structure.md)，看 `__init__.py` 的懒加载机制。
- 想亲手在 Python 里跑通一次生成（库型后端）？继续学 [u1-l4 动手跑通第一次生成](u1-l4-first-generation.md)。
- 想深入 DFlash 的算法实现（草稿如何起草、目标如何验证）？进入第二单元 [u2-l1 投机解码全局视图与生成控制流](u2-l1-spec-decoding-control-flow.md)，从 Transformers 参考实现的 `model.py` 读起。
