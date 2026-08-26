# u5-l1 PassContext 与验证器体系

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清 `PassContext` 的「作用域式配置」模型：它是一个线程本地的上下文栈，`with` 语句进出栈，Pass 流水线从中读取一切运行配置。
2. 区分两个容易混淆的开关：`VerificationLevel`（流水线级的自动验证强度）与 `VerificationMode`（`VerificationInstrument` 的逐 Pass 验证时机）。
3. 理解 `IRProperty` / `PropertyVerifier` / `PropertyVerifierRegistry` 三者如何组成「流水线不变量的守护体系」——包括本版本新增的 `AccCompactValid` 验证器。
4. 读懂 `pass_properties.h` 中 `required / produced / invalidated` 三组属性的配合，特别是「同一属性被后续 Pass 失效再重产」这一模式为什么存在。

本讲是 Pass 流水线深入单元（u5）的第一讲，回答的问题是：**47 个 Pass 接力改写 IR 的过程中，编译器靠什么保证每一步都没有把 IR 改坏？**

## 2. 前置知识

- **Pass 与流水线**（u3-l5）：PyPTO 的 Default 策略把约 47 个 Pass 按顺序跑在 `Program` 上，每个 Pass 读入一份 IR、输出一份新 IR。接力越多，中间任何一步出错就越难定位——验证器体系就是为这个「错误传播」问题设计的。
- **IRProperty（IR 属性）**：一条对整份 IR 的全局断言，例如「所有变量使用都被其定义支配」（`UseAfterDef`）或「所有 Tile 内存空间已推断」（`TileMemoryInferred`）。属性要么成立要么不成立，验证器的工作就是判断它是否成立。
- **Diagnostic（诊断）**：一条结构化报告，携带严重级别、规则名、错误码、消息和 `span`（源码位置）。验证失败时抛出的 `VerificationError` 里就装着一批 Diagnostic。
- **span**：每个 IR 节点都带一个 `span_`，记录它来自哪个源文件的第几行第几列。DSL 解析器写入它，验证器报错时读出它——这是把「编译器内部的 IR 错误」翻译回「作者源码的某一行」的桥梁。
- **线程本地存储（thread-local）**：每个线程各有一份的全局变量。PyPTO 支持并发编译，PassContext 用 thread-local 栈保证两个线程的编译互不串扰。

一个直观比喻：Pass 流水线像一条工厂流水线，每个 Pass 是一道工序。`PassContext` 是车间的「作业环境面板」（谁监工、检验强度多少），`IRProperty` 是每道工序都要维持的「工艺标准」，`PropertyVerifier` 是逐项检查标准的「质检员」，`PropertyVerifierRegistry` 是质检员的「排班表」。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [include/pypto/ir/transforms/pass_context.h](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/include/pypto/ir/transforms/pass_context.h) | PassContext 与全部 Instrument 类的声明 |
| [src/ir/transforms/pass_context.cpp](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/transforms/pass_context.cpp) | 上述类的实现：线程本地栈、验证仪器的执行逻辑、诊断输出 |
| [include/pypto/ir/transforms/ir_property.h](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/include/pypto/ir/transforms/ir_property.h) | `IRProperty` 枚举、`IRPropertySet` 位集、`PassProperties` 结构、`VerificationLevel` |
| [include/pypto/ir/transforms/pass_properties.h](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/include/pypto/ir/transforms/pass_properties.h) | 全部内建 Pass 的属性声明（required/produced/invalidated） |
| [src/ir/transforms/passes.cpp](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/transforms/passes.cpp) | `PassPipeline::Run`——流水线主循环里自动验证的记账逻辑 |
| [src/ir/verifier/property_verifier_registry.cpp](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/verifier/property_verifier_registry.cpp) | 验证器注册表：IRProperty → 验证器工厂的映射与报告生成 |
| [src/ir/verifier/verify_acc_compact.cpp](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/verifier/verify_acc_compact.cpp) | 新增的 `AccCompactValid` 验证器实现（L0C compact 契约） |
| [python/pypto/ir/instruments.py](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/python/pypto/ir/instruments.py) | Python 侧的自定义仪器：打印→解析往返检查仪器 |
| [python/pypto/ir/compile.py](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/python/pypto/ir/compile.py) | `compile()` 如何继承外层 PassContext 的配置与仪器 |
| [docs/en/dev/passes/99-verifier.md](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/docs/en/dev/passes/99-verifier.md) | 验证器体系的权威文档：全部内建规则与属性集合表 |

## 4. 核心概念与源码讲解

### 4.1 PassContext：Pass 的作用域式运行环境

#### 4.1.1 概念说明

`PassContext` 是 Pass 的「运行环境」。项目规则（`.claude/rules/pass-context-config.md`）明确要求：**所有 Pass 相关配置必须放进 PassContext，禁止全局可变状态**。它承载的东西包括：

- **仪器列表**（instruments）：每个 Pass 前后要执行的回调集合；
- **验证等级**（`verification_level`）：流水线自动验证的强度；
- **诊断配置**（`diagnostic_phase` / `disabled_diagnostics`）：警告与性能提示的开关；
- **内存规划器**（`memory_planner`）：PyPTO / PtoAS / DsaRP 三选一；
- **L0C 双缓冲开关**与**目标运行时 ABI**（`runtime`）。

它的「作用域式」体现在：C++ 侧用 `EnterContext()/ExitContext()` 压栈出栈，Python 侧直接做成上下文管理器。嵌套的 `with` 内层覆盖外层，退出时自动恢复——同一个进程里先后（或并发于不同线程的）两次编译互不影响。

#### 4.1.2 核心流程

```text
with PassContext([...]):          # Python
   └─ __enter__ → EnterContext()  # 绑定层转发
        └─ previous_ = current_; current_ = this   # 压栈（线程本地）
     编译 / 运行 Pass
        └─ PassPipeline::Run 读取 PassContext::Current() 的配置
        └─ 每个 Pass 执行前后：ctx->RunBeforePass / RunAfterPass
           └─ 逐个仪器调用其回调
   └─ __exit__ → ExitContext()    # current_ = previous_（出栈）
```

没有激活任何 PassContext 时，`PassContext::Current()` 返回空指针，流水线退回环境变量默认值（如 `PYPTO_VERIFY_LEVEL`）。

#### 4.1.3 源码精读

线程本地栈本体只有一个指针，栈由 `previous_` 链串起来：

- [src/ir/transforms/pass_context.cpp:L40-L40](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/transforms/pass_context.cpp#L40-L40) 定义 `thread_local PassContext* PassContext::current_ = nullptr;`——每个线程一份的「当前上下文」。
- [src/ir/transforms/pass_context.cpp:L334-L344](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/transforms/pass_context.cpp#L334-L344) `EnterContext()` 把旧的 `current_` 存进 `previous_` 再把自己设为 `current_`；`ExitContext()` 用 `INTERNAL_CHECK` 防御不成对的出栈，然后恢复 `previous_`。嵌套 `with` 由此天然支持。

类的字段一览（配置都在这里，没有别的全局变量）：

- [include/pypto/ir/transforms/pass_context.h:L306-L312](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/include/pypto/ir/transforms/pass_context.h#L306-L312) 构造函数签名：七个配置参数依次是仪器列表、验证等级、诊断相位、禁用诊断集合、内存规划器、L0C 双缓冲、运行时 ABI，全部带默认值。
- [include/pypto/ir/transforms/pass_context.h:L404-L413](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/include/pypto/ir/transforms/pass_context.h#L404-L413) 成员列表与 `static thread_local PassContext* current_`。

静态查询入口与后端分发：

- [src/ir/transforms/pass_context.cpp:L367-L367](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/transforms/pass_context.cpp#L367-L367) `PassContext::Current()` 返回栈顶；为空表示没有激活的上下文。
- [src/ir/transforms/pass_context.cpp:L369-L373](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/transforms/pass_context.cpp#L369-L373) `GetBackendHandler()`——Pass 查询按后端变化的行为（hazard 开关、布局规则等）都走这里，而不是自己在 Pass 里写 `if (backend == ...)` 分支。

Python 绑定把 C++ 的进出栈包装成 `with` 协议：

- [python/bindings/modules/passes.cpp:L310-L347](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/python/bindings/modules/passes.cpp#L310-L347) `PassContext` 绑定；其中 `__enter__` 调 `EnterContext`、`__exit__` 调 `ExitContext`（约 L329-L333），因此 Python 里直接 `with passes.PassContext([...]):` 即可。

`compile()` 与外层 PassContext 的关系值得单独记住：

- [python/pypto/ir/compile.py:L134-L153](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/python/pypto/ir/compile.py#L134-L153) 若外层已有激活的 PassContext，`compile()` 会**继承**它的仪器与全部配置（`instruments = outer_instruments + extra_instruments`），未显式指定的参数回落到外层值。
- [python/pypto/ir/compile.py:L71-L103](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/python/pypto/ir/compile.py#L71-L103) `_validate_pass_context_conflicts`：在 PassContext 已经激活时再给 `compile()` 传 `verification_level` 等参数会直接 `RuntimeError`——配置只能有一个主人。这是「作用域式配置」的纪律体现。

#### 4.1.4 代码实践

**实践目标**：验证 PassContext 的栈式行为——嵌套两层上下文，观察配置覆盖与恢复。

**操作步骤**（示例代码，可在已构建的环境里以 `python` 交互执行）：

```python
from pypto.pypto_core import passes

ctx_outer = passes.PassContext([], passes.VerificationLevel.NONE)
ctx_inner = passes.PassContext([], passes.VerificationLevel.ROUNDTRIP)

with ctx_outer:
    print("outer:", passes.PassContext.current().get_verification_level())
    with ctx_inner:
        print("inner:", passes.PassContext.current().get_verification_level())
    print("back  :", passes.PassContext.current().get_verification_level())
print("exit  :", passes.PassContext.current())   # 预期 None
```

**需要观察的现象**：三行打印分别输出 NONE / ROUNDTRIP / NONE；最后一行 `current()` 为 `None`。

**预期结果**：内层 `with` 覆盖外层配置，退出内层后精确恢复外层，全部退出后栈空。运行结果待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `current_` 必须是 `thread_local` 而不是普通全局变量？

**答案**：PyPTO 支持并发编译（例如多进程/多线程各编各的 kernel）。若共享一个全局指针，线程 A 的 `ExitContext` 会把线程 B 正在使用的上下文弹掉，配置互相污染。thread-local 让每个线程拥有独立栈，`EnterContext/ExitContext` 只影响本线程。

**练习 2**：在 `with passes.PassContext([], passes.VerificationLevel.BASIC):` 内部调用 `ir.compile(program, verification_level=passes.VerificationLevel.NONE)` 会发生什么？为什么这样设计？

**答案**：抛出 `RuntimeError`（[python/pypto/ir/compile.py:L71-L103](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/python/pypto/ir/compile.py#L71-L103) 的冲突检查）。设计原因：外层上下文已经拥有这份配置，若允许内层静默覆盖，退出作用域后行为不可预期；正确做法是在外层 PassContext 构造时就设定好。

**练习 3**：Pass 想知道「当前目标后端是否需要 GM pipe buffer」，应该读哪个接口？

**答案**：`PassContext::Current()->GetBackendHandler()` 返回的 `BackendHandler` 虚接口（[src/ir/transforms/pass_context.cpp:L369-L373](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/transforms/pass_context.cpp#L369-L373)），而不是在 Pass 里硬编码 `if (GetBackendType() == ...)`。

### 4.2 Instrument 家族与两级验证开关

#### 4.2.1 概念说明

**Instrument（仪器）**是挂在 Pass 执行前后的回调对象。基类 `PassInstrument` 定义三个钩子：`RunBeforePass`、`RunAfterPass`、`RunAfterPipeline`。内建四个实现：

| 仪器 | 职责 |
| --- | --- |
| `VerificationInstrument` | 按 `VerificationMode` 在每个 Pass 前后验证 IR 属性 |
| `CallbackInstrument` | 包两个用户回调，做轻量插桩（打印 IR、日志） |
| `ReportInstrument` | 只携带输出目录，告诉别的仪器报告文件写哪里 |
| `DiagnosticInstrument` | 在 Pass 边界跑注册的警告/性能提示检查 |

**两个验证开关必须分清**：

- `VerificationMode`（仪器级）：`None / Before / After / BeforeAndAfter`——控制 `VerificationInstrument` 这个**仪器**何时触发。它是「逐 Pass、含结构属性」的重型检查。
- `VerificationLevel`（流水线级）：`None / Basic / Roundtrip`——控制 `PassPipeline::Run` 自带的**自动验证**强度。`Basic`（默认）只对轻量属性集合「每个属性恰验证一次」；`Roundtrip` 在此之上叠加打印→解析的结构相等检查（Python 侧对应 `make_roundtrip_instrument`）。

#### 4.2.2 核心流程

`VerificationInstrument` 的逻辑（伪代码）：

```text
RunBeforePass(pass, program):
    若 mode ∈ {Before, BeforeAndAfter}:
        验证  pass.required ∪ GetStructuralProperties()   # 不满足 → 抛 VerificationError

RunAfterPass(pass, program):
    若 mode ∈ {After, BeforeAndAfter}:
        验证  pass.produced ∪ GetStructuralProperties()   # 不满足 → 抛 VerificationError
```

`PassPipeline::Run` 的自动验证记账（`VerificationLevel != None` 时）：

```text
verified = ∅
流水线入口: 验证 GetStructuralProperties() ∩ GetVerifiedProperties()，并入 verified
对每个 pass p:
    current = p(current)
    verified -= p.invalidated ∩ GetVerifiedProperties()   # 被失效的属性不再可信
    to_verify = p.produced ∩ GetVerifiedProperties() − verified
    若 to_verify 非空: 验证之，并入 verified               # 每个属性只验证一次
```

注意关键点：**流水线只验证 produced（产出）的属性**，不验证 required（那是 `VerificationInstrument` 的职责）；属性被 `invalidated` 后从 `verified` 集合移除，若后续 Pass 重新产出它，会再次获得一次验证机会——这正是 4.4 节「失效再重产」模式的运行时基础。

#### 4.2.3 源码精读

仪器基类与三个钩子：

- [include/pypto/ir/transforms/pass_context.h:L71-L101](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/include/pypto/ir/transforms/pass_context.h#L71-L101) `PassInstrument` 纯虚基类：`RunBeforePass`（L80）、`RunAfterPass`（L87）、`RunAfterPipeline`（L95，默认空操作，供流水线末尾的分析使用）。
- [include/pypto/ir/transforms/pass_context.h:L58-L63](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/include/pypto/ir/transforms/pass_context.h#L58-L63) `VerificationMode` 四值枚举。
- [include/pypto/ir/transforms/ir_property.h:L235-L239](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/include/pypto/ir/transforms/ir_property.h#L235-L239) `VerificationLevel` 三值枚举，注释明确 Basic 的语义是「每个轻量属性恰验证一次」。

`VerificationInstrument` 的实现：

- [src/ir/transforms/pass_context.cpp:L75-L89](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/transforms/pass_context.cpp#L75-L89) 前后钩子分别验证 `pass.GetRequiredProperties().Union(GetStructuralProperties())` 与 `pass.GetProducedProperties().Union(GetStructuralProperties())`——**结构属性永远跟着一起查**，保证任何 Pass 都不能破坏基础不变量。
- [src/ir/transforms/pass_context.cpp:L56-L71](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/transforms/pass_context.cpp#L56-L71) `VerifyOrThrowWithContext`：收集诊断，只要有 Error 级别就生成报告并抛 `VerificationError`。注释特别说明它必须与注册表抛出**同一种异常类型**——否则用户看到的异常种类取决于是否恰好装了仪器（即 `PYPTO_VERIFY_LEVEL`），那是测试基建泄漏进语义。

流水线侧的记账：

- [src/ir/transforms/passes.cpp:L215-L218](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/transforms/passes.cpp#L215-L218) 验证等级优先读激活的 PassContext，否则回落环境变量默认值。
- [src/ir/transforms/passes.cpp:L250-L256](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/transforms/passes.cpp#L250-L256) 流水线入口先验证结构属性与轻量集合的**交集**（所以例如 `ArrayNotEscaped` 只在逐 Pass 仪器里查，不在流水线入口查）。
- [src/ir/transforms/passes.cpp:L259-L273](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/transforms/passes.cpp#L259-L273) 逐 Pass 循环里的核心三步：先从 `verified` 减去 `invalidated`，再算 `produced ∩ 轻量集合 − verified`，非空则验证并并入。同一个属性第二次被产出时会因已在 `verified` 中而跳过——**除非它先被失效过**。

Python 侧的自定义仪器示例：

- [python/pypto/ir/instruments.py:L18-L102](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/python/pypto/ir/instruments.py#L18-L102) `make_roundtrip_instrument()`：每个 Pass 之后把 IR `python_print` 成文本、`parse` 回 Program、`assert_structural_equal` 比对——任一环节失败即指出打印器/解析器在该 Pass 产出的 IR 上失真。它最终包成名为 `"RoundtripInstrument"` 的 `CallbackInstrument`（L99-L102），是「用仪器守护流水线」的最佳范例。

绑定层把两个枚举与仪器类都暴露给 Python：

- [python/bindings/modules/passes.cpp:L164-L175](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/python/bindings/modules/passes.cpp#L164-L175) `VerificationMode` 与 `VerificationLevel` 枚举绑定（`NONE/BEFORE/AFTER/BEFORE_AND_AFTER` 与 `NONE/BASIC/ROUNDTRIP`）。
- [python/bindings/modules/passes.cpp:L281-L284](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/python/bindings/modules/passes.cpp#L281-L284) `VerificationInstrument(mode)` 构造绑定。

#### 4.2.4 代码实践

**实践目标**：亲手体验 `VerificationMode` 的两个方向——合法 IR 通过、非法 IR 在 Pass 执行前就被拦下。

**操作步骤**：仓库已有现成测试覆盖这两个方向，直接阅读并运行（构建环境就绪后）：

```bash
source .claude/skills/testing/load-env.sh
python -m pytest tests/ut/ir/transforms/test_verifier.py -v -k verification_instrument
```

两个测试的模式值得对照（示例代码引用自测试文件）：

- [tests/ut/ir/transforms/test_verifier.py:L356-L367](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/tests/ut/ir/transforms/test_verifier.py#L356-L367) 非法方向：构造违反 `NoRedundantBlocks` 的嵌套 SeqStmts 程序，在 `pytest.raises(pypto.Error, match="Pre-verification failed")` 里用 `BEFORE_AND_AFTER` 模式跑 `normalize_stmt_structure()`——**Pass 还没执行**错误就抛出来了。
- [tests/ut/ir/transforms/test_verifier.py:L369-L390](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/tests/ut/ir/transforms/test_verifier.py#L369-L390) 合法方向：干净小程序同样模式下跑 `convert_to_ssa()`，不抛错，证明 after 钩子确实执行且通过。

**需要观察的现象**：两个测试一红一绿的语义（前者验证「抛错」，后者验证「不抛错」）；错误消息以 `Pre-verification failed before pass '...'` 开头。

**预期结果**：`-k verification_instrument` 选中两个测试全部通过。运行结果待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：`VerificationLevel.Basic` 与 `VerificationMode.AFTER` 都会「验证」，它们的差别是什么？

**答案**：`Basic` 是流水线内建的自动验证，只查 `GetVerifiedProperties()` 里的轻量属性、每个属性整个流水线只查一次（除非被失效后重产）；`AFTER` 是装进 PassContext 的 `VerificationInstrument` 的行为，**每个 Pass 之后**都查该 Pass 的 `produced ∪ 全部结构属性`，粒度细、开销大，用于排查「哪一步改坏了 IR」。

**练习 2**：为什么 `VerificationInstrument` 的 after 钩子要并上 `GetStructuralProperties()`，而 `PassPipeline` 不这么做？

**答案**：结构属性（类型检查、use-after-def 等）是任何时刻都必须成立的根本不变量，仪器按 Pass 粒度全量守护，防止某个未声明任何属性的 Pass 偷偷破坏它们（[src/ir/transforms/pass_context.cpp:L87-L88](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/transforms/pass_context.cpp#L87-L88)）；流水线的自动验证以「轻量、每个属性一次」为目标，只在入口查结构属性与轻量集合的交集（[src/ir/transforms/passes.cpp:L250-L256](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/transforms/passes.cpp#L250-L256)），常态编译不为此拖慢 47 个 Pass。

**练习 3**：想让每次编译都检查「打印器能否忠实还原 IR」，有哪两种手段？

**答案**：把 `VerificationLevel` 设为 `ROUNDTRIP`（枚举注释即「Basic + 打印→解析检查」，[include/pypto/ir/transforms/ir_property.h:L235-L239](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/include/pypto/ir/transforms/ir_property.h#L235-L239)）；或手动把 `make_roundtrip_instrument()` 返回的仪器放进 `PassContext` 的仪器列表（[python/pypto/ir/instruments.py:L18-L102](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/python/pypto/ir/instruments.py#L18-L102)）。

### 4.3 PropertyVerifierRegistry 与 Diagnostic 报告

#### 4.3.1 概念说明

`PropertyVerifierRegistry` 是进程级单例，维护 `IRProperty → 验证器工厂` 的映射。每条验证规则是一个 `PropertyVerifier` 子类，实现两个方法：`GetName()`（规则名，进报告）和 `Verify(program, diagnostics)`（遍历 IR、把发现的问题追加进诊断列表）。

按验证时机，属性分两类（文档 [docs/en/dev/passes/99-verifier.md:L25-L32](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/docs/en/dev/passes/99-verifier.md#L25-L32)）：

| 类别 | 例子 | 验证时机 |
| --- | --- | --- |
| 结构属性 | `TypeChecked`、`UseAfterDef`、`ArrayNotEscaped`、`AtomicAddDtypeValid` 等 | 恒应成立；由 `VerificationInstrument` 逐 Pass 查；不出现在任何 Pass 的 PassProperties 里 |
| 流水线属性 | `SSAForm`、`TileMemoryInferred`、`AccCompactValid` 等 | 由 Pass 产出/失效，按 Pass 声明的契约在产出点验证 |

属性集合的三个入口（[include/pypto/ir/transforms/ir_property.h:L244-L273](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/include/pypto/ir/transforms/ir_property.h#L244-L273)）：`GetVerifiedProperties()`（流水线自动验证的轻量集）、`GetStructuralProperties()`（结构不变量集）、`GetDefaultVerifyProperties()`（`run_verifier()` 的默认集）。

#### 4.3.2 核心流程

```text
PropertyVerifierRegistry（单例）
    ├─ Register(IRProperty, factory)     # 静态初始化期注册全部内建规则
    ├─ VerifyProperties(props, program)  # 逐属性建验证器 → Verify → 汇总 diagnostics
    ├─ VerifyPropertiesOrThrow(...)      # 有 Error 即 GenerateReport + 抛 VerificationError
    └─ GenerateReport(diagnostics)       # 格式化成带位置信息的文本报告
```

验证失败时的报告样式（由 `GenerateReport` 生成）：

```text
IR Verification Report
======================
Total diagnostics: N (n errors, m warnings)

[1] ERROR - UseAfterDefCheck
  Message: <具体问题描述>
  Location: <文件名>:<行>:<列>          ← 来自 Diagnostic.span
  Error Code: 401
```

`Location` 一行就是把 IR 内部错误定位回作者源码的关键——span 由 DSL 解析器在建 IR 节点时写入，报错时原样读出。

#### 4.3.3 源码精读

`AccCompactValid` 的注册（本版本新增，与既有规则并列）：

- [src/ir/verifier/property_verifier_registry.cpp:L72-L72](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/verifier/property_verifier_registry.cpp#L72-L72) `Register(IRProperty::AccCompactValid, CreateAccCompactValidPropertyVerifier);`——注册表按属性值找到工厂，工厂返回验证器实例。整个构造函数（[L40-L106](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/verifier/property_verifier_registry.cpp#L40-L106)）是全部内建规则的注册清单，新增一条验证器就是在这里加一行。
- [src/ir/verifier/property_verifier_registry.cpp:L151-L194](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/verifier/property_verifier_registry.cpp#L151-L194) `GenerateReport`：统计错误/警告数，逐条打印严重级别、规则名、消息、位置与错误码；其中 [L177-L179](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/verifier/property_verifier_registry.cpp#L177-L179) 拼出 `Location: filename:line:column`。

新验证器实现的结构（阅读任何验证器的模板）：

- [src/ir/verifier/verify_acc_compact.cpp:L89-L105](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/verifier/verify_acc_compact.cpp#L89-L105) `AccCompactVisitor`：一个 `IRVisitor` 子类，`CheckFunction` 先查参数再遍历函数体；`VisitVarLike_` 覆写用的是 `VarLike` 统一钩子（同时覆盖 `Var` 与 `IterArg`，见 u4-l2 讲过的 kind-trait 规则）。
- [src/ir/verifier/verify_acc_compact.cpp:L249-L250](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/verifier/verify_acc_compact.cpp#L249-L250) 工厂函数 `CreateAccCompactValidPropertyVerifier()`，即注册表持有的那个工厂。

文档中的权威对照表：

- [docs/en/dev/passes/99-verifier.md:L62-L89](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/docs/en/dev/passes/99-verifier.md#L62-L89) 内建规则总表：每行是「规则名 | IRProperty | 检查内容 + 由哪个 Pass 产出/失效 + 修复建议」。`AccCompactValid` 位于 [L87](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/docs/en/dev/passes/99-verifier.md#L87)。
- [docs/en/dev/passes/99-verifier.md:L178-L183](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/docs/en/dev/passes/99-verifier.md#L178-L183) 三个属性集合的成员表——`GetVerifiedProperties()` 一行（L182）已包含 `AccCompactValid`。

#### 4.3.4 代码实践

**实践目标**：构造一个「引用未定义变量」的非法 IR，观察验证器报告，特别是 `Location` 行里 span 的作用。

**操作步骤**（示例代码）：

```python
import pypto
from pypto import ir, passes

# 1. 用带真实位置信息的 span 构造非法 IR：
#    返回一个从未定义、也不是参数的变量 → 违反 UseAfterDef
span = ir.Span("my_kernel.py", 12, 5)
t = ir.ScalarType(pypto.DataType.INT64)
a = ir.Var("a", t, span)            # 参数（有定义）
ghost = ir.Var("ghost", t, span)    # 从未定义
body = ir.SeqStmts([ir.ReturnStmt([ghost], span)], span)
func = ir.Function("f", [a], [t], body, span)
prog = ir.Program([func], "demo", span)

# 2. 用结构属性集合验证并生成报告
props = passes.get_structural_properties()
diags = passes.PropertyVerifierRegistry.verify(props, prog)
print(passes.PropertyVerifierRegistry.generate_report(diags))
for d in diags:
    print(d.rule_name, d.error_code, d.span.filename, d.span.begin_line)
```

**需要观察的现象**：报告中出现 `ERROR - UseAfterDefCheck`、`Error Code: 401`（错误码表见 [docs/en/dev/passes/99-verifier.md:L133-L149](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/docs/en/dev/passes/99-verifier.md#L133-L149)），且 `Location` 行打印 `my_kernel.py:12:5`。

**预期结果**：诊断非空、全部为 Error 级；span 字段原样回放你构造的位置。真实 DSL 场景中这个位置由解析器自动填写，指向用户源码行。运行结果待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：`Diagnostic` 里的 `span` 为空（`Span.unknown()`）时报告会怎样？这说明 span 的价值是什么？

**答案**：`Location` 一行退化为无意义的占位值。span 的价值是把 IR 层的抽象违规映射回作者源码的文件/行/列——47 个 Pass 之后 IR 与源码早已面目全非，没有 span 的报错只能靠人脑逆向追踪调用链。

**练习 2**：想加一条自己的验证规则，需要动哪几处？

**答案**：按 [docs/en/dev/passes/99-verifier.md:L278-L292](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/docs/en/dev/passes/99-verifier.md#L278-L292) 的步骤：继承 `PropertyVerifier` 实现 `GetName()/Verify()`；写工厂函数；在 `PropertyVerifierRegistry` 注册（参照 [src/ir/verifier/property_verifier_registry.cpp:L72](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/verifier/property_verifier_registry.cpp#L72) 的注册行）；可选地补 Python 绑定与类型桩。

**练习 3**：`run_verifier()` 默认验证哪些属性？它与 `GetStructuralProperties()` 有何不同？

**答案**：默认集是 `GetDefaultVerifyProperties()`（含 `SSAForm`、`TypeChecked`、`NoNestedCalls` 等 10 项，见 [include/pypto/ir/transforms/ir_property.h:L265-L273](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/include/pypto/ir/transforms/ir_property.h#L265-L273)），供手工构造流水线时即插即用；结构集合是「恒真不变量」，两者是交集关系而非包含关系。

### 4.4 Pass 属性契约：produced / invalidated 的配合（以 AccCompactValid 为例）

#### 4.4.1 概念说明

每个 Pass 用一个 `PassProperties` 声明自己与属性系统的关系：

- **required**：运行前必须成立的前置条件；
- **produced**：运行后新保证成立的属性；
- **invalidated**：本 Pass 会破坏的属性。

大多数属性的声明是朴素的：`InlineFunctions` 产出 `InlineFunctionsEliminated`，`ConvertToSSA` 产出 `SSAForm` 并失效 `NormalizedStmtStructure`。但有一类声明初看很怪——**同一个属性同时出现在 produced 和 invalidated 里**。本版本的新验证器 `AccCompactValid` 恰好是最佳活例。

先理解 `AccCompactValid` 查什么。硬件背景：矩阵乘累加器的 `mad` 指令按 lhs 的**有效行数**（valid rows）以 N-分形步长摆放 L0C 数据：

\[ \text{pitch} = \lceil \text{validRow} / 16 \rceil \times 16 \]

而读取方默认按物理行数 `Rows` 推导步长。只有当 Tile 带上 compact（紧凑）标记时，读取方才会改用有效行数重算步长。若累加器有效行收窄了却没标 compact，读取步长与写入步长不一致，L0C 里第一个分形之上的数据全部错位——历史上 #2470（store 路径）与 #2510（Cube→Vector 推送路径）两个 bug 都以「设备上数值错误、无任何诊断」的形态流出。`AccCompactValid` 验证器就是把这个契约变成编译期检查。

#### 4.4.2 核心流程

`AccCompactValid` 在流水线里的生命周期（关键在「窗口」概念）：

```text
Pass 17  InferTileMemorySpace
         └─ produced: AccCompactValid     ← 内存空间在此解析，契约首次可判定 → 验证一次
Pass 21  ExpandMixedKernel
         ├─ invalidated: AccCompactValid  ← 跨核边界的 tile.move 被重建为 tpush/tpop，
         │                                  新造的消费者类型必须重新接受检查
         └─ produced:   AccCompactValid   ← 重新产出 → 流水线再验证一次
```

配合 4.2.2 的记账规则：Pass 21 先把属性从 `verified` 集合移除（因 invalidated），随后又产出它，`to_verify` 非空 → 在 Pass 21 产出的**新 IR** 上再跑一遍验证器。若只 invalidated 不重产，该属性之后不再被验证；若只重产不先失效，流水线看到它已在 `verified` 中会**跳过**第二次验证——两行缺一不可。

同样的「验证窗口」模式在 `AivSplitValid` 上早已存在：`OutlineIncoreScopes` 开窗（产出）→ `ConvertTensorToTileOps`、`InferTileMemorySpace` 失效再重产 → `LowerAutoVectorSplit` 关窗（纯失效，因为区域节点被删除，此后无法再验证）。

#### 4.4.3 源码精读

契约结构体：

- [include/pypto/ir/transforms/ir_property.h:L218-L227](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/include/pypto/ir/transforms/ir_property.h#L218-L227) `struct PassProperties { required; produced; invalidated; }`——三组 `IRPropertySet`（位集实现见 [L132-L216](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/include/pypto/ir/transforms/ir_property.h#L132-L216)，底层是 `uint64_t`，所以枚举上限 64 个，L118-L120 有 `static_assert` 把关）。
- [include/pypto/ir/transforms/ir_property.h:L107-L114](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/include/pypto/ir/transforms/ir_property.h#L107-L114) `AccCompactValid` 枚举项及注释：契约两半——累加算子必须写入 compact 缓冲、分形空间之外的 Tile 不得携带 compact。

两处声明的精确对照：

- [include/pypto/ir/transforms/pass_properties.h:L212-L217](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/include/pypto/ir/transforms/pass_properties.h#L212-L217) `kInferTileMemorySpaceProperties`：`produced` 里含 `AccCompactValid`（L216）。它排在 `AccToGmStoreValid` 之后，注释说明内存空间在此解析，同一批 Tile 空间相关的验证器（含本条）都在这里获得第一个可判定点。
- [include/pypto/ir/transforms/pass_properties.h:L278-L286](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/include/pypto/ir/transforms/pass_properties.h#L278-L286) `kExpandMixedKernelProperties`：L282 把 `AccCompactValid` 放进 `produced`，L283-L286 的注释解释原因——Cube→Vector 边界的 `tile.move` 在此被重建为 tpush/tpop 对、消费者类型是新建的，**契约必须在新 IR 上重查而不是信任 Pass 17 的结论**，故同时声明 `invalidated`。

先例对照（同一模式）：

- [include/pypto/ir/transforms/pass_properties.h:L152-L156](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/include/pypto/ir/transforms/pass_properties.h#L152-L156) `kConvertTensorToTileOpsProperties`：`AivSplitValid` 同时出现在 `produced` 与 `invalidated`——注释讲明它把边界算子改写成 tile 形态并挂上内存信息，正是边界内存契约检查变得可观察的时机。
- [include/pypto/ir/transforms/pass_properties.h:L264-L270](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/include/pypto/ir/transforms/pass_properties.h#L264-L270) `kLowerAutoVectorSplitProperties`：只 `required + invalidated`，不重产——区域节点被删除，验证窗口就此关闭。

验证器内部的三条检查（对应三档错误码 1/2/3）：

- [src/ir/verifier/verify_acc_compact.cpp:L46-L77](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/verifier/verify_acc_compact.cpp#L46-L77) 契约注释，三条规则：(a) 步长可证不同的 `tile.matmul_acc` / `tile.matmul_mx_acc` 必须累加进 compact 缓冲；(b) compact 累加器经 `tile.set_validshape` 改写有效行时不得跨分形边界（否则字节按 32 打包、读者按 16 推导）；(c) 分形空间（Left/Right/Acc）之外的 Tile 不得携带 compact。文档版全文见 [docs/en/dev/passes/99-verifier.md:L87](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/docs/en/dev/passes/99-verifier.md#L87)。
- 报错点分别位于 [src/ir/verifier/verify_acc_compact.cpp:L150](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/verifier/verify_acc_compact.cpp#L150)、[L181](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/verifier/verify_acc_compact.cpp#L181)、[L221](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/verifier/verify_acc_compact.cpp#L221)，错误码 1/2/3。

#### 4.4.4 代码实践

**实践目标**：仅凭文档与属性声明，独立回答「`AccCompactValid` 由哪些 Pass 产出与失效」。

**操作步骤**：

1. 打开 [docs/en/dev/passes/99-verifier.md:L87](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/docs/en/dev/passes/99-verifier.md#L87) 的 `AccCompactValid` 行，找到「Produced by ...」句子。
2. 在 `include/pypto/ir/transforms/pass_properties.h` 中 grep `AccCompactValid`，核对两处声明与文档说法是否一致。
3. 对照 `AivSplitValid`（同文件 L127-L129、L152-L156、L212-L217、L264-L270）体会「开窗—重开窗—关窗」的完整生命周期。

**需要观察的现象**：文档说 produced by `InferTileMemorySpace`、re-produced by `ExpandMixedKernel`；源码里 `kInferTileMemorySpaceProperties.produced`（L216）与 `kExpandMixedKernelProperties` 的 produced（L282）+ invalidated（L286）精确对应。

**预期结果**：产出方两个（InferTileMemorySpace、ExpandMixedKernel），失效方一个（ExpandMixedKernel 自身，失效后立即重产以强制在新 IR 上复验）。此为静态阅读，结论可直接从源码核实。

#### 4.4.5 小练习与答案

**练习 1**：`kExpandMixedKernelProperties` 若删掉 `.invalidated = {AccCompactValid}` 只保留 produced，行为会怎样变化？

**答案**：Pass 21 执行前 `AccCompactValid` 已在 `verified` 集合中（Pass 17 验证过且无人失效），流水线的 `to_verify = produced ∩ 轻量集 − verified` 为空（[src/ir/transforms/passes.cpp:L268-L272](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/transforms/passes.cpp#L268-L272)），重建 tpush/tpop 后的新 IR 将**不再被检查**——恰恰放过 #2510 那类在边界重建时引入的错位。

**练习 2**：为什么 `AccCompactValid` 不能像 `AtomicAddDtypeValid` 那样做成结构属性、在流水线入口就查？

**答案**：结构属性必须在用户手写的 IR 上即可判定（`AtomicAddDtypeValid` 只看 atomic 标志与目标 dtype，见 [src/ir/verifier/property_verifier_registry.cpp:L73-L77](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/src/ir/verifier/property_verifier_registry.cpp#L73-L77) 的注册注释）；而 compact 契约依赖**内存空间归属**（哪些 Tile 在 Acc/Left/Right 分形空间）——这要到 `InferTileMemorySpace` 之后才解析，入口处根本无法判定，所以必须做成流水线属性、在该 Pass 产出点首验。

**练习 3**：一个 Pass 声明了 `required = {SSAForm}` 却没有声明任何 produced，这合法吗？意味着什么？

**答案**：合法。`PassProperties` 三组都可为空（如 [include/pypto/ir/transforms/pass_properties.h:L106-L106](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/include/pypto/ir/transforms/pass_properties.h#L106-L106) 的 `kSimplifyProperties` 就是全空）。`ir_property.h` 的枚举注释明确说明：不是所有 Pass 都产出属性——性能优化类 Pass（如 MemoryReuse）只消费既有保证、不建立新的可验证不变量，这是设计使然。

## 5. 综合实践

把本讲四个模块串成一个完整的排查演练。场景：你怀疑某个 Pass 把 IR 改坏了，要用验证体系定位是哪一个。

**任务**：

1. **准备**（构建环境就绪后）：

   ```bash
   source .claude/skills/testing/load-env.sh
   ```

2. **重型逐 Pass 验证**：用 `VerificationInstrument(AFTER)` 包住一次编译。外层 PassContext 的仪器会被 `compile()` 继承（[python/pypto/ir/compile.py:L134-L153](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/python/pypto/ir/compile.py#L134-L153)），所以写法是（示例代码）：

   ```python
   from pypto.pypto_core import passes
   from pypto import ir

   with passes.PassContext([passes.VerificationInstrument(passes.VerificationMode.AFTER)]):
       compiled = ir.compile(program)   # 不要再传 verification_level，会触发冲突检查
   ```

   若某个 Pass 之后 IR 非法，异常消息形如 `Post-verification failed after pass '<名字>'`，随附完整报告——第一个出错 Pass 就此锁定。

3. **对照轻量模式**：换成 `passes.PassContext([], passes.VerificationLevel.BASIC)` 再编译，观察默认等级下同样的错误是否仍被拦截（提示：取决于该属性是否在 `GetVerifiedProperties()` 且是否被某 Pass 产出）。

4. **定位源码行**：把步骤 2 报告里的 `Location` 信息与 `span` 字段记下来，对照 DSL 源码找到对应行；再对照 [docs/en/dev/passes/99-verifier.md:L62-L89](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/docs/en/dev/passes/99-verifier.md#L62-L89) 规则表的「Fix」列写出修复思路。

5. **收尾**：在属性声明表里找到步骤 2 报错的属性，画出它「产出—失效—重产」的时间线（参照 4.4.4 的做法）。

**验收标准**：能写出一份包含「出错 Pass 名 → 违反的属性 → 规则名与错误码 → 源码位置 → 修复建议」五要素的排查笔记。步骤 2/3 的具体输出待本地验证。

## 6. 本讲小结

- `PassContext` 是线程本地栈上的 Pass 运行环境：仪器、验证等级、诊断配置、内存规划器、后端句柄全在里面；`with` 进出栈，嵌套覆盖，`compile()` 自动继承外层配置且禁止重复指定。
- 两个验证开关分工明确：`VerificationLevel`（None/Basic/Roundtrip）控制流水线自动验证的强度，`Basic` 对轻量属性集「每属性恰一次」；`VerificationMode`（Before/After/BeforeAndAfter）控制 `VerificationInstrument` 的逐 Pass 验证，且总是连带全部结构属性。
- `PropertyVerifierRegistry` 以 `IRProperty → 工厂` 的单例映射组织全部验证规则；诊断带 `span`，报告的 `Location` 行把 IR 违规映射回作者源码行列。
- 本版本新增的 `AccCompactValid` 验证器守护 L0C 紧凑模式契约（pitch = ⌈validRow/16⌉×16），由 `InferTileMemorySpace` 首次产出、`ExpandMixedKernel` 失效后立即重产，在新 IR 上强制复验。
- 「同一属性同时出现在 produced 与 invalidated」不是笔误：流水线只验证 produced、且已验证过的属性会被跳过，先失效再重产是获得第二次验证机会的唯一途径；`AivSplitValid` 是同一模式的先例（开窗—重开窗—关窗）。

## 7. 下一步学习建议

- **下一讲（u5-l2）**：进入前端优化三连——`inline_functions`、`unroll_loops`、`ctrl_flow_transform`。阅读时留意它们的 `PassProperties` 声明（本讲 4.4 的分析工具直接可用），思考「内联为什么必须最先跑」与它产出的 `InlineFunctionsEliminated` 属性之间的关系。
- **延伸阅读**：[docs/en/dev/passes/99-verifier.md](https://github.com/hw-native-sys/pypto/blob/ec5d20c1818634e35b349a014a57afb998abea67/docs/en/dev/passes/99-verifier.md) 的内建规则总表是排错时的第一手资料——每条规则都写了「由哪个 Pass 产出 / 怎么修」。
- **动手方向**：到 u5-l8 你将亲手写一个新 Pass；届时回看本讲 4.4 的属性声明模式，想清楚你的 Pass 要不要 required、要不要 produced——属性声明错了，流水线对你的 Pass 的验证承诺就是空头支票。
