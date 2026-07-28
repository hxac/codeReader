# 模型导出流程

## 1. 本讲目标

本讲解决一个「反向」问题：`assets/<model>/` 目录里那一堆 `.ncnn.param`、`.ncnn.bin`、`vocab.txt`、`merges.txt`、`model.json` 究竟从哪里来？

学完本讲，你应当能够：

- 画出从 HuggingFace（PyTorch）模型到 ncnn 可加载产物的整体管线图，并指出哪些步骤由本仓库脚本完成、哪些步骤依赖外部工具。
- 读懂 `export/` 下四个 Python 脚本各自的职责：`extract_tokenizer.py`、`hunyuan_ocr_tokenizer.py`、`hunyuan_ocr_add_kvcache.py`、`nllb_export.py`。
- 理解贯穿四个脚本的两条「隐式契约」：分词器的「行号 == token id」，以及 KV cache 的「`cache_k%d`/`cache_v%d` 入、`out_cache_k%d`/`out_cache_v%d` 出」槽位命名。
- 把导出产物的文件名/字段，对应回 C++ 运行时在 `model.json` 与 `.param` 里读取的内容。

> 重要提醒：本仓库的 README 把「完善模型导出管线文档」列为 Roadmap 待办项，并明确写道「**Older export scripts may become outdated as the runtime evolves.**（导出脚本可能随运行时演进而过时）」。因此本讲涉及的脚本细节以仓库 `f2f29e4` 版本为准，涉及 PyTorch→ncnn 的转换工具（`pnnx`）等外部环节，一律标注「待确认」。详见 [readme.md:292-294](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/readme.md#L292-L294)。

## 2. 前置知识

在进入导出脚本前，先用通俗语言回顾几个基础概念。本讲依赖 **u1-l5（model.json 配置体系）**，请确保你已经知道：

- **ncnn 的两种产物文件**。ncnn 不读 `.safetensors` 也不读 `.onnx`，它读自己原生的两类文件：`.ncnn.param`（网络结构，纯文本可读）和 `.ncnn.bin`（权重，二进制）。运行时靠 `load_param`/`load_model` 成对加载。详见 [u1-l5 讲义](./u1-l5-model-json-config.md)。
- **HuggingFace（HF）模型的常见组成**。一个 HF 模型仓库通常含：权重（`.safetensors`/`.bin`，PyTorch 张量）、`config.json`（结构超参）、`tokenizer.json` + `tokenizer_config.json`（分词器）。导出，本质上是把 PyTorch 的权重与结构翻译成 ncnn 的 `.param`/`.bin`，把 HF 的分词器翻译成 `vocab.txt`/`merges.txt`。
- **TorchScript 与 `pnnx`**。PyTorch 模型可用 `torch.jit.trace`/`script` 序列化成 TorchScript `.pt` 文件（一种「可独立运行、不再依赖模型定义代码」的中间表示）。ncnn 官方提供了 **`pnnx`（PyTorch Neural Network Exchange）** 工具，能把 TorchScript `.pt` 转成 ncnn 的 `.param`/`.bin`。`pnnx` 是 ncnn 上游的外部工具，**不在本仓库内**，这是本讲「待确认」环节的主要来源。
- **KV cache 槽位契约**（来自 u2-l2）。共享文本运行时 `llm_run_decoder_with_kv` 按层号读写 KV cache，输入槽位叫 `cache_k%d`/`cache_v%d`，输出槽位叫 `out_cache_k%d`/`out_cache_v%d`。导出后的 `.param` 必须长成这个样子，运行时才能驱动它做增量解码。
- **行号 == token id**（来自 u3-l2）。`BpeTokenizer` 用 `vocab.txt` 的行号当 token id。所以导出分词器时，「词表第几行」就是「id 等于几」，排序不能乱。

一句话总括：导出 = **权重翻译（PyTorch→ncnn）+ 分词器翻译（HF→行号文本）+ 结构对齐（让它满足运行时的槽位契约）+ 配置落地（写 model.json）**。

## 3. 本讲源码地图

本讲只涉及 `export/` 目录下的四个脚本，以及它们产物在 C++ 侧的「消费方」。

| 文件 | 语言 | 职责 | 产物 |
|------|------|------|------|
| `export/extract_tokenizer.py` | Python（仅标准库） | 从 HF `tokenizer.json` 抽取词表与合并表，并为嵌入模型生成一份 `model.json` | `vocab.txt`、`merges.txt`、`model.json` |
| `export/hunyuan_ocr_tokenizer.py` | Python（仅标准库） | 导出 HunyuanOCR 的「基础词表 + 818 个特殊令牌」全量词表 | `vocab.txt`（120818 行）、`merges.txt` |
| `export/hunyuan_ocr_add_kvcache.py` | Python（仅标准库） | 改写 HunyuanOCR 解码器 `.param`，为 SDPA 算子注入 KV cache 槽位 | 改写后的 `.ncnn.param`（原地覆盖，留 `.nokv` 备份） |
| `export/nllb_export.py` | Python（依赖 `torch` + `transformers`） | 把 NLLB 模型导出成三个 TorchScript 模块，供 `pnnx` 进一步转 ncnn | `embed.pt`、`encoder_noembed.pt`、`decoder_noembed.pt` |

C++ 侧的消费方（用来反推契约，不属于 `export/`）：

| 文件 | 作用 |
|------|------|
| `src/ncnn_text_runtime.cpp` | 读写 KV cache 槽位 `cache_k%d`/`out_cache_k%d`，是 KV 注入脚本要对齐的目标 |
| `src/nllb_600m.cpp` | NLLB 运行时，加载 `embed`/`encoder_noembed`/`decoder_noembed` 三套 ncnn 子网，并在 C++ 里补算正弦位置编码 |
| `examples/nllb_main.cpp` | 指明 NLLB 期望的文件名（`embed.ncnn.param` 等），是 `.pt`→`.ncnn.*` 转换后命名的依据 |

> 注意：`export/` 下**只有这四个脚本**，没有别的导出工具。多数模型（Qwen3、MiniCPM 等）的权重转换并不在本仓库完成——README 指引从镜像下载已转换好的模型目录（[readme.md:90-103](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/readme.md#L90-L103)）。本仓库只提供了 NLLB（权重）与 HunyuanOCR（分词器 + KV 注入）这两条「自托管」的导出链路，外加一个通用的嵌入模型分词器抽取脚本。

---

## 4. 核心概念与源码讲解

### 4.1 导出全景：从 HuggingFace 到 assets 目录

#### 4.1.1 概念说明

「导出（export）」是一个把**训练框架里的模型**翻译成**推理运行时能加载的产物**的过程。ncnn_llm 的运行时只认三类东西：

1. ncnn 的 `.param`/`.bin`（网络结构与权重）；
2. 纯文本的 `vocab.txt`/`merges.txt`（分词器）；
3. `model.json`（把前面两者与各种 `setting` 串起来的清单）。

而模型的源头通常是 HuggingFace 上的 PyTorch 模型。于是导出天然分成三条互不相干的支线：

- **权重支线**：PyTorch 张量 → ncnn `.param`/`.bin`。这条路最重，依赖外部工具。
- **分词器支线**：HF `tokenizer.json` → `vocab.txt`/`merges.txt`。这条路是纯文本处理，本仓库脚本可独立完成。
- **结构后处理支线**：对某些模型（如 HunyuanOCR），原始导出的 `.param` 不带 KV cache，需要再改写一次，让它满足共享运行时的槽位契约。

理解导出的关键，是分清「哪一步在仓库内、哪一步在仓库外」，否则容易把 `nllb_export.py` 产出的 `.pt` 误当成运行时直接能用的文件。

#### 4.1.2 核心流程

下图把三条支线汇成一张完整的导出管线（带 ★ 的是本仓库脚本，带 ☆ 的是外部环节）：

```text
                HuggingFace 模型仓库
        ( .safetensors / tokenizer.json / config.json )
                        │
        ┌───────────────┼───────────────────────┐
        ▼               ▼                       ▼
   【权重支线】      【分词器支线】          【结构后处理】
   ★ nllb_export.py  ★ extract_tokenizer.py   ★ hunyuan_ocr_add_kvcache.py
   → *.pt            → vocab.txt + merges.txt  → 改写 *.param 注入 KV cache
        │            ★ hunyuan_ocr_tokenizer.py
        │                 → vocab.txt(120818)        │
        ☆ pnnx (外部)                                    │
        → *.ncnn.param / *.ncnn.bin                     │
        │                │                              │
        └───────────────►├◄─────────────────────────────┘
                         ▼
              汇总到 assets/<model>/
              + 手写或脚本生成 model.json
                         │
                         ▼
              C++ 运行时按 model.json 加载
```

几个要点：

- **权重支线最少自托管**。`nllb_export.py` 只走到 TorchScript `.pt` 这一步；从 `.pt` 到 `.ncnn.param`/`.bin` 依赖 ncnn 上游的 `pnnx`（**待确认**：仓库未提供该工具与具体调用命令）。其他模型家族的权重转换完全在仓库外完成。
- **分词器支线完全自托管**，且无需 PyTorch。两个脚本都只用 Python 标准库（`json`/`os`/`sys`/`glob`）。
- **结构后处理只对需要的模型做**。HunyuanOCR 的解码器原始导出是「每步跑全序列」的 SDPA，需要 `hunyuan_ocr_add_kvcache.py` 改写成「带 KV cache 的增量」版本，才能被共享运行时驱动。

#### 4.1.3 源码精读

先看仓库对导出管线的「官方表态」——README 的 Roadmap 与免责声明：

[readme.md:286-294](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/readme.md#L286-L294) —— Roadmap 里把「Document model export pipelines in more detail（完善导出管线文档）」列为待办；紧接着的免责声明明确：导出脚本可能随运行时演进而过时，应以最新的模型示例与 `model.json` 为准。这就是本讲多处标注「待确认」的根源。

再看产物目录的约定形态（与 u1-l5 一致）：

[readme.md:96-103](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/readme.md#L96-L103) —— 模型目录放在 `assets/<model>/` 下，内含 `model.json`、若干 `*.ncnn.param`/`*.ncnn.bin` 和分词器文件。导出的最终目标就是把一个这样的目录凑齐。

最后看 `export/` 到底有哪些脚本（确认没有遗漏）：

```bash
# 在仓库根目录执行
ls export/
# extract_tokenizer.py  hunyuan_ocr_add_kvcache.py
# hunyuan_ocr_tokenizer.py  nllb_export.py
```

只有四个文件，与「源码地图」一致。

#### 4.1.4 代码实践

1. **实践目标**：用 `git` 与目录列举，确认 `export/` 的真实内容，并对照本节的管线图标注每个脚本属于哪条支线。
2. **操作步骤**：
   - 在仓库根目录执行 `git ls-files export/`，列出被版本控制的导出脚本。
   - 用 `wc -l export/*.py` 查看每个脚本的规模（`nllb_export.py` 最大，因为它内含完整的导出 + 贪心解码示例）。
   - 对照 4.1.2 的管线图，在笔记里把四个脚本分别归入「权重 / 分词器 / 结构后处理」三条支线。
3. **需要观察的现象**：`git ls-files export/` 只输出四个文件名；`nllb_export.py` 行数远多于另外三个。
4. **预期结果**：四脚本归类为——`nllb_export.py`（权重支线）、`extract_tokenizer.py` + `hunyuan_ocr_tokenizer.py`（分词器支线）、`hunyuan_ocr_add_kvcache.py`（结构后处理支线）。
5. **待本地验证**：无（纯列举命令）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `extract_tokenizer.py` 只用 Python 标准库，而 `nllb_export.py` 必须依赖 `torch` 与 `transformers`？

**参考答案**：两者处理的「原料」不同。`extract_tokenizer.py` 的原料是结构化的 `tokenizer.json`（已经是 JSON 文本），只需 `json` 模块解析再重新排版成 `vocab.txt`/`merges.txt` 即可；而 `nllb_export.py` 的原料是 PyTorch 模型对象，需要 `transformers` 加载权重、用 `torch.jit.trace` 把 `nn.Module` 序列化成 TorchScript，因此必须依赖 `torch`。

**练习 2**：假设你要接入一个全新的 LLM 家族，本仓库的四个导出脚本能直接复用吗？

**参考答案**：不能直接复用。`nllb_export.py` 是 NLLB 专用（encoder-decoder、含 cross-attention），`hunyuan_ocr_*` 是 HunyuanOCR 专用。新 LLM 的权重转换需自行用 `pnnx` 或社区工具完成（**待确认**）；分词器若也是 BPE/BBPE，可参考 `extract_tokenizer.py` 的「行号 == id」逻辑改写，但不能直接套用其硬编码的嵌入模型 `model.json` 模板。

---

### 4.2 分词器导出：行号 == token id 的不变量

#### 4.2.1 概念说明

分词器导出解决的问题是：HF 的 `tokenizer.json` 把词表存成「`{token: id}` 字典」，而 ncnn_llm 的 `BpeTokenizer`（见 u3-l2）把词表存成「每行一个 token」的纯文本 `vocab.txt`，**用行号当 id**。导出脚本的核心职责，就是把前者无损翻译成后者，并保证「第 N 行的 token，其 id 恰为 N」。

本仓库有两个分词器导出脚本，差异在于「是否包含特殊令牌」：

- `extract_tokenizer.py`：通用，只抽基础词表，并顺手为嵌入模型生成一份 `model.json`。
- `hunyuan_ocr_tokenizer.py`：HunyuanOCR 专用，把「基础词表 + `added_tokens_decoder` 里的 818 个特殊令牌」合并成一张连续的、覆盖全部 120818 个 id 的词表。

为什么 HunyuanOCR 要单独写一个？因为它的 `lm_head`/`embedding` 是按**全量词表**（含特殊令牌）训练的，id 必须与全量词表的行号一一对应；而 `extract_tokenizer.py` 只导出基础词表，特殊令牌留给 `model.json` 的 `additional_special_tokens` 字段单独处理（见 u3-l1）。

#### 4.2.2 核心流程

**`extract_tokenizer.py` 的流程**：

1. 读 `tokenizer.json`，取 `model.vocab`（`{token: id}`）与 `model.merges`。
2. 把 vocab 按 id 升序排序，逐行写入 `vocab.txt`（保证行号 == id）。
3. 把 merges 逐对（`a b`）写入 `merges.txt`（行序 == 合并 rank，见 u3-l2）。
4. 读 `tokenizer_config.json`，抽取 `bos/eos/unk/...` 等特殊令牌字符串。
5. 生成一份硬编码给嵌入模型的 `model.json`（`model_type: embedding`，tokenizer `type: bpe`，含 `setting.embed_dim/rope_head_dim/rope_theta`）。

**`hunyuan_ocr_tokenizer.py` 的流程**（多了「合并特殊令牌」与「补空洞」两步）：

1. 在 HF 缓存里找 HunyuanOCR 的 snapshot 目录。
2. 读 `tokenizer.json` 的 `model.vocab` 与 `tokenizer_config.json` 的 `added_tokens_decoder`（`{id_str: {content}}`）。
3. 计算词表大小 `size = max(id) + 1`（跨基础词表与 added tokens 取最大 id）。
4. 建一个长度为 `size` 的数组 `id_to_token`，基础词表与 added tokens 各自按 id 填入。
5. **补空洞**：若某些 id 没有对应 token（数组里是 `None`），用合成占位符 `<|unused_{i}|>` 填上，保证「行号 == id」不出现错位。
6. 逐行写入 `vocab.txt`（共 `size` 行，即 120818 行）；merges 写入 `merges.txt`。

#### 4.2.3 源码精读

先看通用脚本如何保证「行号 == id」——靠「按 id 升序排序」：

[export/extract_tokenizer.py:13-25](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/export/extract_tokenizer.py#L13-L25) —— 用 `sorted(vocab.items(), key=lambda x: x[1])` 按 id（`x[1]`）升序排序后逐行写入。这样第 0 行就是 id=0 的 token，第 N 行就是 id=N 的 token。merges 直接按原列表顺序写入（HF 的 merges 列表本身已按 rank 排好）。

特殊令牌的抽取（注意它读的是 `tokenizer_config.json`，不是 `tokenizer.json`）：

[export/extract_tokenizer.py:27-36](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/export/extract_tokenizer.py#L27-L36) —— 遍历 7 个常见特殊令牌键（`bos_token`/`eos_token`/`unk_token`/...），存在即记录。这些字符串稍后会写进生成的 `model.json` 的 `tokenizer.bos/eos/...` 字段，由 C++ 构造函数解析成 id（见 u3-l1）。

脚本生成的 `model.json` 模板（注意它是**硬编码给嵌入模型**的）：

[export/extract_tokenizer.py:38-65](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/export/extract_tokenizer.py#L38-L65) —— 生成的 JSON 里 `model_type` 写死 `"embedding"`、`params` 指向 `jina_v5_nano_text_matching.ncnn.param/bin`、`setting.embed_dim=768`。这说明该脚本是「为 Jina 嵌入模型量身定做」的，换模型时这部分需要手改。它产出的 `tokenizer.type: "bpe"` 会驱动 C++ 选 `BpeTokenizer`（u3-l1）。

再看 HunyuanOCR 专用脚本如何合并特殊令牌并补空洞：

[export/hunyuan_ocr_tokenizer.py:36-53](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/export/hunyuan_ocr_tokenizer.py#L36-L53) —— 先用 `size = max_id + 1` 开数组，再把基础 vocab 与 `added_tokens_decoder` 各按 id 填进 `id_to_token[idx]`；随后扫描 `None` 位置，用 `<|unused_{i}|>` 占位符填满。这一步是关键：HF 词表里 id 不一定连续，若不补空洞，`vocab.txt` 的行号就会与 id 错位，导致整张词表错乱。注释（49-52 行）点明了「keep line<->id alignment」的目的。

脚本如何找到 HF 模型（直接扫缓存目录，不依赖 `transformers`）：

[export/hunyuan_ocr_tokenizer.py:14-19](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/export/hunyuan_ocr_tokenizer.py#L14-L19) —— 用 `glob` 在 `~/.cache/huggingface/hub/models--tencent--HunyuanOCR/snapshots/*` 下找 snapshot。因此运行前提是「已经用 `huggingface-cli download` 之类的工具把模型下到本地缓存」。

#### 4.2.4 代码实践

1. **实践目标**：亲手跑通最简单的 `extract_tokenizer.py`（仅用标准库，无需 PyTorch、无需下载大模型），观察它产出的 `vocab.txt`/`merges.txt`/`model.json`，验证「行号 == id」。
2. **操作步骤**：
   - 在临时目录手写一个最小 `tokenizer.json`：
     ```json
     {"model": {"vocab": {"hello": 0, "world": 1, "foo": 3, "bar": 2},
                "merges": [["he","llo"],["wor","ld"]]}}
     ```
     再手写一个最小 `tokenizer_config.json`：`{"eos_token": "<|end|>", "bos_token": "<|start|>"}`。
   - 执行 `python export/extract_tokenizer.py tokenizer.json ./out_tok tokenizer_config.json`。
   - 用 `cat -n out_tok/vocab.txt` 查看每行行号与内容。
3. **需要观察的现象**：`vocab.txt` 的顺序按 id 升序排列，即 `hello(0) / world(1) / bar(2) / foo(3)`，与输入字典里「foo=3, bar=2」的乱序不同；`model.json` 里 `tokenizer.eos` 为 `"<|end|>"`。
4. **预期结果**：`vocab.txt` 共 4 行，第 3 行（行号从 0 起算即第 4 行）是 `foo`；`merges.txt` 两行；`model.json` 是合法 JSON 且 `model_type` 为 `"embedding"`。
5. **待本地验证**：若你的环境没有 Python，可改为纯阅读型实践——对照 4.2.3 的源码链接，在脑中模拟「sorted by id」后 `vocab.txt` 的逐行内容。

#### 4.2.5 小练习与答案

**练习 1**：若把 `extract_tokenizer.py` 第 15 行的 `sorted(..., key=lambda x: x[1])` 改成「不排序、直接遍历 `vocab.items()`」，会发生什么？

**参考答案**：Python 字典按插入顺序遍历，id 不一定升序。这样 `vocab.txt` 的行号就不再等于 token id，`BpeTokenizer` 会把「行 N」当作「id N」查表，导致 encode/decode 全盘错位、输出乱码。这正是排序这一步存在的意义。

**练习 2**：`hunyuan_ocr_tokenizer.py` 为什么要用 `<|unused_{i}|>` 填补 id 空洞，而不是直接跳过？

**参考答案**：因为「行号 == id」要求 `vocab.txt` 第 i 行对应 id=i。若某个 id 没有对应 token 还跳过它，后面的 token 就会整体上移一行，从此行号与 id 永久错位。用占位符填上，既保住对齐，又因为这个 id 本不会被模型用到而不影响正确性。

**练习 3**：`extract_tokenizer.py` 生成的 `model.json` 能直接给 Qwen3 这类 LLM 用吗？

**参考答案**：不能。它的 `model_type` 硬编码为 `"embedding"`、`params` 指向 `jina_v5_nano_text_matching`、`setting` 是嵌入模型的维度。它只适合嵌入模型。LLM 的 `model.json` 要按 u1-l5 的 `params`（`embed_token_*`/`decoder_*`/`proj_out_*`）与 `setting`（`attn_cnt`/`rope`）手写，不能套用此模板。

---

### 4.3 KV cache 注入：让 .param 满足共享运行时契约

#### 4.3.1 概念说明

这是本讲最精巧的一步。问题背景来自 u2-l2：共享文本运行时 `llm_run_decoder_with_kv` 期望解码器 `.param` 的每一层注意力算子都暴露 KV cache 槽位——输入叫 `cache_k{i}`/`cache_v{i}`，输出叫 `out_cache_k{i}`/`out_cache_v{i}`，靠这些槽位在 prefill/decode 之间传递历史 K/V。

但 HunyuanOCR 从 PyTorch 直接导出的解码器 `.param` 并不长这样：它的 SDPA（Scaled Dot-Product Attention）算子是「每步跑全序列」的版本——4 个输入（q、k、v、mask）、1 个输出（out），没有 cache 槽位。直接交给运行时会失败。

好在 ncnn 的 `SDPA` 算子**原生支持** KV cache：只要把算子改写成 6 输入（q、k、v、mask、cache_k、cache_v）、3 输出（out、out_cache_k、out_cache_v），并打开 `kvcache_enabled` 参数位，同一个算子就能做增量推理。`hunyuan_ocr_add_kvcache.py` 就是做这件事的「`.param` 文本后处理器」——它把每一条 SDPA 行改写成带 cache 的签名，再补一个 `Input kv_cache` 层把所有 `cache_k{i}`/`cache_v{i}` 暴露成网络输入，最后修正文件头的层数/张量数计数。

脚本注释明说，这套改写是「照抄」GLM-OCR 已有的做法（`glm_ocr_text_decoder.ncnn.param` 本就是带 cache 的），让 HunyuanOCR 对齐到同一套契约。

#### 4.3.2 核心流程

`hunyuan_ocr_add_kvcache.py` 处理的是 ncnn `.param` 纯文本文件，流程是纯字符串改写：

1. **校验与备份**：读文件，确认首行是 ncnn param 的魔数 `7767517`；首次运行时把原文备份成 `<param>.nokv`。
2. **解析头部**：第二行是 `<层数> <张量数>`，记录原始计数。
3. **定位插入点**：在 body 里找到 `Input ... in3` 这一行（即第 4 个网络输入），KV cache 输入层要插在它后面。
4. **逐条改写 SDPA**：对每一条 `SDPA` 行——
   - 解析出原 4 输入（q/k/v/mask）与 1 输出（out）；
   - 追加两个新输入 `cache_k{i}`/`cache_v{i}` 与两个新输出 `out_cache_k{i}`/`out_cache_v{i}`（`i` 是该 SDPA 的层序号）；
   - 强制设三个参数：`5=1`（has_mask）、`6=1/sqrt(head_dim)`（scale）、`7=1`（kvcache_enabled）；
   - 重排成新的 SDPA 行。
5. **插入 KV cache 输入层**：新增一行 `Input kv_cache 0 <2N> cache_k0 cache_v0 cache_k1 cache_v1 ...`，把所有 cache 张量声明为网络输入。
6. **重算头部计数**：层数 +1（新增的 Input 层），张量数增加 `2N`（N 个 cache_k + N 个 cache_v），写回第二行。

其中缩放系数为：

\[
\text{scale} = \frac{1}{\sqrt{d_{\text{head}}}}, \quad d_{\text{head}} = 128 \;\Rightarrow\; \text{scale} \approx 0.0883883
\]

#### 4.3.3 源码精读

脚本顶部的文档注释把整件事的来龙去脉讲得很清楚，是理解本模块的最佳入口：

[export/hunyuan_ocr_add_kvcache.py:1-13](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/export/hunyuan_ocr_add_kvcache.py#L1-L13) —— 注释说明：原始导出的 SDPA 是 4 入 1 出（每步全序列），而 ncnn 的 SDPA 原生支持 6 入 3 出的 KV cache 模式（参数位 `5=has_mask`、`6=scale`、`7=kvcache_enabled`），正是 `glm_ocr_text_decoder.ncnn.param` 用的签名；本脚本把每条 SDPA 改写成该签名，加一个产出所有 `cache_k{i}`/`cache_v{i}` 的 `Input kv_cache` 层，并修正头部计数。

默认目标路径与缩放常数：

[export/hunyuan_ocr_add_kvcache.py:16-19](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/export/hunyuan_ocr_add_kvcache.py#L16-L19) —— 默认改写 `assets/hunyuan_ocr/hunyuan_ocr_text_decoder.ncnn.param`；`HEAD_DIM=128`，`SCALE` 用 `%g` 格式化成 `0.0883883`。

读文件、校验魔数、备份：

[export/hunyuan_ocr_add_kvcache.py:22-33](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/export/hunyuan_ocr_add_kvcache.py#L22-L33) —— 首行必须是 `7767517`（ncnn param 文件版本魔数），否则报错退出；首次运行写一份 `.nokv` 备份，避免原始无 cache 版本丢失。

SDPA 行的核心改写（本模块的心脏）：

[export/hunyuan_ocr_add_kvcache.py:57-94](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/export/hunyuan_ocr_add_kvcache.py#L57-L94) —— 对每条 SDPA：拆出原 4 输入（q/k/v/mask）与 1 输出（out），追加 `cache_k{sdpa_i}`/`cache_v{sdpa_i}` 作输入、`out_cache_k{sdpa_i}`/`out_cache_v{sdpa_i}` 作输出；强制 `pd["5"]="1"`、`pd["6"]=SCALE`、`pd["7"]="1"`；随后构造一行新的 `Input kv_cache` 层（91-92 行），产出所有 cache blob，插在 `in3` 之后。

改写完成后重算头部并写回：

[export/hunyuan_ocr_add_kvcache.py:96-104](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/export/hunyuan_ocr_add_kvcache.py#L96-L104) —— 重新统计非空层数与张量数（每个 Input/算子行的第 4 字段是它的输出张量个数，累加即总张量数），写回第二行头部。

最后，对照 C++ 侧的消费方，确认槽位命名一致（这是整条导出链路的「对账」环节）：

[src/ncnn_text_runtime.cpp:49-69](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_text_runtime.cpp#L49-L69) —— 运行时在 decode 阶段（`is_prefill=false`）按 `cache_k%d`/`cache_v%d` 喂入旧 cache，按 `out_cache_k%d`/`out_cache_v%d` 取出新 cache；prefill 阶段则不喂 cache、用 `emplace_back` 追加。这套 `%d` 按层号递增的命名，正是 `hunyuan_ocr_add_kvcache.py` 里 `cache_k{sdpa_i}` 产出的东西。两端命名一致，导出的 HunyuanOCR 解码器才能被共享运行时驱动——这就是 u2-l2 所述「跨模型族共享 decoder + KV cache 运行时」在设计层面的契约落点。

#### 4.3.4 代码实践

1. **实践目标**：通过纸面跟踪一条 SDPA 行的改写，理解「4入1出 → 6入3出」具体长什么样，并对照 C++ 确认命名契合。
2. **操作步骤**：
   - 阅读 [export/hunyuan_ocr_add_kvcache.py:57-83](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/export/hunyuan_ocr_add_kvcache.py#L57-L83)。
   - 假设改写前某条 SDPA 行（简化）为：
     `SDPA  attn_0  4 1  q0 k0 v0 mask0  out0  5=0 6=0.0883883`
     其中 `4 1` 表示 4 输入 1 输出。请按脚本逻辑写出改写后的行。
   - 再写出新增的 `Input kv_cache` 行（假设共 2 层 SDPA）。
3. **需要观察的现象**：改写后该行变成 6 输入 3 输出；参数位 `5` 被强制为 `1`；新增的 cache blob 名称里带层序号。
4. **预期结果**：
   - 改写后：`SDPA attn_0 6 3 q0 k0 v0 mask0 cache_k0 cache_v0 out0 out_cache_k0 out_cache_v0 5=1 6=0.0883883 7=1`
   - 新增输入层：`Input kv_cache 0 4 cache_k0 cache_v0 cache_k1 cache_v1`（2 层 × 2 = 4 个 cache blob）。
5. **待本地验证**：若有现成的 `assets/hunyuan_ocr/hunyuan_ocr_text_decoder.ncnn.param.nokv`（即原始无 cache 备份），可对比 `.nokv` 与改写后文件的 SDPA 行差异，验证上述手算。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `hunyuan_ocr_add_kvcache.py` 必须在「`pnnx` 转换之后」、而不是之前运行？

**参考答案**：它改写的是 ncnn 的 `.param` 文本，而 `pnnx` 的输入是 TorchScript `.pt`、输出才是 `.param`/`.bin`。所以顺序必须是：先 `nllb_export.py`/导出工具产出 `.pt` → `pnnx` 转成 `.param`/`.bin` → 再用本脚本改写 `.param` 注入 KV cache。颠倒顺序没有 `.param` 可改。

**练习 2**：脚本里 `pd["5"]="1"`、`pd["7"]="1"` 分别对应 SDPA 的什么参数？为什么 `6`（scale）要用 `1/sqrt(128)`？

**参考答案**：据脚本注释，`5=has_mask`（`1` 表示启用注意力掩码）、`7=kvcache_enabled`（`1` 表示开启 KV cache 模式）、`6=scale` 是注意力的缩放系数。注意力分数为 \(q\cdot k / \sqrt{d_{\text{head}}}\)，`head_dim=128`，故 scale \(=1/\sqrt{128}\approx0.0883883\)。

**练习 3**：改写后头部「张量数」为什么会增加 `2N`（N 为层数）？

**参考答案**：每层新增了 `cache_k{i}` 与 `cache_v{i}` 两个由 `Input kv_cache` 层产出的张量，共 N 层 → 新增 `2N` 个网络级张量；而每层新增的 `out_cache_k{i}`/`out_cache_v{i}` 是 SDPA 自己的输出张量，也计入总数。头部「张量数 = 全部算子输出 blob 的去重总数」，所以会相应上升。脚本正是靠累加每行输出个数来重算这个值的（见 96-99 行）。

---

### 4.4 TorchScript 权重导出：nllb_export.py 与产物对齐

#### 4.4.1 概念说明

`nllb_export.py` 是四个脚本里唯一处理「权重」的，也是唯一依赖 PyTorch 的。它的职责是：把 HuggingFace 上的 NLLB-200 翻译模型拆成三个 TorchScript 模块并保存，供后续 `pnnx` 转成 ncnn。

它体现了三条精心设计的「切分」原则：

- **token embedding 与 encoder/decoder 分离**。`embed.pt` 只做 token 查表（+可选缩放），单独成块；encoder/decoder 不含 embedding。
- **正弦位置编码不导出**。NLLB 用正弦（sinusoidal）绝对位置编码，脚本把它留在 Python/C++ 侧「相加」进 embedding，而不打进 ncnn 图。运行时 `nllb_600m.cpp` 里有对应的 `sinusoidal_positional_embedding` C++ 实现，两端算法必须一致。
- **lm_head 并入 decoder**。decoder 模块末尾自带 `lm_head`，直接产出 logits，省得再单独导一个投影子网。

> **待确认**：脚本只产出 `.pt`（TorchScript）。从 `.pt` 到 `embed.ncnn.param/bin`、`encoder_noembed.ncnn.param/bin`、`decoder_noembed.ncnn.param/bin` 的转换，依赖 ncnn 上游的 `pnnx` 工具，仓库未提供该工具与调用命令。但「文件名基底完全对应」这一事实，可由 C++ 侧 `nllb_main.cpp` 的期望文件名反推确认。

#### 4.4.2 核心流程

`nllb_export.py` 的 `export_torchscript` 函数流程：

1. 用 `AutoModelForSeq2SeqLM.from_pretrained` 加载 NLLB 模型，取其 `encoder`/`decoder` 与 `lm_head`。
2. 构造三个 TS 友好的包装模块：
   - `EmbeddingWithScale`：带显式缩放的 token embedding（拷贝 HF 权重）。
   - `EncoderNoEmbedTS`：encoder 各层 + layer_norm，输入已是 embedding。
   - `DecoderNoEmbedTS`：decoder 各层 + layer_norm + lm_head，输入是 embedding + encoder 输出 + self-attn mask。
3. 用 `torch.jit.trace` 各自追踪并保存成 `embed.pt`/`encoder_noembed.pt`/`decoder_noembed.pt`。
4.（脚本还附带了 `scripted_greedy_decode` 函数，仅用导出的三个模块做贪心解码，用于在 Python 侧验证导出正确性，不属于 ncnn 运行时链路。）

正弦位置编码的数学定义（与 C++ 侧一致，位置从 1 起算）：

\[
\text{PE}(pos, 2k) = \sin\!\left(\frac{pos}{10000^{2k/d}}\right), \quad
\text{PE}(pos, 2k+1) = \cos\!\left(\frac{pos}{10000^{2k/d}}\right)
\]

#### 4.4.3 源码精读

脚本顶部的文档注释列出了三个导出模块与若干设计决策：

[export/nllb_export.py:3-17](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/export/nllb_export.py#L3-L17) —— 明确：token embedding 与位置编码分离导出；**学习到的**位置编码可导出，**正弦**位置编码不导出（留 Python 处理）；decoder 以目标语言 token 起始；导出 `embed.pt`、`encoder_noembed.pt`、`decoder_noembed.pt` 三个模块。

带显式缩放的 token embedding 包装：

[export/nllb_export.py:32-41](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/export/nllb_export.py#L32-L41) —— `EmbeddingWithScale` 用 `F.embedding` 查表后乘以一个 buffer `_scale`。把缩放显式化是为了让 TorchScript 能稳定追踪（避免动态属性）。`export_torchscript` 里会把 HF embedding 的权重 `copy_` 进来（见 179-187 行）。

encoder/decoder 的「无 embedding」包装：

[export/nllb_export.py:117-130](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/export/nllb_export.py#L117-L130) —— `EncoderNoEmbedTS.forward` 直接接收 `hidden_states`（已是 embedding），逐层跑 encoder layer，末尾过 layer_norm。decoder 包装同理但额外接 `encoder_hidden_states`（cross-attention）与 `self_attn_mask`，并在末尾接 `lm_head` 出 logits（见 136-160 行）。

正弦位置编码的 Python 实现（不导出，仅用于验证解码）：

[export/nllb_export.py:72-94](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/export/nllb_export.py#L72-L94) —— `SinusoidalPositionalEmbeddingTS` 用 `inv = exp(arange * (-log(10000)/half_dim))` 算频率，再 `[sin, cos]` 拼接。注释（70-71 行）明确「This module is used directly in Python, NOT exported」。这段代码是 C++ 侧 `sinusoidal_positional_embedding` 的「参照实现」。

**产物对齐**（导出的关键验收点）。C++ 运行时期望的文件名：

[examples/nllb_main.cpp:73-78](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/examples/nllb_main.cpp#L73-L78) —— 期望 `embed.ncnn.param/bin`、`encoder_noembed.ncnn.param/bin`、`decoder_noembed.ncnn.param/bin`。基底名（`embed`、`encoder_noembed`、`decoder_noembed`）与 `nllb_export.py` 产出的三个 `.pt` 基底名**逐一对应**，印证了「`.pt` 经 `pnnx` 转 `.ncnn.*`」的中间环节（**待确认**具体 `pnnx` 命令）。

C++ 侧如何「补」上未导出的正弦位置编码：

[src/nllb_600m.cpp:163-183](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/nllb_600m.cpp#L163-L183) —— `embedding_forward` 先用 `embed_net_` 查出 token embedding（`out0`），再调 `sinusoidal_positional_embedding(...)` 算位置编码，`add_mats_inplace` 相加后返回。`pos==-1` 走全序列版本，否则走单位置版本（对应自回归解码的每一步）。这与导出脚本「正弦不导出」的决策正是一对：导出端不画进图，运行时端在 C++ 里算。

#### 4.4.4 代码实践

1. **实践目标**：在不安装 PyTorch 的前提下，用「源码阅读 + 产物名对账」理清 NLLB 的导出链路与「正弦不导出」的切分。
2. **操作步骤**：
   - 阅读 [export/nllb_export.py:3-17](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/export/nllb_export.py#L3-L17) 的设计说明。
   - 对照 [examples/nllb_main.cpp:73-78](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/examples/nllb_main.cpp#L73-L78)，在笔记里画一张「`.pt`（脚本产物）→ `pnnx`（外部）→ `.ncnn.param/bin`（运行时产物）」的命名对应表。
   - 对照 [src/nllb_600m.cpp:163-183](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/nllb_600m.cpp#L163-L183)，标注「token embedding 在 ncnn 图里」「正弦位置编码在 C++ 里」的分工。
3. **需要观察的现象**：三个 `.pt` 的基底名与三个 `.ncnn.*` 的基底名完全一致；C++ 里 `embedding_forward` 同时调用了 embed 子网与 `sinusoidal_positional_embedding`。
4. **预期结果**：对应表为 `embed.pt→embed.ncnn.*`、`encoder_noembed.pt→encoder_noembed.ncnn.*`、`decoder_noembed.pt→decoder_noembed.ncnn.*`；结论「正弦位置编码不在 ncnn 图、由 C++ 补算」成立。
5. **待本地验证**：若你装好了 `torch`+`transformers` 并能联网下载 `facebook/nllb-200-distilled-600M`，可运行 `python export/nllb_export.py`，会在 `./export_ts/` 下得到三个 `.pt`，并打印一行 `[Scripted] => <翻译结果>` 用以验证导出正确性。注意：这只是到 `.pt` 为止，转 ncnn 仍需 `pnnx`（**待确认**）。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `nllb_export.py` 把 `lm_head` 并进 `decoder_noembed.pt`，而不是像 LLM 那样单独导一个 `proj_out` 子网？

**参考答案**：这是 NLLB 导出的设计选择。NLLB 的 decoder 与 lm_head 紧耦合，把它们捆在一起追踪成一个 TorchScript 模块，可以减少 `pnnx` 转换后的子网数量与运行时来回。这与 ncnn_llm 的 LLM 运行时（`proj_out_net` 单独成网、见 u2-l3）是两套不同的切分约定，反映了 encoder-decoder 翻译模型与 decoder-only LLM 在结构上的差异。

**练习 2**：假如某天 `nllb_600m.cpp` 改用 RoPE 取代正弦位置编码，`nllb_export.py` 需要怎么改？

**参考答案**：正弦位置编码的计算会从「C++ 补算」迁移到「打进 ncnn 图」（或在 C++ 里改调 RoPE 生成函数）。导出脚本里 `SinusoidalPositionalEmbeddingTS` 这段「Python 参照实现」就不再适用，要么删掉、要么换成 RoPE 的等价实现；同时 `embedding_forward` 里的 `sinusoidal_positional_embedding` 调用也要相应替换。这体现了「导出端与运行时端必须对位置编码的算法达成一致」这一不变量。

**练习 3**：脚本里 `export_torchscript` 用 `torch.jit.trace` 而非 `torch.jit.script`，有什么潜在风险？

**参考答案**：`trace` 是「喂一组样例输入、记录所有算子调用」，它只捕捉**实际执行到的路径**，对含 `if/else` 等数据相关控制流的模块会丢失未走到分支。本脚本里 encoder/decoder 的层循环与 layer_norm 是固定结构，`trace` 尚可胜任；但若模型有「按输入长度切换分支」的逻辑，`trace` 会给出不完整的图。这也是脚本注释里强调「device-safe masks、trace fallback on target device」的原因。

---

## 5. 综合实践

**任务**：为「HunyuanOCR」模型梳理一份完整的「导出产物清单与来源」文档，把本讲四个脚本与最终 `assets/hunyuan_ocr/` 目录里的每个文件一一对应。

要求：

1. 先列出 `assets/hunyuan_ocr/` 目录应当包含的全部产物文件（参考 [readme.md:96-103](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/readme.md#L96-L103) 的目录结构约定，以及 u6-l3 提到的 HunyuanOCR 子网组成）。
2. 对每个产物，标注它的「来源脚本」与「所属支线」：
   - `vocab.txt` / `merges.txt` ← `hunyuan_ocr_tokenizer.py`（分词器支线）
   - `hunyuan_ocr_text_decoder.ncnn.param`（KV cache 版）← `hunyuan_ocr_add_kvcache.py`（结构后处理支线），其原始 `.param` 来自外部权重转换（**待确认**）
   - 视觉/嵌入等其他 `.ncnn.*` ← 外部工具（**待确认**）
   - `model.json` ← 手写（参考 u1-l5 与 u6-l3 的字段说明）
3. 画一张时序图，标出四个脚本的执行先后：`（外部）权重导出 → pnnx 转 .param/.bin → hunyuan_ocr_add_kvcache.py 改写 decoder → hunyuan_ocr_tokenizer.py 导出词表 → 手写 model.json → 落到 assets/`。
4. 验证「两端契约」对账：
   - 分词器端：确认 `vocab.txt` 行号 == token id（对应 4.2）。
   - 解码器端：对照 [src/ncnn_text_runtime.cpp:49-69](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_text_runtime.cpp#L49-L69)，确认改写后 `.param` 里的 `cache_k%d`/`out_cache_k%d` 与运行时读写一致（对应 4.3）。

**预期产出**：一份 Markdown 表格 + 一张时序图，能清楚回答「`assets/hunyuan_ocr/` 下每个文件分别由谁、在哪一步产生，以及它如何满足 C++ 运行时的契约」。涉及外部工具（`pnnx`、HF 下载）的环节，明确标注「待确认」。

## 6. 本讲小结

- 导出分三条支线：**权重**（PyTorch→ncnn，依赖外部 `pnnx`，**待确认**）、**分词器**（HF JSON→行号文本，仓库自托管）、**结构后处理**（改写 `.param` 注入 KV cache，仓库自托管）。
- `extract_tokenizer.py` 把 HF `tokenizer.json` 翻译成 `vocab.txt`/`merges.txt`，靠「按 id 升序排序」保证**行号 == token id**，并顺手为嵌入模型生成硬编码的 `model.json`。
- `hunyuan_ocr_tokenizer.py` 在前者基础上合并 818 个特殊令牌、用 `<|unused_{i}|>` 补 id 空洞，产出覆盖全量 120818 个 id 的连续词表。
- `hunyuan_ocr_add_kvcache.py` 把解码器 `.param` 里的 SDPA 从「4入1出」改写成「6入3出」并打开 `kvcache_enabled`，注入 `cache_k%d`/`cache_v%d` 入与 `out_cache_k%d`/`out_cache_v%d` 出，精确对齐 `llm_run_decoder_with_kv` 的槽位契约。
- `nllb_export.py` 把 NLLB 拆成 `embed`/`encoder_noembed`/`decoder_noembed` 三个 TorchScript 模块，刻意**不导出正弦位置编码**（由 C++ `sinusoidal_positional_embedding` 补算），产物基底名与 `nllb_main.cpp` 期望的 `.ncnn.*` 一一对应。
- 贯穿全讲的两个不变量：**分词器行号 == token id**、**KV cache 槽位命名与运行时一致**；README 已声明导出脚本可能过时，细节以仓库最新版本为准。

## 7. 下一步学习建议

- 想动手接入新模型？继续读 **u8-l6（接入新模型家族）**，它会把本讲的 model.json 字段、分词器选择、是否需要新 RoPE 变体/自定义算子等串成一份「接入清单」。
- 想验证导出产物的正确性？结合 **u8-l2（benchmark）** 与 **u8-l3（测试）**：前者能在不读真实权重的前提下确认 `.param` 结构能跑通，后者能验证分词器/模板等纯逻辑。
- 对 KV cache 的运行时行为还想深入？回顾 **u2-l2（共享文本运行时）** 与 **u2-l4（generate 主循环）**，看本讲注入的 cache 槽位在 prefill/decode 中如何被读写。
- 对 NLLB 这条 encoder-decoder 链路感兴趣？回顾 **u6-l5（NLLB 翻译）**，对照本讲的「正弦不导出」决策，理解导出端与运行时端的算法一致性要求。
