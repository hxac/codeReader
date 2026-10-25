# bookend：embedding、final norm 与 lm_head

## 1. 本讲目标

学完本讲，你应该能够：

- 读懂 `src/bookend.rs` 的三个薄封装：`glm52_embed_into`、`glm52_final_norm_into`、`glm52_lm_head_into`，并说出它们各自的形状契约。
- 解释 embedding 查表为什么必须「设备端读 token_ids」才能被 CUDA Graph 安全回放，以及每步输入是如何被原地重写的。
- 弄清 `Rows<C>` 「缓冲自带行数、宽度是类型事实」的约定，理解 bookend 为什么不做任何形状校验。
- 知道 bookend 在整步 forward 中的位置：它夹住 78 层堆栈的两端，全部是 bf16 运算而非 fp8。

本讲承接 u3-l1（FP8 块量化原语）：上一讲的主角是 fp8 投影 helper，本讲正好是它们的「反面」——模型里仅有的三处明文 bf16 大件。

## 2. 前置知识

**bookend（书挡）**。一本摊开的书两端各有一块书挡，把中间的书页夹住。在本 crate 里，78 层 decoder 堆栈就是「书页」，而夹住它的两块「书挡」是：

- **入口端**：token embedding 查表——把 token id 变成 6144 维 hidden 向量；
- **出口端**：final RMSNorm + lm_head 投影 + 贪心 argmax——把最后一层的 hidden 变成下一个 token。

**bf16（bfloat16）**。16 位浮点格式：1 位符号、8 位指数（与 f32 相同）、7 位尾数。数值范围和 f32 一样大，精度比 f32 低。GLM5.2 的 checkpoint 里绝大多数「大」矩阵（MLA 投影、MoE 专家权重）是 fp8 e4m3 + 块 scale（见 u2-l1、u3-l1），但 embedding 表、`model.norm.weight`、`lm_head.weight` 这三个张量在 checkpoint 里就是 bf16——本讲的三个函数因此全部走共享的 bf16 kernel，完全不碰 fp8 原语。

**RMSNorm（均方根归一化）**。对每个向量的元素除以其均方根，再逐元素乘以权重 \( w \)：

\[
y_i = \frac{x_i}{\sqrt{\dfrac{1}{n}\sum_{j=1}^{n} x_j^2 + \epsilon}} \cdot w_i,\qquad n = 6144,\ \epsilon = 10^{-5}
\]

它是 LayerNorm 去掉均值中心化的变体，计算更便宜。GLM5.2 模型里每一层都有两个 RMSNorm，加上最后这个 final norm，全部共享 checkpoint 里的同一个 `rms_norm_eps = 1e-5`。

**CUDA Graph 与「输入面」**（上一讲 `scratch` 约束的延伸）。CUDA Graph 捕获一段 kernel 序列后，回放（replay）时**不会重新解析参数**：每个 kernel 用的设备指针和形状在捕获时就固化了。因此被捕获的代码只能通过「往同一块固定设备内存里写新内容」来接收新输入。这块设备内存就叫这步的输入面（input surface）。对 decode 整步 forward 来说，`token_ids` 就是输入面的一员——这就是本讲反复出现的「graph 安全」的含义。

**lm_head**。输出投影矩阵 \([V, H]\)（词表 × hidden），hidden 乘上它得到每个词的 logits，取最大者即贪心解码的下一个 token。GLM5.2 里 \( V = 154\,880 \)、\( H = 6144 \)，且 `tie_word_embeddings = false`——lm_head 与 embedding 是两份独立的权重（u2-l1 的 `probe_config_json` 钉死了这一点）。

## 3. 本讲源码地图

| 文件 | 角色 |
|------|------|
| `src/bookend.rs` | 本讲主角：三个薄封装函数，全文不到 90 行 |
| `src/config.rs` | 唯一被 bookend 引用的 GLM5.2 专属事实：`GLM52_HIDDEN`、`GLM52_VOCAB`、`GLM52_RMS_EPS` |
| `src/rows.rs` | `Rows<C>` 类型化行缓冲：形状契约的载体 |
| `src/model/step_body.rs` | bookend 的 decode 消费点：整步 forward 的首尾两拍 |
| `src/model/mod.rs` | 输入重写（`memcpy_htod` token_ids）、权重构建与 lm_head 词表分片 |
| `src/model/launch_ahead.rs` | 第二条输入重写路径：feed kernel 设备侧原地推进 |
| `src/oracle/bookend.rs` | 精度门：对三个 bookend 分别断言（实践与练习素材） |
| `src/prefill_tp.rs` | TP4 prefill 中 bookend 的另一种用法（词表并行 pack/unpack） |

## 4. 核心概念与源码讲解

### 4.1 glm52_embed_into：设备端 embedding 查表

#### 4.1.1 概念说明

embedding 查表在数学上就是一次 gather（按下标取行）：

\[
\text{out}[r, :] = E[\text{token\_ids}[r], :],\qquad E \in \mathbb{R}^{154880 \times 6144},\ r < T
\]

它连 GEMM 都不是，所以 fp8 量化在这里毫无意义；同样地，查表是逐位复制的 bf16 gather，精度是**精确**的（后面 oracle 门的断言分级会用到这一点）。

本模块要回答的核心问题是：**为什么 `token_ids` 必须是设备缓冲，kernel 在设备上读它？**

设想反例：如果 host 代码把 token id 数组直接作为 kernel 参数传进去，那么 CUDA Graph 捕获时记录的就是「当时那份 host 数据的拷贝」，之后每次回放都查同一批 token——每一步生成都重复第一个 token，服务完全错误。正确做法分两层：

1. `token_ids` 是一块**固定的设备缓冲**（`CudaSlice<u32>`），kernel 参数里的指针永远指向它；
2. 每步开始时 host 只把新内容**写进这块缓冲**（`memcpy_htod` 或设备侧 feed kernel），内容变了、指针没变，graph 回放自然查到新 token。

#### 4.1.2 核心流程

decode 一步中 embedding 相关的时序（prologue = graph 回放前的 host 侧准备）：

```text
[prologue, graph 外]
  1. host 组装 tokens_host[row] = token（来自调度器）
  2. memcpy_htod(tokens_host → self.token_ids)      ← 原地重写输入面
  3. 同一原语 gather 每行 position 的 cos/sin 旋转表行
[graph 内第一拍]
  4. glm52_embed_into: embedding_rows_into(embed 表, token_ids,
                                          T 行 → s.hidden)
  5. rms_norm(s.hidden, layers[0].input_ln) → 进入第 0 层
```

注意第 0 层的 input norm 是独立一次 `rms_norm_rows_into` 调用——因为第 0 层的「残差」就是 embedding 输出本身，没有上一层的收尾可以融合。

#### 4.1.3 源码精读

函数本体只有 8 行，几乎全部信息在签名和注释里：

- [src/bookend.rs:28-36](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/bookend.rs#L28-L36) — `glm52_embed_into`：行数 `tokens` 从**输出缓冲** `out.tokens()` 读出，然后调用共享 kernel `embedding_rows_into`。doc 注释直接点明关键事实：`token_ids` 是设备缓冲，**在设备上读取**，所以查表是 CUDA-graph 安全的——调度器每步原地重写它。
- [src/bookend.rs:25-27](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/bookend.rs#L25-L27) — 形状契约：`embed[token_ids[r]] → [T, HIDDEN]`。
- [src/model/mod.rs:1566-1575](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/model/mod.rs#L1566-L1575) — 每步 prologue 的输入重写：host 把每个 row 的 `(token, position)` 写进 `tokens_host` / `positions_host`，然后四条 `memcpy_htod` 把 token、position、slot_mapping、seq_lens 写进固定设备缓冲。这就是「内容变、指针不变」的 host 侧版本。
- [src/model/mod.rs:1576-1581](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/model/mod.rs#L1576-L1581) — 同一个 `embedding_rows_into` 原语的第二个用途：把每行 position 对应的 RoPE cos/sin 旋转表行 gather 下来（设备端按 `positions` 查表，同样是 graph 安全的）。**一个 gather 原语服务两种查表**——这是「薄封装」哲学的体现：共享 kernel 不关心你查的是词表还是旋转表。
- [src/model/step_body.rs:71](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/model/step_body.rs#L71) — 整步 forward 的第一拍就是 `glm52_embed_into(..., token_ids, &mut s.hidden)`；紧接的注释说明第 0 层 input norm 为何独立。
- 底层 kernel 签名在 `pegainfer-kernels`：[../pegainfer-kernels/src/ops/embedding.rs:43-49](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ops/embedding.rs#L43-L49) — `ids: &CudaSlice<u32>`，参数本身就是设备指针，从类型上就杜绝了「host 数组直传」的错误写法。

#### 4.1.4 代码实践：跟随一个 token id 走进 embedding（源码阅读型）

1. **实践目标**：亲手确认「token id 从调度器到 embedding 表行」的完整数据面，列出 decode 一步的全部输入面缓冲。
2. **操作步骤**：
   - 在 `pegainfer-glm52/src` 下执行 `grep -rn "memcpy_htod(&tokens_host" src/`，定位写入点；
   - 打开 `src/model/mod.rs` 的 `decode_step` 尾部（约 1540-1610 行），阅读 prologue 对 `tokens_host`、`positions_host`、`kv.slot_mapping`、`seq_lens` 的组装；
   - 打开 `src/model/step_body.rs:71`，确认 `token_ids` 以 `&CudaSlice<u32>` 穿过整个 78 层循环的起点；
   - 打开 `pegainfer-kernels/src/ops/embedding.rs` 的 `embedding_rows_into`，只读签名与开头的 buffer 校验，体会「kernel 层只见无类型切片」的边界。
3. **需要观察的现象**（静态）：`token_ids` 设备缓冲只被两类代码写——prologue 的 `memcpy_htod` 与 launch-ahead 的 feed kernel（4.3 节）；graph 内部没有任何代码写它，只读。
4. **预期结果**：写出一张「输入面五缓冲」表——`token_ids`、`positions`、`slot_mapping`、`seq_lens`、`bucket.block_table`，各配一个写入点行号。本实践不需要 GPU，可立即完成。
5. 若想看真实数值输出，可运行 oracle 门（需 GPU + GLM-5.2-FP8 checkpoint，默认 `#[ignore]`）：

   ```bash
   PEGAINFER_TEST_MODEL_PATH=/data/models/GLM-5.2-FP8 \
     cargo test --release -p pegainfer-glm52 --features glm52 --lib bookend_oracle -- --ignored --nocapture
   ```

   该命令的通过输出为 `bookend embed: digest exact (8 rows)`（见 `src/oracle/bookend.rs:247`）。待本地验证（本环境无 GPU 与权重）。

#### 4.1.5 小练习与答案

**练习 1**：如果 `glm52_embed_into` 的 `token_ids` 参数类型改成 `&[u32]`（host 切片），CUDA Graph 回放会发生什么？为什么？

**答案**：kernel 参数无法直接接受 host 切片；若先把 host 数据拷进一次性设备缓冲再启动 kernel，graph 固化的是「捕获那次分配」的指针。之后每次回放查的都是捕获时的旧 token，每步输出重复同一 token。正确做法就是现状：固定的 `CudaSlice<u32>` + 每步原地重写内容。

**练习 2**：`glm52_embed_into` 里 `let tokens = out.tokens();`——为什么行数从**输出**缓冲读，而不是单独传一个 `tokens: usize` 参数？

**答案**：`Rows<C>` 的构造函数保证 `data.len() == tokens * C`（见 4.3.3 的 `rows.rs`），行数随缓冲走。输出行数 = 输入 token 数 = 查表输出行数，从 `out` 读行数让「输入输出行数一致」成为签名事实，消灭了单独传参可能造成的行数不一致；这也让函数体内无需任何 `ensure!` 形状校验。

**练习 3**：`model.embed_tokens.weight` 和 `lm_head.weight` 各占多少显存？为什么 u2-l4 的精确显存账本必须把这两块算进去？

**答案**：两块都是 \(154880 \times 6144\) 个 bf16 = 951,582,720 元素 × 2 B ≈ 1.77 GiB，合计约 3.54 GiB。它们是每 rank 全量复制的非专家权重（u2-l2），与 EP 宽度无关，是固定开销的大头之一，账本漏掉会直接高估可用 KV 页数。

### 4.2 final norm 与 lm_head：步尾投影

#### 4.2.1 概念说明

78 层跑完后，残差流停在 `s.hidden`（\([T, 6144]\)）。出口端 bookend 做三件事：

1. **final RMSNorm**：用 `model.norm.weight` 归一化（\(\epsilon = 10^{-5}\)，与全模型共享同一 eps）；
2. **lm_head 投影**：一次 bf16 GEMM 得到 logits \([T, 154880]\)；
3. **贪心 argmax**（严格说在 step_body 里、不在 bookend.rs 里）：每行取最大 logit 的词 id。

为什么这一段保持 bf16？checkpoint 事实是：`model.norm.weight` 与 `lm_head.weight` 就是 bf16 张量（fp8 权重在 u2-l2 的契约里成对携带 `weight_scale_inv`，这两个没有）。从直觉上讲（这是直觉、不是源码声明）：norm 是逐元素运算没有矩阵可量化；lm_head 每步只投影 T 行，但它的输出直接决定采样分布，精度损失会直接变成错词。作为对照，u3-l1 的 fp8 路径省下的是 78 层 × 每层多个投影的带宽与算力，bookend 每步只各跑一次，量级完全不同。

#### 4.2.2 核心流程

```text
graph 内最后一拍:
  s.hidden [T, 6144]
    → glm52_final_norm_into → s.final_normed [T, 6144]
    → glm52_lm_head_into   → s.logits      [T, 154880]   (或 [T, shard_rows])
    → argmax_bf16_split_into → (values, indices) [T]      (两阶段、设备端)
graph 外:
  argmax D2H（pinned 落地缓冲，每行仅 6 字节）
  采样行绕过 argmax：eager 重算全词表 logits → gpu_sample_batch_into
```

lm_head GEMM 的形状安排是一个值得记住的小技巧：**把 tokens 放在 GEMM 的 n 维**。cuBLAS 输出按列主序，`[lm_head.rows, T]` 列主序在内存上恰好等于 `[T, lm_head.rows]` 行主序——一步到位得到 argmax 想要的「紧凑行主序 logits」，不需要任何转置 kernel。

#### 4.2.3 源码精读

- [src/bookend.rs:40-56](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/bookend.rs#L40-L56) — `glm52_final_norm_into`：直接转调 `rms_norm_rows_into`，传入 `GLM52_RMS_EPS`（1e-5 的 f32 版本）与 `GLM52_HIDDEN`。doc 注释给出语义：`rms_norm(hidden, model.norm.weight, eps=1e-5)`。
- [src/bookend.rs:63-88](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/bookend.rs#L63-L88) — `glm52_lm_head_into`：一次 `gemm_strided_batched_bf16`，参数依次是 `transpose_a=true`、`m=lm_head.rows`、`n=tokens`、`k=GLM52_HIDDEN`、`batch=1`。注释（58-62 行）说明 EP8 传全词表头、attention-TP decode 传本 rank 的连续词表分片；「tokens 在 n 维」的布局技巧也写在注释里。
- [src/model/step_body.rs:160-161](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/model/step_body.rs#L160-L161) — decode 整步的收尾两拍：final norm + lm_head。
- [src/model/step_body.rs:162-178](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/model/step_body.rs#L162-L178) — 设备端两阶段贪心 argmax：先按 4096 宽的 tile 并行算局部最大，再每行一个 block 汇总。注释点明两个关键事实：步的出口从「全词表行」缩到**每行 6 字节**（bf16 值 2B + i32 下标 4B），kernel 链就此结束在设备上（graph 边界）；tie 时最小下标获胜、NaN 永不获胜，与 host 扫描语义一致。
- [src/model/mod.rs:741-757](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/model/mod.rs#L741-L757) — 权重构建：`take_tensor` 取出 `model.embed_tokens.weight` 与 `lm_head.weight` 的原始字节，字节长度校验 \( V \times H \times 2 \) 后 `retype_owned` 成 bf16 `DeviceMatrix`，行数钉死 `[VOCAB, HIDDEN]`——这就是 bookend 模块注释说的「形状在 model build 时已钉死」。
- [src/model/mod.rs:758-784](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/model/mod.rs#L758-L784) — attention-TP 时额外构建 `decode_lm_head`：校验词表能被 rank 数整除后，用 `memcpy_dtod` 从全词表头切出本 rank 的连续分片 \([V/\text{ranks}, H]\)，并记下 `decode_vocab_start`。紧凑 logits 的宽度因此等于 `lm_head.rows`——同一个 bookend 函数天然支持分片头。
- [src/model/mod.rs:1392-1407](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/model/mod.rs#L1392-1407) — 采样的代价：贪心行吃 argmax 就够，但需要采样的行要完整词表分布；若 graph 里用的是分片头，就在 graph 外**eager 重算一次全头 lm_head**，只有被采样的步付这笔钱。
- [src/prefill_tp.rs:1315-1349](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/prefill_tp.rs#L1315-L1349) — TP4 prefill 的另一种消费方式：每 rank 对自己的词表分片做 final norm + lm_head + 局部 argmax，然后 `glm52_vocab_parallel_pack_launch` → NCCL all-reduce → `unpack_launch` 汇出全局 argmax（词表并行，u5-l4 展开）。

#### 4.2.4 代码实践：bookend 出口的形状与字节账本（纸面计算型）

1. **实践目标**：用真实常量算出 bookend 出口在不同 batch 下的数据量，体会「argmax 把出口压缩 5 万倍」的设计动机。
2. **操作步骤**：
   - 从 `src/config.rs` 取 \( V = 154\,880 \)、\( H = 6144 \)；
   - 对 \( T \in \{1, 8, 96\} \)（96 = `GLM52_MAX_STEP_ROWS`，见 [src/model/mod.rs:285-288](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/model/mod.rs#L285-L288)）分别计算：logits 缓冲字节数 \( T \times V \times 2 \)、argmax 出口字节数 \( T \times 6 \)、lm_head GEMM 的 FLOP 数 \( 2 T H V \)；
   - 把三行数字填进一张表。
3. **需要观察的现象**：纯计算，无运行现象；重点看压缩比。
4. **预期结果**（可自行核对）：

   | T | logits（bf16） | argmax 出口 | lm_head FLOPs |
   |---|---|---|---|
   | 1 | 309,760 B ≈ 303 KiB | 6 B | ≈ 1.90 GFLOP |
   | 8 | 2,478,080 B ≈ 2.36 MiB | 48 B | ≈ 15.2 GFLOP |
   | 96 | 29,736,960 B ≈ 28.4 MiB | 576 B | ≈ 182.7 GFLOP |

   8 行时压缩约 51,600 倍——步出口 D2H 从 MiB 级缩到几十字节，这正是 launch-ahead（u6-l4）能用极小代价阻塞等结果的前提。
5. 无需 GPU；数字均为常量推导。

#### 4.2.5 小练习与答案

**练习 1**：`glm52_lm_head_into` 的 GEMM 里 `m = lm_head.rows` 而不是 `GLM52_VOCAB`。这个「不写死」带来了哪两个功能？

**答案**：(a) attention-TP decode 可以传本 rank 的词表分片（行数 = V/ranks），输出紧凑分片 logits；(b) prefill_tp 的词表并行路径同样复用该函数对分片头投影。也就是「函数宽度跟权重走」，全头与分片头共用一段代码。

**练习 2**：为什么采样行不能只靠 graph 内的 argmax，而 model/mod.rs:1400-1407 要在 graph 外重算一次 lm_head？

**答案**：argmax 只保留每行最大者的下标，丢掉了完整分布；采样（temperature/top_k/top_p/min_p）需要整行 logits。graph 内若用了分片头，分布不完整，所以对含采样行的步，在 graph 外 eager 地用全词表头再投影一次，供 `gpu_sample_batch_into` 使用。纯贪心步完全不付这笔开销。

**练习 3**：final norm 的 eps 从哪里来、被谁校验？

**答案**：来自 checkpoint 的 `rms_norm_eps`，被 [src/config.rs:146-157](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/config.rs#L146-L157) 用 `ensure_float_close` 钉在 1e-5（容差 1e-12）；[src/config.rs:47-50](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/config.rs#L47-L50) 把它变成 GPU kernel 消费的 f32 常量 `GLM52_RMS_EPS`，全模型所有 RMSNorm 共享这一个值。

### 4.3 graph 安全的输入重写与 Rows 形状契约

#### 4.3.1 概念说明

本模块把前两节反复出现的两个机制正式讲清：

**（A）输入重写的两条路径。** graph 固化指针，输入只能经「固定设备缓冲的内容更新」流入，而更新有两条路径：

1. **prologue 的 host 写入**（4.1 已见）：graph 回放前，host 组装本步行表并 `memcpy_htod` 进输入面。每步必经，语义直白，代价是一次 host→device 拷贝加 host 侧组装。
2. **launch-ahead 的 feed kernel**（设备侧原地推进）：当前步还在 GPU 上跑时，提前入队一个小 kernel，读上一步 argmax 的结果**在设备上**把 `token_ids`、`positions`、`slot_mapping`、`seq_lens` 各推进一格，然后立刻回放下一步的 graph。整条链不经过 host，把约 0.8 ms 的 host 侧 launch 延迟藏进当前步的执行时间里（完整协议在 u6-l4，本讲只看它对输入面做了什么）。

**（B）`Rows<C>` 形状契约。** bookend 函数体内零形状校验，靠的是 [src/rows.rs](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/rows.rs#L1-L17) 的设计：宽度 `C` 是 const 泛型类型参数，行数 `tokens` 存在值里，`data.len() == tokens * C` 由唯一的构造函数 `zeros` 保证。于是「把 `[T, HIDDEN]` 缓冲传给要 `[T, VOCAB]` 的函数」直接编译不过；接收方从值读行数、从类型读宽度。历史上每个消费者入口重复的约 30 处 `ensure!(len == tokens * C)` 因此全部消失。

#### 4.3.2 核心流程

launch-ahead 对输入面的操作序列（graph 外、当前步执行期间入队）：

```text
条件: lease 成立（形状与上一 captured 步一致、无行跨页界）
  1. glm52_decode_feed_launch(argmax_indices → token_ids,    ← 设备端原地推进
                              positions+1, slot_mapping+1, seq_lens+1)
  2. embedding_rows_into(cos_table, positions) → cos          ← 重新 gather 旋转行
     embedding_rows_into(sin_table, positions) → sin
  3. bucket.graph.launch_captured()                           ← 提前回放下一步
条件不成立: 本步退化为普通路径（prologue 全量覆写 + 回放）
```

「lease 不成立就全量覆写」是安全网：投机回放赌的是「下一步形状与已捕获的 graph 一致」，赌输时按 prologue 路径重来，正确性不受影响。

#### 4.3.3 源码精读

- [src/bookend.rs:1-9](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/bookend.rs#L1-L9) — 模块注释三连：三件套全是 bf16；它们只是共享 kernel 的薄封装，GLM5.2 专属事实只有维度；**形状不在这里重验**——权重矩阵在 model build 时钉死 `[VOCAB, HIDDEN]`，`Rows` 缓冲按构造自带行列数。
- [src/rows.rs:25-30](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/rows.rs#L25-L30) — `Rows<C>` 结构：`data: CudaSlice<bf16>` + `tokens: usize`，文档断言 `len == tokens * C` by construction。
- [src/rows.rs:33-43](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/rows.rs#L33-L43) — 唯一构造点 `zeros`（分配即建立不变量）与行数读取器 `tokens()`；kernel-launch 边界才交出无类型切片（[src/rows.rs:45-53](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/rows.rs#L45-L53) 的 `data()/data_mut()`，注释写明「宽度/行数事实到此为止」）。
- [src/model/launch_ahead.rs:75-77](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/model/launch_ahead.rs#L75-L77) — lease 分支开头的注释：**只有真实行携带 slot 语义；padding 尾巴复用 slot id 0 并自喂（self-feed），无害**。padding 行查 embedding 表第 0 行、把 KV 写进 padding 页，对真实请求零影响——这正是 u7-l1 free-running 纪律里「padding 行的输入必须构造性确定」在 bookend 输入面上的体现。
- [src/model/launch_ahead.rs:107-128](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/model/launch_ahead.rs#L107-L128) — feed 链本体：校验全部完成之后（112-116 行注释解释为何所有可失败检查必须在 launch 之前），`glm52_decode_feed_launch` 原地推进四个输入缓冲、重 gather cos/sin、`launch_captured` 回放下一步 graph。
- [src/model/launch_ahead.rs:129-131](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/model/launch_ahead.rs#L129-L131) — host 侧镜像 `device_positions` 同步 +1，让下一次 lease 判断依然基于准确的位置（feed kernel 不做 host 回读，位置账本由 host 维护）。
- [src/model/mod.rs:1588-1599](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/model/mod.rs#L1588-L1599) — 普通路径把 plan、scratch、block table 打包成 `Glm52DecodeStep` 传入 step body——计划与输入面在每个 bucket 状态里成套，回放时看到的就是该 bucket 的固定指针集。

#### 4.3.4 代码实践：静态剖析 feed 链与 padding 自喂（源码阅读型）

1. **实践目标**：确认 launch-ahead 路径下输入面的全部写入者，并推演 padding 行自喂为何无害。
2. **操作步骤**：
   - 读 [src/model/launch_ahead.rs:75-133](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/model/launch_ahead.rs#L75-L133)，列出 feed kernel 更新的缓冲清单与更新顺序；
   - 找到 lease 的两个形状前提：bucket graph 已捕获（107-111 行）、没有任何行跨 64-token 页界（100-105 行，因为 feed 只把 `slot_mapping` +1，跨页就写进错误页）；
   - 用 `grep -n "GLM52_MODEL_LEN_ALIGN" src/model/mod.rs` 确认 64 对齐常量的来源。
3. **需要观察的现象**（静态推演）：padding 行的 token 永远来自上次 argmax 的 index（首步为 0），position 停在 padding 页内，KV 落在 block 0（padding 页，u7-l5）。三个量都不指向任何真实请求的数据。
4. **预期结果**：写出一段 5-8 行的论证，说明「padding 行自喂不影响任何真实行的 embedding、KV 与 argmax」。无需 GPU。
5. feed kernel 本体（`glm52_decode_feed_launch` 的 CUDA 实现）在 `pegainfer-kernels`，本实践只需理解其调用契约；深入 kernel 源码可留到 u6-l4。

#### 4.3.5 小练习与答案

**练习 1**：launch-ahead 的 feed kernel 在 graph **外**入队，为什么它更新输入面是安全的？如果把它挪进 captured graph 会发生什么？

**答案**：feed kernel 读的是**上一步 argmax 的输出**（graph 外的 D2H 目标之外的设备缓冲），依赖关系由流顺序保证：它排在下一步 graph 回放之前，而下一步 graph 又排在当前步之后。若挪进 captured graph，回放顺序虽然保留，但它消费的「上一步 argmax」在回放序列里位置固定，无法表达「只在 lease 成立时才 feed」的条件逻辑——graph 是固定 kernel 序列，条件路径必须留在 graph 外（这也呼应 u7-l1 的「无条件 collective 链」纪律：变的东西放数据里，不放控制流里）。

**练习 2**：`Rows<C>` 把宽度做成类型参数后，下列哪个错误会被编译器拦下：把 `Rows<154880>`（logits）当作 `Rows<6144>`（hidden）传给 `glm52_final_norm_into`；还是某 bucket 的 scratch 只分配了 4 行却按 8 行使用？

**答案**：前者编译失败——两个 `Rows` 是不同类型。后者编译器拦不住：行数是运行期值，`tokens()` 说多少就是多少；这由更外层的 bucket 构造纪律保证（每个 bucket 的 scratch、plan、graph 成套，见 `Glm52BucketState`）。类型管宽度、构造管行数——这就是「不变量移到构造点」的边界。

**练习 3**：为什么 `Rows` 的 `data()` 文档说「宽度/行数事实在 kernel-launch 边界停止」？

**答案**：`pegainfer-kernels` 的 kernel 包装（如 `embedding_rows_into`）接收的是无类型 `CudaSlice`，形状再次变成裸数字参数。类型化事实只在 model crate 内部成立；跨到 kernels crate 就是普通防御式校验（`embedding_rows_into` 开头的 `ensure!`）。薄封装的「薄」正来自这条清晰的边界。

## 5. 综合实践

**任务：为 bucket=8 的一步 decode 编写「bookend 时刻表」。**

把一步 decode 拆成三个时段，逐项列出 bookend 相关发生的每一件事、发生在哪条路径（graph 内 / graph 外 / prologue / feed 链）、依据的源码行号：

1. **prologue（graph 回放前）**：输入面五缓冲各自的写入者与行号（[src/model/mod.rs:1566-1587](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/model/mod.rs#L1566-L1587)）；
2. **graph 内第一拍**：`glm52_embed_into` 与第 0 层 input norm（[src/model/step_body.rs:71-83](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/model/step_body.rs#L71-L83)）；
3. **graph 内最后一拍与出口**：final norm、lm_head、两阶段 argmax、6 字节/行的 pinned D2H（[src/model/step_body.rs:160-178](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/model/step_body.rs#L160-L178) 与 [src/model/mod.rs:501-506](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/model/mod.rs#L501-L506) 的 pinned 落地缓冲注释）。

验收标准：表里每个条目都能点开对应永久链接核对；再回答两个收尾问题——(a) 若这步含一个采样行，出口多出哪一次 eager 投影（[src/model/mod.rs:1392-1407](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/model/mod.rs#L1392-1407)）；(b) 若下一步形状不变，这张表里哪些条目会被 feed 链替换（[src/model/launch_ahead.rs:117-128](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/model/launch_ahead.rs#L117-L128)）。有 GPU 时可用 `--dump-graph-png`（u6-l3）导出 bucket-8 图，在图上找到 embed 与 argmax 节点作为交叉验证（待本地验证）。

## 6. 本讲小结

- bookend 是夹住 78 层堆栈的两端：入口的 embedding 查表，出口的 final RMSNorm + lm_head + 设备端 argmax；三者全是 bf16，是模型里明文不量化的三件大件（embed 表与 lm_head 各约 1.77 GiB）。
- `src/bookend.rs` 三个函数是纯薄封装：GLM5.2 专属事实只有维度常量；形状契约完全外包——权重在 model build 时钉死 `[VOCAB, HIDDEN]`，缓冲由 `Rows<C>` 按构造携带行列数，函数体内零 `ensure!`。
- graph 安全的输入面：`token_ids` 必须是固定设备缓冲、kernel 在设备上读；内容经两条路径更新——prologue 的 `memcpy_htod` 与 launch-ahead 的 feed kernel 设备侧原地推进（赌输 lease 就全量覆写重来）。
- lm_head 把 tokens 放在 GEMM 的 n 维，列主序输出即紧凑行主序 logits；`lm_head.rows` 不写死使全头与词表分片共用一个函数，采样行为此在 graph 外 eager 重算全头。
- argmax 把步出口从 MiB 级 logits 压到每行 6 字节，两阶段 kernel 与 host 扫描语义一致（最小下标胜、NaN 不胜），并让 kernel 链结束在设备上。

## 7. 下一步学习建议

下一讲 **u3-l3《dense MLP 与 Rows 类型化行缓冲》**深入 `Rows<C>` 的设计动机（消灭约 30 处重复 `ensure!`）并走进前 3 层 dense MLP 如何复用 u3-l1 的 fp8 helper——与本讲形成 bf16/fp8 的完整拼图。之后按依赖顺序：u4-l1（MLA 前半投影，把 fp8 调度用到 78 层循环里）、u6-l2（`run_step_body` 整步组装，看 bookend 在完整前向里的位置）、u6-l3（CUDA Graph 预捕获）与 u6-l4（launch-ahead 完整协议）。建议顺带阅读 `docs/models/glm52/` 下的 `whole-step-decode-graph.md` 与 `free-running-dp.md`，本讲的「输入面」概念在那里扩展成整条 fleet 的纪律。
