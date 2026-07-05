# 内存序 Token 排序

## 1. 本讲目标

本讲承接 u6-l2「数据流分析与整除性传播」——那一讲产出的 `dataflow_result`（别名集 `alias_set` 等）会被本讲的 `token_order_pass` 直接消费。学完本讲你应当能够：

1. 说清**为什么 cuTile 必须在编译期为访存操作显式定序**，否则会在 GPU 内存模型下出错。
2. 描述一条 **memory token chain（内存 token 链）** 是如何用 `MakeToken` / `JoinTokens` 把看似无依赖的 load/store 串成禁止重排的有向边的。
3. 读懂 `token_order_pass` 的两阶段结构：先做**块内存效应分析**，再做**token 穿线**，并能跟踪线性块、循环、分支里的 token 传播。
4. 理解**循环并行 store 优化**（LayerNorm/RMSNorm 模式）为何能把串行 store 解放成并行。
5. 知道调试开关 `CUDA_TILE_TESTING_DISABLE_TOKEN_ORDER` 的作用与风险。

## 2. 前置知识

- **Tile IR 的 SSA 风格**（u5-l5）：纯计算操作之间靠 `Var` 的 def-use 自然形成数据依赖，后端无法打乱。但**访存操作之间没有这种 SSA 边**——两次 `ct.store` 写同一数组，谁先谁后在 IR 里看不出来。
- **GPU 内存模型**（u4-l3、docs/source/memory_model.rst）：cuTile 允许编译器与硬件为性能重排访存，**跨 block 的访存顺序默认不保证**；即便在一个 kernel 内部，若没有显式依赖边，后端也可能重排。
- **别名集 `alias_set`**（u6-l2）：dataflow_analysis 给每个数组指针打的一个位掩码整数（`AliasSet = int`），两个数组「可能指向同一片显存」时它们的位会重叠；`ALIAS_UNIVERSE = -1` 表示「可能别名于一切」（全 1）。本讲用 `alias_set` 判断两次访存**是否可能冲突**。
- **`MemoryEffect`**（u5-l5 / ir.py）：每个访存操作声明自己的副作用等级 `NONE < LOAD < STORE`。
- **携带值（carried values）**（u5-l4）：循环体内被改写的变量会变成循环的入口参数与结果，逐轮回传。token 也是一种会被携带的「值」。

> 关键直觉：**纯计算的顺序由数据依赖保证，访存的顺序由 token 链保证。** token 就是 cuTile 给访存操作人为添加的那条「禁止重排」边。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [_passes/token_order.py](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/token_order.py) | 本讲主角：`token_order_pass` 及其全部辅助函数（块内存效应分析、token 穿线、循环并行 store）。 |
| [_ir/ops.py](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_ir/ops.py) | 定义带 token 的访存操作（`TileLoad`/`TileStore`/`LoadPointer`/`StorePointer`/`TileAtomic*`）以及 `MakeToken`/`JoinTokens` 两个 token 工具操作。 |
| [_ir/ops_utils.py](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_ir/ops_utils.py) | `memory_order_has_acquire` / `memory_order_has_release`：判断一次访存的内存序是否携带 acquire/release 语义。 |
| [_ir/ir.py](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_ir/ir.py) | `MemoryEffect` 枚举（`NONE<LOAD<STORE`）。 |
| [_passes/dataflow_analysis.py](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/dataflow_analysis.py) | 提供 `DataflowResult` / `alias_set` / `ALIAS_UNIVERSE`，是 token 排序的输入。 |
| [_compile.py](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_compile.py) | `_transform_ir` 调用 `token_order_pass`，并受调试开关门控。 |
| [_debug.py](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_debug.py) | `CUDA_TILE_TESTING_DISABLE_TOKEN_ORDER` 开关。 |
| [test/test_token_order.py](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/test/test_token_order.py) | 用 filecheck 锁定 token 链形状的回归测试，是理解本讲最直观的材料。 |

---

## 4. 核心概念与源码讲解

本讲拆成 5 个最小模块：4.1 token 与定序的直觉；4.2 pass 的整体结构与数据骨架；4.3 线性块与控制流里的 token 传播；4.4 循环并行 store 优化；4.5 关闭 token 排序的调试开关。

### 4.1 内存 token 链：为什么访存需要定序

#### 4.1.1 概念说明

考虑这段内核：

```python
ct.store(X, index=(0,), tile=a)   # 写 X
b = ct.gather(X, ...)             # 读 X（可能读到刚才写的值）
ct.store(X, index=(0,), tile=b)   # 再写 X
```

三条语句在 Tile IR 里是三个独立的 `TileStore`/`LoadPointer` 操作。它们之间**没有 `Var` 的数据依赖**（`gather` 的输入是数组 `X` 本身，不是上一条 store 的结果）。于是后端在调度到硬件时，理论上可以把它们任意重排——但语义上这是一段「写—读—写」的串行序列，重排就会读错、写错。

纯计算不会出这个问题：`c = a + b` 必须等 `a`、`b` 就绪，SSA 的 def-use 边天然定序。**访存缺的正是这条边。** cuTile 的解决办法是给每个访存操作配一个「token」：

- 每个访存操作有一个**输入 token**（`token` 操作数）和一个**输出 token**（结果的第二个返回值）。
- 后端语义规定：**一个访存操作必须等它的输入 token 就绪后才能执行**。
- 把 op N 的输出 token 接到 op N+1 的输入 token 上，就形成一条「必须按序执行」的链——这就是 **memory token chain（内存 token 链）**。

token 是一种特殊的 SSA 值，类型为 `TokenTy`，由两个工具操作产生：

- `MakeToken`：凭空造出一个「根 token」（链的起点）。
- `JoinTokens`：把多个 token 合并成一个（扇入），表示「要等这若干条链全部就绪」。

#### 4.1.2 核心流程

一条线性 token 链的构造流程：

1. 在块首插入一个 `MakeToken`，得到根 token `t0`。
2. 对第 1 个访存操作：把它的 `token` 操作数设为 `t0`，它产出一个新 token `t1`。
3. 对第 2 个访存操作：把它的 `token` 设为 `t1`，产出 `t2`……
4. 如此穿线，直到块末。

若某步需要汇合多条链（例如一个 store 必须等「上一条 store」和「上一条 load」都完成），就插入一个 `JoinTokens` 把多条链并为一条，再喂给该操作的输入 token。

用伪代码描述「op 的输出 token 流向下一个 op 的输入 token」这一核心不变量：

\[
\text{token}_{\text{in}}(\text{op}_{i+1}) \leftarrow \text{token}_{\text{out}}(\text{op}_{i})
\]

后端只要尊重「输入 token 就绪才能执行」这一条，就不敢把链上的访存打乱。

#### 4.1.3 源码精读

带 token 的访存操作以 `TileLoad` 为例。它有一个可选的 `token` 输入操作数，且结果元组的第二项是一个 token：

[ops.py:873-881](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_ir/ops.py#L873-L881) —— `TileLoad` 声明 `memory_effect=LOAD`，第 881 行的 `token: Optional[Var] = operand(default=None)` 就是输入 token；它的 `result_vars` 是 `(tile, token)` 两个值，第二个是输出 token。`TileStore`、`LoadPointer`、`StorePointer`、`TileAtomic*` 结构相同，故统称 **tko（token-in/token-out）操作**。

两个 token 工具操作：

[ops.py:1692-1702](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_ir/ops.py#L1692-L1702) —— `MakeToken`，opcode `make_token`，无操作数，产出一个 `TokenTy` 值，用于链的起点。

[ops.py:1705-1717](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_ir/ops.py#L1705-L1717) —— `JoinTokens`，`tokens` 是一组输入 token，产出一个合并后的 token。

#### 4.1.4 代码实践

**实践目标**：在 dump 出的字节码/MLIR 中亲眼看一条最短的 token 链。

**操作步骤**：

1. 写一个最简单的 load→store 内核：

   ```python
   import cuda.tile as ct
   @ct.kernel
   def k(X, TILE: ct.Constant[int]):
       tx = ct.load(X, index=(0,), shape=(TILE,))
       ct.store(X, index=(0,), tile=tx)
   ```

2. 参考 [test/test_token_order.py:111-126](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/test/test_token_order.py#L111-L126) 里 `get_bytecode` 的用法，或设置 `CUDA_TILE_DUMP_TILEIR=1` 跑一次 launch，得到 MLIR/cutileir 文本。

3. 在产物里搜索 `make_token`、`load_view_tko`、`join_tokens`、`store_view_tko`。

**需要观察的现象**：应当看到形如 `NoControlFlowCheckDirective` 描述的序列（[test_token_order.py:98-108](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/test/test_token_order.py#L98-L108)）：

```
%t0 = make_token
%v1, %t1 = load_view_tko ... token=%t0
%t3 = join_tokens %t0, %t1
%t6 = store_view_tko ... token=%t3
```

`store` 的 `token` 正是 `load` 之后那条 join 出来的 `%t3`，于是 store 必须晚于 load——这就是「写读定序」。

**预期结果**：store 的输入 token 能沿着 join 回溯到 load 的输出 token 与根 token。**待本地验证** dump 文本格式细节（不同版本字段顺序可能略有差异）。

#### 4.1.5 小练习与答案

**练习 1**：如果把上面内核的 `ct.store(...)` 删掉，只剩一个 `ct.load`，dump 里还会有 `join_tokens` 吗？

> **答案**：会有。load 分支会「eagerly」把自己的结果 token 与 `last_op` token 用 `JoinTokens` 合并，更新 `last_op`，为后续可能出现的访存留好接口（详见 4.3）。

**练习 2**：为什么纯计算操作（如 `ct.add`）不需要 token？

> **答案**：纯计算的结果是其操作数的纯函数，操作数 `Var` 的 def-use 边已经强制了执行顺序；只有访存才缺这条边，所以才需要 token。

---

### 4.2 token_order_pass 的整体结构与数据骨架

#### 4.2.1 概念说明

`token_order_pass` 不是「无脑把所有访存串成一条链」——那会抹掉所有可并行的机会（两个互不冲突的 load 完全可以并行）。它要做的更精细：

- **按别名集（alias_set）分链**：只对「可能访问同一片显存」的访存互相定序；指向不同数组的访存各走各的链，互不阻塞。
- **区分「上一条任意访存」与「上一条 store」**：load 只需等上一条 **store**（RAW，读后写 hazard），而 store 要等上一条**任意访存**（WAR/WAW/RAW 都要防）。这让「连续多个 load 同一数组」能并行。
- **尊重 acquire/release 语义**：带 acquire 的读、带 release 的写会引入跨链的同步。

为此，pass 先做一遍**块内存效应分析**，给每个 `Block`（含循环体、分支体）打一张「该块对每个 alias_set 产生了什么效应、是否含 acquire」的汇总表；再带着这张表做 **token 穿线**。

#### 4.2.2 核心流程

pass 的入口只有四步（[token_order.py:102-110](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/token_order.py#L102-L110)）：

```
1. _get_block_memory_effects(root_block, ...)   # 自底向上汇总每个块的内存效应
2. context = TokenOrderContext(dataflow_result, block_memory_effects)
3. root_tok = make_token_var(...)               # 链的根 token
4. token_map = defaultdict(lambda: root_tok)    # 每个 (alias_set, role) → 当前 token，缺省回退到根
5. _to_token_order_in_block(root_block, context, token_map)   # 递归穿线
6. 块首插入 MakeToken(root_tok)
```

核心数据结构是 **token_map**：它的键是 `TokenKey`，值是当前那条链的「末端 token」。键有两类：

- `AliasTokenKey(alias_set, role)`：某个别名集上的 token，`role ∈ {LAST_OP, LAST_STORE}`；
- `ACQUIRE_TOKEN_KEY`：一个全局哨兵，记录「最近一次带 acquire 语义的访存产生的 token」，用于跨链同步。

`MemoryEffects` 把「一个块对若干 alias_set 的效应」打包成一张有序字典，并支持 `|` 合并（取每条 alias 的更强效应、acquire 位取或）——[token_order.py:26-50](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/token_order.py#L26-L50)。

`TokenRole` / `AliasTokenKey` / `TokenKey` 的定义在 [token_order.py:64-75](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/token_order.py#L64-L75)。

#### 4.2.3 源码精读

块内存效应分析 [_get_block_memory_effects](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/token_order.py#L125-L154)（L125–L154）：遍历块内每个操作，查它的 `memory_effect` 与（对 `TileLoad`/`TileStore`/原子类）`memory_order_has_acquire`，按「输入数组指针的 `alias_set`」归类，用 `|` 累加；并递归把嵌套块的效应并入父块（L150–L152）。`TileAssert` 被显式忽略（L131），`TilePrintf` 被挂到 `ALIAS_UNIVERSE`（L134–L136，保守定序打印）。

`MemoryEffect` 枚举本身定义在 [ir.py:252-258](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_ir/ir.py#L252-L258)，注释明确「`NONE < LOAD < STORE`」，这正是 `|` 合并时取 `max` 的依据。

acquire/release 判定在 [ops_utils.py:201-206](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_ir/ops_utils.py#L201-L206)：`ACQUIRE`/`ACQ_REL` 算有 acquire，`RELEASE`/`ACQ_REL` 算有 release。

别名集来源在 [dataflow_analysis.py:27-29](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/dataflow_analysis.py#L27-L29)：`AliasSet = int`，`ALIAS_UNIVERSE = -1`（全 1 掩码，表示「可能与任意数组别名」）。两个集合是否重叠就是按位与：\( a \& b \neq 0 \)。

#### 4.2.4 代码实践

**实践目标**：确认 pass 的执行时机与输入依赖。

**操作步骤**：阅读 [_compile.py:95-119](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_compile.py#L95-L119) 里的 `_transform_ir`，按出现顺序列出每个 pass，并标注哪些 pass 的输出会被 `token_order_pass` 使用。

**需要观察的现象**：调用顺序是 `eliminate_assign → DCE → dataflow → add_divby → token_order → rewrite → hoist → ...`。`token_order_pass` 紧跟 `add_divby_pass`，二者**共享同一个 `dataflow_result`**；而 `hoist_loop_invariants` 被**刻意安排在 token_order 之后**（注释 L110–L111 解释：否则可能把 load 错误地外提到循环外）。

**预期结果**：你能用一句话说明「为什么 dataflow 必须在 token_order 之前，而 hoist 必须在 token_order 之后」。这是本模块的验收点。

#### 4.2.5 小练习与答案

**练习 1**：`token_map` 为什么用 `defaultdict(lambda: root_tok)`？

> **答案**：kernel 里未必每个 alias_set 都已有访存；当一个新访存引用的 alias_set 此前没出现过时，它的「上一条」应当回退到根 token `t0`（即「没有需要等待的前驱」）。defaultdict 让这条回退天然成立。

**练习 2**：`MemoryEffects.__or__` 为什么对 acquire 位用「或」、对每个 alias 的 effect 用 `max`？

> **答案**：合并两段代码的内存效应时，只要其中任一段含 acquire，合并体就必须被视为含 acquire（保守）；效应强度取更强者（`STORE > LOAD > NONE`），保证后续定序不遗漏冲突。

---

### 4.3 线性块与控制流里的 token 传播

#### 4.3.1 概念说明

`_to_token_order_in_block` 是 pass 的心脏。它遍历块内每个操作并分派：

- **访存操作**（`TileLoad`/`LoadPointer`/`TileStore`/`StorePointer`/原子类）：算出它该等哪些 token（`_get_input_token`），用 `dataclasses.replace` 把 `token` 操作数填进去，并更新 `token_map` 里对应键指向新产出的 token。
- **`Loop`**：循环体可能反复访存，token 必须像普通携带值一样「进循环体—每轮回传—出循环」。按体块的 `MemoryEffects` 给每个受影响的 `(alias_set, role)` 新增一对「入口参数 token / 结果 token」，递归处理体块，最后用结果 token 覆盖外层 `token_map`。
- **`IfElse`**：合并 then/else 两支的内存效应，给受影响的键新增结果 token；两支各自从外层 `token_map` 的副本出发独立穿线，汇合后用结果 token 覆盖外层。
- **`Continue`/`Break`/`EndBranch`**：把退出点的若干 token 追加到这些终止符的 `values`/`outputs` 里，让它们随控制流回流。

#### 4.3.2 核心流程

**load 分支**（[token_order.py:168-193](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/token_order.py#L168-L193)）：

1. 取该数组指针的 `alias_set`，定位 `last_store_key` 与 `last_op_key`。
2. `input_tok = _get_input_token(last_store_key, ...)` —— load **只等「上一条 store」**（重叠 alias_set 的 last_store），不等上一条 load，故连续 load 可并行。
3. 用 `replace(op, token=input_tok)` 落实输入 token。
4. **eagerly** 把 `last_op` 与本 load 的结果 token `JoinTokens` 合并，更新 `last_op`（注意：load **不更新** `last_store`）。
5. 若该 load 带 acquire，把 `ACQUIRE_TOKEN_KEY` 指向其结果 token。

**store/原子分支**（[token_order.py:195-243](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/token_order.py#L195-L243)）：`input_tok = _get_input_token(last_op_key, ...)` —— store 等「上一条**任意**访存」（重叠 alias_set 的 last_op），即同时防 WAR/WAW/RAW；写完后 `last_op` 与 `last_store` **都**更新为该 store 的结果 token。原子类同 store，但还按 acquire 语义更新 `ACQUIRE_TOKEN_KEY`。

**该 join 哪些 token** 由 [_collect_join_tokens](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/token_order.py#L396-L421)（L396–L421）决定，三条规则：

- `ACQUIRE_TOKEN_KEY` 总是被合并（acquire 要同步所有先前的 acquire）。
- 若本操作带 release 且对方是 `LAST_OP` → 合并（release 同步一切先前访存）。
- 若对方与本键 **role 相同且 alias_set 按位与非零** → 合并（可能冲突）。

`_get_input_token`（[token_order.py:424-436](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/token_order.py#L424-L436)）只在「需要合并多个 token」时才插一个 `JoinTokens`，单个则直接复用，避免冗余操作。

**控制流携带**：循环里 `append_new_carried_var`（[token_order.py:252-258](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/token_order.py#L252-L258)）同时追加「初始值 / 体块入口参数 / 结果变量」三个 token Var，使 token 像普通携带值一样流过 `Loop`。`Continue`/`Break` 用 [_get_cf_exit_tokens](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/token_order.py#L439-L456)（L439–L456）收集退出时应带走的 token，追加到终止符。

#### 4.3.3 源码精读

load 分支核心：[token_order.py:168-193](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/token_order.py#L168-L193)。注意 L174 的 `_get_input_token(last_store_key, ...)`（load 只等 store），与 L186–L190 的「eager join 更新 last_op」。

store 分支核心：[token_order.py:206-220](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/token_order.py#L206-L220)。注意 L211 的 `_get_input_token(last_op_key, ...)`（store 等任意访存），与 L219–L220 同时更新 `last_op` 和 `last_store`。

Loop 处理：[token_order.py:245-297](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/token_order.py#L245-L297)。L262–L281 按 `body_mem_effects` 给 LOAD 效应加 1 个携带 token、给 STORE 效应加 2 个（last_op + last_store）；L279–L281 单独处理 acquire。

IfElse 处理：[token_order.py:311-351](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/token_order.py#L311-L351)。L315 合并两支效应，L325–L337 为受影响键分配结果 token，L343–L346 两支各自递归。

Continue/Break/EndBranch：[token_order.py:299-357](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/token_order.py#L299-L357)，把退出 token 追加进终止符的 `values`/`outputs`。

#### 4.3.4 代码实践

**实践目标**：用一个「读—改—写同一数组」的内核验证 store 必须晚于 load。

**操作步骤**：

1. 编写内核（取自测试套件的 `store_buffer` 思路，[test_token_order.py:20-27](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/test/test_token_order.py#L20-L27)）：

   ```python
   @ct.kernel
   def flip(X, TILE: ct.Constant[int]):
       ct.store(X, index=(0,), tile=ct.arange(TILE, dtype=X.dtype))
       rev = ct.arange(TILE, start=TILE - 1, step=-1, dtype=np.int32)
       tx = ct.gather(X, rev)        # 必须读到上一条 store 的结果
       ct.store(X, index=(0,), tile=tx)
   ```

2. dump 出字节码，对照 `NoControlFlowCheckDirective`（[test_token_order.py:98-108](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/test/test_token_order.py#L98-L108)）。

3. 运行内核：把 `X` 初始化为 0，结果应是 `arange` 的翻转。

**需要观察的现象**：dump 中第二条 `store_view_tko`（或 `store_ptr_tko`）的 `token` 必须能沿 `join_tokens` 回溯到第一条 store 的输出 token 与 gather 的输出 token——形成「store₁ → gather → store₂」的链。

**预期结果**：内核数值正确（数组被翻转一次）。若关闭 token 排序（见 4.5）再跑，**可能**出现 gather 读到翻转前的旧值——这就是无定序时的正确性风险（**待本地验证**，是否出错取决于后端实际是否重排）。

#### 4.3.5 小练习与答案

**练习 1**：连续两个 `ct.load` 同一数组，第二个 load 的输入 token 来自哪里？它会被强制晚于第一个 load 吗？

> **答案**：来自该 alias_set 的 `last_store`（而不是 `last_op`）。因为 load 不更新 `last_store`，第二个 load 的输入与第一个 load **无关**，所以两个 load **可以并行**；只有第一个 load 之后 eagerly 更新的 `last_op` 不影响后续 load 的输入。

**练习 2**：一个 `IfElse` 两支分别 store 了不同数组，汇合后外层的 `last_store` 会被怎样更新？

> **答案**：`merged_mem_effects` 取两支的并集；每个被 store 的 alias_set 都会分配一个新的「结果 token」加入 `IfElse` 的 `result_vars`，外层 `token_map` 的对应 `last_store`/`last_op` 被更新为这些结果 token，表示「 whichever 分支走了，其 store 都已完成」。

---

### 4.4 循环并行 store 优化

#### 4.4.1 概念说明

朴素的循环 token 穿线会让每轮的 store 等上一轮的 store（因为 store 等 `last_op`，而上一轮 store 就是 `last_op`），于是循环里的 store 被强制**串行**。但有一类常见模式（LayerNorm、RMSNorm 的写回）其实不需要串行：

```python
for i in range(n):
    ct.store(X, index=(i,), tile=...)   # 每轮写不同的 i，互不重叠
```

各轮写入的内存区域由归纳变量 `i` 唯一确定、互不相交，**没有冲突**，完全可以并行发射。`token_order_pass` 内置了这个特判，把这类 store 从「等上一轮」解放为「只等循环之前」的 token。

#### 4.4.2 核心流程

两步：

1. **判定**（[_get_parallel_stores](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/token_order.py#L462-L508)，L462–L508）：对 for-loop 体，逐个 alias_set 检查：
   - 该数组在循环体（含嵌套块）里**只有这一个**访存操作，且它就是 `TileStore`；
   - 该 alias_set 不是 `ALIAS_UNIVERSE`、位计数 ≤ 1（即「确定不与他人别名」）；
   - store 的某个索引是归纳变量的单射函数（目前仅支持 `idx == induction_var` 这种最简形式，[_filter_by_store_index](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/token_order.py#L522-L538) 的 `is_idx_injective`）。
2. **改写**（[_try_loop_parallel_store](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/token_order.py#L541-L583)，L541–L583）：把命中的 store 的输入 token 设为**循环之前**的 `last_op` token（`parent_token_map`），而非上一轮的 token；这样各轮 store 只依赖循环外，彼此独立。

#### 4.4.3 源码精读

`_get_parallel_stores` 的过滤条件：[token_order.py:494-504](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/token_order.py#L494-L504)——「该 alias_set 上恰好 1 个访存、且是 TileStore、且嵌套块对该数组无效应」。文档字符串 [token_order.py:466-473](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/token_order.py#L466-L473) 明确点名 LayerNorm/RMSNorm。

`_filter_by_store_index` 的单射判定：[token_order.py:526-528](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/token_order.py#L526-L528)，目前只认 `idx_var.name == loop_op.induction_var.name`，注释里留了 TODO 支持更复杂的单射（如 `j = i*2+3`）。

改写逻辑：[token_order.py:557-583](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/token_order.py#L557-L583)。关键在 L558–L559：`parent_token_map = innermost_loop_info.parent_token_map`，`before_loop_last_op_tok = parent_token_map[last_op_key]`——输入 token 取自**循环外**。store 分支在主循环里调用它（[token_order.py:197-204](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_passes/token_order.py#L197-L204)），命中即 `continue` 跳过普通串行化路径。

测试用例 `parallel_store` 与期望链形状：[test_token_order.py:313-317](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/test/test_token_order.py#L313-L317) 与 [test_token_order.py:259-269](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/test/test_token_order.py#L259-L269)。注意 directive 里循环体的 `store_view_tko token = %[[TOKEN2]]`（循环前的 token），而循环外的 store 才依赖 `%[[LOOP_TOK]]`。

#### 4.4.4 代码实践

**实践目标**：对比「可并行 store」与「不可并行 store」两种循环的 token 链差异。

**操作步骤**：

1. 写两个内核（取自 `TestForLoopParallelStoreMLIR`）：

   ```python
   @ct.kernel
   def k_parallel(X, n: int, TILE: ct.Constant[int]):
       tx = ct.load(X, index=(0,), shape=(TILE,))
       for i in range(n):           # 每轮写不同 i → 可并行
           ct.store(X, index=(i,), tile=tx)
   ```

   ```python
   @ct.kernel
   def k_serial(X, n: int, TILE: ct.Constant[int]):
       tx = ct.load(X, index=(0,), shape=(TILE,))
       for i in range(n):
           ct.store(X, index=(i,), tile=tx)
           ty = ct.load(X, index=(i,), shape=(TILE,))  # 又读 X → 破坏单访存条件
           ct.store(X, index=(i,), tile=ty)
   ```

2. 分别 dump，对照 `ForLoopParallelStoreCheckDirective`（[test_token_order.py:259-269](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/test/test_token_order.py#L259-L269)）与 `ForLoopLoadStoreCheckDirective`（[test_token_order.py:213-224](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/test/test_token_order.py#L213-L224)）。

**需要观察的现象**：

- `k_parallel`：循环里的 store 的 token 直接是循环前的 `%TOKEN2`，循环只携带 1 个 token。
- `k_serial`：循环携带 2 个 token（last_op + last_store），每轮 store 依赖上一轮的 token——串行。

**预期结果**：`k_parallel` 的循环 `iter_values` 只多出 1 个 token 参数；`k_serial` 多出 2 个。这正反映了「并行 vs 串行」。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `non_parallel_store_non_disjoint`（[test_token_order.py:389-394](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/test/test_token_order.py#L389-L394)）里的 store 不能并行？

> **答案**：该测试里 `X` 是 `broadcast_to` 出来的，各轮 `store(X, index=(bidx, i))` 实际写入的物理地址彼此重叠（非单射/非不相交），强行并行会产生数据竞争。`_get_parallel_stores` 通过 `may_alias_internally` 与单射判定把它排除。

**练习 2**：把命中条件的「嵌套块对该数组无效应」去掉会有什么后果？

> **答案**：若嵌套块（如内层循环或被调用函数）也对同一数组有访存，那么本轮 store 与那些访存可能冲突；只等循环前的 token 就会漏掉这些依赖，导致重排后结果错误。所以该条件是安全的必要条件。

---

### 4.5 调试开关：关闭 token 排序

#### 4.5.1 概念说明

`CUDA_TILE_TESTING_DISABLE_TOKEN_ORDER` 是一个**仅供测试/调试**的环境变量。置 1 时，`_transform_ir` 会**完全跳过** `token_order_pass`，所有访存操作保留 `token=None`，彼此没有任何定序边。这等于把「保证访存顺序」的责任交还给后端/硬件的默认行为——在依赖性访存上**可能产生错误结果**。

它的存在价值是**二分定位**：当一个内核结果出错时，关掉 token 排序可以判断问题是否出在 token 链本身（或它与其他 pass 的交互），还是出在别处。同理还有 `CUDA_TILE_TESTING_DISABLE_DIV`（关闭整除性注入，u6-l2）。

#### 4.5.2 核心流程

1. 启动时读取环境变量：`CUDA_TILE_TESTING_DISABLE_TOKEN_ORDER = os.environ.get(..., "0") == "1"`。
2. `_transform_ir` 用 `if not CUDA_TILE_TESTING_DISABLE_TOKEN_ORDER:` 门控是否调用 `token_order_pass`。

#### 4.5.3 源码精读

开关定义：[debug.py:14-15](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_debug.py#L14-L15)。注意文件头注释 L8 写明这些是「Internal environment variables for debugging」。

门控位置：[compile.py:105-106](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_compile.py#L105-L106)——`if not CUDA_TILE_TESTING_DISABLE_TOKEN_ORDER: token_order_pass(func_body, dataflow_result)`。紧邻的 L102–L103 是 `CUDA_TILE_TESTING_DISABLE_DIV` 对 `add_divby_pass` 的同等门控，二者结构对称。

#### 4.5.4 代码实践

**实践目标**：观察关闭 token 排序后 dump 的差异。

**操作步骤**：

1. 用 4.3.4 的 `flip` 内核。
2. 第一次正常运行并 dump。
3. 设 `CUDA_TILE_TESTING_DISABLE_TOKEN_ORDER=1` 重新运行并 dump。
4. 对比两份产物：第一次应有 `make_token`/`join_tokens`/`token=...`；第二次这些应大量消失（访存的 token 操作数保持为空）。

**需要观察的现象**：关闭后，`store` 与 `gather` 之间不再有 token 依赖边。

**预期结果**：dump 差异印证 token 链确实由本 pass 注入。**数值是否出错「待本地验证」**——取决于后端在该 kernel 上是否实际重排了访存；这正是该开关作为「二分工具」的意义：若关闭后结果变了，说明正确性曾依赖 token 链。

#### 4.5.5 小练习与答案

**练习 1**：为什么这个开关叫 `..._TESTING_...` 而不是普通的 `CUDA_TILE_DISABLE_...`？

> **答案**：它不是给生产用户用的优化旋钮，而是给测试与开发者定位问题用的逃逸口。命名上的 `TESTING` 前缀是在警告：生产环境关掉它可能让依赖性访存出错。

**练习 2**：关掉 token 排序后，`hoist_loop_invariants` 会受影响吗？

> **答案**：`hoist` 在调用顺序上始终晚于 token 排序的位置（[compile.py:110-112](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/src/cuda/tile/_compile.py#L110-L112)）。若 token 排序被跳过，IR 里没有 token 边，hoist 更可能把 load 外提到循环外（这正是注释警告的情形），进一步加剧访存重排风险。

---

## 5. 综合实践

把本讲的知识串起来，做一个「token 链探针」：

1. 写一个内核，它**同时**包含：(a) 一段「写—读—写」同一数组（必须串行）；(b) 一个 for-loop，循环体只对一个互不相交的数组 store 归纳变量索引（可并行）；(c) 一个 `IfElse`，两支各自 store 不同数组。例如：

   ```python
   @ct.kernel
   def probe(X, Y, Z, cond, n: int, TILE: ct.Constant[int]):
       # (a) 串行段
       ct.store(X, index=(0,), tile=ct.arange(TILE, dtype=X.dtype))
       tx = ct.gather(X, ct.arange(TILE, start=TILE-1, step=-1, dtype=np.int32))
       ct.store(X, index=(0,), tile=tx)
       # (b) 可并行循环
       ty = ct.load(Y, index=(0,), shape=(TILE,))
       for i in range(n):
           ct.store(Y, index=(i,), tile=ty)
       # (c) 分支 store
       if cond:
           ct.store(Z, index=(0,), tile=ty)
       else:
           ct.store(Z, index=(1,), tile=ty)
   ```

2. dump 字节码，逐段标注：
   - (a) 段里三条访存如何串成一条链（store₁ → gather → store₂）；
   - (b) 段循环只携带 1 个 token、store 依赖循环前 token；
   - (c) 段 `IfElse` 的 `result_vars` 多出 token、两支如何汇合。

3. 设 `CUDA_TILE_TESTING_DISABLE_TOKEN_ORDER=1` 重跑，确认上述 token 边消失。

4. 用一句话回答：**如果把 (a) 段的 `gather` 换成对另一个独立数组 `W` 的 load，token 链会怎样变化？**（提示：别名集不同 → 各走各的链，互不阻塞。）

这个任务覆盖了 4.1（链的直觉）、4.2（数据骨架）、4.3（控制流传播）、4.4（并行 store）、4.5（调试开关）全部内容。

## 6. 本讲小结

- **访存需要显式定序**：纯计算靠 SSA def-use 定序，访存之间没有这条边；cuTile 用 token 链（`MakeToken`/`JoinTokens` + 访存操作的 `token` 操作数）人为补上「禁止重排」的依赖。
- **按别名集分链 + 双角色**：`token_map` 以 `(alias_set, LAST_OP|LAST_STORE)` 为键；load 只等 `last_store`（连续 load 可并行），store/原子等 `last_op`（防所有 hazard）。
- **两阶段**：先 `_get_block_memory_effects` 汇总每块内存效应，再 `_to_token_order_in_block` 递归穿线，二者共享 u6-l2 的 `dataflow_result`。
- **控制流携带 token**：循环把 token 当携带值（LOAD 加 1 个、STORE 加 2 个），分支合并两支效应后分配结果 token，`Continue`/`Break`/`EndBranch` 携带退出 token。
- **循环并行 store 优化**：LayerNorm/RMSNorm 模式（单 store、索引单射、不相交）被特判为只依赖循环前 token，解开串行。
- **调试开关**：`CUDA_TILE_TESTING_DISABLE_TOKEN_ORDER=1` 跳过本 pass，用于二分定位；生产环境慎用，依赖性访存可能因此出错。

## 7. 下一步学习建议

- 继续往后读优化 pass：**u6-l4「代码外提、循环分裂与模式重写」**，重点理解为什么 `hoist_loop_invariants` **必须**在 token 排序之后（本讲已埋下伏笔），以及 `split_loops` 如何与携带 token 交互。
- 向后端走：**u7-l1「IR 到字节码」**，看 token 操作（`make_token`/`join_tokens`/`*_tko`）如何被 `generate_bytecode` 编码进线性字节码（对应 `encode_MakeTokenOp`/`encode_JoinTokensOp`/`encode_LoadViewTkoOp`）。
- 想看更复杂的 token 形状，直接精读 [test/test_token_order.py](https://github.com/nvidia/cutile-python/blob/40bcc5aa0161ac0d70f064c98286029ac756101b/test/test_token_order.py) 中的 `TestMemoryOrderMLIR`（acquire/release/acq_rel 在无控制流、if、for、while 下的链形状）与 `TestRuntimeAlias`（运行时别名如何把不同数组合并到同一条链）。
- 若对内存模型本身感兴趣，回到 **u4-l3** 与 docs/source/memory_model.rst，把 `MemoryOrder`/`MemoryScope` 与本讲的 acquire/release token 处理对照阅读。
