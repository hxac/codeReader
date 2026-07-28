# NLLB 机器翻译

## 1. 本讲目标

本讲讲解 ncnn_llm 项目里唯一的「真·encoder-decoder」运行时 `nllb_600m`,它把 Meta 的 NLLB-600(No Language Left Behind)翻译模型跑在 ncnn 上,实现 200+ 语言对之间的互译。

学完后你应该能够:

1. 说清 encoder-decoder 架构在本项目里与前几讲 decoder-only 运行时(LLM/VLM/OCR/ASR)的本质区别——多出一个独立的 encoder,decoder 通过 cross-attention 读 encoder 的输出。
2. 读懂 `nllb_600m` 的构造流程:三张子网(embed / encoder / decoder)+ BPE 词表,并理解它为何绕开 `model.json` 直接吃文件路径。
3. 掌握 `translate` 的四个重载(同步/流式 × 默认/自定义配置)以及流式输出的「增量解码」原理。
4. 说出正弦位置编码(sinusoidal positional embedding)的公式与它在 encoder、decoder 两端各自的作用,并理解它和 RoPE 的区别。
5. 解释源语言 / 目标语言 token(`zho_Hans`、`eng_Latn` 等)如何塞进序列、如何引导翻译方向。

## 2. 前置知识

阅读本讲前,请先掌握以下概念(本手册前几讲已建立):

- **encoder-decoder 与 decoder-only**:前面几讲的 LLM、VLM、OCR、ASR 都是 decoder-only——只有一个 Transformer decoder 堆,靠 KV cache 自回归续写。NLLB 是 encoder-decoder:先有一个 encoder 把源语言句子编码成一串「上下文向量」,再有一个 decoder 一边自回归生成目标语言、一边用 **cross-attention** 去「看」encoder 输出。
- **KV cache 与 `cache_k%d`/`cache_v%d` 命名约定**:这是 ncnn_llm 与导出脚本之间的隐式契约,在 u2-l2 已讲过。NLLB 只在 **decoder** 侧使用 KV cache,encoder 不用。
- **基类 `ncnn_llm_base` 的公共能力**:NLLB 的实现类继承自它,复用 `create_option`、`load_net`、`sample_logits`、`KVCache` 类型别名以及正弦位置编码工具函数(见 u2-l1)。
- **BPE 分词器**:NLLB 用的是 `BpeTokenizer`(见 u3-l1、u3-l2),其词表里除了普通子词,还有一批特殊的「语言 token」。

> 一个贯穿全讲的关键直觉:**NLLB 是项目里最「古典」的 Transformer**——它用原始论文里的正弦绝对位置编码、用 encoder-decoder 双塔、用 `</s>` 当 decoder 起始符,几乎不依赖前面几讲那些为 decoder-only 设计的 RoPE、mRoPE、xdrope。它是一个自包含的翻译小引擎。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [src/nllb_600m.h](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/nllb_600m.h) | `nllb_600m` 对外头文件:`NllbConfig` 配置结构、构造函数、四个 `translate` 重载,用 pImpl 隐藏实现。 |
| [src/nllb_600m.cpp](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/nllb_600m.cpp) | 核心实现:内部类 `Impl`(继承 `ncnn_llm_base`)、`translate_stream` 翻译主链路、encoder/decoder 前向。 |
| [src/ncnn_llm_base.h](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_base.h) | 基类公共能力,本讲重点用其中的正弦位置编码、`sample_logits`、`KVCache` 等。 |
| [examples/nllb_main.cpp](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/examples/nllb_main.cpp) | 示例入口:命令行参数解析、拼装模型文件路径、演示同步与流式两种翻译。 |
| [export/nllb_export.py](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/export/nllb_export.py) | 把 HuggingFace NLLB 模型导出成 ncnn 的 `.param`/`.bin`(细节在 u8-l4,本讲只顺带提及)。 |

> 提醒:NLLB **不走 `model.json`**。它的构造函数直接接收 9 个文件路径,所以 `nllb_main` 在 xmake 里甚至不依赖 `nlohmann_json`(见 [xmake.lua:105-109](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/xmake.lua#L105-L109))。这与 `ncnn_llm_gpt` 系列很不一样。

## 4. 核心概念与源码讲解

### 4.1 nllb_600m 构造与三类子网

#### 4.1.1 概念说明

NLLB-600 是一个标准的 encoder-decoder Transformer,共 24 层 decoder。要跑起来它需要三类 ncnn 子网:

- **embed 子网**:token id → 词嵌入向量(本质是一个查表 `Embed` 层)。encoder 和 decoder **共用**这一个嵌入矩阵(权重共享),所以源语言、目标语言、生成出的 token 都走它。
- **encoder 子网**:把源句子的嵌入序列编码成一串上下文向量(bidirectional self-attention)。
- **decoder 子网**:自回归生成目标语言,每层同时做 self-attention(看已生成的目标 token)和 cross-attention(看 encoder 输出)。

此外还需要一个 **BPE 分词器**(vocab.txt + merges.txt),它的词表里除了普通子词,还有语言 token(`eng_Latn`、`zho_Hans`、…)和特殊符(`</s>`、`<unk>`、`<mask>`)。

`nllb_600m` 类采用 **pImpl 惯用法**(pointer to implementation):对外头文件只暴露一个 `std::unique_ptr<Impl> impl_`,真正的逻辑全在 cpp 里的私有内部类 `Impl` 里。这样做能加快编译、隐藏 ncnn 细节。`Impl` 继承 `ncnn_llm_base`,从而白嫖基类那一整套工具。

#### 4.1.2 核心流程

构造 `Impl` 的顺序:

1. 调基类构造 `ncnn_llm_base(use_vulkan, 4)`,设置 4 线程、可选 Vulkan。
2. 用 `create_option()` 生成 `ncnn::Option`,赋给三张子网。
3. 依次 `load_net` 加载 embed / encoder / decoder 三张网;任一失败则 `ok_=false`。
4. 同时构造 BPE 分词器,注册四个特殊 token(`</s>`、`<unk>`、`<mask>`)。
5. 若一切正常,从词表里查出 `</s>` 的 id,存为 `bos_eos_id_`(默认 2)。NLLB 里 `</s>` 既是结束符也是 decoder 起始符。

#### 4.1.3 源码精读

内部类继承基类并定下 24 层:

[src/nllb_600m.cpp:20](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/nllb_600m.cpp#L20) —— 译码器层数常量。

[src/nllb_600m.cpp:24-35](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/nllb_600m.cpp#L24-L35) —— `Impl` 继承 `ncnn_llm_base(use_vulkan, 4)`,注意 4 线程写死在基类构造里。

构造函数主体,加载三张网并用 BPE 注册特殊 token:

[src/nllb_600m.cpp:44-52](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/nllb_600m.cpp#L44-L52) —— 用 `BpeTokenizer::LoadFromFiles` 加载词表,并显式声明 `</s>`/`<unk>`/`<mask>` 为特殊 token(其中 `bos_token` 和 `eos_token` 都是 `</s>`)。

[src/nllb_600m.cpp:60-70](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/nllb_600m.cpp#L60-L70) —— 依次 `load_net` 三张子网。

[src/nllb_600m.cpp:72-81](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/nllb_600m.cpp#L72-L81) —— 加载成功后,从词表查 `</s>` 的 id;若词表里没有这个 token,直接把 `ok_` 置为 `false`,让整个对象「带病不可用」。

成员默认值 `bos_eos_id_{2}`:

[src/nllb_600m.cpp:273](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/nllb_600m.cpp#L273) —— `</s>` 在 NLLB 词表里的标准 id 就是 2。

> 这里的 `ok_` 健康检查机制来自基类(见 [src/ncnn_llm_base.h:138-145](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_base.h#L138-L145) 的 `load_net`):任何一次加载失败都会把 `ok_` 永久置假,对外通过 `ok()` 查询,后续 `translate` 一律短路返回空。

#### 4.1.4 代码实践

**实践目标**:验证构造流程,看清「加载失败 → `ok()` 为假」的健康检查。

**操作步骤**(源码阅读 + 可选运行):

1. 构建 nllb_main:`xmake build nllb_main`(若尚未装 ncnn master,见 u1-l2)。
2. 阅读上面的构造函数,数清楚它要 9 个文件:embed 的 param/bin、encoder 的 param/bin、decoder 的 param/bin、vocab.txt、merges.txt。注意 encoder/decoder 的文件名带 `noembed` 后缀(见 4.2.3),说明嵌入层被拆到了单独的 embed 子网。
3. (可选)故意把 `--model-dir` 指向一个不存在/不完整的目录,再跑 `xmake run nllb_main --model-dir ./assets/not_exist`,观察控制台打印的 `Failed to load ... model` 报错。

**需要观察的现象**:`nllb_main.cpp` 里 `translator.translate(...)` 在 `ok_` 为假时直接返回空串,因此你会看到 `[Sync] Output:` 后面什么都没有,而不是程序崩溃——这正是「尽早失败但不崩」的设计。

**预期结果**:正常情况下 `ok()` 为真,能产出翻译;缺文件时打印加载失败、输出为空。若你本地没有模型权重,**这步标注「待本地验证」**,转而只做源码阅读。

#### 4.1.5 小练习与答案

**练习 1**:为什么 `Impl` 要继承 `ncnn_llm_base`,而不是自己写一套 `create_option` / `load_net`?

**参考答案**:因为这些是全模态通用的公共能力(见 u2-l1)。继承能复用 Vulkan/线程管理、网络加载、采样、正弦位置编码等,避免重复造轮子。`ncnn_llm_base` 的构造函数是 `protected`,必须继承才能用,NLLB 正是这么做的。

**练习 2**:`bos_eos_id_` 默认值是 2。如果换一个分词器、`</s>` 的 id 变成了别的值,代码会出错吗?

**参考答案**:不会。构造函数在 [nllb_600m.cpp:72-81](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/nllb_600m.cpp#L72-L81) 运行时从词表动态查出真实 id 覆盖默认值;只有当词表里压根没有 `</s>` 时才会把 `ok_` 置假。默认值 2 只是 NLLB 标准词表的一个「占位猜测」。

---

### 4.2 encoder-decoder 翻译主链路与流式 callback

#### 4.2.1 概念说明

这是本讲的核心。一段源语言文本被翻译成目标语言,经过下面这条链:

```
源文本
  → BPE 分词 + 前置源语言 token + 末尾 </s>
  → embed 子网(查表) + 正弦位置编码      [encoder 输入嵌入]
  → encoder 子网(双向 self-attention)    [encoder_output:源句的上下文向量]
  → decoder 预填充 </s> 种子               [初始化 decoder 的 KV cache]
  → 自回归循环:
       embed(上一个 token) + 正弦位置编码
       → decoder 子网(self-attn + cross-attn 看 encoder_output + 更新 KV cache)
       → lm_head → 采样下一个 token
       → 遇 </s> 停止
       → 增量解码,通过 callback 流式吐字
```

两个关键区别于 decoder-only 的点:

1. **encoder 只跑一次**:它把整句源文本一次性编码成 `encoder_output`,之后 decoder 在每一步生成都通过 **cross-attention** 去反复「读」这份输出。所以 `encoder_output` 在整个生成循环里是常量,被反复喂给 decoder 的 `in1`。
2. **KV cache 只在 decoder 侧**:encoder 是一次性前向、无缓存;decoder 像前几讲一样靠 `cache_k%d`/`cache_v%d` 累积历史。

`translate` 对外提供 **4 个重载**,本质是「同步 vs 流式」「默认配置 vs 自定义 `NllbConfig`」两个维度的组合:

| 重载 | 返回 | 配置 | 用途 |
|------|------|------|------|
| `translate(text, src, tgt)` | `string` | 默认 | 最简单,一次性拿完整译文 |
| `translate(text, src, tgt, config)` | `string` | 自定义 | 想调温度/top_k/top_p/采样/max_steps |
| `translate(text, src, tgt, callback)` | `bool` | 默认 | 流式,每生成一点就回调 |
| `translate(text, src, tgt, config, callback)` | `bool` | 自定义 | 流式 + 自定义配置 |

「同步」版本内部其实就是调「流式」版本,把所有 delta 拼成一个字符串返回(见 [nllb_600m.cpp:84-92](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/nllb_600m.cpp#L84-L92) 的 `translate_sync`)。

#### 4.2.2 核心流程

`translate_stream` 的伪代码:

```
若 ok_ 为假 → return false
src_lang_id = 词表[source_lang];  tgt_lang_id = 词表[target_lang]
若任一语言 token 不存在 → return false

input_ids = bpe.encode(文本, add_bos=false, add_eos=true)   # 末尾自动加 </s>
input_ids.insert(开头, src_lang_id)                          # 前置源语言 token
embed_input = embedding_forward(input_ids, pos=-1)           # 查表 + 整段正弦位置编码
encoder_output = encoder_forward(embed_input)                # 一次性编码源句

bos = [bos_eos_id_]                                          # </s> 当 decoder 起始符
bos_embed = embedding_forward(bos, pos=-1)
kv_cache = decoder_prefill(bos_embed, encoder_output)        # 种子 KV cache

last_index = tgt_lang_id                                     # 第一个解码步喂目标语言 token
for pos in 2..max_steps:
    step_embed = embedding_forward([last_index], pos)        # 单 token + 单位置编码
    (logits, kv_cache) = decoder_decode(step_embed, encoder_output, kv_cache)
    last_index = sample_logits(logits, sample_cfg)           # 默认贪心 argmax
    output.push(last_index)
    if last_index == bos_eos_id_: break                      # 遇 </s> 停止
    delta = decode(output) 去掉已输出的前缀
    callback(delta)                                          # 流式吐增量
return true
```

注意三个细节:

- **自回归的「上一个 token」**:`last_index` 每轮被更新为采样结果,下一轮就被 embed 进 decoder。初始值是 `tgt_lang_id`(详见 4.4)。
- **停止条件**:采到 `bos_eos_id_`(`</s>`)就 break。
- **流式增量解码**:BPE 解码可能因后续合并而改变已输出字符串的前缀,所以不能简单地「每步 decode 当前 token」。代码的做法是每步都对**整个 output 序列**重新 decode,再和上次的解码结果比较,只把「新增后缀」喂给 callback。

#### 4.2.3 源码精读

主链路(分三段看):

[src/nllb_600m.cpp:99-118](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/nllb_600m.cpp#L99-L118) —— 健康检查 → 解析语言 id → 分词(末尾加 `</s>`)→ 前置源语言 token → 整段嵌入 → encoder 前向。

[src/nllb_600m.cpp:120-132](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/nllb_600m.cpp#L120-L132) —— 用 `</s>` 预填充 decoder 拿到种子 KV cache;把 `last_index` 设为目标语言 token,准备采样配置。

[src/nllb_600m.cpp:134-157](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/nllb_600m.cpp#L134-L157) —— 自回归主循环:嵌入上一 token → decoder decode(更新 KV cache)→ 采样 → 记录 → 遇 `</s>` 停 → 增量解码并回调。

三个前向函数:

[src/nllb_600m.cpp:185-191](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/nllb_600m.cpp#L185-L191) —— `encoder_forward`:只喂 `in0`(嵌入序列)、取 `out0`,**不传任何 mask**。这正说明 encoder 是双向的(整句互相可见),区别于 decoder 的因果注意力。

[src/nllb_600m.cpp:193-223](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/nllb_600m.cpp#L193-L223) —— `decoder_prefill`:喂 `in0`(hidden)、`in1`(encoder_output,供 cross-attention)、`in2`(因果掩码),并按层号抽取 `out_cache_k%d`/`out_cache_v%d` 组成 KV cache。掩码是 `seq_len×seq_len` 的下三角阵(未来位置填 `-inf` 屏蔽)。

[src/nllb_600m.cpp:225-260](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/nllb_600m.cpp#L225-L260) —— `decoder_decode`:喂新 token 的嵌入(`in0`)、**同一份** `encoder_output`(`in1`)、`1×1` 全零掩码(`in2`,因为单 token 恒为序列末尾,因果性天然满足),并按层号回灌旧的 `cache_k%d`/`cache_v%d`(`in`)、抽取新的 `out_cache_k%d`/`out_cache_v%d`(`out`),最后取 `out0` 作为 logits。注意这里是「读旧 cache、写新 cache」(新的 KV cache 长度 +1)。

四个 `translate` 公开方法:

[src/nllb_600m.cpp:315-345](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/nllb_600m.cpp#L315-L345) —— 四个重载都先检查 `impl_` 和 `impl_->ok()`,然后委托给 `translate_sync`(返回 string)或 `translate_stream`(返回 bool + callback)。

`NllbConfig` 配置结构:

[src/nllb_600m.h:7-13](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/nllb_600m.h#L7-L13) —— `temperature`/`top_k`/`top_p`/`do_sample`/`max_steps`,默认 `do_sample=false`(贪心)、`max_steps=512`。

#### 4.2.4 代码实践

**实践目标**:跑通同步翻译,观察 `encoder→prefill→自回归→</s>停止` 的行为,并与流式输出对比。

**操作步骤**:

1. 准备模型:在 `assets/nllb_600m/` 下放好 9 个文件(权重需自行从 HuggingFace 导出,见 [export/nllb_export.py](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/export/nllb_export.py))。文件名以 [examples/nllb_main.cpp:72-80](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/examples/nllb_main.cpp#L72-L80) 为准:`embed.ncnn.param/bin`、`encoder_noembed.ncnn.param/bin`、`decoder_noembed.ncnn.param/bin`、`vocab.txt`、`merges.txt`。
2. 运行默认例子(英→中):
   ```
   xmake run nllb_main
   ```
   默认文本见 [examples/nllb_main.cpp:16](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/examples/nllb_main.cpp#L16),默认 `--src eng_Latn --tgt zho_Hans`(见 [examples/nllb_main.cpp:13-18](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/examples/nllb_main.cpp#L13-L18))。
3. 观察输出里 `[Sync] Output:`(一次性)与 `[Stream] Output:`(逐字刷新)两段。

**需要观察的现象**:同步版一次性打印整句;流式版的 `Output:` 会随着生成逐渐变长,印证 callback 在每一步被调用。

**预期结果**:得到一句通顺的中文翻译(英→中)。若本地无权重,本步骤标注「待本地验证」,改为纯源码阅读:在 [nllb_600m.cpp:134-157](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/nllb_600m.cpp#L134-L157) 的循环里,逐行标注「这一行在做 embed / decoder / 采样 / 停止判断 / 增量解码」。

#### 4.2.5 小练习与答案

**练习 1**:为什么 `decoder_decode` 每一步都要重新喂 `encoder_output`,而不是像 KV cache 那样缓存起来?

**参考答案**:因为 cross-attention 的 K/V 来自 encoder_output,它本身在整个生成过程中是**常量**。理论上确实可以预先算好 encoder 的 K/V 缓存复用,但本实现的 ncnn decoder 子网把 cross-attention 内部化了(通过 `in1` 接收 encoder_output,由网络内部计算 cross 的 K/V),所以 C++ 侧每步只需把同一份 `encoder_output` 喂进去即可,简单且无歧义。

**练习 2**:`decoder_prefill` 里的掩码是 `seq_len×seq_len` 下三角阵,而 `decoder_decode` 里是 `1×1` 的全零。为什么 decode 阶段不需要因果掩码?

**参考答案**:decode 阶段每次只输入当前这一个 token(它已是序列的最末位),self-attention 只需注意历史 KV cache 中的所有 token 与自己,不存在「未来」可泄露的位置,因果性天然成立,所以掩码退化为单个 0。prefill 阶段一次性输入多个 token,必须用因果掩码屏蔽「未来」位置。

---

### 4.3 正弦位置编码 sinusoidal_positional_embedding

#### 4.3.1 概念说明

位置编码告诉 Transformer「每个 token 处在第几个位置」。前面几讲的 LLM/VLM 用 **RoPE**(旋转位置编码,作用在 query/key 上、只依赖相对位置);NLLB 用的是更古老的 **绝对正弦位置编码**(原始 *Attention is All You Need* 论文):给每个位置算一个固定向量,直接**加**到 token 嵌入上。

为什么 NLLB 用它而不是 RoPE?因为 NLLB 模型当初就是这么训练的——ncnn_llm 在复现时要忠实还原训练时的位置编码方案,否则权重对不上、输出会乱。这也呼应了本讲开头:NLLB 是「最古典」的 Transformer。

基类头里有两个函数:

- `sinusoidal_positional_embedding(seq_len, d_model)`:一次性给整段序列(长度 `seq_len`)算位置编码,返回 `(d_model, seq_len)` 矩阵。用于 encoder 输入和 decoder 的 prefill。
- `sinusoidal_positional_embedding_for_pos(position, d_model)`:只给**单个**位置算一行,返回 `(d_model,)` 向量。用于 decoder 自回归的每一步。

#### 4.3.2 核心流程与数学原理

设模型维度为 \(d\),取 \(d/2\) 个不同频率。先算每个频率的倒数(inv_freq):

\[
\text{inv\_freq}_j = \exp\!\left(-j \cdot \frac{\ln 10000}{d/2}\right),\quad j=0,1,\dots,d/2-1
\]

它等价于 \(1/10000^{2j/d}\):\(j\) 越小频率越高(变化越快)。再对位置 \(p\) 计算角度并取 sin/cos:

\[
\text{PE}(p,\,j) = \sin(p\cdot \text{inv\_freq}_j),\qquad
\text{PE}(p,\,j+d/2) = \cos(p\cdot \text{inv\_freq}_j)
\]

也就是说每个位置的前半维放 sin、后半维放 cos。

⚠️ **注意本实现的「1 起始」约定**:全序列版里 `pos = i + 1`([src/ncnn_llm_base.h:63](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_base.h#L63)),即位置从 1 开始而不是 0。这与 decoder 自回归循环从 `pos=2` 起步是刻意对齐的(详见 4.3.3)。

流程伪代码:

```
half_dim = d_model / 2
for j in 0..half_dim-1:
    inv_freq[j] = exp(-j * ln(10000) / half_dim)
for 每个位置 i:
    pos = i + 1                       # 全序列版;单位置版直接用传入 position
    for j in 0..half_dim-1:
        angle = pos * inv_freq[j]
        emb[i][j]          = sin(angle)
        emb[i][j+half_dim] = cos(angle)
```

#### 4.3.3 源码精读

全序列版:

[src/ncnn_llm_base.h:49-72](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_base.h#L49-L72) —— `sinusoidal_positional_embedding(seq_len, d_model)`,注意第 63 行 `pos = i + 1` 的 1 起始约定,以及前半维 sin、后半维 cos 的填法。

单位置版:

[src/ncnn_llm_base.h:74-97](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_base.h#L74-L97) —— `sinusoidal_positional_embedding_for_pos(position, d_model)`,只算一行;若 `d_model` 为奇数则末位置 0 兜底。

在 NLLB 里被 `embedding_forward` 调用:

[src/nllb_600m.cpp:163-183](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/nllb_600m.cpp#L163-L183) —— embed 子网查表得 `out0` 后,**在 C++ 里**把位置编码加上去(`pos==-1` 走整段版、否则走单位置版)。这说明 embed 子网只负责查表,不含位置信息;位置编码由运行时补上。

位置编号的呼应:

- encoder 输入(整段,`pos=-1`):位置 1, 2, …, N。
- decoder 预填充的 `</s>`(整段版、`seq_len=1`):位置 1。
- decoder 自回归循环从 `pos=2` 开始([src/nllb_600m.cpp:134](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/nllb_600m.cpp#L134)):目标语言 token 落在位置 2,第一个真正生成的词落在位置 3,依此类推。

所以「预填充占用了位置 1」与「循环从 pos=2 起步」是同一套编号体系,不会重复或跳号。

#### 4.3.4 代码实践

**实践目标**:亲手算几行正弦位置编码,直观感受「低维变化快、高维变化慢」。

**操作步骤**(源码阅读 + 可选手算):

1. 阅读上面两个函数的实现,确认 inv_freq 公式与 sin/cos 填法。
2. 设 \(d=8\)(即 `half_dim=4`),手算位置 \(p=1,2,3\) 的前 4 维 sin 值:
   - 先算 `inv_freq[j] = exp(-j * ln(10000)/4)`,j=0,1,2,3。
   - 再算 `sin(p * inv_freq[j])`。
3. 对照观察:j=0 那一维随 p 变化最快(j=0 时 inv_freq=1,sin(p) 周期最短);j=3 那一维几乎不变(inv_freq 极小,接近常数 0)。

**需要观察的现象**:同一列(j 固定)随 p 增大呈正弦波动;j 越大波动越平缓。

**预期结果**:你会得到一张「低维抖动剧烈、高维近乎平直」的位置编码表,这正是 Transformer 用不同频率编码相对/绝对位置的直觉。若不想手算,可写一个 10 行的小程序调用 `sinusoidal_positional_embedding(8, 8)` 打印矩阵(标注「示例代码」)。

#### 4.3.5 小练习与答案

**练习 1**:正弦位置编码和 RoPE(前几讲用的)最根本的区别是什么?

**参考答案**:正弦编码是**绝对**位置编码——给每个绝对位置一个固定向量,直接加到嵌入上,模型靠这些绝对向量间接推断相对关系;RoPE 是**相对**位置编码——作用在 query/key 上,通过对每对维度做旋转,使内积只依赖两个 token 的相对距离,不依赖绝对位置。NLLB 用前者是因为它就是这么训练的。

**练习 2**:为什么需要 `sinusoidal_positional_embedding_for_pos` 这么一个「单位置」版本,不能复用全序列版?

**参考答案**:自回归 decode 阶段每次只输入一个 token,只需算这一个位置的一行编码;若用全序列版得构造一个长度为「已生成总长」的矩阵,既浪费又容易把位置编号搞错。单位置版直接接收 `pos` 返回一行,简洁且与循环里的 `pos` 变量天然对应。

---

### 4.4 源/目标语言 token 与翻译方向

#### 4.4.1 概念说明

NLLB 一个模型支持 200+ 语言对,靠的就是**语言 token**:词表里有一批形如 `eng_Latn`(英语,拉丁字母)、`zho_Hans`(中文,简体)、`fra_Latn`(法语)、`jpn_Jpan`(日语)…的特殊 token。命名遵循「BCP-47 语言码 + 文字脚本」约定。

语言 token 在翻译中扮演两个不同角色:

- **源语言 token**(source_lang):放在 **encoder 输入的最前面**,告诉 encoder「接下来这句是什么语言」。它帮助 encoder 用对应语言的特征去编码。
- **目标语言 token**(target_lang):作为 **decoder 生成的第一个 token**(forced bos / decoder 起始),告诉 decoder「请用这种语言来生成译文」。这正是**控制翻译方向**的开关——同一句源文本、同一个 encoder 输出,只要把 decoder 起始的目标语言 token 换成不同语言,就能译成不同目标语言。

#### 4.4.2 核心流程

1. 从词表查出 `src_lang_id = token_to_id[source_lang]`、`tgt_lang_id = token_to_id[target_lang]`。若任一不存在,直接返回失败(防止拼错语言代码)。
2. encoder 输入 = `[src_lang_id] + bpe.encode(文本, add_eos=true)`,即「源语言 token 在最前、正文 token 在后、`</s>` 在末尾」。
3. decoder 起始符是 `</s>`(用于 prefill 种子 KV cache)。
4. 自回归循环的「上一个 token」初值设为 `tgt_lang_id`——也就是说 decoder 生成的第一步先「吃掉」目标语言 token,再开始产出真正的译文。这就是目标语言如何引导翻译方向。

#### 4.4.3 源码精读

语言 id 解析与失败保护:

[src/nllb_600m.cpp:103-112](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/nllb_600m.cpp#L103-L112) —— 用 `token_to_id().find()` 查两个语言 token;找不到就打印 `Unknown language tokens` 并返回 false。

源语言 token 前置:

[src/nllb_600m.cpp:114-115](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/nllb_600m.cpp#L114-L115) —— `encode(text, false, true)` 第二参 `add_bos=false`、第三参 `add_eos=true`(签名见 [src/utils/tokenizer/bpe_tokenizer.h:23-27](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/utils/tokenizer/bpe_tokenizer.h#L23-L27)),故序列末尾自动带 `</s>`;随后 `insert(begin, src_lang_id)` 把源语言 token 放到最前面。

目标语言 token 作为生成起点:

[src/nllb_600m.cpp:124](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/nllb_600m.cpp#L124) —— `last_index = tgt_lang_id`,自回归循环的第一步会把它 embed 进 decoder,从而把生成导向目标语言。

#### 4.4.4 代码实践

**实践目标**:验证「换目标语言 token = 换翻译方向」,并体会源/目标语言 token 各自的位置。

**操作步骤**:

1. 先跑默认英→中:`xmake run nllb_main`(`--src eng_Latn --tgt zho_Hans`)。
2. 反向译中→英:`xmake run nllb_main --src zho_Hans --tgt eng_Latn --text " ncnn 是最好的边缘端神经网络推理框架"`。
3. 换第三种目标语言(若有相应训练支持),例如 `--tgt fra_Latn` 译成法语。

**需要观察的现象**:同一句输入,只改 `--src`/`--tgt`,encoder/decoder 走的是同一个模型权重,但语言 token 不同,译文语言随之改变。

**预期结果**:英→中产出中文,中→英产出英文。这印证「方向完全由语言 token 决定」。若本地无权重,标注「待本地验证」,改为源码阅读:在 [nllb_600m.cpp:103-124](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/nllb_600m.cpp#L103-L124) 标注「源语言 token 进 encoder 输入、目标语言 token 进 decoder 起点」两条线。

#### 4.4.5 小练习与答案

**练习 1**:如果把 `source_lang` 和 `target_lang` 都填成同一个语言(如都填 `eng_Latn`),会发生什么?

**参考答案**:程序不会报错(两个 token 都存在),encoder 会按英语编码,decoder 起始 token 也是英语,模型大概率做「英语→英语」的近似复述(可能略有改写)。这正好说明语言 token 是**条件信号**而非硬性约束——模型只是被「引导」去用目标语言生成。

**练习 2**:为什么源语言 token 放在 encoder 输入**最前面**,而不是最后面或中间?

**参考答案**:这与 NLLB 的训练约定一致(训练时源语言 token 恒在句首、`</s>` 在句尾)。推理必须复现训练分布,否则位置编码与权重对不上。放在句首也让 encoder 的双向注意力能第一时间「看到」语言标识。

---

## 5. 综合实践

把本讲四个模块串起来,完成一次「读懂 + 跑通 + 解释」的端到端任务。

**任务**:用 `nllb_main` 做一次英→中翻译,并能在源码里逐阶段解释发生了什么。

**步骤**:

1. **构建**:`xmake build nllb_main`(确认 target 名见 [xmake.lua:105-109](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/xmake.lua#L105-L109))。
2. **跑同步翻译**:`xmake run nllb_main --text "Hello, world." --src eng_Latn --tgt zho_Hans`,记录 `[Sync] Output`。
3. **跑流式翻译**:观察 `[Stream] Output` 是否逐字增长,印证 callback 机制。
4. **写一份阶段说明**:对照 [nllb_600m.cpp:99-160](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/nllb_600m.cpp#L99-L160),用表格列出每个阶段对应的代码行与作用:

   | 阶段 | 代码行 | 作用 |
   |------|--------|------|
   | 解析语言 token | L103-112 | 查 src/tgt id,失败保护 |
   | 分词 + 前置源语言 token | L114-115 | encoder 输入序列 |
   | 嵌入 + 正弦位置编码 | L117 | `embedding_forward(pos=-1)` |
   | encoder 前向 | L118 | 一次性得 encoder_output |
   | decoder 种子 prefill | L120-122 | `</s>` 初始化 KV cache |
   | 目标语言起点 | L124 | `last_index = tgt_lang_id` |
   | 自回归循环 | L134-157 | embed→decode→采样→停止→增量解码 |

5. **回答两个解释题**(写进笔记):
   - 正弦位置编码在 encoder 端和 decoder 端分别起什么作用?(答:都把绝对位置信息加到嵌入上,让双方知道 token 处于第几位;encoder 用整段版,decoder 自回归用单位置版。)
   - 源语言 token 和目标语言 token 如何引导翻译方向?(答:源语言 token 在 encoder 输入最前,标识输入语言;目标语言 token 作为 decoder 生成起点,把输出导向目标语言。)

**预期结果**:得到正确译文,并能口头复述 encoder-decoder 的完整数据流。若无权重,步骤 2-3 标注「待本地验证」,但步骤 4-5 的源码解释必须完成。

## 6. 本讲小结

- `nllb_600m` 是项目里唯一的 **encoder-decoder** 运行时,用 pImpl 隐藏实现、内部类继承 `ncnn_llm_base` 复用公共能力。
- 构造时加载 **三类子网**(embed / encoder / decoder)+ BPE 词表,**绕开 `model.json`**、直接吃文件路径;`</s>` 既是 eos 也是 decoder 起始符(id 默认 2)。
- 翻译主链路:分词 → 嵌入+正弦位置编码 → encoder 一次性编码 → decoder 种子 prefill → 自回归循环(embed→decode 更新 KV cache→采样→遇 `</s>` 停止→增量解码)。
- `translate` 提供 **4 个重载**(同步/流式 × 默认/自定义配置),流式版靠「对整个 output 重新 decode、只吐新增后缀」实现稳定的增量输出。
- NLLB 用**正弦绝对位置编码**(基类提供两个函数),而非前几讲的 RoPE;且采用 1 起始的位置编号,与 decoder 循环从 `pos=2` 起步刻意对齐。
- 翻译方向完全由**语言 token** 决定:源语言 token 前置于 encoder 输入,目标语言 token 作为 decoder 生成起点,二者结合即可在 200+ 语言对间切换。

## 7. 下一步学习建议

- **横向对比采样实现**:本讲 NLLB 用的是基类 `sample_logits`(默认贪心,见 [src/ncnn_llm_base.h:147-169](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_base.h#L147-L169))。建议复习 u3-l4,对比它和共享运行时 `llm_select_next_token`(带 repetition penalty)的区别。
- **看导出脚本**:想理解 NLLB 权重如何从 HuggingFace 变成 ncnn 的 `embed`/`encoder_noembed`/`decoder_noembed`,可读 [export/nllb_export.py](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/export/nllb_export.py),对应 u8-l4。
- **打通 KV cache 契约**:本讲 decoder 用的 `cache_k%d`/`cache_v%d` 命名与 u2-l2 的共享运行时完全一致,可回头体会这套「读旧写新」的缓存更新模式如何被多个运行时复用。
- **尝试扩展**:若想加一个新功能(如限制最大生成长度、加 beam search),可参考 `NllbConfig` 的字段,在 `translate_stream` 循环里做实验,这是相对独立、风险小的练手点。
