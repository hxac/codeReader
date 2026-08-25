# PatchManager：不改 vLLM 源码的运行时补丁机制

## 1. 本讲目标

学完本讲，你应该能够：

1. 解释 monkey patch（运行时补丁）的原理，以及 omni-npu 如何用一个 `VLLMPatch` 基类把「替换 vLLM 类/模块的某个符号」标准化。
2. 读懂 `PatchManager` 的注册、两级去重与按 `OMNI_NPU_VLLM_PATCHES` 环境变量应用补丁的完整流程，理解 `ALL` 的展开规则。
3. 掌握补丁目录的组织方式：`patches/common`（公共补丁）与 `patches/models/<模型目录>`（模型专属补丁），以及 `OMNI_NPU_PATCHES_DIR` 手动指定与按 `model_type` 自动匹配两条加载路径。
4. 能独立分析任意一个补丁文件：它替换了 vLLM 哪个模块的哪个符号、为什么必须在 NPU 部署栈上这样做。

## 2. 前置知识

### 2.1 什么是 monkey patch

Python 中，类和模块在运行时都是可以修改的对象。「monkey patch」指的是：**程序启动后，用一个新的函数或属性替换某个已存在类/模块上的同名符号**，从而改变其行为——完全不改它的源码文件。例如：

```python
# 示例代码：最简 monkey patch
from vllm.v1.core.sched.scheduler import Scheduler

def my_update_from_output(self, *args, **kwargs):
    print("被补丁接管了")
    return Scheduler.__dict__["_orig_update_from_output"](self, *args, **kwargs)

Scheduler.update_from_output = my_update_from_output  # 替换类方法
```

之后所有 `Scheduler` 实例调用 `update_from_output` 都会走新函数，且 `self` 仍是原来的 `Scheduler` 实例——新函数可以借用 `self` 访问原对象的全部属性。这就是零侵入适配的基础。

### 2.2 为什么 omni-npu 需要补丁机制

回顾 u2-l1：镜像中的 vLLM 是「空设备后端」的 0.14.0+empty 版本，omni-npu 以 out-of-tree 插件身份提供全部 NPU 能力，**原则上不修改 vLLM 源码**。但总有一些改动无法通过「实现平台接口」完成，例如：

- vLLM 上游某个函数的默认行为在 NPU 调度路径下有 bug，需要改几行逻辑；
- openPangu 模型需要 vLLM 支持上游没有的参数取值（如新的 KV Cache dtype）；
- MoE + 专家并行 + DP + MTP 投机解码组合下，上游调度器存在会导致挂死的边界条件。

这些改动的共同点是：**目标代码在 vLLM 包里，又必须按需、可开关地生效**。PatchManager 就是把这些散落的 monkey patch 统一成「一个文件一个补丁、注册名管理、环境变量开关」的工程化机制。

### 2.3 与前几讲的衔接

- u2-l1 讲过 omni-npu 在 `pyproject.toml` 注册了三个 entry points，其中 `omni_npu_patches` 指向 `omni_npu.vllm_patches:apply_patches`——本讲就从这个函数开始深入。
- u2-l2 的 `NPUPlatform` 是「声明式」适配（vLLM 预留的接口），本讲的补丁是「命令式」适配（直接替换符号），两者互补。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `components/omni-npu/src/omni_npu/vllm_patches/core.py` | 补丁基类 `VLLMPatch` 与注册装饰器 `register_patch`，定义「补丁长什么样」 |
| `components/omni-npu/src/omni_npu/vllm_patches/patch_manager.py` | `PatchManager`：注册表、去重、按环境变量应用 |
| `components/omni-npu/src/omni_npu/vllm_patches/__init__.py` | 入口 `apply_patches`：自动扫描补丁目录（`auto_import_patches`）并应用 |
| `components/omni-npu/src/omni_npu/vllm_patches/patches/common/patch_scheduler.py` | 公共补丁示例：补 vLLM `Scheduler.update_from_output` |
| `components/omni-npu/src/omni_npu/vllm_patches/patches/models/pangu_v2_hybrid/patch_scheduler.py` | 模型专属补丁示例：补 `Scheduler._update_request_with_output` |
| `components/omni-npu/src/omni_npu/vllm_patches/patches/models/pangu_v2_moe/patch_kv_cache_dtype.py` | 模型专属补丁示例：扩展 vLLM 支持的 `cache_dtype` 取值 |
| `components/omni-npu/src/omni_npu/vllm_patches/patches/examples/llm_engine_hello_world.py` | 官方提供的最小补丁示例（教学用） |
| `components/omni-npu/src/omni_npu/vllm_patches/README.md` | 补丁机制的官方使用文档 |
| `components/omni-npu/pyproject.toml` | 声明 `omni_npu_patches` entry point，是 vLLM 调用补丁系统的「钩子线」 |
| `tools/ansible/92B/omni_infer_server_template_performance1P1D_92B_bf16_open.yml` | 部署模板，真实生产中补丁环境变量的写法 |
| `components/omni-npu/tests/unit/vllm_patch/test_patch_dir_mapping.py` | 目录映射逻辑的单元测试 |

补丁目录全景（可自行 `ls` 验证）：

```text
patches/
├── common/          # 公共补丁，约 28 个 patch_*.py，对所有模型加载
├── models/          # 模型专属补丁
│   ├── pangu_v2_hybrid/   # openPangu V2 hybrid 注意力形态专属（11 个补丁）
│   └── pangu_v2_moe/      # openPangu V2 MoE 专属（4 个补丁）
└── examples/        # 教学示例，hello world
```

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：**monkey patch 机制**（补丁怎么写）、**patch 注册表**（补丁怎么注册与应用）、**模型专属补丁分组**（补丁文件怎么组织与发现）。

### 4.1 monkey patch 机制：VLLMPatch 与 register_patch

#### 4.1.1 概念说明

如果每个补丁都手写 `setattr`，很快会遇到三个问题：替换了什么没记录、重复替换互相覆盖、想临时关掉某个补丁只能改代码。omni-npu 用一个基类 + 一个装饰器把 monkey patch 标准化：

- **`VLLMPatch` 基类**：补丁类继承它，用类属性 `_attr_names_to_apply` 声明「要往目标上设置哪些符号」；类里定义的同名方法/属性就是新实现。
- **`register_patch(name, target)` 装饰器**：把补丁类与「注册名」「目标 vLLM 类或模块」绑定，并登记进 `PatchManager`。

于是每个补丁文件天然自带三要素说明书：**我叫什么（注册名）、我改谁（target）、我改什么（`_attr_names_to_apply`）**。

#### 4.1.2 核心流程

一个补丁从定义到生效：

```text
① 编写：@register_patch("名字", vLLM目标类/模块)
        class XxxPatch(VLLMPatch):
            _attr_names_to_apply = ["方法名", ...]
            def 方法名(self, ...): ...      # 新实现

② 注册：补丁文件被 import 时，装饰器副作用执行：
        cls._target = target
        PatchManager.register("名字", cls)   # 存入类级字典 registered_patches

③ 应用：XxxPatch.apply() 被调用时：
        for name in _attr_names_to_apply:
            检查保留名(apply/_target/_attr_names_to_apply) → 跳过并告警
            检查符号冲突 → 目标已被打过同符号补丁则 ValueError
            在目标._omni_npu_applied_patches 上登记
            setattr(target, name, 新实现)    # monkey patch 真正发生
```

关键设计：**补丁方法签名里的 `self` 接管的是原 vLLM 对象**。因为 `setattr` 把普通函数挂到目标类上，实例访问时 Python 自动绑定，`self` 就是 `Scheduler` 等目标类的实例——补丁方法内可以直接使用 `self.requests`、`self.connector` 等原对象的全部状态，仿佛自己就是原类的方法。

#### 4.1.3 源码精读

先看基类与装饰器，位于 [core.py:15-27](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/vllm_patches/core.py#L15-L27)：`VLLMPatch` 的类注释直接给出了标准用法模板——继承、声明 `_attr_names_to_apply`、定义新方法、调用 `MyPatch.apply()`。

应用逻辑在 [core.py:29-62](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/vllm_patches/core.py#L29-L62)，四个要点：

1. `apply()` 是类方法，直接对目标类/模块操作，不需要实例化补丁。
2. [core.py:35-37](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/vllm_patches/core.py#L35-L37)：在目标对象上 lazily 创建 `_omni_npu_applied_patches` 字典，这是**目标级去重账本**——记录「目标的哪个符号被哪个补丁类改过」。
3. [core.py:49-53](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/vllm_patches/core.py#L49-L53)：若目标符号已被别的补丁类改过，直接 `ValueError` 拒绝——防止两个补丁悄悄互相覆盖造成难以排查的行为错乱。
4. [core.py:57-60](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/vllm_patches/core.py#L57-L60)：若待设置的属性是已绑定的方法对象（例如从其他补丁类「转借」的实现），用 `MethodType(attr.__func__, target)` 重新绑定到目标上，再 `setattr`。

注册装饰器在 [core.py:65-74](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/vllm_patches/core.py#L65-L74)：它做了两件事——把 `target` 存为补丁类的 `_target` 私有属性，并调用 `PatchManager.register(name, cls)` 入表。注意 [core.py:66-67](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/vllm_patches/core.py#L66-L67) 限制了 target 只能是类或模块，不能是实例或函数。

官方最小示例在 [llm_engine_hello_world.py:17-31](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/vllm_patches/patches/examples/llm_engine_hello_world.py#L17-L31)：`LLMEngineHelloWorldPatch` 的目标是 vLLM 的 `LLMEngine` 类，`_attr_names_to_apply = ['print_hello_world', 'get_supported_tasks']` 同时展示了「新增属性」（原类没有 `print_hello_world`）和「替换方法」（重写 `get_supported_tasks`，先打印再转调原逻辑）两种用法。同文件 [llm_engine_hello_world.py:34-40](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/vllm_patches/patches/examples/llm_engine_hello_world.py#L34-L40) 则演示了 target 是**模块**（`vllm.engine.arg_utils`）而非类的情形。

#### 4.1.4 代码实践

**实践目标**：不依赖 NPU、不依赖 vLLM，在普通 Python 环境验证 `VLLMPatch` 的替换与去重行为。

**操作步骤**（以下为示例代码，保存为 `/tmp/try_patch.py`，在装有 omni-npu 源码的环境执行；若本机没有 omni-npu，可将 core.py 中的两个类复制进脚本自测）：

```python
# 示例代码：验证 VLLMPatch 的核心行为
from omni_npu.vllm_patches.core import VLLMPatch, register_patch

class FakeScheduler:            # 假装这是 vLLM 的类
    def update_from_output(self):
        return "original"

@register_patch("FakeSchedPatch", FakeScheduler)
class FakeSchedPatch(VLLMPatch):
    _attr_names_to_apply = ["update_from_output"]
    def update_from_output(self):
        return f"patched, I can see self's attr: {self.marker}"

s = FakeScheduler()
s.marker = "hello"              # 原对象上的属性
print(s.update_from_output())   # 应用前：original
FakeSchedPatch.apply()
print(s.update_from_output())   # 应用后：patched, I can see self's attr: hello
FakeSchedPatch.apply()          # 再次应用 → 目标级账本命中，ValueError
```

**需要观察的现象**：

1. `apply()` 前后输出从 `original` 变为 `patched...`，且补丁方法内能读到 `self.marker`——证明 `self` 接管了原对象。
2. 第二次 `apply()` 抛出 `ValueError: FakeScheduler.update_from_output already patched by FakeSchedPatch`。

**预期结果**：以上两个现象都出现，说明「替换 + 目标级去重」均按 4.1.2 的流程工作。若本机未安装 omni-npu 包，此实践改为纯源码阅读：对照 [core.py:39-60](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/vllm_patches/core.py#L39-L60) 逐行写下每一步操作，结论「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `_attr_names_to_apply` 里的名字不允许是 `apply`、`_target`、`_attr_names_to_apply`？

**参考答案**：这三个是 `VLLMPatch` 自身的框架保留名（应用入口、目标引用、声明列表）。若允许把它们 setattr 到目标上，会用补丁框架的内部机制覆盖 vLLM 目标类上的同名符号，造成不可预期行为。[core.py:40-42](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/vllm_patches/core.py#L40-L42) 对此做了拦截并打告警跳过。

**练习 2**：一个补丁类能否同时替换一个类的两个方法？能否同时给两个类打补丁？

**参考答案**：可以替换同一目标的多个方法——`_attr_names_to_apply` 是列表，逐个 setattr（如示例中的 `print_hello_world` 与 `get_supported_tasks`）。但一个补丁类只能绑定**一个** target（装饰器只存一个 `_target`），要打两个类需定义两个补丁类。

### 4.2 patch 注册表：PatchManager 的注册、去重与按环境变量应用

#### 4.2.1 概念说明

`VLLMPatch` 解决「单个补丁怎么写」，`PatchManager` 解决「一批补丁怎么管」。它提供三件事：

1. **注册表**：类级字典 `registered_patches`，键是注册名（字符串），值是补丁类。
2. **应用器**：按名字取出补丁类并调用其 `apply()`，带**管理器级去重**（同一补丁不重复应用）和**容错**（单个补丁失败不阻断启动）。
3. **环境变量开关**：用 `OMNI_NPU_VLLM_PATCHES` 决定「应用全部」还是「只应用指定几个」。

注意两级去重的分工：管理器级管「**同一个补丁**别应用两次」（警告跳过），目标级管「**同一个目标符号**别被两个不同补丁类改」（抛错拒绝）。

#### 4.2.2 核心流程

`manager.apply_patches()` 的决策树：

```text
读取环境变量 OMNI_NPU_VLLM_PATCHES（strip 后）
│
├─ 未设置 / 空字符串 / 恰好等于 "ALL"
│     └─ apply_all_patches()：遍历 registered_patches 逐个应用
│
└─ 否则（如 "PatchA,PatchB"）
      └─ apply_patches_from_env()：按逗号切分、strip、逐个应用

apply_patch(name) 内部：
  ├─ 未注册 → error 日志，返回（不中断）
  ├─ 已在 applied_patches → warning 日志，返回
  └─ 调用补丁类.apply()
        ├─ 成功 → 记入 applied_patches
        └─ 抛异常（含目标级 ValueError）→ error 日志吞掉，继续下一个
```

#### 4.2.3 源码精读

注册表本体在 [patch_manager.py:12-21](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/vllm_patches/patch_manager.py#L12-L21)：`registered_patches` 是**类属性**字典（所有实例共享一份全局注册表），`register` 类方法直接赋值 `cls.registered_patches[patch_name] = patch_class`——这意味着若两个补丁用同一个注册名，后者会静默覆盖前者，因此注册名需保持唯一。

单个应用逻辑在 [patch_manager.py:23-36](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/vllm_patches/patch_manager.py#L23-L36)：三段式检查（未注册→error；已应用→warning；否则 `apply()` 并记入 `self.applied_patches`）。特别注意 [patch_manager.py:32-36](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/vllm_patches/patch_manager.py#L32-L36) 的 `try/except` 把一切异常（包括 4.1 讲的目标级 `ValueError`）压成 error 日志——这是 fail-soft 设计：**一个补丁失败不会拖垮整个服务启动，但行为可能静默缺失**，所以排查「某个 NPU 特性没生效」时要先搜启动日志里的 `failed to apply`。

全量应用与按名应用分别在 [patch_manager.py:38-46](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/vllm_patches/patch_manager.py#L38-L46) 与 [patch_manager.py:48-64](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/vllm_patches/patch_manager.py#L48-L64)。前者的触发条件与 `ALL` 展开规则在总入口 [patch_manager.py:66-75](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/vllm_patches/patch_manager.py#L66-L75)：**环境变量未设置、空串、或恰好是字符串 `ALL`（区分大小写）时应用全部已注册补丁；否则按逗号分隔的注册名列表应用**。最后一行打印 `successfully applied patches: [...]`，是启动日志里确认补丁生效的最直接证据。这套默认行为与官方文档 [README.md:130-137](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/vllm_patches/README.md#L130-L137) 的描述一致。

还有一个必须理解的**时序约束**，见 [README.md:215-217](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/vllm_patches/README.md#L215-L217)：只有在补丁应用**之后**才被 import 的代码才会使用补丁后的实现。这靠 u2-l1 讲过的 entry point 机制保证——[pyproject.toml:36-38](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/pyproject.toml#L36-L38) 把 `omni_npu_patches = "omni_npu.vllm_patches:apply_patches"` 注册为 `vllm.general_plugins` 入口，vLLM 启动早期执行 `load_general_plugins` 时就会调用它，此时 vLLM 的大部分模块尚未被深度使用，替换能赶在生效前落地。

#### 4.2.4 代码实践

**实践目标**：在部署模板中定位补丁开关，并学会用日志验证「只应用指定补丁」。

**操作步骤**：

1. 打开 92B BF16 部署模板，找到 [omni_infer_server_template_performance1P1D_92B_bf16_open.yml:78-83](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_server_template_performance1P1D_92B_bf16_open.yml#L78-L83)，阅读 `# patch` 注释块：`VLLM_PLUGINS` 逐字点名三个插件（u2-l1 已讲），`OMNI_NPU_PATCHES_DIR` 指定补丁目录（4.3 详解），`OMNI_NPU_VLLM_PATCHES="ALL"` 表示应用全部。
2. 若有已部署容器（u1-l4 的环境），把模板中 `OMNI_NPU_VLLM_PATCHES` 的值改为 `"SchedulerPatch"`，重跑 `run_server` tag。
3. 在 `LOG_PATH` 下的 `server_*.log` 中检索三类日志行：`registered as`（注册）、`applying patches:`（应用名单）、`successfully applied patches:`（结果）。

**需要观察的现象**：改前日志的 `applying patches:` 列表很长（common + 两个模型目录的全部补丁）；改后只应用 `SchedulerPatch` 一个，其余补丁虽已注册但不应用。

**预期结果**：确认「注册」与「应用」是两个独立环节——所有被 import 的补丁文件都会注册，但只有名单内的会被 `apply()`。此步骤需要真实 NPU 容器，**待本地验证**。无环境时可先做日志阅读替代：在任一现成 `server_0.log` 中 grep `patch` 关键字观察已有输出。

#### 4.2.5 小练习与答案

**练习 1**：`OMNI_NPU_VLLM_PATCHES="all"`（小写）会发生什么？

**参考答案**：不会应用全部。`apply_patches` 里是严格相等比较 `apply_all_env == 'ALL'`（[patch_manager.py:70](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/vllm_patches/patch_manager.py#L70)），小写 `all` 会走 `apply_patches_from_env` 分支，被当作注册名 `all` 去查找，未注册则打 `patch all not registered` 并跳过——结果是**一个补丁都不应用**。

**练习 2**：生产排查时，哪个日志行能一次性看到「本进程实际生效了哪些补丁」？

**参考答案**：`successfully applied patches: [...]`（[patch_manager.py:75](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/vllm_patches/patch_manager.py#L75)）。它列出的正是管理器实例的 `applied_patches` 名单，注册但未应用的补丁不会出现在这里。

### 4.3 模型专属补丁分组：目录组织与自动发现

#### 4.3.1 概念说明

补丁文件放在哪里决定了它的**生效范围**：

- `patches/common/`：公共补丁，无论加载什么模型都会被 import 注册。内容是跨模型的通用修复（调度、前缀缓存、HCCL、tokenizer 复用等）。
- `patches/models/<目录名>/`：模型专属补丁，只有「被选中」的目录才会 import。选中方式有两条路径：
  - **手动**：设置 `OMNI_NPU_PATCHES_DIR="目录名"`（支持逗号分隔多个），走**精确匹配**；
  - **自动**：不设环境变量时，从启动命令里解析 `--model` 路径，读其 `config.json` 的 `model_type`，走**模糊匹配**（映射表 → 前缀 → 包含）。

真实部署用的是手动路径：模板里显式写死 `OMNI_NPU_PATCHES_DIR="pangu_v2_hybrid, pangu_v2_moe"`（bf16 与 w8a8 模板均如此），不依赖模型名推断，行为完全确定。

需要说明开源仓现状：`patches/models/` 下当前只有 `pangu_v2_hybrid` 与 `pangu_v2_moe` 两个目录；映射表中出现的 `pangu_v2_base`、`pangu_sink_swa_mla`、`openpangu_ultra_omni`、`deepseek`、`qwen` 等目录**未随本开源仓发布**，匹配时不存在即被跳过，不影响已发布目录的加载。

#### 4.3.2 核心流程

入口 `apply_patches()`（[vllm_patches/__init__.py:220-224](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/vllm_patches/__init__.py#L220-L224)）= 先 `auto_import_patches()` 再 `manager.apply_patches()`。前者把补丁文件 import 进来（触发注册），后者按 4.2 的规则应用。

`auto_import_patches()` 的完整决策流程：

```text
1. 无条件先加载 patches/common/ 全部 *.py（跳过 __init__.py，按文件名排序）

2. 读取 OMNI_NPU_PATCHES_DIR：
   ├─ 有值 → 手动模式，model_type = 环境变量值 → 精确匹配 _find_patch_dir_exact
   └─ 无值 → 自动模式：
         model_type = sys.argv 中 --model 的下一个参数（缺省取 argv[2]）
         model_type = 该路径 config.json 里的 "model_type" 字段
         把 model_type 写回环境变量（供后续递归/子进程复用）
         → 模糊匹配 _find_patch_dir_fuzzy

3. 匹配阶段（两条路径都会先查映射表 _get_patch_dir_names）：
   映射表命中 → 得到逗号分隔的目录名列表（一个模型可映射多个目录）
   精确匹配：逐个名字与 models/ 下子目录名忽略大小写相等比较
   模糊匹配：映射表无命中时退化为 前缀匹配 / 包含匹配

4. 对每个匹配到的目录：import_patches_from_dir 按文件名排序逐个 import
   （import 的副作用 = @register_patch 完成 4.2 的注册）
```

#### 4.3.3 源码精读

**目录扫描器** [vllm_patches/__init__.py:30-49](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/vllm_patches/__init__.py#L30-L49)：`import_patches_from_dir` 用 `rglob("*.py")` 递归收集补丁文件，**按文件名排序**保证加载顺序确定（例如 `patch_prefilled_token_skip_tokenize.py` 排在 `patch_scheduler.py` 之前，后者的顶部 import 才能拿到前者导出的辅助函数）；再用 `importlib.util.spec_from_file_location` 以 `omni_npu.vllm_patches.<子包>.<文件名>` 的合法模块名手动加载，并登记 `sys.modules` 防止重复加载。

**model_type 的两个来源**：手动模式直接用环境变量；自动模式下 [vllm_patches/__init__.py:19-27](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/vllm_patches/__init__.py#L19-L27) 从命令行参数中定位 `--model` 的值（无 `--model` 时回退取 `sys.argv[2]`，即 `vllm serve <model_path>` 的位置参数），[vllm_patches/__init__.py:52-67](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/vllm_patches/__init__.py#L52-L67) 再读该目录 `config.json` 的 `model_type` 字段。查 u3-l1 将精读的 `match_hf_configs.json` 可知：`openpangu_v2_92B`、`openpangu_v2_505B` 等模型的 `model_type` 均为 `openpangu_v2`。

**映射表** [vllm_patches/__init__.py:111-126](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/vllm_patches/__init__.py#L111-L126)：`_get_patch_dir_names` 维护「model_type → 补丁目录列表」的映射，例如 `"openpangu_v2": "pangu_v2_base,pangu_sink_swa_mla"`、`"pangu_v2_hybrid": "pangu_v2_base,pangu_v2_hybrid"`，未登记的 model_type 返回空列表。这个映射有双重作用：

- 自动模式：`openpangu_v2` 模型按表展开到多个目录（基座目录 + 特性目录），实现「模型族通用补丁 + 模型特有补丁」分层——正如 [README.md:164-188](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/vllm_patches/README.md#L164-L188) 所述，族内共享的补丁放基座目录，单模型独有的放各自目录；
- 手动模式：`_find_patch_dir_exact`（[vllm_patches/__init__.py:70-104](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/vllm_patches/__init__.py#L70-L104)）同样先过映射表——所以模板里写 `OMNI_NPU_PATCHES_DIR="pangu_v2_hybrid, pangu_v2_moe"` 时，`pangu_v2_hybrid` 会先被展开为 `pangu_v2_base,pangu_v2_hybrid`：`pangu_v2_base` 在开源仓中不存在，逐名字精确匹配时静默跳过；`pangu_v2_hybrid` 存在，命中加载。`pangu_v2_moe` 不在映射表，原样匹配到 `patches/models/pangu_v2_moe` 目录。

**模糊匹配回退** [vllm_patches/__init__.py:129-166](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/vllm_patches/__init__.py#L129-L166)：映射表未命中时，遍历 `models/` 子目录做「model_type 以目录名为前缀」或「目录名是 model_type 子串」的宽松匹配。匹配优先级（映射表 > 前缀 > 包含）在 [README.md:116-121](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/vllm_patches/README.md#L116-L121) 有官方说明。

**主流程与单例** [vllm_patches/__init__.py:169-214](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/vllm_patches/__init__.py#L169-L214)：`auto_import_patches` 按 4.3.2 流程执行，其中 [vllm_patches/__init__.py:192-199](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/vllm_patches/__init__.py#L192-L199) 是手动/自动的分岔点，且自动模式会把推断出的 model_type **写回环境变量**——这样同一进程后续再进入此函数时就走「已设置」分支，避免重复解析。模块底部 [vllm_patches/__init__.py:217](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/vllm_patches/__init__.py#L217) 创建模块级单例 `manager`，全部补丁经它应用。

**两个真实模型补丁的效果**（先睹为快，u3 会展开模型侧）：

- `pangu_v2_moe` 目录的 [patch_kv_cache_dtype.py:44-47](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/vllm_patches/patches/models/pangu_v2_moe/patch_kv_cache_dtype.py#L44-L47) 把扩展的 dtype 字面量类型 `CacheDType` 写进 vLLM 的 `cache` 模块；[patch_kv_cache_dtype.py:80-87](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/vllm_patches/patches/models/pangu_v2_moe/patch_kv_cache_dtype.py#L80-L87) 甚至覆写 `apply` 类方法直接改 dataclass 注解，让 `vllm serve` 的 CLI 不拒绝 `hif8_ds_mla` 这类 NPU 专属 KV Cache 精度参数——展示了 `_attr_names_to_apply` 之外「补丁类自带定制 apply」的进阶用法。
- 映射测试 [test_patch_dir_mapping.py:7-18](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/tests/unit/vllm_patch/test_patch_dir_mapping.py#L7-L18) 断言了 `openpangu_v2` 等模型到目录列表的展开结果，是理解映射表最短的入口（注意其引用的部分目录未随开源仓发布，见 4.3.4）。

#### 4.3.4 代码实践

**实践目标**：亲手推导部署模板中 `OMNI_NPU_PATCHES_DIR="pangu_v2_hybrid, pangu_v2_moe"` 的完整展开过程，并用测试/命令验证推导。

**操作步骤**：

1. **列表**：执行 `ls components/omni-npu/src/omni_npu/vllm_patches/patches/models/`，确认当前存在的目录（应为 `pangu_v2_hybrid` 与 `pangu_v2_moe`）。
2. **推导**（纸面作业，逐名字走一遍 `_find_patch_dir_exact`）：
   - `pangu_v2_hybrid` → 查映射表命中 `"pangu_v2_base,pangu_v2_hybrid"` → 逐个精确匹配：`pangu_v2_base` 不存在（跳过），`pangu_v2_hybrid` 存在（加载）；
   - `pangu_v2_moe` → 映射表未命中 → 原样精确匹配 `pangu_v2_moe`（加载）；
   - 注意模板字符串里的空格（`"pangu_v2_hybrid, pangu_v2_moe"`）由 `_split_patch_dir_names` 的 `strip()` 处理。
3. **验证一（只读命令）**：`grep -rn "register_patch(" components/omni-npu/src/omni_npu/vllm_patches/patches/models/pangu_v2_moe/` 列出该目录全部注册名（应为 `CacheDTypePatch`、`CacheDTypeSelectorPatch`、`CacheConfigPatch`、`CacheConfigFieldTypePatch`、`ExtendStrDtypeToTorchDtype`、`ExtendModelsConfigStrDtypeToTorchDtype`）。
4. **验证二（可选，需 omni-npu 环境）**：`cd components/omni-npu && python -m pytest tests/unit/vllm_patch/test_patch_dir_mapping.py -v`，对照输出检查：哪些用例只测映射表函数（应通过），哪些用例依赖 `pangu_v2_base`、`pangu_sink_swa_mla` 目录实体（开源仓缺这些目录，预计 `test_find_patch_dir_exact_supports_multiple_manual_dirs` 与 `test_find_patch_dir_fuzzy_supports_multiple_auto_dirs` 断言失败）。

**需要观察的现象**：步骤 4 中「纯映射表断言」与「目录实体断言」的通过情况截然不同。

**预期结果**：`test_get_patch_dir_names_*` 三个用例通过（映射表逻辑自洽）；两个 `_find_patch_dir_*` 用例因引用未发布目录而失败——这恰好实证了「映射表与目录实体是两回事」。步骤 4 需本地环境，**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：为什么不建议生产部署依赖「自动模式」（按 `model_type` 匹配），而模板里都显式设置 `OMNI_NPU_PATCHES_DIR`？

**参考答案**：自动模式依赖命令行参数解析（`--model` 或 `argv[2]`）和权重目录下 `config.json` 的 `model_type` 字段，再经映射表/前缀/包含三级模糊匹配，链路长且有回退分支；手动模式精确匹配目录名，行为确定、可审计。生产模板（如 [omni_infer_server_template_performance1P1D_92B_bf16_open.yml:81](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_server_template_performance1P1D_92B_bf16_open.yml#L81)）选择后者，代价是换模型族时要人工改这一行。

**练习 2**：一个补丁只对 `pangu_v2_hybrid` 形态的模型有意义，应放在哪个目录？若误放进 `common/` 会发生什么？

**参考答案**：放 `patches/models/pangu_v2_hybrid/`。误放进 `common/` 后，由于部署默认 `OMNI_NPU_VLLM_PATCHES="ALL"`，它会在**所有模型**（包括非 pangu 模型）上被应用，轻则无效报错被 try/except 吞掉，重则改变不相关模型的行为。目录划分就是生效范围的围栏。

**练习 3**：`auto_import_patches` 为什么要在自动模式下把推断出的 `model_type` 写回 `OMNI_NPU_PATCHES_DIR` 环境变量？

**参考答案**：见 [vllm_patches/__init__.py:192-199](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/vllm_patches/__init__.py#L192-L199) 的注释「Auto-set for recursive calls」：同一进程或子进程再次进入该函数时，环境变量已有值，直接走精确匹配，避免重复解析 `sys.argv`/`config.json`，也保证多次调用结果一致。

## 5. 综合实践

本讲的综合实践就是规格指定的分析任务：**从 `patches/common` 与 `patches/models/pangu_v2_hybrid` 中各挑一个补丁，写清楚它替换了 vLLM 哪个模块的哪个符号、为什么必须在 NPU 部署栈上这样做。**下面给出两个示范分析与操作流程。

**第一步：产出注册名清单**（只读命令，任何环境可做）：

```bash
grep -rn "register_patch(" components/omni-npu/src/omni_npu/vllm_patches/patches/common/ \
                           components/omni-npu/src/omni_npu/vllm_patches/patches/models/pangu_v2_hybrid/
```

**第二步：按下面的表格模板整理分析**（示范答案已填好）：

| 项 | common 选 `patch_scheduler.py` | pangu_v2_hybrid 选 `patch_scheduler.py` |
| --- | --- | --- |
| 注册名 | `SchedulerPatch`（[common/patch_scheduler.py:29](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/vllm_patches/patches/common/patch_scheduler.py#L29)） | `PanguV2SchedulerPatch`（[pangu_v2_hybrid/patch_scheduler.py:31](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/vllm_patches/patches/models/pangu_v2_hybrid/patch_scheduler.py#L31)） |
| 目标 | vLLM 类 `vllm.v1.core.sched.scheduler.Scheduler` | 同一个类 `Scheduler` |
| 替换符号 | `update_from_output`（`_attr_names_to_apply`，[common/patch_scheduler.py:31](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/vllm_patches/patches/common/patch_scheduler.py#L31)） | `_update_request_with_output`（[pangu_v2_hybrid/patch_scheduler.py:33-35](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/vllm_patches/patches/models/pangu_v2_hybrid/patch_scheduler.py#L33-L35)） |
| 关键改动 | 投机解码分支增加 `and generated_token_ids` 判空检查，源码注释 `Patch: Added ...` 明确标出（[common/patch_scheduler.py:94-101](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/vllm_patches/patches/common/patch_scheduler.py#L94-L101)）；并在末尾追加 `drain_pending_finish_outputs(self, outputs)`（[common/patch_scheduler.py:242](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/vllm_patches/patches/common/patch_scheduler.py#L242)） | 开启 MTP 时把有效截断长度缩为 `max_model_len - 3×num_speculative_tokens`（[pangu_v2_hybrid/patch_scheduler.py:65-67](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/vllm_patches/patches/models/pangu_v2_hybrid/patch_scheduler.py#L65-L67)），并在停机判定处换用增强版 `check_stop`（[pangu_v2_hybrid/patch_scheduler.py:78](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/vllm_patches/patches/models/pangu_v2_hybrid/patch_scheduler.py#L78)） |
| 为什么必须在 NPU 栈上做 | 上游假设每步必有采样输出；NPU 侧调度路径可能返回空 `sampled_token_ids`，不判空会把 `num_accepted` 算成负数并错误回退 `num_computed_tokens`（根因细节待本地验证）。`drain_pending_finish_outputs` 来自 common 补丁 `patch_prefilled_token_skip_tokenize`（[common/patch_scheduler.py:22-24](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/vllm_patches/patches/common/patch_scheduler.py#L22-L24)），配合 token 复用特性输出补齐结果 | 注释写明：MoE + 专家并行 + DP 下，各 DP 组在接近 `max_model_len` 时 drafter（MTP/EAGLE）执行不一致会导致服务挂死，需提前终止请求；×3 裕量对应异步调度流水线的位置滞后（[pangu_v2_hybrid/patch_scheduler.py:41-64](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/vllm_patches/patches/models/pangu_v2_hybrid/patch_scheduler.py#L41-L64) 有完整推导）。这是 openPangu 在 NPU 上「MoE+EP+DP+MTP」组合部署特有的边界条件，vLLM 上游没有此行为 |

**第三步（动手验证，需 u1-l4 的容器环境，待本地验证）**：在模板中把 `OMNI_NPU_VLLM_PATCHES` 改为 `"SchedulerPatch,PanguV2SchedulerPatch"` 重启，从日志确认只有这两个补丁被应用；随后发送一个长输出请求，观察 D 节点日志中请求是否在接近 `max_model_len` 处提前正常收尾（而非挂死）——即模型补丁实际生效的行为证据。

**第四步（自主重复）**：从第一步清单中再任选两个补丁（例如 common 的 `patch_eplb_parallel.py` 与 pangu_v2_hybrid 的 `patch_static_sink_attention.py`），不看正文、独立填写同样的表格，检验自己是否掌握了「注册名 / target / 符号 / 动机」四要素分析法。

## 6. 本讲小结

- omni-npu 用 `VLLMPatch` 基类 + `register_patch` 装饰器把 monkey patch 标准化：补丁类声明 `_attr_names_to_apply`，`apply()` 把新实现 `setattr` 到 vLLM 目标类/模块上，补丁方法的 `self` 直接接管原 vLLM 对象。
- 去重有两级：`PatchManager.applied_patches` 防「同一补丁重复应用」（警告跳过）；目标上的 `_omni_npu_applied_patches` 防「两个补丁改同一符号」（抛 `ValueError`，被管理器吞成 error 日志，不影响启动）。
- 应用开关 `OMNI_NPU_VLLM_PATCHES`：未设置/空串/严格等于 `ALL` 时应用全部已注册补丁，否则按逗号分隔的注册名白名单应用；`successfully applied patches:` 日志是生效名单的权威来源。
- 补丁目录即生效范围：`common/` 全模型加载；`models/<目录>/` 经 `OMNI_NPU_PATCHES_DIR` 手动精确匹配或 `model_type`（映射表→前缀→包含）自动匹配加载；生产模板统一用手动模式写死 `pangu_v2_hybrid, pangu_v2_moe`。
- 整条链路由 entry point 串起：`VLLM_PLUGINS` 点名 `omni_npu_patches` → vLLM 启动早期调用 `omni_npu.vllm_patches:apply_patches` → `auto_import_patches()` 注册 → `manager.apply_patches()` 应用；补丁必须赶在目标模块被实际使用前生效。
- 分析任一补丁的四要素：注册名（`@register_patch` 第一参）、目标（第二参）、符号（`_attr_names_to_apply`）、动机（与上游实现 diff，注意源码里 `Patch:` 开头的注释标记）。

## 7. 下一步学习建议

本讲补齐了 omni-npu 插件机制的第三块拼图（平台声明 u2-l2、Worker 生命周期 u2-l3、运行时补丁本讲）。建议接下来：

1. **进入 u3-l1（OpenPanguV2 MoE 模型结构总览）**：模型补丁目录 `pangu_v2_hybrid`、`pangu_v2_moe` 之所以存在，正是因为模型本身有 hybrid 稀疏注意力与 MoE 两大特性；学完模型结构再回看这些补丁，动机会更清晰。
2. **顺读两个「进阶补丁」源码**：`patches/models/pangu_v2_moe/patch_kv_cache_dtype.py`（自定义 `apply` 的补丁）与 `patches/common/patch_eplb_parallel.py`（u9-l2 专家负载均衡的 vLLM 侧衔接点），体会 `_attr_names_to_apply` 之外的表达力。
3. **为 u10-l3（二次开发）做储备**：本讲的四要素分析法就是「新增一个补丁」的需求分析模板——可先在纸上为「给 `Scheduler.schedule` 加一行耗时日志」设计补丁类，等学到测试体系（u10-l2）后补上单测闭环。
