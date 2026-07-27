# Puzzle 04 Backward Op：广播与反向算子

## 1. 本讲目标

本讲在 Puzzle 03（外积式二维加法）的基础上，把「二维分块 + 二维 `T.Parallel`」用到一个真实算子上：**带广播的 fused multiply-ReLU**，并进一步写出它的**反向（求梯度）kernel**。

学完本讲，你应该能够：

1. 理解 PyTorch 的「自动广播」在 TileLang 里需要**手动**完成——通过把一维输入 `B` 装进一个长度为 `BLOCK_M` 的 tile，在二维循环里用 `B_local[j]` 反复引用它。
2. 把前向 `C[i,j] = max(0, A[i,j] * B[j])` 写成一个二维分块 kernel，复用上一讲的外积分块结构。
3. 用**链式法则（chain rule）**推导出反向公式 \(dA[i,j] = dC[i,j] \cdot B[j] \cdot \mathbb{1}[A[i,j]\cdot B[j] > 0]\)，并把它落成一个 kernel。
4. 理解 ReLU 的导数本质是一个**条件掩码（mask）**，学会用 `T.if_then_else` 把这个 0/1 掩码表达成可微的标量乘法。

本讲是本手册第一次接触「**反向算子 / autograd**」这一主题，为后续理解 GEMM、FlashAttention 的前反向打基础。

## 2. 前置知识

在开始前，请确认你已理解下面几个概念（它们都来自前几讲）：

- **二维 `T.Kernel` 与块索引**：`with T.Kernel(N//BLOCK_N, M//BLOCK_M, threads=256) as (pid_n, pid_m)` 决定 grid 形状，`pid_n`/`pid_m` 是每个 block 定位自身数据区间的依据（详见 u2-l3）。
- **多维 `T.Parallel(i, j)`**：声明一个二维迭代空间并整体并行划分，循环体里写标量表达式（详见 u2-l3）。
- **`T.alloc_fragment`**：把「一个 block 内所有线程的寄存器」抽象成一块可像普通 buffer 操作的 fragment，配合 `T.copy` 在 global 与 fragment 间批量搬运（详见 u2-l2）。
- **`T.if_then_else(cond, a, b)`**：TileLang 里表达「逐元素运行时分支」的表达式，Python 原生 `if` 只能在编译期静态求值（详见 u2-l1）。
- **链式法则与 PyTorch autograd**：若 \(C = f(A)\)，给定上游梯度 \(dC = \partial \mathcal{L}/\partial C\)，则 \(dA = \partial \mathcal{L}/\partial A = dC \cdot f'(A)\)。本讲会手算这个导数，再用 `torch.autograd` 验证。

一个一句话回顾本讲的「广播」直觉：上一讲里 `C[i,j] = A[i] + B[j]`，`A[i]` 只依赖 `i`、`B[j]` 只依赖 `j`；本讲把 `+` 换成 `*` 并套上 ReLU，再把 `A` 升级成二维 `A[i,j]`，于是 `B[j]` 像「广播」一样沿 `N` 轴被每一行复用。

## 3. 本讲源码地图

本讲只涉及两个文件，它们结构完全相同——题目文件留空 `TODO`，答案文件给出完整实现：

| 文件 | 作用 |
| --- | --- |
| [puzzles/04-backward-op.py](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/puzzles/04-backward-op.py) | 题目本体：含两段算子规格（前向 / 反向）、torch 参考实现、留空 `TODO` 的两个 kernel 骨架，以及 `run_*` 与 `__main__`。 |
| [ans/04-backward-op.py](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/ans/04-backward-op.py) | 参考答案：与题目文件同名函数的完整实现，用来对照学习。 |

测试框架沿用 [common/utils.py](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/common/utils.py) 里的 `test_puzzle`（u1-l2 已讲透）：它按「最后一个张量即输出」的约定自动构造输入、跳过输出、跑 torch 参考后用 `torch.allclose` 比对。本讲无需再读它，但要知道反向 kernel 有 3 个输入（`A, B, dC`）、1 个输出（`dA`），`test_puzzle` 会自动造 3 个随机输入。

---

## 4. 核心概念与源码讲解

本讲按三个最小模块推进：先写**前向**（理解广播），再推导**反向的链式法则**（拿到数学公式），最后把公式落成**带条件掩码的 kernel**。

### 4.1 广播 mul-relu 前向

#### 4.1.1 概念说明

题目定义的前向算子是：

\[
C[i, j] = \max\bigl(0,\; A[i, j] \cdot B[j]\bigr),\quad A\in\mathbb{R}^{N\times M},\; B\in\mathbb{R}^{M}
\]

在 PyTorch 里这只要一行 `(A * B).relu_()`，因为 `A * B` 会自动把 `B` 从 `(M,)` 广播成 `(N, M)`。但 **TileLang 不会替你做形状广播**——它的张量运算是写在一个标量循环体里的，没有「形状对齐」这一步。因此这里的「广播」要由你手动实现：

- 把 `B` 沿 `M` 轴切成一段长 `BLOCK_M` 的 tile，装进一维 fragment `B_local`；
- 在二维循环 `(i, j)` 里写 `A_local[i,j] * B_local[j]`——`B_local[j]` 对所有 `i` 都相同，这正是「`B` 沿 `N` 轴广播」在 kernel 层面的样子。

换句话说，**广播 = 同一段 `B` 的 tile 被一个 block 内所有行复用**。这也直接带来上一讲讲过的「读放大」收益：每个 block 只读 `BLOCK_M` 个 `B` 元素，却能服务 `BLOCK_N × BLOCK_M` 个输出，`B` 的全局读取量被砍掉一个 `BLOCK_N` 因子。

#### 4.1.2 核心流程

前向 kernel 的执行流程（每个 block 独立完成自己的 `BLOCK_N × BLOCK_M` 子矩阵）：

```text
1. 用 (pid_n, pid_m) 换算本 block 负责的数据起点 n_idx = pid_n*BLOCK_N, m_idx = pid_m*BLOCK_M
2. 分配三个 fragment：
     A_local : (BLOCK_N, BLOCK_M)   # 二维输入 tile
     B_local : (BLOCK_M,)           # 一维输入 tile（被广播的那一个）
     C_local : (BLOCK_N, BLOCK_M)   # 二维输出 tile
3. T.copy(A[n_idx, m_idx], A_local)   # 从 global 搬入 A 的子矩阵
4. T.copy(B[m_idx], B_local)          # 从 global 搬入 B 的一段
5. for i, j in T.Parallel(BLOCK_N, BLOCK_M):
       C_local[i,j] = A_local[i,j] * B_local[j]      # 广播乘法（B_local[j] 跨 i 复用）
       C_local[i,j] = if_then_else(C_local[i,j] > 0, C_local[i,j], 0)  # ReLU
6. T.copy(C_local, C[n_idx, m_idx])   # 搬回 global
```

注意第 5 步里 `A_local[i,j] * B_local[j]` 是**标量表达式**（`i,j` 是循环变量），不存在形状不匹配；`B_local[j]` 对固定 `j` 在所有 `i` 上取同一值，就是广播的语义。

#### 4.1.3 源码精读

先看题目里 torch 的参考实现，确认我们追求的语义就是「逐元素乘 + ReLU」，`torch.mul` 自动广播 `B`：

[ans/04-backward-op.py:L39-L46](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/ans/04-backward-op.py#L39-L46) — 参考实现 `(A * B).relu_()`，`A` 为 `(N,M)`、`B` 为 `(M,)`，靠 torch 自动广播。

再看 kernel 骨架，确认声明部分：`N, M = T.const("N, M")` 声明符号维度、`A/B/C` 的形状与 dtype、分块大小 `BLOCK_N/BLOCK_M` 是编译期超参（调用 `compile` 时绑定）：

[puzzles/04-backward-op.py:L49-L59](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/puzzles/04-backward-op.py#L49-L59) — 前向 kernel 声明骨架，`TODO` 在第 57 行，需要补全 `with T.Kernel(...)` 内的 device 计算。

参考答案的完整实现如下，对应上面流程的 6 步：

[ans/04-backward-op.py:L58-L70](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/ans/04-backward-op.py#L58-L70) — 这段代码做了：①二维 `T.Kernel` 解包 `(bx, by)` 并换算起点；②用 `T.alloc_fragment` 分配 `A_local/B_local/C_local`（注意 `B_local` 是一维 `(BLOCK_M,)`）；③两次 `T.copy` 把 `A` 子矩阵与 `B` 一段搬入寄存器；④`T.Parallel(BLOCK_N, BLOCK_M)` 里先乘、再用 `T.if_then_else(... > 0, ..., 0)` 做 ReLU；⑤`T.copy` 把结果搬回 `C`。

值得对照的两点：
- 这里的块索引在答案里命名为 `bx, by`，而在反向 kernel 与 Puzzle 03 里命名为 `pid_n, pid_m`——它们都是同一个 `blockIdx`，只是取名不同（u1-l3 已澄清过）。
- ReLU 用了两步写（先存乘积、再用 `if_then_else` 覆盖）。也可以合并成一行 `C_local[i,j] = T.if_then_else(A_local[i,j]*B_local[j] > 0, A_local[i,j]*B_local[j], 0)`，两者等价；答案的两步写更贴近「先算 `Z=A*B`、再 `relu(Z)`」的数学结构，便于和反向对照。

#### 4.1.4 代码实践

**实践目标**：补全前向 kernel，跑通 `test_puzzle` 并与 torch 对齐。

**操作步骤**：

1. 打开 `puzzles/04-backward-op.py`，定位第 57 行的 `# TODO`。
2. 参照上面流程的 6 步（或直接参考 `ans/04-backward-op.py` 的 L58–L70），在 `with T.Kernel(N // BLOCK_N, M // BLOCK_M, threads=256) as (bx, by):` 里补全实现。
3. 在仓库根目录运行：

   ```bash
   python3 puzzles/04-backward-op.py
   ```

   > 若报 `ModuleNotFoundError: No module named 'common'`，说明仓库根目录不在 `sys.path` 上，改用 `PYTHONPATH=. python3 puzzles/04-backward-op.py` 即可。

**需要观察的现象**：脚本会先打印 `=== Fused Multiplication ReLU with Broadcasting ===`，接着 `test_puzzle` 打印一行 `✅ Results match: True`（前向）；反向那行暂时还是 `❌`（因为还没补）。前向只依赖本节内容，应当率先通过。

**预期结果**：前向 `✅ Results match: True`。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `B_local` 也声明成二维 `(BLOCK_N, BLOCK_M)` 并在 `T.copy` 时让每行都复制同一段 `B`，结果还对吗？为什么答案偏要用一维 `(BLOCK_M,)`？

> **答案**：结果仍正确（每行都是同一个 `B[j]`），但会浪费寄存器——`B` 沿 `N` 轴本就是常量，没必要复制 `BLOCK_N` 份。一维 `B_local` 既省存储，也直观体现「`B` 是被广播的那一维」。

**练习 2**：把答案里的两步 ReLU 改写成一行 `T.if_then_else` 的形式，重新跑 `test_puzzle`，确认结果不变。

> **答案**：`C_local[i, j] = T.if_then_else(A_local[i, j] * B_local[j] > 0, A_local[i, j] * B_local[j], 0)`。`test_puzzle` 仍应报 `✅`，说明两种写法语义等价。

---

### 4.2 反向算子的链式法则

#### 4.2.1 概念说明

训练神经网络时，前向算出 loss 后，优化器需要的是「loss 对每个参数的梯度」。反向传播（backward / autograd）就是用**链式法则**把上游梯度一层层传回输入。

对本算子，题目给定上游梯度 \(dC = \partial \mathcal{L}/\partial C\)（形状 `(N,M)`），要求算 \(dA = \partial \mathcal{L}/\partial A\)（同样 `(N,M)`）。注意：**只对 `A` 求梯度**，不对 `B` 求（题目明确「compute the gradient of the loss w.r.t. A」）。

这是一个「自定义反向算子」的典型场景：当我们手写前向 kernel 而不走 PyTorch 原生算子时，PyTorch 没法自动帮你反传，于是你得自己把导数写成一个 kernel。TileLang 的意义正在于此——它能让你把任意前向/反向都编译成高效 GPU 代码。

#### 4.2.2 核心流程

记 \(Z[i,j] = A[i,j]\cdot B[j]\)，则 \(C[i,j] = \mathrm{relu}(Z[i,j]) = \max(0, Z[i,j])\)。对 \(A[i,j]\) 用链式法则（注意 \(Z\) 对 \(A\) 的偏导正好是 \(B[j]\)）：

\[
\frac{\partial \mathcal{L}}{\partial A[i,j]}
= \frac{\partial \mathcal{L}}{\partial C[i,j]}\cdot
  \frac{\partial C[i,j]}{\partial Z[i,j]}\cdot
  \frac{\partial Z[i,j]}{\partial A[i,j]}
\]

其中 ReLU 的导数是「开关函数」：

\[
\frac{\partial C[i,j]}{\partial Z[i,j]} = \mathbb{1}\bigl[Z[i,j] > 0\bigr] =
\begin{cases} 1, & A[i,j]\cdot B[j] > 0 \\ 0, & \text{otherwise}\end{cases}
\]

而

\[
\frac{\partial Z[i,j]}{\partial A[i,j]} = B[j]
\]

合并，并把 \(dC[i,j]\) 记作 \(\partial \mathcal{L}/\partial C[i,j]\)：

\[
\boxed{\,dA[i,j] = dC[i,j]\cdot B[j]\cdot \mathbb{1}\bigl[A[i,j]\cdot B[j] > 0\bigr]\,}
\]

这正是题目 `Definition` 里写的那一行（见 [puzzles/04-backward-op.py:L93-L97](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/puzzles/04-backward-op.py#L93-L97)）。直觉读法：**梯度只能穿过那些前向「没被 ReLU 掐掉」的位置**——若前向 \(A\cdot B \le 0\)，ReLU 把它截断为 0，该位置对 loss 没贡献，梯度为 0。

> 关于 \(Z=0\) 处的次梯度：PyTorch 的 `relu` 在 0 处约定导数为 0，公式里用严格 `> 0` 与之一致。

#### 4.2.3 源码精读

题目用 `torch.autograd` 给出反向参考实现，它是我们验证手算公式的「金标准」：

[ans/04-backward-op.py:L113-L127](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/ans/04-backward-op.py#L113-L127) — 这段代码：克隆 `A/B` 并打开 `requires_grad`；前向 `C = torch.relu(A * B)`；调用 `C.backward(dC)` 让 autograd 自动反传；最后返回 `A.grad`。我们的 kernel 必须与它 `allclose`。

反向 kernel 的声明与前向几乎一样，只是多了一个输入 `dC`、输出换成 `dA`，并且加了 `pass_configs`：

[puzzles/04-backward-op.py:L117-L133](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/puzzles/04-backward-op.py#L117-L133) — 反向 kernel 骨架。注意第 117–122 行的 `pass_configs` 关掉了 `TL_DISABLE_WARP_SPECIALIZED` 与 `TL_DISABLE_TMA_LOWER` 两个高级 lowering（u2-l2 已介绍），目的是让 `print_source_code()` 生成的 CUDA 更易读，对本算子的正确性无影响。

#### 4.2.4 代码实践

**实践目标**：在动键盘之前，先用一个小例子确认你真的理解了链式法则。

**操作步骤**：

1. 用 Python 起一个最小对照（示例代码，非项目原有代码）：

   ```python
   import torch
   A = torch.tensor([[ 1.0, -2.0],[ 3.0,  0.0]], requires_grad=True)
   B = torch.tensor([ 2.0, -1.0], requires_grad=True)
   dC = torch.tensor([[ 1.0,  1.0],[ 1.0,  1.0]])
   torch.relu(A * B).backward(dC)
   print(A.grad)
   # 手算: A*B = [[2, 2],[6, 0]]; mask = [[>0, >0],[>0, !(>0)]] = [[1,1],[1,0]]
   # dA = dC*B*mask = [[1*2*1, 1*(-1)*1],[1*2*1, 1*(-1)*0]] = [[2, -1],[2, 0]]
   ```

2. 对比打印值与你手算的 `[[2, -1],[2, 0]]`。

**需要观察的现象**：`A.grad` 的第 4 个元素（对应 `A=0, B=-1`，前向 `A*B=0`）应为 `0`，验证了「\(Z=0\) 处梯度为 0」的约定。

**预期结果**：`A.grad` 与手算的 `[[2, -1],[2, 0]]` 完全一致。

#### 4.2.5 小练习与答案

**练习 1**：如果题目改成「对 `B` 求梯度」\(dB\)，公式会是什么样？（提示：\(Z\) 对 `B[j]` 的偏导会聚合多个 `i`。）

> **答案**：\(dB[j] = \sum_{i=0}^{N-1} dC[i,j]\cdot A[i,j]\cdot \mathbb{1}[A[i,j]\cdot B[j] > 0]\)。注意现在要在 `i` 维上**归约求和**——这正是下一讲 Puzzle 05（Reduce Sum）要解决的问题，所以本题故意只问 \(dA\)，回避了归约。

**练习 2**：为什么反向公式里那个 `> 0` 用的是 `A[i,j]*B[j]`，而不是 `C[i,j]`？

> **答案**：因为 ReLU 的导数是看「**输入** \(Z\) 是否大于 0」，而 \(Z=A\cdot B\)。虽然 `C > 0` 与 `A*B > 0` 在大多数情况下等价（ReLU 输出大于 0 当且仅当输入大于 0），但用 \(Z\)（即 `A*B`）表达更贴合数学定义，也避免了 `C` 恰好为 0 时的歧义。

---

### 4.3 条件掩码（A*B>0）与梯度回传

#### 4.3.1 概念说明

上一节我们得到 \(dA[i,j] = dC[i,j]\cdot B[j]\cdot \mathbb{1}[A[i,j]\cdot B[j] > 0]\)。最后那个 \(\mathbb{1}[\cdot]\) 就是一个**条件掩码（mask）**：值为 1 表示「这个位置前向是激活的，梯度放行」；值为 0 表示「前向被 ReLU 掐掉，梯度归零」。

在 TileLang 里，这个 0/1 掩码用 `T.if_then_else(cond, 1, 0)` 表达——它返回一个标量 1.0 或 0.0，可以直接参与乘法。于是整条梯度公式可以**全部写在一个 `T.Parallel` 循环体里，没有任何分支跳转**，对 GPU 非常友好（无 warp divergence 风险来自控制流，所有线程执行相同的乘法指令序列，只是各自乘了 0 或 1）。

#### 4.3.2 核心流程

反向 kernel 的结构与前向同构，只是输入多了一个 `dC`、输出换成 `dA`：

```text
1. (pid_n, pid_m) 换算起点 n_idx, m_idx
2. 分配缓冲：
     A_local, B_local, dC_local : 装载三个输入的 tile
     dA_local : (BLOCK_N, BLOCK_M) 输出 tile
3. T.copy 把 A、B、dC 三段从 global 搬入
4. for i, j in T.Parallel(BLOCK_N, BLOCK_M):
       mask = if_then_else(A_local[i,j]*B_local[j] > 0, 1, 0)   # ReLU 的导数
       dA_local[i,j] = mask * dC_local[i,j] * B_local[j]         # 链式法则
5. T.copy(dA_local, dA[n_idx, m_idx])   # 搬回 global
```

这里有一个**值得注意的实现细节**：参考答案把三个**只读输入** tile（`A_local/B_local/dC_local`）放在 **shared memory**（`T.alloc_shared`），而把**输出** `dA_local` 放在 **fragment**（`T.alloc_fragment`）。前向则把所有 tile 都放在 fragment。两种选择都正确，差别在于：

- `T.alloc_shared`：分配在 block 内共享内存上，block 内所有线程可见，适合暂存从 global 读进来的数据；
- `T.alloc_fragment`：分配在寄存器上，更快但容量更小，适合需要反复读写的中间结果（这里输出 `dA_local` 计算后即搬走）。

本讲不展开 shared memory 的细节（那是 u4-l4 / u5 的主线），你只需知道：**答案作者为反向的输入 tile 选了 shared、为输出选了 fragment，这是一种合理但非唯一的取舍；你全部用 fragment 也能通过 `test_puzzle`。**

#### 4.3.3 源码精读

参考答案的反向 kernel 完整实现：

[ans/04-backward-op.py:L145-L162](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/ans/04-backward-op.py#L145-L162) — 这段代码做了：①`T.Kernel` 解包 `(pid_n, pid_m)`、换算起点；②`A_local/B_local/dC_local` 用 `T.alloc_shared`、`dA_local` 用 `T.alloc_fragment`；③三次 `T.copy` 搬入输入；④`T.Parallel(BLOCK_N, BLOCK_M)` 里用 `T.if_then_else(A_local[i,j]*B_local[j] > 0, 1, 0)` 造掩码，再乘 `dC_local[i,j]*B_local[j]`；⑤`T.copy` 把 `dA_local` 搬回。

聚焦第 157–160 行的核心计算：

```python
for i, j in T.Parallel(BLOCK_N, BLOCK_M):
    dA_local[i, j] = (
        T.if_then_else(A_local[i, j] * B_local[j] > 0, 1, 0) * dC_local[i, j] * B_local[j]
    )
```

对照数学公式 \(dA = dC\cdot B\cdot \mathbb{1}[A\cdot B>0]\)，三者逐项对应：`T.if_then_else(...>0, 1, 0)` 是 \(\mathbb{1}[\cdot]\)，`dC_local[i,j]` 是 \(dC\)，`B_local[j]` 是 \(B[j]\)（同样是被广播、跨 `i` 复用的一维 tile）。

另外注意 `B_local` 在反向里声明为 `T.alloc_shared((BLOCK_M,), dtype)`（一维，与前向一致），再次体现「广播维用一维 tile」的约定。

最后，`test_puzzle` 对反向的调用在 [puzzles/04-backward-op.py:L136-L148](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/puzzles/04-backward-op.py#L136-L148)（`run_mul_relu_bwd`），它把 `N=8192, M=4096, BLOCK_N=64, BLOCK_M=64` 作为超参传给 `compile`；`test_puzzle` 会自动造 `A/B/dC` 三个随机输入、用 `ref_mul_relu_bwd`（autograd）算金标准、再 `torch.allclose(atol=1e-2, rtol=1e-2)` 比对。

#### 4.3.4 代码实践

**实践目标**：补全反向 kernel，使 `dA` 与 `torch.autograd` 的结果对齐。

**操作步骤**：

1. 打开 `puzzles/04-backward-op.py`，定位第 131 行的 `# TODO`。
2. 按 4.3.2 流程补全 `with T.Kernel(N // BLOCK_N, M // BLOCK_M, threads=256) as (pid_n, pid_m):` 内的实现。你可以选择全部用 `T.alloc_fragment`（与前向风格一致），也可以照答案用 shared 装输入、fragment 装输出——两者都正确。
3. 运行：

   ```bash
   python3 puzzles/04-backward-op.py
   ```

4. （可选）把答案里被注释掉的两行（[puzzles/04-backward-op.py:L142-L143](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/puzzles/04-backward-op.py#L142-L143)）取消注释，`kernel.print_source_code()` 会打印生成的 CUDA 源码，观察 `if_then_else` 是如何被 lowering 成三元运算或 `select` 指令的。

**需要观察的现象**：脚本打印 `=== Fused Multiplication ReLU with Broadcasting, Backward ===`，随后 `test_puzzle` 报 `✅ Results match: True`。

**预期结果**：前向与反向两行均为 `✅ Results match: True`。

> 说明：本环境是否已装好 GPU 与 tilelang 不确定；若运行时报找不到 GPU 或 `import tilelang` 失败，可先按 u1-l1 跑 `python3 scripts/check_tilelang_env.py` 排查。无法本地运行时，以上结果标注为「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：把反向循环体改写成「不用显式 mask、直接用一层 `T.if_then_else`」的形式，即 `dA_local[i,j] = T.if_then_else(A*B > 0, dC*B, 0)`。重跑 `test_puzzle`，结果应不变。思考两种写法在生成的 CUDA 上可能有何差异。

> **答案**：语义完全等价，`test_puzzle` 仍报 `✅`。`mask*...` 的写法是一条纯乘法指令链（mask 是 0.0/1.0），无控制流；`if_then_else(cond, a, 0)` 可能被编译成 `select`/`cmove` 或同样优化成乘法。对这种 0/1 二值情形，两者性能通常接近，可作为「代码检视」练习用 `print_source_code()` 对比。

**练习 2**：如果把 `B_local[j]` 误写成 `B_local[i]`（下标用错维度），反向会怎样？

> **答案**：编译期 `B_local` 形状是 `(BLOCK_M,)`，用 `i`（范围 `0..BLOCK_N`）索引会越界/报错；即便形状碰巧不报错，结果也会错乱，`test_puzzle` 报 `❌`。这提醒：**广播维的下标必须用与其 tile 维度对应的那个循环变量**（`B` 沿 `M` 切分，故用 `j`）。

---

## 5. 综合实践

把本讲三个模块串起来，完成一个「前向 + 反向 + autograd 对照」的小任务：

1. **补全两个 kernel**：在 `puzzles/04-backward-op.py` 里补全 `tl_mul_relu_bcast`（前向）与 `tl_mul_relu_bwd`（反向），运行 `python3 puzzles/04-backward-op.py` 使两处均打印 `✅`。
2. **改一个问题规模**：把 `run_mul_relu_bwd` 里的 `M` 从 `4096` 改成 `8192`（保持 `BLOCK_M=64`），确认 kernel 仍能编译并通过——这验证了 `M` 作为符号维度在 `compile` 时绑定、分块逻辑与具体规模解耦。
3. **手算交叉验证**：仿照 4.2.4 的小例子，自己构造一个 \(2\times2\) 的 `A`、长度 2 的 `B` 与全 1 的 `dC`，先用本讲公式手算 `dA`，再与 `ref_mul_relu_bwd`（autograd）对照，最后与 kernel 输出对照，三者应一致。
4. **（选做）代码检视**：取消注释 `run_mul_relu_bwd` 里的 `kernel.print_source_code()`，找到 `if_then_else` 对应的 CUDA 语句，写一两句观察（例如它是否变成了 `fmax`/`fmin`/`select`）。

> 第 3 步若无法运行 GPU kernel，至少完成「手算 vs autograd」对照，并标注 kernel 部分为「待本地验证」。

## 6. 本讲小结

- **广播在 TileLang 里是手动的**：`B` 沿 `N` 轴的广播 = 用一维 tile `B_local[j]` 在二维循环里跨 `i` 反复引用；没有 torch 那种自动形状对齐。
- **前向 = 二维分块 + fused mul-relu**：复用 Puzzle 03 的 `(pid_n, pid_m)` 外积分块结构，把 `A` 子矩阵与 `B` 一段搬入寄存器，在 `T.Parallel(i,j)` 里乘 + ReLU。
- **反向靠链式法则**：\(dA = dC \cdot B \cdot \mathbb{1}[A\cdot B > 0]\)，其中 \(\mathbb{1}[\cdot]\) 正是 ReLU 的导数。
- **条件掩码是 0/1 标量**：用 `T.if_then_else(cond, 1, 0)` 表达，整条梯度公式可写成无控制流的乘法链，对 GPU 友好。
- **shared vs fragment 是实现取舍**：参考答案把只读输入放 shared、输出放 fragment，但全用 fragment 也正确；本讲不深入 shared memory，留待后续。
- **验证靠 autograd 金标准**：`ref_mul_relu_bwd` 用 `torch.autograd` 反传得到 `A.grad`，`test_puzzle` 以 `allclose(1e-2)` 与 kernel 比对。

## 7. 下一步学习建议

本讲的反向公式 \(dA\) **回避了沿 `N` 维的求和**——但若改成对 `B` 求梯度（见 4.2.5 练习 1），就必须在 `i` 维上归约。这正是下一讲 **Puzzle 05 Reduce Sum** 的主题：学习 `T.reduce_sum` 这个归约 TileOp、用 `T.Serial` 分块累加、以及 `T.clear` 的累加语义。掌握归约后，你就能写出任意需要跨轴聚合的反向算子，也为紧接着的 Puzzle 06 Softmax（数值稳定归约 + exp）与 Puzzle 07 FlashAttention 打好地基。

建议在进入下一讲前，先确保本讲的「手算链式法则」你已经能独立完成——反向算子的数学直觉比 kernel 写法更值得带走。
