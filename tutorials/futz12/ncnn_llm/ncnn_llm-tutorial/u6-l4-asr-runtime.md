# Qwen3-ASR 语音识别

## 1. 本讲目标

本讲讲解 ncnn_llm 中的语音识别（ASR，Automatic Speech Recognition）运行时 `ncnn_llm_asr`。它把一段声音波形变成文字，是「输入音频、输出文本」的模态。学完本讲你应当能够：

- 说清 ASR 的端到端链路：PCM 波形 → mel 频谱 → 音频卷积/编码器 → 拼入文本 decoder → lm_head → 解码文字。
- 理解音频前端如何把「采样点」一步步压缩成「音频 token」，并能用 `feat_out_len` 算出给定时长对应多少个音频 token。
- 看懂 ASR 如何复用 u2-l2 讲过的四个共享文本运行时函数（`llm_run_text_embed` / `llm_run_decoder_with_kv` / `llm_run_lm_head` / `llm_select_next_token`），以及为什么它的 RoPE 用了一个特别大的 `theta`（1e6）。
- 阅读并跟踪 `asr_main.cpp` → `transcribe` → `prefill` + `generate` 的调用链。

本讲依赖 **u2-l2（共享文本运行时）** 和 **u2-l3（prefill 流程）**，请先确认你理解那四个自由函数、KV cache 的 `cache_k%d/cache_v%d` 命名约定，以及因果掩码的构造方式。

## 2. 前置知识

### 2.1 PCM 波形与采样率

声音在计算机里最常见的表示是 **PCM（脉冲编码调制）**：把连续的声波按固定间隔采样，每个采样点存一个振幅值。本讲里的 PCM 是 16 kHz（每秒 16000 个采样点）、单声道、`float` 取值在 \([-1, 1]\) 之间的数组。一段 3 秒的音频就是 48000 个浮点数。

### 2.2 mel 频谱（log-mel spectrogram）

直接把 PCM 喂给神经网络效果不好，因为波形里同时混了大量频率。常规做法是做 **短时傅里叶变换（STFT）**：把波形切成相互重叠的小窗，每个窗算一次频谱，得到「时间 × 频率」的二维矩阵。再把频率轴换成 **mel 刻度**（一种更贴近人耳听觉、对低频分辨率更高的非线性刻度），最后取对数压缩动态范围，就得到 **log-mel 频谱**。它是几乎所有现代语音模型（Whisper、Qwen-Audio 等）的标配输入。

几个关键量（在 `model.json` 的 `setting.audio` 里可配，见 [src/ncnn_llm_asr.h:79-84](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_asr.h#L79-L84)）：

- `n_fft`（FFT 窗长，默认 400，对应 25 ms）
- `hop_length`（窗移步长，默认 160，对应 10 ms）
- `n_mels`（mel 频带数，默认 128）

> 直觉：hop=160、采样率 16000，所以每秒产生 \(16000/160=100\) 个 mel 帧。

### 2.3 与 VLM 的类比

如果你学过 U5（视觉语言模型），ASR 的整体思路会非常眼熟：

| 环节 | VLM（图像） | ASR（音频） |
|---|---|---|
| 原始输入 | BGR 像素图 | PCM 波形 |
| 特征前端 | ViT 视觉编码器 | mel + audio_conv + audio_encoder |
| 注入方式 | `<\|image_pad\|>` 占位符展开 | `<\|audio_pad\|>` 占位符展开 |
| 后续 | 复用文本 decoder 自回归 | 复用文本 decoder 自回归 |

核心套路一致：用一个模态专属的「编码器」把原始信号压成一段向量序列，再用文本 token 的占位符把这段向量「拼接」进文本嵌入流，最后交给共享的文本 decoder。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [src/ncnn_llm_asr.h](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_asr.h) | ASR 运行时类声明：六个 ncnn 子网、配置字段、`wav_to_logmel`/`run_audio`/`feat_out_len` 接口 |
| [src/ncnn_llm_asr.cpp](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_asr.cpp) | 全部实现：构造配置加载、mel 计算、分块卷积、prefill、generate、transcribe |
| [examples/asr_main.cpp](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/examples/asr_main.cpp) | 命令行入口：读 WAV、构造 `ncnn_llm_asr`、调用 `transcribe` |
| [src/ncnn_text_runtime.h](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_text_runtime.h) | 被复用的四个共享文本运行时函数（u2-l2 已讲） |
| [src/ncnn_llm_gpt.h](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.h) | `GenerateConfig` 与 `ncnn_llm_gpt_ctx` 上下文结构（u2-l5 已讲） |

ASR 运行时 `ncnn_llm_asr` 继承自 `ncnn_llm_base`（u2-l1），但不继承 `ncnn_llm_gpt`——它自己持有一套独立的 embed/decoder/lm_head 子网，但**复用**了 gpt 的上下文类型 `ncnn_llm_gpt_base_ctx` 和共享运行时函数。

## 4. 核心概念与源码讲解

### 4.1 整体架构、网络与配置

#### 4.1.1 概念说明

`ncnn_llm_asr` 是 Qwen3-ASR 的端到端 ncnn 运行时。它的总体设计在头文件顶部的一段注释里写得最清楚，建议先读这段注释建立全局印象：

```
pcm(16k mono) -> mel(STFT+gemm) -> audio_conv(per 100-frame chunk) -> audio_encoder
  -> splice into text_embed(audio_pad slots) -> decoder(KV cache) -> lm_head -> greedy decode.
```

也就是说，ASR 把语音识别拆成「音频前端」和「语言模型后端」两大段：

- **音频前端**：PCM → mel → audio_conv（分块卷积）→ audio_encoder，输出一段「音频嵌入」序列，形状为 `(hidden_size, L)`，其中 `L` 是音频 token 数。
- **语言模型后端**：把这段音频嵌入用 `<|audio_pad|>` 占位符拼接进文本 prompt，再走和文本 LLM 完全一样的 prefill/generate，自回归地吐出文字。

这正是 u2-l2 强调的「跨模态共享 decoder + KV cache 运行时」主线的又一次落地：ASR 没有重新写一套 decoder，而是把音频嵌入喂给了同一套共享函数。

#### 4.1.2 核心流程

构造时一共加载 **6 个 ncnn 子网**，分两组：

| 组 | 子网 | 输入 → 输出 | 作用 |
|---|---|---|---|
| 音频前端 | `mel_net_` | PCM → 功率 mel | STFT + mel 滤波器组 |
| 音频前端 | `audio_conv_net_` | 100 帧 mel → (896, 13) | 分块卷积下采样 |
| 音频前端 | `audio_encoder_net_` | (896, L) → (1024, L) | 音频 Transformer 编码器 |
| 文本后端 | `text_embed_net_` | token id → embedding | 词嵌入查表 |
| 文本后端 | `text_decoder_net_` | embed + KV → hidden | 因果 Transformer decoder |
| 文本后端 | `lm_head_net_` | hidden → logits | 投影到词表 |

推理时的数据流（伪代码）：

```
pcm                                  # float 数组, 16k 单声道
  -> wav_to_logmel  -> logmel        # (F, 128)  F = 样本数 / hop
  -> run_audio      -> audio_emb     # (1024, L) L = feat_out_len(F)
  -> prefill:
       组装 token_ids = prefix + L×audio_pad + suffix
       text_embed(token_ids)         # (1024, seq_len)
       用 audio_emb 覆盖 audio_pad 那一段
       generate_rope_embed_cache(theta=1e6)
       因果 mask
       decoder(is_prefill=true)      # 填充 KV cache, 取最后一个 token 的 hidden
       lm_head -> argmax             # 首个 token
  -> generate:                       # 复用共享运行时, 逐 token 解码直到 eos
```

#### 4.1.3 源码精读

**构造函数：加载 6 个子网。** 注意 ASR 显式把所有精度开关关掉，走纯 fp32 CPU 路径——注释解释原因：Qwen3-ASR 的残差流数值幅度很大，必须保留全精度，否则会崩。这与 LLM/VLM 运行时（用 bf16 storage）形成对比。

[src/ncnn_llm_asr.cpp:28-44](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_asr.cpp#L28-L44) 创建六个 Net 并关闭 fp16/bf16：这里逐个置 `use_fp16_packed / use_fp16_storage / use_fp16_arithmetic / use_bf16_storage = false`。

[src/ncnn_llm_asr.cpp:49-64](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_asr.cpp#L49-L64) 通过 `config["params"]` 的 12 个字段（每网 param+bin）加载权重，任一失败即 `ok_=false`。注意 ASR 的 params 键名与 gpt 不同：是 `mel_param/mel_bin`、`audio_conv_param/audio_conv_bin`、`audio_encoder_*`、`text_embed_*`、`text_decoder_*`、`lm_head_*`。

**配置读取：分词器与 setting。**

[src/ncnn_llm_asr.cpp:67-77](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_asr.cpp#L67-L77) 加载 bbpe 分词器，并把 `additional_special_tokens`（含 `<|audio_pad|>` 等）逐个 `AddAdditionalSpecialToken`，再把它们的 id 收进 `additional_special_id_set_`——这个集合在 `generate` 里用来判断「当前 token 是不是特殊 token、要不要解码成文字」。`eos_ids` 可配置成一个集合。

[src/ncnn_llm_asr.cpp:80-99](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_asr.cpp#L80-L99) 读取 `setting`：`attn_cnt`（decoder 层数，默认 28）、`hidden_size`（1024）、`head_dim`（128）、`rope_theta`（默认 **1000000.0**，即 1e6，远大于文本 LLM 常用的 1e4~1e5）、各类特殊 token id，以及 `setting.audio` 块的音频前端参数。

**prompt 模板：prefix / suffix。**

[src/ncnn_llm_asr.cpp:101-106](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_asr.cpp#L101-L106) 用硬编码 token id 拼出 prompt 骨架。展开成文本就是（注释也写明了）：

```
<|im_start|>system\n<|im_end|>\n<|im_start|>user\n<|audio_start|>
  [ L 个 <|audio_pad|> ]
<|audio_end|><|im_end|>\n<|im_start|>assistant\n
```

`prefix_ids_` 是 `<|im_start|>system\n<|im_end|>\n<|im_start|>user\n<|audio_start|>`，`suffix_ids_` 是 `<|audio_end|><|im_end|>\n<|im_start|>assistant\n`，中间夹 `L` 个 `<|audio_pad|>`。这是 Qwen3-ASR 的 ChatML 变体，prefill 时音频嵌入会覆盖这 `L` 个占位符。

#### 4.1.4 代码实践

**实践目标**：在源码层面建立 ASR 的「网络清单 + 配置字段」对照表，确认每个子网的输入输出形状。

**操作步骤**：

1. 打开 [src/ncnn_llm_asr.h:53-59](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_asr.h#L53-L59)，列出 6 个 `shared_ptr<ncnn::Net>` 成员。
2. 对照 [src/ncnn_llm_asr.cpp:49-64](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_asr.cpp#L49-L64)，写出每个子网对应的 `params` 键名。
3. 在笔记里画一张表：`子网名 | params 键 | 输入张量 | 输出张量`。

**预期结果**：你会得到一张 6 行的表，并能口头复述「PCM→mel→conv→encoder→embed→decoder→lm_head」的顺序。

> 待本地验证：若你已下载 ASR 模型权重到 `assets/qwen3_asr_0.6b/`，可 `cat assets/qwen3_asr_0.6b/model.json` 对照真实的 `params` 与 `setting.audio` 字段值。

#### 4.1.5 小练习与答案

**练习 1**：ASR 构造函数为什么把所有 fp16/bf16 开关都关掉？换成 LLM 运行时会怎样？

**答案**：因为 Qwen3-ASR 的残差流数值幅度很大，半精度会溢出/丢精度导致结果错误（见 [src/ncnn_llm_asr.cpp:39-44](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_asr.cpp#L39-L44) 的注释）。LLM 运行时为了省内存、提速会开 bf16 storage，但 ASR 选择牺牲性能换正确性。

**练习 2**：`prefix_ids_` 和 `suffix_ids_` 里为什么没有出现 `audio_pad` 的 id？

**答案**：`audio_pad` 的数量 `L` 取决于音频长度，是运行时才知道的，所以它不能写死在模板里，而是在 `prefill` 里动态插入 `L` 个（见 4.5 节）。

---

### 4.2 wav_to_logmel：PCM 波形到 mel 频谱

#### 4.2.1 概念说明

`wav_to_logmel` 是音频前端的第一步，把 PCM 浮点数组变成 log-mel 频谱矩阵。STFT 和 mel 滤波器组本身是固定的信号处理运算，ncnn_llm 把它们导出成了一个 ncnn 子网 `mel_net_`（用卷积/gemm 实现 STFT），这样推理时就不用在 C++ 里手写 FFT——直接调一次 ncnn 即可。算完原始功率 mel 后，再在 C++ 里做对数压缩和归一化（这部分是后处理，不在子网里）。

#### 4.2.2 核心流程

```
1. 把 PCM 拷进 ncnn::Mat audio(n)
2. mel_net: input("in0", audio) -> extract("out0", mel)   # mel = 原始功率 mel, (frames, n_mels)
3. 计算实际帧数 F = n / hop_length   # 丢弃最后一个中心填充帧, 对齐 Whisper
4. 对数压缩: v = log10(max(v, 1e-10))
5. 动态范围地板: 把所有低于 (全局最大 - 8) 的值钳到地板   # 80 dB 动态范围
6. 归一化: v = (v + 4) / 4
7. 返回 (F, n_mels) 的 log-mel 矩阵
```

#### 4.2.3 源码精读

**调 mel 子网算原始功率 mel。**

[src/ncnn_llm_asr.cpp:128-137](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_asr.cpp#L128-L137) 把 PCM 拷进 `ncnn::Mat audio(n)`，用 `in0/out0` 插槽（u2-l2 讲过的 ncnn 调用三步法）调 `mel_net_`，得到原始功率 mel。这里说明 STFT+mel 滤波器组已被「编译」进 ncnn 图。

**计算帧数 F。**

[src/ncnn_llm_asr.cpp:139-141](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_asr.cpp#L139-L141) `F = n / hop_length_`。注释说「drop the trailing center-pad frame (matches Whisper stft[...,:-1])」——即丢掉 STFT 因为对称 padding 产生的最后一个冗余帧，和 HF 的 Whisper 处理器保持一致。这是把 ncnn 输出截到「真实」帧数的关键一行。

**对数压缩 + 地板 + 归一化。**

[src/ncnn_llm_asr.cpp:143-164](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_asr.cpp#L143-L164) 三步后处理：

1. 逐元素 `v = log10(max(v, 1e-10))`，同时记录全局最大值 `gmax`。
2. 算地板 `floor_v = gmax - 8.0f`（log10 域 8 ≈ 80 dB 动态范围），把低于地板的值钳到地板。
3. 归一化 `v = (v + 4.0f) / 4.0f`，把数值大致搬到 \([0, 1]\) 附近。

最终输出 `out` 形状为 `(F, n_mels)`，即 `(w=F, h=128)`。注意 ncnn::Mat 这里用 `w` 表示帧数（时间轴）、`h` 表示 mel 维度，`row(i)` 取第 i 个 mel 频带跨所有帧。

#### 4.2.4 代码实践

**实践目标**：手动验证「每秒音频 → 100 mel 帧」的换算。

**操作步骤**：

1. 假设你有一段 3 秒、16 kHz 单声道的 wav，采样点数 \(n = 48000\)。
2. 在 [src/ncnn_llm_asr.cpp:139](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_asr.cpp#L139) 处心算 `F = 48000 / 160 = 300`。
3. 验证：\(F = \text{时长} \times 100\)，即每秒 100 帧。

**预期结果**：3 秒 → 300 帧。反推：300 帧 → 3 秒。

> 待本地验证：在 `wav_to_logmel` 返回前临时加一行 `printf("F=%d n_mels=%d\n", F, n_mels_);`，跑 `asr_main` 观察 `F` 是否等于 `样本数/160`。

#### 4.2.5 小练习与答案

**练习 1**：为什么用 `log10` 而不是 `log`（自然对数）？地板设成 `gmax - 8` 对应多少分贝？

**答案**：`log10` 是音频领域的惯例，10 倍功率 = 1 Bels = 10 dB。`gmax - 8` 在 log10 域是 8 个单位 = 80 dB 的动态范围，即比峰值低 80 dB 以上的能量被钳平，避免极端小值放大噪声。

**练习 2**：`F = n / hop_length_` 是整数除法，会不会丢掉尾部样点？

**答案**：会丢掉最后不足一个 hop（< 160 样点，即 < 10 ms）的部分，这与 Whisper 处理器只取完整帧的行为一致，对识别影响可忽略。

---

### 4.3 feat_out_len：mel 帧到音频 token 的换算

#### 4.3.1 概念说明

mel 频谱有 `F` 帧，但 audio 卷积/编码器会把时间轴**下采样**，最终只产生 `L` 个音频 token。`L` 由静态函数 `feat_out_len(n)` 给出，它复刻了导出脚本 `export_modules.feat_out_len` 的 Python 向下取整除法。理解 `feat_out_len` 是理解 ASR「音频占多少个 token」的钥匙，也是 prefill 里决定 `<|audio_pad|>` 数量的依据。

#### 4.3.2 核心流程

下采样的核心规律是 **「每 100 帧 mel → 13 个音频 token」**，这正好对应 4.2 里「每秒 100 mel 帧」，所以一条最重要的经验法则：

\[ \text{音频 token 数} \approx 13 \times \text{音频秒数} \]

即 **每秒语音约产生 13 个音频 token**。

完整 100 帧的块贡献 13 个 token；不足 100 帧的尾段则按 stride-2 卷积的递推给出较少 token。设 mel 帧数为 \(n\)：

- 完整块数 \(C = \lfloor n / 100 \rfloor\)，贡献 \(13C\) 个 token。
- 尾段帧数 \(r = n \bmod 100\)，贡献一个由递推公式决定的小数。

代码里的递推（用 Python 风格的向下取整除法 \(\lfloor \cdot/\cdot \rfloor\)，注意 \(r=0\) 时尾段贡献为 0）：

\[ \text{leave} = n \bmod 100 \]
\[ fl = \lfloor (\text{leave}-1)/2 \rfloor + 1 \]
\[ L = \lfloor (\lfloor (fl-1)/2 \rfloor + 1 - 1)/2 \rfloor + 1 + 13\lfloor n/100 \rfloor \]

几个典型值（可直接当作速查表）：

| mel 帧数 \(n\) | \(L = \text{feat\_out\_len}(n)\) | 含义 |
|---|---|---|
| 1 | 1 | 10 ms |
| 50 | 7 | 0.5 秒 |
| 100 | 13 | 1 秒（1 完整块） |
| 150 | 20 | 1.5 秒（1 块 + 半块 7） |
| 200 | 26 | 2 秒 |
| 1000 | 130 | 10 秒 |

可以看到「13 token/秒」非常稳：100 帧→13、1000 帧→130。

#### 4.3.3 源码精读

**Python floor 除法的 C++ 实现。**

[src/ncnn_llm_asr.cpp:116-126](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_asr.cpp#L116-L126) 是整个换算的核心。注意它先定义了一个 `fdiv` lambda 来模拟 Python 的 `//`（对负数也向下取整），这是为了让 C++ 行为和 HF 导出脚本逐位一致。因为在 `leave=0` 时 `leave-1=-1`，C++ 默认的 `/` 是向零截断（`-1/2=0`），而 Python `//` 是向下取整（`-1//2=-1`），两者不同，必须用 `fdiv` 纠正。

最后 `return ... + (n / 100) * 13` 这一行的 `(n/100)*13` 就是「完整块数 × 每块 13 token」。

**在 run_audio 里被调用。**

[src/ncnn_llm_asr.cpp:168-171](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_asr.cpp#L168-L171) `run_audio` 一开始就算 `L = feat_out_len(F)`，作为最终音频 token 数；同时算块数 `D = (F + chunk_frames_ - 1) / chunk_frames_`（向上取整，处理尾段不足 100 帧的情况）。

#### 4.3.4 代码实践

**实践目标**：用 `feat_out_len` 推算一段音频的 token 占用。

**操作步骤**：

1. 假设 wav 时长 4.8 秒、16 kHz → 样本数 \(n_{\text{pcm}} = 76800\)。
2. 算 mel 帧数 \(F = 76800 / 160 = 480\)。
3. 用 `feat_out_len(480)`：完整块 \(C = 4\)，贡献 \(52\)；尾段 \(r = 80\)。
4. 查表/递推估算尾段（80 帧介于 50→7 和 100→13 之间，线性粗估约 10）。
5. 真实值需按公式算：可在本地写个 5 行的 C++ 小程序调用 `ncnn_llm_asr::feat_out_len(480)` 打印。

**预期结果**：4.8 秒音频约产生 \(52 + \text{尾段} \approx 62\) 个音频 token，符合「13 token/秒 × 4.8 秒 ≈ 62」。

> 待本地验证：写一个只链接该静态函数的最小程序，或直接在 `prefill` 里读 [src/ncnn_llm_asr.cpp:219-220](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_asr.cpp#L219-L220) 那行现成的 `printf`——它已经会打印 `audio_tokens=%d`，即 `L`。

#### 4.3.5 小练习与答案

**练习 1**：`feat_out_len` 为什么不直接 `return n / 100 * 13` 完事？

**答案**：因为最后一块常常不足 100 帧（`leave = n % 100`），它下采样后并非 0 个 token，而是一个由 stride-2 卷积递推决定的小数（见公式），必须单独算。

**练习 2**：30 秒的音频大概占多少个音频 token？prefill 序列里会有多少个 `<|audio_pad|>`？

**答案**：约 \(13 \times 30 = 390\) 个音频 token；prefill 里会插入 390 个 `<|audio_pad|>` 占位符（见 4.5）。

---

### 4.4 run_audio：分块卷积 + 音频编码器

#### 4.4.1 概念说明

`run_audio` 把 log-mel 频谱变成最终的音频嵌入序列 `(hidden_size, L)`。它分两步：

1. **audio_conv（分块卷积）**：把 mel 按 100 帧切块，每块独立过一次卷积网，得到 `(896, 13)`——即每块 100 帧压成 13 个 token、每 token 896 维。这正好印证了 4.3 的「100 帧→13 token」。
2. **audio_encoder（音频 Transformer 编码器）**：把所有 token 在 token 轴上拼起来、截到 `L` 个，再过一遍 Transformer 编码器，把维度从 896 变到 1024（`hidden_size`），得到喂给文本 decoder 的音频嵌入。

「分块」而不是整段一次过卷积，是因为卷积网是按固定 100 帧窗导出的，尾段不足要补零。

#### 4.4.2 核心流程

```
logmel: (F, 128)
块数 D = ceil(F / 100)
for c in 0..D-1:
    切出第 c 块 chunk (100, 128), 不足补零
    audio_conv(chunk) -> convout (896, 13)
    收集 convout
把所有 convout 在 token 轴拼接 -> hidden (896, L)   # 截到 L 个真实 token
audio_encoder(hidden) -> audio_emb (1024, L)
```

#### 4.4.3 源码精读

**分块 + 补零。**

[src/ncnn_llm_asr.cpp:177-186](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_asr.cpp#L177-L186) 循环每个块：`chunk` 先 `fill(0.0f)` 补零，再用 `memcpy` 把真实帧 `cnt = min(100, F - s)` 拷进去。尾块不足 100 帧时右侧自动是零。这里 `chunk` 形状是 `(w=100, h=128)`，单通道。

**逐块过 audio_conv。**

[src/ncnn_llm_asr.cpp:187-195](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_asr.cpp#L187-L195) 每块用 `in0/out0` 调 `audio_conv_net_`，输出 `convout` 形状 `(w=896, h=13)`——即 13 个 token、每 token 896 维。注释 `(w = 896, h = 13)` 与 4.3 的「100 帧→13 token」完全对应。所有块的输出收集进 `toks`。

**拼接 + 截断到 L 个真实 token。**

[src/ncnn_llm_asr.cpp:197-204](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_asr.cpp#L197-L204) 申请 `hidden (conv_w, L)`，把每个 `convout` 的 13 行逐行 `memcpy` 进去，写到 `L` 行就停。注意这里只取每块卷积输出的「前若干行」来凑足 `L`——因为尾块实际只对应 `feat_out_len(leave)` 个 token，而不是完整的 13 个，多出来的要丢掉。这就是为什么 4.3 必须精确算 `L`：它决定了从卷积输出里取多少行。

**audio_encoder 升维。**

[src/ncnn_llm_asr.cpp:206-212](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_asr.cpp#L206-L212) 把 `hidden (896, L)` 喂给 `audio_encoder_net_`，输出 `audio_emb (1024, L)`，即把维度从卷积的 896 升到 decoder 的 `hidden_size` 1024。这一步是纯 Transformer 编码器（无 KV cache、整段一次前向）。

#### 4.4.4 代码实践

**实践目标**：理解「分块卷积 + 截断」如何与 `feat_out_len` 配合。

**操作步骤**：

1. 读 [src/ncnn_llm_asr.cpp:168-213](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_asr.cpp#L168-L213) 整个 `run_audio`。
2. 假设 `F = 150`：则 `D = ceil(150/100) = 2` 块；`L = feat_out_len(150) = 20`。
3. 第一块（帧 0..99）→ 13 个 token；第二块（帧 100..149，补零到 100）→ 卷积仍输出 13 行，但 `feat_out_len(50)=7`，所以只取前 7 行。13 + 7 = 20 = L。✓
4. 验证拼接循环 [src/ncnn_llm_asr.cpp:200-204](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_asr.cpp#L200-L204) 的 `written < L` 条件会在写到第 20 行时停下，丢弃第二块多余的 6 行。

**预期结果**：你能在脑中演练「卷积按块产出 13 行/块，但总行数被 `L` 截断」的过程，理解 `feat_out_len` 与卷积输出截断的一致性。

> 待本地验证：在 `run_audio` 拼接循环里加 `printf("block %d wrote %d rows\n", c, ...)`，跑 `asr_main` 观察每块实际写入行数。

#### 4.4.5 小练习与答案

**练习 1**：为什么不把整段 mel 一次喂给 audio_conv，而要分块？

**答案**：卷积子网是按固定 100 帧输入导出的（结构、权重都假定 100 帧），整段不同长度无法直接喂。分块把任意长度归约成「若干个 100 帧 + 一个补零尾块」，每块独立处理。

**练习 2**：`audio_conv` 输出 896 维，`audio_encoder` 输出 1024 维，为什么不直接让卷积输出 1024？

**答案**：这是 Qwen3-ASR 原始模型结构的设计——卷积负责时序下采样（100→13），编码器负责语义建模并把特征投影到与文本 decoder 一致的 `hidden_size`，便于后续拼接进文本嵌入流。

---

### 4.5 audio_pad 槽位拼接与共享运行时（prefill / generate / transcribe）

#### 4.5.1 概念说明

音频嵌入 `(1024, L)` 算出来后，怎么喂给文本 decoder？答案和 VLM 的 `inject_image_embeds`（u5-l3）同源：**用占位符拼接**。文本 prompt 里放 `L` 个 `<|audio_pad|>` 占位 token，先让 `text_embed` 把它们（连同前后文本）查表成嵌入，再用音频嵌入**覆盖**这 `L` 个占位符的位置。之后整段嵌入序列就交给 u2-l2 的共享函数 `llm_run_decoder_with_kv` 跑 prefill、`llm_run_lm_head` 投影、`llm_select_next_token` 采样——和纯文本 LLM 没有任何区别。

`generate` 则完全是 u2-l4 自回归循环的 ASR 版本：逐 token embed → decoder（KV cache 增量更新）→ lm_head → 采样，直到遇到 eos。ASR 默认走贪心解码（`temperature=0, top_k=1`）。

#### 4.5.2 核心流程

**prefill（带音频）：**

```
logmel = wav_to_logmel(pcm)
audio_emb = run_audio(logmel)          # (1024, L)
token_ids = prefix_ids + [audio_pad]×L + suffix_ids
token_embed = text_embed(token_ids)    # (1024, seq_len)
用 audio_emb 覆盖 token_embed 的 audio_pad 那 L 行
rope cache: generate_rope_embed_cache(seq_len, head_dim, 0, theta=1e6)
因果 mask (seq_len, seq_len)
decode_out = decoder(token_embed, mask, rope, kv_cache, attn_cnt, is_prefill=true)
last_hidden = decode_out 的最后一行
logits = lm_head(last_hidden)
next_token = argmax(logits)            # 贪心取首 token
返回 ctx {kv_cache, cur_token=next_token, position_id=seq_len}
```

**generate（自回归，复用共享运行时）：**

```
克隆 ctx
for step in 0..max_new_tokens:
    tok = ctx.cur_token
    若 tok ∈ eos_ids: break
    若 tok 不是特殊 token: callback(decode(tok))
    cur_embed = text_embed(tok)                # 单 token
    rope cache: generate_rope_embed_cache(1, head_dim, position_id, theta=1e6)
    position_id++
    mask = 全零 (1, kv_len+1)                  # 单 token, 因果天然满足
    decode_out = decoder(cur_embed, mask, rope, kv_cache, attn_cnt, is_prefill=false)
    logits = lm_head(decode_out)
    next_id = llm_select_next_token(logits, history, sample_cfg)
    cur_token = next_id; history.insert(next_id)
```

注意 ASR 的 generate 比文本 LLM 简化了一些：没有 `<think>` 标签处理、没有工具调用分流，但多了「特殊 token 跳过解码」（audio 的输出里有 `<language>`、`<asr_text>` 等结构标记，不能当普通文字解码出来）。

#### 4.5.3 源码精读

**prefill：组装 prompt + 覆盖占位符。**

[src/ncnn_llm_asr.cpp:215-229](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_asr.cpp#L215-L229) 先算 mel 和 audio_emb，记下 `first_audio_index`（占位符起始下标），然后把 `prefix_ids` + `L` 个 `audio_pad_id_` + `suffix_ids` 拼成 `token_ids`。`seq_len = 前缀 + L + 后缀`。这一步就是「在文本流里给音频预留 L 个座位」。

[src/ncnn_llm_asr.cpp:231-236](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_asr.cpp#L231-L236) 调共享函数 `llm_run_text_embed`（u2-l2）把 `token_ids` 查表成 `token_embed (1024, seq_len)`，再用 `memcpy` 把 `audio_emb` 的 `L` 行**逐行覆盖**到 `first_audio_index .. first_audio_index+L` 的位置——占位符的嵌入被真实音频嵌入替换。这与 u5-l3 的 `inject_image_embeds`「单占位符展开」略有不同：ASR 是「预先放好 L 个占位符再逐个覆盖」，VLM 是「放 1 个占位符再删除并插入 H 个」。

**prefill：RoPE（theta=1e6）+ 因果 mask + 共享 decoder。**

[src/ncnn_llm_asr.cpp:238-248](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_asr.cpp#L238-L248) 生成 **基础 RoPE** cache（`generate_rope_embed_cache`，u4-l1 讲过），关键是第 5 个参数 `rope_theta_` = 1e6——远大于文本 LLM 常用的 1e4~1e5。ASR 序列很长（音频 token 一秒 13 个，几十秒就几百 token），大 theta 让低频维度转得更慢，更适合长序列。因果 mask 构造方式与 u2-l3 完全一致：对角线及以下为 0、以上为 `-1e38f`。

[src/ncnn_llm_asr.cpp:250-255](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_asr.cpp#L250-L255) 调共享函数 `llm_run_decoder_with_kv`（`is_prefill=true`，填充 KV cache），取最后一行 `decode_out.row_range(seq_len-1, 1)`，过 `llm_run_lm_head` 得 logits，`argmax1d` 贪心取首 token。注意 ASR prefill **没有**采用 u2-l3 的「两段式 pop_back」设计，而是整段一次过 decoder 再切最后一行——因为这里整段已含音频嵌入，结构上更简单。

[src/ncnn_llm_asr.cpp:257-261](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_asr.cpp#L257-L261) 把 KV cache、首 token、`position_id=seq_len` 打包进 `ncnn_llm_gpt_base_ctx`（u2-l5 讲过的上下文类型）返回。

**generate：自回归循环。**

[src/ncnn_llm_asr.cpp:264-285](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_asr.cpp#L264-L285) 先 `clone()` 输入 ctx（u2-l5 的克隆机制，保护调用方），再进 `for` 循环。每步：取 `cur_token`、遇 eos 跳出；用 `special_id_begin_`（151643）和 `additional_special_id_set_` 判断当前 token 是否特殊，**非特殊**才 `bpe->decode` 成文字回调出去——这是 ASR 独有的过滤，避免把 `<language>`、`<asr_text>` 这类结构标记当文字吐出来。

[src/ncnn_llm_asr.cpp:287-300](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_asr.cpp#L287-L300) 单 token embed → 单位置 RoPE（`position_id` 递增）→ 全零增量 mask `(1, kv_len+1)`（decode 阶段因果天然满足，u2-l4 讲过）→ 共享 `llm_run_decoder_with_kv`（`is_prefill=false`，读旧 cache 原地覆盖）→ `llm_run_lm_head`。这一段与 u2-l4 的 generate 主循环几乎逐行对应，差别只在 ASR 用 `text_embed_net_/text_decoder_net_/lm_head_net_` 这些自有的子网，而非 gpt 的成员。

[src/ncnn_llm_asr.cpp:302-313](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_asr.cpp#L302-L313) 组装 `LlmTokenSampleConfig`（u3-l4 讲过），调共享 `llm_select_next_token`（含 repetition penalty，但 `asr_main` 默认 `repetition_penalty=1.0` 即不惩罚）。默认配置 `temperature=0, top_k=1, do_sample=0` 走纯贪心。

**transcribe：prefill + generate + 切分输出。**

[src/ncnn_llm_asr.cpp:317-352](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_asr.cpp#L317-L352) 是便捷接口：先 `prefill`，再 `generate`（`out_ids` 收集所有 token id），最后按 `<asr_text>`（`asr_text_id_`）把输出切成「语言」和「文字」两段。模型实际输出格式是 `<language> 语言 <asr_text> 转写文字`，所以 `transcribe` 用 `std::find` 找到 `asr_text_id_`，之前的 id 解码成语言、之后的解码成文字，并对语言做去前缀（剥掉模型自带的 `"language "` 字样）和 trim。

**入口：asr_main.cpp。**

[examples/asr_main.cpp:109-118](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/examples/asr_main.cpp#L109-L118) 是典型用法：构造 `GenerateConfig`（贪心解码），调 `asr.transcribe(pcm, cfg, &language)`，打印语言和文字。

#### 4.5.4 代码实践

**实践目标**：跟踪完整的 `asr_main → transcribe → prefill + generate` 链路，理解 `prefix_ids/suffix_ids` 围绕 `audio_pad` 的模板作用。

**操作步骤**：

1. 读 [examples/asr_main.cpp:77-123](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/examples/asr_main.cpp#L77-L123) 的 `main`：`read_wav` 读 PCM → 构造 `ncnn_llm_asr` → 配置贪心 `GenerateConfig` → `transcribe`。
2. 跟进 [src/ncnn_llm_asr.cpp:317-320](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_asr.cpp#L317-L320)：`transcribe` 先 `prefill(pcm)` 得首 token 的 ctx，再 `generate(ctx, cfg, nullptr, &gen_ids)`。
3. 在 [src/ncnn_llm_asr.cpp:223-229](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_asr.cpp#L223-L229) 处确认 prompt 装配顺序：`prefix_ids_`（system+user 开头+`<|audio_start|>`）→ `L` 个 `audio_pad` → `suffix_ids_`（`<|audio_end|>`+`<|im_end|>`+assistant 开头）。
4. 解释模板作用：`prefix/suffix` 提供对话上下文框架（system/user/assistant 角色），中间的 `audio_pad` 是音频嵌入的「座位」，模型看到完整 ChatML 结构后从 assistant 位置开始续写语言和文字。

**需要观察的现象**（若本地有模型与一段 16k 单声道 wav）：

- `[ncnn_llm_asr] audio: samples=..., mel_frames=..., audio_tokens=...` 这行（[src/ncnn_llm_asr.cpp:219-220](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_asr.cpp#L219-L220)）打印的 `audio_tokens` 应等于 `feat_out_len(mel_frames)`。
- 最终输出形如 `language: Chinese` / `text: 你好世界`。

**预期结果**：能口头复述「prompt = prefix + L×audio_pad + suffix，audio_emb 覆盖 audio_pad 段，共享 decoder prefill 出首 token，generate 自回归吐出 `<language> ... <asr_text> 文字`，transcribe 按标记切分」。

> 待本地验证：需要先按 u8-l4 的导出流程或官方指引准备 `assets/qwen3_asr_0.6b/`（含 model.json 与 6 个子网的 param/bin、vocab/merges），然后 `xmake build asr_main && xmake run asr_main --audio your_16k_mono.wav`。仓库 `assets/` 默认不含模型权重（见 u1-l3）。

#### 4.5.5 小练习与答案

**练习 1**：ASR prefill 为什么能用整段一次过 decoder 再取最后一行，而不像 u2-l3 那样做「两段式 pop_back」？

**答案**：u2-l3 的两段式是为了在「主体只取 KV cache、末位单 token 取 hidden」之间节省 prefill 显存/计算。ASR 这里整段已含音频嵌入、序列相对可控，且实现上更直接：整段过 decoder 填满 KV cache，再用 `row_range(seq_len-1, 1)` 切最后一行经 lm_head 取首 token。两者目的相同（取末位 logits + 填 KV cache），只是取舍不同。

**练习 2**：`generate` 里 `if (!is_special && callback)` 这个判断去掉会怎样？

**答案**：模型输出里夹着 `<|endoftext|>`、`<language>`、`<asr_text>` 等特殊 token，若不过滤、直接 `bpe->decode`，这些控制标记会变成乱码或空串混进转写文字里。该判断保证只有真正的文字 token 才回调输出。

**练习 3**：ASR 的 `rope_theta_=1e6` 比文本 LLM 大一两个数量级，好处是什么？

**答案**：theta 越大，`inv_freq = 1/theta^(2i/d)` 越小，旋转角越慢，位置编码的「周期」越长，更适合 ASR 这种「音频 token 多、序列长」的场景，避免长序列上高频维度旋转角过大导致外推崩坏（参见 u4-l1 关于 RoPE 频率分布的讨论）。

## 5. 综合实践

把本讲四个最小模块串起来，做一次「音频时长 → token 占用 → prompt 结构」的全链推演。

**任务**：假设你有一段 6.5 秒、16 kHz 单声道的 wav。

1. **采样点**：算出 PCM 样本数 \(n_{\text{pcm}}\)。
2. **mel 帧**：用 `wav_to_logmel` 的规则算 \(F\)。
3. **音频 token**：用 4.3 的速查表/递推算 \(L = \text{feat\_out\_len}(F)\)。
4. **prompt 长度**：写出 `token_ids` 的总长度 `seq_len`（= `prefix_ids_.size() + L + suffix_ids_.size()`，参考 [src/ncnn_llm_asr.cpp:105-106](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_asr.cpp#L105-L106) 的 prefix/suffix 各 9/6 个 id）。
5. **覆盖位置**：标出 `first_audio_index` 与被 `audio_emb` 覆盖的行范围。
6. **首 token**：说明 prefill 后 `ctx->position_id` 等于什么，`ctx->cur_token` 是怎么得到的。
7. **解码**：描述 `generate` 如何逐 token 走共享 `llm_run_decoder_with_kv`（`is_prefill=false`），直到 eos；`transcribe` 又如何按 `<asr_text>` 切出语言与文字。

**参考答案要点**：

1. \(n_{\text{pcm}} = 16000 \times 6.5 = 104000\)。
2. \(F = 104000 / 160 = 650\) 帧。
3. \(L = \text{feat\_out\_len}(650)\)：完整块 \(C=6\) → 78；尾段 \(r=50\) → 7；\(L=85\)（约 \(13 \times 6.5 = 84.5\)，吻合）。
4. `seq_len = 9 + 85 + 6 = 100`。
5. `first_audio_index = 9`，覆盖行 `9 .. 93`。
6. `position_id = seq_len = 100`；`cur_token = argmax(lm_head(decoder 最后一行))`。
7. generate 每步单 token embed + 单位置 RoPE(theta=1e6) + 全零增量 mask + 共享 decoder（读旧 KV、原地覆盖、长度+1）+ lm_head + `llm_select_next_token`（贪心）；遇 eos 停；transcribe 用 `std::find(asr_text_id_)` 把 `gen_ids` 切成语言段与文字段分别解码。

> 待本地验证：跑 `xmake run asr_main --audio your.wav`，对照 [src/ncnn_llm_asr.cpp:219-220](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_asr.cpp#L219-L220) 打印的 `audio_tokens`，验证你的手算 \(L\)。

## 6. 本讲小结

- ASR 是「输入音频、输出文本」的模态，运行时 `ncnn_llm_asr` 继承 `ncnn_llm_base`，自带 6 个子网（mel / audio_conv / audio_encoder + text_embed / text_decoder / lm_head）。
- 音频前端链路：PCM → `wav_to_logmel` 得 `(F, 128)` log-mel（每秒 100 帧）→ `run_audio` 分块卷积（每 100 帧→13 token）+ 编码器得 `(1024, L)` 音频嵌入。
- `feat_out_len(n)` 给出 mel 帧到音频 token 的换算，核心经验法则是 **每秒语音约 13 个音频 token**；它用 Python floor 除法保证与导出脚本逐位一致。
- 注入机制：prompt 里放 `L` 个 `<|audio_pad|>` 占位符，`text_embed` 查表后用音频嵌入逐行覆盖，与 VLM 的 `inject_image_embeds` 同源。
- prefill/generate **原样复用** u2-l2 的四个共享函数；RoPE 用基础版但 `theta=1e6` 适配长序列；默认贪心解码。
- ASR 的 `generate` 比文本 LLM 多了「特殊 token 跳过解码」过滤；`transcribe` 按 `<asr_text>` 标记把输出切成语言与文字两段。构造时显式关闭所有 fp16/bf16，走纯 fp32 CPU 路径以保数值精度。

## 7. 下一步学习建议

- **对比三种「编码器注入」模态**：把本讲（音频）与 u5（VLM 图像）、u6-l3（OCR）的注入方式并排看，体会「占位符 + 覆盖/展开」这一统一套路在不同模态下的变体。
- **深入 RoPE**：本讲只用基础 RoPE 但 `theta=1e6`，建议回看 u4-l1 理解 theta 对频率分布的影响，以及 NTK/YaRN/LongRoPE 如何在更长序列上扩展。
- **导出侧**：ASR 的 `feat_out_len` 必须与导出脚本一致，建议结合 u8-l4 阅读 `export/` 下的导出脚本（如有 ASR 相关），理解 mel/audio_conv/audio_encoder 如何从 PyTorch 导出成 ncnn 子网。
- **采样对比**：ASR 默认贪心（`temperature=0, top_k=1`），可回看 u3-l4 的 `llm_select_next_token`，尝试把 `asr_main` 的 `GenerateConfig` 改成带温度采样，观察转写稳定性变化（待本地验证）。
