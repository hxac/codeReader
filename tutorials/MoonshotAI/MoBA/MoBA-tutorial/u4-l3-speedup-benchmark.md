# 性能测试：MoBA 相对 flash-attn 的加速比测量

## 1. 本讲目标

学完本讲，你应该能够：

1. 掌握 GPU 基准测试的正确姿势：为什么必须预热（warmup）、为什么必须在计时前后调用 `torch.cuda.synchronize()`、为什么用 `time.perf_counter` 而不是 `time.time`。
2. 读懂 `tests/test_moba_speedup.py`，理解它测的是「前向 + 反向」整体耗时，以及 flash-attn 全量因果注意力基线是如何设置的。
3. 从计算量角度推导 MoBA 理论加速上限，理解 `topk`、`chunk_size`、`seqlen` 三个参数如何共同决定稀疏度，并判断哪些配置下 MoBA 反而更慢。
4. 能够独立设计并运行一个多配置对比实验，把结果整理成加速比表格。

本讲是前面所有源码精读（u3 系列）的「验收环节」：读懂了算法，现在用数字证明它值得。

## 2. 前置知识

### 2.1 GPU 异步执行：为什么「看起来很快」是假的

PyTorch 调用 CUDA 算子时，Python 端只做一件事——把算子丢进 GPU 的执行队列（kernel launch），然后**立即返回**，并不会等 GPU 真正算完。这叫异步执行。如果你在 launch 后立刻读 `time.perf_counter()`，测到的只是「提交任务」的时间（微秒级），而不是「完成任务」的时间。

`torch.cuda.synchronize()` 的作用是阻塞 Python 线程，直到 GPU 队列里所有任务全部完成。因此正确的计时模板是：

```python
torch.cuda.synchronize()          # 确保计时前 GPU 是空闲的
start = time.perf_counter()       # 开始计时
for ...:                          # 被测代码
    ...
torch.cuda.synchronize()          # 等待 GPU 真正算完
elapsed = time.perf_counter() - start
```

两个 `synchronize` 缺一不可：前者防止把上一段代码的尾巴算进来，后者防止 GPU 还没干完就停表。

### 2.2 为什么用 time.perf_counter

`time.perf_counter()` 是单调递增的高精度时钟，专门用于测量时间段，不受系统时间被 NTP 校准等影响；而 `time.time()` 是墙钟时间，精度低且可能回跳。测耗时永远用前者。

### 2.3 为什么需要 warmup

第一次运行一段 CUDA 代码时，额外开销很多：CUDA context 与 kernel 模块加载、PyTorch 缓存分配器首次 `cudaMalloc`、GPU 频率从低功耗状态爬升（boost）。这些一次性成本若混进计时，结果会严重偏大。做法是：正式计时前先把同样的计算跑几遍（warmup 迭代），让所有缓存、频率进入稳态。

### 2.4 加速比与稀疏度

- **加速比（speedup）**：\( \text{speedup} = T_{\text{基线}} / T_{\text{新方法}} \)。大于 1 表示更快，40x 表示快 40 倍。
- **稀疏度**：MoBA 下每个 query 实际参与注意力计算的 KV 数约为 `topk × chunk_size`（当前块走 self 支路 + `topk-1` 个历史块走 MoBA 支路），而全量因果注意力平均每个 query 要看 \( S/2 \) 个 key。两者之比就是计算量占比。

### 2.5 基线是谁：三个容易混淆的「加速比」

读 MoBA 的性能数字前必须先问「相对谁」：

| 加速比说法 | 基线 | 出处 |
| --- | --- | --- |
| 「up to 40x speedup」 | `moba_naive`（教学实现） | README |
| `test_moba_speedup.py` 打印的 `Speedup` | flash-attn 全量因果注意力 | 本讲主角 |
| 「MoBA 需要继续训练才能获得加速」 | （使用前提，非性能数字） | README 注意事项 |

README 中 40x 的原始表述在 Implementation Details 一节，明确写的是 **compared to moba_naive**：

> **moba_efficient**: Our production-ready implementation optimized for performance. It achieves up to 40x speedup compared to moba_naive (tested with 32K sequence length, 1 attention head, MoBA Block 2048 and MoBA Topk 3).

而 `test_moba_speedup.py` 测的是 MoBA 相对 **flash-attn** 的加速——也就是「块稀疏带来的真实收益」。naive 实现要 materialize \( S \times S \) 的大矩阵、在 PyTorch 逐算子层面做掩码 softmax，本身就极慢，拿它当基线得到的 40x 里大部分是「naive 太慢」贡献的。两个数字都有意义，但含义完全不同，混用就会误读。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [tests/test_moba_speedup.py](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/tests/test_moba_speedup.py) | 本讲主角：完整的 GPU 基准测试脚本，含数据构造、warmup、计时、加速比打印 |
| [README.md](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/README.md) | 40x 加速声明的出处与测试条件（32K、单头、block 2048、topk 3） |
| [moba/moba_efficient.py](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_efficient.py) | 被测对象 `moba_attn_varlen`；本讲从「开销」视角重新审视它的 gate 计算与索引重排（u3 系列已逐行精读过） |
| [tests/test_moba_attn.py](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/tests/test_moba_attn.py) | 正确性测试（u4-l2 已讲），其 `generate_data` 与本讲的版本几乎相同 |

## 4. 核心概念与源码讲解

### 4.1 warmup 迭代：让 GPU 进入稳态

#### 4.1.1 概念说明

基准测试的第一条军规：**被测代码必须先「跑热」再计时**。本脚本为 flash 基线和 MoBA 各设了独立的一轮 warmup，因为两者的 kernel、中间张量形状、显存分配模式完全不同——flash 跑热了不代表 MoBA 的 `einsum`、`index_select`、`nonzero` 等 kernel 也完成了首次加载与显存池预热。

#### 4.1.2 核心流程

```
对每个候选（flash 基线、MoBA）：
  1. 跑 warmup_iters 次完整的前向 + 反向（不计时）
  2. torch.cuda.synchronize()（清空队列，作为计时起点屏障）
  3. start = time.perf_counter()
  4. 跑 perf_test_iters 次完整的前向 + 反向
  5. torch.cuda.synchronize()（等 GPU 真正干完）
  6. elapsed = (perff_counter() - start) / perf_test_iters
```

注意 warmup 跑的不是「半个流程」而是**完整的前向 + 反向**，与正式计时循环里的工作完全一致。

#### 4.1.3 源码精读

迭代次数的定义：[tests/test_moba_speedup.py:L44-L46](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/tests/test_moba_speedup.py#L44-L46)

```python
    # Warmup
    warmup_iters = 3
    perf_test_iters = 10
```

3 次 warmup、10 次正式迭代，是「快速冒烟测试」级别的配置；严肃的学术 benchmark 通常会用几十次迭代并多次重复取中位数（见 4.4 节的讨论）。

flash 基线的 warmup 循环：[tests/test_moba_speedup.py:L49-L51](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/tests/test_moba_speedup.py#L49-L51)

```python
    for _ in range(warmup_iters):
        o = flash_attn_varlen_func(q, k, v, cu_seqlen, cu_seqlen, max_seqlen, max_seqlen, causal=True)
        torch.autograd.backward(o, vo_grad)
```

被测对象是「前向 + 反向」整体：先做一次 causal 的 flash-attn varlen 前向，再对输出 `o` 用固定上游梯度 `vo_grad` 反传。选前向 + 反向而不是只测前向，是因为训练场景下反向的计算量与前向同量级，只测前向会高估收益。

MoBA 一侧的 warmup：[tests/test_moba_speedup.py:L64-L66](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/tests/test_moba_speedup.py#L64-L66)

```python
    for _ in range(warmup_iters):
        om = moba_attn_varlen(q, k, v, cu_seqlen, max_seqlen, moba_chunk_size=moba_chunk_size, moba_topk=moba_topk)
        torch.autograd.backward(om, vo_grad)
```

两次 warmup 调用的参数（同一个 `q/k/v/cu_seqlen`、同一组 `moba_chunk_size/moba_topk`）与正式计时完全一致，保证 CUDA 内存池里留下的缓存块尺寸正好是后面要复用的尺寸。

一个值得注意的细节：循环里既没有 `zero_grad()` 也没有 `o.detach()`，10 次迭代的梯度会**累积**到 `q.grad/k.grad/v.grad` 里。对本测试这不构成计时污染（梯度累加只是一次逐元素 add，开销相对注意力可忽略，且计算量恒定），但如果你要写更严格的 benchmark，可以在每次迭代末尾 `q.grad = None` 并留意该操作本身的开销。

#### 4.1.4 代码实践

1. **实践目标**：亲眼看到「不 warmup 会让第一轮慢多少」。
2. **操作步骤**：把 `warmup_iters` 临时改成 0，在计时循环里打印每次迭代的单独耗时（在循环体内加一个 `torch.cuda.synchronize()` + `time.perf_counter()`，此为示例代码，请勿修改仓库文件，复制到独立脚本里做）：

   ```python
   # 示例代码：观察首轮惩罚
   for i in range(perf_test_iters):
       o = flash_attn_varlen_func(q, k, v, cu_seqlen, cu_seqlen, max_seqlen, max_seqlen, causal=True)
       torch.autograd.backward(o, vo_grad)
       torch.cuda.synchronize()
       print(f"iter {i}: {time.perf_counter()}")
   ```

3. **需要观察的现象**：第 0 次迭代与后续迭代的间隔明显大于后续迭代之间的间隔。
4. **预期结果**：首尾时间戳差值呈现「第一次大、后面小」的形态；具体倍数因 GPU 型号而异，待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：如果只把 flash 基线 warmup 了、MoBA 没 warmup，测出的加速比会偏大还是偏小？

**答案**：偏大。MoBA 首轮的一次性开销（kernel 加载、显存分配）会被算进 MoBA 的计时，使 `time_moba` 偏大，`time_flash / time_moba` 偏大——即在真实加速比之上虚增。反向情形（只 warmup MoBA）则会让加速比偏小。

**练习 2**：warmup 循环里为什么必须包含 `torch.autograd.backward`，只跑前向行不行？

**答案**：不行。反向传播会触发一组完全不同的 kernel（flash-attn 反向、梯度累加），并为中间计算图分配显存。若 warmup 只跑前向，正式计时的第一次反向仍要付首轮惩罚。

### 4.2 cuda synchronize 计时：正确的时间测量

#### 4.2.1 概念说明

本模块拆解脚本中真正「按秒表」的六行代码。核心是 2.1 节的异步执行模型：launch 是异步的，`perf_counter` 在 CPU 上走表，所以必须在停表前把 GPU 队列排干。此外，起点前的 `synchronize` 同样重要——它保证计时开始时 GPU 队列为空，上一段（warmup）的尾巴不会混入测量窗口。

#### 4.2.2 核心流程

```
flash 计时（MoBA 计时结构完全对称）：
  synchronize → perf_counter → N 次前向+反向 → synchronize → perf_counter
  time_flash = 总耗时 / N × 1000   （换算成毫秒/次）
  speedup = time_flash / time_moba
```

#### 4.2.3 源码精读

flash 基线的计时窗口：[tests/test_moba_speedup.py:L53-L60](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/tests/test_moba_speedup.py#L53-L60)

```python
    torch.cuda.synchronize()
    start_flash = time.perf_counter()
    for _ in range(perf_test_iters):
        o = flash_attn_varlen_func(q, k, v, cu_seqlen, cu_seqlen, max_seqlen, max_seqlen, causal=True)
        torch.autograd.backward(o, vo_grad)
        
    torch.cuda.synchronize()
    time_flash = (time.perf_counter() - start_flash) / perf_test_iters * 1000
```

第 53 行的 `synchronize` 是起点屏障；第 59 行的是终点屏障；第 60 行除以迭代次数并乘 1000，得到「每次（前向+反向）平均毫秒数」。

MoBA 一侧对称的计时窗口：[tests/test_moba_speedup.py:L69-L76](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/tests/test_moba_speedup.py#L69-L76)

```python
    torch.cuda.synchronize()
    start_moba = time.perf_counter()
    for _ in range(perf_test_iters):
        om = moba_attn_varlen(q, k, v, cu_seqlen, max_seqlen, moba_chunk_size=moba_chunk_size, moba_topk=moba_topk)
        torch.autograd.backward(om, vo_grad)
    
    torch.cuda.synchronize()
    time_moba = (time.perf_counter() - start_moba) / perf_test_iters * 1000
```

除了被测函数不同，两个计时块逐行同构——这正是公平对比的前提：测量方法本身的任何系统偏差（同步方式、时钟、迭代数）对两边同等作用。

有个隐含的公平性要点：MoBA 的计时循环里，CPU 端 Python 开销（gate、索引变换的几十行 glue code）与 GPU 计算是**流水线并行**的——只要 CPU 能赶在 GPU 之前把 kernel 提交完，wall time 就由 GPU 决定；反之若 CPU 跟不上（launch bound），wall time 由 CPU 决定。对 GPU-bound 的长序列场景前者成立，这也是脚本选择在 GPU 上测大序列的原因。

#### 4.2.4 代码实践

1. **实践目标**：量化「忘记 synchronize」造成的假象。
2. **操作步骤**：复制脚本为独立文件，注释掉第 59 行的 `torch.cuda.synchronize()`（只保留起点那个），运行并比较打印的 `time_flash`。
3. **需要观察的现象**：注释后 `time_flash` 变得极小（可能只有几毫秒甚至更低）。
4. **预期结果**：因为停表时 GPU 队列里还压着没算完的任务，测到的接近「10 次 kernel launch 的 CPU 时间」而非「10 次计算的 GPU 时间」。具体数值待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：能否用 CUDA Event（`torch.cuda.Event(enable_timing=True)`）替代 `synchronize + perf_counter`？各有什么优劣？

**答案**：可以。在计时窗口前后各 `record()` 一个 event，结束时 `end.synchronize()` 后用 `start.elapsed_time(end)`。CUDA event 测的是 GPU 时间线上的间隔，天然排除 CPU 端 launch 之前的空闲；而 `synchronize + perf_counter` 测的是包含 CPU 提交开销的 wall time。本脚本的场景（前向+反向整体吞吐）两种方法都常用；当 CPU 端 Python glue 较重、想单独看 GPU 纯计算时，event 更精确。

**练习 2**：起点前若不加 `synchronize`（第 53 行），哪一段计算可能被错误计入？

**答案**：MoBA warmup 最后一轮反向的尾部 kernel（或 CPU 端尚未返回的调用）。起点时刻 GPU 队列非空，这些残留工作会在计时窗口内被算进 flash 基线的耗时，使基线偏慢、加速比虚高。

### 4.3 flash-attn 基线与被测对象的设置

#### 4.3.1 概念说明

一个加速比数字由「分子是谁、分母是谁、输入是什么」三者决定。本模块看后两者：基线用 `flash_attn_varlen_func`（causal、同一份 varlen 输入），被测对象是 `moba_attn_varlen`。数据由 `generate_data` 构造——与正确性测试 `test_moba_attn.py` 里的版本几乎相同，保证「测得快的那个」和「测得对的那个」吃的是同一种数据。

#### 4.3.2 核心流程

```
generate_data：
  固定 random/torch/cuda 三处种子
  → q/k/v: [seqlen, head, head_dim]，requires_grad=True
  → cu_seqlen: random.sample 抽 batch-1 个切点（不放回、无重复）→ 排序 → 补 0 与 seqlen → int32
  → max_seqlen = max(相邻差)
```

#### 4.3.3 源码精读

数据构造入口：[tests/test_moba_speedup.py:L38-L42](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/tests/test_moba_speedup.py#L38-L42)

```python
def test_attn_varlen_moba_speed(batch, head, seqlen, head_dim, moba_chunk_size, moba_topk, dtype=torch.bfloat16):
    """Speed test comparing v3 vs v4 moba attention"""
    # Get data
    q, k, v, cu_seqlen, max_seqlen = generate_data(batch, seqlen, head, head, head_dim, dtype)
    vo_grad = torch.randn_like(q)
```

注意 `generate_data` 的第 3、4 个参数都传了 `head`——即 Q 头数等于 KV 头数（非 GQA），因为 MoBA 内核要求两侧头数一致（u4-l1 讲过 wrapper 里才做 GQA 复制）。默认 dtype 是 `bfloat16`，与真实训练一致。`vo_grad` 是反传用的固定上游梯度，`randn_like(q)` 保证每次反传的梯度计算量恒定。

varlen 批次的构造：[tests/test_moba_speedup.py:L26-L32](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/tests/test_moba_speedup.py#L26-L32)

```python
    # gen cu seqlen
    cu_seqlen = random.sample(range(1, seqlen - 1), batch - 1) if batch > 1 else []
    cu_seqlen.sort()
    cu_seqlen = [0] + cu_seqlen + [seqlen]
    cu_seqlen = torch.tensor(cu_seqlen, device=device, dtype=torch.int32)

    # max_seqlen
    max_seqlen = torch.amax(cu_seqlen[1:] - cu_seqlen[:-1])
```

`random.sample` 不放回抽样保证切点互不相同，因此不会出现空序列；排序、补端点后转成 flash-attn 约定的 int32 前缀和。`max_seqlen` 取相邻差的最大值，仅作内核启动参数。这段与 `test_moba_attn.py` 的 `generate_data` 同源（u4-l2 讲过），种子也固定为 0，所以性能数字是**可复现的**——同一台机器上重复运行，输入完全一致。

脚本入口的默认配置：[tests/test_moba_speedup.py:L83-L85](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/tests/test_moba_speedup.py#L83-L85)

```python
if __name__ == "__main__":
    test_attn_varlen_moba_speed(batch=1, head=1, seqlen=32768, head_dim=128, moba_chunk_size=512, moba_topk=3)
    print("simple speed test finished")
```

batch=1、head=1、32K 序列、head_dim=128、chunk 512、topk 3。与 README 40x 的条件（chunk 2048）不同——chunk 512 会切成 64 个块，稀疏度更高（见 4.4 节推导）。单头设置使 gate 与索引 glue 的开销最小，突出注意力本体计算的差异。

#### 4.3.4 代码实践

1. **实践目标**：确认默认配置的输入形状与块结构。
2. **操作步骤**：在 GPU 环境运行 `python tests/test_moba_speedup.py`（依赖按 README 安装：`conda create -n moba python=3.10 && pip install .`，flash-attn==2.6.3）。
3. **需要观察的现象**：终端打印 `batch:1 head:1 seqlen:32768 chunk:512 topk:3`、Flash 与 MoBA 的毫秒数、Speedup。
4. **预期结果**：Speedup 大于 1，量级在个位数到十倍之间（取决于 GPU 型号），待本地验证。无 GPU 环境时跳过运行，进入 4.4 的理论推导与 5 的综合实践分析部分。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `vo_grad` 用 `randn_like(q)` 而不是全 1？

**答案**：全 1 的上游梯度数学上合法但数值上病态——反向中大量符号相同的梯度相加，某些 kernel 路径的耗时可能与真实分布不同（且无法检验数值正确性）。随机的 `vo_grad` 模拟真实训练里的梯度分布，让反向耗时更具代表性。对本脚本而言两者计算量相同，主要是一致性/习惯问题。

**练习 2**：把 `batch` 从 1 改成 8，其他不变，flash 基线的总计算量会明显变化吗？

**答案**：几乎不变。varlen 打包下总 token 数仍是 `seqlen`（`cu_seqlen` 终点固定为 seqlen，切分成 8 段），因果注意力的计算量取决于各段长度的平方和——切分后每段更短，平方和反而**下降**（\( \sum s_i^2 < S^2 \)），所以基线可能略快。这是 varlen 基准的一个注意点：改变 batch 实际改变了序列长度分布。

### 4.4 加速比统计：从计算量模型到「什么时候 MoBA 反而慢」

#### 4.4.1 概念说明

加速比不是黑盒数字，它可以先算出来。MoBA 的收益来自稀疏化（省掉 \( O(S^2) \) 的主项），成本来自 gate 打分、索引重组与 LSE 合并（都是 \( O(S) \) 级的附加项）。当稀疏化省下的量盖不住附加项，MoBA 就会变慢。本模块建立这个量化模型。

#### 4.4.2 核心流程与数学模型

全量因果注意力：第 \( s \) 个 query 要看 \( s+1 \) 个 key，平均约 \( S/2 \) 个；每个 query-key 交互在 score 与 PV 两步各花 \( O(D) \)，再加上反向约 2 倍，前后向总量级为：

\[ F_{\text{full}} \sim c \cdot S \cdot \frac{S}{2} \cdot D \]

MoBA：每个 query 参与的 KV 数约为 \( \text{topk} \times \text{chunk\_size} \)（1 个当前块走 self 支路 + topk−1 个历史块走 moba 支路），注意力本体计算量为：

\[ F_{\text{moba-attn}} \sim c \cdot S \cdot (\text{topk} \times \text{chunk\_size}) \cdot D \]

于是注意力本体的**理论加速上限**（忽略 gate 等附加开销）：

\[ \text{speedup}_{\max} \approx \frac{S/2}{\text{topk} \times \text{chunk\_size}} \]

用脚本默认配置（\( S \)=32768，chunk=512，topk=3）验算：

\[ \text{speedup}_{\max} \approx \frac{16384}{3 \times 512} = \frac{16384}{1536} \approx 10.7\text{x} \]

用 README 40x 的条件（chunk=2048，topk=3）验算：

\[ \text{speedup}_{\max} \approx \frac{16384}{6144} \approx 2.7\text{x} \]

后者正是 README 那个 40x 的「清醒剂」：**相对 flash-attn 的理论收益只有约 2.7 倍**，40x 是相对极慢的 naive 实现测的（naive 要 materialize \( S \times S \) 矩阵）。两个数字不矛盾，因为分母不同。

再减去 MoBA 的附加成本，实际加速比 = 注意力节省 − glue 开销：

| 附加项 | 位置 | 量级 |
| --- | --- | --- |
| gate 打分 `einsum("nhd,shd->nhs")`（fp32） | [moba/moba_efficient.py:L345-L347](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_efficient.py#L345-L347) | \( O(N_{\text{chunk}} \cdot H \cdot S \cdot D) \)，线性于 S 但常数不小，且是 memory-bound 的 fp32 运算 |
| 候选块 gather（`filtered_kv`） | [moba/moba_efficient.py:L326-L330](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_efficient.py#L326-L330) | 几乎搬运一遍全部 KV（\( O(S \cdot H \cdot D) \) 显存流量） |
| `kv = torch.stack((k, v), dim=1)` | [moba/moba_efficient.py:L299](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_efficient.py#L299) | 额外一次全量 K、V 拷贝 |
| `nonzero`/`index_select`/`scatter_` 索引重排 | [moba/moba_efficient.py:L365-L386](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_efficient.py#L365-L386) | memory-bound，大小正比于选中记录数 |
| LSE 合并（`index_reduce`/`index_add_` 等） | [moba/moba_efficient.py:L130-L168](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_efficient.py#L130-L168) | 正比于 moba 支路记录数，fp32 |

由此可以预测** MoBA 变慢/收益消失的配置**：

1. **topk 拉满**（topk ≥ 块数）：MoBA 支路覆盖全部历史块，注意力计算量回到 \( O(S^2) \)，还白付 gate 与重排开销，必然慢于 flash 基线。
2. **topk = 1 或序列短于一个块**：[moba/moba_efficient.py:L314-L321](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_efficient.py#L314-L321) 中 `need_moba_attn` 为假，直接 `return flash_attn_varlen_func(...)`——与基线跑的是同一个函数，加速比恰好 ≈ 1（甚至因前置的 `calc_chunks`、`torch.stack` 略慢）。
3. **chunk_size 很小**：块数 \( N_{\text{chunk}} = S / \text{chunk} \) 暴涨，gate 矩阵 [N_chunk, H, S]、`filtered_kv` 搬运、`nonzero` 清单都随之变大；同时小块让 flash-attn 的分块并行效率下降。
4. **短序列**：\( S \) 小则 \( O(S^2) \) 主项本来就小，线性附加项占比高。MoBA 是长序列技术，S 上万才有意义。

反向还有一处常被忽略的附加成本：MoBA 的 moba 支路要按 `moba_q_sh_indices` 地址簿对 `dout/out/lse` 做三次 `index_select` 按记录复制（[moba/moba_efficient.py:L232-L241](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_efficient.py#L232-L241)），以及 `dmkv = torch.stack(...)` 的打包（[moba/moba_efficient.py:L266](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_efficient.py#L266)）。

#### 4.4.3 源码精读（结果输出）

加速比的最终计算与打印：[tests/test_moba_speedup.py:L78-L80](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/tests/test_moba_speedup.py#L78-L80)

```python
    print(f"\nbatch:{batch} head:{head} seqlen:{seqlen} chunk:{moba_chunk_size} topk:{moba_topk}")
    print(f"Flash: {time_flash:.2f}ms, MoBA: {time_moba:.2f}ms")
    print(f"Speedup:  {time_flash / time_moba:.2f}x")
```

第一行打印全部配置（自己给自己的数字附上测试条件，这是好习惯），第三行就是 `speedup = time_flash / time_moba`。注意脚本**没有**做多次重复实验的统计（只跑一轮 10 次取平均，没有中位数/标准差/置信区间），也没有报告方差——读到 `3.42x` 这样的数字时应意识到 ±10% 的抖动是正常的。改进方法见综合实践。

#### 4.4.4 代码实践

1. **实践目标**：验证理论模型能预测实测加速比的趋势。
2. **操作步骤**（有 GPU）：
   - 复制 `tests/test_moba_speedup.py` 为独立脚本（不要改动仓库文件）；
   - 把 `__main__` 改成网格循环：`seqlen ∈ {8192, 32768}`、`moba_topk ∈ {2, 3, 8}`、`moba_chunk_size` 固定 2048，共 6 组调用；
   - 每组先用 4.4.2 的公式手算 `speedup_max ≈ (S/2) / (topk × chunk)` 填入「预测」列，再运行填「实测」列：

   | seqlen | topk | chunk | 预测上限 (x) | 实测 (x) | 偏差 |
   | --- | --- | --- | --- | --- | --- |
   | 8192 | 2 | 2048 | 2.0 | 待本地验证 | |
   | 8192 | 3 | 2048 | 1.33 | 待本地验证 | |
   | 8192 | 8 | 2048 | 0.5（变慢） | 待本地验证 | |
   | 32768 | 2 | 2048 | 4.0 | 待本地验证 | |
   | 32768 | 3 | 2048 | 2.67 | 待本地验证 | |
   | 32768 | 8 | 2048 | 1.0（持平） | 待本地验证 | |

3. **需要观察的现象**：实测值普遍低于预测上限（glue 开销所致）；topk=8 且 seqlen=8192 时（块数 = 4，topk=8 ≥ 块数−1），加速比跌破 1。
4. **预期结果**：预测列与实测列同向变化，且 S 越大、topk 越小，实测越接近上限。

#### 4.4.5 小练习与答案

**练习 1**：为什么实测加速比总是低于理论上限 \( (S/2)/(\text{topk} \times \text{chunk}) \)？

**答案**：三个原因。① gate 计算、索引重排、LSE 合并等附加开销没有被模型计入；② 当前块走的 self 支路与历史块走的 moba 支路是**两次**独立 kernel launch，GPU 利用率低于一次大 kernel；③ moba 支路里每个 query 严格算 topk×chunk 个 KV，而全量基线因因果性平均只算 \( S/2 \)，模型里分子已经按平均近似，但 kernel 内部的分块调度还有额外损耗。

**练习 2**：固定 `seqlen=32768`、`topk=3`，把 `chunk_size` 从 2048 减到 128，理论加速上限如何变？实际会如预期吗？

**答案**：上限从 \( 16384/6144 \approx 2.67\text{x} \) 升到 \( 16384/384 \approx 42.7\text{x} \)。但实际不会接近 42x：chunk=128 时块数达 256，gate 矩阵 [256, H, 32768] 的 fp32 einsum、`filtered_kv` 搬运、`nonzero` 清单都急剧膨胀，且 128 长度的 KV 块使 flash-attn 内部并行度变差。理论收益大不等于实测收益大——这正是需要真实 benchmark 的理由。

**练习 3**：`test_attn_varlen_moba_speed` 若把 `perf_test_iters` 从 10 改成 1，结果可信吗？

**答案**：大幅降低。单次迭代受偶发扰动（其他进程抢占 GPU、时钟波动）影响大；10 次取平均已能抹平大部分抖动，但仍建议多次重复取中位数。迭代次数越多单次噪声影响越小，但总运行时间变长。

## 5. 综合实践：一份完整的 MoBA 性能报告

把本讲全部知识串起来，产出一份小报告（`mobareport.md`，放在你自己的工作目录，不要放进仓库）：

**任务**：回答「在我的硬件上，MoBA 什么时候值得用？」

1. **基线复现**：运行未修改的 `python tests/test_moba_speedup.py`，记下默认配置（32K/单头/chunk 512/topk 3）的 Flash、MoBA 毫秒数与加速比。
2. **网格实验**：按 4.4.4 的表格跑 `seqlen ∈ {8192, 32768}` × `topk ∈ {2, 3, 8}`（chunk 固定 2048），每组重复 3 轮取中位数，填入表格。
3. **边界探测**：补两组极端配置——`topk=1`（应观察到加速比 ≈ 1，对应退化分支）；`topk=16`（超过块数，应观察到加速比 < 1）。用 4.4.2 的公式解释每组数字。
4. **可信度增强**：给脚本加上「每组随机换一个 torch 种子再测一遍」的逻辑，观察同一配置的两次结果差多少，写进报告的「误差分析」一节。
5. **无 GPU 替代方案**：若没有 GPU，完成纯分析版报告——（a）推导每个配置的理论上限；（b）在 [moba/moba_efficient.py](https://github.com/MoonshotAI/MoBA/blob/b5d58363311d3ca946f1ec444182727c15e338b5/moba/moba_efficient.py) 里逐段标注哪些行是「随 topk×chunk 增长的注意力本体」、哪些是「随 S×N_chunk 增长的 glue 开销」；（c）据此论证哪三个配置最可能让 MoBA 变慢，并给出理由。

**验收标准**：报告里每个实测数字都附测试条件；「变慢」的配置都有计算量层面的解释；能指出 README 40x 与你实测数字的差异来源（基线不同：naive vs flash-attn）。

## 6. 本讲小结

- GPU 计时的铁律是「warmup + 双端 `torch.cuda.synchronize()` + `time.perf_counter`」：launch 异步、执行异步，不同步就是测了个寂寞；起点前的同步防止上一段的尾巴混入。
- 本脚本测的是**前向 + 反向**整体吞吐，flash 基线与 MoBA 用同一份固定种子构造的 varlen 数据、逐行对称的计时代码，保证公平且可复现。
- 理论加速上限 \( \approx (S/2)/(\text{topk} \times \text{chunk\_size}) \)：默认配置（32K/chunk 512/topk 3）上限约 10.7x；README 的 40x 是相对 naive 实现且条件为 chunk 2048（理论对 flash-attn 仅约 2.7x）——读性能数字先问「相对谁、什么条件」。
- 实测加速比恒低于上限，差额来自 gate 打分、`filtered_kv`/`kv.stack` 搬运、`nonzero`/`index_select` 索引重排、LSE 合并等 \( O(S) \) 级 memory-bound 附加项。
- topk 拉满、topk=1（退化分支）、chunk 过小、序列过短这四类配置下，MoBA 收益消失甚至变慢；单次 10 迭代平均无方差统计，严肃结论需要多次重复取中位数。

## 7. 下一步学习建议

- 下一讲 u4-l4「设计取舍与二次开发」：把本讲的量化工具用到极致——实现一个自己的稀疏模式变体（如强制选中第一个块），用本讲的计时方法测量「新稀疏度」对应的性能变化，完成从读懂到改造的闭环。
- 延伸阅读：flash-attn 官方仓库的 benchmark 脚本（`flash_attn/utils/benchmark.py` 一类）对比本脚本，学习 CUDA event 计时与自动参数扫描的写法；论文 [MoBA: Mixture of Block Attention](https://arxiv.org/abs/2502.13189)（仓库内附 PDF）中的 computation-time 实验图对应 README `figures/computation_time.png`，可与你的网格实验结果相互印证。
- 若对计时精度有进一步兴趣，可了解 `torch.profiler`（profile 级别的 kernel 耗时分解，能直接看到 gate einsum 与 flash kernel 各占多少毫秒），把本讲的「附加开销」从推测变成测量。
