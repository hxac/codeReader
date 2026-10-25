# INT4 Marlin 权重打包：从 checkpoint 布局到专家主序显存缓冲

## 1. 本讲目标

上一讲（u2-l2）回答了「一个 rank 该拿哪些张量、切哪一段」。本讲回答下一层问题：**拿到之后，INT4 专家权重要被整形成什么样，Marlin GEMM 核才能直接吃？**

学完本讲你应该能够：

1. 说出 checkpoint 中一个专家投影的两份张量 `weight_packed` / `weight_scale` 的形状、dtype 与含义，并对 gate / up / down 三种投影手算形状与字节数。
2. 解释 compressed-tensors 打包 INT4 布局与 Marlin 布局的差别：为什么加载期要做一次「重排权重 + 重排 scale」，而字节总数不变。
3. 说出 w13 融合前后的维度变化：两个 \(2048 \times 7168\) 的 INT4 矩阵融合为一份 Marlin 缓冲，对应 GEMM 输出宽度 `MARLIN_W13_OUT_DIM` \(= 2 \times 2048 = 4096\)。
4. 完整跟踪一条路径：safetensors 里的张量名 → rank 名字集 → GPU raw 张量 → 专家主序 Marlin 缓冲 → `KimiRankExpertMarlinWeights`，并解释打包为什么发生在分片加载循环内部。

## 2. 前置知识

- **INT4 组量化（group-wise quantization）**：把权重矩阵按输入维（K 维）分组，Kimi-K2.6 的组大小是 32。每组共享一个 bf16 缩放因子 \(s\)，组内 32 个权重各自编码成 4 位无符号整数 \(q \in [0,15]\)，真实权重近似为 \(w \approx s \cdot q\)。4 位只是「存储格式」，进入计算前会配合 scale 还原成数值。
- **pack-quantized 打包**：4 位值不能单独寻址，所以 checkpoint 把连续 8 个 int4 打包进一个 `int32`（8 × 4 bit = 32 bit）。Kimi 的 checkpoint（compressed-tensors 风格命名）因此给每个投影两份张量：`weight_packed`（I32）与 `weight_scale`（BF16）。
- **nibble（半字节）**：一个字节分低 4 位与高 4 位，各放一个 int4。`KimiInt4NibbleOrder::LowThenHigh` 表示一个字节里先存低 nibble 再存高 nibble；缓冲字段名里的 `uint4b8` 指「4 位无符号值、以 8 位容器承载」。
- **Marlin**：为小 batch（≤64 token）高度优化的 INT4 GEMM CUDA 核族（vLLM 生态广泛使用）。它要求权重按自己的瓦片读取顺序**离线重排**、scale 按其索引模式**置换**，运行时零额外整理。PegaInfer 把 Marlin 内核与 Rust FFI 封装放在 `pegainfer-kernels`，本讲只消费它的接口，不进入 CUDA 细节（那是 u6 单元的事）。
- **为什么在加载期打包**：重排是一次性「搬家」——只换布局、不压缩字节。放在加载期做一次，之后每次 decode 都是纯 GEMM，不再付整理成本。
- **专家主序（expert-major）**：把本 rank 的 48 个本地专家的同一投影在目的缓冲里首尾相接，专家 0 在最前。路由元数据用「专家 e 的段偏移」即可直接定位数据，这为后续 MoE 讲义里 Marlin 对 DeepEP 接收缓冲的零拷贝直读埋下伏笔。
- 承接 u2-l1 的 `KimiK2WeightManifest` 名字树与 u2-l2 的 `KimiRankWeightNames` / `local_expert_range` / `ep_rank`。

## 3. 本讲源码地图

以下路径均相对 `pegainfer-kimi-k2/`。

| 文件 | 在本讲中的角色 |
| --- | --- |
| `src/weights.rs` | `weights` 模块根：从 `pegainfer-kernels` 引入 reorder / fuse 的 FFI 封装，声明子模块 |
| `src/weights/manifest.rs` | 名字层：INT4 投影张量名的构造、本地专家区间的推导（u2-l1/u2-l2 已讲，本讲只取用） |
| `src/weights/package.rs` | **本讲主战场**：raw 张量 → 专家主序 Marlin 缓冲的全部打包逻辑（794 行） |
| `src/weights/load.rs` | 调用方：分片流式加载，每个 shard 装完后触发打包（完整加载链路是 u2-l4 的主题） |
| `src/config.rs` | 几何常量：专家中间维 2048、路由专家 384、INT4 组 32 |
| `src/typed_scratch.rs` | 消费侧宽度常量 `MARLIN_W13_OUT_DIM` |

## 4. 核心概念与源码讲解

### 4.1 INT4 量化布局：checkpoint 侧的两份张量

#### 4.1.1 概念说明

每个路由专家有 gate / up / down 三个投影，每个投影在 checkpoint 里不是一份矩阵，而是成对的两份：

- `weight_packed`：dtype 为 **I32** 的二维张量，形状 \([\text{out_dim}, \text{in_dim}/8]\)。一行有 \(\text{in_dim}/8\) 个 int32，每个 int32 装 8 个 int4，正好覆盖该输出行全部 \(\text{in_dim}\) 个权重。
- `weight_scale`：dtype 为 **BF16** 的二维张量，形状 \([\text{out_dim}, \text{in_dim}/32]\)。每行 \(\text{in_dim}/32\) 个 scale，每 32 个连续输入通道共享一个。

也就是说，量化只发生在输入维（列）上：同一行内每 32 个权重共用一个 scale。字节数公式：

\[ \text{bytes}_{packed} = \text{out_dim} \times \frac{\text{in_dim}}{8} \times 4 \text{B}, \qquad \text{bytes}_{scale} = \text{out_dim} \times \frac{\text{in_dim}}{32} \times 2 \text{B} \]

三种投影的几何由角色枚举给出：gate / up 是 \((2048, 7168)\)，down 是 \((7168, 2048)\)。

#### 4.1.2 核心流程

张量名是怎么拼出来的（u2-l1 的回顾 + 本讲的精确化）：

```
language_model.model.layers.{layer_idx}.mlp.experts.{expert_idx}.{projection}.weight_packed
language_model.model.layers.{layer_idx}.mlp.experts.{expert_idx}.{projection}.weight_scale
其中 projection ∈ {gate_proj, up_proj, down_proj}
```

由此手算三种投影的形状：

| 投影 | out_dim | in_dim | weight_packed（I32） | weight_scale（BF16） | 单专家两份合计 |
| --- | --- | --- | --- | --- | --- |
| gate / up | 2048 | 7168 | [2048, 896] = 7 MiB | [2048, 224] = 896 KiB | 7.875 MiB |
| down | 7168 | 2048 | [7168, 256] = 7 MiB | [7168, 64] = 896 KiB | 7.875 MiB |

注意对称性：down 的 packed 与 gate/up 的 packed 字节数完全相同（\(7168 \times 256 = 2048 \times 896 = 1835008\) 个 int32），所以一个专家一层的三投影打包前后都是 \(3 \times 7.875 = 23.625\) MiB。

#### 4.1.3 源码精读

名字构造——前缀拼接逻辑在 manifest 层完成：[src/weights/manifest.rs:L698-L709](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/weights/manifest.rs#L698-L709) 用 `mlp.experts.{expert_idx}.{projection}` 前缀拼出 `weight_packed` / `weight_scale` 两个张量名；[src/weights/manifest.rs:L711-L720](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/weights/manifest.rs#L711-L720) 再包上 `language_model.model.layers.{layer_idx}` 前缀。这两个名字最终进入 u2-l2 讲过的 `KimiInt4ProjectionWeightNames`（[src/weights/manifest.rs:L141-L144](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/weights/manifest.rs#L141-L144)）。

专家张量的加载方式——[src/weights/manifest.rs:L597-L603](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/weights/manifest.rs#L597-L603) 里两份张量都以 `KimiTensorLoadSlice::Full` 加载：**EP 的切分单位是「整个专家」**（一个 rank 拥有 48 个完整专家），而不是像注意力/词表那样按行/列切片。

角色几何与 dtype 门——[src/weights/package.rs:L40-L61](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/weights/package.rs#L40-L61) 的 `KimiInt4ProjectionRole::dims()` 给出 (out, in)，并把角色映射到内核枚举 `W1Gate / W3Up / W2Down`；[src/weights/package.rs:L220-L228](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/weights/package.rs#L220-L228) 在取 raw 张量时强制 `weight_packed` 为 I32、`weight_scale` 为 BF16。

形状公式出现在校验函数里——[src/weights/package.rs:L447-L484](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/weights/package.rs#L447-L484) 的 `validate_expert_major_projection`，其中 L457-L458 写死了 4.1.1 的两条公式：

```rust
let packed_i32_shape = [out_dim, in_dim / 8];
let scale_bf16_shape = [out_dim, in_dim / KIMI_K2_INT4_GROUP_SIZE];
```

（`KIMI_K2_INT4_GROUP_SIZE = 32` 定义于 [src/config.rs:L28-L33](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/config.rs#L28-L33)，同处还有 `KIMI_K2_EXPERT_INTERMEDIATE = 2048`、`KIMI_K2_ROUTED_EXPERTS = 384`。）

#### 4.1.4 代码实践

1. **实践目标**：不看代码也能写出三种投影的两份张量形状。
2. **操作步骤**：在纸上为 layer 4 的专家 37 写出 6 个张量的完整名字与形状（gate/up/down × packed/scale）；然后打开 checkpoint 的 `model.safetensors.index.json`（如有权重）或对照 `src/weights/tests.rs` 里伪造 fixture 的 [src/weights/tests.rs:L284](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/weights/tests.rs#L284)（`fake_int4` 函数），核对名字拼法。
3. **需要观察的现象**：名字的每个片段（`language_model` / `model` / `layers` / `mlp` / `experts` / `gate_proj` / `weight_packed`）分别对应 manifest.rs 的哪一行拼接代码。
4. **预期结果**：6 个名字与 [src/weights/manifest.rs:L698-L720](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/weights/manifest.rs#L698-L720) 的拼接结果逐字一致；形状与 4.1.2 表格一致。
5. 运行 `cargo test -p pegainfer-kimi-k2 --lib weights` 验证 manifest 测试通过——**待本地验证**（无 GPU 也可运行，manifest 层不碰 CUDA）。

#### 4.1.5 小练习与答案

**练习 1**：如果组大小从 32 改成 64，`weight_scale` 的形状怎么变？`weight_packed` 受影响吗？
答案：scale 变成 \([\text{out_dim}, \text{in_dim}/64]\)（gate/up 为 [2048, 112]）；`weight_packed` 不变，因为打包只与 4 bit × 8 = 32 bit 有关，与量化组大小无关。这也是为什么代码里 packed 用 `/8`、scale 用 `/ KIMI_K2_INT4_GROUP_SIZE` 两条不同公式。

**练习 2**：`int4_projection_view`（package.rs:220-228）为什么必须同时检查 dtype 与名字？
答案：raw 张量表以名字为键，dtype 来自 safetensors 头（加载时经 `view.dtype()` 存入，见 [src/weights/load.rs:L129-L135](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/weights/load.rs#L129-L135)）。名字对但 dtype 错（例如有人用 BF16 存了 packed）会让后续按 I32 语义重排的 kernel 读出垃圾，所以要在打包前拦下。

### 4.2 Marlin 重排与 nibble 顺序

#### 4.2.1 概念说明

checkpoint 的 packed 布局是「行主序 + 8 个 int4 一个 int32」的**线性**布局，对 CPU 友好、对 safetensors 友好。但 Marlin GEMM 核按**瓦片**（tile）从显存搬数据进共享内存，它期望权重已经按自己的瓦片遍历顺序排好、scale 已按它的索引模式置换好。如果把线性布局直接喂给 Marlin，核内每次访存都要做复杂的地址换算。

所以打包期做两件事：

1. `kimi_marlin_int4_reorder_weight`：把 48 个专家拼接后的 packed 权重重排进 Marlin 的瓦片布局（字段名 `weight_packed_marlin_uint4b8`）；
2. `kimi_marlin_int4_reorder_scale`：把 scale 置换到 Marlin 期望的排列（字段名 `weight_scale_marlin_permuted`）。

两个函数都是 GPU kernel：数据从进显存那一刻起就不再回 host，拼接和重排全部走 D2D。nibble 顺序 `KimiInt4NibbleOrder::LowThenHigh` 由重排 kernel 按 manifest 参数消费，决定字节内两个 4 位值先低后高的次序。

还有一个「元数据」角色：`KimiInt4WeightManifest`。它由 `KimiInt4WeightManifest::ep8(role, ep_rank, nibble_order)` 构造，描述这份缓冲的角色、专家数、逻辑形状、打包形状、scale 形状，是 Rust 侧与 CUDA 侧共用的「这份缓冲长什么样」合同。

#### 4.2.2 核心流程

对一个 MoE 层的一种投影角色（以 gate 为例），打包是一条四缓冲流水：

```
48 份 raw weight_packed (I32, 各 7 MiB)          48 份 raw weight_scale (BF16, 各 896 KiB)
        │ D2D 逐份拷贝、首尾拼接                          │ D2D 逐份拷贝、首尾拼接
        ▼                                                ▼
staging: weight_packed_offset_binary (u8)        staging: weight_scale_checkpoint (bf16)
        │ kimi_marlin_int4_reorder_weight                  │ kimi_marlin_int4_reorder_scale
        ▼                                                ▼
weight_packed_marlin_uint4b8 (u8)                weight_scale_marlin_permuted (bf16)
        └──────────────────┬───────────────────────────────┘
                           ▼
              KimiExpertMajorProjectionMarlinBuffers
     （staging 两个缓冲是函数局部变量，返回即释放）
```

要点：**拼接在前、重排在后**；重排 kernel 只认「一份连续的 expert-major 输入」；Marlin 布局的字节数与输入相同（只是搬家）。

#### 4.2.3 源码精读

核心函数是 [src/weights/package.rs:L486-L560](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/weights/package.rs#L486-L560) 的 `pack_expert_major_projection_marlin_buffers`，步骤逐一对应：

- L493：先调 4.1 讲过的 `validate_expert_major_projection`，从**真实张量**累计出 `plan`（专家数、两份总字节数）。
- L494-L499：构造内核侧合同 `manifest = KimiInt4WeightManifest::ep8(role.kernel_role(), ep_rank, KimiInt4NibbleOrder::LowThenHigh)` 并 `validate()`。注意 `ep8`——本打包路径写死了 EP 世界大小为 8，这与 u1-l2 讲过的两种拓扑（TP8/EP8 与 TP1/DP8/EP8）的 `local_experts = 384 / 8 = 48` 一致。
- L500-L509：**双重核对**——plan（从张量实测）与 manifest（从常量推导）必须逐项相等（专家数、out/in 维、packed 元素数、scale 字节数）。任何不匹配直接报错，把「权重文件与几何常量不一致」挡在加载期。
- L511-L522：一次性分配四个缓冲，尺寸全部取自 manifest：

```rust
let mut weight_packed_offset_binary = ctx.stream().alloc_zeros::<u8>(manifest.packed_shape.elements())?;
let mut weight_packed_marlin_uint4b8 = ctx.stream().alloc_zeros::<u8>(manifest.packed_shape.elements())?;
let mut weight_scale_checkpoint = ctx.stream().alloc_zeros::<bf16>(manifest.scale_shape.elements())?;
let mut weight_scale_marlin_permuted = ctx.stream().alloc_zeros::<bf16>(manifest.scale_shape.elements())?;
```

- L524-L532：`copy_projection_component_to_contiguous` 把 48 份 raw packed 逐份 D2D 拷进第一个 staging 缓冲（实现见 [src/weights/package.rs:L610-L648](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/weights/package.rs#L610-L648)，逐份累计 offset 并检查总量恰好填满）。
- L533-L538：`kimi_marlin_int4_reorder_weight` 完成权重重排。
- L539-L545：`copy_projection_component_to_typed_contiguous` 拼接 scale（实现见 [src/weights/package.rs:L681-L726](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/weights/package.rs#L681-L726)，带 SAFETY 注释说明这是「字节保持」的 D2D 拷贝）。
- L546-L551：`kimi_marlin_int4_reorder_scale` 置换 scale。
- L553-L559：返回只含两份 Marlin 布局缓冲的 `KimiExpertMajorProjectionMarlinBuffers`（结构定义在 [src/weights/package.rs:L72-L78](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/weights/package.rs#L72-L78)）。

这两个重排函数从哪里来？[src/weights.rs:L30-L39](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/weights.rs#L30-L39) 一并引入 `kimi_marlin_int4_reorder_weight` / `kimi_marlin_int4_reorder_scale` / `kimi_marlin_int4_fuse_w13` 与 `KimiInt4WeightManifest` / `KimiInt4NibbleOrder` 等——它们实现在兄弟 crate `pegainfer-kernels`（`kimi-k2` feature 下的 CUDA 内核与 FFI），本讲不展开其内部。

#### 4.2.4 代码实践

1. **实践目标**：能在不看注释的情况下复述四缓冲流水的顺序与每个缓冲的宿命。
2. **操作步骤**：打开 [src/weights/package.rs:L486-L560](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/weights/package.rs#L486-L560)，在纸上给 L511-L551 的每一步标号（① 分配 ② 拼接 packed ③ 重排 packed ④ 拼接 scale ⑤ 重排 scale），并回答：哪两个缓冲活不过这个函数？
3. **需要观察的现象**：四个 `alloc_zeros` 的尺寸来源是 manifest 还是 plan；两次重排调用的第一个参数都是 `&ctx.as_device_context()`（即 GPU 流上下文）。
4. **预期结果**：staging 两缓冲（`offset_binary`、`checkpoint`）是局部变量，函数返回即释放；只有 Marlin 布局的两份进返回值。plan 只做校验，不用于分配。
5. 若要眼见为实：在有 GPU 与权重的机器上对比加载日志中 `resident_raw_bytes` 与 `expert_package_bytes`（[src/weights/load.rs:L193-L200](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/weights/load.rs#L193-L200)）——**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：为什么不能跳过 staging、直接把 48 份 raw 张量逐份喂给重排 kernel？
答案：重排 kernel 的输入约定是**一份连续的 expert-major 缓冲**（配合 manifest 里的 packed/scale 形状）。48 份 raw 是 48 个互不相邻的 `CudaSlice`，先 D2D 拼接才能满足输入约定；拼接顺便固化了「专家主序」这一地址布局，这正是后续路由零拷贝定位的根基。

**练习 2**：`manifest.packed_shape.elements()`（L504）与 `plan.packed_bytes`（L474 累计）分别从哪来？为什么两者必须相等？
答案：前者从几何常量推导（EP8 合同），后者从磁盘上真实张量的 bytes 累计。相等性证明「checkpoint 的字节数与编译期几何完全吻合」——若模型文件是别的量化格式或别的中间维，这里会立刻报错而不是在运行期算出错误结果。

**练习 3**：`LowThenHigh` 改成 `HighThenLow` 会发生什么？
答案：重排 kernel 会按另一种字节内 nibble 次序解释输入，等价于把每个字节的两个 int4 互换，权重值整体错乱。nibble 顺序由 checkpoint 的打包器决定，必须与写入端一致，因此代码把它作为显式参数传给 manifest。

### 4.3 w13 融合：gate 与 up 合成一份 Marlin 缓冲

#### 4.3.1 概念说明

MoE 专家的 FFN 要算 \(\mathrm{swiglu}(x W_1^{\mathrm{T}}) \odot (x W_3^{\mathrm{T}}) W_2^{\mathrm{T}}\)：gate 权重 \(W_1\) 与 up 权重 \(W_3\) 形状相同（都是 \(2048 \times 7168\)）。Marlin 路径把两者**融合成一份 w13 缓冲**，让一次 GEMM 同时产出 gate 半区和 up 半区，输出宽度翻倍：

\[ \mathrm{MARLIN\_W13\_OUT\_DIM} = 2 \times \mathrm{KIMI\_K2\_EXPERT\_INTERMEDIATE} = 2 \times 2048 = 4096 \]

即一次 w13 GEMM 输出 \([\mathrm{tokens}, 4096]\)，前 2048 列是 gate、后 2048 列是 up（激活函数再把两半组合，细节留给 u6 的 MoE 讲义）。融合发生在**重排之后**：fuse 吃的是两份已经是 Marlin 布局的 gate/up 缓冲，产出一份融合 Marlin 缓冲。

#### 4.3.2 核心流程

```
gate Marlin 缓冲 (48 专家 × 7 MiB packed + 48 × 896 KiB scale)
up   Marlin 缓冲 (同上)
        │  kimi_marlin_int4_fuse_w13（GPU kernel，按 Marlin 瓦片几何交错两份）
        ▼
w13 融合缓冲：packed 字节 = gate + up；scale 字节 = gate + up
        ▼
GEMM 输出宽度 = MARLIN_W13_OUT_DIM = 4096（消费侧 scratch 的列数）
```

字节数手算（单层、48 个本地专家）：packed \(= 2 \times 48 \times 7 \text{MiB} = 672 \text{MiB}\)，scale \(= 2 \times 48 \times 896 \text{KiB} = 84 \text{MiB}\)。

#### 4.3.3 源码精读

融合函数 [src/weights/package.rs:L562-L608](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/weights/package.rs#L562-L608)：

- L567-L572：确认输入角色确实是 Gate 与 Up；
- L573-L582：确认 gate/up 的 plan 完全同形，且 out_dim 等于 `KIMI_K2_EXPERT_INTERMEDIATE`（2048）、in_dim 等于 `KIMI_K2_HIDDEN`（7168）；
- L583-L588：融合缓冲的尺寸就是两份输入之和（`gate.len() + up.len()`）——再次体现「融合不产生新字节，只是交错布局」；
- L590-L596：调 `kimi_marlin_int4_fuse_w13`（同样来自 [src/weights.rs:L37-L39](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/weights.rs#L37-L39) 引入的 FFI）；
- L598-L606：组装 `KimiExpertMajorW13MarlinBuffers`（结构在 [src/weights/package.rs:L95-L102](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/weights/package.rs#L95-L102)，字段含 `intermediate_dim` 与 `group_size = 32`）并调 `as_marlin_weight().validate()` 自检。

输出宽度常量住在消费侧：[src/typed_scratch.rs:L32](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/typed_scratch.rs#L32) 定义 `MARLIN_W13_OUT_DIM = 2 * KIMI_K2_EXPERT_INTERMEDIATE`，被两条 MoE 后端当作 w13 输出 scratch 的列数使用。

融合的调用时机在单层打包函数里：[src/weights/package.rs:L289-L319](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/weights/package.rs#L289-L319) 先分别打包 gate / up / down 三份投影缓冲，L319 一行完成融合。

#### 4.3.4 代码实践

1. **实践目标**：用字节算术验证「融合 = 布局交错，不是数据变换」。
2. **操作步骤**：手算单层 w13 的 packed 与 scale 字节数（按 4.3.2 公式），再读 [src/weights/package.rs:L583-L588](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/weights/package.rs#L583-L588) 确认分配尺寸就是两输入之和；最后把单层结果乘以 60 层，与 rank 级 INT4 权重总量对账（手算约 \(3 \times 48 \times 60 \times 7.875 \text{MiB} \approx 66.4 \text{GiB}\)，即每卡专家权重量级）。
3. **需要观察的现象**：融合前后没有 dtype 变化（u8 + bf16），没有元素数变化；`package_bytes()`（[src/weights/package.rs:L116-L119](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/weights/package.rs#L116-L119)）只是把两份长度相加。
4. **预期结果**：纸面数字与公式一致；66.4 GiB 是手算估计，可用 `kimi_model_report` 的 bytes 统计核对——**待本地验证**。
5. **附加思考**：fuse 要求 gate/up 已经是 Marlin 布局（先 4.2 后 4.3 的顺序不可交换）——如果先 fuse 后 reorder，哪一步会失去「按 Marlin 瓦片交错」的语义？

#### 4.3.5 小练习与答案

**练习 1**：w13 融合后，为什么不把 down（\(7168 \times 2048\)）也融进去凑一个「w123」？
答案：down 的形状与 gate/up 不同（输出维 7168、输入维 2048），且在计算图里位置不同：w13 的输出先进激活函数，down 吃激活后的结果。形状与数据流都不允许它和 w13 共享一次 GEMM，所以结构上是 `KimiMoeLayerExpertMarlinWeights { w13, down }` 两份（[src/weights/package.rs:L122-L127](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/weights/package.rs#L122-L127)）。

**练习 2**：`MARLIN_W13_OUT_DIM` 为什么定义在 `typed_scratch.rs`（消费侧）而不是 `package.rs`（生产侧）？
答案：它是 decode scratch 缓冲 `w13_out` 的**列宽**（[src/typed_scratch.rs:L124](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/typed_scratch.rs#L124)），描述的是 GEMM 输出的形状而非权重的形状；权重侧的对应事实「融合缓冲字节 = gate + up」由 fuse 函数自己保证。把输出宽度放在 scratch 层，与 u5-l4 要讲的「类型化解码缓冲一次定型」体系一致。

### 4.4 专家主序缓冲组织与流式打包

#### 4.4.1 概念说明

打包产物是一个三层容器：

```
KimiRankExpertMarlinWeights                          （一个 rank）
 ├─ rank, local_expert_range, total_bytes
 └─ layers: Vec<KimiMoeLayerExpertMarlinWeights>     （60 个 MoE 层）
     ├─ layer_idx, total_bytes
     ├─ w13:  KimiExpertMajorW13MarlinBuffers        （48 专家融合 gate+up）
     └─ down: KimiExpertMajorProjectionMarlinBuffers （48 专家 down）
```

「专家主序」指最内层两份缓冲里 48 个专家首尾相接、按全局专家号升序排列。序不是约定俗成，是被校验的合同：[src/weights/package.rs:L728-L755](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/weights/package.rs#L728-L755) 的 `validate_local_expert_name_order` 确保名字列表恰是从 `local_expert_range.start` 开始的连续 48 个专家（区间来自 [src/weights/manifest.rs:L482-L491](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/weights/manifest.rs#L482-L491)，按 `ep_rank * 48 .. (ep_rank+1) * 48` 推导，即 [src/config.rs:L332](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/config.rs#L332) 的 `KIMI_K2_ROUTED_EXPERTS / ep_world`）。有了这条合同，「本地专家 e 的数据在缓冲的哪一段」就是纯算术，路由元数据可以零拷贝索引。

打包完还要**释放原料**：[src/weights/package.rs:L337-L374](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/weights/package.rs#L337-L374) 把已打包层的 6 × 48 个 raw 张量从 `weights.tensors`（BTreeMap）里移除并扣减 `total_bytes`——否则 rank 显存里同时躺着 raw 和 Marlin 两份。

一个对照：**router 权重不参与 Marlin 打包**。[src/weights/package.rs:L415-L445](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/weights/package.rs#L415-L445) 的 `KimiRouterGpuWeights::copy_to_device_weights` 只是把 bf16 的 `gate_weight` \([384, 7168]\) 与 f32 的 `e_score_correction_bias` \([384]\) 拷成类型化设备权重——router 是小矩阵、不量化、每次前向整用，不需要 INT4/Marlin 那套。

#### 4.4.2 核心流程

打包不是「全部加载完再打包」，而是**流式**的，发生在分片加载循环内部（[src/weights/load.rs:L105-L157](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/weights/load.rs#L105-L157)）：

```
for shard in load_plan.shards:                 # load.rs:105
    mmap + deserialize safetensors             # load.rs:108-110
    for spec in shard.tensors:                 # load.rs:111
        H2D 拷贝（Full 整拷 / 切片拷）          # load.rs:117-128
        insert 进 weights.tensors (BTreeMap)   # load.rs:129-144
    weights.pack_loaded_expert_marlin_layers(  # load.rs:146-151  ← 每个 shard 之后
        ctx, names, &mut packed, &mut layers)
```

`pack_loaded_expert_marlin_layers`（[src/weights/package.rs:L230-L259](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/weights/package.rs#L230-L259)）内部：

1. 逐层循环，跳过非 MoE 层、已打包层（`packed_layers` 集合去重，跨 shard 幂等）；
2. 用 `has_all_routed_expert_raw`（L329-L335）检查该层 48 个专家的 raw 是否**凑齐**——一个层的专家张量可能分散在多个 shard 文件里，凑不齐就先跳过；
3. 凑齐一层 → `pack_moe_layer_expert_marlin_weights`（[src/weights/package.rs:L275-L327](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/weights/package.rs#L275-L327)）：名字序校验 → gate/up/down 三次 4.2 打包 → 4.3 融合 → `as_marlin_weights().validate()` 自检；
4. `remove_packaged_routed_expert_raw_tensors`（L254）删 raw 释放显存；
5. 全部 shard 读完：确保恰好 60 层打包（[src/weights/load.rs:L165-L170](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/weights/load.rs#L165-L170)），`validate_non_expert_typed_view` 校验非专家张量（L171），组装最终的 `KimiRankExpertMarlinWeights { rank, local_expert_range, layers, total_bytes }`（[src/weights/load.rs:L173-L178](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/weights/load.rs#L173-L178)）。

这样峰值显存约等于「还没凑齐的层的 raw」+「已打包的 Marlin 缓冲」+ 一个 staging，而不是「全部 raw + 全部 Marlin」——对每卡 66 GiB 级别的专家权重，这笔账很关键。

#### 4.4.3 源码精读（数据结构清单）

- raw 张量的家：`KimiGpuRawTensor` / `KimiRankGpuWeights`（[src/weights/package.rs:L10-L22](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/weights/package.rs#L10-L22)）——名字、dtype、形状、字节数与不透明字节负载 `CudaSlice<u8>`。
- 打包层容器与内核视图：`KimiMoeLayerExpertMarlinWeights` / `KimiRankExpertMarlinWeights`（[src/weights/package.rs:L122-L143](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/weights/package.rs#L122-L143)）；`as_marlin_weights()` 把两份缓冲临时借出为 `KimiMarlinInt4ExpertWeights { w13, w2_down }` 交给 kernel——是零拷贝借用视图，不是拷贝。
- 加载结果持有打包产物：[src/weights/load.rs:L62](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/weights/load.rs#L62) 的结构体字段 `expert_kernel_weights`。
- 运行时消费方：worker 持有它（[src/runner/worker.rs:L501](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/runner/worker.rs#L501) 的 `expert_kernels` 字段），NCCL 与 DeepEP 两条 MoE 后端各自取用（[src/runner/moe_nccl.rs:L75](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/runner/moe_nccl.rs#L75)、[src/runner/moe_deepep.rs:L221](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/runner/moe_deepep.rs#L221)）——这是 u6 单元的主菜。
- 加载日志：[src/weights/load.rs:L193-L200](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/weights/load.rs#L193-L200) 打出 `resident_raw_bytes`（还留在 BTreeMap 里的）、`expert_package_bytes`（打包产物）与 `packed_moe_layers`，可直接用于对账。

#### 4.4.4 代码实践

1. **实践目标**：解释「打包为什么在 shard 循环内」，并用日志字段验证理解。
2. **操作步骤**：通读 [src/weights/load.rs:L105-L157](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/weights/load.rs#L105-L157) 与 [src/weights/package.rs:L244-L256](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/weights/package.rs#L244-L256)，写出「若把 L146-L151 挪到循环外，峰值显存会怎么变」的推演；再用 `rg "expert_package_bytes" -n src/` 找到日志点。
3. **需要观察的现象**（在有 GPU 与权重的机器上）：加载日志末尾 `packed_moe_layers=60`、`resident_raw_bytes` 显著小于 `loaded_bytes`（专家部分已被 Marlin 包替换）——**待本地验证**。
4. **预期结果**：能说出三个显存项（未凑齐层的 raw、已打包 Marlin、staging 临时）谁是常驻谁是瞬态；`packed_moe_layers` 恰为 60，否则 L165-L170 的 ensure 会报错。
5. 无 GPU 环境时改做源码阅读版：画出 `packed_layers: BTreeSet<usize>` 在 3 个 shard 依次处理后的内容变化表（假设 layer 1 的专家分布在 shard 1/2、layer 2 全在 shard 3）。

#### 4.4.5 小练习与答案

**练习 1**：`validate_local_expert_name_order` 检查的是什么不变量？破坏它会导致什么？
答案：检查「名字列表 = 从 local_expert_range.start 起连续 48 个全局专家号」。破坏后专家在缓冲里的物理次序与路由的本地索引错位——路由给专家 5 的 token 会读到专家 6 的权重，输出悄悄错误而不会崩，属于最难排查的一类 bug，所以加载期强制校验。

**练习 2**：`remove_packaged_routed_expert_raw_tensors` 里 `ensure!(removed_bytes <= self.total_bytes)`（L357-L363）防什么？
答案：防御性核算：要删的字节数不可能超过 BTreeMap 当前记账总额。正常流程必然成立；它保证「删除 + 扣账」这对操作不会让 `total_bytes` 变成负数，且在异常时给出含 rank 与字节数上下文的报错。

**练习 3**：为什么 router 的两个张量留在 raw 表里（由 `validate_non_expert_typed_view` 持续校验），而专家张量打包后必须删除？
答案：router 小（384 × 7168 bf16 ≈ 5.25 MiB）且前向直接整用，保留 raw 引用并按需拷出类型化视图即可；专家权重量级是每 rank 66 GiB，raw 与 Marlin 双份驻留会直接撑爆显存，所以打包完成即删 raw、只留 Marlin 缓冲。

## 5. 综合实践

**任务**：写出一段不超过 30 行的注释体伪代码，描述某 MoE 层的 gate_up（gate_proj + up_proj）权重从 checkpoint 布局到 expert-major Marlin 布局的完整变换步骤，并为每一步在 `package.rs` 中标注对应函数调用点的行号。

### 5.1 实践目标

把 4.1-4.4 的四步（拼接 → 重排 → 融合 → 释放）串成一条可背诵的链路，做到「给我一个 safetensors 名字，我能说出它的字节最终躺在哪个缓冲的哪一段」。

### 5.2 参考答案（示例伪代码，非项目代码）

```text
# 输入：某 MoE 层 48 个本地专家的 raw 张量（H2D 之后，住在 weights.tensors）
#   gate/up 各 48 份：weight_packed[I32, 2048, 896] + weight_scale[BF16, 2048, 224]
#   名字来自 manifest.rs 的 rank_weight_names 过滤 local_expert_range（manifest.rs:444-449）

# 第 0 步：进入单层打包，先校验专家名序 = 连续 48 个全局专家号
validate_local_expert_name_order(...)                      # package.rs:282-287 -> 728-755

for role in [Gate, Up]:                                    # package.rs:289-318
    plan = validate_expert_major_projection(role, 48 份)   #   package.rs:493 -> 447-484
    manifest = KimiInt4WeightManifest::ep8(role, ep_rank,
                                          LowThenHigh)     #   package.rs:494-498
    assert plan == manifest 的形状与字节数                  #   package.rs:500-509
    staging_packed, marlin_packed = alloc_u8 × 2           #   package.rs:511-516
    staging_scale, marlin_scale = alloc_bf16 × 2           #   package.rs:517-522
    D2D 拼接 48 份 weight_packed → staging_packed           #   package.rs:524-532
    kimi_marlin_int4_reorder_weight(staging_packed
                                    → marlin_packed)       #   package.rs:533-538
    D2D 拼接 48 份 weight_scale → staging_scale             #   package.rs:539-545
    kimi_marlin_int4_reorder_scale(staging_scale
                                   → marlin_scale)         #   package.rs:546-551
    # staging 两缓冲在此函数返回时释放，marlin 两缓冲进入返回值

w13_packed = alloc_u8(gate + up 字节)                      # package.rs:583-585
w13_scale  = alloc_bf16(gate + up 字节)                    # package.rs:586-588
kimi_marlin_int4_fuse_w13(gate, up → w13)                  # package.rs:319 -> 590-596
down = 对 Down 角色重复 for 循环（无融合）                  # package.rs:309-318
remove 已打包层全部 raw 张量并扣账                          # package.rs:254 -> 337-374
# 最终：KimiMoeLayerExpertMarlinWeights { w13, down } 被 load.rs:173-178
# 汇入 KimiRankExpertMarlinWeights，等待 u6 的 MoE 后端零拷贝借用
```

### 5.3 操作步骤

1. 合上讲义，凭记忆在编辑器里重写上面这段伪代码（行号允许查阅源码补齐）。
2. 用 `rg -n "kimi_marlin_int4|pack_expert_major|fuse_expert_major|validate_expert_major" src/weights/package.rs` 核对每个标注行号。
3. 自查三处易错点：① 拼接发生在重排**前**；② staging 缓冲不进返回值；③ fuse 的输入是 Marlin 布局（重排后的），不是 checkpoint 布局。

### 5.4 预期结果与观察

- 每条伪代码行都能在 package.rs 找到唯一对应行号；`rg` 输出与标注一致。
- 若在有 GPU 与权重的机器上运行加载，日志（[src/weights/load.rs:L193-L200](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/weights/load.rs#L193-L200)）应出现 `packed_moe_layers=60` 且 `expert_package_bytes` 与手算的 \(3 \times 48 \times 60 \times 23.625 \text{MiB} \approx 66.4 \text{GiB}\) 同量级——**待本地验证**。
- 无 GPU 环境的完整替代验证：`cargo test -p pegainfer-kimi-k2 --lib weights`（manifest 层测试，不需要 CUDA）——**待本地验证**。

## 6. 本讲小结

- checkpoint 侧每个专家投影是成对两份张量：`weight_packed`（I32，\([\text{out}, \text{in}/8]\)，8 个 int4 一个 int32）与 `weight_scale`（BF16，\([\text{out}, \text{in}/32]\)，组大小 32）；gate/up 是 \((2048, 7168)\)，down 是 \((7168, 2048)\)，单专家单投影恒为 7.875 MiB。
- Marlin 打包是四缓冲流水：D2D 拼接成 expert-major staging → `reorder_weight` / `reorder_scale` 重排成 Marlin 布局；重排只搬家不压缩，plan（实测）与 manifest（EP8 常量合同，nibble 顺序 LowThenHigh）必须逐项相等。
- w13 融合把两份 \(2048 \times 7168\) 的 Marlin 缓冲交错成一份，字节相加、GEMM 输出宽度翻倍为 `MARLIN_W13_OUT_DIM = 4096`（定义在消费侧 typed_scratch.rs:32）。
- 打包产物是三层容器 `KimiRankExpertMarlinWeights → 60 层 → {w13, down}`；专家主序由 `validate_local_expert_name_order` 强校验，路由后续可用纯算术零拷贝定位。
- 打包是流式的：发生在分片加载循环内、凑齐一层打包一层并立即删 raw，峰值显存被压到「未凑齐 raw + 已打包 Marlin + 单层 staging」。
- router 权重（gate_weight + e_score_correction_bias）不量化不打包，只做类型化拷贝，与专家路径形成对照。

## 7. 下一步学习建议

- **u2-l4（mmap 上载与 GPU 权重包）**：本讲只取用了 load.rs 的打包调用点；完整的 mmap → 切片 → H2D 通路、`KimiRankGpuContext` 与层前向缓存的组装是下一讲的主题，届时 4.4.2 的分片循环会被逐行展开。
- **u5-l4（typed scratch）**：`MARLIN_W13_OUT_DIM` 所在的类型化缓冲体系，会解释为什么 w13 输出宽度要在编译期定型。
- **u6-l2 / u6-l3（NCCL 与 DeepEP MoE 路径）**：本讲打包出的 `expert_kernels` 如何被两条 MoE 后端零拷贝借用；特别是 DeepEP 的 `expert_alignment == 8` 与 Marlin 块对齐如何让 dispatch 接收缓冲免 gather/scatter 直读——那正是本讲「专家主序」合同的兑现时刻。
- 延伸阅读（选）：[src/runner/worker/runtime.rs:L100](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/runner/worker/runtime.rs#L100) 附近的 Marlin block size 阶梯（8/16/32/48/64，上限 [src/runner/worker.rs:L92](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/runner/worker.rs#L92) 的 `KIMI_MARLIN_MAX_BLOCK_SIZE = 64`），可先感受「权重布局固定、运行时按 token 数选块大小」的分工。
