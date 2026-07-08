# FlashAttention-2 反向算法与 Delta 预处理

> 本讲是 **Triton 后端反向（u5）** 的第一篇，承接 u4-l1 的「Triton 前向 online softmax 主循环」。
> 本讲只讲一件事：**反向传播为什么要先跑一个独立的 delta 预处理 kernel**，以及它是如何用 FlashAttention-2（后简称 FA-2）的反向技巧把一次「昂贵的逐行 KV 归约」变成「廉价的逐行 D 归约」的。

## 1. 本讲目标

学完本讲你应该能够：

1. 用链式法则推导出注意力反向的 `dQ / dK / dV` 公式，并指出其中那个反复出现的「逐行修正项」`Di`。
2. 解释为什么 `Di` 可以等价地写成 `rowsum(dO * O)`，从而只需 `O` 和 `dO` 就能预算出来，无需重跑 softmax。
3. 读懂 FFPA 反向的**两阶段结构**：先 `_ffpa_bwd_pre_impl` 算 delta，再让主反向 kernel 把 delta 当作每行一个标量查表使用。
4. 说清楚反向为什么必须从前向**保存 `O` 与 `LSE`**。
5. 理解 `preprocess_d_chunk` 选项：在「整 D 一次处理」和「D 维分块处理」之间取舍。

## 2. 前置知识

- **注意力前向**（u4-l1）：\(\[ S=\tau\,QK^{\!\top},\quad P=\mathrm{softmax}_{row}(S),\quad O=PV \]\)，其中 \(\tau\) 是 `softmax_scale`（默认 \(1/\sqrt D\)）。前向用 online softmax 逐块算出每行的 log-sum-exp `LSE`。
- **反向传播（autograd）**：已知上游梯度 \(dO\)（损失对输出的梯度），要倒推对输入 \(Q,K,V\) 的梯度 \(dQ,dK,dV\)。
- **Triton 基础**（u4-l1）：`tl.dot` 做矩阵乘、`tl.load/tl.store` 读写全局显存、`tl.constexpr` 是编译期常量、`program_id` 划分网格。
- **Split-D**（u4-l2）：head_dim `D` 在 `QK^T` 里是归约维（可拆分累加），在 `PV` 里是输出维（只能各存各的）。
- **MMA / 寄存器压力**（u1-l1）：FFPA 主攻大 D，单个 program 装不下整 D，需要分块。

> 本讲不重复前向 online softmax 的细节，只在其结论上推导反向。

## 3. 本讲源码地图

本讲几乎全部来自一个文件：

| 文件 | 作用 |
| --- | --- |
| [`src/ffpa_attn/triton/_ffpa_bwd.py`](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_ffpa_bwd.py) | Triton 反向全部实现：delta 预处理 kernel、dK/dV/dQ 主 kernel、decode 反向、以及把它们串起来的启动器 `_ffpa_attn_backward_triton_impl` |

辅助引用：

| 文件 | 作用 |
| --- | --- |
| [`src/ffpa_attn/functional.py`](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py) | `TritonBackend` 配置类，`preprocess_d_chunk` 在此暴露 |
| [`src/ffpa_attn/triton/__init__.py`](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/__init__.py) | 把反向注册为 `torch.ops.ffpa_attn._bwd_triton` 自定义算子 |
| [`tests/test_ffpa_bwd.py`](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/tests/test_ffpa_bwd.py) | 反向正确性测试，含直接验证 delta 恒等式的用例 |
| [`docs/index.md`](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/docs/index.md) | 反向用法示例 |

## 4. 核心概念与源码讲解

### 4.1 反向传播要算什么：dQ / dK / dV 的链式法则

#### 4.1.1 概念说明

前向是三步复合函数：

\[
S=\tau\,QK^{\!\top}\;\xrightarrow{\mathrm{softmax}}\;P\;\xrightarrow{\times V}\;O
\]

反向已知 \(dO\)，沿着 \(O\to P\to S\to Q,K\) 倒推。矩阵乘对转置的方向很关键：

- \(O=PV\) 对 \(V\) 求导 → \(dV=P^{\!\top}dO\)
- \(O=PV\) 对 \(P\) 求导 → \(dP=dO\,V^{\!\top}\)
- \(P=\mathrm{softmax}(S)\) 对 \(S\) 求导 → \(dS=P\odot\bigl(dP-\mathrm{Di}\bigr)\)，其中 \(\mathrm{Di}\) 是每行一个标量
- \(S=\tau\,QK^{\!\top}\) 对 \(Q,K\) 求导 → \(dQ=\tau\,dS\,K\)、\(dK=\tau\,dS^{\!\top}Q\)

难点全在那个 \(\mathrm{Di}\)。它是 softmax 求导时**「整行所有列互相牵连」**产生的耦合项。FA-2 反向的全部精髓，就是怎么把这个 \(\mathrm{Di}\) 算得便宜。

#### 4.1.2 核心流程

把 \(\mathrm{Di}\) 写成显式形式。softmax 的雅可比为：

\[
\frac{\partial P_{ij}}{\partial S_{ik}}=P_{ij}(\delta_{jk}-P_{ik})
\]

于是

\[
dS_{ij}=\sum_k\frac{\partial P_{ij}}{\partial S_{ik}}\,dP_{ik}
       =P_{ij}\,dP_{ij}-P_{ij}\underbrace{\sum_k P_{ik}\,dP_{ik}}_{\displaystyle\mathrm{Di}_i}
\]

即

\[
\boxed{\,dS_{ij}=P_{ij}\bigl(dP_{ij}-\mathrm{Di}_i\bigr),\qquad
       \mathrm{Di}_i=\sum_k P_{ik}\,dP_{ik}\,}
\]

主反向 kernel 在每个 score 片段上的工作就是：**重建 \(S\) → 重建 \(P\) → 拿到 \(dP\) → 查出本行 \(\mathrm{Di}\) → 算 \(dS\) → 累加 \(dK/dV/dQ\)**。注意 \(dS\) 里那个 \(\tau\)（scale）被统一乘进去，所以 \(dQ,dK\) 自然带上 scale，而 \(dV=P^{\!\top}dO\) 与 scale 无关（因为 \(O=PV\) 不经过 \(S\)）。

#### 4.1.3 源码精读

dK/dV 主 kernel `_ffpa_bwd_dkdv` 在每个 (Q 块, K 块) 上恰好实现了上面四步。先重建 score 和 \(dP\)（Split-D 把 D 切片累加）：

[src/ffpa_attn/triton/_ffpa_bwd.py:708-709](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_ffpa_bwd.py#L708-L709) — `S = tl.dot(q, tl.trans(k), acc=S)` 累加 \(QK^{\!\top}\)，`dP = tl.dot(do, tl.trans(v), acc=dP)` 算 \(dO\,V^{\!\top}=dP\)。

接着用前向保存的 LSE 还原 \(P\)（无需重跑 online softmax），并查表取出本行 delta：

[src/ffpa_attn/triton/_ffpa_bwd.py:729](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_ffpa_bwd.py#L729) — `P = tl.exp(S - lse_i[:, None])`，每个 score 减去该行 LSE 再取指数，等价于除以行和。

[src/ffpa_attn/triton/_ffpa_bwd.py:744](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_ffpa_bwd.py#L744) — `Di = tl.load(D + offs_qm)`，这里的张量 `D` 就是预算好的 delta，**每行一个标量**。

最后套公式算 \(dS\) 并分块累加 \(dK,dV\):

[src/ffpa_attn/triton/_ffpa_bwd.py:777](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_ffpa_bwd.py#L777) — `dS = (P * (dP - Di[:, None]) * softmax_scale).to(DTYPE)`，正是 \(dS_{ij}=P_{ij}(dP_{ij}-\mathrm{Di}_i)\tau\)，scale 在此乘入。

[src/ffpa_attn/triton/_ffpa_bwd.py:803-807](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_ffpa_bwd.py#L803-L807) — `dk_d = tl.trans(tl.dot(tl.trans(q), dS, ...))` 即 \(dK=dS^{\!\top}Q\)；`dv_d = tl.trans(tl.dot(tl.trans(do), P_drop.to(DTYPE), ...))` 即 \(dV=P^{\!\top}dO\)（无 scale）。

dQ 在另一个 kernel `_ffpa_bwd_dq` 里：

[src/ffpa_attn/triton/_ffpa_bwd.py:1292](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_ffpa_bwd.py#L1292) — `dq_d = tl.dot(dS_qk, k, out_dtype=tl.float32)`，即 \(dQ=dS\,K\)（dS 已含 scale）。

> 注意：主反向 kernel 把 `delta` 这个张量形参命名为 `D`（见 [`_ffpa_bwd.py:588`](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_ffpa_bwd.py#L588)），与「head_dim」不是一回事——它是「softmax 的逐行修正项」。

#### 4.1.4 代码实践

**实践目标**：在源码里逐一对应出四个梯度公式。

**操作步骤**：

1. 打开 `src/ffpa_attn/triton/_ffpa_bwd.py`，定位 `_ffpa_bwd_dkdv`（约 591 行起）。
2. 在 Phase 1 的 D 分块循环里找到 `S` 与 `dP` 的两个 `tl.dot`（708-709 行）。
3. 找到 `P = tl.exp(S - lse_i[:, None])`（729 行）和 `Di = tl.load(D + offs_qm)`（744 行）。
4. 找到 `dS`（777 行）和 `dk_d`/`dv_d`（803-807 行）。

**需要观察的现象**：`dV` 用的是 `P_drop`（即 \(P\)），而 `dK` 用的是 `dS`；说明 `dV` 与 scale 无关、`dK` 与 scale 有关。

**预期结果**：你能把 `dV = P^T dO`、`dK = dS^T Q`、`dS = P*(dP-Di)*scale`、`dP = dO V^T` 四式与代码一一对应。这一步是纯阅读，不依赖 GPU。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `dV = P^T dO` 里没有 `softmax_scale`，而 `dK` 里有？

> **答**：`scale` 只出现在 \(S=\tau QK^{\!\top}\) 这一步。`dV` 源自 \(O=PV\)，路径 \(O\to P\to V\) 不经过 \(S\)，故无 scale；`dK` 源自 \(S\)，链式法则带回 \(\tau\)。

**练习 2**：若 `is_causal=True`，`dV` 还会累加被 mask 成 \(-\infty\) 的列吗？

> **答**：不会。kernel 先用 `tl.where(... < seqlen_k, S, -inf)` 和 causal 掩码把非法列置 \(-\infty\)，再做 `P=exp(S-lse)`，于是非法列 \(P=0\)，对 \(dV=P^{\!\top}dO\) 与 \(dS\) 贡献都为 0。

---

### 4.2 Delta 的关键恒等式：rowsum(dO * O)

#### 4.2.1 概念说明

4.1 节留下一个问题：\(\mathrm{Di}_i=\sum_k P_{ik}\,dP_{ik}\) 怎么算？朴素做法是在主 kernel 里**沿整条 KV 序列做一次逐行归约**——但这与「分块、每块只见一个 KV 片段」的设计冲突，且要为每个 Q 行额外扫一遍全部 key/value。

FA-2 的妙招是一个代数恒等式，把这次「沿 KV 归约」换成「沿 D 归约」：

\[
\mathrm{Di}_i=\sum_k P_{ik}\,dP_{ik}
=\sum_k P_{ik}\sum_d dO_{id}\,V_{kd}
=\sum_d dO_{id}\underbrace{\Bigl(\sum_k P_{ik}V_{kd}\Bigr)}_{O_{id}}
=\sum_d dO_{id}\,O_{id}
\]

即

\[
\boxed{\,\mathrm{Di}_i=\mathrm{rowsum}_d\bigl(dO_i\odot O_i\bigr)\,}
\]

关键点：右端**只用到 \(dO\) 和 \(O\)**，二者形状都是 `[B,Nh,Nq,D]`，与 KV 序列长度、与主 kernel 的分块循环**完全无关**。于是 delta 可以用一个极便宜的独立 kernel 一次性算完，主 kernel 只需逐行查表。

这正是文件头注释里那句总结：

[src/ffpa_attn/triton/_ffpa_bwd.py:79](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_ffpa_bwd.py#L79) — `delta = rowsum(dO * O)` 预算一次，两条反向路径（主路径与 decode 路径）都复用它。

#### 4.2.2 核心流程

恒等式的成立依赖于「整行 softmax 概率和为 1」与「\(O=PV\) 的定义」。直觉上：\(\sum_k P_{ik}dP_{ik}\) 是「按注意力权重 \(P\) 加权 \(dP\) 的行和」，而 \(dP=dO\,V^{\!\top}\) 把 \(dO\) 投影回 key 空间，再用 \(P\) 加权求和恰好还原成 \(dO\) 与 \(O\) 在 D 维的点积。一次 \(O(N_q\cdot D)\) 的逐元素乘加，顶替了 \(O(N_q\cdot N_{kv})\) 的归约。

#### 4.2.3 源码精读

预处理 kernel `_ffpa_bwd_pre_impl` 直接实现了这个内积：

[src/ffpa_attn/triton/_ffpa_bwd.py:286-299](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_ffpa_bwd.py#L286-L299) — full-D 模式：把整行 `O`、`dO` 读进寄存器（升 fp32），`delta = tl.sum(o * do, axis=1)`，正是 `rowsum(dO * O)`。

它写出的 `delta` 形状是每行一个标量，布局与 `lse` 一致，随后被主 kernel 当作「逐行修正项 `D`」按行读取（见 4.1.3）。

测试用 PyTorch 直接核对这个恒等式：

[tests/test_ffpa_bwd.py:418](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/tests/test_ffpa_bwd.py#L418) — `ref = (o.float() * do.float()).sum(dim=-1)`，把 kernel 输出 `delta_full`/`delta_d_chunk` 与这条参考逐元素比对（432 行断言三种结果一致）。

#### 4.2.4 代码实践

**实践目标**：用一个最小 CPU/GPU 脚本验证恒等式两端的相等性（不依赖 FFPA 反向，纯数学验证）。

**操作步骤**（示例代码，可运行）：

```python
# 示例代码：验证 Di = rowsum(dO * O) == sum_k P * dP
import torch
torch.manual_seed(0)
Nq, Nk, D = 4, 7, 16
tau = 0.25
Q = torch.randn(1, 1, Nq, D); K = torch.randn(1, 1, Nq, Nk, D)  # 仅用于演示
# 为简化，直接给定 P（行和为1）和 dO
S = torch.randn(Nq, Nk); P = torch.softmax(S, dim=-1)
V = torch.randn(Nk, D); dO = torch.randn(Nq, D)
O = P @ V                      # 前向输出
dP = dO @ V.T                  # dP = dO V^T
lhs = (P * dP).sum(dim=-1)     # 朴素：sum_k P_ik dP_ik
rhs = (dO * O).sum(dim=-1)     # 恒等式：rowsum(dO * O)
print("max abs diff:", (lhs - rhs).abs().max().item())
```

**需要观察的现象**：两端 `max abs diff` 应为 0（浮点误差量级）。

**预期结果**：差值在 ~1e-6 量级（fp32），证明恒等式成立。此脚本无需 GPU，可在 CPU 上跑通。

#### 4.2.5 小练习与答案

**练习 1**：若把 `O` 换成「未归一化的 \(PV\)」（即 \(P\) 行和不为 1），恒等式还成立吗？

> **答**：不成立。推导里 \(\sum_k P_{ik}V_{kd}=O_{id}\) 用到了「\(P\) 就是 softmax 输出且 \(O=PV\)」这一定义；若 \(P\) 行和不为 1，则 \(\sum_k P_{ik}\) 不归一，\(O\) 也不再等于该加权求和，恒等式断裂。

**练习 2**：为什么用 `rowsum(dO*O)` 而不是 `rowsum(P*dP)` 来预算？

> **答**：`rowsum(P*dP)` 需要遍历整条 KV（\(N_{kv}\) 维）且依赖 softmax 的 \(P\)，必须等主 kernel 重建完 \(P\) 才能算；而 `rowsum(dO*O)` 只在 D 维归约、只用前向已存的 \(O\) 和上游 \(dO\)，与 KV 长度无关，可独立、廉价地预先算好。

---

### 4.3 两阶段结构：preprocess + main kernel

#### 4.3.1 概念说明

有了 4.2 的恒等式，FFPA 反向自然分成两阶段：

1. **预处理阶段（cheap）**：`_ffpa_bwd_pre_impl` 对每个 `(batch, head, query 行块)` 算 `delta[row] = rowsum(dO*O)`，只动 D 维，开销 \(O(N_q\cdot D)\)。
2. **主反向阶段（heavy）**：dK/dV/dQ kernel 重建每个 score 片段，把 delta 当「每行一个标量」查表，套 \(dS=P(dP-\mathrm{Di})\tau\) 后累加梯度。

之所以分两阶段，是因为主 kernel 设计成「分块、每个 program 只见一个 KV 片段」以省显存；若让它在内部现算 \(\sum_k P_{ik}dP_{ik}\)，就得跨所有 KV 片段做归约，破坏分块、重算 softmax。恒等式把这个跨片段归约提前到一个独立的小 kernel 里解决，主 kernel 保持纯局部。

同时这也回答了「反向为何必须保存 `O` 和 `LSE`」：

- **保存 `O`**：delta 恒等式需要 `O`；不存就得反向时重算 \(O=PV\)（多一遍全量扫 KV）。
- **保存 `LSE`**：主 kernel 重建 \(P=\exp(S-\mathrm{lse})\) 时需要每行的 log-sum-exp 当归一化常数；不存就得重跑 online softmax。

#### 4.3.2 核心流程

启动器 `_ffpa_attn_backward_triton_impl` 的执行骨架（伪代码）：

```text
1. 准备 lse 行内布局、GQA 的 K/V 头展开、dq/dk/dv 缓冲（由上层 _ffpa_attn_backward_triton 完成）
2. delta = empty_like(lse)
3. 运行 _ffpa_bwd_pre_impl，写入 delta          # 预处理阶段
4. if seqlen_q < 8:
       走 decode 反向（stage1 + reduce）          # delta 同样复用
   else:
       走主反向（dKdV + dQ，或融合 dKdVdQ）         # 主阶段：按行查表用 delta
```

#### 4.3.3 源码精读

`delta` 的分配与预处理 kernel 的调用在启动器前段：

[src/ffpa_attn/triton/_ffpa_bwd.py:2247](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_ffpa_bwd.py#L2247) — `delta = torch.empty_like(lse)`，与 LSE 同布局（末维按 128 向上取整，便于带 mask 的 Triton 读取）。

[src/ffpa_attn/triton/_ffpa_bwd.py:2249-2296](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_ffpa_bwd.py#L2249-L2296) — 分 autotune / 持久化配置 / 默认 config 三条路径启动 `_ffpa_bwd_pre`，网格为 `(cdiv(seqlen_q, BLOCK_M), batch*nheads)`，即按 Q 行块 × (batch×heads) 二维并行。

随后按 `seqlen_q` 分流：短 query 走 decode（`seqlen_q < 8`），否则主路径：

[src/ffpa_attn/triton/_ffpa_bwd.py:2302](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_ffpa_bwd.py#L2302) — `if seqlen_q < 8:` decode 反向分支入口。

[src/ffpa_attn/triton/_ffpa_bwd.py:2471-2788](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_ffpa_bwd.py#L2471-L2788) — 主路径：把 `delta` 作为 `D` 张量随 `dq_args`/`dkdv_args`/`main_args` 传进主 kernel，主 kernel 内部按行 `tl.load(D + offs_qm)` 查表（见 4.1.3）。

> 两阶段的「阶段」体现在「先 delta、后主 kernel」两个独立的 kernel launch；delta 在主 kernel 里只读不写，是纯输入。

#### 4.3.4 代码实践

**实践目标**：复现 `docs/index.md` 的反向示例，把 FFPA 的 `dQ/dK/dV` 与原生 SDPA 对比。

**操作步骤**（来自 [`docs/index.md:169-200`](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/docs/index.md#L169-L200)）：

```python
import math, torch
import torch.nn.functional as F
from ffpa_attn import ffpa_attn_func

B, H, N, D = 1, 32, 8192, 512            # 大 head_dim 场景，FFPA 的主场
scale = 1.0 / math.sqrt(D)

q = torch.randn(B, H, N, D, dtype=torch.bfloat16, device="cuda", requires_grad=True)
k = torch.randn(B, H, N, D, dtype=torch.bfloat16, device="cuda", requires_grad=True)
v = torch.randn(B, H, N, D, dtype=torch.bfloat16, device="cuda", requires_grad=True)

out = ffpa_attn_func(q, k, v, scale=scale)   # 默认 Triton 后端，大 D 进 FFPA
out.sum().backward()
dq, dk, dv = q.grad.detach().clone(), k.grad.detach().clone(), v.grad.detach().clone()

q_ref = q.detach().clone().requires_grad_(True)
k_ref = k.detach().clone().requires_grad_(True)
v_ref = v.detach().clone().requires_grad_(True)
F.scaled_dot_product_attention(q_ref, k_ref, v_ref, scale=scale).sum().backward()

print(f"dQ max_abs_err={(dq - q_ref.grad).abs().max().item():.4e}")
print(f"dK max_abs_err={(dk - k_ref.grad).abs().max().item():.4e}")
print(f"dV max_abs_err={(dv - v_ref.grad).abs().max().item():.4e}")
```

**需要观察的现象**：三条误差都应在 bf16 的合理容差内（通常 1e-1 量级或更小，取决于硬件）。

**预期结果**：`dV` 通常最准（不经过 scale 与 softmax 重算），`dQ/dK` 略大但仍与 SDPA 对齐。**具体数值待本地验证**（本讲未在 GPU 上实跑）。

#### 4.3.5 小练习与答案

**练习 1**：如果把 `save_for_backward` 里保存的 `O` 去掉（仅存 q/k/v/lse），反向还能算对 delta 吗？

> **答**：不能直接算对。`delta=rowsum(dO*O)` 依赖 `O`；缺 `O` 就得在反向里先用 `lse` 与 q/k/v 重算 \(P\)，再 \(O=PV\)，多一遍全量 KV 扫描，性能大幅下降——这正是 FA 系列坚持存 `O` 的原因。

**练习 2**：为什么 `delta` 用 `empty_like(lse)` 而不是 `empty_like(O)`？

> **答**：delta 是「每行一个标量」，逻辑形状 `[B,Nh,Nq]`，与 `lse` 同形；`O` 是 `[B,Nh,Nq,D]`，多一个 D 维。让 delta 与 lse 同布局，主 kernel 就能用同一套 `off_hb * seqlen_q_rounded + offs_m` 寻址同时读 LSE 和 delta。

---

### 4.4 preprocess_d_chunk 选项：大 D 的分块预处理

#### 4.4.1 概念说明

预处理 kernel 要算 `rowsum(dO*O)`，需要把「一整行的 D 维」读到寄存器做点积。当 D 很大（如 512、1024）时，整 D 装进单个 program 的寄存器会很撑。于是有两种模式：

- **full-D 模式（`D_CHUNK=False`，默认）**：`BLOCK_HEADDIM` 取 `max(64, next_power_of_2(D))`（如 D=512 → 512），一个 program 一次性读整行 D 算完 delta。简单，但寄存器占用随 D 线性增长。
- **D_CHUNK 模式（`D_CHUNK=True`，即 `preprocess_d_chunk=True`）**：`BLOCK_HEADDIM` 固定为小块（如 64），把 D 切成多个片段，逐片段读 `O`/`dO`、累加进同一个 `delta` 标量。寄存器占用与 D 无关，代价是多几次全局加载。

这与前向 Split-D 的精神一致：D 维是归约维，可自由拆分相加，结果不变。

#### 4.4.2 核心流程

D_CHUNK 模式把内积拆成片段循环（伪代码）：

```text
delta = 0
for d_chunk in range(cdiv(D, BLOCK_HEADDIM)):
    o_chunk   = load(O[..., d_chunk*BLOCK_HEADDIM : ...])   # 只读一小段 D
    do_chunk  = load(DO[..., d_chunk*BLOCK_HEADDIM : ...])
    delta    += rowsum(o_chunk * do_chunk)                  # 累加进同一个标量
```

数学上 `rowsum(dO*O)` 沿 D 求和可拆成各片段之和，故两种模式结果一致。

#### 4.4.3 源码精读

kernel 用 `if D_CHUNK:` 区分两条路径：

[src/ffpa_attn/triton/_ffpa_bwd.py:268-285](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_ffpa_bwd.py#L268-L285) — D_CHUNK 分支：`for d_chunk in range(num_d_chunks)` 循环读 D 片段，`delta += tl.sum(o * do, axis=1)` 逐段累加。

[src/ffpa_attn/triton/_ffpa_bwd.py:286-299](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_ffpa_bwd.py#L286-L299) — full-D 分支：一次性读整 D，`delta = tl.sum(o * do, axis=1)`。

`BLOCK_HEADDIM` 在 full-D 由运行期启发式决定，在 D_CHUNK 由 autotune/持久化配置给出：

[src/ffpa_attn/triton/_ffpa_bwd.py:227-233](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_ffpa_bwd.py#L227-L233) — `_FFPA_BWD_PRE_HEURISTICS`：full-D 时 `BLOCK_HEADDIM = max(64, next_power_of_2(headdim))`，D_CHUNK 时沿用调用方显式传入的值。

启动器里这个开关决定用哪种 `BLOCK_HEADDIM` 与哪组 config：

[src/ffpa_attn/triton/_ffpa_bwd.py:2274-2296](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_ffpa_bwd.py#L2274-L2296) — 非 autotune 路径：`block_headdim_delta = 64 if preprocess_d_chunk else BLOCK_HEADDIM_DELTA`，据此组装 config 并启动预处理 kernel。

对外通过 `TritonBackend` 暴露（仅反向可用）：

[src/ffpa_attn/functional.py:186](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L186) — 文档：`preprocess_d_chunk: Split the d_chunk preprocess across tiles.`

[src/ffpa_attn/functional.py:200](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L200) — 字段定义 `preprocess_d_chunk: bool = False`。

[src/ffpa_attn/functional.py:217-218](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L217-L218) — 断言：`split_launch`、`preprocess_d_chunk`、`grad_*_storage_dtype` 都是 `backward=True` 专用选项。

[src/ffpa_attn/functional.py:877](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L877) — 把 `backward_meta.preprocess_d_chunk` 透传进低层 triton 反向。

#### 4.4.4 代码实践

**实践目标**：用现成测试确认两种预处理模式产出相同的 delta、且都等于 PyTorch 参考。

**操作步骤**：

1. 阅读 [`tests/test_ffpa_bwd.py:368-405`](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/tests/test_ffpa_bwd.py#L368-L405) 的 `_run_bwd_pre`——它直接调用 `_ffpa_bwd_pre`，分别传 `D_CHUNK=False/True`。
2. 阅读 [`tests/test_ffpa_bwd.py:412-432`](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/tests/test_ffpa_bwd.py#L412-L432) 的 `test_ffpa_bwd_preprocess_full_and_d_chunk`，它对多种 `(N,D)` 断言 `delta_full ≈ ref`、`delta_d_chunk ≈ ref`、`delta_d_chunk ≈ delta_full`。
3. 在有 GPU 的环境运行：`pytest tests/test_ffpa_bwd.py::test_ffpa_bwd_preprocess_full_and_d_chunk -v`。
4. 另可跑端到端用例 [`tests/test_ffpa_bwd.py:435-458`](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/tests/test_ffpa_bwd.py#L435-L458) `test_ffpa_bwd_triton_preprocess_modes`，确认两种模式下的 `dQ/dK/dV` 都对齐 SDPA。

**需要观察的现象**：两个 parametrize id（`pre_full`、`pre_d_chunk`）都通过；`delta_d_chunk` 与 `delta_full` 逐元素接近。

**预期结果**：全部断言通过，证明分块预处理只是「换种切法算同一个内积」，不影响数值。**具体运行输出待本地验证。**

#### 4.4.5 小练习与答案

**练习 1**：`preprocess_d_chunk=True` 会改变反向的最终 `dQ/dK/dV` 吗？

> **答**：不会。它只改变 delta 的**计算方式**（整 D vs 分块累加），而 `rowsum(dO*O)` 沿 D 可拆分相加，两种方式得到（数值上几乎相同的）同一个 delta，主 kernel 随后行为完全一致。

**练习 2**：什么时候该开 `preprocess_d_chunk=True`？

> **答**：当 D 很大（如 1024）、full-D 模式让单个 program 寄存器占用过高、影响占用率或触发寄存器溢出时。对 D 较小（如 320）且整 D 能舒服装下，full-D 模式更省全局加载。FFPA 默认 `False`，必要时由 `TritonBackend(preprocess_d_chunk=True)` 显式开启。

## 5. 综合实践

把本讲四条主线串成一个完整任务：**手工推导 + 源码对照 + 运行验证**。

1. **推导**：在纸上从 \(O=\mathrm{softmax}(\tau QK^{\!\top})V\) 与上游 \(dO\) 出发，推出 \(dV/dP/dS/dQ/dK\) 五式，并标出 \(\mathrm{Di}_i=\sum_k P_{ik}dP_{ik}\) 这一耦合项。
2. **恒等式**：独立推出 \(\mathrm{Di}_i=\mathrm{rowsum}_d(dO_i\odot O_i)\)，并说清楚哪一步用到了「\(P\) 行和为 1」与「\(O=PV\)」。
3. **源码对照**：在 [`_ffpa_bwd.py`](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_ffpa_bwd.py) 里为下列每个量找到对应代码行：
   - delta 的预算：`_ffpa_bwd_pre_impl` 的 `tl.sum(o*do, axis=1)`；
   - delta 的查表使用：`_ffpa_bwd_dkdv` 里 `Di = tl.load(D + offs_qm)`；
   - \(dS\) 公式：`dS = (P * (dP - Di[:, None]) * softmax_scale)...`；
   - 两阶段切换：`_ffpa_attn_backward_triton_impl` 里 `delta = torch.empty_like(lse)` 之后的 pre 启动与主 kernel 启动。
4. **运行验证**：在 GPU 上跑 4.3.4 的 docs 反向示例，记录 `dQ/dK/dV` 三条 `max_abs_err`；再跑 `pytest tests/test_ffpa_bwd.py::test_ffpa_bwd_preprocess_full_and_d_chunk`，确认两模式一致。
5. **思考题**：若把 `preprocess_d_chunk` 从 `False` 改成 `True`，4.3.4 打印的三条误差会怎么变？为什么？（答案：理论上不变，因为 delta 数值一致；实测差异来自不同 launch 的浮点累加顺序，应在容差内。）

## 6. 本讲小结

- 反向链式法则给出 \(dV=P^{\!\top}dO\)、\(dP=dO\,V^{\!\top}\)、\(dS=P\odot(dP-\mathrm{Di})\)、\(dQ=\tau\,dS\,K\)、\(dK=\tau\,dS^{\!\top}Q\)；其中 \(\mathrm{Di}_i=\sum_k P_{ik}dP_{ik}\) 是 softmax 行间耦合项。
- FA-2 的核心恒等式：\(\mathrm{Di}_i=\mathrm{rowsum}_d(dO_i\odot O_i)\)，把昂贵的「沿 KV 归约」换成廉价的「沿 D 归约」，且只需前向已存的 `O` 与上游 `dO`。
- FFPA 反向因此是**两阶段**：先 `_ffpa_bwd_pre_impl` 算 delta，再让 dK/dV/dQ 主 kernel 把 delta 当「每行一个标量」查表使用；decode 反向同样复用 delta。
- 反向必须保存 `O`（给 delta）与 `LSE`（给 \(P=\exp(S-\mathrm{lse})\) 重建），否则要重跑前向的 softmax/矩阵乘。
- `preprocess_d_chunk` 控制 delta 预处理是「整 D 一次」还是「D 分块累加」；结果等价，只在寄存器压力与加载次数间取舍，是 `TritonBackend` 的反向专用开关。

## 7. 下一步学习建议

本讲只讲了 delta 预处理与主反向 kernel 里「如何用 delta」的高层结构，**没有展开 dK/dV 与 dQ 两个 kernel 的网格与 shared-pid 设计**。接下来建议：

1. **u5-l2《dK/dV 与 dQ kernel：shared program-id 设计》**：细读 `_ffpa_bwd_dkdv` 与 `_ffpa_bwd_dq` 的网格映射 `max(cdiv(Nk,BN), cdiv(Nq,BM))`，理解为什么 dQ 可以不用原子加。
2. **u5-l3《Decode 反向与 dQ 跨块归约》**：进入 `seqlen_q < 8` 分支，看 stage1 + reduce 如何跨 K 块累加 dQ。
3. 想了解反向的高级开关（TMA、warp-specialize、persist、split-launch），先读 [`src/ffpa_attn/triton/_ffpa_bwd.py`](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_ffpa_bwd.py) 顶部的「Performance note」长注释（34-69 行），再进 u5-l4。

---

> 配套阅读：本讲的恒等式推导可对照官方 Triton 教程 [`06-fused-attention.py`](https://triton-lang.org/main/_downloads/54a35f6ec55f9746935b9566fb6bb1df/06-fused-attention.py)（文件头注释 [`_ffpa_bwd.py:1-3`](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_ffpa_bwd.py#L1-L3) 标注的来源）。
