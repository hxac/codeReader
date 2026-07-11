# CPU 参考实现全景与训练主循环

## 1. 本讲目标

本讲带你从「全局视角」通览 llm.c 的 CPU 参考实现 `train_gpt2.c`。读完本讲，你应当能够：

- 说清 `GPT2Config` 这 6 个超参数如何决定模型里**每一个张量**的大小。
- 数出 GPT-2 有哪 **16 个参数张量**、哪 **23 个激活张量**，并知道它们各自的形状与含义。
- 理解 llm.c 最具教学价值的工程技巧：**一次性 `malloc` 一整块内存，再用指针排布把各个张量「钉」进去**。
- 看懂 `main()` 里「验证 / 采样生成 / 训练」三段交替的调度，并能定位训练步那经典的「前向 → 清零梯度 → 反向 → 更新」四行调用。
- 解释为什么生成文本时要先把 `gen_tokens` 全填成 `eot_token`，并且**每生成一个 token 都要重算一次前向**。

> 本讲只看「骨架」，不深入每一层的算法细节（那是 Unit 2 的事）。目标是让你在脑子里建立一张 `train_gpt2.c` 的「地图」。

## 2. 前置知识

在开始前，请确认你已经理解以下概念（部分来自前置讲义 u1-l1、u1-l2）：

- **token 与词表（vocabulary）**：文本被切成一个个整数 id，所有可能 id 的集合大小记为 \(V\)（GPT-2 是 50257），为对齐到 128 的倍数而填充后的「填充词表」记为 \(V_p\)（GPT-2 是 50304）。
- **尺寸缩写**：本仓库全程用单字母缩写，务必先记住：
  - \(B\) = batch size（批大小，并行多少条独立序列）
  - \(T\) = sequence length（序列长度，即一条序列含多少 token）
  - \(C\) = channels（通道数/隐藏维度，GPT-2 124M 为 768）
  - \(V\) / \(V_p\) = 真实 / 填充词表大小
  - \(L\) = num_layers（Transformer 层数，GPT-2 124M 为 12）
  - \(NH\) = num_heads（注意力头数，GPT-2 124M 为 12）
- **前向 / 反向 / 训练循环**：前向算出预测与损失；反向沿计算图把损失对每个参数的梯度算出来；优化器用梯度更新参数。这三步循环就是「训练」。
- **指针算术**：C 语言里 `float* p; p + k` 会跳过 `k` 个 `float`（即 `4k` 字节）。本讲会大量出现 `wte + ix * C` 这种「跳到第 `ix` 行」的写法，这是理解张量内存布局的关键。
- **`train_gpt2.c` 的定位**（来自 u1-l1）：它是 llm.c 的「最干净、最可读」的 CPU 参考实现，文件开头注释明确说明它追求简单可读、不使用任何 CPU 特殊指令，只用少量 OpenMP pragma 做加速。

## 3. 本讲源码地图

本讲几乎全部围绕**一个文件**展开：

| 文件 | 作用 | 本讲关注点 |
|------|------|-----------|
| [train_gpt2.c](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c) | CPU fp32 参考实现（约 1182 行） | 全部三个模块 |
| [llmc/tokenizer.h](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/tokenizer.h) | GPT-2 BPE 分词器（仅解码） | `eot_token` 字段的来源 |
| [llmc/dataloader.h](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/dataloader.h) | `.bin` token 流的 batch 切分 | `main` 里加载训练/验证数据的调用 |

`train_gpt2.c` 内部的「分区」可以这样看：

```
第 1~30 行      ：头文件包含
第 35~521 行    ：各层的前向/反向函数（encoder/layernorm/matmul/attention/gelu/residual/softmax/crossentropy）
第 526~705 行   ：模型定义（GPT2Config / ParameterTensors / ActivationTensors / GPT2）  ← 本讲重点
第 707~763 行   ：gpt2_build_from_checkpoint（从 .bin 读权重）                          ← 本讲重点
第 765~891 行   ：gpt2_forward（前向组装）
第 893~1005 行  ：gpt2_zero_grad / gpt2_backward
第 1007~1044 行 ：gpt2_update（AdamW）/ gpt2_free
第 1051~1073 行 ：采样器（random_u32 / random_f32 / sample_mult）
第 1077~1181 行 ：main 训练主循环                                                       ← 本讲重点
```

本讲聚焦第 526~763 行与第 1077~1181 行这两段「骨架」，中间的各层算法留到 Unit 2 精读。

## 4. 核心概念与源码讲解

### 4.1 GPT2Config 与参数/激活张量结构体

#### 4.1.1 概念说明

要描述一个 GPT-2 模型，本质上只需要回答「它有多大」。`GPT2Config` 这个结构体就是用来回答这个问题的——它只装 **6 个整数**，但这 6 个整数一旦确定，模型里所有张量的形状、参数总量、显存/内存占用就**全部确定**了。

模型在运行时会涉及两大类张量：

- **参数张量（parameters / weights）**：模型「学到」的东西，训练就是在更新它们。例如 token 嵌入表 `wte`、位置嵌入表 `wpe`、各层注意力和 MLP 的权重与偏置。GPT-2 共 **16 个**参数张量，数量由宏 `NUM_PARAMETER_TENSORS` 固定为 16。
- **激活张量（activations）**：前向过程中**临时算出来**的中间结果，比如每一层的 LayerNorm 输出、QKV、注意力分数、残差流等。它们随 batch 一起产生、用完即可丢弃（反向传播时还要复用一部分）。GPT-2 共 **23 个**激活张量，数量由宏 `NUM_ACTIVATION_TENSORS` 固定为 23。

把这两类张量分别装进 `ParameterTensors` 和 `ActivationTensors` 两个结构体，好处是：代码里可以用 `params.qkvw`、`acts.ln1` 这样**有名字的指针**，而不是满屏的裸数组下标。这是 llm.c 既快又好读的关键设计。

#### 4.1.2 核心流程

- 模型大小由 6 个超参数描述：`max_seq_len`、`vocab_size`、`padded_vocab_size`、`num_layers`、`num_heads`、`channels`。
- 这 6 个数 → 喂给 `fill_in_parameter_sizes` → 算出 16 个参数张量各自有多少元素。
- 同理 → 喂给 `fill_in_activation_sizes`（还要额外知道本次前向的 \(B, T\)）→ 算出 23 个激活张量各自有多少元素。
- 参数张量数量固定 16，激活张量数量固定 23，这两个常量由宏定义：

\[
\text{NUM\_PARAMETER\_TENSORS} = 16, \quad \text{NUM\_ACTIVATION\_TENSORS} = 23
\]

> 注意一个不对称：参数张量的大小只依赖 `config`（与 \(B, T\) 无关）；而激活张量的大小**还要依赖本次前向的 \(B, T\)**，因为激活随 batch 变化。这正是后续「参数在构建时一次性分配、激活在前向时才懒分配」的根因。

#### 4.1.3 源码精读

**GPT2Config：6 个超参数**（[train_gpt2.c:526-533](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L526-L533)）

```c
typedef struct {
    int max_seq_len;          // 最大序列长度，如 1024
    int vocab_size;           // 真实词表大小，如 50257
    int padded_vocab_size;    // 填充到 %128==0，如 50304
    int num_layers;           // Transformer 层数，如 12
    int num_heads;            // 注意力头数，如 12
    int channels;             // 通道/隐藏维度，如 768
} GPT2Config;
```

注释里的 1024/50257/50304/12/12/768 就是 GPT-2 124M 的配置。这 6 个数决定了下面所有张量的大小。

**ParameterTensors：16 个参数张量的名字指针**（[train_gpt2.c:536-554](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L536-L554)）

```c
#define NUM_PARAMETER_TENSORS 16
typedef struct {
    float* wte;       // (Vp, C)   token 嵌入
    float* wpe;       // (maxT, C) 位置嵌入
    float* ln1w;      // (L, C)    第一个 LayerNorm 的权重
    float* ln1b;      // (L, C)    第一个 LayerNorm 的偏置
    float* qkvw;      // (L, 3C, C) QKV 投影权重
    float* qkvb;      // (L, 3C)    QKV 投影偏置
    float* attprojw;  // (L, C, C)  注意力输出投影权重
    float* attprojb;  // (L, C)     注意力输出投影偏置
    float* ln2w;      // (L, C)    第二个 LayerNorm 的权重
    float* ln2b;      // (L, C)    第二个 LayerNorm 的偏置
    float* fcw;       // (L, 4C, C) MLP 第一层权重
    float* fcb;       // (L, 4C)    MLP 第一层偏置
    float* fcprojw;   // (L, C, 4C) MLP 第二层权重
    float* fcprojb;   // (L, C)     MLP 第二层偏置
    float* lnfw;      // (C)       最终 LayerNorm 的权重
    float* lnfb;      // (C)       最终 LayerNorm 的偏置
} ParameterTensors;
```

注意结构体里**每个成员都只是一个 `float*` 指针**，此时并未分配任何数据。它们稍后会被「钉」到一整块大内存上（见 4.2）。另外，每个 Transformer 层有自己独立的权重（因此 `ln1w`、`qkvw` 等第一维都是 \(L\)），但 `wte`/`wpe`/`lnfw`/`lnfb` 是全模型共享的。

**ActivationTensors：23 个激活张量的名字指针**（[train_gpt2.c:601-626](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L601-L626)）

```c
#define NUM_ACTIVATION_TENSORS 23
typedef struct {
    float* encoded;    // (B, T, C)
    float* ln1;        // (L, B, T, C)
    float* ln1_mean;   // (L, B, T)
    float* ln1_rstd;   // (L, B, T)
    float* qkv;        // (L, B, T, 3C)
    float* atty;       // (L, B, T, C)
    float* preatt;     // (L, B, NH, T, T)
    float* att;        // (L, B, NH, T, T)
    float* attproj;    // (L, B, T, C)
    float* residual2;  // (L, B, T, C)
    // ... 共 23 个，包含 ln2/fch/fch_gelu/fcproj/residual3/lnf/logits/probs/losses 等
} ActivationTensors;
```

可以看到激活的形状普遍带 \(B, T\) 维（因为随 batch 变化），并且像 `preatt`、`att` 这种注意力分数张量形状是 \((L, B, NH, T, T)\)，是整个模型里最大的激活之一。`ln1_mean`/`ln1_rstd` 是 LayerNorm 为了反向传播而缓存下来的均值与倒数标准差。

#### 4.1.4 代码实践

**实践目标**：亲手把 GPT-2 124M 的参数总量算出来，体会「6 个超参数 → 16 个张量 → ~1.24 亿参数」这条链。

**操作步骤（源码阅读型）**：

1. 打开 [train_gpt2.c:556-577](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L556-L577)，这是 `fill_in_parameter_sizes`，它把 16 个张量的元素数逐个写进 `param_sizes[]` 数组。
2. 代入 GPT-2 124M 的配置 \(V_p=50304,\ C=768,\ \text{maxT}=1024,\ L=12\)，按公式逐项计算。例如：
   - `wte` = \(V_p \cdot C = 50304 \times 768 = 38{,}633{,}472\)
   - `qkvw` = \(L \cdot 3C \cdot C = 12 \times 2304 \times 768 = 21{,}233{,}664\)
   - `fcw` = \(L \cdot 4C \cdot C = 12 \times 3072 \times 768 = 28{,}311{,}552\)
3. 把 16 项相加。

**预期结果**：总和约为 \(124{,}475{,}904\)，即约 **1.24 亿**参数——这正是文件名 `gpt2_124M.bin` 里「124M」的由来。`gpt2_build_from_checkpoint` 会在第 744 行把这个数打印成 `num_parameters: ...`。

> 待本地验证：你可以编译运行（见 u1-l2 的 `make train_gpt2`），观察程序启动时打印的 `num_parameters` 是否与你手算一致。

#### 4.1.5 小练习与答案

**练习 1**：如果只把层数 \(L\) 从 12 加倍到 24（其他不变），16 个参数张量里有几个会变大？

**答案**：13 个会变大——所有带 \(L\) 第一维的张量（`ln1w/ln1b/qkvw/qkvb/attprojw/attprojb/ln2w/ln2b/fcw/fcb/fcprojw/fcprojb`，共 12 个）外加……实际上 `wte/wpe/lnfw/lnfb` 这 4 个与 \(L\) 无关，剩下 12 个与 \(L\) 成正比。所以**共 12 个**变大（注意不是 13，`NUM_PARAMETER_TENSORS=16`，去掉与 L 无关的 4 个）。

**练习 2**：为什么激活张量 `preatt` 和 `att` 的形状是 \((L, B, NH, T, T)\) 而不是 \((L, B, NH, T)\)？

**答案**：注意力需要计算**每个查询位置 \(t\) 对每个键位置 \(t_2\)** 的分数，因此是一个 \(T \times T\) 的方阵（每一层、每个 batch、每个头各一份）。这就是为什么注意力是模型里最吃激活内存的部分之一。

---

### 4.2 内存一次性 malloc 与指针排布

#### 4.2.1 概念说明

`ParameterTensors` 里有 16 个指针，`ActivationTensors` 里有 23 个指针。一个朴素做法是：对每个张量各调一次 `malloc`，于是参数要 `malloc` 16 次、激活要 `malloc` 23 次。llm.c **没有**这么做，而是用了一个极其优雅的技巧：

> **只调用一次 `malloc` 申请一整块大内存，然后把 16/23 个指针依次「钉」到这块内存的不同偏移上。**

这个设计有三个好处：

1. **快**：`malloc` 调用次数从 39 次降到 2 次（参数 1 次 + 激活 1 次）。
2. **内存连续、缓存友好**：所有参数挨在一起，读写时缓存命中率高。
3. **便于整体读写文件**：因为参数是连续的一整块，从 checkpoint 读权重时只需**一次** `fread` 就能把全部参数读进来（见 4.2.3）。

这个模式在 llm.c 里反复出现，CUDA 主线版本（Unit 5）也继承了同样的思路。

#### 4.2.2 核心流程

以参数为例，分配流程是：

```
fill_in_parameter_sizes(param_sizes, config)
        │  算出 16 个张量各自的元素数 → param_sizes[0..15]
        ▼
malloc_and_point_parameters(params, param_sizes)
        │  1) 求和 num_parameters = Σ param_sizes[i]
        │  2) 一次 malloc 申请 num_parameters 个 float
        │  3) 让 params->wte   指向偏移 0
        │     让 params->wpe   指向偏移 param_sizes[0]
        │     让 params->ln1w  指向偏移 param_sizes[0]+param_sizes[1]
        │     ……依次累加偏移
        ▼
得到 params_memory（一整块）+ 16 个已对齐好的指针
```

用伪代码描述「钉指针」的核心循环：

```text
iterator = base_address
for i in 0..15:
    ptrs[i] = iterator        # 第 i 个张量指针指向当前迭代位置
    iterator += sizes[i]      # 迭代器向后跳过该张量的长度
```

激活张量的分配逻辑完全一样，只是有 23 个指针、且大小依赖 \(B, T\)。

#### 4.2.3 源码精读

**`fill_in_parameter_sizes`：把 16 个张量的大小算出来**（[train_gpt2.c:556-577](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L556-L577)）

这个函数把 `config` 里的 6 个超参数「展开」成 16 个具体的元素数。例如 `param_sizes[4] = L * (3 * C) * C;` 对应 `qkvw`。读这个函数就能看到模型每一层到底有哪些权重、各自多大。

**`malloc_and_point_parameters`：一次 malloc + 指针排布的核心**（[train_gpt2.c:580-599](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L580-L599)）

```c
float* malloc_and_point_parameters(ParameterTensors* params, size_t* param_sizes) {
    size_t num_parameters = 0;
    for (size_t i = 0; i < NUM_PARAMETER_TENSORS; i++) {
        num_parameters += param_sizes[i];          // ① 求总和
    }
    float* params_memory = (float*)mallocCheck(num_parameters * sizeof(float)); // ② 一次 malloc
    float** ptrs[] = { &params->wte, &params->wpe, ..., &params->lnfb };        // ③ 指针的指针表
    float* params_memory_iterator = params_memory;
    for (size_t i = 0; i < NUM_PARAMETER_TENSORS; i++) {
        *(ptrs[i]) = params_memory_iterator;        // ④ 把第 i 个张量钉到当前迭代位置
        params_memory_iterator += param_sizes[i];   // ⑤ 迭代器后移
    }
    return params_memory;
}
```

这里的 `ptrs[]` 是一个「指向结构体成员指针的指针」数组，它把 16 个 `float*` 成员的地址收集起来，这样就能用一个循环统一处理，而不必写 16 行赋值。`malloc_and_point_activations`（[train_gpt2.c:658-676](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L658-L676)）结构完全对称，只是有 23 个成员。

**`gpt2_build_from_checkpoint`：从 .bin 读权重，一次 fread 装满整块内存**（[train_gpt2.c:707-763](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L707-L763)）

```c
// 读 256 个 int 的文件头
int model_header[256];
freadCheck(model_header, sizeof(int), 256, model_file);
if (model_header[0] != 20240326) { ... }   // 魔数校验
if (model_header[1] != 3) { ... }          // 版本校验

// 从文件头解析 6 个超参数
model->config.max_seq_len = model_header[2];
model->config.vocab_size  = model_header[3];
model->config.num_layers  = model_header[4];
model->config.num_heads   = model_header[5];
model->config.channels    = model_header[6];
model->config.padded_vocab_size = model_header[7];
```

随后在 [train_gpt2.c:748-749](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L748-L749)：

```c
model->params_memory = malloc_and_point_parameters(&model->params, model->param_sizes);
freadCheck(model->params_memory, sizeof(float), num_parameters, model_file);
```

注意第二行：因为 `params_memory` 是一整块连续内存，所以**一次 `fread` 就把全部 ~1.24 亿个参数从文件读进来**——这正是「一次性 malloc」设计带来的最大红利。如果改成 16 次 `malloc`，这里就得写 16 次 `fread`，还要小心每个指针的偏移。

**关键不对称——参数立即分配，激活懒分配**：在 [train_gpt2.c:753-762](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L753-L762)，`gpt2_build_from_checkpoint` 把所有激活相关指针都置为 `NULL`：

```c
model->acts_memory = NULL;
model->grads_memory = NULL;
model->m_memory = NULL;
model->v_memory = NULL;
model->grads_acts_memory = NULL;
```

为什么？因为激活大小依赖 \(B, T\)，而**构建模型时还不知道这次要用多大的 batch**。所以激活要推迟到第一次 `gpt2_forward` 时，根据传入的 \(B, T\) 「懒分配」（见 [train_gpt2.c:790-805](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L790-L805)）。梯度、优化器状态 `m/v` 同理，都是首次反向/更新时才分配。

#### 4.2.4 代码实践

**实践目标**：验证「指针排布」的正确性——相邻两个张量的指针之差，应当恰好等于前一个张量的元素数。

**操作步骤（源码阅读型）**：

1. 假设模型已通过 `gpt2_build_from_checkpoint` 构建。根据 `malloc_and_point_parameters` 的逻辑，写出相邻指针的关系：
   - `params.wpe - params.wte` 应当等于 `param_sizes[0]`（即 \(V_p \cdot C\)）
   - `params.ln1w - params.wpe` 应当等于 `param_sizes[1]`（即 \(\text{maxT} \cdot C\)）
2. 在 [train_gpt2.c:580-599](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L580-L599) 的循环里，确认 `params_memory_iterator` 每次累加的就是 `param_sizes[i]`，与你写出的关系一致。

**预期结果**：每个结构体成员指针都落在前一个成员结束的位置，整块内存严丝合缝、无空隙也无重叠。

**可选动手实践（待本地验证）**：在 `gpt2_build_from_checkpoint` 末尾临时加一行打印（注意：本讲禁止改源码，仅作思路说明，可在自己的副本上做）：

```c
printf("gap wte->wpe = %zu, expected %zu\n",
       (size_t)(model.params.wpe - model.params.wte),
       (size_t)model.param_sizes[0]);
```

两个数字应当相等。

#### 4.2.5 小练习与答案

**练习 1**：为什么 llm.c 选择「一次 malloc + 指针排布」，而不是每个张量单独 malloc？至少说出两条理由。

**答案**：(1) 分配次数从 39 次降到 2 次，开销小；(2) 内存连续，缓存友好，且能用一次 `fread`/`fwrite` 读写全部参数；(3) 释放时也只需 `free(params_memory)` 一次，不易泄漏。

**练习 2**：`ptrs[]` 数组里存放的是 `float**`（指向 `float*` 成员的指针），而不是 `float*`。为什么不直接用 `float*`？

**答案**：因为函数需要**修改结构体里的成员指针本身**（让 `params->wte` 指向新地址）。要修改一个 `float*` 变量，就必须传它的地址 `float**`。这相当于「按引用」传递每个成员指针。

**练习 3**：为什么参数在 `gpt2_build_from_checkpoint` 时就分配，而激活要推迟到 `gpt2_forward`？

**答案**：参数大小只依赖 `config`，构建时即可确定；激活大小还依赖本次前向的 \(B, T\)，构建时未知，故懒分配到首次前向。

---

### 4.3 main 训练主循环

#### 4.3.1 概念说明

`main()` 是整个程序的入口，它把前面所有零件组装成一个**可运行的训练过程**。它的工作可以分成「准备」和「循环」两段：

- **准备阶段**：从 checkpoint 构建模型 → 建两个 DataLoader（训练集、验证集）→ 建 Tokenizer → 给生成文本预留 `gen_tokens` 缓冲。
- **主循环**：`for (step = 0; step <= 40; step++)`，共 41 步。每一步按需做三件事：
  1. **验证（每 10 步）**：在验证集上跑若干 batch，估测验证损失 `val loss`，不更新参数。
  2. **采样生成（每 20 步）**：让模型自回归地生成一段文本打印出来，做「是否在说人话」的 sanity check。
  3. **训练（每步都做）**：经典的「前向 → 清零梯度 → 反向 → 更新」四步，用训练集的一个 batch 更新参数。

这个 41 步的小循环只是为了让你**观察 loss 下降、看模型生成**，不是真正训练出一个好模型——真正训练在 CUDA 主线 + scripts 脚本里（Unit 6、Unit 7）。

#### 4.3.2 核心流程

主循环的调度可以用下面这段伪代码概括：

```text
for step in 0..40:
    if step % 10 == 0:        # 验证
        reset val_loader
        val_loss = mean(forward(val_batch) for _ in 5 个 batch)
        print(val_loss)

    if step > 0 and step % 20 == 0:   # 生成
        gen_tokens[全部] = eot_token          # 用 EOT「开场」
        for t in 1..genT-1:
            forward(gen_tokens, targets=NULL) # 整段重算前向
            probs = acts.probs[0, t-1, :]     # 取上一个位置的预测分布
            next_token = sample_mult(probs)   # 按概率采样
            gen_tokens[t] = next_token
        print(生成的文本)

    # 训练（每步）
    next_batch(train_loader)
    forward(train_batch, targets)   # 算 loss
    zero_grad()                     # 清零梯度
    backward()                      # 反传梯度
    update(lr, beta1, beta2, eps, wd, step+1)  # AdamW 更新
    print(step, train loss, 耗时)
```

训练的「四行调用」是整个 GPT 训练范式的最简表达，务必记住。

#### 4.3.3 源码精读

**main 的准备阶段**（[train_gpt2.c:1077-1106](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L1077-L1106)）

```c
GPT2 model;
gpt2_build_from_checkpoint(&model, "gpt2_124M.bin");   // 从权重文件构建模型
...
int B = 4;   // batch size = 4
int T = 64;  // 序列长度 = 64（必须 <= maxT=1024）
DataLoader train_loader, val_loader;
dataloader_init(&train_loader, train_tokens, B, T, 0, 1, 1);
dataloader_init(&val_loader,   val_tokens,   B, T, 0, 1, 0);
...
Tokenizer tokenizer;
tokenizer_init(&tokenizer, "gpt2_tokenizer.bin");

uint64_t rng_state = 1337;
int* gen_tokens = (int*)mallocCheck(B * T * sizeof(int));
const int genT = 64;   // 生成 64 步
```

注意 \(B=4, T=64\) 是这个参考实现写死的「小尺寸」，目的是让 CPU 也能在合理时间内跑完 41 步。

**训练步：经典的四行调用**（[train_gpt2.c:1162-1171](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L1162-L1171)）

这是本讲最核心的四行（外加取数据和计时）：

```c
dataloader_next_batch(&train_loader);                              // 取一个 batch
gpt2_forward(&model, train_loader.inputs, train_loader.targets, B, T); // ① 前向：算 loss
gpt2_zero_grad(&model);                                            // ② 清零梯度
gpt2_backward(&model);                                             // ③ 反向：算梯度
gpt2_update(&model, 1e-4f, 0.9f, 0.999f, 1e-8f, 0.0f, step+1);     // ④ 更新：AdamW
...
printf("step %d: train loss %f (took %f ms)\n", step, model.mean_loss, time_elapsed_s * 1000);
```

对这四行的解释：

- ① `gpt2_forward`：用当前 batch 做**前向传播**，因为传入了 `targets`，所以会顺带算出交叉熵损失并存到 `model.mean_loss`。
- ② `gpt2_zero_grad`：把上一步累积的梯度（`grads_memory` 和 `grads_acts_memory`）全部清零。这一步必须在 `gpt2_backward` **之前**；至于它排在 `forward` 之后，只是因为 `forward` 只写激活、不碰梯度，先后无所谓。
- ③ `gpt2_backward`：从损失出发，沿计算图反传，把每个参数的梯度写进 `grads`。
- ④ `gpt2_update`：用 AdamW 优化器，根据梯度更新 `params_memory`。这里传入的超参数是：学习率 `1e-4`、\(\beta_1=0.9\)、\(\beta_2=0.999\)、\(\epsilon=10^{-8}\)、权重衰减 `0.0`、时间步 `step+1`（注意 `+1`，因为 step 从 0 开始而 Adam 的偏差修正要求 \(t \ge 1\)）。

**验证段（每 10 步）**（[train_gpt2.c:1113-1123](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L1113-L1123)）：在验证集上跑 5 个 batch（`val_num_batches = 5`），只前向算 loss、取平均，**不做反向也不更新**，纯粹用来监控模型在没见过的数据上的表现。

**采样生成段（每 20 步）**（[train_gpt2.c:1126-1160](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L1126-L1160)）

```c
// 用 EOT「开场」：把 gen_tokens 全部填成 eot_token
for(int i = 0; i < B * T; ++i) {
    gen_tokens[i] = tokenizer.eot_token;
}
printf("generating:\n---\n");
for (int t = 1; t < genT; t++) {
    // 注意：这里每生成一个 token 都把整段 (B,T) 从头重算一遍前向
    gpt2_forward(&model, gen_tokens, NULL, B, T);
    // 只取第 0 个 batch 行、位置 t-1 的预测分布
    float* probs = model.acts.probs + (t-1) * model.config.padded_vocab_size;
    float coin = random_f32(&rng_state);
    int next_token = sample_mult(probs, model.config.vocab_size, coin);
    gen_tokens[t] = next_token;
    ...打印 next_token...
}
```

这段代码里有两个**关键设计问题**，也正是本讲实践任务要你解释的：

1. **为什么先把 `gen_tokens` 全填成 `eot_token`？**
   `eot_token`（`<|endoftext|>`）在 GPT-2 的训练语料里表示「一篇文档的结束/开始」。生成时用它「开场」，等于告诉模型「现在要开始一篇新文档了」，于是模型从文档开头的分布开始续写。这是 GPT-2 无条件（unconditional）生成的标准做法——你给它一个 EOT 作为「提示」，它就从零开始写。

2. **为什么每生成一个 token 都要重新做一次完整前向（`gpt2_forward`）？**
   因为前一个 token 刚刚被填进 `gen_tokens[t]`，位置 \(t\) 的输入变了，所以位置 \(t\) 的输出（即第 \(t+1\) 个 token 的预测）必须重新计算。代码注释（[train_gpt2.c:1134-1137](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L1134-L1137)）坦诚承认这「非常浪费」——它每次都把**整段 \((B, T)\) 所有位置**从头算一遍，但其实只有最后一个位置的预测是新需要的，前面的位置根本没变。对于一个「只用来 sanity check 生成是否正常」的参考实现，为了保持代码极简、避免引入 KV-cache 等复杂度，作者接受了这种浪费。

另外，`probs` 取的是 `probs[0, t-1, :]`（第 0 个 batch 行、位置 \(t-1\)）：因为位置 \(t-1\) 的输出正是对**位置 \(t\) 该填什么 token** 的预测。生成只用了第 0 行（`b=0`），其余 \(B-1\) 行虽然也并行算了，但被忽略——注释说这相当于「并行跑 \(B\) 条生成流，但只看第 0 条」。

#### 4.3.4 代码实践

**实践目标**：定位训练四行调用，并用自己的话解释「EOT 开场 + 逐 token 重算前向」这两个设计。

**操作步骤（源码阅读型 + 运行观察型）**：

1. **定位四行调用**。打开 [train_gpt2.c:1162-1171](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L1162-L1171)，依次找到并抄下：
   - 前向：第 **1165** 行 `gpt2_forward(&model, train_loader.inputs, train_loader.targets, B, T);`
   - 清零梯度：第 **1166** 行 `gpt2_zero_grad(&model);`
   - 反向：第 **1167** 行 `gpt2_backward(&model);`
   - 更新：第 **1168** 行 `gpt2_update(&model, 1e-4f, 0.9f, 0.999f, 1e-8f, 0.0f, step+1);`
2. **追踪 EOT 开场**。在 [train_gpt2.c:1126-1133](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L1126-L1133) 找到先把 `gen_tokens` 全填成 `tokenizer.eot_token` 的循环，以及紧随其后的 `for (int t = 1; t < genT; t++)` 生成循环。确认 `eot_token` 字段来自 `Tokenizer` 结构体（[llmc/tokenizer.h:18-23](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/tokenizer.h#L18-L23)），它就是 `<|endoftext|>` 的 id。
3. **追踪逐 token 重算**。在生成循环体内确认每一步都调用了 `gpt2_forward(&model, gen_tokens, NULL, B, T)`，且 `targets` 传 `NULL`（生成时不需要算 loss）。读 [train_gpt2.c:1134-1137](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L1134-L1137) 的注释，作者明确说这是 wasteful。
4. **运行观察**（前置：已完成 u1-l2 的 `./dev/download_starter_pack.sh` 与 `make train_gpt2`）。运行：
   ```bash
   OMP_NUM_THREADS=4 ./train_gpt2
   ```
   观察输出里每 10 步打印一次的 `val loss`、每 20 步打印的 `generating: ... ---` 段落、以及每步的 `step N: train loss X (took Y ms)`。

**需要观察的现象**：

- 启动时先打印 `[GPT-2]` 配置块、`num_parameters`（约 1.24 亿）、`num_activations`。
- `train loss` 从约 **5.3** 起步，随着 step 推进**逐步下降**（可能带噪声，但总体趋势向下）。
- `val loss` 同样呈下降趋势。
- 在 step 20、40 处会打印一段模型生成的文本（初期可能是乱码或重复，因为才训练了几十步）。

**预期结果**：你能用一句话回答——「EOT 开场是因为它代表『新文档开始』，给模型一个生成起点；逐 token 重算前向是因为每填入一个新 token，后续位置的预测都变了，必须重算——虽然作者注释承认这是为了代码简洁而忍受的浪费。」

> 待本地验证：CPU 上跑完 41 步可能需要几分钟到几十分钟（取决于机器与 `OMP_NUM_THREADS`）。若只关心 loss 趋势，可在看到 step 10 的输出后中断。

#### 4.3.5 小练习与答案

**练习 1**：训练四行里，`gpt2_zero_grad` 能不能放到 `gpt2_forward` **之前**？为什么作者把它放在 `forward` 之后、`backward` 之前？

**答案**：可以。`gpt2_forward` 只写激活（`acts_memory`），完全不读写梯度（`grads_memory`/`grads_acts_memory`）。因此 `zero_grad` 只要排在 `backward` 之前即可，相对 `forward` 的先后无所谓。作者排在 `forward` 之后，是为了让「前向 → 清零 → 反向 → 更新」读起来贴合训练的因果顺序。

**练习 2**：`gpt2_update` 最后一个参数为什么是 `step+1` 而不是 `step`？

**答案**：循环变量 `step` 从 0 开始，而 Adam/AdamW 的偏差修正项 \(1 - \beta_1^t\) 在 \(t=0\) 时为 0，会导致除零。优化器要求时间步 \(t \ge 1\)，所以传 `step+1`，让第一步的 \(t=1\)。

**练习 3**：生成循环里 `gpt2_forward` 的第二个参数（targets）为什么传 `NULL`？

**答案**：生成阶段不需要计算损失，也就不需要目标 token。`gpt2_forward` 内部会检查 `targets != NULL`（[train_gpt2.c:880-890](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L880-L890)），传 `NULL` 时跳过交叉熵计算、把 `mean_loss` 置为 `-1.0f`。我们只需 `acts.probs` 来采样下一个 token。

## 5. 综合实践

把本讲三个模块串起来，完成下面这个「读图 + 追踪」任务：

**任务**：画出 `train_gpt2.c` 从「启动」到「完成第 1 个训练步」的完整数据流图，并标注每一步用到了本讲的哪个概念。

**要求**：

1. 在图上标出以下节点与依赖关系（可画在纸上或用任何画图工具）：
   - `main` 调 `gpt2_build_from_checkpoint("gpt2_124M.bin")`
     - 内部：读文件头 → 填 `GPT2Config`（模块 4.1）→ `fill_in_parameter_sizes` → `malloc_and_point_parameters`（模块 4.2）→ 一次 `fread` 装满 `params_memory`
     - 此时 `acts_memory = NULL`（懒分配）
   - `main` 调 `dataloader_init` / `tokenizer_init`
   - `main` 进入 `step=0` 循环：
     - 先做验证（`val loss`）
     - 再做训练：`dataloader_next_batch` → `gpt2_forward`（这里首次触发激活的懒分配，模块 4.2）→ `gpt2_zero_grad` → `gpt2_backward`（首次触发 `grads_memory` 懒分配）→ `gpt2_update`（首次触发 `m/v_memory` 懒分配）
2. 在图旁用一句话标注：哪一步是「参数被分配」、哪一步是「激活被分配」、哪一步是「梯度/优化器状态被分配」，从而体会 llm.c 的**三级懒分配**策略。
3. 最后，对照你画的图，向自己解释一遍：为什么构建模型时只 malloc 了参数，而激活、梯度、m/v 都推迟了？

**预期产出**：一张包含「文件头解析 → 参数分配 → 数据加载 → 验证 → 前向（含激活懒分配）→ 清零 → 反向（含梯度懒分配）→ 更新（含 m/v 懂分配）」的流程图，以及你对「懒分配」动机的口头解释（核心：这些张量的大小依赖运行时才知道的量，或只在反向/更新时才需要）。

> 这是一个纯源码阅读型实践，不需要运行程序也能完成；若结合第 4.3.4 节的实际运行输出对照，效果更好。

## 6. 本讲小结

- `GPT2Config` 只用 **6 个整数**（`max_seq_len`/`vocab_size`/`padded_vocab_size`/`num_layers`/`num_heads`/`channels`）就完整描述了模型大小，它们决定了所有张量的形状。
- GPT-2 有 **16 个参数张量**（`NUM_PARAMETER_TENSORS`）和 **23 个激活张量**（`NUM_ACTIVATION_TENSORS`），分别装在 `ParameterTensors` 和 `ActivationTensors` 两个「带名字指针」的结构体里，让代码既高效又好读。
- llm.c 最优雅的工程技巧是**一次性 `malloc` 一整块内存，再用指针排布把各张量钉进去**：分配次数少、内存连续、可用一次 `fread`/`fwrite` 读写全部参数。
- `gpt2_build_from_checkpoint` 解析 `.bin` 文件头（魔数 20240326、版本 3、6 个超参数），**立即分配参数**并一次读入；而激活、梯度、优化器状态都**懒分配**（分别在首次前向/反向/更新时）。
- `main` 主循环按 `step % 10 == 0`（验证）、`step % 20 == 0`（生成）、每步（训练）三档调度；训练就是经典的「前向 → 清零梯度 → 反向 → 更新」四行（[train_gpt2.c:1165-1168](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L1165-L1168)）。
- 生成时先用 `eot_token`（`<|endoftext|>`）「开场」作为新文档起点，再逐 token 重算前向——作者注释坦承后者是为代码简洁而忍受的浪费。

## 7. 下一步学习建议

本讲建立的是 `train_gpt2.c` 的「骨架地图」。下一步建议：

- **进入 Unit 2，逐层精读前向算法**：从 [u2-l1 编码层 encoder](u2-l1-encoder-layer.md) 开始，搞懂 `encoder_forward` 怎么把 token + position 嵌入相加；然后依次学 LayerNorm、MatMul、Attention、GELU/残差、Softmax/交叉熵，最后在 [u2-l7 前向组装](u2-l7-forward-assembly.md) 里把这些层串回 `gpt2_forward`。
- **进入 Unit 3，精读反向与优化**：[u3-l1 反向组装](u3-l1-backward-assembly.md) 讲 `gpt2_backward` 如何镜像前向顺序，[u3-l2 AdamW](u3-l2-adamw-optimizer.md) 讲本讲看到的 `gpt2_update` 四行超参数背后的公式。
- **想看正确性如何保证**：直接跳到 [u3-l4 数值正确性测试](u3-l4-correctness-test.md)，看 `test_gpt2.c` 如何用 `debug_state.bin` 把本讲的实现逐元素对照到 PyTorch。
- **延伸阅读源码**：重读 [train_gpt2.c:526-705](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L526-L705) 的模型定义段，你会发现 Unit 2、Unit 3 的所有素材都已经「压缩」在这 180 行里了。
