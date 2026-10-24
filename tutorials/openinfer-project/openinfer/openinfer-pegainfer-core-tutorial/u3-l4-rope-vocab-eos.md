# rope.rs、decodable_vocab.rs 与 eos.rs：解码链路的小配套

## 1. 本讲目标

学完本讲，你应该能够：

1. 复述 RoPE cos/sin 频率表的生成规则（`inv_freq` 公式与半分裂布局），并用 Python/NumPy 独立实现一份对照实现。
2. 说清 `decodable_vocab` 模块的真实职责：**镜像前端 tokenizer 的合并规则，求最宽可解码 token id，把输出选择约束到该 id 空间的稠密前缀**——它不做 id→文本解码，更不做分词。
3. 说明 EOS 集合从检查点的解析方式（来源优先级、单值/列表、去重与范围校验）及其在请求生命周期（prefill→decode→finish）中的消费位置。

本讲是 u3 单元「装配层」的收尾：u3-l1、u3-l2 讲运行期状态（页池、KV 池），u3-l3 讲权重装配，本讲讲**加载期一次性装配**的三件小配套——位置编码表、词表宽度、停止条件。三个文件合计 555 行，都很小，但任何一个出错，推理输出都会「看起来正常、实际全错」，所以每个文件都把「看起来合法但实际错误」的输入挡在加载期。

## 2. 前置知识

- **RoPE（旋转位置编码）**：Transformer 需要知道 token 的先后顺序。RoPE 的做法是：把第 \(p\) 个位置的查询/键向量按角度旋转后再做点积，使点积只依赖相对位置差。每个频率通道 \(i\) 的旋转角是 \(p \times \text{inv\_freq}_i\)，与具体 token 内容无关——因此 cos/sin 值可以**离线预计算成一张表**，前向时直接查表，这就是 rope.rs 存在的全部理由。
- **rotary_dim 与 frequency_dim 分离**：只旋转向量的一部分叫 partial rotary。此时「旋转多少维」（rotary_dim）和「频率公式分母用多少维」（frequency_dim）是两件事——分母固定用 head_dim（frequency_dim），即使只旋转前 rotary_dim 维。这就是源码注释里「比例式（proportional）」公式与「普通 partial」公式的区别，后面有测试专门锁住这个区别。
- **bf16**：brain float 16，指数位多、尾数位少的半精度浮点。Rust 侧由 `half` crate 提供（[Cargo.toml:L14](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-core/Cargo.toml#L14)），本讲里 cos/sin 表以 bf16 存储。
- **tokenizer 的两份文件**：HuggingFace 格式检查点里，`tokenizer.json` 有主词表（`model.vocab`，token→id）和 `added_tokens` 列表；`tokenizer_config.json` 另有 `added_tokens_decoder`（以 id 字符串为键）。一个 id 能否被解码，是这三处并起来的结果。
- **EOS（end-of-sequence）**：终止 token id。生成循环每采出一个 token 就检查它是否在 EOS 集合里，命中即结束请求。
- **稠密前缀（dense prefix）**：如果可解码的 id 集合恰好是 \(0..N\) 无空洞，就能用「只计算前 N 行 logits」这种行范围手段做约束；有空洞时行范围会漏掉或错放 id，所以必须直接拒绝加载。
- **fail-closed**：解析失败时宁可丢弃/报错，也不带着不完整的信息继续跑。
- 前置讲义：u2-l1 讲过 tensor 门面（`DeviceContext` / `DeviceVec`，真身在 pegainfer-kernels），本讲的 `precompute_rope` 用 `DeviceVec::from_host` 把宿主表搬上 GPU，是那套往返链的直接应用。

## 3. 本讲源码地图

| 文件 | 行数 | 职责 | lib.rs 出口 |
| --- | --- | --- | --- |
| `src/rope.rs` | 232 | RoPE cos/sin 表：宿主侧生成 + 上载 GPU | [src/lib.rs:L12](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-core/src/lib.rs#L12) |
| `src/decodable_vocab.rs` | 161 | 镜像前端 tokenizer 合并，求最宽可解码 id 与 logits 行对齐 | [src/lib.rs:L5](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-core/src/lib.rs#L5) |
| `src/eos.rs` | 162 | 从检查点三处来源解析 EOS 集合，全模型线统一 | [src/lib.rs:L6](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-core/src/lib.rs#L6) |

配套事实（本讲验证过）：

- lib.rs 头注释是「Shared runtime API used by pegainfer model crates」（[src/lib.rs:L1-L15](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-core/src/lib.rs#L1-L15)），13 个 `pub mod` 全部平铺。
- 在 pegainfer-core 内部，`RopeTableSpec` / `precompute_rope` / `decodable_vocab_width` / `aligned_logits_rows` / `eos_token_ids` 这五个符号**只出现在各自文件和 lib.rs 里，没有任何内部调用者**（grep 验证）。它们是纯粹的出口面，消费者在各模型 crate——这正是「core 拥有 API 门面、模型 crate 组合 core」分层的又一例证。
- 辨析：ops 再出口里有名字含 rope 的算子（如 `dflash_qk_norm_rope_into`，见 u2-l3），那是 kernel 侧的融合算子，与 `src/rope.rs` 的表生成没有关系。
- 三个文件的测试都用 `tempfile` 造临时检查点目录，它是 core 唯一的 dev-dependency（[Cargo.toml:L31-L32](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-core/Cargo.toml#L31-L32)）。

## 4. 核心概念与源码讲解

### 4.1 rope.rs：RoPE cos/sin 表的宿主侧生成与上载

#### 4.1.1 概念说明

RoPE 前向只需要每对 (位置, 频率通道) 的 cos/sin 值。这些值与输入无关，只由几何参数决定，所以引擎启动时一次性生成整张表上载 GPU，之后每个请求的前向只读表不计算。

为什么这张表住在 core 而不是各模型 crate？因为所有用 RoPE 的模型线（Qwen、Kimi、GLM 等）的表生成公式完全一致，差异只体现在参数上。把「公式 + 校验 + 布局」收进 core，模型 crate 只需从 config 里抄出四个数。

注意这个模块「小而防御性强」：`RopeTableSpec::validate` 的注释直说动机——每个被拒绝的几何「否则会产出一张看起来格式良好、实际是错的表」。比如 rotary_dim 是奇数时，半分裂布局会静默错位。

#### 4.1.2 核心流程

设 rotary_dim = \(R\)（被旋转的分量数，必须为偶数），frequency_dim = \(F\)（频率公式的分母维度），最大序列长 \(N\)，底数 \(\theta\)。表生成分三步：

1. **频率倒数**（对 \(i = 0, 1, \dots, R/2-1\)）：

\[ \text{inv\_freq}_i \;=\; \frac{1}{\theta^{\,2i/F}} \]

2. **逐位置求角并取 cos/sin**：位置 \(p\)、通道 \(i\) 的旋转角是 \(\text{angle}_{p,i} = p \times \text{inv\_freq}_i\)，先算 f32 的 cos/sin，再转 bf16 存储。
3. **半分裂布局**：表按 \([N \times R]\) 平铺，位置 \(p\) 起始于 \(p \times R\)；把同一个值写两遍——前半 \(i\) 与后半 \(i + R/2\)：

\[ \text{cos}[p,\,i] \;=\; \text{cos}[p,\,i+R/2] \;=\; \mathrm{bf16}\big(\cos(\text{angle}_{p,i})\big) \]

（sin 同理。）这样写是因为 rotate-half 风格的 RoPE 把向量切成前后两半，两半用同一个角度旋转，表里直接复制两份，kernel 里就不用做索引换算。

伪代码：

```text
validate: R>0, R 偶, R<=F, N>0, θ 有限且>0, N*R 不溢出 usize   # 否则加载失败
inv_freq[i] = 1 / θ^(2i/F)            # i ∈ [0, R/2)
for p in 0..N:
    base = p * R
    for i in 0..R/2:
        angle = p * inv_freq[i]
        cos[base+i] = cos[base+i+R/2] = bf16(cos(angle))
        sin[base+i] = sin[base+i+R/2] = bf16(sin(angle))
上载: (cos_dev, sin_dev) = DeviceVec::from_host(cos), DeviceVec::from_host(sin)
```

#### 4.1.3 源码精读

**参数结构**：[src/rope.rs:L9-L17](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-core/src/rope.rs#L9-L17) 定义 `RopeTableSpec`，四个字段各管一件事：`rotary_dim` 驱动表的长度和半分裂；`frequency_dim` 的注释写得很克制——「只作为 `theta^(2i/frequency_dim)` 的分母，不做别的」；`max_seq_len` 决定位置数；`theta` 是底数。

**几何校验**：[src/rope.rs:L22-L42](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-core/src/rope.rs#L22-L42) 的 `validate` 依次拒绝：零/奇数 rotary_dim（L29-L30）、rotary_dim 超过 frequency_dim（L31-L34）、零 max_seq_len（L35）、非有限或非正的 theta（L36-L39），最后用 `checked_mul` 保证表元素总数 \(N \times R\) 不溢出 usize（L40-L41）并把它作为返回值。

**公式本体**：[src/rope.rs:L70-L72](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-core/src/rope.rs#L70-L72) 生成 inv_freq，指数是 `i as f32 * 2.0 / frequency_dim as f32`——注意 f32 算术，后面对拍时要保持同样的精度路径。[src/rope.rs:L74-L87](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-core/src/rope.rs#L74-L87) 是双重循环：先 `vec![bf16::ZERO; total]` 清零，再对每个 (pos, i) 算 `freq = pos as f32 * inv_freq[i]`，取 f32 的 cos/sin 后 `bf16::from_f32` 落位；L84-L87 四行赋值就是「前半与后半写同一个值」的半分裂布局。整个 `tables()`（[src/rope.rs:L60-L93](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-core/src/rope.rs#L60-L93)）是**纯宿主侧计算，不需要 GPU**。

**上载**：[src/rope.rs:L46-L55](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-core/src/rope.rs#L46-L55) 的 `precompute_rope` 是唯一需要 `DeviceContext` 的入口：调用 `spec.tables()` 拿到两份宿主 Vec，再用两个 `DeviceVec::from_host`（L52-L53）分别上载，返回 cos/sin 两个设备向量。这就是 u2-l1 讲的「宿主暂存→H2D」最小形态。

**测试即文档**：测试模块固定 `THETA = 1e6`（[src/rope.rs:L101](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-core/src/rope.rs#L101)），四条测试各锁一个契约：

- [src/rope.rs:L122-L152](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-core/src/rope.rs#L122-L152)：rotary_dim == frequency_dim（512/512）时与单参数公式逐位兼容，且参考值由测试内**重新实现公式**得出，不是二次调用被测函数。
- [src/rope.rs:L154-L183](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-core/src/rope.rs#L154-L183)：比例式 (128, 512) 的表与全量 (512, 512) 的表在前 64 个通道逐位一致、后半必然分歧——两行内容相同但偏移不同，所以恒等式只覆盖前一半。
- [src/rope.rs:L185-L203](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-core/src/rope.rs#L185-L203)：比例式不等于「普通 partial」——只有 \(i=0\)（\(\theta^0\)）与分母无关，从 \(i=1\) 起两个公式立刻分开（pos=1 处 cos 差约 0.109，约 28 个 bf16 ulp）。
- [src/rope.rs:L205-L231](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-core/src/rope.rs#L205-L231)：所有非法几何被拒，包括 `usize::MAX` 触发溢出保护和四种坏 theta。


#### 4.1.4 代码实践：与 Python/NumPy 对拍频率表

**实践目标**：证明你真的理解了公式——用另一种语言独立实现一遍，并与 Rust 版输出对拍。

**操作步骤**：

1. 跑通 core 内的既有契约测试（纯 CPU，不需要 GPU）：

```bash
cargo test --release -p pegainfer-core --lib rope
```

2. 写一份 Python/NumPy 对照实现（**示例代码**，参数取自测试里的 512/512、theta=1e6）：

```python
import numpy as np

R, F, N, theta = 512, 512, 8, 1e6
half = R // 2
# 与 Rust 侧一致：i(转f32) * 2.0 / F(转f32) 作为指数，全部 f32 算术
inv_freq = np.array(
    [np.float32(1.0) / np.float32(np.float32(theta) ** np.float32(2.0 * i / F)) for i in range(half)],
    dtype=np.float32,
)
for pos in range(N):
    angle = np.float32(pos) * inv_freq              # 逐元素 f32
    cos_row, sin_row = np.cos(angle), np.sin(angle)  # f32 的 cos/sin
    print(pos, cos_row[:3], sin_row[:3])
```

3. 想逐位对拍 bf16 表值时，可参照现有测试的写法（tests 模块里直接调用私有方法，见 [src/rope.rs:L112-L120](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-core/src/rope.rs#L112-L120) 的 `tables` 辅助函数），临时加一个打印测试、实验后删除（**示例代码**）：

```rust
#[test]
fn dump_rope_reference_values() {
    let (cos, _sin) = RopeTableSpec {
        rotary_dim: 512,
        frequency_dim: 512,
        max_seq_len: 2,
        theta: 1e6,
    }
    .tables()
    .unwrap();
    println!("cos[pos=1][0..3] = {:?}", &cos[512..515]);
}
```

**需要观察的现象**：f32 层面 cos/sin 与 NumPy 一致到约 6 位有效数字；bf16 表值先舍入到最近可表示值再比较。

**预期结果**：`rope` 过滤出的 4 个测试全部通过；Python 对照值数值吻合。若只有最后一位不同，优先怀疑两侧 `powf` 的 ulp 差（Rust `f32::powf` 与 NumPy 的求幂路径不同源），以角度值和前 6 位有效数字为准。逐位结果**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：为什么 cos 表每一行的后半与前半完全相同？

答案：rotate-half 风格的 RoPE 把向量切成前后两半、用同一个角度旋转。表在 [src/rope.rs:L84-L87](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-core/src/rope.rs#L84-L87) 直接把同一个值写到 `base+i` 与 `base+i+half_dim` 两处，kernel 查表时两个下标共用同一套索引逻辑，省去 kernel 内的偏移换算。

**练习 2**：`{ rotary_dim: 128, frequency_dim: 512 }` 与 `{ rotary_dim: 128, frequency_dim: 128 }` 生成的表差在哪？

答案：inv_freq 的指数分母不同。i=0 时 theta^0 = 1 两边一致；从 i=1 起频率立刻分开（测试 [src/rope.rs:L185-L203](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-core/src/rope.rs#L185-L203) 给出 pos=1 处 cos 差约 0.109、约 28 个 bf16 ulp）。前者是「比例式」公式（分母固定用 head_dim 维度），后者只是把被旋转向量当成独立的小表。

**练习 3**：`max_seq_len = usize::MAX`、`rotary_dim = 128` 为什么在加载期失败而不是 OOM？

答案：`validate` 用 `checked_mul` 计算表元素总数（[src/rope.rs:L40-L41](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-core/src/rope.rs#L40-L41)），乘法溢出 usize 即返回错误，走不到内存分配那一步。

### 4.2 decodable_vocab.rs：可解码词表宽度与 logits 行对齐

#### 4.2.1 概念说明

先纠正模块名带来的直觉偏差：`decodable_vocab` **不做任何 id→文本解码，也不做分词**。文本编解码是前端（pegainfer-frontend）的职责。这个模块回答一个更窄的问题：

> 检查点的 lm_head 有 `vocab` 行，但前端 tokenizer 实际只能解码其中哪些 id？输出投影应该为多少行计算 logits？

头注释（[src/decodable_vocab.rs:L1-L6](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-core/src/decodable_vocab.rs#L1-L6)）说得很清楚：前端从 `tokenizer.json`（vocab + added_tokens）与 `tokenizer_config.json`（added_tokens_decoder）的合并结果里服务；core **镜像**同一套合并，求最宽可解码 token id，把输出选择约束到该 id 空间的稠密前缀。

为什么 core 要自己算而不是去问前端？因为约束最终落在 GEMM 形状和 logits 缓冲区上——那是 core/模型 crate 的地盘。镜像规则必须逐字对齐，否则「前端能解码的 id」与「引擎允许选出的 id」两个集合会漂移。

#### 4.2.2 核心流程

合并与求宽度的流程（三处来源、三种失败语义）：

```text
读 tokenizer.json         # 读失败 / 解析失败 / 空 vocab → 整个加载失败（前端离了它无法服务）
ids = model.vocab 的全部 id
ids ∪= added_tokens[].id
读 tokenizer_config.json   # 文件缺失 → 静默贡献为空
  类型化解析成功 → ids ∪= added_tokens_decoder 里能 parse::<u32>() 的键   # 非数字键逐条跳过
  类型化解析失败 → warn 一条，丢弃全部 decoder tokens（fail-closed，镜像前端）
width = ids.len();  max_id = ids.max()
要求 max_id + 1 == width    # 稠密前缀；有空洞直接让加载失败
```

关键不变量：**可解码 id 空间必须是稠密前缀**。「只算前 width 行 logits」是行范围选择，行范围遮不住中间的洞——有洞时要么采样到不可解码 id、要么把合法 id 排除在采样之外，两者都静默损坏输出，所以宁可加载失败。

第二个函数解决对齐问题：GEMM 的 M 维和 logits 缓冲区首维要取行数，而 Qwen3.5-4B 的 248077 个可解码 id 这类不对齐宽度会迫使 cublasLt 退到 align-1 kernel。于是把宽度上取整到 128 的倍数、再不超过检查点行数（[src/decodable_vocab.rs:L112-L114](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-core/src/decodable_vocab.rs#L112-L114)）；**多算出来的行在选择（argmax/采样）之前必须 mask 掉**。

#### 4.2.3 源码精读

**入口骨架**：[src/decodable_vocab.rs:L69-L104](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-core/src/decodable_vocab.rs#L69-L104) 的 `decodable_vocab_width(model_path: &str) -> Result<usize>`。L70-L77 完成第一段合并：读 + 类型化解析 tokenizer.json（失败即 fatal），断言 vocab 非空，`HashSet<u32>` 收拢 vocab 值与 added_tokens 的 id。

**fail-closed 的 schema 契约**：[src/decodable_vocab.rs:L15-L41](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-core/src/decodable_vocab.rs#L15-L41) 的 `FrontendAddedToken` 有全文件最值得读的注释：这些字段**故意不参与任何逻辑**，存在的唯一意义是让整文件类型化解析在前端 schema 变形（比如 `special` 不再是 bool）时**整份失败**，从而镜像前端「一条坏 → 全部 decoder tokens 丢弃」的行为。这是「用类型形状表达协议」的写法样板。

**第二段合并**：[src/decodable_vocab.rs:L79-L91](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-core/src/decodable_vocab.rs#L79-L91)——config 文件缺失时 `if let Ok` 静默贡献为零；类型化解析失败时 `warn!`（日志原文就是「skipping its added tokens like the frontend does」）；成功时只收能解析为 u32 的键，非数字键逐条跳过。

**稠密性检查**：[src/decodable_vocab.rs:L93-L103](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-core/src/decodable_vocab.rs#L93-L103) 求 `width = ids.len()` 与 `max_id`，用 `max_id + 1 == width` 拒绝稀疏 id 空间，错误信息直接点明原因：「a row-range selection bound cannot mask holes」。

**行对齐**：[src/decodable_vocab.rs:L106-L114](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-core/src/decodable_vocab.rs#L106-L114) 的 `aligned_logits_rows(decodable, vocab)` 单行实现 `decodable.next_multiple_of(128).min(vocab)`，注释交代动机（cublasLt align-1、128-token tile）与义务（多出的行必须 mask 后才能参与选择）。

**测试**：[src/decodable_vocab.rs:L129-L160](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-core/src/decodable_vocab.rs#L129-L160) 三条测试分别锁住：vocab 3 个 + added 1 个 + decoder 数字键 2 个（非数字键 x 跳过）= 宽度 6；vocab {0,1} 加 decoder 键 5 是稀疏空间 → 失败；decoder 里一条 `special: "not-a-bool"` 毒化整份解析 → 全部 decoder tokens 丢弃、宽度退回 2。

#### 4.2.4 代码实践：读测试推演宽度

**实践目标**：不写代码，靠读测试把合并与失败语义内化。

**操作步骤**：

1. 运行并观察三个测试通过：

```bash
cargo test --release -p pegainfer-core --lib decodable_vocab
```

2. 打开 [src/decodable_vocab.rs:L150-L160](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-core/src/decodable_vocab.rs#L150-L160)，回答：为什么 `special: "not-a-bool"` 导致宽度是 **2** 而不是 3（tokenizer.json 的 vocab 2 个 + decoder 里那个格式合法的键 2 哪去了）？
3. 心算推演：把该 fixture 里 decoder 的键 2 换成 8，其余不变——加载成功还是失败？宽度是多少？

**需要观察的现象**：3 个用例的通过状态；你对步骤 3 的推演结论。

**预期结果**：步骤 2——整份 tokenizer_config.json 的类型化解析失败，全部 decoder tokens 丢弃，宽度 = tokenizer.json 侧的 2；步骤 3——失败，vocab {0,1} 加 id 8 后 max_id=8、count=3，稠密性检查不成立。运行结果**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：为什么 tokenizer.json 解析失败是 fatal，tokenizer_config.json 解析失败却只 warn + 跳过？

答案：与被镜像的前端一致——前端没有 tokenizer.json 的主词表根本无法服务（fatal），added_tokens_decoder 只是补充，坏了可以降级（丢弃）。warn 文案也在强调「like the frontend does」。

**练习 2**：`aligned_logits_rows(100, 500)` 与 `aligned_logits_rows(600, 500)` 各返回多少？

答案：128（100 上取整到 128 的倍数，未超过 500）；500（640 被 `min(vocab)` 截回 500）。对齐不能凭空造出超出检查点的行。

**练习 3**：若检查点 lm_head 有 151936 行、可解码宽度 151646，稠密性检查怎么判？多出的行怎么办？

答案：通过——稠密性检查针对**可解码 id 集合内部**无洞（max_id+1 == width），不要求可解码宽度等于检查点行数。多出的行不参与输出选择；走行范围优化时，采样前要把 [151646, 对齐宽度) 的行 mask 掉。（行数为示例数字，用于说明关系。）

### 4.3 eos.rs：检查点的 EOS 集合

#### 4.3.1 概念说明

EOS（end-of-sequence）是生成循环的停止条件：decode 每采出一个 token id，先查它是否在 EOS 集合里，命中即把请求标记为完成、走 finish 路径。头注释（[src/eos.rs:L1](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-core/src/eos.rs#L1)）强调「对每条模型线用同样的方式解析」——各模型检查点的 EOS 声明位置五花八门，core 把它收敛成一个函数，模型 crate 不各写各的。

与前两个模块一样，这也是**加载期一次性装配**：EOS 集合启动时解析一次，之后每个请求的 decode 循环只做集合查询。

#### 4.3.2 核心流程

```text
sources = [ generation_config.json, config.json, config.json 的 text_config 子对象 ]
          # 优先级从高到低；text_config 服务把文本模型参数装在子对象里的复合检查点
对 sources 依序尝试：第一个声明了非 null eos_token_id 的来源胜出
  全部落空 → 加载失败（declares no eos_token_id）
取值：单个 id 或 id 数组；空数组 → 失败
每个条目：必须能转 u32（负数 / 字符串 → 失败），且 < vocab_size（越界 → 失败）
按出现顺序去重，返回 Vec<u32>
```

两个容易忽略的细节：

- `null` 视为「未声明」，继续往低优先级来源找（不是失败，也不是空集）。
- 文件**不存在**（NotFound）是正常情况；文件存在但读失败或解析失败是 fatal——见 `read_optional_json`（[src/eos.rs:L62-L70](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-core/src/eos.rs#L62-L70)）对 `ErrorKind::NotFound` 的特判。

#### 4.3.3 源码精读

**函数契约**：[src/eos.rs:L13-L18](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-core/src/eos.rs#L13-L18) 的文档注释交代完整语义：来源顺序、单值或列表、按序去重，「没声明 / 空列表 / 非 u32 条目 / 越界 id」四类失败全部让加载失败。签名 `eos_token_ids(model_path, vocab_size) -> Result<Vec<u32>>`——vocab_size 由调用方传入，越界检查依赖它。

**来源选择**：[src/eos.rs:L20-L38](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-core/src/eos.rs#L20-L38) 构造三个 (来源名, JSON) 候选，`find_map` 找第一个 `get("eos_token_id")` 存在且非 null 的来源；全部落空时 `bail!` 报「declares no eos_token_id」。

**条目解析**：[src/eos.rs:L40-L59](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-core/src/eos.rs#L40-L59)——数组或单值统一为条目列表；空列表 `ensure!` 失败；每条先 `as_u64` 再 `u32::try_from`（所以 -1 和字符串都落进「is not a u32」）；再 `ensure!` 范围检查；最后 `if !ids.contains(&id)` 去重保序。

**测试**：[src/eos.rs:L97-L133](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-core/src/eos.rs#L97-L133) 用一张表覆盖五种真实加载形态：generation_config 里的列表 [7,5,7] 压过 config 的 6（注释点名 Qwen3、Kimi-K2 的形态）；config 里的列表；`eos_token_id: null` 落穿到 text_config（注释点名 Qwen3.5 不带 generation_config 的形态）；generation_config 存在但缺字段、或字段为 null，都落穿。[src/eos.rs:L135-L161](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-core/src/eos.rs#L135-L161) 锁住五类失败各自的错误文案（declares no / lists no / is not a u32 / outside the vocabulary）。

#### 4.3.4 代码实践：给失败用例找命中的检查行

**实践目标**：把每类失败精确映射到源码里的那一条检查。

**操作步骤**：

1. 运行：

```bash
cargo test --release -p pegainfer-core --lib eos
```

2. 打开 [src/eos.rs:L135-L161](https://github.com/openinfer-project/openinfer/blob/8886aa47b95bc4beedb63584eb367276650b2fec/pegainfer-core/src/eos.rs#L135-L161)，为五个失败用例各写一行「用例 → 命中的检查」：
   - config 什么都没声明
   - `{"eos_token_id": []}`
   - `{"eos_token_id": [3, "eos"]}`
   - `{"eos_token_id": -1}`
   - `{"eos_token_id": 16}`（vocab_size=16）

**需要观察的现象**：测试通过；你的映射表。

**预期结果**：依次命中 L37 的 `bail!`、L44 的 `ensure!`、L47-L50 的 u32 转换（字符串条目）、同一条 u32 转换（负数经 `as_u64` 失败）、L51-L54 的范围 `ensure!`。运行结果**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：generation_config.json 是 `{"eos_token_id": [7, 5, 7]}`，config.json 是 `{"eos_token_id": 6}`，返回什么？

答案：`[7, 5]`——第一个声明的来源获胜，列表按出现顺序去重；config 的 6 根本不会被看。

**练习 2**：generation_config.json 是 `{"eos_token_id": null}`，config.json 是 `{"text_config": {"eos_token_id": 9}}`（顶层无该字段），返回什么？

答案：`[9]`——null 视为未声明，顶层 config 也落空，最后由 text_config 提供。这正是测试注释标为「Qwen3.5 without generation_config」的形态。

**练习 3**：为什么 `eos_token_id: 16` 在 vocab_size = 16 时被拒绝？

答案：合法 id 范围是 [0, 16)，16 已越界——否则解码循环可能指向不存在的词表行。vocab_size 由模型 crate 从自己的 config 读出后传入。

## 5. 综合实践：把三件配套串进 decode 循环

三件配套分别服务请求生命周期的不同时刻：rope 表和词表宽度在**加载期**装配，EOS 集合在加载期解析、在**每步 decode** 消费。本实践用一个伪代码骨架把它们串起来，并审计真实消费点。

**实践目标**：

1. 写出（伪代码即可）一个「逐 token 生成」循环，在正确的位置调用正确的 core 接口。
2. 用 grep 找到模型 crate 侧的真实消费点并填表。

**操作步骤**：

1. 阅读下面这副骨架（**示例伪代码**，接口名均为本讲验证过的 core 公开面；GEMM/采样等算子走 u2-l3 讲过的 ops 再出口，此处只示意位置）：

```text
# ---- 加载期（每个进程一次）----
width   = decodable_vocab::decodable_vocab_width(model_path)          # 词表宽度，稠密前缀
rows    = decodable_vocab::aligned_logits_rows(width, vocab_rows)     # 128 对齐、不超检查点行数
eos_set = eos::eos_token_ids(model_path, vocab_rows)                  # 全模型线统一的 EOS 解析
(cos, sin) = rope::precompute_rope(&ctx, &RopeTableSpec{ ... })       # 表在宿主生成、from_host 上载

# ---- 每个请求：prefill → decode 循环 ----
loop {
    hidden = 前向一步(...)            # 注意力用 (cos, sin) 表做位置编码、读写 KV 池（u3-l1/u3-l2）
    logits = lm_head GEMM，M 维 = rows   # 用 rows 而不是 vocab_rows，避免 cublasLt align-1
    mask 掉 logits 的 [width, rows) 行   # 对齐多算出的行在选择前必须 mask
    next = argmax / 采样，且保证 next < width
    if next ∈ eos_set { 请求 finish; break }
    # 否则把 next 追加进序列，进入下一步 decode
}
```

2. 在**工作区根目录**（pegainfer-core 的上一级）运行下面的 grep，找出模型 crate 里的真实调用点，并把结果填进表格（本讲义基于 core 内部验证撰写，模型 crate 内的文件名以你自己的 grep 结果为准，不要照抄别人的结论）：

```bash
grep -rn "precompute_rope\|RopeTableSpec" pegainfer-qwen3/src
grep -rn "decodable_vocab_width\|aligned_logits_rows" pegainfer-qwen3/src
grep -rn "eos_token_ids" pegainfer-qwen3/src
```

   | core 接口 | 模型 crate 消费文件（grep 结果） | 生命周期阶段 |
   | --- | --- | --- |
   | `precompute_rope` | 待填 | 加载期 |
   | `decodable_vocab_width` / `aligned_logits_rows` | 待填 | 加载期 + 每步 decode |
   | `eos_token_ids` | 待填 | 加载期解析，decode 判停 |

   想扩大范围可对其他模型 crate（目录名见 CLAUDE.md 的 crate 表：pegainfer-qwen35、pegainfer-kimi-k2 等）重复同样的 grep。

**需要观察的现象**：每条 grep 命中的文件与行号；它们出现在加载路径还是调度器/executor 的 decode 路径。

**预期结果**：三个模块的消费点都应在模型 crate 的「模型装配/调度」一侧，且 decodable 宽度会一路传到输出投影与采样约束处。具体文件名**以 grep 结果为准**（本讲不预设）；若某条 grep 在某个模型 crate 里没有命中，说明该 crate 未走 core 的这条公共路径，值得把发现记进你的笔记。

## 6. 本讲小结

- `rope.rs`：RoPE 表在宿主侧按公式 \(\text{inv\_freq}_i = \theta^{-2i/F}\) 生成、以 bf16 半分裂布局（前半 = 后半）平铺，再经 `DeviceVec::from_host` 一次性上载；`validate` 把「看似合法、实际错误」的几何全部挡在加载期。rotary_dim（旋转多少维）与 frequency_dim（频率分母）是两个独立参数。
- `decodable_vocab.rs` 的职责是**求宽度，不是做解码**：镜像前端 tokenizer 三处来源的合并规则，要求可解码 id 构成稠密前缀（行范围选择遮不住洞），并把 logits 行数对齐到 128 的倍数、采样前 mask 多出的行。
- `eos.rs`：按 generation_config → config → text_config 的优先级取第一个非 null 声明，单值/列表皆可、按序去重、必须 u32 且在词表内；null 视为未声明，文件缺失正常、解析失败 fatal。
- 三者都是加载期一次性装配 + 纯出口面：core 内部没有任何调用者（grep 验证），消费者在各模型 crate。
- 三个模块的测试全部是纯 CPU 测试（rope 的 `tables()` 不碰 GPU；另两个用 tempfile 造临时检查点），`cargo test --release -p pegainfer-core --lib` 即可运行。

## 7. 下一步学习建议

- 回顾 u2-l1 的 tensor 门面，`precompute_rope` 的两个 `DeviceVec::from_host` 是那套「宿主→设备」往返链的最小真实用例。
- 如果 `ls openinfer-pegainfer-core-tutorial/` 里已有 u3-l2（kv_pool）讲义，结合本讲的 (cos, sin) 表消费点去看 prefill 注意力如何同时用「表」和「KV 池」。
- u4-l1（cuda_graph.rs）将讲解码路径的图捕获：本讲的「加载期一次性装配」思想在那里推到极致——连 kernel 启动序列本身也被预录制。
- 动手方向：对拍完 rope 表后，可以试着自己推一遍 `aligned_logits_rows` 在一个真实检查点上的两个输入值（从 config.json 的 vocab_size 和 tokenizer 的合并宽度出发），再用模型 crate 的 grep 命中点验证你的推断。
