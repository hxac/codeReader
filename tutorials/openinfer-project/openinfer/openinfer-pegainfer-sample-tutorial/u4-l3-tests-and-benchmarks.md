# u4-l3 模型无关的测试策略与性能基准

## 1. 本讲目标

学完本讲，你应该能够：

1. 掌握**合成 logits arena 测试法**——不加载任何模型权重，用 `make_arena`、`peak_row`、`noise_row` 三个小工具把 `select_batch` 的每条策略行为（路由、归位、种子重放、min_p 隔离、容量拒绝）钉成一个 GPU 集成测试。
2. 理解 **GPU-主机 A/B 双实现互证**——`token_logprobs_batch`（设备核）与 `token_logprob_from_row`（主机参考）互为正确性门：id 序列要求逐位相等，数值只要求落在容差内。
3. 读懂**回归测试即文档**的写法——三个历史 bug（per-step philox 迁移、#1046 tile 对齐、min_p 邻居扰动）如何被写成「注释讲历史 + 断言冻结行为」的回归测试。
4. 会跑并会扩展 **criterion 微基准**（greedy / sampling / mixed 三档）与 `--ignored` 延迟探针，理解它们各自回答什么问题。

本讲是单元四的收束篇：前面七讲拆开了每个机制，本讲回答「这些机制凭什么可信、有多快」。

## 2. 前置知识

- **模型无关测试（model-free testing）**：传统集成测试要加载几十 GB 的模型权重才能拿到真实 logits；而这个 crate 的被测对象只是「logits 进、token id 出」的纯策略层，所以测试可以**自己合成 logits**——一个小函数就能生成任意词表宽、任意行数的 arena，秒级启动、确定性可复现。
- **双实现互证（A/B differential testing）**：同一个数学问题（log-softmax + top-k）写了两遍——一遍在主机上（慢但直观、用 f64 累加），一遍在 GPU 核里（快但难肉眼验证）。两遍结果对得上，比「相信其中任何一遍」都更可信。关键设计是**分级的相等性**：排序类结果（top-k 的 id 序列、rank）要求完全相等；浮点数值只要求 \( |got - want| \le 5\times10^{-5} \)。
- **回归测试（regression test）**：修完一个 bug 后，把「触发这个 bug 的输入 + 正确输出」写成永久断言。它既是防复发的外科手术线，也是唯一不会过时的文档——注释会撒谎，断言不会。
- **criterion 微基准**：Rust 生态标准的基准框架。与手写 `Instant::now()` 计时不同，criterion 自动做预热（warmup）、多轮采样、离群值剔除，并给出统计意义上的变化检测——适合回答「这次改动让解码采样变快还是变慢了」。
- **`#[ignore]` 探针**：不是正确性门、只打印数字的测试。用 `#[ignore]` 标记后不进常规 CI 轮转，需要人手动 `--ignored --nocapture` 运行——因为它慢、且没有人可以自动断言「微秒数应该等于多少」。
- 回顾前几讲的关键结论（本讲会反复用到）：`effectively_greedy` 的核坍缩判据 \( \text{top\_p} \le 1/V \)（u2-l1）；`selection_width` 与 tile 垫宽的区别（u2-l2）；三路分发（u2-l3）；`mix_seed` 与 philox 子序列（u2-l4）；log-softmax 主机参考（u3-l1）；设备批式孪生（u3-l2）。

## 3. 本讲源码地图

| 文件 | 作用 | 测试数 |
|------|------|--------|
| `tests/select_batch.rs` | GPU 集成测试：`select_batch` 的路由、归位、种子重放、min_p 隔离、容量拒绝 | 11 个 `#[test]`（其中 1 个 `#[ignore]` 延迟探针） |
| `tests/gpu_logprobs.rs` | GPU 集成测试：`token_logprobs_batch` 与主机参考的 A/B 互证、并列序、越界拒绝、stream override 拒绝 | 7 个 `#[test]` |
| `benches/select_batch.rs` | criterion 微基准：Qwen 词表宽度下 greedy / sampling / mixed 三档 × 4 个 batch 档位 | 1 个基准组 |
| `src/lib.rs` | 被测 API（`select_batch`、`token_logprobs_batch`、`effectively_greedy`、`mix_seed`）+ 内嵌 `mod tests`（3 个纯 CPU 单元测试，只测主机参考 `token_logprob_from_row`） | 3 个 CPU `#[test]` |
| `Cargo.toml` | `autobenches = false` + 显式 `[[bench]]` 声明，criterion 为 dev-dependency | — |

永久链接（本讲全部引用均基于 HEAD `af9e40e1`）：

- [tests/select_batch.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/tests/select_batch.rs)
- [tests/gpu_logprobs.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/tests/gpu_logprobs.rs)
- [benches/select_batch.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/benches/select_batch.rs)
- [src/lib.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/src/lib.rs)
- [Cargo.toml](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/Cargo.toml)

## 4. 核心概念与源码讲解

### 4.1 合成 arena 测试法：不加载权重的集成测试

#### 4.1.1 概念说明

`select_batch` 的输入是一个 logits arena（`HiddenStates`，形状 = 行数 × 词表宽），输出是每行一个 token id。测试这个函数**不需要模型**——只要能造出一个可控的 arena，就能精确预测每条路径应该输出什么。

这套方法的核心是三个小工具：

- `make_arena`：把若干行 `f32` logits 拼平、转 bf16、上传设备，得到一个真实的 `HiddenStates`；
- `peak_row`：**单峰行**——平坦地板上立一个尖峰，贪心结果就是尖峰下标，适合测路由与归位；
- `noise_row`：**确定性噪声行**——用 SplitMix64 风格的整数散列生成量化到 1/128 步长的 bf16 值，大量并列自然涌现，适合测 top-k 并列序与 logprob 数值。

测试文件顶部的模块文档把这条哲学写得很清楚——「裸核正确性归 `pegainfer-kernels`，这里只检查策略层把行接到了正确的路径、把 token 落到了正确的下标」。

#### 4.1.2 核心流程

造一个测试的四步固定套路：

1. `DeviceContext::new()` 拿上下文（**这一行就是「需要 GPU」的标志**——lib 内嵌的 CPU 测试没有它）。
2. 用 `peak_row` / 字面量 / `noise_row` 构造每行的 logits（`Vec<f32>` 或 `Vec<bf16>`）。
3. `make_arena` 上传成 `HiddenStates`；`SampleScratch::new(&ctx, vocab, rows)` 建容量匹配的 scratch。
4. 调 `select_batch`（或 `token_logprobs_batch`），断言 token 序列或与主机参考对照。

`make_arena` 的形状约定：第 `i` 行是第 `i` 个请求的 logits，所有行必须等宽（ ragged 直接 `assert!` 拦下），arena 的 `hidden_dim` = vocab、`seq_len` = 行数——这正好对上 `select_batch` 入口处 `ensure!(vocab == scratch.vocab)` 与 `ensure!(logits.seq_len >= n)` 的两条校验（见 u2-l3）。

#### 4.1.3 源码精读

先看 `make_arena` 与两个生成器（注意 `tests/select_batch.rs` 的版本收 `Vec<f32>` 并在内部转 bf16，`tests/gpu_logprobs.rs` 的版本直接收 `Vec<bf16>`）：

- [tests/select_batch.rs:20-31](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/tests/select_batch.rs#L20-L31)：`make_arena` —— 断言行等宽、`HiddenStates::zeros(ctx, vocab, rows.len())` 分配、`flat_map` 拼平并逐元素 `bf16::from_f32`、`memcpy_htod` 上传后 `ctx.sync()` 确保落盘。
- [tests/select_batch.rs:51-55](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/tests/select_batch.rs#L51-L55)：`peak_row` —— 全 0 地板上把 `r[peak]` 设为 `height`，一行代码让贪心期望值变成常量。
- [tests/gpu_logprobs.rs:25-35](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/tests/gpu_logprobs.rs#L25-L35)：`noise_row` —— 乘黄金伽马常数再两轮混合，最后量化到 `(z % 4096) / 128.0 - 16.0`。量化步长 1/128 与 bf16 的尾数宽度配合，使**并列值在整个词表上反复出现**——这正是 4.2 节并列序测试需要的素材。

一个最小示例——贪心批测试，四行四个尖峰，期望输出就是尖峰下标本身：

- [tests/select_batch.rs:57-78](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/tests/select_batch.rs#L57-L78)：`greedy_batch_picks_each_rows_argmax` —— 峰位 `[3, 7, 0, 15]`，断言 `tokens == vec![3, 7, 0, 15]`。测试同时覆盖了「批式 indexed argmax + 按行号归位」两个机制，却只有 20 行。

对照 lib 内嵌的 CPU 测试模块，可以看清「CPU 单元测试 / GPU 集成测试」的分界线只在于是否构造 `DeviceContext`：

- [src/lib.rs:651-704](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/src/lib.rs#L651-L704)：内嵌 `mod tests` 只 import 了 `token_logprob_from_row`——纯主机函数，3 个测试无需 GPU 即可跑（`cargo test --release -p pegainfer-sample --lib`）。

#### 4.1.4 代码实践

**实践目标**：在没有 GPU 的机器上验证 CPU/GPU 测试分界，并审计 arena 的形状契约。

**操作步骤**：

1. 运行 `cargo test --release -p pegainfer-sample --lib`，确认 3 个主机 logprob 测试通过（不需要 GPU）。
2. 运行 `cargo test --release -p pegainfer-sample --test select_batch`（无 GPU 时预期失败于 `DeviceContext::new()`——这本身就是分界线的证明）。
3. 打开 [tests/select_batch.rs:20-31](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/tests/select_batch.rs#L20-L31)，写下：传入 `rows = [3 行 × 16 宽, 1 行 × 17 宽]` 时，哪一行代码先报错？答案是 L22 的 `assert!(rows.iter().all(|r| r.len() == vocab), "ragged rows")`——它在分配 arena 之前就把参差行拦下，否则 `flat_map` 拼平后行边界就错位了。

**需要观察的现象**：步骤 1 全绿、步骤 2 在设备初始化处失败；步骤 3 确认 ragged 防线在 H2D 之前。

**预期结果**：CPU 测试 3 个通过；GPU 测试因无设备报错——「需要 GPU」的判定依据是测试是否调用 `DeviceContext::new()`，而非任何配置开关。**待本地验证**（本讲义写作环境无 GPU）。

#### 4.1.5 小练习与答案

**练习 1**：`peak_row(vocab=16, peak=5, height=10.0)` 的一行经 `make_arena` 转成 bf16 后，贪心期望输出是什么？如果两个下标同为 10.0（如 `tiny_top_p` 测试的 lo/hi 并列），期望输出又是哪个？

**答案**：前者是 5。后者是**较小的下标**——`argmax` 按最低下标破 bf16 并列，这正是 [tests/select_batch.rs:163-168](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/tests/select_batch.rs#L163-L168) 断言 `vec![lo as u32]` 的依据。

**练习 2**：为什么 `noise_row` 要把输出量化到 1/128 步长，而不是直接用 `(z % 65536) as f32 / 65536.0` 这种细粒度？

**答案**：细粒度几乎不产生并列，而 logprob 通道的并列序契约（tie 按 token id 升序）必须在**有并列可测**的分布上验证。1/128 步长让相同量级落在同一 bf16 表示上，使并列在整个词表上反复涌现（注释原话："so ties recur throughout"）。

**练习 3**：`make_arena` 末尾为什么要 `ctx.sync()`？

**答案**：`memcpy_htod` 是异步入队的；后续 `select_batch` 虽在同一流上按序执行、正确性不依赖这次同步，但测试的计时与「arena 内容确实已就位」的语义需要一次显式同步兜底，避免后续分配/断言与上传交错时产生歧义。

### 4.2 GPU-主机 A/B 对照：双实现互证作为正确性门

#### 4.2.1 概念说明

logprob 通道有一个天然优势：它有**两套独立实现**。

- 主机侧：[src/lib.rs:473-520](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/src/lib.rs#L473-L520) 的 `token_logprob_from_row` —— 逐元素循环、f64 累加、`partition_point` 维护 top-k。慢，但每一行都能对照数学定义直接读懂（u3-l1 精读过）。
- 设备侧：[src/lib.rs:533-649](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/src/lib.rs#L533-L649) 的 `token_logprobs_batch` —— 一次 `logprob_topk_batch_bf16_into` launch 服务整批（u3-l2 精读过）。

`tests/gpu_logprobs.rs` 的全部 7 个测试共用一个断言器 `assert_matches_host`：**设备结果必须匹配主机参考**。这就是双实现互证——两套实现犯同一个错的概率远小于任何一套单独出错。

相等性是分级的：

| 维度 | 相等性要求 | 理由 |
|------|-----------|------|
| `rank` | 完全相等 | 整数计数，无舍入余地 |
| top-k 的 **id 序列** | 完全相等（含并列时的 id 升序） | 排序契约是行为契约，不是数值契约 |
| `logprob` 数值 | \( \le 5\times10^{-5} \) 容差 | 两套浮点路径（f64 累加 vs GPU 归约顺序）舍入不同 |
| 批组合无关性 | **按位**相等（`to_bits`） | 同一行单独算与夹在批里算必须走完全相同的核路径 |

#### 4.2.2 核心流程

`assert_matches_host(row, got, picked, top_k)` 的对照流程：

1. 用同一行数据在主机上算 `want = token_logprob_from_row(row, picked, top_k)`。
2. 断言 `got.rank == want.rank`（严格）。
3. 断言 \( |got.logprob - want.logprob| \le TOL \)。
4. 提取两侧 top-k 的 id 序列，断言完全相等（这同时钉死了「按 logit 降序、并列按 id 升序」的稳定序）。
5. 逐对 zip 断言每个 top-k 的 logprob 也在容差内。

测试数据用 `noise_row`（带并列）与手工构造的「双梯队并列」（tier 8.0 × 3 个 + tier 6.5 × 4 个），把 k 的边界（2/3/5/7）恰好切在并列簇内部。

#### 4.2.3 源码精读

- [tests/gpu_logprobs.rs:13-13](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/tests/gpu_logprobs.rs#L13-L13)：`const TOL: f32 = 5e-5;` —— 整个文件的数值门。bf16 输入下两侧舍入路径不同，5e-5 已足够紧（能抓实错）又足够松（不误报）。
- [tests/gpu_logprobs.rs:37-55](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/tests/gpu_logprobs.rs#L37-L55)：`assert_matches_host` —— 上面流程的落地。注意 L46-48 先比 id 序列再比数值：id 错是排序契约破坏，比数值偏差更严重，先报出来诊断信息更准。
- [tests/gpu_logprobs.rs:94-159](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/tests/gpu_logprobs.rs#L94-L159)：`tie_ordering_matches_host` —— 三峰（901/77/2048 同为 8.0）+ 四次峰（6.5）的双梯队。L132-137 断言 k=5 时 id 序列为 `[77, 901, 2048, 15, 512]`（第一梯队按 id 升序，再进第二梯队），且 `rank == 3`（三个并列峰都计入被选 token 的 rank）。L139-158 更狠：挑三个**相邻 bf16 编码**（`0x3480/0x3481` 与负值 `0xBC24`），它们减去 LSE 后在 f32 里坍缩成同一个值——断言此时 id 序仍为 `[1, 0, 2]`。
- [tests/gpu_logprobs.rs:283-336](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/tests/gpu_logprobs.rs#L283-L336)：`outputs_are_independent_of_batch_composition` —— 同一行（`noise_row(vocab, 0xA11CE)`）单独成批与夹在 3 行批中间，结果必须 **`to_bits()` 逐位相同**。这比容差更强：证明设备核对每一行是逐行独立的，批组合不改变任何一位。
- [tests/gpu_logprobs.rs:57-92](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/tests/gpu_logprobs.rs#L57-L92)：`matches_host_reference_across_k_at_model_vocab` —— 在 151,936 的真实模型词表上，4 个请求覆盖 `k=0/1/5/20`、`picked` 取首/尾/中部，一轮全部对照主机。

#### 4.2.4 代码实践

**实践目标**：亲手加一条 A/B 测试，体验「主机是锚点」的写法。

**操作步骤**（示例代码，加入 `tests/gpu_logprobs.rs`；需要 GPU，**待本地验证**）：

```rust
// 示例代码：小词表全 picked 扫描——每一列都被选一次，逐一与主机对照。
#[test]
fn small_vocab_full_picked_sweep_matches_host() {
    let ctx = DeviceContext::new().unwrap();
    let vocab = 64;
    let rows = vec![noise_row(vocab, 0x5EED)];
    let arena = make_arena(&ctx, &rows);

    let requests: Vec<LogprobRequest> = (0..vocab as u32)
        .map(|picked| LogprobRequest { row: 0, picked, top_k: 4 })
        .collect();
    let got = token_logprobs_batch(&ctx, &arena, &requests).unwrap();

    assert_eq!(got.len(), requests.len());
    for (req, out) in requests.iter().zip(&got) {
        assert_matches_host(&rows[0], out, req.picked, req.top_k);
    }
}
```

**需要观察的现象**：64 个 picked（从最高 logit 的列到最低 logit 的列，rank 从 1 扫到 64）全部通过，包括 rank 最大的列——它的 logprob 是全表最负值，最容易暴露 LSE 舍入差异。

**预期结果**：全绿；若把 `top_k` 改成 0，断言仍应通过（`top_logprobs` 为空，只对照 logprob 与 rank）。**待本地验证**（写作环境无 GPU）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `rank` 用 `assert_eq!` 而 `logprob` 用容差？如果反过来（rank 给容差、logprob 逐位相等）会怎样？

**答案**：rank 是整数（「原始 logit 不小于被选值的个数」），两套实现对同一 bf16 输入数出来必然一致，有容差也无从用起；logprob 两侧的求和顺序与精度（主机 f64 累加 vs GPU 并行归约）不同，逐位相等会被合法舍入差异打爆。反过来做会让测试既测不出排序错误、又永远误报。

**练习 2**：`outputs_are_independent_of_batch_composition` 为什么用 `to_bits()` 而不是 `==` 比较 f32？

**答案**：f32 的 `==` 对 `0.0 == -0.0` 为真、对 NaN 为假，且无法区分「相等」与「恰好同值」；`to_bits()` 要求两个浮点数的位表示完全一致，是比容差更强的契约——它证明批组合没有改变核内的任何一次累加顺序。

**练习 3**：[tests/gpu_logprobs.rs:139-145](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/tests/gpu_logprobs.rs#L139-L145) 用 `bf16::from_bits(0x3480)` 手工构造相邻编码，想测什么？

**答案**：三个输入的原始 logit 严格不同，但减去同一个 LSE 并舍入到 f32 后**坍缩为同一个值**——制造「减法之后才并列」的极端场景，验证排序用的是减 LSE 之前的原始 logit 语义（主机实现里 top-k 用原始值插入、只在最后统一减 LSE，见 [src/lib.rs:501-512](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/src/lib.rs#L501-L512)），设备核必须复刻同一语义。

### 4.3 回归测试即文档：三段被冻结的历史

#### 4.3.1 概念说明

这个 crate 的每个回归测试都是「**注释讲历史 + 断言冻结局**」的二联画：注释解释当年为什么出错（per-step philox 迁移暴露了什么、#1046 的 tile 垫宽是什么），断言把修复后的行为钉死。后来者读测试就能学到不变量，删掉任何一行断言都会立刻弄丢一段历史。

三段最有代表性的历史：

1. **per-step philox 迁移**：引擎从「每请求一个 RNG」迁到「每步一个引擎种子」后，原本被 per-request 种子掩盖的问题暴露了——极小 top_p 的行走拒绝采样器时，会在 bf16 并列的最大值里**随机挑一个**，且每个种子挑得不一样。
2. **#1046 tile 对齐**：qwen35 把 logits GEMM 对齐到 tile 倍数，arena 物理宽度超过可发射词表。若路由判定错用物理宽度当分母，\( \text{top\_p} \le 1/V \) 的核坍缩判定被稀释，本应走 argmax 的行被推进采样器。
3. **min_p 邻居扰动**：min_p 行若与普通行混在同一趟批式采样里，普通行的 philox 子序列位置（批内下标）会被 min_p 行挤动，token 随批组合漂移。

#### 4.3.2 核心流程

回归测试的共同骨架：

```
构造曾经触发 bug 的分布（含 bf16 并列峰）
  ↓
先跑"控制段"：证明在没有修复的路径上，错误行为确实会出现
  ↓
再跑"断言段"：修复后的路由/分区让行为确定且正确
  ↓
跨 64 个种子循环——错误行为是种子相关的，一次断言不够
```

对应到源码行为上，三段历史分别冻结在：

- 案例 1 → `effectively_greedy` 的核坍缩分支：[src/lib.rs:455-461](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/src/lib.rs#L455-L461) 把 \( 0 < \text{top\_p} \le 1/V \) 的行判为 argmax 路径，数学依据是 softmax 最大概率恒满足 \( P_{\max} \ge 1/V \)：
  \[ P_{\max} = \frac{e^{x_{\max}}}{\sum_j e^{x_j}} \ge \frac{e^{x_{\max}}}{V \cdot e^{x_{\max}}} = \frac{1}{V} \]
  于是 top_p 阈值不超过 \( 1/V \) 时核内只剩 argmax 一个 token。
- 案例 2 → `selection_width` 与 vocab 分离：[src/lib.rs:223-225](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/src/lib.rs#L223-L225) 注释明说「垫宽列不是可发射 token，不能移动核判定」，路由分母用 `scratch.selection_width`。
- 案例 3 → min_p 分区：[src/lib.rs:265-267](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/src/lib.rs#L265-L267) 注释说明 min_p 行由 `gpu_sample_batch_into` 内部分区到独立一趟，普通行永远走融合快路径、批内下标不被 min_p 邻居挤动。

#### 4.3.3 源码精读

**案例 1：[tests/select_batch.rs:142-178](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/tests/select_batch.rs#L142-L178)** `tiny_top_p_routes_to_argmax_even_under_bf16_ties`

- L157-159：151,936 宽词表上设两个 **bf16 精确相等** 的 8.0 峰（lo=879, hi=6941）——真并列。
- L163-168：控制段，纯贪心断言 argmax 取 `lo`（最低下标破并列）。
- L170-177：`top_p = 1e-6 < 1/151936 ≈ 6.6e-6`，**64 个种子循环**断言全部等于贪心结果。注释讲透历史：旧的 per-request 种子把这个随机性掩盖了，per-step 种子把它暴露出来。
- 词表取 151,936 不是随手——注释末句说明大词表才匹配模型的 OnlineSoftmax 核 regime。

**案例 2：[tests/select_batch.rs:180-219](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/tests/select_batch.rs#L180-L219)** `padded_arena_width_does_not_suppress_the_argmax_routing`

- L186-195：`decodable = 256`，arena 物理 `vocab = 512`（两倍垫宽），`top_p` 恰取 \( 1/256 \)。
- L199-209：**控制段**——用物理宽度当分母的旧 scratch（`SampleScratch::new`）跑 64 个种子，断言 `sampled_the_peer` **必须出现过**：证明在错误路由下采样器真的会随机输出并列另一个峰 hi。这一段是测试的「负对照」，缺了它后段的相等就可能是巧合。
- L211-218：修复后的 `with_selection_width(vocab=512, selection_width=256)` 下，64 个种子全部回到 `lo`。

**案例 3：[tests/select_batch.rs:277-323](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/tests/select_batch.rs#L277-L323)** `mixed_batch_keeps_plain_rows_on_the_fast_path`

- L288-291：同一份 `spread`（`i * 0.3` 递增）分布造两个 arena——3 行混批（plain/min_p/plain）与 2 行纯批（plain/plain）。
- L294-296：plain 行带 `top_k=4, top_p=0.9`（多均匀抽样），min_p 行 `min_p=0.5`。注释解释为什么这个参数组合能「真的分叉」：融合快路径（拒绝采样、多次均匀抽样）与 min_p 管线（重归一 + 单次抽样）在扩散分布上产出不同 token，走错路径不是 1-ulp 的差别。
- L298-322：64 个种子循环，断言混批里两个 plain 行的 token 与纯批**完全一致**——min_p 邻居零扰动。

顺带一提，源码里也有一段「文档式注释」承载历史：[src/lib.rs:71-76](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/src/lib.rs#L71-L76) 记录了 #704 事故（pageable 回读被 KV 恢复洪流拖成 23.6ms），它不是测试而是字段注释——回归测试之外的另一种冻结方式（u4-l2 精读过）。

#### 4.3.4 代码实践

**实践目标**：理解「控制段」在回归测试里不可替代的作用。

**操作步骤**：阅读 [tests/select_batch.rs:199-209](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/tests/select_batch.rs#L199-L209)，写一段不超过 200 字的分析，回答：如果把控制段整段删掉，只保留 L211-218 的 `bounded` 循环，测试还能不能区分「路由正确」与「argmax 恰好等于采样结果」？

**需要观察的现象 / 预期结果**：不能。控制段在错误路由（物理宽度分母）下实测到采样器输出了 `hi`，证明该分布下两条路径**确实会分叉**；有了这个负对照，后段「64 个种子恒等于 lo」才只能归因于 argmax 路由，而非分布本身让两条路径碰巧同答。删掉控制段后，若未来有人把路由分母改回 vocab 且碰巧分布变了，测试可能静默通过。这是一个纯阅读实践，无需 GPU。

#### 4.3.5 小练习与答案

**练习 1**：案例 1 的测试为什么循环 64 个种子，而不是只跑一个种子？

**答案**：bug 的表现是「种子相关地在并列峰之间随机挑」——单次运行可能恰好挑中 lo，测试静默通过。64 个种子把「每个种子都取 lo」变成强断言：只要有一个种子挑了 hi 就失败。错误行为是概率性的，断言必须覆盖足够多种子。

**练习 2**：案例 2 中 `top_p = 1.0 / decodable as f32`（恰好等于 1/256）而不是更小的 1e-6，为什么这个边界值更有杀伤力？

**答案**：判据是 \( \text{top\_p} \le 1/V \)，恰好等于是**边界上的最后一个应坍缩点**。物理宽度 512 当分母时 \( 1/256 > 1/512 \)，判定失效、行落进采样器——边界值把「分母错一个量级」直接放大成可观测的行为分叉；用远小于边界的 1e-6 时即便分母翻倍也仍满足坍缩，测不出错误。

**练习 3**：`seeded_rows_replay_independent_of_batch_position`（[tests/select_batch.rs:325-371](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/tests/select_batch.rs#L325-L371)）末尾 `assert_ne!` 的注释说「平坦 32k 分布上偶然碰撞概率 ~3e-5」。这个数字怎么来的？为什么敢用它做断言？

**答案**：平坦分布下每个 token 概率约 \( 1/32768 \approx 3\times10^{-5} \)，两个不同 philox 流恰好撞上同一个 token 的概率就是这个量级。它在一次 CI 运行里小到可忽略，但又不是零——注释坦白这一点是诚实的工程表达：断言「几乎必然」成立，若哪天偶发失败，读者知道这可能是极小概率碰撞而非真 bug（重跑即可分辨）。

### 4.4 criterion 微基准与延迟探针

#### 4.4.1 概念说明

正确性门回答「对不对」，基准回答「多快」。这个 crate 有两件测速的工具，分工不同：

- **criterion 微基准**（`benches/select_batch.rs`）：统计式基准，比较**三种参数形态**的成本结构——
  - `greedy`：批式 indexed argmax 的成本**下限**；
  - `sampling`：FlashInfer 采样通道的**全额**成本（softmax + top-k/top-p + 抽样）；
  - `mixed`：贪心与采样各半的**常见服务形态**（两路顺序执行，成本近似相加）。
- **延迟探针**（`#[ignore]` 的测试）：打印**每调用微秒数**，不带任何断言。它回答的是绝对量级问题（「这个调用是亚毫秒还是毫秒级」），而不是相对比较——所以没人能为它写自动断言，只能人工运行人工读数。

两者都必须在**真实词表宽度**（151,936）上跑：采样成本随词表线性走，小词表的微秒数没有参考价值。

#### 4.4.2 核心流程

criterion 基准的结构（`bench_select_batch`）：

```
for batch in [1, 8, 32, 64]:            # 批维档位
    造 arena(vocab=151_936, batch)        # 确定性非平坦 logits（哈希散列）
    建 SampleScratch(vocab, batch)        # 容量匹配
    for (name, params) in [greedy, sampling, mixed]:   # 参数形态
        criterion 以统计方式反复调用 select_batch
        （每次迭代 seed+1，模拟每步新鲜种子）
```

延迟探针的结构（`select_batch_span17_latency_probe`）：

```
17 行 × 151_936 宽的确定性 logits
  ↓
每个参数形态：先 20 次预热 + ctx.sync()
  ↓
计 200 次迭代（每步换 seed），结束后再 ctx.sync()
  ↓
eprintln! 打印 us/call —— 不 assert 任何东西
```

注意探针的计时纪律：`ctx.sync()` 在起表前与收表后各一次——起表前清掉预热的尾巴，收表后把仍在设备上排队的核**算进耗时**。漏掉收尾那次同步，测出来的是「入队速度」而不是「完成速度」。

#### 4.4.3 源码精读

- [benches/select_batch.rs:16-25](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/benches/select_batch.rs#L16-L25)：`arena` —— 用 `i.wrapping_mul(2_654_435_761) % 1000 * 0.01` 的整数散列造**确定性非平坦** logits，注释明说目的是让 top-k/top-p「做真工作」（平坦分布会让采样退化成均匀抽样，测不出核的真实成本）。
- [benches/select_batch.rs:42-60](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/benches/select_batch.rs#L42-L60)：三档 mix 的构造——greedy 是 `p(0.0, -1, 1.0)`（temperature 0 即贪心）、sampling 是 `p(1.0, 50, 0.9)`、mixed 按下标奇偶各取一半。
- [benches/select_batch.rs:63-70](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/benches/select_batch.rs#L63-L70)：`b.iter` 闭包里 `seed = seed.wrapping_add(1)` —— 复刻真实解码循环「每步新鲜引擎种子」的语义，避免把采样路径优化成同一种子的重复调用。
- [benches/select_batch.rs:76-77](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/benches/select_batch.rs#L76-L77)：`criterion_group!` / `criterion_main!` 宏收尾，是 criterion 基准的标准样板。
- [Cargo.toml:2-23](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/Cargo.toml#L2-L23)：`autobenches = false` + 显式 `[[bench]] name = "select_batch", harness = false` —— criterion 自带 main，必须关掉 libtest 的默认 harness；criterion 在 `[dev-dependencies]`。
- [tests/select_batch.rs:373-429](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/tests/select_batch.rs#L373-L429)：延迟探针全文。L375 注释直接给它定性：「Not a correctness gate — prints per-call microseconds」。L377 的 `#[ignore = "GPU latency probe — run manually with --nocapture"]` 让它退出常规轮转。L416-427 是预热-计时-同步-打印的四段式。三个参数形态（greedy / t0.8_topp0.95 / t0.7_topk20_topp0.9）对应 #512 当年要回答的「verify 轮采样成本」问题。

#### 4.4.4 代码实践

**实践目标**：扩展基准的批维覆盖，观察成本随 batch 的伸缩。

**操作步骤**：

1. 把 [benches/select_batch.rs:42-42](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/benches/select_batch.rs#L42-L42) 的 `for &batch in &[1usize, 8, 32, 64]` 改为 `for &batch in &[4usize, 16, 128]`（改的是本地面目，测完还原即可；注意 `SampleScratch` 与 arena 都按 batch 建，无需其它改动）。
2. 运行 `cargo bench -p pegainfer-sample`（criterion 默认即优化构建）。
3. 记录 `select_batch/greedy`、`select_batch/sampling`、`select_batch/mixed` 在三个新档位的时间。
4. 复原第 1 步的修改。

**需要观察的现象**：greedy 档随 batch 增长的斜率（批式 argmax 对行数近似线性、且有核启动固定开销）；sampling 档是否在 batch=128 时出现台阶（词表维 softmax 的并行度饱和）；mixed 是否近似 greedy+sampling 之和（两路顺序执行）。

**预期结果**：三档时间随 batch 单调上升，mixed ≈ greedy + sampling（u2-l3 讲过两路顺序执行、无并行）。具体数字**待本地验证**（写作环境无 GPU，且基准数字依赖具体显卡型号）。

#### 4.4.5 小练习与答案

**练习 1**：延迟探针为什么放在 `#[cfg(test)]` 的测试文件里、标 `#[ignore]`，而不是做成第三个 criterion 基准？

**答案**：探针的产出是**绝对微秒数**（「span-17 一次调用多少微秒」），没有可自动断言的期望值——放进 criterion 也只能人工读数；而它需要 GPU、跑得慢，挂在常规 `cargo test` 上会拖慢每个人。`#[ignore]` + `-- --ignored --nocapture` 让它「按需人工运行、直接 eprintln 打数」，是成本最低的宿主。

**练习 2**：基准的 `arena` 函数特意造非平坦 logits（哈希散列）。如果换成全 0 的平坦 arena，greedy 档的时间会变吗？sampling 档呢？

**答案**：greedy 档几乎不变——argmax 的成本只依赖形状（扫一遍取最大），与数值内容无关。sampling 档会**失真**——top-k/top-p 在平坦分布上退化为均匀截断，拒绝采样的接受/重抽模式与真实分布不同，测出的不再是服务形态的成本。所以注释要求「deterministic non-flat logits so top-k/top-p do real work」。

**练习 3**：探针在 200 次迭代循环**之后**才 `ctx.sync()` 再停表。如果有人不小心把 `ctx.sync()` 挪到循环**之前**、且循环后不再同步，测出的数字会怎么偏？

**答案**：会**系统性偏小**。`select_batch` 内部虽有 `stream_spin_wait`（贪心路径等自己的回读），但采样路径的核是异步入队的；循环后不同步就停表，量到的只是主机侧入队耗时，设备上还在跑的核全都没被计入。

## 5. 综合实践

把本讲三个手法（合成 arena、种子重放契约、与单独调用对照）拧成一个新集成测试。

**任务**：为 `select_batch` 新写一个 4 行混批测试——两行带不同请求种子、一行 min_p、一行贪心——断言两条契约：

1. **种子重放**：两个 seeded 行在「换邻居、换引擎种子」后 token 完全重放（token 是 `(seed, step, 分布)` 的纯函数）；
2. **贪心一致**：贪心行的 token 与它单独成批调用完全一致。

**操作步骤**（示例代码，追加到 `tests/select_batch.rs`，复用文件内已有的 `make_arena`/`refs`/`greedy`/`sampling`/`peak_row`；需要 GPU，**待本地验证**）：

```rust
// 示例代码：四行混批——两个 seeded、一个 min_p、一个贪心。
// 契约一：seeded 行 token 是 (seed, step, 分布) 的纯函数，与批组合、
// 邻居、引擎种子无关；契约二：贪心行 token 与单独调用一致。
#[test]
fn four_row_mix_replays_seeded_and_matches_solo_greedy() {
    let ctx = DeviceContext::new().unwrap();
    let vocab = 32_768;

    // seeded 行用两条不同的扩散分布（换流必换 token），min_p 行用
    // 双 token 支撑（min_p=0.5 只剩 token 3），贪心行用单峰。
    let spread_a: Vec<f32> = (0..vocab).map(|i| i as f32 * 0.3).collect();
    let spread_b: Vec<f32> = (0..vocab).map(|i| i as f32 * -0.2).collect();
    let mut twin = vec![-60.0f32; vocab];
    twin[3] = 1.0;
    twin[11] = 0.0;
    let peak = peak_row(vocab, 5, 10.0);

    let seeded = |seed: u64| SamplingParams {
        temperature: 1.0,
        seed: Some(seed),
        ..SamplingParams::default()
    };
    let mut minp = sampling(1.0, -1, 1.0);
    minp.min_p = 0.5;

    let mut scratch = SampleScratch::new(&ctx, vocab, 4).unwrap();

    // 批 A：[seeded(11), min_p, greedy, seeded(22)]
    let arena_a = make_arena(
        &ctx,
        &[spread_a.clone(), twin.clone(), peak.clone(), spread_b.clone()],
    );
    let params_a = vec![seeded(11), minp.clone(), greedy(), seeded(22)];
    let a = select_batch(
        &ctx,
        &arena_a,
        &refs(&params_a),
        &[3, 0, 0, 7],
        99,
        &mut scratch,
    )
    .unwrap();

    // 批 B：换邻居、换引擎种子、去掉 min_p 行、行序重排。
    let arena_b = make_arena(&ctx, &[peak.clone(), spread_b, spread_a]);
    let params_b = vec![greedy(), seeded(22), seeded(11)];
    let b = select_batch(
        &ctx,
        &arena_b,
        &refs(&params_b),
        &[0, 7, 3],
        1234,
        &mut scratch,
    )
    .unwrap();

    // 契约一：seeded(11) 在批 A 行 0 / 批 B 行 2，同 (seed, step, 分布)。
    assert_eq!(a[0], b[2], "seeded(11) must replay across batch layouts");
    // 契约一：seeded(22) 在批 A 行 3 / 批 B 行 1。
    assert_eq!(a[3], b[1], "seeded(22) must replay across batch layouts");
    // 两条 seeded 流几乎必然不同（碰撞概率 ~1/32768，见 seeded_rows 测试注释）。
    assert_ne!(a[0], a[3], "distinct seeds must select distinct streams");

    // 契约二：贪心行单独调用结果一致（单峰 => 恒为峰下标 5）。
    let arena_solo = make_arena(&ctx, &[peak]);
    let solo = select_batch(
        &ctx,
        &arena_solo,
        &[&greedy()],
        &[0],
        0,
        &mut scratch,
    )
    .unwrap();
    assert_eq!(a[2], solo[0], "greedy row must match its solo call");
    assert_eq!(a[2], 5, "peak row argmax is the peak index");
}
```

**需要观察的现象**：

- 契约一两条 `assert_eq!` 通过——seeded 行换了邻居（批 A 里挨着 min_p 行、批 B 里挨着贪心行）、换了引擎种子（99 → 1234）、换了批内位置（行 0 → 行 2），token 纹丝不动。这正是 u2-l4 讲的「seeded 行必须单行调用、以 `mix_seed` 换流」的直接验证。
- 契约二通过——贪心行在 4 行混批与 1 行独批里都取峰下标 5，验证批式 indexed argmax 的行号归位。
- 把批 B 的 `steps` 从 `[0, 7, 3]` 改成 `[0, 7, 8]`（动 seeded(11) 的步数），第一条断言应当**失败**——步数进了 `mix_seed`，流就换了。

**预期结果**：有 GPU 时 `cargo test --release -p pegainfer-sample --test select_batch four_row_mix` 通过；无 GPU 时先写出测试与断言，标注**待本地验证**（写作环境无 GPU，本讲义未实际运行）。

**配套基准小任务**：按 4.4.4 的步骤把批维换成 `[4, 16, 128]` 跑一轮对比，记录三档时间并检验「mixed ≈ greedy + sampling」的推断。

## 6. 本讲小结

- **模型无关测试法**：`make_arena`（f32 → bf16 → 设备）、`peak_row`（单峰定期望）、`noise_row`（量化散列出并列）三个工具让集成测试不加载任何权重；判一个测试是否需要 GPU，看它是否构造 `DeviceContext`。
- **双实现互证**：`assert_matches_host` 以主机 `token_logprob_from_row` 为锚点对照设备 `token_logprobs_batch`——rank 与 top-k id 序列要求完全相等，logprob 容差 \( 5\times10^{-5} \)，批组合无关性用 `to_bits()` 逐位断言。
- **回归测试即文档**：注释讲历史（per-step philox 迁移、#1046 tile 垫宽、min_p 邻居扰动、#704 pageable 回读），断言冻结局；错误行为是种子相关的，断言要跨 64 个种子；边界值（top_p 恰等于 1/V）与控制段（证明错误路径真的会错）是回归测试的两件杀伤性武器。
- **基准与探针分工**：criterion 三档（greedy 下限 / sampling 全额 / mixed 服务常态）回答相对成本与伸缩；`#[ignore]` 探针打印绝对微秒数，起表前后各一次 `ctx.sync()` 是计时纪律。
- **测试数字的诚实性**：`assert_ne!` 旁标注碰撞概率 \( \approx 3\times10^{-5} \)、探针注释自陈「不是正确性门」——数字与局限都写明，读者才知道失败时该怀疑什么。

## 7. 下一步学习建议

到这里，`pegainfer-sample` 的四个单元（定位与上手、select_batch 主链路、logprob 通道、高级主题）已全部走完。建议的下一步：

1. **向上看消费方**：挑一个模型 crate（推荐 `pegainfer-qwen3`），找它调用 `select_batch` 的解码步，观察 `steps`、每步引擎种子、`SampleScratch` 的生命周期在真实调度器里如何管理——这是对本 crate 全部契约的实战检验。
2. **向下看内核**：`gpu_sample_batch_into` 与 `logprob_topk_batch_bf16_into` 的裸核正确性归 `pegainfer-kernels`；对照本讲的 A/B 思路，去看 kernels crate 自己的核测试如何做黄金值对照。
3. **横向写一个测试**：把综合实践的 4 行混批测试真的跑起来（需要 GPU），再尝试为它加第三条契约——min_p 行在换引擎种子后仍恒为 token 3（对照 [tests/select_batch.rs:242-275](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/tests/select_batch.rs#L242-L275) 的既有断言）。
4. **回到文档**：`docs/subsystems/` 下与本 crate 相关的子系统文档记录了设计决策的「为什么」，是源码注释之外的另一半上下文。
