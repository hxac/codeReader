# 讲义 u2-l2:张量并行几何:TensorParallelConfig 与 LocalGeometry

## 1. 本讲目标

上一讲(u2-l1)我们看到 `Config35` 是一个**对张量并行一无所知**的类型——它只描述"这个模型长什么样",不描述"这张卡分到哪一块"。本讲补上另一半:当多张 GPU 一起服务同一个 Qwen3.5 模型时,每个 rank 如何得到自己那份合法的维度。

学完本讲,你应该能够:

1. 解释「不可表示非法状态」(make invalid states unrepresentable)这种构造期校验风格在 `src/config/tp.rs` 中的落地方式:为什么 `TensorParallelConfig` 的字段是私有的、为什么非法的 `(rank, world_size)` 根本构造不出来。
2. 亲手推导 TP 切分公式:给定全局的 `num_attention_heads`、`num_key_value_heads`、`intermediate_size`、`linear_num_key_heads` 等字段和 `world_size`,算出 `LocalGeometry` 的全部 `local_*` 字段。
3. 说出哪四个字段的整除性失败会触发 `ConfigError::TpIndivisible`,以及为什么 `linear_num_value_heads` 不需要单独检查。
4. 说明「P2c 门」是什么:CUDA Graph 与编译 GQA 组合的兼容性在哪一层校验、为什么它在执行器启动层而不是 `tp.rs` 里、以及未编译组走哪条回退路径。

## 2. 前置知识

### 2.1 张量并行(Tensor Parallelism,TP)是什么

一个 Transformer 的权重是大矩阵。单卡放不下、或想加快前向时,可以把矩阵切开分给多张卡,每张卡算自己那一块,再用集合通信(all-reduce / all-gather)把结果拼回来。这就是 Megatron-LM 风格的张量并行:

- **行切分(row shard)**:把权重矩阵的输出维度切开,每卡持有连续的若干行。本讲后面看到的 `q_proj`、`in_proj` 等输出维度的切分就是行切分。
- **列切分(column shard)**:把输入维度切开。例如 `o_proj` 的输入是注意力头拼接后的维度,头被切走了,输入列也要跟着切。

TP 有几个关键名词:

- **rank**:本进程在 TP 组里的编号,从 0 开始。
- **world_size**:TP 组的卡数,记作 \( W \)。
- **shard(分片)**:某个全局维度中本 rank 持有的 `(offset, len)` 切片。

### 2.2 GQA 与「组比率」

Qwen3.5 的全注意力层用分组查询注意力(GQA):`num_key_value_heads` 个 KV 头服务 `num_attention_heads` 个 Q 头,平均每个 KV 头带

\[ g = \frac{\text{num\_attention\_heads}}{\text{num\_key\_value\_heads}} \]

个 Q 头,称为 **GQA 组比率**。批量解码内核是按组比率编译的——只有一部分组比率有现成内核(本讲 4.3 会看到具体清单)。记住一个本讲最重要的观察:**头切分不改变组比率**,因为分子分母被同一个 \( W \) 除。

### 2.3 「不可表示非法状态」

这是贯穿 PegaInfer 配置层的校验哲学(上一讲已见):与其在调用点到处 `if world_size == 0 { return Err }`,不如让**类型本身无法装下非法值**——字段私有、只能通过一个做完全部检查的构造函数获得实例。于是下游代码拿到值就一定是合法的,校验逻辑只存在一份。本讲的 `TensorParallelConfig` 是这个风格最纯粹的示范。

### 2.4 混合架构提醒

Qwen3.5 每层要么是 full attention(走分页 KV),要么是 linear attention(Gated DeltaNet,走固定大小递推状态)。TP 切分对两种层都要成立:全注意力层切 Q/KV 头,线性注意力层切 key/value 头——这就是为什么 `LocalGeometry` 里同时有 `local_full_attn_*` 和 `local_linear_*` 两族字段。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲视角 |
|---|---|---|
| [src/config/tp.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/config/tp.rs) | `TensorParallelConfig` 与 `LocalGeometry` 的定义、校验与派生 | 本讲主角,全文精读 |
| [src/config/model.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/config/model.rs) | TP 无关的模型配置 `Config35` | 提供 `decode_group_is_compiled`(P2c 门)与头整除不变量 |
| [src/config/error.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/config/error.rs) | 类型化错误枚举 `ConfigError` | 三个 TP 变体:`TpZeroWorldSize`、`TpRankOutOfRange`、`TpIndivisible` |
| [src/config/mod.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/config/mod.rs) | config 子模块的所有权模型声明 | 说明 `model` 与 `tp` 的方向性边界 |
| [src/weights.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/weights.rs) | 模型加载 | `LocalGeometry::try_new` 的生产调用点;`ModelRuntimeConfig` 携带 TP 指派 |
| [src/weights/layers.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/weights/layers.rs) | 权重切片结构 `Shard` / `WeightSource` | 展示 `shard_range` 公式的实际消费方式 |
| [src/tp_executor.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/tp_executor.rs) | TP 执行器 | P2c 门在启动层的落地(第 7 单元展开) |
| [src/batch_decode.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/batch_decode.rs) | 批量解码 | 运行时按 `decode_group_is_compiled` 选择内核路径 |
| [pegainfer-kernels/src/ops/attention.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-kernels/src/ops/attention.rs) | 内核算子面 | `SUPPORTED_GQA_GROUP_SIZES` 常量(P2c 门的依据) |

## 4. 核心概念与源码讲解

本讲按规格拆成三个最小模块:**类型化校验**(4.1)、**shard 切分公式**(4.2)、**kernel 兼容门**(4.3)。

### 4.1 类型化校验:让非法 (rank, world_size) 不可构造

#### 4.1.1 概念说明

`TensorParallelConfig` 回答一个极小的问题:"本进程是 TP 组里的几号、组里一共几张卡?" 只有 `rank` 和 `world_size` 两个字段。

它的设计要点在于:这两个字段**是私有的**。外部没有任何办法直接 `TensorParallelConfig { rank: 7, world_size: 2 }` 这样拼出一个荒谬的指派(rank 7 不存在于 2 卡组)。获得实例的途径只有两条:

1. `TryFrom<(usize, usize)>`——做完全部检查的构造边界;
2. `Default`——单卡(rank 0, world 1)。

于是 `world_size == 0`、`rank >= world_size` 这两类非法指派**在类型层面不可表示**,任何下游代码拿到 `TensorParallelConfig` 就不必再防御性地检查它们。对比一下反面做法:如果全代码库传的是裸 `(usize, usize)`,每个消费点都得自己判一次合法——漏一处就是运行期越界。

#### 4.1.2 核心流程

`TryFrom<(rank, world_size)>` 的校验是两步有序检查、首错即返:

```text
输入 (rank, world_size)
  ├─ world_size == 0 ?           → Err(TpZeroWorldSize)
  ├─ rank >= world_size ?        → Err(TpRankOutOfRange { rank, world_size })
  └─ 否则                        → Ok(TensorParallelConfig { rank, world_size })
```

构造成功后,这个类型对外只暴露四个只读方法:

| 方法 | 含义 |
|---|---|
| `rank()` | 本进程编号 |
| `world_size()` | 组内卡数 \( W \) |
| `is_sharded()` | `world_size > 1`,即"真的在多卡切分吗" |
| `shard_range(total)` | 把全局行数 `total` 均分,返回本 rank 的 `(offset, len)` |

`shard_range` 的公式是朴素的整数除法:

\[ \text{shard\_len} = \left\lfloor \frac{\text{total}}{W} \right\rfloor, \qquad \text{offset} = \text{rank} \times \text{shard\_len} \]

注意这里**不做**余数处理——它只在已被证明整除的维度上被调用(4.2 会看到那道整除门)。这是"每条规则只在一处强制"的又一体现:整除性由 `LocalGeometry::try_new` 负责,`shard_range` 只管算术。

#### 4.1.3 源码精读

先看结构体定义与私有字段——[src/config/tp.rs:L16-L25](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/config/tp.rs#L16-L25) 定义了 `TensorParallelConfig`,`rank` / `world_size` 两个字段没有任何 `pub` 修饰,文档注释明确说明"字段私有,因此非法状态(`world_size == 0`、`rank >= world_size`)无法构造,唯一获得实例的途径是 `try_from` 或 `Default`"。

单卡默认值——[src/config/tp.rs:L27-L34](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/config/tp.rs#L27-L34) 把 `Default` 实现为 rank 0、world 1。这一点很关键:**"单 GPU" 不是一条特殊路径,而是 TP 的 `W=1` 特例**,所以后续加载代码里单卡与 TP1 走的是同一套几何。

构造边界与两步检查——[src/config/tp.rs:L56-L69](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/config/tp.rs#L56-L69) 是 `TryFrom<(usize, usize)>` 的全部实现:先拒 `world_size == 0`,再拒 `rank >= world_size`,两条都过了才构造。注意这个 `impl` 的文档注释写着"独立于任何模型几何校验一个指派"——它不知道也不需要知道模型长什么样。

均分工具——[src/config/tp.rs:L45-L53](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/config/tp.rs#L45-L53) 中 `is_sharded` 是 `world_size > 1` 的谓词,`shard_range` 用整数除法算出 `(rank * shard_len, shard_len)`。

配套的类型化错误——[src/config/error.rs:L66-L77](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/config/error.rs#L66-L77) 定义了三个 TP 变体:`TpZeroWorldSize`、`TpRankOutOfRange { rank, world_size }` 和 `TpIndivisible { field, value, world_size }`。`field` 是 `&'static str`(字段名字符串),这让错误消息能精确说出"哪个字段、什么值、除以多大的世界"失败了。整个枚举 `derive(PartialEq)`,所以测试可以直接 `assert_eq!` 对比错误(下面马上看到)。

#### 4.1.4 代码实践:纸面穷举构造边界

**实践目标**:不看运行结果,精确预测 `TensorParallelConfig::try_from` 对一组输入的输出,再对照 crate 内嵌测试验证。

**操作步骤**(源码阅读型实践,无需 GPU):

1. 打开 [src/config/tp.rs:L287-L300](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/config/tp.rs#L287-L300),这是 crate 自带的测试 `rejects_zero_world_size_and_rank_out_of_range`。
2. 自己先填一张表(示例代码,答案见下方测试断言):

   | 输入 `(rank, world_size)` | 你的预测 |
   |---|---|
   | `(0, 0)` | ? |
   | `(2, 2)` | ? |
   | `(1, 2)` | ? |
   | `(0, 1)` | ? |

3. 对照测试断言:`(0, 0)` 期望 `TpZeroWorldSize`,`(2, 2)` 期望 `TpRankOutOfRange`,而 `(1, 2)` / `(0, 1)` 在同文件 [L264-L270](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/config/tp.rs#L264-L270) 的 `default_tensor_parallel_is_tp1` 测试里等价于合法指派(Default 即 `(0, 1)`,`shard_range(4096)` 得 `(0, 4096)`——独享全部)。
4. (可选,需完整 qwen35 工具链)运行 `cargo test --release -p pegainfer-qwen35 --features qwen35 --lib config::tp` 只跑本模块测试。**待本地验证**:该命令需要 Python + Triton 的 AOT 构建链,构建耗时可能很长。

**需要观察的现象**:四个输入里恰好两个 Err、两个 Ok;Err 的变体与你的预测逐项一致。

**预期结果**:构造边界恰好拦住 `world_size == 0` 与 `rank >= world_size` 两类非法指派,`Default` 与合法二元组都能通过。

#### 4.1.5 小练习与答案

**练习 1**:如果去掉字段私有的设计、允许外部直接构造,代码库里至少要增加多少处防御性检查?

**答案**:每一处消费 `rank` / `world_size` 的代码——`shard_range` 的调用点(layers.rs 里的多处切片)、tp_executor 的 per-rank 构造循环、日志打印——都需要各自检查。私有化 + 单一构造边界把这些检查收敛为 `try_from` 里的两条 `if`。

**练习 2**:`shard_range` 为什么不做余数分配(比如让前几个 rank 多拿一行)?

**答案**:它的调用方只对**已被 `LocalGeometry::try_new` 证明整除**的维度调用(`shard_len` 恰好均分);把整除性检查与切分算术分离,让每条规则只在一处强制。若未来出现不整除的用途,那是新代码该自带的新规则,而不是让这个共享工具默默兼容。

**练习 3**:TP 执行器里 `TensorParallelConfig::try_from((rank, world_size))?` 在哪被调用、`rank` 从哪来?

**答案**:在 [src/tp_executor.rs:L700-L713](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/tp_executor.rs#L700-L713):执行器按 `device_ordinals` 逐卡构造模型,`rank` 是设备列表的枚举下标,`world_size` 是设备数。即使来源看似可靠(自己 `enumerate` 出来的),构造边界仍然走一遍——这就是"不可表示非法状态"的纪律。

### 4.2 shard 切分公式:LocalGeometry 从已验证的 Config35 派生本 rank 维度

#### 4.2.1 概念说明

`LocalGeometry` 回答的问题是:"在 `W` 卡 TP 下,**本 rank** 的全注意力头数、KV 头数、MLP 中间宽度、线性注意力头数……各是多少?" 它是 `Qwen35Model` 加载一切缓冲、切一切权重所依据的几何底座。

它的输入是两个**已经各自合法**的类型:`&Config35`(模型几何,上一讲)和 `TensorParallelConfig`(TP 指派,本讲 4.1)。它的工作分两段:

1. **兼容性校验**:不是所有合法模型都能配所有 `world_size`——切不开就得拒。
2. **维度派生**:对切得开的模型,算出本 rank 的全部 `local_*` 字段。

方向性边界在模块文档里写得非常清楚:[src/config/tp.rs:L1-L11](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/config/tp.rs#L1-L11) 说明本模块**依赖** `model`(读已验证的 `Config35` 来派生分片维度),而 `model` 对 TP 一无所知、**从不反向依赖**本模块;下游代码接受这里产出的 `LocalGeometry` 类型,而不是拿着裸 `(rank, world_size)` 自己重算分片。`src/config/mod.rs` 的模块文档([L1-L9](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/config/mod.rs#L1-L9))把这套所有权模型总结为三条边界。

一句话:**`Config35` 描述"模型是什么",`TensorParallelConfig` 描述"我是谁",`LocalGeometry` 描述"我分到什么"。**

#### 4.2.2 核心流程

`LocalGeometry::try_new(config, tp)` 先做**四条有序整除检查**(首错即返,错误都是 `TpIndivisible`):

```text
num_attention_heads  % W != 0  → TpIndivisible { field: "num_attention_heads", ... }
num_key_value_heads  % W != 0  → TpIndivisible { field: "num_key_value_heads", ... }
intermediate_size    % W != 0  → TpIndivisible { field: "intermediate_size", ... }
linear_num_key_heads % W != 0  → TpIndivisible { field: "linear_num_key_heads", ... }
```

为什么 `linear_num_value_heads` 不在清单里?靠数学传递:`Config35` 层已强制 value 头数是 key 头数的整数倍(value = key × k,见 4.2.3 引用的 `LinearHeadDivisibility` 检查),而这里又强制 key 头数是 \( W \) 的倍数(key = \( W \) × m),于是 value = \( W \) × m × k,必然被 \( W \) 整除。源码注释也点明了这一点:"the value-head count follows from the `Config35` key/value-head invariant"。

四条检查通过后,派生公式如下(记 \( W \) = world_size,`hd` = head_dim = 256,`khd` = linear_key_head_dim = 128,`vhd` = linear_value_head_dim = 128——三个 head dim 都是 AOT 内核烘焙的常量,上一讲已讲):

| 字段 | 公式 |
|---|---|
| `local_num_attention_heads` | \( \text{num\_attention\_heads} / W \) |
| `local_num_key_value_heads` | \( \text{num\_key\_value\_heads} / W \) |
| `local_intermediate_size` | \( \text{intermediate\_size} / W \) |
| `local_full_attn_q_dim` | \( \text{local\_num\_attention\_heads} \times \text{hd} \) |
| `local_full_attn_kv_dim` | \( \text{local\_num\_key\_value\_heads} \times \text{hd} \) |
| `local_full_attn_gated_q_dim` | \( 2 \times \text{local\_full\_attn\_q\_dim} \)(Q 与 gate 拼在一起) |
| `local_linear_num_key_heads` | \( \text{linear\_num\_key\_heads} / W \) |
| `local_linear_num_value_heads` | \( \text{linear\_num\_value\_heads} / W \) |
| `local_linear_q_dim`(内部量) | \( \text{local\_linear\_num\_key\_heads} \times \text{khd} \) |
| `local_linear_v_dim` | \( \text{local\_linear\_num\_value\_heads} \times \text{vhd} \) |
| `local_linear_qkv_dim` | \( 2 \times \text{local\_linear\_q\_dim} + \text{local\_linear\_v\_dim} \)(k 行镜像 q 行) |
| `local_linear_z_dim` | \( = \text{local\_linear\_v\_dim} \)(z 投影输出宽等于 v 维) |

注意一个容易忽略的事实:**`hidden_size` 从不切分**。残差流宽度在所有 rank 上完整保留,被切的只是各投影的输出维与 MLP 中间维。`local_full_attn_q_dim` 等字段也不是逐条独立检查的产物,而是派生——检查只针对"切得开"的头数/宽度,维度跟着头数走。

消费侧:`LocalGeometry` 是 `Clone + Copy` 的小结构,加载时存进 `Qwen35Model.geometry` 字段([src/weights.rs:L57-L87](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/weights.rs#L57-L87)),权重切片结构 `Shard` / `WeightSource` 全部从它取范围——例如 [src/weights/layers.rs:L191-L211](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/weights/layers.rs#L191-L211) 的 `Shard::flat(global, geometry)` 就是 `shard_range` 的一层薄封装,把 `(offset, len)` 与切分前的全局宽度配对(防呆:range 与 global 必须成对出现,防止把 A 维度的范围配到 B 维度的全局值上)。

#### 4.2.3 源码精读

结构体定义——[src/config/tp.rs:L71-L90](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/config/tp.rs#L71-L90) 定义 `LocalGeometry`:它内嵌一个 `tp: TensorParallelConfig` 并携带十个 `local_*` 字段。文档注释强调它"从已验证的模型配置 + TP 指派 + cuda-graph 模式构造一次",下游用**这个类型**来定缓冲大小、算分片,**绝不**用裸的 rank/world-size 对。

四条整除门——[src/config/tp.rs:L109-L138](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/config/tp.rs#L109-L138) 依次检查 `num_attention_heads`、`num_key_value_heads`、`intermediate_size`、`linear_num_key_heads`。第四条上方有一行值得咀嚼的注释:**"Fail closed on an indivisible key-head count rather than falling back to replication"**——宁可拒绝启动,也不悄悄把某个维度复制到每张卡上。复制看似"能用",但会让各 rank 显存与算力不均衡、语义偏离 Megatron 切分约定,后面所有基于"均分"假设的代码(比如 `shard_range` 的整除前提)全部失效。

派生公式本体——[src/config/tp.rs:L140-L166](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/config/tp.rs#L140-L166):先算四个"头数/宽度"的 local 值,再组合出 q/kv/gated/qkv 维度,最后 `Ok(Self { .. })` 一次性构造。注意 `local_full_attn_gated_q_dim` 直接写为 `local_full_attn_q_dim * 2`、`local_linear_qkv_dim` 写为 `local_linear_q_dim * 2 + local_linear_v_dim`,注释说明"k 行镜像 q 行"——融合 qkv 投影里 q 与 k 两段行数相同。

TP1 契约与只读访问器——[src/config/tp.rs:L168-L229](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/config/tp.rs#L168-L229):前半是一批转发到内嵌 `tp` 的方法(`rank` / `world_size` / `is_sharded` / `shard_range`),后半是各 `local_*` 的 getter。中间 [L209-L211](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/config/tp.rs#L209-L211) 有一条"TP1 契约"注释:**world_size 为 1 时所有 local 维度等于全局维度**,所以单卡路径下一切内核/缓冲/递推状态保持 TP 之前的形状——这就是"单 GPU = TP 的 W=1 特例"在数据上的保证。

上游整除不变量(为什么 value 头数免检)——[src/config/model.rs:L254-L271](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/config/model.rs#L254-L271) 是 `Config35` 构造时的两条头整除检查:`linear_num_value_heads` 必须是 `linear_num_key_heads` 的倍数(`LinearHeadDivisibility`),`num_attention_heads` 必须是 `num_key_value_heads` 的倍数(`AttentionHeadDivisibility`)。`tp.rs` 的整除门建立在这两条之上。

生产调用点——[src/weights.rs:L141-L164](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/weights.rs#L141-L164):`from_safetensors_with_runtime_and_capacity` 先 `Config35::from_file`,然后 `runtime.tensor_parallel.unwrap_or_default()`——**单 GPU 加载时没有 TP 指派,`None` 折叠成 `Default`(TP1)**,接着 `LocalGeometry::try_new(&config, tensor_parallel)` 完成"模型 × 指派"的兼容校验。一切发生在 mmap 权重、分配 CUDA 缓冲**之前**——一个切不开的组合在花费任何加载成本之前就被拒绝。TP 指派如何进入 `ModelRuntimeConfig` 见 [src/weights.rs:L36-L43](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/weights.rs#L36-L43)(字段 `tensor_parallel: Option<TensorParallelConfig>`)。

`shard_range` 的三段偏移消费——[src/weights/layers.rs:L393-L406](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/weights/layers.rs#L393-L406):融合线性注意力 qkv 投影的切分不是简单一段,而是对 q、k、v **三个全局段**分别取 `shard_range`,再叠上段基址偏移(`global_q + k_rel`、`global_q + global_k + v_rel`)。这是 `shard_range` 最有代表性的消费方式:每段的 `shard_len` 相同(都除以同一个 \( W \)),但 offset 要加上段起点。

`is_sharded` 的一个实际用途——[src/weights.rs:L188](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/weights.rs#L188):`(geometry.world_size() == 1).then(|| WeightPrefetch::spawn(...))`——只有单卡才预取权重分片;TP 路径每个 rank 各自 mmap,不启用这个预取。

#### 4.2.4 代码实践:手算一套假想配置的 LocalGeometry(本讲主实践)

**实践目标**:不运行代码,纸面推导一个假想配置在 tp=2 下的全部 `local_*` 字段,并找出哪个字段整除失败会触发 `TpIndivisible`。

**操作步骤**:

1. 取假想配置(满足 `Config35` 全部模型级校验):

   ```text
   num_attention_heads   = 32
   num_key_value_heads   = 8
   intermediate_size     = 9216
   linear_num_key_heads  = 16      (value 32 是 key 16 的倍数 ✓)
   linear_num_value_heads= 32
   head_dim = 256, linear_key_head_dim = 128, linear_value_head_dim = 128
   ```

2. 令 \( W = 2 \),rank ∈ {0, 1},按 4.2.2 的公式表逐行计算,填出下表(参考答案见"预期结果"):

   | 字段 | 计算 | 结果 |
   |---|---|---|
   | `local_num_attention_heads` | 32 / 2 | ? |
   | `local_num_key_value_heads` | 8 / 2 | ? |
   | `local_intermediate_size` | 9216 / 2 | ? |
   | `local_full_attn_q_dim` | ? | ? |
   | `local_full_attn_kv_dim` | ? | ? |
   | `local_full_attn_gated_q_dim` | ? | ? |
   | `local_linear_num_key_heads` | 16 / 2 | ? |
   | `local_linear_num_value_heads` | 32 / 2 | ? |
   | `local_linear_q_dim` | ? × 128 | ? |
   | `local_linear_v_dim` | ? × 128 | ? |
   | `local_linear_qkv_dim` | 2×q + v | ? |
   | `local_linear_z_dim` | = v 维 | ? |

3. 回答两个问题:
   - a) 若把 `world_size` 换成 3,第一个触发 `TpIndivisible` 的字段是哪个?错误变体长什么样?
   - b) 构造一个配置,使 `linear_num_value_heads`(32)能被 \( W=2 \) 整除、但 TP 校验仍然失败。
4. 自我校对:crate 自带测试 [src/config/tp.rs:L272-L285](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/config/tp.rs#L272-L285)(`tp2_local_geometry_matches_dense_dims`)用真实 Qwen3.5-4B 形状的夹具(16 q 头 / 4 kv 头 / 9216 中间宽)在 tp2 下断言 `local_num_attention_heads=8`、`local_num_key_value_heads=2`、`local_intermediate_size=4608`、`local_full_attn_q_dim=2048`、`local_full_attn_kv_dim=512`、`local_full_attn_gated_q_dim=4096`——用它核对你用的公式是否与源码一致。

**需要观察的现象**:公式表逐行可复算;四条整除门覆盖且仅覆盖"头数/中间宽度"四个字段,维度字段全部是派生量。

**预期结果**(参考答案):

| 字段 | 结果 |
|---|---|
| `local_num_attention_heads` | 16 |
| `local_num_key_value_heads` | 4 |
| `local_intermediate_size` | 4608 |
| `local_full_attn_q_dim` | 16 × 256 = 4096 |
| `local_full_attn_kv_dim` | 4 × 256 = 1024 |
| `local_full_attn_gated_q_dim` | 4096 × 2 = 8192 |
| `local_linear_num_key_heads` | 8 |
| `local_linear_num_value_heads` | 16 |
| `local_linear_q_dim` | 8 × 128 = 1024 |
| `local_linear_v_dim` | 16 × 128 = 2048 |
| `local_linear_qkv_dim` | 2×1024 + 2048 = 4096 |
| `local_linear_z_dim` | 2048 |

a) `W=3`:第一条检查 `32 % 3 != 0` 即失败,`Err(TpIndivisible { field: "num_attention_heads", value: 32, world_size: 3 })`——四条检查按源码顺序,头一条就是它。
b) 取 `linear_num_key_heads = 1`(`linear_num_value_heads = 32` 仍是它的倍数,模型级校验通过):`1 % 2 != 0`,TP 校验在第四条门上失败,`Err(TpIndivisible { field: "linear_num_key_heads", value: 1, world_size: 2 })`。这正是 crate 测试 [L342-L359](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/config/tp.rs#L342-L359)(`requires_linear_attention_key_head_divisibility`)守护的场景(该测试用 17/34 组合破坏整除并保持 value%key 不变量成立,原理相同)。

#### 4.2.5 小练习与答案

**练习 1**:GQA 组比率 \( g = \) num_attention_heads / num_key_value_heads 在 TP 切分后是多少?

**答案**:不变。两个数同除 \( W \),比率保持 \( g = \frac{32/2}{8/2} = 4 \)。这就是 4.3 中"组比率是 TP 不变量"的来源——每个 rank 对"本 rank 的内核路径"做出**完全相同**的决定。

**练习 2**:为什么 `hidden_size` 不出现在整除门和 `local_*` 字段里?

**答案**:残差流(hidden)不切分,每个 rank 持有完整的 `hidden_size`;被切的是各投影的输出维(按头)与 MLP 中间维。因此不存在"hidden 切不开"的问题,也就不需要 `local_hidden_size`。

**练习 3**:`shard_range(1024)` 在 rank 1、\( W=2 \) 下返回什么?若有人对 `total = 1000` 调用它会发生什么?

**答案**:返回 `(512, 512)`。对 1000 调用会得到 `(500, 500)`——两卡各 500,还有 500 行没人管。源码不阻止这种调用,它的契约是**调用方只对已证整除的维度调用**;这正是把整除门集中在 `LocalGeometry::try_new` 的前提,也是一个典型的"靠类型纪律而非运行时防御"的取舍。

### 4.3 kernel 兼容门:P2c——CUDA Graph 与编译 GQA 组合

#### 4.3.1 概念说明

批量解码(full attention 部分)有一个按 **GQA 组比率**编译的内核族。它不是对所有组比率都有现成版本——[pegainfer-kernels/src/ops/attention.rs:L21](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-kernels/src/ops/attention.rs#L21) 列出支持清单:

```rust
pub const SUPPORTED_GQA_GROUP_SIZES: &[usize] = &[1, 2, 3, 4, 6, 8];
```

CUDA Graph(第 4 单元会细讲)要求把整步解码内核**捕获**进图里重放。如果当前组比率没有编译版批量解码内核,就得改走"批量混合 eager 回退"路径——用 paged-prefill 内核按单 token 步进计划跑注意力。这条回退路径**不能被图捕获**。于是产生一个兼容性问题:

> 用户请求了 CUDA Graph + 当前 GQA 组没有编译内核,怎么办?

答案分两层,这就是源码注释里称为 **P2c** 的门(注释中的编号,指该兼容性条目):

1. **谓词层**(`Config35::decode_group_is_compiled`):组比率 ∈ `SUPPORTED_GQA_GROUP_SIZES`?这是**配置层**的纯函数,不含 TP。
2. **决策层**(执行器启动):`enable_cuda_graph && decode_group_is_compiled()` 才启用图;否则**降级到 eager 路径并打日志**,而不是报错拒绝启动。

关键在于:这个门**不在 `tp.rs` 里**。`LocalGeometry::try_new` 的文档注释([src/config/tp.rs:L93-L108](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/config/tp.rs#L93-L108))明确写着:"CUDA Graph under TP is gated at executor startup on `Config35::decode_group_is_compiled` (P2c): uncompiled GQA groups keep the batched eager path instead of failing validation here."——几何校验管几何,内核路由门放在启动层,职责不混。

而这一切在 TP 下不产生"各 rank 裁决不一致"的风险,原因是 4.2 练习 1 的结论:**组比率 TP 不变**。

#### 4.3.2 核心流程

```text
启动(enable_cuda_graph, W 卡)
  ├─ 每个 rank: LocalGeometry::try_new 通过(几何已验证)
  ├─ 谓词: decode_group_is_compiled()
  │     = SUPPORTED_GQA_GROUP_SIZES.contains(num_attention_heads / num_key_value_heads)
  │     (组比率 TP 不变 ⇒ 所有 rank 得到同一答案)
  ├─ graph_enabled = enable_cuda_graph && compiled
  ├─ 未编译: 打一次 info 日志(Once 保证),decode 留在批量 eager 路径
  └─ 运行期 batch_decode:
        compiled  → 正常批量解码内核(可入图)
        未编译   → paged-prefill 内核 + 每 token 步进计划(eager),
                   且断言 graph_use == Serve(禁止把 eager 路径捕获进图)
```

#### 4.3.3 源码精读

谓词定义——[src/config/model.rs:L146-L152](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/config/model.rs#L146-L152):`decode_group_is_compiled` 一行核心——查 `num_attention_heads / num_key_value_heads` 是否在 `SUPPORTED_GQA_GROUP_SIZES` 里,注释说明"未编译的 GQA 组走批量混合 eager 回退"。注意它读的是**全局**配置字段,而组比率无论全局还是本 rank 都相同。

启动层的门——[src/tp_executor.rs:L714-L733](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/tp_executor.rs#L714-L733):加载完全部 rank 的模型后,取 rank 0 的模型算 `graph_enabled = enable_cuda_graph && first.config().decode_group_is_compiled()`。上方注释就是 P2c 条目的正文:"graph decode under TP requires a compiled batch-decode kernel for the rank-local GQA group. The group ratio is TP-invariant, so every rank decides identically; an uncompiled group keeps the batched eager path byte-for-byte."("byte-for-byte"意为回退路径的数值行为与无图时完全一致。)若请求了图但未启用,用 `std::sync::Once` 只打一次 info 日志,日志里用 `geometry.local_num_attention_heads() / geometry.local_num_key_value_heads()` 报出**本 rank 的**组比率与头数——注意这里正是 `LocalGeometry` 的 `local_*` 字段被消费的实例,且除法结果与全局组比率相同。

运行期的路由——[src/batch_decode.rs:L344-L360](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/batch_decode.rs#L344-L360):每步批量解码时再次按 `self.config.decode_group_is_compiled()` 分路——未编译就构造一个"eager decode"的每 token paged 计划,把全注意力交给 paged-prefill 内核。注释重申:"Head sharding leaves the q-per-kv group size unchanged, so the config-level predicate decides the per-rank route identically on every rank; the reroute adds no collectives."(头切分不改变组比率,配置层谓词让每个 rank 路由一致,改道不增加任何集合通信。)

eager 路径拒绝入图——[src/batch_decode.rs:L428-L443](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/batch_decode.rs#L428-L443):`!decode_group_is_compiled()` 时 `anyhow::ensure!(graph_use == DecodeGraphUse::Serve, ...)`——批量混合 eager 回退只支持惰性的服务态解码,任何试图对它做图捕获/重放的 `graph_use` 都直接报错。这把"eager 路径不可入图"从约定升格为断言。

#### 4.3.4 代码实践:核对一个配置的 P2c 判定

**实践目标**:对若干配置(真实与假想)预测 P2c 门的结果,理解"降级不拒绝"的取舍。

**操作步骤**(纸面推导 + 源码对照):

1. 真实形状:tp.rs 测试夹具的 Qwen3.5-4B 形状是 16 q 头 / 4 kv 头 → 组比率 4 → 查 `SUPPORTED_GQA_GROUP_SIZES = [1,2,3,4,6,8]` → **已编译**。TP2 下本 rank 8 q 头 / 2 kv 头 → 比率仍 4 → 结论不变。
2. 假想形状 A:20 q 头 / 4 kv 头 → 组比率 5 → **不在清单** → 若用户开了 CUDA Graph,启动后看到的是一条 info 日志(不是错误),decode 走 eager 回退。
3. 假想形状 B(4.2.4 的配置):32 / 8 → 比率 4 → 已编译 → TP2 可用图。
4. 阅读上面的三段源码,确认:谓词在 config 层、决策在启动层、路由在每步解码层——三层各用一次同一个纯函数,没有任何一处把组比率写死。

**需要观察的现象**:判定只依赖组比率一个数;同一配置在 W=1 与 W=2 下判定恒同。

**预期结果**:A 类配置(比率 5)在带图请求下仍能启动并服务,只是 decode 永远走 eager;B 类配置可启用图。若运行时有人试图把 eager 路径捕获进图,会在 `batch_decode.rs` 的 `ensure!` 处得到明确错误。

**待本地验证**:如需真实日志输出,可在具备双 GPU 与模型权重的环境运行 TP 服务(第 7 单元的 `tests/serving_tp2.rs` 相关内容);无 GPU 时以源码与上述推理为准。

#### 4.3.5 小练习与答案

**练习 1**:为什么 P2c 门放在执行器启动层,而不是并进 `LocalGeometry::try_new` 的整除检查?

**答案**:两者性质不同。整除性是**几何**不变量——切不开就无法加载,必须 fail-closed 拒绝;组比率 × CUDA Graph 是**运行模式**兼容性——有安全的回退路径(eager),应当降级并告知,而不是拒绝服务。把可回退的检查塞进几何校验会让"能跑但要降级"的配置被误杀。`tp.rs` 的文档注释明确把这一职责让给了启动层。

**练习 2**:TP 下会不会出现 rank 0 走图、rank 1 走 eager 的分裂状态?

**答案**:不会。组比率 TP 不变(分子分母同除 \( W \)),谓词输入在每个 rank 上相同,输出必然一致;`tp_executor.rs` 的注释与 `batch_decode.rs` 的运行期注释都把这列为设计不变量。

**练习 3**:把 `SUPPORTED_GQA_GROUP_SIZES` 从 `[1,2,3,4,6,8]` 扩到含 5,需要动本 crate 的哪些代码?

**答案**:一处都不用动——清单属于 `pegainfer-kernels` 的 attention 模块(内核编译侧),经 `pegainfer_kernels::ops` → `pegainfer_core::ops` 两级重导出后,本 crate 在 `decode_group_is_compiled` 里以全路径 `pegainfer_core::ops::SUPPORTED_GQA_GROUP_SIZES` 引用(`model.rs` 顶部的 `pub(crate) use` 重导出的是 GDN head dim 常量,不是这份清单)。内核侧多编译一个组比率,配置层的谓词自动放行。这体现了"算子面由 kernels 拥有、模型 crate 只消费"的边界。

## 5. 综合实践

把本讲三个模块串成一次完整的"TP 几何审计"。任务:为一个**新规格的 Qwen3.5 检查点**写启动前评审意见。

给定假想 `config.json` 的关键字段:

```json
{
  "num_attention_heads": 40,
  "num_key_value_heads": 8,
  "intermediate_size": 13824,
  "linear_num_key_heads": 8,
  "linear_num_value_heads": 32,
  "head_dim": 256,
  "linear_key_head_dim": 128,
  "linear_value_head_dim": 128
}
```

请完成三步并写出结论:

1. **模型级**(u2-l1 知识):`Config35` 的头整除不变量是否满足?(40 % 8 = 5 ✓;32 % 8 = 4 ✓)
2. **TP 几何级**(本讲 4.2):分别对 \( W = 2 \) 和 \( W = 4 \) 推导全部 `local_*` 字段;判断哪个 \( W \) 能通过四条整除门。参考答案:两个 \( W \) 均可(40/2=20、8/2=4、13824/2=6912、8/2=4;40/4=10、8/4=2、13824/4=3456、8/4=2);\( W=4 \) 时 local kv 头只剩 2,`local_full_attn_kv_dim = 512`。
3. **kernel 门级**(本讲 4.3):组比率 = 40/8 = 5,不在 `[1,2,3,4,6,8]` → 即便几何全部通过,带 `--cuda-graph` 启动也会降级到批量 eager 路径(一条 info 日志),且 eager 路径禁止图捕获。最终评审意见:**该规格在 W=2/4 下可服务,但开不了 CUDA Graph**。

检验方式:把你的推导与 `src/config/tp.rs` 内嵌测试(尤其 [L272-L285](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/config/tp.rs#L272-L285) 与 [L302-L359](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/config/tp.rs#L302-L359))的断言风格对齐;若本地有完整 qwen35 工具链,可仿照测试夹具把上述字段填进 JSON、跑 `cargo test --release -p pegainfer-qwen35 --features qwen35 --lib config::tp` 验证(**待本地验证**)。

## 6. 本讲小结

- **类型化校验**:`TensorParallelConfig` 用私有字段 + `TryFrom` 构造边界让 `world_size == 0`、`rank >= world_size` 不可表示;`Default` 即 TP1,所以"单 GPU"只是 \( W = 1 \) 的特例而非独立路径。
- **shard 切分公式**:`LocalGeometry::try_new` 先过四条有序整除门(`num_attention_heads`、`num_key_value_heads`、`intermediate_size`、`linear_num_key_heads`),失败一律 `TpIndivisible` 且 fail-closed 不复制;然后派生全部 `local_*` 字段,`hidden_size` 从不切分,`shard_range(total) = (rank × total/W, total/W)` 只在已证整除的维度上调用。
- **方向性边界**:`tp` 依赖 `model`、`model` 对 TP 无知;下游接受 `LocalGeometry` 类型而不是裸 `(rank, world_size)`;单卡加载用 `unwrap_or_default()` 折叠成 TP1。
- **kernel 兼容门(P2c)**:`decode_group_is_compiled` 查组比率是否在内核侧的 `SUPPORTED_GQA_GROUP_SIZES = [1,2,3,4,6,8]` 清单里;组比率 TP 不变 ⇒ 各 rank 判定一致;未编译组不拒绝启动,而是降级到批量混合 eager 路径并禁止入图。
- **校验的分层哲学**:几何不变量(切不开)在构造期拒绝,运行模式兼容性(有回退)在启动层降级——两类规则的处置方式刻意不同。

## 7. 下一步学习建议

本讲结束意味着"配置层"完结:你已经掌握 `config.json → Config35 → (rank, world_size) → LocalGeometry` 的完整链条。下一步:

1. **u2-l3(权重加载)**:看 `LocalGeometry` 的第一批真实消费者——`WeightSource` / `Shard` 如何用 `shard_range` 把 safetensors 里的权重按 rank 切上 GPU,以及 `in_proj_qkvz` 的三段偏移拼接(本讲 4.2.3 已埋下伏笔)。
2. **u2-l4(层结构)**:把 `local_full_attn_q_dim`、`local_linear_qkv_dim` 这些字段对到 `FullAttentionLayer` / `LinearAttentionLayer` 的具体权重矩阵上。
3. 想先看 TP 全貌的读者可以跳读 `docs/models/qwen35/tp-design.md`(工作区文档)再回到主线;`tp_executor.rs` 的控制器-worker 协议本身留到第 7 单元。
