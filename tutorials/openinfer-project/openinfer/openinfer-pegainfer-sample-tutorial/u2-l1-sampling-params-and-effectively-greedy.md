# SamplingParams 与 effectively_greedy：一行走不走 argmax 由什么决定

## 1. 本讲目标

学完本讲，你应该能够：

- 读懂 `SamplingParams` 每个字段（`temperature` / `top_k` / `top_p` / `min_p` / `seed`）的语义，以及「显式贪心」`is_greedy()` 的两条判据。
- 推导 `effectively_greedy` 背后的数学：softmax 的最大概率恒 >= \( 1/V \)，因此 `top_p <= 1/V` 时核（nucleus）只剩 argmax 那一个 token。
- 解释为什么 bf16 并列最大值（tie）会让拒绝采样器变得不确定，而 argmax 路径（按下标最低破并列）是确定的。
- 区分两级判定：`is_greedy`（参数显式要求贪心）与 `effectively_greedy`（参数名义上要采样，但语义等价于贪心）。
- 理解路由宽度为什么是 `scratch.selection_width`（可发射词表）而不是 logits arena 的物理宽度（tile 对齐填充列）。

本讲是单元二「select_batch 批式选 token 主链路」的第一讲：路由谓词是整条主链路的第一个决策点，后续的 scratch 布局（u2-l2）与三路分发（u2-l3）都建立在它之上。

## 2. 前置知识

本讲只需要一点概率与浮点数的常识，不需要 GPU 编程经验。

- **logits 与 softmax**：语言模型为词表中的每个 token 输出一个实数分数（logit）。softmax 把一列 logits 变成一列概率：\( p_i = \dfrac{e^{x_i}}{\sum_j e^{x_j}} \)，所有 \( p_i \) 之和为 1。logit 越大，概率越高。
- **贪心解码（greedy / argmax）**：永远选概率最大的 token，即 argmax。它完全确定——同样的输入永远得到同样的输出。`temperature = 0` 的直觉含义就是「退化成 argmax」。
- **温度（temperature）**：采样前把 logits 除以 \( t \)。\( t > 1 \) 拉平分布（更随机），\( t \to 0 \) 则分布尖成一根针（极限即 argmax）。
- **top-k**：只保留概率最高的 k 个 token，其余置零后再归一化。`top_k = 1` 等价于只留一个 token，也就是等价于贪心。
- **top-p / 核采样（nucleus sampling）**：把 token 按概率从高到低排序，取**累计概率恰好达到 `top_p` 的最小前缀**作为候选集（核），再在核内按（归一化后的）概率随机抽一个。`top_p = 1` 表示不裁剪。
- **min_p**：以最大概率为基准的阈值过滤——概率低于 \( \text{min\_p} \times p_{\max} \) 的 token 被屏蔽。默认 `0.0` 表示关闭。
- **bf16（bfloat16）**：一种 16 位浮点格式，指数位与 f32 相同（8 位），但尾数只有 7 位。数值间距粗——例如在 8.0 附近，两个相差不到 0.0625 的 f32 logit 会被舍入成**同一个** bf16 值。也就是说，模型里「并列第一」比直觉中常见得多。
- **确定性（determinism）**：对一个推理引擎，「temperature = 0 的请求每次输出都一样」是用户默认的契约。凡是让 token 依赖随机数流的做法都会破坏它。

本 crate 的背景（u1-l1 已建立）：`pegainfer-sample` 是所有模型 crate 共享的采样策略层，`select_batch` 把一个 logits arena 变成「每行一个 token id」，没有任何逐行逃生舱。

## 3. 本讲源码地图

| 文件 | 角色 | 本讲关注点 |
| --- | --- | --- |
| `pegainfer-sample/src/lib.rs` | crate 全部实现（单文件） | `effectively_greedy` 谓词、`select_batch` 里调用它的路由点、`SampleScratch::with_selection_width` 的宽度语义 |
| `pegainfer-sample/tests/select_batch.rs` | 需要 GPU 的模型无关集成测试 | `tiny_top_p_routes_to_argmax_even_under_bf16_ties` 与 `padded_arena_width_does_not_suppress_the_argmax_routing` 两个回归测试——本讲数学论证的「冻结形式」 |
| `pegainfer-frontend/src/sampler.rs` | 契约 crate（crate 外，一个文件） | `SamplingParams` 结构体定义、默认值、`is_greedy()` 判定 |

`SamplingParams` 不住在采样 crate 里——它是前端契约的一部分，`pegainfer-sample` 通过 `pub use` 再导出（见 [src/lib.rs:L33](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/src/lib.rs#L33)），模型 crate 因此不必直接依赖前端 crate 就能构造参数。这是 u1-l1 讲过的分层：策略归本 crate，契约归前端。

## 4. 核心概念与源码讲解

### 4.1 SamplingParams 契约字段

#### 4.1.1 概念说明

一行（一个请求）怎么采样，完全由它的 `SamplingParams` 描述。这个结构体是「用户意图」的载体：温度多高、裁剪多狠、要不要固定种子。路由的第一级判定 `is_greedy()` 就建立在这些字段上——参数**显式**说「我要贪心」时，无需任何数学论证，直接走 argmax。

关键是要把「用户写字段」和「字段合起来的语义」分开看：`temperature = 0` 与 `top_k = 1` 是两种完全不同的写法，但语义相同（都只剩一个可发射 token）。`is_greedy()` 就是把这两种写法归一成同一个判定的函数。

#### 4.1.2 核心流程

`SamplingParams` 的字段与默认值：

| 字段 | 类型 | 默认值 | 语义 |
| --- | --- | --- | --- |
| `temperature` | `f32` | `0.0` | 采样温度；`< 1e-5` 视为贪心 |
| `top_k` | `i32` | `-1` | 只保留前 k 个 token；`-1` 关闭；`1` 等价贪心 |
| `top_p` | `f32` | `1.0` | 核采样阈值；`1.0` 不裁剪；本讲的主角 |
| `min_p` | `f32` | `0.0` | 相对最大概率的阈值过滤；`0.0` 关闭，有效范围 `[0, 1)` |
| `seed` | `Option<u64>` | `None` | 每请求种子；`Some` 时该行做单行可重放调用（u2-l4 的主题） |
| `ignore_eos` | `bool` | `false` | 是否忽略结束符（本讲不涉及） |

`is_greedy()` 的判定流程（伪代码）：

```text
is_greedy(p) =
    p.temperature < 1e-5        # 温度趋零的极限就是 argmax
    或 p.top_k == 1             # 掩码后只剩一个幸存 token
```

注意默认参数（`temperature = 0.0`）本身就满足第一条——所以「不配置任何采样参数」的请求默认是贪心的。

#### 4.1.3 源码精读

契约结构体定义在 `pegainfer-frontend`（crate 外），字段与文档注释一目了然：

- [pegainfer-frontend/src/sampler.rs:L1-L16](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-frontend/src/sampler.rs#L1-L16) — `SamplingParams` 结构体：五个采样字段加 `ignore_eos`。`min_p` 的注释给出精确定义「概率低于 `min_p * p_max` 的 token 被屏蔽」；`seed` 的注释明确种子能重放什么 是引擎的承诺，不是采样器的）。
- [pegainfer-frontend/src/sampler.rs:L18-L29](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-frontend/src/sampler.rs#L18-L29) — `Default` 实现：`temperature: 0.0`、`top_k: -1`、`top_p: 1.0`、`min_p: 0.0`、`seed: None`。默认即贪心。
- [pegainfer-frontend/src/sampler.rs:L31-L40](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-frontend/src/sampler.rs#L31-L40) — `is_greedy()`：`self.temperature < 1e-5 || self.top_k == 1`。文档注释解释了两点：温度趋零的极限是 argmax 且 `1/temperature` 在那之前早就溢出（核内是乘 `1/t` 的，epsilon 线避开溢出）；vLLM 也在 `1e-5` 处画同一条线。

再导出与路由入口（本 crate 内）：

- [src/lib.rs:L33](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/src/lib.rs#L33) — `pub use pegainfer_frontend::sampler::SamplingParams;`：模型 crate 从本 crate 拿到契约类型，分层单向。
- [src/lib.rs:L171-L177](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/src/lib.rs#L171-L177) — `select_batch` 文档中关于路由的一段：一行在 `effectively_greedy` 成立时走 argmax 路径——显式贪心参数，**或** `top_p <= 1/vocab` 紧到只有 argmax 幸存的核。并说明这样做的两个收益：确定性与跳过一次不必要的 softmax。

测试侧的两个构造辅助函数是读懂参数语义最快的入口：

- [tests/select_batch.rs:L37-L48](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/tests/select_batch.rs#L37-L48) — `greedy()` 就是 `SamplingParams::default()`（默认即贪心）；`sampling(temperature, top_k, top_p)` 构造一个「真采样」行。
- [tests/select_batch.rs:L80-L92](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/tests/select_batch.rs#L80-L92) — `top_k_one_routes_through_greedy_path`：`temperature = 1.0` 但 `top_k = 1`，断言仍取 argmax 峰值——把 `is_greedy` 的第二个析取支钉成测试。

#### 4.1.4 代码实践

1. **实践目标**：不看 `is_greedy` 的实现，凭字段语义预测判定结果，再对照源码验证。
2. **操作步骤**：对下面 6 组参数，先在纸上写出 `is_greedy()` 的真值，再读 [pegainfer-frontend/src/sampler.rs:L37-L39](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-frontend/src/sampler.rs#L37-L39) 核对：

   | temperature | top_k | 你的预测 |
   | --- | --- | --- |
   | 0.0 | -1 | ？ |
   | 1.0 | 1 | ？ |
   | 0.7 | 20 | ？ |
   | 1e-5 | -1 | ？ |
   | 1e-6 | 1 | ？ |
   | 0.8 | 0 | ？ |

3. **需要观察的现象**：第 4 行（`temperature` **恰等于** `1e-5`）是边界——判定用的是严格小于 `<`，所以它**不是**贪心（除非 `top_k == 1`）。第 6 行 `top_k = 0` 既不是 `-1`（关闭）也不是 `1`，`is_greedy` 只认 `== 1`。
4. **预期结果**：六行依次为 是 / 是 / 否 / 否 / 是 / 否。待本地验证（若想跑代码，见第 5 节综合实践里构造 `SamplingParams` 的方式——`is_greedy` 是纯 CPU 函数，可在任意 Rust 单元测试里直接调用）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `is_greedy` 把阈值画在 `temperature < 1e-5`，而不是精确判断 `temperature == 0.0`？

**答案**：文档注释给出两个理由。其一，数学上温度趋零的极限就是 argmax，`1e-5` 以下与 0 没有可分辨的语义差别；其二，采样核内部是把 logits 乘以 `1/temperature`，温度足够小时 `1/t` 早就溢出了，必须在溢出之前就把这类行改道 argmax。vLLM 也在同一条 `1e-5` 线上划界。

**练习 2**：`min_p` 和 `seed` 会影响 `is_greedy` 吗？

**答案**：不会。`is_greedy` 只看 `temperature` 与 `top_k`。`min_p` 是采样路径内部的过滤（`p < min_p * p_max` 的 token 被屏蔽，见 [pegainfer-frontend/src/sampler.rs:L6-L9](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-frontend/src/sampler.rs#L6-L9)）；`seed` 只决定随机数流（u2-l4 专题），两者都不改变「候选集是否只剩一个 token」这个贪心判据。

**练习 3**：默认参数（`SamplingParams::default()`）是贪心的吗？依据是什么？

**答案**：是。默认 `temperature = 0.0 < 1e-5`，命中 `is_greedy` 的第一个析取支（[pegainfer-frontend/src/sampler.rs:L20-L28](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-frontend/src/sampler.rs#L20-L28)）。测试里的 `greedy()` 辅助函数正是直接用 `default()` 表达「一行贪心」。

### 4.2 effectively_greedy 判定

#### 4.2.1 概念说明

`is_greedy` 回答「参数显式要求贪心吗」，但还有一类行：参数名义上要采样（温度正常、`top_k != 1`），可它的 `top_p` 小到核里只剩一个 token——**语义上**它就是贪心。`effectively_greedy` 是把这类「隐式贪心」也识别出来、统一改道 argmax 的谓词。

为什么必须改道，而不是让它照常走采样器？两个原因（都写在 `select_batch` 的文档注释里，[src/lib.rs:L171-L177](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/src/lib.rs#L171-L177)）：

1. **确定性**：当最大概率处存在 bf16 并列时，拒绝采样器会在并列成员里按随机数挑一个——同一个请求换个引擎种子就换输出。argmax 则按下标最低破并列，永远确定。
2. **省一次 softmax**：核内只有一个 token 时，采样通道的 softmax / 归一化全是白做的工作。

这个谓词是 `pub` 的，且文档写明理由：自带贪心路径的模型（如 Kimi-K2，见 u1-l1）必须复刻 `select_batch` 的这条语义，否则同样的参数在两条路径下行为分叉（[src/lib.rs:L448-L454](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/src/lib.rs#L448-L454)）。

#### 4.2.2 核心流程

```text
effectively_greedy(params, vocab_size) =
    params.is_greedy()                          # 第一级：显式贪心
    或 (
        vocab_size > 0                           # 防守：宽度为 0 时 1/0 = +inf，见下
        且 params.top_p.is_finite()              # 防守：NaN / inf 不判定
        且 params.top_p > 0.0                    # 防守：恰为 0 是退化值，不判定
        且 params.top_p <= 1.0 / vocab_size      # 核坍缩判据（4.3 节推导）
    )
```

它在 `select_batch` 中的使用位置——注意宽度传的是 `scratch.selection_width`：

```text
select_batch:
    校验批大小 / 步数 / arena 行数 / vocab 宽度
    is_argmax = |p| effectively_greedy(p, scratch.selection_width)
    greedy 行号集合   = filter_map(is_argmax)          → 一次批式 indexed argmax
    sampling 行号集合 = filter(非 is_argmax 且无 seed)   → 一次 FlashInfer 批式采样
    seeded 行         = 逐行（若 is_argmax 则同样跳过采样）
```

一个容易被忽略的细节：`effectively_greedy` 为真的行在**三条**路径上都被改道——贪心集合收入它（[src/lib.rs:L229-L233](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/src/lib.rs#L229-L233)），无种子采样集合排除它（[src/lib.rs:L268-L271](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/src/lib.rs#L268-L271)），带种子的单行循环也跳过它（[src/lib.rs:L300-L302](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/src/lib.rs#L300-L302)）——对 argmax 谈种子没有意义。

三个防守条件的用意：

- `vocab_size > 0`：Rust 里 `1.0f32 / 0.0f32` 不 panic，结果是 `+inf`。若不防守，任何有限 `top_p` 都满足 `top_p <= inf`，一个荒谬的零宽词表会让**所有**行被判定为贪心。
- `top_p.is_finite()`：NaN 与任何数比较都是 `false`，本条会自然失败，但显式写出让意图可读；`+inf` 同理被排除。
- `top_p > 0.0`：`top_p = 0`（核为空集的退化值）不被认领，保守地下放给采样通道去处理或报错。整个谓词的设计取向是：**只在核坍缩论证无懈可击时才认领**，其余一律交给采样器。

#### 4.2.3 源码精读

- [src/lib.rs:L448-L461](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/src/lib.rs#L448-L461) — `effectively_greedy` 全文：文档注释先给结论（「除显式贪心参数外，`top_p` 在 `1/vocab` 及以下的核只剩 argmax token」），再给理由（softmax 最大值恒 `>= 1/vocab`），最后说明它为何是 `pub`（自带贪心路径的模型必须复刻此语义，否则在 bf16 并列最大值上变得随机、与 `select_batch` 分叉）。函数体即上面伪代码的五条件析取。
- [src/lib.rs:L223-L225](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/src/lib.rs#L223-L225) — `select_batch` 里的路由闭包，连同它的两行注释：「模型为对齐 GEMM 而填充的列不是可发射 token，不能移动 `top_p <= 1/vocab` 的核」。宽度取 `scratch.selection_width` 而非 `logits.hidden_dim`。
- [src/lib.rs:L97-L101](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/src/lib.rs#L97-L101) — `SampleScratch::with_selection_width` 的文档注释，讲清宽度的语义：arena 有 `vocab` 列，但最后 `vocab - selection_width` 列是模型为了凑 GEMM tile 倍数而加宽出来的、**不可发射**的填充列。若用物理宽度做判据，一个语义上贪心的请求会掉进拒绝采样器、在 bf16 并列最大值上随机（#1046 场景，u2-l2 详讲）。
- [src/lib.rs:L86-L88](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/src/lib.rs#L86-L88) — `SampleScratch` 的 `selection_width` 字段文档：路由判定所对照的宽度是「可发射 token 数，不是模型可能对齐得更宽的 arena」。

#### 4.2.4 代码实践

1. **实践目标**：用三个防守条件的边界值，验证 `effectively_greedy` 是一个「保守认领」的谓词。
2. **操作步骤**：在自己克隆的仓库里，往 `src/lib.rs` 底部已有的 `#[cfg(test)] mod tests`（[src/lib.rs:L651-L704](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/src/lib.rs#L651-L704)）中添加一个纯 CPU 测试。以下为**示例代码**（非项目原有代码）：

   ```rust
   use super::{SamplingParams, effectively_greedy};

   #[test]
   fn effectively_greedy_guards_are_conservative() {
       let sample = SamplingParams {
           temperature: 1.0,   // 非 0：排除 is_greedy 的第一条
           top_k: -1,          // 非 1：排除 is_greedy 的第二条
           top_p: 0.5,
           ..SamplingParams::default()
       };
       // 零宽词表：1/0 = +inf，若不防守任何有限 top_p 都会被认领
       assert!(!effectively_greedy(&sample, 0));
       // NaN 与 +inf 的 top_p：is_finite 防守
       let mut nan = sample;
       nan.top_p = f32::NAN;
       assert!(!effectively_greedy(&nan, 8));
       let mut inf = sample;
       inf.top_p = f32::INFINITY;
       assert!(!effectively_greedy(&inf, 8));
       // top_p = 0：退化值，不认领
       let mut zero = sample;
       zero.top_p = 0.0;
       assert!(!effectively_greedy(&zero, 8));
   }
   ```

3. **需要观察的现象**：四个断言全部通过——每一行都**没有**被判成贪心。
4. **预期结果**：`cargo test --release -p pegainfer-sample --lib effectively_greedy` 通过。注意：编译本 crate 需要构建 `pegainfer-kernels` 的 CUDA 工具链，但这个测试运行时不需要 GPU 设备（不构造 `DeviceContext`）。待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：如果删掉 `vocab_size > 0` 这个条件，`effectively_greedy(&params, 0)` 对 `top_p = 0.5` 的行返回什么？后果是什么？

**答案**：`1.0f32 / 0 as f32` 是 `+inf`（Rust 浮点除零不 panic），`0.5 <= inf` 为真，于是返回 `true`——一个采样行被错误改道 argmax。更糟的是**任何**有限 `top_p` 都满足 `<= inf`，零宽词表（本身就是非法状态）会让全部行被吞进贪心路径，把错误从「拒绝非法输入」变成「静默给错答案」。

**练习 2**：`is_greedy` 与 `effectively_greedy` 的分工是什么？为什么不把 `effectively_greedy` 的逻辑并进 `is_greedy`？

**答案**：`is_greedy` 是**契约层**的判定——只看参数自身（`temperature` / `top_k`），不需要知道词表多大，住在 `pegainfer-frontend`。`effectively_greedy` 是**策略层**的判定——需要额外的 `vocab_size`（实际是 selection width）才能做核坍缩论证，住在 `pegainfer-sample`。契约类型无法访问词表宽度，所以两级判定天然分属两层；`effectively_greedy` 内部复用 `is_greedy` 作为第一析取支。

**练习 3**：一个带 `seed: Some(7)`、`top_p = 1e-6`、`temperature = 1.0` 的行，最终走哪条路径？种子起了什么作用？

**答案**：走 argmax 路径。`select_batch` 的带种子循环里有一句 `if is_argmax(p) { continue; }`（[src/lib.rs:L300-L302](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/src/lib.rs#L300-L302)）：语义等价贪心的行连种子路径都不进——argmax 不消耗随机数，种子对它无意义，同时它依然确定。

### 4.3 top_p 核数学与 bf16 并列

#### 4.3.1 概念说明

本节推导 `effectively_greedy` 的数学核心，然后解释它的「敌人」——bf16 并列最大值——为什么会让采样器失去确定性。

**核坍缩论证**：top-p 的核是「按概率降序排列后，累计概率达到 `top_p` 的最小前缀」。只要 `top_p <= 1/V`，核就至多含一个 token。这不是经验观察，是可以证明的定理，而两个回归测试就是这条定理的冻结形式（测试文件头部的注释说明了分工：裸核正确性归 `pegainfer-kernels`，本 crate 测试钉的是策略行为）。

**bf16 并列问题**：推理引擎的设备端 logits 是 bf16。bf16 尾数只有 7 位，两个「概念上不同」的 logits 很容易舍入到同一个 bf16 值——概率完全并列。此时：

- argmax 核按下标最低破并列：永远返回同一个 token。
- 拒绝采样器面对并列（或核边界上的等概率对）时，抽到哪个取决于随机数——而引擎种子每个解码步都在换（u2-l4），于是「top_p 极小」的请求每次跑、每步都可能在并列成员之间跳。

测试注释（[tests/select_batch.rs:L144-L152](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/tests/select_batch.rs#L144-L152)）还记录了历史：旧的每请求 RNG 恰好**掩盖**了这个不确定性（同请求同种子，跳了也看不出来）；迁移到每步 philox 种子后问题暴露，这条路由与回归测试随之诞生。

#### 4.3.2 核心流程

**定理**：对任意 logits 列 \( x_1, \dots, x_V \)（\( V \ge 1 \)），softmax 最大概率满足

\[
p_{\max} \;=\; \frac{e^{x_{\max}}}{\sum_{j=1}^{V} e^{x_j}}
\;\ge\; \frac{e^{x_{\max}}}{V \cdot e^{x_{\max}}}
\;=\; \frac{1}{V}
\]

第二步用了 \( e^{x_j} \le e^{x_{\max}} \) 对所有 \( j \) 成立，故分母 \( \le V e^{x_{\max}} \)。等号当且仅当分布均匀（所有 logit 相同）时取得。

**推论**：若 \( \text{top\_p} \le \dfrac{1}{V} \le p_{\max} \)，则排序后第一个 token 的累计概率已达到 `top_p`，核恰为 { argmax }（及其并列同伴，见下），从核中采样不再有随机性可言——除非核里混进了并列同伴，而那正是 argmax 破并列规则要接管的原因。

**均匀分布的边界情形**（综合实践会手算）：\( V = 8 \)、所有 logit 相等时 \( p_i = 1/8 \)，\( p_{\max} = 1/8 \) 恰好压线。取 `top_p = 1/8`：第一个 token 累计概率 \( 1/8 \ge 1/8 \) 达标，核为单 token。这就是 `effectively_greedy` 用 `<=`（含等号）的原因——等号处核仍是单 token。

**bf16 并列时序**（对照 `tiny_top_p` 测试的场景）：

```text
logits: row[879] = 8.0, row[6941] = 8.0（bf16 下完全相等），其余 0.0，V = 151936
        1/V ≈ 6.58e-6 < p_max ≈ 0.019

top_p = 1e-6 ≤ 1/V：
    若走采样器：并列对进入核边界 → 拒绝采样按随机数挑 → 不同引擎种子可能给出 879 或 6941
    改道 argmax：按下标最低破并列 → 永远 879，与贪心逐种子一致   ← 测试断言的行为
```

#### 4.3.3 源码精读

**回归测试一：tiny top_p 在 bf16 并列下必须与贪心一致。**

- [tests/select_batch.rs:L142-L178](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/tests/select_batch.rs#L142-L178) — `tiny_top_p_routes_to_argmax_even_under_bf16_ties` 全文。注释交代了动机（per-step-philox 迁移的回归）、数学依据（softmax 最大值恒 `>= 1/vocab`）与难点（bf16 顶部并列：argmax 按最低下标破并列，拒绝采样器会按种子挑）。
- [tests/select_batch.rs:L154-L161](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/tests/select_batch.rs#L154-L161) — 场景构造：`vocab = 151_936`（Qwen3 真实词表宽），`row[879] = 8.0` 与 `row[6941] = 8.0`——注释强调 8.0 是 bf16 精确值，两数完全相等，构成**真并列**。注释还点出选大词表是为了匹配模型的 OnlineSoftmax 工作区间。
- [tests/select_batch.rs:L163-L168](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/tests/select_batch.rs#L163-L168) — 第一步先钉住贪心基准：贪心行返回 `[879]`，断言消息写明「argmax 按最低下标破并列」。
- [tests/select_batch.rs:L170-L177](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/tests/select_batch.rs#L170-L177) — 核心断言：`tiny = sampling(1.0, -1, 1e-6)`（注释算清 `1e-6 < 1/vocab ≈ 6.6e-6`），对 64 个不同引擎种子逐一断言 tiny-top_p 行与贪心行输出完全相同——若它掉进采样器，某个种子必然抽出并列同伴 6941。

**回归测试二：路由宽度必须用可发射词表，不是填充后的 arena 宽度。**

- [tests/select_batch.rs:L180-L219](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/tests/select_batch.rs#L180-L219) — `padded_arena_width_does_not_suppress_the_argmax_routing` 全文。注释点名来源：qwen35 把 logits 宽度 tile 对齐到超过可解码词表（#1046）。`decodable = 256`、arena 宽 `vocab = 512`、`top_p = 1/256`。
- [tests/select_batch.rs:L199-L209](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/tests/select_batch.rs#L199-L209) — **对照组**：用 `SampleScratch::new`（selection width = arena 宽 512）时，`1/256 > 1/512`，行被判为非贪心、真走采样器；循环 64 个种子并断言 `sampled_the_peer` 最终变真——这是「采样器在并列下确实随机」的**存在性证明**，也是 4.3.1 论断的实验依据。
- [tests/select_batch.rs:L211-L218](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/tests/select_batch.rs#L211-L218) — **实验组**：`SampleScratch::with_selection_width(&ctx, vocab=512, selection_width=256, 1)` 后，同样的行对全部 64 个种子都稳定返回 `lo`——可发射宽度让 `1/256 <= 1/256` 成立，行回到 argmax 路径。

**路由宽度在实现侧的落点**（呼应 4.2.3）：

- [src/lib.rs:L223-L225](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/src/lib.rs#L223-L225) — 闭包把 `scratch.selection_width` 喂给 `effectively_greedy`；填充列不许移动核判定。

顺带一提，对照「真采样」的边界感：[tests/select_batch.rs:L95-L115](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/tests/select_batch.rs#L95-L115) 的混合批测试里，采样行用 `top_p = 0.5`、`vocab = 16`——注释明说 `0.5` 必须高于 `1/16 = 0.0625` 的 argmax 下限，行才**真的**走采样通道。写测试时选 top_p 也要留意这条地板线。

#### 4.3.4 代码实践

1. **实践目标**：亲手完成核坍缩的纸笔证明，并读懂对照组实验如何证明「采样器在并列下随机」。
2. **操作步骤**：
   - 取 \( V = 8 \)、所有 logit 相等（比如全 0）。写出每个 token 的概率；验证 \( p_{\max} = 1/8 \)。
   - 令 `top_p = 1/8`：按降序累计，第一个 token 累计 \( 1/8 \ge 1/8 \) 即达标，核 = 单 token。再令 `top_p = 1/8 + 10^{-6}`：第一个 token 累计 \( 1/8 < \text{top\_p} \)，核必须再纳入 token——两个候选（均匀分布下任意两个都等价）都满足，核不再单元素。这就是「恰等于」与「略大于」的分界。
   - 把定理的一般形式写一遍：任取一列 logits，用 \( \sum_j e^{x_j} \le V e^{x_{\max}} \) 推出 \( p_{\max} \ge 1/V \)，并注明等号条件（均匀分布）。
   - 读 [tests/select_batch.rs:L199-L209](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/tests/select_batch.rs#L199-L209) 的对照组，回答：为什么这个测试不仅断言「结果 ∈ {lo, hi}」，还**必须**断言 `sampled_the_peer` 在某种子下为真？
3. **需要观察的现象**：纸笔推导中「略大于」情形核大小跳变；对照组断言里 `assert!(sampled_the_peer, ...)` 是硬性要求。
4. **预期结果**：推导成立；对照组若 64 个种子全返回 `lo`，`assert!` 会失败并报「control: an arena-width routing decision puts this row on the sampler」——即测试要求随机性**必须发生**，从而证明路由判据一旦算错宽，行为立刻退化为随机。该测试本身需要 GPU，待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：`vocab = 151_936` 时，`1/vocab` 约是多少？`tiny_top_p` 测试为什么选 `1e-6`？

**答案**：\( 1/151936 \approx 6.58 \times 10^{-6} \)。`1e-6 < 6.58e-6`，满足 `top_p <= 1/vocab`，核坍缩为单 token，行必须走 argmax。若选 `top_p` 在 \( 6.6 \times 10^{-6} \) 与 \( p_{\max} \approx 0.019 \) 之间（比如 `1e-4`），核可能仍很小但**论证不再成立**，谓词正确地不认领它。

**练习 2**：两个测试都把并列设在 `8.0` 对 `8.0`，为什么强调「bf16-exact」？换成 `8.01` 对 `8.0` 会怎样？

**答案**：8.0 的 bf16 表示精确无舍入误差，两行读进设备 arena 后是**真正相等**的 logits，构成真并列。换成 8.01：bf16 在 8.0 附近的间距是 \( 2^3 \times 2^{-7} = 0.0625 \)，8.01 与 8.0 相差 0.01 < 0.0625，两者会舍入到**同一个** bf16 值——还是并列（这恰好说明 bf16 并列多常见）；但如果差值超过 0.0625，就不并列了，argmax 永远选大的，测试就失去了「并列」这个考点。

**练习 3**：`padded_arena_width` 测试里，为什么对照组用 `SampleScratch::new(&ctx, 512, 1)` 而实验组必须用 `with_selection_width(&ctx, 512, 256, 1)`？两个调用各自的 selection width 是多少？

**答案**：`SampleScratch::new` 把 selection width 默认成 vocab（见 [src/lib.rs:L93-L95](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/src/lib.rs#L93-L95)），对照组因此以 512 为判据：`1/256 > 1/512`，行走采样器（对照所欲）。实验组显式传 256：`1/256 <= 1/256`，行回 argmax。两组唯一差异是宽度，排除了其它解释——这是典型的控制变量测试结构。

## 5. 综合实践

**任务**：把 4.2 与 4.3 的边界分析合并成一份「表驱动」CPU 单元测试，为 `effectively_greedy` 覆盖规格要求的五种情形；外加完成 vocab=8 的纸笔验证（4.3.4 已铺垫）。

1. **实践目标**：一个不依赖 GPU 的测试，把 `effectively_greedy` 的两级判定与全部边界一次性钉死。
2. **操作步骤**：
   - 先完成 4.3.4 的纸笔推导（`top_p` 恰等于 / 略大于 `1/vocab` 的核大小跳变）。
   - 在自己克隆的仓库中，向 `src/lib.rs` 的 `#[cfg(test)] mod tests`（[src/lib.rs:L651-L704](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/src/lib.rs#L651-L704)）添加以下测试。**示例代码**（非项目原有代码）：

   ```rust
   use super::{SamplingParams, effectively_greedy};

   /// 表驱动：五种情形一次钉死。non_greedy_base 排除 is_greedy 的两条判据，
   /// 使每行只考验 top_p 分支。
   #[test]
   fn effectively_greedy_table() {
       let non_greedy_base = SamplingParams {
           temperature: 1.0,
           top_k: -1,
           ..SamplingParams::default()
       };
       let with_top_p = |top_p: f32| SamplingParams {
           top_p,
           ..non_greedy_base
       };

       // 情形 1：显式贪心（temperature=0 命中 is_greedy；top_k=1 同理）
       assert!(effectively_greedy(&SamplingParams::default(), 8));
       let topk1 = SamplingParams {
           temperature: 1.0,
           top_k: 1,
           ..SamplingParams::default()
       };
       assert!(effectively_greedy(&topk1, 8));

       // 情形 2：top_p 恰等于 1/vocab —— 均匀分布边界，核仍单 token
       assert!(effectively_greedy(&with_top_p(1.0 / 8.0), 8));

       // 情形 3：top_p 略大于 1/vocab —— 核纳入第二个 token，语义上不是贪心
       assert!(!effectively_greedy(&with_top_p(1.0 / 8.0 + 1e-6), 8));

       // 情形 4：退化 top_p（0 与 NaN）不认领，保守下放采样通道
       assert!(!effectively_greedy(&with_top_p(0.0), 8));
       assert!(!effectively_greedy(&with_top_p(f32::NAN), 8));

       // 情形 5：selection_width 小于 vocab —— 同一 top_p=1/256，
       //        以 256 为宽是贪心（恰压线），以 512 为宽不是（padded 场景）
       assert!(effectively_greedy(&with_top_p(1.0 / 256.0), 256));
       assert!(!effectively_greedy(&with_top_p(1.0 / 256.0), 512));
   }
   ```

   - 运行：`cargo test --release -p pegainfer-sample --lib effectively_greedy -- --nocapture`。
3. **需要观察的现象**：五个情形、八个断言全部通过；特别留意情形 2 与情形 3 之间只差 `1e-6`，判定却翻转——这就是核坍缩的边界。情形 5 的两个断言用同一个 `top_p` 值证明判定随宽度翻转（#1046 的教训浓缩成两行）。
4. **预期结果**：测试通过（`effectively_greedy` 是纯函数，无需 GPU；编译仍需 CUDA 工具链，因为 `pegainfer-kernels` 的 build.rs 编译 `.cu` 源）。待本地验证。
5. **延伸（可选，需 GPU）**：跑一遍两个回归测试，亲眼看对照组里并列同伴被抽出：`cargo test --release -p pegainfer-sample --test select_batch tiny_top_p padded_arena -- --nocapture`。待本地验证。

## 6. 本讲小结

- `SamplingParams` 是前端契约（`pegainfer-frontend`），本 crate 再导出；默认参数（`temperature = 0.0`）即贪心。
- 两级判定：`is_greedy`（显式：`temperature < 1e-5` 或 `top_k == 1`，契约层、不知词表宽度）→ `effectively_greedy`（策略层：追加 `0 < top_p <= 1/selection_width` 的核坍缩判据）。
- 数学核心：\( p_{\max} \ge 1/V \) 恒成立（因 \( \sum e^{x_j} \le V e^{x_{\max}} \)），故 `top_p <= 1/V` 时核只剩 argmax 一个 token；等号在均匀分布处压线仍单 token。
- bf16 尾数只有 7 位，并列最大值常见；argmax 按最低下标破并列（确定），拒绝采样器按随机数挑（不确定）。语义等价贪心的行必须改道 argmax 才能保住「同样请求同样输出」的契约。
- 判定宽度是 `scratch.selection_width`（可发射词表），不是 tile 对齐填充后的 arena 宽度——`tiny_top_p`（含 64 种子循环）与 `padded_arena_width`（含对照组存在性证明）两个回归测试把这两条冻结下来。
- 谓词是保守的：零宽词表、`top_p` 为 0/NaN/inf 一律不认领，下放采样通道；它同时守护三条路径（贪心集合、无种子批式、带种子单行循环）。

## 7. 下一步学习建议

- **下一讲 u2-l2（SampleScratch）**：本讲的 `selection_width` 只是字段一角，下一讲逐缓冲区拆解 `SampleScratch` 的分配布局——`row_indices`、argmax partials、pinned 回读槽位，以及 `with_selection_width` 与 `SampleScratch::new` 的关系。
- **再下一讲 u2-l3（三路分发）**：看 `effectively_greedy` 判定的行号如何流入批式 indexed argmax / FlashInfer 批式采样 / seeded 单行三条通道。
- **源码延伸阅读**：`pegainfer-frontend/src/sampler.rs` 全文（约 40 行，本讲已覆盖全部内容）；以及 [tests/select_batch.rs:L325-L371](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/tests/select_batch.rs#L325-L371) 的 `seeded_rows_replay_independent_of_batch_position`——它展示「语义等价贪心」之外的另一类确定性契约（种子可重放），是 u2-l4 的开场材料。
