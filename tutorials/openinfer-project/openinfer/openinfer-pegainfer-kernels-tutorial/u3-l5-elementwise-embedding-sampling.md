# u3-l5 elementwise、embedding 与采样

## 1. 本讲目标

本讲聚焦 pegainfer-kernels 共享算子层里**最贴近上手写 kernel 的三族算子**：token embedding 查表、softcap/gelu 等逐元素（elementwise）运算、argmax 贪心解码与 FlashInfer top-k/top-p 随机采样。读完本讲，你应该能够：

1. 掌握 `embedding_batch` 的 u32 输入与列主序（column-major）输出约定，理解它的标量/向量两条 kernel 路径。
2. 独立读懂一个简单 CUDA kernel 与其 Rust wrapper 的配对关系（以 softcap、gelu_tanh_mul 为标本）。
3. 理解 argmax 家族的分层设计（单行、批式、两阶段 split、indexed）。
4. 理解 `gpu_sample_batch_into` 的「按行过滤」设计：一个 batch 里每个请求可以有不同的 temperature/top_k/top_p/min_p。
5. 学会查 KERNELS.md 的登记方式，知道哪些 op 在索引表里有 op_id，哪些没有。

## 2. 前置知识

- **HiddenStates 布局约定**（u2-l1 已讲）：token \(i\) 的向量连续存于偏移 \(i \times \text{hidden\_dim}\)。从 cuBLAS 的视角，同一块内存就是 \([\text{hidden\_dim}, \text{seq\_len}]\) 的列主序矩阵。本讲的 embedding 输出、logits 输入都遵守这个约定。
- **bf16（bfloat16）**：16 位浮点，8 位指数、7 位尾数。数值范围与 f32 相同但精度粗。kernel 普遍采用「读入升 f32 计算、单次舍回 bf16」的模式来对齐参考实现的舍入顺序。
- **wrapper 标准五步**（u3-l1 已讲）：查形 → 取 `device_ptr`（守卫保活）→ 转类型 → 传 `active_cu_stream` 并 unsafe 调 FFI → 收 `Result`。
- **FFI 错误契约**（u2-l3 已讲）：新式入口返回 `CUresult`（0 成功）；`gpu_sample_batch_flashinfer_cuda` 返回 `i32`，异常被 `PEGAINFER_FFI_GUARD` 捕获后返回 `-1`，错误细节写入 last_error 通道。
- **采样三参数**：
  - **temperature** \(T\)：softmax 前对 logits 除以 \(T\)。\(T \to 0\) 时分布退化为 one-hot，采样收敛到 argmax；
  - **top-k**：只保留概率最大的 \(k\) 个 token；
  - **top-p（nucleus）**：保留累积概率达到 \(p\) 的最小前缀集合。
  - **min_p**：按「该行最大概率 \(\times\) 阈值」过滤小概率 token（本 crate 后加的管线）。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `src/ops/embedding.rs` | embedding 查表 wrapper：单 token、批量、词表分片三个入口 + 单元测试 |
| `src/ops/elementwise.rs` | 逐元素运算 wrapper 大集合：add、silu_mul、gelu_tanh_mul、softcap、scale、suppress、dtype 转换等 |
| `src/ops/sampling.rs` | 采样 wrapper：`BatchSamplingRow`/`BatchSamplingScratch`、`gpu_sample_batch_into`、argmax 家族、logprob_topk、markov/hedge 系列 |
| `csrc/shared/elementwise.cu` | 上面两个模块的 CUDA 实现与 `extern "C"` 入口 |
| `csrc/shared/flashinfer_sampling.cu` | FlashInfer 采样入口：gather+cast → OnlineSoftmax → 采样 kernel 三段式 |
| `src/ffi/shared.rs` | 对应的 `unsafe extern "C"` 声明 |
| `KERNELS.md` | 算子索引表（op_id → wrapper → FFI symbol → 源文件 → 后端） |
| `tests/softcap_smoke.rs`、`tests/gelu_scale_smoke.rs` | softcap 与 gelu/scale 的 GPU 冒烟门禁 |

注意一个容易混淆的点：embedding 的 CUDA kernel 并不在单独的 `embedding.cu` 里，而是住在 [csrc/shared/elementwise.cu](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/csrc/shared/elementwise.cu) 中；argmax 家族则住在 `csrc/shared/argmax.cu`（本讲只引用它的登记信息，不深入）。

## 4. 核心概念与源码讲解

### 4.1 embedding_batch：u32 输入与列主序输出

#### 4.1.1 概念说明

embedding 查表是每个请求进入模型的第一步：token id（离散整数）→ 查词表矩阵的某一行 → 得到 hidden 向量。它看似只是「取一行」，实际有三个工程问题：

1. **id 必须驻留设备**。token id 若在主机侧，CUDA Graph 回放时无法更新，所以输入是 `CudaSlice<u32>`。
2. **输出布局必须与下游 GEMM 兼容**。下游 cuBLAS 期望列主序 \([\text{hidden}, \text{tokens}]\)，embedding 直接按这个布局写，零转置。
3. **带宽是瓶颈**。查表是纯内存搬运，scalar 版每线程搬 2 字节还要除法取模，向量版用 16 字节 `uint4` 一次搬 8 个 bf16。

张量并行（TP）场景还有第四个问题：词表按 rank 分片，每个 rank 只持有自己分片的行——这就是 `embedding_batch_vocab_shard` 存在的理由。

#### 4.1.2 核心流程

```
embedding_batch(ctx, embed: DeviceMatrix, token_ids: CudaSlice<u32>, out: HiddenStates)
  ├─ 取三个 device_ptr（embed / token_ids / out）
  ├─ unsafe 调 ffi::embedding_batched_cuda(embed, ids, out, hidden, seq_len, stream)
  │     ├─ embedding_vec4_ok(指针 16 字节对齐 && hidden % 8 == 0)?
  │     │     ├─ 是 → embedding_batched_vec4_kernel：grid=(seq_len, row_segs)，块内按 uint4 拷行
  │     │     └─ 否 → embedding_batched_kernel：grid-stride，逐元素 idx/hidden 定位 token
  │     └─ return map_cuda_error(cudaGetLastError())
  └─ result.result()?  → anyhow::Result<()>
```

输出写法：第 \(t\) 个 token 的 hidden 向量写进 `out[t * hidden .. (t+1) * hidden]`，即 token 连续（列主序）。CUDA 源码的注释直接写明 "Column-major output: [hidden_size, seq_len]"。

#### 4.1.3 源码精读

Rust wrapper——[src/ops/embedding.rs:76-99](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ops/embedding.rs#L76-L99)：这就是标准五步的极简形态：取 `embed`/`token_ids`/`out` 三个指针，传 `embed.cols`（hidden 宽度）与 `out.seq_len`（token 数）两个维度，收 `CUresult`。注意它**不额外查形**——维度就藏在 `DeviceMatrix::cols` 和 `HiddenStates::seq_len` 里，输出容量由调用方保证。

同文件还有两个变体值得认识：

- [src/ops/embedding.rs:43-73](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ops/embedding.rs#L43-L73)：`embedding_rows_into` 是同一 kernel 的「裸切片孪生」——输出落在常驻 decode arena（`CudaSlice<bf16>`）而非自有 `HiddenStates`，并带 `rows` 数量检查；注释还说明它被复用为逐 token 的 rope 表行拷贝（位精确复制）。
- [src/ops/embedding.rs:105-132](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ops/embedding.rs#L105-L132)：`embedding_batch_vocab_shard` 是 TP 词表分片版——token 不在本 rank 分片内就写零，调用方跨 rank all-reduce 后还原完整 embedding。

CUDA 侧标量 kernel——[csrc/shared/elementwise.cu:426-440](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/csrc/shared/elementwise.cu#L426-L440)：grid-stride 循环里对每个线性下标 `idx` 做 `token_offset = idx / hidden_size`、`dim_offset = idx % hidden_size`，再读一次 `token_ids[token_offset]` 拷一个元素。每个元素一次除法、一次取模、一次 id 重载——这正是注释里说的「标量版只能跑出约四分之一 DRAM 带宽」的原因。

向量版 kernel——[csrc/shared/elementwise.cu:451-467](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/csrc/shared/elementwise.cu#L451-L467)：`grid.x` 直接按 token 编号（每个块 `__ldg` 读一次 id），`grid.y` 把一行切成多个 16 字节向量段，block 内以 `uint4`（8 个 bf16）为单位流式拷贝。

派发逻辑——[csrc/shared/elementwise.cu:933-963](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/csrc/shared/elementwise.cu#L933-L963)：`embedding_vec4_ok` 要求 `hidden_size % 8 == 0` 且两个基地址 16 字节对齐（行偏移因为整行是整数个向量而自动保持对齐）；满足则走 vec4，否则回退标量。`EMBEDDING_VEC4_BLOCK = 128` 是 H100 实测调出来的（注释记录了 seq/bs 矩阵上 128 比 256 快约 2%）。

单元测试——[src/ops/embedding.rs:143-168](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ops/embedding.rs#L143-L168)：`embedding_batch_vectorized_path_is_bit_exact` 用 `hidden_size = 2560`（Qwen3 生产宽度 → vec4 路径）、`seq_len = 33`（故意不是块数整数倍）断言输出与主机侧 gather **位级相等**——查表是纯拷贝，允许的误差就是零。

#### 4.1.4 代码实践

1. **实践目标**：验证 embedding 的两条 kernel 路径与输出布局约定。
2. **操作步骤**：
   - 在有 GPU 的机器上运行库内单元测试（这些测试需要 `DeviceContext`，无 GPU 会 panic）：
     ```bash
     cargo test --release -p pegainfer-kernels --lib embedding_batch
     ```
   - 打开 [src/ops/embedding.rs:211-239](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ops/embedding.rs#L211-L239) 的 `embedding_batch_vocab_shard_masks_nonlocal_tokens`，注意它用 `hidden_size = 3`（不满足 `% 8 == 0`）强制走标量路径。
3. **需要观察的现象**：vec4 路径测试与标量路径测试都能通过；后者构造的 4 个 token id 中两个越界于本 rank 分片，输出对应位置是 0.0。
4. **预期结果**：全部 embedding 测试绿灯；分片测试断言的期望向量 `[10,11,12, 20,21,22, 0,0,0, 10,11,12]` 正是「本分行拷贝、非本分清零」的直观展示。
5. 测试运行结果：**待本地验证**（需要 GPU 环境；命令本身与 crate 测试组织方式一致）。

#### 4.1.5 小练习与答案

**练习 1**：`embedding_batch` 的 `out` 是 `HiddenStates`，它的 `hidden_dim` 和 `seq_len` 分别来自 wrapper 里的哪个值？

答案：`hidden_dim` 对应 `embed.cols`（kernel 参数 `hidden_size`），`seq_len` 对应 token 数（kernel 参数 `seq_len = out.seq_len`）。输出第 \(t\) 个 token 的向量落在 `out.data[t * hidden_dim ..]`，与下游 cuBLAS 的列主序视角天然一致。

**练习 2**：为什么 vec4 kernel 要求 `hidden_size % 8 == 0` 而不是 `% 4 == 0`？

答案：一个 `uint4` 是 16 字节，bf16 每元素 2 字节，所以一个向量恰好装 8 个 bf16。行宽不是 8 的倍数时行尾会跨界，无法用整向量拷贝，只能回退标量 kernel（见 `embedding_vec4_ok` 的判定）。

**练习 3**：TP 场景下 `embedding_batch_vocab_shard` 为什么非本分片的 token 写零而不是跳过不写？

答案：因为输出行仍然要参与跨 rank 的 all-reduce：写零保证「本 rank 没有贡献」的语义显式落在缓冲区里，各 rank 直接对同一形状的缓冲做规约即可还原完整 embedding（对应官方 ParallelEmbedding 的实现，见 kernel 注释）。

### 4.2 softcap 与 gelu：elementwise 家族的舍入纪律

#### 4.2.1 概念说明

elementwise 家族（`src/ops/elementwise.rs`，30+ 个导出函数）覆盖加法、残差缩放累加、dtype 转换、行列搬运、激活函数等。这一族是学习「wrapper ↔ kernel 配对」的最佳标本，因为每个 kernel 都短小独立。本讲选三个 gemma4 线的代表：

- **softcap**（最终 logits 软限幅）：\( x' = c \cdot \tanh(x / c) \)，Gemma 4 在所有已发布尺寸上声明 `final_logit_softcapping = 30.0`。
- **gelu_tanh_mul**（MLP 激活）：Gemma 4 用 `gelu_pytorch_tanh` 近似——
  \[ \text{gelu}(x) = 0.5\,x\left(1 + \tanh\!\left(\sqrt{2/\pi}\,\left(x + 0.044715\,x^3\right)\right)\right) \]
  再乘 up 投影：`out = gelu_tanh(gate) * up`。
- **scale_bf16**（逐层标量乘）：Gemma 4 的 `layer_scalar` 是 `[1]` 权重，加载时读到主机，kernel 收到的只是一个 `f32` 标量。

它们共同的纪律是**舍入顺序必须与参考实现对齐**：HF 参考是 bf16 张量上的逐元素链，所以 kernel 在 f32 里算激活、先舍到 bf16、再在 f32 里乘、最后再舍一次——两次舍入的位置不是性能选择，是数值对拍（parity）的硬约束。

#### 4.2.2 核心流程

以 `softcap_bf16_in_place` 为例：

```
wrapper (Rust)
  ├─ 校验 cap 有限且 > 0（否则 bail）
  ├─ checked_extent → 元素总数，checked_i32 → 防 i32 溢出
  ├─ 取 buf 的 device_ptr_mut
  ├─ unsafe ffi::softcap_bf16_in_place_cuda(ptr, cap, n, active_cu_stream)
  └─ result.result()? 收 CUresult
kernel (CUDA)
  grid-stride 循环：x = f32(buf[idx]); buf[idx] = bf16(cap * tanhf(x / cap))
```

`gelu_tanh_mul_batch_into` 多了一层 **Columns 列带** 概念：`gate` 和 `up` 是某行宽（`hidden_dim`）里的一段列区间 `[col, col+width)`，各有自己的行 stride——两者可以来自同一行融合 `gate|up` 投影的输出，kernel 按 `gate_stride`/`up_stride` 分别寻址，免拆分直接算。

#### 4.2.3 源码精读

softcap wrapper——[src/ops/elementwise.rs:1089-1112](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ops/elementwise.rs#L1089-L1112)：先拒绝非法 cap（0、负数、NaN、Inf 全部 `bail!`），再走 `checked_extent` + `checked_i32` 的标准防线，最后传活跃流调用 FFI。doc 注释明确写了公式与「f32 内算、单次舍回 bf16」的依据。

softcap kernel——[csrc/shared/elementwise.cu:352-362](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/csrc/shared/elementwise.cu#L352-L362)：三行主体的 grid-stride 循环，`__bfloat162float` 升精度、`tanhf`、`__float2bfloat16` 单次舍回。

gelu wrapper——[src/ops/elementwise.rs:1019-1083](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ops/elementwise.rs#L1019-L1083)：`Columns` 借 `Into<Columns>` 泛型接入；先校验 gate/up 列带同宽、输出形状匹配、列带不越界且 `checked_extent` 通过，然后把列带起点换算成字节偏移加到基址指针上，kernel 收到的是「列带首地址 + 行 stride」。

gelu kernel（标量）——[csrc/shared/elementwise.cu:322-343](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/csrc/shared/elementwise.cu#L322-L343)：`kSqrt2OverPi = 0.7978845608028654f` 与 `0.044715f` 立方项；注意 L341 的双重舍入 `__float2bfloat16(__bfloat162float(__float2bfloat16(gelu_g)) * u)` ——激活先落 bf16 再乘，正是对齐 HF 的 op 序列。

gelu 向量版与派发——[csrc/shared/elementwise.cu:832-888](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/csrc/shared/elementwise.cu#L832-L888)：`gelu_tanh_mul_vec8_kernel` 每线程用 `uint4` 处理 8 个元素（`Pack8` union），逐元素算术与标量版完全相同（两者舍入一致）；launcher `gelu_tanh_mul_cuda` 在 `cols`、两个 stride 均 `% 8 == 0` 且三个指针 16 字节对齐时选 vec8，否则回退标量——与 embedding 的 vec4 派发是同一个模式。

scale kernel——[csrc/shared/elementwise.cu:392-401](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/csrc/shared/elementwise.cu#L392-L401)：最简的 in-place 乘标量。它的 wrapper（[src/ops/elementwise.rs:1184-1205](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ops/elementwise.rs#L1184-L1205)）只校验 scale 有限——不限制正负，因为 `layer_scalar` 语义上允许任意有限值。

FFI 声明——[src/ffi/shared.rs:219-224](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ffi/shared.rs#L219-L224)（softcap）、[src/ffi/shared.rs:201-210](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ffi/shared.rs#L201-L210)（gelu）：都是 `-> CUresult` 的新式契约，与 u2-l3 讲的错误通道对接。

**关于 KERNELS.md 登记**：截至当前 HEAD，`softcap_bf16_in_place`、`gelu_tanh_mul_batch_into`、`scale_bf16_in_place` **没有**出现在 KERNELS.md 的表格中；表里登记的 elementwise 条目是跨模型线复用的 `qwen3_4b.elementwise.add`（[KERNELS.md:35](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/KERNELS.md#L35)）与 `shared.elementwise.accumulate_bf16_token_scaled_to_f32`（[KERNELS.md:13](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/KERNELS.md#L13)）。这三个 gemma4 专属算子目前只服务单条模型线，是「未进路由表」的典型样例——读源码时不要因为 KERNELS.md 里查不到就怀疑函数不存在，反过来也要养成「改动后检查是否该补登记」的习惯（见 Editing Rule 一节）。

#### 4.2.4 代码实践

1. **实践目标**：跑通 softcap 与 gelu/scale 的两个冒烟门禁，理解「主机镜像参考 + 2 ulp 容差」的测试写法。
2. **操作步骤**：
   ```bash
   # 在 GPU 机器上；PEGAINFER_REQUIRE_GPU=1 让「没有设备」从 skip 变为失败
   PEGAINFER_REQUIRE_GPU=1 cargo test --release -p pegainfer-kernels --test softcap_smoke
   PEGAINFER_REQUIRE_GPU=1 cargo test --release -p pegainfer-kernels --test gelu_scale_smoke
   ```
   然后对照 [tests/softcap_smoke.rs:14-16](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/tests/softcap_smoke.rs#L14-L16) 的 `host_softcap`：主机参考就是 `bf16::from_f32(cap * (x / cap).tanh()).to_f32()`，与 kernel 公式逐操作对应；容差取 `(e.abs() * 0.008).max(0.02)`——两个 bf16 ulp 相对、近零处兜底。
3. **需要观察的现象**：`softcap_matches_host_reference`、`gelu_tanh_mul_matches_host_reference`、`scale_bf16_scales_exactly` 全绿；`softcap_rejects_bad_cap` 与 `scale_bf16_rejects_non_finite_scale` 验证非法参数在 wrapper 层就被拒绝（kernel 根本不发射）。
4. **预期结果**：scale 测试因为刻意选了 2 的幂次标量，断言是**位级相等**；softcap/gelu 是容差比较（设备 `tanhf` 与主机 `tanh` 可能差最后一个 f32 ulp）。
5. 测试运行结果：**待本地验证**（需要 GPU）。

#### 4.2.5 小练习与答案

**练习 1**：gelu kernel 里为什么先把激活结果 `__float2bfloat16(gelu_g)` 再转回 f32 去乘 up，而不是直接 `gelu_g * u` 一次舍入？

答案：因为 HF 参考实现里 `act_fn(gate)` 会先物化成一个 bf16 张量，再与 up 做逐元素乘。中间那次 bf16 舍入是参考 op 序列的一部分；跳过它会让某些边界值差一个网格步，parity 测试就会失败。

**练习 2**：`softcap_bf16_in_place` 为什么要求 `cap > 0`？`cap = 0` 会发生什么？

答案：公式 \( c\tanh(x/c) \) 在 \( c=0 \) 时除零。wrapper 在 L1090-1094 直接 `bail!`，把 0、负数、NaN、Inf 全部挡在主机侧——这是「结构化检查优先于设备侧灾难」的防线模式。

**练习 3**：对照 vec8 派发条件，如果 `gate_stride = 4096`、`up_stride = 4096`、`cols = 5120`、指针全部对齐，会走哪条 kernel？

答案：`cols % 8 == 0`、两个 stride `% 8 == 0`、指针 16 字节对齐——全部满足，走 `gelu_tanh_mul_vec8_kernel`。

### 4.3 argmax 家族：贪心解码的四层演进

#### 4.3.1 概念说明

贪心解码（greedy）= 每行 logits 取最大者下标。看起来简单，但在推理引擎里它要同时满足：批式（一个 batch 多行）、arena 稀疏（只对活跃请求的行算）、长词表并行度（一行 15 万个元素不能只有一个块扫）、确定性平局规则。于是 wrapper 层出现了四层：

| 函数 | 形态 | 典型用户 |
| --- | --- | --- |
| `argmax` | 单向量，临时分配输出，D2H 回读 | 旧路径/调试 |
| `argmax_batch_bf16_into` | 连续多行，预分配输出 | 批式贪心门禁 |
| `argmax_bf16_split_into` | 两阶段：tile 部分和 → 每行 finalize | 长词表高并行 |
| `argmax_batch_bf16_split_indexed_into` | split + 行索引（arena 里挑行） | 生产 decode epilogue |

平局与 NaN 规则是全家族共享的硬契约：**全局下标最小者胜出，NaN 永不获胜**（见 `argmax_bf16_split_into` 的 doc 注释）。

#### 4.3.2 核心流程

两阶段 split 版的并行分解：把一行 \(n\) 个元素切成 \(\lceil n / 4096 \rceil \) 个 tile，每个 tile 由一个 CTA 算出局部 (value, global index)，写入 partials 缓冲；第二阶段每行一个块把 partials 归约成最终结果。partials 容量由 Rust 侧的 `argmax_batch_bf16_split_partials_len` 给出：

\[ \text{partials} = \text{rows} \times \left\lceil \frac{\text{vocab}}{4096} \right\rceil \]

因为 partials 携带**全局**下标，每行结果与单块扫描位级相同、且与其他行无关——并行化没有改变语义。

#### 4.3.3 源码精读

单行版——[src/ops/sampling.rs:296-323](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ops/sampling.rs#L296-L323)：`argmax` 临时分配一个 `i32` 输出，调 FFI 后 D2H + `ctx.sync()` 回读。注意它的 FFI（[src/ffi/shared.rs:255](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ffi/shared.rs#L255)）**返回 void**——这是 u3-l1 讲过的第一代形态：无错误回收，全靠后续 CUDA 调用暴露问题。doc 注释同时指出：模型 decode 路径实际走 `pegainfer-sample` 的 `select_batch`（批式）。

批式版——[src/ops/sampling.rs:325-366](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ops/sampling.rs#L325-L366)：`argmax_batch_bf16_into` 校验 `values`（每行一个 bf16 最大值槽）与 `out`（每行一个 i32 下标槽）容量后一次发射；同样不回收错误码。

split 版——[src/ops/sampling.rs:394-444](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ops/sampling.rs#L394-L444)：`argmax_bf16_split_into` 的检查清单值得逐条读：logits 容量 ≥ `rows * n`、输出容量 ≥ rows、partials 容量 ≥ `argmax_batch_bf16_split_partials_len(rows, n)`（容量公式在 [src/ops/sampling.rs:368-371](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ops/sampling.rs#L368-L371)，tile 常量 4096）。

indexed 版——[src/ops/sampling.rs:450-512](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ops/sampling.rs#L450-L512)：`argmax_batch_bf16_split_indexed_into` 多收一个 `row_indices: CudaSlice<i32>`——decode arena 里只挑活跃请求的行。这正是 KERNELS.md 里 `qwen3_4b.sampling.greedy` 登记的生产路径（[KERNELS.md:36](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/KERNELS.md#L36)：「compact greedy rows, one indexed batched argmax read-back per step」）。CUDA 实现统一在 `csrc/shared/argmax.cu`。

#### 4.3.4 代码实践

1. **实践目标**：用纸面计算 + 源码追踪理解 split argmax 的缓冲契约。
2. **操作步骤**：
   - 设 `rows = 4`、`vocab = 151936`（Qwen3 词表），手算 `argmax_batch_bf16_split_partials_len(4, 151936)`。
   - 在 [src/ffi/shared.rs:829](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ffi/shared.rs#L829) 附近找到 `argmax_batch_bf16_cuda` 的 extern 声明，再打开 `csrc/shared/argmax.cu` 找到同名入口，确认 finalize 阶段如何用 partials 里携带的全局下精选出「最小下标优先」的赢家。
3. **需要观察的现象**：partials 公式 `rows * ceil(vocab / 4096)` 与 kernel 内 tile 划分一致；C++ 入口的参数校验在 Rust 检查之外构成第二道防线。
4. **预期结果**：手算得 \(\lceil 151936/4096 \rceil = 38\)，partials = 4 × 38 = **152** 个元素。
5. `csrc/shared/argmax.cu` 的逐行内容：**待确认**（本讲未逐行精读该文件；以上行为依据 wrapper doc 注释与 KERNELS.md 登记，深入阅读留给读者按上述路径验证）。

#### 4.3.5 小练习与答案

**练习 1**：为什么生产 decode 用 indexed 版而不是连续批式版？

答案：decode arena 的行槽是按请求分配的，活跃请求可能只占 arena 的稀疏子集；indexed 版只对 `row_indices` 列出的行计算并紧凑写回，省掉对空行的扫描与多余 D2H。

**练习 2**：「NaN 永不获胜」在贪心解码里为什么重要？

答案：某个 logit 变 NaN（例如坏权重或上溢）时，若 NaN 能赢，argmax 会稳定选中它的下标，模型输出被钉死；该规则让 NaN 行为退化成「NaN 之外的最大值胜出」，配合上游 kimi-k2 router 的 NaN 修复思路（选出的专家互不重复）属于同一类鲁棒性设计。

**练习 3**：`argmax`（单行版）与 `argmax_batch_bf16_split_into` 的 wrapper 在错误处理上有何代差？

答案：前者调用的 `argmax_cuda` 返回 void，launch 失败无从得知；后者的 `argmax_batch_bf16_split_cuda` 同样返回 void（第一代 FFI），但 wrapper 层用完整的容量前置检查把最常见的失败模式（缓冲不足）挡在发射之前——这是「防线前移」而非「错误回收」的过渡形态。

### 4.4 gpu_sample_batch：按行过滤的 FlashInfer 采样

#### 4.4.1 概念说明

随机采样是贪心的对偶：不是取最大，而是按概率分布抽一个 token。引擎的真实负载是**混合批**——同一批里请求 A 要 temperature=0.7+top_p=0.9，请求 B 要 top_k=50，请求 C 全关。`gpu_sample_batch_into` 的「按行过滤」（per-row filters）设计正是为此：

- 每行一个 `BatchSamplingRow { row, temperature, top_k, top_p, min_p }`；
- temperature/top_k/top_p/min_p 都是**每行独立的设备端数组**，FlashInfer kernel 按行读取；
- 贪心行（`temperature <= 0` 或 `top_k == 1`）被显式拒绝——它们属于 argmax 路径，见 wrapper 的 `ensure!`；
- min_p 行被**分区成独立一趟**：min_p 管线的 u 缩放与幸存者谓词与融合快路径不同，混跑会让 `min_p == 0` 的行抽到与单独跑不同的 token，所以 wrapper 在这一层（而不是每个调用方）做拆分。

与 argmax 家族最大的不同：整批只需**三次 kernel 发射、一次同步、一次 D2H**（gather+cast → softmax → 采样），比逐请求采样的 host 往返少几个数量级。

#### 4.4.2 核心流程

```
gpu_sample_batch_into(ctx, logits: HiddenStatesRef, rows, seed, scratch)
  ├─ 全 min_p 或全非 min_p？→ 直接一趟 sample_uniform_batch_into
  └─ 混批：partition 成 (plain, minp) 两组
       ├─ plain 趟：seed
       ├─ minp 趟：minp_seed = seed ^ 0x9E37_79B9_7F4A_7C15（避免两趟 philox 子序列重叠）
       └─ 按 rows 原顺序交织两组结果返回

sample_uniform_batch_into（每趟）
  ├─ 逐行校验：row < arena 行数、temperature > 0 有限、top_p ∈ (0,1]、min_p ∈ [0,1)
  ├─ top_k 钳位：k <= 0 或 k >= vocab → vocab（= 禁用）
  ├─ H2D：row_indices / temperature / top_k / top_p（+ min_p 如启用）
  ├─ unsafe ffi::gpu_sample_batch_flashinfer_cuda(...) → err: i32
  │    C++ 内部三段：
  │      1) gather_cast_logits_f32_kernel：按行 gather bf16 → 紧凑 f32
  │      2) FlashInfer OnlineSoftmax（带每行 temperature，词表切分策略）
  │      3) min_p 管线：RadixTopKRenorm → TopPRenorm → MinPSamplingFromProb
  │         快路径：TopKTopPSamplingFromProb / TopPSamplingFromProb / SamplingFromProb
  ├─ D2H 读 out + valid，ctx.sync()
  └─ 校验 valid[i] != 0 且 token ∈ [0, vocab) → Vec<u32>
```

随机性契约：一次调用一个 philox seed，行与行靠 philox 子序列解相关，因此**调用方必须每个 decode step 换新 seed**（doc 注释原文）。同 seed 完全可复现（有测试钉住）。

#### 4.4.3 源码精读

行描述与 scratch——[src/ops/sampling.rs:22-77](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ops/sampling.rs#L22-L77)：`BatchSamplingScratch` 一次性预分配 probs/row_indices/temperature/top_k/top_p/min_p/topk_row_states/valid/out/softmax_workspace 十块缓冲，`max_rows × vocab` 定容。softmax workspace 的字节数公式 `max_rows * vocab.div_ceil(8192) * 8 + 256` 对应 FlashInfer OnlineSoftmax 的词表切分策略（每 8192 列一组 `{f32 max, f32 denominator}` 部分和 + 对齐余量）——scratch 尺寸必须与 kernel 内部假设严格一致，这类「尺寸公式跨语言耦合」是共享算子层最需要小心的维护点。

入口与 min_p 分区——[src/ops/sampling.rs:94-124](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ops/sampling.rs#L94-L124)：分区逻辑与第二趟 seed 派生（异或黄金比例常数 `0x9E37_79B9_7F4A_7C15`）的注释非常值得精读——两趟的子序列都从 0 重启，复用同一 seed 会让 minp 行 i 与 plain 行 i 拿到同一条均匀流。

单趟实现——[src/ops/sampling.rs:127-290](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ops/sampling.rs#L127-L290)：逐行校验（L156-197）把非法参数全部挡在主机侧；top_k 钳位注释点明「FlashInfer 读 u32，禁用即任何 k ≥ vocab」；FFI 调用（L229-255）传入 `has_top_k_filter`/`has_top_p_filter` 两个标量开关，让 C++ 侧直接选对应 kernel 而不用每行动态分派；读回后（L263-289）校验 `valid` 标志与 token 范围。

C++ 入口——[csrc/shared/flashinfer_sampling.cu:69-149](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/csrc/shared/flashinfer_sampling.cu#L69-L149)：`PEGAINFER_FFI_GUARD_BEGIN/END(-1)` 包裹（返回类型是 `int` 而非 `CUresult`：0 成功、cudaError 枚举值、-1 异常）；三段式依次是 [gather_cast](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/csrc/shared/flashinfer_sampling.cu#L19-L35)（bf16→f32 行收集，`GATHER_CAST_ELEMS_PER_THREAD = 8`）、`OnlineSoftmax`、按过滤器组合选采样 kernel。函数头的长注释解释了为什么不用 FlashInfer 的 `seed_arr`（它整批只读 `seed_arr[0]` 并把 `blockIdx.x` 折进子序列——请求的随机流会随它在批里的位置漂移）。

FFI 声明——[src/ffi/shared.rs:285-305](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ffi/shared.rs#L285-L305)：19 个参数的签名，返回 `i32`；Rust 侧 `ensure!(err == 0, ...)` 配 `ffi_exception_message(err)` 解码细节。

单元测试——[src/ops/sampling.rs:1160-1225](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ops/sampling.rs#L1160-L1225)：这是本讲综合实践的直接依据。三个在测行混在一批里：top_k=5 行必须落进 top-5 集合、top_p=0.5 行必须抽中唯一核内 token、**temperature=0.05 行必须等于 argmax token 31999**——64 个 seed 全部成立。测试装置还用 `POISON_TOKEN = 7777`（logit 20.0）投毒所有非在测行：行 gather 若拿错行，所有断言立刻崩——这是「arena 稀疏行」场景的标准测试技巧。

KERNELS.md 登记：`shared.sampling.batch_topk_topp`（[KERNELS.md:15](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/KERNELS.md#L15)）与 `qwen3_4b.sampling.random`（[KERNELS.md:37](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/KERNELS.md#L37)）都指向同一 FFI symbol；注意 Runtime owner 一列在 qwen3 行写的是 `pegainfer-core::ops::select_batch_tokens_into`——贪心与非贪心在 core 层汇合到一个 select 入口，再分流到本讲的 argmax / gpu_sample 两条路。

#### 4.4.4 代码实践

1. **实践目标**：通过既有单元测试验证「温度趋近 0 → 采样收敛到 argmax」。
2. **操作步骤**：
   ```bash
   cargo test --release -p pegainfer-kernels --lib batch_sampling
   ```
   重点读 [src/ops/sampling.rs:1160-1225](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ops/sampling.rs#L1160-L1225)：`row_t` 在 token 31999 上放 logit 4.0、其余为 0，`temperature = 0.05`，断言 64 个 seed 全部抽中 31999。
3. **需要观察的现象**：温度极低时 softmax 退化为 one-hot（\( e^{4/0.05} \) 与 \( e^{0/0.05} \) 的比值约 \(e^{80}\)，远超 f32 范围），采样器只能抽中 argmax；同一测试里 top_k/top_p 行的行为也各自被钉住。
4. **预期结果**：`batch_sampling_*` 系列测试全绿（含同 seed 可复现、异 seed 发散、min_p 掩蔽、贪心行被拒绝）。
5. 测试运行结果：**待本地验证**（需要 GPU）。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `gpu_sample_batch_into` 拒绝 `temperature = 0` 的行？

答案：temperature=0 在 softmax 里是除零；语义上它就是贪心，应走 argmax 路径。wrapper 的 `ensure!` 报错信息直接写了「greedy rows take the argmax path」，并有 `batch_sampling_rejects_greedy_rows` 测试钉住。

**练习 2**：混批里 min_p 行为什么要单独跑一趟、且第二趟要换 seed？

答案：min_p 管线的 u 缩放（`u * q`）与幸存者谓词与融合快路径不同，混跑会改变 `min_p == 0` 行的采样结果（破坏「单独跑与批里跑结果一致」）；而两趟的 philox 子序列都从 0 重启，复用 seed 会让 minp 行 i 与 plain 行 i 共享同一条均匀流、产生相关样本，所以第二趟用 `seed ^ 0x9E37_79B9_7F4A_7C15`。

**练习 3**：`BatchSamplingScratch::new` 的 softmax workspace 公式里为什么是 `div_ceil(8192)`？

答案：FlashInfer OnlineSoftmax 对小 batch × 大词表（典型 decode 形态）采用词表切分策略：每 8192 列产生一对 `{max, denominator}` 部分和，scratch 按 `max_rows * ceil(vocab/8192)` 对准备。改小 tile 或换策略时这个跨语言公式必须同步改——这正是它被集中在 scratch 构造里的原因。

## 5. 综合实践

把本讲四个模块串起来：**embedding → logits → 贪心 vs 采样收敛对比**。以下测试代码为「示例代码」（基于 `src/ops/embedding.rs` 与 `src/ops/sampling.rs` 既有单元测试的真实 API 写成，**未在本环境运行过**），放进 `tests/` 下即可用 `cargo test --release -p pegainfer-kernels --test <文件名>` 运行：

```rust
//! 示例代码：贪心与采样在温度趋近 0 时收敛一致的对比门禁。
use half::bf16;
use pegainfer_kernels::ops::{
    argmax_batch_bf16_into, embedding_batch, gpu_sample_batch_into, BatchSamplingRow,
    BatchSamplingScratch,
};
use pegainfer_kernels::tensor::{DeviceContext, DeviceMatrix, HiddenStates};

const VOCAB: usize = 64;  // 小词表便于人工核对
const HIDDEN: usize = 16; // % 8 == 0 -> embedding 走 vec4 快路径
const ROWS: usize = 3;

#[test]
fn greedy_and_sampling_converge_as_temperature_shrinks() {
    let ctx = DeviceContext::new().expect("GPU required");

    // 1) embedding：u32 token id -> 列主序 hidden 行（4.1）
    let embed_host: Vec<bf16> = (0..VOCAB * HIDDEN)
        .map(|i| bf16::from_f32((((i % 509) as f32) - 254.0) * 0.01))
        .collect();
    let embed = DeviceMatrix::from_host(&ctx, &embed_host, VOCAB, HIDDEN).expect("embed");
    let ids: Vec<u32> = vec![3, 5, 7];
    let token_ids = ctx.stream.clone_htod(&ids).expect("ids");
    let mut hidden = HiddenStates::zeros(&ctx, HIDDEN, ROWS).expect("hidden");
    embedding_batch(&ctx, &embed, &token_ids, &mut hidden).expect("embedding");
    // embedding 与 logits 无权重连接，这里仅验证查表本身位精确：
    let got = hidden.to_host(&ctx).expect("hidden D2H");
    for (t, &id) in ids.iter().enumerate() {
        let row = &embed_host[id as usize * HIDDEN..(id as usize + 1) * HIDDEN];
        assert!(row.iter().zip(&got[t * HIDDEN..(t + 1) * HIDDEN])
            .all(|(a, b)| a.to_bits() == b.to_bits()));
    }

    // 2) 确定性伪随机 logits：无 rand 依赖，行内无平局（34*i mod 97 在
    //    任意 97 个连续 i 上单射），保证 argmax 唯一。
    let logits_host: Vec<bf16> = (0..ROWS * VOCAB)
        .map(|i| bf16::from_f32((((i * 131) % 97) as f32) - 48.0))
        .collect();
    let logits = HiddenStates::from_host(&ctx, &logits_host, VOCAB, ROWS).expect("logits");

    // 3) 贪心路径：argmax_batch_bf16_into（4.3）
    let mut values = ctx.stream.alloc_zeros::<bf16>(ROWS).expect("values");
    let mut greedy = ctx.stream.alloc_zeros::<i32>(ROWS).expect("greedy");
    argmax_batch_bf16_into(&ctx, &logits, &mut values, &mut greedy).expect("argmax");
    let greedy: Vec<i32> = ctx.stream.clone_dtoh(&greedy).expect("D2H greedy");
    ctx.sync().expect("sync");
    // 主机侧复核：每行的期望 argmax
    for (r, got) in greedy.iter().enumerate() {
        let row = &logits_host[r * VOCAB..(r + 1) * VOCAB];
        let want = row.iter()
            .enumerate()
            .max_by(|a, b| a.1.partial_cmp(b.1).unwrap())
            .unwrap().0 as i32;
        assert_eq!(*got, want, "row {r}: device argmax != host argmax");
    }

    // 4) 采样路径：temperature -> 0 时收敛到 argmax（4.4）
    let mut scratch = BatchSamplingScratch::new(&ctx, ROWS, VOCAB).expect("scratch");
    for seed in 0..32u64 {
        let rows: Vec<BatchSamplingRow> = (0..ROWS)
            .map(|row| BatchSamplingRow {
                row,
                temperature: 0.01, // 趋近 0 但必须 > 0
                top_k: -1,         // 禁用（= 任何 k >= vocab）
                top_p: 1.0,        // 关闭
                min_p: 0.0,        // 关闭
            })
            .collect();
        let sampled = gpu_sample_batch_into(&ctx, logits.as_ref(), &rows, seed, &mut scratch)
            .expect("sample");
        for i in 0..ROWS {
            assert_eq!(sampled[i] as i32, greedy[i],
                "seed {seed} row {i}: temperature→0 应收敛到 argmax");
        }
    }
}
```

操作步骤与观察点：

1. 把上面的示例存为 `tests/greedy_sampling_convergence.rs`（这属于你自己的练习产物，不要提交到源码库）。
2. 在 GPU 机器上运行：
   ```bash
   PEGAINFER_REQUIRE_GPU=1 cargo test --release -p pegainfer-kernels \
     --test greedy_sampling_convergence -- --nocapture
   cargo test --release -p pegainfer-kernels --test softcap_smoke
   cargo test --release -p pegainfer-kernels --test gelu_scale_smoke
   ```
3. 把 `temperature` 从 0.01 逐步调大到 1.0 再到 4.0，观察断言在哪个温度开始失败——这正是采样从「退化为贪心」过渡到「覆盖次优 token」的边界（对照 `batch_sampling_applies_per_row_temperature` 的两 token 分布实验）。
4. 把 `top_k` 从 `-1` 改成 `1`，预期行为：等同贪心但仍走采样 kernel；再改成 0（也是禁用）验证语义一致。
5. 预期结果：temperature=0.01 时 32 个 seed × 3 行全部与 argmax 一致；运行结果**待本地验证**。

## 6. 本讲小结

- **embedding_batch**：u32 设备端 token id → 列主序 `[hidden, tokens]` 输出；`hidden % 8 == 0` 且 16 字节对齐走 `uint4` 向量路径（约 4 倍带宽），否则回退标量；TP 场景用 vocab_shard 版 + all-reduce。
- **elementwise 家族的纪律是舍入顺序**：softcap/gelu 的「f32 计算、按参考 op 序列落 bf16」不是风格偏好，而是 parity 门禁的硬约束；wrapper 层用 `checked_extent`/`checked_i32`/参数有限性检查把失败挡在 kernel 发射之前。
- **argmax 家族四层演进**：单行 → 批式 → 两阶段 split（partials 携带全局下标，与单块扫描位级一致）→ indexed（arena 稀疏行），平局最小下标胜、NaN 永不赢；单行版 FFI 返回 void 是第一代形态的活化石。
- **gpu_sample_batch 的按行过滤**：每行独立的 temperature/top_k/top_p/min_p 设备端数组 + 两个整批 filter 开关，三次发射一次同步搞定混合批；贪心行被拒绝、min_p 行独立成趟且第二趟换 seed。
- **KERNELS.md 登记的真实样貌**：embedding/sampling/argmax 有 op_id（且 qwen3 行的 Runtime owner 指向 `pegainfer-core::ops::select_batch_tokens_into` 的汇合入口），而 gemma4 专属的 softcap/gelu/scale 尚未入表——索引表登记「需要跨线路由的算子面」，不是全部算子的清单。

## 7. 下一步学习建议

- **u3-l6（norm 家族与第一个 CUDA kernel 精读）**：把本讲的「wrapper ↔ kernel 配对」推进到 head→blockIdx、head_dim→threadIdx 的映射推导，并认识 `common.cuh` 的公共设施。
- **u4-l2（端到端新增一个共享算子）**：本讲的 softcap/gelu 已经是五步改动（csrc kernel → FFI 声明 → wrapper → smoke 测试 → KERNELS.md 表行）的现成先例，下一讲把它变成肌肉记忆。
- 顺手对照阅读：`csrc/shared/argmax.cu`（补上本讲 4.3.4 留下的待确认项）与 `pegainfer-core` 的 `select_batch_tokens_into`（看贪心/随机两条路如何在 core 层汇合）。
