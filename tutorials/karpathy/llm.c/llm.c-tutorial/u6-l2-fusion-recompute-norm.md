# 重计算、融合算子与 global norm

## 1. 本讲目标

本讲是「训练工程」单元的第二篇，聚焦 CUDA 主线 `train_gpt2.cu` 里三类「让训练既省显存又快、还数值稳定」的工程手段。学完后你应该能够：

- 说清 `recompute`（重计算）选项 0/1/2 各自丢弃哪些前向激活、在反向里如何重算，以及它如何用「多一次算力」换「少一大块显存」。
- 看懂 `fused_residual_forward5`（残差相加 + LayerNorm 融合）与 `fused_classifier`（softmax + 交叉熵 + 反向启动融合）两个融合算子的收益来源。
- 理解 `global_norm` 如何用两阶段、确定性归约算出全模型梯度范数，以及 `grad_clip` / `grad_scale` 如何在 AdamW 之前对梯度做按范数裁剪。

本讲只讲「策略与装配」，不展开 kernel 内部每一行优化——那是上一讲（u5-l4）和 dev/cuda 内核库（u7-l1）的内容。

## 2. 前置知识

本讲默认你已经读过：

- **u5-l1**：`floatX` 精度宏、`ParameterTensors`/`ActivationTensors`、`TensorSpec` 与「一次 `cudaMalloc` + 指针排布」的内存模型。
- **u5-l4**：手写 CUDA kernel 的三种并行套路（元素级切片、行内 warp 归约、全局 block 归约），以及 `blockReduce`/`warpReduceSum`、`x128` 向量化、`.cs`/`.cg` cache hint 等公共地基。
- **u6-l1**：混合精度训练、BF16 工作副本 + FP32 master weights 的关系。

三个会用到的关键事实先列在这里：

1. 反向传播需要复用前向保存的激活（如 LayerNorm 的 `mean`/`rstd`、MLP 的 `fch_gelu`），这是 u3-l1 讲过的「checkpointing 取舍」。
2. llm.c 反向里所有**参数梯度**用 `+=` 累加，所有**激活梯度**用 `=` 覆盖写——唯一的例外是残差流，它的梯度必须 `+=`（见 [llmc/layernorm.cuh:1-10](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/layernorm.cuh#L1-L10) 文件头注释）。
3. GPU 上「一次 kernel 启动」的固定开销不大，但「一次显存往返」（把中间结果写回 HBM 再读回来）很贵。融合算子的核心收益几乎都来自减少显存往返，而不是减少 FLOP。

GPT-2 124M 的尺寸缩写沿用全册约定：层数 \(L=12\)、通道 \(C=768\)、序列长 \(T\)、batch \(B\)、真实词表 \(V=50257\)、对齐后 \(V_p=50304\)。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `train_gpt2.cu` | CUDA 主线。本讲关注其中三处：`fill_in_activation_sizes`（按 recompute 决定激活尺寸）、`gpt2_forward`/`gpt2_backward_and_reduce`（recompute 与融合算子的调用点）、`gpt2_calculate_grad_norm` 与 `main` 训练循环（grad norm 与裁剪）。 |
| `llmc/layernorm.cuh` | LayerNorm 的前向/反向 kernel，外加本讲的 `fused_residual_forward5`（残差相加 + LayerNorm 融合）。 |
| `llmc/fused_classifier.cuh` | 把 softmax + 交叉熵 + 反向启动融合进单个 kernel，且**从不物化完整概率向量**。 |
| `llmc/global_norm.cuh` | 两阶段、确定性（无 atomicAdd）的全局梯度平方范数归约。 |
| `llmc/adamw.cuh` | AdamW kernel，本讲只看它的 `grad_scale` 参数如何作用于梯度（裁剪的落点）。 |

## 4. 核心概念与源码讲解

### 4.1 recompute：以算力换显存的策略

#### 4.1.1 概念说明

反向传播要用到前向的中间激活。对 GPT-2 而言，最占显存的几个激活张量都带一个 \(L\)（层数）维度：

- `ln1` / `ln2`：每个 Transformer block 两个 LayerNorm 的输出，形状 \((L, B, T, C)\)。
- `fch_gelu`：MLP 升维后经过 GELU 的输出，形状 \((L, B, T, 4C)\)——注意这里是 \(4C\)，是上面两个的 4 倍，**最吃显存**。

「重计算」（gradient checkpointing / recomputation）的思路很简单：

> 如果某个前向激活既很占显存、又能用「更便宜的前向输入 + 一次轻量前向」重新算出来，那就别在显存里为每一层都存一份——反向到那一层时再就地重算一次即可。代价是这部分前向算了两遍（一遍真前向、一遍反向里补算），换来的是激活显存的大幅下降。

llm.c 用一个 `recompute` 整数把这件事参数化为三档：

- `recompute == 0`：不重算。每一层的 `fch_gelu`、`ln1`、`ln2` 都存满 \(L\) 份。
- `recompute == 1`：只重算 GELU。`fch_gelu` 只留 1 份（不是 \(L\) 份），反向时按需重算。
- `recompute == 2`：再重算 LayerNorm。`ln1`/`ln2` 也只留 1 份，反向时重算。

默认值是 `1`——这是「显存-算力」的甜点：干掉最大的 `fch_gelu` 这块大头，又不引入太多额外前向。

#### 4.1.2 核心流程

recompute 的实现分两半，前向与反向必须对称：

```text
【分配阶段】 fill_in_activation_sizes(recompute):
  fch_gelu 尺寸 = (recompute < 1) ? L*B*T*4C : B*T*4C   # 0→存L份，1/2→只存1份
  ln1    尺寸 = (recompute < 2) ? L*B*T*C  : 0          # 0/1→存L份，2→不分配
  ln2    尺寸 = (recompute < 2) ? L*B*T*C  : 0

【前向 gpt2_forward】 每层 l:
  l_fch_gelu 指针 = (recompute < 1) ? fch_gelu + l*... : fch_gelu   # 0→每层独立槽；1/2→所有层复用同一个槽
  ... 把 l_fch_gelu 当作本层 GELU 输出写下去 ...

【反向 gpt2_backward_and_reduce】 每层 l（逆序）:
  if recompute >= 1: gelu_forward(l_fch_gelu, l_fch_pre_gelu, ...)   # 用 pre-gelu 重算 gelu
  if recompute >= 2: layernorm_forward(l_ln2, ...)                    # 用 residual2 重算 ln2
  ... 用重算出来的 l_fch_gelu / l_ln2 做反向 ...
  if recompute >= 2: layernorm_forward(l_ln1, ...)                    # 用 residual 重算 ln1
```

注意三个对称要求：

1. 前向把所有层都写进**同一个** `fch_gelu` 槽（recompute≥1 时），意味着上一层的 gelu 输出会被下一层覆盖——这没问题，因为反向到每一层时都会先重算再使用。
2. 反向必须**先重算、再消费**，顺序与该激活在反向里第一次被读取的位置对齐。
3. recompute 只影响「会被丢弃的激活」，像 `fch`（pre-gelu，即 GELU 的输入）这样的前向输入必须**全程保留**，因为它是重算 GELU 的原料。

#### 4.1.3 源码精读

**(1) recompute 是模型的一个整数字段，默认 1**

[train_gpt2.cu:318](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L318)：recompute 字段定义，注释写明 0|1|2 = none / gelu / gelu+ln。

[train_gpt2.cu:349](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L349)：默认值 `model->recompute = 1;`，注释「good default: recompute gelu but not layernorm」。

它也是命令行参数 `-r`，见 [train_gpt2.cu:1406](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L1406) 的帮助文本与 [train_gpt2.cu:1449](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L1449)、[train_gpt2.cu:1488](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L1488) 的解析，所以可在不重编译的情况下切换。

**(2) recompute 决定激活缓冲的分配尺寸**

这是「省显存」的物理落点——不分配就不占显存：

[train_gpt2.cu:219-254](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L219-L254)：`fill_in_activation_sizes`，注意三条受 recompute 控制的尺寸：

- [train_gpt2.cu:226](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L226)：`tensors[1] = TENSOR_SPEC(data->ln1, (recompute < 2) ? L * B * T * C : 0);`
- [train_gpt2.cu:238](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L238)：`tensors[7] = TENSOR_SPEC(data->ln2, (recompute < 2) ? L * B * T * C : 0);`
- [train_gpt2.cu:243](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L243)：`tensors[11] = TENSOR_SPEC(data->fch_gelu, (recompute < 1) ? L * B * T * 4*C : B * T * 4*C);` ← 本讲实践任务的主角

`size == 0` 时分配器会把该指针设为 `NULL`（见 [train_gpt2.cu:275-276](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L275-L276)），这是「未分配」的哨兵，防止误用。

**(3) 前向：本讲实践任务的三元表达式**

[train_gpt2.cu:713](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L713)：

```c
// reuse the same activation buffer at each layer, as we'll re-compute the gelu during backward
// very useful because we dramatically reduce VRAM usage, and may be able to fit larger batch size
floatX* l_fch_gelu = (model->recompute < 1) ? acts.fch_gelu + l * B * T * 4*C : acts.fch_gelu;
```

含义：当 `recompute < 1`（即 0）时，每层用各自独立的槽 `acts.fch_gelu + l*...`；否则（1 或 2）所有层都指向**同一个** `acts.fch_gelu`（第 0 层的槽）。注释说得直白：「复用同一个激活缓冲，因为反向时会重算 gelu；这能大幅降低显存、允许更大 batch。」

同样的指针切换也发生在 `l_ln1`、`l_ln2`：[train_gpt2.cu:703](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L703) 与 [train_gpt2.cu:707](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L707)，当 `recompute >= 2` 时它们指向 `acts.lnf`（最终的 lnf 缓冲）当临时缓冲复用。

**(4) 反向：先重算、再消费**

[train_gpt2.cu:895-899](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L895-L899)：GELU 重算

```c
if(model->recompute >= 1) {
    // recompute >= 1 means we recompute gelu. in this case,
    // l_fch_gelu is just a buffer, so re-compute the gelu from l_fch here
    gelu_forward(l_fch_gelu, l_fch_pre_gelu, B*T*4*C, main_stream);
}
```

`l_fch_pre_gelu` 就是 `acts.fch`（GELU 的输入，全程保留），用它把当前层的 gelu 输出重新填进那个唯一的 `l_fch_gelu` 槽，紧接着 [train_gpt2.cu:900](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L900) 的 `matmul_backward` 才会读取它。

[train_gpt2.cu:901-904](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L901-L904) 与 [train_gpt2.cu:920-922](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L920-L922)：LayerNorm 重算，分别在各反向 matmul 之前用 `layernorm_forward` 把 `l_ln2`/`l_ln1` 现场算出来。

#### 4.1.4 代码实践

**实践目标**：亲手在源码里定位 `fch_gelu` 的「缓冲复用三元表达式」，并定量解释 `recompute=1` 相对 `recompute=0` 省了多少显存、多花了多少算力。

**操作步骤**：

1. 打开 `train_gpt2.cu`，跳到 [第 713 行](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L713)，确认前向里 `l_fch_gelu` 的指针切换表达式。
2. 再跳到 [第 243 行](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L243)，确认 `fch_gelu` 的**分配尺寸**用同一个三元判断。
3. 跳到 [第 895-899 行](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L895-L899)，确认反向里有对应的「重算」调用。
4. 取一组典型值（GPT-2 124M：\(L=12, C=768\)，设 \(B=4, T=1024\)，BF16 即每元素 2 字节）手算两档显存。

**需要观察的现象 / 预期结果**（待本地验证数值）：

- `recompute=0`：`fch_gelu` 存 \(L \cdot B \cdot T \cdot 4C = 12 \times 4 \times 1024 \times 3072 \approx 1.51 \times 10^8\) 个元素，BF16 下约 \(288\) MiB。
- `recompute=1`：只存 \(B \cdot T \cdot 4C = 4 \times 1024 \times 3072 \approx 1.26 \times 10^7\) 个元素，约 \(24\) MiB。
- 二者之差约 \(264\) MiB——这就是「省下的显存」。算力代价：反向里多跑一次 `gelu_forward`（作用于 \(B \cdot T \cdot 4C\) 个元素，每层一次，共 \(L\) 次），相对整个 block 的 matmul 而言开销很小，这正是默认值取 1 的原因。

> 说明：以上字节换算未把 `ln1`/`ln2`（recompute=2 才省）计入；如需精确峰值显存，建议直接运行 `./train_gpt2cu ...`，看启动时打印的 `allocating N MiB for activations`（见 [train_gpt2.cu:262](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L262)），分别用 `-r 0` 与 `-r 1` 各跑一次对比。若本地无 GPU，此项标注「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `fch`（pre-gelu）即使开了 recompute 也必须全程保留，而 `fch_gelu`（post-gelu）可以丢？

> **答案**：`fch` 是重算 GELU 的**输入原料**（见 [train_gpt2.cu:898](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L898) 的 `gelu_forward(l_fch_gelu, l_fch_pre_gelu, ...)`）；丢了它就没法重算。`fch_gelu` 是 GELU 的**输出**，可以用 `fch` 现算出来，所以能丢。

**练习 2**：把 `recompute` 从 1 调到 2，前向算力会不会变多？反向呢？

> **答案**：前向算力基本不变（前向该跑的 LayerNorm 一次没少，只是把输出写进复用槽）。反向会变多：每一层都要额外重算 `ln1` 和 `ln2`（[train_gpt2.cu:903](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L903) 与 [train_gpt2.cu:921](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L921)）。所以 `recompute=2` 是「再牺牲一点反向算力，再省 `ln1`/`ln2` 那 \(2 \cdot L \cdot B \cdot T \cdot C\) 的显存」。

### 4.2 融合算子：fused_residual_forward5 与 fused_classifier

#### 4.2.1 概念说明

「融合」（fusion）在 GPU 编程里指把多个算子合并进同一个 kernel，目的是**减少显存往返**。每多写一次中间结果到 HBM、再读回来，就多一份带宽开销；把两步在寄存器/共享内存里接力完成，就能省掉这一来一回。

llm.c 里有两个教科书级的融合算子，本讲各看一个：

1. **`fused_residual_forward5`**：把「残差相加」和「LayerNorm」两个前向算子融合。原本需要先把 `residual = inp1 + inp2` 写到一块 \(B \cdot T \cdot C\) 的全局内存、再让 LayerNorm kernel 读它做归一化；融合后，相加的结果先留在共享内存里，紧接着就地算 mean/var/rstd 并写出归一化结果，省掉一次 \(B \cdot T \cdot C\) 的「写后再读」。注意它仍然会把 `residual` 写到全局内存——因为反向要用（LayerNorm 反向需要原始输入 `inp`，见 u2-l2）。

2. **`fused_classifier`**：把「softmax + 交叉熵损失」与「反向的 dlogits 计算」融合进单个 kernel，并且**从不物化完整的概率向量**。它一边算 softmax 的 running-max/running-sum（online softmax，见 u5-l5），一边只在目标 token 处取出概率算损失，紧接着用同样的 softmax 参数把 `dlogits = (probs - onehot) * dloss` 直接写回 logits 缓冲——于是「前向算损失」和「反向算第一个梯度」在同一个 kernel 里完成，probs 这个 \(B \cdot T \cdot V_p\) 的大向量从头到尾不需要单独存。

#### 4.2.2 核心流程

**fused_residual_forward5** 的单 token 数据流：

```text
对每个 token (b,t)，由一个 block（内含多个 warp，每 warp 处理一个 token）负责：
  1. weight/bias 载入共享内存（一次性，全 block 共享）
  2. for c in C（向量化 x128）:
        res[c] = inp1[c] + inp2[c]          # 残差相加
        s_res[c] = res[c]                    # 暂存到共享内存（不写回全局再读）
        sum += res[c]                         # 累计求均值
  3. mean = warpReduceSum(sum) / C
  4. for c in C: v += (res[c]-mean)^2        # 第二趟，求方差
  5. rstd = rsqrt(v/C + eps)
  6. for c in C:
        normed[c] = rstd*(res[c]-mean)*w[c] + b[c]   # 归一化 + 缩放偏移
  输出：residual[b,t,:]（写全局，供反向）、normed[b,t,:]（喂下一个 matmul）、mean/rstd（供反向）
```

**fused_classifier** 的单行（一个 token 的 logits）数据流：

```text
prepare_softmax_blockwide3（online softmax，一个 block 管一行 logits）：
  running maxval + running sumval → 得 SoftmaxParams{Scale=1/sum, Offset=max}

损失（单线程，threadIdx.x==0）：
  prob_target = exp(logits[ix] - Offset) * Scale
  losses[idx] -= logf(prob_target)          # losses 预先 memset 为 0

__syncthreads()  # 防止读 logits 算损失 与 把 logits 改写成 dlogits 之间发生竞争

dlogits（全 block 并行，模板 WriteDLogits 控制）：
  for i in V（向量化 x128）:
     prob_i = exp(logits[i] - Offset) * Scale
     indicator = (i == ix) ? 1 : 0
     logits[i] = (prob_i - indicator) * dloss   # 原地：logits 被「更新」为 dlogits
```

关键约束：因为要把 `logits` 原地改写成 `dlogits`，所以算损失（读 logits）和写 dlogits（改 logits）之间必须 `__syncthreads()`，否则同一个 block 内会有数据竞争（[llmc/fused_classifier.cuh:88-92](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/fused_classifier.cuh#L88-L92) 的注释详细解释了这个 bug）。

#### 4.2.3 源码精读

**(1) fused_residual_forward5 kernel**

[llmc/layernorm.cuh:142-219](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/layernorm.cuh#L142-L219)：`fused_residual_forward_kernel5`。

- [llmc/layernorm.cuh:156-162](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/layernorm.cuh#L156-L162)：weight/bias 载入共享内存，必须在 `idx` 越界检查之前完成（否则会有线程提前退出导致同步死锁，注释 [llmc/layernorm.cuh:73-74](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/layernorm.cuh#L73-L74) 在 kernel6 里有同样说明）。
- [llmc/layernorm.cuh:175-185](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/layernorm.cuh#L175-L185)：残差相加，结果既 `store128cs(residual+c)`（写全局供反向），又存进 `s_res`（共享内存，紧接着算统计量用）——**这就是「融合」的落点**：相加结果不经过 HREM 往返就直接进入 LayerNorm 计算。
- [llmc/layernorm.cuh:201-213](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/layernorm.cuh#L201-L213)：用 mean/rstd 做归一化 + 缩放偏移，写出 `normed`。

[llmc/layernorm.cuh:467-490](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/layernorm.cuh#L467-L490)：启动器 `fused_residual_forward5`。它先尝试申请超过 48 KiB 的动态共享内存（[llmc/layernorm.cuh:479](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/layernorm.cuh#L479) 的 `cudaFuncSetAttribute`），失败则**回退到非融合版本**——先 `residual_forward` 再 `layernorm_forward`（[llmc/layernorm.cuh:485-487](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/layernorm.cuh#L485-L487)）。注意回退调用的 `layernorm_forward(..., N, 1, C, ...)` 把 `B=N, T=1`，行数仍是 \(N=B \cdot T\)。

调用点在 `gpt2_forward` 的每个 block：[train_gpt2.cu:734](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L734)（MLP 子层后的残差+ln2）与 [train_gpt2.cu:744-749](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L744-L749)（跨 block 融合：用本层残差 + 下一层的 ln1，或最后一层用 lnf）。

**(2) fused_classifier kernel**

[llmc/fused_classifier.cuh:19-63](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/fused_classifier.cuh#L19-L63)：`prepare_softmax_blockwide3`，online softmax，返回 `{Scale=1/sum, Offset=max}`。

[llmc/fused_classifier.cuh:82-86](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/fused_classifier.cuh#L82-L86)：损失计算，只在目标 token 处取概率：

```c
if(threadIdx.x == 0) {
    float prob = expf((float)logits[idx * P + ix] - sp.Offset) * sp.Scale;
    losses[idx] -= logf(prob);
}
```

`losses` 在调用前被 `cudaMemset` 清零（[train_gpt2.cu:775](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L775)），所以 `-= logf(prob)`（logf 为负）得到正的交叉熵 \(-\log p_{\text{target}}\)。

[llmc/fused_classifier.cuh:96-117](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/fused_classifier.cuh#L96-L117)：dlogits 计算，招牌公式 `dlogit = (prob - indicator) * dloss`（[llmc/fused_classifier.cuh:107](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/fused_classifier.cuh#L107)）。这正是 u2-l6 推导过的融合反向结论 \(\partial L/\partial x_k = p_k - \mathbf{1}_{k=\text{target}}\)，再乘 `dloss = 1/(B \cdot T)`（训练）或 `1/(B \cdot T \cdot \text{grad\_accum})`（梯度累积）。注意它**用第二遍读 logits 重算 prob**（[llmc/fused_classifier.cuh:100](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/fused_classifier.cuh#L100) 注释「this is the 2nd read of logits」），赌它还在 L2 cache——从而彻底免去物化 probs。

模板参数 `WriteDLogits`：验证时传 `False`（只算损失、不改 logits，[train_gpt2.cu:778](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L778)），训练时传 `True`（把 logits 原地改成 dlogits，[train_gpt2.cu:822](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L822)）。`WriteProbs` 默认 `False`，仅在推理/调试时才真的写出 probs。

[llmc/fused_classifier.cuh:139-149](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/fused_classifier.cuh#L139-L149)：启动器，grid_size = B*T（每行一个 block），block_size = 1024。

#### 4.2.4 代码实践

**实践目标**：理解「融合」如何把多次显存往返压成一次，并能在源码里分清 fused_classifier 的「前向部分」与「反向部分」。

**操作步骤**：

1. 在 [llmc/fused_classifier.cuh:96-117](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/fused_classifier.cuh#L96-L117) 找到写 dlogits 的循环，确认它读的是 `logits`（第二遍）、写回的也是 `logits`（原地更新）。
2. 对比 `gpt2_validate` 与 `gpt2_backward_and_reduce` 两次调用：[train_gpt2.cu:778](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L778) 传 `False`、[train_gpt2.cu:822](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L822) 传 `True`。
3. 阅读文件头注释 [llmc/fused_classifier.cuh:1-6](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/fused_classifier.cuh#L1-L6)：它把三个事实写得很清楚——前向算交叉熵、不物化完整 logits 归一化（只在 target 处取）、同时启动反向。

**需要观察的现象 / 预期结果**：

- 不带 fused_classifier 时，前向需要物化 `probs(B,T,Vp)`、反向再读它算 dlogits，是两次 \(B \cdot T \cdot V_p\) 的显存往返；融合后这两次都被吸收进 kernel 内部对 `logits` 的就地读写，probs 缓冲根本不分配（`fused_classifier` 的 `probs` 参数在调用处传 `(floatX*)NULL`，见 [train_gpt2.cu:147](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L147) 附近的启动器实现——实际调用 [train_gpt2.cu:778](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L778) 未传 probs）。
- fused_residual_forward5 则把「residual 写出 → layernorm 读回」压成「residual 写出的同时，在共享内存里接力算 layernorm」。若本地有 GPU，可在 ncu（见 u7-l3）里对比该 kernel 与「residual_forward + layernorm_forward」两次启动的 DRAM 吞吐差异。无 GPU 则标注「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：fused_residual_forward5 既然融合了，为什么还要把 `residual` 写到全局内存？

> **答案**：因为 LayerNorm 反向需要原始输入（即残差相加的结果）来算 `dinp`（见 u2-l2、u5-l4）。融合省的是「前向里 residual 写出后、立即被 layernorm 读回」这一次的读，而不是省掉 residual 本身的保存——反向还要用它。

**练习 2**：fused_classifier 里 `__syncthreads()`（[llmc/fused_classifier.cuh:92](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/fused_classifier.cuh#L92)）如果删掉会怎样？

> **答案**：会出数据竞争。算损失（[llmc/fused_classifier.cuh:84](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/fused_classifier.cuh#L84)）读的是原始 logits，而写 dlogits（[llmc/fused_classifier.cuh:107](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/fused_classifier.cuh#L107)）把 logits 原地改成 \((p-\text{onehot})\cdot\text{dloss}\)。若不同步，threadIdx.x==0 算损失时可能读到已被改写、范围在 \([-1,1]\) 的值，叠在 `sp.Offset`（可能 < -90）上会算出 `exp(90+)` 而溢出。注释 [llmc/fused_classifier.cuh:88-92](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/fused_classifier.cuh#L88-L92) 记录的就是这个真实 bug。

### 4.3 global_norm 与梯度裁剪

#### 4.3.1 概念说明

训练大模型时，偶尔会出现某个 batch 让梯度突然变得很大（loss spike）。如果直接拿这种梯度去更新参数，一步就可能把模型推飞、再也回不来。**梯度裁剪**（gradient clipping）是标准的护栏：限制整个梯度向量的「长度」（L2 范数）不超过一个阈值，超了就等比例缩小。

llm.c 用的是「按全局范数裁剪」（clip-by-global-norm）：

- 先算出全部参数梯度拼成的大向量 \(\mathbf{g}\) 的 L2 范数 \(\|\mathbf{g}\|_2 = \sqrt{\sum_i g_i^2}\)。
- 设阈值 \(\tau\)（llm.c 取 1.0）。若 \(\|\mathbf{g}\|_2 > \tau\)，则把整个梯度向量乘以 \(\tau / \|\mathbf{g}\|_2\)；否则不缩放。

数学上，用一个标量 `grad_scale` 把裁剪塞进 AdamW：

\[
\text{grad\_scale} = \begin{cases} \tau / \|\mathbf{g}\|_2 & \text{若 } \|\mathbf{g}\|_2 > \tau \\ 1 & \text{否则} \end{cases}
\]

更新时每个参数先用 `grad_scale` 缩放自己的梯度（见 4.3.3 的 adamw 落点）。这样既实现了裁剪，又不需要真的去改写整个梯度缓冲——只传一个标量。

#### 4.3.2 核心流程

```text
【1. 算平方和】 global_norm_squared_kernel（2D grid，确定性、无 atomicAdd）：
  每个线程：grid-stride 累加 g_i^2 → blockReduce → 得到本 block 的部分和
  每个 block：把部分和写到 out[blockIdx.y * gridDim.x + blockIdx.x]（唯一槽位，无需原子）
  blockIdx.y = num_slices：允许一次启动同时处理多个张量/多层，互不干扰

【2. 汇总部分和】 global_sum_deterministic（在 train_gpt2.cu 里调用）：
  把所有 block 写出的部分和确定性求和到 out[0]
  （多卡时再 ncclAllReduce 把各卡的平方和加起来）

【3. 回 CPU、开根号】 gpt2_calculate_grad_norm 返回 sqrt(out[0])

【4. 算 grad_scale】 main 训练循环：
  grad_clip = 1.0
  grad_scale = (grad_norm > grad_clip) ? grad_clip / grad_norm : 1.0

【5. 喂给 AdamW】 gpt2_update(..., grad_scale, ...) → adamw_update kernel：
  grad = grad_scale * grads[idx]   # 每个参数的梯度在更新前被缩放
```

这里有个**确定性归约**（deterministic reduction）的设计要点：浮点加法不满足结合律，`atomicAdd` 的累加顺序取决于线程调度，跨两次运行可能得到不同结果——对复现实验（regression test、可复现训练）是致命的。所以 llm.c 故意避开 `atomicAdd`：让每个 block 写到一个**唯一**的输出槽（由 `(blockIdx.x, blockIdx.y)` 决定下标），再用一个单独的、顺序确定的归约 kernel 求和。代价是「两个 kernel」而不是「一个带原子的 kernel」，换来的是逐位可复现。

#### 4.3.3 源码精读

**(1) global_norm kernel**

[llmc/global_norm.cuh:14-24](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/global_norm.cuh#L14-L24)：`global_norm_squared_for_range`，每个线程 grid-stride 累加 \(g_i^2\)，再用 `blockReduce<warpReduceSum>` 归约到 block 级。

[llmc/global_norm.cuh:26-36](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/global_norm.cuh#L26-L36)：`global_norm_squared_kernel`，二维 grid：

```c
float block_sum = global_norm_squared_for_range(data + blockIdx.y * stride, count);
if(threadIdx.x == 0) {
    size_t out_index = blockIdx.y * gridDim.x + blockIdx.x;
    out[out_index] = out[out_index] + block_sum;   // 唯一槽位，非原子
}
```

注释 [llmc/global_norm.cuh:29-31](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/global_norm.cuh#L29-L31) 明说：「我们想避免 atomic add，所以把它和另一个求和 kernel 组合起来」。`blockIdx.y`（`num_slices`）让多层/多张量能在同一次启动里各自归约、互不写冲突。

[llmc/global_norm.cuh:38-46](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/global_norm.cuh#L38-L46)：还定义了一个 `global_norm_aggregate_kernel`，但当前主线用的是 [train_gpt2.cu:1019](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L1019) / [train_gpt2.cu:1028](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L1028) 调用的 `global_sum_deterministic` 来做最终汇总。

[llmc/global_norm.cuh:68-89](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/global_norm.cuh#L68-L89)：启动器 `global_norm_squared`。两个关键决定：

- grid 大小取「刚好铺满 SM」（[llmc/global_norm.cuh:76](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/global_norm.cuh#L76)），刻意不用 `CEIL_DIV`——注释 [llmc/global_norm.cuh:72-75](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/global_norm.cuh#L72-L75) 解释：少一个 block 只是微小性能损失，多一个 block 却是灾难（它只能等所有其它 block 结束才能启动）。
- `reset` 参数（[llmc/global_norm.cuh:84-86](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/global_norm.cuh#L84-L86)）：第一次调用时 memset 清零输出缓冲，后续调用累加进同一个缓冲。

**(2) 在模型层算 grad norm**

[train_gpt2.cu:992-1033](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L992-L1033)：`gpt2_calculate_grad_norm`。

- 它复用 `acts.output` 缓冲来写平方和（[train_gpt2.cu:997](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L997)），因为算 grad norm 时前向已经结束、output 缓冲空闲——又一次「缓冲复用」。
- 分两种情况：ZeRO Stage 1（[train_gpt2.cu:1002-1023](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L1002-L1023)）下每张卡只持有自己分片的梯度，需要逐张量、逐分片算后再 `ncclAllReduce` 跨卡求和；普通 DDP（[train_gpt2.cu:1024-1029](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L1024-L1029)）下梯度已经 all-reduce 平均过，每卡直接算全量平方和即可。
- 最后 [train_gpt2.cu:1031](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L1031) `grad_norm = sqrtf(grad_norm_squared)`。

**(3) 算 grad_scale 并喂给 AdamW**

[train_gpt2.cu:1850-1852](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L1850-L1852)：

```c
float grad_clip = 1.0f;
float grad_scale = (grad_norm > grad_clip) ? grad_clip / grad_norm : 1.0f;
gpt2_update(&model, step_learning_rate, 0.9f, 0.95f, 1e-8f, weight_decay, grad_scale, step+1, &multi_gpu_config);
```

注意这行还顺带实现了「跳过坏更新」的护栏：[train_gpt2.cu:1844-1848](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L1844-L1848) 用 loss/grad norm 的 z-score 检测离群，若 loss 飙得太高就直接跳过这一步更新（比裁剪更激进的防护）。

**裁剪的真正落点**在 AdamW kernel 第一行：[llmc/adamw.cuh:26](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/adamw.cuh#L26) `float grad = grad_scale * (float)grads_memory[idx];`——每个参数在进入 AdamW 的 m/v 更新之前，梯度先被这个标量缩放。grad_scale 经 `gpt2_update`（[train_gpt2.cu:1101-1105](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L1101-L1105)）透传进 `adamw_update`。

#### 4.3.4 代码实践

**实践目标**：跟踪「梯度范数 → grad_scale → adamw 第一行」这条链，确认裁剪是用一个标量实现的，而不是改写整个梯度缓冲。

**操作步骤**：

1. 跳到 [train_gpt2.cu:1841](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L1841)，看 `grad_norm` 如何由 `gpt2_calculate_grad_norm` 算出。
2. 跟着 [train_gpt2.cu:1850-1852](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L1850-L1852) 看清 grad_scale 的三元表达式。
3. 进入 `gpt2_update`（[train_gpt2.cu:1035](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L1035)），顺着 [train_gpt2.cu:1101-1105](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L1101-L1105) 看 grad_scale 被传给 `adamw_update`。
4. 最后在 [llmc/adamw.cuh:26](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/adamw.cuh#L26) 确认 `grad = grad_scale * grads[idx]`。

**需要观察的现象 / 预期结果**（待本地验证）：

- 正常训练步 `grad_norm` 通常小于 1.0，此时 `grad_scale == 1.0`，裁剪不生效，日志（[train_gpt2.cu:1872](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L1872) 打印的 `norm` 字段）能看到它的值。
- 偶尔 loss spike 时 `grad_norm` 会突然变大（比如几十、上百），此时 `grad_scale = 1.0 / grad_norm` 远小于 1，把这一步的更新幅度压回去。
- 若想观察裁剪是否触发，可在 [train_gpt2.cu:1851](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L1851) 后临时加一行 `printf` 打印 `grad_scale`（**只读调试，不修改源码逻辑**），或在日志里对照 `norm` 列与 1.0 的大小。无 GPU 则标注「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：为什么 llm.c 用「逐参数乘标量 grad_scale」来实现裁剪，而不是真的去遍历梯度缓冲把它们都缩小一遍？

> **答案**：因为裁剪是「等比例缩放」，比例是个标量 \(\tau/\|\mathbf{g}\|\)。AdamW kernel 本来就要逐参数遍历（算 m、v、更新 θ），只需在那趟循环里多乘一个 grad_scale（[llmc/adamw.cuh:26](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/adamw.cuh#L26)），零额外显存往返、零额外 kernel。单独再扫一遍梯度缓冲反而多一次 \(O(\text{参数量})\) 的读写。

**练习 2**：`global_norm_squared_kernel` 为什么用二维 grid、且每个 block 写到唯一槽位而不是 `atomicAdd`？

> **答案**：为了**确定性**。`atomicAdd` 的累加顺序随线程调度变化，浮点加法又不满足结合律，会导致 grad norm 逐位不可复现。改成「每个 (blockIdx.x, blockIdx.y) 写唯一下标」（[llmc/global_norm.cuh:32-35](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/global_norm.cuh#L32-L35)）再由顺序确定的归约 kernel 汇总，就能逐位复现。`blockIdx.y`（num_slices）顺便让多层/多张量能在一次启动里并行处理而不写冲突。

## 5. 综合实践

把本讲三个模块串成一个「显存账 + 安全网」的小调研。仍以 GPT-2 124M（\(L=12, C=768\)）、BF16、设 \(B=4, T=1024\) 为例：

1. **显存账（recompute）**：按 4.1.4 的方法，分别算出 `recompute=0/1/2` 三档下，`fch_gelu`、`ln1`、`ln2` 三类激活合计占多少 MiB。指认 [train_gpt2.cu:243](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L243)、[train_gpt2.cu:226](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L226)、[train_gpt2.cu:238](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L238) 三条尺寸表达式，说明「省下的那部分显存」在反向里是用 [train_gpt2.cu:898](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L898)、[train_gpt2.cu:903](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L903)、[train_gpt2.cu:921](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L921) 三次重算换回来的。

2. **融合收益（fusion）**：在 `gpt2_forward` 里统计 `fused_residual_forward5` 的调用次数（[train_gpt2.cu:734](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L734)、[train_gpt2.cu:744-749](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L744-L749)），说明它每次省下的是哪个 \(B \cdot T \cdot C\) 中间缓冲的「写后再读」；再说明 `fused_classifier`（[train_gpt2.cu:778](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L778) / [train_gpt2.cu:822](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L822)）省下了哪个 \(B \cdot T \cdot V_p\) 的 probs 缓冲。

3. **安全网（grad clip）**：画出从 `gpt2_calculate_grad_norm`（[train_gpt2.cu:992](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L992)）到 `adamw_update` 的 `grad = grad_scale * grads[idx]`（[llmc/adamw.cuh:26](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/adamw.cuh#L26)）的完整调用链，标出 grad_norm 在哪里开根号、grad_scale 在哪里用三元判断算出、最终在哪里作用到每个参数。

**验收**：能用自己的话讲清「recompute 省的是哪块显存、付出的是什么；融合省的是哪种往返；grad_scale 为什么是一个标量就够」这三件事。

## 6. 本讲小结

- `recompute`（0/1/2）是显存-算力的三档开关：默认 `1` 把最占显存的 `fch_gelu` 从 \(L\) 份缩成 1 份，反向时用 `fch`（pre-gelu）重算（[train_gpt2.cu:713](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L713) 指针切换、[train_gpt2.cu:898](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L898) 重算）；`=2` 再对 `ln1`/`ln2` 如法炮制。
- 融合算子的收益来自**减少显存往返**而非减少 FLOP：`fused_residual_forward5` 把残差相加结果留在共享内存里接力做 LayerNorm；`fused_classifier` 把 softmax+交叉熵+dlogits 吸收进单 kernel 且从不物化 probs。
- `fused_classifier` 用 online softmax（running max + running sum）只取目标处概率算损失，再用招牌公式 `dlogit = (prob - indicator)*dloss` 把 logits 原地改写成 dlogits，前后向各算一次、读两遍 logits（赌 L2 命中）。
- `global_norm` 用「每个 block 写唯一槽位 + 单独确定性归约」避开 `atomicAdd`，换取 grad norm 的逐位可复现；`blockIdx.y`（num_slices）让多层并行归约不冲突。
- 梯度裁剪是「按全局范数」：`grad_scale = (grad_norm > 1.0) ? 1.0/grad_norm : 1.0`，只传一个标量；真正作用点在 AdamW kernel 第一行 `grad = grad_scale * grads[idx]`，零额外显存写回。
- 这三件事共同把训练推向「显存够用、kernel 数少、数值稳定且可复现」，是 CUDA 主线能在大模型上稳定跑起来的工程基础。

## 7. 下一步学习建议

- 接着学 **u6-l3（学习率调度、MFU 与日志）**：它会讲解 `get_learning_rate` 如何决定本讲的 `step_learning_rate`，以及训练日志里 `norm`（本讲的 grad_norm）那一列是怎么打出来的。
- 若想深入 kernel 内部的极致优化（如 `layernorm_forward_kernel6` 用 cooperative groups / 共享内存、`layernorm_backward_kernel10` 的多 warp 归约），转到 **u7-l1（dev/cuda 内核库）**，那里有 `layernorm_forward.cu` 的 kernel1~6 演进与 benchmark。
- 若对「确定性归约」「多卡 grad norm 聚合」感兴趣，可先读 **u6-l4（多 GPU / ZeRO / NCCL）**，理解 ZeRO Stage 1 下 `gpt2_calculate_grad_norm` 为何要逐分片算再 `ncclAllReduce`。
