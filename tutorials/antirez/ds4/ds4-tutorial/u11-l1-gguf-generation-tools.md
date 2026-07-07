# GGUF 生成与量化工具

## 1. 本讲目标

本讲聚焦 ds4 离线模型构建工具链的入口：`gguf-tools/deepseek4-quantize`。它是一个**纯 C、模型专用**的量化器，把 Hugging Face 上的 DeepSeek V4 原始权重（safetensors）重新打包成 ds4 能直接 mmap 加载的 GGUF。

学完后你应当能够：

1. 说清楚「模板 GGUF + HF safetensors」这条重生成流水线里，**模板提供什么、HF 提供什么**，以及为什么要把两者分开。
2. 看懂 `--experts`、`--routed-w2`、`--attention-proj`、`--shared`、`--output` 等「张量族」覆盖开关如何把 u1-l2 讲过的**非对称量化策略**（只压 routed 专家）落到具体张量上。
3. 掌握 `--dry-run` 与 `--compare-tensor` 两个校验入口，学会在动辄 80–170 GB 的全量写出之前，廉价地验证配方是否正确。

> 本讲只讲「生成与校验」这一层。量化块（`q2_K`/`q4_K`/`iq2_xxs`）的字节布局与点积数学在 u3-l4 已讲过；imatrix 的**采集**与质量打分留给 u11-l2。

## 2. 前置知识

阅读本讲前，请确认你已经理解以下概念（否则建议先看对应讲义）：

- **GGUF 文件格式**：一种「自描述」的二进制容器，开头是 magic + 版本 + KV 元数据表 + 张量信息表 + 对齐填充 + 张量数据。ds4 用 mmap 整体映射它（见 u3-l1）。
- **safetensors**：Hugging Face 的权重存储格式，一个 JSON 头描述每个张量的名字、dtype、形状与字节偏移，紧跟裸张量字节；大模型会分片成多个 `.safetensors` 文件，外加一个 `model.safetensors.index.json` 总索引。
- **非对称量化策略**（u1-l2 / u3-l4）：ds4 只把 routed MoE 专家压到 2bit（gate/up 用 `IQ2_XXS`、down 用 `Q2_K`），而 shared 专家、注意力投影、output head 保持 `Q8_0`/`F16` 等高精度。
- **imatrix（重要性矩阵）**：一个按列给出的权重向量，告诉量化器哪些列更重要。`IQ2_XXS` 这类超低比特格式强依赖它（u3-l4）。
- **FP8（E4M3）/ FP4 / E8M0**：DeepSeek V4 官方权重的存储格式。dense 权重是 FP8 E4M3，routed 专家是 4bit packed（`I8` 字节里每字节塞两个 4bit 码），两者都配一个 `F8_E8M0` 的分块 scale 张量。本讲的工具要先把它们**反量化回 float32**，才能重新量化成 ds4 自己的块格式。

一个关键直觉：**模板是骨架，HF 是血肉**。模板 GGUF 告诉工具「要生成哪些张量、按什么顺序、逻辑形状多大、分词器和元数据长什么样」；HF safetensors 提供「每个张量的真实数值字节」。两者都不够单独成事——HF 不知道 ds4 的张量命名约定，模板里的字节又可能是旧配方。

## 3. 本讲源码地图

本讲涉及三个核心文件，全部位于 `gguf-tools/` 子目录：

| 文件 | 作用 | 本讲视角 |
| --- | --- | --- |
| [gguf-tools/deepseek4-quantize.c](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/gguf-tools/deepseek4-quantize.c) | 主程序：safetensors → GGUF 量化器（约 1900 行纯 C） | 流水线、张量族策略、校验入口都在这里 |
| [gguf-tools/quants.c](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/gguf-tools/quants.c) | 「刻意做窄」的本地量化实现 | 提供块格式 trait 表与 `q8_0/q4_K/q2_K/iq2_xxs` 四个写端 |
| [gguf-tools/quants.h](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/gguf-tools/quants.h) | 量化器的窄头接口 | 枚举值与 GGUF 类型 ID 对齐 |
| [gguf-tools/README.md](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/gguf-tools/README.md) | 工具使用说明 | Q2/Q4 生成命令、imatrix fallback 公式 |

补充：构建方式见 [gguf-tools/Makefile](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/gguf-tools/Makefile)，一行 `make -C gguf-tools` 即可，**不链接 GGML**（u1-l1 讲过的「格式借鉴、架构自建」原则在此落地）。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：**模板 + HF 重生成**、**张量族覆盖**、**校验选项**。

### 4.1 模板 + HF 重生成

#### 4.1.1 概念说明

`deepseek4-quantize` 解决的核心问题是：**ds4 的 GGUF 命名约定、张量顺序、分词器、元数据，Hugging Face 一个都不给；而 Hugging Face 给的真实权重字节，模板 GGUF 里又是旧配方的量化字节**。

所以工具采用「双输入」设计：

- **`--template`**：一个已存在的 ds4 GGUF。它贡献**骨架**——GGUF KV 元数据（含分词器、架构常量、`deepseek4.expert_count`）、张量信息表（名字、顺序、逻辑形状 `ne[]`、对齐方式）。模板的字节本身会被**丢弃**，只借用它的「说明书」。
- **`--hf`**：Hugging Face 模型目录（含 `model.safetensors.index.json`）。它贡献**血肉**——每个张量的真实数值，以 FP8/FP4/F16/BF16 等 dtype 存储。

工具的职责是：按模板列出的张量顺序，逐个把 GGUF 张量名**翻译**成 HF safetensors 名，从 HF 读字节、反量化回 float32，再按本run的配方重新量化，最后流式写出一个全新的 GGUF。整个过程**不依赖任何通用 GGUF 库**——GGUF 读写、safetensors 解析、FP8/FP4 反量化全是本目录里几百行手写 C。

一句话：**模板决定「生成什么」，HF 决定「数值多少」**。

#### 4.1.2 核心流程

主流程在 [`main`](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/gguf-tools/deepseek4-quantize.c#L1867-L1908) 里，是一条线性流水线：

```
parse_args                  # 解析 --hf/--template/--out/各 --张量族/--imatrix 等
  ↓
imatrix_load (可选)         # 若给了 --imatrix，读 legacy .dat
  ↓
load_gguf_metadata(template)# 只读模板的元数据 + 张量信息表，丢字节
  ↓
build_output_context        # 对每个张量套用 policy，决定新 type，重算 offset/size
  ↓
print_plan                  # 打印计划（n_tensors、type_changes、approx_file_bytes）
  ↓
if (--dry-run) return;      # 校验出口 1：到此为止，不碰 HF 字节
  ↓
db_open(hf)                 # 打开 safetensors 索引
  ↓
if (--compare-tensor)       # 校验出口 2：只重生成一个张量并字节比对，退出
    compare_one_tensor → 退出
  ↓
write_full_gguf             # 全量写出：逐张量 generate_tensor → 写盘
```

注意三个关键设计：

1. **模板的 KV 元数据是「原样拷贝」的**（按字节切片保留），不做翻译——这保证了分词器、架构常量等上百条元数据零失真地搬到新文件。
2. **专家数（routed expert count）来自模板元数据** `deepseek4.expert_count`，缺失时回退到 Flash 默认 256。
3. **imatrix 来源会被写进输出 GGUF 的元数据**（`quantize.imatrix.file` 等），作为「这份 GGUF 用哪个 imatrix 量化」的溯源；同时模板里**旧的**溯源 KV 会被丢弃，避免新旧重复。

数据流（单个非专家张量）：

```
模板张量名 blk.0.attn_q_a.weight
   │  hf_name_for_regular() 查 top_map/layer_map
   ▼
HF 名   layers.0.attn.wq_a.weight
   │  db_read() 从 safetensors 读字节
   ▼
dtype=F8_E4M3 → dequant_fp8_weight(配 .scale) → float32 行
   │  f32_to_type(target=q8_0, imatrix)
   ▼
Q8_0 量化字节 → 写进输出 GGUF
```

#### 4.1.3 源码精读

**(a) 模板元数据加载：只读说明书，丢字节**

[`load_gguf_metadata`](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/gguf-tools/deepseek4-quantize.c#L1458-L1550) 把模板的 KV 记录按**字节切片**原样保留进 `g.kv_raw`，只对极少数 key 做结构化解析：

- `general.alignment`（对齐粒度，默认 32）；
- `deepseek4.expert_count`（routed 专家数）。

它还顺手丢弃模板自带的旧 imatrix 溯源 key，注释解释了原因——否则输出会同时含新旧两份冲突的溯源元数据：

```c
/* 模板可能带着上一次量化的 imatrix 溯源；丢掉，本 run 稍后写新的，
   否则输出会同时含 stale 与 new 两份重复元数据。 */
if (!is_imatrix_kv_key(key)) {
    kv_keep[n_kv_keep++] = (byte_span){ ... };
}
```

紧接着它读**张量信息表**（名字、`n_dims`、`ne[]`、`type`、`old_offset`），并据此算出每个模板张量的字节大小 `t->size`——但**完全不去读模板的张量数据区**。这就是「借说明书、不借字节」。

**(b) 名字翻译表：GGUF 名 ↔ HF 名**

GGUF 用 `blk.N.attn_q_a.weight` 这种扁平命名，HF 用 `layers.N.attn.wq_a.weight` 这种嵌套命名。两张静态表 [`top_map`](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/gguf-tools/deepseek4-quantize.c#L913-L920)（顶层张量）与 [`layer_map`](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/gguf-tools/deepseek4-quantize.c#L922-L955)（每层张量）把两者一一对应，例如：

```c
{ "attn_q_a.weight",  "attn.wq_a.weight" },   // layer_map 里的一条
{ "token_embd.weight", "embed.weight" },       // top_map 里的一条
```

[`hf_name_for_regular`](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/gguf-tools/deepseek4-quantize.c#L957-L979) 先查 `top_map`，命中直接返回；否则解析出 `blk.N.` 的层号，把剩余后缀查 `layer_map`，拼成 `layers.{N}.{hf_name}`。**这正是「模板给顺序与名字、HF 给字节」的衔接点。**

> routed 专家走另一套命名，见 [`parse_expert_tensor`](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/gguf-tools/deepseek4-quantize.c#L881-L896)：GGUF 把一层全部 N 个专家压成单个张量 `blk.N.ffn_{gate,down,up}_exps.weight`，而 HF 是逐专家存成 `layers.N.ffn.experts.{x}.{w1,w2,w3}.weight`。

**(c) 从 HF 字节反量化回 float32**

[`generate_regular`](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/gguf-tools/deepseek4-quantize.c#L1167-L1204) 读完 HF 张量后，按 dtype 分流：

```c
if (strcmp(te->info.dtype, "F8_E4M3") == 0) {
    /* FP8 权重必须配一个同名 .scale（F8_E8M0）张量一起反量化 */
    f32 = dequant_fp8_weight(&w, &s, &n);
} else {
    f32 = tensor_to_f32(&w, &n);   /* F32/BF16/F16 直接转 */
}
```

FP8 反量化 [`dequant_fp8_weight`](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/gguf-tools/deepseek4-quantize.c#L683-L709) 按 128×128 的分块 scale 还原：每个 128×128 块共享一个 E8M0 scale \(s\)，块内每个 E4M3 系数 \(q\) 还原为 \(q \cdot s\)。其中 E8M0 是纯 2 的幂（指数位直接左移 23 位塞进 float）：

\[ s = 2^{e-127}, \qquad x = q_{\text{e4m3}} \cdot s \]

专家权重是更紧凑的 FP4：[`dequant_fp4_weight`](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/gguf-tools/deepseek4-quantize.c#L711-L739) 把每个 `I8` 字节拆成两个 4bit 码，查一张 16 项的码本表 `fp4_table`，再乘以每 32 列一组的 E8M0 scale。

**(d) 多线程重生专家**

[`generate_expert`](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/gguf-tools/deepseek4-quantize.c#L1266-L1296) 对一层全部 N 个专家（默认 256）开 `--threads`（默认 8）个 worker 线程并行量化，每个 worker 调 [`generate_one_expert`](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/gguf-tools/deepseek4-quantize.c#L1224-L1245) 处理一个专家 id `xid`，量化完按 `xid * per_expert` 偏移写进同一个输出缓冲，最终拼成 GGUF 里的单个打包张量。

**(e) 全量写出**

[`write_full_gguf`](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/gguf-tools/deepseek4-quantize.c#L1615-L1656) 先写 magic + 版本 + 张量数 + KV 数，再 `memcpy` 模板的 `kv_raw`，追加本 run 的 imatrix 溯源 KV（[`write_imatrix_kvs`](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/gguf-tools/deepseek4-quantize.c#L1436-L1456)），重写张量信息表（带新 type 与新 offset），对齐填充后**逐张量**调 `generate_tensor` 流式写出。读到哪写到哪，不一次性占满内存——这对 170 GB 的输出是必须的。

#### 4.1.4 代码实践

**实践目标**：亲手追踪一个张量名从「模板」走到「HF 字节」的全过程，直观感受「模板给顺序、HF 给字节」的分工。

**操作步骤**（纯源码阅读型，无需下载模型）：

1. 打开 [gguf-tools/README.md 的 Q2/Q4 生成命令](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/gguf-tools/README.md#L59-L110)，注意 Q2 模板文件名 `...IQ2XXS-w2Q2K-AProjQ8-SExpQ8-OutQ8...`，这串名字其实是一份「张量族配方缩写」。
2. 在 [`layer_map`](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/gguf-tools/deepseek4-quantize.c#L922-L955) 里找到 `attn_q_a.weight` 这一行，记下它映射到的 HF 名。
3. 在 [`generate_regular`](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/gguf-tools/deepseek4-quantize.c#L1167-L1204) 里确认：它先 `hf_name_for_regular(gguf_name)` 拿到 HF 名，再 `db_read` 读字节、`dequant_fp8_weight`/`tensor_to_f32` 反量化、`f32_to_type` 再量化。

**需要观察的现象**：模板张量名（GGUF 命名）与 HF 张量名是**两套完全不同的字符串**；同一个逻辑权重在两边名字不同，靠 `layer_map` 这张人写表硬连起来。

**预期结果**：你能用一句话回答——模板提供了「张量清单、顺序、逻辑形状、分词器、元数据、专家数」，HF 提供了「每个张量的真实权重字节（FP8/FP4/F16）」。

> 若本地没有 HF 权重，本实践为「源码阅读型」，无需运行；步骤 1–3 全在源码里完成。

#### 4.1.5 小练习与答案

**练习 1**：为什么工具不直接用 HF 的 safetensors，而要先有一个「模板 GGUF」？

> **答案**：HF 只给权重字节，不知道 ds4 的张量命名约定（`blk.N.attn_q_a.weight`）、张量顺序、分词器、架构常量、`deepseek4.expert_count` 等元数据。模板 GGUF 是一份「ds4 能理解的说明书」，工具照它的清单逐项从 HF 取字节重组。没有模板，工具就不知道要生成什么、按什么顺序、什么形状。

**练习 2**：模板里某个张量的旧量化字节会被写进输出文件吗？

> **答案**：不会。`load_gguf_metadata` 只读模板的元数据和张量**信息表**（名字/形状/type/offset），完全不去读模板的张量数据区；输出字节全部来自 HF，经「反量化回 float32 → 按本 run 配方再量化」重新生成。

---

### 4.2 张量族覆盖

#### 4.2.1 概念说明

u1-l2 讲过 ds4 的非对称量化策略：**只压 routed 专家，其余保持高精度**。但「其余」是个模糊的概念——注意力投影、shared 专家、output head、embedding 各自该用什么精度？这正是 `deepseek4-quantize` 的「张量族（tensor family）」机制要回答的。

工具把所有张量按**语义族**分类，每一族可以单独指定一个目标量化类型。族与 CLI 开关一一对应：

| 族 | CLI 开关 | 典型精度（Q2 配方） |
| --- | --- | --- |
| routed 专家 gate/up（w1/w3） | `--routed-w1` / `--routed-w3` 或 `--experts` | `iq2_xxs`（约 2bit） |
| routed 专家 down（w2） | `--routed-w2` | `q2_k` |
| 注意力投影（q/kv/output a/b） | `--attention-proj` | `q8_0` |
| 其它 attention/indexer/compressor | `--attention` | `q8_0` / `f16` |
| shared 专家 | `--shared` | `q8_0` |
| output head | `--output` | `q8_0` |
| token embedding | `--embedding` | 视配方 |

这套机制让 README 里那条 Q2 命令（`IQ2XXS-w2Q2K-AProjQ8-SExpQ8-OutQ8`）可以直接用一串 `--routed-w1/--routed-w2/...` 覆盖出来，而不必改一行 C 代码。

#### 4.2.2 核心流程

判定一个张量该用什么类型的逻辑集中在 [`policy_type`](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/gguf-tools/deepseek4-quantize.c#L1027-L1052)，它按固定**优先级**逐层判定：

```
1. --tensor-type NAME=TYPE 覆盖（精确名或前缀匹配）→ 最高优先
2. routed 专家（parse_expert_tensor 命中）→ 按 w1/w2/w3 分别取
3. 模板类型若已是不可再量化的量化类型（且非 f32/f16/bf16）→ 原样保留
4. 1-D 张量（norm/bias 等）→ 原样保留
5. embedding / output / shared / attention_proj / attention → 对应族
6. 兜底 dense → --dense；再兜底 → 模板原类型
```

要点：

- **专家三分**：gate（w1）、down（w2）、up（w3）是三个独立族，对应 DeepSeek V4 MoE 专家的三段 FFN 投影。这正是 Q2 配方里「gate/up 用 IQ2_XXS、down 用 Q2_K」能成立的基础。
- **「保留」也是一种策略**：如果模板里某张量已经是 `f16` 而你没显式覆盖，工具不会乱压它——这避免了误把不该压的张量压坏。
- **`DS4Q_TYPE_COUNT` 表示「未设置」**：每个族字段初值都是这个哨兵，`policy_type` 见到它就跳过该族、继续往下判。

#### 4.2.3 源码精读

**(a) 策略结构体**

[`quant_policy`](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/gguf-tools/deepseek4-quantize.c#L986-L991) 是一张「族 → 类型」的表：

```c
typedef struct {
    ds4q_type routed_w1, routed_w2, routed_w3;        /* 专家三段 */
    ds4q_type attention_proj, attention, shared, embedding, output, dense;
    type_override *overrides;                          /* --tensor-type 列表 */
    int n_overrides;
} quant_policy;
```

CLI 解析时 [`parse_args`](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/gguf-tools/deepseek4-quantize.c#L1740-L1814) 把所有族字段初始化为 `DS4Q_TYPE_COUNT`（未设置），再按开关填充，例如：

```c
} else if (strcmp(arg, "--experts") == 0 || strcmp(arg, "--routed") == 0) {
    ds4q_type t = parse_type(need_value(argc, argv, &i, arg));
    p.policy.routed_w1 = p.policy.routed_w2 = p.policy.routed_w3 = t;  /* 三段同设 */
}
```

注意 `--experts` 是个快捷方式，把 w1/w2/w3 一次设成同一个类型；想分开（Q2 配方）就得用 `--routed-w1`/`--routed-w2`/`--routed-w3` 分别给。

**(b) 专家分类器**

[`parse_expert_tensor`](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/gguf-tools/deepseek4-quantize.c#L881-L896) 用一条 `sscanf` 识别 routed 专家张量并分出 w1/w2/w3：

```c
if (sscanf(name, "blk.%d.ffn_%15[^_]_exps.weight%n", &layer, kind, &rest) == 2 ...) {
    e.part = strcmp(kind,"gate")==0 ? EXP_W1
           : strcmp(kind,"down")==0 ? EXP_W2 : EXP_W3;
}
```

非专家张量则靠一组子串分类器：[`is_attention_projection`](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/gguf-tools/deepseek4-quantize.c#L993-L997)（查 `.attn_q_a/.attn_kv/.attn_output_a` 等）、[`is_attention_tensor`](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/gguf-tools/deepseek4-quantize.c#L999-L1001)（含 compressor/indexer）、[`is_shared_expert`](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/gguf-tools/deepseek4-quantize.c#L1003-L1005)（`_shexp.`）、[`is_output_tensor`](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/gguf-tools/deepseek4-quantize.c#L1007-L1009)（`output.` 前缀）。

**(c) 判定主函数**

[`policy_type`](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/gguf-tools/deepseek4-quantize.c#L1027-L1052) 就是 4.2.2 那张优先级表的直接落地。注意两道「保留」闸门：

```c
/* 模板里已是不可再量化的量化类型（如 q6_k），且不是浮点 → 原样保留 */
if (tmpl->type != F32 && tmpl->type != F16 && tmpl->type != BF16
    && !ds4q_can_quantize(tmpl->type)) return tmpl->type;
if (tensor_n_dims(tmpl) <= 1) return tmpl->type;   /* 1-D 的 norm/bias 不压 */
```

第二道尤其重要：`norm`、`bias` 这类 1-D 张量永远原样保留，避免被误量化。

**(d) 可量化的只有四种**

[`ds4q_type_traits`](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/gguf-tools/quants.c#L39-L74) 表里 `can_quantize=true` 的**只有** `q8_0`、`q2_K`、`q4_K`、`iq2_xxs` 四种（表里还命名了其它几十种 GGUF 类型 ID，但都标 `can_quantize=false`，仅为元数据兼容）。这意味着族覆盖开关能给的量化类型就被限死在这四种（外加 `f32/f16/bf16` 浮点直转）。

#### 4.2.4 代码实践

**实践目标**：把 README 里 Q2 配方的文件名翻译成一串 CLI 覆盖开关，验证你理解了「族」机制。

**操作步骤**：

1. 读 [README 的 Q2 生成命令](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/gguf-tools/README.md#L69-L77) 与 [覆盖开关列表](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/gguf-tools/README.md#L89-L97)。
2. 模板名 `IQ2XXS-w2Q2K-AProjQ8-SExpQ8-OutQ8` 的含义是：gate/up 专家 = `iq2_xxs`、down 专家（w2）= `q2_k`、注意力投影（AProj）= `q8_0`、shared 专家（SExp）= `q8_0`、output = `q8_0`。
3. 把它翻成等价 CLI：`--routed-w1 iq2_xxs --routed-w3 iq2_xxs --routed-w2 q2_k --attention-proj q8_0 --shared q8_0 --output q8_0`。
4. 对照 [`parse_args`](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/gguf-tools/deepseek4-quantize.c#L1771-L1791) 确认每个开关都被识别、填进 `quant_policy` 对应字段。

**需要观察的现象**：文件名缩写与 CLI 开关是**同一份配方的两种写法**——文件名是给人看的标签，CLI 是给程序的指令。

**预期结果**：你能解释为什么 down 专家（w2）单独用 `q2_k` 而不是 `iq2_xxs`——因为 w2 是专家 FFN 的最后一段投影，对精度更敏感（u3-l4 讲过 `q2_K` 比 `IQ2_XXS` 精度更高但体积更大）。

> 待本地验证：若有模板与 HF 权重，可加 `--dry-run`（见 4.3）跑这串覆盖，对比 `type_changes` 列表是否与文件名缩写一致。

#### 4.2.5 小练习与答案

**练习 1**：如果用户既没给 `--experts` 也没给任何 `--routed-*`，routed 专家会被量化成什么？

> **答案**：`policy_type` 在专家分支里，当对应族字段仍是 `DS4Q_TYPE_COUNT`（未设置）时，会落到 `return tmpl->type;`——即**沿用模板里该专家张量原本的类型**。所以模板本身也编码了一份默认配方，覆盖开关只是改写它。

**练习 2**：`--tensor-type blk.5.ffn_gate_inp.weight=q8_0` 会作用于哪些张量？

> **答案**：`policy_type` 对 `--tensor-type` 既做**精确名匹配**也做**前缀匹配**（`str_starts`）。`ffn_gate_inp.weight` 是 router 张量，前缀匹配会命中所有 `blk.{N}.ffn_gate_inp.weight`，把它们统一设为 `q8_0`。这给了「按名字片段精确改一族」的逃生舱。

---

### 4.3 校验选项

#### 4.3.1 概念说明

全量生成一个 ds4 GGUF 是**重活**：README 说 2bit 家族输出约 80–90 GB、4bit routed 专家家族约 150–170 GB，还得留临时空间。在这种量级下，跑完才发现配方错了（比如某族类型给错、imatrix 没对齐、HF 反量化有 bug）代价极高。

所以工具提供三个**廉价校验入口**，让你在真正写盘前先验证：

- **`--dry-run`**：只规划、不读 HF、不写盘。打印「会生成多少张量、哪些类型会变、文件大概多大」。
- **`--compare-tensor NAME`**：只重生**一个**张量，与模板（或 `--compare-gguf` 指定的参考文件）里的同名字节逐字节比对，给出哈希与首个不一致偏移。
- **`--imatrix-strict`**：对强依赖 imatrix 的格式（`iq2_xxs`），若某张量找不到匹配的 imatrix 向量就直接报错，而不是悄悄走合成兜底。

此外 `--overwrite` 是一个安全闸：输出文件已存在时默认拒绝覆盖，必须显式声明才放行。

#### 4.3.2 核心流程

校验逻辑嵌在 [`main`](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/gguf-tools/deepseek4-quantize.c#L1867-L1908) 的流水线里，构成两个「提前退出点」：

```
build_output_context → print_plan
   ├── --dry-run ?  → return 0            （出口 A：不碰 HF）
   ├── 打开 db_open(hf)
   └── --compare-tensor ? → compare_one_tensor → return 0  （出口 B：只比一个张量）
write_full_gguf                              （真正全量写）
```

`--dry-run` 最早退出（连 HF 都不打开），`--compare-tensor` 稍晚（要读 HF 的一个张量）。两者都**不会**碰输出文件。

`print_plan` 的输出是校验的核心信息源：它列出每个发生 `type_change` 的张量（旧类型 → 新类型）、张量总数、元数据字节数、未对齐的张量字节数、近似文件总字节数、类型变更计数。一眼就能看出「覆盖开关有没有生效、文件会有多大」。

#### 4.3.3 源码精读

**(a) `--dry-run`：计划打印完即返回**

[`print_plan`](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/gguf-tools/deepseek4-quantize.c#L1658-L1675) 遍历所有张量，凡类型有变的打印一行：

```c
if (src->type != dst->type) {
    changed++;
    printf("type_change: %s %s -> %s\n", dst->name,
           ds4q_type_name(src->type), ds4q_type_name(dst->type));
}
printf("approx_file_bytes: %zu\n", out_ctx->data_offset + out_ctx->tensor_bytes);
```

随后 [`main`](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/gguf-tools/deepseek4-quantize.c#L1885-L1886) 里一行 `if (p.dry_run) return 0;` 截断后续。注意它发生在 `db_open`（打开 HF）**之前**，所以 dry-run 连 safetensors 都不读。

**(b) `--compare-tensor`：单张量字节比对**

[`compare_one_tensor`](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/gguf-tools/deepseek4-quantize.c#L1825-L1865) 重生一个张量、读参考 GGUF 的同名字量，算两边各自的 FNV-1a 64 哈希，再逐字节比对：

```c
printf("generated_fnv1a64: %016" PRIx64 "\n", fnv1a64_bytes(generated.data, generated.size));
printf("reference_fnv1a64: %016" PRIx64 "\n", fnv1a64_bytes(reference.data, reference.size));
...
if (!mismatches) printf("byte_compare: OK\n");
else printf("byte_compare: FAIL mismatches=%zu first=%zu\n", mismatches, first);
```

典型用法见 [README](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/gguf-tools/README.md#L99-L109)：

```sh
gguf-tools/deepseek4-quantize \
  --hf ../deepseek-v4-quants/hf/DeepSeek-V4-Flash \
  --template MODEL.gguf \
  --compare-tensor blk.0.attn_q_a.weight
```

它回答的问题是：「我的工具链（HF 反量化 + 再量化）对**这一个**张量，能不能复现模板里的字节？」这是回归测试的利器——改了 `quants.c` 里某个块格式后，用它验证输出没漂。

> 注意：`--compare-tensor` 默认拿**模板**当参考；想跟另一份 GGUF 比，加 `--compare-gguf FILE`。参考类型与再量化类型不同时，`generated_bytes` 与 `reference_bytes` 会不同，比对仍会跑（按较短长度比，多出部分计为 mismatch）。

**(c) imatrix 三态：外给 / 合成 / 严格**

当目标类型 `requires_imatrix`（目前仅 `iq2_xxs`/`iq2_xs`/`iq1_s` 为真，见 [traits 表](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/gguf-tools/quants.c#L39-L74)）却没给 imatrix 时，[`f32_to_type`](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/gguf-tools/deepseek4-quantize.c#L1090-L1131) 会合成一个**权重能量兜底**（与 [README 公式](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/gguf-tools/README.md#L111-L123) 一致）：

\[ \text{importance}[c] = \sum_{\text{row}} \text{row}[c]^2 \]

```c
if (!im_ptr && ds4q_requires_imatrix(type)) {
    synthetic = xcalloc((size_t)ncols, sizeof(float));
    for (int64_t r = 0; r < nrows; r++)
        for (int64_t c = 0; c < ncols; c++)
            synthetic[c] += row[c] * row[c];   /* 每列平方和 */
    im_ptr = synthetic;
}
```

这是「能用但不最优」的兜底——它只看权重自身的列能量，不反映真实激活分布。若你想强制要求**必须**有真实 imatrix（拒绝悄悄兜底），加 `--imatrix-strict`：[`imatrix_find`](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/gguf-tools/deepseek4-quantize.c#L819-L855) 找不到匹配向量时会 `exit(1)`。

**(d) `--overwrite` 安全闸**

[`parse_args`](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/gguf-tools/deepseek4-quantize.c#L1810-L1812) 末尾：

```c
if (p.out_gguf && file_exists(p.out_gguf) && !p.overwrite)
    die("output exists; use --overwrite");
```

避免一次手滑覆盖掉几十上百 GB 的好文件。

#### 4.3.4 代码实践

**实践目标**：用 `--dry-run` 与 `--compare-tensor` 这对「廉价探针」，在不写出全量模型的前提下验证一个配方。

**操作步骤**：

1. **dry-run 推演**：阅读 [README 的 dry-run 建议](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/gguf-tools/README.md#L62-L68) 与 [`print_plan`](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/gguf-tools/deepseek4-quantize.c#L1658-L1675)。设想你给 `--experts iq2_xxs --routed-w2 q2_k --attention-proj q8_0 --shared q8_0 --output q8_0`，描述 `print_plan` 会打印哪些 `type_change` 行、`approx_file_bytes` 大致落在哪个量级（README 说 2bit 约 80–90 GB）。
2. **compare-tensor 推演**：阅读 [`compare_one_tensor`](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/gguf-tools/deepseek4-quantize.c#L1825-L1865)。说明 `--compare-tensor blk.0.attn_q_a.weight` 会输出哪几行（tensor 名、类型、双方字节数、双方 FNV-1a64 哈希、`byte_compare: OK/FAIL`）。
3. **imatrix 决策**：对照 [traits 表](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/gguf-tools/quants.c#L54) 与 [README 的 fallback 段](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/gguf-tools/README.md#L111-L123)，回答：若不给 `--imatrix`、又用 `--experts iq2_xxs`，工具会怎样？加 `--imatrix-strict` 后又会怎样？

**需要观察的现象**：`--dry-run` 连 HF 都不读（在 `db_open` 之前返回）；`--compare-tensor` 只读 HF 的一个张量；两者都不碰输出文件。

**预期结果**：
- dry-run 会打印若干 `type_change: blk.N.ffn_gate_exps.weight f32 -> iq2_xxs` 之类，以及 `approx_file_bytes` 落在 80–90 GB 区间。
- compare-tensor 输出 `byte_compare: OK`（当工具能精确复现模板字节）或 `FAIL mismatches=... first=...`（当存在漂移）。
- 不给 imatrix 用 `iq2_xxs` → 走平方和合成兜底，能跑但质量打折；加 `--imatrix-strict` → 找不到匹配向量直接报错退出。

> 待本地验证：以上步骤 1、3 的精确数值（type_change 行数、文件字节数）依赖真实模板与 HF，本地无权重时为推演；步骤 2 的输出格式可从源码直接确认。

#### 4.3.5 小练习与答案

**练习 1**：`--dry-run` 和 `--compare-tensor` 哪个更「省」？为什么？

> **答案**：`--dry-run` 更省。它在 `db_open`（打开 HF safetensors 索引）**之前**就 `return 0`，完全不读任何张量字节；`--compare-tensor` 虽然只重生一个张量，但仍要打开 HF、读该张量字节并反量化。前者验证「配方计划」，后者验证「单张量字节正确性」。

**练习 2**：`iq2_xxs` 的 `requires_imatrix` 为 true，`q4_k` 为 false（见 traits 表）。这意味着什么？

> **答案**：`iq2_xxs` 每值仅约 2bit、靠码本查表编码，**必须**有列重要性向量指导码本搜索才能保证质量——所以没给 imatrix 时要么用平方和合成兜底、要么 `--imatrix-strict` 报错。而 `q4_k` 是带 4bit 加分组、自适应 scale 的仿射量化，imatrix 只是「可选增益」而非必需，所以 `requires_imatrix=false`，不给也能正常量化。这正是 u3-l4 讲过的根本区别在本工具里的直接体现。

**练习 3**：为什么 `load_gguf_metadata` 要丢弃模板里旧的 `quantize.imatrix.*` 元数据？

> **答案**：模板可能带着上一次量化的 imatrix 溯源信息。如果原样保留，本 run 又会写入**新的** imatrix 溯源（`write_imatrix_kvs`），输出 GGUF 就会同时含新旧两份冲突的同名 KV。所以加载时丢弃旧的、写出时只写本 run 的，保证溯源唯一可信（见 [load_gguf_metadata 注释](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/gguf-tools/deepseek4-quantize.c#L1496-L1507)）。

---

## 5. 综合实践

**任务**：你是 ds4 的模型构建负责人，要为社区发布一份新的 Q2 routed-experts GGUF。请设计一份「发布前校验清单」，并用源码证据支撑每一条。

请按顺序回答并给出对应的源码位置：

1. **配方翻译**：把目标文件名 `DeepSeek-V4-Flash-IQ2XXS-w2Q2K-AProjQ8-SExpQ8-OutQ8-chat-v2-imatrix.gguf` 翻译成一串 `--routed-*`/`--attention-proj`/`--shared`/`--output` 覆盖开关。（依据：[`quant_policy`](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/gguf-tools/deepseek4-quantize.c#L986-L991) 与 [`parse_args`](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/gguf-tools/deepseek4-quantize.c#L1771-L1791)）

2. **先 dry-run**：跑 `--dry-run`，检查 `type_changes` 计数与 `approx_file_bytes` 是否落在 80–90 GB。（依据：[`print_plan`](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/gguf-tools/deepseek4-quantize.c#L1658-L1675)、[README 输出量级](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/gguf-tools/README.md#L62-L68)）

3. **抽一个张量比对**：用 `--compare-tensor blk.0.attn_q_a.weight` 验证 HF→float32→q8_0 这条链路能字节级复现模板。（依据：[`compare_one_tensor`](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/gguf-tools/deepseek4-quantize.c#L1825-L1865)）

4. **imatrix 把关**：确认给了 `--imatrix`，并加 `--imatrix-strict` 防止 `iq2_xxs` 悄悄走平方和兜底。（依据：[`f32_to_type` 合成分支](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/gguf-tools/deepseek4-quantize.c#L1117-L1126)、[`imatrix_find` strict 分支](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/gguf-tools/deepseek4-quantize.c#L850-L854)）

5. **防覆盖**：确认输出路径不存在，或显式带 `--overwrite`。（依据：[`parse_args` 末尾](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/gguf-tools/deepseek4-quantize.c#L1810-L1812)）

完成后再回答一个开放问题：步骤 3 里，如果你改动了 `quants.c` 的 `q8_0` 块实现，`compare-tensor` 对一个原本 `q8_0` 的注意力投影张量**可能**会报告 `byte_compare: FAIL`，这一定是 bug 吗？为什么？

> 参考思路：不一定。`q8_0` 量化涉及 scale 的钳位与舍入，不同实现的边界行为可能合法地不同；`FAIL` 提示你「字节漂了」，需要结合 u11-l3 的测试向量与 u11-l2 的质量打分判断这是可接受的数值差异还是真正的回归。

## 6. 本讲小结

- `deepseek4-quantize` 是**纯 C、模型专用**的 safetensors→GGUF 量化器，不链接 GGML，GGUF 读写、safetensors 解析、FP8/FP4 反量化全是本目录手写。
- 它采用**双输入**设计：**模板 GGUF** 提供元数据、分词器、张量顺序、逻辑形状与专家数（骨架）；**HF safetensors** 提供每个张量的真实权重字节（血肉）。两者靠 `top_map`/`layer_map` 这对人写命名翻译表衔接。
- **张量族机制**（`quant_policy` + `policy_type`）把 u1-l2 的非对称量化策略落到具体张量：routed 专家三段（w1/w2/w3）、attention 投影、shared、output、embedding、dense 各成一族，可单独指定类型；判定按「覆盖 → 专家 → 保留量化类型 → 1-D 保留 → 各族 → 兜底」的固定优先级。
- 实际能写出的量化类型**只有四种**：`q8_0`、`q2_K`、`q4_K`、`iq2_xxs`（traits 表里 `can_quantize=true` 者）。
- 三个**校验入口**让大模型写出可前置验证：`--dry-run`（不读 HF、只打印计划）、`--compare-tensor`（重生一个张量并字节比对，配 FNV-1a64 哈希）、`--imatrix-strict`（拒绝悄悄走合成 imatrix 兜底）；外加 `--overwrite` 防覆盖安全闸。
- imatrix 三态：外部 `.dat`（最佳）、权重平方和合成兜底（`importance[c]=Σ row[c]²`，能用但不优）、`--imatrix-strict` 报错（强约束）；imatrix 溯源会写进输出 GGUF 元数据，旧溯源被丢弃以避免冲突。

## 7. 下一步学习建议

本讲只覆盖了「生成与校验」。要凑齐 ds4 模型构建的全貌，建议继续：

1. **u11-l2 imatrix 收集与质量测试**：本讲的 `--imatrix` 吃的是一个 `.dat` 文件——下一讲讲它如何用 `ds4 --imatrix-out` 在真实语料上采集 routed-MoE 激活统计，以及 `quality-testing/` 如何把本地 GGUF 对照官方 DeepSeek 续写打分。
2. **u3-l4 量化格式与张量族**（若尚未精读）：本讲的「四种可量化类型」的块字节布局、点积参考实现、imatrix 为何对 `iq2_xxs` 必需而对 `q4_k` 可选，根因都在那里。
3. **u11-l3 测试向量与回归**：本讲的 `--compare-tensor` 是「单张量字节级」校验；下一讲讲「整条 prefill 的 logit 级」回归，用官方/本地 golden 向量发现后端数值漂移。
4. **动手延伸**：读完本讲后，可尝试给 `quants.c` 的 `ds4q_type_traits` 表里某个 `can_quantize=false` 的格式（如 `q6_k`）补一个量化函数，再用 `--compare-tensor` 验证——这是理解「块格式与搜索过程」最直接的练习。
