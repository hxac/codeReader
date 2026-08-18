# 为 EPLB 编写正确性测试

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 EPLB 放置方案必须满足的**不变量清单**（形状、值域、守恒、覆盖、互逆、布局、组-节点对齐），并能追溯到每条不变量对应的源码依据。
2. 用 `torch` 的 `bincount`、`nonzero`、布尔掩码、`scatter` 等原语，把每条不变量写成**可执行的断言**。
3. 构造一个**参数矩阵**，让它同时覆盖 hierarchical 与 global 两条策略分支、`groups_per_pack == 1` 等平凡分支、`num_replicas == num_logical_experts` 无冗余分支，以及非法参数触发的 `AssertionError` 分支。
4. 理解「结构正确性」与「负载均衡质量」是两类不同的问题——本讲只测前者，后者留给下一讲（u3-l2）。

本讲的最终产出是一个完整的 `test_eplb.py`（示例代码，仓库本身并不包含它）。

## 2. 前置知识

### 2.1 什么是不变量（invariant）

**不变量**是指：对任意合法输入，程序的输出都必须满足的结构性质。它与「输出具体是什么值」无关。

以 EPLB 为例，`rebalance_experts` 返回的是一个「放置方案」：哪些逻辑专家被复制了几个副本、每个副本放在哪张 GPU 上。这个方案**必须**满足一些结构约束（比如每个逻辑专家至少有一个副本），但**不必须**是全局最优的（README 明确说这是启发式）。所以：

- **结构约束** → 用不变量断言测试（本讲）。
- **均衡质量** → 用指标评估（下一讲 u3-l2）。

把两者分开，测试才不会脆弱：贪心算法在权重并列（tie）时的选择可能因 PyTorch 版本而异，但无论怎么并列，结构不变量都必须成立。

### 2.2 为什么 EPLB 特别适合不变量测试

回顾示意图（u1-l4 已建立的四函数地图）：

```
rebalance_experts (入口: 规范化 + 分派 + 组装逆映射)
 └─ rebalance_experts_hierarchical (三步主流程)
     ├─ balanced_packing  (组 → 节点)
     ├─ replicate_experts (节点内复制)
     └─ balanced_packing  (物理专家 → GPU)
```

整条链路是纯函数式的张量变换：输入只有 `weight` 和 4 个整数参数，输出只有 3 个张量，没有 IO、没有随机数、没有隐藏状态。这意味着：

- 同一输入必得同一输出（可做金标准回归测试）。
- 所有正确性要求都能表达成「输出张量之间的代数关系」（可做不变量测试）。

### 2.3 本讲用到的 torch 测试工具箱

| 工具 | 用途 |
|---|---|
| `torch.bincount(x, minlength=N)` | 统计一维整数张量中每个值的出现次数，本讲用来数副本 |
| `tensor.nonzero()` | 找出满足条件（非零/True）的下标，本讲用来定位槽位 |
| 布尔掩码 `mask[x]` | 按另一张量的值查表，如「槽位上的专家是否属于组 g」 |
| `torch.equal(a, b)` | 严格相等（含 dtype 与 shape），比 `(a == b).all()` 更严格 |
| `assert` / `pytest.raises(AssertionError)` | 断言与异常断言 |

### 2.4 运行测试的两种方式

仓库没有 `pyproject.toml`、`setup.py`，也没有声明 pytest 依赖，唯一的第三方依赖是 torch（见 u1-l3）。因此本讲的 `test_eplb.py` 设计为**两种方式都能跑**：

- 有 pytest：`python -m pytest test_eplb.py -v`
- 没有 pytest：直接 `python test_eplb.py`，用 `try/except AssertionError` 替代 `pytest.raises`。

### 2.5 一个关键区分：每 GPU「专家数」相等 ≠ 每 GPU「负载」均衡

`phy_experts_per_gpu = num_physical_experts // num_gpus`（[eplb.py:L96](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L96)）保证每张 GPU 的**槽位数**相同——这是布局契约，由返回张量的形状自动满足。但每张 GPU 分到的**负载**是否均衡，取决于贪心算法的质量，需要拿 `weight` 去算。本讲测前者并为后者铺路。

## 3. 本讲源码地图

| 文件 | 本讲关注点 |
|---|---|
| [eplb.py](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py) | 核心被测对象。重点阅读入口 `rebalance_experts`（L131-162）与 `rebalance_experts_hierarchical` 的断言（L89-96）、三步主流程（L103-129）、两个被复用的子函数 `balanced_packing`（L5-41）、`replicate_experts`（L44-71） |
| [README.md](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/README.md) | 两策略说明（L15-31）与 12 专家示例及其确定输出（L39-57），后者充当「金标准」回归数据 |

**注意**：仓库中没有 `tests/` 目录，`test_eplb.py` 是本讲要新建的文件（示例代码），属于你自己的实践产物，不会改动仓库源码。

## 4. 核心概念与源码讲解

本讲围绕两个最小模块展开：`rebalance_experts`（入口契约与逆映射组装）和 `rebalance_experts_hierarchical`（层级策略的结构保证）。按「推导不变量 → 实现断言 → 覆盖分支」三步走。

### 4.1 从契约到不变量清单：测试设计的源头

#### 4.1.1 概念说明

写测试之前先问：**从哪里知道程序「应该」做什么？** 三个来源，可靠性递增：

1. **文档契约**：docstring 写明的参数、返回形状与约束。
2. **前置断言**：代码里显式 `assert` 的合法输入范围——它们圈定了不变量必须成立的定义域。
3. **数据流推导**：顺着代码读出「哪些量守恒」「哪些表互逆」「哪些编码保证了对齐」。

EPLB 的特殊之处在于：它的输出是一组**互相关联的映射表**（`phy2log`、`log2phy`、`logcnt`），所以最有力的不变量恰恰是表与表之间的代数关系，而不是任何一张表的具体取值。

#### 4.1.2 核心流程

推导不变量清单的流程：

```text
读函数签名与 docstring
        │
        ▼
读前置断言（合法参数域）─────────► 导出「非法参数必须报错」的负向测试
        │
        ▼
读主数据流（replicate → packing → 映射链复合）
        │
        ▼
回答三个问题：
  ① 什么量守恒？        （副本总数）
  ② 哪些表互为逆？      （phy2log ↔ log2phy/logcnt）
  ③ 哪种编码承诺了对齐？（槽位布局、组-节点对齐）
        │
        ▼
得到不变量清单 INV-1 ~ INV-7
```

本讲推导出的清单（后两小节逐条实现）：

| 编号 | 名称 | 数学表述 | 源码依据 |
|---|---|---|---|
| INV-1 | 形状契约 | `phy2log ∈ [L,M]`，`logcnt ∈ [L,E]`，`log2phy ∈ [L,E,c_max]` | 入口 docstring 与返回值组装 |
| INV-2 | 值域 | \( 0 \le \text{phy2log}[l,s] < E \) | `replicate_experts` 只写合法专家 id |
| INV-3 | 守恒 | \( \sum_e \text{logcnt}[l,e] = M,\ \forall l \) | 复制循环每轮恰好 +1 |
| INV-4 | 覆盖与计数一致 | \( \text{logcnt}[l,e] = \#\{s : \text{phy2log}[l,s]=e\} \ge 1 \) | 初始身份映射 + 循环自增 |
| INV-5 | 互逆一致 | `log2phy[l,e,0:c]` 与 `phy2log[l]` 中 e 的槽位集合双向一致；`log2phy[l,e,c:] ≡ -1` | 入口 scatter 组装 |
| INV-6 | 布局契约 | 槽位 s 可重构为 `[节点][节点内GPU][卡内序号]`；每 GPU 恰 \( M/P \) 个槽位；层级策略下同一专家的副本不跨节点 | `phy2pphy` 编码与 `view/flatten` |
| INV-7 | 组-节点对齐（仅层级） | 同组专家的全部副本落在同一节点；每节点恰 \( G/N \) 个组 | `log2mlog` 编码 + 装箱基数约束 |

其中 \( L \) 为层数、\( E \) 为逻辑专家数（`weight.shape[1]`）、\( M \) 为物理专家总数（`num_replicas`）、\( P \) 为 GPU 数、\( G \) 为组数、\( N \) 为节点数、\( c = \text{logcnt}[l,e] \)、\( c_{\max} = \max_e \text{logcnt} \)。

#### 4.1.3 源码精读

**入口的文档契约。** [eplb.py:L133-L147](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L133-L147) 是 `rebalance_experts` 的 docstring：它写明了输入 `weight: [layers, num_logical_experts]`、`num_replicas` 必须是 `num_gpus` 的倍数、三个返回值分别是 `[layers, num_replicas]`、`[layers, num_logical_experts, X]`、`[layers, num_logical_experts]`。注意 docstring 对 `log2phy` 第三维只写了 `X`——它到底是什么，需要读代码：

**第三维的真身。** [eplb.py:L157-L159](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L157-L159) 中 `maxlogcnt = logcnt.max().item()`，随后 `log2phy` 的第三维就是 `maxlogcnt`。这直接给出 INV-1 中最容易被忽略的一条断言。

**输入规范化。** [eplb.py:L148-L149](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L148-L149) 先取形状再把 `weight.float().cpu()`。测试意义：**传入 int 张量也合法**（会被转成 float），且所有输出都在 CPU 上——断言不需要关心输入设备。

**策略分派。** [eplb.py:L150-L156](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L150-L156) 按 `num_groups % num_nodes == 0` 分派：能整除走层级策略原参数；不能整除以退化参数 `(num_replicas, 1, 1, num_gpus)` 复用同一实现（即 global 策略，见 u2-l6）。测试意义：**分派条件本身是被测对象**——同一组不变量要在两条分支上都成立，且 global 分支下「节点」退化为 1，INV-7 应当跳过。

**层级函数的前置断言。** [eplb.py:L89-L96](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L89-L96) 依次断言 `E % G == 0`、`G % N == 0`、`P % N == 0`、`M % P == 0`，并算出 `phy_experts_per_gpu = M // P`。这四条就是「非法参数负向测试」的规格书——违反它们必须抛 `AssertionError`。注意还有一个**隐性约束** `M >= E` 不在这四条里，它由 `replicate_experts` 内部的断言把守（见 4.2.3）。

#### 4.1.4 代码实践

**实践 1：人肉验证不变量（跑通再谈测试）**

1. **实践目标**：在写任何断言之前，先对 README 示例人眼确认 INV-3、INV-4、INV-5 各成立一次，建立「不变量是具体可见的」的直觉。
2. **操作步骤**：
   - 运行 README 示例（u1-l3 已搭建环境）：

     ```python
     import torch
     import eplb
     weight = torch.tensor([[ 90, 132,  40,  61, 104, 165,  39,   4,  73,  56, 183,  86],
                            [ 20, 107, 104,  64,  19, 197, 187, 157, 172,  86,  16,  27]])
     phy2log, log2phy, logcnt = eplb.rebalance_experts(weight, 16, 4, 2, 8)
     print(phy2log.tolist()); print(log2phy.tolist()); print(logcnt.tolist())
     ```

   - 手工数一数：`phy2log` 第 0 行里 id=5 出现几次？与 `logcnt[0, 5]` 是否一致（INV-4）？
   - 把 `logcnt[0]` 全部加起来，是否恰好等于 16（INV-3）？
   - 找一个 `logcnt` 值为 2 的专家 e，检查 `log2phy[0, e]` 是否形如 `[某槽位, 另一槽位]`，且这两个槽位上的 `phy2log[0]` 都等于 e（INV-5）。
3. **需要观察的现象**：README 第 0 行输出 `[5, 6, 5, 7, 8, 4, 3, 4, 10, 9, 10, 2, 0, 1, 11, 1]`（[README.md:L55-L56](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/README.md#L55-L56)）中，5、4、10、1 各出现 2 次，其余各 1 次——共 4 个被复制的专家，对应每层 \( M - E = 4 \) 个冗余副本。
4. **预期结果**：三层检查全部吻合；由此可推出该例 `maxlogcnt = 2`，`log2phy` 形状为 `[2, 12, 2]`。
5. 本实践在 u1-l3 环境下可直接复现；若你的 PyTorch 版本排序行为不同导致输出与 README 有差异，请以「不变量是否成立」为准继续学习——这恰好是 4.1.1 所说「不要断言具体值」的原因。

#### 4.1.5 小练习与答案

**练习 1**：证明 \( c_{\max} = \max_e \text{logcnt}[l,e] \ge \lceil M/E \rceil \)。

**答案**：反证法。若所有 \( \text{logcnt}[l,e] < M/E \)，则 \( \sum_e \text{logcnt}[l,e] < E \cdot \frac{M}{E} = M \)，与 INV-3 的 \( \sum_e \text{logcnt}[l,e] = M \) 矛盾。又因为 logcnt 是整数，最大值至少为 \( \lceil M/E \rceil \)。README 例中 \( M/E = 16/12 \approx 1.33 \)，故 \( c_{\max} \ge 2 \)；实际恰好为 2，达到下界。这条不等式可以写成第 8 条断言（对分层调用它按节点规模成立：每层每节点 \( \sum = M/N \)、\( E/N \) 个专家，下界相同）。

**练习 2**：为什么通用不变量测试**不应**断言 `phy2log` 等于某个具体张量（金标准回归除外）？

**答案**：`balanced_packing` 是贪心：当两个包的 `pack_weights` 并列时，`min(range, key=...)` 取编号较小者（[eplb.py:L34-L35](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L34-L35)）；而物品的降序来自 `sort`（[eplb.py:L27](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L27)），权重并列时的次序属于未承诺的实现细节，跨 PyTorch 版本可能不同。断言具体值会让测试对实现细节过敏；断言结构不变量则对任何正确的实现都成立——这正是属性测试（property-based testing）的思想。

**练习 3**：docstring 之外还有一条「隐式契约」：同一逻辑专家的多个副本会被赋予互不相同的 `rank`。它从哪段代码来？为什么入口不返回 `phyrank` 却仍能构造出 `log2phy`？

**答案**：`replicate_experts` 中新副本的 `rank` 取自复制前的 `logcnt`（[eplb.py:L66-L70](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L66-L70)），因此每个逻辑专家的副本 rank 恰为 \( 0, 1, \dots, c-1 \)。（逻辑编号, rank）于是构成无冲突的扁平地址 \( e \cdot c_{\max} + r \)；入口正是用 `phy2log * maxlogcnt + phyrank` 作 scatter 的 index（[eplb.py:L160-L161](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L160-L161)），一次性把槽位号写进 `log2phy`。`phyrank` 是构造逆映射的中间量，被入口消费后不再外泄——`phy2log` 与 `log2phy` 因此是一对互逆读法（u2-l6）。

### 4.2 七条核心不变量的 torch 断言实现

#### 4.2.1 概念说明

把 4.1 的清单翻译成代码，核心是三个翻译模式：

1. **计数 → `bincount`**：「`logcnt[l,e]` 等于 e 在 `phy2log[l]` 中的出现次数」一句，直接译为 `torch.bincount(phy2log[l], minlength=E)` 与 `logcnt[l]` 的 `equal`。
2. **定位 → 掩码 + `nonzero`**：「e 的所有槽位」译为 `(phy2log[l] == e).nonzero()`；「组 g 内专家占用的槽位」译为「先查表再掩码」。
3. **-1 padding → 先切有效段**：`log2phy` 的尾部是哨兵 -1，任何统计前都要按 `logcnt` 切出前缀，避免把哨兵当槽位。

#### 4.2.2 核心流程

校验器 `check_invariants` 的流程：

```text
读入 weight 与 4 个拓扑参数
  │
  ├─ 计算有效拓扑：G%N==0 ? (N, P/N) : (1, P)     # global 退化为单节点
  ├─ 调用 rebalance_experts 得三张表
  │
  ├─ INV-1 形状 ──► INV-2 值域 ──► INV-3 守恒 ──► INV-4 计数一致
  ├─ INV-5 互逆（逐层逐专家，双向 + padding）
  ├─ INV-6 布局重构 view(L, N_eff, P/N_eff, M/P) + 副本不跨节点（层级）
  └─ INV-7 组-节点对齐（仅层级）：组内槽位同节点 + 每节点恰 G/N 组
```

#### 4.2.3 源码精读

**INV-3/INV-4 的源头：复制循环。** [eplb.py:L62-L71](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L62-L71) 中 `logcnt` 初始化为全 1、`phy2log` 前 `num_log` 列是身份映射 `arange`；随后循环 `for i in range(num_log, num_phy)` 每轮把某个 `logcnt` 加 1。于是每行总和恒为 \( E' + (M' - E') = M' \)（这里 \( E', M' \) 是分层调用时的每节点规模 \( E/N, M/N \)），且每个专家至少 1 个副本——INV-3、INV-4 由此得证。注意 [eplb.py:L59-L60](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L59-L60) 的 `assert num_redundant >= 0` 是隐性约束 \( M \ge E \) 的把守者（分层下即 \( M/N \ge E/N \)）。

**INV-5 的源头：入口的 scatter。** [eplb.py:L158-L161](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L158-L161) 先用 -1 铺满 `log2phy`，再以扁平地址 `phy2log * maxlogcnt + phyrank` 一次 `scatter_` 写入槽位号。由此读出三条可测性质：有效位置的值就是物理槽位号；未写到的位置保持 -1，且恰好是每个专家第 \( c \) 个之后的槽位（rank 只有 \( 0..c-1 \)）；`phy2log[l, log2phy[l,e,r]] == e` 双向成立。

**INV-6 的源头：槽位编码。** [eplb.py:L119](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L119) 的 `phy2pphy = pack_index * phy_experts_per_gpu + rank_in_pack` 把「节点内第几张 GPU、第几个槽」编码为一个混合进制数；[eplb.py:L123-L127](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L123-L127) 再按节点拼接并 `flatten` 成最终 `[L, M]`。所以最终列号 s 的语义是：

\[ s = \underbrace{n}_{\text{节点}} \cdot \frac{M}{N_{\text{eff}}} + \underbrace{g'}_{\text{节点内GPU}} \cdot \frac{M}{P} + \underbrace{r}_{\text{卡内序号}} \]

其中 \( N_{\text{eff}} \) 是有效节点数（层级为 \( N \)，global 为 1）。这就是 `phy2log.view(L, N_eff, P // N_eff, M // P)` 这条断言的全部含义。

**INV-7 的源头：组编码 + 基数约束。** [eplb.py:L104-L108](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L104-L108) 把「组打包结果」编码进置换 `log2mlog`：同组专家获得连续的 mlog 编号段（节点内），而 `balanced_packing` 的容量断言（[eplb.py:L19-L20](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L19-L20)，每包恰 \( G/N \) 个组）保证组不跨节点。两层保证合起来即 INV-7。global 分支下 \( G=N=1 \) 退化，组约束无意义，测试必须跳过。

#### 4.2.4 代码实践

**实践 2：实现通用校验器 `check_invariants`（示例代码，待本地验证）**

1. **实践目标**：把 INV-1 ~ INV-7 写成一个可复用的校验函数，任何参数组合都能调用。
2. **操作步骤**：新建 `test_eplb.py`（放在仓库根目录或任意可 `import eplb` 的位置），写入：

   ```python
   # test_eplb.py —— EPLB 放置方案不变量测试（示例代码，非仓库原有文件）
   import torch
   import eplb


   def check_invariants(weight, num_replicas, num_groups, num_nodes, num_gpus):
       """对一个参数组合跑全部结构不变量，返回三张表供调用方继续检查。"""
       L, E = weight.shape
       M, G, N, P = num_replicas, num_groups, num_nodes, num_gpus
       hierarchical = (G % N == 0)
       N_eff = N if hierarchical else 1          # global 退化为单节点
       gpus_per_node = P // N_eff
       phy_per_gpu = M // P
       per_node_slots = M // N_eff

       phy2log, log2phy, logcnt = eplb.rebalance_experts(
           weight, num_replicas, num_groups, num_nodes, num_gpus)

       # INV-1 形状契约（log2phy 第三维恰为全局最大副本数）
       assert phy2log.shape == (L, M)
       assert logcnt.shape == (L, E)
       c_max = logcnt.max().item()
       assert log2phy.shape == (L, E, c_max)

       # INV-2 值域
       assert phy2log.min() >= 0 and phy2log.max() < E

       # INV-3 守恒：每层副本总数恒等于 M
       assert torch.equal(logcnt.sum(-1), torch.full((L,), M))

       # INV-4 覆盖与计数一致：logcnt 与 phy2log 的逐值计数完全一致
       for l in range(L):
           assert torch.equal(torch.bincount(phy2log[l], minlength=E), logcnt[l])
       assert (logcnt >= 1).all()

       # INV-5 互逆一致（含 -1 padding 恰落在尾部）
       for l in range(L):
           for e in range(E):
               c = logcnt[l, e].item()
               slots = (phy2log[l] == e).nonzero().squeeze(-1)
               assert slots.numel() == c
               filled = log2phy[l, e, :c]
               assert (filled >= 0).all()
               assert filled.sort().values.equal(slots.sort().values)
               assert (log2phy[l, e, c:] == -1).all()
               assert (phy2log[l][filled] == e).all()   # 反向读法

       # INV-6 布局契约：列号按 [节点][节点内GPU][卡内序号] 编码
       layout = phy2log.view(L, N_eff, gpus_per_node, phy_per_gpu)
       assert layout.shape == (L, N_eff, gpus_per_node, phy_per_gpu)

       if hierarchical:
           # INV-6b 同一逻辑专家的所有副本不跨节点（比 INV-7 更细的定位断言）
           for l in range(L):
               for e in range(E):
                   slots = (phy2log[l] == e).nonzero().squeeze(-1)
                   nodes = slots // per_node_slots
                   assert (nodes == nodes[0]).all(), f"层{l} 专家{e} 副本跨节点"

           # INV-7 组-节点对齐：同组同节点，且每节点恰 G/N 个组
           gs = E // G
           for l in range(L):
               group2node = []
               for g in range(G):
                   in_group = torch.zeros(E, dtype=torch.bool)
                   in_group[g * gs:(g + 1) * gs] = True
                   slot_mask = in_group[phy2log[l]]        # [M]：槽位上的专家是否属于组 g
                   slots = slot_mask.nonzero().squeeze(-1)
                   nodes = slots // per_node_slots
                   assert (nodes == nodes[0]).all(), f"层{l} 组{g} 跨节点"
                   group2node.append(nodes[0].item())
               assert torch.equal(
                   torch.bincount(torch.tensor(group2node), minlength=N),
                   torch.full((N,), G // N))

       return phy2log, log2phy, logcnt
   ```

3. **需要观察的现象**：先手工构造一个**故意违反** INV-6b 的场景验证测试本身有效——例如把 `phy2log` 的第 0 列与最后一列对调后再跑同样的检查逻辑，应当看到断言报错（测试的「元验证」：能通过的测试不一定是好测试，能抓住错误的才是）。
4. **预期结果**：对合法输入全部通过；对被破坏的方案必有一条 INV 失败。
5. **待本地验证**：本环境无法运行 Python，以上代码是按源码语义逐行推导编写的；请在本地跑通后确认。

几个实现细节的说明：

- `slots // per_node_slots`：整除运算把槽位号映射回节点号，依据正是 4.2.3 推出的编码公式。
- INV-6b（副本不跨节点）逻辑上被 INV-7 蕴含，但独立保留它有调试价值：INV-7 失败而 INV-6b 通过，说明是「组打包」错位；两者都失败，更可能是节点基地址偏移（[eplb.py:L123-L125](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L123-L125)）出错。分层的断言能缩小定位范围。
- `torch.equal` 比 `assert (a == b).all()` 更严格（同时比较 shape 与 dtype），测试中应优先使用。

#### 4.2.5 小练习与答案

**练习 1**：INV-5 的双层循环是 \( O(L \times E) \) 次小操作。请给出一个部分向量化的改写思路。

**答案**：按 `logcnt` 生成有效位掩码 `mask = arange(c_max) < logcnt.unsqueeze(-1)`（形状 `[L,E,c_max]`），则「padding 全为 -1」一条可整体写作一条断言：取出 `log2phy[~mask]`，断言其等于全 -1 的同形张量；「反向读法」也可部分向量化：把 `log2phy` 的有效位按层铺回 `[L, M]` 形状的索引（用 `masked_select(mask).view(L, -1)`，每行恰 M 个有效位），再断言 `phy2log.gather(-1, 该索引)` 等于 `logcnt` 按有效位展开后重复的专家编号（即 `torch.arange(E).expand(L, E).repeat_interleave` 按 `logcnt` 展开）。注意「集合相等」（排序后比较）这一条难以完全向量化，且 `phyrank` 不外泄使得「rank 顺序」无法直接复现，只能比较集合——这也是入口不返回 `phyrank` 的小代价。

**练习 2**：从 [eplb.py:L62-L71](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L62-L71) 出发，严格证明分层调用下 INV-3 在「每层」粒度成立（而非仅每节点）。

**答案**：分层调用 `replicate_experts` 时输入被 `view` 成 `[L*N, E/N]`（[eplb.py:L112-L113](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L112-L113)），输出的 `mlogcnt` 形状为 `[L*N, E/N]`，即每层的每个节点段独立满足「总和 = \( M/N \)」。`logcnt` 由 `mlogcnt.view(L, -1)` 经 `log2mlog` 重排回来（[eplb.py:L128](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L128)），重排不改变集合，故每层 \( \sum_e \text{logcnt}[l,e] = N \times M/N = M \)。global 分支 \( N=1 \) 时同理直接成立。

**练习 3**：INV-6 的 `view` 断言似乎「自动成立」（元素总数必然匹配）。它作为测试的价值是什么？

**答案**：它是一道**布局契约回归测试**。`view(L, N_eff, gpus_per_node, phy_per_gpu)` 之所以能成立且语义正确，依赖函数按「节点优先、节点内 GPU 次之、卡内序号最后」的顺序组织列（由 [eplb.py:L123-L127](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L123-L127) 的 `view(num_layers, num_nodes, -1)` 与 flatten 顺序决定）。若未来有人改动返回布局（例如改成 GPU 优先），形状不变、下游却会把专家放到错误的卡上——INV-6b/INV-7 以及 u3-l4 的重排流水线都会随之失败。契约测试锁住的就是这种「形状看不出来的语义」。

### 4.3 参数矩阵与边界分支：让测试真正覆盖代码

#### 4.3.1 概念说明

跑 1000 组随机参数 ≠ 覆盖良好。随机参数几乎总是落在「主路径」上；真正的风险藏在分支里。对本仓库，必须**显式**命中的分支有：

| 分支 | 位置 | 触发条件 |
|---|---|---|
| hierarchical 主路径 | [eplb.py:L150-L153](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L150-L153) | `G % N == 0` |
| global 退化路径 | [eplb.py:L154-L156](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L154-L156) | `G % N != 0` |
| `balanced_packing` 平凡分支（Step 1） | [eplb.py:L22-L25](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L22-L25) | 组装箱时 `groups_per_pack == 1`，即 \( G = N \) |
| `balanced_packing` 平凡分支（Step 3） | 同上 | 物理专家装箱时每包 1 个，即 \( M/N = P/N \Leftrightarrow M = P \) |
| `replicate_experts` 零循环 | [eplb.py:L66](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L66) | \( M = E \)（无冗余），`logcnt` 全 1、`maxlogcnt=1`、`log2phy` 无 -1 |
| 非法参数断言 | [eplb.py:L90-L95](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L90-L95)、[eplb.py:L59-L60](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L59-L60) | 违反任一整除关系或 \( M < E \) |

#### 4.3.2 核心流程

构造参数组合的方法：先把合法域写成不等式组

\[ E \bmod G = 0,\quad G \bmod N = 0,\quad P \bmod N = 0,\quad M \bmod P = 0,\quad M \ge E \]

再对每个想覆盖的分支**追加**一个等式（如 \( G = N \)、\( M = E \)、\( G \bmod N \ne 0 \)），解出具体数字，最后逐一代入验证全部约束满足。这比「随手挑数字」可靠得多——本讲在准备用例时就曾因漏算 \( M \bmod P \) 而淘汰过 `(G=4, N=3, M=16, P=6)` 这样的组合（\( 16 \bmod 6 \ne 0 \) 会直接触发断言，根本走不到 global 分支）。

#### 4.3.3 源码精读

**分派对非法输入的「遮蔽效应」。** 值得专门指出：`E % G != 0` 这条非法条件**并不总**会报错——若同时 `G % N != 0`，入口会走 global 分支，把参数替换为 `(M, 1, 1, P)`（[eplb.py:L154-L156](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L154-L156)），此时 \( E \bmod 1 = 0 \) 恒成立，原本非法的组数被静默忽略。因此负向测试必须**成对**构造：同一个 `E % G != 0`，要分别测 `G % N == 0`（必须抛断言错误）与 `G % N != 0`（合法走 global）两种结局。这是读分派代码才能发现的测试设计点。

**平凡分支的行为。** [eplb.py:L22-L25](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L22-L25) 在 `groups_per_pack == 1` 时直接返回「物品 i 进包 i、包内排名 0」，跳过排序贪心。对 Step 1 而言这意味着组 g 固定进节点 g——**不再做任何负载均衡**（因为每节点只装得下 1 个组，无自由度可言）。测试此分支验证的是「不崩溃且不变量仍成立」，而非「更均衡」。

**无冗余分支的连锁反应。** \( M = E \) 时复制循环零次，`logcnt` 全 1，于是 `maxlogcnt = 1`、`log2phy` 第三维为 1 且**不含任何 -1**；Step 3 装箱的权重 `tokens_per_phy = tokens_per_mlog / 1` 退化为原始负载。这组断言能同时锻炼 INV-1（第三维恰为 1）与 INV-5（padding 段为空）。

#### 4.3.4 代码实践

**实践 3：参数矩阵 + 负向测试 + README 金标准（示例代码，待本地验证）**

1. **实践目标**：让 `test_eplb.py` 覆盖 4.3.1 表格中的全部分支。
2. **操作步骤**：在 `test_eplb.py` 中追加：

   ```python
   # ---------- 正向参数矩阵 ----------
   # (E, G, N, M, P, 说明)
   CASES = [
       (12, 4, 2, 16, 8, "README 示例 / hierarchical 主路径"),
       (12, 4, 4, 16, 8, "G==N：Step1 组装箱平凡分支"),
       (12, 2, 2, 16, 2, "每节点单卡：Step3 num_packs=1"),
       (12, 4, 2, 12, 6, "M==E：无冗余，复制循环零次"),
       (12, 4, 3, 18, 6, "G%N!=0：global 退化路径"),
       (12, 4, 3, 12, 6, "global + 无冗余"),
   ]


   def test_all_cases():
       torch.manual_seed(0)
       for E, G, N, M, P, desc in CASES:
           for L in (1, 3):                       # 同时覆盖单层
               weight = torch.randint(1, 1000, (L, E))
               check_invariants(weight, M, G, N, P)
       # 大规模随机压力：64 专家、8 组、4 节点、16 卡
       weight = torch.rand(2, 64) * 1000
       check_invariants(weight, 80, 8, 4, 16)


   # ---------- 负向参数测试 ----------
   INVALID_CASES = [
       (13, 2, 1, 16, 4),   # E % G != 0 且 G % N == 0 → 必须报错
       (12, 4, 2, 18, 8),   # M % P != 0
       (12, 4, 2, 8, 8),    # M < E（隐性约束，由 replicate_experts 把守）
       (12, 4, 4, 16, 6),   # P % N != 0
   ]

   def test_invalid_params():
       for E, G, N, M, P in INVALID_CASES:
           weight = torch.rand(2, E)
           try:
               eplb.rebalance_experts(weight, M, G, N, P)
           except AssertionError:
               continue
           raise AssertionError(f"参数 {E,G,N,M,P} 本应触发断言错误")


   # ---------- README 金标准回归 ----------
   def test_readme_example():
       weight = torch.tensor([[ 90, 132,  40,  61, 104, 165,  39,   4,  73,  56, 183,  86],
                              [ 20, 107, 104,  64,  19, 197, 187, 157, 172,  86,  16,  27]])
       phy2log, log2phy, logcnt = eplb.rebalance_experts(weight, 16, 4, 2, 8)
       expected = torch.tensor([[ 5,  6,  5,  7,  8,  4,  3,  4, 10,  9, 10,  2,  0,  1, 11,  1],
                                [ 7, 10,  6,  8,  6, 11,  8,  9,  2,  4,  5,  1,  5,  0,  3,  1]])
       assert torch.equal(phy2log, expected)


   if __name__ == "__main__":          # 无 pytest 时的运行入口
       test_all_cases()
       test_invalid_params()
       test_readme_example()
       print("all tests passed")
   ```

3. **需要观察的现象**：
   - `python test_eplb.py`（或 `python -m pytest test_eplb.py -v`）输出 `all tests passed`；
   - 注释掉 `test_invalid_params` 里 `except AssertionError: continue` 之外任一用例，观察具体是源码哪一行断言把错误抛出（对照 [eplb.py:L90-L95](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L90-L95) 与 [eplb.py:L59-L60](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L59-L60)）；
   - 用 `(12, 4, 2, 16, 16)`（即 \( M = P \)，每卡恰 1 个物理专家）追加一个用例，验证 Step 3 平凡分支——注意此参数需满足 \( M \ge E \)（16 ≥ 12 ✓）且 \( M \bmod P = 0 \)（16 % 16 = 0 ✓）。
4. **预期结果**：全部正向用例通过；4 个负向用例都被 `AssertionError` 拦截；README 金标准一致。
5. **待本地验证**：以上代码为按源码推导的示例代码，本环境未实际运行；README 期望值取自 [README.md:L55-L56](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/README.md#L55-L56) 的打印注释。若金标准不一致而所有不变量通过，大概率是权重并列时排序/取最小值的次序差异，属实现细节（见 4.1.5 练习 2），此时应以不变量为准并在测试中注明版本。

用例与分支的对应关系（供核对）：

| 用例 (E,G,N,M,P) | 命中分支 |
|---|---|
| (12, 4, 2, 16, 8) | hierarchical 主路径；README 金标准同参数 |
| (12, 4, 4, 16, 8) | Step 1 平凡分支（G==N，每组独占节点） |
| (12, 2, 2, 16, 2) | Step 3 `num_packs=1`（每节点单卡，8 个专家全进 1 个包） |
| (12, 4, 2, 12, 6) | 复制零循环（无冗余），`maxlogcnt=1`、无 -1 padding |
| (12, 4, 3, 18, 6) | global 退化路径（18 % 6 = 0 ✓，此前 16 % 6 ≠ 0 的组合不合法） |
| (12, 4, 3, 12, 6) | global + 无冗余 |
| L=1 的全部重复 | 单层边界（`balanced_packing` 循环仅一轮） |
| (64, 8, 4, 80, 16) | 大规模压力（向量化路径的真实规模） |

#### 4.3.5 小练习与答案

**练习 1**：构造一个触发 **Step 3** 平凡分支（`groups_per_pack == 1`）的合法参数组合，并说明为什么这个条件等价于 \( M = P \)。

**答案**：Step 3 调用 `balanced_packing(tokens_per_phy, P // N)`（[eplb.py:L118](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L118)），物品数是每节点物理专家数 \( M/N \)，包数是每节点 GPU 数 \( P/N \)，故 `groups_per_pack = (M/N) / (P/N) = M/P`，等于 1 当且仅当 \( M = P \)。再叠加 \( M \ge E \) 得 \( P \ge E \)。例如 `(E=12, G=4, N=2, M=16, P=16)`：16 % 16 = 0 ✓、16 % 2 = 0 ✓、4 % 2 = 0 ✓、12 % 4 = 0 ✓、16 ≥ 12 ✓，每卡恰 1 个物理专家。

**练习 2**：`INVALID_CASES` 第一条为何必须选 `G % N == 0` 的组合？若改成 `(E=13, G=4, N=3, M=16, P=6)` 会发生什么？

**答案**：因为分派有遮蔽效应（4.3.3）。`G=4, N=3` 时 `G % N != 0`，入口走 global 分支 `(16, 1, 1, 6)`，`E % 1 == 0` 恒真——但该组合还会死在 `16 % 6 != 0` 上，误让人以为测到了 `E % G` 断言，实际测到的是 `M % P` 断言，测试意图被污染。要让「`E % G != 0` 必须报错」这条负向用例名实相符，必须保证 `G % N == 0` 使其进入 hierarchical 分支。写负向测试时要对照报错的具体行号，确认「死于哪条断言」与用例注释一致。

**练习 3**：`test_all_cases` 里固定了 `torch.manual_seed(0)`。随机测试中固定种子是让测试「确定」还是「更弱」？如何兼顾？

**答案**：固定种子保证可复现（失败可重放、CI 不抖动），但单靠一个种子覆盖面窄。兼顾做法是分层：CI 里跑固定种子的确定用例（本讲做法）；本地开发时再临时换成多个随机种子或使用 Hypothesis 一类的属性测试框架做随机搜索——无论哪种，判定标准始终是同一份 `check_invariants`，这正是把「不变量逻辑」与「输入生成」解耦的好处。

## 5. 综合实践

**任务：交付你的 `test_eplb.py` 并做一次「破坏性验证」。**

1. **整合**：把 4.2.4 的 `check_invariants`、4.3.4 的参数矩阵/负向用例/金标准合并成完整文件，确保 `python test_eplb.py` 与 `python -m pytest test_eplb.py -v` 两种方式都能跑。
2. **扩展两个用例**（自行推导约束后再验证）：
   - 全相等权重：`weight = torch.ones(L, E)`。观察 `balanced_packing` 在所有物品并列时的行为——组 g 是否按编号顺序进包？此时 INV-7 的组-节点分布是什么形态？（提示：降序 `sort` 保序 + `min` 取编号最小的未满最轻包，两者都偏向小编号。）
   - 极端长尾：把一个专家的权重设为其余总和的 10 倍，观察它的 `logcnt` 是否显著大于其他专家（这已摸到 u3-l2「均衡质量」的门口，但本讲只记录现象，不下结论）。
3. **破坏性验证（元测试）**：写一个 `test_mutation`，人为构造一张非法方案（例如把 `phy2log` 某行随机重排若干列、或把某个 `logcnt` 加一），确认 `check_invariants` 的校验逻辑套上去**必然**报错。能抓错的校验器才是活的校验器。
4. **产出**：一张「用例 × 不变量」的覆盖表格，标注每个用例命中了哪些分支、每条不变量在哪些用例上被执行过。
5. **待本地验证**：以上全部为可操作步骤，具体输出请在本机运行后记录；若发现与本讲推导不符的现象，优先怀疑参数是否满足 4.3.2 的不等式组，再怀疑断言实现。

## 6. 本讲小结

- EPLB 输出的是「放置方案」，其正确性用**结构不变量**测试，其均衡**质量**用指标评估——本讲只做前者，共梳理 7 条：形状、值域、守恒、覆盖计数、互逆一致、布局契约、组-节点对齐。
- 每条不变量都能追溯到具体源码：守恒源于 `replicate_experts` 的初始化 + 每轮自增一次；互逆源于入口的 `scatter_` 扁平地址 `phy2log * maxlogcnt + phyrank`；组对齐源于 `log2mlog` 编码与装箱基数约束的合力。
- `log2phy` 第三维恰为 `logcnt.max()`，-1 哨兵只出现在每个专家第 \( c \) 个副本之后的槽位；\( c_{\max} \ge \lceil M/E \rceil \) 是可免费获得的一道额外断言。
- 参数矩阵要按「合法域不等式组 + 目标分支等式」推导构造，显式覆盖两条策略分支、两个装箱平凡分支、无冗余零循环、单层、大规模与四类非法参数。
- 分派存在**遮蔽效应**：`E % G != 0` 在 `G % N != 0` 时被 global 分支静默吞掉——负向测试必须成对构造并核对「死于哪条断言」。
- 通用不变量断言不断言具体输出值；金标准回归（README 示例）单独存放，并注明对排序并列行为的版本敏感性。

## 7. 下一步学习建议

本讲得到的 `check_invariants` 与参数矩阵是后续两讲的公共基础设施：

- 下一讲 **u3-l2（负载均衡质量评估）**：在同一参数矩阵上，把「每 GPU 专家数」（布局契约，自动满足）升级为「每 GPU 负载」（算法质量），利用 `weight[phy2log] / logcnt[phy2log]` 按槽位求和计算每卡负载，对比 hierarchical / global / 无冗余基线的 max/min、max/mean 与标准差，并对照理论下界 \( \sum w / P \)。
- 若你对实现细节的兴趣大于评估，可以先跳读 **u3-l3（性能与设备一致性）**，那里用本讲的测试守护一次真实的向量化改造，并分析两个修复 commit（`d52c72d`、`e1100fe`）。
- 建议同时精读 [eplb.py:L74-L129](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L74-L129)（层级函数全文），边读边自问「这一行为哪条 INV 负责」——能一对一回答，说明你已把实现与契约打通。
