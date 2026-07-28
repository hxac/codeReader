# ncnn 调用模式与共享文本运行时

## 1. 本讲目标

上一讲（u2-l1）我们读了全模态运行时的公共底座 `ncnn_llm_base.h`，认识了 `KVCache` 类型别名、`create_option`、`load_net` 和基类自带的 `sample_logits`。本讲往下走一层，进入「真正驱动网络前向」的地方。

本讲聚焦 `src/ncnn_text_runtime.h` 与 `src/ncnn_text_runtime.cpp` 这两个文件，它们只暴露 **四个自由函数**，却是整个项目「跨模型族共享 decoder + KV cache 运行时」这一设计主线的具体落地。

学完本讲，你应该能够：

1. 说清 ncnn 的 `Net` / `Extractor` 调用模式：`in0/out0`、`input/extract` 这一组成对出现的约定。
2. 理解 KV cache 在 prefill 与 decode 两个阶段的输入输出张量命名（`cache_k%d` / `cache_v%d` 进，`out_cache_k%d` / `out_cache_v%d` 出），以及 `is_prefill` 开关如何决定「追加」还是「覆盖」。
3. 记住这四个共享函数的签名与职责，并能在 LLM、OCR、ASR 三种模态的源码里一眼认出它们的调用。
4. 区分本项目「两套采样实现」中属于共享运行时的那一套（`llm_select_next_token` + `sampling.cpp`），并理解 repetition penalty 的数学含义。

## 2. 前置知识

- **ncnn 是什么**：一个纯 C++ 的神经网络推理引擎，核心抽象是 `ncnn::Net`（一张网络）和 `ncnn::Extractor`（一次前向推理的「执行器」）。本项目把 `.param`/`.bin` 加载进 `Net`，再用 `Extractor` 喂数据、取结果。u1-l2 已讲过依赖关系，u2-l1 讲过 `load_net`。
- **Extractor 的「插槽」语义**：一次前向可以有很多输入和很多输出。ncnn 用字符串名字来标识这些「插槽」，约定输入从 `in0`、`in1`、`in2`…… 递增，输出从 `out0`、`out1`…… 递增。你也可以用自定义名字（如 `cache_k0`）作为额外插槽。
- **KV cache 直觉**：Transformer 的自注意力每层都会产生一对矩阵 K 和 V。把它们缓存下来，下一步生成新 token 时就不必重算历史——这就是「KV cache」。u2-l1 已经给出过它的类型定义。
- **prefill 与 decode 两阶段**：把用户输入的一长串 prompt 一次性喂进去（prefill，并行处理整段），产出首 token 与第一批 KV cache；之后每生成一个 token 只喂 1 个 token 进去（decode），复用并追加 KV cache。本讲的四个函数同时服务于这两个阶段。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
| --- | --- |
| [src/ncnn_text_runtime.h](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_text_runtime.h) | 声明 `LlmTokenSampleConfig` 与四个共享函数的原型。 |
| [src/ncnn_text_runtime.cpp](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_text_runtime.cpp) | 四个函数的实现，是本讲精读的核心。 |
| [src/ncnn_llm_base.h](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_base.h) | 提供 `KVCache` 类型别名（第 14 行）与 `argmax1d` 等工具，被这四个函数直接依赖。 |
| [src/sampling.h](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/sampling.h) / [src/sampling.cpp](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/sampling.cpp) | 采样工具函数（`softmax_vec` / `apply_top_k` / `apply_top_p` / `sample_from_probs`），被 `llm_select_next_token` 调用。 |
| [src/ncnn_llm_gpt.cpp](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp) | LLM 的 generate 循环调用这四个函数，是「调用方」代表。 |
| [src/ncnn_llm_ocr.cpp](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_ocr.cpp) / [src/ncnn_llm_asr.cpp](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_asr.cpp) | OCR 与 ASR 运行时**复用**这同一套函数，是「跨模态共享」的证据。 |

## 4. 核心概念与源码讲解

### 4.1 llm_run_text_embed：token id → embedding 与 ncnn 调用模式

#### 4.1.1 概念说明

文本进入模型的第一步，是把每个整数 token id 映射成一个稠密向量（embedding）。这件事由一张独立的 `embed_net` 完成（在 model.json 里对应 `params` 块的 `embed_token_*` 字段，见 u1-l5）。

这个函数是四个共享函数里**最简单**的一个，因此也是理解 ncnn `Extractor` 调用模式的最佳入口。掌握它的「三步走」之后，后面三个函数只是「插槽更多」而已。

#### 4.1.2 核心流程

一次 ncnn 前向推理的标准三步：

1. **建执行器**：`Extractor ex = net.create_extractor();` —— 从 `Net` 派生一次独立的前向上下文。
2. **喂数据**：`ex.input("in0", input_mat);` —— 把输入张量塞进名为 `in0` 的输入插槽。
3. **取结果**：`ex.extract("out0", output_mat);` —— 从名为 `out0` 的输出插槽把结果取出来。

> `Extractor` 是一次性的：每次前向都要新建一个。这也是为什么你在源码里会反复看到 `create_extractor()`。

`llm_run_text_embed` 有两个重载：一个接收一串 id（prefill 用），一个接收单个 id（decode 用）。

#### 4.1.3 源码精读

先看头文件里的声明，有两个重载：

```cpp
ncnn::Mat llm_run_text_embed(ncnn::Net& embed_net, const std::vector<int>& input_ids);
ncnn::Mat llm_run_text_embed(ncnn::Net& embed_net, int token_id);
```

详见 [src/ncnn_text_runtime.h:20-21](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_text_runtime.h#L20-L21)（声明了两个重载），第 23-30 行的另外两个声明是 decoder 与 lm_head，本讲后面讲。

「一串 id」重载的实现，把 vector 包成 `ncnn::Mat` 后走标准三步：

```cpp
ncnn::Mat llm_run_text_embed(ncnn::Net& embed_net, const std::vector<int>& input_ids) {
    ncnn::Mat input_ids_mat((int)input_ids.size(), 1, (void*)input_ids.data());
    input_ids_mat = input_ids_mat.clone();   // 关键：拷贝出独立内存

    ncnn::Mat token_embed;
    ncnn::Extractor ex = embed_net.create_extractor();
    ex.input("in0", input_ids_mat);          // 喂进 in0
    ex.extract("out0", token_embed);         // 从 out0 取
    return token_embed;
}
```

详见 [src/ncnn_text_runtime.cpp:12-21](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_text_runtime.cpp#L12-L21)。

两个要点：

- `ncnn::Mat(宽度, 高度, 外部指针)` 这种构造方式**不拷贝数据**，只是套一层壳指向 `input_ids.data()`。这里紧接着 `.clone()` 是为了把数据复制成 ncnn 自己管理的连续内存，避免后续 `input_ids` 离开作用域后悬空。这是本项目里反复出现的「外部指针 + clone」模式。
- 输入插槽名是字符串字面量 `"in0"`、输出是 `"out0"`。这两个名字不是随意的，是 ncnn 的默认约定，导出脚本必须保证 `.param` 里第一层算子的输入输出就叫这两个名字。

「单个 id」重载几乎一模一样，只是把向量换成单个 int，见 [src/ncnn_text_runtime.cpp:23-32](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_text_runtime.cpp#L23-L32)。

#### 4.1.4 代码实践

**实践目标**：在真实调用方里确认「`in0`/`out0` 三步走」与 `.clone()` 的存在。

**操作步骤**：

1. 打开 [src/ncnn_llm_ocr.cpp:511](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_ocr.cpp#L511)，这是 OCR decode 阶段调用单 id 重载的地方：`ncnn::Mat cur_embed = llm_run_text_embed(*text_embed_net_, ctx->cur_token);`。
2. 再看 [src/ncnn_llm_asr.cpp:232](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_asr.cpp#L232)，这是 ASR 调用「一串 id」重载的地方。

**需要观察的现象**：三种模态（LLM/OCR/ASR）调用的是**同一个函数**，只是传进去的 `Net` 对象不同。

**预期结果**：你会确认「token id → embedding」这一步在整个项目里只有这一份实现，没有任何模态各自重写。这正是「共享运行时」的字面含义。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `llm_run_text_embed` 在构造 `input_ids_mat` 之后还要 `.clone()` 一次？直接用原 Mat 会怎样？

> **参考答案**：`ncnn::Mat(size, 1, (void*)ptr)` 这种构造只是把外部指针包成 Mat，不拥有数据。若不 clone，一旦 `input_ids` 在函数返回后被销毁，返回的 `token_embed` 所依赖的输入内存就可能失效。`.clone()` 强制 ncnn 分配并拷贝一块独立内存，保证前向推理使用的数据生命周期安全。

**练习 2**：导出脚本导出的 embed 网络，其 `.param` 第一层算子的输入输出为什么必须叫 `in0`/`out0`？

> **参考答案**：因为 `llm_run_text_embed` 用 `ex.input("in0", ...)` 与 `ex.extract("out0", ...)` 按名字绑定插槽。ncnn 按字符串名匹配，名字对不上就喂不进数据或取不出结果。`in0/out0` 是 ncnn 默认约定，四个共享函数全部沿用。

---

### 4.2 llm_run_decoder_with_kv：decoder 前向与 KV cache 命名约定（核心）

#### 4.2.1 概念说明

这是四个函数里**最重要、也最复杂**的一个，是整个共享运行时的心脏。它做三件事：

1. 把 embedding 序列、注意力 mask、RoPE 的 cos/sin cache 喂进 decoder 网络；
2. 按 `is_prefill` 决定是否把**已有的** KV cache 作为输入喂进去；
3. 从 decoder 取出更新后的 KV cache（写回引用参数 `kv_cache`）以及 hidden states（返回值）。

理解它的关键，是 ncnn 里 KV cache 张量的**命名约定**。decoder 网络的 `.param` 为每一层注意力都开了一对输入插槽和一对输出插槽，名字按层号 `i` 拼接：

- 输入：`cache_k0` / `cache_v0`、`cache_k1` / `cache_v1`、……、`cache_k{attn_cnt-1}` / `cache_v{attn_cnt-1}`
- 输出：`out_cache_k0` / `out_cache_v0`、……、`out_cache_k{attn_cnt-1}` / `out_cache_v{attn_cnt-1}`

其中 `attn_cnt` 就是 Transformer 的层数（来自 model.json 的 `setting.attn_cnt`，见 u1-l5）。这个命名规则是**导出脚本（u8-l4）和运行时之间的一份隐式契约**——脚本怎么命名，运行时就怎么读写。

#### 4.2.2 核心流程

```
建执行器 ex
ex.input("in0", embeds)      # embedding 序列
ex.input("in1", mask)        # 因果/注意力 mask
ex.input("in2", cos_cache)   # RoPE cos
ex.input("in3", sin_cache)   # RoPE sin

if 不是 prefill（即 decode 阶段）:
    for 每一层 i in [0, attn_cnt):
        ex.input("cache_k{i}", kv_cache[i].first)   # 喂入旧 K
        ex.input("cache_v{i}", kv_cache[i].second)  # 喂入旧 V

for 每一层 i in [0, attn_cnt):
    ex.extract("out_cache_k{i}", k_cache)  # 取出新 K
    ex.extract("out_cache_v{i}", v_cache)  # 取出新 V
    if 是 prefill:
        kv_cache.emplace_back(k, v)        # 从空开始：追加
    else:
        kv_cache[i] = (k, v)               # 覆盖旧值

ex.extract("out0", decode_out)  # 取出 hidden states
return decode_out
```

`is_prefill` 这个布尔开关决定了写回方式，这点非常关键：

- **prefill 阶段**：调用方传进来的 `kv_cache` 通常是**空的**（刚构造），所以用 `emplace_back` 一层层追加，调用结束后 `kv_cache.size() == attn_cnt`。
- **decode 阶段**：`kv_cache` 已经有 `attn_cnt` 层了，每层用新的 K/V **原地覆盖**（`kv_cache[i] = ...`）。注意此时还把旧 cache 作为输入喂了回去（见上面 `if (!is_prefill)` 分支），decoder 内部把新 token 的 K/V 追加到旧 cache 上，输出合并后的完整 cache。

> 为什么 prefill 阶段不喂入 cache？因为 prefill 时还没有任何历史 KV cache，decoder 一次性处理整段 prompt，直接产出每一层的初始 cache。decode 阶段才有「历史」需要喂回去。

#### 4.2.3 源码精读

先看签名，七个参数里前四个是输入张量，第五个 `KVCache&` 是**既输入又输出**，后两个控制层数与模式：

```cpp
ncnn::Mat llm_run_decoder_with_kv(ncnn::Net& decoder_net,
                                  const ncnn::Mat& embeds,
                                  const ncnn::Mat& mask,
                                  const ncnn::Mat& cos_cache,
                                  const ncnn::Mat& sin_cache,
                                  KVCache& kv_cache,
                                  int attn_cnt,
                                  bool is_prefill);
```

见 [src/ncnn_text_runtime.h:23-30](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_text_runtime.h#L23-L30)。其中 `KVCache` 是 u2-l1 讲过的类型别名 `std::vector<std::pair<ncnn::Mat, ncnn::Mat>>`（见 [src/ncnn_llm_base.h:14](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_base.h#L14)）。

实现里，先用 `snprintf` 按层号拼出名字字符串，再 `input`/`extract`。这是整个函数最值得记住的模式：

```cpp
ncnn::Extractor ex = decoder_net.create_extractor();
ex.input("in0", embeds);
ex.input("in1", mask);
ex.input("in2", cos_cache);
ex.input("in3", sin_cache);

if (!is_prefill) {
    for (int i = 0; i < attn_cnt; i++) {
        char name_k_in[16], name_v_in[16];
        std::snprintf(name_k_in, sizeof(name_k_in), "cache_k%d", i);
        std::snprintf(name_v_in, sizeof(name_v_in), "cache_v%d", i);
        ex.input(name_k_in, kv_cache[i].first);
        ex.input(name_v_in, kv_cache[i].second);
    }
}
```

见 [src/ncnn_text_runtime.cpp:43-57](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_text_runtime.cpp#L43-L57)。这段把 `in0~in3` 四个常规输入喂进去；decode 阶段额外喂入每层的旧 KV cache。

接着是输出端，按 `is_prefill` 决定写回方式：

```cpp
for (int i = 0; i < attn_cnt; i++) {
    char name_k_out[32], name_v_out[32];
    std::snprintf(name_k_out, sizeof(name_k_out), "out_cache_k%d", i);
    std::snprintf(name_v_out, sizeof(name_v_out), "out_cache_v%d", i);
    ncnn::Mat k_cache, v_cache;
    ex.extract(name_k_out, k_cache);
    ex.extract(name_v_out, v_cache);
    if (is_prefill) {
        kv_cache.emplace_back(std::move(k_cache), std::move(v_cache));  // 追加
    } else {
        kv_cache[i] = std::make_pair(std::move(k_cache), std::move(v_cache));  // 覆盖
    }
}
ex.extract("out0", decode_out);
return decode_out;
```

见 [src/ncnn_text_runtime.cpp:59-74](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_text_runtime.cpp#L59-L74)。注意两点：

1. 用了 `std::move` 把 K/V 所有权移进 `kv_cache`，避免拷贝整张矩阵。
2. hidden states 从默认的 `out0` 取出，作为返回值。

**真实调用方对照**——OCR 的 prefill 是 `is_prefill=true` 的典型用法，传一个**空** `KVCache` 进去：

```cpp
KVCache kv_cache;
ncnn::Mat decode_out = llm_run_decoder_with_kv(*text_decoder_net_, token_embed, mask, cos_cache, sin_cache,
                                               kv_cache, attn_cnt_, true);
```

见 [src/ncnn_llm_ocr.cpp:446-448](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_ocr.cpp#L446-L448)。调用结束后 `kv_cache` 被填满 `attn_cnt_` 层，随后被搬进 ctx 保存（第 459 行 `ctx->kv_cache = std::move(kv_cache);`）。

而 OCR 的 generate 循环是 `is_prefill=false` 的典型用法，把 ctx 里已有的 cache 喂回去、原地更新：

```cpp
ncnn::Mat decode_out = llm_run_decoder_with_kv(*text_decoder_net_, cur_embed, mask, cos_cache, sin_cache,
                                               ctx->kv_cache, attn_cnt_, false);
```

见 [src/ncnn_llm_ocr.cpp:522-523](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_ocr.cpp#L522-L523)。

> **Qwen3.5 混合架构的例外**：当模型带 `sconv_cnt`/`gdr_cnt`（自定义算子 ShortConv/GDR，见 u7-l4）时，generate 循环**不调用** `llm_run_decoder_with_kv`，而是把同样的逻辑内联展开，额外多喂/多取 `cache_conv%d` 与 `cache_gdr%d`。详见 [src/ncnn_llm_gpt.cpp:913-968](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L913-L968)。普通 Transformer 走共享函数，混合架构走内联——这是目前共享函数唯一的「特例」。

#### 4.2.4 代码实践

**实践目标**：亲手验证 prefill 与 decode 两个阶段 `kv_cache` 的大小与写回方式。

**操作步骤（源码阅读型）**：

1. 在 [src/ncnn_text_runtime.cpp:59-71](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_text_runtime.cpp#L59-L71) 的循环里，在 `if (is_prefill)` 与 `else` 两个分支各加一行临时日志，例如 `fprintf(stderr, "prefill emplace layer %d\n", i);` 与 `fprintf(stderr, "decode overwrite layer %d\n", i);`（**仅用于本地观察，验证后请还原**）。
2. 运行任意一个示例（如 `xmake run llm_ncnn_run assets/某模型`，参考 u1-l2）。

**需要观察的现象**：

- prefill 阶段日志以 `emplace` 出现 `attn_cnt` 次，且此时 `kv_cache` 从 0 增长到 `attn_cnt`。
- 之后每生成一个 token，`decode overwrite` 出现 `attn_cnt` 次，`kv_cache` 大小始终是 `attn_cnt`。

**预期结果**：直观看到「prefill 追加、decode 覆盖」的区别。若本地没有模型权重无法运行，**待本地验证**，可改为纯阅读：对照 [src/ncnn_text_runtime.cpp:66-70](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_text_runtime.cpp#L66-L70) 口述两个分支的区别即可。

#### 4.2.5 小练习与答案

**练习 1**：如果模型的 `setting.attn_cnt` 写成了 24，但 decoder 网络实际只有 28 层，调用 `llm_run_decoder_with_kv` 会发生什么？

> **参考答案**：函数会按 `attn_cnt=24` 循环，只读写 `cache_k0~cache_k23` 与 `out_cache_k0~out_cache_k23`。剩下 4 层的 cache 既不会被喂入也不会被取出，等价于这 4 层每步都「失忆」（无历史 KV），模型行为会出错。`attn_cnt` 必须严格等于网络真实层数，这是 model.json 里少数几个「填错就会静默出错」的字段。

**练习 2**：为什么 decode 阶段要把旧 `kv_cache` 作为输入喂回去，而 prefill 阶段不用？

> **参考答案**：decode 阶段每次只处理 1 个新 token，但注意力需要看到**全部历史** token 的 K/V，所以必须把历史 KV cache 喂回 decoder，由网络内部把新 token 的 K/V 追加到历史末尾，再输出合并后的完整 cache。prefill 阶段一次性处理整段 prompt，还没有历史，decoder 直接从 prompt 计算出初始 cache，所以不需要喂入。

**练习 3**：`std::move(k_cache)` 在这里的作用是什么？去掉 `std::move` 会怎样？

> **参考答案**：`std::move` 把 K/V 矩阵的所有权「移动」进 `kv_cache`，避免拷贝整块矩阵内存。去掉 `std::move` 会退化为拷贝构造，功能正确但每层多一次大矩阵拷贝，decode 每步都要付这个开销，性能下降。

---

### 4.3 llm_run_lm_head：hidden states → logits

#### 4.3.1 概念说明

decoder 输出的 hidden states 还不是最终的 token 概率。需要再用一张 `lm_head_net`（在 model.json 里对应 `proj_out_*` 字段，见 u1-l5）把 hidden states 投影到词表维度，得到每个候选 token 的原始分数（logits）。这一步通常就是一个矩阵乘法 + 可选 bias，逻辑很简单。

#### 4.3.2 核心流程

标准三步：建执行器 → 喂 `in0` → 取 `out0`。和 `llm_run_text_embed` 是同一套模式，只是网络不同、数据流向相反（embedding 是 id→向量，lm_head 是向量→词表分数）。

#### 4.3.3 源码精读

```cpp
ncnn::Mat llm_run_lm_head(ncnn::Net& lm_head_net, const ncnn::Mat& hidden_states) {
    ncnn::Mat logits;
    ncnn::Extractor ex = lm_head_net.create_extractor();
    ex.input("in0", hidden_states);
    ex.extract("out0", logits);
    return logits;
}
```

见 [src/ncnn_text_runtime.cpp:77-83](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_text_runtime.cpp#L77-L83)。声明在 [src/ncnn_text_runtime.h:32](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_text_runtime.h#L32)。

一个**重要的调用方细节**：lm_head 通常只需要**最后一个 token 位置**的 hidden state（因为自回归生成只关心「下一个 token」）。所以调用方往往先从 decoder 输出里切出最后一行再喂给 lm_head。例如 OCR prefill 里：

```cpp
ncnn::Mat last_hidden = decode_out.row_range(seq_len - 1, 1);  // 只取最后一行
ncnn::Mat logits = llm_run_lm_head(*lm_head_net_, last_hidden);
```

见 [src/ncnn_llm_ocr.cpp:451-452](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_ocr.cpp#L451-L452)。而 decode 阶段因为只输入了 1 个 token，decoder 输出本来就只有一行，直接整块喂进去即可（见 [src/ncnn_llm_ocr.cpp:525](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_ocr.cpp#L525)）。

#### 4.3.4 代码实践

**实践目标**：理解「切最后一行」在 prefill 与 decode 两种场景下的差异。

**操作步骤**：

1. 对比 OCR prefill（[src/ncnn_llm_ocr.cpp:451-452](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_ocr.cpp#L451-L452)，用 `row_range` 切最后一行）与 OCR decode（[src/ncnn_llm_ocr.cpp:525](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_ocr.cpp#L525)，直接喂整块）两处对 `llm_run_lm_head` 的调用。
2. 思考：decode 阶段为什么不也需要 `row_range`？

**需要观察的现象 / 预期结果**：prefill 一次输入了 `seq_len` 个 token，decoder 输出有 `seq_len` 行，但下一个 token 只取决于最后一行，所以必须切；decode 一次只输入 1 个 token，输出只有 1 行，无需切。如果本地有 ncnn 模型可用，可自行打印 `decode_out.h`（行数）来验证，**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：`llm_run_lm_head` 的输入 hidden states 形状是 `(hidden_dim, seq_len)`，输出 logits 形状是什么？

> **参考答案**：`(vocab_size, seq_len)`——每个位置都得到一组覆盖整个词表的分数。但生成时通常只取最后一行（最后一个位置）的 `vocab_size` 个分数用于选下一个 token。

**练习 2**：为什么 lm_head 在本项目里被单独做成一张网络（`proj_out_net`），而不是和 decoder 合在一起？

> **参考答案**：拆分后，decoder 网络专注于「带 KV cache 的 Transformer 层」，lm_head 专注于「投影到词表」。这样 decoder 的输出可以被复用（比如某些模型还要拿 hidden states 做别的事），也方便不同模型族复用同一套调用骨架。更重要的是，它让 KV cache 的输入输出插槽只出现在 decoder 网络里，命名约定不会被 lm_head 干扰。

---

### 4.4 llm_select_next_token：logits → token id（采样 + repetition penalty）

#### 4.4.1 概念说明

拿到 logits（一组覆盖词表的原始分数）后，要决定「下一个 token 是哪个」。这是采样策略发挥作用的地方。

u2-l1 已经指出本项目有**两套采样实现**：

- **基类私有版** `ncnn_llm_base::sample_logits`（[src/ncnn_llm_base.h:147](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_base.h#L147)）：服务 NLLB 这类自己跑解码循环、且**不需要** repetition penalty 的运行时；用 `SampleConfig`，不支持重复惩罚。
- **共享运行时版** `llm_select_next_token`（本节）：服务 LLM/OCR/ASR 这些用共享 decoder 的运行时；用 `LlmTokenSampleConfig`，**额外支持 repetition penalty**，并把采样算子委托给 `sampling.cpp` 的自由函数。

本节讲的是后者。它多了一个 `history` 参数（已经生成过的 token 集合），用来做重复惩罚。

#### 4.4.2 核心流程

```
1. 把 logits 的前 vocab_size 个 float 拷贝到 scores 向量
2. repetition penalty：
   for 每个 t in history:
       if scores[t] < 0:  scores[t] *= repetition_penalty   # 负的更负
       else:              scores[t] /= repetition_penalty   # 正的变小
3. if 不采样 或 temperature <= 0:
       return argmax(scores)             # 贪心
4. softmax(scores, temperature)          # 带温度归一化为概率
5. if top_k > 0: apply_top_k(scores)
   if top_p < 1.0: apply_top_p(scores)
6. if 概率和非法（非有限或 <=0）: return argmax(scores)   # 兜底
7. return sample_from_probs(scores)      # 按概率离散抽样
```

**repetition penalty 的数学含义**：设惩罚系数 \(\rho\) = `repetition_penalty`，对历史中出现过的 token \(t\)，其分数 \(s_t\) 调整为：

\[
s'_t = \begin{cases} s_t \cdot \rho & \text{若 } s_t < 0 \\ s_t \,/\, \rho & \text{若 } s_t \ge 0 \end{cases}
\]

当 \(\rho > 1\) 时：负分数乘以 \(\rho\) 变得更负，正分数除以 \(\rho\) 变得更小——两种情况都会**降低**该 token 被 softmax 选中的概率，从而抑制重复。当 \(\rho = 1\) 时不惩罚。注意它是施加在 softmax **之前**的原始分数上的，而不是概率上。

**带温度 softmax**（数值稳定形式，\(T\) 为温度）：

\[
p_i = \frac{\exp((z_i - z_{\max})/T)}{\sum_j \exp((z_j - z_{\max})/T)}
\]

温度 \(T\) 越大分布越平（更随机），\(T \to 0\) 退化为贪心。

#### 4.4.3 源码精读

采样配置结构（多了 `vocab_size` 与 `repetition_penalty`，这是和基类 `SampleConfig` 的关键区别）：

```cpp
struct LlmTokenSampleConfig {
    int vocab_size = 0;
    float temperature = 1.0f;
    float top_p = 1.0f;
    int top_k = 0;
    float repetition_penalty = 1.0f;
    int do_sample = 0;
};
```

见 [src/ncnn_text_runtime.h:11-18](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_text_runtime.h#L11-L18)。

函数实现，先拷贝 logits 再做 repetition penalty：

```cpp
const int vocab_size = cfg.vocab_size > 0 ? cfg.vocab_size : logits.w;
std::vector<float> scores(vocab_size);
std::memcpy(scores.data(), logits.data, sizeof(float) * vocab_size);

for (int t : history) {
    if (t < 0 || t >= vocab_size) continue;
    if (scores[t] < 0) {
        scores[t] *= cfg.repetition_penalty;   // 负的乘
    } else {
        scores[t] /= cfg.repetition_penalty;   // 正的除
    }
}
```

见 [src/ncnn_text_runtime.cpp:88-99](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_text_runtime.cpp#L88-L99)。注意 `vocab_size` 可由配置显式指定（否则取 `logits.w`），这允许 logits 矩阵实际宽度大于词表时的截断。

随后是「贪心 or 采样」分流，采样路径委托给 `sampling.cpp`：

```cpp
if (cfg.do_sample != 1 || cfg.temperature <= 0.0f) {
    return (int)(std::max_element(scores.begin(), scores.end()) - scores.begin());  // 贪心
}

softmax_vec(scores, cfg.temperature);
if (cfg.top_k > 0) apply_top_k(scores, cfg.top_k);
if (cfg.top_p < 1.0f) apply_top_p(scores, cfg.top_p);

const float sum = std::accumulate(scores.begin(), scores.end(), 0.0f);
if (!std::isfinite(sum) || sum <= 0.0f) {
    return (int)(std::max_element(scores.begin(), scores.end()) - scores.begin());  // 兜底贪心
}
return sample_from_probs(scores);
```

见 [src/ncnn_text_runtime.cpp:101-114](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_text_runtime.cpp#L101-L114)。

被调用的 `softmax_vec` / `apply_top_k` / `apply_top_p` / `sample_from_probs` 都在 [src/sampling.cpp](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/sampling.cpp) 里。其中 `sample_from_probs` 用 `std::discrete_distribution` 按概率做离散抽样：

```cpp
int sample_from_probs(const std::vector<float>& probs) {
    std::discrete_distribution<int> dist(probs.begin(), probs.end());
    return dist(rng);
}
```

见 [src/sampling.cpp:52-55](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/sampling.cpp#L52-L55)。注意它用的是 `sampling.cpp` 文件作用域内的静态 `rng`（[src/sampling.cpp:5](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/sampling.cpp#L5)），和基类成员 `rng_` 是两个不同的随机源——这是「两套实现」的又一处体现。

**真实调用方**：LLM 的 generate 循环先填好 `LlmTokenSampleConfig`，再调用本函数：

```cpp
LlmTokenSampleConfig sample_cfg;
sample_cfg.vocab_size = vocab_size;
sample_cfg.temperature = cfg.temperature;
sample_cfg.top_p = cfg.top_p;
sample_cfg.top_k = cfg.top_k;
sample_cfg.repetition_penalty = cfg.repetition_penalty;
sample_cfg.do_sample = cfg.do_sample;
int next_id = llm_select_next_token(logits_mat, history, sample_cfg);
```

见 [src/ncnn_llm_gpt.cpp:972-979](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_gpt.cpp#L972-L979)。OCR 里调用方式完全一致（[src/ncnn_llm_ocr.cpp:527-534](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_ocr.cpp#L527-L534)），再次印证共享。

#### 4.4.4 代码实践

**实践目标**：用一小段构造的 logits 复现「贪心 vs 采样」与「repetition penalty」的效果，不依赖任何模型。

**操作步骤**：下面是一段**示例代码**（非项目原有文件，可自行放入一个临时 `.cpp` 编译运行），它直接调用 `llm_select_next_token` 验证行为：

```cpp
// 示例代码：验证 llm_select_next_token 的采样与重复惩罚行为
#include <cstdio>
#include <ncnn/mat.h>
#include "ncnn_text_runtime.h"

int main() {
    // 构造一个 5 词的假 logits：token 2 分数最高
    const int V = 5;
    float buf[V] = {0.1f, 0.2f, 3.0f, 0.4f, 0.5f};
    ncnn::Mat logits(V, 1, (void*)buf);
    logits = logits.clone();

    LlmTokenSampleConfig cfg;
    cfg.vocab_size = V;

    // 1) 贪心（do_sample=0）：必返回 argmax = 2
    cfg.do_sample = 0;
    std::unordered_set<int> empty_hist;
    int greedy = llm_select_next_token(logits, empty_hist, cfg);
    std::printf("greedy -> %d (expect 2)\n", greedy);

    // 2) repetition penalty：把 token 2 放进 history，rho=2.0
    cfg.repetition_penalty = 2.0f;
    std::unordered_set<int> hist = {2};
    // token 2 分数 3.0 >= 0，除以 2 -> 1.5，仍可能是最大；可把 rho 调大到 5 观察
    int penalized = llm_select_next_token(logits, hist, cfg);
    std::printf("after penalty rho=2.0 -> %d\n", penalized);

    return 0;
}
```

**需要观察的现象**：

- 贪心模式恒定返回 `2`。
- `repetition_penalty` 调大到 `5.0` 后，token 2 的分数 `3.0/5 = 0.6` 被压到比 token 4 的 `0.5` 略高，仍可能选 2；若把 `buf[4]` 改成 `0.7`，惩罚后 argmax 会跳到 `4`。

**预期结果**：直观看到 repetition penalty 对「正分数」做除法、压低其被选概率。**待本地验证**（需要把 `ncnn_text_runtime.cpp` 与 `sampling.cpp` 一起编译链接，参考 u1-l2 的 target 结构；最简单是临时把本文件加进 `test_llm` target 的源码列表里跑）。

#### 4.4.5 小练习与答案

**练习 1**：`llm_select_next_token` 与基类 `sample_logits` 最关键的两个区别是什么？

> **参考答案**：(1) 本函数支持 **repetition penalty**——多一个 `history` 参数，在 softmax 前对历史 token 的分数做「负乘正除」调整；基类版没有这个能力。(2) 本函数把采样算子委托给 `sampling.cpp` 的自由函数（用文件级静态 `rng`），基类版则把采样实现内联在类里、用成员 `rng_`。

**练习 2**：为什么 repetition penalty 对负分数用乘法、对非负分数用除法，而不是统一用一种？

> **参考答案**：为了保证「无论分数正负，惩罚都让它更不可能被选中」。对正分数，除以 \(\rho>1\) 让它变小；对负分数，乘以 \(\rho>1\) 让它更负（更小）。如果统一用除法，负分数除以 \(\rho>1\) 反而会变大（更接近 0），就起不到惩罚作用了。

**练习 3**：函数末尾的 `if (!std::isfinite(sum) || sum <= 0.0f)` 兜底分支，会在什么情况下触发？

> **参考答案**：当 softmax 后概率和出现非有限值（NaN/Inf，可能源于数值溢出或极端温度）或全为 0（被 top_k/top_p 全部截断的退化情况）时触发，此时退回贪心 argmax，保证函数始终返回一个合法 token id，不会因为采样数值异常而崩溃。

---

## 5. 综合实践

把本讲四个函数串成一条完整的「token id → logits → token id」推理链。这是后续 u2-l3（prefill）与 u2-l4（generate）要做的事的最小骨架，本讲先把它跑通。

**实践目标**：用 `llm_run_text_embed` → `llm_run_decoder_with_kv` → `llm_run_lm_head` → `llm_select_next_token` 串联一次 prefill 推理，打印出 argmax 得到的下一个 token id，并正确处理 `attn_cnt` 与 `is_prefill`。

**操作步骤**：

1. **准备模型目录**：在 `assets/` 下放好一个文本模型目录（含 model.json 与 embed/decoder/proj_out 的 `.param`/`.bin`），参考 u1-l5。从 model.json 读出 `setting.attn_cnt`、`rope.rope_head_dim`、`rope.rope_theta`。
2. **加载三张网络**：参考 u2-l1 的 `load_net` 与 `create_option`，分别加载 embed_net、decoder_net、proj_out_net。
3. **构造输入**：取一串 token id（可先用分词器 encode 一句话，分词器在 u3 讲，这里也可硬编码几个 id 做最小验证）。
4. **按 prefill 链路调用**（下面是**示例代码**骨架，对照 [src/ncnn_llm_ocr.cpp:446-455](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_ocr.cpp#L446-L455) 的真实写法）：

   ```cpp
   // 示例代码：四函数串联的最小 prefill 骨架
   std::vector<int> token_ids = { /* 你的 token id 序列 */ };
   int attn_cnt = /* model.json 的 setting.attn_cnt */;

   // 1) id -> embedding
   ncnn::Mat token_embed = llm_run_text_embed(*embed_net, token_ids);

   // 2) RoPE cos/sin（基础版，变体见 u4）
   ncnn::Mat cos_cache, sin_cache;
   generate_rope_embed_cache((int)token_ids.size(), rope_head_dim, 0,
                             cos_cache, sin_cache, rope_theta);

   // 3) 因果 mask（上三角填 -1e38）
   int n = (int)token_ids.size();
   ncnn::Mat mask(n, n); mask.fill(0.0f);
   for (int i = 0; i < n; i++)
       for (int j = i + 1; j < n; j++)
           mask.row(i)[j] = -1e38f;

   // 4) decoder（prefill：传空 KVCache，is_prefill=true）
   KVCache kv_cache;
   ncnn::Mat decode_out = llm_run_decoder_with_kv(*decoder_net, token_embed,
                                                  mask, cos_cache, sin_cache,
                                                  kv_cache, attn_cnt, true);
   // 5) 取最后一个位置 -> lm_head -> logits
   ncnn::Mat last_hidden = decode_out.row_range(n - 1, 1);
   ncnn::Mat logits = llm_run_lm_head(*proj_out_net, last_hidden);

   // 6) 贪心选下一个 token
   LlmTokenSampleConfig cfg; cfg.vocab_size = vocab_size; cfg.do_sample = 0;
   int next_id = llm_select_next_token(logits, {}, cfg);
   std::printf("next token id = %d\n", next_id);
   ```

5. **检查点**：确认第 4 步调用后 `kv_cache.size() == attn_cnt`（因为 `is_prefill=true` 走的是 `emplace_back` 分支）。

**需要观察的现象**：程序打印出一个整数 token id；`kv_cache` 被填满到 `attn_cnt` 层。

**预期结果**：得到一个合法的 token id，且 `kv_cache.size() == attn_cnt`。如果把它喂给分词器的 decode，应得到一个有意义的下一个「词片段」。由于依赖真实模型权重，完整运行结果**待本地验证**。

**如果没有模型权重**：退化为源码阅读型实践——跟踪 [src/ncnn_llm_ocr.cpp:446-462](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_ocr.cpp#L446-L462)（prefill）与 [src/ncnn_llm_ocr.cpp:511-537](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_llm_ocr.cpp#L511-L537)（decode），在纸上画出四个函数的数据流图（`token id → embed → [mask+rope+kv] decoder → hidden → lm_head → logits → sample → next id`），并标注每一步用到的 `is_prefill` 取值。

## 6. 本讲小结

- ncnn 的前向调用是固定三步：`create_extractor()` → `input("inN", ...)` → `extract("outN", ...)`，输入输出靠字符串插槽名绑定，默认 `in0/out0`。
- `llm_run_text_embed` 把 token id（单个或一串）经 embed 网络变成 embedding；最简单的 Extractor 用法，含「外部指针 + `.clone()`」内存安全模式。
- `llm_run_decoder_with_kv` 是共享运行时的心脏：按层号拼接的 `cache_k%d/cache_v%d`（输入）与 `out_cache_k%d/out_cache_v%d`（输出）命名约定是运行时与导出脚本之间的契约；`is_prefill` 决定 KV cache 是 `emplace_back`（追加）还是 `=`（覆盖）。
- `llm_run_lm_head` 把 hidden states 投影成词表 logits；prefill 时调用方需先用 `row_range` 切出最后一个位置。
- `llm_select_next_token` 是「带 repetition penalty 的共享版采样」：负分数乘、正分数除以 \(\rho\) 做惩罚，再走贪心或 softmax+top_k+top_p+离散抽样；它和基类 `sample_logits` 是本项目两套采样实现。
- 这四个函数被 LLM（gpt）、OCR、ASR 三种模态**原样复用**，是「跨模型族共享 decoder + KV cache 运行时」设计主线的直接证据；唯一例外是带 `sconv_cnt`/`gdr_cnt` 的 Qwen3.5 混合架构在 generate 里把 decoder 调用内联展开。

## 7. 下一步学习建议

- **u2-l3 prefill 文本预填充流程**：把本讲的四函数骨架放回 `ncnn_llm_gpt::prefill`，看真实 prefill 如何「弹出最后一个 token 单独 decode」，以及因果 mask 的逐行构造。
- **u2-l4 generate 自回归解码主循环**：看 generate 如何反复调用这四个函数、每步推进 position_id 与 KV cache，以及 eos/think/tool_call 的停止与分流。
- **u3-l4 采样与解码策略**：深入 `sampling.cpp` 里 top_k/top_p 的实现细节与两套采样的全面对比。
- **u4-l1 RoPE 基础与长上下文变体**：本讲里反复出现的 `cos_cache/sin_cache` 是怎么生成的，以及 NTK/YaRN/LongRoPE 变体。
