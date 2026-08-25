# NPUPlatform：设备声明、HCCL 通信与配置改写

## 1. 本讲目标

上一讲（u2-l1）我们搞清楚了 omni-npu 是如何被 vLLM「发现」的：`plugin()` 探测到 `torch_npu` 后返回类路径字符串 `omni_npu.platform.NPUPlatform`。本讲就精读这个类本身。读完本讲，你应该能够：

1. 说出 `NPUPlatform` 声明的设备名（`npu`）、dispatch key（`PrivateUse1`）与分布式后端（`hccl`），并解释每一项向 vLLM 传递了什么信息。
2. 逐条追踪 `check_and_update_config` 对 vLLM 默认配置的全部修改项：worker 类、block_size、编译 pass 开关、图模式切换。
3. 理解 `get_attn_backend_cls` 的三分支后端名解析逻辑，以及 `NPU_ATTENTION_BACKEND` 注册表如何允许第三方插件覆盖内置注意力后端。

## 2. 前置知识

### 2.1 平台抽象层（Platform Interface）

vLLM 内部代码大量写着 `current_platform.xxx`：它想知道有几张卡、显存还剩多少、该用哪个通信后端、该起哪个 worker 进程。为了让同一套引擎代码同时支持 CUDA / ROCm / NPU / CPU，vLLM 定义了 `Platform` 基类（位于镜像内的 `vllm/platforms/interface.py`），把所有「设备相关」的问题抽象成一组类方法。子类只需覆写这些方法，引擎其余部分即可原封不动地运行在新硬件上。

`NPUPlatform` 就是这份「答题卡」的 NPU 版本。它回答的核心问题只有一个：**vLLM 默认按 GPU 假设写的配置，在昇腾 NPU 上应该改成什么？**

### 2.2 dispatch key 与 PrivateUse1

PyTorch 用「dispatch key」来路由算子：一个 tensor 运算进来，框架根据 tensor 的设备类型选择对应的算子实现。CUDA 有官方 key，而第三方加速器（昇腾、寒武纪等）通过 `torch_npu` 这类扩展包注册到 PyTorch 预留的 `PrivateUse1` 通道上。所以 `NPUPlatform` 声明 `dispatch_key = "PrivateUse1"`，意思是「我的算子走 PyTorch 的私有扩展通道」。

### 2.3 HCCL 集合通信

多卡训练/推理时，各卡之间要做 all_reduce、all_gather 等集合通信。NVIDIA 生态用 NCCL，昇腾生态对应物是 HCCL（Huawei Collective Communication Library）。`torch.distributed` 以 `hccl` 为 backend 后，底层通信就由 HCCL 执行——这是理解 `dist_backend = "hccl"` 与 `NPUCommunicator` 的关键。

### 2.4 注册表模式与装饰器

「注册表模式」指维护一个全局字典 `名字 -> 类路径`，需要时按名字查表。`@register_attention_backend("NPUDSA")` 这样的装饰器在类定义时自动把类写进字典。它带来的好处是：调用方（platform）只知道名字，不必 import 具体模块；第三方还能用相同名字覆盖内置实现。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `components/omni-npu/src/omni_npu/platform.py` | 本讲主角。`NPUPlatform` 类 + `ConfigUpdater` 辅助类，声明设备属性并改写 vLLM 配置 |
| `components/omni-npu/src/omni_npu/distributed/communicator.py` | `NPUCommunicator`：基于 torch.distributed（HCCL 后端）的设备通信器，实现 all_reduce / all_gather 等集合通信 |
| `components/omni-npu/src/omni_npu/attention/backends/utils.py` | 注意力后端注册表：`NPU_ATTENTION_BACKEND` 字典、`@register_attention_backend` 装饰器、插件覆盖机制 |
| `components/omni-npu/src/omni_npu/attention/backends/__init__.py` | 导入全部内置后端后触发插件覆盖，并把覆盖后的类重新绑定回模块名 |
| `components/omni-npu/src/omni_npu/attention/backends/attention.py` / `dsa.py` | 内置后端注册示例：`VLLM_NPU_ATTN` 与 `NPUDSA` |
| `components/omni-npu/src/omni_npu/vllm_plugin.py` | 上一讲讲过的入口函数，返回 `NPUPlatform` 类路径，是本讲的「来路」 |
| `tools/ansible/92B/omni_infer_server_template_performance1P1D_92B_bf16_open.yml` | 部署侧证据：`VLLM_PLUGINS` 环境变量在此设置，最终激活本讲的全部机制 |

## 4. 核心概念与源码讲解

本讲拆成 4 个最小模块：**Platform 接口与设备声明**、**配置改写**、**HCCL 通信器**、**注意力后端注册表**。

### 4.1 Platform 接口与设备声明

#### 4.1.1 概念说明

`NPUPlatform` 继承 vLLM 的 `Platform` 基类，是 omni-npu 与 vLLM 之间的「合同履行方」。它在类体顶部用 6 个类属性完成最基本的设备声明；其余几十个方法则按 vLLM 的调用时机逐个回答「这件事在 NPU 上该怎么做」。这一模块只看「声明」部分——它决定了 vLLM 如何称呼和识别这款设备。

#### 4.1.2 核心流程

```text
vllm 启动
  └─ 加载 VLLM_PLUGINS 指定的插件（部署模板 ansible yml L79）
       └─ plugin() 探测 torch_npu 成功
            └─ 返回 "omni_npu.platform.NPUPlatform"（类路径字符串）
                 └─ vLLM 延迟 import 并实例化 NPUPlatform
                      └─ 引擎各处通过 current_platform.<方法> 拿到 NPU 答案
```

#### 4.1.3 源码精读

先看来路——入口函数在探测成功时返回的正是本讲的类：

[components/omni-npu/src/omni_npu/vllm_plugin.py:L17-L26](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/vllm_plugin.py#L17-L26)

这段代码尝试 `import torch_npu`：只要能导入就返回 `omni_npu.platform.NPUPlatform`；即便没有独立 torch_npu 包，只要 `torch.npu` 存在也认定 NPU 环境；两者都失败才返回 `None` 让位给其他平台。

接着看类声明与 6 个设备属性：

[components/omni-npu/src/omni_npu/platform.py:L52-L64](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/platform.py#L52-L64)

逐项解读这张「身份证」：

| 属性 | 值 | 含义 |
| --- | --- | --- |
| `_enum` | `HUAWEI_NPU` 或 `OOT` | 平台枚举。若 vLLM 内置了 HUAWEI_NPU 枚举就用它；否则退回 OOT（out-of-tree，外部插件平台），这是对 vLLM 版本差异的防御 |
| `device_name` / `device_type` | `"npu"` | torch.device 的类型名，vLLM 据此构造 `torch.device("npu")` |
| `dispatch_key` | `"PrivateUse1"` | PyTorch 第三方加速器算子分发通道 |
| `ray_device_key` | `"NPU"` | 使用 ray 集群调度时的设备资源名 |
| `dist_backend` | `"hccl"` | torch.distributed 集合通信后端 |
| `device_control_env_var` | `"ASCEND_RT_VISIBLE_DEVICES"` | 相当于 NPU 版的 `CUDA_VISIBLE_DEVICES`，控制进程可见哪些卡 |

`try/except AttributeError` 这个小细节值得注意：它让 omni-npu 可以适配「vLLM 认识 NPU」和「vLLM 完全不认识 NPU」两种宿主版本。

除声明外，还有一组「设备基础能力」方法，形式都很短：

[components/omni-npu/src/omni_npu/platform.py:L70-L80](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/platform.py#L70-L80)

`set_device` / `get_device_name` / `inference_mode` 把 torch 的 CUDA 习惯调用翻译成 `torch.npu.*`。类似地，L97-L108 的三个方法 `get_current_memory_usage`、`device_count`、`mem_get_info` 也只是转调 `torch.npu` 的同名能力，供 vLLM 做显存预算与调度决策。

还有一个「伪装者」方法非常能体现适配层的灵活性：

[components/omni-npu/src/omni_npu/platform.py:L110-L124](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/platform.py#L110-L124)

`is_cuda_alike` 默认语义是「是否类 CUDA 设备」。NPU 本不属于 CUDA/ROCM，但 vLLM 少数代码路径（这里精确点名了 `vllm/v1/worker/utils.py` 的 `bind_kv_cache` 调用者）只有走「cuda alike」分支才能正确绑定 KV cache。于是该方法用 `traceback` 检查调用栈：若是这两处特定调用点就返回 `True`，其余情况保持诚实。这是零侵入适配中「定向撒谎」的典型技巧——既不改 vLLM 源码，又能绕过设备类型硬编码。

#### 4.1.4 代码实践

**实践目标**：验证 `NPUPlatform` 的声明属性，并体会「类路径字符串 + 延迟 import」机制。

**操作步骤**（需要在已部署容器或任何装有 torch_npu 与 omni-npu 的环境执行；无 NPU 机器时完成步骤 3 的源码标注即可）：

1. 进容器：`docker exec -it <p容器名> bash`。
2. 执行下面命令（示例代码，非项目原有）：

```bash
python -c "
from omni_npu.platform import NPUPlatform
print('device_type =', NPUPlatform.device_type)
print('dispatch_key =', NPUPlatform.dispatch_key)
print('dist_backend =', NPUPlatform.dist_backend)
print('device_control_env_var =', NPUPlatform.device_control_env_var)
"
```

3. 源码标注：在 `platform.py` L59-L64 旁为每个属性写一行注释，说明「vLLM 拿到这个值后会用它做什么」（例如 `dist_backend` 会被用于初始化 torch.distributed 进程组）。

**需要观察的现象**：步骤 2 应打印出 `npu` / `PrivateUse1` / `hccl` / `ASCEND_RT_VISIBLE_DEVICES`。

**预期结果**：属性值与本讲表格一致；若 `import omni_npu` 失败，说明当前环境未安装插件（回到 u1-l2 的 `bash build/build.sh -m omni-npu` 或使用部署镜像）。容器内实际输出**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `_enum` 要用 try/except 包起来？如果直接写 `_enum = PlatformEnum.HUAWEI_NPU` 会有什么风险？

**答案**：`PlatformEnum.HUAWEI_NPU` 只有在较新的 vLLM 中才存在。若宿主 vLLM 没有这个枚举成员，类定义阶段就会抛 `AttributeError`，导致整个插件 import 失败——连「降级为 OOT 平台」的机会都没有。try/except 让 omni-npu 同时兼容认识与不认识 NPU 的 vLLM 版本。

**练习 2**：`device_control_env_var = "ASCEND_RT_VISIBLE_DEVICES"` 在部署中有什么实际用途？

**答案**：它告诉 vLLM「控制本进程可见设备的环境变量名」。vLLM 在切分张量并行/数据并行时，会通过设置该变量把不同 worker 进程绑定到不同 NPU 卡，作用等同于 CUDA 生态的 `CUDA_VISIBLE_DEVICES`。

**练习 3**：`is_cuda_alike` 为什么要检查调用栈而不是永远返回 True？

**答案**：无差别返回 True 会让 vLLM 在所有路径上都把 NPU 当 CUDA 处理，可能触发不存在的 CUDA API（如 NCCL、cudaGraph 原生调用）。定向检查调用栈只对确知安全的两处调用点「放行」，其余路径保持真实设备语义，把影响面压到最小。

### 4.2 check_and_update_config：对 vLLM 默认配置的修正

#### 4.2.1 概念说明

vLLM 的全部运行参数汇总在一个 `VllmConfig` 对象里，其中大量默认值是按 GPU 假设写死的。`Platform.check_and_update_config` 是 vLLM 留给平台插件的「改卷机会」：在配置组装完成之后、引擎真正启动之前，平台可以把不适用于自己的默认值改掉。`NPUPlatform` 对它的实现集中在 [components/omni-npu/src/omni_npu/platform.py:L143-L163](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/platform.py#L143-L163)，另把图编译相关的部分委托给 `ConfigUpdater`。

#### 4.2.2 核心流程

```text
vLLM 解析 CLI/配置 → 组装 VllmConfig
  └─ current_platform.check_and_update_config(vllm_config)
       ├─ ConfigUpdater.update_vllm_config
       │    ├─ 挂载 npu_compilation_config（GE 图编译开关）
       │    └─ torch 版本过低则强制关闭 ge 图
       ├─ parallel_config.worker_cls ← NPUWorker 类路径
       ├─ cache_config.block_size ← 128（仅当用户未指定）
       ├─ 三个 CUDA 融合 pass 开关 ← False
       └─ 图模式为 FULL/FULL_DECODE_ONLY → splitting_ops 清空
```

#### 4.2.3 源码精读

主入口：

[components/omni-npu/src/omni_npu/platform.py:L143-L163](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/platform.py#L143-L163)

逐条列出这张「改写清单」：

1. **worker 类替换**（L147-L148）：`parallel_config.worker_cls = "omni_npu.worker.npu_worker.NPUWorker"`。vLLM 默认起 GPU 版 worker，这里改成字符串路径，vLLM 在需要时才 import。这是下一讲（u2-l3）的主角。
2. **KV 块大小**（L150-L152）：`block_size` 未指定时设为 128。KV Cache 按 block 分页，128 恰与 NPU 注意力算子偏好的页粒度一致（在 u4 的 KV 传输、u7 的主机缓存中会反复见到这个数字）。
3. **关闭三个融合 pass**（L154-L156）：`fuse_norm_quant` / `fuse_act_quant` / `fuse_attn_quant` 是 CUDA torch.compile 的融合优化，NPU 图编译暂不支持，置 False 防止生成非法图。
4. **整图模式切换**（L158-L163）：当 vLLM 的 `cudagraph_mode` 为 `FULL` 或 `FULL_DECODE_ONLY` 时，清空 `splitting_ops`——即不做分段（piecewise），整图交给 ACL Graph 捕获，日志会打出 "using only ACL Graph Mode"。

配置改写的第一项委托给了 `ConfigUpdater`：

[components/omni-npu/src/omni_npu/platform.py:L20-L49](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/platform.py#L20-L49)

`update_vllm_config` 做三件事：给 `vllm_config` 动态挂一个 `npu_compilation_config` 属性（`NPUCompilationConfig` 实例）；根据环境变量 `TORCH_COMPILE_GE` 决定是否启用 GE（图引擎）编译；再从 vLLM 的 `additional_config` 里读取用户通过 `--additional-config` 传入的 `graph_model_compile_config` 覆盖默认值。`_handle_graph_mode` 则在 torch 版本不支持 dynamo 时兜底关闭 GE 图。图编译细节属于 u5-l2 的内容，这里只需记住入口。

另一个值得关注的钩子是 `pre_register_and_update`：

[components/omni-npu/src/omni_npu/platform.py:L126-L141](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/platform.py#L126-L141)

其 docstring 明确说明：它在 `VllmConfig` 初始化、CLI 参数解析**之前**被调用，用于提前注册平台专属内容——这里 import `omni_npu.layers`，确保 NPU 自定义层（RMSNorm、激活函数等，见 u3-l4）在配置解析前就完成注册。

#### 4.2.4 代码实践

**实践目标**：把 `check_and_update_config` 的每条改写与部署模板中的启动参数对应起来。

**操作步骤**：

1. 打开 [tools/ansible/92B/omni_infer_server_template_performance1P1D_92B_bf16_open.yml:L79](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/tools/ansible/92B/omni_infer_server_template_performance1P1D_92B_bf16_open.yml#L79)，确认 `export VLLM_PLUGINS="omni-npu,omni_npu_patches,omni_custom_models"`——`omni-npu` 正是激活本节机制的插件名。
2. 在同一模板中搜索 `block-size`、`cudagraph-mode`、`additional-config` 等关键字，记录部署侧是否显式指定了这些参数。
3. 建立对照表（示例代码，非项目原有）：

| 模板中的参数 | platform.py 中的处理 | 生效值 |
| --- | --- | --- |
| 未写 block-size | L151-L152 补默认 | 128 |
| 显式写了 block-size | `if block_size is None` 不成立 | 保持用户值 |
| `--cudagraph-mode full` | L160-L163 清空 splitting_ops | 整图 ACL Graph |

**需要观察的现象**：能明确回答「哪些值来自用户、哪些值来自平台补默认、哪些值被平台强制覆盖」。

**预期结果**：`block_size` 是「用户优先、平台兜底」；三个融合 pass 是「平台强制关闭」；`worker_cls` 是「平台强制替换」。表中第三列的具体取值在真实启动日志中的体现**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：如果用户在启动命令里显式传了 `--block-size 64`，最终生效值是多少？为什么？

**答案**：64。因为 L151 的条件是 `cache_config.block_size is None`，只有用户未指定（None）时平台才补 128；显式传入的值不会被覆盖。

**练习 2**：为什么 L154-L156 要关闭 `fuse_norm_quant` 等三个 pass，而不是留着让编译器自己判断？

**答案**：这三个 pass 是面向 CUDA Inductor 后端设计的融合规则。NPU 走自己的图编译路径（ACL Graph / GE），若这些 pass 被启用，可能在图中插入 NPU 不支持的融合算子或调度节点，导致编译失败或结果错误。平台层提前显式关闭，比让下游报错更容易排查。

**练习 3**：`pre_register_and_update` 与 `check_and_update_config` 的调用时机有何不同？各自适合做什么？

**答案**：前者在 CLI 解析和 VllmConfig 初始化**之前**调用，适合「注册」类动作（如注册自定义层、量化方案，让后续配置解析能识别相关参数）；后者在配置组装**之后**调用，适合「修正」类动作（替换默认值、关闭不支持的特性）。二者顺序不可颠倒。

### 4.3 NPUCommunicator：基于 HCCL 的设备通信器

#### 4.3.1 概念说明

vLLM 的多卡通信由 `GroupCoordinator` 统一管理，其中「设备侧集合通信」委托给一个 communicator 类——平台通过 `get_device_communicator_cls` 告诉它用哪个。`NPUPlatform` 返回 `omni_npu.distributed.communicator.NPUCommunicator`。这个类继承 vLLM 的 `CudaCommunicator`（复用其进程组管理逻辑），但把所有集合通信原语重新实现为对 `torch.distributed` 的直接调用——在 NPU 上，torch.distributed 的 backend 正是上一模块声明的 `hccl`。

为什么需要它？因为 vLLM 的 CUDA communicator 会调用 NCCL 专属 API，NPU 上不存在；而 MoE 专家并行需要的 all_to_all、变长 all_gatherv 等语义，HCCL 与 NCCL 的封装层也各有差异，必须有一层显式适配。

#### 4.3.2 核心流程

```text
vLLM 初始化进程组（backend=hccl）
  └─ GroupCoordinator 需要设备通信器
       └─ platform.get_device_communicator_cls() → "…NPUCommunicator"
            └─ NPUCommunicator(cpu_group, device, device_group, …)
                 ├─ super().__init__ 复用 CudaCommunicator 的组管理
                 └─ self.dist_module = torch.distributed
模型前向中的 all_reduce / all_gather / all_to_all …
  └─ 全部转调 self.dist_module.xxx(..., group=self.device_group)  ← HCCL 执行
```

#### 4.3.3 源码精读

平台侧的指定只有一行：

[components/omni-npu/src/omni_npu/platform.py:L170-L173](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/platform.py#L170-L173)

`get_device_communicator_cls` 返回 `NPUCommunicator` 的类路径字符串，注释直言「把 vLLM 指向我们基于 HCCL 的通信器实现」。

通信器的构造与自检：

[components/omni-npu/src/omni_npu/distributed/communicator.py:L18-L40](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/distributed/communicator.py#L18-L40)

构造函数先调用父类 `CudaCommunicator.__init__` 继承进程组簿记（world_size、rank 映射等），再保存 `torch.distributed` 模块引用；若 `torch.npu` 不存在则立刻抛错——把「torch_npu 没装好」这类环境问题在通信器创建时就暴露出来，而不是等到第一次集合通信时才报晦涩错误。

三个最常用的原语：

[components/omni-npu/src/omni_npu/distributed/communicator.py:L54-L84](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/distributed/communicator.py#L54-L84)

- `all_reduce`（L54-L56）：原地归约后返回输入张量，一行转调。
- `all_gather`（L58-L70）：先 `all_gather_into_tensor` 拼成 `(world_size × N, …)`，再 reshape + `movedim` 把 world_size 维挪到指定 `dim`。设输入在第 0 维大小为 \( n \)、world_size 为 \( w \)，输出该维即为 \( w \cdot n \)。
- `reduce_scatter`（L72-L84）：world_size 为 1 时直接返回（省一次通信）；否则把目标维挪到第 0 维、`contiguous` 后调用 `reduce_scatter_tensor` 均分。

torch.distributed 没有 `all_gatherv`（变长聚合）API，这里用循环 broadcast 模拟：

[components/omni-npu/src/omni_npu/distributed/communicator.py:L109-L142](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/distributed/communicator.py#L109-L142)

当 `sizes is None` 时走高效的 `all_gather_into_tensor`；当各 rank 张量长度不同时，退化为「每个 rank 广播自己的份、全体拼接」的慢路径（L132-L141）。这正对应 MoE 专家并行中各 rank token 数不等的场景（u3-l3 会用到）。

EP 场景还有一个模型级准备钩子：

[components/omni-npu/src/omni_npu/distributed/communicator.py:L42-L51](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/distributed/communicator.py#L42-L51)

`prepare_communication_buffer_for_model` 在本通信器是 EP（专家并行）通信器时，遍历模型里所有 `FusedMoE` / `SharedFusedMoE` / `NPUFusedMoE` / `NPUSharedFusedMoE` 模块并调用其 `maybe_init_modular_kernel()`，为 all_to_all 通信预初始化缓冲区。

最后看两个「诚实的不作为」：

[components/omni-npu/src/omni_npu/distributed/communicator.py:L263-L283](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/distributed/communicator.py#L263-L283)

`destroy` 无需清理（HCCL 由 torch.distributed 生命周期管理），`dispatch` / `combine` 直接原样返回——基类留的钩子，NPU 路径暂不使用。

#### 4.3.4 代码实践

**实践目标**：不依赖 NPU，用 CPU 上的 torch 验证 `all_gather` 输出 reshape 逻辑（L58-L70）的正确性。

**操作步骤**：

1. 在任何装有 torch 的机器上运行以下脚本（示例代码，非项目原有）：

```python
import torch

# 模拟 world_size=2 的 all_gather 结果拼接段
# 假设两个 rank 各持有形状 [3, 4] 的张量
a = torch.arange(12).reshape(3, 4)
b = (torch.arange(12) + 100).reshape(3, 4)
world_size = 2
dim = 0

# all_gather_into_tensor 的直接产物：按 rank 顺序首尾拼接
output_tensor = torch.cat([a, b], dim=0)
input_size = a.size()

# 以下复刻 communicator.py L65-L69 的三行 reshape
output_tensor = output_tensor.reshape((world_size,) + input_size)
output_tensor = output_tensor.movedim(0, dim)
output_tensor = output_tensor.reshape(
    input_size[:dim] + (world_size * input_size[dim],) + input_size[dim + 1:]
)
print(output_tensor.shape)   # 期望 [6, 4]
print(output_tensor)
# 对照「正确答案」： interleaved 语义应等价于 torch.cat([a, b], dim=0)
print(torch.equal(output_tensor, torch.cat([a, b], dim=dim)))
```

2. 把 `dim` 改成 `1` 再跑一次，观察 movedim 的作用。

**需要观察的现象**：`dim=0` 时输出形状 `[6, 4]` 且与 `torch.cat` 结果相等；`dim=1` 时形状变为 `[3, 8]`，两个 rank 的数据沿列交错排布。

**预期结果**：验证 reshape 三部曲确实把「按 rank 拼接」转换成「沿目标维聚合」的正确语义。此实验纯 CPU 可跑，不涉及 HCCL；真实 NPU 上的通信耗时与数值**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：`NPUCommunicator` 为什么继承 `CudaCommunicator` 而不是从零实现？

**答案**：父类承担了大量与硬件无关的簿记工作（进程组管理、rank 映射、world_size 计算、通信量统计等），这些逻辑对 NPU 同样成立。继承后子类只需替换真正设备相关的集合通信原语，代码量与出错面都最小。

**练习 2**：阅读 L86-L107 的 `reduce_scatterv`，注释声称「变长不支持时回退标准 reduce_scatter」，但 L103-L106 两个分支的代码完全相同。这说明了什么？

**答案**：这是一个尚未完成（或刻意保守）的实现：变长路径目前仍按均分语义调用 `reduce_scatter_tensor`，只有当 `sizes` 恰好均分时结果才正确。注释与实现不一致是阅读信号——若上游传入非均分 sizes，此处是潜在 bug 点，也提示真实变长场景可能由其他路径（如 all_to_all）承担。

**练习 3**：`dispatch` / `combine` 为什么是恒等函数还要显式定义？

**答案**：vLLM 基类把这两个方法作为 MoE 分发/合并的扩展钩子调用。显式定义为恒等，既声明「NPU 路径不在此层做额外通信」，也避免子类意外继承到父类的 CUDA 行为——这是一种用空实现表达设计决策的常见手法。

### 4.4 注意力后端注册表与插件覆盖

#### 4.4.1 概念说明

注意力是推理引擎中设备差异最大的部分。vLLM 通过 `Platform.get_attn_backend_cls` 询问平台该用哪个注意力后端类。omni-npu 的做法分两层：

1. **名字解析**：`NPUPlatform.get_attn_backend_cls` 根据 vLLM 传来的 `attn_selector_config`（是否 MLA、是否稀疏注意力）从三个内置名中选一个。
2. **注册表查询**：拿名字去 `NPU_ATTENTION_BACKEND` 全局字典查类路径。第三方包可通过 entry point（`omni.attention_backends` 组）注册同名后端实现覆盖。

这样内置实现与外部增强解耦：omni-proxy 式的「宿主稳定、插件增强」结构在这里又一次出现。

#### 4.4.2 核心流程

```text
vLLM 需要创建注意力后端
  └─ platform.get_attn_backend_cls(selected_backend, attn_selector_config)
       ├─ use_mla 且 use_sparse → "NPUDSA"     （openPangu 稀疏注意力，u3-l2）
       ├─ use_mla 仅 → "NPUMLA"
       └─ 否则 → "VLLM_NPU_ATTN"               （通用 NPU 注意力）
       └─ get_attention_backend(name) 查注册表 NPU_ATTENTION_BACKEND
            ↑ 注册来源① import 时 @register_attention_backend 装饰器写入
            ↑ 注册来源② entry point 组 omni.attention_backends 的插件覆盖
```

#### 4.4.3 源码精读

名字解析与注册表查询：

[components/omni-npu/src/omni_npu/platform.py:L175-L193](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/platform.py#L175-L193)

注意两个要点：其一，vLLM 传入的 `selected_backend` 参数被**忽略**——平台完全按 `attn_selector_config.use_mla / use_sparse` 自行决策，这是对 vLLM 后端选择逻辑的显式接管；其二，返回的不是硬编码类名，而是先经 `get_attention_backend(backend_name)` 查表（L192 注释："Query registry first (allows plugins to override)"），从而给插件留下覆盖空间。

注册表本体与装饰器：

[components/omni-npu/src/omni_npu/attention/backends/utils.py:L20-L23](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/attention/backends/utils.py#L20-L23)

`NPU_ATTENTION_BACKEND` 就是一个「名字 → 类路径字符串」的字典（L20），与 u2-l1 讲过的 entry point「模块：属性」字符串风格一致——延迟 import，避免循环依赖。

[components/omni-npu/src/omni_npu/attention/backends/utils.py:L182-L202](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/attention/backends/utils.py#L182-L202)

`register_attention_backend(backend)` 返回装饰器：被装饰的类必须实现静态方法 `reshape_kv_cache`（NPU KV Cache 是 NZ 格式，每个后端都要能整备自己的缓存布局，否则注册时直接抛 `NotImplementedError`），然后把 `模块.类名` 写入注册表。`get_attention_backend(name)` 则是一行查表。

内置后端的注册实例：

[components/omni-npu/src/omni_npu/attention/backends/attention.py:L143-L153](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/attention/backends/attention.py#L143-L153)

通用后端：常量 `VLLM_NPU_ATTN` 在同文件 [L50](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/attention/backends/attention.py#L50) 定义，`NPUAttentionBackend` 被装饰注册。

[components/omni-npu/src/omni_npu/attention/backends/dsa.py:L52-L56](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/attention/backends/dsa.py#L52-L56)

稀疏注意力后端：`NPUDSABackend` 以名字 `"NPUDSA"` 注册（继承 MLA 公共基类，细节在 u3-l2 展开）。此外 `mla.py` L75 注册了 `NPUMLA`，`mome.py` L41 注册了 `NPUPanguMome`。

插件覆盖链路有三段。第一段，扫描 entry point 建立插件映射（结果缓存）：

[components/omni-npu/src/omni_npu/attention/backends/utils.py:L132-L160](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/attention/backends/utils.py#L132-L160)

每个 `omni.attention_backends` 组的 entry point 被加载后按其 `get_name()` 返回值索引——同名即可覆盖内置实现。

第二段，覆盖决策（含逃生门）：

[components/omni-npu/src/omni_npu/attention/backends/utils.py:L230-L286](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/attention/backends/utils.py#L230-L286)

`apply_plugin_overrides` 先快照内置注册表（L256，防止加载插件时其装饰器先污染快照），再逐名字比对：插件存在且类路径不同就替换注册表项并记入 `overrides`。环境变量 `DISABLE_PLUGIN_BACKENDS`（如 `NPUDSA,NPUMLA`，见 L163-L179 的 `_is_plugin_disabled`）可按名字禁用覆盖，用于排查插件问题。

第三段，包初始化时触发并重绑定模块名：

[components/omni-npu/src/omni_npu/attention/backends/__init__.py:L21-L37](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/attention/backends/__init__.py#L21-L37)

`import omni_npu.attention.backends` 时：L6-L15 先 import 四个内置后端模块（触发各自装饰器完成注册），L22 调用 `apply_plugin_overrides()`，L30-L37 再把被覆盖的类**写回**原模块的命名空间——保证无论外部代码用 `from …backends import NPUDSABackend` 还是 `from …backends.dsa import NPUDSABackend`，拿到的都是插件版本。顺序不能颠倒：插件模块 import 基类，若基类尚未注册完就加载插件会造成循环导入（L233-L236 注释明确说明）。

#### 4.4.4 代码实践

**实践目标**：观察注册表的真实内容与插件覆盖是否发生。

**操作步骤**（需在部署容器内执行；无环境则做源码推演）：

1. 容器内运行（示例代码，非项目原有）：

```bash
python -c "
from omni_npu.attention.backends.utils import (
    NPU_ATTENTION_BACKEND, get_available_backends)
print(get_available_backends())
for k, v in NPU_ATTENTION_BACKEND.items():
    print(f'{k:16s} -> {v}')
"
```

2. 再运行 `pip list | grep -i omni` 查看是否有第三方包注册了 `omni.attention_backends` entry point。
3. 源码推演（任何环境可做）：在 `get_attn_backend_cls` 的三个分支旁分别注明「openPangu-2.0 走哪个分支」。提示：openPangu-2.0 使用 DSA 稀疏注意力（见 u1-l1 与 u3-l2），对应 `use_mla=True, use_sparse=True`。

**需要观察的现象**：步骤 1 打印的名字集合应包含 `VLLM_NPU_ATTN`、`NPUDSA`、`NPUMLA`（可能还有 `NPUPanguMome`），每个名字映射到一个 `omni_npu.…` 类路径；若无插件包，所有路径都应指向 `omni_npu` 内部模块。

**预期结果**：基础部署下注册表全部为内置实现；若步骤 2 发现第三方包，则对应名字的路径会指向该包。容器内实际输出**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：`get_attn_backend_cls` 忽略 vLLM 传入的 `selected_backend`，自选后端名。这种「接管」有什么好处与风险？

**答案**：好处是 NPU 后端集合与 vLLM 内置后端名（FLASH_ATTN、FLASHINFER 等）并非一一对应，强行映射只会失真；按 use_mla/use_sparse 重新决策能精确匹配 NPU 实现。风险是 vLLM 未来新增的选择维度（如新注意力特性开关）会被静默忽略，需要 omni-npu 跟进维护这个分支逻辑。

**练习 2**：`register_attention_backend` 为什么强制要求被注册类实现 `reshape_kv_cache`？

**答案**：NPU 的 KV Cache 使用 NZ 等特殊内存排布（参见 utils.py L26-L36 的 `cache_fit_shape`，16 位类型 NZ 维为 16），不同注意力算子要求的布局不同。平台层无法预知每种布局，于是把「整备自己的缓存视图」定为每个后端的必备契约，注册时即校验，把缺实现的问题挡在 import 阶段。

**练习 3**：若线上怀疑某插件注意力后端导致结果异常，如何快速回退到内置实现？

**答案**：设置环境变量 `DISABLE_PLUGIN_BACKENDS=<后端名>`（逗号分隔可多个）后重启服务。`_is_plugin_disabled` 命中即跳过覆盖并恢复内置路径（`apply_plugin_overrides` L263-L269），无需卸载插件包。

## 5. 综合实践

**任务：编写一份「NPUPlatform 平台适配清单」。**

这是本讲规格中规定的核心实践。目标是把 `NPUPlatform` 的全部类方法整理成一张「方法 → vLLM 调用时机 → 返回值/效果」的清单，至少覆盖 worker 类、block_size、communicator、图后端四项。

**步骤**：

1. 通读 [components/omni-npu/src/omni_npu/platform.py:L52-L239](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-npu/src/omni_npu/platform.py#L52-L239)，为每个方法补一行注释标注调用时机。判断依据有三：方法自身 docstring（如 `pre_register_and_update` L132-L139 明说「在 VllmConfig 初始化前调用」）、vLLM Platform 接口约定、以及在容器内 `vllm` 包源码（`vllm/platforms/interface.py` 与其调用点）中搜索方法名验证。标注不确定的写「待确认」。
2. 完成下表（参考答案已给出前四项）：

| 适配项 | 来源方法（platform.py 行号） | 返回值 / 效果 |
| --- | --- | --- |
| worker 类 | check_and_update_config（L147-L148） | `omni_npu.worker.npu_worker.NPUWorker` |
| block_size | check_and_update_config（L150-L152） | 未指定时补 128 |
| communicator | get_device_communicator_cls（L170-L173） | `omni_npu.distributed.communicator.NPUCommunicator`（HCCL） |
| 图后端 | get_static_graph_wrapper_cls / get_compile_backend / get_pass_manager_cls（L206-L225） | `ACLGraphWrapper` / `NpuGraphExAdaptor` / `GraphPassManager` |
| dist_backend | （你来填） | （你来填） |
| 注意力后端 | （你来填，含三分支条件） | （你来填） |
| LoRA punica | （你来填） | （你来填） |

3. 在容器内用一条命令抽查验证（示例代码）：`python -c "from omni_npu.platform import NPUPlatform as P; print(P.get_device_communicator_cls()); print(P.get_compile_backend())"`，与清单对照。
4. 把清单保存进团队文档，并注明 HEAD（`2c16b67`）——本讲的永久链接均锚定此提交。

**预期结果**：清单中每一项都能给出「行号 + 返回值 + 一句调用时机说明」；容器抽查输出与清单一致。第 3 步的实际输出**待本地验证**。

## 6. 本讲小结

- `NPUPlatform` 用 6 个类属性完成设备声明：`device_type="npu"`、`dispatch_key="PrivateUse1"`、`dist_backend="hccl"`、`device_control_env_var="ASCEND_RT_VISIBLE_DEVICES"` 等，是 vLLM 识别 NPU 的「身份证」。
- `check_and_update_config` 是平台对 vLLM 默认配置的集中改写点：替换 worker 类为 `NPUWorker`、block_size 兜底 128、关闭三个 CUDA 融合 pass、整图模式下清空 splitting_ops；图编译细节委托 `ConfigUpdater`。
- `pre_register_and_update` 在配置解析前注册 NPU 自定义层，`is_cuda_alike` 用调用栈定向放行两处 CUDA 分支——两者都是零侵入适配的关键技巧。
- `NPUCommunicator` 继承 `CudaCommunicator` 复用簿记，把 all_reduce/all_gather/reduce_scatter/all_to_all 等原语转调 torch.distributed（HCCL 后端）；变长 `all_gatherv` 用循环 broadcast 模拟。
- 注意力后端经 `get_attn_backend_cls` 三分支（NPUDSA/NPUMLA/VLLM_NPU_ATTN）解析出名字后查 `NPU_ATTENTION_BACKEND` 注册表；entry point `omni.attention_backends` 可同名覆盖内置实现，`DISABLE_PLUGIN_BACKENDS` 是逃生门。

## 7. 下一步学习建议

`check_and_update_config` 把 worker 类改成了 `NPUWorker`——下一讲 **u2-l3（NPUWorker 与 NPUModelRunner 生命周期）** 就顺着这条线走：追踪 `init_device` → 分布式初始化 → 模型加载 → KV 传输初始化的完整时序，弄清本讲替换进来的 worker 如何使用这里的 HCCL 通信器。若想先横向补课：图编译相关的 `ACLGraphWrapper` / `NpuGraphExAdaptor` 留待 **u5-l2**，稀疏注意力后端 `NPUDSABackend` 的内部实现留待 **u3-l2**；补丁机制（`omni_npu_patches` 插件）则在 **u2-l4** 讲解。
