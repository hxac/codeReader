# 批量重副本策略：plan_transfers 与可插拔 replica policy

## 1. 本讲目标

上一讲（u8-l7）我们读懂了 affine 分片迁移的「单参数」通道：`plan_transfer` 把源与目标两张仿射映射组合成一份按 `TransferSegment` 组织的搬运计划。本讲进入 #8936 在其上新增的「批量规划层」，学完后你应该能够：

1. 说清 `plan_transfers` 与 `plan_transfer` 的分工：单参数规划 vs 一次同步全参数批量规划，以及二者如何共享 `_prepare`/`_emit` 这两段校验与发射逻辑（`plan_transfer` 行为完全不变）。
2. 逐字段解读 `ReplicaRequest`/`ReplicaPart`/`ReplicaAnswer` 请求-答案协议：请求按什么固定顺序排列、携带哪些信息、`source_processes`/`target_processes` 如何把「映射 location」翻译成「进程」、为什么单 holder 区域也要入列。
3. 对比三个内置策略：`first_holder`、`local_first_least_loaded`、`per_request(selector)`，并能写出自己的第四个。
4. 列出答案发射前的全部校验：一请求一答、只能选已验证 holder、`ReplicaPart` 必须沿 axis 0 精确平铺（gap/重叠/越界/非整数坐标均拒绝）、`min_split_bytes` 下限、`PlanBudget.max_pair_checks` 预算（且超限在展开 parts 之前按答案长度先拒绝）。
5. 解释两条贯穿性的原理：「任意一致的计划都是精确的」（策略只决定从哪读、不决定送达什么），以及「所有 rank 必须返回相同答案」（每个 rank 各自发射同一调度的一半，executor 的 schedule digest 会拒绝不一致）——后者决定了成本感知策略（#8252）的成本输入必须来自全员共享的数据（如 #8966 的链路标定 `fit_links`），而非 rank 本地测量。

本讲全部代码都可以在纯 CPU、无分布式环境下运行：规划（planning）本身只是纯 Python 计算。

## 2. 前置知识

### 2.1 承接 u8-l7 的认知

本讲默认你已经理解上一讲建立的这些概念，这里只做一句话回温：

- **仿射映射（affine map）**：`ParamAffineMap` 用若干 `AffinePiece`（形状 + source/dest 两侧 strided 视图 + `locations` 副本集 + 可逆 scale）刻画「一个 rank 的分片在完整逻辑张量中的位置」。`replicated_map`（全复制）、`contiguous_split_map`（沿某一维连续切）是两个最常用的构造器。
- **规划（`plan_transfer`）**：把源映射与目标映射组合，为每个「目标 piece 与源区域的交集」选一个读取方（holder），发射为 `TransferSegment`（一条可直接执行的 strided 拷贝：源 rank/offset/strides + 目标 rank/offset/strides + 共享 shape）。
- **`PlanBudget`**：规划器自身工作量的上限（不是正确性约束），防止 piece 数本身是 bug 的映射迫使无界枚举。
- **执行（`execute_transfer`）**：各 rank 先对计划摘要（digest）达成共识，再按轮次协议搬运。

### 2.2 本讲的新直觉：从「逐个问路」到「看全图派活」

`plan_transfer` 的 `replica_selector` 是**逐路口问路**式的：每个有多份副本的区域单独问一次「从哪读」，每次问话互相看不见。这在单参数世界里够用，但一次权重同步（sync）通常要搬成百上千个参数——如果每个区域都独立选「第一个 holder」，所有负载会砸在同一批发送进程上，网络成为瓶颈。

`plan_transfers` 换成**看全图派活**：把一次同步的全部请求按固定顺序排成一列，一次性交给一个「重副本策略」（replica policy），让它统筹决定每个区域从哪读、甚至是否把一个大区域切成几段分给不同发送方。这正是负载均衡（load balancing）的标准建模：

\[ L_s = \sum_{\text{请求 } r \text{ 指派给发送方 } s} \mathrm{size}(r), \qquad \text{目标：让各 } L_s \text{ 尽量均匀} \]

其中 \(\mathrm{size}\) 是请求的字节数（或元素数）。策略输出的不是搬运本身，而只是一份「指派表」——这一点引出本讲最重要的一条定理化结论，见 4.5。

### 2.3 两个容易混淆的「rank」

`ParamAffineMap` 里的 rank 是**映射 location**（源映射与目标映射各自从 0 编号），不是进程号：同一个数字在两侧命名的是不同进程。要把 location 翻译成真正的进程，需要调用方额外提供 `source_processes` / `target_processes` 两张映射表。记住这一点，4.3 里「为什么无映射时不假设本地拷贝」才好理解。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [deepspeed/checkpoint/affine_transfer.py](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/checkpoint/affine_transfer.py) | 规划器主体。本讲重点是新增的 `plan_transfers`（L530-L637）、协议数据类（L118-L169）、答案校验 `_resolve`（L659-L721）；`plan_transfer`（L494-L527）与 `_prepare`/`_emit`（L724-L796）是上讲已读、本讲复用的骨架 |
| [deepspeed/checkpoint/affine_replica_policy.py](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/checkpoint/affine_replica_policy.py) | #8936 新模块，仅 90 行：三个内置策略 `first_holder` / `local_first_least_loaded` / `per_request`。策略住在这里而不在规划器里，正是为了让成本感知策略可以独立生长 |
| [tests/unit/checkpoint/test_affine_replica_policy.py](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/tests/unit/checkpoint/test_affine_replica_policy.py) | 65 个纯 CPU 测试：随机策略精确性、三策略行为、请求顺序与字段、拒绝矩阵、预算时机 |
| [docs/code-docs/source/affine-transfer.rst](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/docs/code-docs/source/affine-transfer.rst) | 官方契约文档，新增 "Planning a sync with replica policies" 一节（L36-L75） |
| [deepspeed/checkpoint/affine_transfer_executor.py](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/checkpoint/affine_transfer_executor.py) | 执行器。本讲只看一处：批量调度的 digest 共识拒绝（L417-L418），它是「所有 rank 必须答案一致」的执行侧执法者 |

辅助背景（不必重读）：[deepspeed/checkpoint/affine.py](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/checkpoint/affine.py) 提供实践要用的 `replicated_map`（L568-L595）、`contiguous_split_map`（L598-L623）与 `extract`（L451 起）。

## 4. 核心概念与源码讲解

### 4.1 plan_transfers 批量编排与 _prepare/_emit 复用

#### 4.1.1 概念说明

`plan_transfer` 一次规划一个参数；`plan_transfers` 一次规划**一次同步的全部参数**。设计上的关键取舍是：批量层没有另写一套校验，而是原样复用单参数层的两个函数——

- `_prepare(target_map, source_map, budget)`：校验两张映射（几何合法性、分片铺贴、holder 声明一致、源全覆盖），并把目标 piece 切成与源区域的交集（`_Intersection`），每个交集附带「已验证 holder 列表」。
- `_emit(prepared, choices, budget)`：把「(区域, 选定 holder)」对发射成合并后的 `TransferSegment` 并检查计划预算。

因此批量层新增的只有：请求的**收集与排序**、策略的**一次调用**、答案的**逐条校验**（4.4）。`plan_transfer` 的行为一个字节都没变——这一点不是口头承诺，而是被测试固化的契约（见 4.1.4）。

#### 4.1.2 核心流程

`plan_transfers` 的执行流程：

1. 校验关键字参数：`source_processes`/`target_processes` 必须是 int→int 映射；`element_size` 对每个参数必须是正 int；`min_split_bytes` 必须是正 int 且**依赖** `element_size`（映射数的是元素，不是字节）。
2. 对 `pairs` 中每个参数（按名字排序）调用 `_prepare`，得到校验过的交集列表。**任何一对映射不合法都会在策略运行之前抛错**。
3. 把所有交集展开为条目 `(参数名, 目标 location, 交集 origin, 交集 extent)` 并排序——这就是「固定顺序」：参数名 → 目标 location → 逻辑 origin → extent。
4. 按该顺序构造 `ReplicaRequest` 元组。
5. 调用策略一次（未传则惰性导入默认策略 `local_first_least_loaded`）。
6. 检查「一请求一答」，然后逐答案走 `_resolve`（4.4）+ 预算检查，得到每个交集的 (区域, holder) 切分。
7. 逐参数调用 `_emit`，返回 `{参数名: TransferPlan}`。

伪代码：

```text
plan_transfers(pairs, ..., replica_policy):
    校验 process/element_size/min_split_bytes 参数
    prepared = {name: _prepare(target, source, budget) for name in sorted(pairs)}
    entries  = [(name, target_rank, origin, extent), ...] 排序
    requests = [ReplicaRequest(...) for entry in entries]
    answers  = (replica_policy or local_first_least_loaded)(requests)
    断言 len(answers) == len(requests)
    for entry, request, answer in zip(entries, requests, answers):
        先按答案长度查预算; parts = _resolve(request, item, answer, ...)
        累计 parts 数并再查预算
    return {name: _emit(prepared[name], choices[name], budget)}
```

#### 4.1.3 源码精读

函数签名与完整契约写在 docstring 里，值得先读一遍：[deepspeed/checkpoint/affine_transfer.py:L530-L567](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/checkpoint/affine_transfer.py#L530-L567)。这段 docstring 明确说了四件事：每个 pair 的校验与 `plan_transfer` 完全一致；请求按「参数、目标 location、origin」固定顺序排列且**单 holder 区域也入列**（其负载是策略搬不动的负载）；每个答案发射前都会被检查；所有 rank 必须给出相同答案、成本输入必须来自全员共享的数据。

参数校验段，注意 `min_split_bytes` 依赖 `element_size` 的原因直接写在报错信息里：[deepspeed/checkpoint/affine_transfer.py:L568-L583](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/checkpoint/affine_transfer.py#L568-L583)——「the maps count elements, not bytes」（映射数的是元素，不是字节）。

请求收集与固定顺序排序：[deepspeed/checkpoint/affine_transfer.py:L585-L594](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/checkpoint/affine_transfer.py#L585-L594)。先逐参数 `_prepare`（复用单参数校验），再把所有交集展开成条目并按 `(name, target.rank, origin, extent)` 排序——固定顺序不只是美观，它让「策略看到请求的顺序」成为契约的一部分（4.3 的默认策略依赖它保证确定性）。

默认策略的惰性导入：[deepspeed/checkpoint/affine_transfer.py:L614-L616](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/checkpoint/affine_transfer.py#L614-L616)。`from .affine_replica_policy import local_first_least_loaded` 放在函数体内，是因为 `affine_replica_policy` 要从 `affine_transfer` 导入协议类型——模块级互相导入会成环，惰性导入把环剪断。

一请求一答的检查：[deepspeed/checkpoint/affine_transfer.py:L617-L621](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/checkpoint/affine_transfer.py#L617-L621)。`_is_sequence` 要求「可索引且有长度」的真实序列——生成器会被拒绝（`len()` 不了），字符串和 bytes 也不算序列（[L640-L642](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/checkpoint/affine_transfer.py#L640-L642)）。

发射前的预算双重检查：[deepspeed/checkpoint/affine_transfer.py:L628-L637](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/checkpoint/affine_transfer.py#L628-L637)。第一遍在 `_resolve` 展开 parts **之前**按答案长度查预算（注释说明：预算约束的是规划器自身的工作量，所以超长答案在长度上就该被拒绝，而不是等每个 part 都展开成盒子之后）；第二遍在累计解析后的 parts 数之后再查一次（int 答案解析后只算 1 个 part）。

对比单参数版的 `plan_transfer`（上一讲已精读，这里只看与批量层共享的骨架）：[deepspeed/checkpoint/affine_transfer.py:L494-L527](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/checkpoint/affine_transfer.py#L494-L527)。它同样走 `_prepare` → 逐交集选 holder（`replica_selector` 只在 `len(holders) > 1` 时被咨询，默认取 `holders[0]`，即最小 holder location）→ `_emit`。批量层没有动它，只是把它内部的「选 holder」一步抽出来放进了策略。

#### 4.1.4 代码实践

**实践目标**：验证「`plan_transfers` + `first_holder`/`per_request` 与 `plan_transfer` 逐 segment 完全一致」，亲手确认批量层没有改变单参数行为。

**操作步骤**（以下为示例代码，可直接保存为 `check_equiv.py` 在仓库根目录用 `python check_equiv.py` 运行；仅需 CPU 版 torch）：

```python
# 示例代码：仿 tests/unit/checkpoint/test_affine_replica_policy.py 的等价性测试
from deepspeed.checkpoint.affine import contiguous_split_map, replicated_map
from deepspeed.checkpoint.affine_replica_policy import first_holder, per_request
from deepspeed.checkpoint.affine_transfer import plan_transfer, plan_transfers

def select(request):                       # plan_transfer 风格的选择器：取最大 holder
    return max(c.source_rank for c in request.candidates)

pairs = {
    'dense':  (contiguous_split_map((12, 6), [2, 4], 1), replicated_map((12, 6), 3)),
    'vector': (contiguous_split_map((9, ), [4, 5], 0),   replicated_map((9, ), 3)),
}

for policy, selector in ((first_holder, None), (per_request(select), select)):
    plans = plan_transfers(pairs, replica_policy=policy)
    for name, (target_map, source_map) in pairs.items():
        assert plans[name] == plan_transfer(target_map, source_map, replica_selector=selector)
print("equivalence OK")
```

也可以直接跑仓库自带的固化测试：

```bash
python -m pytest tests/unit/checkpoint/test_affine_replica_policy.py::test_first_holder_and_per_request_reproduce_plan_transfer -v
```

**需要观察的现象**：两条断言路径（`first_holder` 对应 `replica_selector=None`、`per_request(select)` 对应同一 `select`）都无异常通过。

**预期结果**：输出 `equivalence OK`；pytest 用例 PASSED。`TransferPlan` 是 frozen dataclass，`==` 是逐字段比较，所以「计划相等」意味着 segments、逻辑形状、搬运元素数、统计信息全部一致。待本地验证（本讲义编写时未代跑，但断言与仓库测试 [test_affine_replica_policy.py:L113-L122](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/tests/unit/checkpoint/test_affine_replica_policy.py#L113-L122) 逐字对应）。

#### 4.1.5 小练习与答案

**练习 1**：`plan_transfers` 为什么先对 `pairs` 的键 `sorted()`，再对条目排序？只用一次排序行不行？

**答案**：条目排序键是 `(name, target_rank, origin, extent)`，其实一次排序就够；逐参数 `sorted(pairs)` 另有作用——保证 `_prepare` 与 `element_size` 校验按确定顺序执行，让最先失败的错误信息（哪个参数先报错）也是确定的。两次排序共同把「请求顺序」钉死为契约，供确定性策略（4.3）依赖。

**练习 2**：如果策略返回一个生成器而不是列表，会发生什么？

**答案**：[L618-L621](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/checkpoint/affine_transfer.py#L618-L621) 抛 `InvalidReplicaPolicyError`：`_is_sequence` 要求可索引且有长度，生成器两者皆无。测试 [test_affine_replica_policy.py:L289-L299](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/tests/unit/checkpoint/test_affine_replica_policy.py#L289-L299) 固定了这一行为（同时验证了元组这种真序列可用）。

**练习 3**：映射不合法时策略会被调用吗？

**答案**：不会。`_prepare` 在策略调用之前逐参数执行，几何错误先抛 `AffineTransferError`；测试 [test_affine_replica_policy.py:L376-L386](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/tests/unit/checkpoint/test_affine_replica_policy.py#L376-L386) 用「策略从未被调用」的哨兵列表固化了这一顺序。

### 4.2 ReplicaRequest / ReplicaPart 请求-答案协议

#### 4.2.1 概念说明

策略与规划器之间靠三个冻结 dataclass 通信，构成一个极小的「请求-答案」协议：

- **`ReplicaRequest`（请求）**：一个目标 piece 与一个源区域的交集，附上**全部已验证 holder**。它是一次问话的完整上下文。
- **`ReplicaAnswer`（答案）**：两种形态之二——一个 int（某个已验证 holder 的 location，整段都从它读），或一组 `ReplicaPart`（把区域沿 axis 0 切成几段，每段各指名一个 holder）。
- **`ReplicaPart`（部分）**：一次切分：`logical_origin` + `shape`（沿 axis 0 的一个行带）+ `source_location`（这一段从谁读）。

协议的几个设计决定值得咀嚼：

1. **单 holder 区域也入列**。它们没有选择余地，但它们的字节数是「策略搬不动的负载」——一个诚实的负载均衡者必须把不可移动的负载计入账本，否则它会高估自己的调度自由度。
2. **location 与进程分开**。`target_location` 与 `candidates[i].source_rank` 是两张映射各自的键；`target_process`/`candidate_processes` 才是进程，且未传映射表时为 `None`。这承认了一个现实：源世界与目标世界是两个不同的进程集合。
3. **`bytes` 单独提供**。映射数的是元素，字节需要调用方给 `element_size`。

#### 4.2.2 核心流程

一个请求从产出到消费的生命周期：

```text
_prepared[name].intersections 中的每个 _Intersection
    │  (target: 目标 piece 帧, shared: 交集盒子, holders: 已验证 holder 帧元组)
    ▼
ReplicaRequest{parameter, target_location, target_process,
               logical_origin, shape, target_offset, target_strides,
               candidates=(ReplicaCandidate(rank, offset, strides), ...),
               candidate_processes=(...), bytes}
    ▼  全部请求按固定顺序一次性交给策略
策略返回 [answer, ...]    # answer ∈ int | Sequence[ReplicaPart]
    ▼
_resolve: 把答案翻译回 ((LogicalBox, _Frame), ...) 并拒绝一切非法形态
    ▼
_emit: 发射为 TransferSegment
```

`ReplicaCandidate` 里的 `source_offset`/`source_strides` 是「该 holder 的分片内地址」——回想上讲的教训：piece 的 `source_*` 寻址完整逻辑张量、`dest_*` 才寻址分片，所以发给策略的候选地址用的是 holder 帧的 `dest` 侧（`holder.address(shared)`）。

#### 4.2.3 源码精读

协议数据类全部集中在 [deepspeed/checkpoint/affine_transfer.py:L118-L169](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/checkpoint/affine_transfer.py#L118-L169)：

- `ReplicaCandidate`（L118-L122）：一个 holder 的最小名片——location + 分片内 offset/strides。
- `ReplicaRequest`（L135-L157）：docstring（L137-L143）专门解释了 location 与进程的两个命名空间、`None` 的语义；`numel` 属性（L155-L157）给无 `element_size` 时的策略一个元素数兜底。
- `ReplicaPart`（L160-L165）：一行注释说明它只能沿 axis 0 切。
- 两个类型别名（L168-L169）：`ReplicaAnswer = Union[int, Sequence[ReplicaPart]]`、`ReplicaPolicy = Callable[[Sequence[ReplicaRequest]], Sequence[ReplicaAnswer]]`——策略就是「请求序列 → 答案序列」的可调用对象，**可以是带状态的对象**（见 [affine_replica_policy.py:L13-L14](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/checkpoint/affine_replica_policy.py#L13-L14)：规划器自身不持有任何状态）。

请求的构造现场：[deepspeed/checkpoint/affine_transfer.py:L596-L612](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/checkpoint/affine_transfer.py#L596-L612)。注意三个细节：`target_offset=item.target.address(item.shared)`（目标分片内地址）；candidates 逐 holder 用 `holder.address(item.shared)`（各自分片内地址，不同 holder 的局部 offset 天然可以不同）；`bytes` 只有给了 `element_size` 才有值（交集体积 × 每元素字节）。

location→进程的翻译与缺失报错：[deepspeed/checkpoint/affine_transfer.py:L651-L656](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/checkpoint/affine_transfer.py#L651-L656)——映射表存在却漏了某个 location 时直接抛 `AffineTransferError`，不静默填 `None`（那会让策略把「跨进程传输」误判成别的）。

#### 4.2.4 代码实践

**实践目标**：亲眼看到请求的固定顺序与字段填充方式（有/无映射表两种形态）。

**操作步骤**（示例代码，改写自 [test_affine_replica_policy.py:L135-L178](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/tests/unit/checkpoint/test_affine_replica_policy.py#L135-L178)）：

```python
# 示例代码：录制策略看到的请求
from deepspeed.checkpoint.affine import contiguous_split_map, replicated_map
from deepspeed.checkpoint.affine_transfer import plan_transfers

def recording(policy):
    seen = []
    def wrapper(requests):
        seen.extend(requests)
        return policy(requests)
    return wrapper, seen

pairs = {
    'b': (contiguous_split_map((4, 2), [2, 2], 0), replicated_map((4, 2), 2)),
    'a': (contiguous_split_map((6, ), [6], 0),      contiguous_split_map((6, ), [6], 0)),
}
wrapper, seen = recording(lambda rs: [r.candidates[0].source_rank for r in rs])
plan_transfers(pairs,
               source_processes={0: 7, 1: 8}, target_processes={0: 8, 1: 9},
               element_size={'a': 4, 'b': 2}, replica_policy=wrapper)
```

**需要观察的现象**：`seen` 的顺序是 `[('a',0,(0,)), ('b',0,(0,0)), ('b',1,(2,0))]`——参数名字典序在前（`'a'` 先于 `'b'`，与 `pairs` 的书写顺序无关），然后目标 location，然后 origin。`'a'` 是单 holder 请求：`candidates` 只有一个 location 0、`candidate_processes == (7,)`、`bytes == 24`；`'b'` 的第二个请求有两个候选 holder（location 0 和 1，进程 7 和 8）、`bytes == 8`。

**预期结果**：与测试断言一致；再跑一次不带 `source_processes`/`target_processes`/`element_size` 的调用，观察 `target_process is None`、`candidate_processes == (None, None)`、`bytes is None`（对应 [test_affine_replica_policy.py:L173-L178](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/tests/unit/checkpoint/test_affine_replica_policy.py#L173-L178)）。待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `ReplicaRequest` 要携带 `target_offset`/`target_strides`？策略选 holder 又用不上它们。

**答案**：协议把「策略需要的」与「答案校验/发射需要的」放在同一张请求里，避免规划器另建平行结构；同时 `per_request` 要把请求降级成 `plan_transfer` 风格的 `ReplicaSelectionRequest`（它含同样的目标侧字段，见 [affine_replica_policy.py:L80-L87](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/checkpoint/affine_replica_policy.py#L80-L87)），让旧选择器零改动迁移。

**练习 2**：两个 holder 的 `ReplicaCandidate.source_offset` 一定相同吗？

**答案**：不一定。同一逻辑区域在两个 holder 分片中的局部位置由各自的 `dest` 侧布局决定；构造现场逐 holder 调 `holder.address(item.shared)`（[L607-L608](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/checkpoint/affine_transfer.py#L607-L608)），`_sources` 的 docstring 也点明了这一点（不同目标可用不同 holder，其局部 offset 无需一致）。

**练习 3**：如果 `element_size` 漏了某个参数会怎样？

**答案**：在策略运行之前就抛 `AffineTransferError`（`element_size for 'x' is None, not a positive int`，[L574-L578](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/checkpoint/affine_transfer.py#L574-L578)）——协议要么给全部请求配字节，要么一个都不配。

### 4.3 affine_replica_policy 三策略

#### 4.3.1 概念说明

[affine_replica_policy.py](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/checkpoint/affine_replica_policy.py) 的模块 docstring 把这一层的设计立场说得很清楚：策略住在这里、不住在规划器里，所以成本感知策略可以独立生长而规划器不变。三个内置策略从简到繁：

| 策略 | 一句话 | 用途 |
| --- | --- | --- |
| `first_holder` | 每个请求取候选中**最小 holder location** | 等价于 `plan_transfer` 默认行为，作对照基线 |
| `local_first_least_loaded` | 目标进程自己持有就本地读；否则发给本批次**已分配字节最少**的发送进程 | `plan_transfers` 的默认策略：能省链路的省链路，剩下的做负载均衡 |
| `per_request(selector)` | 把 `plan_transfer` 风格的逐请求选择器包装成批量策略 | 兼容层：旧选择器零改动进入批量世界 |

`local_first_least_loaded` 的账本规则（docstring [L33-L41](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/checkpoint/affine_replica_policy.py#L33-L41)）四条：

1. **单 holder 请求计入负载**——那份负载搬不动，但占了发送方的带宽；
2. **本地拷贝不计**——它不占用任何链路；
3. **平手取最小 location**，且请求按规划器的固定顺序到达，所以答案是**确定的**（deterministic）；
4. **从不切分**（可以证明：负载均衡问题在这层只用「整段指派」这个动作就够用了，切分留给未来的成本感知策略）。

#### 4.3.2 核心流程

`local_first_least_loaded` 对每个请求的判定流程：

```text
for request in requests:                      # 固定顺序
    local = [holder_location for holder, process in zip(candidates, candidate_processes)
             if process is not None and process == target_process]
    if local:                                 # 目标进程自己持有 → 本地读
        answer = min(local);  continue        # 本地拷贝不记账
    sender(i) = ('process', p_i)  若候选带进程号, 否则 ('location', loc_i)
    chosen = argmin_i (load[sender(i)], loc_i)   # 字典序：先负载后 location
    load[sender(chosen)] += request.bytes or request.numel
    answer = candidates[chosen].source_rank
```

形式化地，它是在线贪心地最小化各发送方的累计负载，字典序平手规则 \(\arg\min_i (L_{s_i}, \ell_i)\) 保证唯一确定的选择。

注意 `sender` 的键设计：有进程映射时按**进程**记账（两个 location 指向同一进程时负载合并），无映射时退化为按 location 记账——因为「同一数字在两侧命名不同进程」，无映射时绝不能假设 location 相同就是本地拷贝。

#### 4.3.3 源码精读

`first_holder`：[deepspeed/checkpoint/affine_replica_policy.py:L27-L29](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/checkpoint/affine_replica_policy.py#L27-L29)。一行：`request.candidates[0].source_rank`。候选按 holder rank 排序（上讲 `_sources` 保证），所以 `[0]` 就是最小 location——与 `plan_transfer` 的 `holders[0]` 默认完全对应。

`local_first_least_loaded` 主体：[deepspeed/checkpoint/affine_replica_policy.py:L42-L64](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/checkpoint/affine_replica_policy.py#L42-L64)。本地判定（L45-L51）用 `zip(request.candidates, request.candidate_processes)` 对照目标进程；`sender` 键函数（L53-L57）实现进程/location 双记账；选择（L59-L60）用 Python 元组比较天然实现「先负载、后 location」的字典序 `argmin`；记账尺寸（L61）`request.bytes if request.bytes is not None else request.numel`——有字节用字节，没有退化为元素数。

`per_request`：[deepspeed/checkpoint/affine_replica_policy.py:L67-L90](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/checkpoint/affine_replica_policy.py#L67-L90)。单 holder 请求直接作答不经选择器（L77-L79），多 holder 请求把 `ReplicaRequest` 降级为 `ReplicaSelectionRequest` 后调用旧选择器（L80-L87）——字段名与 `plan_transfer` 构造的选择请求完全一致，这就是「零改动迁移」的实现。

官方文档对三个策略与调用方式的总结：[docs/code-docs/source/affine-transfer.rst:L36-L75](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/docs/code-docs/source/affine-transfer.rst#L36-L75)，其中 L42-L50 是带 `source_processes`/`target_processes`/`element_size`/`min_split_bytes` 的完整调用示例。

#### 4.3.4 代码实践

**实践目标**：用三组小实验分别验证默认策略的「本地优先」「不可搬负载计入」「无映射不假设本地」三条行为。

**操作步骤**（示例代码，断言取自 [test_affine_replica_policy.py:L185-L234](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/tests/unit/checkpoint/test_affine_replica_policy.py#L185-L234)）：

```python
# 示例代码：默认策略三连测
from deepspeed.checkpoint.affine import contiguous_split_map, replicated_map
from deepspeed.checkpoint.affine_transfer import plan_transfers
from deepspeed.checkpoint.affine_replica_policy import local_first_least_loaded
from deepspeed.checkpoint.affine_transfer import ReplicaCandidate, ReplicaRequest
import dataclasses

# 实验 1：目标进程持有 → 本地读
pairs = {'w': (contiguous_split_map((8, 4), [4, 4], 0), replicated_map((8, 4), 3))}
plans = plan_transfers(pairs, source_processes={0: 10, 1: 11, 2: 12},
                       target_processes={0: 12, 1: 11})
by_target = {s.target_rank: s.source_rank for s in plans['w'].segments}
assert by_target == {0: 2, 1: 1}     # 目标 location0(进程12)=源location2；目标1(进程11)=源1

# 实验 2：单 holder 的 64 字节让 location0 变忙，复制的 'w' 让给 location1
pairs = {'pinned': (contiguous_split_map((16,), [16], 0), contiguous_split_map((16,), [16], 0)),
         'w': (contiguous_split_map((4,), [4], 0), replicated_map((4,), 2))}
plans = plan_transfers(pairs, source_processes={0: 0, 1: 1}, target_processes={0: 5},
                       element_size={'pinned': 4, 'w': 4})
assert {n: sorted({s.source_rank for s in p.segments}) for n, p in plans.items()} == \
       {'pinned': [0], 'w': [1]}

# 实验 3：无映射时 location 相同 ≠ 本地拷贝，平手落回 location 0
req = ReplicaRequest('w', 1, None, (0,), (4,), 0, (1,),
                     (ReplicaCandidate(0, 0, (1,)), ReplicaCandidate(1, 0, (1,))),
                     (None, None), None)
assert local_first_least_loaded([req]) == [0]
assert local_first_least_loaded([dataclasses.replace(req, target_process=11,
       candidate_processes=(10, 11))]) == [1]
print("policy behaviour OK")
```

**需要观察的现象**：实验 1 中目标 location 0 虽然编号最小，仍从源 location 2 读（同进程 12）；实验 2 中 `'w'` 放弃更小的 location 0 改读 location 1（location 0 已被搬不动的 `'pinned'` 占了 64 字节）；实验 3 中同一条请求，带不带进程映射答案不同。

**预期结果**：打印 `policy behaviour OK`。若想看「等量区域摊开」的第四种行为，可用 `contiguous_split_map((8,2),[2,2,2,2],0)` 配双 holder 源，观察 `by_target == {0:0, 1:1, 2:0, 3:1}` 的交替（[test_affine_replica_policy.py:L212-L216](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/tests/unit/checkpoint/test_affine_replica_policy.py#L212-L216)）。待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：`local_first_least_loaded` 为什么「从不切分」也不损害正确性？

**答案**：切分是**性能**动作不是正确性动作——4.5 将证明副本一致时任何被接受的答案（整段指派或合法切分）送达的数据相同。不做切分只是放弃了把大区域摊到多个发送方的能力，这个能力留给未来的成本感知策略通过同一个协议提供。

**练习 2**：请求到达顺序为什么对 `local_first_least_loaded` 是正确性问题而不仅是性能问题？

**答案**：它的账本 `load` 是跨请求累积的在线状态；如果两个 rank 上请求顺序不同，同一策略会算出不同答案，而 executor 要求所有 rank 答案一致（4.5）——否则触发 digest 拒绝。固定顺序（参数、目标 location、origin）+ 字典序平手规则共同把「相同输入 ⇒ 相同答案」钉死。

**练习 3**：写一个「随机选 holder」的策略需要几行？

**答案**：`lambda requests: [random.choice([c.source_rank for c in r.candidates]) for r in requests]` 一行即可——但注意跨 rank 一致性：随机源必须由全员相同的种子驱动（见 4.5 与综合实践）。

### 4.4 答案校验、min_split_bytes 与 pair budget

#### 4.4.1 概念说明

规划器对答案的态度是**先验证后发射**：任何一条答案不合格，整次规划抛 `InvalidReplicaPolicyError`（[L64-L69](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/checkpoint/affine_transfer.py#L64-L69)），**一个 segment 都不会发出**。校验分四组：

1. **形状**：答案序列必须一请求一答、是真序列（生成器/字符串拒绝）。
2. **int 答案**：`type(answer) is int` 且必须是**该请求已验证 holder** 之一。注意 `type(...) is int` 连 `True`（bool 是 int 的子类）和 `1.0` 都拒绝——一个布尔或浮点在这里只会是 bug。
3. **parts 答案**：每个元素必须是 `ReplicaPart`；`source_location` 必须是已验证 holder 的 int；`logical_origin`/`shape` 必须是维度与请求盒一致的 int 元组；必须**沿 axis 0 平铺整个区域**：除首轴外的每一维都要与请求盒完全一致（即只许切首轴），行方向上从请求首行走到末行、不许 gap、不许重叠、不许提前停或冲出界外；不许空 part；标量（0 维）不可切分。
4. **尺寸与预算**：给了 `min_split_bytes` 时，**切分**（parts 数 > 1）的每个 part 折算字节后不得低于下限（未切分的整段答案永远不会「太小」）；每个参数累计的 parts 总数不得超过 `PlanBudget.max_pair_checks`（默认 \(2^{18}\)），且超限在 parts 被逐个展开**之前**就按答案长度拒绝。

axis-0 平铺的数学表述：请求盒首轴范围 \([r_0, r_0 + n)\)，则 parts 排序后须满足

\[ p_0 = r_0, \qquad p_{i+1} = p_i + m_i, \qquad p_{k-1} + m_{k-1} = r_0 + n \]

其中 \(p_i\)、\(m_i\) 是第 i 个 part 的首轴 origin 与长度。这个「行走」证明同时给出无 gap（\(p_{i+1} \geq p_i + m_i\) 的等号情形）与无重叠。

#### 4.4.2 核心流程

`_resolve` 的判定流程：

```text
answer 是 int?
    是 → 在 holders 字典里? 否 → InvalidReplicaPolicyError
         返回 [(整个交集盒, 该 holder)]
answer 是非空序列?
    否 → InvalidReplicaPolicyError
    逐 part: 必须是 ReplicaPart; source_location 是已验证 holder;
             origin/shape 是 ndim 维 int 元组; 非首轴维度与请求盒一致;
             不许空 part
    标量(ndim==0): 只许恰好 1 个 part
    parts 按首轴 origin 排序
    行走: row 从盒首行出发, 每个 part 必须恰从 row 开始
          origin < row → overlap; origin > row → gap; 走完必须停在盒末行
    len(parts)>1 且给了 min_split_bytes: 每 part 体积×元素字节 ≥ 下限
    返回 [(part 盒, 对应 holder), ...]
```

为什么「与请求盒比对」而不是「parts 互相比对」？`_resolve` 的 docstring（[L661-L666](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/checkpoint/affine_transfer.py#L661-L666)）给出理由：每个 part 按构造只能沿首轴切且其余轴与请求盒一致，所以「从请求首行走到末行」本身就证明了互不相交又无缝隙——不需要两两对比 parts 的声明。

#### 4.4.3 源码精读

`_resolve` 全函数：[deepspeed/checkpoint/affine_transfer.py:L659-L721](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/checkpoint/affine_transfer.py#L659-L721)。分段看：

- int 分支与 holder 校验：[L671-L675](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/checkpoint/affine_transfer.py#L671-L675)。`holders` 字典由该交集的**已验证** holder 帧构成——答案只能在这个集合里挑。
- part 逐项检查：[L680-L699](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/checkpoint/affine_transfer.py#L680-L699)。`tuple(part.logical_origin)` 对 `None` 之类的字段抛 `TypeError`，被就地转成 `InvalidReplicaPolicyError`（L688-L691）——策略的任何畸形输出都被翻译成同一类协议错误而不是让规划器崩栈。
- 「只许切首轴」的检查：[L694-L696](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/checkpoint/affine_transfer.py#L694-L696)。`origin[1:] != box.origin[1:] or shape[1:] != box.extent[1:]` 即拒绝——沿 axis 1 切分（如把 (6,4) 切成左右两半 (6,2)+(6,2)）在这里被拦下。
- 行走与 gap/overlap 判定：[L705-L714](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/checkpoint/affine_transfer.py#L705-L714)。`kind = 'overlap' if part_box.origin[0] < row else 'gap'`——同一个不等式区分两种错误；末尾 `row != 盒末行` 捕获「提前停」与「冲出界」（提前停的报错是 "stops at row ..., inside or past"）。
- `min_split_bytes` 下限：[L715-L720](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/checkpoint/affine_transfer.py#L715-L720)。注意条件 `len(parts) > 1`——未切分的整段永远合法（哪怕小于下限），因为下限约束的是「为均衡而切」的行为：切出小于下限的碎片只会徒增调度开销。
- 预算检查时机：[L628-L636](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/checkpoint/affine_transfer.py#L628-L636)（已在 4.1.3 读过）+ `_check_pair_budget` 本体 [L645-L648](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/checkpoint/affine_transfer.py#L645-L648)。`PlanBudget.max_pair_checks` 的默认值 \(2^{18}\) 定义在 [L83](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/checkpoint/affine_transfer.py#L83)。

拒绝矩阵被完整参数化在测试里：[test_affine_replica_policy.py:L261-L286](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/tests/unit/checkpoint/test_affine_replica_policy.py#L261-L286)——非 holder、`True`、`1.0`、`None`、`'0'`、空列表、非 ReplicaPart、holder 越界、bool location、gap、overlap、提前停、冲出界、沿 axis 1 切、空 part、维度数错、浮点尺寸、两个 `None` 字段，共 19 种答案全部断言抛 `InvalidReplicaPolicyError`。

「预算在展开前检查」的时机证明：[test_affine_replica_policy.py:L360-L373](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/tests/unit/checkpoint/test_affine_replica_policy.py#L360-L373) 定义了一个 `__getitem__` 直接 `raise AssertionError` 的 Sequence——如果规划器先读了 parts 再查预算，测试自己就会炸；它通过说明预算确实只看 `len(answer)`。

#### 4.4.4 代码实践

**实践目标**：亲手触发六类拒绝，记住每类报错的措辞与时机。

**操作步骤**：

```bash
python -m pytest tests/unit/checkpoint/test_affine_replica_policy.py -v -k "refused or required or cannot_be_split or min_split_bytes or budget or arguments"
```

再用示例代码单独复现两类最典型的（改写自 [test_affine_replica_policy.py:L309-L316](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/tests/unit/checkpoint/test_affine_replica_policy.py#L309-L316) 与 [L338-L343](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/tests/unit/checkpoint/test_affine_replica_policy.py#L338-L343)）：

```python
# 示例代码：min_split_bytes 与 pair budget
from deepspeed.checkpoint.affine import contiguous_split_map, replicated_map
from deepspeed.checkpoint.affine_transfer import (InvalidReplicaPolicyError, PlanBudget,
                                                  ReplicaPart, plan_transfers)
pairs = {'w': (contiguous_split_map((6, 4), [6], 0), replicated_map((6, 4), 2))}
split = lambda rs: [[ReplicaPart((0, 0), (2, 4), 0), ReplicaPart((2, 0), (4, 4), 1)]] * len(rs)

plan_transfers(pairs, element_size={'w': 4}, min_split_bytes=32, replica_policy=split)   # 2x4x4=32B, 通过
try:
    plan_transfers(pairs, element_size={'w': 4}, min_split_bytes=33, replica_policy=split)
except InvalidReplicaPolicyError as e:
    print('min_split:', e)

rows = lambda rs: [[ReplicaPart((r, 0), (1, 1), r % 2) for r in range(64)]] * len(rs)
row_pairs = {'w': (contiguous_split_map((64, 1), [64], 0), replicated_map((64, 1), 2))}
plan_transfers(row_pairs, PlanBudget(max_pair_checks=64), replica_policy=rows)           # 恰好 64, 通过
try:
    plan_transfers(row_pairs, PlanBudget(max_pair_checks=63), replica_policy=rows)
except InvalidReplicaPolicyError as e:
    print('budget:', e)
```

**需要观察的现象**：`min_split_bytes=33` 时小 part（32 字节）触发拒绝；`max_pair_checks=63` 时 64 个 part 触发拒绝；而两处边界值（32、64）恰好通过。

**预期结果**：打印两条 `InvalidReplicaPolicyError` 信息（一条含 `under min_split_bytes 33`，一条含 `more than max_pair_checks 63 parts`）；pytest 子集全部 PASSED（该文件 65 个用例均为纯 CPU）。待本地验证。

#### 4.4.5 小练习与答案

**练习 1**：策略返回 `[ReplicaPart((0,0),(2,4),0), ReplicaPart((3,0),(3,4),1)]`（跳过第 2 行）会被哪一行拒绝？报错怎么说？

**答案**：行走循环 [L705-L711](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/checkpoint/affine_transfer.py#L705-L711)：第一个 part 走到 row=2，第二个 part origin[0]=3 ≠ 2 且 3 > 2，判为 `gap`，报 `parts leave a gap at row 2`。若反过来重叠（origin=1 < row=2）则报 `overlap`。

**练习 2**：为什么「未切分的整段答案不受 `min_split_bytes` 约束」是合理设计而不是漏洞？

**答案**：`min_split_bytes` 的语义是「别为了均衡切出太碎的片」——约束的是策略的**切分行为**，不是数据本身的尺寸。单 holder 的区域本来就只能是那个尺寸，拒绝它只会让协议把「没得选」也当成错误。

**练习 3**：`max_pair_checks` 同时约束规划器的哪些内部检查与策略的 parts 数？为什么策略侧的检查要提前到展开之前？

**答案**：规划器侧它约束源区域两两相交检查、holder 覆盖检查、目标×源配对数、覆盖并集检查（上讲已读）；策略侧约束每参数 parts 总数。提前的原因在 [L629-L631](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/checkpoint/affine_transfer.py#L629-L631) 的注释：预算约束的是规划器的工作量，若先展开再查，超长答案（可能几十万个 part）已经白白做了展开工作才被拒绝。

### 4.5 跨 rank 一致性与共享成本输入

#### 4.5.1 概念说明

这一节是整层的理论地基，两句话构成一对此消彼长的约束：

**第一句：任意一致的计划都是精确的（"Any consistent plan is exact"）。**
策略只决定**数据从哪里读**，不决定**送达什么**。副本一致（同一逻辑区域的每个已验证 holder 持有相同字节）时，无论策略选哪个 holder、怎么切分，任何被接受的答案送达的数据都相同。测试文件的 docstring 直接把它归到 affine IR 正确性论证（#8252 证明草案）名下：[test_affine_replica_policy.py:L5-L15](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/tests/unit/checkpoint/test_affine_replica_policy.py#L5-L15)。这就把「策略可以随便选」从鲁莽变成了安全——规划器用 4.4 的校验收口，校验之内自由。

**第二句：所有 rank 必须返回相同答案。**
执行时每个 rank 只发射同一调度中属于自己的那一半（自己持有的源、自己负责的目标），所以所有 rank 必须先对「整张调度」达成一致。执行器把每个 rank 的批量调度摘要（digest）收集比对，不一致直接拒绝：`'ranks disagree about the transfer schedule (batch schedule)'`，见 [affine_transfer_executor.py:L417-L418](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/checkpoint/affine_transfer_executor.py#L417-L418)。`plan_transfers` 的 docstring 原样引用了这条报错（[L556-L560](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/checkpoint/affine_transfer.py#L556-L560)）。

两句合起来的工程推论：**策略的成本输入必须来自全员共享的数据**。如果某个 rank 用自己测的链路带宽做决策而别的 rank 测出不同的数，答案就会分叉、digest 拒绝、同步失败。所以成本感知策略（#8252 的目标）只能吃「每 rank 都一样」的输入——例如 #8966 计划的链路标定（`fit_links`）：先跨 rank 收集测量、拟合成一份共识链路模型，再交给策略。`affine_replica_policy` 的模块 docstring 把这条纪律写成了设计前提（[L13-L15](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/checkpoint/affine_replica_policy.py#L13-L15)）：带状态的策略可以是任何可调用对象，规划器不持有状态；其答案必须在同步的每个 rank 上一致，故成本输入永远不能来自 rank 本地测量。

#### 4.5.2 核心流程

一次真实同步中「规划一致 → 执行一致」的闭环：

```text
所有 rank: 用相同的 pairs + 相同的(确定性)策略各自调 plan_transfers
        │  (策略输入: 全员相同的 maps / element_size / 共享成本模型)
        ▼
所有 rank 得到字节级相同的计划 → 摘要 digest 相同
        │
        ▼
execute_transfers: 各 rank 摘要 all-gather 比对
        ├─ 有 rank 拒绝 → 'a rank rejected the transfer batch before it began'
        ├─ digest 不一致 → 'ranks disagree about the transfer schedule (batch schedule)'
        └─ 一致 → 各 rank 发射自己那一半(本地源→远端目标 / 远端源→本地目标)
```

#### 4.5.3 源码精读

随机策略测试是「任意一致的计划都是精确的」的机器化陈述：[test_affine_replica_policy.py:L77-L110](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/tests/unit/checkpoint/test_affine_replica_policy.py#L77-L110)。`_random_policy` 对每个请求或随机选 holder、或沿 axis 0 随机切 1~3 刀并给每刀随机 holder（还把 parts 顺序打乱——校验会重排序，所以乱序答案也应被接受）；`test_every_accepted_answer_moves_the_same_data` 跑 24 个种子，每个计划经 `_apply_plan` 在朴素缓冲上执行后断言两件事：目标分片与「`extract` 直取的参考分片」逐元素相等（`torch.equal`，fp64），且**每个元素恰好被写一次**（writes 计数全 1）——「精确」一词的可执行含义。

`_apply_plan`/`_assert_moves_exactly` 这两个测试脚手架：[test_affine_replica_policy.py:L40-L60](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/tests/unit/checkpoint/test_affine_replica_policy.py#L40-L60)。注释明确它们「是测试脚手架，绝不是生产路径」——生产执行走 executor 的轮次协议与 staging 预算，而这里用 `torch.as_strided` + 写计数在单进程内模拟，正适合用来验证「计划语义」本身。

执行侧的 digest 执法：[deepspeed/checkpoint/affine_transfer_executor.py:L414-L418](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/checkpoint/affine_transfer_executor.py#L414-L418)。`decisions` 是各 rank 对「本批调度摘要 + 是否拒绝」的表态；任何一个 rank 拒绝则整批不动数据；摘要集合大小不为 1 即分叉。这是「规划层要求一致」在执行层的对应物——两层共同把「一致」变成硬约束而非君子协定。

文档中对应的一句契约：[docs/code-docs/source/affine-transfer.rst:L71-L75](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/docs/code-docs/source/affine-transfer.rst#L71-L75)——「Every rank must return the same answers, so cost inputs must come from data every rank shares, such as a link calibration gathered across ranks, never from a rank-local measurement.」

#### 4.5.4 代码实践

**实践目标**：用随机策略复现「任意一致的计划都是精确的」，并设计一个「会分叉」的策略理解 digest 拒绝的必然性。

**操作步骤**（示例代码，改写自 [test_affine_replica_policy.py:L102-L110](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/tests/unit/checkpoint/test_affine_replica_policy.py#L102-L110)）：

```python
# 示例代码：随机策略精确性
import random, torch
from deepspeed.checkpoint.affine import contiguous_split_map, replicated_map
from deepspeed.checkpoint.affine_transfer import ReplicaPart, plan_transfers

def random_policy(rng):
    def policy(requests):
        answers = []
        for r in requests:
            locs = [c.source_rank for c in r.candidates]
            rows = r.shape[0] if r.shape else 1
            if not r.shape or rows == 1 or rng.random() < 0.4:
                answers.append(rng.choice(locs)); continue
            cuts = sorted(rng.sample(range(1, rows), rng.randint(1, min(3, rows - 1))))
            starts, ends = [0] + cuts, cuts + [rows]
            parts = [ReplicaPart((r.logical_origin[0] + s,) + r.logical_origin[1:],
                                 (e - s,) + r.shape[1:], rng.choice(locs))
                     for s, e in zip(starts, ends)]
            rng.shuffle(parts)                # 乱序也应被接受(校验会重排)
            answers.append(parts)
        return answers
    return policy

pairs = {'rows': (contiguous_split_map((10, 4), [3, 7], 0), replicated_map((10, 4), 2))}
plans = plan_transfers(pairs, replica_policy=random_policy(random.Random(7)))
print(plans['rows'].statistics)               # 观察 segments_before_merge vs segments
```

然后做一个思想实验（源码阅读型实践）：写一个策略 `lambda rs: [hash(r) % 2 and r.candidates[0].source_rank or r.candidates[-1].source_rank for r in rs]`——它在本进程内「看似确定」，但若不同 rank 对同一逻辑张量的 `hash` 顺序或候选排序有任何分歧就会给出不同答案。对照 [affine_transfer_executor.py:L417-L418](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/checkpoint/affine_transfer_executor.py#L417-L418) 回答：这个策略在真实多 rank 同步中会死在哪一步？

**需要观察的现象**：随机策略（含切分、乱序）生成的计划能正常返回；`statistics` 中 `segments_before_merge`（发射前候选段数，含切分产生的多段）大于等于 `segments`（合并后）——`_coalesce` 把相邻可拼的段重新拼起来了。

**预期结果**：`plan_transfers` 无异常返回；若进一步套用测试里的 `_assert_moves_exactly`（用 `source_map.extract(full, rank)` 做参考），24 个种子下目标分片全部逐元素相等且写计数全 1——这就是「精确」。思想实验的答案见下面练习 2。待本地验证。

#### 4.5.5 小练习与答案

**练习 1**：副本**不一致**时（某 holder 的数据坏了），「任意一致的计划都是精确的」还成立吗？协议如何兜底？

**答案**：不成立——该定理的前提就是副本一致。协议的兜底在于：holder 集合来自 `_validate_holders` 的「已验证」几何（每个 holder 恰好覆盖该逻辑盒），覆盖的是**位置**正确性；字节一致性属于上层（检查点校验/同步前的数据一致性保障），规划器管不到也不假装管到。这正是 docstring 把责任划在「what arrives is fixed by the maps」的原因。

**练习 2**（思想实验答案）：依赖 `hash` 的策略会在哪一步失败？

**答案**：在执行器的摘要比对处失败——各 rank 算出的答案不同 → 计划不同 → digest 不同 → `ranks disagree about the transfer schedule (batch schedule)`，整批在数据移动之前被拒绝（这是安全的失败：目标未被污染）。教训：策略的每一步只能依赖全员相同的输入。

**练习 3**：为什么 `local_first_least_loaded` 在多 rank 下天然一致，而「选本 rank 最近的 holder」不行？

**答案**：`local_first_least_loaded` 的输入只有请求序列（全员相同）+ 全员相同的账本演化规则（固定顺序 + 字典序），没有任何「我这个 rank 是谁」的信息——它模拟的是全图视角。而「离我最近」以 rank 本地身份为输入，不同 rank 答案必然不同。判断一个策略能否安全上多 rank，就看它的决策能否在「无身份的旁观者」手里重放出来。

## 5. 综合实践

把五个模块串成一个完整的实验脚本（纯 CPU，无分布式依赖），目标是走通从「构造分片」到「自定义策略」到「拒绝矩阵」的全链路。仓库根目录创建 `batch_policy_lab.py`：

```python
# 示例代码：本讲综合实践
import random
import torch

from deepspeed.checkpoint.affine import contiguous_split_map, replicated_map
from deepspeed.checkpoint.affine_transfer import (InvalidReplicaPolicyError, PlanBudget, ReplicaPart,
                                                  plan_transfer, plan_transfers)
from deepspeed.checkpoint.affine_replica_policy import first_holder, local_first_least_loaded, per_request

def product(shape):
    n = 1
    for s in shape:
        n *= s
    return n

def apply_plan(plan, target_map, source_shards):     # 改写自测试脚手架 _apply_plan
    targets = {r: (torch.full((product(s),), -12345.0, dtype=torch.float64),
                   torch.zeros(product(s), dtype=torch.int64)) for r, s in target_map.shard_shapes.items()}
    for seg in plan.segments:
        src = torch.as_strided(source_shards[seg.source_rank].reshape(-1), seg.shape,
                               seg.source_strides, seg.source_offset)
        values, writes = targets[seg.target_rank]
        torch.as_strided(values, seg.shape, seg.target_strides, seg.target_offset).copy_(src)
        torch.as_strided(writes, seg.shape, seg.target_strides, seg.target_offset).add_(1)
    return targets

def assert_exact(plan, source_map, target_map, full):
    sources = {r: source_map.extract(full, r).contiguous() for r in source_map.shard_shapes}
    for rank, (values, writes) in apply_plan(plan, target_map, sources).items():
        assert torch.equal(values.view(target_map.shard_shapes[rank]), target_map.extract(full, rank))
        assert bool(torch.all(writes == 1)), f'rank {rank} 写计数异常'

pairs = {
    'dense': (contiguous_split_map((12, 6), [2, 4], 1), replicated_map((12, 6), 3)),
    'rows':  (contiguous_split_map((10, 4), [3, 7], 0), replicated_map((10, 4), 2)),
}

# 第 1 步：等价性——批量层没有改变单参数行为
plans = plan_transfers(pairs, replica_policy=first_holder)
for name, (t, s) in pairs.items():
    assert plans[name] == plan_transfer(t, s), name

# 第 2 步：随机策略（随机 holder + axis 0 随机切分 + 乱序 parts）——任意一致的计划都是精确的
def random_policy(rng):
    def policy(requests):
        out = []
        for r in requests:
            locs = [c.source_rank for c in r.candidates]
            rows = r.shape[0] if r.shape else 1
            if not r.shape or rows == 1 or rng.random() < 0.4:
                out.append(rng.choice(locs)); continue
            cuts = sorted(rng.sample(range(1, rows), rng.randint(1, min(3, rows - 1))))
            parts = [ReplicaPart((r.logical_origin[0] + a,) + r.logical_origin[1:],
                                 (b - a,) + r.shape[1:], rng.choice(locs))
                     for a, b in zip([0] + cuts, cuts + [rows])]
            rng.shuffle(parts)
            out.append(parts)
        return out
    return policy

for seed in range(24):
    plans = plan_transfers(pairs, replica_policy=random_policy(random.Random(seed)))
    for name, (t, s) in pairs.items():
        torch.manual_seed(seed)
        assert_exact(plans[name], s, t, torch.randn(tuple(t.logical_shape), dtype=torch.float64))

# 第 3 步：自定义负载均衡策略（轮流指派 + 本地优先），对照默认策略的分布
def round_robin_local(requests):
    turn, answers = 0, []
    for r in requests:
        local = [c.source_rank for c, p in zip(r.candidates, r.candidate_processes)
                 if p is not None and p == r.target_process]
        if local:
            answers.append(min(local)); continue
        multi = [c.source_rank for c in r.candidates]
        answers.append(multi[turn % len(multi)]); turn += 1
    return answers

plans = plan_transfers(pairs, replica_policy=round_robin_local)
default = plan_transfers(pairs, replica_policy=local_first_least_loaded)
for name in pairs:
    print(name, 'round_robin:', sorted({s.source_rank for s in plans[name].segments}),
          'default:', sorted({s.source_rank for s in default[name].segments}))

# 第 4 步：拒绝矩阵——五类非法答案
bad = {
    '非候选 holder': lambda rs: [9] * len(rs),
    '沿 axis 1 切分': lambda rs: [[ReplicaPart((0, 0), (6, 2), 0), ReplicaPart((0, 2), (6, 2), 1)]] * len(rs),
    'gap': lambda rs: [[ReplicaPart((0, 0), (2, 4), 0), ReplicaPart((3, 0), (3, 4), 1)]] * len(rs),
    '小于 min_split_bytes': None,   # 需要单独传参, 见下
    '超出 pair budget': lambda rs: [[ReplicaPart((r, 0), (1, 1), r % 2) for r in range(64)]] * len(rs),
}
one = {'w': (contiguous_split_map((6, 4), [6], 0), replicated_map((6, 4), 2))}
for label, policy in bad.items():
    if label == '小于 min_split_bytes':
        try:
            plan_transfers(one, element_size={'w': 4}, min_split_bytes=10**9,
                           replica_policy=lambda rs: [[ReplicaPart((0, 0), (2, 4), 0),
                                                       ReplicaPart((2, 0), (4, 4), 1)]] * len(rs))
        except InvalidReplicaPolicyError as e:
            print(f'[{label}] {e}')
        continue
    rows_pair = {'w': (contiguous_split_map((64, 1), [64], 0), replicated_map((64, 1), 2))} \
        if label == '超出 pair budget' else one
    kwargs = {'limits': PlanBudget(max_pair_checks=63)} if label == '超出 pair budget' else {}
    try:
        plan_transfers(rows_pair, replica_policy=policy, **kwargs)
    except InvalidReplicaPolicyError as e:
        print(f'[{label}] {e}')
print('batch policy lab done')
```

（`PlanBudget` 经 `plan_transfers` 的 `limits` 形参传入，见 [L530-L537](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/deepspeed/checkpoint/affine_transfer.py#L530-L537) 的签名。）

**实践目标**：一站式验证本讲五条主线——批量/单参数等价、随机策略精确性、自定义策略的「本地优先 + 跨进程均衡」、五类拒绝各自的报错措辞与拒绝时机。

**需要观察的现象与预期结果**：前两步无断言失败（24 个种子全部「逐元素相等 + 写计数全 1」）；第 3 步打印两种策略对每个参数用到的源 location 集合（复制源下二者通常不同——`round_robin_local` 轮流、默认策略贪心最少负载）；第 4 步打印五条 `InvalidReplicaPolicyError`，措辞分别含 `not a validated holder`、`only cuts along axis 0 are supported`、`leave a gap at row 2`、`under min_split_bytes`、`more than max_pair_checks 63 parts`。全部可在 CPU 完成；本讲义编写时未代跑，标注待本地验证。跑完后可再用 `python -m pytest tests/unit/checkpoint/test_affine_replica_policy.py -q` 对照官方 65 个用例。

## 6. 本讲小结

- **分层**：`plan_transfers` 在 `plan_transfer` 之上只加了「收集排序请求 → 一次调用策略 → 逐答案校验」三件事，`_prepare`/`_emit` 原样复用，单参数行为零变化（由等价性测试固化）。
- **协议**：`ReplicaRequest` 按固定顺序（参数、目标 location、origin、extent）携带全部已验证 holder；location 与进程是两个命名空间，靠 `source_processes`/`target_processes` 翻译；单 holder 区域也入列，因为它的负载是策略搬不动的负载。
- **三策略**：`first_holder` 复刻单参数默认；`local_first_least_loaded`（批量默认）本地优先 + 最少负载贪心 + 平手取最小 location + 从不切分；`per_request` 把旧选择器零改动包进批量世界。
- **校验**：一请求一答、只能选已验证 holder、parts 必须沿 axis 0 精确平铺（gap/重叠/越界/非整数/空 part 全拒）、`min_split_bytes` 只约束切分、`max_pair_checks` 在展开 parts 前按答案长度先拒——违例一律 `InvalidReplicaPolicyError`，一个 segment 都不发射。
- **定理**：任意一致的计划都是精确的——策略只决定从哪读、不决定送达什么；副本一致时一切被接受的答案等价，随机策略 × 24 种子的「逐元素相等 + 写计数全 1」测试是它的机器化陈述。
- **纪律**：所有 rank 必须返回相同答案（executor 的 digest 比对是执法者），因此成本输入必须来自全员共享的数据（如 #8966 的链路标定 `fit_links`）而非 rank 本地测量——这一层就是 #8252 成本感知策略的接入点。

## 7. 下一步学习建议

1. **向下落地执行**：本讲止步于「计划」。读 [tests/unit/checkpoint/test_affine_transfer_executor.py](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/tests/unit/checkpoint/test_affine_transfer_executor.py) 与 [test_affine_transfer_executor_external_group.py](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/tests/unit/checkpoint/test_affine_transfer_executor_external_group.py)，看 `execute_transfers` 如何用 `TransferRequest` 把多个参数的计划打包成 peer 消息、digest 共识如何与 `StagingBudget` 配合。
2. **向上找调用方**：`Grep` 检索仓库中 `plan_transfers` 的调用点，跟踪「一次同步」如何收集 `pairs`、如何为两侧映射各建 `location → 进程` 表——把本讲的协议放进真实的权重迁移（如 AutoEP 换布局）语境。
3. **写一个真正的成本感知策略**：以 `local_first_least_loaded` 为模板，构造一份假想的共享链路模型（例如 `{(进程对): 带宽}`），实现「最小完成时间」指派；用 4.5 的「无身份旁观者可重放」标准自检它在多 rank 下是否一致。这是 #8252 方向的最小原型。
4. **回读契约文档**：[docs/code-docs/source/affine-transfer.rst](https://github.com/deepspeedai/DeepSpeed/blob/84679e9d3a2582bffc53a5259d13cfc7278c9772/docs/code-docs/source/affine-transfer.rst) 的 "Transport and staging" 一节预告了零拷贝传输（GPUDirect RDMA scatter/gather）落地时线上契约的可能变化——理解规划层为何刻意不依赖任何具体传输。
