# Status 翻转与字段 carve-out

## 1. 本讲目标

本讲承接 u9-l1 的四阶段信任模型，聚焦一个具体的工程动作：**把一个算子从 `status: spec-only` 翻转成 `status: implemented`**。

读完本讲，你应该能够：

1. 说清 `status` 字段的两种取值（`spec-only` / `implemented`）的语义，以及翻转发生在「实现 PR」里的时机。
2. 默写出**实现 PR 允许触碰的窄化字段集**（carve-out），并判断一个具体的 manifest 改动是合法还是越界。
3. 解释为何翻转后 `source.kernel_map` **通常必须同时补齐**——因为 L0 schema 检查已把它从「advisory 警告」升级为「硬错误」。
4. 理解 carve-out 只是「收窄禁止项」，绝不是「放松信任边界」，也绝不许为了迎合代码去改 manifest。

---

## 2. 前置知识

本讲默认你已经掌握 u9-l1 讲清的四阶段信任模型，这里只做最小回温，不重复细节。

**四阶段流水线**：

```
Manifest → Test → Implementation → Benchmark
```

每个阶段用三个标题声明自己的信任契约：

- **OWNS**——本阶段作者拥有什么（必填）。
- **MUST NOT WRITE**——本阶段绝对不能写什么（必填）。
- **MUST NOT**——结构性耦合禁令，通常是禁止某些 import（选填）。

关键直觉：**阶段之间彼此独立，任何一方都不能静默地削弱另一方的保证**。reads（读访问）不被监管，信任模型只管 write（写）和 import 级别的耦合。

**Manifest 阶段**是 op 接口的唯一真相来源，人审、独立 PR。它 OWNS 算子签名、dtype、workload 形状、roofline 公式、status、kernel_map（dispatch 登记表）、`torch_compile_fullgraph` 等「用户可见的能力声明」；MUST NOT WRITE kernel 内部、dispatch 策略或测试逻辑。

**`status` 是什么？** 它是 manifest 条目里的一个枚举字段，取值两种：

- `spec-only`：只有规约，还没有实现。此时该 op 的 manifest 是「设计稿」，validator 只跑 L0（纯 YAML 静态检查），codegen（见 u8-l1）会跳过、保留 `NotImplementedError` stub。
- `implemented`：规约与实现齐备。validator 跑全级 L0–L4，codegen 在类定义时合成真实方法体。

**翻转（status flip）** 就是指 `spec-only → implemented` 这一次跃迁。它通常发生在「实现 PR」里——开发者写完 kernel/Op/test/bench，在同一个 PR 里把对应 op 的 manifest `status` 从 `spec-only` 改成 `implemented`，让实现正式「上岗」。

> 一个一句话定调（来自项目根文档）：实现不服从规约时，标 `status: spec-only`，在后续 PR 修代码——**绝不改 manifest 去迎合代码**。

---

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [`CLAUDE.md`](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/CLAUDE.md) | 顶层确立 design-first / spec-driven 哲学：code conforms to spec，反之不成立。 |
| [`docs/design/trust-model.md`](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/docs/design/trust-model.md) | 四阶段信任模型的权威描述，含 §Manifest 的 OWNS/MUST NOT WRITE 与「Status flip carve-out」小结。 |
| [`.claude/rules/manifest-trust-model.md`](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/.claude/rules/manifest-trust-model.md) | carve-out 的**完整枚举**：实现 PR 可改哪几个字段、其余字段需单独 manifest-only PR。 |
| [`scripts/validate_manifest.py`](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/scripts/validate_manifest.py) | 验证器。`_l0_kernel_map` 在 L0 把「implemented 但缺 kernel_map」判为硬错误——这是本讲「翻转后必须补 kernel_map」的执行机制。 |
| [`tests/test_validate_manifest.py`](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tests/test_validate_manifest.py) | 验证器测试。`test_kernel_map_status_gating` 锁定了「implemented 缺 kernel_map 报错、spec-only 缺则不报」的契约。 |
| [`tileops/manifest/normalization.yaml`](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/manifest/normalization.yaml) | 真实算子样例。`RMSNormFwdOp` 是 `implemented` 且填了 `source.kernel_map`，可对照「翻转后的正确长相」。 |

---

## 4. 核心概念与源码讲解

### 4.1 status 翻转流程

#### 4.1.1 概念说明

「翻转」是 spec-driven 开发里最重要的一次状态跃迁。在此之前，op 只活在设计稿里；翻转之后，它进入「可被用户调用、可被 validator 全级校验、可被基准评测」的正式轨道。

翻转的**动作本身**极小——就是把 YAML 里一行：

```yaml
status: spec-only
```

改成：

```yaml
status: implemented
```

但这一次改动会触发一连串自动机制：

1. **codegen 总开关打开**（u8-l1）：`Op.__init_subclass__` 看到 `status == "implemented"`，才会去合成 `_validate_dtypes`、`_infer_output_shapes`、`eval_roofline` 的真实方法体；`spec-only` 一律跳过、保留 `NotImplementedError` stub。
2. **validator 关卡收紧**：spec-only 只过 L0，implemented 要过 L0–L4 全级，且 L0/L2/L3/L4 的检查内容都更严。
3. **信任边界激活**：实现 PR 一旦翻转，它对 manifest 的写权限就被严格窄化成 carve-out（见 4.2）。

所以「翻转」不是孤立的一行 YAML 改动，而是**一把钥匙**——它同时打开 codegen、收紧 validator、激活 carve-out 的窄化写权限。

#### 4.1.2 核心流程

一个典型的「实现 → 翻转」PR，按时间顺序：

```
1. 在 tileops/kernels/ 下写 TileLang kernel（Implementation 阶段 OWNS）
2. 在 tileops/ops/    下写 Op（L2 调度）
3. 在 tests/ops/      下写正确性测试（Test 阶段 OWNS ref_program/check）
4. 在 benchmarks/ops/ 下写基准（Benchmark 阶段 OWNS）
5. 在 manifest YAML   下：
   a. 若是新 op：先在独立 manifest-only PR 里落 spec-only 条目
   b. 实现就绪后：本 PR 把 status: spec-only → implemented
   c. 填/对齐 source.{kernel_map, op, test, bench}
   d. （若 promotion）补 workloads 至少 2 条
6. 跑 scripts/validate_manifest.py --strict --check-op <op>，确认全绿
7. 跑正确性测试与基准，确认实现真的成立
```

注意第 5a：**规约先于实现独立落 PR**。这是信任模型的核心——manifest 由人审，不能和实现混在一个 PR 里被「裹挟」通过。第 5b 的翻转是「实现 PR 唯一被允许碰 manifest 契约字段」的入口，但能碰的字段是窄化的（见 4.2）。

#### 4.1.3 源码精读

先看根文档如何定调 spec-driven 的方向——code 服从 spec：

> [CLAUDE.md:7](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/CLAUDE.md#L7)：`design-first, spec-driven` 开发——设计文档与 `tileops/manifest/` 是权威规约；**code conforms to the spec, not the other way around**。

这条方向定下后，trust-model.md 进一步把它落到阶段契约。翻转属于「Implementation 阶段产出了一个能跑的实现，要正式入册」的事件：

> [trust-model.md:19-32](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/docs/design/trust-model.md#L19-L32)：§Manifest 说明 Manifest 阶段 OWNS 算子签名、dtype、workload 形状、roofline 公式、status、kernel_map、`torch_compile_fullgraph` 等，且 MUST NOT WRITE kernel 内部/dispatch 策略/测试逻辑。Manifest 是「人审、独立 PR」。

再看真实样例：`RMSNormFwdOp` 已经是 `implemented`：

> [normalization.yaml:7-10](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/manifest/normalization.yaml#L7-L10)：`RMSNormFwdOp` 条目，`status: implemented`。这就是「翻转完成后」的样子——它已通过 codegen 与全级 validator。

#### 4.1.4 代码实践

**实践目标**：用一个真实算子，亲手追踪「翻转」会激活哪些机制。

**操作步骤**：

1. 打开 `tileops/manifest/normalization.yaml`，找到 `RMSNormFwdOp` 条目，确认它是 `status: implemented`。
2. 在脑中把这一行临时改成 `status: spec-only`，然后回答：
   - codegen（u8-l1）会做什么？（提示：跳过合成、保留 stub）
   - validator（u9-l2）的关卡会怎样变化？（提示：只过 L0，跳过 L1–L4 与 parity 扩展）
3. 跑（或阅读）验证器对这个算子的检查：
   ```bash
   python scripts/validate_manifest.py --strict --check-op RMSNormFwdOp
   ```

**需要观察的现象**：
- `--check-op` 会对该算子及其变体家族跑全级检查；因为是 `implemented`，应输出 L0–L4 全部通过（或仅 advisory 提示）。
- 若（脑内或本地临时）把它降回 `spec-only`，L1–L4 会被跳过，只有 L0 schema 检查运行。

**预期结果**：你会直观看到「翻转」这一行 YAML 如何同时切换 codegen 开关与 validator 关卡强度。

> 若无 CUDA GPU 或环境不便，本步可降级为「源码阅读型实践」：阅读 `_l0_kernel_map` 与 `check_l0` 的调用关系即可，不必真跑。

#### 4.1.5 小练习与答案

**练习 1**：为什么规约（manifest）必须先于实现，以独立 PR 落地，而不能和实现混在一个 PR 里？

**参考答案**：因为信任模型要求各阶段独立、人审 manifest。若 manifest 和实现混在一个 PR，实现者就可能为了让自己的代码通过而顺手改 manifest 契约（比如放松 dtype 约束、改 shape 规则），从而静默削弱 spec 的保证。独立 PR + 人审强制 code 服从 spec，而不是 spec 迁就 code。

**练习 2**：一个 op 翻转成 `implemented` 之后，validator 对它的检查级别是变多还是变少？为什么？

**参考答案**：变多。`spec-only` 只过 L0（纯 YAML 静态检查），因为还没有实现可校验；`implemented` 要过 L0–L4 全级，包括 L1 signature 反射对齐、L2 shape parity、L3 dtype parity、L4 bench 契约。理由是：实现一旦「上岗」，它的真实方法体（codegen 合成）与代码签名必须和 manifest 契约逐一吻合，否则信任链断裂。

---

### 4.2 carve-out 字段集

#### 4.2.1 概念说明

信任模型的默认立场是：**实现 PR 不许碰 manifest 的契约字段**。因为实现者有「让代码通过」的利益冲突，放任它改契约就是请狐狸看鸡舍。

但翻转那一刻，确实有几样东西「非改不可」：

- 你把 `status` 从 spec-only 改成 implemented——`status` 本身就是契约字段，总得让实现 PR 改吧？
- 你新写的 test/bench 文件路径，得登记进 `source.test` / `source.bench` 吧？
- 新实现的 kernel 类，得登记进 `source.kernel_map` 吧？
- promotion 时原本可能没有 workloads，现在测试要求至少 2 条负载，得补吧？

**carve-out**（豁免清单）就是这份「实现 PR 被允许触碰的窄化字段集」。它的措辞非常克制——是「收窄禁止项」，不是「放松信任边界」。

#### 4.2.2 核心流程

carve-out 的判定可以画成一张决策表。给定实现 PR 对某个 aligned op 的 manifest 改动，逐字段判断：

| 字段 | 实现能否在翻转 PR 里改？ | 条件 |
| --- | --- | --- |
| `status` | ✅ 可以，任意方向 | 无（升/降级都允许） |
| `source.kernel_map` 条目 | ✅ 可以 | 无（登记新 kernel） |
| `source.test`、`source.bench` 路径值 | ✅ 可以 | 无（发现性指针，非契约字段，可指向本 PR 写的 per-op test/bench） |
| `workloads` | ⚠️ 有条件 | **仅当**本 PR 同时翻转 `spec-only → implemented`（promotion 强制非空 workloads） |
| `torch_compile_fullgraph` | ⚠️ 有条件 | **仅当**同时带上其注册的 compile-test 证据 |
| 其余所有契约字段 | ❌ 不行 | 需单独的 **manifest-only PR** + 人审 |

「其余所有」具体包括：`family`、`ref_api`、`signature`、`roofline.*`、`params`、`output-dtype`、`shape_rules`、`source.kernel`、`source.op`、`source.bench_manifest_driven`，以及**任何不配合翻转的 `workloads` 改动**。

> 两条铁律（来自项目根的规则文件）：
> 1. **Implementation does not conform to spec → set `status: spec-only`, fix code in follow-up PR. Never modify manifest to match code.**
> 2. **Do not remove `roofline.vars`, `shape_rules`, or `params` to silence validator errors.**

这两条堵住了最常见的两种「逃避」：为了让代码过而反向改 spec，以及为了让 validator 闭嘴而删契约字段。

#### 4.2.3 源码精读

carve-out 的**完整枚举**写在规则文件里，这是本讲最权威的源：

> [.claude/rules/manifest-trust-model.md:6-18](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/.claude/rules/manifest-trust-model.md#L6-L18)：`## Status flip carve-out` 小节。逐条列出实现 PR 可改的字段，并明确「Every other field … needs a separate manifest-only PR with human review」。结尾一句点题：**The carve-out narrows the prohibition; it does not relax the trust boundary.**

trust-model.md 把同一份清单浓缩成一段：

> [trust-model.md:26-30](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/docs/design/trust-model.md#L26-L30)：`### Status flip carve-out`——实现 PR 仅可改 `status`、`source.kernel_map`、`source.test`、`source.bench`、（仅 promotion 时）`workloads`、（仅随 compile-test 证据时）`torch_compile_fullgraph`；其余契约字段一律单独 manifest-only PR。并把完整枚举指回 `.claude/rules/manifest-trust-model.md`。

注意两个「发现性指针」字段 `source.test` / `source.bench` 为什么被豁免：规则原文称它们是 **discoverability pointers, not contractual fields**——它们只是告诉人「这个 op 的测试/基准在哪个文件」，不参与契约校验（不会影响 dtype/shape/roofline 的正确性判断），所以允许实现 PR 把它指向自己刚写的 per-op test/bench 文件。

#### 4.2.4 代码实践

**实践目标**：练习用 carve-out 清单对一个假想 PR 做 review。

**操作步骤**：阅读 [.claude/rules/manifest-trust-model.md:6-18](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/.claude/rules/manifest-trust-model.md#L6-L18) 的完整清单后，对下面这份「假想实现 PR 的 manifest diff」逐行判合法/越界：

```diff
 # RMSNormFwdOp 的实现 PR（已写完 kernel/op/test/bench）
   RMSNormFwdOp:
     ref_api: "torch.nn.functional.rms_norm"
     family: normalization
-    status: spec-only
+    status: implemented
     signature:
-      inputs:
-        x: {dtype: "float16"}            # 行 X：放宽了 dtype
+      inputs:
+        x: {dtype: "float16 | bfloat16"}
     roofline:
-      vars: { N: "x.shape[-1]" }          # 行 Y：删了 vars
     source:
-      kernel: tileops/kernels/norm/rms_norm.py
       kernel_map:
         rms_norm: RMSNormKernel
       op: tileops/ops/norm/rms_norm.py
-      test: tests/ops/test_old_rms_norm.py
+      test: tests/ops/test_rms_norm.py    # 行 Z：换了 test 路径
+      bench: benchmarks/ops/bench_norm.py # 行 W：新增 bench 路径
```

**需要逐行判断的项**：

| 行 | 改动 | carve-out 判定 |
| --- | --- | --- |
| status 翻转 | spec-only → implemented | ✅ 合法 |
| 行 X | 放宽 `signature.inputs.x.dtype` | ❌ 越界，属 `signature` |
| 行 Y | 删 `roofline.vars` | ❌ 越界，属 `roofline.*`，且直接违反「不许删 vars 压错」铁律 |
| 行 Z | 改 `source.test` 路径 | ✅ 合法（发现性指针） |
| 行 W | 加 `source.bench` 路径 | ✅ 合法（发现性指针） |

**预期结果**：行 X、行 Y 必须拆到单独的 **manifest-only PR** + 人审；行 Z、行 W、status 翻转、`kernel_map` 可留在本实现 PR。这就是 carve-out 的全部用法。

> 本实践是「源码阅读 + 判定」型，无需运行命令。结论可直接对照规则文件验证。

#### 4.2.5 小练习与答案

**练习 1**：一个实现 PR 想把 `signature.inputs.x.dtype` 从 `float16` 扩成 `float16 | bfloat16`，理由是「我的 kernel 反正两种都支持」。允许吗？

**参考答案**：不允许。`signature` 属契约字段，不在 carve-out 清单里，必须拆到单独 manifest-only PR + 人审。即便 kernel 支持，也不许在实现 PR 里顺手放宽契约——否则实现者会单方面扩大 op 的能力面，绕过人审。

**练习 2**：`source.test` / `source.bench` 为什么被 carve-out 豁免，而 `source.kernel` / `source.op` 不被豁免？

**参考答案**：因为 test/bench 路径是「发现性指针」——它们只告诉人和工具「测试/基准文件在哪」，不参与 dtype/shape/roofline 契约的正确性判断，所以允许实现 PR 指向自己刚写的 per-op 文件。而 `source.kernel` / `source.op` 指向实现入口、与 codegen 和 dispatch 强相关，属契约性登记，留给 manifest-only PR 审。

---

### 4.3 kernel_map 必填（L0 硬错误）

#### 4.3.1 概念说明

`source.kernel_map` 是 manifest 里的一张「dispatch key → Kernel 类名」登记表。比如 RMSNormFwdOp 的 `kernel_map: { rms_norm: RMSNormKernel }` 表示「`rms_norm` 这个 dispatch key 由 `RMSNormKernel` 实现」。

它是 Manifest 阶段 OWNS 的字段（属于「dispatch registration table」）。从 u2-l2 我们知道，Op 在构造期通过 `default_kernel_map` + 用户 override 组装出运行时 `kernel_map`；而 manifest 里的 `source.kernel_map` 是**规约侧的镜像**，告诉读者「这个 op 真正用哪些 kernel、怎么 dispatch」。

本讲的关键更新：**在 L0 schema 检查里，`implemented` 的 op 缺 `kernel_map` 已从「advisory 警告」升级为「硬错误」。** 这条规则直接决定了——**翻转时通常必须同时补齐 kernel_map**。

为什么？因为一个 `implemented` 的 op 必然通过 `default_kernel_map` dispatch 到真实 kernel，若 manifest 不登记这张表，等于把「op 到底用哪个 kernel」从规约里藏起来，破坏了「manifest 是 op 接口唯一真相来源」的根本承诺。

#### 4.3.2 核心流程

把「翻转」与「kernel_map 必填」串起来：

```
实现 PR 翻转 status: spec-only → implemented
        │
        ├── 1. carve-out 允许本 PR 改 status、source.kernel_map、source.test/bench…
        │
        ├── 2. L0 检查 _l0_kernel_map：
        │      if status == "implemented" and source.kernel_map is missing:
        │           → 硬错误（不再只是 warning）
        │
        ├── 3. 因此本 PR 必须同时补 source.kernel_map（否则 CI 的 validator 直接挂）
        │
        └── 4. 补 kernel_map 恰好落在 carve-out 允许的「source.kernel_map entries」上
                   → 合法，无需拆 PR
```

这个闭环很优雅：**carve-out 允许改 `source.kernel_map`**，而 **L0 又要求 implemented 必须有 `kernel_map`**——两者共同作用，使得「翻转时补 kernel_map」既必要又合法，不会卡在信任边界上。

特殊情况：若一个 op 翻转时确实没有传统意义上的 kernel（比如纯用 tensor primitive 实现的 elementwise，dispatch 到 fallback），它仍需在 `source.kernel_map` 填一个合法的 `str -> str` 映射（哪怕是单条），以满足 L0 的「mapping of str -> str」要求。空 dict `{}` 在类型上合法（见下方测试），但实务中通常至少登记一个 dispatch key。

#### 4.3.3 源码精读

先看真实的 implemented op 长什么样：

> [normalization.yaml:47-54](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tileops/manifest/normalization.yaml#L47-L54)：`RMSNormFwdOp` 的 `source` 块——含 `kernel`、`kernel_map: { rms_norm: RMSNormKernel }`、`op`、`test`、`bench`、`bench_manifest_driven`。这就是翻转后 `kernel_map` 已补齐的标准长相。

再看执行机制——validator 的 L0 检查：

> [scripts/validate_manifest.py:546-576](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/scripts/validate_manifest.py#L546-L576)：`_l0_kernel_map` 函数。docstring 直说 **kernel_map "required when implemented"**，理由是「An implemented op dispatches through `default_kernel_map`; omitting the declaration hides that dispatch table from the spec」。关键分支在 571–575 行：当 `entry.get("status") == "implemented"` 且 `kernel_map` 为空时，调用 `err(...)`（写入 errors，而非 warnings）——这就是从 advisory 升级为硬错误的代码点。

`check_l0` 在 schema 段统一调用它：

> [scripts/validate_manifest.py:666](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/scripts/validate_manifest.py#L666)：`errors.extend(_l0_kernel_map(op_name, entry, warnings))`——L0 主流程把 `_l0_kernel_map` 的错误并入 `errors`，意味着任何 implemented op 缺 `kernel_map` 都会让 L0 失败。

测试锁定了这条契约：

> [tests/test_validate_manifest.py:374-390](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tests/test_validate_manifest.py#L374-L390)：`test_kernel_map_status_gating`。三个断言：① implemented 且 pop 掉 kernel_map → 期望含 `"kernel_map is missing"` 的 error（硬错误）；② spec-only 且无 kernel_map → 不报任何 kernel_map 相关诊断；③ implemented 且 `kernel_map={}` → 合法（空映射也是 str->str）。

> 同文件 [test_validate_manifest.py:392-400](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/tests/test_validate_manifest.py#L392-L400)：`test_kernel_map_malformed_rejected`——非 mapping（如字符串）或值非 str（如 `{"fwd": 123}`）也会被 L0 拒绝。

#### 4.3.4 代码实践

**实践目标**：亲手验证「翻转后缺 kernel_map 会被 L0 判为硬错误」，并理解为什么补它既必要又合法。

**操作步骤**：

1. 阅读测试 `test_kernel_map_status_gating`（上面的链接），对照函数 `_l0_kernel_map` 的 571–575 行，确认「implemented 缺 kernel_map」走的是 `err()`（写 errors）而非 warnings。
2. 运行该测试，观察三段断言的行为：
   ```bash
   python -m pytest tests/test_validate_manifest.py::TestL0Checks::test_kernel_map_status_gating -v
   ```
   （若本地环境无 pytest/CUDA，改为阅读断言：三段断言已分别覆盖硬错误 / 不报 / 空映射合法。）
3. 推理题：设想你正在为 op `Foo` 提实现 PR，翻转为 implemented 时发现 manifest 没填 `source.kernel_map`。回答：
   - 你能在这个实现 PR 里直接补 `kernel_map` 吗？（对照 4.2 的 carve-out）
   - 如果 validator 仍把它当 advisory，你会怎样？现在它已是硬错误，又会怎样？

**需要观察的现象**：
- 步骤 2 的测试应**通过**——证明「implemented 缺 kernel_map 报错、spec-only 不报、空映射合法」这组契约成立。
- 若有人尝试在「翻转但不补 kernel_map」的状态下跑 validator，CI 在 preflight 的 `--strict` 全级检查就会因 L0 报 `kernel_map is missing` 而失败。

**预期结果**：
- 步骤 3 答案：**能**补——`source.kernel_map entries` 正在 carve-out 清单上，实现 PR 可直接填；正是因为它必须填（L0 硬错误），carve-out 才特意把它列进豁免，否则翻转会卡死。若 validator 仍当 advisory，翻转可能在「规约没登记 dispatch 表」的情况下蒙混过关；升级为硬错误后，这种蒙混在 CI 即被拦截。

> 若无运行环境，本步可降级为「源码阅读 + 推理」：阅读 `_l0_kernel_map` 与 `test_kernel_map_status_gating` 即可得出全部结论。

#### 4.3.5 小练习与答案

**练习 1**：为什么「kernel_map 必填」选在 L0（schema）而不是 L4（bench）层面执行？

**参考答案**：因为 kernel_map 缺失是**结构性**问题（规约没登记 dispatch 表），与性能基准无关。L0 是纯 YAML 静态检查、不导入代码，最适合拦截这种「契约字段缺失」。把它放在 L0 还有一个好处：spec-only 也跑 L0，但 `_l0_kernel_map` 内部用 `elif status == "implemented"` 精确门控，所以 spec-only 缺 kernel_map 不报——既保护了 spec-only 的灵活性，又卡死了 implemented 的蒙混。

**练习 2**：一个实现 PR 翻转 op 时，发现自己写的 dispatch 与原 spec-only 条目里登记的 kernel_map 不一致。它能在本 PR 里改 kernel_map 吗？如果不能，应该怎么办？

**参考答案**：可以。`source.kernel_map entries` 在 carve-out 清单上，实现 PR 允许改/补登记条目。这正是 carve-out 的设计意图——翻转时 dispatch 表通常需要落定，把它列为豁免项让翻转不必卡在信任边界。但注意：若不一致涉及的是 `signature`/`roofline` 等其他契约字段，则不能在本 PR 改，需拆 manifest-only PR；若实现确实无法服从原 spec，正确做法是**保留 status: spec-only**，在后续 PR 修代码，而不是改 manifest 迎合代码。

---

## 5. 综合实践

把本讲三个最小模块串成一个完整的「翻转 review」任务。

**场景**：开发者 Alice 提交了一个实现 PR，把 `BarFwdOp`（原 spec-only）翻转为 implemented。她在 PR 的 manifest diff 里改了这些字段：

```diff
   BarFwdOp:
-    status: spec-only
+    status: implemented
     source:
       kernel_map:
-        bar_fwd: OldBarKernel          # 旧登记，已不存在
+        bar_fwd: BarKernel             # 新 kernel 类名
+        bar_gemv: BarGemvKernel        # 新增的 GEMV 快路径 kernel
+      test: tests/ops/test_bar.py      # 新写的 test 文件路径
+      bench: benchmarks/ops/bench_bar.py
+    torch_compile_fullgraph: true      # 声称 fullgraph 可用
     roofline:
-      vars: { N: "x.shape[-1]" }       # 顺手删了，理由是「没用上」
```

**请你完成**：

1. **逐字段判定**：对照 [.claude/rules/manifest-trust-model.md:6-18](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/.claude/rules/manifest-trust-model.md#L6-L18) 的 carve-out 清单，把上面每一行改动标为「合法（留本 PR）」或「越界（拆 manifest-only PR）」。
2. **kernel_map 推理**：说明为什么 Alice 删/改 `kernel_map` 是被允许的，并解释如果她翻转后**完全不留** `kernel_map` 会发生什么（提示：L0 硬错误）。
3. **`torch_compile_fullgraph` 判定**：这个字段在什么条件下才能随翻转 PR 改？Alice 的 PR 是否满足？
4. **修方案**：给出 Alice 的 PR 应当如何拆分才能通过信任边界——哪些留下、哪些拆走。

**参考结论**：

1. 判定表：
   - `status` 翻转 → ✅ 合法。
   - `kernel_map` 改条目 + 新增条目 → ✅ 合法（`source.kernel_map entries` 在豁免清单）。
   - `test` / `bench` 路径 → ✅ 合法（发现性指针）。
   - `torch_compile_fullgraph: true` → ⚠️ 仅当**同时带上其注册的 compile-test 证据**才合法；Alice 若只声明而无对应 compile-test，则越界，需拆 PR 或补证据。
   - 删 `roofline.vars` → ❌ 越界，属 `roofline.*`，且直接违反「不许删 vars 压错」铁律，必须拆 manifest-only PR + 人审（或干脆驳回、保留 spec-only）。
2. `kernel_map` 改/增被允许，是 carve-out 专门把 `source.kernel_map entries` 列入豁免；若翻转后完全不留 kernel_map，`_l0_kernel_map`（[validate_manifest.py:571-575](https://github.com/tile-ai/TileOPs/blob/2392b7ed28edea82505b50b639463ee564576e38/scripts/validate_manifest.py#L571-L575)）会因 `status == "implemented"` 且缺失而发硬错误，CI 在 preflight 的 `--strict` 全级即失败。
3. `torch_compile_fullgraph` 必须随「其注册的 compile-test 证据」一同改（carve-out 措辞："only together with its registered compile-test evidence"）。Alice 仅置 `true` 而无对应 compile-test → 不满足，应补证据或拆走。
4. 拆分方案：本实现 PR 仅保留 `status` 翻转、`kernel_map` 改动、`test`/`bench` 路径；`torch_compile_fullgraph` 待补 compile-test 证据后再随同一 PR 加，或拆一个带证据的小 PR；删 `roofline.vars` 必须撤回——实现服从规约，若实现真用不上 vars，那是实现的问题，应保留 spec-only 并修代码，而不是删 vars 压错。

---

## 6. 本讲小结

- **翻转 = 一把钥匙**：把 `status: spec-only → implemented` 这一行 YAML 改动，同时打开 codegen 开关、收紧 validator 关卡、激活 carve-out 的窄化写权限。
- **carve-out 是窄化清单**：实现 PR 仅可改 `status`、`source.kernel_map` 条目、`source.test`/`source.bench` 路径、（仅 promotion 时）`workloads`、（仅随 compile-test 证据时）`torch_compile_fullgraph`；其余契约字段一律单独 manifest-only PR + 人审。
- **kernel_map 必填已升级为硬错误**：`_l0_kernel_map` 对 `implemented` 缺 `kernel_map` 直接写 errors（不再 advisory），所以翻转通常必须同时补 `source.kernel_map`。
- **必要性与合法性闭环**：L0 强制 implemented 必有 kernel_map，而 carve-out 又允许实现 PR 改 kernel_map 条目——两者配合让「翻转时补 kernel_map」既必要又合法。
- **铁律**：实现不服从 spec 就标 spec-only、修代码，**绝不改 manifest 迎合代码**；也不许删 `roofline.vars`/`shape_rules`/`params` 来让 validator 闭嘴。
- **carve-out 收窄禁止项，不放松信任边界**：豁免几个字段是为了让翻转可行，绝不是给实现者开后门改契约。

---

## 7. 下一步学习建议

- **向下一站**：本讲是信任模型篇的收尾。建议进入 u10（torch.compile 集成），看 `torch_compile_fullgraph` 这个 carve-out 字段在编译边界上如何被真正使用与验证。
- **横向巩固**：回头读 u9-l2（验证器五级检查），把本讲的「L0 硬错误」放回 L0–L4 全景，理解 `--check-op` 与 `--strict` 如何在 CI preflight 强制 carve-out 与 kernel_map 必填。
- **源码深读**：精读 `scripts/validate_manifest.py` 的 `_l0_kernel_map` 与 `check_l0`，再读 `tests/test_validate_manifest.py` 的 status-gating / malformed 两组测试，亲手跑一遍以巩固「硬错误」直觉。
- **动手挑战**：挑一个仍为 `spec-only` 的真实 op（用 `load_manifest()` 过滤），草拟一份「实现 PR + 翻转」的 manifest diff，自审是否每一行改动都落在 carve-out 清单内——这是检验你是否真正掌握本讲的最佳方式。
