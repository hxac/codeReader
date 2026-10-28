# rank 切分计划：TP 头片、EP 专家片与词表片

> 所属单元：u2 权重加载 · 本讲 id：u2-l2 · 前置：u2-l1（index 解析与 KimiK2WeightManifest）

## 1. 本讲目标

上一讲（u2-l1）我们把 `model.safetensors.index.json` 的 `weight_map` 解析成了与模型同构的名字树 `KimiK2WeightManifest`——但那只是「仓库清点」，全部 208,215 个 text 张量一个都还没分配。本讲回答加载前的最后一道规划题：**一块 GPU（一个 rank）到底要哪些名字、每个名字要哪一段？**

学完本讲，你应该能够：

1. 手算任意 rank 在 `KimiK2ParallelShape::tp8_ep8()`（TP8/DP1）与 `tp1_dp8()`（TP1/DP8）两种拓扑下的三条区间：`attention_head_range`、`vocab_range`、`local_expert_range`；
2. 区分两份产物的职责：`rank_weight_names` 是「点名册」（谁跟我走），`rank_sliced_load_plan` 是「切片加载计划」（每人带哪段行李），并说出它们分别被谁消费；
3. 解释为什么 384 个路由专家在 EP8 下每个 rank 恰好分到 48 个，以及这个整除性由谁保证；
4. 把区间换算成具体张量的行/列切片（`q_b_proj`、`kv_b_proj`、`o_proj`、`lm_head`……），读懂 `KimiTensorLoadSlice` 的 `Full` / `RowRange` / `ColRange` 三种取值各自的含义。

本讲仍然**不读任何分片字节**——一切计算都发生在名字与整数区间上，无 GPU、无权重也能完整验证。

## 2. 前置知识

- **TP / DP / EP（承接 u1-l2、u1-l4）**：TP（tensor parallel）把单个张量按维度切开分给多卡；DP（data parallel）让每卡持有完整的单副本、各自服务不同请求；EP（expert parallel）把 MoE 的路由专家按编号分给各卡。Kimi-K2 只支持两种 8 卡拓扑：TP8/DP1（MoE 走 NCCL）与 TP1/DP8（MoE 走 DeepEP），两者的 `ep_world = tp_world × dp_world` 都等于 8。
- **半开区间 `Range<usize>`**：Rust 的 `a..b` 表示 `[a, b)`，含头不含尾。本讲所有区间都是半开区间，区间长度 = `end - start`。
- **二维权重的方向约定**：checkpoint 里的 `*.weight` 一律是 `[out_features, in_features]`（行 = 输出维，列 = 输入维）。于是「按输出维切」（注意力头数、词表行数、FFN 中间维）是**行切片**；「按输入维切」（`down_proj` 的输入是被 TP 切过的中间维、`o_proj` 的输入是被 TP 切过的头维）是**列切片**。
- **MLA 注意力的名字与形状（承接 u1-l2）**：`q_a_proj`（下投影到 1536）、`q_b_proj`（上投影到 64 头 × 192 = 12288，其中每头 nope 128 + rope 64）、`kv_a_proj_with_mqa`（压缩 KV，512 + rope 64，**与头无关**）、`kv_b_proj`（上投影到 64 头 × (128 + 128) = 16384）、`o_proj`（输入 64 头 × 128 = 8192）。
- **safetensors weight_map（承接 u2-l1）**：张量名 → 分片文件名的映射；`KimiK2WeightManifest` 已经把它组织成顶层 3 张量 + 61 层（第 0 层 dense、其余 60 层 MoE）的名字树。

## 3. 本讲源码地图

| 文件 | 在本讲中的角色 |
|---|---|
| `src/config.rs` | `KimiK2ParallelShape`（两种拓扑构造子、`new` 的三条除法、`parallel_config`）与全部几何常量 |
| `src/weights/manifest.rs` | 本讲主战场：`rank_plan` 区间推导 → `rank_tensor_names` / `rank_weight_names` 点名 → `rank_sliced_load_plan` 切片计划，以及逐类张量的切片规则 |
| `src/weights/load.rs` | 切片规格的定义端（`KimiTensorLoadSlice` / `KimiTensorLoadSpec` / `KimiRankSlicedLoadPlan`）与消费端（`load_rank_sliced_weights_to_gpu`、`sliced_tensor_bytes`） |
| `src/weights/tests.rs` | `tiny_manifest()` 合成清单 + rank=3 的全套切片断言，是本讲所有手算的标尺 |
| `src/weights.rs` | weights 子树的共享头：`KIMI_K2_WEIGHT_INDEX`、`TEXT_PREFIX` 与 config 常量的引入（`use super::*` 的来源） |

## 4. 核心概念与源码讲解

### 4.1 rank_plan 区间推导

#### 4.1.1 概念说明

一个 Kimi-K2 checkpoint 有 208,215 个 text 张量，一块 GPU 既装不下、也只需要其中的八分之一。「哪些归我」这个问题被压缩成**三条半开区间**：

- `attention_head_range`：64 个注意力头里，本 rank 拥有哪一段（按 **tp_rank** 切）；
- `vocab_range`：163,840 行词表里，本 rank 的 embedding/lm_head 覆盖哪一段（按 **tp_rank** 切）；
- `local_expert_range`：384 个路由专家里，本 rank 常驻哪一段（按 **ep_rank** 切）。

头和词表跟着 TP 走、专家跟着 EP 走——这正是 u1-l2 说过的「EP 与 TP/DP 正交」在加载层的落点：两种拓扑的 `ep_world` 同为 8，所以**专家区间在两种拓扑下完全相同**；不同的只是头与词表这两条 TP 区间。

#### 4.1.2 核心流程

设 `tp_world = T`、`dp_world = D`、`ep_world = E = T × D`，全局 rank 为 r，则本 rank 的

```text
tp_rank ∈ [0, T)      ep_rank ∈ [0, E)      （由 ParallelConfig 换算，见 4.1.3）

heads(r)   = [ tp_rank · (64 / T),     (tp_rank + 1) · (64 / T) )
vocab(r)   = [ tp_rank · (163840 / T), (tp_rank + 1) · (163840 / T) )
experts(r) = [ ep_rank · (384 / E),    (ep_rank + 1) · (384 / E) )
```

代入两种拓扑（`new` 里的三条除法）：

| 每 rank 宽度 | TP8/DP1（`tp8_ep8`） | TP1/DP8（`tp1_dp8`） |
|---|---|---|
| `heads_per_tp = 64 / T` | 8 | 64 |
| `vocab_per_tp = 163840 / T` | 20,480 | 163,840 |
| `local_experts = 384 / E` | 48 | 48 |

**为什么恰好 48 个专家**：384 = 8 × 48，整除；这个整除性不是巧合而是被 `validate` 强制的门（见 4.1.3 第 4 条引用）。两种拓扑 E 同为 8，所以每 rank 都是 48 个，不多不少；rank r 拥有全局编号 `[48r, 48r + 48)` 的专家。

#### 4.1.3 源码精读

1. **拓扑构造与三条除法**。[src/config.rs:L315-L322](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/config.rs#L315-L322) 定义两个构造子：`tp8_ep8()` 就是 `Self::new(8, 1)`，`tp1_dp8()` 就是 `Self::new(1, 8)`。真正干活的是 [src/config.rs:L325-L335](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/config.rs#L325-L335)：先算 `ep_world = tp_world * dp_world`，再在 [L331-L333](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/config.rs#L331-L333) 做三条整数除法，把 `heads_per_tp`、`local_experts`、`vocab_per_tp` 定死。字段声明在 [src/config.rs:L288-L294](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/config.rs#L288-L294)。

2. **区间推导本体**。[src/weights/manifest.rs:L275-L299](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/weights/manifest.rs#L275-L299) 的 `rank_plan`：先 `ensure!(rank < ep_world)` 拒绝越界 rank，再经 `parallel_config().tp_rank(rank)` / `ep_rank(rank)` 拿到两个局部坐标，然后三行乘法直接写出区间（`tp_rank * heads_per_tp..(tp_rank + 1) * heads_per_tp` 等），最后调 `rank_tensor_names` 数一遍张量个数填进 `tensor_count`。产物结构 `KimiRankWeightPlan` 声明在 [src/weights/manifest.rs:L90-L98](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/weights/manifest.rs#L90-L98)。`parallel_config()` 本身只是 [src/config.rs:L355-L357](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/config.rs#L355-L357) 的一行 `ParallelConfig::new(tp_world, dp_world)`。

   关于 `tp_rank` / `ep_rank` 的精确公式：它住在 `pegainfer_frontend::parallel::ParallelConfig`（本 crate 之外，本讲不展开）。本 crate 的测试锁定了可观察行为：TP8/DP1 下 rank 3 的头区间是 24..32（即 tp_rank = 3 = rank）、rank 1 的专家区间是 48..96（即 ep_rank = 1 = rank）；TP1/DP8 下 tp_world = 1 使 tp_rank 恒为 0，头区间必为 0..64。TP1/DP8 下 `ep_rank` 的具体取值公式**待确认**——本讲 4.1.4 的实践正是用单测去验证「ep_rank = rank」这一预测。

3. **专家区间的独立入口**。[src/weights/manifest.rs:L482-L491](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/weights/manifest.rs#L482-L491) 的 `rank_local_expert_range` 单独暴露同一条专家区间（`ep_rank * local_experts..(ep_rank + 1) * local_experts`），点名与切片两条路径都复用它，保证不会算出两份不一致的答案。

4. **整除门**。[src/weights/manifest.rs:L262-L271](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/weights/manifest.rs#L262-L271) 的 `validate` 用一条 `ensure!` 同时卡死三个整除：`KIMI_K2_ROUTED_EXPERTS`（384，[src/config.rs:L30](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/config.rs#L30)）整除 `ep_world`、`KIMI_K2_HEADS`（64，[src/config.rs:L20](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/config.rs#L20)）与 `KIMI_K2_VOCAB`（163,840，[src/config.rs:L14](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/config.rs#L14)）整除 `tp_world`。「每 rank 恰好 48」由此成为不变量：换拓扑时 [src/weights/manifest.rs:L238-L242](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/weights/manifest.rs#L238-L242) 的 `with_parallel_shape` 会带着新 shape 重跑 `validate`（`from_index_json` 默认先给 tp8_ep8，见 [L232](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/weights/manifest.rs#L232)）。

#### 4.1.4 代码实践

**实践目标**：不写代码，先把手算能力练出来——对 rank=3 在两种拓扑下算出三条区间及它们换算出的具体张量切片，再用项目现成的单测核对。

**操作步骤**：

1. 按上面的公式手算 rank=3 的 `tp8_ep8` 与 `tp1_dp8` 区间，并继续换算（×192 = `KIMI_K2_Q_HEAD_DIM`，×256 = nope 128 + v 128，×128 = `KIMI_K2_V_HEAD_DIM`，见 [src/config.rs:L23-L26](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/config.rs#L23-L26)）。
2. 打开 [src/weights/tests.rs:L62-L182](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/weights/tests.rs#L62-L182)（`rank_sliced_load_plan_applies_tp8_ep8_slices`），逐条比对你的手算。
3. 运行：`cargo test --release -p pegainfer-kimi-k2 --features kimi-k2 --lib rank_sliced`。

**需要观察的现象 / 预期结果**：rank=3 的完整答案表（tp1_dp8 列为本讲推导，标注**待本地验证**）：

| 量 | tp8_ep8（tp_rank=3, ep_rank=3） | tp1_dp8（tp_rank=0, ep_rank=3） |
|---|---|---|
| `attention_head_range` | 24..32 | 0..64 |
| `vocab_range` | 61,440..81,920 | 0..163,840 |
| `local_expert_range` | 144..192 | 144..192 |
| `q_b_proj` 行（×192） | 4,608..6,144 | 0..12,288 |
| `kv_b_proj` 行（×256） | 6,144..8,192 | 0..16,384 |
| `o_proj` 列（×128） | 3,072..4,096 | 0..8,192 |
| dense gate/up 行（18,432/8=2,304） | 6,912..9,216 | 0..18,432 |
| shared gate/up 行（2,048/8=256） | 768..1,024 | 0..2,048 |

tp8_ep8 列的每个数字都与 [src/weights/tests.rs:L69-L141](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/weights/tests.rs#L69-L141) 的断言一一对应（embed RowRange 61,440..81,920、`q_b_proj` 4,608..6,144、`kv_b_proj` 6,144..8,192、`o_proj` ColRange 3,072..4,096、dense gate 6,912..9,216、shared down ColRange 768..1,024）。若测试通过而你的手算不一致，回头检查的是乘数（192/256/128），不是代码。注意：构建 `kimi-k2` feature 会连带编译 CUDA 内核；无 GPU 机器可设 `PEGAINFER_CUDA_SM`（如 `120`）绕过 SM 探测——该环境变量行为见 CLAUDE.md，测试能否在无 GPU 环境编译通过**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：手算 rank=6 在 `tp8_ep8` 下的三条区间。
**答案**：tp_rank=ep_rank=6。头：6×8..7×8 = 48..56；词表：6×20,480..7×20,480 = 122,880..143,360；专家：6×48..7×48 = 288..336。

**练习 2**：`tp1_dp8` 下每个 rank 的词表区间都是 0..163,840，覆盖全表——那它和 `Full` 有区别吗？
**答案**：有。区间数值上是「全范围」，但计划里的切片类型仍是 `RowRange { start: 0, end: 163_840 }` 而不是 `Full`（切片由 `rank_plan` 的区间机械生成，不因覆盖全表而升级为 `Full`）。对加载器两者最终拷贝的字节相同（`local_shape` 都返回原形状），但类型不同意味着日志与计划里仍能看到这是「TP 切片退化为全宽」的事实。

**练习 3**：为什么 `q_a_proj`、`kv_a_proj_with_mqa`、两个 layernorm 不在任何切分区间里？
**答案**：它们的形状与注意力头数无关——`q_a_proj` 输出是 1536 的压缩秩、`kv_a_proj_with_mqa` 输出是 512+64 的压缩 KV，norm 是逐元素向量。头切分只作用于「按头堆叠」的上投影矩阵（`q_b_proj`、`kv_b_proj`）与以头维为输入的 `o_proj`，所以这些压缩域张量在所有 rank 上整份复制（`Full`）。

### 4.2 张量命名方案：点名产物 rank_tensor_names 与 rank_weight_names

#### 4.2.1 概念说明

有了三条区间，下一步是产出**点名册**。crate 里有两份看似重叠、实则分工不同的产物：

- **`rank_tensor_names(rank)`**：扁平名单，`Vec<&KimiTensorEntry>`（名字 + 所在分片）。它的主要使命是**数个数**——`rank_plan.tensor_count` 就来自它的 `len()`，后续切片计划的容量与对账都以它为基准。
- **`rank_weight_names(rank)`**：结构化点名册 `KimiRankWeightNames`（[src/weights/manifest.rs:L174-L180](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/weights/manifest.rs#L174-L180)）：按层组织、每层带 attention 九名 + dense/MoE 分型，MoE 层列出本 rank 的 48 个专家并保留 **`global_expert` 全局编号**。它的消费者是打包与校验：INT4 Marlin 打包要知道「哪些专家是我的、叫什么名字」，非专家张量的类型化视图校验要按结构逐名核对。

一句话区分：**`rank_tensor_names` 答「一共几个、在哪片」，`rank_weight_names` 答「按模型结构怎么组织、我的专家是全局几号」**。第三份产物（切片计划，见 4.3）才答「每个名字加载哪一段」。

#### 4.2.2 核心流程

点名规则的伪代码（两种拓扑共用，唯一变量是 `local_expert_range`）：

```text
names = [token_embedding, final_norm, lm_head]          # 顶层 3 个
for layer in 0..61:
    names += 9 个注意力名字                               # 每层都有
    if layer == 0:                                        # 唯一 dense 层
        names += [gate_proj, up_proj, down_proj]
    else:                                                 # 60 个 MoE 层
        names += [router.gate, router.e_score_correction_bias]   # 2
        names += [shared.gate, shared.up, shared.down]           # 3
        for e in 0..384:
            if e ∈ local_expert_range:                    # 48 个命中
                names += 6 个名字                          # gate/up/down × (packed, scale)
```

于是每 rank 张量数为 3 + (9 + 3) + 60 × (9 + 5 + 48 × 6) = 3 + 12 + 18,120 = **18,135**。

两个值得注意的推论：

- **两种拓扑的 `tensor_count` 相同（18,135）**。TP 只改切片边界、不改名单；EP 划分两拓扑一致。名单差异只来自专家区间，而区间相同。
- checkpoint 全量 208,215 与每 rank 18,135 的差 = 190,080 = 每 MoE 层丢弃 3,168 个（非本地专家 336×3 投影×3 名字 = 3,024，加本地专家从不读取的 `weight_shape` 48×3×1 = 144）× 60 层。测试注释（[src/weights/tests.rs:L66-L68](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/weights/tests.rs#L66-L68)）专门强调了 `weight_shape`（8,640 个）不被任何 kernel 读取（#234）。

#### 4.2.3 源码精读

1. **扁平名单**。[src/weights/manifest.rs:L301-L328](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/weights/manifest.rs#L301-L328) 的 `rank_tensor_names`：先取 `rank_local_expert_range`，然后按上面的伪代码顺序 push；专家过滤的关键一行是 [L318](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/weights/manifest.rs#L318) 的 `local_expert_range.contains(&expert.expert_idx)`——按**全局编号**做成员判断，不做「全局 → 本地」的重编号。三个 push 辅助函数在 [src/weights/manifest.rs:L732-L756](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/weights/manifest.rs#L732-L756)（attention 九连 push、dense 三连、INT4 投影两连）。

2. **结构化点名册**。[src/weights/manifest.rs:L427-L480](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/weights/manifest.rs#L427-L480) 的 `rank_weight_names`：先 `rank_plan`（拿到区间与 tensor_count 垫底），再逐层做 `from_manifest` 名字克隆；MoE 分支在 [L443-L449](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/weights/manifest.rs#L443-L449) 对 `routed_experts` 做「filter 区间命中 + map 转换」。`KimiRoutedExpertWeightNames::from_manifest`（[src/weights/manifest.rs:L528-L537](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/weights/manifest.rs#L528-L537)）保留 `global_expert: manifest.expert_idx`——全局编号就是下游 Marlin 打包定位专家槽位的钥匙。

3. **名字字符串从哪来**（u2-l1 已建好，这里只回指）：`layer_tensor` 在 [src/weights/manifest.rs:L710-L719](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/weights/manifest.rs#L710-L719) 拼 `language_model.model.layers.{layer_idx}.{suffix}`，专家名在 [src/weights/manifest.rs:L697-L708](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/weights/manifest.rs#L697-L708)（`mlp.experts.{expert_idx}.{projection}` + `.weight_packed`/`.weight_scale`）。前缀常量 `TEXT_PREFIX` 与索引文件名在 [src/weights.rs:L61-L62](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/weights.rs#L61-L62)。

4. **点名册的消费端**（预告 u2-l3/u2-l4）：`load_rank_sliced_weights_to_gpu` 开头在 [src/weights/load.rs:L82-L87](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/weights/load.rs#L82-L87) 校验 `load_plan.rank == names.rank`——两份产物必须描述同一个 rank；随后 [L146-L151](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/weights/load.rs#L146-L151) 用 `names` 做流式 Marlin 打包、[L171](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/weights/load.rs#L171) 做非专家张量的类型化视图校验，最终 `names.plan.local_expert_range` 被原样缝进 [src/weights/load.rs:L173-L178](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/weights/load.rs#L173-L178) 的 `KimiRankExpertMarlinWeights`——区间从「计划」变成「显存包的属性」。

#### 4.2.4 代码实践

**实践目标**：验证点名个数公式 18,135 与专家归属规则。

**操作步骤**：

1. 读 [src/weights/tests.rs:L20-L30](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/weights/tests.rs#L20-L30)（`rank_tensor_names_filter_local_experts`）：rank 0 有 `experts.0.` 与 `experts.47.`、没有 `experts.48.`；rank 1 反之。
2. 读 [src/weights/tests.rs:L32-L59](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/weights/tests.rs#L32-L59)（`rank_weight_names_are_local_and_typed`）：rank 1 的 `local_expert_range == 48..96`、第 1 层 48 个专家、首尾全局编号 48 与 95。
3. 运行：`cargo test --release -p pegainfer-kimi-k2 --features kimi-k2 --lib rank_weight_names` 与 `... --lib rank_tensor_names`。

**需要观察的现象**：两个测试通过；`rank_weight_names_are_local_and_typed` 断言 `names.layers.len() == KIMI_K2_LAYERS`（61），第 0 层是 Dense、第 1 层是 MoE。

**预期结果**：与手算一致：rank 1 专家 = 全局 48..=95。`tensor_count` 的显式断言（18,135）在 4.3 的切片计划测试里（[src/weights/tests.rs:L68](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/weights/tests.rs#L68)），两处互相印证。

#### 4.2.5 小练习与答案

**练习 1**：不运行代码，算出 rank=3 的点名册里有多少个名字带 `experts.` 片段？
**答案**：60 层 × 48 专家 × 6 名字 = 17,280 个；占全部 18,135 的约 95%。这也是为什么专家权重单独走 Marlin 打包流水线而不挤在通用加载路径里。

**练习 2**：`rank_weight_names` 里 `routed_experts[0].global_expert` 对 rank 3 是多少？如果下游内核想要「本地 0 号专家」，应该看哪个字段？
**答案**：rank 3 的 `local_expert_range` 是 144..192，所以 `[0].global_expert == 144`。「本地 0 号」就是数组下标 0，全局身份看 `global_expert` 字段——本地槽位与全局编号的换算关系 global = 144 + local 由区间保证。

**练习 3**：为什么 `rank_tensor_names` 用 `contains(&expert.expert_idx)` 过滤而不是先算 `local = expert_idx - range.start` 再按本地编号重排？
**答案**：保持名字与 checkpoint 完全一致是最重要的不变量——名字是 safetensors 里的查找键，改了就找不到张量。全局编号到本地槽位的映射留给下游（Marlin 打包按数组顺序分配槽位），加载层不做任何重命名，出错面最小。

### 4.3 切片加载计划：rank_sliced_load_plan 与 KimiTensorLoadSlice

#### 4.3.1 概念说明

切片加载计划是两份产物的合体执行版：**名字（点名）× 区间（rank_plan）→ 逐张量的加载规格**。每个规格是一个三元组 `KimiTensorLoadSpec { name, shard, slice }`，其中 `slice` 是三态枚举：

- `Full`：整份加载（与头/词表/中间维无关的张量、以及 EP 整专家归属的 INT4 权重）；
- `RowRange { start, end }`：取连续的 `[start, end)` 行——在字节层面是**连续区间**，可以一次切片；
- `ColRange { start, end }`：取 `[start, end)` 列——行主序存储里列不连续，必须**逐行 gather** 成紧凑的行主序小块。

规格再**按 shard 分组**成 `KimiShardTensorLoadPlan` 列表：这样消费端每个分片文件只需 `mmap` 一次、反序列化一次，然后把该分片里属于本 rank 的所有切片依次拷入显存。行切片与列切片的差别直接决定了 `sliced_tensor_bytes` 的两种实现路径。

#### 4.3.2 核心流程

`rank_sliced_load_plan(rank)` 的两步：

```text
第一步  rank_tensor_load_specs(rank)：        # manifest.rs:348
    plan = rank_plan(rank)                     # 三条区间 + tensor_count
    vocab_rows = RowRange(vocab_range)          # embed 与 lm_head 共用
    push(embed_tokens, vocab_rows); push(final_norm, Full); push(lm_head, vocab_rows)
    for layer in 0..61:
        push_attention_load_specs(...)          # 见下表
        if dense:  rows = rank_shard_rows(18432, tp_rank, tp_world)
                   gate/up ← RowRange(rows);  down ← ColRange(rows)
        else:      router 两名 ← Full
                   shared_rows = rank_shard_rows(2048, tp_rank, tp_world)
                   shared gate/up ← RowRange; shared down ← ColRange
                   for e in 本地 48 专家: 6 名全 ← Full
    双重自检：名字不重复、个数 == plan.tensor_count

第二步  按 shard 分桶：                        # manifest.rs:330
    by_shard: BTreeMap<shard, Vec<spec>>       # 每个分片一个桶，分片名有序
    → KimiRankSlicedLoadPlan { rank, shards, tensor_count }
```

注意力九名的切法归纳（[src/weights/manifest.rs:L551-L595](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/weights/manifest.rs#L551-L595)）：

| 张量 | 切片 | 边界公式 |
|---|---|---|
| `input_layernorm` / `q_a_proj` / `q_a_layernorm` | Full | 压缩域，与头无关 |
| `q_b_proj` | RowRange | 头区间 × `KIMI_K2_Q_HEAD_DIM`（192） |
| `kv_a_proj_with_mqa` / `kv_a_layernorm` | Full | 压缩 KV，每 rank 整份 |
| `kv_b_proj` | RowRange | 头区间 × (nope 128 + v 128 = 256) |
| `o_proj` | **ColRange** | 头区间 × `KIMI_K2_V_HEAD_DIM`（128）——头维是它的输入 |
| `post_attention_layernorm` | Full | 逐元素 |

`down_proj` 系列同理走列切片：`row_slice_to_col_slice` 把同一个 `[start, end)` 从「行语义」原样翻译成「列语义」（[src/weights/manifest.rs:L614-L621](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/weights/manifest.rs#L614-L621)），因为 gate/up 的输出区间恰好就是 down 的输入区间。

#### 4.3.3 源码精读

1. **切片三态与形状/字节数推导**。[src/weights/load.rs:L6-L10](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/weights/load.rs#L6-L10) 定义枚举；[L12-L32](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/weights/load.rs#L12-L32) 的 `local_shape` 由完整形状推出**加载后的本地形状**（RowRange 得 `[end-start, cols]`，ColRange 得 `[rows, end-start]`）并校验边界；[L34-L36](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/weights/load.rs#L34-L36) 的 `local_bytes` 乘上元素字节数（[L268-L275](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/weights/load.rs#L268-L275) 只认 BF16/F32/I32/U8）。

2. **规格与计划容器**。[src/weights/load.rs:L40-L45](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/weights/load.rs#L40-L45)（`KimiTensorLoadSpec`）、[L47-L51](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/weights/load.rs#L47-L51)（按分片分组）、[L53-L58](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/weights/load.rs#L53-L58)（`KimiRankSlicedLoadPlan`：rank + shards + tensor_count）。

3. **逐张量规格生成**。[src/weights/manifest.rs:L348-L425](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/weights/manifest.rs#L348-L425) 的 `rank_tensor_load_specs`：[L352-L358](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/weights/manifest.rs#L352-L358) 顶层三张量（embed/lm_head 共用同一个 `vocab_rows`）；dense 分支 [L362-L371](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/weights/manifest.rs#L362-L371)；MoE 分支 [L372-L405](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/weights/manifest.rs#L372-L405)（router Full ×2、shared 用 `KIMI_K2_EXPERT_INTERMEDIATE` 2048 切行、专家 Full）。收尾的双重自检在 [L410-L423](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/weights/manifest.rs#L410-L423)：先用 `BTreeSet` 查重名，再对账 `specs.len() == plan.tensor_count`——名单与切片两套逻辑各自数出的个数必须相等，否则拒绝启动。

4. **按分片分桶**。[src/weights/manifest.rs:L330-L346](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/weights/manifest.rs#L330-L346)：`rank_tensor_load_specs` 的扁平结果被 `BTreeMap<shard, Vec<spec>>` 分桶（分片名天然有序），再折叠成 `KimiShardTensorLoadPlan` 列表。三个小工具：`push_load_spec`（[L539-L549](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/weights/manifest.rs#L539-L549)）、`rank_shard_rows`（[L605-L612](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/weights/manifest.rs#L605-L612)，`debug_assert` 整除后按 rank × (total/world) 切行）、`row_slice_to_col_slice`（[L614-L621](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/weights/manifest.rs#L614-L621)）。INT4 投影两条 Full 在 [L597-L603](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/weights/manifest.rs#L597-L603)。

5. **消费端：mmap 一次、逐规格拷贝**。[src/weights/load.rs:L75-L160](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/weights/load.rs#L75-L160) 的 `load_rank_sliced_weights_to_gpu`：外层 `for shard in &load_plan.shards`（[L105](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/weights/load.rs#L105)）对每个分片 `mmap_file` + `SafeTensors::deserialize`（[L108-L110](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/weights/load.rs#L108-L110)）；内层逐规格取 view、算 `local_shape`/`local_bytes`（[L111-L116](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/weights/load.rs#L111-L116)），`Full` 直接整块 H2D，否则先 `sliced_tensor_bytes` 再 H2D（[L117-L128](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/weights/load.rs#L117-L128)）；插入 `BTreeMap` 时的防重复守卫在 [L139-L144](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/weights/load.rs#L139-L144)；全部完成后对账 `loaded_tensor_count == load_plan.tensor_count`（[L158-L164](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/weights/load.rs#L158-L164)）。

6. **行切与列切的字节实现**。[src/weights/load.rs:L213-L258](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/weights/load.rs#L213-L258) 的 `sliced_tensor_bytes`：RowRange 是连续字节区间直切（[L222-L237](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/weights/load.rs#L222-L237)）；ColRange 逐行 `copy_from_slice` 把每行的目标列段拼进紧凑输出（[L238-L255](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/weights/load.rs#L238-L255)）。字节级行为被 [src/weights/tests.rs:L185-L195](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/weights/tests.rs#L185-L195) 固化：3×4 的 BF16 取列 1..3，输出按行主序重排为 `[2,3,4,5, 10,11,12,13, 18,19,20,21]`。

#### 4.3.4 代码实践

**实践目标**：以 `tiny_manifest()` 为已知输入，完整预测 rank=3 的 `rank_sliced_load_plan` 输出，再用现成测试核对。

**操作步骤**：

1. 先读 [src/weights/tests.rs:L223-L298](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/weights/tests.rs#L223-L298)：`tiny_manifest` 手工构造全部 61 层、`text_tensor_count: 208_215`、默认 `parallel: tp8_ep8()`；所有条目经 `top()` 挂同一个分片名 `model-00001-of-00064.safetensors`（[L293-L297](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/weights/tests.rs#L293-L297)）。
2. 据此预测：`tensor_count = 18,135`；`shards` 只有 **1 个桶**（所有名字同一分片）；`embed_tokens`/`lm_head` 是 RowRange 61,440..81,920；专家 143 缺席、144 与 191 的 packed/scale 为 Full、`weight_shape` 全程缺席（[src/weights/tests.rs:L143-L181](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/weights/tests.rs#L143-L181)）。
3. 运行：`cargo test --release -p pegainfer-kimi-k2 --features kimi-k2 --lib rank_sliced_load_plan_applies_tp8_ep8_slices`。

**需要观察的现象**：测试通过，且断言逐条命中你的预测。

**预期结果**：18,135 个规格、单桶分片、专家边界 143/144/191/192 的在场与缺席全部如预测。真实 checkpoint 里分片有 64 个左右，`shards` 会有多桶（BTreeMap 按分片名排序），加载日志（[src/weights/load.rs:L98-L103](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/weights/load.rs#L98-L103)）会打出 `tensors=` 与 `shards=` 计数，可与计划对账（该日志需要真实权重才能观察，**待本地验证**）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `o_proj` 用 ColRange 而 `q_b_proj` 用 RowRange？
**答案**：`q_b_proj` 的输出维是「64 头 × 192」，TP 切的是输出维（行）；`o_proj` 的输入维是「64 头 × 128」，TP 切的是输入维（列）。切输出用行切片（字节连续，便宜），切输入用列切片（逐行 gather，贵但必要）。

**练习 2**：`rank_tensor_load_specs` 结尾为什么要同时查「重名」和「个数」两个不变量？一个不够吗？
**答案**：它们防的是不同的 bug。重名查的是「同一张量被两个分支重复加入」（例如 dense/MoE 分支写错），个数对账查的是「点名册（`rank_plan.tensor_count`，来自 `rank_tensor_names`）与切片生成器各自数出的总数一致」——两条独立代码路径（push 辅助 vs 切片循环）漏掉或多算任何一类张量都会被抓住。只查一个就会放过另一类错误。

**练习 3**：如果未来支持 TP4/DP2（ep_world 仍为 8），哪些数字会变、哪些不变？
**答案**：`heads_per_tp` 变为 16、`vocab_per_tp` 变为 40,960（TP 从 8 降到 4，宽度翻倍）；`local_experts` 仍为 48（E 不变）；`tensor_count` 仍为 18,135（名单只依赖专家划分）；`q_b_proj` 等切片边界按新头宽重算。前提是 `validate` 的整除门通过（64、163,840 均被 4 整除，384 被 8 整除——成立）。

## 5. 综合实践

**任务**：写一个跨拓扑对照单测，把本讲三条区间、点名个数、专家归属全部钉死，并顺带验证 4.1 里标注「待确认」的 TP1/DP8 rank 映射预测。

把下面这段**示例代码**（非项目原有代码）追加到 `src/weights/tests.rs` 末尾，然后运行：

```bash
cargo test --release -p pegainfer-kimi-k2 --features kimi-k2 --lib rank3_plan_matches -- --nocapture
```

```rust
// 示例代码：追加到 src/weights/tests.rs（练习用）
#[test]
fn rank3_plan_matches_across_topologies() {
    let tp8 = tiny_manifest().rank_weight_names(3).unwrap();
    let dp8 = tiny_manifest()
        .with_parallel_shape(KimiK2ParallelShape::tp1_dp8())
        .unwrap()                       // 换拓扑会重跑 validate
        .rank_weight_names(3)
        .unwrap();

    // 三条区间：手算结果（本讲 4.1.4 的表格）
    assert_eq!(tp8.plan.attention_head_range, 24..32);
    assert_eq!(dp8.plan.attention_head_range, 0..64);
    assert_eq!(tp8.plan.vocab_range, 61_440..81_920);
    assert_eq!(dp8.plan.vocab_range, 0..163_840);
    assert_eq!(tp8.plan.local_expert_range, 144..192);
    assert_eq!(dp8.plan.local_expert_range, 144..192);   // EP 划分两拓扑相同

    // 点名个数：TP 只改切片不改名单
    assert_eq!(tp8.plan.tensor_count, 18_135);
    assert_eq!(dp8.plan.tensor_count, 18_135);

    // 专家归属：全局 144..=191，首尾对齐
    for names in [&tp8, &dp8] {
        match &names.layers[1].kind {
            KimiLayerWeightKindNames::Moe(moe) => {
                assert_eq!(moe.routed_experts.len(), 48);
                assert_eq!(moe.routed_experts[0].global_expert, 144);
                assert_eq!(moe.routed_experts[47].global_expert, 191);
            }
            KimiLayerWeightKindNames::Dense(_) => panic!("layer1 must be MoE"),
        }
    }
}
```

**观察与预期**：

- 全部断言通过，则 4.1.4 表格的 tp1_dp8 列（含「ep_rank = rank」的预测）由「待本地验证」升级为「已验证」；
- 若 `dp8.plan.local_expert_range` 不是 144..192，说明 TP1/DP8 下 `ParallelConfig` 的 ep_rank 映射与预测不符——回到 `pegainfer-frontend` 的 `parallel` 模块核对公式，并修正本讲的推导；
- 若想再进一步，把 `rank_weight_names(3)` 换成 `rank_sliced_load_plan(3)` 并断言 `dp8` 侧 `q_b_proj` 为 RowRange 0..12,288、`o_proj` 为 ColRange 0..8,192（切片全宽但类型仍是 Range）。

无 GPU 机器上构建前可设 `PEGAINFER_CUDA_SM`（如 `120`）绕过 SM 探测；本测试本身不碰 GPU，但 crate 构建会连带编译 CUDA 内核，能否在无 GPU 环境通过**待本地验证**。

## 6. 本讲小结

- 一个 rank 的加载需求被压缩成三条半开区间：**头区间与词表区间按 tp_rank 切，专家区间按 ep_rank 切**；`rank_plan`（manifest.rs:275）用三行乘法生成它们，`validate` 的整除门保证切得尽。
- 两种拓扑的 `ep_world` 同为 8，所以 **384 个专家在两种拓扑下都是每 rank 恰好 48 个**（384 = 8 × 48）；TP 的差异只体现在头/词表/FFN 中间维的切片宽度上。
- 两份产物分工明确：`rank_weight_names` 是**结构化点名册**（供 Marlin 打包与类型化校验消费，保留 `global_expert` 全局编号），`rank_sliced_load_plan` 是**逐张量切片加载计划**（`name + shard + slice` 三元组按分片分桶，供 `load_rank_sliced_weights_to_gpu` 消费）；`rank_tensor_names` 是垫在两者之下的扁平名单，提供 `tensor_count` 基准。
- 切片三态 `Full / RowRange / ColRange` 对应「整份 / 切输出维（字节连续）/ 切输入维（逐行 gather）」；`q_b_proj`、`kv_b_proj`、gate/up 走行切，`o_proj`、down_proj 走列切，压缩域张量与 INT4 专家权重整份。
- 每 rank 加载 18,135 个张量（3 + 12 + 60 × 302），比 checkpoint 全量少掉的 190,080 个 = 其他 rank 的专家 + 从不读取的 `weight_shape`；计划侧有重名与个数双检，加载侧有 rank 一致性与计数对账，双向兜底。

## 7. 下一步学习建议

下一讲 **u2-l3「INT4 Marlin 权重打包」**接本讲的尾巴：`local_expert_range` 在 [src/weights/load.rs:L173-L178](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/weights/load.rs#L173-L178) 被缝进 `KimiRankExpertMarlinWeights`，随后那些以 `Full` 整份加载的 `weight_packed`/`weight_scale` 会被重排成 Marlin 核需要的布局并融合 w13。建议先自己重读 `pack_loaded_expert_marlin_layers` 的调用点，再进入 u2-l3。若想先看这些区间在运行时如何变成张量宽度，可跳读 [src/config.rs:L338-L352](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kimi-k2/src/config.rs#L338-L352) 的 `KimiLocalDims`（u5 系列会大量使用）。
