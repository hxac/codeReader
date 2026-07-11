# MatMul：朴素版与缓存分块版

## 1. 本讲目标

矩阵乘法（matrix multiplication，下称 matmul）是整个 GPT-2 里出现最频繁、也是最耗时的算子。学完本讲，你应当能够：

- 说清「一个线性层 = 一次 matmul + bias」的数学含义，以及 `(B,T,C)`、权重 `(OC,C)`、输出 `(B,T,OC)` 三者的指针寻址方式。
- 看懂 `train_gpt2.c` 里 `matmul_forward_naive` 与带 cache-blocking（分块）的 `matmul_forward` 两版前向，并解释为什么分块版能把权重的内存流量降低到原来的 \(1/8\)。
- 独立推导并实现 `matmul_backward` 对「输入、权重、偏置」三路梯度的计算，理解为什么必须拆成两轮循环才能高效并行。
- 用 `dev/cpu/matmul_forward.c` 这个独立小工具验证两版前向输出一致并对它们计时。

## 2. 前置知识

在进入本讲前，你需要先具备以下认知（来自前置讲义）：

- **行主序与一维数组指针算术**：llm.c 不用真正的多维数组，所有张量都是「一维 `float*` + 行主序」。寻址统一写成「基地址 + 行号 × 每行元素数 + 列号」。本讲会反复用到这个公式。
- **下一个 token 预测与残差流维度 C**：残差流始终保持通道数 `C`（GPT-2 124M 中为 768）。matmul 负责把残差流「投影」到不同维度（如注意力用的 `3C`、MLP 隐藏层用的 `4C`、词表 `Vp`），再投影回 `C`。
- **梯度的累加（`+=`）与 `gpt2_zero_grad`**：前置讲义（u2-l1 编码层、u2-l2 LayerNorm）已经讲过，反向时同一目标会被多处写入，因此梯度用 `+=` 累加，并依赖每步开头的清零。本讲的 `matmul_backward` 同样遵循这一规则。
- **`B/T/C/V/L` 缩写**：batch、序列长度、通道数、词表大小、层数；本讲额外引入 **`OC`（output channels，输出通道数）**。

一个直觉：神经网络里几乎所有的「可学习参数」都装在 matmul 的权重矩阵里。GPT-2 124M 约 1.24 亿参数，绝大多数就分布在 `qkvw/attprojw/fcw/fcprojw` 这些 `(OC,C)` 矩阵中。所以「优化 matmul」≈「优化整个训练」。

## 3. 本讲源码地图

本讲只涉及两个文件，但它们从不同角度讲同一件事：

| 文件 | 作用 | 关键位置 |
| --- | --- | --- |
| `train_gpt2.c` | CPU 参考实现。包含朴素前向、分块前向、三路反向，以及它们在 `gpt2_forward` / `gpt2_backward` 中的真实调用点 | `matmul_forward_naive`、`matmul_forward`、`matmul_backward` |
| `dev/cpu/matmul_forward.c` | 独立的「教学 + benchmark」小程序。把朴素版与分块版各封装成一个 kernel，自动比对正确性并计时 | `matmul_forward_cpu`、`matmul_forward_ngc92`、`main` |

一句话区分：`train_gpt2.c` 里的 matmul 是「真正跑训练时用的」；`dev/cpu/matmul_forward.c` 是「专门用来对比两版实现、观察加速比」的沙盒。

## 4. 核心概念与源码讲解

### 4.1 朴素 matmul 与指针寻址

#### 4.1.1 概念说明

一个线性层做的事情可以用一个公式概括：

\[
\text{out}[b,t,o] = \text{bias}[o] + \sum_{i=0}^{C-1} \text{inp}[b,t,i] \cdot \text{weight}[o,i]
\]

写成矩阵形式（把 `(B,T)` 两个维度拍平成 `B*T` 行）：

\[
\text{Out} = \text{Inp}\, W^\top + \text{bias}
\]

其中：

- `inp` 形状 `(B,T,C)`：每个 batch、每个时间位置上有一个长度为 `C` 的向量（比如 LayerNorm 之后的残差流）。
- `weight` 形状 `(OC, C)`：**注意是 `(OC,C)` 而不是 `(C,OC)`**。第 `o` 个输出通道对应权重矩阵的第 `o` 行 `weight[o,:]`。所以公式里出现的是 \(W^\top\)。
- `bias` 形状 `(OC,)`：每个输出通道一个偏置，对所有 `(b,t)` 位置**广播**（即每个位置都加上同一个 `bias[o]`）。
- `out` 形状 `(B,T,OC)`。

为什么要强调权重的形状？因为它直接决定了指针寻址公式：`weight[o*C + i]`（行 `o`、列 `i`）。在 GPT-2 里不同线性层的 `OC` 不同：注意力 QKV 投影 `OC=3C`、注意力输出投影 `OC=C`、MLP 第一层 `OC=4C`、MLP 第二层把 `4C` 投影回 `C`、最后的 logits 投影 `OC=Vp`（词表大小）。

matmul 还有一个重要特性：**它对输出元素是完全并行的（embarrassingly parallel）**。每个 `out[b,t,o]` 只依赖自己的那行权重和那个 `(b,t)` 的输入，输出元素之间互不依赖。这正是后面能用 OpenMP `collapse(2)` 大规模并行的前提。

#### 4.1.2 核心流程

朴素前向就是一个「四重循环」，伪代码如下：

```
for b in 0..B:                  # 第 1 维：batch
    for t in 0..T:              # 第 2 维：时间
        bt = b*T + t            # 把 (b,t) 拍平
        for o in 0..OC:         # 第 3 维：输出通道
            val = bias[o] if bias else 0
            for i in 0..C:      # 第 4 维：输入通道（内积）
                val += inp[bt*C + i] * weight[o*C + i]
            out[bt*OC + o] = val
```

关键点：

1. **循环顺序**是 `b → t → o → i`。最内层 `i` 做一次长度为 `C` 的内积（点乘）。
2. **bias 广播**：每个 `(b,t,o)` 都从同一个 `bias[o]` 起步累加，体现「偏置与位置无关」。
3. **指针寻址**完全套用「基地址 + 行号 × 每行元素数 + 列号」：`inp[bt*C + i]`、`weight[o*C + i]`、`out[bt*OC + o]`。
4. **并行**：用 `#pragma omp parallel for collapse(2)` 把 `b`、`t` 两层循环合并，每个 `(b,t)` 交给一个线程，互不干扰。

计算量：一次前向 matmul 的浮点运算数为 \(2 \cdot B \cdot T \cdot C \cdot OC\)（乘法和加法各算一次）。这也是源码注释说「most of the running time is spent here」的由来。

#### 4.1.3 源码精读

朴素前向在 [train_gpt2.c:163-182](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L163-L182)，逐行说明：

- 第 169 行 `#pragma omp parallel for collapse(2)`：把 `b`、`t` 两个外层循环合并后并行，每个 `(b,t)` 是一个独立工作单元。
- 第 172 行 `int bt = b * T + t;`：把二维 `(b,t)` 拍平成一维索引，后面寻址都基于 `bt`。
- 第 174 行 `float val = (bias != NULL) ? bias[o] : 0.0f;`：偏置可选（最后的 logits 投影用 `wte` 当权重、没有偏置，会传 `NULL`）。
- 第 175–177 行：长度为 `C` 的内积循环，`inp[bt*C + i] * weight[o*C + i]`。
- 第 178 行：写出 `out[bt*OC + o] = val;`。

`dev/cpu/matmul_forward.c` 里有一个算法完全等价、但写法更「教科书」的参考实现 `matmul_forward_cpu`，见 [dev/cpu/matmul_forward.c:21-41](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/dev/cpu/matmul_forward.c#L21-L41)。它和朴素版的唯一区别是**把基地址提前算好**，内层循环更干净：

```c
float* out_bt = out + b * T * OC + t * OC;     // 这一行输出的起点
const float* inp_bt = inp + b * T * C + t * C; // 这个 (b,t) 的输入向量
for (int o = 0; o < OC; o++) {
    float val = (bias != NULL) ? bias[o] : 0.0f;
    const float* wrow = weight + o*C;          // 第 o 个输出通道的权重行
    for (int i = 0; i < C; i++) {
        val += inp_bt[i] * wrow[i];
    }
    out_bt[o] = val;
}
```

这种 `wrow = weight + o*C` 的写法把「行指针」提出来，循环体里只剩 `inp_bt[i] * wrow[i]`，可读性更好，也方便编译器做向量化。朴素版和它**计算结果相同**，只是把指针运算内联进了下标里。

#### 4.1.4 代码实践

这是一个「手算 + 验证指针寻址」的源码阅读型实践。

1. **实践目标**：确认你真的理解了 `(B,T,C)` 与 `(OC,C)` 的指针寻址，而不是凭感觉。
2. **操作步骤**：
   - 取一组极小参数：`B=1, T=2, C=3, OC=2`。
   - 手工构造 `inp = [1,2,3, 4,5,6]`（两个长度为 3 的向量）、`weight = [1,0,0, 0,1,1]`（两行：第 0 行 `[1,0,0]`、第 1 行 `[0,1,1]`）、`bias = [10, 20]`。
   - 套用公式手算 `out[0,0,0]`、`out[0,0,1]`、`out[0,1,0]`、`out[0,1,1]`。
3. **需要观察的现象**：核对你的手算结果是否等于按 `bt*C+i`、`o*C+i` 这些下标逐项代入的值。
4. **预期结果**：
   - `out[0,0,0] = bias[0] + inp[0]*weight[0] + inp[1]*weight[1] + inp[2]*weight[2] = 10 + 1*1 + 2*0 + 3*0 = 11`
   - `out[0,0,1] = 20 + 1*0 + 2*1 + 3*1 = 25`
   - `out[0,1,0] = 10 + 4*1 + 5*0 + 6*0 = 14`
   - `out[0,1,1] = 20 + 4*0 + 5*1 + 6*1 = 31`
   - 即 `out = [11,25, 14,31]`。
5. 如果你愿意，可以照搬 `matmul_forward_naive` 的循环写一个 10 行左右的最小 C 程序（**示例代码**，非项目原有）打印上述结果验证；若不便编译，本步骤可标注「待本地验证」，手算结果已足以确认理解。

#### 4.1.5 小练习与答案

**练习 1**：为什么权重的形状是 `(OC, C)` 而不是 `(C, OC)`？如果把权重存成 `(C, OC)`，内层循环要怎么改？

**答案**：存成 `(OC, C)` 是为了让「同一个输出通道 `o` 的所有权重」在内存里连续（占第 `o` 行），内积循环沿 `i` 连续读 `weight[o*C + i]`，cache 友好。若改成 `(C, OC)`，则第 `o` 个输出通道的权重散落在 `weight[i*OC + o]`（步长为 `OC`），内层循环变成了跨步访问，性能下降；公式也要相应改成 `weight[i*OC + o]`。

**练习 2**：朴素版最外两层用 `collapse(2)` 并行了 `(b,t)`，能不能改成并行 `o`（输出通道）？两者哪个更好？

**答案**：理论上可以，因为输出元素互不依赖，并行 `(b,t)` 或并行 `o` 都不会产生写冲突。但并行 `(b,t)` 通常更好：`(b,t)` 的取值数 `B*T` 远大于 `OC`（例如 `B*T` 可达数千而 `OC=768`），能切分出更多、更细粒度的工作单元，负载更均衡。

---

### 4.2 cache blocking 版本

#### 4.2.1 概念说明

朴素版正确，但**慢**。瓶颈不在计算，而在**访存**。我们来看朴素版的访存行为：

- 循环顺序是 `b → t → o → i`。对固定的一个 `(b,t)`，要遍历全部 `OC` 个输出通道，于是要把**整个权重矩阵**（`OC*C` 个元素）从头到尾扫一遍。
- 处理完一个 `(b,t)`，转到下一个 `(b,t)` 时，**又要把整个权重矩阵重新扫一遍**。
- 所以权重被重复加载了 `B*T` 次。

权重大到什么程度？以 MLP 的 `fch` 层为例，`weight` 是 `(4C, C) = (3072, 768)`，约 236 万个 float、约 **9.4 MB**，远超 typical 的 L2 cache（几 MB）。于是几乎每次重扫都是 cache miss，要回到主存取数。朴素版的权重内存流量为：

\[
\text{naive 权重流量} = B \cdot T \cdot OC \cdot C \cdot 4 \text{ 字节}
\]

**cache blocking（分块）** 的核心思路：与其每次只处理 1 个 `(b,t)`，不如**一次处理一小块（`LOOP_UNROLL=8` 个）连续的 `(b,t)`**，让从主存取来的一个权重元素在被淘汰前被**复用 8 次**。这把权重流量降到原来的 \(1/8\)：

\[
\text{tiled 权重流量} = \frac{B \cdot T}{8} \cdot OC \cdot C \cdot 4 \text{ 字节}
\]

这种技巧有时也叫 **register tiling**（寄存器分块）：把 8 个中间结果 `result[0..7]` 一直留在寄存器里，把权重 `w` 也读进寄存器后复用 8 次，最后才一次性写回主存。

#### 4.2.2 核心流程

分块版的循环顺序与朴素版**完全不同**，关键在于把「对 `bt` 的循环」从最外层挪到最内层并展开：

```
LOOP_UNROLL = 8
if B*T 不是 8 的倍数: 回退到朴素版            # 防止越界

for obt in 0..B*T step 8:                    # 外层：每次处理 8 个 (b,t)
    for o in 0..OC:                          # 输出通道
        result[0..7] = bias[o]               # 8 个累加器放寄存器，初值=bias
        for i in 0..C:                       # 内积维
            w = weight[i + o*C]              # ★ 一个权重元素只从主存读一次
            for ibt in 0..8:                 # ★ 复用 w 八次
                bt = obt + ibt
                result[ibt] += inp[bt*C + i] * w
        for ibt in 0..8:                     # 写回 8 个输出
            out[(obt+ibt)*OC + o] = result[ibt]
```

两个关键改动：

1. **权重只读一次，复用 8 次**：内层 `for ibt` 把同一个 `w` 连乘 8 个不同 `(b,t)` 的输入。这就是流量降为 \(1/8\) 的来源。
2. **8 个累加器常驻寄存器**：`result[LOOP_UNROLL]` 在整个 `i` 循环期间不落主存，循环结束才写回。配合 `-Ofast`，编译器会把内层 `ibt` 循环编译成一串 **FMA（fused multiply-add，融合乘加）** 指令，吞吐更高。

注意一个代价：内层读 `inp[bt*C + i]` 时，8 个 `bt` 之间的步长是 `C`（跨步访问，是一种 gather）。也就是说我们用「输入的跨步读」换来了「权重的 8 倍复用」。因为权重才是那个大到装不进 cache 的大数组，这笔交易非常划算。

还有一个**安全回退**：分块要求 `B*T` 是 8 的倍数，否则最后一个不完整块会写出界。所以代码开头先判断 `B*T % 8 != 0`，不满足就回退到朴素版。GPT-2 训练时 `B*T` 通常是 8 的倍数（如 `B=4..512`、`T=1024`），所以正常情况下走的是分块路径。

#### 4.2.3 源码精读

分块前向在 [train_gpt2.c:184-229](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L184-L229)，要点：

- 第 195–199 行：定义 `LOOP_UNROLL = 8`，并做 `B*T % 8 != 0` 的回退判断，不满足就调用 `matmul_forward_naive`。
- 第 203 行 `#pragma omp parallel for`：外层对 `obt` 块并行（每个块 = 8 个 `(b,t)` 是一个工作单元）。
- 第 207 行 `float result[LOOP_UNROLL];`：8 个寄存器累加器。
- 第 209–211 行：用 `bias[o]`（若存在）初始化 8 个累加器。
- 第 215–216 行：`float w = weight[i + o * C];` —— **整个优化最核心的一行**，权重元素读进寄存器。
- 第 217–220 行：内层 `ibt` 循环，用同一个 `w` 给 8 个累加器各做一次乘加。
- 第 223–226 行：把 8 个结果写回 `out`。

同样的算法在 `dev/cpu/matmul_forward.c` 里被独立实现为 `matmul_forward_ngc92`（`ngc92` 是贡献者的 GitHub 用户名，对应仓库 git 历史中的优化提交），见 [dev/cpu/matmul_forward.c:43-88](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/dev/cpu/matmul_forward.c#L43-L88)。它和 `train_gpt2.c` 的分块版几乎逐行一致，区别只是：它在不满足 `B*T % 8 == 0` 时直接 `printf("MUST BE A MULTIPLE OF 8")` 然后 `return`（教学版，不做回退），而主线版会优雅回退到朴素实现。

#### 4.2.4 代码实践

这是本讲的主实践：**编译运行 `dev/cpu/matmul_forward.c`，对比两版前向的正确性与耗时**。

1. **实践目标**：亲眼看到「分块版与朴素版输出一致（在 1e-5 容差内）」并且「分块版更快」，从而理解 cache blocking 的收益。
2. **操作步骤**：
   - 该文件顶部给出了 MSVC 的编译示例。在 Linux 上可用 gcc（**示例命令**，非项目原有）：
     ```
     gcc -O3 dev/cpu/matmul_forward.c -o matmul_forward -lm
     ./matmul_forward
     ```
     （个别较老的 glibc 可能需要追加 `-lrt` 以链接 `clock_gettime`。）
   - `main` 函数（[dev/cpu/matmul_forward.c:114-186](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/dev/cpu/matmul_forward.c#L114-L186)）会先用 `matmul_forward_cpu` 算出参考结果，再依次验证 kernel 0（朴素）和 kernel 1（分块），最后对两者分别计时。
3. **需要观察的现象**：
   - 验证阶段应打印每个 kernel 的前 5 个元素对比，最后输出 `OK`，以及总览 `All kernels passed! Starting benchmarks.`。
   - 计时阶段会分别打印 `Kernel #0, (took ... ms)` 和 `Kernel #1, (took ... ms)`。
4. **预期结果**：两个 kernel 都通过验证（说明分块没有改变数值结果，只是重排了循环）；耗时上 **kernel #1（分块）应明显快于 kernel #0（朴素）**。具体加速比取决于你的 CPU 与 cache 大小，**待本地验证**，但典型情况下能观察到数倍提升。
5. 若你机器上没有 gcc 或无法编译，可改为「源码阅读型实践」：对照 [train_gpt2.c:215-220](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L215-L220) 解释「`w` 被读一次、在内层 `ibt` 循环里被复用 8 次」如何把权重内存流量降到 \(1/8\)。

#### 4.2.5 小练习与答案

**练习 1**：分块版的内层读 `inp[(obt+ibt)*C + i]` 是跨步 `C` 的访问，为什么这个「不友好」的访问模式整体上仍然划算？

**答案**：因为我们要最小化的是**权重**的流量。权重 `(OC,C)` 是大数组（如 9.4MB），朴素版要重复扫 `B*T` 次；分块版通过复用把权重流量降到 \(1/8\)。输入 `inp` 虽然变成了跨步读，但每个 `inp` 元素本就只被用一次（在朴素版里也是流式读），跨步读只是损失了部分 cache 局部性，换来的却是权重大幅减流——净收益为正。

**练习 2**：把 `LOOP_UNROLL` 从 8 改成 16，一定更快吗？

**答案**：不一定。增大 `LOOP_UNROLL` 能提高权重复用倍数，但也意味着要在寄存器里同时持有更多累加器（`result[16]`），增加**寄存器压力**；一旦寄存器溢出（spill 到栈），反而变慢。8 是作者在「复用收益」与「寄存器压力」之间权衡后的经验值。

---

### 4.3 matmul_backward 三路梯度

#### 4.3.1 概念说明

反向传播要回答的问题：已知上游传回来的梯度 `dout`（形状与 `out` 相同，`(B,T,OC)`），求对三个输入 `inp`、`weight`、`bias` 的梯度 `dinp`、`dweight`、`dbias`。

对前向公式 \(\text{out}[b,t,o] = \text{bias}[o] + \sum_i \text{inp}[b,t,i] \cdot \text{weight}[o,i]\) 逐项求偏导，得到三路梯度：

\[
\text{dinp}[b,t,i] = \sum_{o=0}^{OC-1} \text{dout}[b,t,o] \cdot \text{weight}[o,i]
\]

\[
\text{dweight}[o,i] = \sum_{b,t} \text{dout}[b,t,o] \cdot \text{inp}[b,t,i]
\]

\[
\text{dbias}[o] = \sum_{b,t} \text{dout}[b,t,o]
\]

直觉解读：

- **`dinp`**：求和沿输出通道 `o` 进行。注意这里用的是**前向的权重 `weight`**（不是 `dweight`），因为 \(\partial \text{out}/\partial \text{inp} = \text{weight}\)。这正是反向需要前向权重的原因。
- **`dweight`**：求和沿所有 `(b,t)` 位置进行——同一个权重元素 `weight[o,i]` 被全部 `B*T` 个位置共享，所以梯度要全部累加。
- **`dbias`**：最简单，把 `dout` 沿 `(b,t)` 求和即可。

三路梯度全部用 **`+=` 累加**，原因和前置讲义一致：这些梯度缓冲区（尤其是 `dweight`、`dbias`）会被多个位置、甚至多个上层（如 `wte` 同时被 matmul 和 encoder 两处写梯度）写入，必须累加而非覆盖，并依赖 `gpt2_zero_grad` 在每步开头清零。

#### 4.3.2 核心流程

`matmul_backward` 把三路梯度拆成**两轮循环**，每轮选择不同的并行轴以避免写冲突：

**第 1 轮：算 `dinp`，并行 `(b,t)`**

```
for b, t (并行):                       # 每个线程独占一个 (b,t)
    dinp_bt = dinp + (b*T+t)*C
    dout_bt = dout + (b*T+t)*OC
    for o in 0..OC:
        d = dout_bt[o]
        for i in 0..C:
            dinp_bt[i] += weight[o*C+i] * d   # 用前向权重
```

不同线程写不同的 `dinp_bt` 行，**无冲突**。

**第 2 轮：算 `dweight` 与 `dbias`，并行 `o`（输出通道）**

```
for o (并行):                          # 每个线程独占一个输出通道 o
    dwrow = dweight + o*C
    for b, t:
        d = dout[(b*T+t)*OC + o]
        if dbias != NULL: dbias[o] += d
        for i in 0..C:
            dwrow[i] += inp[(b*T+t)*C+i] * d   # 用前向输入 inp
```

不同线程写不同的 `dwrow`（即 `dweight` 的不同行），**无冲突**。注意这一轮用的是**前向输入 `inp`**（作为 `const` 传入的反向缓存）。

**为什么是两轮，而不是一轮？** 源码注释说得很直白：「this backward could be done in a single "round" of loops but that doesn't afford an efficient parallelization strategy」。如果合并成一轮，多个 `(b,t)` 会同时往同一个 `dweight[o,i]` / `dbias[o]` 上累加，产生**数据竞争**（要么加锁变慢，要么结果错误）。拆成两轮、各选一个互不冲突的并行轴，就能放心地用 OpenMP 大规模并行。

#### 4.3.3 源码精读

反向实现在 [train_gpt2.c:231-269](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L231-L269)：

- 第 239 行 `#pragma omp parallel for collapse(2)`：第 1 轮，并行 `(b,t)`。
- 第 242–243 行：提前算好 `dout_bt`、`dinp_bt` 行指针（和 `dev/cpu` 那种「提行指针」风格一致）。
- 第 244–250 行：对每个 `o` 取 `d = dout_bt[o]`，再用前向权重 `wrow = weight + o*C` 把梯度散播回 `dinp_bt[i]`（`+=`）。
- 第 254 行 `#pragma omp parallel for`：第 2 轮，并行 `o`。
- 第 262 行 `if (dbias != NULL) { dbias[o] += d; }`：偏置可选——最终的 logits 投影（权重绑定 `wte`、无偏置）会传 `NULL`，见下方调用点。
- 第 263–265 行：用前向输入 `inp_bt` 累加 `dweight`（`+=`）。

真实调用点能帮你把这套抽象落到模型里。前向在 `gpt2_forward` 中，一个 Transformer 块里有 4 次 matmul，加最后 1 次 logits 投影，见 [train_gpt2.c:864-876](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L864-L876)：

- 第 864 行 `matmul_forward(l_qkv, l_ln1, l_qkvw, l_qkvb, B, T, C, 3*C);` —— QKV 投影，`OC=3C`。
- 第 866 行 attproj，`OC=C`。
- 第 869 行 fch（MLP 升维），`OC=4C`。
- 第 871 行 fcproj（MLP 降维），输入维度是 `4C`、`OC=C`（注意这里 `C` 实参传的是 `4*C`）。
- 第 876 行 `matmul_forward(acts.logits, acts.lnf, params.wte, NULL, ...)` —— 最终 logits 投影，**用 `wte` 当权重、`bias` 传 `NULL`**（这就是前置讲义提到的「权重绑定 weight tying」）。

反向在 `gpt2_backward` 中以镜像顺序调用，见 [train_gpt2.c:935](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L935)（最终 logits，`dbias=NULL`）和每层 4 次 [train_gpt2.c:994-1001](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L994-L1001)。注意反向函数签名里 `dout`、`inp`、`weight` 都是 `const`——它需要前向缓存的输入与权重，这正是「反向依赖前向激活」的体现。

#### 4.3.4 代码实践

这是一个「推导 + 对照代码」的源码阅读型实践。

1. **实践目标**：确认你理解三路梯度的公式来源，以及「反向用前向的 `weight`/`inp`」这一点。
2. **操作步骤**：
   - 对着本讲 4.3.1 的三个公式，在纸上标出每个公式的求和维度（`dinp` 沿 `o` 求和；`dweight`/`dbias` 沿 `(b,t)` 求和）。
   - 打开 [train_gpt2.c:231-269](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L231-L269)，逐行核对：第 1 轮里 `dinp_bt[i] += wrow[i] * d` 对应 `dinp` 公式（用的 `wrow` 来自前向 `weight`）；第 2 轮里 `dwrow[i] += inp_bt[i] * d` 对应 `dweight` 公式（用的 `inp_bt` 来自前向 `inp`）。
   - 找到 [train_gpt2.c:935](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L935) 这一行，确认它第 3 个参数（`dbias`）传的是 `NULL`。
3. **需要观察的现象**：三路梯度的代码实现与你的公式推导逐项吻合；最终 logits 反向不写 `dbias`。
4. **预期结果**：
   - `dinp_bt[i] += wrow[i] * d` ⟺ `dinp[b,t,i] += weight[o,i] * dout[b,t,o]`，沿 `o` 累加 ✓
   - `dwrow[i] += inp_bt[i] * d` ⟺ `dweight[o,i] += inp[b,t,i] * dout[b,t,o]`，沿 `(b,t)` 累加 ✓
   - `dbias[o] += d` ⟺ `dbias[o] += dout[b,t,o]`，沿 `(b,t)` 累加 ✓
   - 第 935 行 `dbias` 实参为 `NULL`，因为 logits 投影没有偏置项。
5. 进阶思考（可选）：为什么 `dweight` 必须用 `+=`？提示——同一个 `weight[o,i]` 被所有 `B*T` 个 `(b,t)` 位置共享，梯度要全部累加进来；同时这些梯度缓冲在每步开头由 `gpt2_zero_grad` 清零，才能保证累加正确。

#### 4.3.5 小练习与答案

**练习 1**：如果把第 1 轮和第 2 轮合并成「一轮三重循环」同时算三路梯度，会遇到什么问题？

**答案**：会出现写冲突。合并后，多个 `(b,t)` 位置会同时向同一个 `dweight[o,i]` 或 `dbias[o]` 累加（因为同一个 `o` 的权重/偏置被所有位置共享），多线程下就是数据竞争。拆成两轮、各选一个互不冲突的并行轴（第 1 轮并行 `(b,t)`、第 2 轮并行 `o`）才能安全地大规模并行。

**练习 2**：为什么 `matmul_backward` 需要把前向的 `inp` 和 `weight` 都作为 `const` 参数传进来？

**答案**：因为反向公式里 `dweight` 用到了前向输入 `inp`（`dweight[o,i] += inp[b,t,i] * dout[b,t,o]`），`dinp` 用到了前向权重 `weight`（`dinp[b,t,i] += weight[o,i] * dout[b,t,o]`）。这些前向值在反向时必须仍然可用，这正是「反向依赖前向缓存激活」的体现，也是 LayerNorm 讲义里提到的「checkpointing / 激活缓存」思想的另一个例子。

---

## 5. 综合实践

把本讲三块内容串起来：用 `dev/cpu/matmul_forward.c` 作为沙盒，亲手感受「朴素 → 分块」的优化，并补上反向的脑力推演。

1. **编译并跑通**：按 4.2.4 的命令编译运行，确认看到 `All kernels passed!` 以及两个 kernel 的耗时（**待本地验证**具体毫秒数）。
2. **理解正确性比对**：阅读 [dev/cpu/matmul_forward.c:196-216](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/dev/cpu/matmul_forward.c#L196-L216) 的 `validate_results_cpu`，注意它用的是**相对容差** `t_eff = tolerance + fabs(cpu_reference[i])`（即 `1e-5 + 参考值绝对值`），说明两版前向允许极小的浮点误差（由 FMA 与求和顺序差异引起）。
3. **改动并观察（可选）**：把 `matmul_forward_ngc92` 里的 `LOOP_UNROLL` 从 8 改成 1（等价于不分块），重新编译运行，对比耗时变化；再改成 16，观察是否更快还是因寄存器溢出而变慢。每次改动后都要确认验证仍打印 `OK`。
4. **补全反向脑力推演**：`dev/cpu/matmul_forward.c` 只测前向。请你基于 4.3 的三路梯度公式，口头描述「如果给它加一个 `matmul_backward` 的正确性测试，应该怎么组织」——具体说，参考前向的写法，先用一份朴素的 `matmul_backward_cpu` 当标尺，再验证主线 `matmul_backward` 与之一致。
5. **产出**：用一句话总结「分块版为什么快」，以及「反向为什么必须拆两轮」。

如果你无法编译，至少完成第 2、4、5 步（纯源码阅读），这也是达标的要求。

## 6. 本讲小结

- matmul 是 GPT-2 里最频繁、最耗时的算子；一个线性层就是 `out = inp @ weight^T + bias`，权重形状为 `(OC, C)`，输出 `(B,T,OC)`。
- 朴素版 `matmul_forward_naive` 是四重循环 `b→t→o→i`，沿 `i` 做内积；所有寻址都遵循「基地址 + 行号×每行元素数 + 列号」。
- 朴素版的瓶颈是访存：权重矩阵太大装不进 cache，被重复扫了 `B*T` 次。
- 分块版 `matmul_forward` 通过「一次处理 8 个 `(b,t)`、把权重读进寄存器复用 8 次」，把权重内存流量降到 \(1/8\)；不满足 `B*T % 8 == 0` 时回退到朴素版。
- `dev/cpu/matmul_forward.c` 是独立沙盒，能验证两版前向输出一致（相对容差 1e-5）并对比耗时。
- `matmul_backward` 求三路梯度 `dinp`（沿 `o` 求和，用前向 `weight`）、`dweight`（沿 `(b,t)` 求和，用前向 `inp`）、`dbias`（沿 `(b,t)` 求和）；全部 `+=` 累加，依赖 `gpt2_zero_grad` 清零。
- 反向拆成两轮循环、各选一个互不冲突的并行轴（第 1 轮并行 `(b,t)`、第 2 轮并行 `o`），才能避免写冲突并高效并行。

## 7. 下一步学习建议

- **继续本单元**：matmul 的结果会喂给注意力层。下一篇 **u2-l4 因果自注意力** 讲解 QKV 投影（本身就是一次 `OC=3C` 的 matmul）之后的多头缩放点积、因果 mask 与 softmax，建议接着读。
- **回头看**：如果你想再体会一次「同一算子的朴素版 vs 优化版」，可以提前浏览 **u7-l1 dev/cuda 内核库**，那里展示了 CUDA 版 matmul 从朴素到 cuBLASLt 的多版本演进（本讲的 cache blocking 思想在 GPU 上会以 tiling 形式再次出现）。
- **源码延伸**：等进入 **u5 CUDA 主线** 时，重点关注 `llmc/matmul.cuh` 如何用 cuBLASLt 取代手写三重循环——那本质上是把本讲的「自己分块」交给高度优化的库去做。
