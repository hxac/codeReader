# token_logprob_from_row：主机端 log-softmax 参考实现

> 本讲属于单元三（logprob 通道），依赖 u1-l1 的分层认知：`pegainfer-sample` 是所有模型 crate 共享的采样策略层，本讲进入它的第二条职责——logprob 提取——的主机侧参考实现。

## 1. 本讲目标

学完本讲，你应该能够：

1. **推导 log-softmax**：为什么 `logprob(picked) = x_picked − LSE(row)`，其中 LSE（log-sum-exp）要先用最大值平移再求指数和、取对数。
2. **说清精度纪律**：对一个约 16 万宽的词表，为什么指数和必须用 f64 累加，而 f32 累加会损失精度、甚至大到吃掉 GPU 对照测试的容差。
3. **解释 rank 的计算时点**：为什么名次必须在减去 LSE **之前**用原始 logit 比较，而不是比较算出来的 logprob。
4. **读懂 top-k 的维护手法**：插入 + 截断的 O(V·k) 算法、`partition_point` 二分定位、以及并列值按 token id 升序的稳定序是怎么来的。
5. 理解 `T: Copy + Into<f32>` 这一个泛型约束如何让 Qwen 喂 `f32`、Kimi 直接喂 `bf16`，省掉一次 16 万元素的主机加宽拷贝。

## 2. 前置知识

本讲只涉及主机端数学，不需要 CUDA 知识，但需要以下概念：

- **logits**：模型最后一层输出的原始分数向量，长度等于词表大小 V（Qwen3 系为 151,936）。它还不是概率；经 softmax 才变成概率分布。
- **softmax 与 log-softmax**：softmax 把 logits 变成概率 \( p_i = \dfrac{e^{x_i}}{\sum_j e^{x_j}} \)。我们实际要的是它的对数形式（数值上更稳定、也更快）：

  \[
  \log p_i = x_i - \mathrm{LSE}(x), \qquad \mathrm{LSE}(x) = \max_j x_j + \log \sum_j e^{x_j - \max_k x_k}
  \]

  「先用最大值平移」是数值技巧：平移后每个指数项 \( e^{x_j - m} \in (0, 1] \)，且至少有一项等于 1，所以和落在 \([1, V]\) 区间，既不会溢出也不会全下溢。
- **bf16（bfloat16）**：1 位符号 + 8 位指数 + 7 位尾数的半精度浮点。它的指数域与 f32 相同（动态范围一样），但尾数只有 8 位有效位（含隐含位），精度约为 f32 的 \( \frac{1}{256} \)。设备上的 logits arena 就是 bf16。bf16 能被 f32 **无损**表示，所以 `Into<f32>` 对它成立。
- **机器精度（machine epsilon）**：浮点相邻两数的最小相对间隔。\( \varepsilon_{f32} = 2^{-24} \approx 6\times 10^{-8} \)，\( \varepsilon_{f64} = 2^{-53} \approx 1.1\times 10^{-16} \)。把 N 个数顺序累加，最坏情况下相对误差约 \( (N-1)\varepsilon \)——误差会随项数增长，这是本讲的核心动机之一。
- **`Vec::partition_point`**：Rust 标准库方法，在**有序**向量上二分查找「谓词为真的最长前缀」的长度，代价 \( O(\log n) \)。本讲用它维护降序 top-k 表。
- **vLLM 的 rank 约定**：一个 token 的 rank = 词表中 logit **大于等于**它的 token 个数（含并列、含自己），所以 rank ∈ [1, V]。并列最大值的 rank 等于并列的个数。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [src/lib.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/src/lib.rs) | 整个 crate 唯一的源文件。本讲主角 `token_logprob_from_row` 在 L473-L520，配三个 CPU 单元测试（L657-L703） |
| [pegainfer-frontend/src/engine/event.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-frontend/src/engine/event.rs) | 契约类型 `TokenLogprob` 的定义处（L3-L9），含 rank 的 vLLM 约定注释 |
| [pegainfer-qwen35/src/logprobs.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/logprobs.rs) | Qwen 侧调用方的快照类型 `LogprobSnapshot = (Vec<f32>, usize)`（L5），证明 f32 喂入路径 |
| [pegainfer-qwen35/src/executor.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/executor.rs) | 真实调用点（L240-L242）：prefill 首 token 的 logprob 计算 |
| [tests/gpu_logprobs.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/tests/gpu_logprobs.rs) | GPU-主机 A/B 测试：把本函数当作设备批式孪生 `token_logprobs_batch` 的对照标准（容差 L13，模型词表 L58-L60） |

> 提示：`pegainfer-sample` 的源码只有一个 `src/lib.rs`（约 700 行），本讲引用行号均出自它，除非另注明 crate。

## 4. 核心概念与源码讲解

回顾 u1-l1 的定位：这个 crate 有两条模型无关职责，`select_batch` 负责「选哪个 token」，logprob 通道负责「这个 token 有多大概率」。logprob 通道有一对孪生实现——设备端批式的 `token_logprobs_batch`（下一讲 u3-l2），和本讲的主机单行参考实现 `token_logprob_from_row`。它是**正确性的锚点**：GPU 测试拿它当对照标准，所以在精度和顺序语义上它必须做到「可解析验证」的严格。

### 4.1 泛型行输入：一份代码同时服务 f32 与 bf16

#### 4.1.1 概念说明

`token_logprob_from_row` 接收的是**一整行原始 logits**（不是概率）加上被选中 token 的下标 `picked`，返回契约类型 `TokenLogprob`：

```rust
// pegainfer-frontend/src/engine/event.rs（契约定义）
pub struct TokenLogprob {
    pub logprob: f32,
    /// Count of vocabulary logits >= this token's logit, including ties (vLLM convention).
    pub rank: u32,
    pub top_logprobs: Vec<(u32, f32)>,
}
```

三个字段的语义：`logprob` 是被选 token 的 log-softmax 值；`rank` 是它的名次（vLLM 约定，含并列）；`top_logprobs` 是全词表按 logit 降序的前 k 名 `(token_id, logprob)` 列表。

问题：设备上的 logits 是 bf16，而 Qwen 的执行器在主机上已经持有一份 f32 快照，Kimi 则直接从设备 arena 拷出 bf16 行。如果函数签名写死 `&[f32]`，Kimi 每算一行 logprob 都要先把约 16 万个 bf16 加宽拷贝成 f32——而 logprob 只在**请求方明确要求时**才计算（见函数文档注释 [src/lib.rs:L463-L465](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/src/lib.rs#L463-L465)），为一条偶发路径付一次永久拷贝税很不划算。

解法是一个最小泛型约束 `T: Copy + Into<f32>`：调用方原样交出自己已有的类型，函数在遍历内部逐元素转换。bf16→f32 是无损加宽，语义与先整体拷贝再计算完全一致。

#### 4.1.2 核心流程

```text
调用方（两种形态）                     本函数
─────────────────────                ─────────────────────
Qwen: &[f32]（主机快照）  ──┐
Kimi: &[bf16]（设备行）   ──┼──> token_logprob_from_row::<T>(row, picked, top_k)
GPU A/B 测试: &[bf16]     ──┘         │
                                      ├─ 守卫：空行 / picked 越界 → None
                                      ├─ 一遍 O(V)：求 max、累加 rank（4.3 讲）
                                      ├─ 一遍 O(V)：f64 指数和（4.2 讲）
                                      └─ 组装 TokenLogprob
```

两条硬性前置条件（来自文档注释 [src/lib.rs:L466-L472](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/src/lib.rs#L466-L472)）：

1. **行必须是未分片的全词表**：分片局部的 logsumexp 不是全局的，分片调用方必须先跨 rank 归并（#236）。
2. 输入是**原始 logits**，函数内部自己做 softmax 数学。

#### 4.1.3 源码精读

函数签名与守卫：

```rust
pub fn token_logprob_from_row<T>(row: &[T], picked: u32, top_k: usize) -> Option<TokenLogprob>
where
    T: Copy + Into<f32>,
{
    let picked = picked as usize;
    if row.is_empty() || picked >= row.len() {
        return None;
    }
```

- [src/lib.rs:L473-L480](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/src/lib.rs#L473-L480)：签名、泛型约束 `T: Copy + Into<f32>`，以及两个守卫——空行或 `picked` 越界直接返回 `None`，不 panic。这是「调用方可能拿到坏输入」的契约式防御，对应单元测试 [src/lib.rs:L695-L703](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/src/lib.rs#L695-L703) 的两条 `is_none()` 断言。

真实调用方的两种喂法：

- [pegainfer-qwen35/src/executor.rs:L240-L242](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/executor.rs#L240-L242)：Qwen3.5 在 prefill 后计算首 token 的 logprob，`cpu_logits[i]` 的类型是 `LogprobSnapshot = (Vec<f32>, usize)`（见 [pegainfer-qwen35/src/logprobs.rs:L5](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/logprobs.rs#L5)）——**f32 路径**。
- [src/lib.rs:L466-L469](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/src/lib.rs#L466-L469)：文档注释明说 "callers feed `f32` (Qwen, already on host as f32) or `bf16` (Kimi, straight from the device arena) without a widening copy"——**bf16 路径**，Kimi 直接把设备 arena 拷出的 bf16 行喂进来，零加宽拷贝。
- [tests/gpu_logprobs.rs:L37-L38](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/tests/gpu_logprobs.rs#L37-L38)：GPU A/B 测试以 `&[bf16]` 调用本函数，把结果当对照标准。

契约类型的导入点：

- [src/lib.rs:L32](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/src/lib.rs#L32)：`use pegainfer_frontend::engine::TokenLogprob;`——返回类型来自前端契约 crate（u1-l1 讲过：模块文档里的旧称 `pegainfer-engine` 即今天的 `pegainfer-frontend`）。它的字段定义与 rank 约定见 [pegainfer-frontend/src/engine/event.rs:L3-L9](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-frontend/src/engine/event.rs#L3-L9)。

另外注意 `half`（bf16 的来源 crate）是本 crate 的**正式依赖**而非 dev-dependency（[Cargo.toml:L8-L13](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/Cargo.toml#L8-L13)），因为 `SampleScratch` 的 `top1_values` 本身就是 `CudaSlice<half::bf16>`——测试模块里 `use half::bf16;` 才能直接可用。

#### 4.1.4 代码实践

1. **实践目标**：确认本讲的验证途径不需要 GPU，并亲眼看到两种喂法共用同一入口。
2. **操作步骤**：
   - 进入 `pegainfer-sample/` 目录，运行 `cargo test --release --lib`（不构造 `DeviceContext` 的单元测试都能跑）。
   - 打开 [src/lib.rs:L651-L704](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/src/lib.rs#L651-L704) 的 `mod tests`，数一数测试：前两个测试构造 `Vec<bf16>`，第三个构造 `[f32]`——同一函数、两种元素类型。
   - 运行 `cargo doc --no-deps -p pegainfer-sample`，在生成的文档里找 `token_logprob_from_row`，确认签名是泛型 `T`。
3. **需要观察的现象**：三个测试全部通过；文档页面上 `T: Copy + Into<f32>` 出现在函数签名下方。
4. **预期结果**：`token_logprob_matches_exact_log_softmax`、`token_logprob_k_larger_than_vocab`、`token_logprob_f32_input_and_guards` 三项全绿。若你的机器没有 Rust 工具链或编译失败，本步骤「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：为什么泛型约束是 `Copy` 而不是 `Clone`？

**答案**：函数体里的遍历写法是 `for &v in row`——按引用模式解构出值，要求元素可按位复制。`Copy` 是比 `Clone` 更弱（更小）的约束：`f32` 和 `bf16` 都满足 `Copy`，而用 `Clone` 会允许调用方传入带副作用/堆内存的类型，对这个纯数学函数毫无必要。约束越小，能喂进来的类型越多。

**练习 2**：`picked = u32::MAX`、词表 151,936 时会发生什么？

**答案**：不 panic。`picked as usize = 4294967295 >= row.len()`，命中 [src/lib.rs:L478-L480](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/src/lib.rs#L478-L480) 的守卫返回 `None`，调用方（如 qwen35 executor 里 `and_then` 链）得到 `None` 即「这条请求没有 logprob」。

**练习 3**：bf16 词表里最大值被 3 个 token 并列，取其中任意一个，rank 是多少？

**答案**：3。rank 统计的是「logit **大于等于** picked 的 token 个数，含并列、含自己」（vLLM 约定，[pegainfer-frontend/src/engine/event.rs:L6-L7](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-frontend/src/engine/event.rs#L6-L7)），三个并列最大值互相都「>=」，所以 rank = 3。

### 4.2 log-softmax 与 f64 累加：16 万宽词表为什么不能用 f32 求和

#### 4.2.1 概念说明

logprob 的全部数学就是一行：\( \log p_{\text{picked}} = x_{\text{picked}} - \mathrm{LSE}(x) \)。难点全在数值上。

三步技巧，每步都有明确理由：

1. **max 平移**：先求 \( m = \max_j x_j \)，计算 \( e^{x_j - m} \)。平移后各项 \( \in (0, 1] \)，且至少一项等于 1，因此 \( \text{sum} \in [1, V] \)，\( \ln(\text{sum}) \in [0, \ln V] \)——求和与取对数都落在条件良好的区间，不会上溢（直接算 \( e^{x_j} \)，logit 到 90 就爆 f32 了）也不会全下溢。
2. **f64 累加**：求和在 f64 里做。这是本讲的精度核心，下面用误差传播论证。
3. **f32 收口**：最终 `log_sum_exp = max + sum.ln() as f32`，回到契约的 f32——损失被压到最后一步的一次舍入里，约 \( 10^{-7} \) 量级，可接受。

**f32 累加为什么不行**：顺序累加 N 个正数，每一步加法都可能引入至多 \( \varepsilon \) 的相对舍入，最坏情形总相对误差约 \( (N-1)\varepsilon \)。代入两个数：

\[
(N-1)\,\varepsilon_{f32} \approx 151{,}936 \times 6\times 10^{-8} \approx 9\times 10^{-3}, \qquad
(N-1)\,\varepsilon_{f64} \approx 151{,}936 \times 1.1\times 10^{-16} \approx 1.7\times 10^{-11}
\]

f32 最坏情形下 sum 的相对误差可达千分之九；误差出现在求和对数里，logprob 直接偏 \( |\ln(1 \pm 0.009)| \approx 0.009 \) nats。即便按随机游走估计 \( \sqrt{N}\varepsilon \approx 390 \times 6\times10^{-8} \approx 2.3\times 10^{-5} \)，也已经和 GPU A/B 测试的对照容差 `TOL = 5e-5`（[tests/gpu_logprobs.rs:L13](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/tests/gpu_logprobs.rs#L13)）同一量级——主机参考实现是全系统的锚点，它自己先抖起来，A/B 对照就失去了意义。f64 累加把这部分压到 \( 10^{-11} \)，彻底出局。

顺带一提，源码文档注释用的表述就是这条论证的浓缩版："Exponentials accumulate in f64 — summing a 160k-wide vocab in f32 loses precision"（[src/lib.rs:L468-L469](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/src/lib.rs#L468-L469)）。

#### 4.2.2 核心流程

```text
阶段 1（一遍 O(V)，与 4.3 的 rank 共享）：
    max = max_j x_j

阶段 2（一遍 O(V)）：
    sum = 0 (f64)
    for j in 0..V:
        sum += f64( exp( f32(x_j − max) ) )     # 平移在 f32 做（输入本来就是 f32/bf16），
                                                 # 指数与累加在 f64 做
收口：
    LSE = f32( max + ln(sum) )
    logprob(picked) = x_picked − LSE
```

注意阶段 2 里 `x − max` 这一步减法留在 f32：两个 f32 的减法自身只有一次 ulp 级舍入，且设备端核做同样的平移，保持两侧一致；**随 V 增长的误差源只有累加**，所以只把累加（和指数）升级到 f64。

#### 4.2.3 源码精读

```rust
let picked_val: f32 = row[picked].into();
let mut rank = 0;
let mut max = f32::NEG_INFINITY;
for &v in row {
    let value = v.into();
    max = max.max(value);
    rank += u32::from(value >= picked_val);
}
let mut sum = 0f64;
for &v in row {
    let x: f32 = v.into();
    sum += f64::from(x - max).exp();
}
let log_sum_exp = max + sum.ln() as f32;
```

- [src/lib.rs:L482-L489](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/src/lib.rs#L482-L489)：第一遍 O(V) 同时做两件事——`max` 用 `f32::NEG_INFINITY` 作初值（空行已被守卫挡掉，所以 max 一定被真实值覆盖），`rank` 用 `value >= picked_val` 计数。**注意比较的是原始 logit**，详见 4.3 的时点论证。
- [src/lib.rs:L490-L494](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/src/lib.rs#L490-L494)：第二遍 O(V) 的 f64 指数和。`x - max` 是 f32 减法，`f64::from(...)` 无损加宽，`.exp()` 与 `+=` 都在 f64 域。
- [src/lib.rs:L495](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/src/lib.rs#L495)：收口一行——f64 的 `sum.ln()` 先 `as f32`，再与 f32 的 `max` 相加，得到 f32 的 LSE。
- 最终 `logprob: picked_val - log_sum_exp` 在 [src/lib.rs:L515-L519](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/src/lib.rs#L515-L519) 的返回处组装——用第一遍循环里存下的**原始** `picked_val`，与 top-k 是否包含 picked 无关（k=0 时 logprob 依然正确）。

单元测试如何把这条数学钉死：

- [src/lib.rs:L657-L678](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/src/lib.rs#L657-L678)：`token_logprob_matches_exact_log_softmax` 选的输入 `[1.0, 3.0, 2.0, 0.0, 3.0]` 全部是 bf16 可精确表示的值，于是期望值可以**解析**写出（`let lse = (e^1 + e^3 + e^2 + 1 + e^3).ln()`，在测试里用 f64 直接算），再断言函数输出与解析值误差 < 1e-6。这就是「参考实现」的验证风格：不用另一个实现当标准，用数学当标准。
- [src/lib.rs:L680-L693](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/src/lib.rs#L680-L693)：`token_logprob_k_larger_than_vocab` 顺手验证了 softmax 归一性：全词表 logprob 取指数求和应等于 1（误差 < 1e-6）——这是 LSE 数学正确性的另一面。

#### 4.2.4 代码实践

1. **实践目标**：亲眼看到 f32 与 f64 累加在真实词表宽度下的差距，而不只是看公式。
2. **操作步骤**：把下面这段独立小程序存成 `sum_prec.rs`（放在讲义目录或任意临时目录，**不要**放进仓库源码树），编译运行：

   ```rust
   // 示例代码：独立小程序，与项目无关。
   // rustc -O sum_prec.rs -o sum_prec && ./sum_prec
   fn main() {
       let vocab = 151_936usize; // Qwen3 词表宽度，取自 tests/gpu_logprobs.rs
       // 温和峰形的合成 logits：绝大多数远低于 max，模拟真实分布。
       let logits: Vec<f32> = (0..vocab)
           .map(|i| (i as f32 * 0.0007).sin() * 8.0 - 10.0)
           .collect();
       let max = logits.iter().cloned().fold(f32::NEG_INFINITY, f32::max);
       let sum32: f32 = logits.iter().map(|&x| (x - max).exp()).sum();
       let sum64: f64 = logits.iter().map(|&x| f64::from(x - max).exp()).sum();
       println!("f32 sum = {sum32:.9}");
       println!("f64 sum = {sum64:.9}");
       println!("rel diff        = {:.3e}", (f64::from(sum32) - sum64).abs() / sum64);
       println!("logprob 偏移(nats) = {:.3e}", (f64::from(sum32) / sum64).ln().abs());
   }
   ```

3. **需要观察的现象**：`rel diff` 落在 \( 10^{-5} \sim 10^{-4} \) 量级（介于随机游走估计 2.3e-5 与最坏上界 9e-3 之间，具体值取决于舍入的随机走向）；换几组合成分布多跑几次，偏差会变但量级稳定。
4. **预期结果**：logprob 偏移与 GPU A/B 容差 5e-5 同量级或更大——印证「f32 累加会吃掉对照容差」。具体数字「待本地验证」（不同 CPU、不同分布会给出不同值，这正是要点：f32 累加的误差不可控）。

#### 4.2.5 小练习与答案

**练习 1**：如果不做 max 平移，直接算 \( \sum_j e^{x_j} \)，什么会先出问题？

**答案**：上溢。\( e^{x} \) 在 f32 里超过 \( 3.4\times 10^{38} \) 就变成 `inf`，这只要求 \( x \gtrsim 88.7 \)——大词表 logits 的绝对值很容易超过。平移后每项 \( \le 1 \)，和 \( \le V \)，天然安全；同时取对数后的 \( \ln(\text{sum}) \) 被压在 \( [0, \ln V] \)（约 12），条件数良好。

**练习 2**：为什么 `x - max` 留在 f32、不一起升级到 f64？

**答案**：两个原因。其一，输入 `x` 与 `max` 本来就是 f32（bf16 喂入时也是先转 f32），f32 减法只有一次 ulp 级舍入，且这一步的误差**不随 V 增长**——每个元素独立平移一次；随 V 增长的唯一误差源是 N 项累加，所以只有累加需要 f64。其二，设备端核（`token_logprobs_batch` 背后的 kernel）对 bf16 输入做的是同样的 f32 平移，主机参考与设备实现在这一步保持同构，A/B 对照才收敛到 5e-5 容差内。

**练习 3**：估算：词表翻倍到 32 万，f32 顺序累加的最坏相对误差变成多少？

**答案**：约 \( 1.8\times 10^{-2} \)——误差与项数 N 线性增长（\( (N-1)\varepsilon \)），词表翻倍最坏误差翻倍。f64 对应值约 \( 3.4\times 10^{-11} \)，仍然彻底无关痛痒。这就是文档注释「160k-wide vocab」措辞的分量：宽度本身就是风险因子。

### 4.3 rank 与 top-k 提取：原始 logit 定名次，插入 + 截断维护前 k

#### 4.3.1 概念说明

**rank 的时点问题**。rank 定义为 \( |\{\,j : x_j \ge x_{\text{picked}}\,\}| \)。看似可以先算出所有 logprob 再比较 \( \text{lp}_j \ge \text{lp}_{\text{picked}} \)，但源码特意在**减 LSE 之前**、第一遍循环里就用原始 logit 比较，并留下注释 "Rank before subtracting LSE so f32 rounding cannot change raw-logit order"（[src/lib.rs:L497](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/src/lib.rs#L497)）。原因：浮点舍入是单调的，不会把「大于」变「小于」，但**会把「严格小于」塌缩成「相等」**——两个原始 logit 相差比如 \( 10^{-9} \)（f32 已分辨不开），各自减去同一个 f32 的 LSE 再各自舍入，可能落到同一个 f32 值上；此时用 `>=` 比较 logprob 会把那个本应更小的 token 也计入 rank，名次凭空 +1。用原始 logit 比较则不存在这个问题：输入里的相等就是真的相等（bf16 只有 8 位尾数，并列极常见），不等就是真的不等。

**top-k 的维护手法**。不求全排序（\( O(V \log V) \) 且要一份索引拷贝），而是维护一个长度至多 k 的降序表，一遍扫描插入：

- **快速跳过**：表满且新值 `<=` 表尾（第 k 名）→ 直接 `continue`。真实分布里绝大多数 logit 远低于前 k 名，这个分支消化了几乎全部元素。
- **二分定位**：`partition_point` 在降序表上找到第一个「值小于新值」的位置——由于表按值降序、同值按 id 升序，这个位置恰好让新条目排在所有「值 >= 它」的条目**之后**。
- **插入 + 截断**：`insert` 后表长最多 k+1，`truncate(k)` 挤掉最小者。

**并列的稳定序**。因为外层按 id 从 0 递增遍历，且相等值插入在既有相等值之后，并列 token 在 top 表里天然按 **id 升序**排列。这不是偶然的副作用而是被测试钉死的行为契约（`assert_eq!(ids, vec![1, 4, 2])`——两个并列的 3.0，id 1 排在 id 4 前面）。

复杂度：每个未跳过的元素付出 \( O(\log k) \) 的二分加 \( O(k) \) 的插入搬移，整体最坏 \( O(V \cdot k) \)。k 是请求参数（通常个位数到几十），远优于全排序。

#### 4.3.2 核心流程

```text
k = min(top_k, V)                      # k 先夹到词表宽度
top = Vec::with_capacity(k + 1)        # 插入+截断最多瞬时到 k+1，预分配避免循环内重分配
                                      # 不变量：top 按值降序、同值按 id 升序，长度 <= k

for id in 0..V:
    val = f32(row[id])
    if top.len() == k and val <= top[k-1].值:   # 表满且进不了前 k → 跳过
        continue                                #（并列于第 k 名时，先来的 incumbent 留任）
    pos = top.partition_point(|(_, kept)| kept >= val)  # 二分：值 >= val 的前缀长度
    top.insert(pos, (id, val))                  # 落在所有 >= val（含相等）的条目之后
    top.truncate(k)                             # 挤掉最小

for 条目 in top: 条目.值 -= LSE                 # 排完序才减 LSE —— 与 rank 同一条纪律
```

与 rank 的对称性：**排序用原始 logit，报告值最后才减 LSE**。顺序语义（谁进前 k、谁在前）由原始 logit 决定，f32 舍入无从插手；减 LSE 只改变报告的数值，不改变顺序。

#### 4.3.3 源码精读

```rust
// Rank before subtracting LSE so f32 rounding cannot change raw-logit order.
let k = top_k.min(row.len());
let mut top: Vec<(u32, f32)> = Vec::with_capacity(k + 1);
if k > 0 {
    for (id, &v) in row.iter().enumerate() {
        let val: f32 = v.into();
        if top.len() == k && val <= top[k - 1].1 {
            continue;
        }
        let pos = top.partition_point(|&(_, kept)| kept >= val);
        top.insert(pos, (id as u32, val));
        top.truncate(k);
    }
    for entry in &mut top {
        entry.1 -= log_sum_exp;
    }
}
```

- [src/lib.rs:L497-L499](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/src/lib.rs#L497-L499)：注释（rank 纪律）、`k = top_k.min(row.len())` 把 k 夹到词表宽度（对应测试 `token_logprob_k_larger_than_vocab` 里 `top_k=32`、词表 2，返回长度 2）、容量 `k + 1`。
- [src/lib.rs:L500-L509](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/src/lib.rs#L500-L509)：插入 + 截断主体。跳过条件用 `<=`（不是 `<`）：表满时与第 k 名**并列**的新值直接跳过，先到者（更小的 id）留任——这正是并列按 id 升序的另一半保证。`partition_point` 的谓词 `kept >= val` 返回「值 >= val 的前缀长度」，即新条目的插入下标。
- [src/lib.rs:L510-L512](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/src/lib.rs#L510-L512)：排序完成后统一减 LSE。整个 `if k > 0` 块在 k=0 时整体跳过——top_logprobs 为空，但外层的 `logprob`/`rank` 照常计算（它们在第一、二遍循环里就完成了）。
- [src/lib.rs:L515-L519](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/src/lib.rs#L515-L519)：返回 `TokenLogprob { logprob: picked_val - log_sum_exp, rank, top_logprobs: top }`。

用既有测试的输入手工走一遍状态演进（`[1.0, 3.0, 2.0, 0.0, 3.0]`，k=3）：

| 处理 id | val | 动作 | top（值降序，同值 id 升序） |
|---|---|---|---|
| 0 | 1.0 | 插入 | `[(0, 1.0)]` |
| 1 | 3.0 | 插入队首 | `[(1, 3.0), (0, 1.0)]` |
| 2 | 2.0 | 插入中间 | `[(1, 3.0), (2, 2.0), (0, 1.0)]` |
| 3 | 0.0 | 表满且 0.0 ≤ 1.0 → 跳过 | 不变 |
| 4 | 3.0 | 表满但 3.0 > 2.0；`partition_point` 返回 1（前缀 `[(1,3.0)]` 的值都 ≥ 3.0），插到 id 1 之后，截断挤掉 `(0, 1.0)` | `[(1, 3.0), (4, 3.0), (2, 2.0)]` |

结果 ids = `[1, 4, 2]`，与 [src/lib.rs:L672-L674](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/src/lib.rs#L672-L674) 的断言完全一致；rank 断言（picked=2 → 3；picked=0 → 4；picked=1 → 2，见 [src/lib.rs:L668-L670](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/src/lib.rs#L668-L670)）也都来自同一条 `>=` 计数。

#### 4.3.4 代码实践

1. **实践目标**：不运行代码，仅凭源码逻辑手工执行插入 + 截断算法，验证自己对谓词与稳定序的理解。
2. **操作步骤**：取行 `[0.5, -1.0, 2.0, 2.0, 0.5, 3.0]`（六元素，三处并列），`top_k = 3`，`picked = 5`。逐 id 写出每一步 `top` 的内容、跳过分支是否触发、`partition_point` 返回值；最后写出返回的 `top_logprobs`（id 顺序）与 `rank`。再算 `top_k = 6`（k > 并列宽度）与 `top_k = 0` 两种变体。
3. **需要观察的现象**：id 3 的 2.0 会插到 id 2 的 2.0 之后而不是之前；id 4 的 0.5 在 k=3 时被跳过（0.5 ≤ 表尾 2.0），在 k=6 时能进表并排在 id 0 的 0.5 之后。
4. **预期结果**：k=3 时 ids = `[5, 2, 3]`、rank = 1（3.0 唯一最大）；k=6 时 ids = `[5, 2, 3, 0, 4, 1]`；k=0 时 `top_logprobs` 为空但 rank 仍为 1、logprob 仍等于 \( 3.0 - \mathrm{LSE} \)。可用既有测试 `token_logprob_k_larger_than_vocab`（[src/lib.rs:L680-L693](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/src/lib.rs#L680-L693)）的归一性断言自检：六项 logprob 取指数求和应等于 1。

#### 4.3.5 小练习与答案

**练习 1**：跳过条件为什么用 `val <= top[k-1].1` 而不是 `val < top[k-1].1`？

**答案**：若用 `<`，表满时与第 k 名**并列**的新值会走插入路径——插到并列者之后再被 `truncate` 挤掉，结果虽然相同（incumbent 留任），却白白付出一次 O(k) 搬移；更重要的是语义显式化：「先到的并列者（id 更小）获胜」。用 `<=` 一行同时买到正确性与快速路径。

**练习 2**：`partition_point(|&(_, kept)| kept >= val)` 在表 `[(1, 3.0), (4, 3.0), (2, 2.0)]`、`val = 3.0` 时返回什么？为什么这个返回值恰好保证并列按 id 升序？

**答案**：返回 2——前缀 `[(1, 3.0), (4, 3.0)]` 的值都满足 `>= 3.0`，`(2, 2.0)` 不满足。新条目 (5, 3.0) 插到下标 2，落在两个 3.0 之后。因为外层按 id 升序遍历，任何后到的相等值永远插在先到的相等值之后，归纳可得同值组内 id 严格升序。

**练习 3**：这个算法最坏时间复杂度是多少？什么时候会真正达到？

**答案**：\( O(V \cdot k) \)——V 个元素每个最多一次 \( O(\log k) \) 二分加一次 \( O(k) \) 的 `Vec::insert` 搬移。真正达到要求几乎每个新值都能挤进前 k（比如完全随机的 logits 且 k 较大时）。真实解码步的分布高度尖峰，绝大多数元素命中 `continue` 快速跳过，实际行为接近 \( O(V) \)。

## 5. 综合实践

把本讲三个模块（泛型输入、LSE 数学、rank/top-k 语义）串成一个任务：**先手算，再写测试钉死**。

### 第一步：手算一行 5 个 logit

取行 `[-2.0, 0.5, 1.25, -0.75, 1.25]`（五个值全部 bf16 可精确表示，与既有测试的选值纪律一致），`picked = 2`（值 1.25，与 id 4 并列最大），`top_k = 3`。

按 4.2 的三步法：max = 1.25；平移后求和（用 f64）：

| id | logit | \( x - m \) | \( e^{x-m} \) |
|----|-------|-------------|----------------|
| 0 | −2.0 | −3.25 | 0.038774 |
| 1 | 0.5 | −0.75 | 0.472367 |
| 2 | 1.25 | 0 | 1.0 |
| 3 | −0.75 | −2.0 | 0.135335 |
| 4 | 1.25 | 0 | 1.0 |

\[
\text{sum} = 2.646476,\qquad \ln(\text{sum}) = 0.973229,\qquad \mathrm{LSE} = 1.25 + 0.973229 = 2.223229
\]

于是：

- `logprob(picked=2) = 1.25 − 2.223229 = −0.973229`（即概率 \( e^{-0.973229} = 1/2.646476 \approx 0.3779 \)）；
- `rank(picked=2) = |{x >= 1.25}| = |{id2, id4}| = 2`；若 `picked = 1`（0.5），rank = |{0.5, 1.25, 1.25}| = 3，logprob = −1.723229；
- `top_logprobs(k=3)`：值降序、同值 id 升序 → `[(2, −0.973229), (4, −0.973229), (1, −1.723229)]`；
- `top_k = 0` 时 `top_logprobs = []`，但 logprob 仍是 −0.973229、rank 仍是 2。

### 第二步：仿照既有测试写一个新单元测试

仿照 [src/lib.rs:L657-L678](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/src/lib.rs#L657-L678) 的 `token_logprob_matches_exact_log_softmax`，在**本地克隆**的 `src/lib.rs` 测试模块（这是仅测试代码的学习性修改，不要提交）中加入：

```rust
// 示例代码：建议新增的单元测试（写入本地克隆的 mod tests 内，仅作学习用途）。
#[test]
fn token_logprob_negative_and_tied_maxima() {
    // 含负值与并列最大值，全部 bf16 可精确表示，期望值可解析求出。
    let row: Vec<bf16> = [-2.0f32, 0.5, 1.25, -0.75, 1.25]
        .iter()
        .map(|&v| bf16::from_f32(v))
        .collect();
    // f64 参考：max = 1.25，sum = 2·e^0 + e^-0.75 + e^-2 + e^-3.25
    let lse = (2.0 + (-0.75f64).exp() + (-2.0f64).exp() + (-3.25f64).exp()).ln() as f32;

    let out = token_logprob_from_row(&row, 2, 3).unwrap();

    // (1) picked 的 logprob 与手算 f64 参考一致（误差 < 1e-6）。
    assert!((out.logprob - (1.25 - lse)).abs() < 1e-6);
    // 并列最大：两个 1.25 都计入 rank；次大 token id=1 的 rank 为 3。
    assert_eq!(out.rank, 2);
    assert_eq!(token_logprob_from_row(&row, 1, 0).unwrap().rank, 3);
    // (2) 并列 token 在 top_logprobs 中按 id 升序（2 在 4 之前）。
    let ids: Vec<u32> = out.top_logprobs.iter().map(|&(id, _)| id).collect();
    assert_eq!(ids, vec![2, 4, 1]);
    // (3) k=0：top_logprobs 为空，但 logprob / rank 仍然正确。
    let bare = token_logprob_from_row(&row, 2, 0).unwrap();
    assert!(bare.top_logprobs.is_empty());
    assert_eq!(bare.rank, 2);
    assert!((bare.logprob - (1.25 - lse)).abs() < 1e-6);
}
```

运行 `cargo test --release --lib token_logprob_negative_and_tied_maxima`。此测试不需要 GPU。如果你手算的第一步与断言不一致，先检查自己的 LSE 再怀疑代码——这正是「参考实现 + 解析期望值」的开发方式。运行结果「待本地验证」。

## 6. 本讲小结

- `token_logprob_from_row` 是 logprob 通道的**主机参考实现**：两遍 O(V) 加一个 O(V·k) 的 top-k 扫描，只在请求方要求 logprob 时运行；它同时是 GPU 批式孪生（u3-l2）的对照标准。
- 数学骨架是 log-softmax \( \log p_{picked} = x_{picked} - \mathrm{LSE} \)：max 平移防溢出、把和压进 \([1, V]\)，f64 累加把随词表宽度线性增长的求和误差从最坏 \( \sim 10^{-2} \) 压到 \( \sim 10^{-11} \)。
- **顺序语义一律用原始 logit**：rank 在减 LSE 之前计数，top-k 排完序才统一减 LSE——f32 舍入可以把「严格小于」塌缩成「相等」却不能反转大小，用原始值比较让舍入无从制造假并列。
- top-k 用「快速跳过 + partition_point 二分定位 + 插入 + 截断」维护，最坏 \( O(V \cdot k) \)；并列按 token id 升序是被测试钉死的稳定契约（`[1, 4, 2]`）。
- 一个泛型约束 `T: Copy + Into<f32>` 让 Qwen 喂 f32、Kimi 喂 bf16 共用同一入口，省掉 16 万元素的主机加宽拷贝；行必须是**未分片的全词表**（#236）。
- 防御式守卫：空行或 picked 越界返回 `None` 而不是 panic；k 被夹到词表宽度。

## 7. 下一步学习建议

下一讲 **u3-l2「token_logprobs_batch：设备端批量孪生与它的防护栏」**会把本讲的数学搬到 GPU 上：一次 `logprob_topk_batch_bf16_into` launch 处理整批请求，输入三组 i32 缓冲、输出五组缓冲按 `i*k_max + j` 布局回读。带着本讲的三条纪律去读它，你会处处看到对应物——设备核同样按原始 logit 定 rank 与顺序（所以 A/B 的 ids 断言是**精确相等**、数值断言才用 5e-5 容差，见 [tests/gpu_logprobs.rs:L37-L55](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/tests/gpu_logprobs.rs#L37-L55)）。建议先通读该测试的 `assert_matches_host`，再进 `token_logprobs_batch`。之后可以延伸阅读 `pegainfer-kernels` 中 `logprob_topk_batch_bf16_into` 的 FFI 定义，看设备端的 f64/归约策略与主机参考如何对齐。
