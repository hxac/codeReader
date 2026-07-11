# 二次开发与复现训练：scripts 与 Llama3 扩展

## 1. 本讲目标

本讲是「内核库、评测、剖析与二次开发」单元（Unit 7）的最后一篇，也是整本手册从「读懂 llm.c」走向「用 llm.c 做事」的转折点。学完后你应当能够：

- 读懂 `scripts/run_*.sh` 这些复现脚本里的每一个命令行参数，并能根据自己的 GPU 数量、显存、数据量改写出自己的训练命令。
- 理解「模型规模描述符」（descriptor）机制：一条 `-e "d12"` 或 `-e "gpt3:c768"` 字符串如何决定层数 L、通道数 C、头数 NH 与上下文长度 T。
- 看懂 `train_llama3.py` 相对 `train_gpt2.py` 改了哪三处核心（RoPE / GQA / SwiGLU），从而理解「把 llm.c 的 GPT-2 骨架扩展到一个新架构」需要动哪些地方。

本讲默认你已经学完 u6（训练工程：混合精度、融合算子、学习率调度、ZeRO 多卡、多节点）。我们会反复用到 `-d`、`-b`、`-t`、`-z`、`-r`、`-q` 这些标志，它们的确切含义都在 u6 里讲过，这里只关注如何把它们组合成一条真实的复现命令。

## 2. 前置知识

在进入源码前，先用通俗语言澄清三个最容易混淆的点。

**(1) `-d` 不是描述符，`-e` 才是。** 这是本讲最大的一个「坑」。命令行里有两个长得很像的标志：

- `-d <int>` 是 **total_batch_size（全局 batch 的 token 总数）**，是一个整数。
- `-e <string>` 是 **load_filename**，它既可以是一个 `.bin` 权重文件名，也可以是一个「描述符」字符串（如 `d12`、`gpt3:c768`）。

所以「`-d` 描述符如何决定层数与通道」这个说法其实把两件事混到了一起：决定层数与通道的是 `-e` 里的描述符，`-d` 只决定 batch 大小。本讲 4.2 节会专门讲描述符，4.1 节讲 `-d`。

**(2) 「复现」= 用同样的数据、同样的模型规模、同样的超参跑出接近的 loss 曲线。** llm.c 的 `scripts/` 目录里放的不是「教学 demo」，而是作者用来**真正复现 GPT-2 / GPT-3 训练**的逐字命令。脚本顶部注释甚至算好了要花多少美元（GPT-2 124M 在 8×A100 上约 \$20、约 94 分钟）。

**(3) 「能力度」（capability）= 6ND。** 脚本注释里反复出现的 `6 * 124e6 * 10e9` 是 Chinchilla/scaling-laws 里的经验式：一个训练 run 的总计算量约等于 \(6 \times N \times D\)，其中 N 是参数量、D 是训练 token 数。它不是模型本身的属性，而是「这次训练用了多少算力」的度量，常用来横向比较不同 run。

\[ \text{FLOPs} \approx 6 \cdot N \cdot D \]

**(4) 新架构 ≠ 重写一切。** 从 GPT-2 扩展到 Llama3，骨架（embedding → L 个 Transformer block → 最终 norm → lm_head）几乎不变，真正改动的是 block 内部的三件事。`train_llama3.py` 就是用最小改动展示这条扩展路径。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [scripts/run_gpt2_124M.sh](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/scripts/run_gpt2_124M.sh) | 8×A100 上复现 GPT-2 124M（10B token、FineWeb）的逐字命令 |
| [scripts/run_gpt3_125M.sh](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/scripts/run_gpt3_125M.sh) | 8×A100 上复现 GPT-3 125M（300B token、上下文 2048）的逐字命令 |
| [scripts/README.md](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/scripts/README.md) | 解释显存不足时如何调 `-r`/`-b`/`-t`，以及单卡/多卡/多节点如何改命令 |
| [train_gpt2.cu](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu) | CUDA 主线。本讲重点读 `gpt2_set_hyperparameters` / `gpt3_set_hyperparameters` / `gpt_build_from_descriptor`，以及 `main` 里的命令行解析与模型构建分派 |
| [train_llama3.py](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_llama3.py) | Llama3.1 的 PyTorch 参考实现，展示 RoPE/GQA/SwiGLU 三处扩展，并把权重写成 `.bin` 供 C 端读取 |

## 4. 核心概念与源码讲解

### 4.1 复现训练脚本：scripts/run_*.sh

#### 4.1.1 概念说明

`scripts/` 下的每个 `run_*.sh` 都是一个**自包含的训练配方**：它假设你已经下好数据、编译好 `train_gpt2cu`，然后给你一条 `mpirun ... ./train_gpt2cu <一大串参数>` 的命令，外加一个 `while true` 的**断点续训循环**。

为什么要套循环？因为跑 300B token 的训练要 24 小时，中途进程可能因为节点掉线、GPU 报错等原因退出。脚本用一个 `done_file`（如 `log_gpt2_124M/DONE_00018865`）作为「完成标志」：训练正常跑完最后一步时，程序会写出这个 `DONE_步数` 文件；脚本每次启动前先检查它是否存在，存在就退出，否则就再跑一次 `train_gpt2cu`（靠 `-y 1` 自动从最新 checkpoint 续训）。

#### 4.1.2 核心流程

一个复现脚本的核心流程可以用下面这段伪代码概括：

```
make train_gpt2cu USE_CUDNN=1          # 先编译（启用 cuDNN Flash Attention）
out_dir="log_gpt2_124M"
done_file="$out_dir/DONE_00018865"     # 完成标志：步数写进文件名

while true:
    if exists(done_file): break        # 跑完了，退出

    mpirun -np 8 ./train_gpt2cu \      # 8 个进程（对应 8 张卡）
        -i <训练数据 .bin 通配符> \
        -j <验证数据 .bin 通配符> \
        -o $out_dir \                  # 日志与 checkpoint 输出目录
        -e "d12" \                     # 模型描述符（决定 L/C/NH/T）
        -b 64 -t 1024 \                # 每卡 micro-batch 与序列长度
        -d 524288 \                    # 全局 batch（token 总数）
        -l 0.0006 -u 700 -q 0.0 \      # 学习率 / warmup / 最终衰减比例
        -z 1 -r 0 \                    # ZeRO stage 与 recompute
        -y 1                           # 断点续训：从最新 checkpoint 恢复
    sleep 1
```

理解这条命令的关键，是分清四组参数的分工：

- **数据**：`-i`/`-j`/`-o`（训练集、验证集、输出目录）。
- **模型规模**：`-e`（描述符，见 4.2）。
- **batch 形状**：`-b`（每卡 micro-batch）、`-t`（序列长度）、`-d`（全局 batch）。这三者加上卡数共同决定梯度累积步数。
- **优化与显存**：`-l`/`-u`/`-q`/`-c`（lr、warmup、衰减、权重衰减）、`-z`（ZeRO）、`-r`（recompute）、`-w`（master weights）。

#### 4.1.3 源码精读

先看 GPT-2 124M 脚本顶部的「成本核算」与续训循环骨架，注释把训练规模与硬件开销算得清清楚楚：

[scripts/run_gpt2_124M.sh:1-19](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/scripts/run_gpt2_124M.sh#L1-L19) — 注释里 `6 * 124e6 * 10e9 = 7.44e18` 就是上文说的 6ND 能力度；`18865 步 × 524288 token/步 ≈ 10B token`；`while true` 循环靠 `done_file` 判断是否已完成。

接着是真正的训练命令，这是本讲最重要的一段：

[scripts/run_gpt2_124M.sh:23-39](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/scripts/run_gpt2_124M.sh#L23-L39) — `mpirun -np 8` 启动 8 个进程；`-b 64 -t 1024 -d 524288`；`-l 0.0006 -q 0.0 -u 700`；`-z 1 -r 0`；`-e "d12"`；`-y 1`。

这条命令里每一个标志在 `train_gpt2cu` 的 `error_usage()` 帮助文本里都有定义。对照看几个关键的：

[train_gpt2.cu:1379-1383](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L1379-L1383) — 帮助文本明确写着 `-b` 是「per-GPU, micro batch size B」、`-t` 是 sequence length、`-d` 是 total desired batch size（默认 `B*T*num_processes`，即不做梯度累积）。**这段帮助文本是「`-d` 不是描述符」的最权威证据**。

命令行解析在 main 里是一个「穷人版 argparse」——逐对读取 `flag, value`：

[train_gpt2.cu:1464-1472](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L1464-L1472) — 注意 `argv[i][1] == 'e'` 把值赋给 `load_filename`（这才是描述符的入口），而 `argv[i][1] == 'd'` 把值赋给 `total_batch_size`。两者完全不同。

接下来是「梯度累积步数」的计算，这是把 `-b`/`-t`/`-d` 与卡数串起来的关键公式：

[train_gpt2.cu:1512-1519](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L1512-L1519) — `tokens_per_fwdbwd = B * T * num_processes` 是「一次前向+反向处理多少 token」；`grad_accum_steps = total_batch_size / tokens_per_fwdbwd`。以 GPT-2 脚本为例：\(64 \times 1024 \times 8 = 524288\)，恰等于 `-d 524288`，所以 `grad_accum_steps = 1`（不累积）。如果你只有 1 张卡，同样的 `-d 524288` 会让 `grad_accum_steps = 524288 / (64×1024×1) = 8`——这就是 `scripts/README.md` 说的「单卡也能跑，结果相同，只是慢 8 倍」。

最后，显存吃紧时怎么调？官方建议在 `scripts/README.md` 里：

[scripts/README.md:13-17](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/scripts/README.md#L13-L17) — 先试 `-r 1`（重算 GELU，省显存换算力）；还不行就把 micro-batch `-b` 减半（64→32→16→8），同时自动增多梯度累积来维持同样的 `-d`；再不行就缩短上下文 `-t`。

#### 4.1.4 代码实践

**实践目标**：对比 `run_gpt2_124M.sh` 与 `run_gpt3_125M.sh` 在 batch 形状、学习率与步数上的差异，理解 GPT-3 复现为什么要那样改参数。

**操作步骤**：

1. 打开两个脚本，逐行比对 `mpirun` 之后的参数。
2. 用计算器（或心算）算出两者的 `tokens_per_fwdbwd` 与 `grad_accum_steps`。
3. 回答下面表格里的问题。

**需要观察的现象 / 预期结果**：

把两个脚本的关键参数填入下表（这些值都直接来自源码）：

| 参数 | GPT-2 124M (`run_gpt2_124M.sh`) | GPT-3 125M (`run_gpt3_125M.sh`) | 说明 |
| --- | --- | --- | --- |
| `-b`（每卡 micro-batch） | 64 | 32 | GPT-3 减半 |
| `-t`（序列长度） | 1024 | 2048 | GPT-3 翻倍，对应 GPT-3 的 2048 上下文 |
| 每卡 token 数 B·T | 65536 | 65536 | **完全相同** |
| `-d`（全局 batch） | 524288 | 524288 | 相同（8 卡 → grad_accum=1） |
| `-l`（学习率） | 0.0006 | 0.0006 | 相同 |
| `-u`（warmup） | 700 | 700 | 相同 |
| `-q`（最终 lr 衰减比例） | 0.0（衰减到 0） | 0.1（衰减到 10%） | GPT-3 保留一点尾 lr |
| `-x`（max_steps） | 未设（=−1，跑 1 个 epoch≈18865 步） | 572204（显式） | GPT-2 跑完一遍数据；GPT-3 要多轮 epoch |
| `-e`（描述符） | `d12` | `gpt3:c768` | 决定模型规模，见 4.2 |
| `-n`（checkpoint 频率） | 5000 | 10000（且 `-nk 5 -nm 50000`） | GPT-3 跑得久，要轮转保留 checkpoint |

核心结论：**GPT-3 把上下文从 1024 翻倍到 2048，为了让每卡 token 数和全局 batch 不变，必须把 micro-batch 从 64 减半到 32**。这是一种「显存预算守恒」——更长的序列更费显存，只能用更小的 batch 来换。同时因为 GPT-3 要训 300B token（GPT-2 只训 10B），`max_steps` 从「跑一遍数据」变成显式的 572204 步，学习率衰减也从「衰减到 0」改成「衰减到 10%」以支持更长训练。

（注：本实践是源码阅读型，无需运行；若你真的想跑，需要 8×A100 80GB 与 FineWeb 数据，详见 `dev/data/fineweb.py`。）

#### 4.1.5 小练习与答案

**练习 1**：如果你只有 1 张 A100，想用 `run_gpt2_124M.sh` 的参数训练，但显存不够，应该怎么改？

**参考答案**：把 `mpirun -np 8 ./train_gpt2cu` 改成 `./train_gpt2cu`（`scripts/README.md` 的单卡改法），保持 `-d 524288` 不变——此时 `grad_accum_steps` 会自动变成 8（`524288/(64×1024×1)`），结果数学等价、只是慢 8 倍。若仍 OOM，按 README 建议先 `-r 1`，再把 `-b` 从 64 减到 32/16。

**练习 2**：`-d 524288` 这个数字是怎么来的？

**参考答案**：\(524288 = 2^{19} = 64 \times 1024 \times 8\)，正好是「8 卡 × 每卡 micro-batch 64 × 序列长 1024」，让 `grad_accum_steps=1`。它是一个有意选成 2 的幂、且能被 `B*T*num_processes` 整除的全局 batch（`train_gpt2.cu` 第 1518 行有 `assert(total_batch_size % tokens_per_fwdbwd == 0)`）。

**练习 3**：`while true` 循环里为什么要 `sleep 1`？

**参考答案**：防止 `train_gpt2cu` 立刻崩溃时陷入「疯狂重启」的死循环，给系统一口气；同时 `done_file` 检查保证训练真正完成后能退出循环。

---

### 4.2 模型规模描述符：gpt_build_from_descriptor

#### 4.2.1 概念说明

「描述符」（descriptor）是 llm.c 的一个便利设计：**不提供 `.bin` 权重文件，而是提供一个短字符串，程序就按字符串指定的规模随机初始化一个模型**。这让你不用预先准备权重文件就能从零训练任意规模的 GPT-2/GPT-3。

描述符通过 `-e` 传入（与 `.bin` 文件共用同一个标志），有三种合法前缀：

- `dX`（legacy）：GPT-2，X 是**层数**，如 `d12` = GPT-2 small（124M）。
- `gpt2:dX`：同上，显式写法，如 `gpt2:d48` = GPT-2 xl（1558M）。
- `gpt3:cX`：GPT-3，X 是**通道数**，如 `gpt3:c768` = 最小的 GPT-3（125M）。

注意一个微妙区别：**GPT-2 用层数索引规模，GPT-3 用通道数索引规模**。原因是 GPT-3 各档的层数不是一一对应的（同一层数可能对应多档），用通道数更稳定。

#### 4.2.2 核心流程

描述符的解析与分派分三步：

```
读 -e 的值 load_filename
  ├─ 若 -y 1 且找到 checkpoint  → gpt2_build_from_checkpoint（续训）
  ├─ 若以 .bin 结尾              → gpt2_build_from_checkpoint（加载权重）
  └─ 否则（是描述符）            → gpt_build_from_descriptor（随机初始化）
                                    ├─ "dX" / "gpt2:dX" → gpt2_set_hyperparameters(depth)
                                                    └─ 查表得 channels/num_heads, max_seq_len=1024
                                                    ├─ "gpt3:cX"      → gpt3_set_hyperparameters(channels)
                                                                    └─ 查表得 depth/head_size, num_heads=C/hs, max_seq_len=2048
                                                    └─ 统一设 vocab=50257, padded_vocab=50304
                                                    └─ 分配权重 + 随机初始化（seed 42，对齐 PyTorch）
```

查表是「描述符如何决定层数与通道」的核心：代码里硬编码了若干档位，每档对应一组 `(层数 L, 通道 C, 头数 NH)`。

#### 4.2.3 源码精读

先看 GPT-2 的查表函数，它按**层数**决定通道与头数：

[train_gpt2.cu:519-536](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L519-L536) — 比如 `depth==12` 这一行 `{ channels = 768; num_heads = 12; }` 就是 GPT-2 small（124M）；最后统一设 `max_seq_len = 1024`。这张表从 30M（d6）一路列到 12.2B（d84）。

再看 GPT-3 的查表函数，它按**通道数**决定层数与 head_size：

[train_gpt2.cu:538-560](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L538-L560) — `channels==768` 这一行 `{ depth = 12; head_size = 64; }` 即最小的 GPT-3（125M，对应脚本里的 `gpt3:c768`）；`num_heads = channels / head_size`；统一设 `max_seq_len = 2048`。注释特别说明：这里的 GPT-3 用的是 **dense attention**，不是真正 GPT-3 的「稠密+带状交替注意力」，所以「不必然与 GPT-3 完全一致」。

把三种前缀分派到上面两个函数的，是总入口 `gpt_build_from_descriptor`：

[train_gpt2.cu:562-586](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L562-L586) — 函数顶部注释把三种合法格式写得很清楚；第 571-579 行用前缀判断分派；第 582-583 行统一设 `vocab_size=50257`、`padded_vocab_size=50304`（对齐到 128，详见 u4-l2）；第 585 行 `gpt2_allocate_weights` 按刚设好的 config 分配权重内存。

紧接着是「随机初始化」。**这里有一个关键细节**：初始化用 `mt19937` 随机数生成器、种子固定 42，并且严格按 PyTorch 的张量顺序填值——目的是让 C 端随机初始化的模型能与 PyTorch 参考实现**逐位一致**，从而复用同一套 debug state 正确性测试（见 u3-l4）：

[train_gpt2.cu:587-641](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L587-L641) — 注释写明 `weights ~N(0, 0.02), biases 0, c_proj weights ~N(0, 0.02/(2*L)**0.5)`；第 590-591 行 `manual_seed(&init_rng, 42)`；残差投影权重额外缩放 `residual_scale = 1/sqrt(2L)` 以提升深层训练稳定性。

最后，main 里「是加载 `.bin` 还是解析描述符」的三路分派：

[train_gpt2.cu:1574-1586](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L1574-L1586) — `resuming==1` 优先从 checkpoint 恢复；否则 `ends_with_bin(load_filename)` 判断是不是 `.bin` 文件（是则加载权重）；都不是才走 `gpt_build_from_descriptor`。**这就是「`-e` 既能传文件名又能传描述符」的实现原理：靠后缀 `.bin` 区分**。

#### 4.2.4 代码实践

**实践目标**：亲手把 `d12` 和 `gpt3:c768` 两个描述符「翻译」成具体的模型形状，验证它们确实对应脚本注释里的 124M / 125M。

**操作步骤**：

1. 打开 [train_gpt2.cu:519-560](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L519-L560)。
2. 对 `d12`：从 GPT-2 表读出 `L=12, C=768, NH=12, T=1024`。
3. 对 `gpt3:c768`：从 GPT-3 表读出 `L=12, C=768, head_size=64 → NH=12, T=2048`。
4. 用 GPT-2 的参数量经验公式估算参数量。

**需要观察的现象 / 预期结果**：

GPT-2 参数量近似公式（忽略 LayerNorm 等小项）：

\[ N \approx V\!\cdot\!C \;(\text{wte}) \;+\; L\!\cdot\!\left[\,C^2\!\cdot\!12 \;(\text{qkv+proj+fc+fcproj 四个 } C^2 \text{ 级矩阵})\,\right] \]

代入 `d12`：\(V=50257, C=768, L=12\)。仅 wte 一项约 \(50257\times768\approx 38.6\text{M}\)；每层 Transformer 约 \(12 \times 768^2 \approx 7.08\text{M}\)，12 层约 85M；加上 wpe(\(1024\times768\)) 等小项，**总计约 124M**——与脚本注释「gpt2 (124M)」吻合。

而 `gpt3:c768` 与 `d12` 的**模型形状几乎一模一样**（都是 L=12, C=768, NH=12），唯一差别是 `max_seq_len` 从 1024 变成 2048。这也解释了为什么两个脚本跑的模型参数量都是 ~124-125M，但 GPT-3 版本要把 `-t` 从 1024 改成 2048。

**预期结果**：你能用一句话说清「`-e "d12"` 决定了 L=12、C=768、NH=12、T=1024」，以及「描述符是通过 `-e`（不是 `-d`）传入、靠查 `gpt2/gpt3_set_hyperparameters` 这两张硬编码表来决定层数与通道的」。

#### 4.2.5 小练习与答案

**练习 1**：为什么 GPT-3 用通道数 `cX` 而不是层数 `dX` 来索引规模？

**参考答案**：见 [train_gpt2.cu:539](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L539) 注释——GPT-3 的层数与模型档位不是一一对应（比如 depth=24 同时对应 350M/760M/1.3B 三档），用通道数才能唯一确定一档。

**练习 2**：描述符初始化出来的模型，为什么能和 PyTorch 参考实现逐位一致？

**参考答案**：因为 [train_gpt2.cu:590-591](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L590-L591) 用了与 PyTorch 同款的 `mt19937` 生成器、固定种子 42，并严格按 PyTorch 的张量顺序填值。这让「C 端从描述符随机初始化」与「PyTorch 端随机初始化」产生完全相同的初值，从而可以共用 debug state 做正确性比对（承接 u3-l4）。

**练习 3**：如果我传 `-e gpt2_124M_bf16.bin`，会走哪条分支？

**参考答案**：走 `gpt2_build_from_checkpoint`（加载权重），因为它以 `.bin` 结尾，`ends_with_bin` 返回真——见 [train_gpt2.cu:1579-1581](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L1579-L1581)。只有不以 `.bin` 结尾的字符串才会被当成描述符。

---

### 4.3 架构扩展方向：train_llama3.py 与 Llama3

#### 4.3.1 概念说明

llm.c 的主线 `train_gpt2.cu` 只实现了 GPT-2 架构。如果你想训 Llama3，怎么办？`train_llama3.py` 给出了答案：**用 PyTorch 写一份新架构的参考实现，把它当作权重生产者与正确性标尺，权重的写出格式沿用 u4-l2 讲过的 `.bin` 协议（只是换了魔数、加了新字段）**。

文件顶部开宗明义地列出了 Llama3 相对 GPT-2 的**三处核心差异**：

[train_llama3.py:5-8](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_llama3.py#L5-L8)

1. **RoPE**（Rotary Positional Encoding，旋转位置编码）：用旋转矩阵编码相对位置，取代 GPT-2 的可学习位置嵌入 `wpe`。
2. **GQA**（Grouped Query Attention，分组查询注意力）：Key/Value 的头数少于 Query 的头数，省显存、提速。
3. **SwiGLU**（Swish-Gated Linear Unit）：MLP 的激活函数从 GELU 换成带门控的 SwiGLU。

此外还有两处次要但显眼的改动：用 **RMSNorm** 取代 LayerNorm；线性层**去掉 bias**。

#### 4.3.2 核心流程

把 GPT-2 扩展到 Llama3，block 内部的数据流对比是这样的：

```
GPT-2 block:                        Llama3 block:
  x                                 x
  ├ ln1 (LayerNorm)                 ├ ln_1 (RMSNorm)
  ├ QKV投影 (OC=3C)                 ├ c_attn投影 (OC=(NH+2*NKV)*hd，含GQA)
  ├ 注意力 (无位置编码的裸attn)      ├ apply_rotary_emb (RoPE 旋转 Q,K)
  ├ c_proj                          ├ 注意力 (repeat_kv 复制 KV)
  ├ + 残差                          ├ c_proj
  ├ ln2 (LayerNorm)                 ├ + 残差
  ├ fc → GELU → fcproj              ├ ln_2 (RMSNorm)
  └ + 残差                          ├ c_fc2(silu) ⊙ c_fc   (SwiGLU) → c_proj
                                    └ + 残差
```

扩展一个新架构在工程上要动三处：

1. **模型代码**：写新的 `CausalSelfAttention`/`MLP`/`Block`（含 RoPE、GQA、SwiGLU）。
2. **权重契约**：写新的 `write_tensors`（张量顺序变了，多了 `c_fc2` 和独立的 `lm_head`）与新的 `.bin` 头（新魔数、新 config 字段）。
3. **C 端 kernel**：为新算子（RoPE 旋转、GQA 广播、SwiGLU）写 CUDA 实现——这是 `train_llama3.py` 尚未完成、留给读者的部分（文件注释里 `TODO: add the actual commands`）。

#### 4.3.3 源码精读

**差异 1：RoPE。** Llama3 不再学一张位置嵌入表，而是预先算好「旋转角」`freqs_cis`（复数形式），在前向时把 Q、K 当成复数乘以对应位置的旋转因子：

[train_llama3.py:104-114](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_llama3.py#L104-L114) — `apply_rotary_emb` 把 Q/K reshape 成复数、乘以 `freqs_cis`、再变回实数。这是「相对位置编码」——两个 token 的相对距离决定了它们 Q·K 内积的旋转角度。

旋转角本身在模型构造时一次性预计算：

[train_llama3.py:116-125](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_llama3.py#L116-L125) — `precompute_freqs_cis` 按公式 `freq = 1/theta^(2i/d)` 生成频率、与位置 `t` 做外积、再表成复数 `e^{i·freq·t}`；`theta=500000` 是 Llama3 的基数（GPT-2/早期 Llama 用 10000）。

**差异 2：GQA。** 投影矩阵的输出通道数从 GPT-2 的 `3*C` 变成 `(NH + 2*NKV)*hd`——Query 仍是 NH 个头，但 Key/Value 只有 NKV 个头（Llama3 8B 是 NH=32、NKV=8）。注意力计算前要把 KV「复制」到与 Q 同样多：

[train_llama3.py:59-68](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_llama3.py#L59-L68) — `repeat_kv` 把每个 KV 头复制 `n_rep = NH/NKV` 次；当 `n_rep==1`（NKV=NH）时退化为普通多头注意力（MHA），这就是 GQA 的「退化端点」。

投影与切分在 `CausalSelfAttention.forward` 里：

[train_llama3.py:159-174](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_llama3.py#L159-L174) — `c_attn` 一次投影出 `(NH+2*NKV)*hd` 维，再 `split` 成 Q/K/V 三段（Q 最大、KV 各 NKV 个头）；第 174 行对 Q、K 施加 RoPE；推理时还有 KV cache（第 176-180 行）。注意所有线性层都 `bias=False`。

**差异 3：SwiGLU。** MLP 从 GPT-2 的「升维→GELU→降维」变成「两条升维路 SiLU 门控相乘→降维」：

[train_llama3.py:219-226](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_llama3.py#L219-L226) — `x2 = silu(c_fc2(x))` 作门控、与 `c_fc(x)` 相乘、再 `c_proj` 降维。隐藏维度也不是简单的 `4C`，而是 `int(2·4C/3)` 再按 `ffn_dim_multiplier` 与 `multiple_of` 取整（见 [train_llama3.py:209-214](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_llama3.py#L209-L214)），这就是为什么 Llama3 权重多了一个 `c_fc2` 矩阵。

**次要差异：RMSNorm。** 用「均方根」归一化，不减均值、无 bias：

[train_llama3.py:133-144](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_llama3.py#L133-L144) — `_norm` 是 `x * rsqrt(mean(x²)+eps)`，再乘一个可学习的 `weight`。比 LayerNorm 少了「减均值」和 bias，计算更省。

**新架构的权重契约。** 这是「二次开发」最关键的一课——新架构必须配一套新的 `.bin` 写出函数。对比 GPT-2 的 16 类参数（u4-l2），Llama3 的 `write_tensors` 多写了 `c_fc2` 与独立的 `lm_head`（不与 wte 绑定）：

[train_llama3.py:848-868](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_llama3.py#L848-L868) — 张量顺序为 `wte → ln_1(L) → c_attn(L) → c_proj(L) → ln_2(L) → c_fc(L) → c_fc2(L) → mlp.c_proj(L) → ln_f → lm_head`。注意第 864 行的 `c_fc2` 是 GPT-2 没有的，第 868 行的 `lm_head` 独立于 `wte`（GPT-2 是权重绑定，只写一次 wte）。

`.bin` 头也换了新魔数、塞进更多 config 字段：

[train_llama3.py:870-901](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_llama3.py#L870-L901) — 第 879 行魔数是 `20240803`（区别于 GPT-2 的 20240326、数据文件的 20240520、tokenizer 的 20240328，承接 u1-l4/u4-l2 的「不同 .bin 用不同魔数」约定）；header 里多了 `n_kv_head`、`ffn_dim_multiplier`、`multiple_of`、`rope_theta`、`use_scaled_rope` 等 Llama3 专有字段（第 885-892 行），这些是 C 端重建模型所必需的。

Llama3 的默认配置（`LlamaConfig`）把这些新超参都列了出来：

[train_llama3.py:245-269](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_llama3.py#L245-L269) — `vocab_size=128256`（远大于 GPT-2 的 50257）、`block_size=8192`、`n_head=32, n_kv_head=8`（GQA）、`rope_theta=500000`、`use_scaled_rope=True`。第 267-269 行的三个断言是 GQA 的硬约束：`n_kv_head ≤ n_head`、`n_head % n_kv_head == 0`、`n_embd % n_head == 0`。

#### 4.3.4 代码实践

**实践目标**：通过对比 `train_llama3.py` 与 `train_gpt2.py`（u4-l1）的 block 结构，量化「扩展一个新架构」到底要改哪些代码。

**操作步骤**：

1. 打开 [train_llama3.py:146-203](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_llama3.py#L146-L203)（`CausalSelfAttention`）与 [train_llama3.py:205-226](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_llama3.py#L205-L226)（`MLP`）。
2. 对照 u4-l1 讲过的 GPT-2 `CausalSelfAttention`/`MLP`，列出「新增」「修改」「删除」的行。
3. 打开 [train_llama3.py:848-901](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_llama3.py#L848-L901)，数一数 Llama3 的 `.bin` 比 GPT-2 多了哪些张量、多了哪些 config 字段。

**需要观察的现象 / 预期结果**：

填写下面这张「扩展清单」：

| 维度 | GPT-2 | Llama3 | 改动类型 |
| --- | --- | --- | --- |
| 位置编码 | 可学习 `wpe(maxT, C)` | RoPE，无参数（预计算 `freqs_cis`） | 替换 |
| 注意力头 | NH 个头（MHA） | NH 个 Q 头 + NKV 个 KV 头（GQA，`repeat_kv`） | 修改 |
| QKV 投影 | `OC=3C` | `OC=(NH+2·NKV)·hd` | 修改 |
| MLP 激活 | GELU | SwiGLU（`silu(c_fc2)⊙c_fc`） | 替换 |
| MLP 升维矩阵数 | 1（`c_fc`） | 2（`c_fc` + `c_fc2`） | 新增 |
| 归一化 | LayerNorm（有 bias） | RMSNorm（无 bias） | 替换 |
| 线性层 bias | 有 | 无 | 删除 |
| 输出投影 | 权重绑定（共享 wte） | 独立 `lm_head` | 修改 |
| `.bin` 张量类数 | 16 | 17（多 `c_fc2`，且 `lm_head` 独立） | 新增 |
| `.bin` 魔数 | 20240326 | 20240803 | 新增 |
| 词表大小 | 50257（pad 50304） | 128256 | 修改 |

**预期结论**：扩展一个新架构，「模型代码」的改动集中在 block 内部三处（RoPE/GQA/SwiGLU）外加 RMSNorm；但「权重契约」的改动是成体系的——新魔数、新张量顺序、新 config 字段必须同时改，C 端读取代码也要同步更新。这正是为什么 `train_llama3.py` 的文档字符串说它的首要任务是「把权重存成文件供 C 读取」。

#### 4.3.5 小练习与答案

**练习 1**：为什么 Llama3 的 `c_attn` 投影输出通道数不是 `3C`？

**参考答案**：因为 GQA 让 Key/Value 的头数少于 Query。投影输出 = `(NH + 2·NKV)·hd`（见 [train_llama3.py:159](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_llama3.py#L159)）。以 8B 为例 `NH=32, NKV=8, hd=128`，输出 = `(32+16)·128 = 6144`，而非 `3·4096=12288`——KV 头少了，投影矩阵也小了，这正是 GQA 省显存的来源。

**练习 2**：如果要让主线 `train_gpt2.cu` 也支持 Llama3，至少要新增哪些 CUDA kernel？

**参考答案**：至少三件：(1) RoPE 旋转 kernel（对 Q/K 施加 `freqs_cis`）；(2) GQA 的 `repeat_kv` 广播 kernel（或改造 attention kernel 直接支持不等头数）；(3) SwiGLU 融合 kernel（`silu(c_fc2)⊙c_fc`）。另外要把 LayerNorm kernel 换成 RMSNorm，并改造 attention 的位置编码入口（去掉 `wpe` 查表、改用旋转）。注意 `train_llama3.py` 顶部注释的 `TODO` 表明这部分 C 端实现尚未完成。

**练习 3**：`train_llama3.py` 里的 `adapt_llama_state_dict_keys_hf`（[train_llama3.py:361-401](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_llama3.py#L361-L401)）在做什么？这反映了二次开发的什么通用步骤？

**参考答案**：它把 HuggingFace 版 Llama3 的 state_dict 键名（`model.embed_tokens.weight`、`self_attn.q_proj` 等）改写成 llm.c 风格的键名（`transformer.wte.weight`、`attn.c_attn` 等），还要把分开的 q/k/v 投影拼成一个 `c_attn`、对 HF 做「反 permute」。这反映了二次开发的通用步骤——**键名适配 / 权重转换**：当你从外部加载一个预训练模型时，几乎总要写一层「键名重命名 + 张量重排」的胶水代码，把别人的命名约定对齐到自己的 schema。

## 5. 综合实践

**综合任务**：为「单卡 A100、FineWeb 100M token 子集、想从零训一个 GPT-2 small」写一条 `train_gpt2cu` 命令，并解释每个参数。

要求：

1. 用描述符指定 GPT-2 124M（`-e "d12"`）。
2. 单卡运行（不用 `mpirun`）。
3. 全局 batch 设为 65536（即 `2^16`），micro-batch 用 `-b 4`、序列长 `-t 1024`——先算出 `grad_accum_steps`。
4. 显存只够用 recompute（`-r 1`），开 ZeRO stage 0（单卡）。
5. 学习率 `6e-4`、warmup 50 步、最终衰减到 10%、跑 1000 步、每 100 步存一次 checkpoint。

**参考命令**（示例代码，请按你的环境调整路径）：

```bash
# 示例代码：单卡从头训练 GPT-2 124M 的命令
./train_gpt2cu \
    -i "dev/data/fineweb100M/fineweb_train_*.bin" \
    -j "dev/data/fineweb100M/fineweb_val_*.bin" \
    -o log_gpt2_124M_single \
    -e "d12" \
    -b 4 -t 1024 -d 65536 \
    -l 0.0006 -u 50 -q 0.1 \
    -z 0 -r 1 -w 1 \
    -x 1000 -n 100 -y 0 \
    -v 50 -s 200 -g 64 -h 0
```

**自检要点**：

- `grad_accum_steps = 65536 / (4×1024×1) = 16`（单卡 `num_processes=1`），即每个训练步内部做 16 次 micro-batch 前向反向再平均——数学上等价于 batch=65536，但显存只用 batch=4。
- `-e "d12"` 让程序查表得 L=12、C=768、NH=12、T=1024（见 4.2），随机初始化（seed 42）。
- `-r 1` 重算 GELU 省显存（承接 u6-l2）；`-w 1` 保留 fp32 master weights（承接 u6-l1）。
- `-q 0.1` 让学习率从 `6e-4` 沿 cosine 衰减到 `6e-5`（承接 u6-l3）。

（待本地验证：实际可运行的 token 数取决于你的 FineWeb 数据准备情况；若数据不足，可先用 `dev/data/tinyshakespeare.py` 生成的小数据集把命令跑通，观察 loss 是否下降。）

## 6. 本讲小结

- `scripts/run_*.sh` 是真正的复现配方：`while true` + `done_file` 实现断点续训，`-y 1` 让 `train_gpt2cu` 自动从最新 checkpoint 恢复。
- **`-d` 是全局 batch（整数），`-e` 才是模型来源（`.bin` 文件或描述符）**——这是最容易踩的坑；`grad_accum_steps = total_batch_size / (B·T·num_processes)`。
- GPT-2 124M 与 GPT-3 125M 的模型形状几乎相同（L=12, C=768, NH=12），关键差别是上下文长度 1024→2048，因此 GPT-3 把 micro-batch 从 64 减半到 32 以维持显存预算。
- 描述符（`dX` / `gpt2:dX` / `gpt3:cX`）通过 `gpt_build_from_descriptor` 分派到两张硬编码查表函数决定 L/C/NH/T；GPT-2 按层数、GPT-3 按通道数索引；描述符初始化用 mt19937+seed42 与 PyTorch 逐位对齐。
- main 靠 `ends_with_bin` 区分「加载权重」与「解析描述符」两条路径。
- 从 GPT-2 扩展到 Llama3 要改三处核心（RoPE/GQA/SwiGLU）+ RMSNorm，且必须配套改 `.bin` 权重契约（新魔数 20240803、新张量顺序、新 config 字段）；`train_llama3.py` 是参考实现与权重生产者，C 端 kernel 是留给读者的扩展方向。

## 7. 下一步学习建议

到这里，整本 llm.c 学习手册的 32 篇讲义就全部学完了。接下来你可以朝三个方向继续：

1. **真正跑一次复现**：按 `scripts/run_gpt2_124M.sh` 在你能拿到的 GPU 上跑一个缩小版（比如单卡、tinyshakespeare 数据），亲手观察 loss 下降、Hellaswag 准确率上升，把前 6 个单元的算法知识「兑现」成一次真实训练。
2. **二次开发实战**：以 `train_llama3.py` 为蓝本，尝试为 `train_gpt2.cu` 补上 RoPE 或 RMSNorm 的 CUDA kernel（参考 u5-l4 的 kernel 套路），跑通 `test_gpt2.cu` 式的正确性比对。这是把 llm.c 从「读」转向「写」的关键一步。
3. **回看主线源码**：带着本单元对「脚本-描述符-架构扩展」的整体认识，重读一遍 `train_gpt2.cu` 的 `main` 函数（[train_gpt2.cu:1419](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L1419) 起），你会发现命令行参数、模型构建、训练循环、checkpoint/续训这些「工程骨架」其实比算法本身更值得反复揣摩——它们才是把一个研究 Demo 变成可复现训练系统的关键。

此外，llm.c 仓库本身还在演进，建议定期关注 `dev/cuda/`（新的优化 kernel）、`scripts/`（新的复现脚本）和 `doc/`（新的逐层教程）三个目录的更新。
