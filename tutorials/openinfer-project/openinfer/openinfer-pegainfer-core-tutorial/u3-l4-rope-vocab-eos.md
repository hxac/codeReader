# rope.rs、decodable_vocab.rs 与 eos.rs：解码链路的小配套

## 1. 本讲目标

学完本讲你应该能够：

- 复述 `rope.rs` 生成 RoPE cos/sin 表的精确公式（含 half-split 布局），并能用 Python/NumPy 独立实现一份对照版本，与 Rust 版输出对拍。
- 解释 `decodable_vocab.rs` 的真实职责：它**不做** id→文本解码，而是镜像 frontend 的分词器 schema 合并，求出「可解码词表宽度」，并把这个宽度变成输出投影 GEMM 的行数约束（含 128 对齐）。
- 说明 `eos.rs` 从检查点解析 EOS 集合的三级回退顺序、全部失败模式，以及它在请求生命周期（prefill → decode → finish）中的消费位置。
- 把三件事串成一条完整链路：**位置编码表 → 前向输出 token id → 词表宽度约束与 EOS 停止**。

## 2. 前置知识

### 位置编码与 RoPE

Transformer 的注意力本身「不知道」词的先后顺序，必须额外注入位置信息。RoPE（Rotary Position Embedding，旋转位置编码）的做法是：把每个注意力头的向量切成若干「二维对」，每对看作复平面上的一个点，按位置乘上一个角度旋转。位置越靠后，旋转角越大。两个位置做注意力内积时，各自的角度差恰好等于相对位置差——这就是「相对位置编码」的直觉。

旋转角 \(\phi\) 只依赖位置和频率，与前向数据无关，所以可以**提前一次性算好整张 cos/sin 表**，前向时 kernel 直接查表，不必在 GPU 上反复算三角函数。

### token id、logits 与采样

模型每一步的输出不是文字，而是一个整数 token id。最后一层输出投影（lm_head）是一个大矩阵乘（GEMM）：行数等于词表行数 \(V\)，对每个候选 token 算出一个分数。这些分数叫 logits；从 logits 里选出一个 id 的过程叫采样（贪心 argmax 是最简单的一种）。

### 检查点目录里有什么

一个模型目录（如 `models/Qwen3-4B`）里，本讲相关的文件有：

| 文件 | 内容 | 本讲的消费者 |
| --- | --- | --- |
| `tokenizer.json` | 分词器主 schema：`model.vocab`（token→id）与 `added_tokens` | `decodable_vocab.rs` |
| `tokenizer_config.json` | 分词器配置：`added_tokens_decoder` 等 | `decodable_vocab.rs` |
| `generation_config.json` | 生成参数，可能含 `eos_token_id` | `eos.rs` |
| `config.json` | 模型几何；多模态检查点可能把文本模型藏在 `text_config` 里 | `eos.rs` |

而 RoPE 表的参数（`rotary_dim`、`theta` 等）来自模型 config 的解析，由模型 crate 传入——`rope.rs` 只认一个显式的几何结构体。

### 承接 u2-l1：tensor 门面

本讲会用到 u2-l1 讲过的两个事实：`crate::tensor` 是 `pegainfer_kernels::tensor` 的一行再出口门面；`DeviceVec::from_host(ctx, &host)` 是「宿主向量 → 一次 H2D 拷贝 → 设备侧 RAII 缓冲」的入口。`rope.rs` 正是 core 内部消费这两个类型的一个典型样本。

## 3. 本讲源码地图

三个文件都位于 `pegainfer-core/src/`，都在 [src/lib.rs:3-15](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/lib.rs#L3-L15) 的 `pub mod` 清单里声明（`decodable_vocab` 在 [src/lib.rs:5](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/lib.rs#L5)、`eos` 在 [src/lib.rs:6](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/lib.rs#L6)、`rope` 在 [src/lib.rs:12](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/lib.rs#L12)）。

| 文件 | 行数 | 公开面（全部） | 一句话职责 |
| --- | --- | --- | --- |
| `src/rope.rs` | 232 | `RopeTableSpec`、`precompute_rope` | 宿主生成 RoPE cos/sin 表并一次 H2D 上传 |
| `src/decodable_vocab.rs` | 161 | `decodable_vocab_width`、`aligned_logits_rows` | 镜像 frontend 分词器合并，求可解码词表宽度与对齐行数 |
| `src/eos.rs` | 162 | `eos_token_ids` | 按统一三级回退解析检查点的 EOS id 集合 |

三者的共性（在本讲撰写环境中实际 grep 验证过：`grep -rn "crate::rope\|crate::decodable_vocab\|crate::eos" --include="*.rs" src tests` 无任何匹配）：

- **core 内部没有任何消费者**——它们是纯出口模块，只被模型 crate 使用。
- 都在**装配期（加载模型时）执行一次**，不在 decode 热路径上。
- 都采用 **fail-fast** 纪律：检查点/分词器里缺了什么、格式不对，直接让加载失败，绝不静默猜一个默认值。

> 待确认：三个接口在模型 crate（`pegainfer-qwen3` 等）中的具体调用点，本讲撰写环境无法访问兄弟 crate 目录（会话只授权了 `pegainfer-core`），相关行号留作本讲实践任务，由读者本地 grep 回填。

## 4. 核心概念与源码讲解

### 4.1 rope.rs：RoPE cos/sin 表的生成与上传

#### 4.1.1 概念说明

RoPE 需要一张「位置 × 频率」的 cos/sin 查找表。这张表有三个特点决定了它的实现形态：

1. **纯常量**：只依赖几何参数（旋转维度、基数 theta、最大长度），与输入数据、与权重都无关。所以宿主侧一次算好、量化成 bf16、一次 H2D 上传、常驻显存即可，GPU 上不必重算。
2. **half-split 布局**：旋转以「一对维度」为单位。half-split 风格把向量的前半与后半配对旋转，所以表把每个 cos/sin 值**在前半和后半各写一份**，kernel 对两段各自用同一个下标 \(i\) 查表。
3. **两个独立的维度参数**：`rotary_dim`（实际被旋转的分量数）与 `frequency_dim`（频率公式的分母）是两回事。分母用 `frequency_dim`（通常是整个 head_dim）而旋转只用前 `rotary_dim` 维——这就是所谓 proportional RoPE，与「rotary_dim 当分母」的普通 partial RoPE 是**不同的表**（源码测试专门钉死了这一点）。

#### 4.1.2 核心流程

```text
RopeTableSpec { rotary_dim: r, frequency_dim: f, max_seq_len: n, theta }
        |
        v  validate()                      <- 拒绝一切「算出来看着合法其实错」的几何
        |  返回 total = n * r（usize 乘法溢出检查）
        v  tables()                        <- 纯 CPU：f32 算 cos/sin -> 量化 bf16
        |     inv_freq[i] = theta^(-2i/f)            （i = 0 .. r/2-1）
        |     对每个 pos：angle = pos * inv_freq[i]
        |     half-split：值写 [pos*r + i] 与 [pos*r + i + r/2] 两个槽
        v  precompute_rope(ctx, spec)
        |     DeviceVec::from_host x 2      <- cos 表、sin 表各一次 H2D
        v
(DeviceVec<cos>, DeviceVec<sin>)            <- 常驻显存，前向只读
```

数学上，表值由两条公式给出：

\[ \mathrm{inv\_freq}[i] = \theta^{-\,2i/F}, \quad i = 0, 1, \ldots, r/2 - 1 \]

\[ \phi_{\text{pos},i} = \text{pos} \cdot \mathrm{inv\_freq}[i] \]

\[ \cos_{\text{host}}[\text{pos} \cdot r + i] = \cos_{\text{host}}[\text{pos} \cdot r + i + r/2] = \cos(\phi_{\text{pos},i}) \]

其中 sin 表同形。注意分母是 \(F\)（`frequency_dim`），不是 \(r\)。

#### 4.1.3 源码精读

**几何参数结构体。** [src/rope.rs:9-17](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/rope.rs#L9-L17) 定义 `RopeTableSpec`，字段注释直接说明了两个维度参数的分工：`rotary_dim` 驱动表的长度与 half-split，`frequency_dim` 「只做 `theta^(2i / frequency_dim)` 的分母，不做别的」。

```rust
pub struct RopeTableSpec {
    /// Components that get rotated. Drives the cache extent and the half-split.
    pub rotary_dim: usize,
    /// Denominator of `theta^(2i / frequency_dim)`, and nothing else.
    pub frequency_dim: usize,
    pub max_seq_len: usize,
    pub theta: f32,
}
```

**校验先行。** [src/rope.rs:19-43](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/rope.rs#L19-L43) 的 `validate()` 在算任何三角函数之前拒绝不可用几何，注释（[src/rope.rs:20-21](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/rope.rs#L20-L21)）说得很直白：**每一种被拒绝的情况，若放行都会产出一张「看起来格式完好、实际是错的」表**：

- `rotary_dim` 为正、为偶数（[src/rope.rs:29-30](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/rope.rs#L29-L30)）——旋转按维度对进行，奇数会让 half 截断错位；
- `rotary_dim <= frequency_dim`（[src/rope.rs:31-34](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/rope.rs#L31-L34)）；
- `max_seq_len` 为正、`theta` 有限且为正（[src/rope.rs:35-39](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/rope.rs#L35-L39)）；
- `n * r` 用 `checked_mul` 防溢出（[src/rope.rs:40-42](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/rope.rs#L40-L42)），返回的 `total` 就是两张表各自的元素个数。

**上传入口。** [src/rope.rs:45-55](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/rope.rs#L45-L55) 是模块对外的唯一函数：宿主算表，然后 cos、sin 各做一次 `DeviceVec::from_host`（H2D），返回两个设备侧缓冲：

```rust
pub fn precompute_rope(
    ctx: &DeviceContext,
    spec: &RopeTableSpec,
) -> Result<(DeviceVec, DeviceVec)> {
    let (cos_host, sin_host) = spec.tables()?;
    Ok((
        DeviceVec::from_host(ctx, &cos_host)?,
        DeviceVec::from_host(ctx, &sin_host)?,
    ))
}
```

**表生成本体。** [src/rope.rs:68-72](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/rope.rs#L68-L72) 先算 `half_dim` 与逆频率序列，公式就是上面的 theta^(-2i/F)：

```rust
let half_dim = rotary_dim / 2;

let inv_freq: Vec<f32> = (0..half_dim)
    .map(|i| 1.0 / theta.powf(i as f32 * 2.0 / frequency_dim as f32))
    .collect();
```

[src/rope.rs:77-89](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/rope.rs#L77-L89) 双层循环填表：外层位置、内层频率对，`f32` 算 cos/sin 后经 `bf16::from_f32` 量化（[src/rope.rs:81-82](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/rope.rs#L81-L82)），再按 half-split 写进两个槽位（[src/rope.rs:83-87](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/rope.rs#L83-L87)）：

```rust
// Half-split layout: [cos(0)..cos(half-1), cos(0)..cos(half-1)]
cos_host[base + i] = cos_val;
cos_host[base + i + half_dim] = cos_val;
sin_host[base + i] = sin_val;
sin_host[base + i + half_dim] = sin_val;
```

**测试钉死的行为。** 文件后半（[src/rope.rs:95-232](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/rope.rs#L95-L232)）是纯 CPU 单测（不需要 GPU；`tables()` 是私有方法，测试写在同文件的 `mod tests` 里）：

- `rope_tables_backward_compatible_with_single_param`（[src/rope.rs:122-152](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/rope.rs#L122-L152)）：`rotary_dim == frequency_dim` 时与「单参数公式」逐位（`to_bits`）一致——这是旧模型的兼容锚点。
- `rope_tables_proportional_matches_full_rope_leading_half`（[src/rope.rs:154-183](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/rope.rs#L154-L183)）：proportional 表（128/512）的前 64 个条目与全维表（512/512）对应行的前缀一致。
- `rope_tables_proportional_is_not_ordinary_partial`（[src/rope.rs:185-203](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/rope.rs#L185-L203)）：分母不同的两种表从 i=1 起立刻分离（注释给出量化数字：pos=1 处 cos 差约 0.109，约 28 个 bf16 ulp）。
- `rope_spec_rejects_unusable_geometry`（[src/rope.rs:205-231](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/rope.rs#L205-L231)）：把 `validate()` 的每条拒绝路径各打一枪，包括 `theta` 为 0、负数、NaN、无穷。

#### 4.1.4 代码实践：用 Python/NumPy 复现并对拍

1. **实践目标**：证明你真的理解了公式与 half-split 布局——独立写出一份 Python 实现，与 Rust 版逐值对拍。
2. **操作步骤**：
   - 第一步，写 Python 参考实现（**示例代码**，非项目原有代码）：

     ```python
     import numpy as np

     def rope_tables(rotary_dim, frequency_dim, max_seq_len, theta):
         half = rotary_dim // 2
         i = np.arange(half, dtype=np.float64)
         inv_freq = 1.0 / np.power(theta, 2.0 * i / frequency_dim)
         cos = np.zeros((max_seq_len, rotary_dim), dtype=np.float32)
         sin = np.zeros((max_seq_len, rotary_dim), dtype=np.float32)
         for pos in range(max_seq_len):
             phi = pos * inv_freq          # 角度 = pos * inv_freq[i]
             c, s = np.cos(phi), np.sin(phi)
             cos[pos, :half] = c           # half-split：前半
             cos[pos, half:] = c           # half-split：后半复制同一份
             sin[pos, :half] = s
             sin[pos, half:] = s
         return cos, sin
     ```

   - 第二步，让 Rust 版把表值吐出来。`tables()` 是私有函数，所以 dump 测试必须写在 `src/rope.rs` 已有的 `#[cfg(test)] mod tests`（[src/rope.rs:95-110](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/rope.rs#L95-L110)）内部，那里已有 `spec()` 辅助函数可直接复用（**示例代码**；这是本地临时实验，验证完请删除，不要提交）：

     ```rust
     #[test]
     fn dump_tables_for_cross_check() {
         let (cos, _sin) = spec(128, 128, 4).tables().unwrap();
         for pos in 0..4 {
             let row: Vec<String> = (0..8)
                 .map(|i| format!("i={i}:{:.6}", cos[pos * 128 + i].to_f32()))
                 .collect();
             println!("pos={pos} {}", row.join(" "));
         }
     }
     ```

   - 第三步，运行并抓输出：`cargo test --release -p pegainfer-core --lib dump_tables_for_cross_check -- --nocapture`（纯 CPU，不需要 GPU）。
   - 第四步，Python 侧用同样参数（`rotary_dim=128, frequency_dim=128, max_seq_len=4, theta=1e6`，1e6 来自测试常量 [src/rope.rs:101](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/rope.rs#L101)）打印对应位置，逐值比对。
3. **需要观察的现象**：
   - `pos=0` 行：cos 全为 1.0，sin 全为 0.0（角度为 0）；
   - 任意行的后半段与前半段逐值相同（half-split 复制）；
   - 随 `i` 增大，`inv_freq[i]` 递减，同位置上角度变化越来越慢；
   - 由于 Rust 版量化到 bf16（约 3 位十进制有效数字），两侧不是逐位相等，差异应落在 bf16 舍入误差内（相对误差量级 \(2^{-8}\)）。
4. **预期结果**：在 bf16 舍入容差内，两侧表值一致。**待本地验证**（本讲撰写环境未执行上述对拍）。

#### 4.1.5 小练习与答案

**练习 1**：`rotary_dim` 为什么必须是偶数？
**答案**：旋转以「一对维度」为单位：`half_dim = rotary_dim / 2`（[src/rope.rs:68](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/rope.rs#L68)），half-split 布局把每个值写在前半第 `i` 槽与后半第 `i + half_dim` 槽（[src/rope.rs:84-87](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/rope.rs#L84-L87)）。奇数 `rotary_dim` 会让整数除法截断、后半错位，产出一张「看似合法实则错误」的表，所以 `validate()` 在 [src/rope.rs:30](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/rope.rs#L30) 直接拒绝。

**练习 2**：proportional RoPE（`rotary_dim=128, frequency_dim=512`）与普通 partial RoPE（`rotary_dim=128, frequency_dim=128`）的表差在哪？
**答案**：只有 i=0 相同（theta^0=1，与分母无关）；从 i=1 起两套 `inv_freq` 立刻分离，pos=1、i=1 处 cos 差约 0.109（约 28 个 bf16 ulp）。测试 `rope_tables_proportional_is_not_ordinary_partial`（[src/rope.rs:185-203](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/rope.rs#L185-L203)）同时断言了「i=0 必须相等」和「i=1 必须不等」。

**练习 3**：把表的生成放宿主、一次上传，相比让 GPU kernel 每步现算 cos/sin，有什么收益？
**答案**：表只依赖几何参数，是纯常量——一次生成、一次 H2D、常驻复用（[src/rope.rs:45-55](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/rope.rs#L45-L55)）；宿主用 f32 计算后统一量化 bf16（[src/rope.rs:81-82](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/rope.rs#L81-L82)），每步前向只做查表，省掉 decode 热路径上的重复三角函数计算。

### 4.2 decodable_vocab.rs：可解码词表宽度

#### 4.2.1 概念说明

这个模块的名字容易误导：它**不做分词、不做 id→文本解码**——那是 frontend 的事。头注释（[src/decodable_vocab.rs:1-6](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/decodable_vocab.rs#L1-L6)）写明了它的真实职责：frontend 实际可解码的 token 集合是 `tokenizer.json`（`vocab` + `added_tokens`）与 `tokenizer_config.json`（`added_tokens_decoder`）的**并集**；core 镜像这次合并，算出**最宽可解码 token id**，再把输出选择约束到该 id 空间的**稠密前缀**。

为什么值得做：检查点的输出投影有 V 行（vocab 行数），但 frontend 真正能解码的 id 可能只有前 `decodable` 个。输出投影 GEMM 只需算这个前缀，其余行根本不用算——省一大块矩阵乘。doc 注释举了实例：Qwen3.5-4B 只解码 248077 个 id（[src/decodable_vocab.rs:106-111](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/decodable_vocab.rs#L106-L111)）。

两个关键设计约束：

- **fail-closed 镜像 frontend**：`tokenizer_config.json` 解析失败时，frontend 会丢弃全部 decoder tokens；core 用「整文件类型化解析」达到同样效果——任何一个条目畸形（例如 `special` 不是 bool），整个文件解析失败，全部 decoder tokens 丢弃并发 warn（[src/decodable_vocab.rs:87-89](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/decodable_vocab.rs#L87-L89)）。宁可少算也不能与 frontend 的解码集合不一致。
- **必须稠密**：输出选择用的是「行范围」截断，**表达不了洞**。若 id 空间有洞（如 {0,1,5}），行范围会把 5 永远挡在外面、输出空间被静默截断——所以直接让加载失败（[src/decodable_vocab.rs:98-102](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/decodable_vocab.rs#L98-L102)）。

#### 4.2.2 核心流程

```text
model_path/tokenizer.json          <- 必须可读、可解析、vocab 非空（失败 = 致命错误）
   model.vocab 的全部 id 值
   + added_tokens[].id
                +
model_path/tokenizer_config.json   <- 可选；整文件解析失败 -> warn + 全部丢弃
   added_tokens_decoder 的数字 key（非数字 key 逐条跳过）
                v
        HashSet<u32> ids
                v
   width = ids.len()；max_id = max(ids)
   要求 max_id + 1 == width（id 空间是稠密前缀，否则报错）
                v
   aligned_logits_rows(decodable, vocab)
   = decodable.next_multiple_of(128).min(vocab)
```

#### 4.2.3 源码精读

**一个「故意死掉」的 schema。** [src/decodable_vocab.rs:15-41](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/decodable_vocab.rs#L15-L41) 定义 `FrontendAddedToken`，doc 注释（[src/decodable_vocab.rs:15-23](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/decodable_vocab.rs#L15-L23)）值得整段读懂：这是一个显式拥有的 frontend 兼容契约，字段**没有被任何代码读取**（所以挂着 `#[allow(dead_code)]`），它们存在的唯一意义是「塑造解析」——让 `special: "not-a-bool"` 这类畸形条目使整文件类型化解析失败，从而镜像 frontend 的丢弃行为。

**两份输入的 schema。** [src/decodable_vocab.rs:43-48](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/decodable_vocab.rs#L43-L48) 对应 `tokenizer.json`（其 `model.vocab` 是 token→id 的 `HashMap<String, u32>`，见 [src/decodable_vocab.rs:50-53](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/decodable_vocab.rs#L50-L53)），[src/decodable_vocab.rs:55-59](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/decodable_vocab.rs#L55-L59) 对应 `tokenizer_config.json` 的 `added_tokens_decoder`（key 是字符串形式的 id）。

**合并主函数。** [src/decodable_vocab.rs:69-104](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/decodable_vocab.rs#L69-L104) 的 `decodable_vocab_width` 分三步：

1. 读并解析 `tokenizer.json`，vocab 非空；收集 `model.vocab` 的全部 id 值和 `added_tokens` 里的 id（[src/decodable_vocab.rs:70-77](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/decodable_vocab.rs#L70-L77)）——这一步失败是致命的（frontend 离开它无法服务）。
2. 尝试 `tokenizer_config.json`：解析成功则并入数字 key（非数字 key 用 `filter_map(... .ok())` 逐条跳过）；解析失败只 warn 并跳过（[src/decodable_vocab.rs:79-91](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/decodable_vocab.rs#L79-L91)），warn 文案明说「like the frontend does」。
3. 稠密性检查：`width = ids.len()`、`max_id = max(ids)`，要求 `max_id + 1 == width`（[src/decodable_vocab.rs:93-103](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/decodable_vocab.rs#L93-L103)），错误信息直说原因——行范围选择 bound 掩盖不了洞。

**对齐行数。** [src/decodable_vocab.rs:106-114](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/decodable_vocab.rs#L106-L114) 的 `aligned_logits_rows` 只有一行实质逻辑，但 doc 信息量很大：GEMM 的 M 维和 logits buffer 的主维都取这个宽度；未对齐的宽度（如 248077）会把 cuBLASLt 逼到 align-1 kernel，所以向上取整到 128 的倍数、再以检查点行数封顶；**超出 `decodable` 的行必须在选择前 mask 掉**（这是消费者的责任，不是本函数做的）：

```rust
pub fn aligned_logits_rows(decodable: usize, vocab: usize) -> usize {
    decodable.next_multiple_of(128).min(vocab)
}
```

**测试。** [src/decodable_vocab.rs:116-161](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/decodable_vocab.rs#L116-L161) 用 `tempfile` 临时目录造 fixture（纯 CPU，不需要 GPU）：三个测试分别钉死「合并结果 = 稠密宽度 6」（[src/decodable_vocab.rs:129-139](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/decodable_vocab.rs#L129-L139)，含一条非数字 key 被跳过）、「稀疏 id 空间失败」（[src/decodable_vocab.rs:141-148](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/decodable_vocab.rs#L141-L148)）、「一条坏 decoder 条目丢掉全部 decoder tokens」（[src/decodable_vocab.rs:150-160](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/decodable_vocab.rs#L150-L160)，宽度回落到 2）。

#### 4.2.4 代码实践：跑通并手工推演三个测试

1. **实践目标**：用现有单测验证你对合并规则、fail-closed 与稠密约束的理解。
2. **操作步骤**：
   - 运行：`cargo test --release -p pegainfer-core --lib decodable_vocab -- --nocapture`（纯 CPU，应当全绿；**待本地验证**——本讲撰写环境未执行）。
   - 打开 [src/decodable_vocab.rs:129-139](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/decodable_vocab.rs#L129-L139)，合上讲义手工推演 fixture：vocab 给了 {0,1,2}，`added_tokens` 给 id 3，decoder keys 给 "4"、"5" 和非数字 "x"。自己算：ids = {0..5}，width = 6，max_id = 5，5 + 1 == 6，通过。
   - 再对 [src/decodable_vocab.rs:150-160](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/decodable_vocab.rs#L150-L160) 推演：id 2 的条目合法、id 3 的 `special` 是字符串 → 整文件解析失败 → decoder tokens 全丢 → 宽度回落为 2。
3. **需要观察的现象**：测试通过；手工推演与断言值（6 和 2）一致。
4. **预期结果**：三个测试全绿，两次推演与断言吻合。
5. 若本地有真实模型目录（如 `models/Qwen3-4B`），可加一步（可选）：用 `jq '[.model.vocab[], .added_tokens[]?.id] | length' tokenizer.json` 粗看 id 数量，体会真实词表的规模。**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：为什么一条畸形 decoder 条目会丢掉**全部** decoder tokens，而不是只跳过那一条？
**答案**：因为 frontend 就是这么做的，core 必须镜像（[src/decodable_vocab.rs:61-68](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/decodable_vocab.rs#L61-L68) 的 doc 与 [src/decodable_vocab.rs:87-89](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/decodable_vocab.rs#L87-L89) 的 warn 文案）。实现上靠「整文件类型化解析 + 字段 schema 塑形」：`FrontendAddedToken` 的字段虽不被读取，但保证畸形条目让整个 `TokenizerConfigIds` 解析失败（fail-closed），随后 match 到 `Err` 分支整体跳过。

**练习 2**：Qwen3.5-4B 的 decodable 宽度是 248077，`aligned_logits_rows(248077, vocab)` 返回多少？为什么要取整？
**答案**：248192（248077 = 128×1938 + 13，向上进一块 → 128×1939 = 248192）。因为输出投影 GEMM 的 M 维和 logits buffer 的主维都取这个宽度，未对齐的奇数宽度会让 cuBLASLt 落到 align-1 kernel（[src/decodable_vocab.rs:106-111](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/decodable_vocab.rs#L106-L111)）；`.min(vocab)` 保证不超检查点实际行数。

**练习 3**：若 ids 集合是 {0, 1, 5}，`decodable_vocab_width` 会怎样？
**答案**：报错、加载失败。width = 3 而 max_id = 5，`max_id + 1 == width` 不成立（[src/decodable_vocab.rs:98-102](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/decodable_vocab.rs#L98-L102)），错误信息明说「id space is not dense」——行范围选择表达不了洞，静默截断输出空间是不可接受的。测试 [src/decodable_vocab.rs:141-148](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/decodable_vocab.rs#L141-L148) 钉死此行为。

### 4.3 eos.rs：检查点的 EOS 集合

#### 4.3.1 概念说明

EOS（end of sequence）是「生成到此为止」的特殊 token。decode 循环每步采样出一个 token id，若命中 EOS 集合，请求就 finish。有的检查点有**多个** EOS（`eos_token_id` 是数组），有的把生成参数放进 `generation_config.json`，有的只在 `config.json`（甚至其嵌套的 `text_config`）里声明。

`eos.rs` 的价值在于**统一**：头注释（[src/eos.rs:1](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/eos.rs#L1)）明说「The checkpoint's EOS set, resolved the same way for every model line」——所有模型线共用这一份解析，避免每个模型 crate 各写一份、彼此漂移。

它同样贯彻 fail-fast：一个不声明 EOS、声明了空列表、条目不是 u32、或 id 越界出词表的检查点，直接让加载失败（[src/eos.rs:13-17](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/eos.rs#L13-L17)）——因为一个永远停不下来的推理请求比一次加载失败糟糕得多。

#### 4.3.2 核心流程

```text
按顺序找第一个「声明了非 null eos_token_id」的来源：
  1. generation_config.json
  2. config.json
  3. config.json 的 text_config          <- 嵌套配置（多模态系）
  都没声明 -> bail（加载失败）
        v
字段形状归一：单值 -> [单值]；数组 -> 逐项
        v
空列表 -> bail
逐项校验：
  必须能转 u32（负数、字符串、超范围都失败）
  必须 < vocab_size（越界失败）
  去重保序（Vec::contains）
        v
Vec<u32>（按首次出现顺序、去重后的 EOS id 集合）
```

#### 4.3.3 源码精读

**签名与契约。** [src/eos.rs:18](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/eos.rs#L18) 的 `eos_token_ids(model_path, vocab_size)` 接收检查点目录和词表行数——后者用于越界校验，由调用方（模型 crate）从 config 解析后传入。

**三级回退表达成数据。** [src/eos.rs:20-32](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/eos.rs#L20-L32) 把三个来源装进一个 `sources` 数组（带可读的来源名，报错时能说清是哪个文件），[src/eos.rs:33-38](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/eos.rs#L33-L38) 用 `find_map` 取第一个命中者；注意 `null` 被显式当作「未声明」继续回退：

```rust
let Some((source, field)) = sources.into_iter().find_map(|(source, json)| {
    let field = json?.get("eos_token_id")?;
    (!field.is_null()).then_some((source, field))
}) else {
    bail!("{} declares no eos_token_id", model_path.display());
};
```

**形状归一与逐项校验。** [src/eos.rs:40-58](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/eos.rs#L40-L58)：单值包装成单元素数组；空列表 bail；每个条目走 `as_u64()` + `u32::try_from`（负数和字符串在这里死掉，错误信息带来源与原值）；越界检查 `id < vocab_size`；最后 `if !ids.contains(&id)` 去重并保持首次出现顺序。

**可选文件的读取。** [src/eos.rs:62-70](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/eos.rs#L62-L70) 的 `read_optional_json` 三分支：解析成功返回 `Some`；文件不存在（`ErrorKind::NotFound`）返回 `None`——这是回退链能继续的前提；其他 IO 错误（如权限）则是致命错误。

**测试即模型线名录。** [src/eos.rs:96-133](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/eos.rs#L96-L133) 的用例注释直接点名真实模型线：generation_config 的**列表** `[7, 5, 7]` 覆盖 config 的单值 6（注释标注 Qwen3、Kimi-K2 走这条路径，结果 `[7, 5]`）；config 顶层为 `null` 时落到 `text_config` 的 9（注释标注 Qwen3.5 无 generation_config 的情形）。[src/eos.rs:135-161](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/eos.rs#L135-L161) 则把五种失败模式（不声明、空列表、字符串条目、负数、越界）逐一断言。

#### 4.3.4 代码实践：推演回退矩阵并找消费点

1. **实践目标**：内化三级回退与失败模式；找到 EOS 集合在请求生命周期中的实际消费位置。
2. **操作步骤**：
   - 运行现有测试：`cargo test --release -p pegainfer-core --lib eos -- --nocapture`（纯 CPU；**待本地验证**）。
   - 对 [src/eos.rs:98-128](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/eos.rs#L98-L128) 的六个用例，先合上讲义手写预期 `Vec`，再对照 [src/eos.rs:129-132](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/eos.rs#L129-L132) 的断言核对。
   - 在**仓库根目录**执行（本讲撰写环境无法访问兄弟 crate，行号待本地验证）：

     ```bash
     git grep -n "eos_token_ids" -- pegainfer-qwen3 pegainfer-qwen35
     ```

     记录：模型 crate 在装配期哪一行调用它、得到的 `Vec<u32>` 存进什么结构、decode 循环哪一步对照它决定 finish（若 finish 判断落在 frontend/EngineHandle 一侧，把证据也记下来）。
3. **需要观察的现象**：六个用例推演全对；grep 能定位到至少一个模型 crate 的调用点。
4. **预期结果**：能画出「解析（装配期，一次）→ 存储 → 每步 decode 采样后比对 → finish」的时序；模型 crate 内行号由读者本地回填。

#### 4.3.5 小练习与答案

**练习 1**：为什么还要查 `config.json` 的 `text_config`？
**答案**：嵌套配置的检查点把文本模型的字段藏在 `text_config` 里。测试注释点名：Qwen3.5 在没有 generation_config.json、且 config 顶层 `eos_token_id` 为 `null` 时，靠 `text_config` 提供 EOS（[src/eos.rs:111-116](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/eos.rs#L111-L116)）；`null` 在 [src/eos.rs:35](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/eos.rs#L35) 被当作未声明继续回退。

**练习 2**：`eos_token_id: -1` 会发生什么？
**答案**：加载失败。`serde_json::Value::as_u64()` 对负数返回 `None`，落入 `with_context` 的「is not a u32」错误（[src/eos.rs:47-50](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/eos.rs#L47-L50)），测试 [src/eos.rs:149](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/eos.rs#L149) 钉死。

**练习 3**：`[7, 5, 7]` 解析成什么？为什么选择保序去重？
**答案**：`[7, 5]`。逐项 push 前用 `Vec::contains` 去重、保留首次出现顺序（[src/eos.rs:55-57](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/eos.rs#L55-L57)）。保序让同一检查点解析出的集合**逐字节确定**——这既是可测试性（测试可直接 `assert_eq!` 一个 `Vec`），也延续了 u1-l1 以来「构造性确定性」的纪律：同样的输入永远得到同样的输出。

### 4.4 串起来：三个模块在请求生命周期中的位置

#### 4.4.1 概念说明

三个模块都只在**装配期**运行一次，但服务的时刻横跨整条推理链路：RoPE 表服务于每一步前向（prefill 与 decode 都要给 Q/K 注入位置），词表宽度约束决定每步输出投影的形状，EOS 集合决定请求何时结束。

#### 4.4.2 核心流程

| 时机 | 模块 | core 接口 | 结果去向 |
| --- | --- | --- | --- |
| 装配期（一次） | rope | `precompute_rope` | cos/sin 表常驻显存，被每步前向的旋转 kernel 只读查询 |
| 装配期（一次） | decodable_vocab | `decodable_vocab_width` + `aligned_logits_rows` | 决定输出投影 GEMM 的行数与 logits buffer 的主维 |
| 装配期（一次） | eos | `eos_token_ids` | EOS id 集合交给模型 crate 保存 |
| 每步 decode | （模型 crate，core 之外） | — | logits 前缀 mask → 采样出 token id → 命中 EOS 集合即 finish |

#### 4.4.3 源码精读

三个模块的声明集中在 [src/lib.rs:3-15](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/lib.rs#L3-L15)（`decodable_vocab` 在 [src/lib.rs:5](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/lib.rs#L5)、`eos` 在 [src/lib.rs:6](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/lib.rs#L6)、`rope` 在 [src/lib.rs:12](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-core/src/lib.rs#L12)），core 内部再无其他模块引用它们（见第 3 节的 grep 证据）——它们是纯粹的「出口」，全部消费发生在模型 crate。表中最后一行的具体实现位置（mask 在哪里做、finish 事件如何产生）属于模型 crate 与 frontend 的领域，本讲环境无法访问兄弟 crate，**待本地验证**。

## 5. 综合实践

**任务：把「推理出口链路」写成一份带证据的伪代码笔记。**

1. **实践目标**：把本讲三个模块串成一个从装配到停止的完整故事，并用 grep 给每个环节补上真实消费点。
2. **操作步骤**：
   - 第一步，写出装配期 + decode 循环的伪代码（**示例代码**，非项目实现）：

     ```text
     装配期（模型 crate 加载检查点时，各执行一次）：
       ctx = DeviceContext::new()
       (cos, sin) = pegainfer_core::rope::precompute_rope(&ctx, &spec)?       # 表常驻显存
       decodable  = decodable_vocab_width(model_path)?                        # 镜像 frontend 合并
       rows       = aligned_logits_rows(decodable, vocab_rows)                # 128 对齐、vocab 封顶
       eos_ids    = eos_token_ids(model_path, vocab_rows)?                    # fail-fast

     每步 decode：
       hidden = 前向(上一步 token, 查 cos/sin 表旋转 Q/K)                       # rope 表被只读查询
       logits = lm_head(hidden[.., ..rows])                                    # 只算对齐前缀行
       logits[decodable..] 全部置 -inf                                          # mask 是消费者责任
       token = sample(logits)                                                   # 采样出 token id
       if token 属于 eos_ids: 发出 finish；break                                # eos 集合命中即停
     ```

   - 第二步，给每个环节找证据。在仓库根执行：

     ```bash
     git grep -n "precompute_rope\|RopeTableSpec" -- pegainfer-qwen3
     git grep -n "decodable_vocab_width\|aligned_logits_rows" -- pegainfer-qwen3
     git grep -n "eos_token_ids" -- pegainfer-qwen3
     ```

   - 第三步，回填一张消费表：接口 → 调用文件:行号 → 用在装配/前向/停止的哪一环；mask 与 finish 判断的落点若在模型 crate 之外（frontend 的 `EngineHandle`/`TokenEvent` 一侧），也记入表中。
3. **需要观察的现象**：伪代码的每个环节都能对应到至少一条真实代码位置；`precompute_rope` 应出现在模型装配处而非 decode 循环内。
4. **预期结果**：一份「接口 → 消费点 → 生命周期阶段」三列对照表。本讲撰写环境无法访问 `pegainfer-qwen3`，全部行号**待本地验证**。

## 6. 本讲小结

- `rope.rs`：宿主侧按 inv_freq[i] = theta^(-2i/F) 生成 half-split 布局的 cos/sin 表，量化 bf16 后经 `DeviceVec::from_host` 各上传一次；`rotary_dim` 与 `frequency_dim` 分离以支持 proportional RoPE，`validate()` 把一切「看似合法实则错误」的几何挡在计算之前。
- `decodable_vocab.rs`：不做 id→文本解码；它镜像 frontend 的 tokenizer schema 合并，求最宽可解码 id，要求 id 空间是稠密前缀（否则加载失败），并用 `next_multiple_of(128)` 给出输出投影的对齐行数；超出 `decodable` 的行由消费者在选择前 mask。
- `eos.rs`：`generation_config.json` → `config.json` → `text_config` 三级回退取第一个非 null 声明，单值/数组归一、u32 与词表边界校验、保序去重，任何失败都让加载 fail-fast。
- 三者都是 core 的纯出口模块（core 内部零消费点）、装配期一次性执行，全部消费发生在模型 crate。
- 三个模块共同构成推理出口链路：位置编码表（输入侧）→ 前向输出 token id → 词表宽度约束与 EOS 停止（输出侧）。

## 7. 下一步学习建议

- 先完成本讲的 grep 回填任务，再进入 **u4-l1（cuda_graph.rs）**：你会发现 RoPE 表这类「常驻显存、指针不变」的常量正是 CUDA Graph 回放所依赖的内存纪律的一部分。
- 装配期的邻居是 **u3-l3（weight_loader）**：权重装显存与本讲的表上传/配置解析同属模型加载阶段，可对照两讲的 H2D 路径差异（一次性 `from_host` vs pinned 双缓冲流式上载）。
- 想看「宽度约束」的下游效果，可去模型 crate 检索 `aligned_logits_rows` 的调用处，观察 logits buffer 与输出投影 GEMM 的形状如何由它决定（行号待本地验证）。
