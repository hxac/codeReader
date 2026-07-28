# 架构取舍、限制与二次开发方向

## 1. 本讲目标

本讲是 AttentionEngine 学习路线的收官篇。前面二十四讲我们沿着「用户 API → 符号 IR → 降级 → 模板 → 引擎」这条编译链，从外到内、从浅到深地把每一层都拆开看过。本讲不再引入新机制，而是退后一步，从**整体**的视角回答三个问题：

1. **它能做什么、不能做什么？** —— 量化框架的能力边界，并把每一条限制精确落到具体源码行。
2. **它接下来要做什么？** —— 梳理 README/docs 中声明的 roadmap，并与仓库里**实际已经实现**的代码做交叉比对，识别「文档落后于代码」的部分。
3. **我想扩展它，该从哪里下手？** —— 给出一张「功能类型 → 落在哪一层 → 改哪些文件」的决策表，作为二次开发的路线图。

学完本讲，你应该能够：

- 准确说出 online_func 为何不支持自动微分、反向只算 q/k/v 梯度的代码依据；
- 区分「README 声明的 roadmap」与「源码里散落的 TODO 注释」两类待办；
- 评估新增一个算子 / 一个新后端 / 一个新场景（varlen、blocksparse）分别要动 IR、降级、模板的哪几层，并写出实现与对齐测试的思路。

## 2. 前置知识

本讲建立在 u5-l5「综合实战：实现一种新的自定义注意力」之上，并串联此前所有讲义的核心结论。开始前请确认你已经理解：

- **编译链四层架构**（u1-l3、u3-l2）：`transform`（符号 IR）→ `codegen`（发射）→ `lower`（降级编排）→ `template`（模板渲染）。lower 层是唯一同时认识另外三层的「指挥者」。
- **自动微分边界**（u2-l1、u2-l5、u5-l5）：`score_mod` 的反向不依赖 PyTorch autograd，而由 `SymbolScalar._backward` 手写的反向模式自动微分完成；这套手写 autodiff 只支持有限算子，因此反向约束了用户的写法（例如 sigmoid 要用 tanh 等价写）。
- **两种后端**（u5-l1、u5-l2）：`tl`（TileLang，训练/解码主力）与 `cute`（CuTe C++，面向 Hopper），输入相同、产物形态不同。
- **引擎分发**（u3-l3）：`AttentionEngine` 按 `backend` 分流，tl 路径再按 `qkv_meta` 形状分发到 5 个 `lower_*` 函数。

如果上述任何一点陌生，建议先回看对应讲义。本讲所有结论都会**引用真实源码行号**，不靠记忆、不编造。

## 3. 本讲源码地图

本讲是一篇「总结 + 导航」型讲义，引用的文件横跨文档与核心代码：

| 文件 | 在本讲的作用 |
| --- | --- |
| `README.md` | 声明的测试设备（含 AMD MI250 待办）与四条 Roadmap |
| `docs/API.md` | Upcoming Features：varlen/blocksparse、OnlineFunc.combine、解码 mask_mod |
| `attn_script/Readme.md` | 前端用法说明，以及明确写出的两条 Limitation |
| `attention_engine/core/transform/graph.py` | 底层 Node IR，文件首行 TODO 与大量 `_backward` 未实现 |
| `attention_engine/core/transform/core.py` | 用户层 `SymbolScalar._backward`，决定 score_mod 反向能支持哪些算子 |
| `attention_engine/attn_engine/attn_engine.py` | `_select_lower_template` 形状分发，是评估「新场景」落点的关键 |
| `attention_engine/core/lower/lower.py` | `lower_tl` 主编排；反向 autotuner 的 TODO |
| `attention_engine/autotuner/decider.py` | 静态配置空间枚举（已就绪但未接线） |

## 4. 核心概念与源码讲解

### 4.1 能力边界与限制

#### 4.1.1 概念说明

AttentionEngine 是一个「编译式注意力框架」，因此它的能力边界不是一句「支持 / 不支持某种注意力」能概括的，而要沿**三个正交维度**分别度量：

1. **表达力边界（expressiveness）**：用户写出的 Python 描述，哪些能被编译器**正确地**翻译成前向 + 反向 device 代码。这里的关键瓶颈是手写自动微分支持的算子集合——一个表达式即便前向能跑，只要它用的算子没实现 `_backward`，反向就会失败。
2. **场景边界（scene coverage）**：哪些输入形状 / 计算结构被覆盖。例如训练（MHA/GQA）、解码（MHA/GQA/MLA decode）、变长（varlen）、稀疏掩码（blocksparse）等。
3. **平台边界（platform）**：哪些 GPU 厂商被测试验证。目前只有 NVIDIA H100，AMD 仍在 roadmap。

这三条边界**彼此独立**：一个注意力可能在 tl 后端训练场景下前后向都正确，却在 cute 后端没有反向；也可能前向在所有场景都对，反向却只覆盖 q/k/v 梯度。把它们分开看，才不会笼统地误判「这个框架能不能用」。

#### 4.1.2 核心流程

把三个维度映射到代码位置：

```
表达力边界
  └─ online_func 不参与自动微分 → 用户必须手写 fwd/bwd 四段方法
  └─ score_mod 反向走 SymbolScalar._backward → 仅支持 {Add,Mul,Div,Tanh,Max,Log}
  └─ 反向只算 q/k/v 梯度，不算 custom input 张量梯度

场景边界
  └─ 训练 MHA / GQA        → lower.py / lower_gqa.py        （前后向齐全）
  └─ 解码 MHA / GQA / MLA  → lower_decode*.py                （反向多为 TODO）
  └─ CuTe 后端              → lower_cute.py                   （反向在 roadmap）

平台边界
  └─ NVIDIA H100            （已验证）
  └─ AMD MI250              （roadmap）
```

下面逐条用源码佐证。

#### 4.1.3 源码精读

**(1) online_func 不支持自动微分，需手写 bwd。** 这是最根本的一条限制，由前端文档明确写出：

[attn_script/Readme.md:94-96](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attn_script/Readme.md#L94-L96) —— 说明 `Online_func does not support autodiff, so user need to define the fwd and bwd for online function.`，即在线算法的前向（`online_fwd`/`online_fwd_epilogue`）与反向（`forward`/`backward`）四段方法必须由用户手写，框架不会对它求导。这正是 u2-l6、u5-l5 反复强调「四段方法签名由基类固定、逐元素路线退化为恒等」的根源。

**(2) 反向只算 q/k/v 梯度。** 同一段文档紧接着写：

[attn_script/Readme.md:96](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attn_script/Readme.md#L96) —— `for backward, only the grad of q, k, v is computed, not including custom input tensor.`。也就是说，你在 `CustomIO` 里声明的可学习张量（如 softmax_bias）**拿不到梯度**。如果你的注意力需要端到端训练 bias，目前必须在框架外自行处理。

**(3) score_mod 反向的算子边界。** 这是表达力边界里最容易踩坑的一条。`score_mod` 的反向由 `SymbolScalar._backward` 手写，它只显式处理了 6 种算子类型：

[attention_engine/core/transform/core.py:126-195](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/transform/core.py#L126-L195) —— `_backward` 用一串 `if self.code.type == ...` 分支处理 `Add`（L129）、`Mul`（L140）、`Div`（L152）、`Tanh`（L165）、`Max`（L174）、`Log`（L187），其余一律落入 `else` 抛出 `NotImplementedError`（L194）。

这意味着：用户在 `score_mod` 里若用了 `exp`、`sigmoid`（直接用 `exp`）、`abs`、减法 `a-b` 等，反向会直接报错。因此前述例子才要用 tanh 等价地表达 sigmoid、用加法改写减法。注意 `Sub` 在用户层并未单独实现反向——`__sub__` 挂的是 `Sub` 节点，会落入 else 分支。

更底层地，`graph.py` 的 `Node._backward` 覆盖面更窄，文件第一行就挂了 TODO：

[attention_engine/core/transform/graph.py:1-9](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/transform/graph.py#L1-L9) —— 第 1 行 `# TODO: implement more op bwd`，第 8-9 行基类 `_backward` 直接 `raise NotImplementedError`。

[attention_engine/core/transform/graph.py:123-182](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/transform/graph.py#L123-L182) —— `Exp`/`Exp2`/`Log`/`Log2`/`Tanh`/`Abs`/`Max` 等算子的 `Node._backward` 全部 `raise NotImplementedError`；只有 `Add`/`Mul`/`Neg`/`Div` 真正实现了反向（见 L53-L122）。两层 IR 的可微集合不同，是因为用户实际触发的是 `SymbolScalar._backward`（core.py），而 `Node._backward`（graph.py）目前主要服务线性注意力的部分路径。

**(4) CuTe 后端反向仍在 roadmap。** README 的 Roadmap 第一条就是：

[README.md:166-170](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/README.md#L166-L170) —— 四条 roadmap，其中 `Support backward on CuTe backend` 排在首位。CuTe 后端当前只覆盖前向（见 u5-l1），反向模板文件 `flash_bwd*.cu/.h` 虽然存在于模板目录，但 `lower_cute` 的降级产物尚未完整对接反向（`lower_cute` 在 `online_func` 缺省时会清空 bwd 相关字段）。

**(5) AMD 平台待验证。**

[README.md:5-9](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/README.md#L5-L9) —— Tested Devices 列出 NVIDIA H100 已验证，AMD MI250 标注 `(TODO)`。

#### 4.1.4 代码实践

**实践目标**：亲手量化「表达力边界」到底有多窄，建立对反向可用算子集合的精确认识。

**操作步骤**：

1. 在仓库根目录用只读检索统计 `graph.py` 里有多少算子的 `_backward` 抛出 `NotImplementedError`，对照实现了反向的算子，列出两个集合。
2. 打开 `core.py`，把 `SymbolScalar._backward`（L126-L195）里每个 `if self.code.type ==` 分支摘出来，确认用户层真正可微的算子集合。
3. 对照 `attn_script/` 下的几个 score_mod（mha 的 `score * softmax_scale`、sigmoidattn 的 `tanh` 写法），验证它们用到的算子都落在可微集合内。

**需要观察的现象**：`graph.py` 的可微集合（Add/Mul/Neg/Div）比 `core.py` 用户层可微集合（Add/Mul/Div/Tanh/Max/Log）更小；sigmoidattn 用 `tanh` 而非 `exp`，正是因为 `Exp` 不在用户层可微集合里。

**预期结果**：你能画出一张「算子 → 前向支持 → SymbolScalar 反向支持 → Node 反向支持」的三列对照表。

**待本地验证**：若要确认某个具体算子触发哪一层的 NotImplementedError，可在 Python 里构造对应 `SymbolScalar` 表达式并调用 `.backward()`，观察报错信息（参考 u2-l2、u5-l6 的调试法）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `sigmoidattn.py` 的 score_mod 要用 `tanh` 等价地写 sigmoid，而不是直接 `(score).exp() / (1+(score).exp())`？

**答案**：因为 `Exp` 在 `SymbolScalar._backward` 中未实现（落入 L194 的 `else` 分支抛 `NotImplementedError`），而 `tanh` 是已实现的（L165）。sigmoid 与 tanh 存在恒等变换 `sigmoid(x) = (1+tanh(x/2))/2`，故可用可微的 tanh 等价表达。

**练习 2**：在线性注意力里，若某 `q_mod` 用到了 `abs()`，它的反向能否由框架自动生成？为什么？

**答案**：不能。`Abs` 在 `SymbolScalar._backward`（core.py）与 `Node._backward`（graph.py）中均未实现反向，会抛 `NotImplementedError`。`get_reduce("abssum")` 这种规约虽然前向有 `ReduceAbsSum` 节点，但其反向同样未实现。

---

### 4.2 roadmap 与 TODO

#### 4.2.1 概念说明

AttentionEngine 的「待办事项」分布在**两个层面**，读者要学会区分：

1. **声明的 roadmap**：写在 `README.md` 末尾与 `docs/API.md` 的 Upcoming Features，是面向用户的承诺清单。它代表「作者希望做到的事」。
2. **代码里的 TODO 注释**：散落在 `lower*.py`、`core.py`、`graph.py` 等文件中，是面向开发者的实现债务。它代表「作者写到一半、知道还没做完的事」。

这两者**并不总是一致**：有的 roadmap 项实际已经部分实现（文档落后于代码），有的 TODO 则不在任何 roadmap 里。判断一个功能的真实状态，唯一可靠的方法是**到代码里验证**，而不是只读 checklist。这是本节最重要的方法论。

#### 4.2.2 核心流程

把两份清单并列：

```
README Roadmap（面向用户）
  ├─ Support backward on CuTe backend
  ├─ Support decoding shape
  ├─ Support more sparse mask pattern
  └─ Support AMD MI250

API Upcoming Features（面向用户的接口演进）
  ├─ AttentionLibrary 高层 API（按名字取现成注意力）
  ├─ varlen / block-sparse mask / block-sparse indices
  ├─ OnlineFunc.combine（combine kernel 的用户层抽象）
  └─ decoding 的 mask_mod（带 custom_fwd_inputs 偏移）

代码 TODO（面向开发者，零散）
  ├─ graph.py:1      "implement more op bwd"
  ├─ lower.py:664    "Bwd config(TODO: autotuner bwd)"
  ├─ core.py:504     "TODO: sparse block"
  └─ lower_decode*.py 多处 "bwd: TODO" 等
```

#### 4.2.3 源码精读

**(1) README Roadmap 的四条。**

[README.md:166-170](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/README.md#L166-L170) —— 这是官方对外承诺的四个方向。

**(2) API Upcoming Features 的接口演进。**

[docs/API.md:111-156](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/docs/API.md#L111-L156) —— 其中最具结构性的是三块：高层 `AttentionLibrary` API（L113-L121，按 `attn_type`/`mask_type`/`use_types` 取现成注意力）；varlen 与 blocksparse 的运行期参数 `cu_len_q`/`block_sparse_mask`/`block_sparse_indices`（L124-L139）；以及 `OnlineFunc.combine` 方法（L140-L148）和解码期带偏移的 `mask_mod`（L149-L155）。

**(3) 关键观察：roadmap 会落后于代码。** 这是最容易被忽略、却最实用的一点。以「Support decoding shape」为例，README 把它列为未完成项，但引擎其实已经能按形状自动分发到三条解码路径：

[attention_engine/attn_engine/attn_engine.py:218-332](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/attn_engine.py#L218-L332) —— `_select_lower_template` 已实现 MLA 解码（L235-L250）、GQA 解码（L252-L271）、MHA 解码（L273-L290）三条分支。这说明「解码」至少在前向层面已经落地，README 的 checklist 并未同步更新。

同理，`OnlineFunc.combine` 在 API 里是「Upcoming」，但 MLA 解码的 combine kernel 已经在用（见 u4-l4、u5-l2）。所以读 roadmap 时要心里有数：**打勾与否不完全等于代码有没有**。

**(4) 代码 TODO 是更细颗粒的债务。** 例如反向的 autotuner 尚未接线：

[attention_engine/core/lower/lower.py:664](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/lower/lower.py#L664) —— `# Bwd config(TODO: autotuner bwd)`，前向已接 `tune`/`tune_file`（见 L641-L645 的 `TunnerOutput`），反向仍是写死的默认 block 尺寸。

又如稀疏掩码的扩展点：

[attention_engine/core/transform/core.py:504](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/transform/core.py#L504) —— `# TODO: sparse block`，标注了 blocksparse 进一步扩展的留白位置。

#### 4.2.4 代码实践

**实践目标**：建立一张「roadmap 项 → 实际代码状态」的对照表，学会用代码而不是文档判断功能成熟度。

**操作步骤**：

1. 列出 README 四条 roadmap。
2. 对每一条，用检索（`git ls-files`、`Grep`）在 `attention_engine/` 下找对应的实现证据：
   - "decoding shape" → 搜 `lower_decode`，确认有 3 个解码降级文件；
   - "sparse mask" → 搜 `BlockAttn`/`blockattn`/`block_mask`，确认 blocksparse 模板存在；
   - "CuTe backward" → 搜 `lower_cute` 与 `flash_bwd`，确认模板文件在但降级未完整对接；
   - "AMD MI250" → 搜 `arch/`，确认只有 H100/A100/RTX4090，无 AMD arch。
3. 据此给每条打上「已实现 / 部分实现 / 未开始」三档。

**需要观察的现象**：「decoding」与「sparse mask」会落在「部分实现」——前向在、反向或某些模式待补；「CuTe backward」与「AMD」基本是「未开始 / 仅前向」。

**预期结果**：得到一张区分文档承诺与代码现实的表，避免把 roadmap 当功能列表误用。

#### 4.2.5 小练习与答案

**练习 1**：README 把「Support decoding shape」列为未完成，但代码里已有 `lower_decode.py` / `lower_decode_gqa.py` / `lower_decode_mla.py`。如何解释这个矛盾？

**答案**：roadmap 描述的是「完整支持」的目标（含反向、含所有掩码模式），而代码目前实现了前向与部分场景。文档的 checklist 没有随前向落地而更新，导致「部分实现」被笼统地仍挂在未完成列表里。判断真实状态应以代码为准。

**练习 2**：`docs/API.md` 里 `OnlineFunc.combine` 标为 Upcoming，但它是否已经在某条代码路径中被使用？

**答案**：是。MLA 解码（`lower_decode_mla` + CuTe kv_shared 路径）的 combine kernel 已经实现了沿 `num_split` 维的 log-sum-exp 合并（见 u4-l4、u5-l2）。用户层的 `OnlineSoftmax.combine` 与手写 macro 数学等价。所以它属于「代码已用、API 抽象待正式化」的状态。

---

### 4.3 二次开发切入点

#### 4.3.1 概念说明

要扩展 AttentionEngine，关键是判断「这个新需求会落到编译链的哪一层」。我们用一个**层级责任心智模型**来决策：

- **新算子 / 新的可微性** → 落在 IR 层（`transform/graph.py` + `transform/core.py`）与 codegen 层（`codegen/tl_gen.py`）。
- **新场景（新形状关系）** → 落在 lower 层（新增/修改 `lower_*.py`）+ template 层（新增模板）+ 引擎分发（`attn_engine.py`）。
- **新后端** → 落在 codegen 层（新增 `to_xxx_op` 发射器）+ template 层（新模板目录）+ 引擎 `backend` 分流。
- **新的自动调优 / 硬件** → 落在 autotuner 层（`arch/` + `decider.py`）与 lower 层的接线。

这个模型的好处是：**改动的半径是可预测的**。加一个算子的反向，通常只动两个文件；加一个新后端，则要横跨四层。先评估半径，再决定值不值得做。

#### 4.3.2 核心流程

决策表（功能类型 → 涉及层 → 代表文件）：

| 想做的事 | IR 层 | codegen 层 | lower 层 | template 层 | 引擎/其它 |
| --- | --- | --- | --- | --- | --- |
| 为 `Sub`/`Exp` 补反向 | graph.py + core.py | tl_gen.py（发射） | — | — | — |
| 新增 varlen 场景 | — | — | 新 lower_varlen.py | 新模板 | attn_engine 分发 |
| 新稀疏 mask + blocksparse | core.py（infer_mask） | — | lower.py | blockattn 模板 | — |
| 新后端（如 triton 直发） | — | 新 to_xxx_op | 新 lower_xxx.py | 新模板目录 | backend 分流 |
| 接线静态 autotuner | — | — | lower.py（解注释） | — | decider.py + arch/ |
| 新硬件（AMD） | — | — | — | — | arch/ 新增类 |

#### 4.3.3 源码精读

**(1) 加一个新算子的反向：照葫芦画瓢。** 以「为 `Sub` 实现反向」为例，参照已有 `Add` 的写法。用户层入口在 `SymbolScalar._backward`：

[attention_engine/core/transform/core.py:126-151](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/transform/core.py#L126-L151) —— `Add` 的反向是把上游梯度 `grad` 原样分发给两个操作数（L129-L139），多路径用 `+` 累加。`Sub = a - b` 的导数是 `da=grad, db=-grad`，因此只需在 `_backward` 里加一个 `elif self.code.type == "Sub"` 分支，照 `Add` 的结构写，再把 `b` 的梯度取负即可。底层 `graph.py` 的 `Node._backward` 也要同步实现（当前 L102-L103 是 `raise NotImplementedError`）。注意梯度本身是符号 `SymbolScalar` 子树，取负用重载的 `__neg__`，无需数值计算。

**(2) 新稀疏 mask + blocksparse：从判定函数入手。** 模板选择由 `infer_mask` 三分支决定（见 u2-l8）：

[attention_engine/core/transform/core.py:504](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/transform/core.py#L504) —— `# TODO: sparse block` 标注了 blocksparse 进一步扩展的留白。新增一种稀疏模式（如 sliding window、block-diagonal）通常需要：在 `core.py` 增加对应的判定函数（类似 `is_causal_mask`/`is_less_causal_mask`）、在 `lower_tl` 的 `infer_mask` 增加分支、确认 `TlBlockAttnTemplate` 能消费对应的 `block_mask`。前向往往不难，难点在反向对掩码边界的处理与正确性对齐。

**(3) 接线静态 autotuner：解开已有但休眠的逻辑。** `decider.py` 已经能枚举合法配置：

[attention_engine/autotuner/decider.py:40-128](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/autotuner/decider.py#L40-L128) —— `decider` 笛卡尔枚举 `block_M/N/K`、`num_threads`、`stages`，经 shared_mem / reg_cap / register_per_thread 三道上限过滤（L98-L99），并可独立运行验证（见 `__main__` L131-L143）。但目前它**没有**被 `lower.py` 调用——真正在跑的是运行期 `tune`/`tune_file`（[lower.py:641-645](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/lower/lower.py#L641-L645)）。把静态 `decider` 接到编译期、用硬件建模预过滤配置空间，是一个边界清晰、风险可控的改进点。

#### 4.3.4 代码实践

**实践目标**：从 roadmap 中选一项，写出完整的实现思路（涉及哪些文件、改哪一层、如何测试对齐），而不实际改源码。

**操作步骤（以「为 `Sub` 算子补反向」为例）**：

1. **定位**：确认 `Sub` 在用户层与底层都缺反向——`core.py` 的 `_backward` 无 `Sub` 分支（落入 L194 else），`graph.py:102-103` 直接 `raise NotImplementedError`。
2. **推导导数**：对 `out = a - b`，`dout/da = 1`、`dout/db = -1`，故 `grad_a = grad`、`grad_b = -grad`。
3. **写实现**：在 `core.py` 加 `elif self.code.type == "Sub"` 分支，参照 `Add`（L129-L139）的累加结构，`b` 的梯度用 `grad.__neg__()` 或 `-grad`；在 `graph.py` 的 `Sub._backward` 里同样补上符号梯度（用 `Neg` 节点表达负号）。
4. **设计对齐测试**：构造 `score_mod = lambda score, ...: score - bias`（bias 为 Const），调用 `SymbolScalar.backward` 得到 `dscores`，与 PyTorch autograd 对同一表达式的数值梯度比对（rtol/atol），方法沿用 u5-l4 的 `check_close`。
5. **回归**：确保现有 `sigmoidattn`/`reluattn` 等例子仍通过（它们目前回避了 `Sub`，补全后不应受影响）。

**需要观察的现象**：补全后，含减法的 `score_mod` 不再抛 `NotImplementedError`，且反向数值与 PyTorch 参考一致。

**预期结果**：得到一份「两文件改动 + 一对齐测试」的最小实现计划。

**待本地验证**：实际数值对齐需在有 TileLang/CUDA 的环境里跑（参考 u1-l2 的环境配置）。

> 示例代码（非项目原有，仅示意 `Sub` 反向的写法）：
> ```python
> # 在 SymbolScalar._backward 中，参照 Add 分支新增：
> elif self.code.type == "Sub":
>     if self.prev[0].require_grad:
>         grad0 = copy(grad)
>         if self.prev[0].grad:
>             grad0 = grad0 + self.prev[0].grad
>         self.prev[0].grad = grad0
>     if self.prev[1].require_grad:
>         grad1 = -grad               # b 的梯度取负
>         if self.prev[1].grad:
>             grad1 = grad1 + self.prev[1].grad
>         self.prev[1].grad = grad1
> ```

#### 4.3.5 小练习与答案

**练习 1**：如果我想新增一个 Triton 直发后端（绕过 TileLang），按层级责任模型，最少要动哪几层？

**答案**：至少三层——codegen 层（新增 `to_triton_op` 发射器，把符号 DAG 翻译成 Triton 代码）、template 层（新增 Triton 模板目录）、引擎层（在 `attn_engine.py` 的 `backend` 分流里加 `elif backend == "triton"`）。IR 层（transform）可不动，因为符号 DAG 与目标语言解耦（见 u2-l4）。

**练习 2**：为什么「接线静态 autotuner」被认为是一个「边界清晰、风险可控」的改进点？

**答案**：因为 `decider.py` 的枚举与过滤逻辑已完整可运行（有 `__main__` 自测），只是尚未被 `lower.py` 调用。改动半径仅限 lower 层的接线（把 `decider(...)` 的结果喂给 `TunnerOutput`），不涉及 IR 与模板，且可用现有 benchmark（u5-l4）验证性能不回退。属于典型的「最后一公里」接线工作。

---

## 5. 综合实践

**任务**：充当一次「AttentionEngine 贡献者」，从 roadmap 中认领一项，产出一份可评审的实现方案。

请从以下三项中任选其一，按统一模板交付（**只写方案，不实际改源码**）：

1. **为新算子补反向**：为 `Exp` 或 `Sub` 在 `core.py` + `graph.py` 实现手写 `_backward`。
2. **为新稀疏 mask 适配 blocksparse**：实现一个 sliding-window 或 block-diagonal 的 `mask_mod`，让它正确走 `TlBlockAttnTemplate`。
3. **接线静态 autotuner**：让 `decider.py` 的配置空间在编译期被 `lower.py` 消费，作为运行期 `tune` 的预过滤。

**交付模板**：

1. **需求陈述**：一句话说明要解决什么、对应 roadmap/TODO 的哪一条。
2. **涉及文件清单**：列出要改的文件与每个文件的改动性质（新增函数 / 修改分支 / 新增模板）。
3. **分层落点**：用 4.3.2 的决策表标注改动落在 IR / codegen / lower / template / 引擎的哪几层。
4. **关键推导**：写出必要的数学（如算子导数）或控制流（如新的分发条件）。
5. **对齐测试方案**：选择哪个参考实现（flash-attn / torch einsum / fla）、用哪个 `do_bench_*` 或 `check_close` 验证、rtol/atol 取多少。
6. **风险与回归**：列出可能影响的现有例子（mha/sigmoidattn/reluattn 等），说明如何保证不回退。

**评判标准**：方案应能让另一位开发者照着直接动手——文件清单准确、分层正确、对齐测试可执行。完成后，你已具备向本仓库提 PR 的前置认识。

## 6. 本讲小结

- AttentionEngine 的能力边界要沿**表达力 / 场景 / 平台**三个正交维度分别度量，不能笼统判断。
- `online_func` 不支持自动微分，四段方法须手写；反向**只算 q/k/v 梯度**，不算 custom input 张量——这两条由 `attn_script/Readme.md` 明确写定。
- `score_mod` 反向受 `SymbolScalar._backward` 的算子集合约束，目前仅支持 `{Add, Mul, Div, Tanh, Max, Log}`，其余抛 `NotImplementedError`；底层 `graph.py` 覆盖面更窄，首行挂着 `# TODO: implement more op bwd`。
- 待办分两层：README/API 的**声明 roadmap** 与源码里零散的 **TODO 注释**；二者不一致，且 roadmap 会**落后于代码**（解码、combine 已部分落地却仍挂在未完成列表）。
- 二次开发用「层级责任模型」决策：新算子→IR，新场景→lower+template+引擎，新后端→codegen+template+引擎，新硬件/调优→autotuner。
- 静态 autotuner（`decider.py`）逻辑已就绪但未接线，是边界清晰的改进切入点。

## 7. 下一步学习建议

本讲是学习路线的终点，但不是学习的终点。建议按兴趣选择方向继续深入：

1. **想贡献算子 / 修复反向**：重读 u2-l1（Node 图）、u2-l2（SymbolScalar）、u2-l5（score_mod 降级），再回到本讲 4.3 的实践，从 `Sub`/`Exp` 的反向入手提第一个 PR。
2. **想做新场景 / 新后端**：重读 u3-l2（lower_tl 全链路）、u3-l3（引擎分发）、u5-l1（CuTe 后端），对照本讲的决策表，评估 varlen 或 Triton 后端的工作量。
3. **想做性能 / 自动调优**：重读 u5-l3（autotuner 与硬件建模），尝试把 `decider.py` 接入编译期，并用 u5-l4 的 benchmark 验证收益。
4. **想理解设计哲学**：把全册 25 讲连起来读一遍，重点体会「前端符号描述 ↔ 后端代码生成」的解耦——这套编译思路不仅适用于注意力，也是你设计其它「DSL → kernel」系统的参考范式。

无论选哪条路，记住本讲反复强调的一条方法论：**判断功能的真实状态，永远以代码为准，而不是以文档 checklist 为准。**
