# VariantSpec 变体接口机制

## 1. 本讲目标

学完本讲，你应该能够：

- 说出 `VariantSpec` 这个声明式配方（recipe）由哪些字段组成，以及它把「变体之间的共性」抽到了哪里。
- 用 `'a'/'b'/'mn'/'row'/'col'` 五种操作数角色解释 `canonicalize` 如何把用户传入的张量规整成设备侧能消化的形状，并理解 `b_kn`、`dense_2d`、`swap_ab` 三类决策。
- 解释 `IfacePlan` 如何在「冷路径记录、热路径重放」中，把规范化结果**烘焙成位编码视图码**，让命中路径不再重算角色规则。
- 看懂 `gemm_symmetric` 这条机器的真实用户，并诚实判断哪些变体**没有**走这条机器。

> 本讲承接 u4-l3（公共 GEMM API 表面）。u4-l3 在 4.3 节已经从「使用者视角」点过 `VariantSpec` 与 `_SYMMETRIC_SPEC`；本讲**反过来**，从「机器内部」逐行拆解 `gemm_iface.py`，重点讲 u4-l3 没展开的**视图编码**、**声明式热路径**与**角色互换语义**。

## 2. 前置知识

本讲默认你已经掌握：

- **操作数角色**：一次 GEMM \(D=\alpha(A@B)+\beta C+\text{bias}\) 里，`A`/`B` 是收缩维操作数，`C`/`D` 是与输出同形的 M×N 张量，`bias` 是沿 N（行向量）或 M（列向量）的一维向量。
- **swap_ab**：部分 `GemmConfig` 会把设备侧的 A、B 对调（等价于算 \(B^T@A^T=D^T\)），用于在硬件上选择更优的 tile 朝向（见 u4-l2）。一旦对调，输出的 M/N 维也就互换，于是 M×N 张量、行/列向量都要跟着「翻转角色」。
- **b_kn**：调用方给的 `B` 通常是 `(K, N)`；SM90+ 的 dense 路径可以保持这个 `(K, N)` 朝向，把「转成 `(N, K)`」推迟到 trace/编译期重标，省掉每次调用 ~1.5µs 的 `.mT` dispatcher 开销（见 u4-l4 的 `tensor_key`/`scalar_mode`）。
- **冷路径 / 热路径**：第一次（或换了元数据的）调用走「冷路径」做校验、autotune、构建启动计划并缓存；相同元数据、只换数据指针的后续调用走「热路径」，直接重放计划（见 u4-l1 的 build/run 拆分）。

一个贯穿本讲的关键直觉：**变体（symmetric / act / norm_act …）之间的差异，绝大多数是「多一个操作数」「换个激活」「换个 tile 配置」这类结构差异；而它们都要做的「把用户张量规整成设备张量」「记录并缓存启动计划」「热路径重放」几乎一模一样。** `VariantSpec` 就是把这层共性抽出来的一次重构。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [quack/gemm_iface.py](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_iface.py) | **本讲主角**。声明式变体机器：`VariantSpec` 配方、`Canon` 规范化结果、`canonicalize` 规则、`IfacePlan` 计划、视图编码 `_canon_view_codes`/`_apply_code`、`make_iface_plan`/`run_variant` 入口、`swap_pair`/`vec_ports` 端口映射。 |
| [quack/gemm_interface.py](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_interface.py) | 变体的**用户侧**。`_SYMMETRIC_SPEC` 是 `VariantSpec` 当前唯一的真实实例；`gemm_symmetric` 是它的公共入口。`gemm_act`/`gemm_gated` 则**没有**用 `VariantSpec`，作为「诚实对照」。 |

> 提醒：源码里 `VariantSpec` 的真实实例**只有一个** `_SYMMETRIC_SPEC`。本讲会出现为 `gemm_act` 设想的「示例 VariantSpec」，这些是为讲解角色语义而写的**示例代码**，不是项目原有代码，届时会明确标注。

## 4. 核心概念与源码讲解

### 4.1 VariantSpec：变体的声明式配方（角色与 epilogue 字段）

#### 4.1.1 概念说明

`gemm_iface.py` 的模块 docstring 直白地说出了这次重构的动机：`gemm_interface.py` 里每一个变体（act/gated、dact/dgated、norm_act/norm_gated、rms、symmetric）过去都**手写复制**了同样四块代码——操作数规范化器、信号量规则、接口计划 NamedTuple + 模块级缓存 + 热路径重新推导规范化、以及一个 `@autotune` 包装。这四块几乎逐字相同，却散落在每个变体里。

[quack/gemm_iface.py:4-21](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_iface.py#L4-L21) 说明：本模块把那四块**只写一次**，做成声明式机器。一个变体只需提供一个 `VariantSpec`（操作数角色 + epilogue 字段指派 + 少量钩子），自己只保留：公共 eager 函数（签名、输出分配、空输入语义）、custom-op schema、参考实现。

换句话说，`VariantSpec` 是一个**填空式表格**：你声明「我有哪些操作数、它们各扮演什么角色、冷/热路径分别调谁、B 要不要保持 `(K,N)`、要不要信号量」，机器就替你完成规范化、计划记录与热路径重放。

#### 4.1.2 核心流程

一台「变体机器」的生命周期可以画成两条路径（冷/热），共享同一个 `canonicalize`：

```text
                          ┌─ canonicalize(spec, named, ...) ─┐   共享规范化
所有调用都要经过的角色规整 ──┤  按 role 做 .mT / unsqueeze / swap │
                          └──────────────────────────────────┘
                                        │
            ┌───────────────────────────┴───────────────────────────┐
   冷路径（首次 / 换元数据）                                  热路径（命中计划缓存）
            │                                                           │
   semaphore = spec.semaphore(...)                          读 IfacePlan.replay 闭包
   spec.cold(canon, sem, config, dynamic, ctx)              （视图码已烘焙，不再跑角色规则）
       → 构建 dispatch_plan，返回                            → run_gemm_epi_plan / spec.warm
   make_iface_plan(spec, named, ..., dispatch_plan)          （见 4.3）
       → 把规范化结果编码成视图码，包进 replay 闭包
       → 存入 _xxx_iface_plan_cache
```

`VariantSpec` 本身不执行任何逻辑，它只是**数据**：一个 `NamedTuple`。真正干活的是 `canonicalize`、`make_iface_plan`、`run_variant` 这几个函数，它们把 `spec` 当配置来读。

#### 4.1.3 源码精读

**`VariantSpec` 的全部字段**：

[quack/gemm_iface.py:51-90](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_iface.py#L51-L90) —— 这是变体的声明式配方，字段可分三组：

| 字段 | 含义 |
|------|------|
| `name` | 变体名，仅用于报错信息（如 `f"variant {spec.name!r} has no warm hook"`）。 |
| `tensor_roles` | `((operand_name, role), ...)`，声明每个操作数扮演的角色，role ∈ `'a'/'b'/'mn'/'row'/'col'`。这是规范化的**唯一输入**。 |
| `cold` | 冷路径启动器 `cold(canon, semaphore, b_kn, config, dynamic, ctx) → dispatch_plan`，拥有该变体的「调 dispatch 包装」调用。 |
| `warm` | 热路径启动器 `warm(plan, canon, semaphore, ctx) → None`。**可空**：用 `warm_slots` 声明式映射的变体把它设 `None`。 |
| `b_kn_rule` | `(sm90_plus, varlen_m, swap_ab, ctx) -> bool`。`True` 表示 B 保持调用方 `(K,N)` 朝向留到 trace 期重标；`False` 表示就地 `.mT`。默认 `sm90_plus and not swap_ab`（[L69](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_iface.py#L69)）。 |
| `dense_2d_ok` | `(ctx) -> bool`，dense-2D 直通的额外否决票（默认放行）。 |
| `semaphore` | `(dynamic, capacity, device, warm) -> Optional[Tensor]`，信号量分配规则。默认「SM90 动态调度才分配」（[L76-L78](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_iface.py#L76-L78)）。 |
| `warm_slots` / `warm_epi` / `warm_extras` | **声明式热路径**专用：当变体的热路径就是一次朴素的 `run_gemm_epi_plan` 调用时，用这三个字段描述操作数→端口的映射，机器会专门建一个闭包，**不必**提供 `warm` 钩子。 |

其中五种角色是整个机器的「词汇表」，docstring 把它当作契约逐条写明：

[quack/gemm_iface.py:22-39](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_iface.py#L22-L39) —— 五种角色的规范化契约（节选）：

- `'a'`/`'b'`：GEMM 操作数。`'b'` 以调用方形状 `(k, n)` 到达，除非 `b_kn`，否则被 `.mT` 重标成 `(n, k)`；`swap_ab` 下 dispatch 层的 A/B 互换。
- `'mn'`：M×N 的 epilogue 张量（D / C / PreAct / PostAct / aux）；`swap_ab` 下经 `.mT` 转置。
- `'row'`：`(n,)` 或 `(l, n)` 向量，升维成 `(1, n)`；`swap_ab` 下**变成 colvec**（输出转置把 N 重标成 M——同一个张量，不同的 epilogue 端口）。
- `'col'`：`(m,)` 或 `(l, m)` 向量（varlen 下保持 1-D）；`swap_ab` 下**变成 rowvec**。
- dense-2D：SM90+ 且所有 `a`/`b`/`mn` 操作数都是 2-D 且无 varlen（且变体不否决）时，张量**不升批次维直通**；否则每个 2-D 的 `a`/`b`/`mn` 都加一个 size-1 批次维。

**唯一真实实例 `_SYMMETRIC_SPEC`**：

[quack/gemm_interface.py:2649-2659](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_interface.py#L2649-L2659) 定义：

```python
_SYMMETRIC_SPEC = VariantSpec(
    name="symmetric",
    tensor_roles=(("A", "a"), ("B", "b"), ("out", "mn"), ("C", "mn")),
    cold=_symmetric_cold,
    warm=_symmetric_warm,
    # The symmetric dispatch takes B operand-shaped (m, k): always relabel.
    b_kn_rule=lambda sm90_plus, varlen_m, swap_ab, ctx: False,
    semaphore=lambda dynamic, capacity, device, warm: (
        torch.zeros(1, dtype=torch.int32, device=device) if dynamic and not warm else None
    ),
)
```

要点逐条对应：

- 角色表里 `out` 和 `C` 都是 `'mn'`（都是 `(M,M)` 方阵），**没有** `'row'`/`'col'`——symmetric 不带 bias。
- `b_kn_rule` 恒为 `False`：symmetric 的设备侧入口 `gemm_symmetric_dispatch` 期望 B 是 `(M,K)` 朝向（注释 [L2654](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_interface.py#L2654)），所以总是就地 `.mT`，不走「保持 `(K,N)`」快路径。
- `warm_slots` 没设（默认 `None`），所以它走自己的 `_symmetric_warm` 钩子，**不**用声明式热路径。
- `semaphore` 被改写：默认只在 SM90 动态调度分配；这里改成「冷路径且 dynamic 就分配，热路径永不分配」。

> **诚实提示（重要）**：尽管模块 docstring 把 act/gated/norm_act/rms 都列为「设计目标用户」，但在当前 HEAD（`60d8808`）下，`gemm_act`/`gemm_gated` 已经被**移植到 epilogue-object surface**，走的是 `quack.epilogue.library.linear_act_mod`，**不再**构造 `VariantSpec`。`gemm_gated` 只是 `gemm_act` 的别名（[L2239](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_interface.py#L2239) `gemm_gated = gemm_act`）。移植说明见 [quack/gemm_interface.py:798-804](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_interface.py#L798-L804)。因此 `_SYMMETRIC_SPEC` 是 `VariantSpec` 机器**唯一的真实样本**，本讲所有「机器内部」讲解都以它为锚。

#### 4.1.4 代码实践

**实践目标**：确认 `_SYMMETRIC_SPEC` 是 `VariantSpec` 的唯一真实实例，并理解「诚实对照」。

**操作步骤**（源码阅读）：

1. 用 `git grep -n "VariantSpec(" quack/` 在仓库里搜 `VariantSpec(` 的构造点。你会看到**唯一**一条命中：[quack/gemm_interface.py:2649](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_interface.py#L2649) 的 `_SYMMETRIC_SPEC`。
2. 读 [quack/gemm_interface.py:798-804](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_interface.py#L798-L804)：`gemm_act`/`gemm_gated` 的注释明说它们已 ported 到 epilogue-object surface（`linear_act_mod` 拥有规范化、计划缓存、调优）。
3. 读 [quack/gemm_interface.py:2239](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_interface.py#L2239)：`gemm_gated = gemm_act` 一行别名。

**需要观察的现象**：全仓库只有 `gemm_symmetric` 一条路径真正「构造并使用」`VariantSpec`；`gemm_act`/`gemm_gated` 是同一函数的两个名字。

**预期结果**：能用自己的话说明「`VariantSpec` 是为统一变体而设计的机器，但当前只有 symmetric 接入了它；其余变体走了更新的 epilogue-object 路线」。这是阅读源码时避免被过时 docstring 误导的关键。

#### 4.1.5 小练习与答案

**练习 1**：`_SYMMETRIC_SPEC.tensor_roles` 里为什么 `out` 和 `C` 都是 `'mn'`，而不是一个 `'mn'`、一个 `'col'`？

**参考答案**：`C` 是残差项 \(\beta C\)，形状 `(M,M)`，与输出 `D` 同形，属于「M×N epilogue 张量」即 `'mn'`；`'col'`/`'row'` 专指沿单维广播的**向量**（如 bias）。`C` 不是向量，故为 `'mn'`。见契约 [L28-L30](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_iface.py#L28-L30)。

**练习 2**：`_SYMMETRIC_SPEC` 没有设 `warm_slots`。这意味着它的热路径走哪条分支？

**参考答案**：走 `warm` 钩子分支（`make_iface_plan` 的 `else`，[L301-L308](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_iface.py#L301-L308)），即 `_symmetric_warm`。`warm_slots` 非 `None` 时才走声明式 `run_gemm_epi_plan` 分支。

---

### 4.2 操作数规范化规则：canonicalize 与视图编码

#### 4.2.1 概念说明

`canonicalize` 是整台机器的**心脏**：它读 `spec.tensor_roles` 和一组决策（`sm90_plus`/`varlen_m`/`swap_ab`），把用户传入的 `named` 字典（操作数名→张量）变换成设备侧期望的朝向与秩。它有两个硬约束：

1. **冷/热路径都要跑**——所以它必须**几乎不分配**（除了它创建的张量视图）。
2. 它的输出 `Canon` 不仅含变换后的张量，还含三个布尔决策（`swapped`/`b_kn`/`dense_2d`），供启动器使用。

但「热路径每次都重跑角色规则」是浪费——同一元数据下，规范化结果完全相同。于是机器在**记录计划时**用 `_canon_view_codes` 把规范化「翻译」成一串位编码（`_MT_PRE`/`_UNSQ`/`_MT_POST`），热路径用 `_apply_code` 只做三个分支测试就能还原。这是本讲相对 u4-l3 最关键的新增内容。

#### 4.2.2 核心流程

规范化的判定顺序（伪代码）：

```text
b_kn   = spec.b_kn_rule(sm90_plus, varlen_m, swap_ab, ctx)
dense_2d = sm90_plus and not varlen_m and spec.dense_2d_ok(ctx)
           and 每个 a/b/mn 操作数都 (None 或 ndim==2)
for (name, role) in spec.tensor_roles:
    t = named[name]
    if t is None: 保留 None
    elif role == 'b':
        if not b_kn:        t = t.mT          # 转成 (N,K)
        if not dense_2d and ndim==2 and not varlen_m: t = t.unsqueeze(0)
    elif role in ('a','mn'):
        if not dense_2d and ndim==2 and not varlen_m: t = t.unsqueeze(0)
    elif role == 'row':
        if ndim==1: t = t.unsqueeze(0)        # (L,N)
    elif role == 'col':
        if ndim==1 and not varlen_m: t = t.unsqueeze(0)   # (L,M)
    if swap_ab and role == 'mn': t = t.mT      # mn 在批次化之后才转置
return Canon(tensors=out, swapped, b_kn, dense_2d)
```

三个决策的含义：

- **`b_kn`**：B 要不要保持 `(K,N)`。`True`→留到 trace 期重标（省 `.mT`）；`False`→现在就 `.mT`。
- **`dense_2d`**：能不能让 2-D 操作数**不升批次维**直通。`True`→所有 a/b/mn 保持 2-D；`False`→2-D 的 a/b/mn 加一维变 3-D（kernel 见到的总是带批次的形式）。
- **`swap_ab`**：A/B 是否对调。它只影响 `'mn'`（事后 `.mT`）和启动器里的 A/B 互换（`swap_pair`），**不**改变 `'row'`/`'col'` 的张量本身——它们的「端口」翻转由 `vec_ports` 在设备侧解决。

视图编码用三个可按位组合的标志（[L188-L190](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_iface.py#L188-L190)）：

| 编码 | 值 | 含义 | 何时置位 |
|------|----|------|----------|
| `_MT_PRE` | 1 | 批次化**之前**的 `.mT` | role `'b'` 且 `not b_kn` |
| `_UNSQ` | 2 | `unsqueeze(0)` | 任何需要升批次维的角色 |
| `_MT_POST` | 4 | 批次化**之后**的 `.mT` | role `'mn'` 且 `swap_ab` |

（三个常量值 `1/2/4` 故意取 2 的幂，便于按位或组合。）按**位序**应用：先 `_MT_PRE`，再 `_UNSQ`，再 `_MT_POST`——这正好复现 `canonicalize` 里 `'b'`（先 `.mT` 后 unsqueeze）和 `'mn'`（先 unsqueeze 后 `.mT`）的顺序。

#### 4.2.3 源码精读

**`canonicalize` 主循环**：

[quack/gemm_iface.py:102-154](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_iface.py#L102-L154)。注意几个细节：

- L111 先算 `b_kn`，L114-L123 算 `dense_2d`（要求所有 a/b/mn 操作数 `None` 或 `ndim==2`）。
- L130-L139 处理 `'b'`：`not b_kn` 时 `.mT`，再按需 `unsqueeze(0)`。注释 L133-L138 特别指出：varlen_m 的 B 是按序列索引的（契约就是 3-D），**不能**对 2-D 的 varlen_m B 做 `unsqueeze(0)`——那会在第一序列之后读到越界垃圾（2026-07-14 修的 bug）。
- L143-L148 处理 `'row'`/`'col'`：1-D 才升维；`'col'` 在 varlen_m 下**保持 1-D**（total_m 不是批次维）。
- L151-L152：`swap_ab and role == "mn"` 才事后 `.mT`——这是 mn 与 a/b 的关键区别。

**视图编码的「翻译」**：

[quack/gemm_iface.py:193-216](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_iface.py#L193-L216) `_canon_view_codes`：它**不重新做规范化**，而是看着 `canon`（已规范化的结果）和原始 `named`，反推出「每个操作数被施加了哪些视图」。比如 role `'b'`：若 `not canon.b_kn` 则置 `_MT_PRE`；若 `not dense_2d and ndim==2` 则置 `_UNSQ`。role `'mn'`：`not dense_2d and ndim==2 and not varlen_m` 置 `_UNSQ`，`swapped` 再置 `_MT_POST`。

[quack/gemm_iface.py:219-229](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_iface.py#L219-L229) `_apply_code`：按位序还原——`_MT_PRE`→`.mT`，`_UNSQ`→`unsqueeze(0)`，`_MT_POST`→`.mT`。docstring（L194-L196）承诺：**在相同元数据上重放这些编码，能精确复现 `canonicalize()`**——而「相同元数据」正是接口计划缓存键所保证的。

**端口映射小工具**（设备侧冷/热钩子用）：

[quack/gemm_iface.py:356-359](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_iface.py#L356-L359) `swap_pair(canon, a, b)`：返回 dispatch 层的 `(A, B)`，`swapped` 时交换。这是 `swap_ab` 下「A/B 对调」的统一实现。

[quack/gemm_iface.py:362-373](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_iface.py#L362-L373) `vec_ports(canon, name, *, base)`：把一个 `'row'`/`'col'` 操作数映射到 `(rowvec, colvec)` 两个 dispatch 端口。`base` 是「未交换时的端口」；`swapped` 时翻转：一个 row 向量在 swap 下变成 colvec（同一个张量，换个端口）。这正对应契约里「row 在 swap_ab 下变成 colvec」。

#### 4.2.4 代码实践

**实践目标**：用**真实的** `canonicalize` 函数观察角色如何被规整（纯张量视图，便于推理）。

**操作步骤**（**示例代码**，非项目原有；导入是否可在本机直跑**待本地验证**，因为 `import quack.*` 会触发 cutlass-dsl 副作用，但 `canonicalize` 本身只做 `.mT`/`unsqueeze`，不碰 CUDA）：

```python
# 示例代码：用真实 canonicalize 观察角色规整
import torch
from quack.gemm_iface import VariantSpec, canonicalize

# 为讲解构造的「示例 gemm_act 配方」——非项目原有代码
demo_spec = VariantSpec(
    name="demo_act",
    tensor_roles=(
        ("A", "a"), ("B", "b"),                  # 收缩操作数
        ("C", "mn"), ("preact_out", "mn"),       # M×N epilogue 张量
        ("bias", "row"),                         # 行向量
    ),
    cold=lambda *a, **k: None,
    warm=lambda *a, **k: None,
)

named = {
    "A": torch.randn(4, 8),       # (M, K)
    "B": torch.randn(8, 16),      # (K, N)
    "C": None,
    "preact_out": torch.randn(4, 16),
    "bias": torch.randn(16),      # (N,)
}

# 场景 1：SM90+、非 varlen、非 swap → dense_2d=True，2-D 全直通，bias 升一维
c1 = canonicalize(demo_spec, named, sm90_plus=True, varlen_m=False, swap_ab=False)
print("dense_2d:", c1.dense_2d, "b_kn:", c1.b_kn)
print({k: (v.shape if v is not None else None) for k, v in c1.tensors.items()})
# 预期：dense_2d True, b_kn True; A/B/preact_out 保持 2-D, bias→(1,16)

# 场景 2：swap_ab=True → C/preact_out 这类 'mn' 会被事后 .mT
named2 = dict(named, C=torch.randn(4, 16))
c2 = canonicalize(demo_spec, named2, sm90_plus=True, varlen_m=False, swap_ab=True)
print("swapped:", c2.swapped, "C strides:", c2.tensors["C"].stride())
# 预期：swapped True；'mn' 的 C 被转置（stride 翻转），bias 形状不变（端口翻转交给 vec_ports）
```

**需要观察的现象**：

- 场景 1：`dense_2d=True`，`b_kn=True`，A/B/C/preact_out 全部保持 2-D，只有 `bias`（role `'row'`）从 `(16,)` 升到 `(1,16)`。
- 场景 2：`'mn'` 张量被 `.mT`（看 stride 是否翻转），而 `bias`（`'row'`）**形状不变**——它的「端口翻转」要等到调 `vec_ports(..., base="row")` 时才发生。

**预期结果**：能解释「`swap_ab` 对向量张量本身不做改动，只改它接入哪个端口；对 mn 张量则直接 `.mT`」。**待本地验证**导入与打印结果。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `_canon_view_codes` 要区分 `_MT_PRE` 和 `_MT_POST` 两个 `.mT`，而不是一个？

**参考答案**：因为 `'b'` 的 `.mT` 发生在**批次化之前**（先转成 `(N,K)` 再 `unsqueeze`），而 `'mn'` 的 `.mT`（仅 swap 下）发生在**批次化之后**（先 `unsqueeze` 成 `(1,M,N)` 再转置）。两者在 `_apply_code` 里的相对顺序不同，必须用两个独立位区分，否则无法精确复现 `canonicalize` 的视图顺序。见 [L188-L190](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_iface.py#L188-L190) 与 `_apply_code` [L219-L229](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_iface.py#L219-L229)。

**练习 2**：`dense_2d` 快捷路径的四个条件是什么？其中为什么 `varlen_m` 必须排除？

**参考答案**：四个条件（[L114-L123](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_iface.py#L114-L123)）：①`sm90_plus`；②`not varlen_m`；③`spec.dense_2d_ok(ctx)`（变体否决票）；④所有 a/b/mn 操作数 `None` 或 `ndim==2`。排除 varlen_m 是因为 varlen_m 的「第 0 维是 total_m（所有序列拼起来的总行数），不是批次」——它的 a/mn 张量按契约保持 2-D，但语义上不是「无批次的稠密 2-D」，不能套用 dense-2D 直通规则（见契约 [L36-L38](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_iface.py#L36-L38)）。

---

### 4.3 IfacePlan：接口计划的构建、缓存与热路径重放

#### 4.3.1 概念说明

`IfacePlan` 是变体机器版的「接口层计划」，对应 `gemm` 主入口里的 `_GemmIfacePlan`（u4-l1/u4-l3 讲过）。它缓存「一次 eager 调用里所有**由元数据决定**的产物」：校验过的输出分配配方、解析好的 config、动态调度标志、捕获到的 dispatch_plan，以及最重要的——一个**热路径重放闭包** `replay(named, ctx)`。

关键巧思在于：`replay` 闭包在**记录时**（`make_iface_plan`）就把规范化结果烘焙成视图码、把 swap 标志、向量端口字段名、是否需要信号量**全部解析定**。热路径调用 `replay` 时**什么都不重推**——只对每个张量跑 `_apply_code`（三个分支测试）+ 分配输出 + 填标量。

#### 4.3.2 核心流程

`gemm_symmetric` 一次完整调用的冷/热分流（其他接入 `VariantSpec` 的变体同理）：

```text
gemm_symmetric(A, B, C, out, alpha, beta, ...)
  │
  ├─ 解包 blockscaled / 折算 scale / SF 批次规整（与 gemm 同构）
  ├─ 构造 plan_key（tensor_key×N + scalar_mode×2 + ...）→ 查 _gemm_symmetric_iface_plan_cache
  │     命中（热）→ alloc_outputs(plan, out=out) → plan.replay(named, ctx) → return
  ├─ 空输入 → _empty_k_matmul_into → return
  ├─ torch.compile → gemm_symmetric_out 自定义算子 → return
  └─ eager 冷路径：
       dispatch_plan = _gemm_symmetric_execute(...) → run_variant(_SYMMETRIC_SPEC, ...)
           canonicalize → semaphore → spec.cold(...) → gemm_symmetric_dispatch → 返回 plan
       make_iface_plan(_SYMMETRIC_SPEC, named, config=None, ..., dispatch_plan)
           → canonicalize 一次，编码成视图码，包成 replay 闭包 → IfacePlan
       → 存进 _gemm_symmetric_iface_plan_cache，return out
```

`make_iface_plan` 内部按 `warm_slots` 是否为 `None` 分两支构建 `replay`：

- **声明式分支**（`warm_slots` 非 `None`，当前无变体使用）：直接调 `run_gemm_epi_plan`，把 a/b/d/c 端口（swap 下互换）、epi 值端口（`warm_epi`，向量端口按 swap 选 row/col）、`warm_extras` 的 per-call 标量全部在闭包里写死。
- **钩子分支**（`warm_slots` 为 `None`，`_SYMMETRIC_SPEC` 走这条）：闭包里重放所有操作数的视图码、按需分配信号量，然后调 `spec.warm(dispatch_plan, Canon(...), sem, ctx)`。

#### 4.3.3 源码精读

**`IfacePlan` 与输出分配**：

[quack/gemm_iface.py:157-174](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_iface.py#L157-L174) `IfacePlan`：字段 `config`（可空，symmetric 传 `None` 因为它用固定方阵 tile、不走 GemmConfig）、`dynamic_scheduler`、`out_recipes`（`((name, shape, dtype), ...)` 输出分配配方）、`dispatch_plan`、`replay`（`replay(named, ctx)`，记录时由 `make_replay` 建）。docstring（L160-L165）强调：**接口计划键必须涵盖 dispatch 计划键所涵盖的一切**——因为热路径只留这一个键。

[quack/gemm_iface.py:177-182](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_iface.py#L177-L182) `alloc_outputs`：热路径用它把调用者留 `None` 的输出按配方 `torch.empty` 出来。

**`make_iface_plan` 的两个闭包分支**：

[quack/gemm_iface.py:232-316](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_iface.py#L232-L316)。几个要点：

- L256：`swap_ab = config.swap_ab if config is not None else False`——symmetric 传 `config=None`，故**永不 swap**（与「输出是对称方阵」语义一致）。
- L257-L259：跑一次 `canonicalize`。
- L260：`_canon_view_codes` 把规范化翻译成视图码。
- L264：**探一次信号量**——`sem_dynamic = sem_fn(...) is not None`。这是元数据静态的；只有「分配」动作是 per-call。这样「不需要信号量的热路径」可以完全跳过分配规则。
- L266-L299：声明式分支（`warm_slots` 非 `None`）。它从 `quack.gemm_runtime.host` 延迟导入 `run_gemm_epi_plan`，解析 a/b/d/c 操作数名（swap 下 a/b 互换，L271-L272），按 `warm_epi` 建 epi 程序（`"vec"` 条目按 swap 选 row/col 字段，L278-L281），最后在闭包里直接调 `run_gemm_epi_plan`。
- L301-L308：钩子分支（`_SYMMETRIC_SPEC` 走这条）。闭包重放每个操作数的视图码（`{name: _apply_code(named[name], code) ...}`），按 `sem_dynamic` 分配信号量，调 `warm(dispatch_plan, Canon(...), sem, ctx)`。

**`run_variant` 冷/热总入口**：

[quack/gemm_iface.py:319-353](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_iface.py#L319-L353)。L344 算信号量（`warm=` 传 `dispatch_plan is not None`）；L345-L352 是热路径（`dispatch_plan is not None`）——若变体没 `warm` 钩子就报错（L347-L350），引导你改走 `IfacePlan.replay`；L353 是冷路径，调 `spec.cold`。docstring（L330-L336）说明：来自接口计划的热路径重放应**优先用 `IfacePlan.replay`**（不重推），这条 `run_variant` 主要服务冷调用与无计划的热重放。

**`gemm_symmetric` 如何把三者串起来**：

[quack/gemm_interface.py:2662-2692](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_interface.py#L2662-L2692) `_gemm_symmetric_execute`：一行 `run_variant(_SYMMETRIC_SPEC, dict(A=A,B=B,out=out,C=C), config=None, ..., ctx=dict(alpha,beta,SFA,SFB,...))`。

[quack/gemm_interface.py:2695](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_interface.py#L2695) 模块级缓存 `_gemm_symmetric_iface_plan_cache: dict[tuple, IfacePlan]`。

[quack/gemm_interface.py:2728-2760](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_interface.py#L2728-L2760) 热路径：查缓存命中后 `alloc_outputs(plan, dict(out=out), A.device)["out"]` 分配输出，再 `plan.replay(dict(A=A,B=B,out=out,C=C), dict(alpha=...,beta=...,SFA=...,SFB=...,bs_format_a=...,bs_format_b=...))`。注意 `replay` 的两个参数：`named`（操作数）与 `ctx`（变体的不透明附加包，含 alpha/beta/SF/format）。

[quack/gemm_interface.py:2814-2822](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_interface.py#L2814-L2822) 冷路径结尾：`make_iface_plan(_SYMMETRIC_SPEC, dict(A=A,B=B,out=out,C=C), config=None, dynamic_scheduler=..., out_recipes=(("out", out_shape, out_dtype),), dispatch_plan=dispatch_plan)` 记录计划并存缓存。

**`_symmetric_cold` / `_symmetric_warm` 钩子**：

[quack/gemm_interface.py:2604-2629](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_interface.py#L2604-L2629) `_symmetric_cold`：按 SM 查方阵 tile 配置（`_symmetric_gemm_config`，[L2549-L2561](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_interface.py#L2549-L2561)，如 SM100 用 `256×256, cluster_m=2`），从 `canon.tensors` 取已规范化的 A/B/out/C，调 `gemm_symmetric_dispatch`，其余参数（alpha/beta/SF/format）从 `ctx` 取。

[quack/gemm_interface.py:2632-2646](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_interface.py#L2632-L2646) `_symmetric_warm`：调 `run_gemm_symmetric_plan(plan, A, B, out, C, alpha=..., beta=..., SFA=..., SFB=...)`。注释 L2633-L2634 点明：信号量在这里**从不被消费**（`is_dynamic_persistent` 意味着 SM100+，其调度器用 CLC），故忽略。

#### 4.3.4 代码实践

**实践目标**：跟踪 `gemm_symmetric` 的 plan_key，理解「接口计划键必须涵盖 dispatch 键」这条铁律。

**操作步骤**（源码阅读）：

1. 读 [quack/gemm_interface.py:2728-2744](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_interface.py#L2728-L2744)：列出 `plan_key` 的全部字段——四个 `tensor_key`（A/B/C/out）+ `A.device` + `out_dtype` + `dynamic_scheduler` + 两个 `scalar_mode`（alpha/beta）+ 两个 `tensor_key`（SFA/SFB）+ 两个 `bs_format`。
2. 读 [quack/gemm_symmetric.py:249-260](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_symmetric.py#L249-L260)：dispatch 层 `_GemmSymmetricPlan` 的缓存键。注意 `alpha_mode`/`beta_mode`（即 `scalar_mode`）也在 dispatch 键里。
3. 对照 [quack/gemm_iface.py:160-L165](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_iface.py#L160-L165) 的 docstring：接口键里的 `scalar_mode(alpha)`/`scalar_mode(beta)` 正是为了**涵盖** dispatch 键里的 `alpha_mode`/`beta_mode`。

**需要观察的现象**：接口计划键 ⊇ dispatch 计划键。alpha/beta 是「标量还是设备张量」会选到**结构不同**的编译 epilogue，所以两种 mode 必须进两层键。

**预期结果**：能解释「为什么不能只把 dtype/shape 放进接口键」——因为 dispatch_plan 依赖 alpha/beta 的 mode，接口键若漏了它，热路径会拿错误的 dispatch_plan 重放。**待本地验证**：在 SM90+ 上跑 `tests/test_gemm_symmetric.py` 的 `scalar_kind="tensor"` 与 `scalar_kind="float"` 两组，确认它们各自缓存、互不污染。

#### 4.3.5 小练习与答案

**练习 1**：`make_iface_plan` 里 `sem_dynamic = sem_fn(...) is not None` 这个「探一次」有什么用？

**参考答案**：信号量的**是否需要**由元数据决定（动态调度 + 架构），是静态的；但**分配动作**是 per-call 的（每次热路径都可能新建一个 `torch.zeros(1, ...)`）。先探一次把「需要与否」固定成 `sem_dynamic` 布尔，热路径闭包就只在 `sem_dynamic` 为真时才调 `sem_fn` 分配，不需要信号量的变体（如 SM100+ 的 symmetric）能完全跳过分配规则。见 [L261-L264](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_iface.py#L261-L264)。

**练习 2**：`gemm_symmetric` 调 `make_iface_plan` 时为什么传 `config=None`？那 `swap_ab` 会被解析成什么？

**参考答案**：symmetric 用**固定方阵 tile**（由 `_symmetric_gemm_config` 按 SM 查表，烘焙进 `_symmetric_cold`），不走 `GemmConfig`/autotune，故 `config=None`。`make_iface_plan` 里 `swap_ab = config.swap_ab if config is not None else False`（[L256](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_iface.py#L256)）→ `swap_ab=False`，与「输出是对称方阵、不需要对调 A/B」一致。

---

## 5. 综合实践

**任务**：把本讲三个模块（配方 / 规范化 / 计划重放）串起来——为 `gemm_act` **设想**一个 `VariantSpec`，与真实的 `_SYMMETRIC_SPEC` 对照，并用角色语义解释 `swap_ab` 下的互换。

> 这是**设计型 + 源码阅读型**实践。下面的「示例 VariantSpec」是为讲解而构造的**示例代码**，不是项目原有代码——真实的 `gemm_act` 走 `linear_act_mod`，并未接入这台机器。

**步骤 1｜对照两个变体的角色表**

`gemm_act` 的语义是 `postact = act(alpha*A@B + C + bias)`（可选 `store_preact` 存 preact）。设想它的角色表（示例）：

```python
# 示例代码：为讲解设想的 gemm_act VariantSpec（非项目原有）
ACT_SPEC = VariantSpec(
    name="act",
    tensor_roles=(
        ("A", "a"), ("B", "b"),
        ("C", "mn"),          # 残差，(M,N)
        ("preact_out", "mn"), # 激活前的累加器，(M,N)
        ("postact_out", "mn"),# 激活后输出，(M,N)
        ("bias", "row"),      # 行向量，(N,)
    ),
    cold=lambda canon, sem, config, dyn, ctx: None,   # 示意
    warm=lambda plan, canon, sem, ctx: None,          # 示意
)
```

与 `_SYMMETRIC_SPEC`（[L2649-L2659](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_interface.py#L2649-L2659)）的差异：

| 维度 | `_SYMMETRIC_SPEC` | 设想的 `ACT_SPEC`（示例） |
|------|-------------------|--------------------------|
| `mn` 操作数 | `out`、`C` 两个 | `C`、`preact_out`、`postact_out` 三个 |
| 向量操作数 | 无 | `bias` 一个 `'row'` |
| `b_kn_rule` | 恒 `False`（dispatch 要 `(M,K)` 朝向） | 默认 `sm90_plus and not swap_ab`（dense 路径可留 `(K,N)`） |
| `semaphore` | 冷路径 + dynamic 才分配 | 默认（仅 SM90 dynamic） |

**步骤 2｜解释 `swap_ab` 下 `'mn'`/`'row'`/`'col'` 如何互换**

- `'mn'`：`canonicalize` 直接对张量 `.mT`（[L151-L152](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_iface.py#L151-L152)）。设想的 `ACT_SPEC` 里 `C`/`preact_out`/`postact_out` 在 swap 下都会转置——因为输出 \(D^T\) 的 M/N 互换，这些 M×N 张量必须跟着翻。
- `'row'`（如 `bias`）：张量**形状不变**，但接入的端口翻转——`vec_ports(canon, "bias", base="row")` 在 `swapped` 时返回 `(None, bias)`，即从 rowvec 端口改接到 colvec 端口（[L362-L373](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_iface.py#L362-L373)）。直觉：bias 原本沿 N 广播，swap 后 N 变成 M，所以它改沿 M 广播→colvec。
- `'col'`：同理反向，swap 下变 rowvec。
- `'a'`/`'b'`：张量本身不动，靠 `swap_pair(canon, "A", "B")` 在 dispatch 层互换（[L356-L359](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_iface.py#L356-L359)）。

**步骤 3｜说明 dense-2D 快捷路径的条件**

`canonicalize` 里 `dense_2d` 为 `True` 需同时满足（[L114-L123](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_iface.py#L114-L123)）：

1. `sm90_plus`（Hopper 及以上，dense 路径才支持 trace 期重标）；
2. `not varlen_m`（变长序列的 total_m 不是批次，不能套用）；
3. `spec.dense_2d_ok(ctx)`（变体可否决，例如 act 在 `concat_layout` 下否决）；
4. 每个 `'a'`/`'b'`/`'mn'` 操作数都 `None` 或 `ndim==2`（全 2-D 才能不升批次维直通）。

命中时，2-D 操作数**不 `unsqueeze`**、B 在 `b_kn` 下**保持 `(K,N)`**，全部留到 trace 期重标，省掉每调用多次 `.mT`/`unsqueeze` 的 dispatcher 开销（注释 [L650-L654](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_interface.py#L650-L654)）。

**需要观察的现象 / 预期结果**：能用自己的话说清「`swap_ab` 对向量只改端口、对 mn 直接转置、对 a/b 在 dispatch 层互换」三条规则，并指出 dense-2D 直通是 SM90+ 无 varlen 的全 2-D 快路径。**待本地验证**：在 4.2.4 的示例脚本里把 `sm90_plus` 切到 `False`，观察 `dense_2d` 变 `False`、`b_kn` 变 `False`、A/B 被升维成 3-D。

## 6. 本讲小结

- `VariantSpec` 是把「GEMM 变体共享的规范化 + 计划缓存 + 热路径」抽到一处的**声明式配方**：`tensor_roles`（五种角色）+ 冷/热钩子 + `b_kn_rule`/`dense_2d_ok`/`semaphore` 等规则字段。
- 当前 HEAD（`60d8808`）下，`_SYMMETRIC_SPEC` 是它**唯一的真实实例**；`gemm_act`/`gemm_gated` 已移植到 epilogue-object surface（`linear_act_mod`），并未接入这台机器——阅读时要警惕 docstring 的过时描述。
- `canonicalize` 是机器心脏，按角色施加 `.mT`/`unsqueeze`/swap；`'b'` 先转置后升维，`'mn'` 先升维后（swap 下）转置，`'row'`/`'col'` 张量本身在 swap 下不动、只换端口。
- `_canon_view_codes`/`_apply_code` 把规范化**烘焙成位编码视图码**（`_MT_PRE`/`_UNSQ`/`_MT_POST`），让热路径 `IfacePlan.replay` 用三个分支测试还原，不再重推角色规则。
- `IfacePlan` 键**必须涵盖 dispatch 键**（故含 `scalar_mode(alpha/beta)`）；`make_iface_plan` 按 `warm_slots` 是否为 `None` 分「声明式 `run_gemm_epi_plan`」与「`warm` 钩子」两支构建重放闭包，symmetric 走后者。
- dense-2D 直通需 SM90+ + 无 varlen_m + 变体不否决 + 全 2-D 操作数，命中则操作数不升批次维、B 保持 `(K,N)`，省去 per-call 视图开销。

## 7. 下一步学习建议

- **向下（设备侧 epilogue）**：声明式热路径分支里的 `run_gemm_epi_plan` 来自 `quack/gemm_runtime/host.py`（u5、u6 会展开）。读 [quack/gemm_runtime/host.py:509](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_runtime/host.py#L509) 附近的 `run_gemm_epi_plan`，看「接口计划重放」最终如何落到设备内核。
- **横向（变体迁移）**：对比 `gemm_act` 走的 `quack.epilogue.library.linear_act_mod`（[quack/gemm_interface.py:807-872](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_interface.py#L807-L872) `_gemm_act_call`）与本讲的 `run_variant`，理解为什么项目选择把更多变体迁到 epilogue-object 路线——这是 u6「可组合 Epilogue 系统」的引子。
- **纵向（调度与信号量）**：`semaphore` 规则里的 `tile_count_semaphore` 与 SM90 动态调度、SM100+ CLC 的关系，详见 u3-l4（tile_scheduler）与 u3-l5（异步流水线与同步原语）。
- **测试验证**：跑 `pytest tests/test_gemm_symmetric.py -x` 验证 `_SYMMETRIC_SPEC` 的端到端数值正确性；用 `git grep -n "VariantSpec"` 持续观察未来是否有新变体接入这台机器。
