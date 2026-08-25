# 二次开发：新增补丁、模型配置与连接器

## 1. 本讲目标

学完本讲，你应该能够：

1. 独立写一个最小 `VLLMPatch` 补丁类，把它注册进 `PatchManager`，并在容器内用 `OMNI_NPU_VLLM_PATCHES` 只启用它、从日志确认生效。
2. 按照 `model_config/README.md` 的官方流程，为一个新模型登记最佳实践配置：登记 `match_hf_configs.json` 指纹 → 路由 `best_practice_configs.json` → 落盘配置 json → 用 `CUSTOM_MODEL_CONFIG_PATH` 验证。
3. 理解 KV connector 的注册扩展点（`KVConnectorFactory` 注册表 + `omni.kv_connectors` entry point）与模型装饰器扩展点（`plugin_decorators.py` 的 pre/post 钩子）。
4. 掌握「进容器改代码」的开发工作流：定位 omni-npu 在容器内的源码位置、修改后如何让改动生效。

本讲是专家层实战讲义：不再讲新机制，而是把 u2-l4（补丁）、u5-l1（模型配置）、u4-l2（connector）、u2-l1（entry points）四讲拆开过的扩展点收拢成一张「二次开发地图」，并各配一次动手操作。

## 2. 前置知识

本讲默认你已读完 u2-l4、u5-l1，并了解 u2-l1 的 entry points 概念。开始前用通俗语言回顾四个关键概念：

- **monkey patch（运行时补丁）**：程序启动后，用 `setattr` 把某个类/模块上的函数换成自己的版本，从而改变行为且不修改上游源码。u2-l4 讲过 omni-npu 靠它做到「零改 vLLM 源码」。
- **entry point（入口点）**：Python 包在安装时写进元数据的「名字 → 模块:属性」字符串表。任何程序都能用 `importlib.metadata.entry_points()` 查表并延迟导入，这是插件被「发现」的标准机制（u2-l1 讲过 vLLM 靠 `vllm.platform_plugins` 发现 omni-npu）。
- **工厂注册表（factory registry）**：一个「名字 → 类路径字符串」的字典。调用方只给名字（如 `kv-transfer-config` 里的 `"LLMDataDistConnector"`），工厂查表动态 import 对应类。新增实现只需往表里加一行，不需要改调用方。
- **dataclass 配置类**：用 `@dataclass` 声明的、带默认值的配置对象。omni-npu 的 `ModelExtraConfig` 由 `TaskConfig` / `ModelParallelConfig` / `ModelOperatorOptConfig` 三个 dataclass 组成，json 文件只负责覆盖默认值（u5-l1 讲过）。

一个容易混淆的点先钉死——两个名字相近的环境变量管的是完全不同的事：

| 环境变量 | 管什么 | 生效阶段 |
| --- | --- | --- |
| `OMNI_NPU_PATCHES_DIR` | 从 `patches/models/` 下加载**哪个模型补丁目录**（决定哪些补丁被 import 注册） | 补丁导入阶段 |
| `OMNI_NPU_VLLM_PATCHES` | 已注册的补丁中**应用哪些**（逗号分隔名单，或空/`ALL` 全量） | 补丁应用阶段 |

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [components/omni-npu/src/omni_npu/vllm_patches/core.py](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/vllm_patches/core.py) | `VLLMPatch` 基类与 `register_patch` 装饰器——写新补丁的直接父类 |
| [components/omni-npu/src/omni_npu/vllm_patches/patch_manager.py](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/vllm_patches/patch_manager.py) | `PatchManager`：注册表、去重、按环境变量应用 |
| [components/omni-npu/src/omni_npu/vllm_patches/__init__.py](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/vllm_patches/__init__.py) | `auto_import_patches`：扫描 common 与模型专属目录完成注册，entry point 入口 `apply_patches` |
| [components/omni-npu/src/omni_npu/vllm_patches/patches/common/patch_config_file_lock.py](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/vllm_patches/patches/common/patch_config_file_lock.py) | 最简单的一个真实补丁，作为仿写模板 |
| [components/omni-npu/src/omni_npu/model_config/README.md](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/model_config/README.md) | 新增模型配置 json 的官方四步流程 |
| [components/omni-npu/src/omni_npu/model_config/config_loader/loader.py](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/model_config/config_loader/loader.py) | 配置加载实现：指纹匹配、最佳实践路由、`CUSTOM_MODEL_CONFIG_PATH` 覆盖 |
| [components/omni-npu/src/omni_npu/connector/register.py](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/connector/register.py) | 把 `LLMDataDistConnector` 注册进 vLLM `KVConnectorFactory` |
| [components/omni-npu/src/omni_npu/platform.py](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/platform.py) | `import_kernels`：内置 connector 注册 + 消费 `omni.kv_connectors` entry point |
| [components/omni-npu/src/omni_npu/connector/llmdatadist_connector_v1.py](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/connector/llmdatadist_connector_v1.py) | 内置 connector 的门面类，新 connector 的参考架构 |
| [components/omni-npu/src/omni_npu/plugin_decorators.py](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/plugin_decorators.py) | 模型前向等关键路径的 pre/post 钩子装饰器工厂 |
| [components/omni-npu/pyproject.toml](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/pyproject.toml) | omni-npu 自己注册的 vLLM entry points |
| [README.md](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/README.md) | 顶层部署说明，「推理代码适配」一节即容器内改码入口 |

## 4. 核心概念与源码讲解

### 4.1 patch 扩展：从零写一个补丁并注册应用

#### 4.1.1 概念说明

omni-npu 对 vLLM 的适配有两条腿（u2-l4）：`NPUPlatform` 的**声明式**适配（告诉 vLLM「我是谁」），以及 `VLLMPatch` 的**命令式**适配（直接替换 vLLM 的函数/方法）。本模块讲后者如何扩展：你要给自己的环境加一个行为——比如给某个 vLLM 函数加一行调试日志——正确的姿势不是 fork vLLM，而是写一个补丁类：

- 补丁类继承 `VLLMPatch`，用 `_attr_names_to_apply` 声明要替换哪些符号；
- `@register_patch(名字, 目标)` 把它登记进 `PatchManager` 的注册表；
- 把文件放进 `patches/common/`（所有模型生效）或 `patches/models/<模型>/`（特定模型生效），`auto_import_patches` 会自动 import 它完成注册；
- `apply_patches` 在 vLLM 启动早期（由 entry point `omni_npu_patches` 触发）按 `OMINI_NPU_VLLM_PATCHES` 决定应用范围。

#### 4.1.2 核心流程

写一个新补丁的五步法：

```text
1. 定位目标：找到 vLLM 中要替换的类/模块与符号（函数名或方法名）
2. 模块级保存原实现：_ORIGINAL = Target.symbol   ← 必须在类定义前抓住，否则替换后再拿就拿到自己的了
3. 定义补丁类：
   @register_patch("MyPatchName", Target)
   class MyPatch(VLLMPatch):
       _attr_names_to_apply = ["symbol"]
       def symbol(self, ...):        # 签名与原函数一致
           ...自己的逻辑（如打日志）...
           return _ORIGINAL(self, ...)  # 回调原实现
4. 放置文件：patches/common/ 或 patches/models/<模型目录>/
5. 验证：启动时看两条日志
   "patch applied: MyPatch => Target.symbol"          （apply 成功）
   "successfully applied patches: ['MyPatchName']"    （最终名单）
```

应用阶段的选择逻辑：

```text
apply_patches():
    env = OMNI_NPU_VLLM_PATCHES
    if env 为空 或 == "ALL":  应用全部已注册补丁
    else:                     按逗号分隔名单应用
```

#### 4.1.3 源码精读

**补丁基类：`VLLMPatch.apply()` 与 `register_patch` 装饰器。**
[components/omni-npu/src/omni_npu/vllm_patches/core.py:15-27](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/vllm_patches/core.py#L15-L27) 定义基类：`_attr_names_to_apply` 是待替换符号名单，docstring 就是官方用法示例。

[components/omni-npu/src/omni_npu/vllm_patches/core.py:29-62](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/vllm_patches/core.py#L29-L62) 是 `apply()` 的全部逻辑，四件事：

1. 在目标对象上挂一个 `_omni_npu_applied_patches` 字典做**符号级去重**（第 49-53 行）：若 `Target.symbol` 已被别的补丁替换过，直接 `raise ValueError`——这是 u2-l4 说的「两个补丁改同一符号」防线；
2. 校验符号确实定义在本补丁类的 `__dict__` 里（第 44-45 行），写错名字立即报错而不是静默失效；
3. 若是方法则用 `MethodType` 重绑到目标类（第 57-58 行），保证替换后 `self` 语义正确；
4. `setattr(target, name, attr)` 完成替换并打 `patch applied` 日志（第 60-62 行）。

[components/omni-npu/src/omni_npu/vllm_patches/core.py:65-74](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/vllm_patches/core.py#L65-L74) 是装饰器：只接受类或模块作为目标，把目标存在 `cls._target` 上并调用 `PatchManager.register(name, cls)` 完成注册——所以**注册发生在 import 补丁文件时**，不等到应用时。

**注册与应用：`PatchManager`。**
[components/omni-npu/src/omni_npu/vllm_patches/patch_manager.py:12-21](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/vllm_patches/patch_manager.py#L12-L21) 的 `register` 是类方法，写进类级字典 `registered_patches`——全进程共享一份注册表。

[components/omni-npu/src/omni_npu/vllm_patches/patch_manager.py:23-36](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/vllm_patches/patch_manager.py#L23-L36) 的 `apply_patch` 做第二级去重（同一补丁重复应用只告警）并 `try/except` 包住应用动作——**单个补丁失败只记 error 日志，不会阻断启动**（u2-l4 讲过），这意味着补丁写错了服务照样起，必须看日志确认。

[components/omni-npu/src/omni_npu/vllm_patches/patch_manager.py:66-75](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/vllm_patches/patch_manager.py#L66-L75) 是应用入口 `apply_patches`：环境变量未设、空串或严格等于 `ALL` 时全量应用，否则走 `apply_patches_from_env`（第 48-64 行）按逗号分隔名单应用，最后打印 `successfully applied patches`——**这份名单是验证补丁生效的权威日志**。

**目录发现：`auto_import_patches`。**
[components/omni-npu/src/omni_npu/vllm_patches/__init__.py:169-214](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/vllm_patches/__init__.py#L169-L214) 规定注册的来源：先无条件 import `common/` 目录下所有 `.py`（第 183-185 行），再确定 model_type——没设 `OMNI_NPU_PATCHES_DIR` 时从命令行 `--model` 参数读权重目录、再读其 `config.json` 的 `model_type`（第 192-196 行，注意首次会自动回写环境变量），然后经映射表/前缀/包含三级模糊匹配找到模型专属目录（第 201-208 行）；用户手工设置了该环境变量则走精确匹配（`_find_patch_dir_exact`，第 70-104 行）。所以：**放进 common/ 的补丁必然被注册，放进 models/ 子目录的补丁要看 model_type 是否命中**。

[components/omni-npu/src/omni_npu/vllm_patches/__init__.py:30-49](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/vllm_patches/__init__.py#L30-L49) 的 `import_patches_from_dir` 用 `rglob("*.py")` 递归扫描并按文件名排序逐个 import——不需要在任何地方「登记文件名」，落盘即注册。

**谁触发这一切：entry point。**
[components/omni-npu/pyproject.toml:36-38](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/pyproject.toml#L36-L38) 把 `omni_npu_patches = "omni_npu.vllm_patches:apply_patches"` 注册进 `vllm.general_plugins`，vLLM 启动早期加载它。而 [components/omni-npu/src/omni_npu/vllm_patches/__init__.py:220-224](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/vllm_patches/__init__.py#L220-L224) 的 `apply_patches` 先 `auto_import_patches()` 注册、再 `manager.apply_patches()` 应用。**前提是部署侧 `VLLM_PLUGINS` 没有把这个插件排除掉**（u2-l1、u1-l4 讲过生产模板用 `VLLM_PLUGINS` 点名插件）。

**仿写模板：最简单的真实补丁。**
[components/omni-npu/src/omni_npu/vllm_patches/patches/common/patch_config_file_lock.py:17-36](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/vllm_patches/patches/common/patch_config_file_lock.py#L17-L36)：第 17 行 `_ORIGINAL_GET_CONFIG_PARSER = vllm_config.get_config_parser` 在替换前抓住原函数；第 34 行 `@register_patch("NPUConfigParserFileLockPatch", vllm_config)` 以**模块**为目标；第 36 行 `_attr_names_to_apply = ["get_config_parser"]` 只替换这一个符号。[patches/common/patch_config_file_lock.py:38-72](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/vllm_patches/patches/common/patch_config_file_lock.py#L38-L72) 的替换函数先调 `_ORIGINAL_GET_CONFIG_PARSER` 拿到原始返回值、在其外面包一层文件锁逻辑再返回——**「保存原实现 → 包装 → 返回」**就是补丁的标准形态。

#### 4.1.4 代码实践

**实践 A（推荐先做，无需 NPU、无需启动服务）：在容器里手工验证注册与应用机制。**

1. 实践目标：证明「注册 → 应用 → 符号被替换」这条链路，并亲眼看到两级去重。
2. 操作步骤：

   进入已部署的 P 节点容器（容器名以 u1-l4 的 inventory 为准）：

   ```bash
   docker exec -it <prefill容器名> bash
   ```

   执行下面这段脚本（**示例代码**，非项目原有代码，注意选用 `schedule` 符号——现有的 `SchedulerPatch` 已占用 `update_from_output`，撞符号会触发去重报错，这本身就是一个实验点）：

   ```python
   # 示例代码：验证 VLLMPatch 注册与应用机制
   import logging
   logging.basicConfig(level=logging.INFO)

   from vllm.v1.core.sched.scheduler import Scheduler
   from omni_npu.vllm_patches.core import VLLMPatch, register_patch
   from omni_npu.vllm_patches.patch_manager import PatchManager

   _ORIGINAL_SCHEDULE = Scheduler.schedule   # 步骤2：先抓住原实现

   @register_patch("MyTraceSchedulerPatch", Scheduler)   # 步骤3：注册
   class TraceSchedulerPatch(VLLMPatch):
       _attr_names_to_apply = ["schedule"]

       def schedule(self):
           logger = logging.getLogger("my-patch")
           logger.info("[my-patch] Scheduler.schedule called")
           return _ORIGINAL_SCHEDULE(self)

   print("registered:", list(PatchManager.registered_patches)[-1])
   print("before patch, qualname =", Scheduler.schedule.__qualname__)
   TraceSchedulerPatch.apply()                             # 手工应用
   print("after  patch, qualname =", Scheduler.schedule.__qualname__)
   TraceSchedulerPatch.apply()                             # 重复应用 → 只应告警
   ```

3. 需要观察的现象：
   - `registered: MyTraceSchedulerPatch`——装饰器在 import 时就完成注册；
   - `before` 的 qualname 是 vLLM 原类（形如 `Scheduler.schedule`），`after` 变成 `TraceSchedulerPatch.schedule`——符号已被 `setattr` 替换；
   - 第二次 `apply()` 触发 `already applied` 告警（第二级去重）。
4. 预期结果：替换成功且不抛异常。若报 `already patched by SchedulerPatch` 之类错误，说明你撞了已被占用的符号——换一个即可。本实践在纯 CPU 容器内即可跑通（import vLLM 不需要 NPU 设备），具体表现**待本地验证**。

**实践 B（完整链路）：让补丁随服务启动并只启用它。**

1. 实践目标：走完「落盘 → 自动注册 → 环境变量筛选 → 日志确认」全链路。
2. 操作步骤：
   1. 在容器内找到 omni-npu 源码路径（方法见 4.4.3），把上面示例保存为 `patches/common/patch_my_trace.py`；
   2. 在 ansible 模板 `run_vllm_server_prefill_cmd` 的命令中追加环境变量：
      `OMNI_NPU_VLLM_PATCHES=MyTraceSchedulerPatch`（临时实验也可以直接 `docker exec -e` 传入后重启脚本）；
   3. 重跑 `--tags run_server`，然后 `tail -f $LOG_PATH/server_0.log`。
3. 需要观察的现象：日志依次出现 `patch class TraceSchedulerPatch registered as MyTraceSchedulerPatch`、`patch applied: TraceSchedulerPatch => Scheduler.schedule`、`successfully applied patches: [..., 'MyTraceSchedulerPatch']`，随后每个调度步打印一条 `[my-patch] Scheduler.schedule called`。
4. 预期结果：名单里只有你的补丁和走默认链路必需的补丁——注意 `OMINI_NPU_VLLM_PATCHES` 设了名单就**只应用名单内的**，生产模板原本依赖的全量补丁会被关掉，所以实验后必须删掉文件或清掉该环境变量再重启。完整服务上的表现**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `_ORIGINAL_SCHEDULE = Scheduler.schedule` 必须写在补丁类定义之前？写在 `apply()` 之后会怎样？
答案：`register_patch`/`apply` 只是注册和替换，不保存原实现；若在替换之后再取 `Scheduler.schedule`，拿到的是自己的补丁函数，回调就变成无限递归。项目现有补丁（如 `patch_config_file_lock.py` 第 17 行）一律在模块顶部先抓住原符号。

**练习 2**：把补丁文件放进 `patches/models/pangu_v2_base/` 与放进 `patches/common/`，注册行为有何不同？
答案：`common/` 在 `auto_import_patches` 里无条件 import（必然注册）；`models/` 子目录要等 model_type 经过映射表/前缀/包含匹配命中该目录才 import。想让补丁只对某模型生效就放 models 目录，想全局生效就放 common。

**练习 3**：`OMINI_NPU_VLLM_PATCHES="A,B"` 中 A 不存在，服务会挂吗？
答案：不会。`apply_patch` 先查注册表，未注册只打 `patch ... not registered` 的 error 日志并返回（[patch_manager.py:24-26](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/vllm_patches/patch_manager.py#L24-L26)）；所以拼错名字不会报错终止，必须核对 `successfully applied patches` 名单。

### 4.2 配置扩展：为一个新模型登记最佳实践配置

#### 4.2.1 概念说明

u5-l1 讲过配置系统怎么**用**，本模块讲怎么**加**：当你接入一个新模型（或新规格）时，如何让 omni-npu 自动为它选中一份调优配置 json。这件事的官方文档就在 `model_config/README.md` 里，流程共四步，本质上是在维护两张查找表加一份数据文件：

- `match_hf_configs.json`：**架构超参指纹表**——把权重 `config.json` 里的一组超参（hidden_size、num_attention_heads 等）映射成一个「模型规格名」；
- `best_practice_configs.json`：**路由表**——按「模型规格 × 硬件 × 精度 × 部署形态」四元组路由到 P/D 两份配置 json 的路径；
- 配置 json 本体：两段式结构（`model_parallel_config` + `operator_optimization_config`），只覆盖 dataclass 默认值。

#### 4.2.2 核心流程

新模型的登记四步（对应 README 的 1-4 步）：

```text
第 1 步  查/登记指纹：在 match_hf_configs.json 加一条
         "my-model": { model_type, hidden_size, num_attention_heads, ... }
         ← key 即模型规格名，字段取自权重 config.json 且必须逐值相等
第 2 步  定场景：最优配置放 high_throughout/（默认）或 low_latency/
         （后者需 --additional-config 传 enable_low_latency=true）
         或用户自定义配置（不走路由，由 CUSTOM_MODEL_CONFIG_PATH 直指）
第 3 步  登记/新增路由：在对应目录 best_practice_configs.json 的
         configs 中加 "1P1D": {prefill_config_file, decode_config_file}
第 4 步  落盘配置 json：放到指定模型目录（如 openpangu_v2/）下
```

运行时的匹配逻辑（u5-l1 已讲，此处给出扩展者视角的要点）：

```text
load_model_extra_config
  ├─ parse_hf_config: 权重 config.json 的超参与指纹表逐条逐字段比对
  │    0 个命中 → 用 model_type 当名字（后续路由大概率落空 → 回退默认值）
  │    1 个命中 → 用该规格名
  │    多个命中 → 仅 deepseek_v3/v32 有特判，否则抛 RuntimeError
  ├─ CUSTOM_MODEL_CONFIG_PATH 已设 → 直接加载该 json，跳过路由（优先级最高）
  └─ 否则查 best_practice_configs.json：model × hardware × precision
       命中 → 取当前部署形态（P 或 D）的 json 加载
       未命中 → 告警并回退 dataclass 默认值
```

#### 4.2.3 源码精读

**官方流程文档。**
[components/omni-npu/src/omni_npu/model_config/README.md:24-31](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/model_config/README.md#L24-L31) 给出第一步：新模型必须先在 `match_hf_configs.json` 登记，登记内容是权重 config.json 上的架构属性，一级 key 与 `best_practice_configs.json` 的 `model` 字段一一对应。文档第 29-67 行给了 deepseek_v3 / qwen-235B / kimi-k2 三个完整示例。

[components/omni-npu/src/omni_npu/model_config/README.md:68-79](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/model_config/README.md#L68-L79) 给出第二步：最优配置分 `high_throughout` 与 `low_latency` 两类目录，后者需 `ADDITIONAL_CONFIG='{"enable_low_latency":true}'` 打开；用户自定义配置走 `CUSTOM_MODEL_CONFIG_PATH`，**给的是相对路径，必须在 configs 目录下**。[README.md:80-98](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/model_config/README.md#L80-L98) 给出第三步的路由 json 格式，并强调一条工程约束：**UT 会校验任意两个配置文件加载出的配置类对象不能完全一致**，冗余配置会被拦截。

**指纹匹配的实现。**
[components/omni-npu/src/omni_npu/model_config/config_loader/loader.py:244-276](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/model_config/config_loader/loader.py#L244-L276) 的 `parse_hf_config`：把 hf_config 转 dict 后与指纹表逐条比对，要求**每个字段都存在且相等**才算命中（第 256-260 行）；零命中回退 `model_type`（第 266-267 行），多命中除 deepseek 特判外抛错（第 268-274 行）。紧接着第 278-312 行从 `quantization_config` 推导 `quant_type`（如 `w8a8c16`），这是路由的第三维。

**覆盖通道的实现。**
[components/omni-npu/src/omni_npu/model_config/config_loader/loader.py:315-339](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/model_config/config_loader/loader.py#L315-L339) 的 `_init_model_extra_config`：`CUSTOM_MODEL_CONFIG_PATH` 非空时用 `os.path.join(default_config_path, 相对路径)` 直读 json（第 317-322 行）——这就是「相对路径必须在 configs 下」约束的来源；随后 json 的两个段分别 `filter_dict_by_dataclass` 过滤后构造 `ModelParallelConfig` 与 `ModelOperatorOptConfig`（第 330-334 行）。注意 `filter_dict_by_dataclass` 意味着**json 里拼错/多余的键会被静默丢弃**（u5-l1 踩过的坑）。

**路由查表。**
[components/omni-npu/src/omni_npu/model_config/config_loader/loader.py:358-369](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/model_config/config_loader/loader.py#L358-L369) 的 `_get_best_practice_config`：按 `enable_low_latency` 选目录，读对应 `best_practice_configs.json`，以 `model == model_name and hardware == hardware_platform and precision == quant_type` 三元组筛出 `configs_list`。而硬件名、PD 形态等任务事实由 [loader.py:27-80](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/model_config/config_loader/loader.py#L27-L80) 的 `load_model_extra_config` 采集（设备名映射 A2/A3/A5、`ROLE` 环境变量判 P/D、`PREFILL_POD_NUM` 等），最后 `_print_model_config` 打出 `ModelExtraConfig:` 回显——**验证配置生效以这行日志为准**。

**一份真实配置 json 的样子。**
[components/omni-npu/src/omni_npu/model_config/configs/high_throughput/openpangu_v2/openpangu_v2_92b_bf16_a3_1p1d_p.json:1-29](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/model_config/configs/high_throughout/openpangu_v2/openpangu_v2_92b_bf16_a3_1p1d_p.json#L1-L29)：两段式结构——`model_parallel_config` 含序列并行开关与逐层并行覆盖表，`operator_optimization_config` 含 `use_noncontiguous_kv`、`enable_moe_agrs` 等算子开关。文件名编码了「规格_精度_硬件_形态_角色」五要素，是命名约定的活样板。

#### 4.2.4 代码实践

**实践：为假想模型 `my-model` 登记配置并用 `CUSTOM_MODEL_CONFIG_PATH` 验证。**

1. 实践目标：不启动路由全链路，先用最高优先级通道验证「json → dataclass → 日志回显」这段装载链路，再补齐路由登记。
2. 操作步骤：
   1. 在容器内 omni-npu 源码的 `model_config/configs/` 下建目录 `my_model/`，放入 `my_model_bf16_a3_1p1d_p.json`（**示例代码**，结构仿照 4.2.3 的真实文件裁剪）：

      ```json
      {
          "model_parallel_config": {
              "ena_seq_parallel": false,
              "layer_parallel_config": {}
          },
          "operator_optimization_config": {
              "use_noncontiguous_kv": true,
              "router_gating_in_fp32": true
          }
      }
      ```

   2. 先做装载验证（无需 NPU 权重，`CUSTOM_MODEL_CONFIG_PATH` 是相对 configs 的路径）：

      ```bash
      CUSTOM_MODEL_CONFIG_PATH=my_model/my_model_bf16_a3_1p1d_p.json \
        python -c "from omni_npu.model_config.config_loader import loader; print('import ok')"
      ```

      完整验证需走 `load_model_extra_config(model_config, vllm_config, scheduler_config)`（需要真实 config 与设备），日常调试以服务启动日志为准。
   3. 补齐路由：在 `match_hf_configs.json` 顶部加一条指纹（key 用 `my-model`，字段抄你手头权重的 config.json，如 `{"model_type": "my_model", "hidden_size": ..., "num_attention_heads": ..., "vocab_size": ...}`）；再在 `high_throughout/best_practice_configs.json` 中新增 `{"model": "my-model", "hardware": "A3", "precision": "bf16", "configs": {"1P1D": {"prefill_config_file": "my_model/my_model_bf16_a3_1p1d_p.json", "decode_config_file": "my_model/my_model_bf16_a3_1p1d_d.json"}}}`。
   4. 若 json 里的键与 dataclass 字段不一致（故意拼错一个，如 `use_noncontiguous_kv_typo`），重复步骤 2/3 观察。
3. 需要观察的现象：
   - 设置 `CUSTOM_MODEL_CONFIG_PATH` 后日志出现 `Get custom_model_config_path from environ: my_model/...`；
   - 启动日志的 `ModelExtraConfig:` 回显中 `router_gating_in_fp32` 等值与 json 一致；
   - 拼错键时**没有报错**、该键对应的默认值原样出现在回显里（静默丢弃）。
4. 预期结果：回显与 json 一致即装载成功；路由链路（指纹 → 路由表）的完整命中需要在真实权重上启动验证，**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：指纹表里只登记了 `hidden_size` 一个字段，会发生什么？
答案：所有 hidden_size 相同的模型都会命中该条目，容易触发「多个匹配」的 RuntimeError（`parse_hf_config` 第 268-274 行，deepseek 除外）。指纹字段要选到能把规格唯一区分开的组合，参考现有条目一般列 8-10 个字段。

**练习 2**：`CUSTOM_MODEL_CONFIG_PATH` 里写绝对路径 `/root/my.json` 会怎样？
答案：加载会失败——代码用 `os.path.join(default_config_path, custom_model_config_path)` 拼的是 configs 目录下的相对位置；文件不在 configs 下就找不到。自定义文件必须放进 `model_config/configs/` 树内再给相对路径。

**练习 3**：为什么 README 强调「两个配置文件加载出的配置类对象不能一致，否则 UT 拦截」？
答案：路由表的多个条目指向内容完全相同的 json 时，文件本身没有存在价值（直接复用一个即可），冗余配置会随维护漂移。这条 UT 约束是在用测试守卫配置集的**最小性**。具体校验用例可在 `components/omni-npu/tests/` 下检索 model_config 相关测试查阅（待确认具体用例文件）。

### 4.3 connector 扩展点：KVConnectorFactory 注册与 omni.kv_connectors

#### 4.3.1 概念说明

PD 分离部署时，`kv-transfer-config` JSON 里的 `kv_connector` 字段只填一个**名字**（如 `"LLMDataDistConnector"`），vLLM 拿名字到 `KVConnectorFactory` 的注册表里查出「模块路径 + 类名」再动态 import。这个设计把「传输 KV 的实现」做成了可插拔点：

- **内置注册**：omni-npu 在平台初始化时把自带的 `LLMDataDistConnector` 注册进去；
- **第三方注册**：任何 pip 包都可以在自己的 `pyproject.toml` 里注册 `omni.kv_connectors` entry point，omni-npu 会遍历该组并调用每个入口函数，由它完成自定义 connector 的注册——**不用改 omni-npu 一行代码**。

这与 u2-l2 讲过的 `NPU_ATTENTION_BACKEND` 注意力后端注册表是同一个模式：名字 → 路径字符串 → entry point 可覆盖。

#### 4.3.2 核心流程

connector 从「名字」到「对象」的完整链路：

```text
vllm serve ... --kv-transfer-config '{"kv_connector": "MyConnector", ...}'
  ↓ 启动早期
NPUPlatform.import_kernels()
  ├─ patch_compile_decorators()              # 编译装饰器（u5-l2 线索）
  ├─ register_connectors()                   # 内置：LLMDataDistConnector 注册进工厂
  └─ for ep in entry_points(group="omni.kv_connectors"):
         ep.load()()                         # 第三方：每个入口是一个注册函数
  ↓ 引擎按 kv_role（producer/consumer）实例化
KVConnectorFactory 查表 → import 模块 → 取类 → MyConnector(vllm_config, role, kv_cache_config)
```

写一个新 connector 需要两件事：实现 `KVConnectorBase_V1` 的子类（P/D 两侧 scheduler/worker 四类协作的参考架构见 u4-l2），以及把「名字 → 模块:类名」登记进工厂。

#### 4.3.3 源码精读

**防御式注册函数。**
[components/omni-npu/src/omni_npu/connector/register.py:9-34](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/connector/register.py#L9-L34) 的 `_safe_register`：先探测工厂的注册表字典（`_registry` 或 `_connectors`，兼容 vLLM 不同版本的字段名），名字已存在就跳过（幂等），否则调用 `KVConnectorFactory.register_connector(name, module, class_name)` 并打 `connector: registered KV connector: ... -> ...` 日志——注意注册的是**字符串路径**，不是类本身，import 被推迟到实例化时。

**内置注册入口。**
[components/omni-npu/src/omni_npu/connector/register.py:37-47](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/connector/register.py#L37-L47) 的 `register_connectors` 目前只注册一条：`"LLMDataDistConnector" -> omni_npu.connector.llmdatadist_connector_v1.LLMDataDistConnector`——这正是 u4-l1 强调「connector 名必须与注册表逐字符一致」的注册侧源头。新内置 connector 在这里加一行 `_safe_register` 即可。

**第三方扩展点的消费侧。**
[components/omni-npu/src/omni_npu/platform.py:82-95](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/platform.py#L82-L95) 的 `import_kernels`：先做编译装饰器注入与内置 connector 注册（第 84-88 行），然后**遍历 `omni.kv_connectors` entry point 组**，逐个 `ep.load()` 拿到注册函数并调用（第 90-95 行），单个第三方 connector 加载失败只告警不阻断。omni-npu 自己的 [pyproject.toml:33-38](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/pyproject.toml#L33-L38) 并没有注册这一组——内置走硬编码，该组专留给第三方包（omni-cache 的 `OmniCacheConnector` 即以独立组件形态接入，见 u7-l2）。

**内置 connector 的门面分派——新 connector 的参考架构。**
[components/omni-npu/src/omni_npu/connector/llmdatadist_connector_v1.py:131-168](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/connector/llmdatadist_connector_v1.py#L131-L168)：`LLMDataDistConnector` 是 `KVConnectorBase_V1` 的门面类，构造函数里按 `role`（SCHEDULER/WORKER）× `is_prefill`（由 `kv_role` 推出，第 154 行）两个正交维度把职责分派给四个协作类（第 157-168 行），自身的方法（如第 174-179 行的 `get_num_new_matched_tokens`）只是转发。自研 connector 时沿用这个「门面 + 四协作类」骨架，就能同时满足 vLLM 在 scheduler 进程与 worker 进程两侧的调用。

#### 4.3.4 代码实践

**实践：验证注册表内容，并推演自定义 connector 的接入路径。**

1. 实践目标：确认内置注册真的发生、看清「名字 → 字符串路径」的表内容，并写出第三方接入的注册函数骨架。
2. 操作步骤：
   1. 在容器内执行（**示例代码**）：

      ```bash
      docker exec -it <prefill容器名> python -c "
      from omni_npu.connector.register import register_connectors
      register_connectors()
      from vllm.distributed.kv_transfer.kv_connector.factory import KVConnectorFactory
      reg = getattr(KVConnectorFactory, '_registry', None) or getattr(KVConnectorFactory, '_connectors', {})
      for name, val in reg.items():
          print(name, '->', val)
      "
      ```

   2. 再执行一次步骤 1，观察重复注册时的日志（幂等性验证）。
   3. 在纸上写出第三方包的接入两件套（**示例代码**）：

      ```python
      # 第三方包内：my_connector/__init__.py
      def register():
          from vllm.distributed.kv_transfer.kv_connector.factory import KVConnectorFactory
          KVConnectorFactory.register_connector(
              "MyConnector", "my_connector.my_impl", "MyConnectorImpl")
      ```

      ```toml
      # 第三方包的 pyproject.toml
      [project.entry-points."omni.kv_connectors"]
      my_connector = "my_connector:register"
      ```

3. 需要观察的现象：步骤 1 打印 `LLMDataDistConnector -> ...`（值形态可能是模块/类名字符串对，以容器内实际输出为准）；步骤 2 第二次运行出现 `already present in KVConnectorFactory registry, skip` 且不报错。
4. 预期结果：注册表可查询、重复注册被跳过。步骤 1/2 在容器内即可完成；`register_connector` 的确切签名随 vLLM 版本可能不同，以容器内 `vllm` 0.14 的实际代码为准，**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `_safe_register` 注册的是「模块路径 + 类名」字符串，而不是直接 import 类再注册？
答案：延迟导入。注册发生在平台初始化早期，若此时就 import connector 实现，会连带拉起其全部依赖（llm_datadist、ZMQ 等）；注册字符串把成本推迟到真正实例化该 connector 的进程（scheduler/worker）里，也让「注册了但本次部署不用」成为零开销。

**练习 2**：第三方 connector 加载失败（entry point 的 `ep.load()` 抛异常），服务还能起吗？
答案：能。[platform.py:90-95](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/platform.py#L90-L95) 对每个入口 `try/except` 只打 warning；只有当部署确实用 `kv_connector` 指定了该名字而工厂查不到时，才会在实例化阶段失败。

**练习 3**：`kv-transfer-config` 里把名字写成 `llmdatadistconnector`（小写），会发生什么？
答案：注册表按名字精确匹配，查不到该名字，实例化阶段报未知 connector。这就是 u4-l1 强调「名字必须与注册表逐字符一致」的原因——大小写也算差异。

### 4.4 装饰器扩展点与容器内开发工作流

#### 4.4.1 概念说明

前三个模块覆盖的扩展点分别作用于「vLLM 符号替换」「模型配置」「KV 传输」，还有一个更细粒度的扩展点：**模型前向关键路径上的 pre/post 钩子**。`plugin_decorators.py` 提供一组装饰器，把 omni-npu 内部函数（模型加载、输入准备、注意力、前向、输出回收）变成可被外部插件挂钩的缝隙——外部包只需在约定的 entry point 组里注册一个带 `pre_xxx`/`post_xxx` 方法的类，就能在不动 omni-npu 源码的前提下观察或改写这些阶段。它与补丁机制互补：补丁改 **vLLM** 的行为，装饰器挂 **omni-npu 自己**的行为。

本模块同时收拢「进容器改代码」的工作流——四类扩展点最终都要落到「在容器里改哪、怎么生效」。

#### 4.4.2 核心流程

装饰器的执行模型：

```text
@某装饰器
def 被装饰函数(*args, **kwargs):
    ...

运行时实际执行 wrapper：
  1. 加载 entry point 组里的全部插件类，逐个实例化
  2. pre 钩子：依次调用 plugin.pre_xxx(*args, **kwargs)
       返回 dict → update 进 kwargs（改写入参）
  3. ret = 原函数(*args, **kwargs)
  4. post 钩子：依次调用 plugin.post_xxx(*args, result=ret, **kwargs)
  5. 返回 ret

conditional 变体（init_config 专用）：
  pre 钩子返回 True → 跳过原函数（整体替换语义）
```

容器内开发工作流：

```text
1. docker exec 进入对应容器（P/D/C 各自的容器）
2. pip list | grep omni-npu                 # 确认安装与版本
3. 定位源码目录（构建时 pip install -e 安装，源码在容器内）
4. 修改源码 / 新增 patch 文件 / 增加配置 json
5. 重启该节点的服务脚本（重跑 --tags run_server）使 Python 进程重新加载
6. 以日志回显验证（patch 名单 / ModelExtraConfig / connector 注册行）
```

#### 4.4.3 源码精读

**装饰器工厂。**
[components/omni-npu/src/omni_npu/plugin_decorators.py:14-61](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/plugin_decorators.py#L14-L61) 的 `create_plugin_decorator`：装饰器应用时（import 时）就加载并缓存 entry point 组（第 28-31 行，`_cached_eps` 避免重复查询）；wrapper 里先跑 pre 钩子、返回 dict 的结果合并进 kwargs（第 46-50 行），再调原函数，最后跑 post 钩子（第 52-57 行）。插件类在**每次调用时实例化**（第 43 行），所以插件可以用带状态的实例。

**条件跳过变体。**
[components/omni-npu/src/omni_npu/plugin_decorators.py:64-141](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/plugin_decorators.py#L64-L141) 的 `create_conditional_plugin_decorator`：pre 钩子返回 `True` 则跳过原函数（第 111-126 行），实现「插件完全接管」语义；docstring 第 81-91 行给出插件类写法示例。钩子内抛异常会记日志后 `raise`（第 117-119 行），不做静默吞掉——因为改写配置/输入的钩子半途失败比直接报错更危险。

**预定义的八个挂点。**
[components/omni-npu/src/omni_npu/plugin_decorators.py:144-168](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/plugin_decorators.py#L144-L168) 与 [plugin_decorators.py:201-217](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/plugin_decorators.py#L201-L217)：`load_model`、`init_config`（conditional）、`prepare_inputs`、`reinitialize_input_batch`、`model_forward`、`update_from_output`、`model_output` 七个固定挂点，各自对应 `omni.<名>_decorators` entry point 组；[plugin_decorators.py:171-197](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/plugin_decorators.py#L171-L197) 的 `attn_decorator(type)` 则按注意力类型动态生成组名 `omni.{type}_attn_decorators`（dsa/mla/mome 等），统一挂 `pre_attn`/`post_attn`。第三方包在这些组里注册 entry point 即可挂钩。

**容器内改码入口。**
[README.md:118-125](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/README.md#L118-L125)「推理代码适配」一节：需要修改推理代码时，用 `pip list | grep omni-npu` 查看组件在 docker 内的安装情况，进入对应 docker 修改。镜像构建脚本 [tools/docker/build_whl.sh:56-72](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/docker/build_whl.sh#L56-L72) 显示：构建时 omniinfer 源码会被复制/克隆进容器并执行 `bash build/build.sh -m <modules>`——而 u1-l2 已确认该脚本对 omni-npu 最终执行 `pip install -e .`（可编辑安装），所以**容器内存有 omni-npu 源码目录，直接改源码、重启服务进程即可生效**，不必重打镜像。这是本讲四类扩展点共用的落地通道。

#### 4.4.4 代码实践

**实践：用装饰器挂点给模型前向加观测日志。**

1. 实践目标：体验「不改 omni-npu 源码」的挂点扩展：注册一个插件类，在 `post_model_forward` 钩子里打日志。
2. 操作步骤：
   1. 在容器内新建最小插件包（**示例代码**）：

      ```python
      # /tmp/my_fwd_plugin/my_plugin.py
      class FwdObserver:
          def post_model_forward(self, *args, result=None, **kwargs):
              print("[my-plugin] model forward done, result type:", type(result))
      ```

      ```toml
      # /tmp/my_fwd_plugin/pyproject.toml
      [project]
      name = "my-fwd-plugin"
      version = "0.0.1"

      [project.entry-points."omni.model_forward_decorators"]
      fwd_observer = "my_plugin:FwdObserver"
      ```

   2. 安装并验证 entry point 可被发现：

      ```bash
      cd /tmp/my_fwd_plugin && pip install --no-deps .
      python -c "from importlib.metadata import entry_points; \
        print([ep.name for ep in entry_points().select(group='omni.model_forward_decorators')])"
      ```

   3. 重启服务（重跑 `--tags run_server`），观察引擎日志。
3. 需要观察的现象：步骤 2 打印 `['fwd_observer']`；步骤 3 中每次模型前向后出现 `[my-plugin] model forward done ...` 日志。
4. 预期结果：钩子被调用即链路通。注意插件在**每次前向都会实例化并执行**，生产上挂高成本钩子会拖慢 decode；真实服务上的日志位置与频率**待本地验证**。实验完 `pip uninstall my-fwd-plugin` 清理。

#### 4.4.5 小练习与答案

**练习 1**：`create_plugin_decorator` 与 `create_conditional_plugin_decorator` 的本质区别是什么？各自适合什么场景？
答案：普通版钩子只能「观察/改入参」，原函数一定执行；conditional 版的 pre 钩子返回 True 可以**整体跳过原函数**，适合「某些条件下用完全不同的实现替换」的场景（如 `init_config` 被插件接管）。

**练习 2**：插件 pre 钩子返回一个 dict，会发生什么？
答案：`wrapper` 把它 `update` 进 kwargs（[plugin_decorators.py:49-50](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/plugin_decorators.py#L49-L50)），随后原函数以改写后的关键字参数执行——这是插件改写行为的官方通道；返回其他类型则被忽略。

**练习 3**：改完容器内 omni-npu 源码后，为什么必须重启服务而不是立即生效？
答案：omni-npu 是以可编辑安装（`pip install -e .`）进容器的 Python 包，源码改动只影响「之后 import 的模块」；运行中的服务进程已把模块加载进内存，重跑 `--tags run_server` 让 Python 进程重新 import 才能加载新代码。

## 5. 综合实践

**任务：给一套已部署的 1P1D 服务做一次「三扩展点」联合演练——为排障加一个调度日志补丁、为实验钉死一份自定义配置、并盘点 connector 注册链路。**

按以下顺序完成（三类扩展点各占一步，互不干扰、可独立回滚）：

1. **补丁**：按 4.1.4 实践 B 在 P 节点容器内新增 `MyTraceSchedulerPatch`，在 decode 侧命令同样传入 `OMINI_NPU_VLLM_PATCHES=MyTraceSchedulerPatch` 观察两侧行为差异（P 侧重 prefill 批调度、D 侧重逐 token 步进），从 `successfully applied patches` 名单确认；完成后删除文件与环境变量。
2. **配置**：把 D 节点当前命中的最佳实践 json 复制为 `my_model/custom_d.json`，改一个无害开关（如 `router_gating_in_fp32`），用 `CUSTOM_MODEL_CONFIG_PATH` 指过去重启，对照 `ModelExtraConfig:` 回显确认覆盖，再用一条固定 prompt 粗测首 token 延迟变化；完成后取消环境变量。
3. **connector**：在 P、D 两侧容器分别执行 4.3.4 步骤 1 的注册表打印，确认两侧都有 `LLMDataDistConnector`；再从 `server_0.log` 里找出 `connector: registered KV connector` 日志行，标注它出现在启动流程的哪个阶段（平台初始化 `import_kernels` 期间）。
4. **收尾**：写一份一页纸记录——三个扩展点各自的「改动文件 / 生效开关 / 验证日志行 / 回滚动作」四栏对照表。这张表就是你后续在这个仓库做二次开发的操作手册。

预期结果：三步全部以日志为证据链完成；任何一步异常都能按表回滚到基线。整体效果**待本地验证**。

## 6. 本讲小结

- **补丁扩展**：新补丁 = 继承 `VLLMPatch` + `@register_patch(名字, 目标)` + 模块级保存原实现；文件落进 `patches/common/`（必注册）或 `patches/models/<模型>/`（按 model_type 匹配注册），`OMINI_NPU_VLLM_PATCHES` 决定应用名单，两级去重防重复与撞符号，单个失败不阻断启动，验证以 `successfully applied patches` 日志为准。
- **配置扩展**：新模型登记四步——指纹表（`match_hf_configs.json`，字段逐值相等才命中）→ 定场景目录（`high_throughout`/`low_latency`）→ 路由表（`best_practice_configs.json`，模型×硬件×精度）→ 落盘两段式 json；`CUSTOM_MODEL_CONFIG_PATH` 是优先级最高的覆盖通道但只接受 configs 下的相对路径；拼错的键被静默丢弃，最终以 `ModelExtraConfig:` 回显为准。
- **connector 扩展点**：`kv_connector` 名字经 `KVConnectorFactory` 注册表（名字 → 模块:类名字符串，延迟 import）解析；内置 `LLMDataDistConnector` 由 `register_connectors` 防御式注册，第三方包经 `omni.kv_connectors` entry point 注入注册函数；门面 + 四协作类是自研 connector 的参考架构。
- **装饰器扩展点**：`plugin_decorators.py` 在模型加载/输入准备/注意力/前向/输出等八个挂点提供 pre/post 钩子，pre 返回 dict 可改入参、conditional 版返回 True 可整体替换原函数；第三方在 `omni.*_decorators` entry point 组注册插件类即可挂钩，不动 omni-npu 源码。
- **开发工作流**：镜像内 omni-npu 源码以 `pip install -e .` 可编辑安装（`build_whl.sh` → `build/build.sh` 链路），`docker exec` 进容器直接改源码，重跑 `--tags run_server` 重新加载；四类扩展点共用这条通道。
- **方法论**：选择扩展点时先问「要改的是谁的行为」——改 vLLM 用补丁、改模型调优用配置 json、换 KV 传输用 connector、观察/改写 omni-npu 自身关键路径用装饰器；一切验证以启动日志回显为证据。

## 7. 下一步学习建议

- **下一讲 u10-l4（生产综合实战）**：把本讲的三类扩展点与量化（u8）、OmniCache（u7）、分组调度（u6）组合进 505B 大规模拓扑，检验你在真实约束下挑选扩展点的能力。
- **源码延伸阅读 1**：通读 [patches/common/](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/vllm_patches/patches/common/) 下 29 个补丁的 `_attr_names_to_apply` 与目标符号，整理一张「vLLM 符号 → NPU 侧动机」清单，你会得到一份比文档更准的 vLLM 适配面地图。
- **源码延伸阅读 2**：对照 [components/omni-cache/omni_cache/connector/](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-cache/omni_cache/connector/) 的目录结构（prefill/decode/scheduler 分目录），体会 4.3 的门面架构在真实第三方 connector 里的展开方式。
- **动手延伸**：按 u10-l2 的测试方法，为你新增的补丁仿照 [tests/unit/vllm_patches/](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/tests/unit/vllm_patches/) 下现有用例补一个最小单测，让二次开发成果可回归。
