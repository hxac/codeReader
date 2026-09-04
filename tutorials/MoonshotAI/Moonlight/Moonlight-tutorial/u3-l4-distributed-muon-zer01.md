# 走向分布式：ZeRO-1 式 Muon 与 Megatron 集成

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清楚 ZeRO-1「优化器状态分片」到底分片了什么、为什么能把显存降下来、降到原来的几分之一。
2. 画出数据并行训练一步之内的通信数据流：DDP 的梯度 all-reduce，以及 ZeRO-1 的 reduce-scatter + all-gather，并能比较两者的通信量。
3. 基于 `examples/toy_train.py` 的真实代码，论证「Muon 的逐参数更新在数学上天然可分片」——这正是 README 宣称「保持算法数学性质」的代码依据。
4. 亲手把 toy 训练改造成 `torch.distributed` 的 DDP 版本并跑通，说清楚 Muon 的动量缓冲在 DDP 下放在哪里、与论文的 ZeRO-1 方案差在哪里。
5. 知道工业级分布式 Muon 的入口在 Megatron-LM PR #1428，并掌握一套「带着问题清单去读论文 PDF 与 PR diff」的方法。

先交代一个诚实的前提：**本仓库内没有分布式 Muon 的源码**。仓库的全部可执行代码就是单机单卡的 `examples/toy_train.py`；分布式实现位于外部仓库 Megatron-LM 的 PR #1428（README 明确给出链接），技术细节写在 `Moonlight.pdf` 中。本讲义作者的运行环境无法解析该 PDF 的文本内容，因此凡涉及 PDF 内部的章节编号与实现细节，本讲一律标注「待确认」，并给出你可以自行核对的验证路径。能够从本仓库源码与 README 原文直接验证的内容，本讲都会给出永久链接与行号。

## 2. 前置知识

### 2.1 什么是数据并行（Data Parallelism, DP）

最朴素的扩展方式：把同一份模型复制 \( d \) 份（\( d \) 个「进程/.rank」），每个 rank 拿不同的 mini-batch 分别做前向和反向，再把各自算出的梯度**求平均**，保证所有副本的参数同步更新。`toy_train.py` 目前是单进程版本，没有任何进程间通信；本讲实践会把它改成 DP。

### 2.2 训练一步要占多少显存

以 float32 训练（`toy_train.py` 实际情况——u3-l2 已确认 `Qwen2Config` 里的 `torch_dtype="bfloat16"` 只是元数据，`Qwen2ForCausalLM(config)` 构造出的权重是 float32）为例，设参数量为 \( N \)，每个参数占用：

| 组成部分 | AdamW | Moonlight 版 Muon |
|---|---|---|
| 参数本身 | 4 字节 | 4 字节 |
| 梯度 | 4 字节 | 4 字节 |
| 优化器状态 | 一阶动量 4 + 二阶动量 4 = 8 字节 | Muon 分支只有动量缓冲 4 字节；AdamW 后备分支（嵌入等）仍是 8 字节 |

关键观察：**优化器状态与参数同形状、同寿命**（见 4.1.3 的源码），但 Muon 的矩阵参数只需要一个动量缓冲，比 AdamW 少一份二阶矩。参数量越大，状态占的显存越可观——这就是 ZeRO 要优化的对象。

### 2.3 三种集合通信原语

分布式训练的通信几乎都由三个原语拼成（\( d \) 为 rank 数，\( \Phi \) 为参与通信的张量总字节数）：

- **all-reduce**：每个 rank 都得到「所有 rank 梯度之和」。环形实现下通信量为 \( \frac{2(d-1)}{d}\Phi \)。
- **reduce-scatter**：梯度**求和后按 rank 切片**，第 \( i \) 个 rank 只拿到第 \( i \) 片。通信量 \( \frac{d-1}{d}\Phi \)。
- **all-gather**：每个 rank 把自己的一份数据拼成完整张量发给所有人。通信量 \( \frac{d-1}{d}\Phi \)。

注意恒等关系：一次 all-reduce ≈ 一次 reduce-scatter + 一次 all-gather。这个等式是理解「ZeRO-1 不增加总通信量」的钥匙（见 4.2）。

### 2.4 ZeRO 的三个级别

ZeRO（Zero Redundancy Optimizer）按「冗余在哪里」分三级：ZeRO-1 切**优化器状态**，ZeRO-2 再切**梯度**，ZeRO-3 再切**参数本身**。级别越高越省显存，但改动越大。README 中 Moonlight 选择的是 ZeRO-1 风格——只切优化器状态。

## 3. 本讲源码地图

| 文件 | 角色 |
|---|---|
| [README.md](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/README.md) | 本讲最核心的可验证材料：分布式 Muon 的三点官方声明（内存最优、通信高效、保持数学性质）与 Megatron-LM PR #1428 的入口链接 |
| [examples/toy_train.py](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py) | 单进程参照系：`Muon.step` 的逐参数循环（分片的最小单元）、优化器状态的定义位置、将被改造为 DDP 的训练主循环 |
| Moonlight.pdf | 技术报告。分布式实现章节的细节以 PDF 原文为准（本环境无法提取其文本，涉及处标注待确认） |
| Megatron-LM PR #1428（外部） | 工业级分布式 Muon 实现，链接见 README 第 10 行。仓库外资源，细节待确认 |

## 4. 核心概念与源码讲解

### 4.1 ZeRO-1 状态分片

#### 4.1.1 概念说明

ZeRO-1 的出发点是一个朴素的事实：数据并行下每个 rank 都持有一份**完整**的优化器状态，但这些状态互不相同的工作其实可以拆开——第 \( i \) 个 rank 只负责第 \( i \) 片参数的状态维护与更新。分片后：

\[ \text{每 rank 状态显存} = \frac{\text{状态总量}}{d}, \quad d = \text{数据并行度} \]

配合 2.2 的表格：AdamW 的状态是 8 字节/参数，Muon 矩阵参数只要 4 字节/参数。所以「Muon + ZeRO-1」是双重节省——**算法本身先省一半状态，ZeRO-1 再除以并行度**。这就是 README 中「memory optimal」的直观含义。

为什么 Muon 的状态天然更小？回看 u2-l3/u2-l5：Muon 分支只需一个 `momentum_buffer`（动量缓冲），没有二阶矩；AdamW 分支（嵌入矩阵 + 一维 norm 向量）才需要 `moment1`/`moment2` 两份。

#### 4.1.2 核心流程

单进程一步（现状，`toy_train.py`）：

```text
for 每个参数 p（完整遍历）:
    g = p.grad
    维护 p 自己的优化器状态（动量等）
    计算更新并写回 p
```

ZeRO-1 一步（目标形态，伪代码）：

```text
① 各 rank 分别前向/反向，得到本地梯度
② reduce-scatter：梯度和按 rank 切片，rank i 只拿到第 i 片梯度
③ rank i 只对分到的那部分参数：
     读取本地状态分片 → 计算更新（Muon：动量 + Newton-Schulz + 缩放）→ 更新参数分片
④ all-gather：把各 rank 更新后的参数分片拼回完整参数，广播给所有 rank
⑤ 下一个 mini-batch，回到 ①
```

要点：**分片的单位是「参数」，不是「矩阵的行/列」**。这一点对 Muon 尤其重要——Newton-Schulz 正交化是对整个矩阵做的运算，如果把一个矩阵切开分给不同 rank，谁也无法独立完成正交化；而按参数整存整取，每个 rank 拿到的是若干完整矩阵，正交化可以就地完成。

#### 4.1.3 源码精读

**优化器状态定义在哪里。** Muon 分支的状态只有一个动量缓冲，惰性创建：

[examples/toy_train.py:L185-L189](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L185-L189)

```python
state = self.state[p]
if "momentum_buffer" not in state:
    state["momentum_buffer"] = torch.zeros_like(g)
buf = state["momentum_buffer"]
buf.mul_(momentum).add_(g)
```

这段代码做了什么：`torch.zeros_like(g)` 按梯度形状开一块与参数同形状的显存——这就是 ZeRO-1 要切分的对象。注意它是**逐参数独立**的：状态键是 `self.state[p]`，状态更新 `buf.mul_(momentum).add_(g)` 只依赖这个参数自己的梯度和自己的缓冲，与任何其他参数无关。

AdamW 后备分支的状态则是三件套：

[examples/toy_train.py:L219-L227](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L219-L227)

```python
if "step" not in state:
    state["step"] = 0
    state["moment1"] = torch.zeros_like(g)
    state["moment2"] = torch.zeros_like(g)
```

这段代码做了什么：一阶矩 + 二阶矩 + 步数计数器。同样逐参数独立。

**逐参数更新循环——分片的最小单元。**

[examples/toy_train.py:L175-L203](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L175-L203)

```python
for p in params:
    g = p.grad
    ...
    u = zeropower_via_newtonschulz5(g, steps=group["ns_steps"])
    adjusted_lr = self.adjust_lr_for_muon(lr, p.shape)
    p.data.mul_(1 - lr * wd)
    p.data.add_(u, alpha=-adjusted_lr)
```

这段代码做了什么：Muon 分支的整个更新过程——动量、正交化、按形状缩放（u2-l4 的 `adjust_lr_for_muon`，[examples/toy_train.py:L142-L148](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L142-L148)）、权重衰减、写回——全部发生在单参数闭包内，**没有任何跨参数依赖**。把这 84 个矩阵参数任意划分给 \( d \) 个 rank，每个 rank 对自己的子集执行同一段代码，合并结果与单进程逐个执行完全一致。这就是 README 说「保持算法数学性质」在代码层面的依据：分片只是把 `for p in params` 这个循环拆给多个人做，循环体一字不改。

**README 的官方声明。**

[README.md:L29](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/README.md#L29)

> We develop a distributed version of Muon with ZeRO-1 style optimization, achieving optimal memory efficiency and reduced communication overhead while preserving the mathematical properties of the algorithm.

这句英文是本讲一切「论文侧」论述的锚点：ZeRO-1 风格、内存最优、通信开销降低、保持数学性质。四个关键词分别对应本讲 4.1、4.2、4.3 的展开。

#### 4.1.4 代码实践

1. **实践目标**：亲手算一笔「状态显存账」，体会 Muon + ZeRO-1 各自省了多少。
2. **操作步骤**：
   - 写一个独立脚本（示例代码，不属于仓库），对默认配置（`hidden_size=1024`，12 层，`vocab_size=151936`）的模型实例化后统计：

   ```python
   # 示例代码：统计 toy 模型的优化器状态显存（float32 训练口径）
   # 依据 toy_train.py 的分组规则（get_optimizer, L293-L304）
   from transformers import Qwen2Config, Qwen2ForCausalLM
   import torch

   config = Qwen2Config(hidden_size=1024, num_hidden_layers=12,
                        num_attention_heads=16, intermediate_size=4864,
                        vocab_size=151936, tie_word_embeddings=True)
   model = Qwen2ForCausalLM(config)

   muon_bytes = adamw_bytes = 0
   for name, p in model.named_parameters():
       is_muon = p.ndim >= 2 and "embed_tokens" not in name and "lm_head" not in name
       if is_muon:
           muon_bytes += p.numel() * 4        # 仅 momentum_buffer
           adamw_bytes += p.numel() * 8       # exp_avg + exp_avg_sq
       else:
           muon_bytes += p.numel() * 8        # AdamW 后备分支 moment1+moment2
           adamw_bytes += p.numel() * 8
   for d in (1, 2, 4, 8):
       print(f"DP={d}: Muon 状态 {muon_bytes/d/2**30:.2f} GiB/rank, "
             f"AdamW 状态 {adamw_bytes/d/2**30:.2f} GiB/rank")
   ```

   - 也可以直接跑 `python3 -c "from examples import ..."` 风格的统计，或把脚本存为独立文件执行。
3. **需要观察的现象**：Muon 状态总量约是 AdamW 的六到七成（嵌入矩阵占默认模型参数约四成，它走 8 字节的 AdamW 后备分支，拉高了 Muon 的平均值）；DP=8 时每 rank 状态缩为约 1/8。
4. **预期结果**：两组数字都随 \( d \) 线性缩小；Muon 一列恒小于等于 AdamW 一列。具体数值「待本地验证」（取决于 transformers 版本的默认字段，量级应有参考价值）。
5. 若无 GPU，本实践在 CPU 上同样可完成——只统计 `numel`，不涉及训练。

#### 4.1.5 小练习与答案

**练习 1**：为什么 ZeRO-1 不把一个参数矩阵「切开」分给多个 rank？

**答案**：Muon 的核心步骤 Newton-Schulz 正交化（[examples/toy_train.py:L49-L76](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L49-L76)）需要对完整矩阵做 `X @ X.T` 这类全局运算，矩阵的每一行都影响所有奇异值；切成行/列分片后，单个 rank 拿到的局部信息不足以恢复整体的正交化结果，必须额外通信才能补齐，破坏了「分片后本地独立计算」的收益。按参数整存整取则没有这个问题——循环体（L175-L203）对每个参数独立成立。（ZeRO-3 切参数时同样以参数为粒度做 all-gather 再计算。）

**练习 2**：ZeRO-1 分片后，动量缓冲 `momentum_buffer` 的形状在单个 rank 上是什么样？

**答案**：仍然是「完整参数的形状」。因为分片粒度是参数：rank \( i \) 分到的每一个参数 \( p \)，其状态 `zeros_like(p)` 与单进程时完全相同；rank 没分到的参数，则连状态带更新都不在本 rank 上。变化的是「每个 rank 持有多少个参数的状态」，而不是「单个状态张量的形状」。

**练习 3**：若把 `momentum_buffer` 从 float32 换成 bfloat16 存储会有什么收益与风险？

**答案**：收益是状态显存直接减半。风险是动量的累加式 `buf.mul_(momentum).add_(g)` 在低精度下长期运行会有舍入误差累积（小梯度被吞掉），且 u2-l2 已说明 Newton-Schulz 的数值稳定恰恰依赖先做谱归一化。论文实际采用何种精度的状态存储「待确认」（以 Moonlight.pdf 与 PR #1428 为准）。

### 4.2 通信优化模式

#### 4.2.1 概念说明

分布式训练的第二个瓶颈是通信。先看标准 DDP：反向传播过程中，每个 rank 把**完整梯度**用 all-reduce 求平均，通信量为

\[ \text{DDP 每步通信量} = \frac{2(d-1)}{d}\,\Phi \approx 2\Phi \]

其中 \( \Phi \) 是全部梯度的字节数。再看 ZeRO-1 的做法（4.1.2 的伪代码）：梯度用 **reduce-scatter** 分发（每 rank 只收自己那片梯度的和），更新完的参数用 **all-gather** 拼回，总通信量为

\[ \text{ZeRO-1 每步通信量} = \frac{d-1}{d}\Phi + \frac{d-1}{d}\Phi = \frac{2(d-1)}{d}\Phi \]

**两者总通信量相同**——ZeRO-1 的省显存并不是用翻倍通信换来的。这是 ZeRO 论文的经典结论，也解释了 README 为什么可以把「内存最优」与「通信高效」并列为两条优点而不自相矛盾。

「通信优化」还有一层算法相关的含义：Muon 的更新在写回参数前是正交化矩阵 \( u \)（谱范数为 1，见 u2-l2），如果需要在 rank 之间搬运，可以用低精度（bfloat16）表示而不损失方向信息——但论文中具体在哪个环节、以何种精度通信，属于「待确认」的论文细节（见 4.4 的问题清单）。

#### 4.2.2 核心流程

DDP 与 ZeRO-1 一步的对照数据流：

```text
DDP（本讲实践将实现的版本）:
  前向 → 反向(本地梯度) → all-reduce(梯度求平均, 全量Φ)
       → 每个 rank 用【完整的】优化器状态更新【全部】参数 → 下一批

ZeRO-1（论文目标形态）:
  前向 → 反向(本地梯度) → reduce-scatter(梯度和切片, Φ/d 落地每 rank)
       → 每 rank 只更新【1/d 的参数】(Muon: 动量→NS→缩放→衰减)
       → all-gather(更新后的参数分片, 拼回全量) → 下一批
```

两者显存对比：DDP 每 rank 持有全部状态（冗余 \( d \) 份）；ZeRO-1 每 rank 只持 \( 1/d \)。

#### 4.2.3 源码精读

**将被改造的训练主循环。**

[examples/toy_train.py:L347-L356](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L347-L356)

```python
for epoch in range(epoch):
    for step, batch in enumerate(train_loader):
        batch = batch.to(device)
        input_ids = batch
        outputs = model(input_ids=input_ids, labels=input_ids)
        loss = outputs.loss
        loss.backward()
        optimizer.step()
        lr_scheduler.step()
        optimizer.zero_grad()
```

这段代码做了什么：u1-l3 精读过的五连训练步。在 DDP 版本中，`loss.backward()` 这一行会被 DDP 的梯度钩子拦截——每个参数桶（bucket）的梯度一就绪就触发 all-reduce；`optimizer.step()` 之前所有梯度已完成平均。**代码一行都不用改**，通信被自动织入 `backward`，这是理解「DDP 是 ZeRO-1 的退化基线」的最佳入口。

**梯度从哪里来（分片的输入端）。**

[examples/toy_train.py:L175-L179](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L175-L179)

```python
for p in params:
    g = p.grad
    if g is None:
        continue
```

这段代码做了什么：优化器消费的是 `p.grad`。DP 环境下这个属性存放的是「本地梯度的全局平均」（DDP 语义）或「梯度和的本分片」（ZeRO-1 语义）——优化器代码本身对此无感知，通信模式的差异全部被 `p.grad` 这个接口吸收了。这就是好的分层设计：算法与通信解耦。

**README 对通信的另一处表述。**

[README.md:L19](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/README.md#L19)

> We open-source our distributed Muon implementation that is memory optimal and communication efficient.

这段英文做了什么：摘要中确认「开源的分布式实现」同时满足内存最优与通信高效两个性质。注意这句里的 "open-source" 指的是 Megatron-LM PR #1428（见 4.3），不是本仓库的 `toy_train.py`。

#### 4.2.4 代码实践

1. **实践目标**：用具体数字验证「reduce-scatter + all-gather = all-reduce」的通信量等式。
2. **操作步骤**：取 \( d=8 \)，\( \Phi \) = 默认模型梯度总量（上一实践已算出参数量，float32 梯度即 4 字节/参数）。分别代入三个公式：
   - all-reduce：\( \frac{2 \times 7}{8}\Phi = 1.75\Phi \)
   - reduce-scatter：\( \frac{7}{8}\Phi = 0.875\Phi \)；all-gather 同。
3. **需要观察的现象**：0.875 + 0.875 = 1.75，等式成立。
4. **预期结果**：写出一段两三行的算术验证即可；进一步思考：既然总量相同，ZeRO-1 的「通信高效」从哪里来？（提示：通信可以与反向计算重叠；且 reduce-scatter 后每 rank 只需对 \( 1/d \) 的梯度做后续处理，降低了显存带宽压力。此分析为通用背景知识，论文的具体重叠策略「待确认」。）

#### 4.2.5 小练习与答案

**练习 1**：DDP 下每个 rank 的 Muon 动量缓冲会保持一致吗？为什么？

**答案**：会。DDP 把梯度 all-reduce 成相同的平均值，每个 rank 的 `buf.mul_(momentum).add_(g)`（[L189](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L189)）吃进相同的输入；Newton-Schulz 是确定性运算（无随机数），所以各 rank 的状态与更新每一步都保持一致（浮点求和顺序可能造成极微小的位级差异）。代价是：\( d \) 份完全相同的状态占了 \( d \) 倍冗余显存——这正是 ZeRO-1 要消除的。

**练习 2**：ZeRO-1 里 all-gather 搬运的是「梯度」「动量」还是「更新后的参数」？

**答案**：更新后的参数（或等价的参数增量）。梯度的流向是 reduce-scatter（进），参数的流向是 all-gather（出）；动量缓冲始终留在负责该参数的 rank 本地，从不跨 rank 搬运——这是它能被干净分片的原因。

**练习 3**：如果把 DP 度从 8 提到 64，ZeRO-1 每 rank 的状态显存和每步通信量分别怎么变？

**答案**：状态显存除以 64（线性下降，这就是扩展的意义）；通信量 \( \frac{2 \times 63}{64}\Phi \approx 1.97\Phi \)，从 1.75Φ 略增并趋近上界 \( 2\Phi \)——通信量对 \( d \) 不敏感，显存才是随 \( d \) 线性改善的量。

### 4.3 Megatron-LM 集成入口

#### 4.3.1 概念说明

Moonlight 仓库采取「论文 + 玩具复现」策略：`toy_train.py` 验证算法的正确性（单进程），工业级实现（ZeRO-1 分片、与 Megatron 训练框架的流水线/张量并行正交组合）合入 NVIDIA Megatron-LM 的 PR #1428。Megatron-LM 是 NVIDIA 维护的大模型训练框架，其「分布式优化器」（distributed optimizer）本身就实现了 ZeRO-1 风格的优化器状态分片；PR #1428 把 Muon 作为一种新的优化器接入这套现成的分片基础设施。**该 PR 的内部改动（涉及哪些文件、NS 迭代如何与通信重叠、动量精度等）本讲义未核验，标注待确认**——这正是本讲实践要去读的材料。

#### 4.3.2 核心流程

从「想用 Muon 训大模型」到「跑在 Megatron 上」的路径：

```text
本仓库 toy_train.py（算法原型, 单卡）
    ↓ 论文给出两大规模化改造（权重衰减 + 更新 RMS 一致化, u2-l3/u2-l4）
Megatron-LM PR #1428（工业实现: ZeRO-1 分片 + 通信优化）
    ↓ 与 Megatron 的 TP/PP/EP 并行正交组合
Moonlight 16B-A3B MoE 训练（5.7T tokens）
```

#### 4.3.3 源码精读

**入口链接本体。**

[README.md:L7-L11](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/README.md#L7-L11)

```html
<a href="Moonlight.pdf">... Tech Report</a> |  
<a href="https://huggingface.co/moonshotai/Moonlight-16B-A3B">... HuggingFace</a> | 
<a href="https://github.com/NVIDIA/Megatron-LM/pull/1428">... Megatron-LM</a>
```

这段 HTML 做了什么：README 顶部的三个官方入口——技术报告 PDF、HuggingFace 权重、Megatron-LM PR。**`https://github.com/NVIDIA/Megatron-LM/pull/1428` 是分布式 Muon 实现的唯一官方代码入口**，与仓库平级并列，可见其分量。

**Key Ingredients 中的定位。**

[README.md:L23-L31](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/README.md#L23-L31)

这段列表做了什么：三大技术贡献中，「Efficient Distributed Implementation」（L29）位列第二，排在缩放分析（L27）之后、scaling law 验证（L31）之前——分布式实现被视为与算法改进同等级的工程贡献，而不是附注。

**引用信息（找到论文正文的另一途径）。**

[README.md:L145-L152](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/README.md#L145-L152)

这段 BibTeX 做了什么：给出 arXiv 编号 2502.16982。若本地 PDF 阅读不便，可在 arXiv 上检索同名论文《Muon is Scalable for LLM Training》阅读 HTML 版本（网络可达性待本地验证）。

#### 4.3.4 代码实践

1. **实践目标**：建立对 PR #1428 的第一手认知，而不是转述。
2. **操作步骤**：
   - 打开 `https://github.com/NVIDIA/Megatron-LM/pull/1428`；
   - 只看三处：PR 描述（作者如何陈述动机与设计）、`Files changed` 标签页的**文件名清单**（不做逐行阅读，先回答「改了哪些目录」——优化器目录？通信目录？）、以及Review 讨论中出现的技术关键词；
   - 把文件名清单与 4.1.2 的伪流程对号入座：哪里实现「分片」、哪里实现「NS 正交化」、哪里实现「按形状缩放」。
3. **需要观察的现象**：改动是否集中在 Megatron 的 optimizer 相关模块；是否出现 `newtonschulz`/`zeropower`/`muon` 等命名；PR 状态（开放/已合并）。
4. **预期结果**：产出一份 10 行以内的笔记：「PR 标题、状态、改动文件分类、与 toy_train.py 的对应关系」。具体内容「待本地验证」——本讲义作者无法在该环境访问外部网络，不预填任何未经核验的细节。
5. 若网络不可达，退化为阅读 Moonlight.pdf 中 distributed implementation 相关章节（用 PDF 阅读器搜索 "ZeRO"、"distributed" 关键词定位）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 Moonlight 团队把工业实现合入 Megatron-LM，而不是在本仓库自建分布式训练框架？

**答案**（基于仓库事实的合理推断，推断属性已标注）：Megatron-LM 已有成熟的分布式优化器（ZeRO-1 分片基础设施）、张量/流水线并行与 MoE 支持；把 Muon 作为新优化器接入，只需复用分片框架并替换逐参数更新逻辑（toy_train.py L175-L237 那段），工程量与验证成本远低于自建。本仓库则保持极简（单文件示例），降低算法阅读门槛。

**练习 2**：`toy_train.py` 与 Megatron 版 Muon 在「更新 RMS 一致化」上应该有何关系？

**答案**：数学上应等价。`adjust_lr_for_muon`（[L142-L148](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L142-L148)）只依赖 `lr` 与 `p.shape`，都是分片后本 rank 本地可得的量——所以分片不影响该公式的执行。验证 Megatron 版是否也实现了 \( 0.2\sqrt{\max(A,B)} \) 缩放，是阅读 PR 时的必查项（结果待确认）。

### 4.4 论文实现细节（待确认）

#### 4.4.1 概念说明

本模块的存在本身就是方法论示范：**学习一个开源项目时，把「已验证」与「待验证」分开记录**。下表是本讲义写作时（2026-09）的证据状态：

| 命题 | 状态 | 依据 |
|---|---|---|
| 分布式 Muon 采用 ZeRO-1 风格优化 | 已验证 | [README.md:L29](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/README.md#L29) 原文 |
| 实现内存最优、通信高效、保持数学性质 | 已验证（宣称） | 同上，属于作者的定性声明 |
| 工业实现位于 Megatron-LM PR #1428 | 已验证 | [README.md:L10](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/README.md#L10) 链接 |
| 动量缓冲的数值精度（fp32/bf16） | 待确认 | 需读 PDF/PR diff |
| NS 正交化与梯度 reduce-scatter 是否重叠/融合 | 待确认 | 需读 PDF/PR diff |
| 更新后参数以何种精度 all-gather | 待确认 | 需读 PDF/PR diff |
| ZeRO-1 分片与 TP/PP/EP 并行如何组合 | 待确认 | 需读 PDF/PR diff |
| 分布式实现章节的编号与页码 | 待确认 | 本环境无法提取 PDF 文本 |

#### 4.4.2 核心流程

带着问题读论文的标准流程：

```text
① 先读 README 的三点声明（本讲 4.1.3/4.3.3 已引用原文）
② 打开 Moonlight.pdf，全文搜索关键词: "ZeRO", "distributed", "reduce-scatter",
   "all-gather", "memory", "communication"
③ 每找到一处，向上定位所属章节，判断它回答了上表哪个待确认命题
④ 交叉验证: 同一命题到 PR #1428 的代码/diff 里找对应实现
⑤ 把结论回填表格，标注证据页码或文件名
```

#### 4.4.3 源码精读

本模块没有可精读的本仓库源码——这正是它的教学点。唯一的「源码」是 PDF 文件本身：

[Moonlight.pdf](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/Moonlight.pdf)（仓库根目录的技术报告，`git ls-files` 可确认其存在；无法给出 PDF 内部的行号级链接，属正常限制）。

作为对照，可验证的锚点是 README 摘要里对开源范围的完整表述：

[README.md:L14-L21](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/README.md#L14-L21)

这段摘要做了什么：声明了两个开放动作——「open-source our distributed Muon implementation」（对应 Megatron PR）与「release the pretrained, instruction-tuned, and intermediate checkpoints」（对应 HuggingFace，u3-l3 已覆盖）。注意摘要全篇没有说分布式实现在**本仓库**内——初学者常见的误读就在这里。

#### 4.4.4 代码实践

1. **实践目标**：完成 4.4.1 表格中至少三个「待确认」命题的核实。
2. **操作步骤**：按 4.4.2 的流程读 PDF 与 PR；每个命题记录「证据位置 + 一句结论」。
3. **需要观察的现象**：论文表述与 PR 代码是否一致；如有出入，以哪个为准（通常以代码为准，论文可能滞后）。
4. **预期结果**：一张填好的命题表。若某命题在两处都找不到明确答案，保留「待确认」并写下你自己的实验验证方案（例如：在 Megatron 里跑两卡 Muon，用 nsys/PyTorch profiler 抓通信原语的调用序列）。

#### 4.4.5 小练习与答案

**练习 1**：设计一个「黑盒」实验，不读 Megatron 源码也能判断其 Muon 是否做了状态分片。

**答案**：固定模型与并行度，分别用（a）Megatron+AdamW、（b）Megatron+Muon、（c）单卡 Muon 运行，记录每 rank 的优化器显存占用（torch.cuda.max_memory_allocated 或框架日志）。若 (b) 的状态占用约为 (c) 的 \( 1/d \) 且明显低于「不分片」的理论值，即可判定分片生效。

**练习 2**：为什么「保持数学性质」（与单卡等价）对 Muon 的多卡验证特别重要？

**答案**：Moonlight 的核心论证链是「toy 上的算法改进（u2 系列）→ 大规模训练收益」。如果分布式版本在数学上不等价（例如分片导致 NS 在不完整矩阵上计算），16B 训练的收益就无法归因于算法本身。逐参数独立 updates（L175-L203 无跨参数依赖）使等价性在代码结构上成立，这让「算法贡献」与「系统贡献」可以被分开评估。

## 5. 综合实践

**任务：把 `toy_train.py` 改造成 DDP 版本，绘制通信数据流图，并写清楚它与论文 ZeRO-1 方案的差距。**

### 5.1 实践目标

1. 获得一个可运行的两卡（或 CPU 两进程）Muon 分布式训练。
2. 亲手确认 DDP 下 Muon 状态的放置方式与冗余问题。
3. 用对照表说清 DDP 版与 ZeRO-1 版在「显存、通信、代码改动量」三个维度的差异。

### 5.2 改造步骤（示例代码）

以下改动均为**示例代码**（仓库原有代码的修改建议，非仓库内容）。建议先复制为 `examples/toy_train_ddp.py` 再动手（注意：本讲义规定不改源码，请在自己的副本上实验）。

**第一步：初始化进程组（放在 `__main__` 开头，[L316-L327](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L316-L327) 之后）**

```python
import os
import torch.distributed as dist
from torch.nn.parallel import DistributedDataParallel as DDP
from torch.utils.data.distributed import DistributedSampler

dist.init_process_group(backend="nccl")        # 无 GPU 时改用 "gloo"
local_rank = int(os.environ["LOCAL_RANK"])
torch.cuda.set_device(local_rank)
rank, world_size = dist.get_rank(), dist.get_world_size()
```

**第二步：化解数据管线的两个竞态。** 原代码所有 rank 都会执行分词并写同一个缓存文件：

[examples/toy_train.py:L26-L33](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L26-L33)

```python
if os.path.exists(f"{self.dataset_name}.bin"):
    self.tokens = torch.load(f"{self.dataset_name}.bin")
else:
    for text in tqdm(self.texts, desc="Tokenizing texts"):
        ...
    torch.save(self.tokens, f"{self.dataset_name}.bin")
```

多进程同时进入 `else` 分支会并发写 `openwebtext-100k.bin`，可能写出损坏的缓存。处理方式：让 rank 0 先完成缓存，其余 rank 等待后再构造（示例代码）：

```python
if rank == 0:
    model, train_loader = get_model_and_dataloader(...)   # rank 0 负责下载+分词+写缓存
dist.barrier()
if rank != 0:
    model, train_loader = get_model_and_dataloader(...)   # 其余 rank 命中缓存直接读 .bin
```

**第三步：DataLoader 换用 DistributedSampler**（替换 [L254](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L254) 的 `shuffle=True` 写法，需把 `train_dataset` 传出或在函数内传入 rank 信息）：

```python
sampler = DistributedSampler(train_dataset, num_replicas=world_size, rank=rank, shuffle=True)
train_loader = DataLoader(train_dataset, batch_size=16, sampler=sampler)
```

每个 epoch 开始前调用 `sampler.set_epoch(epoch)`，保证各 epoch 的打乱不同。

**第四步：包 DDP、按 rank 放设备。** 对应原代码 [L336-L337](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L336-L337)：

```python
model = model.to(local_rank)
model = DDP(model, device_ids=[local_rank])     # CPU/gloo 下: DDP(model)
optimizer = get_optimizer(args.optimizer, model.module, lr=args.lr)  # 用裸模块的参数
```

传 `model.module` 的原因：`get_optimizer`（[L287-L311](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L287-L311)）按 `named_parameters()` 的名字过滤 `embed_tokens`/`lm_head`；DDP 包装会加 `module.` 前缀，但子串匹配仍能命中，直接传 `model.module` 则与单进程完全一致，优化器构造时机在 `model.to(device)` 之后也安全（u1-l3 已确认状态是惰性创建的）。

**第五步：日志与循环微调。** [L327](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L327) 的日志文件名加 rank 后缀避免多进程写同一文件；训练循环（L347-L356）本身不用改一行——`loss.backward()` 会自动触发梯度 all-reduce；建议日志只在 rank 0 打印。注意 `DistributedSampler` 会让 `len(train_loader)` 变为 `ceil(样本数/world_size/16)`，从而轻微改变 cosine 调度的总步数（L341-L346）——若要与单进程严格对照，把 `num_training_steps` 固定为单进程值即可。

**第六步：启动。**

```bash
# 单机双卡
torchrun --standalone --nproc_per_node=2 examples/toy_train_ddp.py \
    --optimizer muon --hidden_size 512 --lr 1e-3
# 无 GPU 时（nccl 换 gloo 后）同样命令可用 CPU 验证流程
```

**运行结果「待本地验证」**（本讲义写作环境未执行该实验）。可观察的检查点：两个进程的 loss 序列应几乎同步下降（可容忍浮点尾差）；`nvidia-smi` 应看到两份等大的优化器状态显存。

### 5.3 回答两个核心问题

**问题一：DDP 下 Muon 的状态应放在哪里？**

每个 rank 本地、完整一份。证据链：DDP 保证 `p.grad` 各 rank 相同（平均后的梯度）→ `momentum_buffer` 的更新式 `buf.mul_(momentum).add_(g)`（[L189](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L189)）输入相同 → NS 是确定性运算 → 各 rank 状态与更新始终一致。所以 DDP 版**不需要任何跨 rank 的状态同步**，但也因此冗余了 \( d \) 倍状态显存。

**问题二：这个 DDP 版本与论文的 ZeRO-1 方案差在哪里？**

| 维度 | 你的 DDP 版 | 论文 ZeRO-1 版 |
|---|---|---|
| 优化器状态 | 每 rank 完整一份（\( d \) 倍冗余） | 每 rank 只 \( 1/d \)，按参数粒度分片 |
| 梯度通信 | all-reduce 全量梯度 | reduce-scatter，每 rank 落地 \( 1/d \) |
| 参数同步 | 无需（各 rank 更新一致） | all-gather 更新后的参数分片 |
| 每步总通信量 | \( \frac{2(d-1)}{d}\Phi \) | 相同（4.2 的等式） |
| 有效 batch | 16×world_size（lr 可能需要重调） | 同样随 DP 度变大 |
| 实现载体 | `torch.nn.parallel.DDP` 装饰式改造 | Megatron-LM PR #1428（细节待确认） |

一句话总结差距：**DDP 版只借走了「梯度平均」这一步通信，把 ZeRO-1 的核心收益（状态分片省显存）完全留在了桌上**；两者通信总量相同，差别全在显存与扩展性。

### 5.4 交付物

1. 可运行的 `toy_train_ddp.py`（自己的副本）。
2. 一张手绘或文本版的通信数据流图，包含两种模式（DDP / ZeRO-1），标出每个箭头用的原语与张量（梯度、状态、参数）。
3. 4.4.1 命题表中至少三项的核实结论。

## 6. 本讲小结

- ZeRO-1 分片的对象是**优化器状态**，分片粒度是**参数**；Muon 的状态只有一个动量缓冲（4 字节/参数），天然比 AdamW（8 字节/参数）省一半，再叠加 \( 1/d \) 的分片收益。
- 通信三原语的恒等式 reduce-scatter + all-gather ≡ all-reduce 决定了 ZeRO-1 与 DDP **总通信量相同**——省显存不以翻倍通信为代价。
- `toy_train.py` 的 Muon 更新循环（L175-L203）逐参数独立、无跨参数依赖，这是「分片后保持数学性质」的直接代码依据；`adjust_lr_for_muon` 只依赖 `lr` 与 `p.shape`，分片后本地可得。
- 分布式 Muon 的工业实现**不在本仓库**：官方入口是 README 第 10 行链接的 Megatron-LM PR #1428 与 Moonlight.pdf；论文内部细节（状态精度、通信重叠、与 TP/PP 组合）本讲标注待确认，并给出了核实路径。
- 综合实践把 toy 训练改造为 DDP：状态各 rank 完整冗余、更新自动保持一致；与 ZeRO-1 的差距集中在显存，而非通信。

## 7. 下一步学习建议

- 下一讲（u3-l5）回到本仓库源码，练习二次开发：在 `get_optimizer` 中接入自定义优化器组合、在 `name2path` 中接入新数据集——你会再次用到本讲的 DDP 副本做回归对照。
- 延伸阅读顺序建议：Moonlight.pdf 的分布式实现章节（带着 4.4.1 的命题表）→ Megatron-LM PR #1428 的 Files changed → Megatron-LM 官方文档中 Distributed Optimizer 一节（理解现成的 ZeRO-1 基础设施）→ KellerJordan/Muon 仓库（`toy_train.py` L46-L47 注释标明的上游来源，对比单卡原版与 Moonlight 改造版的差异）。
- 若想动手验证通信行为：在 DDP 副本上用 `torch.profiler` 抓一次 `loss.backward()`，观察 `nccl:all_reduce` 事件的触发时序与桶（bucket）聚合，把 4.2 的公式变成可见的火焰图。
