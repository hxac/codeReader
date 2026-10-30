# u2-l4：mix_seed 与确定性：seeded 行为什么必须单独调用

## 1. 本讲目标

学完本讲，你应该能够：

1. 逐行读懂 `mix_seed` 的 SplitMix64 实现，说出三个魔法常数各自的角色（黄金比例 γ 预扩散、两轮乘法-异或移位终结器）。
2. 解释 philox 这类计数器式随机数发生器的 (seed, subsequence, offset) 三元组结构，以及 FlashInfer 采样核为什么把「批内位置」折进子序列——这是 seeded 行必须单行调用的根本原因。
3. 复述可重放契约：一个带种子的行，其 token 是 (seed, step, 分布) 的**纯函数**，与批组合、邻居、引擎种子全部无关。
4. 说清两级种子的分工：引擎级种子每步新鲜（步间去相关、行间靠子序列去相关），请求级种子按 (seed, step) 混合换流（跨批组合可重放）。

本讲是单元二「select_batch 主链路」的收尾：u2-l3 已经走完了三路分发的流程，本讲把第三路（seeded 单行调用）背后的**种子体系**挖到底。

## 2. 前置知识

### 2.1 伪随机数发生器（PRNG）与「流」

PRNG 是一台确定性状态机：给定初始状态（种子），它产出一条完全确定的数列。我们说「一条流」，指的就是**由某个种子决定的那条确定序列**。两个调用拿到同一条流，就会得到相同的「随机」数——这不是缺陷，而是可重放（replay）的基础。

### 2.2 计数器式 PRNG：philox

传统 PRNG（如线性同余）必须按序推进内部状态，天然串行。GPU 上几万个线程不能共享一个状态机，所以 CUDA 生态普遍用**计数器式**发生器，philox 是代表：

\[ \text{output} = \phi^{\,offset}\big(\text{counter},\ \text{key}\big) \]

- **key**（密钥，通常就是种子）：选择「哪一本密码本」；
- **counter**（计数器）：翻到第几页；
- **offset**：本页内跳过前几个数。

它没有需要保护的可变状态——任何一个 (key, counter, offset) 组合都可以独立计算，因此完美并行。cuRAND 的 `curand_init(seed, subsequence, offset)` 就是这个三元组的经典封装：subsequence 决定计数器的高位段（选流），offset 决定流内偏移。FlashInfer 的采样核沿用同一约定。

**子序列（subsequence）是本讲的主角**：它就是「这条流是第几条」的编号。

### 2.3 SplitMix64 与雪崩

SplitMix64 是一个 64 位混合函数：输入一个 64 位数，输出一个「看起来随机」的 64 位数。它的招牌性质是**雪崩（avalanche）**：输入哪怕只翻转 1 位，输出平均约有一半的位（期望 32/64 位）翻转。形式化地说，对输出的任意一位，

\[ P(\text{该位翻转}) \approx \tfrac{1}{2} \]

好的混合函数把「数值上接近」的输入（如 step=5 与 step=6）映射成「统计独立」的输出——这正是把相邻解码步变成独立随机流所需要的。

### 2.4 一个关键字：0x9E3779B97F4A7C15

这个常数是 \( \gamma \approx 2^{64}/\varphi \) 的整数截断（\(\varphi\) 为黄金比例），俗称「黄金伽马」。用它做乘法（Weyl 序列 / Fibonacci 哈希）可以把连续整数 0, 1, 2, … 均匀地散布在 64 位空间里：相邻两步的差恒为 γ（一个与 \(2^{64}\) 互素的奇数），而不是 1。记住它——本讲会在两个不同的 crate 里遇到它。

## 3. 本讲源码地图

| 文件 | 在本讲中的角色 |
|---|---|
| `pegainfer-sample/src/lib.rs` | `mix_seed` 的定义与文档（L436-446）；`select_batch` 的种子契约文档（L179-188）与 seeded 单行循环（L296-318） |
| `pegainfer-sample/tests/select_batch.rs` | `seeded_rows_replay_independent_of_batch_position`（L325-371，本讲的核心回归测试）；`sampling_is_seed_deterministic_and_actually_samples`（L117-140，引擎种子侧） |
| `pegainfer-kernels/src/ops/sampling.rs` | `gpu_sample_batch_into` 的种子传递；min_p 第二趟的「换钥匙」（L107-110）；FFI 调用里的标量 seed 与 offset=0（L251-252） |
| `pegainfer-kernels/csrc/shared/flashinfer_sampling.cu` | CUDA 侧的一手注释：「一个种子整批共用、子序列=行号」（L44-46）与「per-request 种子刻意不走 seed_arr」（L62-68） |
| `pegainfer-frontend/src/sampler.rs` | `SamplingParams.seed` 字段的两级承诺措辞（L10-14） |
| `pegainfer-qwen3/src/scheduler/plan.rs` | 引擎级种子「每步新鲜」的真实实现：每个执行计划抽取一个全新 `sample_seed`（L104/L114/L131/L155） |

## 4. 核心概念与源码讲解

### 4.1 SplitMix64 `mix_seed`：把 (seed, step) 混成一个 philox 种子

#### 4.1.1 概念说明

一个带种子的请求要生成 100 个 token，就要做 100 次采样，每次都要一条随机流。最偷懒的做法是每步都用 `philox(key = request_seed, subsequence = 0)`——但这样每一步都重放**同一条流**：每步的第一个均匀数 \(u_0\) 都相同。采样是「拿 \(u\) 与累积分布比较」，自回归生成里每步的分布都在变，同一个 \(u_0\) 序列反复使用会造成系统性偏差（例如永远倾向分位数相同的那个位置）。

所以必须把「步数」也掺进种子里：每一步换一条新流。但怎么掺？

- `seed + step` 或 `seed ^ step`：相邻 step 只差几个低位。philox 对相邻 key 的表现没有「保证独立」的承诺，把宝押在下游发生器上是脆弱的。
- `mix_seed`：先用黄金伽马把 step 摊开，再跑一遍 SplitMix64 终结器，**从构造上保证**雪崩——相邻 (seed, step) 对拿到统计独立的流，不依赖 philox 的任何性质。

这是防御性工程：混合层的职责就是把「数值接近的输入」变成「毫无结构的种子」。

顺带一提，它还解决了另一个问题：两个不同请求碰巧拿到同一个种子时，如果每步都从 `(seed, 0)` 起步，它们会逐 token 互为重放；混入各自的 step 后至少各步独立（是否同流还取决于各自的请求内步数——请求级种子的唯一性本来就是调用方的责任）。

#### 4.1.2 核心流程

`mix_seed(seed, step)` 的执行过程：

```text
z₀ = seed ^ (step × γ)          # γ = 0x9E3779B97F4A7C15（黄金伽马）
z₁ = (z₀ ^ (z₀ >> 30)) × M₁     # M₁ = 0xBF58476D1CE4E5B9
z₂ = (z₁ ^ (z₁ >> 27)) × M₂     # M₂ = 0x94D049BB133111EB
输出 = z₂ ^ (z₂ >> 31)           # 收尾 xorshift
```

分步解读：

1. **预扩散**：`step.wrapping_mul(γ)` 把 0, 1, 2, … 映射成彼此相距 γ 的散点（Weyl 序列）；再与 seed 异或，让不同请求的轨道错开。此时 step 的信息只在低位附近，还需要终结器。
2. **两轮「右移异或 × 常数乘」**：每轮先把高位混进低位（`z ^ (z >> s)`），再用奇常数乘法把低位信息推回高位。两轮之后，输入任意一位的差异都被扩散到全部 64 位。
3. **收尾 xorshift**：最后一次移位异或平衡输出位分布。

这就是标准的 SplitMix64 终结器（finalizer）——原版 SplitMix64（Steele、Lea、Flood, 2014）内部推进状态用的正是这段代码。此处没有状态、只有混合：它把 (seed, step) 这个「结构化输入」一次性打成良分布种子。

#### 4.1.3 源码精读

先看文档注释——它声明了这个函数的契约与存在理由：

> SplitMix64 over (seed, step): a distinct, well-mixed philox seed per request step, deterministic across runs and batch layouts. Public for the models that drive their own greedy path (see the module docs) and must reproduce `select_batch`'s seeded-row semantics: mix the request seed with the request-local step, one single-row call per seeded row.

[mix_seed 的文档与实现：src/lib.rs:L436-L446](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/src/lib.rs#L436-L446) ——整个函数只有 6 行：黄金伽马预混合加两轮终结器。注意它是 `pub` 的：像 Kimi-K2 这种自驱贪心路径的模型，若要复刻 seeded 行的语义，必须用**同一个**混合函数，否则同种子同步数会得到不同 token（跨 crate 的行为一致性靠 `pub` + 文档钉住）。

三行主体与常数一一对应：

```rust
pub fn mix_seed(seed: u64, step: u64) -> u64 {
    let mut z = seed ^ step.wrapping_mul(0x9E37_79B9_7F4A_7C15);
    z = (z ^ (z >> 30)).wrapping_mul(0xBF58_476D_1CE4_E5B9);
    z = (z ^ (z >> 27)).wrapping_mul(0x94D0_49BB_1331_11EB);
    z ^ (z >> 31)
}
```

（[src/lib.rs:L441-L446](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/src/lib.rs#L441-L446)，截取自上一链接范围。）

黄金伽马在本仓库里还有第二份差事——kernels 层为 min_p 第二趟采样派生「另一把钥匙」用的正是同一个常数：

> Distinct philox key for the second pass: both passes restart their subsequences at 0, so reusing `seed` would hand minp row i the same uniform stream as plain row i and correlate their tokens.

[pegainfer-kernels/src/ops/sampling.rs:L107-L110](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-kernels/src/ops/sampling.rs#L107-L110) ——`let minp_seed = seed ^ 0x9E37_79B9_7F4A_7C15;`。同一个「黄金比例 = 去相关」的团队直觉在两处独立出现：一处把 step 从种子中分离，一处把两趟采样的流分离。这段代码同时是一条重要证据，4.2 节会用到（「两次调用的子序列都从 0 重启」）。

#### 4.1.4 代码实践

**实践目标**：亲眼看到雪崩——相邻输入的输出汉明距离集中在 32 附近。

**操作步骤**：

1. 把下面的程序存成独立文件（示例代码，不依赖仓库，纯 CPU）：

```rust
// 示例代码：mix_seed_avalanche.rs —— 从 src/lib.rs 原样复制的 mix_seed
fn mix_seed(seed: u64, step: u64) -> u64 {
    let mut z = seed ^ step.wrapping_mul(0x9E37_79B9_7F4A_7C15);
    z = (z ^ (z >> 30)).wrapping_mul(0xBF58_476D_1CE4_E5B9);
    z = (z ^ (z >> 27)).wrapping_mul(0x94D0_49BB_1331_11EB);
    z ^ (z >> 31)
}

fn main() {
    let base = mix_seed(7, 5);
    println!("mix_seed(7,5) = {base:#018x}");
    println!("mix_seed(7,6) = {:#018x}", mix_seed(7, 6));
    println!("mix_seed(8,5) = {:#018x}", mix_seed(8, 5));
    for (s, t) in [(7u64, 6u64), (8, 5)] {
        let d = (base ^ mix_seed(s, t)).count_ones();
        println!("hamming(mix(7,5), mix({s},{t})) = {d} bits");
    }
}
```

2. 编译运行：`rustc mix_seed_avalanche.rs && ./mix_seed_avalanche`。
3. 把 `(7, 6)` 换成 `(7, 7)`、`(100, 5)` 等多试几对，记录汉明距离。

**需要观察的现象**：三个输出十六进制看起来毫无规律；任意两对之间的汉明距离在 32 附近波动（通常落在 24~40 区间），而不是 1 或 2。

**预期结果**：距离期望为 \(64 \times \tfrac{1}{2} = 32\)，标准差 \( \sqrt{64 \times \tfrac14} = 4 \)。这是统计现象，具体数值待本地验证；若某对距离持续个位数，说明混合出了问题（不应发生）。

#### 4.1.5 小练习与答案

**练习 1**：为什么预扩散用 `step.wrapping_mul(γ)` 而不是 `seed + step`？

**答案**：`seed + step` 让相邻 step 的候选种子只差 1（低位一个比特的进位链），它们是否对应独立随机流完全取决于下游 philox 对相邻 key 的行为——没有契约保证。乘以黄金伽马后相邻 step 相差一个满 64 位的无理比例常数，再经终结器雪崩，独立性由构造保证。另外 `seed ^ step` 更糟：step 的低位直接透传。

**练习 2**：`mix_seed(7, 5)` 与 `mix_seed(5, 7)` 相等吗？

**答案**：函数对两个输入不对称（step 要先乘 γ 再异或，seed 直接异或），所以没有理由相等；作为良混合函数，二者碰巧相等的概率约 \(2^{-64}\)，实际可以认为必然不等。但注意契约只保证**相同输入相同输出**，并不保证交换律——不要写出依赖对称性的代码。

**练习 3**：文档说这个函数 `pub` 是给「自驱贪心路径的模型」用的。假设某模型只实现了贪心解码，为什么还需要 `mix_seed`？

**答案**：贪心路径本身不消耗随机数，但该模型仍可能收到带 `seed` 的非贪心请求（如 Kimi-K2 的非贪心行就路由回本 crate 的通道）。只要它想在任何路径上复刻 `select_batch` 的 seeded 行为——同 (seed, step) 出同 token——就必须用同一个混合函数与同一种单行调用方式，否则跨路径的语义就会分叉。

### 4.2 philox 子序列与批组合：seeded 行为什么不能搭批式调用

#### 4.2.1 概念说明

4.1 节解决了「seeded 行每步换什么种子」，本节解决「为什么它必须**单独**调用」——答案藏在 FlashInfer 批式采样核的随机流编排里。

批式采样核（`SamplingFromProb` / `TopPSamplingFromProb` / `TopKTopPSamplingFromProb` / `MinPSamplingFromProb`）把批里每一行分给一个 CUDA block。每个 block 需要一条自己的随机流，而 philox 恰好可以无状态地「按编号取流」：block 的编号 `blockIdx.x` 正好当作子序列。于是批式调用的流编排是：

\[ \text{流}(\text{第 } j \text{ 行}) = \text{philox}\big(\text{key} = \text{seed},\ \text{subsequence} = j\big) \]

关键在于：**种子是整批一个，行的身份完全由它在批内的物理位置（压缩下标）承载**。FlashInfer 的接口本有一个 `seed_arr`（逐行种子数组）参数，但这些核实际只读 `seed_arr[0]` 当作整批的标量种子——逐行种子通道形同虚设。

对无种子行这毫无问题：它的流本来就允许随批组合漂移（没有承诺过可重放）。但对带种子的行是致命的：同一个请求这步在批尾（j=2）、下步在批首（j=0），流就换了，token 跟着换——「token 是 (seed, step, 分布) 的纯函数」这个契约立刻破产。

解法在 Rust 层：seeded 行退出批式调用，自己发一次 `n_rows = 1` 的调用。单行调用的 grid 只有一个 block，`blockIdx.x` **恒为 0**——批内位置这个变量被物理消灭，流只由 `mix_seed(request_seed, step)` 决定。

#### 4.2.2 核心流程

两条路径的流编排对比：

```text
批式调用（无种子行，n 行）:
  key         = seed                 # 整批一个：引擎的本步种子
  subsequence = blockIdx.x ∈ [0, n)  # 行的压缩位置
  ⇒ 流(行 j) = philox(seed, j)       # 随批组合变化（无承诺，OK）

单行调用（seeded 行，n = 1）:
  key         = mix_seed(request_seed, step)
  subsequence = blockIdx.x = 0        # 恒 0
  ⇒ 流(行)   = philox(mix_seed(seed, step), 0)   # 纯函数，可重放
```

「子序列 = 调用内压缩位置」不是注释修辞，min_p 分区是最硬的证据：`gpu_sample_batch_into` 遇到混合批会拆成两次独立调用（plain 一趟、min_p 一趟），**两次调用的子序列都从 0 重新编号**。如果第二趟沿用同一个种子，plain 批的第 i 行和 min_p 批的第 i 行会拿到同一条流、产出相关的 token——所以才必须换钥匙（`seed ^ γ`）。同一行在批内的子序列编号取决于「哪些邻居被分到了哪一趟」，这正说明：**只要坐批式调用，你的流就由批组合决定**。

#### 4.2.3 源码精读

CUDA 侧的一手注释，先看流编排的总述：

> One philox seed per call; rows decorrelate through the philox subsequence (= row index), so the caller must supply a fresh seed per decode step.

[flashinfer_sampling.cu:L44-L46](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-kernels/csrc/shared/flashinfer_sampling.cu#L44-L46) ——括号里的「= row index」说破了子序列的物理含义：行号。

再看设计决策本身——为什么 per-request 种子不走 `seed_arr`：

> Per-request seeds deliberately do NOT go through FlashInfer's `seed_arr`: these kernels read `seed_arr[0]` (one seed for the whole batch) and fold `blockIdx.x` into the philox subsequence, so a request's stream would change with its position in the batch. Seeded rows are instead sampled as their own n_rows=1 calls by the Rust layer, with the request seed and step mixed into `seed` — blockIdx is then always 0 and replay is independent of batch composition.

[flashinfer_sampling.cu:L62-L68](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-kernels/csrc/shared/flashinfer_sampling.cu#L62-L68) ——这段注释把 4.2.1 的整条论证链写死了：读 `seed_arr[0]` → 位置进子序列 → 流随位置变 → 只能单行调用。

四处核 launch 印证了「没有逐行种子通道」：无论走哪条采样路径，`seed_arr` 实参都是 `nullptr`，种子走标量：

- min_p 管道的 [MinPSamplingFromProb，flashinfer_sampling.cu:L123-L127](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-kernels/csrc/shared/flashinfer_sampling.cu#L123-L127)
- 融合快路径的三种核（TopKTopP / TopP / 纯温度采样）[flashinfer_sampling.cu:L130-L145](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-kernels/csrc/shared/flashinfer_sampling.cu#L130-L145)

每一处都是 `/*seed_arr=*/nullptr, seed, /*offset_arr=*/nullptr, offset` 的形态。Rust 侧的 FFI 调用相应地只传一个标量 seed 和 offset=0：

[pegainfer-kernels/src/ops/sampling.rs:L251-L252](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-kernels/src/ops/sampling.rs#L251-L252) ——FFI 实参表末尾的 `seed, 0`：整个 Rust→CUDA 接口面上不存在逐行种子的位置。

最后回到本 crate，`select_batch` 的函数文档把这条约束写进了契约：

> `steps[i]` is row `i`'s request-local decode step. It only matters for rows whose params carry a `seed`: a seeded row is sampled as its own single-row call with `mix_seed(request_seed, step)` as the philox seed, so its tokens are a pure function of (seed, step, distribution) — FlashInfer's kernels fold the batch position into the philox subsequence, so seeded rows cannot ride the batched call without their stream changing with batch composition.

[src/lib.rs:L182-L188](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/src/lib.rs#L182-L188) ——策略层对内核层约束的忠实转述。

对应的实现就是 seeded 循环（u2-l3 已走过流程，这里只看种子语义）：

[src/lib.rs:L296-L318](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/src/lib.rs#L296-L318) ——循环顶部两个 `continue` 各自筛掉一类行：无 `p.seed` 的行交给上面的批式调用（L297-299）；`is_argmax` 的行已经走过贪心路径（L300-302，u2-l1 讲过：贪心不消耗随机数，种子对它无意义）。剩下的行构造**单元素**数组 `row`，以 `mix_seed(request_seed, steps[i])` 为种子调用 `gpu_sample_batch_into`（L310-316），结果直接落位 `tokens[i] = sampled[0]`（L317）。批内位置恒为 0 的保证不在 Rust 代码里，而在 `n=1 ⇒ grid 一维 ⇒ blockIdx.x == 0` 这个 CUDA 事实里——L293-295 的注释点明了它。

#### 4.2.4 代码实践

**实践目标**：沿着调用链亲手核实「逐行种子通道不存在」，并找出批式调用破坏重放的**第二个**原因。

**操作步骤**：

1. 打开 [pegainfer-kernels/src/ops/sampling.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-kernels/src/ops/sampling.rs)，在 `sample_uniform_batch_into` 的 FFI 调用里找到 `seed` 与 `offset` 两个实参（L251-252），确认它们是标量，且 `BatchSamplingRow` 结构体（L22-29）里没有任何 seed 字段。
2. 打开 [flashinfer_sampling.cu](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-kernels/csrc/shared/flashinfer_sampling.cu)，数一数 `/*seed_arr=*/nullptr` 出现的次数（应为 4 处，L126、L134、L139、L144），确认所有采样核都走标量种子。
3. 阅读混合批分区逻辑 [sampling.rs:L101-L111](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-kernels/src/ops/sampling.rs#L101-L111)，回答：**假设** FlashInfer 明天修复了 `seed_arr`（真的逐行读种子），seeded 行就能搭批式车了吗？

**需要观察的现象 / 预期结果**：第 3 问的答案是**仍不能**。分区逻辑表明：一行是否被拆进 min_p 第二趟，取决于**同批其他行**是否带 min_p（L101 的 `all(...)` 判断）；两趟子序列都从 0 重启、靠不同钥匙区分（L107-110）。即使有了逐行种子，一个 seeded 行的邻居换了（比如混进一个 min_p 行），它就会被换到另一趟、子序列编号随之改变——流仍然随批组合漂移。「邻居的参数会改你的流」是批式调用的结构性质，不是 `seed_arr` 的实现缺陷。这就是策略层干脆用单行调用的原因：把批组合这个变量从物理上排除。

本实践为源码阅读型，无需 GPU，预期结果即上述文字结论。

#### 4.2.5 小练习与答案

**练习 1**：分离 min_p 两趟为什么用「换钥匙」（`seed ^ γ`）而不是「子序列偏移」？

**答案**：两趟是**两次独立的核调用**，每次调用的子序列都从 0 重新编号——「第二趟从第 n 处接着编」在 FlashInfer 的接口里表达不出来（`offset` 也是标量 0）。调用侧唯一可控的旋钮就是传进去的种子本身，所以把种子换掉是最直接、且与接口语义一致的做法。

**练习 2**：无种子行换批后 token 会变吗？这算 bug 吗？

**答案**：很可能会变——它的子序列是它在压缩批里的位置，批组合变则位置变则流变。不算 bug：前端契约对无种子行只承诺「相同引擎种子 + 相同批布局」下的可重放（见 4.3.3 的 sampler.rs 文档），从未承诺跨批布局稳定。

**练习 3**：一个 seeded 行错误地留在了批式调用里，批 A（3 行）中它排第 0、批 B（2 行）中它排第 1。它的 philox 子序列分别是多少？

**答案**：分别是 0 和 1——即 `philox(engine_seed, 0)` 与 `philox(engine_seed, 1)`，两条不同的流；再加上两个批的引擎种子通常也不同，token 几乎必然不同。这正是回归测试 `seeded_rows_replay_independent_of_batch_position` 用断言 `a[2] == b[0]` 专门防御的失效模式。

### 4.3 每步引擎种子推进：两级种子的分工

#### 4.3.1 概念说明

现在把两讲的内容拼成完整的种子体系。系统里同时存在两级种子，各自背一份承诺：

| | 引擎种子 `seed`（`select_batch` 参数） | 请求种子 `p.seed`（`SamplingParams` 字段） |
|---|---|---|
| 谁产生 | 调度器，**每个解码步新鲜抽取** | 请求方随请求给定，请求存活期内不变 |
| 服务谁 | 整批的所有无种子采样行（一次批式调用共用） | 单个带种子的行（逐行单次调用） |
| 步间去相关 | 每步换种子 | `mix_seed(seed, step)` 每步换流 |
| 行间去相关 | philox 子序列（批内压缩位置） | 不需要（每行独立调用） |
| 可重放承诺 | 同种子 + 同批布局 | token = f(seed, step, 分布)，与批组合、邻居、引擎种子**全部无关** |

分工的直觉：**引擎种子管「这批人这一步用哪套随机性」，请求种子管「这个请求这一生用哪条轨道」**。前者允许（也只允许）批内布局参与决定流；后者必须把批内布局排除在外——所以一个走批式、一个走单行。

注意一个容易忽略的细节：seeded 但**语义等价贪心**的行（`effectively_greedy` 为真，u2-l1）根本不进种子体系——贪心路径不消耗随机数。种子体系只覆盖真正采样的行。

#### 4.3.2 核心流程

一个解码步的种子时间线：

```text
调度器:  sample_seed ← StdRng 每步抽取一个新 u64     # 「fresh per step」
执行器:  select_batch(ctx, logits, params, steps, sample_seed, scratch)
          ├─ 贪心行（含 seeded 贪心行）: 批式 argmax     # 不碰任何种子
          ├─ 无种子采样行: 一次批式调用
          │     key = sample_seed, subsequence = 批内压缩位置
          └─ seeded 采样行: 逐行单行调用
                key = mix_seed(p.seed, steps[i]), subsequence = 0
```

其中 `steps[i]` 是**请求内**局部步数，不是全局引擎步数：一个已经生成了 5 个 token 的请求，无论这一步它和谁同批、无论引擎重启过多少次，`steps[i]` 都是 5。这是跨批组合重放成立的最后一块拼图——(seed, step) 两个输入都不含任何批布局信息。

#### 4.3.3 源码精读

`select_batch` 文档先立下引擎种子的硬性契约：

> `seed` must be fresh per decode step (one engine seed at startup, advanced per step); unseeded rows decorrelate through the philox subsequence.

[src/lib.rs:L179-L180](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/src/lib.rs#L179-L180) ——「每步新鲜」是要求，不新鲜（复用上一步种子）意味着无种子行每步重放同一条子序列流，与 4.1.1 讨论的逐步重启问题同构。

承诺的原始出处在前端契约的字段文档里，措辞值得逐句读：

> Per-request sampling seed, handed to the engine as the request gave it. What a seed replays is the engine's promise: a fixed seed gives the same tokens for the same request where the engine's draw depends on nothing else, or at the same batch layout where it does.

[pegainfer-frontend/src/sampler.rs:L10-L14](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-frontend/src/sampler.rs#L10-L14) ——后半句精确划出了两级承诺的边界：引擎的抽取「不依赖别的东西」时，固定种子给出相同 token；「依赖批布局」时，只在相同批布局下重放。seeded 单行调用实现前者，无种子批式调用落在后者。

引擎侧怎么兑现「每步新鲜」？以 qwen3 为例，调度器持有主机端 `StdRng`，每个执行计划抽取一个全新的 `sample_seed` 发给执行器：

[pegainfer-qwen3/src/scheduler/plan.rs:L109-L118](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/scheduler/plan.rs#L109-L118) ——`Decode` 分支里 `sample_seed: rand::RngExt::random(rng)`（L114）；prefill（L104）、speculative verify（L131）、unified（L155）各分支同样各抽一个。文档说的「启动取一个、每步推进」在这里的实现形态是「每步从 StdRng 现抽」——两者都满足硬性契约「不得复用上一步的种子」。

测试侧，引擎种子的确定性与「真的在采样」由这个测试钉住：

[tests/select_batch.rs:L117-L140](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/tests/select_batch.rs#L117-L140) ——均匀 logits 上（argmax 恒选 0）：同种子两次调用结果相等（L128-130）；64 个不同种子扫出多于一个 token（L132-139），证明采样通道真的在消耗随机性而非偷偷走 argmax。

而本讲的主角测试把请求级契约冻结成断言。先看契约陈述：

> The per-request seed contract: a seeded row's token is a pure function of (seed, step, distribution) — independent of where the row sits in the batch, of its neighbors, and of the engine's per-step seed.

[tests/select_batch.rs:L325-L327](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/tests/select_batch.rs#L325-L327)

测试体（[tests/select_batch.rs:L329-L371](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/tests/select_batch.rs#L329-L371)）设计了两个精心扰动的批布局：

- **批 A**（[L345-L353](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/tests/select_batch.rs#L345-L353)）：3 行 `[greedy, unseeded, seeded(7)]`，steps `[0, 0, 5]`，引擎种子 99——seeded 行在**批尾**。
- **批 B**（[L354-L362](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/tests/select_batch.rs#L354-L362)）：2 行 `[seeded(7), greedy]`，steps `[5, 0]`，引擎种子 1234——seeded 行在**批首**。

两批之间，邻居、批大小、批内位置、引擎种子**全部不同**，唯一保持的是 (seed=7, step=5)。断言 `a[2] == b[0]`（[L363](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/tests/select_batch.rs#L363)）于是同时排除了四类潜在依赖——这就是「纯函数」的可操作化定义。

测试没有停在「相等」：若实现退化成忽略 seed/step 的常数流，上面的断言照样通过。所以还有两条**不等**断言（[L367-L370](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/tests/select_batch.rs#L367-L370)）：step 5→6（c）、seed 7→8（d）都必须移动流。注释（[L365-L366](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/tests/select_batch.rs#L365-L366)）算过账：平坦 32k 词表上偶然碰撞概率约 \(1/32768 \approx 3\times10^{-5}\)，两条断言足以可信。

#### 4.3.4 代码实践

**实践目标**：核实引擎种子「每步一次、整批一份」的供给节奏，并观察投机解码分支的一个有趣不对称。

**操作步骤**：

1. 打开 [pegainfer-qwen3/src/scheduler/plan.rs:L92-L162](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/scheduler/plan.rs#L92-L162)，数一数 `sample_seed: rand::RngExt::random(rng)` 出现的分支（应为 4 处：prefill L104、decode L114、speculative verify L131、unified L155）。
2. 注意 `execute_plan` 的签名（L96）：`rng: &mut StdRng` 由调度器持有、逐步复用——每次 `random` 都从这条主机流抽取新值，这就是「每步新鲜」的实现。
3. 观察不对称：`SpeculativeDecode` 分支里 `DraftPlan`（L124-126）**没有** `sample_seed` 字段，而 `VerifyPlan`（L129-133）有。

**需要观察的现象 / 预期结果**：每个执行计划至多抽取一次 sample_seed，整批共享；投机解码的草稿（draft）计划不携带采样种子，只有验证（verify）轮携带。对第 3 点可先记下观察、提出你的推测（例如：草稿提议的选 token 方式与验证轮不同），等到学习 qwen3 的投机解码讲义时再回头验证——本讲不展开。

本实践为源码阅读型，无需 GPU。

#### 4.3.5 小练习与答案

**练习 1**：同一个请求带 seed=7，在两台机器上各生成 100 个 token。第 5 步的 token 一定相同吗？

**答案**：契约说是：token 是 f(7, 5, 分布) 的纯函数，「分布」由权重与前缀决定。在数值路径一致（同权重、同前缀、同 kernel 行为）的前提下相同；若两台机器的浮点路径本身有差异，分布就可能不同——那不是种子体系的失败，而是「分布」这个输入变了。种子体系承诺的只是：**分布相同 ⇒ token 相同**。

**练习 2**：如果调用方偷懒把 `steps` 传成全 0，会发生什么？

**答案**：每个 seeded 行每步都算 `mix_seed(seed, 0)`，得到同一条流。token 未必每步相同（分布每步在变），但均匀数序列每步从同一点重启，产生跨步系统性偏差；同时两个碰巧同种子的请求会逐 token 互为重放。契约「step 变化 ⇒ 流移动」被静默破坏——而 `select_batch` 只校验 `steps.len() == n`（L201-205），不校验其内容，正确传 steps 是调用方的责任。

**练习 3**：回归测试里那两条 `assert_ne!`（step 变化、seed 变化）删掉行不行？

**答案**：不行。重放断言 `a[2] == b[0]` 对「常数流」实现（例如错误地恒用 `philox(0, 0)`）同样成立——所有调用返回同一个 token，相等永真。发散断言才排除这种空洞通过：step 与 seed 都必须真正进入流的选择。测试注释用 ~3e-5 的碰撞概率说明「不等」断言在平坦分布上可信。这是测试设计的一般教训：**相等断言防「做错」，不等断言防「没做」**。

## 5. 综合实践

**任务**：为 `mix_seed` 写一组纯 CPU 单元测试，验证它的三个性质——确定性、步间不碰撞、种子雪崩；然后回答关于回归测试布局设计的思考题。

**背景**：`src/lib.rs` 内嵌的 `mod tests` 目前只有三个 logprob 测试，`mix_seed` 没有任何直接测试（它的行为目前只被 GPU 集成测试间接覆盖）。`mix_seed` 是纯函数，测它不需要 GPU。

**操作步骤**：

1. 在你的本地工作副本中，打开 `pegainfer-sample/src/lib.rs`，把下面的测试加进文件底部的 `mod tests`（学习练习；测完记得还原，不作为对仓库的改动提交）：

```rust
// 示例代码：加在 #[cfg(test)] mod tests 内；也可放到独立小 crate
// （mix_seed 是 pub 的，可用 use pegainfer_sample::mix_seed; 引用）
use super::mix_seed;

#[test]
fn mix_seed_same_inputs_give_same_output() {
    // 性质 1：纯函数——相同 (seed, step) 必须复现。
    for seed in [0u64, 7, u64::MAX] {
        for step in [0u64, 1, 5, 1 << 40] {
            assert_eq!(mix_seed(seed, step), mix_seed(seed, step));
        }
    }
}

#[test]
fn mix_seed_distinct_steps_do_not_collide() {
    // 性质 2：step 变化输出几乎必然不同。
    let mut seen = std::collections::HashSet::new();
    for step in 0..1024u64 {
        seen.insert(mix_seed(7, step));
    }
    assert_eq!(seen.len(), 1024, "1024 个步数应产生 1024 个不同种子");
}

#[test]
fn mix_seed_avalanches_on_seed_lsb() {
    // 性质 3：翻转 seed 最低位，输出每位约以 1/2 概率翻转。
    let trials = 4096u64;
    let mut flipped = 0u64;
    for step in 0..trials {
        let a = mix_seed(0xDEAD_BEEF, step);
        let b = mix_seed(0xDEAD_BEEF ^ 1, step);
        flipped += (a ^ b).count_ones() as u64;
    }
    let ratio = flipped as f64 / (trials as f64 * 64.0);
    assert!(
        (0.45..=0.55).contains(&ratio),
        "雪崩比例 {ratio} 偏离 0.5 过远"
    );
}
```

2. 运行：`cargo test --release -p pegainfer-sample --lib mix_seed -- --nocapture`。

**需要观察的现象**：三条测试全部通过；第三条打印不出任何东西（无碰撞可报）。

**预期结果**（理论推导，统计断言待本地验证）：

- 性质 1 平凡成立（纯函数）。
- 性质 2 的碰撞概率约为
  \[ \binom{1024}{2} \cdot 2^{-64} = \frac{1024 \times 1023}{2} \times 2^{-64} \approx 2.8 \times 10^{-14} \]
  即 1024 个输出全不相同几乎必然。
- 性质 3 的总翻转位数为 \(4096 \times 64 = 262144\)，期望 \(131072\)，标准差 \( \sqrt{262144 \times 0.25} = 256 \)，比例标准差约 \(0.001\)；界 \([0.45, 0.55]\) 宽松到约 51 个标准差，SplitMix64 是公认的好混合器，应当稳定通过。

3. **思考题**：为什么 `seeded_rows_replay_independent_of_batch_position` 要专门构造「seeded 行在批尾（批 A）」与「在批首（批 B）」两个布局？

**参考答案**：因为被防御的失效模式恰恰是「批内位置进入随机流」。假如实现退化成让 seeded 行搭批式调用，它的 philox 子序列就是它在压缩批里的位置：批 A 中位置 2、批 B 中位置 0，加上两批引擎种子不同（99 vs 1234），token 几乎必然不同，断言 `a[2] == b[0]` 会立刻失败。同时两个布局把邻居（greedy+unseeded vs greedy）、批大小（3 vs 2）也全部扰动掉——在这些全部变化之后仍要求 token 相等，才证明唯一被依赖的输入就是 (seed=7, step=5)，即「纯函数」契约。若只测单一布局，批位置、邻居、批大小这些变量根本没被扰动，测试对它们是否影响输出毫无发言权。

## 6. 本讲小结

- `mix_seed` 是 6 行 SplitMix64：黄金伽马 `0x9E3779B97F4A7C15` 预扩散 step，两轮乘法-异或移位终结器把 (seed, step) 打成雪崩良好的 philox 种子；它是 `pub` 的，供自驱路径的模型复刻同一种子语义。
- FlashInfer 批式采样核整批只读一个标量种子（`seed_arr[0]`），把 `blockIdx.x` 折进 philox 子序列——行的随机流由「批内压缩位置」决定；min_p 分区「两趟子序列都从 0 重启、需换钥匙」证明这是真实语义而非注释修辞。
- 因此 seeded 行必须单行调用：`n_rows = 1` 时 `blockIdx.x` 恒 0，批内位置这个变量被物理消灭，token 成为 (seed, step, 分布) 的纯函数。
- 两级种子分工：引擎种子每步新鲜（qwen3 由调度器的 StdRng 逐计划抽取）、服务整批无种子行、行间靠子序列去相关；请求种子经 `mix_seed(seed, step)` 每步换流、逐行调用、与批组合完全解耦。
- 契约测试的布局设计是一门手艺：批 A/批 B 把位置、邻居、批大小、引擎种子全部扰动后仍要求相等（防「做错」），step/seed 变化要求不等（防「没做」的常数流）。

## 7. 下一步学习建议

单元二到此完成——`select_batch` 的三路分发、路由谓词、缓冲布局与种子体系已经全部走通。接下来两条路任选：

1. **主线**：进入单元三第 1 讲 `u3-l1-token-logprob-reference.md`，学习平行的 logprob 通道——从主机端 `token_logprob_from_row` 的 log-softmax 数学开始。采样与 logprob 是本 crate 的两大职责，学完即拥有全貌。
2. **延伸阅读**（可选）：
   - [pegainfer-kernels/csrc/shared/flashinfer_sampling.cu](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-kernels/csrc/shared/flashinfer_sampling.cu) 全文——看四种采样核如何按行参数选择，与本讲的三路分发对照。
   - `pegainfer-kernels` 采样单测中的 `batch_sampling_same_seed_is_deterministic`（[src/ops/sampling.rs:L1249-L1280](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-kernels/src/ops/sampling.rs#L1249-L1280)）——内核层的种子确定性证据，与 `pegainfer-sample` 层的 `sampling_is_seed_deterministic_and_actually_samples` 互为镜像。
   - u4-l1（分阶段贪心路径）会再次遇到「贪心不消耗随机数」这一事实的回响：正因为 argmax 无随机性，它才能被整段捕获进 CUDA 图。可以在学单元四前先记住这条性质。
