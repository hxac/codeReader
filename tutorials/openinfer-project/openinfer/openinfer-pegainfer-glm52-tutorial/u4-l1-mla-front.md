# u4-l1 MLA 前半：q_a/kv_a/q_b 投影与 norm

## 1. 本讲目标

学完本讲你应该能够：

1. 画出 MLA 前半（front half）的两条投影形状链：查询链 `hidden → q_a(2048) → q_b → 64 头 × 256`，键值链 `hidden → kv_a(512+64)`，并说出每个数字对应的 config 常量。
2. 理解 latent KV 表示：为什么每个 token 每层只需缓存 512 维压缩向量 + 64 维 rope 分量，而不是逐头的 K/V。
3. 分清 `fp8_linear_into` / `fp8_linear_pair_into` / `fp8_linear_partials_into` / `fp8_linear_large_m_into` 四个投影变体的行数适用域，以及 `Glm52MlaFront` 为它们准备的持久缓冲。
4. 读懂 `Glm52MlaLayerWeights::from_device` 的装载期形状校验，以及 `qa_kva` 打包权重的构建条件、显存记账（约 1.2 GiB）与 #812 宽桶复用。

## 2. 前置知识

本讲承接 u3-l1（FP8 块量化原语）与 u2-l1（模型几何常量），只增量引入 MLA 本身的概念。

- **KV cache 是推理显存的头号开销**。标准多头注意力里，每个 token 每层要缓存 heads × head_dim 份 K 和 V。以 GLM5.2 为例，64 头 × 256 维如果逐头缓存，一个 token 一层就是两份 [64, 256] 形状的 bf16 张量。
- **MLA（Multi-head Latent Attention，多头潜在注意力）**的解法是把 KV 低秩压缩：键值先投到一个很小的 latent 向量（GLM5.2 是 512 维），缓存里只存这个 latent；注意力计算时再用上采矩阵把 latent 展开成每个头自己的 K/V。
- **低秩直觉**：kv_b 矩阵 \( W_{KV} \) 可以近似分解为「下投影 \( W_{down} \)（hidden → 512）」和「上投影 \( W_{UK}/W_{UV} \)（512 → 每头 K/V）」两步。缓存里只保留中间量 \( c_{kv} = W_{down} \cdot h \)，上投影可以合并进注意力里的 Q 侧（这就是 u4-l3 要讲的 absorb）。
- **fp8 投影族**（u3-l1 已建立）：小 batch（不超过 8 行）走 weight-only GEMV，权重 fp8、激活保持 bf16；大 batch 走「激活量化 + SM100 groupwise GEMM」。分界常量是 FP8_GEMV_MAX_ROWS = 8（[src/fp8.rs:35](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/fp8.rs#L35)）。
- **RMSNorm**：比 LayerNorm 少一次均值中心化，只除以均方根再乘缩放系数：

\[ \mathrm{RMSNorm}(x) = \frac{x}{\sqrt{\frac{1}{n}\sum_{i} x_i^2 + \epsilon}} \odot \gamma \]

GLM5.2 全模型的 \( \epsilon = 10^{-5} \)，见 GLM52_RMS_NORM_EPS（[src/config.rs:47-50](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/config.rs#L47-L50)）。
- **Rows&lt;C&gt;**（u3-l3 已建立）：用 const 泛型把缓冲宽度变成类型事实的行缓冲包装，本讲里 q_resid: Rows(2048) 与 hidden: Rows(6144) 都是它。
- **指针稳定性**（为 u6-l3 CUDA Graph 铺垫）：整步前向会被捕获成 CUDA Graph 回放，所有中间缓冲的指针必须步与步之间不变——所以 front 的中间量放在启动期一次性分配的持久 scratch 里，每步原地覆写。

## 3. 本讲源码地图

| 文件 | 角色 |
|------|------|
| [src/mla_front.rs](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/mla_front.rs#L1-L5) | 本讲主角（624 行）：每层注意力权重 Glm52MlaLayerWeights、持久 scratch Glm52MlaFront、三个执行入口（prefill / decode q 相 / decode rest 相） |
| [src/config.rs](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/config.rs#L9-L50) | 几何常量的唯一来源（u2-l1 已建立），MLA 相关常量集中在 L20-L50 |
| [src/fp8.rs](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/fp8.rs#L35) | 投影 kernel 调度层（u3-l1 已建立），本讲只引用四个入口函数 |
| [src/layer.rs](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/layer.rs#L212-L257) | 消费方：decoder 层在 L212 调 front_q，L230 把 q_resid 交给 DSA 索引器，L257 调 front_rest |
| [src/mla_decode.rs](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/mla_decode.rs#L404-L507) | 消费方（u4-l3 主角）：attend 半场读 q_full / kv_c / k_pe / ckv |
| [src/oracle/mla.rs](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/oracle/mod.rs#L12) | MLA 精度门（u10-l1 展开），本讲实践任务会用到它的运行命令 |

模块头的定位说明：[src/mla_front.rs:1-5](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/mla_front.rs#L1-L5) 写明本文件是「前半场」——从 hidden（形状 T 行 × 6144 列）到持久 front scratch 的 q_a/kv_a/q_b 投影与 norm，加上本层的驻留权重；「后半场」（absorb、FlashMLA 稀疏解码、o_proj）在 mla_decode 里消费 front 的缓冲。

## 4. 核心概念与源码讲解

### 4.1 投影链与形状数学

#### 4.1.1 概念说明

MLA 前半场做两件事：

1. **查询链**：当前步的 hidden 状态先压到 2048 维的低秩空间（q_a），做一次 RMSNorm（q_a_layernorm），再升维展开成 64 个注意力头的查询向量（q_b）。查询不进缓存，每步现算。
2. **键值链**：hidden 压到 576 维（kv_a）= 512 维 latent + 64 维 rope 分量。这个 576 维向量就是每个 token 写进 KV cache 的全部内容——它会在 cache-pack 阶段按层切片落盘（u4-l2 展开）。

「前半场/后半场」切分的意义：前半场只依赖 hidden 本身，**在 DSA 索引器选出稀疏 top-k 之前就能全部算完**；后半场才需要知道选了哪些 token。这个切分让索引器可以和 q_b/kv_a 投影在两条 stream 上并行（见 4.3.2 的 front_q/front_rest 拆分）。

#### 4.1.2 核心流程

一步之内的形状链（T 为本步行数，全宽 heads = 64）：

~~~text
查询链（不进缓存，每步重算）:
  hidden [T, 6144]
    --q_a_proj  (fp8 [2048, 6144])--&gt;  q_a     [T, 2048]
    --q_a_layernorm (RMSNorm, gamma 长度 2048)--&gt;  q_resid [T, 2048]
    --q_b_proj  (fp8 [16384, 2048])--&gt;  q_full  [T, 64, 256]   ← 每头 192 nope + 64 rope

键值链（写入 KV cache 的全部内容）:
  hidden [T, 6144]
    --kv_a_proj_with_mqa (fp8 [576, 6144])--&gt;  ckv [T, 576]
    --ckv split--&gt;  kv_c_raw [T, 512]  +  k_pe [T, 64]
    --kv_a_layernorm (RMSNorm, gamma 长度 512)--&gt;  kv_c [T, 512]
  （k_pe 保持原样，rope 旋转发生在 attend 半场）
~~~

派生量全部来自 config 常量：GLM52_Q_B_OUT = 64 × 256 = 16384、GLM52_KV_A_OUT = 512 + 64 = 576、GLM52_KV_B_OUT = 64 × 448 = 28672（[src/config.rs:29-32](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/config.rs#L29-L32)）。

#### 4.1.3 源码精读

**几何常量（config 唯一来源）**：[src/config.rs:20-32](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/config.rs#L20-L32) 定义了本讲用到的全部 MLA 维度：GLM52_HEADS = 64、GLM52_Q_LORA_RANK = 2048、GLM52_KV_LORA_RANK = 512、GLM52_QK_NOPE_HEAD_DIM = 192、GLM52_QK_ROPE_HEAD_DIM = 64、GLM52_QK_HEAD_DIM = 192 + 64、GLM52_V_HEAD_DIM = 256，以及派生输出宽度 GLM52_Q_B_OUT / GLM52_KV_A_OUT / GLM52_KV_B_OUT / GLM52_O_PROJ_IN。

**模块内别名**：[src/mla_front.rs:43-54](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/mla_front.rs#L43-L54) 把这些常量起成短名（Q_LORA、KV_LORA、Q_HEAD、KV_A_OUT、KV_B_ROWS_PER_HEAD = QK_NOPE + V_HEAD 即 448），注释说明「形状数学密集，数值只放在一处」。这是本 crate 的普遍风格：**形状数字永远从 config 编译期常量推导，不出现魔法数**。

**装载期形状自检**：[src/mla_front.rs:100-114](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/mla_front.rs#L100-L114)（from_host，测试路径）逐个把 checkpoint 张量钉死到架构形状：q_a [2048, 6144]、q_b [heads×256, 2048]、kv_a [576, 6144]、kv_b [heads×448, 512]、o_proj [6144, heads×256]。L97-99 的注释解释了为什么要在这里查而不是等到 forward：错误形状的 checkpoint 会通过自洽的 upload 长度检查，然后死在很深的 GPU slice panic 里。生产路径 from_device 做同样的检查（[src/mla_front.rs:158-163](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/mla_front.rs#L158-L163)），另外校验两个 layernorm gamma 的长度必须是 2048/512（[src/mla_front.rs:164-169](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/mla_front.rs#L164-L169)）。

**头数推导**：[src/mla_front.rs:229-249](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/mla_front.rs#L229-L249) 的 mla_heads 从（可能被 attention-TP 切片过的）q_b/kv_b/o_proj 形状反推本实例持有几个头：q_b 的行数必须是 256 的正倍数、不超过 64，且三个矩阵推出的头数必须一致。TP 分片时每 rank 持 8/64 个头，FlashMLA 的 query/latent 仍保持全宽（见 [L56-60](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/mla_front.rs#L56-L60) 结构体注释）。

**config.json 探针**：识别期与启动期各跑一次的 probe_config_json 把这些维度钉进白名单校验——num_attention_heads = 64、q_lora_rank = 2048、kv_lora_rank = 512、qk_nope_head_dim = 192、qk_rope_head_dim = 64、v_head_dim = 256（[src/config.rs:102-110](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/config.rs#L102-L110)）。也就是说，Rust 侧常量与 HF config 是双向锁死的：任何一侧漂移都会在启动时被拒绝。

#### 4.1.4 代码实践

**实践目标**：用一张真实 GLM5.2 的 config.json 验证 probe 的白名单行为，并手工核对一次形状推导。

1. 操作步骤（CPU 即可）：
   - 在 checkpoint 目录找到 config.json，读出 q_lora_rank、kv_lora_rank、qk_nope_head_dim、qk_rope_head_dim、v_head_dim、num_attention_heads 六个字段；
   - 对照 [src/config.rs:102-110](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/config.rs#L102-L110) 逐项核对，应当全部等于 2048 / 512 / 192 / 64 / 256 / 64；
   - 手算 kv_a_proj_with_mqa 的输出维：512 + 64 = 576，与 GLM52_KV_A_OUT 对照。
2. 需要观察的现象：六个字段与常量一一对应；任何一个不相等（比如换一个非 GLM5.2 的 checkpoint），probe_config_json 会在 ensure_eq_usize 处返回错误、拒绝启动。
3. 预期结果：形状链 hidden [T,6144] → q_a [T,2048] → q_b [T,64×256] 与 hidden [T,6144] → kv_a [T,576] 全部可以从这六个字段推出来，不需要任何额外信息。
4. 若手头没有 checkpoint，此实践退化为纯源码阅读：从 [src/config.rs:29-32](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/config.rs#L29-L32) 的派生常量反推公式即可（GLM52_KV_B_OUT = 64 × (192 + 256) = 28672）。

#### 4.1.5 小练习与答案

1. **练习**：q_b 的输出维 16384 = 64 × 256。如果只知道 q_b_proj 权重的形状是 [16384, 2048]，如何反推头数？——**答案**：mla_heads（[src/mla_front.rs:237](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/mla_front.rs#L237)）用 heads = q_b_n / Q_HEAD，并要求商恰好是正整数且不超过 64。
2. **练习**：为什么 o_proj 的输入维是 heads × 256（GLM52_O_PROJ_IN）而不是 heads × 448？——**答案**：注意力输出只有 V 分支：每头的输出是 256 维 v_head_dim，qk 的 192/64 维只参与打分不参与输出，所以 o_proj 形状为 [6144, heads×256]（[src/mla_front.rs:114](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/mla_front.rs#L114)）。
3. **练习**：kv_b 每头 448 行 = 192 + 256，这两个数分别是什么？——**答案**：前 192 行是 W_UK（nope 上采，喂给注意力打分的 K 分量），后 256 行是 W_UV（V 上采），见 [src/mla_front.rs:54](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/mla_front.rs#L54) 与 4.2.3 的 dequant_kv_b。

### 4.2 head 维度分解与 latent KV 表示

#### 4.2.1 概念说明

每个查询头的 256 维被拆成两部分：

- **nope 192 维**（GLM52_QK_NOPE_HEAD_DIM）：不做位置编码的「内容」部分，可以吸收进 latent 空间计算；
- **rope 64 维**（GLM52_QK_ROPE_HEAD_DIM）：做 RoPE 旋转的部分。RoPE 的逐位置旋转无法塞进低秩 latent——旋转作用在原始 64 维坐标上，Q/K 必须在旋转后的空间里做内积，所以 rope 分量单独投影、单独缓存（k_pe）。

KV 侧同理：latent 512 维（kv_c，norm 之后）+ rope 64 维（k_pe）。**缓存里每 token 每层的 MLA 内容就是 512 + 64 = 576 个 bf16**，与头数无关——这就是 MLA 对 KV cache 的压缩本质。对比逐头 MHA：64 头 × 256 维 × 2（K 和 V）= 32768 个数，压缩比约 28 倍。

注意力打分的 softmax scale 也由这个分解决定：\( 1/\sqrt{256} = 0.0625 \)，即 GLM52_SM_SCALE（[src/config.rs:37-39](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/config.rs#L37-L39)），注释说明 rope_type 为 default 意味着没有 YaRN mscale 修正。

kv_b 的 448 = 192 + 256 行拆成两个吸收因子：

\[ W_{UK} = \text{kv\_b}[:, :192, :] \in \mathbb{R}^{64 \times 192 \times 512}, \quad W_{UV} = \text{kv\_b}[:, 192:, :] \in \mathbb{R}^{64 \times 256 \times 512} \]

u4-l3 会讲它们如何被「吸收」进 query 侧；本讲只需记住：**kv_b 不以 fp8 形式驻留，装载时在 host 上反量化成 bf16 的 W_UK/W_UV 上传**。

#### 4.2.2 核心流程

kv 侧的数据流（decode 路径，fold_kv_pack = false）：

~~~text
hidden [T, 6144]
  --kv_a (fp8 GEMV/GEMM)--&gt;  ckv [T, 576]        ← compressed_kv(512) 与 k_pe(64) 拼接存放
  --glm52_mla_ckv_split_launch--&gt;  kv_c_raw [T, 512]  +  k_pe [T, 64]   ← 设备端拆包
  --rms_norm_rows_into(kv_a_ln, KV_LORA)--&gt;  kv_c [T, 512]
~~~

要点：

- ckv 是「拼接布局」，因为 kv_a 是**一次** GEMM 算出 576 维；拆包（split）与 norm 是两个独立 kernel；
- norm 只作用于 512 维的 kv_c_raw，64 维 rope 分量**不做 norm**；
- FlashInfer 后端走 fold_kv_pack = true 时这条链在 front 里提前结束：split 与 norm 被融合进 attend 侧的 glm52_mla_front_pack_fp8_launch，ckv 保持 raw（[src/mla_front.rs:544-549](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/mla_front.rs#L544-L549) 与 [src/mla_decode.rs:337](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/mla_decode.rs#L337)）。同一条数学链，两种 kernel 编排——这是「算什么」与「怎么算」分离的典型例子。

#### 4.2.3 源码精读

**常量定义**：[src/config.rs:25-31](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/config.rs#L25-L31)。注意 GLM52_QK_HEAD_DIM 和 GLM52_KV_A_OUT 都是**由两个分量相加推导**的 const 表达式，不是独立魔法数。

**ckv 拆包与 norm（decode）**：[src/mla_front.rs:600-609](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/mla_front.rs#L600-L609)——glm52_mla_ckv_split_launch 把 ckv [T,576] 拆成 kv_c_raw 与 k_pe，随后 rms_norm_rows_into 用 kv_a_ln 的 gamma、GLM52_RMS_EPS、宽度 KV_LORA = 512 把 kv_c_raw 归一化进 kv_c。prefill 路径是同一条链（[src/mla_front.rs:415-425](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/mla_front.rs#L415-L425)），query 侧的 q_a_layernorm 同理用宽度 2048（[src/mla_front.rs:517-525](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/mla_front.rs#L517-L525)）。

**kv_b 反量化成吸收因子**：[src/mla_front.rs:251-298](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/mla_front.rs#L251-L298) 的 dequant_kv_b 在 host 上做三重循环（heads × 448 行 × 512 列），按 128×128 块查 scale 表（scale_cols = 512/128 = 4，见 [L275](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/mla_front.rs#L275) 与 [L283](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/mla_front.rs#L283)），把 fp8 e4m3 字节乘块 scale 转成 bf16；前 192 行写入 w_uk，其余写入 w_uv（[L285-289](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/mla_front.rs#L285-L289)），最后 H2D 上传。生产路径里 kv_b 的 fp8 字节先 clone_dtoh 拉回 host 一次、反量化后**直接丢弃不驻留**（[src/mla_front.rs:133-136](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/mla_front.rs#L133-L136) 注释与 [L170-L180](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/mla_front.rs#L170-L180) 实现）。驻留形态见结构体字段：w_uk [heads, 192, 512] 与 w_uv [heads, 256, 512]（[src/mla_front.rs:68-69](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/mla_front.rs#L68-L69)）。

**softmax scale**：[src/config.rs:36-39](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/config.rs#L36-L39)——scale 取 \( 1/\sqrt{256} = 0.0625 \)，同时 GLM52_ROPE_HALF = 32 说明 rope 旋转表宽度 32 对，MLA 与 indexer 共用这张表。

#### 4.2.4 代码实践

**实践目标**：算出一个 64-token KV 页里 MLA 部分的字节数，为 u4-l2 的页布局建立直觉。

1. 操作步骤（纸笔即可）：
   - 每 token 每层的 MLA 缓存内容 = kv_c(512) + k_pe(64) = 576 个 bf16 = 1152 字节；
   - 一页 64 token × 78 层（GLM52_LAYERS，[src/config.rs:11](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/config.rs#L11)）：1152 × 64 × 78 = 5,756,928 字节，约 5.49 MiB；
   - 对比逐头 MHA 同等页：64 头 × 256 维 × 2（K,V）× 2 字节 × 64 token × 78 层，约 163 MiB。
2. 需要观察的现象：压缩比约 28.4 倍（32768/1152），只取决于维度分解，与具体实现无关。
3. 预期结果：MLA 切片约 5.5 MiB 每页；u4-l2 会看到一页还叠 21 个 full-indexer 层的 index-K 切片与 L78 MTP 镜像。
4. 本实践为纯推导，无需运行。

#### 4.2.5 小练习与答案

1. **练习**：为什么 k_pe 不做 RMSNorm？——**答案**：norm 的 gamma kv_a_ln 长度是 512（KV_LORA，[src/mla_front.rs:164-169](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/mla_front.rs#L164-L169) 校验），HF 参考实现里 kv_a_layernorm 只定义在 latent 512 维上；rope 分量随后要参与三角函数旋转，保持投影原值。
2. **练习**：GLM52_HEAD_DIM = 192（[src/config.rs:22](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/config.rs#L22)）和 GLM52_QK_NOPE_HEAD_DIM = 192 数值相同，它们是同一个东西吗？——**答案**：数值恰好相等但语义不同：前者是 config 里的通用 head_dim 字段，后者是 MLA nope 分量；MLA 形状用的是后者（[src/mla_front.rs:48](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/mla_front.rs#L48)）。
3. **练习**：kv_b 全宽 fp8 权重约多大？反量化成 bf16 后呢？——**答案**：fp8 字节 = 28672 × 512，约 14.0 MiB，外加 scale 表；bf16 的 W_UK + W_UV = (64×192 + 64×256) × 512 × 2 = 28.0 MiB，恰好翻倍。

### 4.3 front scratch 持久缓冲与 fp8 投影调度

#### 4.3.1 概念说明

Glm52MlaFront 是前半场的全部中间量容器，设计上有三个关键决定：

1. **持久 + 原地覆写**：缓冲在启动期按 bucket 尺寸一次性分配，每步由 front_q/front_rest 原地写入。这是整步 CUDA Graph（u6-l3）的前提——graph 回放要求指针不变，原地覆写让「重放」等价于「重算」。
2. **78 层共享一个实例**：layer N 的 front 值在 layer N 的 attend 消费后即死，所以不需要 78 份 scratch（[src/mla_front.rs:300-307](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/mla_front.rs#L300-L307) 的结构体注释）。
3. **行数烤进 scratch**：tokens 在构造时固定，所有 front kernel 从 front.tokens()（即 q_resid.tokens()）取行数，单一事实来源（[src/mla_front.rs:376-379](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/mla_front.rs#L376-L379)）。

decode 与 prefill 两形态：decode 版多带 GEMV partial 缓冲、打包输出 sink 和（行数超过 8 时）wide GEMM scratch；prefill 版（new_prefill）把这些缩成 1 个元素的占位（[src/mla_front.rs:329-336](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/mla_front.rs#L329-L336) 与 [L357-365](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/mla_front.rs#L357-L365)），因为 prefill 永远走大 batch GEMM 路径。

**fp8 投影变体的行数适用域**（承接 u3-l1）：

| 变体 | 位置 | 适用域 | front 里的对应缓冲 |
|------|------|--------|--------------------|
| fp8_linear_into | [src/fp8.rs:392](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/fp8.rs#L392) | 小 m 的 weight-only GEMV（t 不超过 8） | gemv_partial |
| fp8_linear_pair_into | [src/fp8.rs:437](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/fp8.rs#L437) | t = 1，两个共享输入的投影并一次 grid | 复用输出缓冲 |
| fp8_linear_partials_into | [src/fp8.rs:415](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/fp8.rs#L415) | mma 表有路由的打包权重（batched mma） | gemv_partial + qa_kva_out |
| fp8_linear_large_m_into | [src/fp8.rs:66](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/fp8.rs#L66) | 大 m：激活量化 + groupwise GEMM（t 超过 8 或 prefill） | wide: Glm52Fp8GemmScratch |

#### 4.3.2 核心流程

decode 前半场被拆成两个入口（layer.rs 在中间插入 DSA 索引器）：

~~~text
layer 一步（简化）:
  glm52_mla_front_q_into(...)        # q 相：q_a 投影 + q_a_layernorm → q_resid
  —— 把 q_resid 交给 DSA 索引器（aux stream 可并行）——
  glm52_mla_front_rest_into(...)     # rest 相：q_b + kv_a + split + kv_a norm
~~~

front_q 内部按行数 t 分派 q_a（kv_a 同吃 hidden，被一并处理）：

~~~text
if t == 1:
    fp8_linear_pair_into(q_a, kv_a)          # 一次拼接 grid 算两个投影，少一个 graph 节点
elif qa_kva_fused(t):                        # mma 表对该行数与打包形状 [2624, 6144] 有路由
    fp8_linear_partials_into(qa_kva 打包权重)  # 部分和进 gemv_partial
    glm52_gemv_split_reduce_launch(...)       # 把部分和拆解回 q_a [T,2048] 与 ckv [T,576]
elif wide 存在 (t &gt; 8):
    有打包权重: fp8_linear_large_m_into(qa_kva)   # 一次 GEMM + 一次激活量化
               + extract_hidden_rows_raw_into × 2  # 从 [T,2624] 切出 q_a 与 ckv
    无打包权重: fp8_linear_large_m_into(q_a)
else:
    fp8_linear_into(q_a)                      # 普通 weight-only GEMV（2..8 行且无路由）
最后统一: rms_norm_rows_into(q_a → q_resid)
~~~

front_rest 的 kv_a 有三个「跳过」条件（已经有人算过）：t == 1（pair 算过）、qa_kva_fused（fused mma 算过）、qa_kva_packed_wide（wide 打包 GEMM 算过），三者以或关系合并在一行判断里（[src/mla_front.rs:583](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/mla_front.rs#L583)）。

#### 4.3.3 源码精读

**scratch 结构体**：[src/mla_front.rs:300-327](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/mla_front.rs#L300-L327)。字段注释即形状契约：q_a [T,2048]（pre norm）、q_resid: Rows(2048)（post norm，pub 给索引器）、q_full [T, heads, 256]（紧凑头分片，pub 给 attend）、ckv [T,576]、kv_c_raw [T,512]、kv_c [T,512]、k_pe [T,64]（pre-rope）。gemv_partial 的注释（L318-319）说明它必须每 scratch 一份：ctx/aux 双 stream 重叠时绝不能共享。qa_kva_out [T,2624] 是「仅校验用的 bf16 sink」——mma 路由保证 partials 路径不写它，但 kernel 签名要求有落点（L321-322）。

**构造器**：[src/mla_front.rs:338-374](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/mla_front.rs#L338-L374)。with_decode_scratch 先校验 heads 在 1..=64 内（L344-347），decode 形态下 gemv_partial 与 qa_kva_out 按 T 分配、prefill 形态给 1 个元素（L357-365）；wide 仅当 decode 且 tokens 超过 FP8_GEMV_MAX_ROWS（8）才建，且 GEMM scratch 的行数取 tokens 向上取整到 4 的倍数（L367-372）——GEMM kernel 的对齐要求。

**两个谓词**：[src/mla_front.rs:528-542](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/mla_front.rs#L528-L542)。qa_kva_fused 是「front_q 取 fused、front_rest 跳过 kv_a」共用的**唯一**判定（打包权重存在，且 glm52_gemv_mma_routes(t, n, k) 对当前行数为真）；qa_kva_packed_wide 是 wide 路径的孪生。构建期用 8 行查询一次决定是否造 pack（[src/mla_front.rs:197-211](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/mla_front.rs#L197-L211)），运行期按真实 t 再查同一个谓词——batch-8 的查询覆盖了所有 fused bucket，更小的 batch 永远没有 packed 入口（L197-201 注释）。

**front_q 的四路分派**：[src/mla_front.rs:438-525](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/mla_front.rs#L438-L525)。

- t = 1 的 pair 路径（[L439-449](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/mla_front.rs#L439-L449)）：注释明说动机是「bs=1 时一个拼接 grid 去掉一个 graph 节点，同时保留两个 checkpoint 权重和输出缓冲」；
- fused mma 路径（[L450-477](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/mla_front.rs#L450-L477)）：fp8_linear_partials_into 对打包权重 [2624, 6144] 出部分和，ensure 断言 ksplit 大于 0 防御性地确认确实走了 split-k 路径（L464-467），然后 glm52_gemv_split_reduce_launch 把 partial 解交织回 q_a 与 ckv 两个紧凑缓冲（L468-477）；
- wide 打包路径（[L478-503](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/mla_front.rs#L478-L503)）：#812 的 profile 显示宽步被「瘦长 per-projection GEMM + 各自激活量化」支配，打包后一次 GEMM + 一次量化同时算出 q_a 和 kv_a（L480-484 注释），再用 extract_hidden_rows_raw_into 从 [T,2624] 里按偏移 0 与 2048 切列（L486-503）；
- 普通 GEMV 兜底（[L507-516](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/mla_front.rs#L507-L516)）。

四条路径殊途同归于 rms_norm_rows_into 把 q_a 归一化进 q_resid（[L517-525](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/mla_front.rs#L517-L525)）。

**打包权重的显存记账**：[src/mla_front.rs:213-227](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/mla_front.rs#L213-L227)。glm52_qa_kva_twin_bytes 精确算出这层「双胞胎」副本的 per-rank 字节数，无路由则为 0；注释警告：twin 在 free-VRAM 探测**之后**才落地，不记账的约 1.2 GiB 会吃掉 post-build headroom 下限——这正是 u2-l4 精确账本的一行条目。结构体字段注释（[L70-78](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/mla_front.rs#L70-L78)）还给出动机数据：打包 launch 7.3 微秒对比单独 q_a 7.1 微秒（batch 8、GB300），kv_a 的字节「免费搭车」，代价是每层约 15.4 MiB 的 twin。

**prefill 入口**：[src/mla_front.rs:382-426](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/mla_front.rs#L382-L426)。断言 rows 为正、是 4 的倍数、不超过 front.tokens() 且权重与 scratch 头数一致（L390-396），全部投影走 fp8_linear_large_m_into（caller 传入共享的 Glm52Fp8GemmScratch），顺序 q_a → norm → q_b → kv_a → split → norm，没有任何分支——prefill 永远是大 batch。

**测试便捷入口**：[src/mla_front.rs:614-624](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/mla_front.rs#L614-L624) 的 glm52_mla_front_into 只是 front_q + front_rest 的串联，标注 cfg(test)——生产代码必须显式走两段以插入索引器。

#### 4.3.4 代码实践

**实践目标**：为一个具体的 decode 步（T = 32，attention-TP 分片 heads = 8）开出完整的 front 缓冲账本，并判定 kernel 路径。

1. 操作步骤（纸笔 + 源码对照）：
   - 按 [src/mla_front.rs:348-372](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/mla_front.rs#L348-L372) 逐字段算字节数（bf16 = 2 字节，结果见下方预期结果表）；
   - 判定路径：t = 32 超过 8，wide 存在；若当前 GPU 的 mma 表有 batch-8 路由（Blackwell），则 qa_kva pack 存在，q_a 走「打包 wide GEMM + 两次 extract」；否则走单独的 fp8_linear_large_m_into(q_a)；
   - 用 git grep -n qa_kva_fused src/ 复核两个消费点（front_q 取用、front_rest 跳过 kv_a）。
2. 需要观察的现象：Glm52MlaFront::new(ctx, 32, 8) 中 q_full = 32 × 8 × 256（紧凑分片），heads 校验允许 1..=64；wide 的 GEMM scratch 行数 = 32（已是 4 的倍数，不进位）。
3. 预期结果（T = 32, heads = 8）：

| 缓冲 | 形状 | 字节 |
|------|------|------|
| q_a | [32, 2048] | 131072 |
| q_resid | Rows(2048) | 131072 |
| q_full | [32, 8, 256] | 131072 |
| ckv | [32, 576] | 36864 |
| kv_c_raw 与 kv_c | [32, 512] 两个 | 32768 + 32768 |
| k_pe | [32, 64] | 4096 |
| qa_kva_out | [32, 2624] | 167936 |
| gemv_partial | f32 × 32 × F | F = GLM52_GEMV_MMA_SCRATCH_FLOATS_PER_ROW（待本地验证：常量在 pegainfer-kernels 的 ops 模块） |
| wide | Glm52Fp8GemmScratch::new(32, 6144) | 按 fp8 GEMM 实现分配 |

4. 有 GPU 的机器可选跑 MLA 精度门（需要 checkpoint，待本地验证）：

~~~bash
cargo test --release -p pegainfer-glm52 --features glm52 --lib mla_oracle -- --ignored --nocapture
~~~

该命令来自 [src/oracle/mod.rs:12](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/oracle/mod.rs#L12) 的模块文档，门禁内容在 u10-l1 展开。

#### 4.3.5 小练习与答案

1. **练习**：qa_kva twin 每层多大？78 层总共多大？与源码注释的「约 15.4 MiB 每层」「约 1.2 GiB」对得上吗？——**答案**：按 [src/mla_front.rs:225-226](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/mla_front.rs#L225-L226) 的公式：权重 2624 × 6144 = 16,117,248 字节 + scale 21 × 48 × 4 = 4032 字节（21 = 2624 除以 128 向上取整，48 = 6144 除以 128），合计 16,121,280 字节约 15.38 MiB；乘 78 层 = 1,257,459,840 字节约 1.17 GiB。两个注释数字都能复现。
2. **练习**：为什么 t = 1 不复用打包权重？——**答案**：t = 1 走 fp8_linear_pair_into：注释（[src/mla_front.rs:440-441](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/mla_front.rs#L440-L441)）说明 bs=1 时一个拼接 grid 就能去掉一个 graph 节点，且保留两个原始 checkpoint 权重与输出缓冲；打包 mma 路径的收益在 batch 8 才兑现（L70-77 的微秒对比）。
3. **练习**：q_resid 为什么是 Rows(2048) 而其他缓冲是裸 CudaSlice？——**答案**：q_resid 要以 Rows 语义被两个下游消费——DSA 索引器的 wq_b(q_resid)（[src/layer.rs:230](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/layer.rs#L230)）和 q_b 投影的输入——用 u3-l3 的类型化包装把「宽度 2048 + 行数」变成构造点不变量；同时 front.tokens() 就是从它读的（[src/mla_front.rs:376-379](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/mla_front.rs#L376-L379)）。

## 5. 综合实践

**任务：把一步 decode 的 MLA 前半场变成一张可核对的账本。**

设定：EP8 拓扑下的一个 rank，attention 不分片（heads = 64），decode bucket = 8，当前 GPU 为 Blackwell（mma 表有 batch-8 路由，qa_kva pack 存在）。

1. **形状链**：写出 hidden 到 q_full / kv_c / k_pe 的每一步张量形状（T = 8）。
2. **路径判定**：按 4.3.2 的决策树确定 q_a、q_b、kv_a 各走哪个 fp8 变体，并指出 front_rest 里 kv_a 是否被跳过（提示：t = 8 不等于 1，qa_kva_fused(8) 查 glm52_gemv_mma_routes(8, 2624, 6144)——构建期同谓词为真，所以 fused 生效，kv_a 被跳过）。
3. **缓冲账**：decode 形态下 T = 8 时哪些字段为完整尺寸、wide 是否存在（8 超过 8 为假 → 不存在）。
4. **权重账**：该 rank 每层 MLA 的驻留权重有哪些（q_a / q_a_ln / q_b / kv_a / kv_a_ln / o_proj 的 fp8 ProjWeight，bf16 的 W_UK 与 W_UV，以及 qa_kva twin），用 4.3.5 第 1 题的方法算 twin 字节。
5. **验证方式**：全部数字应能只从 [src/config.rs:20-50](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/config.rs#L20-L50) 常量与 [src/mla_front.rs:43-54](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/mla_front.rs#L43-L54) 的别名推导出来；有 GPU 与 checkpoint 的读者可用 4.3.4 的 mla_oracle 命令对 MLA 数值做端到端校验（待本地验证）。

这个任务串起了本讲三个模块：形状数学（4.1）给出维度，head 分解（4.2）给出 kv_b 拆解与 latent 语义，scratch 与调度（4.3）给出路径与缓冲。

## 6. 本讲小结

- MLA 前半场 = 两条投影链：查询链 hidden [T,6144] → q_a [T,2048] → RMSNorm → q_resid → q_b → q_full [T,heads,256]，键值链 hidden → kv_a → ckv [T,576] → split → kv_c [T,512] + k_pe [T,64]；全部维度由 [src/config.rs:20-32](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/config.rs#L20-L32) 的编译期常量推导。
- latent KV 表示：每 token 每层只缓存 512（latent，norm 后）+ 64（rope，不 norm），与头数无关，压缩比约 28 倍；kv_b 不驻留 fp8，装载时 host 端按 128×128 块反量化成 bf16 吸收因子 W_UK [heads,192,512] 与 W_UV [heads,256,512]。
- 同一组投影权重有四条执行路径：t = 1 的 pair 拼接 grid、batch-8 的打包 mma partials + split-reduce、t 超过 8 的 wide GEMM（可复用同一打包）、2..8 无路由的普通 GEMV；qa_kva_fused 是前后两段共用的唯一谓词。
- Glm52MlaFront 是指针稳定的持久 scratch，78 层共享一个实例、行数烤进构造；decode 与 prefill 两形态差在 GEMV partial、qa_kva_out 与 wide GEMM scratch。
- 打包权重的约 1.2 GiB 通过 glm52_qa_kva_twin_bytes 计入 u2-l4 的精确显存账本——形状数学直接服务于容量规划。

## 7. 下一步学习建议

- **下一讲（u4-l2）**：KV 页布局。本讲的 ckv [T, 576] 在 cache-pack 时被切成每层的 576 维切片写进 64-token 页；去看页表寻址为何要求 cap 64 对齐、一页如何容纳 78 个 MLA 切片 + 21 个 index-K 切片 + L78 MTP 镜像。
- **再下一讲（u4-l3）**：MLA 解码后半场。本讲留下的 W_UK 与 W_UV 在那里被「吸收」进 query，QUERY_DIM = 512 + 64 = 576 的由来也会揭晓；fold_kv_pack 为真时被 front 跳过的 split 与 norm 会以融合 kernel 的形式重新出现。
- **延伸阅读**：[src/layer.rs:212-257](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/layer.rs#L212-L257) 看 front 两段入口如何被 decoder 层编排、q_resid 如何喂给 DSA 索引器（u4-l4 展开）；精度验证读 src/oracle/mla.rs 的门禁（u10-l1 展开，运行命令见 [src/oracle/mod.rs:12](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/oracle/mod.rs#L12)）。
