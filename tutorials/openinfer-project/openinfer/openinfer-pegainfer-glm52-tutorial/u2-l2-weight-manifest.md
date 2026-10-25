# u2-l2 checkpoint 布局与权重清单

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 `model.safetensors.index.json` 如何被解析为 `Glm52WeightManifest`，以及「张量名 → 分片文件」这张表如何驱动后续一切装载规划。
2. 复述 rank bundle 切分的分配原则：**非专家栈全量复制到每个 rank、256 个路由专家按 rank 区间精确切分、TP4 张量复制形态不带专家包**，并能读懂 `validate_rank_coverage` 双向差集的防漏逻辑。
3. 手推 expert region 的四个步长常量（每个专家恰好 36 MiB + 9 KiB），解释为什么 expert-major 打包**必须在 H2D 拷贝时完成**而不能加载后再重排。
4. 说明第 78 层（MTP 层）权重在「checkpoint 覆盖校验」与「resident 加载计划」两套清单里的不同归属规则。

本讲承接 u2-l1：那里钉死了全部几何常量并核对了 `config.json`；本讲顺着 `validate_startup` 的下一步走——probe 通过后，引擎要回答的问题是「磁盘上这数百个 safetensors 分片里的几千个张量，每个该进哪张 GPU、放在缓冲区的哪个偏移」。

## 2. 前置知识

- **safetensors 与 index.json**：safetensors 是 Hugging Face 的安全张量序列化格式（文件头是 JSON 元数据，正文是裸字节）。大模型通常切成多个分片文件（`model-00001-of-000XX.safetensors` …），另有一个索引文件 `model.safetensors.index.json`，其中 `weight_map` 字段是「张量名 → 分片文件名」的映射。加载器只需要按这张表逐名取张量。
- **EP 与 TP 两种并行**（u1-l2 已引入 `--moe-topo`）：
  - **EP（专家并行）**：256 个路由专家按 rank 均分（EP8 每 rank 32 个），每个专家**整只**落在一张卡上；非专家部分（embedding、注意力、dense MLP、路由器、共享专家）每张卡**全量复制**。
  - **TP4（张量并行）**：4 张卡镜像执行同一逻辑 rank，专家与共享专家被切成行/列切片，由独立的第二趟装载（`load_tp_slice_layer`）负责。
- **H2D（host-to-device）拷贝**：CPU 内存到 GPU 显存的拷贝。本讲的 loader 用 `mmap` 把分片文件映射进地址空间，再从映射区直接拷入显存。
- **fp8 块量化**（u2-l1 的 `quantization_config` 契约）：权重本体是 e4m3（每元素 1 字节），每 128×128 块配一个 f32 反缩放因子——所以**每个 fp8 权重张量都成对携带一个 `weight_scale_inv`**。scale 张量形状是 \(\lceil rows/128 \rceil \times \lceil cols/128 \rceil\) 的 f32。
- **fail-fast 双向校验**：本讲的校验不是「尽力而为」，而是「清单与 checkpoint 逐名精确相等，差一个就拒绝启动」。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `src/weights.rs` | 本讲主角：manifest 解析、rank bundle 切分、expert region 步长、`expert_placement` 落位函数、`expected_tensor_contract` 张量契约、覆盖校验与全部纯 CPU 单测 |
| `src/config.rs` | 权重清单消费的几何常量（`GLM52_ROUTED_EXPERTS=256`、`GLM52_EXPERT_INTERMEDIATE=2048`、`GLM52_MTP_LAYER=78` 等） |
| `src/lib.rs` | `Glm52MoeTopo` 拓扑派生量（`device_count` / `ep_local_experts` / `uses_ep_expert_bundles`）；`validate_startup` 在启动链路里调用 `all_rank_load_bundles` 并打日志 |
| `src/weights/load.rs` | bundle 的消费端：逐分片 mmap、契约核对、专家张量按 offset 直接写入打包区、装载后完整性断言 |
| `src/moe_tp.rs` | TP4 第二趟切片装载，经 `manifest.shard_for` 反查任意专家张量所在分片 |

## 4. 核心概念与源码讲解

### 4.1 manifest 解析：从 index.json 到张量契约

#### 4.1.1 概念说明

`Glm52WeightManifest` 是 checkpoint 的**目录页**：它不持有任何权重字节，只持有一张 `BTreeMap<String, String>`（张量名 → 分片文件名）。在这张目录页之上，本 crate 还维护第二套知识——**张量契约** `expected_tensor_contract`：凭 u2-l1 钉死的几何常量，每个合法张量名的 dtype 与 shape 都能**从名字推导出来**，不需要读分片文件头。

两套知识配合成一个闭环：装载时每取一个张量，先问契约「它应该长什么样」，再核对分片里的真实视图——dtype、shape、字节数三关全过才拷入 GPU。这意味着「拿错几何的 checkpoint」在 probe 阶段就死了，而「名字对但内容形状错」的损坏分片在装载第一关就死。

#### 4.1.2 核心流程

```text
from_model_dir(model_path)
  → 读 model.safetensors.index.json，serde_json 解析
  → from_index_json: 取 weight_map 对象 → BTreeMap<张量名, 分片文件>
  → validate_rank_coverage()          # 4.2 详讲：与生成清单双向求差
  → Ok(Glm52WeightManifest)

之后两条消费路径：
  A. all_rank_load_bundles(topo, native_mtp)   # 4.2：按拓扑切 rank 加载包
  B. shard_for(name)                           # TP4 切片第二趟反查分片
```

契约推导的关键结构：每个 fp8 投影经 `push_fp8` 登记为**两个名字**（`*.weight` + `*.weight_scale_inv`），契约分别为：

\[ \text{weight}: \text{F8\_E4M3} \ [rows, cols], \qquad \text{scale}: \text{F32} \ [\lceil rows/128\rceil, \lceil cols/128\rceil] \]

#### 4.1.3 源码精读

**manifest 构造**——[src/weights.rs:L234-L259](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/weights.rs#L234-L259)：`from_model_dir` 拼出索引文件路径（文件名常量 `GLM52_WEIGHT_INDEX` 定义于 [L38](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/weights.rs#L38)），读盘、解析、交给 `from_index_json`。后者要求顶层有 `weight_map` 对象且每项值是字符串，装进 `BTreeMap` 后**立即**调用 `validate_rank_coverage`——清单不完整的 checkpoint 连 manifest 都构造不出来。`BTreeMap` 保证后续所有按名迭代都是字典序确定性的。

**张量契约主入口**——[src/weights.rs:L498-L547](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/weights.rs#L498-L547)：`expected_tensor_contract(name)` 按名字分派。三个全局张量（`embed_tokens` / `lm_head` 是 `[VOCAB, HIDDEN]` bf16，`model.norm` 是 `[HIDDEN]` bf16）先判；随后 `parse_layer_index` 解出层号并要求 \(\text{layer} \le 78\)（[L506-L510](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/weights.rs#L506-L510)，MTP 层是合法上限）；再依次尝试 MTP 专属形状、norm、注意力投影、indexer、MLP 三族契约，**全部未命中则报「无契约」**——引擎明确拒绝认识以外的张量。

**投影契约的 scale 形状**——[src/weights.rs:L621-L637](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/weights.rs#L621-L637)：`projection_contract` 是所有 fp8 投影的公共形状逻辑——`{stem}.weight` 是 `[rows, cols]` e4m3，`{stem}.weight_scale_inv` 是 `ceildiv(128)` 后的 f32。注意力五投影的 rows/cols 见 [L549-L576](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/weights.rs#L549-L576)（如 `q_b_proj` 是 `[Q_B_OUT=16384, Q_LORA=2048]`），indexer 契约见 [L578-L601](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/weights.rs#L578-L601)（`wk` 是 `[128, 6144]`，`wq_b` 是 `[4096, 2048]`）。dense 层与专家层共用 `mlp_contract`（[L603-L619](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/weights.rs#L603-L619)），但中间维不同：前 3 层 12288，之后 2048——**同一个 `gate_proj` 后缀在 dense 层和专家层形状不同**，靠层号区分。

**字节数换算**——[src/weights.rs:L218-L232](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/weights.rs#L218-L232) 与 [L646-L653](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/weights.rs#L646-L653)：`byte_len` 用 checked_mul 防溢出地算「元素数 × 每元素字节」，dtype 支持表极短——e4m3 算 1 字节、bf16 算 2、f32 算 4，其它直接报错。这个字节数后面要当**装载账本**用（4.3）。

**TP4 的反查接口**——[src/weights.rs:L271-L278](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/weights.rs#L271-L278)：`shard_for(name)` 返回某张量所在分片文件名。它的注释点明用途——TP 切片第二趟需要解析**每一个**专家的张量（不只是本 rank 那 32 只），实际调用点在 [src/moe_tp.rs:L246-L253](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/moe_tp.rs#L246-L253)。

#### 4.1.4 代码实践

1. **实践目标**：用契约公式手推两个张量的 dtype / shape / 字节数，再（如有本地 checkpoint）与真实 index.json 对账。
2. **操作步骤**：
   - 推导 `model.layers.0.self_attn.q_b_proj.weight` 与 `...weight_scale_inv`：从 u2-l1 常量 `Q_B_OUT=16384`、`Q_LORA_RANK=2048` 出发，套用上面的公式算 shape 与 `byte_len`。
   - 若本地有 GLM5.2 模型目录，用下面的示例脚本核对（**示例代码**，非项目原有）：
     ```bash
     python3 -c "
     import json
     m = json.load(open('models/GLM5.2/model.safetensors.index.json'))['weight_map']
     names = [n for n in m if 'layers.0.self_attn.q_b_proj' in n]
     print([(n, m[n]) for n in names], len(m))"
     ```
3. **需要观察的现象**：`weight` 是 e4m3 `[16384, 2048]` 共 \(16384 \times 2048 \times 1 = 33\,554\,432\) 字节（恰 32 MiB）；`weight_scale_inv` 是 f32 \([128, 16]\) 共 8192 字节；两个名字映射到**同一个**分片文件。
4. **预期结果**：手推值与 [src/weights.rs:L549-L576](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/weights.rs#L549-L576) 的契约及单测 [L743-L752](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/weights.rs#L743-L752)（断言 `q_b_proj` 为 `[16384, 2048]`、`kv_b_proj` 为 `[28672, 512]`）一致。无本地模型时脚本部分**待本地验证**，手推部分可立即完成。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `mlp_contract` 需要层号参数，而 `attention_projection_contract` 不需要？
答案：注意力的投影形状（q_a/q_b/kv_a/kv_b/o_proj）在全部 78 层一致，尺寸只由 MLA 常量决定；而 `gate_proj`/`up_proj`/`down_proj` 的中间维在前 3 层是 `GLM52_DENSE_INTERMEDIATE=12288`、之后是 `GLM52_EXPERT_INTERMEDIATE=2048`，同名后缀形状随层变，必须靠 [L611-L615](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/weights.rs#L611-L615) 的层号分支区分。

**练习 2**：`weight_map` 里若多出一个 `model.layers.5.foo.weight`，会在哪一步、以什么方式失败？
答案：构造 manifest 时的 `validate_rank_coverage`（4.2）：生成清单里没有任何规则会产出这个名字，它会落进「checkpoint 有、清单没有」的差集，启动报 `missing_sample=[...]`。即失败发生在**装载任何字节之前**。

**练习 3**：`dtype_element_bytes` 遇到 `F16` 会怎样？这体现了什么设计？
答案：报错退出（[src/weights.rs:L646-L653](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/weights.rs#L646-L653) 只列 e4m3/bf16/f32）。契约层就锁死 dtype 集合，配合 u2-l1 probe 的 `dtype=bfloat16` 门，半精度变体 checkpoint 无从进入装载。

### 4.2 rank bundle 切分与覆盖校验

#### 4.2.1 概念说明

manifest 只回答「张量在哪个文件」；`all_rank_load_bundles` 回答「**这张卡**要哪些张量、按什么顺序」。它为拓扑里每个 rank 生成一个 `Glm52RankLoadBundle`，内含：

- **plan**：rank 号、该 rank 的专家区间 `expert_range`、张量总数 `tensor_count`、是否 native MTP；
- **shards**：按分片文件归组的加载计划 `Vec<Glm52ShardLoadPlan>`——装载时**一个分片一次 mmap**，把它名下的张量按序取完再换下一个。

分配原则一句话：**非专家栈全量复制，专家按区间切分**。区间公式：

\[ \text{expert\_range}(r) = [\,r \cdot L,\ (r+1) \cdot L\,), \qquad L = \frac{256}{\text{EP 宽度}} \]

EP8 → 每 rank 32 只，EP4 → 64 只，EP64 → 4 只。TP4 不切专家（`uses_ep_expert_bundles()` 为 false），bundle 的 `expert_range` 是空区间 `0..0`，专家与共享专家全靠第二趟切片装载。

「覆盖校验」是切分前的地基：`validate_rank_coverage` 用一个固定分区生成全部应有的名字，与 checkpoint 的键集**双向求差**，两边都必须为空——既防漏装（checkpoint 有张量没人要），也防空指针（计划里有名字 checkpoint 里没有）。

#### 4.2.2 核心流程

```text
validate_startup (lib.rs)
  manifest = Glm52WeightManifest::from_model_dir(model_path)?   # 内含覆盖校验
  rank_bundles = manifest.all_rank_load_bundles(moe_topo, native_mtp)?
  取 options.ranks 区间内的 bundle → 记录 tensor_count / expert_range → 打日志

all_rank_load_bundles
  for rank in 0..moe_topo.device_count():
      rank_load_bundle(rank)
        names = rank_resident_tensor_names(rank, topo, native_mtp)   # 生成该 rank 的名字清单
        按 weight_map 把名字归组到分片文件（BTreeMap → 分片有序）
        expert_range = uses_ep ? rank*local..(rank+1)*local : 0..0

rank_resident_tensor_names 的生成规则（EP 形态）：
  非专家栈（全量复制）：embed_tokens / model.norm / lm_head
      + 每层 14 个注意力张量（+full indexer 层再 7 个）
      + 前 3 层 dense MLP ×6，其余层 路由器×2 + 共享专家×6
  专家（区间切分）：75 个 MoE 层 × 本 rank L 只专家 × 每只 6 张量
  MTP 第 78 层：仅 native_mtp=true 时追加（4.4）
```

#### 4.2.3 源码精读

**bundle 切分入口**——[src/weights.rs:L261-L269](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/weights.rs#L261-L269)：`all_rank_load_bundles` 对 `0..moe_topo.device_count()` 逐 rank 调 `rank_load_bundle` 并收集。设备数来自 [src/lib.rs:L227-L236](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/lib.rs#L227-L236)（EP8=8、EP4/TP4=4、EP16/32/64 各自），每 rank 专家数来自 [src/lib.rs:L271-L278](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/lib.rs#L271-L278) 的 `ep_local_experts = 256 / expected_ep_size`。

**单个 bundle 的组装**——[src/weights.rs:L280-L322](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/weights.rs#L280-L322)：先由 `rank_resident_tensor_names` 生成名字清单；再把每个名字经 `weight_map` 查到分片文件，`entry().or_default()` 归组进 `by_shard`（BTreeMap，分片按字典序处理）；最后组装 `plan`（注意 [L303-L309](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/weights.rs#L303-L309) 的注释：张量复制形态的 resident 计划不含专家张量，bundle 拿空区间）。

**resident 名单生成**——[src/weights.rs:L342-L375](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/weights.rs#L342-L375)：`rank_resident_tensor_names` 先 push 非专家栈（EP 形态 `include_shared_experts=true`），再在 EP 分支里为 3..78（不含 78）的每层 push 本 rank 区间的专家；`native_mtp` 为真时追加 MTP 非专家名单与第 78 层的专家区间。非专家栈的构成见 [L377-L399](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/weights.rs#L377-L399)：逐层 14 个注意力张量（[L412-L435](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/weights.rs#L412-L435)，indexer 张量**按 checkpoint 是否存在 `k_norm.weight` 决定**是否追加 7 个）+ dense/MoE 分支；专家名单由 [L483-L491](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/weights.rs#L483-L491) 的 `push_routed_experts` 按 `gate/up/down × {weight, scale}` 展开成每专家 6 个名字。

**覆盖校验（防漏核心）**——[src/weights.rs:L437-L462](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/weights.rs#L437-L462)：`validate_rank_coverage` 用 [L324-L340](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/weights.rs#L324-L340) 的 `rank_tensor_names`（固定 EP8 分区，**与启动拓扑无关**——见 [L39-L45](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/weights.rs#L39-L45) 注释：覆盖校验只需要「某个」能盖满全部张量的分区）把 8 个 rank 的名字并成 `generated` 集合，与 checkpoint 键集做两次 `difference`：

- `missing` = checkpoint 有、任何 rank 都不要 → 白白躺在盘上，多半是清单规则漏了一种张量；
- `extra` = 清单生成了、checkpoint 没有 → 装载时 `weight_map.get` 会落空。

两个差集各抽样 5 个进错误消息，非空即拒绝。**这正是「manifest 不变量」与「resident 计划」的分层**：覆盖校验按 checkpoint 全量（含第 78 层）生成，resident 计划则刻意少装服务用不到的张量——两者是两套清单，前者保证磁盘完整可解释，后者保证 H2D 零浪费。

**启动链路的消费点**——[src/lib.rs:L1668-L1687](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/lib.rs#L1668-L1687)：`validate_startup` 里先建 manifest 再切 bundles，取 `options.ranks` 区间（跨节点部署时每进程只 host 自己的 rank，u8-l2）内的 `tensor_count` 与 `expert_range` 记入 `StartupValidation` 并打出日志 `rank_expert_ranges=... rank_plan_tensors=...`——这条日志是实践任务的观察窗口。bundle 随后按 rank 发给各自的 worker 线程（`src/runner.rs` 的指令处理，见 [src/runner.rs:L322](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/runner.rs#L322)），由 4.3 的 loader 消费。

**拓扑单测佐证**——[src/weights.rs:L884-L915](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/weights.rs#L884-L915)：`ep4_resident_plan_carries_64_expert_bundles` 断言 EP4 rank 1 恰好持有 64..128 区间、rank 4 越界报错；[L754-L779](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/weights.rs#L754-L779)：`resident_load_plans_exclude_validation_only_and_duplicate_tensors` 断言 EP8 名单含 shared_experts 与 experts、TP4 名单两者皆无（走切片第二趟）。

#### 4.2.4 代码实践

1. **实践目标**：不看答案，手算 EP8（无 MTP）单个 rank 的 `tensor_count`，并预测启动日志字段。
2. **操作步骤**：
   - 按下表逐类数名字（每层注意力 14 个、full indexer 层再 7 个——full 层共 21 个，u2-l1 已推）：
     | 类别 | 数量 |
     |---|---|
     | 全局（embed / norm / lm_head） | 3 |
     | 注意力 78 层 × 14 | 1092 |
     | indexer 21 层 × 7 | 147 |
     | dense MLP 3 层 × 6 | 18 |
     | MoE 非专家 75 层 × (路由 2 + 共享 6) | 600 |
     | 路由专家 75 层 × 32 只 × 6 | 14400 |
   - 求和得 `tensor_count`；再预测 `validate_startup` 日志里 8 个 rank 的 `rank_expert_ranges` 与 `rank_plan_tensors`。
   - 交叉验证：EP8 区间切分有现成单测 [L697-L706](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/weights.rs#L697-L706)（`ranges[0]=0..32`、`ranges[7]=224..256`、总长 256）。
3. **需要观察的现象**：六类相加恰为 16260；EP4 同法算出 1860 + 75×64×6 = 30660。
4. **预期结果**：EP8 无 MTP 每 rank 16260 个张量；日志形如 `rank_expert_ranges=[0..32, 32..64, …, 224..256], rank_plan_tensors=[16260; 8]`。有 GPU 环境时可实际启动核对日志（**待本地验证**）；手算部分不依赖运行。
5. 延伸：想想 `tensor_count` 的下游用途——loader 末尾用它断言「计划多少、装载多少」（4.3）。

#### 4.2.5 小练习与答案

**练习 1**：覆盖校验为什么固定用 EP8 分区，而不随 `--moe-topo` 变？
答案：[src/weights.rs:L39-L45](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/weights.rs#L39-L45) 注释写明：服务路径的 rank/专家数从启动拓扑派生，而覆盖校验只需要「某个」能盖满每个 checkpoint 张量的分区——256 = 8×32 的固定切分与拓扑无关地回答「清单规则是否完整」。换个拓扑（如 EP64）时，覆盖不变量不变，变的只是 resident 计划的区间。

**练习 2**：resident 计划刻意「少装」了哪些张量？为什么？
答案：两处：不开 native MTP 时的整个第 78 层（服务模型不消费），以及 TP4 首趟计划里的共享专家与全部路由专家（由 `load_tp_slice_layer` 切片装载，首趟再装就是浪费 H2D 带宽与临时 HBM——见 [L342-L345](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/weights.rs#L342-L345) 注释）。装载后还有 `ensure_consumed` 兜底（[src/weights/load.rs:L143-L159](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/weights/load.rs#L143-L159)）：计划里的张量必须全部被模型取走，否则报错。

**练习 3**：`by_shard` 用 `BTreeMap` 而不是 `HashMap`，除了确定性还有什么实际意义？
答案：分片按文件名字典序处理，同一拓扑、同一 checkpoint 的装载顺序完全确定——这让「最慢分片」等性能日志（[src/weights/load.rs:L336-L350](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/weights/load.rs#L336-L350)）跨进程可比较，也让 u2-l3 将讲的 mmap 分组行为可复现。

### 4.3 expert region 步长常量与 H2D 直接落位

#### 4.3.1 概念说明

u5-l2 将讲到的 DeepEP dispatch 契约要求专家权重排成 **expert-major 打包**：每层的本 rank 专家拼成四个连续大区（W13 权重 / W13 scale / W2 权重 / W2 scale），区内第 i 只专家占 `[i×stride, (i+1)×stride)`，W13 区里每只专家内部是 `[gate; up]` 两段拼接。问题：checkpoint 里专家是**逐张量散装**的（`experts.37.gate_proj.weight` 一个独立张量），谁来做重排？

答案：**装载器在 H2D 拷贝时直接按最终偏移写入**。源码注释给出了硬约束：EP8 一个 rank 的专家板约 85 GiB，若先原样载入再在 GPU 上重排，板子加打包副本 ≈ 170 GiB，超过 141 GiB 的 HBM——**重排在物理上不可能**，落位只能发生在 H2D 那一次拷贝里。为此 `weights.rs` 用四个步长常量把「每只专家在每类区里的位置」变成纯算术。

#### 4.3.2 核心流程

设 \(I = 2048\)（专家中间维）、\(H = 6144\)、块 \(B = 128\)，则：

\[
\begin{aligned}
\text{PROJ\_W13} &= I \times H = 12{,}582{,}912 \text{ B（gate 或 up 之一，恰 12 MiB）} \\
\text{W13\_WEIGHT\_STRIDE} &= 2 \times \text{PROJ\_W13} = 24\ \text{MiB（gate 段 + up 段）} \\
\text{W13\_SCALE\_STRIDE} &= 2 \times \tfrac{I}{B} \times \tfrac{H}{B} \times 4 = 6144 \text{ B} \\
\text{W2\_WEIGHT\_STRIDE} &= H \times I = 12\ \text{MiB（down 单独成区）} \\
\text{W2\_SCALE\_STRIDE} &= \tfrac{H}{B} \times \tfrac{I}{B} \times 4 = 3072 \text{ B}
\end{aligned}
\]

单只专家合计 \(24\,\text{MiB} + 6\,\text{KiB} + 12\,\text{MiB} + 3\,\text{KiB} = 37{,}758{,}192\) 字节；每层每 rank（32 只）\( \approx 1.125 \) GiB；75 个 MoE 层 \( \approx 84.4 \) GiB——与源码「~85 GiB」注释互证。

落位算法（`expert_placement`）：

```text
名字形如 model.layers.{L}.mlp.experts.{e}.{proj}
  e ∉ rank 区间 → 报错（计划被破坏，fail-fast）
  e ∈ 区间      → local = e - 区间起点
  proj 分派：
    gate_proj.weight        → W13Weight 区, local × W13_WEIGHT_STRIDE
    up_proj.weight          → W13Weight 区, 上者 + PROJ_W13（紧随 gate 段）
    *_weight_scale_inv      → 对应 scale 区, 同构偏移
    down_proj.*             → W2 区, local × W2_STRIDE
```

#### 4.3.3 源码精读

**步长常量与「为何必须 H2D 落位」**——[src/weights.rs:L49-L66](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/weights.rs#L49-L66)：注释直说打包布局必须与 `Glm52MoeExpertBank::pack_from_host` **逐字节一致**、且「装载后重排不可行——85 GiB 板 + 等大副本 > 141 GiB HBM，所以落位发生在 loader 里」。五个常量全部由 `GLM52_EXPERT_INTERMEDIATE × GLM52_HIDDEN` 等几何表达式书写。

**区类型与区大小**——[src/weights.rs:L68-L98](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/weights.rs#L68-L98)：`Glm52ExpertRegionKind` 四值枚举（W13Weight / W13Scale / W2Weight / W2Scale），`region_bytes(local)` = `local_experts × expert_stride`——EP8 是 32 只、EP4 是 64 只（注释点名），同一公式服务所有 EP 宽度。

**落位函数**——[src/weights.rs:L100-L162](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/weights.rs#L100-L162)：`expert_placement` 先剥 `model.layers.` 与 `.mlp.experts.` 前缀——**剥不动说明不是专家张量，返回 `None` 走普通张量路径**；剥得动则解析层号/专家号，专家号不在本 rank 区间立即报错（[L136-L139](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/weights.rs#L136-L139)）；随后按 4.3.2 的表分派 `(区类型, offset)`。注意 `up_proj` 的偏移是 `local × stride + PROJ_W13_BYTES`——gate 段之后的下半场。

**消费端：按偏移直写**——[src/weights/load.rs:L204-L287](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/weights/load.rs#L204-L287)：装载循环对每个张量先过三关契约核对（dtype / shape / bytes，[L214-L236](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/weights/load.rs#L214-L236)）；`expert_placement` 命中时，按层懒分配四个区（`Glm52ExpertLayerRegions::alloc`，[src/weights/load.rs:L89-L104](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/weights/load.rs#L89-L104)），校验 `offset + bytes ≤ 区长` 后调用 `uploader.upload(ctx, view.data(), region, placement.offset)` **直接把 mmap 里的字节写进最终位置**（[L238-L263](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/weights/load.rs#L238-L263)），并把写入字节数累计进 `region_written`。未命中则走普通分支：独立分配一块设备缓冲整存（[L264-L281](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/weights/load.rs#L264-L281)）。

**装载账本与空洞防线**——[src/weights/load.rs:L295-L320](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/weights/load.rs#L295-L320)：三条终检——装载张量数 == `plan.tensor_count`（4.2 手算的那个数在此对账）、装载字节数 == `planned_total_bytes`（`bundle.planned_total_bytes` 用契约逐张量累加，[src/weights.rs:L190-L206](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/weights.rs#L190-L206)）、以及**每个区写入字节数 == 区容量**。最后一条最关键：区是先分配后零散写入的，若某个专家张量缺席，区里会留下未初始化的「洞」，陈旧的设备字节将静默变成模型权重——`region_written` 逐 (layer, region) 对账，注释写明「洞 = 陈旧字节悄悄成为权重」。

**打包正确性的单测**——[src/weights.rs:L708-L740](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/weights.rs#L708-L740)：`expert_placement_matches_from_host_packing` 让 rank 1（区间 32..64）的六种后缀按 checkpoint 顺序落位，断言偏移无洞连续、且总长恰为 `region_bytes(32)`——即 H2D 落位与旧的 host 打包路径逐字节等价。

#### 4.3.4 代码实践

1. **实践目标**：手推五个步长常量与「每 rank 专家板字节数」，验证 ~85 GiB 的物理约束；再运行打包等价单测。
2. **操作步骤**：
   - 代入 \(I=2048, H=6144, B=128\)，按 4.3.2 公式笔算五个常量与单专家合计。
   - 算每层（32 只）与 75 层累计，与 [src/weights.rs:L53-L54](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/weights.rs#L53-L54) 注释的「~85 GiB」对账。
   - 有 CUDA 工具链的机器上运行纯 CPU 逻辑的打包单测：
     ```bash
     cargo test --release -p pegainfer-glm52 --features glm52 --lib weights::tests::expert_placement_matches_from_host_packing -- --nocapture
     ```
3. **需要观察的现象**：12 MiB / 24 MiB / 6144 B / 12 MiB / 3072 B；单专家 37,758,192 B；75 层累计 ≈ 84.4 GiB——「板 + 等大副本」确实放不进 141 GiB HBM，重排不可行的论证成立。
4. **预期结果**：手算与常量定义逐一吻合。测试命令属于 `--lib` 单测族（CLAUDE.md 记载全 workspace lib 测试约 9 秒），但编译 crate 需要 CUDA 工具链，本机若无则**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `up_proj.weight` 的偏移要加 `EXPERT_PROJ_W13_BYTES` 而 `gate_proj.weight` 不用？
答案：W13 区内每只专家是 `[gate; up]` 拼接（[L142-L152](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/weights.rs#L142-L152)）：gate 落在专家段起点，up 紧随其后偏移半个步长。拼接布局让 u5 的 DeepGEMM 一次 GEMM 同时吃 gate 和 up 两半。

**练习 2**：假如删掉 `region_written` 对账，哪种缺陷会静默逃逸？
答案：缺张量（例如某层某专家的 `down_proj.weight_scale_inv` 因清单 bug 没进计划且恰好绕过覆盖校验）时，该偏移区间保持 `cudaMalloc` 的陈旧字节并成为「权重」——模型照跑但输出错误，且无任何报错。对账把这类洞变成启动失败（[src/weights/load.rs:L308-L320](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/weights/load.rs#L308-L320)）。

**练习 3**：EP64 下 `region_bytes` 是多少只专家的量？这个数从哪条链路传进来？
答案：4 只（256/64）。`Glm52ExpertLayerRegions::alloc(ctx, bundle.plan.expert_range.len())` 的实参来自 bundle 的区间长度，而区间由 `moe_topo.ep_local_experts()` 在 4.2 的切分阶段写入——拓扑 → bundle → loader 一条链，loader 自身不感知拓扑。

### 4.4 MTP layer-78 权重归属

#### 4.4.1 概念说明

u2-l1 讲过 `GLM52_MTP_LAYER = 78`：checkpoint 在 78 个主干层之后多带一个 MTP（multi-token prediction）解码器层。本讲必须讲清它在**两套清单**里的不同待遇：

1. **覆盖校验（manifest 不变量）**：第 78 层的**全部**非专家张量 + 全部 256 只专家**永远**在 `rank_tensor_names` 的生成集里——不管要不要用 MTP，清单必须能完整解释 checkpoint 的每个字节。
2. **resident 计划（装载计划）**：第 78 层**只在 `--glm52-native-mtp` 开启时**进入加载计划，且形态随拓扑分叉：EP 形态装载「MTP 非专家名单 + 本 rank 区间在第 78 层的专家」；TP4 只装载 bookend（enorm/hnorm/eh_proj/shared_head.norm）与注意力等，专家照旧走切片第二趟。

一句话：**覆盖校验管「checkpoint 可解释」，resident 计划管「服务用多少装多少」**，第 78 层是两者差异最大的样本。

#### 4.4.2 核心流程

```text
覆盖校验侧 (rank_tensor_names, 固定 EP8):
  push_checkpoint_non_expert_names
    = resident 非专家(含共享专家) + push_mtp_non_expert_names(含共享专家)
  + 逐层 3..=78 push_routed_experts(rank 区间)     # 注意 ..= 含第 78 层

resident 侧 (rank_resident_tensor_names):
  EP 形态且 native_mtp:
    push_mtp_non_expert_names(include_shared=true)
    push_routed_experts(layer=78, 本 rank 区间)      # 只装自己那 32/64/… 只
  TP4 且 native_mtp:
    push_mtp_non_expert_names(include_shared=false)  # 专家与共享专家走切片趟
  否则: 第 78 层完全缺席
```

#### 4.4.3 源码精读

**MTP 非专家名单**——[src/weights.rs:L401-L410](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/weights.rs#L401-L410)：`push_mtp_non_expert_names` 推入四个 MTP 专属张量（`enorm` / `hnorm` / `eh_proj` / `shared_head.norm`），复用 `push_attention_names` 装注意力，再按 `include_shared_experts` 决定是否带共享专家。这些名字的契约在 [L512-L521](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/weights.rs#L512-L521)：三个 norm 是 `[6144]` bf16；`eh_proj` 是 `[6144, 12288]` bf16——输入是 hidden 与 embedding 的**拼接**（宽 \(2H\)），这是 u9-l3 MTP 数学的形状伏笔。

**覆盖侧永远含 78 层**——[src/weights.rs:L324-L340](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/weights.rs#L324-L340)：`rank_tensor_names` 的专家循环是 `GLM52_DENSE_LAYERS..=GLM52_MTP_LAYER`（含端点 78），非专家侧经 [L396-L399](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/weights.rs#L396-L399) 显式带上 MTP 名单——[L324-L326](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/weights.rs#L324-L326) 的文档注释点明「这是 manifest 不变量，resident 计划故意省略服务不消费的张量」。

**resident 侧的条件装载**——[src/weights.rs:L342-L375](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/weights.rs#L342-L375)：EP 分支里主干专家循环是 `..GLM52_LAYERS`（**不含** 78），`native_mtp` 为真才追加 MTP 名单与第 78 层的本 rank 专家区间（[L365-L368](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/weights.rs#L365-L368)）；TP4 分支只追加 `include_shared=false` 的 MTP 名单（[L370-L372](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/weights.rs#L370-L372)）。

**单测三连**——EP8：[src/weights.rs:L781-L820](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/weights.rs#L781-L820) 断言 rank 3 开 MTP 后第 78 层专家恰为 96..128、`eh_proj` 在列、plain 计划零 78 层张量；EP4：[L822-L861](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/weights.rs#L822-L861) 同构断言 192..256；TP4：[L863-L882](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/weights.rs#L863-L882) 断言 bookend 在列而 `mlp.experts.*` 全空。

#### 4.4.4 代码实践

1. **实践目标**：解释单测里 rank 3 的专家区间为何是 96..128，并推演 EP64 + native MTP 时 rank 17 的第 78 层专家区间。
2. **操作步骤**：
   - 代入区间公式 \( [r \cdot L, (r+1) \cdot L) \)：EP8 的 \(L=32\)、\(r=3\)；EP64 的 \(L=4\)、\(r=17\)。
   - 打开 [src/weights.rs:L781-L820](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/weights.rs#L781-L820) 核对断言里的两个边界专家名（96 与 127）。
   - 回答：EP64 + MTP 下，第 78 层的 `experts.70.gate_proj.weight` 会进哪个 rank 的计划？（提示：\(70 \div 4\)）
3. **需要观察的现象**：EP8 rank 3 → \(3×32=96\) 起、128 止；EP64 rank 17 → \(17×4=68\) 起、72 止；专家 70 落在 rank 17（\(68 \le 70 < 72\)）。
4. **预期结果**：与单测断言及区间公式零偏差。全程纸面推导，无需运行环境。

#### 4.4.5 小练习与答案

**练习 1**：不开 native MTP 时，第 78 层的张量既不在任何 resident 计划里，为何覆盖校验仍然放行？
答案：覆盖校验用的是 `rank_tensor_names`（含 78 层的独立清单），不是 resident 计划——两套清单本来就是分层的：前者要求生成集 == checkpoint 键集（78 层在场），后者按需省略。放行与省略并不矛盾。

**练习 2**：`eh_proj` 的形状为什么是 `[6144, 12288]`？
答案：MTP 的输入是「final-norm 前的 hidden 与 token embedding 的拼接」，宽 \(2H = 12288\)，投影回 \(H = 6144\)（行主序 out×in）。契约见 [L519-L521](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/weights.rs#L519-L521)，数学细节留给 u9-l3。

**练习 3**：`parse_layer_index` 为什么允许层号到 78 而不是 77？
答案：[L506-L510](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/weights.rs#L506-L510) 的上界是 `GLM52_MTP_LAYER`——78 是 checkpoint 的合法层号（MTP 层），超过它（比如 79）才是「契约外」的非法张量。

## 5. 综合实践

**任务：为一次「EP8 + native MTP」启动编制完整权重装载账本，并静态验证。**

1. **张量数账本**：在 4.2 表格基础上追加 MTP 增量——第 78 层非专家（4 个 MTP 专属 + 14 注意力 + 2 路由 + 6 共享专家 = 26，indexer 视 checkpoint 是否给 78 层配 k_norm 而定，配了再 +7）+ 本 rank 32 只 × 6 专家张量——得出带 MTP 的 `tensor_count`（无 indexer 附加时 = 16260 + 26 + 192 = 16478），写出推导过程。
2. **字节账本**：用 4.3 的步长常量算出：专家区四类各自的每层容量、专家板总字节（75+1 层）、并用契约抽查三个非专家张量（`lm_head.weight`、`layers.0.mlp.gate_proj.weight_scale_inv`、`layers.78.eh_proj.weight`）的字节数。
3. **静态验证**：把账本与三处源码事实对账——[src/weights.rs:L781-L820](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/weights.rs#L781-L820)（rank 3 的 96..128）、[src/weights/load.rs:L295-L320](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/weights/load.rs#L295-L320)（三条终检各对上账本的哪一列）、[src/lib.rs:L1677-L1687](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/lib.rs#L1677-L1687)（启动日志会打印账本里的哪两个字段）。
4. **可选实机验证（待本地验证，需 GPU + checkpoint + CUDA 工具链）**：`cargo run --release --features glm52 -- --model-path models/GLM5.2 --moe-topo ep8 --glm52-native-mtp` 启动，在日志里找 `rank_expert_ranges` / `rank_plan_tensors` 与装载画像 `weight load profile` 行，与自己账本逐项比对；纯 CPU 环境可退而运行 `cargo test --release -p pegainfer-glm52 --features glm52 --lib weights::tests` 跑通本讲引用的全部纯逻辑单测。
5. 产出：一页账本表 + 三行「账本列 ↔ 源码事实」对照。这张账本是 u2-l3（mmap/staging 如何把字节搬上去）与 u2-l4（这些字节如何挤进显存预算）的直接输入。

## 6. 本讲小结

- `Glm52WeightManifest` 只是一张「张量名 → 分片文件」的 BTreeMap（[src/weights.rs:L234-L259](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/weights.rs#L234-L259)）；每个名字的 dtype/shape/字节数由 `expected_tensor_contract` 从 u2-l1 几何常量**按名推导**，装载前三关核对。
- 分配原则：非专家栈全量复制、路由专家按 \([rL,(r{+}1)L)\) 区间切分（\(L=256/\text{EP}\)）、TP4 首趟不带专家包（空区间，走 `shard_for` 驱动的切片第二趟）。
- 覆盖校验是双向差集的精确相等（[src/weights.rs:L437-L462](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/weights.rs#L437-L462)）：防「checkpoint 有张量没人要」，也防「计划要的名字不存在」；它用固定 EP8 分区，与启动拓扑解耦。
- expert-major 打包必须在 H2D 时落位：85 GiB 专家板 + 等大重排副本放不进 141 GiB HBM（[L49-L55](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/weights.rs#L49-L55)）；五个步长常量把落位变成纯算术，`region_written` 对账杜绝「洞里藏陈旧字节」。
- 第 78 层（MTP）在覆盖校验里永远在场，在 resident 计划里仅随 `--glm52-native-mtp` 进入，且 EP 与 TP4 形态拿到的子集不同——「checkpoint 可解释」与「按需装载」是两套清单。

## 7. 下一步学习建议

下一讲 **u2-l3（rank 权重加载：mmap、staging 与 GPU 驻留）**深入本讲 bundle 的字节搬运侧：`load.rs` 的分片 mmap 生命周期、pageable 与 pinned 双缓冲两种上传器的取舍、装载报告与空闲 VRAM 探测。阅读时带着本讲的三个问题去：(1) `planned_total_bytes` 账本如何在同步点被消费；(2) mmap 为什么要分组保活（`MMAP_EVENT_GROUP` / `LIVE_MMAP_GROUPS`）；(3) `Glm52RankGpuWeights::ensure_consumed` 如何保证「计划全消费」。之后 u2-l4 会把这些字节计入显存预算，推导 `max_model_len` 与 KV 池大小。若想先看权重的消费现场，可跳读 `src/model/build.rs` 里 `take_tensor` / `take_expert_layer` 的调用——那是 u3 算子层的入口。
