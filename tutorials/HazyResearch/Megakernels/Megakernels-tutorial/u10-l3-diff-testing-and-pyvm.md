# diff 测试与 python_vm 参考实现

> 阶段：advanced · 依赖：[u4-l3 生成器执行模型](u4-l3-generators-execution-model.md)、[u3-l2 latency 指令集](u3-l2-latency-instruction-set.md)
>
> 本讲对应手册单元 **U10·L3**。承接 [U4·L3](u4-l3-generators-execution-model.md) 的「`globs` 黑板模型」与 [U3·L2](u3-l2-latency-instruction-set.md) 的「七条 latency 指令」——本讲要回答的问题是：**我们怎么相信那个编译进 CUDA 的 megakernel 算对了？** 答案是一套**差分测试（differential testing）**：用一份纯 PyTorch 写的「参考虚拟机」`PyVM_Interpreter`，按和内核**完全相同的分块方式**把每个 op 算一遍，再把两边的逐张量结果对照打印。

---

## 1. 本讲目标

学完本讲后，你应当能够：

1. 说清 `PyVM_Interpreter` 是什么：它不是「跑一整层 `nn.functional`」，而是**一个按指令逐条分发的 Python 循环**，用 `instruction_to_solver[type(instruction)](globals, instruction)` 把每条 `Instruction` 派发给一个手写的纯 PyTorch solver。
2. 解释 `INSTRUCTION_TO_SOLVER` 这张「指令类 → solver 函数」的映射表是如何把抽象指令「兑现」成具体的张量计算的，以及 `dispatch.py` 如何按 `setting`（latency/throughput）挑选不同的映射表。
3. 读懂三个公共数值原语 `matvec` / `rms_norm` / `matvec_with_residual`，并区分 `matvec` 的**两种模式**：整段输出模式与 **split-K 归约模式**。
4. 读懂 latency 下的关键 solver（`o_proj_residual`、`layer_norm_double_matvec_silu`、`rms_lm_head`、`partial_attention`、`attention_reduction`），并指出它们**如何刻意镜像内核的分块粒度与 barrier 同步**——这正是差分测试有意义的根本原因。
5. 走通 `diff_test.py` 的对照主流程（`gpy` 跑 pyvm、`gmk` 跑 mk、最后 `gpy.diff(gmk)`），解释 `BaseGlobals.diff()` 打印的 **`max adiff`** 与 **`mean rdiff`** 两个指标的定义、含义与可接受范围。

---

## 2. 前置知识

### 2.1 为什么要「差分测试」？

megakernel 把整个 transformer 层 + `lm_head` 编译进**一个常驻 GPU 的内核**，里面充满了 warp 特化、split-K 归约、在线 softmax、跨 SM 屏障（[U5–U9](u9-l3-remaining-ops.md)）。这种代码极容易写错一个偏移、漏一次累加、把某段 reduction 切错——而且错误往往**不崩、只是数值悄悄偏了**。

最稳的验证思路不是「单测某个算子」，而是**对照**：用一份「简单到不可能错」的参考实现跑同一份输入，把中间张量逐个比对。这份参考实现就是 `python_vm`。

### 2.2 关键回顾：`globs` 是一块「共享黑板」

[U3·L1](u3-l1-base-globals-and-instruction-set.md) 讲过，`BaseGlobals`（[megakernels/instructions.py:10-69](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/instructions.py#L10-L69)）是一块「黑板」，megakernel 不接收临时参数，而是直接读写这块黑板上的字段（权重、KV cache、`hidden_states`、`attn_out`、`silu_out`、`logits`、`barriers`……）。

差分测试之所以可行，正是因为**中间量都落在固定的 `globs` 字段里**：pyvm 写进 `gpy` 这块黑板，mk 写进 `gmk` 这块黑板，最后逐字段对比即可。如果某个 op 的输出字段对不上，就精确地定位到了哪一步算错。

### 2.3 关键回顾：七条 latency 指令构成一条 DAG

[U3·L2](u3-l2-latency-instruction-set.md) 列出了 latency 模式的七条指令（opcode 1–7）：

| opcode | 指令类 | 含义 |
| --- | --- | --- |
| 1 | `LayerNorm_QKV_MatVecRopeAppend` | attn 路的 RMSNorm + QKV matvec + RoPE + 追加 KV cache |
| 2 | `PartialAttention` | 注意力的一个分片（在线 softmax 的一部分） |
| 3 | `AttentionReduction` | 合并多个 attention 分片（LSE 归并） |
| 4 | `O_ProjResidual` | o_proj + 残差累加回 `hidden_states` |
| 5 | `LayerNormDoubleMatVecSiLU` | mlp 路的 RMSNorm + gate/up 双 matvec + SiLU |
| 6 | `DownProjResidual` | down_proj + 残差累加回 `hidden_states` |
| 7 | `RMS_LM_Head` | 最后的 RMSNorm + lm_head 投影到 logits |

本讲的 `INSTRUCTION_TO_SOLVER` 表正是给这七条指令**各配一个 Python solver**。

> 提示：本讲只讲 **latency 设置**（单批、`batch_size=1`）下的 pyvm。throughput 设置有自己的 `demos/throughput/python_vm.py`，结构同构，本讲不展开。

---

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| [megakernels/python_vm.py](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/python_vm.py) | **共享层**：`PyVM_Interpreter` 解释器 + 公共数值原语 `matvec` / `rms_norm` / `matvec_with_residual` + `print_state`。所有 demo 都复用它。 |
| [megakernels/demos/latency/python_vm.py](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/latency/python_vm.py) | **latency 专属层**：七个 solver 函数 + `INSTRUCTION_TO_SOLVER` 映射表。把抽象指令「兑现」成具体张量计算。 |
| [megakernels/scripts/diff_test.py](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/scripts/diff_test.py) | **对照脚本**：构造 `gpy`/`gmk` 两块黑板 → pyvm 跑 `gpy`、mk 跑 `gmk` → `gpy.diff(gmk)` 打印逐张量差异。 |
| [megakernels/instructions.py](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/instructions.py) | `BaseGlobals`（含 `diff()` 与 `diff_tensors()`）与 `Instruction` 基类。 |
| [megakernels/dispatch.py](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/dispatch.py) | 按 `setting` 把正确的 `INSTRUCTION_TO_SOLVER` 表喂给 `PyVM_Interpreter`。 |
| [megakernels/demos/latency/instructions.py](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/latency/instructions.py) | 七条指令的 dataclass 定义（含 `opcode` / `prev_opcode` / `cost`）。 |

---

## 4. 核心概念与源码讲解

### 4.1 PyVM_Interpreter：用 Python 当「参考虚拟机」

#### 4.1.1 概念说明

megakernel 在 GPU 上是一个「取指—译码—执行」的虚拟机（见 [U6 控制器](u4-l3-generators-execution-model.md)）。`PyVM_Interpreter` 是它的 **Python 翻版**：同样的「逐条取指令、按指令类型分发执行」循环，但执行体是纯 PyTorch 张量运算，跑在普通的 eager 模式下，没有任何 CUDA 黑魔法。

它的价值在于「简单可信」：

- **同样吃 `globs` + `instructions`**：和 mk 内核吃同一份黑板、同一条指令序列，所以两者面对的是**完全相同的计算任务**。
- **执行体透明**：每条指令对应一个一眼能读懂的 Python 函数，没有 warp 特化、没有 `__syncthreads`、没有 TMA。若 pyvm 与 mk 对不上，错的一定（大概率）在 mk 侧。

#### 4.1.2 核心流程

`PyVM_Interpreter.interpret` 的全部逻辑可以写成一句话：

```text
for instruction in instructions:
    solver = instruction_to_solver[type(instruction)]   # 用「指令类」查表
    solver(globs, instruction)                          # 执行：读 globs、写 globs
```

也就是一个**按类型分发（dispatch by type）的解释器循环**。注意它**不依赖 opcode 整数**，而是直接用 Python 的 `type(instruction)` 作为字典 key——这比内核里用整数 opcode 跳转更直白，也更不容易写错。

#### 4.1.3 源码精读

解释器的「循环本体」就是模块级函数 `interpret_with_pyvm`：

[ megakernels/python_vm.py:83-87 ](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/python_vm.py#L83-L87) —— 遍历指令列表，用 `instruction_to_solver[type(instruction)]` 查到对应 solver 并调用。`type(instruction)` 取的是指令的**具体子类**（如 `O_ProjResidual`），正好是映射表的 key。

`PyVM_Interpreter` 类只是把这个循环包成一个持有映射表的对象：

[ megakernels/python_vm.py:90-96 ](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/python_vm.py#L90-L96) —— 构造时接收一张 `instruction_to_solver` 表，`interpret` 直接转发给上面的模块函数。注意它对 latency/throughput **完全无感**：差别全在那张表里，表由 `dispatch.py` 注入。

[ megakernels/dispatch.py:41-42 ](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/dispatch.py#L41-L42) —— `make_pyvm_interpreter(mode)` 按 `setting` 从 `INSTRUCTION_TO_SOLVER_MAP` 取出对应映射表，造一个 `PyVM_Interpreter`。`INSTRUCTION_TO_SOLVER_MAP` 本身定义在 [dispatch.py:27-30](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/dispatch.py#L27-L30)，latency 用 `LATENCY_INSTRUCTION_TO_SOLVER`，throughput 用另一张。

> 设计要点：**「解释器骨架」与「指令实现」解耦**。共享层只提供「按类型分发」的循环；具体每条指令怎么算，延迟在 `demos/<setting>/python_vm.py` 里。这与内核侧「控制器（取指/分发）」和「op 五子结构（执行）」的分层是完全对应的（见 [U8 op 接口](u9-l3-remaining-ops.md)）。

#### 4.1.4 代码实践

**目标**：在不跑 GPU 的前提下，亲手感受「按类型分发」的解释器循环。

1. 阅读上面三段源码，确认 `interpret_with_pyvm` 的循环体只有一行真正的派发。
2. 在 Python 里写一个**最小自造解释器**（示例代码，非项目代码）：

   ```python
   # 示例代码：模仿 PyVM_Interpreter 的按类型分发
   class Add:
       def __init__(self, x): self.x = x
   class Mul:
       def __init__(self, x): self.x = x

   def do_add(state, ins): state["v"] += ins.x
   def do_mul(state, ins): state["v"] *= ins.x

   SOLVERS = {Add: do_add, Mul: do_mul}

   def interpret(state, instructions):
       for ins in instructions:
           SOLVERS[type(ins)](state, ins)   # 与 interpret_with_pyvm 同构

   s = {"v": 1}
   interpret(s, [Add(2), Mul(3), Add(1)])    # ((1+2)*3)+1 = 10
   print(s)  # {'v': 10}
   ```

3. **观察**：把映射表里 `Add` 的 solver 换成「什么都不做」，重跑——输出应变成 `3`（`1*3`）。这印证了「行为完全由表决定，解释器骨架本身不含任何业务逻辑」。

#### 4.1.5 小练习与答案

**Q1**：`PyVM_Interpreter` 是按 `opcode()` 整数分发的，还是按 `type(instruction)` 分发的？为什么这样选？

**答**：按 `type(instruction)` 分发（[python_vm.py:87](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/python_vm.py#L87)）。因为 Python 侧用类型对象作字典 key 更直白、不易写错；整数 `opcode` 主要服务于**内核侧**（指令带是 int32 张量，控制器必须用整数跳转），pyvm 不需要。

**Q2**：如果要给 throughput 模式加一条全新指令，需要改 pyvm 的哪一层？

**答**：只改 `demos/throughput/python_vm.py`——新增一个 solver 函数，并把它登记进该文件的 `INSTRUCTION_TO_SOLVER` 表。共享层的 `PyVM_Interpreter` 一行都不用动。

---

### 4.2 公共数值原语：matvec / rms_norm / matvec_with_residual

#### 4.2.1 概念说明

七个 latency solver 之间共享几个底层「积木」。它们在 `megakernels/python_vm.py` 里定义（latency 专属文件里又复制了一份一模一样的，见后文）。理解这三个原语，是读懂任何 solver 的前提。

- **`matvec`**：矩阵-向量乘，但支持**两种切法**。
- **`rms_norm`**：RMS 归一化，Llama 里所有 Norm 都用它。
- **`matvec_with_residual`**：把 split-K 的多段部分和**累加进残差流**。

#### 4.2.2 核心流程

**`matvec` 的两种模式**（这是本节最重要的区分）：

```text
# 模式 A：整段输出（reduce=False）
切输出维 [start:end] 行，对整条 vec 做完整归约
out = einsum(mat[start:end], vec, "o i, i -> o")   # 得到 o 个元素的完整输出

# 模式 B：split-K 归约（reduce=True）
同时切输出维 [start:end] 和归约维 [red_start:red_end]
out = einsum(mat[start:end, red_start:red_end], vec[red_start:red_end], "o i, i -> o")
# 这只是「部分和」——必须把所有 red_idx 遍历累加才得到完整结果
```

为什么要 split-K？因为 `o_proj`/`down_proj` 的归约维度很大（如 8192），内核里会把这条归约维度**切成多段**，让不同 warp 各算一段再累加。pyvm 必须用同样的切法，才能逐段对上。

**`rms_norm`**：先升 float32 算方差，再缩放，最后乘权重：

\[
\text{rms\_norm}(x, w, \varepsilon) = w \cdot x \cdot \frac{1}{\sqrt{\text{mean}(x^2) + \varepsilon}}
\]

注意它**在 float32 下算方差**、最后再回到原精度，这是为了和内核的 RMSNorm 数值行为对齐（内核也在高精度下算 norm）。

**`matvec_with_residual`**：循环遍历归约块，每块调一次 reduce 模式的 `matvec`，把部分和 `+=` 累加进残差张量。

#### 4.2.3 源码精读

[ megakernels/python_vm.py:9-12 ](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/python_vm.py#L9-L12) —— `get_start_end(block_size, block_idx)` 把「块号」翻译成「起止下标」：`start = block_size*block_idx`，`end = start + block_size`。这是所有分块计算的公共算术。

[ megakernels/python_vm.py:15-33 ](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/python_vm.py#L15-L33) —— `matvec`。关键看 `if reduce:` 分支：reduce 模式下，`mat` 和 `vec` **都**被切到 `[red_start:red_end]`，只算部分和；非 reduce 模式只切输出维。两种模式都用 `einsum(mat, vec, "o i, i -> o")` 收尾。

[ megakernels/python_vm.py:36-42 ](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/python_vm.py#L36-L42) —— `rms_norm`。`variance = inp.pow(2).mean(-1, keepdim=True)`，然后 `inp * torch.rsqrt(variance + eps)`，最后乘 `weight`。注意开头 `inp = inp.to(torch.float32)`、结尾 `inp.to(input_dtype)`——先升精度算，再降回去。

[ megakernels/python_vm.py:45-67 ](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/python_vm.py#L45-L67) —— `matvec_with_residual`。`for block_idx in range(...)` 遍历归约块，每块算一次 reduce 模式的 `matvec`，再 `residual[start:end] += matvec_out.to(residual.dtype)`。`.to(residual.dtype)` 这步降精度（通常 bf16），正是和内核「在 bf16 残差流上累加」对齐。

> 旁注：[megakernels/demos/latency/python_vm.py:23-80](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/latency/python_vm.py#L23-L80) 里**重复定义**了一份完全相同的 `get_start_end`/`matvec`/`rms_norm`/`matvec_with_residual`。这是历史遗留的复制（latency solver 直接 `import` 了 latency 文件自己那份）。读源码时把它们当同一套原语即可。

#### 4.2.4 代码实践

**目标**：用纯 torch 验证「split-K 累加 == 整段一次算」，建立对 reduce 模式的信任。

```python
# 示例代码：验证 matvec 两种模式等价
import torch
from einops import einsum
torch.manual_seed(0)

o, i = 32, 48           # 输出 32 维，归约 48 维
mat = torch.randn(o, i)
vec = torch.randn(i)
block_size = 16         # 把归约维切成 16 一段

# 整段一次算（reduce=False 的等价）
full = einsum(mat, vec, "o i, i -> o")

# split-K：遍历归约块累加
acc = torch.zeros(o)
for red_idx in range(i // block_size):
    rs = red_idx * block_size
    re = rs + block_size
    acc += einsum(mat[:, rs:re], vec[rs:re], "o i, i -> o")

print("max diff:", (full - acc).abs().max())   # 应为 0（或极小浮点误差）
```

**预期结果**：`max diff` 为 `0`（理论完全一致；实际因浮点累加顺序可能到 ~1e-6）。这印证了内核用 split-K 切块累加，与一次完整归约在数学上等价——pyvm 用同样的切法，才能和内核逐段对上。

#### 4.2.5 小练习与答案

**Q1**：`matvec(..., reduce=False)` 与 `reduce=True` 的最大区别是什么？

**答**：`reduce=False` 只切**输出维**，对**整条**归约维求和，直接得到完整输出；`reduce=True` 同时切输出维与归约维，只得到**一段部分和**，需要外层循环遍历所有 `reduction_idx` 累加才完整。

**Q2**：为什么 `rms_norm` 要先 `.to(torch.float32)` 再算方差？

**答**：bf16 下算 `mean(x^2)` 精度太差（bf16 只有 ~3 位有效数字），会让归一化尺度本身就不准。先升到 float32 算方差、算完再降回原精度，能和内核高精度 norm 的数值行为对齐，从而让 diff 更小。

---

### 4.3 latency solvers 与 INSTRUCTION_TO_SOLVER 映射

#### 4.3.1 概念说明

`demos/latency/python_vm.py` 里为七条指令各写了一个 solver，最后用一张表 `INSTRUCTION_TO_SOLVER` 把「指令类 → solver 函数」绑死。这张表就是 4.1 里那个解释器循环的「业务字典」。

这里有两个设计要点贯穿全部 solver，先讲清楚：

1. **刻意镜像内核的分块粒度**。solver 不是「一把梭」地用 `F.scaled_dot_product_attention`，而是按 `block_size`/`num_partials`/`partial_idx` 把计算切成和内核**一样大小的块**。否则 pyvm 只是在算「正确的最终结果」，无法暴露内核在**分块边界**上的 bug。
2. **barrier 检查/更新镜像内核的同步**。每个 solver 开头几乎都有一句 `assert op_barriers[...] == <期望值>`，结尾都 `barriers[...] += <增量>`。这些 assert 复刻了内核侧「这条指令必须等前置 op 写够若干次才能开始」的到达计数（arrival count）。pyvm 顺序执行本不会乱序，这些 assert 的真正用途是**给调度器/内核的同步契约做文档化的不变量校验**。

#### 4.3.2 核心流程

七条 solver 的职责一览（与 [U3·L2](u3-l2-latency-instruction-set.md) 的 opcode 表对应）：

```text
opcode 1  layer_norm_matvec_rope_append : RMSNorm → QKV matvec(逐块) → q/k 做 RoPE → k/v 追加 KV cache
opcode 2  partial_attention              : 取本分片的 k/v → QK^T*scale → softmax → softmax@V → 存 (lse, out) 中间量
opcode 3  attention_reduction            : 用 LSE 把多个 partial 的 (lse, out) 合并成最终 attn_out
opcode 4  o_proj_residual                : split-K matvec(o_proj) → 累加进 hidden_states（残差）
opcode 5  layer_norm_double_matvec_silu  : RMSNorm → gate/up 双 matvec → silu(gate)*up → 存 silu_out
opcode 6  down_proj_residual             : split-K matvec(down_proj) → 累加进 hidden_states（残差）
opcode 7  rms_lm_head                    : RMSNorm → lm_head matvec(逐块) → 存 logits
```

attention 这条线（opcode 2 + 3）是数值上最绕的：partial 阶段先算「未归一」的分片输出和它的 log-sum-exp，reduction 阶段再用 LSE 把分片合并。这是因为内核用的是**在线 softmax + 分片归并**（详见 [U9·L1/U9·L3](u9-l3-remaining-ops.md)）；pyvm 必须照搬这套「先分片、后 LSE 合并」的流程，而不是一次 softmax 完事。

#### 4.3.3 源码精读

先看映射表本身：

[ megakernels/demos/latency/python_vm.py:396-404 ](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/latency/python_vm.py#L396-L404) —— `INSTRUCTION_TO_SOLVER` 把七个指令类分别绑到七个函数。这张表正是 `dispatch.py` 注入 `PyVM_Interpreter` 的那张表。

**matvec 类 solver**——以 `o_proj_residual` 为例（opcode 4）：

[ megakernels/demos/latency/python_vm.py:83-104 ](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/latency/python_vm.py#L83-L104) —— 开头 `assert op_barriers[0] == globals.num_attention_heads` 检查前置 attention 已经写够；随后直接调 `matvec_with_residual(...)` 把 `o_proj_weights @ attn_out` 累加进 `hidden_states`；结尾更新本 op 的 barrier。`down_proj_residual`（[L107-124](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/latency/python_vm.py#L107-L124)）结构完全同构，只是换了权重/输入/blocks。两者都依赖 4.2 的 split-K 累加原语。

**rms_norm + matvec 类 solver**——以 `layer_norm_double_matvec_silu` 为例（opcode 5）：

[ megakernels/demos/latency/python_vm.py:127-165 ](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/latency/python_vm.py#L127-L165) —— 先对 `hidden_states` 做 `rms_norm`（用 attn/MLP 共享的那套原语）；再在 `for block_idx in instruction.block_idxs:` 里**逐块**算 `up_matvec` 和 `gate_matvec`（非 reduce 模式，每块独立算一段输出）；`post_silu = F.silu(gate_matvec) * up_matvec` 写进 `silu_out`。注意它是**逐块**写的，和内核逐块 store 的粒度一致。`rms_lm_head`（[L251-274](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/latency/python_vm.py#L251-L274)）是同模式的「RMSNorm + 逐块 matvec 写 logits」。

> 最复杂的 `layer_norm_matvec_rope_append`（opcode 1，[L168-248](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/latency/python_vm.py#L168-L248)）需要把 QKV 投影输出按 `q`/`k`/`v` 区段分别处理：`q`/`k` 段要做 RoPE（`apply_rotary_pos_emb_interleaved`），`k`/`v` 段要写进 KV cache 的当前位置 `pos_id`。它属于 [U8·L4](u8-l4-op-rms-qkv-rope-append.md) 的内容，本讲只点明它同样「逐块 + 更新 barrier」。

**attention 类 solver**——`partial_attention`（opcode 2）与 `attention_reduction`（opcode 3）：

[ megakernels/demos/latency/python_vm.py:277-346 ](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/latency/python_vm.py#L277-L346) —— `partial_attention` 先按 `partial_idx`/`num_partials` 算出本分片负责的 KV token 区间 `[start_token:end_token)`；然后 `qk = einsum(q, k, "h i, k i -> h k")`、`scaled_qk = qk * attn_scale`、`softmax = torch.softmax(scaled_qk, -1)`、`out = softmax @ v`。关键在最后：它**不直接写最终 `attn_out`**，而是把本分片的 `lse` 和 `out` 存进 `attn_lse_intermediates` / `attn_out_intermediates`，留给 reduction 合并。`lse = torch.log2(torch.sum(torch.exp(scaled_qk), -1))` 这里用 `log2`/`exp2` 体系是为了和内核「以 2 为底」的 LSE 约定对齐（见 [U9·L3](u9-l3-remaining-ops.md)）。第 319 行注释也点明：把 softmax 结果降到 16-bit 会引入微小数值差，这正是 diff 不为零的主要来源之一。

[ megakernels/demos/latency/python_vm.py:349-393 ](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/latency/python_vm.py#L349-L393) —— `attention_reduction` 用 LSE 合并多个分片：取 `max_lse`，`adjusted_factors = (lses - max_lse).exp2()`，`reduced = (outs * adjusted_factors.unsqueeze(-1)).sum(1) / new_denominator`。若 `is_terminal` 则写最终 `attn_out`，否则把合并结果作为「新的一个分片」写回中间量（支持多级归并）。合并的数学本质是：

\[
\text{out}_{\text{merged}} = \frac{\sum_j \text{out}_j \cdot 2^{\text{lse}_j - M}}{\sum_j 2^{\text{lse}_j - M}}, \quad M = \max_j \text{lse}_j
\]

#### 4.3.4 代码实践

**目标**：通过源码阅读，确认 pyvm 「逐块 + barrier」的两个不变量。

1. 打开 [latency/python_vm.py](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/latency/python_vm.py)，在 `o_proj_residual`、`layer_norm_double_matvec_silu`、`partial_attention` 三个 solver 里各找到「开头的 assert」与「结尾的 `barriers[...] +=`」。
2. 列一张表：每个 solver 检查的 barrier 期望值（如 `num_attention_heads`、`128`、`512`、`4`）分别对应 [U9·L2](u9-l2-cross-op-global-barriers.md) 里哪个跨 op 屏障契约。
3. **观察**：这些 assert 在 pyvm 顺序执行里**永远会成立**（因为 pyvm 不会乱序）。所以它们的作用是「文档化内核同步契约 + 在 mk 调度被改动时由 pyvm 侧最先暴露问题」。

**预期结果**：你能把每个 assert 的右值解释成「前置 op 一共要写满多少个 block / 多少个 head」。这趟「读 assert」是理解内核同步最快的入口。

#### 4.3.5 小练习与答案

**Q1**：`partial_attention` 为什么不直接写 `attn_out`，而是写 `attn_lse_intermediates` / `attn_out_intermediates`？

**答**：因为一个 head 的注意力要对**整条序列**做 softmax，而序列被切成了多个 KV 分片，每个分片只看到一部分 key/value。只有把所有分片的 `(lse, out)` 都收齐，`attention_reduction` 才能用 LSE 把它们合并成「等价于对全序列做 softmax」的结果。直接写 `attn_out` 会丢掉归一化所需的分母信息。

**Q2**：`o_proj_residual` 和 `down_proj_residual` 都调 `matvec_with_residual`，两者区别在哪？

**答**：四件事不同：权重张量（`o_proj_weights` vs `down_proj_weights`）、输入激活（`attn_out` vs `silu_out`）、`block_size`（`o_proj_block_size` vs `down_proj_block_size`）、以及 barrier 检查的期望到达数。内核侧它们复用同一个 `MatVecAddOp` 模板（见 [U9·L3](u9-l3-remaining-ops.md)），pyvm 侧也复用同一个原语函数。

---

### 4.4 diff_test.py 对照流程与 diff() 指标

#### 4.4.1 概念说明

有了 pyvm，差分测试的主流程就很直白了：**造两块内容相同的黑板 `gpy` 和 `gmk`，让 pyvm 在 `gpy` 上跑、mk 在 `gmk` 上跑，再逐字段对比**。`diff_test.py` 就是这个流程的脚本化。

对比用一个指标函数 `diff_tensors`，对每个张量打印两个数：

- **`max adiff`**：绝对误差的最大值 \(\max |a - b|\)，反映「最坏情况偏差了多少」。
- **`mean rdiff`**：平均相对误差。定义是**对称相对误差**

\[
\text{rdiff} = \frac{2\,|a - b|}{|a| + |b| + 10^{-6}}
\]

分子是 2 倍绝对误差，分母是两者绝对值的平均（`+1e-6` 防除零）。它对「大数的小误差」和「小数的相对误差」都比单纯 `adiff` 更公平。

#### 4.4.2 核心流程

`diff_test.py` 的 `main` 大致分五段：

```text
① 加载模型 + 构造解释器
   model = LlamaForCausalLM.from_pretrained(...)
   builder   = make_schedule_builder(setting)          # latency 调度器
   mk_interp = make_mk_interpreter(...)                # 内核解释器
   py_interp = make_pyvm_interpreter(...)              # pyvm 解释器

② 造两块黑板 gpy / gmk（同模型、同初值）
   spy = builder.build(model, layer_limit, ...)        # 含 globs = gpy
   smk = builder.with_new_globals(spy, model)          # 换一套 globs = gmk
   # 用同样的随机种子填 hidden_states / k_cache / v_cache，保证两边输入一致

③ 准备指令并分配到 SM
   instructions = spy.get_linear_instructions()
   assigned_to_sms = assign_to_sms(...)                # 见 [U4·L1]
   tensorize_instructions(gpy, assigned_to_sms)        # 序列化成指令张量，见 [U4·L2]
   tensorize_instructions(gmk, assigned_to_sms)

④ 执行
   pyvm_interpreter.interpret(gpy, instructions)       # pyvm 跑 gpy
   mk_interpreter.interpret(gmk)                       # 内核跑 gmk

⑤ 对照
   gpy.diff(gmk)                                        # 逐张量打印 max adiff / mean rdiff
```

注意第 ② 段有个**关键细节**：`k_cache`/`v_cache` 原本来自同一个 model 对象，`gpy` 和 `gmk` 共享同一块内存。脚本特意 `clone()` 了一份（[diff_test.py:117-118](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/scripts/diff_test.py#L117-L118)），否则一边的 KV append 会污染另一边，对照就失效了。

#### 4.4.3 源码精读

**对照执行 + diff 的核心几行**：

[ megakernels/scripts/diff_test.py:209-226 ](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/scripts/diff_test.py#L209-L226) —— `skip_pyvm=False` 时先 `pyvm_interpreter.interpret(gpy, instructions)`，再 `mk_interpreter.interpret(gmk)`，最后 `gpy.diff(gmk)`。`skip_pyvm` 开关让你能只跑 mk（比如只测内核耗时）。

**`BaseGlobals.diff` —— 决定「比哪些字段」**：

[ megakernels/instructions.py:54-66 ](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/instructions.py#L54-L66) —— 遍历 `BaseGlobals` 的每个字段，**跳过**：非张量、名字含 `"weights"` 的（权重两边本就来自同一份模型，比了没意义）、`rope_cos`/`rope_sin`（预算好的常量）、以及可选地跳过 KV cache（`skip_kv_cache`）。剩下的就是**激活缓冲**（`hidden_states`、`post_ln_rope_q`、`attn_out`、`silu_out`、`logits` 等）——这些正是每一步 op 的「输出」，正是我们想验证的对象。

**`diff_tensors` —— 两个指标的算法**：

[ megakernels/instructions.py:72-80 ](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/instructions.py#L72-L80) —— 先 `.float()` 升精度（避免 bf16 比较时再丢精度），`diff = a - b`，`adiff = diff.abs()`，`rdiff = 2 * adiff / (a.abs() + b.abs() + 1e-6)`，打印每个张量的 `max adiff` 与 `mean rdiff`。

**脚本配置**：

[ megakernels/scripts/diff_test.py:22-44 ](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/scripts/diff_test.py#L22-L44) —— `ScriptConfig` 关键参数：`model`（默认 `Llama-3.2-1B-Instruct`）、`layer_limit`（默认 `1`，只跑一层，方便快速对照）、`skip_pyvm`、`setting`（默认 `latency`）、`stop_after_op`/`start_after_op`（可只对照某个 op 子段）。命令行用 pydra 的 `key=value` 风格（见 [U1·L3](u1-l3-build-and-run-llama-demo.md)）。

#### 4.4.4 代码实践

**目标 1（无需 GPU，可直接运行）**：亲手算一遍 `adiff`/`rdiff`，建立对两个指标的手感。

```python
# 示例代码：复刻 diff_tensors 的两个指标
import torch
torch.manual_seed(0)

a = torch.randn(1000)                      # 假装是 pyvm 输出
b = a + 0.01 * torch.randn(1000)           # 假装是 mk 输出（带微小数值差）

a, b = a.float(), b.float()
adiff = (a - b).abs()
rdiff = 2 * adiff / (a.abs() + b.abs() + 1e-6)
print(f"max adiff: {adiff.max()}, mean rdiff: {rdiff.mean()}")
```

把扰动从 `0.01` 改成 `0.001` 重跑，**观察**：`max adiff` 大致按比例下降；`mean rdiff` 也随之下降。再故意造一个「大数的小误差」点（如 `b[0] = a[0] * 1.001`，`a[0]` 很大），观察 `max adiff` 会变大但该点的 `rdiff` 仍很小——这正是 `rdiff` 存在的意义。

**目标 2（需 GPU + 已编译 mk + 已下载模型权重，待本地验证）**：跑真实对照。

```bash
# 默认就对照 1 层（layer_limit=1），1B 模型，latency 设置
python -m megakernels.scripts.diff_test
```

**需要观察的现象**：脚本会先打印 `pyvm time` 与 `mk time`，最后逐张量打印一行行：

```text
hidden_states: max adiff: <...>, mean rdiff: <...>
post_ln_rope_q: max adiff: <...>, mean rdiff: <...>
attn_out: max adiff: <...>, mean rdiff: <...>
silu_out: max adiff: <...>, mean rdiff: <...>
logits: max adiff: <...>, mean rdiff: <...>
...
```

**两个指标的含义与可接受范围**：

- `max adiff`：该张量任意一个元素的最大绝对偏差。它直接反映「最坏那个点偏了多少」。由于内核在 bf16 下累加、split-K 的归约顺序也与 pyvm 不同，这个值**通常不会是 0**；典型地它会落在「该张量数值量级的 \(10^{-3}\) 量级」附近（bf16 的相对精度约 \(2^{-8} \approx 4\times10^{-3}\)，但多数元素远好于此）。
- `mean rdiff`：该张量所有元素对称相对误差的均值。它把「大数的小误差」和「小数」放到同一尺度比较。**这是判断「整体是否对齐」的主指标**：对正确的内核，`mean rdiff` 通常应**远小于 \(10^{-2}\)**（经验上常见在 \(10^{-3}\) 量级甚至更小）。若某个张量的 `mean rdiff` 显著高于其它（比如跑到 \(10^{-1}\) 甚至更大），基本说明对应的 op 有 bug。

> 阈值说明：项目源码里**没有硬编码阈值/没有断言**——`diff()` 只打印不 `assert`（见 [instructions.py:66](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/instructions.py#L66)）。判定「可接受」靠人工看：`mean rdiff` 全部在 \(10^{-3}\) 量级、`max adiff` 与张量量级相称即为正常；某字段突刺即定位到对应 op。**具体数值待本地验证**，因为它依赖 GPU 型号、模型、`layer_limit` 等运行环境。

#### 4.4.5 小练习与答案

**Q1**：`BaseGlobals.diff` 为什么跳过名字含 `"weights"` 的字段？

**答**：权重两边都来自同一份 `model`（同一次 `from_pretrained`），本就是同一个张量，比较它们永远得到 0，没有信息量。diff 关心的是「op 的输出激活是否一致」，所以只比激活缓冲。

**Q2**：为什么 `diff_tensors` 要先把两个张量 `.float()`？

**答**：激活通常存成 bf16。如果直接用 bf16 算 `a - b`，做差这一步本身又会丢一次精度，让差异失真。先升到 float32 再做差，得到的是「真实差异」的更准确估计。

**Q3**：如果只怀疑 `o_proj` 这一个 op 算错了，怎么用脚本聚焦到它？

**答**：用 `stop_after_op` / `start_after_op`（[diff_test.py:28-29](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/scripts/diff_test.py#L28-L29)）把指令序列截断到 o_proj 附近再对照；或直接看 `hidden_states` 这个字段（o_proj 的残差写回它）的 `mean rdiff` 是否突刺。

---

## 5. 综合实践

把本讲的三块知识（解释器、solver、diff 指标）串起来，做一次「**给 pyvm 加一条诊断**」的小任务：

1. **读懂执行链**：从 [dispatch.py:41-42](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/dispatch.py#L41-L42) 出发，确认 `make_pyvm_interpreter("latency")` 注入的是 [latency/python_vm.py:396-404](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/latency/python_vm.py#L396-L404) 那张表。
2. **加一行诊断**：在 `o_proj_residual`（[latency/python_vm.py:83-104](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/latency/python_vm.py#L83-L104)）的 `matvec_with_residual(...)` 调用之后，临时加一行打印本 op 写入前后的 `globals.hidden_states.abs().mean()`（这是**示例代码**，仅供本地调试，勿提交）。
3. **对照运行**：跑 `python -m megakernels.scripts.diff_test`（待本地验证），结合 4.4.4 的指标解读，确认 `hidden_states` 的 `mean rdiff` 落在 \(10^{-3}\) 量级。
4. **解释**：用一句话说明「为什么 pyvm 里加这行 print 不会影响 `gpy.diff(gmk)` 的结果」——因为 print 只读不写 `globs`，黑板内容不变。

> 这条链路正是项目作者开发内核时的日常：改一个 op → 跑 diff_test → 看哪个字段突刺 → 回到对应 solver/内核定位。

---

## 6. 本讲小结

- `PyVM_Interpreter` 是 megakernel 虚拟机的 **Python 翻版**：一个「按 `type(instruction)` 查表分发」的循环（[python_vm.py:83-96](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/python_vm.py#L83-L96)），业务全在 `instruction_to_solver` 表里。
- 共享原语 `matvec` 有**两种模式**：整段输出 vs split-K 归约；`rms_norm` 在 float32 下算方差；`matvec_with_residual` 把 split-K 部分和累加进残差（[python_vm.py:9-67](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/python_vm.py#L9-L67)）。
- latency 的 `INSTRUCTION_TO_SOLVER` 表（[latency/python_vm.py:396-404](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/latency/python_vm.py#L396-L404)）把七条指令各绑一个 solver，solver **刻意镜像内核的分块粒度与 barrier 同步**——这是差分测试有意义的根本。
- attention 走「partial（存 lse+out 中间量）→ reduction（LSE 合并）」两步（[latency/python_vm.py:277-393](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/latency/python_vm.py#L277-L393)），照搬内核的在线 softmax + 分片归并。
- `diff_test.py` 用两块同初值黑板 `gpy`/`gmk` 分别跑 pyvm/mk，再 `gpy.diff(gmk)`（[diff_test.py:209-226](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/scripts/diff_test.py#L209-L226)）。
- `diff()` 只比激活缓冲（跳过权重/常量/KV cache），`max adiff` 看最坏偏差、`mean rdiff`（对称相对误差）看整体对齐度（[instructions.py:54-80](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/instructions.py#L54-L80)）；正确内核的 `mean rdiff` 通常在 \(10^{-3}\) 量级。

---

## 7. 下一步学习建议

- **对比 throughput 的 pyvm**：读 [demos/throughput/python_vm.py](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/throughput/python_vm.py)，看批处理设置下 `INSTRUCTION_TO_SOLVER` 与 latency 版的差异，体会「同一套解释器骨架服务不同指令集」。
- **回到内核侧验证**：结合 [U9·L3](u9-l3-remaining-ops.md) 的 attention_reduction LSE 推导，对照本讲 `attention_reduction` 的 Python 实现，确认两边公式逐项一致——这是「pyvm 可信」的最终背书。
- **上手扩展**：尝试给 latency 加一条新指令（如把 `RMS_LM_Head` 拆成两段），按「定义指令类 → 写 solver → 登记进表 → 跑 diff_test」四步走通，验证 `mean rdiff` 仍合格。
- **性能侧**：`diff_test.py` 同时打印了 `pyvm time` 与 `mk time`——可以顺手比较两者，体会「常驻 megakernel 相比 Python 逐 op 解释」的加速比（这也是项目低延迟主张的量化体现）。
