# MoonEP 是什么：专家并行通信与负载不均衡问题

## 1. 本讲目标

本讲是 MoonEP 学习手册的第一篇。读完本讲，你应该能够：

1. 用自己的话说清楚 **Mixture-of-Experts（MoE）** 和**专家并行（Expert Parallelism, EP）** 是什么、为什么大规模模型离不开它。
2. 熟练说出 MoonEP 的符号系统 \(S, K, E, R, B, NvS, H, H'\) 各自的含义，并能写出"每个 rank 派发出 \(S \times K\) 个 token、每个专家期望接收 \(R \cdot S \cdot K / E\) 个 token"这类基本关系。
3. 理解 **maxvio** 指标的定义，以及路由不均衡为什么会同时拖慢通信、计算和训练（木桶效应、动态形状、显存碎片）。
4. 概述 MoonEP 的三大核心设计——**完美均衡（动态冗余专家）**、**在线规划**、**零拷贝与静态形状**——并说清它们之间的依赖关系。
5. 知道 `moonep` 这个 Python 包对外只导出 `Buffer` 和 `MoonEPCommPlan` 两个名字，理解"极小的 API 面"意味着什么。

本讲不涉及任何内核实现细节，只建立概念框架和问题意识。后续所有讲义都以本讲的符号系统和问题定义为公共语言。

## 2. 前置知识

### 2.1 Transformer 与 MoE

一个标准 Transformer 层由注意力模块和前馈网络（FFN）组成。MoE（Mixture-of-Experts，混合专家）把单个大 FFN 替换成 \(E\) 个并列的"专家" FFN，并加一个**路由器（router）**：每个 token 经 router 打分后，只送往得分最高的 \(K\) 个专家（top-k 路由），最后把这 \(K\) 份输出加权求和。这样模型总参数量可以做得极大，而每个 token 的计算量只随 \(K\)（而不是 \(E\)）增长。

MoE 层的前向可以概括为：

```text
hidden [S, H]                      # 本 rank 有 S 个 token，每个 H 维
  → router 打分
  → topk_experts [S, K]            # 每 token 选中的 K 个专家编号
  → route_weights [S, K]           # 对应的加权系数
  → 派发（dispatch）：token 拷贝发往专家所在的 rank，按专家分组排好
  → 专家 FFN 计算（分组 GEMM）
  → 归并（combine）：K 份输出加权求和，送回原 rank 的 token 顺序
  → 输出 [S, H]
```

其中"派发"和"归并"就是 EP 通信库（MoonEP）要解决的问题。

### 2.2 分布式训练中的 rank 与并行方式

- **rank**：分布式训练中的一个进程（通常绑一张 GPU）。`R` 个 rank 组成一个通信组。
- 常见并行方式：数据并行（每个 rank 拿不同数据、模型相同）、张量并行（把单个矩阵切开）。而 **专家并行（EP）** 是把 \(E\) 个专家**切分**到 \(R\) 个 rank 上，每个 rank 只存放 \(E/R\) 个专家的权重——因为几百个专家的全部权重放不进单卡显存。token 必须在网络中"流向"专家。

### 2.3 GPU 间通信的一点直觉

本讲只需知道：同机多卡之间通过 **NVLink/NVSwitch** 高速互联，rank 之间发送 token 本质上是 GPU 之间的内存读写。MoonEP 的内核会直接通过 NVLink 对称内存读写远端显存，这些细节留到第二单元展开。

### 2.4 一个概率小知识

本讲的代码实践会用到对数正态分布：若 \(x \sim \mathcal{N}(0, \sigma)\)，则 \(e^x\) 服从对数正态分布。\(\sigma\) 越大，指数化后专家 logit 的差距越大，路由就越"偏"。MoonEP 用参数 `bias_ratio`（即 \(\sigma\)）来控制生成路由的不均衡程度。

## 3. 本讲源码地图

本讲只涉及三个文件（都是"读懂即可"，不需要逐行精读）：

| 文件 | 角色 | 本讲关注点 |
| --- | --- | --- |
| [README.md](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/README.md) | 项目定位、三大设计、性能结论、完整 API 用法 | 符号定义、maxvio 公式、三大设计、API walkthrough |
| [moonep/\_\_init\_\_.py](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/__init__.py) | 包的公共导出门面 | `Buffer` 与 `MoonEPCommPlan` 两个导出 |
| [tests/generate_topk_routing.py](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/generate_topk_routing.py) | 测试/基准共用的 top-k 路由生成器 | `bias_ratio` 如何控制路由偏置，`tokens_per_expert` 如何统计 |

为了建立全局感，提前认识一下仓库布局（后续讲义逐个深入）：

```text
MoonEP/
├── README.md               # 本讲主材料
├── setup.py                # 构建 CUDA 扩展 moonep._C
├── moonep/                 # Python 包：API + 全部 CuTe DSL 内核
│   ├── __init__.py         # 本讲：公共导出
│   ├── api.py              # Buffer 类（第 4 讲上手、第 6 单元深入）
│   ├── buffer.py           # NVLink 对称内存基础设施（第 2 单元）
│   ├── planning.py         # 在线规划器（第 3 单元）
│   ├── dispatch*.py        # dispatch 内核与 epilogue（第 4 单元）
│   ├── combine*.py         # combine 内核与 prologue（第 4 单元）
│   ├── prefetch.py         # 权重预取（第 5 单元）
│   └── grad_reduce.py      # 梯度归约（第 5 单元）
├── csrc/                   # C++/CUDA：VMM 绑定（第 2 单元）
├── tests/                  # 测试与参考实现（第 6 单元）
└── benchmarks/             # 基准脚本（第 6 单元）
```

## 4. 核心概念与源码讲解

### 4.1 MoE 专家并行与 MoonEP 的符号系统

#### 4.1.1 概念说明

MoonEP 的自我定位是"一个通过**动态冗余专家**让各 rank token 负载完美均衡的专家并行通信库"。见 README 开头：

- [README.md:L3](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/README.md#L3)：一句话定位——"keeps token loads perfectly balanced across ranks via dynamic redundant experts"（通过动态冗余专家保持各 rank 的 token 负载完美均衡）。

要理解这句话，先把符号系统建立起来。README 在 Usage 一节给出了完整记号：

- [README.md:L43](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/README.md#L43)：定义 \(S\)（每 rank 输入 token 数）、\(K\)（每 token 的 routed top-k）、\(E\)（EP 组内路由专家总数）、\(R\)（EP rank 数）、\(B\)（每 rank 权重预取槽数）、\(NvS\)（每 rank 派发槽位数 = \(S \times K\) 个真实 token 加上按 VM group 的 padding）、\(H\)（hidden 维）、\(H'\)（专家 FFN 中间维）。

整理成表（以 README 的 API 示例参数为典型值）：

| 符号 | 含义 | 典型值 |
| --- | --- | --- |
| \(S\) | 每 rank 输入 token 数 | 4096 |
| \(K\) | 每 token 路由到的专家数（top-k） | 8 |
| \(E\) | EP 组内路由专家总数 | 256 |
| \(R\) | EP rank 数（通信组大小） | 8 |
| \(E/R\) | 每 rank 存放的本地专家数（home 专家） | 32 |
| \(B\) | 每 rank 权重预取槽数（冗余专家数上限） | 训练必须 \(E/R\)，推理推荐 3–4 |
| \(NvS\) | 每 rank 派发槽位：\(S \times K\) 真实 token + padding | ≥ 32768 |
| \(H\) | 隐藏维 | 7168 |
| \(H'\) | 专家 FFN 中间维 | 由模型决定 |

几个派生关系值得现在就记住：

- 每个 rank 派发出 \(S \times K\) 份 token 拷贝（一个 token 被选中 \(K\) 次，就要发 \(K\) 份）。
- 全局共有 \(R \cdot S \cdot K\) 份 token 拷贝，落到 \(E\) 个专家上，完美均衡时每个专家期望接收
  \[ \bar{T} = \frac{R \cdot S \cdot K}{E} \]
  个 token（上例中 \(= 8 \times 4096 \times 8 / 256 = 1024\)）。
- MoonEP 的核心承诺：**每个 rank 接收的 token 数恒等于 \(S \times K\)，与路由多偏无关**——注意是"接收"，不是"发出"。发出量由 router 决定无法改变，MoonEP 改变的是**这些 token 分别由哪个 rank 计算**。

#### 4.1.2 核心流程

一次使用 MoonEP 的 MoE 前向（逻辑视角）：

```text
每个 rank 各自持有:
  hidden_sh [S, H]                    # 本 rank 的输入
  topk_experts_sk [S, K] + route_weights_sk [S, K]   # router 输出
  tokens_per_expert [E]               # 本 rank 统计的每专家 token 数

1. 在线规划 (planning)          # rank0 汇聚全局信息，决定每个 token 去哪个 rank
2. dispatch                     # token 直接写到远端 rank 的 expert 分组位置
3. prefetch_weight              # 把被复制的远程专家权重搬到本地预取槽
4. 专家 FFN (分组 GEMM)          # 框架侧计算，读 cu_seqlens 决定激活哪些专家行
5. combine                      # K 份输出加权求和，回到 token 顺序
```

反向传播则使用 dispatch/combine 的对偶操作（详见 4.3.2）。本讲只需要记住这个骨架；其中第 1、2、3 步就是 MoonEP 的三大设计各自落地的位置。

#### 4.1.3 源码精读

README 的 API walkthrough 给出了构造 `Buffer` 的最小示例：

- [README.md:L73-L81](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/README.md#L73-L81)：`Buffer(S=4096, H=7168, K=8, E=256, num_ep_ranks=8, num_sms=32, token_padding=128)`——构造参数就是符号系统的直接体现；`B` 缺省取 \(E // R\)，也可显式传 `B=4`；四个通信入口都支持 `async_finish=True`。

dispatch 前向的输入输出约定：

- [README.md:L86-L95](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/README.md#L86-L95)：输入是 `hidden_sh [S,H] bf16`、`route_weights_sk [S,K] fp32`、`topk_experts_sk [S,K] int32`、`tokens_per_expert [E] int32`；输出是 `hidden_nvsh [NvS,H]`、`route_weights_nvs [NvS]`、`cu_seqlens [E+B]`、`plan`。注意输出第一维是 **NvS** 而不是 S——这就是"静态形状"的第一处体现。

MoonEP 与训练/推理框架的契约只有两样东西：

- [README.md:L45](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/README.md#L45)：一个连续的对称内存权重张量 `[E+B, H, H']`，加上一个由 dispatch 返回的 `cu_seqlens[E+B]`（每个专家行的 padded token 结束偏移），分组 GEMM 只按行索引寻址。

#### 4.1.4 代码实践

**实践目标**：不看答案，手工写出 dispatch 输入输出每个张量的形状与 dtype，然后核对。

**操作步骤**：

1. 阅读 [README.md:L86-L95](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/README.md#L86-L95) 的 dispatch fwd 一节。
2. 取参数 \(S=4096, H=7168, K=8, E=256, R=8\)（`B` 取默认 \(E/R=32\)），在纸上写下四个输入、四个输出的名字、形状、dtype。
3. 与下表核对（这就是"答案"）：

| 张量 | 方向 | 形状 | dtype |
| --- | --- | --- | --- |
| `hidden_sh` | 输入 | \([S, H] = [4096, 7168]\) | bf16 |
| `route_weights_sk` | 输入 | \([S, K] = [4096, 8]\) | fp32 |
| `topk_experts_sk` | 输入 | \([S, K] = [4096, 8]\) | int32 |
| `tokens_per_expert` | 输入 | \([E] = [256]\) | int32 |
| `hidden_nvsh` | 输出 | \([NvS, H]\)，\(NvS \ge S \times K = 32768\) | bf16 |
| `route_weights_nvs` | 输出 | \([NvS]\) | fp32 |
| `cu_seqlens` | 输出 | \([E+B] = [288]\) | int32 |
| `plan` | 输出 | `MoonEPCommPlan`（规划快照对象） | — |

**需要观察的现象 / 预期结果**：输出的第一维 \(NvS\) 与输入的 \(S\) 脱钩——无论路由怎么偏，输出形状不变。这正是后文"静态形状"设计的前奏。

#### 4.1.5 小练习与答案

**练习 1**：若某 MoE 层 \(S=2048, K=6, E=192, R=8\)，完美均衡时每个专家平均接收多少 token？每 rank 派发多少份拷贝？

答案：全局拷贝数 \(R \cdot S \cdot K = 8 \times 2048 \times 6 = 98304\)，除以 \(E=192\) 得 \(\bar{T} = 512\)。每 rank 派发 \(S \times K = 12288\) 份拷贝。

**练习 2**：`tokens_per_expert` 是每 rank 各自统计的本地计数（形状 `[E]`，统计"本 rank 的 S 个 token 想去哪些专家"）。为什么 dispatch 需要它，而单靠 `topk_experts_sk` 不够？

答案：`topk_experts_sk` 本身确实蕴含了同样的信息，但逐 token 重新统计一遍（bincount）有成本；MoonEP 假设框架在 router 之后本来就会统计每专家 token 数（分组 GEMM 也需要它），于是直接复用框架已有的 `tokens_per_expert`，避免重复计算。这也体现了 MoonEP"与框架共享中间量"的设计取向。

**练习 3**：为什么 \(E\) 必须能被 \(R\) 整除（README 中 \(E/R\) 反复出现）？

答案：EP 把 \(E\) 个专家均分到 \(R\) 个 rank，每个 rank 拥有 \(E/R\) 个"home 专家"；权重缓冲区行 \([0, E)\) 按 \(E/R\) 一段切给各 rank（见 [README.md:L53](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/README.md#L53)）。同时训练时 \(B = E/R\) 的约束也来源于"规划器最多从一个远程 home group 复制专家"（见 [README.md:L58](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/README.md#L58)）。

### 4.2 maxvio：量化路由不均衡及其危害

#### 4.2.1 概念说明

理想情况下每个专家接收 \(\bar{T}\) 个 token；现实中 router 输出是倾斜的——某些"热门"专家接收远超平均的 token。MoonEP 用一个无量纲指标 **maxvio** 刻画这种倾斜，README 性能一节给出了定义：

- [README.md:L15-L17](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/README.md#L15-L17)：
  \[ \text{maxvio} = \max_e \left( \frac{T_e}{\bar{T}} \right) - 1 \]
  其中 \(T_e\) 是路由到专家 \(e\) 的 token 数，\(\bar{T}\) 是完美均衡下的期望值。maxvio = 0 表示完美均衡；maxvio = 1 表示最热专家接收了期望值的 2 倍。

为什么不均衡在 EP 里是"灾难级"问题？README 的端到端实验结论说得很直白：

- [README.md:L24](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/README.md#L24)：通信时间**由最热的 rank 决定**（"whose latency is set by the hottest rank"）——all-to-all 通信必须等最慢的收发方结束，这是典型的木桶效应。
- [README.md:L31-L32](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/README.md#L31-L32)：端到端训练中，不均衡带来两个恶果——最热 rank 计算量膨胀使迭代时间随 maxvio 爬升；每层激活形状随路由变化导致 GPU 显存碎片化，最终在高不均衡下 **OOM**。

对比之下 MoonEP 的表现：

- [README.md:L23-L25](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/README.md#L23-L25)：与 DeepEP v2 的通信对比中，MoonEP 通信时间曲线随 maxvio 增长几乎平坦，且对比口径**已把 MoonEP 额外的规划、预取内核计入**关键路径。
- [README.md:L32](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/README.md#L32)：每个 rank 每层恒定计算 \(S \times K\) 个 token，迭代时间平坦、显存形状完全静态、不会 OOM。

#### 4.2.2 核心流程

在代码里，maxvio 是通过**可控偏置的路由生成器**实现的。`tests/generate_topk_routing.py` 的 `generate_topk_routing` 生成两种路由：

```text
输入: S, K, E, R, bias_ratio, seed, rank
if bias_ratio == 0:        # 均衡基线
    按 round-robin 公式给每个 token 的第 k 份拷贝指定 (目标rank, 目标本地下标)
    → 每个专家恰好收到 S·K/E 个 token（结构上精确均衡）
else:                      # 偏置路由
    从共享种子的对数正态分布采样 E 个专家 logit
    → 每 token 按 logit 概率无放回抽 K 个专家
统计 tokens_per_expert = bincount(topk)
输出: topk [S,K] int32, tpe [E] int32
```

两个关键设计让模拟贴近真实训练：

1. **共享种子** `g_shared`（种子 = `seed`）生成专家 logit 分布，**各 rank 相同**——所以热门专家在各 rank 间"对齐"，全局不均衡不会被平均掉；
2. **本地种子** `g_local`（种子 = `rank`）驱动每 token 抽样，各 rank 独立。

`bias_ratio` 就是底层对数正态分布的 \(\sigma\)：0 → 精确均衡；0.5 → 轻度倾斜；1 → 典型 dropless-MoE 倾斜；2 → 重度；5 → 近退化。

#### 4.2.3 源码精读

- [tests/generate_topk_routing.py:L6-L23](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/generate_topk_routing.py#L6-L23)：函数签名与 docstring——明确标注了 `bias_ratio` 的语义刻度（0/0.5/1/2/5），以及"shared generator ← base seed, inner generator ← ep rank"的双种子设计，保证与训练中偏置路由生成器逐位一致。

- [tests/generate_topk_routing.py:L24-L33](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/generate_topk_routing.py#L24-L33)：均衡分支。`epn = E // R` 是每 rank 本地专家数；`target_rank = (toks + ks) % R`、`target_local = ((toks // R) + ks) % epn` 用 round-robin 保证均匀覆盖；`perm` 是本 rank 随机置换，把"均匀下标"打散到随机专家上——均匀性保持、专家标签随机。以 \(S=4096, K=8, E=256, R=8\) 为例可以推出每个专家**恰好**收到 128 个 token（每 rank），maxvio 精确为 0。

- [tests/generate_topk_routing.py:L34-L41](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/generate_topk_routing.py#L34-L41)：偏置分支。`logits = exp(normal(0, bias_ratio, (E,)))` 是共享的对数正态专家权重；`torch.multinomial(probs, K, replacement=False)` 按 logit 概率为每个 token 无放回抽 K 个专家。热门专家 logit 呈指数级领先，token 大量涌向少数专家。

- [tests/generate_topk_routing.py:L43-L44](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/generate_topk_routing.py#L43-L44)：`tpe = torch.bincount(topk.flatten(), minlength=E)`——用 bincount 统计每专家 token 数，这正是 dispatch 输入 `tokens_per_expert` 的来源。

#### 4.2.4 代码实践

**实践目标**：亲手构造均衡与偏置两种路由，计算 maxvio，验证"偏置路由下 maxvio 显著大于 0"，把 4.2 的公式变成肌肉记忆。

**操作步骤**：

1. 新建 `maxvio_demo.py`（放在仓库外或 `MoonEP-tutorial/` 下均可；**不要**写进 `moonep/` 或 `tests/`），写入以下**示例代码**（逻辑复刻自 `tests/generate_topk_routing.py`，改为 CPU 运行，不需要 GPU 与编译扩展）：

   ```python
   # maxvio_demo.py —— 示例代码（复刻 tests/generate_topk_routing.py 的生成逻辑，CPU 版）
   import torch

   def generate_topk_routing(S, K, E, R, bias_ratio, seed, rank):
       g_shared = torch.Generator().manual_seed(seed)   # 各 rank 相同：专家热度分布
       g_local = torch.Generator().manual_seed(rank)    # 各 rank 不同：逐 token 抽样
       if bias_ratio == 0.0:                            # round-robin 均衡基线
           epn = E // R
           toks = torch.arange(S)
           ks = torch.arange(K)
           target_rank = (toks[:, None] + ks[None, :]) % R
           target_local = ((toks[:, None] // R) + ks[None, :]) % epn
           perm = torch.randperm(epn, generator=g_local)
           topk = (target_rank * epn + perm[target_local]).to(torch.int32)
       else:                                            # 对数正态偏置路由
           logits = torch.exp(torch.normal(0.0, bias_ratio, (E,),
                                           generator=g_shared))
           probs = logits[None, :].expand(S, E)
           topk = torch.multinomial(probs, K, replacement=False,
                                    generator=g_local).to(torch.int32)
       tpe = torch.bincount(topk.flatten(), minlength=E).to(torch.int32)
       return topk, tpe

   S, K, E, R = 4096, 8, 256, 8
   T_bar = S * K * R / E          # 完美均衡下每专家期望 token 数 = 1024

   for bias_ratio in [0.0, 0.5, 1.0, 2.0]:
       tpe_total = torch.zeros(E, dtype=torch.long)
       for rank in range(R):      # 模拟 R 个 rank，热门专家跨 rank 对齐
           _, tpe = generate_topk_routing(S, K, E, R, bias_ratio,
                                          seed=42, rank=rank)
           tpe_total += tpe.long()
       maxvio = tpe_total.max().item() / T_bar - 1
       hot = torch.topk(tpe_total, 3)
       print(f"bias_ratio={bias_ratio:>4}: maxvio={maxvio:7.3f} | "
             f"最热 3 个专家 token 数={hot.values.tolist()} (期望 {T_bar:.0f})")
   ```

2. 运行 `python maxvio_demo.py`。

**需要观察的现象**：

- `bias_ratio=0.0` 一行：maxvio 精确等于 0（round-robin 结构保证，非随机近似）。
- `bias_ratio=0.5 / 1.0 / 2.0` 各行：maxvio 逐档增大；最热专家的 token 数达到期望值的若干倍。
- 由于共享种子，各 rank 的热门专家编号一致——全局 maxvio 不会被多 rank 平均掉。

**预期结果**：`bias_ratio=0` 时 maxvio = 0；偏置档位的具体 maxvio 数值随种子而变（典型地，`bias_ratio=1.0` 时最热专家达到期望值的数倍，`bias_ratio=2.0` 时更甚），具体数值**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：maxvio = 2 代表什么？如果 \(\bar{T} = 1024\)，最热专家收到多少 token？

答案：maxvio = 2 表示最热专家 token 数是期望值的 3 倍，即 \(T_{\max} = 3 \times 1024 = 3072\)。

**练习 2**：为什么不直接用 \(T_e\) 的方差或最大值来衡量，而要除以 \(\bar{T}\) 归一化？

答案：\(T_e\) 的绝对量随 \(S, K, R\) 线性缩放，不同规模之间不可比；除以 \(\bar{T}\) 后得到无量纲比率，才能像 README 那样在同一张图里扫描不同规模下的不均衡度（[README.md:L13](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/README.md#L13)：基准在 H20、EP=8 下扫描 router imbalance）。只看最大值而不减 1 也可以，减 1 只是把"完美均衡"平移到 0 点，让曲线更好读。

**练习 3**：`generate_topk_routing` 里如果 `g_shared` 改成每个 rank 用不同种子，对模拟结果有什么影响？

答案：各 rank 的专家热度分布将不再对齐，某专家在 rank A 是热门、在 rank B 可能是冷门，全局求和后不均衡会被部分平均掉，模拟出的 maxvio 偏低、高估系统的均衡性。真实训练中热门专家（由同一个 router 权重决定）天然跨 rank 对齐，所以共享种子是更忠实的模拟（见 [tests/generate_topk_routing.py:L19-L23](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/generate_topk_routing.py#L19-L23) 的说明）。

### 4.3 MoonEP 的三大核心设计

#### 4.3.1 概念说明

README 在开头列出三大设计，这是理解 MoonEP 全部源码的纲：

- [README.md:L5-L9](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/README.md#L5-L9)：

  1. **Perfect balance（完美均衡）**：无论路由多偏，每个 rank 恰好接收 \(S \times K\) 个 token。做法是维护**少量冗余专家**：由当前 router 输出在线规划，在专家计算前把需要复制的远程专家权重**预取**到本地；反向传播时再把冗余专家的梯度归约回其 home rank。
  2. **Online planning（在线规划）**：规划由一个近乎最优的 GPU 规划内核完成，开销可忽略。
  3. **Zero copy and static shapes（零拷贝与静态形状）**：派发与归并融合（fused permute/unpermute）——token 被直接写到远端 rank 的 expert 分组位置，计算直接拿到缓冲区视图；只需固定 \(S \times K\) 大小的缓冲，形状静态可知，消除逐层 MoE 宿主端同步。

三者的关系可以这样理解：

- **规划**是大脑：每步从 `tokens_per_expert` 算出"复制哪些专家、每个 token 由谁计算"。
- **冗余专家（预取）**是手段：让本来要发给热门 rank 的 token 改由本地冗余副本计算，从而削平接收峰值。
- **零拷贝 + 静态形状**是收益兑现：均衡让形状恒定为 \(S \times K\)，于是缓冲可以预先分配成固定大小，dispatch 直接写最终位置，框架拿到的视图无需再拷贝。

缺一不可：没有规划，冗余专家集合无从得知；没有冗余专家，均衡无从实现；没有静态形状，均衡的收益会被动态形状的同步与碎片成本吃掉。

#### 4.3.2 核心流程

把三大设计放进一次完整训练步（README 的 API walkthrough 就是这个顺序）：

```text
前向:
  router → topk_experts_sk / route_weights_sk / tokens_per_expert
  dispatch fwd        → hidden_nvsh, route_weights_nvs, cu_seqlens, plan
  prefetch_weight     → 按 plan 把远程专家权重搬进预取槽 [E, E+B)
  专家 FFN            → 读 cu_seqlens，对激活的行做分组 GEMM
  combine fwd         → 输出 [S, H] + 收集回的 route_weights
反向:
  combine bwd = 一次 dispatch（复用保存的 plan，免规划、免预取）
                → grad_expert_output_nvsh
  专家 FFN 反向       → 写入 [E+B, H, H'] 梯度缓冲
  dispatch bwd = 一次 combine（把每 token 的 K 份梯度求和回 token 顺序）
                → grad_hidden_sh
  reduce_grad         → 把冗余专家槽位上的梯度归约回 home rank 并清零槽位
```

对应源码位置（本讲只建立索引，细节在后续单元）：

- dispatch fwd：[README.md:L86-L95](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/README.md#L86-L95)
- prefetch_weight：[README.md:L97-L104](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/README.md#L97-L104)
- dispatch bwd（combine + reduce_grad）：[README.md:L108-L128](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/README.md#L108-L128)
- combine fwd：[README.md:L133-L140](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/README.md#L133-L140)
- combine bwd（复用 plan 的 dispatch）：[README.md:L144-L152](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/README.md#L144-L152)，注释明确说明 planning 被跳过、无需预取。

冗余专家的物理载体是**权重缓冲区布局**：

- [README.md:L51-L54](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/README.md#L51-L54)：`[E+B, H, H']` 连续权重张量——行 \([0, E)\) 是所有 rank 的本地专家（每段 \(E/R\) 行物理上**就是** home rank 的参数内存，通过对称内存映射到所有 rank）；行 \([E, E+B)\) 是本 rank 预取槽，由 `prefetch_weight` 填充，规划器通过 `cu_seqlens` 把被复制专家的 token 段指向这些槽。预取槽物理内存来自**进程级共享池**，额外成本是每投影 \(B\) 个专家权重（全部层合计），而不是每层一份。
- [README.md:L56-L59](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/README.md#L56-L59)：\(B\) 的设置规则——训练必须 \(B = E/R\)（规划器每 rank 最多从一个远程 home group 复制专家）；推理允许 \(B < E/R\)（推荐 3–4），溢出时分组 GEMM 直接经对称映射读 home rank 权重，慢一点但正确。

零拷贝模式的边界条件：

- [README.md:L156-L173](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/README.md#L156-L173)：`zero_copy=True` 时 dispatch 返回通信缓冲区的视图、专家 FFN 原地读写；但视图别名会被下一次 `dispatch`/`combine` 覆盖，**不能跨通信调用持有**（autograd 若为其保存 backward，必须退回 `zero_copy=False`）。

#### 4.3.3 源码精读（补充：性能口径）

评估 MoonEP 时容易忽略的公平性问题，README 特意声明：

- [README.md:L25](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/README.md#L25)：与 DeepEP v2 的对比**把 MoonEP 额外的规划与权重预取内核也计入**关键路径——即使如此，dispatch 总时间仍与 DeepEP v2 的纯 dispatch 持平并在高不均衡下反超，combine 在所有档位显著更快。读基准图时记住这一点，避免"MoonEP 只是转移了开销"的误读。

#### 4.3.4 代码实践

**实践目标**：把"冗余专家的显存成本"算清楚，体会 \(B\) 的取值权衡。

**操作步骤**：

1. 新建 `memory_budget.py`（**示例代码**，纯 Python 算术，无需 GPU）：

   ```python
   # memory_budget.py —— 示例代码：估算 [E+B, H, H'] 权重缓冲的显存
   def weight_buffer_gib(E, B, H, Hp, dtype_bytes=2):
       rows = E + B
       return rows * H * Hp * dtype_bytes / 1024**3

   E, R, H, Hp = 256, 8, 7168, 2048   # H' 仅为演示取值
   proj = ("gate", "up", "down")

   for scenario, B in [("训练 B=E/R", E // R), ("推理 B=4", 4)]:
       per_proj = weight_buffer_gib(E, B, H, Hp)
       print(f"{scenario}: B={B}, 单投影 {per_proj:.2f} GiB, "
             f"三投影共 {3 * per_proj:.2f} GiB (逻辑视图大小)")
   # 物理成本：预取槽来自进程级共享池，额外物理显存 = B·H·H'·2字节·3投影
   extra = weight_buffer_gib(0, E // R, H, Hp) * 3
   print(f"训练时预取槽额外物理显存(三投影, 全部层合计): {extra:.2f} GiB")
   ```

2. 运行并对照 [README.md:L54](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/README.md#L54) 的说法核对："extra cost is `B` expert weights per projection **in total**, not per layer"。

**需要观察的现象 / 预期结果**：训练档 \(B=32\) 与推理档 \(B=4\) 的**逻辑**缓冲大小差别不大（行数 288 对 260）；而**物理**额外成本只按每投影 \(B\) 个专家计一次（进程级池共享），与层数无关。具体 GiB 数值取决于你代入的 \(H'\)，**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：既然冗余专家能削峰，为什么不干脆每个 rank 复制全部 \(E\) 个专家？

答案：那样 EP 就退化成"每 rank 全量专家"，参数显存乘以 \(R\)，恰恰违背 EP 存在的意义（放不下才切开）。MoonEP 只复制**少量**（\(B\) 个）当前最热的远程专家，且集合每步在线更新——热度会随训练漂移，静态复制无法跟踪。训练时 \(B = E/R\) 是上界而非目标：规划器保证每 rank 最多从一个远程 home group 复制（[README.md:L58](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/README.md#L58)）。

**练习 2**：combine bwd 为什么可以"复用 plan、免规划、免预取"？

答案：combine bwd 是把输出梯度按**同一套路由**散射回 expert 分组位置——目标位置与前向 dispatch 完全一致，所以直接用保存的 `plan` 再做一次 dispatch 即可（[README.md:L144-L152](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/README.md#L144-L152)）。权重已经在本地（前向预取过、尚未被覆盖），自然也无需再次预取。

**练习 3**：如果某框架在 dispatch 返回的 `hidden_nvsh` 视图上直接保存了 autograd 引用（跨通信调用持有），会发生什么？该怎么处理？

答案：视图别名的是通信缓冲内部状态，下一次 `dispatch`/`combine` 会覆盖它，backward 读到的将是脏数据。README 明确要求此类场景退回 `zero_copy=False`，让接口返回独立张量（[README.md:L173](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/README.md#L173)）。

### 4.4 moonep 包的公共导出

#### 4.4.1 概念说明

`moonep/__init__.py` 是包的"门面"——`from moonep import Buffer` 时 Python 执行的就是它。它定义了使用者能看到的全部公共名字，因此是衡量**API 面大小**的最直接证据。

#### 4.4.2 核心流程

`from moonep import Buffer` 触发的导入链：

```text
moonep/__init__.py
  └→ moonep/api.py            # Buffer 类定义在此 (api.py:440)
       ├→ moonep/buffer.py    # 对称内存基础设施 (其中 buffer.py:10 导入编译扩展 moonep._C)
       ├→ moonep/planning.py  # MoonEPCommPlan 与在线规划器
       ├→ moonep/dispatch*.py / combine*.py   # 通信内核
       ├→ moonep/prefetch.py / grad_reduce.py # 预取与梯度归约
       └→ ...
```

也就是说，`import moonep` 会连带加载全部内核模块并要求 `moonep._C`（CUDA 扩展）已经编译安装——这就是为什么必须先 `pip install -e .` 才能使用（构建细节是下一讲 u1-l2 的主题）。

#### 4.4.3 源码精读

- [moonep/\_\_init\_\_.py:L1-L9](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/__init__.py#L1-L9)：全文只有 9 行——从 `.api` 导入 `Buffer`，从 `.planning` 导入 `MoonEPCommPlan`，`__all__` 也只有这两个名字。**整个库的公共 API 就是一个缓冲类加一个规划数据类**：`Buffer` 承载 `dispatch` / `combine` / `prefetch_weight` / `reduce_grad` 四个入口（以及 `destroy`），`MoonEPCommPlan` 是 dispatch 返回、需要跨前向/反向保存的规划快照。

这种极小 API 面的含义：

- 所有复杂度（规划算法、TMA 流水线、去重、对称内存）都被封装在 `Buffer` 内部，使用方只需理解"构造 → 四个入口 → destroy"。
- `MoonEPCommPlan` 被**显式导出**而不是当作私有类型，说明它是 MoonEP 与框架之间契约的一部分——框架必须把它从前向传到反向（见 4.3.2 的 combine bwd 复用路径）。

#### 4.4.4 代码实践

**实践目标**：亲手验证导出面与导入链，不依赖运行 GPU 代码。

**操作步骤**：

1. 在仓库根目录执行文本搜索（等价于下面的 Grep）：查找 `class Buffer` 与 `class MoonEPCommPlan` 的定义位置。你会得到：
   - [moonep/api.py:L440](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L440)——`class Buffer`
   - [moonep/planning.py:L31-L32](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/planning.py#L31-L32)——`@dataclass(frozen=True, slots=True)` + `class MoonEPCommPlan`（不可变、`slots` 优化的数据类，对应"plan 是不可变快照"的设计）。
2. （可选，需先完成构建）运行 `python -c "import moonep; print(moonep.__all__)"`，预期输出 `['Buffer', 'MoonEPCommPlan']`；未编译 `moonep._C` 时该命令会失败——这本身就是"必须先构建"的证据。**待本地验证**。

**需要观察的现象 / 预期结果**：`__all__` 恰好两个名字；`MoonEPCommPlan` 是 `frozen=True` 的 dataclass（不可变），与 README 中"save it for prefetch/combine and both backward passes"（[README.md:L95](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/README.md#L95)）的用法呼应。

#### 4.4.5 小练习与答案

**练习 1**：`MoonEPCommPlan` 为什么设计成不可变（`frozen=True`）？

答案：plan 是一次规划结果的**快照**，前向的 prefetch、combine 与两次反向都要基于同一份路由决定。如果可变，任何一环的意外修改都会让其他环节读到不一致的路由，且难以排查；不可变数据类从类型系统层面杜绝了这种错误（定义见 [moonep/planning.py:L31-L32](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/planning.py#L31-L32)）。

**练习 2**：为什么 `buffer.py` 需要 `moonep._C` 而 `planning.py` 不需要？

答案：`moonep._C` 封装的是 CUDA VMM（虚拟内存管理）与 NVLink 相关的 C++ 原语（如分布式显存分配与句柄传递），属于 `buffer.py` 负责的内存基础设施层；`planning.py` 的规划内核用 CuTe DSL（Python 侧 `nvidia-cutlass-dsl`）编写，不直接调用这些 C++ 绑定（导入关系见 [moonep/buffer.py:L10](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/buffer.py#L10) 与 4.4.2 的导入链）。这一分层是第 2、3 单元的入口。

## 5. 综合实践

**任务**：扩展 4.2.4 的 `maxvio_demo.py`，从"专家级不均衡"推进到"rank 级木桶效应"，把本讲三个知识点（符号系统、maxvio、三大设计的动机）串成一条线。

**要求实现**：

1. 对 `bias_ratio ∈ {0.0, 0.5, 1.0, 2.0}` 各生成 \(R=8\) 个 rank 的路由（沿用共享种子设计）。
2. 除全局 maxvio 外，再统计**每个 rank 将要接收的 token 数**（按 MoonEP 之前的"原生 EP"语义：token 发往其专家的 home rank，即 `topk // (E // R)` 后 bincount）。
3. 输出三行汇总：全局 maxvio、最热 rank 接收数与平均接收数 \(S \times K\) 的比值、最冷 rank 接收数与 \(S \times K\) 的比值。

**观察与思考题**（写在脚本输出后面）：

- 原生 EP 下，最热 rank 接收数 / \(S \times K\) 应随 bias_ratio 显著增长——这就是 README 里"latency is set by the hottest rank"的数值化身。
- MoonEP 承诺把这一比值固定为 1：无论 maxvio 多大，每个 rank 恰好接收 \(S \times K\)。结合 4.3 想一想：被复制到预取槽的专家，其 token 从"远端 rank 计算"变成了"本地计算"，这就是削峰的全部秘密。
- 预期：`bias_ratio=0` 时所有 rank 接收数相等且等于 \(S \times K\)；偏置档位的最热 rank 比值**待本地验证**（应明显大于 1 并随 bias_ratio 递增）。

**提示**：rank 级统计只需两行——`home_rank = topk // (E // R)`，再对 `home_rank` 做 `bincount(minlength=R)` 并对 R 个 rank 求和。

## 6. 本讲小结

- **EP 的问题设定**：\(E\) 个专家切分到 \(R\) 个 rank（每 rank \(E/R\) 个 home 专家），每 rank 的 \(S\) 个 token 各派发 \(K\) 份拷贝，通信与计算都围绕 \(S, K, E, R, B, NvS, H, H'\) 这套符号展开。
- **maxvio 量化不均衡**：\(\text{maxvio} = \max_e(T_e/\bar{T}) - 1\)，\(\bar{T} = R \cdot S \cdot K / E\)；不均衡的代价是木桶效应（最热 rank 定时延）+ 动态形状（显存碎片、OOM），`tests/generate_topk_routing.py` 用对数正态 `bias_ratio` 可控地复现这一切。
- **三大设计环环相扣**：在线规划决定复制哪些远程专家 → 冗余专家预取削平接收峰值，使每 rank 恒接收 \(S \times K\) → 均衡使形状静态化，零拷贝视图直接交给计算，消除边界拷贝与宿主同步。
- **代价被显式管理**：冗余专家的显存 = 每投影 \(B\) 个专家（进程级池、全层共享，训练 \(B=E/R\)、推理推荐 3–4）；对比基准把规划与预取内核计入关键路径后 MoonEP 仍占优。
- **API 面极小**：`moonep/__init__.py` 只导出 `Buffer`（四个入口 + `destroy`）与不可变的 `MoonEPCommPlan`（跨前向/反向传递的规划快照）。

## 7. 下一步学习建议

- **下一讲 u1-l2（构建、安装与运行测试）**：动手执行 `pip install -e .`，理解 `setup.py` 如何把 `csrc/bindings.cu` 编译成 `moonep._C`——本讲 4.4 已经埋下"导入依赖编译扩展"的伏笔。
- **再下一讲 u1-l3（代码地图）**：系统走一遍 `moonep/`、`csrc/`、`tests/`、`benchmarks/` 的每个文件，把本讲"源码地图"一节扩充为完整索引。
- **提前阅读（可选）**：通读 [README.md](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/README.md) 的 Usage 一节两遍——第一遍对照本讲符号表，第二遍尝试不看注释复述每个 API 的输入输出形状。
- 本讲提到的 `MoonEPCommPlan` 字段细节、`zero_copy` 的断言机制，分别会在 u3-l1（规划数据结构）和 u6-l2（零拷贝模式）深入。
