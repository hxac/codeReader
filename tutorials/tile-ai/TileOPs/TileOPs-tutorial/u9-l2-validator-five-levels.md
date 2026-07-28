# 验证器的五级检查

> 本讲承接 [u9-l1 四阶段信任模型](u9-l1-trust-model-stages.md) 与 [u8-l1 __init_subclass__ 钩子与自动安装](u8-l1-codegen-init-subclass-hook.md)。u9-l1 讲清了「manifest→test→implementation→benchmark 四阶段各自拥有什么、不可写什么」的**信任边界**；u8-l1/u8-l3 讲清了「`__init_subclass__` 据 manifest 自动合成 `_validate_dtypes` / `eval_roofline` / `_infer_output_shapes` 三个 codegen 契约」的**生产侧**。本讲站到**消费侧**——CI 里的 `scripts/validate_manifest.py` 如何把这些声明性契约变成可执行的关卡，把「代码服从规约」从口号落实成一条 `exit code != 0`。

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出五级检查（L0 schema / L1 signature / L2 shape / L3 dtype / L4 bench）**各自检查什么、数据来自哪里**，并能画出一个算子条目从 YAML 到「通过/失败」的完整判定流程。
2. 解释 **L0 中 `status: implemented` 的算子缺 `source.kernel_map` 已从 advisory（警告）升级为硬错误** 这一规则，并能指出对应代码与报错文本。
3. 讲清 **L2/L3 的 parity 扩展**（C1 对照 `_infer_output_shapes`、C2 对照 `_validate_dtypes`）是如何用「mock 输入 + 反射调用」在 CPU 上反向探测 Op 类是否守约的；以及 **L4 要求基准用 `load_workloads` 与 `eval_roofline`** 的 AST 契约。
4. 区分 **advisory 模式与 `--strict` 模式**：哪些检查始终是硬错误、哪些 C1–C7 严格 parity 检查在两种模式下分别走 errors 还是 warnings。
5. 用 `--check-op <name>` 把全部五级强制作用到一个算子及其变体家族上，并读懂它的输出。

## 2. 前置知识

本讲默认你已经掌握以下概念（来自前置讲义）：

- **信任模型四阶段**（[u9-l1](u9-l1-trust-model-stages.md)）：manifest 是规约真相来源，代码服从规约、不可改规约迎合代码。验证器正是这条边界的**机械执行者**。
- **manifest 结构**（[u4-l1](u4-l1-manifest-organization-loading.md)、[u4-l2](u4-l2-signature-shape-rules.md)、[u4-l3](u4-l3-workloads-and-source.md)）：每个算子条目含 `signature` / `workloads` / `roofline` / `source` / `status`；`signature.inputs` 是有序映射，`source.kernel_map` 是 dispatch 登记表。
- **三个 codegen 契约方法**（[u8-l1](u8-l1-codegen-init-subclass-hook.md)、[u8-l3](u8-l3-dtype-codegen.md)）：`_infer_output_shapes`、`_validate_dtypes`、`eval_roofline` 由 `__init_subclass__` 据 manifest 自动合成。验证器的 parity 检查就是去**调用这些合成出的方法**看它们是否守约。
- **基准三件套**（[u6-l2](u6-l2-manifest-driven-benchmark.md)、[u6-l3](u6-l3-benchmark-report-and-baselines.md)）：基准必须用 `load_workloads` 取形状、用 `op.eval_roofline()` 取 FLOP/字节，禁止本地硬编码公式。L4 就是这条信任边界的把关者。

补充三个本讲会用到的概念，怕初学者卡住：

- **advisory vs blocking**：CI 里「错误（error）」会让进程返回非零退出码、阻断流水线；「警告（warning）」只是打印、不阻断。TileOPs 把检查分成两类：基础检查（L0–L4）**始终是 error**，而一组更严格的 parity 检查（C1–C7）**默认是 warning（advisory），用 `--strict` 才升为 error（blocking）**——这样可以把严格的关卡先落地，再慢慢清存量。
- **`inspect.signature` / `.bind`**：反射出一个函数的形参表，`.bind(*args, **kwargs)` 模拟一次调用、检查实参能否对上形参而**不真正执行函数体**。验证器用它做 parity 探测的接缝（见 [u8-l3](u8-l3-dtype-codegen.md)）。
- **AST（抽象语法树）解析**：把一段 Python 源码解析成语法树再遍历，**不执行它**。L4 用 AST 检查基准文件「确实导入并调用了 `load_workloads`」，比「在文件里搜字符串」更可靠——不会被注释或变量名骗到。

一个最关键的心智锚点：**验证器是「manifest 这份规约」的编译期/静态检查器**。它不跑 GPU、不构造真正的 Op（除反射调用 codegen 方法外），它的全部工作就是把 YAML 声明与 Op 类的**声明性属性**（forward 形参、类体里有没有 `dispatch_kernel`、合成方法是不是还是基类 stub）逐条比对。读懂本讲，你就读懂了 TileOPs「spec-driven」四字背后那道闸门。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [scripts/validate_manifest.py](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/scripts/validate_manifest.py) | **本讲主角**。3700 行的单文件验证器：L0–L4 检查函数、C1–C7 严格 parity、`validate_manifest` 编排器、`main` CLI。 |
| [tests/test_validate_manifest.py](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tests/test_validate_manifest.py) | 验证器的单测；用**合成 manifest**逐条验证每个检查分支，是理解行为的最佳参考。 |
| [docs/design/manifest.md](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/docs/design/manifest.md) | 规约本体：R15（状态门控）、source 字段表（`kernel_map` 必填）、R21（workload 键）等。 |
| [.github/workflows/preflight.yml](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/.github/workflows/preflight.yml) | CI 实际调用方式：`--levels schema,signature,shape,dtype,bench --strict`。 |

## 4. 核心概念与源码讲解

### 4.1 五级检查全景：范围、数据来源与状态门控

#### 4.1.1 概念说明

`validate_manifest.py` 的模块文档字符串一句话概括了五级：

[scripts/validate_manifest.py:1-19](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/scripts/validate_manifest.py#L1-L19) — 这段 docstring 列出 schema / signature / shape / dtype / bench 五类检查，并声明「spec-only 算子只过 schema，implemented 算子过全部」。

每级的**范围**与**数据来源**如下表：

| 级别 | 标签 | 检查什么 | 数据来源 |
| --- | --- | --- | --- |
| **L0 schema** | `[schema]` | YAML 结构：必填字段、类型、嵌套、`kernel_map` 必填、`variant_of` 一致性、源文件存在 | manifest YAML（纯静态） |
| **L1 signature** | `[signature]` | `Op.forward()` 形参 == manifest `inputs`（按序）∪ `params` | manifest `signature` + 导入 Op 类反射 `forward` |
| **L2 shape** | `[shape]` | `shape_rules` 是合法 Python 表达式；**parity**：`_infer_output_shapes` 在 mock 输入下的输出满足 `shape_rules` 与声明 shape | manifest `signature` + 反射调用 codegen 方法 |
| **L3 dtype** | `[dtype]` | dtype 串是合法 torch 类型 / `same_as(ref)` / `promote_int_to_float`；`dtype_combos` 数据合法；**parity**：`_validate_dtypes` 接受所有声明组合、拒绝越界组合 | manifest `signature` + 反射调用 codegen 方法 |
| **L4 bench** | `[bench]` | 基准文件用 AST 证明：导入了 `load_workloads` 并以算子名为参调用、调用了 `op.eval_roofline()`（或等价的 `workloads_to_params` / `ManifestBenchmark`） | manifest `source.bench` 指向的基准源文件 |

关键认知：**L0 完全是 YAML 静态检查，不导入任何 tileops 代码**；L1–L4 才会导入 Op 类做反射。这决定了「L0 失败就短路、不再跑 L1–L4」的设计（见 4.1.2）。

#### 4.1.2 核心流程

整个验证由编排器 `validate_manifest(...)` 驱动，对每个算子条目执行如下流程：

```text
对 manifest 里每个 op_name, entry:
 │
 ├─ 若 --check-op 指定：只处理 {check_op} ∪ {variant_of==check_op 的变体}，其余跳过
 │
 ├─ L0 schema（若 "schema" in levels）
 │     check_l0() + check_source_paths()
 │     若有 schema 错误 → 记录后 continue（短路，跳过 L1-L4）   ★
 │
 ├─ 计算 spec_only = (status == "spec-only")
 ├─ 若 spec_only 且 未指定 --check-op → continue（只过 L0）      ★ 状态门控
 │
 ├─ 反射解析 Op 类（_resolve_op_class），供 parity 检查复用
 │
 ├─ L1 signature  → all_errors（始终硬错误）
 ├─ L2 shape      → check_l2 (硬) + check_l2_infer_parity (C1，进 strict_errors)
 ├─ L3 dtype      → check_l3 (硬) + check_l3_validate_dtypes_parity (C2，进 strict_errors)
 ├─ C3-C7 严格 parity（ctor/forward/dispatch/stub）→ strict_errors
 └─ L4 bench      → check_bench_declaration (硬) + check_l4_benchmark (依 bench_manifest_driven 定硬/软)

全部算子处理完后：
  strict_errors → 若 --strict：并入 all_errors（blocking）
                 否则：每条加前缀 "STRICT-PARITY (advisory):" 并入 all_warnings（不阻断）
  返回 (dedup(errors), dedup(warnings))
```

两条标 ★ 的短路/门控规则是理解「为什么有的算子只报 schema 错误」的关键。

#### 4.1.3 源码精读

**状态门控 + 短路**（编排器主循环）：

[scripts/validate_manifest.py:3503-3516](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/scripts/validate_manifest.py#L3503-L3516) — L0 schema 先跑；`if schema_errors: continue` 实现短路；随后 `if spec_only and check_op is None: continue` 实现「spec-only 只过 L0」的门控。注意条件里多了 `and check_op is None`——这正是 `--check-op` 能**强行让 spec-only 算子也跑 L1–L4** 的开关。

`ALL_LEVELS` 常量定义了五级名字集合，是 `--levels` 标志的合法取值：

[scripts/validate_manifest.py:3428](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/scripts/validate_manifest.py#L3428) — `ALL_LEVELS = frozenset({"schema","signature","shape","dtype","bench"})`。

**advisory / `--strict` 双模式路由**（编排器末尾）：

[scripts/validate_manifest.py:3602-3606](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/scripts/validate_manifest.py#L3602-L3606) — `strict_parity` 为真（`--strict` 或环境变量 `MANIFEST_STRICT_BLOCKING=1`）时，C1–C7 严格 parity 失败并入 errors（blocking）；否则降级为带 `STRICT-PARITY (advisory):` 前缀的 warning。这就是 advisory 与 strict 的本质区别。

CLI 入口 `main()` 读取这两个开关：

[scripts/validate_manifest.py:3661-3664](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/scripts/validate_manifest.py#L3661-L3664) — `--strict` 命令行标志或环境变量 `MANIFEST_STRICT_BLOCKING=1` 任一为真即开启 strict 模式。

#### 4.1.4 代码实践

1. **实践目标**：在真实 manifest 上观察 advisory 与 strict 模式的差异。
2. **操作步骤**：
   - 先跑 advisory（默认）：
     ```bash
     python scripts/validate_manifest.py --levels schema,signature,shape,dtype,bench
     ```
   - 再跑 strict：
     ```bash
     python scripts/validate_manifest.py --levels schema,signature,shape,dtype,bench --strict
     ```
3. **需要观察的现象**：两者开头分别打印 `parity-mode: ADVISORY` 与 `parity-mode: STRICT`；advisory 模式会额外打印一段「STRICT-PARITY (C1-C7) failures are reported as warnings and do NOT block」提示。对比两者的 `WARNING:` 与 `FAILED:` 计数——同一批 C1–C7 parity 失败，在 advisory 里是 warning、在 strict 里是 error。
4. **预期结果**：若当前代码库已清完存量 parity 债，两者都可能 `All manifest checks passed.`；否则 strict 会在 advisory 仅 warning 的项上报 error 并返回退出码 1。
5. **环境说明**：验证器需 tileops 可导入（torch/tilelang），但全程**不执行 GPU kernel**（parity 探测用 `torch.empty(0, device="cpu")` 与 `cls.__new__` 构造 mock）。待本地验证具体计数。

#### 4.1.5 小练习与答案

- **练习 1**：为什么编排器要在 L0 schema 失败时 `continue` 而不是继续跑 L1–L4？
  - **答**：L1–L4 都依赖一个结构合法的 `entry`（如 `check_l1` 要读 `sig.get("inputs")`）。YAML 结构都坏了，后续检查会因 KeyError/AttributeError 崩溃或报一堆派生噪声。先修好 schema 再谈深层一致性，符合「fail fast、单一根因」原则。
- **练习 2**：`spec_only and check_op is None` 这个条件里，去掉 `and check_op is None` 会破坏什么功能？
  - **答**：会破坏 `--check-op` 对 spec-only 算子强制全级检查的能力——`--check-op SomeSpecOnlyOp` 将仍被门控跳过 L1–L4。这个条件正是 R15「`--check-op` forces L0-L4 on a targeted entry」的代码落点。

---

### 4.2 L0 schema 检查与 `kernel_map` 必填

#### 4.2.1 概念说明

L0（`check_l0`）是唯一纯 YAML 的级别，也是 spec-only 算子唯一会跑的级别。它的职责是：**在动任何 Python 反射之前，保证 YAML 在结构上是自洽的**——必填字段齐全、类型正确、`variant_of` 引用有效、`source` 指向的文件真实存在。

本讲的重点是 L0 里的一条**本轮刚升级**的规则：`status: implemented` 的算子**必须**声明 `source.kernel_map`（dispatch key → Kernel 类名的登记表）。它的依据在规约里写得很清楚——manifest.md 的 source 字段表把 `kernel_map` 标为「Required when `status: implemented`」：

[docs/design/manifest.md:350-357](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/docs/design/manifest.md#L350-L357) — `kernel_map` 行注明「Required when `status: implemented`」，`bench_manifest_driven` 行注明「Required `true` when `status: implemented`; makes L4 a hard CI error」。

为什么必须？因为 implemented 算子运行时通过 `default_kernel_map` 派发 kernel（见 [u2-l2](u2-l2-kernel-selection-and-arch.md)）；若 manifest 不登记这张表，等于把派发逻辑对 spec 隐藏——读者无法从规约知道这个算子用了哪些 kernel。

#### 4.2.2 核心流程

`_l0_kernel_map(op_name, entry, warnings)` 的判定逻辑：

```text
取 source.kernel_map
 │
 ├─ 若 kernel_map 不是 None：
 │     若不是 dict        → 报 "[schema] ... kernel_map must be a mapping"
 │     否则逐项检查 k,v   → 任一非 str → 报 "kernel_map entries must be str -> str"
 │
 └─ 若 kernel_map 是 None（即未声明）：
       若 status == "implemented" → 报硬错误：
            "status is 'implemented' but kernel_map is missing
             (must be a mapping of str -> str)"            ★ 本轮升级点
       否则（spec-only 或缺 status）→ 不报
```

`check_l0` 用一张表 `_L0_SECTIONS` 驱动 signature / workloads / roofline / source 四个子段校验，`kernel_map` 是其中由 `_l0_kernel_map` 专门处理的子规则：

[scripts/validate_manifest.py:583-588](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/scripts/validate_manifest.py#L583-L588) — `_L0_SECTIONS` 表把四个子段的「容器类型 + 校验函数」声明式地串起来。

#### 4.2.3 源码精读

**`kernel_map` 缺失的硬错误分支**——这是本讲最关键的一处源码：

[scripts/validate_manifest.py:546-576](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/scripts/validate_manifest.py#L546-L576) — `_l0_kernel_map`。注意 `elif entry.get("status") == "implemented": err(...)` 这一支调用的是 `err`（往 `errors` 列表追加），而非 `warnings.append`。docstring 也从旧的「advisory (warning)」改成了「required when implemented」。

`err` 与 `warn` 都由工厂函数 `_emit_to` 生成，区别只在追加进哪个 sink（`errors` 还是 `warnings`）：

[scripts/validate_manifest.py:546-555](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/scripts/validate_manifest.py#L546-L555) — 函数签名仍保留 `warnings: list[str] | None` 形参（向后兼容调用点），但 implemented 缺 `kernel_map` 的分支已不再使用它，改走 `err`。

**测试佐证**（行为契约）：

[tests/test_validate_manifest.py:374-390](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tests/test_validate_manifest.py#L374-L390) — `test_kernel_map_status_gating` 三段断言：implemented 缺 kernel_map → `assert any("kernel_map is missing" in e for e in errors)`（**断言进 errors，是硬错误**）；spec-only 缺 kernel_map → 无任何 kernel_map 诊断；`kernel_map: {}`（空映射）合法通过。

#### 4.2.4 代码实践

1. **实践目标**：亲手触发并确认「implemented 缺 kernel_map」是硬错误而非警告。
2. **操作步骤**（源码阅读型，无需运行环境）：
   - 读 `_l0_kernel_map`（[L546-576](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/scripts/validate_manifest.py#L546-L576)），对照 `_emit_to`（[L2425-L2434](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/scripts/validate_manifest.py#L2425-L2434)）确认 `err` 追加进 `errors`、`warn` 追加进 `warnings`。
   - 读测试 `test_kernel_map_status_gating`（[L374-390](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tests/test_validate_manifest.py#L374-L390)），看断言如何区分「进 errors」与「无诊断」。
   - 可选：用合成 manifest 复现——在 python 里 `import` 验证器模块（测试用 `importlib.util.spec_from_file_location` 加载，见 [tests/test_validate_manifest.py:26-31](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tests/test_validate_manifest.py#L26-L31)），构造一个 `status: implemented` 但无 `kernel_map` 的 entry，调用 `check_l0("t", entry)` 观察返回里含 `"kernel_map is missing"`。
3. **需要观察的现象**：返回列表里出现 `[schema] t: status is 'implemented' but kernel_map is missing (must be a mapping of str -> str)`，且它在 `validate_manifest(...)` 的返回 `(errors, warnings)` 中落在 **errors** 元组。
4. **预期结果**：与测试断言一致——这是会让 `main()` 返回退出码 1 的硬错误。
5. 待本地验证（若环境可导入验证器模块）。

#### 4.2.5 小练习与答案

- **练习 1**：对比 advisory 与这条 `kernel_map` 规则——为什么 `kernel_map` 缺失走的是硬错误，而不是像 C1–C7 那样默认 advisory？
  - **答**：`kernel_map` 属于 L0 基础 schema 检查（`check_l0` 返回进 `all_errors`），基础检查**一律是硬错误**，不经过 strict_errors 路由。只有 C1–C7 这组「严格 parity」才享受 advisory/strict 双模式。换言之，「manifest 结构性完整性」是底线、无折衷空间。
- **练习 2**：一个 `kernel_map: {}`（空映射）的 implemented 算子能通过 L0 吗？合理吗？
  - **答**：能通过（`_l0_kernel_map` 对空 dict 不报错，测试 [L388-390](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tests/test_validate_manifest.py#L388-L390) 证实）。它表示「已声明这张表、只是当前为空」——比如复合算子的派发下放给子 Op 的场景。规约要求的是**声明存在**，而非非空。

---

### 4.3 L1 signature 与 L4 bench 契约

#### 4.3.1 概念说明

L1 和 L4 是两端的一对检查：**L1 管「Op 类的形参表与 manifest 对齐」**（输入侧），**L4 管「基准文件真的在用 manifest 的数据」**（评测侧）。

- **L1（`check_l1` → `check_l1_signature`）**：导入 Op 类，反射 `forward()` 与 `__init__()` 的形参名，断言 manifest 声明的 `inputs` 按**声明顺序**出现在 `forward()` 前缀、每个 manifest `params` 都能在 `__init__()` 或 `forward()` 里找到、每个 `static_dims` 键都是 `__init__` 形参。这是 R1「有序字典」与 R3「参数放置」的机械落地。
- **L4（`check_l4_benchmark` + `check_bench_declaration`）**：用 AST 解析 `source.bench` 指向的基准文件，证明它**导入了 `load_workloads`（来自 `tileops.manifest`）并以该算子名为参调用**、**调用了 `eval_roofline()`**（直接 `op.eval_roofline()` 或经 `ManifestBenchmark`/`workloads_to_params` 等价路径）。这正对应 u6-l2/u6-l3 反复强调的「禁止本地硬编码形状与公式」——L4 把它变成一条可执行的 CI 规则。

#### 4.3.2 核心流程

**L1**（`check_l1_signature`，[L760-827](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/scripts/validate_manifest.py#L760-L827)）：

```text
expected = list(manifest_inputs.keys())              # inputs 按声明序
        + [params 中出现在 forward_params 里的名]
若 forward_params != expected              → 报 "forward() params ... do not match manifest order ..."
对每个 manifest param：
  若不在 (forward_params ∪ init_params)     → 报 "manifest param 'x' not found in __init__() or forward()"
对每个 static_dims 键：
  若不在 init_params                        → 报 "static_dims key 'N' not found in __init__() (R20)"
```

**L4**（`check_l4_benchmark`，[L2996-3039](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/scripts/validate_manifest.py#L2996-L3039)）：

```text
AST 解析 bench 文件
usage = _ast_manifest_call_usage(tree, op_name, {"load_workloads","eval_roofline"})
  遍历 ImportFrom 与 Call 节点：
    - load_workloads：须 from tileops.manifest import 且以 op_name 为首参调用
    - eval_roofline：须有形如 x.eval_roofline() 的属性调用
    - 等价路径：workloads_to_params / ManifestBenchmark（来自 benchmarks.benchmark_base）也认
若 not usage["load_workloads"] → 报 "must import load_workloads ... and call it with op name"
若 not usage["eval_roofline"] → 报 "must call eval_roofline() on an Op instance or use ManifestBenchmark ..."
```

L4 的报错最终是「硬错误」还是「警告」，由 `source.bench_manifest_driven` 决定（编排器 [L3577-3584](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/scripts/validate_manifest.py#L3577-L3584)）：声明 `true` 则 L4 错误进 errors（硬）；未声明则降级为 warning。而 `check_bench_declaration`（[L3407-3425](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/scripts/validate_manifest.py#L3407-L3425)）进一步要求：**implemented 且有 bench 指针的算子必须声明 `bench_manifest_driven: true`**，不许默默 opt-out。

#### 4.3.3 源码精读

**L1 形参反射**——取 `forward()` 显式命名形参（排除 `self`、`*args`、`**kwargs`）：

[scripts/validate_manifest.py:905-918](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/scripts/validate_manifest.py#L905-L918) — `_get_forward_params` 用 `inspect.signature` 仅保留显式命名参数，因为 manifest params 必须以命名实参出现。

**L4 的 AST 等价路径识别**——这是 L4 最精巧的部分，把「直接调用」与「经 helper 间接调用」都认作合规：

[scripts/validate_manifest.py:2938-2993](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/scripts/validate_manifest.py#L2938-L2993) — `_ast_manifest_call_usage` 维护一张 `_INDIRECT_EQUIV` 等价表（`workloads_to_params`≡`load_workloads`、`ManifestBenchmark`≡`eval_roofline`），既认直接导入也认间接 helper；`_call_uses_expected_op_name` 还解析模块级 `NAME = 'op'` 常量绑定，支持「把算子名存进变量再传参」的写法。

**bench 契约声明要求**：

[scripts/validate_manifest.py:3407-3425](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/scripts/validate_manifest.py#L3407-L3425) — `check_bench_declaration`：implemented + 有 bench → 必须声明 `bench_manifest_driven: true`，否则报硬错误。spec-only 或无 bench 指针者豁免。

#### 4.3.4 代码实践

1. **实践目标**：理解 L4 如何用 AST 判定一个基准文件是否合规。
2. **操作步骤**（源码阅读型）：
   - 挑一个 implemented 算子的基准文件，如 `benchmarks/ops/bench_norm.py`（与 [u6-l2](u6-l2-manifest-driven-benchmark.md) 讲过的 `workloads_to_params` / `ManifestBenchmark` 对应）。
   - 在其中定位 `load_workloads` / `workloads_to_params` / `ManifestBenchmark` / `eval_roofline` 的导入与调用点。
   - 对照 `_ast_manifest_call_usage`（[L2938-2993](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/scripts/validate_manifest.py#L2938-L2993)）判断：若把 `load_workloads` 的调用注释掉，L4 会报哪条 error？
3. **需要观察的现象**：合规基准里上述符号的导入源分别是 `tileops.manifest`（直接）或 `benchmarks.benchmark_base`（间接 helper），调用首参是算子名常量。
4. **预期结果**：`check_l4_benchmark("RMSNormFwdOp", "benchmarks/ops/bench_norm.py", repo_root)` 返回空列表（合规）；删掉 `load_workloads` 调用后返回含 `"must import load_workloads ... and call it with op name"` 的列表。
5. 待本地验证。

#### 4.3.5 小练习与答案

- **练习 1**：L4 为什么用 AST 而不是简单的 `grep "load_workloads"` 字符串匹配？
  - **答**：字符串匹配会被注释、文档字符串、或恰好同名的局部变量骗过——既会漏报（关键调用被注释掉仍匹配字符串）也会误报。AST 解析只认真正的 `import` 与 `Call` 节点，能区分「导入了且调用了」与「仅仅在注释里提到」。
- **练习 2**：一个 implemented 算子声明了 `source.bench` 但没写 `bench_manifest_driven`，会发生什么？
  - **答**：`check_bench_declaration` 报硬错误 `[bench] ... source.bench_manifest_driven must be declared true`（[L3421-3425](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/scripts/validate_manifest.py#L3421-L3425)）。规约不允许 implemented 算子默默退出 manifest 驱动基准契约。

---

### 4.4 L2/L3 的 parity 扩展与 `--check-op`

#### 4.4.1 概念说明

L2、L3 各有两层：**一层是纯 manifest 数据检查**（`shape_rules` 语法、dtype 串合法性、`dtype_combos` 数据合法），**另一层是 parity（一致性）扩展**——反向调用 Op 类上由 codegen 合成的方法，验证它们与 manifest 声明一致。后者就是 C1（shape parity）与 C2（dtype parity），属于 C1–C7 严格 parity 家族，默认 advisory。

parity 的核心思想：manifest 声明了「这个算子在这些形状/ dtype 下输出这些形状 / 接受这些 dtype」；codegen 据此合成出 `_infer_output_shapes` / `_validate_dtypes`（见 [u8-l1](u8-l1-codegen-init-subclass-hook.md)、[u8-l3](u8-l3-dtype-codegen.md)）。验证器**构造 mock 输入、反射调用这两个方法**，看它们的输出是否真的满足 manifest 的 `shape_rules` 与 dtype 声明。这是「代码服从规约」在形状/dtype 维度的闭环验证——全程在 CPU 上、不跑 GPU。

`--check-op <name>` 则是一个**作用域开关**：把全部五级（含 spec-only 本会被门控跳过的 L1–L4）强制作用到指定算子及其**变体家族**（`variant_of == name` 的所有变体）上，其余算子一律跳过。用途是在改某个算子时只验证它一家，而不必扫全量。

#### 4.4.2 核心流程

**`--check-op` 家族作用域**（编排器 [L3470-3477](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/scripts/validate_manifest.py#L3470-L3477)）：

```text
若 --check-op 指定：
  variant_family = {check_op}
                ∪ {name : entry["variant_of"] == check_op}
  循环里：op_name not in variant_family → 跳过
  门控条件 "spec_only and check_op is None" → 因 check_op 非 None，spec-only 也跑 L1-L4
若 --check-op 名字不在 manifest → 立即返回错误 "--check-op: op '...' not found"
```

**C1 shape parity**（`check_l2_infer_parity`，[L1910-2176](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/scripts/validate_manifest.py#L1910-L2176)）：

```text
若类未 override _infer_output_shapes → warn（不静默通过），跳过
mock_shapes = _mock_input_shapes(sig)        # 从 shape_rules/shape 声明合成 mock 形状
mock_self   = _build_mock_self(cls, ...)     # cls.__new__ 构造，注入 params 与 static_dims
result = infer_fn(mock_self, **{name_shape: ...})   # 反射调用，不跑 GPU
对每条 shape_rules[i]：
  在含 inputs/outputs/params/dim_sizes 的 ctx 里 eval
  若 result 输出使规则为假 → 报 "_infer_output_shapes output violates shape_rules[i]"
对每个声明 output shape：
  比对 result 的秩与各维（input-bound 符号须精确匹配）
```

**C2 dtype parity**（`check_l3_validate_dtypes_parity`，[L2537-2860](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/scripts/validate_manifest.py#L2537-L2860)）：遍历 `dtype_combos`（或无 combos 时遍历笛卡尔积），对每个组合构造 mock CPU 张量、反射调用 `_validate_dtypes`，断言**声明的组合被接受、越界组合被拒绝**；还有专门的越界负向探针 `_probe_out_of_union`。

#### 4.4.3 源码精读

**`--check-op` 家族作用域与名字校验**：

[scripts/validate_manifest.py:3466-3477](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/scripts/validate_manifest.py#L3466-L3477) — 名字不在 manifest 立即报错；否则构造 `variant_family = {check_op} | {variant_of==check_op 的变体}`。

**C1 的 mock-self 构造**（不跑 `__init__`、不碰 GPU）：

[scripts/validate_manifest.py:1970-1999](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/scripts/validate_manifest.py#L1970-L1999) — 解析 `static_dims` 成具体整数注入 mock self（避免 `self.N` 触发 AttributeError 而误跳过），用 `inspect.signature(infer_fn).bind(...)` 先验形参对齐，再调用。body 抛异常视为硬 L2 错误（[L1998-2009](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/scripts/validate_manifest.py#L1998-L2009)）。

**C2 的负向探针**：

[scripts/validate_manifest.py:2463-2534](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/scripts/validate_manifest.py#L2463-L2534) — `_probe_out_of_union`：从一个已知接受的 baseline 组合出发，逐个把非 `same_as` 绑定的输入换成「声明并集之外」的 dtype，断言每次都被 `_validate_dtypes` 拒绝；`self.dtype` 钉在 baseline 主 dtype，确保只有输入张量偏离。

**C1–C7 的标签与 strict-only 标签**（路由辅助）：

[scripts/validate_manifest.py:3375-3382](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/scripts/validate_manifest.py#L3375-L3382) — `STRICT_TAGS` 列出所有可能由 strict parity 产生的标签；`STRICT_ONLY_TAGS`（`[ctor]/[forward]/[dispatch]/[stub]`）只由 strict parity 产生，便于断言「这些标签不会从基础检查泄漏」。

#### 4.4.4 代码实践

1. **实践目标**：用 `--check-op` 对单个算子跑全级检查，逐级读懂输出。
2. **操作步骤**：
   ```bash
   python scripts/validate_manifest.py --check-op RMSNormFwdOp
   ```
   （可加 `-v` 看每个被检算子的进度行；可加 `--strict` 把 C1–C7 升为 error。）
3. **需要观察的现象**：
   - 输出顶部 `check-op: RMSNormFwdOp` 与 `parity-mode: ADVISORY`（或 STRICT）。
   - 因 `--check-op`，即便该算子有 spec-only 的变体也会被纳入家族检查；不在家族里的算子完全不出现。
   - 若有 C1/C2 parity 偏差，advisory 模式下出现在 `WARNING:` 段（带 `STRICT-PARITY (advisory):` 前缀），`--strict` 下出现在 `FAILED:` 段。
4. **预期结果**：对当前 HEAD 的 `RMSNormFwdOp`，应通过全部五级（无 error）；advisory 模式下若有遗留 parity 债则只见 warning。退出码：有 error 为 1，否则 0。
5. 待本地验证（需 tileops 可导入环境；全程无 GPU 执行）。

#### 4.4.5 小练习与答案

- **练习 1**：C2 parity 为什么除了「正向」（声明组合应被接受）还要做「负向」（越界组合应被拒绝）探针？
  - **答**：只做正向会被一个「来者不拒」的 `_validate_dtypes` 骗过——它接受一切，自然也接受声明组合。负向探针确保实现**确实收紧到了声明的并集**，而非过宽。这是「契约」双向性的体现：既不能比声明窄（拒合法），也不能比声明宽（收非法）。
- **练习 2**：`--check-op RMSNormFwdOp` 会不会顺带检查 `RMSNormBwdOp`？为什么？
  - **答**：不会，除非 `RMSNormBwdOp` 的 `variant_of` 恰好是 `RMSNormFwdOp`。家族作用域只收集 `{check_op}` 与「`variant_of == check_op`」的变体（[L3474-3477](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/scripts/validate_manifest.py#L3474-L3477)），Fwd/Bwd 是各自独立的 primary，互不为变体。

---

## 5. 综合实践

把本讲四条主线串起来：**L0–L4 五级、`kernel_map` 硬错误、parity 扩展、advisory/strict 与 `--check-op`**。

任务：假设你要给一个**新实现的算子** `FooFwdOp` 翻转 `status: spec-only → implemented`，在合入前用验证器自查。

1. **跑全级 strict 验证**（模拟 CI 的把关）：
   ```bash
   python scripts/validate_manifest.py --check-op FooFwdOp --strict -v
   ```
2. **逐级核对**预期会触发的检查（按本讲四节对照）：
   - **L0**：`source.kernel_map` 必须已声明（否则 `[schema] FooFwdOp: status is 'implemented' but kernel_map is missing`，硬错误，退出码 1）；`source.bench_manifest_driven` 需为 `true`。
   - **L1**：`forward()` 形参前缀须等于 `signature.inputs` 的声明序；`static_dims` 键须是 `__init__` 形参。
   - **L2**：`_infer_output_shapes`（由 codegen 合成）在 mock 输入下的输出须满足 `shape_rules`（C1，strict 下为 error）。
   - **L3**：`_validate_dtypes` 须接受所有声明 dtype 组合、拒绝越界组合（C2，strict 下为 error）。
   - **L4**：`source.bench` 文件须 AST 可证地用 `load_workloads` 与 `eval_roofline`（或等价 helper）。
3. **对照 advisory**：去掉 `--strict` 重跑，确认 C1–C7 失败（若有）从 `FAILED:` 段挪到 `WARNING:` 段、退出码归 0——体会「strict 是把关、advisory 是过渡」的设计意图。
4. **记录**：把每级或过或不过的结论，连同对应的 `[schema]/[signature]/[shape]/[dtype]/[bench]/[stub]` 标签，整理成一份翻转前自查清单。标签前缀本身就是定位检查函数的索引（如 `[stub]` → `check_c6/c7`、`[dispatch]` → `check_c5`）。

> 若本地无可导入环境，可改读 `TestCheckOp`（[tests/test_validate_manifest.py:2115-2298](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tests/test_validate_manifest.py#L2115-L2298)）与 `test_kernel_map_status_gating`（[L374-390](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tests/test_validate_manifest.py#L374-L390)），用合成 manifest 在 CPU 上复现同样的判定路径。

## 6. 本讲小结

- 验证器把 manifest 这份规约变成 **L0 schema / L1 signature / L2 shape / L3 dtype / L4 bench** 五级关卡；L0 纯 YAML 静态检查，L1–L4 才反射导入 Op 类。
- **状态门控（R15）**：`spec-only` 只过 L0；`implemented` 过全部；`--check-op <name>` 强制把全部五级作用到该算子及其变体家族，绕过 spec-only 门控。
- **本轮升级**：`status: implemented` 缺 `source.kernel_map` 已从 advisory（warning）变为 **L0 硬错误**（`err`），报错文本从「should be」改为「must be」，由 `test_kernel_map_status_gating` 锁定。
- **双模式路由**：基础检查（L0–L4）始终是硬错误；C1–C7 严格 parity（含 L2 的 shape parity、L3 的 dtype parity、以及 ctor/forward/dispatch/stub）默认 **advisory**，仅 `--strict` 或 `MANIFEST_STRICT_BLOCKING=1` 时升为 blocking。CI 在 preflight 里以 `--strict` 跑全级。
- **parity 扩展**用「mock 输入 + `inspect.signature(...).bind(...)` + 反射调用 codegen 合成方法」在 CPU 上闭环验证「代码服从规约」，既有正向（声明组合被接受）也有负向（越界组合被拒绝）探针。
- **L4** 用 AST（非字符串匹配）证明基准文件确实用 `load_workloads` 取形状、`eval_roofline()` 取 FLOP/字节，并把「implemented 必须声明 `bench_manifest_driven: true`」落成硬规则。

## 7. 下一步学习建议

- **[u9-l3 Status 翻转与字段 carve-out](u9-l3-status-flip-carveout.md)**：本讲综合实践里的「翻转 status」正是 carve-out 规则约束的场景——实现 PR 只能窄化改 `status` / `source.kernel_map` / `source.test` / `source.bench` 等少量字段，且因 L0 要求 implemented 必填 `kernel_map`，翻转时通常须同 PR 补齐。两讲合读，可完整理解「翻转」的规约侧与验证侧。
- **重读 [u8-l1](u8-l1-codegen-init-subclass-hook.md) 与 [u8-l3](u8-l3-dtype-codegen.md)**：把 codegen（生产侧合成 `_validate_dtypes` / `eval_roofline` / `_infer_output_shapes`）与本讲（消费侧 parity 探测这些方法）对读，理解「同一份 manifest 声明被两套机制消费」的闭环。
- **阅读源码**：`scripts/validate_manifest.py` 的编排器 `validate_manifest`（[L3431-3608](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/scripts/validate_manifest.py#L3431-L3608)）是本讲的总线索；想深入某一 parity 探针，可从 `check_l2_infer_parity`（[L1910](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/scripts/validate_manifest.py#L1910)）与 `check_l3_validate_dtypes_parity`（[L2537](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/scripts/validate_manifest.py#L2537)）入手。
