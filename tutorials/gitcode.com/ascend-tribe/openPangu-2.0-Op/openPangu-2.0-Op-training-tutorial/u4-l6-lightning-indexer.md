# u4-l6 LightningIndexerEnhance：索引器算子的 Cube+Vector 协同

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清 lightning indexer 在稀疏注意力训练链路中的角色：它是 sparse FlashAttention（u4-l5）的「索引生产者」，负责为每个 query token 挑出 Top-\(k\) 个最重要的 KV 位置。
2. 读懂 `_def.cpp` 的输入输出清单，并掌握「从 OpDef 统计算子接口规模」的方法。
3. 看懂 `proto.cpp` 中 InferShape / InferDataType 如何在没有任何数据的情况下推导输出 shape 与类型。
4. 描述昇腾混合核（AIC:AIV = 1:2）上 Cube 与 Vector 两类核的流水线协同：Cube 做打分矩阵乘，Vector 做加权、规约、排序与 TopK，二者靠跨核事件标志位（CrossCoreSetFlag / CrossCoreWaitFlag）逐基本块握手。
5. 对照 `service_cube.h` 与 `service_vector.h`，把算法公式里的每一步落到具体的设备侧函数。

## 2. 前置知识

- **稀疏注意力的两步走**：u4-l5 讲过，`sparse_flash_attention_enhance` 的 softmax 分母只在「索引选中 ∩ 因果允许」的 token 子集上求和，其 `sparse_indices` 输入（int32，每行存选中的 KV 块编号，-1 尾部填充）就来自本讲的 lightning indexer。两者在训练图中是「生产者 → 消费者」关系。
- **GQA 与 group size \(g\)**：query 有 \(N_1\) 个头，key 只有 \(N_2=1\) 个头（MQA），\(g = N_1 / N_2\)。同一组内 \(g\) 个 query 头共享一个 key 头，indexer 要把这 \(g\) 个头的打分「压」成一份索引。
- **Cube 核与 Vector 核**：昇腾 AI Core 有两类核——Cube 核擅长矩阵乘（Mmad），Vector 核擅长逐元素运算、规约、排序。`KERNEL_TYPE_MIX_AIC_1_2` 表示一个 Cube 核配两个 Vector 核的混排（u4-l3/u4-l4 已见过 1:2 混合核）。
- **五级存储与 ND/NZ 排布**：数据从 GM（Global Memory）经 MTE2 搬入 L1（A1/B1 buffer），再经 MTE1 装载到 L0（A2/B2/CO1），矩阵乘结果经 Fixpipe 写回 GM。Cube 的 Mmad 要求 NZ 分形排布，所以有 `DataCopy` 的 Nd2Nz 转换。
- **跨核同步**：`CrossCoreSetFlag` / `CrossCoreWaitFlag` 是 Cube 核与 Vector 核之间的事件握手原语，配合双缓冲 workspace 实现「Cube 算第 \(i\) 块、Vector 处理第 \(i-1\) 块」的软件流水。
- **TopK 的排序归并实现**：向量侧的 TopK 不是一次全排序，而是「块内排序（Sort32/Sort）→ 四路归并（MrgSort）→ 截断」的归并树；跨核时还要再归并一轮（代码里叫 LD，Long Decode）。
- **模板化 tilingKey**：u4-l5 见过 `GET_TPL_TILING_KEY`——Host 侧把 dtype/layout 等枚举编码成 tilingKey，Device 侧入口按它实例化模板分支。本算子是同一机制的又一个样本。

## 3. 本讲源码地图

算子根目录：`ascendc/src/ops-transformer/attention/lightning_indexer_enhance/`

| 文件 | 作用 |
| --- | --- |
| `docs/npu_lightning_indexer_enhance.md` | 功能说明、计算公式、参数约束、torch 调用示例 |
| `op_api/aclnn_lightning_indexer_enhance.h/.cpp` | aclnn 两段式对外接口（u2-l5 已讲该模式，本讲略） |
| `op_host/lightning_indexer_enhance.cpp/.h` | L0 封装层（namespace l0op）：补默认值、INFER_SHAPE、入发射列表 |
| `op_host/lightning_indexer_enhance_def.cpp` | **OpDef 原型注册**：输入/输出/属性声明 + 芯片配置 |
| `op_host/lightning_indexer_enhance_proto.cpp` | **InferShape / InferDataType 实现** |
| `op_host/lightning_indexer_enhance_tiling.h/.cpp` | Tiling：参数解析校验（LIInfoParser）+ 切分（DoTiling） |
| `op_kernel/lightning_indexer_enhance.cpp` | kernel 入口：按 tilingKey 实例化 `LIPreload` 模板类 |
| `op_kernel/lightning_indexer_enhance_common.h` | 公共类型：`LIType`、`RunInfo`、`ConstInfo`、`SplitCoreInfo` |
| `op_kernel/lightning_indexer_enhance_template_tiling_key.h` | 模板参数（dtype/layout/PA）的合法组合声明 |
| `op_kernel/lightning_indexer_enhance_kernel.h` | `LIPreload` 调度器：分核、循环编排、Cube/Vector 握手 |
| `op_kernel/lightning_indexer_enhance_service_cube.h` | **Cube 侧服务 `LIMatmul`**：打分矩阵乘五级流水 |
| `op_kernel/lightning_indexer_enhance_service_vector.h` | **Vector 侧服务 `LIVector`**：加权/规约/TopK/LD 归并 |
| `op_kernel/lightning_indexer_enhance_vector.h` | `LIServiceVec` 命名空间：CopyIn/DoScale/DoReduce/SortAll 等底层函数 |
| `tests/st/test_npu_lightning_indexer_enhance.py` | ST 精度测试，内含 CPU golden 参考实现 |

建议阅读顺序：docs 公式 → `_def.cpp` → `proto.cpp` → kernel 入口 → `kernel.h` 调度 → 两个 service 头文件。

## 4. 核心概念与源码讲解

先用一张图建立全局直觉。lightning indexer 的本质是**为每个 token 算一行重要性分数，然后取 TopK**：

```text
query [B,S1,N1,D] ──┐
                    │  Cube核: S = ReLU(Q · K^T)   (逐 g 头一行, fp32 落 GM)
key   [*,S2,N2,D] ──┘
                    │  Vector核: 加权 W ⊙ S → 沿 g 求和 → 因果截断 → 排序TopK
weights [B,S1,N1] ──┘
                    ↓
sparse_indices [B,S1,N2,K] (int32)   + 可选 sparse_values
```

官方计算公式（[docs/npu_lightning_indexer_enhance.md:L13-L17](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/lightning_indexer_enhance/docs/npu_lightning_indexer_enhance.md#L13-L17)）：

\[
\text{Indices}=\operatorname{Top-}k\Big\{\ \mathbf{1}_{1\times g}\ @\ \big[(W\ @\ \mathbf{1}_{1\times S_k})\odot\operatorname{ReLU}(Q_{index}@K_{index}^{\top})\big]\Big\}
\]

其中 \(Q_{index}\in\R^{g\times d}\) 是某个 token 的 index query，\(K_{index}\in\R^{S_k\times d}\) 是上下文 index key，\(W\in\R^{g\times 1}\) 是每组一个的可学习权重，\(d=128\)。三步：打分（Matmul + ReLU）→ 组内加权求和（\(\odot W\) 后 \(\mathbf{1}_{1\times g}\) 左乘求和）→ TopK。注意：**这里没有 softmax**——重要性分数是 ReLU 加权和，不是概率；学习目标里提到的「vector 侧 softmax/归一」在当前实现中对应的是「加权 + 沿 \(g\) 规约」，阅读时以源码为准。

下面按五个最小模块精读。

### 4.1 op_host 之 _def.cpp：原型注册与输入输出清单

#### 4.1.1 概念说明

`_def.cpp` 是算子的「户口本」（u2-l2 建立的心智模型）：向 CANN 注册算子名、每个输入输出的必选性/类型/格式、带默认值的属性，以及编译期芯片白名单。对调用者来说，它就是接口规模的权威清单。

#### 4.1.2 核心流程

1. `Input()/Output()/Attr()` 链式声明，顺序即运行期索引（tiling 侧 `QUERY_INDEX=0` 等常量与之对齐）。
2. `OpAICoreConfig` 声明动态能力开关与 `ExtendCfgInfo` 附加信息。
3. `AICore().AddConfig(...)` 逐芯片注册；`OP_ADD(类名)` 把原型登记进注册表。

#### 4.1.3 源码精读

必选输入三个（query/key/weights），可选输入三个（变长元数据与块表）：

- [lightning_indexer_enhance_def.cpp:L23-L33](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/lightning_indexer_enhance/op_host/lightning_indexer_enhance_def.cpp#L23-L33)：`query`、`key`、`weights` 三个必选输入，类型限 BF16/FP16、格式 ND；query/weights 带 `AutoContiguous()`（要求框架保证内存连续）。
- [lightning_indexer_enhance_def.cpp:L34-L48](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/lightning_indexer_enhance/op_host/lightning_indexer_enhance_def.cpp#L34-L48)：三个可选输入——`actual_seq_lengths_query/key`（各 batch 有效长度，int32）与 `block_table`（PageAttention 的 KV 块映射表，int32）。
- [lightning_indexer_enhance_def.cpp:L49-L53](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/lightning_indexer_enhance/op_host/lightning_indexer_enhance_def.cpp#L49-L53)：输出 `sparse_indices`（必选，int32）与 `sparse_values`（可选，与输入同 dtype）。
- [lightning_indexer_enhance_def.cpp:L54-L62](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/lightning_indexer_enhance/op_host/lightning_indexer_enhance_def.cpp#L54-L62)：九个属性全部 OPTIONAL 且带默认值：`layout_query="BSND"`、`layout_key="PA_BSND"`、`sparse_count=2048`（筛选前 2048）、`sparse_mode=3`（只算下三角）、`pre_tokens/next_tokens=int64 最大值`、`return_value=false`、`sparse_block_size=1`、`sparse_block_mode=0`。
- [lightning_indexer_enhance_def.cpp:L64-L74](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/lightning_indexer_enhance/op_host/lightning_indexer_enhance_def.cpp#L64-L74)：`OpAICoreConfig` 打开动态 shape/格式/维数支持，`ExtendCfgInfo` 声明 `aclnnSupport` 与 `jitCompile` 策略；`AddConfig("ascend910b")` 与 `AddConfig("ascend910_93")` 双注册——A2 与 A3 芯片都支持（与 docs 产品表一致）。
- [lightning_indexer_enhance_def.cpp:L77](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/lightning_indexer_enhance/op_host/lightning_indexer_enhance_def.cpp#L77)：`OP_ADD(LightningIndexerEnhance)` 完成注册，类名是四层（def/tiling/proto/kernel）对齐的锚点。

#### 4.1.4 代码实践

**实践目标**：统计 `_def.cpp` 的输入输出个数并制成「接口规格表」，与 tiling 侧索引常量互相验证。

**操作步骤**：

1. 打开 `_def.cpp`，数 `this->Input(...)`、`this->Output(...)`、`this->Attr(...)` 的出现次数，记录每项的 ParamType 与 DataType。
2. 打开 [lightning_indexer_enhance_tiling.h:L46-L64](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/lightning_indexer_enhance/op_host/lightning_indexer_enhance_tiling.h#L46-L64) 的 `QUERY_INDEX...ATTR_SPARSE_BLOCK_MODE_INDEX` 常量，核对顺序一致。

**需要观察的现象 / 预期结果**：应得到下表（输入 6、输出 2、属性 9）：

| 类别 | 名称 | 必选性 | 类型 | 说明 |
| --- | --- | --- | --- | --- |
| 输入 0 | query | REQUIRED | BF16/FP16 | [B,S1,N1,D] 或 [T,N1,D] |
| 输入 1 | key | REQUIRED | BF16/FP16 | PA_BSND/BSND/TND，N2=1 |
| 输入 2 | weights | REQUIRED | BF16/FP16 | [B,S1,N1] 或 [T,N1] |
| 输入 3 | actual_seq_lengths_query | OPTIONAL | INT32 | TND 时必传（前缀和） |
| 输入 4 | actual_seq_lengths_key | OPTIONAL | INT32 | TND/PA 时必传 |
| 输入 5 | block_table | OPTIONAL | INT32 | 仅 PA_BSND 场景 |
| 输出 0 | sparse_indices | REQUIRED | INT32 | TopK 索引 |
| 输出 1 | sparse_values | OPTIONAL | 同输入 | TopK 分数值 |

属性 9 个：layout_query、layout_key、sparse_count、sparse_mode、pre_tokens、next_tokens、return_value、sparse_block_size、sparse_block_mode。tiling 侧常量 `ACTUAL_SEQ_K_INDEX=4`、`BLOCK_TABLE_INDEX=5` 与 def 的声明顺序一一对应，证明「Input 顺序即索引」。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `_def.cpp` 里 `block_table` 挪到 `weights` 之前声明，还会正常工作吗？
**答案**：不会。tiling 侧 `WEIGHTS_INDEX=2`、`BLOCK_TABLE_INDEX=5` 是按当前顺序写死的（kernel 入口参数顺序同样如此）。调序必须同步修改 tiling.h 的索引常量、kernel 入口形参顺序与 L0/aclnn 层的传参，漏一处就会张冠李戴——这是 u2-l2「顺序契约」的又一次体现。

**练习 2**：`sparse_values` 为什么设计成 OPTIONAL？
**答案**：它由 `return_value` 属性控制（默认 false）。下游稀疏 FA 只需要索引；只有训练中需要索引对应分数（例如 u4-l7 的 KL loss 反向）时才付出额外搬运与输出开销。输出可选让「不返回值」的场景省一次 Cast 和一次 GM 写。

**练习 3**：`sparse_mode=3` 在文档里叫什么模式？
**答案**：rightDownCausal——以右顶点为划分的下三角因果掩码（[docs:L57-L60](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/lightning_indexer_enhance/docs/npu_lightning_indexer_enhance.md#L57-L60)），即每个 query 只与「不超过自身位置」的 key 计分。

### 4.2 op_host 之 proto.cpp：InferShape 与 InferDataType

#### 4.2.1 概念说明

`proto.cpp` 实现「图编译期」的输出推导：不给数据、只给输入 shape 与属性，就要算出输出 shape 和 dtype。与 u4-l2 讲过的 FA InferShape 相比，这里的推导多了一个关键输入——`sparse_count` 与 `sparse_block_size` 共同决定输出最后一维 \(K\)。

#### 4.2.2 核心流程

1. 从 `InferShapeContext` 取 query/key shape 与属性指针，全部判空（防御式风格，u3-l1）。
2. 计算 \(K=\lceil \text{sparse\_count} / \text{sparse\_block\_size} \rceil\)。
3. 校验 `layout_query ∈ {TND, BSND}`，按布局分支推导：
   - BSND：输出 `[B, S, N2, K]`（B/S 取自 query，N 取自 key——因为输出按 KV 头数组织）。
   - TND：输出 `[T, N, K]`，其中 N 的维下标取决于 key 是否 PA 排布。
4. InferDataType 固定 `sparse_indices=int32`、`sparse_values=同 query`。

#### 4.2.3 源码精读

- [lightning_indexer_enhance_proto.cpp:L23-L30](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/lightning_indexer_enhance/op_host/lightning_indexer_enhance_proto.cpp#L23-L30)：输入/属性索引常量，与 `_def.cpp` 声明顺序对齐（又一次「顺序契约」）。
- [lightning_indexer_enhance_proto.cpp:L51-L55](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/lightning_indexer_enhance/op_host/lightning_indexer_enhance_proto.cpp#L51-L55)：`selected_count = (sparse_count + sparse_block_size - 1) / sparse_block_size`，即向上取整 \(K=\lceil\text{sparse\_count}/\text{sparse\_block\_size}\rceil\)。
- [lightning_indexer_enhance_proto.cpp:L66-L79](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/lightning_indexer_enhance/op_host/lightning_indexer_enhance_proto.cpp#L66-L79)：BSND 分支——输出四维 `[B,S,N,K]`，注意第 2 维 N 取的是 **keyShape 的 N**（`keyShape->GetDim(2)`）：输出按 KV 头数（N2=1）组织而非 query 头数 N1。
- [lightning_indexer_enhance_proto.cpp:L80-L93](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/lightning_indexer_enhance/op_host/lightning_indexer_enhance_proto.cpp#L80-L93)：TND 分支——输出三维 `[T,N,K]`；第 86 行 `nDimIndex = (layout_key=="PA_BSND") ? 2 : 1` 说明 key 的 N 维位置随排布变化（PA 是 [blockNum, blockSize, N, D] 四维，TND 是 [T,N,D] 三维）。
- [lightning_indexer_enhance_proto.cpp:L99-L112](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/lightning_indexer_enhance/op_host/lightning_indexer_enhance_proto.cpp#L99-L112)：InferDataType——输出 0 固定 int32，输出 1 取 query 的 dtype。
- [lightning_indexer_enhance_proto.cpp:L114-L116](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/lightning_indexer_enhance/op_host/lightning_indexer_enhance_proto.cpp#L114-L116)：`IMPL_OP_INFERSHAPE(LightningIndexerEnhance)` 把两个推导函数挂到算子名上——这是 proto 层的「注册宏」，与 tiling 的 `IMPL_OP_OPTILING` 同构。

顺带一提 [op_host/lightning_indexer_enhance.cpp:L65-L96](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/lightning_indexer_enhance/op_host/lightning_indexer_enhance.cpp#L65-L96)：L0 封装层先 `AllocTensor` 造出两个空输出，再经 `INFER_SHAPE` 宏调用上面的推导，最后 `ADD_TO_LAUNCHER_LIST_AICORE` 把任务排进 executor——正好复习 u2-l5 的两段式链路。

#### 4.2.4 代码实践

**实践目标**：用一组具体参数手工执行 InferShape，验证你能脱离源码推输出。

**操作步骤**（纯纸面推演，无需环境）：

1. 取 docs 示例参数：`layout_query="BSND"`，query `[1,1,64,128]`，key（PA）`[32,256,1,128]`，`sparse_count=2048`，`sparse_block_size=1`。
2. 套用 `selected_count = (2048+1-1)/1 = 2048`；BSND 分支输出 `[B=1, S=1, N=key.dim(2)=1, K=2048]`。
3. 与 docs 示例打印的 `torch.Size([1, 1, 1, 2048])`（[docs:L203-L204](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/lightning_indexer_enhance/docs/npu_lightning_indexer_enhance.md#L203-L204)）比对。

**需要观察的现象 / 预期结果**：手工推导 `[1,1,1,2048]` 与文档输出一致。再换 `sparse_count=4096, sparse_block_size=8` 复推一次，应得 \(K=512\)。

**待本地验证**：若在有 CANN 环境的机器上，可把 `layout_query` 改成 `"TND"` 传四维 query，观察 InferShape 报错 `queryDims must be 3`（proto.cpp L81-L84 的校验分支）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 BSND 输出的 N 维取 key 的 N 而不是 query 的 N1？
**答案**：一组 \(g=N_1/N_2\) 个 query 头的打分被规约成一份索引，输出每个 token 只有一行 TopK，按 KV 侧头数（N2=1）组织最自然；这也与下游稀疏 FA 的 `sparse_indices` 语义对齐。

**练习 2**：`sparse_block_size=8`、`sparse_count=100` 时 \(K\) 是多少？这个 \(K\) 的语义是什么？
**答案**：\(K=\lceil 100/8 \rceil=13\)。语义是「按 8 个 token 一个 block 挑前 13 个 block」——sparse_block_size>1 时索引单位从 token 变成 block（kernel 里会先对 block 内分数取 ReduceMax 再排序，见 4.5 节）。

**练习 3**：InferShape 里为什么拿不到 `actual_seq_lengths` 的值？
**答案**：InferShape 发生在图编译期，只看静态 shape 与属性；变长张量的真实数值要到运行期 tiling/kernel 才能读（kernel 侧用 `actualSeqLengthsGm.GetValue` 逐 batch 取）。这正是 FA 系列「静态推形状、动态靠 tiling」的通用分工。

### 4.3 op_kernel 入口与 LIPreload：Cube+Vector 协同调度

#### 4.3.1 概念说明

本算子 kernel 层分三层：入口（选模板）、调度器 `LIPreload`（分核 + 编排循环 + 核间握手）、两个服务类（`LIMatmul`/`LIVector`，见 4.4/4.5）。调度器是理解「Cube+Vector 协同」的主线：同一个 kernel 程序在 Cube 核和 Vector 核上都运行，靠 `if ASCEND_IS_AIV / ASCEND_IS_AIC` 走不同分支，再靠跨核事件按基本块交替推进。

#### 4.3.2 核心流程

```text
入口 lightning_indexer_enhance()
  ├─ 按 tilingKey 模板参数 (DT_Q/DT_K/PA/LAYOUT_T/K_LAYOUT_T) 选 fp16/bf16 分支
  └─ INVOKE_LI_NO_KFC_OP_IMPL: 解包 TilingData → LIPreload::Init → Process

LIPreload::Init()
  ├─ AIV: tmpBlockIdx∈[0,47], aiCoreIdx=blockIdx/2   (2 个 Vector 共享 1 个 Cube 的 workspace)
  ├─ AIC: tmpBlockIdx∈[0,23], aiCoreIdx=blockIdx
  ├─ InitTilingData / InitActualSeqLen
  ├─ SplitCore(aiCoreIdx, ...) → 本核负责的 [bN2Start..bN2End]×[gS1..]×[s2..] 区间
  ├─ workspace 三段式划分: mm1ResGm(打分) | vec1ResGm(LD中间结果) | vec1ParamGm(LD参数)
  └─ AIV→vectorService.Init / AIC→matmulService.Init, 各自 InitBuffers

LIPreload::Process()
  ├─ usedCoreNum==0 → ProcessInvalid: 全部输出填 -1 (AIV 侧 InitGlobalMemory)
  ├─ ProcessMain: 三层循环 (bN2 → gS1 → s2) 逐基本块 ProcessBaseBlock
  └─ ProcessDecode: S2 被多核切分时, 由 Vector 核做跨核 TopK 归并 (LD)

ProcessBaseBlock(loop, s2Idx)   ← 每个基本块的握手
  AIC: Wait(syncV1C1) → ComputeMm1(打分) → Set(syncC1V1)
  AIV: Wait(syncC1V1) → ProcessVec(加权/规约/TopK) → Set(syncV1C1)
```

双缓冲：打分矩阵写 workspace 时用 `(loop % 2)` 选奇偶缓冲（[service_vector.h:L283](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/lightning_indexer_enhance/op_kernel/lightning_indexer_enhance_service_vector.h#L283)、[service_cube.h:L386](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/lightning_indexer_enhance/op_kernel/lightning_indexer_enhance_service_cube.h#L386)），于是 Cube 算第 \(i\) 块时 Vector 还在消费第 \(i-1\) 块。

#### 4.3.3 源码精读

- [lightning_indexer_enhance.cpp:L45-L55](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/lightning_indexer_enhance/op_kernel/lightning_indexer_enhance.cpp#L45-L55)：入口是 6 个模板参数的 `extern "C" __global__ __aicore__` 函数——`DT_Q/DT_K/DT_OUT/PAGE_ATTENTION/LAYOUT_T/K_LAYOUT_T`，10 个 GM 指针参数（6 输入 + 2 输出 + workspace + tiling），顺序与 def 声明一致。
- [lightning_indexer_enhance.cpp:L60-L62](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/lightning_indexer_enhance/op_kernel/lightning_indexer_enhance.cpp#L60-L62)：`GetUserWorkspace` 取用户 workspace；`KERNEL_TASK_TYPE_DEFAULT(KERNEL_TYPE_MIX_AIC_1_2)` 声明混合核类型——Cube:Vector = 1:2。
- [lightning_indexer_enhance.cpp:L64-L70](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/lightning_indexer_enhance/op_kernel/lightning_indexer_enhance.cpp#L64-L70)：dtype 分支——只有 `DT_Q==DT_K==FP16` 走 half 实例，否则（含 BF16）走 `bfloat16_t` 实例；输出类型固定 int32。合法组合由 [lightning_indexer_enhance_template_tiling_key.h:L43-L67](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/lightning_indexer_enhance/op_kernel/lightning_indexer_enhance_template_tiling_key.h#L43-L67) 的 `ASCENDC_TPL_SEL` 枚举（fp16/bf16 × PA/非 PA 共 4 组），tiling 侧用 `GET_TPL_TILING_KEY` 编码（[lightning_indexer_enhance_tiling.cpp:L835-L836](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/lightning_indexer_enhance/op_host/lightning_indexer_enhance_tiling.cpp#L835-L836)）——u4-l5 稀疏 FA 同款机制。
- [lightning_indexer_enhance.cpp:L23-L43](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/lightning_indexer_enhance/op_kernel/lightning_indexer_enhance.cpp#L23-L43)：两个宏——`LI_COPY_TILING_DATA` 用 `GET_TILING_DATA_WITH_STRUCT`（u2-l4 讲过）把 GM 字节流解包为 `LITilingData`；`INVOKE_LI_NO_KFC_OP_IMPL` 实例化 `LIPreload<LIType<...>>` 并执行 `Init + Process`。
- [lightning_indexer_enhance_kernel.h:L557-L563](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/lightning_indexer_enhance/op_kernel/lightning_indexer_enhance_kernel.h#L557-L563)：Init 的第一步就区分核身份——AIV 的 `aiCoreIdx = GetBlockIdx()/2`（两个 Vector 核映射到同一个 Cube 编号，共享同一段打分 workspace），AIC 直接用自身编号。
- [lightning_indexer_enhance_kernel.h:L572-L591](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/lightning_indexer_enhance/op_kernel/lightning_indexer_enhance_kernel.h#L572-L591)：workspace 三段式布局的注释与实现——`mm1ResGm`（每 Cube 核双份 \(512\times512\) fp32 打分块）→ `vec1ResGm`（LD 中间 TopK 结果）→ `vec1ParamGm`（LD 参数）。tiling 侧按同一公式给 workspace 定容（[lightning_indexer_enhance_tiling.cpp:L793-L803](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/lightning_indexer_enhance/op_host/lightning_indexer_enhance_tiling.cpp#L793-L803)），两侧必须严格一致。
- [lightning_indexer_enhance_kernel.h:L593-L607](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/lightning_indexer_enhance/op_kernel/lightning_indexer_enhance_kernel.h#L593-L607)：AIV 分支初始化 `vectorService` 并绑定输出 GM；AIC 分支初始化 `matmulService` 并绑定 query/key（PA 时含 blockTable）。
- [lightning_indexer_enhance_kernel.h:L809-L822](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/lightning_indexer_enhance/op_kernel/lightning_indexer_enhance_kernel.h#L809-L822)：**本讲最核心的 14 行**——`ProcessBaseBlock`：AIC 先 `CrossCoreWaitFlag(syncV1C1)` 等 Vector 腾出打分缓冲，`ComputeMm1` 后 `CrossCoreSetFlag(syncC1V1)`（PIPE_FIX，即打分落 GM 完成）通知 Vector；AIV 对称地等 `syncC1V1`、做 `ProcessVec`、置 `syncV1C1`（PIPE_MTE2，消费完毕）。一置一等构成逐块乒乓。
- [lightning_indexer_enhance_kernel.h:L717-L726](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/lightning_indexer_enhance/op_kernel/lightning_indexer_enhance_kernel.h#L717-L726)：`Process()` 总控——无任务时 `ProcessInvalid` 清输出为 -1；否则 `ProcessMain + ProcessDecode`。
- [lightning_indexer_enhance_kernel.h:L728-L761](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/lightning_indexer_enhance/op_kernel/lightning_indexer_enhance_kernel.h#L728-L761)：`ProcessInvalid`——AIV 侧把整个 `sparse_indices` 用 `InitGlobalMemory` 填 `INVALID_IDX(-1)`，`return_value` 时 `sparse_values` 填 -inf（fp16 `0xFC00`/bf16 `0xFF80` 位模式）。「空输入 → 全 -1 输出」是稀疏 FA 消费端约定的守门员。
- [lightning_indexer_enhance_kernel.h:L763-L807](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/lightning_indexer_enhance/op_kernel/lightning_indexer_enhance_kernel.h#L763-L807)：`ProcessMain` 三层循环（bN2 → gS1 → s2）+ 变长处理：`curActSeqLenIsZero` 时调 `DealActSeqLenIsZero` 清无效行；末尾 AIC 双重 `CrossCoreWaitFlag(syncV1C1)` 等配对 Vector 收尾。
- [lightning_indexer_enhance_kernel.h:L263-L275](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/lightning_indexer_enhance/op_kernel/lightning_indexer_enhance_kernel.h#L263-L275)：`GetS2BaseBlockNumOnMask`——因果模式下本 gS1 基本块真正需要算的 S2 块数：\( \text{validLen} = \min(s_1\text{Offset} + (S_2 - S_1) + s_1\text{Base},\ S_2) \)。这就是「下三角只算该算的」在调度层的落地（稀疏模式=3 时生效，`attenMaskFlag = (sparseMode == 3)`，[L176](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/lightning_indexer_enhance/op_kernel/lightning_indexer_enhance_kernel.h#L176)）。
- [lightning_indexer_enhance_kernel.h:L506-L515](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/lightning_indexer_enhance/op_kernel/lightning_indexer_enhance_kernel.h#L506-L515)：`SplitCore` 分派到 `SplitCoreNormalLD` / `SplitCoreAverageLD` 两套分核。注意 [tiling.cpp:L778](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/lightning_indexer_enhance/op_host/lightning_indexer_enhance_tiling.cpp#L778) 中 `isAverageLD` 恒为 `false`——AverageLD 是「已备而未用」的路径（与 u3-l3 见过的公共件无人调用现象同款，读库要 grep 核实）。
- [lightning_indexer_enhance_common.h:L21-L36](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/lightning_indexer_enhance/op_kernel/lightning_indexer_enhance_common.h#L21-L36)：`LI_LAYOUT` 枚举与 `LIType` 类型包——把 dtype/PA/layout 打成模板参数包，是入口宏 `LIType<__VA_ARGS__>` 的原材料。
- [lightning_indexer_enhance_common.h:L38-L60](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/lightning_indexer_enhance/op_kernel/lightning_indexer_enhance_common.h#L38-L60)：`RunInfo`——一个基本块的完整描述（bIdx/gS1Idx/s2Idx、实际规模、GM 偏移、首尾标志），是调度器传给两个 service 的「工单」。

tiling 侧只需补一句：[lightning_indexer_enhance_tiling.cpp:L711-L744](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/lightning_indexer_enhance/op_host/lightning_indexer_enhance_tiling.cpp#L711-L744) 的 `ParseAndCheck` 按「取平台→取参数→校验 dtype/layout/shape→推导 B/N/G/S1/S2」的顺序装填 `LITilingInfo`；[L753-L840](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/lightning_indexer_enhance/op_host/lightning_indexer_enhance_tiling.cpp#L753-L840) 的 `DoTiling` 设 blockDim（AIV 数）、workspace、18 个字段的 `LITilingData`（[tiling.h:L81-L102](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/lightning_indexer_enhance/op_host/lightning_indexer_enhance_tiling.h#L81-L102)）与模板 tilingKey。注意 tiling 的切分比 FA 朴素得多：基本块常量（\(M=512, S_2=512, S_1=8\)）直接写死在 [kernel.h:L86-L89](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/lightning_indexer_enhance/op_kernel/lightning_indexer_enhance_kernel.h#L86-L89)，分核则在 kernel 内逐核重放（SplitCore）——Host 只给 `usedCoreNum`，这是「Host 粗切、Device 细分」的另一种风格。

#### 4.3.4 代码实践

**实践目标**：把 Cube+Vector 的乒乓握手画成时序图，并数出一次基本块执行中两类核各做了什么。

**操作步骤**：

1. 精读 [kernel.h:L809-L822](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/lightning_indexer_enhance/op_kernel/lightning_indexer_enhance_kernel.h#L809-L822)，在纸上画两条泳道（AIC / AIV×2），标注 `Wait(syncV1C1)`、`ComputeMm1`、`Set(syncC1V1)`、`Wait(syncC1V1)`、`ProcessVec`、`Set(syncV1C1)` 的先后。
2. 回答：为什么 `syncC1V1` 用 `PIPE_FIX`、`syncV1C1` 用 `PIPE_MTE2`？
3. 思考：如果去掉 `(loop % 2)` 双缓冲，只留单块 workspace，握手会变成什么样？

**需要观察的现象 / 预期结果**：

```text
AIC : ── Wait(syncV1C1)[i] ─ ComputeMm1[i] ─ Set(PIPE_FIX)[i] ─ Wait(syncV1C1)[i+1] ─ ComputeMm1[i+1] ...
AIV : ── Wait(syncC1V1)[i-1] ─ ProcessVec[i-1] ─ Set(PIPE_MTE2) ─ Wait(syncC1V1)[i] ─ ProcessVec[i] ...
```

`PIPE_FIX` 是 Cube 侧 Fixpipe（L0C→GM）完成的事件，打分真正落 GM 才通知 Vector；`PIPE_MTE2` 是 Vector 侧搬入完成的事件，缓冲被读走才允许 Cube 覆写。去掉双缓冲后第 \(i+1\) 块的 ComputeMm1 必须等 ProcessVec\[i\] 完全结束，流水退化成串行，吞吐约减半。

**待本地验证**：在 profiler（如 msprof）下对比双缓冲路径与手工改为单缓冲（改 `WS_DOBULE` 相关偏移）的核耗时。

#### 4.3.5 小练习与答案

**练习 1**：`KERNEL_TYPE_MIX_AIC_1_2` 中 "1_2" 是什么意思？代码哪里体现了 2？
**答案**：1 个 Cube 核配 2 个 Vector 核。体现在 [kernel.h:L557-L559](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/lightning_indexer_enhance/op_kernel/lightning_indexer_enhance_kernel.h#L557-L559) 的 `aiCoreIdx = tmpBlockIdx / 2`，以及 [service_vector.h:L293-L294](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/lightning_indexer_enhance/op_kernel/lightning_indexer_enhance_service_vector.h#L293-L294) 用 `blockId_ % 2` 把 S1 行分给两个 Vector 核各算一半。

**练习 2**：tiling 里 `usedCoreNum` 设成了什么？kernel 里的 `aiCoreIdx >= usedCoreNum` 检查（[kernel.h:L765-L767](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/lightning_indexer_enhance/op_kernel/lightning_indexer_enhance_kernel.h#L765-L767)）防的是什么？
**答案**：`usedCoreNum = blockDim`（按 AIV 数算出的混核 block 数，[tiling.cpp:L756-L760/L823](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/lightning_indexer_enhance/op_host/lightning_indexer_enhance_tiling.cpp#L756-L760)）。任务不足以占满所有核时（如 B=1、S 很短），多余核的 `aiCoreIdx` 越界，直接 return 避免空跑与越界写。

**练习 3**：为什么分核逻辑（SplitCore）放在 kernel 里而不是 tiling 里？
**答案**：分核依赖每个 batch 的 `actualSeqLengths` 真实值（[kernel.h:L282-L295](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/lightning_indexer_enhance/op_kernel/lightning_indexer_enhance_kernel.h#L282-L295) 的 `GetTotalBaseBlockNum` 逐 batch 累加）。变长场景下这些值 tiling 阶段虽可读，但把细粒度负载均衡放到 kernel 内逐核重放，可以让同一份编译产物自适应不同长度分布（代价是每个核都要重放一遍循环计数）。

### 4.4 op_kernel 之 service_cube.h：Cube 侧打分矩阵乘

#### 4.4.1 概念说明

`LIMatmul` 只干一件事：算 \(S = \operatorname{ReLU}(Q\cdot K^{\top})\) 并把 fp32 结果写进 workspace 的打分缓冲。特别之处在于它**没有用** CANN 的 Matmul 高阶接口（`lib/matmul_intf.h` 的 Matmul 对象，u4-l4 反向算子用的是那种），而是用 `DataCopy(Nd2Nz) → LoadData3D/LoadData2D → Mmad → DataCopy(Fixpipe)` 原语手写了整条流水——文件头注释直言「use 5 buffer for matmul l1, better pipeline」。读它能看清昇腾 Cube 的存储层级到底怎么被手动编排。

#### 4.4.2 核心流程

`ComputeMm1` 的四层循环（S2 的 GM 分段 → S1g 的 GM 分段 → S2 的 L0 分形 → S1g 的 L0 分形）：

```text
for s2GmOffset (步长256):            # key 的 GM 段
    Wait(KEY 事件) → KeyNd2Nz(/ForPA): GM→L1(B1), ND 转 NZ 分形
    for s1gGmOffset (步长256):        # query 的 GM 段, 仅首个S2块搬一次
        QueryNd2Nz: GM→L1(A1)
        for s2L1Offset (步长128):     # L1→L0(B2)
            for s1gL1Offset (步长128): # L1→L0(A2)
                Wait(L0 事件) → LoadQueryToL0a(LoadData3D) + LoadKeyToL0b(LoadData2D)
                ComuteL0c: Mmad(C[j] = A[j]·B[j])     # L0 上 128×128×128
                Fixp: L0C→GM, nz2nd + ReLU + 双缓冲选址
```

多缓冲：key 3 份 L1 buffer、query 2 份、L0AB/L0C 各 2 份（`KEY_BUF_NUM=3` 等，[service_cube.h:L43-L45](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/lightning_indexer_enhance/op_kernel/lightning_indexer_enhance_service_cube.h#L43-L45)），靠 `bufIdx % BUF_NUM` 轮转，配合 `WaitFlag/SetFlag<HardEvent::MTE1_MTE2 / M_MTE1 ...>` 做 MTE2（GM→L1）、MTE1（L1→L0）、M（Mmad）三级流水。

#### 4.4.3 源码精读

- [lightning_indexer_enhance_service_cube.h:L54-L65](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/lightning_indexer_enhance/op_kernel/lightning_indexer_enhance_service_cube.h#L54-L65)：两级基本块——L1 层 \(256\times128\)（M×D）、L0 层 \(128\times128\)。D 固定 128 与 tiling 的 `headDim==128` 校验（[tiling.cpp:L453-L455](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/lightning_indexer_enhance/op_host/lightning_indexer_enhance_tiling.cpp#L453-L455)）互为因果：headDim 不是 128 这套分形就对不齐。
- [lightning_indexer_enhance_service_cube.h:L114-L128](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/lightning_indexer_enhance/op_kernel/lightning_indexer_enhance_service_cube.h#L114-L128)：`InitBuffers` 用 `TBuf<TPosition::A1/B1/A2/B2/CO1>` 在 L1/L0 上开缓冲——注意这些位置不是 UB，是 Cube 专属的片上存储（对照 u2-l4 的 TPipe/UB 世界观，Cube 侧有自己的 buffer 体系）。
- [lightning_indexer_enhance_service_cube.h:L142-L203](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/lightning_indexer_enhance/op_kernel/lightning_indexer_enhance_service_cube.h#L142-L203)：`ComputeMm1` 主体，即上面的四层循环。两个细节：(1) query 只在 `isFirstS2InnerLoop && s2GmOffset==0` 时搬入 L1（L163-L169），后续 S2 块复用；(2) 尾块用 `s?L?RealSize` 逐层收缩，保证变长 S2 不越界。
- [lightning_indexer_enhance_service_cube.h:L206-L239](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/lightning_indexer_enhance/op_kernel/lightning_indexer_enhance_service_cube.h#L206-L239)：`KeyNd2Nz`——把 ND 排布的 key 搬成 NZ 分形（`dstNzC0Stride` 对齐到 16 的 `BLOCK_CUBE`），并按「前/后 L0 分形」决定落在 L1 buffer 的哪一段（注释：暂时只支持两个分形，对应 \(S_2\) 基本块 512 = 2×256）。
- [lightning_indexer_enhance_service_cube.h:L243-L281](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/lightning_indexer_enhance/op_kernel/lightning_indexer_enhance_service_cube.h#L243-L281)：`KeyNd2NzForPA`——PageAttention 版取数：先由 `block_table[bIdx * maxBlockNumPerBatch + s2BlkId]` 查块号，再乘 `kCacheStride`（支持非连续 KV cache，tiling 侧 `GetKeyStride` 校验过 stride 合法性，[tiling.cpp:L525-L559](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/lightning_indexer_enhance/op_host/lightning_indexer_enhance_tiling.cpp#L525-L559)），且搬运长度不得超过块边界（L258-L259）。
- [lightning_indexer_enhance_service_cube.h:L303-L336](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/lightning_indexer_enhance/op_kernel/lightning_indexer_enhance_service_cube.h#L303-L336)：`LoadQueryToL0a` 用 `LoadData3DParamsV2` 把 L1 的 query 装进 L0A——把矩阵装载伪装成 1×1 卷积滑窗（`l1H/l1W/channelSize` 对应 M0/M1/K），`padList[3]=255` 的注释说明尾部 padding 不影响滑窗结果。
- [lightning_indexer_enhance_service_cube.h:L354-L369](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/lightning_indexer_enhance/op_kernel/lightning_indexer_enhance_service_cube.h#L354-L369)：`ComuteL0c`——一次 `Mmad`：\(m=\)S1g 行、\(n=\)S2 列、\(k=128\)，`cmatrixInitVal=true` 覆盖式累加；小分块（\((m/16)(n/16)<10\)）后补 `PipeBarrier<PIPE_M>` 防读冒险。
- [lightning_indexer_enhance_service_cube.h:L372-L389](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/lightning_indexer_enhance/op_kernel/lightning_indexer_enhance_service_cube.h#L372-L389)：`Fixp`——把 L0C 的 fp32 结果经 Fixpipe 写 GM：`nz2ndEn=true` 转 ND、**`reluPre=1` 在这里施加 ReLU**、`dstStride=actualSingleProcessSInnerSizeAlign`（行对齐到 32B）、目标地址带 `(runInfo.loop % 2)` 双缓冲偏移。这一行就是公式里 ReLU 的落点。
- [lightning_indexer_enhance_service_cube.h:L392-L419](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/lightning_indexer_enhance/op_kernel/lightning_indexer_enhance_service_cube.h#L392-L419)：`AllocEventID/FreeEventID`——进入/退出时把所有缓冲的事件预置为「可用」，收尾时对称 Wait 掉，保证下一次 kernel 复用事件不残留。

#### 4.4.4 代码实践

**实践目标**：核对手写 Matmul 的分块数学：验证 \(512\times512\) 的基本块确实由 4×2×1×1 个 L0 级 \(128\times128\) Mmad 拼成。

**操作步骤**（纸面推演）：

1. 从 [kernel.h:L86-L87](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/lightning_indexer_enhance/op_kernel/lightning_indexer_enhance_kernel.h#L86-L87) 取 \(M\_BASE\_SIZE=512\)、\(S2\_BASE\_SIZE=512\)；从 [service_cube.h:L54-L60](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/lightning_indexer_enhance/op_kernel/lightning_indexer_enhance_service_cube.h#L54-L60) 取两级块 256/128。
2. 数 `ComputeMm1` 四层循环在满块时的迭代数：s2Gm(512/256=2) × s1gGm(512/256=2) × s2L1(256/128=2) × s1gL1(256/128=2)，其中 Mmad 发生在最内两层。
3. 检查 L1 缓冲容量是否够：`KEY_BUF_NUM=3` 份 \(256\times128\)、`QUERY_BUF_NUM=2` 份 \(256\times128\)（[L116-L119](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/lightning_indexer_enhance/op_kernel/lightning_indexer_enhance_service_cube.h#L116-L119)）。

**需要观察的现象 / 预期结果**：最内两层每次一个 \(128\times128\times128\) 的 Mmad，一个 \(512\times512\) 基本块共 \(2\times2\times2\times2=16\) 次 Mmad（按 s2Gm×s1gGm×s2L1×s1gL1 组合）。key 的 L1 需要 3 份 \(256\times128\) 是因为 GM 层 s2 循环向前预取两段时仍要保住当前段。bf16 下单个 L1 key buffer 为 \(256\times128\times2\text{B}=64\text{KB}\)，3 份 192KB，在 A2/A3 的 L1 容量内。

**待本地验证**：精确的 L1/L0 容量上限与事件时序需在昇腾环境用 msprof 确认。

#### 4.4.5 小练习与答案

**练习 1**：为什么 query 用 3D 装载（LoadData3D）而 key 用 2D（LoadData2D）？
**答案**：query 的 L1 排布要支持「同一份 Q 与多个 K 段复用」，且尾块 M 行数任意——3D 滑窗参数（l1H/mExtension/padList）天然支持从 L1 的任意行窗口取子矩阵进 L0A；key 每段都是整块搬运，2D 按 repeat 平铺即可（[L339-L351](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/lightning_indexer_enhance/op_kernel/lightning_indexer_enhance_service_cube.h#L339-L351)）。

**练习 2**：ReLU 为什么放在 Fixpipe（reluPre）而不是 Vector 侧做？
**答案**：Fixpipe 是 L0C→GM 的必经站，在这里顺手做 ReLU 是「免费」的（不占 Vector 指令、不多读一遍 GM）；若放 Vector 侧，\(g\times S_2\) 的矩阵要多一次逐元素 pass。这是 Cube/Vector 职责划分里典型的「能并入数据通路的操作不单独做」。

**练习 3**：`KeyNd2NzForPA` 里 `s2BlkId = (s2L1Offset + s2GmOffset) / kCacheBlockSize`，如果不做块边界截断（L258-L259）会发生什么？
**答案**：一次 DataCopy 可能横跨两个 KV cache block，而两个 block 在 GM 上不一定连续（块号经 block_table 间接寻址），会把别的 block 的数据抄进当前段的 NZ 分形里，打分行错位、TopK 索引整体错误——且这种错误不越界、不报错，只能靠精度测试抓出来。

### 4.5 op_kernel 之 service_vector.h：Vector 侧加权、规约与 TopK

#### 4.5.1 概念说明

`LIVector` 消费 Cube 写好的打分矩阵，完成公式剩余部分：\(\odot W\) → 沿 \(g\) 求和 → 因果截断 → 排序 TopK → 抽取索引输出。此外它还承担三件「边界杂务」：无效位置清 -1、变长 S2 尾块处理、跨核 TopK 归并（LD）。两个 Vector 核（配对同一 Cube）按 S1 行对半分工。

#### 4.5.2 核心流程

`ProcessVec`（单个基本块、单个 Vector 核）：

```text
对每个本核负责的 S1 行 (innerS1Idx):
  1. CopyIn: 打分块 mm1ResGm[loop%2 双缓冲] + weights 行 → UB (G 分组 ping-pong)
  2. DoScale: W 广播(Brcb)后逐行 Mul; G>16 时分组累加
  3. DoReduce: 沿 G 行二分法 Add, 得到本行分数 [s2]
  4. 因果截断: cuRealAcSeq 限制 cuS2Len (只处理合法列)
  5. TopK 两路:
     a. actS1Size>4 或 sparseCountFlag: SortAll 全排序 → MergeSort 并入 globalTopkUb_
     b. 小场景缓存路径: 每块 Sort → 攒 4 块 MrgBasicBlock/SparseTopK 精排
  6. 输出两路:
     a. 本核独占整个 S2: Extract 拆 value/index → CopyOut 到 indiceOutGm(/valueOutGm)
     b. S2 被多核切分: 把 TopK 中间结果+参数写入 vec1ResGm/vec1ParamGm, 留给 LD
收尾: BSND 布局下补清无效 S1 行 (-1)
ProcessDecode → ProcessLD: 沿核链 MrgSort 归并各核 TopK → 最终 Extract/CopyOut
```

#### 4.5.3 源码精读

- [lightning_indexer_enhance_service_vector.h:L29-L30](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/lightning_indexer_enhance/op_kernel/lightning_indexer_enhance_service_vector.h#L29-L30)：两个关键常量 `BASE_TOPK=2048`（单核 TopK 缓冲容量）、`LD_PARAM_NUM=16`（LD 参数槽位数）。
- [lightning_indexer_enhance_service_vector.h:L283-L298](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/lightning_indexer_enhance/op_kernel/lightning_indexer_enhance_service_vector.h#L283-L298)：基本块地址——`mmGmOffset = (loop%2) * (s1Base*gSize*s2Base)` 选奇偶打分缓冲；`blockId_%2` 把 S1 行对半分给两个 Vector 核（`cuS1ProcNumPerAiv`），奇数核地址加半段偏移。
- [lightning_indexer_enhance_service_vector.h:L313-L326](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/lightning_indexer_enhance/op_kernel/lightning_indexer_enhance_service_vector.h#L313-L326)：因果截断——`cuRealAcSeq = actS2 - (actS1 - cuS1Begin)`（右对齐下三角），随后 `cuS2Len = min(块尾, cuRealAcSeq) - 块首`：非法列根本不进入排序，等价于 CPU golden 里 `reduce_out[-1-i, tmp_s2-i:] = -inf`（[test_npu_lightning_indexer_enhance.py:L95-L97](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/lightning_indexer_enhance/tests/st/test_npu_lightning_indexer_enhance.py#L95-L97)）。
- [lightning_indexer_enhance_service_vector.h:L335-L370](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/lightning_indexer_enhance/op_kernel/lightning_indexer_enhance_service_vector.h#L335-L370)：G 轴外层循环（每组 `groupInner_=16` 个头，ping-pong 双缓冲 UB）——`LIServiceVec::CopyIn` 搬打分行与权重，`LIServiceVec::DoScale` 完成加权。
- [lightning_indexer_enhance_vector.h:L82-L115](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/lightning_indexer_enhance/op_kernel/lightning_indexer_enhance_vector.h#L82-L115)：`DoScale` 三步——非 float 类型先 `Cast`；`Brcb` 把 \([\text{groupInner},1]\) 的权重广播成 \([\text{groupInner},8]\)；逐行 `Mul`（首组直写 reduceCache，后续组原地乘再 `Add` 累加，天然减少一次拷贝）。
- [lightning_indexer_enhance_service_vector.h:L372-L375](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/lightning_indexer_enhance/op_kernel/lightning_indexer_enhance_service_vector.h#L372-L375) + [lightning_indexer_enhance_vector.h:L130-L153](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/lightning_indexer_enhance/op_kernel/lightning_indexer_enhance_vector.h#L130-L153)：`DoReduce`——沿 G 行求和用「最近 2 的幂二分累加」：先把非 2 幂的尾巴 Add 掉，再逐层折半，\(O(r \cdot a)\) 次加法但每步都是整块向量 Add，比逐行循环快得多。
- [lightning_indexer_enhance_service_vector.h:L378-L441](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/lightning_indexer_enhance/op_kernel/lightning_indexer_enhance_service_vector.h#L378-L441)：`sparseBlockSize==1` 主路径的 TopK。先备好「分数 + 全局索引」双通道（`Adds(sortIndiceUbInt, globalTopkIndice_, cuBaseS2Idx)` 生成带块基址的索引，无效位填 -1）；然后二选一：
  - [L396-L403](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/lightning_indexer_enhance/op_kernel/lightning_indexer_enhance_service_vector.h#L396-L403)：`SortAll`（Sort32 起步、四路 MrgSort 逐层归并，[vector.h:L188-L247](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/lightning_indexer_enhance/op_kernel/lightning_indexer_enhance_vector.h#L188-L247)）+ `MergeSort` 并入已有序的 `globalTopkUb_`（[vector.h:L271-L318](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/lightning_indexer_enhance/op_kernel/lightning_indexer_enhance_vector.h#L271-L318)，超过 3072 时拆三段四路归并）。
  - [L404-L438](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/lightning_indexer_enhance/op_kernel/lightning_indexer_enhance_service_vector.h#L404-L438)：小场景（actS1Size≤4 且非 sparseCountFlag）缓存路径——每个 512 块局部 `Sort` 进 `SortedBasicBlock_`，攒满 4 块或 S2 结束时 `MrgBasicBlock`/`SparseTopK`（[vector.h:L329-L393](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/lightning_indexer_enhance/op_kernel/lightning_indexer_enhance_vector.h#L329-L393)）精排，避免频繁小归并。
- [lightning_indexer_enhance_service_vector.h:L443-L483](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/lightning_indexer_enhance/op_kernel/lightning_indexer_enhance_service_vector.h#L443-L483)：本核独占 S2 时直接输出——`Extract` 把 (value,index) 交错对拆开（sparse_count>4096 时分两段各拷一半），索引 `CopyOut` 到 `indiceOutGm`；`return_value` 时 fp32 `Cast` 回 bf16/fp16 再拷 `valueOutGm`。
- [lightning_indexer_enhance_service_vector.h:L485-L527](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/lightning_indexer_enhance/op_kernel/lightning_indexer_enhance_service_vector.h#L485-L527)：S2 跨核时的「存档」——TopK 中间结果写 `vec1ResGm`，同时把 16 个参数（needFd、s2AcSeq、s2Start/s2End、isS2End、输出偏移等）写 `vec1ParamGm`；头/尾两个槽位（`wsInfoOffset += paramNum_`）标记本核片段该与前一核还是后一核归并。
- [lightning_indexer_enhance_service_vector.h:L761-L918](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/lightning_indexer_enhance/op_kernel/lightning_indexer_enhance_service_vector.h#L761-L918)：`ProcessLD`——跨核归并：先扫参数区找本组 needFd=1 的 S1 行（L783-L790），从自己尾片段开始沿核链 `while(needFd==1)` 逐核搬头片段进 UB，每攒 4 条 `MrgSort` 四路归并一次（L833-L855），直到 `isS2End`；最后 `Extract` + `DataCopyPad` 写出最终 TopK 索引（L889-L916）。谁来做 LD？普通路径由分核时 `isLD`（S2 组的首核，[kernel.h:L355](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/lightning_indexer_enhance/op_kernel/lightning_indexer_enhance_kernel.h#L355)）指定。
- [lightning_indexer_enhance_service_vector.h:L920-L1163](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/lightning_indexer_enhance/op_kernel/lightning_indexer_enhance_service_vector.h#L920-L1163)：`ProcessAverageLD`——均分版 LD（多核分摊归并行）；但如 4.3 节所述，tiling 恒置 `isAverageLD=false`，当前为不可达代码，阅读时作参考实现看即可。
- [lightning_indexer_enhance_service_vector.h:L528-L724](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/lightning_indexer_enhance/op_kernel/lightning_indexer_enhance_service_vector.h#L528-L724)：`sparseBlockSize>1` 的 block 级路径——先 `ReduceMax` 把每个 block 内分数压成代表值（`sparse_block_mode=0` 按最大值，[docs:L70](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/lightning_indexer_enhance/docs/npu_lightning_indexer_enhance.md#L70)），索引以 block 为单位（`cuBaseS2BlockIdx`），攒满 `sparseBlockSize` 个块后再走与上面相同的排序输出流程。
- [lightning_indexer_enhance_service_vector.h:L246-L274](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/lightning_indexer_enhance/op_kernel/lightning_indexer_enhance_service_vector.h#L246-L274)：`CleanInvalidOutput`——单行兜底清 -1（变长 S1、actSeqLen=0 等场景调用），与调度层的 `ProcessInvalid`/`DealActSeqLenIsZero` 构成三层「无效必清」防线。

#### 4.5.4 代码实践

**实践目标**：用 torch（CPU 即可）复现 lightning indexer 的核心数据流，并为每一步标注 kernel 侧对应函数，形成「算法 ↔ 源码」映射表。

**操作步骤**：

1. 阅读仓库自带的 CPU golden [test_npu_lightning_indexer_enhance.py:L53-L104](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/lightning_indexer_enhance/tests/st/test_npu_lightning_indexer_enhance.py#L53-L104)，理解它按 batch 展开计算的方式。
2. 运行下面的复现脚本（示例代码，任何有 torch 的机器可跑，无需 NPU）：

```python
# 示例代码：lightning indexer 核心数据流的 CPU 复现（不含 PA/变长，仅算法主线）
import torch

torch.manual_seed(0)
B, S1, S2, N1, D, K = 1, 4, 512, 64, 128, 32   # N2=1, g=N1/N2=64

query   = torch.randn(S1, N1, D, dtype=torch.float32)   # 一个 batch
key     = torch.randn(S2, D, dtype=torch.float32)
weights = torch.randn(S1, N1, dtype=torch.float32)

# 步骤1: 打分 Q·K^T —— 对应 Cube 侧 LIMatmul::ComputeMm1(ComuteL0c 的 Mmad)
#        ReLU 在 Cube Fixp 阶段施加 (Fixp 的 reluPre=1)
score = torch.relu(torch.einsum('snd,td->nst', query, key))     # [S1, N1(g), S2]

# 步骤2: 加权 W ⊙ S —— 对应 Vector 侧 LIServiceVec::DoScale (Brcb 广播 + Mul)
weighted = score * weights.unsqueeze(-1)                        # [S1, g, S2]

# 步骤3: 沿 g 求和 —— 对应 LIServiceVec::DoReduce (二分法 Add)
reduce_out = weighted.sum(dim=1)                                # [S1, S2]

# 步骤4: 因果掩码(sparse_mode=3 右对齐下三角)
#        对应 Vector 侧 cuRealAcSeq 截断 cuS2Len (非法列不进排序)
for i in range(S1):
    reduce_out[S1 - 1 - i, S2 - i:] = float('-inf')

# 步骤5: TopK —— 对应 SortAll/MergeSort(或 Sort/MrgBasicBlock/SparseTopK) + Extract
values, indices = torch.sort(reduce_out, dim=1, descending=True)
sparse_indices = indices[:, :K].to(torch.int32)                 # 输出 int32
print(sparse_indices.shape)      # 期望 [S1, K] = [4, 32]
print(sparse_indices[0][:8])     # 首行前 8 个选中位置(均 < S2-i)
```

3. 对照输出检查因果性：第 \(j\) 行（从后往前数第 \(i=S1-1-j\) 行）的所有索引都应 \(< S2-i\)。

**需要观察的现象 / 预期结果**：脚本输出 `torch.Size([4, 32])`，且首行（最后一个 token）索引上限为 \(S2-1\)，末行（第一个 token）索引上限为 \(S2-S1\)。把 `K` 改成 2048 以上时注意本脚本是全排序，而 kernel 侧用归并树——结果集合相同、顺序可能不同（ST 测试正是用「集合相等」断言，[test:L38-L52](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/lightning_indexer_enhance/tests/st/test_npu_lightning_indexer_enhance.py#L38-L52)）。

完整映射表：

| # | 算法步骤 | torch 语句 | kernel 侧函数 | 位置 |
| --- | --- | --- | --- | --- |
| 1 | 打分 \(QK^\top\) | `einsum` | `LIMatmul::ComputeMm1` → `ComuteL0c`(Mmad) | service_cube.h:L142/L354 |
| 2 | ReLU | `torch.relu` | `Fixp` 的 `reluPre=1`（Fixpipe 顺带） | service_cube.h:L384 |
| 3 | 加权 \(\odot W\) | `* weights` | `LIServiceVec::DoScale`（Brcb+Mul） | vector.h:L82；调用 service_vector.h:L359 |
| 4 | 沿 \(g\) 求和 | `.sum(dim=1)` | `LIServiceVec::DoReduce`（二分 Add） | vector.h:L130；调用 service_vector.h:L374 |
| 5 | 因果截断 | `[...]=-inf` | `cuRealAcSeq`/`cuS2Len` 限列 | service_vector.h:L313-L326 |
| 6 | 排序 TopK | `torch.sort` | `SortAll`+`MergeSort` 或 `Sort`+`MrgBasicBlock`+`SparseTopK` | service_vector.h:L396-L438 |
| 7 | 索引抽取输出 | 切片+转 int32 | `Extract` + `CopyOut` | service_vector.h:L448-L483 |
| 8 | 跨核归并 | （单核无对应） | `ProcessLD` 的 `MrgSort` 链 | service_vector.h:L761-L918 |

**待本地验证**：与 NPU 输出的逐行集合相等性比对需要昇腾环境（参考 ST 用例 `test_bsnd_lightning_indexer_eager`）。

#### 4.5.5 小练习与答案

**练习 1**：`globalTopkUb_` 里存的是「分数+索引」交错对，`InitSortOutBuf` 为什么要初始化成 `-inf, -1, -inf, -1 ...`？
**答案**：归并排序要求输入有序且定长。TopK 缓冲初始为空，用 \(-\infty\)（分数最小）和 \(-1\)（无效索引）填充的「空记录」参与 MrgSort 时永远排在有效记录之后，归并结果前 \(k\) 个即真实 TopK——这是排序式 TopK 的标准哨兵技巧（[vector.h:L160-L180](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/lightning_indexer_enhance/op_kernel/lightning_indexer_enhance_vector.h#L160-L180)，mask1/mask0 两个交替位掩码分别写奇偶位置）。

**练习 2**：`Extract` 之后 `indiceOutGm` 的地址为什么用 `info.indiceOutOffset + cuS1Idx * sparseCount` 而不是简单地连续递增？
**答案**：输出的逻辑形状是 \([B,S1,N2,K]\)，一行 \(K\) 个索引；`indiceOutOffset` 是本 batch/头的基本块基地址（BSND 与 TND 的偏移公式不同，见 [kernel.h:L699-L708](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/lightning_indexer_enhance/op_kernel/lightning_indexer_enhance_kernel.h#L699-L708)），`cuS1Idx * sparseCount` 跳到本 token 的行首。两个 Vector 核分摊不同 S1 行、多个核分摊不同 S2 段，各自只写自己那段，必须由地址公式保证不相交。

**练习 3**：LD 归并为什么「每个核都忽略自己的头规约」？由谁兜底？
**答案**：一条 S2 链上，核 \(i\) 的尾片段与核 \(i+1\) 的头片段是同一段数据的两份视角，只需归并一次。约定「尾-头」方向由前一个核负责：链首核从自己的尾片段开始向后收头片段（[service_vector.h:L777-L780](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/lightning_indexer_enhance/op_kernel/lightning_indexer_enhance_service_vector.h#L777-L780) 注释），链首核没有头规约天然成立；非链首核的 `ProcessLD` 因 `s1ProcNum==0` 或 rank 越界提前 return。这样每段恰好被归并一次，不多不少。

## 5. 综合实践

把本讲全部知识串成一条「文档 → Host → Device → 验证」的完整链路：

1. **规格侧**：完成 4.1.4 的接口规格表，并用 4.2.4 的参数手工推一次 InferShape。
2. **Host 侧**：在 [tiling.cpp:L843-L855](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/lightning_indexer_enhance/op_host/lightning_indexer_enhance_tiling.cpp#L843-L855) 的 `TilingForLightningIndexerEnhance` 里跟踪一次调用：`ParseAndCheck` 装填 `LITilingInfo` → `DoTiling` 写 18 个 TilingData 字段、workspace 与模板 tilingKey。回答：为什么 `headDim` 只允许 128（提示：Cube 分形常量）。
3. **Device 侧**：画完整的算子执行时序图（含 Cube/Vector 双泳道、双缓冲、LD 阶段），并把 4.5.4 的映射表逐行标注到图上。
4. **验证侧**：跑通 4.5.4 的 CPU 复现脚本；有 NPU 环境时按 [docs:L94-L131](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/lightning_indexer_enhance/docs/npu_lightning_indexer_enhance.md#L94-L131) 的单算子示例调用 `torch.ops.custom.npu_lightning_indexer_enhance`，与 CPU 结果做「集合相等」比对（复用 ST 的 `_check_indices_equal`）；无 NPU 环境则写明缺口清单（CANN 算子包、torch_npu、omni_training_custom_ops wheel）。
5. **思考题**：如果把本算子接到 u4-l5 的稀疏 FA 后面组成训练图，`sparse_indices` 的 -1 填充与 `sparse_block_size` 语义分别如何影响下游？（答案要点：-1 尾部让 FA 的取数端跳过无效槽；block 模式下索引单位是块号，FA 侧按 `sparse_block_size` 展开成 token 范围。）

## 6. 本讲小结

- lightning indexer 是稀疏注意力训练的「索引生产者」：\( \text{TopK}\{\mathbf{1}_{1\times g} @ [(W@\mathbf{1})\odot\text{ReLU}(QK^\top)]\} \)——打分在 Cube、加权/规约/TopK 在 Vector、ReLU 免费搭在 Fixpipe 上。
- `_def.cpp` 给出接口权威清单：6 输入（3 必选）、2 输出（1 必选）、9 属性，A2/A3 双芯片注册；`proto.cpp` 用 \(K=\lceil\text{sparse\_count}/\text{sparse\_block\_size}\rceil\) 推输出 \([B,S,N2,K]\)/\([T,N,K]\)。
- kernel 是 `KERNEL_TYPE_MIX_AIC_1_2` 混合核：调度器 `LIPreload` 用 `CrossCoreSetFlag/WaitFlag`（`PIPE_FIX` 与 `PIPE_MTE2`）+ `(loop%2)` 双缓冲 workspace 实现 Cube/Vector 逐基本块乒乓。
- Cube 侧 `LIMatmul` 手写五级流水（Nd2Nz→L1→LoadData3D/2D→L0→Mmad→Fixpipe），PA 场景经 block_table + kCacheStride 间接寻址取非连续 KV。
- Vector 侧 `LIVector` 完成加权（DoScale）、G 规约（DoReduce 二分累加）、TopK（Sort/MrgSort 归并树 + 哨兵 -inf/-1）、因果截断（cuRealAcSeq）与跨核 LD 归并；无效位置三层防线统一清 -1。
- tiling 采用「Host 粗切（blockDim/workspace/18 字段）+ Device 细分（SplitCore 逐核重放）」；`isAverageLD` 当前恒 false，AverageLD 是已备未用路径。

## 7. 下一步学习建议

- 下一讲 **u4-l7 SparseLightningIndexerGradKlLoss** 讲 indexer 家族的反向：KL 散度对 logits 求梯度，正好消费本讲 `return_value=true` 时输出的 `sparse_values`，建议先复习本讲的分数定义。
- 回读 **u4-l5** 的稀疏 FA kernel（`sparse_flash_attention_enhance_kernel_mla.h`），确认 `sparse_indices` 的消费方式与本讲的产出约定（-1 填充、块单位）闭环。
- 想深挖 Vector 排序原语的，精读 [lightning_indexer_enhance_vector.h](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Op/blob/c1d24e36d7fb94a98607a02b0edc88c47b64d850/training/ascendc/src/ops-transformer/attention/lightning_indexer_enhance/op_kernel/lightning_indexer_enhance_vector.h) 的 `SortAll`/`MergeSort`，对照 CANN 的 Sort/MrgSort 指令文档理解四路归并树的参数含义。
- 想对比「手写 Cube 流水」与「Matmul 高阶接口」两种风格的，回到 u4-l4 的 `basic_modules/cube_op.h` 做一次同任务对照，体会抽象层级与可控性的取舍。
