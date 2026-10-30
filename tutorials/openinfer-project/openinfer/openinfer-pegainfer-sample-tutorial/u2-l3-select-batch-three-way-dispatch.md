# select_batch 三路分发：argmax、批式采样与 seeded 单行

## 1. 本讲目标

学完本讲，你应该能够：

1. 沿着 `src/lib.rs` 里的 `select_batch` 主体，从上到下走通一次完整的批式选 token 调用：参数如何被切成三个集合、每个集合走哪条 GPU 路径、结果如何按行号归位。
2. 逐条说出函数顶部四个 `ensure!` 校验各自守护的不变量（步数对齐、scratch 容量、arena 行数、vocab 宽度），以及为什么违反它们必须报错而不是「将就着跑」。
3. 画出 greedy 行的完整数据流：主机行号表 H2D 上传 → 批式 indexed argmax → pinned 缓冲 D2H 回读 → 自旋等待 → 按行号散写回 `tokens`。
4. 解释无种子采样行为什么能合并成一次 FlashInfer 批式调用、`min_p` 行为什么在底层 `gpu_sample_batch_into` 内部被自动分区、带种子的行为什么必须逐行单调用。
5. 读懂 `benches/select_batch.rs` 里 greedy / sampling / mixed 三档基准各自度量的成本含义：greedy 是批式 argmax 的成本下限，mixed 是常见服务形态。

## 2. 前置知识

本讲建立在前两讲（u2-l1、u2-l2）的概念之上，开始前请确认你能回答这些问题：

- **logits arena 是什么**：一个 `HiddenStates` 设备缓冲，`seq_len` 是行数、`hidden_dim` 是每行宽度（= 词表宽度，可能被模型垫宽到 GEMM tile 倍数），数据是按行摊平的 bf16。解码步结束时，第 `i` 行是第 `i` 个请求「下一个 token 在整个词表上的偏好分布」（未归一化的 logit）。
- **`SamplingParams` 的字段**：`temperature` / `top_k` / `top_p` / `min_p` / `seed`（[pegainfer-frontend/src/sampler.rs:L1-L16](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-frontend/src/sampler.rs#L1-L16)）。默认参数（temperature=0.0）就是贪心。
- **`is_greedy` 与 `effectively_greedy` 两级判定**（u2-l1）：前者只认显式贪心（`temperature < 1e-5 || top_k == 1`），后者追加「核坍缩」判据——softmax 最大概率恒满足 \( p_{\max} \ge 1/V \)，所以 `top_p <= 1/V` 时核里只剩 argmax 一个 token，这类行必须走确定性的 argmax 路径。
- **`SampleScratch` 的缓冲布局**（u2-l2）：贪心链与非贪心链住在互不相交的设备缓冲里，构造时按 `max_rows × vocab` 一次分配、长期持有，解码路径因 CUDA 图烘焙地址而要求指针稳定，**每步只写不重新分配**。
- **philox 与种子纪律**（本讲只要求直觉，细节在 u2-l4）：FlashInfer 采样核用 philox 计数器 RNG；一次批式调用共用一个种子，行与行靠 philox 子序列去相关；引擎种子每个解码步推进一次。

一个简单的术语表：

| 术语 | 含义 |
|------|------|
| 行（row） | arena 里的一行 logits，对应一个并发请求在本步的候选分布 |
| 贪心行 | `effectively_greedy` 为真的行，走批式 indexed argmax |
| 采样行 | 其余行，走 FlashInfer 温度/top-k/top-p 通道 |
| seeded 行 | `params[i].seed == Some(_)` 的采样行，逐行单调用以保证可重放 |
| H2D / D2H | 主机到设备 / 设备到主机的拷贝 |
| pinned 缓冲 | 固定页主机内存，D2H 拷贝可以保持异步（#704 教训，u4-l2 详述） |

## 3. 本讲源码地图

| 文件 | 角色 |
|------|------|
| [src/lib.rs:L189-L321](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/src/lib.rs#L189-L321) | 本讲主角：`select_batch` 主体——校验、三路分发、按行号归位 |
| [src/lib.rs:L448-L461](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/src/lib.rs#L448-L461) | 路由谓词 `effectively_greedy`（u2-l1 精读过，本讲引用） |
| [src/lib.rs:L436-L446](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/src/lib.rs#L436-L446) | `mix_seed`：请求种子 × 请求内步数的 SplitMix64 混合（u2-l4 主角） |
| [tests/select_batch.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/tests/select_batch.rs) | 模型无关集成测试：用合成 arena 把每条路由行为钉死（需要 GPU） |
| [benches/select_batch.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/benches/select_batch.rs) | criterion 微基准：greedy / sampling / mixed 三档（需要 GPU） |
| [pegainfer-kernels/src/ops/sampling.rs:L94-L124](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-kernels/src/ops/sampling.rs#L94-L124) | 底层 `gpu_sample_batch_into`：本讲讲它的 min_p 分区与返回约定 |
| [pegainfer-frontend/src/sampler.rs:L31-L39](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-frontend/src/sampler.rs#L31-L39) | `is_greedy` 契约定义（承自 u2-l1） |

阅读建议：先通读 `select_batch` 的文档注释（L162-L188），再带着「三路分发」的地图读主体；遇到 `effectively_greedy` / scratch 缓冲细节时回翻前两讲。

## 4. 核心概念与源码讲解

先给一张总览图。`select_batch` 把 `params` 切成三个**互不相交**的集合，按固定顺序依次处理，最后合成一个与输入行对齐的 `Vec<u32>`：

```text
params[0..n]（每行一份采样参数）
        │
        ├─ 集合 ①：effectively_greedy(p, selection_width) == true
        │      → 收集行号 [i32]，一次批式 indexed argmax
        │        （H2D 行号表 → argmax 核 → pinned D2H → 自旋等待）
        │
        ├─ 集合 ②：!effectively_greedy(p) 且 p.seed == None
        │      → 组装 Vec<BatchSamplingRow>，一次 gpu_sample_batch_into
        │        （min_p 行在底层再被自动分区出自己的 pass）
        │
        └─ 集合 ③：!effectively_greedy(p) 且 p.seed == Some(s)
               → 逐行构造单元素 [BatchSamplingRow]，
                 以 mix_seed(s, steps[i]) 为种子单调用

三路结果全部按「原始行号 i」写回 tokens[i] → Ok(tokens)
```

模块文档把这条设计线说得很直白：这个函数是「把一个 logits 竞技场变成每行一个 token id」的唯一入口，**没有逐行逃生舱**——调用方没法退化回 `for i { sample(i) }`（[src/lib.rs:L4-L11](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/src/lib.rs#L4-L11)）。三路分发正是这个承诺的实现方式：能批的尽量批，只有「可重放语义」强迫单行的 seeded 行例外。

### 4.1 前置校验与容量不变量

#### 4.1.1 概念说明

`select_batch` 是解码热路径上的公共入口，五个模型 crate 都在用（例如 [pegainfer-gemma4/src/engine.rs:L984](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-gemma4/src/engine.rs#L984)、[pegainfer-qwen35/src/tp_executor.rs:L1570](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/tp_executor.rs#L1570)）。入口处的校验不是为了「礼貌」，而是在守护三条已经 baking 进缓冲布局的不变量：

1. **形状不变量**：scratch 的所有设备缓冲在构造时按 `max_rows × vocab` 定型（u2-l2）。batch 超容量或词表宽度不符，意味着后续每个核的形状检查都会失败——但与其让深处某个 FFI 调用报一个不知所云的错，不如在门口用带数字的错误信息拒绝。
2. **不重新分配纪律**：解码路径的指针稳定性（CUDA 图捕获）要求「每步只写不分配」。所以超容量时**绝不**临时扩缓冲，直接报错——调度器应该按 scratch 容量限制批大小，而不是让采样层悄悄扩容。
3. **参数对齐**：`steps` 与 `params` 是两条平行数组，长度不一致说明调用方组装批时出了 bug，这一步必须在任何 GPU 工作之前发现。

#### 4.1.2 核心流程

函数主体最前面的执行顺序：

```text
n = params.len()
若 n == 0 → 直接返回空 Vec（空批短路，不碰 GPU）
ensure!(steps.len() == n)        # 平行数组对齐
ensure!(n <= scratch.max_rows)   # 容量
ensure!(logits.seq_len >= n)     # arena 行数够用
vocab = logits.hidden_dim
ensure!(vocab == scratch.vocab)  # 词表宽度一致
```

注意最后一个校验的两侧：**形状校验用 `scratch.vocab`（arena 物理宽度），而稍后的路由判定用 `scratch.selection_width`（可发射宽度）**——这正是 u2-l2 讲过的「vocab 管形状、selection_width 管路由」分工在源码里的落点。

#### 4.1.3 源码精读

空批短路——零行请求不产生任何 GPU 流量，直接返回空向量：[src/lib.rs:L197-L200](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/src/lib.rs#L197-L200)。这段保证调度器在极端情况（某步没有可采样行）下可以无顾虑地调用。

第一个 `ensure!`：`steps` 必须与 `params` 逐行平行——`steps[i]` 是第 `i` 行所属请求的「请求内解码步数」，只有 seeded 行会用到它（见 4.3）：[src/lib.rs:L201-L205](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/src/lib.rs#L201-L205)

```rust
ensure!(
    steps.len() == n,
    "select_batch: {} steps for {n} rows",
    steps.len()
);
```

第二个 `ensure!`：批大小不得超过 scratch 容量，错误信息带上两侧数字，方便定位是调度器放行了过大的批：[src/lib.rs:L206-L210](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/src/lib.rs#L206-L210)。它对应的集成测试是 `batch_larger_than_scratch_is_rejected`（2 行批塞进 1 行 scratch，断言返回 `Err`）：[tests/select_batch.rs:L221-L240](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/tests/select_batch.rs#L221-L240)。

第三个 `ensure!`：arena 至少要有 `n` 行。允许 `seq_len > n`（arena 可以比本步采样的行更多），但绝不允许少于 `n`：[src/lib.rs:L211-L215](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/src/lib.rs#L211-L215)。

第四个 `ensure!`：arena 的 `hidden_dim` 必须等于 scratch 构造时的 vocab——宽度不符说明调用方换了个模型/词表却复用了旧 scratch：[src/lib.rs:L217-L222](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/src/lib.rs#L217-L222)。

四条之后，路由谓词以闭包形式固定下来，供三处复用；旁边那行注释提醒：模型垫宽出来的列不是可发射 token，不得参与 `top_p <= 1/vocab` 判定的分母：[src/lib.rs:L223-L226](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/src/lib.rs#L223-L226)

```rust
let is_argmax = |p: &&SamplingParams| effectively_greedy(p, scratch.selection_width);
let mut tokens = vec![0u32; n];
```

`tokens` 先全零初始化；接下来三段路径各自认领自己那部分下标，互不重叠（见 4.4）。

#### 4.1.4 代码实践

1. **实践目标**：把四条校验与「哪条测试钉住它」对上号，并能预测每条校验失败时的错误文本。
2. **操作步骤**：
   - 通读 [src/lib.rs:L197-L222](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/src/lib.rs#L197-L222) 的四条 `ensure!`；
   - 打开 [tests/select_batch.rs:L221-L240](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/tests/select_batch.rs#L221-L240)，确认它踩中的是第二条（容量）；
   - 在纸上写出另外三条校验各自的最小触发方式（例如：传 `&[0]` 给 2 行 params 触发第一条；用 `SampleScratch::new(ctx, 8, 1)` 配 16 宽 arena 触发第四条）。
3. **需要观察的现象**：错误信息里带不带两侧数字（如 `2 rows exceeds scratch capacity 1`）。
4. **预期结果**：四条校验都能在 GPU 工作开始之前拒绝调用；错误信息足以判断是调度器还是 scratch 构造方的责任。
5. 若想实际运行触发（需 GPU 构造 `DeviceContext`）：**待本地验证**——本机无 GPU 时，纸面推演即完成本实践。

#### 4.1.5 小练习与答案

**练习 1**：为什么超容量时不做成「自动扩容 scratch」，而是报错？
**答案**：解码路径的设备缓冲地址被 CUDA 图烘焙，要求指针稳定（u2-l2）；扩容意味着换新缓冲、换新地址，等于每步重分配，破坏图捕获。正确的分层是调度器按 `scratch.max_rows()` 限制批大小，采样层只负责拒绝违约调用。

**练习 2**：`ensure!(logits.seq_len >= n)` 允许 `seq_len > n`。这对调用方有什么用？
**答案**：arena 可以一次准备多于本步要采样的行（例如统一形状的执行缓冲），`select_batch` 只按行号 `0..n-1` 采样前 `n` 行；只要行数不少于 `n` 就不越界。

**练习 3**：如果把 `is_argmax` 闭包里的 `scratch.selection_width` 改成 `scratch.vocab`，哪类请求的行为会变？
**答案**：使用了 `with_selection_width` 垫宽的场景（如 qwen35 的 tile 对齐，#1046）：`top_p` 落在 \( 1/V_{\text{emittable}} < \text{top\_p} \le 1/V_{\text{arena}} \) 的行会从 argmax 路径错误地掉进采样器，在 bf16 并列最大值上随机化。回归测试 [tests/select_batch.rs:L180-L219](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/tests/select_batch.rs#L180-L219) 的对照组专门钉住了这一点。

### 4.2 greedy 行批式 indexed argmax

#### 4.2.1 概念说明

第一路处理「确定性行」：不管贪心是显式的（`temperature=0`）还是语义等价的（`top_p <= 1/selection_width` 核坍缩），这些行的正确答案都只有一个——分布的最大值下标。把它们**全部合并成一次批式 indexed argmax**：

- **indexed** 的含义：argmax 核不扫整张 arena 的所有行，而是按一张行号表只取贪心行。行号表是紧凑的（长度 = 贪心行数），所以一次调用无论批内 1 行还是 64 行贪心，都只有一次 H2D、一次核发射、一次 D2H。
- **确定性**：argmax 核对 bf16 并列最大值按**最低下标**破并列。这与拒绝采样器「并列时靠种子随机挑一个」形成对照——正是 u2-l1 讲过的必须改道 argmax 的理由。
- **为什么不省掉 D2H**：调用方（引擎）需要把 token id 发回请求方，主机必须拿到这个 `u32`。所以这一路注定有一次设备→主机回读，也注定要等它（否则 `tokens` 里是垃圾）。回读的内存与同步纪律（pinned 页、`stream_spin_wait`）在 u4-l2 有专题，本讲只需要知道「等的是这次拷贝自己的事件」。

#### 4.2.2 核心流程

```text
greedy = [i32; 满足 is_argmax 的行号，升序]          # 主机侧紧凑列表
若 greedy 非空：
    memcpy_htod(greedy → scratch.row_indices)         # ① H2D：上传行号表
    argmax_batch_bf16_split_indexed_into(             # ② 核：按行号表做两阶段 argmax
        logits, row_indices, greedy.len(),
        partial_values, partial_indices,              #    split 归约的中间产物
        top1_values,                                  #    每 argmax 行的 top-1 logit（本函数不读）
        argmax_out)                                   #    每 argmax 行一个 token id
    memcpy_dtoh(argmax_out → scratch.argmax_host)     # ③ D2H：pinned 落地
    stream_spin_wait(ctx)                             # ④ 只等这次拷贝的事件
    for (k, row) in greedy:                           # ⑤ 按行号散写
        tokens[row] = argmax_host[k] as u32
```

「两阶段 split argmax」是宽词表（15 万列）下的经典手法：先把每行切成 4096 宽的条带各算局部最大（partials 缓冲，长度公式见 u2-l2），再归约出全局 argmax。本讲把它当黑盒，只关心它的输入输出约定。

#### 4.2.3 源码精读

收集贪心行号——`filter_map` + `then_some` 把谓词为真的下标收成 `Vec<i32>`（i32 是行号表的设备元素类型）：[src/lib.rs:L228-L233](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/src/lib.rs#L228-L233)

```rust
let greedy: Vec<i32> = params
    .iter()
    .enumerate()
    .filter_map(|(i, p)| is_argmax(p).then_some(i as i32))
    .collect();
```

H2D 上传：把紧凑行号表写进构造时分配好的 `scratch.row_indices`（容量 `max_rows`，足够容纳任何合法批的贪心子集）：[src/lib.rs:L234-L237](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/src/lib.rs#L234-L237)。

一次批式 indexed argmax 发射，输出与中间产物全部落在 scratch 的贪心链缓冲上（u2-l2 逐个讲过：partials 是 split 归约中间产物、`top1_values` 是核必需的 out-param 但本函数不读、`argmax_out` 每 argmax 行一个 token）：[src/lib.rs:L238-L247](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/src/lib.rs#L238-L247)

```rust
argmax_batch_bf16_split_indexed_into(
    ctx, logits, &scratch.row_indices, greedy.len(),
    &mut scratch.argmax_partial_values, &mut scratch.argmax_partial_indices,
    &mut scratch.top1_values, &mut scratch.argmax_out,
)?;
```

D2H 回读到 pinned 主机缓冲，随后自旋等待。紧挨着的注释解释了为什么「只等这次拷贝的事件」就够：拷贝与 argmax 核在同一条流上按序排队，等到拷贝事件就传递覆盖了更早排队的核——效果等价于旧的整流同步，但把调度器唤醒从步骤循环里拿掉了：[src/lib.rs:L248-L259](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/src/lib.rs#L248-L259)

```rust
ctx.stream
    .memcpy_dtoh(&scratch.argmax_out, &mut scratch.argmax_host)?;
// Blocks on this copy's own event — which transitively covers the
// argmax kernel queued before it on the same stream...
stream_spin_wait(ctx)?;
let out = scratch.argmax_host.as_slice()?;
```

最后按行号散写——`argmax_out` 的第 `k` 个元素对应行号表第 `k` 项（即 `greedy[k]`），不是 arena 的第 `k` 行：[src/lib.rs:L260-L262](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/src/lib.rs#L260-L262)

```rust
for (k, &row) in greedy.iter().enumerate() {
    tokens[row as usize] = out[k] as u32;
}
```

对应的集成测试 `greedy_batch_picks_each_rows_argmax`：4 行全贪心、峰在各行不同位置（3/7/0/15），断言 tokens 与峰位逐一相等——同时验证了 argmax 的正确性与按行归位：[tests/select_batch.rs:L57-L78](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/tests/select_batch.rs#L57-L78)。另一个测试 `top_k_one_routes_through_greedy_path` 钉住「`top_k == 1` 也是贪心」的契约级行为：[tests/select_batch.rs:L80-L92](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/tests/select_batch.rs#L80-L92)。

#### 4.2.4 代码实践

1. **实践目标**：纸面完整走一遍 4 行贪心批的数据流，明确每段缓冲的长度与内容。
2. **操作步骤**：对照 [tests/select_batch.rs:L57-L78](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/tests/select_batch.rs#L57-L78)（`vocab=16`，4 行 `peak_row`，峰在 `[3, 7, 0, 15]`），写出：
   - `greedy` 向量的内容与长度；
   - H2D 拷贝的字节数；
   - `argmax_out` 与 `argmax_host` 的内容（`argmax_out[k]` 对应哪个峰）；
   - 散写循环执行 4 次后 `tokens` 的内容。
3. **需要观察的现象**：`argmax_out` 的顺序由**行号表顺序**决定（`[0,1,2,3]` → 峰 `[3,7,0,15]`），而不是由 token 值排序决定。
4. **预期结果**：`greedy == [0,1,2,3]`；H2D 为 4 个 i32；`argmax_host == [3,7,0,15]`；`tokens == vec![3,7,0,15]`，与测试断言一致。
5. 有 GPU 时可运行 `cargo test --release -p pegainfer-sample --test select_batch greedy_batch -- --nocapture` 实测；无 GPU 则本实践以纸面推演完成，**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：`top1_values` 是这个核的输出，为什么 `select_batch` 不读它？
**答案**：它是 argmax 核接口要求的 out-param（每行 top-1 logit 值）。`select_batch` 只要 token id；top-1 值的消费者在别处（比如需要把 logit 值继续参与归约的调用方），这里只是把缓冲借给核写。

**练习 2**：如果贪心行只有 1 行，这套 H2D→核→D2H→等待 的流程是否浪费？为什么仍然值得？
**答案**：单看一次调用确实有固定开销，但这条路径的意义在于**批内多行合并**与**统一入口**：混批时一次 indexed argmax 同时服务所有贪心行；且指针全部落在常驻 scratch 上，解码路径可被 CUDA 图捕获（u4-l1 的分阶段路径正是它的图友好变体）。

**练习 3**：`tokens[row as usize] = out[k] as u32` 里连续两次类型转换，各自在转换什么？
**答案**：`row as usize` 把设备行号表的 i32 下标转回 Rust 索引类型；`out[k] as u32` 把核输出的 i32 token id 转成对外的 `u32`（词表 id 用无符号表示，i32 是设备缓冲的传输格式）。

### 4.3 非 greedy 行：批式 FlashInfer 采样与 seeded 单行

#### 4.3.1 概念说明

第二、三路处理真正需要「抽签」的行，按**有没有请求级种子**再切一刀：

- **无种子行（第二路）**：合并成一次 `gpu_sample_batch_into` 批式调用，共用引擎本步的种子。行与行的随机流靠 philox **子序列**去相关——批内第 `i` 行用的是这个种子下的第 `i` 条子序列。这是服务常态：绝大多数请求不带种子，追求的是批式吞吐。
- **带种子行（第三路）**：逐行构造**单元素**批，以 `mix_seed(request_seed, steps[i])` 为种子单独调用。原因在函数文档里写得很清楚：FlashInfer 的核把**批内位置**折进 philox 子序列——同一行在批首、批尾会拿到不同的随机流。要让「token 是 (seed, step, 分布) 的纯函数」这个可重放契约成立（与批组合、邻居、引擎种子都无关），唯一办法是让批内位置恒为 0，即单行调用（`blockIdx` 恒 0，[src/lib.rs:L293-L295](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/src/lib.rs#L293-L295) 的注释）。
- **min_p 的自动分区**：`BatchSamplingRow` 携带 `min_p` 字段，但调用方**不需要**按 min_p 预先分组——`gpu_sample_batch_into` 内部会把 `min_p > 0` 的行切出来单独跑一遍。为什么在底层做而不在上层做？底层文档给了两个理由：min_p 核的 u 缩放（\( u \cdot q \)）与幸存者谓词和融合快路径不同，min_p=0 的行若搭错车会抽出不同的 token；在底层统一分区，能把「min_p=0 的行永远走原始快路径」变成对所有调用方都成立的承诺（[pegainfer-kernels/src/ops/sampling.rs:L87-L93](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-kernels/src/ops/sampling.rs#L87-L93)）。

`BatchSamplingRow` 的契约值得读一遍：贪心行（`temperature <= 0` 或 `top_k == 1`）明确**不属于**这个类型的目标用户，`top_k <= 0` 表示禁用，`min_p = 0.0` 表示禁用：[pegainfer-kernels/src/ops/sampling.rs:L14-L29](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-kernels/src/ops/sampling.rs#L14-L29)。`select_batch` 的 argmax 过滤恰好保证了送进来的行都满足这份契约。

#### 4.3.2 核心流程

第二路（无种子批式）：

```text
sampling_rows = [BatchSamplingRow { row, temperature, top_k, top_p, min_p }
                 for (i, p) in params if !is_argmax(p) && p.seed.is_none()]
若非空：
    sampled = gpu_sample_batch_into(ctx, logits.as_ref(), sampling_rows,
                                    seed,            # 引擎本步种子
                                    &mut scratch.sampling)
    for (r, token) in sampling_rows.zip(sampled):
        tokens[r.row] = token
```

`gpu_sample_batch_into` 内部（对上层透明）：

```text
若全部行同属一类（全 min_p 或全非 min_p）→ 单趟直通
否则：
    (minp, plain) = rows.partition(r.min_p > 0)
    plain_tokens = 采样(plain, seed)                       # 融合快路径
    minp_seed   = seed ^ 0x9E37_79B9_7F4A_7C15              # 第二把 philox 钥匙
    minp_tokens = 采样(minp, minp_seed)                     # min_p 专用管线
    按原 rows 顺序交错归并两段结果
```

第二把钥匙不是装饰：两趟都从子序列 0 起步，沿用同一个 `seed` 会让 min_p 行 `i` 与 plain 行 `i` 拿到**同一条**均匀随机流，token 之间产生相关性（[pegainfer-kernels/src/ops/sampling.rs:L107-L111](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-kernels/src/ops/sampling.rs#L107-L111) 的注释）。

第三路（seeded 单行）：

```text
for (i, p) in params:
    if p.seed 是 None: continue            # 无种子行已在第二路处理
    if is_argmax(p):      continue         # seeded 但贪心 → 已走第一路，确定性本就成立
    sampled = gpu_sample_batch_into(ctx, logits.as_ref(),
                                    [BatchSamplingRow { row: i, ... }],
                                    mix_seed(p.seed, steps[i]),   # 请求种子 × 请求内步数
                                    &mut scratch.sampling)
    tokens[i] = sampled[0]
```

#### 4.3.3 源码精读

第二路的过滤器是本讲最关键的一行谓词——「非贪心 **且** 无种子」：[src/lib.rs:L265-L279](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/src/lib.rs#L265-L279)

```rust
let sampling_rows: Vec<BatchSamplingRow> = params
    .iter()
    .enumerate()
    .filter(|(_, p)| !is_argmax(p) && p.seed.is_none())
    .map(|(i, p)| BatchSamplingRow {
        row: i,
        temperature: p.temperature,
        top_k: p.top_k,
        top_p: p.top_p,
        min_p: p.min_p,
    })
    .collect();
```

注意映射时的取舍：`seed` **没有**进入 `BatchSamplingRow`——这个结构只描述分布形状（温度/截断/阈值），随机种子是**每次调用**的参数而非每行的属性。这正是「批式调用共用一个引擎种子、seeded 行单独传混合种子」的接口表达。

一次批式调用加按行归位（`r.row` 从行描述里取回原始下标）：[src/lib.rs:L280-L291](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/src/lib.rs#L280-L291)

```rust
let sampled = gpu_sample_batch_into(
    ctx, logits.as_ref(), &sampling_rows, seed, &mut scratch.sampling,
)?;
for (r, token) in sampling_rows.iter().zip(&sampled) {
    tokens[r.row] = *token;
}
```

底层分区逻辑（本 crate 之外，但决定了上层「min_p 行可与普通行混批」的自由）：同质批直通单趟，混合批 partition 后两趟、两把钥匙、按原序归并：[pegainfer-kernels/src/ops/sampling.rs:L101-L124](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-kernels/src/ops/sampling.rs#L101-L124)

```rust
if rows.iter().all(|r| r.min_p > 0.0) || rows.iter().all(|r| r.min_p <= 0.0) {
    return sample_uniform_batch_into(ctx, logits, rows, seed, scratch);
}
let (minp, plain): (Vec<_>, Vec<_>) = rows.iter().copied().partition(|r| r.min_p > 0.0);
let plain_tokens = sample_uniform_batch_into(ctx, logits, &plain, seed, scratch)?;
let minp_seed = seed ^ 0x9E37_79B9_7F4A_7C15;
let minp_tokens = sample_uniform_batch_into(ctx, logits, &minp, minp_seed, scratch)?;
```

第三路的循环体。先跳过无种子行，再跳过贪心行——**seeded 且贪心**的行早在第一路就拿到了确定答案，种子对它毫无意义，这里不能再进采样器：[src/lib.rs:L293-L309](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/src/lib.rs#L293-L309)

```rust
for (i, p) in params.iter().enumerate() {
    let Some(request_seed) = p.seed else { continue; };
    if is_argmax(p) { continue; }
    let row = [BatchSamplingRow { row: i, temperature: p.temperature,
                                  top_k: p.top_k, top_p: p.top_p, min_p: p.min_p }];
```

单行调用，种子用 `mix_seed(request_seed, steps[i])` 混合（SplitMix64 的混合数学在 u2-l4 精读），结果直接按下标落位：[src/lib.rs:L310-L318](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/src/lib.rs#L310-L318)

```rust
    let sampled = gpu_sample_batch_into(
        ctx, logits.as_ref(), &row,
        mix_seed(request_seed, steps[i]), &mut scratch.sampling,
    )?;
    tokens[i] = sampled[0];
}
```

三条测试分别钉住这一大块的三个侧面：

- `min_p_row_takes_the_sampler_path_and_filters`：min_p=0.5 把 0.27 概率的 token 挡在门外（64 个种子全部返回 token 3），min_p=0.0 时同一行跨 128 个种子能探索到两个 token——证明该行真的在采样器里而不是 argmax：[tests/select_batch.rs:L242-L275](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/tests/select_batch.rs#L242-L275)。
- `mixed_batch_keeps_plain_rows_on_the_fast_path`：min_p 邻居加入批后，普通行的 token 必须与「无 min_p 批」逐一相同（64 个种子）——把底层分区的承诺升级成了 `select_batch` 层的回归：[tests/select_batch.rs:L277-L323](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/tests/select_batch.rs#L277-L323)。
- `seeded_rows_replay_independent_of_batch_position`：seeded 行在批尾（邻居不同、引擎种子 99）与在批首（引擎种子 1234）抽出同一 token；改步数或改种子则必须换 token——可重放契约的两面：[tests/select_batch.rs:L325-L371](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/tests/select_batch.rs#L325-L371)。

#### 4.3.4 代码实践

1. **实践目标**：解释「min_p 邻居不影响普通行」如何从底层分区的实现细节变成上层可断言的性质。
2. **操作步骤**：
   - 通读 [pegainfer-kernels/src/ops/sampling.rs:L87-L93](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-kernels/src/ops/sampling.rs#L87-L93)（分区动机）与 [tests/select_batch.rs:L277-L323](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/tests/select_batch.rs#L277-L323)（上层断言）；
   - 回答：混合批里 plain 行的 philox 子序列编号由什么决定？为什么分区后它与「min_p-free 批」完全一致？
   - 再回答：测试注释为什么特意选 `top_k=4, top_p=0.9` 的平缓分布，而不是单峰分布？
3. **需要观察的现象**：断言是精确相等（`assert_eq!`），不是统计近似。
4. **预期结果**：plain 行的子序列编号 = 它在**plain 子批**中的下标；分区把 min_p 行从 plain 批里剔除后，plain 批与「本来就只有这两行」的批**逐位相同**（同种子、同分布、同批内位置），token 必然一致。单峰分布下两条路径都会坍缩到峰上，测不出分歧；平缓分布 + top_k/top_p 让融合快路径（多次均匀抽取的拒绝采样）与 min_p 管线（重归一 + 单次抽取）真的会给出不同 token。
5. 运行该测试需 GPU：`cargo test --release -p pegainfer-sample --test select_batch mixed_batch_keeps_plain -- --nocapture`，**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：第二路的谓词是 `!is_argmax(p) && p.seed.is_none()`。第三路循环里为什么还要再判一次 `is_argmax(p)`？
**答案**：第三路遍历的是**全部** params（不是第二路过滤后的残集），所以要先用 `seed.is_none()` 跳过无种子行；而一个行可能**既有种子又贪心**（例如请求给了 seed 但 temperature=0）。这类行在第一路已按 argmax 得到确定答案，种子对确定性路径没有意义，第三路必须跳过它，否则同一行会被写两次且语义冲突。

**练习 2**：seeded 行的单行调用也用 `scratch.sampling`，会不会和第二路批式调用的缓冲冲突？
**答案**：不会。三段路径在同一线程上**顺序执行**，第二路调用返回时它对 scratch 的读写已经结束；seeded 单行调用复用同一批缓冲正是「一次分配、每步只写」纪律的收益（u2-l2）。冲突的前提是并发进入，而 `select_batch` 是同步函数。

**练习 3**：为什么第二路把 `seed` 作为整批一个参数，而 `BatchSamplingRow` 里没有 per-row 种子字段？
**答案**：接口形状反映随机模型：无种子行的随机性来源是「引擎每步种子 + 批内位置（philox 子序列）」，行自身不携带随机性参数；per-row 种子只有 seeded 行需要，而它们走的是单行调用通道，种子作为调用参数传入即可，不必污染批式结构。

### 4.4 结果按行号归位：三路合流与基准语义

#### 4.4.1 概念说明

三路各自产生「自己集合内有序」的结果：argmax 输出按 `greedy` 行号表排序、批式采样输出按 `sampling_rows` 顺序、seeded 每次只有一个元素。而函数的返回契约是**按原始行序的 `Vec<u32>`**（文档 L162-L169：`params[i]` 治理第 `i` 行，返回第 `i` 个 token）。归位（scatter）就是把三种局部顺序映射回全局行号。

归位的正确性依赖一个**划分不变量**：三个集合由两个谓词层层切开——

\[ \underbrace{\{i : \text{is\_argmax}(p_i)\}}_{\text{第一路}} \;\Big|\; \underbrace{\{i : \neg\text{is\_argmax}(p_i) \wedge \text{seed}_i = \bot\}}_{\text{第二路}} \;\Big|\; \underbrace{\{i : \neg\text{is\_argmax}(p_i) \wedge \text{seed}_i = \top\}}_{\text{第三路}} \]

三者两两不相交、并集为全集，所以 `tokens` 的每个下标**恰好被写一次**，初始化的 `0u32` 只是占位，永远不会残留到返回值里。

基准的语义也挂在这套分发上。`benches/select_batch.rs` 固定 Qwen 词表 151 936，扫 batch ∈ {1, 8, 32, 64}，每档跑三种参数混合（[benches/select_batch.rs:L38-L74](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/benches/select_batch.rs#L38-L74)）：

| 档 | 参数 | 全部行落入 | 度量的是什么 |
|----|------|-----------|--------------|
| greedy | `p(0.0, -1, 1.0)` | 第一路 | 批式 indexed argmax 的成本下限（H2D + 核 + pinned D2H + 自旋） |
| sampling | `p(1.0, 50, 0.9)` | 第二路 | FlashInfer 批式采样通道的全额成本（gather + softmax + 采样 + 同步） |
| mixed | 偶数行贪心、奇数行采样 | 第一路 + 第二路 | 常见服务形态：一次调用顺序跑完两条路径 |

基准文件头一句话就是本讲分发模型的浓缩：「Greedy is the batched argmax floor; sampling is the FlashInfer pass; mixed is the common serving case」（[benches/select_batch.rs:L1-L3](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/benches/select_batch.rs#L1-L3)）。mixed 之所以是常态：线上请求天然混着「要确定性输出的」和「要多样性的」，而一份 scratch 的贪心/采样缓冲互不相交、顺序复用，使混批不需要任何额外分配。

另外注意基准循环里的一个细节：每次迭代 `seed = seed.wrapping_add(1)`（[benches/select_batch.rs:L63-L70](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/benches/select_batch.rs#L63-L70)）——忠实模拟「引擎种子每步推进」的调用纪律，而不是反复用同一个种子。

#### 4.4.2 核心流程

一次完整 `select_batch` 调用的时序（单线程、单 CUDA 流，顺序排队）：

```text
校验（4.1）
│
├─[第一路] H2D 行号表 → argmax 核 → D2H(pinned) → 自旋等待 → 散写 tokens
├─[第二路] 组装 BatchSamplingRow[] → gpu_sample_batch_into(seed)
│          （内部：plain 趟 [→ min_p 趟] → 归并）→ zip 散写 tokens
├─[第三路] 逐 seeded 行：单行 gpu_sample_batch_into(mix_seed(s, step))
│          → tokens[i] = sampled[0]
│
└─ Ok(tokens)   # 长度 n，第 i 个元素即第 i 行的下一个 token
```

三段的执行顺序固定（贪心 → 批式 → seeded），彼此通过 scratch 复用与流上排队自然串行，无需额外同步（第三路内部每次调用自己会同步回读 token）。

#### 4.4.3 源码精读

`mixed_batch_routes_and_places_each_row` 是三路合流的教科书案例——3 行批：第 0、2 行贪心，第 1 行 `top_p=0.5` 采样（`1/16 = 0.0625 < 0.5`，确实在采样器一侧）；分布近单峰（峰概率≈1）使采样核必然抽中峰：[tests/select_batch.rs:L94-L115](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/tests/select_batch.rs#L94-L115)

```rust
let params = vec![greedy(), sampling(1.0, -1, 0.5), greedy()];
...
assert_eq!(tokens, vec![2, 9, 4]);
```

逐行拆开（本讲代码实践的素材，答案见 4.4.4）：

- 第一路：`greedy == [0, 2]` → argmax 输出 `[2, 4]` → `tokens[0]=2, tokens[2]=4`；
- 第二路：`sampling_rows == [{row:1, t=1.0, top_k=-1, top_p=0.5, min_p=0}]` → 单趟批采样 → `tokens[1]=9`；
- 第三路：批内无 seeded 行，循环空转。

三段归位写法对照（散写、zip 散写、直写下标）——同一不变量的三种表达：

| 路 | 归位语句 | 顺序来源 |
|----|----------|----------|
| 第一路 | `tokens[row as usize] = out[k] as u32`（[src/lib.rs:L260-L262](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/src/lib.rs#L260-L262)） | `greedy` 行号表第 `k` 项 |
| 第二路 | `tokens[r.row] = *token`（[src/lib.rs:L288-L290](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/src/lib.rs#L288-L290)） | `BatchSamplingRow.row` 字段 |
| 第三路 | `tokens[i] = sampled[0]`（[src/lib.rs:L317](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/src/lib.rs#L317)） | 循环变量即行号 |

基准的三档混合定义：[benches/select_batch.rs:L45-L60](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/benches/select_batch.rs#L45-L60)，偶数下标贪心、奇数下标采样，正好让两种路径在同一个批里都承担一半负载。

`sampling_is_seed_deterministic_and_actually_samples` 从另一端钉住整条链路：同种子重放一致、64 个种子在均匀分布上探索出多个 token——分发与归位都没有把行弄丢或弄串：[tests/select_batch.rs:L117-L140](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/tests/select_batch.rs#L117-L140)。

#### 4.4.4 代码实践

本实践是本讲的核心任务，全部可在纸面完成，无需 GPU。

1. **实践目标**：手工跟踪 `mixed_batch_routes_and_places_each_row` 中三行各自的路由与归位；再自行设计一个更复杂的 5 行批并用源码谓词核对。
2. **操作步骤（第一部分）**：对照 [tests/select_batch.rs:L94-L115](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/tests/select_batch.rs#L94-L115) 与 [src/lib.rs:L189-L321](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/src/lib.rs#L189-L321)，对每一行写下：落入哪个集合、哪次 GPU 调用产生它的 token、写回 `tokens` 的哪个下标。参考答案：

   | 行 | 参数 | 集合 | token 来源 | 写回 |
   |----|------|------|-----------|------|
   | 0 | `greedy()`（默认） | 第一路，`greedy=[0,2]` 的第 0 项 | indexed argmax 输出 `out[0]=2` | `tokens[0]=2` |
   | 1 | `sampling(1.0,-1,0.5)`：`top_p=0.5 > 1/16`，非贪心、无种子 | 第二路，`sampling_rows` 第 0 项 | `gpu_sample_batch_into` 返回 `sampled[0]=9`（近单峰，核必抽峰） | `tokens[1]=9` |
   | 2 | `greedy()` | 第一路，`greedy=[0,2]` 的第 1 项 | indexed argmax 输出 `out[1]=4` | `tokens[2]=4` |

   引擎种子 `7` 与 `steps=[0,0,0]` 在这个用例里不影响结果（无 seeded 行；贪心不消耗种子）。
3. **操作步骤（第二部分，见第 5 节综合实践）**：设计 5 行批并用 `filter` 闭包核对。
4. **需要观察的现象**：三行写回互不冲突；返回值顺序由**行号**决定，与三段的执行顺序（第一路先于第二路）无关。
5. **预期结果**：`tokens == vec![2, 9, 4]`，与测试断言一致；每个下标恰好被写一次。

#### 4.4.5 小练习与答案

**练习 1**：如果第二路的归位误写成 `tokens[k] = *token`（用循环序号而不是 `r.row`），`mixed_batch_routes_and_places_each_row` 还能通过吗？
**答案**：不能。第二路只有第 1 行，`k=0`，会把 token 9 写进 `tokens[0]`（覆盖贪心的 2），而 `tokens[1]` 保持初始 0，断言 `vec![2,9,4]` 两个位置都会错。这正是该测试存在的意义：它对「归位下标」敏感。

**练习 2**：mixed 基准档的成本近似等于 greedy 档加 sampling 档之和，还是两者取最大？为什么？
**答案**：近似**之和**。第一路与第二路在同一条流上顺序执行（先 argmax 含同步等待，再批式采样含自己的同步），没有可重叠的窗口——这是「单入口顺序三段」设计的明确代价换收益：接口最简单、scratch 复用最彻底。若要重叠就需要 u4-l1 那样的分阶段/异步改造。

**练习 3**：为什么 `tokens` 初始化用 `0u32` 而不是 `u32::MAX` 之类的哨兵值？初始化值会不会泄漏到返回值？
**答案**：划分不变量保证每个下标恰好被写一次，初始化值只是分配内存的占位，永远不泄漏——所以任何值都行，`0` 最便宜。反过来说，如果哪天有人改坏了谓词（比如第三路忘了跳过贪心行），哨兵值能让测试更快暴露「有的行没被写」，这是潜在的改进点，但当前实现依赖不变量而非哨兵。

## 5. 综合实践

**任务：设计一个 5 行混合批，逐行预测路由，并用源码谓词核对；有 GPU 时升级成集成测试。**

批的设定期望（也是对答案的预埋）：

| 行 | 参数（示例） | 预测集合 |
|----|--------------|----------|
| 0 | `greedy()` | 第一路（`greedy` 第 0 项） |
| 1 | `sampling(1.0,-1,1.0)` 且 `min_p = 0.5` | 第二路（`sampling_rows` 内，底层再分进 min_p 趟） |
| 2 | `temperature=1.0, seed=Some(7)`，其余默认 | 第三路（seeded 单行循环，仅此一次迭代） |
| 3 | `greedy()` | 第一路（`greedy` 第 1 项） |
| 4 | `sampling(1.0, 50, 0.9)`，无种子 | 第二路（`sampling_rows` 内，底层走 plain 趟） |

**步骤**：

1. **纸面核对谓词**：逐行打开 [src/lib.rs:L229-L233](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/src/lib.rs#L229-L233)（第一路 `filter_map`）、[src/lib.rs:L268-L279](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/src/lib.rs#L268-L279)（第二路 `filter`）、[src/lib.rs:L296-L302](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/src/lib.rs#L296-L302)（第三路两个 `continue`），确认上表预测。应当得到：`greedy == [0, 3]`；`sampling_rows == [row 1, row 4]`（一次批式调用，内部 partition 成 plain `[4]` 与 minp `[1]` 两趟）；seeded 循环只对行 2 迭代一次。
2. **写出归位**：第一路 `out[0] → tokens[0]`、`out[1] → tokens[3]`；第二路 `sampled[0] → tokens[1]`、`sampled[1] → tokens[4]`；第三路 `tokens[2] = sampled[0]`。检查：5 个下标各写一次。
3. **（可选，需 GPU）写成集成测试**：仿照 [tests/select_batch.rs:L20-L31](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/tests/select_batch.rs#L20-L31) 的 `make_arena` 与 [tests/select_batch.rs:L325-L371](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/tests/select_batch.rs#L325-L371) 的 seeded 断言结构，写一个新测试（**示例代码**，仅示骨架，不是仓库已有代码）：

   ```rust
   #[test]
   fn five_row_mix_routes_each_row_once() {
       // arena：5 行，vocab=16，行 0/3 用 peak_row 使贪心结果可预测；
       // 行 2 用 flat 分布使 seeded 行为纯随机流。
       // 断言：
       //  1) tokens[0]、tokens[3] 等于各行峰位；
       //  2) 行 2 换引擎种子重跑结果不变（单行 mix_seed 与引擎种子无关），
       //     改 steps[2] 或 seed 则变化；
       //  3) 行 4 的 token 与「只提交行 4」的单独调用一致
       //     （plain 趟内位置恒为 0）。
   }
   ```

4. **预期结果**：纸面部分应当完全确定；GPU 部分若本机无 GPU，先写出完整测试与预期断言并标注**待本地验证**，有 GPU 时用 `cargo test --release -p pegainfer-sample --test select_batch five_row -- --nocapture` 跑通。
5. **反思题**：行 2 的 token 为什么与引擎种子（第 5 个参数）无关，而行 4 的 token 与它强相关？

## 6. 本讲小结

- `select_batch` 是唯一入口、没有逐行逃生舱：它把 `params` 按「`effectively_greedy` × 有无种子」切成三个互不相交的集合，顺序走三段路径，结果按原始行号合成一个 `Vec<u32>`。
- 顶部四个 `ensure!` 分别守护步数对齐、scratch 容量、arena 行数、vocab 宽度；超容量宁可报错也不扩容，因为解码缓冲的指针稳定性（CUDA 图）高于便利性。
- 贪心行合批走 indexed argmax：紧凑行号表 H2D → 一次核发射 → pinned D2H → 自旋只等该拷贝的事件（传递覆盖同流上更早的 argmax 核）→ 按行号散写。
- 无种子采样行合批走一次 `gpu_sample_batch_into`，共用引擎步种子；`min_p` 行在底层自动分区出自己的 pass（第二把 philox 钥匙防止两趟相关），使「min_p=0 行永远走融合快路径」对一切调用方成立。
- seeded 行逐行单调用、种子为 `mix_seed(request_seed, step)`：FlashInfer 把批内位置折进 philox 子序列，只有单行调用（批内位置恒 0）能让 token 成为 (seed, step, 分布) 的纯函数。
- 基准三档对应三种成本语义：greedy = 批式 argmax 下限，sampling = FlashInfer 通道全额，mixed = 常见服务形态（两路顺序执行，成本近似相加）。

## 7. 下一步学习建议

- **下一讲（u2-l4）**：`mix_seed 与确定性`——精读 SplitMix64 混合的数学、philox 子序列与批组合的相互作用、引擎种子按步推进与请求种子按 (seed, step) 混合的分工。本讲第三路留下的「为什么单行就够了」将在那里得到完整证明。
- **承前补强**：如果 4.1 的校验与 4.2 的缓冲名称让你卡住，回翻 u2-l2（SampleScratch 布局）与 u2-l1（`effectively_greedy` 数学）。
- **源码延伸阅读**：[pegainfer-kernels/src/ops/sampling.rs:L126-L150](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-kernels/src/ops/sampling.rs#L126-L150) 的 `sample_uniform_batch_into`——看批式调用的主机侧参数组装与容量校验如何与本讲的第二路对接。
- **后续预告（u4 单元）**：贪心路径的 CUDA 图友好变体（`greedy_argmax_ids` / `greedy_stage_readback` / `greedy_collect_resident` 四件套）、pinned 内存与流同步的 #704 教训——它们都是本讲第一路在「图捕获」与「回读纪律」两个维度上的深化。
