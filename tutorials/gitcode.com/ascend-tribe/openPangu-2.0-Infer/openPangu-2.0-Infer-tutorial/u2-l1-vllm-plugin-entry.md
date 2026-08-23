# vLLM 插件体系与 omni-npu 的三个入口

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 `vllm.platform_plugins` 与 `vllm.general_plugins` 两个 entry point 组各自的职责差异。
2. 读懂 omni-npu 在 `pyproject.toml` 中注册的**三个入口**（一个平台插件 + 两个通用插件），并能说出每个入口被调用后分别发生了什么。
3. 逐行解读 `vllm_plugin.py` 中的 `plugin()` 函数：它如何探测 `torch_npu`、为什么返回的是「类路径字符串」而不是类对象、为什么探测失败要返回 `None`。
4. 解释部署模板里 `export VLLM_PLUGINS="omni-npu,omni_npu_patches,omni_custom_models"` 这一行与插件加载的关系。
5. 在已部署容器内用 `importlib.metadata` 亲手打印 omni-npu 注册的全部入口，并验证 `plugin()` 的返回值。

## 2. 前置知识

### 2.1 什么是「插件（plugin）」与「零侵入（out-of-tree）」

把 vLLM 想象成一台通用推理主机，它出厂时自带 CUDA（NVIDIA GPU）支持。要让同一台主机跑在华为昇腾 NPU 上，有两种做法：

- **改源码（in-tree）**：直接修改 vLLM 仓库，加入 NPU 分支。代价是每次 vLLM 升级都要重新合并代码，维护成本极高。
- **做插件（out-of-tree）**：vLLM 预留「插座」，第三方包只要按约定的格式注册，vLLM 启动时会自动发现并加载，**vLLM 一行代码都不用改**。

omni-npu 走的是第二条路。omni-npu 的 README 开头三行就把这个定位说清楚了：

> A vLLM (0.14.0) out-of-tree platform plugin that enables running vLLM on NPU (Ascend/torch_npu).
> - Loaded via vLLM plugin entry points (no code changes to vLLM required).

参见 [components/omni-npu/README.md:L1-L7](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/README.md#L1-L7)，这段话是整个组件存在的意义。

### 2.2 什么是 entry points（入口点）

entry points 是 Python 打包标准（PEP 621）里的一个机制：包作者在 `pyproject.toml` 里声明「我的包在某个**组（group）**下提供一个叫某名字的**可调用对象**」，安装工具（pip）会把这些声明写进 `site-packages` 里的包元数据（`*.dist-info` 目录）。之后**任何第三方程序**都能通过 `importlib.metadata.entry_points(group="组名")` 查到这些声明，无需 import 你的包。

一个 entry point 的值写成 `模块路径:属性名` 的字符串形式，例如：

```
omni_npu.vllm_plugin:plugin
```

表示「import 模块 `omni_npu.vllm_plugin`，然后取它的 `plugin` 属性」。它的解析过程可以形式化为：

\[ \mathrm{load}(\text{ep}) = \mathrm{getattr}\big(\mathrm{import\_module}(\text{ep.module}),\ \text{ep.attr}\big) \]

关键点：**发现靠元数据（不触发 import），调用靠字符串解析（延迟 import）**。这让 vLLM 可以先「扫描」所有插件再决定加载谁。

### 2.3 承接上一讲：插件是怎么装进容器的

u1-l2 讲过，构建流水线最终对 omni-npu 执行的是 `pip install -e .`（可编辑安装）。这条命令除了把源码挂进 Python 环境，还做了一件对本讲至关重要的事：**把 `pyproject.toml` 里的 entry points 声明写进包元数据**。没有这一步，vLLM 永远发现不了 omni-npu。

镜像构建脚本也能佐证「vLLM 本体不带任何设备后端、设备能力全靠插件」这一点：vLLM 是以 `VLLM_TARGET_DEVICE=empty`（空设备后端）方式从源码安装的，见 [tools/docker/build_whl.sh:L44-L46](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/docker/build_whl.sh#L44-L46)；随后同一脚本调用 `bash build/build.sh -m omni-npu` 把插件装进去，见 [tools/docker/build_whl.sh:L72](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/docker/build_whl.sh#L72)。

> 版本备注：镜像构建脚本默认 `VLLM_VERSION` 为 `v0.12.0`（可用参数覆盖），而 omni-npu README 标称兼容 `vllm==0.14.0`。以你实际镜像内的 `pip show vllm` 为准。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| [components/omni-npu/pyproject.toml](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/pyproject.toml#L33-L38) | omni-npu 的打包描述 | 三个 entry point 的声明处，本讲「地图的原点」 |
| [components/omni-npu/src/omni_npu/vllm_plugin.py](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/vllm_plugin.py#L4-L26) | 平台插件探测函数 | `plugin()` 如何判断「我在不在 NPU 环境里」 |
| [components/omni-npu/src/omni_npu/platform.py](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/platform.py#L52-L64) | `NPUPlatform` 平台类 | `plugin()` 返回值指向的最终目标（本讲只看类头，精读留给 u2-l2） |
| [components/omni-npu/src/omni_npu/vllm_patches/\_\_init\_\_.py](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/vllm_patches/__init__.py#L217-L224) | 第二个入口 `apply_patches` | 补丁入口被调用时做了什么（机制细节留给 u2-l4） |
| [components/omni-npu/src/omni_npu/v1/models/\_\_init\_\_.py](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/v1/models/__init__.py#L7-L22) | 第三个入口 `register_models` | 向 vLLM 注册 openPangu 模型结构 |
| [components/omni-npu/README.md](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/README.md#L31-L36) | 组件自述 | "How it works" 一节对插件链路的官方描述 |
| [tools/ansible/92B/omni_infer_server_template_performance1P1D_92B_bf16_open.yml](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_server_template_performance1P1D_92B_bf16_open.yml#L74-L86) | 1P1D 部署模板 | 部署侧如何用环境变量「点名」加载这三个插件 |

先给一张总览表——omni-npu 的**三个入口**：

| # | entry point 组 | 名字 | 值（模块:属性） | 被调用后做什么 |
| --- | --- | --- | --- | --- |
| 1 | `vllm.platform_plugins` | `omni-npu` | `omni_npu.vllm_plugin:plugin` | 探测 NPU 环境，返回平台类路径 `omni_npu.platform.NPUPlatform` |
| 2 | `vllm.general_plugins` | `omni_npu_patches` | `omni_npu.vllm_patches:apply_patches` | 扫描并应用运行时补丁（monkey patch） |
| 3 | `vllm.general_plugins` | `omni_custom_models` | `omni_npu.v1.models:register_models` | 向 vLLM 注册 openPangu V2 模型与 MTP 模型 |

前两个组名是 vLLM 约定的「插座规格」：`platform_plugins` 组里的函数负责**回答「我是谁的设备平台」**，`general_plugins` 组里的函数则是**通用的启动期钩子**，vLLM 启动时会逐个调用它们，做什么完全由插件自己决定。

## 4. 核心概念与源码讲解

### 4.1 vLLM 平台插件接口

#### 4.1.1 概念说明

「平台插件」解决的问题是：vLLM 内部到处都在问「我在什么硬件上？」——该用哪个 device、哪个分布式通信后端、哪个 worker 类、注意力用哪个后端。vLLM 把这些问题的答案抽象成一个 **Platform 接口类**（vLLM 侧的 `vllm.platforms.interface.Platform`），并规定：启动时先在 `vllm.platform_plugins` 组里逐个调用入口函数，**谁返回了非 `None` 的平台类路径，就用谁的平台**。

因此平台插件本质上是一份「设备声明书」+「配置改写器」：

- 声明书：设备名、dispatch key、通信后端、可见设备的环境变量名等；
- 配置改写器：在 vLLM 解析完命令行参数后，把默认值改写成 NPU 合适的值（如 worker 类、block size、注意力后端）。

omni-npu README 的 "How it works" 一节概括了这条链路：插件注册在 `vllm.platform_plugins` 组下 → vLLM 在 `torch_npu` 可用时发现 `omni_npu.platform.NPUPlatform` → 平台类设置 `device_type=npu`、配置 worker 类并使用 HCCL 通信。见 [components/omni-npu/README.md:L31-L36](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/README.md#L31-L36)。

#### 4.1.2 核心流程

从 vLLM 进程启动到 NPUPlatform 生效，简化时序如下（vLLM 侧细节本仓库不含源码，以官方 vLLM 行为为准）：

```
vllm 进程启动
  │
  ├─ ① 扫描 entry point 组 vllm.platform_plugins（只读元数据，不 import 插件）
  │
  ├─ ② 逐个调用入口函数（这里是 omni_npu.vllm_plugin.plugin）
  │       ├─ 返回 None    → 跳过，尝试下一个平台插件 / 回落到默认 CUDA 平台
  │       └─ 返回类路径字符串 → ③ import 该模块、实例化平台类
  │
  ├─ ④ vLLM 解析 CLI 参数 → 调用平台的 pre_register_and_update / check_and_update_config
  │       （NPU 在这里把 worker_cls 改写为 omni_npu.worker.npu_worker.NPUWorker）
  │
  └─ ⑤ 引擎初始化 → 调用平台的 import_kernels 等钩子 → 进入正常推理流程
```

注意 ③：`plugin()` 返回的是**字符串**路径而不是类对象，vLLM 负责后续 import。这是一个刻意的解耦设计，下一模块详述。

#### 4.1.3 源码精读

先看「声明书」本体——`NPUPlatform` 的类头（本讲只看声明，方法精读在 u2-l2）：

[components/omni-npu/src/omni_npu/platform.py:L52-L64](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/platform.py#L52-L64)

```python
class NPUPlatform(Platform):
    try:
        # In case vllm already defined HUAWEI_NPU platform
        _enum = PlatformEnum.HUAWEI_NPU
    except AttributeError:
        # fallback to OOT
        _enum = PlatformEnum.OOT
    device_name: str = "npu"
    device_type: str = "npu"
    dispatch_key: str = "PrivateUse1"
    ray_device_key: str = "NPU"
    dist_backend: str = "hccl"
    device_control_env_var: str = "ASCEND_RT_VISIBLE_DEVICES"
```

这段代码向 vLLM 声明了五件事：

| 声明项 | 值 | 含义 |
| --- | --- | --- |
| `device_type` | `"npu"` | vLLM 全局设备类型变为 npu（README 用法中的 `--device npu` 即对应它） |
| `dispatch_key` | `"PrivateUse1"` | torch 的自定义设备后端分发键，torch_npu 走 PrivateUse1 通道 |
| `dist_backend` | `"hccl"` | 多卡/多机集合通信用 HCCL（对标 GPU 的 NCCL） |
| `device_control_env_var` | `"ASCEND_RT_VISIBLE_DEVICES"` | 「哪几张卡可见」由这个环境变量控制（对标 `CUDA_VISIBLE_DEVICES`） |
| `_enum` | `HUAWEI_NPU` 或 `OOT` | 若 vLLM 内置了华为 NPU 枚举就用它，否则用 OOT（out-of-tree），兼容不同 vLLM 版本 |

「配置改写器」的入口在 `check_and_update_config`，其中最关键的一行是把 vLLM 默认的 worker 类替换为 NPU 版：

[components/omni-npu/src/omni_npu/platform.py:L143-L148](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/platform.py#L143-L148)

```python
@classmethod
def check_and_update_config(cls, vllm_config: "VllmConfig") -> None:
    ConfigUpdater.update_vllm_config(vllm_config)
    parallel_config = vllm_config.parallel_config
    parallel_config.worker_cls = "omni_npu.worker.npu_worker.NPUWorker"
```

同样值得注意的是 `import_kernels` 钩子——它在引擎初始化时被调用，除了注册 NPU 自定义算子，还**用同样的 entry points 机制加载自己的扩展**（`omni.kv_connectors` 组，KV 传输连接器的注册口，u10-l3 会展开）：

[components/omni-npu/src/omni_npu/platform.py:L83-L95](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/platform.py#L83-L95)

```python
@classmethod
def import_kernels(cls):
    from omni_npu.compilation.decorators import patch_compile_decorators
    patch_compile_decorators()
    from omni_npu.connector import register_connectors
    register_connectors()
    for ep in entry_points().select(group="omni.kv_connectors"):
        try:
            register_fn = ep.load()
            register_fn()
        except Exception as e:
            logger.warning(f"Failed to load connector {ep.name}: {e}")
```

这说明 entry points 不只是「接入 vLLM」的手段，也是 omni-npu **自身做扩展**的通用模式——学会了本讲，你同时也看懂了它自己插座的用法。

#### 4.1.4 代码实践

**实践目标**：验证「平台插件真的装进了容器、且能被 Python 找到」，并直观感受平台类声明的信息。

**操作步骤**（承接 u1-l4 部署的环境，容器名 `docker_p` 定义于 [tools/ansible/92B/omni_infer_server_template_performance1P1D_92B_bf16_open.yml:L22-L24](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_server_template_performance1P1D_92B_bf16_open.yml#L22-L24)）：

1. 在 P 节点宿主机上进入容器：
   ```bash
   docker exec -it docker_p bash
   ```
2. 这也是 omni-npu README 故障排查建议的第一步——确认两个包都在：
   ```bash
   pip show vllm omni-npu
   ```
3. 不启动服务，直接 import 平台类并打印声明信息（示例代码）：
   ```bash
   python3 -c "
   from omni_npu.platform import NPUPlatform
   print('device_type   =', NPUPlatform.device_type)
   print('dist_backend  =', NPUPlatform.dist_backend)
   print('dispatch_key  =', NPUPlatform.dispatch_key)
   print('device_env_var=', NPUPlatform.device_control_env_var)
   "
   ```
   （worker 类的改写需要走完整 vLLM 配置流程才能观察，这里先只看四项静态声明。）

**需要观察的现象**：

- `pip show` 列出 `omni-npu` 版本 0.2.0（见 [components/omni-npu/pyproject.toml:L5-L7](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/pyproject.toml#L5-L7)），且 `Location` 指向实际安装路径；可编辑安装时应指向源码目录。
- 打印出的四项与 4.1.3 表格一致：`npu` / `hccl` / `PrivateUse1` / `ASCEND_RT_VISIBLE_DEVICES`。

**预期结果**：四项声明值与源码完全一致。若 `pip show omni-npu` 找不到包，说明插件没装进该镜像——这正是 README "Troubleshooting" 里 "Plugin not detected" 的场景（参见 [components/omni-npu/README.md:L62-L66](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/README.md#L62-L66)）。完整运行结果**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：`NPUPlatform` 为什么要写 `try: _enum = PlatformEnum.HUAWEI_NPU except AttributeError: _enum = PlatformEnum.OOT` 这段看似奇怪的代码？

**参考答案**：不同 vLLM 版本的 `PlatformEnum` 内容不同：有的版本内置了 `HUAWEI_NPU` 枚举值，有的没有。这段代码让同一个插件类能同时兼容两类版本——有则用内置枚举，没有则退回 `OOT`（out-of-tree 通用枚举）。这是 out-of-tree 插件应对 vLLM 版本差异的典型防御性写法。

**练习 2**：如果不加载 omni-npu 插件，直接用 `VLLM_TARGET_DEVICE=empty` 安装的 vLLM 启动服务，会发生什么？

**参考答案**：vLLM 没有任何可用的设备后端（empty 安装本身就是「无设备」构建），也找不到声明 `device_type="npu"` 的平台类，启动会因没有平台/设备支持而失败。这反过来说明：镜像里 vLLM 的 NPU 能力 100% 来自 omni-npu 插件，而不是 vLLM 本体。

**练习 3**：`device_control_env_var = "ASCEND_RT_VISIBLE_DEVICES"` 在部署链路里出现在哪里？

**参考答案**：在 ansible 模板拼装的 pd_run.sh 参数里，`--ascend-rt-visible-devices "${PREFILL_SERVER_LIST}"` 把 inventory 中的 16 卡列表传给服务进程（见 [tools/ansible/92B/omni_infer_server_template_performance1P1D_92B_bf16_open.yml:L137](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_server_template_performance1P1D_92B_bf16_open.yml#L137)），最终落到这个环境变量上（承接 u1-l3「16 卡列表换算张量并行度」的知识）。

### 4.2 entry points 机制：三个入口的声明处

#### 4.2.1 概念说明

2.2 节已经介绍了 entry points 的原理。本模块把镜头对准声明本体：`pyproject.toml` 的两个 entry point 组、共三个入口。理解这一小节后，你看到部署模板里的 `VLLM_PLUGINS="omni-npu,omni_npu_patches,omni_custom_models"` 时，应该能立刻反应出这三个名字各自指向哪段代码。

两个组的分工：

- `vllm.platform_plugins`：**平台发现**。vLLM 对这组入口的返回值有强契约——必须返回「平台类路径字符串或 None」，vLLM 据此选定设备平台。每个 vLLM 进程最终只会选定一个平台。
- `vllm.general_plugins`：**通用启动钩子**。vLLM 不关心返回值，只是逐个调用；插件借这个时机做任意初始化。omni-npu 用它做两件事：打补丁（入口 2）和注册模型（入口 3）。

#### 4.2.2 核心流程

三个入口从「声明」到「被调用」的全链路：

```
components/omni-npu/pyproject.toml  （开发期：声明三个入口）
        │  pip install -e .   （安装期：写入 site-packages/*.dist-info 元数据）
        ▼
vLLM 进程启动（运行期）
        │
        ├─ importlib.metadata.entry_points(group=...)   ← 只读元数据，零 import
        │
        ├─ 组 vllm.platform_plugins
        │     └─ omni-npu → load() → omni_npu.vllm_plugin.plugin()
        │                        返回 "omni_npu.platform.NPUPlatform" → 选定为平台
        │
        └─ 组 vllm.general_plugins
              ├─ omni_npu_patches   → omni_npu.vllm_patches.apply_patches()
              │        自动导入 common + 模型专属补丁目录并应用到 vLLM 对象上
              └─ omni_custom_models → omni_npu.v1.models.register_models()
                       向 AutoConfig 与 vLLM ModelRegistry 注册 openPangu V2 模型
```

#### 4.2.3 源码精读

三个入口的声明全部集中在这 6 行：

[components/omni-npu/pyproject.toml:L33-L38](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/pyproject.toml#L33-L38)

```toml
[project.entry-points."vllm.platform_plugins"]
omni-npu = "omni_npu.vllm_plugin:plugin"

[project.entry-points."vllm.general_plugins"]
omni_npu_patches = "omni_npu.vllm_patches:apply_patches"
omni_custom_models = "omni_npu.v1.models:register_models"
```

TOML 语法提示：中括号里的 `"vllm.platform_plugins"` / `"vllm.general_plugins"` 是 entry point 的**组名**，组内每一行 `名字 = "值"` 声明一个入口。

逐项解读（**以下为对源码的转述，非逐字复制**）：

| 名字 | 值 | 解读 |
| --- | --- | --- |
| `omni-npu` | `omni_npu.vllm_plugin:plugin` | 名字带连字符，是「平台插件」的身份标识；指向 4.3 节精读的探测函数 |
| `omni_npu_patches` | `omni_npu.vllm_patches:apply_patches` | 指向 `vllm_patches` 包 `__init__.py` 里的 `apply_patches` 函数 |
| `omni_custom_models` | `omni_npu.v1.models:register_models` | 指向 `v1/models` 包 `__init__.py` 里的 `register_models` 函数 |

配套的包布局声明（说明源码在 `src/` 目录下、包名为 `omni_npu*`）：

[components/omni-npu/pyproject.toml:L40-L45](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/pyproject.toml#L40-L45)

入口 2 的函数体（补丁机制的细节在 u2-l4 展开，这里只看骨架）：

[components/omni-npu/src/omni_npu/vllm_patches/\_\_init\_\_.py:L217-L224](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/vllm_patches/__init__.py#L217-L224)

```python
manager = PatchManager()

def apply_patches():
    # auto import and register patches
    auto_import_patches()
    manager.apply_patches()
```

两步：`auto_import_patches()` 按目录扫描补丁文件并触发注册（common 目录 + 按 `model_type` 映射的模型专属目录）；`manager.apply_patches()` 依据 `OMNI_NPU_VLLM_PATCHES` 环境变量决定应用哪些（未设置、为空或 `ALL` 时全部应用，见 [components/omni-npu/src/omni_npu/vllm_patches/patch_manager.py:L66-L73](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/vllm_patches/patch_manager.py#L66-L73)）。

入口 3 的函数体：

[components/omni-npu/src/omni_npu/v1/models/\_\_init\_\_.py:L7-L22](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/v1/models/__init__.py#L7-L22)

```python
def register_models():
    from transformers import AutoConfig
    from transformers.configuration_utils import PretrainedConfig

    class OpenPanguV2Config(PretrainedConfig):
        model_type = "openpangu_v2"
        keys_to_ignore_at_inference = ["past_key_values"]

    AutoConfig.register("openpangu_v2", OpenPanguV2Config)

    ModelRegistry.register_model(
        "OpenPanguV2ForCausalLM",
        "omni_npu.v1.models.pangu.pangu_v2_moe:OpenPanguV2ForCausalLM")
    ModelRegistry.register_model(
        "OpenPanguV2MTPModel",
        "omni_npu.v1.models.pangu.pangu_v2_moe_mtp:OpenPanguV2MTP")
```

它做两层注册：先让 transformers 的 `AutoConfig` 认识 `openpangu_v2` 这个 `model_type`（否则加载权重目录里的 `config.json` 都会报未知模型）；再把 vLLM 的 `ModelRegistry` 中 `OpenPanguV2ForCausalLM`（主模型）和 `OpenPanguV2MTPModel`（MTP 投机解码 draft 模型）两个架构名映射到 omni-npu 自己的实现模块（u3-l1 精读）。注意这里同样用「架构名 → 模块:类名字符串」的延迟导入风格，与 entry points 一脉相承。

最后看部署侧如何「点名」这三个插件。1P1D 模板中 prefill 与 decode 两侧都显式导出了同一个环境变量（两处内容一致）：

[tools/ansible/92B/omni_infer_server_template_performance1P1D_92B_bf16_open.yml:L78-L83](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_server_template_performance1P1D_92B_bf16_open.yml#L78-L83)

```bash
# patch
export VLLM_PLUGINS="omni-npu,omni_npu_patches,omni_custom_models"
export HYBRID_ATTN_GROUP_SIZE=17
export OMNI_NPU_PATCHES_DIR="pangu_v2_hybrid, pangu_v2_moe"
export OMNI_NPU_VLLM_PATCHES="ALL"
export CUSTOM_MODEL_CONFIG_PATH="low_latency/openpangu_v2/pangu_v2_moe_bf16_a3_92B_xp1d_p_open.json"
```

decode 侧同样的四连 export 见 [tools/ansible/92B/omni_infer_server_template_performance1P1D_92B_bf16_open.yml:L165-L172](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_server_template_performance1P1D_92B_bf16_open.yml#L165-L172)。注意**这三个名字与 pyproject.toml 中 entry point 的名字逐字对应**（连字符/下划线都不能错）。vLLM 侧用 `VLLM_PLUGINS` 控制加载哪些插件；显式列出三个名字既保证确定性，也把「本服务依赖哪些插件」写成了部署配置的一部分。vLLM 对该变量的精确过滤规则（例如对 platform 组与 general 组是否同样生效）属于 vLLM 源码行为，本仓库不含其源码，可在容器内 `grep -rn "VLLM_PLUGINS" /opt/vllm/vllm/` 查证——待本地验证。

#### 4.2.4 代码实践

**实践目标**：用 Python 标准库亲手打印 omni-npu 注册的全部 entry points，验证「声明 → 元数据 → 可发现」这条链路真的成立。

**操作步骤**：

1. 进入已部署容器（NPU 环境里 omni-npu 已安装）：
   ```bash
   docker exec -it docker_p bash
   ```
2. 执行（示例代码，一行版便于复制）：
   ```bash
   python3 -c "
   from importlib.metadata import entry_points
   for group in ('vllm.platform_plugins', 'vllm.general_plugins'):
       print('==', group)
       for ep in entry_points(group=group):
           print('  ', ep.name, '->', ep.value)
   "
   ```
3. 追问一步：看看 `ep.load()` 是否真的能拿到可调用对象（只对 `omni-npu` 这一个入口做）：
   ```bash
   python3 -c "
   from importlib.metadata import entry_points
   ep = [e for e in entry_points(group='vllm.platform_plugins') if e.name == 'omni-npu'][0]
   fn = ep.load()
   print('loaded:', fn, '| callable:', callable(fn))
   "
   ```
4. 没有容器的读者可在任意装了 omni-npu 的开发机（`pip install -e components/omni-npu`，注意其 `requires-python >= 3.11`，见 [components/omni-npu/pyproject.toml:L10](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/pyproject.toml#L10)）上执行同样命令。

**需要观察的现象**：

- 第 2 步应打印出两组、共三个入口，`名字 -> 值` 与 4.2.3 的表格逐字一致。
- 第 3 步 `loaded` 应打印 `<function plugin at 0x...>`，`callable` 为 `True`——这一步真正发生了 `import omni_npu.vllm_plugin`。

**预期结果**：

```
== vllm.platform_plugins
   omni-npu -> omni_npu.vllm_plugin:plugin
== vllm.general_plugins
   omni_npu_patches -> omni_npu.vllm_patches:apply_patches
   omni_custom_models -> omni_npu.v1.models:register_models
```

（若镜像里还装了别的 vLLM 插件，会出现额外行，属正常现象。）注意：`entry_points(group=...)` 关键字写法要求 Python ≥ 3.10；老 API 是 `entry_points()['vllm.platform_plugins']`。完整运行结果**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：把 `VLLM_PLUGINS` 的值改成 `"omni-npu"`（去掉后两个），按本讲机制推理服务会出现什么变化？

**参考答案**：平台插件仍会加载（NPU 探测、NPUPlatform 生效不变），但 `apply_patches` 与 `register_models` 两个 general 插件不再被调用：运行时补丁不会应用，`OpenPanguV2ForCausalLM` 也不会注册——加载 openPangu 权重时 vLLM 的 ModelRegistry 找不到该架构，服务启动报错。这个思想实验说明三个入口**各司其职、缺一不可**。（精确结论取决于 vLLM 版本对 VLLM_PLUGINS 的过滤实现，建议在测试环境实测——待本地验证。）

**练习 2**：为什么入口值写成 `"omni_npu.vllm_plugin:plugin"` 这种字符串，而不是直接 `from omni_npu.vllm_plugin import plugin` 写成 Python 表达式？

**参考答案**：pyproject.toml 是静态配置文件，pip 安装时只把它当字符串写进元数据，不能执行代码。字符串形式让「发现」（读元数据）与「加载」（import 模块）彻底分离：vLLM 扫描时零开销，真正选定平台后才触发 import，避免了「扫描所有插件就 import 一堆重依赖」的问题。

**练习 3**：入口 2 的名字 `omni_npu_patches` 与入口 1 的名字 `omni-npu` 一个用下划线、一个用连字符，这有问题吗？

**参考答案**：没有任何强制规范，entry point 的名字只是组内的标识字符串，只需与引用方（`VLLM_PLUGINS`、日志）逐字匹配即可。事实上下游引用恰好逐字复用了这两个名字（连字符与下划线都原样保留），改名反而会造成不兼容——这本身是一个「配置耦合在字符串上」的活例子。

### 4.3 插件探测逻辑：`plugin()` 函数精读

#### 4.3.1 概念说明

`plugin()` 是整个 omni-npu 里最短、却最先被 vLLM 调用的函数（整个文件只有 27 行）。它要回答一个问题：**「当前进程是不是跑在昇腾 NPU 环境里？」**

它的设计有三个值得学习的点：

1. **返回类路径字符串而非类对象**：调用方（vLLM）拿到 `"omni_npu.platform.NPUPlatform"` 后自行 import。这样探测阶段完全不需要加载 `platform.py` 及其依赖（torch、vllm.logger 等重型模块），探测失败时零成本退出。
2. **失败返回 `None` 而不是抛异常**：这是平台插件组的契约——返回 `None` 意味着「我不是这个环境的平台」，vLLM 会继续尝试下一个平台插件或回落默认平台。抛异常则会中断其他插件的探测。
3. **两级探测 + 容错**：先试独立的 `torch_npu` 包，再退回检查 `torch.npu` 属性，兼容不同发行方式的 torch_npu 安装。

#### 4.3.2 核心流程

`plugin()` 的决策逻辑可用伪代码描述：

```
def plugin():
    尝试 import torch
      失败 → return None              # 连 torch 都没有，必然不是推理环境

    尝试 import torch_npu
      成功 → return "omni_npu.platform.NPUPlatform"
      失败 → 若 torch 有 npu 属性
               → return "omni_npu.platform.NPUPlatform"   # 某些构建把 npu 直接编进 torch
             否则 → return None                            # 交给别的平台插件
```

注意源码注释里的一个细节：只要 `import torch_npu` 成功就认定是 NPU 平台，**即使此刻设备数为 0**（容器初始化时序问题）也照常返回——因为「探测」只负责定性，定量（能看到几张卡）留给后续初始化。

#### 4.3.3 源码精读

完整函数（全文仅此一个定义）：

[components/omni-npu/src/omni_npu/vllm_plugin.py:L4-L26](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/vllm_plugin.py#L4-L26)

```python
def plugin() -> str | None:
    """
    Entry point for vLLM to discover the NPU platform plugin.

    Returns the fully-qualified class name of the Platform implementation
    if an NPU environment is detected; otherwise returns None so vLLM can
    fall back to other platforms.
    """
    try:
        import torch
    except Exception:
        return None

    try:
        import torch_npu  # noqa: F401
        # If torch_npu imports, assume NPU platform is intended, even if
        # device_count is 0 at this moment (e.g., container init timing).
        return "omni_npu.platform.NPUPlatform"
    except Exception:
        # Fallback: some builds expose torch.npu without separate torch_npu pkg
        if hasattr(torch, "npu"):
            return "omni_npu.platform.NPUPlatform"
        return None
```

逐段解读：

- **L4**：函数签名 `-> str | None`——用类型标注把「平台插件组」的契约写在脸上：要么类路径字符串，要么 `None`。
- **L12-L15**：第一级守卫。`import torch` 失败说明连 PyTorch 都没装，直接返回 `None`；注意捕获的是 `Exception`（而不是仅 `ImportError`），任何安装损坏情形都安全退出。
- **L17-L21**：第二级探测，主力路径。`import torch_npu` 成功即返回平台类路径；`# noqa: F401` 表明 import 本身就是探测动作，不使用该模块。注释明确说明「即使 device_count 为 0 也认为 intended」（容器初始化时序场景）。
- **L22-L26**：兜底路径。部分构建把 NPU 支持直接编进 torch（没有独立 `torch_npu` 包），用 `hasattr(torch, "npu")` 识别；两者皆无则返回 `None`，把机会让给其他平台插件。

对比一下部署模板：镜像里同时装有 torch 与 torch_npu（L2 镜像的设备层负责，见 u1-l2/u10-l1 的构建分层），所以在生产容器中 `plugin()` 走的总是 L17-L21 的主力路径。

#### 4.3.4 代码实践

**实践目标**：验证 `plugin()` 的返回值，并观察「有 torch_npu」与「无 torch_npu」两种环境下的行为差异。

**操作步骤**：

1. 在 NPU 容器内（torch_npu 可用）：
   ```bash
   docker exec -it docker_p bash
   python3 -c "
   import omni_npu.vllm_plugin as p
   ret = p.plugin()
   print('plugin() ->', ret)
   assert ret == 'omni_npu.platform.NPUPlatform', 'unexpected return'
   print('assert passed: matches entry-point contract')
   "
   ```
2. 顺手验证返回的类路径真的能取到类，且声明与 4.1 一致：
   ```bash
   python3 -c "
   import importlib
   ret = __import__('omni_npu.vllm_plugin', fromlist=['plugin']).plugin()
   mod_name, cls_name = ret.rsplit('.', 1)
   cls = getattr(importlib.import_module(mod_name), cls_name)
   print(cls.__name__, '| device_type =', cls.device_type)
   "
   ```
3. （可选，无 NPU 的开发机）`pip install -e components/omni-npu` 后执行第 1 步命令，观察 `plugin()` 的返回值是否变为 `None`。

**需要观察的现象**：

- 第 1 步在 NPU 容器内打印 `plugin() -> omni_npu.platform.NPUPlatform`，断言通过。
- 第 2 步打印 `NPUPlatform | device_type = npu`——证明「字符串路径 → 真实类」这条 vLLM 将走的路是通的。
- 第 3 步在无 torch_npu 的机器上：若装了 CPU 版 torch，则 `import torch` 成功、`import torch_npu` 失败且 torch 无 `npu` 属性，应打印 `plugin() -> None`。

**预期结果**：NPU 容器内两步都成功；普通开发机上返回 `None`——这正是该函数「环境探针」语义的体现。完整运行结果**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `plugin()` 在 `import torch` 失败时返回 `None` 而不是抛出 `ImportError`？

**参考答案**：平台插件组的调用契约是「用返回值表态」：返回类路径表示认领该环境，返回 `None` 表示让位。若抛异常，vLLM 的平台发现流程会被打断，其他平台插件（包括 vLLM 默认的 CUDA 平台回落）都没机会执行。对「探测」类函数而言，`None` 是标准的否定语义。

**练习 2**：源码注释说「即使 device_count 是 0 也返回平台类路径」。假设某个容器里 torch_npu 装了但驱动没挂载好（设备数为 0），按本讲机制推演接下来会发生什么？

**参考答案**：平台探测会「成功」，NPUPlatform 被选定，`check_and_update_config` 等配置流程照常进行；失败会推迟到初始化阶段——`NPUWorker` 初始化设备或 `torch.npu.device_count()` 相关调用时才暴露设备不可用。也就是说探测函数故意只做定性判断，把硬件级失败留给更靠近设备的代码去报告（对照 u1-l5 的日志分层排障思路：这类问题应到 `server_0.log` 里找）。

**练习 3**：`plugin()` 里 `hasattr(torch, "npu")` 这个兜底分支存在的意义是什么？

**参考答案**：torch_npu 有两种发行形态：独立 pip 包（主流，`import torch_npu` 成功），以及直接把 NPU 支持编进 torch 的定制构建（没有独立包，但 `torch.npu` 命名空间存在）。兜底分支让插件对第二种形态也能正确认领 NPU 环境，扩大兼容面。

## 5. 综合实践

**任务：制作一张「插件加载追踪表」，把三个入口从部署配置一路追到生效证据。**

这个任务把本讲三个模块串起来，产出一张可以直接贴进团队文档的表。步骤：

1. **起点：部署配置**。打开 [tools/ansible/92B/omni_infer_server_template_performance1P1D_92B_bf16_open.yml:L78-L86](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_server_template_performance1P1D_92B_bf16_open.yml#L78-L86)，记下 `VLLM_PLUGINS` 里点名的三个插件名。
2. **对照：entry point 声明**。在 [components/omni-npu/pyproject.toml:L33-L38](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/pyproject.toml#L33-L38) 找到每个名字对应的「模块:属性」，逐字核对名字是否一致。
3. **深入：读入口函数**。依次打开 `omni_npu/vllm_plugin.py`、`omni_npu/vllm_patches/__init__.py`、`omni_npu/v1/models/__init__.py`，用一句话概括每个函数做什么、依赖哪些环境变量（提示：入口 2 依赖 `OMNI_NPU_PATCHES_DIR` 与 `OMNI_NPU_VLLM_PATCHES`，它们就在同一屏 export 里）。
4. **验证：容器内实测**。在 `docker_p` 容器里执行 4.2.4 的打印命令与 4.3.4 的验证命令，把真实输出贴进表格。
5. **产出**：填完下面这张表（示例行已给出）：

| 插件名（VLLM_PLUGINS） | entry point 值 | 入口函数行为 | 关键环境变量 | 实测输出/生效证据 |
| --- | --- | --- | --- | --- |
| `omni-npu` | `omni_npu.vllm_plugin:plugin` | 探测 torch_npu，返回平台类路径 | 无 | `plugin() -> omni_npu.platform.NPUPlatform`（待本地验证） |
| `omni_npu_patches` | …（自己填） | …（自己填） | …（自己填） | …（自己填） |
| `omni_custom_models` | …（自己填） | …（自己填） | …（自己填） | …（自己填） |

加分项：在无 NPU 的开发机上 `pip install -e components/omni-npu` 后重复第 4 步，观察 `plugin()` 返回 `None` 而另两个入口的 entry point 元数据依然可见——用一张「同一命令、两种环境」的对照截图，论证「发现靠元数据、加载靠环境」这两层是解耦的。

## 6. 本讲小结

- vLLM 通过两个 entry point 组接入外部能力：`vllm.platform_plugins` 负责「选平台」（返回类路径字符串或 `None`，契约严格），`vllm.general_plugins` 是通用启动钩子（vLLM 只管调用）。
- omni-npu 注册了**三个入口**：`omni-npu`（平台探测）、`omni_npu_patches`（运行时补丁）、`omni_custom_models`（模型注册），声明集中在 `pyproject.toml`，靠 `pip install -e .` 写进包元数据后才能被 vLLM 发现——这就是「零侵入」的全部秘密。
- `plugin()` 是一个纯「环境探针」：torch 存在 → torch_npu 可导入（或 torch 带 npu 属性）→ 返回 `"omni_npu.platform.NPUPlatform"`；任何一环失败返回 `None` 让位其他平台，且只做定性判断（device_count 为 0 也认领）。
- 探测成功的终点是 `NPUPlatform`：它声明 `device_type=npu`、HCCL 通信、`ASCEND_RT_VISIBLE_DEVICES` 设备可见性，并在 `check_and_update_config` 中把 worker 类改写为 `omni_npu.worker.npu_worker.NPUWorker`。
- 部署侧用 `export VLLM_PLUGINS="omni-npu,omni_npu_patches,omni_custom_models"` 显式点名三个插件，名字必须与 entry point 名逐字一致；配套的 `OMNI_NPU_PATCHES_DIR` / `OMNI_NPU_VLLM_PATCHES` 则控制入口 2 的补丁范围。
- entry points 模式在本仓库内部也被复用（`omni.kv_connectors` 组），学会本讲等于同时掌握了给 omni-npu 自己写扩展插件的钥匙。

## 7. 下一步学习建议

- **下一讲（u2-l2）**：深入 `NPUPlatform` 本体——逐个方法精读 `check_and_update_config` 改写了哪些 vLLM 配置、`get_attn_backend_cls` 如何选择注意力后端、`get_device_communicator_cls` 如何指向 HCCL 通信器。本讲的「声明书」比喻会在那一讲变成逐项落实的配置清单。
- **u2-l4**：如果对入口 2 的补丁机制好奇，可提前阅读 `PatchManager` 的注册/去重/按环境变量应用流程，理解「不改 vLLM 源码改行为」的另一半实现。
- **u3-l1**：入口 3 注册的 `OpenPanguV2ForCausalLM` 指向 `pangu_v2_moe.py`，那是一个 2400 余行的 MoE 模型实现，是单元 3 的主角。
- **延伸阅读**：Python 官方文档的 `importlib.metadata` 章节与 PEP 621（pyproject.toml 声明式元数据），能把本讲的 entry points 机制补成完整知识块；vLLM 官方文档的 "Plugin System" 章节则给出平台插件接口的权威定义。
