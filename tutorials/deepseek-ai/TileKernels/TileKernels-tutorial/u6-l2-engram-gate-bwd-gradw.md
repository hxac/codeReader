# Engram 反向与权重梯度归约

## 1. 本讲目标

本讲承接 u6-l1（engram 门控前向 kernel 与持久化调度），把镜头从前向切到反向。学完后你应当能够：

- 说清前向 `save_for_backward` 保存的四个中间量（`dot` / `gate_score` / `rstd_x` / `rstd_k`）各自在反向里被用来做什么，以及为什么 `dot` 保存的是**未归一化**的原始点积。
- 跟读 `engram_gate_bwd_kernel` 的三段式反向流程（算 \(G=\partial L/\partial g\) → 写 `grad_v` → 写 `grad_x/grad_k/grad_w`），并理解为什么四个梯度输出里只有 `grad_w` 采用「分块部分梯度 + 二次归约」。
- 讲清 `grad_w_reduce` 如何把「跨持久化块求和」「融合权重拆分成 \(w_h/w_e\)」「就地累加进 fp32 缓冲」三件事融进同一个 kernel。
- 解释当 `weight_hidden` 拥有 `main_grad` 时，`EngramGateFn.backward` 为什么对该参数返回 `None`。

## 2. 前置知识

- **反向传播与 autograd**：神经网络训练需要损失对每个参数的梯度。PyTorch 用 `torch.autograd.Function` 的 `forward`/`backward` 对来手写不可导/融合算子的梯度。`backward` 的返回值个数必须等于 `forward` 的输入个数，逐位对应；不需要梯度的输入位返回 `None`。
- **engram 门控的前向数学**（u6-l1 已建立，这里复习记号）：
  - 输入 \(x\)（hidden states）、\(k\)（key embedding）、\(v\)（value embedding），形状分别为 \((N,h,H)\)、\((N,h,H)\)、\((N,H)\)，其中 \(h=\text{hc\_mult}=4\)、\(H=\text{hidden\_size}\)。
  - 两个 RMSNorm 权重 \(w_h\)、\(w_e\)，形状 \((h,H)\)；前向把它们预先融合成 \(w=w_h\odot w_e\)（逐元素乘），形状 \((h,H)\)，存为 fp32。
  - 前向四步：RMSNorm → 加权点积 → signed-sqrt 门控 → 残差输出。
- **持久化 kernel**（u6-l1）：网格维度绑硬件 SM 数而非 token 数，每个 block 在 `Serial` 循环里处理不重不漏的一段 token（`per_block=ceil(num_tokens/num_persistent_blocks)`），跨 token 常驻复用权重。反向 kernel 沿用这套切分。
- **shared memory 与 cp.async 流水线**（u2-l2、u6-l1）：用 `async_copy` 异步搬数据进 SMEM、用 `ptx_wait_group` 等待、用双缓冲隐藏延迟。本讲只复述结构，不再展开。

> 一句话定位：前向「算出 `gate` 并把中间量存盘」，反向「拿着存盘的中间量，把上游梯度 `grad_out` 分发回 \(x/k/v/w\)」。本讲的重点是这套分发链里**最巧妙的两环**——权重梯度的「分块部分梯度 + 二次归约」，以及它与训练框架 `main_grad` 缓冲的对接。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [tile_kernels/engram/engram_gate_kernel.py](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/engram/engram_gate_kernel.py) | engram 门控的前向 + 反向 kernel。本讲聚焦后半部分：`get_engram_gate_bwd_kernel`（反向 prim_func）与 `engram_gate_bwd`（wrapper）。 |
| [tile_kernels/engram/engram_grad_w_reduce_kernel.py](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/engram/engram_grad_w_reduce_kernel.py) | `grad_w_reduce`：把反向产出的「按持久化块切分的部分权重梯度」归约、拆分成 \(w_h/w_e\)、就地累加进 fp32 缓冲。 |
| [tile_kernels/modeling/engram/engram_gate.py](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/modeling/engram/engram_gate.py) | `EngramGateFn`：用 `torch.autograd.Function` 把前向/反向/`grad_w_reduce` 串成可求导层，并实现 `main_grad` 就地累加 + 返回 `None` 的优化。 |
| [tile_kernels/torch/engram.py](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/torch/engram.py) | `engram_gate_ref`：纯 PyTorch 参考实现，支持 autograd，是对拍与推导梯度的「标准答案」。 |
| [tests/engram/test_engram_gate_bwd.py](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tests/engram/test_engram_gate_bwd.py) / [tests/engram/test_engram_grad_w_reduce.py](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tests/engram/test_engram_grad_w_reduce.py) | 反向与权重归约的正确性 + benchmark 测试，是本讲代码实践的依据。 |

## 4. 核心概念与源码讲解

### 4.1 前向保存与反向复用的对应关系

#### 4.1.1 概念说明

反向传播要算 \(\partial L/\partial x\)、\(\partial L/\partial k\)、\(\partial L/\partial v\)、\(\partial L/\partial w\)，但前向里很多中间量（点积、门控值、RMSNorm 的 rstd）在反向里会**反复用到**。重新算一遍既慢又会因为 bf16 累加误差导致梯度对不上 autograd 参考值。标准做法是：前向把这些中间量**存盘**（写到一段独立的 HBM 缓冲），反向直接读回来用。

engram 门控存了四个 fp32 中间量。理解本节的关键是搞清「存了什么 → 反向拿来算什么」，并注意到一个容易踩的坑：**存的点积是未归一化的原始点积**，不是最终喂给 sigmoid 的那个值。

#### 4.1.2 核心流程

前向数学（复习 + 固定记号）：

\[
\text{raw\_dot}=\sum_{d} x_d\, w_d\, k_d,\qquad w=w_h\odot w_e
\]

\[
\text{rstd}_x=\text{rsqrt}\!\left(\tfrac{1}{H}\sum_d x_d^2+\varepsilon\right),\quad
\text{rstd}_k=\text{rsqrt}\!\left(\tfrac{1}{H}\sum_d k_d^2+\varepsilon\right)
\]

\[
\hat d=\text{raw\_dot}\cdot\text{rstd}_x\cdot\text{rstd}_k\cdot H^{-1/2}
\]

\[
g=\sigma\!\left(\operatorname{copysign}\!\left(\sqrt{\max(|\hat d|,\,c)}\ ,\ \hat d\right)\right),\qquad \text{output}=x+g\cdot v
\]

其中 \(c=\text{clamp\_value}\)、\(\sigma\) 是 sigmoid、\(\operatorname{copysign}\) 给平方根补回符号（即 signed-sqrt）。

前向存盘四个量，反向各自的用途：

| 前向保存量（wrapper 名） | 形状 | 反向 kernel 里的名字 | 反向用途 |
| --- | --- | --- | --- |
| `dot`（即「原始点积」raw_dot） | \((N,h)\) fp32 | `dot_in` | ① 门控导数的「夹断判据」\(\vert\hat d\vert<c\)；② 门控导数的幅度因子 \(1/\sqrt{\lvert\text{raw\_dot}\rvert}\)；③ RMSNorm 反向里的 \(\text{dot\_x}=\text{raw\_dot}\cdot\text{rstd}_x^2/H\)、\(\text{dot\_k}=\text{raw\_dot}\cdot\text{rstd}_k^2/H\) |
| `gate_score`（即最终门控值 \(g\)） | \((N,h)\) fp32 | `gate_in` | ① \(\text{grad}_v=\sum_h \text{grad\_out}_h\cdot g_h\)；② 门控导数因子 \(g(1-g)\) |
| `rstd_x` | \((N,h)\) fp32 | `rstd_x_in` | ① 门控导数幅度里的 \(\text{rstd}_x\)；② \(\text{dot\_x}\) |
| `rstd_k` | \((N,h)\) fp32 | `rstd_k_in` | ① 门控导数幅度里的 \(\text{rstd}_k\)；② \(\text{dot\_k}\) |

> **为什么存的是 raw\_dot 而不是 \(\hat d\)？** 因为反向里复现 \(\hat d\) 本就需要 \(\text{rstd}_x/\text{rstd}_k\)（它们反正也要为 RMSNorm 反向而保存），存 raw\_dot 既省一次乘法、又能直接用 \(\vert\text{raw\_dot}\rvert\) 表达幅度因子。这是一处典型的「存最原始量、在反向上即时重构派生量」的取舍。

#### 4.1.3 源码精读

前向 kernel 在归一化**之前**就把累加器存进 `dot_out`，归一化、signed-sqrt、sigmoid 之后再把最终门控值存进 `gate_score`：

[tile_kernels/engram/engram_gate_kernel.py:146-158](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/engram/engram_gate_kernel.py#L146-L158) —— 先存 `dot_out`（未归一化的原始点积）、`rstd_x`、`rstd_k`；第 152 行才做 \(\times\text{rstd}_x\times\text{rstd}_k\times\text{scalar}\)，第 154 行做 signed-sqrt + sigmoid，最后第 158 行存 `gate_score`。注意 148 行存的 `gate_score_reducer[0]` 在此刻还只是 \(\sum x\cdot w\cdot k\)，名字虽叫 `gate_score_reducer`，语义是点积。

wrapper 把这四个量作为独立缓冲分配并传给 kernel，它们就是反向的输入：

[tile_kernels/engram/engram_gate_kernel.py:65-68](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/engram/engram_gate_kernel.py#L65-L68) —— 前向 prim_func 的四个「存盘」输出张量声明。

参考实现 `engram_gate_ref` 用同样的语义保存，注释里明确点出 `raw_dot` 对应 kernel 的 `dot_out`：

[tile_kernels/torch/engram.py:102-112](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/torch/engram.py#L102-L112) —— `raw_dot = einsum(...)` 未归一化，`save_for_backward=True` 时返回 `(output, raw_dot, gate_score, rstd_x, rstd_k)`，与 kernel 一一对应。

#### 4.1.4 代码实践

**实践目标**：把「前向存盘 → 反向用途」的映射在真实代码里走一遍，确认 `dot` 确实是原始点积。

**操作步骤**（待本地验证，需要 CUDA 13.1+ 与 SM90/SM100 环境）：

1. 打开 [tests/engram/test_engram_gate_bwd.py:51-60](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tests/engram/test_engram_gate_bwd.py#L51-L60)，注意测试用 `engram_gate_ref(..., save_for_backward=True)` 拿到 `dot_ref/gate_score_ref/rstd_x_ref/rstd_k_ref`，再**原样**喂给 `engram_gate_bwd`。
2. 在 `engram_gate_ref` 里临时打印 `raw_dot` 与 `dot`（归一化后），观察二者差异；再确认 kernel 接收的 `dot_in` 与 `raw_dot` 数值一致。
3. 运行：`pytest tests/engram/test_engram_gate_bwd.py -n 4`。

**需要观察的现象**：把 `dot`（raw）误换成归一化后的 \(\hat d\) 喂给 `engram_gate_bwd`，`grad_x/grad_k/grad_wh/grad_we` 的 `calc_diff` 会立刻超过 `1e-8` 阈值而报错——这验证了「反向依赖的是原始点积」。

**预期结果**：原样喂入时五个 `calc_diff` 均 `< 1e-8`，测试通过。

#### 4.1.5 小练习与答案

**练习 1**：如果前向忘了保存 `rstd_k`，反向里哪一项梯度会算不出来？

**答案**：`grad_k` 的 RMSNorm 反向项（`dot_k = raw_dot·rstd_k²/H`）和门控导数幅度因子都会失真；具体说，没有 `rstd_k` 就无法重构 \(\hat d\)，也就无法算 \(\partial L/\partial\text{raw\_dot}\)，进而 `grad_k`、`grad_w` 都会错。

**练习 2**：`gate_score` 在反向里同时服务于 `grad_v` 和门控导数，这两处分别用的是 \(g\) 的哪个性质？

**答案**：`grad_v` 用 \(g\) 本身（\(\text{grad}_v=\sum_h\text{grad\_out}_h\cdot g_h\)，因为 output 对 \(v\) 的导数是 \(g\)）；门控导数用 \(g(1-g)\)（sigmoid 的导数）。

---

### 4.2 engram_gate_bwd kernel：反向传播三段式

#### 4.2.1 概念说明

反向 kernel 接收上游梯度 `grad_out`（\(\partial L/\partial\text{output}\)）和前向存盘的四个量，产出四个梯度：`grad_x`、`grad_k`、`grad_v`、`grad_w_partial`。前三个是「激活梯度」——带有 token 维 \((N,h,H)\) 或 \((N,H)\)，每个持久化 block 各写各的 token，互不重叠，**无需跨块归约**。第四个 `grad_w_partial` 是「参数梯度」——形状 \((\text{num\_persistent\_blocks},h,H)\)，没有 token 维，所有 block 都对同一个 \((h,H)\) 参数有贡献，于是必须跨 block 求和，这催生了 4.3 节的二次归约 kernel。

这个「激活梯度直写、参数梯度走分块归约」的分工，是本节最该带走的设计观点。

#### 4.2.2 核心流程

反向数学（令 \(G=\partial L/\partial g=\sum_d\text{grad\_out}_d\cdot v_d\)，即对门控值的上游梯度）：

门控对原始点积的导数（signed-sqrt 的关键性质：\(\operatorname{copysign}(\sqrt{|d|},d)\) 对 \(d\) 的导数恒为正，符号自动抵消）：

\[
\frac{\partial L}{\partial\,\text{raw\_dot}}
= G\cdot g(1-g)\cdot \frac{1}{2}\sqrt{\frac{H^{-1/2}\,\text{rstd}_x\,\text{rstd}_k}{|\text{raw\_dot}|}}
\]

当 \(|\hat d|<c\) 时（signed-sqrt 被夹断）置 0。这就是 kernel 里的 `dldg_r`。

四个梯度：

\[
\text{grad}_v=\sum_{h}\text{grad\_out}_h\cdot g_h
\]

\[
\text{grad}_x=\text{grad\_out}+\text{dldg\_r}\cdot(k\odot w-x\cdot\text{dot\_x}),\quad
\text{dot\_x}=\text{raw\_dot}\cdot\text{rstd}_x^2/H
\]

\[
\text{grad}_k=\text{dldg\_r}\cdot(x\odot w-k\cdot\text{dot\_k}),\quad
\text{dot\_k}=\text{raw\_dot}\cdot\text{rstd}_k^2/H
\]

\[
\text{grad\_w\_partial}\big|_{\text{block}}=\sum_{\text{token}\in\text{block}}\text{dldg\_r}\cdot x\cdot k
\]

kernel 的三段式（每个 token 串行处理）：

```
prologue : grad_out / v 进 SMEM（双缓冲，下一 token 预取）
pass 1a  : 算 G = Σ grad_out·v（每头两 warp 分担）→ 跨 warp 用 SMEM 归约
          → 由 G 经门控导数得 dldg_r = ∂L/∂raw_dot
pass 1b  : 写 grad_v（带 token 维，直写）
pass 2   : 流水线遍历 hidden 维 tile，同时写 grad_x、grad_k，并把
          dldg_r·x·k 累加进寄存器 grad_w_local（跨本 block 全部 token）
epilogue : 把 grad_w_local 写到本 block 的槽 grad_w_partial[pid_p]
```

注意 `grad_w_local` 是**每 block 一份的寄存器累加器**，在 `for i_s`（token 循环）里只累加、不写出，循环结束后一次性写到 `grad_w_partial[pid_p]`。这正是「部分梯度」的由来。

#### 4.2.3 源码精读

反向 kernel 的线程组织：8 warp / CTA，每头 2 warp（`hc_mult=4` 固定）；输入含前向四个存盘量，输出含 `grad_w_partial`：

[tile_kernels/engram/engram_gate_kernel.py:260-275](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/engram/engram_gate_kernel.py#L260-L275) —— prim_func 形参：`dot_in/gate_in/rstd_x_in/rstd_k_in` 是前向存盘量的反向入口，`grad_w_partial` 是「按块切分」的部分权重梯度。

token 循环开头，把前向存盘量读进每头标量寄存器：

[tile_kernels/engram/engram_gate_kernel.py:329-336](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/engram/engram_gate_kernel.py#L329-L336) —— 读 `gate_in[i_s,head_id]`、`rstd_x_in`、`rstd_k_in`、`dot_in`（注意 `dot_in_local` 取的是原始点积）。

Pass 1a：先每头两 warp 各算一部分 \(G=\sum\text{grad\_out}\cdot v\)，跨 warp 经 `dldg_smem` 归约，再套门控导数得 `dldg_r`：

[tile_kernels/engram/engram_gate_kernel.py:366-386](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/engram/engram_gate_kernel.py#L366-L386) —— `warp_reduce_sum` 后把两 warp 的结果写进 `dldg_smem[head_id,0/1]`；第 381 行 `dldg_r = dldg_smem[head_id,0]+dldg_smem[head_id,1]`；第 382-386 行就是上式门控导数（含夹断判据与 \(g(1-g)\cdot\frac12\sqrt{\cdots}\)）。

Pass 1b：`grad_v` 直写（带 token 维，每个 token 现算现写）：

[tile_kernels/engram/engram_gate_kernel.py:388-397](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/engram/engram_gate_kernel.py#L388-L397) —— \(\text{grad}_v=\sum_h\text{grad\_out}_h\cdot g_h\)。

Pass 2：流水分块同时写 `grad_x`、`grad_k`，并把 `dldg_r·x·k` 累加进 `grad_w_local`（循环体与 epilogue 各一次）：

[tile_kernels/engram/engram_gate_kernel.py:428-434](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/engram/engram_gate_kernel.py#L428-L434) —— `grad_x`/`grad_k` 的 RMSNorm 反向项，以及关键的 `grad_w_local[...] += dldg_r * x * k`（融合权重的部分梯度累加）。

Epilogue：把每块累加好的 `grad_w_local` 一次性写到本 block 的槽位：

[tile_kernels/engram/engram_gate_kernel.py:461-465](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/engram/engram_gate_kernel.py#L461-L465) —— `grad_w_partial[pid_p, head_id, ...] = grad_w_local[...]`，每个 block 写自己那一页。

wrapper 侧的细节：**反向把 `num_persistent_blocks` 直接取成 `get_num_sms()`**（不像前向走占用启发式），并把 `grad_w_partial` 按 `num_sms` 维分配：

[tile_kernels/engram/engram_gate_kernel.py:557-564](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/engram/engram_gate_kernel.py#L557-L564) —— 第 557 行 `get_engram_gate_bwd_kernel(..., get_num_sms(), ...)`；第 564 行 `grad_w_partial = torch.empty((get_num_sms(), hc_mult, hidden_size), ...)`。

> **设计取舍**：之所以不用全局原子把所有块的 `grad_w` 直接累加到一个 \((h,H)\) 缓冲，是因为持久化 block 数随 SM 数（上百个）变化，大量 block 竞争同一组地址的原子加会严重拖慢。改成「每块写自己一页 → 再用一个轻量 kernel 跨页求和」，把竞争从反向主 kernel 里剥离出来，是经典的 split-K 风格拆分。

#### 4.2.4 代码实践

**实践目标**：验证「激活梯度直写、参数梯度分块」的分工——确认 `grad_x/grad_k/grad_v` 形状带 token 维且无需 `.sum(0)`，而 `grad_w_partial` 必须跨块求和。

**操作步骤**：

1. 阅读 [tests/engram/test_engram_gate_bwd.py:57-63](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tests/engram/test_engram_gate_bwd.py#L57-L63)。注意第 61 行 `grad_w_fused = grad_w_partial.sum(0)`，而 `grad_x/grad_k/grad_v` 直接拿来和参考比。
2. 临时把第 61 行改成 `grad_w_fused = grad_w_partial[0]`（只取第一块），运行 `pytest tests/engram/test_engram_gate_bwd.py -n 4`。
3. （可选）设 `TK_PRINT_KERNEL_SOURCE=1` 打印反向 kernel 的 CUDA 源码，定位 `grad_w_partial` 的写出语句。

**需要观察的现象**：步骤 2 改动后 `grad_wh/grad_we` 的 `calc_diff` 远超 `1e-8`（因为丢掉了其余 block 的贡献），而 `grad_x/grad_k/grad_v` 仍接近 0。

**预期结果**：改回 `.sum(0)` 后全部通过；这直接说明只有 `grad_w` 需要跨块归约。

#### 4.2.5 小练习与答案

**练习 1**：signed-sqrt \(\operatorname{copysign}(\sqrt{|d|},d)\) 对 \(d\) 的导数在 \(d>0\) 与 \(d<0\) 时分别是多少？为何 kernel 里 `dldg_r` 不带 `sign(raw_dot)`？

**答案**：两种情况下导数都是 \(\tfrac{1}{2}/\sqrt{|d|}\)（正）。因为 \(d>0\) 时导数为 \(\tfrac{1}{2\sqrt{d}}\)，\(d<0\) 时 signed-sqrt \(=-\sqrt{-d}\)，对 \(d\) 求导得 \(-\tfrac{1}{2\sqrt{-d}}\cdot(-1)=\tfrac{1}{2\sqrt{|d|}}\)。符号在求导中抵消，所以 `dldg_r` 无需显式 `sign`。

**练习 2**：为什么 `grad_v` 可以在 Pass 1b「现算现写」，而 `grad_w` 必须用寄存器跨 token 累加、最后统一写出？

**答案**：`grad_v` 带 token 维，每个 token 的贡献彼此独立、写到不同地址，无需累加；`grad_w` 是参数梯度（无 token 维），同一 \((h,H)\) 元素要累加本 block 内所有 token 的贡献，只能先用寄存器累加再写一次，避免对同一地址的高频原子写。

---

### 4.3 grad_w_reduce：跨块归约 + 融合拆分 + 就地累加

#### 4.3.1 概念说明

`grad_w_partial` 只是「半成品」：它是融合权重 \(w=w_h\odot w_e\) 的梯度，且按持久化块切成了 `num_persistent_blocks` 页。要得到训练真正需要的 \(w_h\)、\(w_e\) 各自梯度，还要做三件事：

1. **跨块求和**：\(\text{grad\_w\_fused}=\sum_{\text{blocks}}\text{grad\_w\_partial}\)。
2. **链式拆分**：由 \(\text{raw\_dot}=\sum x\odot(w_h\odot w_e)\odot k\)，得 \(\partial L/\partial w_h=\text{grad\_w\_fused}\odot w_e\)、\(\partial L/\partial w_e=\text{grad\_w\_fused}\odot w_h\)。
3. **就地累加**：把结果 \(+\!=\) 进既有的 fp32 梯度缓冲（而不是覆盖），以支持梯度累积 / `main_grad`。

`grad_w_reduce` 把这三步融进同一个 kernel，省掉两次中间 HBM 往返。

#### 4.3.2 核心流程

```
grid (hc_mult, num_tiles=H/512), threads=128
每 block 处理 (一头, 一段 512 元的 hidden 切片)：
  1. 把该切片的 wh/we/grad_wh/grad_we 各 load 进 fragment
  2. 把 grad_w_partial[:, head, 切片] 按 num_batches=4 分批进 SMEM，
     串行累加进 grad_w_fragment  →  得到该切片的 grad_w_fused
  3. grad_wh += grad_w_fused · we ;  grad_we += grad_w_fused · wh
  4. 把 grad_wh/grad_we 写回（覆盖原缓冲位置，因为是 += 的结果）
```

数学上：

\[
\text{grad\_wh}_d \mathrel{+}= \left(\sum_{b}\text{grad\_w\_partial}[b,h,d]\right)\cdot w_{e,d},\qquad
\text{grad\_we}_d \mathrel{+}= \left(\sum_{b}\text{grad\_w\_partial}[b,h,d]\right)\cdot w_{h,d}
\]

> 为什么用 `+=`？因为 `grad_wh/grad_we` 是「已有内容」的梯度缓冲（可能已累加了前一个 microbatch 或前一层通过 `main_grad` 共享的梯度）。kernel 先 `T.copy` 把它读进 fragment，做完 `+=` 再写回，等价于就地累加。

#### 4.3.3 源码精读

kernel 构造函数：`num_persistent_blocks` 由 `grad_w_partial.shape[0]` 决定（即反向里取的 `num_sms`），并要求它能被 `num_batches=4` 整除：

[tile_kernels/engram/engram_grad_w_reduce_kernel.py:14-26](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/engram/engram_grad_w_reduce_kernel.py#L14-L26) —— `blk_d=512`、`num_batches=4`、`num_rows=num_persistent_blocks//num_batches`。

跨块求和：把 `num_persistent_blocks` 维分成 4 批进 SMEM，串行累加进 `grad_w_fragment`，并用 `Pipelined(num_stages=2)` 隐藏加载延迟：

[tile_kernels/engram/engram_grad_w_reduce_kernel.py:50-55](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/engram/engram_grad_w_reduce_kernel.py#L50-L55) —— 分批 `T.copy` 进 `grad_w_shared`，`Serial(num_rows)` 内 `Parallel(blk_d)` 累加 `grad_w_fragment[j] += grad_w_shared[i,j]`。

融合拆分 + 就地累加（注意第 47-48 行先把 `grad_weight_hidden/embed` 读进 fragment，所以这里的 `+=` 是在「旧值」上累加）：

[tile_kernels/engram/engram_grad_w_reduce_kernel.py:45-62](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/engram/engram_grad_w_reduce_kernel.py#L45-L62) —— 第 47-48 行 load 既有 `grad_wh/grad_we`；第 57-59 行 `grad_wh_fragment += grad_w_fragment·we`、`grad_we_fragment += grad_w_fragment·wh`；第 61-62 行写回。

测试里的参考实现把这三步写成一目了然的纯 PyTorch，是对拍依据：

[tests/engram/test_engram_grad_w_reduce.py:15-19](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tests/engram/test_engram_grad_w_reduce.py#L15-L19) —— `grad_w_sum = grad_w_partial.sum(0)`；`grad_weight_hidden += grad_w_sum · weight_embed`；`grad_weight_embed += grad_w_sum · weight_hidden`，与 kernel 完全等价。

#### 4.3.4 代码实践

**实践目标**：确认 `grad_w_reduce` 与「`.sum(0)` + 拆分 + `+=`」的朴素参考位精确一致，并理解 `+=` 的累加语义。

**操作步骤**：

1. 阅读 [tests/engram/test_engram_grad_w_reduce.py:44-54](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tests/engram/test_engram_grad_w_reduce.py#L44-L54)。注意参考与被测**用同一份初始化好的 `grad_wh_ref/grad_we_ref`**（第 45-48 行 clone），验证 `+=` 而非覆盖。
2. 运行 `pytest tests/engram/test_engram_grad_w_reduce.py -n 4`。
3. 把 `grad_weight_hidden` 初始值改成非零随机（已经是这样），观察 kernel 输出 = 初始值 + `grad_w_sum·we`。

**需要观察的现象**：`calc_diff < 1e-10`；若误把 kernel 的 `+=` 改成 `=`（覆盖），diff 会立刻约等于「初始缓冲值」本身。

**预期结果**：测试通过，证明 kernel 实现的是就地累加。

#### 4.3.5 小练习与答案

**练习 1**：如果 `num_persistent_blocks` 不能被 `num_batches=4` 整除，kernel 会怎样？

**答案**：第 25 行 `assert num_persistent_blocks % num_batches == 0` 会触发断言错误。实际 SM 数（Hopper 132、Blackwell 148）都能被 4 整除，所以反向把 `num_persistent_blocks` 取成 `num_sms` 是安全的。

**练习 2**：把拆分公式 \(\partial L/\partial w_h=\text{grad\_w\_fused}\odot w_e\) 推导出来。

**答案**：\(\text{raw\_dot}=\sum_d x_d(w_{h,d}w_{e,d})k_d\)，故 \(\partial\text{raw\_dot}/\partial w_{h,d}=x_d w_{e,d} k_d\)；又 \(\text{grad\_w\_fused}_d=\partial L/\partial w_d=\partial L/\partial\text{raw\_dot}\cdot x_d k_d\)，链式得 \(\partial L/\partial w_{h,d}=\partial L/\partial\text{raw\_dot}\cdot x_d k_d\cdot w_{e,d}=\text{grad\_w\_fused}_d\cdot w_{e,d}\)。

---

### 4.4 main_grad fp32 缓冲与返回 None 的优化

#### 4.4.1 概念说明

`grad_w_reduce` 产出的是 fp32 的 `grad_wh/grad_we`。在单卡训练里，直接把它作为参数的 `.grad` 返回即可。但在大模型分布式训练（Megatron / TransformerEngine 等）里，参数常带一个 fp32 的 `main_grad` 缓冲，用于：

- 跨 microbatch 的**梯度累积**；
- 与反向计算**重叠**的通信（all-reduce / reduce-scatter）。

如果 autograd 还单独返回一个临时 `.grad` 张量，框架随后得再把它 `add` 进 `main_grad`，既多一份显存、又多一次 kernel。engram 的做法是：检测参数有没有 `main_grad`，有就直接把 `main_grad` 当作 `grad_w_reduce` 的就地累加目标，然后对该参数返回 `None`——告诉 autograd「这个参数的梯度我已经处理好了，不要再分配 `.grad`」。

#### 4.4.2 核心流程

```
backward(grad_output):
  grad_x, grad_k, grad_v, grad_w_partial = engram_gate_bwd(...)   # 反向主 kernel
  main_grad_wh = getattr(weight_hidden, 'main_grad', None)
  main_grad_we = getattr(weight_embed , 'main_grad', None)
  grad_wh = main_grad_wh if main_grad_wh is not None else zeros_like(wh, fp32)
  grad_we = main_grad_we if main_grad_we is not None else zeros_like(we, fp32)
  grad_w_reduce(grad_w_partial, wh, we, grad_wh, grad_we)         # 就地累加
  return (grad_x, grad_k, grad_v,
          None if main_grad_wh else grad_wh,                       # 已累加进 main_grad
          None if main_grad_we else grad_we,
          None, None)                                              # clamp_value/eps 非张量
```

返回元组的长度 = forward 输入个数（7 个：hidden_states, k, v, weight_hidden, weight_embed, clamp_value, eps），逐位对应；末尾两个 `None` 对应两个 Python float 超参。

#### 4.4.3 源码精读

`forward` 用 `ctx.save_for_backward` 同时保存输入与四个中间量：

[tile_kernels/modeling/engram/engram_gate.py:49-52](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/modeling/engram/engram_gate.py#L49-L52) —— 保存 `x,k,v,weight_hidden,weight_embed,weight_fused,dot,gate_score,rstd_x,rstd_k`。

`backward` 的 `main_grad` 检测 + 就地累加：

[tile_kernels/modeling/engram/engram_gate.py:74-81](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/modeling/engram/engram_gate.py#L74-L81) —— `getattr(weight_hidden,'main_grad',None)`；有则直接当 `grad_wh`，无则 `torch.zeros_like(...,fp32)`；`grad_w_reduce` 在两种情况下都就地累加。

返回 `None` 的位置：

[tile_kernels/modeling/engram/engram_gate.py:85-92](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/modeling/engram/engram_gate.py#L85-L92) —— 第 89/90 行对拥有 `main_grad` 的参数返回 `None`；第 91 行两个 `None` 对应 `clamp_value`、`eps`。

#### 4.4.4 代码实践

**实践目标**：在脚本里给 `weight_hidden` 挂一个 `main_grad`，确认 `backward` 对它返回 `None`、且 `main_grad` 被就地更新。

**操作步骤**（示例代码，可在交互式 Python / 临时脚本里运行；待本地验证）：

```python
# 示例代码：演示 main_grad 路径，非项目原有
import torch
from tile_kernels.modeling.engram import engram_gate

N, h, H = 4, 4, 4096
x = torch.randn(N, h, H, dtype=torch.bfloat16, device='cuda', requires_grad=True)
k = torch.randn(N, h, H, dtype=torch.bfloat16, device='cuda', requires_grad=True)
v = torch.randn(N, H, dtype=torch.bfloat16, device='cuda', requires_grad=True)
wh = torch.randn(h, H, dtype=torch.bfloat16, device='cuda')
we = torch.randn(h, H, dtype=torch.bfloat16, device='cuda')

# 关键：给 wh 挂一个 main_grad 缓冲
wh.main_grad = torch.zeros_like(wh, dtype=torch.float32)

out = engram_gate(x, k, v, wh, we, clamp_value=1e-6, eps=1e-20)
out.sum().backward()

print("wh.main_grad nonzero:", wh.main_grad.abs().sum().item() > 0)
print("wh.grad:", wh.grad)   # 预期为 None（已就地累加进 main_grad）
```

**需要观察的现象**：`wh.main_grad` 变成非零（被 `grad_w_reduce` 就地累加）；`wh.grad is None`。`we` 没有 `main_grad`，所以 `we.grad` 是一个正常的 fp32 张量。

**预期结果**：与上述一致——这正是「返回 None = 梯度已就地处理」的可观测表现。

#### 4.4.5 小练习与答案

**练习 1**：如果 `backward` 对拥有 `main_grad` 的参数**不**返回 `None`，而是返回累加后的 `grad_wh`，会发生什么？

**答案**：autograd 会把 `grad_wh` 赋给 `wh.grad`，于是同一份梯度既在 `wh.main_grad` 里（被 `grad_w_reduce` 累加）、又在 `wh.grad` 里，后续框架若再把 `wh.grad` 加进 `main_grad` 就会**重复累加**，梯度翻倍。返回 `None` 正是为了避免这种双计。

**练习 2**：返回元组为什么必须是 7 个元素、且最后两个是 `None`？

**答案**：`torch.autograd.Function` 要求 `backward` 返回值与 `forward` 输入参数**逐位对应**。`forward(ctx, hidden_states, k, v, weight_hidden, weight_embed, clamp_value, eps)` 共 7 个输入，所以返回 7 个；`clamp_value`、`eps` 是 Python float（无梯度），对应位必须是 `None`。

---

## 5. 综合实践

把本讲四节串成一个端到端的「反向链路追踪」任务。

**任务**：用 `engram_gate_ref`（支持 autograd）当标准答案，手动复现一次「前向存盘 → 反向主 kernel → 权重归约」的全链路，并验证每一步数值对齐。

**步骤**：

1. 按 [tests/engram/test_engram_gate_bwd.py:15-28](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tests/engram/test_engram_gate_bwd.py#L15-L28) 造一组 `(x,k,v,wh,we,grad_out)`，`weight_fused = wh.float()*we.float()`。
2. 调 `engram_gate_ref(..., save_for_backward=True)` 拿 `(o, dot, gate_score, rstd_x, rstd_k)`，并令参考走 `o.backward(grad_out)` 得 `x.grad/k.grad/v.grad/wh.grad/we.grad`。
3. 调 `engram_gate_bwd(grad_out, x, k, v, weight_fused, dot, gate_score, rstd_x, rstd_k, clamp_value)` 得 `(grad_x, grad_k, grad_v, grad_w_partial)`，验证前三者与参考的 `calc_diff < 1e-8`。
4. 调 `grad_w_reduce(grad_w_partial, wh, we, grad_wh=zeros(...), grad_we=zeros(...))`，验证 `grad_wh/grad_we` 与参考的 `wh.grad/we.grad` 对齐（参考 test_engram_gate_bwd.py 第 61-63 行的 `.sum(0)` + 拆分，理解 `grad_w_reduce` 把这三步合一）。
5. 最后给 `wh` 挂一个 `main_grad`（4.4.4 的示例），重新走一遍 `EngramGateFn.apply(...).sum().backward()`，确认 `wh.grad is None` 且 `main_grad` 被更新。

**交付**：一张表，列出「前向存盘量 → 反向用途 → 对应源码行 → 与参考对齐的 `calc_diff`」，并写一段话解释「为什么只有 `grad_w` 需要二次归约、而 `grad_x/grad_k/grad_v` 不需要」。

> 若无 GPU 环境，可退化为「源码阅读型实践」：只读地完成第 1-2 步的链路对照与表格填写，并在每处标注 `calc_diff` 的**预期量级**（前三者 `< 1e-8`，权重 `< 1e-10`）。

## 6. 本讲小结

- 前向存盘四个 fp32 中间量：`dot`（**未归一化**的原始点积 raw_dot）、`gate_score`（最终门控值 \(g\)）、`rstd_x`、`rstd_k`；它们分别支撑反向的门控导数、RMSNorm 反向、`grad_v` 与门控导数因子。
- 门控对 raw_dot 的导数 `dldg_r` 形如 \(G\cdot g(1-g)\cdot\tfrac12\sqrt{H^{-1/2}\text{rstd}_x\text{rstd}_k/|\text{raw\_dot}|}\)，signed-sqrt 的符号在求导中抵消，故无需显式 `sign`。
- 反向 kernel 三段式：算 \(G\) → 写 `grad_v` → 流水写 `grad_x/grad_k` 并累加 `grad_w_local`。其中 `grad_x/grad_k/grad_v` 带 token 维、各 block 直写；只有 `grad_w`（参数梯度）按块写成 `grad_w_partial`，需二次归约。
- `grad_w_reduce` 一口做三事：跨 `num_persistent_blocks` 求和、按链式拆成 `grad_wh/grad_we`、就地把结果 `+=` 进既有 fp32 缓冲。
- `main_grad` 优化：参数带 `main_grad` 时直接当作就地累加目标、并对其返回 `None`，避免临时 `.grad` 与重复累加；返回元组逐位对应 forward 的 7 个输入。

## 7. 下一步学习建议

- **横向对比另一条「手写反向」链路**：阅读 [tile_kernels/mhc/sinkhorn_kernel.py](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/mhc/sinkhorn_kernel.py) 与 [tile_kernels/modeling/mhc/ops/sinkhorn.py](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/modeling/mhc/ops/sinkhorn.py)（u7-l3），体会「迭代算法必须保存全部中间量再逆序回传」与 engram「保存少量标量中间量」两种反向风格的差异。
- **深化 autograd.Function 范式**：进入 u8-l1，把本讲的 `EngramGateFn` 作为模板，学习 `save_for_backward` 的选择原则与 `main_grad`/返回 `None` 这类「与训练框架对接」的工程惯例。
- **硬件感知视角**：结合 u10-l1，用 `set_num_sms` 改变可用 SM 数，观察反向里 `num_persistent_blocks=num_sms` 与 `grad_w_reduce` 的 `num_batches=4` 约束如何随之变化（注意保持 `num_sms % 4 == 0`）。
