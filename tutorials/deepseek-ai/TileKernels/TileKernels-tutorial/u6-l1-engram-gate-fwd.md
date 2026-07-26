# Engram 门控前向 kernel 与持久化调度

## 1. 本讲目标

本讲深入 `engram_gate_kernel.py` 的**前向 kernel**，解决三件事：

1. **讲清 engram 门控的前向数学**：RMSNorm + signed-sqrt 点积 + sigmoid 门控到底在算什么。
2. **讲清 persistent kernel（持久化 kernel）模式**：为什么网格大小绑定硬件 SM 数而不是 token 数，每个持久化块如何循环覆盖一串 token。
3. **讲清 SM 占用启发式**：`_choose_blk_d` 与 `_choose_num_persistent_blocks` 如何根据共享内存预算估算「每个 SM 能塞几块、总共该启动几块」。

学完后，你应能：手算给定 `hidden_size` 下的 `blocks_per_sm` 与 `num_persistent_blocks`，并说明持久化块如何不重不漏地覆盖全部 token。反向 kernel 与权重梯度归约留到下一讲（u6-l2）。

## 2. 前置知识

本讲默认你已掌握 u2-l2（GPU 三级存储 `global → shared → register`、`T.alloc_shared`/`T.alloc_local`、`T.copy` 与 `disable_tma`）和 u2-l3（`T.Serial`/`T.vectorized`、`T.ceildiv`、warp 级规约）。在此基础上补充三个概念：

- **RMSNorm（Root Mean Square Normalization）**：与 LayerNorm 不同，RMSNorm 不减均值，只用均方根做缩放。对向量 \(x\in\mathbb{R}^{H}\)：
  \[ \text{rstd}(x) = \frac{1}{\sqrt{\frac{1}{H}\sum_{d} x_d^2 + \varepsilon}}, \qquad \hat{x}_d = x_d \cdot \text{rstd}(x) \]
  \(\hat{x}\) 的均方根约为 1。`rstd` = reciprocal std（标准差的倒数），是 kernel 里反复出现的量。

- **signed-sqrt（带符号开方）**：对实数 \(a\)，定义 \(\text{signed\_sqrt}(a)=\text{sign}(a)\cdot\sqrt{|a|}\)。它把数值「压向 ±1」：\(a>1\) 时缩小，\(0<a<1\) 时放大，并保留符号。数学上即 \(a^{1/2}\) 在实数域的保号延拓。

- **persistent kernel（持久化 kernel）**：常规 CUDA 写法是「一个问题块启动一个 thread block」，token 一多网格就爆炸、启动开销大、小批量时 SM 空转。持久化做法反过来：**只启动硬件「恰好能同时驻留」数量的块**，每块在一个 `Serial` 循环里连续处理多个 token，块「常驻」SM 不被反复启动。好处是省启动开销、跨 token 复用常驻数据（如权重）、网格大小随硬件自适应而非随问题规模。

- **hc_mult**：engram 把隐藏态沿一个「超连接倍数」维度展开，输入形状是 `(num_tokens, hc_mult, hidden_size)`，本项目固定 `hc_mult=4`。每个 token 有 4 个并行的「头」要分别算门控分。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [tile_kernels/engram/engram_gate_kernel.py](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/engram/engram_gate_kernel.py) | 前向 + 反向 kernel 与 wrapper。本讲只读前向 kernel（`get_engram_gate_fwd_kernel` 与 `engram_gate_fwd`） |
| [tile_kernels/config.py](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/config.py) | 硬件探测：`get_num_sms` / `get_max_smem_per_sm`，占用启发式依赖它 |
| [tile_kernels/torch/engram.py](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/torch/engram.py) | 纯 PyTorch 参考实现 `engram_gate_ref`，是理解数学与对拍验证的权威 |
| [tests/engram/test_engram_gate_fwd.py](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tests/engram/test_engram_gate_fwd.py) | 前向正确性对拍与 benchmark，实践时用来验证理解 |

## 4. 核心概念与源码讲解

### 4.1 Engram 门控的前向数学：RMSNorm + signed_sqrt 点积 + sigmoid 门控

#### 4.1.1 概念说明

Engram 门控是一种「带门控的残差融合」：给定隐藏态 \(x\)、键嵌入 \(k\)、值嵌入 \(v\)，输出是把 \(v\) 按「\(x\) 与 \(k\) 的相似度」缩放后加回 \(x\)。整套前向可以写成一行（见 modeling 层文档串）：

\[
\text{out} = x + \sigma\bigl(\text{signed\_sqrt}(\text{dot}(\widehat{x\cdot w_h},\; \widehat{k\cdot w_e}) \cdot \text{scalar})\bigr)\cdot v
\]

拆开看，分四步：

1. **分别 RMSNorm** \(x\) 与 \(k\)，得 \(\text{rstd}_x\)、\(\text{rstd}_k\)。注意权重 \(w_h\)、\(w_e\) 是逐元素乘进去的，且 kernel 收到的不是两个权重，而是它们的**融合权重** \(\text{weight\_fused}=w_h\cdot w_e\)（融合 kernel 见 u6-l3），省一次访存。
2. **点积**：\(\text{raw\_dot}=\sum_d x_d\cdot k_d\cdot \text{weight\_fused}_d\)，再归一化 \(\text{dot}=\text{raw\_dot}\cdot\text{rstd}_x\cdot\text{rstd}_k\cdot\text{scalar}\)，其中 \(\text{scalar}=H^{-1/2}\)。
3. **门控激活**：\(\text{gate}=\sigma(\text{signed\_sqrt}(\text{clamp}(|\text{dot}|,c)))\)，\(c\) 是 `clamp_value`（防 \(|\text{dot}|\to 0\) 时开方梯度爆炸）。
4. **残差输出**：\(\text{out}_d=x_d+\text{gate}\cdot v_d\)。注意这里的 \(x\) 是**原始** \(x\)（残差支路），不是归一化后的 \(\hat{x}\)。

一个关键设计：前向把 \(\text{raw\_dot}\)（未归一化的点积）、\(\text{gate}\)、\(\text{rstd}_x\)、\(\text{rstd}_k\) 存到全局内存（`save_for_backward=True`），反向直接复用，避免重算。

#### 4.1.2 核心流程

```
输入 x:(N, hc_mult, H) bf16, k:(N, hc_mult, H) bf16, v:(N, H) bf16, weight_fused:(hc_mult, H) fp32
  │
  ├─ 对每个 (token i, head h)：
  │    1) 累加 rstd_x  = Σ x²          （规约）
  │       累加 rstd_k  = Σ k²          （规约）
  │       累加 raw_dot = Σ x·weight_fused·k   （规约）
  │    2) rstd_x = rsqrt(rstd_x / H + eps)，rstd_k 同理
  │    3) raw_dot、rstd_x、rstd_k 存盘（供反向）
  │    4) dot  = raw_dot · rstd_x · rstd_k · scalar
  │       gate = sigmoid( copysign(sqrt(clamp(|dot|, c)), dot) )   # 即 signed_sqrt + sigmoid
  │       gate 存盘
  │    5) out[i,h,d] = x[i,h,d] + gate · v[i,d]   # 逐元素，v 广播到每个 head
  └─
输出 out:(N, hc_mult, H) bf16 (+ 可选 dot/gate/rstd_x/rstd_k)
```

规约步是计算瓶颈，因为每个 token 要把 \(H\) 维（4096 或 7168）扫一遍算三个累加器；输出步要把同样的 \(H\) 维再扫一遍写回。两次扫 \(H\) 怎么省带宽？kernel 用了「Pass 1 算门 / Pass 2 写输出」的两遍式 + `cp.async` 双缓冲流水，下文细讲。

#### 4.1.3 源码精读

权威数学在 PyTorch 参考里，逐行对应 kernel：

[tile_kernels/torch/engram.py:89-108](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/torch/engram.py#L89-L108) —— 前向数学的参考实现，`raw_dot` 即存盘的 `dot_out`：

```python
rstd_x = torch.rsqrt(x.pow(2).mean(-1) + eps)
rstd_k = torch.rsqrt(k_f.pow(2).mean(-1) + eps)
raw_dot = torch.einsum('...d,...d->...', x * wh, k_f * we)   # = Σ x·k·weight_fused
dot = raw_dot * rstd_x * rstd_k * scalar
signed_sqrt = dot.abs().clamp_min(clamp_value).sqrt() * dot.sign()
gate_score = signed_sqrt.sigmoid()
output = x + gate_score.unsqueeze(-1) * v.unsqueeze(-2)
```

kernel 里三个累加器在 Pass 1 的内层循环里一次算出，`weight_fused` 直接当 `w_local` 读入：

[tile_kernels/engram/engram_gate_kernel.py:106-116](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/engram/engram_gate_kernel.py#L106-L116) —— 三个规约（`rstd_x_local` 累加 x²、`rstd_k_local` 累加 k²、`gate_score_local` 累加 `x·w·k`）共用一次 load：

```python
for i_k in T.vectorized(vec_size):
    w_local[i_k] = weight_fused[pid_h, sub_base + thread_idx * vec_size + i_k]
for i_k in T.serial(vec_size):
    rstd_x_local[0]  += x_local[i_k] * x_local[i_k]
    rstd_k_local[0]  += k_local[i_k] * k_local[i_k]
    gate_score_local[0] += x_local[i_k] * w_local[i_k] * k_local[i_k]
```

规约完成后做 `rsqrt`（注意 `/hidden_size` 把「平方和」还原成「均方」），并存盘、算门：

[tile_kernels/engram/engram_gate_kernel.py:139-158](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/engram/engram_gate_kernel.py#L139-L158) —— warp 归约 → rstd → 存盘 → 归一化点积 → 门控激活：

```python
rstd_k_reducer[0] = T.warp_reduce_sum(rstd_k_local[0])
rstd_x_reducer[0] = T.warp_reduce_sum(rstd_x_local[0])
gate_score_reducer[0] = T.warp_reduce_sum(gate_score_local[0])

rstd_x_reducer[0] = T.rsqrt(rstd_x_reducer[0] / hidden_size + eps)
rstd_k_reducer[0] = T.rsqrt(rstd_k_reducer[0] / hidden_size + eps)

if save_for_backward:            # 存的是 raw_dot（尚未 ×rstd×scalar）
    if thread_idx == 0:
        dot_out[i_s, pid_h] = gate_score_reducer[0]
        ...

gate_score_reducer[0] = gate_score_reducer[0] * rstd_x_reducer[0] * rstd_k_reducer[0] * scalar
gate_score_reducer[0] = T.sigmoid(
    T.copysign(T.sqrt(T.clamp(T.abs(gate_score_reducer[0]), clamp_value, float('inf'))),
               gate_score_reducer[0]))
```

`T.copysign(a, b)` 即 `|a|·sign(b)`，所以这一长行等价于参考里的 `sqrt(clamp_min(|dot|)).sign(dot)` 再 `sigmoid`。务必看清：**`dot_out` 在第 152 行乘 `rstd` 与 `scalar` 之前就存盘了**，所以它存的是「未归一化的原始点积」`raw_dot`，与参考 `engram_gate_ref` 返回的第二个值一致——测试正是用它俩直接对拍（`diff_dot < 2e-10`）。

输出步（Pass 2）：

[tile_kernels/engram/engram_gate_kernel.py:171-177](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/engram/engram_gate_kernel.py#L171-L177) —— `out = x + gate · v`，\(x\) 复用 Pass 1 缓存在 `x_smem` 的原始行，\(v\) 从 `kv_smem` 流入：

```python
x_local[i_k] = x_smem[sub_base + thread_idx * vec_size + i_k]
v_local[i_k] = kv_smem[tile_phase, i_sub * reduce_blk + thread_idx * vec_size + i_k]
output[i_s, pid_h, sub_base + thread_idx * vec_size + i_k] = x_local[i_k] + gate_score_reducer[0] * v_local[i_k]
```

wrapper 侧固定 `scalar = hidden_size**-0.5`，并把 `num_sms` 当编译期参数喂进 JIT 构造器：

[tile_kernels/engram/engram_gate_kernel.py:496-502](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/engram/engram_gate_kernel.py#L496-L502) —— `scalar` 与 `get_num_sms()` 注入：

```python
scalar = hidden_size**-0.5
...
kernel = get_engram_gate_fwd_kernel(hidden_size, eps, scalar, k_stride_s, k_stride_h, v_stride_s,
                                    get_num_sms(), clamp_value, hc_mult, save_for_backward)
```

> 关键结论：数学上 `dot_out=raw_dot`（未归一化），门控用归一化后的 `dot`，输出残差用**原始** \(x\)。三者别混。

#### 4.1.4 代码实践

实践目标：确认 kernel 与参考在 `dot_out`、`gate`、`out` 三者上数值一致，并验证「输出用原始 x」这一隐藏点。

操作步骤：

1. 读 [tests/engram/test_engram_gate_fwd.py:40-70](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tests/engram/test_engram_gate_fwd.py#L40-L70)，注意它对 `dot/gate_score/rstd_x/rstd_k` 都做 `calc_diff(...) < 2e-10` 断言——这是「kernel 存盘量与参考返回量一一对应」的硬证据。
2. 在本地 GPU 上运行：
   ```bash
   pytest tests/engram/test_engram_gate_fwd.py -k "not benchmark" -n 4
   ```
3. （源码阅读型）对照 [tile_kernels/torch/engram.py:103-104](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/torch/engram.py#L103-L104) 与 kernel 第 148 行，回答：为什么参考里 `dot = raw_dot * rstd_x * rstd_k * scalar`，而 kernel 存盘的 `dot_out` 却不乘这三项？

需要观察的现象：测试通过；`out_no_save == out_save`（位精确，见第 70 行 `assert_equal`），说明 `save_for_backward` 只影响「是否存中间量」，不影响前向数值。

预期结果：5 个量（out/dot/gate/rstd_x/rstd_k）的 `calc_diff` 全部 `< 2e-10`。

第 3 步答案：因为反向需要的「点积」是 raw_dot，归一化项（rstd）单独存了，反向可按需重组；存 raw_dot 信息无损且省一次乘法。若无法本地运行，标注「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：把 `clamp_value` 设成 0 会有什么数值风险？
答案：\(|\text{dot}|\to 0\) 时 \(\sqrt{|\text{dot}|}\to 0\)，但 signed-sqrt 在 0 处的导数 \(\sim 1/\sqrt{|\text{dot}|}\to\infty\)，反向会梯度爆炸；`clamp_value=1e-6` 把开方输入截在正数，保证导数有界。

**练习 2**：为什么 `scalar = hidden_size**-0.5` 而不是 `1/hidden_size`？
答案：RMSNorm 后 \(\hat{x}\) 的均方根为 1，即 \(\|\hat{x}\|_2\approx\sqrt{H}\)。点积 \(\sum_d \hat{x}_d\hat{k}_d\) 的量级是 \(O(H)\)，除以 \(\sqrt{H}\) 后才是「余弦相似度」量级 \(O(\sqrt{H})\)，再经 signed-sqrt 压缩进 \((-1,1)\) 让 sigmoid 工作在敏感区。`1/H` 会压过头。

**练习 3**：输出里的 \(x\) 用的是归一化后的 \(\hat{x}\) 还是原始 \(x\)？
答案：原始 \(x\)（残差支路）。kernel Pass 2 直接读 `x_smem`（存的是输入行），参考第 108 行 `x + gate_score * v` 也用原始 `x`。

---

### 4.2 持久化 kernel 模式：固定网格 + token 分片循环

#### 4.2.1 概念说明

常规 kernel 写法是「一个 token-head 启动一个 block」：网格大小 = `num_tokens × hc_mult`。问题有两个：① `num_tokens` 是运行时动态符号，token 一多网格极大，启动开销陡增；② 小批量（如推理 decode 时 token 数很少）时网格小于 SM 数，大量 SM 空转。

持久化 kernel 反其道而行：**网格大小绑定硬件**（由 `num_sms` 推出的 `num_persistent_blocks`），与 `num_tokens` 无关。每个 block 在 `T.Serial` 循环里连续吃掉一段 token，块「常驻」SM。这样：

- 网格规模稳定，不随 token 数膨胀；
- `weight_fused` 等常量可留在寄存器跨 token 复用；
- 小批量时也能填满 SM（只要 `num_persistent_blocks >= num_sms`）。

代价是 kernel 内部多一层「分片 + 循环 + 边界裁剪」的逻辑。

#### 4.2.2 核心流程

```
网格 grid = (hc_mult, num_persistent_blocks)，每块 1 个 warp (threads=32)
绑定 pid_h ∈ [0, hc_mult)，pid_b ∈ [0, num_persistent_blocks)

per_block = ceildiv(num_tokens, num_persistent_blocks)        # 每块分到的 token 数（运行时算）
t_start   = min(per_block * pid_b,     num_tokens)
t_end     = min(per_block * (pid_b+1), num_tokens)

for i_s in Serial(t_start, t_end):     # 该持久化块串行处理 [t_start, t_end) 内的 token
    ... 对 token i_s、head pid_h 算 Pass1(门) + Pass2(输出) ...
```

覆盖性证明：因为 `per_block = ceildiv(num_tokens, N)`，有 `per_block·N >= num_tokens`，所以并集 \(\bigcup_{b} [\text{per\_block}\cdot b,\ \text{per\_block}\cdot(b+1))\) 覆盖 \([0,\text{num\_tokens})\)；`min(..., num_tokens)` 只是把末尾块的越界部分裁掉。每个 token 恰好落在一个块里，不重不漏。

注意：`per_block` 只依赖 `pid_b`，与 `pid_h` 无关——即对同一个 `pid_b`，4 个头处理的是**同一串 token**。

#### 4.2.3 源码精读

网格定义与线程数：

[tile_kernels/engram/engram_gate_kernel.py:70-71](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/engram/engram_gate_kernel.py#L70-L71) —— 二维网格 `(hc_mult, num_persistent_blocks)`，每块一个 warp：

```python
with T.Kernel(hc_mult, num_persistent_blocks, threads=threads) as (pid_h, pid_b):
    thread_idx = T.get_thread_binding()
```

token 分片与边界裁剪（`num_tokens` 是动态符号，`T.ceildiv` 在 kernel 内运行时求值）：

[tile_kernels/engram/engram_gate_kernel.py:86-90](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/engram/engram_gate_kernel.py#L86-L90) —— 分片计算与持久化循环：

```python
per_block = T.ceildiv(num_tokens, num_persistent_blocks)
t_start = T.min(per_block * pid_b, num_tokens)
t_end   = T.min(per_block * (pid_b + 1), num_tokens)

for i_s in T.Serial(t_start, t_end):
    ...
```

`num_persistent_blocks` 是编译期参数（在 Python 侧算好烤进 kernel），`num_tokens` 是运行时符号——这正是 u2-l1 讲过的「编译期 vs 运行时」切分：分块数随硬件固化，token 数随输入变化。

`weight_fused` 的跨 token 复用体现在它只依赖 `pid_h`，在每个持久化块的整个 token 循环里不变，可常驻寄存器（见第 112 行 `weight_fused[pid_h, ...]`）。

> 这里的持久化是「软持久化」：块内 `Serial` 循环复用寄存器/共享内存，而非硬件级线程块驻留。效果上同样达到了「网格随硬件、跨 token 复用」的目标。

#### 4.2.4 代码实践

实践目标：验证持久化块对 token 的覆盖性。

操作步骤（纯计算，无需 GPU）：

1. 设 `num_persistent_blocks = 528`（4.3 节会算出此值），分别取 `num_tokens = 2048`、`num_tokens = 2049`、`num_tokens = 10`。
2. 对每个 `pid_b ∈ {0, 1, 527}`，手算 `per_block = ceildiv(num_tokens, 528)`、`t_start`、`t_end`。
3. 验证：所有 `pid_b` 的 \([t_start, t_end)\) 并集恰为 \([0, \text{num\_tokens})\)，且相邻块无缝衔接。

需要观察的现象：
- `num_tokens=2048`：`per_block = ceildiv(2048,528)=4`，前 512 块各分 4 个 token（512×4=2048），后 16 块 `t_start==t_end` 空转——这是持久化 kernel 处理「token 不是块数整数倍」的正常现象，`Serial` 循环 0 次直接跳过。
- `num_tokens=10`：`per_block=1`，只有前 10 块有活干，其余空转；但 528 个块仍全部启动填满 SM，这就是「小批量也能填满硬件」的收益。

预期结果：三种情形下并集都精确覆盖 \([0, \text{num\_tokens})\)，无重叠无遗漏。

#### 4.2.5 小练习与答案

**练习 1**：为什么网格用 `(hc_mult, num_persistent_blocks)` 而不是 `(num_persistent_blocks,)` 然后在块内循环 4 个头？
答案：把 `pid_h` 放进网格维度让 4 个头**并行**跑在不同 block 上（独立 SM/warp 调度），比块内串行 4 个头更充分利用硬件；同时 `weight_fused` 仍按 `pid_h` 区分，各块互不干扰。

**练习 2**：若 `num_tokens` 不是 `num_persistent_blocks` 的倍数，尾部块会怎样？
答案：`per_block = ceildiv(...)` 已向上取整，尾部若干块的 `t_start==t_end`（`min` 裁剪后区间为空），`Serial(t_start,t_end)` 循环 0 次，块空转但不出错。

**练习 3**：持久化相比「每 token 一个块」，省了什么开销？
答案：① kernel launch 开销（一次启动固定块数，而非随 token 数增长）；② 跨 token 复用常驻数据（`weight_fused`、`x_smem` 双缓冲）省访存；③ 小批量时网格仍 ≥ SM 数，避免空转。

---

### 4.3 SM 占用启发式：_choose_blk_d 与 _choose_num_persistent_blocks

#### 4.3.1 概念说明

持久化块数 `num_persistent_blocks` 不是拍脑袋定的，而是按「**GPU 一次能同时驻留多少块**」来估算——这就是 occupancy（占用率）启发式。两个约束：

1. **共享内存约束**：每块占用的 SMEM 不能超过「单 SM 共享内存上限 ÷ 块数」。SMEM 占用越大，单 SM 能并发驻留的块越少。
2. **寄存器约束**：即便 SMEM 够，寄存器压力也会限制并发块数（每块寄存器用太多，SM 塞不下）。这里用一个经验上界 16 兜底。

目标是让 `hc_mult × num_persistent_blocks ≈ num_sms × blocks_per_sm`，即**总块数恰好填满 GPU 一次能驻留的槽位**，既不浪费 SM，也不过度超订引发额外上下文切换。

另外还有一个子问题：`blk_d`（k/v 在共享内存里的 tile 大小，`kv_smem` 是 `(2, blk_d)` 的双缓冲）。它要能整除 `hidden_size`，且至少留 2 个 tile（否则双缓冲无意义），并尽量大以摊薄循环开销。

#### 4.3.2 核心流程

```
# 1. 选 tile 大小 blk_d（编译期）
_choose_blk_d(hidden_size):
    for blk in [1024, 768, 512, 256]:
        if hidden_size % blk == 0 and hidden_size >= 2*blk:   # 整除 + 至少 2 tile
            return blk

# 2. 估算每块 SMEM
smem_bytes = hidden_size * 2          # x_smem: bf16
           + blk_d * 4                # kv_smem: (2, blk_d) bf16 = 2*blk_d*2

# 3. 每 SM 能驻留几块
blocks_per_sm = min( max_smem_per_sm // smem_bytes, 16 )   # 16: 寄存器压力兜底

# 4. 反推持久化块数（总块数 = hc_mult * num_persistent_blocks ≈ num_sms * blocks_per_sm）
num_persistent_blocks = num_sms * blocks_per_sm // hc_mult
```

`max_smem_per_sm` 与 `num_sms` 由 [tile_kernels/config.py](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/config.py) 探测：

[tile_kernels/config.py:7-10](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/config.py#L7-L10) 与 [tile_kernels/config.py:19-29](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/config.py#L19-L29) —— 两个带 `lru_cache` 的硬件探测函数：

```python
@functools.lru_cache(maxsize=None)
def get_device_num_sms() -> int:
    prop = torch.cuda.get_device_properties(torch.cuda.current_device())
    return prop.multi_processor_count

@functools.lru_cache(maxsize=None)
def get_max_smem_per_sm() -> int:
    prop = torch.cuda.get_device_properties(torch.cuda.current_device())
    return prop.shared_memory_per_multiprocessor
```

`get_num_sms()` 默认返回设备真实 SM 数，但可被 `set_num_sms(n)` 覆盖（用于调优实验，把可用并行度人为调小）。

#### 4.3.3 源码精读

`_choose_blk_d` 从大到小挑第一个合法 tile：

[tile_kernels/engram/engram_gate_kernel.py:35-39](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/engram/engram_gate_kernel.py#L35-L39) —— 选 tile：

```python
def _choose_blk_d(hidden_size):
    for blk in [1024, 768, 512, 256]:
        if hidden_size % blk == 0 and hidden_size >= 2 * blk:
            return blk
    raise ValueError(f'No valid blk_d for hidden_size={hidden_size}')
```

- `hidden_size=4096`：4096%1024==0 且 4096≥2048 → `blk_d=1024`，`num_blk=4`。
- `hidden_size=7168`：7168%1024==0 且 7168≥2048 → `blk_d=1024`，`num_blk=7`。

注释明说「只对 {4096, 7168} 调过性能」（第 34 行），其他 `hidden_size` 能跑但非最优。

占用估算主体：

[tile_kernels/engram/engram_gate_kernel.py:41-46](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/engram/engram_gate_kernel.py#L41-L46) —— SMEM 预算 + 双重 cap：

```python
def _choose_num_persistent_blocks(hidden_size, blk_d, num_sms, hc_mult):
    # smem per block: x_smem (bf16) + kv_smem double-buffer (bf16)
    smem_bytes = hidden_size * 2 + blk_d * 4
    blocks_per_sm = min(get_max_smem_per_sm() // smem_bytes, 16)  # 16: register pressure cap
    return num_sms * blocks_per_sm // hc_mult
```

读法：`hidden_size*2` 是 `x_smem`（一行 bf16）；`blk_d*4` 是 `kv_smem` 的双缓冲 `(2, blk_d)` bf16（`2*blk_d*2` 字节）。`blocks_per_sm` 取「SMEM 允许的块数」与「寄存器压力上界 16」的较小值。最后 `// hc_mult` 把「总槽位」摊到 `pid_h` 之外的那个网格维度。

`smem_bytes` 注释强调这是**前向**的 SMEM 账单（反向 kernel 更复杂，有 `go_smem`/`v_smem`/`x_smem`/`k_smem`/`w_smem` 等，见 u6-l2），所以前向/反向各有独立的占用估算，不能共用。

派生维度（影响内层循环结构）：

[tile_kernels/engram/engram_gate_kernel.py:51-56](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/engram/engram_gate_kernel.py#L51-L56) —— `num_blk`/`reduce_blk`/`sub_blks`：

```python
num_blk = hidden_size // blk_d        # hidden 维切成几个 tile
reduce_blk = threads * vec_size       # 32 * 8 = 256，一个 warp 一轮处理的元素数
sub_blks = blk_d // reduce_blk        # 一个 tile 内分几段（每段 256 元素）
v_start_phase = num_blk % 2           # Pass2 复用 kv_smem 装 v 时的相位对齐
```

`threads=32`（单 warp）、`vec_size=8`（每线程向量化 8 元素），故 `reduce_blk=256`；`blk_d=1024` 时 `sub_blks=4`，即每 tile 分 4 段、每段 32 线程×8 元素。

> 关键结论：`num_persistent_blocks` 由 `num_sms × blocks_per_sm ÷ hc_mult` 决定，是「填满 GPU 一次驻留槽位」的估算；`blk_d` 由整除与双缓冲约束决定。两者共同把网格规模绑定到硬件而非问题规模。

#### 4.3.4 代码实践

实践目标：手算给定 `hidden_size` 下的 `blocks_per_sm` 与 `num_persistent_blocks`，并解释覆盖。

操作步骤：

1. 先在你的 GPU 上查出两个硬件常量（示例代码，非项目代码）：
   ```python
   import torch
   prop = torch.cuda.get_device_properties(0)
   print(prop.multi_processor_count, prop.shared_memory_per_multiprocessor)
   ```
   记下 `num_sms` 与 `max_smem_per_sm`（单位：字节）。若本地无 GPU，标注「待本地验证」并用假设值代入。
2. 对 `hidden_size = 4096`（`blk_d=1024`）：
   - `smem_bytes = 4096*2 + 1024*4 = 12288` 字节
   - `blocks_per_sm = min(max_smem_per_sm // 12288, 16)`
   - `num_persistent_blocks = num_sms * blocks_per_sm // 4`
3. 对 `hidden_size = 7168`（`blk_d=1024`）重复上一步。
4. 验证总块数 `hc_mult * num_persistent_blocks` 是否 `≈ num_sms * blocks_per_sm`。

需要观察的现象（以典型 Hopper H100 假设值 `num_sms=132`、`max_smem_per_sm=228 KB=233472` 为例，**具体数值待本地验证**）：

| hidden_size | blk_d | smem_bytes | max_smem//smem | blocks_per_sm | num_persistent_blocks | 总块数 |
|---|---|---|---|---|---|---|
| 4096 | 1024 | 12288 | 19 | **16**（被寄存器 cap 限制） | 132*16//4=528 | 2112 |
| 7168 | 1024 | 18432 | 12 | 12 | 132*12//4=396 | 1584 |

注意 `hidden_size=4096` 时 SMEM 本可塞 19 块，但被「寄存器压力 cap=16」截断——这正是该启发式不纯粹依赖 SMEM 的体现。

预期结果：`hc_mult * num_persistent_blocks` 与 `num_sms * blocks_per_sm` 最多差 `hc_mult-1`（来自 `// hc_mult` 的整除截断）。

覆盖解释：代入 4.2 节，`num_persistent_blocks` 个块按 `per_block = ceildiv(num_tokens, num_persistent_blocks)` 分片，并集精确覆盖 \([0,\text{num\_tokens})\)。例如 `hidden_size=4096`、`num_tokens=2048`、`num_persistent_blocks=528`：`per_block=4`，前 512 块各吃 4 token，后 16 块空转，全部 2048 token 被覆盖。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `blocks_per_sm` 要与 16 取 `min`？
答案：SMEM 只是占用约束之一，寄存器是另一个。每块寄存器用得多时，即便 SMEM 富余，SM 也塞不下太多块。16 是项目对 engram 前 kernel 寄存器压力的经验上界，防止「SMEM 说能塞 19 块、实际寄存器只够 16 块」的误判。

**练习 2**：把 `hidden_size` 从 4096 翻倍到 8192（假设 `%1024==0`），`num_persistent_blocks` 会怎么变？
答案：`smem_bytes` 增大（`x_smem` 翻倍），`blocks_per_sm` 不增反减，故 `num_persistent_blocks` 减小。更大的 hidden 意味着每块更「重」（SMEM/寄存器更多），GPU 同时驻留的块数更少——这是占用启发式自带的「问题变大→并发块变少」的负反馈。

**练习 3**：若用 `set_num_sms(32)` 把可用 SM 调小，`num_persistent_blocks` 如何变化？前向数值会变吗？
答案：`num_persistent_blocks` 随 `num_sms` 线性减小（`num_sms*blocks_per_sm//hc_mult`），网格变小、每块分到更多 token。前向数值**不变**（覆盖性保证每个 token 仍被处理一次），变的是性能——这正是 u10-l1 硬件感知调优的抓手。

---

## 5. 综合实践

把本讲三块知识串起来：为一个新 GPU 推算 engram 前向的完整调度参数，并解释它如何覆盖全部 token。

任务：

1. 探测本机 GPU 的 `num_sms` 与 `max_smem_per_sm`（用 config.py 的函数或直接 `torch.cuda.get_device_properties`）。
2. 取 `hidden_size ∈ {4096, 7168}`，按 4.3.4 的表格手算 `blk_d`、`smem_bytes`、`blocks_per_sm`、`num_persistent_blocks`、总块数。
3. 取 `num_tokens = 4096`，算出 `per_block`，写出 `pid_b=0`、`pid_b=num_persistent_blocks-1` 两个块的 `[t_start, t_end)`，并验证覆盖。
4. （可选，需 GPU）运行 benchmark 观察带宽：
   ```bash
   pytest tests/engram/test_engram_gate_fwd.py -k benchmark --run-benchmark \
       --benchmark-output /tmp/engram_fwd.jsonl
   ```
   读 JSONL 里的 `bandwidth_gbs`，对比 `save=True` 与 `save=False` 两条记录，解释「存中间量多写了 dot/gate/rstd_x/rstd_k 四个小张量（每 token 仅 `hc_mult` 个标量）」对带宽的微小影响。
5. （可选，需 GPU）用 `set_num_sms` 把 SM 减半，重跑 benchmark，观察延迟变化并解释（提示：`num_persistent_blocks` 减半 → 每块 token 翻倍 → 串行循环更长，但仍是同一总工作量）。

交付：一张参数表 + 一段覆盖性说明 + （可选）一组带宽数据。无法在 GPU 上验证的部分明确标注「待本地验证」。

## 6. 本讲小结

- Engram 前向数学四步：分别 RMSNorm → 融合权重点积 `raw_dot=Σ x·k·weight_fused` → 归一化 `dot=raw_dot·rstd_x·rstd_k·scalar`（`scalar=H^-0.5`）→ `gate=sigmoid(signed_sqrt(clamp(|dot|)))` → 残差输出 `out=x+gate·v`；其中 `x` 用原始值，`weight_fused=wh·we` 融合一次读。
- 前向存盘四个量 `dot_out=raw_dot`、`gate`、`rstd_x`、`rstd_k` 供反向复用；`dot_out` 存的是**未归一化**的原始点积（在乘 rstd/scalar 之前保存）。
- Persistent kernel：网格 `(hc_mult, num_persistent_blocks)` 绑定硬件，`per_block=ceildiv(num_tokens, num_persistent_blocks)` 分片，`Serial` 循环覆盖 `[t_start, t_end)`，块常驻复用 `weight_fused`。
- 覆盖性：`per_block·N >= num_tokens` + `min` 裁剪 ⇒ 每个 token 恰落一块，不重不漏；尾部块可能空转。
- 占用启发式：`smem_bytes = hidden_size*2 + blk_d*4`；`blocks_per_sm = min(max_smem//smem_bytes, 16)`（16 是寄存器压力 cap）；`num_persistent_blocks = num_sms*blocks_per_sm//hc_mult`，目标是用「恰好填满 GPU 一次驻留槽位」的块数。
- `blk_d` 从 `[1024,768,512,256]` 选第一个整除且 `hidden_size≥2·blk` 的值，保证至少 2 个 tile 供双缓冲；项目只对 `{4096,7168}` 调过性能。

## 7. 下一步学习建议

- **u6-l2 Engram 反向与权重梯度归约**：承接本讲存盘的 `dot/gate/rstd_x/rstd_k`，看反向 kernel 如何算 `grad_x/grad_k/grad_v`，以及 `grad_w_partial` 为何要跨 `num_persistent_blocks` 再做一次 `grad_w_reduce` 归约（这正是持久化「分块累加」的收尾步骤）。
- **u6-l3 Engram 哈希与融合权重**：本讲把 `weight_fused` 当现成输入，它的来源 `fused_weight`（把两个 RMSNorm 权重融成一个 fp32 张量）就在那里。
- **u10-l1 SM/共享内存感知调优**：本讲的占用启发式是硬件感知调优的典型案例，u10-l1 会把 `set_num_sms` 与 `get_max_smem_per_sm` 拉成系统主题，配合 benchmark 做扫描实验。
- 继续阅读 [tile_kernels/engram/engram_gate_kernel.py](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/engram/engram_gate_kernel.py) 的反向部分（192-467 行）与 [tile_kernels/modeling/engram/engram_gate.py](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/modeling/engram/engram_gate.py)，看 autograd.Function 如何把前向/反向串成可求导层。
