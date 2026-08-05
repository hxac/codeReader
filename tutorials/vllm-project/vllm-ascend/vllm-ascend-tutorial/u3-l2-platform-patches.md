# Platform Patch 实战解析

## 1. 本讲目标

学完本讲后，读者应该能够：

- 说清楚 `patch/platform/` 下的补丁在**引擎核心子进程**的哪个阶段被触发、为什么必须在那里触发。
- 用源码讲明白 `patch_fused_moe.py` 是如何把上游 `FusedMoE` 工厂重定向到 `AscendMoERunner` 的，并能画出从「模型导入」到「拿到 Ascend runner」的时序。
- 读懂 `patch_kv_cache_utils.py` 与 `patch_mamba_config.py` 这类「KV 缓存 + Mamba 相关」补丁的改写逻辑。
- 读懂 `patch_distributed.py` 与 `patch_use_v2_model_runner.py` 这类「平台行为改写」补丁的意图。
- 理解「platform 补丁」与「worker 补丁」共享逻辑时如何复用以避免重复包装。

## 2. 前置知识

本讲建立在 u3-l1《Patch 机制总览与两阶段应用》之上，这里只回顾三个最关键的术语，不再展开：

- **Monkey-patch（猴子补丁）**：在运行时把某个模块里的函数/类替换成自己的版本，而不修改上游源码。三步套路是「捕获原对象 → 定义替换实现 → 把替换实现重绑定回原模块」。
- **两阶段补丁**：`adapt_patch(is_global_patch=True)` 触发 **platform 补丁**，在引擎核心子进程生效；`adapt_patch(is_global_patch=False)` 触发 **worker 补丁**，在每一个 worker 子进程的 `__init__` 生效。
- **登记规范五要素**：每个补丁要在 `patch/__init__.py` 里登记 What（补了什么）/ Why（为什么）/ How（怎么补）/ Related PR / Future Plan（何时可移除）。

如果你对上面任意一点感到陌生，请先回看 u3-l1。本讲只聚焦 `patch/platform/` 目录下「真正在引擎核心改写调度、分布式、MoE、KV 缓存」的那几个实战补丁。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [patch/platform/\_\_init\_\_.py](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/patch/platform/__init__.py) | platform 补丁的「总开关」：import 它即触发全部 platform 补丁。 |
| [patch/platform/patch_fused_moe.py](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/patch/platform/patch_fused_moe.py) | 把上游 `FusedMoE` 工厂重定向到 `AscendMoERunner`。本讲的主角。 |
| [patch/platform/patch_kv_cache_utils.py](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/patch/platform/patch_kv_cache_utils.py) | 放宽上游对「混合 KV cache + 上下文并行」的限制，并接管 DeepSeek V4 的张量布局规划。 |
| [patch/platform/patch_mamba_config.py](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/patch/platform/patch_mamba_config.py) | 在 NPU 上把 Mamba/注意力块大小对齐到 128 token 内核约束。 |
| [patch/platform/patch_distributed.py](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/patch/platform/patch_distributed.py) | 310P 上用 `all_gather` 模拟 `broadcast`/`all_reduce`，解决张量对齐问题。 |
| [patch/platform/patch_use_v2_model_runner.py](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/patch/platform/patch_use_v2_model_runner.py) | 让 `VllmConfig.use_v2_model_runner` 只由环境变量决定，绕过上游的模型架构白名单。 |
| [patch/\_\_init\_\_.py](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/patch/__init__.py) | 全部补丁的登记簿（What/Why/How/PR/Future Plan）。 |
| [utils.py](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/utils.py) | 定义两阶段补丁的总闸 `adapt_patch`。 |

> 阅读提示：`patch/platform/__init__.py` 本身几乎没有逻辑代码，它的全部「魔力」来自「import 某个补丁模块，该模块在被 import 的瞬间就在模块顶层完成重绑定」。所以理解 platform 补丁的关键是**逐个打开补丁文件看它的模块级代码**，而不是看 `__init__.py`。

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：
1. Platform 补丁的生效时机与登记规范
2. `patch_fused_moe`：MoE 工厂重定向（主角）
3. KV 缓存与 Mamba 相关补丁
4. 平台行为改写类补丁（分布式 / v2 runner）

### 4.1 Platform 补丁的生效时机与登记规范

#### 4.1.1 概念说明

`patch/platform/` 里的补丁和 `patch/worker/` 里的补丁最大的区别是**生效进程不同**：

- **platform 补丁**在**引擎核心（EngineCore）子进程**生效，影响的是调度器、配置校验、KV cache 规划等「全局逻辑」。这些逻辑运行在 worker 启动之前，worker 子进程根本碰不到它们。
- **worker 补丁**在**每个 worker 子进程**生效，影响的是模型前向、算子替换等「单卡执行逻辑」。

正因为生效进程不同，所以两阶段补丁必须分开触发——spawn 出来的 worker 是全新解释器，不会继承父进程已经打好的补丁。

#### 4.1.2 核心流程

platform 补丁的触发链是：

```text
vLLM 启动
  └─ 扫描 vllm.platform_plugins entry points
     └─ 调用 vllm_ascend.register()        返回 NPUPlatform 路径
        └─ vLLM 选中 NPUPlatform
           └─ 调用 NPUPlatform.pre_register_and_update()
              └─ adapt_patch(is_global_patch=True)
                 └─ import vllm_ascend.patch.platform
                    └─ platform/__init__.py 逐行 import 各补丁模块
                       └─ 每个补丁模块在「被 import 瞬间」完成重绑定 ✓
```

此外，由于 vLLM 在 engine-core 子进程里会通过**通用插件回调**（`register_connector`/`register_model_loader`/`register_service_profiling`）重新加载插件，而这些回调运行的地方不一定会先经过 `pre_register_and_update`，所以 vllm-ascend 用 `_ensure_global_patch()` 这道「同进程一次」的幂等闸门，保证平台级补丁在 engine-core 子进程里也一定会被打上。

#### 4.1.3 源码精读

总闸 `adapt_patch` 非常简洁，它只做一件事：按标志位 import 对应的子包，import 的副作用就是打补丁。

[utils.py:533-537](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/utils.py#L533-L537) — `adapt_patch` 的全部实现：取 `True` 就 import `platform` 包，取 `False`（默认）就 import `worker` 包：

```python
def adapt_patch(is_global_patch: bool = False):
    if is_global_patch:
        from vllm_ascend.patch import platform  # noqa: F401
    else:
        from vllm_ascend.patch import worker  # noqa: F401
```

[platform.py:182-187](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/platform.py#L182-L187) — `NPUPlatform.pre_register_and_update` 是 platform 补丁的第一触发点：

```python
@classmethod
def pre_register_and_update(cls, parser=None) -> None:
    # Adapt the global patch here.
    from vllm_ascend.utils import adapt_patch
    adapt_patch(is_global_patch=True)
```

[\_\_init\_\_.py:56-70](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/__init__.py#L56-L70) — `_ensure_global_patch` 是幂等闸门，被各通用插件回调调用，确保 engine-core 子进程也能打上平台补丁：

```python
_GLOBAL_PATCH_APPLIED = False

def _ensure_global_patch():
    global _GLOBAL_PATCH_APPLIED
    if _GLOBAL_PATCH_APPLIED:
        return
    from vllm_ascend.utils import adapt_patch
    adapt_patch(is_global_patch=True)
    _GLOBAL_PATCH_APPLIED = True
```

[patch/platform/\_\_init\_\_.py:17-46](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/patch/platform/__init__.py#L17-L46) — 这里逐行 import 了全部 platform 补丁，注意有几条带条件分支（310P 与非 310P 加载不同 Mamba 补丁；`patch_multiproc_executor` 仅在开启动态 EPLB 时加载）。读这段就能知道当前到底打了哪些 platform 补丁：

```python
import vllm_ascend.patch.platform.patch_distributed  # noqa
import vllm_ascend.patch.platform.patch_kv_cache_utils  # noqa
...
import vllm_ascend.patch.platform.patch_use_v2_model_runner  # noqa
from vllm_ascend.utils import is_310p
if not is_310p():
    import vllm_ascend.patch.platform.patch_mamba_config  # noqa
else:
    import vllm_ascend.patch.platform.patch_mamba_config_310  # noqa
...
import vllm_ascend.patch.platform.patch_fused_moe  # noqa
import vllm_ascend.patch.platform.patch_dp_device_ids  # noqa
```

> 细节：`import` 语句的顺序本身是有意义的——比如 `patch_fused_moe` 必须在任何模型被 import **之前**打上，否则模型会先拿到未替换的 `FusedMoE`。这一点在 4.2 节会展开。

关于登记规范，[patch/\_\_init\_\_.py:87-104](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/patch/__init__.py#L87-L104) 给出了 `patch_fused_moe` 的五要素示例：What 是 `FusedMoE` 工厂、Why 是因为模型会直接 import 并调用它、How 是同时替换包 `__init__` 与 layer 模块两处绑定、Future Plan 是「等上游暴露后端分发钩子后移除」。本讲后续每个补丁都会回扣这套规范。

#### 4.1.4 代码实践

1. **实践目标**：确认 platform 补丁确实在「模型 import 之前」生效。
2. **操作步骤**（源码阅读型）：
   - 打开 [patch/platform/patch_fused_moe.py:18-28](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/patch/platform/patch_fused_moe.py#L18-L28) 头部注释，它写明了 worker `__init__` 的三步 import 顺序。
   - 再对照 [worker/worker.py:108-113](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/worker/worker.py#L108-L113)，确认顺序确实是「先 `adapt_patch()` 再 `from vllm_ascend import ops`，最后才模型加载」。
3. **需要观察的现象**：注释里标注的 `adapt_patch() → FusedMoE patched → ops → 模型加载拿到 patched FusedMoE` 这条因果链。
4. **预期结果**：你能解释「为什么补丁必须在模型 import 之前打」——因为模型模块顶层 `from vllm.model_executor.layers.fused_moe import FusedMoE` 是在 import 时就把名字绑定到本地命名空间，补晚了本地绑定已经是旧对象，替换无效。
5. 待本地验证（无 NPU 也可完成，纯源码阅读）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `adapt_patch` 选择「import 子包」而不是「显式调用一个 `apply()` 函数」？

> **参考答案**：因为 Python 在 import 一个模块时会执行它的模块级代码。把重绑定写在模块顶层，就能保证「import 即生效」，调用方只需一行 import 即可，且天然支持「子进程重新 import 重新打补丁」。如果改成显式 `apply()`，每个调用点都要记得调用，容易遗漏，也无法直接利用 import 副作用。

**练习 2**：`_ensure_global_patch` 为什么需要一个模块级布尔 `_GLOBAL_PATCH_APPLIED`？

> **参考答案**：因为同一个进程里可能先被 `register_connector`、再被 `register_model_loader` 等多个回调触发，如果不加幂等保护，同一批补丁会被重复 import/重绑定。虽然多数补丁重绑定是幂等的，但 `patch_fused_moe` 这类「捕获原对象」的补丁如果被二次包装就会出错（详见 4.2）。布尔闸门保证每进程只打一次。

---

### 4.2 patch_fused_moe：MoE 工厂重定向（主角）

#### 4.2.1 概念说明

这是整个 `patch/platform/` 里最经典、也最值得吃透的一个补丁，因为它同时示范了三个高难度问题：

1. **被补对象是「工厂函数」而不是「类」**。上游 vLLM 的 `FusedMoE` 其实是一个**工厂函数**（factory function），它在内部 `return FusedMoELayer(...)`，并接受 `runner_cls`、`routed_experts_cls` 等参数来决定用哪个 MoE runner。
2. **模型是「按名字直接 import」它的**。DeepSeek 等模型在源码顶部写 `from vllm.model_executor.layers.fused_moe import FusedMoE`，import 的瞬间就把 `FusedMoE` 这个名字绑定到了**模型模块自己的命名空间**。
3. **同一个名字存在于两个地方**。`FusedMoE` 既出现在包的 `__init__`（即 `vllm.model_executor.layers.fused_moe`），也出现在 layer 模块（`vllm.model_executor.layers.fused_moe.layer`）。模型 `from ... import FusedMoE` 拿到的是包 `__init__` 里 re-export 的那个名字。

所以 vllm-ascend 必须在**模型被 import 之前**，把这两处的 `FusedMoE` 绑定**都**替换成自己的 `_ascend_FusedMoE`，否则模型拿到的还是原版工厂，永远走不到 Ascend runner。

补丁的最终效果是：当模型调用 `FusedMoE(...)` 时，实际进入 `_ascend_FusedMoE`，它把默认的 `runner_cls` 改成 `AscendMoERunner`、`routed_experts_cls` 改成 `AscendRoutedExperts`，其余参数原样交给捕获的原工厂。

#### 4.2.2 核心流程

`_ascend_FusedMoE` 替换函数的内部决策流程（伪代码）：

```text
def _ascend_FusedMoE(*args, runner_cls=None, routed_experts_cls=None, **kwargs):
    if runner_cls is None:                  # 调用方没显式指定 runner
        runner_cls = AscendMoERunner        #   → 默认走 Ascend runner
    if routed_experts_cls is None:          # 调用方没显式指定 experts
        routed_experts_cls = AscendRoutedExperts

    # EPLB（专家负载均衡）相关：把冗余专家数透传给上游工厂
    if 开启了动态 EPLB 或指定了 expert_map:
        校验 vLLM 与 Ascend 的冗余专家数不冲突
        kwargs["enable_eplb"] = True
        kwargs["num_redundant_experts"] = 配置值

    kwargs.pop("hash", None)                # DeepSeek V4 专属，进厂前已消费
    tid2eid = kwargs.pop("tid2eid", None)   # Ascend 专属，属于 RoutedExperts
    routed_experts_args["n_shared_experts"] = ...
    if tid2eid is not None:
        routed_experts_args["tid2eid"] = tid2eid

    return _original_FusedMoE(              # 调用捕获的原工厂
        *args, runner_cls=runner_cls,
        routed_experts_cls=routed_experts_cls,
        routed_experts_args=routed_experts_args, **kwargs)
```

完整时序（见 4.2.4 实践的图）：

```text
[平台层，worker.__init__ 之前/之中]
  adapt_patch(is_global_patch=True/False)
    └─ import patch_fused_moe
       └─ 捕获 _original_FusedMoE = layer.FusedMoE
       └─ 把 layer.FusedMoE  = _ascend_FusedMoE   (重绑定 1)
       └─ 把 pkg.FusedMoE    = _ascend_FusedMoE   (重绑定 2)

[模型加载时]
  from vllm.model_executor.layers.fused_moe import FusedMoE
    └─ Python 解析到 pkg.FusedMoE，此时已是 _ascend_FusedMoE ✓
  模型层 __init__: self.mlp = FusedMoE(...)
    └─ 进入 _ascend_FusedMoE
       └─ runner_cls 默认填 AscendMoERunner
       └─ 调用 _original_FusedMoE(...) 构造真正的 FusedMoELayer
          └─ 该层的 runner 实例是 AscendMoERunner ✓
```

#### 4.2.3 源码精读

先看头部注释，它本身就是一份迷你设计文档，写明了 import 顺序约束和「为什么两处都要替换」：

[patch_fused_moe.py:18-28](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/patch/platform/patch_fused_moe.py#L18-L28) — 设计说明：

```python
# Patch vllm's FusedMoE factory to use AscendMoERunner by default.
# vllm's FusedMoE is a factory function (not a class). deepseek_v2 and other
# models do `from vllm.model_executor.layers.fused_moe import FusedMoE` and
# call it directly, so we must patch the binding in the package __init__ as
# well as the layer module before any model is imported.
```

捕获原对象——必须在替换之前把「真身」存下来，否则替换后再也拿不回来了：

[patch_fused_moe.py:39](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/patch/platform/patch_fused_moe.py#L39-L39) — 捕获原始工厂：

```python
_original_FusedMoE = _fused_moe_layer.FusedMoE
```

选择默认 runner——根据是否 310P 走不同实现，这体现了「同一段抽象，不同硬件不同实现」：

[patch_fused_moe.py:43-53](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/patch/platform/patch_fused_moe.py#L43-L53) — 按硬件选默认 runner/experts：

```python
if is_310p():
    from vllm_ascend._310p.fused_moe.fused_moe import AscendMoERunner310, AscendRoutedExperts310
    _DefaultAscendMoERunner = AscendMoERunner310
    _DefaultAscendRoutedExperts = AscendRoutedExperts310
else:
    from vllm_ascend.ops.fused_moe.fused_moe import AscendMoERunner
    from vllm_ascend.ops.fused_moe.routed_experts import AscendRoutedExperts
    _DefaultAscendMoERunner = AscendMoERunner
    _DefaultAscendRoutedExperts = AscendRoutedExperts
```

被重定向到的两个真实类定义在：
- [ops/fused_moe/fused_moe.py:32](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/ops/fused_moe/fused_moe.py#L32-L32) — `class AscendMoERunner(MoERunner)`。
- [ops/fused_moe/routed_experts.py:256](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/ops/fused_moe/routed_experts.py#L256-L256) — `class AscendRoutedExperts(RoutedExperts)`。

替换函数的核心：默认值注入 + EPLB 透传 + Ascend 专属参数剥离：

[patch_fused_moe.py:56-97](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/patch/platform/patch_fused_moe.py#L56-L97) — `_ascend_FusedMoE` 全文（关键片段）：

```python
def _ascend_FusedMoE(*args, runner_cls=None, runner_args=None,
                     routed_experts_cls=None, routed_experts_args=None, **kwargs):
    if runner_cls is None:
        runner_cls = _DefaultAscendMoERunner
    if routed_experts_cls is None:
        routed_experts_cls = _DefaultAscendRoutedExperts
    # EPLB：把冗余专家槽透传给上游工厂，保证建权重时就有冗余位
    eplb_config = get_ascend_config().eplb_config
    if eplb_config.dynamic_eplb or eplb_config.expert_map_path is not None:
        ...
        kwargs["enable_eplb"] = True
        kwargs["num_redundant_experts"] = configured_redundancy or upstream_redundancy
    kwargs.pop("hash", None)                 # DeepSeek V4 专属
    tid2eid = kwargs.pop("tid2eid", None)    # Ascend 专属
    routed_experts_args = dict(routed_experts_args) if routed_experts_args else {}
    routed_experts_args["n_shared_experts"] = n_shared_experts
    if tid2eid is not None:
        routed_experts_args["tid2eid"] = tid2eid
    return _original_FusedMoE(*args, runner_cls=runner_cls,
                              routed_experts_cls=routed_experts_cls,
                              routed_experts_args=routed_experts_args, **kwargs)
```

> 读懂 `runner_cls=None` 这个判断：只有当**模型没显式指定** runner 时才注入 Ascend 默认值。这意味着如果某个模型明确传了 `runner_cls=XXX`，补丁会尊重它——这是一种「非破坏式默认值」设计，避免误伤需要特殊 runner 的模型。

最后是两处重绑定——这是整个补丁「生效」的关键动作：

[patch_fused_moe.py:100-101](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/patch/platform/patch_fused_moe.py#L100-L101) — 同时替换 layer 模块与包 `__init__`：

```python
_fused_moe_layer.FusedMoE = _ascend_FusedMoE
_fused_moe_pkg.FusedMoE   = _ascend_FusedMoE
```

为什么 worker 侧也有一个 `patch_fused_moe` 却只有一行 import？为了避免「二次包装」：worker 是全新进程，会重新 import 上游 `FusedMoE`，如果 worker 再写一份替换逻辑，就会把已经替换过的 `_ascend_FusedMoE` 当成「原始对象」再包一层。解决办法是 worker 直接复用 platform 补丁：

[patch/worker/patch_fused_moe.py:18-20](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/patch/worker/patch_fused_moe.py#L18-L20) — worker 复用 platform 补丁：

```python
# Reuse the platform patch. Keeping the monkey patch in one module avoids
# wrapping an already patched FusedMoE factory during worker initialization.
import vllm_ascend.patch.platform.patch_fused_moe  # noqa: F401
```

#### 4.2.4 代码实践（本讲主实践）

1. **实践目标**：画出「模型导入 `FusedMoE` → 被重定向到 `AscendMoERunner`」的完整执行时序，并标注每一步发生在哪个进程、哪行代码。
2. **操作步骤**：
   - 阅读 [patch_fused_moe.py](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/patch/platform/patch_fused_moe.py) 全文。
   - 在纸上画出下面这张时序图（进程用泳道区分）：

     ```text
     [EngineCore 进程]
       pre_register_and_update
         → adapt_patch(is_global_patch=True)
            → import patch.platform → import patch_fused_moe
               → _original_FusedMoE = layer.FusedMoE   (捕获)
               → layer.FusedMoE = _ascend_FusedMoE      (重绑定 layer)
               → pkg.FusedMoE   = _ascend_FusedMoE      (重绑定 pkg)

     [Worker 进程 __init__]
       adapt_patch()  → import patch.worker → import worker/patch_fused_moe
            → 复用 platform/patch_fused_moe（幂等，不二次包装）
       from vllm_ascend import ops
       load_model()
         → 模型模块执行 from vllm...fused_moe import FusedMoE
              → 取到 pkg.FusedMoE，即 _ascend_FusedMoE ✓
         → 模型层 self.mlp = FusedMoE(...)
              → 进入 _ascend_FusedMoE
                 → runner_cls=AscendMoERunner, routed_experts_cls=AscendRoutedExperts
                 → _original_FusedMoE(...) 构造真层，runner 即 AscendMoERunner ✓
     ```
   - 在图上用三种颜色/标记区分：①捕获原对象 ②两处重绑定 ③模型取到替换后的对象。
3. **需要观察的现象**：模型拿到的 `FusedMoE` 名字，在 worker 进程里到底是 `_ascend_FusedMoE` 还是原版。
4. **预期结果**：是 `_ascend_FusedMoE`。因为 worker 进程通过 `worker/patch_fused_moe` 复用了 platform 补丁，import 模型时包 `__init__` 的 `FusedMoE` 已被重绑定。
5. 待本地验证（如能在 NPU 环境打断点，可在 `_ascend_FusedMoE` 入口打印 `runner_cls` 确认为 `AscendMoERunner`；无 NPU 则以源码阅读结论为准）。

#### 4.2.5 小练习与答案

**练习 1**：如果只重绑定 `_fused_moe_layer.FusedMoE`，而忘了重绑定 `_fused_moe_pkg.FusedMoE`，会发生什么？

> **参考答案**：模型写的是 `from vllm.model_executor.layers.fused_moe import FusedMoE`，取的是**包 `__init__`**（`_fused_moe_pkg`）里 re-export 的那个名字。如果只替换 layer 模块而没替换包 `__init__`，模型 import 时拿到的仍是包里那个**旧**的 `FusedMoE`，补丁对它完全无效。这就是注释强调「must patch the binding in the package `__init__` as well as the layer module」的原因。

**练习 2**：为什么 `_ascend_FusedMoE` 要 `kwargs.pop("hash", None)` 和 `kwargs.pop("tid2eid", None)`？

> **参考答案**：`hash` 是 DeepSeek V4 的专属参数，在调用 `FusedMoE` 之前就已经被消费，原版工厂不认识它，留着会报「意外参数」错；`tid2eid` 是 Ascend 专属、属于 `AscendRoutedExperts` 而非上游工厂的字段，所以要从 `kwargs` 里取出，转而塞进 `routed_experts_args`。两者都是「在交给上游工厂前，先把上游不认识的参数剥离/转放」。

**练习 3**：为什么 worker 侧不直接复制一份 `_ascend_FusedMoE`，而是 import 复用 platform 补丁？

> **参考答案**：worker 是 spawn 出的全新进程，会重新执行 `import vllm.model_executor.layers.fused_moe`。如果 worker 自己再写一份替换，`_original_FusedMoE = _fused_moe_layer.FusedMoE` 捕获到的可能已经是被 platform 补丁替换过的 `_ascend_FusedMoE`，于是会出现「包装了已包装的工厂」的二次嵌套。把替换逻辑集中在一个模块、worker 只 import 它，既保证幂等，又让逻辑只有一处真相源。

---

### 4.3 KV 缓存与 Mamba 相关补丁

#### 4.3.1 概念说明

这一组补丁的共同主题是：**上游 vLLM 为 CUDA 设备写死的某些「内存/块大小」假设，在 Ascend 上不成立，需要改写。** 典型有两类：

- **`patch_kv_cache_utils.py`**：上游在 vLLM PR #40860 里加了一条限制——「混合 KV cache 组（多种 block size）不支持上下文并行（DCP）」。这条限制对 CUDA 成立，但 Ascend 能为 MLA 层和 SWA-MLA 层**各自独立**做上下文并行，所以必须放宽。
- **`patch_mamba_config.py`**：上游默认 Mamba 块大小是 16，Ascend 的状态拷贝内核要求按 **128 token** 对齐，且要求「注意力页大小 ≥ Mamba 页大小」。所以需要在配置校验阶段重算块大小。

#### 4.3.2 核心流程

`patch_kv_cache_utils` 的改写规则（伪代码）：

```text
def _ascend_resolve_kv_cache_block_sizes(kv_cache_config, vllm_config):
    groups = kv_cache_config.kv_cache_groups
    if 组数 <= 1:
        return block_size*dcp, block_size*dcp          # 单组，直接放大 dcp 倍
    if dcp != 1:                                        # 多组 + 上下文并行
        scheduler_block_size = lcm(各组 block_size) * dcp
        if 未开 prefix caching:
            return scheduler_block_size, scheduler_block_size
        else:
            return scheduler_block_size, gcd(各组 block_size)   # hash 用 gcd
    return 原版逻辑                                      # dcp==1 时回退上游
```

关键数学关系：调度块大小取各组块大小的**最小公倍数（LCM）**再乘 DCP 因子，保证所有组都能整除；而用于 prefix cache 哈希的块大小取**最大公约数（GCD）**，保证哈希粒度对齐。用公式表达：

\[ \text{scheduler\_block\_size} = \mathrm{lcm}(b_1, b_2, \dots, b_n) \times \text{dcp} \]

\[ \text{hash\_block\_size} = \mathrm{gcd}(b_1, b_2, \dots, b_n) \]

`patch_mamba_config` 的核心是「以 ssm 块大小为基准，向上对齐到 128 的倍数来定注意力块大小，再把 Mamba 页大小 pad 到与注意力页大小相等」。

#### 4.3.3 源码精读

[patch_kv_cache_utils.py:23-56](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/patch/platform/patch_kv_cache_utils.py#L23-L56) — `_ascend_resolve_kv_cache_block_sizes`，多组 + DCP 用 LCM 替代上游的报错：

```python
def _ascend_resolve_kv_cache_block_sizes(kv_cache_config, vllm_config):
    ...
    if len(groups) <= 1:
        bs = cache_config.block_size * dcp
        return bs, bs
    if dcp != 1:
        group_block_sizes = [g.kv_cache_spec.block_size for g in groups]
        scheduler_block_size = math.lcm(*group_block_sizes) * dcp
        if not cache_config.enable_prefix_caching:
            return scheduler_block_size, scheduler_block_size
        hash_block_size = math.gcd(*group_block_sizes)
        return scheduler_block_size, hash_block_size
    return _orig_resolve_kv_cache_block_sizes(kv_cache_config, vllm_config)
```

> 注意最后那行 `return _orig_resolve_kv_cache_block_sizes(...)`：当 `dcp == 1`（无上下文并行）时，补丁**回退到上游原逻辑**。这是「只改自己需要改的分支，其余不动」的良好实践，能减少与上游的差异面、降低未来升级成本。

补丁尾部还替换了多个函数，且特别处理了 vLLM v0.24.0 的重命名问题——上游把 `_get_kv_cache_config_deepseek_v4` 改名成 `_get_kv_cache_config_packed`，于是补丁两个名字都打上：

[patch_kv_cache_utils.py:248-259](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/patch/platform/patch_kv_cache_utils.py#L248-L259) — 多处重绑定，含 engine/core 的直接引用：

```python
vllm.v1.core.kv_cache_utils.resolve_kv_cache_block_sizes = _ascend_resolve_kv_cache_block_sizes
vllm.v1.core.kv_cache_utils.group_and_unify_kv_cache_specs = group_and_unify_kv_cache_specs
vllm.v1.core.kv_cache_utils._get_kv_cache_groups_uniform_groups = _get_kv_cache_groups_uniform_groups
# v0.24.0 重命名后，get_kv_cache_config_from_groups 直接调用 _get_kv_cache_config_packed
vllm.v1.core.kv_cache_utils._get_kv_cache_config_packed = _get_kv_cache_config_deepseek_v4
# engine/core.py 是直接 import 了该函数，所以要单独替换它的引用
import vllm.v1.engine.core  # noqa: E402
vllm.v1.engine.core.resolve_kv_cache_block_sizes = _ascend_resolve_kv_cache_block_sizes
```

> 这段揭示了 Monkey-patch 的一个常见陷阱：如果某模块用 `from xxx import func` 把函数**直接绑定**到自己命名空间，那么光替换 `xxx.func` 是不够的，还得替换那个模块里的引用。注释里 `engine/core.py imports the function directly` 就是在提醒这一点。

再看 Mamba 配置补丁。它在配置校验阶段（`verify_and_update_config`）重算块大小，核心约束是「Ascend 内核要求 128 对齐」：

[patch_mamba_config.py:58](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/patch/platform/patch_mamba_config.py#L58-L58) — 写死内核块对齐常量：

```python
kernel_block_size = 128
```

[patch_mamba_config.py:78-97](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/patch/platform/patch_mamba_config.py#L78-L97) — 以 ssm 块大小为基准对齐注意力块大小，并断言两者页大小相等：

```python
# NOTE(zxr): 受 Ascend 硬件限制，需让所有 cache 张量连续，
# 所以把 ssm_block 与 attn_block 的页大小对齐
...
attn_block_size = kernel_block_size * cdiv(ssm_block_page_size, kernel_block_size * attn_single_token_k_page_size)
assert attn_single_token_k_page_size * attn_block_size == ssm_block_page_size, (
    "Cannot align ssm_page_size and attn_page_size."
)
```

最后完成重绑定（用 `@classmethod` 包装后替换）：

[patch_mamba_config.py:149](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/patch/platform/patch_mamba_config.py#L149-L149) — 替换上游类方法：

```python
vllm.model_executor.models.config.HybridAttentionMambaModelConfig.verify_and_update_config = verify_and_update_config
```

登记规范方面，[patch/\_\_init\_\_.py:149-159](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/patch/__init__.py#L149-L159) 说明：Why 是「上游默认 block size 16 在 Ascend 不支持」，How 是「在 NPU 上设为 128」，Future Plan 是「等上游合并 PR 后移除」。注意 310P 走的是另一个补丁 `patch_mamba_config_310.py`（见 [patch/platform/\_\_init\_\_.py:26-29](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/patch/platform/__init__.py#L26-L29) 的分支）。

#### 4.3.4 代码实践

1. **实践目标**：理解 `patch_kv_cache_utils` 在「多组 + DCP」下为何用 LCM 而非简单取最大值。
2. **操作步骤**：
   - 假设有两个 KV cache 组，block size 分别为 \(b_1 = 4\)、\(b_2 = 6\)，DCP = 2。
   - 手算：\(\mathrm{lcm}(4, 6) = 12\)，\(\mathrm{gcd}(4, 6) = 2\)。
   - 代入补丁公式得 `scheduler_block_size = 12 * 2 = 24`，开 prefix caching 时 `hash_block_size = 2`。
   - 验证：24 既能被 \(4 \times 2\) 整除，也能被 \(6 \times 2\) 整除（\(\Rightarrow\) 每组都能整块切分），符合调度要求。
3. **需要观察的现象**：若改用「取最大值 6」会怎样？\(6 \times 2 = 12\)，但 \(12 / (4 \times 2) = 1.5\) 不是整数，组 1 无法整块切分，调度会出错。
4. **预期结果**：LCM 是「能让所有组都整除」的最小块大小，这正是上游报错、Ascend 用 LCM 放宽的正确性所在。
5. 待本地验证（纯数学推导，无需 NPU）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `hash_block_size` 用 GCD 而不是和 `scheduler_block_size` 一样用 LCM？

> **参考答案**：prefix cache 的哈希是按块粒度算的，哈希块必须能被**每一组**的块整除才能保证不同组命中同一哈希桶时语义一致。GCD 是「所有组块大小的公共因子」，用它能保证哈希粒度对齐；用 LCM 反而会让哈希粒度过大、命中率下降。

**练习 2**：`patch_mamba_config` 为什么放在 platform 补丁（而不是 worker 补丁）里？

> **参考答案**：因为 `verify_and_update_config` 是在**配置校验阶段**被调用，属于引擎核心的全局逻辑，运行在 worker 启动之前。worker 进程根本不会执行配置校验，所以这类「改配置」的补丁必须是 platform 补丁。这与 `patch_fused_moe` 必须在模型 import 前打上是同样的「时机」道理。

---

### 4.4 平台行为改写类补丁（分布式 / v2 runner）

#### 4.4.1 概念说明

最后看两个「改写平台行为」的补丁，它们都不涉及模型/算子，而是修正上游对硬件能力的错误假设：

- **`patch_distributed.py`**：310P 上 `torch.distributed.broadcast`/`all_reduce` 对 `int64` 张量有对齐问题，补丁用 `all_gather` 模拟它们。**只在 310P 生效**。
- **`patch_use_v2_model_runner.py`**：上游 `VllmConfig.use_v2_model_runner` 除了看环境变量，还会按模型架构白名单、Triton 可用性等自动启用 v2 runner；但 Ascend 的 v2 runner 还没完全兼容这些自动启用的场景，所以补丁改成「只认环境变量」，把兼容性判断交还给 NPU runner 自己。

#### 4.4.2 核心流程

`patch_distributed` 的替换策略（以 broadcast 为例）：

```text
def broadcast310p(tensor, src=0, group=None, async_op=False, group_src=None):
    if tensor 在 CPU 上:
        走原版 fn                       # CPU 不涉及 NPU 对齐
    root = group_src or src
    用 all_gather 收集所有 rank 的 tensor
    tensor[...] = tensor_list[src]      # 只取 src 那一份广播出去
    return NullHandle() if async_op else None
```

它同时替换了 `torch.distributed.broadcast` 和更底层的 `torch.distributed.distributed_c10d.broadcast`（两处都要替换，原因和 4.2 节「两处重绑定」一样：不同调用路径可能走不同入口）。`all_reduce` 同理，对 `int64` 张量改用 `all_gather` 后在端侧做 SUM/MAX。

`patch_use_v2_model_runner` 则极简：

```text
def _patched_use_v2_model_runner(self) -> bool:
    use_v2 = envs.VLLM_USE_V2_MODEL_RUNNER
    if use_v2 is not None:
        return use_v2          # 只认环境变量
    return False               # 未设则默认关
```

#### 4.4.3 源码精读

[patch_distributed.py:33-54](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/patch/platform/patch_distributed.py#L33-L54) — 用 `all_gather` 模拟 `broadcast`：

```python
def communication_adaptation_310p():
    def broadcast310p_wrapper(fn):
        def broadcast310p(tensor, src=0, group=None, async_op=False, group_src=None):
            root = group_src if group_src is not None else src
            if tensor.device == torch.device("cpu"):
                return fn(tensor, src=root, group=group, async_op=async_op)
            rank = torch.distributed.get_rank(group)
            world_size = torch.distributed.get_world_size(group)
            tensor_list = [torch.empty_like(tensor) for _ in range(world_size)]
            tensor_list[rank] = tensor
            torch.distributed.all_gather(tensor_list, tensor, group=group)
            tensor[...] = tensor_list[src]
            if async_op:
                return NullHandle()
            else:
                return None
        return broadcast310p

    torch.distributed.broadcast = broadcast310p_wrapper(torch.distributed.broadcast)
    torch.distributed.distributed_c10d.broadcast = broadcast310p_wrapper(
        torch.distributed.distributed_c10d.broadcast)
```

> 注意这里用了「装饰器工厂」`broadcast310p_wrapper(fn)`：它捕获原函数 `fn`，返回一个新函数。这种写法的好处是 broadcast 和底层 `distributed_c10d.broadcast` 可以复用同一套替换逻辑，只各自传入自己的原函数。

[patch_distributed.py:88-89](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/patch/platform/patch_distributed.py#L88-L89) — 只在 310P 时才打补丁（模块级条件执行）：

```python
if get_ascend_device_type() == AscendDeviceType._310P:
    communication_adaptation_310p()
```

这是一种「硬件门控」模式：非 310P 卡 import 这个模块什么都不会发生，避免影响 A2/A3 主力卡。

再看 v2 runner 补丁，这是本讲最小的补丁：

[patch_use_v2_model_runner.py:5-20](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/patch/platform/patch_use_v2_model_runner.py#L5-L20) — 把属性改写成「只读环境变量」：

```python
def _patched_use_v2_model_runner(self) -> bool:
    """Return VLLM_USE_V2_MODEL_RUNNER env directly.
    The upstream use_v2_model_runner gate-keeps the v2 runner with
    per-model architecture whitelists, Triton availability checks, and
    feature-support inspections. On Ascend the v2 runner is controlled
    purely by the VLLM_USE_V2_MODEL_RUNNER environment variable;
    model-compatibility decisions are deferred to the NPU runner itself.
    """
    use_v2 = envs.VLLM_USE_V2_MODEL_RUNNER
    if use_v2 is not None:
        return use_v2
    return False


VllmConfig.use_v2_model_runner = property(_patched_use_v2_model_runner)
```

> 关键点：这里补的不是普通方法，而是用 `property(...)` 替换 `VllmConfig.use_v2_model_runner`。因为上游把它实现成了**属性**（property），补丁必须也用 property 替换，签名里多了一个 `self`。这种「按上游的访问形式（方法/属性/类方法）来决定补丁形态」是写补丁的基本功。

与 4.2 节呼应，worker 侧也有一个 `patch_v2/patch_use_v2_model_runner.py`，它同样只是复用 platform 补丁（见登记 [patch/\_\_init\_\_.py:1177-1191](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/patch/__init__.py#L1177-L1191)），保证 EngineCore 子进程与 worker 子进程行为一致。

#### 4.4.4 代码实践

1. **实践目标**：对比「属性补丁」与「函数补丁」的写法差异。
2. **操作步骤**：
   - 打开 [patch_use_v2_model_runner.py:20](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/patch/platform/patch_use_v2_model_runner.py#L20-L20)，看到它用 `VllmConfig.use_v2_model_runner = property(_patched_use_v2_model_runner)`。
   - 再对比 [patch_mamba_config.py:149](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/patch/platform/patch_mamba_config.py#L149-L149)，那里是直接赋值一个 `@classmethod`。
3. **需要观察的现象**：两种替换的右侧表达式形态不同（`property(...)` vs 直接函数/类方法）。
4. **预期结果**：你能总结出规律——补一个**属性**要用 `property()`，补一个**实例方法**直接赋函数，补一个**类方法**要 `@classmethod` 包装，补一个**模块级函数**直接对模块属性赋值。选错形态会导致 `self`/`cls` 错位或访问报错。
5. 待本地验证（无 NPU 可纯源码对比）。

#### 4.4.5 小练习与答案

**练习 1**：`patch_distributed` 为什么要同时替换 `torch.distributed.broadcast` 和 `torch.distributed.distributed_c10d.broadcast`？

> **参考答案**：`torch.distributed.broadcast` 是上层入口，但它内部往往委托给底层的 `distributed_c10d.broadcast`；而有些代码路径会直接调用底层那个。只替换上层的话，直接走底层的调用就绕过了补丁。两处都替换才能覆盖全部调用路径。这和 4.2 节「包 `__init__` 与 layer 模块两处都要替换」是同一个道理。

**练习 2**：`_patched_use_v2_model_runner` 为何在 `use_v2 is None` 时返回 `False` 而不是抛错？

> **参考答案**：这是「默认关闭」的安全策略。Ascend 的 v2 runner 尚未完全兼容上游自动启用的场景，所以当用户**没显式**要求开 v2 时，应当默认走稳定的 v1 路径，而不是因为上游的模型架构白名单自动开启而崩溃。把「是否兼容」的判断交还给 NPU runner 自己（`model-compatibility decisions are deferred to the NPU runner itself`），更可控。

---

## 5. 综合实践

把本讲四个模块串起来，完成下面这个「补丁诊断」小任务：

**任务背景**：假设你给 vllm-ascend 新加了一个 MoE 模型，启动后发现它的 MoE 层用的还是**上游原版** runner（没走到 `AscendMoERunner`），导致前向在 NPU 上报算子错误。请按下面的步骤定位并解释。

1. **第一步（4.1）**：确认补丁有没有被打上。说明你会检查哪两个文件、哪两行来验证 `_ascend_FusedMoE` 是否完成了两处重绑定。
   - 参考：[patch_fused_moe.py:100-101](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/patch/platform/patch_fused_moe.py#L100-L101)。
2. **第二步（4.2）**：如果补丁打上了但模型仍拿原版，最可能的原因是「补丁比模型 import 晚」。请引用 [patch_fused_moe.py:18-28](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/patch/platform/patch_fused_moe.py#L18-L28) 的注释说明 import 顺序约束，并指出应该在哪一行（[worker/worker.py:108-113](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/worker/worker.py#L108-L113)）确保 `adapt_patch()` 早于模型加载。
3. **第三步（4.2）**：如果模型是「显式传了 `runner_cls=XXX`」的新模型，`_ascend_FusedMoE` 会不会注入 `AscendMoERunner`？为什么？引用 [patch_fused_moe.py:64-67](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/patch/platform/patch_fused_moe.py#L64-L67) 说明。
4. **产出**：写一段 150 字左右的「诊断结论」，说明这个 MoE 模型没走到 Ascend runner 的可能原因（至少列两种），以及各自的修复方向。

> 参考诊断方向：①补丁未被触发（确认是否走了 platform/worker 补丁链路）；②补丁晚于模型 import；③模型显式指定了非 Ascend runner；④该模型走的是 `models/` 目录下的直接实现而非 patch 路径（参见 u11-l1）。

## 6. 本讲小结

- **platform 补丁在引擎核心子进程生效**：由 `NPUPlatform.pre_register_and_update` → `adapt_patch(is_global_patch=True)` → `import patch.platform` 触发，影响调度/配置/KV 等全局逻辑；`_ensure_global_patch` 提供每进程一次的幂等保证。
- **`patch_fused_moe` 是最经典的工厂补丁**：被补对象是工厂函数且模型按名字直接 import，所以必须**捕获原对象**并**同时重绑定包 `__init__` 与 layer 模块两处**，且必须在模型 import 之前完成；`runner_cls=None` 时才注入 Ascend 默认值，是非破坏式默认。
- **worker 侧通过复用 platform 补丁避免二次包装**：`worker/patch_fused_moe.py` 只有一行 import，既保证幂等又让逻辑只有一处真相源。
- **KV/Mamba 补丁修正的是「CUDA 假设」**：`patch_kv_cache_utils` 用 LCM×dcp 放宽多组+DCP 限制（回退分支保持上游行为）；`patch_mamba_config` 按 128 内核对齐重算块大小。
- **平台行为补丁要按上游访问形态来补**：`patch_distributed` 用装饰器工厂同时替换上下两层入口且只在 310P 生效；`patch_use_v2_model_runner` 用 `property()` 替换属性、只认环境变量。
- **写补丁的五条基本功**：①补丁必须早于上游真正取用该符号；②同一符号多处绑定的都要替换（含直接 import 引用）；③按上游形态选 property/方法/类方法；④只改需要改的分支、其余回退上游；⑤用硬件门控避免影响其他卡。

## 7. 下一步学习建议

- 下一讲 **u3-l3《Worker Patch 实战解析》** 会进入 `patch/worker/` 目录，讲解模型前向、投机解码、Triton 算子、CUDA Graph 相关的 worker 级补丁。建议先对照本讲的 `worker/patch_fused_moe.py` 复用模式，带着「worker 补丁为什么不能直接复制 platform 逻辑」这个问题去读。
- 想深入理解被重定向到的 `AscendMoERunner` 全链路，可继续读 **u7-l3《Fused MoE 引擎与通信》**。
- 想理解 KV/Mamba 补丁背后的 MLA/DSA 注意力，可读 **u5-l2《MLA / SFA / DSA 与稀疏注意力》**。
- 想亲手贡献一个新补丁，直接跳到 **u11-l5《二次开发实战：贡献一个新补丁》**，那里有命名、文档化、lint、提交的完整规范。
