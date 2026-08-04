# 平台抽象：CUDA/ROCm/NPU/XPU/MUSA

## 1. 本讲目标

vLLM-Omni 要在五种差异极大的硬件上跑同一套代码：NVIDIA GPU（CUDA）、AMD GPU（ROCm）、华为昇腾 NPU、Intel XPU、摩尔线程 MUSA。它们各自有不同的设备 API、不同的注意力 kernel、不同的 worker 类、不同的图捕获机制。本讲讲解 vLLM-Omni 如何用一个**平台抽象层（Platform Abstraction）**把这些差异藏在一道统一接口后面。

学完本讲，你应该能够：

1. 说出 `OmniPlatform` 抽象基类对外暴露了哪几类「平台相关」的方法（worker 类、注意力后端、设备、图包装器等）。
2. 理解 `current_omni_platform` 这个**惰性单例（lazy singleton）**是如何被自动探测、且全局只允许激活一个的。
3. 看懂五个平台子类（CUDA/ROCm/NPU/XPU/MUSA）在「默认注意力后端」「AR worker 类」「设备句柄」上的具体差异，并能填出一张平台行为对照表。
4. 把本讲与 u7-l1（注意力后端选择）、u5-l3（diffusion worker）连起来：u7-l1 的 selector 最终调用的就是 `current_omni_platform.get_diffusion_attn_backend_cls(...)`，u5-l3 的 `init_device` 最终调用的就是 `current_omni_platform.get_torch_device(...)`。

---

## 2. 前置知识

阅读本讲前，建议你已经了解以下概念（本手册前置讲义已覆盖）：

- **平台（Platform）**：vLLM 自己就有一套 `Platform` 抽象（`vllm.platforms.interface.Platform`），用来屏蔽 CUDA/CPU 等差异。vLLM-Omni 的 `OmniPlatform` 不是另起炉灶，而是**继承** vLLM 的 `Platform`，在其上叠加 omni 专属接口。这和 u2-l1 讲过的「修改🟡 / 新增🔴」二分法一脉相承——平台层属于「新增🔴」，靠新增子类而非改 vLLM 源码来扩展。
- **注意力后端（attention backend）**：u7-l1 讲过，diffusion 注意力按 `role` 命名站点，selector 用四级优先级（per_role → role_category → default → 平台默认）解析出一个具体的 kernel 实现（`AttentionBackend`）。本讲的「平台默认」就是第四级，由各平台类提供。
- **stage / worker**：u3-l3 讲过每个 stage 是一个独立进程；u4-l1 讲过 AR stage 内部用 `GPUARWorker`、`GPUARModelRunner`。本讲回答：为什么 NPU 上用的是 `NPUARWorker` 而不是 `GPUARWorker`？答案是平台类在启动期把 worker 类名换掉了。

几个本讲会用到的术语：

| 术语 | 含义 |
|------|------|
| `current_omni_platform` | 一个全局可访问的「当前平台」单例对象，进程内只初始化一次 |
| 惰性初始化（lazy init） | 第一次访问 `current_omni_platform` 时才真正探测硬件并构造对象，而非 import 时就构造 |
| 探测函数（probe） | 返回平台类全限定名（qualname）或 `None` 的函数，`None` 表示「这个硬件不存在」 |
| 多重继承（multiple inheritance） | `class NPUOmniPlatform(OmniPlatform, NPUPlatform)` 同时继承 omni 接口与硬件基类 |
| qualname | 形如 `vllm_omni.platforms.npu.platform.NPUOmniPlatform` 的「模块路径.类名」字符串，可被 `resolve_obj_by_qualname` 反射加载 |

---

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
|------|------|
| [vllm_omni/platforms/interface.py](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/platforms/interface.py) | 定义 `OmniPlatform` 抽象基类与 `OmniPlatformEnum`，是所有平台类的「契约」 |
| [vllm_omni/platforms/__init__.py](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/platforms/__init__.py) | 定义五个探测函数、`current_omni_platform` 惰性单例、激活冲突校验 |
| [vllm_omni/platforms/cuda/platform.py](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/platforms/cuda/platform.py) | CUDA 平台类，含最复杂的注意力后端降级逻辑（Blackwell/TRTLLM/cuDNN/FlashInfer/FA/SDPA） |
| [vllm_omni/platforms/npu/platform.py](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/platforms/npu/platform.py) | NPU 平台类，换用 `NPUARWorker`、`ACLGraphWrapper`、`set_ascend_forward_context` |
| [vllm_omni/platforms/rocm/platform.py](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/platforms/rocm/platform.py) | ROCm 平台类，注意力依赖 `aiter`，AR 后端有特殊覆盖逻辑 |
| [vllm_omni/platforms/xpu/platform.py](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/platforms/xpu/platform.py) | Intel XPU 平台类，换用 `XPUARWorker` |
| [vllm_omni/platforms/musa/platform.py](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/platforms/musa/platform.py) | 摩尔线程 MUSA 平台类 |
| [vllm_omni/diffusion/attention/selector.py](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/attention/selector.py) | 注意力 selector，在「平台默认」分支调用 `current_omni_platform.get_diffusion_attn_backend_cls`（消费者侧） |
| [vllm_omni/engine/stage_init_utils.py](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/engine/stage_init_utils.py) | `resolve_worker_cls` 在 stage 启动期向平台要 AR/generation worker 类（消费者侧） |
| [vllm_omni/diffusion/worker/diffusion_worker.py](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/worker/diffusion_worker.py) | `init_device` 用平台拿设备句柄、初始化分布式（消费者侧） |

---

## 4. 核心概念与源码讲解

本讲拆为三个最小模块：① `OmniPlatform` 抽象接口；② `current_omni_platform` 的检测与访问；③ 各平台的 worker / 注意力后端映射。

### 4.1 OmniPlatform 抽象接口

#### 4.1.1 概念说明

`OmniPlatform` 解决的核心问题是：**「换一种硬件，到底有哪些东西必须跟着换？」**

把这个问题的答案枚举出来，就是 `OmniPlatform` 要定义的接口。粗略归类：

- **设备句柄类**：`get_torch_device()` 返回 `torch.device("cuda")` 还是 `torch.device("npu")`？`get_device_count()`、`synchronize()`、`get_free_memory()` 各自调用哪个后端 API？
- **Worker 类**：AR stage 用哪个 worker 类？generation stage 用哪个？diffusion stage 用哪个 worker / model runner？
- **注意力后端**：当用户没显式指定时，平台默认选哪个 diffusion 注意力 kernel？
- **图与编译**：是否支持 `torch.compile`（inductor）？图捕获用 `CUDAGraphWrapper` 还是 `ACLGraphWrapper`？
- **stage 配置默认路径**：默认部署 YAML 从哪个目录读？
- **omni 专属的杂项钩子**：diffusion fused-MoE 的运行时准备、跨流同步事件（`record_device_event`）等。

`OmniPlatform` 继承自 vLLM 的 `Platform`，因此它「白拿」了 vLLM 已经实现好的大量通用能力（显存、设备名等），只在 vLLM 没覆盖的地方补 omni 接口。这种「多重继承」模式让每个具体平台类（如 `NPUOmniPlatform`）写成 `class NPUOmniPlatform(OmniPlatform, NPUPlatform)`，左边继承 omni 接口、右边继承硬件实现，两边各司其职。

#### 4.1.2 核心流程

`OmniPlatform` 作为抽象基类，其设计流程是：

1. 定义类属性 `_omni_enum: OmniPlatformEnum`，每个子类用它在构造期「自报家门」。
2. 提供一组 `is_xxx()` 谓词方法（`is_cuda()`/`is_npu()` 等），统一用 `_omni_enum` 比较，避免代码里到处写 `isinstance(x, CudaOmniPlatform)`。
3. 把平台相关方法声明为 `@classmethod`，绝大多数以 `raise NotImplementedError` 作为默认——**强制子类必须实现**（这是「契约」的体现）。
4. 少数方法给出跨平台通用的默认实现（如 `get_diffusion_worker_cls` 默认指向 `DiffusionWorker`，大多数平台无需覆盖）。
5. 另设一个兜底类 `UnspecifiedOmniPlatform`，用于「探测不到任何硬件」的 CPU 兜底场景。

> 注意一个重要区分：`is_xxx()` 比较的是 `_omni_enum`，而不是 `isinstance`。这意味着即便某个 OOT（out-of-tree）插件平台子类的类名你完全不认识，只要它把 `_omni_enum` 设对了，谓词方法照样工作。

#### 4.1.3 源码精读

**枚举与基类声明**——`OmniPlatformEnum` 列出全部支持的硬件类别（含 `OOT` 给外部插件、`UNSPECIFIED` 给兜底）：

[文件路径:vllm_omni/platforms/interface.py:L19-L28](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/platforms/interface.py#L19-L28) 定义枚举；

[文件路径:vllm_omni/platforms/interface.py:L31-L58](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/platforms/interface.py#L31-L58) 声明 `OmniPlatform(OmniPlatform, Platform)` 基类与六个 `is_xxx()` 谓词——它们全部等价于「`self._omni_enum == 某枚举值`」：

```python
class OmniPlatform(Platform):
    _omni_enum: OmniPlatformEnum

    def is_npu(self) -> bool:
        return self._omni_enum == OmniPlatformEnum.NPU
    # ... is_xpu / is_cuda / is_rocm / is_musa / is_out_of_tree 同理
```

**Worker 类契约**——三个 `raise NotImplementedError` 的方法，分别决定 AR、generation、（默认）diffusion worker 用哪个类。返回值是**字符串全限定名**而非类对象，目的是让调用方按需懒加载、避免在探测阶段就 import 重依赖：

[文件路径:vllm_omni/platforms/interface.py:L60-L70](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/platforms/interface.py#L60-L70) 给出 AR/generation worker 与默认 stage 配置路径的抽象方法。

**注意力后端契约**——`get_diffusion_attn_backend_cls` 接收「用户是否显式选了后端」「head_size」「是否允许 TRTLLM 作默认」三个参数，返回一个后端类全限定名字符串。基类只声明、不实现：

[文件路径:vllm_omni/platforms/interface.py:L107-L128](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/platforms/interface.py#L107-L128) 注意力后端选择抽象，方法 docstring 解释了三个入参的含义。

**跨平台通用默认**——diffusion worker 与 model runner 类大多数平台无需改，基类直接给出默认指向 omni 的通用实现：

[文件路径:vllm_omni/platforms/interface.py:L140-L157](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/platforms/interface.py#L140-L157) `get_diffusion_worker_cls` / `get_diffusion_model_runner_cls` 的默认实现，分别指向 `DiffusionWorker` 和 `DiffusionModelRunner`。

**跨流同步钩子**——`record_device_event` 在基类默认返回 `None`（安全 no-op），但 NPU 会覆盖它：因为昇腾的 HCCL 通信可能用「默认流之外」的内部流，必须先同步默认流再记录事件，否则下游 `wait_event` 拿到的可能是没写完的数据。这个细节正是平台抽象的价值——把「硬件特有的坑」收进子类：

[文件路径:vllm_omni/platforms/interface.py:L195-L209](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/platforms/interface.py#L195-L209) 基类默认 no-op 的 `record_device_event`，docstring 说明为何 ROCm/XPU/MUSA 落到 no-op、NPU 需覆盖。

**图包装器与前向上下文**——基类默认用 vLLM 的 `CUDAGraphWrapper` 与 `set_forward_context`；NPU 覆盖成昇腾的 `ACLGraphWrapper` 与 `set_ascend_forward_context`（并把参数名 `cudagraph_runtime_mode` 改名成 `aclgraph_runtime_mode`）：

[文件路径:vllm_omni/platforms/interface.py:L266-L298](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/platforms/interface.py#L266-L298) `get_graph_wrapper_cls` 与 `set_forward_context` 的平台中立默认实现。

**兜底类**——探测不到任何硬件时使用，设备类型退化为 CPU：

[文件路径:vllm_omni/platforms/interface.py:L301-L313](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/platforms/interface.py#L301-L313) `UnspecifiedOmniPlatform`，`get_device_count()` 返回 0、设备为 CPU。

#### 4.1.4 代码实践

**实践目标**：用源码阅读的方式，搞清「哪些方法必须由子类实现、哪些有默认实现」，并体会「返回字符串全限定名而非类」的设计动机。

**操作步骤**：

1. 打开 [vllm_omni/platforms/interface.py](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/platforms/interface.py)，用编辑器搜索 `raise NotImplementedError`。
2. 把每个抛异常的方法名，按「设备句柄 / Worker 类 / 注意力后端 / 图与编译 / 其它」分类，填进一张表。
3. 再搜索 `return "`（带引号的 return），找出哪些方法给了「字符串全限定名」的默认实现。
4. 思考：为什么这些方法返回 `"vllm_omni.diffusion.worker.diffusion_worker.DiffusionWorker"` 这样的字符串，而不是直接 `return DiffusionWorker`？

**需要观察的现象**：

- 抛 `NotImplementedError` 的方法大多和「具体硬件强绑定」（设备、AR worker、注意力后端）。
- 给默认实现的方法大多是「omni 自己写的、与硬件无关的类」（diffusion worker/model runner、profiler）。

**预期结果**：你会得到一张「抽象方法 vs 默认方法」对照表，前者子类必填、后者子类可空。返回字符串全限定名是为了让调用方（`resolve_obj_by_qualname`）按需 import——探测阶段不触发重依赖加载，这和 u2-l1 讲过的「懒加载避免拖垮轻量子进程」是同一动机。

#### 4.1.5 小练习与答案

**练习 1**：`OmniPlatform` 继承自 vLLM 的 `Platform`，却还自己定义了 `is_cuda()`。vLLM 的 `Platform` 不是已经有 `is_cuda` 了吗，为什么 omni 要重写？

**参考答案**：omni 的 `is_cuda()` 改为比较 `_omni_enum == OmniPlatformEnum.CUDA`，而非 vLLM 默认的基于 `_enum`（vLLM 的 `PlatformEnum`）。这样 omni 的所有平台类（包括未来的 OOT 插件类）都通过同一套 `OmniPlatformEnum` 自报家门，谓词行为与 omni 的探测/激活逻辑保持一致；否则一个 omni 自定义平台可能 `_enum` 没设对而被 vLLM 谓词误判。

**练习 2**：`get_diffusion_attn_backend_cls` 为什么不在基类给一个 `return DiffusionAttentionBackendEnum.TORCH_SDPA.get_path()` 的默认实现？这样不就能少写几行？

**参考答案**：因为「默认后端」本身就是**平台相关**的核心决策（CUDA 在 Blackwell 上默认 TRTLLM/cuDNN，NPU 默认走 mindiesd 的 FLASH_ATTN，ROCm 默认走 aiter）。如果基类给一个看似通用的 SDPA 默认，会诱导子类忘记覆盖、导致硬件性能白白损失。`raise NotImplementedError` 是「强制子类做选择」的契约式提醒。

---

### 4.2 current_omni_platform：检测与访问

#### 4.2.1 概念说明

有了抽象基类，还需要一个机制回答「当前进程到底该用哪个平台类？」。这就是 `current_omni_platform`——一个**进程级全局单例**。

它的两个关键设计：

1. **惰性初始化**：不在 `import vllm_omni.platforms` 时就探测硬件，而是等到第一次真正访问 `current_omni_platform` 这个名字时才探测。这非常重要，因为探测函数会去 `import pynvml`、`import amdsmi`、`import torchada` 这些重且不一定装得上的库；如果 import 阶段就跑，会让所有轻量子进程（哪怕是 NPU 机器上跑的某个 CUDA 无关脚本）都背上报错风险。
2. **自动探测 + 唯一性约束**：系统会依次问五个内置探测函数「你在不在？」，外加任何已注册的 OOT 插件。被激活的平台**必须唯一**——同时探测到两个就抛 `RuntimeError`，因为不同硬件的 worker 类、kernel 完全不兼容，混用必崩。

#### 4.2.2 核心流程

`current_omni_platform` 的解析流程（在 `resolve_current_omni_platform_cls_qualname` 中）：

1. 收集「内置探测函数表」`builtin_omni_platform_plugins`（cuda/rocm/npu/xpu/musa）和「OOT 插件表」（经 `load_omni_plugins_by_group` 从 entry points 发现）。
2. 依次调用每个探测函数；返回非 `None` 的记为「已激活」，追加到 `activated_plugins`。注意：**所有探测函数都会被调用一遍**（异常被吞掉），即使前一个已命中。
3. 把已激活集合分别与「内置表」「OOT 表」求交集。
4. 优先级判定（顺序很关键）：
   - 若 ≥2 个 OOT 插件激活 → `RuntimeError`（互斥冲突）。
   - 若恰好 1 个 OOT 插件激活 → 用它（**OOT 优先级高于内置**）。
   - 若 ≥2 个内置激活 → `RuntimeError`。
   - 若恰好 1 个内置激活 → 用它。
   - 否则（一个都没探到）→ 兜底 `UnspecifiedOmniPlatform`。
5. 拿到类全限定名后，`resolve_obj_by_qualname` 反射加载并**实例化**（调用 `()`），存进模块级变量 `_current_omni_platform`。
6. 第二次访问 `current_omni_platform` 时直接返回缓存的单例，不再重复探测。

#### 4.2.3 源码精读

**五个内置探测函数**——每个都遵循同一模式：try 探测硬件库 → 命中则返回类全限定名，否则返回 `None`；任何异常都降级为「不可用」。

CUDA 用 `pynvml` 数 GPU 数量：

[文件路径:vllm_omni/platforms/__init__.py:L20-L40](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/platforms/__init__.py#L20-L40) `cuda_omni_platform_plugin` 用 `pynvml.nvmlDeviceGetCount() > 0` 判定。

NPU 用 `torch.npu.is_available()`：

[文件路径:vllm_omni/platforms/__init__.py:L65-L78](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/platforms/__init__.py#L65-L78) `npu_omni_platform_plugin` 探测 `torch.npu`。

ROCm 用 `amdsmi`，XPU 用 `torch.xpu`，MUSA 用 `torchada`，对应：

[文件路径:vllm_omni/platforms/__init__.py:L43-L62](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/platforms/__init__.py#L43-L62) ROCm（`amdsmi`）；

[文件路径:vllm_omni/platforms/__init__.py:L81-L104](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/platforms/__init__.py#L81-L104) XPU（`torch.xpu` + 选 `xccl`/`ccl` 后端）；

[文件路径:vllm_omni/platforms/__init__.py:L107-L120](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/platforms/__init__.py#L107-L120) MUSA（`torchada.is_musa_platform()`）。

**内置探测表与唯一性判定**——`resolve_current_omni_platform_cls_qualname` 是整个检测的核心：

[文件路径:vllm_omni/platforms/__init__.py:L132-L164](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/platforms/__init__.py#L132-L164) 核心解析逻辑：遍历内置 + OOT → 收集激活集合 → 按「OOT 优先、内置次之、否则兜底」四档判定，任何一档出现 ≥2 个就 `RuntimeError`。关键几行：

```python
activated_builtin_plugins = list(set(activated_plugins) & set(builtin_omni_platform_plugins.keys()))
activated_oot_plugins = list(set(activated_plugins) & set(platform_plugins.keys()))

if len(activated_oot_plugins) >= 2:
    raise RuntimeError(f"Only one OmniPlatform plugin can be activated, but got: {activated_oot_plugins}")
elif len(activated_oot_plugins) == 1:
    platform_cls_qualname = platform_plugins[activated_oot_plugins[0]]()
    ...
elif len(activated_builtin_plugins) >= 2:
    raise RuntimeError(...)
elif len(activated_builtin_plugins) == 1:
    platform_cls_qualname = builtin_omni_platform_plugins[activated_builtin_plugins[0]]()
else:
    platform_cls_qualname = "vllm_omni.platforms.interface.UnspecifiedOmniPlatform"
```

**惰性单例**——靠模块级 `__getattr__` 实现：访问 `current_omni_platform` 这个名字时，若 `_current_omni_platform is None` 才触发解析与实例化，并把构造栈存进 `_init_trace`（便于排查「是谁第一次访问的」）：

[文件路径:vllm_omni/platforms/__init__.py:L167-L187](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/platforms/__init__.py#L167-L187) `__getattr__` 守卫：第一次访问才解析 + 实例化 + 记录调用栈。

**插件发现机制**——OOT 平台经 entry points（`vllm_omni.platform_plugins` 组）发现，与内置表 `chain` 后一起参与探测，受 `VLLM_PLUGINS` 环境变量白名单控制：

[文件路径:vllm_omni/plugins/__init__.py:L24-L58](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/plugins/__init__.py#L24-L58) `load_omni_plugins_by_group` 用 `importlib.metadata.entry_points(group=...)` 发现插件，并按 `VLLM_PLUGINS` 过滤。

#### 4.2.4 代码实践

**实践目标**：在你自己的机器上，亲眼看 `current_omni_platform` 被解析成哪个平台类，并理解惰性初始化的「懒」体现在哪。

**操作步骤**：

1. 写一个最小脚本 `probe_platform.py`（**示例代码**，非项目原有文件）：

   ```python
   import vllm_omni.platforms as P
   # 此刻还没探测：单例仍是 None
   print("before access:", P._current_omni_platform)
   # 第一次访问名字，才触发探测
   plat = P.current_omni_platform
   print("class:", type(plat).__module__ + "." + type(plat).__name__)
   print("enum:", plat._omni_enum)
   print("is_cuda:", plat.is_cuda(), "is_npu:", plat.is_npu())
   # 第二次访问直接拿缓存，不会重复探测
   print("same singleton:", plat is P.current_omni_platform)
   ```

2. 在装有 NVIDIA GPU 的机器上运行 `python probe_platform.py`。
3. 在没有 GPU 的纯 CPU 容器里再跑一次。

**需要观察的现象**：

- 访问前 `_current_omni_platform` 是 `None`；访问后才变成对象——证明惰性。
- GPU 机器上应打印 `...cuda.platform.CudaOmniPlatform` 且 `is_cuda=True`。
- CPU 容器里应打印 `...interface.UnspecifiedOmniPlatform`。
- `same singleton` 应为 `True`，证明只探测一次。
- 若出现 `RuntimeError: Only one OmniPlatform plugin can be activated`，说明同时装了两个冲突的后端（罕见，通常是环境装坏）。

**预期结果 / 待本地验证**：在标准 CUDA 机器上，输出 `CudaOmniPlatform` + `OmniPlatformEnum.CUDA`。若你的机器无 GPU，输出 `UnspecifiedOmniPlatform`。具体打印取决于本机硬件，**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：为什么探测函数要「全部跑一遍」而不是「命中第一个就 return」？

**参考答案**：因为要检测**冲突**。如果短路返回，一台同时被两个探测函数误判为命中的机器（比如装了不完整的 amdsmi 又有 CUDA）就会静默选错平台。全部跑完再统计激活数，才能在 ≥2 时主动抛错，把环境问题尽早暴露。

**练习 2**：如果一台 NPU 机器上同时注册了一个 OOT 平台插件，会发生什么？

**参考答案**：两者都会进入 `activated_plugins`。由于 OOT 优先级高于内置，且 OOT 恰好 1 个，系统会**选 OOT 而非内置 NPU**（见第 4.2.2 步骤 4 的分支顺序）。这给了「用定制平台覆盖默认 NPU 行为」的能力，但也意味着装 OOT 插件要谨慎。

**练习 3**：`_init_trace` 记录的是什么，有什么用？

**参考答案**：它用 `traceback.format_stack()` 记录「第一次访问 `current_omni_platform` 时的完整调用栈」。排查时若怀疑某个子进程在不该探测的时机探测了（从而 import 了重库导致崩溃），可以打印 `_init_trace` 定位是哪行代码第一次触发了访问。

---

### 4.3 各平台的 worker / 注意力后端映射

#### 4.3.1 概念说明

抽象接口和单例机制搭好后，真正的「硬件差异」就体现在五个子类里。这一模块回答两个最关键的问题：

1. **「我的 stage 要起 worker 了，用哪个 worker 类？」**——消费者是 [vllm_omni/engine/stage_init_utils.py](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/engine/stage_init_utils.py) 的 `resolve_worker_cls` 与 [vllm_omni/engine/arg_utils.py](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/engine/arg_utils.py)。
2. **「用户没指定注意力后端，默认给哪个？」**——消费者是 [vllm_omni/diffusion/attention/selector.py](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/attention/selector.py) 的 `get_attn_backend_for_role`（即 u7-l1 的「平台默认」第四级）。

一个贯穿始终的设计：**所有平台类的 AR/generation worker 方法都返回字符串全限定名**，调用方拿到字符串后再用 `resolve_obj_by_qualname` 反射加载。这样「选类」这一步不依赖任何硬件库就能完成。

#### 4.3.2 核心流程

**Worker 类解析流程**（AR / generation stage 启动时）：

1. stage 启动器拿到 `engine_args["worker_type"]`（`"ar"` 或 `"generation"`）。
2. 调 `current_omni_platform.get_omni_ar_worker_cls()`（或 generation）拿到字符串全限定名。
3. 写回 `engine_args["worker_cls"]`，后续 vLLM 的 `WorkerWrapperBase` 据此反射实例化。

**注意力后端默认选择流程**（diffusion 模型构造期，承接 u7-l1）：

1. `Attention.__init__` 声明 `role`，selector 在四级优先级前三级（per_role / role_category / default）都没命中时，落到第四级「平台默认」。
2. selector 调 `current_omni_platform.get_diffusion_attn_backend_cls(selected_backend=None, head_size=..., allow_trtllm_default=...)`。
3. 平台类根据自身硬件能力（算力、装的库）返回一个 `DiffusionAttentionBackendEnum.XXX.get_path()` 字符串。
4. selector 用 `@cache` 缓存结果，避免同一 `(backend_name, head_size, allow_trtllm_default)` 组合重复跑校验、重复打日志。

#### 4.3.3 源码精读

**消费者侧——selector 调平台**（u7-l1 的延续）：

[文件路径:vllm_omni/diffusion/attention/selector.py:L50-L69](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/attention/selector.py#L50-L69) `_cached_get_backend_cls` 调 `current_omni_platform.get_diffusion_attn_backend_cls(...)` 拿字符串再加载，并用 `@cache` 去重。

[文件路径:vllm_omni/diffusion/attention/selector.py:L145-L153](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/attention/selector.py#L145-L153) 「平台默认」第四级分支：前三级 spec 都为 None 时，传 `selected_backend=None` 给平台。

**消费者侧——stage 启动期要 worker 类**：

[文件路径:vllm_omni/engine/stage_init_utils.py:L152-L167](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/engine/stage_init_utils.py#L152-L167) `resolve_worker_cls` 按 `worker_type` 调 `get_omni_ar_worker_cls` / `get_omni_generation_worker_cls`，写回 `engine_args["worker_cls"]`。

[文件路径:vllm_omni/engine/arg_utils.py:L206-L210](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/engine/arg_utils.py#L206-L210) `OmniEngineArgs.__post_init__` 里同样的兜底解析。

**消费者侧——diffusion worker 的 init_device 用平台拿设备**：

[文件路径:vllm_omni/diffusion/worker/diffusion_worker.py:L273-L307](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/worker/diffusion_worker.py#L273-L307) `init_device` 用 `current_omni_platform.get_torch_device(rank)` / `set_device` / `init_diffusion_worker_vllm_config` 完成跨硬件设备初始化——这就是 u5-l3 讲的「init_device 经 current_omni_platform 完成跨硬件设备选择」的具体落点。

**CUDA 平台类**——默认平台，注意力后端逻辑最复杂：

[文件路径:vllm_omni/platforms/cuda/platform.py:L20-L35](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/platforms/cuda/platform.py#L20-L35) `class CudaOmniPlatform(OmniPlatform, CudaPlatformBase)` 多重继承，AR worker 指向 `GPUARWorker`、generation 指向 `GPUGenerationWorker`。

[文件路径:vllm_omni/platforms/cuda/platform.py:L204-L247](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/platforms/cuda/platform.py#L204-L247) CUDA 默认注意力后端的**降级瀑布**（`selected_backend is None` 分支），优先级从高到低：
   1. 数据中心 Blackwell（sm10.x）+ `head_size==128` + 装了 `trtllm-gen` kernel → `TRTLLM_ATTN`；
   2. Blackwell + cuDNN ≥ 9.5 → `CUDNN_ATTN`；
   3. Blackwell + 装了 flashinfer → `FLASHINFER_ATTN`；
   4. sm ≥ 80 + 装了 flash_attn → `FLASH_ATTN`；
   5. 兜底 → `TORCH_SDPA`。
   这正是 u7-l1 提到的「CUDA 上逐级降级 TRTLLM→CUDNN→FLASHINFER→FLASH_ATTN→SDPA」的源头代码。

**NPU 平台类**——换 worker、换图、换前向上下文：

[文件路径:vllm_omni/platforms/npu/platform.py:L24-L71](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/platforms/npu/platform.py#L24-L71) `class NPUOmniPlatform(OmniPlatform, NPUPlatform)`，`__init__` 里调用一系列 `apply_*_patch()` 做昇腾专属适配；AR worker 指向 `NPUARWorker`、generation 指向 `NPUGenerationWorker`——**与 CUDA 不同**。

[文件路径:vllm_omni/platforms/npu/platform.py:L121-L154](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/platforms/npu/platform.py#L121-L154) NPU 默认注意力后端：装了 `mindiesd` → `FLASH_ATTN`，否则 `TORCH_SDPA`（远比 CUDA 简单，因为 NPU 没有 TRTLLM/cuDNN 这些）。

[文件路径:vllm_omni/platforms/npu/platform.py:L234-L256](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/platforms/npu/platform.py#L234-L256) NPU 覆盖 `get_graph_wrapper_cls` → `ACLGraphWrapper`、`set_forward_context` → `set_ascend_forward_context`（参数改名 `aclgraph_runtime_mode`）。

**ROCm 平台类**——AR worker 复用 GPU 版本，注意力依赖 aiter：

[文件路径:vllm_omni/platforms/rocm/platform.py:L59-L66](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/platforms/rocm/platform.py#L59-L66) ROCm 的 AR/generation worker **复用** `GPUARWorker`/`GPUGenerationWorker`（与 CUDA 相同）。

[文件路径:vllm_omni/platforms/rocm/platform.py:L73-L131](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/platforms/rocm/platform.py#L73-L131) ROCm 默认注意力后端：`aiter` 库在 gfx942/gfx950 上可用 → `FLASH_ATTN`，否则 `TORCH_SDPA`。注意类 docstring 里特别说明：**AR** 注意力后端的覆盖逻辑并不在这个方法里，而在 `stage_init_utils.extract_legacy_stage_metadata`（因为 vLLM 自 v0.19.0 起 ROCm 默认 `ROCM_ATTN`，与 omni 兼容性不保证，故强制改回 `TRITON_ATTN`/`ROCM_AITER_FA`）。

**XPU 平台类**——有专属 worker：

[文件路径:vllm_omni/platforms/xpu/platform.py:L34-L40](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/platforms/xpu/platform.py#L34-L40) XPU 的 AR/generation worker 指向 `XPUARWorker`/`XPUGenerationWorker`（专属）。

[文件路径:vllm_omni/platforms/xpu/platform.py:L42-L73](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/platforms/xpu/platform.py#L42-L73) XPU 默认注意力后端：按设备架构能力判定是否 `FLASH_ATTN`，否则 `TORCH_SDPA`；并特意排除 Intel Max 1100/1550。

**MUSA 平台类**——复用 GPU worker，注意力要求算力 ≥ 3.1：

[文件路径:vllm_omni/platforms/musa/platform.py:L25-L32](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/platforms/musa/platform.py#L25-L32) MUSA 复用 `GPUARWorker`/`GPUGenerationWorker`。

[文件路径:vllm_omni/platforms/musa/platform.py:L43-L110](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/platforms/musa/platform.py#L43-L110) MUSA 默认注意力后端：算力 ≥ 3.1 + 装了 mate 包 → `FLASH_ATTN`，否则 `TORCH_SDPA`。

**后端枚举与覆盖机制**——所有平台返回的字符串都来自这个枚举，且支持运行时覆盖：

[文件路径:vllm_omni/diffusion/attention/backends/registry.py:L38-L67](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/attention/backends/registry.py#L38-L67) `DiffusionAttentionBackendEnum` 把每个后端名映射到默认类全限定名。

[文件路径:vllm_omni/diffusion/attention/backends/registry.py:L69-L87](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/attention/backends/registry.py#L69-L87) `get_path()` 会先查 `_DIFFUSION_ATTN_OVERRIDES` 覆盖表——这就是 OOT 平台能用 `@register_diffusion_backend` 把某个后端替换成自己实现的机制。

#### 4.3.4 代码实践

**实践目标**：在不实际切换硬件的前提下，用源码阅读填出「CUDA vs NPU」平台行为对照表，并验证「换 worker 类」发生在哪一行。

**操作步骤**：

1. 打开本讲 4.3.3 列出的五个平台文件。
2. 为 CUDA 与 NPU 两个平台，分别查表填出下列字段（见下方「需要观察的现象」中的表格骨架）。
3. 在 [vllm_omni/engine/stage_init_utils.py](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/engine/stage_init_utils.py) 里定位：当 `worker_type == "ar"` 时，CUDA 会拿到 `GPUARWorker`、NPU 会拿到 `NPUARWorker`——确认这一「分流」是由平台类的返回值决定的，而非 `if platform.is_npu()` 分支。
4.（可选验证）写一段**示例代码**直接对比两个类的返回值（不实例化、不触发探测，只读 classmethod）：

   ```python
   # 示例代码：仅读取 classmethod 返回的字符串，不触发硬件探测
   from vllm_omni.platforms.cuda.platform import CudaOmniPlatform
   from vllm_omni.platforms.npu.platform import NPUOmniPlatform
   for cls in (CudaOmniPlatform, NPUOmniPlatform):
       print(cls.__name__, "→", cls.get_omni_ar_worker_cls())
   ```

**需要观察的现象**——你应该能填出这样一张对照表（骨架，请自行补全）：

| 维度 | CudaOmniPlatform | NPUOmniPlatform |
|------|------------------|-----------------|
| `_omni_enum` | `CUDA` | `NPU` |
| 父类（硬件侧） | `CudaPlatformBase` | `NPUPlatform`（vllm-ascend） |
| `get_omni_ar_worker_cls()` | `...worker.gpu_ar_worker.GPUARWorker` | `...platforms.npu.worker.npu_ar_worker.NPUARWorker` |
| `get_omni_generation_worker_cls()` | `...gpu_generation_worker.GPUGenerationWorker` | `...npu_generation_worker.NPUGenerationWorker` |
| 默认 diffusion 注意力后端 | TRTLLM→CUDNN→FlashInfer→FA→SDPA 瀑布 | 装了 mindiesd→FA，否则 SDPA |
| `get_torch_device()` | `torch.device("cuda", rank)` | `torch.device("npu", rank)` |
| `get_graph_wrapper_cls()` | `CUDAGraphWrapper` | `ACLGraphWrapper` |
| `set_forward_context` | vLLM `set_forward_context` | `set_ascend_forward_context` |
| `supports_torch_inductor()` | `True` | `False` |
| `record_device_event()` | 直接 record | 先 `current_stream().synchronize()` 再 record |

**预期结果**：你会发现两个平台在「worker 类」「图包装器」「前向上下文」上**完全不同**，但它们都实现同一套 `OmniPlatform` 接口——这正是抽象层的意义：上层代码（selector、stage 启动器、init_device）只面向 `current_omni_platform` 写一次，硬件差异被收进子类。可选脚本的打印结果**待本地验证**（取决于 import 是否成功，NPU 类 import 需要 `vllm_ascend`）。

#### 4.3.5 小练习与答案

**练习 1**：ROCm 和 MUSA 的 `get_omni_ar_worker_cls()` 都返回 `GPUARWorker`，而 NPU/XPU 返回各自的专属 worker。这说明什么？

**参考答案**：说明「是否需要专属 worker」取决于硬件与 vLLM 默认 worker 的兼容程度。ROCm/MUSA 在 vLLM 里本质上仍走 CUDA-like 路径（设备类型也是 `cuda`），所以能直接复用 `GPUARWorker`；而 NPU（设备 `npu`）、XPU（设备 `xpu`）的设备语义和算子都不同，必须各自实现 worker 子类。这是「多重继承右边那一个」决定兼容性的体现。

**练习 2**：CUDA 的 `get_diffusion_attn_backend_cls` 有几百行，而 NPU 的只有十几行。这种复杂度差异合理吗？

**参考答案**：合理。CUDA 生态有 TRTLLM、cuDNN、FlashInfer、FlashAttention 等多种成熟 kernel，且要区分 Blackwell/Hopper/Ampere 不同代际的算力与软件栈版本，因此需要一个精细的降级瀑布来挑最优 kernel；NPU 生态目前主要靠 `mindiesd` 提供 FLASH_ATTN，选项少，逻辑自然简单。复杂度反映的是「可选 kernel 的丰富程度」，而非代码质量问题。

**练习 3**：如果一个 OOT 插件平台想把 `FLASH_ATTN` 后端换成自己的实现，该用哪个机制？

**参考答案**：用 [registry.py](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/attention/backends/registry.py) 提供的 `@register_diffusion_backend(DiffusionAttentionBackendEnum.FLASH_ATTN)` 装饰器，把自定义类写进 `_DIFFUSION_ATTN_OVERRIDES`。之后任何平台返回 `FLASH_ATTN.get_path()` 时，`get_path()` 会优先返回覆盖后的路径——这比改平台类更轻量。

---

## 5. 综合实践

**任务**：你被要求「把 vLLM-Omni 从 CUDA 迁移到 NPU」，但手头没有 NPU 机器。请仅凭源码，产出一份《平台迁移影响清单》，回答以下问题：

1. **启动期**：`current_omni_platform` 会怎么被解析成 `NPUOmniPlatform`？定位探测函数与唯一性校验代码。
2. **设备初始化**：diffusion worker 的 `init_device` 在 NPU 上会调用哪些不同的平台方法？（提示：`get_torch_device`、`set_device`、`init_diffusion_worker_vllm_config`、`init_diffusion_model_runner_runtime`）。
3. **AR stage**：AR worker 会从 `GPUARWorker` 换成什么？由哪行代码决定？
4. **注意力后端**：一个不指定后端的 diffusion 模型，在 NPU 上默认用哪个后端？依赖什么库？
5. **图捕获**：CUDA Graph 会失效吗？被什么替代？
6. **风险点**：列出至少 2 个 NPU 上「行为不同于 CUDA、可能踩坑」的地方（提示：`supports_torch_inductor`、`record_device_event` 的 HCCL 同步、AR 注意力后端不在平台方法里而在 `stage_init_utils`）。

**产出要求**：

- 每个问题给出对应的源码永久链接（行号）。
- 把第 4.3.4 的对照表扩展成「CUDA vs NPU 迁移差异表」。
- 标注哪些结论可以在 CUDA 机器上用「示例代码 + 源码阅读」验证，哪些只能「待本地 NPU 验证」。

**参考思路**：

- 第 1 题看 [platforms/__init__.py:L65-L78](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/platforms/__init__.py#L65-L78) 与 [L132-L164](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/platforms/__init__.py#L132-L164)。
- 第 3 题看 [stage_init_utils.py:L152-L167](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/engine/stage_init_utils.py#L152-L167) 调 [npu/platform.py:L65-L67](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/platforms/npu/platform.py#L65-L67)。
- 第 6 题风险点：NPU `supports_torch_inductor()=False`（不能用 torch.compile）、`record_device_event` 必须先同步默认流（HCCL 跨流坑）、ROCm 同理的 AR 后端覆盖在 NPU 是否也有类似旁路（需查 `extract_legacy_stage_metadata`）——后两者**待本地 NPU 验证**。

---

## 6. 本讲小结

- `OmniPlatform(OmniPlatform, Platform)` 是所有平台类的契约，用 `_omni_enum` 自报家门、用 `is_xxx()` 谓词屏蔽 `isinstance`，把「设备句柄 / worker 类 / 注意力后端 / 图与编译」四类硬件差异收进一套接口。
- `current_omni_platform` 是**惰性单例**：靠模块级 `__getattr__` 守卫，第一次访问才探测；五个内置探测函数全部跑完后做唯一性校验（≥2 个就 `RuntimeError`），OOT 插件优先级高于内置，探不到则兜底 `UnspecifiedOmniPlatform`。
- 消费者有三处：注意力 selector（u7-l1 第四级「平台默认」）、stage 启动器 `resolve_worker_cls`、diffusion worker `init_device`——它们都只面向 `current_omni_platform` 写一次。
- 五个平台子类在「worker 类」「默认注意力后端」「图包装器」上差异显著：CUDA/ROCm/MUSA 复用 `GPUARWorker`，NPU/XPU 各有专属 worker；CUDA 注意力后端是 TRTLLM→CUDNN→FlashInfer→FA→SDPA 的瀑布，NPU 仅 mindiesd→FA / SDPA，ROCm 靠 aiter，MUSA 要求算力 ≥ 3.1。
- 所有平台方法都返回**字符串全限定名**而非类对象，由 `resolve_obj_by_qualname` 按需懒加载——这是「探测阶段不拖入重依赖」的关键，与 u2-l1 的懒加载动机一致。
- 后端枚举 `DiffusionAttentionBackendEnum` 支持 `@register_diffusion_backend` 运行时覆盖，让 OOT 平台无需改平台类即可替换某个后端实现。

---

## 7. 下一步学习建议

- **回到 u7-l1**：现在再看注意力 selector 的「平台默认」第四级，你应该能完全看懂它调用的 `get_diffusion_attn_backend_cls` 在不同硬件上返回什么。建议重读 [selector.py](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/attention/selector.py) 的四级优先级，体会「配置优先、平台兜底」的分工。
- **u8-l1 量化体系**：量化也是平台相关的（u8-l1 讲过 `get_quant_method` 按「在线/离线 × 硬件平台」十字交叉分派），可与本讲对照阅读 [vllm_omni/quantization/factory.py](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/quantization/factory.py) 的平台委托逻辑。
- **NPU 深入**：若你关心昇腾，可继续阅读 [vllm_omni/platforms/npu/](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/platforms/npu/) 下的 `worker/`、`layers/fused_moe.py`、`models/`，看 `NPUOmniPlatform.__init__` 里那一串 `apply_*_patch` 具体改了什么。
- **扩展实践**：尝试写一个最小的 OOT 平台插件（注册到 `vllm_omni.platform_plugins` entry point），让它返回一个自定义 worker 类名，观察 `resolve_current_omni_platform_cls_qualname` 是如何优先选中它的——这能验证你对「OOT 优先」机制的理解。
