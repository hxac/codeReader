# 算子融合 pass

## 1. 本讲目标

本讲深入 MLC LLM 编译流水线（见 [u7-l2](u7-l2-pass-pipeline-overview.md)）的 Phase 1 / Phase 3 中**算子融合类 pass** 的内部实现。学完后你应当能够：

1. 说清「为什么要融合」——讲明白访存墙（memory wall）与 kernel 启动开销这两类收益。
2. 读懂四种融合 pass 的源码：`FuseAddRMSNorm`、`FuseDequantizeMatmulEwise`、`FuseTransposeMatmul`、`FuseDequantizeTranspose`。
3. 区分 TVM Relax 里的两种融合改图机制：声明式 `FuseOpsByPattern` 与手动 `PyExprMutator` 改写，并知道各自适用场景。
4. 针对一个具体 pass，画出融合前后的 IR 对比，指出它消除了哪个中间 buffer。

本讲只讲**融合**本身；派发类（BLAS/Triton/KV cache）、附加类（采样/推测解码辅助）pass 见 [u8-l2](u8-l2-dispatch-passes.md) 与 [u8-l3](u8-l3-attach-passes.md)。

## 2. 前置知识

### 2.1 访存墙与算术强度

GPU 的算力（FLOPS）增长远快于显存带宽（bytes/s）。一个算子是否「卡在访存」上，用**算术强度**（arithmetic intensity）衡量：

\[
\text{算术强度} = \frac{\text{浮点运算次数}}{\text{访问的字节数}}
\]

- `matmul`：每个输出元素要做 \(K\) 次乘加，但只读 \(K\) 个输入元素。算术强度高，是 **compute-bound**。
- `add`、`rms_norm`、`dequantize`、`permute_dims`：每个元素只做 \(O(1)\) 次运算却要读写整个张量。算术强度低，是 **memory-bound**。

对 memory-bound 算子，瓶颈是「搬数据」，所以**减少数据搬运 = 提速**。这正是融合的核心收益。

### 2.2 kernel 启动开销

GPU 上每一次 kernel 启动都有固定开销（微秒级，包含命令提交、网格调度等）。在 decode 阶段，每生成一个 token 都要跑一遍整网，此时单个 kernel 计算量很小，启动开销占比就很高。把多个小 kernel 合成一个，能直接减少启动次数。

### 2.3 TVM Relax 的两种改图机制

| 机制 | 写法 | 特点 | 本讲使用者 |
|---|---|---|---|
| 声明式模式匹配 | `relax.transform.FuseOpsByPattern([(name, pattern, annotations, check)])` | 用 `is_op`/`wildcard` 描述要匹配的子图，TVM 自动融合成单个 `call_tir` 复合 PrimFunc，再交给后续调度器 | `FuseDequantizeMatmulEwise`、`FuseTransposeMatmul`（第一阶段） |
| 手动改写 | 继承 `PyExprMutator`（`@mutator`），重写 `visit_call_` | 自己遍历 IR、判定模式、生成「手写调度过」的 PrimFunc 直接替换 | `FuseAddRMSNorm`、`FuseDequantizeTranspose`、`FuseTransposeMatmul`（第二阶段） |

> 为什么不全用声明式？因为像 `rms_norm` 这种带**跨维度归约**的算子，通用调度器（DLight）很难生成足够好的 kernel；MLC 选择**手写一个带 warp 级归约的 TIR kernel**（`tirx.is_scheduled: 1`），这只能靠手动改写来注入。

### 2.4 命名约定提示（tirx / s_tir / SBlock）

本仓库近期完成了对 TVM `tir`→`tirx` 的重构适配（见 git log 中 `Adapt to TVM PrimType and tirx refactor`）。因此你在源码里会看到：

- `from tvm.script import tirx as T`，并用 `@T.prim_func(...)`、`T.sblock(...)`、`T.sblock_alloc_buffer(...)`、`T.axis.remap(...)` —— 这些是重构后的新 API，对应传统 TVM 文档里的 `tir`/`Block`/`allocate`，功能一致但写法更新。
- `tvm.tirx`、`tvm.s_tir` 同理。

读源码时把它们当作「新版 TIR DSL」即可。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| `python/mlc_llm/compiler_pass/fuse_add_norm.py` | 融合 `rms_norm(add(x1,x2), w)`，手写 decode/prefill 两套带 warp 归约的 kernel |
| `python/mlc_llm/compiler_pass/fuse_dequantize_matmul_ewise.py` | 用声明式 `FuseOpsByPattern` 融合 `dequantize → matmul → elementwise`，参数化扫描多种 epilogue 档位 |
| `python/mlc_llm/compiler_pass/fuse_transpose_matmul.py` | 两阶段融合：先 `FuseOpsByPattern` 标记 `matmul(x, permute_dims(w))`，再用 mutator 把转置烘进 `NT_matmul` 的索引 |
| `python/mlc_llm/compiler_pass/fuse_dequantize_transpose.py` | 折叠 dequantize TIR 内部自带的 `T_transpose` 与外层 `permute_dims`，省掉一次转置物化 |
| `python/mlc_llm/compiler_pass/pipeline.py` | 把上述 pass 挂到 Phase 1（高层图）与 Phase 3（TIR 级），决定执行顺序 |
| `python/mlc_llm/support/max_thread_check.py` | `get_max_num_threads_per_block`：为 `FuseAddRMSNorm` 选线程数 `TX` |

## 4. 核心概念与源码讲解

### 4.1 融合的动机：消除中间 buffer 的显存往返

#### 4.1.1 概念说明

考虑一个 Transformer 解码层里最常见的一段：

\[
h = x_1 + x_2,\quad o = \text{RMSNorm}(h,\, w)
\]

其中 \(x_1\) 是残差、\(x_2\) 是注意力/FFN 的输出，\(h\) 是相加结果，\(o\) 是归一化后送进下一层的值。**如果不融合**，编译器会把它们落成两个独立的 kernel：

1. kernel A `add`：读 \(x_1, x_2\) → 把 \(h\) **写回全局显存**；
2. kernel B `rms_norm`：把 \(h\) **从全局显存读回** → 算归约 → 把 \(o\) 写回。

问题在于 \(h\) 这个**中间 buffer** 被完整地写了一次又读了一次，纯属「搬运」，因为它原本可以留在寄存器里直接喂给归约。对于 `(batch, 1, hidden)` 这种 decode 形状，\(h\) 有几十上百 MB，这一来一回的显存带宽浪费很可观，外加多一次 kernel 启动。

**融合**的目标就是：让 \(h\) 待在寄存器/共享内存里，一次 kernel 同时算出 \(o\)。

#### 4.1.2 核心流程

MLC 的融合 pass 通用执行框架：

```text
遍历 IRModule 里每个 relax.Function
  └─ 对每个 relax.Call 做后序/模式匹配
       ├─ 命中目标模式？（op 名 + 结构 + 形状约束）
       │     否 → 原样返回
       │     是 → 生成（或复用）一个融合 PrimFunc
       └─ 用 call_tir(融合PrimFunc, ...) 替换原子图
最后 remove_all_unused 清掉无用绑定，finalize
```

两种改写机制（2.3 节）共用这套框架，差别只在「模式怎么描述」和「融合 kernel 怎么来」。

#### 4.1.3 源码精读：四种融合 pass 在流水线里的位置

融合 pass 的执行顺序由 [pipeline.py:121-150](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/compiler_pass/pipeline.py#L121-L150) 决定，中文说明：Phase 1 在「高层算子图」上跑 `FuseDequantizeTranspose`→`BLASDispatch`→`FuseAddRMSNorm`→`FuseTransposeMatmul`，Phase 3 在「降到 TIR 后」再跑 `FuseDequantizeMatmulEwise`。

```python
# Phase 1. Passes on high-level operator graph（节选）
FuseFTDequantizeEpilogue(),
FuseDequantizeTranspose(),
BLASDispatch(target) if cublas_gemm else ...,   # 见 u8-l2
(
    FuseAddRMSNorm(target=target)
    if target.kind.name != "llvm" else ...        # CPU(llvm) 不做本融合
),
FuseTransposeMatmul(),
...
# Phase 3. Passes on TIR（节选）
FuseDequantizeMatmulEwise(),
```

两个要点：

- **`FuseAddRMSNorm` 在 CPU（`llvm`）上被跳过**（[pipeline.py:127-131](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/compiler_pass/pipeline.py#L127-L131)）：因为它生成的是 GPU 风格的手写 kernel，CPU 走通用调度即可。
- **`FuseDequantizeMatmulEwise` 排在 Phase 3**：它要匹配 `call_tir` 形式的 `dequantize`/`matmul`，而这些名字是 Phase 2 的 `LegalizeOps`+`FuseOps` 降级后才产生的，所以必须等降级完成。

#### 4.1.4 代码实践：在流水线里定位融合 pass

1. **目标**：建立「融合 pass 处于五阶段流水线的哪一段、受哪些 target 条件控制」的整体印象。
2. **步骤**：
   - 打开 [pipeline.py](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/compiler_pass/pipeline.py)，找到 Phase 1 与 Phase 3 两个 `_DebugDump("debug-phaseN.py", ...)` 之间的 pass 列表。
   - 列出本讲的四个 pass 分别落在哪个 Phase，以及各自外层的 `if target.kind.name != ...` 条件。
3. **需要观察的现象**：你会发现 `FuseAddRMSNorm` 受 `!= "llvm"` 保护，而 `FuseDequantizeMatmulEwise` 没有这种保护（它在 TIR 级、跨 target 通用）。
4. **预期结果**：得到一张「pass → Phase → target 条件」三列对照表。
5. 待本地验证：若你启用 `debug_dump`（见 [u7-l2](u7-l2-pass-pipeline-overview.md) 中 `debug_dump` 参数），可直接对比 `debug-phase1.py` 与 `debug-phase3.py` 里 IRModule 的差异，肉眼看到融合前后效果。

#### 4.1.5 小练习与答案

- **Q1**：为什么 `add + rms_norm` 的融合收益通常比把两个 `matmul` 融在一起更显著？
  - **A**：`add`/`rms_norm` 是 memory-bound（算术强度 \(O(1)\)），瓶颈在搬数据，融合直接减少显存往返；而 `matmul` 是 compute-bound，融合对它的边际收益小，且 GEMM 通常要交给 cuBLAS/Triton 等专门库（派发类 pass），不适合随便融。
- **Q2**：本仓库的融合 pass 用了哪两种改图机制？分别举一个使用者。
  - **A**：声明式 `FuseOpsByPattern`（如 `FuseDequantizeMatmulEwise`）与手动 `PyExprMutator` 改写（如 `FuseAddRMSNorm`）。

---

### 4.2 add + RMSNorm 融合（FuseAddRMSNorm）

这是本讲的核心 pass，也是综合实践的对象。

#### 4.2.1 概念说明

`FuseAddRMSNorm` 把 `rms_norm(add(x1, x2), w)` 这一对算子融进一个**手写调度**的 TIR kernel。难点在于：

1. RMSNorm 含**跨 hidden 维的归约**（求平方和），需要 warp/线程块级归约；
2. 残差 `add` 的结果除了喂给 RMSNorm，**还要作为残差继续往下一层传**——所以融合 kernel 必须**同时输出两样东西**：归一化结果 \(o\) 与残差 \(h\)。

RMSNorm 的数学定义（\(H\) 为 hidden 维大小，\(\varepsilon\) 为防除零小量）：

\[
\text{RMS}(h) = \sqrt{\frac{1}{H}\sum_{j} h_j^2 + \varepsilon},\qquad o_i = \frac{h_i}{\text{RMS}(h)} \cdot w_i = h_i \cdot w_i \cdot \text{rsqrt}\!\left(\frac{1}{H}\sum_j h_j^2 + \varepsilon\right)
\]

#### 4.2.2 核心流程

```text
visit_call_(call):
  1. call 是不是 relax.nn.rms_norm？dtype 是不是 fp16/bf16？  否→跳过
  2. weight = call.args[1], eps = call.attrs.epsilon
  3. y = lookup_binding(call.args[0])   # 取喂给 rms_norm 的那个变量绑定的值
  4. y 是不是 relax.add(x1, x2)？       否→跳过
  5. 形状约束：hidden % TX == 0？        否→跳过
  6. is_prefill = (n == 1)               # 选择 prefill 或 decode 版 kernel
  7. 复用或新建对应的手写 PrimFunc（仅每个 Function 建一次）
  8. tuple_out = call_tir(func, [x1,x2,weight], out_ty=[ty, ty])
  9. new_o = tuple_out[0]                # 归一化结果，作为本 call 的返回
     new_h = emit(tuple_out[1])          # 残差
     set_var_remap(call.args[0], new_h)  # 把下游对残差变量的引用重定向到 new_h
```

关键设计：**用 `set_var_remap` 把「下游要用到的残差」重新指向融合 kernel 的第二个输出**，从而在消除中间 buffer 的同时不破坏数据流。

#### 4.2.3 源码精读

**Pass 类骨架**（[fuse_add_norm.py:149-165](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/compiler_pass/fuse_add_norm.py#L149-L165)）：标准 `@tvm.transform.module_pass` 外壳，`transform_module` 把实际工作交给 `_FuseAddRMSNormRewriter`。其中 `TX = min(1024, get_max_num_threads_per_block(target))` 决定每个 block 用多少线程去并行处理 hidden 维。

**模式匹配**（[fuse_add_norm.py:187-210](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/compiler_pass/fuse_add_norm.py#L187-L210)）：逐条校验「rms_norm + add + dtype + 形状」。注意 `lookup_binding(call.args[0])` 是关键一步——它把一个 `relax.Var` 解析回它实际绑定的表达式，从而能跨数据流边识别出「rms_norm 的输入其实是个 add」：

```python
# 匹配 "rms_norm(add(x1, x2), w)" 模式
if call.op != tvm.ir.Op.get("relax.nn.rms_norm") or call.ty.dtype not in ["bfloat16", "float16"]:
    return call
weight = call.args[1]
eps = call.attrs.epsilon
y = self.lookup_binding(call.args[0])           # 跨绑定查 add
if not isinstance(y, relax.Call) or y.op != tvm.ir.Op.get("relax.add"):
    return call
x1, x2 = y.args[0], y.args[1]
n, _, h = x1.ty.shape
h = int(h)
if h % self.TX != 0:                             # hidden 必须能被线程数整除
    return call
```

`is_prefill = n == 1`（[fuse_add_norm.py:212-213](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/compiler_pass/fuse_add_norm.py#L212-L213)）：prefill 形状为 `(1, seq_len, hidden)`（首维为 1），decode 形状为 `(batch, 1, hidden)`（首维为 batch），据此选不同 kernel；并且 `prefill_norm_gv`/`decode_norm_gv` 做**惰性缓存**，每个 `relax.Function` 只生成一次。

**输出 tuple + 重绑残差**（[fuse_add_norm.py:228-238](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/compiler_pass/fuse_add_norm.py#L228-L238)）：

```python
tuple_output = self.builder_.emit(
    relax.call_tir(func_gv, [x1, x2, weight], out_ty=[x1.ty, x2.ty])  # 两个同形输出
)
new_o = relax.TupleGetItem(tuple_output, 0)          # 归一化结果
new_y = self.builder_.emit(relax.TupleGetItem(tuple_output, 1))  # 残差
self.set_var_remap(call.args[0], new_y)              # 下游用残差的地方都改指向 new_y
return new_o
```

**手写 decode kernel 的关键片段**（[fuse_add_norm.py:42-78](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/compiler_pass/fuse_add_norm.py#L42-L78)），中文标注三段作用：

```python
for i in range(add_local_size):
    with T.sblock("T_add"):
        add_local[h // TX] = A[bx, 0, h] + B[bx, 0, h]      # ① 相加写进寄存器
    with T.sblock("T_write_back"):
        add[bx, 0, h] = add_local[h // TX]                    # ② 残差写回（下游要用）
...
    sum_local[tx, bx, 0] += T.float32(add_local[i]) * T.float32(add_local[i])  # ③ 平方和归约
...
        out[bx, 0, h] = T.cast(
            T.rsqrt(sum_shared[bx, 0] * inv_hidden_size + eps)               # rsqrt(均值+eps)
            * T.float32(add_local[h // TX]) * T.float32(C[h]),               # *残差*权重
            dtype=in_dtype,
        )
```

可以看出 `add_local` 是**寄存器里的中间结果**：它直接参与③的归约，**没有作为 rms_norm 的输入从全局显存读回**。唯一保留的是 ② 中残差的一次写回——因为下游残差路径确实需要它。prefill 版本（[fuse_add_norm.py:83-146](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/compiler_pass/fuse_add_norm.py#L83-L146)）逻辑相同，只是 `blockIdx.x` 遍历 `seq_len` 而非 `batch`。

#### 4.2.4 代码实践：画出融合前后 IR 对比（本讲核心实践）

1. **实践目标**：把 `FuseAddRMSNorm` 融合前后的 Relax IR 画出来，并指出被消除的中间 buffer。
2. **操作步骤**：
   - 阅读 [fuse_add_norm.py:187-238](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/compiler_pass/fuse_add_norm.py#L187-L238)，确认它匹配的模式是 `rms_norm(add(x1,x2), w)`。
   - 在纸上（或注释里）画出**融合前**的一段伪 IR：

     ```text
     %h     = relax.add(%residual, %attn_out)        # 写回全局显存 (batch,1,H)
     %o     = relax.nn.rms_norm(%h, %weight)         # 从全局显存读回 %h
     # ... 下游残差路径继续使用 %h ...
     ```

   - 再画出**融合后**的伪 IR：

     ```text
     %t    = call_tir(@fuse_add_norm_decode, (%residual,%attn_out,%weight),
                      out_sinfo=[(batch,1,H),(batch,1,H)])
     %o    = %t[0]            # 归一化结果
     %h    = %t[1]            # 残差（set_var_remap 后下游 %h 都指向这里）
     ```

3. **需要观察的现象**：融合后 `add` 与 `rms_norm` 两个 `relax` 算子合并成单个 `call_tir`，且该 `call_tir` 返回二元组；原来绑定 `%h` 的变量被 `set_var_remap` 重定向到二元组的第二项。
4. **预期结果**：能口头说明——**被消除的是 `%h` 作为 rms_norm 输入的那一次「写回 + 读回」全局显存往返**；`add` 的结果 `add_local` 留在寄存器里直接参与平方和归约。残差路径仍有一次写回（②），但那是真实需要的，不算浪费。
5. 待本地验证：若启用 `debug_dump`，对比 `debug-phase1.py`（融合后）与更早的 dump，可肉眼确认 `%h` 的中间绑定消失。

#### 4.2.5 小练习与答案

- **Q1**：为什么融合 kernel 要返回二元组而不是只返回归一化结果？
  - **A**：因为残差 `add` 的结果 `h` 还要继续作为残差传给下游（下一层的残差连接）。融合 kernel 既然把 `h` 算进了寄存器，就顺便把它写回显存（②），并通过 `set_var_remap` 把下游对原 `%h` 的引用改指过来。
- **Q2**：`is_prefill = n == 1` 里 `n` 指什么？为什么它能区分两种阶段？
  - **A**：`n` 是张量第一维。prefill 形状 `(1, seq_len, H)` 首维为 1；decode 形状 `(batch, 1, H)` 首维为 batch。两者规约轴的排布不同，故分别用 `prefill_add_rms` 与 `decode_add_rms` 两套 kernel。

---

### 4.3 dequantize + matmul + elementwise 融合（FuseDequantizeMatmulEwise）

#### 4.3.1 概念说明

量化模型（见 [u5-l2](u5-l2-group-quantization.md)）在运行时要先把 int4/低比特权重 `dequantize` 成 fp16/bf16，再做 `matmul`。如果不融合，反量化出的整个权重矩阵会**物化到显存**，再被 matmul 读走——这是极大的带宽浪费（毕竟 matmul 本可以边读量化权重边反量化边累加）。

`FuseDequantizeMatmulEwise` 进一步把 matmul 后面跟的 **elementwise epilogue**（如 bias 加、激活）也一起融进来，形成 `dequantize → matmul → ewise` 的大融合 kernel。它采用**声明式 `FuseOpsByPattern`**，代码极简。

#### 4.3.2 核心流程

```text
对 (n_aux_tensor ∈ {0..4}) × (match_ewaye ∈ {0,1,2,3,6}) 组合:
    构造模式 _pattern(match_ewaye, n_aux_tensor)
    用 FuseOpsByPattern([("dequantize_matmul", 模式, 注解, check)]) 匹配并融合
最后 FuseTIR() 把融合后的 Relax 子图塌缩成单个 TIR PrimFunc
```

- `n_aux_tensor`：反量化函数除「缩放后权重」外带的**辅助张量数**（如 scale、zero point 等）。
- `match_ewise`：matmul 之后要融合的 **elementwise 张量数**（epilogue 深度档位）。
- 特例：`match_ewise == 6`（最深 epilogue）仅在 `n_aux_tensor == 4` 时才尝试——避免组合爆炸，只对最复杂的反量化尝试最深的 epilogue 融合。

#### 4.3.3 源码精读

**Pass 主循环**（[fuse_dequantize_matmul_ewise.py:12-34](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/compiler_pass/fuse_dequantize_matmul_ewise.py#L12-L34)）：双层循环枚举 epilogue/辅助张量组合，每个组合挂一次 `FuseOpsByPattern`，最后 `FuseTIR()`：

```python
for n_aux_tensor in [0, 1, 2, 3, 4]:
    for match_ewise in [0, 1, 2, 3, 6]:
        if match_ewise == 6 and n_aux_tensor != 4:   # 最深 epilogue 仅配最多辅助张量
            continue
        seq.append(relax.transform.FuseOpsByPattern(
            [("dequantize_matmul", *_pattern(match_ewise, n_aux_tensor))]))
seq.append(relax.transform.FuseTIR())
```

**模式描述**（[fuse_dequantize_matmul_ewise.py:37-85](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/compiler_pass/fuse_dequantize_matmul_ewise.py#L37-L85)）：用 `is_op("relax.call_tir")` + `TuplePattern` 描述「先一个 call_tir 反量化产出 w，再一个 call_tir 把 x、w 和若干 ewise 张量做 matmul」：

```python
w = is_op("relax.call_tir")(GlobalVarPattern(),
        TuplePattern([w_scaled] + [wildcard() for _ in range(n_aux_tensor)]), ...)   # dequantize
matmul = is_op("relax.call_tir")(GlobalVarPattern(),
        TuplePattern([x, w] + [wildcard() for _ in range(match_ewise)]), ...)        # matmul(+ewise)
```

**用命名约定做语义校验**（[fuse_dequantize_matmul_ewise.py:57-83](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/compiler_pass/fuse_dequantize_matmul_ewise.py#L57-L83)）：降到 TIR 后，`call_tir` 已经丢失了「这是什么算子」的语义，只能靠**目标 PrimFunc 的名字前缀**判断——`w` 那个 call_tir 必须指向名字以 `dequantize`/`fused_dequantize` 开头的函数，`matmul` 那个必须以 `matmul`/`fused_matmul`/`NT_matmul`/`fused_NT_matmul` 开头：

```python
def _check_decoding(ctx):
    g_var = ctx.annotated_expr["w"].args[0]
    return g_var.name_hint.startswith("dequantize") or g_var.name_hint.startswith("fused_dequantize")

def _check_matmul(ctx):
    g_var = ctx.annotated_expr["matmul"].args[0]
    return (g_var.name_hint.startswith("matmul")
            or g_var.name_hint.startswith("fused_matmul")
            or g_var.name_hint.startswith("NT_matmul")
            or g_var.name_hint.startswith("fused_NT_matmul"))
```

> **教学点**：这是「靠命名约定恢复语义」的典型手法。降到 TIR 后类型信息变弱，融合 pass 依赖 Phase 2 降级时给 PrimFunc 起的规范名字来识别算子家族。这也解释了为什么 `NT_matmul`（B 转置的 GEMM，见 4.4）这个名字会在多处被检查。

#### 4.3.4 代码实践：理解参数化扫描与命名校验

1. **目标**：搞清「为什么用一个 pass 跑很多次 `FuseOpsByPattern`」。
2. **步骤**：
   - 在 [fuse_dequantize_matmul_ewise.py:18-34](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/compiler_pass/fuse_dequantize_matmul_ewise.py#L18-L34) 数出实际会跑多少次 `FuseOpsByPattern`（`n_aux_tensor` 5 个 × `match_ewise` 5 个 − 4 个被 `continue` 跳过 = 21 次，再扣除 `match_ewise==6` 与 `n_aux_tensor!=4` 的组合）。
   - 在 [fuse_dequantize_matmul_ewise.py:75-80](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/compiler_pass/fuse_dequantize_matmul_ewise.py#L75-L80) 找到 `NT_matmul` 前缀检查，回想 4.4 节会看到 `FuseTransposeMatmul` 正是产出 `NT_matmul`。
3. **现象**：先匹配浅 epilogue（`match_ewise=0`，只融 dequantize+matmul），再逐步加深；已融部分会被后续匹配复用（`fused_matmul`/`fused_dequantize` 前缀即因此而来）。
4. **预期**：能解释「多次扫描」是为了逐层累积融合深度——一次 `FuseOpsByPattern` 只融一层，多次才能把深 epilogue 全部吃进来。
5. 待本地验证。

#### 4.3.5 小练习与答案

- **Q1**：为什么 `_check` 要靠 GlobalVar 的名字前缀做判断，而不是看 op 类型？
  - **A**：Phase 2 降级后这些算子都变成了 `relax.call_tir`，op 类型相同（都是 `call_tir`），区分它们到底是什么算子只能靠目标 PrimFunc 的命名约定。
- **Q2**：`match_ewise == 6` 为什么只在 `n_aux_tensor == 4` 时尝试？
  - **A**：避免组合爆炸——最深的 epilogue 融合收益通常只对带最多辅助张量的复杂反量化（如某些 ft-quant 风格方案）才划算，对简单反量化尝试深 epilogue 既低效又容易误匹配。这是「按需收敛」的扫描策略。

---

### 4.4 transpose 类融合（FuseTransposeMatmul + FuseDequantizeTranspose）

#### 4.4.1 概念说明

Transformer 里常见 `matmul(x, permute_dims(w))`——即权重在内存里是一个布局，但计算时要按转置布局读。如果老老实实先 `permute_dims` 把整个权重转置物化出来，又是一笔白费的显存搬运。

本节两个 pass 都是为了**消灭这层显式的转置物化**，但角度不同：

- **`FuseTransposeMatmul`**：把 `matmul(x, permute_dims(w))` 改写成一个 `NT_matmul`（NT = B 矩阵转置的 GEMM），转置体现在**读取时的索引**上，不单独物化转置权重。
- **`FuseDequantizeTranspose`**：当 dequantize 的 TIR 函数**内部已经带了一个 `T_transpose` 块**，而外层 Relax 又套了一个 `permute_dims`，二者叠加是冗余；本 pass 把它们折叠掉，让 dequantize 直接产出目标布局。

#### 4.4.2 核心流程

**FuseTransposeMatmul 两阶段**：

```text
阶段 A: FuseOpsByPattern 标记
    模式: matmul(x, permute_dims(w))，且转置是「最后两维交换」
    命中 → 打上 Composite="transpose_matmul_fuse" 属性，包成一个子函数
阶段 B: _TransposeMatmulFuser 改写
    遇到带 Composite 属性的子函数 → 用 call_te(te_transposed_matmul, ...)
    生成单个 NT_matmul PrimFunc（转置烘进索引）
```

**FuseDequantizeTranspose**：

```text
遇到 relax.matmul：
    若 LHS 形状倒数第二维 != 1 → 跳过（只处理单 token 行的 decode 风格，不做通用 GeMM）
    取 RHS：必须是 permute_dims(call_tir(dequantize, ...))
    检查 dequantize 的 TIR：体内必须以 T_transpose 块结尾
    → 复制 dequantize PrimFunc、去掉结尾 T_transpose、产出「未转置布局」的 dequantize
    → 改写 matmul 直接消费这个新 dequantize（吃掉外层 permute_dims）
```

#### 4.4.3 源码精读

**FuseTransposeMatmul 的模式与转置校验**（[fuse_transpose_matmul.py:31-50](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/compiler_pass/fuse_transpose_matmul.py#L31-L50)）：只匹配「最后两维交换」的转置（2D 且 `axes is None`，或 `axes` 恰为 `[..., -2,-1] 交换]`）：

```python
wT = is_op("relax.permute_dims")(w)
o  = is_op("relax.matmul")(x, wT)

def _check(context):
    transpose_call = context.annotated_expr["wT"]
    ndim = transpose_call.args[0].ty.ndim
    if ndim == 2 and transpose_call.attrs.axes is None:
        return True                       # 2D 默认转置就是最后两维交换
    axes = list(range(ndim)); axes[-1], axes[-2] = axes[-2], axes[-1]
    return list(transpose_call.attrs.axes) == axes
```

**第二阶段：把转置烘进 matmul 索引**（[fuse_transpose_matmul.py:131-143](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/compiler_pass/fuse_transpose_matmul.py#L131-L143)）：识别到带 `Composite="transpose_matmul_fuse"` 属性的子函数后，用 `call_te(te_transposed_matmul, ...)` 重新生成一个名为 `NT_matmul` 的 PrimFunc：

```python
if "Composite" in function.attrs and function.attrs["Composite"] == "transpose_matmul_fuse":
    out_dtype = function.ret_ty.dtype
    return self.builder_.call_te(
        te_transposed_matmul, call.args[1], call.args[0],
        primfunc_name_hint="NT_matmul",            # ← 产出 NT_matmul，会被 4.3 的 _check 识别
    )
```

`te_transposed_matmul` 内部（[fuse_transpose_matmul.py:64-129](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/compiler_pass/fuse_transpose_matmul.py#L64-L129)）通过 `bT_shape`（把 b 最后两维交换）算输出形状，并在 `multiply_compute` 里**用转置后的索引访问 b**——于是「转置」在读取时就地完成，不需要单独的转置 buffer。产出的 `NT_matmul` 正是 4.3 节 `_check_matmul` 认得的那个前缀。

**FuseDequantizeTranspose 的结构匹配**（[fuse_dequantize_transpose.py:42-68](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/compiler_pass/fuse_dequantize_transpose.py#L42-L68)）：逐层用 `lookup_binding` 跨绑定确认 `matmul(x, permute_dims(dequantize(...)))` 链路，并显式声明「不为通用 GeMM 做融合」（只处理 LHS 倒数第二维为 1 的情形）：

```python
if call.op != tvm.ir.Op.get("relax.matmul"):
    return call
# 只在 LHS 为单行（decode 风格）时处理，不做通用 GeMM
if (call.args[0].ty.ndim < 2
        or not isinstance(call.args[0].ty.shape[-2], tirx.IntImm)
        or call.args[0].ty.shape[-2].value != 1):
    return call
matmul_rhs = self.lookup_binding(call.args[1])      # permute_dims
transpose_input = self.lookup_binding(matmul_rhs.args[0])  # call_tir(dequantize,...)
```

随后它**深度检查 dequantize TIR 的结构**（[fuse_dequantize_transpose.py:70-80](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/compiler_pass/fuse_dequantize_transpose.py#L70-L80)）：要求该 PrimFunc 体是一个两段 `SeqStmt`，且第二段是一个以 `T_transpose` 命名的循环块。只有满足这一严格结构，才构造去掉结尾转置的新 PrimFunc（[fuse_dequantize_transpose.py:82-107](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/compiler_pass/fuse_dequantize_transpose.py#L82-L107)），并用 `s_tir.renew_defs` 深拷贝避免 IR 节点跨 PrimFunc 复用：

```python
new_func = s_tir.renew_defs(new_func)                       # 深拷贝，防止节点重复
g_var = self.builder_.add_func(new_func, func_name="dequantize")
dequantize_matmul_rhs = self.builder_.emit(
    relax.call_tir(g_var, transpose_input.args[1], out_ty=matmul_rhs.ty))
return relax.op.matmul(call.args[0], dequantize_matmul_rhs, out_dtype=call.attrs.out_dtype)
```

#### 4.4.4 代码实践：追踪 NT_matmul 的「跨 pass 串联」

1. **目标**：理解 4.3 与 4.4 如何通过 `NT_matmul` 命名「接力」。
2. **步骤**：
   - 在 [fuse_transpose_matmul.py:142](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/compiler_pass/fuse_transpose_matmul.py#L142) 确认 `FuseTransposeMatmul` 产出名为 `NT_matmul` 的 PrimFunc。
   - 在 [fuse_dequantize_matmul_ewise.py:75-80](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/compiler_pass/fuse_dequantize_matmul_ewise.py#L75-L80) 确认 `FuseDequantizeMatmulEwise` 的 `_check_matmul` 认得 `NT_matmul` 前缀。
   - 注意二者所在 Phase：`FuseTransposeMatmul` 在 Phase 1，`FuseDequantizeMatmulEwise` 在 Phase 3——前者先产出 `NT_matmul`，后者后到、识别并继续往上融 dequantize。
3. **现象**：一个 pass 的「输出命名」成为另一个 pass 的「输入判据」，构成跨 Phase 的协作。
4. **预期**：能用一句话说出「`FuseTransposeMatmul`（Phase 1）产出 `NT_matmul`，`FuseDequantizeMatmulEwise`（Phase 3）靠这个名字把它和 dequantize 再融成一个大 kernel」。
5. 待本地验证。

#### 4.4.5 小练习与答案

- **Q1**：`FuseTransposeMatmul` 把 `permute_dims(w)` 消除后，w 的「转置」体现在哪里？
  - **A**：体现在新生成的 `NT_matmul`（由 `te_transposed_matmul` 构造）的 `multiply_compute` 里——`b` 用转置后的索引访问，即「读取时即时转置」，不再单独物化一份转置权重。
- **Q2**：`FuseDequantizeTranspose` 为什么对 LHS 倒数第二维不为 1 的情形直接跳过？
  - **A**：源码注释明确「Do not fuse dequantize-transpose for GeMM」。它只针对 LHS 为单行（decode 风格，`(batch,1,K)`）的情形做折叠；通用 GeMM（多行 LHS）走别的路径，不在本 pass 处理范围。

## 5. 综合实践

把本讲三个模块串起来，完成一项「融合全景」追踪任务：

**任务**：选取一个量化 Llama 模型（如 Llama-3-8B q4f16_1），按下列步骤把本讲四个 pass 的作用拼成一张图。

1. **画数据流**：在一个 Transformer 解码层里，标出至少三处会被融合的位置：
   - 残差加 + RMSNorm → `FuseAddRMSNorm`（对应 [fuse_add_norm.py:187-238](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/compiler_pass/fuse_add_norm.py#L187-L238)）；
   - 注意力/FFN 里 `matmul(x, permute_dims(w))` → `FuseTransposeMatmul`（[fuse_transpose_matmul.py:31-50](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/compiler_pass/fuse_transpose_matmul.py#L31-L50)）；
   - 量化权重 `dequantize → matmul → bias/激活` → `FuseDequantizeMatmulEwise`（[fuse_dequantize_matmul_ewise.py:37-85](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/compiler_pass/fuse_dequantize_matmul_ewise.py#L37-L85)）。
2. **完成 4.2.4 的核心实践**：画出 `FuseAddRMSNorm` 融合前后的 IR 对比，并写明它消除了 `%h`（rms_norm 输入）的「写回 + 读回」显存往返，残差 `add` 则保留一次写回供下游使用。
3. **列出每个 pass 的收益类型**：填一张表（访存减少 / kernel 启动减少 / 是否手写调度 / 改图机制），核对：
   - `FuseAddRMSNorm`：访存 + 启动都减；手写调度；`PyExprMutator`。
   - `FuseDequantizeMatmulEwise`：访存大幅减（反量化权重不再物化）；声明式 `FuseOpsByPattern`。
   - `FuseTransposeMatmul`：访存减（转置权重不物化）；两阶段（`FuseOpsByPattern` + mutator）。
   - `FuseDequantizeTranspose`：访存减（折叠冗余转置）；`PyExprMutator`。
4. **回答串联问题**：为什么 `FuseDequantizeMatmulEwise` 必须排在 Phase 3，而 `FuseTransposeMatmul` 排在 Phase 1？（提示：前者要等降级成 `call_tir` 且依赖 `NT_matmul` 命名；后者负责产出该命名。）

待本地验证：若环境允许，开启 `debug_dump`（见 [u7-l1](u7-l1-compile-interface.md) 的 `CompileArgs`），对比 `debug-phase1.py` 与 `debug-phase3.py`，肉眼确认上述融合确实发生。

## 6. 本讲小结

- 算子融合的根本收益是**减少显存往返**（针对 memory-bound 算子）与**减少 kernel 启动**（针对 decode 小算子）。
- MLC 用两种改图机制：声明式 `FuseOpsByPattern`（简洁、靠命名校验）与手动 `PyExprMutator`（能注入手写调度 kernel）。
- `FuseAddRMSNorm` 把 `rms_norm(add(x1,x2),w)` 融成手写 kernel，**消除残差作为 rms_norm 输入的显存读回**，并通过 `set_var_remap` 把残差第二输出重绑给下游。
- `FuseDequantizeMatmulEwise` 用参数化扫描把 `dequantize → matmul → ewise` 多层 epilogue 累积融合，靠 `dequantize`/`matmul`/`NT_matmul` 等命名前缀恢复语义。
- `FuseTransposeMatmul` 把 `matmul(x, permute_dims(w))` 改写为 `NT_matmul`，转置烘进读取索引；产出的 `NT_matmul` 命名成为 `FuseDequantizeMatmulEwise` 的接力判据。
- `FuseDequantizeTranspose` 折叠 dequantize TIR 内部 `T_transpose` 与外层 `permute_dims` 的冗余，仅处理 decode 风格的单行 LHS。

## 7. 下一步学习建议

- 接着读 [u8-l2 派发 pass](u8-l2-dispatch-passes.md)：看 `BLASDispatch`、`DispatchTritonKernel` 如何把通用 GEMM 替换成 cuBLAS/Triton，与本讲的「融合」互补——融合负责把 epilogue 收进来，派发负责把主算子换成最优实现。
- 再读 [u8-l3 运行时函数附加 pass](u8-l3-attach-passes.md)：理解 `attach_sampler` 等为何在编译期生成。
- 想亲手验证融合效果，可参考 [u7-l1](u7-l1-compile-interface.md) 用 `compile` 接口配合 `debug_dump` 导出每个 Phase 的 IR，对照本讲的伪 IR 画图。
- 若对调度细节感兴趣，深入 `fuse_add_norm.py` 的 decode kernel（[L21-L80](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/python/mlc_llm/compiler_pass/fuse_add_norm.py#L21-L80)），研究 `sum_local`→`sum_shared` 的 warp 级归约（register file → shared memory reduction）手法。
