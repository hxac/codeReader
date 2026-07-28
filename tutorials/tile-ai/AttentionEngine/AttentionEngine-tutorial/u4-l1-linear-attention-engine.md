# 线性注意力引擎 LinearAttentionEngine

## 1. 本讲目标

前面几讲我们一直在讲 **transformer 注意力**：用 `score_mod`/`mask_mod`/`online_func` 描述 \(\mathrm{softmax}(QK^\top)V\) 这一族计算。本讲转向 AttentionEngine 支持的另一条路线——**线性注意力（linear attention）**，它由一个独立的引擎 `LinearAttentionEngine` 驱动。

本讲学完后，你应该能够：

1. 说清楚**线性注意力与 transformer 注意力在计算结构上的根本差异**：前者把 \(QK^\top\) 这个 \(O(T^2)\) 的注意力矩阵拆成「分块递推的状态 \(H\) + 块内注意力」，从而把复杂度降到 \(O(T)\)。
2. 读懂 `LinearAttentionEngine` 这个入口：构造即编译、md5 缓存、importlib 动态加载，把生成的 kernel 挂成可 `mod(q,k,v,decay)` 调用、可 `.backward()` 的 PyTorch 模块。
3. 掌握四个 mod（`q_mod`/`k_mod`/`v_mod`/`decay_mod`）如何被 `lower_linear.py` 分别符号化并降级，理解「**fuse 进 kernel（tl 片段）**」与「**在宿主机执行（pytorch 片段）**」两种降级落点的区别。
4. 在源码中认出**双 kernel 骨架**：`chunk_fwd_h`（计算跨块状态 \(H\)）与 `chunk_o`（聚合输出 \(O\)），以及反向的四段 kernel。

## 2. 前置知识

### 2.1 线性注意力在算什么

transformer 注意力的核心是一张 \([T,T]\) 的注意力矩阵 \(A=QK^\top\)，再乘 \(V\) 得到输出。当序列长度 \(T\) 很大时，这张矩阵既费显存又费算力（\(O(T^2)\)）。

线性注意力的思路是：把 \(Q,K,V\) 切成若干长度为 \(BT\) 的块，用一个**可递推的中间状态 \(H\)**（也叫 memory / state）代替完整的注意力矩阵。数学上，带衰减（gate / decay）的线性注意力可写成如下递推：

\[
S_t = \gamma_t \odot S_{t-1} + K_t^\top V_t,\qquad O_t = Q_t S_t
\]

其中 \(\gamma\) 是衰减（gate），\(S\) 是累积状态。若把序列分成块，第 \(c\) 块的输出由两部分组成：

- **块间（inter-chunk）**：\(O\) 的「历史」部分 = \(Q_c \cdot H_{c-1}\)，其中 \(H_{c-1}\) 是到上一块为止累积的状态。
- **块内（intra-chunk）**：\(O\) 的「局部」部分 = \((Q_c K_c^\top \odot M_\gamma)\, V_c\)，\(M_\gamma\) 是块内衰减掩码。

这正好对应本讲将要看到的**两个 kernel**：

- `chunk_fwd_h`：逐块递推并落盘状态 \(H\)（形状 \([B,H,NT\cdot D, DV]\)）。
- `chunk_o`：用 \(H\) 算块间部分，再叠加块内注意力，得到最终输出 \(O\)。

> 直觉记忆：transformer 注意力是「先算整张 \(QK^\top\) 再乘 \(V\)」；线性注意力是「先把 \(K^\top V\) 累积进状态 \(H\)，再用 \(Q\) 去读 \(H\)」。复杂度从 \(O(T^2)\) 降到 \(O(T)\)。

### 2.2 你需要带走的前置概念

本讲承接 u3-l3（引擎入口：分发、编译、缓存），并复用 u2 系列建立的符号 IR 知识。请确认你已经了解：

- **符号降级（lowering）**：把用户写的 Python 函数用符号诱饵跑一遍，得到 DAG，再用 `generate_tl_from_dag` 发射成代码（u2-l3）。
- **`to_tl` 布尔开关**：同一个 DAG 既可发射成 TileLang 片段（`to_tl=True`，fuse 进 kernel），也可发射成 PyTorch 片段（`to_tl=False`，在宿主机执行）（u2-l4）。
- **md5 缓存 + importlib 动态加载**：生成源码落盘成 `cache/<hash>.py`，再 `exec_module` 取出符号（u3-l3）。

线性注意力与 transformer 注意力的一个关键差异：**transformer 注意力的 IR 基本是逐元素标量 `SymbolScalar`**（`score_mod` 对每个 score 变换）；**线性注意力的 mod 经常作用在整块张量上**，所以本讲你会看到 `SymbolicArray`（带形状、可表达 `K^T V` 这种块运算的符号对象）大量出现。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [attention_engine/attn_engine/linear_attn_engine.py](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/attn_engine/linear_attn_engine.py) | 引擎入口 `LinearAttentionEngine`：构造即编译、md5 缓存、importlib 加载。 |
| [attention_engine/core/lower/lower_linear.py](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/lower/lower_linear.py) | 降级编排：`lower_tl` 总入口 + `lowerKmod`/`lowerVmod`/`lowerFusedVmod`/`lowerDecaymod`/`lowerQmod`/`lowerQmodFused`。 |
| [attention_engine/core/template/linear_attn_template.py](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/template/linear_attn_template.py) | Jinja2 模板包装类 `TlLinearAttnTemplate`。 |
| [attention_engine/core/template/tl_template/linear/linear_tl.py](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/template/tl_template/linear/linear_tl.py) | TileLang 骨架：`chunk_fwd_h`/`chunk_o`/反向四 kernel + `LinearAttention` autograd 接口。 |
| [attn_script/retention_linear.py](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attn_script/retention_linear.py) | 用户示例：retention 线性注意力（仅用 `q_mod`+`decay_mod`）。 |

一句话定位：`linear_attn_engine.py` 是「**编译器驱动**」，`lower_linear.py` 是「**降级编排者**」（它同时认识 transform/codegen/template 三层），`linear_tl.py` 是「**带洞的骨架程序**」，`retention_linear.py` 是「**最小可跑的用户输入**」。

---

## 4. 核心概念与源码讲解

### 4.1 LinearAttentionEngine 入口：构造即编译

#### 4.1.1 概念说明

`LinearAttentionEngine` 与 transformer 的 `AttentionEngine` 是平行的两个入口。它的构造签名暴露的是线性注意力的四个可定制点：

- `q_mod(q, custom_io)`：对查询 \(Q\) 的逐元素/逐块变换（常见是乘 \(\frac{1}{\sqrt{D}}\) 缩放）。
- `k_mod(k, custom_io)`：对键 \(K\) 的变换。
- `v_mod(v, custom_io)`：对值 \(V\) 的变换。
- `decay_mod(decay, custom_io)`：对衰减 \(\gamma\) 的变换（retention 里取 `log`，配合后续 cumsum 得到累积衰减）。

这四个 mod 与 transformer 的 `score_mod`/`online_func` 是「**对偶**」关系：transformer 注意力把可定制性放在「分数变换 + 行级在线算法」上；线性注意力把可定制性放在「对 \(Q/K/V/\gamma\) 的逐输入预处理」上。用户只挑自己需要的 mod 传，其余传 `None`。

#### 4.1.2 核心流程

构造一次引擎，依次发生三件事（与 u3-l3 的 `AttentionEngine` 几乎同构）：

1. **降级编译**：调用 `lower_tl(...)` 把用户的 mod 编译成一份完整的 TileLang 源码字符串 `tl_code`。
2. **md5 缓存**：对 `tl_code` 取 md5，落盘到 `attn_engine/cache/<hash>.py`；命中则跳过写盘。
3. **importlib 动态加载**：`exec_module` 执行该文件，取出符号 `linear_attention`，挂到 `self.attention`。

运行期 `__call__` 只是把真实张量转发给 `self.attention`。注意取出的符号名是 **`linear_attention`**（对应骨架末尾的 `linear_attention = LinearAttention.apply`），这是引擎与模板之间的隐式契约。

#### 4.1.3 源码精读

引擎类与构造函数：[linear_attn_engine.py:10-13](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/attn_engine/linear_attn_engine.py#L10-L13) 定义了 `LinearAttentionEngine.__init__`，它把所有构造参数原样转交给 `_compile_tl`。注意它的可定制点就是 `q_mod`/`k_mod`/`v_mod`/`decay_mod`/`custom_io`，外加 autotuner 三件套（`tune`/`tune_filename`/`tune_bwd`）。

`_compile_tl` 调用降级并缓存：[linear_attn_engine.py:21-34](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/attn_engine/linear_attn_engine.py#L21-L34) 中，`tl_code = lower_tl(...)` 是整条编译链的入口；`self.tl_code = tl_code` 这行特意保留了生成源码，方便调试导出。

md5 缓存 + importlib 加载：[linear_attn_engine.py:40-52](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/attn_engine/linear_attn_engine.py#L40-L52)。这段与 transformer 引擎完全一致：`code_hash = hashlib.md5(tl_code.encode()).hexdigest()` 决定缓存文件名，`spec_from_file_location` + `exec_module` 把文件加载成模块，最后 `self.attention = tl_attn.linear_attention` 取出可调用对象。

> 一个容易忽略的细节：源码里有几行被注释掉的 `exec(tl_code, globals(), local_vars)`（[linear_attn_engine.py:36-39](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/attn_engine/linear_attn_engine.py#L36-L39)）。这说明项目早期曾在内存里直接 `exec` 生成代码，后来改为「落盘 + importlib」以便利用文件级缓存。调试时你可以仿照这种思路，把 `self.tl_code` 写到本地 `.py` 文件里人工阅读。

#### 4.1.4 代码实践

**实践目标**：在不跑 GPU 的前提下，确认「构造即编译」确实生成了一份可读的 Python 源码。

**操作步骤**：

1. 打开 `attn_script/retention_linear.py`，定位到 `__main__` 块（[retention_linear.py:65-84](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attn_script/retention_linear.py#L65-L84)）。
2. 想象（或在本机配好 TileLang 后真正）执行：构造 `mod = LinearAttentionEngine(...)`。
3. 构造完成后，访问 `mod.tl_code`，把它写到一个文件，例如 `retention_linear_gen.py`。
4. 在该生成文件里搜索 `def chunk_fwd_h`、`def chunk_o`、`class LinearAttention`，确认它们都存在。

**需要观察的现象**：生成的源码里，`LinearAttention.forward` 的函数体应包含 `decay_1 = torch.log(decay)`（来自 `decay_mod`）和形如 `bq = bq * scale` 的片段（来自 `q_mod`）。

**预期结果**：你能拿到一份完整的、可被 `exec_module` 执行的 Python 文件，它就是双 kernel + autograd 接口的最终产物。

> 待本地验证：受限于 TileLang/cuda 环境，本实践是否可在本机完整运行取决于 u1-l2 描述的环境是否就绪。即便无法运行，阅读 `lower_tl` 也能在脑中重建这份源码。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `LinearAttentionEngine` 没有 `backend` 参数（而 `AttentionEngine` 有 `tl`/`cute` 之分）？

> **答案**：线性注意力目前只实现了 TileLang（tl）后端，没有 CuTe 路径（对照 u5-l1 的 cute 后端只服务 transformer 注意力），所以不需要 backend 分流。`_compile_tl` 直接调用 `lower_tl`。

**练习 2**：如果把同一份 `q_mod`/`decay_mod` 用**相同形状**连续构造两次 `LinearAttentionEngine`，第二次会重新生成代码吗？

> **答案**：不会。第二次 `lower_tl` 仍会执行并得到相同的 `tl_code`，md5 相同，`os.path.exists(file_path)` 命中后跳过写盘，但仍会 `exec_module` 重新加载（这一步在内存里重复，但耗时可忽略）。

---

### 4.2 双 kernel 骨架：chunk-h 与 chunk-o

#### 4.2.1 概念说明

线性注意力不能用 transformer 那种「一个大 kernel 跑完整个 `online_func`」的方式，因为它的核心是**跨块递推的状态 \(H\)**。\(H\) 必须先被算出来并落盘，才能被输出阶段读取。所以 AttentionEngine 把前向拆成两个独立的 TileLang kernel：

- **h-kernel（`chunk_fwd_h`）**：输入 \(K,V,g\)（衰减），逐块递推状态 \(H\)，写到全局缓冲 `h`，形状 \([B,H,NT\cdot D, DV]\)。
- **o-kernel（`chunk_o`）**：输入 \(H,Q,K,V,g\)，计算 \(O = \underbrace{Q H_{\text{prev}}}_{\text{块间}} + \underbrace{(QK^\top\odot M_\gamma)V}_{\text{块内}}\)。

反向则更碎，拆成 **dh / dqkg / dv** 三个 kernel（外加重算 h），合计四段。这套「chunk-h + chunk-o」结构直接借鉴自 [flash-linear-attention](https://github.com/sustcsonglin/flash-linear-attention) 库的 chunk 算法。

#### 4.2.2 核心流程

以 h-kernel 为例，它的单线程块逻辑（伪代码）：

```
# 每个 thread block 负责一个 (BK, BV) 切片 + 一个 (batch, head)
b_h = 0                                  # 片上状态，跨块复用
for i_t in range(NT):                    # 遍历时间块
    落盘 b_h → h[当前块]                 # 先把「到上一块为止」的状态写回
    glast = g[本块末位]                  # 累积衰减（cumsum 后）
    b_h *= exp2(glast * LOG2E)           # 用块末衰减整体衰减历史状态
    b_k = load K[本块]; 应用 k_mod       # k_mod 在这里 fuse
    # 块内按位置衰减 k，转置后与 V 做矩阵乘，累加进 b_h
    b_kt = b_k^T * exp2((glast - g) * LOG2E)
    b_h += b_kt @ V[本块]
```

o-kernel 的逻辑：

```
# 每个 thread block 负责一个 (BV, 时间块) 切片 + 一个 (batch, head)
bo = 0
for ik in range(NK):                     # 遍历 D 维切块
    bq = load Q[本块, ik 切块]; 应用 q_mod   # q_mod 在这里 fuse
    bs = bq @ K^T                         # 块内注意力矩阵 [BT,BT]
    bo += bq @ H_prev[ik 切块]            # 块间：读上一块状态
bo *= exp2(g * LOG2E)                     # 块内整体衰减
bs *= exp2((g_i - g_j) * LOG2E); 下三角化  # 块内衰减掩码
bo += (v_mod) bs @ V                      # 块内贡献
写回 bo → O
```

注意衰减 \(g\) 的用法：它在进入 kernel 前先经过 `decay_mod`（如 `log`）再做**块内前缀和**（`chunk_local_cumsum_scalar`），所以 `g[t]` 实际是「到本块第 t 步为止的累积对数衰减」。`LOG2E = 1.44269504` 是为了用 GPU 快速指令 `exp2`：\(\exp(x) = \mathrm{exp2}(x\cdot \log_2 e)\)，这正是 u2-l4 讲过的换底技巧。

#### 4.2.3 源码精读

**衰减的块内前缀和**：[linear_tl.py:25-49](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/template/tl_template/linear/linear_tl.py#L25-L49)。这是一个用 triton 写的辅助 kernel `chunk_local_cumsum_scalar`，它对每个长度为 `BT` 的块独立做 `cumsum`（注意是 *local*——每块重置），把逐点的 `decay_mod` 输出转成「块内累积衰减」，供 h/o kernel 的 `exp2(...*LOG2E)` 使用。

**h-kernel 的递推循环**：[linear_tl.py:172-211](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/template/tl_template/linear/linear_tl.py#L172-L211)。关键几行：
- `b_glast[0] = g[bb,bhead,(i_t+1)*BT-1]`（[L183](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/template/tl_template/linear/linear_tl.py#L183)）取块末累积衰减。
- `b_h[i0,i1] *= T.exp2(b_glast[0] * LOG2E)`（[L193-L194](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/template/tl_template/linear/linear_tl.py#L193-L194)）整体衰减历史状态。
- `{{k_mod_expr_fused_h | indent(20)}}`（[L201](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/template/tl_template/linear/linear_tl.py#L201)）是 Jinja2 占位符——`v_mod` 在 h-kernel 里的 fuse 注入点（命名见 4.3 的解释）。
- `b_kt[i0,i1] = b_k[i1,i0]*T.exp2((b_glast[0]-b_g[i1]) * LOG2E)`（[L204-L205](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/template/tl_template/linear/linear_tl.py#L204-L205)）块内逐位置衰减并转置 \(K\)。
- `T.gemm(b_kt, b_v_shared, b_h, ...)`（[L207](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/template/tl_template/linear/linear_tl.py#L207)）把 \(K^T V\) 累加进状态。

**o-kernel 的块间+块内聚合**：[linear_tl.py:349-388](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/template/tl_template/linear/linear_tl.py#L349-L388)。关键几行：
- `{{q_mod_expr | indent(20)}}`（[L359](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/template/tl_template/linear/linear_tl.py#L359)）`q_mod` 的 fuse 注入点（紧跟 `T.copy(bq_shared, bq)` 之后，借助 inplace 复用把结果写回 `bq`）。
- `T.gemm(bq, bk_shared, bs, transpose_B=True, ...)`（[L361](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/template/tl_template/linear/linear_tl.py#L361)）算块内注意力矩阵 \(QK^T\)。
- `T.gemm(bq, b_state_shared, bo, ...)`（[L362](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/template/tl_template/linear/linear_tl.py#L362)）算块间部分 \(Q H_{\text{prev}}\)。
- `{{v_mod_expr_fused_o | indent(16)}}`（[L381](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/template/tl_template/linear/linear_tl.py#L381)）`v_mod` 在 o-kernel 的 fuse 注入点。
- `T.gemm(bs_cast, bv_shared, bo, ...)`（[L385](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/template/tl_template/linear/linear_tl.py#L385)）叠加块内贡献 \((QK^T\odot M_\gamma)V\)。

**autograd 接口把两个 kernel 串起来**：[linear_tl.py:1129-1156](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/template/tl_template/linear/linear_tl.py#L1129-L1156)。`forward` 先执行各 mod 的宿主机片段（`{{decay_mod_expr}}`、`{{k_mod_expr}}`、`{{v_mod_expr}}`），再做 `decay_cumsum = chunk_local_cumsum_scalar(...)`，最后依次调用 `chunk_fwd_h_mod(...)` 得到 `h`、`chunk_fwd_o_mod(h, q, k, v, decay_cumsum, ...)` 得到 `o`。`h` 是块间传递的中间张量，不返回给用户，但 `ctx.save_for_backward(q, k, v, decay, ...)` 保存了反向重算所需的输入。

> 注意 `forward` 的签名 `forward(ctx, q, k, v, decay, {{custom_inputs_list}})`（[L1130](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/template/tl_template/linear/linear_tl.py#L1130)）：线性注意力的第四个位置参数固定是 **decay**，这解释了 `do_bench_retention_linear` 为何以 `linear_attention(q1, k1, v1, g1)` 形式调用。

#### 4.2.4 代码实践

**实践目标**：在源码层面把「双 kernel」的依赖关系画清楚。

**操作步骤**：

1. 在 `linear_tl.py` 中找到 `class LinearAttention(torch.autograd.Function)`（[L1127](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/template/tl_template/linear/linear_tl.py#L1127)）。
2. 阅读 `forward`（[L1129-L1156](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/template/tl_template/linear/linear_tl.py#L1129-L1156)），列出它依次调用了哪些已编译模块（`chunk_fwd_h_mod`、`chunk_fwd_o_mod`）。
3. 找到这两个模块的「工厂函数」定义：`chunk_fwd_h`（[L114](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/template/tl_template/linear/linear_tl.py#L114)）与 `chunk_o`（[L283](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/template/tl_template/linear/linear_tl.py#L283)），记下它们各自的输入 `T.Buffer` 形参顺序。
4. 画一条数据流：`K,V,g → chunk_fwd_h → h → chunk_o（连同 Q,K,V,g）→ O`。

**需要观察的现象**：`chunk_o` 的形参里既有 `h`（来自 h-kernel 的产物），又有 `k`、`v`（块内注意力要重算 \(QK^T\)、再乘 \(V\)），所以 \(K,V\) 在前向被两个 kernel 各读一次。

**预期结果**：你得出的依赖图应说明——**h-kernel 是 o-kernel 的前置依赖**，二者通过中间张量 `h` 解耦；这也意味着 h-kernel 必须先写完整个 `h`，o-kernel 才能开始（两个独立 kernel 的天然同步点）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `chunk_fwd_h` 的输出 `h` 形状是 `(batch, head, NT*dim, dimv)` 而不是 `(batch, head, dim, dimv)`？

> **答案**：因为每个时间块结束时都要落盘一份状态 \(H_c\)（供 o-kernel 读取「到该块为止」的历史），所以状态维度被扩张了 `NT` 倍：`NT*dim`。o-kernel 在第 `c` 块时读取 `h[..., by*dim:(by+1)*dim, ...]`，即上一块的状态。

**练习 2**：`exp2(x * LOG2E)` 与 `exp(x)` 数值上是什么关系？为什么不用 `exp`？

> **答案**：\(\mathrm{exp2}(x\cdot \log_2 e) = 2^{x\log_2 e} = e^x = \exp(x)\)，二者完全等价。用 `exp2` 是因为 GPU 上 `exp2` 指令通常比 `exp` 更快（u2-l4 的换底优化）。模板顶部定义 `LOG2E = 1.44269504` 即 \(\log_2 e\)。

---

### 4.3 q/k/v/decay mod 的前向与反向降级

#### 4.3.1 概念说明

`lower_linear.py` 是线性注意力的降级编排者。它把用户的四个 mod 分别符号化、生成代码片段，再填进模板的占位符。理解本节的关键是区分**两种降级落点**：

| 落点 | 生成方式 | 注入位置 | 典型 mod |
| --- | --- | --- | --- |
| **fuse 进 kernel（tl 片段）** | `generate_tl_from_dag([new_x])`（`to_tl` 默认 True） | kernel 内部循环，作用在 fragment 上 | `q_mod`（`lowerQmodFused`） |
| **宿主机执行（pytorch 片段）** | `generate_tl_from_dag([new_x], to_tl=False)` | `LinearAttention.forward/backward` 函数体，作用在整张张量上 | `k_mod`/`v_mod`/`decay_mod` |

为什么有的 fuse、有的不 fuse？fuse 进 kernel 能省一次全局内存往返，但要求 mod 的计算只依赖**当前 fragment 内**的数据（如逐元素缩放 \(Q\cdot\frac1{\sqrt D}\)）；而 `decay_mod` 的 `log` 之后还要做**跨步 cumsum**，这是 fragment 内做不了的，必须在宿主机先算好再喂给 kernel。

另一个重点是**反向降级**：每个 mod 不仅生成前向片段，还用 `SymbolScalar.backward`（u2-l1/u2-l2 讲过的手写反向模式 autodiff）生成对应的 `*_bwd_expr` 片段，注入到 `LinearAttention.backward`。

#### 4.3.2 核心流程

`lower_tl` 的编排顺序（[lower_linear.py:366-408](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/lower/lower_linear.py#L366-L408)）：

1. 从 `qkv_meta` 抽取形状：`BATCH, HQ, N_CTX, D_HEAD = qkv_meta[0].shape`，`DV = qkv_meta[2].shape[3]`，`HK`、`H` 分别来自 k、v 的 head 维（注意线性注意力里 \(Q,K\) 的 head 数可以不同，支持 GQA 风格的 head 分组）。
2. 构造两个数据类 `lowerOutput`（降级字段）与 `TunnerOutput`（autotuner 字段）。
3. 按 `k_mod → v_mod → decay_mod → q_mod` 的顺序逐个降级（每个 mod 不为 `None` 才降级）。
4. 拼接 custom inputs 的形参/梯度列表。
5. 把 `lowerOutput.__dict__` 与 `TunnerOutput.__dict__` 合并，灌进 `TlLinearAttnTemplate` 渲染。

各 mod 的降级函数对应关系（这是本讲的「接线表」，务必记牢）：

| 用户 mod | 降级函数 | 前向字段（→模板占位符） | 落点 |
| --- | --- | --- | --- |
| `q_mod` | `lowerQmodFused` | `q_mod_expr` → `{{q_mod_expr}}`（o-kernel 内） | **fuse 进 o-kernel** |
| `q_mod`（反向） | `lowerQmod` | `q_mod_expr1` / `q_mod_bwd_expr` → backward 函数体 | 宿主机（仅反向） |
| `k_mod` | `lowerKmod` | `k_mod_expr` / `k_name` → `{{k_mod_expr}}`、`{{k_name}}`（forward 函数体） | **宿主机** |
| `v_mod` | `lowerVmod`（或 `lowerFusedVmod` 失败回退） | `v_mod_expr` / `v_name` → forward 函数体 | 宿主机 |
| `v_mod`（fuse 优化） | `lowerFusedVmod` | `v_mod_expr_fused_o`（o-kernel）、`k_mod_expr_fused_h`（h-kernel） | **fuse 进 kernel** |
| `decay_mod` | `lowerDecaymod` | `decay_mod_expr` / `decay_name` → `{{decay_mod_expr}}`、`{{decay_name}}` | **宿主机**（后续 cumsum） |

> 命名陷阱：`lowerFusedVmod` 的第二个分支会把结果写进 `k_mod_expr_fused_h` 字段（[lower_linear.py:273-286](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/lower/lower_linear.py#L273-L286)），但它注入的是 h-kernel 里 `b_k`（即 \(K\)）所在的位置（[linear_tl.py:201](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/template/tl_template/linear/linear_tl.py#L201)）。这是历史命名遗留——它表达的是「与 v_mod 同形的变换，分别作用在 o-kernel 的 `bs` 和 h-kernel 的 `b_k` 上」。retention_linear.py 不传 v_mod，所以走不到这条分支。

#### 4.3.3 源码精读

**`lower_tl` 的形状抽取与顺序编排**：[lower_linear.py:366-401](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/lower/lower_linear.py#L366-L401)。注意 `if v_mod:` 这段（[L382-L392](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/lower/lower_linear.py#L382-L392)）用 `try/except BaseException` 做「**先尝试 fuse，失败回退到宿主机**」的容错降级——`lowerFusedVmod` 对 v_mod 的输入形状有约束（不支持 4 维 shape_idx，见 [L225](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/lower/lower_linear.py#L225)），不满足就抛异常回退到 `lowerVmod`。

**`lowerDecaymod`：retention 的 log 衰减**：[lower_linear.py:289-321](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/lower/lower_linear.py#L289-L321)。它用一个 `SymbolicArray("decay", Var("decay"), shape_idx=["B","H","T"])` 当诱饵，执行 `new_decay = decay_mod(decay, ...)`。对 retention 的 `decay_mod = lambda d: d.log()`，`new_decay` 即 `decay.log()`。随后 `generate_tl_from_dag([new_decay], to_tl=False)` 生成 pytorch 片段，写入 `decay_mod_expr`（形如 `decay_1 = torch.log(decay)`），并把 `new_decay.varname`（`decay_1`）写入 `decay_name`。反向部分用 `new_decay.backward(dnew_decay)` 生成 `decay_mod_bwd_expr`。

**`lowerQmodFused`：q_mod 的缩放 fuse**：[lower_linear.py:359-363](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/lower/lower_linear.py#L359-L363)。诱饵是 `SymbolScalar("bq", Var("bq"), shape_idx=["BT","BK"])`——注意是 **fragment 级的 `SymbolScalar`**（不是整张张量的 `SymbolicArray`），shape_idx 用 `["BT","BK"]` 对应 o-kernel 里的 fragment。对 `q_mod = lambda q: q * scale`，`generate_tl_from_dag([new_q])`（`to_tl` 默认 True）生成 tl 片段。由于 `bq` 的 `count==1`，inplace 复用优化（u2-l3）会把输出名重写回 `bq`，最终片段是 `bq = bq * scale`，正好注入 o-kernel 的 `{{q_mod_expr}}` 位置（紧跟 `T.copy(bq_shared, bq)` 之后）。

**`lowerKmod`：宿主机降级 + 反向**：[lower_linear.py:138-170](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/lower/lower_linear.py#L138-L170)。诱饵是整张 `SymbolicArray("k", ..., shape_idx=["B","H","T","D"])`，`to_tl=False` 生成 pytorch 片段，作用在宿主机的完整 `k` 张量上。反向用 `new_k.backward(dnew_k)` + `return_inputs=True` 收集所有需要梯度的输入，再生成 `k_mod_bwd_expr`。

**全局反向梯度收集**：[lower_linear.py:135](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/lower/lower_linear.py#L135) 定义了模块级 `bwd_custom_output_dict = {}`，每个 `lower*mod` 把 custom input 的梯度变量名登记进去；最终在 `lower_tl` 末尾由 [L402-L404](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/lower/lower_linear.py#L402-L404) 拼成 `custom_inputs_grad_list`，作为 `LinearAttention.backward` 的返回值尾部（对应 `return dq, dk, dv, dg2, <custom grads>`，见 [linear_tl.py:1216](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/template/tl_template/linear/linear_tl.py#L1216)）。

#### 4.3.4 代码实践（本讲核心实践）

**实践目标**：跟读 `retention_linear.py`，分别说明 `q_mod`（缩放）与 `decay_mod`（log 衰减）如何影响生成代码，并对照降级函数给出对应关系。

**操作步骤**：

1. **看清用户的两个 mod**：
   - `decay_mod`：[retention_linear.py:25-26](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attn_script/retention_linear.py#L25-L26)，`return decay.log()`。
   - `q_mod` 与 `scale`：[retention_linear.py:28-31](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attn_script/retention_linear.py#L28-L31)，`scale = 1/D**0.5`（D=256），`return q * scale`。
   - 构造时**没有传 `k_mod`、`v_mod`**（[L76-L81](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attn_script/retention_linear.py#L76-L81)），所以 `lower_tl` 里 `if k_mod:`、`if v_mod:` 都跳过。

2. **追踪 `decay_mod` 的降级**（走 `lowerDecaymod`）：
   - 诱饵 `decay` 形状 `["B","H","T"]`，`new_decay = decay.log()`。
   - 生成 `decay_mod_expr = "decay_1 = torch.log(decay)"`，`decay_name = "decay_1"`。
   - 在模板 `forward` 里展开为（[linear_tl.py:1137-L1148](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/template/tl_template/linear/linear_tl.py#L1137-L1148)）：
     ```python
     decay_1 = torch.log(decay)                                  # {{decay_mod_expr}}
     ...
     decay_cumsum = chunk_local_cumsum_scalar(decay_1, BT)       # {{decay_name}} = decay_1
     ```
   - **结论**：`decay_mod` 的 `log` 不进 kernel，而是在宿主机算成 `decay_1`，再经 `chunk_local_cumsum_scalar` 转成块内累积衰减 `decay_cumsum`，作为 `g` 喂给 h/o kernel。

3. **追踪 `q_mod` 的降级**（走 `lowerQmodFused`）：
   - 诱饵 `bq` 形状 `["BT","BK"]`（fragment），`new_q = bq * scale`。
   - 生成 `q_mod_expr`（tl 片段），经 inplace 复用得 `bq = bq * scale`。
   - 在模板 `chunk_o` 里展开为（[linear_tl.py:357-L361](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/template/tl_template/linear/linear_tl.py#L357-L361)）：
     ```python
     T.copy(bq_shared, bq)
     bq = bq * scale            # {{q_mod_expr}}  ← q_mod fuse 在这里
     T.gemm(bq, bk_shared, bs, transpose_B=True, ...)
     ```
   - **结论**：`q_mod` 的缩放 fuse 进 **o-kernel**，作用在 fragment `bq` 上，紧跟在 `Q` 从 shared 拷到 fragment 之后、`QK^T` gemm 之前。

4. **填对应关系表**（把本实践结论填进去）：

   | retention 的 mod | 走的降级函数 | 影响哪个 kernel | 落点 |
   | --- | --- | --- | --- |
   | `q_mod`（×1/√D） | `lowerQmodFused` | o-kernel（`chunk_o`） | fuse 进 fragment |
   | `decay_mod`（log） | `lowerDecaymod` | h-kernel 与 o-kernel 都受影响（通过 `decay_cumsum`/`g`） | 宿主机 + cumsum |
   | `k_mod`（未传） | 不调用 `lowerKmod` | — | — |
   | `v_mod`（未传） | 不调用 `lowerVmod`/`lowerFusedVmod` | — | — |

**需要观察的现象**：`q_mod` 只影响 o-kernel（h-kernel 里看不到 `scale`）；`decay_mod` 同时影响两个 kernel，因为它通过 `decay_cumsum` 这个共享输入渗透到 h-kernel 的 `b_glast`、`b_g` 与 o-kernel 的 `bg`、`bg1`。

**预期结果**：你能用一句话回答——**`q_mod` fuse 进 o-kernel 的 `bq`；`decay_mod` 在宿主机取 log 后做块内 cumsum，再作为衰减 `g` 同时驱动 h-kernel 的状态衰减与 o-kernel 的块内掩码**。

> 待本地验证：如能在本机跑通 `retention_linear.py`，把 `mod.tl_code` 导出后用 `grep -n "scale\|torch.log\|decay_1\|bq = bq"` 检视，可直接看到上述片段；若环境不具备，纯源码跟读同样能完成本表。

#### 4.3.5 小练习与答案

**练习 1**：如果用户把 `q_mod` 写成 `q * scale + bias`（`bias` 是一个 custom input），`lowerQmodFused` 还能正常 fuse 进 o-kernel 吗？

> **答案**：能。`bias` 会作为 custom input 被 `generate_tl_from_dag` 收集，生成形如 `bq = bq * scale + bias_local` 的 tl 片段注入 o-kernel。但前提是 `bias` 的 shape_idx 能映射到 fragment 形状（参见 `lowerFusedVmod` 里对 4 维 shape_idx 的拒绝逻辑，[lower_linear.py:224-L225](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/lower/lower_linear.py#L224-L225) 的类似约束）。

**练习 2**：为什么 `decay_mod` 用 `SymbolicArray`（shape `["B","H","T"]`）当诱饵，而 `q_mod`（fused）用 `SymbolScalar`（shape `["BT","BK"]`）？

> **答案**：因为 `decay_mod` 的产物要在**宿主机**做整张量的 `chunk_local_cumsum_scalar`，必须以完整张量形状表达；而 fuse 版 `q_mod` 直接作用在 kernel 内的 fragment 上，用 fragment 形状 `["BT","BK"]` 才能让 `to_tl_op` 推导出正确的 `T.Parallel(BT, BK)` 循环下标。

**练习 3**：`lowerVmod` 和 `lowerFusedVmod` 都处理 `v_mod`，`lower_tl` 如何在两者间选择？

> **答案**：[lower_linear.py:382-L392](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/lower/lower_linear.py#L382-L392) 用 `try: lowerFusedVmod(...) except BaseException: lowerVmod(...)`。先尝试 fuse（更快），若 `v_mod` 的输入形状不被 fuse 路径支持（如 4 维）就抛异常，回退到宿主机降级。这是一种「性能优先、正确性兜底」的策略。

---

## 5. 综合实践

**任务**：在不修改框架源码的前提下，**用源码阅读法预测**：如果把 `retention_linear.py` 的 `decay_mod` 从 `decay.log()` 改成 `decay.log() * 0.5`（即衰减强度减半），生成代码会在哪些地方变化？并解释对两个 kernel 数值行为的影响。

**建议步骤**：

1. **定位降级入口**：`decay_mod` 经 `lowerDecaymod`（[lower_linear.py:289-L321](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/lower/lower_linear.py#L289-L321)）降级。`new_decay = decay.log() * 0.5` 会多挂一个 `Mul` 节点（乘 `Const(0.5)`）。
2. **预测宿主机片段**：`decay_mod_expr` 会变成 `decay_1 = torch.log(decay) * 0.5`（pytorch 后端把 `Mul` 发射成 `* 0.5`），`decay_name` 仍是 `decay_1`。
3. **追踪对 cumsum 的影响**：`decay_cumsum = chunk_local_cumsum_scalar(decay_1, BT)` 的输入减半，故每步累积衰减 `g[t]` 减半。
4. **追踪对 kernel 的影响**：
   - h-kernel 里 `b_h *= exp2(glast * LOG2E)`（[L193-L194](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/template/tl_template/linear/linear_tl.py#L193-L194)）：`glast` 减半 → 历史状态 \(H\) 衰减更慢 → 更久地保留早期信息。
   - o-kernel 里 `bo *= exp2(bg * LOG2E)`、`bs *= exp2((bg_i - bg_j) * LOG2E)`（[L369-L378](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/template/tl_template/linear/linear_tl.py#L369-L378)）：块内注意力掩码更平缓。
5. **验证（可选）**：若本机能跑，把改动后的 `mod.tl_code` 与原版 diff，确认只有 `decay_1` 那一行变化；再用 `do_bench_retention_linear` 的 `print_debug` 观察与 fla 参考实现的误差（注意此时参考实现也需同步调整 `g` 才能对齐，否则误差会变大——这正好验证「衰减强度」对输出的影响）。

**预期产出**：一段说明文字 + 一张「改动点 → 受影响代码位置 → 数值影响」的对照表，体现你对「mod 降级 → 模板注入 → kernel 行为」整条链路的把握。

## 6. 本讲小结

- **线性注意力与 transformer 注意力的结构差异**：前者用可递推的跨块状态 \(H\) 代替 \(O(T^2)\) 的注意力矩阵，复杂度降到 \(O(T)\)，因此天然需要「先算 \(H\)、再算 \(O\)」的双 kernel 结构。
- **`LinearAttentionEngine` 入口**：构造即编译，经 `lower_tl` 生成 TileLang 源码，md5 缓存于 `cache/`，importlib 加载取出 `linear_attention` 符号；与 transformer 引擎同构，但无 backend 分流（仅 tl）。
- **四个 mod 的两种降级落点**：`q_mod` fuse 进 o-kernel（`lowerQmodFused`，fragment 级 `SymbolScalar`）；`k_mod`/`v_mod`/`decay_mod` 走宿主机（`to_tl=False`，整张量级 `SymbolicArray`）；`v_mod` 优先尝试 fuse、失败回退。
- **双 kernel 骨架**：`chunk_fwd_h` 递推并落盘状态 `h`（形状 `[B,H,NT·D,DV]`），`chunk_o` 读 `h` 算块间部分、再叠加块内注意力得到 `O`；二者通过中间张量 `h` 解耦。
- **衰减的处理链**：`decay_mod`（log）→ 宿主机 `chunk_local_cumsum_scalar`（块内 cumsum）→ 作为 `g` 同时驱动 h-kernel 的状态衰减与 o-kernel 的块内掩码，全程用 `exp2(x*LOG2E)` 换底。
- **反向**：由 `SymbolScalar.backward` 手写 autodiff 生成各 `*_bwd_expr`，注入 `LinearAttention.backward`；反向拆成 dh/dqkg/dv 三段 kernel 并重算 `h`。

## 7. 下一步学习建议

- **横向对比 transformer 引擎**：回到 u3-l3 与 u3-l2，对照 `AttentionEngine` 的形状分发与 `lower_tl` 编排，体会「transformer 注意力的降级三件套（score_mod/online_func/custom_inputs）」与「线性注意力的四 mod」在设计哲学上的对偶。
- **继续向 expert 层深入**：
  - 读 u5-l3（autotuner），对照本讲看到的 `TunnerOutput`（`BK_h`/`BV_h`/`num_stages_h`/`num_threads_h` 等）与 `linear_tl.py` 里的 `generate_config_h`/`generate_config_o`，理解双 kernel 各自的配置空间如何被硬件约束过滤。
  - 读 u5-l4（基准与正确性），理解 `do_bench_retention_linear` 如何用 fla 的 `chunk_retention` 当参考实现来校验本讲生成的 kernel。
- **源码延伸阅读**：
  - `attn_script/` 下的其他线性注意力示例：`mamba2_ngroup1.py`（mamba2）、`simple_gla.py`（gated retention）、`retnetion_linear.py`（retnet 线性）。对比它们的 `q_mod`/`v_mod`/`decay_mod` 写法，体会同一套降级机制如何覆盖不同线性注意力变体。
  - `flash-linear-attention` 库的 chunk 算法，它是本讲双 kernel 结构的学术来源；理解其数学有助于将来为 `lower_linear.py` 扩展新的 mod 或新的线性注意力变体（见 u5-l7 的二次开发切入点）。
